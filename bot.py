# -*- coding: utf-8 -*-
"""
Trend & Momentum Scanner v6.0
==============================
DIP sistemi:
  StochRSI < 0.05, WR < -70, OBV_OSC < -40, WT < -75, VOL > 1.5x, MACD hist <= 0
  EMA200 üstü + EMA200 yükseliyor | Funding < 0
  Ek bilgi: Hammer (yeşil) / Bullish Engulfing / Morning Star

TREND sistemi:
  BB_BREAK: ALL4_LOOSE + BB(15) ust bant kirilimi
  VOL3_BB:  ALL4_LOOSE + 3 barda hacim trendi + BB kirilimi

BİRİKİM sistemi:
  RSI 45-68, WR -65/-15, OBV↑, WT↑, KDJ-J↑, ADX<30↑, EMA200↑, Hacim 1-2x

NOT: BB Retest sistemi kaldırıldı.
"""

import asyncio
import json
import time
import threading
import os
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
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

STOCH_RSI_THRESH = float(os.getenv("STOCH_RSI_THRESH", "0.05"))
WR_THRESH        = float(os.getenv("WR_THRESH",        "-80"))
OBV_OSC_THRESH   = float(os.getenv("OBV_OSC_THRESH",   "-50"))
WT_THRESH        = float(os.getenv("WT_THRESH",        "-75"))
DIP_VOL_MULT     = float(os.getenv("DIP_VOL_MULT",     "1.5"))
STOP_PCT         = float(os.getenv("STOP_PCT",         "10.0"))
TREND_STOP_PCT   = float(os.getenv("TREND_STOP_PCT",   "10.0"))

SIGNAL_COOLDOWN_HOURS = int(os.getenv("SIGNAL_COOLDOWN_HOURS", "4"))
MIN_LIQUIDITY         = float(os.getenv("MIN_LIQUIDITY",       "1000000"))
MAX_SYMBOLS           = int(os.getenv("MAX_SYMBOLS",           "0"))

WS_STREAM_CHUNK = int(os.getenv("WS_STREAM_CHUNK", "120"))
BOOTSTRAP_BARS  = int(os.getenv("BOOTSTRAP_BARS",  "500"))
KEEP_BARS       = int(os.getenv("KEEP_BARS",       "300"))

BOOT_EVERY = 50
TR_TZ      = timezone(timedelta(hours=3))

IGNORED_COINS = set([
    # Leveraged tokens
    'UP/USDT','DOWN/USDT','BEAR/USDT','BULL/USDT',
    # Stablecoins
    'USDC/USDT','TUSD/USDT','FDUSD/USDT','DAI/USDT','USDP/USDT',
    'USDE/USDT','UST/USDT','USD/USDT','XUSD/USDT','USD1/USDT','BFUSD/USDT',
    'USTC/USDT','BUSD/USDT','FRAX/USDT','LUSD/USDT','GUSD/USDT','SUSD/USDT',
    'USDS/USDT','USDX/USDT','USDD/USDT','CUSD/USDT','OUSD/USDT','MUSD/USDT',
    'U/USDT',
    # Fiat
    'EUR/USDT','TRY/USDT','GBP/USDT','BRL/USDT','RUB/USDT',
    'AUD/USDT','BIDR/USDT','IDRT/USDT','VAI/USDT',
    # Wrapped tokens
    'PAXG/USDT','WBTC/USDT','WETH/USDT','WBNB/USDT','BETH/USDT',
    'BTCB/USDT','HBTC/USDT',
])
LEVERAGED_PATTERNS = ['UP','DOWN','BULL','BEAR','3L','3S','2L','2S','5L','5S','10L','10S']

# ============================================================
# 0.1) STATS
# ============================================================
stats           = Counter()
ws_1h_closes    = 0
tracked_symbols = []
signal_counter  = 0

def tr_now_str():
    return datetime.now(timezone.utc).astimezone(TR_TZ).strftime("%Y-%m-%d %H:%M:%S")

def print_summary():
    print("\n--- TARAMA OZETI ---", flush=True)
    print(f"Sembol       : {len(tracked_symbols)}", flush=True)
    print(f"1H kapanis   : {ws_1h_closes}", flush=True)
    print(f"Sinyal       : {stats.get('signal_sent', 0)}", flush=True)
    print(f"  Dip        : {stats.get('dip_sent', 0)}", flush=True)
    print(f"  Trend      : {stats.get('trend_sent', 0)}", flush=True)
    print(f"  Birikim    : {stats.get('birikim_sent', 0)}", flush=True)
    for k, lbl in [
        ("cooldown",         "Cooldown"),
        ("low_liquidity",    "Dusuk hacim"),
        ("data_missing",     "Veri yok"),
        ("filtered",         "Dip filtre eledi"),
        ("trend_filtered",   "Trend filtre eledi"),
        ("tr_yesil_degil",   "Trend: yesil degil"),
        ("tr_body_kucuk",    "Trend: body kucuk"),
        ("tr_false_breakout","Trend: tuzak mum"),
        ("birikim_filtered", "Birikim filtre eledi"),
        ("tr_kapanis_dusuk", "Trend: kapanis dusuk"),
        ("tr_hacim_dusuk",   "Trend: hacim dusuk"),
        ("tr_direnc",        "Trend: direnc kirilmadi"),
        ("tr_adx",           "Trend: ADX dusuk"),
        ("tr_ema50",         "Trend: EMA50 yakin"),
        ("tr_ema200",        "Trend: EMA200 yakin"),
        ("tr_atr",           "Trend: ATR kucuk"),
        ("tr_son3bar",       "Trend: son3bar az"),
        ("tr_bb",            "Trend: BB kirilmadi"),
    ]:
        v = stats.get(k, 0)
        if v:
            print(f"  {lbl:22s}: {v}", flush=True)
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

exchange_spot = ccxt.binance({
    "apiKey":  BINANCE_API_KEY   or None,
    "secret":  BINANCE_API_SECRET or None,
    "options": {"defaultType": "spot", "adjustForTimeDifference": True},
    "enableRateLimit": True,
    "timeout": 15000,
})
exchange_fut = ccxt.binance({
    "options": {"defaultType": "future", "adjustForTimeDifference": True},
    "enableRateLimit": True,
    "timeout": 15000,
})

# ============================================================
# 2) DATA STORE
# ============================================================
@dataclass
class SignalCandidate:
    symbol:   str
    result:   dict
    tr_time:  datetime
    sig_type: str = "dip"

bars_1h:          dict = {}
funding_cache:    dict = {}
last_signal_ts:   dict = {}
last_scan_result: dict = {}
all_signals:      list = []
btc_4h_cache:     dict = {"trend": "?", "ema50": None, "close": None, "updated": None}

# ============================================================
# 3) SEMBOL HAVUZU
# ============================================================
async def load_symbols_pool():
    await api_gate.call(exchange_spot.load_markets)
    syms = []
    for s, market in exchange_spot.markets.items():
        if not s.endswith("/USDT"): continue
        if not market.get("active", False): continue
        if not market.get("spot", False): continue
        if s in IGNORED_COINS: continue
        base = s.replace("/USDT", "")
        if any(base.endswith(p) for p in LEVERAGED_PATTERNS): continue
        syms.append(s)

    if not syms:
        return []

    volumes = {}
    for attempt in range(2):
        for i in range(0, len(syms), 100):
            part = syms[i:i + 100]
            try:
                res = await api_gate.call(exchange_spot.fetch_tickers, part)
                if isinstance(res, dict):
                    for k, v in res.items():
                        vol = float(v.get("quoteVolume", 0) or 0)
                        if vol > 0:
                            volumes[k] = vol
            except Exception:
                continue
        await asyncio.sleep(0.5)

    missing = [s for s in syms if volumes.get(s, 0) == 0]
    if missing:
        print(f"  Eksik hacim: {len(missing)} coin tek tek sorgulanıyor...", flush=True)
        for sym in missing[:50]:
            try:
                ticker = await api_gate.call(exchange_spot.fetch_ticker, sym)
                vol = float(ticker.get("quoteVolume", 0) or 0)
                if vol > 0:
                    volumes[sym] = vol
            except Exception:
                pass
            await asyncio.sleep(0.05)

    filtered_syms = [s for s in syms if volumes.get(s, 0) >= MIN_LIQUIDITY]
    sorted_syms = sorted(filtered_syms, key=lambda x: volumes.get(x, 0), reverse=True)
    print(f"Sembol filtresi: {len(syms)} toplam → {len(sorted_syms)} (min {MIN_LIQUIDITY/1e6:.1f}M USDT)", flush=True)
    return sorted_syms[:MAX_SYMBOLS] if MAX_SYMBOLS else sorted_syms

# ============================================================
# 4) FUNDING RATE
# ============================================================
async def fetch_funding_rate(symbol):
    sym_fut = symbol.replace("/USDT", "USDT")
    try:
        loop = asyncio.get_running_loop()
        data = await loop.run_in_executor(
            None, lambda: exchange_fut.fetch_funding_rate(sym_fut)
        )
        rate = data.get("fundingRate")
        funding_cache[symbol] = float(rate) if rate is not None else None
    except Exception:
        funding_cache[symbol] = None

async def refresh_btc_4h():
    try:
        df = await fetch_df("BTC/USDT", "4h", 100)
        if df is None or len(df) < 50:
            return
        c    = df["close"]
        e50  = c.ewm(span=50, adjust=False).mean()
        e200 = c.ewm(span=200, adjust=False).mean()
        last_c   = float(c.iloc[-1])
        last_e50 = float(e50.iloc[-1])
        last_e200= float(e200.iloc[-1])
        if last_c > last_e50 > last_e200:
            trend = "⬆️ Güçlü Yükseliş"
        elif last_c > last_e50:
            trend = "🟢 Yükseliş"
        elif last_c > last_e200:
            trend = "🟡 Karışık"
        else:
            trend = "🔴 Düşüş"
        btc_4h_cache["trend"]   = trend
        btc_4h_cache["ema50"]   = last_e50
        btc_4h_cache["close"]   = last_c
        btc_4h_cache["updated"] = datetime.now(timezone.utc).strftime("%H:%M")
        print(f"BTC 4H: {trend} | Fiyat:{last_c:.0f} EMA50:{last_e50:.0f}", flush=True)
    except Exception as e:
        print(f"BTC 4H hata: {e}", flush=True)

async def refresh_funding_cache(symbols):
    print("  Funding rate cache dolduruluyor...", flush=True)
    ok = 0
    for sym in symbols:
        await fetch_funding_rate(sym)
        if funding_cache.get(sym) is not None:
            ok += 1
        await asyncio.sleep(0.05)
    print(f"  → {ok}/{len(symbols)} sembolde funding rate", flush=True)

# ============================================================
# 5) VERİ + İNDİKATOR
# ============================================================
async def fetch_df(symbol, timeframe, limit):
    raw = await api_gate.call(
        exchange_spot.fetch_ohlcv, symbol, timeframe=timeframe, limit=limit
    )
    df = pd.DataFrame(raw, columns=["timestamp","open","high","low","close","volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df.set_index("timestamp", inplace=True)
    return df

def prepare_bars(df):
    df = df.copy()
    c, h, l, v = df["close"], df["high"], df["low"], df["volume"]

    h14 = h.rolling(14).max(); l14 = l.rolling(14).min()
    df["wr"] = -100 * (h14 - c) / (h14 - l14).replace(0, np.nan)

    d    = c.diff()
    gain = d.clip(lower=0).ewm(com=13, adjust=False).mean()
    loss = (-d).clip(lower=0).ewm(com=13, adjust=False).mean()
    rsi  = 100 - 100 / (1 + gain / loss.replace(0, np.nan))
    rsi_min = rsi.rolling(14).min(); rsi_max = rsi.rolling(14).max()
    stoch_k = (rsi - rsi_min) / (rsi_max - rsi_min).replace(0, np.nan)
    df["stoch_rsi"] = stoch_k.rolling(3).mean()

    obv    = (v * np.sign(c.diff()).fillna(0)).cumsum()
    obv_ma = obv.rolling(20).mean()
    df["obv_osc"] = (obv - obv_ma) / obv_ma.abs().replace(0, np.nan) * 100

    ap    = (h + l + c) / 3
    esa   = ap.ewm(span=10, adjust=False).mean()
    d_abs = (ap - esa).abs().ewm(span=10, adjust=False).mean()
    ci    = (ap - esa) / (0.015 * d_abs.replace(0, np.nan))
    df["wt"] = ci.ewm(span=21, adjust=False).mean()

    e12  = c.ewm(span=12, adjust=False).mean()
    e26  = c.ewm(span=26, adjust=False).mean()
    macd = e12 - e26
    df["macd_hist"] = macd - macd.ewm(span=9, adjust=False).mean()

    tr = pd.concat([h-l,(h-c.shift()).abs(),(l-c.shift()).abs()],axis=1).max(axis=1)
    df["atr"]    = tr.ewm(alpha=1/14, adjust=False).mean()
    df["vol_ma"] = v.rolling(20).mean()
    df["ema50"]  = c.ewm(span=50,  adjust=False).mean()
    df["ema200"] = c.ewm(span=200, adjust=False).mean()

    up  = h.diff(); dn = -l.diff()
    pdm = up.where((up>dn)&(up>0), 0.0)
    mdm = dn.where((dn>up)&(dn>0), 0.0)
    atr14 = tr.ewm(alpha=1/14, adjust=False).mean()
    pdi = 100*pdm.ewm(alpha=1/14,adjust=False).mean()/(atr14+1e-10)
    mdi = 100*mdm.ewm(alpha=1/14,adjust=False).mean()/(atr14+1e-10)
    dx  = (pdi-mdi).abs()/(pdi+mdi+1e-10)*100
    df["adx"] = dx.ewm(alpha=1/14, adjust=False).mean()

    bb_ma = c.rolling(15).mean(); bb_std = c.rolling(15).std()
    df["bb15_upper"] = bb_ma + 2.0 * bb_std

    low9  = l.rolling(9).min(); high9 = h.rolling(9).max()
    rsv   = (c-low9)/(high9-low9).replace(0,np.nan)*100
    K = rsv.ewm(com=2, adjust=False).mean()
    D = K.ewm(com=2, adjust=False).mean()
    df["kdj_j"] = 3*K - 2*D

    d_rsi = c.diff()
    g_rsi = d_rsi.clip(lower=0).ewm(com=13, adjust=False).mean()
    l_rsi = (-d_rsi).clip(lower=0).ewm(com=13, adjust=False).mean()
    df["rsi"] = 100 - 100/(1+g_rsi/l_rsi.replace(0, np.nan))

    def percentrank(series, n=100):
        def _pr(x):
            window = x[-n:] if len(x) >= n else x
            cur = window[-1]
            return round(sum(1 for v in window[:-1] if v < cur) / max(len(window)-1, 1) * 100, 1)
        return series.rolling(n, min_periods=10).apply(lambda x: _pr(x), raw=True)
    df["atr_pct"] = percentrank(df["atr"], 100)

    def hma(series, n):
        half   = max(int(n/2), 1)
        sqrt_n = max(int(n**0.5), 1)
        wma_half = series.ewm(span=half*2-1, adjust=False).mean()
        wma_full = series.ewm(span=n*2-1, adjust=False).mean()
        raw = 2*wma_half - wma_full
        return raw.ewm(span=sqrt_n*2-1, adjust=False).mean()
    df["hma15"] = hma(c, 15)

    return df.dropna(subset=["stoch_rsi","wr","obv_osc","wt","macd_hist",
                              "atr","ema50","ema200","adx","bb15_upper","rsi","kdj_j"])

# ============================================================
# 6) MUM FORMASYONLARI (ek bilgi — zorunlu değil)
# ============================================================
def is_hammer(df, i):
    """
    Yeşil Hammer: kapanış > açılış, alt gölge >= gövde×2,
    üst gölge küçük, gövde mumun üst %40'ında.
    """
    if i < 1: return False
    try:
        o = float(df["open"].iloc[i])
        c = float(df["close"].iloc[i])
        h = float(df["high"].iloc[i])
        l = float(df["low"].iloc[i])
    except: return False

    if c <= o: return False          # yeşil mum şartı
    body       = c - o
    rng        = h - l
    if rng <= 0 or body <= 0: return False
    upper_wick = h - c
    lower_wick = o - l
    if lower_wick < body * 2.0: return False
    if upper_wick > body * 0.5: return False
    if (o - l) / rng < 0.55:   return False
    return True

def is_engulfing(df, i):
    """
    Bullish Engulfing: önceki kırmızı mumu tamamen yutan yeşil mum.
    """
    if i < 1: return False
    try:
        o0 = float(df["open"].iloc[i]);   c0 = float(df["close"].iloc[i])
        o1 = float(df["open"].iloc[i-1]); c1 = float(df["close"].iloc[i-1])
    except: return False
    body0 = abs(c0 - o0); body1 = abs(c1 - o1)
    if body1 <= 0: return False
    return (c1 < o1           # önceki kırmızı
            and c0 > o0       # şimdiki yeşil
            and o0 <= c1      # açılış önceki kapanışın altında
            and c0 >= o1      # kapanış önceki açılışın üstünde
            and body0 >= body1 * 0.8)

def is_morning_star(df, i):
    """
    Morning Star: büyük kırmızı → küçük gövde → büyük yeşil
    """
    if i < 2: return False
    try:
        o0 = float(df["open"].iloc[i]);   c0 = float(df["close"].iloc[i])
        o1 = float(df["open"].iloc[i-1]); c1 = float(df["close"].iloc[i-1])
        o2 = float(df["open"].iloc[i-2]); c2 = float(df["close"].iloc[i-2])
    except: return False
    b0 = abs(c0-o0); b1 = abs(c1-o1); b2 = abs(c2-o2)
    avg = (b0+b1+b2)/3 if (b0+b1+b2) > 0 else 1
    mid2 = o2 - b2/2
    return (c2 < o2 and b2 >= avg*1.0
            and b1 <= avg*0.5
            and c0 > o0 and b0 >= avg*1.0
            and c0 >= mid2)

def detect_candle(df, i):
    """
    Mum formasyonu tespiti — öncelik sırası ile.
    Döner: formasyon adı veya boş string.
    """
    if is_morning_star(df, i): return "🌅 Morning Star"
    if is_engulfing(df, i):    return "🟢 Bullish Engulfing"
    if is_hammer(df, i):       return "🔨 Hammer"
    return ""

# ============================================================
# 7) SİNYAL KRİTERLERİ
# ============================================================
def check_dip_signal(df, symbol):
    """Dip sistemi — orijinal parametreler, BTC filtresi yok"""
    if len(df) < 60:
        return None

    last  = df.iloc[-2]
    entry = float(df.iloc[-1]["close"])

    def sf(col):
        v = last.get(col, np.nan)
        return None if pd.isna(v) else float(v)

    stoch = sf("stoch_rsi")
    wr    = sf("wr")
    obv   = sf("obv_osc")
    wt    = sf("wt")
    hist  = sf("macd_hist")

    if None in (stoch, wr, obv, wt, hist): return None
    if stoch >= STOCH_RSI_THRESH: return None
    if wr    >= WR_THRESH:        return None
    if obv   >= OBV_OSC_THRESH:   return None
    if wt    >= WT_THRESH:        return None
    if hist  >  0:                return None

    e200      = sf("ema200")
    e200_prev = float(df["ema200"].iloc[-3]) if len(df) >= 3 else None
    if e200 is None or entry <= e200:
        stats["filtered"] += 1; return None
    if e200_prev is not None and e200 <= e200_prev:
        stats["filtered"] += 1; return None

    vol = sf("volume") if "volume" in last.index else None
    vm  = sf("vol_ma")
    if vol is not None and vm is not None and vm > 0:
        if vol < vm * DIP_VOL_MULT: return None

    funding = funding_cache.get(symbol)
    # Funding zorunlu değil — negatifse öncelikli sinyal (💰)
    funding_neg = funding is not None and funding < 0

    atr_val  = sf("atr")
    stop_fix = round(entry * (1 - STOP_PCT / 100), 8)
    if atr_val and atr_val > 0:
        vol_ratio_atr = atr_val / entry
        if vol_ratio_atr > 0.04:   atr_mult = 2.0
        elif vol_ratio_atr > 0.02: atr_mult = 1.8
        else:                      atr_mult = 1.4
        stop_atr   = round(entry - atr_val * atr_mult, 8)
        floor_stop = round(entry * 0.97, 8)
        cap_stop   = round(entry * 0.88, 8)
        stop_use   = min(max(stop_atr, cap_stop), floor_stop)
        tp1 = round(entry + atr_val * 1.5, 8)
        tp2 = round(entry + atr_val * 3.0, 8)
    else:
        stop_use = stop_fix
        tp1 = round(entry * 1.05, 8)
        tp2 = round(entry * 1.10, 8)

    vol_cur = sf("volume") if "volume" in last.index else None
    vol_ma  = sf("vol_ma")
    vol_mult_val = round(vol_cur / vol_ma, 1) if (vol_cur and vol_ma and vol_ma > 0) else None

    # Mum formasyonu — ek bilgi
    candle = detect_candle(df, len(df)-2)

    return {
        "symbol":      symbol,
        "type":        "dip",
        "entry":       round(entry, 8),
        "stop":        stop_use,
        "tp1":         tp1,
        "tp2":         tp2,
        "atr":         round(atr_val, 8) if atr_val else None,
        "vol_mult":    vol_mult_val,
        "stoch_rsi":   round(stoch, 4),
        "wr":          round(wr,    2),
        "obv_osc":     round(obv,   2),
        "wt":          round(wt,    2),
        "macd_hist":   round(hist,  8),
        "funding":     round(funding, 6) if funding is not None else None,
        "funding_neg": funding_neg,
        "candle":      candle,
        "atr_pct":     round(float(df["atr_pct"].iloc[-2]), 1) if "atr_pct" in df.columns else None,
        "hma15":       round(float(df["hma15"].iloc[-2]), 8)   if "hma15"  in df.columns else None,
    }

def check_birikim_signal(df, symbol):
    """Birikim/Momentum Sinyali — orijinal parametreler"""
    if len(df) < 60: return None

    bar  = df.iloc[-2]; bar1 = df.iloc[-3]; bar2 = df.iloc[-4]
    entry = float(df.iloc[-1]["close"])

    def sf(row, col):
        v = row.get(col, np.nan)
        return None if pd.isna(v) else float(v)

    rsi  = sf(bar,  "rsi")
    wr   = sf(bar,  "wr")
    obv  = sf(bar,  "obv_osc"); obv1 = sf(bar1, "obv_osc"); obv2 = sf(bar2, "obv_osc")
    wt   = sf(bar,  "wt");      wt1  = sf(bar1, "wt")
    kdj  = sf(bar,  "kdj_j");   kdj1 = sf(bar1, "kdj_j")
    vol  = sf(bar,  "volume") if "volume" in bar.index else None
    vm   = sf(bar,  "vol_ma")
    e200 = sf(bar,  "ema200")
    atr  = sf(bar,  "atr")
    adx  = sf(bar,  "adx");     adx1 = sf(bar1, "adx")

    if None in (rsi, wr, obv, obv1, obv2, wt, wt1, kdj, kdj1, e200, atr, adx, adx1):
        return None

    if not (45 <= rsi <= 68):       return None
    if not (-65 <= wr <= -15):      return None
    if obv <= 0:                    return None
    if not (obv > obv1 > obv2):     return None
    if wt <= 0 or wt <= wt1:        return None
    if kdj < 35 or kdj <= kdj1:     return None
    if adx >= 30 or adx <= adx1:    return None
    if entry <= e200:               return None

    if vol is not None and vm is not None and vm > 0:
        vr = vol / vm
        if vr > 2.0 or vr < 1.0: return None

    stop_atr   = round(entry - atr * 1.5, 8)
    floor_stop = round(entry * 0.97, 8)
    cap_stop   = round(entry * 0.90, 8)
    stop_use   = min(max(stop_atr, cap_stop), floor_stop)
    tp1 = round(entry + atr * 2.0, 8)
    tp2 = round(entry + atr * 4.0, 8)

    vol_mult = round(vol/vm, 1) if (vol and vm and vm > 0) else None
    atr_pct  = round(atr/entry*100, 2)
    candle   = detect_candle(df, len(df)-2)

    return {
        "symbol":   symbol, "type": "birikim",
        "entry":    round(entry, 8), "stop": stop_use, "tp1": tp1, "tp2": tp2,
        "rsi":      round(rsi, 1), "wr": round(wr, 1),
        "obv_osc":  round(obv, 1), "wt": round(wt, 1), "kdj_j": round(kdj, 1),
        "vol_mult": vol_mult, "atr_pct": atr_pct, "candle": candle,
        "atr_pct_risk": round(float(df["atr_pct"].iloc[-2]), 1) if "atr_pct" in df.columns else None,
        "hma15":    round(float(df["hma15"].iloc[-2]), 8) if "hma15" in df.columns else None,
        "funding":  funding_cache.get(symbol),
        "funding_neg": funding_cache.get(symbol) is not None and funding_cache.get(symbol) < 0,
    }

def check_trend_signal(df, symbol):
    """Trend sistemi — BB_BREAK + VOL3_BB, BTC filtresi yok"""
    if len(df) < 60: return None

    bar   = df.iloc[-2]; prev = df.iloc[-3]
    entry = float(df.iloc[-1]["close"])

    def sf(row, col):
        v = row.get(col, np.nan)
        return None if pd.isna(v) else float(v)

    o   = sf(bar, "open");  c   = sf(bar, "close")
    h   = sf(bar, "high");  l   = sf(bar, "low")
    vol = sf(bar, "volume"); atr = sf(bar, "atr")
    vm  = sf(bar, "vol_ma"); e50 = sf(bar, "ema50"); e200 = sf(bar, "ema200")
    adx = sf(bar, "adx");   bbu = sf(bar, "bb15_upper"); pbbu = sf(prev, "bb15_upper")
    adx3 = sf(df.iloc[-5], "adx")

    if None in (o, c, h, l, vol, atr, vm, e50, e200, adx, bbu, pbbu, adx3): return None
    if atr <= 0 or vm <= 0 or e50 <= 0 or e200 <= 0: return None

    if c <= o:                     stats["tr_yesil_degil"]   += 1; return None
    body = abs(c - o)
    if body < atr * 1.2:           stats["tr_body_kucuk"]    += 1; return None
    if body > atr * 2.0:           stats["tr_false_breakout"]+= 1; return None
    rng = h - l
    if rng <= 0: return None
    if (c - l) / rng < 0.65:      stats["tr_kapanis_dusuk"] += 1; return None
    if vol < vm * 1.6:             stats["tr_hacim_dusuk"]   += 1; return None

    highs50 = df["high"].iloc[-52:-2].values
    if len(highs50) < 10: return None
    if c < float(np.max(highs50)) * 1.01: stats["tr_direnc"] += 1; return None

    if adx <= 20:   stats["tr_adx"]   += 1; return None
    if adx <= adx3: stats["tr_adx"]   += 1; return None
    if (c - e50)  / e50  * 100 <= 8.0:  stats["tr_ema50"]  += 1; return None
    if (c - e200) / e200 * 100 <= 12.0: stats["tr_ema200"] += 1; return None
    if atr / c * 100 <= 1.5:            stats["tr_atr"]    += 1; return None

    c3 = sf(df.iloc[-5], "close")
    if c3 is None or c3 <= 0: return None
    if (c - c3) / c3 * 100 <= 5.0: stats["tr_son3bar"] += 1; return None

    prev2     = df.iloc[-4]
    prev2_bbu = sf(prev2, "bb15_upper")
    prev_below  = sf(prev,  "close") < pbbu
    prev2_below = prev2_bbu is not None and sf(prev2, "close") < prev2_bbu
    if not (prev_below or prev2_below): stats["tr_bb"] += 1; return None
    if c < bbu:                         stats["tr_bb"] += 1; return None

    trend_subtype = "BB_BREAK"
    vol3_ok = False
    if len(df) >= 6:
        v_win = df["volume"].iloc[-5:-2].values
        if len(v_win) == 3:
            x     = np.arange(3, dtype=float)
            slope = np.polyfit(x, v_win, 1)[0]
            if slope > 0 and v_win[-1] >= np.mean(v_win):
                vol3_ok = True; trend_subtype = "VOL3_BB"

    vol_ratio_atr = atr / entry
    if vol_ratio_atr > 0.04:   atr_mult_t = 2.0
    elif vol_ratio_atr > 0.02: atr_mult_t = 1.8
    else:                      atr_mult_t = 1.4
    stop_atr   = round(entry - atr * atr_mult_t, 8)
    floor_stop = round(entry * 0.97, 8)
    cap_stop   = round(entry * 0.88, 8)
    stop_use   = min(max(stop_atr, cap_stop), floor_stop)
    tp1 = round(entry + atr * 1.5, 8)
    tp2 = round(entry + atr * 3.0, 8)
    vol_mult_val = round(vol / vm, 1) if vm > 0 else None

    return {
        "symbol":      symbol, "type": "trend", "subtype": trend_subtype,
        "entry":       round(entry, 8), "stop": stop_use, "tp1": tp1, "tp2": tp2,
        "vol_mult":    vol_mult_val,
        "ema50_dist":  round((c-e50)/e50*100, 1),
        "ema200_dist": round((c-e200)/e200*100, 1),
        "adx":         round(adx, 1),
        "atr_ratio":   round(atr/c*100, 2),
        "vol3_ok":     vol3_ok,
        "funding":     funding_cache.get(symbol),
        "funding_neg": False,
        "atr_pct":     round(float(df["atr_pct"].iloc[-2]), 1) if "atr_pct" in df.columns else None,
        "hma15":       round(float(df["hma15"].iloc[-2]), 8)   if "hma15"  in df.columns else None,
    }

# ============================================================
# 8) TELEGRAM
# ============================================================
def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
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

def build_dip_message(r, tr_time, sig_num):
    now         = tr_time.strftime("%d/%m/%Y %H:%M")
    sym         = r["symbol"].replace("/USDT", "")
    funding_neg = r.get("funding_neg", False)
    funding_val = r.get("funding")
    icon        = "💰" if funding_neg else "🔵"
    tp1         = r.get("tp1"); tp2 = r.get("tp2")
    vol_mult    = r.get("vol_mult")
    btc_trend   = btc_4h_cache.get("trend", "?")
    candle      = r.get("candle", "")

    hist_val = r.get("macd_hist", 0)
    hist_str = f"{hist_val:.6f}" if abs(hist_val) < 0.0001 else (
               f"{hist_val:.5f}" if abs(hist_val) < 0.01 else f"{hist_val:.4f}")

    lines = [
        f"🕐 {now}",
        "",
        f"{icon} <b>#{sym}/USDT  •  DİP DÖNÜŞÜ  •  1H</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        f"💵 <b>Giriş</b>    {fmt_price(r['entry'])}",
        f"🛡️ <b>Stop</b>     {fmt_price(r['stop'])}  (Adaptive ATR)",
        f"🎯 <b>TP1</b>      {fmt_price(tp1)}  (+{round((tp1/r['entry']-1)*100,1) if tp1 else '?'}%)",
        f"🎯 <b>TP2</b>      {fmt_price(tp2)}  (+{round((tp2/r['entry']-1)*100,1) if tp2 else '?'}%)",
        "━━━━━━━━━━━━━━━━━━━━",
        "📊 <b>İndikatörler</b>",
        f"<b>StochRSI</b>   {r['stoch_rsi']:.4f}",
        f"<b>W%R</b>        {r['wr']:.1f}",
        f"<b>OBV_OSC</b>    {r['obv_osc']:.1f}",
        f"<b>WaveTrend</b>  {r['wt']:.1f}",
        f"<b>MACD Hist</b>  {hist_str}",
    ]
    if vol_mult is not None:
        lines.append(f"<b>Hacim</b>      {vol_mult}x ortalama")
    if funding_val is not None:
        fund_icon = "  💰" if funding_neg else ""
        lines.append(f"<b>Funding</b>    {funding_val:+.4f}%{fund_icon}")
    if candle:
        lines.append(f"<b>Formasyon</b>  {candle}  ✅")

    atr_pct = r.get("atr_pct"); hma15 = r.get("hma15")
    if atr_pct is not None:
        if atr_pct >= 80:   vol_risk = f"⚠️ Yüksek (%{atr_pct:.0f})"
        elif atr_pct >= 50: vol_risk = f"🟡 Orta (%{atr_pct:.0f})"
        else:               vol_risk = f"🟢 Düşük (%{atr_pct:.0f})"
    else:
        vol_risk = "?"
    lines += [
        "━━━━━━━━━━━━━━━━━━━━",
        f"<b>BTC 4H</b>     {btc_trend}",
        f"<b>Vol.Risk</b>   {vol_risk}",
    ]
    if hma15:
        lines.append(f"<b>HMA15</b>      {fmt_price(hma15)}  (trailing stop ref.)")
    lines += ["━━━━━━━━━━━━━━━━━━━━", f"⏱ Cooldown: {SIGNAL_COOLDOWN_HOURS}H  |  #{sig_num} sinyal"]
    return "\n".join(lines)

def build_birikim_message(r, tr_time, sig_num):
    now      = tr_time.strftime("%d/%m/%Y %H:%M")
    sym      = r["symbol"].replace("/USDT", "")
    tp1      = r.get("tp1"); tp2 = r.get("tp2")
    vol_mult = r.get("vol_mult")
    btc_trend= btc_4h_cache.get("trend", "?")
    atr_pct  = r.get("atr_pct_risk"); hma15 = r.get("hma15")
    funding_val = r.get("funding"); funding_neg = r.get("funding_neg", False)
    candle   = r.get("candle", "")

    if atr_pct is not None:
        if atr_pct >= 80:   vol_risk = f"⚠️ Yüksek (%{atr_pct:.0f})"
        elif atr_pct >= 50: vol_risk = f"🟡 Orta (%{atr_pct:.0f})"
        else:               vol_risk = f"🟢 Düşük (%{atr_pct:.0f})"
    else:
        vol_risk = "?"

    lines = [
        f"🕐 {now}", "",
        f"🟣 <b>#{sym}/USDT  •  BİRİKİM  •  1H</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        f"💵 <b>Giriş</b>    {fmt_price(r['entry'])}",
        f"🛡️ <b>Stop</b>     {fmt_price(r['stop'])}  (Adaptive ATR)",
        f"🎯 <b>TP1</b>      {fmt_price(tp1)}  (+{round((tp1/r['entry']-1)*100,1) if tp1 else '?'}%)",
        f"🎯 <b>TP2</b>      {fmt_price(tp2)}  (+{round((tp2/r['entry']-1)*100,1) if tp2 else '?'}%)",
        "━━━━━━━━━━━━━━━━━━━━",
        "📊 <b>Göstergeler</b>",
        f"<b>RSI</b>        {r['rsi']:.1f}",
        f"<b>W%R</b>        {r['wr']:.1f}",
        f"<b>OBV_OSC</b>    {r['obv_osc']:.1f} ↑",
        f"<b>WaveTrend</b>  {r['wt']:.1f} ↑",
        f"<b>KDJ-J</b>      {r['kdj_j']:.1f} ↑",
    ]
    if vol_mult: lines.append(f"<b>Hacim</b>      {vol_mult}x ortalama")
    if funding_val is not None:
        fund_icon = "  💰" if funding_neg else ""
        lines.append(f"<b>Funding</b>    {funding_val:+.4f}%{fund_icon}")
    if candle:
        lines.append(f"<b>Formasyon</b>  {candle}  ✅")
    lines += [
        "━━━━━━━━━━━━━━━━━━━━",
        f"<b>BTC 4H</b>     {btc_trend}",
        f"<b>Vol.Risk</b>   {vol_risk}",
    ]
    if hma15:
        lines.append(f"<b>HMA15</b>      {fmt_price(hma15)}  (trailing ref.)")
    lines += ["━━━━━━━━━━━━━━━━━━━━", f"⏱ Cooldown: {SIGNAL_COOLDOWN_HOURS}H  |  #{sig_num} sinyal"]
    return "\n".join(lines)

def build_trend_message(r, tr_time, sig_num):
    now      = tr_time.strftime("%d/%m/%Y %H:%M")
    sym      = r["symbol"].replace("/USDT", "")
    vol3     = r.get("vol3_ok", False)
    quality  = "⭐ VOL3+BB" if vol3 else "BB Kırılım"
    tp1      = r.get("tp1"); tp2 = r.get("tp2")
    vol_mult = r.get("vol_mult")
    btc_trend= btc_4h_cache.get("trend", "?")

    lines = [
        f"🕐 {now}", "",
        f"📈 <b>#{sym}/USDT  •  TREND  •  1H  •  {quality}</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        f"💵 <b>Giriş</b>      {fmt_price(r['entry'])}",
        f"🛡️ <b>Stop</b>       {fmt_price(r['stop'])}  (Adaptive ATR)",
        f"🎯 <b>TP1</b>        {fmt_price(tp1)}  (+{round((tp1/r['entry']-1)*100,1) if tp1 else '?'}%)",
        f"🎯 <b>TP2</b>        {fmt_price(tp2)}  (+{round((tp2/r['entry']-1)*100,1) if tp2 else '?'}%)",
        "━━━━━━━━━━━━━━━━━━━━",
        "📊 <b>Trend Göstergeleri</b>",
        f"<b>EMA50 Uzak</b>   +{r['ema50_dist']:.1f}%",
        f"<b>EMA200 Uzak</b>  +{r['ema200_dist']:.1f}%",
        f"<b>ADX</b>          {r['adx']:.1f}",
        f"<b>ATR/Fiyat</b>    %{r['atr_ratio']:.2f}",
    ]
    if vol_mult is not None:
        lines.append(f"<b>Hacim</b>        {vol_mult}x ortalama")
    atr_pct = r.get("atr_pct"); hma15 = r.get("hma15")
    if atr_pct is not None:
        if atr_pct >= 80:   vol_risk = f"⚠️ Yüksek (%{atr_pct:.0f})"
        elif atr_pct >= 50: vol_risk = f"🟡 Orta (%{atr_pct:.0f})"
        else:               vol_risk = f"🟢 Düşük (%{atr_pct:.0f})"
    else:
        vol_risk = "?"
    lines += [
        "━━━━━━━━━━━━━━━━━━━━",
        f"<b>BTC 4H</b>       {btc_trend}",
        f"<b>Vol.Risk</b>     {vol_risk}",
    ]
    if hma15:
        lines.append(f"<b>HMA15</b>        {fmt_price(hma15)}  (trailing stop ref.)")
    lines += ["━━━━━━━━━━━━━━━━━━━━", f"⏱ Cooldown: {SIGNAL_COOLDOWN_HOURS}H  |  #{sig_num} sinyal"]
    return "\n".join(lines)

# ============================================================
# 9) BOOTSTRAP
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
    print(f"Bootstrap basladi | {len(symbols)} sembol", flush=True)
    for i, sym in enumerate(symbols, 1):
        if i % BOOT_EVERY == 0:
            print(f"  -> {i}/{len(symbols)}", flush=True)
        if await bootstrap_symbol(sym):
            ok += 1
    print(f"Bootstrap bitti | {ok}/{len(symbols)}", flush=True)

# ============================================================
# 10) SİNYAL WORKER
# ============================================================
async def signal_worker(candidate_queue):
    global signal_counter
    while True:
        sig = await candidate_queue.get()
        try:
            symbol   = sig.symbol
            result   = sig.result
            tr_time  = sig.tr_time
            sig_type = sig.sig_type

            try:
                ticker    = await api_gate.call(exchange_spot.fetch_ticker, symbol)
                liquidity = float(ticker.get("quoteVolume", 0) or 0)
                recent_vol = float(ticker.get("baseVolume", 0) or 0) * float(ticker.get("last", 0) or 0)
                if liquidity < MIN_LIQUIDITY and recent_vol < MIN_LIQUIDITY / 24:
                    stats["low_liquidity"] += 1
                    candidate_queue.task_done()
                    continue
            except Exception:
                pass

            await fetch_funding_rate(symbol)
            result["funding"]     = funding_cache.get(symbol)
            result["funding_neg"] = result["funding"] is not None and result["funding"] < 0

            signal_counter += 1

            if sig_type == "trend":
                msg  = build_trend_message(result, tr_time, signal_counter)
                stats["trend_sent"] += 1
                icon = "📈"
            elif sig_type == "birikim":
                msg  = build_birikim_message(result, tr_time, signal_counter)
                stats["birikim_sent"] += 1
                icon = "🟣"
            else:
                msg  = build_dip_message(result, tr_time, signal_counter)
                stats["dip_sent"] += 1
                icon = "💰" if result.get("funding_neg") else "🔵"

            send_telegram(msg)

            last_signal_ts.setdefault(symbol, {})[sig_type] = tr_time.replace(tzinfo=None)
            result["time"]     = tr_time.strftime("%Y-%m-%d %H:%M")
            result["sig_type"] = sig_type
            all_signals.insert(0, result)
            if len(all_signals) > 200: all_signals.pop()

            stats["signal_sent"] += 1
            log_signal(result, tr_time)

            subtype_str = result.get("subtype","") if sig_type=="trend" else ""
            print(
                f"SINYAL {icon} [{sig_type.upper()}{' '+subtype_str if subtype_str else ''}] "
                f"{symbol} | giriş:{fmt_price(result['entry'])}"
                + (f" | {result.get('candle','')}" if result.get('candle') else ""),
                flush=True
            )

        except Exception as e:
            print(f"Worker hata: {str(e)[:100]}", flush=True)
        finally:
            candidate_queue.task_done()

# ============================================================
# 11) PERFORMANS TAKİP
# ============================================================
SIGNAL_LOG_PATH = "/tmp/signal_log.json"

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

def log_signal(result, tr_time):
    entry = {
        "id":          f"{result['symbol']}_{int(tr_time.timestamp())}",
        "symbol":      result["symbol"],
        "entry":       result["entry"],
        "stop":        result["stop"],
        "tp1":         result.get("tp1"),
        "tp2":         result.get("tp2"),
        "sig_type":    result.get("sig_type", "dip"),
        "subtype":     result.get("subtype", ""),
        "funding_neg": result.get("funding_neg", False),
        "candle":      result.get("candle", ""),
        "time":        tr_time.isoformat(),
        "status":      "open",
        "peak_pct":    0.0,
        "tp1_hit":     False,
        "tp2_hit":     False,
        "close_time":  None,
        "close_price": None,
        "close_ret":   None,
    }
    signal_log.insert(0, entry)
    if len(signal_log) > 500: signal_log.pop()
    save_signal_log(signal_log)
    pending_by_symbol.setdefault(result["symbol"], []).append(entry)

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

        cur_ret = (bar_high - e) / e * 100
        if cur_ret > entry["peak_pct"]:
            entry["peak_pct"] = round(cur_ret, 2)

        tp1 = entry.get("tp1"); tp2 = entry.get("tp2")
        if tp2 and bar_high >= tp2 and not entry.get("tp2_hit"): entry["tp2_hit"] = True
        if tp1 and bar_high >= tp1 and not entry.get("tp1_hit"): entry["tp1_hit"] = True

        if bar_low <= stp:
            entry.update({"status":"loss","close_time":bar_time.isoformat(),
                          "close_price":round(stp,8),"close_ret":round((stp-e)/e*100,2)})
            to_close.append(entry); continue

        if tp2 and bar_high >= tp2:
            entry.update({"status":"win","close_time":bar_time.isoformat(),
                          "close_price":round(tp2,8),"close_ret":round((tp2-e)/e*100,2)})
            to_close.append(entry); continue

        if elapsed_h >= 24:
            entry.update({"status":"expired","close_time":bar_time.isoformat(),
                          "close_price":round(bar_close,8),"close_ret":round((bar_close-e)/e*100,2)})
            to_close.append(entry)

    if to_close:
        for e in to_close:
            pending_by_symbol[symbol].remove(e)
        if not pending_by_symbol[symbol]:
            del pending_by_symbol[symbol]
        save_signal_log(signal_log)

def perf_summary():
    closed       = [s for s in signal_log if s["status"] in ("loss","expired")]
    dip_closed   = [s for s in closed if s.get("sig_type","dip")=="dip"]
    trend_closed = [s for s in closed if s.get("sig_type")=="trend"]
    def avg_peak(lst):
        peaks = [s["peak_pct"] for s in lst if s.get("peak_pct") is not None]
        return round(sum(peaks)/len(peaks), 2) if peaks else 0.0
    return {
        "total":          len(signal_log),
        "open":           sum(1 for s in signal_log if s["status"]=="open"),
        "closed":         len(closed),
        "losses":         sum(1 for s in closed if s["status"]=="loss"),
        "expired":        sum(1 for s in closed if s["status"]=="expired"),
        "avg_peak":       avg_peak(closed),
        "dip_total":      len([s for s in signal_log if s.get("sig_type","dip")=="dip"]),
        "dip_avg_peak":   avg_peak(dip_closed),
        "trend_total":    len([s for s in signal_log if s.get("sig_type")=="trend"]),
        "trend_avg_peak": avg_peak(trend_closed),
    }

# ============================================================
# 12) MUM KAPANIŞINI İŞLE
# ============================================================
async def on_1h_close(symbol, o, h, l, c, v, ts_ms, candidate_queue):
    global ws_1h_closes
    ws_1h_closes += 1
    beat(symbol=symbol)

    bar_time = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    check_pending_for_symbol(symbol, h, l, c, bar_time)

    df = bars_1h.get(symbol)
    if df is None or len(df) < 60:
        stats["data_missing"] += 1; return

    tstamp = pd.to_datetime(ts_ms, unit="ms", utc=True)
    df.loc[tstamp, ["open","high","low","close","volume"]] = [o, h, l, c, v]
    df = df.sort_index()
    if len(df) > KEEP_BARS: df = df.iloc[-KEEP_BARS:]
    df = prepare_bars(df)
    bars_1h[symbol] = df

    tr_now = datetime.now(timezone.utc).astimezone(TR_TZ)
    sig_ts = last_signal_ts.get(symbol, {})

    def cooldown_ok(key):
        last = sig_ts.get(key)
        if not last: return True
        hrs = (tr_now.replace(tzinfo=None) - last.replace(tzinfo=None)).total_seconds() / 3600
        if hrs < SIGNAL_COOLDOWN_HOURS:
            stats["cooldown"] += 1; return False
        return True

    # DİP
    if cooldown_ok("dip"):
        result = check_dip_signal(df, symbol)
        if result:
            await candidate_queue.put(SignalCandidate(symbol=symbol, result=result,
                                                       tr_time=tr_now, sig_type="dip"))
        else:
            stats["filtered"] += 1

    # TREND
    if cooldown_ok("trend"):
        result = check_trend_signal(df, symbol)
        if result:
            await candidate_queue.put(SignalCandidate(symbol=symbol, result=result,
                                                       tr_time=tr_now, sig_type="trend"))
        else:
            stats["trend_filtered"] += 1

    # BİRİKİM
    if cooldown_ok("birikim"):
        result = check_birikim_signal(df, symbol)
        if result:
            await candidate_queue.put(SignalCandidate(symbol=symbol, result=result,
                                                       tr_time=tr_now, sig_type="birikim"))
        else:
            stats["birikim_filtered"] += 1

# ============================================================
# 13) WEBSOCKET
# ============================================================
def to_ws(symbol):
    return symbol.replace("/", "").lower()

async def ws_chunk(symbols, candidate_queue):
    streams = "/".join([f"{to_ws(s)}@kline_1h" for s in symbols])
    url     = f"wss://stream.binance.com:9443/stream?streams={streams}"
    retry   = 0
    while True:
        try:
            async with websockets.connect(url, ping_interval=None,
                                           open_timeout=30, close_timeout=10,
                                           max_size=10*1024*1024) as ws:
                retry = 0
                print(f"WS baglandi ({len(symbols)} sembol)", flush=True)

                async def keep_alive(ws):
                    while True:
                        await asyncio.sleep(20)
                        try:
                            pong = await ws.ping()
                            await asyncio.wait_for(pong, timeout=10)
                        except Exception: break

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
                        sym = data.get("data",{}).get("s","").upper().replace("USDT","/USDT")
                        try:
                            await on_1h_close(sym,
                                float(k["o"]),float(k["h"]),float(k["l"]),float(k["c"]),
                                float(k["v"]),int(k["t"]), candidate_queue)
                        except Exception as e:
                            print(f"on_1h_close hata [{sym}]: {str(e)[:80]}", flush=True)
                finally:
                    ping_task.cancel()
        except Exception as e:
            retry  += 1
            backoff = min(60, 5 * (2 ** min(retry, 4)))
            print(f"WS koptu -> {backoff}s: {str(e)[:50]}", flush=True)
            await asyncio.sleep(backoff)

async def ws_all(symbols, candidate_queue):
    tasks = []
    for i in range(0, len(symbols), WS_STREAM_CHUNK):
        chunk = symbols[i:i+WS_STREAM_CHUNK]
        tasks.append(asyncio.create_task(ws_chunk(chunk, candidate_queue)))
    await asyncio.gather(*tasks)

# ============================================================
# 14) FLASK DASHBOARD
# ============================================================
flask_app  = Flask(__name__)
bot_status = {"status": "BOOT"}

import logging
logging.getLogger("werkzeug").setLevel(logging.ERROR)
flask_app.logger.disabled = True

heartbeat  = {"last": tr_now_str(), "epoch": time.time(), "symbol": "?"}
_last_beat = 0.0

def beat(symbol=None):
    global _last_beat
    now = time.time()
    if now - _last_beat >= 10:
        heartbeat["last"] = tr_now_str(); heartbeat["epoch"] = now
        if symbol: heartbeat["symbol"] = symbol
        _last_beat = now

def heartbeat_pinger():
    while True:
        heartbeat["last"] = tr_now_str(); heartbeat["epoch"] = time.time()
        time.sleep(15)

def watchdog_thread():
    while True:
        stale = time.time() - float(heartbeat.get("epoch", 0))
        if stale > 600:
            print(f"WATCHDOG: {int(stale)}s stale", flush=True); os._exit(1)
        time.sleep(10)

def clean_json(obj):
    if isinstance(obj, dict):  return {k: clean_json(v) for k, v in obj.items()}
    if isinstance(obj, list):  return [clean_json(i) for i in obj]
    if isinstance(obj, float):
        if obj != obj or obj in (float("inf"), float("-inf")): return None
        return obj
    if hasattr(obj, "item"): return clean_json(obj.item())
    return obj

@flask_app.route("/")
def home():
    now      = datetime.now(TR_TZ).strftime("%H:%M:%S")
    sig_rows = ""
    for s in all_signals[:30]:
        st      = s.get("sig_type","dip")
        subtype = s.get("subtype","")
        fund_neg= s.get("funding_neg", False)
        candle  = s.get("candle","")
        if st == "trend":
            icon="⭐" if subtype=="VOL3_BB" else "📈"; border_c="#00d4ff"; type_label=f"TREND {subtype}"
        elif st == "birikim":
            icon="🟣"; border_c="#cc88ff"; type_label="BİRİKİM"
        else:
            icon="💰" if fund_neg else "🔵"; border_c="#c8e86a" if fund_neg else "#00f080"; type_label="DİP"
        fund_val = s.get("funding")
        fund_str = f"{fund_val:+.4f}%" if fund_val is not None else "—"
        if st == "trend":
            ind_str = f"EMA50:+{s.get('ema50_dist',0):.1f}%  EMA200:+{s.get('ema200_dist',0):.1f}%  ADX:{s.get('adx',0):.1f}"
        else:
            ind_str = f"StRSI:{s.get('stoch_rsi',0):.4f}  WR:{s.get('wr',0):.1f}  OBV:{s.get('obv_osc',0):.1f}  WT:{s.get('wt',0):.1f}"
        candle_str = f"  {candle}" if candle else ""
        sig_rows += (
            f'<div class="sig" style="border-color:{border_c}">'
            f'<div class="sr"><b>{icon} {s.get("symbol","")} <small style="color:#3d5a6a">[{type_label}]</small>'
            f'{candle_str}</b>'
            f'<span style="color:#3d5a6a;font-size:.65rem">{s.get("time","")[:16]}</span></div>'
            f'<div class="sd">💵 {fmt_price(s.get("entry"))}  🛡️ {fmt_price(s.get("stop"))}</div>'
            f'<div class="sd">{ind_str}  Funding:{fund_str}</div>'
            f'</div>'
        )
    ps = perf_summary()
    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Scanner v6.0</title>
<meta http-equiv="refresh" content="30">
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#06090d;color:#b8cdd8;font-family:'Courier New',monospace;padding:20px;max-width:960px;margin:0 auto}}
h1{{color:#00d4ff;letter-spacing:4px;font-size:1.1rem;margin-bottom:16px}}
.stats{{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:20px}}
.stat{{background:#0c1117;border:1px solid #1c2a36;padding:9px 14px;border-radius:4px;min-width:80px}}
.sv{{font-size:1.1rem;color:#00d4ff;display:block;font-weight:bold}}
.sl{{font-size:.58rem;color:#3d5a6a;text-transform:uppercase;letter-spacing:1px}}
h3{{color:#00f080;margin:0 0 10px;font-size:.78rem;letter-spacing:2px}}
.sig{{background:#031409;border-left:3px solid #00f080;padding:10px 14px;margin:5px 0;border-radius:2px}}
.sr{{display:flex;justify-content:space-between;align-items:center;margin-bottom:5px}}
.sd{{font-size:.73rem;margin:2px 0;color:#8aa8b8}}
.params{{background:#0c1117;border:1px solid #1c3a20;border-radius:4px;padding:10px 14px;margin-bottom:16px;font-size:.72rem;color:#5a8a6a;line-height:1.8}}
.footer{{color:#3d5a6a;font-size:.62rem;margin-top:20px;border-top:1px solid #1c2a36;padding-top:10px;line-height:2}}
.badge{{display:inline-block;padding:2px 8px;border-radius:3px;font-size:.65rem;margin-right:6px}}
</style></head><body>
<h1>SCANNER <small style="font-size:.6rem;color:#3d5a6a">v6.0</small></h1>
<div class="params">
  <span class="badge" style="background:#0d1a0d;color:#00f080">🔵 DİP</span>
  StochRSI&lt;{STOCH_RSI_THRESH} WR&lt;{WR_THRESH} OBV&lt;{OBV_OSC_THRESH} WT&lt;{WT_THRESH} MACD≤0 | EMA200↑ | Funding&lt;0 | Hammer/Engulfing/MorningStar (ek bilgi)<br>
  <span class="badge" style="background:#0d1520;color:#00d4ff">📈 TREND</span>
  BB(15) Kırılım + EMA200&gt;%12 + ADX&gt;20 + ALL4_LOOSE<br>
  <span class="badge" style="background:#1a0a1a;color:#cc88ff">🟣 BİRİKİM</span>
  RSI 45-68 | OBV↑ | WT↑ | KDJ-J↑ | ADX&lt;30↑ | EMA200↑
</div>
<div class="stats">
  <div class="stat"><span class="sv">{len(tracked_symbols)}</span><span class="sl">Sembol</span></div>
  <div class="stat"><span class="sv">{ws_1h_closes}</span><span class="sl">1H Kapanış</span></div>
  <div class="stat"><span class="sv">{stats.get("signal_sent",0)}</span><span class="sl">Toplam</span></div>
  <div class="stat"><span class="sv" style="color:#00f080">{stats.get("dip_sent",0)}</span><span class="sl">🔵 Dip</span></div>
  <div class="stat"><span class="sv" style="color:#00d4ff">{stats.get("trend_sent",0)}</span><span class="sl">📈 Trend</span></div>
  <div class="stat"><span class="sv" style="color:#cc88ff">{stats.get("birikim_sent",0)}</span><span class="sl">🟣 Birikim</span></div>
  <div class="stat"><span class="sv">{bot_status["status"]}</span><span class="sl">Durum</span></div>
  <div class="stat"><span class="sv">{now}</span><span class="sl">Saat TR</span></div>
</div>
<h3>SON SİNYALLER</h3>
{sig_rows if sig_rows else '<p style="color:#3d5a6a;font-size:.8rem;padding:8px 0">Henüz sinyal yok.</p>'}
<div class="footer">
  Heartbeat: {heartbeat["last"]} | Son coin: {heartbeat["symbol"]}
  &nbsp;|&nbsp; <a href="/performance" style="color:#00d4ff">📈 Performans</a><br>
  Eleme: Cooldown:{stats.get("cooldown",0)} Hacim:{stats.get("low_liquidity",0)} DipFiltre:{stats.get("filtered",0)}
</div>
</body></html>"""

@flask_app.route("/api/status")
def api_status():
    data = clean_json({"status": bot_status["status"], "total_symbols": len(tracked_symbols),
                       "ws_1h_closes": ws_1h_closes, "signals": all_signals[:30],
                       "stats": dict(stats), "heartbeat": heartbeat})
    return flask_app.response_class(json.dumps(data, ensure_ascii=False), mimetype="application/json")

@flask_app.route("/api/health")
def api_health():
    return {"status": "ok", "time": tr_now_str()}

@flask_app.route("/performance")
def perf_dashboard():
    ps   = perf_summary()
    rows = ""
    for s in signal_log[:50]:
        st    = s.get("status","open")
        st_col= "#00f080" if st=="win" else ("#ff4444" if st=="loss" else ("#ffb300" if st=="expired" else "#3d5a6a"))
        stype = s.get("sig_type","dip")
        sub   = s.get("subtype","")
        icon  = "⭐" if sub=="VOL3_BB" else ("📈" if stype=="trend" else ("🟣" if stype=="birikim" else ("💰" if s.get("funding_neg") else "🔵")))
        peak  = s.get("peak_pct", 0)
        cr    = s.get("close_ret")
        ct    = (s.get("close_time") or "")[:16]
        candle= s.get("candle","")
        def fmt_ret(r):
            if r is None: return "—"
            col = "#00f080" if float(r)>0 else "#ff4444"
            return f'<span style="color:{col}">{float(r):+.2f}%</span>'
        rows += f"""<tr>
          <td>{s.get("time","")[:16]}</td>
          <td><b>{icon} {s.get("symbol","")}</b></td>
          <td style="color:{'#00d4ff' if stype=='trend' else '#cc88ff' if stype=='birikim' else '#00f080'}">{stype.upper()}{' '+sub if sub else ''}</td>
          <td>{s.get("entry","")}</td>
          <td style="color:#00f080">+{peak}%</td>
          <td>{'✅' if s.get('tp1_hit') else '—'}</td>
          <td>{'✅' if s.get('tp2_hit') else '—'}</td>
          <td>{fmt_ret(cr)}</td>
          <td>{ct}</td>
          <td>{candle}</td>
          <td style="color:{st_col}">{st.upper()}</td>
        </tr>"""
    return f"""<!DOCTYPE html><html lang="tr"><head>
<meta charset="UTF-8"><title>Performans v6</title>
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
<h1>📈 SİNYAL PERFORMANSI v6.0</h1>
<div class="sub"><a href="/">← Ana Sayfa</a> &nbsp;|&nbsp; {tr_now_str()}</div>
<div class="cards">
  <div class="card"><div class="cv">{ps.get("total",0)}</div><div class="cl">Toplam</div></div>
  <div class="card"><div class="cv">{ps.get("open",0)}</div><div class="cl">Açık</div></div>
  <div class="card"><div class="cv" style="color:#ff4444">{ps.get("losses",0)}</div><div class="cl">Stop</div></div>
  <div class="card"><div class="cv" style="color:#ffb300">{ps.get("expired",0)}</div><div class="cl">Expired</div></div>
  <div class="card"><div class="cv">{ps.get("avg_peak",0)}%</div><div class="cl">Ort. Peak</div></div>
  <div class="card"><div class="cv" style="color:#00f080">{ps.get("dip_total",0)}</div><div class="cl">🔵 Dip</div></div>
  <div class="card"><div class="cv" style="color:#00d4ff">{ps.get("trend_total",0)}</div><div class="cl">📈 Trend</div></div>
</div>
<table><thead><tr>
  <th>Zaman</th><th>Sembol</th><th>Tip</th><th>Giriş</th>
  <th>Peak%</th><th>TP1</th><th>TP2</th><th>Kapanış%</th><th>Kapanış Zamanı</th><th>Formasyon</th><th>Durum</th>
</tr></thead><tbody>{rows}</tbody></table>
</body></html>"""

# ============================================================
# 15) MAIN
# ============================================================
async def periodic_summary():
    tick = 0
    while True:
        await asyncio.sleep(600)
        print_summary()
        tick += 1
        if tick % 2 == 0:
            await refresh_btc_4h()

async def main():
    print("Scanner v6.0 baslatiliyor...", flush=True)
    print(f"DIP: StRSI<{STOCH_RSI_THRESH} WR<{WR_THRESH} OBV<{OBV_OSC_THRESH} WT<{WT_THRESH}", flush=True)
    print("TREND: BB(15) kirilim + ALL4_LOOSE", flush=True)
    print("BİRİKİM: RSI 45-68 + OBV/WT/KDJ-J artiyor", flush=True)
    print("NOT: BB Retest sistemi kaldirildi.", flush=True)

    symbols = await load_symbols_pool()
    if not symbols:
        print("Sembol yuklenemedi", flush=True); return

    global tracked_symbols
    tracked_symbols      = list(symbols)
    bot_status["status"] = "BOOTSTRAP"
    print(f"{len(symbols)} sembol yuklendi", flush=True)

    await bootstrap_all(symbols)
    await refresh_funding_cache(symbols)
    await refresh_btc_4h()
    rebuild_pending()
    print(f"Pending sinyaller: {sum(len(v) for v in pending_by_symbol.values())}", flush=True)
    print_summary()

    candidate_queue = asyncio.Queue()
    asyncio.create_task(signal_worker(candidate_queue))
    asyncio.create_task(periodic_summary())

    bot_status["status"] = "LIVE"
    print(f"LIVE | {len(symbols)} sembol | Dip + Trend + Birikim izleniyor", flush=True)

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
