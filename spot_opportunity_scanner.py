# -*- coding: utf-8 -*-
"""
INTRADAY SPOT SCANNER
=====================

Sifirdan yazilmis Binance Spot tarayici.

Strateji mimarisi:
  - 1H = coin secimi / yon / ana destek-direnc / BTC'ye gore guc
  - 15M = giris zamanlamasi / market structure shift / breakout-retest / pullback
  - Spot only; emir vermez. Manuel inceleme adayi uretir.
  - Son acik mum karar hesaplarina dahil edilmez.
  - Eski SPOT_SCANNER state, setup, cooldown ve scoring mantigini kullanmaz.

Render uyumlulugu icin dosya adi ve servis dis sozlesmeleri korunur:
  - / ve /health endpoint
  - PORT, TELEGRAM_*, PORTFOLIO_* environment degiskenleri
  - Portfolio /api/signal ve opsiyonel /api/analyze entegrasyonu
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np
import pandas as pd
import requests
from flask import Flask, jsonify

BINANCE_API = "https://api.binance.com"
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TELEGRAM_THREAD_ID = int(os.getenv("SIGNAL_THREAD_ID", "2"))
PORTFOLIO_URL = os.getenv("PORTFOLIO_URL", "").rstrip("/")
PORTFOLIO_TOKEN = os.getenv("PORTFOLIO_TOKEN", "")
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"
SCAN_ON_START = os.getenv("SCAN_ON_START", "true").lower() == "true"
MAX_WORKERS = max(1, min(8, int(os.getenv("MAX_WORKERS", "4"))))
MIN_QUOTE_VOLUME = float(os.getenv("MIN_QUOTE_VOLUME", "1000000"))
PREFERRED_QUOTE_VOLUME = float(os.getenv("PREFERRED_QUOTE_VOLUME", "15000000"))
LOW_LIQ_VOLUME_RATIO = float(os.getenv("LOW_LIQ_VOLUME_RATIO", "2.5"))
WATCHLIST_MAX = max(5, min(50, int(os.getenv("WATCHLIST_MAX", "30"))))
MIN_WATCH_SCORE = float(os.getenv("MIN_WATCH_SCORE", "55"))
MIN_ENTRY_SCORE = float(os.getenv("MIN_ENTRY_SCORE", "68"))
ALERT_COOLDOWN_HOURS = float(os.getenv("ALERT_COOLDOWN_HOURS", "4"))
ACCOUNT_SIZE = float(os.getenv("ACCOUNT_SIZE", "10000"))
RISK_PER_TRADE_PCT = float(os.getenv("RISK_PER_TRADE_PCT", "1.25"))
MAX_POSITION_PCT = float(os.getenv("MAX_POSITION_PCT", "40"))
SUPPORT_BUFFER_PCT = float(os.getenv("SUPPORT_BUFFER_PCT", "2.5"))
MIN_TARGET_PCT = float(os.getenv("MIN_TARGET_PCT", "1.5"))
MAX_STOP_PCT = float(os.getenv("MAX_STOP_PCT", "6.5"))
STATE_FILE = os.getenv("SCANNER_STATE_FILE", "/tmp/spot_intraday_state_v1.json")
TR_TZ = timezone(timedelta(hours=3))
HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "Botum-IntradaySpotScanner/2.0"})

IGNORED_BASES = {
    "USDT", "USDC", "BUSD", "TUSD", "DAI", "PAX", "HUSD", "USDP", "GUSD", "FDUSD",
    "EUR", "TRY", "GBP", "USD", "BRL", "RUB", "AUD", "XUSD", "USD1", "USDE", "BFUSD",
    "USDS", "USDD", "PYUSD", "AEUR", "EURI", "USTC", "FRAX", "LUSD", "SUSD", "USDX",
    "CUSD", "OUSD", "MUSD", "RLUSD", "BIDR", "IDRT", "VAI", "PAXG", "XAUT", "WBTC",
    "WETH", "WBNB", "BETH", "BTCB", "HBTC", "U",
}
LEVERAGED_SUFFIXES = (
    "UP", "DOWN", "BULL", "BEAR", "2L", "2S", "3L", "3S", "5L", "5S", "10L", "10S"
)
CRYPTO_BASES_ENDING_B = {"BNB", "DGB", "TRB", "CKB", "SHIB", "ARB", "BB", "YB"}

app = Flask(__name__)
logging.getLogger("werkzeug").setLevel(logging.ERROR)
runtime: dict[str, Any] = {
    "status": "BOOT", "strategy": "15M execution + 1H confirmation", "dry_run": DRY_RUN,
    "last_universe_scan": None, "last_execution_scan": None, "symbols": 0, "watchlist": 0,
    "entry_ready": 0, "sent": 0, "btc_regime": None, "last_error": None,
}

@dataclass
class Zone:
    low: float
    high: float
    strength: float
    source: str
    touches: int = 1

    @property
    def center(self) -> float:
        return (self.low + self.high) / 2

@dataclass
class WatchItem:
    symbol: str
    quote_volume_24h: float
    watch_score: float
    h1_price: float
    h1_atr: float
    support: Zone
    resistance: Zone
    h1_trend: str
    h1_structure: str
    relative_strength_1h: float
    relative_strength_4h: float
    volume_ratio_1h: float
    reasons: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)

@dataclass
class Candidate:
    symbol: str
    setup: str
    price: float
    entry_low: float
    entry_high: float
    stop: float
    target1: float
    target2: float
    target_pct: float
    stop_pct: float
    rr: float
    position_size: float
    risk_dollars: float
    entry_score: float
    btc_regime: str
    support: Zone
    resistance: Zone
    reasons: list[str]
    risks: list[str]
    metrics: dict[str, Any]

def tr_now() -> datetime:
    return datetime.now(timezone.utc).astimezone(TR_TZ)

def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        x = float(value)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default

def clamp(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, float(value)))

def fmt_price(value: float) -> str:
    if value >= 1000:
        return f"{value:,.2f}"
    if value >= 100:
        return f"{value:.2f}"
    if value >= 1:
        return f"{value:.4f}"
    if value >= 0.01:
        return f"{value:.6f}"
    return f"{value:.10f}".rstrip("0")

def pct_change(new: float, old: float) -> float:
    return (new / old - 1) * 100 if old else 0.0

def load_state() -> dict[str, Any]:
    try:
        with open(STATE_FILE, encoding="utf-8") as handle:
            data = json.load(handle)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def save_state(data: dict[str, Any]) -> None:
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_FILE)
    except Exception as exc:
        print(f"[STATE] Yazma hatasi: {exc}", flush=True)

def api_get(path: str, params: dict | None = None, attempts: int = 4) -> Any:
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            response = HTTP.get(BINANCE_API + path, params=params, timeout=15)
            if response.status_code in (418, 429):
                time.sleep(2 ** attempt)
                continue
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            last_error = exc
            time.sleep(0.4 * (2 ** attempt))
    raise RuntimeError(f"Binance API basarisiz: {path}: {last_error}")

def fetch_ohlcv(symbol: str, interval: str, limit: int = 260) -> pd.DataFrame:
    rows = api_get("/api/v3/klines", {"symbol": symbol, "interval": interval, "limit": limit})
    if not isinstance(rows, list) or len(rows) < 60:
        raise ValueError(f"Yetersiz mum: {symbol} {interval}")
    df = pd.DataFrame(rows, columns=[
        "open_time", "open", "high", "low", "close", "volume", "close_time",
        "quote_volume", "trades", "taker_base", "taker_quote", "ignore",
    ])
    for col in ("open", "high", "low", "close", "volume", "quote_volume", "taker_quote"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
    now_ms = int(time.time() * 1000)
    if rows and int(rows[-1][6]) > now_ms:
        df = df.iloc[:-1].copy()
    return df.dropna(subset=["open", "high", "low", "close", "volume"]).reset_index(drop=True)

def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50)

def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["ema20"] = out["close"].ewm(span=20, adjust=False).mean()
    out["ema50"] = out["close"].ewm(span=50, adjust=False).mean()
    out["ema200"] = out["close"].ewm(span=200, adjust=False).mean()
    out["rsi"] = rsi(out["close"])
    rsi_low = out["rsi"].rolling(14).min()
    rsi_high = out["rsi"].rolling(14).max()
    stoch = 100 * (out["rsi"] - rsi_low) / (rsi_high - rsi_low).replace(0, np.nan)
    out["stoch_k"] = stoch.rolling(3).mean().fillna(50)
    out["stoch_d"] = out["stoch_k"].rolling(3).mean().fillna(50)
    ema12 = out["close"].ewm(span=12, adjust=False).mean()
    ema26 = out["close"].ewm(span=26, adjust=False).mean()
    out["macd"] = ema12 - ema26
    out["macd_signal"] = out["macd"].ewm(span=9, adjust=False).mean()
    out["macd_hist"] = out["macd"] - out["macd_signal"]
    prev_close = out["close"].shift(1)
    tr = pd.concat([
        out["high"] - out["low"], (out["high"] - prev_close).abs(), (out["low"] - prev_close).abs()
    ], axis=1).max(axis=1)
    out["atr"] = tr.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    out["vol_ratio"] = out["volume"] / out["volume"].rolling(20).median().replace(0, np.nan)
    out["taker_buy_ratio"] = out["taker_quote"] / out["quote_volume"].replace(0, np.nan)
    return out

def pivot_indices(df: pd.DataFrame, side: str, window: int = 2) -> list[int]:
    series = df["low"] if side == "low" else df["high"]
    result: list[int] = []
    for i in range(window, len(df) - window):
        chunk = series.iloc[i - window:i + window + 1]
        extreme = chunk.min() if side == "low" else chunk.max()
        if series.iloc[i] == extreme:
            result.append(i)
    return result

def swing_structure(df: pd.DataFrame) -> dict[str, Any]:
    d = df.tail(100).reset_index(drop=True)
    lows = pivot_indices(d, "low", 2)
    highs = pivot_indices(d, "high", 2)
    low_vals = [safe_float(d.iloc[i]["low"]) for i in lows[-3:]]
    high_vals = [safe_float(d.iloc[i]["high"]) for i in highs[-3:]]
    trend = "MIXED"
    if len(low_vals) >= 2 and len(high_vals) >= 2:
        higher_lows = low_vals[-1] > low_vals[-2]
        higher_highs = high_vals[-1] > high_vals[-2]
        lower_lows = low_vals[-1] < low_vals[-2]
        lower_highs = high_vals[-1] < high_vals[-2]
        if higher_lows and higher_highs:
            trend = "HH_HL"
        elif lower_lows and lower_highs:
            trend = "LH_LL"
        elif higher_lows:
            trend = "HL_BUILDING"
        elif higher_highs:
            trend = "HH_BUILDING"
    return {
        "trend": trend,
        "last_swing_low": low_vals[-1] if low_vals else safe_float(d["low"].tail(10).min()),
        "prev_swing_low": low_vals[-2] if len(low_vals) >= 2 else None,
        "last_swing_high": high_vals[-1] if high_vals else safe_float(d["high"].tail(10).max()),
        "prev_swing_high": high_vals[-2] if len(high_vals) >= 2 else None,
    }

def build_zone(df: pd.DataFrame, side: str, lookback: int, window: int, source: str) -> Zone | None:
    d = df.tail(lookback).reset_index(drop=True)
    idxs = pivot_indices(d, "low" if side == "support" else "high", window)
    if not idxs:
        return None
    current = safe_float(d["close"].iloc[-1])
    atr_now = max(safe_float(d["atr"].iloc[-1]), current * 0.002)
    levels = [safe_float(d.iloc[i]["low" if side == "support" else "high"]) for i in idxs]
    if side == "support":
        viable = [x for x in levels if x < current]
        viable.sort(reverse=True)
    else:
        viable = [x for x in levels if x > current]
        viable.sort()
    if not viable:
        return None
    anchor = viable[0]
    near = [x for x in viable[:12] if abs(x - anchor) <= atr_now * 0.8]
    center = float(np.mean(near)) if near else anchor
    half = min(atr_now * 0.28, current * 0.006)
    touches = max(1, len(near))
    strength = min(100.0, 25 + touches * 12)
    return Zone(center - half, center + half, strength, source, touches)

def merge_supports(h1: pd.DataFrame, m15: pd.DataFrame) -> Zone | None:
    h1_zone = build_zone(h1, "support", 180, 2, "1H")
    m15_zone = build_zone(m15, "support", 160, 2, "15M")
    if h1_zone and m15_zone:
        atr = max(safe_float(m15["atr"].iloc[-1]), safe_float(m15["close"].iloc[-1]) * 0.002)
        if abs(h1_zone.center - m15_zone.center) <= atr * 2.0:
            return Zone(
                low=min(h1_zone.low, m15_zone.low), high=max(h1_zone.high, m15_zone.high),
                strength=min(100, h1_zone.strength + 20), source="1H+15M",
                touches=h1_zone.touches + m15_zone.touches,
            )
    return h1_zone or m15_zone

def choose_resistance(h1: pd.DataFrame, m15: pd.DataFrame, price: float) -> Zone | None:
    candidates = [
        build_zone(m15, "resistance", 160, 2, "15M"),
        build_zone(h1, "resistance", 180, 2, "1H"),
    ]
    viable = [z for z in candidates if z and z.low > price]
    if not viable:
        return None
    viable.sort(key=lambda z: z.low)
    meaningful = [z for z in viable if pct_change(z.low, price) >= MIN_TARGET_PCT]
    return (meaningful or viable)[0]

def h1_state(df: pd.DataFrame) -> dict[str, Any]:
    d = add_indicators(df)
    last = d.iloc[-1]
    structure = swing_structure(d)
    price = safe_float(last["close"])
    ema20 = safe_float(last["ema20"])
    ema50 = safe_float(last["ema50"])
    ema200 = safe_float(last["ema200"])
    trend = "BULL" if price > ema20 > ema50 else "BEAR" if price < ema20 < ema50 else "MIXED"
    return {
        "df": d, "price": price, "atr": safe_float(last["atr"]), "rsi": safe_float(last["rsi"]),
        "vol_ratio": safe_float(last["vol_ratio"], 1.0),
        "taker_buy_ratio": safe_float(last["taker_buy_ratio"], 0.5), "trend": trend,
        "structure": structure, "ema20": ema20, "ema50": ema50, "ema200": ema200,
        "ret_1h": pct_change(price, safe_float(d["close"].iloc[-2])),
        "ret_4h": pct_change(price, safe_float(d["close"].iloc[-5])),
        "macd_hist": safe_float(last["macd_hist"]),
        "macd_hist_prev": safe_float(d["macd_hist"].iloc[-2]),
    }

def m15_state(df: pd.DataFrame) -> dict[str, Any]:
    d = add_indicators(df)
    last = d.iloc[-1]
    prev = d.iloc[-2]
    structure = swing_structure(d)
    price = safe_float(last["close"])
    return {
        "df": d, "price": price, "atr": safe_float(last["atr"]), "rsi": safe_float(last["rsi"]),
        "stoch_k": safe_float(last["stoch_k"]), "stoch_d": safe_float(last["stoch_d"]),
        "stoch_prev_k": safe_float(prev["stoch_k"]), "stoch_prev_d": safe_float(prev["stoch_d"]),
        "vol_ratio": safe_float(last["vol_ratio"], 1.0),
        "taker_buy_ratio": safe_float(last["taker_buy_ratio"], 0.5),
        "ema20": safe_float(last["ema20"]), "ema50": safe_float(last["ema50"]),
        "macd_hist": safe_float(last["macd_hist"]), "macd_hist_prev": safe_float(prev["macd_hist"]),
        "structure": structure, "last_open": safe_float(last["open"]), "last_high": safe_float(last["high"]),
        "last_low": safe_float(last["low"]), "prev_high": safe_float(prev["high"]),
        "prev_low": safe_float(prev["low"]), "bar_id": int(pd.Timestamp(last["open_time"]).timestamp()),
    }

def btc_context() -> dict[str, Any]:
    h1 = h1_state(fetch_ohlcv("BTCUSDT", "1h", 260))
    m15 = m15_state(fetch_ohlcv("BTCUSDT", "15m", 180))
    structure = h1["structure"]["trend"]
    red = (
        h1["ret_1h"] <= -1.25 or
        (h1["trend"] == "BEAR" and structure == "LH_LL" and h1["ret_4h"] <= -1.8) or
        (m15["price"] < m15["ema20"] < m15["ema50"] and m15["vol_ratio"] >= 1.8 and m15["taker_buy_ratio"] < 0.43)
    )
    yellow = h1["ret_1h"] < -0.45 or h1["trend"] == "BEAR" or structure in {"LH_LL", "MIXED"}
    regime = "RED" if red else "YELLOW" if yellow else "GREEN"
    return {
        "regime": regime, "price": h1["price"], "ret_1h": h1["ret_1h"], "ret_4h": h1["ret_4h"],
        "trend": h1["trend"], "structure": structure, "m15_vol_ratio": m15["vol_ratio"],
        "m15_taker_buy_ratio": m15["taker_buy_ratio"],
    }

def get_spot_universe() -> list[tuple[str, float]]:
    exchange = api_get("/api/v3/exchangeInfo")
    tickers = api_get("/api/v3/ticker/24hr")
    ticker_map = {x.get("symbol"): x for x in tickers if isinstance(x, dict)}
    result: list[tuple[str, float]] = []
    for item in exchange.get("symbols", []):
        symbol = item.get("symbol", "")
        base = item.get("baseAsset", "")
        if item.get("status") != "TRADING" or item.get("quoteAsset") != "USDT":
            continue
        if not item.get("isSpotTradingAllowed", True):
            continue
        is_leveraged = any(base.endswith(s) and len(base) > len(s) + 2 for s in LEVERAGED_SUFFIXES)
        is_bstock = base.endswith("B") and base not in CRYPTO_BASES_ENDING_B
        if base == "BTC" or base in IGNORED_BASES or is_leveraged or is_bstock:
            continue
        quote_volume = safe_float(ticker_map.get(symbol, {}).get("quoteVolume"))
        if quote_volume < MIN_QUOTE_VOLUME:
            continue
        result.append((symbol, quote_volume))
    return sorted(result, key=lambda x: x[1], reverse=True)

def score_watch(symbol: str, quote_volume: float, btc: dict[str, Any]) -> WatchItem | None:
    state = h1_state(fetch_ohlcv(symbol, "1h", 260))
    d = state["df"]
    price = state["price"]
    support = build_zone(d, "support", 180, 2, "1H")
    resistance = build_zone(d, "resistance", 180, 2, "1H")
    if not support or not resistance or resistance.low <= price:
        return None
    support_distance = pct_change(price, support.high)
    target_room = pct_change(resistance.low, price)
    rel1 = state["ret_1h"] - safe_float(btc["ret_1h"])
    rel4 = state["ret_4h"] - safe_float(btc["ret_4h"])
    low_liq_ok = quote_volume >= PREFERRED_QUOTE_VOLUME or state["vol_ratio"] >= LOW_LIQ_VOLUME_RATIO
    if not low_liq_ok:
        return None
    score = 0.0
    reasons: list[str] = []
    risks: list[str] = []
    structure = state["structure"]["trend"]
    if structure == "HH_HL":
        score += 18
        reasons.append("1H HH/HL yapisi")
    elif structure in {"HL_BUILDING", "HH_BUILDING"}:
        score += 11
        reasons.append("1H yukari yapi olusuyor")
    elif structure == "LH_LL":
        score -= 12
        risks.append("1H LH/LL dusus yapisi")
    if state["trend"] == "BULL":
        score += 12
        reasons.append("1H fiyat EMA20 ve EMA50 ustunde")
    elif state["trend"] == "BEAR":
        score -= 8
        risks.append("1H EMA trendi zayif")
    if rel1 >= 0.8:
        score += 12
        reasons.append(f"BTC'ye gore 1H +%{rel1:.2f} goreceli guc")
    elif rel1 >= 0.25:
        score += 7
    elif rel1 < -0.8:
        score -= 8
        risks.append("1H BTC'den belirgin zayif")
    if rel4 >= 1.5:
        score += 13
        reasons.append(f"BTC'ye gore 4H +%{rel4:.2f} goreceli guc")
    elif rel4 >= 0.5:
        score += 8
    elif rel4 < -1.5:
        score -= 8
    if state["vol_ratio"] >= 2.0:
        score += 10
        reasons.append(f"1H hacim anomalisi {state['vol_ratio']:.1f}x")
    elif state["vol_ratio"] >= 1.25:
        score += 6
    if state["taker_buy_ratio"] >= 0.55:
        score += 5
    elif state["taker_buy_ratio"] < 0.43:
        score -= 5
    if 0 <= support_distance <= 2.5:
        score += 10
        reasons.append("1H ana destege yakin")
    elif support_distance > 5:
        score -= 7
        risks.append("1H ana destege uzak")
    if target_room >= 4:
        score += 10
        reasons.append(f"Ilk dirence +%{target_room:.1f} alan")
    elif target_room >= MIN_TARGET_PCT:
        score += 5
    else:
        score -= 12
        risks.append("Ilk dirence alan dar")
    if 48 <= state["rsi"] <= 68:
        score += 5
    elif state["rsi"] > 75:
        score -= 5
        risks.append("1H RSI isinmis")
    if state["macd_hist"] > state["macd_hist_prev"]:
        score += 5
    if btc["regime"] == "YELLOW" and rel4 < 0.8:
        score -= 7
    if btc["regime"] == "RED" and rel4 < 2.0:
        score -= 15
    score = clamp(score)
    if score < MIN_WATCH_SCORE:
        return None
    return WatchItem(
        symbol=symbol, quote_volume_24h=quote_volume, watch_score=round(score, 1), h1_price=price,
        h1_atr=state["atr"], support=support, resistance=resistance, h1_trend=state["trend"],
        h1_structure=structure, relative_strength_1h=round(rel1, 3), relative_strength_4h=round(rel4, 3),
        volume_ratio_1h=round(state["vol_ratio"], 2), reasons=reasons[:6], risks=risks[:5],
    )

def build_watchlist(btc: dict[str, Any]) -> list[WatchItem]:
    universe = get_spot_universe()
    runtime["symbols"] = len(universe)
    found: list[WatchItem] = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        jobs = {pool.submit(score_watch, symbol, quote_volume, btc): symbol for symbol, quote_volume in universe}
        for future in as_completed(jobs):
            symbol = jobs[future]
            try:
                item = future.result()
                if item:
                    found.append(item)
            except Exception as exc:
                print(f"[1H] {symbol}: {str(exc)[:120]}", flush=True)
    found.sort(key=lambda x: (x.watch_score, x.relative_strength_4h, x.quote_volume_24h), reverse=True)
    return found[:WATCHLIST_MAX]

def detect_execution(item: WatchItem, btc: dict[str, Any]) -> Candidate | None:
    if btc["regime"] == "RED":
        return None
    h1 = h1_state(fetch_ohlcv(item.symbol, "1h", 220))
    m15 = m15_state(fetch_ohlcv(item.symbol, "15m", 220))
    d = m15["df"]
    price = m15["price"]
    atr = max(m15["atr"], price * 0.0015)
    support = merge_supports(h1["df"], d)
    resistance = choose_resistance(h1["df"], d, price)
    if not support or not resistance:
        return None
    structure = m15["structure"]
    last_swing_high = safe_float(structure.get("last_swing_high"))
    last_swing_low = safe_float(structure.get("last_swing_low"))
    prev_swing_low = safe_float(structure.get("prev_swing_low"))
    bullish_candle = m15["price"] > m15["last_open"]
    body = m15["price"] - m15["last_open"]
    candle_range = max(m15["last_high"] - m15["last_low"], 1e-12)
    body_ratio = body / candle_range
    mss = (
        bullish_candle and price > m15["prev_high"] and last_swing_high > 0 and
        price >= last_swing_high * 0.999 and (prev_swing_low <= 0 or last_swing_low >= prev_swing_low)
    )
    prior = d.iloc[-10:-2]
    breakout_level = safe_float(prior["high"].max())
    retest = (
        breakout_level > 0 and safe_float(d.iloc[-2]["low"]) <= breakout_level * 1.004 and
        safe_float(d.iloc[-2]["close"]) >= breakout_level * 0.997 and price > breakout_level and bullish_candle
    )
    pullback = (
        safe_float(d["low"].tail(6).min()) <= m15["ema20"] * 1.004 and
        price > m15["ema20"] > m15["ema50"] and price > m15["prev_high"] and bullish_candle
    )
    recent_ranges = (d["high"] - d["low"]).tail(10)
    older_ranges = (d["high"] - d["low"]).iloc[-30:-10]
    compressed = (
        not older_ranges.empty and safe_float(recent_ranges.iloc[:-1].median()) <= safe_float(older_ranges.median()) * 0.72
    )
    expansion = compressed and body_ratio >= 0.55 and m15["vol_ratio"] >= 1.6 and price > m15["prev_high"]
    setups = []
    if mss:
        setups.append("15M YAPI DONUSU")
    if retest:
        setups.append("15M BREAKOUT-RETEST")
    if pullback:
        setups.append("15M PULLBACK-DEVAM")
    if expansion:
        setups.append("15M SIKISMA-GENISLEME")
    if not setups:
        return None
    score = item.watch_score * 0.45
    reasons = list(item.reasons[:4])
    risks = list(item.risks[:3])
    if mss:
        score += 10
        reasons.append("15M mikro HH/HL teyidi")
    if retest:
        score += 8
        reasons.append("15M kirilim sonrasi retest korundu")
    if pullback:
        score += 7
        reasons.append("15M EMA20 pullback sonrasi devam")
    if expansion:
        score += 8
        reasons.append("15M sikisma sonrasi hacimli genisleme")
    if m15["vol_ratio"] >= 2.0:
        score += 10
    elif m15["vol_ratio"] >= 1.5:
        score += 7
    elif m15["vol_ratio"] < 0.9:
        score -= 5
        risks.append("15M hacim teyidi zayif")
    if m15["taker_buy_ratio"] >= 0.55:
        score += 2
    if 50 <= m15["rsi"] <= 70:
        score += 5
    elif m15["rsi"] > 78:
        score -= 8
        risks.append("15M RSI asiri isinmis")
    stoch_cross = m15["stoch_k"] > m15["stoch_d"] and m15["stoch_prev_k"] <= m15["stoch_prev_d"]
    if stoch_cross:
        score += 2
    if m15["macd_hist"] > m15["macd_hist_prev"]:
        score += 3
    ema_distance_atr = (price - m15["ema20"]) / max(atr, 1e-12)
    if ema_distance_atr > 2.2:
        score -= 12
        risks.append("15M EMA20'den fazla uzak; FOMO riski")
    if btc["regime"] == "YELLOW":
        if item.relative_strength_1h < 0.5 or item.relative_strength_4h < 1.0:
            return None
        score -= 3
        risks.append("BTC rejimi YELLOW; secici davran")
    structural_low = min(support.low, last_swing_low if last_swing_low > 0 else support.low)
    stop = structural_low * (1 - SUPPORT_BUFFER_PCT / 100)
    stop_pct = (price - stop) / price * 100
    if stop_pct <= 0 or stop_pct > MAX_STOP_PCT:
        return None
    target1 = resistance.low
    target_pct = pct_change(target1, price)
    if target_pct < MIN_TARGET_PCT:
        return None
    risk_per_unit = price - stop
    target2 = max(resistance.high, price + risk_per_unit * 1.5)
    rr = target_pct / stop_pct
    if target_pct >= 4:
        score += 6
    elif target_pct >= 2.5:
        score += 4
    if rr >= 1.2:
        score += 4
    elif rr < 0.65:
        score -= 8
        risks.append("Ilk dirence gore R/R zayif")
    score = clamp(score)
    if score < MIN_ENTRY_SCORE:
        return None
    risk_dollars = ACCOUNT_SIZE * (RISK_PER_TRADE_PCT / 100)
    raw_position = risk_dollars / (stop_pct / 100)
    max_position = ACCOUNT_SIZE * (MAX_POSITION_PCT / 100)
    position_size = min(raw_position, max_position)
    entry_pad = min(atr * 0.18, price * 0.0035)
    entry_low = max(support.high, price - entry_pad)
    entry_high = price + entry_pad * 0.25
    return Candidate(
        symbol=item.symbol, setup=" + ".join(setups[:2]), price=price, entry_low=entry_low,
        entry_high=entry_high, stop=stop, target1=target1, target2=target2, target_pct=target_pct,
        stop_pct=stop_pct, rr=rr, position_size=position_size, risk_dollars=risk_dollars,
        entry_score=score, btc_regime=btc["regime"], support=support, resistance=resistance,
        reasons=reasons[:7], risks=risks[:6] or ["Belirgin ek risk yok; manuel grafik kontrolu gerekli"],
        metrics={
            "watch_score": item.watch_score, "entry_score": round(score, 1),
            "quote_volume_24h": round(item.quote_volume_24h, 2),
            "relative_strength_1h": item.relative_strength_1h,
            "relative_strength_4h": item.relative_strength_4h,
            "volume_ratio_1h": item.volume_ratio_1h, "volume_ratio_15m": round(m15["vol_ratio"], 2),
            "taker_buy_ratio_15m": round(m15["taker_buy_ratio"], 3), "rsi_15m": round(m15["rsi"], 1),
            "rsi_1h": round(h1["rsi"], 1), "ema20_distance_atr_15m": round(ema_distance_atr, 2),
            "h1_trend": h1["trend"], "h1_structure": h1["structure"]["trend"],
            "m15_structure": structure["trend"], "bar_id": m15["bar_id"],
            "btc_ret_1h": round(safe_float(btc["ret_1h"]), 2),
            "btc_ret_4h": round(safe_float(btc["ret_4h"]), 2),
        },
    )

def candidate_message(c: Candidate) -> str:
    sym = c.symbol.removesuffix("USDT")
    reasons = "\n".join(f"✅ {x}" for x in c.reasons)
    risks = "\n".join(f"⚠️ {x}" for x in c.risks)
    return (
        f"🚦 <b>ENTRY READY — #{sym}</b>\n"
        f"<b>{c.setup}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 Anlik: <code>{fmt_price(c.price)}</code>\n"
        f"🟦 Giris bolgesi: <code>{fmt_price(c.entry_low)}–{fmt_price(c.entry_high)}</code>\n"
        f"🟩 Destek: <code>{fmt_price(c.support.low)}–{fmt_price(c.support.high)}</code> ({c.support.source})\n"
        f"🛑 Stop: <code>{fmt_price(c.stop)}</code> (-%{c.stop_pct:.2f})\n"
        f"🎯 TP1 / ilk direnc: <code>{fmt_price(c.target1)}</code> (+%{c.target_pct:.2f})\n"
        f"🎯 TP2 referans: <code>{fmt_price(c.target2)}</code>\n"
        f"⚖️ Ilk hedef R/R: <b>{c.rr:.2f}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 <b>$10K risk plani</b>\n"
        f"Hesap riski: <b>${c.risk_dollars:,.0f}</b> (%{RISK_PER_TRADE_PCT:g})\n"
        f"Max pozisyon: <b>${c.position_size:,.0f}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🌐 BTC rejimi: <b>{c.btc_regime}</b>\n"
        f"📐 1H secim skoru: {c.metrics['watch_score']:.1f} / 100\n"
        f"⚡ 15M giris skoru: <b>{c.entry_score:.1f} / 100</b>\n\n"
        f"<b>Neden hazir?</b>\n{reasons}\n\n"
        f"<b>Riskler</b>\n{risks}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>Spot only · Otomatik alim degildir · Manuel grafik kontrolu gerekli.</i>"
    )

def send_telegram(text: str) -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[TELEGRAM] Token/chat id yok; mesaj yalniz loglandi.", flush=True)
        return False
    payload: dict[str, Any] = {
        "chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True,
    }
    if TELEGRAM_THREAD_ID:
        payload["message_thread_id"] = TELEGRAM_THREAD_ID
    try:
        response = HTTP.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json=payload, timeout=15,
        )
        if not response.ok:
            print(f"[TELEGRAM] HTTP {response.status_code}: {response.text[:180]}", flush=True)
        return response.ok
    except Exception as exc:
        print(f"[TELEGRAM] Hata: {exc}", flush=True)
        return False

def send_portfolio(c: Candidate) -> str:
    if not PORTFOLIO_URL:
        return ""
    payload = {
        "symbol": c.symbol.replace("USDT", "/USDT"), "entry": round(c.price, 10),
        "limit_price": round(c.price, 10), "signal_price": round(c.price, 10),
        "stop": round(c.stop, 10), "tp1": round(c.target1, 10), "tp2": round(c.target2, 10),
        "tp3": None, "sig_type": "spot_opportunity", "sub_type": c.setup.lower().replace(" ", "_"),
        "source": "spot-scanner", "phase": "manual_review",
        "entry_zone": [round(c.entry_low, 10), round(c.entry_high, 10)],
        "support_zone": [round(c.support.low, 10), round(c.support.high, 10)],
        "resistance_zone": [round(c.resistance.low, 10), round(c.resistance.high, 10)],
        "target_pct": round(c.target_pct, 2), "stop_pct": round(c.stop_pct, 2), "rr": round(c.rr, 2),
        "position_size": round(c.position_size, 2), "risk_dollars": round(c.risk_dollars, 2),
        "setup": c.setup, "positives": c.reasons, "risks": c.risks, **c.metrics,
    }
    headers = {"Content-Type": "application/json"}
    if PORTFOLIO_TOKEN:
        headers["Authorization"] = f"Bearer {PORTFOLIO_TOKEN}"
    try:
        response = HTTP.post(f"{PORTFOLIO_URL}/api/signal", json=payload, headers=headers, timeout=12)
        if response.status_code in (200, 201):
            return str((response.json() or {}).get("id", ""))
        if response.status_code == 409:
            print(f"[PORTFOLIO] Zaten acik: {c.symbol}", flush=True)
            return ""
        print(f"[PORTFOLIO] HTTP {response.status_code}: {response.text[:180]}", flush=True)
    except Exception as exc:
        print(f"[PORTFOLIO] Hata: {exc}", flush=True)
    return ""

def request_analyzer(c: Candidate, portfolio_id: str) -> None:
    if not PORTFOLIO_URL or not portfolio_id:
        return
    signal = {
        "symbol": c.symbol.replace("USDT", "/USDT"), "type": "spot_opportunity", "source": "spot-scanner",
        "entry": round(c.price, 10), "stop": round(c.stop, 10), "tp1": round(c.target1, 10),
        "tp2": round(c.target2, 10), "setup": c.setup, "target_pct": round(c.target_pct, 2),
        "stop_pct": round(c.stop_pct, 2), "rr": round(c.rr, 2), "positives": c.reasons,
        "risks": c.risks, **c.metrics,
    }
    headers = {"Content-Type": "application/json"}
    if PORTFOLIO_TOKEN:
        headers["Authorization"] = f"Bearer {PORTFOLIO_TOKEN}"
    try:
        response = HTTP.post(
            f"{PORTFOLIO_URL}/api/analyze",
            json={"signal": signal, "recent_count": 0, "sig_num": 0, "portfolio_id": portfolio_id},
            headers=headers, timeout=12,
        )
        if not response.ok:
            print(f"[ANALYZER] HTTP {response.status_code}: {response.text[:160]}", flush=True)
    except Exception as exc:
        print(f"[ANALYZER] Hata: {exc}", flush=True)

def serialize_watch(item: WatchItem) -> dict[str, Any]:
    return asdict(item)

def deserialize_watch(data: dict[str, Any]) -> WatchItem:
    return WatchItem(
        symbol=data["symbol"], quote_volume_24h=safe_float(data.get("quote_volume_24h")),
        watch_score=safe_float(data.get("watch_score")), h1_price=safe_float(data.get("h1_price")),
        h1_atr=safe_float(data.get("h1_atr")), support=Zone(**data["support"]),
        resistance=Zone(**data["resistance"]), h1_trend=str(data.get("h1_trend", "MIXED")),
        h1_structure=str(data.get("h1_structure", "MIXED")),
        relative_strength_1h=safe_float(data.get("relative_strength_1h")),
        relative_strength_4h=safe_float(data.get("relative_strength_4h")),
        volume_ratio_1h=safe_float(data.get("volume_ratio_1h"), 1.0),
        reasons=list(data.get("reasons", [])), risks=list(data.get("risks", [])),
    )
def refresh_watchlist() -> tuple[dict[str, Any], list[WatchItem]]:
    runtime.update({"status": "SCANNING_1H", "last_error": None})
    started = time.time()
    btc = btc_context()
    watchlist = build_watchlist(btc)
    state = load_state()
    state["watchlist"] = [serialize_watch(x) for x in watchlist]
    state["watchlist_created_at"] = tr_now().isoformat()
    state.setdefault("alerts", {})
    save_state(state)
    runtime.update({
        "status": "RUNNING", "last_universe_scan": tr_now().isoformat(), "watchlist": len(watchlist),
        "btc_regime": btc["regime"],
    })
    print(
        f"[1H] Evren={runtime['symbols']} | watchlist={len(watchlist)} | BTC={btc['regime']} "
        f"1H {btc['ret_1h']:+.2f}% 4H {btc['ret_4h']:+.2f}% | {time.time()-started:.1f}s", flush=True,
    )
    if watchlist:
        preview = " | ".join(f"{x.symbol}:{x.watch_score:.0f}" for x in watchlist[:10])
        print(f"[1H] Top watch: {preview}", flush=True)
    return btc, watchlist

def execution_scan(btc: dict[str, Any] | None = None, watchlist: list[WatchItem] | None = None) -> list[Candidate]:
    runtime.update({"status": "SCANNING_15M", "last_error": None})
    started = time.time()
    state = load_state()
    if btc is None:
        btc = btc_context()
    if watchlist is None:
        raw_watch = state.get("watchlist", [])
        watchlist = [deserialize_watch(x) for x in raw_watch if isinstance(x, dict)]
    runtime["btc_regime"] = btc["regime"]
    if not watchlist:
        print("[15M] Watchlist bos; once 1H tarama gerekiyor.", flush=True)
        runtime.update({"status": "RUNNING", "entry_ready": 0, "last_execution_scan": tr_now().isoformat()})
        return []
    candidates: list[Candidate] = []
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, 6)) as pool:
        jobs = {pool.submit(detect_execution, item, btc): item.symbol for item in watchlist}
        for future in as_completed(jobs):
            symbol = jobs[future]
            try:
                candidate = future.result()
                if candidate:
                    candidates.append(candidate)
            except Exception as exc:
                print(f"[15M] {symbol}: {str(exc)[:120]}", flush=True)
    candidates.sort(key=lambda c: (c.entry_score, c.metrics["relative_strength_4h"]), reverse=True)
    alerts = state.setdefault("alerts", {})
    now_ts = time.time()
    sent = 0
    emitted: list[Candidate] = []
    for candidate in candidates:
        previous = alerts.get(candidate.symbol, {}) if isinstance(alerts.get(candidate.symbol), dict) else {}
        last_bar = int(previous.get("bar_id", 0) or 0)
        last_sent = safe_float(previous.get("sent_at"))
        same_bar = last_bar == int(candidate.metrics["bar_id"])
        cooldown = last_sent and now_ts - last_sent < ALERT_COOLDOWN_HOURS * 3600
        if same_bar or cooldown:
            continue
        text = candidate_message(candidate)
        print("\n" + text.replace("<b>", "").replace("</b>", "").replace("<code>", "").replace("</code>", "").replace("<i>", "").replace("</i>", ""), flush=True)
        portfolio_id = ""
        telegram_ok = False
        if DRY_RUN:
            print(f"[DRY-RUN] {candidate.symbol}: dis gonderim yapilmadi.", flush=True)
        else:
            portfolio_id = send_portfolio(candidate)
            if portfolio_id:
                threading.Thread(target=request_analyzer, args=(candidate, portfolio_id), daemon=True).start()
            telegram_ok = send_telegram(text)
        alerts[candidate.symbol] = {
            "bar_id": int(candidate.metrics["bar_id"]),
            "sent_at": now_ts if (DRY_RUN or portfolio_id or telegram_ok) else 0,
            "entry": candidate.price, "stop": candidate.stop, "tp1": candidate.target1,
            "score": candidate.entry_score,
        }
        emitted.append(candidate)
        if portfolio_id or telegram_ok:
            sent += 1
    save_state(state)
    runtime.update({
        "status": "RUNNING", "last_execution_scan": tr_now().isoformat(), "entry_ready": len(emitted), "sent": sent,
    })
    print(
        f"[15M] Watch={len(watchlist)} | ham={len(candidates)} | yeni={len(emitted)} | "
        f"gonderim={sent} | BTC={btc['regime']} | {time.time()-started:.1f}s", flush=True,
    )
    return emitted

def run_cycle(force_universe: bool = False) -> None:
    now = tr_now()
    state = load_state()
    watch_created = state.get("watchlist_created_at")
    watchlist_stale = True
    if watch_created:
        try:
            created = datetime.fromisoformat(watch_created)
            watchlist_stale = (now - created).total_seconds() >= 55 * 60
        except Exception:
            pass
    if force_universe or watchlist_stale or now.minute < 5:
        btc, watchlist = refresh_watchlist()
        execution_scan(btc, watchlist)
    else:
        execution_scan()

def scheduler_loop() -> None:
    if SCAN_ON_START:
        try:
            run_cycle(force_universe=True)
        except Exception as exc:
            runtime.update({"status": "ERROR", "last_error": str(exc)})
            print(f"[START] {exc}", flush=True)
    while True:
        now = tr_now()
        targets = [1, 16, 31, 46]
        future: list[datetime] = []
        for minute in targets:
            target = now.replace(minute=minute, second=15, microsecond=0)
            if target > now:
                future.append(target)
        next_run = min(future) if future else (now + timedelta(hours=1)).replace(minute=1, second=15, microsecond=0)
        time.sleep(max(20, (next_run - now).total_seconds()))
        try:
            run_cycle(force_universe=next_run.minute == 1)
        except Exception as exc:
            runtime.update({"status": "ERROR", "last_error": str(exc)})
            print(f"[LOOP] {exc}", flush=True)
            time.sleep(30)

@app.route("/")
@app.route("/health")
def health():
    return jsonify({"service": "spot-opportunity-scanner", **runtime}), 200

def run_flask() -> None:
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, use_reloader=False)

def main() -> None:
    print("=" * 72, flush=True)
    print("INTRADAY SPOT SCANNER — 15M execution + 1H confirmation", flush=True)
    print("Spot only | sifirdan yazildi | otomatik emir YOK", flush=True)
    print(
        f"Watch>={MIN_WATCH_SCORE:g} | Entry>={MIN_ENTRY_SCORE:g} | Risk=%{RISK_PER_TRADE_PCT:g} | "
        f"Max pozisyon=%{MAX_POSITION_PCT:g}", flush=True,
    )
    print(
        f"DRY_RUN={DRY_RUN} — " + ("Portfolio/Telegram kapali" if DRY_RUN else "Portfolio/Telegram AKTIF"), flush=True,
    )
    print("=" * 72, flush=True)
    runtime["status"] = "STARTING"
    threading.Thread(target=run_flask, daemon=True, name="health-server").start()
    scheduler_loop()

if __name__ == "__main__":
    main()
