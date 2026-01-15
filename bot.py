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

# --- 1. SİSTEM AYARLARI ---
API_KEY = os.getenv('BINANCE_API_KEY')
API_SECRET = os.getenv('BINANCE_SECRET_KEY')
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

# Aynı coine ne kadar süre tekrar bakmasın? (Dakika)
COOLDOWN_MINUTES = 120 

IGNORED_COINS = [
    'UP/USDT', 'DOWN/USDT', 'BEAR/USDT', 'BULL/USDT',
    'USDC/USDT', 'TUSD/USDT', 'FDUSD/USDT', 'DAI/USDT', 'USDP/USDT',
    'EUR/USDT', 'TRY/USDT', 'GBP/USDT', 'BUSD/USDT', 'USTC/USDT',
    'PAXG/USDT', 'WBTC/USDT', 'USDE/USDT', 'BRL/USDT', 'RUB/USDT',
    'AUD/USDT', 'UST/USDT', 'USD/USDT', 'XUSD/USDT', 'USD1/USDT',
]

BTC_SYMBOL = 'BTC/USDT'

# Borsa Bağlantısı
exchange = ccxt.binance({
    'apiKey': API_KEY,
    'secret': API_SECRET,
    'options': {'defaultType': 'spot'},
    'enableRateLimit': True,
    'timeout': 30000
})

app = Flask(__name__)
signal_history = {} 

@app.route('/')
def home():
    return "🚀 Sniper Bot (MASTER TRADER MODU) Aktif!"

# --- 2. PROFESYONEL VERİ YÖNETİMİ ---

def get_tradable_symbols():
    try:
        exchange.load_markets()
        symbols = [s for s in exchange.markets if s.endswith('/USDT') 
                   and exchange.markets[s]['active'] 
                   and not any(i in s for i in IGNORED_COINS)]
        return symbols
    except: return []

def get_data(symbol, timeframe, limit=100):
    try:
        time.sleep(0.1) # Rate Limit Koruması
        bars = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        df = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df.set_index('timestamp', inplace=True)
        return df
    except: return None

# --- 3. BÜYÜK RESİM ANALİZİ (4H TREND FİLTRESİ) ---
# Trader Mantığı: "Trendin tersine işlem açma (Extreme durumlar hariç)"

def check_4h_trend(symbol):
    try:
        df_4h = get_data(symbol, '4h', limit=100)
        if df_4h is None: return "Veri Yok", False

        # Göstergeler
        ema200 = ta.ema(df_4h['close'], length=200).iloc[-1]
        ema50 = ta.ema(df_4h['close'], length=50).iloc[-1]
        rsi = ta.rsi(df_4h['close'], length=14).iloc[-1]
        close = df_4h['close'].iloc[-1]

        # SENARYO 1: BOĞA PİYASASI (Güvenli)
        # Fiyat EMA200 üzerindeyse trend yukarıdır. Her türlü alım denenebilir.
        if close > ema200:
            return "YÜKSELİŞ TRENDİ", True

        # SENARYO 2: AYI PİYASASI (Tehlikeli)
        # Fiyat EMA200 altındaysa trend aşağıdır.
        # Sadece "Aşırı Satım" (RSI < 30) varsa "Tepki Alımı"na izin ver.
        # Yoksa "Trend düşüyor, alma" de.
        elif close < ema200:
            if rsi < 30:
                return "DÜŞÜŞ TRENDİ (Ama Aşırı Satımda - Tepki Gelebilir)", True
            else:
                return "DÜŞÜŞ TRENDİ (Riskli - Uzak Dur)", False
        
        return "NÖTR", True

    except: return "Hata", False

# --- 4. GİRİŞ STRATEJİLERİ (15 Dakikalık Tetikçiler) ---

# A. SFP (SWING FAILURE PATTERN) - Dip Avcısı
def strategy_sfp(df_15m):
    try:
        last = df_15m.iloc[-1]
        # 24 Saatlik Dip (96 Mum)
        low_24h = df_15m['low'].iloc[-97:-1].min()
        
        # Şartlar:
        # 1. 24 Saatlik dibin altına iğne attı (Stop Patlatma)
        # 2. Mum kapanışı tekrar o dibin üzerine çıktı (Reclaim)
        swept = last['low'] < low_24h
        reclaimed = last['close'] > low_24h
        
        # 3. Hacim Teyidi: Hacim ortalamanın 2 katı olmalı (Balina Hareketi)
        vol_ma = df_15m['volume'].rolling(20).mean().iloc[-1]
        vol_spike = last['volume'] > (vol_ma * 2.0)
        
        # 4. RSI Dipte Olmalı (Ucuzluk Teyidi)
        rsi = ta.rsi(df_15m['close'], length=14).iloc[-1]
        rsi_cheap = rsi < 40

        if swept and reclaimed and vol_spike and rsi_cheap:
            return True, "🦅 DİP DÖNÜŞÜ (SFP)", f"24h Dip ({low_24h:.4f}) Hacimli Süpürüldü"
        return False, None, None
    except: return False, None, None

# B. MOMENTUM & TREND (Trend Takipçisi)
def strategy_momentum(df_15m):
    try:
        # WaveTrend
        ap = (df_15m['high'] + df_15m['low'] + df_15m['close']) / 3
        esa = ta.ema(ap, 10)
        d = ta.ema(abs(ap - esa), 10)
        ci = (ap - esa) / (0.015 * d)
        wt1 = ta.ema(ci, 21)
        wt2 = ta.sma(wt1, 4)
        
        # MFI (Para Girişi)
        mfi = ta.mfi(df_15m['high'], df_15m['low'], df_15m['close'], df_15m['volume'], length=14).iloc[-1]
        
        # SuperTrend
        st = ta.supertrend(df_15m['high'], df_15m['low'], df_15m['close'], length=10, multiplier=3)
        st_dir = st[st.columns[1]].iloc[-1] # 1=Up, -1=Down

        # Şartlar:
        # 1. WaveTrend AL vermiş (Kesişim)
        wt_cross = (wt1.iloc[-2] < wt2.iloc[-2]) and (wt1.iloc[-1] > wt2.iloc[-1])
        # 2. WaveTrend tepede değil (Güvenli bölge)
        wt_safe = wt1.iloc[-1] < 55
        # 3. Güçlü Para Girişi Var (MFI > 55)
        mfi_strong = mfi > 55
        # 4. Trend Yönü Yukarı (SuperTrend Yeşil)
        trend_up = st_dir == 1

        if wt_cross and wt_safe and mfi_strong and trend_up:
            return True, "🚀 TREND MOMENTUM", f"WT Sinyali + Para Girişi (MFI:{int(mfi)})"
        return False, None, None
    except: return False, None, None

# C. RSI UYUMSUZLUK (Gizli Balina Alımı)
def strategy_divergence(df_15m):
    try:
        df_15m['rsi'] = ta.rsi(df_15m['close'], length=14)
        last = df_15m.iloc[-1]
        
        # Son 30 mumda dip arama
        window = 30
        prev_low = df_15m['low'].iloc[-window-1:-1].min()
        prev_low_idx = df_15m['low'].iloc[-window-1:-1].idxmin()
        prev_rsi = df_15m.loc[prev_low_idx]['rsi']
        
        # Şartlar:
        # 1. Fiyat yeni dip yaptı (Lower Low)
        # 2. RSI yeni dip yapmadı, yükseldi (Higher Low)
        price_lower = last['low'] < prev_low
        rsi_higher = last['rsi'] > prev_rsi
        # 3. RSI 35'in altında (Aşırı Satım Bölgesi)
        rsi_oversold = last['rsi'] < 35
        # 4. Yeşil mum kapattı (Dönüş başladı)
        green_candle = last['close'] > last['open']

        if price_lower and rsi_higher and rsi_oversold and green_candle:
            return True, "🐂 POZİTİF UYUMSUZLUK", "Fiyat düşerken RSI yükseliyor (Güç Toplama)"
        return False, None, None
    except: return False, None, None

# --- 5. ANA BEYİN (ORCHESTRATOR) ---

def run_analysis():
    tr_time = datetime.now() + timedelta(hours=3)
    print(f"\n🔎 [ANALİZ] Saat: {tr_time.strftime('%H:%M')} | Trader Mantığı Devrede")
    
    symbols = get_tradable_symbols()
    
    # Hafıza Temizliği
    current_time = datetime.now()
    to_remove = [sym for sym, t in signal_history.items() if (current_time - t) > timedelta(minutes=COOLDOWN_MINUTES)]
    for sym in to_remove: del signal_history[sym]

    for symbol in symbols:
        try:
            if symbol in signal_history: continue

            # ADIM 1: 15 DAKİKALIKTA FIRSAT ARA (Scouting)
            df_15m = get_data(symbol, '15m', limit=150)
            if df_15m is None: continue

            signal_type = None
            strategy_name = ""
            reason_desc = ""

            # Stratejileri Kontrol Et
            is_sfp, s_name, s_desc = strategy_sfp(df_15m)
            if is_sfp:
                signal_type = "SFP"; strategy_name = s_name; reason_desc = s_desc
            else:
                is_mom, m_name, m_desc = strategy_momentum(df_15m)
                if is_mom:
                    signal_type = "MOM"; strategy_name = m_name; reason_desc = m_desc
                else:
                    is_div, d_name, d_desc = strategy_divergence(df_15m)
                    if is_div:
                        signal_type = "DIV"; strategy_name = d_name; reason_desc = d_desc
            
            # Sinyal yoksa geç
            if not signal_type: continue

            # ADIM 2: 4 SAATLİK ONAY (Confirmation)
            # 15dk'lık sinyal var ama 4 saatlik izin veriyor mu?
            print(f"⏳ {symbol} potansiyel ({signal_type}). 4H Trendine bakılıyor...")
            trend_status, is_safe = check_4h_trend(symbol)

            if not is_safe:
                print(f"❌ {symbol} REDDEDİLDİ. Sebep: {trend_status}")
                continue # Ana trend aşağı, 15dk sinyalini çöpe at.

            # ADIM 3: İŞLEM HESAPLAMALARI (Execution)
            # ATR bazlı dinamik stop
            atr = ta.atr(df_15m['high'], df_15m['low'], df_15m['close'], length=14).iloc[-1]
            last_price = df_15m['close'].iloc[-1]
            
            # Stop: Fiyatın 2 ATR altı (Gürültüden etkilenmez)
            stop_price = last_price - (2 * atr)
            risk_pct = ((last_price - stop_price) / last_price) * 100
            
            # Risk %5'ten büyükse işlem açma (Çok volatil)
            if risk_pct > 5.0:
                print(f"❌ {symbol} RED: Stop çok uzak (%{risk_pct:.2f})")
                continue

            # Hedefler (R/R: 2 ve 4)
            tp1 = last_price + (2 * (last_price - stop_price))
            tp2 = last_price + (4 * (last_price - stop_price))
            potential = ((tp1 - last_price) / last_price) * 100

            # SİNYAL GÖNDER
            signal_history[symbol] = datetime.now()
            signal_time = tr_time.strftime('%d %b %H:%M')

            msg = f"""
<b>{strategy_name}</b>
━━━━━━━━━━━━━━━━━━━━
<b>#{symbol}</b>
━━━━━━━━━━━━━━━━━━━━
📝 <b>KURGU:</b> {reason_desc}
🛡️ <b>TEYİT:</b> {trend_status} (4H Onaylı)

💵 <b>GİRİŞ :</b> <code>{last_price:.4f}</code>
🛑 <b>STOP  :</b> <code>{stop_price:.4f}</code> (Risk: %{risk_pct:.2f})

🎯 <b>HEDEFLER</b>
━━━━━━━━━━━━━━━━━━━━
1️⃣ Hedef (2R): <code>{tp1:.4f}</code>
2️⃣ Hedef (4R): <code>{tp2:.4f}</code>
Potansiyel: <b>%{potential:.2f}</b>
"""
            try:
                requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", 
                            json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"})
                print(f"✅ SİNYAL GÖNDERİLDİ: {symbol}")
            except Exception as e:
                print(f"Telegram Hatası: {e}")

        except Exception as e:
            continue

    print("🏁 Tarama Bitti.")
    gc.collect()

if __name__ == "__main__":
    scheduler = BackgroundScheduler()
    scheduler.add_job(func=run_analysis, trigger="interval", minutes=5)
    scheduler.start()
    print("🚀 BOT BAŞLATILDI (4H TEYİTLİ PROFESYONEL SİSTEM).")

    def ilk_tarama():
        time.sleep(10)
        run_analysis()

    threading.Thread(target=ilk_tarama).start()

    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port, use_reloader=False)
