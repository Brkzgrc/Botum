import pandas as pd
import time
import requests
import urllib3
import sys
from flask import Flask
import threading

# --- RENDER İÇİN WEB SUNUCUSU ---
app = Flask('')
@app.route('/')
def home(): return "Bot Calisiyor!"
def run_web(): app.run(host='0.0.0.0', port=8080)
threading.Thread(target=run_web, daemon=True).start()

# --- BOT AYARLARI ---
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
API_KEY = "YpqKmrTNC7YQ5yOCKtftEwwP0VXD3z9I8OjL8mltLzSjjqfe78MIm7dty6ZHBD85"
TELEGRAM_TOKEN = "7583261338:AAHASreSYIaX-6QAXIUflpyf5HnbQXq81Dg"
CHAT_ID = "5124859166"
EXCLUDED = ['USDC', 'FDUSD', 'TUSD', 'USDP', 'BUSD', 'DAI', 'EUR', 'TRY', 'GBP', 'PAXG']

sent_signals = {}

def send_telegram(message):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, data={'chat_id': CHAT_ID, 'text': message, 'parse_mode': 'Markdown'}, verify=False, timeout=10)
    except: pass

def get_all_spot_symbols():
    try:
        url = "https://api.binance.com/api/v3/exchangeInfo"
        res = requests.get(url, verify=False, timeout=10).json()
        return [sym['symbol'] for sym in res['symbols'] if sym['status'] == 'TRADING' and sym['quoteAsset'] == 'USDT' and sym['isSpotTradingAllowed'] and 'UP' not in sym['symbol'] and 'DOWN' not in sym['symbol'] and sym['baseAsset'] not in EXCLUDED]
    except: return []

print("🚀 BULUTTA BOT BAŞLATILDI!", flush=True)
send_telegram("🤖 *Bot Bulut Sunucusunda Başlatıldı!* \nArtık iPad'ini kapatabilirsin.")

while True:
    try:
        all_coins = get_all_spot_symbols()
        for s in all_coins:
            url = f"https://api.binance.com/api/v3/klines?symbol={s}&interval=15m&limit=100"
            r = requests.get(url, verify=False, timeout=5).json()
            df = pd.DataFrame(r, columns=['ts', 'o', 'h', 'l', 'c', 'v', 'ct', 'qa', 'nt', 'tb', 'tq', 'i'])
            df[['c', 'v']] = df[['c', 'v']].astype(float)
            
            delta = df['c'].diff()
            gain = (delta.where(delta > 0, 0)).rolling(14).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
            rsi = 100 - (100 / (1 + (gain / loss.replace(0, 0.001)))).iloc[-1]
            vol_spike = df['v'].iloc[-1] > (df['v'].iloc[-21:-1].mean() * 3.5)
            
            if rsi < 25 and vol_spike:
                if s not in sent_signals or (time.time() - sent_signals[s]) > 14400:
                    price = df['c'].iloc[-1]
                    msg = f"🛡️ *YENİ SİNYAL*\n💎 *Coin:* {s}\n💰 *Fiyat:* {price}\n📊 *RSI:* {rsi:.1f}\n📈 *Hacim:* 3.5X"
                    send_telegram(msg)
                    sent_signals[s] = time.time()
            time.sleep(0.1)
        time.sleep(60)
    except Exception as e:
        time.sleep(10)
