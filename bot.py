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

# Sinyal Soğuma Süresi (Dakika)
# Bir coin sinyal verdikten sonra kaç dakika sessize alınsın?
COOLDOWN_MINUTES = 60 

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

# GLOBAL DEĞİŞKEN: Sinyal Geçmişini Tutan Hafıza
signal_history = {}

@app.route('/')
def home():
    return "🚀 Sniper Bot (COOLDOWN AKTİF) Çalışıyor!"

# --- 3. YARDIMCI FONKSİYONLAR ---

def get_tradable_symbols():
    try:
        exchange.load_markets()
        symbols = []
        for symbol in exchange.markets:
            if symbol.endswith('/USDT') and exchange.markets[symbol]['active']:
                if not any(ignored in symbol for ignored in IGNORED_COINS):
                    symbols.append(symbol)
        # print(f"✅ Tarama Listesi: {len(symbols)} coin.")
        return symbols
    except Exception as e:
        print(f"Liste hatası: {e}")
        return []

def get_data(symbol, timeframe, limit=100):
    try:
        time.sleep(0.15) 
        bars = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        df = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df.set_index('timestamp', inplace=True)
        return df
    except:
        return None

# --- 4. STRATEJİ MOTORLARI ---
# === STRATEJİ 1: SFP (TUZAK AVCISI - SIKIŞTIRILMIŞ VERSİYON) ===
def detect_sfp(df_15m):
    try:
        # İndikatör Hazırlığı
        df_15m['rsi'] = ta.rsi(df_15m['close'], length=14)
        df_15m['vol_ma'] = ta.sma(df_15m['volume'], length=20)
        
        last = df_15m.iloc[-1]
        
        # 1. Swing Low Belirleme (Daha geriye bakıyoruz: 50 mum)
        # Sadece çok belirgin dipleri ciddiye al.
        past_candles = df_15m.iloc[-51:-1]
        swing_low = past_candles['low'].min()
        
        # 2. TEMEL SFP KURALI
        swept = last['low'] < swing_low       # Dibi deldi
        reclaimed = last['close'] > swing_low # Üstünde kapattı
        
        # --- YENİ FİLTRELER (GÜRÜLTÜ ENGELLEYİCİ) ---
        
        # FİLTRE A: RSI FİLTRESİ
        # SFP genellikle aşırı satım bölgesinde çalışır. 
        # RSI 50'nin üzerindeyse bu bir tuzak değil, düzeltmedir.
        rsi_ok = last['rsi'] < 45
        
        # FİLTRE B: MUM ŞEKLİ (PINBAR / ÇEKİÇ)
        # Mumun gövdesi yukarıda olmalı. Yani uzun bir alt iğne olmalı.
        # (Kapanış - Düşük) / (Yüksek - Düşük) oranı %60'tan büyük olmalı.
        candle_range = last['high'] - last['low']
        if candle_range == 0: return False, None
        
        body_position = (last['close'] - last['low']) / candle_range
        is_pinbar = body_position > 0.60 
        
        # FİLTRE C: HACİM
        # Hacim ortalamanın üzerinde olmalı
        vol_ok = last['volume'] > last['vol_ma']

        # HEPSİ BİRDEN OLACAK
        if swept and reclaimed and rsi_ok and is_pinbar and vol_ok:
            stop_dist = ((last['close'] - last['low']) / last['close']) * 100
            return True, {
                'type': '🦅 SFP (PINBAR + RSI)',
                'price': last['close'],
                'stop': last['low'],
                'risk': stop_dist,
                'desc': f'Stop patlatıldı, RSI:{int(last["rsi"])} ve Güçlü Mum Kapanışı.'
            }
        return False, None
    except: return False, None
        
# === STRATEJİ 2: BULLISH DIVERGENCE (UYUMSUZLUK) ===
def detect_divergence(df_15m):
    try:
        df_15m['rsi'] = ta.rsi(df_15m['close'], length=14)
        last = df_15m.iloc[-1]
        
        window = 20
        scan_range = df_15m.iloc[-(window+1):-1]
        prev_low_val = scan_range['low'].min()
        prev_low_idx = scan_range['low'].idxmin()
        prev_rsi_val = df_15m.loc[prev_low_idx]['rsi']
        
        curr_low_val = last['low']
        curr_rsi_val = last['rsi']
        
        price_lower = curr_low_val < prev_low_val
        rsi_higher = curr_rsi_val > prev_rsi_val
        rsi_oversold = curr_rsi_val < 45 
        green_candle = last['close'] > last['open']

        if price_lower and rsi_higher and rsi_oversold and green_candle:
            stop_dist = ((last['close'] - last['low']) / last['close']) * 100
            return True, {
                'type': '🐂 RSI UYUMSUZLUK',
                'price': last['close'],
                'stop': last['low'],
                'risk': stop_dist,
                'desc': 'Fiyat düşerken RSI yükseliyor (Güç Topluyor).'
            }
        return False, None
    except: return False, None

# === STRATEJİ 3: WT + MFI + SUPERTREND (MOMENTUM) ===
def detect_momentum_indicators(df_15m):
    try:
        # WaveTrend
        ap = (df_15m['high'] + df_15m['low'] + df_15m['close']) / 3
        esa = ta.ema(ap, 10)
        d = ta.ema(abs(ap - esa), 10)
        ci = (ap - esa) / (0.015 * d)
        wt1 = ta.ema(ci, 21) 
        wt2 = ta.sma(wt1, 4) 
        
        # MFI
        mfi = ta.mfi(df_15m['high'], df_15m['low'], df_15m['close'], df_15m['volume'], length=14)
        
        # SuperTrend
        st = ta.supertrend(df_15m['high'], df_15m['low'], df_15m['close'], length=10, multiplier=3)
        st_dir = st[st.columns[1]] 
        
        # ADX
        adx = ta.adx(df_15m['high'], df_15m['low'], df_15m['close'])['ADX_14']

        last = df_15m.iloc[-1]
        last_wt1 = wt1.iloc[-1]
        last_wt2 = wt2.iloc[-1]
        prev_wt1 = wt1.iloc[-2]
        prev_wt2 = wt2.iloc[-2]
        last_mfi = mfi.iloc[-1]
        last_st = st_dir.iloc[-1]
        last_adx = adx.iloc[-1]

        # ŞARTLAR: WaveTrend Kesişimi + MFI Para Girişi + Trend Gücü
        wt_cross_up = (prev_wt1 < prev_wt2) and (last_wt1 > last_wt2)
        wt_valid_level = last_wt1 < 55
        mfi_ok = last_mfi > 40
        trend_ok = (last_st == 1) and (last_adx > 20)

        if wt_cross_up and wt_valid_level and mfi_ok and trend_ok:
            atr_val = ta.atr(df_15m['high'], df_15m['low'], df_15m['close'], length=14).iloc[-1]
            stop_price = last['close'] - (2 * atr_val)
            stop_dist = ((last['close'] - stop_price) / last['close']) * 100

            return True, {
                'type': '🚀 WT-MFI MOMENTUM',
                'price': last['close'],
                'stop': stop_price,
                'risk': stop_dist,
                'desc': f'WaveTrend AL + Para Girişi (MFI:{int(last_mfi)}) + Trend Güçlü'
            }
        
        return False, None

    except Exception as e:
        return False, None

# --- 5. ANA ANALİZ DÖNGÜSÜ ---

def run_analysis():
    tr_time = datetime.now() + timedelta(hours=3)
    print(f"\n🔎 [TARAMA] Saat: {tr_time.strftime('%H:%M')}")
    
    symbols = get_tradable_symbols()
    
    # Hafıza Temizliği (Eski kayıtları sil)
    # Eğer 60 dakikadan eski kayıt varsa sözlükten çıkar
    current_time = datetime.now()
    to_remove = []
    for sym, time_val in signal_history.items():
        if (current_time - time_val) > timedelta(minutes=COOLDOWN_MINUTES):
            to_remove.append(sym)
    for sym in to_remove:
        del signal_history[sym]

    # BTC Durumu
    btc_df = get_data(BTC_SYMBOL, '4h', limit=200)
    btc_trend = "NÖTR"
    if btc_df is not None:
        sma200 = ta.sma(btc_df['close'], length=200).iloc[-1]
        btc_trend = "AYI" if btc_df['close'].iloc[-1] < sma200 else "BOĞA"

    for symbol in symbols:
        try:
            # 1. SOĞUMA KONTROLÜ (COOLDOWN CHECK)
            # Eğer bu coine son 60 dakikada sinyal atıldıysa, PAS GEÇ.
            if symbol in signal_history:
                continue

            # 15 Dakikalık Veri
            df_15m = get_data(symbol, '15m', limit=100)
            if df_15m is None: continue

            signal_found = False
            data = {}

            # Strateji 1: SFP
            is_sfp, sfp_data = detect_sfp(df_15m)
            if is_sfp:
                signal_found = True
                data = sfp_data
            
            # Strateji 2: Uyumsuzluk
            if not signal_found:
                is_div, div_data = detect_divergence(df_15m)
                if is_div:
                    signal_found = True
                    data = div_data
            
            # Strateji 3: Momentum (WT-MFI)
            if not signal_found:
                is_mom, mom_data = detect_momentum_indicators(df_15m)
                if is_mom:
                    signal_found = True
                    data = mom_data

            # SİNYAL GÖNDERİMİ
            if signal_found:
                if data['risk'] > 4.0: continue

                # Sinyal Hafızasına Kaydet (Bir daha bakma)
                signal_history[symbol] = datetime.now()

                risk_amt = data['price'] - data['stop']
                tp1 = data['price'] + (risk_amt * 2)
                tp2 = data['price'] + (risk_amt * 4)
                tp_pct = ((tp1 - data['price']) / data['price']) * 100
                
                signal_time = tr_time.strftime('%d %b %H:%M')

                msg = f"""
🎯 <b>KESKİN NİŞANCI SİNYALİ</b>
━━━━━━━━━━━━━━━━━━━━
<b>#{symbol}</b>   |   ⏱ <code>{signal_time}</code>
━━━━━━━━━━━━━━━━━━━━
🛠 <b>STRATEJİ:</b> {data['type']}
📝 <b>NEDEN:</b> {data['desc']}

💵 <b>GİRİŞ :</b> <code>{data['price']:.4f}</code>
🛡️ <b>STOP  :</b> <code>{data['stop']:.4f}</code> (Risk: %{data['risk']:.2f})

🎯 <b>HEDEFLER (%5-10 Hedefli)</b>
━━━━━━━━━━━━━━━━━━━━
1️⃣ Hedef: <code>{tp1:.4f}</code>
2️⃣ Hedef: <code>{tp2:.4f}</code>
Potansiyel: <b>%{tp_pct:.2f}</b>

🌍 <b>Piyasa:</b> BTC {btc_trend} Modunda
"""
                try:
                    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
                    payload = {"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"}
                    requests.post(url, json=payload)
                except Exception as e:
                    print(f"Telegram Hatası: {e}")

                print(f"✅ SİNYAL: {symbol} - {data['type']}")

        except Exception as e:
            continue

    print("🏁 Tarama Bitti.")
    gc.collect()

# --- 6. BAŞLATMA ---
if __name__ == "__main__":
    scheduler = BackgroundScheduler()
    scheduler.add_job(func=run_analysis, trigger="interval", minutes=5)
    scheduler.start()
    print("🚀 BOT BAŞLATILDI (TEKRAR SİNYAL KORUMALI).")

    def ilk_tarama():
        time.sleep(10)
        run_analysis()

    threading.Thread(target=ilk_tarama).start()

    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port, use_reloader=False)
