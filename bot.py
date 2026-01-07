import pandas as pd
import numpy as np
import time
import requests
import threading
import os
import sys
from datetime import datetime, timedelta
from flask import Flask

# --- 1. RENDER WEB SUNUCUSU ---
app = Flask(__name__)

@app.route('/')
def home():
    return "🚀 Bot Aktif - 7/24 Sinyal Tarayıcı (Progress Mod)"

# --- 2. AYARLAR ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
EXCLUDED = ['USDC', 'FDUSD', 'TUSD', 'USDP', 'BUSD', 'DAI', 'EUR', 'TRY', 'PAXG', 'WBTC', 'USDE']
sent_signals = {}

# --- 3. YARDIMCI FONKSİYONLAR ---
def get_top_volume_coins(limit=60):
    try:
        url = "https://api1.binance.com/api/v3/ticker/24hr"
        response = requests.get(url, timeout=10)
        if response.status_code != 200: return []
        tickers = response.json()
        valid_coins = []
        for t in tickers:
            symbol = t['symbol']
            if symbol.endswith('USDT'):
                quote_vol = float(t['quoteVolume'])
                if quote_vol > 30000000:
                    base = symbol.replace('USDT', '')
                    if base not in EXCLUDED and "UP" not in symbol and "DOWN" not in symbol:
                        valid_coins.append({'symbol': symbol, 'vol': quote_vol})
        valid_coins.sort(key=lambda x: x['vol'], reverse=True)
        return [x['symbol'] for x in valid_coins[:limit]]
    except:
        return []

def get_data(symbol, interval):
    try:
        url = f"https://api1.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit=100"
        response = requests.get(url, timeout=5)
        r = response.json()
        df = pd.DataFrame(r, columns=['ts','o','h','l','c','v','ct','qa','nt','tb','tq','i'])
        cols = ['o','h','l','c','v','tb']
        df[cols] = df[cols].astype(float)
        
        # --- İNDİKATÖRLER ---
        df['obv'] = (np.sign(df['c'].diff()) * df['v']).fillna(0).cumsum()
        
        delta = df['c'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        rs = gain / (loss + 1e-9)
        df['rsi'] = 100 - (100 / (1 + rs))
        
        df['sma20'] = df['c'].rolling(20).mean()
        df['std20'] = df['c'].rolling(20).std()
        df['upper'] = df['sma20'] + (2 * df['std20'])
        df['lower'] = df['sma20'] - (2 * df['std20'])
        df['bandwidth'] = ((df['upper'] - df['lower']) / df['sma20']) * 100
        
        exp1 = df['c'].ewm(span=12, adjust=False).mean()
        exp2 = df['c'].ewm(span=26, adjust=False).mean()
        df['macd'] = exp1 - exp2
        df['signal'] = df['macd'].ewm(span=9, adjust=False).mean()
        
        df['delta_pct'] = (df['tb'] / df['v']) * 100
        
        df['tr'] = pd.concat([df['h']-df['l'], abs(df['h']-df['c'].shift(1)), abs(df['l']-df['c'].shift(1))], axis=1).max(axis=1)
        df['atr'] = df['tr'].rolling(14).mean()
        
        # ADX
        df['up_move'] = df['h'].diff()
        df['down_move'] = df['l'].diff()
        df['plus_dm'] = np.where((df['up_move'] > df['down_move']) & (df['up_move'] > 0), df['up_move'], 0)
        df['minus_dm'] = np.where((df['down_move'] > df['up_move']) & (df['down_move'] > 0), df['down_move'], 0)
        df['plus_di'] = 100 * (df['plus_dm'].rolling(14).mean() / (df['atr'] + 1e-9))
        df['minus_di'] = 100 * (df['minus_dm'].rolling(14).mean() / (df['atr'] + 1e-9))
        df['dx'] = 100 * abs(df['plus_di'] - df['minus_di']) / (df['plus_di'] + df['minus_di'] + 1e-9)
        df['adx'] = df['dx'].rolling(14).mean()

        recent_high = df['h'].rolling(50).max().iloc[-1]
        recent_low = df['l'].rolling(50).min().iloc[-1]
        df['fib_target'] = recent_high + ((recent_high - recent_low) * 0.618)
        
        # Uyumsuzluk
        df['price_low'] = df['l'].rolling(5).min()
        df['rsi_low'] = df['rsi'].rolling(5).min()
        df['bull_div'] = (df['l'] <= df['price_low']) & (df['rsi'] > df['rsi_low'].shift(1)) & (df['rsi'] < 35)

        return df
    except:
        return None

def get_depth_score(symbol):
    try:
        res = requests.get(f"https://api1.binance.com/api/v3/depth?symbol={symbol}&limit=20", timeout=3).json()
        bids = sum([float(i[0]) * float(i[1]) for i in res['bids']])
        asks = sum([float(i[0]) * float(i[1]) for i in res['asks']])
        if asks == 0: return 1.0
        return bids / asks
    except:
        return 1.0

# --- 4. ANA TARAMA FONKSİYONU ---
def scan_market():
    tr_now = datetime.utcnow() + timedelta(hours=3)
    print(f"▶️ [BAŞLADI] {tr_now.strftime('%H:%M:%S')} (TR)", flush=True)

    btc_df = get_data("BTCUSDT", "15m")
    if btc_df is None: 
        print("⚠️ BTC verisi alınamadı, tekrar deneniyor...", flush=True)
        return

    btc_change = ((btc_df['c'].iloc[-1] - btc_df['c'].iloc[-2]) / btc_df['c'].iloc[-2]) * 100
    if btc_change < -0.8:
        print(f"⚠️ BTC Düşüşte (%{btc_change:.2f}), pas geçiliyor.", flush=True)
        return

    targets = get_top_volume_coins(limit=60)
    print(f"📋 Hedef Listesi: {len(targets)} coin taraniyor...", flush=True)

    # --- PROGRESS BAR (İLERLEME ÇUBUĞU) ---
    for i, s in enumerate(targets):
        # Her 10 coinde bir log bas (Botun donmadığını gör)
        if i % 10 == 0:
            print(f"⏳ Analiz ediliyor... {i}/{len(targets)} ({s})", flush=True)

        try:
            df = get_data(s, "15m")
            if df is None: continue
            
            curr = df.iloc[-1]
            prev = df.iloc[-2]
            
            score = 0
            strategy_name = ""
            risk_lvl = "ORTA"
            
            # 1. Squeeze
            vol_avg = df['v'].rolling(20).mean().iloc[-1]
            is_squeeze = curr['bandwidth'] < 6.0 
            is_breakout = (curr['c'] > curr['upper']) and (curr['v'] > vol_avg * 2.5)
            if is_squeeze and is_breakout:
                score += 8
                strategy_name = "🧨 BARUT FIÇISI"
                risk_lvl = "YÜKSEK"
            
            # 2. Dip Avcısı
            whale_ratio = get_depth_score(s)
            if curr['rsi'] < 30 and whale_ratio > 2.5 and curr['c'] > prev['h']:
                score += 7
                strategy_name = "⚓ DİP BALİNASI"
                risk_lvl = "DÜŞÜK"

            # 3. Momentum
            if curr['macd'] > curr['signal'] and curr['rsi'] > 50 and curr['rsi'] < 70:
                if curr['v'] > vol_avg and curr['adx'] > 25:
                    score += 6
                    strategy_name = "🌊 TREND SÖRFÜ"
                    risk_lvl = "ORTA"

            if curr['delta_pct'] > 60: score += 1
            if btc_change > 0.1: score += 1
            if curr['obv'] > df['obv'].iloc[-5]: score += 1
            
            if score >= 7:
                atr_val = curr['atr']
                stop_price = curr['c'] - (atr_val * 1.5)
                target_price = max(curr['fib_target'], curr['c'] + (atr_val * 4))
                stop_pct = ((stop_price - curr['c']) / curr['c']) * 100
                target_pct = ((target_price - curr['c']) / curr['c']) * 100
                
                if s not in sent_signals or (time.time() - sent_signals[s]) > 10800:
                    msg = (
                        f"⚡ **SİNYAL TESPİT EDİLDİ** ⚡\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"🪙 **{s}** (Spot / 15m)\n"
                        f"📡 **Strateji:** {strategy_name}\n"
                        f"🏆 **Güven Skoru:** {score}/10\n"
                        f"📊 **Risk:** {risk_lvl}\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"🟢 **GİRİŞ:** {curr['c']:.5f}\n"
                        f"🎯 **HEDEF:** {target_price:.5f} (+%{target_pct:.2f})\n"
                        f"🛑 **STOP:** {stop_price:.5f} (%{stop_pct:.2f})\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"💡 **ÖNERİ:** Tek seferde satmak istiyorsun.\n"
                        f"• Fiyat %2 yükselirse Stop'u girişe çek.\n"
                        f"• Hedefe gelince acıma, sat çık.\n"
                        f"• Bekleme Süresi: Max 60 Dakika.\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"🌊 **Delta:** %{curr['delta_pct']:.1f} Alıcılı\n"
                        f"🐋 **Balina Oranı:** Alıcılar x{whale_ratio:.1f} baskın\n"
                        f"📈 **ADX Trend:** {curr['adx']:.1f}\n"
                        f"🔗 [Binance Spot](https://www.binance.com/en/trade/{s}_USDT?type=spot)"
                    )
                    
                    if TELEGRAM_TOKEN and CHAT_ID:
                        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", 
                                      data={'chat_id': CHAT_ID, 'text': msg, 'parse_mode': 'Markdown'})
                        sent_signals[s] = time.time()
                        print(f"✅ SİNYAL GÖNDERİLDİ: {s}")

            time.sleep(0.1) # API için kısa bekleme

        except Exception as e:
            continue

    next_run = tr_now + timedelta(minutes=15)
    print(f"🏁 [BİTTİ] Sonraki Tarama: {next_run.strftime('%H:%M:%S')}", flush=True)
    print("-" * 40, flush=True)

# --- 5. ÖLÜMSÜZ DÖNGÜ ---
def start_loop():
    print("🚀 Bot Başlatılıyor... İlerleme Modu Aktif", flush=True)
    while True:
        try:
            # Buraya ekstra try-except koydum ki döngü ASLA kırılmasın
            scan_market()
        except Exception as e:
            print(f"⚠️ Kritik Döngü Hatası: {e} - Tekrar başlatılıyor...", flush=True)
        
        # 2 Dakika bekle
        time.sleep(900)

# Gunicorn Tetikleyicisi
threading.Thread(target=start_loop, daemon=True).start()

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=8080)
