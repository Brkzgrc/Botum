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

from api_logger import log_usage as _log_usage
_PROMPT_V_SIGNAL  = "1.1"   # sinyal değerlendirme prompt versiyonu
_PROMPT_V_WATCHER = "1.0"   # market watcher prompt versiyonu
_PROMPT_V_MANUAL  = "1.0"   # kullanıcı isteğiyle güncel coin görünümü
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
    return {
        "price": price,
        "rsi": round(float(rsi.iloc[-1]), 1) if pd.notna(rsi.iloc[-1]) else None,
        "rsi_direction": _direction(rsi.tolist(), epsilon=0.4),
        "macd_position": "pozitif" if macd.iloc[-1] >= 0 else "negatif",
        "macd_hist_direction": _direction(macd_hist.tolist()),
        "macd_cross": "üstünde" if macd.iloc[-1] >= macd_signal.iloc[-1] else "altında",
        "stoch_rsi": round(float(stoch_k.iloc[-1]), 1) if pd.notna(stoch_k.iloc[-1]) else None,
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

def _manual_zone_text(zone: dict | None) -> str:
    if not zone:
        return "veriyle güvenilir bölge oluşmadı"
    tfs = "/".join(sorted(zone["tfs"]))
    return f"{_fmt(zone['low'])}–{_fmt(zone['high'])} ({tfs})"


def _clean_manual_analysis(text: str) -> str:
    """Model talimata rağmen Markdown/İngilizce kalıntısı üretirse Telegram öncesi temizle."""
    cleaned = (text or "").replace("\\*", "").replace("**", "").replace("__", "")
    cleaned = cleaned.replace("bounceback", "yukarı tepki").replace("bounce back", "yukarı tepki")
    cleaned = cleaned.replace("MACD histogram ufuklaşması", "MACD histogramının yataylaşması")
    return cleaned.strip()


def _manual_tf_text(label: str, snap: dict | None) -> str:
    if not snap:
        return f"{label}: veri yok"
    return (
        f"{label}: fiyat={_fmt(snap['price'])}; RSI={snap['rsi']} ve {snap['rsi_direction']}; "
        f"MACD={snap['macd_position']}, histogram {snap['macd_hist_direction']}, çizgi sinyalin {snap['macd_cross']}; "
        f"StochRSI={snap['stoch_rsi']}, {snap['stoch_direction']}, K çizgisi D'nin {snap['stoch_cross']}; "
        f"OBV {snap['obv_direction']}; Williams%R={snap['willr']} ve {snap['willr_direction']}; "
        f"fiyat EMA20'nin {snap['ema20_relation']} ({snap['ema20_distance_atr']} ATR), "
        f"EMA20 {snap['ema20_direction']}, EMA dizilimi {snap['ema_order']}; hacim {snap['vol_ratio']}x"
    )


def analyze_coin_on_demand(symbol: str) -> bool:
    """Thread 38 için, Portfolio sinyalinden bağımsız tek seferlik güncel coin analizi."""
    pair = symbol.replace("/", "").upper()
    base = pair[:-4] if pair.endswith("USDT") else pair
    pair = base + "USDT"
    display_symbol = base + "/USDT"
    tasks = {
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
    if not ANTHROPIC_API_KEY:
        send_decision("Manuel analiz için ANTHROPIC_API_KEY bulunamadı.")
        return False

    coin_frames = {"1H": raw.get("coin_1h"), "4H": raw.get("coin_4h"), "1D": raw.get("coin_1d")}
    coin = {label: _manual_tf_snapshot(data) for label, data in coin_frames.items()}
    btc = {"1H": _manual_tf_snapshot(raw.get("btc_1h")),
           "4H": _manual_tf_snapshot(raw.get("btc_4h")),
           "1D": _manual_tf_snapshot(raw.get("btc_1d"))}
    current_price = coin["1H"]["price"]
    zones = _manual_zones(coin_frames, current_price)
    fg_val, fg_label = _fear_greed()
    fg_text = f"{fg_val} ({fg_label})" if fg_val is not None else "veri yok"
    technical_block = "\n".join(_manual_tf_text(x, coin[x]) for x in ("1H", "4H", "1D"))
    btc_block = "\n".join(_manual_tf_text(x, btc[x]) for x in ("1H", "4H", "1D"))
    zone_block = "\n".join([
        f"Yakın destek: {_manual_zone_text(zones['near_support'])}",
        f"Sonraki destek: {_manual_zone_text(zones['next_support'])}",
        f"Uzak yapısal destek: {_manual_zone_text(zones['structural_support'])}",
        f"İlk direnç: {_manual_zone_text(zones['resistance_1'])}",
        f"Sonraki direnç: {_manual_zone_text(zones['resistance_2'])}",
    ])
    prompt = f"""Sen yalnızca kullanıcının istediği anda çalışan spot piyasa yardımcısısın.
Bu bir otomatik emir veya kesin al-sat kararı değildir. Kullanıcı 1H, 4H ve 1D hareketlerini birlikte okuyup manuel karar verir.
Göstergelerde sabit eşiklerden çok yön değişimini, fiyatın dinlenme/geri çekilme yapısını ve BTC bağlamını önemse.
Bir koşulu tek başına zorunlu filtre yapma; olumlu ve olumsuz kanıtların ağırlığını birlikte anlat.
Yalnız aşağıdaki güncel verileri kullan. Eski scanner sinyali yoktur. Bölge veya veri uydurma.

[COIN: {base} | GÜNCEL FİYAT: {_fmt(current_price)}]
{technical_block}

[BTC BAĞLAMI]
{btc_block}
Fear & Greed: {fg_text}

[HESAPLANAN GÜNCEL BÖLGELER]
{zone_block}

Türkçe, sade ve kısa yaz. İngilizce kelime, K/D kısaltması veya "ufuklaşma" gibi doğal olmayan ifade kullanma.
StochRSI çizgilerini gerekiyorsa "hızlı çizgi/yavaş çizgi" diye anlat. GİR/DİKKAT/RİSKLİ etiketi kullanma.
RSI'nın sayısal seviyesini merkeze alma; göstergelerin yönü ve fiyat hareketi önceliklidir.
Uzak yapısal desteği güncel giriş bölgesi gibi sunma. İlk veya sonraki direnç verisi yoksa kesinlikle seviye tahmin etme;
aynen "veriyle güvenilir bölge oluşmadı" yaz.
Çıktı biçimi tam olarak şu olsun; Markdown işareti kullanma:

Ne oluyor?
2-3 cümle.

Ne anlama geliyor?
2-3 cümle; mevcut fiyattan kovalamak mı yoksa bölge/dönüş beklemek mi daha anlamlı açıkla.

İzlenecek bölgeler
• Yakın destek: verilen bölge
• Sonraki destek: verilen bölge
• Uzak yapısal destek: verilen bölge; güncel fiyattan uzaksa bunu açıkça belirt
• İlk direnç: verilen bölge
• Direnç aşılırsa: verilen sonraki bölge

Neye dikkat edilmeli?
1-2 cümle; görünümü hangi fiyat kapanışı veya BTC hareketinin zayıflatacağını koşullu anlat.
Kısa vadeli değerlendirmede direnç teyidi için 1H veya gerekirse 4H kapanış/retest kullan; 1D kapanışı isteme.

Ben olsam ne yapardım?
En fazla 3 kısa cümle. Kesin emir verme. Şu olursa beklerdim / şu bölgede şu teyidi arardım / şu durumda uzak dururdum şeklinde uygulanabilir kişisel senaryo yaz."""
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        started = time.time()
        resp = client.messages.create(model="claude-haiku-4-5-20251001", max_tokens=650,
                                      messages=[{"role": "user", "content": prompt}])
        _log_usage("manual_coin_analysis", "haiku", _PROMPT_V_MANUAL,
                   resp.usage.input_tokens, resp.usage.output_tokens, time.time() - started,
                   prompt_chars=len(prompt))
        body = _clean_manual_analysis(resp.content[0].text)
    except Exception as exc:
        print(f"[MANUEL ANALYZER CLAUDE] {pair}: {exc}", flush=True)
        send_decision(f"#{html.escape(base)} güncel analizi şu anda oluşturulamadı; daha sonra tekrar dene.")
        return False
    stamp = _tr_now().strftime("%d/%m/%Y %H:%M")
    message = (f"🔎 <b>#{html.escape(base)} GÜNCEL GÖRÜNÜM</b>\n🕐 {stamp}\n"
               f"━━━━━━━━━━━━━━━━━━━━\n{html.escape(body)}\n━━━━━━━━━━━━━━━━━━━━\n"
               "<i>Manuel inceleme içindir; otomatik emir değildir.</i>")
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
