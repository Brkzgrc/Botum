# -*- coding: utf-8 -*-
"""
Squeeze & Breakout Detector v3.0
Patlama 6-12 saat ÖNCE tespit eden sistem
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
# 0) KULLANICI AYARLARI
# ============================================================
BINANCE_API_KEY = ""
BINANCE_API_SECRET = ""

TELEGRAM_TOKEN = "7583261338:AAFwkpxsumCBpYI5Ai-aIiII6INm_thmg-I"
TELEGRAM_CHAT_ID = "5124859166"

# --- RİSK ---
ACCOUNT_SIZE = 5000.0
RISK_PERCENT = 2.0

# --- FİLTRELER ---
MIN_LIQUIDITY = 5_000_000        # Minimum $5M likidite
MIN_POTENTIAL_PCT = 12.0         # Minimum %12 potansiyel
MAX_RISK_PCT = 10.0              # Maksimum %10 risk
MIN_RR = 1.5                     # Minimum 1:1.5 RR

# Cooldown
SIGNAL_COOLDOWN_HOURS = 18       # Aynı coin'den 18 saatte 1 sinyal

MAX_SYMBOLS = None
USE_TOP_VOLUME_POOL = True

# --- WS / DATA ---
BOOTSTRAP_LIMIT_15M = 300
BOOTSTRAP_LIMIT_4H = 100
KEEP_BARS_15M = 300
KEEP_BARS_4H = 100
WS_KLINE_INTERVAL = "15m"
WS_STREAM_CHUNK = 120

TR_TZ = timezone(timedelta(hours=3))

# ============================================================
# 0.1) LOG / STATS
# ============================================================
BOOT_PROGRESS_EVERY = 20
PRINT_SIGNAL_LOG = True

stats = Counter()
ws_close_count = 0
tracked_symbols = []

def tr_now_str():
    return datetime.now(timezone.utc).astimezone(TR_TZ).strftime("%Y-%m-%d %H:%M:%S")

def print_summary():
    total = len(tracked_symbols) if tracked_symbols else 0
    total_closes = ws_close_count or 1
    
    print("\n📊 TARAMA SONUÇ ÖZETİ", flush=True)
    print("━━━━━━━━━━━━━━━━━━━━", flush=True)
    print(f"🧭 Takip edilen coin     : {total}", flush=True)
    print(f"🕯️ Son 15dk kapanış      : {total} coin", flush=True)
    print(f"🔍 Sıkışma tespit        : {stats.get('squeeze_detected',0)}", flush=True)
    print(f"✅ Gönderilen sinyal     : {stats.get('signal_sent',0)}", flush=True)
    print("— Eleme sebepleri —", flush=True)

    keys = [
        ("no_squeeze",           "Sıkışma yok"),
        ("no_accumulation",      "Birikim yok"),
        ("low_potential",        "Düşük potansiyel"),
        ("high_risk",            "Yüksek risk"),
        ("bad_rr",               "Kötü RR"),
        ("low_liquidity",        "Düşük likidite"),
        ("cooldown",             "Cooldown"),
        ("data_insufficient",    "Veri yetersiz"),
    ]
    
    any_printed = False
    for k, label in keys:
        v = stats.get(k, 0)
        if v:
            any_printed = True
            pct = (v / total_closes) * 100
            print(f"• {label:20s}: {v:5d} ({pct:5.1f}%)", flush=True)
    if not any_printed:
        print("• (Henüz eleme/olay yok)", flush=True)

    print("━━━━━━━━━━━━━━━━━━━━\n", flush=True)

# ============================================================
# 1) API GATE
# ============================================================
class ApiGate:
    def __init__(self, min_interval_sec=0.25, max_concurrent=3, max_retries=6):
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
                    await asyncio.sleep(backoff)
                except (ccxt.NetworkError, ccxt.RequestTimeout, ccxt.ExchangeNotAvailable) as e:
                    backoff = min(20.0, (0.8 * (2 ** attempt)))
                    await asyncio.sleep(backoff)
                except ccxt.ExchangeError as e:
                    backoff = min(12.0, (0.6 * (2 ** attempt)))
                    await asyncio.sleep(backoff)
                except Exception as e:
                    backoff = min(8.0, (0.5 * (2 ** attempt)))
                    await asyncio.sleep(backoff)
            raise RuntimeError("API call failed")

api_gate = ApiGate()

exchange = ccxt.binance({
    "apiKey": BINANCE_API_KEY or None,
    "secret": BINANCE_API_SECRET or None,
    "options": {"defaultType": "spot", "adjustForTimeDifference": True},
    "enableRateLimit": True,
    "timeout": 15000,
})

# ============================================================
# 2) UTILS
# ============================================================
def fmt_price(symbol: str, price) -> str:
    try:
        if price is None:
            return "N/A"
        return exchange.price_to_precision(symbol, float(price))
    except Exception:
        try:
            p = float(price)
            if p == 0:
                return "0"
            if p < 0.01:
                return f"{p:.8f}"
            if p < 1:
                return f"{p:.6f}"
            return f"{p:.4f}"
        except Exception:
            return "N/A"

# ============================================================
# 3) DATA STORE
# ============================================================
@dataclass
class Signal:
    symbol: str
    analysis: dict
    df_15m: pd.DataFrame
    df_4h: pd.DataFrame
    tr_time: datetime

bars_15m: dict[str, pd.DataFrame] = {}
bars_4h: dict[str, pd.DataFrame] = {}
last_signal_ts: dict[str, datetime] = {}

IGNORED_COINS = set([
    'UP/USDT','DOWN/USDT','BEAR/USDT','BULL/USDT',
    'USDC/USDT','TUSD/USDT','FDUSD/USDT','DAI/USDT','USDP/USDT',
    'EUR/USDT','TRY/USDT','GBP/USDT','BUSD/USDT','USTC/USDT',
    'PAXG/USDT','WBTC/USDT','USDE/USDT','BRL/USDT','RUB/USDT',
    'AUD/USDT','UST/USDT','USD/USDT','XUSD/USDT','USD1/USDT',
    'BFUSD/USDT',  # ← YENİ: Binance Funding USD (stablecoin)
])

# ============================================================
# 3.1) SIGNAL LOGGING - YENİ EKLEME
# ============================================================
SIGNAL_LOG_FILE = "/mnt/user-data/outputs/signal_log.json"

def save_signal_log(analysis: dict, tr_time: datetime):
    """Sinyali kaydet"""
    try:
        # Mevcut kayıtları oku
        if os.path.exists(SIGNAL_LOG_FILE):
            with open(SIGNAL_LOG_FILE, "r") as f:
                logs = json.load(f)
        else:
            logs = []
        
        # Yeni kayıt ekle
        log_entry = {
            "symbol": analysis["symbol"],
            "timestamp": tr_time.isoformat(),
            "entry_price": analysis["current_price"],
            "resistance_1": analysis["resistance_1"],
            "resistance_2": analysis["resistance_2"],
            "support": analysis["support"],
            "potential_pct": analysis["potential_pct"],
            "risk_pct": analysis["risk_pct"],
            "rr_ratio": analysis["rr_ratio"],
            "status": "ACTIVE"
        }
        
        logs.append(log_entry)
        
        # Kaydet
        os.makedirs(os.path.dirname(SIGNAL_LOG_FILE), exist_ok=True)
        with open(SIGNAL_LOG_FILE, "w") as f:
            json.dump(logs, f, indent=2)
            
    except Exception as e:
        print(f"⚠️ Log kayıt hatası: {e}", flush=True)

# ============================================================
# 4) MARKET POOL
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
    if not USE_TOP_VOLUME_POOL or not MAX_SYMBOLS:
        return syms[:MAX_SYMBOLS] if MAX_SYMBOLS else syms
    
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
    return syms_sorted[:MAX_SYMBOLS] if MAX_SYMBOLS else syms_sorted

# ============================================================
# 5) FETCH & PREPARE
# ============================================================
async def fetch_ohlcv_df(symbol: str, timeframe: str, limit: int):
    bars = await api_gate.call(exchange.fetch_ohlcv, symbol, timeframe=timeframe, limit=limit)
    df = pd.DataFrame(bars, columns=["timestamp","open","high","low","close","volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df.set_index("timestamp", inplace=True)
    return df

def prepare_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    # Temel indikatörler
    df["ema20"] = ta.ema(df["close"], length=20)
    df["ema50"] = ta.ema(df["close"], length=50)
    df["atr"] = ta.atr(df["high"], df["low"], df["close"], length=14)
    df["rsi"] = ta.rsi(df["close"], length=14)
    df["vol_ma"] = df["volume"].rolling(20, min_periods=1).mean()
    
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
    
    # BB Width (sıkışma tespiti)
    if "bb_upper" in df.columns and "bb_lower" in df.columns and "bb_mid" in df.columns:
        df["bb_width"] = ((df["bb_upper"] - df["bb_lower"]) / df["bb_mid"]) * 100
    
    return df

# ============================================================
# 6) ANALİZ FONKSİYONLARI
# ============================================================

def detect_squeeze(df_15m: pd.DataFrame) -> dict:
    """
    Sıkışma tespiti:
    - BB width düşük mü?
    - ATR düşüyor mu?
    - Fiyat dar range'de mi?
    """
    try:
        if len(df_15m) < 50:
            return {"detected": False, "reason": "Veri yetersiz"}
        
        last = df_15m.iloc[-1]

        # ✅ YENİ: Stablecoin kontrolü (ATR çok düşükse eleme)
        current_price = float(last["close"])
        atr = float(last["atr"])
        atr_pct = (atr / current_price) * 100
        
        if atr_pct < 0.15:  # ATR %0.15'ten düşükse stablecoin olabilir
            return {"detected": False, "reason": "ATR çok düşük (stablecoin?)"}        

        # 1) BB Width kontrolü
        if "bb_width" not in df_15m.columns or pd.isna(last["bb_width"]):
            return {"detected": False, "reason": "BB hesaplanamadı"}
        
        bb_width = float(last["bb_width"])
        bb_width_ma = df_15m["bb_width"].rolling(50).mean().iloc[-1]
        
        # BB son 50 mumun en darı mı?
        recent_50_bb = df_15m["bb_width"].iloc[-50:]
        is_narrowest = bb_width == recent_50_bb.min()
        
        # 2) ATR düşüyor mu?
        atr_now = float(last["atr"])
        atr_prev = float(df_15m["atr"].iloc[-5])
        atr_decreasing = atr_now < atr_prev
        
        # 3) Fiyat dar range'de mi? (son 20 mumda %10'dan az hareket)
        recent_20 = df_15m.iloc[-20:]
        range_high = float(recent_20["high"].max())
        range_low = float(recent_20["low"].min())
        range_pct = ((range_high - range_low) / range_low) * 100
        
        # 4) Hacim azalıyor mu? (satış baskısı bitiyor)
        vol_now = float(last["volume"])
        vol_ma = float(last["vol_ma"]) if not pd.isna(last["vol_ma"]) else 0
        vol_decreasing = vol_now < vol_ma * 0.8 if vol_ma > 0 else False
        
        # Sıkışma var mı?
        squeeze_detected = (
            bb_width < 4.0 and  # BB %4'ten dar
            (is_narrowest or bb_width < bb_width_ma * 0.7) and  # Son 50 mumun en darı VEYA ortalamanın %70 altında
            range_pct < 12.0 and  # Son 20 mumda %12'den az hareket
            atr_decreasing  # ATR düşüyor
        )
        
        if not squeeze_detected:
            return {"detected": False, "reason": "Sıkışma yok"}
        
        # Sıkışma süresi (kaç saattir dar range'de)
        squeeze_hours = 0
        for i in range(len(df_15m)-1, max(0, len(df_15m)-50), -1):
            if df_15m["bb_width"].iloc[i] < 4.5:
                squeeze_hours += 0.25  # 15 dakika
            else:
                break
        
        return {
            "detected": True,
            "bb_width": bb_width,
            "range_pct": range_pct,
            "squeeze_hours": squeeze_hours,
            "vol_decreasing": vol_decreasing,
            "score": 10 - bb_width  # Dar olduğu kadar yüksek skor
        }
        
    except Exception as e:
        return {"detected": False, "reason": f"Hata: {str(e)[:50]}"}

def detect_accumulation(df_15m: pd.DataFrame) -> dict:
    """
    Birikim tespiti:
    - Higher lows var mı? (dipte yükseliş)
    - Volume pattern pozitif mi?
    - RSI oversold'dan çıkıyor mu?
    """
    try:
        if len(df_15m) < 30:
            return {"detected": False, "reason": "Veri yetersiz"}
        
        last = df_15m.iloc[-1]
        recent_20 = df_15m.iloc[-20:]
        
        # 1) Higher lows tespiti (dipte yükseliş)
        lows = recent_20["low"].values
        higher_lows_count = 0
        for i in range(1, len(lows)):
            if lows[i] > lows[i-1]:
                higher_lows_count += 1
        
        higher_lows_ratio = higher_lows_count / (len(lows) - 1)
        has_higher_lows = higher_lows_ratio > 0.4  # %40'tan fazla yükselen dip
        
        # 2) Volume Delta (yeşil vs kırmızı hacim)
        green_volume = 0
        red_volume = 0
        for i in range(-20, 0):
            candle = df_15m.iloc[i]
            vol = float(candle["volume"])
            if float(candle["close"]) > float(candle["open"]):
                green_volume += vol
            else:
                red_volume += vol
        
        buy_pressure = (green_volume / (green_volume + red_volume)) if (green_volume + red_volume) > 0 else 0.5
        strong_buy_pressure = buy_pressure > 0.55  # %55'ten fazla alım hacmi
        
        # 3) RSI oversold'dan çıkış
        rsi = float(last["rsi"])
        rsi_prev = float(df_15m["rsi"].iloc[-5])
        rsi_rising_from_oversold = (rsi > 35 and rsi < 60 and rsi > rsi_prev)
        
        # Birikim var mı?
        accumulation_detected = has_higher_lows or (strong_buy_pressure and rsi_rising_from_oversold)
        
        if not accumulation_detected:
            return {"detected": False, "reason": "Birikim yok"}
        
        return {
            "detected": True,
            "higher_lows": has_higher_lows,
            "buy_pressure": buy_pressure * 100,
            "rsi": rsi,
            "score": (buy_pressure * 5) + (5 if has_higher_lows else 0)
        }
        
    except Exception as e:
        return {"detected": False, "reason": f"Hata: {str(e)[:50]}"}

def find_support_resistance_v2(df_15m: pd.DataFrame, current_price: float) -> dict:
    """
    Gerçek test edilmiş direnç/destek seviyeleri bul
    """
    try:
        if len(df_15m) < 100:
            return None
        
        lookback = min(200, len(df_15m))  # Daha fazla veri
        recent = df_15m.iloc[-lookback:]
        
        # 1) Fiyat seviyeleri ve kaç kez test edildiğini say
        highs = recent["high"].values
        lows = recent["low"].values
        closes = recent["close"].values
        
        # Fiyat aralıklarını oluştur (tolerance ile)
        tolerance = current_price * 0.01  # %1 tolerans
        
        resistance_tests = {}
        support_tests = {}
        
        # Her mumda fiyatın hangi seviyelere yaklaştığını say
        for i in range(len(recent)):
            high = highs[i]
            low = lows[i]
            
            # Resistance test (yukarı dokunma)
            for level in resistance_tests.keys():
                if abs(high - level) < tolerance:
                    resistance_tests[level] += 1
            
            # Yeni level ekle
            if high > current_price:
                # Yakın level var mı kontrol et
                found = False
                for level in resistance_tests.keys():
                    if abs(high - level) < tolerance:
                        found = True
                        break
                if not found:
                    resistance_tests[high] = 1
            
            # Support test (aşağı dokunma)
            for level in support_tests.keys():
                if abs(low - level) < tolerance:
                    support_tests[level] += 1
            
            if low < current_price:
                found = False
                for level in support_tests.keys():
                    if abs(low - level) < tolerance:
                        found = True
                        break
                if not found:
                    support_tests[low] = 1
        
        # 2) En çok test edilen seviyeleri bul
        # En az 3 kez test edilmiş olmalı
        strong_resistances = [(lvl, cnt) for lvl, cnt in resistance_tests.items() if cnt >= 3]
        strong_supports = [(lvl, cnt) for lvl, cnt in support_tests.items() if cnt >= 3]
        
        # Eğer yeterli güçlü seviye yoksa, en az 2 kez test edileni al
        if not strong_resistances:
            strong_resistances = [(lvl, cnt) for lvl, cnt in resistance_tests.items() if cnt >= 2]
        if not strong_supports:
            strong_supports = [(lvl, cnt) for lvl, cnt in support_tests.items() if cnt >= 2]
        
        # Hala yoksa, en yakınları al
        if not strong_resistances:
            upper = [lvl for lvl in resistance_tests.keys() if lvl > current_price]
            strong_resistances = [(lvl, 1) for lvl in sorted(upper)[:2]] if upper else []
        
        if not strong_supports:
            lower = [lvl for lvl in support_tests.keys() if lvl < current_price]
            strong_supports = [(lvl, 1) for lvl in sorted(lower, reverse=True)[:2]] if lower else []
        
        # 3) En yakın ve en güçlü seviyeleri seç
        if not strong_resistances:
            r1 = current_price * 1.08  # Fallback
            r2 = current_price * 1.15
        else:
            # En yakın ve en güçlü direnç
            resistances_sorted = sorted(strong_resistances, key=lambda x: (x[0] - current_price, -x[1]))
            r1 = resistances_sorted[0][0]
            r2 = resistances_sorted[1][0] if len(resistances_sorted) > 1 else r1 * 1.05
        
        if not strong_supports:
            s1 = current_price * 0.92  # Fallback
        else:
            supports_sorted = sorted(strong_supports, key=lambda x: (current_price - x[0], -x[1]))
            s1 = supports_sorted[0][0]
        
        # 4) Mantık kontrolü: Direnç çok uzaksa (>%30), yakınlaştır
        r1_distance = ((r1 - current_price) / current_price) * 100
        if r1_distance > 30:
            r1 = current_price * 1.15  # Max %15 uzakta
            r2 = r1 * 1.05
        
        # 5) Mantık kontrolü: Destek çok uzaksa (>%15), yakınlaştır
        s1_distance = ((current_price - s1) / current_price) * 100
        if s1_distance > 15:
            s1 = current_price * 0.90  # Max %10 aşağıda
        
        return {
            "resistance_1": float(r1),
            "resistance_2": float(r2),
            "support_1": float(s1),
            "current": float(current_price)
        }
        
    except Exception as e:
        print(f"⚠️ SR hesaplama hatası: {str(e)[:80]}", flush=True)
        return None

def check_4h_trend(df_4h: pd.DataFrame) -> dict:
    """
    4h trend kontrolü:
    - Uptrend mi, downtrend mi?
    - EMA alignment
    """
    try:
        if df_4h is None or len(df_4h) < 50:
            return {"status": "UNKNOWN", "reason": "Veri yok"}
        
        last = df_4h.iloc[-1]
        
        ema20 = float(last["ema20"])
        ema50 = float(last["ema50"])
        close = float(last["close"])
        rsi = float(last["rsi"])
        
        # Trend belirleme
        if close > ema20 and ema20 > ema50:
            trend = "UPTREND"
        elif close < ema20 and ema20 < ema50:
            trend = "DOWNTREND"
        else:
            trend = "SIDEWAYS"
        
        # RSI momentum
        if rsi > 50:
            momentum = "BULLISH"
        elif rsi < 50:
            momentum = "BEARISH"
        else:
            momentum = "NEUTRAL"
        
        return {
            "status": trend,
            "momentum": momentum,
            "rsi": rsi
        }
        
    except Exception:
        return {"status": "UNKNOWN", "reason": "Hesaplama hatası"}

async def check_order_book(symbol: str) -> dict:
    """
    Order book analizi:
    - Bid/Ask dengesi
    - Destek/Direnç gücü
    """
    try:
        ob = await api_gate.call(exchange.fetch_order_book, symbol, 20)
        
        bids = ob.get("bids", [])[:20]
        asks = ob.get("asks", [])[:20]
        
        if not bids or not asks:
            return {"status": "UNKNOWN"}
        
        # Toplam hacim
        bid_volume = sum([b[1] for b in bids])
        ask_volume = sum([a[1] for a in asks])
        
        total = bid_volume + ask_volume
        if total == 0:
            return {"status": "UNKNOWN"}
        
        bid_ratio = bid_volume / total
        
        # Spread
        best_bid = bids[0][0]
        best_ask = asks[0][0]
        spread_pct = ((best_ask - best_bid) / best_bid) * 100
        
        # Değerlendirme
        if bid_ratio > 0.6:
            balance = "STRONG_BID"
        elif bid_ratio < 0.4:
            balance = "STRONG_ASK"
        else:
            balance = "BALANCED"
        
        return {
            "status": "OK",
            "balance": balance,
            "bid_ratio": bid_ratio * 100,
            "spread_pct": spread_pct
        }
        
    except Exception:
        return {"status": "ERROR"}

# ============================================================
# 7) ANA ANALİZ - SİNYAL ÜRETİMİ
# ============================================================
async def analyze_symbol(symbol: str, df_15m: pd.DataFrame, df_4h: pd.DataFrame) -> dict:
    """
    Tam analiz:
    1. Sıkışma var mı?
    2. Birikim var mı?
    3. Potansiyel ne kadar?
    4. Risk ne kadar?
    5. 4h trend uygun mu?
    """
    try:
        # 1) Sıkışma kontrolü
        squeeze = detect_squeeze(df_15m)
        if not squeeze["detected"]:
            stats["no_squeeze"] += 1
            return None
        
        stats["squeeze_detected"] += 1
        
        # 2) Birikim kontrolü
        accumulation = detect_accumulation(df_15m)
        if not accumulation["detected"]:
            stats["no_accumulation"] += 1
            return None
        
        # 3) Fiyat & SR seviyeleri
        current_price = float(df_15m["close"].iloc[-1])
        sr = find_support_resistance(df_15m, current_price)
        
        if sr is None:
            stats["data_insufficient"] += 1
            return None
        
        # 4) Potansiyel & Risk hesaplama
        r1 = sr["resistance_1"]
        r2 = sr["resistance_2"]
        s1 = sr["support_1"]
        
        potential_pct = ((r1 - current_price) / current_price) * 100
        risk_pct = ((current_price - s1) / current_price) * 100
        
        if potential_pct < MIN_POTENTIAL_PCT:
            stats["low_potential"] += 1
            return None
        
        if risk_pct > MAX_RISK_PCT:
            stats["high_risk"] += 1
            return None
        
        rr_ratio = potential_pct / risk_pct if risk_pct > 0 else 0
        
        if rr_ratio < MIN_RR:
            stats["bad_rr"] += 1
            return None
        
        # 5) 4h trend
        trend_4h = check_4h_trend(df_4h)
        
        # 6) Order book
        ob = await check_order_book(symbol)
        
        # 7) Liquidity check
        try:
            ticker = await api_gate.call(exchange.fetch_ticker, symbol)
            liquidity = float(ticker.get("quoteVolume", 0) or 0)
            
            if liquidity < MIN_LIQUIDITY:
                stats["low_liquidity"] += 1
                return None
        except Exception:
            liquidity = 0
        
        # Analiz tamamlandı!
        return {
            "symbol": symbol,
            "current_price": current_price,
            "entry_zone_low": current_price * 0.99,
            "entry_zone_high": current_price * 1.01,
            "resistance_1": r1,
            "resistance_2": r2,
            "support": s1,
            "potential_pct": potential_pct,
            "risk_pct": risk_pct,
            "rr_ratio": rr_ratio,
            "squeeze": squeeze,
            "accumulation": accumulation,
            "trend_4h": trend_4h,
            "order_book": ob,
            "liquidity": liquidity
        }
        
    except Exception as e:
        print(f"⚠️ Analiz hatası {symbol}: {str(e)[:100]}", flush=True)
        return None

# ============================================================
# 8) TELEGRAM
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
            print(f"⚠️ Telegram: {r.status_code}", flush=True)
    except Exception as e:
        print(f"⚠️ Telegram: {e}", flush=True)

def format_signal_message(analysis: dict, tr_time: datetime) -> str:
    """
    Detaylı sinyal mesajı oluştur
    """
    symbol = analysis["symbol"]
    current = analysis["current_price"]
    entry_low = analysis["entry_zone_low"]
    entry_high = analysis["entry_zone_high"]
    r1 = analysis["resistance_1"]
    r2 = analysis["resistance_2"]
    support = analysis["support"]
    potential = analysis["potential_pct"]
    risk = analysis["risk_pct"]
    rr = analysis["rr_ratio"]
    
    squeeze = analysis["squeeze"]
    accum = analysis["accumulation"]
    trend = analysis["trend_4h"]
    ob = analysis["order_book"]
    liq = analysis["liquidity"]
    
    # Fiyat formatla
    current_s = fmt_price(symbol, current)
    entry_low_s = fmt_price(symbol, entry_low)
    entry_high_s = fmt_price(symbol, entry_high)
    r1_s = fmt_price(symbol, r1)
    r2_s = fmt_price(symbol, r2)
    support_s = fmt_price(symbol, support)
    
    # Likidite mesajı
    if liq < 10_000_000:
        liq_msg = f"⚠️ Likidite: ${liq/1e6:.1f}M"
    elif liq < 25_000_000:
        liq_msg = f"ℹ️ Likidite: ${liq/1e6:.1f}M"
    else:
        liq_msg = f"✅ Likidite: ${liq/1e6:.1f}M"
    
    # Order book mesajı
    if ob["status"] == "OK":
        if ob["balance"] == "STRONG_BID":
            ob_msg = f"✅ Alım baskısı güçlü (%{ob['bid_ratio']:.0f})"
        elif ob["balance"] == "STRONG_ASK":
            ob_msg = f"⚠️ Satım baskısı var (%{ob['bid_ratio']:.0f})"
        else:
            ob_msg = f"ℹ️ Dengeli order book"
    else:
        ob_msg = ""
    
    msg = f"""
⚡ <b>SIKIŞMA - PATLAMA BEKLENİYOR</b>

<b>#{symbol}</b>
💵 Şu an: {current_s}
📍 Giriş Bölgesi: {entry_low_s} - {entry_high_s}
━━━━━━━━━━━━━━━━
🎯 1. Direnç: {r1_s}
🎯 2. Direnç: {r2_s} (kırarsa)
🛡️ Destek: {support_s}
━━━━━━━━━━━━━━━━
📊 Potansiyel: %{potential:.1f} | Risk: %{risk:.1f}
📈 RR: 1:{rr:.1f}
━━━━━━━━━━━━━━━━
🔍 <b>ANALİZ:</b>
- {squeeze['squeeze_hours']:.1f} saattir sıkışmada (BB: %{squeeze['bb_width']:.1f})
- Range: %{squeeze['range_pct']:.1f} | {"Hacim azalıyor" if squeeze.get('vol_decreasing') else "Hacim normal"}
- {"✅ Dipte yükseliş var" if accum.get('higher_lows') else "⚠️ Dipte yükseliş yok"}
- Alım hacmi: %{accum['buy_pressure']:.0f} | RSI: {accum['rsi']:.0f}
- 4h Trend: {trend['status']} ({trend['momentum']})
{("• " + ob_msg) if ob_msg else ""}

{liq_msg}

🕐 {tr_time.strftime("%d.%m %H:%M")}
""".strip()
    
    return msg

# ============================================================
# 9) FLASK & HEARTBEAT
# ============================================================
app = Flask(__name__)
bot_status = {"last_run": "Başlamadı", "status": "BOOT", "signal_count": 0}

import logging
logging.getLogger("werkzeug").setLevel(logging.ERROR)
app.logger.disabled = True

heartbeat = {
    "last_beat_tr": None,
    "last_beat_epoch": time.time(),
    "last_symbol": None,
    "status": "BOOT"
}

WATCHDOG_STALE_SEC = 600
_last_beat_ts = 0.0

def beat(symbol=None, status=None):
    global _last_beat_ts
    now = time.time()
    if now - _last_beat_ts >= 10:
        heartbeat["last_beat_tr"] = tr_now_str()
        heartbeat["last_beat_epoch"] = time.time()
        if symbol:
            heartbeat["last_symbol"] = symbol
        if status:
            heartbeat["status"] = status
        _last_beat_ts = now

def heartbeat_pinger():
    while True:
        heartbeat["last_beat_tr"] = tr_now_str()
        heartbeat["last_beat_epoch"] = time.time()
        time.sleep(15)

def watchdog_thread():
    while True:
        try:
            last_epoch = float(heartbeat.get("last_beat_epoch", 0))
            stale = time.time() - last_epoch
            if stale > WATCHDOG_STALE_SEC:
                print(f"🛑 WATCHDOG: {int(stale)}s stale. Restarting...", flush=True)
                os._exit(1)
            time.sleep(10)
        except Exception:
            time.sleep(10)

@app.route("/")
def home():
    now = datetime.now(TR_TZ).strftime("%H:%M:%S")
    return f"""
    <h1>🚀 Squeeze Detector v3.0</h1>
    <p><b>Durum:</b> {bot_status['status']}</p>
    <p><b>Son:</b> {bot_status['last_run']}</p>
    <p><b>Sinyal:</b> {bot_status['signal_count']}</p>
    <p><b>Saat:</b> {now}</p>
    <hr>
    <p><b>Heartbeat:</b> {heartbeat.get('last_beat_tr')}</p>
    <p><b>Son Coin:</b> {heartbeat.get('last_symbol')}</p>
    """

@app.route("/health")
def health():
    return {"bot_status": bot_status, "heartbeat": heartbeat, "stats": dict(stats)}

@app.route("/backtest")
def backtest_report():
    """Verilen sinyallerin performans raporu"""
    try:
        if not os.path.exists(SIGNAL_LOG_FILE):
            return "<h1>📊 Henüz sinyal yok</h1><p>Bot sinyal verdiğinde burada görünecek.</p>"
        
        with open(SIGNAL_LOG_FILE, "r") as f:
            logs = json.load(f)
        
        if not logs:
            return "<h1>📊 Henüz sinyal yok</h1><p>Bot sinyal verdiğinde burada görünecek.</p>"
        
        # Her sinyalin mevcut durumunu kontrol et
        results = []
        for log in logs:
            symbol = log["symbol"]
            entry = log["entry_price"]
            r1 = log["resistance_1"]
            support = log["support"]
            
            # Şu anki fiyatı al
            try:
                ticker = exchange.fetch_ticker(symbol)
                current = ticker["last"]
                
                # Sonuç hesapla
                if current >= r1:
                    result = "WIN"
                    pnl = ((current - entry) / entry) * 100
                elif current <= support:
                    result = "LOSS"
                    pnl = ((current - entry) / entry) * 100
                else:
                    result = "ACTIVE"
                    pnl = ((current - entry) / entry) * 100
                
                results.append({
                    **log,
                    "current_price": current,
                    "pnl_pct": pnl,
                    "result": result
                })
            except Exception as e:
                results.append({**log, "result": "ERROR", "error": str(e)[:50]})
        
        # HTML rapor oluştur
        html = """
        <html>
        <head>
            <title>Backtest Raporu</title>
            <style>
                body { font-family: Arial; padding: 20px; background: #1a1a1a; color: #fff; }
                h1 { color: #4CAF50; }
                .stats { background: #2a2a2a; padding: 15px; border-radius: 8px; margin: 20px 0; }
                .signal { background: #2a2a2a; padding: 10px; margin: 10px 0; border-radius: 5px; border-left: 4px solid #666; }
                .win { border-left-color: #4CAF50; }
                .loss { border-left-color: #f44336; }
                .active { border-left-color: #ff9800; }
            </style>
        </head>
        <body>
        """
        
        html += "<h1>📊 Backtest Raporu</h1>"
        
        wins = [r for r in results if r.get("result") == "WIN"]
        losses = [r for r in results if r.get("result") == "LOSS"]
        active = [r for r in results if r.get("result") == "ACTIVE"]
        errors = [r for r in results if r.get("result") == "ERROR"]
        
        html += '<div class="stats">'
        html += f"<p><b>Toplam Sinyal:</b> {len(results)}</p>"
        html += f"<p>✅ <b>Kazananlar:</b> {len(wins)}</p>"
        html += f"<p>❌ <b>Kaybedenler:</b> {len(losses)}</p>"
        html += f"<p>⏳ <b>Devam Edenler:</b> {len(active)}</p>"
        
        if wins or losses:
            win_rate = (len(wins) / (len(wins) + len(losses))) * 100
            avg_win = sum([r["pnl_pct"] for r in wins]) / len(wins) if wins else 0
            avg_loss = sum([r["pnl_pct"] for r in losses]) / len(losses) if losses else 0
            
            html += f"<p><b>📈 Win Rate:</b> %{win_rate:.1f}</p>"
            html += f"<p><b>💰 Ortalama Kazanç:</b> %{avg_win:.1f}</p>"
            html += f"<p><b>💸 Ortalama Kayıp:</b> %{avg_loss:.1f}</p>"
        
        html += '</div>'
        
        html += "<h2>📋 Sinyal Detayları:</h2>"
        
        # Sinyalleri yeniden eskiye sırala
        results_sorted = sorted(results, key=lambda x: x.get("timestamp", ""), reverse=True)
        
        for r in results_sorted:
            status_emoji = {"WIN": "✅", "LOSS": "❌", "ACTIVE": "⏳", "ERROR": "❓"}.get(r.get("result"), "❓")
            css_class = r.get("result", "").lower()
            
            html += f'<div class="signal {css_class}">'
            html += f"<p><b>{status_emoji} {r['symbol']}</b></p>"
            html += f"<p>📅 {r['timestamp'][:16]}</p>"
            html += f"<p>💵 Giriş: {r['entry_price']:.8f}</p>"
            
            if "current_price" in r:
                html += f"<p>💵 Şu an: {r['current_price']:.8f}</p>"
            if "pnl_pct" in r:
                pnl_color = "green" if r["pnl_pct"] > 0 else "red"
                html += f"<p>📊 PnL: <span style='color:{pnl_color}'><b>%{r['pnl_pct']:.1f}</b></span></p>"
            
            html += f"<p>🎯 Hedef: {r['resistance_1']:.8f} (Pot: %{r['potential_pct']:.1f})</p>"
            html += f"<p>🛡️ Stop: {r['support']:.8f} (Risk: %{r['risk_pct']:.1f})</p>"
            html += f"<p>📊 RR: 1:{r['rr_ratio']:.1f}</p>"
            
            if r.get("result") == "ERROR":
                html += f"<p>⚠️ Hata: {r.get('error', 'Bilinmeyen')}</p>"
            
            html += '</div>'
        
        html += "</body></html>"
        
        return html
        
    except Exception as e:
        return f"<h1>⚠️ Hata</h1><p>{str(e)}</p>"

# ============================================================
# 10) BOOTSTRAP
# ============================================================

async def bootstrap_symbol(symbol: str):
    try:
        df_15m = await fetch_ohlcv_df(symbol, "15m", BOOTSTRAP_LIMIT_15M)
        df_4h = await fetch_ohlcv_df(symbol, "4h", BOOTSTRAP_LIMIT_4H)
        
        if df_15m is None or len(df_15m) < 100:
            return False
        if df_4h is None or len(df_4h) < 50:
            return False
        
        df_15m = prepare_indicators(df_15m)
        df_4h = prepare_indicators(df_4h)
        
        if len(df_15m) > KEEP_BARS_15M:
            df_15m = df_15m.iloc[-KEEP_BARS_15M:]
        if len(df_4h) > KEEP_BARS_4H:
            df_4h = df_4h.iloc[-KEEP_BARS_4H:]
        
        bars_15m[symbol] = df_15m
        bars_4h[symbol] = df_4h
        return True
        
    except Exception:
        return False

async def bootstrap_all(symbols: list[str]):
    ok = 0
    total = len(symbols)
    bot_status["status"] = "BOOTSTRAP"
    print(f"🧱 Hazırlık | toplam={total}", flush=True)
    
    for i, s in enumerate(symbols, 1):
        beat(symbol=s, status="BOOTSTRAP")
        if i % BOOT_PROGRESS_EVERY == 0:
            print(f"-> {i}/{total} ({s})", flush=True)
        got = await bootstrap_symbol(s)
        if got:
            ok += 1
    
    print(f"✅ Hazır | ok={ok}/{total}", flush=True)

# ============================================================
# 11) WS LISTENER
# ============================================================
def to_ws_symbol(symbol: str) -> str:
    return symbol.replace("/", "").lower()

async def ws_listen_klines(symbols: list[str], candidate_queue: asyncio.Queue):
    streams = "/".join([f"{to_ws_symbol(s)}@kline_{WS_KLINE_INTERVAL}" for s in symbols])
    url = f"wss://stream.binance.com:9443/stream?streams={streams}"
    bot_status["status"] = "LIVE"
    print(f"🛰️ Canlı veri | coins={len(symbols)}", flush=True)
    
    retry_count = 0
    while True:
        try:
            async with websockets.connect(url, ping_interval=30, ping_timeout=30) as ws:
                retry_count = 0
                print("✅ Bağlantı OK", flush=True)
                while True:
                    msg = await ws.recv()
                    data = json.loads(msg)
                    payload = data.get("data", {})
                    k = payload.get("k", {})
                    
                    if not k.get("x", False):
                        continue
                    
                    sym_raw = payload.get("s", "")
                    symbol = sym_raw.upper().replace("USDT", "/USDT")
                    
                    ts_ms = int(k.get("t"))
                    o = float(k.get("o"))
                    h = float(k.get("h"))
                    l = float(k.get("l"))
                    c = float(k.get("c"))
                    v = float(k.get("v"))
                    
                    df_15m = bars_15m.get(symbol)
                    if df_15m is None or len(df_15m) < 50:
                        stats["data_insufficient"] += 1
                        continue
                    
                    tstamp = pd.to_datetime(ts_ms, unit="ms", utc=True)
                    df_15m.loc[tstamp, ["open","high","low","close","volume"]] = [o,h,l,c,v]
                    df_15m = df_15m.sort_index()
                    
                    if len(df_15m) > KEEP_BARS_15M:
                        df_15m = df_15m.iloc[-KEEP_BARS_15M:]
                    
                    df_15m = prepare_indicators(df_15m)
                    bars_15m[symbol] = df_15m
                    
                    await evaluate_on_close(symbol, df_15m, candidate_queue)
                    
        except Exception as e:
            retry_count += 1
            backoff = min(60, 5 * (2 ** min(retry_count, 4)))
            print(f"⚠️ WS koptu (#{retry_count}): {backoff}s", flush=True)
            bot_status["status"] = "RECONNECTING"
            await asyncio.sleep(backoff)

async def ws_listen_klines_multi(symbols: list[str], candidate_queue: asyncio.Queue):
    tasks = []
    for i in range(0, len(symbols), WS_STREAM_CHUNK):
        part = symbols[i:i+WS_STREAM_CHUNK]
        tasks.append(asyncio.create_task(ws_listen_klines(part, candidate_queue)))
    await asyncio.gather(*tasks)

# ============================================================
# 12) EVALUATION & WORKER
# ============================================================
async def evaluate_on_close(symbol: str, df_15m: pd.DataFrame, candidate_queue: asyncio.Queue):
    global ws_close_count
    try:
        ws_close_count += 1
        tr_now = datetime.now(timezone.utc).astimezone(TR_TZ)
        bot_status["last_run"] = tr_now.strftime("%H:%M:%S")
        beat(symbol=symbol, status="RUN")
        
        # Cooldown kontrolü
        last_ts = last_signal_ts.get(symbol)
        if last_ts:
            hours_passed = (tr_now.replace(tzinfo=None) - last_ts.replace(tzinfo=None)).total_seconds() / 3600
            if hours_passed < SIGNAL_COOLDOWN_HOURS:
                stats["cooldown"] += 1
                return
        
        # 4h veri
        df_4h = bars_4h.get(symbol)
        if df_4h is None or len(df_4h) < 50:
            stats["data_insufficient"] += 1
            return
        
        # Analiz
        analysis = await analyze_symbol(symbol, df_15m, df_4h)
        
        if analysis is None:
            return
        
        # SİNYAL BULUNDU!
        print(f"🔍 SİNYAL ADAYI: {symbol} | Pot:%{analysis['potential_pct']:.1f} RR:1:{analysis['rr_ratio']:.1f}", flush=True)
        
        await candidate_queue.put(Signal(
            symbol=symbol,
            analysis=analysis,
            df_15m=df_15m.copy(),
            df_4h=df_4h.copy(),
            tr_time=tr_now
        ))
        
    except Exception as e:
        print(f"⚠️ Eval error {symbol}: {str(e)[:80]}", flush=True)

async def signal_worker(candidate_queue: asyncio.Queue):
    """
    Aday sinyalleri işle ve Telegram'a gönder
    """
    while True:
        sig: Signal = await candidate_queue.get()
        try:
            symbol = sig.symbol
            analysis = sig.analysis
            tr_time = sig.tr_time
            
            # Mesaj oluştur
            msg = format_signal_message(analysis, tr_time)
            
            # Gönder
            send_telegram(msg)
            
            # ✅ YENİ: Sinyali kaydet
            save_signal_log(analysis, tr_time)
            
            # Cooldown kaydet
            last_signal_ts[symbol] = tr_time.replace(tzinfo=None)
            
            # İstatistik
            stats["signal_sent"] += 1
            bot_status["signal_count"] += 1
            
            if PRINT_SIGNAL_LOG:
                print(f"✅ SİNYAL GÖNDERİLDİ: {symbol}", flush=True)
            
        except Exception as e:
            print(f"⚠️ Worker error: {str(e)[:100]}", flush=True)
        finally:
            candidate_queue.task_done()

# ============================================================
# 13) MAIN
# ============================================================
def start_flask():
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, use_reloader=False)

async def main():
    print("🚀 Squeeze Detector v3.0", flush=True)
    
    symbols = await load_symbols_pool()
    if not symbols:
        print("🛑 Sembol yok", flush=True)
        return
    
    global tracked_symbols
    tracked_symbols = list(symbols)
    
    print(f"✅ Coins: {len(symbols)}", flush=True)
    
    await bootstrap_all(symbols)
    
    print_summary()
    
    candidate_queue = asyncio.Queue()
    asyncio.create_task(signal_worker(candidate_queue))
    
    # Periyodik özet
    async def periodic_summary():
        while True:
            await asyncio.sleep(600)
            print_summary()
    
    asyncio.create_task(periodic_summary())
    
    await ws_listen_klines_multi(symbols, candidate_queue)

if __name__ == "__main__":
    threading.Thread(target=start_flask, daemon=True).start()
    threading.Thread(target=heartbeat_pinger, daemon=True).start()
    threading.Thread(target=watchdog_thread, daemon=True).start()
    
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("🛑 Durduruldu", flush=True)
