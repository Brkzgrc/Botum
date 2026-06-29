# -*- coding: utf-8 -*-
"""
Portföy Takip Sistemi v3.0
===========================
SMC CHoCH ROC çıkış: TP seviyesine ulaşınca %2.5 trailing başlar → peak'ten -%2.5 ile çıkar.
PUMP çıkış: hard SL | hard TP | 6h expire | trailing yok.

Kaynak: brkzgrc/Botum repo — bu dosya Render'a doğrudan deploy edilir.
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
from claude_analyzer import (process_and_send as _analyzer_process,
                             start_market_watcher as _start_market_watcher,
                             update_archive_outcome as _update_archive_outcome)
from intraday_scanner import start_intraday_scanner

TR_TZ = timezone(timedelta(hours=3))
DATA_DIR = os.getenv("DATA_DIR", "/tmp")
SIGNALS_FILE = os.path.join(DATA_DIR, "portfolio_signals.json")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "300"))
EXPIRE_HOURS = int(os.getenv("EXPIRE_HOURS", "48"))
SHADOW_EXPIRE_HOURS = int(os.getenv("SHADOW_EXPIRE_HOURS", "72"))
AUTH_TOKEN   = os.getenv("PORTFOLIO_AUTH_TOKEN", "")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
CMC_API_KEY  = os.getenv("CMC_API_KEY", "")
GITHUB_REPO  = "brkzgrc/Botum"
GITHUB_FILE  = "portfolio_snapshot.json"
BINANCE_KLINE_URL = "https://api.binance.com/api/v3/klines"
TRAIL_PCT      = 3.0   # bot.py TRAILING_PCT ile eşleşir — peak'in %3 altında kapanır
# PUMP sinyalleri: hard SL + sabit expire (trailing yok)
BOT_EXPIRE_H   = {"pump": 6}   # PUMP için 6h expire
# Ana SMC kaynak listesi — "smc-v2" tek aktif SMC sinyali
SMC_MAIN_SOURCES = ("smc-v2",)

# smc-v2: TP1 aktivasyon → %100 pozisyon %2.5 trailing ile çıkar
FULL_TRAIL_SOURCES  = {"smc-v2"}
SMC_FULL_TRAIL_PCT  = 2.5

# Kaldırılmış sinyal tipleri (sig_type) — DB'de kalır ama UI'da gösterilmez.
HIDDEN_SIG_TYPES = (
    "pump_probability", "pump_prob", "pump_watch",   # eski PUMP_PROBABILITY sistemi
    "panik_pump",                                     # eski PANİK PUMP sistemi (2026-06-28 kaldırıldı)
    "rocket",                                         # eski ROCKET sistemi
    "t24", "t72", "t168",                            # eski T24/T72/T168 sistemleri
)

# Kaldırılmış sinyal kaynakları (source) — DB'de kalır ama UI'da gösterilmez.
# smc-eski-choch-v2: smc-v2 öncülü, gelecekte analyzer için saklanıyor.
HIDDEN_SOURCES = ("smc-eski-discount", "smc-eski-choch", "smc-eski-choch-v2")

app = Flask(__name__)

import logging
logging.getLogger("werkzeug").setLevel(logging.ERROR)

signals_db = []
_lock = threading.Lock()

_ARCHIVE_FILE = os.path.join(DATA_DIR, "learning_archive.json")

def _restore_archive_from_github():
    """Eğer local archive yoksa GitHub'dan çeker."""
    if os.path.exists(_ARCHIVE_FILE) or not GITHUB_TOKEN:
        return
    try:
        headers = {"Authorization": f"token {GITHUB_TOKEN}",
                   "Accept": "application/vnd.github+json"}
        r = requests.get(
            f"https://api.github.com/repos/{GITHUB_REPO}/contents/learning_archive.json?ref=data",
            headers=headers, timeout=10)
        if r.status_code == 200:
            import base64
            content = base64.b64decode(r.json()["content"]).decode()
            with open(_ARCHIVE_FILE, "w", encoding="utf-8") as f:
                f.write(content)
            data = json.loads(content)
            print(f"[ARCHIVE] GitHub'dan geri yüklendi: {len(data)} kayıt.", flush=True)
    except Exception as e:
        print(f"[ARCHIVE] GitHub'dan geri yükleme hatası: {e}", flush=True)

def load_signals():
    global signals_db
    try:
        if os.path.exists(SIGNALS_FILE):
            with open(SIGNALS_FILE, "r", encoding="utf-8") as f:
                signals_db = json.load(f)
            print(f"[DB] {len(signals_db)} sinyal yüklendi.", flush=True)
            _migrate_signals()
        else:
            signals_db = []
    except Exception as e:
        print(f"[DB] Yükleme hatası: {e}", flush=True)
        signals_db = []
    _restore_archive_from_github()

def _migrate_signals():
    """Eski DB kayıtlarındaki bilinen hataları düzelt."""
    fixed = 0

    for sig in signals_db:
        # ROCKET yeniden adlandırma: eski "momentum_devam" → "rocket"
        if sig.get("sig_type") == "momentum_devam":
            sig["sig_type"] = "rocket"
            fixed += 1
        # Eski half_open kayıtlarını win_partial'a çevir (artık bu statü yok)
        if sig.get("status") == "half_open":
            sig["status"] = "win_partial"
            if not sig.get("close_pct"):
                sig["close_pct"] = sig.get("tp1_exit_pct", 0)
            if not sig.get("close_time"):
                sig["close_time"] = sig.get("tp1_time") or sig.get("open_time")
            if not sig.get("close_reason"):
                sig["close_reason"] = "legacy_half"
            fixed += 1
        # tp3 shadow alanlarını yoksa ekle
        if "tp3_shadow" not in sig:
            sig["tp3_shadow"] = None
            sig["tp3_trail_peak"] = 0.0
            sig["tp3_trail_stop_pct"] = 2.5
            sig["tp3_hit_time"] = None
            fixed += 1
    if fixed:
        save_signals()
        print(f"[DB] Migrasyon: {fixed} kayıt düzeltildi.", flush=True)

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
        "tp2": float(data.get("tp2") or 0) or None,
        "tp3": float(data.get("tp3") or 0) or None,
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
        "tp3_shadow": None, "tp3_trail_peak": 0.0, "tp3_trail_stop_pct": 2.5, "tp3_hit_time": None,
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
                  if s["status"] == "open"
                  or s.get("tp3_shadow") == "watching"]
    if not active:
        return

    open_count = sum(1 for s in active if s["status"] == "open")
    tp3_count = sum(1 for s in active if s.get("tp3_shadow") == "watching")
    print(f"[CHECK] {open_count} açık + {tp3_count} TP3 shadow takip...", flush=True)

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

            is_smc = sig.get("source", "bot") in SMC_MAIN_SOURCES
            close_reason = None; close_price = None

            if is_smc:
                # SMC CHoCH ROC: stop → loss | TP1 hit → %2.5 trailing aktif | trail tetik → win_trail/loss
                if low <= stop:
                    close_reason = "stop"; close_price = stop
                    sig["status"] = "loss"
                else:
                    if tp1 and high >= tp1 and not sig.get("tp1_hit"):
                        if sig.get("source") in FULL_TRAIL_SOURCES:
                            sig["tp1_hit"] = True; sig["tp1_time"] = now.isoformat()
                            tp1_pct_v = round((tp1 - entry) / entry * 100, 2)
                            sig["tp1_pct"] = tp1_pct_v
                            need_save = True
                            print(f"  🟡 TP TRAIL AKTİF: {symbol.replace('/USDT','')} | +{tp1_pct_v:.2f}% → %{SMC_FULL_TRAIL_PCT} trailing başladı", flush=True)
                    if sig.get("tp1_hit") and sig.get("source") in FULL_TRAIL_SOURCES:
                        trail_stop = round(sig["peak_price"] * (1 - SMC_FULL_TRAIL_PCT / 100), 8)
                        trail_ret  = round((trail_stop - entry) / entry * 100, 2)
                        if low <= trail_stop:
                            close_reason = "trailing"; close_price = trail_stop
                            sig["status"] = "win_trail" if trail_ret > 0 else "loss"
            else:
                is_pump = sig.get("sig_type") == "pump"
                if is_pump:
                    # PUMP: hard SL, hard TP, 6h expire — trailing yok
                    if tp1 and high >= tp1 and not sig.get("tp1_hit"):
                        sig["tp1_hit"] = True; sig["tp1_time"] = now.isoformat()
                        need_save = True
                        print(f"  🎯 PUMP TP HİT: {symbol.replace('/USDT','')} | +{round((tp1-entry)/entry*100,1)}%", flush=True)
                    if tp2 and high >= tp2:
                        close_reason = "tp2"; close_price = tp2
                        sig["status"] = "win_tp2"
                    elif low <= stop:
                        close_reason = "stop"; close_price = stop
                        sig["status"] = "loss"
                    else:
                        open_time = datetime.fromisoformat(sig["open_time"])
                        if open_time.tzinfo is None: open_time = open_time.replace(tzinfo=TR_TZ)
                        if (now - open_time).total_seconds() / 3600 >= BOT_EXPIRE_H["pump"]:
                            close_reason = "expired"; close_price = close; sig["status"] = "expired"
                else:
                    # Diğer bot sinyalleri: trailing stop primary exit (bot.py ile eşleşir)
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
                try:
                    _update_archive_outcome(sig.get("id", ""), close_reason,
                                            sig["close_pct"], sig["peak_pct"], sig["open_time"])
                except Exception as _ae:
                    print(f"[ARCHIVE] {_ae}", flush=True)


        # TP3 shadow takibi (SMC sinyali TP2'de kapandıktan sonra TP3 izleme)
        elif sig.get("tp3_shadow") == "watching" and sig.get("status") == "win_tp2":
            tp3_val = sig.get("tp3")
            if tp3_val:
                if high > sig.get("tp3_trail_peak", 0):
                    sig["tp3_trail_peak"] = high
                    need_save = True
                trail_stop_pct = sig.get("tp3_trail_stop_pct", 2.5)
                trail_stop = round(sig["tp3_trail_peak"] * (1 - trail_stop_pct / 100), 8)
                if high >= tp3_val:
                    sig["tp3_shadow"] = "başarılı"
                    sig["tp3_hit_time"] = now.isoformat()
                    need_save = True
                    print(f"  🌟 TP3 SHADOW BAŞARILI: {symbol.replace('/USDT','')} | TP3 ulaşıldı!", flush=True)
                elif low <= trail_stop:
                    sig["tp3_shadow"] = "durdu"
                    need_save = True
                    print(f"  ⚡ TP3 SHADOW DURDU: {symbol.replace('/USDT','')} | Trailing tetiklendi", flush=True)
                else:
                    ref_time_str = sig.get("tp2_time") or sig.get("close_time") or sig["open_time"]
                    try:
                        ref_dt = datetime.fromisoformat(ref_time_str)
                        if ref_dt.tzinfo is None: ref_dt = ref_dt.replace(tzinfo=TR_TZ)
                        elapsed = (now - ref_dt).total_seconds() / 3600
                    except Exception:
                        elapsed = 0
                    if elapsed >= SHADOW_EXPIRE_HOURS:
                        sig["tp3_shadow"] = "durdu"
                        need_save = True
                        print(f"  ⏰ TP3 SHADOW SÜRE DOLDU: {symbol.replace('/USDT','')} | {SHADOW_EXPIRE_HOURS}s geçti", flush=True)
            else:
                sig["tp3_shadow"] = "durdu"
                need_save = True

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
    all_sigs = [s for s in all_sigs if s.get("sig_type", "unknown") not in HIDDEN_SIG_TYPES
                and s.get("source", "bot") not in HIDDEN_SOURCES]

    result = {
        "total": len(all_sigs),
        "open": 0, "closed": 0,
        "wins": 0, "win_tp2": 0, "losses": 0, "expired": 0, "tp1_hits": 0,
        "total_pnl": 0.0, "win_loss_pnl": 0.0, "expired_pnl": 0.0,
        "avg_peak": 0.0, "win_rate": 0.0, "real_win_rate": 0.0,
        "analyzer": {
            "gir":     {"total": 0, "wins": 0, "losses": 0, "pnl": 0.0},
            "dikkat":  {"total": 0, "wins": 0, "losses": 0, "pnl": 0.0},
            "riskli":  {"total": 0, "wins": 0, "losses": 0, "pnl": 0.0},
        },
        "by_type": {}, "daily": {}, "weekly": {}, "monthly": {},
    }

    closed_peaks = []
    type_stats = defaultdict(lambda: {
        "total": 0, "open": 0, "wins": 0, "losses": 0, "expired": 0,
        "tp1_hits": 0, "total_pnl": 0.0, "peaks": [],
        "tp2_hits": 0, "tp2_total": 0, "tp2_extra_pnl": 0.0,
        "expired_pnl_sum": 0.0,
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
        elif source == "smc-v2":
            type_key = "SMC CHoCH ROC"
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

        if status == "open":
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
                if status == "win_tp2": result["win_tp2"] += 1
            elif status == "loss": result["losses"] += 1; ts["losses"] += 1
            elif status == "expired":
                result["expired"] += 1; result["expired_pnl"] += pct
                ts["expired"] += 1; ts["expired_pnl_sum"] += pct

            # analyzer istatistikleri (sadece kapanmış sinyaller)
            ad = sig.get("analyzer_decision", "")
            if ad:
                if "✅" in ad:   bucket_key = "gir"
                elif "⚠️" in ad: bucket_key = "dikkat"
                elif "🚫" in ad: bucket_key = "riskli"
                else:            bucket_key = None
                if bucket_key:
                    ab = result["analyzer"][bucket_key]
                    ab["total"] += 1; ab["pnl"] += pct
                    if status in ("win_tp1", "win_tp2", "win_trail"): ab["wins"] += 1
                    elif status == "loss":                             ab["losses"] += 1

        # Günlük / haftalık / aylık istatistikleri
        _pct_for_time = (sig.get("close_pct", 0) or 0) if status != "open" else 0
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
                    if status in ("win_tp1", "win_tp2", "win_trail"):
                        _tb[_tk]["wins"] += 1
                    elif status == "loss":
                        _tb[_tk]["losses"] += 1
            except Exception: pass

        # Alternatif senaryo hesabı (sadece kapanmış sinyaller)
        if status != "open":
            _is_smc = source in SMC_MAIN_SOURCES
            _entry = sig.get("entry", 0) or 0
            _tp1p = sig.get("tp1"); _tp2p = sig.get("tp2"); _stopp = sig.get("stop")
            _tp1_pct = round((_tp1p - _entry) / _entry * 100, 2) if _tp1p and _entry else 0
            _tp2_pct = round((_tp2p - _entry) / _entry * 100, 2) if _tp2p and _entry else 0
            _stop_pct = round((_stopp - _entry) / _entry * 100, 2) if _stopp and _entry else 0
            _pk = sig.get("peak_pct", 0) or 0
            _dp = sig.get("low_pct", 0) or 0
            _closed_pct = sig.get("close_pct", 0) or 0


    # [SMC-ESKİ] Karşılaştırma istatistikleri — TOPLAM/breakdown'a dahil edilmez
    if closed_peaks:
        result["avg_peak"] = round(sum(closed_peaks) / len(closed_peaks), 2)
    if result["closed"] > 0:
        result["win_rate"] = round(result["wins"] / result["closed"] * 100, 1)
    _real_denom = result["wins"] + result["losses"]
    result["real_win_rate"] = round(result["wins"] / _real_denom * 100, 1) if _real_denom > 0 else 0
    result["expired_pnl"]   = round(result["expired_pnl"], 2)
    result["win_loss_pnl"]  = round(result["total_pnl"] - result["expired_pnl"], 2)
    result["total_pnl"]     = round(result["total_pnl"], 2)
    for bk, bv in result["analyzer"].items():
        dec = bv["wins"] + bv["losses"]
        bv["wr"]  = round(bv["wins"] / dec * 100, 1) if dec > 0 else 0
        bv["pnl"] = round(bv["pnl"], 2)
    for tk, ts in type_stats.items():
        closed = ts["wins"] + ts["losses"] + ts["expired"]
        ts["win_rate"] = round(ts["wins"] / closed * 100, 1) if closed > 0 else 0
        ts["avg_peak"] = round(sum(ts["peaks"]) / len(ts["peaks"]), 2) if ts["peaks"] else 0
        ts["total_pnl"] = round(ts["total_pnl"], 2)
        ts["tp2_extra_pnl"] = round(ts["tp2_extra_pnl"], 2)
        ts["tp2_rate"] = round(ts["tp2_hits"] / ts["tp2_total"] * 100, 1) if ts["tp2_total"] > 0 else 0
        ts["expired_pnl"]  = round(ts["expired_pnl_sum"], 2)
        ts["win_loss_pnl"] = round(ts["total_pnl"] - ts["expired_pnl_sum"], 2)
        del ts["peaks"]
        del ts["expired_pnl_sum"]

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
    portfolio_id = data.get("portfolio_id") or ""
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
        return jsonify([s for s in signals_db if s.get("status") == "open" ])

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
# PİYASA VERİSİ API
# ============================================================
_market_cache = {"data": None, "ts": 0}
_MARKET_CACHE_TTL = 180  # saniye
_dom_anchor   = {"others_d": None, "ts": 0}
_DOM_ANCHOR_TTL = 86400  # 24 saat

def _fetch_market_pulse():
    """BTC/ETH fiyat, F&G, dominans, MVRV, ETF, Altcoin Season — 3 dk cache."""
    now_ts = time.time()
    if _market_cache["data"] and now_ts - _market_cache["ts"] < _MARKET_CACHE_TTL:
        return _market_cache["data"]
    out = {}
    # 0. CoinMarketCap (primary source when key is set)
    if CMC_API_KEY:
        _cmc_h = {"X-CMC_PRO_API_KEY": CMC_API_KEY, "Accept": "application/json"}
        try:
            rg = requests.get(
                "https://pro-api.coinmarketcap.com/v1/global-metrics/quotes/latest",
                headers=_cmc_h, timeout=8)
            gd = rg.json().get("data", {})
            qu = gd.get("quote", {}).get("USD", {})
            out["btc_dominance"] = round(float(gd.get("btc_dominance", 0)), 1)
            out["total_mcap"]    = float(qu.get("total_market_cap", 0))
            eth_d = float(gd.get("eth_dominance", 0))
            btc_d = float(gd.get("btc_dominance", 0))
            out["eth_dominance"] = round(eth_d, 1)
            out["total3"] = out["total_mcap"] * (1 - (btc_d + eth_d) / 100)
            _btc_dc = gd.get("btc_dominance_24h_percentage_change")
            _eth_dc = gd.get("eth_dominance_24h_percentage_change")
            if _btc_dc is not None:
                out["btc_dom_change"] = round(float(_btc_dc), 2)
            if _eth_dc is not None:
                out["eth_dom_change"] = round(float(_eth_dc), 2)
        except Exception as e:
            print(f"[MARKET] CMC global hata: {e}", flush=True)
        # USDT dominance via USDT quote (more reliable)
        if "usdt_dominance" not in out or out.get("usdt_dominance", 0) == 0:
            try:
                ru = requests.get(
                    "https://pro-api.coinmarketcap.com/v2/cryptocurrency/quotes/latest",
                    headers=_cmc_h, params={"symbol": "USDT", "convert": "USD"}, timeout=8)
                usdt_data = ru.json().get("data", {}).get("USDT", [])
                if usdt_data:
                    usdt_mc = float(usdt_data[0]["quote"]["USD"]["market_cap"])
                    tot_mc = out.get("total_mcap", 0)
                    if tot_mc:
                        out["usdt_dominance"] = round(usdt_mc / tot_mc * 100, 1)
            except Exception as e:
                print(f"[MARKET] CMC USDT hata: {e}", flush=True)
        # Altcoin Season: % of top 50 non-stablecoin coins beating BTC 90d change
        try:
            rl = requests.get(
                "https://pro-api.coinmarketcap.com/v1/cryptocurrency/listings/latest",
                headers=_cmc_h,
                params={"limit": 51, "convert": "USD", "sort": "market_cap"}, timeout=10)
            coins = rl.json().get("data", [])
            _stables = {"USDT", "USDC", "BUSD", "DAI", "TUSD", "USDP", "FDUSD",
                        "USDE", "PYUSD", "GUSD", "LUSD", "USDD", "FRAX", "CRVUSD"}
            btc_90d = None
            non_btc = []
            for c in coins:
                sym = c.get("symbol", "")
                pct90 = c.get("quote", {}).get("USD", {}).get("percent_change_90d")
                if sym == "BTC":
                    btc_90d = pct90
                elif sym not in _stables and pct90 is not None:
                    non_btc.append(pct90)
            if btc_90d is not None and non_btc:
                beating = sum(1 for p in non_btc if p > btc_90d)
                out["altcoin_season"] = round(beating / len(non_btc) * 100)
        except Exception as e:
            print(f"[MARKET] CMC Altcoin Season hata: {e}", flush=True)
    try:
        r = requests.get(
            "https://api.binance.com/api/v3/ticker/24hr",
            params={"symbols": '["BTCUSDT","ETHUSDT"]'}, timeout=5)
        for t in r.json():
            sym = t["symbol"]
            if sym == "BTCUSDT":
                out["btc_price"]  = float(t["lastPrice"])
                out["btc_change"] = float(t["priceChangePercent"])
                out["btc_volume"] = float(t["quoteVolume"])
            elif sym == "ETHUSDT":
                out["eth_price"]  = float(t["lastPrice"])
                out["eth_change"] = float(t["priceChangePercent"])
    except Exception as e:
        print(f"[MARKET] Binance hata: {e}", flush=True)
    try:
        r2 = requests.get("https://api.alternative.me/fng/", timeout=5)
        d = r2.json()["data"][0]
        out["fng_value"] = int(d["value"])
        out["fng_class"] = d["value_classification"]
    except Exception as e:
        print(f"[MARKET] F&G hata: {e}", flush=True)
    if "btc_dominance" not in out or "total_mcap" not in out:
        try:
            r3 = requests.get("https://api.coinlore.net/api/global/",
                              headers={"User-Agent": "portfolio-tracker/1.0"}, timeout=8)
            gl = r3.json()[0]
            if "btc_dominance" not in out:
                out["btc_dominance"] = round(float(gl.get("btc_d", "0").replace("%", "")), 1)
            if "total_mcap" not in out:
                out["total_mcap"] = float(gl.get("total_mcap", 0))
        except Exception as e:
            print(f"[MARKET] CoinLore hata: {e}", flush=True)
    if "total3" not in out or "usdt_dominance" not in out:
        try:
            r4 = requests.get("https://api.coingecko.com/api/v3/global",
                              headers={"User-Agent": "portfolio-tracker/1.0"}, timeout=8)
            gd = r4.json()["data"]
            total_usd = gd["total_market_cap"]["usd"]
            mcp       = gd["market_cap_percentage"]
            btc_pct   = mcp.get("btc", 0)
            eth_pct   = mcp.get("eth", 0)
            if "total3" not in out:
                out["total3"] = total_usd * (1 - (btc_pct + eth_pct) / 100)
            if "usdt_dominance" not in out:
                out["usdt_dominance"] = round(float(mcp.get("usdt", 0)), 1)
        except Exception as e:
            print(f"[MARKET] CoinGecko hata: {e}", flush=True)
    # USDT Dom fallback — CoinPaprika (CoinGecko rate-limit yaparsa)
    if "usdt_dominance" not in out:
        try:
            r_cp = requests.get(
                "https://api.coinpaprika.com/v1/tickers/usdt-tether",
                params={"quotes": "USD"}, timeout=6)
            usdt_mcap = r_cp.json().get("quotes", {}).get("USD", {}).get("market_cap", 0)
            tot_mc    = out.get("total_mcap", 0)
            if usdt_mcap and tot_mc:
                out["usdt_dominance"] = round(float(usdt_mcap) / float(tot_mc) * 100, 1)
        except Exception as e:
            print(f"[MARKET] USDT Dom fallback hata: {e}", flush=True)
    if "total3" not in out:
        bp = out.get("btc_price", 0)
        ep = out.get("eth_price", 0)
        tm = out.get("total_mcap", 0)
        if bp and ep and tm:
            est = tm - bp * 19_650_000 - ep * 120_000_000
            if est > 0:
                out["total3"] = est
    if "altcoin_season" not in out:
        try:
            import re as _re
            acs_found = False
            for url in ["https://api.blockchaincenter.net/altcoin-season/",
                        "https://www.blockchaincenter.net/altcoin-season-index/api/"]:
                try:
                    ra = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=6)
                    if ra.ok and ra.headers.get("content-type", "").startswith("application/json"):
                        d = ra.json()
                        for key in ("value", "index", "score", "altcoin_season", "altcoinSeason"):
                            if key in d:
                                val = int(d[key])
                                if 5 <= val <= 100:
                                    out["altcoin_season"] = val
                                    acs_found = True
                                    break
                    if acs_found:
                        break
                except Exception:
                    pass
            if not acs_found:
                r5 = requests.get(
                    "https://www.blockchaincenter.net/altcoin-season-index/",
                    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                             "Accept-Language": "en-US,en;q=0.9"},
                    timeout=12)
                for pat in [r'"altcoinSeasonIndex"\s*:\s*(\d+)', r'"altcoinSeason"\s*:\s*(\d+)',
                            r'"value"\s*:\s*(\d+)', r'season_index[^0-9]{0,20}(\d{1,3})',
                            r'seasonValue[^0-9]{0,10}(\d{1,3})', r'id="season"[^>]*>(\d+)',
                            r'class="gauge[^"]*"[^>]*>.*?(\d{1,3})', r'index[^0-9]{0,15}(\d{1,2})\b']:
                    m = _re.search(pat, r5.text, _re.IGNORECASE | _re.DOTALL)
                    if m:
                        val = int(m.group(1))
                        if 5 <= val <= 100:
                            out["altcoin_season"] = val
                            break
        except Exception as e:
            print(f"[MARKET] Altcoin Season hata: {e}", flush=True)
    try:
        r6 = requests.get(
            "https://fapi.binance.com/fapi/v1/premiumIndex",
            params={"symbol": "BTCUSDT"}, timeout=6)
        out["funding_rate"] = float(r6.json().get("lastFundingRate", 0)) * 100
    except Exception as e:
        print(f"[MARKET] Funding Rate hata: {e}", flush=True)
    try:
        r7 = requests.get(
            "https://fapi.binance.com/futures/data/globalLongShortAccountRatio",
            params={"symbol": "BTCUSDT", "period": "1h", "limit": 1}, timeout=6)
        d7 = r7.json()
        if d7:
            out["long_ratio"]  = round(float(d7[0]["longAccount"]) * 100, 1)
            out["short_ratio"] = round(float(d7[0]["shortAccount"]) * 100, 1)
            out["ls_ratio"]    = round(float(d7[0]["longShortRatio"]), 2)
    except Exception as e:
        print(f"[MARKET] Long/Short hata: {e}", flush=True)
    try:
        import re as _re2
        _hdrs = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        }
        # Step 1: fetch SoSoValue page to get current Next.js buildId
        _ss_page = requests.get(
            "https://sosovalue.com/tr/dashboard/total-crypto-spot-etf-fund-flow",
            headers={**_hdrs, "Accept": "text/html,application/xhtml+xml"},
            timeout=15)
        print(f"[MARKET] SoSoValue page HTTP {_ss_page.status_code}", flush=True)
        _bid_m = _re2.search(r'"buildId"\s*:\s*"([^"]+)"', _ss_page.text)
        if _bid_m:
            _bid = _bid_m.group(1)
            # Step 2: fetch Next.js data with dynamic buildId
            _ss_data = requests.get(
                f"https://sosovalue.com/_next/data/{_bid}/tr/dashboard/total-crypto-spot-etf-fund-flow.json",
                params={"currency": "total-crypto-spot-etf-fund-flow"},
                headers={**_hdrs,
                         "Accept": "application/json",
                         "Referer": "https://sosovalue.com/tr/dashboard/total-crypto-spot-etf-fund-flow"},
                timeout=15)
            print(f"[MARKET] SoSoValue data HTTP {_ss_data.status_code}", flush=True)
            if _ss_data.ok:
                _jd = _ss_data.json()
                # Navigate into pageProps → find a list with flow data
                _props = _jd.get("pageProps", _jd)
                _list  = None
                for _key in ("flowList", "fundFlowList", "list", "data", "flowData", "etfList"):
                    _v = _props.get(_key)
                    if isinstance(_v, list) and _v:
                        _list = _v
                        break
                if _list is None:
                    # deep search: first list with ≥5 items
                    def _find_list(obj, depth=0):
                        if depth > 5: return None
                        if isinstance(obj, list) and len(obj) >= 5: return obj
                        if isinstance(obj, dict):
                            for v in obj.values():
                                r = _find_list(v, depth+1)
                                if r: return r
                        return None
                    _list = _find_list(_props)
                print(f"[MARKET] SoSoValue list len={len(_list) if _list else 0}", flush=True)
                if _list:
                    _flows = []
                    for item in _list:
                        if not isinstance(item, dict): continue
                        v = (item.get("totalNetFlow") or item.get("netFlow") or
                             item.get("total") or item.get("flow") or
                             item.get("netInflow") or item.get("totalInflow") or
                             item.get("fundFlow") or 0)
                        try:
                            fv = float(v)
                            # values may be in $ or $M — normalize to $M
                            if abs(fv) > 1e7:
                                fv = round(fv / 1e6, 1)
                            else:
                                fv = round(fv, 1)
                            _flows.append(fv)
                        except (TypeError, ValueError):
                            pass
                    if len(_flows) >= 5:
                        out["etf_flows"] = _flows
                        out["etf_today"] = _flows[-1]
                        print(f"[MARKET] SoSoValue ETF OK: {len(_flows)} gün", flush=True)
                    else:
                        print(f"[MARKET] SoSoValue: veri parse edilemedi, keys={list((_list[0] if _list else {{}}).keys())}", flush=True)
        else:
            print("[MARKET] SoSoValue: buildId bulunamadı", flush=True)
    except Exception as e:
        print(f"[MARKET] ETF flow hata: {e}", flush=True)
    # others_d hesapla ve 24h anchor güncelle
    _bd = out.get("btc_dominance")
    _ed = out.get("eth_dominance")
    _ud = out.get("usdt_dominance", 0) or 0
    if _bd is not None and _ed is not None:
        _od = round(100 - _bd - _ed - _ud, 1)
        out["others_d"] = _od
        if _dom_anchor["others_d"] is None or (now_ts - _dom_anchor["ts"] > _DOM_ANCHOR_TTL):
            _dom_anchor["others_d"] = _od
            _dom_anchor["ts"] = now_ts
        out["others_d_change"] = round(_od - _dom_anchor["others_d"], 1)
    _market_cache["data"] = out
    _market_cache["ts"]   = now_ts
    return out


@app.route("/api/market-data")
def api_market_data():
    return jsonify(_fetch_market_pulse())


@app.route("/api/btc-candles")
def api_btc_candles():
    tf    = request.args.get("tf", "1h")
    limit = min(int(request.args.get("limit", "200")), 500)
    if tf not in {"1m", "5m", "15m", "1h", "4h", "1d", "1w", "1M"}: tf = "1h"
    try:
        r = requests.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": "BTCUSDT", "interval": tf, "limit": limit},
            timeout=10)
        candles = [{"time": int(k[0])//1000,
                    "open":  float(k[1]), "high": float(k[2]),
                    "low":   float(k[3]), "close": float(k[4]),
                    "volume": float(k[5])} for k in r.json()]
        return jsonify(candles)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/market")
def market_dashboard():
    import math as _math
    mp  = _fetch_market_pulse()
    now = tr_now_str()

    def _mcap_fmt(v):
        if not v: return "—"
        if v >= 1e12: return f"${v/1e12:.2f}T"
        if v >= 1e9:  return f"${v/1e9:.1f}B"
        return f"${v/1e6:.0f}M"

    def _chg_fmt(val):
        if val is None: return "—", "#8a9bb0"
        return f"{val:+.2f}%", ("#2ecc71" if val >= 0 else "#e74c3c")

    def _gauge_svg(value, stops, label_text):
        """Speedometer with smooth gradient arc. stops: [("0%",color),("50%",color),...]"""
        cx, cy = 100, 103
        ro, ri = 80, 57

        def _pt(ang, r):
            return (round(cx + r * _math.cos(ang), 2),
                    round(cy - r * _math.sin(ang), 2))

        gid = "gg" + label_text[:4].replace(" ", "").replace("&", "").replace(";", "")
        s_html = "".join(f'<stop offset="{p}" stop-color="{c}"/>' for p, c in stops)

        ox0, oy0 = _pt(_math.pi, ro)
        ox1, oy1 = _pt(0,        ro)
        ix1, iy1 = _pt(0,        ri)
        ix0, iy0 = _pt(_math.pi, ri)
        arc = (f'M {ox0},{oy0} A {ro},{ro} 0 0,1 {ox1},{oy1} '
               f'L {ix1},{iy1} A {ri},{ri} 0 0,0 {ix0},{iy0} Z')

        # Needle + cap color: nearest gradient stop to current value
        nc = "#5a6a7a"
        if value is not None:
            best = 200
            for off_str, col in stops:
                d = abs(float(off_str.strip('%')) - value)
                if d < best:
                    best, nc = d, col

        parts = [
            f'<defs><linearGradient id="{gid}" x1="0%" y1="0%" x2="100%" y2="0%">'
            f'{s_html}</linearGradient></defs>',
            f'<path d="{arc}" fill="#1a2535"/>',
            f'<path d="{arc}" fill="url(#{gid})"/>',
        ]

        if value is not None:
            ang = _math.pi * (1 - max(0, min(100, value)) / 100)
            nx, ny = _pt(ang, ro - 5)
            parts.append(f'<line x1="{cx}" y1="{cy}" x2="{nx}" y2="{ny}" stroke="{nc}" stroke-width="3" stroke-linecap="round"/>')

        parts.append(f'<circle cx="{cx}" cy="{cy}" r="5" fill="{nc}"/>')

        vt = str(value) if value is not None else "—"
        parts.append(f'<text x="100" y="86" text-anchor="middle" fill="#ecf0f1" font-size="22" font-weight="bold" font-family="monospace">{vt}</text>')
        parts.append(f'<text x="100" y="100" text-anchor="middle" fill="#8a9bb0" font-size="8" font-family="monospace">{label_text}</text>')

        return f'<svg viewBox="0 0 200 115" style="width:100%;max-width:180px;height:auto">{"".join(parts)}</svg>'

    def _arrow(delta):
        if delta is None: return "", "#5a6a7a"
        if delta > 0:     return "↑", "#2ecc71"
        if delta < 0:     return "↓", "#e74c3c"
        return "→", "#5a6a7a"

    # ── BTC ──
    btc_p   = mp.get("btc_price")
    eth_p   = mp.get("eth_price")
    btc_vol = mp.get("btc_volume", 0)
    btc_cs, btc_cc = _chg_fmt(mp.get("btc_change"))
    eth_cs, eth_cc = _chg_fmt(mp.get("eth_change"))
    btc_price_fmt = f"${btc_p:,.0f}" if btc_p else "—"
    eth_price_fmt = f"${eth_p:,.0f}" if eth_p else "—"
    vol_fmt = _mcap_fmt(btc_vol)

    btc_dom = mp.get("btc_dominance")
    btc_dom_fmt = f"%{btc_dom}" if btc_dom else "—"
    btc_dom_label = ("altcoin baskılı" if btc_dom and btc_dom >= 58
                     else "dengeli" if btc_dom and btc_dom >= 50 else "altcoin sezonu")
    btc_dom_lc    = ("#e67e22" if btc_dom and btc_dom >= 58
                     else "#5a6a7a" if btc_dom and btc_dom >= 50 else "#2ecc71")
    btc_dom_ar, btc_dom_arc = _arrow(mp.get("btc_dom_change"))

    # ── ETH ──
    eth_btc = round(eth_p / btc_p, 5) if (eth_p and btc_p) else None
    eth_btc_fmt   = f"{eth_btc}" if eth_btc else "—"
    eth_btc_label = ("btc sezonu" if eth_btc and eth_btc < 0.05
                     else "btc baskılı" if eth_btc and eth_btc < 0.065 else "altcoin güçlü")
    eth_btc_lc    = ("#e74c3c" if eth_btc and eth_btc < 0.05
                     else "#e67e22" if eth_btc and eth_btc < 0.065 else "#2ecc71")

    # ── Altcoin ──
    total3     = mp.get("total3")
    total3_fmt = _mcap_fmt(total3) if total3 else "—"
    eth_dom    = mp.get("eth_dominance")
    eth_dom_fmt = f"%{eth_dom}" if eth_dom is not None else "—"
    eth_dom_ar, eth_dom_arc = _arrow(mp.get("eth_dom_change"))
    others_d   = mp.get("others_d")
    others_d_fmt = f"%{others_d}" if others_d is not None else "—"
    others_d_lc  = ("#2ecc71" if others_d and others_d >= 35
                    else "#f1c40f" if others_d and others_d >= 25
                    else "#e67e22" if others_d is not None else "#5a6a7a")
    others_d_ar, others_d_arc = _arrow(mp.get("others_d_change"))
    acs        = mp.get("altcoin_season")
    acs_label  = ("altcoin sezonu" if acs is not None and acs >= 75
                  else "dengeli" if acs is not None and acs >= 25
                  else "btc sezonu" if acs is not None else "—")
    acs_lc     = ("#2ecc71" if acs is not None and acs >= 75
                  else "#f1c40f" if acs is not None and acs >= 25
                  else "#e67e22" if acs is not None else "#5a6a7a")

    # ── Piyasa ──
    total_mc     = mp.get("total_mcap")
    total_mc_fmt = _mcap_fmt(total_mc) if total_mc else "—"
    usdt_dom     = mp.get("usdt_dominance")
    usdt_dom_fmt = f"%{usdt_dom}" if usdt_dom is not None else "—"
    if usdt_dom is None:
        usdt_dom_label, usdt_dom_lc = "—", "#5a6a7a"
    elif usdt_dom >= 7:
        usdt_dom_label, usdt_dom_lc = "kaçış var", "#e67e22"
    elif usdt_dom >= 5:
        usdt_dom_label, usdt_dom_lc = "yüksek", "#f1c40f"
    else:
        usdt_dom_label, usdt_dom_lc = "normal", "#5a6a7a"

    # ── Funding Rate ──
    fr = mp.get("funding_rate")
    if fr is None:
        fr_fmt, fr_label, fr_lc = "—", "—", "#5a6a7a"
    else:
        fr_fmt = f"{fr:.4f}%"
        if fr < -0.01:
            fr_label, fr_lc = "short baskı", "#2ecc71"
        elif fr < 0.01:
            fr_label, fr_lc = "dengeli", "#ecf0f1"
        elif fr < 0.05:
            fr_label, fr_lc = "longa baskı", "#f1c40f"
        else:
            fr_label, fr_lc = "aşırı long", "#e74c3c"

    # ── Long/Short ──
    ls    = mp.get("ls_ratio")
    lr    = mp.get("long_ratio")
    sr    = mp.get("short_ratio")
    lr_fmt = f"%{lr:.1f}" if lr else "—"
    sr_fmt = f"%{sr:.1f}" if sr else "—"
    if ls is None:
        ls_fmt, ls_label, ls_lc = "—", "—", "#5a6a7a"
    else:
        ls_fmt = f"{ls:.2f}"
        if ls > 1.5:
            ls_label, ls_lc = "çok fazla long", "#e74c3c"
        elif ls > 1.2:
            ls_label, ls_lc = "long ağırlıklı", "#f1c40f"
        elif ls < 0.8:
            ls_label, ls_lc = "short ağırlıklı", "#f1c40f"
        else:
            ls_label, ls_lc = "dengeli", "#5a6a7a"

    # ── Funding Rate bar SVG ──
    _FR_RANGE = 0.10
    if fr is not None:
        _fr_cl  = max(-_FR_RANGE, min(_FR_RANGE, fr))
        _fr_dx  = round(12 + (_fr_cl + _FR_RANGE) / (2 * _FR_RANGE) * 176, 1)
        _fr_tx  = max(24, min(176, _fr_dx))
        fr_bar_svg = (
            '<svg viewBox="0 0 200 68" style="width:100%;height:auto">'
            '<defs><linearGradient id="frg" x1="0%" y1="0%" x2="100%" y2="0%">'
            '<stop offset="0%" stop-color="#e74c3c"/>'
            '<stop offset="35%" stop-color="#e67e22"/>'
            '<stop offset="50%" stop-color="#2ecc71"/>'
            '<stop offset="65%" stop-color="#e67e22"/>'
            '<stop offset="100%" stop-color="#e74c3c"/>'
            '</linearGradient></defs>'
            '<rect x="12" y="30" width="176" height="11" rx="5" fill="#1a2535"/>'
            '<rect x="12" y="30" width="176" height="11" rx="5" fill="url(#frg)"/>'
            '<line x1="100" y1="26" x2="100" y2="44" stroke="#5a6a7a" stroke-width="1" stroke-dasharray="2,2"/>'
            f'<text x="{_fr_tx}" y="20" text-anchor="middle" fill="#ecf0f1" font-size="13" font-weight="bold" font-family="monospace">{fr_fmt}</text>'
            f'<circle cx="{_fr_dx}" cy="36" r="7" fill="#ecf0f1" stroke="#0d1421" stroke-width="2"/>'
            '<text x="12" y="56" text-anchor="start" fill="#8a9bb0" font-size="7" font-family="monospace">-0.1%</text>'
            '<text x="100" y="56" text-anchor="middle" fill="#2ecc71" font-size="7" font-family="monospace">0%</text>'
            '<text x="188" y="56" text-anchor="end" fill="#8a9bb0" font-size="7" font-family="monospace">+0.1%</text>'
            f'<text x="100" y="67" text-anchor="middle" fill="{fr_lc}" font-size="7" font-family="monospace">{fr_label}</text>'
            '</svg>'
        )
    else:
        fr_bar_svg = ('<svg viewBox="0 0 200 68" style="width:100%;height:auto">'
                      '<text x="100" y="38" text-anchor="middle" fill="#5a6a7a" font-size="18" font-family="monospace">—</text></svg>')

    # ── Long/Short bar SVG ──
    if lr is not None and sr is not None:
        _ls_dx = round(12 + (lr / 100) * 176, 1)
        _ls_tx = max(24, min(176, _ls_dx))
        ls_bar_svg = (
            '<svg viewBox="0 0 200 68" style="width:100%;height:auto">'
            '<defs><linearGradient id="lsg" x1="0%" y1="0%" x2="100%" y2="0%">'
            '<stop offset="0%" stop-color="#e74c3c"/>'
            '<stop offset="45%" stop-color="#5a6a7a"/>'
            '<stop offset="55%" stop-color="#5a6a7a"/>'
            '<stop offset="100%" stop-color="#2ecc71"/>'
            '</linearGradient></defs>'
            '<rect x="12" y="30" width="176" height="11" rx="5" fill="#1a2535"/>'
            '<rect x="12" y="30" width="176" height="11" rx="5" fill="url(#lsg)"/>'
            '<line x1="100" y1="26" x2="100" y2="44" stroke="#5a6a7a" stroke-width="1" stroke-dasharray="2,2"/>'
            f'<text x="{_ls_tx}" y="20" text-anchor="middle" fill="#ecf0f1" font-size="13" font-weight="bold" font-family="monospace">{ls_fmt}</text>'
            f'<circle cx="{_ls_dx}" cy="36" r="7" fill="#ecf0f1" stroke="#0d1421" stroke-width="2"/>'
            f'<text x="12" y="56" text-anchor="start" fill="#e74c3c" font-size="7" font-family="monospace">Short {sr_fmt}</text>'
            f'<text x="188" y="56" text-anchor="end" fill="#2ecc71" font-size="7" font-family="monospace">Long {lr_fmt}</text>'
            f'<text x="100" y="67" text-anchor="middle" fill="{ls_lc}" font-size="7" font-family="monospace">{ls_label}</text>'
            '</svg>'
        )
    else:
        ls_bar_svg = ('<svg viewBox="0 0 200 68" style="width:100%;height:auto">'
                      '<text x="100" y="38" text-anchor="middle" fill="#5a6a7a" font-size="18" font-family="monospace">—</text></svg>')

    # ── Gauges ──
    fng_v = mp.get("fng_value")
    fng_c = mp.get("fng_class", "")
    fng_label_tr = {"Extreme Fear": "Aşırı Korku", "Fear": "Korku", "Neutral": "Nötr",
                    "Greed": "Hırs", "Extreme Greed": "Aşırı Hırs"}.get(fng_c, fng_c or "—")
    fng_label_c  = {"Extreme Fear": "#e74c3c", "Fear": "#e67e22", "Neutral": "#f1c40f",
                    "Greed": "#a8e063", "Extreme Greed": "#2ecc71"}.get(fng_c, "#8a9bb0")

    btc_dom_nc = ("#2ecc71" if btc_dom and btc_dom < 50
                  else "#f1c40f" if btc_dom and btc_dom < 58 else "#e67e22")

    fng_svg = _gauge_svg(fng_v,
                         [("0%","#e74c3c"),("25%","#e67e22"),("50%","#f1c40f"),
                          ("75%","#a8e063"),("100%","#2ecc71")],
                         "KORKU &amp; HIR&#x15E;")
    if acs is not None:
        _ax1, _ax2, _ay, _ah = 12, 188, 46, 13
        _aw  = _ax2 - _ax1
        _adx = round(_ax1 + max(0, min(100, acs)) / 100 * _aw, 1)
        acs_svg = (
            '<svg viewBox="0 0 200 80" style="width:100%;height:auto">'
            '<defs><linearGradient id="abg" x1="0%" y1="0%" x2="100%" y2="0%">'
            '<stop offset="0%" stop-color="#e67e22"/>'
            '<stop offset="38%" stop-color="#7a6a62"/>'
            '<stop offset="100%" stop-color="#3498db"/>'
            '</linearGradient></defs>'
            f'<rect x="{_ax1}" y="{_ay}" width="{_aw}" height="{_ah}" rx="6" fill="#1a2535"/>'
            f'<rect x="{_ax1}" y="{_ay}" width="{_aw}" height="{_ah}" rx="6" fill="url(#abg)"/>'
            f'<text x="100" y="26" text-anchor="middle" fill="#ecf0f1" font-size="22" font-weight="bold" font-family="monospace">{acs}</text>'
            f'<circle cx="{_adx}" cy="{_ay + _ah // 2}" r="8" fill="#ecf0f1" stroke="#0d1421" stroke-width="2"/>'
            f'<text x="{_ax1}" y="76" text-anchor="start" fill="#e67e22" font-size="8" font-family="monospace">Bitcoin</text>'
            f'<text x="{_ax2}" y="76" text-anchor="end" fill="#3498db" font-size="8" font-family="monospace">Altcoin</text>'
            '</svg>'
        )
    else:
        acs_svg = ('<svg viewBox="0 0 200 80" style="width:100%;height:auto">'
                   '<text x="100" y="45" text-anchor="middle" fill="#5a6a7a" font-size="18" font-family="monospace">—</text>'
                   '</svg>')
    dom_svg = _gauge_svg(btc_dom,
                         [("0%","#2ecc71"),("50%","#f1c40f"),("100%","#e67e22")],
                         "BTC DOMIN.")

    # ── ETF flows ──
    etf_flows   = mp.get("etf_flows", [])
    etf_today   = mp.get("etf_today")
    etf_flows_j = json.dumps(etf_flows)
    if etf_today is None:
        etf_today_fmt, etf_today_c = "—", "#5a6a7a"
    elif etf_today >= 0:
        etf_today_fmt, etf_today_c = f"+${etf_today:.0f}M", "#2ecc71"
    else:
        etf_today_fmt, etf_today_c = f"-${abs(etf_today):.0f}M", "#e74c3c"

    return f"""<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8"><title>Piyasa</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="120">
<style>
:root{{--bg:#0a0e14;--card:#0f1319;--border:#1a2030;--text:#c0cdd8;--dim:#5a6a7a;--accent:#00b4d8;--green:#2ecc71;--red:#e74c3c;--orange:#e67e22;--yellow:#f1c40f}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:var(--bg);color:var(--text);font-family:'JetBrains Mono','Fira Code',monospace;padding:20px;max-width:1400px;margin:0 auto}}
.header{{display:flex;justify-content:space-between;align-items:center;margin-bottom:18px;padding-bottom:14px;border-bottom:1px solid var(--border)}}
.header h1{{color:var(--accent);font-size:1.1rem;letter-spacing:3px}}
.tabs{{display:flex;gap:6px}}
.tab{{background:#0f1319;border:1px solid var(--border);color:var(--dim);padding:5px 16px;border-radius:4px;text-decoration:none;font-size:.72rem;letter-spacing:1px}}
.tab.active{{border-color:var(--accent);color:var(--accent);background:#00b4d811}}
.time{{color:var(--dim);font-size:.7rem}}
.groups-row{{display:flex;gap:10px;margin-bottom:14px}}
.group{{display:flex;flex-direction:column;background:var(--card);border:1px solid var(--border);border-radius:8px;overflow:hidden;flex:1}}
.group-title{{font-size:.58rem;letter-spacing:2px;color:var(--accent);text-transform:uppercase;padding:6px 12px;border-bottom:1px solid var(--border);background:#0c1219;font-weight:700}}
.group-metrics{{display:flex;flex:1}}
.metric{{flex:1;padding:9px 12px;border-right:1px solid var(--border);display:flex;flex-direction:column;justify-content:space-between}}
.metric:last-child{{border-right:none}}
.m-label{{font-size:.5rem;color:#ecf0f1;letter-spacing:1px;text-transform:uppercase;margin-bottom:4px;font-weight:bold}}
.m-value{{font-size:.95rem;font-weight:bold;color:#ecf0f1;line-height:1.1}}
.m-value.lg{{font-size:1.1rem}}
.m-sub{{font-size:.56rem;margin-top:4px;color:var(--dim)}}
.visual-row{{display:grid;grid-template-columns:repeat(5,1fr);gap:10px;margin-bottom:14px;align-items:stretch}}
.card{{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:8px}}
.card h3{{color:var(--accent);font-size:.58rem;letter-spacing:1.5px;margin-bottom:6px;text-transform:uppercase;text-align:center}}
.gauge-wrap{{display:flex;flex-direction:column;align-items:center;padding-top:2px}}
.gauge-label{{font-size:.8rem;font-weight:bold;margin-top:2px}}
.gauge-sub{{font-size:.5rem;color:var(--dim);margin-top:1px;text-align:center}}
.stat-card-inner{{display:flex;flex-direction:column;align-items:center;justify-content:center;padding:10px 4px 8px}}
</style>
</head>
<body>

<div class="header">
  <div style="display:flex;align-items:center;gap:16px">
    <h1>📊 PORTFÖY TAKİP</h1>
    <div class="tabs">
      <a href="/" class="tab">Portföy</a>
      <a href="/market" class="tab active">Piyasa</a>
    </div>
  </div>
  <span class="time">{now} | v3.1</span>
</div>

<div class="groups-row">
  <div class="group">
    <div class="group-title">₿ Bitcoin</div>
    <div class="group-metrics">
      <div class="metric">
        <div><div class="m-label">Fiyat</div><div class="m-value lg">{btc_price_fmt}</div></div>
        <div class="m-sub" style="color:{btc_cc}">{btc_cs}</div>
      </div>
      <div class="metric">
        <div><div class="m-label">Dominans</div><div class="m-value">{btc_dom_fmt} <span style="font-size:.7rem;color:{btc_dom_arc}">{btc_dom_ar}</span></div></div>
        <div class="m-sub" style="color:{btc_dom_lc}">{btc_dom_label}</div>
      </div>
      <div class="metric">
        <div><div class="m-label">24s Hacim</div><div class="m-value">{vol_fmt}</div></div>
        <div class="m-sub" style="color:var(--dim)">USDT</div>
      </div>
    </div>
  </div>
  <div class="group">
    <div class="group-title">⟠ Ethereum</div>
    <div class="group-metrics">
      <div class="metric">
        <div><div class="m-label">Fiyat</div><div class="m-value lg">{eth_price_fmt}</div></div>
        <div class="m-sub" style="color:{eth_cc}">{eth_cs}</div>
      </div>
      <div class="metric">
        <div><div class="m-label">ETH/BTC</div><div class="m-value">{eth_btc_fmt}</div></div>
        <div class="m-sub" style="color:{eth_btc_lc}">{eth_btc_label}</div>
      </div>
    </div>
  </div>
  <div class="group">
    <div class="group-title">🎯 Altcoin</div>
    <div class="group-metrics">
      <div class="metric">
        <div><div class="m-label">Total3</div><div class="m-value">{total3_fmt}</div></div>
        <div class="m-sub" style="color:var(--dim)">BTC+ETH hariç</div>
      </div>
      <div class="metric">
        <div><div class="m-label">ETH Dom</div><div class="m-value">{eth_dom_fmt} <span style="font-size:.7rem;color:{eth_dom_arc}">{eth_dom_ar}</span></div></div>
        <div class="m-sub" style="color:var(--dim)">ETH dominans</div>
      </div>
      <div class="metric">
        <div><div class="m-label">OTHERS.D</div><div class="m-value" style="color:{others_d_lc}">{others_d_fmt} <span style="font-size:.7rem;color:{others_d_arc}">{others_d_ar}</span></div></div>
        <div class="m-sub" style="color:{others_d_lc}">top10 dışı altcoin</div>
      </div>
    </div>
  </div>
  <div class="group">
    <div class="group-title">🌐 Piyasa</div>
    <div class="group-metrics">
      <div class="metric">
        <div><div class="m-label">Total MCap</div><div class="m-value">{total_mc_fmt}</div></div>
        <div class="m-sub" style="color:var(--dim)">tüm kripto</div>
      </div>
      <div class="metric">
        <div><div class="m-label">USDT Dom</div><div class="m-value">{usdt_dom_fmt}</div></div>
        <div class="m-sub" style="color:{usdt_dom_lc}">{usdt_dom_label}</div>
      </div>
      <div class="metric">
        <div><div class="m-label">Funding</div><div class="m-value" style="color:{fr_lc}">{fr_fmt}</div></div>
        <div class="m-sub" style="color:{fr_lc}">{fr_label}</div>
      </div>
    </div>
  </div>
</div>

<div class="visual-row">
  <div class="card">
    <h3>Korku &amp; Hırs</h3>
    <div class="gauge-wrap">
      {fng_svg}
      <div class="gauge-label" style="color:{fng_label_c}">{fng_label_tr}</div>
      <div class="gauge-sub">alternative.me · günlük</div>
    </div>
  </div>
  <div class="card">
    <h3>Altcoin Season</h3>
    <div class="gauge-wrap">
      {acs_svg}
      <div class="gauge-label" style="color:{acs_lc}">{acs_label}</div>
      <div class="gauge-sub">blockchaincenter.net · günlük</div>
    </div>
  </div>
  <div class="card">
    <h3>BTC Dominans</h3>
    <div class="gauge-wrap">
      {dom_svg}
      <div class="gauge-label" style="color:{btc_dom_nc}">{btc_dom_fmt}</div>
      <div class="gauge-sub">CoinLore · anlık</div>
    </div>
  </div>
  <div class="card">
    <h3>Funding Rate</h3>
    <div class="gauge-wrap">
      {fr_bar_svg}
      <div class="gauge-sub">8 saatlik · Binance BTCUSDT</div>
    </div>
  </div>
  <div class="card">
    <h3>Long / Short</h3>
    <div class="gauge-wrap">
      {ls_bar_svg}
      <div class="gauge-sub">Binance · hesap bazlı · 1s</div>
    </div>
  </div>
</div>

<div class="card" style="padding:10px">
  <div style="height:370px;overflow:hidden;border-radius:6px">
    <iframe width="100%" height="420" frameborder="0"
      src="https://www.theblock.co/data/etfs/bitcoin-etf/spot-bitcoin-etf-flows/embed"
      title="Spot Bitcoin ETF Flows"
      style="display:block;margin-top:-2px"></iframe>
  </div>
</div>

<div class="card" style="padding:10px;margin-top:14px">
  <div id="tv_chart"></div>
</div>


<script src="https://s3.tradingview.com/tv.js"></script>
<script>
new TradingView.widget({{
  container_id:"tv_chart",width:"100%",height:460,
  symbol:"BINANCE:BTCUSDT",interval:"60",
  timezone:"Europe/Istanbul",theme:"dark",style:"1",locale:"tr",
  toolbar_bg:"#0f1319",hide_side_toolbar:false,allow_symbol_change:true,
  backgroundColor:"#0a0e14",gridColor:"#1a2030"
}});

</script>
</body>
</html>"""


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
        "open":      ("#3498db", "AÇIK"),
        "win_tp1":   ("#2ecc71", "WIN (TP1)"),
        "win_tp2":   ("#27ae60", "WIN (TP2)"),
        "win_trail": ("#27ae60", "WIN (TRAIL)"),
        "loss":      ("#e74c3c", "LOSS"),
        "expired":   ("#f39c12", "EXPIRED"),
    }
    c, label = colors.get(status, ("#8a9bb0", status.upper()))
    return f'<span style="background:{c};color:#0a0e14;padding:2px 8px;border-radius:3px;font-size:.7rem;font-weight:bold;white-space:nowrap">{label}</span>'

def type_badge(sig):
    sig_type = sig.get("sig_type", "unknown")
    sub = sig.get("sub_type", "")
    source = sig.get("source", "bot")
    if source in SMC_MAIN_SOURCES:
        phase = sig.get("phase", "")
        phase_label = "Discount" if phase == "discount" else ("CHoCH" if phase == "choch" else phase.replace('phase', 'P'))
        if source == "smc-v2":
            return f'<span style="border:1px solid #e67e22;color:#d0d0d0;padding:1px 6px;border-radius:3px;font-size:.65rem;white-space:nowrap">SMC CHoCH ROC</span>'
        src_label = ("SMC-T" if source == "smc-trailing" else
                      "SMC-M" if source == "smc-momentum" else "SMC")
        return f'<span style="border:1px solid #e67e22;color:#d0d0d0;padding:1px 6px;border-radius:3px;font-size:.65rem;white-space:nowrap">{src_label} {phase_label}</span>'
    colors = {
        "dip":              "#2ecc71",
        "trend":            "#3498db",
        "birikim":          "#9b59b6",
        "tp":               "#e67e22",
        "pump":             "#ff4444",
        "panik_pump":       "#ff4444",
        "pump_kisa":        "#ff8800",
        "pump_orta":        "#ffcc00",
        "pump_uzun":        "#00cc66",
        "rocket":           "#00ccaa",
    }
    labels = {
        "pump":             "PUMP",
        "panik_pump":       "PANİK PUMP",
        "pump_kisa":        "KISA VADE",
        "pump_orta":        "ORTA VADE (72s)",
        "pump_uzun":        "UZUN VADE (168s)",
        "rocket":           "ROCKET",
    }
    c = colors.get(sig_type, "#8a9bb0")
    label = labels.get(sig_type, sig_type.upper()) + (f" {sub}" if sub else "")
    return f'<span style="border:1px solid {c};color:#d0d0d0;padding:1px 6px;border-radius:3px;font-size:.65rem;white-space:nowrap">{label}</span>'

def tp3_shadow_badge(sig):
    shadow = sig.get("tp3_shadow")
    if shadow == "başarılı":
        return '<span style="background:#2ecc7122;color:#2ecc71;padding:1px 6px;border-radius:3px;font-size:.6rem">🌟 başarılı</span>'
    if shadow == "durdu":
        return '<span style="background:#e74c3c22;color:#e74c3c;padding:1px 6px;border-radius:3px;font-size:.6rem">⚡ durdu</span>'
    if shadow == "watching":
        return '<span style="background:#3498db22;color:#3498db;padding:1px 6px;border-radius:3px;font-size:.6rem">👁 izleniyor</span>'
    return '<span style="color:#5a6a7a;font-size:.6rem">—</span>'

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
    all_sigs = [s for s in all_sigs if s.get("sig_type", "unknown") not in HIDDEN_SIG_TYPES]

    open_sigs = [s for s in all_sigs if s.get("status") == "open"]
    closed_sigs = [s for s in all_sigs if s.get("status") != "open"]

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
        is_smc_sig = sig.get("source", "bot") in SMC_MAIN_SOURCES
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
            _max_h = {"pump": 6, "pump_orta": 72, "pump_uzun": 168}.get(sig.get("sig_type", ""))
            if _max_h:
                _sc = "#f39c12" if elapsed_h >= _max_h * 0.8 else "#7f8c8d"
                sure_cell = f'<span style="font-size:.7rem;color:{_sc}">{elapsed_h}s / {_max_h}s</span>'
            else:
                sure_cell = f'<span style="font-size:.7rem;color:#7f8c8d">+{elapsed_h}s</span>'
        except Exception:
            pass

        is_full_trail_sig = sig.get("source") in FULL_TRAIL_SOURCES
        tp1_milestone = sig.get("tp1_hit")
        if tp1_milestone and is_full_trail_sig:
            _tp1_hit_pct = sig.get("tp1_pct", tp1_pct)
            tp1_cell = (f'<span style="background:#f39c1233;color:#f39c12;padding:1px 5px;border-radius:3px;'
                        f'font-size:.6rem;white-space:nowrap">🟡 TRAIL AKTİF +{_tp1_hit_pct:.2f}%</span>')
        elif tp1_milestone:
            tp1_cell = (f'<span style="background:#2ecc7133;color:#2ecc71;padding:1px 5px;border-radius:3px;font-size:.6rem;white-space:nowrap">✅ +{tp1_pct}% milestone</span>')
        else:
            tp1_cell = f"{fmt_price(sig['tp1'])} (+{tp1_pct}%)"
        open_rows += f"""<tr>
            <td style="color:#ecf0f1"><b>{sym}</b></td><td>{type_badge(sig)}</td>
            <td>{fmt_price(sig['entry'])}</td>
            <td style="color:{cur_c};font-weight:bold">{fmt_price(sig.get('current_price'))} ({cur_s})</td>
            <td style="color:{peak_c}">{peak_s}</td><td style="color:{low_c}">{low_s}</td>
            <td>{stop_cell}</td><td>{tp1_cell}</td>
            <td>{fmt_price(tp2_val)} (+{tp2_pct_open}%)</td>
            <td style="font-size:.7rem;color:#7f8c8d;white-space:nowrap;text-align:center">{datetime.fromisoformat(sig['open_time']).strftime('%d/%m/%Y') if sig.get('open_time') else '—'}<br><span style="font-size:.65rem;color:#5a6a7a">{datetime.fromisoformat(sig['open_time']).strftime('%H:%M') if sig.get('open_time') else ''}</span></td>
            <td>{sure_cell}</td><td>{analyzer_badge(sig)}</td></tr>"""

    closed_rows = ""
    for sig in closed_sigs[:100]:
        close_c, close_s = pct_color(sig.get("close_pct"))
        peak_c, peak_s = pct_color(sig.get("peak_pct"))
        sym = sig["symbol"].replace("/USDT", "")
        tp1_badge = ""
        _cr = sig.get("close_reason", "")
        if sig.get("tp1_hit") and _cr == "trailing":
            _fin_p = sig.get("close_pct", 0)
            tp1_badge = (f'<span style="color:#3498db;font-size:.58rem">'
                         f'TP1 trail aktif → çıkış:{_fin_p:+.2f}%</span>')
        elif sig.get("tp1_hit"):
            tp1_pct_v = round((sig["tp1"] - sig["entry"]) / sig["entry"] * 100, 1) if sig.get("entry", 0) > 0 else 0
            tp1_badge = f'<span style="color:#2ecc71;font-size:.58rem">✓TP1 +{tp1_pct_v}%</span>'
        _is_smc_closed = sig.get("source", "bot") in SMC_MAIN_SOURCES
        tp3_cell = tp3_shadow_badge(sig) if _is_smc_closed else '<span style="color:#2a3a4a;font-size:.6rem">—</span>'

        closed_rows += f"""<tr>
            <td style="color:#ecf0f1"><b>{sym}</b></td><td>{type_badge(sig)}</td>
            <td>{status_badge(sig.get('status','unknown'))}</td>
            <td>{fmt_price(sig['entry'])}</td>
            <td style="color:{close_c};font-weight:bold">{close_s}</td>
            <td style="color:{peak_c}">{peak_s}</td>
            <td>{tp1_badge}</td>
            <td>{tp3_cell}</td>
            <td>{analyzer_badge(sig)}</td>
            <td style="font-size:.7rem;color:#7f8c8d;white-space:nowrap;text-align:center">{datetime.fromisoformat(sig['open_time']).strftime('%d/%m/%Y') if sig.get('open_time') else '—'}<br><span style="font-size:.65rem;color:#5a6a7a">{datetime.fromisoformat(sig['open_time']).strftime('%H:%M') if sig.get('open_time') else ''}</span></td>
            <td style="font-size:.7rem;color:#7f8c8d;white-space:nowrap;text-align:center">{datetime.fromisoformat(sig['close_time']).strftime('%d/%m/%Y') if sig.get('close_time') else '—'}<br><span style="font-size:.65rem;color:#5a6a7a">{datetime.fromisoformat(sig['close_time']).strftime('%H:%M') if sig.get('close_time') else ''}</span></td></tr>"""

    # Sinyal türü tabloları — SMC vs Bot ayrımı
    smc_type_rows = ""
    bot_type_rows = ""
    for tk, ts in sorted(perf.get("by_type", {}).items()):
        wr = ts.get("win_rate", 0)
        wr_c = "#2ecc71" if wr >= 60 else ("#f39c12" if wr >= 40 else "#e74c3c")
        pnl = ts.get("total_pnl", 0)
        pnl_c = "#2ecc71" if pnl > 0 else ("#e74c3c" if pnl < 0 else "#8a9bb0")
        wl_pnl  = ts.get("win_loss_pnl", pnl)
        wl_pnl_c = "#2ecc71" if wl_pnl > 0 else ("#e74c3c" if wl_pnl < 0 else "#8a9bb0")
        exp_pnl  = ts.get("expired_pnl", 0)
        exp_pnl_c = "#2ecc71" if exp_pnl > 0 else ("#e74c3c" if exp_pnl < 0 else "#8a9bb0")
        exp_pnl_cell = f'{exp_pnl:+.2f}%' if ts.get("expired", 0) > 0 else "—"
        tk_label = tk
        row = (f'<tr><td style="color:#ecf0f1;font-weight:bold">{tk_label}</td>'
               f'<td>{ts.get("total",0)}</td><td style="color:#3498db">{ts.get("open",0)}</td>'
               f'<td style="color:#2ecc71">{ts.get("wins",0)}</td><td style="color:#e74c3c">{ts.get("losses",0)}</td>'
               f'<td style="color:#f39c12">{ts.get("expired",0)}</td>'
               f'<td style="color:{wr_c};font-weight:bold">%{wr}</td>'
               f'<td style="color:{wl_pnl_c};font-weight:bold">{wl_pnl:+.2f}%</td>'
               f'<td style="color:{exp_pnl_c}">{exp_pnl_cell}</td>'
               f'<td style="color:{pnl_c}">{pnl:+.2f}%</td>'
               f'<td>{ts.get("avg_peak",0)}%</td></tr>')
        if tk.startswith("SMC"):
            smc_type_rows += row
        else:
            bot_type_rows += row

    type_rows = smc_type_rows + bot_type_rows

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
    <details data-id="analyzer">
    <summary>🤖 CLAUDE ANALYZER PERFORMANSI — "Karar kalitesi ne?"</summary>
    <div class="tp2-stats">
        {_az_row("gir",    "✅ GİR",     "#2ecc71")}
        {_az_row("dikkat", "⚠️ DİKKAT",  "#f39c12")}
        {_az_row("riskli", "🚫 RİSKLİ",  "#e74c3c")}
    </div>
    </details>
</div>"""

    # TP3 shadow section
    tp3_shadow_watching = [s for s in all_sigs if s.get("tp3_shadow") == "watching" and s.get("status") == "win_tp2"]
    tp3_shadow_rows = ""
    for sig in tp3_shadow_watching[:30]:
        sym = sig["symbol"].replace("/USDT", "")
        entry = sig["entry"]
        tp2_close_pct = sig.get("close_pct", 0) or 0
        tp3_val = sig.get("tp3")
        tp3_pct = round((tp3_val - entry) / entry * 100, 1) if tp3_val and entry > 0 else 0
        trail_peak = sig.get("tp3_trail_peak", 0)
        trail_peak_pct = round((trail_peak - entry) / entry * 100, 1) if trail_peak and entry > 0 else 0
        trail_stop_pct_v = sig.get("tp3_trail_stop_pct", 2.5)
        trail_stop_v = round(trail_peak * (1 - trail_stop_pct_v / 100), 8) if trail_peak else 0
        cur_price = sig.get("current_price", entry)
        cur_pct = sig.get("current_pct", 0)
        cur_c = "#2ecc71" if cur_pct > 0 else ("#e74c3c" if cur_pct < 0 else "#8a9bb0")
        remaining = ""
        try:
            ref_time_str = sig.get("tp2_time") or sig.get("close_time") or sig["open_time"]
            ref_dt = datetime.fromisoformat(ref_time_str)
            if ref_dt.tzinfo is None: ref_dt = ref_dt.replace(tzinfo=TR_TZ)
            remaining = f"{max(0, SHADOW_EXPIRE_HOURS - (now_dt - ref_dt).total_seconds() / 3600):.0f}s"
        except Exception:
            pass
        tp3_shadow_rows += f"""<tr>
            <td style="color:#ecf0f1"><b>{sym}</b></td><td>{type_badge(sig)}</td>
            <td style="color:#2ecc71">{tp2_close_pct:+.2f}%</td>
            <td style="color:{cur_c}">{fmt_price(cur_price)} ({cur_pct:+.2f}%)</td>
            <td>{fmt_price(tp3_val)} (+{tp3_pct}%)</td>
            <td style="color:#f39c12">+{trail_peak_pct:.1f}% &nbsp;<span style="color:#5a6a7a;font-size:.6rem">Trail stop: {fmt_price(trail_stop_v)}</span></td>
            <td style="color:#7f8c8d;font-size:.7rem">{remaining}</td></tr>"""

    tp3_shadow_section = ""
    if tp3_shadow_rows:
        tp3_shadow_section = f"""
<div class="section">
    <details data-id="tp3-shadow">
    <summary>🌟 TP3 SHADOW İZLEME ({len(tp3_shadow_watching)})</summary>
    <p class="note">TP2'de tamamen kapandı — TP3'e ulaşabilir mi? %2.5 trailing ile gözlemsel izleme. Gerçek pozisyon yok.</p>
    <div class="table-wrap"><table><thead><tr>
        <th>Sembol</th><th>Tür</th><th>TP2 Kâr</th><th>Şu An</th><th>TP3 Hedef</th><th>Trail Peak / Stop</th><th>Kalan</th>
    </tr></thead><tbody>{tp3_shadow_rows}</tbody></table></div>
    </details>
</div>"""

    expire_trail_threshold_h = round(EXPIRE_HOURS * EXPIRE_TRAIL_THRESHOLD, 1)


    _smc_eski_section = ""
    html = f"""<!DOCTYPE html>
<html lang="tr"><head>
<meta charset="UTF-8"><title>Portföy Takip v2.8</title>
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
.nav-tab{{background:#0f1319;border:1px solid var(--border);color:var(--text-dim);padding:3px 14px;border-radius:4px;text-decoration:none;font-size:.65rem;letter-spacing:.8px;transition:all .15s;}}
.nav-tab:hover,.nav-tab.active{{border-color:var(--accent);color:var(--accent);background:#00b4d811;}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px;margin-bottom:24px;}}
.card{{background:var(--card);border:1px solid var(--border);border-radius:6px;padding:14px;text-align:center;}}
.card .val{{font-size:1.3rem;font-weight:bold;color:var(--accent);display:block;margin-bottom:4px;}}
.card .lbl{{font-size:.55rem;color:var(--text-dim);text-transform:uppercase;letter-spacing:1px;}}
.section{{margin-bottom:28px;}}
.section h2{{color:var(--accent);font-size:.85rem;letter-spacing:2px;margin-bottom:12px;
  padding-bottom:6px;border-bottom:1px solid var(--border);}}
.section summary{{color:var(--accent);font-size:.85rem;letter-spacing:2px;margin-bottom:12px;
  padding-bottom:6px;border-bottom:1px solid var(--border);cursor:pointer;list-style:none;}}
.section summary::-webkit-details-marker{{display:none;}}
.section summary::before{{content:'▸ ';}}
.section details[open] summary::before{{content:'▾ ';}}
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
.tp2-box summary{{color:#3498db;font-size:.8rem;margin-bottom:10px;cursor:pointer;list-style:none;}}
.tp2-box summary::-webkit-details-marker{{display:none;}}
.tp2-box summary::before{{content:'▸ ';}}
.tp2-box details[open] summary::before{{content:'▾ ';}}
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
</style>
<script>
// Details state persistence — runs before body paint to avoid flash
(function(){{
  var P='det_';
  function restore(){{
    document.querySelectorAll('details[data-id]').forEach(function(el){{
      var saved=localStorage.getItem(P+el.dataset.id);
      if(saved==='open') el.open=true;
      else if(saved==='closed') el.open=false;
      el.addEventListener('toggle',function(){{
        localStorage.setItem(P+el.dataset.id, el.open?'open':'closed');
      }});
    }});
  }}
  if(document.readyState==='loading') document.addEventListener('DOMContentLoaded',restore);
  else restore();
}})();
</script>
</head><body>

<div class="header">
    <div style="display:flex;align-items:center;gap:16px">
        <h1>📊 PORTFÖY TAKİP</h1>
        <div style="display:flex;gap:6px">
            <a href="/" class="nav-tab active">Portföy</a>
            <a href="/market" class="nav-tab">Piyasa</a>
        </div>
    </div>
    <span class="time">
        {now} | v2.8
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
  let total=0, open=0, wins=0, losses=0, expired=0, pnl=0, peaks=[];
  for (const [k, v] of Object.entries(BY_TYPE)) {{
    if (!ACTIVE.has(k)) continue;
    total   += v.total     || 0;
    open    += v.open      || 0;
    wins    += v.wins      || 0;
    losses  += v.losses    || 0;
    expired += v.expired   || 0;
    pnl     += v.total_pnl || 0;
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
    <div class="card"><span class="val" style="color:var(--red)" id="c-loss">{perf.get('losses',0)}</span><span class="lbl">Loss</span></div>
    <div class="card"><span class="val" style="color:var(--orange)" id="c-exp">{perf.get('expired',0)}</span><span class="lbl">Expired</span></div>
    <div class="card"><span class="val" id="c-wr" style="color:{'var(--green)' if perf.get('win_rate',0)>=50 else 'var(--red)'}"
        >%{perf.get('win_rate',0)}</span><span class="lbl">Win Rate</span>
        <span style="font-size:.6rem;color:#8a9bb0;display:block">W/L: %{perf.get('real_win_rate',0)}</span></div>
    <div class="card"><span class="val" id="c-pnl" style="color:{pnl_color_val}">{total_pnl:+.2f}%</span><span class="lbl">Net P&L</span>
        <span style="font-size:.6rem;color:#8a9bb0;display:block">W/L: {perf.get('win_loss_pnl',0):+.2f}% | Exp: {perf.get('expired_pnl',0):+.2f}%</span></div>
    <div class="card"><span class="val" id="c-peak">{perf.get('avg_peak',0)}%</span><span class="lbl">Ort. Peak</span></div>
</div>

<div class="section">
    <details data-id="type-breakdown" open>
    <summary>📈 SİNYAL TÜRÜ BAZLI KIRILIM</summary>
    <div class="table-wrap"><table><thead><tr>
        <th>Tür</th><th>Toplam</th><th>Açık</th><th>Win</th><th>Loss</th><th>Exp.</th>
        <th>Win Rate</th><th>W/L P&L</th><th>Exp P&L</th><th>Toplam P&L</th><th>Ort. Peak</th>
    </tr></thead><tbody>
        {type_rows if type_rows else '<tr><td colspan="11" class="empty">Henüz veri yok</td></tr>'}
    </tbody></table></div>
    </details>
</div>

{_smc_eski_section}

{_analyzer_section}

<div class="section">
    <details data-id="open-pos" open>
    <summary>🔵 AÇIK POZİSYONLAR ({len(open_sigs)})</summary>
    <p class="note">SMC CHoCH ROC: TP1 hit → %2.5 trailing başlar → peak'ten -%2.5 ile çıkar | PUMP: hard SL, hard TP, 6h expire.</p>
    <div class="table-wrap"><table><thead><tr>
        <th>Sembol</th><th>Tür</th><th>Giriş</th><th>Şu An</th><th>Peak</th><th>Dip</th>
        <th>Trail/Stop</th><th>TP1</th><th>TP2</th><th>Tarih</th><th>Süre</th><th>Analiz</th>
    </tr></thead><tbody>
        {open_rows if open_rows else '<tr><td colspan="12" class="empty">Açık pozisyon yok</td></tr>'}
    </tbody></table></div>
    </details>
</div>

{tp3_shadow_section}

<div class="section">
    <details data-id="closed-list">
    <summary>📋 KAPANMIŞ İŞLEMLER (son 100)</summary>
    <div class="table-wrap"><table><thead><tr>
        <th>Sembol</th><th>Tür</th><th>Sonuç</th><th>Giriş</th><th>Getiri</th><th>Peak</th>
        <th>TP1 Hit</th><th>TP3</th><th>Analiz</th><th>Açılış</th><th>Kapanış</th>
    </tr></thead><tbody>
        {closed_rows if closed_rows else '<tr><td colspan="11" class="empty">Henüz kapanmış işlem yok</td></tr>'}
    </tbody></table></div>
    </details>
</div>

<div class="section">
    <details data-id="daily-perf">
    <summary>📅 GÜNLÜK PERFORMANS (son 14 gün)</summary>
    <div class="table-wrap"><table><thead><tr>
        <th>Tarih</th><th>İşlem</th><th>Win</th><th>Loss</th><th>P&L</th>
    </tr></thead><tbody>
        {daily_rows if daily_rows else '<tr><td colspan="5" class="empty">Henüz veri yok</td></tr>'}
    </tbody></table></div>
    </details>
</div>

<div class="footer">
    Portföy Takip v3.0 | SMC CHoCH ROC: TP1 hit → %2.5 trailing | PUMP: hard SL/TP, 6h expire | TP3 shadow %2.5 |
    Kontrol: {CHECK_INTERVAL//60}dk | {now}
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
        sigs = [s for s in sigs if s.get("sig_type", "unknown") not in HIDDEN_SIG_TYPES]
        snapshot = {
            "updated_at": tr_now_str(),
            "performance": perf,
            "open": [s for s in sigs if s.get("status") == "open"],
            "closed": [s for s in sigs if s.get("status") != "open"][-50:],
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

        payload = {"message": f"snapshot {tr_now_str()}", "content": encoded, "branch": "data"}
        if sha:
            payload["sha"] = sha

        r = requests.put(api_url, headers=headers, json=payload, timeout=15)
        if r.status_code in (200, 201):
            print(f"[SNAPSHOT] GitHub'a yazıldı.", flush=True)
        else:
            print(f"[SNAPSHOT] GitHub hata {r.status_code}: {r.text[:120]}", flush=True)
    except Exception as e:
        print(f"[SNAPSHOT] Hata: {e}", flush=True)


def push_archive_to_github():
    """Öğrenen arşivi GitHub'a yükler — deploy sonrası veri kaybını önler."""
    if not GITHUB_TOKEN or not os.path.exists(_ARCHIVE_FILE):
        return
    try:
        with open(_ARCHIVE_FILE, "r", encoding="utf-8") as f:
            content = f.read()
        import base64
        encoded = base64.b64encode(content.encode()).decode()
        headers = {"Authorization": f"token {GITHUB_TOKEN}",
                   "Accept": "application/vnd.github+json"}
        api_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/learning_archive.json"
        r = requests.get(api_url, headers=headers, timeout=10)
        sha = r.json().get("sha") if r.status_code == 200 else None
        payload = {"message": f"archive {tr_now_str()}", "content": encoded, "branch": "data"}
        if sha:
            payload["sha"] = sha
        r = requests.put(api_url, headers=headers, json=payload, timeout=15)
        if r.status_code in (200, 201):
            data = json.loads(content)
            print(f"[ARCHIVE] GitHub'a yazıldı ({len(data)} kayıt).", flush=True)
        else:
            print(f"[ARCHIVE] GitHub hata {r.status_code}", flush=True)
    except Exception as e:
        print(f"[ARCHIVE] GitHub hata: {e}", flush=True)


def snapshot_loop():
    time.sleep(60)  # ilk çalıştırmayı biraz geciktir
    while True:
        push_snapshot_to_github()
        push_archive_to_github()
        time.sleep(1800)  # 30 dakikada bir


# ============================================================
# MAIN
# ============================================================
if __name__ == "__main__":
    print("=" * 50, flush=True)
    print("📊 Portföy Takip Sistemi v2.8", flush=True)
    print("   Bot: Trailing %3 (TP1 milestone, TP2 hedef)", flush=True)
    print("   SMC: stop=loss | TP2=win_tp2 direkt | TP1 milestone | TP3 shadow %2.5 trailing", flush=True)
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
