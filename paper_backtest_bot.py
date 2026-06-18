#!/usr/bin/env python3
"""
Bot.py Sistemleri Backtest — 2022'den bugüne
Sistemler: Panik Pump, Rocket, T72, T168 (normal + v2)

Kullanım:
  python paper_backtest_bot.py              # Cache varsa kullan, yoksa indir
  python paper_backtest_bot.py --no-fetch  # Sadece cache (indirme)
  python paper_backtest_bot.py --coins BTC ETH SOL BNB  # Belirli coinler

NOT: backtest_data/ klasörü SMC backtesti ile ortak — mevcut cache kullanılır.
     rm -rf backtest_data/ && python paper_backtest_bot.py  (taze indirme)
"""

import argparse, os, pickle, time
import datetime as _dt
import numpy as np, pandas as pd

# ─── GENEL AYARLAR ──────────────────────────────────────────────────────────
DATA_DIR       = "backtest_data"
START_DATE     = pd.Timestamp("2022-01-01", tz="UTC")
INITIAL_CAP    = 5_000.0
MAX_POSITIONS  = 5
MAX_POS_SIZE   = 20_000.0
MIN_VOL_24H    = 5_000_000
BTC_CRASH_PCT  = 3.0
CHOCH_SWING    = 5          # BTC downtrend tespiti için

START_TS = int(_dt.datetime(2021, 1, 1, tzinfo=_dt.timezone.utc).timestamp() * 1000)

IGNORED_COINS = {
    "UP/USDT","DOWN/USDT","BEAR/USDT","BULL/USDT",
    "USDC/USDT","TUSD/USDT","FDUSD/USDT","DAI/USDT","USDP/USDT",
    "USDE/USDT","UST/USDT","USD/USDT","XUSD/USDT","USD1/USDT","BFUSD/USDT",
    "USTC/USDT","BUSD/USDT","FRAX/USDT","LUSD/USDT","GUSD/USDT","SUSD/USDT",
    "USDS/USDT","USDX/USDT","USDD/USDT","CUSD/USDT","OUSD/USDT","MUSD/USDT",
    "RLUSD/USDT","U/USDT",
    "EUR/USDT","TRY/USDT","GBP/USDT","BRL/USDT","RUB/USDT",
    "AUD/USDT","BIDR/USDT","IDRT/USDT","VAI/USDT",
    "PAXG/USDT","XAUT/USDT","WBTC/USDT","WETH/USDT","WBNB/USDT","BETH/USDT",
    "BTCB/USDT","HBTC/USDT",
}
LEVERAGED_PATTERNS = ["UP","DOWN","BULL","BEAR","3L","3S","2L","2S","5L","5S","10L","10S"]

# ─── SİSTEM PARAMETRELERİ ───────────────────────────────────────────────────

# Panik Pump
PP_CRASH_MIN    = -15.0
PP_CRASH_MAX    = -7.0
PP_VOL_PERIOD   = 20
PP_VOL_MIN      = 1.5    # normal
PP_VOL_MAX      = 3.0
PP_V2_VOL_MIN   = 2.0    # v2: daha katı
PP_STOP_PCT     = -3.0
PP_TP1_PCT      = 5.0
PP_TP2_PCT      = 10.0
PP_TP3_PCT      = 15.0
PP_TRAIL_PCT    = 3.0    # TP1 sonrası trailing stop
PP_COOLDOWN_H   = 4
PP_EXPIRE_H     = 168

# Rocket
RK_CHANGE_24H   = 10.0
RK_VOL_MIN      = 1.2    # normal
RK_V2_VOL_MIN   = 2.0    # v2
RK_ADX_MIN      = 25     # normal
RK_V2_ADX_MIN   = 30     # v2
RK_STOP_PCT     = -5.0
RK_TP1_PCT      = 8.0
RK_TP2_PCT      = 15.0
RK_TP3_PCT      = 25.0
RK_TRAIL_PCT    = 3.0
RK_COOLDOWN_H   = 4
RK_EXPIRE_H     = 168

# T72
T72_MOM5        = 2.740
T72_DEMA21      = -2.737
T72_DRAWDOWN    = -26.796
T72_MA200S      = 1.028
T72_STOP_PCT    = -5.0
T72_TP1_PCT     = 10.0
T72_TP2_PCT     = 15.0
T72_COOLDOWN_H  = 4
T72_EXPIRE_H    = 72

# T168
T168_DMA200     = 5.657
T168_DMA50      = -5.045
T168_MOM10      = 3.941
T168_DAYS       = 677
T168_STOP_PCT   = -8.0
T168_TP1_PCT    = 25.0
T168_TP2_PCT    = 25.0
T168_COOLDOWN_H = 4
T168_EXPIRE_H   = 168


# ─── VERİ YÜKLEME ───────────────────────────────────────────────────────────
def load_pkl(symbol):
    path = os.path.join(DATA_DIR, symbol.replace("/","_") + ".pkl")
    if not os.path.exists(path): return None
    with open(path,"rb") as f: return pickle.load(f)


def fetch_and_save(symbol):
    try:
        import ccxt
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
        df = df[~df.index.duplicated(keep="first")]
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(os.path.join(DATA_DIR, symbol.replace("/","_")+".pkl"),"wb") as f:
            pickle.dump(df, f)
        return df
    except Exception as e:
        print(f"    ! {symbol}: {e}"); return None


def load_or_fetch(symbol):
    df = load_pkl(symbol)
    if df is not None: return df
    print(f"    ↓ {symbol} indiriliyor...", end=" ", flush=True)
    df = fetch_and_save(symbol)
    if df is not None: print("✓")
    return df


def get_all_binance_symbols():
    try:
        import ccxt
        ex = ccxt.binance({"enableRateLimit": True})
        ex.load_markets()
        result = []
        for sym, mkt in ex.markets.items():
            if not (mkt.get("spot") and mkt.get("active") and sym.endswith("/USDT")): continue
            if sym in IGNORED_COINS: continue
            base = sym.split("/")[0]
            if any(p in base for p in LEVERAGED_PATTERNS): continue
            result.append(sym)
        if "BTC/USDT" in result: result.remove("BTC/USDT")
        result.insert(0, "BTC/USDT")
        return result
    except Exception as e:
        print(f"Binance listesi alınamadı: {e}"); return []


def get_cached_symbols():
    if not os.path.isdir(DATA_DIR): return []
    symbols = []
    for fname in os.listdir(DATA_DIR):
        if not fname.endswith(".pkl"): continue
        sym = fname[:-4].replace("_","/",1)
        if not sym.endswith("/USDT") or sym in IGNORED_COINS: continue
        base = sym.split("/")[0]
        if any(p in base for p in LEVERAGED_PATTERNS): continue
        try:
            with open(os.path.join(DATA_DIR,fname),"rb") as f: df = pickle.load(f)
            if df is None or len(df) < 300: continue
        except Exception: continue
        symbols.append(sym)
    if "BTC/USDT" in symbols: symbols.remove("BTC/USDT")
    symbols.insert(0, "BTC/USDT")
    return symbols


# ─── BTC FİLTRELERİ ─────────────────────────────────────────────────────────
def compute_btc_filters(btc_1h):
    """crash_ok (4H BTC düşüşü yok) ve downtrend_ok (BTC düşüş yapısı yok) döndür."""
    df4 = btc_1h.resample("4h", label="right", closed="right").agg(
        {"open":"first","high":"max","low":"min","close":"last","volume":"sum"}
    ).dropna(subset=["close"])
    c4 = df4["close"].values
    h4 = df4["high"].values
    l4 = df4["low"].values
    n4 = len(df4)

    # BTC crash filtresi
    crash_ok = np.ones(n4, dtype=bool)
    for i in range(1, n4):
        crash_ok[i] = (c4[i] / c4[i-1] - 1) * 100 > -BTC_CRASH_PCT

    # BTC downtrend (swing lows düşüyor mu)
    legs4 = np.zeros(n4, dtype=int); cur4 = 0
    for i in range(CHOCH_SWING, n4):
        ph = h4[i-CHOCH_SWING]; pl = l4[i-CHOCH_SWING]
        wh = h4[i-CHOCH_SWING+1:i+1].max(); wl = l4[i-CHOCH_SWING+1:i+1].min()
        if ph > wh: cur4 = 0
        elif pl < wl: cur4 = 1
        legs4[i] = cur4

    sl4 = None; slx4 = True; prev_sl4 = None; sh4 = None; shx4 = True; trend4 = 0
    downtrend = np.zeros(n4, dtype=bool)
    for i in range(CHOCH_SWING+1, n4):
        if legs4[i] != legs4[i-1]:
            if legs4[i] == 1: prev_sl4 = sl4; sl4 = l4[i-CHOCH_SWING]; slx4 = False
            else: sh4 = h4[i-CHOCH_SWING]; shx4 = False
        ci, cp = c4[i], c4[i-1]
        if sh4 is not None and not shx4 and ci > sh4 and cp <= sh4: shx4 = True; trend4 = 1
        if sl4 is not None and not slx4 and ci < sl4 and cp >= sl4: slx4 = True; trend4 = -1
        if trend4 == -1 and (prev_sl4 is None or (sl4 is not None and sl4 <= prev_sl4)):
            downtrend[i] = True
    df4["crash_ok"] = crash_ok
    df4["downtrend"] = downtrend

    # BTC EMA50 trend (Rocket filtresi)
    ema50 = df4["close"].ewm(span=50, adjust=False).mean()
    btc_ema50_ok = df4["close"] > ema50  # BTC yükseliş trendinde
    df4["ema50_ok"] = btc_ema50_ok

    idx = btc_1h.index
    crash_s    = df4["crash_ok"].reindex(idx, method="ffill").fillna(True)
    downtrend_s= df4["downtrend"].reindex(idx, method="ffill").fillna(False)
    ema50_s    = df4["ema50_ok"].reindex(idx, method="ffill").fillna(True)
    return pd.DataFrame({
        "crash_ok":   crash_s.astype(bool),
        "downtrend_ok": (~downtrend_s).astype(bool),
        "ema50_ok":   ema50_s.astype(bool),
    }, index=idx)


# ─── İNDİKATÖRLER ───────────────────────────────────────────────────────────
def compute_indicators(df):
    """Tüm sistemler için gerekli indikatörleri hesapla."""
    c = df["close"]
    h = df["high"]
    l = df["low"]
    v = df["volume"]

    # Ortak
    vol_ma = v.rolling(PP_VOL_PERIOD).mean().shift(1)      # shift(1): mevcut barı dışarıda bırak
    df["vol_ma"]    = vol_ma
    df["vol_ratio"] = v / vol_ma.replace(0, np.nan)

    # 24H USD hacim (volume filtresi)
    df["vol_24h_usd"] = (c * v).rolling(24).sum()

    # Panik Pump
    df["ret1"]   = (c / c.shift(1) - 1) * 100
    df["c5ago"]  = c.shift(5)

    # Rocket
    df["change_24h"] = (c / c.shift(25) - 1) * 100   # 25 bar = 24H ago (0-indexed)

    # T72
    ema21            = c.ewm(span=21, adjust=False).mean()
    df["dist_ema21"] = (c - ema21) / ema21.replace(0, np.nan) * 100
    df["mom5_pct"]   = (c / c.shift(5) - 1) * 100
    roll_max_700     = c.rolling(700, min_periods=50).max()
    df["coin_drawdown"] = (c - roll_max_700) / roll_max_700.replace(0, np.nan) * 100

    # T72 & T168
    ma50   = c.rolling(50).mean()
    ma200  = c.rolling(200).mean()
    df["dist_ma50"]   = (c - ma50)  / ma50.replace(0, np.nan)  * 100
    df["dist_ma200"]  = (c - ma200) / ma200.replace(0, np.nan) * 100
    df["ma200_slope"] = (ma200 - ma200.shift(20)) / ma200.shift(20).abs().replace(0, np.nan) * 100
    df["mom10_pct"]   = (c / c.shift(10) - 1) * 100

    # T168: bars_since_high
    bar_idx       = pd.Series(np.arange(len(c), dtype=float), index=c.index)
    is_at_high    = c >= roll_max_700 * (1 - 1e-6)
    last_high_pos = bar_idx.where(is_at_high).ffill().fillna(0)
    df["days_since_high"] = bar_idx - last_high_pos

    return df


def calc_adx_di(arr_h, arr_l, arr_c, period=14):
    """Vektörel ADX+DI hesabı. (adx_arr, pdi_arr, ndi_arr) döndür."""
    n = len(arr_c)
    tr  = np.zeros(n)
    pdm = np.zeros(n)
    ndm = np.zeros(n)
    for i in range(1, n):
        tr[i]  = max(arr_h[i]-arr_l[i], abs(arr_h[i]-arr_c[i-1]), abs(arr_l[i]-arr_c[i-1]))
        up = arr_h[i] - arr_h[i-1]
        dn = arr_l[i-1] - arr_l[i]
        pdm[i] = up if up > dn and up > 0 else 0
        ndm[i] = dn if dn > up and dn > 0 else 0

    def wilder(arr, p):
        s = np.zeros(n)
        if p >= n: return s
        s[p] = arr[1:p+1].sum()
        for i in range(p+1, n):
            s[i] = s[i-1] - s[i-1]/p + arr[i]
        return s

    atr_s = wilder(tr,  period)
    pdm_s = wilder(pdm, period)
    ndm_s = wilder(ndm, period)

    pdi = np.where(atr_s > 0, 100 * pdm_s / atr_s, 0.0)
    ndi = np.where(atr_s > 0, 100 * ndm_s / atr_s, 0.0)
    dx  = np.where(pdi + ndi > 0, 100 * np.abs(pdi - ndi) / (pdi + ndi), 0.0)

    adx = np.zeros(n)
    start = period * 2
    if start < n:
        adx[start] = dx[period:start+1].mean()
        for i in range(start+1, n):
            adx[i] = (adx[i-1] * (period-1) + dx[i]) / period

    return adx, pdi, ndi


# ─── ÇIKIŞ FONKSİYONLARI ────────────────────────────────────────────────────
def exit_trail(sig, pos_size, trail_pct, expire_h):
    """
    Bot çıkış mantığı: TP1 milestone → trailing stop aktif → TP2 tam çıkış.
    Stop'a kadar tutulmaya devam eder (TP3 bilgi amaçlı takip edilir).
    """
    entry  = sig["entry"]
    stop   = sig["stop"]
    tp1    = sig["tp1"]
    tp2    = sig["tp2"]
    tp3    = sig.get("tp3")
    rows   = sig["future"]

    sp  = (stop  - entry) / entry
    t1p = (tp1   - entry) / entry
    t2p = (tp2   - entry) / entry

    peak     = entry
    tp1_hit  = False
    tp3_hit  = False

    for ts, row in rows.iloc[:expire_h].iterrows():
        h = float(row["high"])
        l = float(row["low"])
        if h > peak: peak = h

        if not tp1_hit:
            if l <= stop:
                return ts, pos_size * (1 + sp), "stop"
            if h >= tp1:
                tp1_hit = True
        else:
            trail_price = peak * (1 - trail_pct / 100)
            if tp3 and not tp3_hit and h >= tp3:
                tp3_hit = True
            if h >= tp2:
                return ts, pos_size * (1 + t2p), "tp2"
            if l <= trail_price:
                trail_gain = (trail_price - entry) / entry
                label = "trail_after_tp3" if tp3_hit else "trail"
                return ts, pos_size * (1 + trail_gain), label

    if len(rows) > 0:
        idx  = min(expire_h - 1, len(rows) - 1)
        last = float(rows.iloc[idx]["close"])
        exp_pct = (last - entry) / entry
        return rows.index[idx], pos_size * (1 + exp_pct), "expire"
    return sig["entry_time"], pos_size, "no_data"


def exit_tp2_simple(sig, pos_size, expire_h):
    """T72/T168 için: stop veya TP2'de tam çıkış."""
    entry = sig["entry"]
    stop  = sig["stop"]
    tp1   = sig["tp1"]
    tp2   = sig["tp2"]
    rows  = sig["future"]
    sp  = (stop - entry) / entry
    t1p = (tp1  - entry) / entry
    t2p = (tp2  - entry) / entry
    for ts, row in rows.iloc[:expire_h].iterrows():
        h = float(row["high"]); l = float(row["low"])
        if l <= stop: return ts, pos_size * (1 + sp), "stop"
        if h >= tp2:  return ts, pos_size * (1 + t2p), "tp2"
        if h >= tp1:  return ts, pos_size * (1 + t1p), "tp1"
    if len(rows) > 0:
        idx = min(expire_h - 1, len(rows) - 1)
        exp = (float(rows.iloc[idx]["close"]) - entry) / entry
        return rows.index[idx], pos_size * (1 + exp), "expire"
    return sig["entry_time"], pos_size, "no_data"


# ─── SİNYAL TOPLAMA ─────────────────────────────────────────────────────────
def collect_signals(symbols, btc_filters, fetch=True):
    sigs = {k: [] for k in [
        "panik_pump", "panik_pump_v2",
        "rocket", "rocket_v2",
        "t72", "t72_v2",
        "t168", "t168_v2",
    ]}

    for sym_i, symbol in enumerate(symbols, 1):
        if symbol == "BTC/USDT": continue
        print(f"  [{sym_i}/{len(symbols)}] {symbol}", end=" ", flush=True)

        df_raw = load_or_fetch(symbol) if fetch else load_pkl(symbol)
        if df_raw is None or len(df_raw) < 300:
            print("atlandı (veri yok)")
            continue

        df = df_raw.copy()
        df = compute_indicators(df)
        df.dropna(subset=["vol_ma","ret1","change_24h","dist_ema21"], inplace=True)
        if len(df) < 300:
            print("atlandı (kısa)")
            continue

        btc = btc_filters.reindex(df.index, method="ffill")

        h_arr = df["high"].values
        l_arr = df["low"].values
        c_arr = df["close"].values
        adx_arr, pdi_arr, ndi_arr = calc_adx_di(h_arr, l_arr, c_arr, period=14)

        n = len(df)
        last_pp = last_pp_v2 = last_rk = last_rk_v2 = last_t72 = last_t72_v2 = last_t168 = last_t168_v2 = 0.0

        pp_count = rk_count = t72_count = t168_count = 0

        for i in range(250, n - 1):
            ts  = df.index[i]
            if ts < START_DATE: continue

            # --- Temel filtreler ---
            vol24 = float(df["vol_24h_usd"].iloc[i])
            if np.isnan(vol24) or vol24 < MIN_VOL_24H: continue

            price = c_arr[i]
            if np.isnan(price) or price <= 0: continue

            ret1      = float(df["ret1"].iloc[i])
            vol_ratio = float(df["vol_ratio"].iloc[i])
            c5ago     = float(df["c5ago"].iloc[i])
            ts_h = ts.timestamp() / 3600

            btc_crash_ok    = bool(btc["crash_ok"].iloc[i])
            btc_downtrend_ok= bool(btc["downtrend_ok"].iloc[i])
            btc_ema50_ok    = bool(btc["ema50_ok"].iloc[i])

            future = df.iloc[i+1:i+1+max(PP_EXPIRE_H, RK_EXPIRE_H, T168_EXPIRE_H)][
                ["high","low","close"]].copy()

            # ── PANİK PUMP ──────────────────────────────────────────────────
            if (PP_CRASH_MIN <= ret1 <= PP_CRASH_MAX
                    and not np.isnan(vol_ratio)
                    and not np.isnan(c5ago) and c5ago > 0
                    and not ((c5ago - price) / c5ago * 100 >= 4.0)):  # f5t filtresi

                entry = price
                stop  = round(entry * (1 + PP_STOP_PCT/100), 10)
                tp1   = round(entry * (1 + PP_TP1_PCT/100), 10)
                tp2   = round(entry * (1 + PP_TP2_PCT/100), 10)
                tp3   = round(entry * (1 + PP_TP3_PCT/100), 10)
                base  = dict(symbol=symbol, entry_time=ts, entry=entry,
                             stop=stop, tp1=tp1, tp2=tp2, tp3=tp3,
                             future=future, expire_h=PP_EXPIRE_H,
                             vol_ratio=round(vol_ratio,2), ret1=round(ret1,2))

                # Normal: vol [1.5, 3.0]
                if PP_VOL_MIN <= vol_ratio <= PP_VOL_MAX:
                    if ts_h - last_pp >= PP_COOLDOWN_H:
                        sigs["panik_pump"].append(base.copy())
                        last_pp = ts_h
                        pp_count += 1

                # V2: vol [2.0, 3.0] + BTC filtresi
                if PP_V2_VOL_MIN <= vol_ratio <= PP_VOL_MAX:
                    if btc_crash_ok and btc_downtrend_ok:
                        if ts_h - last_pp_v2 >= PP_COOLDOWN_H:
                            sigs["panik_pump_v2"].append(base.copy())
                            last_pp_v2 = ts_h

            # ── ROCKET ──────────────────────────────────────────────────────
            change_24h = float(df["change_24h"].iloc[i])
            adx_val    = float(adx_arr[i])
            pdi_val    = float(pdi_arr[i])
            ndi_val    = float(ndi_arr[i])

            if (not np.isnan(change_24h) and change_24h >= RK_CHANGE_24H
                    and not np.isnan(adx_val) and adx_val > 0
                    and not np.isnan(vol_ratio)
                    and pdi_val > ndi_val):

                entry = price
                stop  = round(entry * (1 + RK_STOP_PCT/100), 10)
                tp1   = round(entry * (1 + RK_TP1_PCT/100), 10)
                tp2   = round(entry * (1 + RK_TP2_PCT/100), 10)
                tp3   = round(entry * (1 + RK_TP3_PCT/100), 10)
                base  = dict(symbol=symbol, entry_time=ts, entry=entry,
                             stop=stop, tp1=tp1, tp2=tp2, tp3=tp3,
                             future=future, expire_h=RK_EXPIRE_H,
                             vol_ratio=round(vol_ratio,2), change_24h=round(change_24h,2),
                             adx=round(adx_val,1))

                # Normal: vol 1.2x, ADX 25, BTC EMA50 filtresi
                if vol_ratio >= RK_VOL_MIN and adx_val >= RK_ADX_MIN and btc_ema50_ok:
                    if ts_h - last_rk >= RK_COOLDOWN_H:
                        sigs["rocket"].append(base.copy())
                        last_rk = ts_h
                        rk_count += 1

                # V2: vol 2.0x, ADX 30, BTC EMA50 filtresi
                if vol_ratio >= RK_V2_VOL_MIN and adx_val >= RK_V2_ADX_MIN and btc_ema50_ok:
                    if ts_h - last_rk_v2 >= RK_COOLDOWN_H:
                        sigs["rocket_v2"].append(base.copy())
                        last_rk_v2 = ts_h

            # ── T72 ─────────────────────────────────────────────────────────
            mom5       = float(df["mom5_pct"].iloc[i])
            dist_e21   = float(df["dist_ema21"].iloc[i])
            drawdown   = float(df["coin_drawdown"].iloc[i])
            ma200s     = float(df["ma200_slope"].iloc[i])

            if (not any(np.isnan(v) for v in [mom5, dist_e21, drawdown, ma200s])
                    and mom5 >= T72_MOM5
                    and dist_e21 <= T72_DEMA21
                    and drawdown >= T72_DRAWDOWN
                    and ma200s >= T72_MA200S):

                entry = price
                stop  = round(entry * (1 + T72_STOP_PCT/100), 10)
                tp1   = round(entry * (1 + T72_TP1_PCT/100), 10)
                tp2   = round(entry * (1 + T72_TP2_PCT/100), 10)
                base  = dict(symbol=symbol, entry_time=ts, entry=entry,
                             stop=stop, tp1=tp1, tp2=tp2,
                             future=df.iloc[i+1:i+1+T72_EXPIRE_H][["high","low","close"]].copy(),
                             expire_h=T72_EXPIRE_H,
                             mom5=round(mom5,2), dist_e21=round(dist_e21,2),
                             drawdown=round(drawdown,2), ma200s=round(ma200s,3))

                if ts_h - last_t72 >= T72_COOLDOWN_H:
                    sigs["t72"].append(base.copy())
                    last_t72 = ts_h
                    t72_count += 1

                # V2: + BTC filtresi
                if btc_crash_ok and btc_downtrend_ok:
                    if ts_h - last_t72_v2 >= T72_COOLDOWN_H:
                        sigs["t72_v2"].append(base.copy())
                        last_t72_v2 = ts_h

            # ── T168 ────────────────────────────────────────────────────────
            dist_ma200  = float(df["dist_ma200"].iloc[i])
            dist_ma50   = float(df["dist_ma50"].iloc[i])
            mom10       = float(df["mom10_pct"].iloc[i])
            days_high   = float(df["days_since_high"].iloc[i])

            if (not any(np.isnan(v) for v in [dist_ma200, dist_ma50, mom10, days_high])
                    and dist_ma200 >= T168_DMA200
                    and dist_ma50  <= T168_DMA50
                    and mom10      >= T168_MOM10
                    and days_high  <= T168_DAYS):

                entry = price
                stop  = round(entry * (1 + T168_STOP_PCT/100), 10)
                tp1   = round(entry * (1 + T168_TP1_PCT/100), 10)
                tp2   = round(entry * (1 + T168_TP2_PCT/100), 10)
                base  = dict(symbol=symbol, entry_time=ts, entry=entry,
                             stop=stop, tp1=tp1, tp2=tp2,
                             future=df.iloc[i+1:i+1+T168_EXPIRE_H][["high","low","close"]].copy(),
                             expire_h=T168_EXPIRE_H,
                             dist_ma200=round(dist_ma200,2), dist_ma50=round(dist_ma50,2),
                             mom10=round(mom10,2), days_high=int(days_high))

                if ts_h - last_t168 >= T168_COOLDOWN_H:
                    sigs["t168"].append(base.copy())
                    last_t168 = ts_h
                    t168_count += 1

                # V2: + BTC filtresi
                if btc_crash_ok and btc_downtrend_ok:
                    if ts_h - last_t168_v2 >= T168_COOLDOWN_H:
                        sigs["t168_v2"].append(base.copy())
                        last_t168_v2 = ts_h

        print(f"PP:{pp_count} RK:{rk_count} T72:{t72_count} T168:{t168_count}", flush=True)

    return sigs


# ─── SİMÜLASYON ─────────────────────────────────────────────────────────────
def simulate(sigs, scenario, exit_fn, **exit_kwargs):
    signals = sigs[scenario]
    if not signals:
        return {"trades":0,"wins":0,"losses":0,"expires":0,
                "wr":0,"final":INITIAL_CAP,"ret":0,"max_dd":0,
                "avg_win":0,"avg_loss":0}

    # Pozisyon limiti (time-ordered)
    signals_sorted = sorted(signals, key=lambda s: s["entry_time"])

    cap      = INITIAL_CAP
    peak_cap = INITIAL_CAP
    max_dd   = 0.0
    open_pos = []   # (exit_time, exit_val)
    wins = losses = expires = 0
    win_pcts = []; loss_pcts = []
    tp1_hits = tp2_hits = tp3_hits = trails = 0

    for sig in signals_sorted:
        et = sig["entry_time"]
        # Kapalı pozisyonları temizle
        closed = [p for p in open_pos if p[0] <= et]
        for _, val in closed:
            cap += val
        open_pos = [p for p in open_pos if p[0] > et]

        if len(open_pos) >= MAX_POSITIONS: continue

        pos_size = min(cap / MAX_POSITIONS, MAX_POS_SIZE)
        if pos_size <= 0: continue
        cap -= pos_size

        exit_time, exit_val, reason = exit_fn(sig, pos_size, **exit_kwargs)
        open_pos.append((exit_time, exit_val))

        pct = (exit_val / pos_size - 1) * 100
        if reason in ("tp1","tp2","tp3","trail","trail_after_tp3"):
            wins += 1; win_pcts.append(pct)
            if reason == "tp2": tp2_hits += 1
            elif reason == "tp1": tp1_hits += 1
            elif reason in ("trail","trail_after_tp3"): trails += 1
        elif reason == "stop":
            losses += 1; loss_pcts.append(pct)
        else:
            expires += 1

        # Peak + drawdown takibi
        cur_cap = cap + sum(v for _, v in open_pos)
        if cur_cap > peak_cap: peak_cap = cur_cap
        dd = (cur_cap - peak_cap) / peak_cap * 100
        if dd < max_dd: max_dd = dd

    # Kalan pozisyonları kapat
    for _, val in open_pos:
        cap += val

    total  = wins + losses + expires
    wr     = wins / (wins + losses) * 100 if (wins + losses) > 0 else 0
    ret    = (cap / INITIAL_CAP - 1) * 100
    avg_w  = sum(win_pcts)  / len(win_pcts)  if win_pcts  else 0
    avg_l  = sum(loss_pcts) / len(loss_pcts) if loss_pcts else 0

    return {
        "trades": total, "wins": wins, "losses": losses, "expires": expires,
        "wr": round(wr, 1), "final": round(cap, 2), "ret": round(ret, 2),
        "max_dd": round(max_dd, 1),
        "avg_win": round(avg_w, 2), "avg_loss": round(avg_l, 2),
        "tp1_hits": tp1_hits, "tp2_hits": tp2_hits,
        "trail_hits": trails,
        "n_sigs": len(signals),
    }


def print_results(label, stats):
    n  = stats["n_sigs"]
    tr = stats["trades"]
    print(f"\n{'═'*56}")
    print(f"  {label}")
    print(f"{'═'*56}")
    print(f"  Sinyal    : {n}")
    print(f"  İşlem     : {tr}  (win:{stats['wins']} loss:{stats['losses']} exp:{stats['expires']})")
    print(f"  Win Rate  : %{stats['wr']}  (W/L bazlı)")
    print(f"  Ort. Kazanç: {stats['avg_win']:+.2f}%   Ort. Kayıp: {stats['avg_loss']:+.2f}%")
    print(f"  TP1 hit   : {stats['tp1_hits']}   TP2 hit: {stats['tp2_hits']}   Trail: {stats['trail_hits']}")
    print(f"  Final Cap : ${stats['final']:,.2f}   Getiri: {stats['ret']:+.2f}%")
    print(f"  Max DD    : {stats['max_dd']:.1f}%")


# ─── MAIN ────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-fetch", action="store_true", help="Sadece cache kullan")
    ap.add_argument("--coins", nargs="+", help="Belirli coinler (örn: BTC ETH SOL)")
    args = ap.parse_args()

    fetch = not args.no_fetch

    print("=" * 60)
    print("  BOT.PY SİSTEMLERİ BACKTEST")
    print(f"  Başlangıç: {START_DATE.date()}  |  Kapital: ${INITIAL_CAP:,.0f}")
    print("=" * 60)

    # Coin listesi
    if args.coins:
        symbols = [f"{c}/USDT" if "/" not in c else c for c in args.coins]
        if "BTC/USDT" not in symbols: symbols.insert(0, "BTC/USDT")
    elif fetch:
        symbols = get_all_binance_symbols()
        if not symbols:
            symbols = get_cached_symbols()
    else:
        symbols = get_cached_symbols()

    print(f"\n{len(symbols)} coin")

    # BTC verisi
    print("\n📊 BTC verisi yükleniyor...")
    btc_df = load_or_fetch("BTC/USDT") if fetch else load_pkl("BTC/USDT")
    if btc_df is None:
        print("HATA: BTC verisi yüklenemedi."); return

    print("📊 BTC filtreleri hesaplanıyor...")
    btc_filters = compute_btc_filters(btc_df)

    # Sinyaller
    print("\n📡 Sinyaller toplanıyor...\n")
    all_sigs = collect_signals(symbols, btc_filters, fetch=fetch)

    print("\n\n" + "─" * 60)
    print("  SONUÇLAR")
    print("─" * 60)

    # ── PANİK PUMP ──────────────────────────────────────────────────────────
    for label, key in [
        ("PANİK PUMP  (vol 1.5-3.0x, BTC filtre yok)", "panik_pump"),
        ("PANİK PUMP V2  (vol 2.0-3.0x + BTC filtre)", "panik_pump_v2"),
    ]:
        stats = simulate(all_sigs, key,
                         exit_fn=exit_trail,
                         trail_pct=PP_TRAIL_PCT,
                         expire_h=PP_EXPIRE_H)
        print_results(label, stats)

    # ── ROCKET ──────────────────────────────────────────────────────────────
    for label, key in [
        ("ROCKET       (vol 1.2x, ADX≥25, BTC EMA50)", "rocket"),
        ("ROCKET V2    (vol 2.0x, ADX≥30, BTC EMA50)", "rocket_v2"),
    ]:
        stats = simulate(all_sigs, key,
                         exit_fn=exit_trail,
                         trail_pct=RK_TRAIL_PCT,
                         expire_h=RK_EXPIRE_H)
        print_results(label, stats)

    # ── T72 ─────────────────────────────────────────────────────────────────
    for label, key in [
        ("T72          (orijinal koşullar, BTC yok)", "t72"),
        ("T72 V2       (orijinal + BTC filtre)",      "t72_v2"),
    ]:
        stats = simulate(all_sigs, key,
                         exit_fn=exit_tp2_simple,
                         expire_h=T72_EXPIRE_H)
        print_results(label, stats)

    # ── T168 ────────────────────────────────────────────────────────────────
    for label, key in [
        ("T168         (orijinal koşullar, BTC yok)", "t168"),
        ("T168 V2      (orijinal + BTC filtre)",      "t168_v2"),
    ]:
        stats = simulate(all_sigs, key,
                         exit_fn=exit_tp2_simple,
                         expire_h=T168_EXPIRE_H)
        print_results(label, stats)

    print(f"\n{'═'*56}\n")


if __name__ == "__main__":
    main()
