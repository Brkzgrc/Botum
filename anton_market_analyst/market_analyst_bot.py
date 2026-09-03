#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Standalone multi-timeframe crypto analyst for Telegram.

Design goals:
- Isolated from the existing Anton deterministic engine.
- Reads Binance spot OHLCV for 1D / 4H / 1H.
- Computes a compact but rich indicator/state snapshot.
- Lets Claude Sonnet reason across timeframes instead of scoring thresholds.
- Never truncates the user-visible analysis: model continuations are stitched,
  then Telegram output is split safely across multiple messages.

Environment:
  ANTHROPIC_API_KEY           required
  TELEGRAM_BOT_TOKEN          required for standalone bot mode
  TELEGRAM_ALLOWED_CHAT_IDS   required for standalone bot mode
  MARKET_ANALYST_MODEL        optional, default: claude-sonnet-5
  MARKET_ANALYST_MAX_TOKENS   optional, default: 7000

Production Anton entegrasyonu standalone poller kullanmaz; ayni mevcut poller
`COIN GPT` komutunu anton_integration.py uzerinden bu modulun
`analyze_symbol()` fonksiyonuna yonlendirir.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import requests

BINANCE_BASE = "https://api.binance.com"
TELEGRAM_BASE = "https://api.telegram.org"
TIMEFRAMES = ("1d", "4h", "1h")
KLINE_LIMIT = 260
HTTP_TIMEOUT = 15
TELEGRAM_SAFE_CHARS = 3800
DEFAULT_MODEL = os.getenv("MARKET_ANALYST_MODEL", "claude-sonnet-5")
DEFAULT_MAX_TOKENS = int(os.getenv("MARKET_ANALYST_MAX_TOKENS", "7000"))
MAX_CONTINUATIONS = 4

SYSTEM_PROMPT = r"""
Sen deneyimli bir spot kripto piyasa analistisin. Görevin indikatörleri tek tek
raporlamak değil; 1D -> 4H -> 1H sıralamasında piyasanın hangi aşamada olduğunu
okumak ve kullanıcının şimdi ne yapmasının daha mantıklı olduğunu anlaşılır
Türkçe ile açıklamak.

ZORUNLU YAKLAŞIM:
1) Önce 1D ana yapı ve momentum rejimini belirle.
2) Sonra 4H'nin 1D içindeki rolünü belirle: devam, düzeltme, dönüş, reset,
   başarısız dönüş, dağılım vb.
3) Sonra 1H'yi zamanlama için oku: ilk dalga, aşırı ısınma, yatay soğuma,
   fiyat düşerek soğuma, ikinci tetik, momentum kaybı vb.
4) StochRSI/MA-StochRSI, RSI, MACD, KDJ, Williams %R, OBV ve hacmi bağlamına
   göre kullan. Hepsini zorla aynı ağırlıkta yorumlama.
5) "Aşırı alım = düşer" veya "aşırı satım = yükselir" gibi mekanik çıkarım
   yapma. Momentumun fiyat düşerek mi yoksa yatay kalarak mı boşaldığını özellikle
   değerlendir.
6) Mum yapısı, ATR, EMA'lar, Bollinger, son salınım high/low ve hacim davranışı
   verilen snapshot'ta varsa indikatör yorumunu teyit etmek/çürütmek için kullan.
7) BTC bağlamı verilmişse altcoin analizine bağla ama coinin kendi yapısını ezme.
8) Kesinlik dili kullanma. Senaryo, tetikleyici ve bozulma koşulunu açık yaz.
9) Kullanıcı spot işlem yapıyor. Kaldıraç/futures önerme.
10) Sonuç yalnızca etiket olmasın. Gerekliyse uzun yaz; fakat tekrar etme.

ÇIKTI DÜZENİ:
- İlk paragraf: tek bakışta ana sonuç.
- "1D", "4H", "1H" başlıkları altında zaman dilimi muhakemesi.
- "Birlikte okuma" bölümünde zaman dilimlerinin birbirine nasıl bağlandığını anlat.
- "Şu an ne yapardım?" bölümünde pratik aksiyon: örn. BEKLE / TUT / YENİ ALIMI
  KOVALAMA / TETİK BEKLE / UZAK DUR. Bunun nedenini ve hangi koşulda aksiyonun
  değişeceğini yaz.
- "Bozulma / teyit" bölümünde 2-4 somut koşul ver.

Veride olmayan bir şeyi uydurma. Fiyat seviyesi veya destek/direnç verilmişse
kullanabilirsin; verilmemiş kesin seviye üretme.
""".strip()


@dataclass
class Candle:
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time: int


class BinanceError(RuntimeError):
    pass


def _sma(values: Sequence[Optional[float]], period: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(values)
    for i in range(period - 1, len(values)):
        window = values[i - period + 1 : i + 1]
        if any(v is None for v in window):
            continue
        out[i] = sum(float(v) for v in window) / period
    return out


def _ema(values: Sequence[float], period: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(values)
    if len(values) < period:
        return out
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    alpha = 2.0 / (period + 1.0)
    prev = seed
    for i in range(period, len(values)):
        prev = (values[i] - prev) * alpha + prev
        out[i] = prev
    return out


def _rsi(values: Sequence[float], period: int = 14) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(values)
    if len(values) <= period:
        return out
    gains, losses = [], []
    for i in range(1, period + 1):
        d = values[i] - values[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    def calc(g: float, l: float) -> float:
        if l == 0:
            return 100.0 if g > 0 else 50.0
        rs = g / l
        return 100.0 - (100.0 / (1.0 + rs))

    out[period] = calc(avg_gain, avg_loss)
    for i in range(period + 1, len(values)):
        d = values[i] - values[i - 1]
        gain, loss = max(d, 0.0), max(-d, 0.0)
        avg_gain = ((avg_gain * (period - 1)) + gain) / period
        avg_loss = ((avg_loss * (period - 1)) + loss) / period
        out[i] = calc(avg_gain, avg_loss)
    return out


def _stoch_rsi(rsi_values: Sequence[Optional[float]], period: int = 14, ma_period: int = 3) -> Tuple[List[Optional[float]], List[Optional[float]]]:
    stoch: List[Optional[float]] = [None] * len(rsi_values)
    for i in range(len(rsi_values)):
        if i < period - 1 or rsi_values[i] is None:
            continue
        window = rsi_values[i - period + 1 : i + 1]
        if any(v is None for v in window):
            continue
        vals = [float(v) for v in window]
        lo, hi = min(vals), max(vals)
        stoch[i] = 50.0 if hi == lo else 100.0 * (float(rsi_values[i]) - lo) / (hi - lo)
    return stoch, _sma(stoch, ma_period)


def _macd(values: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9):
    ef, es = _ema(values, fast), _ema(values, slow)
    line: List[Optional[float]] = [None] * len(values)
    valid_idx, valid_vals = [], []
    for i, (a, b) in enumerate(zip(ef, es)):
        if a is not None and b is not None:
            line[i] = a - b
            valid_idx.append(i)
            valid_vals.append(a - b)
    sig_valid = _ema(valid_vals, signal)
    sig: List[Optional[float]] = [None] * len(values)
    hist: List[Optional[float]] = [None] * len(values)
    for pos, idx in enumerate(valid_idx):
        if sig_valid[pos] is not None:
            sig[idx] = sig_valid[pos]
            hist[idx] = float(line[idx]) - float(sig[idx])
    return line, sig, hist


def _true_range(candles: Sequence[Candle]) -> List[float]:
    tr = []
    for i, c in enumerate(candles):
        if i == 0:
            tr.append(c.high - c.low)
        else:
            pc = candles[i - 1].close
            tr.append(max(c.high - c.low, abs(c.high - pc), abs(c.low - pc)))
    return tr


def _atr(candles: Sequence[Candle], period: int = 14) -> List[Optional[float]]:
    tr = _true_range(candles)
    out: List[Optional[float]] = [None] * len(tr)
    if len(tr) < period:
        return out
    prev = sum(tr[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(tr)):
        prev = ((prev * (period - 1)) + tr[i]) / period
        out[i] = prev
    return out


def _williams_r(candles: Sequence[Candle], period: int = 14) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(candles)
    for i in range(period - 1, len(candles)):
        w = candles[i - period + 1 : i + 1]
        hh, ll = max(c.high for c in w), min(c.low for c in w)
        out[i] = -50.0 if hh == ll else -100.0 * (hh - candles[i].close) / (hh - ll)
    return out


def _kdj(candles: Sequence[Candle], period: int = 9):
    k: List[Optional[float]] = [None] * len(candles)
    d: List[Optional[float]] = [None] * len(candles)
    j: List[Optional[float]] = [None] * len(candles)
    prev_k = prev_d = 50.0
    for i in range(len(candles)):
        if i < period - 1:
            continue
        w = candles[i - period + 1 : i + 1]
        hh, ll = max(c.high for c in w), min(x.low for x in w)
        rsv = 50.0 if hh == ll else 100.0 * (candles[i].close - ll) / (hh - ll)
        prev_k = (2.0 / 3.0) * prev_k + (1.0 / 3.0) * rsv
        prev_d = (2.0 / 3.0) * prev_d + (1.0 / 3.0) * prev_k
        k[i], d[i], j[i] = prev_k, prev_d, 3.0 * prev_k - 2.0 * prev_d
    return k, d, j


def _obv(candles: Sequence[Candle]) -> List[float]:
    out = [0.0] * len(candles)
    for i in range(1, len(candles)):
        if candles[i].close > candles[i - 1].close:
            out[i] = out[i - 1] + candles[i].volume
        elif candles[i].close < candles[i - 1].close:
            out[i] = out[i - 1] - candles[i].volume
        else:
            out[i] = out[i - 1]
    return out


def _pct(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None or b in (None, 0):
        return None
    return 100.0 * (a / b - 1.0)


def _last_non_none(values: Sequence[Optional[float]], offset: int = 0) -> Optional[float]:
    seen = 0
    for v in reversed(values):
        if v is not None:
            if seen == offset:
                return float(v)
            seen += 1
    return None


def _trend_word(current: Optional[float], prev: Optional[float], eps: float = 1e-9) -> str:
    if current is None or prev is None:
        return "veri_yok"
    if current > prev + eps:
        return "yukari"
    if current < prev - eps:
        return "asagi"
    return "yatay"


def _fmt(v: Optional[float], digits: int = 2):
    return None if v is None or not math.isfinite(v) else round(v, digits)


def fetch_klines(symbol: str, interval: str, limit: int = KLINE_LIMIT) -> List[Candle]:
    pair = normalize_pair(symbol)
    r = requests.get(
        f"{BINANCE_BASE}/api/v3/klines",
        params={"symbol": pair, "interval": interval, "limit": limit},
        timeout=HTTP_TIMEOUT,
    )
    if r.status_code != 200:
        raise BinanceError(f"Binance kline hatası ({pair} {interval}): {r.text[:180]}")
    raw = r.json()
    now_ms = int(time.time() * 1000)
    candles = [
        Candle(int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5]), int(x[6]))
        for x in raw
        if int(x[6]) < now_ms
    ]
    if len(candles) < 80:
        raise BinanceError(f"{pair} {interval}: yeterli kapanmış mum yok ({len(candles)}).")
    return candles


def fetch_live_price(symbol: str) -> float:
    pair = normalize_pair(symbol)
    r = requests.get(f"{BINANCE_BASE}/api/v3/ticker/price", params={"symbol": pair}, timeout=HTTP_TIMEOUT)
    if r.status_code != 200:
        raise BinanceError(f"Binance fiyat hatası ({pair}): {r.text[:180]}")
    return float(r.json()["price"])


def normalize_pair(symbol: str) -> str:
    s = re.sub(r"[^A-Z0-9]", "", symbol.upper().strip())
    if s.endswith("USDT"):
        return s
    return s + "USDT"


def base_symbol(symbol: str) -> str:
    pair = normalize_pair(symbol)
    return pair[:-4] if pair.endswith("USDT") else pair


def _recent_swings(candles: Sequence[Candle], lookback: int = 60, wing: int = 2):
    subset = candles[-lookback:]
    highs, lows = [], []
    for i in range(wing, len(subset) - wing):
        c = subset[i]
        if c.high == max(x.high for x in subset[i - wing : i + wing + 1]):
            highs.append(c.high)
        if c.low == min(x.low for x in subset[i - wing : i + wing + 1]):
            lows.append(c.low)
    last = candles[-1].close
    below = [x for x in lows if x <= last]
    above = [x for x in highs if x >= last]
    support = max(below) if below else min(c.low for c in subset)
    resistance = min(above) if above else max(c.high for c in subset)
    return support, resistance


def build_timeframe_snapshot(candles: Sequence[Candle]) -> dict:
    closes = [c.close for c in candles]
    vols = [c.volume for c in candles]
    rsi = _rsi(closes, 14)
    stoch, stoch_ma = _stoch_rsi(rsi, 14, 3)
    macd, macd_signal, macd_hist = _macd(closes)
    k, d, j = _kdj(candles)
    will = _williams_r(candles)
    obv = _obv(candles)
    ema20, ema50, ema200 = _ema(closes, 20), _ema(closes, 50), _ema(closes, 200)
    atr = _atr(candles, 14)

    last, prev = candles[-1], candles[-2]
    support, resistance = _recent_swings(candles)
    atr_last = _last_non_none(atr)
    bb_window = closes[-20:]
    bb_mid = statistics.mean(bb_window)
    bb_std = statistics.pstdev(bb_window)
    bb_upper, bb_lower = bb_mid + 2 * bb_std, bb_mid - 2 * bb_std
    avgv20 = statistics.mean(vols[-21:-1]) if len(vols) >= 21 else statistics.mean(vols[-20:])

    body = abs(last.close - last.open)
    rng = max(last.high - last.low, 1e-12)
    upper_wick = last.high - max(last.open, last.close)
    lower_wick = min(last.open, last.close) - last.low

    obv_now, obv_5 = obv[-1], obv[-6] if len(obv) >= 6 else obv[0]
    closes_3 = closes[-4:]
    price_3bar = _pct(closes_3[-1], closes_3[0]) if len(closes_3) == 4 else None
    range3 = max(c.high for c in candles[-3:]) - min(c.low for c in candles[-3:])
    sideways_ratio = (range3 / atr_last) if atr_last not in (None, 0) else None

    return {
        "last_closed": {
            "time_utc": datetime.fromtimestamp(last.close_time / 1000, tz=timezone.utc).isoformat(),
            "open": _fmt(last.open, 8), "high": _fmt(last.high, 8), "low": _fmt(last.low, 8), "close": _fmt(last.close, 8),
            "change_pct": _fmt(_pct(last.close, prev.close)),
            "body_pct_of_range": _fmt(100 * body / rng),
            "upper_wick_pct_of_range": _fmt(100 * upper_wick / rng),
            "lower_wick_pct_of_range": _fmt(100 * lower_wick / rng),
        },
        "momentum": {
            "rsi14": _fmt(_last_non_none(rsi)),
            "rsi_direction": _trend_word(_last_non_none(rsi), _last_non_none(rsi, 1)),
            "stochrsi": _fmt(_last_non_none(stoch)),
            "ma_stochrsi": _fmt(_last_non_none(stoch_ma)),
            "stochrsi_direction": _trend_word(_last_non_none(stoch), _last_non_none(stoch, 1)),
            "stoch_vs_ma": "ustunde" if (_last_non_none(stoch) or -1) > (_last_non_none(stoch_ma) or 101) else "altinda",
            "macd": _fmt(_last_non_none(macd), 8),
            "macd_signal": _fmt(_last_non_none(macd_signal), 8),
            "macd_hist": _fmt(_last_non_none(macd_hist), 8),
            "macd_hist_direction": _trend_word(_last_non_none(macd_hist), _last_non_none(macd_hist, 1)),
            "kdj_k": _fmt(_last_non_none(k)), "kdj_d": _fmt(_last_non_none(d)), "kdj_j": _fmt(_last_non_none(j)),
            "williams_r14": _fmt(_last_non_none(will)),
        },
        "trend_structure": {
            "ema20": _fmt(_last_non_none(ema20), 8), "ema50": _fmt(_last_non_none(ema50), 8), "ema200": _fmt(_last_non_none(ema200), 8),
            "close_vs_ema20_pct": _fmt(_pct(last.close, _last_non_none(ema20))),
            "close_vs_ema50_pct": _fmt(_pct(last.close, _last_non_none(ema50))),
            "close_vs_ema200_pct": _fmt(_pct(last.close, _last_non_none(ema200))),
            "support_recent": _fmt(support, 8), "resistance_recent": _fmt(resistance, 8),
            "distance_support_pct": _fmt(_pct(last.close, support)),
            "distance_resistance_pct": _fmt(_pct(resistance, last.close)),
        },
        "volatility_volume": {
            "atr14": _fmt(atr_last, 8),
            "atr_pct": _fmt(100 * atr_last / last.close if atr_last is not None else None),
            "bollinger_mid": _fmt(bb_mid, 8), "bollinger_upper": _fmt(bb_upper, 8), "bollinger_lower": _fmt(bb_lower, 8),
            "volume_vs_20bar_avg": _fmt(last.volume / avgv20 if avgv20 else None),
            "obv_direction_5bar": "yukari" if obv_now > obv_5 else "asagi" if obv_now < obv_5 else "yatay",
        },
        "short_behavior": {
            "price_change_3bar_pct": _fmt(price_3bar),
            "three_bar_range_atr_multiple": _fmt(sideways_ratio),
            "note": "3-bar range/ATR düşük ve fiyat değişimi sınırlıysa momentum yatay kalarak soğuyor olabilir; otomatik hüküm değildir.",
        },
    }


def build_market_snapshot(symbol: str) -> dict:
    base = base_symbol(symbol)
    snapshot = {
        "symbol": base,
        "pair": normalize_pair(symbol),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "live_price": fetch_live_price(symbol),
        "timeframes": {},
        "indicator_parameters": {
            "RSI": 14, "StochRSI": 14, "MA_StochRSI": 3, "MACD": "12,26,9",
            "KDJ": "9,3,3-style smoothing", "WilliamsR": 14, "ATR": 14,
            "EMA": [20, 50, 200], "Bollinger": "20,2",
        },
    }
    for tf in TIMEFRAMES:
        snapshot["timeframes"][tf] = build_timeframe_snapshot(fetch_klines(symbol, tf))

    if base != "BTC":
        btc = {"live_price": fetch_live_price("BTC"), "timeframes": {}}
        for tf in TIMEFRAMES:
            btc["timeframes"][tf] = build_timeframe_snapshot(fetch_klines("BTC", tf))
        snapshot["btc_context"] = btc
    return snapshot


def _analysis_prompt(snapshot: dict) -> str:
    return (
        "Aşağıdaki gerçek Binance spot snapshot'ını analiz et. Sayıları tekrar sıralamak yerine "
        "zaman dilimleri arasındaki ilişkiyi yorumla. Özellikle 1H momentum soğumasının fiyat düşüşüyle mi "
        "yoksa yatay fiyat davranışıyla mı gerçekleştiğini verilen veriden tartış.\n\n"
        + json.dumps(snapshot, ensure_ascii=False, indent=2)
    )


def analyze_with_claude(snapshot: dict) -> str:
    key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY tanımlı değil.")
    from anthropic import Anthropic
    client = Anthropic(api_key=key)
    prompt = _analysis_prompt(snapshot)
    messages = [{"role": "user", "content": prompt}]
    chunks: List[str] = []

    for continuation in range(MAX_CONTINUATIONS + 1):
        resp = client.messages.create(
            model=DEFAULT_MODEL,
            max_tokens=DEFAULT_MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=messages,
        )
        text = "".join(block.text for block in resp.content if getattr(block, "type", None) == "text").strip()
        if not text:
            raise RuntimeError("AI analist boş yanıt döndürdü.")
        chunks.append(text)
        if getattr(resp, "stop_reason", None) != "max_tokens":
            break
        if continuation >= MAX_CONTINUATIONS:
            chunks.append("\n[UYARI: Model azami devam sayısına ulaştı.]")
            break
        messages.extend([
            {"role": "assistant", "content": text},
            {"role": "user", "content": "Yanıt token sınırında kesildi. Tam kaldığın yerden devam et; tekrar etme ve analizi mutlaka tamamla."},
        ])

    return "\n\n".join(chunks).strip()


def analyze_symbol(symbol: str) -> str:
    """Tek sembol icin snapshot olustur ve Sonnet analizini dondur."""
    return analyze_with_claude(build_market_snapshot(symbol))


def split_telegram(text: str, max_chars: int = TELEGRAM_SAFE_CHARS) -> List[str]:
    text = text.strip()
    if len(text) <= max_chars:
        return [text]
    parts: List[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= max_chars:
            parts.append(remaining.strip())
            break
        cut = remaining.rfind("\n\n", 0, max_chars)
        if cut < max_chars * 0.55:
            cut = remaining.rfind("\n", 0, max_chars)
        if cut < max_chars * 0.55:
            cut = remaining.rfind(". ", 0, max_chars)
            if cut != -1:
                cut += 1
        if cut < max_chars * 0.55:
            cut = max_chars
        parts.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    if len(parts) > 1:
        total = len(parts)
        return [f"({i}/{total})\n{p}" for i, p in enumerate(parts, 1)]
    return parts


def telegram_request(token: str, method: str, **payload):
    r = requests.post(f"{TELEGRAM_BASE}/bot{token}/{method}", json=payload, timeout=HTTP_TIMEOUT + 20)
    if r.status_code != 200:
        raise RuntimeError(f"Telegram {method} hatası: {r.text[:300]}")
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram {method} başarısız: {data}")
    return data.get("result")


def send_analysis(token: str, chat_id: int, text: str, thread_id: int | None = None):
    for part in split_telegram(text):
        payload = {"chat_id": chat_id, "text": part, "disable_web_page_preview": True}
        if thread_id is not None:
            payload["message_thread_id"] = thread_id
        telegram_request(token, "sendMessage", **payload)


def parse_allowed_chat_ids() -> set[int]:
    raw = os.getenv("TELEGRAM_ALLOWED_CHAT_IDS", "").strip()
    if not raw:
        return set()
    out = set()
    for item in raw.split(","):
        item = item.strip()
        if item:
            out.add(int(item))
    return out


def extract_symbol(text: str) -> Optional[str]:
    t = text.strip().upper()
    if t in {"/START", "/HELP"}:
        return None
    if t.startswith("/ANALIZ ") or t.startswith("/ANALYZE "):
        t = t.split(maxsplit=1)[1]
    t = re.sub(r"\s+", "", t)
    if re.fullmatch(r"[A-Z0-9]{2,15}(USDT)?", t):
        return base_symbol(t)
    return None


def run_bot():
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    allowed = parse_allowed_chat_ids()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN tanımlı değil.")
    if not allowed:
        raise RuntimeError("TELEGRAM_ALLOWED_CHAT_IDS boş. Güvenlik için bot herkese açık başlatılmaz.")

    print(f"Anton MTF Analyst başladı | model={DEFAULT_MODEL} | allowed_chats={len(allowed)}", flush=True)
    offset = None
    while True:
        try:
            payload = {"timeout": 45, "allowed_updates": ["message"]}
            if offset is not None:
                payload["offset"] = offset
            updates = telegram_request(token, "getUpdates", **payload)
            for upd in updates or []:
                offset = upd["update_id"] + 1
                msg = upd.get("message") or {}
                chat = msg.get("chat") or {}
                chat_id = chat.get("id")
                text = msg.get("text") or ""
                if chat_id not in allowed:
                    continue
                if text.strip().lower() in {"/start", "/help"}:
                    telegram_request(token, "sendMessage", chat_id=chat_id,
                                     text="Coin sembolünü yaz: ZEC, BTC, ETH gibi. 1D + 4H + 1H birlikte analiz edilir.")
                    continue
                symbol = extract_symbol(text)
                if not symbol:
                    telegram_request(token, "sendMessage", chat_id=chat_id,
                                     text="Yalnız coin sembolü yaz (ör. ZEC) veya /analiz ZEC kullan.")
                    continue
                telegram_request(token, "sendChatAction", chat_id=chat_id, action="typing")
                try:
                    analysis = analyze_symbol(symbol)
                    header = f"{symbol}/USDT — Çoklu Zaman Dilimi Analizi\n\n"
                    send_analysis(token, chat_id, header + analysis)
                except Exception as exc:
                    telegram_request(token, "sendMessage", chat_id=chat_id,
                                     text=f"{symbol} analizi tamamlanamadı: {type(exc).__name__}: {exc}")
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            print(f"[BOT] geçici hata: {type(exc).__name__}: {exc}", flush=True)
            time.sleep(5)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", help="Telegram olmadan tek coin analiz et (örn. ZEC)")
    parser.add_argument("--snapshot-only", action="store_true", help="AI çağırmadan snapshot JSON yazdır")
    args = parser.parse_args()

    if args.symbol:
        snap = build_market_snapshot(args.symbol)
        if args.snapshot_only:
            print(json.dumps(snap, ensure_ascii=False, indent=2))
        else:
            print(analyze_with_claude(snap))
        return
    run_bot()


if __name__ == "__main__":
    main()
