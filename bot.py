import pandas as pd
import time
import requests
import urllib3
import sys
from flask import Flask
import threading
from datetime import datetime, timedelta

# --- RENDER İÇİN WEB SUNUCUSU ---
app = Flask('')
@app.route('/')
def home(): return "Bot Calisiyor!"
def run_web(): app.run(host='0.0.0.0', port=8080)
threading.Thread(target=run_web, daemon=True).start()

# --- BOT AYARLARI ---
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
TELEGRAM_TOKEN = "7583261338:AAHASreSYIaX-6QAXIUflpyf5HnbQXq81Dg"
CHAT_ID = "5124859166"
EXCLUDED = ['USDC', 'FDUSD', 'TUSD', 'USDP', 'BUSD', 'DAI', 'EUR', 'TRY', 'GBP', 'PAXG']

sent_signals = {}
last_report_time = datetime.now()
scanned_count = 0

def send_telegram(message):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, data={'chat_id': CHAT_ID, 'text': message, 'parse_mode': 'Markdown', 'disable_web_page_preview': 'True'}, verify=False, timeout=10)
    except: pass

def get_all_spot_symbols():
    try:
        url = "https://api.binance.com/api/v3/exchangeInfo"
        res = requests.get(url, verify=False, timeout=10).json()
        return [sym['symbol'] for sym in res['symbols'] if sym['status'] == 'TRADING' and sym['quoteAsset'] == 'USDT' and sym['isSpotTradingAllowed'] and 'UP' not in sym['symbol'] and 'DOWN' not in sym['symbol'] and sym['baseAsset'] not in EXCLUDED]
    except: return []

print("🚀 BULUT BOTU v2.1 BAŞLATILDI!", flush=True)
send_telegram("🤖 *Bulut Botu v2.1 Yayında!*")

while True:
    try:
        # 6 SAATLİK RAPOR KONTROLÜ
        if datetime.now() - last_report_time > timedelta(hours=6):
            report_msg = f"📊 *6 Saatlik Sistem Raporu*\n\n🔹 Durum: Aktif\n🔹 Taranan Coin: {scanned_count}"
            send_telegram(report_msg)
            last_report_time = datetime.now()
            scanned_count = 0

        all_coins = get_all_spot_symbols()
        for s in all_coins:
            scanned_count += 1
            url = f"https://api.binance.com/api/v3/klines?symbol={s}&interval=15m&limit=100"
            r = requests.get(url, verify=False, timeout=5).json()
            df = pd.DataFrame(r, columns=['ts', 'o', 'h', 'l', 'c', 'v', 'ct', 'qa', 'nt', 'tb', 'tq', 'i'])
            df[['c', 'h', 'l', 'v']] = df[['c', 'h', 'l', 'v']].astype(float)
            
            delta = df['c'].diff()
            gain = (delta.where(delta > 0, 0)).rolling(14).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
            rsi = 100 - (100 / (1 + (gain / loss.replace(0, 0.001)))).iloc[-1]
            
            vol_avg = df['v'].iloc[-21:-1].mean()
            vol_ratio = df['v'].iloc[-1] / vol_avg
            
            # SİNYAL MANTIĞI
            if rsi < 25 and vol_ratio > 3.5:
                if s not in sent_signals or (time.time() - sent_signals[s]) > 14400:
                    price = df['c'].iloc[-1]
                    send_telegram(f"🛡️ *SİNYAL:* {s} - Fiyat: {price} - RSI: {rsi:.1f}")
                    sent_signals[s] = time.time()
            time.sleep(0.1)
        
        # --- KRİTİK EKLEME: LOGLARA İZ BIRAKMA ---
        print(f"✅ TÖM PİYASA TARANDI: {datetime.now().strftime('%H:%M:%S')} | Taranan Coin: {len(all_coins)}", flush=True)
        time.sleep(60)

    except Exception as e:
        print(f"❌ HATA: {e}", flush=True)
        time.sleep(10)
