import ccxt
import pandas as pd
import requests
import time
import json
import os
import threading
from flask import Flask
import logging

# Flask'ın her saniye 'GET /' basmasını engellemek için:
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

app = Flask(__name__)

@app.route("/")
def home():
    return "SMC Sniper Running", 200

def run_flask():
    try:
        port = int(os.environ.get("PORT", 10000))
        print(f"[FLASK] running on port {port}", flush=True)
        app.run(host="0.0.0.0", port=port)
    except Exception as e:
        print(f"[FLASK ERROR] {e}", flush=True)
        
# ============================================================
# 1) AYARLAR
# ============================================================
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

TIMEFRAME        = "1h"
RANGE_LOOKBACK   = 30
MIN_VOLUME_24H   = 5_000_000
SCAN_INTERVAL    = 900        # 15 dakika

# ── Aşama eşikleri ──────────────────────────────────────────
PHASE1_DEPTH     = 85         # Hazırlık: fiyat bu derinliğin altında
PHASE1_RSI       = 35         # Hazırlık: RSI bu seviyenin altında
PHASE1_COOLDOWN  = 86400      # 24 saat — aynı coin için tekrar hazırlık atma

PHASE2_DEPTH     = 65         # Aksiyon: CHoCH geldiğinde minimum derinlik
PHASE2_RSI       = 48         # Aksiyon: RSI henüz aşırı alım olmamış
PHASE2_COOLDOWN  = 86400    # 24 saat — aksiyon sinyali için cooldown

SWING_SIZE       = 5          # LuxAlgo iç yapı pivot penceresi

SIGNALS_FILE     = "sent_signals.json"

IGNORED_COINS = {
    'UP/USDT','DOWN/USDT','BEAR/USDT','BULL/USDT','USDC/USDT','TUSD/USDT',
    'FDUSD/USDT','DAI/USDT','USDP/USDT','USDE/USDT','UST/USDT','USD/USDT',
    'XUSD/USDT','USD1/USDT','BFUSD/USDT','USTC/USDT','BUSD/USDT','FRAX/USDT',
    'LUSD/USDT','GUSD/USDT','SUSD/USDT','USDS/USDT','USDX/USDT','USDD/USDT',
    'CUSD/USDT','OUSD/USDT','MUSD/USDT','EUR/USDT','TRY/USDT','GBP/USDT',
    'BRL/USDT','RUB/USDT','AUD/USDT','BIDR/USDT','IDRT/USDT','VAI/USDT',
    'PAXG/USDT','WBTC/USDT','WETH/USDT','WBNB/USDT','BETH/USDT','BTCB/USDT','HBTC/USDT',
}

exchange = ccxt.binance()

# ============================================================
# 2) SİNYAL HAFIZASI
# ============================================================
# Yapı: { "BTCUSDT": { "phase1": 1720000000.0, "phase2": 1720003600.0} }
sent_signals: dict = {}

def load_signals() -> dict:
    if os.path.exists(SIGNALS_FILE):
        try:
            with open(SIGNALS_FILE) as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_signals(data: dict):
    try:
        with open(SIGNALS_FILE, "w") as f:
            json.dump(data, f)
    except Exception as e:
        print(f"[UYARI] Sinyal kaydedilemedi: {e}")

def get_last_sent(symbol: str, phase: str) -> float:
    return sent_signals.get(symbol, {}).get(phase, 0.0)

def mark_sent(symbol: str, phase: str):
    if symbol not in sent_signals:
        sent_signals[symbol] = {}
    sent_signals[symbol][phase] = time.time()
    save_signals(sent_signals)

# ============================================================
# 3) TELEGRAM
# ============================================================
def send_telegram_msg(text: str):
    url     = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
    try:
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code != 200:
            print(f"[TELEGRAM] HTTP {r.status_code}: {r.text[:120]}")
    except Exception as e:
        print(f"[TELEGRAM] Hata: {e}")

# ============================================================
# 4) PİYASA LİSTESİ
# ============================================================
def get_clean_symbols() -> list:
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

# ============================================================
# 5) COİN ADI
# ============================================================
def get_coin_name(symbol: str) -> str:
    """
    Binance market verisinden coinin tam adını çeker.
    Örnek: 'SOL/USDT' -> 'Solana'
    Bulunamazsa sadece base sembolü döner.
    """
    try:
        market = exchange.markets.get(symbol, {})
        full   = market.get("info", {}).get("baseAssetFullName", "")
        if not full:
            full = market.get("info", {}).get("baseAsset", symbol.split("/")[0])
        return full.strip()
    except Exception:
        return symbol.split("/")[0]

# ============================================================
# 6) TEKNİK İNDİKATÖRLER
# ============================================================
def calc_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    hl  = df["high"] - df["low"]
    hpc = (df["high"] - df["close"].shift(1)).abs()
    lpc = (df["low"]  - df["close"].shift(1)).abs()
    tr  = pd.concat([hl, hpc, lpc], axis=1).max(axis=1)
    return tr.rolling(window=period).mean()

def calc_rsi(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder EMA — LuxAlgo ile uyumlu."""
    delta = df["close"].diff()
    gain  = delta.where(delta > 0, 0.0).ewm(alpha=1/period, adjust=False).mean()
    loss  = (-delta.where(delta < 0, 0.0)).ewm(alpha=1/period, adjust=False).mean()
    rs    = gain / loss.replace(0, 1e-10)
    return 100 - (100 / (1 + rs))

# ============================================================
# 6) LuxAlgo PIVOT & YAPI TESPİTİ
# ============================================================
def find_pivot_highs(df: pd.DataFrame, size: int = 5) -> list:
    """Son onaylanmış pivot high noktalarını bulur."""
    highs  = []
    window = min(len(df) - 1, 100)
    for i in range(size, window):
        idx = -(i + 1)
        h   = df["high"].iloc[idx]
        if (all(h >= df["high"].iloc[idx - size : idx].values) and
                all(h >= df["high"].iloc[idx + 1 : idx + size + 1].values)):
            highs.append({"price": h, "ago": i})
    return highs

def find_pivot_lows(df: pd.DataFrame, size: int = 5) -> list:
    """Son onaylanmış pivot low noktalarını bulur."""
    lows   = []
    window = min(len(df) - 1, 100)
    for i in range(size, window):
        idx = -(i + 1)
        l   = df["low"].iloc[idx]
        if (all(l <= df["low"].iloc[idx - size : idx].values) and
                all(l <= df["low"].iloc[idx + 1 : idx + size + 1].values)):
            lows.append({"price": l, "ago": i})
    return lows

def infer_trend_bias(df: pd.DataFrame, size: int = 5) -> str:
    """
    LuxAlgo swingTrend.bias mantığı:
    Son iki pivot high + low karşılaştırması ile trend yönü.
    """
    ph = find_pivot_highs(df, size)
    pl = find_pivot_lows(df, size)
    if len(ph) < 2 or len(pl) < 2:
        return "NEUTRAL"
    hh = ph[0]["price"] > ph[1]["price"]
    hl = pl[0]["price"] > pl[1]["price"]
    if hh and hl:
        return "BULLISH"
    if not hh and not hl:
        return "BEARISH"
    return "NEUTRAL"

def detect_structure_break(df: pd.DataFrame, size: int = 5) -> dict:
    """
    LuxAlgo displayStructure() karşılığı.
    Bullish yapı kırılımını tespit eder ve CHoCH / BOS ayrımı yapar.
    """
    result = {
        "break_type"  : None,   # "CHoCH" | "BOS" | None
        "swing_high"  : None,
        "swing_ago"   : None,
        "trend_before": None,
        "break_pct"   : 0.0,
        "volume_surge": False,
    }

    pivot_highs   = find_pivot_highs(df, size)
    current_close = df["close"].iloc[-1]
    prev_close    = df["close"].iloc[-2]

    # En yakın geçerli pivot high'ı bul
    nearest = next((ph for ph in pivot_highs if ph["ago"] >= 1), None)
    if nearest is None:
        return result

    swing_high = nearest["price"]

    # Crossover: önceki mum altında, mevcut mum üzerinde
    if not (current_close > swing_high and prev_close <= swing_high):
        return result

    trend_before = infer_trend_bias(df, size)
    break_type   = "CHoCH" if trend_before == "BEARISH" else "BOS"

    avg_vol      = df["volume"].iloc[-20:].mean()
    volume_surge = bool(df["volume"].iloc[-1] > avg_vol * 1.2)

    result.update({
        "break_type"  : break_type,
        "swing_high"  : swing_high,
        "swing_ago"   : nearest["ago"],
        "trend_before": trend_before,
        "break_pct"   : round((current_close - swing_high) / swing_high * 100, 3),
        "volume_surge": volume_surge,
    })
    return result

# ============================================================
# 7) MESAJ ŞABLONları
# ============================================================
def build_phase1_msg(symbol, coin_name, current_price, ma200, dist_to_ma200,
                     current_rsi, raw_atr_str, atr_ratio, depth,
                     strategy_label, strategy_note, header_icon,
                     trend_bias) -> str:
    base = symbol.split("/")[0]
    return (
        f"🎯🎯🎯 <b>PUSU KURULDU</b> 🎯🎯🎯\n"
        f"<b>#{base}</b>  <i>{coin_name}</i>\n"
        f"<code>━━━━━━━━━━━━━━━━━━━━</code>\n"
        f"📍 <b>AŞAMA 1 — DERİN İNDİRİM</b>\n"
        f"📈 <b>STRATEJİ:</b> {strategy_label}\n"
        f"<code>━━━━━━━━━━━━━━━━━━━━</code>\n\n"
        f"💵 <b>FİYAT:</b> <code>{current_price}</code>\n"
        f"📊 <b>200 MA:</b> <code>{round(ma200, 6)}</code> "
        f"(<b>%{round(dist_to_ma200, 1)}</b>)\n"
        f"🌀 <b>RSI (14):</b> <b>{round(current_rsi, 2)}</b>\n"
        f"🌋 <b>ATR (TAM):</b> <code>{raw_atr_str}</code>\n"
        f"📊 <b>ATR ORANI:</b> %{round(atr_ratio, 2)}\n"
        f"📉 <b>İNDİRİM DERİNLİĞİ:</b> %{round(depth, 1)}\n"
        f"📐 <b>MEVCUT TREND:</b> {trend_bias}\n\n"
        f"<code>━━━━━━━━━━━━━━━━━━━━</code>\n"
        f"{strategy_note}\n\n"
        f"⏳ <b>CHoCH/BOS bekleniyor — tetik çekilmedi!</b>\n"
        f"👁 TradingView'da izlemeye al."
    )

def build_phase2_msg(symbol, coin_name, current_price, ma200, dist_to_ma200,
                     current_rsi, raw_atr_str, atr_ratio, depth,
                     strategy_label, strategy_note, header_icon,
                     structure) -> str:
    base         = symbol.split("/")[0]
    bt           = structure["break_type"]
    volume_line  = "Evet ✅" if structure["volume_surge"] else "Yok ⚠️"
    icon         = "✅" if bt == "CHoCH" else "🔄"
    strength     = "🔥 <b>GÜÇLÜ — CHoCH (Trend Döndü)</b>" if bt == "CHoCH" \
                   else "💪 <b>ORTA — BOS (Trend Devam)</b>"

    return (
        f"🚀🚀🚀 <b>TETİK ÇEKİLDİ</b> 🚀🚀🚀\n"
        f"<b>#{base}</b>  <i>{coin_name}</i>\n"
        f"<code>━━━━━━━━━━━━━━━━━━━━</code>\n"
        f"⚡ <b>AŞAMA 2 — YAPISAL KIRILIM</b>\n"
        f"🎯 <b>SİNYAL GÜCÜ:</b> {strength}\n"
        f"📈 <b>STRATEJİ:</b> {strategy_label}\n"
        f"<code>━━━━━━━━━━━━━━━━━━━━</code>\n\n"
        f"💵 <b>FİYAT:</b> <code>{current_price}</code>\n"
        f"📊 <b>200 MA:</b> <code>{round(ma200, 6)}</code> "
        f"(<b>%{round(dist_to_ma200, 1)}</b>)\n"
        f"🌀 <b>RSI (14):</b> <b>{round(current_rsi, 2)}</b>\n"
        f"🌋 <b>ATR (TAM):</b> <code>{raw_atr_str}</code>\n"
        f"📊 <b>ATR ORANI:</b> %{round(atr_ratio, 2)}\n"
        f"📉 <b>İNDİRİM DERİNLİĞİ:</b> %{round(depth, 1)}\n\n"
        f"<code>━━━━━━━━━━━━━━━━━━━━</code>\n"
        f"{icon} <b>{bt}:</b> Swing High kırıldı "
        f"(<code>{structure['swing_ago']}</code> mum önce pivot, "
        f"+%{structure['break_pct']})\n"
        f"📦 <b>Hacim Artışı:</b> {volume_line}\n"
        f"📐 <b>Kırılım Öncesi Trend:</b> {structure['trend_before']}\n\n"
        f"{strategy_note}\n\n"
        f"{'🟢 <b>GİRİŞ DEĞERLENDİR!</b> Risk yönetimini unutma.' if bt == 'CHoCH' else '⚠️ <b>DİKKATLİ OL:</b> BOS — trend hâlâ devam ediyordu.'}"
    )

# ============================================================
# 8) ANA ANALİZ MOTORU
# ============================================================
def analyze(symbol: str):
    try:
        now = time.time()

        # ── Veri çek ──────────────────────────────────────────────
        ticker = exchange.fetch_ticker(symbol)
        if float(ticker["quoteVolume"]) < MIN_VOLUME_24H:
            return

        bars = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=250)
        if len(bars) < 220:
            return

        coin_name = get_coin_name(symbol)

        df = pd.DataFrame(bars, columns=["timestamp","open","high","low","close","volume"])

        # ── İndikatörler ──────────────────────────────────────────
        df["atr"]  = calc_atr(df, 14)
        df["rsi"]  = calc_rsi(df, 14)
        df["ma200"]= df["close"].rolling(200).mean()

        price   = df["close"].iloc[-1]
        atr_val = df["atr"].iloc[-1]
        rsi     = df["rsi"].iloc[-1]
        ma200   = df["ma200"].iloc[-1]

        if any(pd.isna(x) for x in [atr_val, ma200]):
            return

        raw_atr   = f"{atr_val:.12f}".rstrip("0").rstrip(".")
        atr_ratio = (atr_val / price) * 100
        dist_ma   = ((price - ma200) / ma200) * 100

        # ── MA200 strateji etiketi ─────────────────────────────────
        if price > ma200:
            s_label = "⏳ <b>UZUN SÜRELİ (Trend Güçlü)</b>"
            s_note  = "👉 <i>Trend arkanda, kârı koşturmaya odaklan.</i>"
            h_icon  = "🟢🟢🟢"
        else:
            s_label = "⚡ <b>KISA SÜRELİ (Vur-Kaç)</b>"
            s_note  = "👉 <i>Trend zayıf, dirençlerde hızlı kâr al.</i>"
            h_icon  = "🔴🔴🔴"

        # ── Discount Zone ─────────────────────────────────────────
        r_high  = df["high"].iloc[-RANGE_LOOKBACK:].max()
        r_low   = df["low"].iloc[-RANGE_LOOKBACK:].min()
        equil   = (r_high + r_low) / 2.0
        span    = equil - r_low
        if span == 0:
            return

        depth = (equil - price) / span * 100   # >0 = discount

        # ── Yapı kırılımı ─────────────────────────────────────────
        structure    = detect_structure_break(df, SWING_SIZE)
        break_type   = structure["break_type"]   # CHoCH | BOS | None
        trend_bias   = infer_trend_bias(df, SWING_SIZE)

        # ============================================================
        # AŞAMA 2 — AKSİYON SİNYALİ
        # Önce Aşama 2'yi kontrol et: CHoCH/BOS geldi mi?
        # Eşik: depth > PHASE2_DEPTH ve RSI < PHASE2_RSI
        # ============================================================
        if break_type in ("CHoCH", "BOS"):
            if depth >= PHASE2_DEPTH and rsi < PHASE2_RSI:
                last_p2 = get_last_sent(symbol, "phase2")
                if now - last_p2 > PHASE2_COOLDOWN:
                    msg = build_phase2_msg(
                        symbol, coin_name, price, ma200, dist_ma,
                        rsi, raw_atr, atr_ratio, depth,
                        s_label, s_note, h_icon, structure
                    )
                    send_telegram_msg(msg)
                    mark_sent(symbol, "phase2")
                    print(f"🚀 [AŞAMA 2] {symbol} | {break_type} | Derinlik: %{round(depth,1)} | RSI: {round(rsi,1)}", flush=True)
                    return
                    
        # ============================================================
        # AŞAMA 1 — HAZIRLIK UYARISI
        # CHoCH yok ama fiyat pusu bölgesinde
        # Eşik: depth > PHASE1_DEPTH ve RSI < PHASE1_RSI
        # ============================================================
        if depth >= PHASE1_DEPTH and rsi < PHASE1_RSI:
            last_p1 = get_last_sent(symbol, "phase1")
            if now - last_p1 > PHASE1_COOLDOWN:
                msg = build_phase1_msg(
                    symbol, coin_name, price, ma200, dist_ma,
                    rsi, raw_atr, atr_ratio, depth,
                    s_label, s_note, h_icon, trend_bias
                )
                send_telegram_msg(msg)
                mark_sent(symbol, "phase1")
                print(f"🎯 [AŞAMA 1] {symbol} | Derinlik: %{round(depth,1)} | RSI: {round(rsi,1)}", flush=True)
                
    except Exception as e:
        print(f"[HATA] {symbol}: {e}", flush=True)
        
# ============================================================
# 9) ÇALIŞTIRICI DÖNGÜ
# ============================================================
def start_scanner():
    global sent_signals
    sent_signals = load_signals()

    print("=" * 50)
    print("🚀  SMC Sniper v3 — İki Aşamalı Radar")
    print("=" * 50)
    print(f"  Timeframe    : {TIMEFRAME}")
    print(f"  Swing size   : {SWING_SIZE} (LuxAlgo standartı)")
    print(f"  Aşama 1      : Derinlik >%{PHASE1_DEPTH} + RSI <{PHASE1_RSI}")
    print(f"  Aşama 2      : Derinlik >%{PHASE2_DEPTH} + RSI <{PHASE2_RSI} + CHoCH/BOS")
    print(f"  Cooldown 1   : {PHASE1_COOLDOWN // 3600}s  |  Cooldown 2: {PHASE2_COOLDOWN // 3600}s")
    print("=" * 50 + "\n")

    while True:
        symbols = get_clean_symbols()
        print(f"🔄 {len(symbols)} coin taranıyor...", flush=True)

        for symbol in symbols:
            analyze(symbol)
            time.sleep(0.35)

        print(f"✅ Tarama bitti. {SCAN_INTERVAL // 60} dakika bekleniyor.\n", flush=True)
        time.sleep(SCAN_INTERVAL)    
        
if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    start_scanner()
