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

# Loglar anında aksın
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
    return "🚀 Sniper Bot (FIRSATÇI MOD) Aktif!"

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

# --- 3. BÜYÜK RESİM & İSYAN MODU (Macro Trend + Bypass) ---
# Burası botun "Gireyim mi?" diye sorduğu kapı.

def check_macro_context(symbol, df_15m):
    try:
        df_4h = get_data(symbol, '4h', limit=100)
        if df_4h is None: return False, "Veri Yok"

        # 4 Saatlik Göstergeler
        ema200 = ta.ema(df_4h['close'], length=200).iloc[-1]
        close_4h = df_4h['close'].iloc[-1]
        rsi_4h = ta.rsi(df_4h['close'], length=14).iloc[-1]

        # 15 Dakikalık Göstergeler (İsyan İçin)
        last_vol = df_15m['volume'].iloc[-1]
        avg_vol = df_15m['volume'].rolling(20).mean().iloc[-1]
        price_change = (df_15m['close'].iloc[-1] - df_15m['open'].iloc[-1]) / df_15m['open'].iloc[-1]

        # SENARYO 1: BOĞA TRENDİ (Normal Kapı)
        if close_4h > ema200:
            return True, "YÜKSELİŞ TRENDİ (Güvenli)"

        # SENARYO 2: İSYAN MODU (Trend Düşüyor ama Fırsat Var!)
        # Fiyat EMA200 altı AMA Hacim 3 Katına çıkmış VE Mum %2+ Yükselmiş
        if close_4h < ema200:
            if last_vol > (avg_vol * 3.0) and price_change > 0.02:
                return True, "⚠️ DÜŞÜŞTE HACİM PATLAMASI (Fırsat Bypass)"
            
            # SENARYO 3: DİP TEPKİSİ
            # Trend Düşüyor ama RSI Aşırı Satımda (Ölü Kedi Sıçraması)
            if rsi_4h < 30:
                return True, "DÜŞÜŞ (Aşırı Satım Tepkisi)"

        # SENARYO 4: DÜŞEN BIÇAK (Reddet)
        return False, "DÜŞÜŞ TRENDİ (Hacimsiz - Uzak Dur)"

    except: return False, "Hata"

# --- 4. KONUM ANALİZİ (DESTEK/DİRENÇ - Order Book Mantığı) ---

def check_structure(symbol, current_price):
    try:
        df_4h = get_data(symbol, '4h', limit=50)
        if df_4h is None: return False, "Veri Yok"

        last = df_4h.iloc[-2]
        pivot = (last['high'] + last['low'] + last['close']) / 3
        s1 = (2 * pivot) - last['high']
        r1 = (2 * pivot) - last['low']
        
        # Dirençte miyiz? (Satış yeme ihtimali)
        dist_r1 = abs(current_price - r1) / current_price
        if current_price >= r1 or dist_r1 < 0.01:
            return False, "DİRENÇTE (Riskli)"

        # Destekte miyiz? (Alım ihtimali)
        dist_s1 = abs(current_price - s1) / current_price
        if current_price <= s1 * 1.02: 
            return True, f"DESTEKTE (S1: {s1:.4f})"
            
        return True, "ARA BÖLGE (Nötr)" 

    except: return True, "Hesap Hatası"

# --- 5. SİNYAL TETİKÇİLERİ (Avcılar) ---

def get_triggers(df_15m):
    signals = []
    try:
        last = df_15m.iloc[-1]
        
        # A. SFP (Dip Tuzağı)
        past_low = df_15m['low'].iloc[-97:-1].min() # 24h Dip
        if last['low'] < past_low and last['close'] > past_low:
            vol_ma = df_15m['volume'].rolling(20).mean().iloc[-1]
            if last['volume'] > (vol_ma * 1.5):
                signals.append(f"🦅 SFP (24h Dip Süpürüldü)")

        # B. MOMENTUM (Trend/Pump)
        # Hızlı momentum için RSI ve MFI kombinasyonu
        rsi = ta.rsi(df_15m['close'], length=14).iloc[-1]
        mfi = ta.mfi(df_15m['high'], df_15m['low'], df_15m['close'], df_15m['volume'], length=14).iloc[-1]
        adx = ta.adx(df_15m['high'], df_15m['low'], df_15m['close'])['ADX_14'].iloc[-1]
        
        # MACD
        macd = ta.macd(df_15m['close'])
        macd_cross = macd['MACD_12_26_9'].iloc[-1] > macd['MACDs_12_26_9'].iloc[-1]

        if mfi > 50 and adx > 25 and macd_cross:
             signals.append(f"🚀 MOMENTUM (Güçlü Akış)")

        # C. RSI UYUMSUZLUK
        prev_rsi = ta.rsi(df_15m['close'], length=14).iloc[-2]
        if df_15m['close'].iloc[-1] < df_15m['close'].iloc[-2] and rsi > prev_rsi and rsi < 40:
            signals.append("🐂 RSI UYUMSUZLUK")

    except: pass
    return signals

# --- 6. MÜKEMMEL SETUP TEYİDİ (Senin Kriterlerin) ---

def check_confirmation(df_15m):
    try:
        # MACD Yukarı Dönüyor mu?
        macd = ta.macd(df_15m['close'])
        macd_up = macd['MACD_12_26_9'].iloc[-1] > macd['MACD_12_26_9'].iloc[-2]
        
        # ADX Güçleniyor mu?
        adx = ta.adx(df_15m['high'], df_15m['low'], df_15m['close'])['ADX_14']
        adx_rising = adx.iloc[-1] > adx.iloc[-2]
        
        # MFI (Para)
        mfi = ta.mfi(df_15m['high'], df_15m['low'], df_15m['close'], df_15m['volume'], length=14).iloc[-1]
        
        # İKNA EDİCİ DURUM:
        if macd_up and (adx_rising or mfi > 45):
            return True, "✅ MACD+ADX+MFI Onaylı"
        return False, "Zayıf Teyit"
    except: return False, "Hata"

# --- 7. ANA ANALİZ DÖNGÜSÜ ---

def run_analysis():
    tr_time = datetime.now() + timedelta(hours=3)
    print(f"\n🔎 [TARAMA] Saat: {tr_time.strftime('%H:%M')} | Bypass Modu Aktif")
    
    symbols = get_tradable_symbols()
    
    # Hafıza Temizliği
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

            # 2. KONUM KONTROLÜ (Dirençte miyiz?)
            struct_safe, struct_msg = check_structure(symbol, current_price)
            if not struct_safe:
                # Dirençteyse Bypass bile kurtarmaz, kafasına vururlar.
                continue

            # 3. MACRO & İSYAN KONTROLÜ (Gireyim mi?)
            # Burası "Düşüşte hacim patlaması varsa GİR" diyen yer.
            macro_safe, macro_msg = check_macro_context(symbol, df_15m)
            if not macro_safe:
                continue # Hacim yoksa ve trend düşüşse GİRME.

            # 4. SİNYAL VE TEYİT
            active_signals = get_triggers(df_15m)
            conf_ok, conf_msg = check_confirmation(df_15m)

            # KARAR:
            # Sinyal varsa VE (Teyitliyse VEYA Hacim Patlaması varsa)
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
🌍 <b>DURUM:</b> {macro_msg}
📊 <b>KONUM:</b> {struct_msg}
✅ <b>TEYİT:</b> {conf_msg}

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
                    print(f"✅ SİNYAL: {symbol} | {macro_msg}")
                except: pass

        except Exception as e:
            continue

    print("🏁 Tarama Bitti.")
    gc.collect()

if __name__ == "__main__":
    scheduler = BackgroundScheduler()
    scheduler.add_job(func=run_analysis, trigger="interval", minutes=5)
    scheduler.start()
    print("🚀 BOT BAŞLATILDI (TRADER ZEKASI + İSYAN MODU).")

    def ilk_tarama():
        time.sleep(10)
        run_analysis()

    threading.Thread(target=ilk_tarama).start()

    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port, use_reloader=False)
