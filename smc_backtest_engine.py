#!/usr/bin/env python3
"""
SMC Backtest — 5 Senaryo (2022'den bugüne)
En başarılı SMC sistemleri: Eski CHoCH, Eski CHoCH V2, SMC Original

Kullanım:
  python smc_backtest_engine.py              # Cache varsa kullan
  python smc_backtest_engine.py --no-fetch  # Sadece cache
  python smc_backtest_engine.py --coins BTC ETH SOL

NOT: 2022 verisi için eski cache silinip yeniden indirilmeli.
     rm -rf backtest_data/ && python smc_backtest_engine.py
"""

import argparse, heapq, json, os, pickle, time
import datetime as _dt
import numpy as np, pandas as pd

# ─── AYARLAR ────────────────────────────────────────────────────────────────────────────
DATA_DIR      = "backtest_data"
START_DATE    = pd.Timestamp("2022-01-01", tz="UTC")
INITIAL_CAP   = 5_000.0
MAX_POSITIONS = 5
FEE_RATE      = 0.001   # %0.1 — giriş ve çıkışta ayrı ayrı uygulanır
SLIPPAGE      = 0.0005  # %0.05 — giriş fiyatına eklenir
PORTFOLIO_ALLOC = 0.20
MAX_POS_SIZE  = 20_000.0   # pozisyon başına maksimum dolar
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

# 2021 başından veri çek — warmup için yeterli geçmiş
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


# ─── VERİ ────────────────────────────────────────────────────────────────────────────
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
    if df is not None:
        return df
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


# ─── BTC FİLTRELİ ────────────────────────────────────────────────────────────────────────────
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
    crash_s     = df4["crash_ok"].reindex(idx,method="ffill").fillna(True).astype(bool)
    downtrend_s = df4["downtrend"].reindex(idx,method="ffill").fillna(False).astype(bool)
    return pd.DataFrame({"crash_ok":crash_s,"downtrend_ok":~downtrend_s},index=idx)


# ─── CHoCH TESPİTİ ────────────────────────────────────────────────────────────────────────────
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


# ─── LUXALGO DISCOUNT ZONE ────────────────────────────────────────────────────────────────────────────
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


# ─── SMC ORIGINAL İÇİN EK İNDİKATÖRLER ───────────────────────────────────────────────────────────────────────
def compute_rsi_atr(df):
    c=df["close"]; prev_c=c.shift(1)
    h=df["high"]; l=df["low"]
    tr=pd.concat([h-l,(h-prev_c).abs(),(l-prev_c).abs()],axis=1).max(axis=1)
    atr=tr.ewm(alpha=1/14,adjust=False).mean()
    delta=c.diff()
    gain=delta.clip(lower=0).ewm(alpha=1/14,adjust=False).mean()
    loss=(-delta).clip(lower=0).ewm(alpha=1/14,adjust=False).mean()
    rsi=100-100/(1+gain/loss.replace(0,np.nan))
    return atr.values, rsi.values


# ─── ÇIKIŞ FONKSİYONLARI ────────────────────────────────────────────────────────────────────────────
# Slippage: giriş fiyatına eklenir → stop/TP yüzdeleri buna göre hesaplanır
# FEE_RATE: çıkışta dönen tutardan kesilir (%0.1 çıkış komisyonu)
# Giriş komisyonu (%0.1) simulate_portfolio'da ayrıca uygulanır

def exit_tp2(sig, pos_size):
    entry=sig["entry"]; stop=sig["stop"]; tp1=sig["tp1"]; tp2=sig["tp2"]
    rows=sig["future"]; expire_h=sig.get("expire_h",EXPIRE_H)
    eff_entry = entry * (1 + SLIPPAGE)
    sp=(stop-eff_entry)/eff_entry; t1p=(tp1-eff_entry)/eff_entry; t2p=(tp2-eff_entry)/eff_entry
    for i,(ts,row) in enumerate(rows.iloc[:expire_h].iterrows()):
        h=float(row["high"]); l=float(row["low"]); c=float(row["close"])
        if l<=stop: return ts, pos_size*(1+sp)*(1-FEE_RATE), "stop"
        if h>=tp2:  return ts, pos_size*(1+t2p)*(1-FEE_RATE), "tp2"
        if h>=tp1:  return ts, pos_size*(1+t1p)*(1-FEE_RATE), "tp1"
    if len(rows)>0:
        idx=min(expire_h-1,len(rows)-1)
        exp_pct=(float(rows.iloc[idx]["close"])-eff_entry)/eff_entry
        return rows.index[idx], pos_size*(1+exp_pct)*(1-FEE_RATE), "expire"
    return sig["entry_time"], pos_size*(1-FEE_RATE), "no_data"


def exit_tp1(sig, pos_size):
    entry=sig["entry"]; stop=sig["stop"]; tp1=sig["tp1"]
    rows=sig["future"]; expire_h=sig.get("expire_h",EXPIRE_H)
    eff_entry = entry * (1 + SLIPPAGE)
    sp=(stop-eff_entry)/eff_entry; t1p=(tp1-eff_entry)/eff_entry
    for ts,row in rows.iloc[:expire_h].iterrows():
        h=float(row["high"]); l=float(row["low"])
        if l<=stop: return ts, pos_size*(1+sp)*(1-FEE_RATE), "stop"
        if h>=tp1:  return ts, pos_size*(1+t1p)*(1-FEE_RATE), "tp1"
    if len(rows)>0:
        idx=min(expire_h-1,len(rows)-1)
        exp_pct=(float(rows.iloc[idx]["close"])-eff_entry)/eff_entry
        return rows.index[idx], pos_size*(1+exp_pct)*(1-FEE_RATE), "expire"
    return sig["entry_time"], pos_size*(1-FEE_RATE), "no_data"


def exit_half(sig, pos_size):
    entry=sig["entry"]; stop=sig["stop"]; tp1=sig["tp1"]; tp2=sig["tp2"]
    rows=sig["future"]; expire_h=sig.get("expire_h",EXPIRE_H)
    eff_entry = entry * (1 + SLIPPAGE)
    sp=(stop-eff_entry)/eff_entry; t1p=(tp1-eff_entry)/eff_entry; t2p=(tp2-eff_entry)/eff_entry
    peak=entry; tp1_hit=False
    for ts,row in rows.iloc[:expire_h].iterrows():
        h=float(row["high"]); l=float(row["low"])
        if not tp1_hit:
            if l<=stop: return ts, pos_size*(1+sp)*(1-FEE_RATE), "stop"
            if h>=tp1: tp1_hit=True; peak=max(entry,h)
        else:
            if h>peak: peak=h
            trail=peak*(1-SMC_TRAIL_PCT/100)
            if h>=tp2: return ts, pos_size*(1+(t1p+t2p)/2)*(1-FEE_RATE), "tp2"
            if l<=trail:
                trail_pct=(trail-eff_entry)/eff_entry
                return ts, pos_size*(1+(t1p+trail_pct)/2)*(1-FEE_RATE), "trail"
    if len(rows)>0:
        idx=min(expire_h-1,len(rows)-1)
        last_pct=(float(rows.iloc[idx]["close"])-eff_entry)/eff_entry
        ret=(t1p+last_pct)/2 if tp1_hit else last_pct
        return rows.index[idx], pos_size*(1+ret)*(1-FEE_RATE), "expire"
    return sig["entry_time"], pos_size*(1-FEE_RATE), "no_data"


def exit_full_trail(sig, pos_size):
    entry=sig["entry"]; stop=sig["stop"]; tp1=sig["tp1"]; tp2=sig["tp2"]
    rows=sig["future"]; expire_h=sig.get("expire_h",EXPIRE_H)
    eff_entry = entry * (1 + SLIPPAGE)
    sp=(stop-eff_entry)/eff_entry; t2p=(tp2-eff_entry)/eff_entry
    peak=entry; tp1_hit=False
    for ts,row in rows.iloc[:expire_h].iterrows():
        h=float(row["high"]); l=float(row["low"])
        if not tp1_hit:
            if l<=stop: return ts, pos_size*(1+sp)*(1-FEE_RATE), "stop"
            if h>=tp1: tp1_hit=True; peak=max(entry,h)
        else:
            if h>peak: peak=h
            trail=peak*(1-SMC_TRAIL_PCT/100)
            if h>=tp2: return ts, pos_size*(1+t2p)*(1-FEE_RATE), "tp2"
            if l<=trail:
                trail_pct=(trail-eff_entry)/eff_entry
                return ts, pos_size*(1+trail_pct)*(1-FEE_RATE), "trail"
    if len(rows)>0:
        idx=min(expire_h-1,len(rows)-1)
        last_pct=(float(rows.iloc[idx]["close"])-eff_entry)/eff_entry
        return rows.index[idx], pos_size*(1+last_pct)*(1-FEE_RATE), "expire"
    return sig["entry_time"], pos_size*(1-FEE_RATE), "no_data"


EXIT_FNS = {"half": exit_half, "tp1": exit_tp1, "tp2": exit_tp2, "full_trail": exit_full_trail}


# ─── SİNYAL TOPLAMA (sadece eski_v2) ───────────────────────────────────────────────────────────────────────
def collect_smc_signals(symbols, btc_filters, fetch=True):
    sigs = {"eski_v2": []}

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

        btc_al = btc_filters.reindex(df.index, method="ffill")
        bts, bds, cls_, swls = run_choch_incremental(df)

        c_arr=df["close"].values
        vol24=df["vol_24h_usd"].values; volr20=df["vol_ratio_20"].values
        n=len(df)
        last_v2 = 0.0

        for i in range(250, n-1):
            ts = df.index[i]
            if ts < START_DATE: continue
            price = c_arr[i]
            if np.isnan(price) or price <= 0: continue
            if np.isnan(vol24[i]) or vol24[i] < MIN_VOL_24H: continue

            vr = float(volr20[i]) if not np.isnan(volr20[i]) else 0.0
            if vr < 3.0: continue

            if not (bts[i]=="CHoCH" and bds[i]=="BULLISH"): continue
            if not bool(btc_al["crash_ok"].iloc[i]): continue
            if not bool(btc_al["downtrend_ok"].iloc[i]): continue

            ts_h = ts.timestamp() / 3600
            if ts_h - last_v2 < COOLDOWN_H: continue

            choch_lvl = cls_[i]; sw_low = swls[i]
            entry = choch_lvl if choch_lvl else price
            stop  = sw_low*0.995 if sw_low else entry*0.95
            if stop >= entry: stop = entry*0.95
            risk  = max(entry-stop, entry*0.01)
            tp1   = entry+risk; tp2 = entry+risk*2

            sigs["eski_v2"].append({
                "symbol":symbol,"entry_time":ts,"entry":entry,
                "stop":stop,"tp1":tp1,"tp2":tp2,
                "future":df.iloc[i+1:i+1+EXPIRE_H][["high","low","close"]].copy(),
                "expire_h":EXPIRE_H,"vol_ratio":vr,
                "risk_pct":round(risk/entry*100,2),
            })
            last_v2 = ts_h

    sigs["eski_v2"].sort(key=lambda x: (x["entry_time"].timestamp(), -x["vol_ratio"]))
    return sigs


# ─── PORTFÖY SİMÜLASYONU ────────────────────────────────────────────────────────────────────────────
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
            sig=data
            entry_fee = pos_size * FEE_RATE
            cash -= pos_size + entry_fee
            open_count+=1
            if open_count>max_open: max_open=open_count
            trade_id=counter; counter+=1
            open_positions[trade_id]=pos_size
            exit_ts,cash_ret,label = exit_fn(sig,pos_size)
            heapq.heappush(queue,(exit_ts.timestamp(),0,counter,"exit",{
                "trade_id":trade_id,"symbol":sig["symbol"],
                "entry_time":sig["entry_time"],"entry":sig["entry"],
                "stop":sig["stop"],"tp1":sig["tp1"],"tp2":sig["tp2"],
                "cash_ret":cash_ret,"label":label,"pos_size":pos_size,
                "entry_fee":entry_fee,
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
                "size":pos_size,"entry_fee":round(entry_fee,2),
                "cash_after":round(cash,2),"open":open_count,
            })
            equity_pts.append((ts,cash+sum(open_positions.values())))
        elif etype=="exit":
            d=data; cash+=d["cash_ret"]; open_count-=1
            open_positions.pop(d["trade_id"],None)
            net_pnl=d["cash_ret"]-(d["pos_size"]+d["entry_fee"])
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
    return trade_log, equity_pts, cash, max_open


# ─── İSTATİSTİK ────────────────────────────────────────────────────────────────────────────
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
    stop_early = stop_mid = stop_late = 0
    for s in stops:
        try:
            entry_t = pd.Timestamp(s["entry_time"])
            exit_t  = pd.Timestamp(s["time"])
            hours   = (exit_t - entry_t).total_seconds() / 3600
            if hours < 24:   stop_early += 1
            elif hours < 48: stop_mid   += 1
            else:            stop_late  += 1
        except Exception:
            pass
    time_stops = len([e for e in exits if e["label"]=="time_stop"])
    return {
        "trades":len([t for t in trade_log if t["type"]=="ENTRY"]),
        "wins":len(wins),"losses":len(stops),"time_stops":time_stops,
        "expires":len(expires),"wr":round(wr,1),
        "final":round(final,2),"ret":round(ret,2),"max_dd":round(max_dd,2),
        "avg_win":round(avg_win,2),"avg_loss":round(avg_loss,2),
        "stop_early":stop_early,"stop_mid":stop_mid,"stop_late":stop_late,
    }


# ─── SENARYO TANIMI ────────────────────────────────────────────────────────────────────────────
SCENARIO_META = [
    ("smc_full_trail", "eski_v2", "full_trail", "SMC V2", "TP1 TRAIL AKTİV + TAM ÇIKIŞ 2.5%"),
    ("smc_half",       "eski_v2", "half",       "SMC V2", "TP1 50% + TRAIL 2.5%"),
    ("smc_tp2",        "eski_v2", "tp2",        "SMC V2", "TP2 ONLY"),
]

PALETTE = ["#e63946","#457b9d","#2a9d8f","#e9c46a","#264653"]


# ─── HTML ÇIKTI ────────────────────────────────────────────────────────────────────────────
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
<meta charset="UTF-8"><title>SMC Backtest Engine</title>
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
<h1>📊 SMC Backtest Engine (2022 → bugün)</h1>
<div class="meta">{run_date} | {n_coins} coin | 1H Binance | ${INITIAL_CAP:,.0f} başlangıç | Maks {MAX_POSITIONS} pozisyon | ${MAX_POS_SIZE:,.0f} maks işlem | Komisyon %{FEE_RATE*100:.1f} giriş+çıkış | Slippage %{SLIPPAGE*100:.2f}</div>
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


# ─── RAPOR ────────────────────────────────────────────────────────────────────────────
def print_report(results, n_coins, active_scenarios=None):
    if active_scenarios is None: active_scenarios = SCENARIO_META
    W=120
    print("\n"+"═"*W)
    print(f"  SMC BACKTEST | {n_coins} coin | 2022→bugün | ${INITIAL_CAP:,.0f} başlangıç | Maks ${MAX_POS_SIZE:,.0f}/işlem | Komisyon %{FEE_RATE*100:.1f} | Slippage %{SLIPPAGE*100:.2f}")
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
        ts_cnt=st.get("time_stops",0); max_open=st.get("max_open",0)
        n_sigs=r.get("n_sigs",0)
        label=f"{sys_name} — {mode}"
        print(f"  {label:<45} {n_sigs:7d} {trades:6d} {wins:7d} {losses:6d} {expires:7d} {wr:6.1f}% {avg_win:+8.2f}% {avg_loss:+8.2f}% {max_dd:7.1f}% {ret:+9.1f}% ${final:12,.2f}")
        print(f"  {'':45}  Max eş zamanlı: {max_open} | Time stop: {ts_cnt} | Stop zamanl.: <24H: {se}  24-48H: {sm}  >48H: {sl_}" if losses else "")
    print("═"*W+"\n")


# ─── MAIN ────────────────────────────────────────────────────────────────────────────
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
    print(f"\n{len(symbols)} coin | ${INITIAL_CAP:,.0f} sermaye | maks {MAX_POSITIONS} pozisyon | ${MAX_POS_SIZE:,.0f} maks işlem")
    print(f"Komisyon: %{FEE_RATE*100:.1f} giriş + %{FEE_RATE*100:.1f} çıkış | Slippage: %{SLIPPAGE*100:.2f}")
    print(f"Sinyaller toplanıyor ({START_DATE.date()} → bugün)...\n")
    sigs = collect_smc_signals(symbols, btc_filters, fetch=do_fetch)
    for sys_name, sig_list in sigs.items():
        print(f"  {sys_name}: {len(sig_list)} sinyal")
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
    now_str = _dt.datetime.now().strftime("%Y%m%d_%H%M")
    html = generate_html(results, len(symbols), active_scenarios)
    out_html = f"backtest_results_{now_str}.html"
    with open(out_html,"w",encoding="utf-8") as f: f.write(html)
    print(f"✓ {out_html} kaydedildi")
    summary = {k:{"n_sigs":v["n_sigs"],"stats":v["stats"]} for k,v in results.items()}
    out_json = f"backtest_results_{now_str}.json"
    with open(out_json,"w",encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
    print(f"✓ {out_json} kaydedildi\n")


if __name__ == "__main__":
    main()
