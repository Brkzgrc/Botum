import pandas as pd
import numpy as np
import time
import requests
import threading
from datetime import datetime
from flask import Flask

# --- SUNUCU AYARI (RENDER KAPANMAMASI İÇİN) ---
app = Flask('')
@app.route('/')
def home(): return "Sistem Aktif"
def run_web(): app.run(host='0.0.0.0', port=8080)
threading.Thread(target=run_web, daemon=True).start()

# --- AYARLAR VE KİMLİK (SENİN BİLGİLERİN) ---
TELEGRAM_TOKEN = "7583261338:AAHASreSYIaX-6QAXIUflpyf5HnbQXq81Dg"
CHAT_ID = "5124859166"
EXCLUDED = ['USDC', 'FDUSD', 'TUSD', 'USDP', 'BUSD', 'DAI', 'EUR', 'TRY', 'PAXG']
sent_signals = {}

# --- 1. VERİ VE İNDİKATÖR MOTORU (HİÇBİRİ SİLİNMEDİ) ---
def get_data(symbol, interval):
    try:
        url = f"https://api1.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit=200"
        r = requests.get(url, timeout=10).json()
        df = pd.DataFrame(r, columns=['ts','o','h','l','c','v','ct','qa','nt','tb','tq','i'])
        df[['c','v','h','l','o']] = df[['c','v','h','l','o']].astype(float)
        
        # OBV (Para Girişi)
        df['obv'] = (np.sign(df['c'].diff()) * df['v']).fillna(0).cumsum()
        
        # RSI ve EMA 200 (Trend Onayı)
        delta = df['c'].diff(); gain = (delta.where(delta > 0, 0)).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        df['rsi'] = 100 - (100 / (1 + (gain / (loss + 1e-6))))
        df['ema200'] = df['c'].ewm(span=200, adjust=False).mean()
        
        # Bollinger (2.2 Sapma - Senin Kriterin)
        df['sma'] = df['c'].rolling(20).mean()
        df['std'] = df['c'].rolling(20).std()
        df['upper'] = df['sma'] + (2.2 * df['std'])
        df['lower'] = df['sma'] - (2.2 * df['std'])
        
        return df
    except: return None

# --- 2. BALİNA ANALİZİ (ORDER BOOK DEEP) ---
def get_whale_ratio(symbol):
    try:
        res = requests.get(f"https://api1.binance.com/api/v3/depth?symbol={symbol}&limit=100", timeout=5).json()
        bids = sum([float(p) * float(q) for p, q in res.get('bids', [])])
        asks = sum([float(p) * float(q) for p, q in res.get('asks', [])])
        return bids / asks if asks > 0 else 1.0
    except: return 1.0

# --- 3. BTC KONTROL VE POZİTİF AYRIŞMA ---
def get_btc_status():
    df_btc = get_data("BTCUSDT", "15m")
    if df_btc is None: return 0, False
    change = ((df_btc['c'].iloc[-1] - df_btc['c'].iloc[-4]) / df_btc['c'].iloc[-4]) * 100
    is_safe = change > -1.0 # BTC %1'den fazla çakılmıyorsa güvenli
    return change, is_safe

# --- 4. ANA KARAR MEKANİZMASI ---
def scan():
    print(f"\n🔄 {datetime.now().strftime('%H:%M:%S')} Tarama Başladı...", flush=True)
    btc_change, btc_safe = get_btc_status()
    
    try:
        symbols = [s['symbol'] for s in requests.get("https://api1.binance.com/api/v3/exchangeInfo").json()['symbols'] 
                   if s['status'] == 'TRADING' and s['quoteAsset'] == 'USDT' and s['baseAsset'] not in EXCLUDED]
        
        for s in symbols:
            df15 = get_data(s, "15m")
            df1h = get_data(s, "1h") # MTF (1 Saatlik Trend Kontrolü)
            if df15 is None or df1h is None: continue
            
            # Değişkenler
            price = df15['c'].iloc[-1]
            last_vol = df15['v'].iloc[-1]
            avg_vol = df15['v'].rolling(20).mean().iloc[-1]
            whale = get_whale_ratio(s)
            
            # --- STRATEJİ SINIFLARI ---
            score = 0
            signal_type = ""
            
            # 1. ROKET (Sert Yükseliş)
            if last_vol > (avg_vol * 3.5) and price > df15['upper'].iloc[-1]:
                score += 5; signal_type = "🚀 ROKET (KIRILIM)"
            
            # 2. DİP AVCISI (Trend İçi)
            if df1h['c'].iloc[-1] > df1h['ema200'].iloc[-1] and df15['rsi'].iloc[-1] < 30:
                score += 4; signal_type = "🛡️ DİP AVCISI"
                
            # 3. POZİTİF AYRIŞMA (BTC'ye Kafa Tutanlar)
            coin_change = ((df15['c'].iloc[-1] - df15['c'].iloc[-4]) / df15['c'].iloc[-4]) * 100
            if btc_change < -0.5 and coin_change > 0:
                score += 4; signal_type = "⚡ AKINTIYA KARŞI (POZİTİF AYRIŞMA)"

            # EK PUANLAR (Balina ve Para Girişi)
            if whale > 2.5: score += 3
            if df15['obv'].iloc[-1] > df15['obv'].iloc[-5]: score += 2
            
            # GÖNDERİM (Skor 9+ ve BTC Kontrolü)
            if score >= 9:
                # BTC çakılırken sadece "Akıntıya Karşı" olanları gönder
                if not btc_safe and "AKINTIYA" not in signal_type: continue
                
                if s not in sent_signals or (time.time() - sent_signals[s]) > 14400:
                    msg = (f"⭐ **{signal_type}**\n\nCoin: #{s}\nFiyat: {price:.8f}\n"
                           f"Skor: {score}/10\nBalina Gücü: x{whale:.1f}\n"
                           f"Hacim Artışı: x{last_vol/avg_vol:.1f}\n"
                           f"BTC Durum: %{btc_change:.2f}\n\n"
                           f"[Binance'de Aç](https://www.binance.com/en/trade/{s}_USDT)")
                    
                    requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", 
                                  data={'chat_id': CHAT_ID, 'text': msg, 'parse_mode': 'Markdown'})
                    sent_signals[s] = time.time()
            time.sleep(0.1) # API Yasaklanmaması için hız sınırı
            
    except Exception as e: print(f"Hata: {e}")

while True:
    scan()
    time.sleep(60)
