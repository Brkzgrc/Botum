import pandas as pd
import numpy as np
import time
import requests
import threading
from datetime import datetime
from flask import Flask

# --- SUNUCU AYARI (RENDER KEEP-ALIVE) ---
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

# --- 1. VERİ VE İNDİKATÖR MOTORU (ANAYASA + YENİ MADDELER) ---
def get_data(symbol, interval):
    try:
        url = f"https://api1.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit=200"
        r = requests.get(url, timeout=10).json()
        df = pd.DataFrame(r, columns=['ts','o','h','l','c','v','ct','qa','nt','tb','tq','i'])
        df[['c','v','h','l','o']] = df[['c','v','h','l','o']].astype(float)
        
        # OBV (Anayasa Filtresi)
        df['obv'] = (np.sign(df['c'].diff()) * df['v']).fillna(0).cumsum()
        
        # RSI ve EMA 200 (Anayasa Filtresi)
        delta = df['c'].diff(); gain = (delta.where(delta > 0, 0)).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        df['rsi'] = 100 - (100 / (1 + (gain / (loss + 1e-6))))
        df['ema200'] = df['c'].ewm(span=200, adjust=False).mean()
        
        # ADX (MADDE 2: Trend Gücü Ölçümü)
        plus_dm = df['h'].diff(); minus_dm = df['l'].diff()
        tr = pd.concat([df['h']-df['l'], abs(df['h']-df['c'].shift()), abs(df['l']-df['c'].shift())], axis=1).max(axis=1)
        atr = tr.rolling(14).mean()
        p_dm = (plus_dm.rolling(14).mean() / atr) * 100
        m_dm = (minus_dm.rolling(14).mean() / atr) * 100
        df['adx'] = (abs((p_dm - m_dm) / (p_dm + m_dm + 1e-6)) * 100).rolling(14).mean()
        
        # Bollinger (Anayasa Özelliği: 2.2 Sapma)
        df['sma'] = df['c'].rolling(20).mean()
        df['std'] = df['c'].rolling(20).std()
        df['upper'] = df['sma'] + (2.2 * df['std'])
        df['lower'] = df['sma'] - (2.2 * df['std'])
        
        # Fibonacci Radar (MADDE 4: Hedef Kontrolü)
        h_max = df['h'].max(); l_min = df['l'].min()
        df['fib_618'] = h_max - (0.618 * (h_max - l_min))
        
        return df
    except: return None

# --- BALİNA ANALİZİ (ANAYASA ÖZELLİĞİ) ---
def get_whale_ratio(symbol):
    try:
        res = requests.get(f"https://api1.binance.com/api/v3/depth?symbol={symbol}&limit=100", timeout=5).json()
        bids = sum([float(p) * float(q) for p, q in res.get('bids', [])])
        asks = sum([float(p) * float(q) for p, q in res.get('asks', [])])
        return bids / asks if asks > 0 else 1.0
    except: return 1.0

# --- ANA TARAMA DÖNGÜSÜ ---
def scan():
    print(f"\n🔄 {datetime.now().strftime('%H:%M:%S')} | TARAMA DÖNGÜSÜ BAŞLATILDI...", flush=True)
    
    # BTC Koruması (Anayasa Filtresi)
    df_btc = get_data("BTCUSDT", "15m")
    if df_btc is None: return
    btc_change = ((df_btc['c'].iloc[-1] - df_btc['c'].iloc[-4]) / df_btc['c'].iloc[-4]) * 100
    btc_safe = btc_change > -1.0
    
    try:
        info = requests.get("https://api1.binance.com/api/v3/exchangeInfo").json()
        symbols = [s['symbol'] for s in info['symbols'] if s['status'] == 'TRADING' and s['quoteAsset'] == 'USDT' and s['baseAsset'] not in EXCLUDED]
        print(f"📡 {len(symbols)} adet coin işleniyor...", flush=True)

        for s in symbols:
            df15 = get_data(s, "15m"); df1h = get_data(s, "1h")
            if df15 is None or df1h is None: continue
            
            last = df15.iloc[-1]
            avg_vol = df15['v'].rolling(20).mean().iloc[-1]
            whale = get_whale_ratio(s)
            score = 0; signal_type = ""
            
            # --- ANAYASA STRATEJİLERİ ---
            # 1. ROKET
            if last['v'] > (avg_vol * 3.5) and last['c'] > last['upper']:
                score += 5; signal_type = "🚀 ROKET (KIRILIM)"
            
            # 2. AKINTIYA KARŞI (Pozitif Ayrışma)
            c_change = ((last['c'] - df15['c'].iloc[-4]) / df15['c'].iloc[-4]) * 100
            if btc_change < -0.5 and c_change > 0:
                score += 4; signal_type = "⚡ AKINTIYA KARŞI"

            # 3. DİP AVCISI (MTF EMA 200 Kontrolü)
            if df1h['c'].iloc[-1] > df1h['ema200'].iloc[-1] and last['rsi'] < 30:
                score += 4; signal_type = "🛡️ DİP AVCISI"

            # EK PUANLAR
            if whale > 2.5: score += 3 # Balina Desteği (Anayasa)
            if last['adx'] > 25: score += 2 # Trend Gücü (Yeni)
            if last['obv'] > df15['obv'].iloc[-5]: score += 1 # OBV Artışı (Anayasa)
            
            # --- SON KARAR VE TP/SL (MADDE 1 & 5) ---
            if score >= 9:
                if not btc_safe and "AKINTIYA" not in signal_type: continue
                
                # Swing Low bazlı Stop Loss ve 1:3 Hedef
                stop_l = df15['l'].rolling(20).min().iloc[-1] * 0.995
                t_profit = last['c'] + ((last['c'] - stop_l) * 3.0)
                fib_inf = "Açık" if last['c'] > last['fib_618'] else "Dirençli"

                if s not in sent_signals or (time.time() - sent_signals[s]) > 14400:
                    msg = (f"⭐ **{signal_type}**\n\n"
                           f"Coin: #{s}\nFiyat: {last['c']:.8f}\nSkor: {score}/10\n"
                           f"🛡️ Stop: {stop_l:.8f}\n🎯 Hedef: {t_profit:.8f}\n"
                           f"📉 Fib Radar: {fib_inf}\n"
                           f"🐳 Balina: x{whale:.1f} | ADX: {last['adx']:.1f}\n"
                           f"🧡 BTC: %{btc_change:.2f}\n\n"
                           f"[Binance'de Aç](https://www.binance.com/en/trade/{s}_USDT)")
                    
                    requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", data={'chat_id': CHAT_ID, 'text': msg, 'parse_mode': 'Markdown'})
                    sent_signals[s] = time.time()
                    print(f"✅ SİNYAL GÖNDERİLDİ: {s}", flush=True)
            time.sleep(0.05) # API Güvenlik Gecikmesi
            
    except Exception as e: print(f"❌ Hata: {e}", flush=True)
    print(f"✅ Tur tamamlandı. 60 saniye sonra tekrar başlayacak...", flush=True)

while True:
    scan()
    time.sleep(60)
