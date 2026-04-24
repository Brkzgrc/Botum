import ccxt
import pandas as pd
import requests
import time
import json
import os
import threading
from collections import Counter
from flask import Flask

app = Flask(__name__)

import logging
logging.getLogger("werkzeug").setLevel(logging.ERROR)

@app.route('/')
def health_check():
    boot_status = "BOOTSTRAPPING" if not bootstrap_done else "RUNNING"
    cached = len(bars_cache)
    return f"SMC Sniper v7 — LuxAlgo | {boot_status} | {cached} coin cached", 200

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
MIN_VOLUME_24H   = 5_000_000
SCAN_INTERVAL    = 900

# LuxAlgo varsayılanları
SWING_LENGTH     = 50

# Bootstrap ayarları
BOOTSTRAP_BARS   = 2500   # başlangıçta 2000 bar çek (~83 gün)
KEEP_BARS        = 2500   # bellekte tut

# Sinyal eşikleri
PHASE1_DEPTH     = 85
PHASE1_RSI       = 35
PHASE1_COOLDOWN  = 86400

PHASE2_DEPTH     = 65
PHASE2_RSI       = 48
PHASE2_COOLDOWN  = 86400

SIGNALS_FILE     = "sent_signals.json"

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

# ============================================================
# 1b) VERİ CACHE + BOOTSTRAP
# ============================================================
bars_cache = {}       # symbol → DataFrame
bootstrap_done = False

def fetch_bars(symbol, limit=BOOTSTRAP_BARS):
    """Binance'den OHLCV çeker. limit > 1000 ise döngüyle çeker."""
    all_bars = []
    since_ms = int((time.time() - limit * 3600) * 1000)  # limit saat geriye

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
        time.sleep(0.15)

    if not all_bars:
        return None

    df = pd.DataFrame(all_bars, columns=["timestamp","open","high","low","close","volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df.set_index("timestamp", inplace=True)
    df = df[~df.index.duplicated(keep='first')]
    return df


def bootstrap_all(symbols):
    """Başlangıçta tüm coinler için geçmiş veriyi çek."""
    global bootstrap_done
    print(f"📦 Bootstrap başladı: {len(symbols)} coin × {BOOTSTRAP_BARS} bar...", flush=True)
    ok = 0
    for idx, symbol in enumerate(symbols, 1):
        if idx % 50 == 0:
            print(f"  → {idx}/{len(symbols)}...", flush=True)
        df = fetch_bars(symbol, BOOTSTRAP_BARS)
        if df is not None and len(df) >= 200:
            bars_cache[symbol] = df.iloc[-KEEP_BARS:] if len(df) > KEEP_BARS else df
            ok += 1
        time.sleep(0.1)
    bootstrap_done = True
    print(f"✅ Bootstrap bitti: {ok}/{len(symbols)} coin yüklendi", flush=True)


def update_cache(symbol):
    """Her taramada sadece son barı çek ve cache'e ekle."""
    try:
        batch = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=5)
        if not batch:
            return

        new_df = pd.DataFrame(batch, columns=["timestamp","open","high","low","close","volume"])
        new_df["timestamp"] = pd.to_datetime(new_df["timestamp"], unit="ms", utc=True)
        new_df.set_index("timestamp", inplace=True)

        if symbol in bars_cache:
            df = bars_cache[symbol]
            # Yeni barları ekle, eski tekrarları kaldır
            combined = pd.concat([df, new_df])
            combined = combined[~combined.index.duplicated(keep='last')]
            combined = combined.sort_index()
            if len(combined) > KEEP_BARS:
                combined = combined.iloc[-KEEP_BARS:]
            bars_cache[symbol] = combined
        else:
            bars_cache[symbol] = new_df
    except Exception:
        pass


# ============================================================
# 1c) BTC TREND
# ============================================================
btc_trend_cache = {"trend": "UNKNOWN", "close": 0, "updated": 0}

def refresh_btc_trend():
    try:
        bars = exchange.fetch_ohlcv("BTC/USDT", timeframe="4h", limit=100)
        if len(bars) < 60:
            return
        df = pd.DataFrame(bars, columns=["ts","open","high","low","close","volume"])
        c = df["close"]
        e50 = c.ewm(span=50, adjust=False).mean()
        e200 = c.ewm(span=200, adjust=False).mean()
        last_c = float(c.iloc[-1])
        last_e50 = float(e50.iloc[-1])
        last_e200 = float(e200.iloc[-1])
        if last_c > last_e50 and last_e50 > last_e200:
            trend = "BULL"
        elif last_c > last_e50:
            trend = "YUKSELIS"
        elif last_c > last_e200:
            trend = "KARISIK"
        else:
            trend = "BEAR"
        btc_trend_cache["trend"] = trend
        btc_trend_cache["close"] = last_c
        btc_trend_cache["updated"] = time.time()
        print(f"BTC 4H: {trend} | Fiyat:{last_c:.0f} EMA50:{last_e50:.0f}", flush=True)
    except Exception as e:
        print(f"BTC trend hata: {e}", flush=True)

# ============================================================
# 2) SİNYAL HAFIZASI
# ============================================================
sent_signals = {}

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

def get_last_sent(symbol, phase):
    return sent_signals.get(symbol, {}).get(phase, 0.0)

def mark_sent(symbol, phase):
    if symbol not in sent_signals:
        sent_signals[symbol] = {}
    sent_signals[symbol][phase] = time.time()
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
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10)
        if r.status_code != 200:
            print(f"[TELEGRAM] HTTP {r.status_code}: {r.text[:120]}")
    except Exception as e:
        print(f"[TELEGRAM] Hata: {e}")

def send_to_portfolio(symbol, price, atr_val, phase, break_type=""):
    if not PORTFOLIO_URL:
        return
    try:
        stop = round(price - atr_val * 2.0, 10)
        tp1  = round(price + atr_val * 2.0, 10)
        tp2  = round(price + atr_val * 4.0, 10)
        payload = {
            "symbol": symbol, "entry": price, "stop": stop,
            "tp1": tp1, "tp2": tp2, "sig_type": "smc",
            "sub_type": break_type, "source": "smc", "phase": phase,
        }
        headers = {"Content-Type": "application/json"}
        if PORTFOLIO_TOKEN:
            headers["Authorization"] = f"Bearer {PORTFOLIO_TOKEN}"
        r = requests.post(f"{PORTFOLIO_URL}/api/signal",
                          json=payload, headers=headers, timeout=5)
        if r.status_code == 201:
            print(f"[PORTFOLIO] SMC sinyal gönderildi: {symbol} ({phase})", flush=True)
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
        result = []
        for symbol, market in exchange.markets.items():
            if not (market["spot"] and market["active"] and symbol.endswith("/USDT")):
                continue
            if symbol in IGNORED_COINS:
                continue
            base = symbol.split("/")[0]
            if any(x in base for x in ["UP", "DOWN", "BULL", "BEAR"]):
                continue
            result.append(symbol)
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
# 7) LuxAlgo SMC — BİREBİR PYTHON ÇEVİRİSİ
# ============================================================
def luxalgo_smc(df, swing_length=SWING_LENGTH):
    highs = df["high"].values
    lows  = df["low"].values
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

        new_leg_high = pivot_bar_high > window_high
        new_leg_low  = pivot_bar_low < window_low

        if new_leg_high:
            current_leg = 0
        elif new_leg_low:
            current_leg = 1
        legs[i] = current_leg

    swing_high_level = None
    swing_high_crossed = True
    swing_low_level = None
    swing_low_crossed = True
    swing_trend = 0

    trailing_top = None
    trailing_bottom = None
    break_type = None
    break_direction = None

    for i in range(swing_length + 1, n):
        prev_leg = legs[i - 1]
        curr_leg = legs[i]

        if curr_leg != prev_leg:
            if curr_leg == 1:
                swing_low_level = lows[i - swing_length]
                swing_low_crossed = False
                trailing_bottom = swing_low_level
            elif curr_leg == 0:
                swing_high_level = highs[i - swing_length]
                swing_high_crossed = False
                trailing_top = swing_high_level

        if trailing_top is not None and highs[i] > trailing_top:
            trailing_top = highs[i]
        if trailing_bottom is not None and lows[i] < trailing_bottom:
            trailing_bottom = lows[i]

        if i == n - 1:
            c_now = closes[i]
            c_prev = closes[i - 1]

            if (swing_high_level is not None and not swing_high_crossed
                    and c_now > swing_high_level and c_prev <= swing_high_level):
                swing_high_crossed = True
                if swing_trend == -1:
                    break_type = "CHoCH"
                else:
                    break_type = "BOS"
                break_direction = "BULLISH"
                swing_trend = 1

            if (swing_low_level is not None and not swing_low_crossed
                    and c_now < swing_low_level and c_prev >= swing_low_level):
                swing_low_crossed = True
                if swing_trend == 1:
                    break_type = "CHoCH"
                else:
                    break_type = "BOS"
                break_direction = "BEARISH"
                swing_trend = -1

    if trailing_top is None or trailing_bottom is None:
        return None
    if trailing_top <= trailing_bottom:
        return None

    top = trailing_top
    bottom = trailing_bottom
    equil = (top + bottom) / 2.0

    current_price = closes[-1]
    span = equil - bottom
    if span <= 0:
        return None

    depth = (equil - current_price) / span * 100

    return {
        'trailing_top': round(top, 10), 'trailing_bottom': round(bottom, 10),
        'equilibrium': round(equil, 10),
        'premium_top': round(top, 10), 'premium_bottom': round(0.95 * top + 0.05 * bottom, 10),
        'discount_top': round(0.95 * bottom + 0.05 * top, 10), 'discount_bottom': round(bottom, 10),
        'depth': round(depth, 2),
        'swing_high': swing_high_level, 'swing_low': swing_low_level,
        'swing_trend': swing_trend,
        'break_type': break_type, 'break_direction': break_direction,
    }

# ============================================================
# 8) MESAJ ŞABLONLARI
# ============================================================
def build_phase1_msg(symbol, coin_name, price, ma200, dist_ma,
                     rsi, raw_atr, atr_ratio, depth, smc_data,
                     strategy_label, strategy_note, trend_bias, patterns):
    base = symbol.split("/")[0]
    p_str = f"{price:.10f}".rstrip("0").rstrip(".")
    m_str = f"{ma200:.10f}".rstrip("0").rstrip(".")
    pattern_line = candle_pattern_summary(patterns)
    pattern_block = ""
    if pattern_line:
        pattern_block = (f"\n<code>━━━━━━━━━━━━━━━━━━━━</code>\n"
                         f"📊 <b>MUM FORMASYONU</b>\n{pattern_line}\n"
                         f"<i>Alıcı baskısı görülüyor — CHoCH/BOS yakın olabilir.</i>")
    return (
        f"🎯🎯🎯 <b>PUSU KURULDU</b> 🎯🎯🎯\n<b>#{base}</b>  <i>{coin_name}</i>\n"
        f"<code>━━━━━━━━━━━━━━━━━━━━</code>\n📍 <b>AŞAMA 1 — DERİN İNDİRİM</b>\n"
        f"📈 <b>STRATEJİ:</b> {strategy_label}\n<code>━━━━━━━━━━━━━━━━━━━━</code>\n\n"
        f"💵 <b>FİYAT:</b> <code>{p_str}</code>\n📊 <b>200 MA:</b> <code>{m_str}</code> (<b>%{round(dist_ma, 1)}</b>)\n"
        f"🌀 <b>RSI (14):</b> <b>{round(rsi, 2)}</b>\n🌋 <b>ATR (TAM):</b> <code>{raw_atr}</code>\n"
        f"📊 <b>ATR ORANI:</b> %{round(atr_ratio, 2)}\n📉 <b>İNDİRİM DERİNLİĞİ:</b> %{round(depth, 1)}\n"
        f"📐 <b>MEVCUT TREND:</b> {trend_bias}{pattern_block}\n"
        f"<code>━━━━━━━━━━━━━━━━━━━━</code>\n{strategy_note}\n\n"
        f"⏳ <b>CHoCH/BOS bekleniyor — tetik çekilmedi!</b>\n👁 TradingView'da izlemeye al.")

def build_phase2_msg(symbol, coin_name, price, ma200, dist_ma,
                     rsi, raw_atr, atr_ratio, depth, smc_data,
                     strategy_label, strategy_note, patterns):
    base = symbol.split("/")[0]
    bt = smc_data['break_type']
    icon = "✅" if bt == "CHoCH" else "🔄"
    strength = ("🔥 <b>GÜÇLÜ — CHoCH (Trend Döndü)</b>" if bt == "CHoCH"
                else "💪 <b>ORTA — BOS (Trend Devam)</b>")
    p_str = f"{price:.10f}".rstrip("0").rstrip(".")
    m_str = f"{ma200:.10f}".rstrip("0").rstrip(".")
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
        f"🚀🚀🚀 <b>TETİK ÇEKİLDİ</b> 🚀🚀🚀\n<b>#{base}</b>  <i>{coin_name}</i>\n"
        f"<code>━━━━━━━━━━━━━━━━━━━━</code>\n⚡ <b>AŞAMA 2 — YAPISAL KIRILIM</b>\n"
        f"🎯 <b>SİNYAL GÜCÜ:</b> {strength}\n📈 <b>STRATEJİ:</b> {strategy_label}\n"
        f"<code>━━━━━━━━━━━━━━━━━━━━</code>\n\n"
        f"💵 <b>FİYAT:</b> <code>{p_str}</code>\n📊 <b>200 MA:</b> <code>{m_str}</code> (<b>%{round(dist_ma, 1)}</b>)\n"
        f"🌀 <b>RSI (14):</b> <b>{round(rsi, 2)}</b>\n🌋 <b>ATR (TAM):</b> <code>{raw_atr}</code>\n"
        f"📊 <b>ATR ORANI:</b> %{round(atr_ratio, 2)}\n📉 <b>İNDİRİM DERİNLİĞİ:</b> %{round(depth, 1)}\n\n"
        f"<code>━━━━━━━━━━━━━━━━━━━━</code>\n"
        f"{icon} <b>{bt}:</b> Swing yapısal kırılım ({smc_data['break_direction']})\n"
        f"📐 <b>Swing Trend:</b> {'BULLISH' if smc_data['swing_trend']==1 else 'BEARISH'}"
        f"{pattern_block}\n\n{strategy_note}\n\n{entry_msg}")

# ============================================================
# 9) ANA ANALİZ MOTORU
# ============================================================
def analyze(symbol):
    try:
        now = time.time()

        # BTC BEAR'dayken SMC sinyali verme
        btc_trend = btc_trend_cache.get("trend", "UNKNOWN")
        if btc_trend == "BEAR":
            scan_stats["btc_bear_skip"] += 1
            return

        # Cache'den veri al, yoksa atla
        df = bars_cache.get(symbol)
        if df is None or len(df) < 200:
            scan_stats["no_cache"] += 1
            return

        # Son barı güncelle
        update_cache(symbol)
        df = bars_cache.get(symbol)
        if df is None or len(df) < 200:
            return

        coin_name = get_coin_name(symbol)

        df["atr"] = calc_atr(df, 14)
        df["rsi"] = calc_rsi(df, 14)
        df["ma200"] = df["close"].rolling(200).mean()

        price   = float(df["close"].iloc[-1])
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

        # ── LuxAlgo SMC hesapla — 2000 bar üzerinden ──
        smc_data = luxalgo_smc(df, SWING_LENGTH)
        if smc_data is None:
            scan_stats["no_pivots"] += 1
            return

        depth      = smc_data['depth']
        break_type = smc_data['break_type']
        break_dir  = smc_data['break_direction']

        if break_type == "CHoCH":
            scan_stats["choch_found"] += 1
        elif break_type == "BOS":
            scan_stats["bos_found"] += 1
        else:
            scan_stats["no_break"] += 1

        if depth < PHASE2_DEPTH:
            scan_stats["depth_low"] += 1

        patterns = detect_candle_patterns(df)
        trend_bias_str = ("BULLISH" if smc_data['swing_trend'] == 1
                          else "BEARISH" if smc_data['swing_trend'] == -1
                          else "NEUTRAL")

        # ── AŞAMA 2: Discount + CHoCH/BOS ──
        if break_type in ("CHoCH", "BOS") and break_dir == "BULLISH":
            if depth >= PHASE2_DEPTH and rsi < PHASE2_RSI:
                last_p2 = get_last_sent(symbol, "phase2")
                if now - last_p2 > PHASE2_COOLDOWN:
                    msg = build_phase2_msg(symbol, coin_name, price, ma200, dist_ma,
                                           rsi, raw_atr, atr_ratio, depth, smc_data,
                                           s_label, s_note, patterns)
                    send_telegram_msg(msg)
                    mark_sent(symbol, "phase2")
                    send_to_portfolio(symbol, price, atr_val, "phase2", break_type)
                    scan_stats["signal_phase2"] += 1
                    pat_log = candle_pattern_summary(patterns)
                    print(f"🚀 [AŞAMA 2] {symbol} | {break_type} | Depth:%{round(depth,1)} | RSI:{round(rsi,1)} | {len(df)} bar"
                          + (f" | {pat_log}" if pat_log else ""), flush=True)
                    return
                else:
                    scan_stats["cooldown_p2"] += 1

        # ── AŞAMA 1: Derin discount ──
        if depth >= PHASE1_DEPTH and rsi < PHASE1_RSI:
            last_p1 = get_last_sent(symbol, "phase1")
            if now - last_p1 > PHASE1_COOLDOWN:
                msg = build_phase1_msg(symbol, coin_name, price, ma200, dist_ma,
                                       rsi, raw_atr, atr_ratio, depth, smc_data,
                                       s_label, s_note, trend_bias_str, patterns)
                send_telegram_msg(msg)
                mark_sent(symbol, "phase1")
                send_to_portfolio(symbol, price, atr_val, "phase1")
                scan_stats["signal_phase1"] += 1
                pat_log = candle_pattern_summary(patterns)
                print(f"🎯 [AŞAMA 1] {symbol} | Depth:%{round(depth,1)} | RSI:{round(rsi,1)} | {len(df)} bar"
                      + (f" | {pat_log}" if pat_log else ""), flush=True)
                return
            else:
                scan_stats["cooldown_p1"] += 1

    except Exception as e:
        print(f"[HATA] {symbol}: {e}")

# ============================================================
# 10) ÇALIŞTIRICI DÖNGÜ
# ============================================================
def start_scanner():
    global sent_signals
    sent_signals = load_signals()

    threading.Thread(target=run_flask, daemon=True).start()

    print("=" * 50)
    print("🚀  SMC Sniper v7 — LuxAlgo + Bootstrap Cache")
    print("=" * 50)
    print(f"  Timeframe      : {TIMEFRAME}")
    print(f"  Bootstrap      : {BOOTSTRAP_BARS} bar ({BOOTSTRAP_BARS//24} gün)")
    print(f"  Swing length   : {SWING_LENGTH}")
    print(f"  Aşama 1        : Depth >%{PHASE1_DEPTH} + RSI <{PHASE1_RSI}")
    print(f"  Aşama 2        : Depth >%{PHASE2_DEPTH} + RSI <{PHASE2_RSI} + CHoCH/BOS")
    print(f"  BTC Filtre     : BEAR'da sinyal üretilmez")
    print("=" * 50 + "\n")

    for attempt in range(3):
        try:
            exchange.load_markets()
            print(f"Markets yüklendi: {len(exchange.markets)} piyasa", flush=True)
            break
        except Exception as e:
            print(f"Markets hata (deneme {attempt+1}): {e}", flush=True)
            time.sleep(5)
    
    # Coin listesi al
    symbols = get_clean_symbols()
    print(f"{len(symbols)} coin bulundu", flush=True)

    # Bootstrap — bir kez 2000 bar çek
    bootstrap_all(symbols)

    # BTC trend
    refresh_btc_trend()

    while True:
        refresh_btc_trend()
        btc_trend = btc_trend_cache.get("trend", "UNKNOWN")

        scan_stats.clear()
        print(f"\n🔄 {len(bars_cache)} coin taranıyor... | BTC: {btc_trend}")

        if btc_trend == "BEAR":
            print(f"⚠️ BTC BEAR — SMC sinyalleri devre dışı.")
        else:
            for symbol in list(bars_cache.keys()):
                analyze(symbol)
                time.sleep(0.3)  # cache'den okuyor, daha hızlı

        total_signals = scan_stats.get("signal_phase1", 0) + scan_stats.get("signal_phase2", 0)
        print(f"\n--- SMC TARAMA ÖZETİ ---", flush=True)
        print(f"Cached coin  : {len(bars_cache)}", flush=True)
        print(f"BTC Trend    : {btc_trend}", flush=True)
        if btc_trend != "BEAR":
            print(f"Cache yok    : {scan_stats.get('no_cache', 0)}", flush=True)
            print(f"Pivot yok    : {scan_stats.get('no_pivots', 0)}", flush=True)
            print(f"CHoCH:{scan_stats.get('choch_found', 0)}  BOS:{scan_stats.get('bos_found', 0)}  Kırılım yok:{scan_stats.get('no_break', 0)}", flush=True)
            print(f"Depth düşük  : {scan_stats.get('depth_low', 0)}", flush=True)
            print(f"Cooldown P1:{scan_stats.get('cooldown_p1', 0)}  P2:{scan_stats.get('cooldown_p2', 0)}", flush=True)
        print(f"Sinyal       : {total_signals}  (🎯 P1:{scan_stats.get('signal_phase1', 0)}  🚀 P2:{scan_stats.get('signal_phase2', 0)})", flush=True)
        print(f"------------------------", flush=True)

        print(f"✅ Tarama bitti. {SCAN_INTERVAL // 60} dakika bekleniyor.\n")
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    start_scanner()
