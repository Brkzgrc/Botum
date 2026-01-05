import pandas as pd
import time
import requests
import urllib3
from flask import Flask
import threading
from datetime import datetime, timedelta

# --- RENDER VE WEB SUNUCUSU ---
app = Flask('')
@app.route('/')
def home(): return "Pro Bot v3.0 Aktif"
def run_web(): app.run(host='0.0.0.0', port=8080)
threading.Thread(target=run_web, daemon=True).start()

# --- AYARLAR VE API ---
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
TELEGRAM_TOKEN = "7583261338:AAHASreSYIaX-6QAXIUflpyf5HnbQXq81Dg"
CHAT_ID = "5124859166"
EXCLUDED = ['USDC', 'FDUSD', 'TUSD', 'USDP', 'BUSD', 'DAI', 'EUR', 'TRY', 'GBP', 'PAXG']
BASE_URLS = ["https://api1.binance.com", "https://api2.binance.com", "https://fapi.binance.com"] # Spot ve Futures

sent_signals = {}

def send_telegram(message):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, data={'chat_id': CHAT_ID, 'text': message, 'parse_mode': 'Markdown', 'disable_web_page_preview': 'True'}, timeout=10)
    except: pass

# 1. PARA AKIŞI: SPOT VE KALDIRAÇ AYRIMI (OPEN INTEREST)
def get_oi_analysis(symbol):
    try:
        f_url = f"https://fapi.binance.com/fapi/v1/openInterest?symbol={symbol}"
        oi_data = requests.get(f_url, timeout=5).json()
        oi = float(oi_data['openInterest'])
        # Burada basitleştirilmiş bir mantıkla OI artış hızı ölçülebilir
        return oi
    except: return 0

# 2. ORDER BOOK: BALİNA DUVARI KONTROLÜ
def get_order_book_status(symbol):
    try:
        url = f"https://api1.binance.com/api/v3/depth?symbol={symbol}&limit=100"
        res = requests.get(url, timeout=5).json()
        bids = sum([float(p)*float(q) for p, q in res['bids'][:20]]) # İlk 20 kademe alım
        asks = sum([float(p)*float(q) for p, q in res['asks'][:20]]) # İlk 20 kademe satım
        return "GÜÇLÜ" if bids > asks * 2 else "ZAYIF"
    except: return "BİLİNMİYOR"

# 3. TEKNİK ANALİZ MOTORU (RSI, BOLLINGER, DESTEK/DİRENÇ)
def analyze_technical(symbol):
    try:
        url = f"https://api1.binance.com/api/v3/klines?symbol={symbol}&interval=15m&limit=100"
        r = requests.get(url, timeout=5).json()
        df = pd.DataFrame(r, columns=['ts','o','h','l','c','v','ct','qa','nt','tb','tq','i'])
        df[['c','v','h','l']] = df[['c','v','h','l']].astype(float)
        
        # RSI
        delta = df['c'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        rsi = 100 - (100 / (1 + (gain / loss.replace(0, 0.001)))).iloc[-1]
        
        # Bollinger
        sma = df['c'].rolling(20).mean()
        std = df['c'].rolling(20).std()
        upper_bb = (sma + (std * 2)).iloc[-1]
        lower_bb = (sma - (std * 2)).iloc[-1]
        
        # Destek / Direnç (Basit Pivot)
        last_price = df['c'].iloc[-1]
        support = df['l'].tail(50).min()
        resistance = df['h'].tail(50).max()
        
        return rsi, lower_bb, upper_bb, last_price, support, resistance, df['v'].iloc[-1] / df['v'].iloc[-21:-1].mean()
    except: return None

print("🚀 PRO BOT v3.0 BAŞLATILDI - ZIRHLI MOD", flush=True)

while True:
    try:
        # Tüm Coin Listesi
        all_res = requests.get("https://api1.binance.com/api/v3/exchangeInfo").json()
        symbols = [s['symbol'] for s in all_res['symbols'] if s['status'] == 'TRADING' and s['quoteAsset'] == 'USDT' and s['baseAsset'] not in EXCLUDED]
        
        print(f"🔄 Tarama Başladı: {len(symbols)} coin inceleniyor...", flush=True)
        
        for s in symbols:
            tech = analyze_technical(s)
            if not tech: continue
            
            rsi, l_bb, u_bb, price, sup, res, vol_ratio = tech
            
            # --- SENARYO 1: DİP AVCISI (RSI + BB) ---
            if rsi < 25 and price <= l_bb:
                score = 6
                signal_type = "🛡️ DİP AVCISI"
                
                # Balina Onayı (Derinlik)
                depth = get_order_book_status(s)
                if depth == "GÜÇLÜ": score += 2
                
                # Para Girişi (OI Kontrolü - Gerçek Spot mu?)
                # OI düşerken fiyat düşüyorsa bu panik satışı ve alım fırsatıdır
                
                if score >= 7:
                    if s not in sent_signals or (time.time() - sent_signals[s]) > 14400:
                        target = price * 1.03 # %3 Kar al
                        stop = price * 0.97   # %3 Stop
                        pot = ((res - price) / price) * 100
                        
                        msg = (f"{signal_type}: #{s}\n"
                               f"⭐ SKOR: {score}/10\n"
                               f"📊 RSI: {rsi:.1f} | Hacim: {vol_ratio:.1f}x\n"
                               f"🐋 Balina Desteği: {depth}\n"
                               f"🎯 Hedef: {target:.4f} (%3)\n"
                               f"🛑 Stop: {stop:.4f}\n"
                               f"📈 Kar Potansiyeli: %{pot:.1f}\n"
                               f"🔗 [Binance](https://www.binance.com/en/trade/{s.replace('USDT','_USDT')})")
                        send_telegram(msg)
                        sent_signals[s] = time.time()
            
            time.sleep(0.05) # Rate limit koruması

        print(f"✅ Döngü bitti. 1 dakika mola. {datetime.now().strftime('%H:%M:%S')}", flush=True)
        time.sleep(60)

    except Exception as e:
        print(f"💥 Ana Döngü Hatası: {e}", flush=True)
        time.sleep(10)
