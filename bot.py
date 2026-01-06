import pandas as pd
import numpy as np
import time
import requests
import urllib3
from flask import Flask
import threading
from datetime import datetime, timedelta

# --- 1. WEB SUNUCUSU ---
app = Flask('')

@app.route('/')
def home():
    return "Zirhli Bot v4.0.1 - LOG SADELESTIRILMIS"

def run_web():
    app.run(host='0.0.0.0', port=8080)

# Render'ın kapanmaması için sunucuyu arka planda başlat
threading.Thread(target=run_web, daemon=True).start()

# Gereksiz uyarıları kapat
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- 2. AYARLAR VE KIMLIK BILGILERI ---
TELEGRAM_TOKEN = "7583261338:AAHASreSYIaX-6QAXIUflpyf5HnbQXq81Dg"
CHAT_ID = "5124859166"
EXCLUDED = ['USDC', 'FDUSD', 'TUSD', 'USDP', 'BUSD', 'DAI', 'EUR', 'TRY', 'GBP', 'PAXG']
sent_signals = {}

# --- 3. MODÜL: TELEGRAM ---
def send_telegram_msg(msg):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        data = {
            'chat_id': CHAT_ID,
            'text': msg,
            'parse_mode': 'Markdown'
        }
        requests.post(url, data=data, timeout=10)
    except Exception as e:
        print(f"Telegram gönderim hatası: {e}")

# --- 4. MODÜL: BALINA DERINLIK ANALIZI ---
def get_order_book_depth(symbol):
    try:
        url = f"https://api1.binance.com/api/v3/depth?symbol={symbol}&limit=100"
        res = requests.get(url, timeout=5).json()
        
        bids_data = res.get('bids', [])
        asks_data = res.get('asks', [])
        
        bid_vol = sum([float(p) * float(q) for p, q in bids_data])
        ask_vol = sum([float(p) * float(q) for p, q in asks_data])
        
        if ask_vol > 0:
            return bid_vol / ask_vol
        else:
            return 1.0
    except:
        return 1.0

# --- 5. MODÜL: POZITIF UYUMSUZLUK (DIVERGENCE) ---
def check_divergence(df):
    try:
        current_price = df['c'].iloc[-1]
        min_price_prev = df['c'].iloc[-15:-1].min()
        
        current_rsi = df['rsi'].iloc[-1]
        min_rsi_prev = df['rsi'].iloc[-15:-1].min()
        
        # Fiyat yeni dip yaparken RSI daha yüksekte kalıyorsa
        if current_price <= min_price_prev and current_rsi > min_rsi_prev:
            return "POZİTİF 📈"
        return "YOK"
    except:
        return "YOK"

# --- 6. MODÜL: TEKNIK ANALIZ MOTORU ---
def get_analysis(symbol):
    try:
        url = f"https://api1.binance.com/api/v3/klines?symbol={symbol}&interval=15m&limit=100"
        r = requests.get(url, timeout=10).json()
        
        df = pd.DataFrame(r, columns=['ts','o','h','l','c','v','ct','qa','nt','tb','tq','i'])
        df[['c','v','h','l']] = df[['c','v','h','l']].astype(float)
        
        # RSI Hesaplama
        delta = df['c'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        rs = gain / (loss + 1e-6)
        df['rsi'] = 100 - (100 / (1 + rs))
        
        # EMA 200 (Trend)
        df['ema200'] = df['c'].ewm(span=200, adjust=False).mean()
        
        # Bollinger Alt Bant
        df['sma20'] = df['c'].rolling(20).mean()
        df['std20'] = df['c'].rolling(20).std()
        df['l_bb'] = df['sma20'] - (df['std20'] * 2.2)
        
        # Hacim Ortalaması (Son 20 mum)
        vol_avg = df['v'].iloc[-21:-1].mean()
        
        # Son 50 mumun en yüksek seviyesi (Roket senaryosu için)
        h50_max = df['h'].iloc[-50:-1].max()
        
        return {
            'p': df['c'].iloc[-1],
            'rsi': df['rsi'].iloc[-1],
            'l_bb': df['l_bb'].iloc[-1],
            'ema': df['ema200'].iloc[-1],
            'vol': df['v'].iloc[-1] / (vol_avg + 1e-6),
            'div': check_divergence(df),
            'h50': h50_max
        }
    except:
        return None

# --- 7. ANA DÖNGÜ VE AKILLI LOGLAMA ---
print("🚀 ZIRHLI BOT v4.0.1 - SIFIR TAVİZ MODU AKTİF", flush=True)

while True:
    try:
        # Binance'den USDT paritelerini çek
        response = requests.get("https://api1.binance.com/api/v3/exchangeInfo").json()
        symbols = [s['symbol'] for s in response['symbols'] if s['status'] == 'TRADING' and s['quoteAsset'] == 'USDT' and s['baseAsset'] not in EXCLUDED]
        
        # TUR BAŞLANGICI LOGU (SENİN İSTEDİĞİN GİBİ SADE)
        current_time = datetime.now().strftime('%H:%M:%S')
        print(f"\n🔄 {current_time} | {len(symbols)} Coin taranıyor...", flush=True)

        for s in symbols:
            # Tek tek coin yazdıran satır silindi, sistem şişmeyecek.
            
            d = get_analysis(s)
            if not d:
                continue
            
            score = 0
            scens = []
            whale_ratio = get_order_book_depth(s)

            # --- SENARYO 1: DİP ANALİZİ ---
            if (d['rsi'] < 26 or d['p'] <= d['l_bb']) and d['vol'] > 1.2:
                score += 4
                scens.append("🛡️ DİP")
            
            # --- SENARYO 2: UYUMSUZLUK ---
            if d['div'] == "POZİTİF 📈" and d['rsi'] < 35:
                score += 5
                scens.append("🚀 UYUMSUZLUK")
            
            # --- SENARYO 3: ROKET (HACİMLİ KIRILIM) ---
            if d['p'] > d['h50'] and d['vol'] > 3.5:
                score += 7
                scens.append("⚡ ROKET")

            # --- EKSTRA PUANLAMALAR ---
            if whale_ratio > 3.0: 
                score += 4
            elif whale_ratio > 2.0: 
                score += 2
                
            if d['vol'] > 4.5: 
                score += 1
            if d['p'] > d['ema']: 
                score += 1
            if d['rsi'] < 18: 
                score += 1

            # --- SİNYAL GÖNDERİM EŞİĞİ (SKOR 9+) ---
            if score >= 9:
                # 4 saatlik (14400 sn) sinyal koruması
                if s not in sent_signals or (time.time() - sent_signals[s]) > 14400:
                    
                    p_formatted = "{:.8f}".format(d['p']).rstrip('0').rstrip('.')
                    
                    msg = (f"*{' / '.join(scens)}*: #{s}\n"
                           f"⭐ SKOR: {score}/10\n"
                           f"━━━━━━━━━━━━━━━\n"
                           f"💵 Giriş: `{p_formatted}`\n"
                           f"📊 RSI: {d['rsi']:.1f}\n"
                           f"🐋 Balina: x{whale_ratio:.1f} | Hacim: {d['vol']:.1f}x\n"
                           f"━━━━━━━━━━━━━━━\n"
                           f"🔗 [Binance](https://www.binance.com/en/trade/{s.replace('USDT','_USDT')})")
                    
                    send_telegram_msg(msg)
                    sent_signals[s] = time.time()
                    
                    # Sinyal gidince loga yaz
                    print(f"✅ SİNYAL GÖNDERİLDİ: {s} | Skor: {score}", flush=True)

            # API ban yememek için milisaniyelik bekleme
            time.sleep(0.05)

        # TUR BİTİŞİ LOGU
        finish_time = datetime.now().strftime('%H:%M:%S')
        next_start = (datetime.now() + timedelta(seconds=60)).strftime('%H:%M:%S')
        print(f"🏁 {finish_time} | Tarama Bitti. Sonraki Tarama: {next_start}", flush=True)
        
        # 1 dakika bekle ve baştan başla
        time.sleep(60)

    except Exception as e:
        print(f"❌ Ana Döngü Hatası: {e}", flush=True)
        time.sleep(10)
