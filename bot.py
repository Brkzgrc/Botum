import pandas as pd
import numpy as np
import time
import requests
import threading
from datetime import datetime
from flask import Flask

# --- 1. SUNUCU AYARI (RENDER KESİNTİSİZ ÇALIŞMA) ---
app = Flask('')

@app.route('/')
def home():
    return "Sistem Aktif"

def run_web():
    app.run(host='0.0.0.0', port=8080)

# Web sunucusunu ayrı bir thread'de başlat
threading.Thread(target=run_web, daemon=True).start()

# --- 2. AYARLAR VE KİMLİK BİLGİLERİ ---
TELEGRAM_TOKEN = "7583261338:AAHASreSYIaX-6QAXIUflpyf5HnbQXq81Dg"
CHAT_ID = "5124859166"
EXCLUDED = ['USDC', 'FDUSD', 'TUSD', 'USDP', 'BUSD', 'DAI', 'EUR', 'TRY', 'PAXG']
sent_signals = {}

# --- 3. VERİ VE TEKNİK ANALİZ MOTORU ---
def get_data(symbol, interval):
    try:
        url = f"https://api1.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit=200"
        response = requests.get(url, timeout=15)
        r = response.json()
        
        df = pd.DataFrame(r, columns=['ts','o','h','l','c','v','ct','qa','nt','tb','tq','i'])
        # Tüm sayısal verileri float tipine çevir (Hata payını sıfırla)
        df[['o','h','l','c','v','tb']] = df[['o','h','l','c','v','tb']].astype(float)
        
        # --- ANAYASA İNDİKATÖRLERİ ---
        # OBV (On-Balance Volume)
        df['obv'] = (np.sign(df['c'].diff()) * df['v']).fillna(0).cumsum()
        
        # Hacim Delta (Agresif Alıcı Oranı)
        df['delta'] = (df['tb'] / df['v']) * 100
        
        # RSI Hesaplaması (Standard 14 Period)
        delta_price = df['c'].diff()
        gain = (delta_price.where(delta_price > 0, 0)).rolling(window=14).mean()
        loss = (-delta_price.where(delta_price < 0, 0)).rolling(window=14).mean()
        rs = gain / (loss + 1e-9)
        df['rsi'] = 100 - (100 / (1 + rs))
        
        # EMA 200 (Ana Trend Belirleyici)
        df['ema200'] = df['c'].ewm(span=200, adjust=False).mean()
        
        # --- MADDE 2: ADX (TAM MATEMATİKSEL ATR BAZLI) ---
        df['up_move'] = df['h'].diff()
        df['down_move'] = df['l'].diff()
        
        df['plus_dm'] = np.where((df['up_move'] > df['down_move']) & (df['up_move'] > 0), df['up_move'], 0)
        df['minus_dm'] = np.where((df['down_move'] > df['up_move']) & (df['down_move'] > 0), df['down_move'], 0)
        
        tr1 = df['h'] - df['l']
        tr2 = abs(df['h'] - df['c'].shift(1))
        tr3 = abs(df['l'] - df['c'].shift(1))
        df['tr'] = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        
        atr = df['tr'].rolling(window=14).mean()
        df['plus_di'] = 100 * (df['plus_dm'].rolling(window=14).mean() / (atr + 1e-9))
        df['minus_di'] = 100 * (df['minus_dm'].rolling(window=14).mean() / (atr + 1e-9))
        df['dx'] = 100 * abs(df['plus_di'] - df['minus_di']) / (df['plus_di'] + df['minus_di'] + 1e-9)
        df['adx'] = df['dx'].rolling(window=14).mean()
        
        # --- MADDE 5: PRICE ACTION (UYUMSUZLUK ANALİZİ) ---
        # Pozitif Uyumsuzluk (Bullish Divergence)
        df['low_min_5'] = df['l'].shift(1).rolling(window=5).min()
        df['rsi_min_5'] = df['rsi'].shift(1).rolling(window=5).min()
        df['bull_div'] = (df['l'] < df['low_min_5']) & (df['rsi'] > df['rsi_min_5']) & (df['rsi'] < 35)
        
        # Negatif Uyumsuzluk (Bearish Divergence - Koruma Filtresi)
        df['high_max_5'] = df['h'].shift(1).rolling(window=5).max()
        df['rsi_max_5'] = df['rsi'].shift(1).rolling(window=5).max()
        df['bear_div'] = (df['h'] > df['high_max_5']) & (df['rsi'] < df['rsi_max_5']) & (df['rsi'] > 65)
        
        # SR-Flip (Direnç Kırılımı)
        df['recent_high_30'] = df['h'].rolling(window=30).max().shift(1)
        df['sr_break'] = (df['c'] > df['recent_high_30']) & (df['v'] > df['v'].rolling(20).mean() * 1.5)
        
        # --- MADDE 4: FIBONACCI RADAR ---
        highest_point = df['h'].max()
        lowest_point = df['l'].min()
        df['fib_618'] = highest_point - (0.618 * (highest_point - lowest_point))
        
        # Bollinger Bands (2.2 Sapma)
        df['ma20'] = df['c'].rolling(window=20).mean()
        df['std20'] = df['c'].rolling(window=20).std()
        df['upper_band'] = df['ma20'] + (2.2 * df['std20'])
        
        return df
    except Exception as e:
        print(f"Veri çekme hatası: {e}")
        return None

# --- 4. BALİNA ANALİZİ (ANAYASA - DERİNLİK) ---
def get_whale_ratio(symbol):
    try:
        res = requests.get(f"https://api1.binance.com/api/v3/depth?symbol={symbol}&limit=100", timeout=10).json()
        bids = sum([float(p) * float(q) for p, q in res.get('bids', [])])
        asks = sum([float(p) * float(q) for p, q in res.get('asks', [])])
        return bids / asks if asks > 0 else 1.0
    except:
        return 1.0

# --- 5. ANA TARAMA FONKSİYONU ---
def scan_market():
    start_time = datetime.now()
    print(f"\n🚀 {start_time.strftime('%H:%M:%S')} | PİYASA TARAMASI BAŞLATILDI...", flush=True)
    
    # BTC Koruması (Anayasa)
    df_btc = get_data("BTCUSDT", "15m")
    if df_btc is None: return
    
    btc_c = df_btc['c'].iloc[-1]
    btc_p = df_btc['c'].iloc[-4]
    btc_change = ((btc_c - btc_p) / btc_p) * 100
    
    try:
        # USDT paritelerini listele
        exchange_info = requests.get("https://api1.binance.com/api/v3/exchangeInfo").json()
        symbols = [s['symbol'] for s in exchange_info['symbols'] if s['status'] == 'TRADING' and s['quoteAsset'] == 'USDT' and s['baseAsset'] not in EXCLUDED]
        
        print(f"📡 {len(symbols)} coin analiz ediliyor. BTC Durumu: %{btc_change:.2f}", flush=True)

        for s in symbols:
            df15 = get_data(s, "15m")
            df1h = get_data(s, "1h")
            
            if df15 is None or df1h is None:
                continue
            
            last = df15.iloc[-1]
            whale = get_whale_ratio(s)
            score = 0
            signal_type = ""
            
            # --- STRATEJİK FİLTRELER ---
            # 1. Ayı Uyumsuzluğu Koruması (Girişi engeller)
            if last['bear_div']: continue
            
            # 2. Hacim Delta Filtresi (Madde 3)
            if last['delta'] < 48: continue
            
            # --- ANAYASA PUANLAMA SİSTEMİ ---
            # RSI Uyumsuzluğu (En yüksek öncelik)
            if last['bull_div']:
                score += 6
                signal_type = "📉 RSI UYUMSUZLUK (BOĞA)"
            
            # SR-Flip Kırılımı
            elif last['sr_break']:
                score += 5
                signal_type = "🧱 SR-FLIP (KIRILIM)"
            
            # Roket Kırılımı
            elif last['v'] > (df15['v'].rolling(20).mean().iloc[-1] * 3.5) and last['c'] > last['upper_band']:
                score += 5
                signal_type = "🚀 ROKET (BOLLINGER)"
            
            # Akıntıya Karşı
            elif btc_change < -0.5 and ((last['c'] - df15['c'].iloc[-4]) / df15['c'].iloc[-4] * 100) > 0:
                score += 4
                signal_type = "⚡ AKINTIYA KARŞI"
                
            # Dip Avcısı (MTF EMA 200 Onaylı)
            elif df1h['c'].iloc[-1] > df1h['ema200'].iloc[-1] and last['rsi'] < 30:
                score += 4
                signal_type = "🛡️ DİP AVCISI"

            # EK PUANLAR
            if whale > 2.5: score += 3 # Balina Desteği
            if last['adx'] > 25: score += 2 # Trend Gücü
            if last['delta'] > 55: score += 2 # Agresif Alıcılar
            if last['obv'] > df15['obv'].iloc[-5]: score += 1 # OBV Artışı
            
            # --- SİNYAL GÖNDERİMİ VE RİSK YÖNETİMİ ---
            if score >= 9:
                # BTC Çok Sert Düşüyorsa Sadece Akıntıya Karşı Girebilir
                if btc_change <= -1.0 and "AKINTIYA" not in signal_type:
                    continue
                
                # Madde 1: Swing Low Stop (Son 20 mumun dibi)
                stop_loss = df15['l'].rolling(window=20).min().iloc[-1] * 0.995
                risk_amount = last['c'] - stop_loss
                
                if risk_amount <= 0: continue
                
                # Madde 5: 1:3 Kâr Oranı
                take_profit = last['c'] + (risk_amount * 3.0)
                
                # Madde 4: Fibonacci Radar Bilgisi
                fib_status = "🟢 YOL AÇIK" if last['c'] > last['fib_618'] else "🔴 DİRENÇ VAR"

                # 4 saatte bir aynı coine sinyal atma
                if s not in sent_signals or (time.time() - sent_signals[s]) > 14400:
                    # PROFESYONEL TELEGRAM KARTI
                    message = (
                        f"🟡 **YENİ SİNYAL TESPİT EDİLDİ**\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"📊 **Strateji:** {signal_type}\n"
                        f"🪙 **Coin:** #{s} / USDT\n"
                        f"📈 **Skor:** {score}/10 | **Balina:** x{whale:.1f}\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"💵 **Giriş:** {last['c']:.8f}\n"
                        f"🛡️ **Stop:** {stop_loss:.8f}\n"
                        f"🎯 **Hedef:** {take_profit:.8f}\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"🔍 **Fibonacci:** {fib_status}\n"
                        f"⚡ **Delta:** %{last['delta']:.1f} | **ADX:** {last['adx']:.1f}\n"
                        f"🌐 **BTC:** %{btc_change:.2f}\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"🔗 [Binance'de İşlem Aç](https://www.binance.com/en/trade/{s}_USDT)"
                    )
                    
                    api_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
                    requests.post(api_url, data={'chat_id': CHAT_ID, 'text': message, 'parse_mode': 'Markdown'})
                    
                    sent_signals[s] = time.time()
                    print(f"✅ SİNYAL GÖNDERİLDİ: {s} | Skor: {score}", flush=True)
            
            # API'yi yormamak için kısa bekleme
            time.sleep(0.05)
            
    except Exception as e:
        print(f"❌ Kritik Hata: {e}", flush=True)
    
    end_time = datetime.now()
    duration = (end_time - start_time).seconds
    print(f"🏁 Tarama Bitti. Süre: {duration}sn. | 60sn Bekleniyor...", flush=True)

# --- ANA DÖNGÜ ---
if __name__ == "__main__":
    while True:
        scan_market()
        time.sleep(60)
