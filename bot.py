import pandas as pd
import numpy as np
import time
import requests
import urllib3
from flask import Flask
import threading
from datetime import datetime, timedelta

# --- 1. WEB SUNUCUSU (RENDER İÇİN) ---
app = Flask('')
@app.route('/')
def home(): return "Pro Bot v3.7 - HEAVY SYSTEM ACTIVE"
def run_web(): app.run(host='0.0.0.0', port=8080)
threading.Thread(target=run_web, daemon=True).start()

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- 2. AYARLAR VE PARAMETRELER ---
TELEGRAM_TOKEN = "7583261338:AAHASreSYIaX-6QAXIUflpyf5HnbQXq81Dg"
CHAT_ID = "5124859166"
EXCLUDED = ['USDC', 'FDUSD', 'TUSD', 'USDP', 'BUSD', 'DAI', 'EUR', 'TRY', 'GBP', 'PAXG']
sent_signals = {}

# --- 3. MODÜL: TELEGRAM GÖNDERİMİ ---
def send_telegram_msg(message):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {'chat_id': CHAT_ID, 'text': message, 'parse_mode': 'Markdown', 'disable_web_page_preview': 'True'}
        requests.post(url, data=payload, timeout=15)
    except Exception as e:
        print(f"❌ Telegram Hatası: {e}")

# --- 4. MODÜL: BALİNA DERİNLİK ANALİZİ (ORDER BOOK) ---
def get_order_book_analysis(symbol):
    try:
        url = f"https://api1.binance.com/api/v3/depth?symbol={symbol}&limit=100"
        res = requests.get(url, timeout=5).json()
        
        bids = res.get('bids', [])
        asks = res.get('asks', [])
        
        bid_vol = sum([float(p) * float(q) for p, q in bids])
        ask_vol = sum([float(p) * float(q) for p, q in asks])
        
        if ask_vol == 0: return 1.0
        return bid_vol / ask_vol
    except Exception as e:
        print(f"⚠️ Derinlik Hatası ({symbol}): {e}")
        return 1.0

# --- 5. MODÜL: KALDIRAÇ (OPEN INTEREST) VERİSİ ---
def get_futures_oi(symbol):
    try:
        url = f"https://fapi.binance.com/fapi/v1/openInterest?symbol={symbol}"
        res = requests.get(url, timeout=5).json()
        return float(res.get('openInterest', 0))
    except:
        return 0

# --- 6. MODÜL: RSI UYUMSUZLUK (DIVERGENCE) MOTORU ---
def check_rsi_divergence(df):
    try:
        # Son 10 mumu incele
        current_price = df['c'].iloc[-1]
        prev_min_price = df['c'].iloc[-15:-1].min()
        
        current_rsi = df['rsi'].iloc[-1]
        prev_min_rsi = df['rsi'].iloc[-15:-1].min()
        
        # Fiyat yeni dip yaparken RSI yapmıyorsa
        if current_price <= prev_min_price and current_rsi > prev_min_rsi:
            return "POZİTİF UYUMSUZLUK 📈"
        return "YOK"
    except:
        return "YOK"

# --- 7. MODÜL: ANA TEKNİK ANALİZ MOTORU ---
def get_detailed_analysis(symbol):
    try:
        url = f"https://api1.binance.com/api/v3/klines?symbol={symbol}&interval=15m&limit=150"
        r = requests.get(url, timeout=10).json()
        df = pd.DataFrame(r, columns=['ts','o','h','l','c','v','ct','qa','nt','tb','tq','i'])
        df[['c','v','h','l','o']] = df[['c','v','h','l','o']].astype(float)
        
        # RSI 14
        delta = df['c'].diff()
        up = delta.clip(lower=0)
        down = -1 * delta.clip(upper=0)
        ema_up = up.ewm(com=13, adjust=False).mean()
        ema_down = down.ewm(com=13, adjust=False).mean()
        rs = ema_up / (ema_down + 0.000001)
        df['rsi'] = 100 - (100 / (1 + rs))
        
        # EMA 200 (Trend)
        df['ema200'] = df['c'].ewm(span=200, adjust=False).mean()
        
        # Bollinger Bantları (20, 2.2)
        df['sma20'] = df['c'].rolling(20).mean()
        df['std20'] = df['c'].rolling(20).std()
        df['l_bb'] = df['sma20'] - (df['std20'] * 2.2)
        
        # Hacim Ortalaması
        avg_vol = df['v'].iloc[-21:-1].mean()
        
        # Destek / Direnç
        support = df['l'].tail(50).min()
        resistance = df['h'].tail(50).max()
        
        return {
            'rsi': df['rsi'].iloc[-1],
            'l_bb': df['l_bb'].iloc[-1],
            'ema200': df['ema200'].iloc[-1],
            'price': df['c'].iloc[-1],
            'vol_ratio': df['v'].iloc[-1] / (avg_vol + 0.0001),
            'div': check_rsi_divergence(df),
            'sup': support,
            'res': resistance
        }
    except Exception as e:
        print(f"❌ Analiz Hatası ({symbol}): {e}")
        return None

# --- 8. ANA DÖNGÜ VE MANTIK KATMANI ---
print("🛡️ PRO BOT v3.7 - SIFIR TAVİZ MODU AKTİF", flush=True)

while True:
    try:
        # Binance Coin Listesi
        resp = requests.get("https://api1.binance.com/api/v3/exchangeInfo").json()
        symbols = [s['symbol'] for s in resp['symbols'] if s['status'] == 'TRADING' and s['quoteAsset'] == 'USDT' and s['baseAsset'] not in EXCLUDED]
        
        print(f"🔄 {datetime.now().strftime('%H:%M:%S')} | {len(symbols)} Coin taranıyor...", flush=True)

        for s in symbols:
            # CANLI LOG: Hangi coin taranıyor gör
            print(f"🔍 İnceleniyor: {s}", flush=True)
            
            ana = get_detailed_analysis(s)
            if not ana: continue
            
            # --- PROFESYONEL FİLTRELEME VE PUANLAMA ---
            # Giriş Şartı: RSI < 26 VEYA Bollinger Alt Bant İhlali
            if ana['rsi'] < 26 or ana['price'] <= ana['l_bb']:
                
                # Ekstra Balina ve OI Verisi
                whale = get_order_book_analysis(s)
                oi = get_futures_oi(s)
                
                score = 5 # Başlangıç
                scenarios = []

                # Senaryo 1: DİP AVCISI
                if ana['rsi'] < 24:
                    score += 1
                    scenarios.append("🛡️ DİP AVCISI")
                
                # Senaryo 2: BALİNA ONAYLI
                if whale > 1.9:
                    score += 2
                    scenarios.append("🐋 BALİNA ONAYLI")
                
                # Senaryo 3: POZİTİF AYRIŞMA
                if ana['div'] != "YOK":
                    score += 2
                    scenarios.append("🚀 POZİTİF AYRIŞMA")
                
                # Senaryo 4: İĞNE OPERASYONU
                if ana['vol_ratio'] > 4.2:
                    score += 1
                    scenarios.append("⚡ İĞNE OPERASYONU")
                
                # Trend Filtresi (Boğa ise +1)
                if ana['price'] > ana['ema200']:
                    score += 1

                # KRİTİK EŞİK: SKOR 8 VE ÜSTÜ
                if score >= 8:
                    if s not in sent_signals or (time.time() - sent_signals[s]) > 21600:
                        
                        target = ana['price'] * 1.035
                        stop = ana['price'] * 0.962
                        pot = ((ana['res'] - ana['price']) / ana['price']) * 100
                        
                        # Fiyat Hassasiyeti (Sıfırları Atma)
                        p_f = "{:.8f}".format(ana['price']).rstrip('0').rstrip('.')
                        t_f = "{:.8f}".format(target).rstrip('0').rstrip('.')
                        s_f = "{:.8f}".format(stop).rstrip('0').rstrip('.')

                        msg = (f"*{' / '.join(scenarios)}*: #{s}\n"
                               f"⭐ SKOR: {score}/10\n"
                               f"━━━━━━━━━━━━━━━\n"
                               f"💵 *Giriş:* `{p_f}`\n"
                               f"📊 RSI: {ana['rsi']:.1f} | {ana['div']}\n"
                               f"🐋 Balina: x{whale:.1f} | Hacim: {ana['vol_ratio']:.1f}x\n"
                               f"📈 Trend: {'BOĞA 🟢' if ana['price'] > ana['ema200'] else 'AYI 🔴'}\n"
                               f"🎯 Hedef: `{t_f}` | 🛑 Stop: `{s_f}`\n"
                               f"💎 Kar Potansiyeli: %{pot:.1f}\n"
                               f"━━━━━━━━━━━━━━━\n"
                               f"🔗 [Binance](https://www.binance.com/en/trade/{s.replace('USDT','_USDT')})")
                        
                        send_telegram_msg(msg)
                        sent_signals[s] = time.time()
                else:
                    # Sinyal atılmayan ama incelenen detayları logda göster
                    print(f"   ⚠️ {s} skor düşük kaldı: {score}/10", flush=True)

            time.sleep(0.05) # API Koruması

        print(f"🏁 Tur Tamamlandı. Beklemeye geçiliyor...", flush=True)
        time.sleep(60)

    except Exception as e:
        print(f"💥 ANA DÖNGÜ HATASI: {e}", flush=True)
        time.sleep(10)
a
