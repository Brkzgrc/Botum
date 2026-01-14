import ccxt
import pandas as pd
import pandas_ta as ta
import time
import os
import requests
import threading
from flask import Flask
from datetime import datetime
import sys
import gc # RAM temizliği için gerekli
from apscheduler.schedulers.background import BackgroundScheduler

# Bu satır logların anında akmasını sağlar:
sys.stdout.reconfigure(line_buffering=True)

# --- 1. AYARLAR VE GÜVENLİK ---
API_KEY = os.getenv('BINANCE_API_KEY')
API_SECRET = os.getenv('BINANCE_SECRET_KEY')
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

# İzlenecek Coin ve Parametreler
# Taranmayacaklar Listesi (Stablecoinler, Fiatlar ve Kaldıraçlılar)
IGNORED_COINS = [
    'UP/USDT', 'DOWN/USDT', 'BEAR/USDT', 'BULL/USDT',
    'USDC/USDT', 'TUSD/USDT', 'FDUSD/USDT', 'DAI/USDT', 'USDP/USDT',
    'EUR/USDT', 'TRY/USDT', 'GBP/USDT', 'BUSD/USDT', 'USTC/USDT', # Virgül eklendi
    'PAXG/USDT', 'WBTC/USDT', 'USDE/USDT', 'BRL/USDT', 'RUB/USDT',
    'AUD/USDT', 'UST/USDT', 'USD/USDT', 'XUSD/USDT', 'USD1/USDT',
]
BTC_SYMBOL = 'BTC/USDT' # Piyasa barometresi
TIMEFRAME_SHORT = '1h'  # Giriş sinyali
TIMEFRAME_LONG = '4h'   # Trend onayı

# --- 2. BORSA BAĞLANTISI ---
exchange = ccxt.binance({
    'apiKey': API_KEY,
    'secret': API_SECRET,
    'options': {'defaultType': 'spot'}, # DİKKAT: Spot yapıldı
    'enableRateLimit': True,
    'timeout': 30000
})

# --- 3. FLASK WEB SUNUCUSU ---
app = Flask(__name__)

@app.route('/')
def home():
    return "🚀 Sniper Bot (Python Modu) 7/24 Aktif!"

# --- 4. YARDIMCI FONKSİYONLAR ---

def send_telegram(message):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": CHAT_ID, "text": message}
        requests.post(url, json=payload)
    except Exception as e:
        print(f"Telegram Hatası: {e}")

def get_tradable_symbols():
    """Binance Spot piyasasındaki uygun USDT çiftlerini bulur."""
    try:
        exchange.load_markets()
        symbols = []
        for symbol in exchange.markets:
            if symbol.endswith('/USDT') and exchange.markets[symbol]['active']:
                if not any(ignored in symbol for ignored in IGNORED_COINS):
                    symbols.append(symbol)
        print(f"✅ Toplam {len(symbols)} adet coin tarama listesine alındı.")
        return symbols
    except Exception as e:
        print(f"Sembol listesi alınamadı: {e}")
        return []

def get_data(symbol, timeframe, limit=100):
    try:
        # 1. Normal Bekleme (Senin istediğin ayar)
        # Her veri isteğinden önce 0.5 saniye bekler.
        # Bir coin için 3 istek yapıldığı için coin başı toplam 1.5 sn sürer.
        time.sleep(0.5) 
        
        bars = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        df = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df.set_index('timestamp', inplace=True)
        return df

    except Exception as e:
        err_msg = str(e)
        
        # 2. Akıllı Fren Sistemi (Anti-Ban)
        # Eğer hata "429" (Çok Hızlı) veya "418" (Banlı) içeriyorsa:
        if "429" in err_msg or "Too Many Requests" in err_msg or "418" in err_msg:
            print(f"🛑 HIZ SINIRI AŞILDI! Binance fren yaptı. ({symbol})")
            print("⏳ Bot cezanın bitmesi için 2 dakika beklemeye geçiyor...")
            
            time.sleep(120) # 120 saniye (2 dakika) sistemi dondur ve bekle
            
            print("▶️ Bekleme bitti, tekrar deneniyor...")
        else:
            # Diğer basit hatalar (internet kopması vs.) için sadece log düş
            print(f"⚠️ Veri alınamadı ({symbol}): {e}")
        
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

def check_rebellion(df_15m):
    """
    BTC düşerken coinin ayrışıp ayrışmadığını (15dk) kontrol eder.
    AYRICA FOMO KORUMASI İÇERİR.
    """
    last_candle = df_15m.iloc[-1]
    
    # 1. Fiyat Değişimi Hesapla (Anlık Mum)
    open_p = last_candle['open']
    close_p = last_candle['close']
    change_pct = ((close_p - open_p) / open_p) * 100
    
    # KURAL A: Güçlü Yükseliş Var mı? (En az %2 yükselmeli)
    is_strong = change_pct > 2.0
    
    # KURAL B: Tepeden mi Giriyoruz? (FOMO KORUMASI)
    # Eğer mum şimdiden %6'dan fazla yükseldiyse girme, riskli.
    not_too_late = change_pct < 6.0
    
    # KURAL C: Hacim Patlaması (Ortalamanın 2.5 katı)
    volume_explosion = last_candle['volume'] > (last_candle['vol_ma'] * 2.5)
    
    # KURAL D: RSI Momentum (RSI 55 üzeri olmalı)
    rsi_strong = last_candle['rsi'] > 55
    
    # Tüm şartlar sağlanmalı
    if is_strong and not_too_late and volume_explosion and rsi_strong:
        return True, f"Ayrışma Onaylandı! (Artış: %{change_pct:.2f})"
    else:
        return False, "Şartlar Sağlanmadı"
        
# --- 6. ANA STRATEJİ MOTORU ---
def run_analysis():
    print(f"\n🔎 [TÜM PİYASA TARANIYOR] Saat: {datetime.now().strftime('%H:%M')}")
    
    # 1. Coin Listesini Al
    symbols = get_tradable_symbols()
    
    # 2. BTC Kontrolü
    btc_safe = check_btc_safety()
    if not btc_safe:
        print("⚠️ BTC Güvenli Değil! Sadece 'İsyan' eden coinler aranacak.")

    # 3. DÖNGÜ BAŞLIYOR (Her coin tek tek sorguya çekiliyor)
    for symbol in symbols:
        try:
            # --- VERİLERİ ÇEK ---
            df_1h = get_data(symbol, TIMEFRAME_SHORT, limit=100)
            df_4h = get_data(symbol, TIMEFRAME_LONG, limit=100)
            df_15m = get_data(symbol, '15m', limit=50) 
            
            if df_1h is None or df_4h is None or df_15m is None: continue

            # --- İNDİKATÖRLERİ HESAPLA ---
            
            # 15M Hesaplamaları
            df_15m['vol_ma'] = ta.sma(df_15m['volume'], length=20)
            df_15m['rsi'] = ta.rsi(df_15m['close'], length=14)

            # 1H Hesaplamaları
            df_1h['rsi'] = ta.rsi(df_1h['close'], length=14)
            df_1h['rsi_ma'] = ta.sma(df_1h['rsi'], length=14)
            df_1h['ema20'] = ta.ema(df_1h['close'], length=20)
            df_1h['ema50'] = ta.ema(df_1h['close'], length=50)
            df_1h['cmf'] = ta.cmf(df_1h['high'], df_1h['low'], df_1h['close'], df_1h['volume'], length=20)
            df_1h['vwap'] = ta.vwap(df_1h['high'], df_1h['low'], df_1h['close'], df_1h['volume'])
            df_1h['vol_ma'] = ta.sma(df_1h['volume'], length=20)
            df_1h['adx'] = ta.adx(df_1h['high'], df_1h['low'], df_1h['close'], length=14)['ADX_14']
            
            # 4H Hesaplamaları
            st_4h = ta.supertrend(df_4h['high'], df_4h['low'], df_4h['close'], length=10, multiplier=3)
            df_4h['st_dir'] = st_4h[st_4h.columns[1]]
            adx_val_4h = ta.adx(df_4h['high'], df_4h['low'], df_4h['close'], length=14)
            df_4h['adx'] = adx_val_4h['ADX_14']
            df_4h['atr'] = ta.atr(df_4h['high'], df_4h['low'], df_4h['close'], length=14)

            last_1h = df_1h.iloc[-1]
            last_4h = df_4h.iloc[-1]
            
            # --- KONTROL LİSTESİ (FİLTRELER) ---
            # A. İsyan Kontrolü
            is_rebelling, rebel_reason = check_rebellion(df_15m)

            # Her turda hafıza temizliği
            del df_1h, df_4h, df_15m

            # BTC Kötüyse ve Coin İsyan Etmiyorsa -> ÇÖPE AT
            if not btc_safe:
                if not is_rebelling: continue 
            
            # B. Ana Trend (4H) Kontrolü
            if last_4h['st_dir'] != 1: continue # Trend Kırmızıysa -> ÇÖPE AT
            if last_4h['adx'] < 20: continue    # Trend Zayıfsa -> ÇÖPE AT

            # C. Kısa Vade Trend (1H)
            if last_1h['adx'] < 20: continue    # 1H Trend Zayıfsa -> ÇÖPE AT

            # D. Para Akışı ve Fiyat
            if last_1h['cmf'] <= 0: continue                  # Para girişi yoksa -> ÇÖPE AT
            if last_1h['close'] <= last_1h['vwap']: continue  # Pahalıysa -> ÇÖPE AT
            if last_1h['volume'] < (last_1h['vol_ma'] * 1.5): continue # Hacim azsa -> ÇÖPE AT

            # E. Teknik Tetikleyiciler
            if not (last_1h['close'] > last_1h['ema20'] > last_1h['ema50']): continue # EMA sırası bozuksa -> ÇÖPE AT
            if not (last_1h['rsi'] > 50 and last_1h['rsi'] > last_1h['rsi_ma']): continue # RSI zayıfsa -> ÇÖPE AT

            # F. Order Book (Tahta Baskısı) - En son bakılır
            if not check_order_book(symbol): continue # Satıcılar çoksa -> ÇÖPE AT

            # --- 4. SİNYAL OLUŞTU ---
            # (Buraya kadar gelen coin tüm testleri geçmiştir)

            entry_price = last_1h['close']
            atr_val = last_4h['atr']
            stop_loss = entry_price - (2 * atr_val)
            take_profit = entry_price + (3 * atr_val)
            
            # Yüzdelik Hesaplama
            tp_pct = ((take_profit - entry_price) / entry_price) * 100
            sl_pct = ((entry_price - stop_loss) / entry_price) * 100
            
            # Strateji ismini Türkçe yapıyoruz
            strategy_tag = "🔥 İSYAN (POZİTİF AYRIŞMA)" if (not btc_safe and is_rebelling) else "🌊 GÜÇLÜ TREND TAKİBİ"
            signal_time = datetime.now().strftime('%d %b %H:%M')

            # --- TÜRKÇE PROFESYONEL MESAJ TASARIMI ---
            msg = f"""
🚀 <b>STRATEJİ:</b> {strategy_tag}
━━━━━━━━━━━━━━━━━━━━
<b>#{symbol}</b>   |   ⏱ <code>{signal_time}</code>
━━━━━━━━━━━━━━━━━━━━

🎯 <b>HEDEFLER VE RİSK YÖNETİMİ</b>
━━━━━━━━━━━━━━━━━━━━
💵 <b>GİRİŞ :</b> <code>{entry_price:.4f}</code>
🛡️ <b>STOP  :</b> <code>{stop_loss:.4f} / Risk: %{sl_pct:.2f}</code>
💰 <b>TP    :</b> <code>{take_profit:.4f} / Potansiyel: %{tp_pct:.2f}</code>

📊 <b>TEKNİK GÖSTERGELER</b>
━━━━━━━━━━━━━━━━━━━━
⚡ <b>Trend Gücü (ADX):</b>
   • 1S: <code>{int(last_1h['adx'])}</code> (Kısa Vade)
   • 4S: <code>{int(last_4h['adx'])}</code> (Ana Trend)
   
🐳 <b>Hacim ve Para Akışı:</b>
   • CMF: ✅ Pozitif (Para Girişi)
   • VWAP: ✅ Fiyat Ort. Üstü
   • Tahta: ✅ Alıcılar Baskın

━━━━━━━━━━━━━━━━━━━━

"""
            try:
                url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
                payload = {"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"}
                requests.post(url, json=payload)
            except Exception as e:
                print(f"Telegram Gönderim Hatası: {e}")

            print(f"✅ TÜRKÇE SİNYAL GÖNDERİLDİ: {symbol}")
        
        except Exception as e:
            continue

    print("🏁 Tarama Bitti. Bellek Temizleniyor.")
    gc.collect()

# --- 7. BAŞLATMA VE ZAMANLAYICI (RENDER İÇİN DÜZELTİLMİŞ) ---
if __name__ == "__main__":
    # 1. Zamanlayıcıyı Başlat
    scheduler = BackgroundScheduler()
    scheduler.add_job(func=run_analysis, trigger="interval", minutes=30)
    scheduler.start()
    print("🚀 Bot Başlatıldı (Python Modu) - 30dk Arayla Tarayacak.")

    # 2. İlk Taramayı "Arka Planda" Başlat (Flask'ı bekletmemek için)
    # Bu sayede Render 'Port scan timeout' hatası vermez.
    def ilk_tarama_baslat():
        print("⏳ İlk tarama 10 saniye içinde başlayacak (Sunucu açılışı bekleniyor)...")
        time.sleep(10) # Flask tam açılsın diye minik bir bekleme
        try:
            run_analysis()
        except Exception as e:
            print(f"İlk tarama hatası: {e}")

    # İşlemi ayrı bir kanalda (Thread) başlatıyoruz
    threading.Thread(target=ilk_tarama_baslat).start()

    # 3. Web Sunucusunu Başlat (HEMEN AÇILMALI)
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port, use_reloader=False)
