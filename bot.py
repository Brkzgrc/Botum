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

# --- 1. AYARLAR ---
API_KEY = os.getenv('BINANCE_API_KEY')
API_SECRET = os.getenv('BINANCE_SECRET_KEY')
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

# ATR filtresi (optimize)
MIN_ATR_PCT = 0.0018
COOLDOWN_MINUTES = 120
PULLBACK_COOLDOWN_MIN = 360  # 6 saat
REACCU_COOLDOWN_MIN = 480  # 8 saat

IGNORED_COINS = [
    'UP/USDT', 'DOWN/USDT', 'BEAR/USDT', 'BULL/USDT',
    'USDC/USDT', 'TUSD/USDT', 'FDUSD/USDT', 'DAI/USDT', 'USDP/USDT',
    'EUR/USDT', 'TRY/USDT', 'GBP/USDT', 'BUSD/USDT', 'USTC/USDT',
    'PAXG/USDT', 'WBTC/USDT', 'USDE/USDT', 'BRL/USDT', 'RUB/USDT',
    'AUD/USDT', 'UST/USDT', 'USD/USDT', 'XUSD/USDT', 'USD1/USDT',
]

MACRO_SYMBOL = "BTC/USDT"   # Makro bağlam (BTC)

# --- MACRO REGIME CACHE ---
macro_cache = {}
MACRO_TTL = timedelta(minutes=10)
EXPLAIN_SIGNALS = True  # Telegram kartına kriter detaylarını ekle/kapat

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

def fmt_price(symbol: str, price) -> str:
    """Borsanın fiyat hassasiyetine göre string döndürür."""
    try:
        if price is None:
            return "N/A"
        return exchange.price_to_precision(symbol, float(price))
    except Exception:
        try:
            p = float(price)
        except Exception:
            return "N/A"

        # fallback: küçük fiyatlarda daha fazla ondalık
        if p == 0:
            return "0"
        if p < 0.01:
            return f"{p:.8f}"
        if p < 1:
            return f"{p:.6f}"
        return f"{p:.4f}"

def get_last_price(symbol: str):
    """Güncel (last) fiyatı çeker."""
    try:
        t = exchange.fetch_ticker(symbol)
        return t.get("last", None)
    except Exception:
        return None

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
    return "✅"

def build_explain_block(symbol: str, df_15m: pd.DataFrame, data: dict) -> str:
    """
    Telegram HTML için: KRİTERLER (ZORUNLU) + NOTLAR (ZORUNLU DEĞİL)
    NOT: Telegram HTML'de '<' ve '>' metin içinde sorun çıkarır; bu yüzden &lt; &gt; kullanıyoruz.
    """
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
        upper_band = float(last.get("upper_band", np.nan))

        req_lines = []
        note_lines = []

        # ---------------- SFP ----------------
        if "SFP" in stype:
            scan_window = 50
            past_window = df_15m.iloc[-scan_window:-1] if len(df_15m) >= scan_window else df_15m.iloc[:-1]
            pivot_idx = past_window["low"].idxmin()
            pivot_low = float(past_window.loc[pivot_idx]["low"])
            pivot_rsi = float(df_15m.loc[pivot_idx]["rsi"]) if "rsi" in df_15m.columns else np.nan

            # vol_ratio -> multipliers (aynı mantık)
            if pd.isna(atr_mean) or atr_mean == 0 or pd.isna(atr):
                vol_ratio = 1.0
            else:
                vol_ratio = atr / atr_mean

            sweep_mult = 0.15
            reclaim_mult = 0.25
            wick_mult = 1.5
            vol_mult = 1.2

            coin_tag = "NORMAL"
            if vol_ratio >= 1.25:
                coin_tag = "🔥 Yüksek Volatilite"
                sweep_mult = 0.25
                reclaim_mult = 0.35
                wick_mult = 1.8
            elif vol_ratio <= 0.85:
                coin_tag = "🧊 Düşük Volatilite"
                sweep_mult = 0.10
                reclaim_mult = 0.20
                wick_mult = 1.5

            sweep_limit = pivot_low - (sweep_mult * atr)
            dip_zone = pivot_low + (reclaim_mult * atr)

            swept = low < sweep_limit
            reclaimed = close > dip_zone

            body = abs(close - open_)
            lower_wick = min(close, open_) - low
            strong_wick = True if body == 0 else lower_wick > (body * wick_mult)

            vol_ok = False
            vol_strength = np.nan
            if not pd.isna(vol) and not pd.isna(vol_ma) and vol_ma != 0:
                vol_strength = vol / vol_ma
                vol_ok = vol > (vol_ma * vol_mult)

            # GOLD/SILVER bilgisi (not)
            is_gold = False
            if not pd.isna(rsi) and not pd.isna(pivot_rsi):
                is_gold = rsi >= (pivot_rsi - 3)

            # --- Daha anlaşılır Türkçe açıklamalar (formül değil, anlam) ---
            sweep_txt = f"ATR'nin yaklaşık %{int(sweep_mult*100)}'i kadar aşağı sarkma"
            reclaim_txt = f"ATR'nin yaklaşık %{int(reclaim_mult*100)}'i kadar geri alma"
            wick_txt = f"Alt fitil, gövdenin {wick_mult:.1f} katından büyük"
            vol_txt = f"Hacim, ortalamanın {vol_mult:.1f} katından büyük"

            req_lines.append(f"{_yn(swept)} Dip süpürme görüldü ({sweep_txt})")
            req_lines.append(
                f"• En düşük=<code>{fmt_price(symbol, low)}</code> | Eşik=<code>{fmt_price(symbol, sweep_limit)}</code> | Referans dip=<code>{fmt_price(symbol, pivot_low)}</code>"
            )

            req_lines.append(f"{_yn(reclaimed)} Fiyat hızla geri topladı ({reclaim_txt})")
            req_lines.append(
                f"• Kapanış=<code>{fmt_price(symbol, close)}</code> | Geri alma eşiği=<code>{fmt_price(symbol, dip_zone)}</code>"
            )

            req_lines.append(f"{_yn(strong_wick)} Güçlü alt fitil var ({wick_txt})")
            req_lines.append(
                f"• Alt fitil=<code>{fmt_price(symbol, lower_wick)}</code> | Gövde=<code>{fmt_price(symbol, body)}</code>"
            )

            req_lines.append(f"{_yn(vol_ok)} Hacim onayı var ({vol_txt})")
            req_lines.append(f"• Hacim gücü=<code>{_fmt_x(vol_strength,2)}</code>")

        # ---------------- PULLBACK ----------------
        elif "PULLBACK" in stype:
            prev = df_15m.iloc[-2] if len(df_15m) >= 2 else last
            ema50_prev = float(df_15m["ema50"].iloc[-6]) if len(df_15m) >= 6 else np.nan

            touched_ema = (low <= ema50 * 1.001) if not pd.isna(low) and not pd.isna(ema50) else False
            prev_low = float(prev.get("low", np.nan))
            prev_sweep = (prev_low < ema50 * 0.999) if not pd.isna(prev_low) and not pd.isna(ema50) else False

            rng = (high - low)
            close_strength = True if rng == 0 else ((close - low) / rng) > 0.65
            bounced = (close > ema50) and (close > open_) and close_strength
            rsi_ok = (rsi < 60) if not pd.isna(rsi) else False

            vol_ok = False
            vol_strength = np.nan
            if not pd.isna(vol) and not pd.isna(vol_ma) and vol_ma != 0:
                vol_strength = vol / vol_ma
                vol_ok = vol > (vol_ma * 1.2)

            slope_ok = (ema50 > ema50_prev) if not pd.isna(ema50_prev) else True
            trend_ok = (ema50 > ema200) if not pd.isna(ema200) else False

            req_lines.append(f"{_yn(slope_ok)} EMA50 eğimi yukarı")
            req_lines.append(f"• EMA50 önce=<code>{_fmt_num(ema50_prev,4)}</code> | şimdi=<code>{_fmt_num(ema50,4)}</code>")

            req_lines.append(f"{_yn(trend_ok)} Trend: <code>EMA50 &gt; EMA200</code>")
            req_lines.append(f"• EMA50=<code>{_fmt_num(ema50,4)}</code> | EMA200=<code>{_fmt_num(ema200,4)}</code>")

            req_lines.append(f"{_yn(touched_ema)} EMA'ya dokunma: <code>low &lt;= EMA50*1.001</code>")
            req_lines.append(f"• low=<code>{fmt_price(symbol, low)}</code> | eşik=<code>{fmt_price(symbol, ema50*1.001)}</code>")

            req_lines.append(f"{_yn(prev_sweep)} Ön sarkma: <code>prev_low &lt; EMA50*0.999</code>")
            req_lines.append(f"• prev_low=<code>{fmt_price(symbol, prev_low)}</code> | eşik=<code>{fmt_price(symbol, ema50*0.999)}</code>")

            req_lines.append(f"{_yn(bounced)} Bounce: yeşil + güçlü kapanış")
            req_lines.append(f"{_yn(rsi_ok)} RSI &lt; 60: <code>{_fmt_num(rsi,2)}</code>")

            req_lines.append(f"{_yn(vol_ok)} Hacim Onayı: <code>hacim &gt; ort_hacim*1.2</code>")
            req_lines.append(f"• Hacim Gücü=<code>{_fmt_x(vol_strength,2)}</code>")

        # ---------------- RE-ACCUMULATION ----------------
        elif "RE-ACCUMULATION" in stype:
            if len(df_15m) < 20:
                return ""

            # strategy_reaccumulation ile aynı parametreler
            lookback = 16
            recent_window = df_15m.iloc[-lookback:-1]

            recent_high = float(recent_window["high"].max())
            recent_low  = float(recent_window["low"].min())
            range_height = recent_high - recent_low

            # Trend filtresi: close > ema50 ve ema50 > ema200
            trend_ok = (close > ema50) and (ema50 > ema200)

            # RSI filtresi: 45-70
            rsi_ok = (not pd.isna(rsi)) and (45 <= rsi <= 70)

            # Range filtresi: <= 2.8*ATR
            range_ok = (not pd.isna(atr)) and (range_height <= (2.8 * atr))

            # Sıkışma: ATR/Ort <= 0.80
            compression = np.nan
            if not pd.isna(atr) and not pd.isna(atr_mean) and atr_mean != 0:
                compression = atr / atr_mean
            compression_ok = (not pd.isna(compression)) and (compression <= 0.80)

            # Breakout seviyesi: recent_high + 0.25*ATR
            breakout_level = (recent_high + (0.25 * atr)) if not pd.isna(atr) else np.nan

            # Önceki kapanış filtre: bir önceki kapanış "kırmamış" olsun
            prev_close = float(df_15m["close"].iloc[-2])
            prev_not_break = (not pd.isna(atr)) and (prev_close <= (recent_high + (0.05 * atr)))

            breakout = (close > breakout_level) if not pd.isna(breakout_level) else False

            # Güçlü kırılım mumu: close_strength>0.72, body_ratio>0.55, yeşil
            rng = (high - low)
            close_strength = True if rng == 0 else ((close - low) / rng) > 0.72
            body = abs(close - open_)
            body_ratio = True if rng == 0 else (body / rng) > 0.55
            strong_candle = (close > open_) and close_strength and body_ratio

            # Hacim: volume > vol_ma*1.6
            vol_strength = np.nan
            vol_ok = False
            if not pd.isna(vol) and not pd.isna(vol_ma) and vol_ma != 0:
                vol_strength = vol / vol_ma
                vol_ok = vol > (vol_ma * 1.6)

            req_lines.append(f"{_yn(trend_ok)} Trend: <code>close &gt; EMA50</code> ve <code>EMA50 &gt; EMA200</code>")
            req_lines.append(f"• close=<code>{fmt_price(symbol, close)}</code> | EMA50=<code>{fmt_price(symbol, ema50)}</code> | EMA200=<code>{fmt_price(symbol, ema200)}</code>")

            req_lines.append(f"{_yn(rsi_ok)} RSI bölgesi: <code>45–70</code>")
            req_lines.append(f"• RSI=<code>{_fmt_num(rsi,2)}</code>")

            req_lines.append(f"{_yn(range_ok)} Sıkışık aralık: <code>range &lt;= 2.8*ATR</code>")
            req_lines.append(f"• range=<code>{fmt_price(symbol, range_height)}</code> | limit=<code>{fmt_price(symbol, 2.8*atr)}</code>")

            req_lines.append(f"{_yn(compression_ok)} Sıkışma Oranı: <code>ATR/Ort &lt;= 0.80</code>")
            req_lines.append(f"• ATR/Ort=<code>{_fmt_num(compression,2)}</code>")

            req_lines.append(f"{_yn(prev_not_break)} Ön onay: <code>prev_close &lt;= recent_high + 0.05*ATR</code>")
            req_lines.append(f"• prev_close=<code>{fmt_price(symbol, prev_close)}</code> | eşik=<code>{fmt_price(symbol, (recent_high + 0.05*atr) if not pd.isna(atr) else np.nan)}</code>")

            req_lines.append(f"{_yn(breakout)} Kırılım: <code>close &gt; recent_high + 0.25*ATR</code>")
            req_lines.append(f"• close=<code>{fmt_price(symbol, close)}</code> | eşik=<code>{fmt_price(symbol, breakout_level)}</code>")

            req_lines.append(f"{_yn(strong_candle)} Güçlü mum: <code>yeşil</code> + <code>güçlü kapanış</code> + <code>gövde oranı</code>")

            req_lines.append(f"{_yn(vol_ok)} Hacim Onayı: <code>hacim &gt; ort_hacim*1.6</code>")
            req_lines.append(f"• Hacim Gücü=<code>{_fmt_x(vol_strength,2)}</code>")

        # ---------------- SQUEEZE BREAKOUT ----------------
        elif "SQUEEZE BREAKOUT" in stype or "BREAKOUT" in stype:
            breakout = close > upper_band if not pd.isna(upper_band) else False
            vol_strength = (vol / vol_ma) if not pd.isna(vol) and not pd.isna(vol_ma) and vol_ma != 0 else np.nan
            vol_ok = (vol > (vol_ma * 2.0)) if not pd.isna(vol) and not pd.isna(vol_ma) else False

            req_lines.append(f"{_yn(breakout)} Kırılım: <code>close &gt; üst_band</code>")
            req_lines.append(f"• close=<code>{fmt_price(symbol, close)}</code> | üst_band=<code>{fmt_price(symbol, upper_band)}</code>")

            req_lines.append(f"{_yn(vol_ok)} Hacim Patlaması: <code>hacim &gt; ort_hacim*2.0</code>")
            req_lines.append(f"• Hacim Gücü=<code>{_fmt_x(vol_strength,2)}</code>")

        # ---------------- BOMB ----------------
        elif "BOMB" in stype or data.get("coin_type") == "BOMB":
            compression = np.nan
            if not pd.isna(atr) and not pd.isna(atr_mean) and atr_mean != 0:
                compression = atr / atr_mean

            rsi_ok = (55 <= rsi <= 68) if not pd.isna(rsi) else False

            vol_strength = (vol / vol_ma) if not pd.isna(vol) and not pd.isna(vol_ma) and vol_ma != 0 else np.nan
            vol_ok = (vol > (vol_ma * 1.5)) if not pd.isna(vol) and not pd.isna(vol_ma) else False

            recent_high = df_15m["high"].iloc[-20:-1].max() if len(df_15m) >= 20 else np.nan
            no_fake = (close <= recent_high * 1.03) if not pd.isna(recent_high) else False

            req_lines.append(f"{_yn(close > ema200)} Trend: <code>close &gt; EMA200</code>")
            req_lines.append(f"• close=<code>{fmt_price(symbol, close)}</code> | EMA200=<code>{fmt_price(symbol, ema200)}</code>")

            req_lines.append(f"{_yn(compression <= 0.75)} Sıkışma Oranı: <code>ATR/Ort &lt;= 0.75</code>")
            req_lines.append(f"• ATR/Ort=<code>{_fmt_num(compression,2)}</code>")

            req_lines.append(f"{_yn(rsi_ok)} RSI bölgesi: <code>55–68</code>")
            req_lines.append(f"• RSI=<code>{_fmt_num(rsi,2)}</code>")

            req_lines.append(f"{_yn(vol_ok)} Hacim Öncü Artış: <code>hacim &gt; ort_hacim*1.5</code>")
            req_lines.append(f"• Hacim Gücü=<code>{_fmt_x(vol_strength,2)}</code>")

            req_lines.append(f"{_yn(no_fake)} Fake yok: <code>close &lt;= son_tepe*1.03</code>")
            req_lines.append(f"• close=<code>{fmt_price(symbol, close)}</code> | limit=<code>{fmt_price(symbol, recent_high*1.03 if not pd.isna(recent_high) else np.nan)}</code>")

        else:
            return ""

        out = []
        if req_lines:
            out.append("🧩 <b>KRİTERLER (ZORUNLU)</b>")
            out.extend(req_lines)
        if note_lines:
            out.append("")
            out.append("ℹ️ <b>NOTLAR (ZORUNLU DEĞİL)</b>")
            out.extend(note_lines)

        return "\n".join(out)

    except Exception as e:
        print(f"⚠️ Explain Block Hatası: {e}", flush=True)
        return ""

app = Flask(__name__)
signal_history = {}  # key: (symbol, strategy_type)  value: datetime(utc)

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
# --- HEARTBEAT / WATCHDOG AYARLARI ---
HEARTBEAT_UPDATE_SEC = 30      # watchdog için: 30 sn'de bir iç heartbeat güncelle
WATCHDOG_STALE_SEC = 300       # 5 dk güncelleme yoksa restart

# Log'a ALIVE basma modu:
# 3600 = saat başı
# 14400 = 4 saatte bir
ALIVE_PRINT_EVERY_SEC = 14400

# 4 saat hizası:
# "TR_4H_03"  -> TR 03:00 bazlı (03/07/11/15/19/23)
# "UTC_4H_00" -> UTC 00:00 bazlı (00/04/08/12/16/20)
ALIVE_ALIGNMENT = "TR_4H_03"

_last_update_ts = 0.0
_last_print_ts = 0.0

def _seconds_until_next_aligned_ping(now_utc: datetime) -> int:
    """
    TR_4H_03: TR saatine göre 03:00 bazlı 4H kapanışları
    UTC_4H_00: UTC 00:00 bazlı 4H kapanışları
    """
    if ALIVE_ALIGNMENT == "UTC_4H_00":
        base = now_utc
        base_hour = 0
    else:
        tr = now_utc.astimezone(timezone(timedelta(hours=3)))
        base = tr
        base_hour = 3

    h = base.hour
    offset = (h - base_hour) % 4
    next_hour = h - offset + 4

    next_day = base
    if next_hour >= 24:
        next_hour -= 24
        next_day = base + timedelta(days=1)

    target = next_day.replace(hour=next_hour, minute=0, second=0, microsecond=0)
    sec = int((target - base).total_seconds())
    return max(sec, 0)


def beat(force_print=False):
    global _last_update_ts, _last_print_ts

    now_ts = time.time()
    utc_now = datetime.now(timezone.utc)
    tr_now = utc_now.astimezone(timezone(timedelta(hours=3)))

    # (A) Watchdog için iç heartbeat güncelle (sık)
    if (now_ts - _last_update_ts) >= HEARTBEAT_UPDATE_SEC:
        heartbeat["last_beat_utc"] = utc_now.strftime("%Y-%m-%d %H:%M:%S")
        heartbeat["last_beat_tr"] = tr_now.strftime("%Y-%m-%d %H:%M:%S")
        heartbeat["status"] = bot_status.get("status", "UNKNOWN")
        _last_update_ts = now_ts

    # (B) Log'a ALIVE basma (seyrek)
    should_print = False

    if force_print:
        should_print = True
    else:
        if ALIVE_PRINT_EVERY_SEC == 3600:
            # saat başı (TR)
            if tr_now.minute == 0 and tr_now.second < 5 and (now_ts - _last_print_ts) > 55:
                should_print = True

        elif ALIVE_PRINT_EVERY_SEC == 14400:
            # 4 saat hizalı (TR_4H_03 veya UTC_4H_00)
            sec_to_next = _seconds_until_next_aligned_ping(utc_now)
            if sec_to_next <= 5 and (now_ts - _last_print_ts) > 60:
                should_print = True

        else:
            # düz periyot
            if (now_ts - _last_print_ts) >= ALIVE_PRINT_EVERY_SEC:
                should_print = True

    if should_print:
        print(
            f"💓 ALIVE | loop={heartbeat.get('loop')} | {heartbeat.get('progress')} | "
            f"{heartbeat.get('last_symbol')} | TR={heartbeat.get('last_beat_tr')}",
            flush=True
        )
        _last_print_ts = now_ts


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
            and s.isascii()
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
        # EMA / ATR / RSI
        df['ema50'] = ta.ema(df['close'], length=50)
        df['ema200'] = ta.ema(df['close'], length=200)
        df['atr'] = ta.atr(df['high'], df['low'], df['close'], length=14)
        df['atr_mean'] = df['atr'].rolling(100, min_periods=50).mean()
        df['rsi'] = ta.rsi(df['close'], length=14)

        # vol_ma: Bunu BB'den önce üret (BB hata verse bile vol_ma kalsın)
        df['vol_ma'] = df['volume'].rolling(20, min_periods=1).mean()

        # Bollinger Bands (robust)
        bb = ta.bbands(df['close'], length=20, std=2)

        upper = None
        if bb is not None and hasattr(bb, "columns"):
            # 'BBU' içeren kolonları bul (sürüm farklarına dayanıklı)
            ucols = [c for c in bb.columns if 'BBU' in str(c).upper()]
            if ucols:
                upper = bb[ucols[0]]  # ilk eşleşeni al

        # Eğer yine yoksa, en azından kırılmadan devam et
        if upper is None:
            # fallback: close üzerinden basit bir üst band yaklaşımı (çok nadir devreye girer)
            m = df['close'].rolling(20, min_periods=1).mean()
            s = df['close'].rolling(20, min_periods=1).std(ddof=0)
            upper = m + 2 * s

        df['upper_band'] = upper
        return df

    except Exception as e:
        print(f"⚠️ Indicator Hatası: {e}", flush=True)
        return df

def get_coin_regime_15m(df_15m):
    """
    Coin'in kendi 15m rejimi: EMA50/200 + ADX ile basit sınıflandırma.
    """
    try:
        if df_15m is None or len(df_15m) < 210:
            return "NEUTRAL"

        # ADX 15m
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

        # BB squeeze yaklaşımı (hazır upper_band var ama alt band yok -> basit)
        return "RANGING"
    except:
        return "NEUTRAL"

def get_macro_regime(_symbol_unused=None):
    """
    Makro bağlamı BTC üzerinden çıkarır.
    NOT: Bu bir 'hard filter' değildir. Aşağıda sadece 'etiket/risk modu' olarak kullanacağız.
    """
    try:
        now = datetime.now(timezone.utc)

        # Cache tek anahtar: BTC
        key = "__BTC__"
        if key in macro_cache:
            regime, ts = macro_cache[key]
            if now - ts < MACRO_TTL:
                return regime, None

        # BTC 1h verisi (EMA200 için 260+)
        df_1h = get_data(MACRO_SYMBOL, '1h', limit=260)
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

        # ADX kolon seçimi
        adx_col = None
        if hasattr(adx_df, "columns"):
            cands = [c for c in adx_df.columns if str(c).upper().startswith("ADX")]
            if cands:
                adx_col = cands[0]
        if adx_col is None:
            macro_cache[key] = ("NEUTRAL", now)
            return "NEUTRAL", None

        adx = adx_df[adx_col].iloc[-1]

        # BB kolon seçimi
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

        # Rejim
        if close < ema50 and close < ema200 and adx > 20:
            regime = "RISK_OFF"     # BTC zayıf -> altlarda risk artar
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
        ema50_prev = df_15m['ema50'].iloc[-6]
        if pd.isna(ema50_prev):
            return False, None        
        if pd.isna(ema50):
            return False, None
        # EMA50 yukarı eğimli olmalı
        if ema50 <= ema50_prev:
            return False, None
        ema200 = last['ema200']
        rsi = last['rsi']
        if pd.isna(ema200):
            return False, None
        if not (ema50 > ema200):
            return False, None

        # 1) EMA'ya gerçekten dokunma (daha sıkı)
        touched_ema = last['low'] <= ema50 * 1.001
        
        # 2) Bounce onayı: sadece yeşil mum yetmez, bir önceki mum EMA altına sarkmış olmalı
        prev = df_15m.iloc[-2]
        prev_sweep = prev['low'] < ema50 * 0.999
        
        # 3) Kapanış EMA üstünde + güçlü kapanış (kapanış, mum aralığının üst kısmında)
        rng = (last['high'] - last['low'])
        close_strength = True if rng == 0 else ((last['close'] - last['low']) / rng) > 0.65
        bounced = (last['close'] > ema50) and (last['close'] > last['open']) and close_strength
        
        # 4) RSI daha seçici
        not_overbought = rsi < 60
        
        # Hacim filtresi (spam keser)
        vol_ok = (not pd.isna(last['vol_ma'])) and (last['volume'] > (last['vol_ma'] * 1.2))
        
        if touched_ema and prev_sweep and bounced and not_overbought and vol_ok:
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
        ema50 = last.get('ema50', np.nan)
        ema200 = last.get('ema200', np.nan)
        atr = last.get('atr', np.nan)
        atr_mean = last.get('atr_mean', np.nan)
        rsi = last.get('rsi', np.nan)

        if pd.isna(ema50) or pd.isna(ema200) or pd.isna(atr) or pd.isna(atr_mean) or atr_mean == 0:
            return False, None

        # Trend filtresi: daha net
        if last['close'] < ema50 or ema50 <= ema200:
            return False, None

        # RSI: re-accu için "çok şişmiş" olmasın
        if pd.isna(rsi) or not (45 <= rsi <= 70):
            return False, None

        # Daha uzun bayrak / sıkışma penceresi
        lookback = 16
        recent_window = df_15m.iloc[-lookback:-1]

        # Gerçek sıkışma: aralık ATR'ye göre küçük olmalı
        range_height = recent_window['high'].max() - recent_window['low'].min()
        if range_height > (2.8 * atr):
            return False, None

        # Volatilite gerçekten düşmüş olmalı (asıl spam kesen filtre)
        compression = atr / atr_mean
        if compression > 0.80:
            return False, None

        # Breakout seviyesi: daha anlamlı marj
        recent_high = recent_window['high'].max()
        breakout_level = recent_high + (0.25 * atr)

        # "Tek seferlik" kırılım: bir önceki kapanış kırmamış olsun
        prev_close = df_15m['close'].iloc[-2]
        if prev_close > (recent_high + 0.05 * atr):
            return False, None

        breakout = last['close'] > breakout_level

        # Kırılım mumu güçlü kapatsın (kapanış, mumun üst bölgesinde)
        rng = (last['high'] - last['low'])
        close_strength = True if rng == 0 else ((last['close'] - last['low']) / rng) > 0.72
        body = abs(last['close'] - last['open'])
        body_ratio = True if rng == 0 else (body / rng) > 0.55
        strong_candle = (last['close'] > last['open']) and close_strength and body_ratio

        # Hacim: patlama arıyoruz (vol_ma üstü yetmiyor)
        vol_ma = last.get('vol_ma', np.nan)
        if pd.isna(vol_ma) or vol_ma == 0:
            return False, None
        vol_ok = last['volume'] > (vol_ma * 1.6)

        if breakout and strong_candle and vol_ok:
            mid_point = (recent_window['high'].max() + recent_window['low'].min()) / 2
            tight_stop = mid_point - (0.6 * atr)
            return True, {
                'type': '🚩 RE-ACCUMULATION (PRO)',
                'desc': f'Trend içi bayrak kırılımı. Sıkışma: {compression:.2f}',
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
            beat(force_print=True)

            gc.collect()

            symbols = get_tradable_symbols()
            reject = defaultdict(int)
            sent_by_type = defaultdict(int)

            # --- STRATEJİ BAZLI COOLDOWN TEMİZLİĞİ ---
            max_cooldown_min = max(COOLDOWN_MINUTES, PULLBACK_COOLDOWN_MIN, REACCU_COOLDOWN_MIN)
            to_remove = [
                k for k, t in signal_history.items()
                if (utc_now - t) > timedelta(minutes=max_cooldown_min)
            ]
            for k in to_remove:
                del signal_history[k]

            count = 0
            total = len(symbols)

            # Makro rejimi tarama başına 1 kez al (cache var ama gereksizi azaltır)
            macro_regime, _df_btc = get_macro_regime()

            for symbol in symbols:
                count += 1
                utc_now = datetime.now(timezone.utc)
                tr_time = utc_now.astimezone(timezone(timedelta(hours=3)))

                heartbeat["last_symbol"] = symbol
                heartbeat["progress"] = f"{count}/{total}"
                beat()

                if count % 20 == 0:
                    print(f"-> İlerleme: {count}/{total} ({symbol})", flush=True)

                try:
                    # --- Veri ---
                    df_15m = get_data(symbol, '15m', limit=260)
                    if df_15m is None:
                        reject["no_15m"] += 1
                        continue

                    df_15m = prepare_indicators(df_15m)
                    coin_regime = get_coin_regime_15m(df_15m)

                    last = df_15m.iloc[-1]
                    if pd.isna(last['atr']) or last['close'] == 0:
                        reject["atr_nan_or_zero"] += 1
                        continue

                    atr_pct = last['atr'] / last['close']
                    if atr_pct < MIN_ATR_PCT:
                        reject["atr_pct_low"] += 1
                        continue

                    # --- Strateji seçimi ---
                    signal_found = False
                    data = {}

                    if coin_regime in ["RANGING", "NEUTRAL"]:
                        if symbol in bomb_history and (utc_now - bomb_history[symbol] < BOMB_COOLDOWN):
                            reject["bomb_cooldown"] += 1
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

                    elif coin_regime == "UPTREND":
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
                            reject["coin_downtrend_no_signal"] += 1
                            continue

                    if not signal_found:
                        reject["no_signal"] += 1
                        continue

                    # --- STRATEJİ BAZLI COOLDOWN (SÜRELİ) ---
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

                    signal_history[strategy_key] = utc_now

                    # Sayaç cooldown sonrası artsın
                    bot_status["signal_count"] += 1

                    # --- BOMB için 24 saat cooldown kaydı ---
                    if data.get('coin_type') == 'BOMB':
                        bomb_history[symbol] = utc_now

                    # --- Risk/Target hesabı ---
                    entry_price = df_15m['close'].iloc[-1]
                    coin_type_info = data.get('coin_type', 'NORMAL')
                    tp_price, tp_pct = find_structural_target(df_15m, entry_price, coin_type_info)
                    stop_price = data['stop']
                    risk_pct = ((entry_price - stop_price) / entry_price) * 100

                    if tp_pct < risk_pct:
                        reject["risk_gt_target"] += 1
                        print(f"❌ {symbol} RED: Risk({risk_pct:.2f}) > Target({tp_pct:.2f})", flush=True)
                        continue

                    # --- Güncel fiyat + Precision format ---
                    last_price = get_last_price(symbol)

                    entry_s = fmt_price(symbol, entry_price)
                    stop_s  = fmt_price(symbol, stop_price)
                    tp_s    = fmt_price(symbol, tp_price)
                    last_s  = fmt_price(symbol, last_price)

                    # --- Özet metrikleri (kart için) ---
                    atr_pct_val = np.nan
                    vol_strength = np.nan
                    compression = np.nan

                    try:
                        c = float(last.get("close", np.nan))
                        a = float(last.get("atr", np.nan))
                        if c and not np.isnan(a) and not np.isnan(c):
                            atr_pct_val = (a / c) * 100.0
                    except Exception:
                        pass

                    try:
                        v = float(last.get("volume", np.nan))
                        vm = float(last.get("vol_ma", np.nan))
                        if vm and not np.isnan(v) and not np.isnan(vm):
                            vol_strength = v / vm
                    except Exception:
                        pass

                    try:
                        a = float(last.get("atr", np.nan))
                        am = float(last.get("atr_mean", np.nan))
                        if am and not np.isnan(a) and not np.isnan(am):
                            compression = a / am
                    except Exception:
                        pass

                    atr_pct_s = _fmt_pct(atr_pct_val, 2)
                    vol_strength_s = _fmt_x(vol_strength, 2)
                    compression_s = _fmt_num(compression, 2)

                    explain_block = build_explain_block(symbol, df_15m, data) if EXPLAIN_SIGNALS else ""

                    # --- Telegram ---
                    signal_time_str = tr_time.strftime('%H:%M')
                    msg = f"""
<b>{data['type']}</b>
━━━━━━━━━━━━━━━━━━━━
<b>#{symbol}</b> | <b>Fiyat:</b> <code>{last_s}</code> | 🕒 {signal_time_str}
━━━━━━━━━━━━━━━━━━━━
🧠 <b>BAĞLAM:</b> BTC=<code>{macro_regime}</code> | CoinRejimi=<code>{coin_regime}</code>
📌 <b>ÖZET:</b> Volatilite (ATR%)=<code>{atr_pct_s}</code> | Hacim Gücü=<code>{vol_strength_s}</code> | Sıkışma Oranı (ATR/Ort)=<code>{compression_s}</code>

💵 <b>GİRİŞ :</b> <code>{entry_s}</code>
🛡️ <b>STOP  :</b> <code>{stop_s}</code> (Risk: %{risk_pct:.2f})
🎯 <b>HEDEF :</b> <code>{tp_s}</code> (Potansiyel: <b>%{tp_pct:.2f}</b>)

📝 <b>NEDEN:</b> {data['desc']}
{explain_block}
"""
                    try:
                        r = requests.post(
                            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                            json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"},
                            timeout=10
                        )

                        if r.status_code != 200:
                            reject["telegram_fail"] += 1
                            print(f"⚠️ Telegram non-200: {r.status_code} | {r.text[:300]}", flush=True)
                        else:
                            reject["sent"] += 1
                            print(f"✅ SİNYAL: {symbol} | {data['type']}", flush=True)
                            sent_by_type[data['type']] += 1

                    except Exception as e:
                        reject["telegram_fail"] += 1
                        print(f"⚠️ Telegram Exception: {e}", flush=True)

                except Exception as e:
                    print(f"⚠️ Hata ({symbol}): {e}", flush=True)
                    continue

            print("\n📊 TARAMA SONUÇ ÖZETİ", flush=True)
            print("━━━━━━━━━━━━━━━━━━━━", flush=True)
            
            if reject.get("sent", 0):
                print(f"✅ Gönderilen Sinyal                  : {reject['sent']}", flush=True)
            
            if reject.get("atr_pct_low", 0):
                print(f"⚫ Düşük Volatilite (ATR yetersiz)     : {reject['atr_pct_low']}", flush=True)
            
            if reject.get("coin_downtrend_no_signal", 0):
                print(f"🔻 Düşüş Trendinde + Sinyal Yok        : {reject['coin_downtrend_no_signal']}", flush=True)
            
            if reject.get("no_signal", 0):
                print(f"⚪ Kurulum Yok (Setup oluşmadı)        : {reject['no_signal']}", flush=True)
            
            if reject.get("cooldown", 0):
                print(f"⏳ Cooldown Engeli (Spam koruması)     : {reject['cooldown']}", flush=True)
            
            if reject.get("risk_gt_target", 0):
                print(f"⚠️ Risk > Hedef (RR uygun değil)       : {reject['risk_gt_target']}", flush=True)
            
            if reject.get("telegram_fail", 0):
                print(f"📡 Telegram Gönderim Hatası            : {reject['telegram_fail']}", flush=True)
            
            if sent_by_type:
                print("📌 Tür Bazlı Gönderim:", flush=True)
                for k, v in sorted(sent_by_type.items(), key=lambda x: x[1], reverse=True)[:10]:
                    print(f"   - {k}: {v}", flush=True)

            print("━━━━━━━━━━━━━━━━━━━━\n", flush=True)
            print("🏁 Tarama Bitti. 5 dakika bekleniyor...", flush=True)
            beat(force_print=True)

            # 5 dakika bekleniyor ama watchdog tetiklenmesin diye heartbeat'li uyku
            bot_status["status"] = "Beklemede (5dk)"
            sleep_total = 300
            step = 10  # 10 saniyede bir beat
            for _ in range(sleep_total // step):
                time.sleep(step)
                beat()  # heartbeat güncelle

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
