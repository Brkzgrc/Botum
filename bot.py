import pandas as pd
import numpy as np
import time
import requests
import threading
from datetime import datetime
from flask import Flask

# --- SUNUCU AYARI (KEEP-ALIVE) ---
app = Flask('')
@app.route('/')
def home(): return "Sistem Aktif"
def run_web(): app.run(host='0.0.0.0', port=8080)
threading.Thread(target=run_web, daemon=True).start()

# --- AYARLAR VE KİMLİK ---
TELEGRAM_TOKEN = "7583261338:AAHASreSYIaX-6QAXIUflpyf5HnbQXq81Dg"
CHAT_ID = "5124859166"
EXCLUDED = ['USDC', 'FDUSD', 'TUSD', 'USDP', 'BUSD', 'DAI', 'EUR', 'TRY', 'PAXG']
sent_signals = {}

# --- VERİ VE İNDİKATÖR MOTORU (GÜNCELLENDİ) ---
def get_data(symbol, interval):
    try:
        url = f"https://api1.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit=200"
        r = requests.get(url, timeout=10).json()
        df = pd.DataFrame(r, columns=['ts','o','h','l','c','v','ct','qa','nt','tb','tq','i'])
        df[['c','v','h','l','o']] = df[['c','v','h','l','o']].astype(float)
        
        # 1. OBV & Hacim Delta (Net Alım)
        df['obv'] = (np.sign(df['c'].diff()) * df['v']).fillna(0).cumsum()
        
        # 2. RSI, EMA 200 ve ADX (Trend Gücü - Madde 2)
        delta = df['c'].diff(); gain = (delta.where(delta > 0, 0)).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        df['rsi'] = 100 - (100 / (1 + (gain / (loss + 1e-6))))
        df['ema200'] = df['c'].ewm(span=200, adjust=False).mean()
        
        # ADX Hesaplama
        plus_dm = df['h'].diff(); minus_dm = df['l'].diff()
        tr = pd.concat([df['h']-df['l'], abs(df['h']-df['c'].shift()), abs(df['l']-df['c'].shift())], axis=1).max(axis=1)
        atr = tr.rolling(14).mean()
        df['adx'] = (abs((plus_dm.rolling(14).mean() - minus_dm.rolling(14).mean()) / (plus_dm.rolling(14).mean() + minus_dm.rolling(14).mean())) * 100).rolling(14).mean()
        
        # 3. Bollinger (2.2 Sapma)
        df['sma'] = df['c'].rolling(20).mean()
        df['std'] = df['c'].rolling(20).std()
        df['upper'] = df['sma'] + (2.2 * df['std'])
        df['lower'] = df['sma'] - (2.2 * df['std'])
        
        # 4. Fibonacci Radar (Madde 4 - Hedef Belirleyici)
        high_max = df['h'].max(); low_min = df['l'].min()
        diff = high_max - low_min
        df['fib_618'] = high_max - (0.618 * diff)
        
        return df
    except: return None

def get_whale_ratio(symbol):
    try:
        res = requests.get(f"https://api1.binance.com/api/v3/depth?symbol={symbol}&limit=100", timeout=5).json()
        bids = sum([float(p) * float(q) for p, q in res.get('bids', [])])
        asks = sum([float(p) * float(q) for p, q in res.get('asks', [])])
        return bids / asks if asks > 0 else 1.0
    except: return 1.0

def get_btc_status():
    df_btc = get_data("BTCUSDT", "15m")
    if df_btc is None: return 0, False
    change = ((df_btc['c'].iloc[-1] - df_btc['c'].iloc[-4]) / df_btc['c'].iloc[-4]) * 100
    return change, (change > -1.0)

# --- ANA TARAMA ---
def scan():
    print(f"🔄 {datetime.now().strftime('%H:%M:%S')} Tarama Başladı...", flush=True)
    btc_change, btc_safe = get_btc_status()
    
    try:
        info = requests.get("https://api1.binance.com/api/v3/exchangeInfo").json()
        symbols = [s['symbol'] for s in info['symbols'] if s['status'] == 'TRADING' and s['quoteAsset'] == 'USDT' and s['baseAsset'] not in EXCLUDED]
        
        for s in symbols:
            df15 = get_data(s, "15m"); df1h = get_data(s, "1h")
            if df15 is None or df1h is None: continue
            
            last = df15.iloc[-1]; prev = df15.iloc[-2]
            avg_vol = df15['v'].rolling(20).mean().iloc[-1]
            whale = get_whale_ratio(s)
            
            # --- STRATEJİ MANTIĞI ---
            score = 0; signal_type = ""
            
            # ADX Filtresi (Madde 2) - Trend Zayıfsa Puan Kır
            trend_strong = last['adx'] > 25
            
            # Roket & Akıntıya Karşı & Dip Avcısı (Eski Mantık Korundu)
            if last['v'] > (avg_vol * 3.5) and last['c'] > last['upper']:
                score += 5; signal_type = "🚀 ROKET (KIRILIM)"
            
            coin_change = ((last['c'] - df15['c'].iloc[-4]) / df15['c'].iloc[-4]) * 100
            if btc_change < -0.5 and coin_change > 0:
                score += 4; signal_type = "⚡ AKINTIYA KARŞI"

            if df1h['c'].iloc[-1] > df1h['ema200'].iloc[-1] and last['rsi'] < 30:
                score += 4; signal_type = "🛡️ DİP AVCISI"

            if whale > 2.5: score += 3
            if trend_strong: score += 2
            
            # --- TP/SL VE PRICE ACTION (Madde 1 & 5) ---
            if score >= 9:
                if not btc_safe and "AKINTIYA" not in signal_type: continue
                
                # Swing Low (Son 20 mumun dibi) - Stop Seviyesi
                stop_loss = df15['l'].rolling(20).min().iloc[-1] * 0.995
                risk = last['c'] - stop_loss
                take_profit = last['c'] + (risk * 3.0) # 1:3 R/R
                
                # Sinyal Gönderim
                if s not in sent_signals or (time.time() - sent_signals[s]) > 14400:
                    fib_status = "Önü Açık" if last['c'] > last['fib_618'] else "Direnç Yakın"
                    msg = (f"⭐ **{signal_type}**\n\nCoin: #{s}\nFiyat: {last['c']:.8f}\n"
                           f"Skor: {score}/10 | ADX: {last['adx']:.1f}\n"
                           f"🛡️ Stop: {stop_loss:.8f}\n🎯 Hedef: {take_profit:.8f}\n"
                           f"📊 Fib Radar: {fib_status}\n"
                           f"🐳 Balina: x{whale:.1f} | BTC: %{btc_change:.2f}\n\n"
                           f"[Binance'de Aç](https://www.binance.com/en/trade/{s}_USDT)")
                    
                    requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", data={'chat_id': CHAT_ID, 'text': msg, 'parse_mode': 'Markdown'})
                    sent_signals[s] = time.time()
            time.sleep(0.1)
            
    except Exception as e: print(f"Hata: {e}")
    print(f"✅ Tarama Bitti. 60 saniye sonra tekrar başlayacak...", flush=True)

while True:
    scan()
    time.sleep(60)
