import ccxt
import pandas as pd
import requests
import time
import json
import os
import threading
from flask import Flask

app = Flask(__name__)

@app.route('/')
def health_check():
    return "SMC Sniper v4 is Running!", 200

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
RANGE_LOOKBACK   = 30
MIN_VOLUME_24H   = 5_000_000
SCAN_INTERVAL    = 900

PHASE1_DEPTH     = 85
PHASE1_RSI       = 35
PHASE1_COOLDOWN  = 86400

PHASE2_DEPTH     = 65
PHASE2_RSI       = 48
PHASE2_COOLDOWN  = 86400

SWING_SIZE       = 5
SIGNALS_FILE     = "sent_signals.json"

IGNORED_COINS = set([
    # Leveraged tokens
    'UP/USDT','DOWN/USDT','BEAR/USDT','BULL/USDT',
    # Stablecoins
    'USDC/USDT','TUSD/USDT','FDUSD/USDT','DAI/USDT','USDP/USDT',
    'USDE/USDT','UST/USDT','USD/USDT','XUSD/USDT','USD1/USDT','BFUSD/USDT',
    'USTC/USDT','BUSD/USDT','FRAX/USDT','LUSD/USDT','GUSD/USDT','SUSD/USDT',
    'USDS/USDT','USDX/USDT','USDD/USDT','CUSD/USDT','OUSD/USDT','MUSD/USDT',
    'U/USDT',
    # Fiat
    'EUR/USDT','TRY/USDT','GBP/USDT','BRL/USDT','RUB/USDT',
    'AUD/USDT','BIDR/USDT','IDRT/USDT','VAI/USDT',
    # Wrapped tokens
    'PAXG/USDT','WBTC/USDT','WETH/USDT','WBNB/USDT','BETH/USDT',
    'BTCB/USDT','HBTC/USDT',
])
exchange = ccxt.binance()

# ============================================================
# 2) SİNYAL HAFIZASI
# ============================================================
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
# 3b) PORTFÖY TAKİP
# ============================================================
def send_to_portfolio(symbol, price, atr_val, phase, break_type=""):
    """SMC sinyalini portföy takip sistemine POST eder."""
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
    delta = df["close"].diff()
    gain  = delta.where(delta > 0, 0.0).ewm(alpha=1/period, adjust=False).mean()
    loss  = (-delta.where(delta < 0, 0.0)).ewm(alpha=1/period, adjust=False).mean()
    rs    = gain / loss.replace(0, 1e-10)
    return 100 - (100 / (1 + rs))

# ============================================================
# 7) MUM FORMASYONU TESPİTİ  [YENİ]
# ============================================================
def detect_candle_patterns(df: pd.DataFrame) -> dict:
    """
    Son kapanan mumda (iloc[-2]) dönüş formasyonu tespit eder.
    Zorunlu filtre değil — CHoCH/BOS teyidini güçlendirici.

    Döner: {
        "hammer":       bool,
        "engulfing":    bool,
        "doji":         bool,
        "morning_star": bool,
    }
    """
    result = {
        "hammer":       False,
        "engulfing":    False,
        "doji":         False,
        "morning_star": False,
    }

    if len(df) < 4:
        return result

    c0 = df.iloc[-2]   # son kapanan mum (sinyal mumu)
    c1 = df.iloc[-3]   # bir önceki
    c2 = df.iloc[-4]   # iki önceki

    o0, h0, l0, cl0 = float(c0["open"]), float(c0["high"]), float(c0["low"]), float(c0["close"])
    o1, h1, l1, cl1 = float(c1["open"]), float(c1["high"]), float(c1["low"]), float(c1["close"])
    o2, h2, l2, cl2 = float(c2["open"]), float(c2["high"]), float(c2["low"]), float(c2["close"])

    body0  = abs(cl0 - o0)
    body1  = abs(cl1 - o1)
    body2  = abs(cl2 - o2)
    range0 = h0 - l0

    if range0 <= 0:
        return result

    upper_wick0 = h0 - max(cl0, o0)
    lower_wick0 = min(cl0, o0) - l0

    # ── HAMMER ──────────────────────────────────────────────
    # Koşullar:
    #   Alt gölge >= gövdenin 2 katı
    #   Üst gölge <= gövdenin %50'si (küçük)
    #   Gövde mumun üst %40'ında (lower_wick / range >= 0.55)
    if (body0 > 0
            and lower_wick0 >= body0 * 2.0
            and upper_wick0 <= body0 * 0.5
            and (min(cl0, o0) - l0) / range0 >= 0.55):
        result["hammer"] = True

    # ── BULLISH ENGULFING ────────────────────────────────────
    # c1 kırmızı, c0 yeşil ve c1'i tamamen yutuyor
    # Gövde büyüklük kontrolü: c0 gövdesi c1 gövdesinin en az %80'ini kapsamalı
    if (cl1 < o1              # c1 kırmızı mum
            and cl0 > o0      # c0 yeşil mum
            and o0 <= cl1     # c0 açılış, c1 kapanışın altında veya eşit
            and cl0 >= o1     # c0 kapanış, c1 açılışın üstünde veya eşit
            and body0 >= body1 * 0.8):
        result["engulfing"] = True

    # ── DOJI ────────────────────────────────────────────────
    # Gövde range'in %8'inden küçük = kararsızlık mumu
    if body0 <= range0 * 0.08:
        result["doji"] = True

    # ── MORNING STAR ────────────────────────────────────────
    # 3 mumlu formasyon:
    #   c2: büyük kırmızı mum
    #   c1: küçük gövdeli mum (doji/ıslık) — gap oluşturabilir
    #   c0: büyük yeşil mum, c2'nin ortasını geçmeli
    avg_body = (body0 + body1 + body2) / 3 if (body0 + body1 + body2) > 0 else 1
    c2_mid   = o2 - body2 / 2   # c2 gövdesinin ortası (kırmızı mum olduğu için)
    if (cl2 < o2                         # c2 kırmızı
            and body2 >= avg_body * 1.0  # c2 ortalama büyüklükte
            and body1 <= avg_body * 0.5  # c1 küçük gövde
            and cl0 > o0                 # c0 yeşil
            and body0 >= avg_body * 1.0  # c0 ortalama büyüklükte
            and cl0 >= c2_mid):          # c0 kapanış c2 gövde ortasını geçiyor
        result["morning_star"] = True

    return result


def candle_pattern_summary(patterns: dict) -> str:
    """
    Tespit edilen formasyonları mesaj satırına çevirir.
    Öncelik: Morning Star > Engulfing > Hammer > Doji
    Birden fazlaysa hepsi listelenir, güç seviyesi belirlenir.
    Hiçbiri yoksa boş string döner.
    """
    found = []
    if patterns.get("morning_star"): found.append("🌅 Morning Star")
    if patterns.get("engulfing"):    found.append("🟢 Bullish Engulfing")
    if patterns.get("hammer"):       found.append("🔨 Hammer")
    if patterns.get("doji"):         found.append("⚖️ Doji")

    if not found:
        return ""

    # Güç etiketi: Morning Star veya 2+ formasyon = çok güçlü
    if patterns.get("morning_star") or len(found) >= 2:
        power = "⚡⚡ ÇOK GÜÇLÜ TEYİT"
    elif patterns.get("engulfing"):
        power = "⚡ GÜÇLÜ TEYİT"
    else:
        power = "✅ TEYİT"

    return f"{power}: {' + '.join(found)}"


# ============================================================
# 8) LuxAlgo PIVOT & YAPI TESPİTİ
# ============================================================
def find_pivot_highs(df: pd.DataFrame, size: int = 5) -> list:
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
    ph = find_pivot_highs(df, size)
    pl = find_pivot_lows(df, size)
    if len(ph) < 2 or len(pl) < 2:
        return "NEUTRAL"
    hh = ph[0]["price"] > ph[1]["price"]
    hl = pl[0]["price"] > pl[1]["price"]
    if hh and hl:       return "BULLISH"
    if not hh and not hl: return "BEARISH"
    return "NEUTRAL"

def detect_structure_break(df: pd.DataFrame, size: int = 5) -> dict:
    result = {
        "break_type"  : None,
        "swing_high"  : None,
        "swing_ago"   : None,
        "trend_before": None,
        "break_pct"   : 0.0,
        "volume_surge": False,
    }

    pivot_highs   = find_pivot_highs(df, size)
    current_close = df["close"].iloc[-1]
    prev_close    = df["close"].iloc[-2]

    nearest = next((ph for ph in pivot_highs if ph["ago"] >= 1), None)
    if nearest is None:
        return result

    swing_high = nearest["price"]

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
# 9) MESAJ ŞABLONLARI
# ============================================================
def build_phase1_msg(symbol, coin_name, current_price, ma200, dist_to_ma200,
                     current_rsi, raw_atr_str, atr_ratio, depth,
                     strategy_label, strategy_note, header_icon,
                     trend_bias, patterns: dict) -> str:
    base  = symbol.split("/")[0]
    p_str = f"{current_price:.10f}".rstrip("0").rstrip(".")
    m_str = f"{ma200:.10f}".rstrip("0").rstrip(".")

    pattern_line = candle_pattern_summary(patterns)
    # Aşama 1'de formasyon varsa beklemeyi güçlendiriyor — "yakın olabilir" mesajı
    if pattern_line:
        pattern_block = (
            f"\n<code>━━━━━━━━━━━━━━━━━━━━</code>\n"
            f"📊 <b>MUM FORMASYONU</b>\n"
            f"{pattern_line}\n"
            f"<i>Alıcı baskısı görülüyor — CHoCH/BOS yakın olabilir.</i>"
        )
    else:
        pattern_block = ""

    return (
        f"🎯🎯🎯 <b>PUSU KURULDU</b> 🎯🎯🎯\n"
        f"<b>#{base}</b>  <i>{coin_name}</i>\n"
        f"<code>━━━━━━━━━━━━━━━━━━━━</code>\n"
        f"📍 <b>AŞAMA 1 — DERİN İNDİRİM</b>\n"
        f"📈 <b>STRATEJİ:</b> {strategy_label}\n"
        f"<code>━━━━━━━━━━━━━━━━━━━━</code>\n\n"
        f"💵 <b>FİYAT:</b> <code>{p_str}</code>\n"
        f"📊 <b>200 MA:</b> <code>{m_str}</code> (<b>%{round(dist_to_ma200, 1)}</b>)\n"
        f"🌀 <b>RSI (14):</b> <b>{round(current_rsi, 2)}</b>\n"
        f"🌋 <b>ATR (TAM):</b> <code>{raw_atr_str}</code>\n"
        f"📊 <b>ATR ORANI:</b> %{round(atr_ratio, 2)}\n"
        f"📉 <b>İNDİRİM DERİNLİĞİ:</b> %{round(depth, 1)}\n"
        f"📐 <b>MEVCUT TREND:</b> {trend_bias}"
        f"{pattern_block}\n"
        f"<code>━━━━━━━━━━━━━━━━━━━━</code>\n"
        f"{strategy_note}\n\n"
        f"⏳ <b>CHoCH/BOS bekleniyor — tetik çekilmedi!</b>\n"
        f"👁 TradingView'da izlemeye al."
    )

def build_phase2_msg(symbol, coin_name, current_price, ma200, dist_to_ma200,
                     current_rsi, raw_atr_str, atr_ratio, depth,
                     strategy_label, strategy_note, header_icon,
                     structure, patterns: dict) -> str:
    base         = symbol.split("/")[0]
    bt           = structure["break_type"]
    volume_line  = "Evet ✅" if structure["volume_surge"] else "Yok ⚠️"
    icon         = "✅" if bt == "CHoCH" else "🔄"
    strength     = "🔥 <b>GÜÇLÜ — CHoCH (Trend Döndü)</b>" if bt == "CHoCH" \
                   else "💪 <b>ORTA — BOS (Trend Devam)</b>"

    p_str = f"{current_price:.10f}".rstrip("0").rstrip(".")
    m_str = f"{ma200:.10f}".rstrip("0").rstrip(".")

    pattern_line = candle_pattern_summary(patterns)
    # Aşama 2'de formasyon varsa çok daha önemli — "giriş güçlü" mesajı
    if pattern_line:
        pattern_block = (
            f"\n<code>━━━━━━━━━━━━━━━━━━━━</code>\n"
            f"📊 <b>MUM FORMASYONU</b>\n"
            f"<b>{pattern_line}</b>\n"
            f"<i>Yapısal kırılım mum formasyonuyla teyit edildi.</i>"
        )
    else:
        pattern_block = ""

    entry_msg = (
        "🟢 <b>GİRİŞ DEĞERLENDİR!</b> Risk yönetimini unutma."
        if bt == "CHoCH"
        else "⚠️ <b>DİKKATLİ OL:</b> BOS — trend hâlâ devam ediyordu."
    )

    # Formasyon varsa giriş mesajını güçlendir
    if pattern_line and bt == "CHoCH":
        entry_msg = "🟢🟢 <b>GÜÇLÜ GİRİŞ SİNYALİ!</b> Formasyon + CHoCH kombinasyonu. Risk yönetimini unutma."

    return (
        f"🚀🚀🚀 <b>TETİK ÇEKİLDİ</b> 🚀🚀🚀\n"
        f"<b>#{base}</b>  <i>{coin_name}</i>\n"
        f"<code>━━━━━━━━━━━━━━━━━━━━</code>\n"
        f"⚡ <b>AŞAMA 2 — YAPISAL KIRILIM</b>\n"
        f"🎯 <b>SİNYAL GÜCÜ:</b> {strength}\n"
        f"📈 <b>STRATEJİ:</b> {strategy_label}\n"
        f"<code>━━━━━━━━━━━━━━━━━━━━</code>\n\n"
        f"💵 <b>FİYAT:</b> <code>{p_str}</code>\n"
        f"📊 <b>200 MA:</b> <code>{m_str}</code> (<b>%{round(dist_to_ma200, 1)}</b>)\n"
        f"🌀 <b>RSI (14):</b> <b>{round(current_rsi, 2)}</b>\n"
        f"🌋 <b>ATR (TAM):</b> <code>{raw_atr_str}</code>\n"
        f"📊 <b>ATR ORANI:</b> %{round(atr_ratio, 2)}\n"
        f"📉 <b>İNDİRİM DERİNLİĞİ:</b> %{round(depth, 1)}\n\n"
        f"<code>━━━━━━━━━━━━━━━━━━━━</code>\n"
        f"{icon} <b>{bt}:</b> Swing High kırıldı "
        f"(<code>{structure['swing_ago']}</code> mum önce pivot, +%{structure['break_pct']})\n"
        f"📦 <b>Hacim Artışı:</b> {volume_line}\n"
        f"📐 <b>Kırılım Öncesi Trend:</b> {structure['trend_before']}"
        f"{pattern_block}\n\n"
        f"{strategy_note}\n\n"
        f"{entry_msg}"
    )

# ============================================================
# 10) ANA ANALİZ MOTORU
# ============================================================
def analyze(symbol: str):
    try:
        now = time.time()

        ticker = exchange.fetch_ticker(symbol)
        if float(ticker["quoteVolume"]) < MIN_VOLUME_24H:
            return

        bars = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=250)
        if len(bars) < 220:
            return

        coin_name = get_coin_name(symbol)
        df = pd.DataFrame(bars, columns=["timestamp","open","high","low","close","volume"])

        df["atr"]   = calc_atr(df, 14)
        df["rsi"]   = calc_rsi(df, 14)
        df["ma200"] = df["close"].rolling(200).mean()

        price   = df["close"].iloc[-1]
        atr_val = df["atr"].iloc[-1]
        rsi     = df["rsi"].iloc[-1]
        ma200   = df["ma200"].iloc[-1]

        if any(pd.isna(x) for x in [atr_val, ma200]):
            return

        raw_atr   = f"{atr_val:.12f}".rstrip("0").rstrip(".")
        atr_ratio = (atr_val / price) * 100
        dist_ma   = ((price - ma200) / ma200) * 100

        if price > ma200:
            s_label = "⏳ <b>UZUN SÜRELİ (Trend Güçlü)</b>"
            s_note  = "👉 <i>Trend arkanda, kârı koşturmaya odaklan.</i>"
            h_icon  = "🟢🟢🟢"
        else:
            s_label = "⚡ <b>KISA SÜRELİ (Vur-Kaç)</b>"
            s_note  = "👉 <i>Trend zayıf, dirençlerde hızlı kâr al.</i>"
            h_icon  = "🔴🔴🔴"

        # Derinlik = fiyatın MA200'ün ne kadar altında olduğu (%)
        # MA200 üstündeyse 0, altındaysa pozitif değer
        if ma200 <= 0:
            return
        
        depth = ((ma200 - price) / ma200) * 100
        if depth < 0:
            depth = 0.0
    
        structure  = detect_structure_break(df, SWING_SIZE)
        break_type = structure["break_type"]
        trend_bias = infer_trend_bias(df, SWING_SIZE)

        # Mum formasyonu tespiti — her iki aşamada da kullanılır
        patterns = detect_candle_patterns(df)

        # ── AŞAMA 2 ──────────────────────────────────────────
        if break_type in ("CHoCH", "BOS"):
            if depth >= PHASE2_DEPTH and rsi < PHASE2_RSI:
                last_p2 = get_last_sent(symbol, "phase2")
                if now - last_p2 > PHASE2_COOLDOWN:
                    msg = build_phase2_msg(
                        symbol, coin_name, price, ma200, dist_ma,
                        rsi, raw_atr, atr_ratio, depth,
                        s_label, s_note, h_icon, structure, patterns
                    )
                    send_telegram_msg(msg)
                    mark_sent(symbol, "phase2")
                    send_to_portfolio(symbol, price, atr_val, "phase2", bt)
                    pat_log = candle_pattern_summary(patterns)
                    print(f"🚀 [AŞAMA 2] {symbol} | {break_type} | Derinlik: %{round(depth,1)} | RSI: {round(rsi,1)}"
                          + (f" | {pat_log}" if pat_log else ""))
                    return

        # ── AŞAMA 1 ──────────────────────────────────────────
        if depth >= PHASE1_DEPTH and rsi < PHASE1_RSI:
            last_p1 = get_last_sent(symbol, "phase1")
            if now - last_p1 > PHASE1_COOLDOWN:
                msg = build_phase1_msg(
                    symbol, coin_name, price, ma200, dist_ma,
                    rsi, raw_atr, atr_ratio, depth,
                    s_label, s_note, h_icon, trend_bias, patterns
                )
                send_telegram_msg(msg)
                mark_sent(symbol, "phase1")
                send_to_portfolio(symbol, price, atr_val, "phase1")
                pat_log = candle_pattern_summary(patterns)
                print(f"🎯 [AŞAMA 1] {symbol} | Derinlik: %{round(depth,1)} | RSI: {round(rsi,1)}"
                      + (f" | {pat_log}" if pat_log else ""))

    except Exception as e:
        print(f"[HATA] {symbol}: {e}")

# ============================================================
# 11) ÇALIŞTIRICI DÖNGÜ
# ============================================================
def start_scanner():
    global sent_signals
    sent_signals = load_signals()

    threading.Thread(target=run_flask, daemon=True).start()

    print("=" * 50)
    print("🚀  SMC Sniper v4 — Mum Formasyonu Teyitli")
    print("=" * 50)
    print(f"  Timeframe    : {TIMEFRAME}")
    print(f"  Swing size   : {SWING_SIZE}")
    print(f"  Aşama 1      : Derinlik >%{PHASE1_DEPTH} + RSI <{PHASE1_RSI}")
    print(f"  Aşama 2      : Derinlik >%{PHASE2_DEPTH} + RSI <{PHASE2_RSI} + CHoCH/BOS")
    print(f"  Formasyonlar : Hammer / Bullish Engulfing / Doji / Morning Star")
    print("=" * 50 + "\n")

    while True:
        symbols = get_clean_symbols()
        print(f"🔄 {len(symbols)} coin taranıyor...")
        for symbol in symbols:
            analyze(symbol)
            time.sleep(0.35)
        print(f"✅ Tarama bitti. {SCAN_INTERVAL // 60} dakika bekleniyor.\n")
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    start_scanner()
