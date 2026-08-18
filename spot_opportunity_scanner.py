# -*- coding: utf-8 -*-
"""
SPOT Opportunity Scanner
========================

Binance Spot USDT paritelerinde, piyasa rejimini kesin filtre yapmadan manuel
incelemeye değer kısa vadeli fırsatları arar. Sistem emir vermez. Adayları
Telegram'a ve mevcut Portfolio Tracker `/api/signal` hattına gönderir.

Temel yaklaşım:
  - 1H giriş zamanlaması, 4H bağlam, 1D/1W yapısal bölge analizi
  - Hiçbir timeframe veya BTC yönü tek başına veto değildir
  - Benzer osilatörler bağımsız oy gibi sayılmaz; aile puanı üretilir
  - Stop sabit yüzdeyle değil, giriş altındaki anlamlı desteğin %2-3 altından
  - Hedef, giriş üstündeki ilk anlamlı tepki/direnç bölgesinden
  - Çıktı "AL" değil, "İNCELEME ADAYI"dır

Mevcut dosyalardan import yapmaz; tek dosya olarak Render'da çalışır.
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np
import pandas as pd
import requests
from flask import Flask, jsonify


# =============================================================================
# AYARLAR
# =============================================================================

BINANCE_API = "https://api.binance.com"
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TELEGRAM_THREAD_ID = int(os.getenv("SIGNAL_THREAD_ID", "2"))
PORTFOLIO_URL = os.getenv("PORTFOLIO_URL", "").rstrip("/")
PORTFOLIO_TOKEN = os.getenv("PORTFOLIO_TOKEN", "")

SCAN_INTERVAL_MIN = int(os.getenv("SCAN_INTERVAL_MIN", "60"))
SCAN_ON_START = os.getenv("SCAN_ON_START", "true").lower() == "true"
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"
MAX_WORKERS = max(1, min(8, int(os.getenv("MAX_WORKERS", "4"))))
MIN_QUOTE_VOLUME = float(os.getenv("MIN_QUOTE_VOLUME", "1000000"))
COOLDOWN_HOURS = float(os.getenv("SIGNAL_COOLDOWN_HOURS", "4"))
SUPPORT_BUFFER_PCT = float(os.getenv("SUPPORT_BUFFER_PCT", "2.5"))
STATE_FILE = os.getenv("SCANNER_STATE_FILE", "/tmp/spot_opportunity_state.json")

TR_TZ = timezone(timedelta(hours=3))
HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "Botum-SpotOpportunityScanner/1.0"})

IGNORED_BASES = {
    "USDT", "USDC", "BUSD", "TUSD", "DAI", "PAX", "HUSD", "USDP",
    "GUSD", "FDUSD", "EUR", "TRY", "GBP", "USD", "BRL", "RUB", "AUD",
    "XUSD", "USD1", "USDE", "BFUSD", "USDS", "USDD", "PYUSD", "AEUR",
    "EURI", "USTC", "FRAX", "LUSD", "SUSD", "USDX", "CUSD", "OUSD",
    "MUSD", "RLUSD", "BIDR", "IDRT", "VAI",
    "PAXG", "XAUT", "WBTC", "WETH", "WBNB", "BETH",
    "BTCB", "HBTC", "U",
}
LEVERAGED_SUFFIXES = ("UP", "DOWN", "BULL", "BEAR", "2L", "2S", "3L", "3S", "5L", "5S", "10L", "10S")
# Binance Spot evrenine dönemsel olarak eklenen tokenlaştırılmış hisse/ETF
# sembolleri kripto coin taramasına dahil edilmez.
# Binance bStock sembolleri B ile biter. Aşağıdaki gerçek kripto varlıklar da
# doğal olarak B ile bittiği için genelleştirilmiş bStock filtresinden muaftır.
CRYPTO_BASES_ENDING_B = {"BNB", "DGB", "TRB", "CKB", "SHIB", "ARB", "BB", "YB"}

app = Flask(__name__)
logging.getLogger("werkzeug").setLevel(logging.ERROR)

runtime = {
    "status": "BOOT",
    "dry_run": DRY_RUN,
    "last_scan_start": None,
    "last_scan_end": None,
    "symbols": 0,
    "deep_scanned": 0,
    "candidates": 0,
    "sent": 0,
    "last_error": None,
}


# =============================================================================
# VERİ MODELLERİ
# =============================================================================

@dataclass
class Zone:
    low: float
    high: float
    center: float
    strength: float
    timeframes: list[str] = field(default_factory=list)
    touches: int = 1


@dataclass
class Candidate:
    symbol: str
    price: float
    entry_low: float
    entry_high: float
    support: Zone
    stop: float
    resistance: Zone
    target_low: float
    target_high: float
    target_pct: float
    stop_pct: float
    rr: float
    setup: str
    observed_setups: list[str]
    positives: list[str]
    risks: list[str]
    historical_notes: list[str]
    tf_summary: dict[str, str]
    metrics: dict[str, Any]


# =============================================================================
# YARDIMCI FONKSİYONLAR
# =============================================================================

def tr_now() -> datetime:
    return datetime.now(timezone.utc).astimezone(TR_TZ)


def fmt_price(v: float) -> str:
    if v >= 1000:
        return f"{v:,.2f}"
    if v >= 100:
        return f"{v:.2f}"
    if v >= 1:
        return f"{v:.4f}"
    if v >= 0.01:
        return f"{v:.6f}"
    return f"{v:.10f}".rstrip("0")


def clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, float(v)))


def safe_float(v: Any, default: float = 0.0) -> float:
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def load_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_state(data: dict) -> None:
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_FILE)
    except Exception as exc:
        print(f"[STATE] Yazma hatası: {exc}", flush=True)


def in_cooldown(symbol: str, state: dict) -> bool:
    last = safe_float(state.get(symbol))
    return last > 0 and time.time() - last < COOLDOWN_HOURS * 3600


def api_get(path: str, params: dict | None = None, attempts: int = 4) -> Any:
    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            response = HTTP.get(BINANCE_API + path, params=params, timeout=15)
            if response.status_code in (418, 429):
                time.sleep(2 ** attempt)
                continue
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            last_exc = exc
            time.sleep(0.5 * (2 ** attempt))
    raise RuntimeError(f"Binance API başarısız: {path}: {last_exc}")


def fetch_ohlcv(symbol: str, interval: str, limit: int) -> pd.DataFrame:
    raw = api_get("/api/v3/klines", {"symbol": symbol, "interval": interval, "limit": limit})
    if not isinstance(raw, list) or len(raw) < 50:
        raise ValueError(f"Yetersiz mum: {symbol} {interval}")
    df = pd.DataFrame(raw, columns=[
        "open_time", "open", "high", "low", "close", "volume", "close_time",
        "quote_volume", "trades", "taker_base", "taker_quote", "ignore",
    ])
    for col in ("open", "high", "low", "close", "volume", "quote_volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    # Son mum henüz kapanmadıysa karar hesaplarına dahil edilmez.
    if len(df) > 1 and int(raw[-1][6]) > int(time.time() * 1000):
        df = df.iloc[:-1].copy()
    return df.dropna(subset=["open", "high", "low", "close", "volume"])


# =============================================================================
# İNDİKATÖRLER
# =============================================================================

def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50)


def stochastic(df: pd.DataFrame, period: int = 14, smooth: int = 3) -> tuple[pd.Series, pd.Series]:
    lowest = df["low"].rolling(period).min()
    highest = df["high"].rolling(period).max()
    k = 100 * (df["close"] - lowest) / (highest - lowest).replace(0, np.nan)
    k = k.rolling(smooth).mean().fillna(50)
    d = k.rolling(smooth).mean().fillna(50)
    return k, d


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for p in (20, 50, 100, 200):
        out[f"ema{p}"] = out["close"].ewm(span=p, adjust=False).mean()
    out["rsi"] = rsi(out["close"])
    rsi_min = out["rsi"].rolling(14).min()
    rsi_max = out["rsi"].rolling(14).max()
    out["stoch_rsi"] = (100 * (out["rsi"] - rsi_min) / (rsi_max - rsi_min).replace(0, np.nan)).fillna(50)
    out["stoch_rsi_k"] = out["stoch_rsi"].rolling(3).mean().fillna(50)
    out["stoch_rsi_d"] = out["stoch_rsi_k"].rolling(3).mean().fillna(50)
    out["macd"] = out["close"].ewm(span=12, adjust=False).mean() - out["close"].ewm(span=26, adjust=False).mean()
    out["macd_signal"] = out["macd"].ewm(span=9, adjust=False).mean()
    out["macd_hist"] = out["macd"] - out["macd_signal"]
    out["stoch_k"], out["stoch_d"] = stochastic(out)
    out["kdj_j"] = 3 * out["stoch_k"] - 2 * out["stoch_d"]
    hh = out["high"].rolling(14).max()
    ll = out["low"].rolling(14).min()
    out["willr"] = (-100 * (hh - out["close"]) / (hh - ll).replace(0, np.nan)).fillna(-50)
    direction = np.sign(out["close"].diff()).fillna(0)
    out["obv"] = (direction * out["volume"]).cumsum()
    prev_close = out["close"].shift(1)
    tr = pd.concat([
        out["high"] - out["low"],
        (out["high"] - prev_close).abs(),
        (out["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    out["atr"] = tr.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    out["vol_ratio"] = out["volume"] / out["volume"].rolling(20).median().replace(0, np.nan)
    return out


def candle_evidence(df: pd.DataFrame) -> tuple[float, list[str]]:
    if len(df) < 4:
        return 0.0, []
    a, b, c = df.iloc[-3], df.iloc[-2], df.iloc[-1]
    rng = max(c["high"] - c["low"], 1e-12)
    body = abs(c["close"] - c["open"])
    lower = min(c["open"], c["close"]) - c["low"]
    upper = c["high"] - max(c["open"], c["close"])
    score = 0.0
    notes: list[str] = []
    if c["close"] > c["open"] and lower >= body * 1.6 and upper <= rng * 0.35:
        score += 32
        notes.append("1H alt fitilli alıcı savunması")
    if b["close"] < b["open"] and c["close"] > c["open"] and c["open"] <= b["close"] and c["close"] >= b["open"]:
        score += 38
        notes.append("1H bullish engulfing")
    if a["close"] < a["open"] and abs(b["close"] - b["open"]) < abs(a["close"] - a["open"]) * 0.45 and c["close"] > (a["open"] + a["close"]) / 2:
        score += 32
        notes.append("1H üç mumlu dönüş yapısı")
    if c["close"] > c["open"] and body / rng >= 0.62 and c["close"] >= c["high"] - rng * 0.18:
        score += 24
        notes.append("1H güçlü kapanış")
    if upper > body * 2 and c["close"] < c["open"]:
        score -= 18
        notes.append("1H üst fitil satış baskısı")
    return clamp(score, -30, 70), notes


def timeframe_state(df: pd.DataFrame, label: str) -> dict[str, Any]:
    d = add_indicators(df)
    x, p = d.iloc[-1], d.iloc[-2]
    price = safe_float(x["close"])
    atr = max(safe_float(x["atr"]), price * 0.005)

    trend = 50.0
    trend += 9 if price > x["ema20"] else -9
    trend += 8 if x["ema20"] > x["ema50"] else -8
    trend += 7 if x["ema50"] > x["ema100"] else -7
    trend += 6 if x["ema100"] > x["ema200"] else -6
    trend += 7 if x["ema20"] > d["ema20"].iloc[-4] else -7

    momentum = 50.0
    rv, rp = safe_float(x["rsi"], 50), safe_float(p["rsi"], 50)
    momentum += 12 if rv > rp else -8
    momentum += 8 if 42 <= rv <= 68 else (-5 if rv > 75 else 3)
    srk, srd = safe_float(x["stoch_rsi_k"], 50), safe_float(x["stoch_rsi_d"], 50)
    if srk > srd and srk > safe_float(p["stoch_rsi_k"], 50):
        momentum += 10
    if safe_float(x["stoch_k"], 50) > safe_float(x["stoch_d"], 50):
        momentum += 6
    if safe_float(x["willr"], -50) > safe_float(p["willr"], -50):
        momentum += 6
    if safe_float(x["macd_hist"]) > safe_float(p["macd_hist"]):
        momentum += 10
    if safe_float(x["kdj_j"], 50) > safe_float(p["kdj_j"], 50):
        momentum += 4

    obv_now = safe_float(x["obv"])
    obv_old = safe_float(d["obv"].iloc[-6])
    volume = 50 + (16 if obv_now > obv_old else -12)
    vr = safe_float(x["vol_ratio"], 1)
    volume += min(16, max(-8, (vr - 1) * 16))

    candle_score, candle_notes = candle_evidence(d)
    ret_1 = (price / safe_float(p["close"], price) - 1) * 100
    ret_6 = (price / safe_float(d["close"].iloc[-7], price) - 1) * 100 if len(d) >= 7 else 0

    if trend >= 65 and momentum >= 57:
        text = "trend ve momentum olumlu"
    elif momentum >= 62:
        text = "tepki momentumu güçleniyor"
    elif trend < 42 and momentum >= 50:
        text = "ana yapı zayıf, tepki denemesi var"
    elif trend < 42:
        text = "düşüş baskısı sürüyor"
    else:
        text = "karışık/yatay"

    return {
        "label": label, "df": d, "price": price, "atr": atr,
        "trend": clamp(trend), "momentum": clamp(momentum), "volume": clamp(volume),
        "candle": candle_score, "candle_notes": candle_notes,
        "rsi": rv, "vol_ratio": vr, "ret_1": ret_1, "ret_6": ret_6,
        "text": text,
    }


# =============================================================================
# DESTEK / DİRENÇ BÖLGELERİ
# =============================================================================

def pivot_points(df: pd.DataFrame, window: int = 3) -> tuple[list[float], list[float]]:
    lows = df["low"]
    highs = df["high"]
    p_lows: list[float] = []
    p_highs: list[float] = []
    for i in range(window, len(df) - window):
        if lows.iloc[i] <= lows.iloc[i-window:i+window+1].min():
            p_lows.append(float(lows.iloc[i]))
        if highs.iloc[i] >= highs.iloc[i-window:i+window+1].max():
            p_highs.append(float(highs.iloc[i]))
    return p_lows[-40:], p_highs[-40:]


def cluster_levels(items: list[tuple[float, str, float]], current: float) -> list[Zone]:
    if not items:
        return []
    # Fiyat ölçeğine uyarlanan küme toleransı; çok volatil bölgelerde yine aşırı
    # geniş tek bir bölge oluşmaması için %0.35-%1.2 aralığında tutulur.
    tolerance = current * 0.007
    ordered = sorted(items, key=lambda x: x[0])
    groups: list[list[tuple[float, str, float]]] = []
    for item in ordered:
        if not groups or abs(item[0] - np.average([x[0] for x in groups[-1]], weights=[x[2] for x in groups[-1]])) > tolerance:
            groups.append([item])
        else:
            groups[-1].append(item)
    zones: list[Zone] = []
    for group in groups:
        prices = [x[0] for x in group]
        weights = [x[2] for x in group]
        center = float(np.average(prices, weights=weights))
        pad = max(current * 0.0025, (max(prices) - min(prices)) / 2)
        tfs = sorted(set(x[1] for x in group))
        strength = sum(weights) + len(tfs) * 2.5 + min(6, len(group))
        zones.append(Zone(center - pad, center + pad, center, strength, tfs, len(group)))
    return zones


def build_zones(states: dict[str, dict], current: float) -> tuple[list[Zone], list[Zone]]:
    support_items: list[tuple[float, str, float]] = []
    resistance_items: list[tuple[float, str, float]] = []
    tf_weights = {"1H": 1.0, "4H": 1.55, "1D": 2.15, "1W": 2.7}
    for label, state in states.items():
        d = state["df"]
        weight = tf_weights[label]
        lows, highs = pivot_points(d.tail(180), 3 if label in ("1H", "4H") else 2)
        support_items.extend((v, label, weight) for v in lows if v < current * 1.01)
        resistance_items.extend((v, label, weight) for v in highs if v > current * 0.99)
        last = d.iloc[-1]
        for p in (20, 50, 100, 200):
            ema = safe_float(last[f"ema{p}"])
            if not ema:
                continue
            ew = weight * (0.75 + p / 400)
            (support_items if ema <= current else resistance_items).append((ema, label, ew))
    supports = [z for z in cluster_levels(support_items, current) if z.center < current]
    resistances = [z for z in cluster_levels(resistance_items, current) if z.center > current]
    supports.sort(key=lambda z: (current - z.high, -z.strength))
    resistances.sort(key=lambda z: (z.low - current, -z.strength))
    return supports, resistances


def choose_support(supports: list[Zone], current: float, atr: float) -> Zone | None:
    viable = [z for z in supports if 0.002 <= (current - z.high) / current <= 0.12]
    if not viable:
        viable = [z for z in supports if z.center < current][:8]
    if not viable:
        return None
    # Yakın ama anlamsız seviyeyi değil; mesafe ve güç dengesini seç.
    return max(viable[:10], key=lambda z: z.strength - ((current - z.high) / max(atr, 1e-12)) * 1.8)


def choose_resistance(resistances: list[Zone], current: float) -> Zone | None:
    viable = [z for z in resistances if z.low > current]
    if not viable:
        return None
    # İlk gerçekçi satış bölgesi; çok zayıf tek dokunuşlu bölgeyi atlayabilir.
    for z in viable:
        if z.strength >= 5 or z.touches >= 2:
            return z
    return viable[0]


# =============================================================================
# ADAY DEĞERLENDİRME
# =============================================================================

def btc_context() -> dict[str, float]:
    try:
        h1 = fetch_ohlcv("BTCUSDT", "1h", 80)
        h4 = fetch_ohlcv("BTCUSDT", "4h", 80)
        return {
            "ret_1h": (h1["close"].iloc[-1] / h1["close"].iloc[-2] - 1) * 100,
            "ret_6h": (h1["close"].iloc[-1] / h1["close"].iloc[-7] - 1) * 100,
            "ret_24h": (h1["close"].iloc[-1] / h1["close"].iloc[-25] - 1) * 100,
            "ret_4h": (h4["close"].iloc[-1] / h4["close"].iloc[-2] - 1) * 100,
        }
    except Exception as exc:
        print(f"[BTC] Bağlam alınamadı: {exc}", flush=True)
        return {"ret_1h": 0, "ret_6h": 0, "ret_24h": 0, "ret_4h": 0}


def classify_setup(states: dict[str, dict], support_distance: float) -> str:
    h1, h4 = states["1H"], states["4H"]
    d1 = states.get("1D", h4)
    if h1["momentum"] >= 62 and h4["trend"] < 45:
        return "KISA VADELİ TEPKİ"
    if h1["trend"] >= 60 and h4["trend"] >= 55:
        return "TREND İÇİ GERİ ÇEKİLME"
    if support_distance <= 2.0 and h1["momentum"] >= 55:
        return "DESTEK TEPKİSİ"
    if d1["trend"] < 45 and h1["momentum"] > 58:
        return "KARŞI-TREND TEPKİ"
    return "KISA VADELİ FIRSAT"


def evaluate_symbol(symbol: str, h1_state: dict, btc: dict[str, float]) -> Candidate | None:
    frames = {"1H": h1_state}
    requests_map = {"4H": ("4h", 260), "1D": ("1d", 260), "1W": ("1w", 160)}
    for label, (interval, limit) in requests_map.items():
        try:
            frames[label] = timeframe_state(fetch_ohlcv(symbol, interval, limit), label)
        except Exception:
            # 4H değerlendirme için temel bağlamdır; 1D ve 1W ise mevcutsa
            # ağırlık sağlar ama hiçbir zaman dilimi tek başına veto değildir.
            if label == "4H":
                raise

    price = h1_state["price"]
    supports, resistances = build_zones(frames, price)
    support = choose_support(supports, price, h1_state["atr"])
    resistance = choose_resistance(resistances, price)
    if not support or not resistance:
        return None

    stop = support.low * (1 - SUPPORT_BUFFER_PCT / 100)
    target_low, target_high = resistance.low, resistance.high
    target_pct = (target_low / price - 1) * 100
    stop_pct = (price - stop) / price * 100
    if target_pct <= 0 or stop_pct <= 0:
        return None
    rr = target_pct / stop_pct
    support_distance = (price - support.high) / price * 100

    # Birleşik puan yoktur. Adaylık, kullanıcının baktığı gözlemlenebilir teknik
    # durumların açık kombinasyonlarıyla oluşur; hiçbir timeframe tek başına veto değildir.
    momentum_turn = h1_state["momentum"] >= 60
    h4_confirmation = frames["4H"]["momentum"] >= 58
    candle_confirmation = h1_state["candle"] > 0
    volume_confirmation = h1_state["volume"] >= 58
    near_support = support_distance <= 3.0
    observed_setups: list[str] = []
    if near_support and (momentum_turn or candle_confirmation or h4_confirmation):
        observed_setups.append("Anlamlı destek yakınında tepki işareti")
    if h1_state["trend"] >= 55 and frames["4H"]["trend"] >= 55 and support_distance <= 4.0:
        observed_setups.append("1H–4H trend yapısında desteğe yakınlık")
    if momentum_turn and (h4_confirmation or candle_confirmation or volume_confirmation):
        observed_setups.append("1H momentum dönüşüne ek teyit")
    if candle_confirmation and volume_confirmation:
        observed_setups.append("Mum davranışı ve hacim birlikte olumlu")
    if not observed_setups:
        return None

    positives: list[str] = []
    risks: list[str] = []
    historical_notes: list[str] = []
    for note in h1_state["candle_notes"]:
        if "satış baskısı" in note:
            risks.append(note)
        elif len(positives) < 2:
            positives.append(note)
    if h1_state["momentum"] >= 60:
        positives.append("1H momentum ailesi yukarı dönüyor")
    if frames["4H"]["momentum"] >= 58:
        positives.append("4H momentum tepkiyi destekliyor")
    if h1_state["volume"] >= 58:
        positives.append("OBV/hacim akışı olumlu")
    if len(support.timeframes) >= 2:
        positives.append(f"Destek çakışması: {'+'.join(support.timeframes)}")
    if h1_state["ret_6"] > btc["ret_6h"] + 1.0:
        positives.append("BTC'ye karşı kısa vadeli göreceli güç")

    if stop_pct > 5:
        risks.append(f"Yapısal stop mesafesi geniş: %{stop_pct:.1f}")
        historical_notes.append("İlk 90 günlük örneklemde %5 üzeri stop mesafeleri daha zayıftı; eleme değildir")
    if frames["4H"]["trend"] < 42:
        risks.append("4H ana yapı hâlâ zayıf")
    if "1D" in frames and frames["1D"]["trend"] < 42:
        risks.append("1D yapı düşüş baskısında")
    if btc["ret_1h"] < -1.2 or btc["ret_4h"] < -2.4:
        risks.append("BTC kısa vadeli baskı oluşturuyor")
    elif btc["ret_6h"] < -2 and h1_state["ret_6"] >= btc["ret_6h"] + 1:
        risks.append("BTC zayıf; coin şimdilik göreceli güçlü")
    if h1_state["rsi"] > 73:
        risks.append("1H RSI kısa vadede ısınmış")
    if target_pct < stop_pct:
        risk += 10
        risks.append("İlk hedef mesafesi yapısal stop mesafesinden küçük")
    if support_distance > 5:
        risks.append("Fiyat seçilen ana desteğin uzağında")
    if target_pct > 3:
        historical_notes.append("İlk 90 günlük örneklemde %3 üzeri ilk hedefler 24 saatte daha seyrek gerçekleşti; eleme değildir")

    entry_pad = min(h1_state["atr"] * 0.18, price * 0.004)
    entry_low = max(support.high, price - entry_pad)
    entry_high = price + entry_pad * 0.35
    setup = classify_setup(frames, support_distance)
    metrics = {
        "support_strength": round(support.strength, 1),
        "support_timeframes": support.timeframes, "resistance_strength": round(resistance.strength, 1),
        "btc_1h_pct": round(btc["ret_1h"], 2), "btc_6h_pct": round(btc["ret_6h"], 2),
        "rsi_1h": round(h1_state["rsi"], 1), "rsi_4h": round(frames["4H"]["rsi"], 1),
        "vol_ratio_1h": round(h1_state["vol_ratio"], 2),
    }
    return Candidate(
        symbol=symbol, price=price, entry_low=entry_low, entry_high=entry_high,
        support=support, stop=stop, resistance=resistance,
        target_low=target_low, target_high=target_high, target_pct=target_pct,
        stop_pct=stop_pct, rr=rr, setup=setup, observed_setups=observed_setups,
        positives=positives[:5] or ["Çoklu gösterge dengesi incelemeye değer"],
        risks=risks[:5] or ["Belirgin ek risk sinyali yok; manuel grafik kontrolü gerekli"],
        historical_notes=historical_notes,
        tf_summary={label: (frames[label]["text"] if label in frames else "yeterli geçmiş veri yok")
                    for label in ("1H", "4H", "1D", "1W")},
        metrics=metrics,
    )


# =============================================================================
# MESAJ VE PORTFOLIO
# =============================================================================

def candidate_message(c: Candidate) -> str:
    sym = c.symbol.removesuffix("USDT")
    observed = "\n".join(f"• {x}" for x in c.observed_setups)
    positives = "\n".join(f"✅ {x}" for x in c.positives)
    risks = "\n".join(f"⚠️ {x}" for x in c.risks)
    history = "\n".join(f"ℹ️ {x}" for x in c.historical_notes)
    history_block = f"\n\n<b>Geçmiş örneklem notu</b>\n{history}" if history else ""
    return (
        f"🔎 <b>İNCELEME ADAYI — #{sym}</b>\n"
        f"<b>{c.setup}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 Anlık: <code>{fmt_price(c.price)}</code>\n"
        f"🟦 Giriş değerlendirme: <code>{fmt_price(c.entry_low)}–{fmt_price(c.entry_high)}</code>\n"
        f"🟩 Ana destek: <code>{fmt_price(c.support.low)}–{fmt_price(c.support.high)}</code> "
        f"({'/'.join(c.support.timeframes)})\n"
        f"🛑 Yapısal stop referansı: <code>{fmt_price(c.stop)}</code> "
        f"(destek altı %{SUPPORT_BUFFER_PCT:g}, risk %{c.stop_pct:.1f})\n"
        f"🎯 İlk satış/direnç: <code>{fmt_price(c.target_low)}–{fmt_price(c.target_high)}</code> "
        f"(+%{c.target_pct:.1f})\n"
        f"⚖️ İlk bölge R/R: <b>{c.rr:.2f}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Zaman dilimleri</b>\n"
        f"• 1H: {c.tf_summary['1H']}\n"
        f"• 4H: {c.tf_summary['4H']}\n"
        f"• 1D: {c.tf_summary['1D']}\n"
        f"• 1W: {c.tf_summary['1W']}\n\n"
        f"<b>Neden taramaya takıldı?</b>\n{observed}\n\n"
        f"<b>Olumlu kanıtlar</b>\n{positives}\n\n"
        f"<b>Riskler</b>\n{risks}{history_block}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>Otomatik alım değildir; grafiği manuel incele.</i>"
    )


def send_telegram(text: str) -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[TELEGRAM] Token/chat id yok; mesaj yalnızca loglandı.", flush=True)
        return False
    try:
        payload: dict[str, Any] = {
            "chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if TELEGRAM_THREAD_ID:
            payload["message_thread_id"] = TELEGRAM_THREAD_ID
        r = HTTP.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json=payload, timeout=15)
        if not r.ok:
            print(f"[TELEGRAM] HTTP {r.status_code}: {r.text[:180]}", flush=True)
        return r.ok
    except Exception as exc:
        print(f"[TELEGRAM] Hata: {exc}", flush=True)
        return False


def send_portfolio(c: Candidate) -> str:
    if not PORTFOLIO_URL:
        return ""
    # Portfolio'nun mevcut SMC yaşam döngüsünü kullanabilmek için source smc-v2
    # kalır. sig_type/sub_type bu sistemin SMC olmadığını açıkça ayırır.
    payload = {
        "symbol": c.symbol.replace("USDT", "/USDT"),
        "entry": round(c.price, 10),
        "limit_price": round(c.price, 10),
        "signal_price": round(c.price, 10),
        "stop": round(c.stop, 10),
        "tp1": round(c.target_low, 10),
        "tp2": round(c.target_high, 10),
        "tp3": None,
        "sig_type": "spot_opportunity",
        "sub_type": c.setup.lower().replace(" ", "_"),
        "source": "smc-v2",
        "phase": "manual_review",
        "observed_setups": c.observed_setups,
        "entry_zone": [round(c.entry_low, 10), round(c.entry_high, 10)],
        "support_zone": [round(c.support.low, 10), round(c.support.high, 10)],
        "resistance_zone": [round(c.target_low, 10), round(c.target_high, 10)],
        "target_pct": round(c.target_pct, 2),
        "stop_pct": round(c.stop_pct, 2),
        "rr": round(c.rr, 2),
        "tf_summary": c.tf_summary,
        "positives": c.positives,
        "risks": c.risks,
        "historical_notes": c.historical_notes,
        **c.metrics,
    }
    headers = {"Content-Type": "application/json"}
    if PORTFOLIO_TOKEN:
        headers["Authorization"] = f"Bearer {PORTFOLIO_TOKEN}"
    try:
        r = HTTP.post(f"{PORTFOLIO_URL}/api/signal", json=payload, headers=headers, timeout=12)
        if r.status_code in (200, 201):
            return str((r.json() or {}).get("id", ""))
        if r.status_code == 409:
            print(f"[PORTFOLIO] Zaten açık: {c.symbol}", flush=True)
            return ""
        print(f"[PORTFOLIO] HTTP {r.status_code}: {r.text[:180]}", flush=True)
    except Exception as exc:
        print(f"[PORTFOLIO] Hata: {exc}", flush=True)
    return ""


def request_analyzer(c: Candidate, portfolio_id: str) -> None:
    """Portfolio servisindeki mevcut Claude Analyzer hattını tetikler.

    Başarısız olması ana sinyali veya cooldown'u bozmaz; bu katman yalnızca
    ikinci görüş üretir.
    """
    if not PORTFOLIO_URL or not portfolio_id:
        return
    signal = {
        "symbol": c.symbol.replace("USDT", "/USDT"),
        "type": "spot_opportunity",
        "source": "smc",
        "entry": round(c.price, 10),
        "stop": round(c.stop, 10),
        "tp1": round(c.target_low, 10),
        "tp2": round(c.target_high, 10),
        "setup": c.setup,
        "observed_setups": c.observed_setups,
        "target_pct": round(c.target_pct, 2),
        "stop_pct": round(c.stop_pct, 2),
        "rr": round(c.rr, 2),
        "tf_summary": c.tf_summary,
        "positives": c.positives,
        "risks": c.risks,
        "historical_notes": c.historical_notes,
        **c.metrics,
    }
    headers = {"Content-Type": "application/json"}
    if PORTFOLIO_TOKEN:
        headers["Authorization"] = f"Bearer {PORTFOLIO_TOKEN}"
    try:
        r = HTTP.post(
            f"{PORTFOLIO_URL}/api/analyze",
            json={"signal": signal, "recent_count": 0, "sig_num": 0, "portfolio_id": portfolio_id},
            headers=headers,
            timeout=12,
        )
        if not r.ok:
            print(f"[ANALYZER] HTTP {r.status_code}: {r.text[:160]}", flush=True)
    except Exception as exc:
        print(f"[ANALYZER] Hata: {exc}", flush=True)


# =============================================================================
# TARAMA
# =============================================================================

def get_spot_universe() -> list[tuple[str, float]]:
    exchange_info = api_get("/api/v3/exchangeInfo")
    tickers = api_get("/api/v3/ticker/24hr")
    ticker_map = {x.get("symbol"): x for x in tickers if isinstance(x, dict)}
    result: list[tuple[str, float]] = []
    for item in exchange_info.get("symbols", []):
        symbol = item.get("symbol", "")
        base = item.get("baseAsset", "")
        if item.get("status") != "TRADING" or item.get("quoteAsset") != "USDT":
            continue
        if not item.get("isSpotTradingAllowed", True):
            continue
        # Kısa gerçek sembolleri (örn. JUP) yanlışlıkla "UP token" sanma.
        is_leveraged = any(base.endswith(s) and len(base) > len(s) + 2 for s in LEVERAGED_SUFFIXES)
        is_bstock = base.endswith("B") and base not in CRYPTO_BASES_ENDING_B
        if base in IGNORED_BASES or base == "BTC" or is_leveraged or is_bstock:
            continue
        quote_volume = safe_float(ticker_map.get(symbol, {}).get("quoteVolume"))
        if quote_volume < MIN_QUOTE_VOLUME:
            continue
        result.append((symbol, quote_volume))
    return sorted(result, key=lambda x: x[1], reverse=True)


def scan_once() -> list[Candidate]:
    runtime.update({"status": "SCANNING", "last_scan_start": tr_now().isoformat(), "last_error": None})
    started = time.time()
    state = load_state()
    universe = get_spot_universe()
    runtime["symbols"] = len(universe)
    btc = btc_context()
    print(f"[SCAN] {len(universe)} spot parite | BTC 1H {btc['ret_1h']:+.2f}% 6H {btc['ret_6h']:+.2f}%", flush=True)

    h1_states: list[tuple[str, dict]] = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        jobs = {pool.submit(fetch_ohlcv, symbol, "1h", 260): symbol for symbol, _ in universe}
        for future in as_completed(jobs):
            symbol = jobs[future]
            try:
                st = timeframe_state(future.result(), "1H")
                if not in_cooldown(symbol, state):
                    h1_states.append((symbol, st))
            except Exception as exc:
                print(f"[1H] {symbol}: {str(exc)[:100]}", flush=True)

    runtime["deep_scanned"] = len(h1_states)
    candidates: list[Candidate] = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        jobs = {pool.submit(evaluate_symbol, sym, st, btc): sym for sym, st in h1_states}
        for future in as_completed(jobs):
            symbol = jobs[future]
            try:
                candidate = future.result()
                if candidate:
                    candidates.append(candidate)
            except Exception as exc:
                print(f"[DEEP] {symbol}: {str(exc)[:120]}", flush=True)

    # Puan veya kalite sıralaması yoktur; yalnızca okunabilir ve kararlı çıktı.
    candidates.sort(key=lambda c: (c.setup, c.symbol))
    sent = 0
    for c in candidates:
        message = candidate_message(c)
        print("\n" + message.replace("<b>", "").replace("</b>", "").replace("<code>", "").replace("</code>", "").replace("<i>", "").replace("</i>", ""), flush=True)
        if DRY_RUN:
            print(f"[DRY-RUN] {c.symbol}: Portfolio, Analyzer ve Telegram gönderimi yapılmadı.", flush=True)
            portfolio_id = ""
            telegram_ok = False
        else:
            portfolio_id = send_portfolio(c)
            if portfolio_id:
                threading.Thread(target=request_analyzer, args=(c, portfolio_id), daemon=True).start()
            telegram_ok = send_telegram(message)
        # Portfolio kapalı olsa bile Telegram başarıyla gittiyse cooldown uygula.
        if portfolio_id or telegram_ok:
            state[c.symbol] = time.time()
            sent += 1
    save_state(state)
    runtime.update({
        "status": "RUNNING", "last_scan_end": tr_now().isoformat(),
        "candidates": len(candidates), "sent": sent,
    })
    print(f"[SCAN] Bitti: {len(candidates)} aday, {sent} gönderim, {time.time()-started:.1f}s", flush=True)
    return candidates


def scanner_loop() -> None:
    if SCAN_ON_START:
        try:
            scan_once()
        except Exception as exc:
            runtime.update({"status": "ERROR", "last_error": str(exc)})
            print(f"[SCAN] Başlangıç hatası: {exc}", flush=True)
    while True:
        now = tr_now()
        # 1H mumun kapanıp Binance'ta kesinleşmesi için saat başından 90 sn sonra.
        next_hour = (now + timedelta(hours=1)).replace(minute=1, second=30, microsecond=0)
        wait = max(60, (next_hour - now).total_seconds())
        if SCAN_INTERVAL_MIN != 60:
            wait = max(60, SCAN_INTERVAL_MIN * 60)
        time.sleep(wait)
        try:
            scan_once()
        except Exception as exc:
            runtime.update({"status": "ERROR", "last_error": str(exc)})
            print(f"[SCAN] Döngü hatası: {exc}", flush=True)
            time.sleep(60)


# =============================================================================
# HEALTH ENDPOINT
# =============================================================================

@app.route("/")
@app.route("/health")
def health():
    return jsonify({"service": "spot-opportunity-scanner", **runtime}), 200


def run_flask() -> None:
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, use_reloader=False)


def main() -> None:
    print("=" * 68, flush=True)
    print("SPOT OPPORTUNITY SCANNER — manuel inceleme adayı sistemi", flush=True)
    print(f"Spot only | birleşik puan yok | stop tamponu=%{SUPPORT_BUFFER_PCT:g}", flush=True)
    print(f"DRY_RUN={DRY_RUN} — " + ("hiçbir dış gönderim yapılmaz" if DRY_RUN else "Portfolio/Telegram gönderimi AKTİF"), flush=True)
    print("Gerçek emir fonksiyonu yoktur.", flush=True)
    print("=" * 68, flush=True)
    runtime["status"] = "STARTING"
    threading.Thread(target=run_flask, daemon=True, name="health-server").start()
    scanner_loop()


if __name__ == "__main__":
    main()
