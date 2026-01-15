import ccxt
import pandas as pd
import pandas_ta as ta
import time
import os
import requests
import threading
from flask import Flask
from datetime import datetime, timedelta
import sys
import gc 
from apscheduler.schedulers.background import BackgroundScheduler

sys.stdout.reconfigure(line_buffering=True)

# --- 1. AYARLAR ---
API_KEY = os.getenv('BINANCE_API_KEY')
API_SECRET = os.getenv('BINANCE_SECRET_KEY')
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

COOLDOWN_MINUTES = 120 

IGNORED_COINS = [
    'UP/USDT', 'DOWN/USDT', 'BEAR/USDT', 'BULL/USDT',
    'USDC/USDT', 'TUSD/USDT', 'FDUSD/USDT', 'DAI/USDT', 'USDP/USDT',
    'EUR/USDT', 'TRY/USDT', 'GBP/USDT', 'BUSD/USDT', 'USTC/USDT',
    'PAXG/USDT', 'WBTC/USDT', 'USDE/USDT', 'BRL/USDT', 'RUB/USDT',
    'AUD/USDT', 'UST/USDT', 'USD/USDT', 'XUSD/USDT', 'USD1/USDT',
]

exchange = ccxt.binance({
    'apiKey': API_KEY,
    'secret': API_SECRET,
    'options': {'defaultType': 'spot'},
    'enableRateLimit': True,
    'timeout': 30000
})

app = Flask(__name__)
signal_history = {} 

@app.route('/')
def home():
    return "🚀 Sniper Bot (REJİM FİLTRELİ AKILLI MOD) Aktif!"

# --- 2. VERİ ÇEKME ---
def get_tradable_symbols():
    try:
        exchange.load_markets()
        symbols = [s for s in exchange.markets if s.endswith('/USDT') 
                   and exchange.markets[s]['active'] 
                   and not any(i in s for i in IGNORED_COINS)]
        return symbols
    except: return []

def get_data(symbol, timeframe, limit=100):
    try:
        time.sleep(0.1) 
        bars = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        df = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df.set_index('timestamp', inplace=True)
        return df
    except: return None

# --- 3. PİYASA REJİMİ ANALİZİ (YENİ BEYİN) ---
# Coin şu an hangi modda? (Trend mi? Yatay mı? Sıkışma mı?)

def identify_market_regime(df_15m):
    try:
        # ADX (Trend Gücü)
        adx = ta.adx(df_15m['high'], df_15m['low'], df_15m['close'])['ADX_14'].iloc[-1]
        
        # Bollinger Bant Genişliği (Volatilite)
        bb = ta.bbands(df_15m['close'], length=20, std=2)
        bb_width = (bb['BBU_20_2.0'].iloc[-1] - bb['BBL_20_2.0'].iloc[-1]) / bb['BBM_20_2.0'].iloc[-1]
        
        # Sıkışma (Squeeze) Kontrolü: Bantlar çok daralmışsa patlama yakındır.
        # Bu değer genelde 0.05 - 0.10 altıysa sıkışma vardır (Coine göre değişir ama genel kabul).
        if bb_width < 0.08:
            return "SQUEEZE" # Sıkışma Modu (Patlama Bekle)

        # Trend Kontrolü
        if adx > 25:
            return "TRENDING" # Trend Modu (Momentum Kullan)
        
        # Yatay Kontrol
        if adx < 20:
            return "RANGING" # Yatay Mod (SFP/Dip Kullan)
            
        return "NEUTRAL" # Kararsız Bölge
    except: return "NEUTRAL"

# --- 4. BÜYÜK RESİM (4H TREND FİLTRESİ) ---
def check_macro_context(symbol, df_15m):
    try:
        df_4h = get_data(symbol, '4h', limit=50)
        if df_4h is None: return False, "Veri Yok"

        ema200 = ta.ema(df_4h['close'], length=200).iloc[-1]
        close_4h = df_4h['close'].iloc[-1]
        rsi_4h = ta.rsi(df_4h['close'], length=14).iloc[-1]

        # İSYAN MODU KONTROLLERİ
        last_vol = df_15m['volume'].iloc[-1]
        avg_vol = df_15m['volume'].rolling(20).mean().iloc[-1]
        price_change = (df_15m['close'].iloc[-1] - df_15m['open'].iloc[-1]) / df_15m['open'].iloc[-1]

        if close_4h > ema200:
            return True, "YÜKSELİŞ TRENDİ"
        
        # Düşüşte Hacim Patlaması (İstisna)
        if close_4h < ema200:
            if last_vol > (avg_vol * 3.0) and price_change > 0.02:
                return True, "⚠️ DÜŞÜŞTE HACİM PATLAMASI (Bypass)"
            if rsi_4h < 30:
                return True, "DÜŞÜŞ (Aşırı Satım Tepkisi)"

        return False, "DÜŞÜŞ TRENDİ"
    except: return False, "Hata"

# --- 5. KONUM ANALİZİ (DESTEK/DİRENÇ) ---
def check_structure(symbol, current_price):
    try:
        df_4h = get_data(symbol, '4h', limit=50)
        if df_4h is None: return False, "Veri Yok"
        last = df_4h.iloc[-2]
        pivot = (last['high'] + last['low'] + last['close']) / 3
        s1 = (2 * pivot) - last['high']
        r1 = (2 * pivot) - last['low']
        
        dist_r1 = abs(current_price - r1) / current_price
        if current_price >= r1 or dist_r1 < 0.01:
            return False, "DİRENÇTE (Riskli)"
        
        dist_s1 = abs(current_price - s1) / current_price
        if current_price <= s1 * 1.02: 
            return True, f"DESTEKTE (S1: {s1:.4f})"
        return True, "ARA BÖLGE" 
    except: return True, "Hata"

# --- 6. AKILLI STRATEJİ SEÇİCİ (REJİME GÖRE) ---

def get_signals_by_regime(df_15m, regime):
    signals = []
    last = df_15m.iloc[-1]
    
    # Ortak İndikatörler
    rsi = ta.rsi(df_15m['close'], length=14).iloc[-1]
    mfi = ta.mfi(df_15m['high'], df_15m['low'], df_15m['close'], df_15m['volume'], length=14).iloc[-1]
    
    # --- REJİM 1: YATAY PİYASA (RANGING) ---
    # Sadece SFP ve Uyumsuzluk çalışır. Trend sinyallerini YOK SAY.
    if regime == "RANGING" or regime == "NEUTRAL":
        # SFP Kontrolü
        past_low = df_15m['low'].iloc[-97:-1].min()
        if last['low'] < past_low and last['close'] > past_low:
             vol_ma = df_15m['volume'].rolling(20).mean().iloc[-1]
             if last['volume'] > (vol_ma * 1.5):
                 signals.append(f"🦅 SFP (Dip Avı - Yatay Piyasa)")
        
        # Uyumsuzluk Kontrolü
        prev_rsi = ta.rsi(df_15m['close'], length=14).iloc[-2]
        if df_15m['close'].iloc[-1] < df_15m['close'].iloc[-2] and rsi > prev_rsi and rsi < 40:
            signals.append("🐂 RSI UYUMSUZLUK")

    # --- REJİM 2: TREND PİYASASI (TRENDING) ---
    # Sadece Momentum çalışır. Dip dönüşü arama (trend ezer geçer).
    if regime == "TRENDING":
        adx = ta.adx(df_15m['high'], df_15m['low'], df_15m['close'])['ADX_14'].iloc[-1]
        macd = ta.macd(df_15m['close'])
        macd_cross = macd['MACD_12_26_9'].iloc[-1] > macd['MACDs_12_26_9'].iloc[-1]
        
        # RSI Şişkinlik Kontrolü (Benim Eklediğim Güvenlik)
        if mfi > 50 and macd_cross and rsi < 70:
             signals.append(f"🚀 MOMENTUM (Trend Takibi)")

    # --- REJİM 3: SIKIŞMA (SQUEEZE) ---
    # Bantlar daraldı, patlama bekleniyor. Hacim artışı kovala.
    if regime == "SQUEEZE":
        vol_ma = df_15m['volume'].rolling(20).mean().iloc[-1]
        if last['volume'] > (vol_ma * 2.5) and last['close'] > last['open']:
             signals.append(f"💥 SQUEEZE BREAKOUT (Patlama)")

    return signals

# --- 7. SETUP TEYİDİ ---
def check_confirmation(df_15m):
    try:
        macd = ta.macd(df_15m['close'])
        macd_up = macd['MACD_12_26_9'].iloc[-1] > macd['MACD_12_26_9'].iloc[-2]
        adx = ta.adx(df_15m['high'], df_15m['low'], df_15m['close'])['ADX_14']
        adx_rising = adx.iloc[-1] > adx.iloc[-2]
        mfi = ta.mfi(df_15m['high'], df_15m['low'], df_15m['close'], df_15m['volume'], length=14).iloc[-1]
        
        rsi = ta.rsi(df_15m['close'], length=14).iloc[-1]
        if rsi > 75: return False, "RSI Aşırı Şişik (>75)" # Güvenlik

        if macd_up and (adx_rising or mfi > 45):
            return True, "✅ MACD+ADX+MFI Onaylı"
        return False, "Zayıf"
    except: return False, "Hata"

# --- 8. ANA ANALİZ ---
def run_analysis():
    tr_time = datetime.now() + timedelta(hours=3)
    print(f"\n🔎 [TARAMA] Saat: {tr_time.strftime('%H:%M')} | Akıllı Rejim Modu")
    
    symbols = get_tradable_symbols()
    current_time = datetime.now()
    to_remove = [sym for sym, t in signal_history.items() if (current_time - t) > timedelta(minutes=COOLDOWN_MINUTES)]
    for sym in to_remove: del signal_history[sym]

    for symbol in symbols:
        try:
            if symbol in signal_history: continue

            # 1. VERİ
            df_15m = get_data(symbol, '15m', limit=150)
            if df_15m is None: continue
            current_price = df_15m['close'].iloc[-1]

            # 2. PİYASA REJİMİ BELİRLE (YENİ ADIM)
            regime = identify_market_regime(df_15m)

            # 3. KONUM KONTROLÜ
            struct_safe, struct_msg = check_structure(symbol, current_price)
            if not struct_safe: continue

            # 4. MACRO KONTROL
            macro_safe, macro_msg = check_macro_context(symbol, df_15m)
            if not macro_safe: continue

            # 5. REJİME UYGUN SİNYAL ARA
            # Bot artık her stratejiyi değil, sadece rejime uyanı dener.
            active_signals = get_signals_by_regime(df_15m, regime)
            conf_ok, conf_msg = check_confirmation(df_15m)

            final_signal = False
            signal_title = ""

            if len(active_signals) > 0 and conf_ok:
                final_signal = True
                signal_title = active_signals[0]
            elif "HACİM PATLAMASI" in macro_msg and conf_ok:
                final_signal = True
                signal_title = "💥 HACİM PATLAMASI (Fırsat)"

            if final_signal:
                signal_history[symbol] = datetime.now()
                
                atr = ta.atr(df_15m['high'], df_15m['low'], df_15m['close'], length=14).iloc[-1]
                stop_price = current_price - (2 * atr)
                risk_pct = ((current_price - stop_price) / current_price) * 100
                
                tp1 = current_price + (risk_pct * 1.5 * current_price / 100)
                tp2 = current_price + (risk_pct * 4.0 * current_price / 100)

                msg = f"""
<b>{signal_title}</b>
━━━━━━━━━━━━━━━━━━━━
<b>#{symbol}</b>
━━━━━━━━━━━━━━━━━━━━
🧠 <b>REJİM:</b> {regime} (Buna göre tarandı)
🌍 <b>DURUM:</b> {macro_msg}
📊 <b>KONUM:</b> {struct_msg}

💵 <b>GİRİŞ :</b> <code>{current_price:.4f}</code>
🛡️ <b>STOP  :</b> <code>{stop_price:.4f}</code> (Risk: %{risk_pct:.2f})

🎯 <b>HEDEFLER</b>
━━━━━━━━━━━━━━━━━━━━
1️⃣ Hedef: <code>{tp1:.4f}</code>
2️⃣ Hedef: <code>{tp2:.4f}</code>
"""
                try:
                    requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", 
                                json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"})
                    print(f"✅ SİNYAL: {symbol} | Rejim: {regime} | {signal_title}")
                except: pass

        except Exception as e:
            continue

    print("🏁 Tarama Bitti.")
    gc.collect()

if __name__ == "__main__":
    scheduler = BackgroundScheduler()
    scheduler.add_job(func=run_analysis, trigger="interval", minutes=5)
    scheduler.start()
    print("🚀 BOT BAŞLATILDI (REJİM ANALİZİ + AKILLI FİLTRE).")

    def ilk_tarama():
        time.sleep(10)
        run_analysis()

    threading.Thread(target=ilk_tarama).start()

    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port, use_reloader=False)
