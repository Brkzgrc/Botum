# -*- coding: utf-8 -*-
"""SPOT_SCANNER production service.

Yeni karar hattı:
  Binance Spot -> Python geniş ön tarama -> Gemini 3.5 Flash-Lite eleme
  -> Claude Sonnet final karar -> Portfolio Tracker + Telegram.

Eski 15M score + 1H confirmation karar motoru kaldırıldı. Yalnız servis sözleşmesi,
Portfolio Tracker bağlantısı, Telegram bildirimi, cooldown/state ve health endpoint'leri
korundu. Spot only; otomatik emir yok.
"""
from __future__ import annotations

import html
import json
import logging
import os
import threading
import time
from typing import Any

import requests
from flask import Flask, jsonify

from spot_ai_engine import (
    GEMINI_API_KEY,
    GEMINI_MODEL,
    ANTHROPIC_API_KEY,
    SONNET_MODEL,
    Candidate,
    discover,
    levels,
    now_tr,
    pct,
    sf,
)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TELEGRAM_THREAD_ID = int(os.getenv("SIGNAL_THREAD_ID", "2"))
PORTFOLIO_URL = os.getenv("PORTFOLIO_URL", "").rstrip("/")
PORTFOLIO_TOKEN = os.getenv("PORTFOLIO_TOKEN", "")
DRY_RUN = os.getenv("DRY_RUN", "true").strip().lower() == "true"
SCAN_ON_START = os.getenv("SCAN_ON_START", "true").strip().lower() == "true"
SCAN_INTERVAL_SECONDS = max(300, int(os.getenv("SCAN_INTERVAL_SECONDS", "900")))
ALERT_COOLDOWN_HOURS = float(os.getenv("ALERT_COOLDOWN_HOURS", "4"))
STATE_FILE = os.getenv("SCANNER_STATE_FILE", "/tmp/spot_ai_scanner_state_v1.json")

HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "Botum-AISpotScanner-Service/1.0"})
app = Flask(__name__)
logging.getLogger("werkzeug").setLevel(logging.ERROR)

runtime: dict[str, Any] = {
    "status": "BOOT",
    "strategy": "Python -> Gemini 3.5 Flash-Lite -> Sonnet 5 -> Portfolio Tracker",
    "dry_run": DRY_RUN,
    "last_scan": None,
    "symbols": 0,
    "python_candidates": 0,
    "gemini_candidates": 0,
    "sonnet_calls": 0,
    "signals": 0,
    "btc_regime": None,
    "last_error": None,
    "gemini_model": GEMINI_MODEL,
    "sonnet_model": SONNET_MODEL,
    "gemini_key": bool(GEMINI_API_KEY),
    "anthropic_key": bool(ANTHROPIC_API_KEY),
}


def _load_state() -> dict[str, Any]:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_state(d: dict[str, Any]) -> None:
    try:
        os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_FILE)
    except Exception as exc:
        print(f"[STATE] {exc}", flush=True)


def _alert_due(symbol: str, state: dict[str, Any]) -> bool:
    rec = (state.setdefault("alerts", {}).get(symbol) or {})
    return time.time() - sf(rec.get("sent_at")) >= ALERT_COOLDOWN_HOURS * 3600


def _portfolio_payload(c: Candidate, decision: dict[str, Any]) -> dict[str, Any]:
    lv = levels(c, decision)
    p, stop, tp1 = lv["price"], lv["stop"], lv["tp1"]
    stop_pct = max(0.0, pct(p, stop))
    target_pct = max(0.0, pct(tp1, p))
    rr = target_pct / stop_pct if stop_pct else 0.0
    return {
        "symbol": c.symbol.replace("USDT", "/USDT"),
        "entry": round(p, 10),
        "limit_price": round(p, 10),
        "signal_price": round(p, 10),
        "stop": round(stop, 10),
        "tp1": round(tp1, 10),
        "tp2": round(lv["tp2"], 10),
        "tp3": None,
        "sig_type": "spot_opportunity",
        "sub_type": f"ai_{str(decision.get('state', 'candidate')).lower()}",
        "source": "spot-scanner-ai",
        "phase": "manual_review",
        "entry_zone": [round(lv["entry_low"], 10), round(lv["entry_high"], 10)],
        "target_pct": round(target_pct, 2),
        "stop_pct": round(stop_pct, 2),
        "rr": round(rr, 2),
        "setup": decision.get("state"),
        "positives": [decision.get("thesis", ""), decision.get("why_now", "")],
        "risks": decision.get("risk_flags", []),
        "ai_pipeline": "python>gemini-3.5-flash-lite>sonnet",
        "python_rank_score": c.rank_score,
        "python_setup_hint": c.setup_hint,
        "gemini_model": GEMINI_MODEL,
        "gemini_verdict": c.gemini.get("verdict"),
        "gemini_quality": c.gemini.get("quality"),
        "gemini_state": c.gemini.get("state"),
        "gemini_reason": c.gemini.get("reason"),
        "gemini_risk": c.gemini.get("risk"),
        "sonnet_model": SONNET_MODEL,
        "sonnet_decision": decision.get("decision"),
        "sonnet_confidence": decision.get("confidence"),
        "sonnet_state": decision.get("state"),
        "sonnet_usage": decision.get("usage"),
    }


def _send_portfolio(c: Candidate, decision: dict[str, Any]) -> str:
    if not PORTFOLIO_URL:
        return ""
    headers = {"Content-Type": "application/json"}
    if PORTFOLIO_TOKEN:
        headers["Authorization"] = f"Bearer {PORTFOLIO_TOKEN}"
    try:
        r = HTTP.post(f"{PORTFOLIO_URL}/api/signal", json=_portfolio_payload(c, decision), headers=headers, timeout=15)
        if r.status_code in (200, 201):
            try:
                return str((r.json() or {}).get("id", "")) or "ok"
            except Exception:
                return "ok"
        if r.status_code == 409:
            return "duplicate"
        print(f"[PORTFOLIO] HTTP {r.status_code}: {r.text[:220]}", flush=True)
    except Exception as exc:
        print(f"[PORTFOLIO] {exc}", flush=True)
    return ""


def _fmt(v: float) -> str:
    v = sf(v)
    if v >= 1000: return f"{v:,.2f}"
    if v >= 100: return f"{v:.2f}"
    if v >= 1: return f"{v:.4f}"
    if v >= .01: return f"{v:.6f}"
    return f"{v:.10f}".rstrip("0")


def _telegram_text(c: Candidate, decision: dict[str, Any]) -> str:
    lv = levels(c, decision)
    risks = decision.get("risk_flags") or []
    risk_text = "\n".join(f"• {html.escape(str(x))}" for x in risks[:3]) or "• Ek risk notu yok; yapısal stop izlenir."
    thesis = html.escape(str(decision.get("thesis", "")))
    why = html.escape(str(decision.get("why_now", "")))
    return (
        f"<b>🚨 #{c.base} AI SPOT SİNYALİ</b>\n"
        f"🕐 {now_tr().strftime('%d/%m/%Y %H:%M')}\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"<b>Karar:</b> {decision['decision']} | <b>Güven:</b> %{sf(decision['confidence']):.0f}\n"
        f"<b>Durum:</b> {decision['state']}\n"
        f"<b>Gemini:</b> {c.gemini.get('verdict')} %{sf(c.gemini.get('quality')):.0f}\n\n"
        f"<b>Neden?</b>\n{thesis}\n\n"
        f"<b>Neden şimdi?</b>\n{why}\n\n"
        f"<b>Fiyat:</b> {_fmt(lv['price'])}\n"
        f"<b>Giriş bölgesi:</b> {_fmt(lv['entry_low'])} – {_fmt(lv['entry_high'])}\n"
        f"<b>Yapısal stop:</b> {_fmt(lv['stop'])}\n"
        f"<b>TP1:</b> {_fmt(lv['tp1'])}\n"
        f"<b>TP2:</b> {_fmt(lv['tp2'])}\n\n"
        f"<b>Riskler</b>\n{risk_text}\n\n"
        "<i>Spot only • Otomatik emir yok • Portfolio Tracker kaydı</i>"
    )


def _send_telegram(c: Candidate, decision: dict[str, Any]) -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    payload: dict[str, Any] = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": _telegram_text(c, decision),
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if TELEGRAM_THREAD_ID:
        payload["message_thread_id"] = TELEGRAM_THREAD_ID
    try:
        r = HTTP.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json=payload, timeout=15)
        if not r.ok:
            print(f"[TELEGRAM] HTTP {r.status_code}: {r.text[:180]}", flush=True)
        return r.ok
    except Exception as exc:
        print(f"[TELEGRAM] {exc}", flush=True)
        return False


def scan_cycle() -> None:
    runtime.update({"status": "SCANNING", "last_error": None, "signals": 0})
    state = _load_state()
    try:
        if not GEMINI_API_KEY:
            raise RuntimeError("GEMINI_API_KEY/GOOGLE_API_KEY eksik")
        if not ANTHROPIC_API_KEY:
            raise RuntimeError("ANTHROPIC_API_KEY eksik")

        finals, stats = discover()
        runtime.update({
            "symbols": stats.get("universe", 0),
            "python_candidates": stats.get("python", 0),
            "gemini_candidates": stats.get("gemini", 0),
            "sonnet_calls": stats.get("sonnet", 0),
            "btc_regime": stats.get("btc_regime"),
        })

        sent = 0
        for c, decision in finals:
            if not _alert_due(c.symbol, state):
                print(f"[COOLDOWN] {c.symbol} tekrar sinyali bastırıldı", flush=True)
                continue
            lv = levels(c, decision)
            print(
                f"[AI SIGNAL] {c.symbol} Gemini={c.gemini.get('quality')} Sonnet={decision.get('confidence')} "
                f"entry={lv['price']} stop={lv['stop']} tp1={lv['tp1']}",
                flush=True,
            )
            if DRY_RUN:
                print(f"[DRY-RUN] {c.symbol} Portfolio/Telegram gönderilmedi", flush=True)
                emitted = True
            else:
                pid = _send_portfolio(c, decision)
                tg = _send_telegram(c, decision)
                emitted = bool(pid or tg)
                if not pid:
                    print(f"[PORTFOLIO] {c.symbol} kaydı oluşmadı", flush=True)
            if emitted:
                state.setdefault("alerts", {})[c.symbol] = {
                    "sent_at": time.time(), "price": lv["price"], "stop": lv["stop"], "tp1": lv["tp1"],
                    "gemini_quality": c.gemini.get("quality"), "sonnet_confidence": decision.get("confidence"),
                }
                sent += 1

        _save_state(state)
        runtime.update({"status": "RUNNING", "last_scan": now_tr().isoformat(), "signals": sent})
        print(
            f"[SCAN DONE] evren={stats.get('universe')} python={stats.get('python')} gemini={stats.get('gemini')} "
            f"sonnet={stats.get('sonnet')} sinyal={sent} BTC={stats.get('btc_regime')} süre={stats.get('duration_s')}s",
            flush=True,
        )
    except Exception as exc:
        runtime.update({"status": "ERROR", "last_error": f"{type(exc).__name__}: {exc}", "last_scan": now_tr().isoformat()})
        print(f"[SCAN ERROR] {type(exc).__name__}: {exc}", flush=True)
        _save_state(state)


def scan_loop() -> None:
    if not SCAN_ON_START:
        time.sleep(SCAN_INTERVAL_SECONDS)
    while True:
        scan_cycle()
        time.sleep(SCAN_INTERVAL_SECONDS)


@app.route("/")
def index():
    return jsonify({"service": "SPOT_SCANNER", "engine": "AI opportunity discovery", **runtime})


@app.route("/health")
def health():
    return jsonify(runtime), 200


if __name__ == "__main__":
    print("=" * 72, flush=True)
    print("SPOT_SCANNER — AI OPPORTUNITY DISCOVERY", flush=True)
    print(f"Pipeline: Python -> {GEMINI_MODEL} -> {SONNET_MODEL} -> Portfolio Tracker", flush=True)
    print(f"DRY_RUN={DRY_RUN} | scan={SCAN_INTERVAL_SECONDS}s", flush=True)
    print(f"Gemini key={'OK' if GEMINI_API_KEY else 'MISSING'} | Anthropic key={'OK' if ANTHROPIC_API_KEY else 'MISSING'}", flush=True)
    print("=" * 72, flush=True)
    threading.Thread(target=scan_loop, daemon=True, name="ai-spot-scanner").start()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")), threaded=True)
