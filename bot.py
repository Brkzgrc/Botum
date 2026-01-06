import pandas as pd
import numpy as np
import time
import requests
import threading
from datetime import datetime
from flask import Flask

# --- 1. SUNUCU AYARI (RENDER İÇİN) ---
app = Flask('')
@app.route('/')
def home(): return "Sistem Aktif"
def run_web(): app.run(host='0.0.0.0', port=8080)
threading.Thread(target=run_web, daemon=True).start()

# --- 2. AYARLAR ---
TELEGRAM_TOKEN = "7583261338:AAHASreSYIaX-6QAXIUflpyf5HnbQXq81Dg"
CHAT_ID = "5124859166"
EXCLUDED = ['USDC', 'FDUSD', 'TUSD', 'USDP', 'BUSD', 'DAI', 'EUR', 'TRY', 'PAXG']
sent_signals = {}

# --- 3. TEKNİK ANALİZ MOTORU (FULL ENTEGRASYON) ---
def get_data(symbol, interval):
    try:
        url = f"https://api1.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit=200"
        r = requests.get(url, timeout=10).json()
        df = pd.DataFrame(r, columns=['ts','o','h','l','c','v','ct','qa','nt','tb','tq','i'])
        df[['c','v','h','l','o', 'tb']] = df[['c','v','h','l','o', 'tb']].astype(float)
        
        # OBV ve Hacim Delta (Filtreleme Gücü)
        df['obv'] = (np.sign(df['c'].diff()) * df['v']).fillna(0).cumsum()
        df['delta'] = (df['tb'] / df['v']) * 100
        
        # RSI ve EMA 200 (Trend)
        delta = df['c'].diff(); gain = (delta.where(delta > 0, 0)).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        df['rsi'] = 100 - (100 / (1 + (gain / (loss + 1e-6))))
        df['ema200'] = df['c'].ewm(span=200, adjust=False).mean()
        
        # ADX (Tam Matematiksel ATR Bazlı)
        plus_dm = df['h'].diff(); minus_dm = df['l'].diff()
        tr = pd.concat([df['h']-df['l'], abs(df['h']-df['c'].shift()), abs(df['l']-df['c'].shift())], axis=1).max(axis=1)
        atr = tr.rolling(14).mean()
        p_dm = (plus_dm.rolling(14).mean() / (atr + 1e-6)) * 100
        m_dm = (minus_dm.rolling(14).mean() / (atr + 1e-6)) * 100
        df['adx'] = (abs((p_dm - m_dm) / (p_dm + m_dm + 1e-6)) * 100).rolling(14).mean()
        
        # --- UYUMSUZLUK (BOĞA & AYI) ---
        df['low_min_5'] = df['l'].shift(1).rolling(5).min()
        df['high_max_5'] = df['h'].shift(1).rolling(5).max()
        df['rsi_min_5'] = df['rsi'].shift(1).rolling(5).min()
        df['rsi_max_5'] = df['rsi'].shift(1).rolling(5).max()
        # Pozitif Uyumsuzluk (Boğa)
        df['bull_div'] = (df['l'] < df['low_min_5']) & (df['rsi'] > df['rsi_min_5']) & (df['rsi'] < 35)
        # Negatif Uyumsuzluk (Ayı - Koruma Filtresi)
        df['bear_div'] = (df['h'] > df['high_max_5']) & (df['rsi'] < df['rsi_max_5']) & (df['rsi'] > 65)
        
        # SR-Flip ve Fibonacci
        df['recent_high'] = df['h'].rolling(30).max().shift(1)
        df['sr_break'] = (df['c'] > df['recent_high']) & (df['v'] > df['v'].rolling(20).mean() * 1.5)
        h_max = df['h'].max(); l_min = df['l'].min()
        df['fib_618'] = h_max - (0.618 * (h_max - l_min))
        df['upper'] = df['c'].rolling(20).mean() + (2.2 * df['c'].rolling(20).std())
        
        return df
    except: return None

def get_whale_ratio(symbol):
    try:
        res = requests.get(f"https://api1.binance.com/api/v3/depth?symbol={symbol}&limit=100", timeout=5).json()
        bids = sum([float(p) * float(q) for p, q in res.get('bids', [])])
        asks = sum([float(p) * float(q) for p, q in res.get('asks', [])])
        return bids / asks if asks > 0 else 1.0
    except: return 1.0

# --- 4. TARAMA VE PROFESYONEL KARAR ---
def scan():
    print(f"\n🔄 {datetime.now().strftime('%H:%M:%S')} | PİYASA TARAMASI...", flush=True)
    df_btc = get_data("BTCUSDT", "15m")
    if df_btc is None: return
    btc_change = ((df_btc['c'].iloc[-1] - df_btc['c'].iloc[-4]) / df_btc['c'].iloc[-4]) * 100
    
    try:
        symbols = [s['symbol'] for s in requests.get("https://api1.binance.com/api/v3/exchangeInfo").json()['symbols'] if s['status'] == 'TRADING' and s['quoteAsset'] == 'USDT' and s['baseAsset'] not in EXCLUDED]
        
        for s in symbols:
            df15 = get_data(s, "15m"); df1h = get_data(s, "1h")
            if df15 is None or df1h is None: continue
            
            last = df15.iloc[-1]; whale = get_whale_ratio(s); score = 0; signal_type = ""
            
            # --- STRATEJİK FİLTRELER ---
            # 1. Ayı Uyumsuzluğu Varsa Alım Yapma (Koruma)
            if last['bear_div']: continue
            # 2. Delta Filtresi (Satıcı Baskısı Varsa Girme)
            if last['delta'] < 48: continue
            
            # --- PUANLAMA ---
            if last['bull_div']: score += 6; signal_type = "📉 RSI UYUMSUZLUK (BOĞA)"
            elif last['sr_break']: score += 5; signal_type = "🧱 SR-FLIP (KIRILIM)"
            elif last['v'] > (df15['v'].rolling(20).mean().iloc[-1] * 3.5) and last['c'] > last['upper']: score += 5; signal_type = "🚀 ROKET"
            elif btc_change < -0.5 and ((last['c'] - df15['c'].iloc[-4]) / df15['c'].iloc[-4] * 100) > 0: score += 4; signal_type = "⚡ AKINTIYA KARŞI"
            elif df1h['c'].iloc[-1] > df1h['ema200'].iloc[-1] and last['rsi'] < 30: score += 4; signal_type = "🛡️ DİP AVCISI"

            if whale > 2.5: score += 3
            if last['adx'] > 25: score += 2
            if last['delta'] > 55: score += 2
            if last['obv'] > df15['obv'].iloc[-5]: score += 1
            
            if score >= 9:
                if btc_change <= -1.0 and "AKINTIYA" not in signal_type: continue
                
                # Risk Yönetimi
                stop_l = df15['l'].rolling(20).min().iloc[-1] * 0.995
                risk = last['c'] - stop_l
                if risk <= 0: continue
                t_profit = last['c'] + (risk * 3.0)
                fib_inf = "🔥 YOL AÇIK" if last['c'] > last['fib_618'] else "⚠️ DİRENÇ YAKIN"

                if s not in sent_signals or (time.time() - sent_signals[s]) > 14400:
                    # PROFESYONEL TELEGRAM KARTI
                    msg = (
                        f"🟡 **YENİ SİNYAL TESPİT EDİLDİ**\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"📊 **Strateji:** {signal_type}\n"
                        f"🪙 **Coin:** #{s} / USDT\n"
                        f"📈 **Skor:** {score}/10 | **Balina:** x{whale:.1f}\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"💵 **Giriş:** {last['c']:.8f}\n"
                        f"🛡️ **Stop:** {stop_l:.8f}\n"
                        f"🎯 **Hedef:** {t_profit:.8f}\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"🔍 **Fibonacci:** {fib_inf}\n"
                        f"⚡ **Delta:** %{last['delta']:.1f} | **ADX:** {last['adx']:.1f}\n"
                        f"🌐 **BTC:** %{btc_change:.2f}\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"🔗 [Binance'de İşlem Aç](https://www.binance.com/en/trade/{s}_USDT)"
                    )
                    requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", data={'chat_id': CHAT_ID, 'text': msg, 'parse_mode': 'Markdown'})
                    sent_signals[s] = time.time()
                    print(f"✅ Sinyal: {s}", flush=True)
            time.sleep(0.05)
    except Exception as e: print(f"❌ Hata: {e}", flush=True)
    print(f"🏁 Beklemede... Next Run: 60s", flush=True)

while True:
    scan(); time.sleep(60)
