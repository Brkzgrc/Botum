# -*- coding: utf-8 -*-
"""
Claude Analyzer — Merkezi Sinyal Değerlendirme Sistemi
======================================================
bot.py ve ileride SMC'den gelen sinyalleri Claude API ile değerlendirir.
Kararları ANALYZER_TELEGRAM_TOKEN üzerinden ayrı bir Telegram botu ile gönderir.

Bağımsız modül — bot.py veya SMC'nin iç yapısına bağlı değildir.
Yeni bir sistem eklemek için: signals = {...}; process_and_send(signals)
"""

import os
import threading
import time
import requests
import numpy as np
import pandas as pd
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

# ============================================================
# AYARLAR
# ============================================================
ANTHROPIC_API_KEY       = os.getenv("ANTHROPIC_API_KEY",       "")
ANALYZER_TELEGRAM_TOKEN = os.getenv("ANALYZER_TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID        = os.getenv("ANALYZER_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID", "")
PORTFOLIO_URL           = os.getenv("PORTFOLIO_URL",           "")
PORTFOLIO_TOKEN         = os.getenv("PORTFOLIO_TOKEN",         "")

TR_TZ = timezone(timedelta(hours=3))

def _tr_now():
    return datetime.now(timezone.utc).astimezone(TR_TZ)

# ============================================================
# TELEGRAM
# ============================================================
def send_decision(text: str, thread_id: int | None = 38):
    token = ANALYZER_TELEGRAM_TOKEN
    if not token or not TELEGRAM_CHAT_ID:
        print("[ANALYZER] Token veya chat_id eksik, mesaj gönderilemedi.", flush=True)
        return
    try:
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text,
                   "parse_mode": "HTML", "disable_web_page_preview": True}
        if thread_id:
            payload["message_thread_id"] = thread_id
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json=payload, timeout=10,
        )
        if r.status_code != 200:
            print(f"[ANALYZER TG] {r.status_code}: {r.text[:80]}", flush=True)
    except Exception as e:
        print(f"[ANALYZER TG] Hata: {e}", flush=True)

# ============================================================
# VERİ ÇEKME — BİNANCE REST
# ============================================================
def _fetch_klines(symbol: str, interval: str, limit: int) -> dict | None:
    pair = symbol.replace("/", "").upper()
    try:
        r = requests.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": pair, "interval": interval, "limit": limit},
            timeout=10,
        )
        if r.status_code != 200:
            return None
        raw = r.json()
        return {
            "opens":   [float(k[1]) for k in raw],
            "closes":  [float(k[4]) for k in raw],
            "highs":   [float(k[2]) for k in raw],
            "lows":    [float(k[3]) for k in raw],
            "volumes": [float(k[5]) for k in raw],
        }
    except Exception:
        return None

# ============================================================
# İNDİKATÖRLER
# ============================================================
def _rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    a = np.array(closes[-(period * 3):], dtype=float)
    d = np.diff(a)
    g = np.where(d > 0, d, 0.0)
    l = np.where(d < 0, -d, 0.0)
    ag = np.mean(g[-period:])
    al = np.mean(l[-period:])
    if al == 0:
        return 100.0
    return round(100 - 100 / (1 + ag / al), 1)

def _ema(closes, span):
    if len(closes) < span // 2:
        return None
    s = pd.Series(closes)
    return round(float(s.ewm(span=span, adjust=False).mean().iloc[-1]), 8)

def _vol_ratio(volumes, period=20):
    if len(volumes) < period + 1:
        return None
    avg = np.mean(volumes[-period - 1:-1])
    return round(float(volumes[-1]) / avg, 2) if avg > 0 else None

def _tf_summary(symbol: str, interval: str, limit: int) -> dict | None:
    data = _fetch_klines(symbol, interval, limit)
    if not data:
        return None
    c = data["closes"]
    v = data["volumes"]
    return {
        "close":     round(float(c[-1]), 8),
        "rsi":       _rsi(c),
        "ema50":     _ema(c, 50),
        "ema200":    _ema(c, 200),
        "vol_ratio": _vol_ratio(v),
    }

def _tf_line(label: str, d: dict | None) -> str:
    if not d:
        return f"  {label}: —"
    parts = []
    if d.get("rsi")       is not None: parts.append(f"RSI {d['rsi']}")
    if d.get("ema50")     is not None: parts.append(f"EMA50 {d['ema50']}")
    if d.get("ema200")    is not None: parts.append(f"EMA200 {d['ema200']}")
    if d.get("vol_ratio") is not None: parts.append(f"Hacim {d['vol_ratio']}x")
    return f"  {label}: Fiyat {d['close']} | {' | '.join(parts)}"

def _fetch_all_tf(symbol: str) -> dict:
    """Coin ve BTC için tüm timeframe verilerini paralel çeker."""
    tasks = {
        "coin_1h": (symbol,    "1h",  100),
        "coin_4h": (symbol,    "4h",  100),
        "coin_1d": (symbol,    "1d",  200),
        "btc_1h":  ("BTC/USDT","1h",  100),
        "btc_4h":  ("BTC/USDT","4h",  100),
    }
    results = {}
    with ThreadPoolExecutor(max_workers=5) as ex:
        futures = {ex.submit(_tf_summary, sym, tf, lim): key
                   for key, (sym, tf, lim) in tasks.items()}
        for fut in as_completed(futures):
            key = futures[fut]
            try:
                results[key] = fut.result()
            except Exception:
                results[key] = None
    return results

# ============================================================
# FEAR & GREED
# ============================================================
def _fear_greed():
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=5)
        if r.status_code == 200:
            d = r.json()["data"][0]
            return int(d["value"]), d["value_classification"]
    except Exception:
        pass
    return None, None

# ============================================================
# BTC DOMINANS (4 saatlik cache)
# ============================================================
_dom_cache: dict = {"data": None, "ts": 0}

def _dominance() -> dict | None:
    now = time.time()
    if _dom_cache["data"] and now - _dom_cache["ts"] < 14400:
        return _dom_cache["data"]
    try:
        r = requests.get("https://api.coingecko.com/api/v3/global", timeout=10)
        if r.status_code != 200:
            return _dom_cache["data"]
        current_dom = float(r.json()["data"]["market_cap_percentage"].get("btc", 0))

        r_btc = requests.get(
            "https://api.coingecko.com/api/v3/coins/bitcoin/market_chart",
            params={"vs_currency": "usd", "days": "30", "interval": "daily"},
            timeout=15,
        )
        r_tot = requests.get(
            "https://api.coingecko.com/api/v3/global/market_cap_chart",
            params={"days": "30"}, timeout=15,
        )
        series = []
        if r_btc.status_code == 200 and r_tot.status_code == 200:
            btc_caps = r_btc.json().get("market_caps", [])
            tot_raw  = r_tot.json()
            tot_caps = (tot_raw.get("market_cap_chart", {}).get("market_cap")
                        or tot_raw.get("market_cap") or [])
            for i in range(min(len(btc_caps), len(tot_caps))):
                t = float(tot_caps[i][1])
                if t > 0:
                    series.append(float(btc_caps[i][1]) / t * 100)

        result: dict = {"current": round(current_dom, 2), "trend_dir": None,
                        "slope_30d": None, "distance": None}
        if len(series) >= 14:
            x = np.arange(len(series), dtype=float)
            y = np.array(series)
            slope, intercept = np.polyfit(x, y, 1)
            trend_val = slope * (len(series) - 1) + intercept
            slope_30d = slope * len(series)
            result.update({
                "trend_dir": "yükseliş" if slope_30d > 1 else ("düşüş" if slope_30d < -1 else "yatay"),
                "slope_30d": round(slope_30d, 2),
                "distance":  round(current_dom - trend_val, 2),
            })
        _dom_cache["data"] = result
        _dom_cache["ts"]   = now
        return result
    except Exception as e:
        print(f"[ANALYZER DOM] {e}", flush=True)
        return _dom_cache["data"]

def _dom_str(dom: dict | None) -> str:
    if not dom:
        return "BTC Dominans: veri yok"
    cur = dom["current"]
    td  = dom.get("trend_dir")
    if not td:
        return f"BTC Dominans: %{cur} (trend verisi yok)"
    sl  = dom.get("slope_30d", 0) or 0
    di  = dom.get("distance")
    line = f"BTC Dominans: %{cur} | 30G Trend: {td} ({sl:+.1f}pp)"
    if di is not None:
        if abs(di) < 0.5:
            line += ("\n  ⚠️ Yükselen trend çizgisine yakın — kırılırsa altcoin sezonu" if td == "yükseliş"
                     else "\n  ⚠️ Düşen trend çizgisine yakın — kırılırsa BTC baskısı azalır")
        elif di > 1.5:
            line += "\n  📈 Trend üstünde — BTC güçlü, altcoinler baskıda"
        elif di < -1.5:
            line += "\n  📉 Trend altında — altcoinlere para akıyor"
    return line

# ============================================================
# BTC MAKRO ANALİZ — 200-Haftalık MA + Fibonacci + Haftalık S/R
# ============================================================
_macro_cache: dict = {"data": None, "ts": 0}

def _fetch_btc_macro() -> dict | None:
    """210 haftalık BTC verisi: 200W MA, Fibonacci (ATH→dip), haftalık pivot S/R. 12 saatlik cache."""
    now = time.time()
    if _macro_cache["data"] and now - _macro_cache["ts"] < 43200:
        return _macro_cache["data"]
    try:
        r = requests.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": "BTCUSDT", "interval": "1w", "limit": 210},
            timeout=15,
        )
        if r.status_code != 200:
            return _macro_cache["data"]
        raw      = r.json()
        w_closes = [float(k[4]) for k in raw]
        w_highs  = [float(k[2]) for k in raw]
        w_lows   = [float(k[3]) for k in raw]

        result: dict = {}

        # 200-haftalık MA
        if len(w_closes) >= 200:
            result["ma200w"] = round(float(np.mean(w_closes[-200:])), 0)

        # Fibonacci: son 3 yıldaki ATH → ATH sonrası döngü dibi
        lookback = min(156, len(w_highs))
        rel_idx  = int(np.argmax(w_highs[-lookback:]))
        ath_idx  = len(w_highs) - lookback + rel_idx
        ath      = float(w_highs[ath_idx])
        if ath_idx < len(w_lows) - 1:
            cycle_low = float(np.min(w_lows[ath_idx:]))
        else:
            cycle_low = float(np.min(w_lows[-52:]))
        fib_range = ath - cycle_low
        result["fib"] = {
            "ath":       round(ath, 0),
            "cycle_low": round(cycle_low, 0),
            "0.236":     round(ath - fib_range * 0.236, 0),
            "0.382":     round(ath - fib_range * 0.382, 0),
            "0.500":     round(ath - fib_range * 0.500, 0),
            "0.618":     round(ath - fib_range * 0.618, 0),
            "0.786":     round(ath - fib_range * 0.786, 0),
        }

        # Haftalık pivot S/R (son 52 hafta, ±2 bar pencere)
        pivots: list[float] = []
        start = max(2, len(w_highs) - 52)
        for i in range(start, len(w_highs) - 2):
            if (w_highs[i] > w_highs[i-1] and w_highs[i] > w_highs[i-2] and
                    w_highs[i] > w_highs[i+1] and w_highs[i] > w_highs[i+2]):
                pivots.append(w_highs[i])
            if (w_lows[i] < w_lows[i-1] and w_lows[i] < w_lows[i-2] and
                    w_lows[i] < w_lows[i+1] and w_lows[i] < w_lows[i+2]):
                pivots.append(w_lows[i])
        pivots.sort()
        clustered: list[float] = []
        for p in pivots:
            if not clustered or p > clustered[-1] * 1.03:
                clustered.append(p)
            else:
                clustered[-1] = round((clustered[-1] + p) / 2, 0)
        result["weekly_sr"] = [round(p, 0) for p in clustered]

        # Fibonacci Bollinger Bands (Rashad) — SMA(20) + ATR(20) × Fib — haftalık
        if len(w_closes) >= 21:
            trs = [max(w_highs[i] - w_lows[i],
                       abs(w_highs[i] - w_closes[i-1]),
                       abs(w_lows[i] - w_closes[i-1]))
                   for i in range(1, len(w_closes))]
            atr20 = sum(trs[-20:]) / 20
            sma20 = sum(w_closes[-20:]) / 20
            result["fbb"] = {
                "sma":        round(sma20, 0),
                "lower_1618": round(sma20 - atr20 * 1.618, 0),
                "lower_2618": round(sma20 - atr20 * 2.618, 0),
                "lower_4236": round(sma20 - atr20 * 4.236, 0),
                "upper_1618": round(sma20 + atr20 * 1.618, 0),
                "upper_2618": round(sma20 + atr20 * 2.618, 0),
            }

        # SSL Hybrid (Mikhel00) — SMA(10) High vs Low — haftalık
        ssl_p = 10
        if len(w_highs) >= ssl_p + 1:
            sma_h      = sum(w_highs[-ssl_p:])      / ssl_p
            sma_l      = sum(w_lows[-ssl_p:])       / ssl_p
            prev_sma_h = sum(w_highs[-ssl_p-1:-1])  / ssl_p
            prev_sma_l = sum(w_lows[-ssl_p-1:-1])   / ssl_p
            cur, prev  = w_closes[-1], w_closes[-2]
            cur_sig  = "bullish" if cur  > sma_h  else ("bearish" if cur  < sma_l  else "neutral")
            prev_sig = "bullish" if prev > prev_sma_h else ("bearish" if prev < prev_sma_l else "neutral")
            cross = None
            if prev_sig == "bullish" and cur_sig == "bearish":
                cross = "boğadan ayıya döndü — güçlü düşüş uyarısı"
            elif prev_sig == "bearish" and cur_sig == "bullish":
                cross = "ayıdan boğaya döndü — dönüş sinyali"
            result["ssl"] = {"signal": cur_sig, "cross": cross,
                             "sma_h": round(sma_h, 0), "sma_l": round(sma_l, 0)}

        _macro_cache["data"] = result
        _macro_cache["ts"]   = now
        return result
    except Exception as e:
        print(f"[ANALYZER MACRO] {e}", flush=True)
        return _macro_cache["data"]

def _btc_macro_str(macro: dict | None, btc_price: float | None = None) -> str:
    if not macro:
        return ""
    lines: list[str] = []

    if "ma200w" in macro:
        ma = macro["ma200w"]
        if btc_price:
            dist = (btc_price - ma) / ma * 100
            pos  = "üstünde" if dist > 0 else "altında"
            lines.append(f"BTC 200-Haftalık MA: ${ma:,.0f}  (şu an %{abs(dist):.1f} {pos})")
        else:
            lines.append(f"BTC 200-Haftalık MA: ${ma:,.0f}")

    if "fib" in macro:
        fib = macro["fib"]
        ath, low = fib["ath"], fib["cycle_low"]
        lines.append(f"BTC Fibonacci (ATH ${ath:,.0f} → Dip ${low:,.0f}):")
        for key, label in [("0.236","23.6%"),("0.382","38.2%"),("0.500","50.0%"),
                            ("0.618","61.8% [altın]"),("0.786","78.6%")]:
            val    = fib[key]
            marker = ""
            if btc_price:
                diff = (btc_price - val) / val * 100
                if abs(diff) < 3:
                    marker = " ◀ YAKINDA"
                elif diff < 0:
                    marker = " (fiyat altında)"
            lines.append(f"  Fib {label}: ${val:,.0f}{marker}")

    if "weekly_sr" in macro and btc_price:
        sr   = macro["weekly_sr"]
        sups = [p for p in sr if p <= btc_price * 1.005][-2:]
        ress = [p for p in sr if p >  btc_price * 0.995][:2]
        if sups or ress:
            lines.append("Haftalık S/R:")
            for s in sups:
                lines.append(f"  Destek: ${s:,.0f}  (-%{(btc_price-s)/btc_price*100:.1f})")
            for rv in ress:
                lines.append(f"  Direnç: ${rv:,.0f}  (+%{(rv-btc_price)/btc_price*100:.1f})")

    if "fbb" in macro and btc_price:
        fbb = macro["fbb"]
        p = btc_price
        if p < fbb["lower_4236"]:
            fbb_zone = f"🟢 AŞIRI UCUZ — 4.236 bandı altı (${fbb['lower_4236']:,.0f})"
        elif p < fbb["lower_2618"]:
            fbb_zone = f"🟡 UCUZ BÖLGE — 2.618 bandı altı (${fbb['lower_2618']:,.0f})"
        elif p < fbb["lower_1618"]:
            fbb_zone = f"⚪ ORTA-UCUZ — 1.618 bandı altı (${fbb['lower_1618']:,.0f})"
        elif p > fbb["upper_2618"]:
            fbb_zone = f"🔴 AŞIRI PAHALI — 2.618 bandı üstü (${fbb['upper_2618']:,.0f})"
        elif p > fbb["upper_1618"]:
            fbb_zone = f"🟠 PAHALI — 1.618 bandı üstü (${fbb['upper_1618']:,.0f})"
        else:
            fbb_zone = f"⚪ NÖTR — SMA: ${fbb['sma']:,.0f}"
        lines.append(f"FBB Haftalık (Rashad): {fbb_zone}")

    if "ssl" in macro:
        ssl = macro["ssl"]
        em = {"bullish": "🟢", "bearish": "🔴", "neutral": "⚪"}.get(ssl["signal"], "⚪")
        ssl_line = f"SSL Hybrid Haftalık: {em} {ssl['signal'].upper()}"
        if ssl["cross"]:
            ssl_line += f"  ⚠️ {ssl['cross']}"
        lines.append(ssl_line)

    return "\n".join(lines)

# ============================================================
# TMA OVERLAY — 3 GÜNLÜK BTC (ArtyFXC)
# ============================================================
def _tma_3d_btc() -> dict | None:
    """TMA Overlay on BTC 3d — beyaz (hızlı) kırmızıyı (yavaş) kesince dip sinyali."""
    data = _fetch_klines("BTC/USDT", "3d", 60)
    if not data or len(data["closes"]) < 25:
        return None
    c = data["closes"]

    def _tma(arr, p):
        half = p // 2 + 1
        if len(arr) < half * 2:
            return None
        sma1 = [sum(arr[i - half:i]) / half for i in range(half, len(arr) + 1)]
        if len(sma1) < half:
            return None
        return sum(sma1[-half:]) / half

    fast_p, slow_p = 14, 21
    fn  = _tma(c,       fast_p)
    fp  = _tma(c[:-1],  fast_p)
    sn  = _tma(c,       slow_p)
    sp  = _tma(c[:-1],  slow_p)

    if None in (fn, fp, sn, sp):
        return None

    trend = "beyaz kırmızı altında — düşüş baskısı" if fn < sn else "beyaz kırmızı üstünde — yükseliş"
    cross = None
    if fp >= sp and fn < sn:
        cross = "AŞAĞI KESİŞİM — dip bölgesi sinyali"
    elif fp <= sp and fn > sn:
        cross = "YUKARI KESİŞİM — dönüş başlıyor"

    return {"trend": trend, "cross": cross,
            "fast": round(fn, 0), "slow": round(sn, 0), "price": round(c[-1], 0)}


# ============================================================
# LİKİDİTE SWEEP TESPİTİ
# ============================================================
def _liquidity_sweep(symbol: str) -> list:
    """
    Son 48 saatte alt wick tespiti — potansiyel likidite temizliği.
    Kriter: alt wick gövdeden 1.5x büyük VE son destek altına inmiş.
    """
    data = _fetch_klines(symbol, "1h", 72)
    if not data or len(data["closes"]) < 20:
        return []
    opens  = data["opens"]
    closes = data["closes"]
    highs  = data["highs"]
    lows   = data["lows"]

    if len(lows) < 20:
        return []
    recent_support = min(lows[-48:-3]) if len(lows) >= 48 else min(lows[:-3])

    sweeps = []
    check_range = range(max(-24, -len(closes) + 1), -1)
    for i in check_range:
        body       = abs(closes[i] - opens[i])
        lower_wick = min(opens[i], closes[i]) - lows[i]
        if lower_wick > max(body * 1.5, closes[i] * 0.003) and lows[i] < recent_support:
            sweeps.append({
                "hours_ago": abs(i),
                "low":       round(lows[i], 6),
                "close":     round(closes[i], 6),
                "wick_pct":  round(lower_wick / closes[i] * 100, 2),
            })
    return sweeps[-3:]


# ============================================================
# PORTFOLIO BAĞLAMI
# ============================================================
_SIG_TYPE_MAP = {
    "capit":     "panik_pump",
    "t72":       "pump_orta",
    "t168":      "pump_uzun",
    "pump_prob": "pump_probability",
}

def _portfolio_context(symbol: str, sig_type: str) -> tuple[str, str]:
    if not PORTFOLIO_URL:
        return "", ""
    try:
        headers = {"Authorization": f"Bearer {PORTFOLIO_TOKEN}"} if PORTFOLIO_TOKEN else {}
        r = requests.get(f"{PORTFOLIO_URL}/api/signals",
                         params={"limit": 500}, headers=headers, timeout=5)
        if r.status_code != 200:
            return "", ""
        signals = r.json()
        if not isinstance(signals, list):
            signals = signals.get("signals", signals.get("data", []))
        closed = [s for s in signals if s.get("status") in ("win", "loss", "expired")]

        pt = _SIG_TYPE_MAP.get(sig_type, sig_type)
        cs = [s for s in closed if s.get("symbol","").upper() == symbol.upper()
              and s.get("sig_type","") == pt]
        if cs:
            wins = [s for s in cs if s.get("status") == "win"]
            wr   = round(len(wins) / len(cs) * 100)
            coin_hist = f"{len(cs)} geçmiş sinyal → {len(wins)} WIN | WR %{wr}"
        else:
            coin_hist = "Bu coin için henüz geçmiş veri yok."

        non_smc = [s for s in closed if "smc" not in s.get("sig_type","").lower()]
        if non_smc[:30]:
            w30  = sum(1 for s in non_smc[:30] if s.get("status") == "win")
            wr30 = round(w30 / 30 * 100)
            sys_hist = f"Son 30 sinyal (SMC hariç): {w30} WIN / {30-w30} LOSS | WR %{wr30}"
        else:
            sys_hist = "Yeterli sistem verisi yok."

        return coin_hist, sys_hist
    except Exception as e:
        print(f"[ANALYZER PORTFOLIO] {e}", flush=True)
        return "", ""

# ============================================================
# CLAUDE DEĞERLENDİRMESİ
# ============================================================
_TYPE_NAMES = {
    "capit":     "PANİK PUMP — kapitülasyon mean reversion | Stop -3% | TP +5/10/15% | WR ~%84",
    "t72":       "ORTA VADE T72 — 3 gün hedef | Stop -5% | TP +10% | WR %54",
    "t168":      "UZUN VADE T168 — 7 gün hedef | Stop -8% | TP +25% | WR %40",
    "pump_prob": "PUMP PROBABILITY — kırılım | Stop ~-5% | TP +8/15/25%",
    "smc":       "SMC — CHoCH yapısal kırılım | Discount Zone + Micro CHoCH",
}
_SOURCE_NAMES = {
    "bot": "Pump Scanner Bot",
    "smc": "SMC Sistemi",
}
_TYPE_SHORT = {
    "capit":     "PANİK PUMP",
    "t72":       "ORTA VADE",
    "t168":      "UZUN VADE",
    "pump_prob": "PUMP PROB",
    "smc":       "SMC CHoCH",
}

def _fmt(p):
    if p is None: return "?"
    p = float(p)
    if p >= 100:    return f"{p:.2f}"
    if p >= 1:      return f"{p:.3f}"
    if p >= 0.01:   return f"{p:.4f}"
    if p >= 0.0001: return f"{p:.6f}"
    return f"{p:.8f}"

def _build_sig_data(signal: dict) -> str:
    sig_type = signal.get("type", "")
    if sig_type == "capit":
        return (f"Düşüş: {signal.get('ret1',0):+.2f}% | "
                f"Hacim: {signal.get('vol_ratio',0):.2f}x | "
                f"ATR: %{signal.get('atr_pct',0):.2f} | "
                f"Funding: {signal.get('funding','?')}")
    if sig_type == "t72":
        return (f"Mom5: +%{signal.get('mom5_pct',0):.2f} | "
                f"EMA21 uzaklık: %{signal.get('dist_ema21',0):.2f} | "
                f"Drawdown: %{signal.get('coin_drawdown',0):.2f} | "
                f"MA200 eğim: +%{signal.get('ma200_slope',0):.3f}")
    if sig_type == "t168":
        return (f"MA200 uzaklık: +%{signal.get('dist_ma200',0):.2f} | "
                f"Mom10: +%{signal.get('mom10_pct',0):.2f} | "
                f"Son zirve: {signal.get('days_since_high',0)} bar")
    if sig_type == "pump_prob":
        return (f"BB genişlik: {signal.get('bb_width',0):.4f} | "
                f"ADX: {signal.get('adx',0):.1f} | "
                f"DI+: {signal.get('di_plus',0):.1f} / DI-: {signal.get('di_minus',0):.1f} | "
                f"Direnç: {_fmt(signal.get('resistance'))} | "
                f"Hacim: {signal.get('vol_ratio',0):.2f}x | "
                f"Güç: {signal.get('strength','?')}")
    # Bilinmeyen tip — tüm alanları yaz
    skip = {"symbol","type","entry","stop","tp1","tp2","tp3","source","_internal"}
    return " | ".join(f"{k}:{v}" for k, v in signal.items() if k not in skip)

def evaluate(signal: dict, recent_count: int = 0) -> str:
    """
    Sinyali Claude ile değerlendirir, karar metni döndürür.
    recent_count: son 1 saatte kaç sinyal geldi (clustering bağlamı için)
    """
    if not ANTHROPIC_API_KEY:
        return ""

    import anthropic

    symbol   = signal.get("symbol", "")
    sig_type = signal.get("type", "unknown")
    source   = signal.get("source", "bot")

    # Tüm verileri paralel çek
    with ThreadPoolExecutor(max_workers=6) as ex:
        fut_tf    = ex.submit(_fetch_all_tf, symbol)
        fut_fg    = ex.submit(_fear_greed)
        fut_dom   = ex.submit(_dominance)
        fut_macro = ex.submit(_fetch_btc_macro)
        fut_tma   = ex.submit(_tma_3d_btc)
        fut_sweep = ex.submit(_liquidity_sweep, symbol)
    tf_data          = fut_tf.result()
    fg_val, fg_label = fut_fg.result()
    dom              = fut_dom.result()
    macro            = fut_macro.result()
    tma              = fut_tma.result()
    sweep            = fut_sweep.result()
    coin_hist, sys_hist = _portfolio_context(symbol, sig_type)

    coin_block = "\n".join([
        _tf_line("1S",  tf_data.get("coin_1h")),
        _tf_line("4S",  tf_data.get("coin_4h")),
        _tf_line("1G",  tf_data.get("coin_1d")),
    ])
    btc_block = "\n".join([
        _tf_line("1S",  tf_data.get("btc_1h")),
        _tf_line("4S",  tf_data.get("btc_4h")),
    ])
    btc_price = (tf_data.get("btc_4h") or {}).get("close") or (tf_data.get("btc_1h") or {}).get("close")
    macro_block = _btc_macro_str(macro, btc_price)

    fg_str = f"{fg_val} ({fg_label})" if fg_val is not None else "bilinmiyor"

    if recent_count >= 3:
        cluster_str = f"⚠️ Son 1 saatte {recent_count} coin sinyal verdi — piyasa geneli baskı!"
    elif recent_count >= 2:
        cluster_str = f"🟡 Son 1 saatte {recent_count} sinyal — dikkatli ol."
    else:
        cluster_str = "Normal (tek sinyal)"

    tma_str = ""
    if tma:
        tma_str = f"\nBTC 3G TMA: {tma['trend']}"
        if tma["cross"]:
            tma_str += f"  ⚠️ {tma['cross']}"

    if sweep:
        sweep_str = "\n[LİKİDİTE SWEEP — Son 24S]\n" + "\n".join(
            f"  {s['hours_ago']}s önce: ${s['low']:,.4f} altına iğne (%{s['wick_pct']:.1f} wick) → kapanış ${s['close']:,.4f}"
            for s in sweep
        )
    else:
        sweep_str = "\n[LİKİDİTE SWEEP — Son 24S]\nBelirgin sweep yok."

    prompt = f"""Sen deneyimli bir kripto risk analistisisin. Ham verileri kendin yorumla.

[SİNYAL]
Kaynak: {_SOURCE_NAMES.get(source, source)}
Coin: #{symbol.replace('/USDT','')} | {_TYPE_NAMES.get(sig_type, sig_type)}
Giriş: {_fmt(signal.get('entry'))} | Stop: {_fmt(signal.get('stop'))} | TP1: {_fmt(signal.get('tp1'))}
{_build_sig_data(signal)}

[KOİN — ÇOKLU ZAMAN DİLİMİ]
{coin_block}

[BTC — ÇOKLU ZAMAN DİLİMİ]
{btc_block}

[BTC MAKRO — UZUN VADE]
{macro_block if macro_block else "veri yok"}{tma_str}

[MARKET]
Fear & Greed: {fg_str}
{_dom_str(dom)}
Sinyal clustering: {cluster_str}

[BU COİN GEÇMİŞİ]
{coin_hist}

[SİSTEM GENEL PERFORMANS — SMC HARİCİ]
{sys_hist}

{sweep_str}

RSI, EMA, hacim, FBB bölgesi, SSL yönü, TMA kesişimi, likidite sweep bağlamını birlikte değerlendir.
Yakın döneme takılma — haftalık ve 3 günlük yapıya önce bak, sonra anlık sinyali değerlendir.
Geçmiş istatistikler sadece bağlamdır — anlık koşullar esastır.

KARAR: [✅ GİR / ⚠️ DİKKAT / 🚫 RİSKLİ]
GEREKÇE: (2-3 cümle — somut veri referansı ver)
UYARI: (varsa 1 cümle, yoksa yazma)"""

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        resp   = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=350,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text.strip()
    except Exception as e:
        print(f"[ANALYZER CLAUDE] {e}", flush=True)
        return ""

# ============================================================
# ANA GİRİŞ NOKTASI
# ============================================================
def _extract_verdict(decision: str) -> str:
    if "✅" in decision: return "✅ GİR"
    if "⚠️" in decision: return "⚠️ DİKKAT"
    if "🚫" in decision: return "🚫 RİSKLİ"
    return ""

def _update_portfolio_analyzer(portfolio_id: str, verdict: str):
    if not portfolio_id or not PORTFOLIO_URL or not verdict:
        return
    try:
        headers = {"Content-Type": "application/json"}
        if PORTFOLIO_TOKEN:
            headers["Authorization"] = f"Bearer {PORTFOLIO_TOKEN}"
        safe_id = portfolio_id.replace("/", "_")
        r = requests.patch(
            f"{PORTFOLIO_URL}/api/signal/{safe_id}/analyzer",
            json={"analyzer_decision": verdict},
            headers=headers, timeout=5,
        )
        if r.status_code != 200:
            print(f"[ANALYZER] Portfolio güncelleme başarısız: {r.status_code} {r.text[:80]}", flush=True)
    except Exception as e:
        print(f"[ANALYZER] Portfolio güncelleme hatası: {e}", flush=True)

def process_and_send(signal: dict, recent_count: int = 0, sig_num: int = 0, portfolio_id: str = ""):
    """
    Sinyali değerlendir ve kararı Analyzer Telegram botuna gönder.

    Herhangi bir sistemden çağrılabilir:
        from claude_analyzer import process_and_send
        process_and_send(signal_dict, recent_count=1, sig_num=42, portfolio_id="SYM_123")

    signal dict zorunlu alanlar: symbol, type, entry, stop, tp1
    Opsiyonel: source ("bot" veya "smc"), tp2, tp3, sistem-spesifik metrikler
    """
    decision = evaluate(signal, recent_count)
    if not decision:
        return

    symbol   = signal.get("symbol", "")
    sig_type = signal.get("type", "")
    source   = signal.get("source", "bot")
    tr_time  = _tr_now()

    source_icon = {"bot": "🤖", "smc": "🔶"}.get(source, "🤖")
    type_short  = _TYPE_SHORT.get(sig_type, sig_type.upper())

    tp2_str = f"  TP2: {_fmt(signal.get('tp2'))}" if signal.get("tp2") else ""
    tp3_str = f"  TP3: {_fmt(signal.get('tp3'))}" if signal.get("tp3") else ""

    num_str = f" #{sig_num}" if sig_num else ""
    src_str = _SOURCE_NAMES.get(source, source)

    msg = (
        f"{source_icon} <b>ANALİZ — #{symbol.replace('/USDT','')} [{type_short}]{num_str}</b>\n"
        f"🕐 {tr_time.strftime('%d/%m/%Y %H:%M')}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💵 Giriş: {_fmt(signal.get('entry'))}  "
        f"🛡️ Stop: {_fmt(signal.get('stop'))}  "
        f"🎯 TP1: {_fmt(signal.get('tp1'))}{tp2_str}{tp3_str}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{decision}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>{src_str} · Analyzer{num_str}</i>"
    )

    send_decision(msg)
    verdict = _extract_verdict(decision)
    _update_portfolio_analyzer(portfolio_id, verdict)
    print(f"[ANALYZER] #{symbol} kararı gönderildi ({source}){f' → {verdict}' if verdict else ''}", flush=True)


# ============================================================
# PERİYODİK PİYASA İZLEME
# ============================================================
_watcher_state: dict = {
    "last_daily": None,  # date nesnesi — günlük rapor için
    "last_4h_ts": 0,     # son 4h kontrolünün unix timestamp'i
    "last_fg":    None,  # son gönderilen F&G değeri
    "last_dom":   None,  # son gönderilen dominans %
    "last_btc":   None,  # son gönderilen BTC fiyatı
}

def _market_report_text(report_type: str, fg_val, fg_label, dom, macro, btc_4h, tma=None) -> str:
    if not ANTHROPIC_API_KEY:
        return ""
    import anthropic

    btc_price  = (btc_4h or {}).get("close")
    btc_rsi    = (btc_4h or {}).get("rsi")
    btc_ema50  = (btc_4h or {}).get("ema50")
    btc_ema200 = (btc_4h or {}).get("ema200")
    fg_str     = f"{fg_val} ({fg_label})" if fg_val is not None else "bilinmiyor"
    macro_str  = _btc_macro_str(macro, btc_price) if macro else "veri yok"

    tma_str = ""
    if tma:
        tma_str = f"\nBTC 3G TMA: {tma['trend']}"
        if tma["cross"]:
            tma_str += f"  ⚠️ {tma['cross']}"

    if report_type == "daily":
        gorev = ("Günlük kapanış özeti yaz. Haftalık yapıya önce bak (FBB zonu, SSL yönü), "
                 "sonra 3 günlük TMA durumunu değerlendir, sonra anlık koşulları yorumla. "
                 "BTC'nin genel durumunu ve bu hafta için beklentiyi anlat. "
                 "Sade, anlaşılır Türkçe kullan — teknik jargon yok, markdown başlık yok. "
                 "Makro Konum ve Bu Hafta Beklentisi olmak üzere 2 kısa paragraf. Her paragraf 2-3 cümle. "
                 "Cümleleri mutlaka tamamla, yarıda bırakma.")
    else:
        gorev = ("Piyasada önemli bir değişim tespit edildi. "
                 "Ne değişti, ne anlama geliyor, nelere dikkat edilmeli? "
                 "Sade Türkçe, 2-3 cümle, teknik jargon kullanma.")

    prompt = f"""Sen deneyimli bir kripto piyasa analistisisin.

[BTC — 4 SAATLİK]
Fiyat: {btc_price} | RSI: {btc_rsi} | EMA50: {btc_ema50} | EMA200: {btc_ema200}

[BTC MAKRO — UZUN VADE]
{macro_str}{tma_str}

[MARKET]
Fear & Greed: {fg_str}
{_dom_str(dom)}

GÖREV: {gorev}"""

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        resp   = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=750,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text.strip()
    except Exception as e:
        print(f"[WATCHER CLAUDE] {e}", flush=True)
        return ""

def _should_alert(fg_val, dom, btc_price) -> str | None:
    """Anlamlı değişim varsa açıklama döndürür, yoksa None."""
    st      = _watcher_state
    reasons = []

    if fg_val is not None and st["last_fg"] is not None:
        if abs(fg_val - st["last_fg"]) >= 10:
            reasons.append(f"F&G {st['last_fg']}→{fg_val}")

    if dom and st["last_dom"] is not None:
        cur_dom = dom.get("current", 0)
        if abs(cur_dom - st["last_dom"]) >= 1.5:
            reasons.append(f"Dominans %{st['last_dom']:.1f}→%{cur_dom:.1f}")

    if btc_price and st["last_btc"]:
        chg = (btc_price - st["last_btc"]) / st["last_btc"] * 100
        if abs(chg) >= 5:
            reasons.append(f"BTC %{chg:+.1f} ({st['last_btc']:,.0f}→{btc_price:,.0f})")

    return ", ".join(reasons) if reasons else None

def _run_market_check(report_type: str):
    try:
        with ThreadPoolExecutor(max_workers=4) as ex:
            fut_fg    = ex.submit(_fear_greed)
            fut_dom   = ex.submit(_dominance)
            fut_macro = ex.submit(_fetch_btc_macro)
            fut_tma   = ex.submit(_tma_3d_btc)
        fg_val, fg_label = fut_fg.result()
        dom              = fut_dom.result()
        macro            = fut_macro.result()
        tma              = fut_tma.result()
        btc_4h           = _tf_summary("BTC/USDT", "4h", 100)
        btc_price        = (btc_4h or {}).get("close")

        st = _watcher_state

        if report_type == "4h":
            change = _should_alert(fg_val, dom, btc_price)
            if not change:
                if fg_val    is not None: st["last_fg"]  = fg_val
                if dom:                   st["last_dom"] = dom.get("current")
                if btc_price:             st["last_btc"] = btc_price
                return
            change_label = f"⚡ Değişim: {change}"
        else:
            change_label = "📅 Günlük Özet"

        text = _market_report_text(report_type, fg_val, fg_label, dom, macro, btc_4h, tma)
        if not text:
            fg_str = f"{fg_val} ({fg_label})" if fg_val is not None else "—"
            dom_cur = dom.get("current", "—") if dom else "—"
            text = (f"BTC: {btc_price or '—'} | F&G: {fg_str} | Dominans: {dom_cur}%\n"
                    f"<i>(Claude API yanıt vermedi — ham veri)</i>")
            print(f"[WATCHER] Claude API boş döndü, ham veriyle gönderiliyor", flush=True)

        tr_time = _tr_now()
        title   = "📊 <b>GÜNLÜK PİYASA RAPORU</b>" if report_type == "daily" else "⚡ <b>PİYASA UYARISI</b>"
        msg = (
            f"{title}\n"
            f"🕐 {tr_time.strftime('%d/%m/%Y %H:%M')}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"{change_label}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"{text}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"<i>Claude Analyzer · Piyasa İzleme</i>"
        )
        thread = None if report_type == "daily" else 38
        send_decision(msg, thread_id=thread)
        print(f"[WATCHER] {report_type} raporu gönderildi (thread {thread})", flush=True)

        if fg_val    is not None: st["last_fg"]  = fg_val
        if dom:                   st["last_dom"] = dom.get("current")
        if btc_price:             st["last_btc"] = btc_price

    except Exception as e:
        print(f"[WATCHER CHECK] {e}", flush=True)

def _market_watcher_loop():
    print("[WATCHER] Başlatıldı — 4h değişim kontrolü + 03:00 günlük rapor.", flush=True)
    while True:
        try:
            now_tr = _tr_now()
            now_ts = time.time()
            st     = _watcher_state

            # Günlük rapor: her gün 06:00-06:04 TR arası
            if now_tr.hour == 6 and now_tr.minute < 5:
                today = now_tr.date()
                if st["last_daily"] != today:
                    st["last_daily"] = today
                    _run_market_check("daily")

            # 4 saatlik değişim kontrolü
            if now_ts - st["last_4h_ts"] >= 14400:
                st["last_4h_ts"] = now_ts
                _run_market_check("4h")

        except Exception as e:
            print(f"[WATCHER LOOP] {e}", flush=True)

        time.sleep(60)

def start_market_watcher():
    """bot.py başlangıcında çağrılır."""
    if not ANALYZER_TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[WATCHER] ANALYZER_TELEGRAM_TOKEN veya CHAT_ID eksik.", flush=True)
        return
    if not ANTHROPIC_API_KEY:
        print("[WATCHER] ANTHROPIC_API_KEY eksik — Claude analizi devre dışı.", flush=True)
        return
    t = threading.Thread(target=_market_watcher_loop, daemon=True, name="market-watcher")
    t.start()
