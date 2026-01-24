import ccxt
import pandas as pd
import pandas_ta as ta
import numpy as np
import time
import os
import requests
import sys
import gc
import threading
from flask import Flask
from datetime import datetime, timedelta, timezone

# --- 1. AYARLAR ---
API_KEY = os.getenv('BINANCE_API_KEY')
API_SECRET = os.getenv('BINANCE_SECRET_KEY')
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

# ATR filtresi (optimize)
MIN_ATR_PCT = 0.0018
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
MACRO_TTL = timedelta(minutes=10)

bot_status = {"last_run": "Henüz Başlamadı", "status": "Bekleniyor...", "signal_count": 0}

sys.stdout.reconfigure(line_buffering=True)

# --- 2. BAĞLANTI AYARLARI ---
exchange = ccxt.binance({
    'apiKey': API_KEY,
    'secret': API_SECRET,
    'options': {'defaultType': 'spot', 'adjustForTimeDifference': True},
    'enableRateLimit': True,
    'timeout': 15000
})

app = Flask(__name__)
signal_history = {}

# --- BOMB CANDIDATE CACHE ---
bomb_history = {}
BOMB_COOLDOWN = timedelta(hours=24)

# --- HEARTBEAT / WATCHDOG ---
heartbeat = {
    "last_beat_utc": None,
    "last_beat_tr": None,
    "last_symbol": None,
    "progress": None,
    "loop": 0,
    "status": "BOOT"
}
HEARTBEAT_EVERY_SEC = 60       # Render log'a en geç 60 sn'de bir "ALIVE" bas
WATCHDOG_STALE_SEC = 300       # 5 dk heartbeat yoksa process'i öldür -> Render restart
_last_beat_ts = 0.0


def beat(force=False):
    global _last_beat_ts
    now_ts = time.time()
    if force or (now_ts - _last_beat_ts) >= HEARTBEAT_EVERY_SEC:
        utc_now = datetime.now(timezone.utc)
        tr_now = utc_now.astimezone(timezone(timedelta(hours=3)))
        heartbeat["last_beat_utc"] = utc_now.strftime("%Y-%m-%d %H:%M:%S")
        heartbeat["last_beat_tr"] = tr_now.strftime("%Y-%m-%d %H:%M:%S")
        heartbeat["status"] = bot_status.get("status", "UNKNOWN")
        print(
            f"💓 ALIVE | loop={heartbeat['loop']} | {heartbeat['progress']} | {heartbeat['last_symbol']} | TR={heartbeat['last_beat_tr']}",
            flush=True
        )
        _last_beat_ts = now_ts


def watchdog():
    while True:
        try:
            if heartbeat["last_beat_utc"] is None:
                time.sleep(5)
                continue

            last = datetime.strptime(heartbeat["last_beat_utc"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            stale = (now - last).total_seconds()

            if stale > WATCHDOG_STALE_SEC:
                print(f"🛑 WATCHDOG: Heartbeat {int(stale)}s stale. Forcing restart...", flush=True)
                os._exit(1)

            time.sleep(10)
        except Exception as e:
            print(f"⚠️ WATCHDOG ERR: {e}", flush=True)
            time.sleep(10)


@app.route('/')
def home():
    now = datetime.now(timezone(timedelta(hours=3))).strftime('%H:%M:%S')
    return f"""
    <h1>🚀 Sniper Bot Kontrol Paneli (OPTIMIZED MOD)</h1>
    <p><b>Durum:</b> {bot_status['status']}</p>
    <p><b>Son Tarama (TR):</b> {bot_status['last_run']}</p>
    <p><b>Toplam Sinyal:</b> {bot_status['signal_count']}</p>
    <p><b>Şu anki Saat:</b> {now}</p>
    <hr>
    <p><b>Heartbeat (TR):</b> {heartbeat.get('last_beat_tr')}</p>
    <p><b>Son Coin:</b> {heartbeat.get('last_symbol')}</p>
    <p><b>İlerleme:</b> {heartbeat.get('progress')}</p>
    <hr>
    <p><i>Ayarlar: ATR %0.18 | Cache 10dk | Hacim 1.2x/2.0x | Watchdog aktif</i></p>
    """


@app.route('/health')
def health():
    return {"bot_status": bot_status, "heartbeat": heartbeat}


# --- 3. VERİ İŞLEMLERİ ---
def get_tradable_symbols():
    try:
        exchange.load_markets()
        symbols = [
            s for s in exchange.markets
            if s.endswith('/USDT')
            and exchange.markets[s].get('active', False)
            and s not in IGNORED_COINS
        ]
        return symbols
    except Exception as e:
        print(f"⚠️ Sembol Listesi Hatası: {e}", flush=True)
        return []


def get_data(symbol, timeframe, limit=200):
    try:
        time.sleep(0.1)
        bars = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        df = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df.set_index('timestamp', inplace=True)
        return df
    except Exception as e:
        print(f"⚠️ Veri Hatası ({symbol} / {timeframe}): {e}", flush=True)
        return None


# --- 4. HESAPLAMA & İNDİKATÖR HAZIRLIĞI ---
def prepare_indicators(df):
    try:
        df['ema50'] = ta.ema(df['close'], length=50)
        df['ema200'] = ta.ema(df['close'], length=200)
        df['atr'] = ta.atr(df['high'], df['low'], df['close'], length=14)
        df['atr_mean'] = df['atr'].rolling(100, min_periods=50).mean()
        df['rsi'] = ta.rsi(df['close'], length=14)
        bb = ta.bbands(df['close'], length=20, std=2)
        df['upper_band'] = bb['BBU_20_2.0']
        df['vol_ma'] = df['volume'].rolling(20).mean()
        return df
    except Exception as e:
        print(f"⚠️ Indicator Hatası: {e}", flush=True)
        return df


def get_macro_regime(symbol):
    try:
        now = datetime.now(timezone.utc)
        if symbol in macro_cache:
            regime, ts = macro_cache[symbol]
            if now - ts < MACRO_TTL:
                return regime, None

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

        macro_cache[symbol] = (regime, now)
        return regime, df_1h
    except Exception as e:
        print(f"⚠️ Macro Regime Hatası ({symbol}): {e}", flush=True)
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
        if "VOLATILE" in coin_type:
            atr = df_15m['atr'].iloc[-1]
            atr_target = entry_price + (4.0 * atr)
            if atr_target > structural_target:
                final_target = atr_target

        tp_pct = ((final_target - entry_price) / entry_price) * 100
        return final_target, tp_pct
    except Exception as e:
        print(f"⚠️ Target Hatası: {e}", flush=True)
        return entry_price * 1.03, 3.0


# --- 5. STRATEJİLER ---
def strategy_sfp_dynamic(df_15m):
    try:
        if len(df_15m) < 60:
            return False, None
        last = df_15m.iloc[-1]

        ema_now = df_15m['ema50'].iloc[-1]
        ema_prev = df_15m['ema50'].iloc[-5]
        if pd.isna(ema_now) or pd.isna(ema_prev):
            return False, None

        if ema_now < ema_prev:
            return False, None

        scan_window = 50
        past_window = df_15m.iloc[-scan_window:-1]
        pivot_idx = past_window['low'].idxmin()
        pivot_low = past_window.loc[pivot_idx]['low']
        pivot_rsi = df_15m.loc[pivot_idx]['rsi']

        atr_now = last['atr']
        atr_mean = last['atr_mean']
        if pd.isna(atr_mean) or atr_mean == 0:
            vol_ratio = 1.0
        else:
            vol_ratio = atr_now / atr_mean

        coin_type_tag = "NORMAL"
        sweep_mult = 0.15
        reclaim_mult = 0.25
        stop_mult = 0.30
        wick_mult = 1.5

        if vol_ratio >= 1.25:
            coin_type_tag = "🔥 VOLATILE"
            sweep_mult = 0.25
            reclaim_mult = 0.35
            stop_mult = 0.45
            wick_mult = 1.8
        elif vol_ratio <= 0.85:
            coin_type_tag = "🧊 CALM"
            sweep_mult = 0.10
            reclaim_mult = 0.20
            stop_mult = 0.25
            wick_mult = 1.5

        sweep_limit = pivot_low - (sweep_mult * atr_now)
        dip_zone = pivot_low + (reclaim_mult * atr_now)

        swept = last['low'] < sweep_limit
        reclaimed = last['close'] > dip_zone

        body = abs(last['close'] - last['open'])
        lower_wick = min(last['close'], last['open']) - last['low']
        strong_wick = True if body == 0 else lower_wick > (body * wick_mult)

        vol_ok = last['volume'] > (last['vol_ma'] * 1.2)

        if swept and reclaimed and strong_wick and vol_ok:
            safe_stop = pivot_low - (stop_mult * atr_now)
            current_rsi = last['rsi']
            is_gold = current_rsi >= (pivot_rsi - 3)

            if is_gold:
                final_type = f"🟢 SFP-A (GOLD) | {coin_type_tag}"
                desc = "Mükemmel Sinyal: Dip Süpürme + RSI Uyumsuzluğu"
            else:
                final_type = f"🟡 SFP-B (SILVER) | {coin_type_tag}"
                desc = "Standart SFP: Dip Süpürme (Uyumsuzluk Yok/Zayıf)"

            return True, {'type': final_type, 'desc': desc, 'stop': safe_stop, 'coin_type': coin_type_tag}

        return False, None
    except Exception as e:
        print(f"⚠️ SFP Hatası: {e}", flush=True)
        return False, None


def strategy_pullback(df_15m):
    try:
        last = df_15m.iloc[-1]
        ema50 = last['ema50']
        ema200 = last['ema200']
        rsi = last['rsi']
        if pd.isna(ema50) or pd.isna(ema200):
            return False, None
        if not (ema50 > ema200):
            return False, None

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
    except Exception as e:
        print(f"⚠️ Pullback Hatası: {e}", flush=True)
        return False, None


def strategy_reaccumulation(df_15m):
    try:
        last = df_15m.iloc[-1]
        ema50 = last['ema50']
        atr = last['atr']
        atr_mean = last['atr_mean']
        if last['close'] < ema50:
            return False, None

        lookback = 12
        recent_window = df_15m.iloc[-lookback:-1]
        range_height = recent_window['high'].max() - recent_window['low'].min()
        if range_height > (4.0 * atr):
            return False, None

        if pd.isna(atr_mean) or atr_mean == 0:
            return False, None

        if (atr / atr_mean) > 0.95:
            return False, None

        breakout_level = recent_window['high'].max() + (0.1 * atr)
        breakout = last['close'] > breakout_level

        body = abs(last['close'] - last['open'])
        upper_wick = last['high'] - last['close']
        strong_candle = (last['close'] > last['open']) and (body > upper_wick)

        vol_ok = last['volume'] > last['vol_ma']

        if breakout and strong_candle and vol_ok:
            mid_point = (recent_window['high'].max() + recent_window['low'].min()) / 2
            tight_stop = mid_point - (0.5 * atr)
            return True, {
                'type': '🚩 RE-ACCUMULATION (PRO)',
                'desc': f'Trend İçi Bayrak Kırılımı. Sıkışma Oranı: {(atr/atr_mean):.2f}',
                'stop': tight_stop,
                'coin_type': 'TREND'
            }
        return False, None
    except Exception as e:
        print(f"⚠️ Re-Accumulation Hatası: {e}", flush=True)
        return False, None


def strategy_breakout(df_15m):
    try:
        last = df_15m.iloc[-1]
        upper_band = last['upper_band']
        vol_ma = last['vol_ma']
        if pd.isna(upper_band):
            return False, None

        breakout = last['close'] > upper_band
        vol_explosion = last['volume'] > (vol_ma * 2.0)

        if breakout and vol_explosion:
            return True, {
                'type': '💥 SQUEEZE BREAKOUT',
                'desc': 'Sıkışma Sonrası Patlama',
                'stop': df_15m['low'].iloc[-3:].min(),
                'coin_type': 'SQUEEZE'
            }
        return False, None
    except Exception as e:
        print(f"⚠️ Breakout Hatası: {e}", flush=True)
        return False, None


def strategy_bomb_candidate(df_15m):
    try:
        if len(df_15m) < 120:
            return False, None

        last = df_15m.iloc[-1]

        # Trend filtresi
        if last['close'] < last['ema200']:
            return False, None

        # ATR sıkışma
        atr = last['atr']
        atr_mean = last['atr_mean']
        if pd.isna(atr_mean) or atr_mean == 0:
            return False, None

        compression = atr / atr_mean
        if compression > 0.75:
            return False, None

        # RSI preload zone
        if not (55 <= last['rsi'] <= 68):
            return False, None

        # Hacim öncü artış
        if last['volume'] < last['vol_ma'] * 1.5:
            return False, None

        # Son 20 mumda fake breakout olmaması
        recent_high = df_15m['high'].iloc[-20:-1].max()
        if last['close'] > recent_high * 1.03:
            return False, None

        stop = last['low'] - (1.2 * atr)

        return True, {
            'type': '💣 BOMB CANDIDATE',
            'desc': 'ATR Sıkışma + Hacim Öncü Artış (1–4 Saatlik Patlama Adayı)',
            'stop': stop,
            'coin_type': 'BOMB'
        }
    except Exception as e:
        print(f"⚠️ Bomb Candidate Hatası: {e}", flush=True)
        return False, None

def send_test_message():
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": "✅ Bot ayakta: test mesajı", "parse_mode": "HTML"},
            timeout=10
        )
        print(f"📨 Telegram test status={r.status_code}, resp={r.text[:200]}", flush=True)
    except Exception as e:
        print(f"⚠️ Telegram test hata: {e}", flush=True)

# --- 6. ANA MOTOR (MAIN THREAD) ---
def run_bot_engine():
    print("🚀 Sniper Bot Motoru BAŞLATILDI (MAIN THREAD)", flush=True)
    bot_status["status"] = "Aktif"

    while True:
        try:
            utc_now = datetime.now(timezone.utc)
            tr_time = utc_now.astimezone(timezone(timedelta(hours=3)))

            time_str = tr_time.strftime('%H:%M:%S')
            print(f"\n🔎 [TARAMA] {time_str} TR", flush=True)
            bot_status["last_run"] = time_str

            heartbeat["loop"] += 1
            heartbeat["progress"] = "START"
            heartbeat["last_symbol"] = None
            beat(force=True)

            gc.collect()

            symbols = get_tradable_symbols()

            reject = {
                "cooldown": 0,
                "macro_downtrend": 0,
                "no_15m": 0,
                "atr_nan_or_zero": 0,
                "atr_pct_low": 0,
                "no_signal": 0,
                "risk_gt_target": 0,
                "telegram_fail": 0,
                "sent": 0,
            }

            # Cooldown temizliği
            to_remove = [sym for sym, t in signal_history.items() if (utc_now - t) > timedelta(minutes=COOLDOWN_MINUTES)]
            for sym in to_remove:
                del signal_history[sym]

            count = 0
            total = len(symbols)

            for symbol in symbols:
                count += 1

                heartbeat["last_symbol"] = symbol
                heartbeat["progress"] = f"{count}/{total}"
                beat()

                if count % 20 == 0:
                    print(f"-> İlerleme: {count}/{total} ({symbol})", flush=True)

                try:
                    if symbol in signal_history:
                        reject["cooldown"] += 1
                        continue

                    regime, _df_1h = get_macro_regime(symbol)
                    if regime == "DOWNTREND":
                        reject["macro_downtrend"] += 1
                        continue

                    df_15m = get_data(symbol, '15m', limit=200)
                    if df_15m is None:
                        reject["no_15m"] += 1
                        continue

                    df_15m = prepare_indicators(df_15m)

                    last = df_15m.iloc[-1]
                    if pd.isna(last['atr']) or last['close'] == 0:
                        reject["atr_nan_or_zero"] += 1
                        continue

                    atr_pct = last['atr'] / last['close']
                    if atr_pct < MIN_ATR_PCT:
                        reject["atr_pct_low"] += 1
                        continue

                    if not signal_found:
                        reject["no_signal"] += 1
                        continue
                    data = {}

                    if regime == "RANGING" or regime == "NEUTRAL":
                        # --- BOMB COOLDOWN KONTROLÜ ---
                        if symbol in bomb_history and (utc_now - bomb_history[symbol] < BOMB_COOLDOWN):
                            continue

                        is_bomb, bomb_data = strategy_bomb_candidate(df_15m)
                        if is_bomb:
                            signal_found = True
                            data = bomb_data
                        else:
                            is_sfp, sfp_data = strategy_sfp_dynamic(df_15m)
                            if is_sfp:
                                signal_found = True
                                data = sfp_data

                    elif regime == "UPTREND":
                        is_pb, pb_data = strategy_pullback(df_15m)
                        if is_pb:
                            signal_found = True
                            data = pb_data
                        else:
                            is_re, re_data = strategy_reaccumulation(df_15m)
                            if is_re:
                                signal_found = True
                                data = re_data

                    elif regime == "SQUEEZE":
                        is_brk, brk_data = strategy_breakout(df_15m)
                        if is_brk:
                            signal_found = True
                            data = brk_data

                    if signal_found:
                        bot_status["signal_count"] += 1
                        signal_history[symbol] = utc_now

                        # --- BOMB SINYALİ İÇİN 24s COOLDOWN KAYDI ---
                        if data.get('coin_type') == 'BOMB':
                            bomb_history[symbol] = utc_now

                        entry_price = df_15m['close'].iloc[-1]
                        coin_type_info = data.get('coin_type', 'NORMAL')
                        tp_price, tp_pct = find_structural_target(df_15m, entry_price, coin_type_info)
                        stop_price = data['stop']
                        risk_pct = ((entry_price - stop_price) / entry_price) * 100

                        if tp_pct < risk_pct:
                            reject["risk_gt_target"] += 1
                            print(f"❌ {symbol} RED: Risk({risk_pct:.2f}) > Target({tp_pct:.2f})", flush=True)
                            continue

                        signal_time_str = tr_time.strftime('%H:%M')
                        msg = f"""
<b>{data['type']}</b>
━━━━━━━━━━━━━━━━━━━━
<b>#{symbol}</b> | 🕒 {signal_time_str}
━━━━━━━━━━━━━━━━━━━━
🧠 <b>BAĞLAM:</b> {regime}
📝 <b>NEDEN:</b> {data['desc']}

💵 <b>GİRİŞ :</b> <code>{entry_price:.4f}</code>
🛡️ <b>STOP  :</b> <code>{stop_price:.4f}</code> (Risk: %{risk_pct:.2f})

🎯 <b>HEDEF</b>
━━━━━━━━━━━━━━━━━━━━
🏆 Hedef: <code>{tp_price:.4f}</code>
Potansiyel: <b>%{tp_pct:.2f}</b>
"""
                        try:
                            r = requests.post(
                                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                                json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"},
                                timeout=10
                            )
                        
                            if r.status_code != 200:
                                print(f"⚠️ Telegram non-200: {r.status_code} | {r.text[:300]}", flush=True)
                            else:
                                print(f"✅ SİNYAL: {symbol} | {data['type']}", flush=True)
                        
                        except Exception as e:
                            print(f"⚠️ Telegram Exception: {e}", flush=True)

                except Exception as e:
                    print(f"⚠️ Hata ({symbol}): {e}", flush=True)
                    continue

            print(f"📊 REJECT SUMMARY: {reject}", flush=True)
            print("🏁 Tarama Bitti. 5 dakika bekleniyor...", flush=True)
            beat(force=True)
            time.sleep(300)

        except Exception as e:
            print(f"🔥 Kritik Döngü Hatası: {e}", flush=True)
            time.sleep(60)


# --- 7. BAŞLATICI ---
if __name__ == "__main__":
    flask_thread = threading.Thread(
        target=app.run,
        kwargs={'host': '0.0.0.0', 'port': int(os.environ.get("PORT", 10000)), 'use_reloader': False}
    )
    flask_thread.daemon = True
    flask_thread.start()

    wd = threading.Thread(target=watchdog, daemon=True)
    wd.start()

    print("🌍 Web Sunucusu Arka Planda Başladı...", flush=True)
    print("🛡️ Watchdog aktif (donma olursa otomatik restart)", flush=True)

    run_bot_engine()
