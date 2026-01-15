import ccxt
import pandas as pd
import pandas_ta as ta
import numpy as np
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

COOLDOWN_MINUTES = 120 

IGNORED_COINS = [
    'UP/USDT', 'DOWN/USDT', 'BEAR/USDT', 'BULL/USDT',
    'USDC/USDT', 'TUSD/USDT', 'FDUSD/USDT', 'DAI/USDT', 'USDP/USDT',
    'EUR/USDT', 'TRY/USDT', 'GBP/USDT', 'BUSD/USDT', 'USTC/USDT',
    'PAXG/USDT', 'WBTC/USDT', 'USDE/USDT', 'BRL/USDT', 'RUB/USDT',
    'AUD/USDT', 'UST/USDT', 'USD/USDT', 'XUSD/USDT', 'USD1/USDT',
]

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
    return "🚀 Sniper Bot (PRICE ACTION MODU) Aktif!"

# --- 2. VERİ ÇEKME ---

def get_tradable_symbols():
    try:
        exchange.load_markets()
        symbols = [s for s in exchange.markets if s.endswith('/USDT') 
                   and exchange.markets[s]['active'] 
                   and not any(i in s for i in IGNORED_COINS)]
        return symbols
    except: return []

def get_data(symbol, timeframe, limit=150):
    try:
        time.sleep(0.1) 
        bars = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        df = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df.set_index('timestamp', inplace=True)
        return df
    except: return None

# --- 3. PİYASA REJİMİ ANALİZİ (1 SAATLİK YÖNETİCİ) ---
# Havanın durumuna balkondan (1H) bakar.
def get_macro_regime(symbol):
    try:
        df_1h = get_data(symbol, '1h', limit=100)
        if df_1h is None: return "NEUTRAL", df_1h

        # EMA Kontrolü
        ema50 = ta.ema(df_1h['close'], length=50).iloc[-1]
        ema200 = ta.ema(df_1h['close'], length=200).iloc[-1]
        
        # ADX (Trend Gücü)
        adx = ta.adx(df_1h['high'], df_1h['low'], df_1h['close'])['ADX_14'].iloc[-1]
        
        # Bollinger Bant Genişliği (Sıkışma Kontrolü)
        bb = ta.bbands(df_1h['close'], length=20, std=2)
        bb_width = (bb['BBU_20_2.0'].iloc[-1] - bb['BBL_20_2.0'].iloc[-1]) / bb['BBM_20_2.0'].iloc[-1]

        close = df_1h['close'].iloc[-1]

        # REJİM KARARLARI:
        # 1. Sıkışma (Patlama Öncesi Sessizlik)
        if bb_width < 0.08: 
            return "SQUEEZE", df_1h

        # 2. Güçlü Yükseliş Trendi
        if close > ema50 and close > ema200 and adx > 25:
            return "UPTREND", df_1h
        
        # 3. Güçlü Düşüş Trendi (İşlem Yasak - Sadece çok dipse SFP)
        if close < ema50 and close < ema200 and adx > 25:
            return "DOWNTREND", df_1h
            
        # 4. Yatay Piyasa
        return "RANGING", df_1h

    except: return "NEUTRAL", None

# --- 4. HEDEF BELİRLEME (MARKET YAPISINA GÖRE) ---
# Rastgele %3 değil, bir önceki tepeye (Swing High) hedef koyar.
def find_structural_target(df_15m, entry_price):
    try:
        # Son 50 mumdaki en yüksek tepeyi bul (Mevcut mum hariç)
        # Bu, potansiyel dirençtir.
        lookback = 50
        past_highs = df_15m['high'].iloc[-lookback:-1]
        swing_high = past_highs.max()
        
        # Eğer tepe çok yakınsa (%0.5), daha geriye bak
        if swing_high <= entry_price * 1.005:
            swing_high = entry_price * 1.03 # Bulamazsa %3 varsayılan koy
            
        tp_pct = ((swing_high - entry_price) / entry_price) * 100
        return swing_high, tp_pct
    except:
        return entry_price * 1.03, 3.0

# --- 5. STRATEJİLER (15 DAKİKALIK İŞÇİLER) ---

# A. GELİŞMİŞ SFP (LİKİDİTE AVLI)
# Sadece rastgele dibi değil, "Fraktal Dibi" temizleyenleri bulur.
def strategy_sfp_pro(df_15m):
    try:
        last = df_15m.iloc[-1]
        
        # 1. Adım: Geçmişteki ÖNEMLİ dipleri bul (Pivot Lows)
        # Basit min() yerine, etrafı yüksek olan dipleri arıyoruz.
        # Son 50 mumda, en düşük dip.
        scan_window = 50
        past_window = df_15m.iloc[-scan_window:-1]
        pivot_low = past_window['low'].min()
        
        # 2. Adım: Sweep (Süpürme) Kontrolü
        # Fiyat bu dibin altına indi mi?
        swept = last['low'] < pivot_low
        
        # 3. Adım: Reclaim (Geri Kazanım)
        # Mum kapanışı tekrar o dibin üzerinde mi?
        reclaimed = last['close'] > pivot_low
        
        # 4. Adım: Wick (İğne) Kalitesi
        # İğne, gövdenin en az 2 katı olmalı.
        body = abs(last['close'] - last['open'])
        lower_wick = min(last['close'], last['open']) - last['low']
        strong_wick = lower_wick > (body * 2.0)
        
        # 5. Adım: Hacim Teyidi
        vol_ma = df_15m['volume'].rolling(20).mean().iloc[-1]
        vol_ok = last['volume'] > (vol_ma * 1.5)

        if swept and reclaimed and strong_wick and vol_ok:
            return True, {
                'type': '🦅 SFP (LİKİDİTE AVI)',
                'desc': f'Önemli Dip ({pivot_low:.4f}) Süpürüldü',
                'stop': last['low']
            }
        return False, None
    except: return False, None

# B. MOMENTUM PULLBACK (TREND KATILIMI)
# Körlemesine değil, EMA'ya geri çekilince alır.
def strategy_pullback(df_15m):
    try:
        # Trend Göstergeleri
        ema50 = ta.ema(df_15m['close'], length=50).iloc[-1]
        ema200 = ta.ema(df_15m['close'], length=200).iloc[-1]
        last = df_15m.iloc[-1]
        prev = df_15m.iloc[-2]

        # 1. Ana Trend Yukarı mı? (15m'de de teyit)
        if not (ema50 > ema200): return False, None
        
        # 2. PULLBACK (Geri Çekilme) Var mı?
        # Fiyat EMA50'ye değdi veya çok yaklaştı, ama altına kalıcı inmedi.
        # Düşük (Low) EMA50'nin altında veya yakınında olmalı.
        touched_ema = last['low'] <= ema50 * 1.002 
        
        # 3. TETİK (Dönüş Mumu)
        # Mum Yeşil kapattı ve EMA50'nin üzerinde kalmayı başardı.
        bounced = last['close'] > last['open'] and last['close'] > ema50
        
        # 4. RSI Kontrolü (Aşırı şişikse girme)
        rsi = ta.rsi(df_15m['close'], length=14).iloc[-1]
        not_overbought = rsi < 65

        if touched_ema and bounced and not_overbought:
            return True, {
                'type': '🚀 EMA PULLBACK (TREND)',
                'desc': 'Trende Geri Çekilme Noktasından Giriş',
                'stop': last['low'] # Stop, dönüş mumunun altı
            }
        return False, None
    except: return False, None

# C. SQUEEZE BREAKOUT (PATLAMA)
# Daralan bantların hacimli kırılımı.
def strategy_breakout(df_15m):
    try:
        last = df_15m.iloc[-1]
        
        # 1. Bant Kontrolü
        bb = ta.bbands(df_15m['close'], length=20, std=2)
        upper_band = bb['BBU_20_2.0'].iloc[-1]
        
        # 2. Kırılım ve Kapanış
        # Fiyat üst bandı kırdı ve orada KAPATTI (Fitil değil)
        breakout = last['close'] > upper_band
        
        # 3. Hacim Teyidi (Z-Score) - ÇOK ÖNEMLİ
        # Hacim ortalamanın 3 katı olmalı (Fakeout yememek için)
        vol_ma = df_15m['volume'].rolling(20).mean().iloc[-1]
        vol_explosion = last['volume'] > (vol_ma * 3.0)
        
        if breakout and vol_explosion:
            return True, {
                'type': '💥 SQUEEZE BREAKOUT',
                'desc': 'Sıkışma Sonrası Hacimli Patlama',
                'stop': df_15m['low'].iloc[-3:].min() # Son 3 mumun dibi stoptur
            }
        return False, None
    except: return False, None


# --- 6. ANA BEYİN (ORCHESTRATOR) ---

def run_analysis():
    tr_time = datetime.now() + timedelta(hours=3)
    print(f"\n🔎 [TARAMA] Saat: {tr_time.strftime('%H:%M')} | Price Action Modu")
    
    symbols = get_tradable_symbols()
    
    current_time = datetime.now()
    to_remove = [sym for sym, t in signal_history.items() if (current_time - t) > timedelta(minutes=COOLDOWN_MINUTES)]
    for sym in to_remove: del signal_history[sym]

    for symbol in symbols:
        try:
            if symbol in signal_history: continue

            # ADIM 1: REJİM BELİRLE (1H Grafik)
            regime, df_1h = get_macro_regime(symbol)
            if regime == "DOWNTREND": continue # Düşüş trendinde işlem yok.
            
            # ADIM 2: 15DK VERİ ÇEK
            df_15m = get_data(symbol, '15m', limit=100)
            if df_15m is None: continue

            signal_found = False
            data = {}

            # ADIM 3: REJİME UYGUN STRATEJİYİ SEÇ
            
            # Senaryo A: YATAY PİYASA (Ranging) -> Sadece SFP Ara
            if regime == "RANGING" or regime == "NEUTRAL":
                is_sfp, sfp_data = strategy_sfp_pro(df_15m)
                if is_sfp:
                    signal_found = True; data = sfp_data

            # Senaryo B: YÜKSELİŞ TRENDİ (Uptrend) -> Pullback Ara
            elif regime == "UPTREND":
                is_pb, pb_data = strategy_pullback(df_15m)
                if is_pb:
                    signal_found = True; data = pb_data

            # Senaryo C: SIKIŞMA (Squeeze) -> Patlama Ara
            elif regime == "SQUEEZE":
                is_brk, brk_data = strategy_breakout(df_15m)
                if is_brk:
                    signal_found = True; data = brk_data
            
            # --- SİNYAL OLUŞUMU ---
            if signal_found:
                signal_history[symbol] = datetime.now()
                entry_price = df_15m['close'].iloc[-1]
                
                # Hedef Belirleme (Market Yapısına Göre)
                tp_price, tp_pct = find_structural_target(df_15m, entry_price)
                
                # Stop Hesapla
                stop_price = data['stop']
                # Eğer stop çok yakınsa (%0.5 altı), ATR ile biraz aç
                if (entry_price - stop_price) / entry_price < 0.005:
                    atr = ta.atr(df_15m['high'], df_15m['low'], df_15m['close'], length=14).iloc[-1]
                    stop_price = entry_price - (1.5 * atr)
                
                risk_pct = ((entry_price - stop_price) / entry_price) * 100
                
                # Risk/Ödül Oranı (RR) Kontrolü
                # Eğer hedef %1, risk %3 ise GİRME.
                if tp_pct < risk_pct:
                    print(f"❌ {symbol} RED: Değmez (Risk: {risk_pct:.2f} > Hedef: {tp_pct:.2f})")
                    continue

                msg = f"""
<b>{data['type']}</b>
━━━━━━━━━━━━━━━━━━━━
<b>#{symbol}</b>
━━━━━━━━━━━━━━━━━━━━
🧠 <b>BAĞLAM:</b> {regime} (1H Analizi)
📝 <b>NEDEN:</b> {data['desc']}

💵 <b>GİRİŞ :</b> <code>{entry_price:.4f}</code>
🛡️ <b>STOP  :</b> <code>{stop_price:.4f}</code> (Risk: %{risk_pct:.2f})

🎯 <b>YAPISAL HEDEF (Swing High)</b>
━━━━━━━━━━━━━━━━━━━━
🏆 Hedef: <code>{tp_price:.4f}</code>
Potansiyel: <b>%{tp_pct:.2f}</b>
"""
                try:
                    requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", 
                                json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"})
                    print(f"✅ SİNYAL: {symbol} | {data['type']}")
                except: pass

        except Exception as e:
            continue

    print("🏁 Tarama Bitti.")
    gc.collect()

if __name__ == "__main__":
    scheduler = BackgroundScheduler()
    scheduler.add_job(func=run_analysis, trigger="interval", minutes=5)
    scheduler.start()
    print("🚀 BOT BAŞLATILDI (PRICE ACTION + REJİM FİLTRESİ).")

    def ilk_tarama():
        time.sleep(10)
        run_analysis()

    threading.Thread(target=ilk_tarama).start()

    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port, use_reloader=False)
