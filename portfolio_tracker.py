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
import time
import threading
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

TR_TZ = timezone(timedelta(hours=3))
DATA_DIR = os.getenv("DATA_DIR", "/tmp")
SIGNALS_FILE = os.path.join(DATA_DIR, "portfolio_signals.json")
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
    # FLASK_SECRET_KEY Render'da tanımlanmadıysa oturumlar imzalanamaz — süreç
    # başına rastgele bir key üretilir, bu da her deploy/restart'ta TÜM
    # oturumların düşmesi demektir ("beni hatırla" işe yaramaz). Site yine de
    # çöküp kapanmasın diye açık bırakılıyor ama FLASK_SECRET_KEY MUTLAKA
    # Render'a env var olarak eklenmeli (örn. `openssl rand -hex 32`).
    import secrets as _secrets
    app.secret_key = _secrets.token_hex(32)
    print("[UYARI] FLASK_SECRET_KEY tanımlı değil — oturumlar her restart'ta düşecek. "
          "Render'a FLASK_SECRET_KEY env var'ı ekleyin.", flush=True)
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = True  # Render hep HTTPS — cookie sadece HTTPS'te gönderilsin

import logging
logging.getLogger("werkzeug").setLevel(logging.ERROR)


def _safe_next(path):
    """Open-redirect koruması: sadece site-içi, tek-slash'lı yollara izin ver."""
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
    # next_url _safe_next() ile sadece "/" ile başlayan bir yol olduğu doğrulanmış
    # olsa da tırnak/HTML karakteri taşıyabilir (örn. /login?next=/"><script>...) —
    # hidden input'un value="..." attribute'ından kaçıp XSS'e yol açmasın diye
    # HTML'e basılmadan önce escape ediliyor (Codex incelemesiyle bulundu).
    return LOGIN_PAGE_HTML.format(next_url=html.escape(next_url, quote=True), error_html=error_html)


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


# ─── ERİŞİM KONTROLÜ ─────────────────────────────────────────────────────────
# Dashboard (bakiye, açık pozisyon, stop/TP, state silme) hiçbir korumaya sahip
# değildi — URL'yi bilen herkes görüntüleyip mutasyon endpoint'lerini
# tetikleyebiliyordu. Bot/servis çağrıları (SMC.py, position_monitor.py,
# claude_analyzer.py, trading-bot) zaten Authorization: Bearer PORTFOLIO_AUTH_TOKEN
# gönderiyor — bunlar etkilenmesin diye geçerli Bearer token her zaman geçer.
# Geri kalan HER ŞEY (tarayıcı/dashboard erişimi) imzalı oturum cookie'si
# gerektiriyor (/login — "30 gün beni hatırla" seçeneğiyle). Basic Auth'un
# yerini aldı: tarayıcı artık sekme/pencere kapanınca şifre sormuyor.
@app.before_request
def _require_auth():
    if request.path in ("/api/health", "/login", "/logout"):
        return None
    if AUTH_TOKEN:
        bearer = request.headers.get("Authorization", "").replace("Bearer ", "")
        if bearer == AUTH_TOKEN:
            return None
    if not DASHBOARD_USER or not DASHBOARD_PASS:
        # DASHBOARD_USER/DASHBOARD_PASS henüz Render'da tanımlanmadıysa dashboard
        # korumasız kalır — mevcut AUTH_TOKEN kontrollerindeki "boşsa açık" deseniyle
        # tutarlı, ama bu env var'lar deploy sonrası MUTLAKA ayarlanmalı.
        return None
    if session.get("authenticated"):
        return None
    return redirect(f"/login?next={_safe_next(request.path)}")

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

def _sync_from_trading_bot():
    """Startup'ta VE periyodik olarak (position_checker_loop, 15dk'da bir) çalışır.
    Trading bot'taki open pozisyonları portfolio sinyalleriyle eşleştir.
    pending_retest → open geçişi kaybolmuşsa burada düzeltilir.
    Portfolio'da open olan ama bot'ta olmayan pozisyonlar (BOT_MISS_THRESHOLD kez üst üste
    doğrulanınca) kapatılır — /api/position-closed webhook'u kaçırılırsa bu telafi eder."""
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

    # Bot'taki tüm semboller — slash'sız normalize ("BLUR/USDT" → "BLURUSDT")
    bot_all_symbols = {sym.replace("/", "").upper() for sym in trade_positions.keys()}
    bot_open_symbols = {
        sym.replace("/", "").upper() for sym, pos in trade_positions.items()
        if pos.get("status") == "open"
    }

    stale = 0
    with _lock:
        # 1) Portfolio'da open olan ama bot'ta olmayan → kapat (deploy sırasında kapandı)
        #    Tek snapshot'a güvenilmez (bot restart sırasında /status geçici eksik dönebilir) —
        #    art arda BOT_MISS_THRESHOLD periyodik kontrolde de yoksa kapatılır.
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

        # 2) Portfolio'da pending_retest olan ama bot'ta yok → sadece 48H dolmuşsa kapat
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
                    continue  # 48 saat dolmamış, dokunma
            except Exception:
                continue
            sig["status"]       = "no_retest"
            sig["close_time"]   = now_str
            sig["close_reason"] = "stale_pending"
            stale += 1
            print(f"[SYNC] {sym_norm} 48H doldu stale pending → no_retest", flush=True)

        # 3) Bot'taki tüm pozisyonları portfolioya yansıt
        #    monitoring/pending → pending_retest | open → open
        for sym, pos in trade_positions.items():
            bot_status = pos.get("status")
            if bot_status not in ("monitoring", "pending", "open"):
                continue
            sym_norm  = sym.replace("/", "").upper()   # her zaman slash'sız
            sym_slash = sym_norm[:-4] + "/USDT" if sym_norm.endswith("USDT") else sym_norm
            entry     = float(pos.get("entry") or pos.get("limit_price") or 0)
            stop      = float(pos.get("stop") or 0)
            tp1       = float(pos.get("tp1") or 0)
            limit_p   = float(pos.get("limit_price") or entry)
            qty       = float(pos.get("qty") or 0)
            port_status = "open" if bot_status == "open" else "pending_retest"
            use_entry   = entry if bot_status == "open" else limit_p

            # Portfolioda zaten aktif kayıt var mı?
            already = any(
                s.get("symbol", "").replace("/", "").upper() == sym_norm
                and s.get("status") in ("open", "pending_retest")
                for s in signals_db
            )
            if already:
                # pending_retest → open geçişi
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

            # Yeni sinyal oluştur — tüm alanlar bot state'inden
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
    """Eski DB kayıtlarındaki bilinen hataları düzelt."""
    fixed = 0

    for sig in signals_db:
        # ROCKET yeniden adlandırma: eski "momentum_devam" → "rocket"
        if sig.get("sig_type") == "momentum_devam":
            sig["sig_type"] = "rocket"
            fixed += 1
        # Eski "smc" source → "smc-v2" (aktif kayıtlar için)
        if sig.get("source") == "smc" and sig.get("status") in ("open", "pending_retest"):
            sig["source"] = "smc-v2"
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
    """Analyzer botuyla yalnız manuel analiz thread'ine kısa sistem mesajı gönder."""
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
    """ZEC, #ZEC, ZECUSDT, ZEC/USDT ve `/analiz ZEC` biçimlerini kabul et."""
    value = (text or "").strip().upper()
    value = re.sub(r"^/(?:ANALIZ|ANALYZE)(?:@[A-Z0-9_]+)?\s+", "", value)
    value = value.lstrip("#").replace("/", "").replace("-", "").replace("_", "")
    if value.endswith("USDT"):
        value = value[:-4]
    if not re.fullmatch(r"[A-Z0-9]{2,15}", value):
        return None
    return value + "USDT"


def _latest_spot_scanner_signal(pair: str):
    """Manuel yorumda yalnız gerçek Portfolio kaydını kullan; seviye uydurma."""
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
    """Thread 38'i long-poll ile dinler; eski mesajları başlangıçta tüketir."""
    if not MANUAL_ANALYZER_ENABLED:
        print("[MANUEL ANALYZER] Devre dışı.", flush=True)
        return
    provider = _manual_analyzer_v2_model if _manual_analyzer_mode == "v2" else "Haiku 4.5"
    key_ready = bool(_manual_gemini_key) if _manual_analyzer_mode == "v2" else bool(_manual_anthropic_key)
    print(
        f"[MANUEL ANALYZER CONFIG] mod={_manual_analyzer_mode} | sağlayıcı={provider} | "
        f"API anahtarı={'hazır' if key_ready else 'eksik'}",
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

    print(f"[MANUEL ANALYZER] Thread {ANALYZER_THREAD_ID} dinleniyor; otomatik analiz kapalı.", flush=True)
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
                pair = _parse_manual_analyzer_symbol(msg.get("text", ""))
                if not pair:
                    continue
                threading.Thread(
                    target=_run_manual_analyzer, args=(pair,), daemon=True,
                    name=f"manual-analyzer-{pair}",
                ).start()
        except Exception as e:
            print(f"[MANUEL ANALYZER] Dinleme hatası: {e}", flush=True)
            time.sleep(5)

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

    # ── AYNI SEMBOLDE POZİSYON KONTROLÜ ──
    incoming_source = data.get("source", "bot")
    with _lock:
        for s in signals_db:
            if s.get("symbol") != data["symbol"] or s.get("source") != incoming_source:
                continue
            if s.get("status") == "open":
                # Spot Scanner kayıtları gerçek pozisyon değil, birbirinden
                # bağımsız sanal fırsat örnekleridir. Aynı coin daha önceki
                # kaydın trailing takibindeyken yeni ve farklı bir kurulum
                # üretebilir; bunu reddetmek performans örneklemini eksiltir.
                if incoming_source == "spot-scanner":
                    continue
                print(f"[SİNYAL] REDDEDILDI: {data['symbol']} zaten açık pozisyonda", flush=True)
                return jsonify({"error": "already open", "symbol": data["symbol"]}), 409
            if s.get("status") == "pending_retest":
                # Yeni CHoCH → eski pending'i güncelle (yeni seviyeleri al, bota ilet)
                now_u = tr_now()
                s["entry"]        = float(data["entry"])
                s["stop"]         = float(data["stop"])
                s["tp1"]          = float(data["tp1"])
                s["tp2"]          = float(data.get("tp2") or 0) or None
                s["limit_price"]  = float(data.get("limit_price") or 0) or None
                s["signal_price"] = float(data.get("signal_price") or 0) or None
                s["open_time"]    = now_u.isoformat()
                s["peak_price"]   = float(data["entry"])
                s["low_price"]    = float(data["entry"])
                s["current_price"]= float(data["entry"])
                s["peak_pct"] = s["low_pct"] = s["current_pct"] = 0.0
                s["analyzer_decision"] = None
                s["analyzer_time"]     = None
                save_signals()
                print(f"[SİNYAL] GÜNCELLENDI (yeni CHoCH): {data['symbol']}", flush=True)
                if incoming_source in SMC_MAIN_SOURCES and TRADING_BOT_URL:
                    threading.Thread(target=_forward_to_trading_bot, args=(dict(s),), daemon=True).start()
                return jsonify({"ok": True, "id": s["id"]}), 200

    now = tr_now()
    signal = {
        "id": f"{data['symbol'].replace('/', '_')}_{int(now.timestamp())}",
        "symbol": data["symbol"],
        "entry": float(data["entry"]),
        "stop": float(data["stop"]),
        "tp1": float(data["tp1"]),
        "tp2": float(data.get("tp2") or 0) or None,
        "tp3": float(data.get("tp3") or 0) or None,
        "limit_price": float(data.get("limit_price") or 0) or None,
        "signal_price": float(data.get("signal_price") or 0) or None,
        "sig_type": data.get("sig_type", data.get("type", "unknown")),
        "sub_type": data.get("sub_type", data.get("subtype", data.get("tp_system", ""))),
        "source": data.get("source", "bot"),
        "phase": data.get("phase", ""),
        "candle": data.get("candle", ""),
        "funding_neg": data.get("funding_neg", False),
        "status": "pending_retest" if data.get("source") in FULL_TRAIL_SOURCES else "open",
        "open_time": now.isoformat(),
        "close_time": None, "close_price": None, "close_reason": None, "close_pct": None,
        "peak_price": float(data["entry"]), "peak_pct": 0.0,
        "low_price": float(data["entry"]), "low_pct": 0.0,
        "current_price": float(data["entry"]), "current_pct": 0.0,
        "tp1_hit": False, "tp1_time": None,
        # Spot Scanner kayıtlarının Portfolio içinde sanal olarak
        # TP1/stop/expire ile sonuçlandırıldığını gösterir. Eski kayıtlarda bu
        # alan yoktur; ilk toplu temizlikte Telegram bildirimi yağmaması için
        # yeni ve eski kayıtları ayırmakta da kullanılır.
        "spot_tracking_v1": data.get("source") == "spot-scanner",
        # analyzer
        "analyzer_decision": None, "analyzer_time": None,
        "last_check": now.isoformat(), "checks": 0,
        "extra": {k: v for k, v in data.items() if k not in required + [
            "sig_type", "type", "sub_type", "subtype", "tp_system",
            "source", "phase", "candle", "funding_neg", "tp2", "limit_price", "signal_price"
        ]},
    }

    with _lock:
        signals_db.insert(0, signal)
        save_signals()

    # SMC sinyallerini trading bot'a ilet (limit emir açılsın)
    if signal["source"] in SMC_MAIN_SOURCES and TRADING_BOT_URL:
        threading.Thread(target=_forward_to_trading_bot, args=(signal,), daemon=True).start()

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


def compute_atr(symbol, period=ATR_PERIOD):
    """Son 1H mumları çekip Wilder ATR(period) hesaplar. Hata/yetersiz veri → None."""
    pair = symbol.replace("/", "").replace("USDT", "USDT")
    try:
        r = requests.get(BINANCE_KLINE_URL, params={
            "symbol": pair, "interval": "1h", "limit": period * 5
        }, timeout=10)
        if r.status_code != 200:
            return None
        klines = r.json()
    except Exception as e:
        print(f"[BINANCE] ATR {symbol} hata: {e}", flush=True)
        return None
    if len(klines) < period + 1:
        return None
    highs  = [float(k[2]) for k in klines]
    lows   = [float(k[3]) for k in klines]
    closes = [float(k[4]) for k in klines]
    trs = []
    for i in range(1, len(klines)):
        trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1])))
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


def trail_stop_price(peak, atr):
    """peak - ATR_MULT*ATR; ATR yoksa/geçersizse sabit %1.84 yedeğe düşer."""
    if atr and atr > 0:
        return peak - ATR_MULT * atr
    return peak * (1 - FALLBACK_TRAIL_PCT / 100)


def spot_opportunity_trail_stop(entry, peak):
    """Spot Scanner: TP1 sonrası peak'ten %2.5 trailing, en düşük giriş fiyatı."""
    return max(entry, peak * (1 - SPOT_OPPORTUNITY_TRAIL_PCT / 100))


def _atr_is_stale(sig):
    ts = sig.get("atr_updated_at")
    if not ts:
        return True
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds()
        return age >= ATR_REFRESH_S
    except Exception:
        return True

# ============================================================
# POZİSYON KONTROL DÖNGÜSÜ
# ============================================================
def check_open_positions():
    now = tr_now()
    with _lock:
        active = [s for s in signals_db if s["status"] == "open"]
    if not active:
        return

    open_count = len(active)
    print(f"[CHECK] {open_count} açık pozisyon takip...", flush=True)

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

            # TP1/trailing DURUMU her zaman bağımsız tespit edilir (sadece takip/
            # görüntüleme amaçlı, KAPATMA kararı değil) — bot webhook'u (/api/tp1-hit)
            # gelmese bile portfolio Binance'te gerçekte ne olduğunu kendi başına bilsin.
            # Aşağıdaki "bot aktifken" bloğu sadece KAPATMA aksiyonunu erteliyor.
            if is_smc and sig.get("source") in FULL_TRAIL_SOURCES:
                if tp1 and high >= tp1 and not sig.get("tp1_hit"):
                    tp1_pct_v = round((tp1 - entry) / entry * 100, 2)
                    atr_val = compute_atr(symbol)
                    with _lock:
                        sig["tp1_hit"] = True; sig["tp1_time"] = now.isoformat()
                        sig["tp1_pct"] = tp1_pct_v
                        sig["atr"] = atr_val
                        sig["atr_updated_at"] = datetime.now(timezone.utc).isoformat()
                        # ÖNEMLİ: bu SADECE portfolio'nun fiyattan gördüğü, bot'un
                        # Binance'te GERÇEKTEN emri değiştirdiğinin kanıtı değil.
                        # Bot yoksa (TRADING_BOT_URL boş) portfolio zaten tek otorite —
                        # onaylı sayılır. Bot varsa /api/tp1-hit webhook'u gelene kadar
                        # onaysız — dashboard bunu asla "güvenli" gibi göstermemeli.
                        sig["tp1_confirmed"] = not bool(TRADING_BOT_URL)
                    need_save = True
                    print(f"  🟡 TP TESPİT EDİLDİ: {symbol.replace('/USDT','')} | +{tp1_pct_v:.2f}% (bağımsız gözlem, bot onayı {'gerekmiyor' if not TRADING_BOT_URL else 'bekleniyor'})", flush=True)
                elif sig.get("tp1_hit") and _atr_is_stale(sig):
                    fresh_atr = compute_atr(symbol)
                    if fresh_atr:
                        with _lock:
                            sig["atr"] = fresh_atr
                            sig["atr_updated_at"] = datetime.now(timezone.utc).isoformat()
                        need_save = True

            # Bot aktifken SMC kapanış KARARINI portfolio vermez — /api/position-closed
            # bekle. TP1 tespiti yukarıda zaten bağımsız yapıldığı için bot'un webhook'u
            # gelmese bile dashboard doğru gösterir; sadece kapatma aksiyonu erteleniyor.
            if TRADING_BOT_URL and sig.get("source") in SMC_MAIN_SOURCES:
                need_save = True
                time.sleep(0.15)
                continue

            close_reason = None; close_price = None; close_status = None

            if is_smc:
                # SMC CHoCH ROC: stop → loss | trail tetik → win_trail/loss
                if low <= stop:
                    close_reason = "stop"; close_price = stop; close_status = "loss"
                elif sig.get("tp1_hit") and sig.get("source") in FULL_TRAIL_SOURCES:
                    trail_stop = round(trail_stop_price(sig["peak_price"], sig.get("atr")), 8)
                    trail_ret  = round((trail_stop - entry) / entry * 100, 2)
                    if low <= trail_stop:
                        close_reason = "trailing"; close_price = trail_stop
                        close_status = "win_trail" if trail_ret > 0 else "loss"
            else:
                is_pump = sig.get("sig_type") == "pump"
                is_spot_opportunity = (
                    sig.get("source") == "spot-scanner"
                    or sig.get("sig_type") == "spot_opportunity"
                )
                if is_spot_opportunity:
                    # Spot Scanner Portfolio'da yalnızca SANAL işlem olarak
                    # izlenir; hiçbir Binance emri veya gerçek satış işlemi yoktur.
                    # TP1 öncesinde stop önceliği korunur. Kaydedilmiş peak/low
                    # da kullanılır; böylece eski adaylar ilk kontrolde doğru
                    # biçimde değerlendirilir.
                    observed_low = min(low, float(sig.get("low_price", low)))
                    observed_high = max(high, float(sig.get("peak_price", high)))
                    # TP1 bir kapanış değil, trailing'i etkinleştiren kilometre
                    # taşıdır. TP1 görülmeden önce yapısal stop ve 24 saatlik
                    # inceleme ufku geçerlidir. TP1 görüldükten sonra süre sınırı
                    # kalkar; peak'ten %2.5 gerileme takip edilir. Trailing tabanı
                    # giriş fiyatıdır, dolayısıyla hedefi görmüş bir kayıt sonradan
                    # zarara dönüştürülmez.
                    if not sig.get("tp1_hit") and observed_low <= stop:
                        close_reason = "stop"
                        close_price = stop
                        close_status = "loss"
                    elif not sig.get("tp1_hit") and tp1 and observed_high >= tp1:
                        sig["tp1_hit"] = True
                        sig["tp1_time"] = sig.get("tp1_time") or now.isoformat()
                        sig["tp1_pct"] = round((tp1 - entry) / entry * 100, 2)
                        need_save = True
                        print(f"  🟡 SPOT TP1 GÖRÜLDÜ: {symbol.replace('/USDT','')} | "
                              f"+{sig['tp1_pct']:.2f}% | %{SPOT_OPPORTUNITY_TRAIL_PCT:.1f} trailing başladı", flush=True)

                    if sig.get("tp1_hit"):
                        trail_stop = round(spot_opportunity_trail_stop(entry, sig["peak_price"]), 8)
                        trail_ret = round((trail_stop - entry) / entry * 100, 2)
                        # Açık 5 dakikalık mumda TP1 ve aşağı fitilin hangisinin
                        # önce oluştuğu bilinemez. Yanlış sıra varsaymamak için
                        # sanal trailing kapanış fiyatıyla kontrol edilir.
                        if close <= trail_stop:
                            close_reason = "trailing"
                            close_price = trail_stop
                            close_status = "win_trail"
                    elif not close_reason:
                        open_time = datetime.fromisoformat(sig["open_time"])
                        if open_time.tzinfo is None:
                            open_time = open_time.replace(tzinfo=TR_TZ)
                        if (now - open_time).total_seconds() / 3600 >= SPOT_OPPORTUNITY_EXPIRE_H:
                            close_reason = "expired"
                            close_price = close
                            close_status = "expired"
                elif is_pump:
                    # PUMP: hard SL, hard TP, 6h expire — trailing yok
                    if tp1 and high >= tp1 and not sig.get("tp1_hit"):
                        with _lock:
                            sig["tp1_hit"] = True; sig["tp1_time"] = now.isoformat()
                        need_save = True
                        print(f"  🎯 PUMP TP HİT: {symbol.replace('/USDT','')} | +{round((tp1-entry)/entry*100,1)}%", flush=True)
                    if tp2 and high >= tp2:
                        close_reason = "tp2"; close_price = tp2; close_status = "win_tp2"
                    elif low <= stop:
                        close_reason = "stop"; close_price = stop; close_status = "loss"
                    else:
                        open_time = datetime.fromisoformat(sig["open_time"])
                        if open_time.tzinfo is None: open_time = open_time.replace(tzinfo=TR_TZ)
                        if (now - open_time).total_seconds() / 3600 >= BOT_EXPIRE_H["pump"]:
                            close_reason = "expired"; close_price = close; close_status = "expired"

            if close_reason:
                with _lock:
                    sig["status"]      = close_status
                    sig["close_time"]  = now.isoformat()
                    sig["close_price"] = round(close_price, 8)
                    sig["close_reason"] = close_reason
                    sig["close_pct"]   = round((close_price - entry) / entry * 100, 2)
                closed_count += 1; need_save = True
                emoji = {"tp1": "🟢", "tp2": "🟢", "trailing": ("💰" if sig["close_pct"] > 0 else "🔴"),
                         "stop": "🔴", "expired": "⏰"}.get(close_reason, "⚪")
                print(f"  {emoji} KAPANDI: {symbol} | {close_reason.upper()} | "
                      f"{sig['close_pct']:+.2f}% | Peak: {sig['peak_pct']:+.2f}%", flush=True)
                # Bu sürümden önce biriken Spot Scanner adaylarını ilk kontrolde
                # sonuçlandırırken onlarca eski Telegram mesajı gönderme. Yeni
                # adaylar normal kapanış bildirimlerini almaya devam eder.
                legacy_spot_backfill = (
                    (sig.get("source") == "spot-scanner" or sig.get("sig_type") == "spot_opportunity")
                    and not sig.get("spot_tracking_v1")
                )
                if not legacy_spot_backfill:
                    _send_telegram_pt(
                        f"{emoji} <b>POZİSYON KAPANDI — {symbol}</b>\n"
                        f"Sebep: {close_reason.upper()} | P&L: {sig['close_pct']:+.2f}%\n"
                        f"Giriş: {entry:.6g} | Çıkış: ~{close_price:.6g} | Peak: {sig['peak_pct']:+.2f}%"
                    )
                try:
                    _update_archive_outcome(sig.get("id", ""), close_reason,
                                            sig["close_pct"], sig["peak_pct"], sig["open_time"])
                except Exception as _ae:
                    print(f"[ARCHIVE] {_ae}", flush=True)


        time.sleep(0.15)

    if need_save or closed_count > 0:
        with _lock:
            save_signals()
        if closed_count > 0:
            print(f"[CHECK] {closed_count} pozisyon kapandı.", flush=True)

def check_pending_retests():
    """Portfolio tarafında pending_retest sinyallerini fiyata göre günceller.
    Bot servisi suspend iken de çalışır — Binance emir durumu yerine fiyat kullanır."""
    now = tr_now()
    with _lock:
        pending   = [s for s in signals_db if s.get("status") == "pending_retest"]
        open_count = len([s for s in signals_db if s.get("status") == "open"])
    if not pending:
        return

    need_save = False
    for sig in pending:
        symbol  = sig["symbol"]
        lp      = sig.get("limit_price")
        open_time = sig.get("open_time")

        # 48H expire kontrolü — bot aktif olsa da çalışır (orphan sinyaller için)
        try:
            ot = datetime.fromisoformat(open_time)
            if ot.tzinfo is None: ot = ot.replace(tzinfo=TR_TZ)
            if (now - ot).total_seconds() / 3600 >= 48:
                with _lock:
                    sig["status"]       = "no_retest"
                    sig["close_time"]   = now.isoformat()
                    sig["close_reason"] = "no_retest"
                need_save = True
                print(f"[PENDING] 48H doldu, retest yok: {symbol}", flush=True)
                continue
        except Exception:
            pass

        # Fiyat bazlı fill: sadece bot yokken çalışır (bot varsa /api/retest-filled bekle)
        if TRADING_BOT_URL:
            continue

        if not lp:
            continue

        price_data = get_current_price_hl(symbol)
        if not price_data:
            continue

        low   = price_data["low"]
        close = price_data["close"]

        # Fiyat limit seviyesine indi → simülasyon fill
        if low <= float(lp):
            entry = float(lp)
            stop  = float(sig.get("stop", 0))
            tp1   = float(sig.get("tp1", 0))
            # Kontrol + güncelleme atomik: aynı lock içinde yap (race condition önleme)
            with _lock:
                open_count = len([s for s in signals_db if s.get("status") == "open"])
                if open_count >= MAX_POSITIONS:
                    print(f"[PENDING] {symbol} fill atlandı: {open_count}/{MAX_POSITIONS} pozisyon dolu", flush=True)
                    continue
                sig["status"]        = "open"
                sig["entry"]         = entry
                sig["open_time"]     = now.isoformat()
                sig["peak_price"]    = entry
                sig["peak_pct"]      = 0.0
                sig["low_price"]     = entry
                sig["low_pct"]       = 0.0
                sig["current_price"] = close
                sig["current_pct"]   = round((close - entry) / entry * 100, 2)
                sig["tp1_hit"]       = False
            need_save = True
            print(f"[PENDING] RETEST DOLDU (simülasyon): {symbol} @ {entry}", flush=True)
            _send_telegram_pt(
                f"✅ <b>RETEST DOLDU — {symbol}</b>\n"
                f"Limit seviyesi ({entry:.6g} $) test edildi.\n"
                f"Stop: {stop:.6g} | TP1: {tp1:.6g}"
            )

    if need_save:
        with _lock:
            save_signals()


def _sync_pending_to_bot():
    """Portfolio'daki pending_retest sinyallerini bot'a ilet (eksik olanlar için)."""
    if not TRADING_BOT_URL:
        return
    try:
        hdrs = {"Content-Type": "application/json"}
        if TRADING_BOT_TOKEN:
            hdrs["X-Bot-Token"] = TRADING_BOT_TOKEN
        r = requests.get(f"{TRADING_BOT_URL}/status", headers=hdrs, timeout=10)
        if r.status_code != 200:
            return
        bot_positions = r.json()  # /status doğrudan {symbol: pos} döndürür
        bot_symbols = {s.replace("/", "").upper() for s in bot_positions}
    except Exception:
        return

    with _lock:
        pending = [s for s in signals_db
                   if s.get("status") == "pending_retest"
                   and s.get("source") in SMC_MAIN_SOURCES]

    for sig in pending:
        sym_norm = sig.get("symbol", "").replace("/", "").upper()
        if sym_norm not in bot_symbols:
            print(f"[SYNC→BOT] {sym_norm} portfolio'da var, bot'ta yok → iletiliyor", flush=True)
            threading.Thread(target=_forward_to_trading_bot, args=(dict(sig),), daemon=True).start()


def _sync_open_from_bot():
    """Periyodik: Bot'ta open olan ama portfolio'da hâlâ pending_retest olan kayıtları open'a çevir.
    /api/retest-filled webhook başarısız olduğunda (bot yeniden başlatma vb.) drift'i giderir."""
    if not TRADING_BOT_URL:
        return
    try:
        hdrs = {"X-Bot-Token": TRADING_BOT_TOKEN} if TRADING_BOT_TOKEN else {}
        r = requests.get(f"{TRADING_BOT_URL}/status", headers=hdrs, timeout=10)
        if not r.ok:
            return
        trade_positions = r.json()
    except Exception as e:
        print(f"[SYNC] Bot erişim hatası: {e}", flush=True)
        return

    bot_open = {
        sym.replace("/", "").upper(): pos
        for sym, pos in trade_positions.items()
        if pos.get("status") == "open"
    }
    if not bot_open:
        return

    updated = 0
    now_str = tr_now().isoformat()
    with _lock:
        for sig in signals_db:
            if sig.get("status") != "pending_retest":
                continue
            if sig.get("source") not in SMC_MAIN_SOURCES:
                continue
            sym_norm = sig.get("symbol", "").replace("/", "").upper()
            if sym_norm not in bot_open:
                continue
            pos = bot_open[sym_norm]
            entry = float(pos.get("entry") or pos.get("limit_price") or sig.get("entry") or 0)
            qty   = float(pos.get("qty") or 0)
            sig["status"]        = "open"
            if entry:
                sig["entry"]     = entry
            if qty:
                sig["fill_qty"]  = qty
            sig["fill_time"]     = now_str
            sig["peak_price"]    = entry or sig.get("entry", 0)
            sig["low_price"]     = entry or sig.get("entry", 0)
            sig["current_price"] = entry or sig.get("entry", 0)
            updated += 1
            print(f"[SYNC] {sym_norm} pending_retest → open (periyodik sync)", flush=True)
        if updated:
            save_signals()
    if updated:
        print(f"[SYNC] Periyodik sync: {updated} pending_retest → open çevrildi", flush=True)

    # TP1/trailing onayı SADECE /api/tp1-hit webhook'una bağımlı kalmasın —
    # webhook kaybolursa (bugün defalarca oldu) dashboard sonsuza kadar
    # "onay bekleniyor" yazabilir, oysa bot'un kendi /status'u zaten gerçeği
    # söylüyor. Burada da (webhook'tan bağımsız) onaylanır — AMA sadece
    # trailing=true YETMEZ: bot'ta bu alan TP1 fiyat şartı sağlanır
    # sağlanmaz set ediliyor, gerçek Binance emri o an henüz konmamış
    # olabilir (nadir ama mümkün). trailing_sl_id'nin de dolu olması
    # şart — o alan SADECE emir Binance'e başarıyla gönderilip bir
    # order ID döndüğünde yazılıyor. Yani "gerçekten trailing" = ikisi
    # birden, tek başına trailing=true "kafasına göre onaylı" demek
    # değil.
    confirmed = 0
    with _lock:
        for sig in signals_db:
            if sig.get("status") != "open" or sig.get("tp1_confirmed"):
                continue
            if sig.get("source") not in FULL_TRAIL_SOURCES:
                continue
            sym_norm = sig.get("symbol", "").replace("/", "").upper()
            pos = bot_open.get(sym_norm)
            if not pos or not pos.get("trailing") or not pos.get("trailing_sl_id"):
                continue
            sig["tp1_hit"] = True
            sig["tp1_confirmed"] = True
            if not sig.get("tp1_time"):
                sig["tp1_time"] = tr_now().isoformat()
            peak = float(pos.get("peak") or 0)
            if peak and peak > sig.get("peak_price", 0):
                sig["peak_price"] = peak
            atr = pos.get("atr")
            if atr:
                sig["atr"] = float(atr)
                sig["atr_updated_at"] = datetime.now(timezone.utc).isoformat()
            confirmed += 1
            print(f"[SYNC] {sym_norm} trailing onaylandı (periyodik /status kontrolü, webhook'tan bağımsız)", flush=True)
        if confirmed:
            save_signals()


def _keepalive_bot():
    if not TRADING_BOT_URL:
        return
    try:
        hdrs = {"X-Bot-Token": TRADING_BOT_TOKEN} if TRADING_BOT_TOKEN else {}
        requests.get(f"{TRADING_BOT_URL}/health", headers=hdrs, timeout=10)
    except Exception:
        pass


def position_checker_loop():
    while True:
        try:
            _keepalive_bot()
            check_pending_retests()
            check_open_positions()
            _sync_pending_to_bot()
            _sync_open_from_bot()
            _sync_from_trading_bot()
        except Exception as e:
            print(f"[CHECK] Döngü hatası: {e}", flush=True)
        time.sleep(CHECK_INTERVAL)

# ============================================================
# PERFORMANS HESAPLAMA
# ============================================================
def classify_signal_outcome(sig):
    """Bir kapanmış sinyalin gerçek sonucunu (kind: 'win'/'loss'/'manual'/None) ve
    süre dolarak (TP1'e ulaşmadan) kapanıp kapanmadığını (is_expired) belirler.
    calc_performance() VE status_badge() burayı kullanır — iki fonksiyonun farklı
    mantıklarla birbirinden sapmasını önlemek için tek yerden yönetiliyor.

    Eski status vocabulary (win_tp1/win_trail/loss/expired — artık hiçbir kod
    yolu üretmiyor, sadece geriye dönük uyumluluk) VE trading bot'un güncel
    /api/position-closed şeması (status="closed" + outcome/close_reason/
    close_pct — bkz. claude_analyzer.py _is_win(), aynı desen) birlikte
    destekleniyor. is_expired kind'dan BAĞIMSIZ — bir işlem hem "win" hem
    "expired sebebiyle kapandı" olabilir (kazançla da kapansa süre dolarak
    kapanmışsa öyle işaretlenir)."""
    status = sig.get("status", "")
    pct = sig.get("close_pct", 0) or 0
    is_expired = False
    if status in ("win_tp1", "win_tp2", "win_trail"):
        kind = "win"
    elif status == "loss":
        kind = "loss"
    elif status == "expired":
        is_expired = True
        kind = "win" if pct > 0 else "loss"
    elif status == "closed":
        close_reason = sig.get("close_reason", "")
        outcome = sig.get("outcome")
        is_expired = close_reason in ("expire", "expire_no_tick")
        if outcome == "manual":
            kind = "manual"  # admin kapatması, gerçek trade sonucu değil
        elif (outcome == "win") if outcome else (pct > 0):
            kind = "win"
        else:
            kind = "loss"
    else:
        kind = None
    return kind, is_expired


def calc_performance():
    with _lock:
        all_sigs = list(signals_db)
    all_sigs = [s for s in all_sigs if s.get("sig_type", "unknown") not in HIDDEN_SIG_TYPES
                and s.get("source", "bot") not in HIDDEN_SOURCES]

    result = {
        "total": len(all_sigs),
        "open": 0, "closed": 0,
        "wins": 0, "win_tp2": 0, "losses": 0, "expired": 0, "tp1_hits": 0,
        "win_pnl": 0.0, "loss_pnl": 0.0, "expired_win": 0, "expired_loss": 0,
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
        # pending_retest: henüz fill olmadı, istatistiğe dahil etme
        # no_retest: fill olmadan iptal, istatistiğe dahil etme
        if status in ("pending_retest", "no_retest"):
            continue
        kind, is_expired = None, False  # açık pozisyonlarda hep None/False kalır;
                                         # bir önceki sinyalden kalma değer sızmasın diye burada resetleniyor
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
            type_key = "Legacy SMC"
        elif source in ("smc", "smc-original"):
            phase = sig.get('phase', '')
            phase_label = "Discount" if phase == "discount" else ("CHoCH" if phase == "choch" else phase.replace('phase', 'P'))
            type_key = f"SMC {phase_label}"
        elif sig_type == "tp":
            type_key = f"TP-{sub.capitalize()}" if sub else "TP"
        elif source == "spot-scanner" or sig_type == "spot_opportunity":
            # Spot Scanner kurulumlarını tek başlıkta eritme; hangi fiyat
            # davranışının gerçekten daha başarılı olduğu ayrı görülebilsin.
            _spot_sub = str(sub or "").casefold().replace("\u0307", "").replace("ı", "i")
            if "erken_dönüş" in _spot_sub:
                type_key = "ERKEN DÖNÜŞ İZLEME"
            elif "sikişma" in _spot_sub:
                type_key = "SIKIŞMA SONRASI DEVAM"
            elif "destek_tepki" in _spot_sub:
                type_key = "DESTEK TEPKİSİ"
            else:
                type_key = "DİĞER"
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
            kind, is_expired = classify_signal_outcome(sig)
            if status == "win_tp2": result["win_tp2"] += 1

            if kind == "win":
                result["wins"] += 1; ts["wins"] += 1; result["win_pnl"] += pct
            elif kind == "loss":
                result["losses"] += 1; ts["losses"] += 1; result["loss_pnl"] += pct
            if is_expired:
                result["expired"] += 1; result["expired_pnl"] += pct
                ts["expired"] += 1; ts["expired_pnl_sum"] += pct
                if kind == "win": result["expired_win"] += 1
                elif kind == "loss": result["expired_loss"] += 1

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
                    if kind == "win": ab["wins"] += 1
                    elif kind == "loss": ab["losses"] += 1

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
                    if kind == "win":
                        _tb[_tk]["wins"] += 1
                    elif kind == "loss":
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
    result["win_pnl"]       = round(result["win_pnl"], 2)
    result["loss_pnl"]      = round(result["loss_pnl"], 2)
    for bk, bv in result["analyzer"].items():
        dec = bv["wins"] + bv["losses"]
        bv["wr"]  = round(bv["wins"] / dec * 100, 1) if dec > 0 else 0
        bv["pnl"] = round(bv["pnl"], 2)
    for tk, ts in type_stats.items():
        # NOT: "expired" artık win/loss'tan bağımsız bir bilgi etiketi (bkz. yukarıdaki
        # is_expired) — paydaya eklenmez, aksi halde expired+win olan trade'ler çift sayılır.
        decided = ts["wins"] + ts["losses"]
        ts["win_rate"] = round(ts["wins"] / decided * 100, 1) if decided > 0 else 0
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
def _forward_to_trading_bot(signal: dict):
    if not TRADING_BOT_URL:
        return
    sym = signal.get("symbol", "")
    hdrs = {"Content-Type": "application/json"}
    if TRADING_BOT_TOKEN:
        hdrs["X-Bot-Token"] = TRADING_BOT_TOKEN
    attempt = 0
    while True:
        attempt += 1
        try:
            r = requests.post(
                f"{TRADING_BOT_URL}/signal",
                json=signal,
                headers=hdrs,
                timeout=30,  # bot uyanma süresi için yüksek
            )
            print(f"[TRADE] Sinyal iletildi: {sym} → HTTP {r.status_code} (deneme {attempt})", flush=True)
            return
        except Exception as e:
            print(f"[TRADE] İletim hatası (deneme {attempt}): {sym} — {e}", flush=True)
            if attempt >= 5:
                print(f"[TRADE] {sym} 5 denemede iletilemedi, vazgeçildi.", flush=True)
                return
            time.sleep(30)


@app.route("/api/analyze", methods=["POST"])
def api_analyze():
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    if AUTH_TOKEN and token != AUTH_TOKEN:
        return jsonify({"error": "unauthorized"}), 401
    if not AUTO_ANALYZER_ENABLED:
        print("[ANALYZER] Otomatik istek atlandı; thread 38 manuel modda.", flush=True)
        return jsonify({"status": "disabled", "mode": "manual_thread_38"}), 202
    data = request.get_json(silent=True) or {}
    signal       = data.get("signal", {})
    recent_count = data.get("recent_count", 0)
    sig_num      = data.get("sig_num", 0)
    portfolio_id = data.get("portfolio_id") or ""
    threading.Thread(
        target=_analyzer_process,
        args=(signal, recent_count, sig_num, portfolio_id),
        daemon=True,
    ).start()
    # NOT (2026-07-25): Buradan trading-bot'a AYRICA _forward_to_trading_bot
    # çağrılıyordu — ama SMC.py aynı sinyal için hem /api/signal (source
    # "smc-v2") hem /api/analyze (source "smc") POST ediyor, ve /api/signal
    # handler'ı zaten SMC_MAIN_SOURCES için forward'ı yapıyor. Sonuç: her
    # sinyal trading-bot'a iki kez iletiliyordu (log'da aynı sembol için
    # "Sinyal iletildi" iki kez görülüyordu). trading_engine.execute()
    # zaten-aktif kontrolü sayesinde gerçek çift pozisyon oluşmuyordu ama
    # gereksiz çift istek/log kirliliği vardı — kaldırıldı. /api/analyze'ın
    # TEK görevi artık Claude analyzer'ı tetiklemek.
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

@app.route("/api/signal/<signal_id>/close", methods=["POST"])
def close_signal(signal_id):
    """Dashboard'daki 'manuel kapat' butonu buraya fetch atıyor. Site geneli
    Basic Auth (before_request) zaten koruyor, ayrıca token kontrolüne gerek
    yok — bkz. clear_signal_history(). Önceden route-özel bir Bearer kontrolü
    vardı, bu da JS'in gerçek PORTFOLIO_AUTH_TOKEN'ı HTML kaynağına gömmesini
    zorunlu kılıyordu (Codex incelemesiyle bulundu, güvenlik açığı — Basic
    Auth arkasındaki herkes token'ı sayfa kaynağından okuyup bot/webhook
    endpoint'lerini de bypass edebilirdi)."""
    now_str = datetime.now(TR_TZ).isoformat()
    with _lock:
        for sig in signals_db:
            if sig.get("id") == signal_id and sig.get("status") == "open":
                sig["status"]       = "closed"
                sig["outcome"]      = "manual"
                sig["close_reason"] = "manual"
                sig["close_time"]   = now_str
                save_signals()
                return jsonify({"ok": True, "closed": signal_id})
    return jsonify({"error": "not found or not open"}), 404

@app.route("/api/signals/close-spot-scanner", methods=["POST"])
def close_spot_scanner_signals():
    """UI'dan yalnızca Spot Scanner kaynaklı açık izleme kayıtlarını kapatır.

    Legacy SMC ve diğer kaynaklardaki açık kayıtlara dokunmaz. Öğrenme arşivi
    ayrı tutulduğu için bu işlem learning_archive.json dosyasını değiştirmez.
    """
    now_str = datetime.now(TR_TZ).isoformat()
    closed = 0
    with _lock:
        for sig in signals_db:
            if sig.get("status") != "open" or sig.get("source") != "spot-scanner":
                continue
            current = float(sig.get("current_price") or sig.get("entry") or 0)
            entry = float(sig.get("entry") or 0)
            sig["status"] = "closed"
            sig["outcome"] = "manual"
            sig["close_reason"] = "bulk_spot_scanner_cleanup"
            sig["close_time"] = now_str
            sig["close_price"] = current
            sig["close_pct"] = round((current - entry) / entry * 100, 2) if entry else 0.0
            closed += 1
        if closed:
            save_signals()
    print(f"[TEMİZLE] Spot Scanner açık kayıtları topluca kapatıldı: {closed}", flush=True)
    return jsonify({"ok": True, "closed": closed})

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

@app.route("/api/signals/clear-history", methods=["POST"])
def clear_signal_history():
    """UI geçmişini ve retest bekleyen kayıtları temizler; yalnızca gerçekten
    açık ('open') kayıtları korur. Bu işlem signals_db/portfolio_signals.json
    üzerinde çalışır; learning_archive.json öğrenme arşivine dokunmaz.
    Site geneli Basic Auth (before_request) zaten koruyor, ayrıca token
    kontrolüne gerek yok."""
    with _lock:
        before = len(signals_db)
        signals_db[:] = [s for s in signals_db if s.get("status") == "open"]
        removed = before - len(signals_db)
        kept = len(signals_db)
        if removed:
            save_signals()
    print(f"[TEMİZLE] Geçmiş ve pending temizlendi: {removed} kayıt silindi, {kept} açık kayıt korundu", flush=True)
    return jsonify({"ok": True, "removed": removed, "kept": kept})

@app.route("/api/signals/delete-by-id", methods=["POST"])
def delete_signal_by_id():
    if AUTH_TOKEN:
        token = request.headers.get("Authorization", "").replace("Bearer ", "")
        if token != AUTH_TOKEN:
            return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    sig_id = data.get("id", "")
    if not sig_id:
        return jsonify({"error": "id required"}), 400
    with _lock:
        before = len(signals_db)
        signals_db[:] = [s for s in signals_db if s.get("id") != sig_id]
        removed = before - len(signals_db)
        if removed:
            save_signals()
    return jsonify({"ok": True, "removed": removed, "id": sig_id})

@app.route("/api/signals/sync-cleanup", methods=["POST"])
def sync_cleanup():
    """Elle tetikle: bot'ta olmayan open → kapat, bot'ta olmayan pending → no_retest, manual → sil."""
    if AUTH_TOKEN:
        token = request.headers.get("Authorization", "").replace("Bearer ", "")
        if token != AUTH_TOKEN:
            return jsonify({"error": "unauthorized"}), 401
    if not TRADING_BOT_URL:
        return jsonify({"error": "TRADING_BOT_URL tanımlı değil"}), 503
    try:
        hdrs = {"X-Bot-Token": TRADING_BOT_TOKEN} if TRADING_BOT_TOKEN else {}
        r = requests.get(f"{TRADING_BOT_URL}/status", headers=hdrs, timeout=10)
        if not r.ok:
            return jsonify({"error": "trading bot erişilemez"}), 502
        trade_positions = r.json()
    except Exception as e:
        return jsonify({"error": str(e)}), 502

    now_str = tr_now().isoformat()
    bot_all_symbols  = {sym.upper() for sym in trade_positions.keys()}
    bot_open_symbols = {sym.upper() for sym, pos in trade_positions.items() if pos.get("status") == "open"}

    closed = stale = manual = 0
    with _lock:
        for sig in signals_db:
            src      = sig.get("source", "")
            sym_norm = sig.get("symbol", "").replace("/", "").upper()
            status   = sig.get("status", "")
            if src not in SMC_MAIN_SOURCES:
                continue
            if status == "open" and sym_norm not in bot_open_symbols:
                sig["status"] = "closed"; sig["close_time"] = now_str
                sig["close_reason"] = "sync_closed"
                sig["close_pct"] = round(
                    (sig.get("current_price", sig["entry"]) - sig["entry"]) / sig["entry"] * 100, 2
                ) if sig.get("entry") else 0
                closed += 1
            elif status == "pending_retest" and sym_norm not in bot_all_symbols:
                sig["status"] = "no_retest"; sig["close_time"] = now_str
                sig["close_reason"] = "stale_pending"
                stale += 1
            elif status == "closed" and sig.get("close_reason") == "manual":
                sig["_delete"] = True
                manual += 1
        signals_db[:] = [s for s in signals_db if not s.get("_delete")]
        save_signals()
    return jsonify({"ok": True, "closed": closed, "stale_pending": stale, "manual_removed": manual})

@app.route("/api/signals/delete-manual", methods=["POST"])
def delete_manual_signals():
    if AUTH_TOKEN:
        token = request.headers.get("Authorization", "").replace("Bearer ", "")
        if token != AUTH_TOKEN:
            return jsonify({"error": "unauthorized"}), 401
    with _lock:
        before = len(signals_db)
        signals_db[:] = [s for s in signals_db if s.get("close_reason") != "manual"]
        removed = before - len(signals_db)
        if removed:
            save_signals()
    return jsonify({"ok": True, "removed": removed})


@app.route("/api/retest-filled", methods=["POST"])
def api_retest_filled():
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    if AUTH_TOKEN and token != AUTH_TOKEN:
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    symbol     = data.get("symbol", "")
    fill_price = float(data.get("fill_price") or 0)
    qty        = float(data.get("qty") or 0)
    if not symbol or not fill_price:
        return jsonify({"error": "missing fields"}), 400
    sym_norm = symbol.replace("/", "").upper()
    now = tr_now()
    with _lock:
        for s in signals_db:
            if s.get("symbol", "").replace("/", "").upper() != sym_norm:
                continue
            if s.get("status") == "pending_retest":
                s["status"]        = "open"
                s["entry"]         = fill_price
                s["fill_qty"]      = qty
                s["fill_time"]     = now.isoformat()
                s["peak_price"]    = fill_price
                s["low_price"]     = fill_price
                s["current_price"] = fill_price
                save_signals()
                print(f"[RETEST] DOLDU: {sym_norm} @ {fill_price}", flush=True)
                return jsonify({"ok": True})
            if s.get("status") == "open":
                # Periyodik sync (_sync_open_from_bot) webhook'tan önce/yerine
                # zaten open'a terfi ettirmiş olabilir — bot bunu hata sanıp
                # sonsuza kadar (60sn'de bir) tekrar dener, gereksiz alarm
                # veren log satırları üretir. Sonuç zaten doğru, başarı dön.
                print(f"[RETEST] {sym_norm} zaten open (muhtemelen periyodik sync yapmış) — idempotent OK", flush=True)
                return jsonify({"ok": True, "note": "already open"})
    return jsonify({"error": "pending_retest not found"}), 404


@app.route("/api/retest-cancelled", methods=["POST"])
def api_retest_cancelled():
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    if AUTH_TOKEN and token != AUTH_TOKEN:
        return jsonify({"error": "unauthorized"}), 401
    data   = request.get_json(silent=True) or {}
    symbol = data.get("symbol", "")
    if not symbol:
        return jsonify({"error": "missing symbol"}), 400
    sym_norm = symbol.replace("/", "").upper()
    now = tr_now()
    with _lock:
        for s in signals_db:
            if s.get("symbol", "").replace("/", "").upper() == sym_norm and s.get("status") == "pending_retest":
                s["status"]       = "no_retest"
                s["close_time"]   = now.isoformat()
                s["close_reason"] = "no_retest"
                save_signals()
                print(f"[RETEST] İPTAL: {sym_norm}", flush=True)
                return jsonify({"ok": True})
    return jsonify({"error": "pending_retest not found"}), 404


@app.route("/api/tp1-hit", methods=["POST"])
def api_tp1_hit():
    """Al-Sat bot TP1 vurup Binance'te GERÇEKTEN trailing emrini koyduğunda
    bildirir. Bu, tp1_confirmed=True yapan TEK yol — portfolio'nun kendi
    bağımsız fiyat gözlemi (tp1_hit) bunu ASLA "onaylı" yapamaz, sadece bu
    webhook yapar. Dashboard confirmed olmayan durumu ayrı (uyarı) gösterir."""
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    if AUTH_TOKEN and token != AUTH_TOKEN:
        return jsonify({"error": "unauthorized"}), 401
    data    = request.get_json(silent=True) or {}
    symbol  = data.get("symbol", "")
    peak    = float(data.get("peak") or 0)
    tp1_pct = float(data.get("tp1_pct") or 0)
    atr     = data.get("atr")
    if not symbol:
        return jsonify({"error": "missing symbol"}), 400
    sym_norm = symbol.replace("/", "").upper()
    now = tr_now()
    with _lock:
        for s in signals_db:
            if s.get("symbol", "").replace("/", "").upper() == sym_norm and s.get("status") == "open":
                s["tp1_hit"]       = True
                s["tp1_confirmed"] = True
                s["tp1_time"] = now.isoformat()
                s["tp1_pct"]  = tp1_pct
                if peak and peak > s.get("peak_price", 0):
                    s["peak_price"] = peak
                    s["peak_pct"]   = tp1_pct
                if atr:
                    s["atr"] = float(atr)
                s["atr_updated_at"] = datetime.now(timezone.utc).isoformat()
                save_signals()
                print(f"[TP1] {sym_norm} trailing başladı (+{tp1_pct:.2f}%) — BOT ONAYLI", flush=True)
                return jsonify({"ok": True})
    return jsonify({"error": "open position not found"}), 404


@app.route("/api/position-closed", methods=["POST"])
def api_position_closed():
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    if AUTH_TOKEN and token != AUTH_TOKEN:
        return jsonify({"error": "unauthorized"}), 401
    data        = request.get_json(silent=True) or {}
    symbol      = data.get("symbol", "")
    reason      = data.get("reason", "sl_binance")
    close_price = float(data.get("close_price") or 0)
    pnl_pct     = float(data.get("pnl_pct") or 0)
    if not symbol:
        return jsonify({"error": "missing symbol"}), 400
    sym_norm = symbol.replace("/", "").upper()
    now = tr_now()
    with _lock:
        for s in signals_db:
            if s.get("symbol", "").replace("/", "").upper() == sym_norm and s.get("status") == "open":
                s["status"]       = "closed"
                s["close_reason"] = reason
                s["close_time"]   = now.isoformat()
                s["close_price"]  = close_price
                s["close_pct"]    = pnl_pct
                s["outcome"]      = "win" if pnl_pct > 0 else "loss"
                save_signals()
                print(f"[POSITION-CLOSED] {sym_norm} @ {close_price} | {reason} | {pnl_pct:+.2f}%", flush=True)
                return jsonify({"ok": True})
    return jsonify({"error": "open position not found"}), 404


@app.route("/api/manual-sync", methods=["POST"])
def api_manual_sync():
    """Bot'tan anlık sync: pending_retest→open dönüşümü + stale temizliği."""
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    if AUTH_TOKEN and token != AUTH_TOKEN:
        return jsonify({"error": "unauthorized"}), 401
    _sync_from_trading_bot()
    return jsonify({"ok": True, "message": "sync tamamlandı"})


@app.route("/api/trade-positions", methods=["GET"])
def api_trade_positions():
    """trading-bot servisinden tüm pozisyonları çeker."""
    if not TRADING_BOT_URL:
        return jsonify({"error": "TRADING_BOT_URL tanımlı değil"}), 503
    try:
        hdrs = {}
        if TRADING_BOT_TOKEN:
            hdrs["X-Bot-Token"] = TRADING_BOT_TOKEN
        r = requests.get(f"{TRADING_BOT_URL}/status", headers=hdrs, timeout=8)
        return jsonify(r.json()), r.status_code
    except Exception as e:
        return jsonify({"error": str(e)}), 503


@app.route("/api/trade-balance", methods=["GET"])
def api_trade_balance():
    """trading-bot servisinden kullanılabilir (free) USDT bakiyesini çeker."""
    if not TRADING_BOT_URL:
        return jsonify({"error": "TRADING_BOT_URL tanımlı değil"}), 503
    try:
        hdrs = {}
        if TRADING_BOT_TOKEN:
            hdrs["X-Bot-Token"] = TRADING_BOT_TOKEN
        r = requests.get(f"{TRADING_BOT_URL}/balance", headers=hdrs, timeout=8)
        return jsonify(r.json()), r.status_code
    except Exception as e:
        return jsonify({"error": str(e)}), 503


@app.route("/api/trade-positions/<symbol>/delete", methods=["POST"])
def api_trade_position_delete(symbol):
    """trading-bot servisinden belirli pozisyonu siler."""
    if not TRADING_BOT_URL:
        return jsonify({"error": "TRADING_BOT_URL tanımlı değil"}), 503
    try:
        hdrs = {}
        if TRADING_BOT_TOKEN:
            hdrs["X-Bot-Token"] = TRADING_BOT_TOKEN
        r = requests.delete(
            f"{TRADING_BOT_URL}/position/{symbol.upper()}",
            headers=hdrs,
            timeout=8,
        )
        return jsonify(r.json()), r.status_code
    except Exception as e:
        return jsonify({"error": str(e)}), 503


# ============================================================
# PİYASA VERİSİ API
# ============================================================
_market_cache = {"data": None, "ts": 0}
_MARKET_CACHE_TTL = 180  # saniye
_dom_anchor   = {"others_d": None, "ts": 0}
_DOM_ANCHOR_TTL = 86400  # 24 saat

def _fetch_market_pulse():
    """BTC/ETH fiyat, F&G, dominans, MVRV, Altcoin Season — 3 dk cache."""
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
        _btc_p = out.get("btc_price")
        if _btc_p:
            _radar = get_radar(price=_btc_p, long_ratio=out.get("long_ratio"))
            if _radar:
                out["radar"] = _radar
    except Exception as e:
        print(f"[MARKET] Radar hata: {e}", flush=True)
    try:
        import re as _re2
        _hdrs = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
        }
        _bb = requests.get("https://bitbo.io/treasuries/etf-flows/", headers=_hdrs, timeout=15)
        if _bb.ok:
            _rows = _re2.findall(r'<tr[^>]*>(.*?)</tr>', _bb.text, _re2.DOTALL)
            _flows = []
            for _row in _rows:
                _cells = _re2.findall(r'<td[^>]*>(.*?)</td>', _row, _re2.DOTALL)
                if len(_cells) < 3:
                    continue
                _raw = _re2.sub(r'<[^>]+>', '', _cells[-1]).strip().replace(',', '').replace('\xa0', '')
                _raw = _raw.replace('(', '-').replace(')', '')
                try:
                    _flows.append(round(float(_raw), 1))
                except (ValueError, TypeError):
                    pass
            if len(_flows) >= 5:
                out["etf_flows"]  = _flows[-30:]
                out["etf_today"]  = _flows[-1]
                out["etf_5d_avg"] = round(sum(_flows[-5:]) / 5, 1)
                out["etf_7d_sum"] = round(sum(_flows[-7:]), 1)
                out["etf_trend"]  = "pozitif" if sum(_flows[-5:]) > 0 else "negatif"
                print(f"[MARKET] Bitbo ETF OK: {len(_flows)} gün, bugün {_flows[-1]}M, 5G ort {out['etf_5d_avg']}M", flush=True)
            else:
                print(f"[MARKET] Bitbo ETF: parse edilemedi ({len(_flows)} satır)", flush=True)
        else:
            print(f"[MARKET] Bitbo ETF HTTP {_bb.status_code}", flush=True)
    except Exception as e:
        print(f"[MARKET] Bitbo ETF hata: {e}", flush=True)
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
        ls_dom_text, ls_dom_color, ls_label, ls_lc = "—", "#5a6a7a", "—", "#5a6a7a"
    else:
        if ls >= 1:
            ls_dom_text  = f"LONG {ls:.2f}x"
            ls_dom_color = "#2ecc71"
        else:
            ls_dom_text  = f"SHORT {(1/ls):.2f}x"
            ls_dom_color = "#e74c3c"
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
            f'<text x="100" y="20" text-anchor="middle" fill="{ls_dom_color}" font-size="13" font-weight="bold" font-family="monospace">{ls_dom_text}</text>'
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

    etf_today = mp.get("etf_today")
    etf_5d    = mp.get("etf_5d_avg")
    if etf_today is None:
        etf_today_fmt, etf_today_c, etf_today_sub = "—", "#5a6a7a", "veri yok"
    else:
        etf_today_fmt = f"+{etf_today:.0f}M" if etf_today >= 0 else f"{etf_today:.0f}M"
        etf_today_c   = "#2ecc71" if etf_today >= 0 else "#e74c3c"
        if etf_5d is not None:
            etf_today_sub = f"5G ort {'+' if etf_5d>=0 else ''}{etf_5d:.0f}M"
        else:
            etf_today_sub = "net giriş" if etf_today >= 0 else "net çıkış"

    _radar_lines = radar_ui_lines(mp.get("radar"))

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
.group{{display:flex;flex-direction:column;background:var(--card);border:1px solid var(--border);border-radius:8px;overflow:hidden;flex:1;min-width:0}}
.group-title{{font-size:.58rem;letter-spacing:2px;color:var(--accent);text-transform:uppercase;padding:6px 12px;border-bottom:1px solid var(--border);background:#0c1219;font-weight:700;white-space:nowrap}}
.group-metrics{{display:flex;flex:1;overflow-x:auto;-webkit-overflow-scrolling:touch}}
.metric{{flex:0 0 auto;min-width:80px;padding:9px 12px;border-right:1px solid var(--border);display:flex;flex-direction:column;justify-content:space-between;text-align:center}}
.metric:last-child{{border-right:none}}
.m-label{{font-size:.5rem;color:#ecf0f1;letter-spacing:1px;text-transform:uppercase;margin-bottom:4px;font-weight:bold;white-space:nowrap}}
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
.btn-refresh{{background:#1a472a;color:#2ecc71;border:1px solid #2ecc7166;border-radius:4px;padding:3px 10px;font-size:.65rem;cursor:pointer;font-family:inherit;}}
@media(max-width:700px){{
  .header{{flex-wrap:wrap;gap:6px}}
  .time{{width:100%;text-align:right;font-size:.6rem}}
  .groups-row{{gap:6px}}
  .visual-row{{grid-template-columns:repeat(2,1fr)}}
  body{{padding:10px}}
}}
.sym-wrap{{display:inline-flex;align-items:center;white-space:nowrap;cursor:default}}
</style>
</head>
<body>

<div class="header">
  <div>
    <h1>📊 PORTFÖY TAKİP</h1>
    <div class="tabs" style="margin-top:6px">
      <a href="/" class="tab">Portföy</a>
      <a href="/market" class="tab active">Piyasa</a>
      <a href="/alsat" class="tab">Al-Sat Bot</a>
    </div>
  </div>
  <span class="time">{now} | v3.1 &nbsp;<button class="btn-refresh" onclick="location.reload()">🔄 Yenile</button></span>
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
        <div><div class="m-label">ETF Akış</div><div class="m-value" style="color:{etf_today_c};white-space:nowrap;font-size:.82rem">{etf_today_fmt}</div></div>
        <div class="m-sub" style="color:{etf_today_c}">{etf_today_sub}</div>
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
      <div class="gauge-label" style="color:{btc_dom_lc};font-size:.75rem">{btc_dom_label}</div>
      <div class="gauge-sub">CoinLore · anlık</div>
    </div>
  </div>
  <div class="card">
    <h3>Funding Rate</h3>
    <div class="gauge-wrap">
      {fr_bar_svg}
      <div class="gauge-sub">8 saatlik · Binance BTCUSDT</div>
      <div class="gauge-sub" style="margin-top:2px">Vadeli (futures) işlemlerde kim baskın</div>
    </div>
  </div>
  <div class="card">
    <h3>Long / Short</h3>
    <div class="gauge-wrap">
      {ls_bar_svg}
      <div class="gauge-sub">Binance · hesap bazlı · 1s</div>
      {''.join(f'<div style="margin-top:{"14px" if i==0 else "3px"};font-size:.58rem;font-weight:bold;color:{"#2ecc71" if "Destek" in l else "#e74c3c"};font-family:monospace">{l}</div>' for i,l in enumerate(_radar_lines))}
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
var _coinParam = new URLSearchParams(location.search).get('coin');
var _tvWidget = new TradingView.widget({{
  container_id:"tv_chart",width:"100%",height:460,
  symbol: _coinParam ? "BINANCE:" + _coinParam : "BINANCE:BTCUSDT",
  interval:"60",
  timezone:"Europe/Istanbul",theme:"dark",style:"1",locale:"tr",
  toolbar_bg:"#0f1319",hide_side_toolbar:false,allow_symbol_change:true,
  backgroundColor:"#0a0e14",gridColor:"#1a2030"
}});
if (_coinParam) {{
  document.getElementById('tv_chart').scrollIntoView({{behavior:'smooth',block:'center'}});
}}
</script>
<script>var SYMCI={json.dumps(_CHART_SVG)};var SYMTV={json.dumps(_TV_LOGO)};</script>
{_SYM_POPUP_HTML}
{_PRICE_TT_HTML}
</body>
</html>"""


def fmt_price(p):
    if p is None: return "—"
    p = float(p)
    if p <= 0: return "0"
    if p >= 100: return f"{p:.2f}"
    if p >= 1:   return f"{p:.4f}".rstrip('0').rstrip('.')
    import math
    # İlk anlamlı haneden itibaren 4 rakam — Binance tick size'larıyla örtüşür
    decimals = min(8, -math.floor(math.log10(p)) + 3)
    return f"{p:.{decimals}f}".rstrip('0').rstrip('.')

_price_precision_cache = {}
_price_precision_lock  = threading.Lock()

def _get_price_precision(symbol: str):
    """Binance'in gerçek PRICE_FILTER tickSize'ından ondalık basamak sayısı.
    exchangeInfo public endpoint — API key gerekmez. Bulunamazsa None döner."""
    with _price_precision_lock:
        if symbol in _price_precision_cache:
            return _price_precision_cache[symbol]
    try:
        r = requests.get("https://api.binance.com/api/v3/exchangeInfo",
                          params={"symbol": symbol}, timeout=5)
        if r.ok:
            data = r.json()
            for f in data["symbols"][0]["filters"]:
                if f["filterType"] == "PRICE_FILTER":
                    import math
                    tick = float(f["tickSize"])
                    precision = max(0, int(round(-math.log10(tick))))
                    with _price_precision_lock:
                        _price_precision_cache[symbol] = precision
                    return precision
    except Exception:
        pass
    return None

def fmt_price_symbol(symbol: str, p):
    """fmt_price'ın sembole duyarlı hâli — Binance'in gerçek tickSize hassasiyetini
    kullanır, aynı sembolün tüm fiyat sütunları (anlık/giriş/stop/TP1) aynı basamak
    sayısıyla görünür. Precision bulunamazsa genel sezgisel fmt_price'a düşer."""
    if p is None: return "—"
    p = float(p)
    if p <= 0: return "0"
    precision = _get_price_precision(symbol)
    if precision is not None:
        return f"{p:.{precision}f}"
    return fmt_price(p)

def pct_color(pct):
    if pct is None: return "#8a9bb0", "—"
    pct = float(pct)
    color = "#2ecc71" if pct > 0 else ("#e74c3c" if pct < 0 else "#8a9bb0")
    return color, f"{pct:+.2f}%"

def status_badge(status, sig=None):
    colors = {
        "open":           ("#3498db", "AÇIK"),
        "pending_retest": ("#f39c12", "RETEST BEKLİYOR"),
        "no_retest":      ("#95a5a6", "RETEST YOK"),
        "win_tp1":        ("#2ecc71", "WIN (TP1)"),
        "win_tp2":        ("#27ae60", "WIN (TP2)"),
        "win_trail":      ("#27ae60", "WIN (TRAIL)"),
        "loss":           ("#e74c3c", "LOSS"),
        "expired":        ("#f39c12", "EXPIRED"),
    }
    if status == "closed" and sig is not None:
        # Trading bot'un güncel şeması: status hep "closed", gerçek sonuç
        # outcome/close_reason/close_pct'te — classify_signal_outcome() ile aynı
        # sınıflandırma (calc_performance() ile tutarlı). Süre dolarak kapanan
        # (is_expired) her zaman sarı zeminde gösterilir, ama gerçek sonuç
        # hem yazıda (EXPIRED +WIN / EXPIRED -LOSS) hem kenarlık renginde
        # (yeşil/kırmızı) — "neden win değil?" sorusunu rozetin kendisi
        # tek bakışta cevaplasın diye.
        kind, is_expired = classify_signal_outcome(sig)
        border = None
        if is_expired:
            c = "#f39c12"
            if kind == "win":
                border, label = "#2ecc71", "EXPIRED +WIN"
            elif kind == "loss":
                border, label = "#e74c3c", "EXPIRED -LOSS"
            else:
                label = "EXPIRED"
        elif kind == "win":
            c, label = ("#2ecc71", "WIN")
        elif kind == "loss":
            c, label = ("#e74c3c", "LOSS")
        elif kind == "manual":
            c, label = ("#8a9bb0", "MANUEL")
        else:
            c, label = colors.get(status, ("#8a9bb0", status.upper()))
    else:
        c, label = colors.get(status, ("#8a9bb0", status.upper()))
        border = None
    border_style = f"2px solid {border}" if border else "none"
    return (f'<span style="background:{c};color:#0a0e14;padding:2px 8px;border-radius:3px;'
            f'font-size:.7rem;font-weight:bold;white-space:nowrap;border:{border_style}">{label}</span>')

def type_badge(sig):
    sig_type = sig.get("sig_type", "unknown")
    sub = sig.get("sub_type", "")
    source = sig.get("source", "bot")
    if source in SMC_MAIN_SOURCES:
        phase = sig.get("phase", "")
        phase_label = "Discount" if phase == "discount" else ("CHoCH" if phase == "choch" else phase.replace('phase', 'P'))
        if source == "smc-v2":
            return f'<span style="border:1px solid #e67e22;color:#d0d0d0;padding:1px 6px;border-radius:3px;font-size:.65rem;white-space:nowrap">Legacy SMC</span>'
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

def analyzer_badge(sig):
    d = sig.get("analyzer_decision") or ""
    if not d:
        return '<span style="color:#5a6a7a;font-size:.6rem">—</span>'
    if "✅" in d:   c, l = "#2ecc71", "✅ GİR"
    elif "⚠️" in d: c, l = "#f39c12", "⚠️ DİKKAT"
    elif "🚫" in d: c, l = "#e74c3c", "🚫 RİSKLİ"
    else:            c, l = "#8a9bb0", d[:12]
    return f'<span style="background:{c}22;color:{c};padding:1px 6px;border-radius:3px;font-size:.6rem">{l}</span>'

_CHART_SVG = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" style="display:block"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>'
_TV_LOGO   = '<img src="https://www.tradingview.com/favicon.ico" width="14" height="14" style="display:block;border-radius:2px;image-rendering:crisp-edges" alt="TV" onerror="this.outerHTML=\'<span style=font-size:.65rem;font-weight:bold;color:#2962ff>TV</span>\'">'

# Fiyat alt satırı tooltip
_PRICE_TT_HTML = (
    '<div id="_ptt" style="display:none;position:fixed;z-index:9998;background:#151d2a;'
    'border:1px solid #2a3a50;border-radius:6px;padding:6px 10px;font-size:.72rem;'
    'color:#c9d1d9;pointer-events:none;line-height:1.7;white-space:nowrap"></div>'
    '<script>(function(){'
    'var el=document.getElementById("_ptt");'
    'document.addEventListener("mousemove",function(e){'
    'var t=e.target.closest("[data-ptt]");'
    'if(!t){el.style.display="none";return;}'
    'el.innerHTML=t.getAttribute("data-ptt");'
    'el.style.display="block";'
    'var x=e.clientX+14,y=e.clientY+14;'
    'if(x+el.offsetWidth+8>window.innerWidth)x=e.clientX-el.offsetWidth-8;'
    'el.style.left=x+"px";el.style.top=y+"px";'
    '});'
    '})();</script>'
)

# Popup position:fixed — table overflow/stacking context'inden bağımsız
_SYM_POPUP_HTML = (
    '<div id="_symp" style="display:none;position:fixed;z-index:9999;background:#151d2a;'
    'border:1px solid #2a3a50;border-radius:7px;padding:3px 5px;gap:3px;'
    'align-items:center;box-shadow:0 4px 14px rgba(0,0,0,.75)"></div>'
    '<style>#_symp .spb{color:#6a8aaa;text-decoration:none;padding:4px 5px;border-radius:5px;'
    'display:flex;align-items:center;transition:color .15s,background .15s}'
    '#_symp .spb:hover{color:#00b4d8;background:#1a2a3a}</style>'
    '<script>(function(){'
    'var pop=document.getElementById("_symp"),t,b=pop.style;'
    'function mk(h,tg,ti,inn){var a=document.createElement("a");'
    'a.href=h;if(tg){a.target=tg;a.rel="noopener";}a.title=ti;a.className="spb";a.innerHTML=inn;return a;}'
    'document.querySelectorAll(".sym-wrap").forEach(function(w){'
    'w.addEventListener("mouseenter",function(){'
    'clearTimeout(t);'
    'var p=w.dataset.pair,r=w.getBoundingClientRect();'
    'pop.innerHTML="";'
    'pop.appendChild(mk("/market?coin="+p+"#tv_chart",null,"Grafikte a\\u00e7",SYMCI));'
    'pop.appendChild(mk("https://www.tradingview.com/chart/?symbol=BINANCE:"+p,"_blank","TradingView\'de a\\u00e7",SYMTV));'
    'b.left=r.left+"px";b.top=(r.bottom+4)+"px";b.display="flex";'
    '});'
    'w.addEventListener("mouseleave",function(){t=setTimeout(function(){b.display="none";},150);});'
    '});'
    'pop.addEventListener("mouseenter",function(){clearTimeout(t);});'
    'pop.addEventListener("mouseleave",function(){b.display="none";});'
    '})();</script>'
)

def sym_cell(sym: str) -> str:
    """Coin adı — hover ile fixed-position popup açar (JS yönetir)."""
    pair = sym + "USDT"
    return f'<span class="sym-wrap" data-pair="{pair}"><b>{sym}</b></span>'

@app.route("/")
def dashboard():
    perf = calc_performance()
    now = tr_now_str()
    now_dt = tr_now()

    with _lock:
        all_sigs = list(signals_db)
    all_sigs = [s for s in all_sigs if s.get("sig_type", "unknown") not in HIDDEN_SIG_TYPES]

    open_sigs    = [s for s in all_sigs if s.get("status") == "open"]
    pending_sigs = [s for s in all_sigs if s.get("status") == "pending_retest"]
    closed_sigs  = [s for s in all_sigs if s.get("status") not in ("open", "pending_retest")]
    closed_sigs.sort(key=lambda s: s.get("close_time") or "", reverse=True)

    open_rows = ""
    for sig in open_sigs[:50]:
        cur_c, cur_s = pct_color(sig.get("current_pct"))
        peak_c, peak_s = pct_color(sig.get("peak_pct"))
        low_c, low_s = pct_color(sig.get("low_pct"))
        sym = sig["symbol"].replace("/USDT", "")
        tp1_pct = round((sig["tp1"] - sig["entry"]) / sig["entry"] * 100, 2) if sig["entry"] > 0 else 0
        tp2_val = sig.get("tp2")
        tp2_pct_open = round((tp2_val - sig["entry"]) / sig["entry"] * 100, 1) if tp2_val and sig["entry"] > 0 else 0

        _tp1_confirmed = sig.get("tp1_confirmed")
        _is_spot_opportunity = (
            sig.get("source") == "spot-scanner"
            or sig.get("sig_type") == "spot_opportunity"
        )
        if sig.get("tp1_hit") and _is_spot_opportunity:
            _live_trail = spot_opportunity_trail_stop(
                sig["entry"], sig.get("peak_price", sig["entry"])
            )
            _trail_pct = round((_live_trail - sig["entry"]) / sig["entry"] * 100, 2)
            stop_cell = f"✅ {fmt_price(_live_trail)} ({_trail_pct:+.2f}%)"
        elif sig.get("tp1_hit") and sig.get("source") in FULL_TRAIL_SOURCES and _tp1_confirmed:
            # SADECE bot onayladıysa (gerçekten Binance'te emir değişti) canlı trail
            # seviyesini "güvenli" gibi gösteriyoruz.
            _live_trail = trail_stop_price(sig.get("peak_price", sig["entry"]), sig.get("atr"))
            _trail_pct  = round((_live_trail - sig["entry"]) / sig["entry"] * 100, 2) if sig["entry"] > 0 else 0
            stop_cell = f"✅ {fmt_price(_live_trail)} ({_trail_pct:+.2f}%)"
        elif sig.get("tp1_hit") and sig.get("source") in FULL_TRAIL_SOURCES:
            # TP1 fiyattan görüldü ama bot HENÜZ onaylamadı — gerçek koruma hâlâ
            # eski stop seviyesinde olabilir, bunu ASLA "trailing" gibi gösterme.
            stop_pct = round((sig["stop"] - sig["entry"]) / sig["entry"] * 100, 2) if sig["entry"] > 0 else 0
            stop_cell = (f'<span style="color:#f39c12" title="TP1 fiyattan görüldü ama bot henüz Binance emrini '
                         f'değiştirmedi — gerçek koruma hâlâ bu seviyede">⚠️ {fmt_price(sig["stop"])} ({stop_pct:+.2f}%)</span>')
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
        if tp1_milestone and _is_spot_opportunity:
            _tp1_hit_pct = sig.get("tp1_pct", tp1_pct)
            tp1_cell = (f'<span style="background:#2ecc7133;color:#2ecc71;padding:1px 5px;border-radius:3px;'
                        f'font-size:.6rem;white-space:nowrap">✅ TP1 GÖRÜLDÜ · %{SPOT_OPPORTUNITY_TRAIL_PCT:.1f} TRAİLİNG</span>')
        elif tp1_milestone and is_full_trail_sig and _tp1_confirmed:
            _tp1_hit_pct = sig.get("tp1_pct", tp1_pct)
            tp1_cell = (f'<span style="background:#2ecc7133;color:#2ecc71;padding:1px 5px;border-radius:3px;'
                        f'font-size:.6rem;white-space:nowrap">✅ TRAİLİNG AKTİF (onaylı) +{_tp1_hit_pct:.2f}%</span>')
        elif tp1_milestone and is_full_trail_sig:
            _tp1_hit_pct = sig.get("tp1_pct", tp1_pct)
            tp1_cell = (f'<span style="background:#f39c1233;color:#f39c12;padding:1px 5px;border-radius:3px;'
                        f'font-size:.6rem;white-space:nowrap" title="Portfolio fiyattan gördü ama bot henüz '
                        f'Binance emrini değiştirmediğini onaylamadı">⚠️ TP1 GEÇİLDİ (onay bekleniyor) +{_tp1_hit_pct:.2f}%</span>')
        elif tp1_milestone:
            tp1_cell = (f'<span style="background:#2ecc7133;color:#2ecc71;padding:1px 5px;border-radius:3px;font-size:.6rem;white-space:nowrap">✅ +{tp1_pct:.2f}% milestone</span>')
        else:
            tp1_cell = f"{fmt_price(sig['tp1'])} (+{tp1_pct:.2f}%)"
        open_rows += f"""<tr>
            <td style="color:#ecf0f1">{sym_cell(sym)}</td><td>{type_badge(sig)}</td>
            <td>{fmt_price(sig['entry'])}</td>
            <td style="color:{cur_c};font-weight:bold">{fmt_price(sig.get('current_price'))} ({cur_s})</td>
            <td style="color:{peak_c}">{peak_s}</td><td style="color:{low_c}">{low_s}</td>
            <td>{stop_cell}</td><td>{tp1_cell}</td>
            <td>{fmt_price(tp2_val)} (+{tp2_pct_open}%)</td>
            <td style="font-size:.7rem;color:#7f8c8d;white-space:nowrap;text-align:center">{datetime.fromisoformat(sig['open_time']).strftime('%d/%m/%Y') if sig.get('open_time') else '—'}<br><span style="font-size:.65rem;color:#5a6a7a">{datetime.fromisoformat(sig['open_time']).strftime('%H:%M') if sig.get('open_time') else ''}</span></td>
            <td>{sure_cell}</td><td>{analyzer_badge(sig)}</td>
            <td><button onclick="closeSignal('{sig['id']}',this)"
                style="background:#e74c3c22;color:#e74c3c;border:1px solid #e74c3c55;
                border-radius:4px;padding:2px 8px;font-size:.6rem;cursor:pointer;font-family:inherit">
                Kapat</button></td></tr>"""

    def _closed_row(sig):
        close_c, close_s = pct_color(sig.get("close_pct"))
        peak_c, peak_s = pct_color(sig.get("peak_pct"))
        sym = sig["symbol"].replace("/USDT", "")
        _cr = sig.get("close_reason", "")
        # NOT: sig['tp1'] portfolio_tracker'ın KENDİ signals_db'sinde tutulan
        # ORİJİNAL (capsiz) TP1 hedefi — position_monitor.py'deki TP1 cap
        # (effective_tp1) bilgisi buraya hiç ulaşmıyor. Bu yüzden bu yüzde
        # asla "TP1 Hit" olarak, gerçekten o yüzdede vurulmuş gibi basılmaz —
        # sadece ayrı, açıkça "Orijinal Hedef" etiketli bir referans olarak
        # gösterilir.
        orig_tp1_pct_v = round((sig["tp1"] - sig["entry"]) / sig["entry"] * 100, 2) if sig.get("entry", 0) > 0 and sig.get("tp1") else 0
        _orig_line = f'<br><span style="color:#5a6a7a;font-size:.55rem">Orijinal Hedef: +{orig_tp1_pct_v:.2f}%</span>' if orig_tp1_pct_v else ''
        if sig.get("tp1_hit") and _cr == "trailing":
            _fin_p = sig.get("close_pct", 0)
            tp1_badge = (f'<span style="color:#3498db;font-size:.58rem">'
                         f'TP1 trail aktif → çıkış:{_fin_p:+.2f}%</span>')
        elif sig.get("tp1_hit"):
            tp1_badge = f'<span style="color:#2ecc71;font-size:.58rem">✓TP1 Hit</span>{_orig_line}'
        else:
            tp1_badge = f'<span style="color:#3a4a5a;font-size:.58rem">Orijinal Hedef: +{orig_tp1_pct_v:.2f}%</span>' if orig_tp1_pct_v else '—'

        return f"""<tr>
            <td style="color:#ecf0f1">{sym_cell(sym)}</td><td>{type_badge(sig)}</td>
            <td>{status_badge(sig.get('status','unknown'), sig)}</td>
            <td>{fmt_price(sig['entry'])}</td>
            <td>{fmt_price(sig.get('close_price'))}</td>
            <td style="color:{close_c};font-weight:bold">{close_s}</td>
            <td style="color:{peak_c}">{peak_s}</td>
            <td>{tp1_badge}</td>
            <td>{analyzer_badge(sig)}</td>
            <td style="font-size:.7rem;color:#7f8c8d;white-space:nowrap;text-align:center">{datetime.fromisoformat(sig['open_time']).strftime('%d/%m/%Y') if sig.get('open_time') else '—'}<br><span style="font-size:.65rem;color:#5a6a7a">{datetime.fromisoformat(sig['open_time']).strftime('%H:%M') if sig.get('open_time') else ''}</span></td>
            <td style="font-size:.7rem;color:#7f8c8d;white-space:nowrap;text-align:center">{datetime.fromisoformat(sig['close_time']).strftime('%d/%m/%Y') if sig.get('close_time') else '—'}<br><span style="font-size:.65rem;color:#5a6a7a">{datetime.fromisoformat(sig['close_time']).strftime('%H:%M') if sig.get('close_time') else ''}</span></td></tr>"""

    no_retest_sigs = [s for s in closed_sigs if s.get("status") == "no_retest"]
    closed_sigs_real = [s for s in closed_sigs if s.get("status") != "no_retest"]

    closed_rows = "".join(_closed_row(sig) for sig in closed_sigs_real[:100])
    no_retest_rows = "".join(_closed_row(sig) for sig in no_retest_sigs[:100])

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

    _smc_eski_section = ""

    # ── Retest Bekleyenler ──────────────────────────────────────────────────────
    _pending_price_cache = {s["symbol"]: get_current_price_hl(s["symbol"]) for s in pending_sigs}

    def _retest_proximity(sig):
        lp = sig.get("limit_price")
        pd = _pending_price_cache.get(sig["symbol"])
        if not pd or not lp:
            return float("inf")
        return abs((pd["close"] - lp) / lp * 100)

    pending_sigs.sort(key=_retest_proximity)  # buy anına (limit seviyesine) en yakın en üstte

    pending_rows = ""
    for sig in pending_sigs[:50]:
        sym = sig["symbol"].replace("/USDT", "")
        lp  = sig.get("limit_price")
        lp_str = fmt_price(lp) if lp else "—"
        tp1_pct = round((sig["tp1"] - sig["entry"]) / sig["entry"] * 100, 2) if sig.get("entry", 0) > 0 and sig.get("tp1") else 0
        try:
            ot = datetime.fromisoformat(sig["open_time"])
            if ot.tzinfo is None: ot = ot.replace(tzinfo=TR_TZ)
            elapsed_h = (now_dt - ot).total_seconds() / 3600
            remaining_h = max(0, 48 - elapsed_h)
            elapsed_str  = f"{int(elapsed_h)}s"
            remaining_str = f"{int(remaining_h)}s"
            rem_color = "#e74c3c" if remaining_h < 6 else ("#f39c12" if remaining_h < 12 else "#7f8c8d")
        except Exception:
            elapsed_str = remaining_str = "—"; rem_color = "#7f8c8d"

        # Canlı fiyat
        sp = sig.get("signal_price")
        _sp_val = float(sp) if sp else 0.0
        _price_data = _pending_price_cache.get(sig["symbol"])
        _cur = _price_data["close"] if _price_data else None
        if _cur and lp:
            _dist_pct = (_cur - lp) / lp * 100   # renk kodu için limit'e uzaklık
            _stop_val = float(sig.get("stop", 0))
            if _cur <= _stop_val:
                _price_color = "#e74c3c"
            elif _dist_pct <= 0.5:
                _price_color = "#f39c12"
            elif _dist_pct <= 3:
                _price_color = "#00b4d8"
            else:
                _price_color = "#7f8c8d"
            # Alt satır: sinyalden değişim / girişe kalan
            if _sp_val > 0:
                _chg_pct = (_cur - _sp_val) / _sp_val * 100
                _chg_color = "#2ecc71" if _chg_pct < 0 else "#e74c3c"
                _tt = "&#9654; Sinyal fiyatından bu yana değişim<br>&#9654; Limit buy hedefine kalan mesafe"
                _sub = (
                    f'<span data-ptt="{_tt}" style="cursor:default">'
                    f'<span style="font-size:.6rem;color:{_chg_color}">{_chg_pct:+.2f}%</span>'
                    f'<span style="font-size:.6rem;color:#3a4a5a"> / </span>'
                    f'<span style="font-size:.6rem;color:{_price_color}">−{_dist_pct:.2f}%</span>'
                    f'</span>'
                )
            else:
                _sub = f'<span style="font-size:.6rem;color:{_price_color}">−{_dist_pct:.2f}%</span>'
            _cur_cell = f'<span style="color:{_price_color};font-weight:bold">{fmt_price(_cur)}</span><br>{_sub}'
        elif _cur:
            _cur_cell = fmt_price(_cur)
        else:
            _cur_cell = '<span style="color:#3a4a5a">—</span>'

        if sp and lp and _sp_val > 0:
            _sig_to_limit = (float(lp) - _sp_val) / _sp_val * 100
            _sp_cell = (f'{fmt_price(sp)}<br>'
                        f'<span style="font-size:.6rem;color:#7f8c8d">{_sig_to_limit:.2f}% girişe</span>')
        else:
            _sp_cell = fmt_price(sp) if sp else '<span style="color:#3a4a5a">—</span>'

        choch_val = float(sig['entry'])
        if lp and choch_val:
            _choch_entry_cell = f'{fmt_price(choch_val)} / <span style="color:#f39c12;font-weight:bold">{lp_str}</span>'
        elif lp:
            _choch_entry_cell = f'<span style="color:#f39c12;font-weight:bold">{lp_str}</span>'
        else:
            _choch_entry_cell = fmt_price(choch_val)

        pending_rows += f"""<tr>
            <td style="color:#ecf0f1">{sym_cell(sym)}</td>
            <td>{_sp_cell}</td>
            <td>{_cur_cell}</td>
            <td>{_choch_entry_cell}</td>
            <td>{fmt_price(sig['stop'])}</td>
            <td>{fmt_price(sig['tp1'])} (+{tp1_pct:.2f}%)</td>
            <td style="color:#7f8c8d;font-size:.7rem">{elapsed_str}</td>
            <td style="color:{rem_color};font-size:.7rem;font-weight:bold">{remaining_str}</td>
            <td>{analyzer_badge(sig)}</td></tr>"""

    if pending_sigs:
        _pending_section = f"""<div class="section">
    <details data-id="pending-retest" open>
    <summary>⏳ RETEST BEKLEYENLER ({len(pending_sigs)})</summary>
    <p class="note">CHoCH seviyesine limit emir konuldu. 48 saat içinde fiyat geri dönmezse otomatik iptal. Anlık fiyattaki % = limite olan uzaklık (limit altına inince emir dolar).</p>
    <div class="table-wrap"><table><thead><tr>
        <th>Sembol</th><th>Sinyal Fiyat</th><th>Anlık Fiyat</th><th>CHoCH / Limit Buy</th><th>Stop</th><th>TP1</th><th>Geçen</th><th>Kalan</th><th>Analiz</th>
    </tr></thead><tbody>
        {pending_rows}
    </tbody></table></div>
    </details>
</div>"""
    else:
        _pending_section = ""


    html = f"""<!DOCTYPE html>
<html lang="tr"><head>
<meta charset="UTF-8"><title>Portföy Takip</title>
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
  table{{font-size:.63rem;}}td,th{{padding:5px 5px;}}
  .header{{flex-wrap:wrap;gap:6px;}}
  .header .time{{width:100%;justify-content:flex-end;}}}}
.sym-wrap{{display:inline-flex;align-items:center;white-space:nowrap;cursor:default}}
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
    <div>
        <h1>📊 PORTFÖY TAKİP</h1>
        <div style="display:flex;gap:6px;margin-top:6px">
            <a href="/" class="nav-tab active">Portföy</a>
            <a href="/market" class="nav-tab">Piyasa</a>
            <a href="/alsat" class="nav-tab">Al-Sat Bot</a>
        </div>
    </div>
    <span class="time">
        {now}
        <button class="btn-refresh" onclick="location.reload()">🔄 Yenile</button>
        <button class="btn-clear"
            onclick="if(confirm('Yalnızca Spot Scanner kaynaklı açık kayıtlar topluca kapatılacak.\\nDiğer açık kayıtlara ve öğrenme arşivine dokunulmayacak.\\nEmin misiniz?')){{fetch('/api/signals/close-spot-scanner',{{method:'POST'}}).then(r=>r.json()).then(d=>{{alert('Kapatılan Spot Scanner kaydı: '+d.closed);location.reload()}})}}"
        >⛔ Spot Açıklarını Kapat</button>
        <button class="btn-clear"
            onclick="if(confirm('Kapanmış geçmiş ve retest bekleyen kayıtlar temizlenecek.\\nAçık pozisyonlara DOKUNULMAZ.\\nÖğrenme arşivi korunur.\\nEmin misiniz?')){{fetch('/api/signals/clear-history',{{method:'POST'}}).then(r=>r.json()).then(d=>{{alert('Temizlendi: '+d.removed+' kayıt ('+d.kept+' açık kayıt korundu)');location.reload()}})}}"
        >🧹 Geçmişi Temizle</button>
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
  const decided = wins + losses;
  const wr = decided > 0 ? (wins / decided * 100).toFixed(1) : 0;
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
    <div class="card"><span class="val" style="color:var(--orange)" id="c-pending">{len(pending_sigs)}</span><span class="lbl">Beklemede</span></div>
    <div class="card"><span class="val" style="color:#3498db" id="c-open">{perf.get('open',0)}</span><span class="lbl">Açık</span></div>
    <div class="card"><span class="val" style="color:var(--green)" id="c-wins">{perf.get('wins',0)}</span><span class="lbl">Win</span>
        <span style="font-size:.6rem;color:#8a9bb0;display:block">{perf.get('win_pnl',0):+.2f}%</span></div>
    <div class="card"><span class="val" style="color:var(--red)" id="c-loss">{perf.get('losses',0)}</span><span class="lbl">Loss</span>
        <span style="font-size:.6rem;color:#8a9bb0;display:block">{perf.get('loss_pnl',0):+.2f}%</span></div>
    <div class="card"><span class="val" style="color:var(--orange)" id="c-exp">{perf.get('expired',0)}</span><span class="lbl">Expired</span>
        <span style="font-size:.6rem;color:#8a9bb0;display:block">{perf.get('expired_win',0)} win / {perf.get('expired_loss',0)} loss | {perf.get('expired_pnl',0):+.2f}%</span></div>
    <div class="card"><span class="val" id="c-wr" style="color:{'var(--green)' if perf.get('real_win_rate',0)>=50 else 'var(--red)'}"
        >%{perf.get('real_win_rate',0)}</span><span class="lbl">Win Rate</span>
        <span style="font-size:.6rem;color:#8a9bb0;display:block">Toplam kapanan: %{perf.get('win_rate',0)}</span></div>
    <div class="card"><span class="val" id="c-pnl" style="color:{pnl_color_val}">{total_pnl:+.2f}%</span><span class="lbl">Net P&L</span>
        <span style="font-size:.6rem;color:#8a9bb0;display:block">W/L: {perf.get('win_loss_pnl',0):+.2f}% | Exp: {perf.get('expired_pnl',0):+.2f}%</span></div>
    <div class="card"><span class="val" id="c-peak">{perf.get('avg_peak',0)}%</span><span class="lbl">Ort. Peak</span></div>
</div>

{_analyzer_section}

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

<div class="section">
    <details data-id="open-pos" open>
    <summary>🔵 AÇIK POZİSYONLAR ({len(open_sigs)})</summary>
    <p class="note">Spot adayları: TP1 → peak'ten %{SPOT_OPPORTUNITY_TRAIL_PCT:.1f} trailing (taban: giriş) | TP1 öncesi 24s ufuk ve yapısal stop. Legacy SMC: TP1 → ATR×0.6 trailing | PUMP: hard SL/TP, 6s expire.</p>
    <div class="table-wrap"><table><thead><tr>
        <th>Sembol</th><th>Tür</th><th>Giriş</th><th>Şu An</th><th>Peak</th><th>Dip</th>
        <th>Trail/Stop</th><th>TP1</th><th>TP2</th><th>Tarih</th><th>Süre</th><th>Analiz</th><th></th>
    </tr></thead><tbody>
        {open_rows if open_rows else '<tr><td colspan="13" class="empty">Açık pozisyon yok</td></tr>'}
    </tbody></table></div>
    </details>
</div>

{_pending_section}


<div class="section">
    <details data-id="closed-list">
    <summary>📋 KAPANMIŞ İŞLEMLER (son 100)</summary>
    <div class="table-wrap"><table><thead><tr>
        <th>Sembol</th><th>Tür</th><th>Sonuç</th><th>Giriş</th><th>Çıkış</th><th>Getiri</th><th>Peak</th>
        <th>TP1 Hit</th><th>Analiz</th><th>Açılış</th><th>Kapanış</th>
    </tr></thead><tbody>
        {closed_rows if closed_rows else '<tr><td colspan="11" class="empty">Henüz kapanmış işlem yok</td></tr>'}
    </tbody></table></div>
    </details>
</div>

<div class="section">
    <details data-id="no-retest-list">
    <summary>🚫 RETEST OLMAYANLAR ({len(no_retest_sigs)})</summary>
    <div class="table-wrap"><table><thead><tr>
        <th>Sembol</th><th>Tür</th><th>Sonuç</th><th>Giriş</th><th>Çıkış</th><th>Getiri</th><th>Peak</th>
        <th>TP1 Hit</th><th>Analiz</th><th>Açılış</th><th>Kapanış</th>
    </tr></thead><tbody>
        {no_retest_rows if no_retest_rows else '<tr><td colspan="11" class="empty">Retest olmayan işlem yok</td></tr>'}
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
    Legacy SMC: CHoCH+1tick limit → retest 48H → fill sonrası SL | TP1 → ATR×0.6 trailing | PUMP: hard SL/TP, 6h expire |
    Kontrol: {CHECK_INTERVAL//60}dk | {now}
</div>
<script>var SYMCI={json.dumps(_CHART_SVG)};var SYMTV={json.dumps(_TV_LOGO)};</script>
<script>
function closeSignal(id,btn){{
  if(!confirm('Bu pozisyonu manuel kapattı olarak işaretle?'))return;
  btn.disabled=true;btn.textContent='...';
  fetch('/api/signal/'+id+'/close',{{method:'POST'}})
    .then(r=>r.json()).then(d=>{{
      if(d.ok){{btn.closest('tr').style.opacity='0.4';btn.textContent='Kapandı';setTimeout(()=>location.reload(),800);}}
      else{{btn.textContent='Hata';btn.disabled=false;}}
    }}).catch(()=>{{btn.textContent='Hata';btn.disabled=false;}});
}}
</script>
{_SYM_POPUP_HTML}
{_PRICE_TT_HTML}
</body></html>"""
    return html


# ============================================================
# AL-SAT BOT SAYFASI
# ============================================================
@app.route("/alsat")
def alsat_page():
    now = tr_now_str()

    trade_positions = {}
    usdt_balance = None
    error_msg = ""
    if TRADING_BOT_URL:
        try:
            hdrs = {}
            if TRADING_BOT_TOKEN:
                hdrs["X-Bot-Token"] = TRADING_BOT_TOKEN
            r = requests.get(f"{TRADING_BOT_URL}/status", headers=hdrs, timeout=6)
            if r.ok:
                trade_positions = r.json()
            else:
                error_msg = f"Trading-bot HTTP {r.status_code}"
        except Exception as e:
            error_msg = str(e)
        try:
            bh = {}
            if TRADING_BOT_TOKEN:
                bh["X-Bot-Token"] = TRADING_BOT_TOKEN
            br = requests.get(f"{TRADING_BOT_URL}/balance", headers=bh, timeout=6)
            if br.ok:
                usdt_balance = br.json().get("usdt_balance")
        except Exception:
            pass
    else:
        error_msg = "TRADING_BOT_URL tanımlı değil"

    balance_val = f"${usdt_balance:,.2f}" if usdt_balance is not None else "—"

    # Açık (fill olmuş) pozisyonların o anki piyasa değeri — trading-bot'un
    # /status'undan gelen qty/current_price zaten mevcut, ek istek gerekmiyor.
    # Pending (henüz fill olmamış limit emri) pozisyonların ayırdığı tutar da
    # (pos_size_usdt) eklenir — bu para usdt_balance'ta da görünmüyor çünkü
    # emir olarak Binance'te bekliyor, aksi halde toplamdan hiç sayılmıyordu.
    positions_value = sum(
        float(p.get("qty", 0) or 0) * float(p.get("current_price", 0) or 0)
        for p in trade_positions.values() if p.get("status") == "open"
    ) + sum(
        float(p.get("pos_size_usdt", 0) or 0)
        for p in trade_positions.values() if p.get("status") == "pending"
    )
    positions_value_val = f"${positions_value:,.2f}"
    total_value_val = f"${usdt_balance + positions_value:,.2f}" if usdt_balance is not None else "—"

    STATUS_LABEL = {
        "monitoring": ('<span style="background:#f39c1222;color:#f39c12;border:1px solid #f39c1255;'
                       'border-radius:3px;padding:2px 8px;font-size:.65rem">İZLEME</span>'),
        "pending":    ('<span style="background:#3498db22;color:#3498db;border:1px solid #3498db55;'
                       'border-radius:3px;padding:2px 8px;font-size:.65rem">EMİR</span>'),
        "open":       ('<span style="background:#2ecc7122;color:#2ecc71;border:1px solid #2ecc7155;'
                       'border-radius:3px;padding:2px 8px;font-size:.65rem">AÇIK</span>'),
    }

    rows = ""
    now_dt = tr_now()
    for sym, pos in trade_positions.items():
        st = pos.get("status", "")
        if pos.get("tp1_pending_trail"):
            # TP1 vuruldu ama koruma (native/ATR trailing ya da eski sabit SL)
            # HENÜZ kurulamadı — normal "AÇIK" rozetiyle karıştırılmamalı,
            # operatör bunu net görüp gerekirse manuel kontrol etmeli.
            badge = ('<span style="background:#e74c3c22;color:#e74c3c;border:1px solid #e74c3c55;'
                     'border-radius:3px;padding:2px 8px;font-size:.65rem;font-weight:bold">'
                     '⚠ KORUMA BEKLİYOR / MANUEL KONTROL</span>')
        else:
            badge = STATUS_LABEL.get(st, f'<span style="color:#7f8c8d;font-size:.65rem">{st}</span>')
        cp  = fmt_price_symbol(sym, pos.get("current_price", 0))
        if st == "open":
            lp = fmt_price_symbol(sym, pos.get("entry", 0))
            tp = "—"
        else:
            lp = fmt_price_symbol(sym, pos.get("limit_price", 0))
            tp = fmt_price_symbol(sym, pos.get("trigger_price", 0))
        is_trailing_pos = bool(pos.get("trailing"))
        if is_trailing_pos:
            sl = "🟡 " + fmt_price_symbol(sym, trail_stop_price(pos.get("peak", 0), pos.get("atr")))
        else:
            sl = fmt_price_symbol(sym, pos.get("stop", 0))
        t1  = fmt_price_symbol(sym, pos.get("tp1", 0))
        try:
            ot = datetime.fromisoformat(pos["open_time"]).replace(tzinfo=timezone.utc)
            elapsed = now_dt - ot
            h, rem = divmod(int(elapsed.total_seconds()), 3600)
            elapsed_str = f"{h}s {rem//60}d"
            if is_trailing_pos:
                # Trailing'e geçmiş pozisyon expire'dan muaf — saat sınırı yok
                rem_str = '<span style="color:#2ecc71">trailing (süre yok)</span>'
            else:
                rem_h = max(0, OPEN_EXPIRE_H - h) if st == "open" else max(0, 48 - h)
                rem_color = "#e74c3c" if rem_h < 6 else "#f39c12" if rem_h < 12 else "#7f8c8d"
                rem_str = f'<span style="color:{rem_color}">{rem_h}s kalan</span>'
        except Exception:
            elapsed_str = "—"
            rem_str = "—"
        sym_disp = sym.replace("USDT", "") + "/USDT"
        rows += f"""<tr>
            <td style="font-weight:bold;color:#e8eaf6">{sym_disp}</td>
            <td>{badge}</td>
            <td style="color:#ffffff;font-weight:bold">{cp if cp != "0" else "—"}</td>
            <td style="color:#00b4d8">{lp}</td>
            <td style="color:#f39c12">{tp}</td>
            <td style="color:#e74c3c">{sl}</td>
            <td style="color:#2ecc71">{t1}</td>
            <td style="font-size:.7rem;color:#7f8c8d">{elapsed_str}</td>
            <td>{rem_str}</td>
            <td><button onclick="deleteTrade('{sym}',this)"
                style="background:#e74c3c22;color:#e74c3c;border:1px solid #e74c3c55;
                border-radius:4px;padding:3px 10px;font-size:.65rem;cursor:pointer;font-family:inherit">
                Sil</button></td>
        </tr>"""

    if not rows:
        rows = '<tr><td colspan="10" style="text-align:center;color:#7f8c8d;padding:20px">Aktif pozisyon yok</td></tr>'

    error_banner = (f'<div style="background:#e74c3c22;border:1px solid #e74c3c55;color:#e74c3c;'
                    f'border-radius:6px;padding:8px 14px;margin-bottom:16px;font-size:.72rem">'
                    f'⚠ Trading-bot bağlantı hatası: {error_msg}</div>') if error_msg else ""

    count = len(trade_positions)
    monitoring_n = sum(1 for p in trade_positions.values() if p.get("status") == "monitoring")
    pending_n    = sum(1 for p in trade_positions.values() if p.get("status") == "pending")
    open_n       = sum(1 for p in trade_positions.values() if p.get("status") == "open")

    return f"""<!DOCTYPE html>
<html lang="tr"><head>
<meta charset="UTF-8"><title>Al-Sat Bot</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="30">
<style>
:root{{--bg:#0a0e14;--card:#0f1319;--border:#1e2a3a;--text:#c9d1d9;--text-dim:#7f8c8d;
  --accent:#00b4d8;--green:#2ecc71;--red:#e74c3c;--orange:#f39c12;}}
*{{box-sizing:border-box;margin:0;padding:0;}}
body{{background:var(--bg);color:var(--text);font-family:'JetBrains Mono','Fira Code','Consolas',monospace;
  padding:20px;max-width:1100px;margin:0 auto;line-height:1.5;}}
.header{{display:flex;justify-content:space-between;align-items:center;margin-bottom:24px;
  padding-bottom:16px;border-bottom:1px solid var(--border);}}
.header h1{{color:var(--accent);font-size:1.1rem;letter-spacing:3px;}}
.header .time{{color:var(--text-dim);font-size:.75rem;display:flex;align-items:center;gap:10px;}}
.btn-refresh{{background:#1a472a;color:#2ecc71;border:1px solid #2ecc7166;border-radius:4px;
  padding:3px 10px;font-size:.65rem;cursor:pointer;font-family:inherit;}}
.nav-tab{{background:#0f1319;border:1px solid var(--border);color:var(--text-dim);padding:3px 14px;
  border-radius:4px;text-decoration:none;font-size:.65rem;letter-spacing:.8px;transition:all .15s;}}
.nav-tab:hover,.nav-tab.active{{border-color:var(--accent);color:var(--accent);background:#00b4d811;}}
.cards{{display:grid;grid-template-columns:repeat(7,1fr);gap:10px;margin-bottom:24px;}}
.card{{background:var(--card);border:1px solid var(--border);border-radius:6px;padding:14px;text-align:center;}}
.card .val{{font-size:1.3rem;font-weight:bold;display:block;margin-bottom:4px;}}
.card .lbl{{font-size:.55rem;color:var(--text-dim);text-transform:uppercase;letter-spacing:1px;}}
table{{width:100%;border-collapse:collapse;font-size:.73rem;}}
th{{background:var(--card);color:var(--text-dim);padding:8px 10px;text-align:left;
  font-size:.6rem;letter-spacing:.8px;border-bottom:1px solid var(--border);}}
td{{padding:8px 10px;border-bottom:1px solid #111820;}}
tr:hover td{{background:#0f151d;}}
.table-wrap{{overflow-x:auto;border:1px solid var(--border);border-radius:6px;}}
.note{{color:var(--text-dim);font-size:.65rem;margin-bottom:12px;font-style:italic;}}
@media(max-width:700px){{.cards{{grid-template-columns:repeat(3,1fr);}}body{{padding:12px;}}}}
</style></head>
<body>
<div class="header">
  <div>
    <h1>🤖 AL-SAT BOT</h1>
    <div style="display:flex;gap:6px;margin-top:6px">
      <a href="/" class="nav-tab">Portföy</a>
      <a href="/market" class="nav-tab">Piyasa</a>
      <a href="/alsat" class="nav-tab active">Al-Sat Bot</a>
    </div>
  </div>
  <span class="time">{now}
    <button class="btn-refresh" onclick="location.reload()">🔄 Yenile</button>
  </span>
</div>

{error_banner}

<div class="cards">
  <div class="card"><span class="val" style="color:var(--accent)">{count}</span><span class="lbl">Toplam</span></div>
  <div class="card"><span class="val" style="color:var(--orange)">{monitoring_n}</span><span class="lbl">İzleme</span></div>
  <div class="card"><span class="val" style="color:#3498db">{pending_n}</span><span class="lbl">Emir</span></div>
  <div class="card"><span class="val" style="color:var(--green)">{open_n}</span><span class="lbl">Açık</span></div>
  <div class="card"><span class="val" style="color:var(--green)">{balance_val}</span><span class="lbl">Kullanılabilir USDT</span></div>
  <div class="card"><span class="val" style="color:#3498db">{positions_value_val}</span><span class="lbl">İşlemdeki Tutar</span></div>
  <div class="card"><span class="val" style="color:#ecf0f1">{total_value_val}</span><span class="lbl">Toplam (Tahmini)</span></div>
</div>

<p class="note">İzleme: fiyat CHoCH+3tick'e gelince limit emir açılır (CHoCH+1tick). Emir: Binance'te limit buy bekliyor. Sil butonu sadece state'den siler — Binance emrini kendin iptal et. 30s otomatik yenileme.</p>

<div class="table-wrap"><table><thead><tr>
  <th>Sembol</th><th>Durum</th><th>Anlık Fiyat</th><th>Limit Buy</th><th>Tetikleyici</th>
  <th>Stop</th><th>TP1</th><th>Geçen</th><th>Kalan</th><th></th>
</tr></thead><tbody>
  {rows}
</tbody></table></div>

<script>
function deleteTrade(sym,btn){{
  if(!confirm(sym+' pozisyonu state\\'den silinsin mi?\\n(Binance emri varsa kendin iptal et)'))return;
  btn.disabled=true;btn.textContent='...';
  fetch('/api/trade-positions/'+sym+'/delete',{{method:'POST'}})
    .then(r=>r.json()).then(d=>{{
      if(d.ok){{btn.closest('tr').remove();}}
      else{{btn.textContent='Hata';btn.style.color='#e74c3c';}}
    }}).catch(()=>{{btn.textContent='Hata';}});
}}
</script>
</body></html>"""


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
        payload = {"message": f"archive {tr_now_str()}", "content": encoded, "branch": "main"}
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
    print("📊 Portföy Takip Sistemi v3.0", flush=True)
    print("   SMC CHoCH ROC: CHoCH+1tick limit → retest 48H → fill sonrası SL | TP1 → ATR×0.6 trailing", flush=True)
    print("   PUMP: hard SL | hard TP | 6h expire | trailing yok", flush=True)
    print("=" * 50, flush=True)
    print(f"  Kontrol aralığı      : {CHECK_INTERVAL}s ({CHECK_INTERVAL // 60} dk)", flush=True)
    print(f"  PUMP expire süresi   : {BOT_EXPIRE_H['pump']} saat", flush=True)
    print(f"  Data dizini          : {DATA_DIR}", flush=True)
    print("=" * 50, flush=True)

    load_signals()
    threading.Thread(target=_sync_from_trading_bot, daemon=True, name="startup-sync").start()
    threading.Thread(target=position_checker_loop, daemon=True).start()
    threading.Thread(target=snapshot_loop, daemon=True, name="github_snapshot").start()
    start_news_watcher()
    start_market_analyzer()
    # Piyasa İzleme, sinyal başına çalışan Analyzer'dan bağımsızdır. F&G/BTC
    # değişim uyarıları eskisi gibi devam eder; AUTO_ANALYZER_ENABLED yalnız
    # `/api/analyze` üzerinden gelen otomatik coin yorumlarını yönetir.
    _start_market_watcher()
    threading.Thread(
        target=_manual_analyzer_poll_loop, daemon=True, name="manual-analyzer-poller"
    ).start()
    start_intraday_scanner()

    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, use_reloader=False)
