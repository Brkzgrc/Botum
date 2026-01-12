import ccxt
import pandas as pd
import pandas_ta as ta
import time
import os
import requests
import threading
from flask import Flask
from datetime import datetime

# --- 1. AYARLAR VE GÜVENLİK ---
API_KEY = os.getenv('BINANCE_API_KEY')
API_SECRET = os.getenv('BINANCE_SECRET_KEY')
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

# İzlenecek Coin ve Parametreler
SYMBOL = 'ETH/USDT'     # Hangi coini takip edeceksin?
BTC_SYMBOL = 'BTC/USDT' # Piyasa barometresi
TIMEFRAME_SHORT = '1h'  # Giriş sinyali
TIMEFRAME_LONG = '4h'   # Trend onayı

# --- 2. BORSA BAĞLANTISI ---
exchange = ccxt.binance({
    'apiKey': API_KEY,
    'secret': API_SECRET,
    'options': {'defaultType': 'future'}, # Vadeli işlemler verisi
    'enableRateLimit': True
})

# --- 3. FLASK WEB SUNUCUSU (RENDER İÇİN) ---
app = Flask(__name__)

@app.route('/')
def home():
    return "🚀 Sniper Bot 7/24 Aktif! Piyasa Taraniyor..."

def run_web_server():
    port = int(os.environ.get("PORT", 10000)) # Render genelde 10000 portunu kullanır
    app.run(host='0.0.0.0', port=port)

# --- 4. YARDIMCI FONKSİYONLAR ---

def send_telegram(message):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": CHAT_ID, "text": message}
        requests.post(url, json=payload)
    except Exception as e:
        print(f"Telegram Hatası: {e}")

def get_data(symbol, timeframe, limit=100):
    try:
        bars = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        df = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        return df
    except Exception as e:
        print(f"Veri çekme hatası ({symbol}): {e}")
        return None

# --- 5. ANALİZ MODÜLLERİ ---

def check_btc_safety():
    """Modül 1: BTC Güvenliği ve Flash Crash Koruması"""
    try:
        df_btc_4h = get_data(BTC_SYMBOL, '4h', limit=50)
        df_btc_15m = get_data(BTC_SYMBOL, '15m', limit=5)
        
        if df_btc_4h is None or df_btc_15m is None: return False

        # Trend: SMA 50 Üstü mü?
        sma50 = ta.sma(df_btc_4h['close'], length=50).iloc[-1]
        trend_ok = df_btc_4h['close'].iloc[-1] > sma50
        
        # Flash Crash: Son 15dk'da %1'den fazla düştü mü?
        open_price = df_btc_15m['open'].iloc[-1]
        close_price = df_btc_15m['close'].iloc[-1]
        crash_pct = ((close_price - open_price) / open_price) * 100
        crash_ok = crash_pct > -1.0 
        
        return trend_ok and crash_ok
    except:
        return False

def check_order_book(symbol):
    """Modül 5: Order Book Baskısı"""
    try:
        orderbook = exchange.fetch_order_book(symbol, limit=20)
        bids = orderbook['bids']
        asks = orderbook['asks']
        
        total_bid_vol = sum([bid[1] for bid in bids])
        total_ask_vol = sum([ask[1] for ask in asks])
        
        if total_ask_vol == 0: return False
        
        ratio = total_bid_vol / total_ask_vol
        return ratio > 1.2 # Alıcılar %20 daha fazla olmalı
    except:
        return False # Veri yoksa risk alma

# --- 6. ANA STRATEJİ MOTORU ---

def run_analysis():
    print(f"🔍 Analiz Başlıyor: {SYMBOL} - {datetime.now()}")
    
    # Verileri Çek
    df_1h = get_data(SYMBOL, TIMEFRAME_SHORT, limit=100)
    df_4h = get_data(SYMBOL, TIMEFRAME_LONG, limit=100)
    
    if df_1h is None or df_4h is None: return

    # --- HESAPLAMALAR ---
    
    # 1H İndikatörleri
    df_1h['rsi'] = ta.rsi(df_1h['close'], length=14)
    df_1h['rsi_ma'] = ta.sma(df_1h['rsi'], length=14)
    df_1h['ema20'] = ta.ema(df_1h['close'], length=20)
    df_1h['ema50'] = ta.ema(df_1h['close'], length=50)
    df_1h['cmf'] = ta.cmf(df_1h['high'], df_1h['low'], df_1h['close'], df_1h['volume'], length=20)
    df_1h['vwap'] = ta.vwap(df_1h['high'], df_1h['low'], df_1h['close'], df_1h['volume'])
    df_1h['vol_ma'] = ta.sma(df_1h['volume'], length=20)
    
    # 4H İndikatörleri
    st_4h = ta.supertrend(df_4h['high'], df_4h['low'], df_4h['close'], length=10, multiplier=3)
    # SuperTrend sütun ismini bul (kütüphane dinamik isimlendirir)
    st_dir_col = st_4h.columns[1] # Genelde 2. sütun Direction'dır (1 veya -1)
    df_4h['st_dir'] = st_4h[st_dir_col]
    
    adx_4h = ta.adx(df_4h['high'], df_4h['low'], df_4h['close'], length=14)
    df_4h['adx'] = adx_4h['ADX_14']
    df_4h['atr'] = ta.atr(df_4h['high'], df_4h['low'], df_4h['close'], length=14)

    # Son Değerler
    last_1h = df_1h.iloc[-1]
    last_4h = df_4h.iloc[-1]
    
    # --- KONTROL LİSTESİ (CHECKLIST) ---

    # 1. BTC Kontrolü
    if not check_btc_safety():
        print("❌ BTC Tehlikeli (Düşüş veya Crash).")
        return

    # 2. Ana Trend (4H)
    if last_4h['st_dir'] != 1: 
        print("❌ 4H Trend Düşüşte (SuperTrend Kırmızı).")
        return
    if last_4h['adx'] < 25:
        print("❌ 4H Trend Zayıf (ADX < 25).")
        return

    # 3. Para Akışı ve Kurumsal (1H)
    if last_1h['cmf'] <= 0:
        print("❌ Para Çıkışı Var (CMF Negatif).")
        return
    if last_1h['close'] <= last_1h['vwap']:
        print("❌ Fiyat VWAP Altında (Pahalı).")
        return
    if last_1h['volume'] < (last_1h['vol_ma'] * 1.5): # En az 1.5 kat hacim
        print("❌ Hacim Yetersiz.")
        return 

    # 4. Teknik Tetikleyiciler (1H)
    if not (last_1h['close'] > last_1h['ema20'] > last_1h['ema50']):
        print("❌ Momentum Dizilimi Yok (EMA).")
        return
    if not (last_1h['rsi'] > 50 and last_1h['rsi'] > last_1h['rsi_ma']):
        print("❌ RSI Tetiği Yok.")
        return

    # 5. Order Book
    if not check_order_book(SYMBOL):
        print("❌ Tahta Baskısı Satıcılı.")
        return

    # --- HEPSİ OLUMLU İSE ---
    atr_val = last_4h['atr']
    stop_loss = last_1h['close'] - (2 * atr_val)
    take_profit = last_1h['close'] + (3 * atr_val)
    
    msg = f"""
    🚨 MÜKEMMEL KURULUM TESPİT EDİLDİ! 🚨
    
    💎 Coin: {SYMBOL}
    💰 Fiyat: {last_1h['close']}
    
    ✅ BTC: Güvenli ve Stabil
    ✅ Trend: 4H Boğa & ADX Güçlü
    ✅ Para: CMF Pozitif & VWAP Üzeri
    ✅ Onay: Hacim Patlaması & Tahta Baskısı
    
    🛑 Stop Loss: {stop_loss:.4f}
    🎯 Hedef: {take_profit:.4f}
    
    (Bot şu an sadece sinyal modunda. Alım yapmadı.)
    """
    send_telegram(msg)
    print("✅ SİNYAL GÖNDERİLDİ!")

# --- 7. BOT DÖNGÜSÜ ---
def bot_loop():
    print("🤖 Bot Motoru Başlatıldı...")
    send_telegram(f"🤖 Bot Başladı! {SYMBOL} izleniyor (Hybrid Mode).")
    
    while True:
        try:
            run_analysis()
            # Her 15 dakikada bir kontrol (Rate limit ve mum kapanışı için ideal)
            time.sleep(900) 
        except Exception as e:
            print(f"⚠️ Ana Döngü Hatası: {e}")
            time.sleep(60)

# --- 8. BAŞLATMA (THREADING) ---
if __name__ == "__main__":
    # Web sunucusunu arka planda başlat
    t = threading.Thread(target=run_web_server)
    t.daemon = True # Ana program kapanınca bu da kapansın
    t.start()
    
    # Botu ana akışta başlat
    bot_loop()
