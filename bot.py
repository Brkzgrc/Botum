# -*- coding: utf-8 -*-
"""
Trend & Momentum Scanner v3.0
1H sinyal + 4H teyit — WebSocket tabanli
RSI, Williams %R, MFI, MACD, BB, Hacim (1H)
EMA20/SMA50, ADX, OBV (4H)
Puanlama: 100 uzerinden (1H=70p, 4H=30p)
"""

import asyncio
import json
import time
import threading
import os
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pandas_ta as ta
import requests
import ccxt
import websockets
from flask import Flask

# ============================================================
# 0) AYARLAR
# ============================================================
BINANCE_API_KEY    = os.getenv("BINANCE_API_KEY",    "")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")
TELEGRAM_TOKEN     = os.getenv("TELEGRAM_TOKEN",     "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID",   "")

RSI_THRESH   = float(os.getenv("RSI_THRESH",   "35"))
WR_THRESH    = float(os.getenv("WR_THRESH",    "-80"))
MFI_THRESH   = float(os.getenv("MFI_THRESH",   "20"))
ADX_THRESH   = float(os.getenv("ADX_THRESH",   "20"))
MIN_SCORE    = int(os.getenv("MIN_SCORE",       "60"))
STRONG_SCORE = int(os.getenv("STRONG_SCORE",    "80"))

ATR_STOP_MULT   = float(os.getenv("ATR_STOP_MULT",   "1.5"))
ATR_TARGET_MULT = float(os.getenv("ATR_TARGET_MULT", "2.5"))

SIGNAL_COOLDOWN_HOURS = int(os.getenv("SIGNAL_COOLDOWN_HOURS", "4"))
MIN_LIQUIDITY         = float(os.getenv("MIN_LIQUIDITY",       "5000000"))
MAX_SYMBOLS           = int(os.getenv("MAX_SYMBOLS",           "0"))

WS_STREAM_CHUNK = int(os.getenv("WS_STREAM_CHUNK", "120"))
BOOTSTRAP_1H    = int(os.getenv("BOOTSTRAP_1H",    "200"))
BOOTSTRAP_4H    = int(os.getenv("BOOTSTRAP_4H",    "100"))
KEEP_1H         = int(os.getenv("KEEP_1H",         "200"))
KEEP_4H         = int(os.getenv("KEEP_4H",         "100"))

BOOT_EVERY = 50
TR_TZ      = timezone(timedelta(hours=3))

IGNORED_COINS = set([
    'UP/USDT','DOWN/USDT','BEAR/USDT','BULL/USDT',
    'USDC/USDT','TUSD/USDT','FDUSD/USDT','DAI/USDT','USDP/USDT',
    'EUR/USDT','TRY/USDT','GBP/USDT','BUSD/USDT','USTC/USDT',
    'PAXG/USDT','WBTC/USDT','USDE/USDT','BRL/USDT','RUB/USDT',
    'AUD/USDT','UST/USDT','USD/USDT','XUSD/USDT','USD1/USDT','BFUSD/USDT',
])

# ============================================================
# 0.1) STATS
# ============================================================
stats           = Counter()
ws_1h_closes    = 0
tracked_symbols = []

def tr_now_str():
    return datetime.now(timezone.utc).astimezone(TR_TZ).strftime("%Y-%m-%d %H:%M:%S")

def print_summary():
    closes = max(ws_1h_closes, 1)
    print("\n--- TARAMA OZETI ---", flush=True)
    print(f"Sembol       : {len(tracked_symbols)}", flush=True)
    print(f"1H kapanis   : {ws_1h_closes}", flush=True)
    print(f"Sinyal       : {stats.get('signal_sent', 0)}", flush=True)
    for k, lbl in [
        ("score_low",     "Skor dusuk"),
        ("cooldown",      "Cooldown"),
        ("low_liquidity", "Dusuk hacim"),
        ("no_4h_data",    "4H veri yok"),
        ("data_missing",  "1H veri yok"),
    ]:
        v = stats.get(k, 0)
        if v:
            print(f"  {lbl:20s}: {v} ({v/closes*100:.1f}%)", flush=True)
    print("--------------------\n", flush=True)

# ============================================================
# 1) API GATE
# ============================================================
class ApiGate:
    def __init__(self, min_interval_sec=0.25, max_concurrent=3, max_retries=6):
        self.min_interval_sec = float(min_interval_sec)
        self.sem              = asyncio.Semaphore(int(max_concurrent))
        self.max_retries      = int(max_retries)
        self._lock            = asyncio.Lock()
        self._last_call_ts    = 0.0

    async def _space(self):
        async with self._lock:
            now  = time.time()
            wait = (self._last_call_ts + self.min_interval_sec) - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call_ts = time.time()

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
                except ccxt.ExchangeError:
                    await asyncio.sleep(min(12.0, 0.6 * (2 ** attempt)))
                except Exception:
                    await asyncio.sleep(min(8.0, 0.5 * (2 ** attempt)))
            raise RuntimeError("API call failed")

api_gate = ApiGate()

exchange = ccxt.binance({
    "apiKey":  BINANCE_API_KEY  or None,
    "secret":  BINANCE_API_SECRET or None,
    "options": {"defaultType": "spot", "adjustForTimeDifference": True},
    "enableRateLimit": True,
    "timeout": 15000,
})

# ============================================================
# 2) DATA STORE
# ============================================================
@dataclass
class SignalCandidate:
    symbol:  str
    result:  dict
    tr_time: datetime

bars_1h:          dict = {}
bars_4h:          dict = {}
last_signal_ts:   dict = {}
last_scan_result: dict = {}
all_signals:      list = []

# ============================================================
# 3) SEMBOL HAVUZU
# ============================================================
async def load_symbols_pool():
    await api_gate.call(exchange.load_markets)
    syms = [
        s for s in exchange.markets
        if s.endswith("/USDT")
        and exchange.markets[s].get("active", False)
        and s not in IGNORED_COINS
        and s.isascii()
    ]
    if not syms:
        return []

    volumes = {}
    for i in range(0, len(syms), 120):
        part = syms[i:i + 120]
        try:
            res = await api_gate.call(exchange.fetch_tickers, part)
            if isinstance(res, dict):
                for k, v in res.items():
                    volumes[k] = float(v.get("quoteVolume", 0) or 0)
        except Exception:
            continue

    sorted_syms = sorted(syms, key=lambda x: volumes.get(x, 0), reverse=True)
    return sorted_syms[:MAX_SYMBOLS] if MAX_SYMBOLS else sorted_syms

# ============================================================
# 4) VERİ + İNDİKATOR
# ============================================================
async def fetch_df(symbol, timeframe, limit):
    raw = await api_gate.call(exchange.fetch_ohlcv, symbol, timeframe=timeframe, limit=limit)
    df  = pd.DataFrame(raw, columns=["timestamp","open","high","low","close","volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df.set_index("timestamp", inplace=True)
    return df

def prepare_1h(df):
    df = df.copy()
    df["rsi"] = ta.rsi(df["close"], length=14)
    h14 = df["high"].rolling(14)
    l14 = df["low"].rolling(14)
    df["wr"] = -100 * (h14.max() - df["close"]) / (h14.max() - l14.min())
    mfi = ta.mfi(df["high"], df["low"], df["close"], df["volume"], length=14)
    df["mfi"] = mfi if mfi is not None else np.nan
    macd_df = ta.macd(df["close"], fast=12, slow=26, signal=9)
    if macd_df is not None:
        cols     = macd_df.columns.tolist()
        hist_col = next((c for c in cols if "MACDh" in c), None)
        if hist_col is None:
            hist_col = next((c for c in cols if "h" in c.lower()), None)
        df["macd_hist"] = macd_df[hist_col] if hist_col else np.nan
    else:
        df["macd_hist"] = np.nan
    bb = ta.bbands(df["close"], length=20, std=2)
    if bb is not None:
        cols  = bb.columns.tolist()
        l_col = next((c for c in cols if "BBL" in c.upper()), None)
        m_col = next((c for c in cols if "BBM" in c.upper()), None)
        u_col = next((c for c in cols if "BBU" in c.upper()), None)
        if l_col: df["bb_lower"] = bb[l_col]
        if m_col: df["bb_mid"]   = bb[m_col]
        if u_col: df["bb_upper"] = bb[u_col]
    df["vol_ma"] = df["volume"].rolling(20, min_periods=1).mean()
    df["atr"]    = ta.atr(df["high"], df["low"], df["close"], length=14)
    return df

def prepare_4h(df):
    df = df.copy()
    df["ema20"] = ta.ema(df["close"], length=20)
    df["sma50"] = ta.sma(df["close"], length=50)
    adx_df = ta.adx(df["high"], df["low"], df["close"], length=14)
    if adx_df is not None:
        cols    = adx_df.columns.tolist()
        adx_col = next((c for c in cols if c.upper().startswith("ADX_")), None)
        df["adx"] = adx_df[adx_col] if adx_col else np.nan
    else:
        df["adx"] = np.nan
    obv = ta.obv(df["close"], df["volume"])
    df["obv"]    = obv if obv is not None else np.nan
    df["obv_ma"] = df["obv"].rolling(20, min_periods=1).mean()
    return df

# ============================================================
# 5) 1H ANALİZ — max 70 puan
# ============================================================
def analyze_1h(df):
    if len(df) < 60:
        return None

    prev = df.iloc[-3]
    last = df.iloc[-2]
    curr = df.iloc[-1]

    def sf(row, col):
        v = row.get(col, np.nan)
        return None if pd.isna(v) else float(v)

    rsi      = sf(last, "rsi")
    wr       = sf(last, "wr")
    mfi      = sf(last, "mfi")
    mhist    = sf(last, "macd_hist")
    mhist_p  = sf(prev, "macd_hist")
    bb_lower = sf(last, "bb_lower")
    close_v  = sf(last, "close")
    low_v    = sf(last, "low")
    vol      = sf(last, "volume")
    vol_ma   = sf(last, "vol_ma")
    atr      = sf(last, "atr")
    entry    = sf(curr, "close")

    if None in (rsi, wr, mfi, entry):
        return None

    # RSI < 35 — max 20p
    c_rsi = rsi < RSI_THRESH
    p_rsi = max(0.0, min(20.0, (RSI_THRESH - rsi) / (RSI_THRESH - 10) * 20)) if c_rsi else 0.0

    # Williams %R < -80 — max 15p
    c_wr = wr < WR_THRESH
    p_wr = max(0.0, min(15.0, (abs(wr) - abs(WR_THRESH)) / (100 - abs(WR_THRESH)) * 15)) if c_wr else 0.0

    # MFI < 20 — max 15p
    c_mfi = mfi < MFI_THRESH
    p_mfi = max(0.0, min(15.0, (MFI_THRESH - mfi) / MFI_THRESH * 15)) if c_mfi else 0.0

    # MACD histogram donuyor — max 10p
    c_macd = False
    p_macd = 0.0
    if mhist is not None and mhist_p is not None:
        turning = (mhist > mhist_p) and (mhist_p < 0)
        crossed = (mhist > 0) and (mhist_p <= 0)
        c_macd  = turning or crossed
        if crossed:
            p_macd = 10.0
        elif turning:
            p_macd = min(7.0, abs(mhist - mhist_p) / max(abs(mhist_p), 1e-10) * 7.0)

    # BB alt banttan geri donus — max 5p
    c_bb = False
    p_bb = 0.0
    if bb_lower is not None and close_v is not None and low_v is not None:
        c_bb = (low_v <= bb_lower * 1.002) and (close_v > bb_lower)
        p_bb = 5.0 if c_bb else 0.0

    # Hacim artisi — max 5p
    c_vol     = False
    p_vol     = 0.0
    vol_ratio = 0.0
    if vol is not None and vol_ma is not None and vol_ma > 0:
        vol_ratio = vol / vol_ma
        c_vol = vol_ratio >= 1.2
        p_vol = min(5.0, (vol_ratio - 1.0) * 5.0) if vol_ratio > 1.0 else 0.0

    return {
        "conditions": {"rsi": c_rsi, "wr": c_wr, "mfi": c_mfi, "macd": c_macd, "bb": c_bb, "vol": c_vol},
        "score_1h":   round(p_rsi + p_wr + p_mfi + p_macd + p_bb + p_vol, 1),
        "rsi":        round(rsi,  2),
        "wr":         round(wr,   2),
        "mfi":        round(mfi,  2),
        "macd_hist":  round(mhist,   8) if mhist   is not None else None,
        "macd_prev":  round(mhist_p, 8) if mhist_p is not None else None,
        "bb_lower":   round(bb_lower, 8) if bb_lower is not None else None,
        "vol_ratio":  round(vol_ratio, 2),
        "atr":        round(atr, 8) if atr is not None else 0.0,
        "entry":      round(entry, 8),
    }

# ============================================================
# 6) 4H TEYİT — max 30 puan
# ============================================================
def analyze_4h(df):
    if df is None or len(df) < 55:
        return None

    last  = df.iloc[-2]
    prev5 = df.iloc[-7:-2]

    def sf(row, col):
        v = row.get(col, np.nan)
        return None if pd.isna(v) else float(v)

    ema20  = sf(last, "ema20")
    sma50  = sf(last, "sma50")
    adx    = sf(last, "adx")
    obv    = sf(last, "obv")
    obv_ma = sf(last, "obv_ma")

    if None in (ema20, sma50, adx):
        return None

    # EMA20 / SMA50 — max 10p
    ema_above    = ema20 > sma50
    recent_cross = False
    for i in range(len(prev5) - 1):
        r0 = prev5.iloc[i];   r1 = prev5.iloc[i + 1]
        e0 = sf(r0, "ema20"); s0 = sf(r0, "sma50")
        e1 = sf(r1, "ema20"); s1 = sf(r1, "sma50")
        if None not in (e0, s0, e1, s1) and e0 <= s0 and e1 > s1:
            recent_cross = True
            break

    c_ema = ema_above or recent_cross
    if recent_cross:
        p_ema = 10.0
    elif ema_above:
        p_ema = min(7.0, (ema20 - sma50) / sma50 * 100 * 2.0)
    else:
        p_ema = 0.0

    # ADX >= 20 — max 10p
    c_adx = adx >= ADX_THRESH
    p_adx = max(0.0, min(10.0, (adx - ADX_THRESH) / 10.0 * 10.0)) if c_adx else 0.0

    # OBV — max 10p
    c_obv      = False
    p_obv      = 0.0
    obv_signal = "veri yok"
    if obv is not None and obv_ma is not None:
        c_obv      = obv > obv_ma
        obv_signal = "ortalama ustunde" if c_obv else "ortalama altinda"
        p_obv      = 10.0 if c_obv else 0.0
        if not c_obv and len(prev5) >= 3:
            try:
                price_ch = float(prev5["close"].iloc[-1]) - float(prev5["close"].iloc[0])
                obv_ch   = float(prev5["obv"].iloc[-1])   - float(prev5["obv"].iloc[0])
                if price_ch < 0 and obv_ch > 0:
                    c_obv = True; p_obv = 8.0; obv_signal = "pozitif diverjans"
            except Exception:
                pass

    return {
        "conditions":   {"ema_cross": c_ema, "adx": c_adx, "obv": c_obv},
        "score_4h":     round(p_ema + p_adx + p_obv, 1),
        "ema20":        round(ema20, 4),
        "sma50":        round(sma50, 4),
        "ema_above":    ema_above,
        "recent_cross": recent_cross,
        "adx":          round(adx, 2),
        "obv_signal":   obv_signal,
    }

# ============================================================
# 7) TAM ANALİZ
# ============================================================
def full_analyze(symbol, df_1h, df_4h):
    r1h = analyze_1h(df_1h)
    if r1h is None:
        return None

    r4h = analyze_4h(df_4h)
    if r4h is None:
        stats["no_4h_data"] += 1
        return None

    score_total = r1h["score_1h"] + r4h["score_4h"]

    if score_total < MIN_SCORE:
        stats["score_low"] += 1
        return None

    entry  = r1h["entry"]
    atr    = r1h["atr"] if r1h["atr"] > 0 else entry * 0.02
    target = round(entry + atr * ATR_TARGET_MULT, 8)
    stop   = round(entry - atr * ATR_STOP_MULT,   8)

    score100 = min(int(round(score_total)), 100)
    strength = "guclu" if score100 >= STRONG_SCORE else "normal"
    met_count = sum({**r1h["conditions"], **r4h["conditions"]}.values())

    return {
        "symbol":        symbol,
        "time":          datetime.now(timezone.utc).isoformat(),
        "entry":         entry,
        "target":        target,
        "stop":          stop,
        "target_pct":    round((target - entry) / entry * 100, 2),
        "stop_pct":      round((entry - stop)   / entry * 100, 2),
        "score100":      score100,
        "score_1h":      r1h["score_1h"],
        "score_4h":      r4h["score_4h"],
        "strength":      strength,
        "met_count":     met_count,
        "conditions_1h": r1h["conditions"],
        "conditions_4h": r4h["conditions"],
        "rsi":           r1h["rsi"],
        "wr":            r1h["wr"],
        "mfi":           r1h["mfi"],
        "macd_hist":     r1h["macd_hist"],
        "macd_prev":     r1h["macd_prev"],
        "vol_ratio":     r1h["vol_ratio"],
        "ema20":         r4h["ema20"],
        "sma50":         r4h["sma50"],
        "ema_above":     r4h["ema_above"],
        "recent_cross":  r4h["recent_cross"],
        "adx":           r4h["adx"],
        "obv_signal":    r4h["obv_signal"],
        "signal":        True,
    }

# ============================================================
# 8) TELEGRAM
# ============================================================
def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text,
                  "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=10,
        )
        if r.status_code != 200:
            print(f"Telegram {r.status_code}: {r.text[:80]}", flush=True)
    except Exception as e:
        print(f"Telegram hata: {e}", flush=True)

def fmt_price(price):
    if price is None: return "?"
    p = float(price)
    if p >= 100:  return f"{p:.2f}"
    if p >= 1:    return f"{p:.3f}"
    if p >= 0.01: return f"{p:.4f}"
    return f"{p:.6f}"

def build_tg_message(r, tr_time):
    sc    = r["score100"]
    emoji = "🟢" if sc >= STRONG_SCORE else ("🟡" if sc >= MIN_SCORE else "🔴")
    label = "Guclu Sinyal ⚡" if r["strength"] == "guclu" else "Normal Sinyal"
    sym   = r["symbol"].replace("/", "").replace("USDT", "")
    now   = tr_time.strftime("%d/%m/%Y %H:%M")
    c1    = r["conditions_1h"]
    c4    = r["conditions_4h"]

    def tick(v): return "✅" if v else "❌"

    mh = r.get("macd_hist"); mp = r.get("macd_prev")
    if mh is not None and mp is not None:
        if mh > 0 and mp <= 0:   macd_txt = "yukari gecti ↑"
        elif mh > mp and mp < 0: macd_txt = "donuyor ↑"
        else:                    macd_txt = "negatif"
    else:
        macd_txt = "?"

    bb_txt  = "alt banttan geri donus" if c1.get("bb") else "tetiklenmedi"
    vol_txt = f"{r.get('vol_ratio', 0):.1f}x ortalama"
    ema_txt = ("kesisim var (yakin)" if r.get("recent_cross")
               else "EMA20 > SMA50" if r.get("ema_above")
               else "EMA20 < SMA50")

    return (
        f"🕐 {now}\n\n"
        f"#{sym}/USDT  •  1H + 4H teyit\n\n"
        f"💵 Giris:  {fmt_price(r.get('entry'))}\n"
        f"🎯 Hedef:  {fmt_price(r.get('target'))} (+%{r.get('target_pct')})\n"
        f"🛡️ Stop:   {fmt_price(r.get('stop'))} (-%{r.get('stop_pct')})\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"📊 Puan: {sc}/100  {emoji}\n\n"
        f"1H Indiktorler:\n"
        f"RSI(14)      →  {r.get('rsi')}  {tick(c1.get('rsi'))}\n"
        f"Williams %R  →  {r.get('wr')}  {tick(c1.get('wr'))}\n"
        f"MFI(14)      →  {r.get('mfi')}  {tick(c1.get('mfi'))}\n"
        f"MACD Hist    →  {macd_txt}  {tick(c1.get('macd'))}\n"
        f"BB           →  {bb_txt}  {tick(c1.get('bb'))}\n"
        f"Hacim        →  {vol_txt}  {tick(c1.get('vol'))}\n\n"
        f"4H Teyit:\n"
        f"EMA20/SMA50  →  {ema_txt}  {tick(c4.get('ema_cross'))}\n"
        f"ADX          →  {r.get('adx')}  {tick(c4.get('adx'))}\n"
        f"OBV          →  {r.get('obv_signal')}  {tick(c4.get('obv'))}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"⚡ {label}  ({r.get('met_count')}/9 kosul)"
    )

# ============================================================
# 9) BOOTSTRAP
# ============================================================
async def bootstrap_symbol(symbol):
    try:
        df1 = await fetch_df(symbol, "1h", BOOTSTRAP_1H)
        if df1 is None or len(df1) < 60: return False
        df1 = prepare_1h(df1)
        bars_1h[symbol] = df1.iloc[-KEEP_1H:] if len(df1) > KEEP_1H else df1

        df4 = await fetch_df(symbol, "4h", BOOTSTRAP_4H)
        if df4 is None or len(df4) < 55: return False
        df4 = prepare_4h(df4)
        bars_4h[symbol] = df4.iloc[-KEEP_4H:] if len(df4) > KEEP_4H else df4

        return True
    except Exception:
        return False

async def bootstrap_all(symbols):
    ok = 0
    print(f"Bootstrap basladi | {len(symbols)} sembol", flush=True)
    for i, sym in enumerate(symbols, 1):
        if i % BOOT_EVERY == 0:
            print(f"  -> {i}/{len(symbols)}", flush=True)
        if await bootstrap_symbol(sym):
            ok += 1
    print(f"Bootstrap bitti | {ok}/{len(symbols)}", flush=True)

# ============================================================
# 10) MUM KAPANISINI ISLE
# ============================================================
def safe_float(val, dec=4):
    try:
        v = float(val)
        return None if (v != v or v in (float("inf"), float("-inf"))) else round(v, dec)
    except Exception:
        return None

async def on_1h_close(symbol, o, h, l, c, v, ts_ms, candidate_queue):
    global ws_1h_closes
    ws_1h_closes += 1
    beat(symbol=symbol, status="LIVE")

    df1 = bars_1h.get(symbol)
    if df1 is None or len(df1) < 60:
        stats["data_missing"] += 1
        return

    tstamp = pd.to_datetime(ts_ms, unit="ms", utc=True)
    df1.loc[tstamp, ["open","high","low","close","volume"]] = [o, h, l, c, v]
    df1 = df1.sort_index()
    if len(df1) > KEEP_1H: df1 = df1.iloc[-KEEP_1H:]
    df1 = prepare_1h(df1)
    bars_1h[symbol] = df1

    tr_now = datetime.now(timezone.utc).astimezone(TR_TZ)

    last_ts = last_signal_ts.get(symbol)
    if last_ts:
        hours = (tr_now.replace(tzinfo=None) - last_ts.replace(tzinfo=None)).total_seconds() / 3600
        if hours < SIGNAL_COOLDOWN_HOURS:
            stats["cooldown"] += 1
            return

    df4    = bars_4h.get(symbol)
    result = full_analyze(symbol, df1, df4)

    prev = df1.iloc[-2]
    last_scan_result[symbol] = {
        "symbol":   symbol,
        "time":     tr_now.isoformat(),
        "price":    round(float(df1.iloc[-1]["close"]), 8),
        "rsi":      safe_float(prev.get("rsi")),
        "wr":       safe_float(prev.get("wr")),
        "mfi":      safe_float(prev.get("mfi")),
        "signal":   result is not None,
        "score100": result["score100"] if result else 0,
        "strength": result["strength"] if result else "",
    }

    if result is None:
        return

    await candidate_queue.put(SignalCandidate(symbol=symbol, result=result, tr_time=tr_now))

async def on_4h_close(symbol, o, h, l, c, v, ts_ms):
    df4 = bars_4h.get(symbol)
    if df4 is None: return
    tstamp = pd.to_datetime(ts_ms, unit="ms", utc=True)
    df4.loc[tstamp, ["open","high","low","close","volume"]] = [o, h, l, c, v]
    df4 = df4.sort_index()
    if len(df4) > KEEP_4H: df4 = df4.iloc[-KEEP_4H:]
    bars_4h[symbol] = prepare_4h(df4)

# ============================================================
# 11) SİNYAL WORKER
# ============================================================
async def signal_worker(candidate_queue):
    while True:
        sig = await candidate_queue.get()
        try:
            symbol = sig.symbol; result = sig.result; tr_time = sig.tr_time
            try:
                ticker    = await api_gate.call(exchange.fetch_ticker, symbol)
                liquidity = float(ticker.get("quoteVolume", 0) or 0)
                if liquidity < MIN_LIQUIDITY:
                    stats["low_liquidity"] += 1
                    continue
            except Exception:
                pass

            send_telegram(build_tg_message(result, tr_time))
            last_signal_ts[symbol] = tr_time.replace(tzinfo=None)
            all_signals.insert(0, result)
            if len(all_signals) > 200: all_signals.pop()
            stats["signal_sent"] += 1
            print(
                f"SINYAL: {symbol} | {result['score100']}/100 {result['strength']} | "
                f"RSI:{result['rsi']} WR:{result['wr']} MFI:{result['mfi']} ADX:{result['adx']}",
                flush=True
            )
        except Exception as e:
            print(f"Worker hata: {str(e)[:100]}", flush=True)
        finally:
            candidate_queue.task_done()

# ============================================================
# 12) WEBSOCKET
# ============================================================
def to_ws(symbol):
    return symbol.replace("/", "").lower()

async def ws_1h_chunk(symbols, candidate_queue):
    streams = "/".join([f"{to_ws(s)}@kline_1h" for s in symbols])
    url = f"wss://stream.binance.com:9443/stream?streams={streams}"
    retry = 0
    while True:
        try:
            async with websockets.connect(url, ping_interval=30, ping_timeout=30) as ws:
                retry = 0
                print(f"1H WS baglandi ({len(symbols)} sembol)", flush=True)
                while True:
                    msg  = await ws.recv()
                    data = json.loads(msg)
                    k    = data.get("data", {}).get("k", {})
                    if not k.get("x", False): continue
                    sym = data.get("data", {}).get("s", "").upper().replace("USDT", "/USDT")
                    await on_1h_close(sym, float(k["o"]), float(k["h"]),
                                      float(k["l"]), float(k["c"]),
                                      float(k["v"]), int(k["t"]), candidate_queue)
        except Exception as e:
            retry += 1
            backoff = min(60, 5 * (2 ** min(retry, 4)))
            print(f"1H WS koptu -> {backoff}s: {str(e)[:50]}", flush=True)
            await asyncio.sleep(backoff)

async def ws_4h_chunk(symbols):
    streams = "/".join([f"{to_ws(s)}@kline_4h" for s in symbols])
    url = f"wss://stream.binance.com:9443/stream?streams={streams}"
    retry = 0
    while True:
        try:
            async with websockets.connect(url, ping_interval=30, ping_timeout=30) as ws:
                retry = 0
                print(f"4H WS baglandi ({len(symbols)} sembol)", flush=True)
                while True:
                    msg  = await ws.recv()
                    data = json.loads(msg)
                    k    = data.get("data", {}).get("k", {})
                    if not k.get("x", False): continue
                    sym = data.get("data", {}).get("s", "").upper().replace("USDT", "/USDT")
                    await on_4h_close(sym, float(k["o"]), float(k["h"]),
                                      float(k["l"]), float(k["c"]),
                                      float(k["v"]), int(k["t"]))
        except Exception as e:
            retry += 1
            backoff = min(60, 5 * (2 ** min(retry, 4)))
            print(f"4H WS koptu -> {backoff}s: {str(e)[:50]}", flush=True)
            await asyncio.sleep(backoff)

async def ws_all(symbols, candidate_queue):
    tasks = []
    for i in range(0, len(symbols), WS_STREAM_CHUNK):
        chunk = symbols[i:i + WS_STREAM_CHUNK]
        tasks.append(asyncio.create_task(ws_1h_chunk(chunk, candidate_queue)))
        tasks.append(asyncio.create_task(ws_4h_chunk(chunk)))
    await asyncio.gather(*tasks)

# ============================================================
# 13) FLASK DASHBOARD
# ============================================================
flask_app  = Flask(__name__)
bot_status = {"status": "BOOT", "signal_count": 0}

import logging
logging.getLogger("werkzeug").setLevel(logging.ERROR)
flask_app.logger.disabled = True

heartbeat  = {"last": tr_now_str(), "epoch": time.time(), "symbol": "?", "status": "BOOT"}
_last_beat = 0.0

def beat(symbol=None, status=None):
    global _last_beat
    now = time.time()
    if now - _last_beat >= 10:
        heartbeat["last"] = tr_now_str(); heartbeat["epoch"] = now
        if symbol: heartbeat["symbol"] = symbol
        if status: heartbeat["status"] = status
        _last_beat = now

def heartbeat_pinger():
    while True:
        heartbeat["last"] = tr_now_str(); heartbeat["epoch"] = time.time()
        time.sleep(15)

def watchdog_thread():
    while True:
        stale = time.time() - float(heartbeat.get("epoch", 0))
        if stale > 600:
            print(f"WATCHDOG: {int(stale)}s stale", flush=True)
            os._exit(1)
        time.sleep(10)

def clean_json(obj):
    if isinstance(obj, dict):  return {k: clean_json(v) for k, v in obj.items()}
    if isinstance(obj, list):  return [clean_json(i) for i in obj]
    if isinstance(obj, float):
        if obj != obj or obj in (float("inf"), float("-inf")): return None
        return obj
    if hasattr(obj, "item"):   return clean_json(obj.item())
    return obj

def score_color(s):
    if s >= STRONG_SCORE: return "#00f080"
    if s >= MIN_SCORE:    return "#ffb300"
    return "#ff3a5c"

@flask_app.route("/")
def home():
    now = datetime.now(TR_TZ).strftime("%H:%M:%S")
    sig_rows = ""
    for s in all_signals[:20]:
        sc  = s.get("score100", 0)
        col = score_color(sc)
        c1  = s.get("conditions_1h", {}); c4 = s.get("conditions_4h", {})
        met = sum(c1.values()) + sum(c4.values())
        lbl = "Guclu ⚡" if s.get("strength") == "guclu" else "Normal"
        sig_rows += (
            f'<div class="sig">'
            f'<div class="sr"><b>{s.get("symbol","")}</b>'
            f'<span style="color:{col};font-weight:bold">{sc}/100</span>'
            f'<span style="color:#3d5a6a;font-size:.65rem">{lbl} | {met}/9</span></div>'
            f'<div class="sd"><span style="color:#00d4ff">${fmt_price(s.get("entry"))}</span>'
            f' → 🎯 {fmt_price(s.get("target"))} (+%{s.get("target_pct","")})'
            f'  🛡️ {fmt_price(s.get("stop"))} (-%{s.get("stop_pct","")})</div>'
            f'<div class="sd">RSI {s.get("rsi","")} | W%R {s.get("wr","")} | MFI {s.get("mfi","")} | ADX {s.get("adx","")}</div>'
            f'<div class="sd" style="color:#3d5a6a">{s.get("time","")[:16]} UTC</div>'
            f'</div>'
        )
    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Scanner v3</title>
<meta http-equiv="refresh" content="30">
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#06090d;color:#b8cdd8;font-family:'Courier New',monospace;padding:20px;max-width:960px;margin:0 auto}}
h1{{color:#00d4ff;letter-spacing:4px;font-size:1.2rem;margin-bottom:18px}}
.stats{{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:22px}}
.stat{{background:#0c1117;border:1px solid #1c2a36;padding:10px 16px;border-radius:4px;min-width:90px}}
.sv{{font-size:1.15rem;color:#00d4ff;display:block;font-weight:bold}}
.sl{{font-size:.58rem;color:#3d5a6a;text-transform:uppercase;letter-spacing:1px}}
h3{{color:#00f080;margin:0 0 10px;font-size:.8rem;letter-spacing:2px}}
.sig{{background:#031409;border-left:3px solid #00f080;padding:12px 16px;margin:6px 0;border-radius:2px}}
.sr{{display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;gap:8px}}
.sd{{font-size:.75rem;margin:3px 0;color:#8aa8b8}}
.footer{{color:#3d5a6a;font-size:.62rem;margin-top:24px;border-top:1px solid #1c2a36;padding-top:12px;line-height:2}}
</style></head><body>
<h1>TREND & MOMENTUM SCANNER <small style="font-size:.65rem;color:#3d5a6a">1H + 4H</small></h1>
<div class="stats">
  <div class="stat"><span class="sv">{len(tracked_symbols)}</span><span class="sl">Sembol</span></div>
  <div class="stat"><span class="sv">{ws_1h_closes}</span><span class="sl">1H Kapanis</span></div>
  <div class="stat"><span class="sv">{stats.get("signal_sent",0)}</span><span class="sl">Sinyal</span></div>
  <div class="stat"><span class="sv">{bot_status["status"]}</span><span class="sl">Durum</span></div>
  <div class="stat"><span class="sv">{now}</span><span class="sl">Saat TR</span></div>
</div>
<h3>SON SİNYALLER</h3>
{sig_rows if sig_rows else '<p style="color:#3d5a6a;font-size:.8rem;padding:10px 0">Henuz sinyal yok.</p>'}
<div class="footer">
  Heartbeat: {heartbeat["last"]} | Son coin: {heartbeat["symbol"]}<br>
  Eleme: Skor:{stats.get("score_low",0)} Cooldown:{stats.get("cooldown",0)}
  Hacim:{stats.get("low_liquidity",0)} 4H:{stats.get("no_4h_data",0)} 1H:{stats.get("data_missing",0)}
</div></body></html>"""

@flask_app.route("/api/status")
def api_status():
    data = clean_json({
        "status": bot_status["status"], "total_symbols": len(tracked_symbols),
        "ws_1h_closes": ws_1h_closes, "signals": all_signals[:30],
        "last_scan": dict(last_scan_result), "stats": dict(stats), "heartbeat": heartbeat,
    })
    return flask_app.response_class(json.dumps(data, ensure_ascii=False), mimetype="application/json")

@flask_app.route("/api/health")
def api_health():
    return {"status": "ok", "time": tr_now_str()}

# ============================================================
# 14) MAIN
# ============================================================
async def periodic_summary():
    while True:
        await asyncio.sleep(600)
        print_summary()

async def main():
    print("Trend & Momentum Scanner v3.0 baslatiliyor...", flush=True)
    symbols = await load_symbols_pool()
    if not symbols:
        print("Sembol yuklenemedi", flush=True); return

    global tracked_symbols
    tracked_symbols = list(symbols)
    bot_status["status"] = "BOOTSTRAP"
    print(f"{len(symbols)} sembol yuklendi", flush=True)

    await bootstrap_all(symbols)
    print_summary()

    candidate_queue = asyncio.Queue()
    asyncio.create_task(signal_worker(candidate_queue))
    asyncio.create_task(periodic_summary())

    bot_status["status"] = "LIVE"
    print(f"WebSocket canli | {len(symbols)} sembol | 1H + 4H", flush=True)
    await ws_all(symbols, candidate_queue)

def start_flask():
    port = int(os.environ.get("PORT", "10000"))
    flask_app.run(host="0.0.0.0", port=port, use_reloader=False)

if __name__ == "__main__":
    threading.Thread(target=start_flask,      daemon=True).start()
    threading.Thread(target=heartbeat_pinger, daemon=True).start()
    threading.Thread(target=watchdog_thread,  daemon=True).start()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Durduruldu", flush=True)
