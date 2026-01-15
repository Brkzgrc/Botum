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
    return "🚀 Sniper Bot (ATR SFP MODU) Aktif!"

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

# --- 3. PİYASA REJİMİ ANALİZİ ---
def get_macro_regime(symbol):
    try:
        df_1h = get_data(symbol, '1h', limit=100)
        if df_1h is None: return "NEUTRAL", df_1h

        ema50 = ta.ema(df_1h['close'], length=50).iloc[-1]
        ema200 = ta.ema(df_1h['close'], length=200).iloc[-1]
        adx = ta.adx(df_1h['high'], df_1h['low'], df_1h['close'])['ADX_14'].iloc[-1]
        
        bb = ta.bbands(df_1h['close'], length=20, std=2)
        bb_width = (bb['BBU_20_2.0'].iloc[-1] - bb['BBL_20_2.0'].iloc[-1]) / bb['BBM_20_2.0'].iloc[-1]
        close = df_1h['close'].iloc[-1]

        if bb_width < 0.08: return "SQUEEZE", df_1h
        if close > ema50 and close > ema200 and adx > 25: return "UPTREND", df_1h
        if close < ema50 and close < ema200 and adx > 25: return "DOWNTREND", df_1h
            
        return "RANGING", df_1h
    except: return "NEUTRAL", None

# --- 4. HEDEF BELİRLEME ---
def find_structural_target(df_15m, entry_price):
    try:
        lookback = 50
        past_highs = df_15m['high'].iloc[-lookback:-1]
        swing_high = past_highs.max()
        
        if swing_high <= entry_price * 1.005:
            swing_high = entry_price * 1.03
            
        tp_pct = ((swing_high - entry_price) / entry_price) * 100
        return swing_high, tp_pct
    except: return entry_price * 1.03, 3.0

# --- 5. STRATEJİLER (GÜNCELLENMİŞ SFP) ---

# A. ATR DESTEKLİ GELİŞMİŞ SFP (ALTIN DENGE)
def strategy_sfp_pro(df_15m):
    try:
        last = df_15m.iloc[-1]
        
        # 1. Pivot Tespiti (Yine de bir referans lazım)
        scan_window = 50
        past_window = df_15m.iloc[-scan_window:-1]
        pivot_low = past_window['low'].min()
        
        # 2. ATR Hesapla (Esneklik için)
        atr = ta.atr(df_15m['high'], df_15m['low'], df_15m['close'], length=14).iloc[-1]
        
        # 3. YENİ MANTIK: Bölge Tanımları
        # Dip Bölgesi (Güvenli Liman): Pivotun biraz üstü
        dip_zone = pivot_low + (0.25 * atr) 
        # Süpürme Sınırı (Tuzak): Pivotun biraz altı (Bu kadar inmesi lazım)
        sweep_limit = pivot_low - (0.15 * atr)
        
        # 4. Şartlar
        # a) Yeterince derine indi mi? (Tuzak kuruldu mu?)
        swept = last['low'] < sweep_limit
        
        # b) Güvenli bölgeye geri döndü mü? (Teyit)
        reclaimed = last['close'] > dip_zone
        
        # c) Wick (İğne) Kalitesi (1.6'ya çekildi - Daha yumuşak)
        body = abs(last['close'] - last['open'])
        lower_wick = min(last['close'], last['open']) - last['low']
        
        # Gövde 0 ise (Doji) hata vermesin
        if body == 0: strong_wick = True 
        else: strong_wick = lower_wick > (body * 1.6)
        
        # d) Hacim Teyidi
        vol_ma = df_15m['volume'].rolling(20).mean().iloc[-1]
        vol_ok = last['volume'] > (vol_ma * 1.5)

        if swept and reclaimed and strong_wick and vol_ok:
            # STOP Mantığı: Sweep'in biraz altı (Rahat Stop)
            safe_stop = pivot_low - (0.3 * atr)
            
            return True, {
                'type': '🦅 SFP (ATR BÖLGESEL)',
                'desc': f'Pivot ({pivot_low:.4f}) Bölgesi Süpürüldü ve Toplandı',
                'stop': safe_stop
            }
        return False, None
    except Exception as e: 
        # print(f"SFP Hata: {e}") 
        return False, None

# B. MOMENTUM PULLBACK
def strategy_pullback(df_15m):
    try:
        ema50 = ta.ema(df_15m['close'], length=50).iloc[-1]
        ema200 = ta.ema(df_15m['close'], length=200).iloc[-1]
        last = df_15m.iloc[-1]

        if not (ema50 > ema200): return False, None
        
        touched_ema = last['low'] <= ema50 * 1.002 
        bounced = last['close'] > last['open'] and last['close'] > ema50
        
        rsi = ta.rsi(df_15m['close'], length=14).iloc[-1]
        not_overbought = rsi < 65

        if touched_ema and bounced and not_overbought:
            return True, {
                'type': '🚀 EMA PULLBACK',
                'desc': 'Trende Geri Çekilme Girişi',
                'stop': last['low']
            }
        return False, None
    except: return False, None

# C. SQUEEZE BREAKOUT
def strategy_breakout(df_15m):
    try:
        last = df_15m.iloc[-1]
        bb = ta.bbands(df_15m['close'], length=20, std=2)
        upper_band = bb['BBU_20_2.0'].iloc[-1]
        
        breakout = last['close'] > upper_band
        vol_ma = df_15m['volume'].rolling(20).mean().iloc[-1]
        vol_explosion = last['volume'] > (vol_ma * 3.0)
        
        if breakout and vol_explosion:
            return True, {
                'type': '💥 SQUEEZE BREAKOUT',
                'desc': 'Sıkışma Sonrası Hacimli Patlama',
                'stop': df_15m['low'].iloc[-3:].min()
            }
        return False, None
    except: return False, None

# --- 6. ANA BEYİN ---
def run_analysis():
    tr_time = datetime.now() + timedelta(hours=3)
    print(f"\n🔎 [TARAMA] Saat: {tr_time.strftime('%H:%M')} | ATR Destekli SFP")
    
    symbols = get_tradable_symbols()
    
    current_time = datetime.now()
    to_remove = [sym for sym, t in signal_history.items() if (current_time - t) > timedelta(minutes=COOLDOWN_MINUTES)]
    for sym in to_remove: del signal_history[sym]

    for symbol in symbols:
        try:
            if symbol in signal_history: continue

            regime, df_1h = get_macro_regime(symbol)
            if regime == "DOWNTREND": continue 
            
            df_15m = get_data(symbol, '15m', limit=100)
            if df_15m is None: continue

            signal_found = False
            data = {}

            # STRATEJİ SEÇİMİ (REJİME GÖRE)
            if regime == "RANGING" or regime == "NEUTRAL":
                is_sfp, sfp_data = strategy_sfp_pro(df_15m)
                if is_sfp:
                    signal_found = True; data = sfp_data

            elif regime == "UPTREND":
                is_pb, pb_data = strategy_pullback(df_15m)
                if is_pb:
                    signal_found = True; data = pb_data

            elif regime == "SQUEEZE":
                is_brk, brk_data = strategy_breakout(df_15m)
                if is_brk:
                    signal_found = True; data = brk_data
            
            if signal_found:
                signal_history[symbol] = datetime.now()
                entry_price = df_15m['close'].iloc[-1]
                
                tp_price, tp_pct = find_structural_target(df_15m, entry_price)
                
                # Stop Botun hesapladığı stop (ATR ile düzeltilmiş)
                stop_price = data['stop']
                risk_pct = ((entry_price - stop_price) / entry_price) * 100
                
                if tp_pct < risk_pct:
                    print(f"❌ {symbol} RED: Risk > Hedef")
                    continue

                msg = f"""
<b>{data['type']}</b>
━━━━━━━━━━━━━━━━━━━━
<b>#{symbol}</b>
━━━━━━━━━━━━━━━━━━━━
🧠 <b>BAĞLAM:</b> {regime}
📝 <b>NEDEN:</b> {data['desc']}

💵 <b>GİRİŞ :</b> <code>{entry_price:.4f}</code>
🛡️ <b>STOP  :</b> <code>{stop_price:.4f}</code> (Risk: %{risk_pct:.2f})

🎯 <b>HEDEF (Swing High)</b>
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
    print("🚀 BOT BAŞLATILDI (ATR SFP + REJİM FİLTRESİ).")

    def ilk_tarama():
        time.sleep(10)
        run_analysis()

    threading.Thread(target=ilk_tarama).start()

    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port, use_reloader=False)
