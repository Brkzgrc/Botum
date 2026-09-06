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
import json
import html
import threading
import time
import re
import requests
import numpy as np
import pandas as pd
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

# ============================================================
# AYARLAR
# ============================================================
ANTHROPIC_API_KEY       = os.getenv("ANTHROPIC_API_KEY",       "")
GEMINI_API_KEY          = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY", "")
ANALYZER_TELEGRAM_TOKEN = os.getenv("ANALYZER_TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID        = os.getenv("ANALYZER_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID", "")
MANUAL_ANALYZER_MODE    = os.getenv("MANUAL_ANALYZER_MODE", "v2").strip().lower()
_MANUAL_ANALYZER_V2_MODEL_RAW = (
    os.getenv("MANUAL_ANALYZER_V2_MODEL")
    or os.getenv("GEMINI_MODEL")
    or "gemini-3.5-flash-lite"
).strip()
MANUAL_ANALYZER_V2_MODEL = {
    "gemini-2.5-flash-lite": "gemini-3.5-flash-lite",
    "gemini-2.5-flash-lite-preview": "gemini-3.5-flash-lite",
    "gemini-2.5-flash-lite-preview-09-2025": "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite-preview": "gemini-3.1-flash-lite",
}.get(_MANUAL_ANALYZER_V2_MODEL_RAW, _MANUAL_ANALYZER_V2_MODEL_RAW)
MANUAL_ANALYZER_V2_RENDERER = os.getenv(
    "MANUAL_ANALYZER_V2_RENDERER", "controlled"
).strip().lower()
MANUAL_ANALYZER_ALLOW_PAID_HAIKU = os.getenv(
    "MANUAL_ANALYZER_ALLOW_PAID_HAIKU", "false"
).strip().lower() == "true"

from api_logger import log_usage as _log_usage
_PROMPT_V_SIGNAL  = "1.1"   # sinyal değerlendirme prompt versiyonu
_PROMPT_V_WATCHER = "1.0"   # market watcher prompt versiyonu
_PROMPT_V_MANUAL  = "4.5"   # doğal yükseliş anlatımı ve genel araç etiketi temizliği
_PROMPT_V_MANUAL_V2 = "1.3"  # Flash-Lite: fiyat alanı ve geri çekilme mesafesi karşılaştırması
_PROMPT_V_MANUAL_V2_PLAN = "2.1"  # Flash-Lite karar planı; Türkçe metni kod kurar
# PORTFOLIO_URL bot.py servisinde tanımlı; bu modül portfolio-tracker
# servisinin İÇİNDE çalıştığı için kendine PATCH/GET atarken Render'ın
# her servise otomatik verdiği RENDER_EXTERNAL_URL'e düşer.
PORTFOLIO_URL           = os.getenv("PORTFOLIO_URL") or os.getenv("RENDER_EXTERNAL_URL", "")
# PORTFOLIO_TOKEN: diğer servisler (SMC.py, bot.py, trading-bot) bu isimle
# tanımlıyor. Ama bu modül portfolio-tracker'ın KENDİ sürecinde çalışıp kendi
# API'sine (Bearer ile korunan /api/signals) istek attığı için, o serviste
# muhtemelen sadece PORTFOLIO_AUTH_TOKEN tanımlı (login sistemi için zorunlu) —
# PORTFOLIO_TOKEN'ı ayrıca tanımlamayı kimse düşünmez, çünkü normalde bir
# servisin kendine bearer token göndermesi gerekmez. Boşsa PORTFOLIO_AUTH_TOKEN'a
# düş — aksi halde bu self-call sessizce 401/login sayfasına düşüp
# "[ANALYZER PORTFOLIO] Expecting value" hatasıyla geçmiş bağlamını kaybediyordu.
PORTFOLIO_TOKEN         = os.getenv("PORTFOLIO_TOKEN") or os.getenv("PORTFOLIO_AUTH_TOKEN", "")

TR_TZ = timezone(timedelta(hours=3))
ARCHIVE_FILE = os.path.join(os.getenv("DATA_DIR", "/tmp"), "learning_archive.json")
_archive_lock = threading.Lock()

def _tr_now():
    return datetime.now(timezone.utc).astimezone(TR_TZ)

# ============================================================
# ÖĞRENEN ARŞİV — Piyasa koşulu → sonuç eşleştirmesi
# ============================================================
def _archive_load() -> list:
    """Lock almadan okur — caller lock almalı."""
    try:
        if os.path.exists(ARCHIVE_FILE):
            with open(ARCHIVE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return []

def _archive_save(entries: list):
    """Lock almadan yazar — caller lock almalı."""
    try:
        with open(ARCHIVE_FILE, "w", encoding="utf-8") as f:
            json.dump(entries, f, ensure_ascii=False, default=str)
    except Exception as e:
        print(f"[ARCHIVE] Kayıt hatası: {e}", flush=True)

def _archive_add_entry(portfolio_id: str, signal: dict, conditions: dict, decision: str):
    """Yeni sinyal değerlendirmesi arşive eklenir."""
    entry_p = float(signal.get("entry") or 0)
    stop_p  = float(signal.get("stop")  or 0)
    tp1_p   = float(signal.get("tp1")   or 0)
    risk_pct = round((stop_p - entry_p) / entry_p * 100, 2) if entry_p > 0 else None
    rr_ratio = None
    if entry_p > 0 and stop_p > 0 and tp1_p > 0:
        risk   = abs(entry_p - stop_p)
        reward = abs(tp1_p   - entry_p)
        if risk > 0:
            rr_ratio = round(reward / risk, 2)
    rec = {
        "id":              portfolio_id or f"{signal.get('symbol','?')}_{int(time.time())}",
        "ts":              datetime.now(timezone.utc).isoformat(),
        "symbol":          signal.get("symbol", ""),
        "sig_type":        signal.get("type", ""),
        "source":          signal.get("source", "bot"),
        "risk_pct":        risk_pct,
        "rr_ratio":        rr_ratio,
        "fg":              conditions.get("fg"),
        "fg_label":        conditions.get("fg_label"),
        "btc_4h_rsi":      conditions.get("btc_4h_rsi"),
        "btc_above_ema50": conditions.get("btc_above_ema50"),
        "dominance":       conditions.get("dominance"),
        "dom_trend":       conditions.get("dom_trend"),
        "tma_trend":       conditions.get("tma_trend"),
        "decision":        decision,
        "outcome":         None,
        "close_reason":    None,
        "close_pct":       None,
        "peak_pct":        None,
        "duration_h":      None,
    }
    with _archive_lock:
        entries = _archive_load()
        entries.append(rec)
        _archive_save(entries)
    print(f"[ARCHIVE] Eklendi: {rec['id']} ({decision})", flush=True)

def update_archive_outcome(portfolio_id: str, close_reason: str, close_pct: float,
                           peak_pct: float, open_time: str):
    """Sinyal kapandığında sonucu arşive yazar — portfolio_tracker.py'den çağrılır."""
    if not portfolio_id:
        return
    with _archive_lock:
        entries = _archive_load()
        for e in entries:
            if e.get("id") == portfolio_id and e.get("outcome") is None:
                e["close_reason"] = close_reason
                e["close_pct"]    = close_pct
                e["peak_pct"]     = peak_pct
                try:
                    open_dt = datetime.fromisoformat(open_time)
                    if open_dt.tzinfo is None:
                        open_dt = open_dt.replace(tzinfo=timezone.utc)
                    e["duration_h"] = round(
                        (datetime.now(timezone.utc) - open_dt).total_seconds() / 3600, 1)
                except Exception:
                    pass
                if close_reason in ("expired", "expired_after_tp1"):
                    e["outcome"] = "expired"
                elif (close_pct or 0) > 0:
                    e["outcome"] = "win"
                else:
                    e["outcome"] = "loss"
                _archive_save(entries)
                print(f"[ARCHIVE] Güncellendi: {portfolio_id} → {e['outcome']} ({close_pct:+.2f}%)", flush=True)
                return

def _archive_condition_context(conditions: dict) -> str:
    """Arşivden benzer piyasa koşullarındaki geçmiş sonuçları özetler."""
    try:
        with _archive_lock:
            entries = _archive_load()
        completed = [e for e in entries if e.get("outcome") in ("win", "loss")]
        if len(completed) < 3:
            total = len([e for e in entries if e.get("outcome") is not None])
            return f"Koşul arşivi: {total} tamamlanmış kayıt — henüz yeterli veri yok."

        fg        = conditions.get("fg")
        btc_above = conditions.get("btc_above_ema50")
        dom       = conditions.get("dominance")

        similar = []
        for e in completed:
            score = 0
            if fg is not None and e.get("fg") is not None and abs(fg - e["fg"]) <= 15:
                score += 1
            if (btc_above is not None and e.get("btc_above_ema50") is not None
                    and btc_above == e["btc_above_ema50"]):
                score += 1
            if dom is not None and e.get("dominance") is not None and abs(dom - e["dominance"]) <= 3:
                score += 1
            if score >= 2:
                similar.append(e)

        if len(similar) < 3:
            wins_all = sum(1 for e in completed if e.get("outcome") == "win")
            wr_all   = round(wins_all / len(completed) * 100)
            return (f"Koşul arşivi: {len(completed)} kayıt (genel WR %{wr_all}) — "
                    f"bu koşullara benzer yeterli örnek yok ({len(similar)} adet).")

        wins    = sum(1 for e in similar if e.get("outcome") == "win")
        wr      = round(wins / len(similar) * 100)
        avg_pct = round(sum(e.get("close_pct") or 0 for e in similar) / len(similar), 2)
        fg_label  = conditions.get("fg_label", "?")
        dom_trend = conditions.get("dom_trend", "?")
        dom_str   = f"{dom:.1f}%" if dom is not None else "?"
        above_str = "üstünde" if btc_above else "altında" if btc_above is not None else "?"
        return (f"Benzer koşullar (F&G ~{fg if fg is not None else '?'} [{fg_label}], "
                f"BTC EMA50 {above_str}, dominans {dom_str} [{dom_trend}]): "
                f"{len(similar)} örnek → {wins} WIN | WR %{wr} | Ort. {avg_pct:+.2f}%")
    except Exception as _e:
        print(f"[ARCHIVE CTX] {_e}", flush=True)
        return ""

# ============================================================
# TELEGRAM
# ============================================================
def send_decision(text: str, thread_id: int | None = 38):
    token = ANALYZER_TELEGRAM_TOKEN
    if not token or not TELEGRAM_CHAT_ID:
        print("[ANALYZER] Token veya chat_id eksik, mesaj gönderilemedi.", flush=True)
        return
    chunks = [text[i:i+4096] for i in range(0, len(text), 4096)]
    try:
        for chunk in chunks:
            payload = {"chat_id": TELEGRAM_CHAT_ID, "text": chunk,
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
            "close_times": [int(k[6]) for k in raw],
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


def _direction(values, lookback=3, epsilon=0.0):
    """Son değerlerin seviyesinden çok hareket yönünü sade biçimde anlatır."""
    clean = [float(v) for v in values if v is not None and np.isfinite(v)]
    if len(clean) < lookback + 1:
        return "veri_yetersiz"
    delta = clean[-1] - clean[-1 - lookback]
    if delta > epsilon:
        return "yükseliyor"
    if delta < -epsilon:
        return "düşüyor"
    return "yatay"


def _manual_tf_snapshot(data: dict | None) -> dict | None:
    """Manuel analiz için seviye değil yön ağırlıklı teknik özet üretir."""
    if not data or len(data.get("closes", [])) < 60:
        return None
    c = pd.Series(data["closes"], dtype=float)
    h = pd.Series(data["highs"], dtype=float)
    l = pd.Series(data["lows"], dtype=float)
    v = pd.Series(data["volumes"], dtype=float)

    ema = {n: c.ewm(span=n, adjust=False).mean() for n in (20, 50, 100, 200)}
    delta = c.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    rsi = 100 - 100 / (1 + gain / loss.replace(0, np.nan))
    fast = c.ewm(span=12, adjust=False).mean()
    slow = c.ewm(span=26, adjust=False).mean()
    macd = fast - slow
    macd_signal = macd.ewm(span=9, adjust=False).mean()
    macd_hist = macd - macd_signal

    rsi_low = rsi.rolling(14).min()
    rsi_high = rsi.rolling(14).max()
    stoch_rsi = (rsi - rsi_low) / (rsi_high - rsi_low).replace(0, np.nan) * 100
    stoch_k = stoch_rsi.rolling(3).mean()
    stoch_d = stoch_k.rolling(3).mean()
    obv = (np.sign(c.diff()).fillna(0) * v).cumsum()
    hh = h.rolling(14).max()
    ll = l.rolling(14).min()
    willr = -100 * (hh - c) / (hh - ll).replace(0, np.nan)

    prev_close = c.shift(1)
    tr = pd.concat([(h - l), (h - prev_close).abs(), (l - prev_close).abs()], axis=1).max(axis=1)
    atr = tr.rolling(14).mean()
    price = float(c.iloc[-1])
    atr_now = float(atr.iloc[-1]) if pd.notna(atr.iloc[-1]) else 0.0
    ema20 = float(ema[20].iloc[-1])
    recent_closes = [round(float(x), 8) for x in c.tail(4)]
    recent_highs = [round(float(x), 8) for x in h.tail(4)]
    recent_lows = [round(float(x), 8) for x in l.tail(4)]
    last_open = float(data["opens"][-1])
    last_high = float(data["highs"][-1])
    last_low = float(data["lows"][-1])
    last_range = max(last_high - last_low, 1e-12)
    def _shape(values) -> str:
        vals = [float(x) for x in values[-4:]]
        rises = sum(vals[i] > vals[i - 1] for i in range(1, len(vals)))
        falls = sum(vals[i] < vals[i - 1] for i in range(1, len(vals)))
        if rises >= 3: return "düzenli yükseliyor"
        if falls >= 3: return "düzenli düşüyor"
        if rises > falls: return "genel olarak yükseliyor"
        if falls > rises: return "genel olarak düşüyor"
        return "karışık/yatay"
    return {
        "price": price,
        "rsi": round(float(rsi.iloc[-1]), 1) if pd.notna(rsi.iloc[-1]) else None,
        "rsi_direction": _direction(rsi.tolist(), epsilon=0.4),
        "macd_position": "pozitif" if macd.iloc[-1] >= 0 else "negatif",
        "macd_hist_direction": _direction(macd_hist.tolist()),
        "macd_cross": "üstünde" if macd.iloc[-1] >= macd_signal.iloc[-1] else "altında",
        "stoch_rsi": round(float(stoch_k.iloc[-1]), 1) if pd.notna(stoch_k.iloc[-1]) else None,
        "stoch_signal": round(float(stoch_d.iloc[-1]), 1) if pd.notna(stoch_d.iloc[-1]) else None,
        "stoch_direction": _direction(stoch_k.tolist(), epsilon=1.0),
        "stoch_cross": "üstünde" if stoch_k.iloc[-1] >= stoch_d.iloc[-1] else "altında",
        "obv_direction": _direction(obv.tolist()),
        "willr": round(float(willr.iloc[-1]), 1) if pd.notna(willr.iloc[-1]) else None,
        "willr_direction": _direction(willr.tolist(), epsilon=1.0),
        "ema20_relation": "üstünde" if price >= ema20 else "altında",
        "ema20_direction": _direction(ema[20].tolist()),
        "ema_order": "20>50>100>200" if all(ema[a].iloc[-1] > ema[b].iloc[-1]
                                                   for a, b in ((20, 50), (50, 100), (100, 200)))
                     else "karışık",
        "ema20_distance_atr": round((price - ema20) / atr_now, 2) if atr_now > 0 else None,
        "atr": atr_now,
        "vol_ratio": _vol_ratio(v.tolist()),
        # Özellikle 15M yorumunda modelin tek bir kalıba bağlı kalmadan fiyat
        # davranışını okuyabilmesi için son mumların ham yapısını da taşı.
        "recent_closes": recent_closes,
        "recent_highs": recent_highs,
        "recent_lows": recent_lows,
        "last_return_pct": round((price / last_open - 1) * 100, 2) if last_open else None,
        "last_body_range_ratio": round(abs(price - last_open) / last_range, 2),
        "last_close_location": round((price - last_low) / last_range, 2),
        "last_candle_closed": bool(data.get("close_times") and
                                   data["close_times"][-1] < int(time.time() * 1000)),
        "recent_close_shape": _shape(c.tail(4).tolist()),
        "recent_high_shape": _shape(h.tail(4).tolist()),
        "recent_low_shape": _shape(l.tail(4).tolist()),
    }


def _cluster_price_zones(points: list[dict], price: float, atr_1h: float) -> list[dict]:
    """Çoklu zaman dilimi pivotlarını yakınlıklarına göre gerçek fiyat bölgelerine toplar."""
    if not points or price <= 0:
        return []
    tolerance = max(price * 0.0035, atr_1h * 0.35 if atr_1h else 0)
    clusters = []
    for point in sorted(points, key=lambda x: x["price"]):
        matched = next((z for z in clusters if abs(point["price"] - z["center"]) <= tolerance), None)
        if matched:
            matched["prices"].append(point["price"])
            matched["weights"] += point["weight"]
            matched["tfs"].add(point["tf"])
            matched["center"] = float(np.average(matched["prices"]))
        else:
            clusters.append({"center": point["price"], "prices": [point["price"]],
                             "weights": point["weight"], "tfs": {point["tf"]}})
    for zone in clusters:
        pad = max(tolerance * 0.35, (max(zone["prices"]) - min(zone["prices"])) / 2)
        zone["low"] = min(zone["prices"]) - pad
        zone["high"] = max(zone["prices"]) + pad
    return clusters


def _manual_zones(frames: dict, price: float) -> dict:
    points = []
    weights = {"1H": 1, "4H": 2, "1D": 3}
    for label, data in frames.items():
        if not data or len(data.get("closes", [])) < 20:
            continue
        highs, lows = data["highs"], data["lows"]
        window = 3 if label == "1H" else 2
        start = max(window, len(highs) - (180 if label == "1H" else 120))
        for i in range(start, len(highs) - window):
            if lows[i] <= min(lows[i-window:i+window+1]):
                points.append({"price": float(lows[i]), "tf": label, "weight": weights[label]})
            if highs[i] >= max(highs[i-window:i+window+1]):
                points.append({"price": float(highs[i]), "tf": label, "weight": weights[label]})
    atr_1h = (_manual_tf_snapshot(frames.get("1H")) or {}).get("atr", 0)
    zones = _cluster_price_zones(points, price, atr_1h)
    supports = sorted([z for z in zones if z["center"] < price],
                      key=lambda z: (price - z["center"]))
    resistances = sorted([z for z in zones if z["center"] > price],
                         key=lambda z: (z["center"] - price))
    strong_supports = sorted(supports, key=lambda z: (-z["weights"], price - z["center"]))
    next_support = None
    if len(supports) > 1:
        next_support = next(
            (z for z in supports[1:] if z["high"] < supports[0]["low"]),
            supports[1],
        )
    structural = strong_supports[0] if strong_supports else None
    return {
        "near_support": supports[0] if supports else None,
        "next_support": next_support,
        "structural_support": structural,
        "resistance_1": resistances[0] if resistances else None,
        "resistance_2": resistances[1] if len(resistances) > 1 else None,
    }

def _adx_calc(highs, lows, closes, period=14):
    """Standart Wilder ADX (0-100 aralığı) — backtest: ADX>=40+drop<=-8% → WR %92."""
    if len(closes) < period * 3:
        return None
    h = np.array(highs, dtype=float)
    l = np.array(lows,  dtype=float)
    c = np.array(closes, dtype=float)
    n = len(c)
    tr   = np.zeros(n); dm_p = np.zeros(n); dm_m = np.zeros(n)
    for i in range(1, n):
        tr[i]   = max(h[i] - l[i], abs(h[i] - c[i-1]), abs(l[i] - c[i-1]))
        up, dn  = h[i] - h[i-1], l[i-1] - l[i]
        dm_p[i] = up if (up > dn and up > 0) else 0.0
        dm_m[i] = dn if (dn > up and dn > 0) else 0.0
    # Wilder sum-smoothing for TR / DM
    atr = np.zeros(n); smp = np.zeros(n); smm = np.zeros(n)
    atr[period] = tr[1:period+1].sum()
    smp[period] = dm_p[1:period+1].sum()
    smm[period] = dm_m[1:period+1].sum()
    for i in range(period + 1, n):
        atr[i] = atr[i-1] - atr[i-1] / period + tr[i]
        smp[i] = smp[i-1] - smp[i-1] / period + dm_p[i]
        smm[i] = smm[i-1] - smm[i-1] / period + dm_m[i]
    with np.errstate(divide="ignore", invalid="ignore"):
        dip = np.where(atr > 0, smp / atr * 100, 0.0)
        dim = np.where(atr > 0, smm / atr * 100, 0.0)
        dx  = np.where((dip + dim) > 0, np.abs(dip - dim) / (dip + dim) * 100, 0.0)
    # Wilder average-smoothing for ADX (gives 0-100 range)
    adx = np.zeros(n)
    adx[2 * period - 1] = dx[period: 2 * period].mean()
    for i in range(2 * period, n):
        adx[i] = (adx[i-1] * (period - 1) + dx[i]) / period
    return round(float(adx[-1]), 1)

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
        "adx":       _adx_calc(data["highs"], data["lows"], c),
    }

def _tf_line(label: str, d: dict | None) -> str:
    if not d:
        return f"  {label}: —"
    parts = []
    if d.get("rsi")       is not None: parts.append(f"RSI {_fmt_ind(d['rsi'])}")
    if d.get("ema50")     is not None: parts.append(f"EMA50 {_fmt_ind(d['ema50'])}")
    if d.get("ema200")    is not None: parts.append(f"EMA200 {_fmt_ind(d['ema200'])}")
    if d.get("vol_ratio") is not None: parts.append(f"Hacim {_fmt_ind(d['vol_ratio'])}x")
    if d.get("adx")       is not None: parts.append(f"ADX {_fmt_ind(d['adx'])}")
    return f"  {label}: Fiyat {_fmt_ind(d['close'])} | {' | '.join(parts)}"

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

_etf_cache: dict = {"data": None, "ts": 0}

def _etf_flow() -> str | None:
    """Bitbo'dan günlük BTC ETF net akışını çeker. Önbellek: 1 saat."""
    now = time.time()
    if _etf_cache["data"] is not None and now - _etf_cache["ts"] < 3600:
        return _etf_cache["data"]
    try:
        import re as _re
        hdrs = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "text/html,application/xhtml+xml",
        }
        r = requests.get("https://bitbo.io/treasuries/etf-flows/", headers=hdrs, timeout=15)
        if not r.ok:
            return _etf_cache["data"]
        rows = _re.findall(r'<tr[^>]*>(.*?)</tr>', r.text, _re.DOTALL)
        flows = []
        for row in rows:
            cells = _re.findall(r'<td[^>]*>(.*?)</td>', row, _re.DOTALL)
            if len(cells) < 3:
                continue
            raw = _re.sub(r'<[^>]+>', '', cells[-1]).strip().replace(',', '').replace('\xa0', '')
            raw = raw.replace('(', '-').replace(')', '')
            try:
                flows.append(round(float(raw), 1))
            except (ValueError, TypeError):
                pass
        if len(flows) >= 5:
            today = flows[-1]
            avg5  = round(sum(flows[-5:]) / 5, 1)
            sign  = "+" if today >= 0 else ""
            trend = "pozitif" if avg5 > 0 else ("negatif" if avg5 < 0 else "nötr")
            result = f"Bugün: {sign}{today}M$ | 5G Ort: {'+' if avg5>=0 else ''}{avg5}M$ | Trend: {trend}"
            _etf_cache["data"] = result
            _etf_cache["ts"]   = now
            return result
    except Exception as e:
        print(f"[ANALYZER ETF] {e}", flush=True)
    return _etf_cache["data"]


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
                if abs(diff) > 20:
                    continue  # Mevcut fiyattan %20'den uzak — irrelevant
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
        _CLOSED_ST = {"win_tp1", "win_tp2", "win_trail", "win_partial",
                      "half_stopped", "half_expired", "loss", "expired", "closed"}
        _WIN_ST    = {"win_tp1", "win_tp2", "win_trail", "win_partial"}
        closed = [s for s in signals if s.get("status") in _CLOSED_ST]

        def _is_win(s):
            st = s.get("status", "")
            if st == "closed":
                # trading bot aktifken /api/position-closed webhook'undan gelen
                # güncel kapanış şeması — status hep "closed", sonuç outcome/close_pct'te
                if s.get("outcome"):
                    return s["outcome"] == "win"
                return bool((s.get("close_pct") or 0) > 0)
            return st in _WIN_ST or (st == "half_stopped" and (s.get("close_pct") or 0) > 0)

        pt = _SIG_TYPE_MAP.get(sig_type, sig_type)
        cs = [s for s in closed if s.get("symbol","").upper() == symbol.upper()
              and s.get("sig_type","") == pt]
        if cs:
            wins = [s for s in cs if _is_win(s)]
            wr   = round(len(wins) / len(cs) * 100)
            coin_hist = f"{len(cs)} geçmiş sinyal → {len(wins)} WIN | WR %{wr}"
        else:
            coin_hist = "Bu coin için henüz geçmiş veri yok."

        non_smc = [s for s in closed if "smc" not in s.get("source","").lower()]
        if non_smc[:30]:
            sample = non_smc[:30]
            w30  = sum(1 for s in sample if _is_win(s))
            n    = len(sample)
            wr30 = round(w30 / n * 100)
            sys_hist = f"Son {n} sinyal (SMC hariç): {w30} WIN / {n-w30} LOSS/EXP | WR %{wr30}"
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
    "rocket":    "ROCKET — momentum devam + hacim artışı | Stop -5% | TP +8/15/25%",
    "smc":       "SMC — CHoCH yapısal kırılım | Discount Zone + Micro CHoCH",
    "pump":      "PUMP — 15m spike ≥15x + 1h hacim ≥5x + 4h ROC ≥24% | Stop -%7 | TP +%20 | 6h expire | WR ~%91",
    "choch_v2":  "SMC CHoCH ROC — yapısal kırılım + ROC momentum | TP hit → %2.5 trailing çıkış",
}
_SOURCE_NAMES = {
    "bot":   "Pump Scanner Bot",
    "smc":   "SMC Sistemi",
    "smc-v2": "SMC CHoCH ROC Sistemi",
}
_TYPE_SHORT = {
    "capit":    "PANİK PUMP",
    "t72":      "ORTA VADE",
    "t168":     "UZUN VADE",
    "smc":      "SMC CHoCH",
    "pump":     "PUMP",
    "choch_v2": "SMC CHoCH ROC",
}

def _fmt(p):
    if p is None: return "?"
    p = float(p)
    if p >= 100:    return f"{p:.2f}"
    if p >= 1:      return f"{p:.3f}"
    if p >= 0.01:   return f"{p:.4f}"
    if p >= 0.0001: return f"{p:.6f}"
    return f"{p:.8f}"

def _fmt_ind(v) -> str:
    """İndikatör değerlerini LLM prompt'u için formatlar — gereksiz hassasiyet yok."""
    if v is None: return "?"
    v = float(v)
    if v >= 10000: return f"{v:,.0f}"
    if v >= 100:   return f"{v:.1f}"
    if v >= 1:     return f"{v:.2f}"
    if v >= 0.01:  return f"{v:.4f}"
    return         f"{v:.6f}"

def _manual_zone_text(zone: dict | None, current_price: float = 0.0) -> str:
    if not zone:
        return "veriyle güvenilir bölge oluşmadı"
    tf_names = {"1H": "1H", "4H": "4H", "1D": "1G", "1W": "1Hft"}
    tfs = " + ".join(tf_names.get(tf, tf) for tf in sorted(zone["tfs"]))
    text = f"{_fmt(zone['low'])}–{_fmt(zone['high'])} ({tfs})"
    if current_price:
        price = float(current_price)
        low, high = float(zone["low"]), float(zone["high"])
        if price > high:
            text += f" — güncel fiyatın %{(price - high) / price * 100:.1f} altında"
        elif price < low:
            text += f" — güncel fiyatın %{(low - price) / price * 100:.1f} üstünde"
        else:
            text += " — fiyat bölgenin içinde"
    return text


def _clean_manual_analysis(text: str) -> str:
    """Model talimata rağmen Markdown/İngilizce kalıntısı üretirse Telegram öncesi temizle."""
    cleaned = (text or "").replace("\\*", "").replace("**", "").replace("__", "")
    # Haiku bazen tool çağrısındaki XML benzeri etiketleri alan metnine taşıyor.
    cleaned = re.sub(r"</?[A-Za-z][^>]*>", "", cleaned)
    cleaned = cleaned.replace(
        "OBV yükseliş patern yükselme katılımını destekliyor",
        "OBV'nin yükselmesi alıcı katılımının sürdüğünü gösteriyor",
    )
    cleaned = cleaned.replace("bounceback", "yukarı tepki").replace("bounce back", "yukarı tepki")
    cleaned = cleaned.replace("MACD histogram ufuklaşması", "MACD histogramının yataylaşması")
    cleaned = cleaned.replace("cari fiyat", "mevcut fiyat").replace("Cari fiyat", "Mevcut fiyat")
    cleaned = cleaned.replace("mikro ortam", "kısa vadeli görünüm")
    cleaned = cleaned.replace("scenario", "senaryo").replace("beklemeği", "beklemek")
    cleaned = cleaned.replace("stabilize etmesi", "yeniden güçlenmesi")
    cleaned = cleaned.replace("stabil hale dönmesi", "yönünü yeniden yukarı çevirmesi")
    cleaned = cleaned.replace("pullback", "geri çekilme").replace("Pullback", "Geri çekilme")
    cleaned = cleaned.replace("geri çekilme (geri çekilme)", "geri çekilme")
    cleaned = cleaned.replace("yükseliş yapıyor", "yükseliyor")
    cleaned = cleaned.replace("belirgin bir sınama (yeniden test)", "belirgin bir yeniden test")
    cleaned = cleaned.replace("Tam bir geri çekilme riski bulunmamakta", "Geri çekilme riski tamamen ortadan kalkmış değil")
    cleaned = cleaned.replace("rüzgar arkasına karşı olsa da", "kısa vadeli piyasa desteği zayıf olsa da")
    cleaned = cleaned.replace("rüzgâr arkasına karşı olsa da", "kısa vadeli piyasa desteği zayıf olsa da")
    cleaned = cleaned.replace("bağlamsal yükseliş temaı", "yükseliş görünümü")
    cleaned = cleaned.replace("yükseliş temaı", "yükseliş görünümü")
    cleaned = cleaned.replace("mekanik bir geri çekilme", "kısa vadeli bir geri çekilme")
    cleaned = cleaned.replace("mekanik satın almaktan", "alım yapmaktan")
    cleaned = cleaned.replace("henüz kurtarıcı", "olumlu görünümü destekliyor")
    cleaned = cleaned.replace("MACD pozitif kalanı", "MACD'nin pozitif kalması")
    cleaned = cleaned.replace("dip diplerle", "diplerle")
    cleaned = cleaned.replace("ticaret katılımı", "alıcı katılımı")
    cleaned = cleaned.replace("dinamik destek", "yakın destek")
    cleaned = cleaned.replace("limited", "sınırlı").replace("Limited", "Sınırlı")
    cleaned = cleaned.replace("konsolidasyon", "yatay dinlenme")
    cleaned = cleaned.replace("genelge takip", "genel piyasayı takip")
    cleaned = cleaned.replace("ivmen", "ivmeyi")
    cleaned = cleaned.replace("daha samimi olabilirdim", "daha güvenli biçimde değerlendirebilirdim")
    cleaned = cleaned.replace("hareketli ortalama dezeni", "hareketli ortalama düzeni")
    cleaned = cleaned.replace("alım basısı", "alım baskısı")
    cleaned = cleaned.replace("dinlenmea", "dinlenmeye")
    cleaned = cleaned.replace("saatlik'de", "saatlik grafikte").replace("saatlik’de", "saatlik grafikte")
    cleaned = cleaned.replace("katı destek", "destek")
    cleaned = cleaned.replace("yükseliş patern yükselme katılımını", "alıcı katılımının sürdüğünü")
    cleaned = cleaned.replace("patern", "yapı")
    cleaned = cleaned.replace("küçük katılım", "küçük bir alım")
    cleaned = cleaned.replace("destek sağlamaktadır", "olumlu görünümü destekliyor")
    cleaned = cleaned.replace(
        "destek dizilimi sağlam",
        "yükselişi destekleyen sıralama korunuyor",
    )
    cleaned = cleaned.replace(
        "bu ortam yavaşlamadan haberi verebilir",
        "bu görünüm kısa vadeli bir yavaşlamanın habercisi olabilir",
    )
    cleaned = cleaned.replace("seviyelerin tümünde", "grafiklerin tümünde")
    cleaned = cleaned.replace("yatay veya hafif zayıflama yapıyor", "yatay seyrediyor veya hafif zayıflıyor")
    cleaned = cleaned.replace("çizginin sinyalin üstünde", "MACD çizgisinin sinyal çizgisinin üzerinde")
    cleaned = cleaned.replace("çizgi sinyal üstünde", "MACD çizgisi sinyal çizgisinin üzerinde")
    cleaned = cleaned.replace(
        "geri çekilme baskısının ılımlı düzeyde kalmadığını gösteriyor",
        "satış baskısının zayıfladığını gösteriyor",
    )
    cleaned = cleaned.replace("müdahalenin gücü", "kısa vadeli alıcı gücü")
    cleaned = cleaned.replace("dinlenme izlerdim", "kısa bir dinlenme oluşup oluşmadığını izlerdim")
    cleaned = cleaned.replace("güçlü yükseliş gösteriyorum", "güçlü yükseliş gösteriyor")
    cleaned = re.sub(
        r"\b([A-Z0-9]{2,15}) tüm EMA dizi hiyerarşisi sağlam kalmıştır",
        lambda match: f"{match.group(1)} için hareketli ortalamaların yükselişi destekleyen sıralaması korunuyor",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = cleaned.replace("hareketlin kırılgan", "hareketin kırılgan")
    cleaned = cleaned.replace("bu dörtte biri uyumlu olarak", "bu dört gösterge uyumlu biçimde")
    cleaned = cleaned.replace("artan yapıdayken", "yükselirken")
    cleaned = cleaned.replace("zayıfladığını işaret ediyor", "zayıflamaya işaret ediyor")
    cleaned = cleaned.replace("hareketli bir yatay dinlenme", "dalgalı bir yatay seyir")
    cleaned = cleaned.replace("kontrol kaybı riski", "daha sert geri çekilme riski")
    cleaned = cleaned.replace("hareket kalitesi belirsizleşmiş", "kısa vadeli yönü belirsizleşmiş")
    cleaned = cleaned.replace(
        "saatlik ve 4 saatlik grafiklerde düşüş trendinde RSI yükselişe karşılık satış baskısı güçlenirken",
        "saatlik ve 4 saatlik grafiklerde RSI yükselse de satış baskısı güçlenirken",
    )
    cleaned = cleaned.replace(
        "bu iki yapının çelişkisi dalgalı bir yatay seyir veya daha sert geri çekilme riski taşıyor",
        "para akışı ile satış baskısının ters yönde ilerlemesi kısa vadede dalgalı bir seyir veya daha sert bir geri çekilme riski yaratıyor",
    )
    cleaned = cleaned.replace("yön net yukarı yönlü gözüküyor", "yön net biçimde yukarı görünüyor")
    cleaned = cleaned.replace("ölçeklerde", "grafiklerde")
    cleaned = cleaned.replace("EMA20'nin üzerinde pozisyon alıyor", "fiyat EMA20'nin üzerinde kalıyor")
    cleaned = re.sub(r"\bema\b", "EMA", cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.replace("hızlı çizginin yavaş çizginin üstünde", "hızlı çizgisinin yavaş çizgisinin üzerinde")
    cleaned = cleaned.replace(
        "günlük StochRSI yön yukarı ve hızlı üstünde",
        "günlük StochRSI yukarı yönlü ve hızlı çizgisi yavaş çizgisinin üzerinde",
    )
    cleaned = cleaned.replace("retest", "yeniden test").replace("Retest", "Yeniden test")
    cleaned = cleaned.replace("overbought", "aşırı alımda").replace("oversold", "aşırı satımda")
    cleaned = cleaned.replace("ciddiyetle aşırı alımda durumda", "belirgin biçimde aşırı alımda")
    cleaned = cleaned.replace("rüzgâr arkası kesintiye uğratabilir", "yükselişi destekleyen ortam zayıflayabilir")
    cleaned = cleaned.replace("pauzasyon", "kısa süreli duraklama").replace("Pauzasyon", "Kısa süreli duraklama")
    cleaned = cleaned.replace("büyük tablo", "4 saatlik ve günlük görünüm").replace("Büyük tablo", "4 saatlik ve günlük görünüm")
    cleaned = cleaned.replace("aşırı satın alım", "hızlı yükseliş").replace("aşırı satım", "hızlı düşüş")
    cleaned = cleaned.replace("fırlatma senaryosu", "geri çekilme ihtimali")
    cleaned = cleaned.replace("kapalı kapanışlar", "son mum hareketleri")
    cleaned = cleaned.replace("trenditli", "trend yönündeki").replace("ATT", "fiyat birimi")
    cleaned = cleaned.replace("yakın destek bölgesine test etmesi", "yakın destek bölgesini test etmesi")
    cleaned = cleaned.replace("yakın desteğe test etmesi", "yakın desteği test etmesi")
    cleaned = cleaned.replace("acil senaryosu değil", "kısa vadeli giriş bölgesi değil")
    # Model bazen talep edilmediği halde başa ikinci bir başlık koyuyor.
    if "Ne oluyor?" in cleaned:
        cleaned = "Ne oluyor?" + cleaned.split("Ne oluyor?", 1)[1]
    return cleaned.strip()


def _natural_manual_text(text: str) -> str:
    """Yapılandırılmış manuel analiz alanlarını kullanıcıya doğal Türkçeyle hazırlar."""
    cleaned = _clean_manual_analysis(str(text or ""))
    cleaned = re.sub(r"\b1H\b", "1 saatlik", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\b4H\b", "4 saatlik", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\b1D\b", "1 günlük", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\b15M\b", "15 dakikalık", cleaned, flags=re.IGNORECASE)
    # Analiz cümlelerinde doğal kullanım; bölge etiketleri kod tarafından ayrıca üretilir.
    cleaned = re.sub(r"\b1 saatlik\b", "saatlik", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\b1 günlük\b", "günlük", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bon beş dakikalık\b", "15 dakikalık", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bon beş dakika\b", "15 dakika", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bbullish\b", "yükseliş yönlü", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bbearish\b", "düşüş yönlü", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\blong\b", "spot alım", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bconfirmation\b", "teyit", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bswing low(?:'ların)?\b", "önceki belirgin diplerin", cleaned, flags=re.IGNORECASE)
    return cleaned.strip(" \n•-")


def _manual_action_items(value) -> list[str]:
    """Haiku liste yerine metin döndürse bile eylem planını kaybetmeden maddelere ayırır."""
    if isinstance(value, (list, tuple)):
        candidates = list(value)
    elif isinstance(value, dict):
        candidates = list(value.values())
    elif isinstance(value, str):
        # Önce satır/madde ayrımını kullan. Model tek paragraf döndürdüyse
        # tamamlanmış cümleleri ayrı eylem maddelerine dönüştür.
        candidates = [part for part in re.split(r"\r?\n+", value) if part.strip()]
        if len(candidates) == 1:
            candidates = [part for part in re.split(r"(?<=[.!?])\s+", value) if part.strip()]
    else:
        candidates = []

    actions = []
    for item in candidates:
        item = re.sub(r"^\s*(?:[-*•]+|\d+[.)])\s*", "", str(item))
        cleaned_item = _natural_manual_text(item)
        if cleaned_item:
            actions.append(cleaned_item)
    return actions[:7]


def _manual_sentence_limit(text: str, limit: int) -> str:
    """Bir alanın model talimatını aşarak uzamasını engeller."""
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", text or "") if p.strip()]
    return " ".join(parts[:limit]).strip()


def _manual_has_language_corruption(text: str) -> bool:
    """Anlamı belirsizleşmiş veya dilbilgisi bozulmuş model metnini yakalar."""
    lowered = (text or "").lower()
    broken_phrases = (
        "saturasyon", "tepe yaşıyor", "çift zaman dilimi", "satın alma güçünün",
        "çizgiler kapanmış", "çizgiler açılmış", "durdurma göster", "mevcut seviyedeki stabilite",
        "konuma alış", "çift alıcı katılımı", "momentum harita", "kapanış bulması",
        "fiyat mevcut seviyeden alım riski", "hareket tutarlı olup uzamış",
        "son mum yapısında sınırlı gövde", "genel yükseliş koşulunun",
        "görmeli", "kalıcı olmama ihtimali", "tekrar azalması olası",
        "çift hızlı-yavaş çizgi", "çift üstünde", "momentum sınırlandırması",
        "göstergelerin güçlü konumu", "devamının devam etmekte",
    )
    return any(phrase in lowered for phrase in broken_phrases)


def _manual_drop_corrupt_sentences(text: str) -> str:
    """Bozuk tek bir cümle yüzünden kullanılabilir cümleleri kaybetmez."""
    return " ".join(
        sentence.strip()
        for sentence in re.split(r"(?<=[.!?])\s+", text or "")
        if sentence.strip() and not _manual_has_language_corruption(sentence)
    )


def _manual_remove_15m_leak(text: str) -> str:
    """Yalnız 15 dakikalık blokta bulunan mum ayrıntılarının ana alanlara sızmasını engeller."""
    timing_only = (
        "son dört", "son mum", "mum gövde", "gövde sınırlı", "sınırlı gövde", "kapanış alt taraf",
        "kapanış üst taraf", "halen açık", "henüz kapanmamış", "15 dakika",
    )
    kept = []
    for sentence in re.split(r"(?<=[.!?])\s+", text or ""):
        lowered = sentence.lower()
        if any(key in lowered for key in timing_only):
            continue
        if sentence.strip():
            kept.append(sentence.strip())
    return " ".join(kept)


def _manual_remove_unsafe_current_buy(text: str, zones: dict, current_price: float) -> str:
    """Uzak desteğe rağmen gerekçesiz 'mevcut fiyattan al' cümlesini rapora bırakmaz."""
    zone = zones.get("near_support")
    if not zone or not current_price or float(current_price) <= float(zone["high"]):
        return text
    gap = (float(current_price) - float(zone["high"])) / float(current_price) * 100
    if gap < 5:
        return text
    kept = []
    for sentence in re.split(r"(?<=[.!?])\s+", text or ""):
        lowered = sentence.lower()
        current_buy = ("mevcut fiyat" in lowered and
                       any(word in lowered for word in ("alım", "pozisyon", "lot")))
        risk_explained = any(word in lowered for word in ("yüksek risk", "agresif", "uzamış", "ema"))
        if current_buy and not risk_explained:
            continue
        if sentence.strip():
            kept.append(sentence.strip())
    return " ".join(kept)


def _manual_enforce_single_stance(text: str) -> str:
    """İlk tavır beklemekse sonraki cümlede mevcut fiyattan alım önermesine izin vermez."""
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text or "") if s.strip()]
    if not sentences:
        return ""
    first = sentences[0].lower()
    waiting = any(phrase in first for phrase in
                  ("beklerdim", "alım düşünmezdim", "girmezdim", "uzak dururdum"))
    if not waiting:
        return " ".join(sentences)

    kept = [sentences[0]]
    for sentence in sentences[1:]:
        lowered = sentence.lower()
        current_entry = (any(phrase in lowered for phrase in
                             ("mevcut fiyattan", "şu anki fiyattan", "hemen")) and
                         any(word in lowered for word in ("alım", "pozisyon", "başlangıç", "giriş")))
        if current_entry:
            continue
        kept.append(sentence)
    return " ".join(kept)


def _manual_align_expectation_with_timing(text: str, timing_snapshot: dict | None) -> str:
    """15 dakikalık görünüm zayıfken mevcut fiyattan alım öneren cümleyi rapordan çıkarır."""
    if not timing_snapshot:
        return text
    directions = [
        timing_snapshot.get("rsi_direction", ""), timing_snapshot.get("macd_hist_direction", ""),
        timing_snapshot.get("stoch_direction", ""), timing_snapshot.get("obv_direction", ""),
        timing_snapshot.get("willr_direction", ""), timing_snapshot.get("ema20_direction", ""),
    ]
    upward = sum(any(word in str(value).lower() for word in ("yüks", "yukarı", "güçlen"))
                 for value in directions)
    downward = sum(any(word in str(value).lower() for word in ("düş", "aşağı", "zayıf"))
                   for value in directions)
    if downward < upward + 2:
        return text

    kept = []
    for sentence in re.split(r"(?<=[.!?])\s+", text or ""):
        lowered = sentence.lower()
        current_entry = (any(phrase in lowered for phrase in
                             ("mevcut fiyattan", "şu anki fiyattan", "hemen")) and
                         any(word in lowered for word in ("alım", "pozisyon", "başlangıç", "giriş")))
        if current_entry:
            continue
        if sentence.strip():
            kept.append(sentence.strip())
    return " ".join(kept)


def _manual_price_location_note(zones: dict, current_price: float) -> str:
    """En yakın hesaplanan desteğin gerçekten ne kadar yakın olduğunu modele açıklar."""
    zone = zones.get("near_support")
    if not zone or not current_price:
        return "Güncel fiyatın en yakın desteğe uzaklığı güvenilir biçimde hesaplanamadı."
    price = float(current_price)
    low, high = float(zone["low"]), float(zone["high"])
    if low <= price <= high:
        return "Güncel fiyat en yakın destek bölgesinin içinde."
    if price > high:
        distance = (price - high) / price * 100
        note = f"En yakın hesaplanan destek güncel fiyatın %{distance:.1f} altında."
        if distance >= 5:
            note += (" Bu bölgenin adı 'yakın destek' olsa da mevcut fiyata yakın bir giriş alanı değildir; "
                     "fiyatın hareketli ortalamalardan uzaklığını ve hareketin ne kadar uzadığını ayrıca tart.")
        return note
    distance = (low - price) / price * 100
    return f"En yakın hesaplanan destek güncel fiyatın %{distance:.1f} üstünde; fiyat destek bölgesinin altında."


def _manual_price_position_summary(zones: dict, current_price: float) -> str:
    """Genel değerlendirmede fiyatın bölgelere göre konumunu deterministik olarak gösterir."""
    zone = zones.get("near_support")
    if not zone or not current_price:
        return "Fiyatın hesaplanan desteklere göre konumu güvenilir biçimde belirlenemedi."
    price = float(current_price)
    low, high = float(zone["low"]), float(zone["high"])
    if low <= price <= high:
        return "Fiyat şu anda en yakın hesaplanan destek bölgesinin içinde."
    if price > high:
        distance = (price - high) / price * 100
        if distance >= 5:
            return (f"En yakın hesaplanan destek güncel fiyatın %{distance:.1f} altında; fiyat desteklerden "
                    "belirgin biçimde uzaklaşmış durumda.")
        return f"En yakın hesaplanan destek güncel fiyatın %{distance:.1f} altında."
    distance = (low - price) / price * 100
    return (f"En yakın hesaplanan destek güncel fiyatın %{distance:.1f} üstünde; fiyat bu bölgenin altında "
            "işlem görüyor.")


def _manual_timing_fallback(snap: dict | None, hourly_snap: dict | None = None) -> str:
    """Modelin 15 dakikalık metni kullanılamazsa gerçek snapshot'tan tutarlı bir özet üretir."""
    if not snap:
        return "15 dakikalık Binance verisi alınamadığı için giriş zamanlaması ayrıca değerlendirilemedi."

    directions = [
        snap.get("rsi_direction", ""), snap.get("macd_hist_direction", ""),
        snap.get("stoch_direction", ""), snap.get("obv_direction", ""),
        snap.get("willr_direction", ""), snap.get("ema20_direction", ""),
    ]
    upward = sum(any(word in str(value).lower() for word in ("yüks", "yukarı", "güçlen"))
                 for value in directions)
    downward = sum(any(word in str(value).lower() for word in ("düş", "aşağı", "zayıf"))
                   for value in directions)

    hourly_directions = [] if not hourly_snap else [
        hourly_snap.get("rsi_direction", ""), hourly_snap.get("macd_hist_direction", ""),
        hourly_snap.get("stoch_direction", ""), hourly_snap.get("obv_direction", ""),
        hourly_snap.get("willr_direction", ""), hourly_snap.get("ema20_direction", ""),
    ]
    hourly_up = sum(any(word in str(value).lower() for word in ("yüks", "yukarı", "güçlen"))
                    for value in hourly_directions)
    hourly_down = sum(any(word in str(value).lower() for word in ("düş", "aşağı", "zayıf"))
                      for value in hourly_directions)

    if upward >= downward + 2:
        if hourly_up >= hourly_down + 2:
            conclusion = ("Bu kısa vadeli hareket saatlik olumlu görünümle aynı yönde güçleniyor; bu, giriş "
                          "zamanlamasını destekliyor ancak fiyatın bulunduğu bölgeyi ve hacmi yine dikkate alırdım.")
        else:
            conclusion = ("Kısa vadeli hareket yeniden yukarı güçleniyor; saatlik görünüm aynı yönde güçlenmeden "
                          "bunu tek başına alım gerekçesi saymazdım.")
    elif downward >= upward + 2:
        conclusion = ("Kısa vadeli hareket zayıflıyor; yeni alım düşünmeden önce satış baskısının durmasını ve "
                      "göstergelerin yeniden yukarı dönmesini beklerdim.")
    else:
        conclusion = ("Kısa vadeli göstergeler aynı yönde değil; bu nedenle giriş zamanlamasının netleşmesi için "
                      "fiyat hareketi ile alıcı ilgisinin birlikte güçlenmesini beklerdim.")

    candle = (
        f"15 dakikalık grafikte son dört kapanış {snap.get('recent_close_shape', 'karışık')}; "
        f"dipler {snap.get('recent_low_shape', 'karışık')}, tepeler "
        f"{snap.get('recent_high_shape', 'karışık')} görünüyor."
    )
    return f"{candle} {conclusion}"


def _manual_expectation_fallback(zones: dict, current_price: float,
                                 timing_snapshot: dict | None,
                                 hourly_snapshot: dict | None) -> str:
    """Modelin beklentisi kullanılamazsa fiyat konumu ve 15 dakikalık gözlemle doğal bir plan üretir."""
    zone = zones.get("near_support")
    if zone and current_price and float(current_price) > float(zone["high"]):
        gap = (float(current_price) - float(zone["high"])) / float(current_price) * 100
        if gap >= 5:
            opening = (f"Fiyat en yakın hesaplanan desteğin %{gap:.1f} üzerinde olduğu için mevcut seviyeden "
                       "aceleyle alım yapmazdım. Yükseliş devam etse bile yeni alım düşünmeden önce fiyatın kısa "
                       "süreli dinlenmesini veya kontrollü biçimde geri çekilmesini beklerdim.")
        else:
            opening = ("Fiyat en yakın desteğe çok uzak olmadığı için alıcıların bu bölgeyi koruyup korumadığını "
                       "izler, güçlenme görülürse küçük ve kademeli bir alımı değerlendirirdim.")
    elif zone and current_price and float(zone["low"]) <= float(current_price) <= float(zone["high"]):
        opening = ("Fiyat destek bölgesinin içinde olduğu için hemen karar vermez, satış baskısının durduğunu ve "
                   "alıcıların yeniden güçlendiğini görürsem küçük bir alımı değerlendirirdim.")
    else:
        opening = "Mevcut fiyat konumu net bir giriş avantajı göstermediği için acele karar vermezdim."
    return f"{opening} {_manual_timing_fallback(timing_snapshot, hourly_snapshot)}"


def _manual_remove_invented_levels(text: str, zones: dict, current_price: float) -> str:
    """Hesaplanan bölgeler ve güncel fiyat dışında uydurulan fiyatlı cümleleri çıkarır."""
    allowed = [float(current_price)] if current_price else []
    for zone in zones.values():
        if zone:
            allowed.extend([float(zone["low"]), float(zone["high"])])

    def is_allowed(number: float) -> bool:
        return any(abs(number - value) <= max(abs(value) * 0.002, 1.0) for value in allowed)

    kept = []
    for sentence in re.split(r"(?<=[.!?])\s+", text or ""):
        # EMA20 gibi gösterge adlarındaki rakamları fiyat seviyesi sayma.
        scan_text = re.sub(r"\b(?:1|4|15)\s+(?:saatlik|günlük|dakikalık|dakika)\b", "", sentence)
        numbers = [float(x.replace(",", ".")) for x in
                   re.findall(r"(?<![A-Za-zÇĞİÖŞÜçğıöşü])\b\d+(?:[.,]\d+)?\b", scan_text)]
        if numbers and any(not is_allowed(number) for number in numbers):
            continue
        if sentence.strip():
            kept.append(sentence.strip())
    return " ".join(kept)


def _manual_technical_fallbacks(coin_snapshots: dict | None) -> list[str]:
    """15 dakika sızıntısı temizlenince eksilen teknik maddeleri ana zaman dilimlerinden tamamlar."""
    if not coin_snapshots:
        return []
    labels = (("1H", "Saatlik"), ("4H", "4 saatlik"), ("1D", "Günlük"))
    available = [(name, coin_snapshots.get(key)) for key, name in labels if coin_snapshots.get(key)]
    if not available:
        return []

    items = []
    def graph_location(labels: list[str]) -> str:
        if not labels:
            return ""
        if len(labels) == 1:
            return f"{labels[0].lower()} grafikte"
        joined = f"{', '.join(label.lower() for label in labels[:-1])} ve {labels[-1].lower()}"
        return f"{joined} grafiklerde"

    obv_up = [name for name, snap in available if "yüks" in str(snap.get("obv_direction", ""))]
    obv_down = [name for name, snap in available if "düş" in str(snap.get("obv_direction", ""))]
    if obv_up or obv_down:
        if len(obv_up) > len(obv_down):
            items.append(f"OBV {graph_location(obv_up)} yükseliyor; bu, alıcı katılımının genel olarak sürdüğünü gösteriyor.")
        elif len(obv_down) > len(obv_up):
            items.append(f"OBV {graph_location(obv_down)} düşüyor; bu, alıcı katılımının zayıfladığını gösteriyor.")
        else:
            items.append("OBV zaman dilimleri arasında aynı yönde ilerlemiyor; para akışı görünümü henüz tam uyumlu değil.")

    ema_bullish = [name for name, snap in available
                   if snap.get("ema20_relation") == "üstünde" and snap.get("ema_order") == "20>50>100>200"]
    if ema_bullish:
        items.append(f"Fiyat {graph_location(ema_bullish)} EMA20'nin üzerinde ve hareketli ortalamalar yükseliş sırasını koruyor; yükseliş yapısı henüz bozulmuş görünmüyor.")

    macd_up = [name for name, snap in available
               if snap.get("macd_cross") == "üstünde" and "yüks" in str(snap.get("macd_hist_direction", ""))]
    if macd_up:
        items.append(f"MACD {graph_location(macd_up)} sinyal çizgisinin üzerinde ve histogram güçleniyor; momentum yukarı yönü destekliyor.")
    return items


def _render_manual_analysis(result: dict, zones: dict, base: str, current_price: float = 0.0,
                            timing_snapshot: dict | None = None,
                            hourly_snapshot: dict | None = None,
                            coin_snapshots: dict | None = None) -> str:
    """Haiku'nun yapılandırılmış cevabını doğrular ve Telegram metnine dönüştürür."""
    if not isinstance(result, dict):
        raise ValueError("Yapılandırılmış manuel analiz alınamadı")

    general = _natural_manual_text(result.get("general_assessment"))
    trade_ideas = _natural_manual_text(result.get("trade_ideas"))
    expectation = _natural_manual_text(result.get("expectation"))
    technical = _manual_action_items(result.get("technical_indicators"))

    general = _manual_remove_15m_leak(general)
    trade_ideas = _manual_remove_15m_leak(trade_ideas)
    technical = [_manual_remove_15m_leak(item) for item in technical]
    trade_ideas = _manual_remove_unsafe_current_buy(trade_ideas, zones, current_price)
    expectation = _manual_remove_unsafe_current_buy(expectation, zones, current_price)
    expectation = _manual_enforce_single_stance(expectation)

    # Ücretli yanıt dönmüş olsa bile bozuk Türkçeyi kullanıcıya gönderme.
    general = _manual_drop_corrupt_sentences(general)
    trade_ideas = _manual_drop_corrupt_sentences(trade_ideas)
    technical = [item for item in technical if not _manual_has_language_corruption(item)]
    if _manual_has_language_corruption(expectation):
        expectation = ""

    general = _manual_sentence_limit(_manual_remove_invented_levels(general, zones, current_price), 3)
    trade_ideas = _manual_sentence_limit(_manual_remove_invented_levels(trade_ideas, zones, current_price), 3)
    expectation = _manual_sentence_limit(_manual_remove_invented_levels(expectation, zones, current_price), 6)
    technical = [cleaned for item in technical
                 if (cleaned := _manual_remove_invented_levels(item, zones, current_price))][:5]
    if len(technical) < 2:
        for fallback_item in _manual_technical_fallbacks(coin_snapshots):
            fallback_lower = fallback_item.lower()
            repeated_indicator = any(
                indicator in fallback_lower and indicator in existing.lower()
                for existing in technical
                for indicator in ("obv", "macd", "ema20", "hareketli ortalama")
            )
            if repeated_indicator:
                continue
            if fallback_item not in technical:
                technical.append(fallback_item)
            if len(technical) >= 2:
                break

    # Direnç hesaplanmadıysa modelin hayalî direnç üzerinden senaryo kurmasını engelle.
    if not zones.get("resistance_1"):
        def keep_resistance_sentence(sentence: str) -> bool:
            lowered = sentence.lower()
            if "direnç" not in lowered:
                return True
            return any(phrase in lowered for phrase in
                       ("güvenilir direnç", "direnç oluşmadı", "direnç hesaplanmadı", "direnç bulunmuyor"))

        trade_ideas = " ".join(sentence for sentence in re.split(r"(?<=[.!?])\s+", trade_ideas)
                               if keep_resistance_sentence(sentence)).strip()
        expectation = " ".join(sentence for sentence in re.split(r"(?<=[.!?])\s+", expectation)
                               if keep_resistance_sentence(sentence)).strip()
        trade_ideas = " ".join(sentence for sentence in re.split(r"(?<=[.!?])\s+", trade_ideas)
                               if not any(word in sentence.lower() for word in ("hedef", "yarın"))).strip()
        expectation = " ".join(sentence for sentence in re.split(r"(?<=[.!?])\s+", expectation)
                               if not any(word in sentence.lower() for word in ("hedef", "yarın"))).strip()

    # Ücretli yanıtın küçük bir alanı boşsa bütün analizi çöpe atma.
    general = general or ("Saatlik, 4 saatlik ve günlük veriler birlikte değerlendirildiğinde coin için tek yönlü "
                          "ve yeterince güçlü bir görünüm oluşmuyor. Fiyatın konumu ile BTC'nin kısa vadeli "
                          "hareketi birlikte izlenmeli.")
    trade_ideas = trade_ideas or ("Fiyat desteklerden belirgin biçimde uzak olduğu için mevcut seviyede giriş "
                                  "riski artmış durumda. Yükseliş sürecekse kısa bir dinlenmenin ardından alıcı "
                                  "katılımının yeniden güçlenmesi daha sağlıklı bir giriş zemini oluşturabilir.")
    if not technical:
        technical = ["Göstergelerden birbirini doğrulayan yeterli ve farklı teknik kanıt üretilemedi."]
    expectation = expectation or _manual_expectation_fallback(
        zones, current_price, timing_snapshot, hourly_snapshot,
    )
    # Model 15 dakikalık gözlemi beklentiye katmazsa gerçek snapshot'tan aynı paragrafa ekle.
    if "15 dakika" not in expectation.lower():
        expectation = f"{expectation} {_manual_timing_fallback(timing_snapshot, hourly_snapshot)}"
    expectation = _manual_align_expectation_with_timing(expectation, timing_snapshot)
    if not expectation or expectation.lower().startswith("15 dakikalık"):
        expectation = _manual_expectation_fallback(
            zones, current_price, timing_snapshot, hourly_snapshot,
        )

    body = (
        f"🔍 Genel Değerlendirme\n{base} şu anda {_fmt(current_price)} seviyesinde işlem görüyor. "
        f"{_manual_price_position_summary(zones, current_price)} {general}\n\n"
        "📉 Teknik Göstergeler\n"
        + "\n".join(f"• {item}" for item in technical)
        + "\n\n📈 Kritik Seviyeler\n"
        f"• Yakın destek: {_manual_zone_text(zones.get('near_support'), current_price)}\n"
        f"• Sonraki destek: {_manual_zone_text(zones.get('next_support'), current_price)}\n"
        f"• Uzak yapısal destek: {_manual_zone_text(zones.get('structural_support'), current_price)}\n"
        f"• İlk direnç: {_manual_zone_text(zones.get('resistance_1'), current_price)}\n"
        f"• Direnç aşılırsa: {_manual_zone_text(zones.get('resistance_2'), current_price)}\n\n"
        f"📌 İşlem Fikirleri\n{trade_ideas}\n\n"
        f"🌌 Benim Beklentim — Ne Yapardım?\n{expectation}"
    )
    return body.replace(f"{base}'nin", f"{base}'in")


def _strip_15m_from_main(text: str) -> str:
    """15M gözleminin model tarafından ana analiz bölümlerine sızmasını engeller."""
    kept = []
    for line in (text or "").splitlines():
        if "15M" not in line and "15 dakika" not in line.lower():
            kept.append(line)
            continue
        sentences = re.split(r"(?<=[.!?])\s+", line.strip())
        clean_sentences = [s for s in sentences
                           if "15M" not in s and "15 dakika" not in s.lower()]
        if clean_sentences:
            kept.append(" ".join(clean_sentences))
    return "\n".join(kept).strip()


def _clean_timing_note(text: str) -> str:
    """15M notunu kısa tutar ve modelin hesaplanmamış sayısal eşik eklemesini siler."""
    sentences = re.split(r"(?<=[.!?])\s+", (text or "").strip())
    safe = []
    for sentence in sentences:
        # 1H/4H/1D/15M gibi zaman dilimleri eşleşmez; bağımsız fiyat rakamları eşleşir.
        if re.search(r"(?<![A-Za-z])\d+(?:[.,]\d+)?(?![A-Za-z])", sentence):
            continue
        safe.append(sentence)
        if len(safe) == 2:
            break
    note = " ".join(safe).strip()
    if note and not note.lower().startswith(("15m", "15 dakika")):
        note = f"15M açısından {note[0].lower() + note[1:] if len(note) > 1 else note.lower()}"
    return note


def _manual_action_text(price: float, zones: dict, coin: dict, btc: dict) -> str:
    """Devam, kararsızlık ve yorulmayı ayırarak tutarlı uygulama senaryosu üretir."""
    near = _manual_zone_text(zones.get("near_support"))
    next_zone = _manual_zone_text(zones.get("next_support"))
    h1, h4, d1 = coin.get("1H") or {}, coin.get("4H") or {}, coin.get("1D") or {}
    b1, b4 = btc.get("1H") or {}, btc.get("4H") or {}

    broad_up = all([
        h4.get("ema20_direction") == "yükseliyor", d1.get("ema20_direction") == "yükseliyor",
        h4.get("ema20_relation") == "üstünde", d1.get("ema20_relation") == "üstünde",
    ])
    h1_up = sum([
        h1.get("rsi_direction") == "yükseliyor",
        h1.get("macd_hist_direction") == "yükseliyor",
        h1.get("stoch_direction") == "yükseliyor",
        h1.get("obv_direction") == "yükseliyor",
    ])
    h1_weak = sum([
        h1.get("rsi_direction") == "düşüyor",
        h1.get("macd_hist_direction") == "düşüyor",
        h1.get("stoch_direction") == "düşüyor",
        h1.get("obv_direction") == "düşüyor",
    ])
    btc_weak = sum([
        b1.get("macd_hist_direction") == "düşüyor", b1.get("obv_direction") == "düşüyor",
        b4.get("macd_hist_direction") == "düşüyor", b4.get("obv_direction") == "düşüyor",
    ])
    stretched = (h1.get("ema20_distance_atr") or 0) > 2.2
    flow_support = h1.get("obv_direction") == "yükseliyor" or (h1.get("vol_ratio") or 0) >= 1.2

    if broad_up and h1_up >= 2 and flow_support and not stretched and btc_weak < 3:
        return (
            "Ben olsam ne yapardım?\n"
            "Geniş zaman dilimlerindeki yükseliş sürerken 1H hareketi ve para akışı da yeniden güçlendiği için "
            "yalnız geri çekilme bekleyip tamamen kenarda kalmazdım. Güçlü 1H kapanışın ardından fiyatın kırdığı "
            f"bölgeyi koruduğunu görürsem kontrollü ve kademeli değerlendirebilirdim; {near} bölgesi kaybedilirse "
            f"devam senaryosundan vazgeçip {next_zone} bölgesini beklerdim."
        )
    if stretched or (h1_weak >= 3 and btc_weak >= 2):
        if stretched and h1_weak >= 3 and btc_weak >= 2:
            reason = "Coin EMA20'den belirgin biçimde uzaklaşmış; ayrıca coin ile BTC'nin kısa vadeli gücü birlikte zayıflıyor."
        elif stretched:
            reason = "Coin kısa vadede EMA20'den belirgin biçimde uzaklaştığı için geri çekilme riski büyümüş."
        else:
            reason = "Coin ile BTC'nin kısa vadeli göstergeleri birlikte güç kaybettiği için yükselişin devamı henüz net değil."
        return (
            "Ben olsam ne yapardım?\n"
            f"{reason} Bu nedenle "
            f"mevcut fiyatı kovalamazdım. {near} bölgesinde 1H kapanışın desteği korumasını ve para akışıyla birlikte "
            f"yukarı dönüş oluşmasını beklerdim; bölge kaybedilirse {next_zone} bölgesini izlerdim."
        )
    return (
        "Ben olsam ne yapardım?\n"
        "Geniş yapı olumlu olsa da kısa vadeli kanıtlar aynı yönde değil; bu nedenle ne doğrudan fiyatı kovalar ne de "
        f"yükseliş ihtimalini tamamen elerdim. {near} bölgesinin korunmasıyla 1H momentumunun yeniden güçlenmesini "
        f"beklerdim; yakın destek kaybedilirse {next_zone} bölgesine kadar işlemden uzak dururdum."
    )


def _manual_tf_text(label: str, snap: dict | None, include_candle_structure: bool = False,
                    include_price: bool = True) -> str:
    if not snap:
        return f"{label}: veri yok"
    price_text = f"fiyat={_fmt(snap['price'])}; " if include_price else ""
    text = (
        f"{label}: {price_text}RSI {snap['rsi_direction']}; "
        f"MACD={snap['macd_position']}, histogram {snap['macd_hist_direction']}, çizgi sinyalin {snap['macd_cross']}; "
        f"StochRSI yön {snap['stoch_direction']}, "
        f"hızlı çizgi yavaş çizginin {snap['stoch_cross']}; "
        f"OBV {snap['obv_direction']}; Williams%R {snap['willr_direction']}; "
        f"fiyat EMA20'nin {snap['ema20_relation']} ({snap['ema20_distance_atr']} ATR), "
        f"EMA20 {snap['ema20_direction']}, EMA dizilimi {snap['ema_order']}; hacim {snap['vol_ratio']}x"
    )
    if include_candle_structure:
        status = "kapanmış" if snap["last_candle_closed"] else "halen açık ve değişebilir"
        text += (
            f"; son dört kapanış {snap['recent_close_shape']}; son dört tepe {snap['recent_high_shape']}; "
            f"son dört dip {snap['recent_low_shape']}; son mum {status}; "
            f"mum gövdesi {'belirgin' if snap['last_body_range_ratio'] >= 0.55 else 'sınırlı'}, "
            f"kapanış mum aralığının {'üst tarafında' if snap['last_close_location'] >= 0.65 else 'alt tarafında' if snap['last_close_location'] <= 0.35 else 'orta tarafında'}"
        )
    return text


_MANUAL_V2_RESOLVED_MODEL = None


def _resolve_manual_v2_model() -> str:
    """Yapılandırılan modeli doğrular; kapanmışsa güncel kararlı Flash-Lite'ı seçer."""
    global _MANUAL_V2_RESOLVED_MODEL
    if _MANUAL_V2_RESOLVED_MODEL:
        return _MANUAL_V2_RESOLVED_MODEL
    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY bulunamadı")

    available = []
    page_token = None
    for _ in range(4):
        params = {"pageSize": 1000}
        if page_token:
            params["pageToken"] = page_token
        response = requests.get(
            "https://generativelanguage.googleapis.com/v1beta/models",
            headers={"x-goog-api-key": GEMINI_API_KEY},
            params=params,
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
        for item in payload.get("models", []):
            name = str(item.get("name", "")).removeprefix("models/")
            methods = item.get("supportedGenerationMethods", [])
            if "generateContent" in methods:
                available.append(name)
        page_token = payload.get("nextPageToken")
        if not page_token:
            break

    if MANUAL_ANALYZER_V2_MODEL in available:
        _MANUAL_V2_RESOLVED_MODEL = MANUAL_ANALYZER_V2_MODEL
        return _MANUAL_V2_RESOLVED_MODEL

    candidates = [
        name for name in available
        if re.fullmatch(r"gemini-\d+(?:\.\d+)*-flash-lite", name)
    ]
    if not candidates:
        raise RuntimeError(
            f"Yapılandırılan Gemini modeli erişilemiyor ({MANUAL_ANALYZER_V2_MODEL}) "
            "ve kullanılabilir kararlı Flash-Lite modeli bulunamadı"
        )

    def version_key(name: str):
        version = name.removeprefix("gemini-").removesuffix("-flash-lite")
        return tuple(int(part) for part in version.split("."))

    _MANUAL_V2_RESOLVED_MODEL = max(candidates, key=version_key)
    print(
        f"[MANUEL ANALYZER MODEL] {MANUAL_ANALYZER_V2_MODEL} erişilemiyor; "
        f"güncel Flash-Lite={_MANUAL_V2_RESOLVED_MODEL} seçildi.",
        flush=True,
    )
    return _MANUAL_V2_RESOLVED_MODEL


def _manual_v2_quality_issues(result: dict) -> list[str]:
    """Telegram'a gitmeden önce V2 anlatımındaki açık dil ve mantık kusurlarını yakalar."""
    if not isinstance(result, dict):
        return ["Yanıt JSON nesnesi değil."]
    required = ("general_assessment", "technical_indicators", "trade_ideas", "expectation")
    issues = [f"{field} alanı eksik." for field in required if not result.get(field)]
    technical = result.get("technical_indicators")
    if not isinstance(technical, list) or not 2 <= len(technical) <= 4:
        issues.append("technical_indicators iki-dört maddelik liste olmalı.")

    text = " ".join(
        " ".join(value) if isinstance(value, list) else str(value or "")
        for value in (result.get("general_assessment"), technical,
                      result.get("trade_ideas"), result.get("expectation"))
    )
    lowered = text.lower()
    forbidden = {
        "lider kripto para": "Bitcoin için gereksiz 'lider kripto para' kalıbı kullanılmış.",
        "bekleme politikası": "Yapay 'bekleme politikası' kalıbı kullanılmış.",
        "büyük para girişi": "OBV'den kanıtsız 'büyük para girişi' sonucu çıkarılmış.",
        "ana trend": "Yapay 'ana trend' kalıbı kullanılmış.",
        "büyük trend": "Yapay 'büyük trend' kalıbı kullanılmış.",
        "ana yön": "Belirsiz 'ana yön' kalıbı kullanılmış.",
        "ana hareket": "Belirsiz 'ana hareket' kalıbı kullanılmış.",
        "dört saatlik": "Zaman dilimi 'dört saatlik' yerine '4 saatlik' yazılmalı.",
        "fiyatımız": "Fiyat sahiplenen bir dille anlatılmış.",
        "coinimiz": "Coin sahiplenen bir dille anlatılmış.",
        "yönümüz": "Yön sahiplenen bir dille anlatılmış.",
        "kârı cebe": "Kullanıcının açık pozisyonu olduğu varsayılmış.",
        "kapanma eğiliminde": "Açık veya kapanmış mumun durumu belirsiz anlatılmış.",
    }
    issues.extend(message for phrase, message in forbidden.items() if phrase in lowered)
    if re.search(r"\bfikir\w*\s+tamamen\s+geçerliliğini\s+yitir", lowered):
        issues.append("Yakın desteğin kaybı bütün gelecek alım ihtimallerini geçersiz göstermiş.")
    expectation = str(result.get("expectation") or "").lower()
    if "bekle" in expectation and "beklemekten vazgeç" in expectation:
        issues.append("Bekleme tavrıyla 'beklemekten vazgeçme' koşulu mantıksal olarak çelişiyor.")
    return issues


def _manual_v2_normalize_language(result: dict) -> tuple[dict, list[str]]:
    """Anlamı değiştirmeyen yüzeysel dil kusurlarını ücretsiz olarak düzeltir.

    Modeli yeniden çağırmak yalnızca gerçek içerik veya mantık kusurları için
    saklanır. Buradaki dönüşümler veri, karar, koşul veya fiyat seviyesi eklemez.
    """
    if not isinstance(result, dict):
        return result, []

    replacements = (
        (r"\blider kripto para\s+bitcoin\b", "Bitcoin", "lider kripto para Bitcoin→Bitcoin"),
        (r"\blider kripto para\b", "Bitcoin", "lider kripto para→Bitcoin"),
        (r"\bdört saatlik\b", "4 saatlik", "dört saatlik→4 saatlik"),
        (r"\bana yönünü\b", "genel eğilimini", "ana yönünü→genel eğilimini"),
        (r"\bana yönünün\b", "genel eğilimin", "ana yönünün→genel eğilimin"),
        (r"\bana yönünde\b", "genel eğiliminde", "ana yönünde→genel eğiliminde"),
        (r"\bana yönünden\b", "genel eğiliminden", "ana yönünden→genel eğiliminden"),
        (r"\bana yönüne\b", "genel eğilimine", "ana yönüne→genel eğilimine"),
        (r"\bana yönüyle\b", "genel eğilimiyle", "ana yönüyle→genel eğilimiyle"),
        (r"\bana yönlü\b", "genel olarak", "ana yönlü→genel olarak"),
        (r"\bana yönün\b", "genel eğilimin", "ana yönün→genel eğilimin"),
        (r"\bana yönü\b", "genel eğilim", "ana yönü→genel eğilim"),
        (r"\bana yön\b", "genel eğilim", "ana yön→genel eğilim"),
        (r"\bana hareket", "hareket", "ana hareket→hareket"),
        (r"\bana trend\b", "genel eğilim", "ana trend→genel eğilim"),
        (r"\bbüyük trend\b", "geniş görünüm", "büyük trend→geniş görünüm"),
        (r"\bbekleme politikası(?:nı)?\b", "beklemeyi", "bekleme politikası→beklemeyi"),
        (r"\bgenel piyasa ve genel eğilim\b", "genel görünüm", "genel piyasa ve genel eğilim→genel görünüm"),
        (r"\bfiyatın genel eğilimin\b", "fiyatın genel eğiliminin", "fiyatın genel eğilimin→fiyatın genel eğiliminin"),
    )
    applied = []

    def normalize_text(value: str) -> str:
        text = value
        for pattern, replacement, label in replacements:
            def replace_match(match):
                if match.group(0)[:1].isupper() and replacement[:1].islower():
                    return replacement[:1].upper() + replacement[1:]
                return replacement
            updated, count = re.subn(pattern, replace_match, text, flags=re.IGNORECASE)
            if count:
                applied.append(label)
                text = updated
        return text

    normalized = dict(result)
    for field in ("general_assessment", "technical_indicators", "trade_ideas", "expectation"):
        value = normalized.get(field)
        if isinstance(value, str):
            normalized[field] = normalize_text(value)
        elif isinstance(value, list):
            normalized[field] = [normalize_text(item) if isinstance(item, str) else item for item in value]
    return normalized, list(dict.fromkeys(applied))


def _manual_v2_zone_gap(zone: dict | None, price: float, side: str) -> float | None:
    if not zone or not price:
        return None
    boundary = float(zone["high"] if side == "support" else zone["low"])
    distance = price - boundary if side == "support" else boundary - price
    return max(0.0, distance / price * 100)


def _manual_v2_gemini_plan(base: str, current_price: float, technical_block: str,
                           timing_block: str, btc_block: str, zone_block: str,
                           price_location_note: str, model_name: str) -> dict:
    """Gemini yalnız kontrollü karar seçenekleri üretir; kullanıcı metnini yazmaz."""
    prompt = f"""{base} için güncel spot verilerini birlikte değerlendir. Tek göstergeye veya sabit eşiğe göre
mekanik karar verme. Fiyat yapısı, zaman dilimleri, para akışı, BTC etkisi, destek-dirence göre konum ve
15 dakikalık giriş zamanlamasını beraber tart. Kullanıcıya gönderilecek Türkçe metni yazma; yalnız izin verilen
seçeneklerle karar planını doldur.

[COIN — güncel fiyat {_fmt(current_price)}]
{technical_block}
[BTC]
{btc_block}
[BÖLGELER]
{zone_block}
{price_location_note}
[15 DAKİKALIK ZAMANLAMA]
{timing_block}

action: Şu anki tavır. wait=bekle, small_buy=yalnız yüksek riskli küçük başlangıç düşünülebilir,
no_buy=mevcut koşullarda alım düşünme.
coin_1h_view, coin_4h_view, coin_1d_view: Her zaman diliminin göstergelerini birlikte okuyarak görünümü
up, mixed veya down olarak değerlendir; tek göstergeye bağlanma.
reasons: Kararı en iyi açıklayan en fazla üç farklı neden.
entry_trigger: Alımı yeniden değerlendirmek için gereken somut gelişme.
invalidation: Mevcut alım düşüncesini bozan gelişme.
take_profit: Hesaplanmış dirençlere göre yaklaşım; direnç yoksa follow_trend.
timing_15m: 15 dakikalık verinin yalnız giriş zamanlamasına etkisi.
btc_effect: BTC'nin coin üzerindeki güncel etkisi."""
    enums = {
        "action": ["wait", "small_buy", "no_buy"],
        "reason": ["aligned_uptrend", "short_term_weakness", "price_near_resistance",
                   "price_far_support", "limited_price_space", "favorable_price_space",
                   "btc_weakness", "btc_supportive", "buyer_participation", "mixed_timeframes"],
        "entry": ["near_support_hold", "resistance_break_hold", "momentum_recovery", "none"],
        "invalidation": ["near_support_break", "next_support_break", "trend_break"],
        "take_profit": ["first_resistance", "second_resistance", "follow_trend"],
        "timing": ["supportive", "weakening", "mixed", "open_candle_wait"],
        "btc": ["supportive", "neutral", "caution"],
    }
    schema = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": enums["action"]},
            "coin_1h_view": {"type": "string", "enum": ["up", "mixed", "down"]},
            "coin_4h_view": {"type": "string", "enum": ["up", "mixed", "down"]},
            "coin_1d_view": {"type": "string", "enum": ["up", "mixed", "down"]},
            "reasons": {"type": "array", "minItems": 1, "maxItems": 3,
                        "items": {"type": "string", "enum": enums["reason"]}},
            "entry_trigger": {"type": "string", "enum": enums["entry"]},
            "invalidation": {"type": "string", "enum": enums["invalidation"]},
            "take_profit": {"type": "string", "enum": enums["take_profit"]},
            "timing_15m": {"type": "string", "enum": enums["timing"]},
            "btc_effect": {"type": "string", "enum": enums["btc"]},
        },
        "required": ["action", "coin_1h_view", "coin_4h_view", "coin_1d_view",
                     "reasons", "entry_trigger", "invalidation",
                     "take_profit", "timing_15m", "btc_effect"],
    }
    started = time.time()
    response = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent",
        headers={"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY},
        json={
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "responseSchema": schema,
                                 "maxOutputTokens": 350, "temperature": 0.15},
        },
        timeout=45,
    )
    response.raise_for_status()
    payload = response.json()
    parts = payload.get("candidates", [{}])[0].get("content", {}).get("parts", [])
    raw_text = "".join(str(part.get("text") or "") for part in parts)
    if not raw_text.strip():
        raise ValueError("Flash-Lite boş karar planı döndürdü")
    result = json.loads(raw_text)
    usage = payload.get("usageMetadata") or {}
    _log_usage(
        "manual_coin_analysis_v2_plan", model_name, _PROMPT_V_MANUAL_V2_PLAN,
        int(usage.get("promptTokenCount") or 0), int(usage.get("candidatesTokenCount") or 0),
        time.time() - started, prompt_chars=len(prompt),
    )
    return result


def _render_manual_v2_controlled(plan: dict, zones: dict, base: str, current_price: float,
                                 timing_snapshot: dict | None, coin_snapshots: dict,
                                 btc_snapshots: dict) -> str:
    """Kontrollü karar planını güncel sayısal verilerle değişmez Türkçe rapora dönüştürür."""
    coin_dirs = {
        "1H": plan.get("coin_1h_view", "mixed"),
        "4H": plan.get("coin_4h_view", "mixed"),
        "1D": plan.get("coin_1d_view", "mixed"),
    }

    if all(value == "up" for value in coin_dirs.values()):
        coin_view = f"{base} saatlik, 4 saatlik ve günlük grafiklerde yükseliş yapısını koruyor."
    elif coin_dirs["1D"] == "up" and coin_dirs["1H"] != "down":
        coin_view = (f"{base} günlük grafikte yükseliş yapısını koruyor; saatlik ve 4 saatlik görünümde ise "
                     "hareketin hızı aynı ölçüde güçlü değil.")
    elif coin_dirs["1D"] == "down":
        coin_view = (f"{base} günlük görünümde zayıf kalırken saatlik ve 4 saatlik hareketler henüz bu baskıyı "
                     "ortadan kaldıracak kadar uyumlu değil.")
    else:
        coin_view = f"{base} saatlik, 4 saatlik ve günlük grafiklerde aynı yönde ilerlemiyor."

    btc_effect = plan.get("btc_effect")
    if btc_effect == "supportive":
        btc_view = f"Bitcoin'in görünümü {base} üzerindeki genel piyasa baskısını azaltıyor."
    elif btc_effect == "caution":
        btc_view = f"Bitcoin'deki kısa vadeli zayıflama {base} üzerindeki yükseliş hızını sınırlayabilir."
    else:
        btc_view = f"Bitcoin şu anda {base} için belirgin bir destek veya baskı oluşturmuyor."

    obv_up = [key for key in ("1H", "4H", "1D")
              if "yüks" in str((coin_snapshots.get(key) or {}).get("obv_direction", ""))]
    obv_down = [key for key in ("1H", "4H", "1D")
                if "düş" in str((coin_snapshots.get(key) or {}).get("obv_direction", ""))]
    if len(obv_up) > len(obv_down):
        flow_view = "Para akışı göstergesi alıcı katılımının genel olarak sürdüğünü gösteriyor."
    elif len(obv_down) > len(obv_up):
        flow_view = "Para akışı göstergesi alıcı katılımının zayıfladığını gösteriyor."
    else:
        flow_view = "Para akışı zaman dilimleri arasında aynı yönde ilerlemiyor."

    technical = _manual_technical_fallbacks(coin_snapshots)
    if len(technical) < 2:
        technical.append(flow_view)
    if len(technical) < 2:
        technical.append("Göstergeler zaman dilimleri arasında tam uyum göstermediği için fiyatın bulunduğu bölge daha fazla önem kazanıyor.")
    technical = list(dict.fromkeys(technical))[:3]

    support_gap = _manual_v2_zone_gap(zones.get("near_support"), current_price, "support")
    resistance_gap = _manual_v2_zone_gap(zones.get("resistance_1"), current_price, "resistance")
    if support_gap is not None and resistance_gap is not None:
        if resistance_gap < support_gap:
            trade_ideas = (f"İlk dirence kalan yaklaşık %{resistance_gap:.1f} yükseliş alanı, yakın desteğe olası "
                           f"%{support_gap:.1f} geri çekilme mesafesinden küçük. Bu nedenle mevcut seviyeden yeni "
                           "alımın kısa vadeli kazanç alanı sınırlı; destek tepkisini veya direnç üzerinde kalıcılığı "
                           "beklemek daha dengeli olur.")
        else:
            trade_ideas = (f"İlk dirence kadar yaklaşık %{resistance_gap:.1f} alan bulunurken yakın destek yaklaşık "
                           f"%{support_gap:.1f} aşağıda. Fiyatın bulunduğu konum alım ihtimalini tamamen dışlamıyor; "
                           "yine de giriş için alıcıların yeniden güçlendiğini görmek gerekir.")
    elif zones.get("near_support"):
        trade_ideas = ("Yakın destek alım fikri için izlenebilir; ancak güvenilir bir üst direnç hesaplanmadığı için "
                       "kâr alma alanı önceden netleştirilemiyor.")
    else:
        trade_ideas = "Mevcut bölgeler belirgin bir giriş avantajı göstermediği için yeni alımda acele etmezdim."

    action = plan.get("action")
    if action == "small_buy":
        opening = "Ben olsam yalnız yüksek riski kabul ederek küçük ve kademeli bir başlangıç alımını değerlendirirdim."
    elif action == "no_buy":
        opening = "Ben olsam mevcut koşullarda yeni alım düşünmezdim."
    else:
        opening = "Ben olsam şu anda beklerdim."

    reasons = set(plan.get("reasons") or [])
    rationale_parts = []
    if action in {"wait", "no_buy"}:
        if len(obv_down) > len(obv_up):
            rationale_parts.append("alıcı katılımının zayıflaması")
        if btc_effect == "caution" or "btc_weakness" in reasons:
            rationale_parts.append("Bitcoin'in kısa vadede baskı oluşturması")
        if "short_term_weakness" in reasons or "mixed_timeframes" in reasons:
            rationale_parts.append("saatlik ve 4 saatlik hareketin henüz birlikte güçlenmemesi")
        if "limited_price_space" in reasons or "price_near_resistance" in reasons:
            rationale_parts.append("ilk dirence kalan alanın sınırlı olması")
    else:
        if "aligned_uptrend" in reasons:
            rationale_parts.append("zaman dilimlerinin yükselişi desteklemesi")
        if len(obv_up) > len(obv_down) or "buyer_participation" in reasons:
            rationale_parts.append("alıcı katılımının sürmesi")
        if "favorable_price_space" in reasons:
            rationale_parts.append("dirence kadar yeterli fiyat alanı bulunması")

    if rationale_parts:
        selected_reasons = rationale_parts[:2]
        joined_reasons = (selected_reasons[0] if len(selected_reasons) == 1
                          else f"{selected_reasons[0]} ve {selected_reasons[1]}")
        rationale_text = f"Bu tercihin temel nedeni {joined_reasons}."
    else:
        rationale_text = "Bu tercihte fiyatın bulunduğu bölge ile kısa vadeli göstergeleri birlikte dikkate alırdım."

    trigger = plan.get("entry_trigger")
    if trigger == "near_support_hold" and zones.get("near_support"):
        trigger_text = ("Alımı yeniden değerlendirmek için fiyatın yakın desteğe yaklaşmasını, bu bölgede tutunmasını "
                        "ve alıcıların yeniden güçlenmesini görmek isterdim.")
    elif trigger == "resistance_break_hold" and zones.get("resistance_1"):
        trigger_text = "Alımı yeniden değerlendirmek için ilk direncin aşılmasını ve fiyatın bu bölgenin üzerinde kalmasını görmek isterdim."
    elif trigger == "momentum_recovery":
        trigger_text = "Alımı yeniden değerlendirmek için saatlik göstergelerin ve para akışının birlikte yeniden güçlenmesini görmek isterdim."
    else:
        trigger_text = "Yeni alım için mevcut görünümden daha belirgin bir fiyat avantajı oluşmasını beklerdim."

    if timing_snapshot:
        if plan.get("timing_15m") == "supportive":
            timing_text = "15 dakikalık kapanmış mumlar giriş zamanlamasını destekliyor; bunu yine de tek başına alım nedeni saymazdım."
        elif plan.get("timing_15m") == "weakening":
            timing_text = "15 dakikalık kapanmış mumlarda zayıflama sürdüğü için satış baskısının durmasını beklerdim."
        else:
            timing_text = "15 dakikalık kapanmış mumlar net bir giriş zamanlaması göstermiyor."
    else:
        timing_text = "15 dakikalık veri alınamadığı için giriş zamanlamasını ayrıca değerlendiremedim."

    invalidation = plan.get("invalidation")
    if invalidation == "near_support_break" and zones.get("near_support"):
        invalidation_text = ("Yakın destek kaybedilirse yalnız bu bölgeden alım düşüncesi geçersiz olur; sonraki "
                             "destekte güncel verilerle yeniden değerlendirme yapardım.")
    elif invalidation == "next_support_break" and zones.get("next_support"):
        invalidation_text = "Sonraki destek de kaybedilirse alım düşüncesini bırakır ve yeni bir yapı oluşmasını beklerdim."
    else:
        invalidation_text = "Saatlik ve 4 saatlik yapı birlikte aşağı dönerse alım düşüncesini bırakırdım."

    if plan.get("take_profit") == "second_resistance" and zones.get("resistance_2"):
        profit_text = "Olası bir alımdan sonra ilk dirençte kısmi, sonraki dirençte kalan bölüm için kâr almayı değerlendirirdim."
    elif zones.get("resistance_1"):
        profit_text = "Olası bir alımdan sonra ilk direnç bölgesinde kısmi kâr almayı değerlendirirdim."
    else:
        profit_text = "Güvenilir bir direnç hesaplanmadığı için sabit hedef yerine hareket zayıfladıkça kademeli kâr almayı düşünürdüm."

    expectation = " ".join((opening, rationale_text, trigger_text, timing_text, invalidation_text, profit_text))
    general = " ".join((coin_view, flow_view, btc_view))
    return (
        f"🔍 Genel Değerlendirme\n{base} şu anda {_fmt(current_price)} seviyesinde işlem görüyor. "
        f"{_manual_price_position_summary(zones, current_price)} {general}\n\n"
        "📉 Teknik Göstergeler\n" + "\n".join(f"• {item}" for item in technical) +
        "\n\n📈 Kritik Seviyeler\n"
        f"• Yakın destek: {_manual_zone_text(zones.get('near_support'), current_price)}\n"
        f"• Sonraki destek: {_manual_zone_text(zones.get('next_support'), current_price)}\n"
        f"• Uzak yapısal destek: {_manual_zone_text(zones.get('structural_support'), current_price)}\n"
        f"• İlk direnç: {_manual_zone_text(zones.get('resistance_1'), current_price)}\n"
        f"• Direnç aşılırsa: {_manual_zone_text(zones.get('resistance_2'), current_price)}\n\n"
        f"📌 İşlem Fikirleri\n{trade_ideas}\n\n"
        f"🌌 Benim Beklentim — Ne Yapardım?\n{expectation}"
    )


def _manual_v2_gemini_analysis(base: str, current_price: float, technical_block: str,
                               timing_block: str, btc_block: str, zone_block: str,
                               price_location_note: str, model_name: str) -> dict:
    """Doğrulanmış piyasa verisini Flash-Lite ile sade bir danışman anlatımına dönüştürür."""
    if not GEMINI_API_KEY:
        raise ValueError("MANUAL_ANALYZER_MODE=v2 fakat GEMINI_API_KEY bulunamadı")

    prompt = f"""Bir spot trader gibi düşün; fakat sonucu teknik terimlere hâkim olmayan bir müşteriye anlat.
Amaç, {base} için şu dört soruya açık cevap vermektir:
1. Coin ve genel piyasa şu anda nasıl görünüyor?
2. Önemli teknik veriler birlikte ne anlatıyor?
3. Şu an alım düşünülür mü, yoksa hangi koşul beklenir?
4. Fikir hangi gelişmede geçersiz olur ve hesaplanmış direnç varsa nerede kâr alınabilir?

Sabit RSI veya başka gösterge eşiklerine göre mekanik karar verme. Göstergelerin yönünü, fiyat yapısını,
para akışını, BTC etkisini ve fiyatın destek/dirençlere konumunu birlikte değerlendir. Para akışı zayıf diye
fırsatı otomatik eleme; bunun hareketin gücü ve süresi açısından ne anlama geldiğini söyle.

[COIN — güncel fiyat {_fmt(current_price)}]
{technical_block}

[BTC]
{btc_block}

[HESAPLANMIŞ BÖLGELER]
{zone_block}
{price_location_note}

[15 DAKİKALIK GİRİŞ ZAMANLAMASI — yalnız beklenti alanında kullan]
{timing_block}

Yazım kuralları:
- general_assessment: Coinin saatlik, 4 saatlik ve günlük görünümünü ve BTC etkisini 3-4 doğal cümlede özetle.
- technical_indicators: Yalnız karar açısından önemli 2-4 farklı teknik bulgu yaz; her bulgunun fiyat açısından
  ne anlattığını aynı maddede açıkla.
- trade_ideas: Mevcut fiyat, yakın destek ve varsa direnç arasında uygulanabilir olasılıkları 2-3 bağlantılı
  cümlede karşılaştır. İlk dirence kalan yükseliş alanı yakın desteğe olası geri çekilme mesafesinden küçükse,
  yeni alımın kısa vadeli kazanç alanının geri çekilme riskine göre sınırlı olduğunu açıkça söyle. Bu karşılaştırmayı
  destek veya direnci kesin hedefmiş gibi sunmadan, bekleme ya da küçük alım tercihinin temel gerekçesine bağla.
- expectation: En önemli alandır. Tek doğal paragrafta bugün ne yapacağını açıkça söyle: küçük alım, bekleme
  veya alım düşünmeme seçeneklerinden birini seç. Nedenini, alım için görmek istediğin somut gelişmeyi,
  vazgeçme koşulunu ve varsa kâr alma yaklaşımını müşterinin anlayacağı dille anlat. 15 dakikalık veriyi ayrı
  başlık yapmadan yalnız giriş zamanlamasına yardımcı kanıt olarak bu paragrafa kat.
- Kesin gelecek tahmini yapma. BTC zayıf diye coin fırsatını otomatik iptal etme.
- Yeni fiyat seviyesi üretme; sayısal fiyat yazma. Bölgeler rapora kod tarafından eklenecek.
- İngilizce işlem terimi kullanma. Teknik terim gerekiyorsa sade anlamını aynı cümlede açıkla.
- OBV yükselişini "büyük para girişi" veya kesin kurumsal alım diye yorumlama; yalnız alıcı katılımının
  ya da para akışının güçlendiğini söyle.
- BTC görünümünü bölümler arasında aynı kanıtlara dayanarak tutarlı anlat; bir bölümde kararsız, başka bir
  bölümde kesin düşüş gibi birbiriyle çelişen sonuçlar üretme.
- "Genel piyasa ve genel eğilim" gibi aynı anlamı tekrarlayan kalıplar kullanma; doğrudan genel görünümü anlat.
- "Ana hareket" gibi belirsiz bir kalıp kullanma; doğrudan yükseliş, düşüş veya hareket de.
- Kullanıcının açık pozisyonu olduğunu varsayma. Kâr alma fikrini yalnız "mevcut pozisyon varsa" veya
  "olası bir alımdan sonra" koşuluyla anlat; "kârı cebe koy" deme.
- Yakın desteğin kırılması yalnız o destekten alım düşüncesini geçersiz kılar. Bütün alım ihtimalinin tamamen
  bittiğini söyleme; hesaplanmış sonraki destek varsa orada yeni değerlendirme yapılabileceğini belirt.
- 15 dakikalık mum kapanmışsa yalnız gerçekleşen kapanışı anlat. Mum halen açıksa mevcut konumunun değişebileceğini
  açıkça söyle; açık mum için "kapandı" veya "kapanma eğiliminde" deme.
- Coini veya fiyatı sahiplenerek "fiyatımız", "coinimiz", "yönümüz" deme. "Lider kripto para Bitcoin",
  "bekleme politikası" gibi dolaylı ve yapay kalıplar yerine doğrudan ZEC, BTC, beklerdim veya almazdım de.
- 15 dakikalık veriden satış baskısı, hacim zayıflığı veya toparlanma sonucu çıkarıyorsan bunu destekleyen
  somut mum ya da gösterge yönünü aynı cümlede belirt; kanıt yoksa kesin hüküm verme.
- "ana trend", "çerçeve", "tema", "saturasyon", "konuma alış", "momentum harita", "çizgiler kapanmış"
  gibi yapay ifadeler kullanma. Kısa, doğal ve dilbilgisi düzgün Türkçe yaz.
"""
    schema = {
        "type": "object",
        "properties": {
            "general_assessment": {"type": "string"},
            "technical_indicators": {
                "type": "array", "minItems": 2, "maxItems": 4,
                "items": {"type": "string"},
            },
            "trade_ideas": {"type": "string"},
            "expectation": {"type": "string"},
        },
        "required": ["general_assessment", "technical_indicators", "trade_ideas", "expectation"],
    }
    request_prompt = prompt
    for attempt in (1, 2):
        started = time.time()
        response = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent",
            headers={"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY},
            json={
                "contents": [{"role": "user", "parts": [{"text": request_prompt}]}],
                "generationConfig": {
                    "responseMimeType": "application/json",
                    "responseSchema": schema,
                    "maxOutputTokens": 1400,
                    "temperature": 0.25 if attempt == 2 else 0.35,
                },
            },
            timeout=45,
        )
        response.raise_for_status()
        payload = response.json()
        parts = payload.get("candidates", [{}])[0].get("content", {}).get("parts", [])
        raw_text = "".join(str(part.get("text") or "") for part in parts)
        if not raw_text.strip():
            raise ValueError("Flash-Lite boş yanıt döndürdü")
        result = json.loads(raw_text)
        usage = payload.get("usageMetadata") or {}
        try:
            _log_usage(
                "manual_coin_analysis_v2", model_name, _PROMPT_V_MANUAL_V2,
                int(usage.get("promptTokenCount") or 0), int(usage.get("candidatesTokenCount") or 0),
                time.time() - started, prompt_chars=len(request_prompt),
            )
        except Exception as exc:
            print(f"[API_USAGE] Gemini kullanım kaydı yazılamadı: {exc}", flush=True)

        result, normalized_phrases = _manual_v2_normalize_language(result)
        if normalized_phrases:
            print(
                f"[MANUEL ANALYZER V2 DİL] deneme={attempt} | "
                + " | ".join(normalized_phrases),
                flush=True,
            )
        issues = _manual_v2_quality_issues(result)
        if not issues:
            return result
        print(
            f"[MANUEL ANALYZER V2 KONTROL] deneme={attempt} | " + " | ".join(issues),
            flush=True,
        )
        if attempt == 1:
            request_prompt = (
                prompt
                + "\n\n[ÖNCEKİ YANITTA BULUNAN HATALAR]\n- "
                + "\n- ".join(issues)
                + "\nÖnceki yanıtı kopyalama. Aynı güncel verileri kullanarak bütün alanları bu hatalar olmadan yeniden yaz."
            )
    raise ValueError("Flash-Lite çıktısı iki denemede de kalite kontrolünden geçmedi")


def _hybrid_fmt(value: float | None) -> str:
    if value is None:
        return "veri yok"
    value = float(value)
    if value >= 100:
        return f"{value:.2f}"
    if value >= 1:
        return f"{value:.3f}"
    if value >= 0.01:
        return f"{value:.4f}"
    if value >= 0.0001:
        return f"{value:.6f}"
    return f"{value:.8f}"


def _hybrid_pct_gap(price: float, boundary: float, side: str) -> float:
    raw = price - boundary if side == "support" else boundary - price
    return max(0.0, raw / price * 100)


def _hybrid_cluster_zones(frames: dict, live_price: float) -> list[dict]:
    """Yakınlık, zaman dilimi, güncellik ve tekrar sayısıyla bölge üret."""
    tf_weight = {"1H": 1.0, "4H": 2.0, "1D": 3.0}
    lookbacks = {"1H": 140, "4H": 100, "1D": 90}
    wings = {"1H": 3, "4H": 2, "1D": 2}
    points = []
    for label, candles in frames.items():
        subset = candles[-lookbacks[label]:]
        wing = wings[label]
        length = len(subset)
        for idx in range(wing, length - wing):
            window = subset[idx - wing:idx + wing + 1]
            age_ratio = (length - 1 - idx) / max(length - 1, 1)
            recency = max(0.35, 1.0 - 0.65 * age_ratio)
            if subset[idx].low <= min(c.low for c in window):
                points.append((subset[idx].low, label, tf_weight[label] * recency))
            if subset[idx].high >= max(c.high for c in window):
                points.append((subset[idx].high, label, tf_weight[label] * recency))

    h1 = frames["1H"]
    true_ranges = []
    for prev, cur in zip(h1[-15:-1], h1[-14:]):
        true_ranges.append(max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close)))
    atr_1h = float(np.mean(true_ranges)) if true_ranges else 0.0
    tolerance = max(live_price * 0.0035, atr_1h * 0.35)

    clusters: list[dict] = []
    for point, label, score in sorted(points, key=lambda item: item[0]):
        matched = next((z for z in clusters if abs(point - z["center"]) <= tolerance), None)
        if matched is None:
            clusters.append({"center": point, "weighted": point * score, "score": score,
                             "prices": [point], "tfs": {label}, "touches": 1})
            continue
        matched["weighted"] += point * score
        matched["score"] += score
        matched["prices"].append(point)
        matched["tfs"].add(label)
        matched["touches"] += 1
        matched["center"] = matched["weighted"] / matched["score"]

    for zone in clusters:
        pad = max(tolerance * 0.35, (max(zone["prices"]) - min(zone["prices"])) / 2)
        zone["low"] = min(zone["prices"]) - pad
        zone["high"] = max(zone["prices"]) + pad
        zone["strength"] = zone["score"] + min(zone["touches"], 5) * 0.15
    return clusters


def _hybrid_select_zones(frames: dict, live_price: float) -> dict:
    zones = _hybrid_cluster_zones(frames, live_price)
    supports = sorted((z for z in zones if z["high"] < live_price), key=lambda z: live_price - z["high"])
    resistances = sorted((z for z in zones if z["low"] > live_price), key=lambda z: z["low"] - live_price)
    active = sorted((z for z in zones if z["low"] <= live_price <= z["high"]),
                    key=lambda z: -z["strength"])
    # Fiyat bir bölgenin içindeyse bu bölge ilk direnç/karar alanıdır.
    resistance_1 = active[0] if active else (resistances[0] if resistances else None)
    resistance_2 = resistances[0] if active and resistances else (resistances[1] if len(resistances) > 1 else None)
    return {
        "near_support": supports[0] if supports else None,
        "next_support": supports[1] if len(supports) > 1 else None,
        "resistance_1": resistance_1,
        "resistance_2": resistance_2,
    }


def _hybrid_zone_text(zone: dict | None, live_price: float) -> str:
    if not zone:
        return "güvenilir bölge oluşmadı"
    names = {"1H": "1H", "4H": "4H", "1D": "1G"}
    tfs = " + ".join(names[x] for x in sorted(zone["tfs"]))
    text = f"{_hybrid_fmt(zone['low'])}–{_hybrid_fmt(zone['high'])} ({tfs})"
    if zone["low"] <= live_price <= zone["high"]:
        return text + " — fiyat bölgenin içinde"
    if live_price > zone["high"]:
        return text + f" — güncel fiyatın %{(live_price-zone['high'])/live_price*100:.1f} altında"
    return text + f" — güncel fiyatın %{(zone['low']-live_price)/live_price*100:.1f} üstünde"


class _HybridCandle:
    """Normal ücretsiz sorguya ait kapanmış mum kaydı; GPT modülüne bağımlı değildir."""
    __slots__ = ("open_time", "open", "high", "low", "close", "volume", "close_time")

    def __init__(self, row):
        self.open_time = int(row[0])
        self.open = float(row[1])
        self.high = float(row[2])
        self.low = float(row[3])
        self.close = float(row[4])
        self.volume = float(row[5])
        self.close_time = int(row[6])


def _hybrid_normalize_pair(symbol: str) -> str:
    pair = re.sub(r"[^A-Z0-9]", "", symbol.upper().strip())
    return pair if pair.endswith("USDT") else pair + "USDT"


def _hybrid_fetch_closed_klines(symbol: str, interval: str, limit: int = 260) -> list:
    """Açık mumu dışarıda bırakarak normal sorgu için Binance spot verisi çeker."""
    pair = _hybrid_normalize_pair(symbol)
    response = requests.get(
        "https://api.binance.com/api/v3/klines",
        params={"symbol": pair, "interval": interval, "limit": limit},
        timeout=15,
    )
    if response.status_code != 200:
        raise RuntimeError(f"Binance kline hatası ({pair} {interval}): {response.text[:160]}")
    now_ms = int(time.time() * 1000)
    candles = [_HybridCandle(row) for row in response.json() if int(row[6]) < now_ms]
    if len(candles) < 80:
        raise RuntimeError(f"{pair} {interval}: yeterli kapanmış mum yok ({len(candles)})")
    return candles


def _hybrid_fetch_live_price(symbol: str) -> float:
    pair = _hybrid_normalize_pair(symbol)
    response = requests.get(
        "https://api.binance.com/api/v3/ticker/price",
        params={"symbol": pair},
        timeout=15,
    )
    if response.status_code != 200:
        raise RuntimeError(f"Binance fiyat hatası ({pair}): {response.text[:160]}")
    return float(response.json()["price"])


def _hybrid_safe_round(value, digits: int = 2):
    try:
        number = float(value)
        return round(number, digits) if np.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _hybrid_direction(series: pd.Series) -> str:
    valid = series.dropna()
    if len(valid) < 2:
        return "veri_yok"
    if float(valid.iloc[-1]) > float(valid.iloc[-2]) + 1e-9:
        return "yukari"
    if float(valid.iloc[-1]) < float(valid.iloc[-2]) - 1e-9:
        return "asagi"
    return "yatay"


def _hybrid_recent_swings(candles: list, lookback: int = 60, wing: int = 2) -> tuple[float, float]:
    subset = candles[-lookback:]
    highs, lows = [], []
    for idx in range(wing, len(subset) - wing):
        window = subset[idx - wing:idx + wing + 1]
        if subset[idx].high == max(item.high for item in window):
            highs.append(subset[idx].high)
        if subset[idx].low == min(item.low for item in window):
            lows.append(subset[idx].low)
    last = candles[-1].close
    below = [value for value in lows if value <= last]
    above = [value for value in highs if value >= last]
    support = max(below) if below else min(item.low for item in subset)
    resistance = min(above) if above else max(item.high for item in subset)
    return support, resistance


def _hybrid_build_timeframe_snapshot(candles: list) -> dict:
    """GPT analizindeki yararlı teknik okumanın normal sorguya uyarlanmış bağımsız sürümü."""
    close = pd.Series([item.close for item in candles], dtype="float64")
    high = pd.Series([item.high for item in candles], dtype="float64")
    low = pd.Series([item.low for item in candles], dtype="float64")
    volume = pd.Series([item.volume for item in candles], dtype="float64")

    delta = close.diff()
    avg_gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    avg_loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    rsi = 100 - (100 / (1 + avg_gain / avg_loss.replace(0, np.nan)))
    rsi = rsi.where(avg_loss != 0, 100.0)

    rsi_low = rsi.rolling(14, min_periods=14).min()
    rsi_high = rsi.rolling(14, min_periods=14).max()
    stoch_den = rsi_high - rsi_low
    stoch = (100 * (rsi - rsi_low) / stoch_den.replace(0, np.nan)).where(stoch_den != 0, 50.0)
    stoch_ma = stoch.rolling(3, min_periods=3).mean()

    ema20 = close.ewm(span=20, adjust=False, min_periods=20).mean()
    ema50 = close.ewm(span=50, adjust=False, min_periods=50).mean()
    ema200 = close.ewm(span=200, adjust=False, min_periods=200).mean()
    macd = close.ewm(span=12, adjust=False, min_periods=12).mean() - close.ewm(
        span=26, adjust=False, min_periods=26
    ).mean()
    macd_signal = macd.ewm(span=9, adjust=False, min_periods=9).mean()
    macd_hist = macd - macd_signal

    prev_close = close.shift(1)
    true_range = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    atr = true_range.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()

    hh9, ll9 = high.rolling(9, min_periods=9).max(), low.rolling(9, min_periods=9).min()
    rsv = 100 * (close - ll9) / (hh9 - ll9).replace(0, np.nan)
    k_values, d_values, j_values = [], [], []
    k_prev = d_prev = 50.0
    for value in rsv:
        if pd.isna(value):
            k_values.append(np.nan)
            d_values.append(np.nan)
            j_values.append(np.nan)
            continue
        k_prev = (2 / 3) * k_prev + (1 / 3) * float(value)
        d_prev = (2 / 3) * d_prev + (1 / 3) * k_prev
        k_values.append(k_prev)
        d_values.append(d_prev)
        j_values.append(3 * k_prev - 2 * d_prev)
    kdj_k = pd.Series(k_values)
    kdj_d = pd.Series(d_values)
    kdj_j = pd.Series(j_values)

    hh14, ll14 = high.rolling(14, min_periods=14).max(), low.rolling(14, min_periods=14).min()
    williams = -100 * (hh14 - close) / (hh14 - ll14).replace(0, np.nan)

    obv_values = [0.0]
    for idx in range(1, len(candles)):
        if close.iloc[idx] > close.iloc[idx - 1]:
            obv_values.append(obv_values[-1] + volume.iloc[idx])
        elif close.iloc[idx] < close.iloc[idx - 1]:
            obv_values.append(obv_values[-1] - volume.iloc[idx])
        else:
            obv_values.append(obv_values[-1])
    obv = pd.Series(obv_values)

    support, resistance = _hybrid_recent_swings(candles)
    last, previous = candles[-1], candles[-2]
    atr_last = float(atr.dropna().iloc[-1]) if not atr.dropna().empty else None
    bb_mid = float(close.iloc[-20:].mean())
    bb_std = float(close.iloc[-20:].std(ddof=0))
    avg_volume = float(volume.iloc[-21:-1].mean()) if len(volume) >= 21 else float(volume.iloc[-20:].mean())
    candle_range = max(last.high - last.low, 1e-12)
    body = abs(last.close - last.open)
    range3 = max(item.high for item in candles[-3:]) - min(item.low for item in candles[-3:])
    price_3bar = 100 * (last.close / candles[-4].close - 1) if len(candles) >= 4 else None

    def last_value(series: pd.Series):
        valid = series.dropna()
        return float(valid.iloc[-1]) if not valid.empty else None

    stoch_now, stoch_ma_now = last_value(stoch), last_value(stoch_ma)
    return {
        "last_closed": {
            "time_utc": datetime.fromtimestamp(last.close_time / 1000, tz=timezone.utc).isoformat(),
            "open": _hybrid_safe_round(last.open, 8),
            "high": _hybrid_safe_round(last.high, 8),
            "low": _hybrid_safe_round(last.low, 8),
            "close": _hybrid_safe_round(last.close, 8),
            "change_pct": _hybrid_safe_round(100 * (last.close / previous.close - 1)),
            "body_pct_of_range": _hybrid_safe_round(100 * body / candle_range),
            "upper_wick_pct_of_range": _hybrid_safe_round(
                100 * (last.high - max(last.open, last.close)) / candle_range
            ),
            "lower_wick_pct_of_range": _hybrid_safe_round(
                100 * (min(last.open, last.close) - last.low) / candle_range
            ),
        },
        "momentum": {
            "rsi14": _hybrid_safe_round(last_value(rsi)),
            "rsi_direction": _hybrid_direction(rsi),
            "stochrsi": _hybrid_safe_round(stoch_now),
            "ma_stochrsi": _hybrid_safe_round(stoch_ma_now),
            "stochrsi_direction": _hybrid_direction(stoch),
            "stoch_vs_ma": (
                "ustunde" if stoch_now is not None and stoch_ma_now is not None and stoch_now > stoch_ma_now
                else "altinda"
            ),
            "macd": _hybrid_safe_round(last_value(macd), 8),
            "macd_signal": _hybrid_safe_round(last_value(macd_signal), 8),
            "macd_hist": _hybrid_safe_round(last_value(macd_hist), 8),
            "macd_hist_direction": _hybrid_direction(macd_hist),
            "kdj_k": _hybrid_safe_round(last_value(kdj_k)),
            "kdj_d": _hybrid_safe_round(last_value(kdj_d)),
            "kdj_j": _hybrid_safe_round(last_value(kdj_j)),
            "williams_r14": _hybrid_safe_round(last_value(williams)),
        },
        "trend_structure": {
            "ema20": _hybrid_safe_round(last_value(ema20), 8),
            "ema50": _hybrid_safe_round(last_value(ema50), 8),
            "ema200": _hybrid_safe_round(last_value(ema200), 8),
            "close_vs_ema20_pct": _hybrid_safe_round(100 * (last.close / last_value(ema20) - 1)) if last_value(ema20) else None,
            "close_vs_ema50_pct": _hybrid_safe_round(100 * (last.close / last_value(ema50) - 1)) if last_value(ema50) else None,
            "close_vs_ema200_pct": _hybrid_safe_round(100 * (last.close / last_value(ema200) - 1)) if last_value(ema200) else None,
            "support_recent": _hybrid_safe_round(support, 8),
            "resistance_recent": _hybrid_safe_round(resistance, 8),
            "distance_support_pct": _hybrid_safe_round(100 * (last.close / support - 1)) if support else None,
            "distance_resistance_pct": _hybrid_safe_round(100 * (resistance / last.close - 1)),
        },
        "volatility_volume": {
            "atr14": _hybrid_safe_round(atr_last, 8),
            "atr_pct": _hybrid_safe_round(100 * atr_last / last.close) if atr_last else None,
            "bollinger_mid": _hybrid_safe_round(bb_mid, 8),
            "bollinger_upper": _hybrid_safe_round(bb_mid + 2 * bb_std, 8),
            "bollinger_lower": _hybrid_safe_round(bb_mid - 2 * bb_std, 8),
            "volume_vs_20bar_avg": _hybrid_safe_round(last.volume / avg_volume) if avg_volume else None,
            "obv_direction_5bar": (
                "yukari" if obv.iloc[-1] > obv.iloc[-6]
                else "asagi" if obv.iloc[-1] < obv.iloc[-6] else "yatay"
            ),
        },
        "short_behavior": {
            "price_change_3bar_pct": _hybrid_safe_round(price_3bar),
            "three_bar_range_atr_multiple": _hybrid_safe_round(range3 / atr_last) if atr_last else None,
            "note": "Fiyat değişimi sınırlı ve 3 mum aralığı ATR'ye göre düşükse momentum yatay soğuyor olabilir.",
        },
    }


def _hybrid_market_snapshot(symbol: str) -> tuple[dict, dict]:
    pair = _hybrid_normalize_pair(symbol)
    base = pair[:-4]
    coin_frames = {
        "1D": _hybrid_fetch_closed_klines(pair, "1d"),
        "4H": _hybrid_fetch_closed_klines(pair, "4h"),
        "1H": _hybrid_fetch_closed_klines(pair, "1h"),
        "15M": _hybrid_fetch_closed_klines(pair, "15m"),
    }
    btc_frames = {
        "1D": _hybrid_fetch_closed_klines("BTC", "1d"),
        "4H": _hybrid_fetch_closed_klines("BTC", "4h"),
        "1H": _hybrid_fetch_closed_klines("BTC", "1h"),
    }
    live_price = _hybrid_fetch_live_price(pair)
    snapshot = {
        "symbol": base,
        "live_price": live_price,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "coin": {
            tf: _hybrid_build_timeframe_snapshot(coin_frames[tf])
            for tf in ("1D", "4H", "1H")
        },
        "timing_15m": _hybrid_build_timeframe_snapshot(coin_frames["15M"]),
        "btc": {
            tf: _hybrid_build_timeframe_snapshot(btc_frames[tf])
            for tf in ("1D", "4H", "1H")
        },
        "note": "Bütün indikatörler yalnız kapanmış mumlardan hesaplandı; live_price ayrıca anlık fiyattır.",
    }
    zones = _hybrid_select_zones(
        {key: coin_frames[key] for key in ("1D", "4H", "1H")},
        live_price,
    )
    return snapshot, zones
def _hybrid_decision_plan(snapshot: dict, zones: dict, api_key: str, model_name: str) -> tuple[dict, dict, int]:
    zone_payload = {key: None if value is None else {
        "low": round(value["low"], 10), "high": round(value["high"], 10),
        "timeframes": sorted(value["tfs"]), "touches": value["touches"],
        "strength": round(value["strength"], 2),
    } for key, value in zones.items()}
    prompt = """Aşağıdaki Binance spot verisini bir bütün olarak değerlendir. Bütün teknik göstergeler kapanmış
mumlardan hesaplandı; live_price yalnız anlık konumu gösterir. Mekanik tek gösterge kararı verme.

Karar sırası: 1D genel rejim, 4H'nin bu rejimdeki rolü, 1H giriş zamanlaması ve son olarak 15M yardımcı
zamanlama. StochRSI'yi kendi hareketli ortalaması, KDJ, fiyat davranışı, OBV ve MACD ile birlikte oku;
aşırı alım/aşırı satımı tek başına al-sat nedeni yapma. Fiyat düşmeden yatay kalarak momentum boşaltıyorsa
bunu özellikle ayır. Güçlü üst zaman diliminde 1H öncü göstergeler yeniden dönerken MACD'nin gecikmesini
tek başına ret nedeni yapma. 15M yalnız giriş zamanlamasını inceltsin. BTC bağlamını kullan
ama coinin kendi yapısını ezme. Yalnız JSON şemasındaki seçenekleri seç; Türkçe rapor yazma ve seviye uydurma.

VERİ:\n""" + json.dumps({"snapshot": snapshot, "zones": zone_payload}, ensure_ascii=False)
    schema = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["buy_candidate", "wait_trigger", "no_buy"]},
            "day_regime": {"type": "string", "enum": ["up", "mixed", "down"]},
            "h4_role": {"type": "string", "enum": ["continuation", "controlled_pullback", "reversal_attempt", "distribution", "breakdown", "mixed"]},
            "h1_timing": {"type": "string", "enum": ["retrigger", "sideways_reset", "pullback_reset", "overheated", "weakening", "mixed"]},
            "entry_type": {"type": "string", "enum": ["support_reaction", "momentum_retrigger", "resistance_break", "none"]},
            "btc_effect": {"type": "string", "enum": ["supportive", "neutral", "caution"]},
            "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
            "reasons": {"type": "array", "minItems": 1, "maxItems": 4, "items": {"type": "string", "enum": [
                "aligned_uptrend", "buyer_participation", "sideways_cooling", "controlled_pullback",
                "early_retrigger", "price_near_resistance", "price_far_support", "overheated_move",
                "momentum_weakness", "timeframe_conflict", "btc_supportive", "btc_weakness"]}},
        },
        "required": ["action", "day_regime", "h4_role", "h1_timing", "entry_type", "btc_effect", "confidence", "reasons"],
    }
    response = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent",
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
        json={"contents": [{"role": "user", "parts": [{"text": prompt}]}],
              "generationConfig": {"responseMimeType": "application/json", "responseSchema": schema,
                                     "maxOutputTokens": 450, "temperature": 0.1}}, timeout=45,
    )
    response.raise_for_status()
    payload = response.json()
    raw = "".join(str(p.get("text") or "") for p in payload.get("candidates", [{}])[0].get("content", {}).get("parts", []))
    if not raw.strip():
        raise ValueError("Ücretsiz model boş karar planı döndürdü")
    return json.loads(raw), (payload.get("usageMetadata") or {}), len(prompt)


def _hybrid_indicator_bullets(snapshot: dict) -> list[str]:
    coin = snapshot["coin"]
    bullets = []
    h1_momentum = coin["1H"]["momentum"]
    h1_behavior = coin["1H"]["short_behavior"]
    stoch = h1_momentum.get("stochrsi")
    stoch_ma = h1_momentum.get("ma_stochrsi")
    stoch_direction = h1_momentum.get("stochrsi_direction")
    stoch_vs_ma = h1_momentum.get("stoch_vs_ma")
    kdj_k = h1_momentum.get("kdj_k")
    kdj_d = h1_momentum.get("kdj_d")
    price_3bar = h1_behavior.get("price_change_3bar_pct")
    range_atr = h1_behavior.get("three_bar_range_atr_multiple")

    if stoch is not None and stoch_ma is not None:
        if stoch_direction == "yukari" and stoch_vs_ma == "ustunde":
            confirmation = " KDJ de dönüşü destekliyor." if (
                kdj_k is not None and kdj_d is not None and float(kdj_k) > float(kdj_d)
            ) else " KDJ teyidi henüz tam değil."
            if float(stoch) >= 80:
                bullets.append(
                    f"1H StochRSI {stoch:.1f} ile ortalamasının üzerinde yükseliyor; momentum güçlü fakat kısa vadede ısınmış.{confirmation}"
                )
            else:
                bullets.append(
                    f"1H StochRSI {stoch:.1f}, ortalaması {stoch_ma:.1f} üzerine dönüyor; erken momentum toparlanması var.{confirmation}"
                )
        elif stoch_direction == "asagi" and stoch_vs_ma == "altinda":
            sideways = (
                price_3bar is not None and range_atr is not None
                and abs(float(price_3bar)) <= 1.0 and float(range_atr) <= 2.0
            )
            if sideways:
                bullets.append(
                    f"1H StochRSI {stoch:.1f} seviyesine soğuyor; son üç mumda fiyat sınırlı değiştiği için bu henüz yapısal bozulma değil."
                )
            else:
                bullets.append(
                    f"1H StochRSI {stoch:.1f} ile ortalamasının altında geriliyor; kısa vadeli giriş momentumu zayıf."
                )

    obv_up = [
        tf for tf in ("1H", "4H", "1D")
        if coin[tf]["volatility_volume"]["obv_direction_5bar"] == "yukari"
    ]
    if obv_up:
        bullets.append(
            f"OBV {', '.join(obv_up)} görünümünde yükseliyor; alıcı katılımı {len(obv_up)} zaman diliminde fiyatı destekliyor."
        )
    ema_up = [
        tf for tf in ("1H", "4H", "1D")
        if float(coin[tf]["trend_structure"]["close_vs_ema20_pct"] or -999) >= 0
    ]
    if ema_up:
        bullets.append(
            f"Kapanmış mum fiyatı {', '.join(ema_up)} grafiklerinde EMA20 üzerinde; kısa ve orta vadeli yapı tamamen bozulmuş değil."
        )
    macd_up = [
        tf for tf in ("1H", "4H", "1D")
        if coin[tf]["momentum"]["macd_hist_direction"] == "yukari"
    ]
    if macd_up:
        bullets.append(
            f"MACD histogramı {', '.join(macd_up)} görünümünde güçleniyor; momentum bu zaman dilimlerinde yukarı dönüyor."
        )
    if not bullets:
        bullets.append(
            "Ana göstergeler zaman dilimleri arasında ortak bir yön üretmiyor; fiyat seviyeleri daha belirleyici."
        )
    return bullets[:4]


def _hybrid_15m_timing_note(snapshot: dict) -> str:
    """15M yalnızca gün içi giriş zamanlamasını inceltir; ana yönü değiştirmez."""
    momentum = snapshot["timing_15m"]["momentum"]
    stoch_up = (
        momentum.get("stochrsi_direction") == "yukari"
        and momentum.get("stoch_vs_ma") == "ustunde"
    )
    stoch_down = (
        momentum.get("stochrsi_direction") == "asagi"
        and momentum.get("stoch_vs_ma") == "altinda"
    )
    macd_direction = momentum.get("macd_hist_direction")
    if stoch_up and macd_direction == "yukari":
        return "15 dakikalık kapanmış mumlarda StochRSI ve MACD birlikte yukarı dönüyor; giriş zamanlaması güçleniyor."
    if stoch_down and macd_direction == "asagi":
        return "15 dakikalık kapanmış mumlarda StochRSI ve MACD birlikte zayıflıyor; henüz giriş teyidi yok."
    if stoch_up:
        return "15 dakikalık StochRSI erken toparlanıyor ancak MACD teyidi henüz tamamlanmadı."
    if stoch_down:
        return "15 dakikalık StochRSI soğuyor; ana yapı korunuyorsa yeniden yukarı dönüş beklenmeli."
    return "15 dakikalık kapanmış mumlar net bir giriş zamanlaması üretmiyor."

def _hybrid_resistance_close(zone: dict | None, price: float, threshold_pct: float = 0.5) -> bool:
    if not zone or not price:
        return False
    low, high = float(zone["low"]), float(zone["high"])
    if low <= price <= high:
        return True
    boundary = low if price < low else high
    return abs(price - boundary) / price * 100 <= threshold_pct


def _hybrid_validated_plan(plan: dict, zones: dict, price: float) -> dict:
    plan = dict(plan)
    entry = plan.get("entry_type", "none")
    action = plan.get("action", "wait_trigger")
    r1 = zones.get("resistance_1")
    reasons = list(plan.get("reasons") or [])
    if _hybrid_resistance_close(r1, price) and "price_near_resistance" not in reasons:
        reasons.append("price_near_resistance")
    if action == "buy_candidate" and entry == "none":
        action = "wait_trigger"
    if action == "buy_candidate" and plan.get("h1_timing") in {"overheated", "weakening"}:
        action = "wait_trigger"
    if action == "buy_candidate" and _hybrid_resistance_close(r1, price) and entry != "resistance_break":
        action = "wait_trigger"
    plan["action"] = action
    plan["reasons"] = reasons
    return plan



def _hybrid_effective_entry(plan: dict, zones: dict, price: float) -> str:
    """Model belirsiz kalsa da mevcut fiyat konumundan en yakın somut tetik üretilir."""
    entry = plan.get("entry_type", "none")
    r1 = zones.get("resistance_1")
    near = zones.get("near_support")
    if _hybrid_resistance_close(r1, price):
        return "resistance_break"
    if entry != "none":
        return entry
    if near and _hybrid_pct_gap(price, near["high"], "support") <= 1.5:
        return "support_reaction"
    return "momentum_retrigger"



def _hybrid_next_target(zones: dict, price: float, entry: str) -> dict | None:
    """Kırılan/çok yakın direnç hedef yapılmaz; onun üzerindeki ilk gerçek bölge seçilir."""
    candidates = ([zones.get("resistance_2")] if entry == "resistance_break"
                  else [zones.get("resistance_1"), zones.get("resistance_2")])
    valid = [z for z in candidates if z and float(z["low"]) > price]
    if not valid:
        return None
    return min(valid, key=lambda z: float(z["low"]) - price)


def _hybrid_reason_sentence(plan: dict) -> str:
    positive_labels = {
        "aligned_uptrend": "üst zaman dilimlerinin yükselişi desteklemesi",
        "buyer_participation": "alıcı katılımının sürmesi",
        "sideways_cooling": "fiyat fazla gerilemeden momentumun boşalması",
        "controlled_pullback": "geri çekilmenin şimdilik kontrollü kalması",
        "early_retrigger": "saatlik momentumda erken toparlanma görülmesi",
        "btc_supportive": "Bitcoin görünümünün destekleyici olması",
    }
    risk_labels = {
        "price_near_resistance": "fiyatın direnç bölgesinde bulunması",
        "price_far_support": "yakın desteğin mevcut fiyata göre aşağıda kalması",
        "overheated_move": "kısa vadeli hareketin uzamış olması",
        "momentum_weakness": "kısa vadeli momentumun zayıflaması",
        "timeframe_conflict": "zaman dilimlerinin henüz tam uyumlu olmaması",
        "btc_weakness": "Bitcoin'in kısa vadeli baskı oluşturması",
    }
    reasons = plan.get("reasons", [])
    positives = [positive_labels[x] for x in reasons if x in positive_labels][:2]
    risks = [risk_labels[x] for x in reasons if x in risk_labels][:2]

    def joined(items: list[str]) -> str:
        return items[0] if len(items) == 1 else " ve ".join(items)

    if plan.get("action") in {"wait_trigger", "no_buy"} and risks:
        if positives:
            return f"{joined(positives).capitalize()} olumlu; ancak {joined(risks)} nedeniyle yeni alımı aceleye getirmezdim."
        return f"Beklememin temel nedeni {joined(risks)}."
    if positives:
        return f"Bu görüşü {joined(positives)} destekliyor."
    if risks:
        return f"Bu görüşte {joined(risks)} nedeniyle temkinli kalırdım."
    if not positives and not risks:
        return "Kararda fiyatın bulunduğu bölge ile saatlik zamanlamayı birlikte dikkate alırdım."
    return ""



def _hybrid_trade_ideas(plan: dict, zones: dict, price: float, symbol: str) -> str:
    """Gün içi kararını seviye, olası alan ve riskle doğal bir paragrafta açıklar."""
    near = zones.get("near_support")
    r1 = zones.get("resistance_1")
    r2 = zones.get("resistance_2")
    if _hybrid_resistance_close(r1, price):
        sentences = [
            f"{symbol} genel olarak olumlu yapıda olsa da şu an yeni alım için elverişli bir yerde değil; "
            f"fiyat {_hybrid_fmt(r1['low'])}–{_hybrid_fmt(r1['high'])} ilk direnç bölgesinin içinde."
        ]
        upside = (
            _hybrid_pct_gap(price, r2["low"], "resistance")
            if r2 and float(r2["low"]) > price else None
        )
        downside = (
            _hybrid_pct_gap(price, near["high"], "support")
            if near and float(near["high"]) < price else None
        )
        if upside is not None and downside is not None:
            comparison = (
                "Bu nedenle mevcut fiyattan risk/getiri cazip değil."
                if upside <= downside else
                "Yukarı alan geri çekilme mesafesinden büyük olsa da direnç içinden giriş hâlâ teyitsiz."
            )
            sentences.append(
                f"Bir sonraki dirence yaklaşık %{upside:.1f} alan varken yakın desteğe olası geri çekilme "
                f"mesafesi yaklaşık %{downside:.1f}. {comparison}"
            )
        scenarios = []
        scenarios.append(
            f"{_hybrid_fmt(r1['high'])} üzerinde kapanmış 1H mum ve bölgenin korunması alım ihtimalini güçlendirir"
        )
        if near:
            scenarios.append(
                f"{_hybrid_fmt(near['low'])}–{_hybrid_fmt(near['high'])} desteğine kontrollü dönüş ve "
                "bu bölgede satışın durması daha avantajlı bir giriş oluşturur"
            )
        sentences.append("; alternatif olarak ".join(scenarios) + ".")
        if near:
            sentences.append(
                "İlk direnç aşılamazsa kısa vadede yakın desteğe doğru geri çekilme olasılığı artar."
            )
        return " ".join(sentences)

    sentences = []
    if r1 and r1["low"] > price:
        upside = _hybrid_pct_gap(price, r1["low"], "resistance")
        sentences.append(
            f"{symbol} için ilk dirence yaklaşık %{upside:.1f} alan bulunuyor; ancak yeni alımın anlamlı "
            "olması için saatlik momentumun fiyat yapısı ve alıcı katılımıyla birlikte güçlenmesi gerekir."
        )
    else:
        sentences.append(
            f"{symbol} fiyatının üzerinde güvenilir direnç bölgesi oluşmadığı için kesin hedef uydurulmamalı."
        )
    if near:
        sentences.append(
            f"{_hybrid_fmt(near['low'])}–{_hybrid_fmt(near['high'])} yakın desteğine kontrollü dönüş ve "
            "bu bölgede satışın durması daha avantajlı bir giriş senaryosu oluşturabilir."
        )
    return " ".join(sentences)


def _hybrid_render_report(plan: dict, snapshot: dict, zones: dict) -> str:
    price = float(snapshot["live_price"])
    base = snapshot["symbol"]
    plan = _hybrid_validated_plan(plan, zones, price)
    day = {"up": "Günlük ana yapı yukarı eğilimli", "mixed": "Günlük ana yapı karışık", "down": "Günlük ana yapı baskı altında"}[plan["day_regime"]]
    h4 = {"continuation": "4 saatlik görünüm devamı destekliyor", "controlled_pullback": "4 saatlik hareket kontrollü bir düzeltme gösteriyor", "reversal_attempt": "4 saatlik görünüm bir dönüş denemesinde", "distribution": "4 saatlik görünümde alıcı gücü dağılıyor", "breakdown": "4 saatlik yapı aşağı kırılmış görünüyor", "mixed": "4 saatlik görünüm henüz net değil"}[plan["h4_role"]]
    h1 = {"retrigger": "saatlik momentum yeniden yukarı tetikleniyor", "sideways_reset": "saatlik momentum fiyat fazla gerilemeden yatay kalarak soğuyor", "pullback_reset": "saatlik görünüm kontrollü geri çekilme sonrası yeniden güç arıyor", "overheated": "saatlik hareket kısa vadede fazla uzamış", "weakening": "saatlik momentum zayıflıyor", "mixed": "saatlik zamanlama henüz karışık"}[plan["h1_timing"]]
    btc = {"supportive": "Bitcoin görünümü genel piyasa baskısını azaltıyor", "neutral": "Bitcoin belirgin destek veya baskı oluşturmuyor", "caution": "Bitcoin kısa vadeli hareket için ek risk oluşturuyor"}[plan["btc_effect"]]

    action = plan["action"]
    entry = _hybrid_effective_entry(plan, zones, price)
    if action == "buy_candidate":
        opening = "Ben olsam bunu ALIM_ADAYI olarak değerlendirirdim; yine de tek seferde tam büyüklükte girmezdim."
    elif action == "no_buy":
        opening = "Ben olsam mevcut koşullarda yeni alım düşünmezdim."
    else:
        opening = "Ben olsam mevcut fiyattan almaz, uygun giriş koşulu için tetikte beklerdim."

    if entry == "support_reaction" and zones.get("near_support"):
        trigger = "Yakın destekte satışın durması ve saatlik momentumun yeniden yukarı dönmesi giriş koşulum olurdu."
    elif entry == "resistance_break" and zones.get("resistance_1"):
        trigger = "İlk direncin kapanmış saatlik mumla aşılması ve sonrasında bu bölgenin korunması giriş koşulum olurdu."
    elif entry == "momentum_retrigger":
        trigger = "Fiyat yapısı korunurken saatlik öncü göstergelerin yeniden yukarı dönmesi giriş koşulum olurdu."
    else:
        trigger = "Yeni alım için saatlik fiyat hareketi ile alıcı katılımının birlikte güçlenmesini beklerdim."

    if entry == "resistance_break" and zones.get("resistance_1"):
        invalidation = "Kırılım sonrasında fiyat ilk direncin altında yeniden saatlik kapanış yaparsa bu giriş düşüncesinden vazgeçerdim."
    elif zones.get("near_support"):
        invalidation = "Yakın destek kapanmış saatlik mumla kaybedilirse bu kısa vadeli alım düşüncesinden vazgeçerdim."
    else:
        invalidation = "Saatlik ve 4 saatlik yapı birlikte aşağı dönerse alım düşüncesinden vazgeçerdim."

    support_gap = _hybrid_pct_gap(price, zones["near_support"]["high"], "support") if zones.get("near_support") else None
    resistance_gap = _hybrid_pct_gap(price, zones["resistance_1"]["low"], "resistance") if zones.get("resistance_1") else None
    location = []
    if support_gap is not None:
        location.append(f"yakın destek yaklaşık %{support_gap:.1f} aşağıda")
    if resistance_gap is not None:
        resistance_zone = zones.get("resistance_1")
        if resistance_zone and resistance_zone["low"] <= price <= resistance_zone["high"]:
            location.append("fiyat ilk direnç bölgesinin içinde")
        elif resistance_gap < 0.1:
            location.append("ilk direnç hemen üzerinde")
        else:
            location.append(f"ilk direnç yaklaşık %{resistance_gap:.1f} yukarıda")
    location_text = "; ".join(location).capitalize() + "." if location else "Fiyatın yakın bölgelere mesafesi güvenilir biçimde hesaplanamadı."

    timing_note = _hybrid_15m_timing_note(snapshot)
    body = (
        f"🔍 Genel Değerlendirme\n{base} şu anda {_hybrid_fmt(price)} seviyesinde. {location_text} "
        f"{day}; {h4}; {h1}. {btc}.\n\n"
        "📉 Teknik Göstergeler\n" + "\n".join(f"• {x}" for x in _hybrid_indicator_bullets(snapshot)) +
        "\n\n📈 Kritik Seviyeler\n"
        f"• Yakın destek: {_hybrid_zone_text(zones.get('near_support'), price)}\n"
        f"• Sonraki destek: {_hybrid_zone_text(zones.get('next_support'), price)}\n"
        f"• İlk direnç: {_hybrid_zone_text(zones.get('resistance_1'), price)}\n"
        f"• Sonraki direnç: {_hybrid_zone_text(zones.get('resistance_2'), price)}\n\n"
        f"📌 İşlem Fikirleri\n{_hybrid_trade_ideas(plan, zones, price, base)}\n\n"
        f"🌌 Benim Beklentim — Ne Yapardım?\n"
        + (
            f"{opening} Yukarıdaki giriş senaryolarından biri oluşmadan işlem açmazdım. {timing_note}"
            if action == "wait_trigger" else
            f"{opening} {_hybrid_reason_sentence(plan)} {timing_note} {invalidation}"
            if action == "buy_candidate" else
            f"{opening} {_hybrid_reason_sentence(plan)}"
        )
    )
    return body




def analyze_coin_on_demand(symbol: str) -> bool:
    """Thread 38 için, Portfolio sinyalinden bağımsız tek seferlik güncel coin analizi."""
    if MANUAL_ANALYZER_MODE == "v2":
        pair = symbol.replace("/", "").upper()
        base = pair[:-4] if pair.endswith("USDT") else pair
        pair = base + "USDT"
        if not GEMINI_API_KEY:
            send_decision("Ücretsiz manuel analiz için GEMINI_API_KEY bulunamadı; ücretli modele geçilmedi.")
            return False
        try:
            snapshot, zones = _hybrid_market_snapshot(pair)
            model_name = _resolve_manual_v2_model()
            started = time.time()
            plan, usage, prompt_chars = _hybrid_decision_plan(
                snapshot, zones, GEMINI_API_KEY, model_name
            )
            _log_usage(
                "manual_coin_hybrid", model_name, "hybrid-1.2",
                int(usage.get("promptTokenCount") or 0),
                int(usage.get("candidatesTokenCount") or 0),
                time.time() - started, prompt_chars=prompt_chars,
            )
            body = _hybrid_render_report(plan, snapshot, zones)
        except Exception as exc:
            print(f"[MANUEL HYBRID] {pair}: {type(exc).__name__}: {exc}", flush=True)
            send_decision(f"#{html.escape(base)} güncel analizi şu anda oluşturulamadı; daha sonra tekrar dene.")
            return False
        stamp = _tr_now().strftime("%d/%m/%Y %H:%M")
        message = (
            f"🔎 <b>#{html.escape(base)} GÜNCEL GÖRÜNÜM</b>\n🕐 {stamp}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n{html.escape(body)}"
        )
        send_decision(message)
        print(f"[MANUEL HYBRID] {pair}: ücretsiz hibrit analiz gönderildi.", flush=True)
        return True

    pair = symbol.replace("/", "").upper()
    base = pair[:-4] if pair.endswith("USDT") else pair
    pair = base + "USDT"
    display_symbol = base + "/USDT"
    tasks = {
        "coin_15m": (display_symbol, "15m", 240),
        "coin_1h": (display_symbol, "1h", 240), "coin_4h": (display_symbol, "4h", 240),
        "coin_1d": (display_symbol, "1d", 240), "btc_1h": ("BTC/USDT", "1h", 240),
        "btc_4h": ("BTC/USDT", "4h", 240), "btc_1d": ("BTC/USDT", "1d", 240),
    }
    raw = {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        futures = {ex.submit(_fetch_klines, sym, tf, lim): key for key, (sym, tf, lim) in tasks.items()}
        for fut in as_completed(futures):
            try: raw[futures[fut]] = fut.result()
            except Exception: raw[futures[fut]] = None
    if not raw.get("coin_1h") or not raw.get("coin_4h"):
        send_decision(f"#{html.escape(base)} için Binance Spot USDT verisi alınamadı; analiz yapılmadı.")
        print(f"[MANUEL ANALYZER] {pair}: güncel Binance verisi yok, API çağrılmadı.", flush=True)
        return False
    if MANUAL_ANALYZER_MODE not in {"v2", "legacy"}:
        send_decision(
            "Manuel analiz modu geçersiz; güvenlik için API çağrısı yapılmadı. "
            "MANUAL_ANALYZER_MODE yalnız v2 veya legacy olabilir."
        )
        print(
            f"[MANUEL ANALYZER CONFIG ERROR] geçersiz mod={MANUAL_ANALYZER_MODE!r}; API çağrılmadı.",
            flush=True,
        )
        return False
    if MANUAL_ANALYZER_MODE == "v2":
        if not GEMINI_API_KEY:
            send_decision("Manuel analiz v2 için GEMINI_API_KEY bulunamadı; ücretli modele geçiş yapılmadı.")
            return False
        if MANUAL_ANALYZER_V2_RENDERER not in {"controlled", "prose"}:
            send_decision("Manuel analiz V2 renderer ayarı geçersiz; API çağrısı yapılmadı.")
            print(
                f"[MANUEL ANALYZER CONFIG ERROR] geçersiz V2 renderer={MANUAL_ANALYZER_V2_RENDERER!r}",
                flush=True,
            )
            return False
    else:
        if not MANUAL_ANALYZER_ALLOW_PAID_HAIKU:
            send_decision(
                "Ücretli Haiku manuel analizi kilitli; API çağrısı yapılmadı. "
                "Bilinçli geri dönüş için ayrıca MANUAL_ANALYZER_ALLOW_PAID_HAIKU=true gerekir."
            )
            print("[MANUEL ANALYZER LEGACY BLOCKED] ücretli API çağrılmadı.", flush=True)
            return False
        if not ANTHROPIC_API_KEY:
            send_decision("Manuel analiz için ANTHROPIC_API_KEY bulunamadı.")
            return False

    coin_frames = {"1H": raw.get("coin_1h"), "4H": raw.get("coin_4h"), "1D": raw.get("coin_1d")}
    coin = {label: _manual_tf_snapshot(data) for label, data in coin_frames.items()}
    coin_15m = _manual_tf_snapshot(raw.get("coin_15m"))
    btc = {"1H": _manual_tf_snapshot(raw.get("btc_1h")),
           "4H": _manual_tf_snapshot(raw.get("btc_4h")),
           "1D": _manual_tf_snapshot(raw.get("btc_1d"))}
    current_price = coin["1H"]["price"]
    zones = _manual_zones(coin_frames, current_price)
    price_location_note = _manual_price_location_note(zones, current_price)
    fg_val, fg_label = _fear_greed()
    fg_text = f"{fg_val} ({fg_label})" if fg_val is not None else "veri yok"
    technical_block = "\n".join(_manual_tf_text(x, coin[x]) for x in ("1H", "4H", "1D"))
    timing_block = _manual_tf_text("15M", coin_15m, include_candle_structure=True)
    btc_block = "\n".join(_manual_tf_text(x, btc[x], include_price=False) for x in ("1H", "4H", "1D"))
    zone_block = "\n".join([
        f"Yakın destek: {_manual_zone_text(zones['near_support'], current_price)}",
        f"Sonraki destek: {_manual_zone_text(zones['next_support'], current_price)}",
        f"Uzak yapısal destek: {_manual_zone_text(zones['structural_support'], current_price)}",
        f"İlk direnç: {_manual_zone_text(zones['resistance_1'], current_price)}",
        f"Sonraki direnç: {_manual_zone_text(zones['resistance_2'], current_price)}",
    ])
    prompt = f"""Sen yalnızca kullanıcının istediği anda çalışan spot piyasa yardımcısısın.
Bu bir otomatik emir veya kesin al-sat kararı değildir. Kullanıcı 1H, 4H ve 1D hareketlerini birlikte okuyup manuel karar verir.
Göstergelerde sabit eşiklerden çok yön değişimini, fiyatın dinlenme/geri çekilme yapısını ve BTC bağlamını önemse.
Bir koşulu tek başına zorunlu filtre yapma; olumlu ve olumsuz kanıtların ağırlığını birlikte anlat.
15M yalnız "Ben olsam ne yapardım?" bölümündeki giriş zamanlamasını daha yakından gözlemek içindir.
Ne oluyor, Ne anlama geliyor, İzlenecek bölgeler ve Neye dikkat edilmeli bölümlerini 15M'ye göre değiştirme.
15M saatlik, 4 saatlik ve günlük görünümü belirlemez, adayı elemez ve tek başına alım gerekçesi olmaz. Aynı 15M oluşumunu her grafikte aynı
sonuca bağlama; 1H/4H/1D bağlamı, konum, hacim/OBV ve yakın bölgelere göre özgün değerlendir.
Yalnız aşağıdaki güncel verileri kullan. Eski scanner sinyali yoktur. Bölge veya veri uydurma.

[COIN: {base} | GÜNCEL FİYAT: {_fmt(current_price)}]
{technical_block}

[15M YAKIN GÖZLEM — ANA KARAR DEĞİL]
{timing_block}

[BTC BAĞLAMI]
{btc_block}
Fear & Greed: {fg_text}

[HESAPLANAN GÜNCEL BÖLGELER]
{zone_block}

[FİYATIN BÖLGELERE GÖRE KONUMU]
{price_location_note}

Görevin kısa ve anlaşılır bir spot değerlendirmesi üretmektir. İlk bölümleri gereksiz ayrıntıyla uzatma;
asıl muhakeme ve açıklama ağırlığını "Benim Beklentim — Ne Yapardım?" alanına ver. Aynı kanıtı farklı
bölümlerde tekrarlama. Göstergeleri art arda saymak yerine birlikte fiyat açısından ne anlattıklarını açıkla.
Çıktıda 1H/4H/1D/15M kısaltmalarını kullanma. Doğal analiz cümlelerinde "saatlik görünüm", "saatlik OBV",
"4 saatlik yapı", "günlük yön" ve "15 dakikalık grafik" de. "1 saatlik" ve "1 günlük" ifadelerini doğal
cümlelerde kullanma; bunlar yalnız kodun ürettiği bölge etiketlerinde yer alır. "Dört saatlik" yazma; daima
"4 saatlik" kullan.
Teknik bir terim kullanırsan aynı cümlede sade Türkçe anlamını açıkla. Bullish, bearish, long, short, setup, bias,
retest, swing veya confirmation gibi İngilizce işlem dili kullanma. Yalnız spot alım açısından konuş.
"Gövde deformasyonu", "konsolide oluyor", "katılım kalitesi", "tepki adımı" gibi ne yapılacağını açıkça
anlatmayan yapay ifadeler kullanma. "Kontrollü katılım" gibi soyut bir kalıp kullanma; bunun yerine hangi somut
koşulda küçük veya kademeli alımı değerlendireceğini açıkça söyle. "RSI yükseliş yapıyor" yerine "RSI yükseliyor"
gibi doğal konuşma Türkçesi kullan. Aynı Türkçe sözcüğü parantez içinde yeniden açıklama.
"Rüzgâr arkası", "kurtarıcı", "tema", "çerçeve", "mekanik geri çekilme" gibi yapay benzetmeler kullanma.
"Saturasyon", "tepe yaşıyor", "çift zaman dilimi", "konuma alış", "momentum harita", "çizgiler kapanmış"
ve "stabilite" gibi doğal Türkçede anlamı belirsiz kalıplar kullanma. Bir göstergenin yönünü başka bir göstergenin
kesin sonucu gibi sunma; yalnız verilerin birlikte ne anlattığını açıkla.
Her cümlede tek ana düşünceyi tamamla; bozuk veya birbirine eklenmiş uzun cümleler kurma.
XML/HTML etiketi üretme; özellikle <item> veya </item> yazma. Çift olumsuzluk kurma. Williams %R yükseliyorsa
bunu satış baskısının zayıflaması olarak, düşüyorsa satış baskısının güçlenmesi olarak açık ve doğru anlat.
Genel değerlendirme yalnız bir gösterge özeti değildir. Coinin saatlik, 4 saatlik ve günlük yönünü, hareketin normal mi yoksa uzamış mı
olduğunu, hacim veya OBV'nin fiyatı destekleyip desteklemediğini, BTC'nin etkisini ve en önemli kısa vadeli riski
üç-dört doğal cümlede birlikte anlat. Coinin kendisini anlatmadan yalnız OBV veya BTC hakkında iki cümle yazma.

Ana yorum alanlarında yalnız saatlik, 4 saatlik ve günlük verileri kullan. 15 dakikalık veriyi yalnız
"Ben olsam ne yapardım?" eylem planında giriş zamanlamasını açıklamak için kullan. Ana yoruma karıştırma.
Genel değerlendirme, teknik göstergeler ve işlem fikirlerinde "son mum", "mum gövdesi", "son dört kapanış",
"son dört dip" veya "son dört tepe" ayrıntılarını kullanma; bunlar yalnız 15 dakikalık yakın gözlem verisidir.
Sabit gösterge eşikleriyle mekanik karar verme; fiyat yapısını, hareket yönünü, hacim/OBV katılımını, BTC etkisini
ve seviyelere olan konumu birlikte tart. Güçlü trend devam edebilecekse yalnız "beklerdim" deme; küçük veya
kademeli alımın hangi somut durumda düşünülebileceğini de anlat. Hareket uzamış ve alıcı desteği zayıflıyorsa
neden beklemenin daha anlamlı olduğunu açıkça söyle.

Yalnız hesaplanan bölgeleri kullan; yeni fiyat seviyesi uydurma. BTC için fiyat seviyesi verme.
Model alanlarında hiçbir rakamsal fiyat yazma; bölgeler kod tarafından ayrıca eklenecek. Bölgelere yalnız
"yakın destek", "sonraki destek", "ilk direnç" ve "sonraki direnç" adlarıyla gönderme yap.
Uzak yapısal desteği yakın alım bölgesi gibi sunma. Direnç verisi yoksa direnç tahmin etme.
"Yakın destek" adı görecelidir. Fiyat konumu notunda bölge güncel fiyattan belirgin biçimde uzaktaysa onu
yakın giriş alanı gibi anlatma. Mevcut fiyattan küçük alım önereceksen bunun yüksek riskli olduğunu açıkça söyle
ve hareketin uzamasını, EMA uzaklığını, hacmi ve para akışını birlikte gerekçelendir.
Destek bölgelerinin varlığını geri çekilme riskinin olmadığına kanıt sayma; destek yalnızca fiyat gelirse izlenecek
olası tepki alanıdır. Fiyatın desteğe yaklaşmasını tek başına olumsuzluk gibi anlatma; asıl zayıflık desteğin
kaybedilmesi veya satış baskısının güçlenmesidir. "Birinci/ikinci/üçüncü seviye" gibi tanımsız alanlar üretme.
İlk direnç verisi yoksa eylem planında "sonraki direnç", direnç hedefi veya seviyeye dayalı kâr alma yazma.
Beklenti alanında mevcut durumda ne yapacağını, nedenini, hangi koşulda küçük veya kademeli alımı
değerlendireceğini, hangi gelişmede vazgeçeceğini ve direnç varsa kâr alma yaklaşımını sade biçimde anlat.
15 dakikalık gözlemi yalnız bu düşüncenin giriş zamanlamasını netleştiren yardımcı kanıt olarak kullan.
Beklenti alanının ilk cümlesinde bugünkü tavrını net seç: beklemek, yüksek riskli küçük başlangıç yapmak veya
alım düşünmemek. Aynı mevcut koşul için hem hemen alacağını hem de önce bekleyeceğini söyleme. 15 dakikalık
göstergeyi mekanik bir alım kuralına dönüştürme; yalnız seçtiğin tavrı güçlendiren veya zayıflatan kanıt olarak kullan.
Hesaplanan direnç yoksa fiyatın güvenilir bir üst direnç/ hedef bölgesi bulunmayan alanda ilerlediğini açıkça söyle;
uydurma hedef üretme.

Yanıtı serbest metin olarak yazma. Yalnız submit_manual_analysis aracını bir kez çağır ve bütün alanları doldur."""

    analysis_tool = {
        "name": "submit_manual_analysis",
        "description": "Güncel spot görünümünü doğal Türkçe ve ayrı bölümler halinde döndürür.",
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "general_assessment": {
                    "type": "string",
                    "description": (
                        "Üç-dört kısa ve doğal cümle. Coinin saatlik, 4 saatlik ve günlük yönünü, hareketin uzayıp "
                        "uzamadığını, hacim/para "
                        "akışının fiyatı destekleyip desteklemediğini, BTC etkisini ve en önemli kısa vadeli riski "
                        "birlikte özetle. Yalnız OBV veya BTC özeti yazma; gösterge listesi yapma ve 15 dakikalık "
                        "veriden söz etme."
                    ),
                },
                "technical_indicators": {
                    "type": "array",
                    "minItems": 2,
                    "maxItems": 5,
                    "items": {"type": "string"},
                    "description": (
                        "Yalnız karar açısından önemli, birbirinden farklı iki-beş kısa teknik bulgu. RSI, MACD, "
                        "StochRSI, Williams %R, OBV, EMA, hacim ve ATR arasından yalnız anlamlı olanları seç. "
                        "Ham değerleri tekrarlamak yerine fiyat açısından anlamını açıkla. 15 dakikalık veriyi kullanma."
                    ),
                },
                "trade_ideas": {
                    "type": "string",
                    "description": (
                        "İki-üç bağlantılı doğal cümle. Mevcut fiyattan alım, yakın desteğe geri çekilme ve varsa "
                        "direnç kırılımı olasılıklarını karşılaştır. Fikri neyin zayıflatacağını söyle. Liste veya "
                        "kesin emir üretme; 15 dakikalık veriyi kullanma."
                    ),
                },
                "expectation": {
                    "type": "string",
                    "description": (
                        "Raporun en önemli bölümü. Gerektiği kadar ayrıntılı fakat sade, tek doğal paragraf yaz. "
                        "Birinci tekil şahısla; şu anda ne yapacağını, nedenini, olası giriş yaklaşımını, fikrini "
                        "değiştirecek koşulu ve varsa kâr alma yaklaşımını açıkla. Belirsiz biçimde hem alıp hem "
                        "bekleyeceğini söyleme; tercih ettiğin yaklaşımı netleştir. 15 dakikalık mum ve gösterge "
                        "yönlerini yalnız giriş zamanlamasını destekleyen veya zayıflatan yardımcı kanıt olarak "
                        "paragrafın içine kat. İlk cümlede tek bir mevcut tavır seç; beklemeyi seçtiysen sonraki "
                        "cümlede mevcut fiyattan başlangıç alımı önerme. Ayrı 15 dakikalık başlığı veya bağımsız "
                        "sonuç üretme."
                    ),
                },
            },
            "required": ["general_assessment", "technical_indicators", "trade_ideas", "expectation"],
        },
    }
    try:
        if MANUAL_ANALYZER_MODE == "v2":
            resolved_model = _resolve_manual_v2_model()
            print(
                f"[MANUEL ANALYZER V2] {pair}: {resolved_model} | renderer={MANUAL_ANALYZER_V2_RENDERER} başlatıldı.",
                flush=True,
            )
            if MANUAL_ANALYZER_V2_RENDERER == "controlled":
                structured_result = _manual_v2_gemini_plan(
                    base, current_price, technical_block, timing_block, btc_block,
                    zone_block, price_location_note, resolved_model,
                )
                body = _render_manual_v2_controlled(
                    structured_result, zones, base, current_price, coin_15m, coin, btc,
                )
            else:
                structured_result = _manual_v2_gemini_analysis(
                    base, current_price, technical_block, timing_block, btc_block,
                    zone_block, price_location_note, resolved_model,
                )
                body = _render_manual_analysis(
                    structured_result, zones, base, current_price, coin_15m, coin.get("1H"), coin,
                )
            print(f"[MANUEL ANALYZER V2] {pair}: {resolved_model} analizi alındı.", flush=True)
        elif MANUAL_ANALYZER_MODE == "legacy":
            import anthropic
            client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
            started = time.time()
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1500,
                messages=[{"role": "user", "content": prompt}],
                tools=[analysis_tool],
                tool_choice={"type": "tool", "name": "submit_manual_analysis"},
            )
            _log_usage("manual_coin_analysis", "haiku", _PROMPT_V_MANUAL,
                       resp.usage.input_tokens, resp.usage.output_tokens, time.time() - started,
                       prompt_chars=len(prompt))
            tool_block = next(
                (block for block in resp.content
                 if getattr(block, "type", "") == "tool_use"
                 and getattr(block, "name", "") == "submit_manual_analysis"),
                None,
            )
            if tool_block is None or not isinstance(getattr(tool_block, "input", None), dict):
                raise ValueError("Yapılandırılmış manuel analiz alınamadı")
            structured_result = tool_block.input
        if MANUAL_ANALYZER_MODE == "legacy":
            body = _render_manual_analysis(
                structured_result, zones, base, current_price, coin_15m, coin.get("1H"), coin,
            )
    except Exception as exc:
        print(f"[MANUEL ANALYZER {MANUAL_ANALYZER_MODE.upper()}] {pair}: {exc}", flush=True)
        send_decision(f"#{html.escape(base)} güncel analizi şu anda oluşturulamadı; daha sonra tekrar dene.")
        return False
    stamp = _tr_now().strftime("%d/%m/%Y %H:%M")
    message = (f"🔎 <b>#{html.escape(base)} GÜNCEL GÖRÜNÜM</b>\n🕐 {stamp}\n"
               f"━━━━━━━━━━━━━━━━━━━━\n{html.escape(body)}")
    send_decision(message)
    print(f"[MANUEL ANALYZER] {pair}: güncel analiz thread 38'e gönderildi.", flush=True)
    return True


def _build_sig_data(signal: dict) -> str:
    sig_type = signal.get("type", "")
    if sig_type == "pump":
        vr15  = signal.get("vr15")  or signal.get("spike_ratio")
        vr1h  = signal.get("vr1h")  or signal.get("vol_ratio")
        roc   = signal.get("roc_4h") or signal.get("roc_pct")
        ret15 = signal.get("ret15", 0)
        parts = []
        if vr15  is not None: parts.append(f"15m Spike: {float(vr15):.1f}x (eşik ≥15x ✅)")
        if vr1h  is not None: parts.append(f"1h Hacim: {float(vr1h):.1f}x (eşik ≥5x ✅)")
        if roc   is not None: parts.append(f"4h ROC: +%{float(roc):.1f} (eşik ≥%24 ✅)")
        if ret15:              parts.append(f"15m Getiri: +%{float(ret15):.1f}")
        parts.append("Tüm filtreler geçildi")
        return " | ".join(parts)
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
    if sig_type == "rocket":
        return (f"24s değişim: +%{signal.get('change_24h',0):.1f} | "
                f"ADX: {signal.get('adx',0):.1f} | "
                f"DI+: {signal.get('di_plus',0):.1f} / DI-: {signal.get('di_minus',0):.1f} | "
                f"Hacim: {signal.get('vol_ratio',0):.2f}x")
    # Bilinmeyen tip — tüm alanları yaz
    skip = {"symbol","type","entry","stop","tp1","tp2","tp3","source","_internal"}
    return " | ".join(f"{k}:{v}" for k, v in signal.items() if k not in skip)

def evaluate(signal: dict, recent_count: int = 0) -> tuple[str, dict]:
    """
    Sinyali Claude ile değerlendirir, (karar_metni, koşullar_dict) döndürür.
    recent_count: son 1 saatte kaç sinyal geldi (clustering bağlamı için)
    """
    if not ANTHROPIC_API_KEY:
        return "", {}

    import anthropic

    symbol   = signal.get("symbol", "")
    sig_type = signal.get("type", "unknown")
    source   = signal.get("source", "bot")

    # Tüm verileri paralel çek
    with ThreadPoolExecutor(max_workers=7) as ex:
        fut_tf    = ex.submit(_fetch_all_tf, symbol)
        fut_fg    = ex.submit(_fear_greed)
        fut_dom   = ex.submit(_dominance)
        fut_macro = ex.submit(_fetch_btc_macro)
        fut_tma   = ex.submit(_tma_3d_btc)
        fut_sweep = ex.submit(_liquidity_sweep, symbol)
        fut_etf   = ex.submit(_etf_flow)
    tf_data          = fut_tf.result()
    fg_val, fg_label = fut_fg.result()
    dom              = fut_dom.result()
    macro            = fut_macro.result()
    tma              = fut_tma.result()
    sweep            = fut_sweep.result()
    etf_str          = fut_etf.result()
    coin_hist, sys_hist = _portfolio_context(symbol, sig_type)

    # Piyasa koşulları — arşiv eşleştirmesi için
    btc_4h   = tf_data.get("btc_4h") or {}
    coin_1h  = tf_data.get("coin_1h") or {}
    btc_price = btc_4h.get("close")
    conditions = {
        "fg":              fg_val,
        "fg_label":        fg_label,
        "btc_4h_rsi":      btc_4h.get("rsi"),
        "btc_above_ema50": (btc_4h.get("close", 0) > btc_4h.get("ema50", 0))
                           if btc_4h.get("ema50") else None,
        "dominance":       dom.get("current") if dom else None,
        "dom_trend":       dom.get("trend_dir") if dom else None,
        "tma_trend":       tma.get("trend") if tma else None,
        "adx_1h":          coin_1h.get("adx"),
    }
    archive_ctx = _archive_condition_context(conditions)

    coin_block = "\n".join([
        _tf_line("1S",  tf_data.get("coin_1h")),
        _tf_line("4S",  tf_data.get("coin_4h")),
        _tf_line("1G",  tf_data.get("coin_1d")),
    ])
    btc_block = "\n".join([
        _tf_line("1S",  tf_data.get("btc_1h")),
        _tf_line("4S",  tf_data.get("btc_4h")),
    ])
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
        sweep_str = ""

    # PANİK PUMP için ADX kalite notu (backtest: ADX≥40+drop≤-8% → WR%92)
    adx_note = ""
    if sig_type == "capit":
        adx_val  = coin_1h.get("adx")
        ret1_val = float(signal.get("ret1") or 0)
        if adx_val is not None:
            if adx_val >= 40 and ret1_val <= -8:
                adx_note = f"\nADX KALİTE: {adx_val:.0f} ✅ Güçlü trend + derin düşüş — backtest WR %92 segmenti"
            elif adx_val >= 40:
                adx_note = f"\nADX KALİTE: {adx_val:.0f} ✅ Güçlü trend — backtest WR %74+ segmenti"
            elif adx_val >= 25:
                adx_note = f"\nADX KALİTE: {adx_val:.0f} 🟡 Orta güç — backtest WR %57 segmenti"
            else:
                adx_note = f"\nADX KALİTE: {adx_val:.0f} ⚠️ Zayıf trend — backtest WR %50 segmenti"

    prompt = f"""Bu sinyal otomatik teknik filtrelerden geçti. Görevin, filtrelerin görmediği ek piyasa risklerini tespit etmek — teknik koşullar zaten karşılandı.
Somut bir neden olmadan RİSKLİ deme. Aynı şekilde, gerekçende referans verebileceğin SOMUT bir veri noktası
(belirli bir gösterge değeri, haber, likidite sweep, dominans/ETF sinyali, aşırı alım/satım vb.) yoksa DİKKAT de
verme — "piyasa her zaman belirsizdir" gibi genel bir gerekçe DİKKAT için yeterli değildir, bu durumda GİR ver.
DİKKAT sadece somut bir risk işaretine dayanıyorsa anlamlıdır.

[SİNYAL]
Kaynak: {_SOURCE_NAMES.get(source, source)}
Coin: #{symbol.replace('/USDT','')} | {_TYPE_NAMES.get(sig_type, sig_type)}
Giriş: {_fmt(signal.get('entry'))} | Stop: {_fmt(signal.get('stop'))} | TP1: {_fmt(signal.get('tp1'))}
{_build_sig_data(signal)}{adx_note}

[KOİN — ÇOKLU ZAMAN DİLİMİ]
{coin_block}

[BTC — ÇOKLU ZAMAN DİLİMİ]
{btc_block}

[BTC MAKRO — UZUN VADE]
{macro_block if macro_block else "veri yok"}{tma_str}

[MARKET]
Fear & Greed: {fg_str}
{_dom_str(dom)}
BTC ETF Akış: {etf_str if etf_str else "veri yok"}
Sinyal clustering: {cluster_str}

[BU COİN GEÇMİŞİ]
{coin_hist}

[SİSTEM GENEL PERFORMANS — SMC HARİCİ]
{sys_hist}

{f"[GEÇMİŞ PIYASA KOŞUL ARŞİVİ]{chr(10)}{archive_ctx}" if archive_ctx else ""}{sweep_str}

Haftalık ve 3 günlük yapıya önce bak, sonra anlık sinyali değerlendir.
Geçmiş istatistikler sadece bağlamdır — anlık koşullar esastır.

KARAR: [✅ GİR — piyasa koşulları uygun / ⚠️ DİKKAT — belirli risk var / 🚫 RİSKLİ — somut piyasa engeli]
GEREKÇE: (2-3 cümle — somut veri referansı ver)
UYARI: (varsa 1 cümle, yoksa yazma)"""

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            _t0 = time.time()
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=350,
                messages=[{"role": "user", "content": prompt}],
            )
            _log_usage("claude_analyzer", "haiku", _PROMPT_V_SIGNAL,
                       resp.usage.input_tokens, resp.usage.output_tokens, time.time() - _t0,
                       prompt_chars=len(prompt))
            return resp.content[0].text.strip(), conditions
        except Exception as e:
            print(f"[ANALYZER CLAUDE] deneme {attempt}/{max_attempts}: {e}", flush=True)
            if attempt < max_attempts:
                time.sleep(2 * attempt)
    return "", conditions

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
    headers = {"Content-Type": "application/json"}
    if PORTFOLIO_TOKEN:
        headers["Authorization"] = f"Bearer {PORTFOLIO_TOKEN}"
    safe_id = portfolio_id.replace("/", "_")
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            r = requests.patch(
                f"{PORTFOLIO_URL}/api/signal/{safe_id}/analyzer",
                json={"analyzer_decision": verdict},
                headers=headers, timeout=5,
            )
            if r.status_code == 200:
                return
            print(f"[ANALYZER] Portfolio güncelleme başarısız (deneme {attempt}/{max_attempts}): "
                  f"{r.status_code} {r.text[:80]}", flush=True)
        except Exception as e:
            print(f"[ANALYZER] Portfolio güncelleme hatası (deneme {attempt}/{max_attempts}): {e}", flush=True)
        if attempt < max_attempts:
            time.sleep(2 * attempt)

def process_and_send(signal: dict, recent_count: int = 0, sig_num: int = 0, portfolio_id: str = ""):
    """
    Sinyali değerlendir ve kararı Analyzer Telegram botuna gönder.

    Herhangi bir sistemden çağrılabilir:
        from claude_analyzer import process_and_send
        process_and_send(signal_dict, recent_count=1, sig_num=42, portfolio_id="SYM_123")

    signal dict zorunlu alanlar: symbol, type, entry, stop, tp1
    Opsiyonel: source ("bot" veya "smc"), tp2, tp3, sistem-spesifik metrikler
    """
    decision, conditions = evaluate(signal, recent_count)
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

    verdict = _extract_verdict(decision)
    verdict_line = f"<b>{verdict}</b>\n━━━━━━━━━━━━━━━━━━━━\n" if verdict else ""

    msg = (
        f"{source_icon} <b>ANALİZ — #{symbol.replace('/USDT','')} [{type_short}]{num_str}</b>\n"
        f"🕐 {tr_time.strftime('%d/%m/%Y %H:%M')}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💵 Giriş: {_fmt(signal.get('entry'))}  "
        f"🛡️ Stop: {_fmt(signal.get('stop'))}  "
        f"🎯 TP1: {_fmt(signal.get('tp1'))}{tp2_str}{tp3_str}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{verdict_line}"
        f"{decision}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>🤖🐾 ANTON🐾 · {src_str}{num_str}</i>"
    )

    send_decision(msg)
    _update_portfolio_analyzer(portfolio_id, verdict)
    if portfolio_id:
        _archive_add_entry(portfolio_id, signal, conditions, verdict or decision[:20])
    print(f"[ANALYZER] #{symbol} kararı gönderildi ({source}){f' → {verdict}' if verdict else ''}", flush=True)


# ============================================================
# PERİYODİK PİYASA İZLEME
# ============================================================
_watcher_state: dict = {
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
        _t0 = time.time()
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=750,
            messages=[{"role": "user", "content": prompt}],
        )
        _log_usage("market_watcher", "haiku", _PROMPT_V_WATCHER,
                   resp.usage.input_tokens, resp.usage.output_tokens, time.time() - _t0,
                   prompt_chars=len(prompt))
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
            f"<i>🤖🐾 ANTON🐾 · Piyasa İzleme</i>"
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
    print("[WATCHER] Başlatıldı — 4h değişim kontrolü aktif.", flush=True)
    while True:
        try:
            now_ts = time.time()
            st     = _watcher_state

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
