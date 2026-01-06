import pandas as pd
import numpy as np
import time
import requests
import urllib3
from flask import Flask
import threading
from datetime import datetime

# --- 1. WEB SUNUCUSU (RENDER UYUMU) ---
app = Flask('')
@app.route('/')
def home(): return "Pro Bot v3.9 - FULL ARCHITECTURE ACTIVE"
def run_web(): app.run(host='0.0.0.0', port=8080)
threading.Thread(target=run_web, daemon=True).start()

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- 2. AYARLAR ---
TELEGRAM_TOKEN = "7583261338:AAHASreSYIaX-6QAXIUflpyf5HnbQXq81Dg"
CHAT_ID = "5124859166"
EXCLUDED = ['USDC', 'FDUSD', 'TUSD', 'USDP', 'BUSD', 'DAI', 'EUR', 'TRY', 'GBP', 'PAXG']
sent_signals = {}

# --- 3. MODÜL: TELEGRAM ---
def send_telegram_msg(message):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {'chat_id': CHAT_ID, 'text': message, 'parse_mode': 'Markdown', 'disable_web_page_preview': 'True'}
        requests.post(url, data=payload, timeout=15)
    except: pass

# --- 4. MODÜL: BALİNA DERİNLİK ANALİZİ ---
def get_order_book_depth(symbol):
    try:
        url = f"https://api1.binance.com/api/v3/depth?symbol={symbol}&limit=100"
        res = requests.get(url, timeout=5).json()
        bid_vol = sum([float(p) * float(q) for p, q in res.get('bids', [])])
        ask_vol = sum([float(p) * float(q) for p, q in res.get('asks', [])])
        return bid_vol / ask_vol if ask_vol > 0 else 1.0
    except: return 1.0

# --- 5. MODÜL: VADELİ İŞLEMLER (OPEN INTEREST) ---
def get_futures_oi(symbol):
    try:
        url = f"https://fapi.binance.com/fapi/v1/openInterest?symbol={symbol}"
        res = requests.get(url, timeout=5).json()
        return float(res.get('openInterest', 0))
    except: return 0

# --- 6. MODÜL: UYUMSUZLUK (DIVERGENCE) MOTORU ---
def check_divergence(df):
    try:
        curr_p, prev_min_p = df['c'].iloc[-1], df['c'].iloc[-15:-1].min()
        curr_r, prev_min_r = df['rsi'].iloc[-1], df['rsi'].iloc[-15:-1].min()
        if curr_p <= prev_min_p and curr_r > prev_min_r: return "POZİTİF 📈"
        return "YOK"
    except: return "YOK"

# --- 7. MODÜL: ANA ANALİZ MOTORU ---
def get_comprehensive_analysis(symbol):
    try:
        url = f"https://api1.binance.com/api/v3/klines?symbol={symbol}&interval=15m&limit=150"
        res = requests.get(url, timeout=10).json()
        df = pd.DataFrame(res, columns=['ts','o','h','l','c','v','ct','qa','nt','tb','tq','i'])
        df[['c','v','h','l','o']] = df[['c','v','h','l','o']].astype(float)
        
        # RSI 14
        delta = df['c'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        df['rsi'] = 100 - (100 / (1 + (gain / (loss + 0.000001))))
        
        # EMA, Bollinger ve Seviyeler
        df['ema200'] = df['c'].ewm(span=200, adjust=False).mean()
        df['sma20'] = df['c'].rolling(20).mean()
        df['std20'] = df['c'].rolling(20).std()
        df['l_bb'] = df['sma20'] - (df['std20'] * 2.2)
        
        vol_avg = df['v'].iloc[-21:-1].mean()
        high_50 = df['h'].iloc[-50:-1].max() # Son 50 mumun zirvesi
        
        return {
            'price': df['c'].iloc[-1], 'rsi': df['rsi'].iloc[-1], 'l_bb': df['l_bb'].iloc[-1],
            'ema200': df['ema200'].iloc[-1], 'vol_ratio': df['v'].iloc[-1] / (vol_avg + 0.0001),
            'div': check_divergence(df), 'high_50': high_50, 'res': df['h'].tail(60).max()
        }
    except: return None

# --- 8. ANA RADAR DÖNGÜSÜ ---
print("🛡️ PRO BOT v3.9 - TAM KAPSAMLI RADAR AKTİF", flush=True)

while True:
    try:
        info = requests.get("https://api1.binance.com/api/v3/exchangeInfo").json()
        symbols = [s['symbol'] for s in info['symbols'] if s['status'] == 'TRADING' and s['quoteAsset'] == 'USDT' and s['baseAsset'] not in EXCLUDED]
        
        print(f"🔄 {datetime.now().strftime('%H:%M')} | {len(symbols)} Coin taranıyor...", flush=True)

        for symbol in symbols:
            print(f"🔍 Analiz: {symbol}", end='\r', flush=True)
            data = get_comprehensive_analysis(symbol)
            if not data: continue
            
            score = 0
            active_scenarios = []

            # --- SENARYO 1: 🛡️ DİP AVCISI ---
            if data['rsi'] < 26 or data['price'] <= data['l_bb']:
                score += 5
                active_scenarios.append("🛡️ DİP AVCISI")

            # --- SENARYO 2: 🚀 POZİTİF AYRIŞMA ---
            if data['div'] == "POZİTİF 📈":
                score += 6
                active_scenarios.append("🚀 POZİTİF AYRIŞMA")

            # --- SENARYO 3: ⚡ ROKET (KIRILIM) ---
            if data['price'] > data['high_50'] and data['vol_ratio'] > 3.5:
                score += 7
                active_scenarios.append("⚡ ROKET (KIRILIM)")

            # --- EK PUANLAMA KRİTERLERİ ---
            whale = get_order_book_depth(symbol)
            if whale > 2.0: score += 2
            if data['vol_ratio'] > 4.5: score += 1
            if data['price'] > data['ema200']: score += 1
            if data['rsi'] < 20: score += 1

            # SİNYAL BARAJI (8/10)
            if score >= 8:
                if symbol not in sent_signals or (time.time() - sent_signals[symbol]) > 14400:
                    
                    target, stop = data['price'] * 1.04, data['price'] * 0.96
                    p_f = "{:.8f}".format(data['price']).rstrip('0').rstrip('.')
                    t_f = "{:.8f}".format(target).rstrip('0').rstrip('.')
                    s_f = "{:.8f}".format(stop).rstrip('0').rstrip('.')
                    
                    msg = (f"*{' / '.join(active_scenarios)}*: #{symbol}\n"
                           f"⭐ SKOR: {score}/10\n"
                           f"━━━━━━━━━━━━━━━\n"
                           f"💵 Giriş: `{p_f}`\n"
                           f"📊 RSI: {data['rsi']:.1f} | Uyumsuzluk: {data['div']}\n"
                           f"🐋 Balina: x{whale:.1f} | Hacim: {data['vol_ratio']:.1f}x\n"
                           f"🎯 Hedef: `{t_f}` | 🛑 Stop: `{s_f}`\n"
                           f"━━━━━━━━━━━━━━━\n"
                           f"🔗 [Binance](https://www.binance.com/en/trade/{symbol.replace('USDT','_USDT')})")
                    
                    send_telegram_msg(msg)
                    sent_signals[symbol] = time.time()
            
            time.sleep(0.04)
        time.sleep(60)
    except: time.sleep(10)
