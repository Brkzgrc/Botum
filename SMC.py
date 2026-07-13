# ═══════════════════════════════════════════════════════════════
#  SMC — CHoCH Bullish Sinyal Sistemi
#  Kayıt: portfolio_tracker.py (source="smc-v2")
# ───────────────────────────────────────────────────────────────
#  SİNYAL KOŞULLARI
#    • CHoCH Bullish (yapısal kırılım)
#    • Hacim filtresi  : son bar / 20 bar MA ≥ 5.0x
#    • 24h soğuma      : aynı coinde 24 saat tekrar yok
#    • BTC crash filtresi aktif
#    • BTC downtrend filtresi aktif
#
#  STOP / TP
#    • Stop   : yapısal düşük (CHoCH öncesi swing low)
#    • TP1    : 1:1 risk/ödül
#    • TP2    : 1:2 risk/ödül
#
#  BACKTEST SONUÇLARI  (2022-2026, exit_full_trail, vol≥5x, BTC crash+downtrend filtreli)
#    ┌─────────────────────────────────────────────────────────┐
#    │  TP1'e ulaşınca: KAPATMA YOK — trailing aktifleşir     │
#    │  Trailing stop : peak'ten -%2.5 geri çekilince çıkış   │
#    │                                                         │
#    │  Binance Trailing Stop Emri:                            │
#    │    Activation Price = TP1                               │
#    │    Callback Rate    = %2.5                              │
#    └─────────────────────────────────────────────────────────┘
# ═══════════════════════════════════════════════════════════════

import asyncio
import json
import math
import os
import threading
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

import ccxt
import pandas as pd
import requests
import websockets
from flask import Flask


app = Flask(__name__)

import logging
logging.getLogger("werkzeug").setLevel(logging.ERROR)

@app.route('/')
def health_check():
    boot_status = "BOOTSTRAPPING" if not bootstrap_done else "RUNNING"
    cached = len(bars_cache)
    btc_cr = "BTC ÇAKILIYOR 🚨" if btc_crash_cache.get("crashing") else "BTC Normal ✅"
    return (f"SMC v23 — CHoCH+vol≥5x+BTC filtreli | {boot_status} | {cached} coin cached | "
            f"{btc_cr} | {ws_1h_closes} bar kapandı"), 200

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)

# ============================================================
# 1) AYARLAR
# ============================================================
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
PORTFOLIO_URL    = os.getenv("PORTFOLIO_URL", "")
PORTFOLIO_TOKEN  = os.getenv("PORTFOLIO_TOKEN", "")

TIMEFRAME        = "1h"

CHOCH_SWING      = 5    # Micro CHoCH tespiti için (LuxAlgo ile aynı)
VOL_RATIO_MIN    = 7.5  # Hacim filtresi: son bar / 20 bar MA

BOOTSTRAP_BARS   = 2500
KEEP_BARS        = 2500

PHASE2_COOLDOWN  = 86400  # 24 saat cooldown

SIGNALS_FILE     = "sent_signals.json"
WS_STREAM_CHUNK  = 120

IGNORED_COINS = set([
    'UP/USDT','DOWN/USDT','BEAR/USDT','BULL/USDT',
    'USDC/USDT','TUSD/USDT','FDUSD/USDT','DAI/USDT','USDP/USDT',
    'USDE/USDT','UST/USDT','USD/USDT','XUSD/USDT','USD1/USDT','BFUSD/USDT',
    'USTC/USDT','BUSD/USDT','FRAX/USDT','LUSD/USDT','GUSD/USDT','SUSD/USDT',
    'USDS/USDT','USDX/USDT','USDD/USDT','CUSD/USDT','OUSD/USDT','MUSD/USDT',
    'RLUSD/USDT','U/USDT',
    'EUR/USDT','TRY/USDT','GBP/USDT','BRL/USDT','RUB/USDT',
    'AUD/USDT','BIDR/USDT','IDRT/USDT','VAI/USDT',
    'PAXG/USDT','XAUT/USDT','WBTC/USDT','WETH/USDT','WBNB/USDT','BETH/USDT',
    'BTCB/USDT','HBTC/USDT',
])

exchange = ccxt.binance()
scan_stats = Counter()
ws_1h_closes = 0

# ============================================================
# 1b) VERİ CACHE + BOOTSTRAP
# ============================================================
bars_cache = {}
bootstrap_done = False

def fetch_bars_sync(symbol, limit=BOOTSTRAP_BARS):
    all_bars = []
    since_ms = int((time.time() - limit * 3600) * 1000)
    while len(all_bars) < limit:
        try:
            batch = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME,
                                          since=since_ms, limit=1000)
        except Exception as e:
            print(f"  [HATA] {symbol}: {e}", flush=True)
            break
        if not batch:
            break
        all_bars.extend(batch)
        since_ms = batch[-1][0] + 1
        if len(batch) < 1000:
            break
        time.sleep(0.5)
    if not all_bars:
        return None
    df = pd.DataFrame(all_bars, columns=["timestamp","open","high","low","close","volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df.set_index("timestamp", inplace=True)
    df = df[~df.index.duplicated(keep='first')]
    return df

async def bootstrap_symbol(symbol):
    loop = asyncio.get_running_loop()
    try:
        df = await loop.run_in_executor(None, lambda: fetch_bars_sync(symbol, BOOTSTRAP_BARS))
        if df is not None and len(df) >= 200:
            bars_cache[symbol] = df.iloc[-KEEP_BARS:] if len(df) > KEEP_BARS else df
            return True
    except Exception as e:
        print(f"  [Bootstrap hata] {symbol}: {e}", flush=True)
    return False

async def bootstrap_all(symbols):
    global bootstrap_done
    print(f"📦 Bootstrap başladı: {len(symbols)} coin × {BOOTSTRAP_BARS} bar...", flush=True)
    ok = 0
    tasks = [bootstrap_symbol(s) for s in symbols]
    for idx, (s, coro) in enumerate(zip(symbols, tasks), 1):
        result = await coro
        if result:
            ok += 1
        if idx % 50 == 0:
            print(f"  → {idx}/{len(symbols)}...", flush=True)
    print(f"✅ Bootstrap bitti: {ok}/{len(symbols)} coin yüklendi", flush=True)

    bootstrap_done = True

# ============================================================
# 1c) BTC FİLTRELERİ
# ============================================================
btc_crash_cache = {"crashing": False, "updated": 0}
BTC_CRASH_PCT = 3.0
BTC_CRASH_TTL = 1800

btc_downtrend_cache = {"active": False, "updated": 0}
BTC_DOWNTREND_TTL = 3600

def check_btc_crash():
    now = time.time()
    if now - btc_crash_cache["updated"] < BTC_CRASH_TTL:
        return btc_crash_cache["crashing"]
    try:
        bars = exchange.fetch_ohlcv("BTC/USDT", timeframe="4h", limit=2)
        if len(bars) < 2:
            btc_crash_cache["crashing"] = False
        else:
            prev_close = float(bars[-2][4])
            curr_close = float(bars[-1][4])
            change_pct = ((curr_close - prev_close) / prev_close) * 100
            crashing = change_pct <= -BTC_CRASH_PCT
            btc_crash_cache["crashing"] = crashing
            if crashing:
                print(f"⚠️ BTC ÇAKILIYOR: {change_pct:.1f}% (4h)", flush=True)
            else:
                print(f"✅ BTC normal: {change_pct:+.1f}% (4h)", flush=True)
    except Exception as e:
        print(f"BTC crash check hata: {e}", flush=True)
        btc_crash_cache["crashing"] = False
    btc_crash_cache["updated"] = now
    return btc_crash_cache["crashing"]

def check_btc_downtrend_active():
    """
    BTC 4H'de aktif düşüş yapısı var mı?
    Son swing kırılımı düşen dip (swing_trend == -1) VE son dip öncekinden yüksek değilse → True.
    """
    now = time.time()
    if now - btc_downtrend_cache["updated"] < BTC_DOWNTREND_TTL:
        return btc_downtrend_cache["active"]

    active = False
    try:
        bars = exchange.fetch_ohlcv("BTC/USDT", timeframe="4h", limit=200)
        df_btc = pd.DataFrame(bars, columns=["timestamp", "open", "high", "low", "close", "volume"])
        trend, last_low, prev_low = _swing_lows_trend(df_btc, CHOCH_SWING)
        if trend == -1 and (prev_low is None or last_low <= prev_low):
            active = True
        status = "AKTİF 🔻" if active else "yok/durdu ✅"
        print(f"📉 BTC 4H Düşüş Yapısı: {status} (trend={trend}, son_dip={last_low}, önceki_dip={prev_low})", flush=True)
    except Exception as e:
        print(f"BTC downtrend check hata: {e}", flush=True)
        active = False

    btc_downtrend_cache["active"]  = active
    btc_downtrend_cache["updated"] = now
    return active

# ============================================================
# 2) SİNYAL HAFIZASI
# ============================================================
sent_signals = {}
signals_lock = threading.Lock()

def load_signals():
    if os.path.exists(SIGNALS_FILE):
        try:
            with open(SIGNALS_FILE) as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_signals(data):
    try:
        with open(SIGNALS_FILE, "w") as f:
            json.dump(data, f)
    except Exception as e:
        print(f"[UYARI] Sinyal kaydedilemedi: {e}")

def get_last_sent(symbol, phase, source=""):
    key = f"{symbol}_{source}"
    return sent_signals.get(key, {}).get(phase, 0.0)

def mark_sent(symbol, phase, source=""):
    key = f"{symbol}_{source}"
    with signals_lock:
        if key not in sent_signals:
            sent_signals[key] = {}
        sent_signals[key][phase] = time.time()
        save_signals(sent_signals)

# ============================================================
# 3) TELEGRAM + PORTFOLIO
# ============================================================
def send_telegram_msg(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML",
                  "message_thread_id": 2},
            timeout=10)
        if r.status_code != 200:
            print(f"[TELEGRAM] HTTP {r.status_code}: {r.text[:120]}")
    except Exception as e:
        print(f"[TELEGRAM] Hata: {e}")

def send_to_portfolio(symbol, entry_price, atr_val, phase, source, break_type="", stop_price=None, limit_price=None, signal_price=None):
    """entry_price = choch_level (CHoCH seviyesi, LuxAlgo çizgisi)"""
    if not PORTFOLIO_URL:
        return
    try:
        if stop_price is not None:
            stop = round(stop_price, 10)
            risk = entry_price - stop
            tp1  = round(entry_price + risk * 1.0, 10)
            tp2  = round(entry_price + risk * 2.0, 10)
        else:
            stop = round(entry_price * 0.95, 10)
            risk = entry_price - stop
            tp1  = round(entry_price + risk * 1.0, 10)
            tp2  = round(entry_price + risk * 2.0, 10)
        payload = {
            "symbol": symbol, "entry": entry_price, "stop": stop,
            "tp1": tp1, "tp2": tp2, "tp3": None, "sig_type": "smc",
            "sub_type": break_type, "source": source, "phase": phase,
        }
        if limit_price is not None:
            payload["limit_price"] = limit_price
        if signal_price is not None:
            payload["signal_price"] = signal_price
        headers = {"Content-Type": "application/json"}
        if PORTFOLIO_TOKEN:
            headers["Authorization"] = f"Bearer {PORTFOLIO_TOKEN}"
        r = requests.post(f"{PORTFOLIO_URL}/api/signal",
                          json=payload, headers=headers, timeout=5)
        if r.status_code == 201:
            sig_id = r.json().get("id", "")
            print(f"[PORTFOLIO] {source} sinyal gönderildi: {symbol} ({phase}) id={sig_id}", flush=True)
            return sig_id
        elif r.status_code == 409:
            print(f"[PORTFOLIO] {source} zaten açık: {symbol}", flush=True)
        else:
            print(f"[PORTFOLIO] HTTP {r.status_code}: {r.text[:80]}", flush=True)
    except Exception as e:
        print(f"[PORTFOLIO] Hata: {e}", flush=True)
    return ""

# ============================================================
# 4) PİYASA LİSTESİ + COİN ADI
# ============================================================
def get_clean_symbols():
    try:
        exchange.load_markets()
        result = []
        for symbol, market in exchange.markets.items():
            if not (market["spot"] and market["active"] and symbol.endswith("/USDT")):
                continue
            if symbol in IGNORED_COINS:
                continue
            base = symbol.split("/")[0]
            if any(x in base for x in ["UP","DOWN","BULL","BEAR","3L","3S","2L","2S","5L","5S","10L","10S"]):
                continue
            result.append(symbol)
        print(f"  → {len(result)} aktif USDT spot coin", flush=True)
        return result
    except Exception as e:
        print(f"[HATA] Piyasa verisi: {e}")
        return []

def get_coin_name(symbol):
    try:
        market = exchange.markets.get(symbol, {})
        full = market.get("info", {}).get("baseAssetFullName", "")
        if not full:
            full = market.get("info", {}).get("baseAsset", symbol.split("/")[0])
        return full.strip()
    except Exception:
        return symbol.split("/")[0]

# ============================================================
# 5) TEKNİK İNDİKATÖRLER
# ============================================================
def calc_atr(df, period=14):
    hl  = df["high"] - df["low"]
    hpc = (df["high"] - df["close"].shift(1)).abs()
    lpc = (df["low"]  - df["close"].shift(1)).abs()
    tr  = pd.concat([hl, hpc, lpc], axis=1).max(axis=1)
    return tr.rolling(window=period).mean()

def calc_rsi(df, period=14):
    delta = df["close"].diff()
    gain  = delta.where(delta > 0, 0.0).ewm(alpha=1/period, adjust=False).mean()
    loss  = (-delta.where(delta < 0, 0.0)).ewm(alpha=1/period, adjust=False).mean()
    rs    = gain / loss.replace(0, 1e-10)
    return 100 - (100 / (1 + rs))

# ============================================================
# 5b) TICK SIZE YARDIMCI
# ============================================================
_tick_cache: dict = {}

def _get_tick_size(symbol):
    """PRICE_FILTER'dan (tick_size, precision) döner. Binance REST ile garanti alır."""
    if symbol in _tick_cache:
        return _tick_cache[symbol]

    # 1) ccxt markets önbelleği
    try:
        filters = exchange.markets.get(symbol, {}).get("info", {}).get("filters", [])
        for f in filters:
            if f.get("filterType") == "PRICE_FILTER":
                tick = float(f["tickSize"])
                if tick > 0:
                    precision = max(0, int(round(-math.log10(tick))))
                    _tick_cache[symbol] = (tick, precision)
                    return tick, precision
    except Exception:
        pass

    # 2) Binance REST yedek
    try:
        pair = symbol.replace("/", "")
        r = requests.get(
            "https://api.binance.com/api/v3/exchangeInfo",
            params={"symbol": pair}, timeout=5
        )
        if r.status_code == 200:
            for f in r.json().get("symbols", [{}])[0].get("filters", []):
                if f.get("filterType") == "PRICE_FILTER":
                    tick = float(f["tickSize"])
                    if tick > 0:
                        precision = max(0, int(round(-math.log10(tick))))
                        _tick_cache[symbol] = (tick, precision)
                        return tick, precision
    except Exception:
        pass

    return None, 8

def _choch_plus_one_tick(price, symbol):
    """CHoCH seviyesi + 1 tick — limit buy fiyatı."""
    tick, precision = _get_tick_size(symbol)
    if tick is None:
        print(f"[SMC] _choch_plus_one_tick: tick bulunamadı {symbol}, choch={price} aynen döndü", flush=True)
        return price, price
    floored = math.floor(price / tick) * tick
    limit   = round(floored + tick, precision)
    print(f"[SMC] _choch_plus_one_tick: {symbol} | choch={price} tick={tick} → limit={limit}", flush=True)
    return limit, tick

# ============================================================
# 6) Micro CHoCH tespiti (CHOCH_SWING=5, LuxAlgo uyumlu)
# ============================================================
def detect_micro_choch(df, choch_swing=CHOCH_SWING):
    """
    Döner: (break_type, break_direction, swing_trend, choch_level)
      choch_level = kırılan swing seviyesi = LuxAlgo CHoCH çizgisi
    """
    highs  = df["high"].values
    lows   = df["low"].values
    closes = df["close"].values
    n = len(df)

    if n < choch_swing + 10:
        return None, None, 0, None

    legs = [0] * n
    current_leg = 0
    for i in range(choch_swing, n):
        pivot_bar_high = highs[i - choch_swing]
        pivot_bar_low  = lows[i - choch_swing]
        window_high = max(highs[i - choch_swing + 1 : i + 1])
        window_low  = min(lows[i - choch_swing + 1 : i + 1])
        if pivot_bar_high > window_high:
            current_leg = 0
        elif pivot_bar_low < window_low:
            current_leg = 1
        legs[i] = current_leg

    swing_high_level = None; swing_high_crossed = True
    swing_low_level  = None; swing_low_crossed  = True
    swing_trend = 0
    break_type = None; break_direction = None
    choch_level = None

    for i in range(choch_swing + 1, n):
        prev_leg = legs[i - 1]; curr_leg = legs[i]
        if curr_leg != prev_leg:
            if curr_leg == 1:
                swing_low_level   = lows[i - choch_swing]
                swing_low_crossed = False
            elif curr_leg == 0:
                swing_high_level   = highs[i - choch_swing]
                swing_high_crossed = False

        if i < n - 1:
            c = closes[i]; c_prev = closes[i - 1]
            if (swing_high_level is not None and not swing_high_crossed
                    and c > swing_high_level and c_prev <= swing_high_level):
                swing_high_crossed = True; swing_trend = 1
            if (swing_low_level is not None and not swing_low_crossed
                    and c < swing_low_level and c_prev >= swing_low_level):
                swing_low_crossed = True; swing_trend = -1

        if i == n - 1:
            c = closes[i]; c_prev = closes[i - 1]
            if (swing_high_level is not None and not swing_high_crossed
                    and c > swing_high_level and c_prev <= swing_high_level):
                break_type      = "CHoCH" if swing_trend == -1 else "BOS"
                break_direction = "BULLISH"
                swing_trend     = 1
                choch_level     = swing_high_level
            if (swing_low_level is not None and not swing_low_crossed
                    and c < swing_low_level and c_prev >= swing_low_level):
                break_type      = "CHoCH" if swing_trend == 1 else "BOS"
                break_direction = "BEARISH"
                swing_trend     = -1
                choch_level     = swing_low_level

    return break_type, break_direction, swing_trend, choch_level, swing_low_level

# ============================================================
# 7b-2) [SMC-ESKİ] Swing dip dizilimi (düşüş duraklamış mı?)
# ============================================================
def _swing_lows_trend(df, choch_swing=CHOCH_SWING):
    """
    detect_micro_choch ile aynı leg/swing mantığı, ama son İKİ swing dip
    seviyesini de döner — düşüşün durup durmadığını (yükselen dip oluştu mu)
    anlamak için.

    Döner: (swing_trend, son_swing_dip, önceki_swing_dip)
    """
    highs  = df["high"].values
    lows   = df["low"].values
    closes = df["close"].values
    n = len(df)

    if n < choch_swing + 10:
        return 0, None, None

    legs = [0] * n
    current_leg = 0
    for i in range(choch_swing, n):
        pivot_high  = highs[i - choch_swing]
        pivot_low   = lows[i - choch_swing]
        window_high = max(highs[i - choch_swing + 1 : i + 1])
        window_low  = min(lows[i - choch_swing + 1 : i + 1])
        if pivot_high > window_high:
            current_leg = 0
        elif pivot_low < window_low:
            current_leg = 1
        legs[i] = current_leg

    swing_high_level = None; swing_high_crossed = True
    swing_low_level  = None; swing_low_crossed  = True
    prev_swing_low   = None
    swing_trend = 0

    for i in range(choch_swing + 1, n):
        if legs[i] != legs[i - 1]:
            if legs[i] == 1:
                prev_swing_low    = swing_low_level
                swing_low_level   = lows[i - choch_swing]
                swing_low_crossed = False
            else:
                swing_high_level   = highs[i - choch_swing]
                swing_high_crossed = False

        c, c_prev = closes[i], closes[i - 1]
        if (swing_high_level is not None and not swing_high_crossed
                and c > swing_high_level and c_prev <= swing_high_level):
            swing_high_crossed = True; swing_trend = 1
        if (swing_low_level is not None and not swing_low_crossed
                and c < swing_low_level and c_prev >= swing_low_level):
            swing_low_crossed = True; swing_trend = -1

    return swing_trend, swing_low_level, prev_swing_low

# ============================================================
# 8) ANALİZ — Bar kapanışında çalışır
# ============================================================
def _analyze_symbol(symbol):
    try:
        now = time.time()
        df  = bars_cache.get(symbol)
        if df is None or len(df) < 200:
            return

        # BTC filtreleri
        if check_btc_crash():
            scan_stats["btc_crash_skip"] += 1
            return
        # BTC downtrend filtresi kapalı (backtest: crash filtresi yeterli)
        # if check_btc_downtrend_active():
        #     scan_stats["btc_downtrend_skip"] += 1
        #     return
        # CHoCH tespiti
        micro_break, micro_dir, _, choch_level, swing_low = detect_micro_choch(df, CHOCH_SWING)

        if micro_break != "CHoCH" or micro_dir != "BULLISH":
            scan_stats["no_choch"] += 1
            return

        # Hacim filtresi: son bar / önceki 20 bar MA >= VOL_RATIO_MIN
        # shift(1): mevcut barı MA hesabından dışarıda bırak (backtest ile eşleşir)
        vol_ma_20 = df["volume"].rolling(20).mean().iloc[-2]
        if not vol_ma_20 or vol_ma_20 <= 0:
            return
        vol_ratio = float(df["volume"].iloc[-1]) / float(vol_ma_20)
        if vol_ratio < VOL_RATIO_MIN:
            scan_stats["vol_filter_skip"] += 1
            return

        # Cooldown kontrolü (24 saat)
        last_sent = get_last_sent(symbol, "choch_v2", "smc-v2")
        if now - last_sent < PHASE2_COOLDOWN:
            scan_stats["cooldown_skip"] += 1
            return

        # Sinyal hesaplamaları
        if choch_level is None:
            return  # backtest ile tutarlı: swing_high yoksa sinyal üretme

        price = float(df["close"].iloc[-1])

        # Limit buy fiyatı: CHoCH seviyesi + 1 tick (backtest ile aynı fill fiyatı)
        limit_price, _ = _choch_plus_one_tick(choch_level, symbol)
        entry = limit_price if limit_price else choch_level  # risk/tp hesabı fill fiyatından

        stop = round(swing_low * 0.995, 10) if swing_low is not None else round(entry * 0.95, 10)
        if stop >= entry:
            stop = round(entry * 0.95, 10)
        risk = max(entry - stop, entry * 0.01)
        tp1  = round(entry + risk * 1.0, 10)
        tp2  = round(entry + risk * 2.0, 10)

        base      = symbol.split("/")[0]
        coin_name = get_coin_name(symbol)
        _, prec = _get_tick_size(symbol)
        def _fmt(v): return f"{v:.{prec}f}"
        c_str  = _fmt(choch_level)   # ham CHoCH seviyesi (gösterim için)
        l_str  = _fmt(limit_price)   # CHoCH+1tick = gerçek fill = risk hesap bazı
        s_str  = _fmt(stop)
        t1_str = _fmt(tp1)
        t2_str = _fmt(tp2)
        p_str  = _fmt(price)

        msg = (
            f"🚀 <b>CHoCH — Legacy SMC</b>\n"
            f"<b>#{base}</b>  <i>{coin_name}</i>\n"
            f"<code>━━━━━━━━━━━━━━━━━━━━</code>\n"
            f"📍 <b>CHoCH seviyesi:</b> <code>{c_str}</code>\n"
            f"📊 <b>Anlık fiyat:</b> <code>{p_str}</code>\n"
            f"🔵 <b>Limit Alış:</b> <code>{l_str}</code>  ← CHoCH+1 tick\n"
            f"🎯 <b>TP1 (trailing aktifleşir):</b> <code>{t1_str}</code>\n"
            f"🚀 <b>TP2 (tam çıkış):</b> <code>{t2_str}</code>\n"
            f"🛑 <b>STOP:</b> <code>{s_str}</code>\n"
            f"<code>━━━━━━━━━━━━━━━━━━━━</code>\n"
            f"📊 <b>Hacim:</b> {vol_ratio:.2f}x (20 bar MA)\n"
            f"⏳ <b>RETEST BEKLENİYOR</b> — 48 saat içinde limit dolmadıysa otomatik iptal."
        )
        portfolio_id = send_to_portfolio(
            symbol, entry, 0, "choch_v2", "smc-v2", micro_break, stop_price=stop, limit_price=limit_price, signal_price=price
        )
        mark_sent(symbol, "choch_v2", "smc-v2")

        if PORTFOLIO_URL and portfolio_id:
            try:
                sig = {
                    "symbol":      symbol, "type": "smc", "source": "smc",
                    "entry":       entry,  "stop": stop,
                    "tp1":         tp1,    "tp2": tp2,
                    "limit_price": limit_price,
                    "vol_ratio":   round(vol_ratio, 2),
                    "break_type":  micro_break,
                }
                hdrs = {"Content-Type": "application/json"}
                if PORTFOLIO_TOKEN:
                    hdrs["Authorization"] = f"Bearer {PORTFOLIO_TOKEN}"
                requests.post(
                    f"{PORTFOLIO_URL}/api/analyze",
                    json={"signal": sig, "recent_count": 0, "sig_num": 0,
                          "portfolio_id": portfolio_id},
                    headers=hdrs, timeout=10,
                )
            except Exception as ae:
                print(f"[SMC ANALYZER] {ae}", flush=True)

        scan_stats["signal_v2"] += 1
        print(f"🟣 [Legacy SMC] {symbol} | choch:{entry:.8g} | limit:{limit_price:.8g} | stop:{stop:.8g} | vol:{vol_ratio:.2f}x", flush=True)

    except Exception as e:
        print(f"[HATA] {symbol}: {e}", flush=True)

# ============================================================
# 10) BAR KAPANIŞINDA GÜNCELLEME (WebSocket)
# ============================================================
async def on_1h_close(symbol, o, h, l, c, v, ts_ms):
    global ws_1h_closes
    ws_1h_closes += 1

    if not bootstrap_done:
        return
    if symbol not in bars_cache:
        return

    tstamp = pd.to_datetime(ts_ms, unit="ms", utc=True)
    df = bars_cache[symbol]
    df.loc[tstamp, ["open", "high", "low", "close", "volume"]] = [o, h, l, c, v]
    df = df.sort_index()
    df = df[~df.index.duplicated(keep='last')]
    if len(df) > KEEP_BARS:
        df = df.iloc[-KEEP_BARS:]
    bars_cache[symbol] = df

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, lambda: _analyze_symbol(symbol))

# ============================================================
# 11) WEBSOCKET
# ============================================================
def to_ws(symbol):
    return symbol.replace("/", "").lower()

async def ws_chunk(symbols):
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
                        if not k.get("x", False):
                            continue
                        sym = data.get("data", {}).get("s", "").upper().replace("USDT", "/USDT")
                        try:
                            await on_1h_close(sym,
                                float(k["o"]), float(k["h"]), float(k["l"]), float(k["c"]),
                                float(k["v"]), int(k["t"]))
                        except Exception as e:
                            print(f"on_1h_close hata [{sym}]: {str(e)[:80]}", flush=True)
                finally:
                    ping_task.cancel()
        except Exception as e:
            retry  += 1
            backoff = min(60, 5 * (2 ** min(retry, 4)))
            print(f"WS koptu → {backoff}s: {str(e)[:50]}", flush=True)
            await asyncio.sleep(backoff)

async def ws_all(symbols):
    tasks = [
        asyncio.create_task(ws_chunk(symbols[i:i + WS_STREAM_CHUNK]))
        for i in range(0, len(symbols), WS_STREAM_CHUNK)
    ]
    await asyncio.gather(*tasks)

async def periodic_summary():
    global ws_1h_closes
    await asyncio.sleep(3600)
    while True:
        print(f"\n--- SMC TARAMA ÖZETİ ---", flush=True)
        print(f"Cached coin      : {len(bars_cache)}", flush=True)
        print(f"WS bar kapandı   : {ws_1h_closes}", flush=True)
        print(f"CHoCH yok        : {scan_stats.get('no_choch', 0)}", flush=True)
        print(f"Hacim filtresi   : {scan_stats.get('vol_filter_skip', 0)}", flush=True)
        print(f"Cooldown skip    : {scan_stats.get('cooldown_skip', 0)}", flush=True)
        print(f"BTC crash skip   : {scan_stats.get('btc_crash_skip', 0)}", flush=True)
        print(f"BTC downtrend    : {scan_stats.get('btc_downtrend_skip', 0)}", flush=True)
        print(f"Sinyal (V2)      : {scan_stats.get('signal_v2', 0)}", flush=True)
        print(f"------------------------\n", flush=True)
        scan_stats.clear()
        ws_1h_closes = 0
        await asyncio.sleep(3600)

# ============================================================
# 12) ANA ÇALIŞTIRICI
# ============================================================
async def main():
    global sent_signals
    sent_signals = load_signals()

    threading.Thread(target=run_flask, daemon=True).start()

    print("=" * 60)
    print("🚀  SMC v23 — CHoCH + vol≥5x + BTC filtreli")
    print("=" * 60)
    print(f"  Timeframe       : {TIMEFRAME}")
    print(f"  Tetikleyici     : WebSocket (1H bar kapanışında)")
    print(f"  Bootstrap       : {BOOTSTRAP_BARS} bar ({BOOTSTRAP_BARS//24} gün)")
    print(f"  Swing (CHoCH)   : {CHOCH_SWING} bar (micro, LuxAlgo uyumlu)")
    print(f"  Sinyal Şartları : Bullish CHoCH + vol≥{VOL_RATIO_MIN}x + BTC crash/downtrend yok")
    print(f"  Stop            : Swing low × 0.995 (fallback: entry × 0.95)")
    print(f"  TP Yapısı       : TP1=risk×1.0 | TP2=risk×2.0")
    print(f"  Cooldown        : {PHASE2_COOLDOWN//3600}h per coin")
    print("=" * 60 + "\n")

    for attempt in range(3):
        try:
            exchange.load_markets()
            print(f"Markets yüklendi: {len(exchange.markets)} piyasa", flush=True)
            break
        except Exception as e:
            print(f"Markets hata (deneme {attempt+1}): {e}", flush=True)
            await asyncio.sleep(5)

    symbols = get_clean_symbols()
    print(f"{len(symbols)} coin bulundu", flush=True)

    if not symbols:
        print("⚠️ Coin listesi boş! 30 saniye bekleyip tekrar denenecek.", flush=True)
        await asyncio.sleep(30)
        symbols = get_clean_symbols()
        print(f"Tekrar deneme: {len(symbols)} coin bulundu", flush=True)

    await bootstrap_all(symbols)

    print(f"\n🔌 WebSocket bağlantıları kuruluyor ({len(symbols)} sembol, {WS_STREAM_CHUNK}'erli gruplar)...", flush=True)
    await asyncio.gather(
        ws_all(symbols),
        periodic_summary(),
    )

if __name__ == "__main__":
    asyncio.run(main())
