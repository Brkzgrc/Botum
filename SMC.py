import asyncio
import json
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
    active = len([s for s, v in discount_active.items() if v])
    btc_ema = "BTC EMA21 ✅" if btc_ema21_cache.get("above") else "BTC EMA21 ❌"
    struc = "4H PAUSE 🚨" if btc_4h_structural_cache.get("paused") else "4H OK ✅"
    return (f"SMC v17 WS — 4H Crash Filter | {boot_status} | {cached} coin cached | "
            f"{active} discount aktif | {btc_ema} | {struc} | {ws_1h_closes} bar kapandı"), 200

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
MIN_VOLUME_24H   = float(os.getenv("MIN_VOLUME_24H", "5000000"))  # Render env'den kontrol

SWING_LENGTH     = 50   # Discount zone hesabı için (makro)
CHOCH_SWING      = 5    # Micro CHoCH tespiti için (LuxAlgo ile aynı)

BOOTSTRAP_BARS   = 2500
KEEP_BARS        = 2500

PHASE1_RSI       = 30
PHASE1_DEPTH     = 85
PHASE1_COOLDOWN  = 86400

PHASE2_COOLDOWN  = 86400

SIGNALS_FILE     = "sent_signals.json"
WS_STREAM_CHUNK  = 120

IGNORED_COINS = set([
    'UP/USDT','DOWN/USDT','BEAR/USDT','BULL/USDT',
    'USDC/USDT','TUSD/USDT','FDUSD/USDT','DAI/USDT','USDP/USDT',
    'USDE/USDT','UST/USDT','USD/USDT','XUSD/USDT','USD1/USDT','BFUSD/USDT',
    'USTC/USDT','BUSD/USDT','FRAX/USDT','LUSD/USDT','GUSD/USDT','SUSD/USDT',
    'USDS/USDT','USDX/USDT','USDD/USDT','CUSD/USDT','OUSD/USDT','MUSD/USDT',
    'U/USDT',
    'EUR/USDT','TRY/USDT','GBP/USDT','BRL/USDT','RUB/USDT',
    'AUD/USDT','BIDR/USDT','IDRT/USDT','VAI/USDT',
    'PAXG/USDT','WBTC/USDT','WETH/USDT','WBNB/USDT','BETH/USDT',
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

discount_active      = {}
discount_active_lock = threading.Lock()

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

    detected = 0
    for sym, df in bars_cache.items():
        try:
            smc = luxalgo_smc(df, SWING_LENGTH)
            if smc and smc['in_discount']:
                with discount_active_lock:
                    discount_active[sym] = True
                detected += 1
        except Exception:
            pass
    if detected:
        print(f"📍 Bootstrap: {detected} coin discount zone'da → bayrak set edildi", flush=True)

    bootstrap_done = True

# ============================================================
# 1c) BTC FİLTRELERİ
# ============================================================
btc_crash_cache = {"crashing": False, "updated": 0}
btc_ema21_cache = {"above": False, "updated": 0}

BTC_CRASH_PCT = 3.0
BTC_CRASH_TTL = 1800
BTC_EMA21_TTL = 1800

# ── YENİ: 4H Yapısal Kırılma Cache ──────────────────────────────
# Level 1 (paused=True):
#   Son 2 kapanmış 4H mum EMA21 altında (2. daha düşük) +
#   en son mumun çoğunluğu (>%50) EMA50 altında
#   → Yeni sinyal üretilmez
#
# Level 2 (send_alert=True):
#   Level 1 + önceki mumun da çoğunluğu EMA50 altında
#   → Telegram'a bir kez uyarı gönderilir
btc_4h_structural_cache = {"paused": False, "level2_alerted": False, "updated": 0}
BTC_4H_STRUCTURAL_TTL = 3600  # Saatte bir kontrol

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

def check_btc_ema21():
    now = time.time()
    if now - btc_ema21_cache["updated"] < BTC_EMA21_TTL:
        return btc_ema21_cache["above"]
    try:
        bars = exchange.fetch_ohlcv("BTC/USDT", timeframe="1h", limit=30)
        if len(bars) < 22:
            btc_ema21_cache["above"] = False
        else:
            closes = [float(b[4]) for b in bars]
            k = 2 / (21 + 1)
            ema = closes[0]
            for c in closes[1:]:
                ema = c * k + ema * (1 - k)
            price = closes[-1]
            above = price > ema
            btc_ema21_cache["above"] = above
            status = "üzerinde ✅" if above else "altında ❌"
            print(f"📊 BTC EMA21: {ema:.1f} | Fiyat: {price:.1f} | {status}", flush=True)
    except Exception as e:
        print(f"BTC EMA21 check hata: {e}", flush=True)
        btc_ema21_cache["above"] = False
    btc_ema21_cache["updated"] = now
    return btc_ema21_cache["above"]

def check_btc_4h_structural():
    """
    BTC 4H yapısal kırılma tespiti.

    Döner: (paused: bool, send_alert: bool)
      paused     = True  → Yeni sinyal üretme (Level 1)
      send_alert = True  → Telegram uyarısı gönder (Level 2, sadece ilk tespit)

    Level 1 şartları:
      1. Son 2 kapanmış 4H mum kapanışı EMA21 altında
      2. 2. (en son) mum 1.'den (önceki) daha düşük kapandı
      3. En son mumun çoğunluğu (>%50) EMA50 altında
         Formül: (EMA50 - low) / (high - low) > 0.5

    Level 2 şartları (Level 1 +):
      4. Önceki mumun da çoğunluğu EMA50 altında (aynı formül)
    """
    now = time.time()
    if now - btc_4h_structural_cache["updated"] < BTC_4H_STRUCTURAL_TTL:
        # Cached sonuç — alert yeni değil
        return btc_4h_structural_cache["paused"], False, None

    paused     = False
    send_alert = False
    levels     = None

    try:
        bars = exchange.fetch_ohlcv("BTC/USDT", timeframe="4h", limit=60)
        if len(bars) < 55:
            btc_4h_structural_cache["updated"] = now
            return False, False

        closes = pd.Series([float(b[4]) for b in bars])
        ema21  = closes.ewm(span=21, adjust=False).mean()
        ema50  = closes.ewm(span=50, adjust=False).mean()

        # Son 2 KAPANMIŞ bar:
        #   bars[-1] → halen açık olabilecek mevcut 4H bar (kullanma)
        #   bars[-2] → en son kapanmış 4H bar  (c1)
        #   bars[-3] → 2. en son kapanmış 4H bar (c2)
        c1_h, c1_l, c1_c = float(bars[-2][2]), float(bars[-2][3]), float(bars[-2][4])
        c2_h, c2_l, c2_c = float(bars[-3][2]), float(bars[-3][3]), float(bars[-3][4])
        e21_c1, e21_c2 = float(ema21.iloc[-2]), float(ema21.iloc[-3])
        e50_c1, e50_c2 = float(ema50.iloc[-2]), float(ema50.iloc[-3])
        levels = {"btc": c1_c, "ema21": e21_c1, "ema50": e50_c1}

        # Koşul 1: Her iki kapanış EMA21 altında
        cond_ema21 = (c1_c < e21_c1) and (c2_c < e21_c2)

        # Koşul 2: En son mum öncekinden daha düşük kapandı
        cond_lower = c1_c < c2_c

        # Koşul 3: c1 mumunun çoğunluğu EMA50 altında
        range1 = c1_h - c1_l
        if range1 > 0:
            cond_maj_c1 = (e50_c1 - c1_l) / range1 > 0.5
        else:
            cond_maj_c1 = c1_c < e50_c1

        # Koşul 4 (Level 2): c2 mumunun da çoğunluğu EMA50 altında
        range2 = c2_h - c2_l
        if range2 > 0:
            cond_maj_c2 = (e50_c2 - c2_l) / range2 > 0.5
        else:
            cond_maj_c2 = c2_c < e50_c2

        level1 = cond_ema21 and cond_lower and cond_maj_c1
        level2 = level1 and cond_maj_c2

        paused = level1

        # Alert mantığı: level2 ilk kez tetiklendiyse gönder
        was_alerted = btc_4h_structural_cache["level2_alerted"]
        if level2 and not was_alerted:
            send_alert = True
            btc_4h_structural_cache["level2_alerted"] = True
        elif not level1:
            # Şart kalktı → sonraki tetiklenme için sıfırla
            btc_4h_structural_cache["level2_alerted"] = False

        status = f"Level{'2' if level2 else '1'} — PAUSE 🚨" if level1 else "NORMAL ✅"
        print(
            f"🔍 BTC 4H Yapısal: {status} | "
            f"c1={c1_c:.0f} EMA21={e21_c1:.0f} EMA50={e50_c1:.0f} | "
            f"c2={c2_c:.0f} EMA21={e21_c2:.0f}",
            flush=True
        )

    except Exception as e:
        print(f"BTC 4H structural check hata: {e}", flush=True)

    btc_4h_structural_cache["paused"]  = paused
    btc_4h_structural_cache["updated"] = now
    return paused, send_alert, levels

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

def send_to_portfolio(symbol, entry_price, atr_val, phase, source, break_type=""):
    """entry_price = choch_level (CHoCH seviyesi, LuxAlgo çizgisi)"""
    if not PORTFOLIO_URL:
        return
    try:
        stop = round(entry_price - atr_val * 4.0, 10)
        tp1  = round(entry_price + atr_val * 4.0, 10)
        tp2  = round(entry_price + atr_val * 6.0, 10)
        payload = {
            "symbol": symbol, "entry": entry_price, "stop": stop,
            "tp1": tp1, "tp2": tp2, "sig_type": "smc",
            "sub_type": break_type, "source": source, "phase": phase,
        }
        headers = {"Content-Type": "application/json"}
        if PORTFOLIO_TOKEN:
            headers["Authorization"] = f"Bearer {PORTFOLIO_TOKEN}"
        r = requests.post(f"{PORTFOLIO_URL}/api/signal",
                          json=payload, headers=headers, timeout=5)
        if r.status_code == 201:
            print(f"[PORTFOLIO] {source} sinyal gönderildi: {symbol} ({phase})", flush=True)
        elif r.status_code == 409:
            print(f"[PORTFOLIO] {source} zaten açık: {symbol}", flush=True)
        else:
            print(f"[PORTFOLIO] HTTP {r.status_code}: {r.text[:80]}", flush=True)
    except Exception as e:
        print(f"[PORTFOLIO] Hata: {e}", flush=True)

# ============================================================
# 4) PİYASA LİSTESİ + COİN ADI
# ============================================================
def get_clean_symbols():
    try:
        exchange.load_markets()
        # Hacim verisi için tüm ticker'ları çek
        print("📊 24h hacimleri çekiliyor (volume filtresi)...", flush=True)
        tickers = exchange.fetch_tickers()
        result = []
        filtered_vol = 0
        for symbol, market in exchange.markets.items():
            if not (market["spot"] and market["active"] and symbol.endswith("/USDT")):
                continue
            if symbol in IGNORED_COINS:
                continue
            base = symbol.split("/")[0]
            if any(x in base for x in ["UP", "DOWN", "BULL", "BEAR"]):
                continue
            # Hacim filtresi (MIN_VOLUME_24H env variable)
            ticker = tickers.get(symbol, {})
            vol = ticker.get("quoteVolume") or 0
            if vol < MIN_VOLUME_24H:
                filtered_vol += 1
                continue
            result.append(symbol)
        print(f"  → Hacim filtresi ({MIN_VOLUME_24H/1e6:.0f}M): {filtered_vol} coin elendi", flush=True)
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
# 6) MUM FORMASYONU TESPİTİ
# ============================================================
def detect_candle_patterns(df):
    result = {"hammer": False, "engulfing": False, "doji": False, "morning_star": False}
    if len(df) < 4:
        return result
    c0 = df.iloc[-2]; c1 = df.iloc[-3]; c2 = df.iloc[-4]
    o0, h0, l0, cl0 = float(c0["open"]), float(c0["high"]), float(c0["low"]), float(c0["close"])
    o1, h1, l1, cl1 = float(c1["open"]), float(c1["high"]), float(c1["low"]), float(c1["close"])
    o2, h2, l2, cl2 = float(c2["open"]), float(c2["high"]), float(c2["low"]), float(c2["close"])
    body0 = abs(cl0 - o0); body1 = abs(cl1 - o1); body2 = abs(cl2 - o2)
    range0 = h0 - l0
    if range0 <= 0:
        return result
    upper_wick0 = h0 - max(cl0, o0)
    lower_wick0 = min(cl0, o0) - l0
    if body0 > 0 and lower_wick0 >= body0 * 2.0 and upper_wick0 <= body0 * 0.5 and (min(cl0, o0) - l0) / range0 >= 0.55:
        result["hammer"] = True
    if cl1 < o1 and cl0 > o0 and o0 <= cl1 and cl0 >= o1 and body0 >= body1 * 0.8:
        result["engulfing"] = True
    if body0 <= range0 * 0.08:
        result["doji"] = True
    avg_body = (body0 + body1 + body2) / 3 if (body0 + body1 + body2) > 0 else 1
    c2_mid = o2 - body2 / 2
    if cl2 < o2 and body2 >= avg_body and body1 <= avg_body * 0.5 and cl0 > o0 and body0 >= avg_body and cl0 >= c2_mid:
        result["morning_star"] = True
    return result

def candle_pattern_summary(patterns):
    found = []
    if patterns.get("morning_star"): found.append("🌅 Morning Star")
    if patterns.get("engulfing"):    found.append("🟢 Bullish Engulfing")
    if patterns.get("hammer"):       found.append("🔨 Hammer")
    if patterns.get("doji"):         found.append("⚖️ Doji")
    if not found:
        return ""
    if patterns.get("morning_star") or len(found) >= 2:
        power = "⚡⚡ ÇOK GÜÇLÜ TEYİT"
    elif patterns.get("engulfing"):
        power = "⚡ GÜÇLÜ TEYİT"
    else:
        power = "✅ TEYİT"
    return f"{power}: {' + '.join(found)}"

# ============================================================
# 7a) LuxAlgo SMC — Makro (discount zone, SWING_LENGTH=50)
# ============================================================
def luxalgo_smc(df, swing_length=SWING_LENGTH):
    highs  = df["high"].values
    lows   = df["low"].values
    closes = df["close"].values
    n = len(df)

    if n < swing_length + 10:
        return None

    legs = [0] * n
    current_leg = 0
    for i in range(swing_length, n):
        pivot_bar_high = highs[i - swing_length]
        pivot_bar_low  = lows[i - swing_length]
        window_high = max(highs[i - swing_length + 1 : i + 1])
        window_low  = min(lows[i - swing_length + 1 : i + 1])
        if pivot_bar_high > window_high:
            current_leg = 0
        elif pivot_bar_low < window_low:
            current_leg = 1
        legs[i] = current_leg

    swing_high_level = None; swing_high_crossed = True
    swing_low_level  = None; swing_low_crossed  = True
    swing_trend = 0
    trailing_top = None; trailing_bottom = None

    for i in range(swing_length, n):
        prev_leg = legs[i - 1] if i > 0 else 0; curr_leg = legs[i]
        if curr_leg != prev_leg:
            if curr_leg == 1:
                swing_low_level   = lows[i - swing_length]
                swing_low_crossed = False
                trailing_bottom   = swing_low_level
            elif curr_leg == 0:
                swing_high_level   = highs[i - swing_length]
                swing_high_crossed = False
                trailing_top       = swing_high_level
        if trailing_top    is not None and highs[i] > trailing_top:    trailing_top    = highs[i]
        if trailing_bottom is not None and lows[i]  < trailing_bottom: trailing_bottom = lows[i]

        if i < n - 1:
            c = closes[i]; c_prev = closes[i - 1]
            if (swing_high_level is not None and not swing_high_crossed
                    and c > swing_high_level and c_prev <= swing_high_level):
                swing_high_crossed = True
                swing_trend = 1
            if (swing_low_level is not None and not swing_low_crossed
                    and c < swing_low_level and c_prev >= swing_low_level):
                swing_low_crossed = True
                swing_trend = -1

    top    = trailing_top    if trailing_top    is not None else max(highs)
    bottom = trailing_bottom if trailing_bottom is not None else min(lows)

    if top == bottom:
        return None

    depth = (top - closes[-1]) / (top - bottom) * 100
    discount_top    = 0.55 * top + 0.45 * bottom
    discount_bottom = bottom

    return {
        'top':             top,
        'bottom':          bottom,
        'equilibrium':     round(0.5 * top + 0.5 * bottom, 10),
        'discount_top':    round(discount_top, 10),
        'discount_bottom': round(discount_bottom, 10),
        'premium_top':     round(top, 10),
        'premium_bottom':  round(0.95 * top + 0.05 * bottom, 10),
        'depth':           round(depth, 2),
        'in_discount':     closes[-1] <= discount_top,
        'swing_high':      swing_high_level,
        'swing_low':       swing_low_level,
        'swing_trend':     swing_trend,
        'break_type':      None,
        'break_direction': None,
    }

# ============================================================
# 7b) Micro CHoCH tespiti (CHOCH_SWING=5, LuxAlgo uyumlu)
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

    return break_type, break_direction, swing_trend, choch_level

# ============================================================
# 8) MESAJ ŞABLONLARI
# ============================================================
def build_phase2_msg(symbol, coin_name, choch_price, bar_close, ma200, dist_ma,
                     rsi, raw_atr, atr_ratio, depth, smc_data,
                     strategy_label, strategy_note, patterns, source_label, atr_val=None):
    """
    choch_price : CHoCH seviyesi (kırılan swing high = LuxAlgo çizgisi) — GİRİŞ FİYATI
    bar_close   : CHoCH barının kapanışı — bilgi amaçlı
    """
    base = symbol.split("/")[0]
    bt = smc_data['break_type']
    icon = "✅" if bt == "CHoCH" else "🔄"
    strength = ("🔥 <b>GÜÇLÜ — CHoCH (Trend Döndü)</b>" if bt == "CHoCH"
                else "💪 <b>ORTA — BOS (Trend Devam)</b>")

    cp_str  = f"{choch_price:.10f}".rstrip("0").rstrip(".")
    bc_str  = f"{bar_close:.10f}".rstrip("0").rstrip(".")
    m_str   = f"{ma200:.10f}".rstrip("0").rstrip(".")

    if atr_val:
        stop_val = round(choch_price - atr_val * 4.0, 10)
        tp1_val  = round(choch_price + atr_val * 4.0, 10)
        tp2_val  = round(choch_price + atr_val * 6.0, 10)
        stop_str = f"{stop_val:.10f}".rstrip("0").rstrip(".")
        tp1_str  = f"{tp1_val:.10f}".rstrip("0").rstrip(".")
        tp2_str  = f"{tp2_val:.10f}".rstrip("0").rstrip(".")
        tp_block = f"🎯 <b>TP1:</b> <code>{tp1_str}</code>\n🚀 <b>TP2:</b> <code>{tp2_str}</code>\n🛑 <b>STOP:</b> <code>{stop_str}</code>\n"
    else:
        tp_block = ""

    dt = smc_data['discount_top']
    db = smc_data['discount_bottom']
    dt_str = f"{dt:.10f}".rstrip("0").rstrip(".")
    db_str = f"{db:.10f}".rstrip("0").rstrip(".")

    pattern_line = candle_pattern_summary(patterns)
    pattern_block = ""
    if pattern_line:
        pattern_block = (f"\n<code>━━━━━━━━━━━━━━━━━━━━</code>\n"
                         f"📊 <b>MUM FORMASYONU</b>\n<b>{pattern_line}</b>\n"
                         f"<i>Yapısal kırılım mum formasyonuyla teyit edildi.</i>")

    entry_msg = ("🟢 <b>GİRİŞ DEĞERLENDİR!</b> Risk yönetimini unutma." if bt == "CHoCH"
                 else "⚠️ <b>DİKKATLİ OL:</b> BOS — trend hâlâ devam ediyordu.")
    if pattern_line and bt == "CHoCH":
        entry_msg = "🟢🟢 <b>GÜÇLÜ GİRİŞ SİNYALİ!</b> Formasyon + CHoCH kombinasyonu."

    return (
        f"🚀🚀🚀 <b>CHoCH / BOS</b> 🚀🚀🚀\n<b>#{base}</b>  <i>{coin_name}</i>\n"
        f"<code>━━━━━━━━━━━━━━━━━━━━</code>\n⚡ <b>AŞAMA 2 — YAPISAL KIRILIM</b>\n"
        f"🎯 <b>SİNYAL GÜCÜ:</b> {strength}\n"
        f"🏷 <b>KAYNAK:</b> {source_label}\n"
        f"📈 <b>STRATEJİ:</b> {strategy_label}\n<code>━━━━━━━━━━━━━━━━━━━━</code>\n\n"
        f"📍 <b>CHoCH Seviyesi:</b> <code>{cp_str}</code>\n"
        f"💵 <b>Bar kapanış:</b> <code>{bc_str}</code>\n"
        f"{tp_block}"
        f"<code>━━━━━━━━━━━━━━━━━━━━</code>\n"
        f"📊 <b>200 MA:</b> <code>{m_str}</code> (<b>%{round(dist_ma, 1)}</b>)\n"
        f"🌀 <b>RSI (14):</b> <b>{round(rsi, 2)}</b>\n"
        f"🌋 <b>ATR:</b> <code>{raw_atr}</code> (%{round(atr_ratio, 2)})\n"
        f"📉 <b>DISCOUNT ZONE:</b> <code>{db_str}</code> — <code>{dt_str}</code>\n\n"
        f"<code>━━━━━━━━━━━━━━━━━━━━</code>\n"
        f"{icon} <b>{bt}:</b> Swing yapısal kırılım ({smc_data['break_direction']})\n"
        f"📐 <b>Swing Trend:</b> {'BULLISH' if smc_data['swing_trend']==1 else 'BEARISH'}"
        f"{pattern_block}\n\n{strategy_note}\n\n{entry_msg}")

# ============================================================
# 9) ANALİZ — Bar kapanışında çalışır
# ============================================================
def _analyze_symbol(symbol):
    try:
        now = time.time()
        df  = bars_cache.get(symbol)
        if df is None or len(df) < 200:
            return

        coin_name = get_coin_name(symbol)

        # ── 4H Yapısal Kırılma Kontrolü (YENİ v17) ───────────────────────
        # Sadece 1 saatte bir API çağrısı yapar (TTL ile cache'lenir).
        # paused_4h  = True → Level 1: yeni sinyal üretme
        # send_4h_alert = True → Level 2: Telegram uyarısı gönder (ilk tetiklenme)
        paused_4h, send_4h_alert, levels_4h = check_btc_4h_structural()

        if send_4h_alert:
            lvl_str = ""
            if levels_4h:
                lvl_str = (
                    f"📊 BTC: <b>${levels_4h['btc']:,.0f}</b> | "
                    f"EMA21: ${levels_4h['ema21']:,.0f} | "
                    f"EMA50: ${levels_4h['ema50']:,.0f}\n\n"
                )
            alert_msg = (
                "🚨 <b>BTC 4H YAPISAL KIRILMA — DİKKAT!</b>\n\n"
                + lvl_str +
                "⚠️ İki ardışık 4H mum <b>EMA21 altında</b> kapandı (2. daha dip)\n"
                "⚠️ Her iki mumun <b>çoğunluğu EMA50 altında</b>\n\n"
                "🔴 Yeni sinyaller <b>duraklatıldı</b>\n"
                "📌 Açık pozisyonlar için stoplar devrede\n"
                "👁 Manuel takip önerilir"
            )
            send_telegram_msg(alert_msg)
            print("🚨 BTC 4H Yapısal Kırılma (Level 2) — Telegram uyarısı gönderildi", flush=True)

        df["atr"]   = calc_atr(df, 14)
        df["rsi"]   = calc_rsi(df, 14)
        df["ma200"] = df["close"].rolling(200).mean()

        price   = float(df["close"].iloc[-1])   # Bar kapanışı (referans)
        atr_val = float(df["atr"].iloc[-1])
        rsi     = float(df["rsi"].iloc[-1])
        ma200   = float(df["ma200"].iloc[-1])

        if any(pd.isna(x) for x in [atr_val, ma200]):
            return

        raw_atr   = f"{atr_val:.12f}".rstrip("0").rstrip(".")
        atr_ratio = (atr_val / price) * 100
        dist_ma   = ((price - ma200) / ma200) * 100

        if price > ma200:
            s_label = "⏳ <b>UZUN SÜRELİ (Trend Güçlü)</b>"
            s_note  = "👉 <i>Trend arkanda, kârı koşturmaya odaklan.</i>"
        else:
            s_label = "⚡ <b>KISA SÜRELİ (Vur-Kaç)</b>"
            s_note  = "👉 <i>Trend zayıf, dirençlerde hızlı kâr al.</i>"

        smc_data = luxalgo_smc(df, SWING_LENGTH)
        if smc_data is None:
            scan_stats["no_pivots"] += 1
            return

        depth       = smc_data['depth']
        in_discount = smc_data['in_discount']
        patterns    = detect_candle_patterns(df)

        # ── AŞAMA 1: Discount Zone Bildirimi ──────────────────────────────
        if in_discount:
            scan_stats["in_discount"] += 1
            # paused_4h = True → 4H yapısal kırılma var, Phase 1 de beklet
            if depth >= PHASE1_DEPTH and rsi <= PHASE1_RSI and not check_btc_crash() and check_btc_ema21() and not paused_4h:
                with discount_active_lock:
                    discount_active[symbol] = True
                last_p1 = get_last_sent(symbol, "discount", "smc-original")
                if now - last_p1 > PHASE1_COOLDOWN:
                    base = symbol.split("/")[0]
                    dt_str = f"{smc_data['discount_top']:.10f}".rstrip("0").rstrip(".")
                    db_str = f"{smc_data['discount_bottom']:.10f}".rstrip("0").rstrip(".")
                    msg = (f"📉 <b>#{base}</b> — Discount Zone\n"
                           f"💵 <code>{price:.8g}</code> | "
                           f"🌀 RSI: <b>{round(rsi, 1)}</b> | "
                           f"📊 Depth: %<b>{round(depth, 1)}</b>\n"
                           f"🗺 Zone: <code>{db_str}</code> — <code>{dt_str}</code>")
                    send_telegram_msg(msg)
                    mark_sent(symbol, "discount", "smc-original")
                    scan_stats["signal_phase1_smc-original"] += 1
                    print(f"📉 [DISCOUNT] {symbol} | Depth:%{round(depth,1)} | RSI:{round(rsi,1)}", flush=True)
        else:
            scan_stats["not_in_discount"] += 1

        # ── AŞAMA 2: Micro CHoCH (sadece discount_active olanlar) ─────────
        with discount_active_lock:
            is_active = discount_active.get(symbol, False)

        if not is_active:
            return

        if check_btc_crash():
            scan_stats["btc_crash_skip"] += 1
            return

        # 4H yapısal kırılma → Phase 2 sinyali üretme
        if paused_4h:
            scan_stats["btc_4h_pause_skip"] += 1
            return

        micro_break, micro_dir, micro_trend, choch_level = detect_micro_choch(df, CHOCH_SWING)

        if micro_break == "CHoCH" and micro_dir == "BULLISH":
            scan_stats["choch_found"] += 1
            last_p2 = get_last_sent(symbol, "choch", "smc-original")
            if now - last_p2 > PHASE2_COOLDOWN:
                # choch_level yoksa (beklenmedik durum) bar kapanışını kullan
                entry_price = choch_level if choch_level is not None else price

                smc_data['break_type']      = micro_break
                smc_data['break_direction'] = micro_dir
                smc_data['swing_trend']     = micro_trend

                msg = build_phase2_msg(
                    symbol, coin_name,
                    choch_price=entry_price,   # CHoCH seviyesi = giriş fiyatı
                    bar_close=price,           # Bar kapanışı = referans
                    ma200=ma200, dist_ma=dist_ma,
                    rsi=rsi, raw_atr=raw_atr, atr_ratio=atr_ratio,
                    depth=depth, smc_data=smc_data,
                    strategy_label=s_label, strategy_note=s_note,
                    patterns=patterns, source_label="SMC", atr_val=atr_val)

                send_telegram_msg(msg)
                mark_sent(symbol, "choch", "smc-original")
                # Portfolio: choch_level baz alınarak TP/SL hesaplanır
                send_to_portfolio(symbol, entry_price, atr_val, "choch", "smc-original", micro_break)
                scan_stats["signal_phase2_smc-original"] += 1
                pat_log = candle_pattern_summary(patterns)
                print(f"🚀 [CHoCH] {symbol} | CHoCH:{entry_price:.8g} | Close:{price:.8g} | RSI:{round(rsi,1)}"
                      + (f" | {pat_log}" if pat_log else ""), flush=True)
                with discount_active_lock:
                    discount_active[symbol] = False
            else:
                scan_stats["cooldown_p2_smc-original"] += 1

        elif micro_break == "BOS" and micro_dir == "BULLISH":
            scan_stats["bos_found"] += 1
        else:
            scan_stats["no_break"] += 1

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
        with discount_active_lock:
            active_count = len([s for s, v in discount_active.items() if v])
        total_signals = (
            scan_stats.get("signal_phase1_smc-original", 0) +
            scan_stats.get("signal_phase2_smc-original", 0)
        )
        print(f"\n--- SMC TARAMA ÖZETİ ---", flush=True)
        print(f"Cached coin      : {len(bars_cache)}", flush=True)
        print(f"WS bar kapandı   : {ws_1h_closes}", flush=True)
        print(f"Discount aktif   : {active_count} (CHoCH bekleniyor)", flush=True)
        print(f"Discount'ta      : {scan_stats.get('in_discount', 0)}", flush=True)
        print(f"Zone dışında     : {scan_stats.get('not_in_discount', 0)}", flush=True)
        print(f"Pivot yok        : {scan_stats.get('no_pivots', 0)}", flush=True)
        print(f"CHoCH:{scan_stats.get('choch_found', 0)}  BOS:{scan_stats.get('bos_found', 0)}  Kırılım yok:{scan_stats.get('no_break', 0)}", flush=True)
        print(f"Cooldown P1:{scan_stats.get('cooldown_p1_smc-original', 0)}  P2:{scan_stats.get('cooldown_p2_smc-original', 0)}", flush=True)
        print(f"BTC crash skip   : {scan_stats.get('btc_crash_skip', 0)}", flush=True)
        print(f"BTC 4H pause skip: {scan_stats.get('btc_4h_pause_skip', 0)}", flush=True)  # YENİ
        print(f"Toplam sinyal    : {total_signals}", flush=True)
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
    print("🚀  SMC v17 — 4H Yapısal Crash Filter")
    print("=" * 60)
    print(f"  Timeframe       : {TIMEFRAME}")
    print(f"  Tetikleyici     : WebSocket (1H bar kapanışında)")
    print(f"  Bootstrap       : {BOOTSTRAP_BARS} bar ({BOOTSTRAP_BARS//24} gün)")
    print(f"  Swing (Discount): {SWING_LENGTH} bar (makro)")
    print(f"  Swing (CHoCH)   : {CHOCH_SWING} bar (micro, LuxAlgo uyumlu)")
    print(f"  Min Hacim       : {MIN_VOLUME_24H/1e6:.0f}M USDT/24h (env: MIN_VOLUME_24H)")
    print(f"  Giriş Fiyatı    : CHoCH Seviyesi (kırılan swing high)")
    print(f"  Bar kapanışı    : Referans olarak mesajda gösterilir")
    print(f"  Aşama 1         : Discount + Depth>%{PHASE1_DEPTH} + RSI<{PHASE1_RSI} + BTC EMA21")
    print(f"  Aşama 2         : Micro CHoCH → Tam sinyal + Portfolio")
    print(f"  [YENİ] 4H Filter: 2 kapanmış 4H mum EMA21 altı (2.↓) + çoğunluk EMA50 altı")
    print(f"           Level 1 : Sinyal dur (paused)")
    print(f"           Level 2 : Level 1 + önceki mum da EMA50 çoğunluk altı → Telegram uyarı")
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
    print(f"{len(symbols)} coin bulundu (hacim filtresi sonrası)", flush=True)

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
