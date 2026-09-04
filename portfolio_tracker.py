# -*- coding: utf-8 -*-
"""
Portföy Takip Sistemi v3.0
===========================
SMC CHoCH ROC giriş: CHoCH+1tick LIMIT BUY → retest bekler (48H). Fill sonrası SL yerleşir.
SMC CHoCH ROC çıkış: TP1 hit → ATR×0.6 trailing → peak'ten -ATR×0.6 ile çıkar (fallback: -%1.84 sabit).
PUMP çıkış: hard SL | hard TP | 6h expire | trailing yok.

Kaynak: brkzgrc/Botum repo — bu dosya Render'a doğrudan deploy edilir.
"""

import html
import json
import os
import re
import shutil
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from collections import defaultdict

import requests
from flask import Flask, request, jsonify, Response, session, redirect
from news_watcher import start_news_watcher
from market_analyzer import start_market_analyzer
from claude_analyzer import (process_and_send as _analyzer_process,
                             analyze_coin_on_demand as _analyzer_current_coin,
                             start_market_watcher as _start_market_watcher,
                             update_archive_outcome as _update_archive_outcome,
                             MANUAL_ANALYZER_MODE as _manual_analyzer_mode,
                             MANUAL_ANALYZER_V2_MODEL as _manual_analyzer_v2_model,
                             GEMINI_API_KEY as _manual_gemini_key,
                             ANTHROPIC_API_KEY as _manual_anthropic_key)
from intraday_scanner import start_intraday_scanner
from liquidity_radar import get_radar, radar_ui_lines
from anton_scanner.gpt_sonnet_analyzer.anton_integration import (
    parse_gpt_symbol as _parse_gpt_analyzer_symbol,
    _run_gpt_analysis as _run_gpt_analyzer,
)

TR_TZ = timezone(timedelta(hours=3))
DATA_DIR = os.getenv("DATA_DIR", "/tmp")
SIGNALS_FILE = os.path.join(DATA_DIR, "portfolio_signals.json")
HISTORY_CORRECTION_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "portfolio_history_correction_20260827.json",
)
HISTORY_CORRECTION_BACKUP = SIGNALS_FILE + ".before_chronology_fix_20260827.bak"
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "300"))
AUTH_TOKEN              = os.getenv("PORTFOLIO_AUTH_TOKEN", "")
DASHBOARD_USER          = os.getenv("DASHBOARD_USER", "")
DASHBOARD_PASS          = os.getenv("DASHBOARD_PASS", "")
FLASK_SECRET_KEY        = os.getenv("FLASK_SECRET_KEY", "")
GITHUB_TOKEN            = os.getenv("GITHUB_TOKEN", "")
CMC_API_KEY             = os.getenv("CMC_API_KEY", "")
TRADING_BOT_URL         = os.getenv("TRADING_BOT_URL", "")
TRADING_BOT_TOKEN       = os.getenv("TRADING_BOT_TOKEN", "")
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
ANALYZER_TELEGRAM_TOKEN = os.getenv("ANALYZER_TELEGRAM_TOKEN", "")
ANALYZER_CHAT_ID = os.getenv("ANALYZER_CHAT_ID") or TELEGRAM_CHAT_ID
ANALYZER_THREAD_ID = int(os.getenv("ANALYZER_THREAD_ID", "38"))
# Pozitif kişisel TELEGRAM_CHAT_ID çoğu kurulumda kullanıcının Telegram user
# id'sidir. İstenirse ANALYZER_ALLOWED_USER_ID ile açıkça geçersiz kılınabilir.
ANALYZER_ALLOWED_USER_ID = os.getenv("ANALYZER_ALLOWED_USER_ID") or (
    TELEGRAM_CHAT_ID if TELEGRAM_CHAT_ID and not TELEGRAM_CHAT_ID.startswith("-") else ""
)
AUTO_ANALYZER_ENABLED = os.getenv("AUTO_ANALYZER_ENABLED", "false").strip().lower() == "true"
MANUAL_ANALYZER_ENABLED = os.getenv("MANUAL_ANALYZER_ENABLED", "true").strip().lower() == "true"
_MANUAL_ANALYZER_INFLIGHT = set()
_MANUAL_ANALYZER_INFLIGHT_LOCK = threading.Lock()
GITHUB_REPO  = "brkzgrc/Botum"
GITHUB_FILE  = "portfolio_snapshot.json"
BINANCE_KLINE_URL = "https://api.binance.com/api/v3/klines"
# PUMP sinyalleri: hard SL + sabit expire (trailing yok)
BOT_EXPIRE_H   = {"pump": 6}   # PUMP için 6h expire
MAX_POSITIONS  = 5              # trading_engine ile aynı değer
OPEN_EXPIRE_H  = 24              # position_monitor.py'deki OPEN_EXPIRE_H ile aynı tutulmalı (sadece görüntüleme)
SPOT_OPPORTUNITY_EXPIRE_H = 24   # Spot Scanner sanal inceleme ufku
SPOT_OPPORTUNITY_TRAIL_PCT = 2.5 # TP1 sonrası peak'ten sabit takip mesafesi
BOT_MISS_THRESHOLD = 2           # _sync_from_trading_bot: art arda kaç periyodik kontrolde
                                  # bot'ta bulunamazsa "open" kaydı kapatılır (tek blip'e güvenilmez)
# Ana SMC kaynak listesi — "smc-v2" tek aktif SMC sinyali
SMC_MAIN_SOURCES = ("smc-v2",)

# smc-v2: TP1 aktivasyon → %100 pozisyon ATR trailing ile çıkar (bot devre dışıyken fallback takip)
FULL_TRAIL_SOURCES  = {"smc-v2"}
ATR_PERIOD          = 14
ATR_MULT            = 0.6      # trail_stop = peak - ATR_MULT * ATR(14, 1H)
ATR_REFRESH_S       = 1800     # ATR en fazla bu kadar saniyede bir yeniden çekilir
FALLBACK_TRAIL_PCT  = 1.84     # ATR çekilemezse: peak'ten bu % ile sabit trailing

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

if FLASK_SECRET_KEY:
    app.secret_key = FLASK_SECRET_KEY
else:
    import secrets as _secrets
    app.secret_key = _secrets.token_hex(32)
    print("[UYARI] FLASK_SECRET_KEY tanımlı değil — oturumlar her restart'ta düşecek. "
          "Render'a FLASK_SECRET_KEY env var'ı ekleyin.", flush=True)
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = True

import logging
logging.getLogger("werkzeug").setLevel(logging.ERROR)


def _safe_next(path):
    if path and path.startswith("/") and not path.startswith("//"):
        return path
    return "/"


LOGIN_PAGE_HTML = """<!DOCTYPE html><html lang="tr"><head>
<meta charset="UTF-8"><title>Giriş — Botum</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root{{--bg:#0a0e14;--card:#0f1319;--border:#1e2a3a;--text:#c9d1d9;--text-dim:#7f8c8d;--accent:#00b4d8;--red:#e74c3c;}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:var(--bg);color:var(--text);font-family:-apple-system,"Segoe UI",Helvetica,Arial,sans-serif;
  display:flex;align-items:center;justify-content:center;min-height:100vh}}
.box{{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:32px;width:100%;max-width:340px}}
h1{{color:var(--accent);font-size:1.2rem;margin-bottom:20px;text-align:center}}
label{{display:block;font-size:.75rem;color:var(--text-dim);margin-bottom:6px;margin-top:14px}}
input[type=text],input[type=password]{{width:100%;background:#0a0e14;border:1px solid var(--border);
  border-radius:6px;padding:9px 10px;color:var(--text);font-size:.9rem}}
input[type=text]:focus,input[type=password]:focus{{outline:none;border-color:var(--accent)}}
.remember{{display:flex;align-items:center;gap:8px;margin-top:16px;font-size:.8rem;color:var(--text-dim)}}
button{{width:100%;margin-top:20px;background:var(--accent);color:#0a0e14;border:none;border-radius:6px;
  padding:10px;font-size:.9rem;font-weight:bold;cursor:pointer}}
.error{{color:var(--red);font-size:.8rem;margin-top:12px;text-align:center}}
</style></head><body>
<div class="box">
  <h1>🔒 Botum Dashboard</h1>
  <form method="POST">
    <input type="hidden" name="next" value="{next_url}">
    <label>Kullanıcı adı</label>
    <input type="text" name="username" autofocus required>
    <label>Şifre</label>
    <input type="password" name="password" required>
    <label class="remember"><input type="checkbox" name="remember" checked style="width:auto"> 30 gün beni hatırla</label>
    <button type="submit">Giriş Yap</button>
    {error_html}
  </form>
</div>
</body></html>"""


@app.route("/login", methods=["GET", "POST"])
def login_page():
    next_url = _safe_next(request.values.get("next", "/"))
    error_html = ""
    if request.method == "POST":
        u = request.form.get("username", "")
        p = request.form.get("password", "")
        if u == DASHBOARD_USER and p == DASHBOARD_PASS:
            session.clear()
            session["authenticated"] = True
            session.permanent = bool(request.form.get("remember"))
            return redirect(next_url)
        error_html = '<div class="error">Kullanıcı adı veya şifre hatalı.</div>'
    return LOGIN_PAGE_HTML.format(next_url=html.escape(next_url, quote=True), error_html=error_html)


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


@app.before_request
def _require_auth():
    if request.path in ("/api/health", "/login", "/logout"):
        return None
    if AUTH_TOKEN:
        bearer = request.headers.get("Authorization", "").replace("Bearer ", "")
        if bearer == AUTH_TOKEN:
            return None
    if not DASHBOARD_USER or not DASHBOARD_PASS:
        return None
    if session.get("authenticated"):
        return None
    return redirect(f"/login?next={_safe_next(request.path)}")

signals_db = []
_lock = threading.Lock()

_ARCHIVE_FILE = os.path.join(DATA_DIR, "learning_archive.json")

def _restore_archive_from_github():
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
            _apply_spot_history_correction_20260827()
        else:
            signals_db = []
    except Exception as e:
        print(f"[DB] Yükleme hatası: {e}", flush=True)
        signals_db = []
    _restore_archive_from_github()

def _sync_from_trading_bot():
    if not TRADING_BOT_URL:
        return
    try:
        hdrs = {"X-Bot-Token": TRADING_BOT_TOKEN} if TRADING_BOT_TOKEN else {}
        r = requests.get(f"{TRADING_BOT_URL}/status", headers=hdrs, timeout=10)
        if not r.ok:
            return
        trade_positions = r.json()
    except Exception as e:
        print(f"[SYNC] Trading bot erişim hatası: {e}", flush=True)
        return

    now_str = tr_now().isoformat()
    updated = 0
    closed = 0
    bot_all_symbols = {sym.replace("/", "").upper() for sym in trade_positions.keys()}
    bot_open_symbols = {
        sym.replace("/", "").upper() for sym, pos in trade_positions.items()
        if pos.get("status") == "open"
    }
    stale = 0
    with _lock:
        for sig in signals_db:
            if sig.get("status") != "open":
                continue
            if sig.get("source") not in SMC_MAIN_SOURCES:
                continue
            sym_norm = sig.get("symbol", "").replace("/", "").upper()
            if sym_norm in bot_open_symbols:
                sig["bot_miss_count"] = 0
                continue
            miss = sig.get("bot_miss_count", 0) + 1
            sig["bot_miss_count"] = miss
            if miss < BOT_MISS_THRESHOLD:
                print(f"[SYNC] {sym_norm} portfolio open ama bot'ta yok ({miss}/{BOT_MISS_THRESHOLD}) — henüz kapatılmadı", flush=True)
                continue
            sig["status"]       = "closed"
            sig["close_time"]   = now_str
            sig["close_reason"] = "sync_closed"
            sig["close_pct"]    = round(
                (sig.get("current_price", sig["entry"]) - sig["entry"]) / sig["entry"] * 100, 2
            ) if sig["entry"] else 0
            closed += 1
            print(f"[SYNC] {sym_norm} portfolio open ama bot'ta yok ({miss}/{BOT_MISS_THRESHOLD}) → kapatıldı", flush=True)

        now_dt_sync = datetime.fromisoformat(now_str).replace(tzinfo=TR_TZ) if "+" not in now_str else datetime.fromisoformat(now_str)
        for sig in signals_db:
            if sig.get("status") != "pending_retest":
                continue
            if sig.get("source") not in SMC_MAIN_SOURCES:
                continue
            sym_norm = sig.get("symbol", "").replace("/", "").upper()
            if sym_norm in bot_all_symbols:
                continue
            try:
                ot = datetime.fromisoformat(sig["open_time"])
                if ot.tzinfo is None: ot = ot.replace(tzinfo=TR_TZ)
                if (now_dt_sync - ot).total_seconds() / 3600 < 48:
                    continue
            except Exception:
                continue
            sig["status"]       = "no_retest"
            sig["close_time"]   = now_str
            sig["close_reason"] = "stale_pending"
            stale += 1
            print(f"[SYNC] {sym_norm} 48H doldu stale pending → no_retest", flush=True)

        for sym, pos in trade_positions.items():
            bot_status = pos.get("status")
            if bot_status not in ("monitoring", "pending", "open"):
                continue
            sym_norm  = sym.replace("/", "").upper()
            sym_slash = sym_norm[:-4] + "/USDT" if sym_norm.endswith("USDT") else sym_norm
            entry     = float(pos.get("entry") or pos.get("limit_price") or 0)
            stop      = float(pos.get("stop") or 0)
            tp1       = float(pos.get("tp1") or 0)
            limit_p   = float(pos.get("limit_price") or entry)
            qty       = float(pos.get("qty") or 0)
            port_status = "open" if bot_status == "open" else "pending_retest"
            use_entry   = entry if bot_status == "open" else limit_p
            already = any(
                s.get("symbol", "").replace("/", "").upper() == sym_norm
                and s.get("status") in ("open", "pending_retest")
                for s in signals_db
            )
            if already:
                if bot_status == "open":
                    for sig in signals_db:
                        sig_sym = sig.get("symbol", "").replace("/", "").upper()
                        if sig_sym == sym_norm and sig.get("status") == "pending_retest":
                            sig["status"]        = "open"
                            sig["entry"]         = entry
                            sig["fill_qty"]      = qty
                            sig["peak_price"]    = entry
                            sig["low_price"]     = entry
                            sig["current_price"] = entry
                            updated += 1
                            print(f"[SYNC] {sym_norm} pending_retest → open", flush=True)
                            break
                continue
            new_sig = {
                "id":            f"sync_{sym_norm}_{now_str[:10]}",
                "symbol":        sym_slash,
                "source":        "smc-v2",
                "sig_type":      pos.get("sig_type", "choch"),
                "status":        port_status,
                "signal_price":  use_entry,
                "entry":         use_entry,
                "limit_price":   limit_p,
                "stop":          stop,
                "tp1":           tp1,
                "open_time":     pos.get("open_time", now_str),
                "peak_price":    use_entry,
                "low_price":     use_entry,
                "current_price": use_entry,
                "peak_pct":      0.0,
                "low_pct":       0.0,
                "current_pct":   0.0,
            }
            if bot_status == "open":
                new_sig["fill_qty"] = qty
            signals_db.append(new_sig)
            updated += 1
            print(f"[SYNC] {sym_norm} bot:{bot_status} → portfolio:{port_status} (yeni)", flush=True)
        if updated or closed or stale:
            save_signals()
    print(f"[SYNC] Tamamlandı: {updated} açıldı, {closed} kapatıldı, {stale} stale temizlendi.", flush=True)


def _migrate_signals():
    fixed = 0
    for sig in signals_db:
        if sig.get("sig_type") == "momentum_devam":
            sig["sig_type"] = "rocket"
            fixed += 1
        if sig.get("source") == "smc" and sig.get("status") in ("open", "pending_retest"):
            sig["source"] = "smc-v2"
            fixed += 1
        if sig.get("status") == "half_open":
            sig["status"] = "win_partial"
            if not sig.get("close_pct"):
                sig["close_pct"] = sig.get("tp1_exit_pct", 0)
            if not sig.get("close_time"):
                sig["close_time"] = sig.get("tp1_time") or sig.get("open_time")
            if not sig.get("close_reason"):
                sig["close_reason"] = "legacy_half"
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


def _apply_spot_history_correction_20260827():
    if not os.path.exists(HISTORY_CORRECTION_FILE):
        return
    try:
        with open(HISTORY_CORRECTION_FILE, "r", encoding="utf-8") as f:
            payload = json.load(f)
        records = payload.get("records") or {}
        if len(records) != 49:
            print(f"[KRONOLOJİ] Güvenlik: 49 yerine {len(records)} düzeltme var; uygulanmadı.", flush=True)
            return

        def same_number(actual, expected):
            try:
                actual = float(actual); expected = float(expected)
                return abs(actual - expected) <= max(1e-12, abs(expected) * 1e-8)
            except (TypeError, ValueError):
                return False

        pending = []
        skipped = []
        for sig in signals_db:
            correction = records.get(sig.get("id"))
            if not correction:
                continue
            if sig.get("source") != "spot-scanner" or sig.get("status") == "open":
                skipped.append(f"{sig.get('id')}: kaynak/durum")
                continue
            if not all((
                same_number(sig.get("entry"), correction.get("expected_entry")),
                same_number(sig.get("tp1"), correction.get("expected_tp1")),
                same_number(sig.get("stop"), correction.get("expected_stop")),
            )):
                skipped.append(f"{sig.get('id')}: fiyat doğrulaması")
                continue
            fields = {
                "status": correction["status"],
                "close_reason": correction["close_reason"],
                "close_price": correction["close_price"],
                "close_pct": correction["close_pct"],
                "close_time": correction["close_time"],
                "tp1_hit": correction["tp1_hit"],
                "tp1_time": correction["tp1_time"],
                "chronology_audit": payload.get("audit_version"),
            }
            if any(sig.get(key) != value for key, value in fields.items()):
                pending.append((sig, fields))
        if not pending:
            print("[KRONOLOJİ] Geçmiş Spot Scanner kayıtları zaten güncel.", flush=True)
            return
        if not os.path.exists(HISTORY_CORRECTION_BACKUP):
            shutil.copy2(SIGNALS_FILE, HISTORY_CORRECTION_BACKUP)
            print(f"[KRONOLOJİ] Yedek oluşturuldu: {HISTORY_CORRECTION_BACKUP}", flush=True)
        for sig, fields in pending:
            sig.update(fields)
        save_signals()
        print(f"[KRONOLOJİ] {len(pending)} kayıt düzeltildi; atlanan={len(skipped)}.", flush=True)
        for item in skipped:
            print(f"[KRONOLOJİ] Atlandı: {item}", flush=True)
    except Exception as e:
        print(f"[KRONOLOJİ] Düzeltme uygulanamadı: {e}", flush=True)

def tr_now():
    return datetime.now(timezone.utc).astimezone(TR_TZ)

def tr_now_str():
    return tr_now().strftime("%Y-%m-%d %H:%M:%S")

def _send_telegram_pt(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "message_thread_id": 2,
                "parse_mode": "HTML",
            },
            timeout=10,
        )
    except Exception as e:
        print(f"[PT] Telegram hata: {e}", flush=True)


def _send_analyzer_thread(text: str):
    if not ANALYZER_TELEGRAM_TOKEN or not ANALYZER_CHAT_ID:
        return
    try:
        payload = {
            "chat_id": ANALYZER_CHAT_ID,
            "message_thread_id": ANALYZER_THREAD_ID,
            "text": text,
            "disable_web_page_preview": True,
        }
        r = requests.post(
            f"https://api.telegram.org/bot{ANALYZER_TELEGRAM_TOKEN}/sendMessage",
            json=payload, timeout=10,
        )
        if not r.ok:
            print(f"[MANUEL ANALYZER TG] HTTP {r.status_code}: {r.text[:120]}", flush=True)
    except Exception as e:
        print(f"[MANUEL ANALYZER TG] Gönderim hatası: {e}", flush=True)


def _parse_manual_analyzer_symbol(text: str):
    value = (text or "").strip().upper()
    value = re.sub(r"^/(?:ANALIZ|ANALYZE)(?:@[A-Z0-9_]+)?\s+", "", value)
    value = value.lstrip("#").replace("/", "").replace("-", "").replace("_", "")
    if value.endswith("USDT"):
        value = value[:-4]
    if not re.fullmatch(r"[A-Z0-9]{2,15}", value):
        return None
    return value + "USDT"


def _latest_spot_scanner_signal(pair: str):
    with _lock:
        matches = [
            dict(s) for s in signals_db
            if s.get("source") == "spot-scanner"
            and s.get("symbol", "").replace("/", "").upper() == pair
        ]
    if not matches:
        return None
    matches.sort(key=lambda s: s.get("open_time") or "", reverse=True)
    stored = matches[0]
    extra = stored.get("extra") if isinstance(stored.get("extra"), dict) else {}
    signal = {
        **extra,
        "symbol": stored.get("symbol", pair[:-4] + "/USDT"),
        "type": stored.get("sig_type", "spot_opportunity"),
        "source": "spot-scanner",
        "entry": stored.get("entry"),
        "stop": stored.get("stop"),
        "tp1": stored.get("tp1"),
        "tp2": stored.get("tp2"),
        "tp3": stored.get("tp3"),
        "setup": stored.get("sub_type", ""),
    }
    return stored, signal


def _run_manual_analyzer(pair: str):
    with _MANUAL_ANALYZER_INFLIGHT_LOCK:
        if pair in _MANUAL_ANALYZER_INFLIGHT:
            print(f"[MANUEL ANALYZER] {pair}: analiz zaten çalışıyor, tekrar yok sayıldı.", flush=True)
            return
        _MANUAL_ANALYZER_INFLIGHT.add(pair)
    try:
        provider = _manual_analyzer_v2_model if _manual_analyzer_mode == "v2" else "Haiku 4.5"
        print(f"[MANUEL ANALYZER] {pair}: mod={_manual_analyzer_mode}, sağlayıcı={provider} başlatıldı.", flush=True)
        _analyzer_current_coin(pair)
    finally:
        with _MANUAL_ANALYZER_INFLIGHT_LOCK:
            _MANUAL_ANALYZER_INFLIGHT.discard(pair)


def _manual_analyzer_poll_loop():
    if not MANUAL_ANALYZER_ENABLED:
        print("[MANUEL ANALYZER] Devre dışı.", flush=True)
        return
    provider = _manual_analyzer_v2_model if _manual_analyzer_mode == "v2" else "Haiku 4.5"
    key_ready = bool(_manual_gemini_key) if _manual_analyzer_mode == "v2" else bool(_manual_anthropic_key)
    print(
        f"[MANUEL ANALYZER CONFIG] mod={_manual_analyzer_mode} | sağlayıcı={provider} | "
        f"API anahtarı={'hazır' if key_ready else 'eksik'} | GPT route=GPT Sonnet Analyzer",
        flush=True,
    )
    if not ANALYZER_TELEGRAM_TOKEN or not ANALYZER_CHAT_ID:
        print("[MANUEL ANALYZER] Token veya chat id eksik; dinleyici başlamadı.", flush=True)
        return

    url = f"https://api.telegram.org/bot{ANALYZER_TELEGRAM_TOKEN}/getUpdates"
    offset = None
    try:
        first = requests.get(url, params={"timeout": 0, "limit": 100}, timeout=10).json()
        updates = first.get("result", []) if first.get("ok") else []
        if updates:
            offset = max(int(u["update_id"]) for u in updates) + 1
    except Exception as e:
        print(f"[MANUEL ANALYZER] Başlangıç offset hatası: {e}", flush=True)

    print(
        f"[MANUEL ANALYZER] Thread {ANALYZER_THREAD_ID} dinleniyor | normal=mevcut Anton | `COIN GPT`=GPT Sonnet Analyzer.",
        flush=True,
    )
    while True:
        try:
            params = {"timeout": 25, "limit": 50, "allowed_updates": json.dumps(["message"])}
            if offset is not None:
                params["offset"] = offset
            r = requests.get(url, params=params, timeout=35)
            data = r.json()
            if not data.get("ok"):
                print(f"[MANUEL ANALYZER] getUpdates HTTP {r.status_code}: {r.text[:120]}", flush=True)
                time.sleep(5)
                continue
            for update in data.get("result", []):
                offset = int(update["update_id"]) + 1
                msg = update.get("message") or {}
                chat_id = str((msg.get("chat") or {}).get("id", ""))
                thread_id = msg.get("message_thread_id")
                sender_id = str((msg.get("from") or {}).get("id", ""))
                if chat_id != str(ANALYZER_CHAT_ID) or thread_id != ANALYZER_THREAD_ID:
                    continue
                if (msg.get("from") or {}).get("is_bot"):
                    continue
                if ANALYZER_ALLOWED_USER_ID and sender_id != str(ANALYZER_ALLOWED_USER_ID):
                    print(f"[MANUEL ANALYZER] Yetkisiz kullanıcı yok sayıldı: {sender_id}", flush=True)
                    continue

                text = msg.get("text", "")
                gpt_pair = _parse_gpt_analyzer_symbol(text)
                if gpt_pair:
                    threading.Thread(
                        target=_run_gpt_analyzer,
                        args=(gpt_pair, ANALYZER_TELEGRAM_TOKEN, chat_id, thread_id),
                        daemon=True,
                        name=f"gpt-sonnet-{gpt_pair}",
                    ).start()
                    continue

                pair = _parse_manual_analyzer_symbol(text)
                if not pair:
                    continue
                threading.Thread(
                    target=_run_manual_analyzer, args=(pair,), daemon=True,
                    name=f"manual-analyzer-{pair}",
                ).start()
        except Exception as e:
            print(f"[MANUEL ANALYZER] Dinleme hatası: {e}", flush=True)
            time.sleep(5)

# NOTE: The remainder of portfolio_tracker.py is intentionally unchanged in behavior.
# It is imported from the preserved implementation module below to avoid duplicating
# unrelated application code in this integration-only change.
from portfolio_tracker_runtime import *
