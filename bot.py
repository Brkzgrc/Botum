# -*- coding: utf-8 -*-
"""
Hybrid Candidate System v8.0
Otomatik tarama + Order book filtreleme + Manuel onay
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
from flask import Flask

# ============================================================
# AYARLAR
# ============================================================
TELEGRAM_TOKEN = "7583261338:AAFwkpxsumCBpYI5Ai-aIiII6INm_thmg-I"
TELEGRAM_CHAT_ID = "5124859166"

# ADAY FİLTRELERİ
MIN_SCORE = 7.0                    # Minimum skor
MIN_POTENTIAL = 12.0               # Minimum %12 potansiyel
MIN_LIQUIDITY = 3_000_000          # $3M

# ORDER BOOK FİLTRELERİ
MIN_BID_ASK_RATIO = 1.8            # Alım/Satım minimum 1.8x
MAX_SPREAD_PCT = 0.5               # Maksimum %0.5 spread

# TARAMA AYARLARI
SCAN_INTERVAL_HOURS = 1            # Her 1 saatte bir tara
CANDIDATE_COOLDOWN_HOURS = 24      # Aynı coin 24 saatte 1 kez

TR_TZ = timezone(timedelta(hours=3))

# ============================================================
# GLOBALS
# ============================================================
stats = Counter()
last_candidate_ts = {}

IGNORED_COINS = set([
    'UP/USDT','DOWN/USDT','BEAR/USDT','BULL/USDT',
    'USDC/USDT','TUSD/USDT','FDUSD/USDT','DAI/USDT','USDP/USDT',
    'EUR/USDT','TRY/USDT','GBP/USDT','BUSD/USDT','USTC/USDT',
    'PAXG/USDT','WBTC/USDT','USDE/USDT','BRL/USDT','RUB/USDT',
    'AUD/USDT','UST/USDT','USD/USDT','BFUSD/USDT','RLUSD/USDT',
])

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
                        wait = (self._last + 0.3) - time.time()
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
    
    macd = ta.macd(df["close"], fast=12, slow=26, signal=9)
    if macd is not None and hasattr(macd, "columns"):
        for col in macd.columns:
            col_upper = str(col).upper()
            if "MACD_12" in col_upper:
                df["macd"] = macd[col]
            elif "MACDS_12" in col_upper:
                df["macd_signal"] = macd[col]
    
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
# SKORLAMA
# ============================================================
def calculate_trend_score(df_1h, df_4h):
    try:
        score = 0.0
        details = {}
        
        if df_4h is None or len(df_4h) < 50:
            return {"score": 0, "details": {}}
        
        last_4h = df_4h.iloc[-1]
        close_4h = float(last_4h["close"])
        ema20_4h = float(last_4h["ema20"])
        ema50_4h = float(last_4h["ema50"])
        
        if close_4h > ema20_4h and ema20_4h > ema50_4h:
            score += 1.5
            details["ema"] = "Perfect"
        elif close_4h > ema20_4h or ema20_4h > ema50_4h:
            score += 0.75
            details["ema"] = "Partial"
        
        if df_1h is not None and len(df_1h) > 20:
            rsi = float(df_1h.iloc[-1]["rsi"])
            if 45 <= rsi <= 60:
                score += 1.0
            elif 40 <= rsi <= 65:
                score += 0.6
            elif 35 <= rsi <= 70:
                score += 0.3
            details["rsi"] = rsi
        
        if close_4h > float(last_4h["open"]):
            score += 0.5
        
        return {"score": round(score, 2), "details": details}
    except Exception:
        return {"score": 0, "details": {}}

def calculate_accumulation_score(df_15m, df_1h):
    try:
        score = 0.0
        details = {}
        
        if df_1h is None or len(df_1h) < 50:
            return {"score": 0, "details": {}}
        
        recent_24 = df_1h.iloc[-24:]
        recent_20 = df_1h.iloc[-20:]
        
        vol_mean = float(recent_24["volume"].mean())
        vol_std = float(recent_24["volume"].std())
        big_vol = sum(1 for i in range(len(recent_24)) if float(recent_24["volume"].iloc[i]) > vol_mean + vol_std)
        
        if big_vol >= 3:
            score += 1.5
        elif big_vol == 2:
            score += 1.0
        elif big_vol == 1:
            score += 0.5
        
        details["vol_spikes"] = big_vol
        
        lows = recent_20["low"].values
        hl = sum(1 for i in range(1, len(lows)) if lows[i] > lows[i-1])
        hl_ratio = hl / (len(lows) - 1) if len(lows) > 1 else 0
        
        if hl_ratio > 0.5:
            score += 1.0
        elif hl_ratio > 0.4:
            score += 0.7
        elif hl_ratio > 0.3:
            score += 0.4
        
        green = sum(float(recent_20.iloc[i]["volume"]) for i in range(len(recent_20)) if float(recent_20.iloc[i]["close"]) > float(recent_20.iloc[i]["open"]))
        red = sum(float(recent_20.iloc[i]["volume"]) for i in range(len(recent_20)) if float(recent_20.iloc[i]["close"]) <= float(recent_20.iloc[i]["open"]))
        bp = green / (green + red) if (green + red) > 0 else 0
        
        if bp > 0.6:
            score += 0.5
        elif bp > 0.55:
            score += 0.3
        
        details["buy_pressure"] = round(bp * 100, 1)
        
        return {"score": round(score, 2), "details": details}
    except Exception:
        return {"score": 0, "details": {}}

def calculate_squeeze_score(df_1h):
    try:
        score = 0.0
        details = {}
        
        if df_1h is None or len(df_1h) < 50:
            return {"score": 0, "details": {}}
        
        last = df_1h.iloc[-1]
        
        if "bb_width" in df_1h.columns and not pd.isna(last["bb_width"]):
            bbw = float(last["bb_width"])
            if bbw < 3.0:
                score += 1.0
            elif bbw < 4.0:
                score += 0.7
            elif bbw < 5.0:
                score += 0.4
            details["bb"] = round(bbw, 2)
        
        atr = float(last["atr"])
        atr_avg = float(df_1h["atr"].iloc[-50:].mean())
        if atr < atr_avg * 0.6:
            score += 0.5
        elif atr < atr_avg * 0.8:
            score += 0.3
        
        r48 = df_1h.iloc[-48:]
        rh = float(r48["high"].max())
        rl = float(r48["low"].min())
        rpct = ((rh - rl) / rl) * 100
        if rpct < 8.0:
            score += 0.5
        elif rpct < 12.0:
            score += 0.3
        details["range"] = round(rpct, 2)
        
        return {"score": round(score, 2), "details": details}
    except Exception:
        return {"score": 0, "details": {}}

def calculate_momentum_score(df_15m, df_1h):
    try:
        score = 0.0
        details = {}
        
        if df_15m is None or len(df_15m) < 50:
            return {"score": 0, "details": {}}
        
        last = df_15m.iloc[-1]
        c = float(last["close"])
        o = float(last["open"])
        h = float(last["high"])
        l = float(last["low"])
        v = float(last["volume"])
        vma = float(last["vol_ma"]) if not pd.isna(last["vol_ma"]) else 0
        
        body = ((c - o) / (h - l)) if h > l else 0
        vr = (v / vma) if vma > 0 else 0
        
        if c > o and vr > 2.0 and body > 0.65:
            score += 1.0
        elif c > o and vr > 1.8 and body > 0.60:
            score += 0.6
        elif c > o and vr > 1.5 and body > 0.55:
            score += 0.3
        
        details["vol_ratio"] = round(vr, 2)
        
        r40 = df_15m.iloc[-40:-1]
        if c > float(r40["high"].max()):
            score += 0.5
        elif c > float(df_15m.iloc[-20:-1]["high"].max()):
            score += 0.3
        
        if df_1h and len(df_1h) > 20:
            l1h = df_1h.iloc[-1]
            if "macd" in df_1h.columns and "macd_signal" in df_1h.columns:
                if float(l1h["macd"]) > float(l1h["macd_signal"]):
                    score += 0.5
        
        return {"score": round(score, 2), "details": details}
    except Exception:
        return {"score": 0, "details": {}}

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
# ORDER BOOK ANALİZİ
# ============================================================
async def check_order_book(symbol):
    """
    Order book analizi - Alım baskısı var mı?
    """
    try:
        ob = await api_gate.call(exchange.fetch_order_book, symbol, 20)
        
        if not ob or "bids" not in ob or "asks" not in ob:
            return None
        
        bids = ob["bids"][:20]
        asks = ob["asks"][:20]
        
        if not bids or not asks:
            return None
        
        # Bid/Ask volume
        bid_volume = sum([b[1] for b in bids])
        ask_volume = sum([a[1] for a in asks])
        
        if ask_volume == 0:
            return None
        
        bid_ask_ratio = bid_volume / ask_volume
        
        # Spread
        best_bid = bids[0][0]
        best_ask = asks[0][0]
        spread_pct = ((best_ask - best_bid) / best_bid) * 100
        
        # Büyük emirler var mı? (Top 5 bid ortalamadan 2x büyük)
        if len(bids) >= 5:
            avg_bid_size = sum([b[1] for b in bids]) / len(bids)
            big_bids = sum(1 for b in bids[:5] if b[1] > avg_bid_size * 2)
        else:
            big_bids = 0
        
        return {
            "bid_ask_ratio": round(bid_ask_ratio, 2),
            "spread_pct": round(spread_pct, 3),
            "big_bids": big_bids,
            "status": "OK" if bid_ask_ratio >= MIN_BID_ASK_RATIO and spread_pct <= MAX_SPREAD_PCT else "WEAK"
        }
        
    except Exception as e:
        print(f"⚠️ OB {symbol}: {str(e)[:30]}", flush=True)
        return None

# ============================================================
# ANA TARAMA
# ============================================================
async def scan_symbol(symbol):
    """Tek bir coin'i tara"""
    try:
        # Veri çek
        df_15m = await fetch_ohlcv_df(symbol, "15m", 100)
        df_1h = await fetch_ohlcv_df(symbol, "1h", 100)
        df_4h = await fetch_ohlcv_df(symbol, "4h", 100)
        
        if df_15m is None or len(df_15m) < 50:
            return None
        if df_1h is None or len(df_1h) < 50:
            return None
        if df_4h is None or len(df_4h) < 20:
            return None
        
        # İndikatörler
        df_15m = prepare_indicators(df_15m)
        df_1h = prepare_indicators(df_1h)
        df_4h = prepare_indicators(df_4h)
        
        # Skorlama
        trend = calculate_trend_score(df_1h, df_4h)
        accumulation = calculate_accumulation_score(df_15m, df_1h)
        squeeze = calculate_squeeze_score(df_1h)
        momentum = calculate_momentum_score(df_15m, df_1h)
        
        total_score = trend["score"] + accumulation["score"] + squeeze["score"] + momentum["score"]
        
        # Minimum skor
        if total_score < MIN_SCORE:
            return None
        
        # SR & Potansiyel
        current_price = float(df_15m["close"].iloc[-1])
        sr = find_resistance_support(df_1h, current_price)
        
        if not sr:
            return None
        
        potential_pct = ((sr["resistance"] - current_price) / current_price) * 100
        risk_pct = ((current_price - sr["support"]) / current_price) * 100
        
        if potential_pct < MIN_POTENTIAL:
            return None
        
        # Likidite
        ticker = await api_gate.call(exchange.fetch_ticker, symbol)
        if not ticker:
            return None
        
        liquidity = float(ticker.get("quoteVolume", 0) or 0)
        if liquidity < MIN_LIQUIDITY:
            return None
        
        # Order book kontrol
        ob = await check_order_book(symbol)
        
        if not ob or ob["status"] != "OK":
            stats["weak_orderbook"] += 1
            return None
        
        return {
            "symbol": symbol,
            "price": current_price,
            "total_score": round(total_score, 2),
            "trend": trend,
            "accumulation": accumulation,
            "squeeze": squeeze,
            "momentum": momentum,
            "resistance": sr["resistance"],
            "support": sr["support"],
            "potential_pct": potential_pct,
            "risk_pct": risk_pct,
            "liquidity": liquidity,
            "order_book": ob
        }
        
    except Exception as e:
        print(f"⚠️ Scan {symbol}: {str(e)[:50]}", flush=True)
        return None

async def scan_all_symbols(symbols):
    """Tüm coinleri tara"""
    print(f"🔍 Tarama başladı: {len(symbols)} coin", flush=True)
    
    candidates = []
    
    for i, symbol in enumerate(symbols, 1):
        if i % 50 == 0:
            print(f"-> {i}/{len(symbols)}", flush=True)
        
        # Cooldown kontrol
        last_ts = last_candidate_ts.get(symbol)
        if last_ts:
            tr_now = datetime.now(timezone.utc).astimezone(TR_TZ)
            hours = (tr_now.replace(tzinfo=None) - last_ts.replace(tzinfo=None)).total_seconds() / 3600
            if hours < CANDIDATE_COOLDOWN_HOURS:
                continue
        
        result = await scan_symbol(symbol)
        
        if result:
            candidates.append(result)
            print(f"✅ ADAY: {symbol} | {result['total_score']}/10", flush=True)
    
    print(f"📊 Tarama bitti: {len(candidates)} aday bulundu", flush=True)
    stats["last_scan_candidates"] = len(candidates)
    
    return candidates

# ============================================================
# TELEGRAM MESAJI
# ============================================================
def format_candidate_message(candidate, tr_time):
    symbol = candidate["symbol"]
    price = candidate["price"]
    score = candidate["total_score"]
    
    trend = candidate["trend"]
    accum = candidate["accumulation"]
    squeeze = candidate["squeeze"]
    momentum = candidate["momentum"]
    
    resistance = candidate["resistance"]
    support = candidate["support"]
    potential = candidate["potential_pct"]
    risk = candidate["risk_pct"]
    liq = candidate["liquidity"]
    ob = candidate["order_book"]
    
    price_s = fmt_price(symbol, price)
    res_s = fmt_price(symbol, resistance)
    sup_s = fmt_price(symbol, support)
    
    stars = "⭐⭐⭐" if score >= 7.5 else "⭐⭐"
    
    msg = f"""
🔍 <b>ADAY TESPİT EDİLDİ</b>

<b>#{symbol}</b>
💵 Fiyat: {price_s}
━━━━━━━━━━━━━━━━
{stars} <b>SKOR: {score}/10</b>

📊 <b>Skorlar:</b>
- Trend: {trend['score']}/3
- Birikim: {accum['score']}/3
- Sıkışma: {squeeze['score']}/2
- Momentum: {momentum['score']}/2

📈 <b>Order Book:</b>
- Alım/Satım: {ob['bid_ask_ratio']}x
- Spread: %{ob['spread_pct']}
- Büyük alım: {"✅ Var" if ob['big_bids'] > 0 else "❌ Yok"}

🎯 Direnç: {res_s} (+%{potential:.1f})
🛡️ Destek: {sup_s} (-%{risk:.1f})
💰 Likidite: ${liq/1e6:.1f}M

⚠️ <b>ADAY - Grafiği kontrol et!</b>

🕐 {tr_time.strftime("%d.%m %H:%M")}
""".strip()
    
    return msg

async def send_candidates(candidates):
    """Adayları Telegram'a gönder"""
    tr_now = datetime.now(timezone.utc).astimezone(TR_TZ)
    
    if not candidates:
        summary = f"""
📊 <b>TARAMA SONUCU</b>

🕐 {tr_now.strftime("%d.%m %H:%M")}

❌ Aday bulunamadı

📉 Eleme:
- Zayıf order book: {stats.get('weak_orderbook', 0)}
""".strip()
        send_telegram(summary)
        return
    
    # Her aday için mesaj gönder
    for candidate in candidates[:5]:  # Maksimum 5 aday
        msg = format_candidate_message(candidate, tr_now)
        send_telegram(msg)
        
        # Cooldown kaydet
        last_candidate_ts[candidate["symbol"]] = tr_now.replace(tzinfo=None)
        
        await asyncio.sleep(2)  # Telegram rate limit
    
    stats["candidates_sent"] += len(candidates[:5])

# ============================================================
# TARAMA LOOP
# ============================================================
async def scanner_loop():
    """Ana tarama döngüsü"""
    print("🚀 Hybrid Candidate System v8.0", flush=True)
    print(f"⏱️ Tarama: Her {SCAN_INTERVAL_HOURS} saatte", flush=True)
    
    # Sembol listesi
    symbols = await load_symbols_pool()
    if not symbols:
        print("❌ Sembol yok", flush=True)
        return
    
    print(f"✅ {len(symbols)} coin yüklendi", flush=True)
    
    while True:
        try:
            tr_now = datetime.now(timezone.utc).astimezone(TR_TZ)
            print(f"\n⏰ Tarama başladı: {tr_now.strftime('%H:%M')}", flush=True)
            
            # Tüm coinleri tara
            candidates = await scan_all_symbols(symbols)
            
            # Adayları gönder
            await send_candidates(candidates)
            
            print(f"✅ Tarama tamamlandı", flush=True)
            print(f"💤 Sonraki tarama: {SCAN_INTERVAL_HOURS} saat sonra\n", flush=True)
            
        except Exception as e:
            print(f"⚠️ Loop hatası: {str(e)[:100]}", flush=True)
        
        # Bekle
        await asyncio.sleep(SCAN_INTERVAL_HOURS * 3600)

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
    <head><style>
    body{{background:#1a1a1a;color:#fff;font-family:Arial;padding:20px}}
    h1{{color:#4CAF50}}
    .stat{{background:#2a2a2a;padding:15px;margin:10px 0;border-radius:8px}}
    </style></head>
    <body>
    <h1>🔍 Hybrid Candidate System v8.0</h1>
    <div class="stat">
    <p><b>Time:</b> {tr_now_str()}</p>
    <p><b>Son taramada aday:</b> {stats.get('last_scan_candidates', 0)}</p>
    <p><b>Toplam gönderilen:</b> {stats.get('candidates_sent', 0)}</p>
    <p><b>Zayıf order book:</b> {stats.get('weak_orderbook', 0)}</p>
    </div>
    <div class="stat">
    <p>⏱️ Tarama sıklığı: Her {SCAN_INTERVAL_HOURS} saat</p>
    <p>📊 Minimum skor: {MIN_SCORE}/10</p>
    <p>📈 Order book ratio: {MIN_BID_ASK_RATIO}x</p>
    </div>
    </body></html>
    """

@app.route("/health")
def health():
    return {"ok": True, "stats": dict(stats)}

def start_flask():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")), use_reloader=False)

# ============================================================
# MAIN
# ============================================================
if __name__ == "__main__":
    threading.Thread(target=start_flask, daemon=True).start()
    
    try:
        asyncio.run(scanner_loop())
    except KeyboardInterrupt:
        print("🛑 Stop", flush=True)
