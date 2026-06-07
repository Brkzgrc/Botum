# -*- coding: utf-8 -*-
"""
Portföy Takip Sistemi v2.6
===========================
v2.5 + SMC yarı çıkış: SMC sinyallerinde TP1'de %50 kapatılır,
      kalan %50 TP2'ye veya stop'a kadar takip edilir.

NOT: Bu dosya geliştirme referansı içindir.
     Değişiklikleri gerçek portfolio-tracker reposuna manuel kopyala.
"""

import json
import os
import time
import threading
from datetime import datetime, timedelta, timezone
from collections import defaultdict

import requests
from flask import Flask, request, jsonify
from news_watcher import start_news_watcher
from market_analyzer import start_market_analyzer
from claude_analyzer import process_and_send as _analyzer_process, start_market_watcher as _start_market_watcher
from intraday_scanner import start_intraday_scanner

TR_TZ = timezone(timedelta(hours=3))
DATA_DIR = os.getenv("DATA_DIR", "/tmp")
SIGNALS_FILE = os.path.join(DATA_DIR, "portfolio_signals.json")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "300"))
EXPIRE_HOURS = int(os.getenv("EXPIRE_HOURS", "48"))
SHADOW_EXPIRE_HOURS = int(os.getenv("SHADOW_EXPIRE_HOURS", "72"))
AUTH_TOKEN   = os.getenv("PORTFOLIO_AUTH_TOKEN", "")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPO  = "brkzgrc/Botum"
GITHUB_FILE  = "portfolio_snapshot.json"
BINANCE_KLINE_URL = "https://api.binance.com/api/v3/klines"
EXPIRE_TRAIL_THRESHOLD = float(os.getenv("EXPIRE_TRAIL_THRESHOLD", "0.80"))
EXPIRE_TRAIL_PCT = float(os.getenv("EXPIRE_TRAIL_PCT", "2.0"))
TRAIL_PCT  = 3.0   # bot.py TRAILING_PCT ile eşleşir — peak'in %3 altında kapanır
SIM_TP1    = 5.0   # Hayali senaryo parametreleri (sabit)
SIM_TP2    = 10.0
SIM_STOP   = -2.5

app = Flask(__name__)

import logging
logging.getLogger("werkzeug").setLevel(logging.ERROR)

signals_db = []
_lock = threading.Lock()

def load_signals():
    global signals_db
    try:
        if os.path.exists(SIGNALS_FILE):
            with open(SIGNALS_FILE, "r", encoding="utf-8") as f:
                signals_db = json.load(f)
            print(f"[DB] {len(signals_db)} sinyal yüklendi.", flush=True)
        else:
            signals_db = []
    except Exception as e:
        print(f"[DB] Yükleme hatası: {e}", flush=True)
        signals_db = []

def save_signals():
    try:
        with open(SIGNALS_FILE, "w", encoding="utf-8") as f:
            json.dump(signals_db[-2000:], f, ensure_ascii=False, default=str, indent=None)
    except Exception as e:
        print(f"[DB] Kayıt hatası: {e}", flush=True)

def tr_now():
    return datetime.now(timezone.utc).astimezone(TR_TZ)

def tr_now_str():
    return tr_now().strftime("%Y-%m-%d %H:%M:%S")

# ============================================================
# SİNYAL ALMA ENDPOINT'İ
# ============================================================
@app.route("/api/signal", methods=["POST"])
def receive_signal():
    if AUTH_TOKEN:
        token = request.headers.get("Authorization", "").replace("Bearer ", "")
        if token != AUTH_TOKEN:
            return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(force=True, silent=True)
    if not data:
        return jsonify({"error": "no json body"}), 400

    required = ["symbol", "entry", "stop", "tp1"]
    for field in required:
        if field not in data:
            return jsonify({"error": f"missing field: {field}"}), 400

    # ── AYNI SEMBOLDE AÇIK POZİSYON KONTROLÜ ──
    with _lock:
        for s in signals_db:
            if s.get("symbol") == data["symbol"] and s.get("status") == "open" and s.get("source") == data.get("source", "bot"):
                print(f"[SİNYAL] REDDEDILDI: {data['symbol']} zaten açık pozisyonda", flush=True)
                return jsonify({"error": "already open", "symbol": data["symbol"]}), 409

    now = tr_now()
    signal = {
        "id": f"{data['symbol'].replace('/', '_')}_{int(now.timestamp())}",
        "symbol": data["symbol"],
        "entry": float(data["entry"]),
        "stop": float(data["stop"]),
        "tp1": float(data["tp1"]),
        "tp2": float(data.get("tp2", 0)) or None,
        "sig_type": data.get("sig_type", data.get("type", "unknown")),
        "sub_type": data.get("sub_type", data.get("subtype", data.get("tp_system", ""))),
        "source": data.get("source", "bot"),
        "phase": data.get("phase", ""),
        "candle": data.get("candle", ""),
        "funding_neg": data.get("funding_neg", False),
        "status": "open",
        "open_time": now.isoformat(),
        "close_time": None, "close_price": None, "close_reason": None, "close_pct": None,
        "peak_price": float(data["entry"]), "peak_pct": 0.0,
        "low_price": float(data["entry"]), "low_pct": 0.0,
        "current_price": float(data["entry"]), "current_pct": 0.0,
        "tp1_hit": False, "tp1_time": None,
        "tp2_shadow": "watching", "tp2_hit": False, "tp2_time": None,
        "tp2_peak_after_tp1": 0.0, "tp2_shadow_end": None,
        "trailing_shadow": "watching", "trailing_peak": 0.0,
        "trailing_stop_pct": 2.0, "trailing_exit_price": None,
        "trailing_exit_pct": None, "trailing_shadow_end": None,
        # expire trailing alanları
        "expire_trailing_active": False,
        "expire_trailing_peak": 0.0,
        "expire_trailing_stop_pct": EXPIRE_TRAIL_PCT,
        "expire_trailing_exit_price": None,
        "expire_trailing_exit_pct": None,
        # analyzer
        "analyzer_decision": None, "analyzer_time": None,
        "last_check": now.isoformat(), "checks": 0,
        "extra": {k: v for k, v in data.items() if k not in required + [
            "sig_type", "type", "sub_type", "subtype", "tp_system",
            "source", "phase", "candle", "funding_neg", "tp2"
        ]},
    }

    with _lock:
        signals_db.insert(0, signal)
        save_signals()

    print(f"[SİNYAL] {signal['sig_type'].upper()} | {signal['symbol']} | "
          f"Giriş: {signal['entry']} | Kaynak: {signal['source']}", flush=True)
    return jsonify({"ok": True, "id": signal["id"]}), 201

# ============================================================
# BİNANCE FİYAT KONTROLÜ
# ============================================================
def get_current_price_hl(symbol):
    pair = symbol.replace("/", "").replace("USDT", "USDT")
    try:
        r = requests.get(BINANCE_KLINE_URL, params={
            "symbol": pair, "interval": "5m", "limit": 1
        }, timeout=10)
        if r.status_code == 200:
            data = r.json()
            if data:
                k = data[0]
                return {"high": float(k[2]), "low": float(k[3]), "close": float(k[4])}
    except Exception as e:
        print(f"[BINANCE] {symbol} hata: {e}", flush=True)
    return None

# ============================================================
# POZİSYON KONTROL DÖNGÜSÜ
# ============================================================
def check_open_positions():
    now = tr_now()
    with _lock:
        active = [s for s in signals_db
                  if s["status"] in ("open", "half_open") or s.get("tp2_shadow") == "watching"]
    if not active:
        return

    open_count = sum(1 for s in active if s["status"] in ("open", "half_open"))
    shadow_count = sum(1 for s in active if s["status"] != "open" and s.get("tp2_shadow") == "watching")
    print(f"[CHECK] {open_count} açık + {shadow_count} shadow takip...", flush=True)

    closed_count = 0
    need_save = False

    for sig in active:
        symbol = sig["symbol"]
        price_data = get_current_price_hl(symbol)
        if not price_data:
            continue
        high = price_data["high"]; low = price_data["low"]; close = price_data["close"]
        entry = sig["entry"]

        if sig["status"] == "open":
            stop = sig["stop"]; tp1 = sig["tp1"]; tp2 = sig.get("tp2")
            if high > sig["peak_price"]:
                sig["peak_price"] = high
                sig["peak_pct"] = round((high - entry) / entry * 100, 2)
            if low < sig["low_price"]:
                sig["low_price"] = low
                sig["low_pct"] = round((low - entry) / entry * 100, 2)
            sig["current_price"] = close
            sig["current_pct"] = round((close - entry) / entry * 100, 2)
            sig["last_check"] = now.isoformat()
            sig["checks"] = sig.get("checks", 0) + 1

            is_smc = sig.get("source", "bot") in ("smc", "smc-original", "smc-trailing", "smc-momentum")
            close_reason = None; close_price = None

            if is_smc:
                # SMC: stop kontrolü önce, TP1'de yarı çıkış
                if low <= stop:
                    close_reason = "stop"; close_price = stop
                    sig["status"] = "loss"
                    sig["tp2_shadow"] = "not_reached"
                elif high >= tp1 and tp2:
                    tp1_pct_v = round((tp1 - entry) / entry * 100, 2)
                    sig["status"] = "half_open"
                    sig["tp1_hit"] = True; sig["tp1_time"] = now.isoformat()
                    sig["tp1_exit_price"] = round(tp1, 8)
                    sig["tp1_exit_pct"] = tp1_pct_v
                    close_reason = None
                    need_save = True
                    print(f"  🎯 TP1 YARI ÇIKIŞ: {symbol.replace('/USDT','')} | +{tp1_pct_v}% | TP2 takipte", flush=True)
                else:
                    open_time = datetime.fromisoformat(sig["open_time"])
                    if open_time.tzinfo is None: open_time = open_time.replace(tzinfo=TR_TZ)
                    if (now - open_time).total_seconds() / 3600 >= EXPIRE_HOURS:
                        close_reason = "expired"; close_price = close; sig["status"] = "expired"
            else:
                # Bot sinyalleri: trailing stop primary exit (bot.py ile eşleşir)
                trail_stop_price = round(sig["peak_price"] * (1 - TRAIL_PCT / 100), 8)
                sig["trail_stop"] = trail_stop_price

                if tp1 and high >= tp1 and not sig.get("tp1_hit"):
                    sig["tp1_hit"] = True; sig["tp1_time"] = now.isoformat()
                    need_save = True
                    print(f"  🏁 TP1 MİLESTONE: {symbol.replace('/USDT','')} | +{round((tp1-entry)/entry*100,1)}% | devam", flush=True)

                if tp2 and high >= tp2:
                    close_reason = "tp2"; close_price = tp2
                    sig["status"] = "win_tp2"
                elif low <= trail_stop_price:
                    trail_ret = round((trail_stop_price - entry) / entry * 100, 2)
                    close_reason = "trailing"; close_price = trail_stop_price
                    sig["status"] = "win_trail" if trail_ret > 0 else "loss"
                else:
                    open_time = datetime.fromisoformat(sig["open_time"])
                    if open_time.tzinfo is None: open_time = open_time.replace(tzinfo=TR_TZ)
                    if (now - open_time).total_seconds() / 3600 >= EXPIRE_HOURS:
                        close_reason = "expired"; close_price = close; sig["status"] = "expired"

            if close_reason:
                sig["close_time"] = now.isoformat()
                sig["close_price"] = round(close_price, 8)
                sig["close_reason"] = close_reason
                sig["close_pct"] = round((close_price - entry) / entry * 100, 2)
                closed_count += 1; need_save = True
                emoji = {"tp2": "🟢", "trailing": ("💰" if sig["close_pct"] > 0 else "🔴"),
                         "stop": "🔴", "expired": "⏰"}.get(close_reason, "⚪")
                print(f"  {emoji} KAPANDI: {symbol} | {close_reason.upper()} | "
                      f"{sig['close_pct']:+.2f}% | Peak: {sig['peak_pct']:+.2f}%", flush=True)

        elif sig["status"] == "half_open":
            # SMC yarı çıkış — TP1'de %50 kapatıldı, TP2 veya stop'a kadar takip
            tp2 = sig.get("tp2"); stop = sig["stop"]
            tp1_exit_pct = sig.get("tp1_exit_pct", 0)
            if high > sig["peak_price"]:
                sig["peak_price"] = high
                sig["peak_pct"] = round((high - entry) / entry * 100, 2)
                sig["tp2_peak_after_tp1"] = sig["peak_pct"]
            if low < sig["low_price"]:
                sig["low_price"] = low
                sig["low_pct"] = round((low - entry) / entry * 100, 2)
            sig["current_price"] = close
            sig["current_pct"] = round((close - entry) / entry * 100, 2)
            sig["last_check"] = now.isoformat()
            sig["checks"] = sig.get("checks", 0) + 1

            if tp2 and high >= tp2:
                tp2_pct = round((tp2 - entry) / entry * 100, 2)
                combined_pct = round((tp1_exit_pct + tp2_pct) / 2, 2)
                sig["status"] = "win_tp2"
                sig["tp2_hit"] = True; sig["tp2_time"] = now.isoformat()
                sig["tp2_shadow"] = "hit"
                sig["close_time"] = now.isoformat()
                sig["close_price"] = round(tp2, 8)
                sig["close_reason"] = "tp2"
                sig["close_pct"] = combined_pct
                need_save = True; closed_count += 1
                print(f"  🎯🎯 TP2 KAPANDI: {symbol.replace('/USDT','')} | TP2:+{tp2_pct}% | Ort:+{combined_pct}%", flush=True)
            elif low <= stop:
                stop_pct = round((stop - entry) / entry * 100, 2)
                combined_pct = round((tp1_exit_pct + stop_pct) / 2, 2)
                sig["status"] = "half_stopped"
                sig["close_time"] = now.isoformat()
                sig["close_price"] = round(stop, 8)
                sig["close_reason"] = "stop_after_tp1"
                sig["close_pct"] = combined_pct
                sig["tp2_shadow"] = "stopped"
                need_save = True; closed_count += 1
                emoji2 = "💰" if combined_pct > 0 else "🔴"
                print(f"  {emoji2} YARIM STOP: {symbol.replace('/USDT','')} | TP1:+{tp1_exit_pct}% Stop:{stop_pct:+.2f}% | Ort:{combined_pct:+.2f}%", flush=True)
            else:
                tp1_time_str = sig.get("tp1_time", sig["open_time"])
                try:
                    tp1_dt = datetime.fromisoformat(tp1_time_str)
                    if tp1_dt.tzinfo is None: tp1_dt = tp1_dt.replace(tzinfo=TR_TZ)
                    elapsed_since_tp1 = (now - tp1_dt).total_seconds() / 3600
                except Exception:
                    elapsed_since_tp1 = 0
                if elapsed_since_tp1 >= SHADOW_EXPIRE_HOURS:
                    close_pct_now = round((close - entry) / entry * 100, 2)
                    combined_pct = round((tp1_exit_pct + close_pct_now) / 2, 2)
                    sig["status"] = "half_expired"
                    sig["close_time"] = now.isoformat()
                    sig["close_price"] = round(close, 8)
                    sig["close_reason"] = "expired_after_tp1"
                    sig["close_pct"] = combined_pct
                    sig["tp2_shadow"] = "missed"
                    need_save = True; closed_count += 1
                    print(f"  ⏰ YARIM EXPİRE: {symbol.replace('/USDT','')} | Ort:{combined_pct:+.2f}%", flush=True)

        time.sleep(0.15)

    if need_save or closed_count > 0:
        with _lock:
            save_signals()
        if closed_count > 0:
            print(f"[CHECK] {closed_count} pozisyon kapandı.", flush=True)

def position_checker_loop():
    while True:
        try:
            check_open_positions()
        except Exception as e:
            print(f"[CHECK] Döngü hatası: {e}", flush=True)
        time.sleep(CHECK_INTERVAL)

# ============================================================
# PERFORMANS HESAPLAMA
# ============================================================
def calc_performance():
    with _lock:
        all_sigs = list(signals_db)

    result = {
        "total": len(all_sigs), "open": 0, "closed": 0,
        "wins": 0, "win_partial": 0, "losses": 0, "expired": 0, "tp1_hits": 0,
        "total_pnl": 0.0, "avg_peak": 0.0, "win_rate": 0.0,
        "analyzer": {
            "gir":     {"total": 0, "wins": 0, "losses": 0, "pnl": 0.0},
            "dikkat":  {"total": 0, "wins": 0, "losses": 0, "pnl": 0.0},
            "riskli":  {"total": 0, "wins": 0, "losses": 0, "pnl": 0.0},
        },
        "sim_bot":  {"tp2": 0, "tp1": 0, "stop": 0, "open": 0, "pnl": 0.0},
        "sim_smc":  {"tp2": 0, "tp1": 0, "stop": 0, "open": 0, "pnl": 0.0},
        "smc_alt": {
            "actual":   {"wins": 0, "losses": 0, "expired": 0, "total": 0, "pnl": 0.0},
            "tp1_only": {"wins": 0, "losses": 0, "expired": 0, "total": 0, "pnl": 0.0},
            "tp2_only": {"wins": 0, "losses": 0, "expired": 0, "total": 0, "pnl": 0.0},
        },
        "bot_alt": {
            "actual":   {"wins": 0, "losses": 0, "expired": 0, "total": 0, "pnl": 0.0},
            "tp1_only": {"wins": 0, "losses": 0, "expired": 0, "total": 0, "pnl": 0.0},
        },
        "by_type": {}, "daily": {}, "weekly": {}, "monthly": {},
    }

    closed_peaks = []
    type_stats = defaultdict(lambda: {
        "total": 0, "open": 0, "wins": 0, "win_partial": 0, "losses": 0, "expired": 0,
        "tp1_hits": 0, "total_pnl": 0.0, "peaks": [],
        "tp2_hits": 0, "tp2_total": 0, "tp2_extra_pnl": 0.0,
        "tp2_stopped": 0,
    })

    for sig in all_sigs:
        status = sig.get("status", "open")
        sig_type = sig.get("sig_type", "unknown")
        sub = sig.get("sub_type", "")
        source = sig.get("source", "bot")

        if source == "smc-trailing":
            phase = sig.get('phase', '')
            phase_label = "Discount" if phase == "discount" else ("CHoCH" if phase == "choch" else phase.replace('phase', 'P'))
            type_key = f"SMC-T {phase_label}"
        elif source == "smc-momentum":
            phase = sig.get('phase', '')
            phase_label = "Discount" if phase == "discount" else ("CHoCH" if phase == "choch" else phase.replace('phase', 'P'))
            type_key = f"SMC-M {phase_label}"
        elif source in ("smc", "smc-original"):
            phase = sig.get('phase', '')
            phase_label = "Discount" if phase == "discount" else ("CHoCH" if phase == "choch" else phase.replace('phase', 'P'))
            type_key = f"SMC {phase_label}"
        elif sig_type == "tp":
            type_key = f"TP-{sub.capitalize()}" if sub else "TP"
        else:
            type_key = sig_type.upper()

        ts = type_stats[type_key]
        ts["total"] += 1

        if status in ("open", "half_open"):
            result["open"] += 1; ts["open"] += 1
        else:
            result["closed"] += 1
            pct = sig.get("close_pct", 0) or 0
            result["total_pnl"] += pct; ts["total_pnl"] += pct
            if sig.get("tp1_hit"): result["tp1_hits"] += 1; ts["tp1_hits"] += 1
            peak = sig.get("peak_pct", 0)
            closed_peaks.append(peak); ts["peaks"].append(peak)
            if status in ("win_tp1", "win_tp2", "win_trail"):
                result["wins"] += 1; ts["wins"] += 1
            elif status == "win_partial":
                result["wins"] += 1; result["win_partial"] += 1
                ts["wins"] += 1; ts["win_partial"] += 1
            elif status == "half_stopped":
                if sig.get("close_pct", 0) > 0:
                    result["wins"] += 1; ts["wins"] += 1
                else:
                    result["losses"] += 1; ts["losses"] += 1
            elif status == "half_expired":
                result["expired"] += 1; ts["expired"] += 1
            elif status == "loss": result["losses"] += 1; ts["losses"] += 1
            elif status == "expired": result["expired"] += 1; ts["expired"] += 1

            # analyzer istatistikleri (sadece kapanmış sinyaller)
            ad = sig.get("analyzer_decision", "")
            if ad and status not in ("open", "half_open"):
                if "✅" in ad:   bucket_key = "gir"
                elif "⚠️" in ad: bucket_key = "dikkat"
                elif "🚫" in ad: bucket_key = "riskli"
                else:            bucket_key = None
                if bucket_key:
                    ab = result["analyzer"][bucket_key]
                    ab["total"] += 1; ab["pnl"] += pct
                    if status in ("win_tp1", "win_tp2", "win_trail", "win_partial"): ab["wins"] += 1
                    elif status in ("loss", "half_stopped"):                          ab["losses"] += 1

        # Günlük / haftalık / aylık istatistikleri
        _pct_for_time = (sig.get("close_pct", 0) or 0) if status not in ("open", "half_open") else 0
        open_time_str = sig.get("open_time", "")
        if open_time_str:
            try:
                dt = datetime.fromisoformat(open_time_str)
                day_key = dt.strftime("%Y-%m-%d")
                week_key = dt.strftime("%Y-W%W")
                month_key = dt.strftime("%Y-%m")
                for _tb, _tk in [(result["daily"], day_key),
                                  (result["weekly"], week_key),
                                  (result["monthly"], month_key)]:
                    if _tk not in _tb:
                        _tb[_tk] = {"trades": 0, "pnl": 0.0, "wins": 0, "losses": 0}
                    _tb[_tk]["trades"] += 1; _tb[_tk]["pnl"] += _pct_for_time
                    if status in ("win_tp1", "win_partial"): _tb[_tk]["wins"] += 1
                    elif status == "loss": _tb[_tk]["losses"] += 1
            except Exception: pass

        # Alternatif senaryo hesabı (sadece kapanmış sinyaller)
        if status not in ("open", "half_open"):
            _is_smc = source in ("smc", "smc-original", "smc-trailing", "smc-momentum")
            _entry = sig.get("entry", 0) or 0
            _tp1p = sig.get("tp1"); _tp2p = sig.get("tp2"); _stopp = sig.get("stop")
            _tp1_pct = round((_tp1p - _entry) / _entry * 100, 2) if _tp1p and _entry else 0
            _tp2_pct = round((_tp2p - _entry) / _entry * 100, 2) if _tp2p and _entry else 0
            _stop_pct = round((_stopp - _entry) / _entry * 100, 2) if _stopp and _entry else 0
            _pk = sig.get("peak_pct", 0) or 0
            _dp = sig.get("low_pct", 0) or 0
            _closed_pct = sig.get("close_pct", 0) or 0

            if _is_smc:
                sa = result["smc_alt"]
                sa["actual"]["total"] += 1
                if status in ("win_tp1", "win_tp2", "win_trail", "win_partial"):
                    sa["actual"]["wins"] += 1; sa["actual"]["pnl"] += _closed_pct
                elif status in ("loss", "half_stopped"):
                    if _closed_pct > 0: sa["actual"]["wins"] += 1
                    else: sa["actual"]["losses"] += 1
                    sa["actual"]["pnl"] += _closed_pct
                else:
                    sa["actual"]["expired"] += 1

                sa["tp1_only"]["total"] += 1
                if _tp1_pct > 0 and _pk >= _tp1_pct:
                    sa["tp1_only"]["wins"] += 1; sa["tp1_only"]["pnl"] += _tp1_pct
                elif _stop_pct < 0 and _dp <= _stop_pct:
                    sa["tp1_only"]["losses"] += 1; sa["tp1_only"]["pnl"] += _stop_pct
                else:
                    sa["tp1_only"]["expired"] += 1

                if _tp2_pct > 0:
                    sa["tp2_only"]["total"] += 1
                    if _pk >= _tp2_pct:
                        sa["tp2_only"]["wins"] += 1; sa["tp2_only"]["pnl"] += _tp2_pct
                    elif _stop_pct < 0 and _dp <= _stop_pct:
                        sa["tp2_only"]["losses"] += 1; sa["tp2_only"]["pnl"] += _stop_pct
                    else:
                        sa["tp2_only"]["expired"] += 1
            else:
                ba = result["bot_alt"]
                ba["actual"]["total"] += 1
                if status in ("win_tp1", "win_tp2", "win_trail", "win_partial"):
                    ba["actual"]["wins"] += 1; ba["actual"]["pnl"] += _closed_pct
                elif status == "loss":
                    ba["actual"]["losses"] += 1; ba["actual"]["pnl"] += _closed_pct
                else:
                    ba["actual"]["expired"] += 1

                ba["tp1_only"]["total"] += 1
                if _tp1_pct > 0 and _pk >= _tp1_pct:
                    ba["tp1_only"]["wins"] += 1; ba["tp1_only"]["pnl"] += _tp1_pct
                elif _stop_pct < 0 and _dp <= _stop_pct:
                    ba["tp1_only"]["losses"] += 1; ba["tp1_only"]["pnl"] += _stop_pct
                else:
                    ba["tp1_only"]["expired"] += 1

    # Hayali senaryo hesabı (sadece kapanmış sinyaller — açık pozisyonlar dahil değil)
    for sig in all_sigs:
        if sig.get("status") in ("open", "half_open"):
            continue
        is_smc = sig.get("source", "bot") in ("smc", "smc-original", "smc-trailing", "smc-momentum")
        bucket = result["sim_smc"] if is_smc else result["sim_bot"]
        pk = sig.get("peak_pct", 0) or 0
        dp = sig.get("low_pct", 0) or 0
        if pk >= SIM_TP2:
            bucket["tp2"] += 1; bucket["pnl"] += SIM_TP2
        elif pk >= SIM_TP1 and dp > SIM_STOP:
            bucket["tp1"] += 1; bucket["pnl"] += SIM_TP1
        elif dp <= SIM_STOP:
            bucket["stop"] += 1; bucket["pnl"] += SIM_STOP
        else:
            bucket["open"] += 1

    if closed_peaks:
        result["avg_peak"] = round(sum(closed_peaks) / len(closed_peaks), 2)
    if result["closed"] > 0:
        result["win_rate"] = round(result["wins"] / result["closed"] * 100, 1)
    result["total_pnl"] = round(result["total_pnl"], 2)
    for bk, bv in result["analyzer"].items():
        dec = bv["wins"] + bv["losses"]
        bv["wr"]  = round(bv["wins"] / dec * 100, 1) if dec > 0 else 0
        bv["pnl"] = round(bv["pnl"], 2)
    for sb in ("sim_bot", "sim_smc"):
        b = result[sb]
        decided = b["tp2"] + b["tp1"] + b["stop"]
        b["wr"]  = round((b["tp2"] + b["tp1"]) / decided * 100, 1) if decided > 0 else 0
        b["pnl"] = round(b["pnl"], 2)

    for ak in ("smc_alt", "bot_alt"):
        for sk in result[ak]:
            s = result[ak][sk]
            dec = s["wins"] + s["losses"]
            s["wr"] = round(s["wins"] / dec * 100, 1) if dec > 0 else 0
            s["pnl"] = round(s["pnl"], 2)

    for tk, ts in type_stats.items():
        closed = ts["wins"] + ts["losses"] + ts["expired"]
        ts["win_rate"] = round(ts["wins"] / closed * 100, 1) if closed > 0 else 0
        ts["avg_peak"] = round(sum(ts["peaks"]) / len(ts["peaks"]), 2) if ts["peaks"] else 0
        ts["total_pnl"] = round(ts["total_pnl"], 2)
        ts["tp2_extra_pnl"] = round(ts["tp2_extra_pnl"], 2)
        ts["tp2_rate"] = round(ts["tp2_hits"] / ts["tp2_total"] * 100, 1) if ts["tp2_total"] > 0 else 0
        del ts["peaks"]

    result["by_type"] = dict(type_stats)
    return result

# ============================================================
# API ENDPOINT'LERİ
# ============================================================
@app.route("/api/analyze", methods=["POST"])
def api_analyze():
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    if AUTH_TOKEN and token != AUTH_TOKEN:
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    signal      = data.get("signal", {})
    recent_count = data.get("recent_count", 0)
    sig_num     = data.get("sig_num", 0)
    portfolio_id = data.get("portfolio_id", "")
    threading.Thread(
        target=_analyzer_process,
        args=(signal, recent_count, sig_num, portfolio_id),
        daemon=True,
    ).start()
    return jsonify({"status": "queued"}), 202


@app.route("/api/health")
def api_health():
    return jsonify({"status": "ok", "time": tr_now_str()})

@app.route("/api/performance")
def api_performance():
    return jsonify(calc_performance())

@app.route("/api/signals")
def api_signals():
    status_filter = request.args.get("status", "all")
    type_filter = request.args.get("type", "all")
    limit = int(request.args.get("limit", "100"))
    with _lock:
        sigs = list(signals_db)
    if status_filter != "all":
        sigs = [s for s in sigs if s.get("status") == status_filter]
    if type_filter != "all":
        sigs = [s for s in sigs if s.get("sig_type") == type_filter]
    return jsonify(sigs[:limit])

@app.route("/api/open")
def api_open():
    with _lock:
        return jsonify([s for s in signals_db if s.get("status") == "open"])

@app.route("/api/signal/<signal_id>/analyzer", methods=["PATCH"])
def update_analyzer(signal_id):
    data = request.get_json(force=True, silent=True)
    if not data or "analyzer_decision" not in data:
        return jsonify({"error": "missing analyzer_decision"}), 400
    with _lock:
        for s in signals_db:
            if s.get("id") == signal_id:
                s["analyzer_decision"] = data["analyzer_decision"]
                s["analyzer_time"]     = tr_now().isoformat()
                save_signals()
                print(f"[ANALYZER] {s['symbol']} → {data['analyzer_decision']}", flush=True)
                return jsonify({"ok": True})
    return jsonify({"error": "not found"}), 404

@app.route("/api/signal/<signal_id>", methods=["DELETE"])
def delete_signal(signal_id):
    if AUTH_TOKEN:
        token = request.headers.get("Authorization", "").replace("Bearer ", "")
        if token != AUTH_TOKEN:
            return jsonify({"error": "unauthorized"}), 401
    with _lock:
        before = len(signals_db)
        signals_db[:] = [s for s in signals_db if s.get("id") != signal_id]
        if len(signals_db) != before:
            save_signals()
            return jsonify({"ok": True, "deleted": signal_id})
        return jsonify({"error": "not found"}), 404

@app.route("/api/signals/clear-test", methods=["POST"])
def clear_test_signals():
    if AUTH_TOKEN:
        token = request.headers.get("Authorization", "").replace("Bearer ", "")
        if token != AUTH_TOKEN:
            return jsonify({"error": "unauthorized"}), 401
    with _lock:
        before = len(signals_db)
        signals_db[:] = [s for s in signals_db if s.get("source") != "test"]
        save_signals()
    return jsonify({"ok": True, "removed": before - len(signals_db)})

@app.route("/api/signals/clear-all", methods=["POST"])
def clear_all_signals():
    if AUTH_TOKEN:
        token = request.headers.get("Authorization", "").replace("Bearer ", "")
        if token != AUTH_TOKEN:
            return jsonify({"error": "unauthorized"}), 401
    with _lock:
        count = len(signals_db)
        signals_db.clear()
        save_signals()
    return jsonify({"ok": True, "removed": count})

@app.route("/api/signals/clear-all-ui", methods=["POST"])
def clear_all_signals_ui():
    with _lock:
        count = len(signals_db)
        signals_db.clear()
        save_signals()
    return jsonify({"ok": True, "removed": count})

# ============================================================
# HTML DASHBOARD
# ============================================================
def fmt_price(p):
    if p is None: return "—"
    p = float(p)
    if p >= 100: return f"{p:.2f}"
    if p >= 1: return f"{p:.3f}"
    if p >= 0.01:   return f"{p:.4f}"
    if p >= 0.0001: return f"{p:.6f}"
    return f"{p:.8f}"

def pct_color(pct):
    if pct is None: return "#8a9bb0", "—"
    pct = float(pct)
    color = "#2ecc71" if pct > 0 else ("#e74c3c" if pct < 0 else "#8a9bb0")
    return color, f"{pct:+.2f}%"

def status_badge(status):
    colors = {
        "open":         ("#3498db", "AÇIK"),
        "half_open":    ("#f39c12", "YARI AÇIK"),
        "win_tp1":      ("#2ecc71", "WIN (TP1)"),
        "win_tp2":      ("#27ae60", "WIN (TP2)"),
        "win_partial":  ("#27ae60", "WIN (TRAIL)"),
        "win_trail":    ("#27ae60", "WIN_TRAIL"),
        "half_stopped": ("#e67e22", "YARIM STOP"),
        "half_expired": ("#e67e22", "YARIM EXP"),
        "loss":         ("#e74c3c", "LOSS"),
        "expired":      ("#f39c12", "EXPIRED"),
    }
    c, label = colors.get(status, ("#8a9bb0", status.upper()))
    return f'<span style="background:{c};color:#0a0e14;padding:2px 8px;border-radius:3px;font-size:.7rem;font-weight:bold;white-space:nowrap">{label}</span>'

def tp2_shadow_badge(sig):
    shadow = sig.get("tp2_shadow", "n/a")
    entry = sig.get("entry", 0)
    m = {"hit": ("#2ecc71", "✅ TP2"), "missed": ("#e74c3c", "❌ MISS"),
         "watching": ("#3498db", "👁 İZLENİYOR"), "stopped": ("#e74c3c", "🔴 STOP")}
    if shadow in m:
        c, l = m[shadow]
        extra = ""
        if shadow == "hit":
            tp2 = sig.get("tp2")
            if tp2 and entry and entry > 0:
                tp2_pct = round((tp2 - entry) / entry * 100, 2)
                extra = f" <span style='color:#a8e6a3;font-size:.58rem'>{fmt_price(tp2)} (+{tp2_pct}%)</span>"
        elif shadow == "stopped":
            stop = sig.get("stop")
            if stop and entry and entry > 0:
                stop_pct = round((stop - entry) / entry * 100, 2)
                extra = f" <span style='color:#f1948a;font-size:.58rem'>{fmt_price(stop)} ({stop_pct:+.2f}%)</span>"
        return f'<span style="background:{c}22;color:{c};padding:1px 6px;border-radius:3px;font-size:.6rem">{l}{extra}</span>'
    return '<span style="color:#5a6a7a;font-size:.6rem">—</span>'

def type_badge(sig):
    sig_type = sig.get("sig_type", "unknown")
    sub = sig.get("sub_type", "")
    source = sig.get("source", "bot")
    if source in ("smc", "smc-original", "smc-trailing", "smc-momentum"):
        phase = sig.get("phase", "")
        phase_label = "Discount" if phase == "discount" else ("CHoCH" if phase == "choch" else phase.replace('phase', 'P'))
        src_label = "SMC-T" if source == "smc-trailing" else ("SMC-M" if source == "smc-momentum" else "SMC")
        return f'<span style="border:1px solid #e67e22;color:#d0d0d0;padding:1px 6px;border-radius:3px;font-size:.65rem;white-space:nowrap">{src_label} {phase_label}</span>'
    colors = {
        "dip":              "#2ecc71",
        "trend":            "#3498db",
        "birikim":          "#9b59b6",
        "tp":               "#e67e22",
        "panik_pump":       "#ff4444",
        "pump_kisa":        "#ff8800",
        "pump_orta":        "#ffcc00",
        "pump_uzun":        "#00cc66",
        "pump_probability": "#0088ff",
        "momentum_devam":   "#00ccaa",
    }
    labels = {
        "panik_pump":       "PANİK PUMP",
        "pump_kisa":        "KISA VADE",
        "pump_orta":        "ORTA VADE (72s)",
        "pump_uzun":        "UZUN VADE (168s)",
        "pump_probability": "PUMP PROB",
        "momentum_devam":   "ROCKET",
    }
    c = colors.get(sig_type, "#8a9bb0")
    label = labels.get(sig_type, sig_type.upper()) + (f" {sub}" if sub else "")
    return f'<span style="border:1px solid {c};color:#d0d0d0;padding:1px 6px;border-radius:3px;font-size:.65rem;white-space:nowrap">{label}</span>'

def analyzer_badge(sig):
    d = sig.get("analyzer_decision") or ""
    if not d:
        return '<span style="color:#5a6a7a;font-size:.6rem">—</span>'
    if "✅" in d:   c, l = "#2ecc71", "✅ GİR"
    elif "⚠️" in d: c, l = "#f39c12", "⚠️ DİKKAT"
    elif "🚫" in d: c, l = "#e74c3c", "🚫 RİSKLİ"
    else:            c, l = "#8a9bb0", d[:12]
    return f'<span style="background:{c}22;color:{c};padding:1px 6px;border-radius:3px;font-size:.6rem">{l}</span>'

@app.route("/")
def dashboard():
    perf = calc_performance()
    now = tr_now_str()
    now_dt = tr_now()

    with _lock:
        all_sigs = list(signals_db)

    open_sigs = [s for s in all_sigs if s.get("status") in ("open", "half_open")]
    closed_sigs = [s for s in all_sigs if s.get("status") not in ("open", "half_open")]
    shadow_watching = [s for s in all_sigs if s.get("tp2_shadow") == "watching" and s.get("status") not in ("open", "half_open")]

    open_rows = ""
    for sig in open_sigs[:50]:
        cur_c, cur_s = pct_color(sig.get("current_pct"))
        peak_c, peak_s = pct_color(sig.get("peak_pct"))
        low_c, low_s = pct_color(sig.get("low_pct"))
        sym = sig["symbol"].replace("/USDT", "")
        tp1_pct = round((sig["tp1"] - sig["entry"]) / sig["entry"] * 100, 1) if sig["entry"] > 0 else 0
        tp2_val = sig.get("tp2")
        tp2_pct_open = round((tp2_val - sig["entry"]) / sig["entry"] * 100, 1) if tp2_val and sig["entry"] > 0 else 0

        # Dinamik trailing stop: peak * %97
        is_smc_sig = sig.get("source", "bot") in ("smc", "smc-original", "smc-trailing", "smc-momentum")
        if not is_smc_sig:
            trail_stop_v = round(sig["peak_price"] * (1 - TRAIL_PCT / 100), 8)
            trail_ret_v  = round((trail_stop_v - sig["entry"]) / sig["entry"] * 100, 1)
            tc = "#f39c12" if trail_ret_v >= 0 else "#e74c3c"
            stop_cell = (f'<span style="background:{tc}22;color:{tc};padding:1px 5px;border-radius:3px;'
                         f'font-size:.6rem;white-space:nowrap">⚡ {fmt_price(trail_stop_v)} ({trail_ret_v:+.2f}%)</span>')
        else:
            stop_pct = round((sig["stop"] - sig["entry"]) / sig["entry"] * 100, 2) if sig["entry"] > 0 else 0
            stop_cell = f"{fmt_price(sig['stop'])} ({stop_pct:+.2f}%)"

        sure_cell = '<span style="font-size:.7rem;color:#7f8c8d">—</span>'
        try:
            ot = datetime.fromisoformat(sig["open_time"])
            if ot.tzinfo is None: ot = ot.replace(tzinfo=TR_TZ)
            elapsed_h = int((now_dt - ot).total_seconds() / 3600)
            _max_h = {"pump_orta": 72, "pump_uzun": 168}.get(sig.get("sig_type", ""))
            if _max_h:
                _sc = "#f39c12" if elapsed_h >= _max_h * 0.8 else "#7f8c8d"
                sure_cell = f'<span style="font-size:.7rem;color:{_sc}">{elapsed_h}s / {_max_h}s</span>'
            else:
                sure_cell = f'<span style="font-size:.7rem;color:#7f8c8d">+{elapsed_h}s</span>'
        except Exception:
            pass

        is_half = sig.get("status") == "half_open"
        tp1_cell = (f'<span style="background:#2ecc7133;color:#2ecc71;padding:1px 5px;border-radius:3px;font-size:.6rem;white-space:nowrap">✅ +{sig.get("tp1_exit_pct",tp1_pct)}% → TP2</span>'
                    if is_half else f"{fmt_price(sig['tp1'])} (+{tp1_pct}%)")
        open_rows += f"""<tr>
            <td style="color:#ecf0f1"><b>{sym}</b></td><td>{type_badge(sig)}</td>
            <td>{fmt_price(sig['entry'])}</td>
            <td style="color:{cur_c};font-weight:bold">{fmt_price(sig.get('current_price'))} ({cur_s})</td>
            <td style="color:{peak_c}">{peak_s}</td><td style="color:{low_c}">{low_s}</td>
            <td>{stop_cell}</td><td>{tp1_cell}</td>
            <td>{fmt_price(tp2_val)} (+{tp2_pct_open}%)</td>
            <td style="font-size:.7rem;color:#7f8c8d;white-space:nowrap">{datetime.fromisoformat(sig['open_time']).strftime('%d/%m/%Y') if sig.get('open_time') else '—'}<br><span style="font-size:.65rem;color:#5a6a7a">{datetime.fromisoformat(sig['open_time']).strftime('%H:%M') if sig.get('open_time') else ''}</span></td>
            <td>{sure_cell}</td><td>{analyzer_badge(sig)}</td></tr>"""

    closed_rows = ""
    for sig in closed_sigs[:100]:
        close_c, close_s = pct_color(sig.get("close_pct"))
        peak_c, peak_s = pct_color(sig.get("peak_pct"))
        sym = sig["symbol"].replace("/USDT", "")
        tp1_badge = ""
        if sig.get("tp1_hit"):
            tp1_pct_v = round((sig["tp1"] - sig["entry"]) / sig["entry"] * 100, 1) if sig.get("entry", 0) > 0 else 0
            tp1_badge = f'<span style="color:#2ecc71;font-size:.58rem">✓TP1 +{tp1_pct_v}%</span>'

        closed_rows += f"""<tr>
            <td style="color:#ecf0f1"><b>{sym}</b></td><td>{type_badge(sig)}</td>
            <td>{status_badge(sig.get('status','unknown'))}</td>
            <td>{fmt_price(sig['entry'])}</td>
            <td style="color:{close_c};font-weight:bold">{close_s}</td>
            <td style="color:{peak_c}">{peak_s}</td>
            <td>{tp1_badge}</td>
            <td>{analyzer_badge(sig)}</td>
            <td style="font-size:.7rem;color:#7f8c8d;white-space:nowrap">{datetime.fromisoformat(sig['open_time']).strftime('%d/%m/%Y') if sig.get('open_time') else '—'}<br><span style="font-size:.65rem;color:#5a6a7a">{datetime.fromisoformat(sig['open_time']).strftime('%H:%M') if sig.get('open_time') else ''}</span></td>
            <td style="font-size:.7rem;color:#7f8c8d;white-space:nowrap">{datetime.fromisoformat(sig['close_time']).strftime('%d/%m/%Y') if sig.get('close_time') else '—'}<br><span style="font-size:.65rem;color:#5a6a7a">{datetime.fromisoformat(sig['close_time']).strftime('%H:%M') if sig.get('close_time') else ''}</span></td></tr>"""

    # Sinyal türü tabloları — SMC vs Bot ayrımı
    smc_type_rows = ""
    bot_type_rows = ""
    for tk, ts in sorted(perf.get("by_type", {}).items()):
        wr = ts.get("win_rate", 0)
        wr_c = "#2ecc71" if wr >= 60 else ("#f39c12" if wr >= 40 else "#e74c3c")
        pnl = ts.get("total_pnl", 0)
        pnl_c = "#2ecc71" if pnl > 0 else ("#e74c3c" if pnl < 0 else "#8a9bb0")
        row = (f'<tr><td style="color:#ecf0f1;font-weight:bold">{tk}</td>'
               f'<td>{ts.get("total",0)}</td><td style="color:#3498db">{ts.get("open",0)}</td>'
               f'<td style="color:#2ecc71">{ts.get("wins",0)}</td><td style="color:#e74c3c">{ts.get("losses",0)}</td>'
               f'<td style="color:#f39c12">{ts.get("expired",0)}</td>'
               f'<td style="color:{wr_c};font-weight:bold">%{wr}</td>'
               f'<td style="color:{pnl_c};font-weight:bold">{pnl:+.2f}%</td>'
               f'<td>{ts.get("avg_peak",0)}%</td></tr>')
        if tk.startswith("SMC"):
            smc_type_rows += row
        else:
            bot_type_rows += row

    # Hayali senaryo verileri
    def _sim_row(b):
        decided = b["tp2"] + b["tp1"] + b["stop"]
        wr_c = "#2ecc71" if b["wr"] >= 55 else ("#f39c12" if b["wr"] >= 40 else "#e74c3c")
        pnl_c = "#2ecc71" if b["pnl"] > 0 else ("#e74c3c" if b["pnl"] < 0 else "#8a9bb0")
        return (b["tp2"], b["tp1"], b["stop"], b["open"], decided, b["wr"], wr_c, b["pnl"], pnl_c)
    sb = perf.get("sim_bot", {}); ss = perf.get("sim_smc", {})
    sbt = _sim_row(sb) if sb else (0,0,0,0,0,0,"#8a9bb0",0,"#8a9bb0")
    sst = _sim_row(ss) if ss else (0,0,0,0,0,0,"#8a9bb0",0,"#8a9bb0")

    # Hayali senaryo — TOPLAM kolonu
    st_tp2  = sbt[0] + sst[0]; st_tp1 = sbt[1] + sst[1]
    st_stop = sbt[2] + sst[2]; st_open = sbt[3] + sst[3]
    st_dec  = st_tp2 + st_tp1 + st_stop
    st_wr   = round((st_tp2 + st_tp1) / st_dec * 100, 1) if st_dec > 0 else 0
    st_pnl  = round(sbt[7] + sst[7], 1)
    st_wrc  = "#2ecc71" if st_wr >= 55 else ("#f39c12" if st_wr >= 40 else "#e74c3c")
    st_pnlc = "#2ecc71" if st_pnl > 0 else ("#e74c3c" if st_pnl < 0 else "#8a9bb0")

    _sim_section = f"""<div class="tp2-box">
    <h3>🎭 HAYALİ SENARYO — "TP1 +5% | TP2 +10% | Stop -2.5% olsaydı ne olurdu?"</h3>
    <p style="color:var(--text-dim);font-size:.6rem;margin-bottom:12px;font-style:italic">
        Tüm sinyallere sabit parametreler uygulanıyor. Peak ve dip verisi üzerinden hesaplanır — gerçek çıkış değil.</p>
    <div class="table-wrap"><table style="font-size:.72rem"><thead><tr>
        <th></th>
        <th style="text-align:center;color:#8a9bb0">Sinyal</th>
        <th style="text-align:center;color:#27ae60">TP2 (+10%)</th>
        <th style="text-align:center;color:#2ecc71">TP1 (+5%)</th>
        <th style="text-align:center;color:#e74c3c">Stop (-2.5%)</th>
        <th style="text-align:center;color:#8a9bb0">Devam/Açık</th>
        <th style="text-align:center;color:#8a9bb0">Win Rate</th>
        <th style="text-align:center;color:#8a9bb0">P&amp;L</th>
    </tr></thead><tbody>
        <tr>
            <td style="color:#3498db">Bot Sinyalleri</td>
            <td style="text-align:center;color:#8a9bb0">{sbt[0]+sbt[1]+sbt[2]+sbt[3]}</td>
            <td style="text-align:center;color:#27ae60">{sbt[0]}</td>
            <td style="text-align:center;color:#2ecc71">{sbt[1]}</td>
            <td style="text-align:center;color:#e74c3c">{sbt[2]}</td>
            <td style="text-align:center;color:#8a9bb0">{sbt[3]}</td>
            <td style="text-align:center"><span style="color:{sbt[6]}">%{sbt[5]}</span></td>
            <td style="text-align:center"><span style="color:{sbt[8]}">{sbt[7]:+.2f}%</span></td>
        </tr>
        <tr>
            <td style="color:#e67e22">SMC Sinyalleri</td>
            <td style="text-align:center;color:#8a9bb0">{sst[0]+sst[1]+sst[2]+sst[3]}</td>
            <td style="text-align:center;color:#27ae60">{sst[0]}</td>
            <td style="text-align:center;color:#2ecc71">{sst[1]}</td>
            <td style="text-align:center;color:#e74c3c">{sst[2]}</td>
            <td style="text-align:center;color:#8a9bb0">{sst[3]}</td>
            <td style="text-align:center"><span style="color:{sst[6]}">%{sst[5]}</span></td>
            <td style="text-align:center"><span style="color:{sst[8]}">{sst[7]:+.2f}%</span></td>
        </tr>
        <tr style="border-top:2px solid #1a3050">
            <td style="color:#c0cdd8;font-weight:bold">TOPLAM</td>
            <td style="text-align:center;color:#8a9bb0;font-weight:bold">{st_tp2+st_tp1+st_stop+st_open}</td>
            <td style="text-align:center;color:#27ae60;font-weight:bold">{st_tp2}</td>
            <td style="text-align:center;color:#2ecc71;font-weight:bold">{st_tp1}</td>
            <td style="text-align:center;color:#e74c3c;font-weight:bold">{st_stop}</td>
            <td style="text-align:center;color:#8a9bb0">{st_open}</td>
            <td style="text-align:center;font-weight:bold"><span style="color:{st_wrc}">%{st_wr}</span></td>
            <td style="text-align:center;font-weight:bold"><span style="color:{st_pnlc}">{st_pnl:+.2f}%</span></td>
        </tr>
    </tbody></table></div>
</div>"""
    type_rows = smc_type_rows + bot_type_rows

    shadow_rows = ""
    for sig in shadow_watching[:30]:
        sym = sig["symbol"].replace("/USDT", "")
        entry = sig["entry"]; tp1_pct = sig.get("close_pct", 0) or 0
        tp2 = sig.get("tp2")
        tp2_pct = round((tp2 - entry) / entry * 100, 1) if tp2 and entry > 0 else 0
        pa = sig.get("tp2_peak_after_tp1", 0)
        pa_c = "#2ecc71" if pa > tp1_pct else "#8a9bb0"
        cur_price = sig.get("current_price", entry)
        cur_pct = sig.get("current_pct", 0)
        cur_c = "#2ecc71" if cur_pct > 0 else ("#e74c3c" if cur_pct < 0 else "#8a9bb0")
        remaining = ""
        try:
            ct = datetime.fromisoformat(sig.get("close_time", sig["open_time"]))
            if ct.tzinfo is None: ct = ct.replace(tzinfo=TR_TZ)
            remaining = f"{max(0, SHADOW_EXPIRE_HOURS - (now_dt - ct).total_seconds() / 3600):.0f}s"
        except Exception: pass
        shadow_rows += f"""<tr>
            <td style="color:#ecf0f1"><b>{sym}</b></td><td>{type_badge(sig)}</td>
            <td style="color:{'#2ecc71' if tp1_pct >= 0 else '#e74c3c'}">{tp1_pct:+.2f}%</td>
            <td style="color:{cur_c}">{fmt_price(cur_price)} ({cur_pct:+.2f}%)</td>
            <td>{fmt_price(tp2)} (+{tp2_pct}%)</td>
            <td style="color:{pa_c}">+{pa:.2f}%</td>
            <td style="color:#7f8c8d;font-size:.7rem">{remaining}</td></tr>"""

    daily_rows = ""
    for day_key in sorted(perf.get("daily", {}).keys(), reverse=True)[:14]:
        d = perf["daily"][day_key]; pnl = d.get("pnl", 0)
        pnl_c = "#2ecc71" if pnl > 0 else ("#e74c3c" if pnl < 0 else "#8a9bb0")
        daily_rows += f"""<tr>
            <td style="color:#ecf0f1">{day_key}</td><td>{d.get('trades',0)}</td>
            <td style="color:#2ecc71">{d.get('wins',0)}</td><td style="color:#e74c3c">{d.get('losses',0)}</td>
            <td style="color:{pnl_c};font-weight:bold">{pnl:+.2f}%</td></tr>"""

    total_pnl = perf.get("total_pnl", 0)
    pnl_color_val = "#2ecc71" if total_pnl > 0 else ("#e74c3c" if total_pnl < 0 else "#8a9bb0")
    win_partial_count = perf.get("win_partial", 0)

    az = perf.get("analyzer", {})
    def _az_row(key, label, color):
        b = az.get(key, {}); t = b.get("total", 0)
        if t == 0:
            return f'<div class="tp2-stat"><span class="v" style="color:{color}">{label}</span><span class="l">— veri yok —</span></div>'
        wr_c = "#2ecc71" if b.get("wr",0) >= 55 else ("#f39c12" if b.get("wr",0) >= 40 else "#e74c3c")
        pc = "#2ecc71" if b.get("pnl",0) > 0 else ("#e74c3c" if b.get("pnl",0) < 0 else "#8a9bb0")
        return (f'<div class="tp2-stat"><span class="v" style="color:{color}">{label}</span>'
                f'<span class="l">{t} sinyal | WR <b style="color:{wr_c}">%{b.get("wr",0)}</b> | P&L <b style="color:{pc}">{b.get("pnl",0):+.2f}%</b></span></div>')
    _analyzer_section = f"""<div class="tp2-box">
    <h3>🤖 CLAUDE ANALYZER PERFORMANSI — "Karar kalitesi ne?"</h3>
    <div class="tp2-stats">
        {_az_row("gir",    "✅ GİR",     "#2ecc71")}
        {_az_row("dikkat", "⚠️ DİKKAT",  "#f39c12")}
        {_az_row("riskli", "🚫 RİSKLİ",  "#e74c3c")}
    </div>
</div>"""

    # Alternatif senaryo bölümleri (SMC ve Bot altına eklenecek)
    smc_a = perf.get("smc_alt", {})
    bot_a = perf.get("bot_alt", {})

    def _alt_cell(data, color):
        if not data or data.get("total", 0) == 0:
            return '<td style="color:#3a4a5a;text-align:center" colspan="1">—</td>'
        wr_c  = "#2ecc71" if data.get("wr",0) >= 55 else ("#f39c12" if data.get("wr",0) >= 40 else "#e74c3c")
        pnl_c = "#2ecc71" if data.get("pnl",0) > 0 else ("#e74c3c" if data.get("pnl",0) < 0 else "#8a9bb0")
        return (f'<td style="text-align:center"><span style="color:#2ecc71">{data.get("wins",0)}</span></td>'
                f'<td style="text-align:center"><span style="color:#e74c3c">{data.get("losses",0)}</span></td>'
                f'<td style="text-align:center"><span style="color:#f39c12">{data.get("expired",0)}</span></td>'
                f'<td style="text-align:center;font-weight:bold"><span style="color:{wr_c}">%{data.get("wr",0)}</span></td>'
                f'<td style="text-align:center;font-weight:bold"><span style="color:{pnl_c}">{data.get("pnl",0):+.2f}%</span></td>')

    _ALT_TH = ('<th style="text-align:center;color:#5a6a7a">Strateji</th>'
               '<th style="text-align:center;color:#2ecc71">Win</th>'
               '<th style="text-align:center;color:#e74c3c">Loss</th>'
               '<th style="text-align:center;color:#f39c12">Exp</th>'
               '<th style="text-align:center;color:#8a9bb0">WR</th>'
               '<th style="text-align:center;color:#8a9bb0">P&amp;L</th>')

    _smc_alt_section = ""
    if smc_a and smc_a.get("actual", {}).get("total", 0) > 0:
        _smc_alt_section = (
            f'<div style="margin-top:10px;padding:10px 14px;background:#070d14;'
            f'border:1px solid #1a2535;border-radius:4px">'
            f'<div style="font-size:.58rem;color:#4a5a6a;letter-spacing:1.5px;'
            f'margin-bottom:10px;text-transform:uppercase">Acaba farklı çıkış olsaydı?</div>'
            f'<div class="table-wrap"><table style="font-size:.7rem"><thead><tr>{_ALT_TH}</tr></thead><tbody>'
            f'<tr><td style="color:#e67e22;white-space:nowrap">½ TP1 + ½ TP2 (gerçek)</td>'
            f'{_alt_cell(smc_a.get("actual",{}), "#e67e22")}</tr>'
            f'<tr><td style="color:#f39c12;white-space:nowrap">Tam TP1 (%100)</td>'
            f'{_alt_cell(smc_a.get("tp1_only",{}), "#f39c12")}</tr>'
            f'<tr><td style="color:#2ecc71;white-space:nowrap">Tam TP2 (%100)</td>'
            f'{_alt_cell(smc_a.get("tp2_only",{}), "#2ecc71")}</tr>'
            f'</tbody></table></div>'
            f'<div style="font-size:.57rem;color:#2a3a4a;margin-top:5px">'
            f'Peak/dip verisi üzerinden — kapanmış sinyaller</div>'
            f'</div>'
        )

    _bot_alt_section = ""
    if bot_a and bot_a.get("actual", {}).get("total", 0) > 0:
        _bot_alt_section = (
            f'<div style="margin-top:10px;padding:10px 14px;background:#070d14;'
            f'border:1px solid #1a2535;border-radius:4px">'
            f'<div style="font-size:.58rem;color:#4a5a6a;letter-spacing:1.5px;'
            f'margin-bottom:10px;text-transform:uppercase">Acaba farklı çıkış olsaydı?</div>'
            f'<div class="table-wrap"><table style="font-size:.7rem"><thead><tr>{_ALT_TH}</tr></thead><tbody>'
            f'<tr><td style="color:#3498db;white-space:nowrap">Trailing %3 (gerçek)</td>'
            f'{_alt_cell(bot_a.get("actual",{}), "#3498db")}</tr>'
            f'<tr><td style="color:#f39c12;white-space:nowrap">Tam TP1 (%100)</td>'
            f'{_alt_cell(bot_a.get("tp1_only",{}), "#f39c12")}</tr>'
            f'</tbody></table></div>'
            f'<div style="font-size:.57rem;color:#2a3a4a;margin-top:5px">'
            f'Peak/dip verisi üzerinden — kapanmış sinyaller</div>'
            f'</div>'
        )

    shadow_section = ""
    if shadow_rows:
        shadow_section = f"""
<div class="section">
    <h2>👁 TP2 SHADOW İZLEME ({len(shadow_watching)})</h2>
    <p class="note">TP1'de kapanmış — TP2'ye stop'a düşmeden ulaşabilir miydi izleniyor.</p>
    <div class="table-wrap"><table><thead><tr>
        <th>Sembol</th><th>Tür</th><th>TP1 Kâr</th><th>Şu An</th><th>TP2 Hedef</th><th>Peak Sonrası</th><th>Kalan</th>
    </tr></thead><tbody>{shadow_rows}</tbody></table></div>
</div>"""

    expire_trail_threshold_h = round(EXPIRE_HOURS * EXPIRE_TRAIL_THRESHOLD, 1)

    _smc_section_block = ""
    if _smc_alt_section:
        _note_smc = "TP1'de %50 çıkış (half_open) → kalan %50 TP2 veya stop'a kadar takip edilir"
        _smc_section_block = (
            f'<div class="section"><h2>🟠 SMC SİNYALLERİ — Acaba Farklı Çıkış Olsaydı?</h2>'
            f'<p class="note">{_note_smc}</p>{_smc_alt_section}</div>'
        )
    _bot_section_block = ""
    if _bot_alt_section:
        _note_bot = "Trailing stop %3 aktif (baştan itibaren) — TP1 milestone, TP2 hedef, peak'in %3 altında kapanır"
        _bot_section_block = (
            f'<div class="section"><h2>🔵 BOT SİNYALLERİ — Acaba Farklı Çıkış Olsaydı?</h2>'
            f'<p class="note">{_note_bot}</p>{_bot_alt_section}</div>'
        )

    html = f"""<!DOCTYPE html>
<html lang="tr"><head>
<meta charset="UTF-8"><title>Portföy Takip v2.7</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="60">
<meta property="og:title" content="Portfolio Tracker">
<meta property="og:description" content="Kripto sinyal takip sistemi">
<meta property="og:image" content="https://raw.githubusercontent.com/Brkzgrc/Botum/main/portfolio_logo.jpg">
<meta property="og:url" content="https://portfolio-tracker-xzvw.onrender.com">
<style>
:root {{--bg:#0a0e14;--card:#0f1319;--border:#1a2030;--text:#c0cdd8;--text-dim:#5a6a7a;
  --accent:#00b4d8;--green:#2ecc71;--red:#e74c3c;--orange:#f39c12;--purple:#9b59b6;}}
*{{box-sizing:border-box;margin:0;padding:0;}}
body{{background:var(--bg);color:var(--text);font-family:'JetBrains Mono','Fira Code','Consolas',monospace;
  padding:20px;max-width:1200px;margin:0 auto;line-height:1.5;}}
.header{{display:flex;justify-content:space-between;align-items:center;margin-bottom:24px;
  padding-bottom:16px;border-bottom:1px solid var(--border);}}
.header h1{{color:var(--accent);font-size:1.1rem;letter-spacing:3px;}}
.header .time{{color:var(--text-dim);font-size:.75rem;display:flex;align-items:center;gap:10px;}}
.btn-refresh{{background:#1a472a;color:#2ecc71;border:1px solid #2ecc7166;border-radius:4px;
  padding:3px 10px;font-size:.65rem;cursor:pointer;font-family:inherit;transition:background .2s;}}
.btn-refresh:hover{{background:#2ecc7133;}}
.btn-clear{{background:#c0392b22;color:#e74c3c;border:1px solid #e74c3c44;border-radius:4px;
  padding:3px 10px;font-size:.65rem;cursor:pointer;font-family:inherit;transition:background .2s;}}
.btn-clear:hover{{background:#c0392b55;}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px;margin-bottom:24px;}}
.card{{background:var(--card);border:1px solid var(--border);border-radius:6px;padding:14px;text-align:center;}}
.card .val{{font-size:1.3rem;font-weight:bold;color:var(--accent);display:block;margin-bottom:4px;}}
.card .lbl{{font-size:.55rem;color:var(--text-dim);text-transform:uppercase;letter-spacing:1px;}}
.section{{margin-bottom:28px;}}
.section h2{{color:var(--accent);font-size:.85rem;letter-spacing:2px;margin-bottom:12px;
  padding-bottom:6px;border-bottom:1px solid var(--border);}}
.section .note{{color:var(--text-dim);font-size:.65rem;margin-top:-8px;margin-bottom:12px;font-style:italic;}}
table{{width:100%;border-collapse:collapse;font-size:.73rem;}}
th{{background:var(--card);color:var(--text-dim);font-size:.58rem;text-transform:uppercase;
  letter-spacing:1px;padding:8px 8px;text-align:left;border-bottom:1px solid var(--border);position:sticky;top:0;}}
td{{padding:7px 8px;border-bottom:1px solid #0d111a;vertical-align:middle;}}
tr:hover td{{background:var(--card);}}
.table-wrap{{overflow-x:auto;border:1px solid var(--border);border-radius:6px;}}
.empty{{color:var(--text-dim);padding:20px;text-align:center;font-size:.8rem;}}
.tp2-box{{background:#0d1520;border:1px solid #1a3050;border-radius:6px;padding:16px;margin-bottom:24px;}}
.tp2-box h3{{color:#3498db;font-size:.8rem;margin-bottom:10px;}}
.tp2-stats{{display:flex;gap:20px;flex-wrap:wrap;font-size:.75rem;}}
.tp2-stat{{display:flex;flex-direction:column;align-items:center;}}
.tp2-stat .v{{font-size:1.1rem;font-weight:bold;}}
.tp2-stat .l{{font-size:.55rem;color:var(--text-dim);margin-top:2px;}}
.footer{{color:var(--text-dim);font-size:.6rem;margin-top:20px;padding-top:12px;
  border-top:1px solid var(--border);text-align:center;}}
.filter-bar{{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:16px;}}
.filter-btn{{background:#0f1319;border:1px solid #1a2030;border-radius:4px;
  color:#5a6a7a;font-size:.62rem;padding:5px 12px;cursor:pointer;font-family:inherit;
  letter-spacing:.5px;transition:all .15s;}}
.filter-btn.active{{border-color:var(--accent);color:var(--accent);background:#00b4d811;}}
@media(max-width:768px){{body{{padding:10px;}}.cards{{grid-template-columns:repeat(3,1fr);}}
  table{{font-size:.63rem;}}td,th{{padding:5px 5px;}}}}
</style></head><body>

<div class="header">
    <h1>📊 PORTFÖY TAKİP</h1>
    <span class="time">
        {now} | v2.7
        <button class="btn-refresh" onclick="location.reload()">🔄 Yenile</button>
        <button class="btn-clear"
            onclick="if(confirm('Tüm sinyaller silinecek.\\nEmin misiniz?')){{fetch('/api/signals/clear-all-ui',{{method:'POST'}}).then(r=>r.json()).then(d=>{{alert('Silindi: '+d.removed+' sinyal');location.reload()}})}}"
        >🗑 Sıfırla</button>
    </span>
</div>

<script>
const BY_TYPE = {json.dumps(perf.get('by_type', {}), ensure_ascii=False)};
const ACTIVE = new Set(Object.keys(BY_TYPE));

function recalc() {{
  let total=0, open=0, wins=0, win_partial=0, losses=0, expired=0, pnl=0, peaks=[];
  for (const [k, v] of Object.entries(BY_TYPE)) {{
    if (!ACTIVE.has(k)) continue;
    total      += v.total      || 0;
    open       += v.open       || 0;
    wins       += v.wins       || 0;
    win_partial+= v.win_partial|| 0;
    losses     += v.losses     || 0;
    expired    += v.expired    || 0;
    pnl        += v.total_pnl  || 0;
    if (v.avg_peak && v.total > 0) peaks.push([v.avg_peak, v.total]);
  }}
  const closed = wins + losses + expired;
  const wr = closed > 0 ? (wins / closed * 100).toFixed(1) : 0;
  const avgPeak = peaks.length
    ? (peaks.reduce((s,[p,n])=>s+p*n,0) / peaks.reduce((s,[,n])=>s+n,0)).toFixed(2)
    : 0;
  const pnlFmt = (pnl >= 0 ? '+' : '') + pnl.toFixed(2) + '%';

  document.getElementById('c-total').textContent   = total;
  document.getElementById('c-open').textContent    = open;
  document.getElementById('c-wins').textContent    = wins;
  document.getElementById('c-trail').textContent   = win_partial;
  document.getElementById('c-loss').textContent    = losses;
  document.getElementById('c-exp').textContent     = expired;
  const wrEl = document.getElementById('c-wr');
  wrEl.textContent = '%' + wr;
  wrEl.style.color = wr >= 50 ? 'var(--green)' : 'var(--red)';
  const pnlEl = document.getElementById('c-pnl');
  pnlEl.textContent = pnlFmt;
  pnlEl.style.color = pnl > 0 ? 'var(--green)' : (pnl < 0 ? 'var(--red)' : '#8a9bb0');
  document.getElementById('c-peak').textContent = avgPeak + '%';
}}

function toggleType(key, btn) {{
  if (ACTIVE.has(key)) {{ ACTIVE.delete(key); btn.classList.remove('active'); }}
  else                  {{ ACTIVE.add(key);    btn.classList.add('active');    }}
  recalc();
}}
</script>

<div class="filter-bar">
  {' '.join(f'<button class="filter-btn active" data-key="{k}" onclick="toggleType(this.dataset.key,this)">{k}</button>' for k in sorted(perf.get('by_type', {})))}
</div>

<div class="cards">
    <div class="card"><span class="val" id="c-total">{perf.get('total',0)}</span><span class="lbl">Toplam</span></div>
    <div class="card"><span class="val" style="color:#3498db" id="c-open">{perf.get('open',0)}</span><span class="lbl">Açık</span></div>
    <div class="card"><span class="val" style="color:var(--green)" id="c-wins">{perf.get('wins',0)}</span><span class="lbl">Win</span></div>
    <div class="card"><span class="val" style="color:#27ae60;font-size:.9rem" id="c-trail">{win_partial_count}</span><span class="lbl">Win (Trail)</span></div>
    <div class="card"><span class="val" style="color:var(--red)" id="c-loss">{perf.get('losses',0)}</span><span class="lbl">Loss</span></div>
    <div class="card"><span class="val" style="color:var(--orange)" id="c-exp">{perf.get('expired',0)}</span><span class="lbl">Expired</span></div>
    <div class="card"><span class="val" id="c-wr" style="color:{'var(--green)' if perf.get('win_rate',0)>=50 else 'var(--red)'}"
        >%{perf.get('win_rate',0)}</span><span class="lbl">Win Rate</span></div>
    <div class="card"><span class="val" id="c-pnl" style="color:{pnl_color_val}">{total_pnl:+.2f}%</span><span class="lbl">Net P&L</span></div>
    <div class="card"><span class="val" id="c-peak">{perf.get('avg_peak',0)}%</span><span class="lbl">Ort. Peak</span></div>
</div>

<div class="section">
    <h2>📈 SİNYAL TÜRÜ BAZLI KIRILIM</h2>
    <div class="table-wrap"><table><thead><tr>
        <th>Tür</th><th>Toplam</th><th>Açık</th><th>Win</th><th>Loss</th><th>Exp.</th>
        <th>Win Rate</th><th>P&L</th><th>Ort. Peak</th>
    </tr></thead><tbody>
        {type_rows if type_rows else '<tr><td colspan="9" class="empty">Henüz veri yok</td></tr>'}
    </tbody></table></div>
</div>

{_smc_section_block}

{_bot_section_block}

{_sim_section}

{_analyzer_section}

<div class="section">
    <h2>🔵 AÇIK POZİSYONLAR ({len(open_sigs)})</h2>
    <p class="note">Bot sinyalleri: ⚡ trailing stop (%3 peak altı) aktif — TP1 milestone, TP2 hedef. SMC: TP1'de %50 çıkış.</p>
    <div class="table-wrap"><table><thead><tr>
        <th>Sembol</th><th>Tür</th><th>Giriş</th><th>Şu An</th><th>Peak</th><th>Dip</th>
        <th>Trail/Stop</th><th>TP1</th><th>TP2</th><th>Tarih</th><th>Süre</th><th>Analiz</th>
    </tr></thead><tbody>
        {open_rows if open_rows else '<tr><td colspan="12" class="empty">Açık pozisyon yok</td></tr>'}
    </tbody></table></div>
</div>

{shadow_section}

<div class="section">
    <h2>📋 KAPANMIŞ İŞLEMLER (son 100)</h2>
    <div class="table-wrap"><table><thead><tr>
        <th>Sembol</th><th>Tür</th><th>Sonuç</th><th>Giriş</th><th>Getiri</th><th>Peak</th>
        <th>TP1 Hit</th><th>Analiz</th><th>Açılış</th><th>Kapanış</th>
    </tr></thead><tbody>
        {closed_rows if closed_rows else '<tr><td colspan="10" class="empty">Henüz kapanmış işlem yok</td></tr>'}
    </tbody></table></div>
</div>

<div class="section">
    <h2>📅 GÜNLÜK PERFORMANS (son 14 gün)</h2>
    <div class="table-wrap"><table><thead><tr>
        <th>Tarih</th><th>İşlem</th><th>Win</th><th>Loss</th><th>P&L</th>
    </tr></thead><tbody>
        {daily_rows if daily_rows else '<tr><td colspan="5" class="empty">Henüz veri yok</td></tr>'}
    </tbody></table></div>
</div>

<div class="footer">
    Portföy Takip v2.7 | Bot: Trailing %3 (TP1 milestone, TP2 hedef) | SMC: %50 TP1 + %50 TP2 |
    Kontrol: {CHECK_INTERVAL//60}dk | Expire: {EXPIRE_HOURS}s | {now}
</div>
</body></html>"""
    return html

# ============================================================
# GITHUB SNAPSHOT
# ============================================================
def push_snapshot_to_github():
    if not GITHUB_TOKEN:
        return
    try:
        perf = calc_performance()
        with _lock:
            sigs = list(signals_db)
        snapshot = {
            "updated_at": tr_now_str(),
            "performance": perf,
            "open": [s for s in sigs if s.get("status") in ("open", "half_open")],
            "closed": [s for s in sigs if s.get("status") not in ("open", "half_open")][-50:],
        }
        content = json.dumps(snapshot, ensure_ascii=False, default=str, indent=2)
        import base64
        encoded = base64.b64encode(content.encode()).decode()

        headers = {
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
        }
        api_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE}"

        # Mevcut dosyanın SHA'sını al (güncelleme için gerekli)
        r = requests.get(api_url, headers=headers, timeout=10)
        sha = r.json().get("sha") if r.status_code == 200 else None

        payload = {"message": f"snapshot {tr_now_str()}", "content": encoded, "branch": "main"}
        if sha:
            payload["sha"] = sha

        r = requests.put(api_url, headers=headers, json=payload, timeout=15)
        if r.status_code in (200, 201):
            print(f"[SNAPSHOT] GitHub'a yazıldı.", flush=True)
        else:
            print(f"[SNAPSHOT] GitHub hata {r.status_code}: {r.text[:120]}", flush=True)
    except Exception as e:
        print(f"[SNAPSHOT] Hata: {e}", flush=True)


def snapshot_loop():
    time.sleep(60)  # ilk çalıştırmayı biraz geciktir
    while True:
        push_snapshot_to_github()
        time.sleep(1800)  # 30 dakikada bir


# ============================================================
# MAIN
# ============================================================
if __name__ == "__main__":
    print("=" * 50, flush=True)
    print("📊 Portföy Takip Sistemi v2.7", flush=True)
    print("   Bot: Trailing %3 (TP1 milestone, TP2 hedef)", flush=True)
    print("   SMC: %50 TP1 yarı çıkış + %50 TP2", flush=True)
    print("=" * 50, flush=True)
    print(f"  Kontrol aralığı      : {CHECK_INTERVAL}s ({CHECK_INTERVAL // 60} dk)", flush=True)
    print(f"  Expire süresi        : {EXPIRE_HOURS} saat", flush=True)
    print(f"  Expire trail eşiği   : %{EXPIRE_TRAIL_THRESHOLD*100:.0f} ({EXPIRE_HOURS * EXPIRE_TRAIL_THRESHOLD:.1f}s)", flush=True)
    print(f"  Expire trail %        : %{EXPIRE_TRAIL_PCT}", flush=True)
    print(f"  TP2 shadow süresi    : {SHADOW_EXPIRE_HOURS} saat", flush=True)
    print(f"  Data dizini          : {DATA_DIR}", flush=True)
    print("=" * 50, flush=True)

    load_signals()
    threading.Thread(target=position_checker_loop, daemon=True).start()
    threading.Thread(target=snapshot_loop, daemon=True, name="github_snapshot").start()
    start_news_watcher()
    start_market_analyzer()
    _start_market_watcher()
    start_intraday_scanner()

    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, use_reloader=False)
