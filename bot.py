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

sent_signals = {}
last_report_time = datetime.now()

def send_telegram(message):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, data={'chat_id': CHAT_ID, 'text': message, 'parse_mode': 'Markdown', 'disable_web_page_preview': 'True'}, verify=False, timeout=10)
    except Exception as e: print(f"❌ Telegram Hatası: {e}")

def get_all_spot_symbols():
    try:
        url = "https://api.binance.com/api/v3/exchangeInfo"
        res = requests.get(url, verify=False, timeout=15)
        if res.status_code != 200:
            print(f"⚠️ Binance Bağlantı Sorunu! Kod: {res.status_code}", flush=True)
            return []
        data = res.json()
        symbols = [sym['symbol'] for sym in data['symbols'] if sym['status'] == 'TRADING' and sym['quoteAsset'] == 'USDT' and sym['baseAsset'] not in EXCLUDED]
        print(f"✅ Binance'ten {len(symbols)} adet coin listesi başarıyla çekildi.", flush=True)
        return symbols
    except Exception as e:
        print(f"❌ Liste Çekme Hatası: {e}", flush=True)
        return []

print("🚀 BULUT BOTU v2.2 BAŞLATILDI - ŞEFFAF MOD", flush=True)
send_telegram("🤖 *Bulut Botu v2.2 Yayında!* \nVeri akışı kontrol ediliyor...")

while True:
    try:
        all_coins = get_all_spot_symbols()
        
        if not all_coins:
            print("⏳ Coin listesi alınamadı, 30 saniye sonra tekrar denenecek...", flush=True)
            time.sleep(30)
            continue

        scanned_this_turn = 0
        for s in all_coins:
            try:
                url = f"https://api.binance.com/api/v3/klines?symbol={s}&interval=15m&limit=100"
                r = requests.get(url, verify=False, timeout=5).json()
                if isinstance(r, dict) and 'code' in r: continue # Hatalı sembolü atla
                
                df = pd.DataFrame(r, columns=['ts', 'o', 'h', 'l', 'c', 'v', 'ct', 'qa', 'nt', 'tb', 'tq', 'i'])
                df[['c', 'h', 'l', 'v']] = df[['c', 'h', 'l', 'v']].astype(float)
                
                # RSI Hesapla
                delta = df['c'].diff()
                gain = (delta.where(delta > 0, 0)).rolling(14).mean()
                loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
                rsi = 100 - (100 / (1 + (gain / loss.replace(0, 0.001)))).iloc[-1]
                
                # Hacim Oranı
                vol_ratio = df['v'].iloc[-1] / df['v'].iloc[-21:-1].mean()
                
                if rsi < 25 and vol_ratio > 3.5:
                    if s not in sent_signals or (time.time() - sent_signals[s]) > 14400:
                        send_telegram(f"🛡️ *SİNYAL:* {s}\nRSI: {rsi:.1f} | Hacim: {vol_ratio:.1f}X")
                        sent_signals[s] = time.time()
                
                scanned_this_turn += 1
                time.sleep(0.05) # Binance'i yormamak için kısa bekleme
            except: continue

        print(f"✅ TARAMA TAMAMLANDI: {datetime.now().strftime('%H:%M:%S')} | Toplam: {scanned_this_turn} Coin", flush=True)
        time.sleep(60)

    except Exception as e:
        print(f"💥 Ana Döngü Hatası: {e}", flush=True)
        time.sleep(10)
