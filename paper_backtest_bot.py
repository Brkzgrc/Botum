#!/usr/bin/env python3
"""
Bot.py Sistemleri Backtest — 2022'den bugüne
Panik Pump / Rocket / T72 / T168 (normal + v2)

Kullanım:
  python paper_backtest_bot.py              # Cache varsa kullan
  python paper_backtest_bot.py --no-fetch  # Sadece cache
  python paper_backtest_bot.py --coins BTC ETH SOL

NOT: backtest_data/ klasörü SMC backtesti ile ortak — mevcut cache kullanılır.
     rm -rf backtest_data/ && python paper_backtest_bot.py  (taze indirme)
"""

import argparse, heapq, json, os, pickle, time
import datetime as _dt
import numpy as np, pandas as pd

# ─── AYARLAR ────────────────────────────────────────────────────────────────
DATA_DIR      = "backtest_data"
START_DATE    = pd.Timestamp("2022-01-01", tz="UTC")
INITIAL_CAP   = 5_000.0
MAX_POSITIONS = 5
MAX_POS_SIZE  = 20_000.0
MIN_VOL_24H   = 5_000_000
BTC_CRASH_PCT = 3.0
CHOCH_SWING   = 5

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
PP_CRASH_MIN  = -15.0;  PP_CRASH_MAX  = -7.0
PP_VOL_MIN    = 1.5;    PP_V2_VOL_MIN = 2.0;  PP_VOL_MAX = 3.0
PP_TRAIL_PCT  = 3.0;    PP_COOLDOWN_H = 4;    PP_EXPIRE_H = 24

RK_CHANGE_24H = 10.0
RK_VOL_MIN    = 1.2;    RK_V2_VOL_MIN = 2.0
RK_ADX_MIN    = 25;     RK_V2_ADX_MIN = 30
RK_TRAIL_PCT  = 3.0;    RK_COOLDOWN_H = 12;   RK_EXPIRE_H = 48

T72_MOM5  = 2.740; T72_DEMA21 = -2.737; T72_DRAWDOWN = -26.796; T72_MA200S = 1.028
T72_COOLDOWN_H = 4; T72_EXPIRE_H = 72

T168_DMA200 = 5.657; T168_DMA50 = -5.045; T168_MOM10 = 3.941; T168_DAYS = 677
T168_COOLDOWN_H = 4; T168_EXPIRE_H = 168


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
    # BTC EMA200 (4H) — Rocket filtresi: bot.py close<EMA200 ise "🔴 Düşüş" → sinyal yok
    df4["ema200_ok"] = df4["close"] > df4["close"].ewm(span=200,adjust=False).mean()
    idx=btc_1h.index
    crash_s     = df4["crash_ok"].reindex(idx,method="ffill").fillna(True).astype(bool)
    downtrend_s = df4["downtrend"].reindex(idx,method="ffill").fillna(False).astype(bool)
    ema200_s    = df4["ema200_ok"].reindex(idx,method="ffill").fillna(True).astype(bool)
    return pd.DataFrame({"crash_ok":crash_s,"downtrend_ok":~downtrend_s,"ema200_ok":ema200_s},index=idx)


# ─── ADX + DI ───────────────────────────────────────────────────────────────
def calc_adx_di(h_arr, l_arr, c_arr, period=14):
    n = len(c_arr)
    tr=np.zeros(n); pdm=np.zeros(n); ndm=np.zeros(n)
    for i in range(1, n):
        tr[i]  = max(h_arr[i]-l_arr[i], abs(h_arr[i]-c_arr[i-1]), abs(l_arr[i]-c_arr[i-1]))
        up = h_arr[i]-h_arr[i-1]; dn = l_arr[i-1]-l_arr[i]
        pdm[i] = up if up>dn and up>0 else 0
        ndm[i] = dn if dn>up and dn>0 else 0
    def wilder(arr, p):
        s=np.zeros(n)
        if p>=n: return s
        s[p]=arr[1:p+1].sum()
        for i in range(p+1,n): s[i]=s[i-1]-s[i-1]/p+arr[i]
        return s
    atr_s=wilder(tr,period); pdm_s=wilder(pdm,period); ndm_s=wilder(ndm,period)
    with np.errstate(invalid="ignore", divide="ignore"):
        pdi = np.where(atr_s>0, 100*pdm_s/atr_s, 0.0)
        ndi = np.where(atr_s>0, 100*ndm_s/atr_s, 0.0)
        dx  = np.where(pdi+ndi>0, 100*np.abs(pdi-ndi)/(pdi+ndi), 0.0)
    adx=np.zeros(n); start=period*2
    if start<n:
        adx[start]=dx[period:start+1].mean()
        for i in range(start+1,n): adx[i]=(adx[i-1]*(period-1)+dx[i])/period
    return adx, pdi, ndi


# ─── ÇIKIŞ FONKSİYONLARI ────────────────────────────────────────────────────
def exit_tp2(sig, pos_size):
    """T72 / T168: stop veya TP2'de tam çıkış."""
    entry=sig["entry"]; stop=sig["stop"]; tp1=sig["tp1"]; tp2=sig["tp2"]
    rows=sig["future"]; expire_h=sig.get("expire_h",168)
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


def exit_trail_pp(sig, pos_size):
    """Panik Pump: TP1 milestone → trailing -%3 peak → TP2 tam çıkış."""
    return _exit_trail(sig, pos_size, trail_pct=PP_TRAIL_PCT)


def exit_trail_rk(sig, pos_size):
    """Rocket: TP1 milestone → trailing -%3 peak → TP2 tam çıkış."""
    return _exit_trail(sig, pos_size, trail_pct=RK_TRAIL_PCT)


def _exit_trail(sig, pos_size, trail_pct):
    entry=sig["entry"]; stop=sig["stop"]; tp1=sig["tp1"]; tp2=sig["tp2"]
    rows=sig["future"]; expire_h=sig.get("expire_h",168)
    sp=(stop-entry)/entry; t2p=(tp2-entry)/entry
    peak=entry; tp1_hit=False
    for ts,row in rows.iloc[:expire_h].iterrows():
        h=float(row["high"]); l=float(row["low"])
        if h>peak: peak=h
        if not tp1_hit:
            if l<=stop: return ts,pos_size*(1+sp),"stop"
            if h>=tp1: tp1_hit=True
        else:
            trail=peak*(1-trail_pct/100)
            if h>=tp2: return ts,pos_size*(1+t2p),"tp2"
            if l<=trail:
                tpct=(trail-entry)/entry
                return ts,pos_size*(1+tpct),"trail"
    if len(rows)>0:
        idx=min(expire_h-1,len(rows)-1)
        exp_pct=(float(rows.iloc[idx]["close"])-entry)/entry
        return rows.index[idx],pos_size*(1+exp_pct),"expire"
    return sig["entry_time"],pos_size,"no_data"


EXIT_FNS = {
    "trail_pp": exit_trail_pp,
    "trail_rk": exit_trail_rk,
    "tp2":      exit_tp2,
}


# ─── SİNYAL TOPLAMA ─────────────────────────────────────────────────────────
def collect_bot_signals(symbols, btc_filters, fetch=True):
    sigs = {k:[] for k in ["pp","pp_v2","pp_adx40","pp_adx40_deep","rocket","rocket_v2","t72","t72_v2","t168","t168_v2"]}

    for sym_i, symbol in enumerate(symbols, 1):
        if symbol == "BTC/USDT": continue
        print(f"  [{sym_i}/{len(symbols)}] {symbol}", flush=True)

        df_raw = load_or_fetch(symbol) if fetch else load_pkl(symbol)
        if df_raw is None or len(df_raw) < 300: continue

        df = df_raw.copy()
        c = df["close"]; v = df["volume"]

        # Hacim
        df["vol_ma"]     = v.rolling(20).mean().shift(1)
        df["vol_ratio"]  = v / df["vol_ma"].replace(0, np.nan)
        df["vol_24h_usd"]= (c * v).rolling(24).sum()

        # Panik Pump
        df["ret1"]  = (c / c.shift(1) - 1) * 100
        df["c5ago"] = c.shift(5)

        # Rocket
        df["change_24h"] = (c / c.shift(25) - 1) * 100

        # T72
        ema21            = c.ewm(span=21, adjust=False).mean()
        df["dist_ema21"] = (c - ema21) / ema21.replace(0, np.nan) * 100
        df["mom5_pct"]   = (c / c.shift(5) - 1) * 100
        roll_max         = c.rolling(700, min_periods=50).max()
        df["drawdown"]   = (c - roll_max) / roll_max.replace(0, np.nan) * 100

        # T72 & T168
        ma50  = c.rolling(50).mean()
        ma200 = c.rolling(200).mean()
        df["dist_ma50"]   = (c - ma50)  / ma50.replace(0, np.nan)  * 100
        df["dist_ma200"]  = (c - ma200) / ma200.replace(0, np.nan) * 100
        df["ma200_slope"] = (ma200 - ma200.shift(20)) / ma200.shift(20).abs().replace(0, np.nan) * 100
        df["mom10_pct"]   = (c / c.shift(10) - 1) * 100
        bar_idx = pd.Series(np.arange(len(c), dtype=float), index=c.index)
        df["days_high"]   = bar_idx - bar_idx.where(c >= roll_max*(1-1e-6)).ffill().fillna(0)

        df.dropna(subset=["vol_ma","ret1","change_24h"], inplace=True)
        if len(df) < 300: continue

        btc = btc_filters.reindex(df.index, method="ffill")
        adx_arr, pdi_arr, ndi_arr = calc_adx_di(
            df["high"].values, df["low"].values, df["close"].values, period=14)

        n = len(df)
        last = {k: 0.0 for k in sigs}

        for i in range(250, n-1):
            ts = df.index[i]
            if ts < START_DATE: continue

            vol24 = float(df["vol_24h_usd"].iloc[i])
            if np.isnan(vol24) or vol24 < MIN_VOL_24H: continue

            price = float(df["close"].iloc[i])
            if np.isnan(price) or price <= 0: continue

            ret1      = float(df["ret1"].iloc[i])
            vol_ratio = float(df["vol_ratio"].iloc[i])
            ts_h      = ts.timestamp() / 3600

            crash_ok    = bool(btc["crash_ok"].iloc[i])
            downtrend_ok= bool(btc["downtrend_ok"].iloc[i])
            ema200_ok   = bool(btc["ema200_ok"].iloc[i])
            adx_val     = float(adx_arr[i])
            pdi_val     = float(pdi_arr[i])

            # ── PANİK PUMP ──────────────────────────────────────────────────
            if (PP_CRASH_MIN <= ret1 <= PP_CRASH_MAX and not np.isnan(vol_ratio)):
                c5 = float(df["c5ago"].iloc[i])
                if not np.isnan(c5) and c5 > 0 and not ((c5-price)/c5*100 >= 4.0):
                    entry = price
                    stop  = round(entry * 0.97, 10)
                    tp1   = round(entry * 1.05, 10)
                    tp2   = round(entry * 1.10, 10)
                    base  = dict(symbol=symbol, entry_time=ts, entry=entry,
                                 stop=stop, tp1=tp1, tp2=tp2,
                                 future=df.iloc[i+1:i+1+PP_EXPIRE_H][["high","low","close"]].copy(),
                                 expire_h=PP_EXPIRE_H, vol_ratio=round(vol_ratio,2), ret1=round(ret1,2),
                                 adx=round(adx_val,1))
                    if PP_VOL_MIN <= vol_ratio <= PP_VOL_MAX:
                        if ts_h - last["pp"] >= PP_COOLDOWN_H:
                            sigs["pp"].append(base.copy()); last["pp"] = ts_h
                    if PP_V2_VOL_MIN <= vol_ratio <= PP_VOL_MAX and crash_ok and downtrend_ok:
                        if ts_h - last["pp_v2"] >= PP_COOLDOWN_H:
                            sigs["pp_v2"].append(base.copy()); last["pp_v2"] = ts_h
                    if PP_VOL_MIN <= vol_ratio <= PP_VOL_MAX and adx_val >= 40:
                        if ts_h - last["pp_adx40"] >= PP_COOLDOWN_H:
                            sigs["pp_adx40"].append(base.copy()); last["pp_adx40"] = ts_h
                    if PP_VOL_MIN <= vol_ratio <= PP_VOL_MAX and adx_val >= 40 and ret1 <= -8.0:
                        if ts_h - last["pp_adx40_deep"] >= PP_COOLDOWN_H:
                            sigs["pp_adx40_deep"].append(base.copy()); last["pp_adx40_deep"] = ts_h

            # ── ROCKET ──────────────────────────────────────────────────────
            change_24h = float(df["change_24h"].iloc[i])
            ndi_val    = float(ndi_arr[i])
            if (not np.isnan(change_24h) and change_24h >= RK_CHANGE_24H
                    and not np.isnan(vol_ratio) and pdi_val > ndi_val and ema200_ok):
                entry = price
                stop  = round(entry * 0.95, 10)
                tp1   = round(entry * 1.08, 10)
                tp2   = round(entry * 1.15, 10)
                base  = dict(symbol=symbol, entry_time=ts, entry=entry,
                             stop=stop, tp1=tp1, tp2=tp2,
                             future=df.iloc[i+1:i+1+RK_EXPIRE_H][["high","low","close"]].copy(),
                             expire_h=RK_EXPIRE_H, vol_ratio=round(vol_ratio,2),
                             change_24h=round(change_24h,2), adx=round(adx_val,1))
                if vol_ratio >= RK_VOL_MIN and adx_val >= RK_ADX_MIN:
                    if ts_h - last["rocket"] >= RK_COOLDOWN_H:
                        sigs["rocket"].append(base.copy()); last["rocket"] = ts_h
                if vol_ratio >= RK_V2_VOL_MIN and adx_val >= RK_V2_ADX_MIN and crash_ok and downtrend_ok:
                    if ts_h - last["rocket_v2"] >= RK_COOLDOWN_H:
                        sigs["rocket_v2"].append(base.copy()); last["rocket_v2"] = ts_h

            # ── T72 ─────────────────────────────────────────────────────────
            mom5=float(df["mom5_pct"].iloc[i]); de21=float(df["dist_ema21"].iloc[i])
            dd=float(df["drawdown"].iloc[i]);   ma200s=float(df["ma200_slope"].iloc[i])
            if (not any(np.isnan(v) for v in [mom5,de21,dd,ma200s])
                    and mom5>=T72_MOM5 and de21<=T72_DEMA21 and dd>=T72_DRAWDOWN and ma200s>=T72_MA200S):
                entry = price
                stop  = round(entry * 0.95, 10)
                tp1   = round(entry * 1.10, 10)
                tp2   = round(entry * 1.15, 10)
                base  = dict(symbol=symbol, entry_time=ts, entry=entry,
                             stop=stop, tp1=tp1, tp2=tp2,
                             future=df.iloc[i+1:i+1+T72_EXPIRE_H][["high","low","close"]].copy(),
                             expire_h=T72_EXPIRE_H)
                if ts_h - last["t72"] >= T72_COOLDOWN_H:
                    sigs["t72"].append(base.copy()); last["t72"] = ts_h
                if crash_ok and downtrend_ok:
                    if ts_h - last["t72_v2"] >= T72_COOLDOWN_H:
                        sigs["t72_v2"].append(base.copy()); last["t72_v2"] = ts_h

            # ── T168 ────────────────────────────────────────────────────────
            dm200=float(df["dist_ma200"].iloc[i]); dm50=float(df["dist_ma50"].iloc[i])
            m10=float(df["mom10_pct"].iloc[i]);    dh=float(df["days_high"].iloc[i])
            if (not any(np.isnan(v) for v in [dm200,dm50,m10,dh])
                    and dm200>=T168_DMA200 and dm50<=T168_DMA50 and m10>=T168_MOM10 and dh<=T168_DAYS):
                entry = price
                stop  = round(entry * 0.92, 10)
                tp1   = round(entry * 1.25, 10)
                tp2   = round(entry * 1.25, 10)
                base  = dict(symbol=symbol, entry_time=ts, entry=entry,
                             stop=stop, tp1=tp1, tp2=tp2,
                             future=df.iloc[i+1:i+1+T168_EXPIRE_H][["high","low","close"]].copy(),
                             expire_h=T168_EXPIRE_H)
                if ts_h - last["t168"] >= T168_COOLDOWN_H:
                    sigs["t168"].append(base.copy()); last["t168"] = ts_h
                if crash_ok and downtrend_ok:
                    if ts_h - last["t168_v2"] >= T168_COOLDOWN_H:
                        sigs["t168_v2"].append(base.copy()); last["t168_v2"] = ts_h

    for k in sigs:
        sigs[k].sort(key=lambda x: x["entry_time"].timestamp())
    return sigs


# ─── PORTFÖY SİMÜLASYONU ────────────────────────────────────────────────────
def simulate_portfolio(signals, exit_fn, initial_cap=INITIAL_CAP, max_positions=MAX_POSITIONS):
    cash=initial_cap; open_count=0; open_positions={}; max_open=0
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
            pos_size=min(cash/max_positions, MAX_POS_SIZE)
            if pos_size<1: continue
            sig=data; cash-=pos_size; open_count+=1
            if open_count>max_open: max_open=open_count
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
            }))
            counter+=1
            trade_log.append({
                "type":"ENTRY","trade_id":trade_id,"symbol":sig["symbol"],
                "time":str(ts)[:16],"entry":round(sig["entry"],6),
                "stop_pct":round((sig["stop"]-sig["entry"])/sig["entry"]*100,2),
                "tp1_pct":round((sig["tp1"]-sig["entry"])/sig["entry"]*100,2),
                "tp2_pct":round((sig["tp2"]-sig["entry"])/sig["entry"]*100,2),
                "vol_ratio":round(sig.get("vol_ratio",0),2),
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
                "tp1_pct":d["tp1_pct"],"tp2_pct":d["tp2_pct"],"stop_pct":d["stop_pct"],
            })
            equity_pts.append((ts,cash+sum(open_positions.values())))
    return trade_log, equity_pts, cash, max_open


# ─── İSTATİSTİK ─────────────────────────────────────────────────────────────
def system_stats(trade_log, equity_pts):
    exits   = [t for t in trade_log if t["type"]=="EXIT"]
    wins    = [e for e in exits if e["label"] in ("tp2","tp1","trail","win")]
    stops   = [e for e in exits if e["label"] in ("stop","time_stop")]
    expires = [e for e in exits if e["label"]=="expire"]
    dec     = len(wins)+len(stops)
    wr      = len(wins)/dec*100 if dec>0 else 0.0
    final   = equity_pts[-1][1] if equity_pts else INITIAL_CAP
    ret     = (final-INITIAL_CAP)/INITIAL_CAP*100
    peak    = INITIAL_CAP; max_dd=0.0
    for _,cap in equity_pts:
        if cap>peak: peak=cap
        dd=(cap-peak)/peak*100
        if dd<max_dd: max_dd=dd
    avg_win  = sum(e["net_pct"] for e in wins)/len(wins)   if wins   else 0.0
    avg_loss = sum(e["net_pct"] for e in stops)/len(stops) if stops  else 0.0
    stop_early=stop_mid=stop_late=0
    for s in stops:
        try:
            hours=(pd.Timestamp(s["time"])-pd.Timestamp(s["entry_time"])).total_seconds()/3600
            if hours<24:   stop_early+=1
            elif hours<48: stop_mid+=1
            else:          stop_late+=1
        except Exception: pass
    return {
        "trades":len([t for t in trade_log if t["type"]=="ENTRY"]),
        "wins":len(wins),"losses":len(stops),"time_stops":0,
        "expires":len(expires),"wr":round(wr,1),
        "final":round(final,2),"ret":round(ret,2),"max_dd":round(max_dd,2),
        "avg_win":round(avg_win,2),"avg_loss":round(avg_loss,2),
        "stop_early":stop_early,"stop_mid":stop_mid,"stop_late":stop_late,
    }


# ─── SENARYOLAR ─────────────────────────────────────────────────────────────
SCENARIO_META = [
    ("pp",              "pp",              "trail_pp", "Panik Pump",         "vol 1.5-3.0x"),
    ("pp_v2",           "pp_v2",           "trail_pp", "Panik Pump V2",      "vol 2.0-3.0x + BTC"),
    ("pp_adx40",        "pp_adx40",        "trail_pp", "Panik Pump ADX≥40",  "vol 1.5-3.0x + ADX≥40"),
    ("pp_adx40_deep",   "pp_adx40_deep",   "trail_pp", "Panik Pump ADX≥40+", "vol 1.5-3.0x + ADX≥40 + düşüş≤-8%"),
    ("rocket",          "rocket",          "trail_rk", "Rocket",             "vol 1.2x ADX≥25"),
    ("rocket_v2",       "rocket_v2",       "trail_rk", "Rocket V2",          "vol 2.0x ADX≥30"),
    ("t72",             "t72",             "tp2",      "T72",                "orijinal"),
    ("t72_v2",          "t72_v2",          "tp2",      "T72 V2",             "orijinal + BTC"),
    ("t168",            "t168",            "tp2",      "T168",               "orijinal"),
    ("t168_v2",         "t168_v2",         "tp2",      "T168 V2",            "orijinal + BTC"),
]

PALETTE = ["#e63946","#f4a261","#457b9d","#2a9d8f","#e9c46a","#264653","#9b59b6","#1abc9c"]


# ─── HTML ÇIKTI ─────────────────────────────────────────────────────────────
def generate_html(results, n_coins, active_scenarios=None):
    if active_scenarios is None: active_scenarios = SCENARIO_META
    rows=""; datasets=[]
    for idx,(key,_sig,_efn,sys_name,mode) in enumerate(active_scenarios):
        r=results.get(key,{}); st=r.get("stats",{})
        trades=st.get("trades",0); wr=st.get("wr",0)
        final=st.get("final",INITIAL_CAP); ret=st.get("ret",0)
        avg_win=st.get("avg_win",0); max_dd=st.get("max_dd",0)
        n_sigs=r.get("n_sigs",0)
        color_ret="#00c853" if ret>=0 else "#d32f2f"
        rows+=(f'<tr>'
               f'<td>{sys_name} — {mode}</td>'
               f'<td>{n_sigs}</td><td>{trades}</td><td>{wr:.0f}%</td>'
               f'<td style="color:{color_ret}">{avg_win:+.2f}%</td>'
               f'<td style="color:{color_ret}">{max_dd:.1f}%</td>'
               f'<td style="color:{color_ret}">{ret:+.1f}%</td>'
               f'<td style="color:{color_ret}">${final:,.0f}</td>'
               f'<td><input type="checkbox" class="tog" data-idx="{idx}" checked></td></tr>')
        eq=r.get("equity",[])
        if eq:
            pts=[{"x":ts.strftime("%Y-%m-%d"),"y":round(v,2)} for ts,v in eq if hasattr(ts,"strftime")]
            color=PALETTE[idx%len(PALETTE)]
            datasets.append(f'{{"label":{json.dumps(f"{sys_name} — {mode}")},"data":{json.dumps(pts)},'
                            f'"borderColor":"{color}","backgroundColor":"{color}20",'
                            f'"borderWidth":2,"pointRadius":0,"fill":false,"tension":0.1}}')
    ds_js="["+",".join(datasets)+"]"
    run_date=_dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    return f"""<!DOCTYPE html><html lang="tr"><head>
<meta charset="UTF-8"><title>Bot Backtest 2022</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#0d1117;color:#c9d1d9;font-family:-apple-system,monospace;padding:20px}}
h1{{color:#58a6ff;font-size:1.4rem;margin-bottom:6px}}
.meta{{color:#8b949e;font-size:0.8rem;background:#161b22;padding:10px;border-radius:6px;border:1px solid #30363d;margin-bottom:16px}}
.card{{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:14px;margin-bottom:16px;overflow-x:auto}}
table{{width:100%;border-collapse:collapse;font-size:0.82rem;min-width:700px}}
th{{background:#21262d;color:#8b949e;padding:8px 10px;text-align:left;position:sticky;top:0}}
td{{padding:7px 10px;border-bottom:1px solid #21262d}}
tr:hover td{{background:#1c2128}}
canvas{{max-height:480px}}
.ctrl{{display:flex;gap:8px;margin-bottom:10px}}
button{{background:#21262d;border:1px solid #30363d;color:#c9d1d9;padding:5px 12px;border-radius:5px;cursor:pointer;font-size:0.8rem}}
button:hover{{background:#30363d}}
input[type=checkbox]{{cursor:pointer;accent-color:#58a6ff}}
</style></head><body>
<h1>📊 Bot Backtest — 10 Senaryo (2022 → bugün)</h1>
<div class="meta">{run_date} | {n_coins} coin | 1H Binance | ${INITIAL_CAP:,.0f} başlangıç | Maks {MAX_POSITIONS} pozisyon | Dinamik boyutlama</div>
<div class="card"><table>
<thead><tr><th>Senaryo</th><th>Sinyal</th><th>Trade</th><th>WR%</th><th>Ort Kazanç</th><th>MaxDD</th><th>Getiri%</th><th>Son Sermaye</th><th>Graf.</th></tr></thead>
<tbody>{rows}</tbody></table></div>
<div class="card">
<div class="ctrl">
  <button onclick="showAll()">Tümü</button>
  <button onclick="hideAll()">Gizle</button>
</div>
<canvas id="ec"></canvas></div>
<script>
const ds={ds_js};
const ch=new Chart(document.getElementById('ec'),{{type:'line',data:{{datasets:ds}},options:{{
  responsive:true,animation:false,interaction:{{mode:'index',intersect:false}},
  plugins:{{legend:{{position:'bottom',labels:{{color:'#8b949e',boxWidth:12,font:{{size:11}}}}}},
    tooltip:{{backgroundColor:'#1c2128',borderColor:'#30363d',borderWidth:1,
      titleColor:'#c9d1d9',bodyColor:'#c9d1d9',
      callbacks:{{label:c=>` ${{c.dataset.label}}: $${{c.parsed.y.toLocaleString()}}`}}}}}},
  scales:{{
    x:{{type:'category',ticks:{{color:'#8b949e',maxTicksLimit:16,maxRotation:0}},grid:{{color:'#21262d'}}}},
    y:{{ticks:{{color:'#8b949e',callback:v=>'$'+v.toLocaleString()}},grid:{{color:'#21262d'}},
       title:{{display:true,text:'Portföy ($)',color:'#8b949e'}}}}
  }}
}}}});
document.querySelectorAll('.tog').forEach(cb=>cb.addEventListener('change',function(){{
  ch.data.datasets[+this.dataset.idx].hidden=!this.checked;ch.update();
}}));
function showAll(){{ch.data.datasets.forEach(d=>d.hidden=false);document.querySelectorAll('.tog').forEach(c=>c.checked=true);ch.update();}}
function hideAll(){{ch.data.datasets.forEach(d=>d.hidden=true);document.querySelectorAll('.tog').forEach(c=>c.checked=false);ch.update();}}
</script></body></html>"""


# ─── RAPOR ──────────────────────────────────────────────────────────────────
def print_report(results, n_coins, active_scenarios=None):
    if active_scenarios is None: active_scenarios = SCENARIO_META
    W=120
    print("\n"+"═"*W)
    print(f"  BOT BACKTEST | {n_coins} coin | 2022→bugün | ${INITIAL_CAP:,.0f} başlangıç | Pozisyon max ${MAX_POS_SIZE:,.0f}")
    print("═"*W)
    print(f"  {'Senaryo':<45} {'Sinyal':>7} {'Trade':>6} {'Kazanç':>7} {'Stop':>6} {'Expire':>7} {'WR%':>6} {'AvgWin':>8} {'AvgLoss':>8} {'MaxDD':>7} {'Getiri':>9} {'Son Sermaye':>13}")
    print("─"*W)
    for key,_sig,_efn,sys_name,mode in active_scenarios:
        r=results.get(key,{}); st=r.get("stats",{})
        trades=st.get("trades",0); wr=st.get("wr",0)
        wins=st.get("wins",0); losses=st.get("losses",0); expires=st.get("expires",0)
        final=st.get("final",INITIAL_CAP); ret=st.get("ret",0)
        avg_win=st.get("avg_win",0); avg_loss=st.get("avg_loss",0); max_dd=st.get("max_dd",0)
        se=st.get("stop_early",0); sm=st.get("stop_mid",0); sl_=st.get("stop_late",0)
        max_open=st.get("max_open",0); n_sigs=r.get("n_sigs",0)
        label=f"{sys_name} — {mode}"
        print(f"  {label:<45} {n_sigs:7d} {trades:6d} {wins:7d} {losses:6d} {expires:7d} {wr:6.1f}% {avg_win:+8.2f}% {avg_loss:+8.2f}% {max_dd:7.1f}% {ret:+9.1f}% ${final:12,.2f}")
        if losses:
            print(f"  {'':45}  Max eş zamanlı: {max_open} | Stop zamanlaması → <24H:{se}  24-48H:{sm}  >48H:{sl_}")
    print("═"*W+"\n")


# ─── MAIN ───────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-fetch",  action="store_true")
    ap.add_argument("--coins",     nargs="*")
    ap.add_argument("--scenarios", nargs="*")
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

    sigs = collect_bot_signals(symbols, btc_filters, fetch=do_fetch)
    for k,v in sigs.items():
        print(f"  {k}: {len(v)} sinyal")

    active_scenarios = SCENARIO_META
    if args.scenarios:
        active_scenarios = [s for s in SCENARIO_META if s[0] in args.scenarios]
        if not active_scenarios:
            print(f"Senaryo bulunamadı: {args.scenarios}"); return

    print("\nPortföy simülasyonları çalışıyor...")
    results = {}
    for key, sig_sys, exit_fn_key, *_ in active_scenarios:
        sig_list = sigs[sig_sys]
        fn       = EXIT_FNS[exit_fn_key]
        log, eq, final_cash, max_open = simulate_portfolio(sig_list, fn)
        st = system_stats(log, eq)
        st["max_open"] = max_open
        results[key] = {"log":log,"equity":eq,"final":final_cash,"stats":st,"n_sigs":len(sig_list)}

    print_report(results, len(symbols), active_scenarios)

    html = generate_html(results, len(symbols), active_scenarios)
    with open("backtest_results_bot.html","w",encoding="utf-8") as f: f.write(html)
    print("✓ backtest_results_bot.html kaydedildi")

    summary = {k:{"n_sigs":v["n_sigs"],"stats":v["stats"]} for k,v in results.items()}
    with open("backtest_results_bot.json","w",encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
    print("✓ backtest_results_bot.json kaydedildi\n")


if __name__ == "__main__":
    main()
