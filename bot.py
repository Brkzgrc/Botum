import pandas as pd
import time
import requests
import urllib3
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
ENDPOINTS = ["https://api1.binance.com", "https://api2.binance.com", "https://api3.binance.com"]

sent_signals = {}
last_report_time = datetime.now()

def send_telegram(message):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, data={'chat_id': CHAT_ID, 'text': message, 'parse_mode': 'Markdown', 'disable_web_page_preview': 'True'}, verify=False, timeout=10)
    except: pass

def get_working_endpoint():
    for base in ENDPOINTS:
        try:
            res = requests.get(f"{base}/api/v3/ping", timeout=5)
            if res.status_code == 200: return base
        except: continue
    return "https://api1.binance.com"

def get_all_spot_symbols(base_url):
    try:
        url = f"{base_url}/api/v3/exchangeInfo"
        res = requests.get(url, verify=False, timeout=15)
        if res.status_code != 200: return []
        data = res.json()
        return [sym['symbol'] for sym in data['symbols'] if sym['status'] == 'TRADING' and sym['quoteAsset'] == 'USDT' and sym['baseAsset'] not in EXCLUDED]
    except: return []

print("🚀 BULUT BOTU v2.4 - SESSİZ MOD AKTİF", flush=True)

while True:
    try:
        current_base = get_working_endpoint()
        all_coins = get_all_spot_symbols(current_base)
        
        if not all_coins:
            print(f"⚠️ {datetime.now().strftime('%H:%M:%S')} - Liste alınamadı, bekleniyor...", flush=True)
            time.sleep(30)
            continue

        scanned_count = 0
        for s in all_coins:
            try:
                url = f"{current_base}/api/v3/klines?symbol={s}&interval=15m&limit=100"
                r = requests.get(url, verify=False, timeout=5).json()
                df = pd.DataFrame(r, columns=['ts', 'o', 'h', 'l', 'c', 'v', 'ct', 'qa', 'nt', 'tb', 'tq', 'i'])
                df[['c', 'v']] = df[['c', 'v']].astype(float)
                
                delta = df['c'].diff()
                gain = (delta.where(delta > 0, 0)).rolling(14).mean()
                loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
                rsi = 100 - (100 / (1 + (gain / loss.replace(0, 0.001)))).iloc[-1]
                vol_ratio = df['v'].iloc[-1] / df['v'].iloc[-21:-1].mean()
                
                if rsi < 25 and vol_ratio > 3.5:
                    if s not in sent_signals or (time.time() - sent_signals[s]) > 14400:
                        binance_link = f"https://www.binance.com/en/trade/{s.replace('USDT', '_USDT')}"
                        send_telegram(f"🛡️ *SİNYAL:* {s}\n📊 RSI: {rsi:.1f}\n📈 Hacim: {vol_ratio:.1f}X\n🔗 [Binance]({binance_link})")
                        sent_signals[s] = time.time()
                scanned_count += 1
                time.sleep(0.05)
            except: continue

        # Sadece Render çıktısında görünür, Telegram'a gitmez
        print(f"✅ DÖNGÜ TAMAM: {datetime.now().strftime('%H:%M:%S')} | {scanned_count} Coin tarandı.", flush=True)
        
        # 6 SAATLİK RAPOR (Sadece bu Telegram'a gider)
        if datetime.now() - last_report_time > timedelta(hours=6):
            send_telegram(f"📊 *6 Saatlik Sistem Raporu*\nBot aktif, son turda {scanned_count} coin tarandı.")
            last_report_time = datetime.now()

        time.sleep(60)
    except Exception as e:
        print(f"💥 Hata: {e}", flush=True)
        time.sleep(10)
