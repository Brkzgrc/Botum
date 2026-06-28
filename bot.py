# -*- coding: utf-8 -*-
"""
pump_scanner_v7_20260627.py
════════════════════════════════════════════════════════
PUMP SİSTEMİ — TEK SİNYAL, ÇOK ZAMAN DİLİMİ

Koşullar (her 1h kapanışında kontrol edilir):
  1. 15m vol spike  : son 4x15m mumda max_vol / 20-bar medyan ≥ 15x
  2. 15m getiri     : son 4x15m mumdaki max getiri ≥ 5% ve < 20%
  3. 1h vol spike   : 1h vol / 20-bar medyan ≥ 5x
  4. 4h trend       : close_4h > MA50(4h)
  5. 4h ROC         : ROC(4h close, 4 bar) ≥ 24%

Stop: -5% | TP: +20% | Max Hold: 24h | Cooldown: 24h
Backtest WR: ~%91 | Beklenti: ~+19% / işlem
════════════════════════════════════════════════════════
"""

import asyncio
import json
import os
import threading
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import ccxt
import numpy as np
import pandas as pd
import requests
import websockets
from flask import Flask

# ============================================================
# AYARLAR
# ============================================================
BINANCE_API_KEY    = os.getenv("BINANCE_API_KEY",    "")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")
TELEGRAM_TOKEN     = os.getenv("TELEGRAM_TOKEN",     "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID",   "")
PORTFOLIO_URL      = os.getenv("PORTFOLIO_URL",       "")
PORTFOLIO_TOKEN    = os.getenv("PORTFOLIO_TOKEN",     "")
ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY",  "")

# PUMP sinyal parametreleri
PUMP_VR15_MIN    = 15.0    # 15m vol spike — 20-bar medyan katsayısı
PUMP_RET15_MIN   = 5.0     # 15m getiri alt sınır %
PUMP_RET15_MAX   = 20.0    # 15m getiri üst sınır % (filtre)
PUMP_VR1H_MIN    = 5.0     # 1h vol spike — 20-bar medyan katsayısı
PUMP_MA50_BARS   = 50      # 4h MA50 periyodu
PUMP_ROC_PERIOD  = 4       # 4h ROC bar sayısı
PUMP_ROC_MIN     = 24.0    # 4h ROC minimum %
PUMP_TP_PCT      = 20.0    # Take profit %
PUMP_SL_PCT      = 5.0     # Stop loss %
PUMP_EXPIRE_H    = 24      # Pozisyon expire süresi (saat)
PUMP_COOLDOWN_H  = int(os.getenv("PUMP_COOLDOWN_H", "24"))  # Cooldown (saat)

# Genel
MIN_LIQUIDITY    = 500_000   # Minimum günlük hacim (USDT) — sembol havuzu için
MAX_SYMBOLS      = int(os.getenv("MAX_SYMBOLS",      "0"))
TRAILING_PCT     = float(os.getenv("TRAILING_PCT",   "0.03"))   # %3 trailing stop
TRAILING_MIN_GAIN = float(os.getenv("TRAILING_MIN_GAIN", "0.0"))
WS_STREAM_CHUNK  = int(os.getenv("WS_STREAM_CHUNK",  "120"))
BOOTSTRAP_BARS   = int(os.getenv("BOOTSTRAP_BARS",   "750"))
KEEP_BARS        = int(os.getenv("KEEP_BARS",         "720"))
VOL_PERIOD       = 20

TR_TZ = timezone(timedelta(hours=3))

IGNORED_COINS = {
    "UP/USDT", "DOWN/USDT", "BEAR/USDT", "BULL/USDT",
    "USDC/USDT", "TUSD/USDT", "FDUSD/USDT", "DAI/USDT", "USDP/USDT",
    "USDE/USDT", "UST/USDT", "USD/USDT", "XUSD/USDT", "USD1/USDT", "BFUSD/USDT",
    "USTC/USDT", "BUSD/USDT", "FRAX/USDT", "LUSD/USDT", "GUSD/USDT", "SUSD/USDT",
    "USDS/USDT", "USDX/USDT", "USDD/USDT", "CUSD/USDT", "OUSD/USDT", "MUSD/USDT",
    "RLUSD/USDT", "U/USDT",
    "EUR/USDT", "TRY/USDT", "GBP/USDT", "BRL/USDT", "RUB/USDT",
    "AUD/USDT", "BIDR/USDT", "IDRT/USDT", "VAI/USDT",
    "PAXG/USDT", "XAUT/USDT",
    "WBTC/USDT", "WETH/USDT", "WBNB/USDT", "BETH/USDT",
    "BTCB/USDT", "HBTC/USDT",
    "BTC/USDT",
}
LEVERAGED_PATTERNS = ["UP", "DOWN", "BULL", "BEAR", "3L", "3S", "2L", "2S", "5L", "5S", "10L", "10S"]

# ============================================================
# GLOBAL DURUM
# ============================================================
stats           = Counter()
ws_1h_closes    = 0
tracked_symbols = []
signal_counter  = 0

bars_1h:   dict = {}
bars_15m:  dict = {}
bars_4h:   dict = {}
bars_1d:   dict = {}
last_pump_ts:   dict = {}
all_signals:    list = []
heartbeat = {"last": "", "epoch": time.time(), "symbol": "?"}
bot_status = {"status": "BOOT"}
_recent_signal_times:    list = []
_secondary_bootstrap_done = False

def tr_now():
    return datetime.now(timezone.utc).astimezone(TR_TZ)

def tr_now_str():
    return tr_now().strftime("%Y-%m-%d %H:%M:%S")

# ============================================================
# API GATE
# ============================================================
class ApiGate:
    def __init__(self, min_interval=0.25, max_concurrent=3, max_retries=6):
        self.min_interval = min_interval
        self.sem          = asyncio.Semaphore(max_concurrent)
        self.max_retries  = max_retries
        self._lock        = asyncio.Lock()
        self._last_ts     = 0.0

    async def _space(self):
        async with self._lock:
            wait = (self._last_ts + self.min_interval) - time.time()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_ts = time.time()

    async def call(self, fn, *args, **kwargs):
        async with self.sem:
            for attempt in range(self.max_retries):
                try:
                    await self._space()
                    loop = asyncio.get_running_loop()
                    return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))
                except (ccxt.RateLimitExceeded, ccxt.DDoSProtection):
                    await asyncio.sleep(min(30.0, 1.0 * (2 ** attempt)))
                except (ccxt.NetworkError, ccxt.RequestTimeout, ccxt.ExchangeNotAvailable):
                    await asyncio.sleep(min(20.0, 0.8 * (2 ** attempt)))
                except Exception:
                    await asyncio.sleep(min(8.0, 0.5 * (2 ** attempt)))
            raise RuntimeError("API call failed after retries")

api_gate = ApiGate()

exchange_spot = ccxt.binance({
    "apiKey":  BINANCE_API_KEY    or None,
    "secret":  BINANCE_API_SECRET or None,
    "options": {"defaultType": "spot", "adjustForTimeDifference": True},
    "enableRateLimit": True,
    "timeout": 15000,
})

# ============================================================
# SEMBOL HAVUZU
# ============================================================
async def load_symbols_pool():
    await api_gate.call(exchange_spot.load_markets)
    syms = [
        s for s, m in exchange_spot.markets.items()
        if s.endswith("/USDT")
        and m.get("active") and m.get("spot")
        and s not in IGNORED_COINS
        and not any(s.replace("/USDT", "").endswith(p) for p in LEVERAGED_PATTERNS)
    ]
    volumes = {}
    for i in range(0, len(syms), 100):
        try:
            res = await api_gate.call(exchange_spot.fetch_tickers, syms[i:i+100])
            for k, v in (res or {}).items():
                vol = float(v.get("quoteVolume", 0) or 0)
                if vol > 0:
                    volumes[k] = vol
        except Exception:
            pass
        await asyncio.sleep(0.5)
    missing = [s for s in syms if volumes.get(s, 0) == 0]
    for sym in missing[:50]:
        try:
            ticker = await api_gate.call(exchange_spot.fetch_ticker, sym)
            vol = float(ticker.get("quoteVolume", 0) or 0)
            if vol > 0:
                volumes[sym] = vol
        except Exception:
            pass
        await asyncio.sleep(0.05)
    filtered = sorted(
        [s for s in syms if volumes.get(s, 0) >= MIN_LIQUIDITY],
        key=lambda x: volumes.get(x, 0), reverse=True
    )
    print(f"Sembol: {len(syms)} → {len(filtered)} (min {MIN_LIQUIDITY/1e6:.1f}M USDT)", flush=True)
    return filtered[:MAX_SYMBOLS] if MAX_SYMBOLS else filtered

# ============================================================
# VERİ ÇEKME
# ============================================================
async def fetch_df(symbol, timeframe, limit):
    raw = await api_gate.call(exchange_spot.fetch_ohlcv, symbol, timeframe=timeframe, limit=limit)
    df  = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df.set_index("timestamp", inplace=True)
    return df

# ============================================================
# İNDİKATÖRLER
# ============================================================
def prepare_bars(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    c, h, l, v = df["close"], df["high"], df["low"], df["volume"]

    df["vol_ma"]     = v.rolling(VOL_PERIOD).mean()
    tr               = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    df["atr"]        = tr.ewm(alpha=1/14, adjust=False).mean()
    df["atr_pct"]    = df["atr"] / c * 100
    df["ema50"]      = c.ewm(span=50,  adjust=False).mean()
    df["ema200"]     = c.ewm(span=200, adjust=False).mean()
    df["close_prev"] = c.shift(1)

    return df.dropna(subset=["vol_ma", "atr", "close_prev"])

# ============================================================
# PUMP SİNYAL SİSTEMİ
# ============================================================
def calc_roc(close_arr, period=4):
    """Rate of Change: (curr - prev_N) / prev_N * 100"""
    if len(close_arr) <= period:
        return None
    prev = float(close_arr[-(period + 1)])
    curr = float(close_arr[-1])
    if prev <= 0:
        return None
    return (curr - prev) / prev * 100


def check_pump_signal(symbol: str) -> dict | None:
    """
    5 koşul kontrolü:
    1. 15m vol spike >= 15x (son 4 mumdaki max / 20-bar medyan)
    2. 15m getiri >= 5% ve < 20% (son 4 mumda)
    3. 1h vol >= 5x medyan
    4. close_4h > MA50(4h)
    5. ROC(4h, 4 bar) >= 24%
    """
    df15 = bars_15m.get(symbol)
    df1h = bars_1h.get(symbol)
    df4h = bars_4h.get(symbol)

    if df15 is None or len(df15) < 25:
        return None
    if df1h is None or len(df1h) < 22:
        return None
    if df4h is None or len(df4h) < PUMP_MA50_BARS + 2:
        return None

    # --- Koşul 1: 15m vol spike ---
    vol15_last4  = df15["volume"].iloc[-4:].values.astype(float)
    max_vol15    = float(np.max(vol15_last4))
    baseline15   = df15["volume"].iloc[-24:-4].values.astype(float)
    if len(baseline15) < 16:
        return None
    med_vol15 = float(np.median(baseline15))
    if med_vol15 <= 0:
        return None
    vr15 = max_vol15 / med_vol15
    if vr15 < PUMP_VR15_MIN:
        return None

    # --- Koşul 2: 15m getiri ---
    open15_first = float(df15["open"].iloc[-4])
    max_high15   = float(df15["high"].iloc[-4:].max())
    if open15_first <= 0:
        return None
    ret15 = (max_high15 / open15_first - 1) * 100
    if not (PUMP_RET15_MIN <= ret15 < PUMP_RET15_MAX):
        return None

    # --- Koşul 3: 1h vol spike ---
    vol1h_last    = float(df1h["volume"].iloc[-1])
    vol1h_base    = df1h["volume"].iloc[-21:-1].values.astype(float)
    if len(vol1h_base) < 10:
        return None
    med_vol1h = float(np.median(vol1h_base))
    if med_vol1h <= 0:
        return None
    vr1h = vol1h_last / med_vol1h
    if vr1h < PUMP_VR1H_MIN:
        return None

    # --- Koşul 4: 4h trend (close > MA50) ---
    close4h  = df4h["close"].values.astype(float)
    ma50_4h  = float(np.mean(close4h[-PUMP_MA50_BARS:]))
    curr4h   = close4h[-1]
    if curr4h <= ma50_4h:
        return None

    # --- Koşul 5: 4h ROC ---
    roc_4h = calc_roc(close4h, period=PUMP_ROC_PERIOD)
    if roc_4h is None or roc_4h < PUMP_ROC_MIN:
        return None

    # Tüm koşullar geçti
    entry = float(df1h["close"].iloc[-1])
    if entry <= 0:
        return None
    stop = round(entry * (1 - PUMP_SL_PCT / 100), 8)
    tp   = round(entry * (1 + PUMP_TP_PCT / 100), 8)

    return {
        "type":    "pump",
        "symbol":  symbol,
        "entry":   round(entry, 8),
        "stop":    stop,
        "tp1":     tp,
        "tp2":     tp,   # tp2 = tp1 → portfolio tracker'da otomatik kapatma tetikler
        "vr15":    round(vr15, 2),
        "ret15":   round(ret15, 2),
        "vr1h":    round(vr1h, 2),
        "ma50_4h": round(ma50_4h, 4),
        "roc_4h":  round(roc_4h, 2),
        "candle":  "1h",
    }

# ============================================================
# YARDIMCI FONKSİYONLAR
# ============================================================
def fmt_price(price):
    if price is None: return "?"
    p = float(price)
    if p >= 100:    return f"{p:.2f}"
    if p >= 1:      return f"{p:.3f}"
    if p >= 0.01:   return f"{p:.4f}"
    if p >= 0.0001: return f"{p:.6f}"
    return f"{p:.8f}"

def _sep():
    return "━━━━━━━━━━━━━━━━━━━━"

# ============================================================
# TELEGRAM MESAJ OLUŞTURUCU
# ============================================================
def build_pump_message(r, tr_time, sig_num):
    sym = r["symbol"].replace("/USDT", "")
    e   = r["entry"]
    lines = [
        f"🕐 {tr_time.strftime('%d/%m/%Y %H:%M')}",
        "",
        f"🚀 <b>#{sym}/USDT  •  PUMP  •  1H</b>",
        _sep(),
        f"💵 <b>Giriş</b>    {fmt_price(e)}",
        f"🛑 <b>Stop</b>     {fmt_price(r['stop'])}  (-{PUMP_SL_PCT:.0f}%)",
        f"🎯 <b>Hedef</b>    {fmt_price(r['tp1'])}  (+{PUMP_TP_PCT:.0f}%)",
        _sep(),
        "📊 <b>Göstergeler</b>",
        f"📈 15m Spike   : <b>{r['vr15']:.1f}x</b>",
        f"📊 15m Hareket : +{r['ret15']:.1f}%",
        f"💧 1h Hacim    : {r['vr1h']:.1f}x medyan",
        f"🔮 4h ROC      : +{r['roc_4h']:.1f}% (4 bar)",
        _sep(),
        f"⏱ WR: ~%91  |  Hold: 24h  |  #{sig_num} sinyal",
    ]
    return "\n".join(lines)

# ============================================================
# GÖNDERIM
# ============================================================
def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text,
                  "parse_mode": "HTML", "disable_web_page_preview": True,
                  "message_thread_id": 5},
            timeout=10,
        )
        if r.status_code != 200:
            print(f"Telegram {r.status_code}: {r.text[:80]}", flush=True)
    except Exception as e:
        print(f"Telegram hata: {e}", flush=True)

def _notify_trailing_activated(entry, old_stop):
    try:
        sym      = entry["symbol"].replace("/USDT", "")
        e        = entry["entry"]
        old_pct  = round((old_stop / e - 1) * 100, 1)
        new_pct  = round((entry["stop"] / e - 1) * 100, 1)
        peak_pct = entry["peak_pct"]
        msg = (
            f"🔄 <b>Trailing Stop Devreye Girdi</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"#{sym}/USDT  [PUMP]\n"
            f"Peak: <b>+{peak_pct:.1f}%</b>\n"
            f"Eski Stop: {old_pct:+.1f}%  →  Yeni Stop: <b>{new_pct:+.1f}%</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━"
        )
        send_telegram(msg)
    except Exception as ex:
        print(f"[TRAILING] Bildirim hata: {ex}", flush=True)

def _notify_trailing_close(entry, close_price, close_ret):
    try:
        sym = entry["symbol"].replace("/USDT", "")
        msg = (
            f"✅ <b>Trailing Stop — Kâr Kapatıldı</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"#{sym}/USDT  [PUMP]\n"
            f"Giriş: {fmt_price(entry['entry'])}  →  Çıkış: {fmt_price(close_price)}\n"
            f"Kâr: <b>+{close_ret:.1f}%</b>  |  Peak: +{entry.get('peak_pct', 0):.1f}%\n"
            f"━━━━━━━━━━━━━━━━━━━━"
        )
        send_telegram(msg)
    except Exception as ex:
        print(f"[TRAILING CLOSE] Bildirim hata: {ex}", flush=True)

# sig_type → portfolio type eşleştirmesi
_SIG_TYPE_MAP = {
    "pump": "pump",
}

def send_to_portfolio(result):
    if not PORTFOLIO_URL: return ""
    try:
        sig_type = _SIG_TYPE_MAP.get(result.get("type", "pump"), "pump")
        payload = {
            "symbol":      result["symbol"],
            "entry":       result["entry"],
            "stop":        result["stop"],
            "tp1":         result.get("tp1"),
            "tp2":         result.get("tp2"),
            "sig_type":    sig_type,
            "sub_type":    "",
            "source":      "bot",
            "candle":      result.get("candle", ""),
            "funding_neg": False,
        }
        headers = {"Content-Type": "application/json"}
        if PORTFOLIO_TOKEN:
            headers["Authorization"] = f"Bearer {PORTFOLIO_TOKEN}"
        r = requests.post(f"{PORTFOLIO_URL}/api/signal",
                          json=payload, headers=headers, timeout=5)
        if r.status_code == 201:
            sig_id = r.json().get("id", "")
            print(f"[PORTFOLIO] Sinyal gönderildi: {result['symbol']} ({sig_type}) id={sig_id}", flush=True)
            return sig_id
        elif r.status_code == 409:
            print(f"[PORTFOLIO] Zaten açık: {result['symbol']}", flush=True)
        else:
            print(f"[PORTFOLIO] HTTP {r.status_code}: {r.text[:80]}", flush=True)
    except Exception as e:
        print(f"[PORTFOLIO] Hata: {e}", flush=True)
    return ""

# ============================================================
# CLAUDE ANALYZER — HTTP WRAPPER
# ============================================================
def _analyzer_safe(signal: dict, recent_count: int, sig_num: int, portfolio_id: str):
    if not PORTFOLIO_URL:
        return
    sym = signal.get("symbol", "?")
    for attempt in range(2):
        try:
            r = requests.post(
                f"{PORTFOLIO_URL}/api/analyze",
                json={"signal": signal, "recent_count": recent_count,
                      "sig_num": sig_num, "portfolio_id": portfolio_id},
                headers={"Authorization": f"Bearer {PORTFOLIO_TOKEN}"},
                timeout=30,
            )
            if r.status_code not in (200, 202):
                print(f"[ANALYZER] HTTP {r.status_code}: {r.text[:80]}", flush=True)
            return
        except Exception as e:
            if attempt == 0:
                print(f"[ANALYZER] #{sym} hata (deneme 1), 5s sonra tekrar: {e}", flush=True)
                time.sleep(5)
            else:
                print(f"[ANALYZER] #{sym} başarısız (2 deneme): {e}", flush=True)

def _recent_signal_count() -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
    _recent_signal_times[:] = [t for t in _recent_signal_times if t > cutoff]
    return len(_recent_signal_times)

def _record_signal_time():
    _recent_signal_times.append(datetime.now(timezone.utc))

# ============================================================
# PERFORMANS TAKİP
# ============================================================
SIGNAL_LOG_PATH = os.path.join(os.getenv("DATA_DIR", "/tmp"), "signal_log.json")
_EXPIRE_H = {"pump": 24}

def load_signal_log():
    try:
        with open(SIGNAL_LOG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []

def save_signal_log(log):
    try:
        with open(SIGNAL_LOG_PATH, "w", encoding="utf-8") as f:
            json.dump(log[-500:], f, ensure_ascii=False, default=str)
    except Exception as e:
        print(f"signal_log kayit hata: {e}", flush=True)

signal_log        = load_signal_log()
pending_by_symbol = {}
_last_periodic_save = 0.0

def log_signal(result, tr_time):
    sig_type = result.get("type", "pump")
    entry_rec = {
        "id":       f"{result['symbol']}_{sig_type}_{int(tr_time.timestamp())}",
        "symbol":   result["symbol"],
        "sig_type": _SIG_TYPE_MAP.get(sig_type, "pump"),
        "raw_type": sig_type,
        "entry":    result["entry"],
        "stop":     result["stop"],
        "tp1":      result.get("tp1"),
        "tp2":      result.get("tp2"),
        "expire_h": _EXPIRE_H.get(sig_type, 24),
        "candle":   result.get("candle", ""),
        "time":     tr_time.isoformat(),
        "status":   "open",
        "peak_pct": 0.0, "tp1_hit": False, "tp2_hit": False, "tp3_hit": False,
        "trailing_active": False, "trailing_stop": None,
        "close_time": None, "close_price": None, "close_ret": None,
        "vr15":   result.get("vr15"),
        "vr1h":   result.get("vr1h"),
        "roc_4h": result.get("roc_4h"),
    }
    signal_log.insert(0, entry_rec)
    if len(signal_log) > 500: signal_log.pop()
    save_signal_log(signal_log)
    pending_by_symbol.setdefault(result["symbol"], []).append(entry_rec)

def rebuild_pending():
    for s in signal_log:
        if s["status"] == "open":
            pending_by_symbol.setdefault(s["symbol"], []).append(s)

def check_pending_for_symbol(symbol, bar_high, bar_low, bar_close, bar_time):
    if symbol not in pending_by_symbol: return
    now      = datetime.now(timezone.utc)
    to_close = []
    for entry in pending_by_symbol[symbol]:
        e   = entry["entry"]; stp = entry["stop"]
        sig_time = datetime.fromisoformat(entry["time"])
        if sig_time.tzinfo is None:
            sig_time = sig_time.replace(tzinfo=timezone.utc)
        elapsed_h = (now - sig_time).total_seconds() / 3600
        expire_h  = entry.get("expire_h", 24)

        cur_ret = (bar_high - e) / e * 100
        if cur_ret > entry["peak_pct"]:
            entry["peak_pct"] = round(cur_ret, 2)

        # Trailing stop güncelleme
        if entry.get("trailing_active") or entry["peak_pct"] >= TRAILING_MIN_GAIN:
            peak_price   = e * (1 + entry["peak_pct"] / 100)
            new_trailing = round(peak_price * (1 - TRAILING_PCT), 8)
            if new_trailing > entry["stop"]:
                was_active = entry.get("trailing_active", False)
                old_stop   = entry["stop"]
                entry["stop"]            = new_trailing
                entry["trailing_stop"]   = new_trailing
                entry["trailing_active"] = True
                stp = new_trailing
                if not was_active:
                    _notify_trailing_activated(entry, old_stop)

        tp1 = entry.get("tp1"); tp2 = entry.get("tp2"); tp3 = entry.get("tp3")
        if tp3 and bar_high >= tp3 and not entry.get("tp3_hit"): entry["tp3_hit"] = True
        if tp2 and bar_high >= tp2 and not entry.get("tp2_hit"): entry["tp2_hit"] = True
        if tp1 and bar_high >= tp1 and not entry.get("tp1_hit"): entry["tp1_hit"] = True

        if bar_low <= stp:
            close_ret_val = round((stp - e) / e * 100, 2)
            status = "win" if close_ret_val > 0 else "loss"
            entry.update({"status": status, "close_time": bar_time.isoformat(),
                          "close_price": round(stp, 8), "close_ret": close_ret_val})
            if status == "win" and entry.get("trailing_active"):
                _notify_trailing_close(entry, round(stp, 8), close_ret_val)
            to_close.append(entry); continue
        if tp2 and bar_high >= tp2:
            entry.update({"status": "win", "close_time": bar_time.isoformat(),
                          "close_price": round(tp2, 8), "close_ret": round((tp2-e)/e*100, 2)})
            to_close.append(entry); continue
        if elapsed_h >= expire_h:
            entry.update({"status": "expired", "close_time": bar_time.isoformat(),
                          "close_price": round(bar_close, 8), "close_ret": round((bar_close-e)/e*100, 2)})
            to_close.append(entry)

    if to_close:
        for en in to_close:
            pending_by_symbol[symbol].remove(en)
        if not pending_by_symbol[symbol]:
            del pending_by_symbol[symbol]
        save_signal_log(signal_log)
    else:
        global _last_periodic_save
        now_ts = time.time()
        if now_ts - _last_periodic_save >= 300:
            _last_periodic_save = now_ts
            save_signal_log(signal_log)

def perf_summary():
    closed = [s for s in signal_log if s["status"] in ("loss", "win", "expired")]
    def avg_peak(lst):
        peaks = [s["peak_pct"] for s in lst if s.get("peak_pct") is not None]
        return round(sum(peaks)/len(peaks), 2) if peaks else 0.0
    return {
        "total":    len(signal_log),
        "open":     sum(1 for s in signal_log if s["status"] == "open"),
        "win":      sum(1 for s in closed if s["status"] == "win"),
        "loss":     sum(1 for s in closed if s["status"] == "loss"),
        "expired":  sum(1 for s in closed if s["status"] == "expired"),
        "avg_peak": avg_peak(closed),
        "win_rate": round(
            sum(1 for s in closed if s["status"] == "win") / len(closed) * 100, 1
        ) if closed else 0.0,
    }

# ============================================================
# BOOTSTRAP
# ============================================================
async def bootstrap_symbol(symbol):
    try:
        df = await fetch_df(symbol, "1h", BOOTSTRAP_BARS)
        if df is None or len(df) < 60: return False
        df = prepare_bars(df)
        bars_1h[symbol] = df.iloc[-KEEP_BARS:] if len(df) > KEEP_BARS else df
        return True
    except Exception:
        return False

async def bootstrap_all(symbols):
    ok = 0
    print(f"Bootstrap: {len(symbols)} sembol", flush=True)
    for i, sym in enumerate(symbols, 1):
        if i % 50 == 0:
            print(f"  → {i}/{len(symbols)}", flush=True)
        if await bootstrap_symbol(sym):
            ok += 1
    print(f"Bootstrap bitti: {ok}/{len(symbols)}", flush=True)

async def bootstrap_secondary_tf(symbols):
    global _secondary_bootstrap_done
    print("Secondary bootstrap başlıyor (15m/4h/1d)...", flush=True)
    ok = 0
    all_syms = list(symbols) + ["BTC/USDT"]
    for i, sym in enumerate(all_syms, 1):
        try:
            df15 = await fetch_df(sym, "15m", 200)
            if df15 is not None and len(df15) >= 20:
                bars_15m[sym] = df15.iloc[-200:]
            df4h = await fetch_df(sym, "4h", 200)
            if df4h is not None and len(df4h) >= 20:
                bars_4h[sym] = df4h.iloc[-200:]
            df1d = await fetch_df(sym, "1d", 250)
            if df1d is not None and len(df1d) >= 20:
                bars_1d[sym] = df1d.iloc[-250:]
            ok += 1
        except Exception:
            pass
        if i % 50 == 0:
            print(f"  Secondary: {i}/{len(all_syms)}", flush=True)
        await asyncio.sleep(0.05)
    _secondary_bootstrap_done = True
    print(f"Secondary bootstrap bitti: {ok}/{len(all_syms)}", flush=True)

# ============================================================
# 4H/1D BAR YENİLEME (periyodik REST çekimi)
# ============================================================
async def refresh_4h_bars(symbols):
    ok = 0
    for sym in symbols:
        try:
            df = await fetch_df(sym, "4h", 3)
            if df is not None and len(df) >= 2:
                if sym in bars_4h:
                    combined = pd.concat([bars_4h[sym], df])
                    combined = combined[~combined.index.duplicated(keep="last")].sort_index().iloc[-200:]
                    bars_4h[sym] = combined
                else:
                    bars_4h[sym] = df.iloc[-200:]
                ok += 1
        except Exception:
            pass
        await asyncio.sleep(0.05)
    print(f"[REFRESH] 4H güncelleme: {ok}/{len(symbols)}", flush=True)

async def refresh_1d_bars(symbols):
    ok = 0
    for sym in symbols:
        try:
            df = await fetch_df(sym, "1d", 3)
            if df is not None and len(df) >= 2:
                if sym in bars_1d:
                    combined = pd.concat([bars_1d[sym], df])
                    combined = combined[~combined.index.duplicated(keep="last")].sort_index().iloc[-250:]
                    bars_1d[sym] = combined
                else:
                    bars_1d[sym] = df.iloc[-250:]
                ok += 1
        except Exception:
            pass
        await asyncio.sleep(0.05)
    print(f"[REFRESH] 1D güncelleme: {ok}/{len(symbols)}", flush=True)

# ============================================================
# SİNYAL WORKER
# ============================================================
@dataclass
class SignalCandidate:
    symbol:  str
    result:  dict
    tr_time: datetime

async def signal_worker(candidate_queue):
    global signal_counter
    while True:
        sig = await candidate_queue.get()
        try:
            symbol   = sig.symbol
            result   = sig.result
            tr_time  = sig.tr_time
            sig_type = result.get("type", "pump")

            # Açık pozisyon tekrar kontrolü
            pt = _SIG_TYPE_MAP.get(sig_type, "pump")
            if any(s["status"] == "open" and s["symbol"] == symbol and s["sig_type"] == pt
                   for s in signal_log):
                print(f"[SKIP] {symbol} — açık pozisyon mevcut, sinyal atlandı", flush=True)
                continue

            signal_counter += 1
            msg = build_pump_message(result, tr_time, signal_counter)
            print(f"SİNYAL 🚀 [PUMP] {symbol}"
                  f" | 15m:{result['vr15']:.1f}x +{result['ret15']:.1f}%"
                  f" | 1h:{result['vr1h']:.1f}x"
                  f" | 4h ROC:+{result['roc_4h']:.1f}%"
                  f" | giriş:{fmt_price(result['entry'])}", flush=True)

            send_telegram(msg)
            portfolio_id = send_to_portfolio(result)

            # Claude Analyzer
            if PORTFOLIO_URL and TELEGRAM_CHAT_ID:
                recent_cnt = _recent_signal_count()
                _record_signal_time()
                sig_snap = dict(result)
                sig_num  = signal_counter
                loop     = asyncio.get_running_loop()
                asyncio.create_task(
                    loop.run_in_executor(
                        None,
                        lambda s=sig_snap, r=recent_cnt, n=sig_num, pid=portfolio_id:
                            _analyzer_safe(s, r, n, pid)
                    )
                )

            # Cooldown güncelle
            last_pump_ts[symbol] = tr_time.replace(tzinfo=None)

            result["time"] = tr_time.strftime("%Y-%m-%d %H:%M")
            all_signals.insert(0, result)
            if len(all_signals) > 200: all_signals.pop()
            stats["signal_sent"] += 1
            log_signal(result, tr_time)

        except Exception as e:
            print(f"Worker hata: {str(e)[:100]}", flush=True)
        finally:
            candidate_queue.task_done()

# ============================================================
# MUM KAPANIŞINI İŞLE
# ============================================================
async def on_1h_close(symbol, o, h, l, c, v, ts_ms, candidate_queue):
    global ws_1h_closes
    ws_1h_closes += 1

    now_ts = time.time()
    if now_ts - heartbeat["epoch"] >= 10:
        heartbeat.update({"last": tr_now_str(), "epoch": now_ts, "symbol": symbol})

    bar_time = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    check_pending_for_symbol(symbol, h, l, c, bar_time)

    df = bars_1h.get(symbol)
    if df is None or len(df) < VOL_PERIOD + 5:
        stats["data_missing"] += 1; return

    tstamp = pd.to_datetime(ts_ms, unit="ms", utc=True)
    df.loc[tstamp, ["open", "high", "low", "close", "volume"]] = [o, h, l, c, v]
    df = df.sort_index()
    if len(df) > KEEP_BARS: df = df.iloc[-KEEP_BARS:]
    df = prepare_bars(df)
    bars_1h[symbol] = df

    tr_time = datetime.now(timezone.utc).astimezone(TR_TZ)

    # --- PUMP sinyali ---
    last_p  = last_pump_ts.get(symbol)
    pump_ok = True
    if last_p is not None:
        elapsed = (tr_time.replace(tzinfo=None) - last_p.replace(tzinfo=None)).total_seconds() / 3600
        if elapsed < PUMP_COOLDOWN_H:
            stats["cooldown"] += 1
            pump_ok = False
    if pump_ok:
        result = check_pump_signal(symbol)
        if result:
            await candidate_queue.put(SignalCandidate(symbol, result, tr_time))
        else:
            stats["filtered"] += 1

async def on_15m_close(symbol, o, h, l, c, v, ts_ms):
    if symbol not in bars_15m:
        return
    tstamp = pd.to_datetime(ts_ms, unit="ms", utc=True)
    df = bars_15m[symbol]
    df.loc[tstamp, ["open", "high", "low", "close", "volume"]] = [o, h, l, c, v]
    df = df.sort_index()
    if len(df) > 200:
        df = df.iloc[-200:]
    bars_15m[symbol] = df

# ============================================================
# WEBSOCKET
# ============================================================
def to_ws(symbol):
    return symbol.replace("/", "").lower()

async def ws_chunk(symbols, candidate_queue):
    stream_list = []
    for s in symbols:
        ws_sym = to_ws(s)
        stream_list.append(f"{ws_sym}@kline_1h")
        stream_list.append(f"{ws_sym}@kline_15m")
    streams = "/".join(stream_list)
    url     = f"wss://stream.binance.com:9443/stream?streams={streams}"
    retry   = 0
    while True:
        try:
            async with websockets.connect(url, ping_interval=None,
                                           open_timeout=30, close_timeout=10,
                                           max_size=10*1024*1024) as ws:
                retry = 0
                print(f"WS bağlandı ({len(symbols)} sembol)", flush=True)
                async def keep_alive(ws):
                    while True:
                        await asyncio.sleep(20)
                        try:
                            pong = await ws.ping()
                            await asyncio.wait_for(pong, timeout=10)
                        except Exception:
                            break
                ping_task = asyncio.create_task(keep_alive(ws))
                try:
                    while True:
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=60)
                        except asyncio.TimeoutError:
                            continue
                        data = json.loads(msg)
                        k    = data.get("data", {}).get("k", {})
                        if not k.get("x", False): continue
                        sym      = data.get("data", {}).get("s", "").upper().replace("USDT", "/USDT")
                        interval = k.get("i", "")
                        try:
                            if interval == "1h":
                                await on_1h_close(sym,
                                    float(k["o"]), float(k["h"]), float(k["l"]), float(k["c"]),
                                    float(k["v"]), int(k["t"]), candidate_queue)
                            elif interval == "15m":
                                await on_15m_close(sym,
                                    float(k["o"]), float(k["h"]), float(k["l"]), float(k["c"]),
                                    float(k["v"]), int(k["t"]))
                        except Exception as e:
                            print(f"ws_handler hata [{sym}/{interval}]: {str(e)[:80]}", flush=True)
                finally:
                    ping_task.cancel()
        except Exception as e:
            retry  += 1
            backoff = min(60, 5 * (2 ** min(retry, 4)))
            print(f"WS koptu → {backoff}s: {str(e)[:50]}", flush=True)
            await asyncio.sleep(backoff)

async def ws_all(symbols, candidate_queue):
    tasks = [
        asyncio.create_task(ws_chunk(symbols[i:i+WS_STREAM_CHUNK], candidate_queue))
        for i in range(0, len(symbols), WS_STREAM_CHUNK)
    ]
    await asyncio.gather(*tasks)

# ============================================================
# FLASK DASHBOARD
# ============================================================
import logging
flask_app = Flask(__name__)
logging.getLogger("werkzeug").setLevel(logging.ERROR)
flask_app.logger.disabled = True

def clean_json(obj):
    if isinstance(obj, dict):  return {k: clean_json(v) for k, v in obj.items()}
    if isinstance(obj, list):  return [clean_json(i) for i in obj]
    if isinstance(obj, float):
        if obj != obj or obj in (float("inf"), float("-inf")): return None
        return obj
    if hasattr(obj, "item"): return clean_json(obj.item())
    return obj

_TYPE_LABEL = {
    "pump":  ("🚀", "PUMP", "#3fb950"),
}

@flask_app.route("/")
def home():
    now = tr_now().strftime("%H:%M:%S")
    sig_rows = ""
    for s in all_signals[:40]:
        stype = s.get("type", "pump")
        icon, label, bc = _TYPE_LABEL.get(stype, ("🚀", "PUMP", "#3fb950"))
        e = s.get("entry"); stop = s.get("stop"); tp1 = s.get("tp1")
        vr15_v = s.get("vr15", 0); vr1h_v = s.get("vr1h", 0); roc_v = s.get("roc_4h", 0)
        ind = f"15m:{vr15_v:.1f}x  +{s.get('ret15',0):.1f}%  |  1h:{vr1h_v:.1f}x  |  4h ROC:+{roc_v:.1f}%"
        sig_rows += (
            f'<div class="sig" style="border-color:{bc}">'
            f'<div class="sr"><b>{icon} {s.get("symbol","")} <small>[{label}]</small></b>'
            f'<span class="ts">{s.get("time","")[:16]}</span></div>'
            f'<div class="sd">💵 {fmt_price(e)}  🛑 {fmt_price(stop)}  🎯 {fmt_price(tp1)}</div>'
            f'<div class="sd">{ind}</div>'
            f'</div>'
        )
    ps = perf_summary()
    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Pump Scanner v7.0</title>
<meta http-equiv="refresh" content="30">
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#06090d;color:#b8cdd8;font-family:'Courier New',monospace;padding:20px;max-width:980px;margin:0 auto}}
h1{{color:#00d4ff;letter-spacing:4px;font-size:1.1rem;margin-bottom:16px}}
.info{{background:#0c1117;border:1px solid #1c2a36;padding:8px 14px;border-radius:4px;margin-bottom:16px;font-size:.72rem;color:#3d5a6a;line-height:1.8}}
.stats{{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:20px}}
.stat{{background:#0c1117;border:1px solid #1c2a36;padding:9px 14px;border-radius:4px;min-width:80px}}
.sv{{font-size:1.1rem;color:#00d4ff;display:block;font-weight:bold}}
.sl{{font-size:.58rem;color:#3d5a6a;text-transform:uppercase;letter-spacing:1px}}
h3{{color:#3fb950;margin:0 0 10px;font-size:.78rem;letter-spacing:2px}}
.sig{{background:#0d1305;border-left:3px solid #3fb950;padding:10px 14px;margin:5px 0;border-radius:2px}}
.sr{{display:flex;justify-content:space-between;align-items:center;margin-bottom:5px}}
.sd{{font-size:.73rem;margin:2px 0;color:#8aa8b8}}
.ts{{color:#3d5a6a;font-size:.65rem}}
.footer{{color:#3d5a6a;font-size:.62rem;margin-top:20px;border-top:1px solid #1c2a36;padding-top:10px;line-height:2}}
</style></head><body>
<h1>PUMP SCANNER <small style="font-size:.6rem;color:#3d5a6a">v7.0 — PUMP SİSTEMİ</small></h1>
<div class="info">
  🚀 PUMP: 15m spike ≥{PUMP_VR15_MIN:.0f}x | 15m getiri ≥{PUMP_RET15_MIN:.0f}% | 1h hacim ≥{PUMP_VR1H_MIN:.0f}x | 4h MA50 trend | 4h ROC ≥{PUMP_ROC_MIN:.0f}%<br>
  Stop: -{PUMP_SL_PCT:.0f}% | TP: +{PUMP_TP_PCT:.0f}% | Hold: {PUMP_EXPIRE_H}h | Cooldown: {PUMP_COOLDOWN_H}h | Backtest WR: ~%91
</div>
<div class="stats">
  <div class="stat"><span class="sv">{len(tracked_symbols)}</span><span class="sl">Sembol</span></div>
  <div class="stat"><span class="sv">{ws_1h_closes}</span><span class="sl">1H Kapanış</span></div>
  <div class="stat"><span class="sv">{stats.get("signal_sent",0)}</span><span class="sl">Toplam Sinyal</span></div>
  <div class="stat"><span class="sv" style="color:#00f080">{ps.get("win",0)}</span><span class="sl">Win</span></div>
  <div class="stat"><span class="sv" style="color:#ff4444">{ps.get("loss",0)}</span><span class="sl">Stop</span></div>
  <div class="stat"><span class="sv">{ps.get("win_rate",0)}%</span><span class="sl">Win Rate</span></div>
  <div class="stat"><span class="sv">{ps.get("avg_peak",0)}%</span><span class="sl">Ort. Peak</span></div>
  <div class="stat"><span class="sv">{bot_status["status"]}</span><span class="sl">Durum</span></div>
  <div class="stat"><span class="sv">{now}</span><span class="sl">Saat TR</span></div>
</div>
<h3>SON SİNYALLER</h3>
{sig_rows if sig_rows else '<p style="color:#3d5a6a;font-size:.8rem;padding:8px 0">Henüz sinyal yok.</p>'}
<div class="footer">
  Heartbeat: {heartbeat["last"]} | Son coin: {heartbeat["symbol"]}
  &nbsp;|&nbsp; <a href="/performance" style="color:#00d4ff">📈 Performans</a><br>
  Filtre: Cooldown:{stats.get("cooldown",0)}  Filtered:{stats.get("filtered",0)}  Data:{stats.get("data_missing",0)}
</div>
</body></html>"""

@flask_app.route("/api/status")
def api_status():
    data = clean_json({
        "status": bot_status["status"], "total_symbols": len(tracked_symbols),
        "ws_1h_closes": ws_1h_closes, "signals": all_signals[:30],
        "stats": dict(stats), "heartbeat": heartbeat,
    })
    return flask_app.response_class(json.dumps(data, ensure_ascii=False), mimetype="application/json")

@flask_app.route("/api/health")
def api_health():
    stale = time.time() - float(heartbeat.get("epoch", 0))
    return {"status": "ok", "time": tr_now_str(), "stale_sec": round(stale)}

@flask_app.route("/performance")
def perf_dashboard():
    ps   = perf_summary()
    rows = ""
    for s in signal_log[:60]:
        st     = s.get("status", "open")
        st_col = "#00f080" if st=="win" else ("#ff4444" if st=="loss" else ("#ffb300" if st=="expired" else "#3d5a6a"))
        peak   = s.get("peak_pct", 0)
        cr     = s.get("close_ret"); ct = (s.get("close_time") or "")[:16]
        stype  = s.get("raw_type", s.get("sig_type", "pump"))
        _, label, tc = _TYPE_LABEL.get(stype, ("🚀", "PUMP", "#3fb950"))
        def fmt_ret(r):
            if r is None: return "—"
            col = "#00f080" if float(r) > 0 else "#ff4444"
            return f'<span style="color:{col}">{float(r):+.2f}%</span>'
        rows += f"""<tr>
          <td>{s.get("time","")[:16]}</td>
          <td><b>{s.get("symbol","")}</b></td>
          <td style="color:{tc}">{label}</td>
          <td>{fmt_price(s.get("entry"))}</td>
          <td>{fmt_price(s.get("stop"))}</td>
          <td>{fmt_price(s.get("tp1"))}</td>
          <td style="color:#00f080">+{peak}%</td>
          <td>{'✅' if s.get('tp1_hit') else '—'}</td>
          <td>{fmt_ret(cr)}</td>
          <td>{ct}</td>
          <td style="color:{st_col}">{st.upper()}</td>
        </tr>"""
    return f"""<!DOCTYPE html><html lang="tr"><head>
<meta charset="UTF-8"><title>Performans — Pump Scanner v7</title>
<meta http-equiv="refresh" content="300">
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:#06090d;color:#b8cdd8;font-family:'Courier New',monospace;padding:24px}}
  h1{{color:#00d4ff;font-size:1.1rem;margin-bottom:4px}}
  .sub{{color:#3d5a6a;font-size:.75rem;margin-bottom:18px}}
  .cards{{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:20px}}
  .card{{background:#0c1117;border:1px solid #1c2a36;border-radius:4px;padding:12px;min-width:110px;text-align:center}}
  .cv{{font-size:1.3rem;font-weight:bold;color:#00d4ff}}
  .cl{{font-size:.62rem;color:#3d5a6a;margin-top:4px}}
  table{{width:100%;border-collapse:collapse}}
  th{{background:#0c1117;color:#3d5a6a;font-size:.65rem;text-transform:uppercase;padding:6px 10px;text-align:left;border-bottom:1px solid #1c2a36}}
  td{{padding:6px 10px;border-bottom:1px solid #0c1117;font-size:.76rem}}
  tr:hover td{{background:#0c1117}}
  a{{color:#00d4ff;text-decoration:none}}
</style></head><body>
<h1>📈 PUMP SCANNER SİNYAL PERFORMANSI v7.0</h1>
<div class="sub"><a href="/">← Ana Sayfa</a> &nbsp;|&nbsp; {tr_now_str()}</div>
<div class="cards">
  <div class="card"><div class="cv">{ps.get("total",0)}</div><div class="cl">Toplam</div></div>
  <div class="card"><div class="cv">{ps.get("open",0)}</div><div class="cl">Açık</div></div>
  <div class="card"><div class="cv" style="color:#00f080">{ps.get("win",0)}</div><div class="cl">Win</div></div>
  <div class="card"><div class="cv" style="color:#ff4444">{ps.get("loss",0)}</div><div class="cl">Stop</div></div>
  <div class="card"><div class="cv" style="color:#ffb300">{ps.get("expired",0)}</div><div class="cl">Expired</div></div>
  <div class="card"><div class="cv">{ps.get("win_rate",0)}%</div><div class="cl">Win Rate</div></div>
  <div class="card"><div class="cv">{ps.get("avg_peak",0)}%</div><div class="cl">Ort. Peak</div></div>
</div>
<table><thead><tr>
  <th>Zaman</th><th>Sembol</th><th>Sistem</th><th>Giriş</th><th>Stop</th><th>Hedef</th>
  <th>Peak%</th><th>TP1</th><th>Kapanış%</th><th>Kapanış Zamanı</th><th>Durum</th>
</tr></thead><tbody>{rows}</tbody></table>
</body></html>"""

# ============================================================
# PERİYODİK GÖREVLER
# ============================================================
async def periodic_tasks():
    tick         = 0
    last_4h_tick = 0
    last_1d_tick = 0
    while True:
        await asyncio.sleep(600)
        tick += 1
        pump_cnt = sum(1 for s in all_signals if s.get("type") == "pump")
        print(
            f"\n╔══════════════ PUMP SCANNER ÖZET ══════════════╗\n"
            f"  Sembol: {len(tracked_symbols):<6} 1H Kapanış: {ws_1h_closes:<6} Toplam Sinyal: {stats.get('signal_sent',0)}\n"
            f"  15m bar: {len(bars_15m):<5} 4H bar: {len(bars_4h):<5} 1D bar: {len(bars_1d)}\n"
            f"  PUMP sinyaller (son oturum): {pump_cnt}\n"
            f"  ── Filtre ──\n"
            f"  Filtered:{stats.get('filtered',0)}  Cooldown:{stats.get('cooldown',0)}"
            f"  Data:{stats.get('data_missing',0)}\n"
            f"╚═══════════════════════════════════════════════╝",
            flush=True,
        )
        # 4H bar güncelleme — her 4 saatte (24 tick × 10 dk = 240 dk)
        if tick - last_4h_tick >= 24:
            last_4h_tick = tick
            asyncio.create_task(refresh_4h_bars(tracked_symbols + ["BTC/USDT"]))
        # 1D bar güncelleme — her 24 saatte (144 tick)
        if tick - last_1d_tick >= 144:
            last_1d_tick = tick
            asyncio.create_task(refresh_1d_bars(tracked_symbols + ["BTC/USDT"]))

# ============================================================
# MAIN
# ============================================================
async def main():
    global tracked_symbols
    print("Pump Scanner v7.0 başlatılıyor — PUMP SİSTEMİ", flush=True)
    print(f"  Koşullar: 15m spike ≥{PUMP_VR15_MIN:.0f}x | 15m getiri ≥{PUMP_RET15_MIN:.0f}% | 1h hacim ≥{PUMP_VR1H_MIN:.0f}x", flush=True)
    print(f"            4h MA50 trend | 4h ROC ≥{PUMP_ROC_MIN:.0f}% ({PUMP_ROC_PERIOD} bar)", flush=True)
    print(f"  Stop: -{PUMP_SL_PCT:.0f}% | TP: +{PUMP_TP_PCT:.0f}% | Hold: {PUMP_EXPIRE_H}h | Cooldown: {PUMP_COOLDOWN_H}h", flush=True)

    symbols = await load_symbols_pool()
    if not symbols:
        print("Sembol yüklenemedi", flush=True); return

    tracked_symbols      = list(symbols)
    bot_status["status"] = "BOOTSTRAP"
    print(f"{len(symbols)} sembol yüklendi", flush=True)

    await bootstrap_all(symbols)
    rebuild_pending()
    print(f"Pending sinyaller: {sum(len(v) for v in pending_by_symbol.values())}", flush=True)

    candidate_queue = asyncio.Queue()
    asyncio.create_task(signal_worker(candidate_queue))
    asyncio.create_task(periodic_tasks())
    asyncio.create_task(bootstrap_secondary_tf(symbols))

    bot_status["status"] = "LIVE"
    print(f"LIVE | {len(symbols)} sembol izleniyor", flush=True)

    await ws_all(symbols, candidate_queue)

def start_flask():
    port = int(os.environ.get("PORT", "10000"))
    flask_app.run(host="0.0.0.0", port=port, use_reloader=False)

if __name__ == "__main__":
    threading.Thread(target=start_flask, daemon=True).start()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Durduruldu", flush=True)
