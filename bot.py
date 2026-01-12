import ccxt
import pandas as pd
import pandas_ta as ta
import time
import os
import requests
from datetime import datetime

# --- AYARLAR VE GÜVENLİK ---
# Bu bilgileri Render'da "Environment Variables" kısmına ekleyeceksin.
API_KEY = os.getenv('BINANCE_API_KEY')
API_SECRET = os.getenv('BINANCE_SECRET_KEY')
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

# İzlenecek Coin ve Ayarlar
SYMBOL = 'ETH/USDT'  # Örnek olarak ETH
TIMEFRAME_SHORT = '1h'
TIMEFRAME_LONG = '4h'
BTC_SYMBOL = 'BTC/USDT'

# Borsa Bağlantısı
exchange = ccxt.binance({
    'apiKey': API_KEY,
    'secret': API_SECRET,
    'options': {'defaultType': 'future'} # Vadeli işlemler için
})

# --- TELEGRAM FONKSİYONU ---
def send_telegram(message):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": CHAT_ID, "text": message}
        requests.post(url, json=payload)
    except Exception as e:
        print(f"Telegram hatası: {e}")

# --- VERİ ÇEKME FONKSİYONU ---
def get_data(symbol, timeframe, limit=100):
    bars = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    df = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    return df

# --- ORDER BOOK ANALİZİ (Modül 5) ---
def check_order_book(symbol):
    try:
        # İlk 20 emri çek
        orderbook = exchange.fetch_order_book(symbol, limit=20)
        bids = orderbook['bids']
        asks = orderbook['asks']
        
        total_bid_vol = sum([bid[1] for bid in bids])
        total_ask_vol = sum([ask[1] for ask in asks])
        
        if total_ask_vol == 0: return False
        
        ratio = total_bid_vol / total_ask_vol
        # Alıcılar satıcıların 1.2 katından fazlaysa ONAY
        return ratio > 1.2
    except:
        return False

# --- BTC GÜVENLİK KONTROLÜ (Modül 1) ---
def check_btc_safety():
    try:
        # BTC 4 Saatlik ve 15 Dakikalık veri
        df_btc_4h = get_data(BTC_SYMBOL, '4h', limit=50)
        df_btc_15m = get_data(BTC_SYMBOL, '15m', limit=5)
        
        # 1. Trend Kontrolü (SMA 50 Üstü mü?)
        sma50 = ta.sma(df_btc_4h['close'], length=50).iloc[-1]
        trend_ok = df_btc_4h['close'].iloc[-1] > sma50
        
        # 2. Flash Crash Koruması (Son 15dk'da %1'den fazla düştü mü?)
        open_price = df_btc_15m['open'].iloc[-1]
        close_price = df_btc_15m['close'].iloc[-1]
        crash_pct = ((close_price - open_price) / open_price) * 100
        crash_ok = crash_pct > -1.0 # %1'den fazla düşüş yoksa True
        
        return trend_ok and crash_ok
    except Exception as e:
        print(f"BTC Kontrol Hatası: {e}")
        return False

# --- ANA STRATEJİ KONTROLÜ ---
def check_strategy(symbol):
    print(f"Analiz ediliyor: {symbol} - {datetime.now()}")
    
    # Verileri Çek
    df_1h = get_data(symbol, TIMEFRAME_SHORT, limit=100)
    df_4h = get_data(symbol, TIMEFRAME_LONG, limit=100)
    
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
    df_4h['supertrend_dir'] = st_4h['SUPERTd_10_3.0'] # 1=Up, -1=Down
    adx_4h = ta.adx(df_4h['high'], df_4h['low'], df_4h['close'], length=14)
    df_4h['adx'] = adx_4h['ADX_14']
    df_4h['atr'] = ta.atr(df_4h['high'], df_4h['low'], df_4h['close'], length=14)

    # Son Değerler
    last_1h = df_1h.iloc[-1]
    prev_1h = df_1h.iloc[-2]
    last_4h = df_4h.iloc[-1]
    
    # --- KONTROL LİSTESİ (MASTER CHECKLIST) ---

    # MODÜL 1: BTC Güvenliği
    if not check_btc_safety():
        print("BTC Güvenli Değil (Düşüş Trendi veya Crash).")
        return

    # MODÜL 2: Ana Trend (4H)
    if last_4h['supertrend_dir'] != 1: return # Trend Aşağı
    if last_4h['adx'] < 25: return # Trend Zayıf

    # MODÜL 3: Para Akışı (1H)
    if last_1h['cmf'] <= 0: return # Para girişi yok
    if last_1h['close'] <= last_1h['vwap']: return # Kurumsal maliyetin altında
    # Hacim Patlaması (Son mum hacmi ortalamanın 2 katı mı?)
    if last_1h['volume'] < (last_1h['vol_ma'] * 2.0): return 

    # MODÜL 4: Teknik Giriş (1H)
    # EMA Sıralaması (Momentum)
    if not (last_1h['close'] > last_1h['ema20'] > last_1h['ema50']): return
    # RSI Tetiklemesi (Kesişim)
    if not (last_1h['rsi'] > 50 and last_1h['rsi'] > last_1h['rsi_ma']): return

    # MODÜL 5: Order Book (Anlık Tahta)
    if not check_order_book(symbol):
        print("Tahta Baskısı Negatif.")
        return

    # --- HEPSİ TAMAMSA ---
    atr_val = last_4h['atr']
    stop_loss = last_1h['close'] - (2 * atr_val)
    take_profit_1 = last_1h['close'] + (3 * atr_val) # Dinamik hedef
    
    msg = f"""
    🚀 MÜKEMMEL KURULUM TESPİT EDİLDİ! 🚀
    
    Coin: {symbol}
    Fiyat: {last_1h['close']}
    
    ✅ BTC Güvenli
    ✅ 4H SuperTrend Yeşil & ADX Güçlü
    ✅ CMF Pozitif (Para Girişi Var)
    ✅ VWAP Üzerinde
    ✅ Hacim Patlaması Mevcut
    ✅ Order Book Alıcı Baskın
    
    🛑 Önerilen Stop: {stop_loss:.4f}
    💰 Önerilen Hedef: {take_profit_1:.4f}
    """
    
    send_telegram(msg)
    print("SİNYAL GÖNDERİLDİ!")
    
    # Buraya otomatik işlem açma kodu eklenebilir.
    # exchange.create_market_buy_order(symbol, miktar) gibi.

# --- SONSUZ DÖNGÜ (RENDER İÇİN) ---
def main():
    print("Bot Başlatıldı... Render Modu Aktif.")
    send_telegram("Bot Başladı! Piyasa taranıyor...")
    
    while True:
        try:
            check_strategy(SYMBOL)
            # Her 15 dakikada bir kontrol et (Rate limit yememek için)
            time.sleep(900) 
        except Exception as e:
            print(f"Ana döngü hatası: {e}")
            time.sleep(60)

if __name__ == "__main__":
    main()
