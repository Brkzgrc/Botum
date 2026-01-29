# -*- coding: utf-8 -*-
"""
Sniper Bot v7.1
- 2 Katmanlı Tarama (KATMAN-1: hafif 15m tarama / KATMAN-2: adaylarda ağır doğrulama)
- Ban/RateLimit/DDOS koruması (ApiGate + Global Cooldown + backoff)
- Watchdog-safe (uzun sleep/backoff sırasında heartbeat günceller)
- Stratejiler: Pullback + Re-accumulation + SFP-GOLD
"""

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
from collections import defaultdict
from flask import Flask
from datetime import datetime, timedelta, timezone
import re

# ===================== ZAMAN DİLİMİ =====================
TR_TZ = timezone(timedelta(hours=3))

# ===================== GLOBAL CACHE / AYARLAR =====================
MARKETS_CACHE = None
MARKETS_LAST_REFRESH = 0
MARKETS_REFRESH_INTERVAL = 3600  # 1 saat
BTC_CACHE_TTL = 900              # 15 dakika
L2_DELAY_SEC = 55                # Katman-2 gecikmesi (coin başı)
BAN_UNTIL_TS = 0                 # Binance ban bitiş epoch (sec)

# ===================== 1) AYARLAR =====================
API_KEY = os.getenv('BINANCE_API_KEY')
API_SECRET = os.getenv('BINANCE_SECRET_KEY')
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

ACCOUNT_SIZE = 5000.0
RISK_PERCENT = 2.0

MIN_ATR_PCT = 0.0018
COOLDOWN_MINUTES = 120
PULLBACK_COOLDOWN_MIN = 360
REACCU_COOLDOWN_MIN = 480

IGNORED_COINS = [
    'UP/USDT', 'DOWN/USDT', 'BEAR/USDT', 'BULL/USDT',
    'USDC/USDT', 'TUSD/USDT', 'FDUSD/USDT', 'DAI/USDT', 'USDP/USDT',
    'EUR/USDT', 'TRY/USDT', 'GBP/USDT', 'BUSD/USDT', 'USTC/USDT',
    'PAXG/USDT', 'WBTC/USDT', 'USDE/USDT', 'BRL/USDT', 'RUB/USDT',
    'AUD/USDT', 'UST/USDT', 'USD/USDT', 'XUSD/USDT', 'USD1/USDT',
]

MACRO_SYMBOL = "BTC/USDT"
EXPLAIN_SIGNALS = True

USE_RS_FILTER = True
RS_BARS_1H  = 4
RS_BARS_4H  = 16
RS_BARS_12H = 48
RS_BARS_24H = 96
RS_MIN_REL_1H  = 0.15
RS_MIN_REL_4H  = 0.50
RS_MIN_SCORE   = 0.60
RS_RISK_OFF_MIN_SCORE = 1.50

bot_status = {"last_run": "Henüz Başlamadı", "status": "Bekleniyor...", "signal_count": 0}
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

# ===================== 2) HEARTBEAT / WATCHDOG =====================
app = Flask(__name__)
signal_history = {}

heartbeat = {
    "last_beat_utc": None,
    "last_beat_tr": None,
    "last_symbol": None,
    "progress": None,
    "loop": 0,
    "status": "BOOT"
}
HEARTBEAT_UPDATE_SEC = 30
WATCHDOG_STALE_SEC = 300

_last_update_ts = 0.0
_last_print_ts = 0.0

def beat(force_print=False):
    global _last_update_ts, _last_print_ts
    now_ts = time.time()
    utc_now = datetime.now(timezone.utc)
    tr_now = utc_now.astimezone(TR_TZ)

    if (now_ts - _last_update_ts) >= HEARTBEAT_UPDATE_SEC:
        heartbeat["last_beat_utc"] = utc_now.strftime("%Y-%m-%d %H:%M:%S")
        heartbeat["last_beat_tr"] = tr_now.strftime("%Y-%m-%d %H:%M:%S")
        heartbeat["status"] = bot_status.get("status", "UNKNOWN")
        _last_update_ts = now_ts

    if force_print and (now_ts - _last_print_ts) > 2:
        print(
            f"💓 ALIVE | loop={heartbeat.get('loop')} | {heartbeat.get('progress')} | "
            f"{heartbeat.get('last_symbol')} | TR={heartbeat.get('last_beat_tr')}",
            flush=True
        )
        _last_print_ts = now_ts

def sleep_with_heartbeat(seconds: float, status: str = None):
    """Uzun sleep sırasında watchdog 'stale' görmesin."""
    if seconds <= 0:
        return
    end = time.time() + seconds
    while True:
        now = time.time()
        if now >= end:
            break
        if status:
            bot_status["status"] = status
        try:
            beat()
        except Exception:
            pass
        time.sleep(min(10.0, end - now))

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
    now_tr = datetime.now(TR_TZ).strftime('%H:%M:%S')
    return f"""
    <h1>🚀 Sniper Bot v7.1 (2 Katman + Ban-Safe)</h1>
    <p><b>Durum:</b> {bot_status['status']}</p>
    <p><b>Son Tarama (TR):</b> {bot_status['last_run']}</p>
    <p><b>Toplam Sinyal:</b> {bot_status['signal_count']}</p>
    <p><b>Şu an (TR):</b> {now_tr}</p>
    <hr>
    <p><b>Heartbeat (TR):</b> {heartbeat.get('last_beat_tr')}</p>
    <p><b>Son Coin:</b> {heartbeat.get('last_symbol')}</p>
    <p><b>İlerleme:</b> {heartbeat.get('progress')}</p>
    <hr>
    <p><b>Stratejiler:</b> ✅ Pullback | ✅ Re-accumulation | ✅ SFP-GOLD</p>
    """

@app.route('/health')
def health():
    return {"bot_status": bot_status, "heartbeat": heartbeat}

# ===================== 3) BAN / RATE LIMIT KATMANI =====================
GLOBAL_COOLDOWN_UNTIL = 0.0

class ApiGate:
    """
    - Tüm REST çağrıları buradan geçer.
    - Min aralık + concurrency limiti + exponential backoff.
    - DDoS/RateLimit görünce GLOBAL_COOLDOWN ile tüm sistemi sakinleştirir.
    - Backoff sırasında beat() çağırır (watchdog-safe).
    """
    def __init__(self, min_interval_sec=0.35, max_concurrent=1, max_retries=6):
        self.min_interval_sec = float(min_interval_sec)
        self.sem = threading.Semaphore(int(max_concurrent))
        self.max_retries = int(max_retries)
        self._lock = threading.Lock()
        self._last_call_ts = 0.0

    def _sleep_for_spacing(self):
        with self._lock:
            now = time.time()
            wait = (self._last_call_ts + self.min_interval_sec) - now
            if wait > 0:
                sleep_with_heartbeat(wait, status="API Spacing")
            self._last_call_ts = time.time()

    def call(self, fn, *args, **kwargs):
        global GLOBAL_COOLDOWN_UNTIL
        with self.sem:
            for attempt in range(self.max_retries):
                try:
                    now = time.time()
                    if GLOBAL_COOLDOWN_UNTIL > now:
                        sleep_with_heartbeat(GLOBAL_COOLDOWN_UNTIL - now, status="Global Cooldown")

                    self._sleep_for_spacing()
                    return fn(*args, **kwargs)

                except (ccxt.RateLimitExceeded, ccxt.DDoSProtection) as e:
                    backoff = min(8.0, (0.6 * (2 ** attempt)))
                    print(f"⏳ RateLimit/DDOS: {type(e).__name__} | backoff={backoff:.1f}s", flush=True)
                    GLOBAL_COOLDOWN_UNTIL = max(GLOBAL_COOLDOWN_UNTIL, time.time() + backoff + 2.0)
                    sleep_with_heartbeat(backoff, status="RateLimit Backoff")

                except (ccxt.NetworkError, ccxt.RequestTimeout, ccxt.ExchangeNotAvailable) as e:
                    backoff = min(8.0, (0.4 * (2 ** attempt)))
                    print(f"🌐 Network: {type(e).__name__} | backoff={backoff:.1f}s", flush=True)
                    sleep_with_heartbeat(backoff, status="Network Backoff")

                except ccxt.ExchangeError as e:
                    backoff = min(4.0, (0.3 * (2 ** attempt)))
                    print(f"🏦 ExchangeError: {str(e)[:120]} | backoff={backoff:.1f}s", flush=True)
                    sleep_with_heartbeat(backoff, status="ExchangeError Backoff")

                except Exception as e:
                    backoff = min(2.0, (0.25 * (2 ** attempt)))
                    print(f"⚠️ API CALL ERR: {type(e).__name__}: {str(e)[:120]} | backoff={backoff:.1f}s", flush=True)
                    sleep_with_heartbeat(backoff, status="Unknown Error Backoff")

            raise RuntimeError("API call failed after retries")

api_gate = ApiGate(
    min_interval_sec=float(os.environ.get("API_MIN_INTERVAL_SEC", 0.35)),
    max_concurrent=int(os.environ.get("API_MAX_CONCURRENT", 1)),
    max_retries=int(os.environ.get("API_MAX_RETRIES", 6)),
)

# ===================== 4) BAĞLANTI =====================
exchange = ccxt.binance({
    'apiKey': API_KEY,
    'secret': API_SECRET,
    'options': {'defaultType': 'spot', 'adjustForTimeDifference': True},
    'enableRateLimit': True,
    'timeout': 15000
})

def handle_binance_ban(e: Exception) -> bool:
    global BAN_UNTIL_TS
    s = str(e)
    m = re.search(r"banned until (\d+)", s)
    if not m:
        return False
    until_ms = int(m.group(1))
    BAN_UNTIL_TS = max(BAN_UNTIL_TS, until_ms / 1000.0)
    return True

def sleep_if_banned():
    global BAN_UNTIL_TS
    now = time.time()
    if BAN_UNTIL_TS > now:
        wait_s = int(BAN_UNTIL_TS - now) + 2
        tr = datetime.fromtimestamp(BAN_UNTIL_TS, tz=TR_TZ).strftime("%Y-%m-%d %H:%M:%S")
        print(f"🛑 IP BAN aktif. Ban bitiş (TR): {tr} | {wait_s}s uyku", flush=True)
        sleep_with_heartbeat(wait_s, status="IP BAN Sleep")

# ===================== 5) MARKET / BTC CACHE =====================
def get_tradable_symbols():
    global MARKETS_CACHE, MARKETS_LAST_REFRESH
    now = time.time()
    if MARKETS_CACHE is None or (now - MARKETS_LAST_REFRESH) > MARKETS_REFRESH_INTERVAL:
        try:
            sleep_if_banned()
            api_gate.call(exchange.load_markets)
            MARKETS_LAST_REFRESH = now
            MARKETS_CACHE = [
                s for s in exchange.markets
                if s.endswith('/USDT')
                and exchange.markets[s].get('active', False)
                and s not in IGNORED_COINS
                and s.isascii()
            ]
            print(f"✅ Markets yenilendi ({len(MARKETS_CACHE)} coin)", flush=True)
        except Exception as e:
            if handle_binance_ban(e):
                sleep_if_banned()
            print(f"⚠️ Markets yenilenemedi: {e}", flush=True)
            return MARKETS_CACHE if MARKETS_CACHE else []
    return MARKETS_CACHE

btc_cache = {"15m": None, "1h": None, "last_update": None}

def get_data(symbol, timeframe, limit=200):
    try:
        sleep_if_banned()
        bars = api_gate.call(exchange.fetch_ohlcv, symbol, timeframe=timeframe, limit=limit)
        df = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df.set_index('timestamp', inplace=True)
        return df
    except Exception as e:
        if handle_binance_ban(e):
            sleep_if_banned()
        return None

def get_cached_btc_data(timeframe):
    global btc_cache
    now = time.time()
    if btc_cache["last_update"] and (now - btc_cache["last_update"]) < BTC_CACHE_TTL:
        return btc_cache[timeframe]
    df = get_data(MACRO_SYMBOL, timeframe, limit=260)
    btc_cache[timeframe] = df
    btc_cache["last_update"] = now
    return df

def wait_until_next_15m_close_tr():
    """TR saatine göre bir sonraki 15m kapanışını bekle (dakika 00/15/30/45, saniye 02)."""
    while True:
        now_tr = datetime.now(TR_TZ)
        m = now_tr.minute
        next_q = ((m // 15) + 1) * 15
        if next_q >= 60:
            target = (now_tr + timedelta(hours=1)).replace(minute=0, second=2, microsecond=0)
        else:
            target = now_tr.replace(minute=next_q, second=2, microsecond=0)
        sleep_sec = (target - datetime.now(TR_TZ)).total_seconds()
        if sleep_sec > 0:
            print(f"😴 Sonraki 15m kapanış bekleniyor: {target.strftime('%H:%M:%S')} TR ({sleep_sec:.0f}s)", flush=True)
            sleep_with_heartbeat(sleep_sec, status="15m Close Wait")
            break
        time.sleep(0.3)

# ===================== 6) FORMAT / UTILS =====================
def fmt_price(symbol: str, price) -> str:
    try:
        if price is None:
            return "N/A"
        return exchange.price_to_precision(symbol, float(price))
    except Exception:
        try:
            p = float(price)
        except Exception:
            return "N/A"
        if p == 0:
            return "0"
        if p < 0.01:
            return f"{p:.8f}"
        if p < 1:
            return f"{p:.6f}"
        return f"{p:.4f}"

def _fmt_num(x, nd=4):
    try:
        if x is None or (isinstance(x, float) and np.isnan(x)):
            return "N/A"
        return f"{float(x):.{nd}f}"
    except Exception:
        return "N/A"

def _fmt_pct(x, nd=2):
    try:
        if x is None or (isinstance(x, float) and np.isnan(x)):
            return "N/A"
        return f"%{float(x):.{nd}f}"
    except Exception:
        return "N/A"

def _fmt_x(x, nd=2):
    try:
        if x is None or (isinstance(x, float) and np.isnan(x)):
            return "N/A"
        return f"{float(x):.{nd}f}x"
    except Exception:
        return "N/A"

def _yn(ok: bool) -> str:
    return "✅" if ok else "❌"

def _pct_change_close(df: pd.DataFrame, bars: int):
    try:
        if df is None or len(df) <= bars:
            return np.nan
        now = float(df["close"].iloc[-1])
        prev = float(df["close"].iloc[-1 - bars])
        if prev == 0:
            return np.nan
        return ((now / prev) - 1.0) * 100.0
    except Exception:
        return np.nan

def calc_relative_strength(df_coin_15m: pd.DataFrame, df_btc_15m: pd.DataFrame):
    coin_1h  = _pct_change_close(df_coin_15m, RS_BARS_1H)
    coin_4h  = _pct_change_close(df_coin_15m, RS_BARS_4H)
    coin_12h = _pct_change_close(df_coin_15m, RS_BARS_12H)
    coin_24h = _pct_change_close(df_coin_15m, RS_BARS_24H)

    btc_1h   = _pct_change_close(df_btc_15m, RS_BARS_1H)
    btc_4h   = _pct_change_close(df_btc_15m, RS_BARS_4H)
    btc_12h  = _pct_change_close(df_btc_15m, RS_BARS_12H)
    btc_24h  = _pct_change_close(df_btc_15m, RS_BARS_24H)

    rel_1h  = coin_1h  - btc_1h  if not np.isnan(coin_1h)  and not np.isnan(btc_1h)  else np.nan
    rel_4h  = coin_4h  - btc_4h  if not np.isnan(coin_4h)  and not np.isnan(btc_4h)  else np.nan
    rel_12h = coin_12h - btc_12h if not np.isnan(coin_12h) and not np.isnan(btc_12h) else np.nan
    rel_24h = coin_24h - btc_24h if not np.isnan(coin_24h) and not np.isnan(btc_24h) else np.nan

    parts = []
    if not np.isnan(rel_1h):  parts.append(0.35 * rel_1h)
    if not np.isnan(rel_4h):  parts.append(0.45 * rel_4h)
    if not np.isnan(rel_12h): parts.append(0.20 * rel_12h)
    score = np.nan if not parts else sum(parts)

    return score, rel_1h, rel_4h, rel_12h, rel_24h, coin_4h, btc_4h

# ===================== 7) İNDİKATÖRLER =====================
def prepare_indicators(df):
    try:
        df['ema50'] = ta.ema(df['close'], length=50)
        df['ema200'] = ta.ema(df['close'], length=200)
        df['atr'] = ta.atr(df['high'], df['low'], df['close'], length=14)
        df['atr_mean'] = df['atr'].rolling(100, min_periods=50).mean()
        df['rsi'] = ta.rsi(df['close'], length=14)
        df['vol_ma'] = df['volume'].rolling(20, min_periods=1).mean()

        bb = ta.bbands(df['close'], length=20, std=2)
        upper = None
        if bb is not None and hasattr(bb, "columns"):
            ucols = [c for c in bb.columns if 'BBU' in str(c).upper()]
            if ucols:
                upper = bb[ucols[0]]
        if upper is None:
            m = df['close'].rolling(20, min_periods=1).mean()
            s = df['close'].rolling(20, min_periods=1).std(ddof=0)
            upper = m + 2 * s
        df['upper_band'] = upper
        return df
    except Exception as e:
        print(f"⚠️ Indicator Hatası: {e}", flush=True)
        return df

def get_coin_regime_15m(df_15m):
    try:
        if df_15m is None or len(df_15m) < 210:
            return "NEUTRAL"
        adx_df = ta.adx(df_15m['high'], df_15m['low'], df_15m['close'], length=14)
        adx_val = None
        if adx_df is not None and hasattr(adx_df, "columns"):
            cands = [c for c in adx_df.columns if str(c).upper().startswith("ADX")]
            if cands:
                adx_val = adx_df[cands[0]].iloc[-1]

        last = df_15m.iloc[-1]
        ema50 = last.get('ema50', np.nan)
        ema200 = last.get('ema200', np.nan)
        close = last.get('close', np.nan)
        if pd.isna(ema50) or pd.isna(ema200) or pd.isna(close):
            return "NEUTRAL"

        if adx_val is not None and not pd.isna(adx_val) and adx_val > 20:
            if close > ema50 and close > ema200:
                return "UPTREND"
            if close < ema50 and close < ema200:
                return "DOWNTREND"
        return "RANGING"
    except Exception:
        return "NEUTRAL"

# ===================== 8) BTC MACRO REGIME (TTL CACHE) =====================
macro_cache = {}
MACRO_TTL = timedelta(minutes=10)

def get_macro_regime(_symbol_unused=None):
    try:
        now = datetime.now(timezone.utc)
        key = "__BTC__"
        if key in macro_cache:
            regime, ts = macro_cache[key]
            if now - ts < MACRO_TTL:
                return regime, None

        df_1h = get_cached_btc_data('1h')
        if df_1h is None or len(df_1h) < 220:
            macro_cache[key] = ("NEUTRAL", now)
            return "NEUTRAL", None

        ema50_s  = ta.ema(df_1h['close'], length=50)
        ema200_s = ta.ema(df_1h['close'], length=200)
        adx_df   = ta.adx(df_1h['high'], df_1h['low'], df_1h['close'], length=14)
        bb_df    = ta.bbands(df_1h['close'], length=20, std=2)

        if ema50_s is None or ema200_s is None or adx_df is None or bb_df is None:
            macro_cache[key] = ("NEUTRAL", now)
            return "NEUTRAL", None

        ema50 = ema50_s.iloc[-1]
        ema200 = ema200_s.iloc[-1]

        adx_col = None
        if hasattr(adx_df, "columns"):
            cands = [c for c in adx_df.columns if str(c).upper().startswith("ADX")]
            if cands:
                adx_col = cands[0]
        if adx_col is None:
            macro_cache[key] = ("NEUTRAL", now)
            return "NEUTRAL", None
        adx = adx_df[adx_col].iloc[-1]

        bbu_col = bbl_col = bbm_col = None
        if hasattr(bb_df, "columns"):
            for c in bb_df.columns:
                uc = str(c).upper()
                if "BBU" in uc and bbu_col is None: bbu_col = c
                if "BBL" in uc and bbl_col is None: bbl_col = c
                if "BBM" in uc and bbm_col is None: bbm_col = c
        if bbu_col is None or bbl_col is None or bbm_col is None:
            macro_cache[key] = ("NEUTRAL", now)
            return "NEUTRAL", None

        bbu = bb_df[bbu_col].iloc[-1]
        bbl = bb_df[bbl_col].iloc[-1]
        bbm = bb_df[bbm_col].iloc[-1]
        close = df_1h['close'].iloc[-1]

        if pd.isna(ema50) or pd.isna(ema200) or pd.isna(adx) or pd.isna(bbu) or pd.isna(bbl) or pd.isna(bbm) or bbm == 0:
            macro_cache[key] = ("NEUTRAL", now)
            return "NEUTRAL", None

        bb_width = (bbu - bbl) / bbm
        if close < ema50 and close < ema200 and adx > 20:
            regime = "RISK_OFF"
        elif close > ema50 and close > ema200 and adx > 20:
            regime = "RISK_ON"
        elif bb_width < 0.08:
            regime = "SQUEEZE"
        else:
            regime = "NEUTRAL"

        macro_cache[key] = (regime, now)
        return regime, df_1h

    except Exception as e:
        print(f"⚠️ Macro Regime Hatası (BTC): {e}", flush=True)
        return "NEUTRAL", None

# ===================== 9) 1H TREND CONFIRMATION =====================
def is_1h_trend_aligned(symbol):
    try:
        df_1h = get_data(symbol, '1h', limit=260)
        if df_1h is None or len(df_1h) < 220:
            return False, "❌ 1h veri yetersiz (EMA200 için)"

        ema50 = ta.ema(df_1h['close'], length=50).iloc[-1]
        ema200 = ta.ema(df_1h['close'], length=200).iloc[-1]
        close = df_1h['close'].iloc[-1]

        if pd.isna(ema50) or pd.isna(ema200):
            return False, "❌ 1h EMA hesaplanamadı"

        if close > ema50 > ema200:
            return True, "✅ 1h Trend: EMA50 > EMA200"
        return False, "❌ 1h Trend: EMA50 <= EMA200"
    except Exception as e:
        return False, f"❌ 1h trend hatası: {str(e)[:40]}"

# ===================== 10) RİSK / POZİSYON =====================
def calc_position_size(entry, stop, account_size=ACCOUNT_SIZE, risk_pct=RISK_PERCENT):
    risk_amount = account_size * (risk_pct / 100.0)
    risk_per_coin = entry - stop
    if risk_per_coin <= 0:
        return 0, 0, 0
    position_usdt = risk_amount / (risk_per_coin / entry)
    position_usdt = min(position_usdt, account_size * 0.25)
    coin_amount = position_usdt / entry
    actual_risk_pct = (risk_per_coin / entry) * 100.0
    return position_usdt, coin_amount, actual_risk_pct

# ===================== 11) SPREAD / LIQUIDITY =====================
def check_spread_safety(symbol):
    try:
        sleep_if_banned()
        orderbook = api_gate.call(exchange.fetch_order_book, symbol, limit=5)
        if not orderbook.get('bids') or not orderbook.get('asks'):
            return False, "⚠️ Spread kontrol edilemedi (orderbook boş)"
        bid = orderbook['bids'][0][0]
        ask = orderbook['asks'][0][0]
        if bid <= 0:
            return False, "⚠️ Spread kontrol edilemedi (bid=0)"
        spread_pct = ((ask - bid) / bid) * 100.0
        if spread_pct > 0.4:
            return False, f"⚠️ Spread geniş: %{spread_pct:.2f}"
        return True, f"✅ Spread: %{spread_pct:.2f}"
    except Exception as e:
        if handle_binance_ban(e):
            sleep_if_banned()
        return True, "ℹ️ Spread kontrol edilemedi"

def get_last_price(symbol: str):
    try:
        sleep_if_banned()
        t = api_gate.call(exchange.fetch_ticker, symbol)
        return t.get("last", None)
    except Exception as e:
        if handle_binance_ban(e):
            sleep_if_banned()
        return None

def get_liquidity_warning(symbol):
    try:
        sleep_if_banned()
        ticker = api_gate.call(exchange.fetch_ticker, symbol)
        vol_24h = float(ticker.get('quoteVolume', 0) or 0)
        if vol_24h < 5_000_000:
            return f"⚠️ Likidite: ${vol_24h/1e6:.1f}M (Düşük)"
        elif vol_24h < 15_000_000:
            return f"ℹ️ Likidite: ${vol_24h/1e6:.1f}M (Orta)"
        return None
    except Exception as e:
        if handle_binance_ban(e):
            sleep_if_banned()
        return "❓ Likidite verisi alınamadı"

# ===================== 12) SR TARGET =====================
def _extract_pivot_prices(df: pd.DataFrame, lookback=140, left=3, right=3):
    try:
        n = len(df)
        if n < (left + right + 10):
            return []
        start = max(0, n - lookback)
        highs = df["high"].values
        lows  = df["low"].values
        pivots = []
        end = n - right - 1
        for i in range(start + left, end):
            h = highs[i]
            l = lows[i]
            if np.isnan(h) or np.isnan(l):
                continue
            if h == np.max(highs[i-left:i+right+1]):
                pivots.append(float(h))
            if l == np.min(lows[i-left:i+right+1]):
                pivots.append(float(l))
        return pivots
    except Exception:
        return []

def _cluster_levels(prices, tol: float):
    if not prices:
        return []
    prices = sorted([p for p in prices if p is not None and not np.isnan(p)])
    if not prices:
        return []
    clusters = [[prices[0]]]
    for p in prices[1:]:
        center = float(np.median(clusters[-1]))
        if abs(p - center) <= tol:
            clusters[-1].append(p)
        else:
            clusters.append([p])
    levels = []
    for c in clusters:
        level = float(np.median(c))
        strength = len(c)
        levels.append((level, strength))
    levels.sort(key=lambda x: x[0])
    return levels

def get_nearest_sr_levels(df_15m: pd.DataFrame, entry: float, atr: float):
    pivots = _extract_pivot_prices(df_15m, lookback=160, left=3, right=3)
    tol = max(0.35 * atr, entry * 0.0018)
    levels = _cluster_levels(pivots, tol=tol)
    support = None
    support_strength = 0
    resistance = None
    resistance_strength = 0
    for lvl, st in levels:
        if lvl < entry:
            support = lvl
            support_strength = st
        elif lvl > entry and resistance is None:
            resistance = lvl
            resistance_strength = st
            break
    return support, support_strength, resistance, resistance_strength

def find_structural_target(df_15m: pd.DataFrame, entry_price: float, coin_type="NORMAL"):
    try:
        atr = float(df_15m["atr"].iloc[-1])
        if np.isnan(atr) or atr <= 0:
            tp = entry_price * 1.03
            return tp, 3.0, "Fallback (%3)", None
        sup, sup_n, res, res_n = get_nearest_sr_levels(df_15m, entry_price, atr)
        if res is not None:
            tp = res - (0.12 * atr)
            if tp <= entry_price * 1.002:
                tp = res
            note = f"Direnç (güç={res_n})"
        else:
            tp = entry_price + max(3.0 * atr, entry_price * 0.02)
            note = "ATR bazlı"
        tp_pct = ((tp - entry_price) / entry_price) * 100.0
        return tp, tp_pct, note, sup
    except Exception as e:
        print(f"⚠️ Target Hatası (SR): {e}", flush=True)
        tp = entry_price * 1.03
        return tp, 3.0, "Fallback (%3)", None

# ===================== 13) STRATEJİLER =====================
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
        strong_wick = True if body == 0 else (lower_wick > (body * wick_mult))

        vol_ok = last['volume'] > (last['vol_ma'] * 1.2)

        if swept and reclaimed and strong_wick and vol_ok:
            safe_stop = pivot_low - (stop_mult * atr_now)
            current_rsi = last['rsi']
            is_gold = current_rsi >= (pivot_rsi - 3)
            if is_gold:
                final_type = f"🟢 SFP-A (GOLD) | {coin_type_tag}"
                desc = "Dip Süpürme + RSI Uyumsuzluğu"
                return True, {'type': final_type, 'desc': desc, 'stop': safe_stop, 'coin_type': coin_type_tag}

        return False, None

    except Exception as e:
        print(f"⚠️ SFP Hatası: {e}", flush=True)
        return False, None

def strategy_pullback(df_15m):
    try:
        last = df_15m.iloc[-1]
        ema50 = last['ema50']
        ema50_prev = df_15m['ema50'].iloc[-6]
        if pd.isna(ema50_prev) or pd.isna(ema50):
            return False, None
        if ema50 <= ema50_prev:
            return False, None

        ema200 = last['ema200']
        rsi = last['rsi']
        if pd.isna(ema200):
            return False, None
        if not (ema50 > ema200):
            return False, None

        touched_ema = last['low'] <= ema50 * 1.001
        prev = df_15m.iloc[-2]
        prev_sweep = prev['low'] < ema50 * 0.999

        rng = (last['high'] - last['low'])
        close_strength = True if rng == 0 else ((last['close'] - last['low']) / rng) > 0.65
        bounced = (last['close'] > ema50) and (last['close'] > last['open']) and close_strength

        not_overbought = rsi < 60
        vol_ok = (not pd.isna(last['vol_ma'])) and (last['volume'] > (last['vol_ma'] * 1.2))

        if touched_ema and prev_sweep and bounced and not_overbought and vol_ok:
            return True, {
                'type': '🚀 EMA PULLBACK',
                'desc': 'Trende Geri Çekilme',
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
        ema50 = last.get('ema50', np.nan)
        ema200 = last.get('ema200', np.nan)
        atr = last.get('atr', np.nan)
        atr_mean = last.get('atr_mean', np.nan)
        rsi = last.get('rsi', np.nan)

        if pd.isna(ema50) or pd.isna(ema200) or pd.isna(atr) or pd.isna(atr_mean) or atr_mean == 0:
            return False, None
        if last['close'] < ema50 or ema50 <= ema200:
            return False, None
        if pd.isna(rsi) or not (45 <= rsi <= 70):
            return False, None

        lookback = 16
        recent_window = df_15m.iloc[-lookback:-1]
        range_height = recent_window['high'].max() - recent_window['low'].min()
        if range_height > (2.8 * atr):
            return False, None

        compression = atr / atr_mean
        if compression > 0.80:
            return False, None

        recent_high = recent_window['high'].max()
        breakout_level = recent_high + (0.25 * atr)
        prev_close = df_15m['close'].iloc[-2]
        if prev_close > (recent_high + 0.05 * atr):
            return False, None

        breakout = last['close'] > breakout_level

        rng = (last['high'] - last['low'])
        close_strength = True if rng == 0 else ((last['close'] - last['low']) / rng) > 0.72
        body = abs(last['close'] - last['open'])
        body_ratio = True if rng == 0 else (body / rng) > 0.55
        strong_candle = (last['close'] > last['open']) and close_strength and body_ratio

        vol_ma = last.get('vol_ma', np.nan)
        if pd.isna(vol_ma) or vol_ma == 0:
            return False, None
        vol_ok = last['volume'] > (vol_ma * 1.6)

        if breakout and strong_candle and vol_ok:
            mid_point = (recent_window['high'].max() + recent_window['low'].min()) / 2
            tight_stop = mid_point - (0.6 * atr)
            return True, {
                'type': '🚩 RE-ACCUMULATION (PRO)',
                'desc': f'Bayrak kırılımı. Sıkışma: {compression:.2f}',
                'stop': tight_stop,
                'coin_type': 'TREND'
            }

        return False, None

    except Exception as e:
        print(f"⚠️ Re-Accumulation Hatası: {e}", flush=True)
        return False, None

# ===================== 14) EXPLAIN BLOCK =====================
def build_explain_block(symbol: str, df_15m: pd.DataFrame, data: dict) -> str:
    try:
        stype = (data.get("type", "") or "").upper()
        last = df_15m.iloc[-1]

        close = float(last.get("close", np.nan))
        open_ = float(last.get("open", np.nan))
        high = float(last.get("high", np.nan))
        low  = float(last.get("low", np.nan))
        atr = float(last.get("atr", np.nan))
        atr_mean = float(last.get("atr_mean", np.nan))
        rsi = float(last.get("rsi", np.nan))
        ema50 = float(last.get("ema50", np.nan))
        ema200 = float(last.get("ema200", np.nan))
        vol = float(last.get("volume", np.nan))
        vol_ma = float(last.get("vol_ma", np.nan))

        req_lines = []

        if "SFP" in stype and "GOLD" in stype:
            scan_window = 50
            past_window = df_15m.iloc[-scan_window:-1] if len(df_15m) >= scan_window else df_15m.iloc[:-1]
            pivot_idx = past_window["low"].idxmin()
            pivot_low = float(past_window.loc[pivot_idx]["low"])
            pivot_rsi = float(df_15m.loc[pivot_idx]["rsi"]) if "rsi" in df_15m.columns else np.nan

            vol_ratio = 1.0 if (pd.isna(atr_mean) or atr_mean == 0 or pd.isna(atr)) else (atr / atr_mean)

            sweep_mult = 0.15
            reclaim_mult = 0.25
            wick_mult = 1.5
            vol_mult = 1.2
            if vol_ratio >= 1.25:
                sweep_mult = 0.25
                reclaim_mult = 0.35
                wick_mult = 1.8
            elif vol_ratio <= 0.85:
                sweep_mult = 0.10
                reclaim_mult = 0.20

            sweep_limit = pivot_low - (sweep_mult * atr) if not pd.isna(atr) else np.nan
            reclaim_level = pivot_low + (reclaim_mult * atr) if not pd.isna(atr) else np.nan

            swept = (low < sweep_limit) if not pd.isna(sweep_limit) else False
            reclaimed = (close > reclaim_level) if not pd.isna(reclaim_level) else False

            body = abs(close - open_)
            lower_wick = min(close, open_) - low
            strong_wick = True if body == 0 else (lower_wick > (body * wick_mult))

            vol_strength = np.nan
            vol_ok = False
            if not pd.isna(vol) and not pd.isna(vol_ma) and vol_ma != 0:
                vol_strength = vol / vol_ma
                vol_ok = vol > (vol_ma * vol_mult)

            req_lines.append(f"{_yn(swept)} Dip süpürme")
            req_lines.append(f"{_yn(reclaimed)} Geri toplama")
            req_lines.append(f"{_yn(strong_wick)} Güçlü alt fitil")
            req_lines.append(f"{_yn(vol_ok)} Hacim onayı")
            req_lines.append(f"✅ RSI uyumsuzluğu (GOLD) • PivotRSI={_fmt_num(pivot_rsi,2)} | RSI={_fmt_num(rsi,2)} • HacimGücü={_fmt_x(vol_strength,2)}")

        elif "PULLBACK" in stype:
            prev = df_15m.iloc[-2] if len(df_15m) >= 2 else last
            ema50_prev = float(df_15m["ema50"].iloc[-6]) if len(df_15m) >= 6 else np.nan
            slope_ok = (ema50 > ema50_prev) if not pd.isna(ema50_prev) and not pd.isna(ema50) else False
            trend_ok = (ema50 > ema200) if not pd.isna(ema50) and not pd.isna(ema200) else False
            touched_ema = (low <= ema50 * 1.001) if not pd.isna(low) and not pd.isna(ema50) else False
            prev_low = float(prev.get("low", np.nan))
            prev_sweep = (prev_low < ema50 * 0.999) if not pd.isna(prev_low) and not pd.isna(ema50) else False

            rng = (high - low)
            close_strength = True if rng == 0 else ((close - low) / rng) > 0.65
            bounced = (close > ema50) and (close > open_) and close_strength
            rsi_ok = (rsi < 60) if not pd.isna(rsi) else False

            vol_strength = np.nan
            vol_ok = False
            if not pd.isna(vol) and not pd.isna(vol_ma) and vol_ma != 0:
                vol_strength = vol / vol_ma
                vol_ok = vol > (vol_ma * 1.2)

            req_lines.append(f"{_yn(slope_ok)} Trend güçleniyor")
            req_lines.append(f"{_yn(trend_ok)} EMA50 > EMA200")
            req_lines.append(f"{_yn(touched_ema)} EMA dokunuş")
            req_lines.append(f"{_yn(prev_sweep)} Temiz sarkma")
            req_lines.append(f"{_yn(bounced)} Güçlü tepki mumu")
            req_lines.append(f"{_yn(rsi_ok)} RSI uygun")
            req_lines.append(f"{_yn(vol_ok)} Hacim onayı • HacimGücü={_fmt_x(vol_strength,2)}")

        elif "RE-ACCUMULATION" in stype:
            if len(df_15m) < 20:
                return ""
            lookback = 16
            recent_window = df_15m.iloc[-lookback:-1]
            recent_high = float(recent_window["high"].max())
            recent_low  = float(recent_window["low"].min())
            range_height = recent_high - recent_low

            trend_ok = (close > ema50) and (ema50 > ema200) if (not pd.isna(close) and not pd.isna(ema50) and not pd.isna(ema200)) else False
            rsi_ok = (not pd.isna(rsi)) and (45 <= rsi <= 70)
            range_ok = (not pd.isna(atr)) and (range_height <= (2.8 * atr))

            compression = np.nan
            if not pd.isna(atr) and not pd.isna(atr_mean) and atr_mean != 0:
                compression = atr / atr_mean
            compression_ok = (not pd.isna(compression)) and (compression <= 0.80)

            breakout_level = (recent_high + (0.25 * atr)) if not pd.isna(atr) else np.nan
            prev_close = float(df_15m["close"].iloc[-2])
            prev_not_break = (not pd.isna(atr)) and (prev_close <= (recent_high + (0.05 * atr)))

            breakout = (close > breakout_level) if not pd.isna(breakout_level) else False
            rng = (high - low)
            close_strength = True if rng == 0 else ((close - low) / rng) > 0.72
            body = abs(close - open_)
            body_ratio = True if rng == 0 else (body / rng) > 0.55
            strong_candle = (close > open_) and close_strength and body_ratio

            vol_strength = np.nan
            vol_ok = False
            if not pd.isna(vol) and not pd.isna(vol_ma) and vol_ma != 0:
                vol_strength = vol / vol_ma
                vol_ok = vol > (vol_ma * 1.6)

            req_lines.append(f"{_yn(trend_ok)} Trend sağlam")
            req_lines.append(f"{_yn(rsi_ok)} RSI dengeli")
            req_lines.append(f"{_yn(range_ok)} Sıkışma aralığı uygun")
            req_lines.append(f"{_yn(compression_ok)} Volatilite düşmüş (comp={_fmt_num(compression,2)})")
            req_lines.append(f"{_yn(prev_not_break)} Kırılım yeni")
            req_lines.append(f"{_yn(breakout)} Yukarı kırılım")
            req_lines.append(f"{_yn(strong_candle)} Güçlü kırılım mumu")
            req_lines.append(f"{_yn(vol_ok)} Hacim patlaması • HacimGücü={_fmt_x(vol_strength,2)}")

        else:
            return ""

        if not req_lines:
            return ""
        out = []
        out.append("🧩 <b>KRİTERLER</b>")
        out.extend(req_lines)
        return "\n".join(out)

    except Exception as e:
        print(f"⚠️ Explain Block Hatası: {e}", flush=True)
        return ""

# ===================== 15) ANA MOTOR (2 KATMAN) =====================
def run_bot_engine():
    print("🚀 Sniper Bot v7.1 BAŞLATILDI (2 Katman + Ban-Safe)", flush=True)
    bot_status["status"] = "Aktif"

    wait_until_next_15m_close_tr()

    while True:
        try:
            utc_now = datetime.now(timezone.utc)
            tr_time = utc_now.astimezone(TR_TZ)
            time_str = tr_time.strftime('%H:%M:%S')

            print(f"\n🔎 [KATMAN-1] {time_str} TR - Tüm coin'lerde 15m sinyal arama", flush=True)
            bot_status["last_run"] = time_str

            heartbeat["loop"] += 1
            heartbeat["progress"] = "START"
            heartbeat["last_symbol"] = None
            beat(force_print=True)

            gc.collect()

            symbols = get_tradable_symbols()
            if not symbols:
                print("⚠️ Tradable coin bulunamadı - 60 sn bekleniyor...", flush=True)
                sleep_with_heartbeat(60, status="No Symbols Wait")
                continue

            print(f"✅ TARAMA BAŞLADI | toplam={len(symbols)} coin", flush=True)

            reject = defaultdict(int)
            sent_by_type = defaultdict(int)
            candidates = []

            max_cooldown_min = max(COOLDOWN_MINUTES, PULLBACK_COOLDOWN_MIN, REACCU_COOLDOWN_MIN)
            to_remove = [k for k, t in list(signal_history.items()) if (utc_now - t) > timedelta(minutes=max_cooldown_min)]
            for k in to_remove:
                del signal_history[k]

            macro_regime, _df_btc_1h = get_macro_regime()
            df_btc_15m = get_cached_btc_data('15m')
            if df_btc_15m is None or len(df_btc_15m) < 120:
                df_btc_15m = None

            count = 0
            total = len(symbols)

            # ---------- KATMAN-1: hafif tarama ----------
            for symbol in symbols:
                count += 1
                utc_now = datetime.now(timezone.utc)
                tr_time = utc_now.astimezone(TR_TZ)

                heartbeat["last_symbol"] = symbol
                heartbeat["progress"] = f"{count}/{total}"
                beat()

                if count % 25 == 0:
                    print(f"-> İlerleme: {count}/{total} ({symbol})", flush=True)

                try:
                    df_15m = get_data(symbol, '15m', limit=260)
                    if df_15m is None or len(df_15m) < 220:
                        reject["no_15m"] += 1
                        continue

                    df_15m = prepare_indicators(df_15m)
                    coin_regime = get_coin_regime_15m(df_15m)

                    # RS filtresi (KATMAN-1'de hafifçe)
                    if USE_RS_FILTER and df_btc_15m is not None:
                        rs_score, rs_1h, rs_4h, rs_12h, rs_24h, coin4h, btc4h = calc_relative_strength(df_15m, df_btc_15m)
                        rs_ok = True
                        if not np.isnan(rs_1h) and rs_1h < RS_MIN_REL_1H:
                            rs_ok = False
                        if not np.isnan(rs_4h) and rs_4h < RS_MIN_REL_4H:
                            rs_ok = False
                        if np.isnan(rs_score) or rs_score < RS_MIN_SCORE:
                            rs_ok = False
                        if macro_regime == "RISK_OFF":
                            if np.isnan(rs_score) or rs_score < RS_RISK_OFF_MIN_SCORE:
                                rs_ok = False
                        if not rs_ok:
                            reject["rs_filter"] += 1
                            continue

                    last = df_15m.iloc[-1]
                    if pd.isna(last['atr']) or last['close'] == 0:
                        reject["atr_nan_or_zero"] += 1
                        continue

                    atr_pct = last['atr'] / last['close']
                    if atr_pct < MIN_ATR_PCT:
                        reject["atr_pct_low"] += 1
                        continue

                    # 3 strateji
                    signal_found = False
                    data = {}

                    if coin_regime == "UPTREND":
                        is_pb, pb_data = strategy_pullback(df_15m)
                        if is_pb:
                            signal_found = True
                            data = pb_data
                        else:
                            is_re, re_data = strategy_reaccumulation(df_15m)
                            if is_re:
                                signal_found = True
                                data = re_data
                            else:
                                is_sfp, sfp_data = strategy_sfp_dynamic(df_15m)
                                if is_sfp:
                                    signal_found = True
                                    data = sfp_data
                    else:
                        is_sfp, sfp_data = strategy_sfp_dynamic(df_15m)
                        if is_sfp:
                            signal_found = True
                            data = sfp_data

                    if not signal_found:
                        reject["no_signal"] += 1
                        continue

                    # Cooldown (KATMAN-1'de bile ele)
                    strategy_key = (symbol, data.get('type', 'UNKNOWN'))
                    cooldown_min = COOLDOWN_MINUTES
                    stype = strategy_key[1].upper()
                    if "PULLBACK" in stype:
                        cooldown_min = PULLBACK_COOLDOWN_MIN
                    elif "RE-ACCUMULATION" in stype:
                        cooldown_min = REACCU_COOLDOWN_MIN
                    if strategy_key in signal_history and (utc_now - signal_history[strategy_key]) < timedelta(minutes=cooldown_min):
                        reject["cooldown"] += 1
                        continue

                    # Aday
                    candidates.append((symbol, data, df_15m, coin_regime, macro_regime))
                    reject["candidate_found"] += 1

                except Exception as e:
                    print(f"⚠️ Hata ({symbol}): {e}", flush=True)
                    continue

            # ---------- KATMAN-2: adaylarda ağır doğrulama ----------
            print(f"\n🔍 [KATMAN-2] Aday sayısı: {len(candidates)}", flush=True)

            if candidates:
                for idx, (symbol, data, df_15m, coin_regime, macro_regime) in enumerate(candidates, 1):
                    print(f"⏳ Aday #{idx}/{len(candidates)}: {symbol} için {L2_DELAY_SEC} sn bekleniyor...", flush=True)
                    sleep_with_heartbeat(L2_DELAY_SEC, status="KATMAN-2 Delay")

                    try:
                        # Spread kontrol
                        spread_ok, spread_msg = check_spread_safety(symbol)
                        if not spread_ok:
                            reject["spread_risk"] += 1
                            print(f"❌ {symbol}: Spread riski nedeniyle elendi", flush=True)
                            continue

                        # 1h trend doğrulama
                        is_aligned, trend_msg = is_1h_trend_aligned(symbol)
                        if not is_aligned:
                            reject["1h_trend_mismatch"] += 1
                            print(f"❌ {symbol}: 1h trend uyumsuzluğu nedeniyle elendi", flush=True)
                            continue

                        # Risk / TP
                        entry_price = float(df_15m['close'].iloc[-1])
                        coin_type_info = data.get('coin_type', 'NORMAL')
                        tp_price, tp_pct, tp_note, sr_support = find_structural_target(df_15m, entry_price, coin_type_info)
                        stop_price = float(data['stop'])

                        atr_now = float(df_15m["atr"].iloc[-1]) if "atr" in df_15m.columns else np.nan
                        if sr_support is not None and not pd.isna(atr_now):
                            buffer = 0.25 * atr_now
                            if stop_price > sr_support:
                                stop_price = float(sr_support - buffer)

                        risk_pct = ((entry_price - stop_price) / entry_price) * 100.0
                        if tp_pct < risk_pct:
                            reject["risk_gt_target"] += 1
                            print(f"❌ {symbol} RED: Risk({risk_pct:.2f}) > Target({tp_pct:.2f})", flush=True)
                            continue

                        # Pozisyon
                        position_usdt, coin_amount, actual_risk_pct = calc_position_size(entry_price, stop_price, ACCOUNT_SIZE, RISK_PERCENT)

                        # Fiyat/likidite
                        last_price = get_last_price(symbol)
                        liquidity_warning = get_liquidity_warning(symbol)

                        entry_s = fmt_price(symbol, entry_price)
                        stop_s  = fmt_price(symbol, stop_price)
                        tp_s    = fmt_price(symbol, tp_price)
                        last_s  = fmt_price(symbol, last_price)

                        # Özet metrikler
                        last_row = df_15m.iloc[-1]
                        atr_pct_val = (atr_now / entry_price) * 100.0 if entry_price and not pd.isna(atr_now) else np.nan

                        vol_strength = np.nan
                        try:
                            v = float(last_row.get("volume", np.nan))
                            vm = float(last_row.get("vol_ma", np.nan))
                            if vm and not np.isnan(v) and not np.isnan(vm):
                                vol_strength = v / vm
                        except Exception:
                            pass

                        compression = np.nan
                        try:
                            a = float(last_row.get("atr", np.nan))
                            am = float(last_row.get("atr_mean", np.nan))
                            if am and not np.isnan(a) and not np.isnan(am):
                                compression = a / am
                        except Exception:
                            pass

                        atr_pct_s = _fmt_pct(atr_pct_val, 2)
                        vol_strength_s = _fmt_x(vol_strength, 2)
                        compression_s = _fmt_num(compression, 2)

                        explain_block = build_explain_block(symbol, df_15m, data) if EXPLAIN_SIGNALS else ""

                        # Cooldown kaydı (KATMAN-2'de kesinle)
                        now_utc = datetime.now(timezone.utc)
                        strategy_key = (symbol, data.get('type', 'UNKNOWN'))
                        signal_history[strategy_key] = now_utc
                        bot_status["signal_count"] += 1

                        # Telegram
                        signal_time_str = datetime.now(TR_TZ).strftime('%H:%M')
                        rr_ratio = (tp_pct / risk_pct) if risk_pct else 0.0

                        msg = f"""
<b>{data['type']}</b>
━━━━━━━━━━━━━━━━━━━━
<b>#{symbol}</b> | <b>Fiyat:</b> {last_s} | 🕒 {signal_time_str}
━━━━━━━━━━━━━━━━━━━━
🧠 <b>BAĞLAM:</b> BTC={macro_regime} | CoinRejimi={coin_regime} | {trend_msg}
{'⚠️ ' + liquidity_warning if liquidity_warning else ''}
{spread_msg}
📌 <b>ÖZET:</b> Volatilite (ATR%)={atr_pct_s} | Hacim Gücü={vol_strength_s} | Sıkışma={compression_s}
💵 <b>GİRİŞ :</b> {entry_s}
🛡️ <b>STOP  :</b> {stop_s} (Risk: %{risk_pct:.2f})
🎯 <b>HEDEF :</b> {tp_s} (Potansiyel: <b>%{tp_pct:.2f}</b>) • <i>{tp_note}</i>
💰 <b>POZİSYON:</b> ${position_usdt:.0f} (~{coin_amount:.2f} adet) | Gerçek Risk: %{actual_risk_pct:.2f} | RR: 1:{rr_ratio:.2f}
📝 <b>NEDEN:</b> {data['desc']}
{explain_block}
""".strip()

                        try:
                            r = requests.post(
                                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                                json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"},
                                timeout=10
                            )
                            if r.status_code != 200:
                                reject["telegram_fail"] += 1
                                print(f"⚠️ Telegram non-200: {r.status_code} | {r.text[:200]}", flush=True)
                            else:
                                reject["sent"] += 1
                                sent_by_type[data['type']] += 1
                                print(f"✅ SİNYAL: {symbol} | {data['type']}", flush=True)
                        except Exception as e:
                            reject["telegram_fail"] += 1
                            print(f"⚠️ Telegram Exception: {e}", flush=True)

                    except Exception as e:
                        print(f"⚠️ Katman-2 Hatası ({symbol}): {e}", flush=True)
                        continue

            # ---------- ÖZET ----------
            print("\n📊 TARAMA SONUÇ ÖZETİ", flush=True)
            print("━━━━━━━━━━━━━━━━━━━━", flush=True)
            if reject.get("candidate_found", 0):
                print(f"🔍 Aday Bulundu                    : {reject['candidate_found']}", flush=True)
            if reject.get("sent", 0):
                print(f"✅ Gönderilen Sinyal                : {reject['sent']}", flush=True)
            if reject.get("1h_trend_mismatch", 0):
                print(f"📉 1h Trend Uyumsuzluğu             : {reject['1h_trend_mismatch']}", flush=True)
            if reject.get("spread_risk", 0):
                print(f"⚠️  Spread Riski                    : {reject['spread_risk']}", flush=True)
            if reject.get("rs_filter", 0):
                print(f"🧲 RS Filter Red                    : {reject['rs_filter']}", flush=True)
            if reject.get("cooldown", 0):
                print(f"⏳ Cooldown Engeli                  : {reject['cooldown']}", flush=True)
            if reject.get("atr_pct_low", 0):
                print(f"⚫ Düşük Volatilite (ATR)            : {reject['atr_pct_low']}", flush=True)
            if reject.get("no_signal", 0):
                print(f"⚪ Kurulum Yok                      : {reject['no_signal']}", flush=True)
            if reject.get("risk_gt_target", 0):
                print(f"⚠️ Risk > Hedef                     : {reject['risk_gt_target']}", flush=True)
            if reject.get("telegram_fail", 0):
                print(f"📡 Telegram Hatası                  : {reject['telegram_fail']}", flush=True)
            if sent_by_type:
                print("📌 Tür Bazlı Gönderim:", flush=True)
                for k, v in sorted(sent_by_type.items(), key=lambda x: x[1], reverse=True)[:10]:
                    print(f"   - {k}: {v}", flush=True)
            print("━━━━━━━━━━━━━━━━━━━━\n", flush=True)

            # Bir sonraki 15m kapanışı bekle
            bot_status["status"] = "Beklemede (15m kapanış)"
            wait_until_next_15m_close_tr()

        except Exception as e:
            print(f"🔥 Kritik Döngü Hatası: {e}", flush=True)
            import traceback
            traceback.print_exc()
            sleep_with_heartbeat(60, status="Critical Error Sleep")

# ===================== 16) BAŞLATICI =====================
if __name__ == "__main__":
    # Uyarılar (sadece ekrana)
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("⚠️ TELEGRAM_TOKEN / TELEGRAM_CHAT_ID yok. Telegram gönderimi çalışmaz.", flush=True)
    if not API_KEY or not API_SECRET:
        print("⚠️ BINANCE_API_KEY / BINANCE_SECRET_KEY yok. Veri çekimi kısıtlı/başarısız olabilir.", flush=True)

    flask_thread = threading.Thread(
        target=app.run,
        kwargs={'host': '0.0.0.0', 'port': int(os.environ.get("PORT", 10000)), 'use_reloader': False}
    )
    flask_thread.daemon = True
    flask_thread.start()

    wd = threading.Thread(target=watchdog, daemon=True)
    wd.start()

    print("🌍 Web Sunucusu Başladı", flush=True)
    print("🛡️ Watchdog aktif (donma olursa otomatik restart)", flush=True)
    print("✅ Aktif Stratejiler: PULLBACK + RE-ACCUMULATION + SFP-GOLD", flush=True)
    print("✅ KATMAN-1: 15m hafif tarama | KATMAN-2: adaylarda spread+1h trend+RR kontrol", flush=True)

    run_bot_engine()
