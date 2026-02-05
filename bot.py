# -*- coding: utf-8 -*-
"""
Sniper Bot (WS 15m + REST sadece aday doğrulama) - Tek dosya

MANTIK:
- 15m mum kapanışları Binance WebSocket'ten alınır (REST ile 433 coin taraması YOK)
- REST sadece:
  (A) başlangıçta her sembol için 15m geçmişini bootstrap etmek (tek sefer)
  (B) aday çıkınca 1h trend + spread + TP/SL/ RR + ticker doğrulaması için

Gerekenler:
  pip install ccxt pandas pandas_ta numpy flask websockets requests
"""

import asyncio
import json
import time
import threading
import os
from collections import defaultdict
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
# 0) KULLANICI AYARLARI
# ============================================================
BINANCE_API_KEY = ""        # Boş bırak (public veri için şart değil)
BINANCE_API_SECRET = ""     # Boş bırak

TELEGRAM_TOKEN = ""         # Bot token
TELEGRAM_CHAT_ID = ""       # Chat id

# --- RİSK ---
ACCOUNT_SIZE = 5000.0
RISK_PERCENT = 2.0

# --- FİLTRE / PARAMETRE ---
MIN_ATR_PCT = 0.0018
COOLDOWN_MINUTES = 120
PULLBACK_COOLDOWN_MIN = 360
REACCU_COOLDOWN_MIN = 480

# Kaç sembol dinlenecek? None => hepsi
MAX_SYMBOLS = None

# Eğer MAX_SYMBOLS kısıtlıysa: en yüksek quoteVolume'a göre seç
USE_TOP_VOLUME_POOL = True

# --- RS (BTC'ye göre güç) ---
USE_RS_FILTER = True
RS_BARS_1H  = 4
RS_BARS_4H  = 16
RS_BARS_12H = 48
RS_BARS_24H = 96
RS_MIN_REL_1H  = 0.15
RS_MIN_REL_4H  = 0.50
RS_MIN_SCORE   = 0.60
RS_RISK_OFF_MIN_SCORE = 1.50

# --- Macro ---
MACRO_SYMBOL = "BTC/USDT"
MACRO_TTL_MIN = 10

# --- Explain (telegram mesajında “neden” bloğu) ---
EXPLAIN_SIGNALS = True

# --- WS / DATA ---
BOOTSTRAP_LIMIT_15M = 260
KEEP_BARS_15M = 300
WS_KLINE_INTERVAL = "15m"
WS_STREAM_CHUNK = 120

# --- TR timezone ---
TR_TZ = timezone(timedelta(hours=3))

# ============================================================
# 0.1) ANLAŞILIR LOG / ÖZET
# ============================================================
BOOT_PROGRESS_EVERY = 20       # Bootstrapte her 20 coinde 1 yaz
PRINT_ADAY_LOG = True          # Aday yakalayınca tek satır yaz
PRINT_SIGNAL_LOG = True        # Sinyal gönderince tek satır yaz

stats = defaultdict(int)       # eleme/olay sayacı
ws_close_count = 0
tracked_symbols = []

def tr_now_str():
    return datetime.now(timezone.utc).astimezone(TR_TZ).strftime("%Y-%m-%d %H:%M:%S")

def print_summary():
    total = len(tracked_symbols) if tracked_symbols else 0
    print("\n📊 TARAMA SONUÇ ÖZETİ (ANLAŞILIR)", flush=True)
    print("━━━━━━━━━━━━━━━━━━━━", flush=True)
    print(f"🧭 Takip edilen coin         : {total}", flush=True)
    print(f"🕯️ 15dk kapanış sayısı       : {ws_close_count}", flush=True)
    print(f"🔍 Aday sayısı               : {stats.get('aday',0)}", flush=True)
    print(f"✅ Gönderilen sinyal         : {stats.get('sinyal_gonderildi',0)}", flush=True)
    print("— Eleme sebepleri —", flush=True)

    keys = [
        ("kurulum_yok",      "Kurulum yok"),
        ("atr_dusuk",        "ATR düşük"),
        ("rs_red",           "RS (BTC) red"),
        ("cooldown",         "Cooldown"),
        ("1h_trend_red",     "1s trend red"),
        ("spread_red",       "Spread red"),
        ("rr_red",           "RR red"),
        ("veri_yetersiz",    "Veri yetersiz"),
        ("atr_yok",          "ATR yok/0"),
    ]
    any_printed = False
    for k, label in keys:
        v = stats.get(k, 0)
        if v:
            any_printed = True
            print(f"• {label:18s}: {v}", flush=True)
    if not any_printed:
        print("• (Henüz eleme/olay yok)", flush=True)

    print("━━━━━━━━━━━━━━━━━━━━\n", flush=True)

# ============================================================
# 1) RATE LIMIT KAPISI (REST için)
# ============================================================
class ApiGate:
    def __init__(self, min_interval_sec=0.22, max_concurrent=1, max_retries=6):
        self.min_interval_sec = float(min_interval_sec)
        self.sem = asyncio.Semaphore(int(max_concurrent))
        self.max_retries = int(max_retries)
        self._lock = asyncio.Lock()
        self._last_call_ts = 0.0

    async def _sleep_for_spacing(self):
        async with self._lock:
            now = time.time()
            wait = (self._last_call_ts + self.min_interval_sec) - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call_ts = time.time()

    async def call(self, fn, *args, **kwargs):
        async with self.sem:
            for attempt in range(self.max_retries):
                try:
                    await self._sleep_for_spacing()
                    loop = asyncio.get_running_loop()
                    return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))

                except (ccxt.RateLimitExceeded, ccxt.DDoSProtection) as e:
                    backoff = min(30.0, (1.0 * (2 ** attempt)))
                    print(f"⏳ RateLimit/DDOS: {type(e).__name__} | backoff={backoff:.1f}s", flush=True)
                    await asyncio.sleep(backoff)

                except (ccxt.NetworkError, ccxt.RequestTimeout, ccxt.ExchangeNotAvailable) as e:
                    backoff = min(20.0, (0.8 * (2 ** attempt)))
                    print(f"🌐 Network: {type(e).__name__} | backoff={backoff:.1f}s", flush=True)
                    await asyncio.sleep(backoff)

                except ccxt.ExchangeError as e:
                    backoff = min(12.0, (0.6 * (2 ** attempt)))
                    print(f"🏦 ExchangeError: {str(e)[:140]} | backoff={backoff:.1f}s", flush=True)
                    await asyncio.sleep(backoff)

                except Exception as e:
                    backoff = min(8.0, (0.5 * (2 ** attempt)))
                    print(f"⚠️ API ERR: {type(e).__name__}: {str(e)[:140]} | backoff={backoff:.1f}s", flush=True)
                    await asyncio.sleep(backoff)

            raise RuntimeError("API call failed after retries")

api_gate = ApiGate(min_interval_sec=0.22, max_concurrent=1, max_retries=6)

# ============================================================
# 2) CCXT EXCHANGE
# ============================================================
exchange = ccxt.binance({
    "apiKey": BINANCE_API_KEY or None,
    "secret": BINANCE_API_SECRET or None,
    "options": {"defaultType": "spot", "adjustForTimeDifference": True},
    "enableRateLimit": True,
    "timeout": 15000,
})

# ============================================================
# 3) UTILS
# ============================================================
def fmt_price(symbol: str, price) -> str:
    try:
        if price is None:
            return "N/A"
        return exchange.price_to_precision(symbol, float(price))
    except Exception:
        try:
            p = float(price)
        except Exception:
            return "N/A"
        if p == 0:
            return "0"
        if p < 0.01:
            return f"{p:.8f}"
        if p < 1:
            return f"{p:.6f}"
        return f"{p:.4f}"

def _fmt_num(x, nd=2):
    try:
        if x is None or (isinstance(x, float) and np.isnan(x)):
            return "N/A"
        return f"{float(x):.{nd}f}"
    except Exception:
        return "N/A"

def _fmt_x(x, nd=2):
    try:
        if x is None or (isinstance(x, float) and np.isnan(x)):
            return "N/A"
        return f"{float(x):.{nd}f}x"
    except Exception:
        return "N/A"

def _pct_change_close(df: pd.DataFrame, bars: int):
    try:
        if df is None or len(df) <= bars:
            return np.nan
        now = float(df["close"].iloc[-1])
        prev = float(df["close"].iloc[-1 - bars])
        if prev == 0:
            return np.nan
        return ((now / prev) - 1.0) * 100.0
    except Exception:
        return np.nan

def calc_relative_strength(df_coin_15m: pd.DataFrame, df_btc_15m: pd.DataFrame):
    coin_1h  = _pct_change_close(df_coin_15m, RS_BARS_1H)
    coin_4h  = _pct_change_close(df_coin_15m, RS_BARS_4H)
    coin_12h = _pct_change_close(df_coin_15m, RS_BARS_12H)
    coin_24h = _pct_change_close(df_coin_15m, RS_BARS_24H)
    btc_1h   = _pct_change_close(df_btc_15m, RS_BARS_1H)
    btc_4h   = _pct_change_close(df_btc_15m, RS_BARS_4H)
    btc_12h  = _pct_change_close(df_btc_15m, RS_BARS_12H)
    btc_24h  = _pct_change_close(df_btc_15m, RS_BARS_24H)

    rel_1h  = coin_1h  - btc_1h  if not np.isnan(coin_1h)  and not np.isnan(btc_1h)  else np.nan
    rel_4h  = coin_4h  - btc_4h  if not np.isnan(coin_4h)  and not np.isnan(btc_4h)  else np.nan
    rel_12h = coin_12h - btc_12h if not np.isnan(coin_12h) and not np.isnan(btc_12h) else np.nan
    rel_24h = coin_24h - btc_24h if not np.isnan(coin_24h) and not np.isnan(btc_24h) else np.nan

    parts = []
    if not np.isnan(rel_1h):  parts.append(0.35 * rel_1h)
    if not np.isnan(rel_4h):  parts.append(0.45 * rel_4h)
    if not np.isnan(rel_12h): parts.append(0.20 * rel_12h)
    score = np.nan if not parts else sum(parts)
    return score, rel_1h, rel_4h, rel_12h, rel_24h

# ============================================================
# 4) DATA STORE
# ============================================================
@dataclass
class Signal:
    symbol: str
    data: dict
    df_15m: pd.DataFrame
    coin_regime: str
    macro_regime: str
    tr_time: datetime

bars_15m: dict[str, pd.DataFrame] = {}
last_signal_ts: dict[tuple, datetime] = {}
macro_cache = {"regime": "NEUTRAL", "ts": None, "df_1h": None}
btc_15m_cache = {"df": None, "ts": 0.0}

# ============================================================
# 5) MARKET POOL
# ============================================================
IGNORED_COINS = set([
    'UP/USDT','DOWN/USDT','BEAR/USDT','BULL/USDT',
    'USDC/USDT','TUSD/USDT','FDUSD/USDT','DAI/USDT','USDP/USDT',
    'EUR/USDT','TRY/USDT','GBP/USDT','BUSD/USDT','USTC/USDT',
    'PAXG/USDT','WBTC/USDT','USDE/USDT','BRL/USDT','RUB/USDT',
    'AUD/USDT','UST/USDT','USD/USDT','XUSD/USDT','USD1/USDT',
])

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

    if not USE_TOP_VOLUME_POOL:
        return syms[:MAX_SYMBOLS] if MAX_SYMBOLS else syms

    # Top volume seçimi (MAX_SYMBOLS kısıtlıysa anlamlı)
    if not MAX_SYMBOLS:
        return syms

    volumes = {}
    CHUNK = 120
    for i in range(0, len(syms), CHUNK):
        part = syms[i:i+CHUNK]
        try:
            res = await api_gate.call(exchange.fetch_tickers, part)
            if isinstance(res, dict):
                for k, v in res.items():
                    qv = v.get("quoteVolume", 0) or 0
                    volumes[k] = float(qv)
        except Exception:
            continue

    syms_sorted = sorted(syms, key=lambda x: volumes.get(x, 0.0), reverse=True)
    return syms_sorted[:MAX_SYMBOLS]

# ============================================================
# 6) REST HELPERS
# ============================================================
async def fetch_ohlcv_df(symbol: str, timeframe: str, limit: int):
    bars = await api_gate.call(exchange.fetch_ohlcv, symbol, timeframe=timeframe, limit=limit)
    df = pd.DataFrame(bars, columns=["timestamp","open","high","low","close","volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df.set_index("timestamp", inplace=True)
    return df

def prepare_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["ema50"] = ta.ema(df["close"], length=50)
    df["ema200"] = ta.ema(df["close"], length=200)
    df["atr"] = ta.atr(df["high"], df["low"], df["close"], length=14)
    df["atr_mean"] = df["atr"].rolling(100, min_periods=50).mean()
    df["rsi"] = ta.rsi(df["close"], length=14)
    df["vol_ma"] = df["volume"].rolling(20, min_periods=1).mean()
    return df

def get_coin_regime_15m(df_15m: pd.DataFrame) -> str:
    try:
        if df_15m is None or len(df_15m) < 210:
            return "NEUTRAL"
        adx_df = ta.adx(df_15m["high"], df_15m["low"], df_15m["close"], length=14)
        adx_val = None
        if adx_df is not None and hasattr(adx_df, "columns"):
            cands = [c for c in adx_df.columns if str(c).upper().startswith("ADX")]
            if cands:
                adx_val = adx_df[cands[0]].iloc[-1]

        last = df_15m.iloc[-1]
        ema50 = last.get("ema50", np.nan)
        ema200 = last.get("ema200", np.nan)
        close = last.get("close", np.nan)
        if pd.isna(ema50) or pd.isna(ema200) or pd.isna(close):
            return "NEUTRAL"

        if adx_val is not None and not pd.isna(adx_val) and adx_val > 20:
            if close > ema50 and close > ema200:
                return "UPTREND"
            if close < ema50 and close < ema200:
                return "DOWNTREND"
        return "RANGING"
    except Exception:
        return "NEUTRAL"

async def get_macro_regime():
    now = datetime.now(timezone.utc)
    ts = macro_cache["ts"]
    if ts and (now - ts) < timedelta(minutes=MACRO_TTL_MIN):
        return macro_cache["regime"], macro_cache["df_1h"]

    try:
        df_1h = await fetch_ohlcv_df(MACRO_SYMBOL, "1h", 260)
        if df_1h is None or len(df_1h) < 220:
            macro_cache.update({"regime":"NEUTRAL","ts":now,"df_1h":None})
            return "NEUTRAL", None

        ema50_s = ta.ema(df_1h["close"], length=50)
        ema200_s = ta.ema(df_1h["close"], length=200)
        adx_df = ta.adx(df_1h["high"], df_1h["low"], df_1h["close"], length=14)
        bb_df = ta.bbands(df_1h["close"], length=20, std=2)
        if any(x is None for x in [ema50_s, ema200_s, adx_df, bb_df]):
            macro_cache.update({"regime":"NEUTRAL","ts":now,"df_1h":None})
            return "NEUTRAL", None

        ema50 = ema50_s.iloc[-1]
        ema200 = ema200_s.iloc[-1]

        adx_col = None
        if hasattr(adx_df, "columns"):
            cands = [c for c in adx_df.columns if str(c).upper().startswith("ADX")]
            if cands:
                adx_col = cands[0]
        if adx_col is None:
            macro_cache.update({"regime":"NEUTRAL","ts":now,"df_1h":None})
            return "NEUTRAL", None
        adx = adx_df[adx_col].iloc[-1]

        bbu_col = bbl_col = bbm_col = None
        if hasattr(bb_df, "columns"):
            for c in bb_df.columns:
                uc = str(c).upper()
                if "BBU" in uc and bbu_col is None: bbu_col = c
                if "BBL" in uc and bbl_col is None: bbl_col = c
                if "BBM" in uc and bbm_col is None: bbm_col = c
        if any(x is None for x in [bbu_col,bbl_col,bbm_col]):
            macro_cache.update({"regime":"NEUTRAL","ts":now,"df_1h":None})
            return "NEUTRAL", None

        bbu = bb_df[bbu_col].iloc[-1]
        bbl = bb_df[bbl_col].iloc[-1]
        bbm = bb_df[bbm_col].iloc[-1]
        close = df_1h["close"].iloc[-1]

        if any(pd.isna(x) for x in [ema50,ema200,adx,bbu,bbl,bbm]) or bbm == 0:
            macro_cache.update({"regime":"NEUTRAL","ts":now,"df_1h":None})
            return "NEUTRAL", None

        bb_width = (bbu - bbl) / bbm
        if close < ema50 and close < ema200 and adx > 20:
            regime = "RISK_OFF"
        elif close > ema50 and close > ema200 and adx > 20:
            regime = "RISK_ON"
        elif bb_width < 0.08:
            regime = "SQUEEZE"
        else:
            regime = "NEUTRAL"

        macro_cache.update({"regime":regime,"ts":now,"df_1h":df_1h})
        return regime, df_1h

    except Exception as e:
        print(f"⚠️ Macro Regime Hatası (BTC): {e}", flush=True)
        macro_cache.update({"regime":"NEUTRAL","ts":now,"df_1h":None})
        return "NEUTRAL", None

async def is_1h_trend_aligned(symbol: str):
    try:
        df_1h = await fetch_ohlcv_df(symbol, "1h", 260)
        if df_1h is None or len(df_1h) < 220:
            return False, "❌ 1s veri eksik"
        ema50 = ta.ema(df_1h["close"], length=50).iloc[-1]
        ema200 = ta.ema(df_1h["close"], length=200).iloc[-1]
        close = df_1h["close"].iloc[-1]
        if pd.isna(ema50) or pd.isna(ema200):
            return False, "❌ 1s EMA hesaplanamadı"
        if close > ema50 > ema200:
            return True, "✅ 1s Trend uygun"
        return False, "❌ 1s Trend uygun değil"
    except Exception:
        return False, "❌ 1s trend hatası"

async def check_spread_safety(symbol: str):
    try:
        ob = await api_gate.call(exchange.fetch_order_book, symbol, 5)
        bids = ob.get("bids") or []
        asks = ob.get("asks") or []
        if not bids or not asks:
            return False, "⚠️ Spread kontrol edilemedi"
        bid = bids[0][0]
        ask = asks[0][0]
        if not bid or bid <= 0:
            return False, "⚠️ Spread kontrol edilemedi"
        spread_pct = ((ask - bid) / bid) * 100
        if spread_pct > 0.4:
            return False, f"⚠️ Spread geniş: %{spread_pct:.2f}"
        return True, f"✅ Spread: %{spread_pct:.2f}"
    except Exception:
        return True, "ℹ️ Spread alınamadı"

def calc_position_size(entry, stop, account_size=ACCOUNT_SIZE, risk_pct=RISK_PERCENT):
    risk_amount = account_size * (risk_pct / 100)
    risk_per_coin = entry - stop
    if risk_per_coin <= 0:
        return 0, 0, 0
    position_usdt = risk_amount / (risk_per_coin / entry)
    position_usdt = min(position_usdt, account_size * 0.25)
    coin_amount = position_usdt / entry
    actual_risk_pct = (risk_per_coin / entry) * 100
    return position_usdt, coin_amount, actual_risk_pct

# ============================================================
# 7) SR TARGET (aynı mantık)
# ============================================================
def _extract_pivot_prices(df: pd.DataFrame, lookback=140, left=3, right=3):
    try:
        n = len(df)
        if n < (left + right + 10):
            return []
        start = max(0, n - lookback)
        highs = df["high"].values
        lows  = df["low"].values
        pivots = []
        end = n - right - 1
        for i in range(start + left, end):
            h = highs[i]; l = lows[i]
            if np.isnan(h) or np.isnan(l):
                continue
            if h == np.max(highs[i-left:i+right+1]):
                pivots.append(float(h))
            if l == np.min(lows[i-left:i+right+1]):
                pivots.append(float(l))
        return pivots
    except Exception:
        return []

def _cluster_levels(prices, tol: float):
    if not prices:
        return []
    prices = sorted([p for p in prices if p is not None and not np.isnan(p)])
    if not prices:
        return []
    clusters = [[prices[0]]]
    for p in prices[1:]:
        center = float(np.median(clusters[-1]))
        if abs(p - center) <= tol:
            clusters[-1].append(p)
        else:
            clusters.append([p])
    levels = []
    for c in clusters:
        levels.append((float(np.median(c)), len(c)))
    levels.sort(key=lambda x: x[0])
    return levels

def get_nearest_sr_levels(df_15m: pd.DataFrame, entry: float, atr: float):
    pivots = _extract_pivot_prices(df_15m, lookback=160, left=3, right=3)
    tol = max(0.35 * atr, entry * 0.0018)
    levels = _cluster_levels(pivots, tol=tol)
    support = None; support_strength = 0
    resistance = None; resistance_strength = 0
    for lvl, st in levels:
        if lvl < entry:
            support = lvl; support_strength = st
        elif lvl > entry and resistance is None:
            resistance = lvl; resistance_strength = st
            break
    return support, support_strength, resistance, resistance_strength

def find_structural_target(df_15m: pd.DataFrame, entry_price: float):
    try:
        atr = float(df_15m["atr"].iloc[-1])
        if np.isnan(atr) or atr <= 0:
            return entry_price * 1.03, 3.0, "Fallback (%3)", None
        sup, sup_n, res, res_n = get_nearest_sr_levels(df_15m, entry_price, atr)
        if res is not None:
            tp = res - (0.12 * atr)
            if tp <= entry_price * 1.002:
                tp = res
            note = f"Yakın direnç (güç={res_n})"
        else:
            tp = entry_price + max(3.0 * atr, entry_price * 0.02)
            note = "Direnç net değil (ATR bazlı)"
        tp_pct = ((tp - entry_price) / entry_price) * 100.0
        return tp, tp_pct, note, sup
    except Exception:
        return entry_price * 1.03, 3.0, "Fallback (%3)", None

# ============================================================
# 8) STRATEJİLER (senin 3 strateji)
# ============================================================
def strategy_sfp_gold(df_15m):
    try:
        if len(df_15m) < 60:
            return False, None
        last = df_15m.iloc[-1]
        ema_now = df_15m["ema50"].iloc[-1]
        ema_prev = df_15m["ema50"].iloc[-5]
        if pd.isna(ema_now) or pd.isna(ema_prev) or ema_now < ema_prev:
            return False, None

        past_window = df_15m.iloc[-50:-1]
        pivot_idx = past_window["low"].idxmin()
        pivot_low = float(past_window.loc[pivot_idx]["low"])
        pivot_rsi = float(df_15m.loc[pivot_idx]["rsi"])

        atr_now = float(last["atr"])
        atr_mean = float(last["atr_mean"]) if not pd.isna(last["atr_mean"]) else 0.0
        vol_ratio = (atr_now / atr_mean) if atr_mean else 1.0

        coin_type_tag = "NORMAL"
        sweep_mult, reclaim_mult, stop_mult, wick_mult = 0.15, 0.25, 0.30, 1.5
        if vol_ratio >= 1.25:
            coin_type_tag = "🔥 VOLATILE"
            sweep_mult, reclaim_mult, stop_mult, wick_mult = 0.25, 0.35, 0.45, 1.8
        elif vol_ratio <= 0.85:
            coin_type_tag = "🧊 CALM"
            sweep_mult, reclaim_mult, stop_mult, wick_mult = 0.10, 0.20, 0.25, 1.5

        sweep_limit = pivot_low - (sweep_mult * atr_now)
        dip_zone = pivot_low + (reclaim_mult * atr_now)

        swept = float(last["low"]) < sweep_limit
        reclaimed = float(last["close"]) > dip_zone

        body = abs(float(last["close"]) - float(last["open"]))
        lower_wick = min(float(last["close"]), float(last["open"])) - float(last["low"])
        strong_wick = True if body == 0 else (lower_wick > (body * wick_mult))

        vol_ok = float(last["volume"]) > (float(last["vol_ma"]) * 1.2) if not pd.isna(last["vol_ma"]) else False

        if swept and reclaimed and strong_wick and vol_ok:
            safe_stop = pivot_low - (stop_mult * atr_now)
            current_rsi = float(last["rsi"])
            is_gold = current_rsi >= (pivot_rsi - 3)
            if is_gold:
                return True, {
                    "type": f"🟢 SFP-A (GOLD) | {coin_type_tag}",
                    "desc": "Dip süpürme + RSI uyumsuzluğu",
                    "stop": safe_stop,
                    "coin_type": coin_type_tag,
                }
        return False, None
    except Exception:
        return False, None

def strategy_pullback(df_15m):
    try:
        last = df_15m.iloc[-1]
        ema50 = float(last["ema50"])
        ema50_prev = float(df_15m["ema50"].iloc[-6])
        if pd.isna(ema50_prev) or pd.isna(ema50) or (ema50 <= ema50_prev):
            return False, None

        ema200 = float(last["ema200"])
        rsi = float(last["rsi"])
        if pd.isna(ema200) or not (ema50 > ema200):
            return False, None

        touched_ema = float(last["low"]) <= ema50 * 1.001
        prev = df_15m.iloc[-2]
        prev_sweep = float(prev["low"]) < ema50 * 0.999

        rng = float(last["high"]) - float(last["low"])
        close_strength = True if rng == 0 else ((float(last["close"]) - float(last["low"])) / rng) > 0.65
        bounced = (float(last["close"]) > ema50) and (float(last["close"]) > float(last["open"])) and close_strength

        not_overbought = rsi < 60
        vol_ok = (not pd.isna(last["vol_ma"])) and (float(last["volume"]) > (float(last["vol_ma"]) * 1.2))

        if touched_ema and prev_sweep and bounced and not_overbought and vol_ok:
            return True, {
                "type": "🚀 EMA PULLBACK",
                "desc": "Trende geri çekilme",
                "stop": float(last["low"]),
                "coin_type": "NORMAL",
            }
        return False, None
    except Exception:
        return False, None

def strategy_reaccumulation(df_15m):
    try:
        last = df_15m.iloc[-1]
        ema50 = float(last["ema50"]); ema200 = float(last["ema200"])
        atr = float(last["atr"]); atr_mean = float(last["atr_mean"])
        rsi = float(last["rsi"])

        if any(pd.isna(x) for x in [ema50, ema200, atr, atr_mean, rsi]) or atr_mean == 0:
            return False, None
        if float(last["close"]) < ema50 or ema50 <= ema200:
            return False, None
        if not (45 <= rsi <= 70):
            return False, None

        lookback = 16
        recent = df_15m.iloc[-lookback:-1]
        range_height = float(recent["high"].max() - recent["low"].min())
        if range_height > (2.8 * atr):
            return False, None

        compression = atr / atr_mean
        if compression > 0.80:
            return False, None

        recent_high = float(recent["high"].max())
        breakout_level = recent_high + (0.25 * atr)
        prev_close = float(df_15m["close"].iloc[-2])
        if prev_close > (recent_high + 0.05 * atr):
            return False, None

        breakout = float(last["close"]) > breakout_level
        rng = float(last["high"]) - float(last["low"])
        close_strength = True if rng == 0 else ((float(last["close"]) - float(last["low"])) / rng) > 0.72
        body = abs(float(last["close"]) - float(last["open"]))
        body_ratio = True if rng == 0 else (body / rng) > 0.55
        strong_candle = (float(last["close"]) > float(last["open"])) and close_strength and body_ratio

        vol_ma = float(last["vol_ma"]) if not pd.isna(last["vol_ma"]) else 0.0
        if vol_ma == 0:
            return False, None
        vol_ok = float(last["volume"]) > (vol_ma * 1.6)

        if breakout and strong_candle and vol_ok:
            mid_point = float((recent["high"].max() + recent["low"].min()) / 2)
            tight_stop = mid_point - (0.6 * atr)
            return True, {
                "type": "🚩 RE-ACCUMULATION (PRO)",
                "desc": f"Bayrak kırılımı. Sıkışma: {compression:.2f}",
                "stop": tight_stop,
                "coin_type": "TREND",
            }
        return False, None
    except Exception:
        return False, None

# ============================================================
# 9) EXPLAIN BLOCK
# ============================================================
def build_explain_block(df_15m: pd.DataFrame, data: dict) -> str:
    if not EXPLAIN_SIGNALS:
        return ""
    try:
        stype = (data.get("type","") or "").upper()
        last = df_15m.iloc[-1]
        rsi = float(last["rsi"])
        atr = float(last["atr"]); atr_mean = float(last["atr_mean"])
        vol = float(last["volume"]); vol_ma = float(last["vol_ma"]) if not pd.isna(last["vol_ma"]) else 0.0
        vol_strength = (vol/vol_ma) if vol_ma else np.nan
        compression = (atr/atr_mean) if atr_mean else np.nan

        lines = ["🧩 <b>KRİTERLER</b>"]
        if "PULLBACK" in stype:
            lines.append(f"• RSI={_fmt_num(rsi,2)} | VolGüç={_fmt_x(vol_strength,2)}")
        elif "RE-ACCUMULATION" in stype:
            lines.append(f"• RSI={_fmt_num(rsi,2)} | Sıkışma={_fmt_num(compression,2)} | VolGüç={_fmt_x(vol_strength,2)}")
        elif "SFP" in stype:
            lines.append(f"• RSI={_fmt_num(rsi,2)} | ATR/Mean={_fmt_num(compression,2)} | VolGüç={_fmt_x(vol_strength,2)}")
        else:
            return ""
        return "\n".join(lines)
    except Exception:
        return ""

# ============================================================
# 10) FLASK / HEARTBEAT / WATCHDOG
# ============================================================
app = Flask(__name__)
bot_status = {"last_run": "Henüz Başlamadı", "status": "BOOT", "signal_count": 0}

import logging
logging.getLogger("werkzeug").setLevel(logging.ERROR)
app.logger.disabled = True

heartbeat = {
    "last_beat_tr": None,
    "last_beat_epoch": time.time(),
    "last_symbol": None,
    "progress": None,
    "loop": 0,
    "status": "BOOT"
}

WATCHDOG_STALE_SEC = 300
_last_beat_ts = 0.0

def beat(symbol=None, progress=None, status=None):
    """Async tarafta çağrılır; 10 sn'de bir günceller."""
    global _last_beat_ts
    now = time.time()
    if now - _last_beat_ts >= 10:
        heartbeat["last_beat_tr"] = tr_now_str()
        heartbeat["last_beat_epoch"] = time.time()
        if symbol is not None:
            heartbeat["last_symbol"] = symbol
        if progress is not None:
            heartbeat["progress"] = progress
        if status is not None:
            heartbeat["status"] = status
        _last_beat_ts = now

def heartbeat_pinger():
    """Render watchdog/health için: async kilitlense bile epoch günceller."""
    while True:
        try:
            heartbeat["last_beat_tr"] = tr_now_str()
            heartbeat["last_beat_epoch"] = time.time()
        except Exception:
            pass
        time.sleep(15)

def watchdog_thread():
    while True:
        try:
            last_epoch = float(heartbeat.get("last_beat_epoch") or 0.0)
            stale = time.time() - last_epoch
            if stale > WATCHDOG_STALE_SEC:
                print(f"🛑 WATCHDOG: Heartbeat {int(stale)}s stale. Forcing restart...", flush=True)
                os._exit(1)
            time.sleep(10)
        except Exception:
            time.sleep(10)

@app.route("/")
def home():
    now = datetime.now(TR_TZ).strftime("%H:%M:%S")
    return f"""
    <h1>🚀 Sniper Bot</h1>
    <p><b>Durum:</b> {bot_status['status']}</p>
    <p><b>Son:</b> {bot_status['last_run']}</p>
    <p><b>Sinyal:</b> {bot_status['signal_count']}</p>
    <p><b>Saat(TR):</b> {now}</p>
    <hr>
    <p><b>Heartbeat(TR):</b> {heartbeat.get('last_beat_tr')}</p>
    <p><b>Son Coin:</b> {heartbeat.get('last_symbol')}</p>
    <p><b>İlerleme:</b> {heartbeat.get('progress')}</p>
    """

@app.route("/health")
def health():
    return {"bot_status": bot_status, "heartbeat": heartbeat, "stats": dict(stats)}

# ============================================================
# 11) TELEGRAM
# ============================================================
def send_telegram(text_html: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text_html, "parse_mode": "HTML"},
            timeout=10
        )
        if r.status_code != 200:
            print(f"⚠️ Telegram non-200: {r.status_code} | {r.text[:200]}", flush=True)
    except Exception as e:
        print(f"⚠️ Telegram Exception: {e}", flush=True)

# ============================================================
# 12) BOOTSTRAP
# ============================================================
async def bootstrap_symbol(symbol: str):
    try:
        df = await fetch_ohlcv_df(symbol, "15m", BOOTSTRAP_LIMIT_15M)
        if df is None or len(df) < 220:
            return False
        df = prepare_indicators(df)
        if len(df) > KEEP_BARS_15M:
            df = df.iloc[-KEEP_BARS_15M:]
        bars_15m[symbol] = df
        return True
    except Exception as e:
        print(f"⚠️ Bootstrap hata {symbol}: {str(e)[:140]}", flush=True)
        return False

async def bootstrap_all(symbols: list[str]):
    ok = 0
    total = len(symbols)
    bot_status["status"] = "BOOTSTRAP"
    print(f"🧱 Hazırlık başlıyor | toplam coin={total}", flush=True)

    for i, s in enumerate(symbols, 1):
        beat(symbol=s, progress=f"Hazırlık {i}/{total}", status="BOOTSTRAP")
        if i % BOOT_PROGRESS_EVERY == 0:
            print(f"-> Hazırlık: {i}/{total} ({s})", flush=True)
        got = await bootstrap_symbol(s)
        if got:
            ok += 1

    print(f"✅ Hazırlık bitti | ok={ok}/{total}", flush=True)

# ============================================================
# 13) WS KLINE LISTENER
# ============================================================
def to_ws_symbol(symbol: str) -> str:
    return symbol.replace("/", "").lower()

async def ws_listen_klines(symbols: list[str], candidate_queue: asyncio.Queue):
    streams = "/".join([f"{to_ws_symbol(s)}@kline_{WS_KLINE_INTERVAL}" for s in symbols])
    url = f"wss://stream.binance.com:9443/stream?streams={streams}"

    bot_status["status"] = "CANLI VERİ (WS)"
    print(f"🛰️ Canlı veri başladı | coin={len(symbols)} | timeframe=15m", flush=True)

    while True:
        try:
            async with websockets.connect(
                url,
                ping_interval=30,
                ping_timeout=30,
                close_timeout=10,
                max_queue=2048,
                compression=None
            ) as ws:
                print("✅ Canlı bağlantı OK", flush=True)
                while True:
                    msg = await ws.recv()
                    data = json.loads(msg)
                    payload = data.get("data", {})
                    k = payload.get("k", {})
                    if not k.get("x", False):  # kapanış değilse
                        continue

                    sym_raw = payload.get("s", "")
                    symbol = sym_raw.upper().replace("USDT", "/USDT")

                    ts_ms = int(k.get("t"))
                    o = float(k.get("o")); h = float(k.get("h"))
                    l = float(k.get("l")); c = float(k.get("c"))
                    v = float(k.get("v"))

                    df = bars_15m.get(symbol)
                    if df is None or len(df) < 50:
                        stats["veri_yetersiz"] += 1
                        continue

                    tstamp = pd.to_datetime(ts_ms, unit="ms", utc=True)
                    df.loc[tstamp, ["open","high","low","close","volume"]] = [o,h,l,c,v]
                    df = df.sort_index()
                    if len(df) > KEEP_BARS_15M:
                        df = df.iloc[-KEEP_BARS_15M:]
                    df = prepare_indicators(df)
                    bars_15m[symbol] = df

                    await evaluate_symbol_on_close(symbol, df, candidate_queue)

        except Exception as e:
            print(f"⚠️ WS kopma: {type(e).__name__}: {str(e)[:160]}", flush=True)
            bot_status["status"] = "WS yeniden bağlanıyor"
            await asyncio.sleep(5)

async def ws_listen_klines_multi(symbols: list[str], candidate_queue: asyncio.Queue):
    tasks = []
    for i in range(0, len(symbols), WS_STREAM_CHUNK):
        part = symbols[i:i+WS_STREAM_CHUNK]
        tasks.append(asyncio.create_task(ws_listen_klines(part, candidate_queue)))
    await asyncio.gather(*tasks)

# ============================================================
# 14) ADAY ÜRETİMİ (15m kapanışında)
# ============================================================
async def evaluate_symbol_on_close(symbol: str, df_15m: pd.DataFrame, candidate_queue: asyncio.Queue):
    global ws_close_count
    try:
        ws_close_count += 1
        tr_now = datetime.now(timezone.utc).astimezone(TR_TZ)
        bot_status["last_run"] = tr_now.strftime("%H:%M:%S")
        heartbeat["loop"] += 1
        beat(symbol=symbol, progress="15m kapanış", status="RUN")

        if df_15m is None or len(df_15m) < 220:
            stats["veri_yetersiz"] += 1
            return

        last = df_15m.iloc[-1]
        if pd.isna(last["atr"]) or float(last["close"]) == 0:
            stats["atr_yok"] += 1
            return

        atr_pct = float(last["atr"]) / float(last["close"])
        if atr_pct < MIN_ATR_PCT:
            stats["atr_dusuk"] += 1
            return

        coin_regime = get_coin_regime_15m(df_15m)
        macro_regime, _ = await get_macro_regime()

        # BTC 15m cache (RS için) - seyrek
        now_ts = time.time()
        if now_ts - btc_15m_cache["ts"] > 60:
            try:
                btc_df = await fetch_ohlcv_df(MACRO_SYMBOL, "15m", 260)
                btc_df = prepare_indicators(btc_df)
                btc_15m_cache["df"] = btc_df
                btc_15m_cache["ts"] = now_ts
            except Exception:
                pass

        # RS filtresi
        if USE_RS_FILTER and btc_15m_cache["df"] is not None:
            rs_score, rs_1h, rs_4h, _, _ = calc_relative_strength(df_15m, btc_15m_cache["df"])
            rs_ok = True
            if not np.isnan(rs_1h) and rs_1h < RS_MIN_REL_1H:
                rs_ok = False
            if not np.isnan(rs_4h) and rs_4h < RS_MIN_REL_4H:
                rs_ok = False
            if np.isnan(rs_score) or rs_score < RS_MIN_SCORE:
                rs_ok = False
            if macro_regime == "RISK_OFF" and (np.isnan(rs_score) or rs_score < RS_RISK_OFF_MIN_SCORE):
                rs_ok = False
            if not rs_ok:
                stats["rs_red"] += 1
                return

        # Strateji seçimi
        signal_found = False
        data = None

        if coin_regime == "UPTREND":
            ok, d = strategy_pullback(df_15m)
            if ok:
                signal_found = True; data = d
            else:
                ok, d = strategy_reaccumulation(df_15m)
                if ok:
                    signal_found = True; data = d
                else:
                    ok, d = strategy_sfp_gold(df_15m)
                    if ok:
                        signal_found = True; data = d
        else:
            ok, d = strategy_sfp_gold(df_15m)
            if ok:
                signal_found = True; data = d

        if not signal_found:
            stats["kurulum_yok"] += 1
            return

        # cooldown
        utc_now = datetime.now(timezone.utc)
        stype = (data.get("type","") or "").upper()
        cooldown_min = COOLDOWN_MINUTES
        if "PULLBACK" in stype:
            cooldown_min = PULLBACK_COOLDOWN_MIN
        elif "RE-ACCUMULATION" in stype:
            cooldown_min = REACCU_COOLDOWN_MIN

        key = (symbol, data.get("type","UNKNOWN"))
        last_ts = last_signal_ts.get(key)
        if last_ts and (utc_now - last_ts) < timedelta(minutes=cooldown_min):
            stats["cooldown"] += 1
            return
        last_signal_ts[key] = utc_now

        # Aday kuyruğa
        stats["aday"] += 1
        if PRINT_ADAY_LOG:
            print(f"🔍 ADAY: {symbol} | {data.get('type','?')} | {tr_now.strftime('%H:%M')}", flush=True)

        await candidate_queue.put(Signal(
            symbol=symbol,
            data=data,
            df_15m=df_15m.copy(),
            coin_regime=coin_regime,
            macro_regime=macro_regime,
            tr_time=tr_now,
        ))

    except Exception as e:
        print(f"⚠️ evaluate error {symbol}: {str(e)[:140]}", flush=True)

# ============================================================
# 15) ADAY DOĞRULAMA WORKER (REST sadece burada)
# ============================================================
async def candidate_worker(candidate_queue: asyncio.Queue):
    while True:
        sig: Signal = await candidate_queue.get()
        try:
            symbol = sig.symbol
            df_15m = sig.df_15m
            data = sig.data
            coin_regime = sig.coin_regime
            macro_regime = sig.macro_regime
            tr_time = sig.tr_time

            # 1h trend
            is_ok, trend_msg = await is_1h_trend_aligned(symbol)
            if not is_ok:
                stats["1h_trend_red"] += 1
                candidate_queue.task_done()
                continue

            # spread
            spread_ok, spread_msg = await check_spread_safety(symbol)
            if not spread_ok:
                stats["spread_red"] += 1
                candidate_queue.task_done()
                continue

            entry_price = float(df_15m["close"].iloc[-1])
            tp_price, tp_pct, tp_note, sr_support = find_structural_target(df_15m, entry_price)
            stop_price = float(data["stop"])

            # SR destek buffer
            atr_now = float(df_15m["atr"].iloc[-1]) if "atr" in df_15m.columns else np.nan
            if sr_support is not None and not pd.isna(atr_now):
                buffer = 0.25 * atr_now
                if stop_price > sr_support:
                    stop_price = float(sr_support - buffer)

            risk_pct = ((entry_price - stop_price) / entry_price) * 100.0
            if tp_pct < risk_pct:
                stats["rr_red"] += 1
                candidate_queue.task_done()
                continue

            position_usdt, coin_amount, actual_risk_pct = calc_position_size(entry_price, stop_price)

            # ticker (likidite)
            liq = ""
            last_price = None
            try:
                t = await api_gate.call(exchange.fetch_ticker, symbol)
                last_price = t.get("last", None)
                qv = float(t.get("quoteVolume", 0) or 0)
                if qv < 5_000_000:
                    liq = f"⚠️ Likidite düşük: ${qv/1e6:.1f}M"
                elif qv < 15_000_000:
                    liq = f"ℹ️ Likidite orta: ${qv/1e6:.1f}M"
            except Exception:
                liq = "❓ Likidite alınamadı"

            entry_s = fmt_price(symbol, entry_price)
            stop_s  = fmt_price(symbol, stop_price)
            tp_s    = fmt_price(symbol, tp_price)
            last_s  = fmt_price(symbol, last_price)

            rr_ratio = (tp_pct / risk_pct) if risk_pct else 0.0
            explain = build_explain_block(df_15m, data)
            signal_time_str = tr_time.strftime("%H:%M")

            msg = f"""
<b>{data['type']}</b>
━━━━━━━━━━━━━━━━━━━━
<b>#{symbol}</b> | <b>Fiyat:</b> {last_s} | 🕒 {signal_time_str}
━━━━━━━━━━━━━━━━━━━━
🧠 <b>BAĞLAM:</b> BTC={macro_regime} | CoinRejimi={coin_regime} | {trend_msg}
{liq}
{spread_msg}
💵 <b>GİRİŞ :</b> {entry_s}
🛡️ <b>STOP  :</b> {stop_s} (Risk: %{risk_pct:.2f})
🎯 <b>HEDEF :</b> {tp_s} (Potansiyel: <b>%{tp_pct:.2f}</b>) • <i>{tp_note}</i>
💰 <b>POZİSYON:</b> ${position_usdt:.0f} (~{coin_amount:.2f}) | GerçekRisk=%{actual_risk_pct:.2f} | RR 1:{rr_ratio:.2f}
📝 <b>NEDEN:</b> {data['desc']}
{explain}
""".strip()

            send_telegram(msg)
            bot_status["signal_count"] += 1
            stats["sinyal_gonderildi"] += 1

            if PRINT_SIGNAL_LOG:
                print(f"✅ SİNYAL: {symbol} | {data['type']} | RR 1:{rr_ratio:.2f}", flush=True)

        except Exception as e:
            print(f"⚠️ Candidate worker err: {str(e)[:160]}", flush=True)
        finally:
            candidate_queue.task_done()

# ============================================================
# 16) MAIN
# ============================================================
def start_flask():
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, use_reloader=False)

async def main():
    print("🚀 Bot başladı", flush=True)

    symbols = await load_symbols_pool()
    if not symbols:
        print("🛑 Sembol havuzu boş.", flush=True)
        return

    global tracked_symbols
    tracked_symbols = list(symbols)

    print(f"✅ Coin sayısı: {len(symbols)} (MAX_SYMBOLS={MAX_SYMBOLS})", flush=True)

    # bootstrap (macro yoksa ekle)
    boot_list = symbols + ([MACRO_SYMBOL] if MACRO_SYMBOL not in symbols else [])
    await bootstrap_all(boot_list)

    # ✅ SADECE BURADA 1 KERE ÖZET
    print_summary()

    # queue + worker
    candidate_queue = asyncio.Queue()
    asyncio.create_task(candidate_worker(candidate_queue))

    # ws listener (multi)
    await ws_listen_klines_multi(symbols, candidate_queue)

if __name__ == "__main__":
    # Flask + Heartbeat + Watchdog + Summary threads
    threading.Thread(target=start_flask, daemon=True).start()
    threading.Thread(target=heartbeat_pinger, daemon=True).start()
    threading.Thread(target=watchdog_thread, daemon=True).start()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("🛑 Durduruldu.", flush=True)
