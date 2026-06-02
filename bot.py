# -*- coding: utf-8 -*-
"""
pump_scanner_v6_20260526.py
════════════════════════════════════════════════════════
3 SİSTEM — TEK BOT  (T24 devre dışı)

SİSTEM 1: PANİK PUMP (Kapitülasyon)
  Crash barı: -15% ile -7% | Hacim 1.5-3x
  Stop: -3% | TP: +5/+10/+15% | Backtest WR: ~%84

SİSTEM 2: PUMP SİNYALİ — KISA VADE (T24)  *** DEVRE DIŞI ***

SİSTEM 3: PUMP SİNYALİ — ORTA VADE (T72)
  mom5_pct>=2.740 & dist_ema21<=-2.737 & coin_drawdown>=-26.796 & ma200_slope>=1.028
  Stop: -5% | TP: +10% | 3 gün | Backtest WR: %54

SİSTEM 4: PUMP SİNYALİ — UZUN VADE (T168)
  dist_ma200>=5.657 & dist_ma50<=-5.045 & mom10_pct>=3.941 & days_since_high<=677
  Stop: -8% | TP: +25% | 7 gün | Backtest WR: %40
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
BINANCE_API_KEY         = os.getenv("BINANCE_API_KEY",         "")
BINANCE_API_SECRET      = os.getenv("BINANCE_API_SECRET",      "")
TELEGRAM_TOKEN          = os.getenv("TELEGRAM_TOKEN",          "")
TELEGRAM_CHAT_ID        = os.getenv("TELEGRAM_CHAT_ID",        "")
PORTFOLIO_URL           = os.getenv("PORTFOLIO_URL",           "")
PORTFOLIO_TOKEN         = os.getenv("PORTFOLIO_TOKEN",         "")
ANTHROPIC_API_KEY       = os.getenv("ANTHROPIC_API_KEY",       "")

# Sistem 1 — Kapitülasyon parametreleri
CRASH_MIN    = float(os.getenv("CRASH_MIN",    "-15.0"))
CRASH_MAX    = float(os.getenv("CRASH_MAX",    "-7.0"))
VOL_MIN      = float(os.getenv("VOL_MIN",      "1.5"))
VOL_MAX      = float(os.getenv("VOL_MAX",      "3.0"))
VOL_PERIOD   = int(os.getenv("VOL_PERIOD",     "20"))

# Genel
MIN_LIQUIDITY         = float(os.getenv("MIN_LIQUIDITY",         "1000000"))
MAX_SYMBOLS           = int(os.getenv("MAX_SYMBOLS",             "0"))
SIGNAL_COOLDOWN_HOURS = int(os.getenv("SIGNAL_COOLDOWN_HOURS",   "4"))
PUMP_COOLDOWN_HOURS   = int(os.getenv("PUMP_COOLDOWN_HOURS",     "4"))
TRAILING_PCT          = float(os.getenv("TRAILING_PCT",          "0.03"))  # %3 trailing stop
TRAILING_MIN_GAIN     = float(os.getenv("TRAILING_MIN_GAIN",     "5.0"))   # %5 kârdan itibaren aktif
WS_STREAM_CHUNK       = int(os.getenv("WS_STREAM_CHUNK",         "120"))
BOOTSTRAP_BARS        = int(os.getenv("BOOTSTRAP_BARS",          "750"))
KEEP_BARS             = int(os.getenv("KEEP_BARS",               "720"))

TR_TZ = timezone(timedelta(hours=3))

IGNORED_COINS = {
    "UP/USDT", "DOWN/USDT", "BEAR/USDT", "BULL/USDT",
    "USDC/USDT", "TUSD/USDT", "FDUSD/USDT", "DAI/USDT", "USDP/USDT",
    "USDE/USDT", "UST/USDT", "USD/USDT", "XUSD/USDT", "USD1/USDT", "BFUSD/USDT",
    "USTC/USDT", "BUSD/USDT", "FRAX/USDT", "LUSD/USDT", "GUSD/USDT", "SUSD/USDT",
    "USDS/USDT", "USDX/USDT", "USDD/USDT", "CUSD/USDT", "OUSD/USDT", "MUSD/USDT",
    "U/USDT",
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

bars_1h:        dict = {}
funding_cache:  dict = {}
last_signal_ts: dict = {}       # kapitülasyon cooldown: symbol → datetime
last_pump_ts:   dict = {}       # T24/T72/T168 cooldown: (symbol, sig_type) → datetime
all_signals:    list = []
btc_4h_cache:   dict = {"trend": "?", "ema50": None, "close": None, "updated": None}
heartbeat = {"last": "", "epoch": time.time(), "symbol": "?"}
bot_status = {"status": "BOOT"}
_recent_signal_times: list = []  # UTC datetimes of signals fired in last 60 min (clustering detection)

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
exchange_fut = ccxt.binance({
    "options": {"defaultType": "future", "adjustForTimeDifference": True},
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

    # --- Mevcut göstergeler (Sistem 1) ---
    df["vol_ma"] = v.rolling(VOL_PERIOD).mean()
    tr        = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    df["atr"] = tr.ewm(alpha=1/14, adjust=False).mean()
    df["atr_pct"]    = df["atr"] / c * 100
    df["ema50"]      = c.ewm(span=50,  adjust=False).mean()
    df["ema200"]     = c.ewm(span=200, adjust=False).mean()
    df["close_prev"] = c.shift(1)

    # --- Yeni göstergeler (Sistem 2-3-4) ---
    ema21  = c.ewm(span=21,  adjust=False).mean()
    ma50   = c.rolling(50).mean()
    ma200  = c.rolling(200).mean()

    df["ema21"]      = ema21
    df["dist_ema21"] = (c - ema21)  / ema21.replace(0, np.nan)  * 100
    df["dist_ma50"]  = (c - ma50)   / ma50.replace(0, np.nan)   * 100
    df["dist_ma200"] = (c - ma200)  / ma200.replace(0, np.nan)  * 100
    df["ma200_slope"]= (ma200 - ma200.shift(20)) / ma200.shift(20).abs().replace(0, np.nan) * 100

    bb_mid = c.rolling(20).mean()
    bb_std = c.rolling(20).std()
    bb_up  = bb_mid + 2 * bb_std
    bb_dn  = bb_mid - 2 * bb_std
    bb_w   = (bb_up - bb_dn) / bb_mid.replace(0, np.nan)
    df["bb_width"]     = bb_w
    df["bb_width_ch3"] = bb_w.diff(3)

    df["mom5_pct"]  = (c - c.shift(5))  / c.shift(5).abs().replace(0, np.nan)  * 100
    df["mom10_pct"] = (c - c.shift(10)) / c.shift(10).abs().replace(0, np.nan) * 100

    # Coin drawdown: yüksekten ne kadar uzakta (%)
    roll_max = c.rolling(KEEP_BARS, min_periods=50).max()
    df["coin_drawdown"] = (c - roll_max) / roll_max.replace(0, np.nan) * 100

    # Son ATH'dan bu yana kaç bar geçti
    bar_idx       = pd.Series(np.arange(len(c), dtype=float), index=c.index)
    is_at_high    = c >= roll_max * (1 - 1e-6)
    last_high_pos = bar_idx.where(is_at_high).ffill().fillna(0)
    df["days_since_high"] = bar_idx - last_high_pos

    return df.dropna(subset=["vol_ma", "atr", "close_prev"])

# ============================================================
# MUM FORMASYONLARI
# ============================================================
def _ohlc(df, i):
    row = df.iloc[i]
    return float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"])

def is_hammer(df, i):
    if i < 1: return False
    o, h, l, c = _ohlc(df, i)
    if c <= o: return False
    body = c - o; rng = h - l
    if rng <= 0 or body <= 0: return False
    return ((o - l) >= body * 2.0 and (h - c) <= body * 0.5 and (o - l) / rng >= 0.55)

def is_engulfing(df, i):
    if i < 1: return False
    o0, _, _, c0 = _ohlc(df, i); o1, _, _, c1 = _ohlc(df, i - 1)
    b0 = abs(c0 - o0); b1 = abs(c1 - o1)
    if b1 <= 0: return False
    return c1 < o1 and c0 > o0 and o0 <= c1 and c0 >= o1 and b0 >= b1 * 0.8

def is_morning_star(df, i):
    if i < 2: return False
    o0, _, _, c0 = _ohlc(df, i); o1, _, _, c1 = _ohlc(df, i-1); o2, _, _, c2 = _ohlc(df, i-2)
    b0=abs(c0-o0); b1=abs(c1-o1); b2=abs(c2-o2)
    avg=(b0+b1+b2)/3 if (b0+b1+b2)>0 else 1
    return (c2<o2 and b2>=avg and b1<=avg*0.5 and c0>o0 and b0>=avg and c0>=o2-b2/2)

def detect_candle(df, i):
    if is_morning_star(df, i): return "🌅 Morning Star"
    if is_engulfing(df, i):    return "🟢 Bullish Engulfing"
    if is_hammer(df, i):       return "🔨 Hammer"
    return ""

# ============================================================
# SİSTEM 1 — KAPİTÜLASYON SİNYALİ (değişmedi)
# ============================================================
def check_capitulation_signal(df: pd.DataFrame, symbol: str) -> dict | None:
    if len(df) < VOL_PERIOD + 5: return None
    bar = df.iloc[-1]
    def gv(col):
        val = bar.get(col, np.nan)
        return None if pd.isna(val) else float(val)
    close_now  = gv("close"); close_prev = gv("close_prev")
    vol_now    = gv("volume"); vol_ma    = gv("vol_ma")
    atr_val    = gv("atr");   atr_pct   = gv("atr_pct")
    if None in (close_now, close_prev, vol_now, vol_ma, atr_val): return None
    if close_prev == 0 or vol_ma == 0: return None
    ret1      = (close_now / close_prev - 1) * 100
    vol_ratio = vol_now / vol_ma
    if not (CRASH_MIN <= ret1 <= CRASH_MAX):
        stats["filtered_crash"] += 1; return None
    if not (VOL_MIN <= vol_ratio <= VOL_MAX):
        stats["filtered_vol"] += 1; return None
    entry = close_now
    funding     = funding_cache.get(symbol)
    funding_neg = funding is not None and funding < 0
    return {
        "symbol": symbol, "type": "capit",
        "entry": round(entry, 8), "stop": round(entry * 0.97, 8),
        "tp1": round(entry * 1.05, 8), "tp2": round(entry * 1.10, 8), "tp3": round(entry * 1.15, 8),
        "ret1": round(ret1, 2), "vol_ratio": round(vol_ratio, 2), "vol_ma": round(vol_ma, 2),
        "atr": round(atr_val, 8), "atr_pct": round(atr_pct, 2) if atr_pct else None,
        "funding": round(funding, 6) if funding is not None else None,
        "funding_neg": funding_neg,
        "candle": detect_candle(df, len(df) - 1),
    }

# ============================================================
# SİSTEM 2 — KISA VADE (T24)
# ============================================================
def check_t24_signal(df: pd.DataFrame, symbol: str) -> dict | None:
    """dist_ema21>=2.566 & dist_ma50<=-5.045 & bb_width_ch3<=-0.019 & ma200_slope<=-1.933"""
    if len(df) < 50: return None
    bar = df.iloc[-1]
    def gv(col):
        val = bar.get(col, np.nan)
        return None if pd.isna(val) else float(val)
    dist_ema21   = gv("dist_ema21")
    dist_ma50    = gv("dist_ma50")
    bb_width_ch3 = gv("bb_width_ch3")
    ma200_slope  = gv("ma200_slope")
    close        = gv("close")
    if None in (dist_ema21, dist_ma50, bb_width_ch3, ma200_slope, close): return None
    if not (dist_ema21 >= 2.566 and dist_ma50 <= -5.045 and
            bb_width_ch3 <= -0.019 and ma200_slope <= -1.933): return None
    return {
        "symbol": symbol, "type": "t24",
        "entry": round(close, 8),
        "stop":  round(close * 0.97, 8),
        "tp1":   round(close * 1.05, 8),
        "tp2":   round(close * 1.05, 8),
        "dist_ema21":   round(dist_ema21, 2),
        "dist_ma50":    round(dist_ma50, 2),
        "bb_width_ch3": round(bb_width_ch3, 4),
        "ma200_slope":  round(ma200_slope, 3),
        "atr_pct":      round(float(bar.get("atr_pct") or 0), 2),
    }

# ============================================================
# SİSTEM 3 — ORTA VADE (T72)
# ============================================================
def check_t72_signal(df: pd.DataFrame, symbol: str) -> dict | None:
    """mom5_pct>=2.740 & dist_ema21<=-2.737 & coin_drawdown>=-26.796 & ma200_slope>=1.028"""
    if len(df) < 50: return None
    bar = df.iloc[-1]
    def gv(col):
        val = bar.get(col, np.nan)
        return None if pd.isna(val) else float(val)
    mom5_pct      = gv("mom5_pct")
    dist_ema21    = gv("dist_ema21")
    coin_drawdown = gv("coin_drawdown")
    ma200_slope   = gv("ma200_slope")
    close         = gv("close")
    if None in (mom5_pct, dist_ema21, coin_drawdown, ma200_slope, close): return None
    if not (mom5_pct >= 2.740 and dist_ema21 <= -2.737 and
            coin_drawdown >= -26.796 and ma200_slope >= 1.028): return None
    return {
        "symbol": symbol, "type": "t72",
        "entry": round(close, 8),
        "stop":  round(close * 0.95, 8),
        "tp1":   round(close * 1.10, 8),
        "tp2":   round(close * 1.10, 8),
        "mom5_pct":      round(mom5_pct, 2),
        "dist_ema21":    round(dist_ema21, 2),
        "coin_drawdown": round(coin_drawdown, 2),
        "ma200_slope":   round(ma200_slope, 3),
        "atr_pct":       round(float(bar.get("atr_pct") or 0), 2),
    }

# ============================================================
# SİSTEM 4 — UZUN VADE (T168)
# ============================================================
def check_t168_signal(df: pd.DataFrame, symbol: str) -> dict | None:
    """dist_ma200>=5.657 & dist_ma50<=-5.045 & mom10_pct>=3.941 & days_since_high<=677"""
    if len(df) < 50: return None
    bar = df.iloc[-1]
    def gv(col):
        val = bar.get(col, np.nan)
        return None if pd.isna(val) else float(val)
    dist_ma200      = gv("dist_ma200")
    dist_ma50       = gv("dist_ma50")
    mom10_pct       = gv("mom10_pct")
    days_since_high = gv("days_since_high")
    close           = gv("close")
    if None in (dist_ma200, dist_ma50, mom10_pct, days_since_high, close): return None
    if not (dist_ma200 >= 5.657 and dist_ma50 <= -5.045 and
            mom10_pct >= 3.941 and days_since_high <= 677): return None
    return {
        "symbol": symbol, "type": "t168",
        "entry": round(close, 8),
        "stop":  round(close * 0.92, 8),
        "tp1":   round(close * 1.25, 8),
        "tp2":   round(close * 1.25, 8),
        "dist_ma200":      round(dist_ma200, 2),
        "dist_ma50":       round(dist_ma50, 2),
        "mom10_pct":       round(mom10_pct, 2),
        "days_since_high": int(days_since_high),
        "atr_pct":         round(float(bar.get("atr_pct") or 0), 2),
    }

# ============================================================
# PUANLAMA (Sistem 1 için)
# ============================================================
def calc_signal_score(ret1: float, vol_ratio: float, liquidity: float) -> int:
    abs_ret = abs(ret1)
    if abs_ret >= 12:   crash_score = 5
    elif abs_ret >= 10: crash_score = 4
    elif abs_ret >= 9:  crash_score = 3
    elif abs_ret >= 8:  crash_score = 2
    else:               crash_score = 1
    if 1.8 <= vol_ratio <= 2.5:  vol_score = 5
    elif 1.5 <= vol_ratio < 1.8: vol_score = 3
    elif 2.5 < vol_ratio <= 3.0: vol_score = 3
    else:                         vol_score = 1
    if liquidity >= 15_000_000:  liq_score = 5
    elif liquidity >= 5_000_000: liq_score = 3
    else:                        liq_score = 1
    raw = crash_score * 0.5 + vol_score * 0.3 + liq_score * 0.2
    return max(1, min(5, round(raw)))

def _stars(score: int) -> str:
    return "⭐" * score

# ============================================================
# FUNDING + BTC 4H
# ============================================================
async def fetch_funding_rate(symbol):
    sym_fut = symbol.replace("/USDT", "USDT")
    try:
        loop = asyncio.get_running_loop()
        data = await loop.run_in_executor(None, lambda: exchange_fut.fetch_funding_rate(sym_fut))
        rate = data.get("fundingRate")
        funding_cache[symbol] = float(rate) if rate is not None else None
    except Exception:
        funding_cache[symbol] = None

async def refresh_funding_cache(symbols):
    print("  Funding cache dolduruluyor...", flush=True)
    ok = 0
    for sym in symbols:
        await fetch_funding_rate(sym)
        if funding_cache.get(sym) is not None: ok += 1
        await asyncio.sleep(0.05)
    print(f"  → {ok}/{len(symbols)} sembolde funding rate", flush=True)

async def refresh_btc_4h():
    try:
        df = await fetch_df("BTC/USDT", "4h", 100)
        if df is None or len(df) < 50: return
        df = prepare_bars(df)
        lc=float(df["close"].iloc[-1]); le50=float(df["ema50"].iloc[-1]); le200=float(df["ema200"].iloc[-1])
        if lc > le50 > le200:   trend = "⬆️ Güçlü Yükseliş"
        elif lc > le50:         trend = "🟢 Yükseliş"
        elif lc > le200:        trend = "🟡 Karışık"
        else:                   trend = "🔴 Düşüş"
        btc_4h_cache.update({"trend": trend, "ema50": le50, "close": lc,
                              "updated": datetime.now(timezone.utc).strftime("%H:%M")})
        print(f"BTC 4H: {trend} | Fiyat:{lc:.0f} EMA50:{le50:.0f}", flush=True)
    except Exception as e:
        print(f"BTC 4H hata: {e}", flush=True)

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

def _pct(new, ref):
    if not ref: return "?"
    return f"+{round((new/ref-1)*100, 1)}%"

def _vol_risk(atr_pct):
    if atr_pct is None: return "?"
    if atr_pct >= 5.0:  return f"⚠️ Yüksek (%{atr_pct:.1f})"
    if atr_pct >= 2.5:  return f"🟡 Orta (%{atr_pct:.1f})"
    return f"🟢 Düşük (%{atr_pct:.1f})"

def _sep():
    return "━━━━━━━━━━━━━━━━━━━━"

# ============================================================
# TELEGRAM MESAJ OLUŞTURUCULAR
# ============================================================
def build_capitulation_message(r, tr_time, sig_num):
    sym  = r["symbol"].replace("/USDT", "")
    e    = r["entry"]
    icon = "💰" if r.get("funding_neg") else "🔴"
    ret1 = r.get("ret1", 0); vr = r.get("vol_ratio", 0)
    lines = [
        f"🕐 {tr_time.strftime('%d/%m/%Y %H:%M')}",
        "",
        f"{icon} <b>#{sym}/USDT  •  PANİK PUMP  •  1H</b>",
        _sep(),
        f"💵 <b>Giriş</b>    {fmt_price(e)}",
        f"🛡️ <b>Stop</b>     {fmt_price(r['stop'])}  (-3%)",
        f"🎯 <b>TP1</b>      {fmt_price(r['tp1'])}  (+5%)",
        f"🎯 <b>TP2</b>      {fmt_price(r['tp2'])}  (+10%)",
        f"🎯 <b>TP3</b>      {fmt_price(r['tp3'])}  (+15%)",
        _sep(),
        "📊 <b>Göstergeler</b>",
        f"📉 <b>Düşüş</b>     {ret1:+.2f}%  (panik satışı)",
        f"📊 <b>Hacim</b>     {vr:.2f}x ortalama  🔥",
    ]
    if r.get("funding") is not None:
        lines.append(f"<b>Funding</b>    {r['funding']:+.4f}%{'  💰' if r.get('funding_neg') else ''}")
    if r.get("candle"):
        lines.append(f"<b>Formasyon</b>  {r['candle']}  ✅")
    lines += [
        _sep(),
        f"<b>BTC 4H</b>     {btc_4h_cache.get('trend','?')}",
        f"<b>Vol. Risk</b>  {_vol_risk(r.get('atr_pct'))}",
        _sep(),
        f"⏱ Geçmiş başarı: ~%84 (TP+15%)  |  #{sig_num} sinyal",
    ]
    return "\n".join(lines)


def build_t24_message(r, tr_time, sig_num):
    sym = r["symbol"].replace("/USDT", "")
    e   = r["entry"]
    lines = [
        f"🕐 {tr_time.strftime('%d/%m/%Y %H:%M')}",
        "",
        f"🔴 <b>#{sym}/USDT  •  PUMP SİNYALİ  •  KISA VADE</b>",
        _sep(),
        f"💵 <b>Giriş</b>   {fmt_price(e)}",
        f"🛑 <b>Stop</b>    {fmt_price(r['stop'])}  (-%3)",
        f"🎯 <b>Hedef</b>   {fmt_price(r['tp1'])}  (+%5)",
        _sep(),
        "📊 <b>Göstergeler</b>",
        f"📈 Fiyat, EMA21'in %{abs(r['dist_ema21']):.1f} üstünde",
        f"📉 Fiyat, MA50'nin %{abs(r['dist_ma50']):.1f} altında",
        f"📉 Bollinger Bandı daralıyor ({r['bb_width_ch3']:.4f})",
        f"📉 MA200 aşağı eğimli (%{r['ma200_slope']:.2f}/20 bar)",
        _sep(),
        f"<b>BTC 4H</b>    {btc_4h_cache.get('trend','?')}",
        f"<b>Vol. Risk</b> {_vol_risk(r.get('atr_pct'))}",
        _sep(),
        f"⏱ Geçmiş başarı: %65  |  #{sig_num} sinyal",
    ]
    return "\n".join(lines)


def build_t72_message(r, tr_time, sig_num):
    sym = r["symbol"].replace("/USDT", "")
    e   = r["entry"]
    lines = [
        f"🕐 {tr_time.strftime('%d/%m/%Y %H:%M')}",
        "",
        f"🟡 <b>#{sym}/USDT  •  PUMP SİNYALİ  •  ORTA VADE</b>",
        _sep(),
        f"💵 <b>Giriş</b>   {fmt_price(e)}",
        f"🛑 <b>Stop</b>    {fmt_price(r['stop'])}  (-%5)",
        f"🎯 <b>Hedef</b>   {fmt_price(r['tp1'])}  (+%10)",
        _sep(),
        "📊 <b>Göstergeler</b>",
        f"📈 Kısa vadeli momentum: +%{r['mom5_pct']:.1f}",
        f"📉 Fiyat, EMA21'in %{abs(r['dist_ema21']):.1f} altında (geri çekilme)",
        f"✅ Düşüş %{abs(r['coin_drawdown']):.1f} (sağlıklı seviye)",
        f"📈 MA200 yukarı eğimli (+%{r['ma200_slope']:.2f}/20 bar)",
        _sep(),
        f"<b>BTC 4H</b>    {btc_4h_cache.get('trend','?')}",
        f"<b>Vol. Risk</b> {_vol_risk(r.get('atr_pct'))}",
        _sep(),
        f"⏱ Geçmiş başarı: %54  |  #{sig_num} sinyal",
    ]
    return "\n".join(lines)


def build_t168_message(r, tr_time, sig_num):
    sym = r["symbol"].replace("/USDT", "")
    e   = r["entry"]
    lines = [
        f"🕐 {tr_time.strftime('%d/%m/%Y %H:%M')}",
        "",
        f"🟢 <b>#{sym}/USDT  •  PUMP SİNYALİ  •  UZUN VADE</b>",
        _sep(),
        f"💵 <b>Giriş</b>   {fmt_price(e)}",
        f"🛑 <b>Stop</b>    {fmt_price(r['stop'])}  (-%8)",
        f"🎯 <b>Hedef</b>   {fmt_price(r['tp1'])}  (+%25)",
        _sep(),
        "📊 <b>Göstergeler</b>",
        f"📈 Fiyat, MA200'ün %{abs(r['dist_ma200']):.1f} üstünde",
        f"📉 Fiyat, MA50'nin %{abs(r['dist_ma50']):.1f} altında",
        f"📈 10 bar momentum: +%{r['mom10_pct']:.1f}",
        f"⏰ Son zirve: {r['days_since_high']} bar önce",
        _sep(),
        f"<b>BTC 4H</b>    {btc_4h_cache.get('trend','?')}",
        f"<b>Vol. Risk</b> {_vol_risk(r.get('atr_pct'))}",
        _sep(),
        f"⏱ Geçmiş başarı: %40  |  #{sig_num} sinyal",
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
                  "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=10,
        )
        if r.status_code != 200:
            print(f"Telegram {r.status_code}: {r.text[:80]}", flush=True)
    except Exception as e:
        print(f"Telegram hata: {e}", flush=True)

def _notify_trailing_activated(entry, old_stop):
    try:
        sym  = entry["symbol"].replace("/USDT", "")
        raw  = entry.get("raw_type", entry.get("sig_type", "capit"))
        lbl  = {"capit": "PANİK PUMP", "t72": "ORTA VADE", "t168": "UZUN VADE",
                "pump_prob": "PUMP PROB"}.get(raw, raw)
        e        = entry["entry"]
        old_pct  = round((old_stop / e - 1) * 100, 1)
        new_pct  = round((entry["stop"] / e - 1) * 100, 1)
        peak_pct = entry["peak_pct"]
        msg = (
            f"🔄 <b>Trailing Stop Devreye Girdi</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"#{sym}/USDT  [{lbl}]\n"
            f"Peak: <b>+{peak_pct:.1f}%</b>\n"
            f"Eski Stop: {old_pct:+.1f}%  →  Yeni Stop: <b>{new_pct:+.1f}%</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━"
        )
        send_telegram(msg)
    except Exception as ex:
        print(f"[TRAILING] Bildirim hata: {ex}", flush=True)

def _notify_trailing_close(entry, close_price, close_ret):
    try:
        sym  = entry["symbol"].replace("/USDT", "")
        raw  = entry.get("raw_type", entry.get("sig_type", "capit"))
        lbl  = {"capit": "PANİK PUMP", "t72": "ORTA VADE", "t168": "UZUN VADE",
                "pump_prob": "PUMP PROB"}.get(raw, raw)
        msg = (
            f"✅ <b>Trailing Stop — Kâr Kapatıldı</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"#{sym}/USDT  [{lbl}]\n"
            f"Giriş: {fmt_price(entry['entry'])}  →  Çıkış: {fmt_price(close_price)}\n"
            f"Kâr: <b>+{close_ret:.1f}%</b>  |  Peak: +{entry.get('peak_pct', 0):.1f}%\n"
            f"━━━━━━━━━━━━━━━━━━━━"
        )
        send_telegram(msg)
    except Exception as ex:
        print(f"[TRAILING CLOSE] Bildirim hata: {ex}", flush=True)

# sig_type → portfolio type eşleştirmesi
_SIG_TYPE_MAP = {
    "capit": "panik_pump",
    "t24":   "pump_kisa",
    "t72":   "pump_orta",
    "t168":  "pump_uzun",
}

def send_to_portfolio(result):
    if not PORTFOLIO_URL: return
    try:
        sig_type = _SIG_TYPE_MAP.get(result.get("type", "capit"), "panik_pump")
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
            "funding_neg": result.get("funding_neg", False),
        }
        headers = {"Content-Type": "application/json"}
        if PORTFOLIO_TOKEN:
            headers["Authorization"] = f"Bearer {PORTFOLIO_TOKEN}"
        r = requests.post(f"{PORTFOLIO_URL}/api/signal",
                          json=payload, headers=headers, timeout=5)
        if r.status_code == 201:
            print(f"[PORTFOLIO] Sinyal gönderildi: {result['symbol']} ({sig_type})", flush=True)
        elif r.status_code == 409:
            print(f"[PORTFOLIO] Zaten açık: {result['symbol']}", flush=True)
        else:
            print(f"[PORTFOLIO] HTTP {r.status_code}: {r.text[:80]}", flush=True)
    except Exception as e:
        print(f"[PORTFOLIO] Hata: {e}", flush=True)

# ============================================================
# CLAUDE SHADOW MODE
# ============================================================
def _recent_signal_count() -> int:
    """Signals fired in the last 60 minutes (clustering detection)."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
    _recent_signal_times[:] = [t for t in _recent_signal_times if t > cutoff]
    return len(_recent_signal_times)

def _record_signal_time():
    _recent_signal_times.append(datetime.now(timezone.utc))

def ask_claude_shadow(result: dict, recent_count: int) -> str:
    if not ANTHROPIC_API_KEY:
        return ""
    try:
        import anthropic
        sym      = result["symbol"].replace("/USDT", "")
        sig_type = result.get("type", "capit")
        btc_trend = btc_4h_cache.get("trend", "?")

        type_names = {
            "capit": "PANİK PUMP (kapitülasyon mean reversion, Stop -3%, TP +5/10/15%, WR ~%84)",
            "t72":   "ORTA VADE PUMP T72 (3 gün hedef, Stop -5%, TP +10%, WR %54)",
            "t168":  "UZUN VADE PUMP T168 (7 gün hedef, Stop -8%, TP +25%, WR %40)",
        }

        if sig_type == "capit":
            indicators = (
                f"Düşüş: {result.get('ret1', 0):+.2f}% (panik satışı)\n"
                f"Hacim: {result.get('vol_ratio', 0):.2f}x ortalama (beklenen: 1.5-3x)\n"
                f"ATR volatilite: %{result.get('atr_pct', 0):.2f}\n"
                f"Funding rate: {result.get('funding', 'bilinmiyor')}"
            )
        elif sig_type == "t72":
            indicators = (
                f"5 bar momentum: +%{result.get('mom5_pct', 0):.2f}\n"
                f"EMA21 uzaklık: %{result.get('dist_ema21', 0):.2f} (altında — geri çekilme)\n"
                f"Coin drawdown: %{result.get('coin_drawdown', 0):.2f}\n"
                f"MA200 eğimi: +%{result.get('ma200_slope', 0):.3f}/20 bar (yukarı = iyi)"
            )
        elif sig_type == "t168":
            indicators = (
                f"MA200 uzaklık: +%{result.get('dist_ma200', 0):.2f}\n"
                f"MA50 uzaklık: %{result.get('dist_ma50', 0):.2f}\n"
                f"10 bar momentum: +%{result.get('mom10_pct', 0):.2f}\n"
                f"Son zirve: {result.get('days_since_high', 0)} bar önce"
            )
        else:
            indicators = ""

        clustering_note = ""
        if recent_count >= 3:
            clustering_note = f"\n⚠️ DİKKAT: Son 1 saatte {recent_count} farklı coin sinyal verdi — BTC çöküşü riski yüksek!"
        elif recent_count >= 2:
            clustering_note = f"\n🟡 NOT: Son 1 saatte {recent_count} sinyal — piyasa genelinde baskı olabilir."

        prompt = f"""Sen bir kripto sinyal değerlendirme asistanısın. Aşağıdaki sinyali analiz et:{clustering_note}

KOİN: #{sym}/USDT
SİSTEM: {type_names.get(sig_type, sig_type)}
BTC 4H TREND: {btc_trend}
SON 1 SAATTEKİ SİNYAL SAYISI: {recent_count}

GÖSTERGELER:
{indicators}

Değerlendirmeni SADECE şu formatta ver (4-5 satır max):
KARAR: [✅ GİR / ⚠️ DİKKAT / 🚫 RİSKLİ]
GEREKÇE: (1-2 cümle)
UYARI: (varsa 1 cümle, yoksa bu satırı yazma)"""

        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        resp   = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=180,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text.strip()
    except Exception as e:
        print(f"[CLAUDE] API hata: {e}", flush=True)
        return ""

def send_claude_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID, "text": text,
                "parse_mode": "HTML", "disable_web_page_preview": True,
            },
            timeout=10,
        )
        if r.status_code != 200:
            print(f"[CLAUDE TG] {r.status_code}: {r.text[:80]}", flush=True)
    except Exception as e:
        print(f"[CLAUDE TG] Hata: {e}", flush=True)

async def _claude_shadow_task(result: dict, recent_count: int, sig_num: int):
    try:
        loop = asyncio.get_running_loop()
        claude_text = await loop.run_in_executor(
            None, lambda: ask_claude_shadow(result, recent_count)
        )
        if not claude_text:
            return
        sym       = result["symbol"].replace("/USDT", "")
        sig_type  = result.get("type", "capit")
        type_short = {"capit": "PANİK PUMP", "t72": "ORTA VADE", "t168": "UZUN VADE"}.get(sig_type, sig_type)
        msg = (
            f"🤖 <b>CLAUDE — #{sym}/USDT [{type_short}] #{sig_num}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"{claude_text}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"<i>Gölge mod · sinyal #{sig_num} · girilmedi</i>"
        )
        send_claude_telegram(msg)
        print(f"[CLAUDE] #{sym} değerlendirmesi gönderildi", flush=True)
    except Exception as e:
        print(f"[CLAUDE] Shadow task hata: {e}", flush=True)

# ============================================================
# PERFORMANS TAKİP
# ============================================================
SIGNAL_LOG_PATH = "/tmp/signal_log.json"
_EXPIRE_H = {"capit": 24, "t24": 24, "t72": 72, "t168": 168}

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
    sig_type = result.get("type", "capit")
    entry_rec = {
        "id":          f"{result['symbol']}_{sig_type}_{int(tr_time.timestamp())}",
        "symbol":      result["symbol"],
        "sig_type":    _SIG_TYPE_MAP.get(sig_type, "panik_pump"),
        "raw_type":    sig_type,
        "entry":       result["entry"],
        "stop":        result["stop"],
        "tp1":         result.get("tp1"),
        "tp2":         result.get("tp2"),
        "tp3":         result.get("tp3"),
        "expire_h":    _EXPIRE_H.get(sig_type, 24),
        "funding_neg": result.get("funding_neg", False),
        "candle":      result.get("candle", ""),
        "time":        tr_time.isoformat(),
        "status":      "open",
        "peak_pct": 0.0, "tp1_hit": False, "tp2_hit": False, "tp3_hit": False,
        "trailing_active": False, "trailing_stop": None,
        "close_time": None, "close_price": None, "close_ret": None,
        "ret1":      result.get("ret1"),
        "vol_ratio": result.get("vol_ratio"),
        "score":     result.get("score"),
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

        # Trailing stop güncelleme — peak %5'i geçince devreye girer
        if entry.get("trailing_active") or entry["peak_pct"] >= TRAILING_MIN_GAIN:
            peak_price    = e * (1 + entry["peak_pct"] / 100)
            new_trailing  = round(peak_price * (1 - TRAILING_PCT), 8)
            if new_trailing > entry["stop"]:
                was_active = entry.get("trailing_active", False)
                old_stop   = entry["stop"]
                entry["stop"]           = new_trailing
                entry["trailing_stop"]  = new_trailing
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
            symbol  = sig.symbol
            result  = sig.result
            tr_time = sig.tr_time
            sig_type = result.get("type", "capit")

            # Likidite kontrolü
            liquidity = 0.0
            try:
                ticker    = await api_gate.call(exchange_spot.fetch_ticker, symbol)
                liquidity = float(ticker.get("quoteVolume", 0) or 0)
                if liquidity < MIN_LIQUIDITY:
                    stats["low_liquidity"] += 1
                    continue
            except Exception:
                pass

            result["liquidity"] = liquidity

            if sig_type == "capit":
                result["score"] = calc_signal_score(
                    result.get("ret1", 0), result.get("vol_ratio", 0), liquidity)
                await fetch_funding_rate(symbol)
                result["funding"]     = funding_cache.get(symbol)
                result["funding_neg"] = result["funding"] is not None and result["funding"] < 0
                msg = build_capitulation_message(result, tr_time, signal_counter + 1)
                icon = "💰" if result.get("funding_neg") else "🔴"
                print(f"SİNYAL {icon} [PANİK PUMP] {symbol}"
                      f" | düşüş:{result['ret1']:+.2f}% | vol:{result['vol_ratio']:.2f}x"
                      f" | giriş:{fmt_price(result['entry'])}", flush=True)
            elif sig_type == "t24":
                msg = build_t24_message(result, tr_time, signal_counter + 1)
                print(f"SİNYAL 🔴 [KISA VADE] {symbol}"
                      f" | ema21:+%{result['dist_ema21']:.1f}"
                      f" | ma50:%{result['dist_ma50']:.1f}"
                      f" | giriş:{fmt_price(result['entry'])}", flush=True)
            elif sig_type == "t72":
                msg = build_t72_message(result, tr_time, signal_counter + 1)
                print(f"SİNYAL 🟡 [ORTA VADE] {symbol}"
                      f" | mom5:+%{result['mom5_pct']:.1f}"
                      f" | ema21:%{result['dist_ema21']:.1f}"
                      f" | giriş:{fmt_price(result['entry'])}", flush=True)
            elif sig_type == "t168":
                msg = build_t168_message(result, tr_time, signal_counter + 1)
                print(f"SİNYAL 🟢 [UZUN VADE] {symbol}"
                      f" | ma200:+%{result['dist_ma200']:.1f}"
                      f" | mom10:+%{result['mom10_pct']:.1f}"
                      f" | giriş:{fmt_price(result['entry'])}", flush=True)
            else:
                continue

            signal_counter += 1
            send_telegram(msg)
            send_to_portfolio(result)

            # Claude shadow mode
            if ANTHROPIC_API_KEY and TELEGRAM_CHAT_ID:
                recent_cnt = _recent_signal_count()
                _record_signal_time()
                asyncio.create_task(_claude_shadow_task(result, recent_cnt, signal_counter))

            # Cooldown güncelle
            if sig_type == "capit":
                last_signal_ts[symbol] = tr_time.replace(tzinfo=None)
            else:
                last_pump_ts[(symbol, sig_type)] = tr_time.replace(tzinfo=None)

            result["time"]     = tr_time.strftime("%Y-%m-%d %H:%M")
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

    # --- Sistem 1: Kapitülasyon ---
    last_c = last_signal_ts.get(symbol)
    capit_ok = True
    if last_c is not None:
        elapsed = (tr_time.replace(tzinfo=None) - last_c.replace(tzinfo=None)).total_seconds() / 3600
        if elapsed < SIGNAL_COOLDOWN_HOURS:
            stats["cooldown"] += 1; capit_ok = False
    if capit_ok:
        result = check_capitulation_signal(df, symbol)
        if result:
            await candidate_queue.put(SignalCandidate(symbol, result, tr_time))
        else:
            stats["capit_filtered"] += 1

    # --- Sistem 2: T24 --- (DEVRE DIŞI - 2026-05-26 analiz sonucu kapatildi)
    # T24 backtest: WR %%10, ort getiri -%%1.7 -> para kaybettiriyor
    pass  # T24 disabled

    # --- Sistem 3: T72 ---
    last_t72 = last_pump_ts.get((symbol, "t72"))
    t72_ok = True
    if last_t72 is not None:
        elapsed = (tr_time.replace(tzinfo=None) - last_t72.replace(tzinfo=None)).total_seconds() / 3600
        if elapsed < PUMP_COOLDOWN_HOURS:
            t72_ok = False
    if t72_ok:
        result = check_t72_signal(df, symbol)
        if result:
            await candidate_queue.put(SignalCandidate(symbol, result, tr_time))

    # --- Sistem 4: T168 ---
    last_t168 = last_pump_ts.get((symbol, "t168"))
    t168_ok = True
    if last_t168 is not None:
        elapsed = (tr_time.replace(tzinfo=None) - last_t168.replace(tzinfo=None)).total_seconds() / 3600
        if elapsed < PUMP_COOLDOWN_HOURS:
            t168_ok = False
    if t168_ok:
        result = check_t168_signal(df, symbol)
        if result:
            await candidate_queue.put(SignalCandidate(symbol, result, tr_time))

# ============================================================
# WEBSOCKET
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
                        sym = data.get("data", {}).get("s", "").upper().replace("USDT", "/USDT")
                        try:
                            await on_1h_close(sym,
                                float(k["o"]), float(k["h"]), float(k["l"]), float(k["c"]),
                                float(k["v"]), int(k["t"]), candidate_queue)
                        except Exception as e:
                            print(f"on_1h_close hata [{sym}]: {str(e)[:80]}", flush=True)
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
    "capit": ("🔴", "PANİK PUMP",   "#ff4444"),
    "t24":   ("🔴", "KISA VADE",    "#ff8800"),
    "t72":   ("🟡", "ORTA VADE",    "#ffcc00"),
    "t168":  ("🟢", "UZUN VADE",    "#00cc66"),
}

@flask_app.route("/")
def home():
    now = tr_now().strftime("%H:%M:%S")
    sig_rows = ""
    for s in all_signals[:40]:
        stype = s.get("type", "capit")
        icon, label, bc = _TYPE_LABEL.get(stype, ("🔴", "SİNYAL", "#ff4444"))
        e = s.get("entry"); stop = s.get("stop"); tp1 = s.get("tp1"); tp2 = s.get("tp2")

        if stype == "capit":
            ret1_v = s.get("ret1", 0); vr_v = s.get("vol_ratio", 0)
            ind = f"Düşüş:{ret1_v:+.2f}%  Hacim:{vr_v:.2f}x"
            fn = s.get("funding_neg", False)
            if fn: icon = "💰"; bc = "#c8e86a"
        elif stype == "t24":
            ind = f"EMA21:+%{abs(s.get('dist_ema21',0)):.1f}  MA50:%{s.get('dist_ma50',0):.1f}  BB-dar:{s.get('bb_width_ch3',0):.3f}"
        elif stype == "t72":
            ind = f"Mom5:+%{s.get('mom5_pct',0):.1f}  EMA21:%{s.get('dist_ema21',0):.1f}  Drawdown:%{s.get('coin_drawdown',0):.1f}"
        elif stype == "t168":
            ind = f"MA200:+%{s.get('dist_ma200',0):.1f}  Mom10:+%{s.get('mom10_pct',0):.1f}  Zirve:{s.get('days_since_high',0)} bar"
        else:
            ind = ""

        tp_str = f"TP1:{fmt_price(tp1)}"
        if tp2 and tp2 != tp1: tp_str += f" / TP2:{fmt_price(tp2)}"
        sig_rows += (
            f'<div class="sig" style="border-color:{bc}">'
            f'<div class="sr"><b>{icon} {s.get("symbol","")} <small>[{label}]</small></b>'
            f'<span class="ts">{s.get("time","")[:16]}</span></div>'
            f'<div class="sd">💵 {fmt_price(e)}  🛑 {fmt_price(stop)}  🎯 {tp_str}</div>'
            f'<div class="sd">{ind}</div>'
            f'</div>'
        )
    ps = perf_summary()
    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Pump Scanner v6.0</title>
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
h3{{color:#ff4444;margin:0 0 10px;font-size:.78rem;letter-spacing:2px}}
.sig{{background:#0d0305;border-left:3px solid #ff4444;padding:10px 14px;margin:5px 0;border-radius:2px}}
.sr{{display:flex;justify-content:space-between;align-items:center;margin-bottom:5px}}
.sd{{font-size:.73rem;margin:2px 0;color:#8aa8b8}}
.ts{{color:#3d5a6a;font-size:.65rem}}
.footer{{color:#3d5a6a;font-size:.62rem;margin-top:20px;border-top:1px solid #1c2a36;padding-top:10px;line-height:2}}
</style></head><body>
<h1>PUMP SCANNER <small style="font-size:.6rem;color:#3d5a6a">v6.0 — 4 SİSTEM</small></h1>
<div class="info">
  🔴 PANİK PUMP: Düşüş {CRASH_MAX:.0f}% ile {CRASH_MIN:.0f}% | Hacim {VOL_MIN:.1f}x-{VOL_MAX:.1f}x | Stop -3% | TP +5/10/15% | WR ~%84<br>
  ⛔ KISA VADE (T24): DEVRE DIŞI<br>
  🟡 ORTA VADE (T72): Mom pozitif + EMA21 altı + Sağlıklı drawdown + MA200 yukarı | Stop -5% | TP +10% | WR %54<br>
  🟢 UZUN VADE (T168): MA200 üstü + MA50 altı + Mom pozitif + Yakın zirve | Stop -8% | TP +25% | WR %40<br>
  BTC 4H: {btc_4h_cache.get("trend","?")}
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
  Eleme: Cooldown:{stats.get("cooldown",0)} Crash:{stats.get("filtered_crash",0)} Vol:{stats.get("filtered_vol",0)} Hacim:{stats.get("low_liquidity",0)}
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
        stype  = s.get("raw_type", s.get("sig_type", "capit"))
        _, label, tc = _TYPE_LABEL.get(stype, ("🔴", "SİNYAL", "#ff4444"))
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
          <td>{'✅' if s.get('tp2_hit') else '—'}</td>
          <td>{fmt_ret(cr)}</td>
          <td>{ct}</td>
          <td style="color:{st_col}">{st.upper()}</td>
        </tr>"""
    return f"""<!DOCTYPE html><html lang="tr"><head>
<meta charset="UTF-8"><title>Performans — Pump Scanner v6</title>
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
<h1>📈 PUMP SCANNER SİNYAL PERFORMANSI v6.0</h1>
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
  <th>Peak%</th><th>TP1</th><th>TP2</th><th>Kapanış%</th><th>Kapanış Zamanı</th><th>Durum</th>
</tr></thead><tbody>{rows}</tbody></table>
</body></html>"""

# ============================================================
# PERİYODİK GÖREVLER
# ============================================================
async def periodic_tasks():
    tick = 0
    while True:
        await asyncio.sleep(600)
        tick += 1
        t24_cnt  = sum(1 for s in all_signals if s.get("type") == "t24")
        t72_cnt  = sum(1 for s in all_signals if s.get("type") == "t72")
        t168_cnt = sum(1 for s in all_signals if s.get("type") == "t168")
        cap_cnt  = sum(1 for s in all_signals if s.get("type") == "capit")
        print(
            f"\n╔══════════════ PUMP SCANNER ÖZET ══════════════╗\n"
            f"  Sembol: {len(tracked_symbols):<6} 1H Kapanış: {ws_1h_closes:<6} Toplam Sinyal: {stats.get('signal_sent',0)}\n"
            f"  ── Son Sinyaller ──\n"
            f"  PANİK PUMP : {cap_cnt}\n"
            f"  KISA VADE  : {t24_cnt}\n"
            f"  ORTA VADE  : {t72_cnt}\n"
            f"  UZUN VADE  : {t168_cnt}\n"
            f"  ── Filtre ──\n"
            f"  Crash:{stats.get('filtered_crash',0)}  Vol:{stats.get('filtered_vol',0)}"
            f"  Hacim:{stats.get('low_liquidity',0)}  Cooldown:{stats.get('cooldown',0)}\n"
            f"╚═══════════════════════════════════════════════╝",
            flush=True,
        )
        if tick % 2 == 0:
            await refresh_btc_4h()

# ============================================================
# MAIN
# ============================================================
async def main():
    global tracked_symbols
    print("Pump Scanner v6.0 başlatılıyor — 3 Sistem (T24 devre dışı)", flush=True)
    print(f"  [1] PANİK PUMP : Crash {CRASH_MAX:.0f}%-{CRASH_MIN:.0f}% + Hacim {VOL_MIN}x-{VOL_MAX}x | Stop -3% | TP +5/10/15% | WR ~%84", flush=True)
    print(f"  [2] T24        : *** DEVRE DIŞI ***", flush=True)
    print(f"  [3] ORTA VADE  : T72 | Stop -5% | TP +10% | WR %54", flush=True)
    print(f"  [4] UZUN VADE  : T168 | Stop -8% | TP +25% | WR %40", flush=True)

    symbols = await load_symbols_pool()
    if not symbols:
        print("Sembol yüklenemedi", flush=True); return

    tracked_symbols      = list(symbols)
    bot_status["status"] = "BOOTSTRAP"
    print(f"{len(symbols)} sembol yüklendi", flush=True)

    await bootstrap_all(symbols)
    await refresh_funding_cache(symbols)
    await refresh_btc_4h()
    rebuild_pending()
    print(f"Pending sinyaller: {sum(len(v) for v in pending_by_symbol.values())}", flush=True)

    candidate_queue = asyncio.Queue()
    asyncio.create_task(signal_worker(candidate_queue))
    asyncio.create_task(periodic_tasks())

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
