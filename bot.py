# -*- coding: utf-8 -*-
"""
Professional Scoring System v7.0
10 üzerinden skorlama - Şeffaf analiz
"""

import asyncio
import json
import time
import threading
import os
from collections import Counter
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

# ============================================================
# AYARLAR
# ============================================================
TELEGRAM_TOKEN = "7583261338:AAFwkpxsumCBpYI5Ai-aIiII6INm_thmg-I"
TELEGRAM_CHAT_ID = "5124859166"

# SİNYAL EŞİKLERİ
STRONG_SIGNAL_MIN = 7.5    # ⭐⭐⭐
GOOD_SIGNAL_MIN = 6.5      # ⭐⭐
MEDIUM_SIGNAL_MIN = 6.0    # ⭐

# POTANSİYEL
MIN_POTENTIAL = {
    "STRONG": 10.0,   # Güçlü sinyal için %10 yeterli
    "GOOD": 12.0,     # İyi sinyal için %12
    "MEDIUM": 15.0    # Orta sinyal için %15
}

MIN_LIQUIDITY = 3_000_000
SIGNAL_COOLDOWN_HOURS = 24

# DATA
BOOTSTRAP_LIMIT_15M = 100
BOOTSTRAP_LIMIT_1H = 100
BOOTSTRAP_LIMIT_4H = 100
KEEP_BARS_15M = 100
KEEP_BARS_1H = 100
KEEP_BARS_4H = 100
WS_KLINE_INTERVAL = "15m"
WS_STREAM_CHUNK = 100

TR_TZ = timezone(timedelta(hours=3))

# ============================================================
# GLOBALS
# ============================================================
stats = Counter()
ws_close_count = 0
tracked_symbols = []

bars_15m = {}
bars_1h = {}
bars_4h = {}
last_signal_ts = {}

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
        self._last = 0.0

    async def call(self, fn, *args, **kwargs):
        async with self.sem:
            for attempt in range(5):
                try:
                    async with self._lock:
                        wait = (self._last + 0.25) - time.time()
                        if wait > 0:
                            await asyncio.sleep(wait)
                        self._last = time.time()
                    
                    loop = asyncio.get_running_loop()
                    return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))
                except Exception:
                    await asyncio.sleep(min(30, 2 ** attempt))
            return None

api_gate = ApiGate()
exchange = ccxt.binance({
    "enableRateLimit": True,
    "timeout": 20000,
    "options": {"defaultType": "spot"}
})

# ============================================================
# UTILS
# ============================================================
def tr_now_str():
    return datetime.now(timezone.utc).astimezone(TR_TZ).strftime("%Y-%m-%d %H:%M:%S")

def fmt_price(symbol, price):
    try:
        return exchange.price_to_precision(symbol, float(price))
    except Exception:
        p = float(price)
        if p < 0.01:
            return f"{p:.8f}"
        elif p < 1:
            return f"{p:.6f}"
        return f"{p:.4f}"

def send_telegram(text):
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

def save_signal_log(analysis, tr_time):
    try:
        logs = []
        if os.path.exists(SIGNAL_LOG_FILE):
            with open(SIGNAL_LOG_FILE, "r") as f:
                logs = json.load(f)
        
        logs.append({
            "symbol": analysis["symbol"],
            "timestamp": tr_time.isoformat(),
            "total_score": analysis["total_score"],
            "potential": analysis["potential_pct"],
            "price": analysis["price"]
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
    
    volumes = {}
    for i in range(0, len(syms), 120):
        res = await api_gate.call(exchange.fetch_tickers, syms[i:i+120])
        if res and isinstance(res, dict):
            for k, v in res.items():
                volumes[k] = float(v.get("quoteVolume", 0) or 0)
    
    return sorted(syms, key=lambda x: volumes.get(x, 0), reverse=True)

# ============================================================
# DATA
# ============================================================
async def fetch_ohlcv_df(symbol, timeframe, limit):
    bars = await api_gate.call(exchange.fetch_ohlcv, symbol, timeframe=timeframe, limit=limit)
    if not bars:
        return None
    df = pd.DataFrame(bars, columns=["timestamp","open","high","low","close","volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df.set_index("timestamp", inplace=True)
    return df

def prepare_indicators(df):
    df = df.copy()
    df["ema20"] = ta.ema(df["close"], length=20)
    df["ema50"] = ta.ema(df["close"], length=50)
    df["rsi"] = ta.rsi(df["close"], length=14)
    df["atr"] = ta.atr(df["high"], df["low"], df["close"], length=14)
    df["vol_ma"] = df["volume"].rolling(20).mean()
    
    # MACD
    macd = ta.macd(df["close"], fast=12, slow=26, signal=9)
    if macd is not None and hasattr(macd, "columns"):
        for col in macd.columns:
            col_upper = str(col).upper()
            if "MACD_12" in col_upper:
                df["macd"] = macd[col]
            elif "MACDS_12" in col_upper:
                df["macd_signal"] = macd[col]
    
    # Bollinger Bands
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
# SKORLAMA SİSTEMİ - 10 ÜZERINDEN
# ============================================================

def calculate_trend_score(df_1h, df_4h):
    """
    TREND ANALİZİ - 3 puan
    """
    try:
        score = 0.0
        details = {}
        
        if df_4h is None or len(df_4h) < 50:
            return {"score": 0, "details": {"error": "Veri yok"}}
        
        last_4h = df_4h.iloc[-1]
        
        # 1) 4h EMA Alignment (1.5 puan)
        close_4h = float(last_4h["close"])
        ema20_4h = float(last_4h["ema20"])
        ema50_4h = float(last_4h["ema50"])
        
        if close_4h > ema20_4h and ema20_4h > ema50_4h:
            score += 1.5
            details["ema_alignment"] = "Perfect"
        elif close_4h > ema20_4h or ema20_4h > ema50_4h:
            score += 0.75
            details["ema_alignment"] = "Partial"
        else:
            details["ema_alignment"] = "Weak"
        
        # 2) RSI Pozisyon (1 puan)
        if df_1h is not None and len(df_1h) > 20:
            last_1h = df_1h.iloc[-1]
            rsi = float(last_1h["rsi"])
            
            if 45 <= rsi <= 60:
                score += 1.0
                details["rsi_position"] = "Ideal"
            elif 40 <= rsi <= 65:
                score += 0.6
                details["rsi_position"] = "Good"
            elif 35 <= rsi <= 70:
                score += 0.3
                details["rsi_position"] = "Ok"
            else:
                details["rsi_position"] = "Bad"
            
            details["rsi"] = rsi
        
        # 3) Fiyat Momentum (0.5 puan)
        if close_4h > float(last_4h["open"]):
            score += 0.5
            details["momentum"] = "Positive"
        else:
            details["momentum"] = "Negative"
        
        return {
            "score": round(score, 2),
            "max_score": 3.0,
            "details": details
        }
        
    except Exception as e:
        return {"score": 0, "details": {"error": str(e)[:50]}}

def calculate_accumulation_score(df_15m, df_1h):
    """
    BİRİKİM ANALİZİ - 3 puan
    """
    try:
        score = 0.0
        details = {}
        
        if df_1h is None or len(df_1h) < 50:
            return {"score": 0, "details": {"error": "Veri yok"}}
        
        recent_24 = df_1h.iloc[-24:]
        recent_20 = df_1h.iloc[-20:]
        
        # 1) Volume Spikes (1.5 puan)
        vol_mean = float(recent_24["volume"].mean())
        vol_std = float(recent_24["volume"].std())
        
        big_volume_count = 0
        for i in range(len(recent_24)):
            if float(recent_24["volume"].iloc[i]) > vol_mean + vol_std:
                big_volume_count += 1
        
        if big_volume_count >= 3:
            score += 1.5
            details["volume_spikes"] = f"{big_volume_count} spikes"
        elif big_volume_count == 2:
            score += 1.0
            details["volume_spikes"] = "2 spikes"
        elif big_volume_count == 1:
            score += 0.5
            details["volume_spikes"] = "1 spike"
        else:
            details["volume_spikes"] = "None"
        
        # 2) Higher Lows Pattern (1 puan)
        lows = recent_20["low"].values
        higher_lows = sum(1 for i in range(1, len(lows)) if lows[i] > lows[i-1])
        hl_ratio = higher_lows / (len(lows) - 1) if len(lows) > 1 else 0
        
        if hl_ratio > 0.5:
            score += 1.0
            details["higher_lows"] = "Strong"
        elif hl_ratio > 0.4:
            score += 0.7
            details["higher_lows"] = "Good"
        elif hl_ratio > 0.3:
            score += 0.4
            details["higher_lows"] = "Weak"
        else:
            details["higher_lows"] = "None"
        
        # 3) Buy Pressure (0.5 puan)
        green_vol = 0
        red_vol = 0
        
        for i in range(len(recent_20)):
            candle = recent_20.iloc[i]
            vol = float(candle["volume"])
            if float(candle["close"]) > float(candle["open"]):
                green_vol += vol
            else:
                red_vol += vol
        
        total_vol = green_vol + red_vol
        buy_pressure = (green_vol / total_vol) if total_vol > 0 else 0
        
        if buy_pressure > 0.6:
            score += 0.5
            details["buy_pressure"] = "Strong"
        elif buy_pressure > 0.55:
            score += 0.3
            details["buy_pressure"] = "Good"
        else:
            details["buy_pressure"] = "Weak"
        
        details["buy_pressure_pct"] = round(buy_pressure * 100, 1)
        
        return {
            "score": round(score, 2),
            "max_score": 3.0,
            "details": details
        }
        
    except Exception as e:
        return {"score": 0, "details": {"error": str(e)[:50]}}

def calculate_squeeze_score(df_1h):
    """
    SIKIŞMA/VOLATİLİTE - 2 puan
    """
    try:
        score = 0.0
        details = {}
        
        if df_1h is None or len(df_1h) < 50:
            return {"score": 0, "details": {"error": "Veri yok"}}
        
        last = df_1h.iloc[-1]
        
        # 1) BB Width (1 puan)
        if "bb_width" in df_1h.columns and not pd.isna(last["bb_width"]):
            bb_width = float(last["bb_width"])
            
            if bb_width < 3.0:
                score += 1.0
                details["bb_squeeze"] = "Strong"
            elif bb_width < 4.0:
                score += 0.7
                details["bb_squeeze"] = "Good"
            elif bb_width < 5.0:
                score += 0.4
                details["bb_squeeze"] = "Weak"
            else:
                details["bb_squeeze"] = "None"
            
            details["bb_width"] = round(bb_width, 2)
        
        # 2) ATR Düşüşü (0.5 puan)
        atr_now = float(last["atr"])
        atr_avg = float(df_1h["atr"].iloc[-50:].mean())
        
        if atr_now < atr_avg * 0.6:
            score += 0.5
            details["atr_drop"] = "Strong"
        elif atr_now < atr_avg * 0.8:
            score += 0.3
            details["atr_drop"] = "Moderate"
        else:
            details["atr_drop"] = "None"
        
        # 3) Range Daralması (0.5 puan)
        recent_48 = df_1h.iloc[-48:]
        range_high = float(recent_48["high"].max())
        range_low = float(recent_48["low"].min())
        range_pct = ((range_high - range_low) / range_low) * 100
        
        if range_pct < 8.0:
            score += 0.5
            details["range"] = "Tight"
        elif range_pct < 12.0:
            score += 0.3
            details["range"] = "Moderate"
        else:
            details["range"] = "Wide"
        
        details["range_pct"] = round(range_pct, 2)
        
        return {
            "score": round(score, 2),
            "max_score": 2.0,
            "details": details
        }
        
    except Exception as e:
        return {"score": 0, "details": {"error": str(e)[:50]}}

def calculate_momentum_score(df_15m, df_1h):
    """
    MOMENTUM - 2 puan
    """
    try:
        score = 0.0
        details = {}
        
        if df_15m is None or len(df_15m) < 50:
            return {"score": 0, "details": {"error": "Veri yok"}}
        
        last_15m = df_15m.iloc[-1]
        
        # 1) Son Mum Gücü (1 puan)
        close = float(last_15m["close"])
        open_price = float(last_15m["open"])
        high = float(last_15m["high"])
        low = float(last_15m["low"])
        vol = float(last_15m["volume"])
        vol_ma = float(last_15m["vol_ma"]) if not pd.isna(last_15m["vol_ma"]) else 0
        
        is_green = close > open_price
        body_ratio = ((close - open_price) / (high - low)) if (high > low) else 0
        vol_ratio = (vol / vol_ma) if vol_ma > 0 else 0
        
        if is_green and vol_ratio > 2.0 and body_ratio > 0.65:
            score += 1.0
            details["candle_strength"] = "Strong"
        elif is_green and vol_ratio > 1.8 and body_ratio > 0.60:
            score += 0.6
            details["candle_strength"] = "Good"
        elif is_green and vol_ratio > 1.5 and body_ratio > 0.55:
            score += 0.3
            details["candle_strength"] = "Weak"
        else:
            details["candle_strength"] = "None"
        
        details["vol_ratio"] = round(vol_ratio, 2)
        details["body_ratio"] = round(body_ratio * 100, 1)
        
        # 2) Yeni Yüksek (0.5 puan)
        recent_40 = df_15m.iloc[-40:-1]
        prev_high = float(recent_40["high"].max())
        
        if close > prev_high:
            recent_20 = df_15m.iloc[-20:-1]
            if close > float(recent_20["high"].max()):
                score += 0.5
                details["new_high"] = "Strong (20 bar)"
            else:
                score += 0.3
                details["new_high"] = "Moderate (40 bar)"
        else:
            details["new_high"] = "None"
        
        # 3) MACD Pozitif (0.5 puan)
        if df_1h is not None and len(df_1h) > 20:
            last_1h = df_1h.iloc[-1]
            if "macd" in df_1h.columns and "macd_signal" in df_1h.columns:
                macd = float(last_1h["macd"])
                macd_signal = float(last_1h["macd_signal"])
                
                if macd > macd_signal:
                    score += 0.5
                    details["macd"] = "Positive"
                else:
                    details["macd"] = "Negative"
        
        return {
            "score": round(score, 2),
            "max_score": 2.0,
            "details": details
        }
        
    except Exception as e:
        return {"score": 0, "details": {"error": str(e)[:50]}}

# ============================================================
# SR & POTANSİYEL
# ============================================================
def find_resistance_support(df_1h, current_price):
    try:
        if df_1h is None or len(df_1h) < 100:
            return None
        
        recent = df_1h.iloc[-100:]
        
        highs = []
        lows = []
        
        for i in range(5, len(recent)-5):
            if recent["high"].iloc[i] == recent["high"].iloc[i-5:i+6].max():
                highs.append(float(recent["high"].iloc[i]))
            if recent["low"].iloc[i] == recent["low"].iloc[i-5:i+6].min():
                lows.append(float(recent["low"].iloc[i]))
        
        upper = [h for h in highs if h > current_price]
        lower = [l for l in lows if l < current_price]
        
        resistance = min(upper) if upper else current_price * 1.12
        support = max(lower) if lower else current_price * 0.92
        
        return {"resistance": resistance, "support": support}
        
    except Exception:
        return None

# ============================================================
# ANA ANALİZ
# ============================================================
async def analyze_symbol(symbol, df_15m, df_1h, df_4h):
    try:
        # 4 aşama skorlama
        trend = calculate_trend_score(df_1h, df_4h)
        accumulation = calculate_accumulation_score(df_15m, df_1h)
        squeeze = calculate_squeeze_score(df_1h)
        momentum = calculate_momentum_score(df_15m, df_1h)
        
        # Toplam skor
        total_score = (
            trend["score"] +
            accumulation["score"] +
            squeeze["score"] +
            momentum["score"]
        )
        
        # Minimum skor kontrolü
        if total_score < MEDIUM_SIGNAL_MIN:
            stats["low_score"] += 1
            return None
        
        # SR & Potansiyel
        current_price = float(df_15m["close"].iloc[-1])
        sr = find_resistance_support(df_1h, current_price)
        
        if not sr:
            stats["no_sr"] += 1
            return None
        
        potential_pct = ((sr["resistance"] - current_price) / current_price) * 100
        risk_pct = ((current_price - sr["support"]) / current_price) * 100
        
        # Sinyal seviyesi belirleme
        if total_score >= STRONG_SIGNAL_MIN:
            signal_level = "STRONG"
            min_pot = MIN_POTENTIAL["STRONG"]
        elif total_score >= GOOD_SIGNAL_MIN:
            signal_level = "GOOD"
            min_pot = MIN_POTENTIAL["GOOD"]
        else:
            signal_level = "MEDIUM"
            min_pot = MIN_POTENTIAL["MEDIUM"]
        
        if potential_pct < min_pot:
            stats["low_potential"] += 1
            return None
        
        # Likidite
        ticker = await api_gate.call(exchange.fetch_ticker, symbol)
        if not ticker:
            return None
        
        liquidity = float(ticker.get("quoteVolume", 0) or 0)
        
        if liquidity < MIN_LIQUIDITY:
            stats["low_liquidity"] += 1
            return None
        
        return {
            "symbol": symbol,
            "price": current_price,
            "total_score": round(total_score, 2),
            "signal_level": signal_level,
            "trend": trend,
            "accumulation": accumulation,
            "squeeze": squeeze,
            "momentum": momentum,
            "resistance": sr["resistance"],
            "support": sr["support"],
            "potential_pct": potential_pct,
            "risk_pct": risk_pct,
            "liquidity": liquidity
        }
        
    except Exception as e:
        print(f"⚠️ {symbol}: {str(e)[:50]}", flush=True)
        return None

# ============================================================
# TELEGRAM
# ============================================================
def format_signal(analysis, tr_time):
    symbol = analysis["symbol"]
    price = analysis["price"]
    total = analysis["total_score"]
    level = analysis["signal_level"]
    
    trend = analysis["trend"]
    accum = analysis["accumulation"]
    squeeze = analysis["squeeze"]
    momentum = analysis["momentum"]
    
    resistance = analysis["resistance"]
    support = analysis["support"]
    potential = analysis["potential_pct"]
    risk = analysis["risk_pct"]
    liq = analysis["liquidity"]
    
    price_s = fmt_price(symbol, price)
    res_s = fmt_price(symbol, resistance)
    sup_s = fmt_price(symbol, support)
    
    # Yıldız sayısı
    if level == "STRONG":
        stars = "⭐⭐⭐"
    elif level == "GOOD":
        stars = "⭐⭐"
    else:
        stars = "⭐"
    
    # Trend detay
    trend_bar = "🟩" * int(trend["score"] * 3) + "⬜" * (9 - int(trend["score"] * 3))
    accum_bar = "🟩" * int(accum["score"] * 3) + "⬜" * (9 - int(accum["score"] * 3))
    squeeze_bar = "🟩" * int(squeeze["score"] * 4.5) + "⬜" * (9 - int(squeeze["score"] * 4.5))
    momentum_bar = "🟩" * int(momentum["score"] * 4.5) + "⬜" * (9 - int(momentum["score"] * 4.5))
    
    msg = f"""
🔍 <b>BİRİKİM TESPİT EDİLDİ</b>

<b>#{symbol}</b>
💵 Fiyat: {price_s}
━━━━━━━━━━━━━━━━
{stars} <b>SKOR: {total}/10</b>

📊 <b>Detay:</b>
- Trend: {trend['score']}/3
  {trend_bar}
- Birikim: {accum['score']}/3
  {accum_bar}
- Sıkışma: {squeeze['score']}/2
  {squeeze_bar}
- Momentum: {momentum['score']}/2
  {momentum_bar}

🎯 Direnç: {res_s} (+%{potential:.1f})
🛡️ Destek: {sup_s} (-%{risk:.1f})
💰 Likidite: ${liq/1e6:.1f}M

🕐 {tr_time.strftime("%d.%m %H:%M")}
""".strip()
    
    return msg

# ============================================================
# WS & EVAL
# ============================================================
async def evaluate_on_close(symbol, df_15m, queue):
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
        
        df_1h = bars_1h.get(symbol)
        df_4h = bars_4h.get(symbol)
        
        if df_1h is None or len(df_1h) < 50:
            return
        if df_4h is None or len(df_4h) < 20:
            return
        
        analysis = await analyze_symbol(symbol, df_15m, df_1h, df_4h)
        
        if analysis:
            print(f"🔍 {symbol} | Skor:{analysis['total_score']}/10 | Pot:%{analysis['potential_pct']:.1f}", flush=True)
            await queue.put(Signal(symbol=symbol, analysis=analysis, tr_time=tr_now))
        
    except Exception as e:
        print(f"⚠️ {symbol[:15]}: {str(e)[:30]}", flush=True)

async def signal_worker(queue):
    while True:
        sig = await queue.get()
        try:
            msg = format_signal(sig.analysis, sig.tr_time)
            send_telegram(msg)
            save_signal_log(sig.analysis, sig.tr_time)
            
            last_signal_ts[sig.symbol] = sig.tr_time.replace(tzinfo=None)
            stats["signal_sent"] += 1
            
            print(f"✅ SİNYAL: {sig.symbol} [{sig.analysis['signal_level']}]", flush=True)
        except Exception as e:
            print(f"⚠️ Worker: {str(e)[:50]}", flush=True)
        finally:
            queue.task_done()

# ============================================================
# BOOTSTRAP
# ============================================================
async def bootstrap_symbol(symbol):
    try:
        df_15m = await fetch_ohlcv_df(symbol, "15m", BOOTSTRAP_LIMIT_15M)
        df_1h = await fetch_ohlcv_df(symbol, "1h", BOOTSTRAP_LIMIT_1H)
        df_4h = await fetch_ohlcv_df(symbol, "4h", BOOTSTRAP_LIMIT_4H)
        
        if df_15m is None or len(df_15m) < 50:
            return False
        if df_1h is None or len(df_1h) < 50:
            return False
        if df_4h is None or len(df_4h) < 20:
            return False
        
        df_15m = prepare_indicators(df_15m)
        df_1h = prepare_indicators(df_1h)
        df_4h = prepare_indicators(df_4h)
        
        bars_15m[symbol] = df_15m.iloc[-KEEP_BARS_15M:]
        bars_1h[symbol] = df_1h.iloc[-KEEP_BARS_1H:]
        bars_4h[symbol] = df_4h.iloc[-KEEP_BARS_4H:]
        return True
    except Exception:
        return False

async def bootstrap_all(symbols):
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

# ============================================================
# WS
# ============================================================
def to_ws_symbol(symbol):
    return symbol.replace("/", "").lower()

async def ws_listen_klines(symbols, queue):
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
                    
                    df_15m = bars_15m.get(symbol)
                    if df_15m is None or len(df_15m) < 20:
                        continue
                    
                    ts = pd.to_datetime(int(k["t"]), unit="ms", utc=True)
                    df_15m.loc[ts, ["open","high","low","close","volume"]] = [
                        float(k["o"]), float(k["h"]), float(k["l"]), float(k["c"]), float(k["v"])
                    ]
                    df_15m = df_15m.sort_index().iloc[-KEEP_BARS_15M:]
                    df_15m = prepare_indicators(df_15m)
                    bars_15m[symbol] = df_15m
                    
                    await evaluate_on_close(symbol, df_15m, queue)
        except Exception:
            retry += 1
            await asyncio.sleep(min(60, 5 * (2 ** min(retry, 4))))

async def ws_listen_multi(symbols, queue):
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
    <html>
    <head>
        <style>
            body {{ background: #1a1a1a; color: #fff; font-family: Arial; padding: 20px; }}
            h1 {{ color: #4CAF50; }}
            .stat {{ background: #2a2a2a; padding: 15px; margin: 10px 0; border-radius: 8px; }}
        </style>
    </head>
    <body>
        <h1>🔍 Professional Scoring System v7.0</h1>
        <div class="stat">
            <p><b>Coins:</b> {len(tracked_symbols)}</p>
            <p><b>Checks:</b> {ws_close_count}</p>
            <p><b>Signals:</b> {stats.get('signal_sent', 0)}</p>
            <p><b>Time:</b> {tr_now_str()}</p>
        </div>
        <div class="stat">
            <h3>Eleme Sebepleri:</h3>
            <p>Low score: {stats.get('low_score', 0)}</p>
            <p>Low potential: {stats.get('low_potential', 0)}</p>
            <p>Low liquidity: {stats.get('low_liquidity', 0)}</p>
        </div>
    </body>
    </html>
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
    print("🔍 Professional Scoring System v7.0", flush=True)
    
    symbols = await load_symbols_pool()
    if not symbols:
        return
    
    print(f"✅ {len(symbols)} coins", flush=True)
    await bootstrap_all(symbols)
    
    queue = asyncio.Queue()
    asyncio.create_task(signal_worker(queue))
    
    # Stats her 10 dakikada
    async def periodic_stats():
        while True:
            await asyncio.sleep(600)
            print(f"📊 [{tr_now_str()}] Checks:{ws_close_count} Signals:{stats.get('signal_sent', 0)}", flush=True)
    
    asyncio.create_task(periodic_stats())
    
    await ws_listen_multi(symbols, queue)

if __name__ == "__main__":
    threading.Thread(target=start_flask, daemon=True).start()
    
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("🛑 Stop", flush=True)
