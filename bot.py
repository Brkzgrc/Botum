import ccxt
import pandas as pd
import pandas_ta as ta
import numpy as np
import time
import os
import requests
import sys
import gc 
from flask import Flask
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler

# --- 1. AYARLAR ---
API_KEY = os.getenv('BINANCE_API_KEY')
API_SECRET = os.getenv('BINANCE_SECRET_KEY')
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

# %0.30 altı volatiliteye sahip coinleri alma (Ölü coin filtresi)
MIN_ATR_PCT = 0.003 
COOLDOWN_MINUTES = 120 

IGNORED_COINS = [
    'UP/USDT', 'DOWN/USDT', 'BEAR/USDT', 'BULL/USDT',
    'USDC/USDT', 'TUSD/USDT', 'FDUSD/USDT', 'DAI/USDT', 'USDP/USDT',
    'EUR/USDT', 'TRY/USDT', 'GBP/USDT', 'BUSD/USDT', 'USTC/USDT',
    'PAXG/USDT', 'WBTC/USDT', 'USDE/USDT', 'BRL/USDT', 'RUB/USDT',
    'AUD/USDT', 'UST/USDT', 'USD/USDT', 'XUSD/USDT', 'USD1/USDT',
]

# --- MACRO REGIME CACHE ---
macro_cache = {}
MACRO_TTL = timedelta(minutes=30)

# Loglar anında aksın
sys.stdout.reconfigure(line_buffering=True)

# --- 2. BAĞLANTI AYARLARI ---
exchange = ccxt.binance({
    'apiKey': API_KEY,
    'secret': API_SECRET,
    'options': {'defaultType': 'spot'},
    'enableRateLimit': True,
    'timeout': 15000 
})

app = Flask(__name__)
signal_history = {} 

@app.route('/')
def home():
    return "🚀 Sniper Bot (SFP A/B + SIMPLE RE-ACCUMULATION) Çalışıyor..."

# --- 3. VERİ İŞLEMLERİ ---
def get_tradable_symbols():
    try:
        exchange.load_markets()
        symbols = [s for s in exchange.markets if s.endswith('/USDT') 
                   and exchange.markets[s]['active'] 
                   and not any(i in s for i in IGNORED_COINS)]
        return symbols
    except Exception as e:
        print(f"Sembol Listesi Hatası: {e}")
        return []

def get_data(symbol, timeframe, limit=200):
    try:
        time.sleep(0.15)
        bars = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        df = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df.set_index('timestamp', inplace=True)
        return df
    except: return None

# --- 4. HESAPLAMA & İNDİKATÖR HAZIRLIĞI ---
def prepare_indicators(df):
    try:
        # EMA'lar
        df['ema50'] = ta.ema(df['close'], length=50)
        df['ema200'] = ta.ema(df['close'], length=200)
        
        # ATR (SFP ve Hedef için)
        df['atr'] = ta.atr(df['high'], df['low'], df['close'], length=14)
        # ATR Rolling Mean (Volatilite tespiti için)
        df['atr_mean'] = df['atr'].rolling(100, min_periods=50).mean()
        
        # RSI (Pullback ve Divergence için)
        df['rsi'] = ta.rsi(df['close'], length=14)
        
        # Bollinger (Squeeze için)
        bb = ta.bbands(df['close'], length=20, std=2)
        df['upper_band'] = bb['BBU_20_2.0']
        
        # Hacim Ortalaması
        df['vol_ma'] = df['volume'].rolling(20).mean()
        
        return df
    except:
        return df

def get_macro_regime(symbol):
    try:
        now = datetime.utcnow()

        # CACHE KONTROL
        if symbol in macro_cache:
            regime, ts = macro_cache[symbol]
            if now - ts < MACRO_TTL:
                return regime, None

        # CACHE YOKSA 1H VERİ ÇEK
        df_1h = get_data(symbol, '1h', limit=100)
        if df_1h is None:
            return "NEUTRAL", None

        ema50 = ta.ema(df_1h['close'], length=50).iloc[-1]
        ema200 = ta.ema(df_1h['close'], length=200).iloc[-1]
        adx = ta.adx(df_1h['high'], df_1h['low'], df_1h['close'])['ADX_14'].iloc[-1]

        bb = ta.bbands(df_1h['close'], length=20, std=2)
        bb_width = (bb['BBU_20_2.0'].iloc[-1] - bb['BBL_20_2.0'].iloc[-1]) / bb['BBM_20_2.0'].iloc[-1]
        close = df_1h['close'].iloc[-1]

        if bb_width < 0.08:
            regime = "SQUEEZE"
        elif close > ema50 and close > ema200 and adx > 25:
            regime = "UPTREND"
        elif close < ema50 and close < ema200 and adx > 25:
            regime = "DOWNTREND"
        else:
            regime = "RANGING"

        # CACHE’E YAZ
        macro_cache[symbol] = (regime, now)
        return regime, df_1h

    except:
        return "NEUTRAL", None

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
        
        # Volatil coinlerde hedefi uzat
        if "VOLATILE" in coin_type:
            atr = df_15m['atr'].iloc[-1]
            atr_target = entry_price + (4.0 * atr)
            if atr_target > structural_target:
                final_target = atr_target

        tp_pct = ((final_target - entry_price) / entry_price) * 100
        return final_target, tp_pct
    except: 
        return entry_price * 1.03, 3.0

# --- 5. STRATEJİLER (GELİŞMİŞ VE SADELEŞTİRİLMİŞ) ---

# A. SFP: GOLD (A) ve SILVER (B) KALİTE AYRIMI (BU KALIYOR)
def strategy_sfp_dynamic(df_15m):
    try:
        if len(df_15m) < 60: return False, None
        last = df_15m.iloc[-1]

        # 1. EMA Eğim Kontrolü
        ema_now = df_15m['ema50'].iloc[-1]
        ema_prev = df_15m['ema50'].iloc[-5]
        if pd.isna(ema_now) or pd.isna(ema_prev): return False, None
        
        ema_slope_negative = ema_now < ema_prev
        ema_slope_positive = ema_now >= ema_prev
        
        if ema_slope_negative: return False, None 
        
        # 2. Pivot ve Veri Hazırlığı
        scan_window = 50
        past_window = df_15m.iloc[-scan_window:-1]
        pivot_idx = past_window['low'].idxmin() 
        pivot_low = past_window.loc[pivot_idx]['low']
        pivot_rsi = df_15m.loc[pivot_idx]['rsi']
        
        atr_now = last['atr']
        atr_mean = last['atr_mean']
        
        if pd.isna(atr_mean) or atr_mean == 0: vol_ratio = 1.0 
        else: vol_ratio = atr_now / atr_mean
            
        coin_type_tag = "NORMAL"
        sweep_mult = 0.15; reclaim_mult = 0.25; stop_mult = 0.30; wick_mult = 1.5 

        if vol_ratio >= 1.25:
            coin_type_tag = "🔥 VOLATILE"
            sweep_mult = 0.25; reclaim_mult = 0.35; stop_mult = 0.45; wick_mult = 1.8 
        elif vol_ratio <= 0.85:
            coin_type_tag = "🧊 CALM"
            sweep_mult = 0.10; reclaim_mult = 0.20; stop_mult = 0.25; wick_mult = 1.5
            
        # 3. SFP Ana Şartları
        sweep_limit = pivot_low - (sweep_mult * atr_now) 
        dip_zone = pivot_low + (reclaim_mult * atr_now)  
        
        swept = last['low'] < sweep_limit
        reclaimed = last['close'] > dip_zone
        
        body = abs(last['close'] - last['open'])
        lower_wick = min(last['close'], last['open']) - last['low']
        
        if body == 0: strong_wick = True
        else: strong_wick = lower_wick > (body * wick_mult)
        
        vol_ok = last['volume'] > (last['vol_ma'] * 1.5)

        if swept and reclaimed and strong_wick and vol_ok:
            safe_stop = pivot_low - (stop_mult * atr_now)
            
            # --- KALİTE KONTROLÜ (A vs B) ---
            current_rsi = last['rsi']
            has_divergence = current_rsi > pivot_rsi
            is_gold = has_divergence and ema_slope_positive
            
            if is_gold:
                final_type = f"🟢 SFP-A (GOLD) | {coin_type_tag}"
                desc = "Mükemmel Sinyal: Dip Süpürme + RSI Uyumsuzluğu + Trend Yönü"
            else:
                final_type = f"🟡 SFP-B (SILVER) | {coin_type_tag}"
                desc = "Standart SFP: Dip Süpürme (Uyumsuzluk Yok/Zayıf)"

            return True, {
                'type': final_type,
                'desc': desc,
                'stop': safe_stop,
                'coin_type': coin_type_tag
            }
        return False, None
    except: return False, None

# B. PULLBACK (DEĞİŞMEDİ)
def strategy_pullback(df_15m):
    try:
        last = df_15m.iloc[-1]
        ema50 = last['ema50']
        ema200 = last['ema200']
        rsi = last['rsi']

        if pd.isna(ema50) or pd.isna(ema200): return False, None
        if not (ema50 > ema200): return False, None
        
        touched_ema = last['low'] <= ema50 * 1.002 
        bounced = last['close'] > last['open'] and last['close'] > ema50
        not_overbought = rsi < 65

        if touched_ema and bounced and not_overbought:
            return True, {
                'type': '🚀 EMA PULLBACK',
                'desc': 'Trende Geri Çekilme (Güvenli Giriş)',
                'stop': last['low'],
                'coin_type': 'NORMAL'
            }
        return False, None
    except: return False, None

# C. RE-ACCUMULATION (SEÇENEK A: TEK TİP & SADE)
# Sadece ATR Daralması <= 0.8 şartı var.
def strategy_reaccumulation(df_15m):
    try:
        last = df_15m.iloc[-1]
        ema50 = last['ema50']
        atr = last['atr']
        atr_mean = last['atr_mean']

        # 1. Trend Yukarı Olmalı
        if last['close'] < ema50: return False, None

        # 2. Süre Filtresi: En az 12 mumluk bir konsolidasyon (Range)
        lookback = 12
        recent_window = df_15m.iloc[-lookback:-1]
        
        recent_high = recent_window['high'].max()
        recent_low = recent_window['low'].min()
        range_height = recent_high - recent_low
        
        if range_height > (4.0 * atr): return False, None

        # 3. ATR Daralması (FIRTINA ÖNCESİ SESSİZLİK)
        # Sadece bu basit filtreye bakıyoruz. A/B yok.
        if pd.isna(atr_mean) or atr_mean == 0: return False, None
        
        contraction_ratio = atr / atr_mean
        
        # Eğer oran 0.8'den büyükse (yani %20 bile daralma yoksa) ÇÖP'tür.
        if contraction_ratio > 0.8: return False, None

        # 4. Kırılım
        breakout_level = recent_high + (0.1 * atr)
        breakout = last['close'] > breakout_level
        
        # 5. Güçlü Mum
        body = abs(last['close'] - last['open'])
        upper_wick = last['high'] - last['close']
        strong_candle = (last['close'] > last['open']) and (body > upper_wick)
        
        vol_ok = last['volume'] > last['vol_ma']

        if breakout and strong_candle and vol_ok:
            mid_point = (recent_high + recent_low) / 2
            tight_stop = mid_point - (0.5 * atr)
            
            return True, {
                'type': '🚩 RE-ACCUMULATION (PRO)', # Tek etiket
                'desc': f'Trend İçi Bayrak Kırılımı. Sıkışma Oranı: {contraction_ratio:.2f}',
                'stop': tight_stop,
                'coin_type': 'TREND'
            }
        return False, None
    except: return False, None

# D. SQUEEZE BREAKOUT (DEĞİŞMEDİ)
def strategy_breakout(df_15m):
    try:
        last = df_15m.iloc[-1]
        upper_band = last['upper_band']
        vol_ma = last['vol_ma']
        
        if pd.isna(upper_band): return False, None

        breakout = last['close'] > upper_band
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

# --- 6. ANA DÖNGÜ ---
def run_analysis():
    # ZAMAN: UTC (Render) -> UTC+3 (Türkiye)
    utc_now = datetime.utcnow()
    tr_time = utc_now + timedelta(hours=3)
    print(f"\n🔎 [TARAMA] Başlıyor... {tr_time.strftime('%H:%M')} TR")
    
    symbols = get_tradable_symbols()
    
    # Hafıza Temizliği
    to_remove = [sym for sym, t in signal_history.items() if (utc_now - t) > timedelta(minutes=COOLDOWN_MINUTES)]
    for sym in to_remove: del signal_history[sym]

    for symbol in symbols:
        try:
            if symbol in signal_history: continue

            regime, df_1h = get_macro_regime(symbol)
            if regime == "DOWNTREND": continue 
            
            df_15m = get_data(symbol, '15m', limit=200)
            if df_15m is None: continue

            # --- OPTİMİZASYON: İndikatörleri ÖNCE hesapla ---
            df_15m = prepare_indicators(df_15m)
            
            # --- ATR TABANLI ELEME ---
            last = df_15m.iloc[-1]
            if pd.isna(last['atr']) or last['close'] == 0: continue
            
            atr_pct = last['atr'] / last['close']
            if atr_pct < MIN_ATR_PCT: continue 

            signal_found = False
            data = {}

            # --- STRATEJİ SEÇİMİ ---
            
            # 1. Yatay Piyasa -> SFP Ara
            if regime == "RANGING" or regime == "NEUTRAL":
                is_sfp, sfp_data = strategy_sfp_dynamic(df_15m)
                if is_sfp: signal_found = True; data = sfp_data

            # 2. Yükseliş Trendi -> Pullback VEYA Re-accumulation Ara
            elif regime == "UPTREND":
                is_pb, pb_data = strategy_pullback(df_15m)
                if is_pb: 
                    signal_found = True; data = pb_data
                else:
                    # Pullback yoksa, trend devam (Re-accumulation) bak
                    is_re, re_data = strategy_reaccumulation(df_15m)
                    if is_re:
                        signal_found = True; data = re_data

            # 3. Sıkışma -> Patlama Ara
            elif regime == "SQUEEZE":
                is_brk, brk_data = strategy_breakout(df_15m)
                if is_brk: signal_found = True; data = brk_data
            
            if signal_found:
                signal_history[symbol] = utc_now
                entry_price = df_15m['close'].iloc[-1]
                
                coin_type_info = data.get('coin_type', 'NORMAL')
                tp_price, tp_pct = find_structural_target(df_15m, entry_price, coin_type_info)
                stop_price = data['stop']
                risk_pct = ((entry_price - stop_price) / entry_price) * 100
                
                if tp_pct < risk_pct:
                    print(f"❌ {symbol} RED: Risk > Hedef", flush=True)
                    continue
                
                signal_time_str = tr_time.strftime('%H:%M')

                msg = f"""
<b>{data['type']}</b>
━━━━━━━━━━━━━━━━━━━━
<b>#{symbol}</b> | 🕒 {signal_time_str}
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
            continue

    print("🏁 Tarama Bitti.", flush=True)
    gc.collect()

# --- 7. BAŞLATICI ---
if __name__ == "__main__":
    scheduler = BackgroundScheduler()
    scheduler.add_job(func=run_analysis, trigger="interval", minutes=5, max_instances=1, coalesce=True)
    scheduler.start()
    
    start_time = datetime.utcnow() + timedelta(hours=3)
    print(f"🚀 BOT BAŞLATILDI (SFP GOLD/SILVER + SIMPLE RE-ACCUMULATION). Saat: {start_time.strftime('%H:%M')}", flush=True)

    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port, use_reloader=False)
