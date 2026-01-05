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
def home(): return "Pro Bot v3.1 Final Aktif"
def run_web(): app.run(host='0.0.0.0', port=8080)
threading.Thread(target=run_web, daemon=True).start()

# --- AYARLAR ---
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
TELEGRAM_TOKEN = "7583261338:AAHASreSYIaX-6QAXIUflpyf5HnbQXq81Dg"
CHAT_ID = "5124859166"
EXCLUDED = ['USDC', 'FDUSD', 'TUSD', 'USDP', 'BUSD', 'DAI', 'EUR', 'TRY', 'GBP', 'PAXG']

sent_signals = {}
last_report_time = datetime.now()

def send_telegram(message):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, data={'chat_id': CHAT_ID, 'text': message, 'parse_mode': 'Markdown', 'disable_web_page_preview': 'True'}, timeout=10)
    except: pass

# 1. BALİNA VE DERİNLİK ANALİZİ
def get_order_book_status(symbol):
    try:
        url = f"https://api1.binance.com/api/v3/depth?symbol={symbol}&limit=50"
        res = requests.get(url, timeout=5).json()
        bids = sum([float(p)*float(q) for p, q in res['bids']]) 
        asks = sum([float(p)*float(q) for p, q in res['asks']])
        # Alım emirleri satışın 1.5 katından fazlaysa destek güçlüdür
        return "GÜÇLÜ ✅" if bids > asks * 1.5 else "ZAYIF ⚠️"
    except: return "BİLİNMİYOR"

# 2. KALDIRAÇ (OI) ANALİZİ
def get_oi_trend(symbol):
    try:
        url = f"https://fapi.binance.com/fapi/v1/openInterest?symbol={symbol}"
        res = requests.get(url, timeout=5).json()
        return float(res['openInterest'])
    except: return 0

# 3. TEKNİK ANALİZ MOTORU
def analyze_market(symbol):
    try:
        url = f"https://api1.binance.com/api/v3/klines?symbol={symbol}&interval=15m&limit=100"
        r = requests.get(url, timeout=5).json()
        df = pd.DataFrame(r, columns=['ts','o','h','l','c','v','ct','qa','nt','tb','tq','i'])
        df[['c','v','h','l']] = df[['c','v','h','l']].astype(float)
        
        # RSI
        delta = df['c'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        rsi = 100 - (100 / (1 + (gain / (loss + 0.0001)))).iloc[-1]
        
        # Bollinger Bantları
        sma = df['c'].rolling(20).mean()
        std = df['c'].rolling(20).std()
        lower_bb = (sma - (std * 2)).iloc[-1]
        
        # Hacim Oranı
        vol_ratio = df['v'].iloc[-1] / df['v'].iloc[-21:-1].mean()
        
        price = df['c'].iloc[-1]
        # Destek/Direnç (Son 50 mum)
        sup = df['l'].tail(50).min()
        res_lv = df['h'].tail(50).max()
        
        return rsi, lower_bb, price, vol_ratio, sup, res_lv
    except: return None

print("🚀 PRO BOT v3.1 FINAL - ANALİZ BAŞLADI", flush=True)

while True:
    try:
        # Pazarın %80'i düşüyorsa Crash Guard devreye girer
        resp = requests.get("https://api1.binance.com/api/v3/ticker/24hr").json()
        market_condition = sum(1 for ticker in resp if float(ticker['priceChangePercent']) < -5)
        
        # Coin Listesi
        all_res = requests.get("https://api1.binance.com/api/v3/exchangeInfo").json()
        symbols = [s['symbol'] for s in all_res['symbols'] if s['status'] == 'TRADING' and s['quoteAsset'] == 'USDT' and s['baseAsset'] not in EXCLUDED]
        
        print(f"🔄 {datetime.now().strftime('%H:%M')} | {len(symbols)} Coin Taranıyor...", flush=True)

        for s in symbols:
            analysis = analyze_market(s)
            if not analysis: continue
            
            rsi, l_bb, price, vol_ratio, sup, res_lv = analysis
            
            # --- PROFESYONEL FİLTRELEME ---
            if rsi < 27 and price <= l_bb * 1.01: # RSI düşük ve Bollinger dibinde
                score = 5
                depth = get_order_book_status(s)
                
                if depth == "GÜÇLÜ ✅": score += 2
                if vol_ratio > 3: score += 2
                if price > sup: score += 1 # Destek üstünde tutunma

                # Sadece 7 ve üzeri kaliteli sinyalleri at
                if score >= 7:
                    if s not in sent_signals or (time.time() - sent_signals[s]) > 14400:
                        # TP/SL Hesaplama
                        target = price * 1.03
                        stop = price * 0.96
                        pot = ((res_lv - price) / price) * 100
                        
                        msg = (f"🛡️ *SİNYAL:* #{s}\n"
                               f"⭐ *GÜVEN SKORU:* {score}/10\n"
                               f"━━━━━━━━━━━━━━━\n"
                               f"📊 RSI: {rsi:.1f} | Hacim: {vol_ratio:.1f}x\n"
                               f"🐋 Balina Desteği: {depth}\n"
                               f"🎯 Hedef (%3): `{target:.4f}`\n"
                               f"🛑 Stop (%4): `{stop:.4f}`\n"
                               f"📈 Dirence Uzaklık: %{pot:.1f}\n"
                               f"━━━━━━━━━━━━━━━\n"
                               f"🔗 [Binance'de İşlem Yap](https://www.binance.com/en/trade/{s.replace('USDT','_USDT')})")
                        
                        send_telegram(msg)
                        sent_signals[s] = time.time()
            
            time.sleep(0.04) # Saniyede 25 istek (Limit koruması)

        # 6 SAATLİK RAPOR
        if datetime.now() - last_report_time > timedelta(hours=6):
            crash_status = "RİSKLİ ⚠️" if market_condition > 50 else "NORMAL ✅"
            send_telegram(f"📊 *Sistem Raporu*\nDurum: Aktif\nPiyasa Riski: {crash_status}\nTarama Turu Tamamlandı.")
            last_report_time = datetime.now()

        print(f"🏁 Tur Bitti. {datetime.now().strftime('%H:%M:%S')}", flush=True)
        time.sleep(60)

    except Exception as e:
        print(f"💥 Hata: {e}", flush=True)
        time.sleep(10)
