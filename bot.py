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
    return "🚀 Bot Aktif - 7/24 Sinyal Tarayıcı (Full Mod)"

# --- 2. AYARLAR VE GÜVENLİK ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

# Taranmayacaklar
EXCLUDED = ['USDC', 'FDUSD', 'TUSD', 'USDP', 'BUSD', 'DAI', 'EUR', 'TRY', 'PAXG', 'WBTC', 'USDE']
sent_signals = {}

# --- 3. AKILLI HACİM FİLTRESİ ---
def get_top_volume_coins(limit=60):
    """
    Sadece Marketin 'Baba' coinlerini tarar.
    Min 30M$ Hacim şartı ile sığ tahtaları eler.
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
                # FİLTRE: Min 30 Milyon Dolar Hacim
                if quote_vol > 30000000:
                    base = symbol.replace('USDT', '')
                    if base not in EXCLUDED and "UP" not in symbol and "DOWN" not in symbol:
                        valid_coins.append({'symbol': symbol, 'vol': quote_vol})
        
        # En yüksek hacimlileri seç
        valid_coins.sort(key=lambda x: x['vol'], reverse=True)
        return [x['symbol'] for x in valid_coins[:limit]]
    except:
        return []

# --- 4. TEKNİK ANALİZ MOTORU (FULL DONANIM) ---
def get_data(symbol, interval):
    try:
        url = f"https://api1.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit=100"
        response = requests.get(url, timeout=5)
        r = response.json()
        
        df = pd.DataFrame(r, columns=['ts','o','h','l','c','v','ct','qa','nt','tb','tq','i'])
        cols = ['o','h','l','c','v','tb']
        df[cols] = df[cols].astype(float)
        
        # --- A. TEMEL GÖSTERGELER ---
        
        # 1. OBV (On-Balance Volume) - Önceki kodda silinen kısım geri geldi
        df['obv'] = (np.sign(df['c'].diff()) * df['v']).fillna(0).cumsum()

        # 2. RSI Hesaplaması
        delta = df['c'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        rs = gain / (loss + 1e-9)
        df['rsi'] = 100 - (100 / (1 + rs))
        
        # 3. Bollinger Bantları & Sıkışma (Squeeze)
        df['sma20'] = df['c'].rolling(20).mean()
        df['std20'] = df['c'].rolling(20).std()
        df['upper'] = df['sma20'] + (2 * df['std20'])
        df['lower'] = df['sma20'] - (2 * df['std20'])
        # Bandwidth: Bantlar ne kadar daraldı?
        df['bandwidth'] = ((df['upper'] - df['lower']) / df['sma20']) * 100
        
        # 4. MACD
        exp1 = df['c'].ewm(span=12, adjust=False).mean()
        exp2 = df['c'].ewm(span=26, adjust=False).mean()
        df['macd'] = exp1 - exp2
        df['signal'] = df['macd'].ewm(span=9, adjust=False).mean()
        
        # 5. Hacim Delta (Alıcı Baskısı)
        df['delta_pct'] = (df['tb'] / df['v']) * 100
        
        # --- B. GELİŞMİŞ GÖSTERGELER ---

        # 6. ATR ve ADX (Tam Matematiksel Hesaplama)
        df['tr1'] = df['h'] - df['l']
        df['tr2'] = abs(df['h'] - df['c'].shift(1))
        df['tr3'] = abs(df['l'] - df['c'].shift(1))
        df['tr'] = df[['tr1', 'tr2', 'tr3']].max(axis=1)
        df['atr'] = df['tr'].rolling(14).mean()
        
        # ADX Hesaplaması (Trend Gücü)
        df['up_move'] = df['h'].diff()
        df['down_move'] = df['l'].diff()
        df['plus_dm'] = np.where((df['up_move'] > df['down_move']) & (df['up_move'] > 0), df['up_move'], 0)
        df['minus_dm'] = np.where((df['down_move'] > df['up_move']) & (df['down_move'] > 0), df['down_move'], 0)
        
        df['plus_di'] = 100 * (df['plus_dm'].rolling(14).mean() / (df['atr'] + 1e-9))
        df['minus_di'] = 100 * (df['minus_dm'].rolling(14).mean() / (df['atr'] + 1e-9))
        df['dx'] = 100 * abs(df['plus_di'] - df['minus_di']) / (df['plus_di'] + df['minus_di'] + 1e-9)
        df['adx'] = df['dx'].rolling(14).mean()

        # 7. Fibonacci 1.618 (Hedef Hesaplama için)
        recent_high = df['h'].rolling(50).max().iloc[-1]
        recent_low = df['l'].rolling(50).min().iloc[-1]
        diff = recent_high - recent_low
        df['fib_target'] = recent_high + (diff * 0.618) # Altın Oran Uzatması
        
        # --- C. UYUMSUZLUK KONTROLLERİ ---
        
        # Bullish Divergence (RSI Dip yapmıyor ama fiyat yapıyor)
        df['price_low'] = df['l'].rolling(5).min()
        df['rsi_low'] = df['rsi'].rolling(5).min()
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

# --- 5. ANA TARAMA FONKSİYONU ---
def scan_market():
    # TR Saati Hesaplama
    tr_now = datetime.utcnow() + timedelta(hours=3)
    print(f"▶️ [BAŞLADI] {tr_now.strftime('%H:%M:%S')} (TR)", flush=True)

    # 1. BTC KONTROLÜ (Çöküşte işlem açma)
    btc_df = get_data("BTCUSDT", "15m")
    if btc_df is None: return
    btc_change = ((btc_df['c'].iloc[-1] - btc_df['c'].iloc[-2]) / btc_df['c'].iloc[-2]) * 100
    
    if btc_change < -0.8: # Son 15dk'da sert düşüş varsa bekle
        print(f"⚠️ BTC Düşüşte (%{btc_change:.2f}), tarama pas geçiliyor.", flush=True)
        return

    # 2. COINLERİ SEÇ
    targets = get_top_volume_coins(limit=55)

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
            # Bantlar çok daralmış (%6 altı) ve Hacim Patlamış
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
            # Trend güçlü (ADX > 25), RSI makul, Hacim artıyor
            if curr['macd'] > curr['signal'] and curr['rsi'] > 50 and curr['rsi'] < 70:
                if curr['v'] > vol_avg and curr['adx'] > 25:
                    score += 6
                    strategy_name = "🌊 TREND SÖRFÜ (Momentum)"
                    risk_lvl = "ORTA"

            # EK PUANLAR (Teyit Mekanizması)
            if curr['delta_pct'] > 60: score += 1 # Alıcı baskın
            if btc_change > 0.1: score += 1 # BTC destekliyor
            if curr['obv'] > df['obv'].iloc[-5]: score += 1 # OBV Artıyor
            
            # --- SİNYAL GÖNDERİMİ (Eşik: 7 Puan) ---
            if score >= 7:
                # Stop Loss ve Hedef Hesaplama (ATR Kullanarak)
                atr_val = curr['atr']
                
                stop_price = curr['c'] - (atr_val * 1.5) # ATR'ye göre dinamik stop
                # Hedef: Fibonacci Golden Pocket veya ATR x4
                target_price = max(curr['fib_target'], curr['c'] + (atr_val * 4))
                
                # Yüzdeler
                stop_pct = ((stop_price - curr['c']) / curr['c']) * 100
                target_pct = ((target_price - curr['c']) / curr['c']) * 100
                
                # Sinyal Tekrarını Önle (3 Saat)
                if s not in sent_signals or (time.time() - sent_signals[s]) > 10800:
                    
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
                        f"🐋 **Balina Oranı:** Alıcılar x{whale_ratio:.1f} baskın\n"
                        f"📈 **ADX Trend:** {curr['adx']:.1f}\n"
                        f"🔗 [Binance Spot](https://www.binance.com/en/trade/{s}_USDT?type=spot)"
                    )
                    
                    if TELEGRAM_TOKEN and CHAT_ID:
                        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", 
                                      data={'chat_id': CHAT_ID, 'text': msg, 'parse_mode': 'Markdown'})
                        sent_signals[s] = time.time()
                        print(f"✅ Sinyal: {s} -> {strategy_name}")

            time.sleep(0.1) # API Limiti için bekleme

        except Exception as e:
            # print(f"Hata ({s}): {e}") # Log kirliliği yapmaması için kapalı
            continue

    # Bitiş Logu
    next_run = tr_now + timedelta(minutes=2) # 2 Dakikada bir tara
    print(f"🏁 [BİTTİ] Sonraki: {next_run.strftime('%H:%M:%S')}", flush=True)
    print("-" * 40, flush=True)

# --- 6. DÖNGÜYÜ BAŞLATAN YENİ YAPI ---
def start_loop():
    """Botun sürekli çalışmasını sağlayan döngü"""
    print("🚀 Bot Başlatılıyor... Gunicorn Modu Aktif", flush=True)
    
    # Başlangıç Test Mesajı
    if TELEGRAM_TOKEN and CHAT_ID:
        try:
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", 
                        data={'chat_id': CHAT_ID, 'text': "🖥️ **Sistem Gunicorn ile Yeniden Başlatıldı**\nFull Strateji Devrede.", 'parse_mode': 'Markdown'})
        except: pass

    while True:
        scan_market()
        time.sleep(120)

# !!! OTOMATİK BAŞLATMA AYARI !!!
# Gunicorn bu dosyayı import ettiği anda bu thread devreye girer.
# Aşağıdaki satır sayesinde "if __name__" bloğuna takılmadan çalışır.
threading.Thread(target=start_loop, daemon=True).start()

if __name__ == "__main__":
    # Bu kısım sadece lokal testler içindir, Render'da Gunicorn burayı görmez.
    app.run(host='0.0.0.0', port=8080)
