# -*- coding: utf-8 -*-
"""
Oversold Scanner v2.0
Squeeze Detector mimarisini temel alır.
WebSocket tabanlı — her mum kapanisinda tetiklenir.
RSI / Williams %R / MACD / StochRSI / ATR
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

RSI_OVERSOLD      = float(os.getenv("RSI_OVERSOLD",      "32"))
WILLIAMS_OVERSOLD = float(os.getenv("WILLIAMS_OVERSOLD", "-80"))
DROP_PCT_THRESH   = float(os.getenv("DROP_PCT",          "4"))
STOCHRSI_THRESH   = float(os.getenv("STOCHRSI_THRESH",  "25"))
MIN_SCORE         = int(os.getenv("MIN_SCORE",           "4"))

ATR_STOP_MULT   = float(os.getenv("ATR_STOP_MULT",   "1.5"))
ATR_TARGET_MULT = float(os.getenv("ATR_TARGET_MULT", "2.5"))

SIGNAL_COOLDOWN_HOURS = int(os.getenv("SIGNAL_COOLDOWN_HOURS", "4"))
MIN_LIQUIDITY         = float(os.getenv("MIN_LIQUIDITY",        "5000000"))
MAX_SYMBOLS           = int(os.getenv("MAX_SYMBOLS",            "0"))

WS_INTERVAL     = os.getenv("WS_INTERVAL",    "1h")
WS_STREAM_CHUNK = int(os.getenv("WS_STREAM_CHUNK", "120"))
BOOTSTRAP_LIMIT = int(os.getenv("BOOTSTRAP_LIMIT", "200"))
KEEP_BARS       = int(os.getenv("KEEP_BARS",        "200"))

TR_TZ = timezone(timedelta(hours=3))

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
BOOT_PROGRESS_EVERY = 50
stats           = Counter()
ws_close_count  = 0
tracked_symbols = []

def tr_now_str():
    return datetime.now(timezone.utc).astimezone(TR_TZ).strftime("%Y-%m-%d %H:%M:%S")

def print_summary():
    closes = max(ws_close_count, 1)
    print("\n TARAMA OZETI", flush=True)
    print("--------------------", flush=True)
    print(f"Takip edilen  : {len(tracked_symbols)}", flush=True)
    print(f"Mum kapanis   : {ws_close_count}", flush=True)
    print(f"Sinyal        : {stats.get('signal_sent', 0)}", flush=True)
    for k, label in [
        ("score_low",     "Skor yetersiz"),
        ("cooldown",      "Cooldown"),
        ("low_liquidity", "Dusuk hacim"),
        ("data_missing",  "Veri yok"),
    ]:
        v = stats.get(k, 0)
        if v:
            print(f"  {label:20s}: {v} ({v/closes*100:.1f}%)", flush=True)
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

    async def _sleep_for_spacing(self):
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
                    await self._sleep_for_spacing()
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

bars:             dict = {}
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
# 4) VERİ + İNDİKATOR HAZIRLIK
# ============================================================
async def fetch_ohlcv_df(symbol, timeframe, limit):
    raw = await api_gate.call(exchange.fetch_ohlcv, symbol, timeframe=timeframe, limit=limit)
    df  = pd.DataFrame(raw, columns=["timestamp","open","high","low","close","volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df.set_index("timestamp", inplace=True)
    return df

def prepare_indicators(df):
    df = df.copy()

    df["ema20"] = ta.ema(df["close"], length=20)
    df["ema50"] = ta.ema(df["close"], length=50)
    df["atr"]   = ta.atr(df["high"], df["low"], df["close"], length=14)
    df["rsi"]   = ta.rsi(df["close"], length=14)

    # Williams %R
    h14 = df["high"].rolling(14)
    l14 = df["low"].rolling(14)
    df["williams_r"] = -100 * (h14.max() - df["close"]) / (h14.max() - l14.min())

    # MACD histogram
    macd_df = ta.macd(df["close"], fast=12, slow=26, signal=9)
    if macd_df is not None:
        cols = macd_df.columns.tolist()
        hist_col = next((c for c in cols if "MACDh" in c), None)
        if hist_col is None:
            hist_col = next((c for c in cols if "h" in c.lower()), None)
        df["macd_hist"] = macd_df[hist_col] if hist_col else np.nan
    else:
        df["macd_hist"] = np.nan

    # StochRSI K
    srsi_df = ta.stochrsi(df["close"], length=14, rsi_length=14, k=3, d=3)
    if srsi_df is not None:
        cols  = srsi_df.columns.tolist()
        k_col = next((c for c in cols if c.lower().endswith("k")), None)
        df["stochrsi_k"] = srsi_df[k_col] if k_col else np.nan
    else:
        df["stochrsi_k"] = np.nan

    df["vol_ma"] = df["volume"].rolling(20, min_periods=1).mean()
    return df

# ============================================================
# 5) OVERSOLD TESPİTİ
# ============================================================
def detect_oversold(df):
    try:
        if len(df) < 60:
            return {"detected": False, "reason": "Veri yetersiz"}

        last = df.iloc[-2]  # son kapanan mum

        def safe(col):
            v = last.get(col, np.nan)
            return None if pd.isna(v) else float(v)

        rsi   = safe("rsi")
        wr    = safe("williams_r")
        srsi  = safe("stochrsi_k")
        mhist = safe("macd_hist")

        if rsi is None or wr is None or srsi is None:
            return {"detected": False, "reason": "Indiktor hesaplanamadi"}

        open_p  = float(last["open"])
        close_p = float(last["close"])
        drop_pct = round((open_p - close_p) / open_p * 100, 2) if open_p > 0 else 0.0

        conditions = {
            "drop_candle":       drop_pct >= DROP_PCT_THRESH,
            "rsi_oversold":      rsi      <= RSI_OVERSOLD,
            "williams_oversold": wr       <= WILLIAMS_OVERSOLD,
            "macd_neg":          mhist is not None and mhist < 0,
            "stochrsi_low":      srsi     <= STOCHRSI_THRESH,
        }
        score = sum(conditions.values())

        if score < MIN_SCORE:
            return {"detected": False, "reason": f"Skor dusuk ({score})"}

        return {
            "detected":   True,
            "conditions": conditions,
            "score":      score,
            "drop_pct":   drop_pct,
            "rsi":        round(rsi,   2),
            "williams_r": round(wr,    2),
            "macd_hist":  round(mhist, 8) if mhist is not None else None,
            "stochrsi":   round(srsi,  2),
        }

    except Exception as e:
        return {"detected": False, "reason": str(e)[:60]}

# ============================================================
# 6) PUAN (100 uzerinden)
# ============================================================
def calc_score100(rsi, wr, mhist, srsi, drop_pct):
    s = 0.0

    if   rsi <= 20:           s += 25
    elif rsi <= 25:           s += 20
    elif rsi <= 30:           s += 15
    elif rsi <= RSI_OVERSOLD: s += 8

    if   wr <= -90:           s += 25
    elif wr <= -85:           s += 20
    elif wr <= -80:           s += 15
    elif wr <= -70:           s += 8

    if   srsi <= 5:                 s += 20
    elif srsi <= 10:                s += 16
    elif srsi <= 20:                s += 12
    elif srsi <= STOCHRSI_THRESH:   s += 6

    if   drop_pct >= 8: s += 20
    elif drop_pct >= 6: s += 16
    elif drop_pct >= 4: s += 10
    elif drop_pct >= 2: s += 5

    if mhist is not None and mhist < 0:
        s += 10

    return min(int(round(s)), 100)

# ============================================================
# 7) TAM ANALİZ
# ============================================================
def analyze_symbol(symbol, df):
    try:
        oversold = detect_oversold(df)
        if not oversold["detected"]:
            return None

        last  = df.iloc[-2]
        curr  = df.iloc[-1]
        entry = float(curr["close"])

        def sf(col):
            v = last.get(col, np.nan)
            return float(v) if not pd.isna(v) else entry

        atr  = sf("atr")
        ma20 = sf("ema20")
        ma50 = sf("ema50")

        target = round(entry + atr * ATR_TARGET_MULT, 8)
        stop   = round(entry - atr * ATR_STOP_MULT,   8)

        target_pct = round((target - entry) / entry * 100, 2) if entry else 0
        stop_pct   = round((entry - stop)   / entry * 100, 2) if entry else 0

        score100 = calc_score100(
            oversold["rsi"],
            oversold["williams_r"],
            oversold.get("macd_hist"),
            oversold["stochrsi"],
            oversold["drop_pct"],
        )

        return {
            "symbol":      symbol,
            "time":        datetime.now(timezone.utc).isoformat(),
            "candle_time": str(df.index[-2]),
            "price":       round(entry, 8),
            "target":      target,
            "stop":        stop,
            "target_pct":  target_pct,
            "stop_pct":    stop_pct,
            "drop_pct":    oversold["drop_pct"],
            "rsi":         oversold["rsi"],
            "williams_r":  oversold["williams_r"],
            "macd_hist":   oversold.get("macd_hist"),
            "stochrsi":    oversold["stochrsi"],
            "ma20":        round(ma20, 8),
            "ma50":        round(ma50, 8),
            "atr":         round(atr,  8),
            "conditions":  oversold["conditions"],
            "score":       oversold["score"],
            "score100":    score100,
            "signal":      True,
        }

    except Exception as e:
        print(f"Analiz hatasi {symbol}: {str(e)[:80]}", flush=True)
        return None

# ============================================================
# 8) TELEGRAM
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
    if price is None:
        return "?"
    p = float(price)
    if p >= 100:   return f"{p:.2f}"
    if p >= 1:     return f"{p:.3f}"
    if p >= 0.01:  return f"{p:.4f}"
    return f"{p:.6f}"

def build_tg_message(r, tr_time):
    conds     = r.get("conditions", {})
    label_map = {
        "drop_candle":       "Sert Dusus",
        "rsi_oversold":      "RSI Oversold",
        "williams_oversold": "Williams %R Oversold",
        "macd_neg":          "MACD Negatif",
        "stochrsi_low":      "StochRSI Dusuk",
    }
    met  = [v for k, v in label_map.items() if conds.get(k)]
    miss = [v for k, v in label_map.items() if not conds.get(k)]
    sym  = r["symbol"].replace("/", "").replace("USDT", "")
    now  = tr_time.strftime("%d/%m/%Y %H:%M")

    return (
        f"🕐 {now}\n\n"
        f"#{sym}/USDT\n"
        f"💵 Giris:  {fmt_price(r.get('price'))}\n"
        f"🎯 Hedef:  {fmt_price(r.get('target'))} (+%{r.get('target_pct')})\n"
        f"🛡️ Stop:   {fmt_price(r.get('stop'))} (-%{r.get('stop_pct')})\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"📈 Indiktorler:\n"
        f"RSI(14) → {r.get('rsi')}\n"
        f"Williams %R → {r.get('williams_r')}\n"
        f"MACD Hist → {r.get('macd_hist')}\n"
        f"StochRSI K → {r.get('stochrsi')}\n"
        f"Son mum → %{r.get('drop_pct')} dusus\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"✅ {', '.join(met) if met else '—'}\n"
        f"❌ {', '.join(miss) if miss else '—'}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"Puan: {r.get('score100')}/100"
    )

# ============================================================
# 9) BOOTSTRAP
# ============================================================
async def bootstrap_symbol(symbol):
    try:
        df = await fetch_ohlcv_df(symbol, WS_INTERVAL, BOOTSTRAP_LIMIT)
        if df is None or len(df) < 60:
            return False
        df = prepare_indicators(df)
        if len(df) > KEEP_BARS:
            df = df.iloc[-KEEP_BARS:]
        bars[symbol] = df
        return True
    except Exception:
        return False

async def bootstrap_all(symbols):
    ok = 0
    print(f"Bootstrap basladi | {len(symbols)} sembol", flush=True)
    for i, sym in enumerate(symbols, 1):
        if i % BOOT_PROGRESS_EVERY == 0:
            print(f"  -> {i}/{len(symbols)}", flush=True)
        if await bootstrap_symbol(sym):
            ok += 1
    print(f"Bootstrap bitti | {ok}/{len(symbols)}", flush=True)

# ============================================================
# 10) MUM KAPANISINI ISLE
# ============================================================
def safe_float(val):
    try:
        v = float(val)
        return None if (v != v or v in (float("inf"), float("-inf"))) else round(v, 4)
    except Exception:
        return None

async def on_candle_close(symbol, o, h, l, c, v, ts_ms, candidate_queue):
    global ws_close_count
    ws_close_count += 1
    beat(symbol=symbol, status="LIVE")

    df = bars.get(symbol)
    if df is None or len(df) < 60:
        stats["data_missing"] += 1
        return

    # Yeni mumu ekle
    tstamp = pd.to_datetime(ts_ms, unit="ms", utc=True)
    df.loc[tstamp, ["open","high","low","close","volume"]] = [o, h, l, c, v]
    df = df.sort_index()
    if len(df) > KEEP_BARS:
        df = df.iloc[-KEEP_BARS:]
    df = prepare_indicators(df)
    bars[symbol] = df

    tr_now = datetime.now(timezone.utc).astimezone(TR_TZ)

    # Cooldown
    last_ts = last_signal_ts.get(symbol)
    if last_ts:
        hours = (tr_now.replace(tzinfo=None) - last_ts.replace(tzinfo=None)).total_seconds() / 3600
        if hours < SIGNAL_COOLDOWN_HOURS:
            stats["cooldown"] += 1
            return

    result = analyze_symbol(symbol, df)

    # Dashboard icin her zaman guncelle
    prev = df.iloc[-2]
    last_scan_result[symbol] = {
        "symbol":     symbol,
        "time":       tr_now.isoformat(),
        "price":      round(float(df.iloc[-1]["close"]), 8),
        "rsi":        safe_float(prev.get("rsi")),
        "williams_r": safe_float(prev.get("williams_r")),
        "macd_hist":  safe_float(prev.get("macd_hist")),
        "stochrsi":   safe_float(prev.get("stochrsi_k")),
        "signal":     result is not None,
        "score":      result["score"]    if result else 0,
        "score100":   result["score100"] if result else 0,
        "conditions": result["conditions"] if result else {},
    }

    if result is None:
        stats["score_low"] += 1
        return

    await candidate_queue.put(SignalCandidate(
        symbol=symbol, result=result, tr_time=tr_now,
    ))

# ============================================================
# 11) SİNYAL WORKER
# ============================================================
async def signal_worker(candidate_queue):
    while True:
        sig = await candidate_queue.get()
        try:
            symbol  = sig.symbol
            result  = sig.result
            tr_time = sig.tr_time

            # Hacim kontrolu
            try:
                ticker    = await api_gate.call(exchange.fetch_ticker, symbol)
                liquidity = float(ticker.get("quoteVolume", 0) or 0)
                if liquidity < MIN_LIQUIDITY:
                    stats["low_liquidity"] += 1
                    continue
            except Exception:
                pass

            msg = build_tg_message(result, tr_time)
            send_telegram(msg)

            last_signal_ts[symbol] = tr_time.replace(tzinfo=None)
            all_signals.insert(0, result)
            if len(all_signals) > 200:
                all_signals.pop()

            stats["signal_sent"] += 1

            print(
                f"SINYAL: {symbol} | Puan:{result['score100']}/100 | "
                f"RSI:{result['rsi']} | W%R:{result['williams_r']} | Dusus:%{result['drop_pct']}",
                flush=True
            )

        except Exception as e:
            print(f"Worker hata: {str(e)[:100]}", flush=True)
        finally:
            candidate_queue.task_done()

# ============================================================
# 12) WEBSOCKET
# ============================================================
def to_ws_sym(symbol):
    return symbol.replace("/", "").lower()

async def ws_chunk(symbols, candidate_queue):
    streams = "/".join([f"{to_ws_sym(s)}@kline_{WS_INTERVAL}" for s in symbols])
    url     = f"wss://stream.binance.com:9443/stream?streams={streams}"
    retry   = 0

    while True:
        try:
            async with websockets.connect(url, ping_interval=30, ping_timeout=30) as ws:
                retry = 0
                print(f"WS baglandi ({len(symbols)} sembol)", flush=True)
                while True:
                    msg  = await ws.recv()
                    data = json.loads(msg)
                    k    = data.get("data", {}).get("k", {})

                    if not k.get("x", False):
                        continue

                    raw_sym = data.get("data", {}).get("s", "")
                    symbol  = raw_sym.upper().replace("USDT", "/USDT")

                    await on_candle_close(
                        symbol,
                        float(k["o"]), float(k["h"]),
                        float(k["l"]), float(k["c"]),
                        float(k["v"]), int(k["t"]),
                        candidate_queue,
                    )

        except Exception as e:
            retry  += 1
            backoff = min(60, 5 * (2 ** min(retry, 4)))
            print(f"WS koptu -> {backoff}s sonra yeniden: {str(e)[:60]}", flush=True)
            await asyncio.sleep(backoff)

async def ws_all(symbols, candidate_queue):
    tasks = [
        asyncio.create_task(ws_chunk(symbols[i:i + WS_STREAM_CHUNK], candidate_queue))
        for i in range(0, len(symbols), WS_STREAM_CHUNK)
    ]
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
            print(f"WATCHDOG: {int(stale)}s stale — yeniden baslatiliyor", flush=True)
            os._exit(1)
        time.sleep(10)

def clean_json(obj):
    if isinstance(obj, dict):
        return {k: clean_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [clean_json(i) for i in obj]
    if isinstance(obj, float):
        if obj != obj or obj in (float("inf"), float("-inf")):
            return None
        return obj
    if hasattr(obj, "item"):
        return clean_json(obj.item())
    return obj

def score_color(s):
    if s >= 80: return "#00f080"
    if s >= 60: return "#ffb300"
    return "#ff3a5c"

@flask_app.route("/")
def home():
    now = datetime.now(TR_TZ).strftime("%H:%M:%S")
    sig_rows = ""
    for s in all_signals[:20]:
        c   = s.get("conditions", {})
        met = sum(c.values())
        sc  = s.get("score100", 0)
        col = score_color(sc)
        sig_rows += (
            f'<div class="sig">'
            f'<div class="sr"><b style="font-size:1rem">{s.get("symbol","")}</b>'
            f'<span style="color:{col};font-weight:bold;font-size:1rem">{sc}/100</span></div>'
            f'<div class="sd">'
            f'<span style="color:#00d4ff">${fmt_price(s.get("price"))}</span>'
            f' &nbsp;|&nbsp; RSI {s.get("rsi","")} &nbsp;|&nbsp; W%R {s.get("williams_r","")}'
            f' &nbsp;|&nbsp; StochRSI {s.get("stochrsi","")}'
            f'</div>'
            f'<div class="sd">'
            f'🎯 Hedef: {fmt_price(s.get("target"))} (+%{s.get("target_pct","")})'
            f' &nbsp;&nbsp; 🛡️ Stop: {fmt_price(s.get("stop"))} (-%{s.get("stop_pct","")})'
            f'</div>'
            f'<div class="sd" style="color:#3d5a6a">{s.get("time","")[:16]} UTC &nbsp;|&nbsp; {met}/5 kosul</div>'
            f'</div>'
        )

    return f"""<!DOCTYPE html>
<html><head>
<meta charset="UTF-8"><title>Oversold Scanner</title>
<meta http-equiv="refresh" content="30">
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#06090d;color:#b8cdd8;font-family:'Courier New',monospace;padding:20px;max-width:1000px;margin:0 auto}}
h1{{color:#00d4ff;letter-spacing:4px;font-size:1.3rem;margin-bottom:18px}}
.stats{{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:22px}}
.stat{{background:#0c1117;border:1px solid #1c2a36;padding:10px 16px;border-radius:4px;min-width:90px}}
.sv{{font-size:1.2rem;color:#00d4ff;display:block;font-weight:bold}}
.sl{{font-size:.58rem;color:#3d5a6a;text-transform:uppercase;letter-spacing:1px}}
h3{{color:#00f080;margin:0 0 10px;font-size:.8rem;letter-spacing:2px;text-transform:uppercase}}
.sig{{background:#031409;border-left:3px solid #00f080;padding:12px 16px;margin:6px 0;border-radius:2px}}
.sr{{display:flex;justify-content:space-between;margin-bottom:5px}}
.sd{{font-size:.75rem;margin:3px 0;color:#8aa8b8}}
.footer{{color:#3d5a6a;font-size:.62rem;margin-top:24px;border-top:1px solid #1c2a36;padding-top:12px;line-height:1.8}}
</style></head><body>
<h1>OVERSOLD SCANNER</h1>
<div class="stats">
  <div class="stat"><span class="sv">{len(tracked_symbols)}</span><span class="sl">Sembol</span></div>
  <div class="stat"><span class="sv">{ws_close_count}</span><span class="sl">Mum Kapanis</span></div>
  <div class="stat"><span class="sv">{stats.get("signal_sent",0)}</span><span class="sl">Sinyal</span></div>
  <div class="stat"><span class="sv">{WS_INTERVAL}</span><span class="sl">Periyot</span></div>
  <div class="stat"><span class="sv">{bot_status["status"]}</span><span class="sl">Durum</span></div>
  <div class="stat"><span class="sv">{now}</span><span class="sl">Saat TR</span></div>
</div>
<h3>Son Sinyaller</h3>
{sig_rows if sig_rows else '<p style="color:#3d5a6a;font-size:.8rem;padding:10px 0">Henuz sinyal yok.</p>'}
<div class="footer">
  Heartbeat: {heartbeat["last"]} &nbsp;|&nbsp; Son coin: {heartbeat["symbol"]}<br>
  Eleme → Skor: {stats.get("score_low",0)} &nbsp;|&nbsp;
  Cooldown: {stats.get("cooldown",0)} &nbsp;|&nbsp;
  Dusuk hacim: {stats.get("low_liquidity",0)} &nbsp;|&nbsp;
  Veri yok: {stats.get("data_missing",0)}
</div>
</body></html>"""

@flask_app.route("/api/status")
def api_status():
    data = clean_json({
        "status":        bot_status["status"],
        "total_symbols": len(tracked_symbols),
        "ws_closes":     ws_close_count,
        "signals":       all_signals[:30],
        "last_scan":     dict(last_scan_result),
        "stats":         dict(stats),
        "heartbeat":     heartbeat,
        "interval":      WS_INTERVAL,
    })
    return flask_app.response_class(
        json.dumps(data, ensure_ascii=False),
        mimetype="application/json",
    )

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
    print("Oversold Scanner v2.0 baslatiliyor...", flush=True)

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
    print(f"WebSocket canli | {len(symbols)} sembol | {WS_INTERVAL}", flush=True)

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
