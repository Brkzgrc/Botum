#!/usr/bin/env python3
"""
Full Backtest — 20 Senaryo (4 SMC × 3 çıkış + 4 Bot × 2 çıkış)
paper_backtest_vl.py iskeletinin genişletilmiş hali.

Kullanım:
  python paper_backtest_vl.py
  python paper_backtest_vl.py --no-fetch
  python paper_backtest_vl.py --coins BTC ETH SOL
"""

import argparse, heapq, json, os, pickle, time
import datetime as _dt
import numpy as np, pandas as pd

# ─── AYARLAR ────────────────────────────────────────────────────────────────
DATA_DIR      = "backtest_data"
START_DATE    = pd.Timestamp("2025-01-01", tz="UTC")
INITIAL_CAP   = 5_000.0
MAX_POSITIONS = 10
COOLDOWN_H    = 24
EXPIRE_H      = 168
CHOCH_SWING   = 5
MIN_VOL_24H   = 5_000_000
BTC_CRASH_PCT = 3.0
SWING_LENGTH  = 50
PHASE1_DEPTH  = 85.0
PHASE1_RSI    = 30.0
ESKI_DISC_DEPTH = 5.0
SMC_TRAIL_PCT = 2.5
BOT_TRAIL_PCT = 3.0

PANIK_MIN = -15.0; PANIK_MAX = -7.0
PANIK_VOL_MIN = 1.5; PANIK_VOL_MAX = 3.0
PANIK_EXPIRE_H = 24; PANIK_COOL_H = 4
T72_EXPIRE_H = 72;   T72_COOL_H = 4
T168_EXPIRE_H = 168; T168_COOL_H = 4
ROCKET_EXPIRE_H = 48; ROCKET_COOL_H = 12
ROCKET_CHG_MIN = 10.0; ROCKET_VOL_MIN = 1.2; ROCKET_ADX_MIN = 25.0

START_TS = int(_dt.datetime(2024, 1, 1, tzinfo=_dt.timezone.utc).timestamp() * 1000)

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


# ─── VERİ ───────────────────────────────────────────────────────────────────
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
    df4 = btc_1h.resample("4h", label="right", closed="right").agg(
        {"open":"first","high":"max","low":"min","close":"last","volume":"sum"}
    ).dropna(subset=["close"])
    n4=len(df4); h4=df4["high"].values; l4=df4["low"].values; c4=df4["close"].values
    crash_ok = np.ones(n4, dtype=bool)
    for i in range(1, n4):
        crash_ok[i] = (c4[i]/c4[i-1]-1)*100 > -BTC_CRASH_PCT
    legs4=np.zeros(n4,dtype=int); cur4=0
    for i in range(CHOCH_SWING, n4):
        ph=h4[i-CHOCH_SWING]; pl=l4[i-CHOCH_SWING]
        wh=h4[i-CHOCH_SWING+1:i+1].max(); wl=l4[i-CHOCH_SWING+1:i+1].min()
        if ph>wh: cur4=0
        elif pl<wl: cur4=1
        legs4[i]=cur4
    sh4=None; shx4=True; sl4=None; slx4=True; prev_sl4=None; trend4=0
    downtrend=np.zeros(n4,dtype=bool)
    for i in range(CHOCH_SWING+1, n4):
        if legs4[i]!=legs4[i-1]:
            if legs4[i]==1: prev_sl4=sl4; sl4=l4[i-CHOCH_SWING]; slx4=False
            else: sh4=h4[i-CHOCH_SWING]; shx4=False
        ci,cp=c4[i],c4[i-1]
        if sh4 is not None and not shx4 and ci>sh4 and cp<=sh4: shx4=True; trend4=1
        if sl4 is not None and not slx4 and ci<sl4 and cp>=sl4: slx4=True; trend4=-1
        if trend4==-1 and (prev_sl4 is None or (sl4 is not None and sl4<=prev_sl4)):
            downtrend[i]=True
    df4["crash_ok"]=crash_ok; df4["downtrend"]=downtrend
    idx=btc_1h.index
    crash_s    = df4["crash_ok"].reindex(idx,method="ffill").fillna(True).astype(bool)
    downtrend_s = df4["downtrend"].reindex(idx,method="ffill").fillna(False).astype(bool)
    return pd.DataFrame({"crash_ok":crash_s,"downtrend_ok":~downtrend_s},index=idx)


# ─── CHoCH TESPİTİ ──────────────────────────────────────────────────────────
def run_choch_incremental(df):
    h=df["high"].values; l=df["low"].values; c=df["close"].values; n=len(df)
    legs=np.zeros(n,dtype=int); cur=0
    for i in range(CHOCH_SWING, n):
        ph=h[i-CHOCH_SWING]; pl=l[i-CHOCH_SWING]
        wh=h[i-CHOCH_SWING+1:i+1].max(); wl=l[i-CHOCH_SWING+1:i+1].min()
        if ph>wh: cur=0
        elif pl<wl: cur=1
        legs[i]=cur
    sh=None; shx=True; sl=None; slx=True; trend=0
    bts=[None]*n; bds=[None]*n; cls_=[None]*n; swls=[None]*n
    for i in range(CHOCH_SWING+1, n):
        if legs[i]!=legs[i-1]:
            if legs[i]==1: sl=l[i-CHOCH_SWING]; slx=False
            else: sh=h[i-CHOCH_SWING]; shx=False
        ci,cp=c[i],c[i-1]; bt=None; bd=None; cl=None
        if sh is not None and not shx and ci>sh and cp<=sh:
            bt="CHoCH" if trend==-1 else "BOS"; bd="BULLISH"; cl=sh; shx=True; trend=1
        if sl is not None and not slx and ci<sl and cp>=sl:
            bt="CHoCH" if trend==1 else "BOS"; bd="BEARISH"; cl=sl; slx=True; trend=-1
        bts[i]=bt; bds[i]=bd; cls_[i]=cl; swls[i]=sl
    return bts, bds, cls_, swls


# ─── LUXALGO DISCOUNT ZONE ──────────────────────────────────────────────────
def compute_luxalgo_incremental(df, swing_length=SWING_LENGTH):
    n=len(df); h=df["high"].values; l=df["low"].values; c=df["close"].values
    legs=np.zeros(n,dtype=int); cur=0
    for i in range(swing_length, n):
        ph=h[i-swing_length]; pl=l[i-swing_length]
        wh=h[i-swing_length+1:i+1].max(); wl=l[i-swing_length+1:i+1].min()
        if ph>wh: cur=0
        elif pl<wl: cur=1
        legs[i]=cur
    disc_top=np.full(n,np.nan); disc_bot=np.full(n,np.nan); depth_arr=np.full(n,np.nan)
    t_top=None; t_bot=None
    for i in range(swing_length, n):
        prev_leg=legs[i-1] if i>0 else 0
        if legs[i]!=prev_leg:
            if legs[i]==1: t_bot=l[i-swing_length]
            elif legs[i]==0: t_top=h[i-swing_length]
        if t_top is not None and h[i]>t_top: t_top=h[i]
        if t_bot is not None and l[i]<t_bot: t_bot=l[i]
        if t_top is not None and t_bot is not None and t_top!=t_bot:
            disc_top[i]=0.55*t_top+0.45*t_bot
            disc_bot[i]=t_bot
            depth_arr[i]=(t_top-c[i])/(t_top-t_bot)*100
    return disc_top, disc_bot, depth_arr


# ─── EK İNDİKATÖRLER ────────────────────────────────────────────────────────
def compute_extra_indicators(df):
    df=df.copy(); c=df["close"]; prev_c=c.shift(1)
    h=df["high"]; l=df["low"]
    tr=pd.concat([h-l,(h-prev_c).abs(),(l-prev_c).abs()],axis=1).max(axis=1)
    df["atr"]=tr.ewm(alpha=1/14,adjust=False).mean()
    delta=c.diff()
    gain=delta.clip(lower=0).ewm(alpha=1/14,adjust=False).mean()
    loss=(-delta).clip(lower=0).ewm(alpha=1/14,adjust=False).mean()
    df["rsi"]=100-100/(1+gain/loss.replace(0,np.nan))
    df["ema21"]=c.ewm(span=21,adjust=False).mean()
    df["ma50"]=c.rolling(50).mean()
    df["ma200"]=c.rolling(200).mean()
    df["ma200_slope"]=(df["ma200"]-df["ma200"].shift(20))/df["ma200"].shift(20).abs().replace(0,np.nan)*100
    df["dist_ema21"]=(c-df["ema21"])/df["ema21"].replace(0,np.nan)*100
    df["dist_ma50"]=(c-df["ma50"])/df["ma50"].replace(0,np.nan)*100
    df["dist_ma200"]=(c-df["ma200"])/df["ma200"].replace(0,np.nan)*100
    df["mom5_pct"]=(c-c.shift(5))/c.shift(5).abs().replace(0,np.nan)*100
    df["mom10_pct"]=(c-c.shift(10))/c.shift(10).abs().replace(0,np.nan)*100
    roll_max=c.rolling(2500,min_periods=50).max()
    df["coin_drawdown"]=(c-roll_max)/roll_max.replace(0,np.nan)*100
    bar_idx=pd.Series(np.arange(len(df)),index=df.index)
    last_high_pos=bar_idx.where(c>=roll_max*(1-1e-6)).ffill().fillna(0)
    df["bars_since_high"]=bar_idx-last_high_pos
    df["close_prev"]=c.shift(1)
    return df


def compute_adx_series(df, period=14):
    h=df["high"].values.astype(float); l=df["low"].values.astype(float)
    c=df["close"].values.astype(float); n=len(c)
    tr_arr=np.zeros(n); dm_p=np.zeros(n); dm_m=np.zeros(n)
    for i in range(1,n):
        tr_arr[i]=max(h[i]-l[i],abs(h[i]-c[i-1]),abs(l[i]-c[i-1]))
        up=h[i]-h[i-1]; down=l[i-1]-l[i]
        dm_p[i]=up if (up>down and up>0) else 0.0
        dm_m[i]=down if (down>up and down>0) else 0.0
    def wilder(arr,p):
        s=np.zeros(len(arr))
        if p<len(arr): s[p]=np.sum(arr[1:p+1])
        for i in range(p+1,len(arr)): s[i]=s[i-1]-s[i-1]/p+arr[i]
        return s
    atr_s=wilder(tr_arr,period); dmp_s=wilder(dm_p,period); dmm_s=wilder(dm_m,period)
    with np.errstate(divide="ignore",invalid="ignore"):
        di_p=np.where(atr_s>0,dmp_s/atr_s*100,0.0)
        di_m=np.where(atr_s>0,dmm_s/atr_s*100,0.0)
        dx=np.where((di_p+di_m)>0,np.abs(di_p-di_m)/(di_p+di_m)*100,0.0)
    return wilder(dx,period), di_p, di_m


# ─── ÇIKIŞ FONKSİYONLARI ────────────────────────────────────────────────────
def exit_tp2(sig, pos_size):
    """TP2'de tam çıkış (orijinal compute_exit davranışı)"""
    entry=sig["entry"]; stop=sig["stop"]; tp1=sig["tp1"]; tp2=sig["tp2"]
    rows=sig["future"]; expire_h=sig.get("expire_h",EXPIRE_H)
    sp=(stop-entry)/entry; t1p=(tp1-entry)/entry; t2p=(tp2-entry)/entry
    for ts,row in rows.iloc[:expire_h].iterrows():
        h=float(row["high"]); l=float(row["low"])
        if l<=stop: return ts,pos_size*(1+sp),"stop"
        if h>=tp2:  return ts,pos_size*(1+t2p),"tp2"
        if h>=tp1:  return ts,pos_size*(1+t1p),"tp1"
    if len(rows)>0:
        idx=min(expire_h-1,len(rows)-1)
        exp_pct=(float(rows.iloc[idx]["close"])-entry)/entry
        return rows.index[idx],pos_size*(1+exp_pct),"expire"
    return sig["entry_time"],pos_size,"no_data"


def exit_tp1(sig, pos_size):
    """TP1'de tam çıkış"""
    entry=sig["entry"]; stop=sig["stop"]; tp1=sig["tp1"]
    rows=sig["future"]; expire_h=sig.get("expire_h",EXPIRE_H)
    sp=(stop-entry)/entry; t1p=(tp1-entry)/entry
    for ts,row in rows.iloc[:expire_h].iterrows():
        h=float(row["high"]); l=float(row["low"])
        if l<=stop: return ts,pos_size*(1+sp),"stop"
        if h>=tp1:  return ts,pos_size*(1+t1p),"tp1"
    if len(rows)>0:
        idx=min(expire_h-1,len(rows)-1)
        exp_pct=(float(rows.iloc[idx]["close"])-entry)/entry
        return rows.index[idx],pos_size*(1+exp_pct),"expire"
    return sig["entry_time"],pos_size,"no_data"


def exit_half(sig, pos_size):
    """SMC actual: TP1'de ½ kapat, kalan %2.5 trail ile takip"""
    entry=sig["entry"]; stop=sig["stop"]; tp1=sig["tp1"]; tp2=sig["tp2"]
    rows=sig["future"]; expire_h=sig.get("expire_h",EXPIRE_H)
    sp=(stop-entry)/entry; t1p=(tp1-entry)/entry; t2p=(tp2-entry)/entry
    peak=entry; tp1_hit=False
    for ts,row in rows.iloc[:expire_h].iterrows():
        h=float(row["high"]); l=float(row["low"])
        if not tp1_hit:
            if l<=stop: return ts,pos_size*(1+sp),"stop"
            if h>=tp1: tp1_hit=True; peak=max(entry,h)
        else:
            if h>peak: peak=h
            trail=peak*(1-SMC_TRAIL_PCT/100)
            if h>=tp2: return ts,pos_size*(1+(t1p+t2p)/2),"tp2"
            if l<=trail:
                trail_pct=(trail-entry)/entry
                return ts,pos_size*(1+(t1p+trail_pct)/2),"trail"
    if len(rows)>0:
        idx=min(expire_h-1,len(rows)-1)
        last_pct=(float(rows.iloc[idx]["close"])-entry)/entry
        ret=(t1p+last_pct)/2 if tp1_hit else last_pct
        return rows.index[idx],pos_size*(1+ret),"expire"
    return sig["entry_time"],pos_size,"no_data"


def exit_bot_actual(sig, pos_size):
    """Bot actual: %3 trailing stop, TP2'de tam çıkış"""
    entry=sig["entry"]; tp2=sig["tp2"]
    rows=sig["future"]; expire_h=sig.get("expire_h",EXPIRE_H)
    t2p=(tp2-entry)/entry; peak=entry
    for ts,row in rows.iloc[:expire_h].iterrows():
        h=float(row["high"]); l=float(row["low"])
        if h>peak: peak=h
        trail=peak*(1-BOT_TRAIL_PCT/100)
        if h>=tp2: return ts,pos_size*(1+t2p),"tp2"
        if l<=trail:
            ret=(trail-entry)/entry
            return ts,pos_size*(1+ret),"win" if ret>0 else "stop"
    if len(rows)>0:
        idx=min(expire_h-1,len(rows)-1)
        exp_pct=(float(rows.iloc[idx]["close"])-entry)/entry
        return rows.index[idx],pos_size*(1+exp_pct),"expire"
    return sig["entry_time"],pos_size,"no_data"


def exit_bot_tp1(sig, pos_size):
    return exit_tp1(sig, pos_size)


EXIT_FNS = {
    "half": exit_half,
    "tp1": exit_tp1,
    "tp2": exit_tp2,
    "bot_actual": exit_bot_actual,
    "bot_tp1": exit_bot_tp1,
}


# ─── SİNYAL TOPLAMA ─────────────────────────────────────────────────────────
def collect_all_signals(symbols, btc_filters, fetch=True):
    sigs = {k:[] for k in ["eski_choch","eski_v2","eski_disc","smc_orig","panik","t72","t168","rocket"]}

    for sym_i, symbol in enumerate(symbols, 1):
        if symbol == "BTC/USDT": continue
        print(f"  [{sym_i}/{len(symbols)}] {symbol}", flush=True)

        df_raw = load_or_fetch(symbol) if fetch else load_pkl(symbol)
        if df_raw is None or len(df_raw) < 300: continue

        df_raw = df_raw.copy()
        vol = df_raw["volume"]
        df_raw["vol_24h_usd"]  = (df_raw["close"] * vol).rolling(24).sum()
        df_raw["vol_ratio_20"] = vol / vol.rolling(20).mean().shift(1)
        df = df_raw.dropna(subset=["vol_24h_usd"]).copy()
        if len(df) < 300: continue

        try:
            df = compute_extra_indicators(df)
        except Exception as e:
            print(f"    İndikatör hatası: {e}"); continue

        disc_top, disc_bot, depth_arr = compute_luxalgo_incremental(df)
        adx_arr, di_p_arr, di_m_arr   = compute_adx_series(df)

        btc_al = btc_filters.reindex(df.index, method="ffill")

        try:
            df4 = df.resample("4h",label="right",closed="right").agg(
                {"open":"first","high":"max","low":"min","close":"last","volume":"sum"}).dropna()
            ema21_4h = df4["close"].ewm(span=21,adjust=False).mean()
            _tmp = (df4["close"]>ema21_4h).reindex(df.index,method="ffill")
            above_4h_ema = pd.Series(np.where(_tmp.isna(),True,_tmp.values),index=df.index,dtype=bool)
        except Exception:
            above_4h_ema = pd.Series(True, index=df.index)

        bts, bds, cls_, swls = run_choch_incremental(df)

        h_arr=df["high"].values; l_arr=df["low"].values
        c_arr=df["close"].values; o_arr=df["open"].values
        vol24=df["vol_24h_usd"].values; volr20=df["vol_ratio_20"].values
        atr_v=df["atr"].values; rsi_v=df["rsi"].values
        n=len(df)

        MAX_FUTURE = max(EXPIRE_H, T168_EXPIRE_H) + 10
        last  = {k:0.0 for k in ["base","v2","disc","orig","panik","t72","t168","rocket"]}
        disc_active = False

        for i in range(250, n-1):
            ts = df.index[i]
            if ts < START_DATE: continue
            price = c_arr[i]
            if np.isnan(price) or price <= 0: continue

            ts_h    = ts.timestamp() / 3600
            bf_crash = bool(btc_al["crash_ok"].iloc[i])
            bf_down  = bool(btc_al["downtrend_ok"].iloc[i])

            if np.isnan(vol24[i]) or vol24[i] < MIN_VOL_24H: continue
            vr = float(volr20[i]) if not np.isnan(volr20[i]) else 0.0

            is_choch  = (bts[i]=="CHoCH" and bds[i]=="BULLISH")
            choch_lvl = cls_[i]; sw_low = swls[i]
            atr = float(atr_v[i]) if not np.isnan(atr_v[i]) else price*0.05

            future_df = df.iloc[i+1:i+1+MAX_FUTURE][["high","low","close"]].copy()

            # ── 1 & 2. Eski CHoCH + V2 ───────────────────────────────────
            if is_choch and bf_crash and bf_down:
                entry = choch_lvl if choch_lvl else price
                stop  = sw_low*0.995 if sw_low else entry*0.95
                if stop >= entry: stop = entry*0.95
                risk  = max(entry-stop, entry*0.01)
                tp1   = entry+risk; tp2 = entry+risk*2
                sig   = {
                    "symbol":symbol,"entry_time":ts,"entry":entry,
                    "stop":stop,"tp1":tp1,"tp2":tp2,
                    "future":future_df.iloc[:EXPIRE_H].copy(),
                    "expire_h":EXPIRE_H,"vol_ratio":vr,
                    "risk_pct":round(risk/entry*100,2),
                }
                if ts_h-last["base"] >= COOLDOWN_H:
                    sigs["eski_choch"].append(sig.copy()); last["base"]=ts_h
                if vr>=1.5 and ts_h-last["v2"] >= COOLDOWN_H:
                    sigs["eski_v2"].append(sig.copy()); last["v2"]=ts_h

            # ── 3. Eski Discount ─────────────────────────────────────────
            if (not np.isnan(disc_bot[i]) and
                    price<=disc_bot[i]*(1+ESKI_DISC_DEPTH/100) and
                    ts_h-last["disc"] >= COOLDOWN_H):
                entry=price; stop=entry-atr*4; tp1=entry+atr*4; tp2=entry+atr*6
                if stop<entry and tp1>entry:
                    sigs["eski_disc"].append({
                        "symbol":symbol,"entry_time":ts,"entry":entry,
                        "stop":stop,"tp1":tp1,"tp2":tp2,
                        "future":future_df.iloc[:EXPIRE_H].copy(),
                        "expire_h":EXPIRE_H,"vol_ratio":vr,
                        "risk_pct":round((entry-stop)/entry*100,2),
                    })
                    last["disc"]=ts_h

            # ── 4. SMC Original CHoCH ────────────────────────────────────
            if (not np.isnan(depth_arr[i]) and not np.isnan(disc_top[i]) and
                    price<=disc_top[i] and depth_arr[i]>=PHASE1_DEPTH and
                    not np.isnan(rsi_v[i]) and rsi_v[i]<=PHASE1_RSI and bf_crash):
                disc_active = True

            if (disc_active and is_choch and bf_crash and
                    bool(above_4h_ema.iloc[i]) and ts_h-last["orig"] >= COOLDOWN_H):
                entry = choch_lvl if choch_lvl else price
                stop  = sw_low*0.995 if sw_low else entry*0.95
                if stop >= entry: stop = entry*0.95
                risk  = max(entry-stop, entry*0.01)
                tp1   = entry+risk; tp2 = entry+risk*2
                sigs["smc_orig"].append({
                    "symbol":symbol,"entry_time":ts,"entry":entry,
                    "stop":stop,"tp1":tp1,"tp2":tp2,
                    "future":future_df.iloc[:EXPIRE_H].copy(),
                    "expire_h":EXPIRE_H,"vol_ratio":vr,
                    "risk_pct":round(risk/entry*100,2),
                })
                last["orig"]=ts_h; disc_active=False

            # ── 5. PANİK PUMP ────────────────────────────────────────────
            close_prev = df["close_prev"].values[i]
            if (not np.isnan(close_prev) and close_prev>0 and
                    ts_h-last["panik"] >= PANIK_COOL_H):
                ret1 = (price/close_prev-1)*100
                if (PANIK_MIN<=ret1<=PANIK_MAX and PANIK_VOL_MIN<=vr<=PANIK_VOL_MAX
                        and price>=o_arr[i]):
                    c5 = c_arr[i-5] if i>=5 else price
                    if c5<=0 or (c5-price)/c5*100<4.0:
                        entry=price; stop=entry*0.97; tp1=entry*1.05; tp2=entry*1.10
                        sigs["panik"].append({
                            "symbol":symbol,"entry_time":ts,"entry":entry,
                            "stop":stop,"tp1":tp1,"tp2":tp2,
                            "future":future_df.iloc[:PANIK_EXPIRE_H].copy(),
                            "expire_h":PANIK_EXPIRE_H,"vol_ratio":vr,
                            "risk_pct":round((entry-stop)/entry*100,2),
                        })
                        last["panik"]=ts_h

            # ── 6. T72 ───────────────────────────────────────────────────
            if ts_h-last["t72"] >= T72_COOL_H:
                m5=df["mom5_pct"].values[i]; de=df["dist_ema21"].values[i]
                dd=df["coin_drawdown"].values[i]; ms=df["ma200_slope"].values[i]
                if not any(np.isnan(x) for x in [m5,de,dd,ms]):
                    if m5>=2.740 and de<=-2.737 and dd>=-26.796 and ms>=1.028:
                        entry=price; stop=entry*0.95; tp1=entry*1.10; tp2=entry*1.15
                        sigs["t72"].append({
                            "symbol":symbol,"entry_time":ts,"entry":entry,
                            "stop":stop,"tp1":tp1,"tp2":tp2,
                            "future":future_df.iloc[:T72_EXPIRE_H].copy(),
                            "expire_h":T72_EXPIRE_H,"vol_ratio":vr,
                            "risk_pct":round((entry-stop)/entry*100,2),
                        })
                        last["t72"]=ts_h

            # ── 7. T168 ──────────────────────────────────────────────────
            if ts_h-last["t168"] >= T168_COOL_H:
                dm2=df["dist_ma200"].values[i]; dm5=df["dist_ma50"].values[i]
                m10=df["mom10_pct"].values[i]; bsh=df["bars_since_high"].values[i]
                if not any(np.isnan(x) for x in [dm2,dm5,m10,bsh]):
                    if dm2>=5.657 and dm5<=-5.045 and m10>=3.941 and bsh<=677:
                        entry=price; stop=entry*0.92; tp1=entry*1.25; tp2=entry*1.25
                        sigs["t168"].append({
                            "symbol":symbol,"entry_time":ts,"entry":entry,
                            "stop":stop,"tp1":tp1,"tp2":tp2,
                            "future":future_df.iloc[:T168_EXPIRE_H].copy(),
                            "expire_h":T168_EXPIRE_H,"vol_ratio":vr,
                            "risk_pct":round((entry-stop)/entry*100,2),
                        })
                        last["t168"]=ts_h

            # ── 8. ROCKET ────────────────────────────────────────────────
            if (i>=25 and ts_h-last["rocket"] >= ROCKET_COOL_H and
                    not np.isnan(adx_arr[i]) and adx_arr[i]>=ROCKET_ADX_MIN and
                    di_p_arr[i]>di_m_arr[i] and bf_down):
                c24 = c_arr[i-24]
                chg = (price/c24-1)*100 if c24>0 else 0
                if chg>=ROCKET_CHG_MIN:
                    vol_avg=df["volume"].values[max(0,i-20):i].mean()
                    vol_now=df["volume"].values[i]
                    if vol_avg>0 and vol_now>=vol_avg*ROCKET_VOL_MIN:
                        entry=price; stop=entry*0.95; tp1=entry*1.08; tp2=entry*1.15
                        sigs["rocket"].append({
                            "symbol":symbol,"entry_time":ts,"entry":entry,
                            "stop":stop,"tp1":tp1,"tp2":tp2,
                            "future":future_df.iloc[:ROCKET_EXPIRE_H].copy(),
                            "expire_h":ROCKET_EXPIRE_H,"vol_ratio":vr,
                            "risk_pct":round((entry-stop)/entry*100,2),
                        })
                        last["rocket"]=ts_h

    for k in sigs:
        sigs[k].sort(key=lambda x: (x["entry_time"].timestamp(), -x["vol_ratio"]))
    return sigs


# ─── PORTFÖY SİMÜLASYONU ────────────────────────────────────────────────────
def simulate_portfolio(signals, exit_fn, initial_cap=INITIAL_CAP, max_positions=MAX_POSITIONS):
    cash=initial_cap; open_count=0
    open_positions={}
    trade_log=[]; equity_pts=[(START_DATE, initial_cap)]
    queue=[]; counter=0
    for sig in signals:
        heapq.heappush(queue,(sig["entry_time"].timestamp(),1,counter,"signal",sig))
        counter+=1
    while queue:
        unix_ts,_,_,etype,data = heapq.heappop(queue)
        ts = pd.Timestamp(unix_ts,unit="s",tz="UTC")
        if etype=="signal":
            if open_count>=max_positions: continue
            pos_size=cash/max_positions
            if cash<pos_size or pos_size<1: continue
            sig=data; cash-=pos_size; open_count+=1
            trade_id=counter; counter+=1
            open_positions[trade_id]=pos_size
            exit_ts,cash_ret,label = exit_fn(sig,pos_size)
            heapq.heappush(queue,(exit_ts.timestamp(),0,counter,"exit",{
                "trade_id":trade_id,"symbol":sig["symbol"],
                "entry_time":sig["entry_time"],"entry":sig["entry"],
                "stop":sig["stop"],"tp1":sig["tp1"],"tp2":sig["tp2"],
                "cash_ret":cash_ret,"label":label,"pos_size":pos_size,
                "stop_pct":round((sig["stop"]-sig["entry"])/sig["entry"]*100,2),
                "tp1_pct":round((sig["tp1"]-sig["entry"])/sig["entry"]*100,2),
                "tp2_pct":round((sig["tp2"]-sig["entry"])/sig["entry"]*100,2),
                "risk_pct":sig.get("risk_pct",0),
            }))
            counter+=1
            trade_log.append({
                "type":"ENTRY","trade_id":trade_id,"symbol":sig["symbol"],
                "time":str(ts)[:16],"entry":round(sig["entry"],6),
                "stop_pct":round((sig["stop"]-sig["entry"])/sig["entry"]*100,2),
                "tp1_pct":round((sig["tp1"]-sig["entry"])/sig["entry"]*100,2),
                "tp2_pct":round((sig["tp2"]-sig["entry"])/sig["entry"]*100,2),
                "risk_pct":sig.get("risk_pct",0),"vol_ratio":round(sig.get("vol_ratio",0),2),
                "size":pos_size,"cash_after":round(cash,2),"open":open_count,
            })
            equity_pts.append((ts,cash+sum(open_positions.values())))
        elif etype=="exit":
            d=data; cash+=d["cash_ret"]; open_count-=1
            open_positions.pop(d["trade_id"],None)
            net_pnl=d["cash_ret"]-d["pos_size"]
            trade_log.append({
                "type":"EXIT","trade_id":d["trade_id"],"symbol":d["symbol"],
                "entry_time":str(d["entry_time"])[:16],"time":str(ts)[:16],
                "label":d["label"],"net_pnl":round(net_pnl,2),
                "net_pct":round(net_pnl/d["pos_size"]*100,2),"cash_ret":round(d["cash_ret"],2),
                "cash_after":round(cash,2),"open":open_count,
                "tp1_pct":d["tp1_pct"],"tp2_pct":d["tp2_pct"],
                "stop_pct":d["stop_pct"],"risk_pct":d["risk_pct"],
            })
            equity_pts.append((ts,cash+sum(open_positions.values())))
    return trade_log, equity_pts, cash


# ─── İSTATİSTİK ─────────────────────────────────────────────────────────────
def system_stats(trade_log, equity_pts, initial_cap):
    exits  = [t for t in trade_log if t["type"]=="EXIT"]
    wins   = [e for e in exits if e["label"] in ("tp2","tp1","trail","win")]
    losses = [e for e in exits if e["label"]=="stop"]
    dec    = len(wins)+len(losses)
    wr     = len(wins)/dec*100 if dec>0 else 0.0
    final  = equity_pts[-1][1] if equity_pts else initial_cap
    ret    = (final-initial_cap)/initial_cap*100
    peak   = initial_cap; max_dd=0.0
    for _,cap in equity_pts:
        if cap>peak: peak=cap
        dd=(cap-peak)/peak*100
        if dd<max_dd: max_dd=dd
    avg_win  = sum(e["net_pct"] for e in wins)/len(wins) if wins else 0.0
    avg_loss = sum(e["net_pct"] for e in losses)/len(losses) if losses else 0.0
    return {
        "trades":len([t for t in trade_log if t["type"]=="ENTRY"]),
        "wins":len(wins),"losses":len(losses),"wr":round(wr,1),
        "final":round(final,2),"ret":round(ret,2),"max_dd":round(max_dd,2),
        "avg_win":round(avg_win,2),"avg_loss":round(avg_loss,2),
    }


# ─── 20 SENARYO ─────────────────────────────────────────────────────────────
SCENARIO_META = [
    # (key, signal_sys, exit_fn_key, display_name, exit_display, group)
    ("eski_choch_half","eski_choch","half",      "Eski CHoCH",         "actual (SMC trail)","SMC"),
    ("eski_choch_tp1", "eski_choch","tp1",       "Eski CHoCH",         "TP1 Only",          "SMC"),
    ("eski_choch_tp2", "eski_choch","tp2",       "Eski CHoCH",         "TP2 Only",          "SMC"),
    ("eski_v2_half",   "eski_v2",  "half",       "Eski CHoCH V2 ≥1.5x","actual (SMC trail)","SMC"),
    ("eski_v2_tp1",    "eski_v2",  "tp1",        "Eski CHoCH V2 ≥1.5x","TP1 Only",          "SMC"),
    ("eski_v2_tp2",    "eski_v2",  "tp2",        "Eski CHoCH V2 ≥1.5x","TP2 Only",          "SMC"),
    ("eski_disc_half", "eski_disc","half",        "Eski Discount",      "actual (SMC trail)","SMC"),
    ("eski_disc_tp1",  "eski_disc","tp1",         "Eski Discount",      "TP1 Only",          "SMC"),
    ("eski_disc_tp2",  "eski_disc","tp2",         "Eski Discount",      "TP2 Only",          "SMC"),
    ("smc_orig_half",  "smc_orig", "half",        "SMC Original CHoCH", "actual (SMC trail)","SMC"),
    ("smc_orig_tp1",   "smc_orig", "tp1",         "SMC Original CHoCH", "TP1 Only",          "SMC"),
    ("smc_orig_tp2",   "smc_orig", "tp2",         "SMC Original CHoCH", "TP2 Only",          "SMC"),
    ("panik_actual",   "panik",    "bot_actual",  "PANİK PUMP",         "actual (Bot trail)","BOT"),
    ("panik_tp1",      "panik",    "bot_tp1",     "PANİK PUMP",         "TP1 Only",          "BOT"),
    ("t72_actual",     "t72",      "bot_actual",  "T72",                "actual (Bot trail)","BOT"),
    ("t72_tp1",        "t72",      "bot_tp1",     "T72",                "TP1 Only",          "BOT"),
    ("t168_actual",    "t168",     "bot_actual",  "T168",               "actual (Bot trail)","BOT"),
    ("t168_tp1",       "t168",     "bot_tp1",     "T168",               "TP1 Only",          "BOT"),
    ("rocket_actual",  "rocket",   "bot_actual",  "ROCKET",             "actual (Bot trail)","BOT"),
    ("rocket_tp1",     "rocket",   "bot_tp1",     "ROCKET",             "TP1 Only",          "BOT"),
]

PALETTE = ["#e63946","#457b9d","#2a9d8f","#e9c46a","#f4a261","#264653","#a8dadc",
           "#6d6875","#b5838d","#e07a5f","#3d405b","#81b29a","#f2cc8f","#118ab2",
           "#06d6a0","#ef476f","#ffd166","#8338ec","#3a86ff","#fb5607"]


# ─── HTML ÇIKTI ─────────────────────────────────────────────────────────────
def generate_html(results, n_coins):
    rows=""; datasets=[]
    for idx,(key,_sig,_efn,sys_name,mode,grp) in enumerate(SCENARIO_META):
        r=results.get(key,{}); st=r.get("stats",{})
        trades=st.get("trades",0); wr=st.get("wr",0)
        final=st.get("final",INITIAL_CAP); ret=st.get("ret",0)
        avg_win=st.get("avg_win",0); max_dd=st.get("max_dd",0)
        n_sigs=r.get("n_sigs",0)
        color_ret="#00c853" if ret>=0 else "#d32f2f"
        badge="smc" if grp=="SMC" else "bot"
        rows+=(f'<tr>'
               f'<td><span class="badge badge-{badge}">{grp}</span> {sys_name} — {mode}</td>'
               f'<td>{n_sigs}</td><td>{trades}</td><td>{wr:.0f}%</td>'
               f'<td style="color:{color_ret}">{avg_win:+.2f}%</td>'
               f'<td style="color:{color_ret}">{max_dd:.1f}%</td>'
               f'<td style="color:{color_ret}">{ret:+.1f}%</td>'
               f'<td style="color:{color_ret}">${final:,.0f}</td>'
               f'<td><input type="checkbox" class="tog" data-idx="{idx}" checked></td></tr>')
        eq=r.get("equity",[])
        if eq:
            pts=[{"x":ts.strftime("%Y-%m-%d"),"y":round(v,2)} for ts,v in eq
                 if hasattr(ts,"strftime")]
            color=PALETTE[idx%len(PALETTE)]
            label=f"{sys_name} — {mode}"
            datasets.append(f'{{"label":{json.dumps(label)},"data":{json.dumps(pts)},'
                            f'"borderColor":"{color}","backgroundColor":"{color}20",'
                            f'"borderWidth":1.5,"pointRadius":0,"fill":false,"tension":0.1}}')

    ds_js="["+",".join(datasets)+"]"
    run_date=_dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    return f"""<!DOCTYPE html><html lang="tr"><head>
<meta charset="UTF-8"><title>Full Backtest — 20 Senaryo</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#0d1117;color:#c9d1d9;font-family:-apple-system,monospace;padding:20px}}
h1{{color:#58a6ff;font-size:1.4rem;margin-bottom:6px}}
.meta{{color:#8b949e;font-size:0.8rem;background:#161b22;padding:10px;border-radius:6px;border:1px solid #30363d;margin-bottom:16px}}
.card{{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:14px;margin-bottom:16px;overflow-x:auto}}
table{{width:100%;border-collapse:collapse;font-size:0.8rem;min-width:800px}}
th{{background:#21262d;color:#8b949e;padding:8px 10px;text-align:left;position:sticky;top:0}}
td{{padding:7px 10px;border-bottom:1px solid #21262d}}
tr:hover td{{background:#1c2128}}
.badge{{display:inline-block;padding:2px 6px;border-radius:4px;font-size:0.7rem;font-weight:700;margin-right:4px}}
.badge-smc{{background:#1a4a6e;color:#58a6ff}}
.badge-bot{{background:#3a1a4e;color:#bc8cff}}
canvas{{max-height:440px}}
.ctrl{{display:flex;gap:8px;margin-bottom:10px;flex-wrap:wrap}}
button{{background:#21262d;border:1px solid #30363d;color:#c9d1d9;padding:5px 12px;border-radius:5px;cursor:pointer;font-size:0.8rem}}
button:hover{{background:#30363d}}
input[type=checkbox]{{cursor:pointer;accent-color:#58a6ff}}
</style></head><body>
<h1>📊 Full Backtest — 20 Senaryo</h1>
<div class="meta">{run_date} | {n_coins} coin | 1H Binance | ${INITIAL_CAP:,.0f} başlangıç | Maks {MAX_POSITIONS} pozisyon | Dinamik boyutlama</div>
<div class="card"><table>
<thead><tr><th>Senaryo</th><th>Sinyal</th><th>Trade</th><th>WR%</th><th>Ort Kazanç</th><th>MaxDD</th><th>Getiri%</th><th>Son Sermaye</th><th>Graf.</th></tr></thead>
<tbody>{rows}</tbody></table></div>
<div class="card">
<div class="ctrl">
  <button onclick="showAll()">Tümü</button>
  <button onclick="hideAll()">Gizle</button>
  <button onclick="filterGrp('SMC')">SMC</button>
  <button onclick="filterGrp('BOT')">BOT</button>
</div>
<canvas id="ec"></canvas></div>
<script>
const ds={ds_js};
const ch=new Chart(document.getElementById('ec'),{{type:'line',data:{{datasets:ds}},options:{{
  responsive:true,animation:false,interaction:{{mode:'index',intersect:false}},
  plugins:{{legend:{{position:'bottom',labels:{{color:'#8b949e',boxWidth:12,font:{{size:10}}}}}},
    tooltip:{{backgroundColor:'#1c2128',borderColor:'#30363d',borderWidth:1,
      titleColor:'#c9d1d9',bodyColor:'#c9d1d9',
      callbacks:{{label:c=>` ${{c.dataset.label}}: $${{c.parsed.y.toLocaleString()}}`}}}}}},
  scales:{{
    x:{{type:'category',ticks:{{color:'#8b949e',maxTicksLimit:12,maxRotation:0}},grid:{{color:'#21262d'}}}},
    y:{{ticks:{{color:'#8b949e',callback:v=>'$'+v.toLocaleString()}},grid:{{color:'#21262d'}},
       title:{{display:true,text:'Portföy ($)',color:'#8b949e'}}}}
  }}
}}}});
document.querySelectorAll('.tog').forEach(cb=>cb.addEventListener('change',function(){{
  ch.data.datasets[+this.dataset.idx].hidden=!this.checked;ch.update();
}}));
function showAll(){{ch.data.datasets.forEach(d=>d.hidden=false);document.querySelectorAll('.tog').forEach(c=>c.checked=true);ch.update();}}
function hideAll(){{ch.data.datasets.forEach(d=>d.hidden=true);document.querySelectorAll('.tog').forEach(c=>c.checked=false);ch.update();}}
const smcKw=['Eski','SMC'];
function filterGrp(g){{
  ch.data.datasets.forEach((d,i)=>{{
    const isSmc=smcKw.some(k=>d.label.includes(k));
    d.hidden=g==='SMC'?!isSmc:isSmc;
  }});
  document.querySelectorAll('.tog').forEach((c,i)=>{{if(i<ch.data.datasets.length)c.checked=!ch.data.datasets[i].hidden;}});
  ch.update();
}}
</script></body></html>"""


# ─── RAPOR ──────────────────────────────────────────────────────────────────
def print_report(results, n_coins):
    W=115
    print("\n"+"═"*W)
    print(f"  FULL BACKTEST — 20 Senaryo | {n_coins} coin | 1H Binance | ${INITIAL_CAP:,.0f} başlangıç")
    print("═"*W)
    print(f"  {'Senaryo':<45} {'Sinyal':>7} {'Trade':>6} {'WR%':>6} {'AvgWin':>8} {'MaxDD':>7} {'Getiri':>8} {'Son Sermaye':>12}")
    print("─"*W)
    prev_grp=""
    for key,_sig,_efn,sys_name,mode,grp in SCENARIO_META:
        if grp!=prev_grp: print("─"*W); prev_grp=grp
        r=results.get(key,{}); st=r.get("stats",{})
        trades=st.get("trades",0); wr=st.get("wr",0)
        final=st.get("final",INITIAL_CAP); ret=st.get("ret",0)
        avg_win=st.get("avg_win",0); max_dd=st.get("max_dd",0)
        n_sigs=r.get("n_sigs",0)
        label=f"{sys_name} — {mode}"
        print(f"  [{grp}] {label:<41} {n_sigs:7d} {trades:6d} {wr:6.1f}% {avg_win:+8.2f}% {max_dd:7.1f}% {ret:+8.1f}% ${final:11,.2f}")
    print("═"*W+"\n")


# ─── MAIN ───────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-fetch", action="store_true")
    ap.add_argument("--coins",    nargs="*")
    args = ap.parse_args()

    do_fetch = not args.no_fetch

    btc_raw = load_or_fetch("BTC/USDT") if do_fetch else load_pkl("BTC/USDT")
    if btc_raw is None:
        print("HATA: BTC/USDT verisi yok."); return
    print("BTC 4H filtreleri hesaplanıyor...")
    btc_filters = compute_btc_filters(btc_raw)

    if args.coins:
        symbols = list(args.coins)
        if "BTC/USDT" not in symbols: symbols.insert(0,"BTC/USDT")
    elif args.no_fetch:
        symbols = get_cached_symbols()
    else:
        print("Binance spot listesi alınıyor...")
        symbols = get_all_binance_symbols() or get_cached_symbols()

    if not symbols:
        print("Geçerli coin bulunamadı."); return

    print(f"\n{len(symbols)} coin | ${INITIAL_CAP:,.0f} sermaye | maks {MAX_POSITIONS} pozisyon")
    print(f"Sinyaller toplanıyor ({START_DATE.date()} → bugün)...\n")

    sigs = collect_all_signals(symbols, btc_filters, fetch=do_fetch)

    for sys_name, sig_list in sigs.items():
        print(f"  {sys_name}: {len(sig_list)} sinyal")

    print("\nPortföy simülasyonları çalışıyor...")
    results = {}
    for key, sig_sys, exit_fn_key, *_ in SCENARIO_META:
        sig_list = sigs[sig_sys]
        fn       = EXIT_FNS[exit_fn_key]
        log, eq, final_cash = simulate_portfolio(sig_list, fn)
        st = system_stats(log, eq, INITIAL_CAP)
        results[key] = {"log":log, "equity":eq, "final":final_cash, "stats":st, "n_sigs":len(sig_list)}

    print_report(results, len(symbols))

    html = generate_html(results, len(symbols))
    with open("backtest_results.html","w",encoding="utf-8") as f: f.write(html)
    print("✓ backtest_results.html kaydedildi")

    summary = {k:{"n_sigs":v["n_sigs"],"stats":v["stats"]} for k,v in results.items()}
    with open("backtest_results.json","w",encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
    print("✓ backtest_results.json kaydedildi\n")


if __name__ == "__main__":
    main()
