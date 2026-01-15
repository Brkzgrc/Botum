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

# Loglar anında aksın (Donma var mı görebilmek için)
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

# --- 2. PERFORMANS AYARLI BAĞLANTI ---
# Eski koddaki mantığa döndük: Timeout düşürüldü, RateLimit açık.
exchange = ccxt.binance({
    'apiKey': API_KEY,
    'secret': API_SECRET,
    'options': {'defaultType': 'spot'},
    'enableRateLimit': True,
    'timeout': 15000 # 30sn yerine 15sn yaptık. Cevap vermeyen coini hemen geçsin, beklemesin.
})

app = Flask(__name__)
signal_history = {} 

@app.route('/')
def home():
    return "🚀 Sniper Bot (STABİL VERSİYON) Çalışıyor..."

# --- 3. VERİ ÇEKME (ESKİ KODUN SAĞLAMLIĞI) ---
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
        print(f"Sembol Listesi Hatası: {e}")
        return []

def get_data(symbol, timeframe, limit=200):
    try:
        # ESKİ KODDAKİ GİBİ UYKU: Rate Limit yememek için kritik.
        # Çift işlem yaptığımız için 0.15 yerine 0.20 yaptık.
        time.sleep(0.2) 
        bars = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        df = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df.set_index('timestamp', inplace=True)
        return df
    except: return None

# --- 4. HESAPLAMA MODÜLLERİ (SENİN YENİ MANTIĞIN) ---
# BURALARA DOKUNMADIM. Sadece yapısal olarak sağlamlaştırdım.

def get_macro_regime(symbol):
    try:
        # Rejim için veri çekiyoruz
        df_1h = get_data(symbol, '1h', limit=100)
        if df_1h is None: return "NEUTRAL", None

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

def find_structural_target(df_15m, entry_price, coin_type="NORMAL"):
    try:
        lookback = 50
        past_highs = df_15m['high'].iloc[-lookback:-1]
        swing_high = past_highs.max()
        
        if swing_high <= entry_price * 1.005:
            structural_target = entry_price * 1.03
        else:
            structural_target = swing_high

        final_target = structural_target
        
        if coin_type == "🔥 VOLATILE":
            atr = ta.atr(df_15m['high'], df_15m['low'], df_15m['close'], length=14).iloc[-1]
            atr_target = entry_price + (4.0 * atr)
            if atr_target > structural_target:
                final_target = atr_target

        tp_pct = ((final_target - entry_price) / entry_price) * 100
        return final_target, tp_pct
    except: 
        return entry_price * 1.03, 3.0

# --- 5. STRATEJİLER (YENİ SİNYAL MEKANİZMALARI) ---
# Dokunulmadı, sadece hata koruması (try-except) eklendi.

def strategy_sfp_dynamic(df_15m):
    try:
        if len(df_15m) < 60: return False, None
        last = df_15m.iloc[-1]

        ema50 = ta.ema(df_15m['close'], length=50)
        if ema50 is None: return False, None
        ema_slope_ok = ema50.iloc[-1] >= ema50.iloc[-5] # Stabil Eğim (5 Mum)
        if not ema_slope_ok: return False, None
        
        scan_window = 50
        past_window = df_15m.iloc[-scan_window:-1]
        pivot_low = past_window['low'].min()
        
        atr_series = ta.atr(df_15m['high'], df_15m['low'], df_15m['close'], length=14)
        atr_now = atr_series.iloc[-1]
        atr_mean = atr_series.rolling(100, min_periods=50).mean().iloc[-1]
        
        if pd.isna(atr_mean) or atr_mean == 0: vol_ratio = 1.0 
        else: vol_ratio = atr_now / atr_mean
            
        coin_type = "NORMAL"
        sweep_mult = 0.15; reclaim_mult = 0.25; stop_mult = 0.30; wick_mult = 1.5 

        if vol_ratio >= 1.25:
            coin_type = "🔥 VOLATILE"
            sweep_mult = 0.25; reclaim_mult = 0.35; stop_mult = 0.45; wick_mult = 1.8 
        elif vol_ratio <= 0.85:
            coin_type = "🧊 CALM"
            sweep_mult = 0.10; reclaim_mult = 0.20; stop_mult = 0.25; wick_mult = 1.5
            
        sweep_limit = pivot_low - (sweep_mult * atr_now) 
        dip_zone = pivot_low + (reclaim_mult * atr_now)  
        
        swept = last['low'] < sweep_limit
        reclaimed = last['close'] > dip_zone
        
        body = abs(last['close'] - last['open'])
        lower_wick = min(last['close'], last['open']) - last['low']
        
        if body == 0: strong_wick = True
        else: strong_wick = lower_wick > (body * wick_mult)
        
        vol_ma = df_15m['volume'].rolling(20).mean().iloc[-1]
        vol_ok = last['volume'] > (vol_ma * 1.5)

        if swept and reclaimed and strong_wick and vol_ok:
            safe_stop = pivot_low - (stop_mult * atr_now)
            return True, {
                'type': f'🦅 SFP ({coin_type})',
                'desc': f'Pivot Süpürüldü. Volatilite: {vol_ratio:.2f}',
                'stop': safe_stop,
                'coin_type': coin_type
            }
        return False, None
    except: return False, None

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
                'desc': 'Trende Geri Çekilme',
                'stop': last['low'],
                'coin_type': 'NORMAL'
            }
        return False, None
    except: return False, None

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
                'desc': 'Sıkışma Sonrası Patlama',
                'stop': df_15m['low'].iloc[-3:].min(),
                'coin_type': 'SQUEEZE'
            }
        return False, None
    except: return False, None

# --- 6. ANA DÖNGÜ (ESKİ KODUN STABİL YAPISIYLA) ---
def run_analysis():
    print(f"\n🔎 [TARAMA] Başlıyor... {datetime.now().strftime('%H:%M')}", flush=True)
    
    symbols = get_tradable_symbols()
    
    # Hafıza Temizliği
    current_time = datetime.now()
    to_remove = [sym for sym, t in signal_history.items() if (current_time - t) > timedelta(minutes=COOLDOWN_MINUTES)]
    for sym in to_remove: del signal_history[sym]

    for symbol in symbols:
        try:
            # 1. Soğuma Kontrolü
            if symbol in signal_history: continue

            # 2. Rejim Analizi (Burada 1H verisi çekiyoruz)
            regime, df_1h = get_macro_regime(symbol)
            if regime == "DOWNTREND": continue 
            
            # 3. Sinyal Analizi (Burada 15m verisi çekiyoruz)
            # DİKKAT: Eski kodda olmayan "İkinci Veri Çekimi" burada.
            # O yüzden get_data içindeki sleep süresini artırdık.
            df_15m = get_data(symbol, '15m', limit=200)
            if df_15m is None: continue

            signal_found = False
            data = {}

            # 4. Strateji Seçimi
            if regime == "RANGING" or regime == "NEUTRAL":
                is_sfp, sfp_data = strategy_sfp_dynamic(df_15m)
                if is_sfp: signal_found = True; data = sfp_data

            elif regime == "UPTREND":
                is_pb, pb_data = strategy_pullback(df_15m)
                if is_pb: signal_found = True; data = pb_data

            elif regime == "SQUEEZE":
                is_brk, brk_data = strategy_breakout(df_15m)
                if is_brk: signal_found = True; data = brk_data
            
            # 5. Mesaj Gönderimi
            if signal_found:
                signal_history[symbol] = datetime.now()
                entry_price = df_15m['close'].iloc[-1]
                
                coin_type_info = data.get('coin_type', 'NORMAL')
                tp_price, tp_pct = find_structural_target(df_15m, entry_price, coin_type_info)
                stop_price = data['stop']
                risk_pct = ((entry_price - stop_price) / entry_price) * 100
                
                if tp_pct < risk_pct:
                    print(f"❌ {symbol} RED: Risk > Hedef", flush=True)
                    continue

                msg = f"""
<b>{data['type']}</b>
━━━━━━━━━━━━━━━━━━━━
<b>#{symbol}</b>
━━━━━━━━━━━━━━━━━━━━
🧠 <b>BAĞLAM:</b> {regime} | <b>TİP:</b> {coin_type_info}
📝 <b>NEDEN:</b> {data['desc']}

💵 <b>GİRİŞ :</b> <code>{entry_price:.4f}</code>
🛡️ <b>STOP  :</b> <code>{stop_price:.4f}</code> (Risk: %{risk_pct:.2f})

🎯 <b>HEDEF</b>
━━━━━━━━━━━━━━━━━━━━
🏆 Hedef: <code>{tp_price:.4f}</code>
Potansiyel: <b>%{tp_pct:.2f}</b>
"""
                try:
                    requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", 
                                json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"})
                    print(f"✅ SİNYAL: {symbol} | {data['type']}", flush=True)
                except Exception as e:
                    print(f"Telegram Hatası: {e}", flush=True)

        except Exception as e:
            # Tekil coin hatası botu durdurmasın
            continue

    print("🏁 Tarama Bitti.", flush=True)
    gc.collect()

# --- 7. BAŞLATICI (KİLİTLENME ÖNLEYİCİ İLE) ---
if __name__ == "__main__":
    scheduler = BackgroundScheduler()
    
    # İŞTE ESKİ KODDA OLMAYAN AMA YENİ KOD İÇİN ŞART OLAN KORUMA:
    # max_instances=1: Eğer bir önceki tarama bitmediyse, yenisini başlatma.
    # coalesce=True: Kaçırılan taramaları biriktirme, sadece sonuncuyu yap.
    scheduler.add_job(func=run_analysis, trigger="interval", minutes=5, max_instances=1, coalesce=True)
    
    scheduler.start()
    print("🚀 BOT BAŞLATILDI (STABİLİTE GARANTİLİ).", flush=True)

    def ilk_tarama():
        time.sleep(10)
        run_analysis()

    threading.Thread(target=ilk_tarama).start()

    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port, use_reloader=False)
