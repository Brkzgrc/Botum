#!/usr/bin/env python3
"""
Full Backtest — 23 Senaryo
9 Sinyal Sistemi × (3 SMC çıkışı veya 2 Bot çıkışı) = 23 Bağımsız Test
Her senaryo: $5.000 başlangıç, maks 10 eş zamanlı pozisyon, dinamik boyutlama
Çıktı: self-contained HTML (karşılaştırma tablosu + Chart.js equity eğrileri)

Kullanım:
  python full_backtest.py              # CoinGecko top 50
  python full_backtest.py --n 20       # İlk 20 coin
  python full_backtest.py --no-fetch   # Önbellekten yükle
  python full_backtest.py --coins BTC ETH SOL
"""
import argparse, json, os, pickle, time, math, sys
from datetime import datetime, timezone
import ccxt, numpy as np, pandas as pd

# ═══════════════════════════════════════════════════════════════
# KONFİGÜRASYON
# ═══════════════════════════════════════════════════════════════
START_TS          = int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
DATA_DIR          = "backtest_data"
INITIAL_CAPITAL   = 5_000.0
MAX_POSITIONS     = 10

SMC_TRAIL_PCT     = 2.5   # Half-open sonrası trailing (portfolio_tracker SMC_TRAIL_PCT)
BOT_TRAIL_PCT     = 3.0   # Bot trailing (portfolio_tracker TRAIL_PCT)

CHOCH_SWING       = 5
SWING_LENGTH      = 50
PHASE1_DEPTH      = 85.0
PHASE1_RSI        = 30.0
ESKI_DISCOUNT_DEPTH = 5.0
MIN_VOL_24H       = 5_000_000
BTC_CRASH_PCT     = 3.0

PANIK_CRASH_MIN   = -15.0
PANIK_CRASH_MAX   = -7.0
PANIK_VOL_MIN     = 1.5
PANIK_VOL_MAX     = 3.0

ROCKET_CHANGE_MIN = 10.0
ROCKET_VOL_MIN    = 1.2
ROCKET_ADX_MIN    = 25.0

EXPIRE_H = {
    "eski_choch":     168,
    "eski_choch_v2":  168,
    "eski_discount":  168,
    "smc_orig_disc":  168,
    "smc_orig_choch": 168,
    "panik_pump":      24,
    "t72":             72,
    "t168":           168,
    "rocket":          48,
}

COOLDOWN_H = {
    "eski_choch":     24,
    "eski_choch_v2":  24,
    "eski_discount":  24,
    "smc_orig_disc":  24,
    "smc_orig_choch": 24,
    "panik_pump":      4,
    "t72":             4,
    "t168":            4,
    "rocket":         12,
}

SMC_SYSTEMS  = ["eski_choch", "eski_choch_v2", "eski_discount",
                "smc_orig_disc", "smc_orig_choch"]
BOT_SYSTEMS  = ["panik_pump", "t72", "t168", "rocket"]

IGNORED_COINS = {
    'UP/USDT','DOWN/USDT','BEAR/USDT','BULL/USDT',
    'USDC/USDT','TUSD/USDT','FDUSD/USDT','DAI/USDT','USDP/USDT',
    'USDE/USDT','UST/USDT','USD/USDT','XUSD/USDT','USD1/USDT','BFUSD/USDT',
    'USTC/USDT','BUSD/USDT','FRAX/USDT','LUSD/USDT','GUSD/USDT','SUSD/USDT',
    'USDS/USDT','USDX/USDT','USDD/USDT','CUSD/USDT','OUSD/USDT','MUSD/USDT',
    'RLUSD/USDT','U/USDT',
    'EUR/USDT','TRY/USDT','GBP/USDT','BRL/USDT','RUB/USDT',
    'AUD/USDT','BIDR/USDT','IDRT/USDT','VAI/USDT',
    'PAXG/USDT','XAUT/USDT','WBTC/USDT','WETH/USDT','WBNB/USDT','BETH/USDT',
    'BTCB/USDT','HBTC/USDT',
}
LEVERAGED_PATTERNS = ["UP","DOWN","BULL","BEAR","3L","3S","2L","2S","5L","5S","10L","10S"]


# ═══════════════════════════════════════════════════════════════
# VERİ ÇEKME & ÖNBELLEKLEME
# ═══════════════════════════════════════════════════════════════
def get_top_coins(n=50):
    import urllib.request
    url = ("https://api.coingecko.com/api/v3/coins/markets"
           "?vs_currency=usd&order=market_cap_desc&per_page=250&page=1&sparkline=false")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read())
    ex = ccxt.binance({"enableRateLimit": True})
    ex.load_markets()
    binance_pairs = set(ex.markets.keys())
    result = []
    for coin in data:
        sym = coin["symbol"].upper() + "/USDT"
        if sym in IGNORED_COINS: continue
        base = coin["symbol"].upper()
        if any(p in base for p in LEVERAGED_PATTERNS): continue
        if sym not in binance_pairs: continue
        result.append(sym)
        if len(result) >= n: break
    if "BTC/USDT" not in result:
        result.insert(0, "BTC/USDT")
    print(f"CoinGecko top {n}: {len(result)} coin seçildi")
    return result


def fetch_ohlcv_full(symbol):
    ex = ccxt.binance({"enableRateLimit": True})
    bars = []; since = START_TS
    while True:
        batch = ex.fetch_ohlcv(symbol, "1h", since=since, limit=1000)
        if not batch: break
        bars.extend(batch)
        if len(batch) < 1000: break
        since = batch[-1][0] + 1
        time.sleep(0.2)
    if not bars: return None
    df = pd.DataFrame(bars, columns=["timestamp","open","high","low","close","volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df.set_index("timestamp", inplace=True)
    return df[~df.index.duplicated(keep="first")]


def cache_path(symbol):
    os.makedirs(DATA_DIR, exist_ok=True)
    return os.path.join(DATA_DIR, symbol.replace("/", "_") + ".pkl")


def load_or_fetch(symbol, force=False):
    p = cache_path(symbol)
    if not force and os.path.exists(p):
        with open(p, "rb") as f: return pickle.load(f)
    df = fetch_ohlcv_full(symbol)
    if df is not None:
        with open(p, "wb") as f: pickle.dump(df, f)
    return df


# ═══════════════════════════════════════════════════════════════
# BTC FİLTRELERİ (backtest.py'den)
# ═══════════════════════════════════════════════════════════════
def compute_btc_filters(btc_1h):
    df4 = btc_1h.resample("4h", label="right", closed="right").agg(
        {"open":"first","high":"max","low":"min","close":"last","volume":"sum"}
    ).dropna(subset=["close"])
    n4 = len(df4)
    h4 = df4["high"].values; l4 = df4["low"].values; c4 = df4["close"].values
    crash_ok = np.ones(n4, dtype=bool)
    for i in range(1, n4):
        crash_ok[i] = (c4[i] / c4[i-1] - 1) * 100 > -BTC_CRASH_PCT
    legs4 = np.zeros(n4, dtype=int); cur4 = 0
    for i in range(CHOCH_SWING, n4):
        ph = h4[i - CHOCH_SWING]; pl = l4[i - CHOCH_SWING]
        wh = h4[i-CHOCH_SWING+1:i+1].max(); wl = l4[i-CHOCH_SWING+1:i+1].min()
        if ph > wh: cur4 = 0
        elif pl < wl: cur4 = 1
        legs4[i] = cur4
    sh4=None; shx4=True; sl4=None; slx4=True; prev_sl4=None; trend4=0
    downtrend_active = np.zeros(n4, dtype=bool)
    for i in range(CHOCH_SWING+1, n4):
        if legs4[i] != legs4[i-1]:
            if legs4[i] == 1:
                prev_sl4 = sl4; sl4 = l4[i-CHOCH_SWING]; slx4 = False
            else:
                sh4 = h4[i-CHOCH_SWING]; shx4 = False
        ci, cp = c4[i], c4[i-1]
        if sh4 is not None and not shx4 and ci > sh4 and cp <= sh4: shx4 = True; trend4 = 1
        if sl4 is not None and not slx4 and ci < sl4 and cp >= sl4: slx4 = True; trend4 = -1
        if trend4 == -1 and (prev_sl4 is None or (sl4 is not None and sl4 <= prev_sl4)):
            downtrend_active[i] = True
    df4["crash_ok"] = crash_ok; df4["downtrend_active"] = downtrend_active
    idx = btc_1h.index
    crash_s    = df4["crash_ok"].reindex(idx, method="ffill").fillna(True).astype(bool)
    downtrend_s = df4["downtrend_active"].reindex(idx, method="ffill").fillna(False).astype(bool)
    return pd.DataFrame({"crash_ok": crash_s, "downtrend_ok": ~downtrend_s}, index=idx)


# ═══════════════════════════════════════════════════════════════
# ARTIMLI CHoCH TESPİTİ (backtest.py'den)
# ═══════════════════════════════════════════════════════════════
def run_choch_incremental(df, choch_swing=CHOCH_SWING):
    h = df["high"].values; l = df["low"].values; c = df["close"].values
    n = len(df)
    legs = np.zeros(n, dtype=int); cur = 0
    for i in range(choch_swing, n):
        ph = h[i-choch_swing]; pl = l[i-choch_swing]
        wh = h[i-choch_swing+1:i+1].max(); wl = l[i-choch_swing+1:i+1].min()
        if ph > wh: cur = 0
        elif pl < wl: cur = 1
        legs[i] = cur
    sh=None; shx=True; sl=None; slx=True; trend=0
    bts=[None]*n; bds=[None]*n; cls_=[None]*n; swls=[None]*n
    for i in range(choch_swing+1, n):
        if legs[i] != legs[i-1]:
            if legs[i] == 1: sl = l[i-choch_swing]; slx = False
            else: sh = h[i-choch_swing]; shx = False
        ci, cp = c[i], c[i-1]
        bt=None; bd=None; cl=None
        if sh is not None and not shx and ci > sh and cp <= sh:
            bt = "CHoCH" if trend == -1 else "BOS"; bd = "BULLISH"; cl = sh
            shx = True; trend = 1
        if sl is not None and not slx and ci < sl and cp >= sl:
            bt = "CHoCH" if trend == 1 else "BOS"; bd = "BEARISH"; cl = sl
            slx = True; trend = -1
        bts[i]=bt; bds[i]=bd; cls_[i]=cl; swls[i]=sl
    return bts, bds, cls_, swls


# ═══════════════════════════════════════════════════════════════
# ARTIMLI LUXALGO SMC (discount zone per-bar)
# ═══════════════════════════════════════════════════════════════
def compute_luxalgo_incremental(df, swing_length=SWING_LENGTH):
    n = len(df)
    h = df["high"].values; l = df["low"].values; c = df["close"].values
    legs = np.zeros(n, dtype=int); cur = 0
    for i in range(swing_length, n):
        ph = h[i-swing_length]; pl = l[i-swing_length]
        wh = h[i-swing_length+1:i+1].max(); wl = l[i-swing_length+1:i+1].min()
        if ph > wh: cur = 0
        elif pl < wl: cur = 1
        legs[i] = cur
    disc_top = np.full(n, np.nan); disc_bot = np.full(n, np.nan)
    depth_arr = np.full(n, np.nan)
    t_top = None; t_bot = None
    for i in range(swing_length, n):
        prev_leg = legs[i-1] if i > 0 else 0
        curr_leg = legs[i]
        if curr_leg != prev_leg:
            if curr_leg == 1: t_bot = l[i-swing_length]
            elif curr_leg == 0: t_top = h[i-swing_length]
        if t_top is not None and h[i] > t_top: t_top = h[i]
        if t_bot is not None and l[i] < t_bot: t_bot = l[i]
        if t_top is not None and t_bot is not None and t_top != t_bot:
            disc_top[i] = 0.55 * t_top + 0.45 * t_bot
            disc_bot[i] = t_bot
            depth_arr[i] = (t_top - c[i]) / (t_top - t_bot) * 100
    return disc_top, disc_bot, depth_arr


# ═══════════════════════════════════════════════════════════════
# İNDİKATÖRLER
# ═══════════════════════════════════════════════════════════════
def compute_indicators(df):
    df = df.copy()
    c = df["close"]; h = df["high"]; l = df["low"]
    prev_c = c.shift(1)
    tr = pd.concat([h - l, (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    df["atr"] = tr.ewm(alpha=1/14, adjust=False).mean()
    delta = c.diff()
    gain  = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
    loss  = (-delta).clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
    df["rsi"] = 100 - 100 / (1 + gain / loss.replace(0, np.nan))
    df["ema21"]      = c.ewm(span=21, adjust=False).mean()
    df["ma50"]       = c.rolling(50).mean()
    df["ma200"]      = c.rolling(200).mean()
    df["ma200_slope"] = (df["ma200"] - df["ma200"].shift(20)) / df["ma200"].shift(20).abs().replace(0, np.nan) * 100
    df["dist_ema21"] = (c - df["ema21"])  / df["ema21"].replace(0, np.nan)  * 100
    df["dist_ma50"]  = (c - df["ma50"])   / df["ma50"].replace(0, np.nan)   * 100
    df["dist_ma200"] = (c - df["ma200"])  / df["ma200"].replace(0, np.nan)  * 100
    df["mom5_pct"]   = (c - c.shift(5))   / c.shift(5).abs().replace(0, np.nan)   * 100
    df["mom10_pct"]  = (c - c.shift(10))  / c.shift(10).abs().replace(0, np.nan)  * 100
    roll_max         = c.rolling(2500, min_periods=50).max()
    df["coin_drawdown"] = (c - roll_max) / roll_max.replace(0, np.nan) * 100
    bar_idx          = pd.Series(np.arange(len(df)), index=df.index)
    is_at_high       = c >= roll_max * (1 - 1e-6)
    last_high_pos    = bar_idx.where(is_at_high).ffill().fillna(0)
    df["bars_since_high"] = bar_idx - last_high_pos
    vol_ma20         = df["volume"].rolling(20).mean()
    df["vol_ratio_20"] = df["volume"] / vol_ma20.shift(1)
    df["close_prev"] = c.shift(1)
    df["vol24_usd"]  = (c * df["volume"]).rolling(24).sum()
    return df


def compute_adx_series(df, period=14):
    h = df["high"].values.astype(float)
    l = df["low"].values.astype(float)
    c = df["close"].values.astype(float)
    n = len(c)
    tr_arr = np.zeros(n); dm_p = np.zeros(n); dm_m = np.zeros(n)
    for i in range(1, n):
        tr_arr[i] = max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
        up = h[i]-h[i-1]; down = l[i-1]-l[i]
        dm_p[i] = up   if (up > down and up > 0) else 0.0
        dm_m[i] = down if (down > up and down > 0) else 0.0
    def wilder(arr, p):
        s = np.zeros(len(arr))
        if p < len(arr): s[p] = np.sum(arr[1:p+1])
        for i in range(p+1, len(arr)):
            s[i] = s[i-1] - s[i-1]/p + arr[i]
        return s
    atr_s = wilder(tr_arr, period); dmp_s = wilder(dm_p, period); dmm_s = wilder(dm_m, period)
    with np.errstate(divide="ignore", invalid="ignore"):
        di_p = np.where(atr_s > 0, dmp_s / atr_s * 100, 0.0)
        di_m = np.where(atr_s > 0, dmm_s / atr_s * 100, 0.0)
        dx   = np.where((di_p+di_m) > 0, np.abs(di_p-di_m)/(di_p+di_m)*100, 0.0)
    adx = wilder(dx, period)
    return adx, di_p, di_m


# ═══════════════════════════════════════════════════════════════
# ÇIKIŞ SİMÜLASYONLARI
# ═══════════════════════════════════════════════════════════════
def sim_smc_actual(fh, fl, fc, entry, stop, tp1, tp2, expire_h):
    """SMC actual: TP1→half_open (50%), kalan 50% trailing (SMC_TRAIL_PCT)."""
    n = min(len(fh), expire_h)
    if n == 0: return 0.0, "expired", 0
    tp1_p  = (tp1 - entry) / entry * 100
    tp2_p  = (tp2 - entry) / entry * 100
    stop_p = (stop - entry) / entry * 100
    peak = entry; half_open = False
    for i in range(n):
        if not half_open:
            if fl[i] <= stop:
                return stop_p, "loss", i + 1
            if fh[i] >= tp1 and tp2 > tp1:
                half_open = True
                peak = max(entry, fh[i])
        else:
            if fh[i] > peak: peak = fh[i]
            trail_stop = peak * (1 - SMC_TRAIL_PCT / 100)
            if fh[i] >= tp2:
                combined = (tp1_p + tp2_p) / 2
                return combined, "win", i + 1
            if fl[i] <= trail_stop:
                trail_p = (trail_stop - entry) / entry * 100
                combined = (tp1_p + trail_p) / 2
                return combined, "trail", i + 1
    last_p = (fc[n-1] - entry) / entry * 100 if n > 0 else 0.0
    if half_open:
        return (tp1_p + last_p) / 2, "expired", n
    return last_p, "expired", n


def sim_smc_tp1_only(fh, fl, fc, entry, stop, tp1, expire_h):
    """SMC tp1_only: stop→loss, TP1→win, expire at close."""
    n = min(len(fh), expire_h)
    tp1_p  = (tp1 - entry) / entry * 100
    stop_p = (stop - entry) / entry * 100
    for i in range(n):
        if fl[i] <= stop: return stop_p, "loss", i + 1
        if fh[i] >= tp1:  return tp1_p, "win", i + 1
    last_p = (fc[n-1] - entry) / entry * 100 if n > 0 else 0.0
    return last_p, "expired", n


def sim_smc_tp2_only(fh, fl, fc, entry, stop, tp2, expire_h):
    """SMC tp2_only: stop→loss, TP2→win, expire at close."""
    n = min(len(fh), expire_h)
    tp2_p  = (tp2 - entry) / entry * 100
    stop_p = (stop - entry) / entry * 100
    for i in range(n):
        if fl[i] <= stop: return stop_p, "loss", i + 1
        if fh[i] >= tp2:  return tp2_p, "win", i + 1
    last_p = (fc[n-1] - entry) / entry * 100 if n > 0 else 0.0
    return last_p, "expired", n


def sim_bot_actual(fh, fl, fc, entry, stop, tp1, tp2, expire_h):
    """
    Bot actual: trailing stop (%3) from entry. TP2→win, trail hit→exit, expire→close.
    Mirrors portfolio_tracker bot exit logic.
    """
    n = min(len(fh), expire_h)
    if n == 0: return 0.0, "expired", 0
    tp2_p = (tp2 - entry) / entry * 100 if tp2 else None
    peak = entry
    for i in range(n):
        if fh[i] > peak: peak = fh[i]
        trail_stop = peak * (1 - BOT_TRAIL_PCT / 100)
        if tp2 and fh[i] >= tp2:
            return tp2_p, "win", i + 1
        if fl[i] <= trail_stop:
            trail_p = (trail_stop - entry) / entry * 100
            outcome = "win" if trail_p > 0 else "loss"
            return trail_p, outcome, i + 1
    last_p = (fc[n-1] - entry) / entry * 100 if n > 0 else 0.0
    return last_p, "expired", n


def sim_bot_tp1_only(fh, fl, fc, entry, stop, tp1, expire_h):
    """Bot tp1_only: signal stop→loss, TP1→win, expire at close."""
    n = min(len(fh), expire_h)
    tp1_p  = (tp1 - entry) / entry * 100
    stop_p = (stop - entry) / entry * 100
    for i in range(n):
        if fl[i] <= stop: return stop_p, "loss", i + 1
        if fh[i] >= tp1:  return tp1_p, "win", i + 1
    last_p = (fc[n-1] - entry) / entry * 100 if n > 0 else 0.0
    return last_p, "expired", n


# ═══════════════════════════════════════════════════════════════
# SİNYAL TESPİTİ
# ═══════════════════════════════════════════════════════════════
def collect_signals_for_coin(symbol, df, df_ind, disc_top, disc_bot, depth_arr,
                             btc_f, btc_f_aligned, adx_arr, di_p_arr, di_m_arr):
    """
    Bir coin için tüm 9 sistemin sinyallerini toplar.
    Returns: {system_name: [(bar_idx, ts_sec, entry, stop, tp1, tp2), ...]}
    """
    n = len(df)
    ts_arr = np.array([t.timestamp() for t in df.index])
    c_arr  = df["close"].values
    h_arr  = df["high"].values
    l_arr  = df["low"].values

    last_sig = {sys: 0.0 for sys in list(SMC_SYSTEMS) + list(BOT_SYSTEMS)}

    bts, bds, cls_, swls = run_choch_incremental(df)

    rsi_arr  = df_ind["rsi"].values
    atr_arr  = df_ind["atr"].values
    vol_ratio_arr = df_ind["vol_ratio_20"].values
    vol24_arr     = df_ind["vol24_usd"].values
    close_prev    = df_ind["close_prev"].values

    dist_ema21_arr  = df_ind["dist_ema21"].values
    dist_ma50_arr   = df_ind["dist_ma50"].values
    dist_ma200_arr  = df_ind["dist_ma200"].values
    ma200_slope_arr = df_ind["ma200_slope"].values
    mom5_arr        = df_ind["mom5_pct"].values
    mom10_arr       = df_ind["mom10_pct"].values
    drawdown_arr    = df_ind["coin_drawdown"].values
    bars_high_arr   = df_ind["bars_since_high"].values

    # 4H EMA21 for SMC Original CHoCH confirm
    try:
        df4 = df.resample("4h", label="right", closed="right").agg(
            {"open":"first","high":"max","low":"min","close":"last","volume":"sum"}
        ).dropna()
        ema21_4h = df4["close"].ewm(span=21, adjust=False).mean()
        above_ema21_4h = (df4["close"] > ema21_4h).reindex(df.index, method="ffill").fillna(True)
    except Exception:
        above_ema21_4h = pd.Series(True, index=df.index)
    above_ema21_4h_arr = above_ema21_4h.values

    # SMC Original: disc_active flag (Phase 1 → Phase 2 state)
    disc_active = False

    result = {sys: [] for sys in list(SMC_SYSTEMS) + list(BOT_SYSTEMS)}

    for i in range(250, n - 1):  # at least 250 bars warmup; leave 1 bar for future
        ts_h = ts_arr[i] / 3600  # hours since epoch
        price = c_arr[i]

        if np.isnan(price) or price <= 0:
            continue

        # ── BTC filtreler ────────────────────────────────────────
        bf = btc_f_aligned.iloc[i]
        btc_crash_ok    = bool(bf["crash_ok"])
        btc_downtrend_ok = bool(bf["downtrend_ok"])

        # ── Discount zone ────────────────────────────────────────
        dt = disc_top[i]; db = disc_bot[i]; dep = depth_arr[i]
        has_discount = not (np.isnan(dt) or np.isnan(db) or np.isnan(dep))
        in_discount_zone = has_discount and price <= dt
        deep_discount = (has_discount and in_discount_zone
                         and dep >= PHASE1_DEPTH
                         and not np.isnan(rsi_arr[i]) and rsi_arr[i] <= PHASE1_RSI)

        # ── ATR (Eski Discount fallback stop/tp) ─────────────────
        atr = atr_arr[i] if not np.isnan(atr_arr[i]) else price * 0.05

        # ── CHoCH state ──────────────────────────────────────────
        is_bullish_choch = (bts[i] == "CHoCH" and bds[i] == "BULLISH")
        choch_lvl = cls_[i]; swing_low = swls[i]
        vol_ratio = vol_ratio_arr[i]

        # ── 1. Eski CHoCH ────────────────────────────────────────
        sys = "eski_choch"
        if (is_bullish_choch and btc_crash_ok and btc_downtrend_ok
                and ts_h - last_sig[sys] >= COOLDOWN_H[sys]):
            entry = choch_lvl if choch_lvl is not None else price
            stop  = swing_low * 0.995 if swing_low is not None else entry * 0.95
            if stop >= entry: stop = entry * 0.95
            risk  = entry - stop
            if risk <= 0: risk = entry * 0.05
            tp1 = entry + risk; tp2 = entry + risk * 2
            result[sys].append((i, ts_arr[i], entry, stop, tp1, tp2))
            last_sig[sys] = ts_h

        # ── 2. Eski CHoCH V2 (vol >= 1.5x) ──────────────────────
        sys = "eski_choch_v2"
        if (is_bullish_choch and btc_crash_ok and btc_downtrend_ok
                and not np.isnan(vol_ratio) and vol_ratio >= 1.5
                and ts_h - last_sig[sys] >= COOLDOWN_H[sys]):
            entry = choch_lvl if choch_lvl is not None else price
            stop  = swing_low * 0.995 if swing_low is not None else entry * 0.95
            if stop >= entry: stop = entry * 0.95
            risk  = entry - stop
            if risk <= 0: risk = entry * 0.05
            tp1 = entry + risk; tp2 = entry + risk * 2
            result[sys].append((i, ts_arr[i], entry, stop, tp1, tp2))
            last_sig[sys] = ts_h

        # ── 3. Eski Discount ─────────────────────────────────────
        sys = "eski_discount"
        if (has_discount and price <= db * (1 + ESKI_DISCOUNT_DEPTH / 100)
                and ts_h - last_sig[sys] >= COOLDOWN_H[sys]):
            entry = price
            stop  = entry - atr * 4.0
            tp1   = entry + atr * 4.0
            tp2   = entry + atr * 6.0
            if stop < entry and tp1 > entry:
                result[sys].append((i, ts_arr[i], entry, stop, tp1, tp2))
                last_sig[sys] = ts_h

        # ── 4. SMC Original Discount (Phase 1 as entry) ──────────
        sys = "smc_orig_disc"
        if (deep_discount and btc_crash_ok
                and ts_h - last_sig[sys] >= COOLDOWN_H[sys]):
            # Mark disc_active for Phase 2 (carried over from below)
            entry = price
            stop  = entry - atr * 4.0
            tp1   = entry + atr * 4.0
            tp2   = entry + atr * 6.0
            if stop < entry and tp1 > entry:
                result[sys].append((i, ts_arr[i], entry, stop, tp1, tp2))
                last_sig[sys] = ts_h

        # ── Update disc_active state ──────────────────────────────
        if deep_discount and btc_crash_ok:
            disc_active = True

        # ── 5. SMC Original CHoCH (Phase 2) ─────────────────────
        sys = "smc_orig_choch"
        if (disc_active and is_bullish_choch and btc_crash_ok
                and above_ema21_4h_arr[i]
                and ts_h - last_sig[sys] >= COOLDOWN_H[sys]):
            entry = choch_lvl if choch_lvl is not None else price
            stop  = swing_low * 0.995 if swing_low is not None else entry * 0.95
            if stop >= entry: stop = entry * 0.95
            risk  = entry - stop
            if risk <= 0: risk = entry * 0.05
            tp1 = entry + risk; tp2 = entry + risk * 2
            result[sys].append((i, ts_arr[i], entry, stop, tp1, tp2))
            last_sig[sys] = ts_h
            disc_active = False  # Reset after Phase 2 signal

        # ── 6. PANİK PUMP ────────────────────────────────────────
        sys = "panik_pump"
        if (not np.isnan(close_prev[i]) and close_prev[i] > 0
                and ts_h - last_sig[sys] >= COOLDOWN_H[sys]):
            ret1 = (price / close_prev[i] - 1) * 100
            if (PANIK_CRASH_MIN <= ret1 <= PANIK_CRASH_MAX
                    and not np.isnan(vol_ratio) and PANIK_VOL_MIN <= vol_ratio <= PANIK_VOL_MAX
                    and price >= df["open"].values[i]):  # close >= open (green candle)
                # Not in deep 5-bar drawdown: check 5-bar ago not >4% above current
                c5ago = c_arr[i-5] if i >= 5 else price
                if c5ago <= 0 or (c5ago - price) / c5ago * 100 < 4.0:
                    entry = price
                    stop  = entry * 0.97
                    tp1   = entry * 1.05
                    tp2   = entry * 1.10
                    result[sys].append((i, ts_arr[i], entry, stop, tp1, tp2))
                    last_sig[sys] = ts_h

        # ── 7. T72 ───────────────────────────────────────────────
        sys = "t72"
        if (not np.isnan(mom5_arr[i]) and not np.isnan(dist_ema21_arr[i])
                and not np.isnan(drawdown_arr[i]) and not np.isnan(ma200_slope_arr[i])
                and mom5_arr[i] >= 2.740 and dist_ema21_arr[i] <= -2.737
                and drawdown_arr[i] >= -26.796 and ma200_slope_arr[i] >= 1.028
                and ts_h - last_sig[sys] >= COOLDOWN_H[sys]):
            entry = price
            stop  = entry * 0.95
            tp1   = entry * 1.10
            tp2   = entry * 1.15
            result[sys].append((i, ts_arr[i], entry, stop, tp1, tp2))
            last_sig[sys] = ts_h

        # ── 8. T168 ──────────────────────────────────────────────
        sys = "t168"
        if (not np.isnan(dist_ma200_arr[i]) and not np.isnan(dist_ma50_arr[i])
                and not np.isnan(mom10_arr[i]) and not np.isnan(bars_high_arr[i])
                and dist_ma200_arr[i] >= 5.657 and dist_ma50_arr[i] <= -5.045
                and mom10_arr[i] >= 3.941 and bars_high_arr[i] <= 677
                and ts_h - last_sig[sys] >= COOLDOWN_H[sys]):
            entry = price
            stop  = entry * 0.92
            tp1   = entry * 1.25
            tp2   = entry * 1.25  # same as tp1 for T168
            result[sys].append((i, ts_arr[i], entry, stop, tp1, tp2))
            last_sig[sys] = ts_h

        # ── 9. ROCKET ────────────────────────────────────────────
        sys = "rocket"
        if (i >= 25 and not np.isnan(adx_arr[i]) and not np.isnan(di_p_arr[i])
                and adx_arr[i] >= ROCKET_ADX_MIN and di_p_arr[i] > di_m_arr[i]
                and btc_downtrend_ok
                and ts_h - last_sig[sys] >= COOLDOWN_H[sys]):
            c24h = c_arr[i-24] if i >= 24 else None
            if c24h and c24h > 0:
                change_24h = (price / c24h - 1) * 100
                if change_24h >= ROCKET_CHANGE_MIN:
                    vol_avg = df["volume"].values[max(0,i-20):i].mean()
                    vol_now = df["volume"].values[i]
                    if vol_avg > 0 and vol_now >= vol_avg * ROCKET_VOL_MIN:
                        entry = price
                        stop  = entry * 0.95
                        tp1   = entry * 1.08
                        tp2   = entry * 1.15
                        result[sys].append((i, ts_arr[i], entry, stop, tp1, tp2))
                        last_sig[sys] = ts_h

    return result


# ═══════════════════════════════════════════════════════════════
# PORTFÖLİO SİMÜLASYONU
# ═══════════════════════════════════════════════════════════════
def simulate_portfolio(raw_signals, price_data, exit_fn,
                       initial_cash=INITIAL_CAPITAL, max_pos=MAX_POSITIONS):
    """
    raw_signals: list of (bar_idx, ts_sec, entry, stop, tp1, tp2, coin, expire_h)
    price_data:  {coin: {'high': array, 'low': array, 'close': array, 'ts': array}}
    exit_fn:     function(fh, fl, fc, entry, stop, tp1, tp2, expire_h) → (pnl_pct, outcome, bars)
    Returns dict with results + equity_curve for Chart.js
    """
    # Pre-compute exit for every signal
    processed = []
    for bar_idx, sig_ts, entry, stop, tp1, tp2, coin, expire_h in raw_signals:
        pd_coin = price_data.get(coin)
        if pd_coin is None: continue
        fh = pd_coin["high"][bar_idx+1:]
        fl = pd_coin["low"][bar_idx+1:]
        fc = pd_coin["close"][bar_idx+1:]
        ft = pd_coin["ts"][bar_idx+1:]
        pnl, outcome, bars = exit_fn(fh, fl, fc, entry, stop, tp1, tp2, expire_h)
        exit_ts = ft[bars-1] if bars > 0 and bars <= len(ft) else (
                  ft[-1] if len(ft) > 0 else sig_ts + expire_h * 3600)
        processed.append({
            "entry_ts": sig_ts, "exit_ts": exit_ts,
            "pnl_pct": pnl, "outcome": outcome,
            "coin": coin, "entry": entry, "stop": stop, "tp1": tp1, "tp2": tp2,
        })

    processed.sort(key=lambda x: x["entry_ts"])

    cash = initial_cash
    positions = []
    trades = []
    equity_curve = []

    def close_expired(before_ts):
        nonlocal cash
        still_open = []
        for pos in positions:
            if pos["exit_ts"] <= before_ts:
                profit = pos["cost"] * pos["pnl_pct"] / 100
                cash += pos["cost"] + profit
                trades.append({**pos, "closed_cash": cash})
                equity_curve.append((pos["exit_ts"], round(cash, 2)))
            else:
                still_open.append(pos)
        positions[:] = still_open

    for sig in processed:
        close_expired(sig["entry_ts"])
        if len(positions) < max_pos and cash > 1.0:
            pos_size = cash / max_pos
            cost = min(pos_size, cash)
            cash -= cost
            positions.append({
                **sig, "cost": cost,
            })

    # Close all remaining positions at their exit time
    positions.sort(key=lambda x: x["exit_ts"])
    for pos in positions:
        profit = pos["cost"] * pos["pnl_pct"] / 100
        cash += pos["cost"] + profit
        trades.append({**pos, "closed_cash": cash})
        equity_curve.append((pos["exit_ts"], round(cash, 2)))

    equity_curve.sort(key=lambda x: x[0])

    # Add start & final points
    if processed:
        start_ts = processed[0]["entry_ts"]
        equity_curve = [(start_ts, initial_cash)] + equity_curve

    wins    = [t for t in trades if t["pnl_pct"] > 0]
    losses  = [t for t in trades if t["pnl_pct"] <= 0]
    total_n = len(trades)
    win_n   = len(wins)
    loss_n  = len(losses)
    wr      = round(win_n / total_n * 100, 1) if total_n > 0 else 0.0
    avg_pnl = round(sum(t["pnl_pct"] for t in trades) / total_n, 4) if total_n > 0 else 0.0
    total_ret_pct = round((cash - initial_cash) / initial_cash * 100, 2)

    # Max drawdown from equity curve
    if equity_curve:
        vals = [v for _, v in equity_curve]
        peak = vals[0]; max_dd = 0.0
        for v in vals:
            if v > peak: peak = v
            dd = (peak - v) / peak * 100
            if dd > max_dd: max_dd = dd
    else:
        max_dd = 0.0

    return {
        "final_capital": round(cash, 2),
        "total_return_pct": total_ret_pct,
        "n_trades": total_n,
        "wins": win_n,
        "losses": loss_n,
        "win_rate": wr,
        "avg_pnl": avg_pnl,
        "max_drawdown": round(max_dd, 2),
        "equity_curve": equity_curve,
    }


# ═══════════════════════════════════════════════════════════════
# HTML ÇIKTI
# ═══════════════════════════════════════════════════════════════
SCENARIO_LABELS = {
    ("eski_choch",     "actual"):   "Eski CHoCH — Actual (SMC trail)",
    ("eski_choch",     "tp1_only"): "Eski CHoCH — TP1 Only",
    ("eski_choch",     "tp2_only"): "Eski CHoCH — TP2 Only",
    ("eski_choch_v2",  "actual"):   "Eski CHoCH V2 (vol≥1.5x) — Actual",
    ("eski_choch_v2",  "tp1_only"): "Eski CHoCH V2 (vol≥1.5x) — TP1 Only",
    ("eski_choch_v2",  "tp2_only"): "Eski CHoCH V2 (vol≥1.5x) — TP2 Only",
    ("eski_discount",  "actual"):   "Eski Discount — Actual (SMC trail)",
    ("eski_discount",  "tp1_only"): "Eski Discount — TP1 Only",
    ("eski_discount",  "tp2_only"): "Eski Discount — TP2 Only",
    ("smc_orig_disc",  "actual"):   "SMC Orig Discount — Actual",
    ("smc_orig_disc",  "tp1_only"): "SMC Orig Discount — TP1 Only",
    ("smc_orig_disc",  "tp2_only"): "SMC Orig Discount — TP2 Only",
    ("smc_orig_choch", "actual"):   "SMC Orig CHoCH — Actual",
    ("smc_orig_choch", "tp1_only"): "SMC Orig CHoCH — TP1 Only",
    ("smc_orig_choch", "tp2_only"): "SMC Orig CHoCH — TP2 Only",
    ("panik_pump",     "actual"):   "PANİK PUMP — Actual (Bot trail)",
    ("panik_pump",     "tp1_only"): "PANİK PUMP — TP1 Only",
    ("t72",            "actual"):   "T72 — Actual (Bot trail)",
    ("t72",            "tp1_only"): "T72 — TP1 Only",
    ("t168",           "actual"):   "T168 — Actual (Bot trail)",
    ("t168",           "tp1_only"): "T168 — TP1 Only",
    ("rocket",         "actual"):   "ROCKET — Actual (Bot trail)",
    ("rocket",         "tp1_only"): "ROCKET — TP1 Only",
}

PALETTE = [
    "#e63946","#457b9d","#2a9d8f","#e9c46a","#f4a261",
    "#264653","#a8dadc","#6d6875","#b5838d","#e07a5f",
    "#3d405b","#81b29a","#f2cc8f","#118ab2","#06d6a0",
    "#ef476f","#ffd166","#06d6a0","#118ab2","#073b4c",
    "#8338ec","#3a86ff","#fb5607",
]


def generate_html(all_results, coins_tested, n_signals_total):
    scenarios_ordered = [
        ("eski_choch",     "actual"),   ("eski_choch",     "tp1_only"), ("eski_choch",     "tp2_only"),
        ("eski_choch_v2",  "actual"),   ("eski_choch_v2",  "tp1_only"), ("eski_choch_v2",  "tp2_only"),
        ("eski_discount",  "actual"),   ("eski_discount",  "tp1_only"), ("eski_discount",  "tp2_only"),
        ("smc_orig_disc",  "actual"),   ("smc_orig_disc",  "tp1_only"), ("smc_orig_disc",  "tp2_only"),
        ("smc_orig_choch", "actual"),   ("smc_orig_choch", "tp1_only"), ("smc_orig_choch", "tp2_only"),
        ("panik_pump",     "actual"),   ("panik_pump",     "tp1_only"),
        ("t72",            "actual"),   ("t72",            "tp1_only"),
        ("t168",           "actual"),   ("t168",           "tp1_only"),
        ("rocket",         "actual"),   ("rocket",         "tp1_only"),
    ]

    def fmt_pct(v):
        sign = "+" if v >= 0 else ""
        color = "#00c853" if v >= 0 else "#d32f2f"
        return f'<span style="color:{color}">{sign}{v:.2f}%</span>'

    def fmt_capital(v):
        color = "#00c853" if v >= INITIAL_CAPITAL else "#d32f2f"
        return f'<span style="color:{color}">${v:,.2f}</span>'

    rows = ""
    best_return = max((r["total_return_pct"] for r in all_results.values() if r), default=0)

    for idx, (sys, mode) in enumerate(scenarios_ordered):
        key = f"{sys}__{mode}"
        r = all_results.get(key)
        if r is None:
            rows += f'<tr><td>{SCENARIO_LABELS.get((sys,mode), key)}</td>' + "<td>—</td>"*8 + "</tr>"
            continue
        is_smc = sys in SMC_SYSTEMS
        grp = "SMC" if is_smc else "BOT"
        highlight = ' style="background:#1e2d1e"' if r["total_return_pct"] >= best_return else ""
        rows += (
            f'<tr{highlight}>'
            f'<td><span class="badge badge-{"smc" if is_smc else "bot"}">{grp}</span>'
            f' {SCENARIO_LABELS.get((sys,mode), key)}</td>'
            f'<td>{r["n_trades"]}</td>'
            f'<td>{r["wins"]}/{r["losses"]}</td>'
            f'<td>{r["win_rate"]:.1f}%</td>'
            f'<td>{fmt_pct(r["avg_pnl"])}</td>'
            f'<td>{fmt_pct(r["total_return_pct"])}</td>'
            f'<td>{fmt_capital(r["final_capital"])}</td>'
            f'<td>{fmt_pct(-r["max_drawdown"])}</td>'
            f'<td><input type="checkbox" class="curve-toggle" data-idx="{idx}" checked></td>'
            f'</tr>'
        )

    # Equity curve datasets
    datasets_js = []
    for idx, (sys, mode) in enumerate(scenarios_ordered):
        key = f"{sys}__{mode}"
        r = all_results.get(key)
        if r is None or not r.get("equity_curve"): continue
        ec = r["equity_curve"]
        pts = [{"x": datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d %H:%M"),
                "y": round(val, 2)} for ts, val in ec]
        color = PALETTE[idx % len(PALETTE)]
        label = SCENARIO_LABELS.get((sys, mode), key)
        datasets_js.append(
            f'{{"label":{json.dumps(label)},'
            f'"data":{json.dumps(pts)},'
            f'"borderColor":"{color}",'
            f'"backgroundColor":"{color}20",'
            f'"borderWidth":1.5,"pointRadius":0,"fill":false,'
            f'"tension":0.1}}'
        )
    datasets_str = "[" + ",".join(datasets_js) + "]"

    run_date = datetime.now().strftime("%Y-%m-%d %H:%M")
    html = f"""<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Full Backtest — 23 Senaryo</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3.0.0/dist/chartjs-adapter-date-fns.bundle.min.js"></script>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: #0d1117; color: #c9d1d9; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', monospace; padding: 20px; }}
  h1 {{ color: #58a6ff; font-size: 1.4rem; margin-bottom: 6px; }}
  .meta {{ color: #8b949e; font-size: 0.82rem; margin-bottom: 20px; }}
  .card {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 16px; margin-bottom: 20px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.82rem; }}
  th {{ background: #21262d; color: #8b949e; text-align: left; padding: 8px 10px; position: sticky; top: 0; }}
  td {{ padding: 7px 10px; border-bottom: 1px solid #21262d; }}
  tr:hover td {{ background: #1c2128; }}
  .badge {{ display: inline-block; padding: 2px 6px; border-radius: 4px; font-size: 0.7rem; font-weight: bold; }}
  .badge-smc {{ background: #1a4a6e; color: #58a6ff; }}
  .badge-bot {{ background: #3a1a4e; color: #bc8cff; }}
  canvas {{ max-height: 450px; }}
  .controls {{ margin-bottom: 10px; display: flex; gap: 8px; flex-wrap: wrap; }}
  button {{ background: #21262d; border: 1px solid #30363d; color: #c9d1d9; padding: 5px 12px; border-radius: 5px; cursor: pointer; font-size: 0.8rem; }}
  button:hover {{ background: #30363d; }}
  input[type=checkbox] {{ cursor: pointer; width: 15px; height: 15px; }}
</style>
</head>
<body>
<h1>📊 Full Backtest — 23 Senaryo</h1>
<div class="meta">
  Çalıştırma: {run_date} UTC |
  Test edilen coin: {coins_tested} |
  Toplam sinyal: {n_signals_total:,} |
  Başlangıç sermayesi: ${INITIAL_CAPITAL:,.0f} |
  Maks pozisyon: {MAX_POSITIONS} |
  Dinamik boyutlama: cash / {MAX_POSITIONS} |
  Veri: 2025-01-01'den bugüne, 1H Binance
</div>

<div class="card">
<table>
  <thead>
    <tr>
      <th>Senaryo</th><th>İşlem</th><th>K/Z</th><th>WR%</th>
      <th>Ort P&L%</th><th>Top. Getiri</th><th>Son Sermaye</th>
      <th>Max DD</th><th>Graf.</th>
    </tr>
  </thead>
  <tbody>
    {rows}
  </tbody>
</table>
</div>

<div class="card">
  <div class="controls">
    <button onclick="showAll()">Tümünü Göster</button>
    <button onclick="hideAll()">Tümünü Gizle</button>
    <button onclick="showGroup('SMC')">Sadece SMC</button>
    <button onclick="showGroup('BOT')">Sadece BOT</button>
  </div>
  <canvas id="equityChart"></canvas>
</div>

<script>
const allDatasets = {datasets_str};
const chart = new Chart(document.getElementById('equityChart'), {{
  type: 'line',
  data: {{ datasets: allDatasets }},
  options: {{
    responsive: true,
    animation: false,
    interaction: {{ mode: 'index', intersect: false }},
    plugins: {{
      legend: {{ position: 'bottom', labels: {{ color: '#8b949e', boxWidth: 12, font: {{ size: 10 }} }} }},
      tooltip: {{
        backgroundColor: '#1c2128',
        borderColor: '#30363d', borderWidth: 1,
        titleColor: '#c9d1d9', bodyColor: '#c9d1d9',
        callbacks: {{
          label: ctx => ` ${{ctx.dataset.label}}: $${{ctx.parsed.y.toFixed(2)}}`
        }}
      }}
    }},
    scales: {{
      x: {{
        type: 'time',
        time: {{ tooltipFormat: 'dd MMM yyyy HH:mm', displayFormats: {{ day: 'dd MMM', month: 'MMM yy' }} }},
        ticks: {{ color: '#8b949e', maxTicksLimit: 12 }},
        grid: {{ color: '#21262d' }}
      }},
      y: {{
        ticks: {{ color: '#8b949e', callback: v => '$' + v.toLocaleString() }},
        grid: {{ color: '#21262d' }},
        title: {{ display: true, text: 'Portföy ($)', color: '#8b949e' }}
      }}
    }}
  }}
}});

document.querySelectorAll('.curve-toggle').forEach(cb => {{
  cb.addEventListener('change', function() {{
    const idx = parseInt(this.dataset.idx);
    if (idx < chart.data.datasets.length) {{
      chart.data.datasets[idx].hidden = !this.checked;
      chart.update();
    }}
  }});
}});

function showAll() {{
  chart.data.datasets.forEach((ds, i) => {{ ds.hidden = false; }});
  document.querySelectorAll('.curve-toggle').forEach(cb => cb.checked = true);
  chart.update();
}}
function hideAll() {{
  chart.data.datasets.forEach((ds, i) => {{ ds.hidden = true; }});
  document.querySelectorAll('.curve-toggle').forEach(cb => cb.checked = false);
  chart.update();
}}
function showGroup(grp) {{
  const smc_labels = {json.dumps([SCENARIO_LABELS.get((s,m),s) for s,m in scenarios_ordered if s in SMC_SYSTEMS])};
  const bot_labels = {json.dumps([SCENARIO_LABELS.get((s,m),s) for s,m in scenarios_ordered if s in BOT_SYSTEMS])};
  const show_set = grp === 'SMC' ? new Set(smc_labels) : new Set(bot_labels);
  chart.data.datasets.forEach((ds, i) => {{ ds.hidden = !show_set.has(ds.label); }});
  document.querySelectorAll('.curve-toggle').forEach((cb, i) => {{
    if (i < chart.data.datasets.length) cb.checked = !chart.data.datasets[i].hidden;
  }});
  chart.update();
}}
</script>
</body>
</html>"""
    return html


# ═══════════════════════════════════════════════════════════════
# ANA AKIŞ
# ═══════════════════════════════════════════════════════════════
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-fetch", action="store_true")
    ap.add_argument("--coins", nargs="*")
    ap.add_argument("--n", type=int, default=50)
    ap.add_argument("--out", default="full_backtest_result.html")
    args = ap.parse_args()

    os.makedirs(DATA_DIR, exist_ok=True)

    if args.coins:
        symbols = [s if "/" in s else s + "/USDT" for s in args.coins]
    elif args.no_fetch:
        pkls = sorted(f for f in os.listdir(DATA_DIR) if f.endswith(".pkl"))
        symbols = [f[:-4].replace("_", "/", 1) for f in pkls
                   if f[:-4].replace("_","/",1).endswith("/USDT")
                   and f[:-4].replace("_","/",1) not in IGNORED_COINS]
        print(f"Cache'den {len(symbols)} coin yüklendi")
    else:
        symbols = get_top_coins(args.n)

    if "BTC/USDT" in symbols: symbols.remove("BTC/USDT")
    symbols.insert(0, "BTC/USDT")

    print(f"\n--- BTC/USDT yükleniyor ---")
    btc_raw = load_or_fetch("BTC/USDT")
    if btc_raw is None: print("BTC verisi alınamadı!"); return

    if not args.no_fetch:
        print(f"\n--- Veri İndirme ({len(symbols)} coin) ---")
        for i, sym in enumerate(symbols, 1):
            if sym == "BTC/USDT": continue
            p = cache_path(sym)
            if os.path.exists(p):
                print(f"  [{i}/{len(symbols)}] {sym} — cache var"); continue
            print(f"  [{i}/{len(symbols)}] {sym} indiriliyor...", end=" ", flush=True)
            df = load_or_fetch(sym)
            print(f"✓ ({len(df)} bar)" if df is not None else "HATA")
            time.sleep(0.1)

    print("\n--- BTC filtreleri hesaplanıyor ---")
    btc_f = compute_btc_filters(btc_raw)

    # Tüm sistemler için sinyal havuzu: {system: [(bar_idx,ts,entry,stop,tp1,tp2,coin,expire_h)]}
    all_signals = {sys: [] for sys in SMC_SYSTEMS + BOT_SYSTEMS}
    price_data  = {}
    coins_tested = 0
    total_coins = len(symbols)

    print(f"\n--- Sinyal Tespiti ({total_coins} coin) ---")
    for sym_idx, symbol in enumerate(symbols, 1):
        if symbol == "BTC/USDT": continue
        print(f"  [{sym_idx}/{total_coins}] {symbol}", end=" ", flush=True)
        df_raw = load_or_fetch(symbol)
        if df_raw is None or len(df_raw) < 300:
            print("→ Yetersiz veri, atlanıyor"); continue

        try:
            df_ind = compute_indicators(df_raw)
        except Exception as e:
            print(f"→ İndikatör hatası: {e}"); continue

        if len(df_ind) < 250:
            print("→ İndikatör sonrası yetersiz veri, atlanıyor"); continue

        disc_top, disc_bot, depth_arr = compute_luxalgo_incremental(df_ind)
        adx_arr, di_p_arr, di_m_arr  = compute_adx_series(df_ind)
        btc_f_aligned = btc_f.reindex(df_ind.index, method="ffill").fillna(
            {"crash_ok": True, "downtrend_ok": True})

        coin_signals = collect_signals_for_coin(
            symbol, df_ind, df_ind, disc_top, disc_bot, depth_arr,
            btc_f, btc_f_aligned, adx_arr, di_p_arr, di_m_arr
        )

        # Store price data for simulation
        price_data[symbol] = {
            "high":  df_ind["high"].values,
            "low":   df_ind["low"].values,
            "close": df_ind["close"].values,
            "ts":    np.array([t.timestamp() for t in df_ind.index]),
        }

        sig_counts = []
        for sys, sigs in coin_signals.items():
            for (bar_idx, ts, entry, stop, tp1, tp2) in sigs:
                all_signals[sys].append((bar_idx, ts, entry, stop, tp1, tp2, symbol, EXPIRE_H[sys]))
            if sigs: sig_counts.append(f"{sys.replace('_', '-')}:{len(sigs)}")

        print(f"✓ [{', '.join(sig_counts) if sig_counts else 'sinyal yok'}]")
        coins_tested += 1

    # Sort all signal lists by timestamp
    for sys in all_signals:
        all_signals[sys].sort(key=lambda x: x[1])

    total_signals = sum(len(v) for v in all_signals.values())
    print(f"\nToplam sinyal: {total_signals:,}")
    for sys, sigs in all_signals.items():
        print(f"  {sys:<20}: {len(sigs):4d} sinyal")

    # Çıkış fonksiyonları
    def make_smc_actual_fn():
        def fn(fh, fl, fc, entry, stop, tp1, tp2, expire_h):
            return sim_smc_actual(fh, fl, fc, entry, stop, tp1, tp2, expire_h)
        return fn

    def make_smc_tp1_fn():
        def fn(fh, fl, fc, entry, stop, tp1, tp2, expire_h):
            return sim_smc_tp1_only(fh, fl, fc, entry, stop, tp1, expire_h)
        return fn

    def make_smc_tp2_fn():
        def fn(fh, fl, fc, entry, stop, tp1, tp2, expire_h):
            return sim_smc_tp2_only(fh, fl, fc, entry, stop, tp2, expire_h)
        return fn

    def make_bot_actual_fn():
        def fn(fh, fl, fc, entry, stop, tp1, tp2, expire_h):
            return sim_bot_actual(fh, fl, fc, entry, stop, tp1, tp2, expire_h)
        return fn

    def make_bot_tp1_fn():
        def fn(fh, fl, fc, entry, stop, tp1, tp2, expire_h):
            return sim_bot_tp1_only(fh, fl, fc, entry, stop, tp1, expire_h)
        return fn

    exit_fns = {
        "actual":   {"smc": make_smc_actual_fn(), "bot": make_bot_actual_fn()},
        "tp1_only": {"smc": make_smc_tp1_fn(),    "bot": make_bot_tp1_fn()},
        "tp2_only": {"smc": make_smc_tp2_fn(),    "bot": None},
    }

    print("\n--- Portföy Simülasyonu ---")
    all_results = {}

    scenarios = []
    for sys in SMC_SYSTEMS:
        for mode in ["actual", "tp1_only", "tp2_only"]:
            scenarios.append((sys, mode, "smc"))
    for sys in BOT_SYSTEMS:
        for mode in ["actual", "tp1_only"]:
            scenarios.append((sys, mode, "bot"))

    for sys, mode, typ in scenarios:
        key = f"{sys}__{mode}"
        sigs = all_signals[sys]
        if not sigs:
            print(f"  {key:<40} — sinyal yok, atlanıyor")
            all_results[key] = None
            continue

        exit_fn = exit_fns[mode][typ]
        print(f"  {key:<40} ({len(sigs)} sinyal)...", end=" ", flush=True)
        try:
            result = simulate_portfolio(sigs, price_data, exit_fn)
            all_results[key] = result
            print(f"✓  ${result['final_capital']:,.2f} ({result['total_return_pct']:+.1f}%) "
                  f"WR:{result['win_rate']:.0f}% Trades:{result['n_trades']}")
        except Exception as e:
            print(f"HATA: {e}")
            all_results[key] = None

    print("\n--- HTML Oluşturuluyor ---")
    html = generate_html(all_results, coins_tested, total_signals)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"✓ {args.out} kaydedildi ({os.path.getsize(args.out)//1024} KB)")

    # JSON sonuçlarını da kaydet
    json_out = args.out.replace(".html", ".json")
    summary = {}
    for (sys, mode, typ) in scenarios:
        key = f"{sys}__{mode}"
        r = all_results.get(key)
        if r:
            summary[key] = {k: v for k, v in r.items() if k != "equity_curve"}
    with open(json_out, "w") as f:
        json.dump(summary, f, indent=2)
    print(f"✓ {json_out} kaydedildi")

    # Konsol özet tablosu
    print("\n" + "═"*100)
    print(f"  {'Senaryo':<42} {'İşlem':>6} {'WR%':>6} {'Getiri%':>9} {'Son Sermaye':>12} {'MaxDD%':>8}")
    print("─"*100)
    for sys, mode, typ in scenarios:
        key = f"{sys}__{mode}"
        r = all_results.get(key)
        label = SCENARIO_LABELS.get((sys, mode), key)
        if r is None:
            print(f"  {label:<42} {'—':>6}")
        else:
            print(f"  {label:<42} {r['n_trades']:6d} {r['win_rate']:6.1f}% "
                  f"{r['total_return_pct']:+9.2f}% ${r['final_capital']:11,.2f} {-r['max_drawdown']:8.1f}%")
    print("═"*100)


if __name__ == "__main__":
    main()
