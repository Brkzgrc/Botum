# -*- coding: utf-8 -*-
"""
Professional Accumulation Detector v5.0
Gerçek birikim tespiti - Patlama öncesi sinyal
"""

import asyncio
import json
import time
import threading
import os
from collections import Counter, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import numpy as np
import pandas as pd
import pandas_ta as ta
import requests
import ccxt
import websockets
from flask import Flask
from scipy.stats import linregress

# ============================================================
# AYARLAR
# ============================================================
BINANCE_API_KEY = ""
BINANCE_API_SECRET = ""

TELEGRAM_TOKEN = "7583261338:AAFwkpxsumCBpYI5Ai-aIiII6INm_thmg-I"
TELEGRAM_CHAT_ID = "5124859166"

# KALİTE FİLTRELERİ
MIN_LIQUIDITY = 4_000_000          # $4M likidite
MIN_POTENTIAL_PCT = 10.0           # Minimum %10 potansiyel
MAX_RISK_PCT = 8.0                 # Maximum %8 risk
MIN_ACCUMULATION_SCORE = 7.0       # 10 üzerinden minimum 7 puan

SIGNAL_COOLDOWN_HOURS = 24

MAX_SYMBOLS = None
USE_TOP_VOLUME_POOL = True

# WS / DATA
BOOTSTRAP_LIMIT_1H = 200
BOOTSTRAP_LIMIT_4H = 100
KEEP_BARS_1H = 200
KEEP_BARS_4H = 100
WS_KLINE_INTERVAL = "1h"
WS_STREAM_CHUNK = 100

TR_TZ = timezone(timedelta(hours=3))

# ============================================================
# GLOBALS
# ============================================================
stats = Counter()
ws_close_count = 0
tracked_symbols = []

bars_1h: dict[str, pd.DataFrame] = {}
bars_4h: dict[str, pd.DataFrame] = {}
last_signal_ts: dict[str, datetime] = {}

IGNORED_COINS = set([
    'UP/USDT','DOWN/USDT','BEAR/USDT','BULL/USDT',
    'USDC/USDT','TUSD/USDT','FDUSD/USDT','DAI/USDT','USDP/USDT',
    'EUR/USDT','TRY/USDT','GBP/USDT','BUSD/USDT','USTC/USDT',
    'PAXG/USDT','WBTC/USDT','USDE/USDT','BRL/USDT','RUB/USDT',
    'AUD/USDT','UST/USDT','USD/USDT','BFUSD/USDT','RLUSD/USDT',
])

SIGNAL_LOG_FILE = "/mnt/user-data/outputs/signal_log.json"

@dataclass
class Signal:
    symbol: str
    analysis: dict
    tr_time: datetime

# ============================================================
# API
# ============================================================
class ApiGate:
    def __init__(self):
        self.sem = asyncio.Semaphore(3)
        self._lock = asyncio.Lock()
        self._last_call_ts = 0.0

    async def call(self, fn, *args, **kwargs):
        async with self.sem:
            for attempt in range(5):
                try:
                    async with self._lock:
                        now = time.time()
                        wait = (self._last_call_ts + 0.25) - now
                        if wait > 0:
                            await asyncio.sleep(wait)
                        self._last_call_ts = time.time()
                    
                    loop = asyncio.get_running_loop()
                    return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))
                except Exception:
                    await asyncio.sleep(min(30, 2 ** attempt))
            raise RuntimeError("API failed")

api_gate = ApiGate()

exchange = ccxt.binance({
    "apiKey": BINANCE_API_KEY or None,
    "secret": BINANCE_API_SECRET or None,
    "options": {"defaultType": "spot"},
    "enableRateLimit": True,
    "timeout": 20000,
})

# ============================================================
# UTILS
# ============================================================
def tr_now_str():
    return datetime.now(timezone.utc).astimezone(TR_TZ).strftime("%Y-%m-%d %H:%M:%S")

def fmt_price(symbol: str, price) -> str:
    try:
        if price is None:
            return "N/A"
        return exchange.price_to_precision(symbol, float(price))
    except Exception:
        p = float(price)
        if p < 0.01:
            return f"{p:.8f}"
        elif p < 1:
            return f"{p:.6f}"
        return f"{p:.4f}"

def send_telegram(text: str):
    if not TELEGRAM_TOKEN:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10
        )
    except Exception as e:
        print(f"⚠️ TG: {e}", flush=True)

def save_signal_log(analysis: dict, tr_time: datetime):
    try:
        logs = []
        if os.path.exists(SIGNAL_LOG_FILE):
            with open(SIGNAL_LOG_FILE, "r") as f:
                logs = json.load(f)
        
        logs.append({
            "symbol": analysis["symbol"],
            "timestamp": tr_time.isoformat(),
            "price": analysis["price"],
            "score": analysis["score"],
            "potential": analysis["potential_pct"],
            "risk": analysis["risk_pct"],
        })
        
        os.makedirs(os.path.dirname(SIGNAL_LOG_FILE), exist_ok=True)
        with open(SIGNAL_LOG_FILE, "w") as f:
            json.dump(logs[-100:], f, indent=2)
    except Exception:
        pass

# ============================================================
# MARKET POOL
# ============================================================
async def load_symbols_pool():
    await api_gate.call(exchange.load_markets)
    syms = [
        s for s in exchange.markets
        if s.endswith("/USDT")
        and exchange.markets[s].get("active", False)
        and s not in IGNORED_COINS
    ]
    if not USE_TOP_VOLUME_POOL or not MAX_SYMBOLS:
        return syms[:MAX_SYMBOLS] if MAX_SYMBOLS else syms
    
    volumes = {}
    for i in range(0, len(syms), 120):
        try:
            res = await api_gate.call(exchange.fetch_tickers, syms[i:i+120])
            if isinstance(res, dict):
                for k, v in res.items():
                    volumes[k] = float(v.get("quoteVolume", 0) or 0)
        except Exception:
            continue
    
    return sorted(syms, key=lambda x: volumes.get(x, 0), reverse=True)[:MAX_SYMBOLS] if MAX_SYMBOLS else sorted(syms, key=lambda x: volumes.get(x, 0), reverse=True)

# ============================================================
# DATA
# ============================================================
async def fetch_ohlcv_df(symbol: str, timeframe: str, limit: int):
    bars = await api_gate.call(exchange.fetch_ohlcv, symbol, timeframe=timeframe, limit=limit)
    df = pd.DataFrame(bars, columns=["timestamp","open","high","low","close","volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df.set_index("timestamp", inplace=True)
    return df

def prepare_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["ema20"] = ta.ema(df["close"], length=20)
    df["ema50"] = ta.ema(df["close"], length=50)
    df["rsi"] = ta.rsi(df["close"], length=14)
    df["atr"] = ta.atr(df["high"], df["low"], df["close"], length=14)
    df["vol_ma"] = df["volume"].rolling(20).mean()
    
    bb = ta.bbands(df["close"], length=20, std=2)
    if bb is not None and hasattr(bb, "columns"):
        for col in bb.columns:
            col_upper = str(col).upper()
            if "BBL" in col_upper:
                df["bb_lower"] = bb[col]
            elif "BBM" in col_upper:
                df["bb_mid"] = bb[col]
            elif "BBU" in col_upper:
                df["bb_upper"] = bb[col]
    
    if "bb_upper" in df.columns and "bb_lower" in df.columns and "bb_mid" in df.columns:
        df["bb_width"] = ((df["bb_upper"] - df["bb_lower"]) / df["bb_mid"]) * 100
    
    return df

# ============================================================
# PROFESYONEL ANALİZ
# ============================================================

def detect_accumulation_zone(df_1h: pd.DataFrame) -> Optional[dict]:
    """
    AŞAMA 1: Birikim bölgesi tespiti
    Skor: 0-10
    """
    try:
        if len(df_1h) < 100:
            return None
        
        score = 0.0
        details = {}
        
        # Son 48 saat (48 mum)
        recent_48 = df_1h.iloc[-48:]
        last = df_1h.iloc[-1]
        
        # 1) Dar Range (3 puan)
        range_high = float(recent_48["high"].max())
        range_low = float(recent_48["low"].min())
        range_pct = ((range_high - range_low) / range_low) * 100
        
        if range_pct < 8:
            score += 3.0
            details["narrow_range"] = True
        elif range_pct < 12:
            score += 1.5
            details["narrow_range"] = "Partial"
        
        details["range_pct"] = range_pct
        
        # 2) Volatilite Düşüşü (2 puan)
        atr_now = float(last["atr"])
        atr_avg = float(df_1h["atr"].iloc[-100:-48].mean())
        
        if atr_now < atr_avg * 0.6:
            score += 2.0
            details["low_volatility"] = True
        elif atr_now < atr_avg * 0.8:
            score += 1.0
            details["low_volatility"] = "Partial"
        
        # 3) Hacim Azalması (2 puan)
        vol_recent = float(recent_48["volume"].mean())
        vol_before = float(df_1h["volume"].iloc[-100:-48].mean())
        
        if vol_recent < vol_before * 0.7:
            score += 2.0
            details["volume_decrease"] = True
        elif vol_recent < vol_before * 0.85:
            score += 1.0
            details["volume_decrease"] = "Partial"
        
        # 4) Lower Lows Durdu (3 puan)
        lows_last_24 = recent_48["low"].iloc[-24:].values
        lows_prev_24 = recent_48["low"].iloc[-48:-24].values
        
        # Trend analizi
        if len(lows_last_24) > 10 and len(lows_prev_24) > 10:
            slope_recent, _, _, _, _ = linregress(range(len(lows_last_24)), lows_last_24)
            slope_before, _, _, _, _ = linregress(range(len(lows_prev_24)), lows_prev_24)
            
            # Düşüş trendi durdu mu?
            if slope_before < 0 and slope_recent >= -0.00001:
                score += 3.0
                details["lower_lows_stopped"] = True
            elif slope_recent > slope_before:
                score += 1.5
                details["lower_lows_stopped"] = "Improving"
        
        return {
            "score": score,
            "max_score": 10.0,
            "details": details
        }
        
    except Exception:
        return None

def detect_smart_money(df_1h: pd.DataFrame) -> Optional[dict]:
    """
    AŞAMA 2: Akıllı para izleri
    Skor: 0-10
    """
    try:
        if len(df_1h) < 50:
            return None
        
        score = 0.0
        details = {}
        
        recent_24 = df_1h.iloc[-24:]
        
        # 1) Volume Clusters (4 puan)
        volumes = recent_24["volume"].values
        vol_mean = float(np.mean(volumes))
        vol_std = float(np.std(volumes))
        
        big_volume_count = sum(1 for v in volumes if v > vol_mean + vol_std)
        
        if big_volume_count >= 3:
            score += 4.0
            details["volume_clusters"] = big_volume_count
        elif big_volume_count >= 2:
            score += 2.0
            details["volume_clusters"] = big_volume_count
        
        # 2) Higher Lows Starting (3 puan)
        lows = recent_24["low"].values
        higher_lows = 0
        for i in range(1, len(lows)):
            if lows[i] > lows[i-1]:
                higher_lows += 1
        
        hl_ratio = higher_lows / (len(lows) - 1)
        
        if hl_ratio > 0.5:
            score += 3.0
            details["higher_lows"] = True
        elif hl_ratio > 0.35:
            score += 1.5
            details["higher_lows"] = "Partial"
        
        # 3) Alım Baskısı (3 puan)
        green_vol = 0
        red_vol = 0
        
        for i in range(len(recent_24)):
            candle = recent_24.iloc[i]
            vol = float(candle["volume"])
            if float(candle["close"]) > float(candle["open"]):
                green_vol += vol
            else:
                red_vol += vol
        
        if green_vol + red_vol > 0:
            buy_pressure = green_vol / (green_vol + red_vol)
            
            if buy_pressure > 0.6:
                score += 3.0
                details["buy_pressure"] = buy_pressure
            elif buy_pressure > 0.52:
                score += 1.5
                details["buy_pressure"] = buy_pressure
        
        return {
            "score": score,
            "max_score": 10.0,
            "details": details
        }
        
    except Exception:
        return None

def detect_breakout_setup(df_1h: pd.DataFrame, df_4h: pd.DataFrame) -> Optional[dict]:
    """
    AŞAMA 3: Patlama hazırlığı
    Skor: 0-10
    """
    try:
        if len(df_1h) < 50 or len(df_4h) < 20:
            return None
        
        score = 0.0
        details = {}
        
        last_1h = df_1h.iloc[-1]
        last_4h = df_4h.iloc[-1]
        
        # 1) BB Squeeze (4 puan)
        if "bb_width" in df_1h.columns:
            bb_width = float(last_1h["bb_width"])
            bb_avg = float(df_1h["bb_width"].iloc[-50:].mean())
            
            if bb_width < 3.0:
                score += 4.0
                details["bb_squeeze"] = True
            elif bb_width < bb_avg * 0.7:
                score += 2.0
                details["bb_squeeze"] = "Partial"
            
            details["bb_width"] = bb_width
        
        # 2) 4h Trend Pozitif (3 puan)
        close_4h = float(last_4h["close"])
        ema20_4h = float(last_4h["ema20"])
        ema50_4h = float(last_4h["ema50"])
        
        if close_4h > ema20_4h and ema20_4h > ema50_4h:
            score += 3.0
            details["trend_4h"] = "UPTREND"
        elif close_4h > ema50_4h:
            score += 1.5
            details["trend_4h"] = "SIDEWAYS_UP"
        
        # 3) RSI Neutral Zone (3 puan)
        rsi = float(last_1h["rsi"])
        
        if 40 <= rsi <= 60:
            score += 3.0
            details["rsi_neutral"] = True
        elif 35 <= rsi <= 65:
            score += 1.5
            details["rsi_neutral"] = "Close"
        
        details["rsi"] = rsi
        
        return {
            "score": score,
            "max_score": 10.0,
            "details": details
        }
        
    except Exception:
        return None

def find_resistance_support(df_1h: pd.DataFrame, current_price: float) -> Optional[dict]:
    """Basit SR hesaplama"""
    try:
        if len(df_1h) < 100:
            return None
        
        recent = df_1h.iloc[-100:]
        
        # Pivot highs/lows
        highs = []
        lows = []
        
        for i in range(5, len(recent)-5):
            if recent["high"].iloc[i] == recent["high"].iloc[i-5:i+6].max():
                highs.append(float(recent["high"].iloc[i]))
            if recent["low"].iloc[i] == recent["low"].iloc[i-5:i+6].min():
                lows.append(float(recent["low"].iloc[i]))
        
        # En yakın direnç/destek
        upper = [h for h in highs if h > current_price]
        lower = [l for l in lows if l < current_price]
        
        resistance = min(upper) if upper else current_price * 1.12
        support = max(lower) if lower else current_price * 0.92
        
        return {
            "resistance": resistance,
            "support": support
        }
        
    except Exception:
        return None

async def analyze_symbol(symbol: str, df_1h: pd.DataFrame, df_4h: pd.DataFrame) -> Optional[dict]:
    """Ana analiz"""
    try:
        # AŞAMA 1: Birikim bölgesi
        accum_zone = detect_accumulation_zone(df_1h)
        if not accum_zone or accum_zone["score"] < 5.0:
            stats["low_accumulation"] += 1
            return None
        
        # AŞAMA 2: Akıllı para
        smart_money = detect_smart_money(df_1h)
        if not smart_money or smart_money["score"] < 5.0:
            stats["no_smart_money"] += 1
            return None
        
        # AŞAMA 3: Patlama hazırlığı
        breakout_setup = detect_breakout_setup(df_1h, df_4h)
        if not breakout_setup or breakout_setup["score"] < 5.0:
            stats["no_breakout_setup"] += 1
            return None
        
        # Toplam skor
        total_score = (
            accum_zone["score"] * 0.35 +
            smart_money["score"] * 0.35 +
            breakout_setup["score"] * 0.30
        )
        
        if total_score < MIN_ACCUMULATION_SCORE:
            stats["low_total_score"] += 1
            return None
        
        # SR ve potansiyel
        current_price = float(df_1h["close"].iloc[-1])
        sr = find_resistance_support(df_1h, current_price)
        
        if not sr:
            stats["no_sr"] += 1
            return None
        
        potential_pct = ((sr["resistance"] - current_price) / current_price) * 100
        risk_pct = ((current_price - sr["support"]) / current_price) * 100
        
        if potential_pct < MIN_POTENTIAL_PCT:
            stats["low_potential"] += 1
            return None
        
        if risk_pct > MAX_RISK_PCT:
            stats["high_risk"] += 1
            return None
        
        # Likidite
        ticker = await api_gate.call(exchange.fetch_ticker, symbol)
        liquidity = float(ticker.get("quoteVolume", 0) or 0)
        
        if liquidity < MIN_LIQUIDITY:
            stats["low_liquidity"] += 1
            return None
        
        return {
            "symbol": symbol,
            "price": current_price,
            "resistance": sr["resistance"],
            "support": sr["support"],
            "potential_pct": potential_pct,
            "risk_pct": risk_pct,
            "score": total_score,
            "accumulation": accum_zone,
            "smart_money": smart_money,
            "breakout_setup": breakout_setup,
            "liquidity": liquidity
        }
        
    except Exception as e:
        print(f"⚠️ {symbol}: {str(e)[:50]}", flush=True)
        return None

# ============================================================
# TELEGRAM
# ============================================================
def format_signal(analysis: dict, tr_time: datetime) -> str:
    symbol = analysis["symbol"]
    price = analysis["price"]
    resistance = analysis["resistance"]
    support = analysis["support"]
    potential = analysis["potential_pct"]
    risk = analysis["risk_pct"]
    score = analysis["score"]
    liq = analysis["liquidity"]
    
    price_s = fmt_price(symbol, price)
    res_s = fmt_price(symbol, resistance)
    sup_s = fmt_price(symbol, support)
    
    msg = f"""
🔍 <b>BİRİKİM TESPİT EDİLDİ</b>

<b>#{symbol}</b>
💵 Fiyat: {price_s}
🎯 Direnç: {res_s} (+%{potential:.1f})
🛡️ Destek: {sup_s} (-%{risk:.1f})
━━━━━━━━━━━━━━━━
⭐ Kalite Skoru: {score:.1f}/10
💰 Likidite: ${liq/1e6:.1f}M

📊 <b>ANALİZ:</b>
- Birikim bölgesinde
- Akıllı para giriyor
- Patlama hazırlığı var

⚠️ Henüz patlama olmadı, bekle!

🕐 {tr_time.strftime("%d.%m %H:%M")}
""".strip()
    
    return msg

# ============================================================
# WS & EVAL
# ============================================================
async def evaluate_on_close(symbol: str, df_1h: pd.DataFrame, queue: asyncio.Queue):
    global ws_close_count
    try:
        ws_close_count += 1
        tr_now = datetime.now(timezone.utc).astimezone(TR_TZ)
        
        # Cooldown
        last_ts = last_signal_ts.get(symbol)
        if last_ts:
            hours = (tr_now.replace(tzinfo=None) - last_ts.replace(tzinfo=None)).total_seconds() / 3600
            if hours < SIGNAL_COOLDOWN_HOURS:
                return
        
        df_4h = bars_4h.get(symbol)
        if df_4h is None or len(df_4h) < 20:
            return
        
        analysis = await analyze_symbol(symbol, df_1h, df_4h)
        
        if analysis:
            print(f"🔍 ADAY: {symbol} | Skor:{analysis['score']:.1f} Pot:%{analysis['potential_pct']:.1f}", flush=True)
            await queue.put(Signal(symbol=symbol, analysis=analysis, tr_time=tr_now))
        
    except Exception as e:
        print(f"⚠️ Eval: {symbol[:20]} {str(e)[:30]}", flush=True)

async def signal_worker(queue: asyncio.Queue):
    while True:
        sig: Signal = await queue.get()
        try:
            msg = format_signal(sig.analysis, sig.tr_time)
            send_telegram(msg)
            save_signal_log(sig.analysis, sig.tr_time)
            
            last_signal_ts[sig.symbol] = sig.tr_time.replace(tzinfo=None)
            stats["signal_sent"] += 1
            
            print(f"✅ SİNYAL: {sig.symbol}", flush=True)
        except Exception as e:
            print(f"⚠️ Worker: {str(e)[:50]}", flush=True)
        finally:
            queue.task_done()

# ============================================================
# BOOTSTRAP & WS
# ============================================================
async def bootstrap_symbol(symbol: str):
    try:
        df_1h = await fetch_ohlcv_df(symbol, "1h", BOOTSTRAP_LIMIT_1H)
        df_4h = await fetch_ohlcv_df(symbol, "4h", BOOTSTRAP_LIMIT_4H)
        
        if df_1h is None or len(df_1h) < 100:
            return False
        if df_4h is None or len(df_4h) < 20:
            return False
        
        df_1h = prepare_indicators(df_1h)
        df_4h = prepare_indicators(df_4h)
        
        bars_1h[symbol] = df_1h.iloc[-KEEP_BARS_1H:]
        bars_4h[symbol] = df_4h.iloc[-KEEP_BARS_4H:]
        return True
    except Exception:
        return False

async def bootstrap_all(symbols: list[str]):
    ok = 0
    print(f"🧱 Hazırlık: {len(symbols)} coin", flush=True)
    
    for i, s in enumerate(symbols, 1):
        if i % 50 == 0:
            print(f"-> {i}/{len(symbols)}", flush=True)
        if await bootstrap_symbol(s):
            ok += 1
    
    print(f"✅ Hazır: {ok}/{len(symbols)}", flush=True)
    global tracked_symbols
    tracked_symbols = symbols

def to_ws_symbol(symbol: str) -> str:
    return symbol.replace("/", "").lower()

async def ws_listen_klines(symbols: list[str], queue: asyncio.Queue):
    streams = "/".join([f"{to_ws_symbol(s)}@kline_{WS_KLINE_INTERVAL}" for s in symbols])
    url = f"wss://stream.binance.com:9443/stream?streams={streams}"
    
    retry = 0
    while True:
        try:
            async with websockets.connect(url, ping_interval=30, ping_timeout=30) as ws:
                retry = 0
                print("✅ WS OK", flush=True)
                while True:
                    msg = await ws.recv()
                    data = json.loads(msg)
                    k = data.get("data", {}).get("k", {})
                    
                    if not k.get("x"):
                        continue
                    
                    symbol = data.get("data", {}).get("s", "").upper().replace("USDT", "/USDT")
                    
                    df_1h = bars_1h.get(symbol)
                    if df_1h is None or len(df_1h) < 50:
                        continue
                    
                    ts = pd.to_datetime(int(k["t"]), unit="ms", utc=True)
                    df_1h.loc[ts, ["open","high","low","close","volume"]] = [
                        float(k["o"]), float(k["h"]), float(k["l"]), float(k["c"]), float(k["v"])
                    ]
                    df_1h = df_1h.sort_index().iloc[-KEEP_BARS_1H:]
                    df_1h = prepare_indicators(df_1h)
                    bars_1h[symbol] = df_1h
                    
                    await evaluate_on_close(symbol, df_1h, queue)
        except Exception:
            retry += 1
            await asyncio.sleep(min(60, 5 * (2 ** min(retry, 4))))

async def ws_listen_multi(symbols: list[str], queue: asyncio.Queue):
    tasks = []
    for i in range(0, len(symbols), WS_STREAM_CHUNK):
        tasks.append(asyncio.create_task(ws_listen_klines(symbols[i:i+WS_STREAM_CHUNK], queue)))
    await asyncio.gather(*tasks)

# ============================================================
# FLASK
# ============================================================
app = Flask(__name__)

import logging
logging.getLogger("werkzeug").setLevel(logging.ERROR)
app.logger.disabled = True

@app.route("/")
def home():
    return f"""
    <h1>🔍 Professional Accumulation Detector v5.0</h1>
    <p>Coins: {len(tracked_symbols)}</p>
    <p>Signals: {stats.get('signal_sent', 0)}</p>
    <p>Time: {tr_now_str()}</p>
    """

@app.route("/health")
def health():
    return {"status": "OK", "stats": dict(stats)}

def start_flask():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")), use_reloader=False)

# ============================================================
# MAIN
# ============================================================
async def main():
    print("🔍 Professional Accumulation Detector v5.0", flush=True)
    
    symbols = await load_symbols_pool()
    if not symbols:
        return
    
    print(f"✅ Symbols: {len(symbols)}", flush=True)
    
    await bootstrap_all(symbols)
    
    queue = asyncio.Queue()
    asyncio.create_task(signal_worker(queue))
    
    await ws_listen_multi(symbols, queue)

if __name__ == "__main__":
    threading.Thread(target=start_flask, daemon=True).start()
    
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("🛑 Durduruldu", flush=True)
