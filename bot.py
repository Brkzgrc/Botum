import pandas as pd
import numpy as np
import time
import requests
import threading
from datetime import datetime
from flask import Flask

# --- 1. SUNUCU AYARI (RENDER KEEP-ALIVE) ---
app = Flask('')
@app.route('/')
def home(): return "Sistem Aktif"
def run_web(): app.run(host='0.0.0.0', port=8080)
threading.Thread(target=run_web, daemon=True).start()

# --- 2. AYARLAR VE KİMLİK ---
TELEGRAM_TOKEN = "7583261338:AAHASreSYIaX-6QAXIUflpyf5HnbQXq81Dg"
CHAT_ID = "5124859166"
EXCLUDED = ['USDC', 'FDUSD', 'TUSD', 'USDP', 'BUSD', 'DAI', 'EUR', 'TRY', 'PAXG']
sent_signals = {}

# --- 3. VERİ VE TEKNİK ANALİZ MOTORU (FULL ANAYASA) ---
def get_data(symbol, interval):
    try:
        url = f"https://api1.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit=200"
        r = requests.get(url, timeout=10).json()
        df = pd.DataFrame(r, columns=['ts','o','h','l','c','v','ct','qa','nt','tb','tq','i'])
        df[['c','v','h','l','o', 'tb']] = df[['c','v','h','l','o', 'tb']].astype(float)
        
        # --- ANAYASA İNDİKATÖRLERİ ---
        # OBV (Trend Onayı)
        df['obv'] = (np.sign(df['c'].diff()) * df['v']).fillna(0).cumsum()
        # Hacim Delta (Madde 3: Agresif Alıcı)
        df['delta'] = (df['tb'] / df['v']) * 100
        # RSI ve EMA 200 (Ana Trend)
        delta = df['c'].diff(); gain = (delta.where(delta > 0, 0)).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        df['rsi'] = 100 - (100 / (1 + (gain / (loss + 1e-6))))
        df['ema200'] = df['c'].ewm(span=200, adjust=False).mean()
        
        # --- MADDE 2: ADX (TAM MATEMATİKSEL) ---
        plus_dm = df['h'].diff(); minus_dm = df['l'].diff()
        tr = pd.concat([df['h']-df['l'], abs(df['h']-df['c'].shift()), abs(df['l']-df['c'].shift())], axis=1).max(axis=1)
        atr = tr.rolling(14).mean()
        p_dm = (plus_dm.rolling(14).mean() / (atr + 1e-6)) * 100
        m_dm = (minus_dm.rolling(14).mean() / (atr + 1e-6)) * 100
        df['adx'] = (abs((p_dm - m_dm) / (p_dm + m_dm + 1e-6)) * 100).rolling(14).mean()
        
        # --- MADDE 5: PRICE ACTION (UYUMSUZLUK & SR-FLIP) ---
        # RSI Pozitif Uyumsuzluk
        df['low_min_5'] = df['l'].shift(1).rolling(5).min()
        df['rsi_min_5'] = df['rsi'].shift(1).rolling(5).min()
        df['bull_div'] = (df['l'] < df['low_min_5']) & (df['rsi'] > df['rsi_min_5']) & (df['rsi'] < 35)
        
        # SR-Flip (Direnç Kırılımı)
        df['recent_high'] = df['h'].rolling(30).max().shift(1)
        df['sr_break'] = (df['c'] > df['recent_high']) & (df['v'] > df['v'].rolling(20).mean() * 1.5)
        
        # --- MADDE 4: FIBONACCI RADAR ---
        h_max = df['h'].max(); l_min = df['l'].min()
        df['fib_618'] = h_max - (0.618 * (h_max - l_min))
        
        # Bollinger 2.2
        df['upper'] = df['c'].rolling(20).mean() + (2.2 * df['c'].rolling(20).std())
        
        return df
    except: return None

# --- 4. BALİNA VE DERİNLİK ANALİZİ (ANAYASA) ---
def get_whale_ratio(symbol):
    try:
        res = requests.get(f"https://api1.binance.com/api/v3/depth?symbol={symbol}&limit=100", timeout=5).json()
        bids = sum([float(p) * float(q) for p, q in res.get('bids', [])])
        asks = sum([float(p) * float(q) for p, q in res.get('asks', [])])
        return bids / asks if asks > 0 else 1.0
    except: return 1.0

# --- 5. ANA TARAMA DÖNGÜSÜ ---
def scan():
    print(f"\n🔄 {datetime.now().strftime('%H:%M:%S')} | TARAMA DÖNGÜSÜ BAŞLATILDI...", flush=True)
    
    # BTC Koruması (Anayasa)
    df_btc = get_data("BTCUSDT", "15m")
    if df_btc is None: return
    btc_change = ((df_btc['c'].iloc[-1] - df_btc['c'].iloc[-4]) / df_btc['c'].iloc[-4]) * 100
    btc_safe = btc_change > -1.0
    
    try:
        info = requests.get("https://api1.binance.com/api/v3/exchangeInfo").json()
        symbols = [s['symbol'] for s in info['symbols'] if s['status'] == 'TRADING' and s['quoteAsset'] == 'USDT' and s['baseAsset'] not in EXCLUDED]
        print(f"📡 {len(symbols)} coin analiz ediliyor... BTC: %{btc_change:.2f}", flush=True)

        for s in symbols:
            df15 = get_data(s, "15m"); df1h = get_data(s, "1h")
            if df15 is None or df1h is None: continue
            
            last = df15.iloc[-1]; score = 0; signal_type = ""
            whale = get_whale_ratio(s); avg_vol = df15['v'].rolling(20).mean().iloc[-1]
            
            # --- ANAYASA STRATEJİLERİ ---
            if last['bull_div']: 
                score += 6; signal_type = "📉 RSI UYUMSUZLUK (BOĞA)"
            elif last['sr_break']: 
                score += 5; signal_type = "🧱 SR-FLIP (KIRILIM)"
            elif last['v'] > (avg_vol * 3.5) and last['c'] > last['upper']: 
                score += 5; signal_type = "🚀 ROKET"
            elif btc_change < -0.5 and ((last['c'] - df15['c'].iloc[-4]) / df15['c'].iloc[-4] * 100) > 0: 
                score += 4; signal_type = "⚡ AKINTIYA KARŞI"
            elif df1h['c'].iloc[-1] > df1h['ema200'].iloc[-1] and last['rsi'] < 30: 
                score += 4; signal_type = "🛡️ DİP AVCISI"

            # EK PUANLAR
            if whale > 2.5: score += 3 # Balina Oranı (Anayasa)
            if last['adx'] > 25: score += 2 # Trend Gücü (Madde 2)
            if last['delta'] > 55: score += 2 # Hacim Delta (Madde 3)
            if last['obv'] > df15['obv'].iloc[-5]: score += 1 # OBV Gücü (Anayasa)
            
            # --- KARAR VE TELEGRAM ---
            if score >= 9:
                if not btc_safe and "AKINTIYA" not in signal_type: continue
                
                # Madde 1: Swing Low Stop ve 1:3 Kâr Hedefi
                stop_l = df15['l'].rolling(20).min().iloc[-1] * 0.995
                risk = last['c'] - stop_l
                t_profit = last['c'] + (risk * 3.0)
                fib_inf = "YOL AÇIK" if last['c'] > last['fib_618'] else "DİRENÇ VAR"

                if s not in sent_signals or (time.time() - sent_signals[s]) > 14400:
                    msg = (f"⭐ **{signal_type}**\n\n"
                           f"Coin: #{s}\nFiyat: {last['c']:.8f}\nSkor: {score}/10\n"
                           f"🛡️ Stop: {stop_l:.8f}\n🎯 Hedef: {t_profit:.8f}\n"
                           f"📉 Fib Radar: {fib_inf}\n🐳 Whale: x{whale:.1f} | ADX: {last['adx']:.1f}\n"
                           f"🧡 BTC: %{btc_change:.2f}\n\n[Binance'de Aç](https://www.binance.com/en/trade/{s}_USDT)")
                    
                    requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", 
                                  data={'chat_id': CHAT_ID, 'text': msg, 'parse_mode': 'Markdown'})
                    sent_signals[s] = time.time(); print(f"✅ SİNYAL: {s}", flush=True)
            time.sleep(0.05)
            
    except Exception as e: print(f"❌ Hata: {e}", flush=True)
    print(f"🏁 Tarama Bitti. 60 saniye bekleniyor...", flush=True)

while True:
    scan(); time.sleep(60)
