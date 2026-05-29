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
TELEGRAM_CHAT_ID        = os.getenv("TELEGRAM_CHAT_ID",        "")
PORTFOLIO_URL           = os.getenv("PORTFOLIO_URL",           "")
PORTFOLIO_TOKEN         = os.getenv("PORTFOLIO_TOKEN",         "")

TR_TZ = timezone(timedelta(hours=3))

def _tr_now():
    return datetime.now(timezone.utc).astimezone(TR_TZ)

# ============================================================
# TELEGRAM
# ============================================================
def send_decision(text: str):
    token = ANALYZER_TELEGRAM_TOKEN
    if not token or not TELEGRAM_CHAT_ID:
        print("[ANALYZER] Token veya chat_id eksik, mesaj gönderilemedi.", flush=True)
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text,
                  "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=10,
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
    with ThreadPoolExecutor(max_workers=3) as ex:
        fut_tf   = ex.submit(_fetch_all_tf, symbol)
        fut_fg   = ex.submit(_fear_greed)
        fut_dom  = ex.submit(_dominance)
    tf_data          = fut_tf.result()
    fg_val, fg_label = fut_fg.result()
    dom              = fut_dom.result()
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

    fg_str = f"{fg_val} ({fg_label})" if fg_val is not None else "bilinmiyor"

    if recent_count >= 3:
        cluster_str = f"⚠️ Son 1 saatte {recent_count} coin sinyal verdi — piyasa geneli baskı!"
    elif recent_count >= 2:
        cluster_str = f"🟡 Son 1 saatte {recent_count} sinyal — dikkatli ol."
    else:
        cluster_str = "Normal (tek sinyal)"

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

[MARKET]
Fear & Greed: {fg_str}
{_dom_str(dom)}
Sinyal clustering: {cluster_str}

[BU COİN GEÇMİŞİ]
{coin_hist}

[SİSTEM GENEL PERFORMANS — SMC HARİCİ]
{sys_hist}

RSI, EMA, hacim, trend uyumu ve momentum verilerini birlikte değerlendir.
Geçmiş istatistikler sadece bağlamdır — anlık koşullar esastır.

KARAR: [✅ GİR / ⚠️ DİKKAT / 🚫 RİSKLİ]
GEREKÇE: (2-3 cümle — somut veri referansı ver)
UYARI: (varsa 1 cümle, yoksa yazma)"""

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        resp   = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text.strip()
    except Exception as e:
        print(f"[ANALYZER CLAUDE] {e}", flush=True)
        return ""

# ============================================================
# ANA GİRİŞ NOKTASI
# ============================================================
def process_and_send(signal: dict, recent_count: int = 0, sig_num: int = 0):
    """
    Sinyali değerlendir ve kararı Analyzer Telegram botuna gönder.

    Herhangi bir sistemden çağrılabilir:
        from claude_analyzer import process_and_send
        process_and_send(signal_dict, recent_count=1, sig_num=42)

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
    print(f"[ANALYZER] #{symbol} kararı gönderildi ({source})", flush=True)
