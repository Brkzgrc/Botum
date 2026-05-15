# -*- coding: utf-8 -*-
"""
╔══════════════════════════════════════════════════════════════════════════════╗
║              KAPİTÜLASYON BARSI SİNYAL BOTU  v5.0                         ║
╠══════════════════════════════════════════════════════════════════════════════╣
║                                                                              ║
║  SİNYAL KRİTERLERİ (Backtest: 2022-2026, 50 coin, 4 yıl)                  ║
║  ─────────────────────────────────────────────────────────                  ║
║                                                                              ║
║  ZORUNLU KOŞUL 1: CRASH BARSI                                               ║
║    Son kapanan 1H barda fiyat -7% ile -15% arasında düşmüş olmalı          ║
║    (-15% altındaki düşüşler genellikle hack/delist = geç)                   ║
║    (-7% altında WR %83.7, -5% altında WR %67.5)                            ║
║                                                                              ║
║  ZORUNLU KOŞUL 2: HACİM SPİKE                                               ║
║    O barın hacmi 20-bar ortalamasının 1.5x - 3x arasında olmalı            ║
║    (1.5x altı = normal hacim, gerçek kapitülasyon değil)                    ║
║    (3x üstü = çok sert çöküş, toparlanma gecikmeli olabilir)               ║
║                                                                              ║
║  MANTIK: "Zayıf eller panik satar, güçlü eller alır"                        ║
║    Büyük hızlı düşüş + ortalamanın üzerinde hacim = kapitülasyon barı      ║
║    Bu bar kapanınca pozisyon alınır, 24H içerisinde toparlanma beklenir     ║
║                                                                              ║
║  BACKTEST SONUÇLARI:                                                         ║
║    ret1 < -7%  + vol 1.5-3x : 245 sinyal, WR %83.7, ort kazanç +15.9%     ║
║    ret1 < -8%  + vol 1.5-3x : 131 sinyal, WR %87.8, ort kazanç +17.7%     ║
║    ret1 < -10% + vol 1.5-3x :  42 sinyal, WR %88.1, ort kazanç +23.2%     ║
║    Yıl bazında istikrar: 2022=%61.9, 2023=%63.2, 2024=%74.4, 2025=%68.3   ║
║                                                                              ║
║  HEDEF VE STOP:                                                              ║
║    TP1: +5%   (geniş kitle için hızlı çıkış)                                ║
║    TP2: +10%  (ana hedef)                                                   ║
║    TP3: +15%  (uzatmak isteyenler için)                                      ║
║    SL : -7%   (pump oncesi max drawdown analizinden — %95 pump korunur)     ║
║                                                                              ║
║  ATLANACAK DURUMLAR:                                                         ║
║    - Stablecoin, leveraged token, fiat pariteleri                            ║
║    - Vol > 3x (çok sert çöküş, toparlanma gecikmeli)                        ║
║    - Aynı coinde 4 saat içinde ikinci sinyal (tekrar sayma)                 ║
║                                                                              ║
║  v4'ten taşınanlar: ApiGate, sembol havuzu, WebSocket altyapısı,            ║
║    Telegram/Portfolio gönderim, Flask dashboard, performans log,             ║
║    Adaptive ATR stop, mum formasyonları, BTC 4H trend, funding cache        ║
║                                                                              ║
║  20260514 — v5: Kapitülasyon sinyaline evrildi                              ║
║             Pump sinyali (RSI+MACD+24H mom) tamamen kaldırıldı             ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

import asyncio
import json
import os
import threading
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import ccxt
import numpy as np
import pandas as pd
import requests
import websockets
from flask import Flask

# ============================================================
# AYARLAR
# ============================================================
BINANCE_API_KEY    = os.getenv("BINANCE_API_KEY",    "")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")
TELEGRAM_TOKEN     = os.getenv("TELEGRAM_TOKEN",     "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID",   "")
PORTFOLIO_URL      = os.getenv("PORTFOLIO_URL",      "")
PORTFOLIO_TOKEN    = os.getenv("PORTFOLIO_TOKEN",    "")

# Kapitülasyon sinyal parametreleri (backtestten türetildi)
CRASH_MIN    = float(os.getenv("CRASH_MIN",    "-15.0"))  # daha sert düşüşleri atla (hack/delist riski)
CRASH_MAX    = float(os.getenv("CRASH_MAX",    "-7.0"))   # minimum crash büyüklüğü
VOL_MIN      = float(os.getenv("VOL_MIN",      "1.5"))    # minimum hacim çarpanı
VOL_MAX      = float(os.getenv("VOL_MAX",      "3.0"))    # maksimum hacim çarpanı
VOL_PERIOD   = int(os.getenv("VOL_PERIOD",     "20"))     # hacim ortalaması kaç bar üzerinden

# Genel
MIN_LIQUIDITY         = float(os.getenv("MIN_LIQUIDITY",         "1000000"))
MAX_SYMBOLS           = int(os.getenv("MAX_SYMBOLS",             "0"))
SIGNAL_COOLDOWN_HOURS = int(os.getenv("SIGNAL_COOLDOWN_HOURS",   "4"))
WS_STREAM_CHUNK       = int(os.getenv("WS_STREAM_CHUNK",         "120"))
BOOTSTRAP_BARS        = int(os.getenv("BOOTSTRAP_BARS",          "500"))
KEEP_BARS             = int(os.getenv("KEEP_BARS",               "300"))

TR_TZ = timezone(timedelta(hours=3))

IGNORED_COINS = {
    "UP/USDT", "DOWN/USDT", "BEAR/USDT", "BULL/USDT",
    "USDC/USDT", "TUSD/USDT", "FDUSD/USDT", "DAI/USDT", "USDP/USDT",
    "USDE/USDT", "UST/USDT", "USD/USDT", "XUSD/USDT", "USD1/USDT", "BFUSD/USDT",
    "USTC/USDT", "BUSD/USDT", "FRAX/USDT", "LUSD/USDT", "GUSD/USDT", "SUSD/USDT",
    "USDS/USDT", "USDX/USDT", "USDD/USDT", "CUSD/USDT", "OUSD/USDT", "MUSD/USDT",
    "U/USDT",
    "EUR/USDT", "TRY/USDT", "GBP/USDT", "BRL/USDT", "RUB/USDT",
    "AUD/USDT", "BIDR/USDT", "IDRT/USDT", "VAI/USDT",
    "PAXG/USDT", "XAUT/USDT",
    "WBTC/USDT", "WETH/USDT", "WBNB/USDT", "BETH/USDT",
    "BTCB/USDT", "HBTC/USDT",
    # BTC kasıtlı çıkarıldı — +7% düşüş çok nadir ve toparlanma yavaş
    "BTC/USDT",
}
LEVERAGED_PATTERNS = ["UP", "DOWN", "BULL", "BEAR", "3L", "3S", "2L", "2S", "5L", "5S", "10L", "10S"]

# ============================================================
# GLOBAL DURUM
# ============================================================
stats           = Counter()
ws_1h_closes    = 0
tracked_symbols = []
signal_counter  = 0

bars_1h:        dict = {}
funding_cache:  dict = {}
last_signal_ts: dict = {}
all_signals:    list = []
btc_4h_cache:   dict = {"trend": "?", "ema50": None, "close": None, "updated": None}
heartbeat = {"last": "", "epoch": time.time(), "symbol": "?"}
bot_status = {"status": "BOOT"}

def tr_now():
    return datetime.now(timezone.utc).astimezone(TR_TZ)

def tr_now_str():
    return tr_now().strftime("%Y-%m-%d %H:%M:%S")

# ============================================================
# API GATE
# ============================================================
class ApiGate:
    def __init__(self, min_interval=0.25, max_concurrent=3, max_retries=6):
        self.min_interval = min_interval
        self.sem          = asyncio.Semaphore(max_concurrent)
        self.max_retries  = max_retries
        self._lock        = asyncio.Lock()
        self._last_ts     = 0.0

    async def _space(self):
        async with self._lock:
            wait = (self._last_ts + self.min_interval) - time.time()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_ts = time.time()

    async def call(self, fn, *args, **kwargs):
        async with self.sem:
            for attempt in range(self.max_retries):
                try:
                    await self._space()
                    loop = asyncio.get_running_loop()
                    return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))
                except (ccxt.RateLimitExceeded, ccxt.DDoSProtection):
                    await asyncio.sleep(min(30.0, 1.0 * (2 ** attempt)))
                except (ccxt.NetworkError, ccxt.RequestTimeout, ccxt.ExchangeNotAvailable):
                    await asyncio.sleep(min(20.0, 0.8 * (2 ** attempt)))
                except Exception:
                    await asyncio.sleep(min(8.0, 0.5 * (2 ** attempt)))
            raise RuntimeError("API call failed after retries")

api_gate = ApiGate()

exchange_spot = ccxt.binance({
    "apiKey":  BINANCE_API_KEY    or None,
    "secret":  BINANCE_API_SECRET or None,
    "options": {"defaultType": "spot", "adjustForTimeDifference": True},
    "enableRateLimit": True,
    "timeout": 15000,
})
exchange_fut = ccxt.binance({
    "options": {"defaultType": "future", "adjustForTimeDifference": True},
    "enableRateLimit": True,
    "timeout": 15000,
})

# ============================================================
# SEMBOL HAVUZU
# ============================================================
async def load_symbols_pool():
    await api_gate.call(exchange_spot.load_markets)
    syms = [
        s for s, m in exchange_spot.markets.items()
        if s.endswith("/USDT")
        and m.get("active") and m.get("spot")
        and s not in IGNORED_COINS
        and not any(s.replace("/USDT", "").endswith(p) for p in LEVERAGED_PATTERNS)
    ]

    volumes = {}
    for i in range(0, len(syms), 100):
        try:
            res = await api_gate.call(exchange_spot.fetch_tickers, syms[i:i+100])
            for k, v in (res or {}).items():
                vol = float(v.get("quoteVolume", 0) or 0)
                if vol > 0:
                    volumes[k] = vol
        except Exception:
            pass
        await asyncio.sleep(0.5)

    missing = [s for s in syms if volumes.get(s, 0) == 0]
    for sym in missing[:50]:
        try:
            ticker = await api_gate.call(exchange_spot.fetch_ticker, sym)
            vol = float(ticker.get("quoteVolume", 0) or 0)
            if vol > 0:
                volumes[sym] = vol
        except Exception:
            pass
        await asyncio.sleep(0.05)

    filtered = sorted(
        [s for s in syms if volumes.get(s, 0) >= MIN_LIQUIDITY],
        key=lambda x: volumes.get(x, 0), reverse=True
    )
    print(f"Sembol: {len(syms)} → {len(filtered)} (min {MIN_LIQUIDITY/1e6:.1f}M USDT)", flush=True)
    return filtered[:MAX_SYMBOLS] if MAX_SYMBOLS else filtered

# ============================================================
# VERİ ÇEKME
# ============================================================
async def fetch_df(symbol, timeframe, limit):
    raw = await api_gate.call(exchange_spot.fetch_ohlcv, symbol, timeframe=timeframe, limit=limit)
    df  = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df.set_index("timestamp", inplace=True)
    return df

# ============================================================
# İNDİKATÖRLER
# ============================================================
def prepare_bars(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    c, h, l, v = df["close"], df["high"], df["low"], df["volume"]

    # Hacim MA (kapitülasyon için temel)
    df["vol_ma"] = v.rolling(VOL_PERIOD).mean()

    # ATR (adaptive stop için)
    tr        = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    df["atr"] = tr.ewm(alpha=1/14, adjust=False).mean()

    # ATR % (volatilite göstergesi)
    df["atr_pct"] = df["atr"] / c * 100

    # EMA50 (BTC trend için)
    df["ema50"]  = c.ewm(span=50,  adjust=False).mean()
    df["ema200"] = c.ewm(span=200, adjust=False).mean()

    # Bar getirisi (ret1 için önceki kapanış)
    df["close_prev"] = c.shift(1)

    return df.dropna(subset=["vol_ma", "atr", "close_prev"])

# ============================================================
# MUM FORMASYONLARI
# ============================================================
def _ohlc(df, i):
    row = df.iloc[i]
    return float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"])

def is_hammer(df, i):
    if i < 1: return False
    o, h, l, c = _ohlc(df, i)
    if c <= o: return False
    body = c - o; rng = h - l
    if rng <= 0 or body <= 0: return False
    return ((o - l) >= body * 2.0
            and (h - c) <= body * 0.5
            and (o - l) / rng >= 0.55)

def is_engulfing(df, i):
    if i < 1: return False
    o0, _, _, c0 = _ohlc(df, i)
    o1, _, _, c1 = _ohlc(df, i - 1)
    b0 = abs(c0 - o0); b1 = abs(c1 - o1)
    if b1 <= 0: return False
    return c1 < o1 and c0 > o0 and o0 <= c1 and c0 >= o1 and b0 >= b1 * 0.8

def is_morning_star(df, i):
    if i < 2: return False
    o0, _, _, c0 = _ohlc(df, i)
    o1, _, _, c1 = _ohlc(df, i - 1)
    o2, _, _, c2 = _ohlc(df, i - 2)
    b0 = abs(c0-o0); b1 = abs(c1-o1); b2 = abs(c2-o2)
    avg = (b0+b1+b2)/3 if (b0+b1+b2) > 0 else 1
    return (c2 < o2 and b2 >= avg
            and b1 <= avg * 0.5
            and c0 > o0 and b0 >= avg
            and c0 >= o2 - b2/2)

def detect_candle(df, i):
    if is_morning_star(df, i): return "🌅 Morning Star"
    if is_engulfing(df, i):    return "🟢 Bullish Engulfing"
    if is_hammer(df, i):       return "🔨 Hammer"
    return ""

# ============================================================
# KAPİTÜLASYON SİNYAL KONTROLÜ
# Backtest: 2022-2026, 50 coin
# ret1 < -7% + vol 1.5-3x → WR %83.7, ort kazanç +15.9%, ~61 sinyal/yıl
# ============================================================
def check_capitulation_signal(df: pd.DataFrame, symbol: str) -> dict | None:
    if len(df) < VOL_PERIOD + 5:
        return None

    # iloc[-1] = az önce kapanan crash barı (on_1h_close içinde eklendi)
    bar = df.iloc[-1]

    def gv(row, col):
        val = row.get(col, np.nan)
        return None if pd.isna(val) else float(val)

    close_now  = gv(bar, "close")
    close_prev = gv(bar, "close_prev")  # bar'ın bir önceki kapanışı (shift(1))
    vol_now    = gv(bar, "volume")
    vol_ma     = gv(bar, "vol_ma")
    atr_val    = gv(bar, "atr")
    atr_pct    = gv(bar, "atr_pct")

    if None in (close_now, close_prev, vol_now, vol_ma, atr_val):
        return None
    if close_prev == 0 or vol_ma == 0:
        return None

    # Bar getirisi (%)
    ret1 = (close_now / close_prev - 1) * 100

    # Hacim çarpanı
    vol_ratio = vol_now / vol_ma

    # KOŞUL 1: Crash barı (-15% ile -7% arası)
    if not (CRASH_MIN <= ret1 <= CRASH_MAX):
        stats["filtered_crash"] += 1
        return None

    # KOŞUL 2: Hacim spike (1.5x - 3x)
    if not (VOL_MIN <= vol_ratio <= VOL_MAX):
        stats["filtered_vol"] += 1
        return None

    # Giriş fiyatı = crash barının kapanışı
    entry = close_now

    # Stop: -7% (backtest SL — pump oncesi max drawdown analizinden)
    stop = round(entry * 0.93, 8)

    # TP'ler
    tp1 = round(entry * 1.05, 8)   # +5%
    tp2 = round(entry * 1.10, 8)   # +10%
    tp3 = round(entry * 1.15, 8)   # +15%

    funding     = funding_cache.get(symbol)
    funding_neg = funding is not None and funding < 0

    return {
        "symbol":      symbol,
        "type":        "capit",
        "entry":       round(entry, 8),
        "stop":        stop,
        "tp1":         tp1,
        "tp2":         tp2,
        "tp3":         tp3,
        "ret1":        round(ret1, 2),
        "vol_ratio":   round(vol_ratio, 2),
        "vol_ma":      round(vol_ma, 2),
        "atr":         round(atr_val, 8),
        "atr_pct":     round(atr_pct, 2) if atr_pct is not None else None,
        "funding":     round(funding, 6) if funding is not None else None,
        "funding_neg": funding_neg,
        "candle":      detect_candle(df, len(df) - 1),
    }

# ============================================================
# SİNYAL PUANLAMA (1–5 ⭐)
# Crash büyüklüğü ve hacim oranına göre
# ============================================================
def calc_signal_score(ret1: float, vol_ratio: float, liquidity: float) -> int:
    # Crash büyüklüğü — %50 ağırlık (daha sert = daha iyi WR)
    abs_ret = abs(ret1)
    if abs_ret >= 12:   crash_score = 5
    elif abs_ret >= 10: crash_score = 4
    elif abs_ret >= 9:  crash_score = 3
    elif abs_ret >= 8:  crash_score = 2
    else:               crash_score = 1   # -7% ile -8% arası

    # Hacim oranı — %30 ağırlık (2x civarı ideal)
    if 1.8 <= vol_ratio <= 2.5:  vol_score = 5
    elif 1.5 <= vol_ratio < 1.8: vol_score = 3
    elif 2.5 < vol_ratio <= 3.0: vol_score = 3
    else:                         vol_score = 1

    # Likidite — %20 ağırlık
    if liquidity >= 15_000_000:  liq_score = 5
    elif liquidity >= 5_000_000: liq_score = 3
    else:                        liq_score = 1

    raw = crash_score * 0.5 + vol_score * 0.3 + liq_score * 0.2
    return max(1, min(5, round(raw)))

def _stars(score: int) -> str:
    return "⭐" * score

# ============================================================
# FUNDING RATE + BTC 4H
# ============================================================
async def fetch_funding_rate(symbol):
    sym_fut = symbol.replace("/USDT", "USDT")
    try:
        loop = asyncio.get_running_loop()
        data = await loop.run_in_executor(None, lambda: exchange_fut.fetch_funding_rate(sym_fut))
        rate = data.get("fundingRate")
        funding_cache[symbol] = float(rate) if rate is not None else None
    except Exception:
        funding_cache[symbol] = None

async def refresh_funding_cache(symbols):
    print("  Funding cache dolduruluyor...", flush=True)
    ok = 0
    for sym in symbols:
        await fetch_funding_rate(sym)
        if funding_cache.get(sym) is not None:
            ok += 1
        await asyncio.sleep(0.05)
    print(f"  → {ok}/{len(symbols)} sembolde funding rate", flush=True)

async def refresh_btc_4h():
    try:
        df = await fetch_df("BTC/USDT", "4h", 100)
        if df is None or len(df) < 50: return
        df = prepare_bars(df)
        lc    = float(df["close"].iloc[-1])
        le50  = float(df["ema50"].iloc[-1])
        le200 = float(df["ema200"].iloc[-1])
        if lc > le50 > le200:   trend = "⬆️ Güçlü Yükseliş"
        elif lc > le50:         trend = "🟢 Yükseliş"
        elif lc > le200:        trend = "🟡 Karışık"
        else:                   trend = "🔴 Düşüş"
        btc_4h_cache.update({"trend": trend, "ema50": le50, "close": lc,
                              "updated": datetime.now(timezone.utc).strftime("%H:%M")})
        print(f"BTC 4H: {trend} | Fiyat:{lc:.0f} EMA50:{le50:.0f}", flush=True)
    except Exception as e:
        print(f"BTC 4H hata: {e}", flush=True)

# ============================================================
# TELEGRAM + PORTFOLIO
# ============================================================
def fmt_price(price):
    if price is None: return "?"
    p = float(price)
    if p >= 100:    return f"{p:.2f}"
    if p >= 1:      return f"{p:.3f}"
    if p >= 0.01:   return f"{p:.4f}"
    if p >= 0.0001: return f"{p:.6f}"
    return f"{p:.8f}"

def _pct(new, ref):
    if not ref: return "?"
    return f"+{round((new/ref-1)*100, 1)}%"

def _vol_risk(atr_pct):
    if atr_pct is None: return "?"
    if atr_pct >= 5.0:  return f"⚠️ Yüksek (%{atr_pct:.1f})"
    if atr_pct >= 2.5:  return f"🟡 Orta (%{atr_pct:.1f})"
    return f"🟢 Düşük (%{atr_pct:.1f})"

def _sep():
    return "━━━━━━━━━━━━━━━━━━━━"

def build_capitulation_message(r, tr_time, sig_num):
    sym  = r["symbol"].replace("/USDT", "")
    e    = r["entry"]
    icon = "💰" if r.get("funding_neg") else "🔴"
    ret1 = r.get("ret1", 0)
    vr   = r.get("vol_ratio", 0)

    lines = [
        f"🕐 {tr_time.strftime('%d/%m/%Y %H:%M')}",
        "",
        f"{icon} <b>#{sym}/USDT  •  PANİK PUMP  •  1H</b>",
        _sep(),
        f"💵 <b>Giriş</b>    {fmt_price(e)}",
        f"🛡️ <b>Stop</b>     {fmt_price(r['stop'])}  (-7%)",
        f"🎯 <b>TP1</b>      {fmt_price(r['tp1'])}  (+5%)",
        f"🎯 <b>TP2</b>      {fmt_price(r['tp2'])}  (+10%)",
        f"🎯 <b>TP3</b>      {fmt_price(r['tp3'])}  (+15%)",
        _sep(),
        "📊 <b>İndikatörler</b>",
        f"📉 <b>Düşüş</b>     {ret1:+.2f}%  (crash barı)",
        f"📊 <b>Hacim</b>     {vr:.2f}x ortalama  🔥",
    ]
    if r.get("funding") is not None:
        lines.append(f"<b>Funding</b>    {r['funding']:+.4f}%{'  💰' if r.get('funding_neg') else ''}")
    if r.get("candle"):
        lines.append(f"<b>Formasyon</b>  {r['candle']}  ✅")
    lines += [
        _sep(),
        f"<b>BTC 4H</b>     {btc_4h_cache.get('trend', '?')}",
        f"<b>Vol.Risk</b>   {_vol_risk(r.get('atr_pct'))}",
        _sep(),
        f"⏱ Cooldown: {SIGNAL_COOLDOWN_HOURS}H  |  #{sig_num} sinyal",
    ]
    return "\n".join(lines)

def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text,
                  "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=10,
        )
        if r.status_code != 200:
            print(f"Telegram {r.status_code}: {r.text[:80]}", flush=True)
    except Exception as e:
        print(f"Telegram hata: {e}", flush=True)

def send_to_portfolio(result):
    if not PORTFOLIO_URL: return
    try:
        payload = {
            "symbol":      result["symbol"],
            "entry":       result["entry"],
            "stop":        result["stop"],
            "tp1":         result.get("tp1"),
            "tp2":         result.get("tp2"),
            "sig_type":    "panik_pump",
            "sub_type":    "",
            "source":      "bot",
            "candle":      result.get("candle", ""),
            "funding_neg": result.get("funding_neg", False),
        }
        headers = {"Content-Type": "application/json"}
        if PORTFOLIO_TOKEN:
            headers["Authorization"] = f"Bearer {PORTFOLIO_TOKEN}"
        r = requests.post(f"{PORTFOLIO_URL}/api/signal",
                          json=payload, headers=headers, timeout=5)
        if r.status_code == 201:
            print(f"[PORTFOLIO] Sinyal gönderildi: {result['symbol']}", flush=True)
        elif r.status_code == 409:
            print(f"[PORTFOLIO] Zaten açık: {result['symbol']}", flush=True)
        else:
            print(f"[PORTFOLIO] HTTP {r.status_code}: {r.text[:80]}", flush=True)
    except Exception as e:
        print(f"[PORTFOLIO] Hata: {e}", flush=True)

# ============================================================
# PERFORMANS TAKİP
# ============================================================
SIGNAL_LOG_PATH = "/tmp/signal_log.json"

def load_signal_log():
    try:
        with open(SIGNAL_LOG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []

def save_signal_log(log):
    try:
        with open(SIGNAL_LOG_PATH, "w", encoding="utf-8") as f:
            json.dump(log[-500:], f, ensure_ascii=False, default=str)
    except Exception as e:
        print(f"signal_log kayit hata: {e}", flush=True)

signal_log        = load_signal_log()
pending_by_symbol = {}

def log_signal(result, tr_time):
    entry_rec = {
        "id":          f"{result['symbol']}_{int(tr_time.timestamp())}",
        "symbol":      result["symbol"],
        "entry":       result["entry"],
        "stop":        result["stop"],
        "tp1":         result.get("tp1"),
        "tp2":         result.get("tp2"),
        "tp3":         result.get("tp3"),
        "sig_type":    "panik_pump",
        "funding_neg": result.get("funding_neg", False),
        "candle":      result.get("candle", ""),
        "time":        tr_time.isoformat(),
        "status":      "open",
        "peak_pct":    0.0, "tp1_hit": False, "tp2_hit": False, "tp3_hit": False,
        "close_time":  None, "close_price": None, "close_ret": None,
        "ret1":      result.get("ret1"),
        "vol_ratio": result.get("vol_ratio"),
        "score":     result.get("score"),
    }
    signal_log.insert(0, entry_rec)
    if len(signal_log) > 500: signal_log.pop()
    save_signal_log(signal_log)
    pending_by_symbol.setdefault(result["symbol"], []).append(entry_rec)

def rebuild_pending():
    for s in signal_log:
        if s["status"] == "open":
            pending_by_symbol.setdefault(s["symbol"], []).append(s)

def check_pending_for_symbol(symbol, bar_high, bar_low, bar_close, bar_time):
    if symbol not in pending_by_symbol: return
    now      = datetime.now(timezone.utc)
    to_close = []
    for entry in pending_by_symbol[symbol]:
        e   = entry["entry"]; stp = entry["stop"]
        sig_time = datetime.fromisoformat(entry["time"])
        if sig_time.tzinfo is None:
            sig_time = sig_time.replace(tzinfo=timezone.utc)
        elapsed_h = (now - sig_time).total_seconds() / 3600

        cur_ret = (bar_high - e) / e * 100
        if cur_ret > entry["peak_pct"]:
            entry["peak_pct"] = round(cur_ret, 2)

        tp1 = entry.get("tp1"); tp2 = entry.get("tp2"); tp3 = entry.get("tp3")
        if tp3 and bar_high >= tp3 and not entry.get("tp3_hit"): entry["tp3_hit"] = True
        if tp2 and bar_high >= tp2 and not entry.get("tp2_hit"): entry["tp2_hit"] = True
        if tp1 and bar_high >= tp1 and not entry.get("tp1_hit"): entry["tp1_hit"] = True

        if bar_low <= stp:
            entry.update({"status": "loss", "close_time": bar_time.isoformat(),
                          "close_price": round(stp, 8), "close_ret": round((stp-e)/e*100, 2)})
            to_close.append(entry); continue
        if tp2 and bar_high >= tp2:
            entry.update({"status": "win", "close_time": bar_time.isoformat(),
                          "close_price": round(tp2, 8), "close_ret": round((tp2-e)/e*100, 2)})
            to_close.append(entry); continue
        if elapsed_h >= 24:
            entry.update({"status": "expired", "close_time": bar_time.isoformat(),
                          "close_price": round(bar_close, 8), "close_ret": round((bar_close-e)/e*100, 2)})
            to_close.append(entry)

    if to_close:
        for en in to_close:
            pending_by_symbol[symbol].remove(en)
        if not pending_by_symbol[symbol]:
            del pending_by_symbol[symbol]
        save_signal_log(signal_log)

def perf_summary():
    closed = [s for s in signal_log if s["status"] in ("loss", "win", "expired")]
    def avg_peak(lst):
        peaks = [s["peak_pct"] for s in lst if s.get("peak_pct") is not None]
        return round(sum(peaks)/len(peaks), 2) if peaks else 0.0
    return {
        "total":    len(signal_log),
        "open":     sum(1 for s in signal_log if s["status"] == "open"),
        "win":      sum(1 for s in closed if s["status"] == "win"),
        "loss":     sum(1 for s in closed if s["status"] == "loss"),
        "expired":  sum(1 for s in closed if s["status"] == "expired"),
        "avg_peak": avg_peak(closed),
        "win_rate": round(
            sum(1 for s in closed if s["status"] == "win") / len(closed) * 100, 1
        ) if closed else 0.0,
    }

# ============================================================
# BOOTSTRAP
# ============================================================
async def bootstrap_symbol(symbol):
    try:
        df = await fetch_df(symbol, "1h", BOOTSTRAP_BARS)
        if df is None or len(df) < 60: return False
        df = prepare_bars(df)
        bars_1h[symbol] = df.iloc[-KEEP_BARS:] if len(df) > KEEP_BARS else df
        return True
    except Exception:
        return False

async def bootstrap_all(symbols):
    ok = 0
    print(f"Bootstrap: {len(symbols)} sembol", flush=True)
    for i, sym in enumerate(symbols, 1):
        if i % 50 == 0:
            print(f"  → {i}/{len(symbols)}", flush=True)
        if await bootstrap_symbol(sym):
            ok += 1
    print(f"Bootstrap bitti: {ok}/{len(symbols)}", flush=True)

# ============================================================
# SİNYAL WORKER
# ============================================================
@dataclass
class SignalCandidate:
    symbol:  str
    result:  dict
    tr_time: datetime

async def signal_worker(candidate_queue):
    global signal_counter
    while True:
        sig = await candidate_queue.get()
        try:
            symbol  = sig.symbol
            result  = sig.result
            tr_time = sig.tr_time

            # Likidite kontrolü
            liquidity = 0.0
            try:
                ticker    = await api_gate.call(exchange_spot.fetch_ticker, symbol)
                liquidity = float(ticker.get("quoteVolume", 0) or 0)
                if liquidity < MIN_LIQUIDITY:
                    stats["low_liquidity"] += 1
                    continue
            except Exception:
                pass

            # Puanlama
            result["liquidity"] = liquidity
            result["score"] = calc_signal_score(
                result.get("ret1", 0),
                result.get("vol_ratio", 0),
                liquidity,
            )

            # Funding güncelle
            await fetch_funding_rate(symbol)
            result["funding"]     = funding_cache.get(symbol)
            result["funding_neg"] = result["funding"] is not None and result["funding"] < 0

            signal_counter += 1
            msg  = build_capitulation_message(result, tr_time, signal_counter)
            icon = "💰" if result.get("funding_neg") else "🔴"

            send_telegram(msg)
            send_to_portfolio(result)

            last_signal_ts[symbol] = tr_time.replace(tzinfo=None)
            result["time"]     = tr_time.strftime("%Y-%m-%d %H:%M")
            result["sig_type"] = "panik_pump"
            all_signals.insert(0, result)
            if len(all_signals) > 200: all_signals.pop()

            stats["signal_sent"] += 1
            log_signal(result, tr_time)

            print(
                f"SİNYAL {icon} [KAPİTÜLASYON] {symbol}"
                f" | düşüş:{result['ret1']:+.2f}%"
                f" | vol:{result['vol_ratio']:.2f}x"
                f" | giriş:{fmt_price(result['entry'])}"
                f" | puan:{result['score']}/5"
                + (f" | {result.get('candle','')}" if result.get("candle") else ""),
                flush=True,
            )

        except Exception as e:
            print(f"Worker hata: {str(e)[:100]}", flush=True)
        finally:
            candidate_queue.task_done()

# ============================================================
# MUM KAPANIŞINI İŞLE
# ============================================================
async def on_1h_close(symbol, o, h, l, c, v, ts_ms, candidate_queue):
    global ws_1h_closes
    ws_1h_closes += 1

    now_ts = time.time()
    if now_ts - heartbeat["epoch"] >= 10:
        heartbeat.update({"last": tr_now_str(), "epoch": now_ts, "symbol": symbol})

    bar_time = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    check_pending_for_symbol(symbol, h, l, c, bar_time)

    df = bars_1h.get(symbol)
    if df is None or len(df) < VOL_PERIOD + 5:
        stats["data_missing"] += 1; return

    # Kapanan barı df'e ekle
    tstamp = pd.to_datetime(ts_ms, unit="ms", utc=True)
    df.loc[tstamp, ["open", "high", "low", "close", "volume"]] = [o, h, l, c, v]
    df = df.sort_index()
    if len(df) > KEEP_BARS: df = df.iloc[-KEEP_BARS:]
    df = prepare_bars(df)
    bars_1h[symbol] = df

    # Cooldown kontrolü
    tr_time = datetime.now(timezone.utc).astimezone(TR_TZ)
    last    = last_signal_ts.get(symbol)
    if last is not None:
        elapsed = (tr_time.replace(tzinfo=None) - last.replace(tzinfo=None)).total_seconds() / 3600
        if elapsed < SIGNAL_COOLDOWN_HOURS:
            stats["cooldown"] += 1
            return

    # Kapitülasyon sinyali kontrolü
    result = check_capitulation_signal(df, symbol)
    if result:
        await candidate_queue.put(SignalCandidate(symbol, result, tr_time))
    else:
        stats["capit_filtered"] += 1

# ============================================================
# WEBSOCKET
# ============================================================
def to_ws(symbol):
    return symbol.replace("/", "").lower()

async def ws_chunk(symbols, candidate_queue):
    streams = "/".join([f"{to_ws(s)}@kline_1h" for s in symbols])
    url     = f"wss://stream.binance.com:9443/stream?streams={streams}"
    retry   = 0
    while True:
        try:
            async with websockets.connect(url, ping_interval=None,
                                           open_timeout=30, close_timeout=10,
                                           max_size=10*1024*1024) as ws:
                retry = 0
                print(f"WS bağlandı ({len(symbols)} sembol)", flush=True)

                async def keep_alive(ws):
                    while True:
                        await asyncio.sleep(20)
                        try:
                            pong = await ws.ping()
                            await asyncio.wait_for(pong, timeout=10)
                        except Exception:
                            break

                ping_task = asyncio.create_task(keep_alive(ws))
                try:
                    while True:
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=60)
                        except asyncio.TimeoutError:
                            continue
                        data = json.loads(msg)
                        k    = data.get("data", {}).get("k", {})
                        if not k.get("x", False): continue
                        sym = data.get("data", {}).get("s", "").upper().replace("USDT", "/USDT")
                        try:
                            await on_1h_close(sym,
                                float(k["o"]), float(k["h"]), float(k["l"]), float(k["c"]),
                                float(k["v"]), int(k["t"]), candidate_queue)
                        except Exception as e:
                            print(f"on_1h_close hata [{sym}]: {str(e)[:80]}", flush=True)
                finally:
                    ping_task.cancel()
        except Exception as e:
            retry  += 1
            backoff = min(60, 5 * (2 ** min(retry, 4)))
            print(f"WS koptu → {backoff}s: {str(e)[:50]}", flush=True)
            await asyncio.sleep(backoff)

async def ws_all(symbols, candidate_queue):
    tasks = [
        asyncio.create_task(ws_chunk(symbols[i:i+WS_STREAM_CHUNK], candidate_queue))
        for i in range(0, len(symbols), WS_STREAM_CHUNK)
    ]
    await asyncio.gather(*tasks)

# ============================================================
# FLASK DASHBOARD
# ============================================================
import logging
flask_app = Flask(__name__)
logging.getLogger("werkzeug").setLevel(logging.ERROR)
flask_app.logger.disabled = True

def clean_json(obj):
    if isinstance(obj, dict):  return {k: clean_json(v) for k, v in obj.items()}
    if isinstance(obj, list):  return [clean_json(i) for i in obj]
    if isinstance(obj, float):
        if obj != obj or obj in (float("inf"), float("-inf")): return None
        return obj
    if hasattr(obj, "item"): return clean_json(obj.item())
    return obj

@flask_app.route("/")
def home():
    now = tr_now().strftime("%H:%M:%S")
    sig_rows = ""
    for s in all_signals[:30]:
        fn    = s.get("funding_neg", False)
        candle= s.get("candle", "")
        icon  = "💰" if fn else "🔴"
        bc    = "#c8e86a" if fn else "#ff4444"
        fv    = s.get("funding"); fs = f"{fv:+.4f}%" if fv is not None else "—"
        score_str = "⭐" * s.get("score", 0) if s.get("score") else "—"
        ret1_v = s.get("ret1", 0); vr_v = s.get("vol_ratio", 0)
        ind   = (f"Düşüş:{ret1_v:+.2f}%  Vol:{vr_v:.2f}x  Puan:{score_str}")
        cs    = f"  {candle}" if candle else ""
        sig_rows += (
            f'<div class="sig" style="border-color:{bc}">'
            f'<div class="sr"><b>{icon} {s.get("symbol","")} <small>[KAPİTÜLASYON]</small>{cs}</b>'
            f'<span class="ts">{s.get("time","")[:16]}</span></div>'
            f'<div class="sd">💵 {fmt_price(s.get("entry"))}  🛡️ {fmt_price(s.get("stop"))}'
            f'  🎯 TP1:{fmt_price(s.get("tp1"))} / TP2:{fmt_price(s.get("tp2"))} / TP3:{fmt_price(s.get("tp3"))}</div>'
            f'<div class="sd">{ind}  Funding:{fs}</div>'
            f'</div>'
        )
    ps = perf_summary()
    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Kapitülasyon Scanner v5.0</title>
<meta http-equiv="refresh" content="30">
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#06090d;color:#b8cdd8;font-family:'Courier New',monospace;padding:20px;max-width:960px;margin:0 auto}}
h1{{color:#00d4ff;letter-spacing:4px;font-size:1.1rem;margin-bottom:16px}}
.stats{{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:20px}}
.stat{{background:#0c1117;border:1px solid #1c2a36;padding:9px 14px;border-radius:4px;min-width:80px}}
.sv{{font-size:1.1rem;color:#00d4ff;display:block;font-weight:bold}}
.sl{{font-size:.58rem;color:#3d5a6a;text-transform:uppercase;letter-spacing:1px}}
h3{{color:#ff4444;margin:0 0 10px;font-size:.78rem;letter-spacing:2px}}
.sig{{background:#0d0305;border-left:3px solid #ff4444;padding:10px 14px;margin:5px 0;border-radius:2px}}
.sr{{display:flex;justify-content:space-between;align-items:center;margin-bottom:5px}}
.sd{{font-size:.73rem;margin:2px 0;color:#8aa8b8}}
.ts{{color:#3d5a6a;font-size:.65rem}}
.footer{{color:#3d5a6a;font-size:.62rem;margin-top:20px;border-top:1px solid #1c2a36;padding-top:10px;line-height:2}}
</style></head><body>
<h1>KAPİTÜLASYON SCANNER <small style="font-size:.6rem;color:#3d5a6a">v5.0</small></h1>
<div style="background:#0c1117;border:1px solid #2a1c1c;padding:8px 14px;border-radius:4px;margin-bottom:16px;font-size:.72rem;color:#3d5a6a">
  🔴 KAPİTÜLASYON: Düşüş {CRASH_MAX:.0f}% ile {CRASH_MIN:.0f}%  |  Hacim {VOL_MIN:.1f}x - {VOL_MAX:.1f}x  |  Stop -7%  |  TP +5/10/15%
  &nbsp;&nbsp;|&nbsp;&nbsp; Backtest WR ~%84  |  BTC 4H: {btc_4h_cache.get("trend","?")}
</div>
<div class="stats">
  <div class="stat"><span class="sv">{len(tracked_symbols)}</span><span class="sl">Sembol</span></div>
  <div class="stat"><span class="sv">{ws_1h_closes}</span><span class="sl">1H Kapanış</span></div>
  <div class="stat"><span class="sv">{stats.get("signal_sent",0)}</span><span class="sl">Toplam Sinyal</span></div>
  <div class="stat"><span class="sv" style="color:#00f080">{ps.get("win",0)}</span><span class="sl">Win</span></div>
  <div class="stat"><span class="sv" style="color:#ff4444">{ps.get("loss",0)}</span><span class="sl">Stop</span></div>
  <div class="stat"><span class="sv">{ps.get("win_rate",0)}%</span><span class="sl">Win Rate</span></div>
  <div class="stat"><span class="sv">{ps.get("avg_peak",0)}%</span><span class="sl">Ort. Peak</span></div>
  <div class="stat"><span class="sv">{bot_status["status"]}</span><span class="sl">Durum</span></div>
  <div class="stat"><span class="sv">{now}</span><span class="sl">Saat TR</span></div>
</div>
<h3>SON KAPİTÜLASYON SİNYALLERİ</h3>
{sig_rows if sig_rows else '<p style="color:#3d5a6a;font-size:.8rem;padding:8px 0">Henüz sinyal yok. (~61 sinyal/yıl beklenir)</p>'}
<div class="footer">
  Heartbeat: {heartbeat["last"]} | Son coin: {heartbeat["symbol"]}
  &nbsp;|&nbsp; <a href="/performance" style="color:#00d4ff">📈 Performans</a><br>
  Eleme: Cooldown:{stats.get("cooldown",0)} Crash:{stats.get("filtered_crash",0)} Vol:{stats.get("filtered_vol",0)} Hacim:{stats.get("low_liquidity",0)}
</div>
</body></html>"""

@flask_app.route("/api/status")
def api_status():
    data = clean_json({
        "status": bot_status["status"], "total_symbols": len(tracked_symbols),
        "ws_1h_closes": ws_1h_closes, "signals": all_signals[:30],
        "stats": dict(stats), "heartbeat": heartbeat,
        "params": {
            "crash_min": CRASH_MIN, "crash_max": CRASH_MAX,
            "vol_min": VOL_MIN, "vol_max": VOL_MAX,
            "cooldown_h": SIGNAL_COOLDOWN_HOURS,
        }
    })
    return flask_app.response_class(json.dumps(data, ensure_ascii=False), mimetype="application/json")

@flask_app.route("/api/health")
def api_health():
    stale = time.time() - float(heartbeat.get("epoch", 0))
    return {"status": "ok", "time": tr_now_str(), "stale_sec": round(stale)}

@flask_app.route("/performance")
def perf_dashboard():
    ps   = perf_summary()
    rows = ""
    for s in signal_log[:50]:
        st     = s.get("status", "open")
        st_col = "#00f080" if st == "win" else ("#ff4444" if st == "loss" else ("#ffb300" if st == "expired" else "#3d5a6a"))
        peak   = s.get("peak_pct", 0)
        cr     = s.get("close_ret")
        ct     = (s.get("close_time") or "")[:16]
        candle = s.get("candle", "")
        ret1_v = s.get("ret1", "—")
        vr_v   = s.get("vol_ratio", "—")
        score_v= "⭐" * s.get("score", 0) if s.get("score") else "—"
        def fmt_ret(r):
            if r is None: return "—"
            col = "#00f080" if float(r) > 0 else "#ff4444"
            return f'<span style="color:{col}">{float(r):+.2f}%</span>'
        rows += f"""<tr>
          <td>{s.get("time","")[:16]}</td>
          <td><b>🔴 {s.get("symbol","")}</b></td>
          <td>{ret1_v}%</td>
          <td>{vr_v}x</td>
          <td>{score_v}</td>
          <td style="color:#00f080">+{peak}%</td>
          <td>{'✅' if s.get('tp1_hit') else '—'}</td>
          <td>{'✅' if s.get('tp2_hit') else '—'}</td>
          <td>{'✅' if s.get('tp3_hit') else '—'}</td>
          <td>{fmt_ret(cr)}</td>
          <td>{ct}</td>
          <td>{candle}</td>
          <td style="color:{st_col}">{st.upper()}</td>
        </tr>"""
    return f"""<!DOCTYPE html><html lang="tr"><head>
<meta charset="UTF-8"><title>Performans — Kapitülasyon v5</title>
<meta http-equiv="refresh" content="300">
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:#06090d;color:#b8cdd8;font-family:'Courier New',monospace;padding:24px}}
  h1{{color:#00d4ff;font-size:1.1rem;margin-bottom:4px}}
  .sub{{color:#3d5a6a;font-size:.75rem;margin-bottom:18px}}
  .cards{{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:20px}}
  .card{{background:#0c1117;border:1px solid #1c2a36;border-radius:4px;padding:12px;min-width:110px;text-align:center}}
  .cv{{font-size:1.3rem;font-weight:bold;color:#00d4ff}}
  .cl{{font-size:.62rem;color:#3d5a6a;margin-top:4px}}
  table{{width:100%;border-collapse:collapse}}
  th{{background:#0c1117;color:#3d5a6a;font-size:.65rem;text-transform:uppercase;padding:6px 10px;text-align:left;border-bottom:1px solid #1c2a36}}
  td{{padding:6px 10px;border-bottom:1px solid #0c1117;font-size:.76rem}}
  tr:hover td{{background:#0c1117}}
  a{{color:#00d4ff;text-decoration:none}}
</style></head><body>
<h1>📈 KAPİTÜLASYON SİNYAL PERFORMANSI v5.0</h1>
<div class="sub"><a href="/">← Ana Sayfa</a> &nbsp;|&nbsp; {tr_now_str()}</div>
<div class="cards">
  <div class="card"><div class="cv">{ps.get("total",0)}</div><div class="cl">Toplam</div></div>
  <div class="card"><div class="cv">{ps.get("open",0)}</div><div class="cl">Açık</div></div>
  <div class="card"><div class="cv" style="color:#00f080">{ps.get("win",0)}</div><div class="cl">Win (TP2)</div></div>
  <div class="card"><div class="cv" style="color:#ff4444">{ps.get("loss",0)}</div><div class="cl">Stop (-7%)</div></div>
  <div class="card"><div class="cv" style="color:#ffb300">{ps.get("expired",0)}</div><div class="cl">Expired</div></div>
  <div class="card"><div class="cv">{ps.get("win_rate",0)}%</div><div class="cl">Win Rate</div></div>
  <div class="card"><div class="cv">{ps.get("avg_peak",0)}%</div><div class="cl">Ort. Peak</div></div>
</div>
<table><thead><tr>
  <th>Zaman</th><th>Sembol</th><th>Düşüş%</th><th>Vol X</th><th>Puan</th>
  <th>Peak%</th><th>TP1(+5%)</th><th>TP2(+10%)</th><th>TP3(+15%)</th>
  <th>Kapanış%</th><th>Kapanış Zamanı</th><th>Formasyon</th><th>Durum</th>
</tr></thead><tbody>{rows}</tbody></table>
</body></html>"""

# ============================================================
# PERİYODİK GÖREVLER
# ============================================================
async def periodic_tasks():
    tick = 0
    while True:
        await asyncio.sleep(600)
        tick += 1
        print(
            f"\n╔══════════════ KAPİTÜLASYON ÖZET ══════════════╗\n"
            f"  Sembol: {len(tracked_symbols):<6} 1H Kapanış: {ws_1h_closes:<6} Sinyal: {stats.get('signal_sent',0)}\n"
            f"  ── Filtre ──\n"
            f"  Crash  : {stats.get('filtered_crash',0):<6} (ret1 aralık dışı)\n"
            f"  Vol    : {stats.get('filtered_vol',0):<6} (hacim aralık dışı)\n"
            f"  Hacim  : {stats.get('low_liquidity',0):<6} (düşük likidite)\n"
            f"  Cooldown: {stats.get('cooldown',0)}\n"
            f"╚═══════════════════════════════════════════════╝",
            flush=True,
        )
        if tick % 2 == 0:
            await refresh_btc_4h()

# ============================================================
# MAIN
# ============================================================
async def main():
    global tracked_symbols
    print("Kapitülasyon Scanner v5.0 başlatılıyor...", flush=True)
    print(f"Sinyal koşulları:", flush=True)
    print(f"  Crash barı: ret1 {CRASH_MAX:.0f}% ile {CRASH_MIN:.0f}% arası", flush=True)
    print(f"  Hacim spike: {VOL_MIN:.1f}x - {VOL_MAX:.1f}x (20-bar ortalama)", flush=True)
    print(f"  Stop: -7% | TP1: +5% | TP2: +10% | TP3: +15%", flush=True)
    print(f"  Cooldown: {SIGNAL_COOLDOWN_HOURS}H", flush=True)
    print(f"  Beklenen: ~%84 WR | ~61 sinyal/yıl", flush=True)

    symbols = await load_symbols_pool()
    if not symbols:
        print("Sembol yüklenemedi", flush=True); return

    tracked_symbols      = list(symbols)
    bot_status["status"] = "BOOTSTRAP"
    print(f"{len(symbols)} sembol yüklendi", flush=True)

    await bootstrap_all(symbols)
    await refresh_funding_cache(symbols)
    await refresh_btc_4h()
    rebuild_pending()
    print(f"Pending sinyaller: {sum(len(v) for v in pending_by_symbol.values())}", flush=True)

    candidate_queue = asyncio.Queue()
    asyncio.create_task(signal_worker(candidate_queue))
    asyncio.create_task(periodic_tasks())

    bot_status["status"] = "LIVE"
    print(f"LIVE | {len(symbols)} sembol izleniyor", flush=True)

    await ws_all(symbols, candidate_queue)

def start_flask():
    port = int(os.environ.get("PORT", "10000"))
    flask_app.run(host="0.0.0.0", port=port, use_reloader=False)

if __name__ == "__main__":
    threading.Thread(target=start_flask, daemon=True).start()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Durduruldu", flush=True)
