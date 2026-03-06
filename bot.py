# -*- coding: utf-8 -*-
"""
Trend & Momentum Scanner v3.1
1H sinyal + 4H teyit — WebSocket tabanli
Guncellenen puanlama:
- RSI tavan 18 (gercekci)
- Williams %R tavan -98 (gercekci)
- MFI tavan 5 (gercekci)
- MACD histogram yaklasma hizi
- EMA20/SMA50 gap daralma hizi
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

# Gercekci tavan seviyeleri — bu seviyelerde tam puan verilir
RSI_CEIL = 18.0    # RSI 18 altinda → 20/20 puan
WR_CEIL  = -98.0   # W%R -98 altinda → 15/15 puan
MFI_CEIL = 5.0     # MFI 5 altinda  → 15/15 puan

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
    # En yuksek skorlu 5 coin
    if _top_scores:
        top5 = sorted(_top_scores.items(), key=lambda x: x[1]["score"], reverse=True)[:5]
        print("  --- En Yuksek Skorlar ---", flush=True)
        for sym, d in top5:
            print(
                f"  {sym:15s} skor={d['score']:5.1f}  "
                f"dip={d.get('dip',0):4.1f}  trend={d.get('trend',0):4.1f}  "
                f"RSI={d.get('rsi','-')}  WR={d.get('wr','-')}",
                flush=True
            )
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

    # Better RSI — Cycler
    # RSI > 69 → bullish mod (1), RSI < 31 → bearish mod (2)
    # Mod sifirlanir: bullish modda RSI < 39, bearish modda RSI > 61
    cycler = [0] * len(df)
    rsi_vals = df["rsi"].tolist()
    for i in range(1, len(rsi_vals)):
        r = rsi_vals[i]
        prev = cycler[i - 1]
        if r is None or (isinstance(r, float) and np.isnan(r)):
            cycler[i] = prev
            continue
        if r > 69:
            cycler[i] = 1   # bullish mod
        elif r < 31:
            cycler[i] = 2   # bearish mod
        elif prev == 1 and r < 39:
            cycler[i] = 0   # bullish moddan cikis
        elif prev == 2 and r > 61:
            cycler[i] = 0   # bearish moddan cikis
        else:
            cycler[i] = prev
    df["rsi_cycler"] = cycler
    # Bearish moddan cikis ani: bir onceki mum cycler==2, bu mum cycler==0
    df["rsi_cycler_exit"] = (df["rsi_cycler"].shift(1) == 2) & (df["rsi_cycler"] == 0)
    h14 = df["high"].rolling(14)
    l14 = df["low"].rolling(14)
    df["wr"] = -100 * (h14.max() - df["close"]) / (h14.max() - l14.min())
    # MFI — TradingView RJR versiyonu (SMA tabanli, kesme ani sinyali)
    # rawMoneyFlow = hlc3 * volume
    # positiveFlow = hlc3 > hlc3[1] ise rawMoneyFlow, yoksa 0
    # negativeFlow = hlc3 < hlc3[1] ise rawMoneyFlow, yoksa 0
    # ratio = sma(positiveFlow, 14) / sma(negativeFlow, 14)
    # MFI = 100 - 100 / (1 + ratio)
    hlc3          = (df["high"] + df["low"] + df["close"]) / 3
    raw_mf        = hlc3 * df["volume"]
    pos_mf        = raw_mf.where(hlc3 > hlc3.shift(1), 0.0)
    neg_mf        = raw_mf.where(hlc3 < hlc3.shift(1), 0.0)
    pos_sma       = pos_mf.rolling(14, min_periods=14).mean()
    neg_sma       = neg_mf.rolling(14, min_periods=14).mean()
    mf_ratio      = pos_sma / neg_sma.replace(0, np.nan)
    df["mfi"]     = 100 - 100 / (1 + mf_ratio)
    # Kesme ani: bir onceki mum 20 ustunde, simdi 20 altina gecti
    df["mfi_cross_os"] = (df["mfi"].shift(1) > 20) & (df["mfi"] <= 20)
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

    # ── Squeeze Momentum (LazyBear) ──────────────────────────
    sqz_len     = 20
    bb_mult     = 2.0
    kc_mult     = 1.5
    bb_basis    = df["close"].rolling(sqz_len).mean()
    bb_dev      = df["close"].rolling(sqz_len).std()
    bb_upper    = bb_basis + bb_mult * bb_dev
    bb_lower    = bb_basis - bb_mult * bb_dev
    tr          = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift(1)).abs(),
        (df["low"]  - df["close"].shift(1)).abs(),
    ], axis=1).max(axis=1)
    kc_ma       = df["close"].rolling(sqz_len).mean()
    kc_range    = tr.rolling(sqz_len).mean()
    kc_upper    = kc_ma + kc_mult * kc_range
    kc_lower    = kc_ma - kc_mult * kc_range
    df["sqz_on"]  = (bb_lower > kc_lower) & (bb_upper < kc_upper)
    df["sqz_off"] = (bb_lower < kc_lower) & (bb_upper > kc_upper)
    # Momentum: linreg delta
    highest  = df["high"].rolling(sqz_len).max()
    lowest   = df["low"].rolling(sqz_len).min()
    delta    = df["close"] - ((highest + lowest) / 2 + bb_basis) / 2
    val_arr  = delta.values.astype(float)
    sqz_val  = np.full(len(val_arr), np.nan)
    for i in range(sqz_len - 1, len(val_arr)):
        y = val_arr[i - sqz_len + 1: i + 1]
        if np.any(np.isnan(y)):
            continue
        x = np.arange(sqz_len, dtype=float)
        p = np.polyfit(x, y, 1)
        sqz_val[i] = p[0] * (sqz_len - 1) + p[1]
    df["sqz_val"] = sqz_val

    return df

# ============================================================
# 5) PUAN FONKSİYONLARI
# ============================================================

def score_rsi(rsi) -> float:
    """
    RSI_CEIL (18) ve altinda → 20 tam puan
    RSI_THRESH (35) ve uzerinde → 0 puan
    Arada lineer.
    """
    if rsi is None or rsi >= RSI_THRESH:
        return 0.0
    if rsi <= RSI_CEIL:
        return 20.0
    return round((RSI_THRESH - rsi) / (RSI_THRESH - RSI_CEIL) * 20.0, 2)

def score_wr(wr) -> float:
    """
    WR_CEIL (-98) ve altinda → 15 tam puan
    WR_THRESH (-80) ve uzerinde → 0 puan
    """
    if wr is None or wr >= WR_THRESH:
        return 0.0
    if wr <= WR_CEIL:
        return 15.0
    return round((WR_THRESH - wr) / (WR_THRESH - WR_CEIL) * 15.0, 2)

def score_mfi(mfi, cross=False) -> float:
    """
    TV RJR MFI mantigi:
    - Kesme ani (az once 20'yi asagi kesti): 15 tam puan
    - Zaten 20 altinda (kesme degil): deger derinligine gore 0-12p
    - MFI_THRESH (20) ve uzerinde: 0 puan

    Kesme anina bonus verilir cunku TV'deki sinyal mantigi budur.
    """
    if mfi is None or mfi >= MFI_THRESH:
        return 0.0
    if cross:
        return 15.0   # tam puan — tam kesme ani
    if mfi <= MFI_CEIL:
        return 12.0   # cok derin ama kesme ani degil — biraz dusuk
    return round((MFI_THRESH - mfi) / (MFI_THRESH - MFI_CEIL) * 12.0, 2)

def score_macd(hist_series) -> tuple:
    """
    Son 6 mumun histogram serisini alir, yaklasma hizini degerlendirir.
    Dondurur: (puan float, durum_metni str, tetiklendi bool)

    Mantik:
    - Sifiri yukari gecti                           → 10p
    - Hala negatif, donuyor + hizli yaklasma (%60+) → 8p
    - Hala negatif, donuyor + orta yaklasma (%30+)  → 6p
    - Hala negatif, donuyor + yavas yaklasma         → 4p
    - Henuz donmemis ama seri %50+ daraldi           → 4p
    - Henuz donmemis ama seri %20+ daraldi           → 2p
    - Aciliyor veya sabit                            → 0p
    """
    if hist_series is None or len(hist_series) < 3:
        return 0.0, "veri yok", False

    vals = [v for v in hist_series[-6:] if v is not None and not np.isnan(v)]
    if len(vals) < 3:
        return 0.0, "veri yok", False

    current  = vals[-1]
    previous = vals[-2]
    oldest   = vals[0]

    # Sifiri yukari gecti
    if current > 0 and previous <= 0:
        return 10.0, "sifiri yukari gecti ↑", True

    # Hala negatif bolgede
    if current < 0:
        turning = current > previous  # son deger oncekinden buyuk = donuyor

        if turning and oldest < 0:
            shrink = (oldest - current) / abs(oldest)  # ne kadar kuculdu (0-1 arasi)
            shrink = max(0.0, min(1.0, shrink))
            if shrink >= 0.6:
                return 8.0, f"donuyor ↑ (hizli %{shrink*100:.0f})", True
            elif shrink >= 0.3:
                return 6.0, f"donuyor ↑ (orta %{shrink*100:.0f})", True
            else:
                return 4.0, f"donuyor ↑ (yavas %{shrink*100:.0f})", True

        # Donmuyor ama mutlak deger daralıyor mu?
        if oldest < 0 and current < 0:
            shrink = (abs(oldest) - abs(current)) / abs(oldest)
            if shrink >= 0.5:
                return 4.0, f"daralıyor (%{shrink*100:.0f})", False
            elif shrink >= 0.2:
                return 2.0, f"daralıyor (%{shrink*100:.0f})", False

    return 0.0, "aciliyor veya sabit", False

def score_ema_sma(df4h) -> tuple:
    """
    EMA20 / SMA50 arasindaki gap'in son 5 mumda nasil degistigini degerlendirir.
    Dondurur: (puan float, durum_metni str, tetiklendi bool)

    Mantik:
    - Son 5 mumda kesisim oldu          → 10p
    - EMA > SMA, mesafeye gore          → 2-7p
    - EMA < SMA ama gap %50+ daraldi    → 6p
    - EMA < SMA ama gap %30+ daraldi    → 4p
    - EMA < SMA ama gap %10+ daraldi    → 2p
    - Gap aciliyor                      → 0p
    """
    if df4h is None or len(df4h) < 10:
        return 0.0, "veri yok", False

    def sf(row, col):
        v = row.get(col, np.nan)
        return None if pd.isna(v) else float(v)

    last  = df4h.iloc[-2]
    prev5 = df4h.iloc[-7:-2]

    ema_now = sf(last, "ema20")
    sma_now = sf(last, "sma50")

    if ema_now is None or sma_now is None:
        return 0.0, "veri yok", False

    # Son 5 mumda asagidan yukari kesisim oldu mu?
    recent_cross = False
    for i in range(len(prev5) - 1):
        r0 = prev5.iloc[i];   r1 = prev5.iloc[i + 1]
        e0 = sf(r0, "ema20"); s0 = sf(r0, "sma50")
        e1 = sf(r1, "ema20"); s1 = sf(r1, "sma50")
        if None not in (e0, s0, e1, s1) and e0 <= s0 and e1 > s1:
            recent_cross = True
            break

    if recent_cross:
        return 10.0, "kesisim oldu (yakin) ↑", True

    if ema_now > sma_now:
        gap_pct = (ema_now - sma_now) / sma_now * 100
        p = min(7.0, gap_pct * 2.0)
        return round(p, 2), f"EMA20 > SMA50 (+%{gap_pct:.1f})", True

    # EMA hala altinda — gap daralıyor mu?
    if len(prev5) >= 3:
        oldest = prev5.iloc[0]
        e_old  = sf(oldest, "ema20")
        s_old  = sf(oldest, "sma50")

        if e_old is not None and s_old is not None and e_old < s_old:
            gap_old = s_old - e_old   # eskiden ne kadar altindaydi
            gap_now = sma_now - ema_now  # simdi ne kadar altinda

            if gap_old > 0:
                shrink = (gap_old - gap_now) / gap_old

                if shrink >= 0.5:
                    return 6.0, f"EMA<SMA gap %{shrink*100:.0f} daraldi ↑", False
                elif shrink >= 0.3:
                    return 4.0, f"EMA<SMA gap %{shrink*100:.0f} daraldi ↑", False
                elif shrink >= 0.1:
                    return 2.0, f"EMA<SMA gap %{shrink*100:.0f} daraldi", False
                elif shrink < 0:
                    return 0.0, "EMA<SMA gap aciliyor ↓", False

    return 0.0, "EMA20 < SMA50", False

# ============================================================
# 6) 1H ANALİZ — max 70 puan
# ============================================================
def analyze_1h(df):
    if len(df) < 60:
        return None

    last = df.iloc[-2]   # son kapanan mum
    curr = df.iloc[-1]   # suanki mum (entry fiyati icin)

    def sf(row, col):
        v = row.get(col, np.nan)
        return None if pd.isna(v) else float(v)

    rsi      = sf(last, "rsi")
    wr       = sf(last, "wr")
    mfi           = sf(last, "mfi")
    mfi_cross_os  = bool(last.get("mfi_cross_os", False))   # TV kesme ani
    bb_lower      = sf(last, "bb_lower")
    close_v  = sf(last, "close")
    low_v    = sf(last, "low")
    vol      = sf(last, "volume")
    vol_ma   = sf(last, "vol_ma")
    atr      = sf(last, "atr")
    entry    = sf(curr, "close")

    if None in (rsi, wr, mfi, entry):
        return None

    # RSI — max 20p + cycler cikis bonusu max 5p
    p_rsi = score_rsi(rsi)
    c_rsi = rsi < RSI_THRESH
    # Cycler: bearish moddan tam cikis ani → bonus
    cycler_exit = bool(last.get("rsi_cycler_exit", False))
    cycler_val  = int(last.get("rsi_cycler", 0))
    if cycler_exit:
        p_rsi = min(25.0, p_rsi + 5.0)   # tam cikis ani: +5p bonus

    # Williams %R — max 15p
    p_wr = score_wr(wr)
    c_wr = wr < WR_THRESH

    # MFI — TV mantigi: kesme aninda tam puan, sadece altinda ise daha az puan
    # Kesme ani (az once 20'yi asagi kesti) = 15 tam puan
    # Zaten 20 altinda ama bu mumda kesmedi = deger derinligine gore puan
    p_mfi = score_mfi(mfi, cross=mfi_cross_os)
    c_mfi = mfi is not None and mfi < MFI_THRESH

    # MACD histogram yaklasma hizi — max 10p
    hist_series = df["macd_hist"].iloc[-7:-1].tolist()
    p_macd, macd_txt, c_macd = score_macd(hist_series)
    mhist = sf(last, "macd_hist")

    # BB alt banttan geri donus — max 5p
    c_bb = False
    p_bb = 0.0
    if bb_lower is not None and close_v is not None and low_v is not None:
        c_bb = (low_v <= bb_lower * 1.002) and (close_v > bb_lower)
        p_bb = 5.0 if c_bb else 0.0

    # Hacim — alici/satici orani (Elder-Ray tarzı) — max 5p
    c_vol       = False
    p_vol       = 0.0
    vol_ratio   = 0.0
    bvol_pct    = 0.0   # alici hacim yuzdesi
    h_v = sf(last, "high"); l_v = sf(last, "low")
    if (vol is not None and vol_ma is not None and vol_ma > 0
            and h_v is not None and l_v is not None
            and close_v is not None and (h_v - l_v) > 0):
        vol_ratio  = vol / vol_ma
        bvol       = vol * (close_v - l_v) / (h_v - l_v)   # alici hacim
        svol       = vol * (h_v - close_v) / (h_v - l_v)   # satici hacim
        bvol_pct   = bvol / (bvol + svol) * 100 if (bvol + svol) > 0 else 50.0
        # Alici agirlikli VE toplam hacim ortalamanin uzerinde
        c_vol = bvol_pct >= 55.0 and vol_ratio >= 1.0
        if bvol_pct >= 65.0 and vol_ratio >= 1.2:
            p_vol = 5.0
        elif bvol_pct >= 60.0 and vol_ratio >= 1.0:
            p_vol = 3.5
        elif bvol_pct >= 55.0:
            p_vol = 2.0
        else:
            p_vol = 0.0

    return {
        "conditions": {
            "rsi":  c_rsi,
            "wr":   c_wr,
            "mfi":  c_mfi,
            "macd": c_macd,
            "bb":   c_bb,
            "vol":  c_vol,
        },
        "score_1h":  round(p_rsi + p_wr + p_mfi + p_macd + p_bb + p_vol, 1),
        "p_rsi":     round(p_rsi,  2),
        "p_wr":      round(p_wr,   2),
        "p_mfi":     round(p_mfi,  2),
        "p_macd":    round(p_macd, 2),
        "p_bb":      p_bb,
        "p_vol":     round(p_vol,  2),
        "rsi":          round(rsi, 2),
        "rsi_cycler":   cycler_val,
        "rsi_cycler_exit": cycler_exit,
        "wr":        round(wr,  2),
        "mfi":       round(mfi, 2),
        "mfi_cross": mfi_cross_os,
        "macd_hist": round(mhist, 8) if mhist is not None else None,
        "macd_txt":  macd_txt,
        "bb_lower":  round(bb_lower, 8) if bb_lower is not None else None,
        "vol_ratio": round(vol_ratio, 2),
        "bvol_pct":  round(bvol_pct, 1),
        "atr":       round(atr, 8) if atr is not None else 0.0,
        "entry":     round(entry, 8),
    }

# ============================================================
# 7) 4H TEYİT — max 30 puan
# ============================================================
def analyze_4h(df):
    if df is None or len(df) < 55:
        return None

    last  = df.iloc[-2]
    prev5 = df.iloc[-7:-2]

    def sf(row, col):
        v = row.get(col, np.nan)
        return None if pd.isna(v) else float(v)

    adx    = sf(last, "adx")
    obv    = sf(last, "obv")
    obv_ma = sf(last, "obv_ma")

    if adx is None:
        return None

    # EMA20 / SMA50 — gap daralma sistemi — max 10p
    p_ema, ema_txt, c_ema = score_ema_sma(df)

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
        # Pozitif diverjans: fiyat dusmus ama OBV artmis
        if not c_obv and len(prev5) >= 3:
            try:
                price_ch = float(prev5["close"].iloc[-1]) - float(prev5["close"].iloc[0])
                obv_ch   = float(prev5["obv"].iloc[-1])   - float(prev5["obv"].iloc[0])
                if price_ch < 0 and obv_ch > 0:
                    c_obv      = True
                    p_obv      = 8.0
                    obv_signal = "pozitif diverjans"
            except Exception:
                pass

    ema20 = sf(last, "ema20")
    sma50 = sf(last, "sma50")

    # Squeeze Momentum — max 4p bonus (OBV ile birlikte max 10p kalir)
    sqz_on  = bool(last.get("sqz_on",  False))
    sqz_off = bool(last.get("sqz_off", False))
    sqz_val_now  = sf(last, "sqz_val")
    sqz_val_prev = sf(df.iloc[-3], "sqz_val") if len(df) >= 3 else None
    p_sqz  = 0.0
    sqz_txt = "veri yok"
    if sqz_val_now is not None and sqz_val_prev is not None:
        momentum_up = sqz_val_now > sqz_val_prev and sqz_val_now > 0
        if sqz_off and momentum_up:
            p_sqz  = 4.0   # squeeze bitti + momentum yukari
            sqz_txt = "squeeze bitti + momentum yukari ↑"
        elif momentum_up:
            p_sqz  = 2.0
            sqz_txt = "momentum yukari ↑"
        elif sqz_on:
            p_sqz  = 0.0
            sqz_txt = "squeeze devam ediyor"
        else:
            sqz_txt = "momentum asagi"
    # OBV max 8p, sqz max 4p — toplam 4H max 32p (genel max 102 olabilir, min(100) ile kesilir)
    p_obv = min(p_obv, 8.0)

    return {
        "conditions": {
            "ema_cross": c_ema,
            "adx":       c_adx,
            "obv":       c_obv,
        },
        "score_4h":   round(p_ema + p_adx + p_obv + p_sqz, 1),
        "p_ema":      round(p_ema, 2),
        "p_adx":      round(p_adx, 2),
        "p_obv":      p_obv,
        "ema20":      round(ema20, 4) if ema20 is not None else None,
        "sma50":      round(sma50, 4) if sma50 is not None else None,
        "ema_txt":    ema_txt,
        "adx":        round(adx, 2),
        "obv_signal": obv_signal,
        "p_sqz":      round(p_sqz, 2),
        "sqz_txt":    sqz_txt,
        "sqz_on":     sqz_on,
        "sqz_off":    sqz_off,
    }


# ============================================================
# 8) UNIFIED ANALİZ SİSTEMİ — Tek sistem, iki mod
# ============================================================
# MOD TESPİTİ: 200 bar geçmişe bakarak swing low/high tespit
# DİP DÖNÜŞÜ: Son dipten +%3~+%20 yükseliş, dip yakın (48H içinde)
# TREND DEVAMI: Son dipten +%20~+%50, henüz yorulmamış
# YORGUN: Son dipten +%50 üzeri VEYA son tepesinin -%3 içinde → sinyal yok
# YATAY: Dip-tepe farkı <%5 → sinyal yok

def detect_market_mode(df):
    """
    200 bar geçmişe bakarak piyasanın nerede olduğunu tespit et.
    Döndürür: ("dip_donus" | "trend_devam" | "yorgun" | "yatay", dict)
    """
    if len(df) < 50:
        return "yatay", {}

    closes = df["close"].values
    highs  = df["high"].values
    lows   = df["low"].values
    curr_price = float(closes[-2])  # son kapanan mum

    # Son 200 bar içinde swing low ve swing high bul
    lookback = min(200, len(df)-2)
    window_closes = closes[-(lookback+1):-1]
    window_highs  = highs[-(lookback+1):-1]
    window_lows   = lows[-(lookback+1):-1]

    # Swing low: son lookback bar içindeki minimum
    swing_low_idx  = int(np.argmin(window_lows))
    swing_low      = float(window_lows[swing_low_idx])
    swing_low_bars_ago = lookback - swing_low_idx  # kaç bar önce

    # Swing high: swing_low'dan SONRA oluşan maksimum
    post_low_highs = window_highs[swing_low_idx:]
    swing_high_idx = swing_low_idx + int(np.argmax(post_low_highs))
    swing_high     = float(window_highs[swing_high_idx])
    swing_high_bars_ago = lookback - swing_high_idx

    # Dipten şu ana kadar yükseliş
    rise_from_low  = (curr_price - swing_low)  / swing_low  * 100 if swing_low > 0 else 0
    # Tepeden şu ana kadar düşüş
    drop_from_high = (swing_high - curr_price) / swing_high * 100 if swing_high > 0 else 0
    # Dip-tepe toplam hareket
    total_move     = (swing_high - swing_low)  / swing_low  * 100 if swing_low > 0 else 0

    info = {
        "swing_low":          round(swing_low, 8),
        "swing_low_bars_ago": swing_low_bars_ago,
        "swing_high":         round(swing_high, 8),
        "swing_high_bars_ago": swing_high_bars_ago,
        "rise_from_low":      round(rise_from_low, 1),
        "drop_from_high":     round(drop_from_high, 1),
        "total_move":         round(total_move, 1),
        "curr_price":         round(curr_price, 8),
    }

    # Mod kararı
    # Yatay: toplam hareket çok küçük
    if total_move < 5.0:
        return "yatay", info

    # Yorgun: tepeye çok yakın (tepeden -%3 içinde) VEYA dipten +%60 üzeri çıkmış
    if drop_from_high <= 3.0 or rise_from_low >= 60.0:
        return "yorgun", info

    # Dip dönüşü: dip yakın (son 48 bar = 48H içinde) VE dipten az yükseliş
    if swing_low_bars_ago <= 48 and rise_from_low <= 25.0:
        return "dip_donus", info

    # Trend devamı: dipten %15-50 yükselmiş, tepeye hala uzak
    if 15.0 <= rise_from_low <= 50.0 and drop_from_high >= 5.0:
        return "trend_devam", info

    # Dip yakın değil ama henüz az yükselmiş → trend devamı dene
    if rise_from_low <= 40.0 and drop_from_high >= 8.0:
        return "trend_devam", info

    return "yatay", info


def analyze_unified_1h(df, mode):
    """
    Moda göre 1H indikatör analizi.
    DİP DÖNÜŞÜ: RSI<40, Williams<-70, MFI<30, MACD dönüyor, hacim artıyor
    TREND DEVAMI: RSI 45-62, Williams -30/-65, MACD pozitif+artıyor, sağlıklı çekilme
    """
    if len(df) < 60:
        return None

    last = df.iloc[-2]
    curr = df.iloc[-1]

    def sf(row, col):
        v = row.get(col, np.nan)
        return None if pd.isna(v) else float(v)

    rsi           = sf(last, "rsi")
    wr            = sf(last, "wr")
    mfi           = sf(last, "mfi")
    mfi_cross_os  = bool(last.get("mfi_cross_os", False))
    bb_lower      = sf(last, "bb_lower")
    bb_mid        = sf(last, "bb_mid")
    bb_upper      = sf(last, "bb_upper")
    close_v       = sf(last, "close")
    low_v         = sf(last, "low")
    high_v        = sf(last, "high")
    vol           = sf(last, "volume")
    vol_ma        = sf(last, "vol_ma")
    atr           = sf(last, "atr")
    entry         = sf(curr, "close")

    if None in (rsi, wr, entry):
        return None

    # Hacim hesabı (her iki mod için ortak)
    vol_ratio = 0.0
    bvol_pct  = 50.0
    p_vol     = 0.0
    if (vol and vol_ma and vol_ma > 0 and high_v and low_v
            and close_v and (high_v - low_v) > 0):
        vol_ratio = vol / vol_ma
        bvol      = vol * (close_v - low_v) / (high_v - low_v)
        svol      = vol * (high_v - close_v) / (high_v - low_v)
        bvol_pct  = bvol / (bvol + svol) * 100 if (bvol + svol) > 0 else 50.0

    # MACD — son 6 mumun histogramı
    hist_vals = []
    for i in range(-7, -1):
        v = sf(df.iloc[i], "macd_hist")
        if v is not None:
            hist_vals.append(v)

    cycler_exit = bool(last.get("rsi_cycler_exit", False))
    cycler_val  = int(last.get("rsi_cycler", 0))

    if mode == "dip_donus":
        # ── HARD FİLTRELER ──────────────────────────────────
        if rsi >= 45:           return None  # RSI zaten yüksek
        if wr  >= -50:          return None  # Williams aşırı alımda
        if mfi is not None and mfi >= 40:
                                return None  # MFI aşırı alımda

        # RSI — max 25p (cycler çıkış bonusu dahil)
        if rsi <= RSI_CEIL:
            p_rsi = 20.0
        elif rsi < RSI_THRESH:
            p_rsi = max(0, (RSI_THRESH - rsi) / (RSI_THRESH - RSI_CEIL) * 20.0)
        else:
            p_rsi = 0.0
        if cycler_exit:
            p_rsi = min(25.0, p_rsi + 5.0)

        # Williams — max 15p
        if wr <= WR_CEIL:
            p_wr = 15.0
        elif wr < WR_THRESH:
            p_wr = max(0, (WR_THRESH - wr) / (WR_THRESH - WR_CEIL) * 15.0)
        else:
            p_wr = 0.0

        # MFI — max 15p
        p_mfi = 0.0
        if mfi is not None:
            if mfi_cross_os:
                p_mfi = 15.0
            elif mfi <= MFI_CEIL:
                p_mfi = 15.0
            elif mfi < MFI_THRESH:
                p_mfi = max(0, (MFI_THRESH - mfi) / (MFI_THRESH - MFI_CEIL) * 15.0)

        # MACD — negatiften dönüyor mu? max 10p
        p_macd = 0.0
        macd_txt = "yetersiz veri"
        if len(hist_vals) >= 3:
            h_last = hist_vals[-1]
            h_prev = hist_vals[-2]
            h_old  = hist_vals[0]
            if h_last > 0 and h_prev <= 0:
                p_macd   = 10.0
                macd_txt = "negatiften pozitife geçti ↑"
            elif h_last < 0 and h_last > h_prev:
                shrink = abs(h_last - h_old) / abs(h_old) if h_old != 0 else 0
                p_macd   = min(10.0, max(0.0, shrink * 10.0))
                macd_txt = f"daralıyor ↑ (%{round(shrink*100)})"
            elif h_last < 0 and h_last < h_prev:
                p_macd   = 0.0
                macd_txt = "devam ediyor ↓"
            else:
                macd_txt = "sabit"

        # BB alt bant dönüşü — max 5p
        p_bb = 0.0
        bb_txt = ""
        if bb_lower and close_v and low_v:
            if low_v <= bb_lower * 1.002 and close_v > bb_lower:
                p_bb   = 5.0
                bb_txt = "alt bantta dönüş"

        # Hacim — alıcı ağırlıklı, max 5p
        if bvol_pct >= 65.0 and vol_ratio >= 1.2:
            p_vol = 5.0
        elif bvol_pct >= 60.0 and vol_ratio >= 1.0:
            p_vol = 3.5
        elif bvol_pct >= 55.0:
            p_vol = 2.0

        score_1h = round(p_rsi + p_wr + p_mfi + p_macd + p_bb + p_vol, 1)
        return {
            "mode":        "dip_donus",
            "score_1h":    score_1h,
            "p_rsi":       round(p_rsi, 2),
            "p_wr":        round(p_wr, 2),
            "p_mfi":       round(p_mfi, 2),
            "p_macd":      round(p_macd, 2),
            "p_bb":        p_bb,
            "p_vol":       round(p_vol, 2),
            "rsi":         round(rsi, 2),
            "wr":          round(wr, 2),
            "mfi":         round(mfi, 2) if mfi else None,
            "mfi_cross":   mfi_cross_os,
            "macd_txt":    macd_txt,
            "bb_txt":      bb_txt,
            "vol_ratio":   round(vol_ratio, 2),
            "bvol_pct":    round(bvol_pct, 1),
            "atr":         round(atr, 8) if atr else 0.0,
            "entry":       round(entry, 8),
            "cycler_exit": cycler_exit,
            "cycler_val":  cycler_val,
        }

    elif mode == "trend_devam":
        # ── HARD FİLTRELER ──────────────────────────────────
        if rsi > 62:            return None  # RSI yorgun
        if rsi < 42:            return None  # RSI çok düşük, trend değil
        if wr > -20:            return None  # Williams aşırı alım — HARD BLOK
        if mfi is not None and mfi > 65:
                                return None  # MFI aşırı alım — HARD BLOK

        # BB üst banda çok yakınsa sinyal verme
        if bb_upper and bb_mid and close_v:
            bb_range = bb_upper - bb_mid
            dist_to_upper = bb_upper - close_v
            if bb_range > 0 and dist_to_upper / bb_range < 0.2:
                return None  # BB üst bandının %20'si içinde → aşırı uzamış

        # RSI 45-62 ideal — max 20p
        if 45 <= rsi <= 55:
            p_rsi = 20.0
        elif 55 < rsi <= 62:
            p_rsi = max(0.0, 20.0 - (rsi - 55) / 7.0 * 15.0)
        elif 42 <= rsi < 45:
            p_rsi = max(0.0, (rsi - 42) / 3.0 * 10.0)
        else:
            p_rsi = 0.0

        # Williams -30 ile -65 arası ideal — max 10p
        # -20 üzeri hard blok zaten, -65 altı da zayıf trend demek
        if -65 <= wr <= -30:
            p_wr = 10.0
        elif -80 <= wr < -65:
            p_wr = max(0.0, (wr - (-80)) / 15.0 * 6.0)
        else:
            p_wr = 0.0

        # MACD pozitif VE artıyor — max 20p
        p_macd = 0.0
        macd_txt = "yetersiz veri"
        if len(hist_vals) >= 4:
            h_last = hist_vals[-1]
            h_prev = hist_vals[-2]
            h_2ago = hist_vals[-3]
            h_old  = hist_vals[0]
            if h_last > 0:
                # Son 3 mumda sürekli artıyor mu?
                if h_last > h_prev > h_2ago:
                    growth = (h_last - h_old) / abs(h_old) if h_old != 0 else 0
                    if growth >= 0.5:
                        p_macd   = 20.0
                        macd_txt = "güçlü momentum ↑"
                    elif growth >= 0.2:
                        p_macd   = 14.0
                        macd_txt = "momentum artıyor ↑"
                    else:
                        p_macd   = 8.0
                        macd_txt = "pozitif ↑"
                elif h_last > h_prev:
                    p_macd   = 6.0
                    macd_txt = "pozitif, yavaş artıyor"
                elif h_last < h_prev:
                    # MACD pozitif ama azalıyor → zayıf
                    p_macd   = 2.0
                    macd_txt = "pozitif ama azalıyor ↓"
                    # Eğer 2+ mum azalıyorsa sinyal verme
                    if h_prev < h_2ago:
                        return None
            else:
                macd_txt = "negatif"
                return None  # Trend takipte MACD negatifse sinyal yok

        # Sağlıklı geri çekilme var mı? (son 1-3H hafif düşüş)
        # Yoksa "tepeye vurmuş" olabilir
        recent_closes = [sf(df.iloc[i], "close") for i in range(-4, -1)]
        recent_closes = [x for x in recent_closes if x is not None]
        p_pullback = 0.0
        pullback_txt = ""
        if len(recent_closes) >= 3:
            # Son 3 mumda hafif geri çekilme var mı?
            max_recent = max(recent_closes[:-1])
            curr_close = recent_closes[-1]
            if max_recent > 0:
                pullback = (max_recent - curr_close) / max_recent * 100
                if 0.5 <= pullback <= 4.0:
                    p_pullback   = 5.0
                    pullback_txt = f"sağlıklı çekilme -%{round(pullback,1)}"
                elif pullback > 4.0:
                    pullback_txt = f"derin çekilme -%{round(pullback,1)}"
                else:
                    pullback_txt = "çekilme yok"

        # Hacim — trend devamında ortalama üzeri yeterli — max 15p
        if vol_ratio >= 2.0 and bvol_pct >= 60.0:
            p_vol = 15.0
        elif vol_ratio >= 1.5 and bvol_pct >= 55.0:
            p_vol = 10.0
        elif vol_ratio >= 1.2:
            p_vol = 6.0
        elif vol_ratio >= 1.0:
            p_vol = 3.0
        else:
            p_vol = 0.0

        # BB orta bandın üstünde ama üst banda uzak — max 5p
        p_bb = 0.0
        bb_txt = ""
        if bb_upper and bb_mid and close_v:
            bb_range = bb_upper - bb_mid
            if bb_range > 0 and close_v > bb_mid:
                dist_pct = (close_v - bb_mid) / bb_range  # 0=orta, 1=üst
                if dist_pct <= 0.5:
                    p_bb   = 5.0
                    bb_txt = "orta bant üstü, uzak"
                elif dist_pct <= 0.75:
                    p_bb   = 2.0
                    bb_txt = "orta bant üstü"
                else:
                    p_bb   = 0.0
                    bb_txt = "üst banda yakın"

        score_1h = round(p_rsi + p_wr + p_macd + p_pullback + p_vol + p_bb, 1)
        return {
            "mode":         "trend_devam",
            "score_1h":     score_1h,
            "p_rsi":        round(p_rsi, 2),
            "p_wr":         round(p_wr, 2),
            "p_mfi":        0.0,
            "p_macd":       round(p_macd, 2),
            "p_bb":         p_bb,
            "p_vol":        round(p_vol, 2),
            "p_pullback":   p_pullback,
            "rsi":          round(rsi, 2),
            "wr":           round(wr, 2),
            "mfi":          round(mfi, 2) if mfi else None,
            "macd_txt":     macd_txt,
            "bb_txt":       bb_txt,
            "pullback_txt": pullback_txt,
            "vol_ratio":    round(vol_ratio, 2),
            "bvol_pct":     round(bvol_pct, 1),
            "atr":          round(atr, 8) if atr else 0.0,
            "entry":        round(entry, 8),
        }

    return None


def analyze_4h_unified(df, mode):
    """
    4H teyit analizi — her iki mod için ortak ama ağırlıklar farklı.
    """
    if df is None or len(df) < 55:
        return None

    last  = df.iloc[-2]
    prev3 = df.iloc[-5:-2]

    def sf(row, col):
        v = row.get(col, np.nan)
        return None if pd.isna(v) else float(v)

    adx    = sf(last, "adx")
    obv    = sf(last, "obv")
    obv_ma = sf(last, "obv_ma")
    ema20  = sf(last, "ema20")
    sma50  = sf(last, "sma50")

    if adx is None:
        return None

    # ADX filtresi — dip dönüşünde gerekmez ama trend devamında zorunlu
    if mode == "trend_devam" and adx < 20:
        return None

    # EMA/SMA analizi
    p_ema = 0.0
    ema_txt = "veri yok"
    if ema20 and sma50:
        gap_pct = (ema20 - sma50) / sma50 * 100

        if mode == "trend_devam":
            # EMA20 > SMA50 ve gap açılıyor mu?
            # Başarısız kesişim kontrolü: önceki barda aynı ilişki korunuyor mu?
            prev_ema = sf(df.iloc[-3], "ema20")
            prev_sma = sf(df.iloc[-3], "sma50")
            if prev_ema and prev_sma:
                prev_gap = (prev_ema - prev_sma) / prev_sma * 100
                gap_change = gap_pct - prev_gap

                if ema20 > sma50 and gap_change > 0:
                    p_ema   = min(15.0, gap_change * 3.0 + 5.0)
                    ema_txt = f"EMA > SMA, gap açılıyor (+{round(gap_change,2)}%)"
                elif ema20 > sma50 and gap_change <= 0:
                    # Gap kapanıyor — başarısız trend işareti
                    p_ema   = 2.0
                    ema_txt = f"EMA > SMA ama gap kapanıyor ({round(gap_change,2)}%)"
                elif ema20 <= sma50:
                    p_ema   = 0.0
                    ema_txt = "EMA < SMA, trend yok"
            else:
                p_ema   = 5.0 if ema20 > sma50 else 0.0
                ema_txt = "EMA > SMA" if ema20 > sma50 else "EMA < SMA"

        elif mode == "dip_donus":
            # Dip dönüşünde EMA/SMA altında olması normaldir
            # EMA yukarı dönüyor mu? (son 3 barda eğim)
            emas = [sf(df.iloc[i], "ema20") for i in range(-4, -1)]
            emas = [x for x in emas if x]
            if len(emas) >= 3 and emas[-1] > emas[-2]:
                p_ema   = 8.0
                ema_txt = "EMA yukarı dönüyor ↑"
            elif len(emas) >= 2 and emas[-1] == emas[-2]:
                p_ema   = 3.0
                ema_txt = "EMA düzleşiyor"
            else:
                p_ema   = 0.0
                ema_txt = "EMA hala aşağı"

    # ADX — max 10p
    c_adx = adx >= ADX_THRESH
    if mode == "trend_devam":
        p_adx = max(0.0, min(10.0, (adx - ADX_THRESH) / 10.0 * 10.0)) if c_adx else 0.0
    else:
        # Dip dönüşünde ADX düşük olabilir, tam puan verme
        p_adx = max(0.0, min(5.0, adx / 30.0 * 5.0))

    # OBV — yön + ivme kontrolü — max 5p
    p_obv     = 0.0
    obv_txt   = "veri yok"
    if obv and obv_ma:
        # OBV ortalamanın üstünde mi?
        obv_above = obv > obv_ma
        # Son 3 barda OBV artıyor mu?
        obv_vals = [sf(df.iloc[i], "obv") for i in range(-4, -1)]
        obv_vals = [x for x in obv_vals if x]
        obv_rising = len(obv_vals) >= 2 and obv_vals[-1] > obv_vals[-2]
        obv_accel  = len(obv_vals) >= 3 and obv_vals[-1] > obv_vals[-2] > obv_vals[-3]

        if obv_above and obv_accel:
            p_obv   = 5.0
            obv_txt = "artıyor + ivmeleniyor ↑↑"
        elif obv_above and obv_rising:
            p_obv   = 3.5
            obv_txt = "artıyor ↑"
        elif obv_above:
            p_obv   = 2.0
            obv_txt = "ortalama üstü"
        elif obv_rising and not obv_above:
            # Pozitif diverjans: fiyat düşük ama OBV artıyor
            p_obv   = 3.0
            obv_txt = "pozitif diverjans ↑"
        else:
            p_obv   = 0.0
            obv_txt = "zayıf"

    # Squeeze momentum (bonus) — max 3p
    sqz_on       = bool(last.get("sqz_on", False))
    sqz_off      = bool(last.get("sqz_off", False))
    sqz_val_now  = sf(last, "sqz_val")
    sqz_val_prev = sf(df.iloc[-3], "sqz_val") if len(df) >= 3 else None
    p_sqz = 0.0
    sqz_txt = ""
    if sqz_val_now and sqz_val_prev:
        if sqz_off and sqz_val_now > sqz_val_prev and sqz_val_now > 0:
            p_sqz   = 3.0
            sqz_txt = "squeeze bitti + momentum ↑"
        elif sqz_val_now > sqz_val_prev and sqz_val_now > 0:
            p_sqz   = 1.5
            sqz_txt = "momentum ↑"

    score_4h = round(p_ema + p_adx + p_obv + p_sqz, 1)
    return {
        "score_4h":   score_4h,
        "p_ema":      round(p_ema, 2),
        "p_adx":      round(p_adx, 2),
        "p_obv":      p_obv,
        "p_sqz":      round(p_sqz, 2),
        "ema20":      round(ema20, 4) if ema20 else None,
        "sma50":      round(sma50, 4) if sma50 else None,
        "ema_txt":    ema_txt,
        "adx":        round(adx, 2),
        "obv_txt":    obv_txt,
        "sqz_txt":    sqz_txt,
        "sqz_on":     sqz_on,
        "sqz_off":    sqz_off,
    }


# ── Debug: en yüksek skorları takip et ──────────────────────
_top_scores: dict = {}

def full_analyze(symbol, df_1h, df_4h):
    """Ana analiz fonksiyonu — mod tespiti + 1H + 4H."""

    # 1) Mod tespiti
    mode, mode_info = detect_market_mode(df_1h)

    if mode in ("yorgun", "yatay"):
        if _top_scores.get(symbol, {}).get("score", 0) > 30:
            print(f"  [DEBUG] {symbol} | mod={mode} | rise={mode_info.get('rise_from_low')}% drop={mode_info.get('drop_from_high')}%", flush=True)
        return None

    # 2) 1H analiz
    r1h = analyze_unified_1h(df_1h, mode)
    if r1h is None:
        # 1H hard filtreden döndü — eski skor varsa logla
        prev_score = _top_scores.get(symbol, {}).get("score", 0)
        if prev_score > 30:
            last = df_1h.iloc[-2]
            rsi_v = float(last.get("rsi", 0) or 0)
            wr_v  = float(last.get("wr", 0) or 0)
            mfi_v = last.get("mfi")
            mfi_v = float(mfi_v) if mfi_v and str(mfi_v) != "nan" else None
            print(f"  [DEBUG] {symbol} | mod={mode} | 1H hard filtre | RSI={round(rsi_v,1)} WR={round(wr_v,1)} MFI={round(mfi_v,1) if mfi_v else 'N/A'}", flush=True)
        return None

    # 3) 1H ön filtre
    if r1h["score_1h"] < 40:
        prev = _top_scores.get(symbol, {}).get("score", 0)
        if r1h["score_1h"] > prev:
            _top_scores[symbol] = {
                "score": r1h["score_1h"], "mode": mode,
                "rsi": r1h.get("rsi"), "wr": r1h.get("wr"),
            }
        stats["score_low"] += 1
        return None

    # 4) 4H teyit
    await_4h = True
    r4h = analyze_4h_unified(df_4h, mode) if df_4h is not None else None
    if r4h is None:
        stats["no_4h_data"] += 1
        print(f"  [DEBUG] {symbol} | mod={mode} | 1H={r1h['score_1h']} | 4H teyit NONE (df4={'None' if df_4h is None else len(df_4h)})", flush=True)
        return None

    score_total = r1h["score_1h"] + r4h["score_4h"]

    prev = _top_scores.get(symbol, {}).get("score", 0)
    if score_total > prev:
        _top_scores[symbol] = {
            "score": round(score_total, 1), "mode": mode,
            "rsi": r1h.get("rsi"), "wr": r1h.get("wr"),
            "dip": round(r1h["score_1h"], 1), "4h": round(r4h["score_4h"], 1),
        }

    if score_total < MIN_SCORE:
        stats["score_low"] += 1
        print(f"  [DEBUG] {symbol} | mod={mode} | toplam={score_total} < MIN_SCORE={MIN_SCORE} | 1H={r1h['score_1h']} 4H={r4h['score_4h']}", flush=True)
        return None

    # 5) Hedef ve stop — moda göre ATR çarpanı farklı
    entry = r1h["entry"]
    atr   = r1h["atr"] if r1h["atr"] > 0 else entry * 0.02

    if mode == "dip_donus":
        target = round(entry + atr * ATR_TARGET_MULT, 8)        # 2.5x
        stop   = round(entry - atr * ATR_STOP_MULT,   8)        # 1.5x
        signal_label = "DİP DÖNÜŞÜ"
        emoji = "🔵"
    else:
        target = round(entry + atr * (ATR_TARGET_MULT + 0.5), 8)  # 3.0x
        stop   = round(entry - atr * (ATR_STOP_MULT  - 0.3), 8)   # 1.2x
        signal_label = "TREND DEVAMI"
        emoji = "🟣"

    score100  = min(int(round(score_total)), 100)
    strength  = "güçlü" if score100 >= STRONG_SCORE else "normal"

    return {
        "symbol":        symbol,
        "signal_type":   mode,   # "dip_donus" | "trend_devam"
        "signal_label":  signal_label,
        "emoji":         emoji,
        "time":          datetime.now(timezone.utc).isoformat(),
        "entry":         entry,
        "target":        target,
        "stop":          stop,
        "target_pct":    round((target - entry) / entry * 100, 2),
        "stop_pct":      round((entry - stop)   / entry * 100, 2),
        "score100":      score100,
        "score_1h":      r1h["score_1h"],
        "score_4h":      r4h["score_4h"],
        "mode":          mode,
        "mode_info":     mode_info,
        # 1H puanlar
        "p_rsi":         r1h["p_rsi"],
        "p_wr":          r1h["p_wr"],
        "p_mfi":         r1h.get("p_mfi", 0),
        "p_macd":        r1h["p_macd"],
        "p_bb":          r1h["p_bb"],
        "p_vol":         r1h["p_vol"],
        "p_pullback":    r1h.get("p_pullback", 0),
        # 4H puanlar
        "p_ema":         r4h["p_ema"],
        "p_adx":         r4h["p_adx"],
        "p_obv":         r4h["p_obv"],
        "p_sqz":         r4h["p_sqz"],
        # İndikatör değerleri
        "rsi":           r1h.get("rsi"),
        "wr":            r1h.get("wr"),
        "mfi":           r1h.get("mfi"),
        "mfi_cross":     r1h.get("mfi_cross", False),
        "macd_txt":      r1h.get("macd_txt", ""),
        "bb_txt":        r1h.get("bb_txt", ""),
        "pullback_txt":  r1h.get("pullback_txt", ""),
        "vol_ratio":     r1h.get("vol_ratio", 0),
        "bvol_pct":      r1h.get("bvol_pct", 50),
        "ema_txt":       r4h["ema_txt"],
        "adx":           r4h["adx"],
        "obv_txt":       r4h["obv_txt"],
        "sqz_txt":       r4h.get("sqz_txt", ""),
        "sqz_on":        r4h["sqz_on"],
        "sqz_off":       r4h["sqz_off"],
        # Swing bilgisi
        "rise_from_low":  mode_info.get("rise_from_low", 0),
        "drop_from_high": mode_info.get("drop_from_high", 0),
        "swing_low_bars": mode_info.get("swing_low_bars_ago", 0),
        # Diğer
        "strength":      strength,
        "signal":        True,
    }

# ============================================================
# 9) TELEGRAM
# ============================================================
def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={
                "chat_id":                  TELEGRAM_CHAT_ID,
                "text":                     text,
                "parse_mode":               "HTML",
                "disable_web_page_preview": True,
            },
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
    """Unified Telegram mesajı — dip dönüşü ve trend devamı için tek fonksiyon."""
    sc    = r["score100"]
    mode  = r.get("signal_type", "dip_donus")
    emoji_score = "🟢" if sc >= STRONG_SCORE else ("🟡" if sc >= MIN_SCORE else "🔴")
    label = r.get("signal_label", "SİNYAL")
    sym   = r["symbol"].replace("/", "").replace("USDT", "")
    now   = tr_time.strftime("%d/%m/%Y %H:%M")
    mode_emoji = r.get("emoji", "🔵")

    def bold(t): return f'<b>{t}</b>'
    def code(t): return f'<code>{t}</code>'

    def val_color(val, good_thresh, bad_thresh, invert=False):
        if val is None: return "—"
        if invert:
            return f"🟢 {val}" if val <= good_thresh else (f"🔴 {val}" if val >= bad_thresh else f"🟡 {val}")
        return f"🟢 {val}" if val >= good_thresh else (f"🔴 {val}" if val <= bad_thresh else f"🟡 {val}")

    def puan_bar(p, max_p):
        filled = round(p / max_p * 5) if max_p > 0 else 0
        bar    = "█" * filled + "░" * (5 - filled)
        return f"{bar} {int(round(p))}/{max_p}"

    rsi_v   = r.get("rsi")
    wr_v    = r.get("wr")
    mfi_v   = r.get("mfi")
    adx_v   = r.get("adx")
    vol_r   = r.get("vol_ratio", 0)
    bvol_v  = r.get("bvol_pct", 50)

    # Renk eşikleri moda göre
    if mode == "dip_donus":
        rsi_txt = val_color(rsi_v, 25, 40, invert=True)
        wr_txt  = val_color(wr_v, -80, -50, invert=True)
        mfi_txt = val_color(mfi_v, 15, 25, invert=True) if mfi_v else "🟡 —"
    else:
        rsi_txt = val_color(rsi_v, 50, 62)   # 50-62 ideal
        wr_txt  = val_color(wr_v, -65, -20, invert=True)  # -65/-30 ideal
        mfi_txt = val_color(mfi_v, 40, 60, invert=True) if mfi_v else "🟡 —"

    adx_txt  = val_color(adx_v, 30, 20)
    vol_txt  = f"🟢 {vol_r:.1f}x" if vol_r >= 1.5 else (f"🟡 {vol_r:.1f}x" if vol_r >= 1.0 else f"🔴 {vol_r:.1f}x")
    bvol_txt = f"🟢 {bvol_v:.0f}% alici" if bvol_v >= 60 else (f"🟡 {bvol_v:.0f}% alici" if bvol_v >= 50 else f"🔴 {bvol_v:.0f}% alici")

    macd_t  = r.get("macd_txt", "—")
    ema_t   = r.get("ema_txt",  "—")
    obv_t   = r.get("obv_txt",  "—")
    sqz_t   = r.get("sqz_txt",  "—")
    bb_t    = r.get("bb_txt",   "—")
    pull_t  = r.get("pullback_txt", "")

    sqz_col = "🟢" if r.get("sqz_off") else ("🟡" if r.get("sqz_on") else "⚪")
    mfi_sfx = "  🔔" if r.get("mfi_cross") else ""

    # Puan değerleri
    p_rsi     = r.get("p_rsi",      0)
    p_wr      = r.get("p_wr",       0)
    p_mfi     = r.get("p_mfi",      0)
    p_macd    = r.get("p_macd",     0)
    p_bb      = r.get("p_bb",       0)
    p_vol     = r.get("p_vol",      0)
    p_pull    = r.get("p_pullback", 0)
    p_ema     = r.get("p_ema",      0)
    p_adx     = r.get("p_adx",      0)
    p_obv     = r.get("p_obv",      0)
    p_sqz     = r.get("p_sqz",      0)

    # Swing bilgisi
    rise  = r.get("rise_from_low",  0)
    drop  = r.get("drop_from_high", 0)
    s_bars = r.get("swing_low_bars", 0)
    swing_txt = f"Dipten +%{rise} | Tepeden -%{drop} | Dip {s_bars}H önce"

    # Puan tablosu moda göre farklı
    if mode == "dip_donus":
        puan_tablo = (
            f"RSI  {puan_bar(p_rsi,  25)}\n"
            f"W%%R  {puan_bar(p_wr,   15)}\n"
            f"MFI  {puan_bar(p_mfi,  15)}\n"
            f"MACD {puan_bar(p_macd, 10)}\n"
            f"BB   {puan_bar(p_bb,    5)}\n"
            f"Vol  {puan_bar(p_vol,   5)}\n"
            f"─────────────────\n"
            f"EMA  {puan_bar(p_ema,  15)}\n"
            f"ADX  {puan_bar(p_adx,   5)}\n"
            f"OBV  {puan_bar(p_obv,   5)}\n"
            f"SQZ  {puan_bar(p_sqz,   3)}"
        )
        ind_lines = [
            bold("── 1H İndikatörler ──"),
            f"{bold('RSI(14)')}      {rsi_txt}",
            f"{bold('Williams %R')}  {wr_txt}",
            f"{bold('MFI(14)')}      {mfi_txt}{mfi_sfx}",
            f"{bold('MACD Hist')}    🟢 {macd_t}",
            f"{bold('Bol. Band')}    {'🟢 ' + bb_t if bb_t else '🔴 tetiklenmedi'}",
            f"{bold('Hacim')}        {vol_txt}  ({bvol_txt})",
        ]
    else:
        puan_tablo = (
            f"RSI  {puan_bar(p_rsi,  20)}\n"
            f"W%%R  {puan_bar(p_wr,  10)}\n"
            f"MACD {puan_bar(p_macd, 20)}\n"
            f"Çekl {puan_bar(p_pull,  5)}\n"
            f"Vol  {puan_bar(p_vol,  15)}\n"
            f"BB   {puan_bar(p_bb,    5)}\n"
            f"─────────────────\n"
            f"EMA  {puan_bar(p_ema,  15)}\n"
            f"ADX  {puan_bar(p_adx,  10)}\n"
            f"OBV  {puan_bar(p_obv,   5)}\n"
            f"SQZ  {puan_bar(p_sqz,   3)}"
        )
        ind_lines = [
            bold("── 1H İndikatörler ──"),
            f"{bold('RSI(14)')}      {rsi_txt}",
            f"{bold('Williams %R')}  {wr_txt}",
            f"{bold('MFI(14)')}      {mfi_txt}",
            f"{bold('MACD Hist')}    🟢 {macd_t}",
            f"{bold('Çekilme')}      {'🟢 ' + pull_t if p_pull > 0 else '🟡 yok'}",
            f"{bold('Hacim')}        {vol_txt}  ({bvol_txt})",
        ]

    lines = [
        f"🕐 {now}",
        "",
        f"{mode_emoji} {bold(f'#{sym}/USDT')}  •  {label}  •  1H + 4H",
        f"📍 {code(swing_txt)}",
        "",
        f"💵 {bold('Giriş:')}  {fmt_price(r.get('entry'))}",
        f"🎯 {bold('Hedef:')}  {fmt_price(r.get('target'))}  {code(f'+%{r.get("target_pct")}' )}",
        f"🛡️ {bold('Stop:')}   {fmt_price(r.get('stop'))}  {code(f'-%{r.get("stop_pct")}' )}",
        "━━━━━━━━━━━━━━━━━━━━",
        f"📊 {bold('PUAN:')}  {bold(str(sc))}/100  {emoji_score}",
        "",
        code(puan_tablo),
        "",
        *ind_lines,
        "",
        bold("── 4H Teyit ──"),
        f"{bold('EMA20/SMA50')}  🟢 {ema_t}",
        f"{bold('ADX')}          {adx_txt}",
        f"{bold('OBV')}          🟢 {obv_t}",
        f"{bold('Squeeze')}      {sqz_col} {sqz_t}",
        "━━━━━━━━━━━━━━━━━━━━",
    ]
    return "\n".join(lines)

# ============================================================
# 10) BOOTSTRAP
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
# 11) MUM KAPANISINI ISLE
# ============================================================
def safe_float(val, dec=4):
    try:
        v = float(val)
        return None if (v != v or v in (float("inf"), float("-inf"))) else round(v, dec)
    except Exception:
        return None

async def refresh_4h(symbol):
    """1H skoru yeterli olunca 4H veriyi REST ile guncelle."""
    try:
        df4 = await fetch_df(symbol, "4h", BOOTSTRAP_4H)
        if df4 is not None and len(df4) >= 55:
            bars_4h[symbol] = prepare_4h(df4.iloc[-KEEP_4H:] if len(df4) > KEEP_4H else df4)
    except Exception:
        pass

async def on_1h_close(symbol, o, h, l, c, v, ts_ms, candidate_queue):
    global ws_1h_closes
    ws_1h_closes += 1
    beat(symbol=symbol, status="LIVE")

    # Acik sinyalleri bu mumun high/low ile kontrol et
    bar_time = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    check_pending_for_symbol(symbol, h, l, c, bar_time)

    df1 = bars_1h.get(symbol)
    if df1 is None or len(df1) < 60:
        stats["data_missing"] += 1
        return

    tstamp = pd.to_datetime(ts_ms, unit="ms", utc=True)
    df1.loc[tstamp, ["open","high","low","close","volume"]] = [o, h, l, c, v]
    df1 = df1.sort_index()
    if len(df1) > KEEP_1H:
        df1 = df1.iloc[-KEEP_1H:]
    df1 = prepare_1h(df1)
    bars_1h[symbol] = df1

    tr_now = datetime.now(timezone.utc).astimezone(TR_TZ)

    last_ts = last_signal_ts.get(symbol)
    if last_ts:
        hours = (tr_now.replace(tzinfo=None) - last_ts.replace(tzinfo=None)).total_seconds() / 3600
        if hours < SIGNAL_COOLDOWN_HOURS:
            stats["cooldown"] += 1
            return

    # Mod tespiti — 1H ön analiz (hafif)
    mode, mode_info = detect_market_mode(df1)

    prev = df1.iloc[-2]
    last_scan_result[symbol] = {
        "symbol":   symbol,
        "time":     tr_now.isoformat(),
        "price":    round(float(df1.iloc[-1]["close"]), 8),
        "rsi":      safe_float(prev.get("rsi")),
        "wr":       safe_float(prev.get("wr")),
        "mfi":      safe_float(prev.get("mfi")),
        "signal":   False,
        "score100": 0,
        "strength": "",
        "mode":     mode,
    }

    if mode in ("yorgun", "yatay"):
        if _top_scores.get(symbol, {}).get("score", 0) > 30:
            print(f"  [DEBUG] {symbol} | mod={mode} | rise={mode_info.get('rise_from_low')}% drop={mode_info.get('drop_from_high')}%", flush=True)
        return

    # 4H veri yenile
    await refresh_4h(symbol)
    df4 = bars_4h.get(symbol)

    result = full_analyze(symbol, df1, df4)

    if result is not None:
        last_scan_result[symbol]["signal"]   = True
        last_scan_result[symbol]["score100"] = result["score100"]
        last_scan_result[symbol]["strength"] = result["strength"]
        await candidate_queue.put(SignalCandidate(
            symbol=symbol, result=result, tr_time=tr_now,
        ))

async def on_4h_close(symbol, o, h, l, c, v, ts_ms):
    df4 = bars_4h.get(symbol)
    if df4 is None:
        return
    tstamp = pd.to_datetime(ts_ms, unit="ms", utc=True)
    df4.loc[tstamp, ["open","high","low","close","volume"]] = [o, h, l, c, v]
    df4 = df4.sort_index()
    if len(df4) > KEEP_4H:
        df4 = df4.iloc[-KEEP_4H:]
    bars_4h[symbol] = prepare_4h(df4)

# ============================================================
# 12) SİNYAL WORKER
# ============================================================
async def signal_worker(candidate_queue):
    while True:
        sig = await candidate_queue.get()
        try:
            symbol  = sig.symbol
            result  = sig.result
            tr_time = sig.tr_time

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
            if len(all_signals) > 200:
                all_signals.pop()
            stats["signal_sent"] += 1
            log_signal(result, tr_time)
            # Pending takibe ekle
            pending_by_symbol.setdefault(result["symbol"], []).append(signal_log[0])

            print(
                f"SINYAL: {symbol} | {result['score100']}/100 [{result['strength']}] | "
                f"RSI:{result['rsi']}({result['p_rsi']}p) "
                f"WR:{result['wr']}({result['p_wr']}p) "
                f"MFI:{result['mfi']}({result['p_mfi']}p) "
                f"MACD:{result['p_macd']}p "
                f"EMA:{result['p_ema']}p "
                f"ADX:{result['adx']}({result['p_adx']}p)",
                flush=True
            )

        except Exception as e:
            print(f"Worker hata: {str(e)[:100]}", flush=True)
        finally:
            candidate_queue.task_done()


# ============================================================
# 12b) SİNYAL PERFORMANS TAKİP
# ============================================================

SIGNAL_LOG_PATH = "/tmp/signal_log.json"

def load_signal_log() -> list:
    try:
        with open(SIGNAL_LOG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []

def save_signal_log(log: list):
    try:
        with open(SIGNAL_LOG_PATH, "w", encoding="utf-8") as f:
            json.dump(log[-500:], f, ensure_ascii=False, default=str)
    except Exception as e:
        print(f"signal_log kayit hata: {e}", flush=True)

signal_log: list = load_signal_log()

def log_signal(result: dict, tr_time):
    """Yeni sinyal kaydeder."""
    entry = {
        "id":          f"{result['symbol']}_{int(tr_time.timestamp())}",
        "symbol":      result["symbol"],
        "signal_type": result.get("signal_type", "dip"),
        "entry":       result["entry"],
        "target":      result["target"],
        "stop":        result["stop"],
        "target_pct":  result.get("target_pct", 0),
        "stop_pct":    result.get("stop_pct", 0),
        "score":       result["score100"],
        "time":        tr_time.isoformat(),
        "status":      "open",    # open | win | loss | expired
        "peak_pct":    0.0,       # en yuksek anlık getiri (%)
        "close_time":  None,      # kapanma zamani
        "close_price": None,      # kapanma fiyati
        "close_ret":   None,      # kapanma getirisi (%)
    }
    signal_log.insert(0, entry)
    if len(signal_log) > 500:
        signal_log.pop()
    save_signal_log(signal_log)

# Pending sinyaller: {symbol: [entry, ...]}
pending_by_symbol: dict = {}

def rebuild_pending():
    """Baslangicta acik sinyalleri pending'e yukle."""
    for s in signal_log:
        if s["status"] == "open":
            sym = s["symbol"]
            pending_by_symbol.setdefault(sym, []).append(s)

def check_pending_for_symbol(symbol: str, bar_high: float, bar_low: float, bar_close: float, bar_time):
    """
    Her 1H mum kapanisinda o sembolun acik sinyallerini kontrol et.
    - Stop tetiklendiyse: aninda LOSS, bir daha bakma
    - Hedef tetiklendiyse: aninda WIN
    - 72H gecti, ne hedef ne stop: EXPIRED, peak kaydet
    Ayni mumda ikisi de tetiklendiyse ONCE STOP kontrol edilir (konservatif).
    """
    if symbol not in pending_by_symbol:
        return

    now      = datetime.now(timezone.utc)
    to_close = []

    for entry in pending_by_symbol[symbol]:
        e   = entry["entry"]
        tgt = entry["target"]
        stp = entry["stop"]

        sig_time = datetime.fromisoformat(entry["time"])
        if sig_time.tzinfo is None:
            sig_time = sig_time.replace(tzinfo=timezone.utc)
        elapsed_h = (now - sig_time).total_seconds() / 3600

        # Peak guncelle
        cur_ret = (bar_high - e) / e * 100
        if cur_ret > entry["peak_pct"]:
            entry["peak_pct"] = round(cur_ret, 2)

        # Ayni mumda once stop kontrol et (konservatif)
        if bar_low <= stp:
            entry["status"]      = "loss"
            entry["close_time"]  = bar_time.isoformat() if hasattr(bar_time, "isoformat") else str(bar_time)
            entry["close_price"] = round(stp, 8)
            entry["close_ret"]   = round((stp - e) / e * 100, 2)
            to_close.append(entry)
            continue

        if bar_high >= tgt:
            entry["status"]      = "win"
            entry["close_time"]  = bar_time.isoformat() if hasattr(bar_time, "isoformat") else str(bar_time)
            entry["close_price"] = round(tgt, 8)
            entry["close_ret"]   = round((tgt - e) / e * 100, 2)
            to_close.append(entry)
            continue

        # 72H gecti — expired
        if elapsed_h >= 72:
            entry["status"]      = "expired"
            entry["close_time"]  = bar_time.isoformat() if hasattr(bar_time, "isoformat") else str(bar_time)
            entry["close_price"] = round(bar_close, 8)
            entry["close_ret"]   = round((bar_close - e) / e * 100, 2)
            to_close.append(entry)

    if to_close:
        for e in to_close:
            pending_by_symbol[symbol].remove(e)
        if not pending_by_symbol[symbol]:
            del pending_by_symbol[symbol]
        save_signal_log(signal_log)

def outcome_tracker_thread():
    """Baslangicta pending sinyalleri yukle."""
    rebuild_pending()
    print(f"Sinyal tracker baslatildi | {sum(len(v) for v in pending_by_symbol.values())} acik sinyal", flush=True)

def perf_summary() -> dict:
    """Sinyal performans ozeti."""
    closed = [s for s in signal_log if s["status"] in ("win","loss","expired")]
    if not closed:
        return {}

    wins    = sum(1 for s in closed if s["status"] == "win")
    losses  = sum(1 for s in closed if s["status"] == "loss")
    expired = sum(1 for s in closed if s["status"] == "expired")
    win_pct = round(wins / len(closed) * 100, 1) if closed else 0

    # Ortalama 24H getiri
    rets = [s["outcomes"]["24h"]["ret_pct"] for s in closed
            if "24h" in s["outcomes"] and "ret_pct" in s["outcomes"]["24h"]]
    avg_ret = round(sum(rets)/len(rets), 2) if rets else 0

    return {
        "total":    len(signal_log),
        "closed":   len(closed),
        "open":     len(signal_log) - len(closed),
        "wins":     wins,
        "losses":   losses,
        "expired":  expired,
        "win_pct":  win_pct,
        "avg_ret_24h": avg_ret,
    }

# ============================================================
# 13) WEBSOCKET
# ============================================================
def to_ws(symbol):
    return symbol.replace("/", "").lower()

async def ws_1h_chunk(symbols, candidate_queue):
    streams = "/".join([f"{to_ws(s)}@kline_1h" for s in symbols])
    url     = f"wss://stream.binance.com:9443/stream?streams={streams}"
    retry   = 0
    while True:
        try:
            async with websockets.connect(
                url,
                ping_interval=None,
                open_timeout=30,
                close_timeout=10,
                max_size=10 * 1024 * 1024,
            ) as ws:
                retry = 0
                print(f"1H WS baglandi ({len(symbols)} sembol)", flush=True)

                # Ping'i ayrı task olarak calistir — on_1h_close'dan bagimsiz
                async def keep_alive(ws):
                    while True:
                        await asyncio.sleep(20)
                        try:
                            pong = await ws.ping()
                            await asyncio.wait_for(pong, timeout=10)
                        except Exception:
                            break  # WS kapanmis, dis dongu yeniden baglayacak

                ping_task = asyncio.create_task(keep_alive(ws))
                try:
                    while True:
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=60)
                        except asyncio.TimeoutError:
                            continue  # Ping task zaten hallediyor
                        data = json.loads(msg)
                        k    = data.get("data", {}).get("k", {})
                        if not k.get("x", False): continue
                        sym = data.get("data", {}).get("s", "").upper().replace("USDT", "/USDT")
                        try:
                            await on_1h_close(
                                sym,
                                float(k["o"]), float(k["h"]),
                                float(k["l"]), float(k["c"]),
                                float(k["v"]), int(k["t"]),
                                candidate_queue,
                            )
                        except Exception as e:
                            print(f"on_1h_close hata [{sym}]: {str(e)[:100]}", flush=True)
                finally:
                    ping_task.cancel()
        except Exception as e:
            retry  += 1
            backoff = min(60, 5 * (2 ** min(retry, 4)))
            print(f"1H WS koptu -> {backoff}s: {str(e)[:50]}", flush=True)
            await asyncio.sleep(backoff)

async def ws_all(symbols, candidate_queue):
    """Sadece 1H WebSocket — 4H verisi REST ile istege bagli guncellenir."""
    tasks = []
    for i in range(0, len(symbols), WS_STREAM_CHUNK):
        chunk = symbols[i:i + WS_STREAM_CHUNK]
        tasks.append(asyncio.create_task(ws_1h_chunk(chunk, candidate_queue)))
    print(f"4H WebSocket kapali — REST ile on-demand guncelleme aktif", flush=True)
    await asyncio.gather(*tasks)

# ============================================================
# 14) FLASK DASHBOARD
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
        heartbeat["last"]  = tr_now_str()
        heartbeat["epoch"] = now
        if symbol: heartbeat["symbol"] = symbol
        if status: heartbeat["status"] = status
        _last_beat = now

def heartbeat_pinger():
    while True:
        heartbeat["last"]  = tr_now_str()
        heartbeat["epoch"] = time.time()
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
    now      = datetime.now(TR_TZ).strftime("%H:%M:%S")
    sig_rows = ""

    for s in all_signals[:20]:
        sc   = s.get("score100", 0)
        col  = score_color(sc)
        c1   = s.get("conditions_1h", {})
        c4   = s.get("conditions_4h", {})
        met  = sum(c1.values()) + sum(c4.values())
        lbl  = "Guclu ⚡" if s.get("strength") == "guclu" else "Normal"
        puan = (
            f"RSI:{s.get('p_rsi',0):.0f} WR:{s.get('p_wr',0):.0f} "
            f"MFI:{s.get('p_mfi',0):.0f} MACD:{s.get('p_macd',0):.0f} "
            f"BB:{s.get('p_bb',0):.0f} Vol:{s.get('p_vol',0):.0f} | "
            f"EMA:{s.get('p_ema',0):.0f} ADX:{s.get('p_adx',0):.0f} OBV:{s.get('p_obv',0):.0f}"
        )
        sig_rows += (
            f'<div class="sig">'
            f'<div class="sr"><b>{s.get("symbol","")}</b>'
            f'<span style="color:{col};font-weight:bold">{sc}/100</span>'
            f'<span style="color:#3d5a6a;font-size:.65rem">{lbl}</span></div>'
            f'<div class="sd"><span style="color:#00d4ff">${fmt_price(s.get("entry"))}</span>'
            f' → 🎯 {fmt_price(s.get("target"))} (+%{s.get("target_pct","")})'
            f'  🛡️ {fmt_price(s.get("stop"))} (-%{s.get("stop_pct","")})</div>'
            f'<div class="sd">RSI {s.get("rsi","")} | W%R {s.get("wr","")} | MFI {s.get("mfi","")} | ADX {s.get("adx","")}</div>'
            f'<div class="sd" style="color:#3d5a6a;font-size:.65rem">{puan}</div>'
            f'<div class="sd" style="color:#3d5a6a">{s.get("time","")[:16]} UTC</div>'
            f'</div>'
        )

    return f"""<!DOCTYPE html>
<html><head>
<meta charset="UTF-8"><title>Scanner v3.1</title>
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
<h1>TREND & MOMENTUM SCANNER <small style="font-size:.65rem;color:#3d5a6a">v3.1 | 1H + 4H</small></h1>
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
  Heartbeat: {heartbeat["last"]} | Son coin: {heartbeat["symbol"]} | <a href="/performance" style="color:#00d4ff">📈 Performans</a><br>
  Eleme: Skor:{stats.get("score_low",0)} Cooldown:{stats.get("cooldown",0)}
  Hacim:{stats.get("low_liquidity",0)} 4H:{stats.get("no_4h_data",0)} 1H:{stats.get("data_missing",0)}
</div>
</body></html>"""

@flask_app.route("/api/status")
def api_status():
    data = clean_json({
        "status":        bot_status["status"],
        "total_symbols": len(tracked_symbols),
        "ws_1h_closes":  ws_1h_closes,
        "signals":       all_signals[:30],
        "last_scan":     dict(last_scan_result),
        "stats":         dict(stats),
        "heartbeat":     heartbeat,
    })
    return flask_app.response_class(
        json.dumps(data, ensure_ascii=False),
        mimetype="application/json",
    )

@flask_app.route("/api/health")
def api_health():
    return {"status": "ok", "time": tr_now_str()}

@flask_app.route("/api/performance")
def api_performance():
    data = clean_json({
        "summary":     perf_summary(),
        "signal_log":  signal_log[:100],
    })
    return flask_app.response_class(
        json.dumps(data, ensure_ascii=False, default=str),
        mimetype="application/json",
    )

@flask_app.route("/performance")
def perf_dashboard():
    ps   = perf_summary()
    rows = ""
    for s in signal_log[:50]:
        st     = s.get("status","open")
        st_col = "#00f080" if st=="win" else ("#ff4444" if st=="loss" else ("#ffb300" if st=="expired" else "#3d5a6a"))
        o24    = s.get("outcomes",{}).get("24h",{})
        o48    = s.get("outcomes",{}).get("48h",{})
        o72    = s.get("outcomes",{}).get("72h",{})
        close_ret  = s.get("close_ret")
        peak_pct   = s.get("peak_pct", 0)
        close_time = (s.get("close_time") or "")[:16]

        def fmt_ret(r):
            if r is None: return "—"
            col = "#00f080" if float(r)>0 else "#ff4444"
            return f'<span style="color:{col}">{r:+}%</span>'

        rows += f"""<tr>
            <td>{s.get("time","")[:16]}</td>
            <td><b>{s.get("symbol","")}</b></td>
            <td>{"🔵" if s.get("signal_type")=="dip" else "🟣"}</td>
            <td>{s.get("score","")}/100</td>
            <td>${s.get("entry","")}</td>
            <td style="color:#00f080">+{peak_pct}%</td>
            <td>{fmt_ret(close_ret)}</td>
            <td>{close_time}</td>
            <td style="color:{st_col}">{st.upper()}</td>
        </tr>"""

    return f"""<!DOCTYPE html><html lang="tr"><head>
<meta charset="UTF-8"><title>Performans</title>
<meta http-equiv="refresh" content="300">
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:#06090d;color:#b8cdd8;font-family:'Courier New',monospace;padding:24px}}
  h1{{color:#00d4ff;font-size:1.1rem;margin-bottom:4px}}
  .sub{{color:#3d5a6a;font-size:.75rem;margin-bottom:20px}}
  .cards{{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:24px}}
  .card{{background:#0c1117;border:1px solid #1c2a36;border-radius:4px;padding:14px;min-width:120px;text-align:center}}
  .cv{{font-size:1.4rem;font-weight:bold;color:#00d4ff}}
  .cl{{font-size:.65rem;color:#3d5a6a;margin-top:4px}}
  table{{width:100%;border-collapse:collapse}}
  th{{background:#0c1117;color:#3d5a6a;font-size:.65rem;text-transform:uppercase;padding:7px 10px;text-align:left;border-bottom:1px solid #1c2a36}}
  td{{padding:7px 10px;border-bottom:1px solid #0c1117;font-size:.78rem}}
  tr:hover td{{background:#0c1117}}
  a{{color:#00d4ff;text-decoration:none}}
</style></head><body>
<h1>📈 SİNYAL PERFORMANSI</h1>
<div class="sub"><a href="/">← Ana Sayfa</a> &nbsp;|&nbsp; {tr_now_str()} &nbsp;|&nbsp; Her 5 dakikada yenilenir</div>
<div class="cards">
  <div class="card"><div class="cv">{ps.get("total",0)}</div><div class="cl">Toplam Sinyal</div></div>
  <div class="card"><div class="cv">{ps.get("open",0)}</div><div class="cl">Açık</div></div>
  <div class="card"><div class="cv" style="color:#00f080">{ps.get("wins",0)}</div><div class="cl">Kazanan</div></div>
  <div class="card"><div class="cv" style="color:#ff4444">{ps.get("losses",0)}</div><div class="cl">Kaybeden</div></div>
  <div class="card"><div class="cv" style="color:#ffb300">{ps.get("expired",0)}</div><div class="cl">Expired</div></div>
  <div class="card"><div class="cv" style="color:{"#00f080" if ps.get("win_pct",0)>=50 else "#ff4444"}">{ps.get("win_pct",0)}%</div><div class="cl">Kazanma Oranı</div></div>
  <div class="card"><div class="cv">{ps.get("avg_ret_24h","—")}%</div><div class="cl">Ort. 24H Getiri</div></div>
</div>
<table><thead><tr>
  <th>Sinyal Zamanı</th><th>Sembol</th><th>Tip</th><th>Skor</th><th>Giriş</th>
  <th>Peak %</th><th>Kapanış %</th><th>Kapanış Zamanı</th><th>Durum</th>
</tr></thead><tbody>{rows}</tbody></table>
</body></html>"""

# ============================================================
# 15) MAIN
# ============================================================
async def periodic_summary():
    while True:
        await asyncio.sleep(600)
        print_summary()

async def main():
    print("Trend & Momentum Scanner v3.1 baslatiliyor...", flush=True)

    symbols = await load_symbols_pool()
    if not symbols:
        print("Sembol yuklenemedi", flush=True)
        return

    global tracked_symbols
    tracked_symbols      = list(symbols)
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
    threading.Thread(target=start_flask,        daemon=True).start()
    threading.Thread(target=heartbeat_pinger,  daemon=True).start()
    threading.Thread(target=watchdog_thread,   daemon=True).start()
    threading.Thread(target=outcome_tracker_thread, daemon=True).start()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Durduruldu", flush=True)
