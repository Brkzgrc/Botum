# -*- coding: utf-8 -*-
"""
Portföy Takip Sistemi v1.0
===========================
- bot.py (Scanner v7) ve SMC.py (SMC Sniper v4) sinyallerini HTTP POST ile alır
- Açık pozisyonları 5 dakikada bir Binance REST API ile kontrol eder
- TP1, TP2, Stop, Expired durumlarını otomatik takip eder
- HTML dashboard sunar (Render'da canlı link)
- JSON dosya tabanlı kayıt
"""

import json
import os
import time
import threading
from datetime import datetime, timedelta, timezone
from collections import defaultdict

import requests
from flask import Flask, request, jsonify

# ============================================================
# AYARLAR
# ============================================================
TR_TZ = timezone(timedelta(hours=3))
DATA_DIR = os.getenv("DATA_DIR", "/tmp")
SIGNALS_FILE = os.path.join(DATA_DIR, "portfolio_signals.json")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "300"))  # 5 dakika
EXPIRE_HOURS = int(os.getenv("EXPIRE_HOURS", "48"))  # 48 saat sonra expire
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
AUTH_TOKEN = os.getenv("PORTFOLIO_AUTH_TOKEN", "")  # Basit auth token

BINANCE_KLINE_URL = "https://api.binance.com/api/v3/klines"

app = Flask(__name__)

import logging
logging.getLogger("werkzeug").setLevel(logging.ERROR)

# ============================================================
# VERİ KATMANI
# ============================================================
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
# TELEGRAM BİLDİRİM
# ============================================================
def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text,
                  "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=10,
        )
    except Exception as e:
        print(f"[TG] Hata: {e}", flush=True)


# ============================================================
# SİNYAL ALMA ENDPOINT'İ
# ============================================================
@app.route("/api/signal", methods=["POST"])
def receive_signal():
    """bot.py ve SMC.py buraya sinyal POST eder."""
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

    now = tr_now()
    signal = {
        "id": f"{data['symbol']}_{int(now.timestamp())}",
        "symbol": data["symbol"],
        "entry": float(data["entry"]),
        "stop": float(data["stop"]),
        "tp1": float(data["tp1"]),
        "tp2": float(data.get("tp2", 0)) or None,
        "sig_type": data.get("sig_type", data.get("type", "unknown")),
        "sub_type": data.get("sub_type", data.get("subtype", data.get("tp_system", ""))),
        "source": data.get("source", "bot"),  # "bot" veya "smc"
        "phase": data.get("phase", ""),  # SMC için: "phase1", "phase2"
        "candle": data.get("candle", ""),
        "funding_neg": data.get("funding_neg", False),
        # Durum alanları
        "status": "open",
        "open_time": now.isoformat(),
        "close_time": None,
        "close_price": None,
        "close_reason": None,
        "peak_price": float(data["entry"]),
        "peak_pct": 0.0,
        "low_price": float(data["entry"]),
        "low_pct": 0.0,
        "tp1_hit": False,
        "tp1_time": None,
        "tp2_hit": False,
        "tp2_time": None,
        "current_price": float(data["entry"]),
        "current_pct": 0.0,
        "last_check": now.isoformat(),
        "checks": 0,
        # Ekstra veri (dashboard için)
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
def get_binance_klines(symbol, interval="5m", limit=1):
    """Binance'den mum verisi çeker."""
    pair = symbol.replace("/", "").replace("USDT", "USDT")
    try:
        r = requests.get(BINANCE_KLINE_URL, params={
            "symbol": pair, "interval": interval, "limit": limit
        }, timeout=10)
        if r.status_code == 200:
            data = r.json()
            if data:
                return data
    except Exception as e:
        print(f"[BINANCE] {symbol} hata: {e}", flush=True)
    return None


def get_current_price_hl(symbol):
    """Son 5 dakikanın high, low, close değerlerini döner."""
    klines = get_binance_klines(symbol, "5m", 1)
    if klines and len(klines) > 0:
        k = klines[0]
        return {
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
        }
    return None


# ============================================================
# POZİSYON KONTROL DÖNGÜSÜ (5dk)
# ============================================================
def check_open_positions():
    """Açık pozisyonları Binance API ile kontrol eder."""
    now = tr_now()

    with _lock:
        open_signals = [s for s in signals_db if s["status"] == "open"]

    if not open_signals:
        return

    print(f"[CHECK] {len(open_signals)} açık pozisyon kontrol ediliyor...", flush=True)
    closed_count = 0

    for sig in open_signals:
        symbol = sig["symbol"]
        price_data = get_current_price_hl(symbol)
        if not price_data:
            continue

        high = price_data["high"]
        low = price_data["low"]
        close = price_data["close"]
        entry = sig["entry"]
        stop = sig["stop"]
        tp1 = sig["tp1"]
        tp2 = sig.get("tp2")

        # Peak/Low güncelle
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

        # TP1 kontrolü
        if tp1 and high >= tp1 and not sig["tp1_hit"]:
            sig["tp1_hit"] = True
            sig["tp1_time"] = now.isoformat()
            print(f"  ✅ TP1 HIT: {symbol} @ {high:.6f}", flush=True)

        # TP2 kontrolü
        if tp2 and high >= tp2 and not sig["tp2_hit"]:
            sig["tp2_hit"] = True
            sig["tp2_time"] = now.isoformat()
            print(f"  ✅✅ TP2 HIT: {symbol} @ {high:.6f}", flush=True)

        # KAPANIŞ KONTROL — öncelik sırası: Stop → TP2 → Expire
        # Not: Aynı mumda hem stop hem TP olabilir — stop öncelikli (worst case)
        close_reason = None
        close_price = None

        if low <= stop:
            # Stop tetiklendi
            close_reason = "stop"
            close_price = stop
            sig["status"] = "loss"
        elif tp2 and high >= tp2:
            # TP2'ye ulaştı — tam kazanç
            close_reason = "tp2"
            close_price = tp2
            sig["status"] = "win"
        else:
            # Expire kontrolü
            open_time = datetime.fromisoformat(sig["open_time"])
            if open_time.tzinfo is None:
                open_time = open_time.replace(tzinfo=TR_TZ)
            elapsed_h = (now - open_time).total_seconds() / 3600
            if elapsed_h >= EXPIRE_HOURS:
                close_reason = "expired"
                close_price = close
                sig["status"] = "expired"

        if close_reason:
            sig["close_time"] = now.isoformat()
            sig["close_price"] = round(close_price, 8)
            sig["close_reason"] = close_reason
            sig["close_pct"] = round((close_price - entry) / entry * 100, 2)
            closed_count += 1

            emoji = "🔴" if close_reason == "stop" else ("🟢" if close_reason == "tp2" else "⏰")
            pct = sig["close_pct"]
            print(f"  {emoji} KAPANDI: {symbol} | {close_reason.upper()} | "
                  f"{pct:+.2f}% | Peak: {sig['peak_pct']:+.2f}%", flush=True)

            # Telegram bildirimi
            sym = symbol.replace("/USDT", "")
            send_telegram(
                f"{emoji} <b>#{sym} {close_reason.upper()}</b>\n"
                f"Giriş: {entry} → Çıkış: {close_price}\n"
                f"Getiri: {pct:+.2f}% | Peak: {sig['peak_pct']:+.2f}%\n"
                f"TP1: {'✅' if sig['tp1_hit'] else '❌'} | "
                f"TP2: {'✅' if sig['tp2_hit'] else '❌'}"
            )

        time.sleep(0.15)  # Rate limit

    if closed_count > 0:
        with _lock:
            save_signals()
        print(f"[CHECK] {closed_count} pozisyon kapandı.", flush=True)


def position_checker_loop():
    """5 dakikada bir açık pozisyonları kontrol eden thread."""
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
    """Tüm sinyaller için performans istatistikleri hesaplar."""
    with _lock:
        all_sigs = list(signals_db)

    result = {
        "total": len(all_sigs),
        "open": 0,
        "closed": 0,
        "wins": 0,
        "losses": 0,
        "expired": 0,
        "tp1_hits": 0,
        "tp2_hits": 0,
        "total_pnl": 0.0,
        "avg_peak": 0.0,
        "win_rate": 0.0,
        "by_type": {},
        "daily": {},
        "weekly": {},
        "monthly": {},
    }

    closed_peaks = []
    type_stats = defaultdict(lambda: {
        "total": 0, "open": 0, "wins": 0, "losses": 0, "expired": 0,
        "tp1_hits": 0, "tp2_hits": 0, "total_pnl": 0.0, "peaks": []
    })

    for sig in all_sigs:
        status = sig.get("status", "open")
        sig_type = sig.get("sig_type", "unknown")
        sub = sig.get("sub_type", "")
        source = sig.get("source", "bot")

        # SMC sinyalleri için type key
        if source == "smc":
            type_key = f"SMC-{sig.get('phase', '').replace('phase', 'P')}"
        elif sig_type == "tp":
            type_key = f"TP-{sub.capitalize()}" if sub else "TP"
        else:
            type_key = sig_type.upper()

        ts = type_stats[type_key]
        ts["total"] += 1

        if status == "open":
            result["open"] += 1
            ts["open"] += 1
        else:
            result["closed"] += 1
            pct = sig.get("close_pct", 0)
            result["total_pnl"] += pct
            ts["total_pnl"] += pct

            if sig.get("tp1_hit"):
                result["tp1_hits"] += 1
                ts["tp1_hits"] += 1
            if sig.get("tp2_hit"):
                result["tp2_hits"] += 1
                ts["tp2_hits"] += 1

            peak = sig.get("peak_pct", 0)
            closed_peaks.append(peak)
            ts["peaks"].append(peak)

            if status == "win":
                result["wins"] += 1
                ts["wins"] += 1
            elif status == "loss":
                result["losses"] += 1
                ts["losses"] += 1
            elif status == "expired":
                result["expired"] += 1
                ts["expired"] += 1

            # Günlük/Haftalık/Aylık
            close_time = sig.get("close_time") or sig.get("open_time", "")
            if close_time:
                try:
                    dt = datetime.fromisoformat(close_time)
                    day_key = dt.strftime("%Y-%m-%d")
                    week_key = dt.strftime("%Y-W%W")
                    month_key = dt.strftime("%Y-%m")

                    for bucket, key in [(result["daily"], day_key),
                                        (result["weekly"], week_key),
                                        (result["monthly"], month_key)]:
                        if key not in bucket:
                            bucket[key] = {"trades": 0, "pnl": 0.0, "wins": 0, "losses": 0}
                        bucket[key]["trades"] += 1
                        bucket[key]["pnl"] += pct
                        if status == "win":
                            bucket[key]["wins"] += 1
                        elif status == "loss":
                            bucket[key]["losses"] += 1
                except Exception:
                    pass

    if closed_peaks:
        result["avg_peak"] = round(sum(closed_peaks) / len(closed_peaks), 2)
    if result["closed"] > 0:
        result["win_rate"] = round(result["wins"] / result["closed"] * 100, 1)
    result["total_pnl"] = round(result["total_pnl"], 2)

    # Type stats finalize
    for tk, ts in type_stats.items():
        closed = ts["wins"] + ts["losses"] + ts["expired"]
        ts["win_rate"] = round(ts["wins"] / closed * 100, 1) if closed > 0 else 0
        ts["avg_peak"] = round(sum(ts["peaks"]) / len(ts["peaks"]), 2) if ts["peaks"] else 0
        ts["total_pnl"] = round(ts["total_pnl"], 2)
        del ts["peaks"]

    result["by_type"] = dict(type_stats)
    return result


# ============================================================
# API ENDPOINT'LERİ
# ============================================================
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
        open_sigs = [s for s in signals_db if s.get("status") == "open"]
    return jsonify(open_sigs)


@app.route("/api/signal/<signal_id>", methods=["DELETE"])
def delete_signal(signal_id):
    """Tek bir sinyali siler."""
    if AUTH_TOKEN:
        token = request.headers.get("Authorization", "").replace("Bearer ", "")
        if token != AUTH_TOKEN:
            return jsonify({"error": "unauthorized"}), 401

    with _lock:
        before = len(signals_db)
        signals_db[:] = [s for s in signals_db if s.get("id") != signal_id]
        after = len(signals_db)
        if before != after:
            save_signals()
            return jsonify({"ok": True, "deleted": signal_id})
        return jsonify({"error": "not found"}), 404


@app.route("/api/signals/clear-test", methods=["POST"])
def clear_test_signals():
    """source=test olan sinyalleri temizler."""
    if AUTH_TOKEN:
        token = request.headers.get("Authorization", "").replace("Bearer ", "")
        if token != AUTH_TOKEN:
            return jsonify({"error": "unauthorized"}), 401

    with _lock:
        before = len(signals_db)
        signals_db[:] = [s for s in signals_db if s.get("source") != "test"]
        after = len(signals_db)
        save_signals()
    return jsonify({"ok": True, "removed": before - after})


# ============================================================
# HTML DASHBOARD
# ============================================================
def fmt_price(p):
    if p is None:
        return "—"
    p = float(p)
    if p >= 100:
        return f"{p:.2f}"
    if p >= 1:
        return f"{p:.3f}"
    if p >= 0.01:
        return f"{p:.4f}"
    return f"{p:.6f}"


def pct_color(pct):
    if pct is None:
        return "#8a9bb0", "—"
    pct = float(pct)
    color = "#2ecc71" if pct > 0 else ("#e74c3c" if pct < 0 else "#8a9bb0")
    return color, f"{pct:+.2f}%"


def status_badge(status):
    colors = {
        "open": ("#3498db", "AÇIK"),
        "win": ("#2ecc71", "WIN"),
        "loss": ("#e74c3c", "LOSS"),
        "expired": ("#f39c12", "EXPIRED"),
    }
    c, label = colors.get(status, ("#8a9bb0", status.upper()))
    return f'<span style="background:{c}22;color:{c};padding:2px 8px;border-radius:3px;font-size:.7rem;font-weight:bold">{label}</span>'


def type_badge(sig):
    sig_type = sig.get("sig_type", "unknown")
    sub = sig.get("sub_type", "")
    source = sig.get("source", "bot")

    if source == "smc":
        phase = sig.get("phase", "")
        return f'<span style="background:#e67e2222;color:#e67e22;padding:2px 6px;border-radius:3px;font-size:.65rem">SMC {phase}</span>'

    colors = {
        "dip": "#2ecc71",
        "trend": "#3498db",
        "birikim": "#9b59b6",
        "tp": "#e67e22",
    }
    c = colors.get(sig_type, "#8a9bb0")
    label = sig_type.upper()
    if sub:
        label += f" {sub}"
    return f'<span style="background:{c}22;color:{c};padding:2px 6px;border-radius:3px;font-size:.65rem">{label}</span>'


@app.route("/")
def dashboard():
    perf = calc_performance()
    now = tr_now_str()

    with _lock:
        all_sigs = list(signals_db)

    open_sigs = [s for s in all_sigs if s.get("status") == "open"]
    closed_sigs = [s for s in all_sigs if s.get("status") != "open"]

    # Açık pozisyonlar tablosu
    open_rows = ""
    for sig in open_sigs[:50]:
        cur_c, cur_s = pct_color(sig.get("current_pct"))
        peak_c, peak_s = pct_color(sig.get("peak_pct"))
        low_c, low_s = pct_color(sig.get("low_pct"))
        sym = sig["symbol"].replace("/USDT", "")
        open_time = (sig.get("open_time", ""))[:16]

        open_rows += f"""<tr>
            <td style="color:#ecf0f1"><b>{sym}</b></td>
            <td>{type_badge(sig)}</td>
            <td>{fmt_price(sig['entry'])}</td>
            <td style="color:{cur_c};font-weight:bold">{fmt_price(sig.get('current_price'))} ({cur_s})</td>
            <td style="color:{peak_c}">{peak_s}</td>
            <td style="color:{low_c}">{low_s}</td>
            <td>{fmt_price(sig['stop'])}</td>
            <td>{fmt_price(sig['tp1'])} / {fmt_price(sig.get('tp2'))}</td>
            <td>{'✅' if sig.get('tp1_hit') else '—'}</td>
            <td style="font-size:.7rem;color:#7f8c8d">{open_time}</td>
        </tr>"""

    # Kapanmış işlemler tablosu
    closed_rows = ""
    for sig in closed_sigs[:100]:
        close_c, close_s = pct_color(sig.get("close_pct"))
        peak_c, peak_s = pct_color(sig.get("peak_pct"))
        sym = sig["symbol"].replace("/USDT", "")
        open_time = (sig.get("open_time", ""))[:16]
        close_time = (sig.get("close_time") or "")[:16]

        closed_rows += f"""<tr>
            <td style="color:#ecf0f1"><b>{sym}</b></td>
            <td>{type_badge(sig)}</td>
            <td>{status_badge(sig.get('status', 'unknown'))}</td>
            <td>{fmt_price(sig['entry'])}</td>
            <td style="color:{close_c};font-weight:bold">{close_s}</td>
            <td style="color:{peak_c}">{peak_s}</td>
            <td>{'✅' if sig.get('tp1_hit') else '—'}</td>
            <td>{'✅' if sig.get('tp2_hit') else '—'}</td>
            <td style="font-size:.7rem;color:#7f8c8d">{open_time}</td>
            <td style="font-size:.7rem;color:#7f8c8d">{close_time}</td>
        </tr>"""

    # Tür bazlı kırılım tablosu
    type_rows = ""
    for tk, ts in sorted(perf.get("by_type", {}).items()):
        wr = ts.get("win_rate", 0)
        wr_color = "#2ecc71" if wr >= 60 else ("#f39c12" if wr >= 40 else "#e74c3c")
        pnl = ts.get("total_pnl", 0)
        pnl_color = "#2ecc71" if pnl > 0 else ("#e74c3c" if pnl < 0 else "#8a9bb0")
        type_rows += f"""<tr>
            <td style="color:#ecf0f1;font-weight:bold">{tk}</td>
            <td>{ts.get('total', 0)}</td>
            <td style="color:#3498db">{ts.get('open', 0)}</td>
            <td style="color:#2ecc71">{ts.get('wins', 0)}</td>
            <td style="color:#e74c3c">{ts.get('losses', 0)}</td>
            <td style="color:#f39c12">{ts.get('expired', 0)}</td>
            <td style="color:{wr_color};font-weight:bold">%{wr}</td>
            <td>{ts.get('tp1_hits', 0)}</td>
            <td>{ts.get('tp2_hits', 0)}</td>
            <td style="color:{pnl_color};font-weight:bold">{pnl:+.2f}%</td>
            <td>{ts.get('avg_peak', 0)}%</td>
        </tr>"""

    # Günlük performans (son 14 gün)
    daily_rows = ""
    daily_data = perf.get("daily", {})
    for day_key in sorted(daily_data.keys(), reverse=True)[:14]:
        d = daily_data[day_key]
        pnl = d.get("pnl", 0)
        pnl_c = "#2ecc71" if pnl > 0 else ("#e74c3c" if pnl < 0 else "#8a9bb0")
        daily_rows += f"""<tr>
            <td style="color:#ecf0f1">{day_key}</td>
            <td>{d.get('trades', 0)}</td>
            <td style="color:#2ecc71">{d.get('wins', 0)}</td>
            <td style="color:#e74c3c">{d.get('losses', 0)}</td>
            <td style="color:{pnl_c};font-weight:bold">{pnl:+.2f}%</td>
        </tr>"""

    # Stat card helper
    total_pnl = perf.get("total_pnl", 0)
    pnl_color = "#2ecc71" if total_pnl > 0 else ("#e74c3c" if total_pnl < 0 else "#8a9bb0")

    html = f"""<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8">
<title>Portföy Takip v1.0</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="60">
<style>
  :root {{
    --bg: #0a0e14;
    --card: #0f1319;
    --border: #1a2030;
    --text: #c0cdd8;
    --text-dim: #5a6a7a;
    --accent: #00b4d8;
    --green: #2ecc71;
    --red: #e74c3c;
    --orange: #f39c12;
    --purple: #9b59b6;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    background: var(--bg);
    color: var(--text);
    font-family: 'JetBrains Mono', 'Fira Code', 'Cascadia Code', 'Consolas', monospace;
    padding: 20px;
    max-width: 1200px;
    margin: 0 auto;
    line-height: 1.5;
  }}
  .header {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 24px;
    padding-bottom: 16px;
    border-bottom: 1px solid var(--border);
  }}
  .header h1 {{
    color: var(--accent);
    font-size: 1.1rem;
    letter-spacing: 3px;
  }}
  .header .time {{
    color: var(--text-dim);
    font-size: .75rem;
  }}
  .cards {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(130px, 1fr));
    gap: 10px;
    margin-bottom: 24px;
  }}
  .card {{
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 14px;
    text-align: center;
  }}
  .card .val {{
    font-size: 1.4rem;
    font-weight: bold;
    color: var(--accent);
    display: block;
    margin-bottom: 4px;
  }}
  .card .lbl {{
    font-size: .6rem;
    color: var(--text-dim);
    text-transform: uppercase;
    letter-spacing: 1px;
  }}
  .section {{
    margin-bottom: 28px;
  }}
  .section h2 {{
    color: var(--accent);
    font-size: .85rem;
    letter-spacing: 2px;
    margin-bottom: 12px;
    padding-bottom: 6px;
    border-bottom: 1px solid var(--border);
  }}
  table {{
    width: 100%;
    border-collapse: collapse;
    font-size: .75rem;
  }}
  th {{
    background: var(--card);
    color: var(--text-dim);
    font-size: .6rem;
    text-transform: uppercase;
    letter-spacing: 1px;
    padding: 8px 10px;
    text-align: left;
    border-bottom: 1px solid var(--border);
    position: sticky;
    top: 0;
  }}
  td {{
    padding: 8px 10px;
    border-bottom: 1px solid #0d111a;
    vertical-align: middle;
  }}
  tr:hover td {{
    background: var(--card);
  }}
  .table-wrap {{
    overflow-x: auto;
    border: 1px solid var(--border);
    border-radius: 6px;
  }}
  .empty {{
    color: var(--text-dim);
    padding: 20px;
    text-align: center;
    font-size: .8rem;
  }}
  .footer {{
    color: var(--text-dim);
    font-size: .6rem;
    margin-top: 20px;
    padding-top: 12px;
    border-top: 1px solid var(--border);
    text-align: center;
  }}
  @media (max-width: 768px) {{
    body {{ padding: 10px; }}
    .cards {{ grid-template-columns: repeat(3, 1fr); }}
    table {{ font-size: .65rem; }}
    td, th {{ padding: 6px 6px; }}
  }}
</style>
</head>
<body>

<div class="header">
    <h1>📊 PORTFÖY TAKİP</h1>
    <span class="time">{now} | v1.0</span>
</div>

<div class="cards">
    <div class="card">
        <span class="val">{perf.get('total', 0)}</span>
        <span class="lbl">Toplam Sinyal</span>
    </div>
    <div class="card">
        <span class="val" style="color:#3498db">{perf.get('open', 0)}</span>
        <span class="lbl">Açık</span>
    </div>
    <div class="card">
        <span class="val" style="color:var(--green)">{perf.get('wins', 0)}</span>
        <span class="lbl">Win</span>
    </div>
    <div class="card">
        <span class="val" style="color:var(--red)">{perf.get('losses', 0)}</span>
        <span class="lbl">Loss</span>
    </div>
    <div class="card">
        <span class="val" style="color:var(--orange)">{perf.get('expired', 0)}</span>
        <span class="lbl">Expired</span>
    </div>
    <div class="card">
        <span class="val" style="color:{'var(--green)' if perf.get('win_rate',0) >= 50 else 'var(--red)'}">
            %{perf.get('win_rate', 0)}</span>
        <span class="lbl">Win Rate</span>
    </div>
    <div class="card">
        <span class="val" style="color:{pnl_color}">{total_pnl:+.2f}%</span>
        <span class="lbl">Net P&L</span>
    </div>
    <div class="card">
        <span class="val">{perf.get('avg_peak', 0)}%</span>
        <span class="lbl">Ort. Peak</span>
    </div>
    <div class="card">
        <span class="val" style="color:var(--green)">{perf.get('tp1_hits', 0)}</span>
        <span class="lbl">TP1 Hit</span>
    </div>
</div>

<div class="section">
    <h2>📈 SİNYAL TÜRÜ BAZLI KIRILIM</h2>
    <div class="table-wrap">
    <table>
        <thead><tr>
            <th>Tür</th><th>Toplam</th><th>Açık</th><th>Win</th><th>Loss</th>
            <th>Exp.</th><th>Win Rate</th><th>TP1</th><th>TP2</th>
            <th>Net P&L</th><th>Ort. Peak</th>
        </tr></thead>
        <tbody>
            {type_rows if type_rows else '<tr><td colspan="11" class="empty">Henüz veri yok</td></tr>'}
        </tbody>
    </table>
    </div>
</div>

<div class="section">
    <h2>🔵 AÇIK POZİSYONLAR ({len(open_sigs)})</h2>
    <div class="table-wrap">
    <table>
        <thead><tr>
            <th>Sembol</th><th>Tür</th><th>Giriş</th><th>Şu An</th>
            <th>Peak</th><th>Dip</th><th>Stop</th><th>TP1/TP2</th>
            <th>TP1</th><th>Açılış</th>
        </tr></thead>
        <tbody>
            {open_rows if open_rows else '<tr><td colspan="10" class="empty">Açık pozisyon yok</td></tr>'}
        </tbody>
    </table>
    </div>
</div>

<div class="section">
    <h2>📋 KAPANMIŞ İŞLEMLER (son 100)</h2>
    <div class="table-wrap">
    <table>
        <thead><tr>
            <th>Sembol</th><th>Tür</th><th>Sonuç</th><th>Giriş</th>
            <th>Getiri</th><th>Peak</th><th>TP1</th><th>TP2</th>
            <th>Açılış</th><th>Kapanış</th>
        </tr></thead>
        <tbody>
            {closed_rows if closed_rows else '<tr><td colspan="10" class="empty">Henüz kapanmış işlem yok</td></tr>'}
        </tbody>
    </table>
    </div>
</div>

<div class="section">
    <h2>📅 GÜNLÜK PERFORMANS (son 14 gün)</h2>
    <div class="table-wrap">
    <table>
        <thead><tr>
            <th>Tarih</th><th>İşlem</th><th>Win</th><th>Loss</th><th>P&L</th>
        </tr></thead>
        <tbody>
            {daily_rows if daily_rows else '<tr><td colspan="5" class="empty">Henüz veri yok</td></tr>'}
        </tbody>
    </table>
    </div>
</div>

<div class="footer">
    Portföy Takip v1.0 | Kontrol aralığı: {CHECK_INTERVAL // 60} dk | Expire: {EXPIRE_HOURS}s |
    Son güncelleme: {now}
</div>

</body>
</html>"""
    return html


# ============================================================
# MAIN
# ============================================================
def start_flask():
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, use_reloader=False)


if __name__ == "__main__":
    print("=" * 50, flush=True)
    print("📊 Portföy Takip Sistemi v1.0", flush=True)
    print("=" * 50, flush=True)
    print(f"  Kontrol aralığı : {CHECK_INTERVAL}s ({CHECK_INTERVAL // 60} dk)", flush=True)
    print(f"  Expire süresi   : {EXPIRE_HOURS} saat", flush=True)
    print(f"  Data dizini     : {DATA_DIR}", flush=True)
    print("=" * 50, flush=True)

    load_signals()

    # Position checker thread
    threading.Thread(target=position_checker_loop, daemon=True).start()

    # Flask
    start_flask()
