import pandas as pd
import numpy as np
import time
import requests
import threading
import os
import sys
from datetime import datetime, timedelta
from flask import Flask

# --- 1. RENDER KESİNTİSİZ ÇALIŞMA AYARI ---
app = Flask('')

@app.route('/')
def home():
    return "🚀 Bot Aktif - 7/24 Sinyal Tarayıcı"

def run_web():
    try:
        app.run(host='0.0.0.0', port=8080)
    except:
        pass

threading.Thread(target=run_web, daemon=True).start()

# --- 2. GÜVENLİK VE AYARLAR ---
# Render Environment Variables kısmına ekle!
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

# Taranmayacaklar
EXCLUDED = ['USDC', 'FDUSD', 'TUSD', 'USDP', 'BUSD', 'DAI', 'EUR', 'TRY', 'PAXG', 'WBTC', 'USDE']
sent_signals = {}

# --- 3. AKILLI HACİM FİLTRESİ ---
def get_top_volume_coins(limit=55):
    """
    Sadece Marketin 'Baba' coinlerini tarar.
    Spot işlemde likidite sorunu yaşamaman için min 30M$ Hacim şartı.
    """
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
                # FİLTRE: Min 30 Milyon Dolar Hacim (Kolay Gir-Çık için)
                if quote_vol > 30000000:
                    base = symbol.replace('USDT', '')
                    if base not in EXCLUDED and "UP" not in symbol and "DOWN" not in symbol:
                        valid_coins.append({'symbol': symbol, 'vol': quote_vol})
        
        # En yüksek hacimlileri seç
        valid_coins.sort(key=lambda x: x['vol'], reverse=True)
        return [x['symbol'] for x in valid_coins[:limit]]
    except:
        return []

def format_volume(vol):
    if vol >= 1000000000: return f"{vol/1000000000:.2f} Milyar $"
    if vol >= 1000000: return f"{vol/1000000:.1f} Milyon $"
    return f"{vol/1000:.0f} Bin $"

# --- 4. TEKNİK ANALİZ MOTORU (FULL DONANIM) ---
def get_data(symbol, interval):
    try:
        url = f"https://api1.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit=100"
        response = requests.get(url, timeout=5)
        r = response.json()
        
        df = pd.DataFrame(r, columns=['ts','o','h','l','c','v','ct','qa','nt','tb','tq','i'])
        cols = ['o','h','l','c','v','tb']
        df[cols] = df[cols].astype(float)
        
        # --- GÖSTERGELER ---
        
        # 1. RSI
        delta = df['c'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        rs = gain / (loss + 1e-9)
        df['rsi'] = 100 - (100 / (1 + rs))
        
        # 2. Bollinger Bantları & Sıkışma (Squeeze)
        df['sma20'] = df['c'].rolling(20).mean()
        df['std20'] = df['c'].rolling(20).std()
        df['upper'] = df['sma20'] + (2 * df['std20'])
        df['lower'] = df['sma20'] - (2 * df['std20'])
        # Bandwidth: Bantlar ne kadar daraldı? (Patlama habercisi)
        df['bandwidth'] = ((df['upper'] - df['lower']) / df['sma20']) * 100
        
        # 3. MACD
        exp1 = df['c'].ewm(span=12, adjust=False).mean()
        exp2 = df['c'].ewm(span=26, adjust=False).mean()
        df['macd'] = exp1 - exp2
        df['signal'] = df['macd'].ewm(span=9, adjust=False).mean()
        
        # 4. Hacim Delta (Alıcı Baskısı)
        df['delta_pct'] = (df['tb'] / df['v']) * 100
        
        # 5. ATR (Volatilite - Stop Loss için)
        df['tr1'] = df['h'] - df['l']
        df['tr2'] = abs(df['h'] - df['c'].shift(1))
        df['tr3'] = abs(df['l'] - df['c'].shift(1))
        df['tr'] = df[['tr1', 'tr2', 'tr3']].max(axis=1)
        df['atr'] = df['tr'].rolling(14).mean()
        
        # 6. Fibonacci 1.618 (Hedef Hesaplama için)
        recent_high = df['h'].rolling(50).max().iloc[-1]
        recent_low = df['l'].rolling(50).min().iloc[-1]
        diff = recent_high - recent_low
        df['fib_target'] = recent_high + (diff * 0.618) # Altın Oran Uzatması
        
        # --- UYUMSUZLUKLAR ---
        # Bullish Divergence (RSI Dip yapmıyor ama fiyat yapıyor)
        df['price_low'] = df['l'].rolling(5).min()
        df['rsi_low'] = df['rsi'].rolling(5).min()
        # Son mumda değil, son 3 mum içinde arıyoruz
        df['bull_div'] = (df['l'] <= df['price_low']) & (df['rsi'] > df['rsi_low'].shift(1)) & (df['rsi'] < 35)

        return df
    except:
        return None

def get_depth_score(symbol):
    """Balina Analizi: Emir Defteri Dengesizliği"""
    try:
        res = requests.get(f"https://api1.binance.com/api/v3/depth?symbol={symbol}&limit=20", timeout=3).json()
        bids = sum([float(i[0]) * float(i[1]) for i in res['bids']]) # Alıcılar
        asks = sum([float(i[0]) * float(i[1]) for i in res['asks']]) # Satıcılar
        
        if asks == 0: return 1.0
        ratio = bids / asks
        return ratio
    except:
        return 1.0

# --- 5. ANA TARAMA DÖNGÜSÜ ---
def scan_market():
    # Zaman ayarı (TR Saati)
    tr_now = datetime.utcnow() + timedelta(hours=3)
    print(f"▶️ [BAŞLADI] {tr_now.strftime('%H:%M:%S')} (TR)", flush=True)

    # 1. BTC KONTROLÜ (Çöküşte işlem açma)
    btc_df = get_data("BTCUSDT", "15m")
    if btc_df is None: return
    btc_change = ((btc_df['c'].iloc[-1] - btc_df['c'].iloc[-2]) / btc_df['c'].iloc[-2]) * 100
    
    if btc_change < -0.8: # Son 15dk'da %0.8'den fazla düşüş varsa bekle
        print(f"⚠️ BTC Düşüşte (%{btc_change:.2f}), tarama pas geçiliyor.", flush=True)
        return

    # 2. COINLERİ SEÇ
    targets = get_top_volume_coins(limit=50) # En baba 50 coin

    for s in targets:
        try:
            df = get_data(s, "15m")
            if df is None: continue
            
            curr = df.iloc[-1]
            prev = df.iloc[-2]
            
            # Değişkenler
            score = 0
            strategy_name = ""
            risk_lvl = "ORTA"
            
            # --- STRATEJİ 1: BOLLINGER SQUEEZE (PATLAMA) ---
            # Bantlar çok daralmış (%5 altı) ve Hacim Patlamış
            vol_avg = df['v'].rolling(20).mean().iloc[-1]
            is_squeeze = curr['bandwidth'] < 6.0 
            is_breakout = (curr['c'] > curr['upper']) and (curr['v'] > vol_avg * 2.5)
            
            if is_squeeze and is_breakout:
                score += 8
                strategy_name = "🧨 BARUT FIÇISI (Sıkışma & Patlama)"
                risk_lvl = "YÜKSEK (Hızlı Hareket)"
            
            # --- STRATEJİ 2: DIP AVCISI (V-REVERSAL) ---
            # RSI çok düşük, Balinalar topluyor, Dönüş başlamış
            whale_ratio = get_depth_score(s)
            if curr['rsi'] < 30 and whale_ratio > 2.5: # Alıcılar 2.5 kat fazla
                if curr['c'] > prev['h']: # Dönüş mumunu gör
                    score += 7
                    strategy_name = "⚓ DİP BALİNASI (Tepki Yükselişi)"
                    risk_lvl = "DÜŞÜK (Dipte)"

            # --- STRATEJİ 3: MOMENTUM (TREND SÖRFÜ) ---
            # Trend güçlü, RSI makul, Hacim artıyor
            if curr['macd'] > curr['signal'] and curr['rsi'] > 50 and curr['rsi'] < 70:
                if curr['v'] > vol_avg and curr['c'] > curr['sma20']:
                    score += 6
                    strategy_name = "🌊 TREND SÖRFÜ (Momentum)"
                    risk_lvl = "ORTA"

            # EK PUANLAR (Teyit Mekanizması)
            if curr['delta_pct'] > 60: score += 1 # Alıcı baskın
            if btc_change > 0.1: score += 1 # BTC destekliyor
            
            # --- SİNYAL GÖNDERİMİ (Eşik: 7 Puan) ---
            if score >= 7:
                # Stop Loss ve Hedef Hesaplama (ATR Kullanarak)
                # Sen "tek seferde satarım" dedin, o yüzden sana en net hedefi veriyoruz.
                atr_val = curr['atr']
                
                stop_price = curr['c'] - (atr_val * 1.5) # ATR'ye göre dinamik stop
                # Hedef: Tek atışlık yer -> Fibonacci Hedefi veya R:R 1:3
                target_price = max(curr['fib_target'], curr['c'] + (atr_val * 4))
                
                # Yüzdeler
                stop_pct = ((stop_price - curr['c']) / curr['c']) * 100
                target_pct = ((target_price - curr['c']) / curr['c']) * 100
                
                # Sinyal Tekrarını Önle (3 Saat)
                if s not in sent_signals or (time.time() - sent_signals[s]) > 10800:
                    
                    # Coin Hacmini bul (Kartta göstermek için)
                    ticker_info = [x for x in get_top_volume_coins(60) if x == s] # Basit lookup
                    vol_txt = "Yüksek Likidite" # Default

                    # --- GÖRSEL SİNYAL KARTI ---
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
                        f"🐋 **Balina Oranı:** Alıcılar x{get_depth_score(s):.1f} baskın\n"
                        f"🔗 [Binance Spot](https://www.binance.com/en/trade/{s}_USDT?type=spot)"
                    )
                    
                    if TELEGRAM_TOKEN and CHAT_ID:
                        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", 
                                      data={'chat_id': CHAT_ID, 'text': msg, 'parse_mode': 'Markdown'})
                        sent_signals[s] = time.time()
                        print(f"✅ Sinyal: {s} -> {strategy_name}")

            time.sleep(0.2) # API Limiti için bekleme

        except Exception as e:
            print(f"Hata ({s}): {e}")
            continue

    # Bitiş Logu
    next_run = tr_now + timedelta(minutes=2) # 2 Dakikada bir tara
    print(f"🏁 [BİTTİ] Sonraki: {next_run.strftime('%H:%M:%S')}", flush=True)
    print("-" * 40, flush=True)

if __name__ == "__main__":
    while True:
        scan_market()
        time.sleep(120) # 2 dakika bekle
