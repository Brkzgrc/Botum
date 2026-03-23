# -*- coding: utf-8 -*-
"""
Trend & Momentum Scanner v5.0
==============================
DIP sistemi (backtest_dip_v1):
  StochRSI < 0.05, WR < -80, OBV_OSC < -50, WT < -75, MACD hist <= 0
  Stop: -%10 | Cooldown: 4H | Funding < 0 → 💰

TREND sistemi (backtest_trend v21/v22):
  BB_BREAK: ALL4_LOOSE + bu barda BB(15) ust bant kirilimi
    → Train %80.7 / Test %78.0
  VOL3_BB:  ALL4_LOOSE + 3 barda hacim trendi + BB kirilimi
    → Train %79.4 / Test %78.3
  Stop: -%10 | Cooldown: 4H | Etiket: 📈 TREND
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

# Dip sistemi esikleri
STOCH_RSI_THRESH = float(os.getenv("STOCH_RSI_THRESH", "0.05"))
WR_THRESH        = float(os.getenv("WR_THRESH",        "-80"))
OBV_OSC_THRESH   = float(os.getenv("OBV_OSC_THRESH",   "-50"))
WT_THRESH        = float(os.getenv("WT_THRESH",        "-75"))
STOP_PCT         = float(os.getenv("STOP_PCT",         "10.0"))
TREND_STOP_PCT   = float(os.getenv("TREND_STOP_PCT",   "10.0"))

SIGNAL_COOLDOWN_HOURS = int(os.getenv("SIGNAL_COOLDOWN_HOURS", "4"))
MIN_LIQUIDITY         = float(os.getenv("MIN_LIQUIDITY",       "500000"))
MAX_SYMBOLS           = int(os.getenv("MAX_SYMBOLS",           "0"))

WS_STREAM_CHUNK = int(os.getenv("WS_STREAM_CHUNK", "120"))
BOOTSTRAP_BARS  = int(os.getenv("BOOTSTRAP_BARS",  "500"))
KEEP_BARS       = int(os.getenv("KEEP_BARS",       "300"))

BOOT_EVERY = 50
TR_TZ      = timezone(timedelta(hours=3))

IGNORED_COINS = set([
    'UP/USDT','DOWN/USDT','BEAR/USDT','BULL/USDT',
    'USDC/USDT','TUSD/USDT','FDUSD/USDT','DAI/USDT','USDP/USDT',
    'EUR/USDT','TRY/USDT','GBP/USDT','BUSD/USDT','USTC/USDT',
    'PAXG/USDT','WBTC/USDT','USDE/USDT','BRL/USDT','RUB/USDT',
    'AUD/USDT','UST/USDT','USD/USDT','XUSD/USDT','USD1/USDT','BFUSD/USDT',
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
    for k, lbl in [
        ("cooldown",      "Cooldown"),
        ("low_liquidity", "Dusuk hacim"),
        ("data_missing",  "Veri yok"),
        ("filtered",      "Filtre eledi"),
    ]:
        v = stats.get(k, 0)
        if v:
            print(f"  {lbl:20s}: {v}", flush=True)
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
    sig_type: str = "dip"   # "dip" veya "trend"

bars_1h:          dict = {}
funding_cache:    dict = {}
last_signal_ts:   dict = {}   # {symbol: {"dip": dt, "trend": dt}}
last_scan_result: dict = {}
all_signals:      list = []

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
    for i in range(0, len(syms), 120):
        part = syms[i:i + 120]
        try:
            res = await api_gate.call(exchange_spot.fetch_tickers, part)
            if isinstance(res, dict):
                for k, v in res.items():
                    volumes[k] = float(v.get("quoteVolume", 0) or 0)
        except Exception:
            continue

    sorted_syms = sorted(syms, key=lambda x: volumes.get(x, 0), reverse=True)
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
    """Hem dip hem trend için tüm indikatörler"""
    df = df.copy()
    c, h, l, v = df["close"], df["high"], df["low"], df["volume"]

    # ── Williams %R ─────────────────────────────────────────
    h14      = h.rolling(14).max()
    l14      = l.rolling(14).min()
    df["wr"] = -100 * (h14 - c) / (h14 - l14).replace(0, np.nan)

    # ── StochRSI ─────────────────────────────────────────────
    d    = c.diff()
    gain = d.clip(lower=0).ewm(com=13, adjust=False).mean()
    loss = (-d).clip(lower=0).ewm(com=13, adjust=False).mean()
    rsi  = 100 - 100 / (1 + gain / loss.replace(0, np.nan))
    rsi_min     = rsi.rolling(14).min()
    rsi_max     = rsi.rolling(14).max()
    stoch_k     = (rsi - rsi_min) / (rsi_max - rsi_min).replace(0, np.nan)
    df["stoch_rsi"] = stoch_k.rolling(3).mean()

    # ── OBV Oscillator ───────────────────────────────────────
    obv          = (v * np.sign(c.diff()).fillna(0)).cumsum()
    obv_ma       = obv.rolling(20).mean()
    df["obv_osc"] = (obv - obv_ma) / obv_ma.abs().replace(0, np.nan) * 100

    # ── WaveTrend ────────────────────────────────────────────
    ap    = (h + l + c) / 3
    esa   = ap.ewm(span=10, adjust=False).mean()
    d_abs = (ap - esa).abs().ewm(span=10, adjust=False).mean()
    ci    = (ap - esa) / (0.015 * d_abs.replace(0, np.nan))
    df["wt"] = ci.ewm(span=21, adjust=False).mean()

    # ── MACD Histogram ───────────────────────────────────────
    e12         = c.ewm(span=12, adjust=False).mean()
    e26         = c.ewm(span=26, adjust=False).mean()
    macd_line   = e12 - e26
    signal_line = macd_line.ewm(span=9, adjust=False).mean()
    df["macd_hist"] = macd_line - signal_line

    # ── Trend indikatörleri ──────────────────────────────────
    # ATR
    tr       = pd.concat([h-l,(h-c.shift()).abs(),(l-c.shift()).abs()],axis=1).max(axis=1)
    df["atr"]    = tr.ewm(alpha=1/14, adjust=False).mean()
    df["vol_ma"] = v.rolling(20).mean()

    # EMA50 / EMA200
    df["ema50"]  = c.ewm(span=50,  adjust=False).mean()
    df["ema200"] = c.ewm(span=200, adjust=False).mean()

    # ADX
    up   = h.diff(); dn = -l.diff()
    pdm  = up.where((up>dn)&(up>0), 0.0)
    mdm  = dn.where((dn>up)&(dn>0), 0.0)
    atr14= tr.ewm(alpha=1/14, adjust=False).mean()
    pdi  = 100*pdm.ewm(alpha=1/14,adjust=False).mean()/(atr14+1e-10)
    mdi  = 100*mdm.ewm(alpha=1/14,adjust=False).mean()/(atr14+1e-10)
    dx   = (pdi-mdi).abs()/(pdi+mdi+1e-10)*100
    df["adx"] = dx.ewm(alpha=1/14, adjust=False).mean()

    # BB(15, 2.0)
    bb_ma        = c.rolling(15).mean()
    bb_std       = c.rolling(15).std()
    df["bb15_upper"] = bb_ma + 2.0 * bb_std

    return df.dropna(subset=["stoch_rsi","wr","obv_osc","wt","macd_hist",
                              "atr","ema50","ema200","adx","bb15_upper"])

# ============================================================
# 6) SİNYAL KRİTERLERİ
# ============================================================
def check_dip_signal(df, symbol):
    """Dip sistemi — mevcut backtest_dip_v1 parametreleri"""
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

    if None in (stoch, wr, obv, wt, hist):
        return None

    if stoch >= STOCH_RSI_THRESH: return None
    if wr    >= WR_THRESH:        return None
    if obv   >= OBV_OSC_THRESH:   return None
    if wt    >= WT_THRESH:        return None
    if hist  >  0:                return None

    funding     = funding_cache.get(symbol)
    funding_neg = funding is not None and funding < 0
    stop        = round(entry * (1 - STOP_PCT / 100), 8)

    return {
        "symbol":      symbol,
        "type":        "dip",
        "entry":       round(entry, 8),
        "stop":        round(stop,  8),
        "stoch_rsi":   round(stoch, 4),
        "wr":          round(wr,    2),
        "obv_osc":     round(obv,   2),
        "wt":          round(wt,    2),
        "macd_hist":   round(hist,  8),
        "funding":     round(funding, 6) if funding is not None else None,
        "funding_neg": funding_neg,
    }

def check_trend_signal(df, symbol):
    """
    Trend sistemi — BB_BREAK_NOW + VOL3_BB_NOW
    ALL4_LOOSE base:
      Displacement + Yerel Direnc + ADX>20 yukselen
      EMA50>%8 + EMA200>%12 + ATR/Fiyat>%1.5 + Son3bar>%5
    + BB(15) ust bant kirilimi (onceki bar altindaydi)
    Ek: VOL3 modu — son 3 barda hacim yukselis trendi
    """
    if len(df) < 60:
        return None

    # Son kapanan mum (sinyal mumu)
    bar   = df.iloc[-2]
    prev  = df.iloc[-3]
    entry = float(df.iloc[-1]["close"])

    def sf(row, col):
        v = row.get(col, np.nan)
        return None if pd.isna(v) else float(v)

    o    = sf(bar, "open")
    c    = sf(bar, "close")
    h    = sf(bar, "high")
    l    = sf(bar, "low")
    vol  = sf(bar, "volume")
    atr  = sf(bar, "atr")
    vm   = sf(bar, "vol_ma")
    e50  = sf(bar, "ema50")
    e200 = sf(bar, "ema200")
    adx  = sf(bar, "adx")
    bbu  = sf(bar, "bb15_upper")
    pbbu = sf(prev, "bb15_upper")
    adx3 = sf(df.iloc[-5], "adx")  # 3 bar onceki adx

    if None in (o, c, h, l, vol, atr, vm, e50, e200, adx, bbu, pbbu, adx3):
        return None
    if atr <= 0 or vm <= 0 or e50 <= 0 or e200 <= 0:
        return None

    # ALL4_LOOSE koşulları

    # 1. Displacement: güçlü yeşil mum
    if c <= o: return None
    body = abs(c - o)
    if body < atr * 1.2: return None
    rng = h - l
    if rng <= 0: return None
    if (c - l) / rng < 0.65: return None
    if vol < vm * 1.6: return None

    # 2. Yerel direnc kirilimi (son 50 bar)
    highs50 = df["high"].iloc[-52:-2].values
    if len(highs50) < 10: return None
    local_res = float(np.max(highs50))
    if c < local_res * 1.01: return None

    # 3. ADX > 20 ve yükselen
    if adx <= 20: return None
    if adx <= adx3: return None

    # 4. EMA50 uzaklığı > %8
    if (c - e50) / e50 * 100 <= 8.0: return None

    # 5. EMA200 uzaklığı > %12
    if (c - e200) / e200 * 100 <= 12.0: return None

    # 6. ATR/Fiyat > %1.5
    if atr / c * 100 <= 1.5: return None

    # 7. Son 3 bar getirisi > %5
    c3 = sf(df.iloc[-5], "close")
    if c3 is None or c3 <= 0: return None
    if (c - c3) / c3 * 100 <= 5.0: return None

    # 8. BB(15) kırılımı: önceki bar altında, bu bar üstünde
    if sf(prev, "close") >= pbbu: return None   # önceki zaten üstündeydi
    if c < bbu: return None                      # bu bar üste çıkmadı

    # Hangi trend tipi?
    trend_subtype = "BB_BREAK"

    # VOL3 kontrolü (ek kalite — varsa işaretle)
    vol3_ok = False
    if len(df) >= 6:
        v_win = df["volume"].iloc[-5:-2].values  # son 3 bar (sinyal hariç)
        if len(v_win) == 3:
            x     = np.arange(3, dtype=float)
            slope = np.polyfit(x, v_win, 1)[0]
            if slope > 0 and v_win[-1] >= np.mean(v_win):
                vol3_ok = True
                trend_subtype = "VOL3_BB"

    stop = round(entry * (1 - TREND_STOP_PCT / 100), 8)

    return {
        "symbol":       symbol,
        "type":         "trend",
        "subtype":      trend_subtype,
        "entry":        round(entry, 8),
        "stop":         round(stop,  8),
        "ema50_dist":   round((c-e50)/e50*100, 1),
        "ema200_dist":  round((c-e200)/e200*100, 1),
        "adx":          round(adx, 1),
        "atr_ratio":    round(atr/c*100, 2),
        "vol3_ok":      vol3_ok,
        "funding":      funding_cache.get(symbol),
        "funding_neg":  False,
    }

# ============================================================
# 7) TELEGRAM
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
    if p >= 100:   return f"{p:.2f}"
    if p >= 1:     return f"{p:.3f}"
    if p >= 0.01:  return f"{p:.4f}"
    return f"{p:.6f}"

def build_dip_message(r, tr_time, sig_num):
    now         = tr_time.strftime("%d/%m/%Y %H:%M")
    sym         = r["symbol"].replace("/USDT", "")
    funding_neg = r.get("funding_neg", False)
    funding_val = r.get("funding")
    icon        = "💰" if funding_neg else "🔵"

    fund_line = None
    if funding_val is not None:
        fund_str  = f"{funding_val:+.4f}%"
        fund_line = f"Funding    {fund_str}{'  💰' if funding_neg else ''}"

    hist_val = r.get("macd_hist", 0)
    hist_str = f"{hist_val:.6f}" if abs(hist_val) < 0.0001 else (
               f"{hist_val:.5f}" if abs(hist_val) < 0.01 else f"{hist_val:.4f}")

    fund_str2 = f"{funding_val:+.4f}%" if funding_val is not None else None

    lines = [
        f"🕐 {now}",
        "",
        f"{icon} <b>#{sym}USDT  •  DİP DÖNÜŞÜ  •  1H</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        f"💵 <b>Giriş</b>    {fmt_price(r['entry'])}",
        f"🛡️ <b>Stop</b>     {fmt_price(r['stop'])}  (-%{STOP_PCT:.0f})",
        "━━━━━━━━━━━━━━━━━━━━",
        "📊 <b>İndikatörler</b>",
        f"<b>StochRSI</b>   {r['stoch_rsi']:.4f}",
        f"<b>W%R</b>        {r['wr']:.1f}",
        f"<b>OBV_OSC</b>    {r['obv_osc']:.1f}",
        f"<b>WaveTrend</b>  {r['wt']:.1f}",
        f"<b>MACD Hist</b>  {hist_str}",
    ]
    if fund_str2:
        fund_icon = "  💰" if funding_neg else ""
        lines.append(f"<b>Funding</b>    {fund_str2}{fund_icon}")
    lines += [
        "━━━━━━━━━━━━━━━━━━━━",
        f"⏱ Cooldown: {SIGNAL_COOLDOWN_HOURS}H  |  #{sig_num} sinyal",
    ]
    return "\n".join(lines)

def build_trend_message(r, tr_time, sig_num):
    now      = tr_time.strftime("%d/%m/%Y %H:%M")
    sym      = r["symbol"].replace("/USDT", "")
    subtype  = r.get("subtype", "BB_BREAK")
    vol3     = r.get("vol3_ok", False)

    # VOL3_BB daha kaliteli — özel ikon
    quality = "⭐ VOL3+BB" if vol3 else "BB Kırılım"

    lines = [
        f"🕐 {now}",
        "",
        f"📈 <b>#{sym}USDT  •  TREND  •  1H  •  {quality}</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        f"💵 <b>Giriş</b>      {fmt_price(r['entry'])}",
        f"🛡️ <b>Stop</b>       {fmt_price(r['stop'])}  (-%{TREND_STOP_PCT:.0f})",
        "━━━━━━━━━━━━━━━━━━━━",
        "📊 <b>Trend Göstergeleri</b>",
        f"<b>EMA50 Uzak</b>   +{r['ema50_dist']:.1f}%",
        f"<b>EMA200 Uzak</b>  +{r['ema200_dist']:.1f}%",
        f"<b>ADX</b>          {r['adx']:.1f}",
        f"<b>ATR/Fiyat</b>    %{r['atr_ratio']:.2f}",
        "━━━━━━━━━━━━━━━━━━━━",
        f"✅ Başarı: ~%78 (test verisi)",
        f"⏱ Cooldown: {SIGNAL_COOLDOWN_HOURS}H  |  #{sig_num} sinyal",
    ]
    return "\n".join(lines)

# ============================================================
# 8) BOOTSTRAP
# ============================================================
async def bootstrap_symbol(symbol):
    try:
        df = await fetch_df(symbol, "1h", BOOTSTRAP_BARS)
        if df is None or len(df) < 60:
            return False
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
# 9) SİNYAL WORKER
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

            # Hacim kontrolü
            try:
                ticker    = await api_gate.call(exchange_spot.fetch_ticker, symbol)
                liquidity = float(ticker.get("quoteVolume", 0) or 0)
                if liquidity < MIN_LIQUIDITY:
                    stats["low_liquidity"] += 1
                    candidate_queue.task_done()
                    continue
            except Exception:
                pass

            # Funding rate güncelle
            await fetch_funding_rate(symbol)
            result["funding"]     = funding_cache.get(symbol)
            result["funding_neg"] = (
                result["funding"] is not None and result["funding"] < 0
            )

            signal_counter += 1

            # Mesaj tipine göre gönder
            if sig_type == "trend":
                msg = build_trend_message(result, tr_time, signal_counter)
                stats["trend_sent"] += 1
                icon = "📈"
            else:
                msg = build_dip_message(result, tr_time, signal_counter)
                stats["dip_sent"] += 1
                icon = "💰" if result.get("funding_neg") else "🔵"

            send_telegram(msg)

            last_signal_ts.setdefault(symbol, {})[sig_type] = tr_time.replace(tzinfo=None)

            result["time"]     = tr_time.strftime("%Y-%m-%d %H:%M")
            result["sig_type"] = sig_type
            all_signals.insert(0, result)
            if len(all_signals) > 200:
                all_signals.pop()

            stats["signal_sent"] += 1
            log_signal(result, tr_time)

            subtype_str = result.get("subtype","") if sig_type=="trend" else ""
            print(
                f"SINYAL {icon} [{sig_type.upper()}{' '+subtype_str if subtype_str else ''}] "
                f"{symbol} | giriş:{fmt_price(result['entry'])}",
                flush=True
            )

        except Exception as e:
            print(f"Worker hata: {str(e)[:100]}", flush=True)
        finally:
            candidate_queue.task_done()

# ============================================================
# 10) PERFORMANS TAKİP
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
        "sig_type":    result.get("sig_type", "dip"),
        "subtype":     result.get("subtype", ""),
        "funding_neg": result.get("funding_neg", False),
        "time":        tr_time.isoformat(),
        "status":      "open",
        "peak_pct":    0.0,
        "close_time":  None,
        "close_price": None,
        "close_ret":   None,
    }
    signal_log.insert(0, entry)
    if len(signal_log) > 500:
        signal_log.pop()
    save_signal_log(signal_log)
    pending_by_symbol.setdefault(result["symbol"], []).append(entry)

def rebuild_pending():
    for s in signal_log:
        if s["status"] == "open":
            pending_by_symbol.setdefault(s["symbol"], []).append(s)

def check_pending_for_symbol(symbol, bar_high, bar_low, bar_close, bar_time):
    if symbol not in pending_by_symbol:
        return
    now      = datetime.now(timezone.utc)
    to_close = []

    for entry in pending_by_symbol[symbol]:
        e   = entry["entry"]
        stp = entry["stop"]

        sig_time = datetime.fromisoformat(entry["time"])
        if sig_time.tzinfo is None:
            sig_time = sig_time.replace(tzinfo=timezone.utc)
        elapsed_h = (now - sig_time).total_seconds() / 3600

        cur_ret = (bar_high - e) / e * 100
        if cur_ret > entry["peak_pct"]:
            entry["peak_pct"] = round(cur_ret, 2)

        if bar_low <= stp:
            entry["status"]      = "loss"
            entry["close_time"]  = bar_time.isoformat()
            entry["close_price"] = round(stp, 8)
            entry["close_ret"]   = round((stp - e) / e * 100, 2)
            to_close.append(entry)
            continue

        if elapsed_h >= 24:
            entry["status"]      = "expired"
            entry["close_time"]  = bar_time.isoformat()
            entry["close_price"] = round(bar_close, 8)
            entry["close_ret"]   = round((bar_close - e) / e * 100, 2)
            to_close.append(entry)

    if to_close:
        for e in to_close:
            pending_by_symbol[symbol].remove(e)
        if not pending_by_symbol[symbol]:
            del pending_by_symbol[symbol]
        save_signal_log(signal_log)

def perf_summary():
    closed      = [s for s in signal_log if s["status"] in ("loss","expired")]
    dip_closed  = [s for s in closed if s.get("sig_type","dip")=="dip"]
    trend_closed= [s for s in closed if s.get("sig_type","dip")=="trend"]
    fund_closed = [s for s in closed if s.get("funding_neg")]

    def avg_peak(lst):
        peaks = [s["peak_pct"] for s in lst if s.get("peak_pct") is not None]
        return round(sum(peaks)/len(peaks), 2) if peaks else 0.0

    return {
        "total":              len(signal_log),
        "open":               sum(1 for s in signal_log if s["status"]=="open"),
        "closed":             len(closed),
        "losses":             sum(1 for s in closed if s["status"]=="loss"),
        "expired":            sum(1 for s in closed if s["status"]=="expired"),
        "avg_peak":           avg_peak(closed),
        "dip_total":          len([s for s in signal_log if s.get("sig_type","dip")=="dip"]),
        "dip_avg_peak":       avg_peak(dip_closed),
        "trend_total":        len([s for s in signal_log if s.get("sig_type")=="trend"]),
        "trend_avg_peak":     avg_peak(trend_closed),
        "fund_neg_total":     len([s for s in signal_log if s.get("funding_neg")]),
        "fund_neg_avg_peak":  avg_peak(fund_closed),
    }

# ============================================================
# 11) MUM KAPANIŞINI İŞLE
# ============================================================
async def on_1h_close(symbol, o, h, l, c, v, ts_ms, candidate_queue):
    global ws_1h_closes
    ws_1h_closes += 1
    beat(symbol=symbol)

    bar_time = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    check_pending_for_symbol(symbol, h, l, c, bar_time)

    df = bars_1h.get(symbol)
    if df is None or len(df) < 60:
        stats["data_missing"] += 1
        return

    # Yeni mumu ekle
    tstamp = pd.to_datetime(ts_ms, unit="ms", utc=True)
    df.loc[tstamp, ["open","high","low","close","volume"]] = [o, h, l, c, v]
    df = df.sort_index()
    if len(df) > KEEP_BARS:
        df = df.iloc[-KEEP_BARS:]
    df = prepare_bars(df)
    bars_1h[symbol] = df

    tr_now = datetime.now(timezone.utc).astimezone(TR_TZ)
    sig_ts = last_signal_ts.get(symbol, {})

    # ── DİP sinyali kontrolü ─────────────────────────────────
    last_dip = sig_ts.get("dip")
    dip_ok   = True
    if last_dip:
        hrs = (tr_now.replace(tzinfo=None) - last_dip.replace(tzinfo=None)).total_seconds() / 3600
        if hrs < SIGNAL_COOLDOWN_HOURS:
            dip_ok = False
            stats["cooldown"] += 1

    if dip_ok:
        dip_result = check_dip_signal(df, symbol)
        if dip_result:
            await candidate_queue.put(SignalCandidate(
                symbol=symbol, result=dip_result,
                tr_time=tr_now, sig_type="dip"
            ))
        else:
            stats["filtered"] += 1

    # ── TREND sinyali kontrolü ───────────────────────────────
    last_trend = sig_ts.get("trend")
    trend_ok   = True
    if last_trend:
        hrs = (tr_now.replace(tzinfo=None) - last_trend.replace(tzinfo=None)).total_seconds() / 3600
        if hrs < SIGNAL_COOLDOWN_HOURS:
            trend_ok = False

    if trend_ok:
        trend_result = check_trend_signal(df, symbol)
        if trend_result:
            await candidate_queue.put(SignalCandidate(
                symbol=symbol, result=trend_result,
                tr_time=tr_now, sig_type="trend"
            ))

# ============================================================
# 12) WEBSOCKET
# ============================================================
def to_ws(symbol):
    return symbol.replace("/", "").lower()

async def ws_chunk(symbols, candidate_queue):
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
                print(f"WS baglandi ({len(symbols)} sembol)", flush=True)

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
                        if not k.get("x", False):
                            continue
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
        chunk = symbols[i:i + WS_STREAM_CHUNK]
        tasks.append(asyncio.create_task(ws_chunk(chunk, candidate_queue)))
    await asyncio.gather(*tasks)

# ============================================================
# 13) FLASK DASHBOARD
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
        heartbeat["last"]  = tr_now_str()
        heartbeat["epoch"] = now
        if symbol:
            heartbeat["symbol"] = symbol
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

@flask_app.route("/")
def home():
    now      = datetime.now(TR_TZ).strftime("%H:%M:%S")
    sig_rows = ""

    for s in all_signals[:30]:
        st       = s.get("sig_type","dip")
        subtype  = s.get("subtype","")
        fund_neg = s.get("funding_neg", False)

        if st == "trend":
            icon      = "⭐" if subtype == "VOL3_BB" else "📈"
            border_c  = "#00d4ff"
            type_label= f"TREND {subtype}"
        else:
            icon      = "💰" if fund_neg else "🔵"
            border_c  = "#c8e86a" if fund_neg else "#00f080"
            type_label= "DİP"

        fund_val  = s.get("funding")
        fund_str  = f"{fund_val:+.4f}%" if fund_val is not None else "—"

        # İndikatör satırı
        if st == "trend":
            ind_str = (f"EMA50:+{s.get('ema50_dist',0):.1f}%  "
                       f"EMA200:+{s.get('ema200_dist',0):.1f}%  "
                       f"ADX:{s.get('adx',0):.1f}  "
                       f"ATR:%{s.get('atr_ratio',0):.2f}")
        else:
            ind_str = (f"StRSI:{s.get('stoch_rsi',0):.4f}  "
                       f"WR:{s.get('wr',0):.1f}  "
                       f"OBV:{s.get('obv_osc',0):.1f}  "
                       f"WT:{s.get('wt',0):.1f}  "
                       f"Funding:{fund_str}")

        sig_rows += (
            f'<div class="sig" style="border-color:{border_c}">'
            f'<div class="sr">'
            f'<b>{icon} {s.get("symbol","")} <small style="color:#3d5a6a">[{type_label}]</small></b>'
            f'<span style="color:#3d5a6a;font-size:.65rem">{s.get("time","")[:16]}</span>'
            f'</div>'
            f'<div class="sd">💵 {fmt_price(s.get("entry"))}  🛡️ {fmt_price(s.get("stop"))}</div>'
            f'<div class="sd">{ind_str}</div>'
            f'</div>'
        )

    ps = perf_summary()
    return f"""<!DOCTYPE html>
<html><head>
<meta charset="UTF-8"><title>Scanner v5.0</title>
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
<h1>SCANNER <small style="font-size:.6rem;color:#3d5a6a">v5.0</small></h1>
<div class="params">
  <span class="badge" style="background:#0d1a0d;color:#00f080">🔵 DİP</span>
  StochRSI&lt;{STOCH_RSI_THRESH} WR&lt;{WR_THRESH} OBV&lt;{OBV_OSC_THRESH} WT&lt;{WT_THRESH} MACD≤0 Stop-%{STOP_PCT:.0f}<br>
  <span class="badge" style="background:#0d1520;color:#00d4ff">📈 TREND</span>
  BB(15) Kırılım + EMA200&gt;%12 + ADX&gt;20 + ALL4_LOOSE | Başarı ~%78 | Stop-%{TREND_STOP_PCT:.0f}<br>
  <span class="badge" style="background:#1a1a0d;color:#ffb300">⭐ VOL3_BB</span>
  Trend + 3bar hacim trend (daha kaliteli)
</div>
<div class="stats">
  <div class="stat"><span class="sv">{len(tracked_symbols)}</span><span class="sl">Sembol</span></div>
  <div class="stat"><span class="sv">{ws_1h_closes}</span><span class="sl">1H Kapanış</span></div>
  <div class="stat"><span class="sv">{stats.get("signal_sent",0)}</span><span class="sl">Toplam</span></div>
  <div class="stat"><span class="sv" style="color:#00f080">{stats.get("dip_sent",0)}</span><span class="sl">🔵 Dip</span></div>
  <div class="stat"><span class="sv" style="color:#00d4ff">{stats.get("trend_sent",0)}</span><span class="sl">📈 Trend</span></div>
  <div class="stat"><span class="sv">{bot_status["status"]}</span><span class="sl">Durum</span></div>
  <div class="stat"><span class="sv">{now}</span><span class="sl">Saat TR</span></div>
</div>
<h3>SON SİNYALLER</h3>
{sig_rows if sig_rows else '<p style="color:#3d5a6a;font-size:.8rem;padding:8px 0">Henüz sinyal yok.</p>'}
<div class="footer">
  Heartbeat: {heartbeat["last"]} | Son coin: {heartbeat["symbol"]}
  &nbsp;|&nbsp; <a href="/performance" style="color:#00d4ff">📈 Performans</a><br>
  Eleme: Cooldown:{stats.get("cooldown",0)} Hacim:{stats.get("low_liquidity",0)} Filtre:{stats.get("filtered",0)}
</div>
</body></html>"""

@flask_app.route("/api/status")
def api_status():
    data = clean_json({
        "status":        bot_status["status"],
        "total_symbols": len(tracked_symbols),
        "ws_1h_closes":  ws_1h_closes,
        "signals":       all_signals[:30],
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

@flask_app.route("/performance")
def perf_dashboard():
    ps   = perf_summary()
    rows = ""
    for s in signal_log[:50]:
        st     = s.get("status", "open")
        st_col = "#00f080" if st=="win" else ("#ff4444" if st=="loss" else ("#ffb300" if st=="expired" else "#3d5a6a"))
        stype  = s.get("sig_type","dip")
        sub    = s.get("subtype","")
        icon   = "⭐" if sub=="VOL3_BB" else ("📈" if stype=="trend" else ("💰" if s.get("funding_neg") else "🔵"))
        peak   = s.get("peak_pct", 0)
        cr     = s.get("close_ret")
        ct     = (s.get("close_time") or "")[:16]

        def fmt_ret(r):
            if r is None: return "—"
            col = "#00f080" if float(r)>0 else "#ff4444"
            return f'<span style="color:{col}">{float(r):+.2f}%</span>'

        rows += f"""<tr>
          <td>{s.get("time","")[:16]}</td>
          <td><b>{icon} {s.get("symbol","")}</b></td>
          <td style="color:{'#00d4ff' if stype=='trend' else '#00f080'}">{stype.upper()}{' '+sub if sub else ''}</td>
          <td>${s.get("entry","")}</td>
          <td style="color:#00f080">+{peak}%</td>
          <td>{fmt_ret(cr)}</td>
          <td>{ct}</td>
          <td style="color:{st_col}">{st.upper()}</td>
        </tr>"""

    return f"""<!DOCTYPE html><html lang="tr"><head>
<meta charset="UTF-8"><title>Performans v5</title>
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
<h1>📈 SİNYAL PERFORMANSI v5.0</h1>
<div class="sub"><a href="/">← Ana Sayfa</a> &nbsp;|&nbsp; {tr_now_str()}</div>
<div class="cards">
  <div class="card"><div class="cv">{ps.get("total",0)}</div><div class="cl">Toplam</div></div>
  <div class="card"><div class="cv">{ps.get("open",0)}</div><div class="cl">Açık</div></div>
  <div class="card"><div class="cv" style="color:#ff4444">{ps.get("losses",0)}</div><div class="cl">Stop</div></div>
  <div class="card"><div class="cv" style="color:#ffb300">{ps.get("expired",0)}</div><div class="cl">Expired</div></div>
  <div class="card"><div class="cv">{ps.get("avg_peak",0)}%</div><div class="cl">Ort. Peak</div></div>
  <div class="card"><div class="cv" style="color:#00f080">{ps.get("dip_total",0)}</div><div class="cl">🔵 Dip</div></div>
  <div class="card"><div class="cv" style="color:#00f080">{ps.get("dip_avg_peak",0)}%</div><div class="cl">Dip Peak</div></div>
  <div class="card"><div class="cv" style="color:#00d4ff">{ps.get("trend_total",0)}</div><div class="cl">📈 Trend</div></div>
  <div class="card"><div class="cv" style="color:#00d4ff">{ps.get("trend_avg_peak",0)}%</div><div class="cl">Trend Peak</div></div>
</div>
<table><thead><tr>
  <th>Sinyal Zamanı</th><th>Sembol</th><th>Tip</th><th>Giriş</th>
  <th>Peak %</th><th>Kapanış %</th><th>Kapanış Zamanı</th><th>Durum</th>
</tr></thead><tbody>{rows}</tbody></table>
</body></html>"""

# ============================================================
# 14) MAIN
# ============================================================
async def periodic_summary():
    while True:
        await asyncio.sleep(600)
        print_summary()

async def main():
    print("Scanner v5.0 baslatiliyor...", flush=True)
    print(f"DIP: StRSI<{STOCH_RSI_THRESH} WR<{WR_THRESH} OBV<{OBV_OSC_THRESH} WT<{WT_THRESH}", flush=True)
    print(f"TREND: BB(15) kirilim + ALL4_LOOSE | Basari ~%78", flush=True)

    symbols = await load_symbols_pool()
    if not symbols:
        print("Sembol yuklenemedi", flush=True)
        return

    global tracked_symbols
    tracked_symbols      = list(symbols)
    bot_status["status"] = "BOOTSTRAP"
    print(f"{len(symbols)} sembol yuklendi", flush=True)

    await bootstrap_all(symbols)
    await refresh_funding_cache(symbols)
    rebuild_pending()
    print(f"Pending sinyaller: {sum(len(v) for v in pending_by_symbol.values())}", flush=True)
    print_summary()

    candidate_queue = asyncio.Queue()
    asyncio.create_task(signal_worker(candidate_queue))
    asyncio.create_task(periodic_summary())

    bot_status["status"] = "LIVE"
    print(f"LIVE | {len(symbols)} sembol | Dip + Trend izleniyor", flush=True)

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
