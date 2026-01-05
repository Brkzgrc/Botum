import pandas as pd
import numpy as np
import time
import requests
import urllib3
from flask import Flask
import threading
from datetime import datetime, timedelta

# --- RENDER KONTROL VE WEB SERVER ---
app = Flask('')
@app.route('/')
def home(): return "Pro Bot v3.5 ULTRA - SIFIR TAVİZ"
def run_web(): app.run(host='0.0.0.0', port=8080)
threading.Thread(target=run_web, daemon=True).start()

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- KONFİGÜRASYON ---
TELEGRAM_TOKEN = "7583261338:AAHASreSYIaX-6QAXIUflpyf5HnbQXq81Dg"
CHAT_ID = "5124859166"
EXCLUDED_COINS = ['USDC', 'FDUSD', 'TUSD', 'USDP', 'BUSD', 'DAI', 'EUR', 'TRY', 'GBP', 'PAXG']
sent_signals = {}

# --- 1. MODÜL: TELEGRAM İLETİŞİM ---
def send_telegram_msg(message):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {'chat_id': CHAT_ID, 'text': message, 'parse_mode': 'Markdown', 'disable_web_page_preview': 'True'}
        requests.post(url, data=payload, timeout=15)
    except Exception as e:
        print(f"Telegram Hatası: {e}")

# --- 2. MODÜL: ORDER BOOK (BALİNA DUVARI) ANALİZİ ---
def get_order_book_depth(symbol):
    try:
        # Tahtanın derinliğini (limit 100) çekiyoruz
        url = f"https://api1.binance.com/api/v3/depth?symbol={symbol}&limit=100"
        res = requests.get(url, timeout=5).json()
        
        # Alım ve Satım emirlerinin toplam hacmi (Price * Quantity)
        bid_volume = sum([float(price) * float(qty) for price, qty in res['bids']])
        ask_volume = sum([float(price) * float(qty) for price, qty in res['asks']])
        
        if ask_volume == 0: return 1.0
        return bid_volume / ask_volume
    except:
        return 1.0

# --- 3. MODÜL: OPEN INTEREST (KALDIRAÇLI PARA AKIŞI) ---
def get_open_interest_data(symbol):
    try:
        # Vadeli taraftaki açık pozisyon miktarını çeker
        url = f"https://fapi.binance.com/fapi/v1/openInterest?symbol={symbol}"
        res = requests.get(url, timeout=5).json()
        return float(res['openInterest'])
    except:
        return 0

# --- 4. MODÜL: DETAYLI TEKNİK ANALİZ VE UYUMSUZLUK ---
def perform_full_technical_analysis(symbol):
    try:
        url = f"https://api1.binance.com/api/v3/klines?symbol={symbol}&interval=15m&limit=150"
        res = requests.get(url, timeout=10).json()
        df = pd.DataFrame(res, columns=['ts','o','h','l','c','v','ct','qa','nt','tb','tq','i'])
        df[['c','v','h','l','o']] = df[['c','v','h','l','o']].astype(float)
        
        # RSI HESAPLAMA
        delta = df['c'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        df['rsi'] = 100 - (100 / (1 + (gain / (loss + 0.000001))))
        current_rsi = df['rsi'].iloc[-1]
        
        # POZİTİF RSI UYUMSUZLUĞU (DIVERGENCE)
        # Fiyat yeni dip yaparken RSI yükseliyor mu?
        divergence = "YOK"
        last_5_min_price = df['c'].iloc[-10:].min()
        last_5_min_rsi = df['rsi'].iloc[-10:].min()
        if df['c'].iloc[-1] <= last_5_min_price and df['rsi'].iloc[-1] > last_5_min_rsi:
            divergence = "POZİTİF UYUMSUZLUK 📈"

        # TREND FİLTRESİ (EMA 200)
        ema200 = df['c'].ewm(span=200, adjust=False).mean().iloc[-1]
        trend_status = "BOĞA 🟢" if df['c'].iloc[-1] > ema200 else "AYI 🔴"

        # BOLLINGER BANTLARI
        sma20 = df['c'].rolling(20).mean()
        std20 = df['c'].rolling(20).std()
        lower_band = (sma20 - (std20 * 2.2)).iloc[-1]
        upper_band = (sma20 + (std20 * 2.2)).iloc[-1]

        # HACİM ANALİZİ
        current_volume = df['v'].iloc[-1]
        avg_volume = df['v'].iloc[-21:-1].mean()
        vol_ratio = current_volume / avg_volume if avg_volume > 0 else 1.0

        # DESTEK VE DİRENÇ (PİVOT)
        support_level = df['l'].tail(50).min()
        resistance_level = df['h'].tail(50).max()
        
        price = df['c'].iloc[-1]
        
        return {
            'rsi': current_rsi, 'l_bb': lower_band, 'u_bb': upper_band,
            'price': price, 'vol_ratio': vol_ratio, 'sup': support_level,
            'res': resistance_level, 'div': divergence, 'trend': trend_status, 'ema': ema200
        }
    except Exception as e:
        return None

# --- 5. ANA DÖNGÜ VE SENARYO YÖNETİMİ ---
print("🚀 v3.5 ULTRA BAŞLATILDI - TAM DONANIMLI MOD", flush=True)

while True:
    try:
        # Piyasa genel durum kontrolü (Crash Guard)
        ticker_24h = requests.get("https://api1.binance.com/api/v3/ticker/24hr").json()
        market_crash_count = sum(1 for t in ticker_24h if float(t['priceChangePercent']) < -7)
        
        # Coin listesini tazele
        exchange_info = requests.get("https://api1.binance.com/api/v3/exchangeInfo").json()
        active_symbols = [s['symbol'] for s in exchange_info['symbols'] if s['status'] == 'TRADING' and s['quoteAsset'] == 'USDT' and s['baseAsset'] not in EXCLUDED_COINS]
        
        for symbol in active_symbols:
            analysis = perform_full_technical_analysis(symbol)
            if not analysis: continue
            
            # Değişkenleri açıyoruz
            rsi = analysis['rsi']
            price = analysis['price']
            l_bb = analysis['l_bb']
            vol_ratio = analysis['vol_ratio']
            div = analysis['div']
            trend = analysis['trend']
            sup = analysis['sup']
            res = analysis['res']
            
            # KRİTER 1: TEMEL FİLTRE (Aşırı Satış veya Bant Dışı)
            if rsi < 26 or price <= l_bb:
                
                # Detaylı verileri çek (Balina ve OI)
                whale_ratio = get_order_book_depth(symbol)
                oi_value = get_futures_data = get_open_interest_data(symbol)
                
                # PUANLAMA VE SENARYO BELİRLEME
                score = 5 # Taban puan
                active_scenarios = []

                # 🛡️ DİP AVCISI
                if rsi < 25: 
                    score += 1
                    active_scenarios.append("🛡️ DİP AVCISI")
                
                # 🐋 BALİNA ONAYLI
                if whale_ratio > 1.8: 
                    score += 2
                    active_scenarios.append("🐋 BALİNA ONAYLI")
                
                # 🚀 POZİTİF AYRIŞMA
                if div != "YOK": 
                    score += 2
                    active_scenarios.append("🚀 POZİTİF AYRIŞMA")
                
                # ⚡ İĞNE OPERASYONU (Ani Hacimli Düşüş)
                if vol_ratio > 4.0:
                    score += 1
                    active_scenarios.append("⚡ İĞNE OPERASYONU")

                # Trend Onayı
                if trend == "BOĞA 🟢": score += 1
                
                # --- FİNAL KARAR ---
                if score >= 8:
                    # Zaman Kilidi (6 Saat)
                    if symbol not in sent_signals or (time.time() - sent_signals[symbol]) > 21600:
                        
                        target_price = price * 1.035
                        stop_loss = price * 0.96
                        potential = ((res - price) / price) * 100
                        
                        # Fiyat Hassasiyeti (FLOKI vb. için)
                        p_str = "{:.8f}".format(price).rstrip('0').rstrip('.')
                        t_str = "{:.8f}".format(target_price).rstrip('0').rstrip('.')
                        s_str = "{:.8f}".format(stop_loss).rstrip('0').rstrip('.')

                        scen_text = " / ".join(active_scenarios)
                        
                        msg = (f"*SİNYAL:* {scen_text}\n"
                               f"⭐ *GÜVEN SKORU:* {score}/10\n"
                               f"━━━━━━━━━━━━━━━\n"
                               f"💵 *Giriş Fiyatı:* `{p_str}`\n"
                               f"📊 RSI: {rsi:.1f} | {div}\n"
                               f"📈 Trend: {trend} | BB: ALT BANT\n"
                               f"🐋 Balina Duvarı: x{whale_ratio:.1f}\n"
                               f"🎯 Hedef: `{t_str}` | 🛑 Stop: `{s_str}`\n"
                               f"💎 Kar Potansiyeli: %{potential:.1f}\n"
                               f"━━━━━━━━━━━━━━━\n"
                               f"🔗 [Binance Trade](https://www.binance.com/en/trade/{symbol.replace('USDT','_USDT')})")
                        
                        send_telegram_msg(msg)
                        sent_signals[symbol] = time.time()
            
            time.sleep(0.04) # API Limit koruması
            
        print(f"🏁 Tur Tamamlandı: {datetime.now().strftime('%H:%M:%S')}", flush=True)
        time.sleep(60)

    except Exception as e:
        print(f"Sistem Hatası: {e}")
        time.sleep(10)
