# -*- coding: utf-8 -*-
"""
Trend & Momentum Scanner v4.0
==============================
Backtest v9 kanıtlı parametreler:
  StochRSI < 0.05
  Williams %R < -80
  OBV_OSC < -50
  WaveTrend < -75
  MACD histogram <= 0
  Stop: -%10  |  Cooldown: 4H

Funding Rate: hard filtre değil, öncelik etiketi
  Funding < 0 → 💰 (öncelikli sinyal)
  Funding ≥ 0 → 🔵 (normal sinyal)
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

# ── Backtest v9 kanıtlı eşikler ──────────────────────────────
STOCH_RSI_THRESH = float(os.getenv("STOCH_RSI_THRESH", "0.05"))
WR_THRESH        = float(os.getenv("WR_THRESH",        "-80"))
OBV_OSC_THRESH   = float(os.getenv("OBV_OSC_THRESH",   "-50"))
WT_THRESH        = float(os.getenv("WT_THRESH",        "-75"))
STOP_PCT         = float(os.getenv("STOP_PCT",         "10.0"))

SIGNAL_COOLDOWN_HOURS = int(os.getenv("SIGNAL_COOLDOWN_HOURS", "4"))
MIN_LIQUIDITY         = float(os.getenv("MIN_LIQUIDITY",       "500000"))
MAX_SYMBOLS           = int(os.getenv("MAX_SYMBOLS",           "0"))

WS_STREAM_CHUNK = int(os.getenv("WS_STREAM_CHUNK", "120"))
BOOTSTRAP_BARS  = int(os.getenv("BOOTSTRAP_BARS",  "200"))
KEEP_BARS       = int(os.getenv("KEEP_BARS",       "200"))

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
    symbol:  str
    result:  dict
    tr_time: datetime

bars_1h:          dict = {}
funding_cache:    dict = {}   # {symbol: funding_rate_float | None}
last_signal_ts:   dict = {}
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

    # Hacim sırala
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
    """Anlık funding rate'i çek, cache'e yaz."""
    sym_fut = symbol.replace("/USDT", "USDT")
    try:
        loop = asyncio.get_running_loop()
        data = await loop.run_in_executor(
            None,
            lambda: exchange_fut.fetch_funding_rate(sym_fut)
        )
        rate = data.get("fundingRate")
        funding_cache[symbol] = float(rate) if rate is not None else None
    except Exception:
        funding_cache[symbol] = None

async def refresh_funding_cache(symbols):
    """Bootstrap sırasında tüm futures semboller için funding rate çek."""
    print("  Funding rate cache dolduruluyor...", flush=True)
    ok = 0
    for sym in symbols:
        await fetch_funding_rate(sym)
        if funding_cache.get(sym) is not None:
            ok += 1
        await asyncio.sleep(0.05)
    print(f"  → {ok}/{len(symbols)} sembolde funding rate bulundu", flush=True)

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
    """
    Tüm indikatörleri hesapla:
    StochRSI, Williams %R, OBV_OSC, WaveTrend, MACD histogram
    """
    df = df.copy()
    c, h, l, v = df["close"], df["high"], df["low"], df["volume"]

    # ── Williams %R ──────────────────────────────────────────
    h14       = h.rolling(14).max()
    l14       = l.rolling(14).min()
    df["wr"]  = -100 * (h14 - c) / (h14 - l14).replace(0, np.nan)

    # ── RSI → StochRSI ───────────────────────────────────────
    d    = c.diff()
    gain = d.clip(lower=0).ewm(com=13, adjust=False).mean()
    loss = (-d).clip(lower=0).ewm(com=13, adjust=False).mean()
    rsi  = 100 - 100 / (1 + gain / loss.replace(0, np.nan))
    rsi_min       = rsi.rolling(14).min()
    rsi_max       = rsi.rolling(14).max()
    stoch_k       = (rsi - rsi_min) / (rsi_max - rsi_min).replace(0, np.nan)
    df["stoch_rsi"] = stoch_k.rolling(3).mean()   # %K smoothed

    # ── OBV Oscillator ───────────────────────────────────────
    obv          = (v * np.sign(c.diff()).fillna(0)).cumsum()
    obv_ma       = obv.rolling(20).mean()
    df["obv_osc"] = (obv - obv_ma) / obv_ma.abs().replace(0, np.nan) * 100

    # ── WaveTrend (LazyBear) ─────────────────────────────────
    ap    = (h + l + c) / 3
    esa   = ap.ewm(span=10, adjust=False).mean()
    d_abs = (ap - esa).abs().ewm(span=10, adjust=False).mean()
    ci    = (ap - esa) / (0.015 * d_abs.replace(0, np.nan))
    df["wt"] = ci.ewm(span=21, adjust=False).mean()

    # ── MACD Histogram ───────────────────────────────────────
    e12        = c.ewm(span=12, adjust=False).mean()
    e26        = c.ewm(span=26, adjust=False).mean()
    macd_line  = e12 - e26
    signal_line = macd_line.ewm(span=9, adjust=False).mean()
    df["macd_hist"] = macd_line - signal_line

    return df.dropna(subset=["stoch_rsi", "wr", "obv_osc", "wt", "macd_hist"])

# ============================================================
# 6) SİNYAL KRİTERLERİ
# ============================================================
def check_signal(df, symbol):
    """
    Son kapanan mumu kontrol et.
    Tüm kriterler sağlanırsa sinyal dict döndür, değilse None.
    """
    if len(df) < 60:
        return None

    last  = df.iloc[-2]   # son kapanan mum
    entry = float(df.iloc[-1]["close"])  # giriş: şu anki fiyat

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

    # ── Backtest v9 kriterleri ───────────────────────────────
    if stoch >= STOCH_RSI_THRESH: return None   # StochRSI < 0.05
    if wr    >= WR_THRESH:        return None   # WR < -80
    if obv   >= OBV_OSC_THRESH:   return None   # OBV_OSC < -50
    if wt    >= WT_THRESH:        return None   # WT < -75
    if hist  >  0:                return None   # MACD hist <= 0

    # Funding rate
    funding     = funding_cache.get(symbol)
    funding_neg = funding is not None and funding < 0

    stop = round(entry * (1 - STOP_PCT / 100), 8)

    return {
        "symbol":      symbol,
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

def build_tg_message(r, tr_time, sig_num):
    now         = tr_time.strftime("%d/%m/%Y %H:%M")
    sym         = r["symbol"].replace("/USDT", "")
    funding_neg = r.get("funding_neg", False)
    funding_val = r.get("funding")

    # Başlık ikonu
    icon = "💰" if funding_neg else "🔵"

    # Funding satırı
    if funding_val is not None:
        fund_str = f"{funding_val:+.4f}%"
        fund_line = f"Funding    {fund_str}{'  💰' if funding_neg else ''}"
    else:
        fund_line = None

    # MACD hist formatı
    hist_val = r.get("macd_hist", 0)
    if abs(hist_val) < 0.0001:
        hist_str = f"{hist_val:.6f}"
    elif abs(hist_val) < 0.01:
        hist_str = f"{hist_val:.5f}"
    else:
        hist_str = f"{hist_val:.4f}"

    lines = [
        f"🕐 {now}",
        "",
        f"{icon} <b>#{sym}USDT</b>  •  DİP DÖNÜŞÜ  •  1H",
        "━━━━━━━━━━━━━━━━━━━━",
        f"💵 Giriş:   {fmt_price(r['entry'])}",
        f"🛡️ Stop:    {fmt_price(r['stop'])}  (-%{STOP_PCT:.0f})",
        "━━━━━━━━━━━━━━━━━━━━",
        "📊 İndikatörler",
        f"StochRSI   {r['stoch_rsi']:.4f}",
        f"W%R        {r['wr']:.1f}",
        f"OBV_OSC    {r['obv_osc']:.1f}",
        f"WaveTrend  {r['wt']:.1f}",
        f"MACD Hist  {hist_str}",
    ]

    if fund_line:
        lines.append(fund_line)

    lines += [
        "━━━━━━━━━━━━━━━━━━━━",
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
            symbol  = sig.symbol
            result  = sig.result
            tr_time = sig.tr_time

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
            send_telegram(build_tg_message(result, tr_time, signal_counter))
            last_signal_ts[symbol] = tr_time.replace(tzinfo=None)

            all_signals.insert(0, result)
            if len(all_signals) > 200:
                all_signals.pop()

            stats["signal_sent"] += 1
            log_signal(result, tr_time)

            fund_str = f"{result['funding']:+.4f}%" if result["funding"] is not None else "—"
            print(
                f"SINYAL {'💰' if result['funding_neg'] else '🔵'} {symbol} | "
                f"StRSI:{result['stoch_rsi']:.4f} "
                f"WR:{result['wr']:.1f} "
                f"OBV:{result['obv_osc']:.1f} "
                f"WT:{result['wt']:.1f} "
                f"Funding:{fund_str}",
                flush=True
            )

        except Exception as e:
            print(f"Worker hata: {str(e)[:100]}", flush=True)
        finally:
            candidate_queue.task_done()

# ============================================================
# 10) SİNYAL PERFORMANS TAKİP
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

signal_log    = load_signal_log()
pending_by_symbol = {}

def log_signal(result, tr_time):
    entry = {
        "id":          f"{result['symbol']}_{int(tr_time.timestamp())}",
        "symbol":      result["symbol"],
        "entry":       result["entry"],
        "stop":        result["stop"],
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

        # Peak güncelle
        cur_ret = (bar_high - e) / e * 100
        if cur_ret > entry["peak_pct"]:
            entry["peak_pct"] = round(cur_ret, 2)

        # Stop kontrolü (önce stop — konservatif)
        if bar_low <= stp:
            entry["status"]      = "loss"
            entry["close_time"]  = bar_time.isoformat()
            entry["close_price"] = round(stp, 8)
            entry["close_ret"]   = round((stp - e) / e * 100, 2)
            to_close.append(entry)
            continue

        # 24H geçti — expired
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
    closed  = [s for s in signal_log if s["status"] in ("loss","expired")]
    # Funding negatif alt grubu
    fund_closed = [s for s in closed if s.get("funding_neg")]
    wins    = sum(1 for s in signal_log if s["status"] == "win")
    losses  = sum(1 for s in closed if s["status"] == "loss")
    expired = sum(1 for s in closed if s["status"] == "expired")
    total_c = len(closed)
    alive   = sum(1 for s in signal_log if s["status"] == "open")

    def avg_peak(lst):
        peaks = [s["peak_pct"] for s in lst if s.get("peak_pct") is not None]
        return round(sum(peaks)/len(peaks), 2) if peaks else 0.0

    return {
        "total":          len(signal_log),
        "open":           alive,
        "closed":         total_c,
        "losses":         losses,
        "expired":        expired,
        "avg_peak":       avg_peak(closed),
        "fund_neg_total": len([s for s in signal_log if s.get("funding_neg")]),
        "fund_neg_avg_peak": avg_peak(fund_closed),
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

    # Cooldown kontrolü
    last_ts = last_signal_ts.get(symbol)
    if last_ts:
        hours = (tr_now.replace(tzinfo=None) - last_ts.replace(tzinfo=None)).total_seconds() / 3600
        if hours < SIGNAL_COOLDOWN_HOURS:
            stats["cooldown"] += 1
            return

    # Sinyal kontrolü
    result = check_signal(df, symbol)
    if result is None:
        stats["filtered"] += 1
        return

    await candidate_queue.put(SignalCandidate(
        symbol=symbol, result=result, tr_time=tr_now,
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
        fund_neg = s.get("funding_neg", False)
        icon     = "💰" if fund_neg else "🔵"
        fund_val = s.get("funding")
        fund_str = f"{fund_val:+.4f}%" if fund_val is not None else "—"
        fund_icon = "  💰" if fund_neg else ""

        sig_rows += (
            f'<div class="sig" style="border-color:{"#c8e86a" if fund_neg else "#00f080"}">'
            f'<div class="sr">'
            f'<b>{icon} {s.get("symbol","")}</b>'
            f'<span style="color:#3d5a6a;font-size:.65rem">{s.get("time","")[:16]} UTC</span>'
            f'</div>'
            f'<div class="sd">'
            f'💵 {fmt_price(s.get("entry"))}  '
            f'🛡️ {fmt_price(s.get("stop"))} (-%{STOP_PCT:.0f})'
            f'</div>'
            f'<div class="sd">'
            f'StRSI:{s.get("stoch_rsi",""):.4f}  '
            f'WR:{s.get("wr",""):.1f}  '
            f'OBV:{s.get("obv_osc",""):.1f}  '
            f'WT:{s.get("wt",""):.1f}  '
            f'Funding:{fund_str}{fund_icon}'
            f'</div>'
            f'</div>'
        )

    ps = perf_summary()
    return f"""<!DOCTYPE html>
<html><head>
<meta charset="UTF-8"><title>Scanner v4.0</title>
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
</style></head><body>
<h1>SCANNER <small style="font-size:.6rem;color:#3d5a6a">v4.0 | Backtest v9</small></h1>
<div class="params">
  StochRSI &lt; {STOCH_RSI_THRESH} &nbsp;|&nbsp;
  W%R &lt; {WR_THRESH} &nbsp;|&nbsp;
  OBV_OSC &lt; {OBV_OSC_THRESH} &nbsp;|&nbsp;
  WT &lt; {WT_THRESH} &nbsp;|&nbsp;
  MACD ≤ 0 &nbsp;|&nbsp;
  Stop -%{STOP_PCT:.0f} &nbsp;|&nbsp;
  Cooldown {SIGNAL_COOLDOWN_HOURS}H &nbsp;|&nbsp;
  💰 = Funding &lt; 0
</div>
<div class="stats">
  <div class="stat"><span class="sv">{len(tracked_symbols)}</span><span class="sl">Sembol</span></div>
  <div class="stat"><span class="sv">{ws_1h_closes}</span><span class="sl">1H Kapanış</span></div>
  <div class="stat"><span class="sv">{stats.get("signal_sent",0)}</span><span class="sl">Sinyal</span></div>
  <div class="stat"><span class="sv">{ps.get("fund_neg_total",0)}</span><span class="sl">💰 Öncelikli</span></div>
  <div class="stat"><span class="sv">{ps.get("open",0)}</span><span class="sl">Açık</span></div>
  <div class="stat"><span class="sv">{bot_status["status"]}</span><span class="sl">Durum</span></div>
  <div class="stat"><span class="sv">{now}</span><span class="sl">Saat TR</span></div>
</div>
<h3>SON SİNYALLER</h3>
{sig_rows if sig_rows else '<p style="color:#3d5a6a;font-size:.8rem;padding:8px 0">Henüz sinyal yok.</p>'}
<div class="footer">
  Heartbeat: {heartbeat["last"]} &nbsp;|&nbsp; Son coin: {heartbeat["symbol"]}
  &nbsp;|&nbsp; <a href="/performance" style="color:#00d4ff">📈 Performans</a><br>
  Eleme: Cooldown:{stats.get("cooldown",0)}
  Hacim:{stats.get("low_liquidity",0)}
  Filtre:{stats.get("filtered",0)}
  Veri:{stats.get("data_missing",0)}
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
        "params": {
            "stoch_rsi": STOCH_RSI_THRESH,
            "wr":        WR_THRESH,
            "obv_osc":   OBV_OSC_THRESH,
            "wt":        WT_THRESH,
            "stop_pct":  STOP_PCT,
            "cooldown_h": SIGNAL_COOLDOWN_HOURS,
        }
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
        st_col = "#00f080" if st == "win" else ("#ff4444" if st == "loss" else ("#ffb300" if st == "expired" else "#3d5a6a"))
        fund_icon = "💰" if s.get("funding_neg") else "🔵"
        peak   = s.get("peak_pct", 0)
        cr     = s.get("close_ret")
        ct     = (s.get("close_time") or "")[:16]

        def fmt_ret(r):
            if r is None: return "—"
            col = "#00f080" if float(r) > 0 else "#ff4444"
            return f'<span style="color:{col}">{float(r):+.2f}%</span>'

        rows += f"""<tr>
          <td>{s.get("time","")[:16]}</td>
          <td><b>{fund_icon} {s.get("symbol","")}</b></td>
          <td>${s.get("entry","")}</td>
          <td style="color:#00f080">+{peak}%</td>
          <td>{fmt_ret(cr)}</td>
          <td>{ct}</td>
          <td style="color:{st_col}">{st.upper()}</td>
        </tr>"""

    return f"""<!DOCTYPE html><html lang="tr"><head>
<meta charset="UTF-8"><title>Performans</title>
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
<h1>📈 SİNYAL PERFORMANSI</h1>
<div class="sub"><a href="/">← Ana Sayfa</a> &nbsp;|&nbsp; {tr_now_str()}</div>
<div class="cards">
  <div class="card"><div class="cv">{ps.get("total",0)}</div><div class="cl">Toplam</div></div>
  <div class="card"><div class="cv">{ps.get("open",0)}</div><div class="cl">Açık</div></div>
  <div class="card"><div class="cv" style="color:#ff4444">{ps.get("losses",0)}</div><div class="cl">Stop Yedi</div></div>
  <div class="card"><div class="cv" style="color:#ffb300">{ps.get("expired",0)}</div><div class="cl">24H Expired</div></div>
  <div class="card"><div class="cv">{ps.get("avg_peak",0)}%</div><div class="cl">Ort. Peak</div></div>
  <div class="card"><div class="cv" style="color:#c8e86a">{ps.get("fund_neg_total",0)}</div><div class="cl">💰 Öncelikli</div></div>
  <div class="card"><div class="cv" style="color:#c8e86a">{ps.get("fund_neg_avg_peak",0)}%</div><div class="cl">💰 Peak Ort.</div></div>
</div>
<table><thead><tr>
  <th>Sinyal Zamanı</th><th>Sembol</th><th>Giriş</th>
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
    print("Scanner v4.0 baslatiliyor...", flush=True)
    print(f"Parametreler: StRSI<{STOCH_RSI_THRESH} WR<{WR_THRESH} OBV<{OBV_OSC_THRESH} WT<{WT_THRESH} Stop-%{STOP_PCT}", flush=True)

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
    print(f"LIVE | {len(symbols)} sembol izleniyor", flush=True)

    await ws_all(symbols, candidate_queue)

def start_flask():
    port = int(os.environ.get("PORT", "10000"))
    flask_app.run(host="0.0.0.0", port=port, use_reloader=False)

if __name__ == "__main__":
    threading.Thread(target=start_flask,       daemon=True).start()
    threading.Thread(target=heartbeat_pinger,  daemon=True).start()
    threading.Thread(target=watchdog_thread,   daemon=True).start()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Durduruldu", flush=True)
