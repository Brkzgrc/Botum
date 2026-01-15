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

# --- ÖNEMLİ AYARLAR ---
COOLDOWN_MINUTES = 120    # Aynı coin 2 saat sussun
MAX_SIGNALS_PER_HOUR = 3  # Saatte Max 3 Sinyal Kotası

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

# GLOBAL DEĞİŞKENLER
signal_history = {} 
hourly_counter = {
    'count': 0,
    'reset_time': datetime.now() + timedelta(hours=1)
}

@app.route('/')
def home():
    return "🚀 Sniper Bot (Dinamik Başlık) Aktif!"

# --- 3. YARDIMCI FONKSİYONLAR ---

def get_tradable_symbols():
    try:
        exchange.load_markets()
        symbols = []
        for symbol in exchange.markets:
            if symbol.endswith('/USDT') and exchange.markets[symbol]['active']:
                if not any(ignored in symbol for ignored in IGNORED_COINS):
                    symbols.append(symbol)
        return symbols
    except Exception as e:
        print(f"Liste hatası: {e}")
        return []

def get_data(symbol, timeframe, limit=150):
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

# === STRATEJİ 1: SFP (ZORLAŞTIRILMIŞ) ===
def detect_sfp(df_15m):
    try:
        df_15m['rsi'] = ta.rsi(df_15m['close'], length=14)
        df_15m['vol_ma'] = ta.sma(df_15m['volume'], length=20)
        
        last = df_15m.iloc[-1]
        
        # 24 Saatlik Dip (96 Mum)
        past_candles = df_15m.iloc[-97:-1] 
        swing_low = past_candles['low'].min()
        
        swept = last['low'] < swing_low       
        reclaimed = last['close'] > swing_low 
        
        body = abs(last['close'] - last['open'])
        lower_wick = min(last['close'], last['open']) - last['low']
        
        if body == 0: is_strong = True
        else: is_strong = lower_wick > (body * 2.0)
        
        rsi_ok = last['rsi'] < 35
        
        # GÜNCELLEME: Hacim şartı 1.5 -> 2.0'a çıkarıldı.
        # Tepki "Çok Sert" olmalı.
        vol_ok = last['volume'] > (last['vol_ma'] * 2.0)

        if swept and reclaimed and is_strong and rsi_ok and vol_ok:
            stop_dist = ((last['close'] - last['low']) / last['close']) * 100
            if stop_dist > 4.5: return False, None
            
            return True, {
                'type': '🦅 SFP (DİP AVCISI)',
                'price': last['close'],
                'stop': last['low'],
                'risk': stop_dist,
                'desc': f'Son 24 saatin dibi ({swing_low:.4f}) hacimli şekilde süpürüldü.'
            }
        return False, None
    except: return False, None

# === STRATEJİ 2: UYUMSUZLUK ===
def detect_divergence(df_15m):
    try:
        df_15m['rsi'] = ta.rsi(df_15m['close'], length=14)
        df_15m['vol_ma'] = ta.sma(df_15m['volume'], length=20)
        last = df_15m.iloc[-1]
        
        window = 30
        scan_range = df_15m.iloc[-(window+1):-1]
        prev_low_val = scan_range['low'].min()
        prev_low_idx = scan_range['low'].idxmin()
        prev_rsi_val = df_15m.loc[prev_low_idx]['rsi']
        
        curr_low_val = last['low']
        curr_rsi_val = last['rsi']
        
        price_lower = curr_low_val < prev_low_val
        rsi_higher = curr_rsi_val > prev_rsi_val
        rsi_oversold = curr_rsi_val < 35
        green_candle = last['close'] > last['open']
        vol_ok = last['volume'] > last['vol_ma']

        if price_lower and rsi_higher and rsi_oversold and green_candle and vol_ok:
            stop_dist = ((last['close'] - last['low']) / last['close']) * 100
            return True, {
                'type': '🐂 RSI UYUMSUZLUK',
                'price': last['close'],
                'stop': last['low'],
                'risk': stop_dist,
                'desc': 'Fiyat dip yaparken RSI yükseliyor (Güç Topluyor).'
            }
        return False, None
    except: return False, None

# === STRATEJİ 3: WT-MFI ===
def detect_momentum_indicators(df_15m):
    try:
        ap = (df_15m['high'] + df_15m['low'] + df_15m['close']) / 3
        esa = ta.ema(ap, 10)
        d = ta.ema(abs(ap - esa), 10)
        ci = (ap - esa) / (0.015 * d)
        wt1 = ta.ema(ci, 21) 
        wt2 = ta.sma(wt1, 4) 
        mfi = ta.mfi(df_15m['high'], df_15m['low'], df_15m['close'], df_15m['volume'], length=14)
        st = ta.supertrend(df_15m['high'], df_15m['low'], df_15m['close'], length=10, multiplier=3)
        st_dir = st[st.columns[1]] 
        adx = ta.adx(df_15m['high'], df_15m['low'], df_15m['close'])['ADX_14']

        last = df_15m.iloc[-1]
        
        wt_cross = (wt1.iloc[-2] < wt2.iloc[-2]) and (wt1.iloc[-1] > wt2.iloc[-1])
        wt_loc = wt1.iloc[-1] < 50
        mfi_ok = mfi.iloc[-1] > 55
        trend_ok = (st_dir.iloc[-1] == 1) and (adx.iloc[-1] > 30)

        if wt_cross and wt_loc and mfi_ok and trend_ok:
            atr = ta.atr(df_15m['high'], df_15m['low'], df_15m['close'], length=14).iloc[-1]
            stop_price = last['close'] - (2 * atr)
            stop_dist = ((last['close'] - stop_price) / last['close']) * 100
            
            if stop_dist > 4.5: return False, None

            return True, {
                'type': '🚀 WT-MFI MOMENTUM',
                'price': last['close'],
                'stop': stop_price,
                'risk': stop_dist,
                'desc': f'WaveTrend AL + Para Akışı ({int(mfi.iloc[-1])})'
            }
        return False, None
    except: return False, None

# --- 5. ANA ANALİZ DÖNGÜSÜ ---

def run_analysis():
    global hourly_counter
    if datetime.now() > hourly_counter['reset_time']:
        hourly_counter['count'] = 0
        hourly_counter['reset_time'] = datetime.now() + timedelta(hours=1)
        # print("\n🔄 Saatlik kota sıfırlandı.")

    if hourly_counter['count'] >= MAX_SIGNALS_PER_HOUR:
        print(f"\n⛔ KOTA DOLDU ({MAX_SIGNALS_PER_HOUR}). Beklemede...")
        gc.collect()
        return

    tr_time = datetime.now() + timedelta(hours=3)
    print(f"\n🔎 [TARAMA] Saat: {tr_time.strftime('%H:%M')} (Sayaç: {hourly_counter['count']})")
    
    symbols = get_tradable_symbols()
    
    # Hafıza Temizliği
    current_time = datetime.now()
    to_remove = [sym for sym, t in signal_history.items() if (current_time - t) > timedelta(minutes=COOLDOWN_MINUTES)]
    for sym in to_remove: del signal_history[sym]

    # BTC Durumu
    btc_df = get_data(BTC_SYMBOL, '4h', limit=200)
    btc_trend = "NÖTR"
    if btc_df is not None:
        sma200 = ta.sma(btc_df['close'], length=200).iloc[-1]
        btc_trend = "AYI" if btc_df['close'].iloc[-1] < sma200 else "BOĞA"

    for symbol in symbols:
        if hourly_counter['count'] >= MAX_SIGNALS_PER_HOUR: break

        try:
            if symbol in signal_history: continue

            df_15m = get_data(symbol, '15m', limit=150)
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
            
            # Strateji 3: WT-MFI
            if not signal_found:
                is_mom, mom_data = detect_momentum_indicators(df_15m)
                if is_mom:
                    signal_found = True
                    data = mom_data

            # SİNYAL GÖNDERİMİ (YENİ FORMAT)
            if signal_found:
                hourly_counter['count'] += 1
                signal_history[symbol] = datetime.now()

                risk_amt = data['price'] - data['stop']
                tp1 = data['price'] + (risk_amt * 2)
                tp2 = data['price'] + (risk_amt * 4)
                tp_pct = ((tp1 - data['price']) / data['price']) * 100
                
                signal_time = tr_time.strftime('%d %b %H:%M')

                # GÜNCELLENMİŞ MESAJ FORMATI (SADE VE NET)
                msg = f"""
<b>{data['type']}</b>
━━━━━━━━━━━━━━━━━━━━
<b>#{symbol}</b>   |   ⏱ <code>{signal_time}</code>
━━━━━━━━━━━━━━━━━━━━
📝 <b>NEDEN:</b> {data['desc']}

💵 <b>GİRİŞ :</b> <code>{data['price']:.4f}</code>
🛡️ <b>STOP  :</b> <code>{data['stop']:.4f}</code> (Risk: %{data['risk']:.2f})

🎯 <b>HEDEFLER</b>
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
    print("🚀 BOT BAŞLATILDI (YENİ BAŞLIKLAR + 2x HACİM).")

    def ilk_tarama():
        time.sleep(10)
        run_analysis()

    threading.Thread(target=ilk_tarama).start()

    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port, use_reloader=False)
