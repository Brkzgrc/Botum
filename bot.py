import ccxt
import pandas as pd
import pandas_ta as ta
import time
import os
import requests
import threading
from flask import Flask
from datetime import datetime, timedelta
import sys
import gc 
from apscheduler.schedulers.background import BackgroundScheduler

# Loglar anında aksın
sys.stdout.reconfigure(line_buffering=True)

# --- 1. AYARLAR ---
API_KEY = os.getenv('BINANCE_API_KEY')
API_SECRET = os.getenv('BINANCE_SECRET_KEY')
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

# Taranmayacaklar
IGNORED_COINS = [
    'UP/USDT', 'DOWN/USDT', 'BEAR/USDT', 'BULL/USDT',
    'USDC/USDT', 'TUSD/USDT', 'FDUSD/USDT', 'DAI/USDT', 'USDP/USDT',
    'EUR/USDT', 'TRY/USDT', 'GBP/USDT', 'BUSD/USDT', 'USTC/USDT',
    'PAXG/USDT', 'WBTC/USDT', 'USDE/USDT', 'BRL/USDT', 'RUB/USDT',
    'AUD/USDT', 'UST/USDT', 'USD/USDT', 'XUSD/USDT', 'USD1/USDT',
]

BTC_SYMBOL = 'BTC/USDT'

# --- 2. BORSA BAĞLANTISI ---
exchange = ccxt.binance({
    'apiKey': API_KEY,
    'secret': API_SECRET,
    'options': {'defaultType': 'spot'},
    'enableRateLimit': True,
    'timeout': 30000
})

app = Flask(__name__)

@app.route('/')
def home():
    return "🚀 Price Action Botu (SFP Modu) Aktif!"

# --- 3. VERİ ÇEKME VE HAZIRLIK ---

def get_tradable_symbols():
    try:
        exchange.load_markets()
        symbols = []
        for symbol in exchange.markets:
            if symbol.endswith('/USDT') and exchange.markets[symbol]['active']:
                if not any(ignored in symbol for ignored in IGNORED_COINS):
                    symbols.append(symbol)
        print(f"✅ PA Taraması için {len(symbols)} coin hazır.")
        return symbols
    except Exception as e:
        print(f"Liste hatası: {e}")
        return []

def get_data(symbol, timeframe, limit=100):
    try:
        time.sleep(0.2) # Hızlı tarama için minik bekleme
        bars = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        df = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df.set_index('timestamp', inplace=True)
        return df
    except Exception as e:
        return None

# --- 4. PRICE ACTION MOTORU (BEYİN) ---

def detect_price_action(symbol, df_1h, df_15m):
    """
    Bu fonksiyon grafikteki GEOMETRİK hareketleri ve TUZAKLARI (SFP) arar.
    İndikatörlere değil, FİYATIN KENDİSİNE bakar.
    """
    try:
        # Son mum ve önceki mumlar
        last_15m = df_15m.iloc[-1]
        
        # 1. MARKET YAPISI (SWING LOW) BELİRLEME
        # Geriye dönük son 30 mumun (mevcut hariç) en düşüğünü bul.
        # Bu nokta "LİKİDİTE HAVUZU"dur. Stopların olduğu yerdir.
        lookback_window = 30
        past_candles = df_15m.iloc[-(lookback_window+1):-1] # Son mum hariç önceki 30
        swing_low = past_candles['low'].min()
        
        # 2. SFP (SWING FAILURE PATTERN) TESPİTİ - TUZAK
        # KURAL: Fiyat Swing Low'un altına İNMELİ (Likidite almalı)
        # AMA: Kapanışı tekrar Swing Low'un ÜSTÜNDE yapmalı.
        
        swept_liquidity = last_15m['low'] < swing_low  # İğne attı mı?
        reclaimed_level = last_15m['close'] > swing_low # Geri topladı mı?
        
        if not (swept_liquidity and reclaimed_level):
            return False, None # SFP Yoksa işlem yok

        # 3. HACİM VE GÜÇ TEYİDİ (Order Flow Proxy)
        # İğne atıldığı mumda hacim var mı?
        df_15m['vol_ma'] = ta.sma(df_15m['volume'], length=20)
        last_vol = df_15m.iloc[-1]['volume']
        avg_vol = df_15m.iloc[-1]['vol_ma']
        
        # Hacim ortalamanın üzerinde olmalı (Fake hareket olmasın)
        if last_vol < avg_vol:
            return False, None

        # 4. MARKET KIRILIMI (MSB) İÇİN ERKEN SİNYAL
        # Mum yeşil kapatmalı (Alıcılar günü kazandı)
        if last_15m['close'] < last_15m['open']:
            return False, None

        # SİNYAL ONAYLANDI: Bilgileri Paketle
        stop_distance = ((last_15m['close'] - last_15m['low']) / last_15m['close']) * 100
        
        data = {
            'price': last_15m['close'],
            'stop': last_15m['low'], # Stop noktası iğnenin ucu
            'swing_low': swing_low,
            'risk': stop_distance,
            'vol_mult': last_vol / avg_vol
        }
        
        return True, data

    except Exception as e:
        print(f"Analiz hatası {symbol}: {e}")
        return False, None

# --- 5. ANA DÖNGÜ ---

def run_analysis():
    tr_time = datetime.now() + timedelta(hours=3)
    print(f"\n🔎 [PRICE ACTION TARAMASI] Saat: {tr_time.strftime('%H:%M')}")
    
    symbols = get_tradable_symbols()
    
    # BTC Kontrolü: 4 Saatlikte 200 HO altındaysa long risklidir.
    # Bu basit bir "Piyasa Çöküyor mu?" kontrolüdür.
    btc_df = get_data(BTC_SYMBOL, '4h', limit=200)
    btc_safe = True
    if btc_df is not None:
        sma200 = ta.sma(btc_df['close'], length=200).iloc[-1]
        if btc_df['close'].iloc[-1] < sma200:
            print("⚠️ BTC 200 SMA Altında (Ayı Piyasası). Sadece mükemmel SFP'ler taranacak.")
            btc_safe = False

    for symbol in symbols:
        try:
            # Sadece 15 Dakikalık Veri Yeterli (Price Action için)
            df_15m = get_data(symbol, '15m', limit=50)
            if df_15m is None: continue

            # Analizi Yap
            is_setup, data = detect_price_action(symbol, None, df_15m)

            if is_setup:
                # BTC güvenli değilse ve Risk %3'ten fazlaysa girme (Stop çok uzak)
                if not btc_safe and data['risk'] > 3.0: continue
                
                # HEDEFLER (Fibo Mantığı veya R/R)
                # SFP işlemlerinde hedef genelde 3R'dır.
                # Giriş: 10, Stop: 9 (Risk 1). Hedef: 13.
                risk_amt = data['price'] - data['stop']
                tp1 = data['price'] + (risk_amt * 2)
                tp2 = data['price'] + (risk_amt * 3.5)
                
                tp_pct = ((tp1 - data['price']) / data['price']) * 100
                
                signal_time = tr_time.strftime('%d %b %H:%M')

                msg = f"""
🦅 <b>PRICE ACTION SİNYALİ (SFP)</b>
━━━━━━━━━━━━━━━━━━━━
<b>#{symbol}</b>   |   ⏱ <code>{signal_time}</code>
━━━━━━━━━━━━━━━━━━━━
🕸 <b>FORMASYON:</b> Swing Failure Pattern (Likidite Avı)
💥 <b>DURUM:</b> Destek altı stoplar patlatıldı ve fiyat geri döndü.

💵 <b>GİRİŞ :</b> <code>{data['price']:.4f}</code>
🛡️ <b>STOP  :</b> <code>{data['stop']:.4f}</code> (İğne Ucu)
⚠️ <b>Risk  :</b> %{data['risk']:.2f}

🎯 <b>HEDEFLER</b>
━━━━━━━━━━━━━━━━━━━━
Target 1 (2R): <code>{tp1:.4f}</code>
Target 2 (3.5R): <code>{tp2:.4f}</code>
Potansiyel: <b>%{tp_pct:.2f}</b>

📊 <b>TEKNİK DETAY</b>
• Eski Dip (Destek): <code>{data['swing_low']:.4f}</code>
• Hacim Artışı: <b>x{data['vol_mult']:.1f}</b>

<i>⚠️ Bu strateji "Stop Avı"nı yakalar. Stopsuz işlem yapma.</i>
"""
                try:
                    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
                    payload = {"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"}
                    requests.post(url, json=payload)
                except Exception as e:
                    print(f"Telegram Hatası: {e}")

                print(f"✅ SFP YAKALANDI: {symbol}")

        except Exception as e:
            continue

    print("🏁 Tarama Bitti.")
    gc.collect()

# --- 6. BAŞLATMA ---
if __name__ == "__main__":
    # SFP anlık bir olaydır, 15 dakikalık mum kapanışında bakılır.
    # Her 5 dakikada bir kontrol eder ki kapanışı kaçırmasın.
    scheduler = BackgroundScheduler()
    scheduler.add_job(func=run_analysis, trigger="interval", minutes=5)
    scheduler.start()
    print("🚀 PRICE ACTION BOTU BAŞLATILDI (SFP MODU).")

    def ilk_tarama():
        time.sleep(10)
        run_analysis()

    threading.Thread(target=ilk_tarama).start()

    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port, use_reloader=False)
