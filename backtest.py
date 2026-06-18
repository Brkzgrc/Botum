#!/usr/bin/env python3
"""
Full Backtest — 20 Senaryo (4 SMC × 3 çıkış + 4 Bot × 2 çıkış)
SMC.py + bot.py metodolojisi, portfolio_tracker.py çıkış mantığı
2020-01-01'den bugüne, 1H OHLCV, Binance

Kullanım:
  python backtest.py                   # Cache varsa kullan, yoksa indir
  python backtest.py --no-fetch        # Sadece cache kullan
  python backtest.py --coins BTC ETH   # Belirli coinler
  python backtest.py --n 50            # İlk N coin (cache yoksa)
"""

import argparse, json, os, pickle, time
from datetime import datetime, timezone
import ccxt, numpy as np, pandas as pd

# ═══════════════════════════════════════════════════════════════════════
# AYARLAR
# ═══════════════════════════════════════════════════════════════════════
START_TS        = int(datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
DATA_DIR        = "backtest_data"

CHOCH_SWING     = 5
SWING_LENGTH    = 50
COOLDOWN_H      = 24
EXPIRE_H        = 168
MIN_VOL_24H     = 5_000_000
BTC_CRASH_PCT   = 3.0
PHASE1_DEPTH    = 85.0
PHASE1_RSI      = 30.0
ESKI_DISC_DEPTH = 5.0     # discount_bottom'un en fazla %5 üzeri
SMC_TRAIL_PCT   = 2.5     # SMC half_open trailing (portfolio_tracker)
BOT_TRAIL_PCT   = 3.0     # Bot trailing (portfolio_tracker)
INITIAL_CAP     = 5_000.0
MAX_POS         = 10

# Bot sistem parametreleri (bot.py'den birebir)
PANIK_MIN = -15.0; PANIK_MAX = -7.0
PANIK_VOL_MIN = 1.5; PANIK_VOL_MAX = 3.0
PANIK_EXPIRE_H = 24; PANIK_COOL_H = 4

T72_EXPIRE_H = 72;  T72_COOL_H = 4
T168_EXPIRE_H = 168; T168_COOL_H = 4
ROCKET_EXPIRE_H = 48; ROCKET_COOL_H = 12
ROCKET_CHG_MIN = 10.0; ROCKET_VOL_MIN = 1.2; ROCKET_ADX_MIN = 25.0

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


# ═══════════════════════════════════════════════════════════════════════
# VERİ ÇEKME
# ═══════════════════════════════════════════════════════════════════════
def get_top_coins(n=50):
    import urllib.request
    url = ("https://api.coingecko.com/api/v3/coins/markets"
           "?vs_currency=usd&order=market_cap_desc&per_page=250&page=1&sparkline=false")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = __import__("json").loads(r.read())
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
    print(f"CoinGecko top {n}: {len(result)} coin")
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
    return os.path.join(DATA_DIR, symbol.replace("/","_") + ".pkl")


def load_or_fetch(symbol, force=False):
    p = cache_path(symbol)
    if not force and os.path.exists(p):
        with open(p,"rb") as f: return pickle.load(f)
    df = fetch_ohlcv_full(symbol)
    if df is not None:
        with open(p,"wb") as f: pickle.dump(df, f)
    return df


# ═══════════════════════════════════════════════════════════════════════
# CHoCH TESPİTİ (artımlı, backtest.py orijinali)
# ═══════════════════════════════════════════════════════════════════════
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
        ci, cp = c[i], c[i-1]; bt=None; bd=None; cl=None
        if sh is not None and not shx and ci > sh and cp <= sh:
            bt = "CHoCH" if trend == -1 else "BOS"; bd = "BULLISH"; cl = sh
            shx = True; trend = 1
        if sl is not None and not slx and ci < sl and cp >= sl:
            bt = "CHoCH" if trend == 1 else "BOS"; bd = "BEARISH"; cl = sl
            slx = True; trend = -1
        bts[i]=bt; bds[i]=bd; cls_[i]=cl; swls[i]=sl
    return bts, bds, cls_, swls


# ═══════════════════════════════════════════════════════════════════════
# BTC FİLTRELERİ (backtest.py orijinali)
# ═══════════════════════════════════════════════════════════════════════
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
    downtrend_active=np.zeros(n4,dtype=bool)
    for i in range(CHOCH_SWING+1, n4):
        if legs4[i]!=legs4[i-1]:
            if legs4[i]==1: prev_sl4=sl4; sl4=l4[i-CHOCH_SWING]; slx4=False
            else: sh4=h4[i-CHOCH_SWING]; shx4=False
        ci,cp=c4[i],c4[i-1]
        if sh4 is not None and not shx4 and ci>sh4 and cp<=sh4: shx4=True; trend4=1
        if sl4 is not None and not slx4 and ci<sl4 and cp>=sl4: slx4=True; trend4=-1
        if trend4==-1 and (prev_sl4 is None or (sl4 is not None and sl4<=prev_sl4)):
            downtrend_active[i]=True
    df4["crash_ok"]=crash_ok; df4["downtrend_active"]=downtrend_active
    idx=btc_1h.index
    crash_s=df4["crash_ok"].reindex(idx,method="ffill").fillna(True).astype(bool)
    downtrend_s=df4["downtrend_active"].reindex(idx,method="ffill").fillna(False).astype(bool)
    return pd.DataFrame({"crash_ok":crash_s,"downtrend_ok":~downtrend_s},index=idx)


# ═══════════════════════════════════════════════════════════════════════
# ARTIMLI LUXALGO SMC (discount zone per-bar)
# ═══════════════════════════════════════════════════════════════════════
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
        prev_leg=legs[i-1] if i>0 else 0; curr_leg=legs[i]
        if curr_leg!=prev_leg:
            if curr_leg==1: t_bot=l[i-swing_length]
            elif curr_leg==0: t_top=h[i-swing_length]
        if t_top is not None and h[i]>t_top: t_top=h[i]
        if t_bot is not None and l[i]<t_bot: t_bot=l[i]
        if t_top is not None and t_bot is not None and t_top!=t_bot:
            disc_top[i]=0.55*t_top+0.45*t_bot
            disc_bot[i]=t_bot
            depth_arr[i]=(t_top-c[i])/(t_top-t_bot)*100
    return disc_top, disc_bot, depth_arr


# ═══════════════════════════════════════════════════════════════════════
# EK İNDİKATÖRLER (bot sistemleri için)
# ═══════════════════════════════════════════════════════════════════════
def compute_extra_indicators(df):
    df=df.copy(); c=df["close"]
    prev_c=c.shift(1); h=df["high"]; l=df["low"]
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
    is_at_high=c>=roll_max*(1-1e-6)
    last_high_pos=bar_idx.where(is_at_high).ffill().fillna(0)
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


# ═══════════════════════════════════════════════════════════════════════
# ÇIKIŞ SİMÜLASYONLARI — bars_held de döndürür
# ═══════════════════════════════════════════════════════════════════════
def sim_half(bh, bl, bc, entry, stop, tp1, tp2, expire_h):
    """SMC actual ≈ ½TP1+½TP2 (portfolio_tracker half_open mantığı)"""
    n=min(len(bh),expire_h)
    if n==0: return 0.0,"expired",0
    tp1_p=(tp1-entry)/entry*100; tp2_p=(tp2-entry)/entry*100; stop_p=(stop-entry)/entry*100
    peak=entry; tp1_hit=False
    for i in range(n):
        if not tp1_hit:
            if bl[i]<=stop: return stop_p,"loss",i+1
            if bh[i]>=tp1: tp1_hit=True; peak=max(entry,bh[i])
        else:
            if bh[i]>peak: peak=bh[i]
            trail=peak*(1-SMC_TRAIL_PCT/100)
            if bh[i]>=tp2: return (tp1_p+tp2_p)/2,"win",i+1
            if bl[i]<=trail: return (tp1_p+(trail-entry)/entry*100)/2,"trail",i+1
    last_p=(bc[n-1]-entry)/entry*100 if n>0 else 0.0
    if tp1_hit: return (tp1_p+last_p)/2,"expired",n
    return last_p,"expired",n


def sim_tp1(bh, bl, bc, entry, stop, tp1, expire_h):
    """Tam TP1 çıkışı"""
    n=min(len(bh),expire_h)
    if n==0: return 0.0,"expired",0
    tp1_p=(tp1-entry)/entry*100; stop_p=(stop-entry)/entry*100
    for i in range(n):
        if bl[i]<=stop: return stop_p,"loss",i+1
        if bh[i]>=tp1: return tp1_p,"win",i+1
    return (bc[n-1]-entry)/entry*100 if n>0 else 0.0,"expired",n


def sim_tp2(bh, bl, bc, entry, stop, tp2, expire_h):
    """Tam TP2 çıkışı"""
    n=min(len(bh),expire_h)
    if n==0: return 0.0,"expired",0
    tp2_p=(tp2-entry)/entry*100; stop_p=(stop-entry)/entry*100
    for i in range(n):
        if bl[i]<=stop: return stop_p,"loss",i+1
        if bh[i]>=tp2: return tp2_p,"win",i+1
    return (bc[n-1]-entry)/entry*100 if n>0 else 0.0,"expired",n


def sim_bot_actual(bh, bl, bc, entry, tp2, expire_h):
    """Bot actual: trailing stop %3'ten itibaren, TP2'de tam çıkış"""
    n=min(len(bh),expire_h)
    if n==0: return 0.0,"expired",0
    tp2_p=(tp2-entry)/entry*100; peak=entry
    for i in range(n):
        if bh[i]>peak: peak=bh[i]
        trail=peak*(1-BOT_TRAIL_PCT/100)
        if bh[i]>=tp2: return tp2_p,"win",i+1
        if bl[i]<=trail:
            ret=(trail-entry)/entry*100
            return ret,"win" if ret>0 else "loss",i+1
    return (bc[n-1]-entry)/entry*100 if n>0 else 0.0,"expired",n


def sim_bot_tp1(bh, bl, bc, entry, stop, tp1, expire_h):
    """Bot tp1_only: sinyal stopu veya TP1"""
    return sim_tp1(bh, bl, bc, entry, stop, tp1, expire_h)


# ═══════════════════════════════════════════════════════════════════════
# PORTFÖLİO SİMÜLASYONU
# ═══════════════════════════════════════════════════════════════════════
def simulate_portfolio(signals, initial=INITIAL_CAP, max_pos=MAX_POS):
    """
    signals: [(entry_ts_h, exit_ts_h, pnl_pct)]
    Döner: (final_capital, equity_curve)  equity_curve = [(ts_h, capital)]
    """
    if not signals: return initial, [(0, initial)]
    signals=sorted(signals,key=lambda x: x[0])
    cash=initial; positions=[]; equity=[(signals[0][0],initial)]

    def close_expired(before_h):
        nonlocal cash
        remain=[]
        for pos in positions:
            if pos[0]<=before_h:
                cash+=pos[1]*(1+pos[2]/100)
                equity.append((pos[0],round(cash,2)))
            else:
                remain.append(pos)
        positions[:]=remain

    for entry_h,exit_h,pnl in signals:
        close_expired(entry_h)
        if len(positions)<max_pos and cash>0:
            cost=cash/max_pos
            cash-=cost
            positions.append((exit_h,cost,pnl))

    for pos in sorted(positions,key=lambda x: x[0]):
        cash+=pos[1]*(1+pos[2]/100)
        equity.append((pos[0],round(cash,2)))

    equity.sort(key=lambda x: x[0])
    return round(cash,2), equity


# ═══════════════════════════════════════════════════════════════════════
# SONUÇ YAPISI (istatistik sayacı)
# ═══════════════════════════════════════════════════════════════════════
def empty_stats():
    return {"wins":0,"losses":0,"partial":0,"expired":0,"total":0,"pnl":0.0}

def record(stats,pnl,outcome):
    stats["total"]+=1; stats["pnl"]+=pnl
    if outcome=="win":           stats["wins"]+=1
    elif outcome in ("trail","win_partial"): stats["partial"]+=1
    elif outcome=="loss":        stats["losses"]+=1
    else:                        stats["expired"]+=1

def finalize(stats):
    t=stats["total"]
    if t==0: stats["wr"]=0.0; stats["avg_pnl"]=0.0; return stats
    wins=stats["wins"]+stats["partial"]; dec=wins+stats["losses"]
    stats["wr"]=round(wins/dec*100,1) if dec>0 else 0.0
    stats["avg_pnl"]=round(stats["pnl"]/t,4)
    stats["pnl"]=round(stats["pnl"],2)
    return stats


# ═══════════════════════════════════════════════════════════════════════
# ANA BACKTEST
# ═══════════════════════════════════════════════════════════════════════
def run(symbols, btc_df):
    print("BTC 4H filtreleri hesaplanıyor...", flush=True)
    btc_f = compute_btc_filters(btc_df)

    # İstatistik sayaçları (sinyal kalitesi için)
    S = {
        "eski_choch_half": empty_stats(), "eski_choch_tp1": empty_stats(), "eski_choch_tp2": empty_stats(),
        "eski_v2_half":    empty_stats(), "eski_v2_tp1":    empty_stats(), "eski_v2_tp2":    empty_stats(),
        "eski_disc_half":  empty_stats(), "eski_disc_tp1":  empty_stats(), "eski_disc_tp2":  empty_stats(),
        "smc_orig_half":   empty_stats(), "smc_orig_tp1":   empty_stats(), "smc_orig_tp2":   empty_stats(),
        "panik_actual":    empty_stats(), "panik_tp1":      empty_stats(),
        "t72_actual":      empty_stats(), "t72_tp1":        empty_stats(),
        "t168_actual":     empty_stats(), "t168_tp1":       empty_stats(),
        "rocket_actual":   empty_stats(), "rocket_tp1":     empty_stats(),
    }

    # Portföy simülasyonu için ham sinyal listesi
    P = {k: [] for k in S}  # [(entry_ts_h, exit_ts_h, pnl_pct)]

    total_coins = len(symbols)

    for sym_idx, symbol in enumerate(symbols, 1):
        if symbol == "BTC/USDT": continue
        print(f"[{sym_idx}/{total_coins}] {symbol}", flush=True)

        df_raw = load_or_fetch(symbol)
        if df_raw is None or len(df_raw) < 300:
            print("  → Yetersiz veri"); continue

        # Temel hacim indikatörleri
        vol = df_raw["volume"]
        df_raw = df_raw.copy()
        df_raw["vol_24h_usd"]  = (df_raw["close"] * vol).rolling(24).sum()
        df_raw["vol_ratio_20"] = vol / vol.rolling(20).mean().shift(1)

        df = df_raw.dropna(subset=["vol_24h_usd"]).copy()
        if len(df) < 300:
            print("  → Yetersiz veri (indikatör sonrası)"); continue

        # Extra indikatörler (bot sistemleri için)
        try:
            df = compute_extra_indicators(df)
        except Exception as e:
            print(f"  → İndikatör hatası: {e}"); continue

        # Luxalgo discount zone
        disc_top, disc_bot, depth_arr = compute_luxalgo_incremental(df)

        # ADX (Rocket için)
        adx_arr, di_p_arr, di_m_arr = compute_adx_series(df)

        # BTC filtreleri
        btc_a = btc_f.reindex(df.index, method="ffill").fillna({"crash_ok":True,"downtrend_ok":True})

        # 4H EMA21 (SMC Original CHoCH için)
        try:
            df4 = df.resample("4h",label="right",closed="right").agg(
                {"open":"first","high":"max","low":"min","close":"last","volume":"sum"}).dropna()
            ema21_4h = df4["close"].ewm(span=21,adjust=False).mean()
            above_4h_ema = (df4["close"]>ema21_4h).reindex(df.index,method="ffill").fillna(True).astype(bool)
        except Exception:
            above_4h_ema = pd.Series(True, index=df.index)

        # CHoCH tespiti
        bts, bds, cls_, swls = run_choch_incremental(df)

        h_arr  = df["high"].values
        l_arr  = df["low"].values
        c_arr  = df["close"].values
        o_arr  = df["open"].values
        vol24  = df["vol_24h_usd"].values
        volr20 = df["vol_ratio_20"].values
        atr_v  = df["atr"].values
        rsi_v  = df["rsi"].values
        n = len(df)
        ts_arr = np.array([t.timestamp()/3600 for t in df.index])

        # Cooldown takibi
        last = {k: 0.0 for k in ["base","v2","disc","orig","panik","t72","t168","rocket"]}
        disc_active = False  # SMC Original Phase 1 → Phase 2

        for i in range(250, n-1):
            ts_h = ts_arr[i]
            price = c_arr[i]
            if np.isnan(price) or price <= 0: continue

            bf_crash = bool(btc_a["crash_ok"].iloc[i])
            bf_down  = bool(btc_a["downtrend_ok"].iloc[i])

            fh = h_arr[i+1:]; fl = l_arr[i+1:]; fc = c_arr[i+1:]
            if len(fh) == 0: continue

            # Hacim filtresi
            if np.isnan(vol24[i]) or vol24[i] < MIN_VOL_24H: continue

            vr = volr20[i]
            is_choch = (bts[i]=="CHoCH" and bds[i]=="BULLISH")
            choch_lvl = cls_[i]; sw_low = swls[i]
            atr = atr_v[i] if not np.isnan(atr_v[i]) else price*0.05

            # ── 1 & 2. Eski CHoCH + V2 ───────────────────────────────────
            if is_choch and bf_crash and bf_down:
                entry = choch_lvl if choch_lvl else price
                stop  = sw_low*0.995 if sw_low else entry*0.95
                if stop>=entry: stop=entry*0.95
                risk=entry-stop; risk=max(risk,entry*0.01)
                tp1=entry+risk; tp2=entry+risk*2

                # Eski CHoCH
                if ts_h-last["base"]>=COOLDOWN_H:
                    ph,oh,bh=sim_half(fh,fl,fc,entry,stop,tp1,tp2,EXPIRE_H)
                    p1,o1,b1=sim_tp1(fh,fl,fc,entry,stop,tp1,EXPIRE_H)
                    p2,o2,b2=sim_tp2(fh,fl,fc,entry,stop,tp2,EXPIRE_H)
                    record(S["eski_choch_half"],ph,oh); P["eski_choch_half"].append((ts_h,ts_h+bh,ph))
                    record(S["eski_choch_tp1"], p1,o1); P["eski_choch_tp1"].append((ts_h,ts_h+b1,p1))
                    record(S["eski_choch_tp2"], p2,o2); P["eski_choch_tp2"].append((ts_h,ts_h+b2,p2))
                    last["base"]=ts_h

                # Eski CHoCH V2 (vol≥1.5x)
                if not np.isnan(vr) and vr>=1.5 and ts_h-last["v2"]>=COOLDOWN_H:
                    ph,oh,bh=sim_half(fh,fl,fc,entry,stop,tp1,tp2,EXPIRE_H)
                    p1,o1,b1=sim_tp1(fh,fl,fc,entry,stop,tp1,EXPIRE_H)
                    p2,o2,b2=sim_tp2(fh,fl,fc,entry,stop,tp2,EXPIRE_H)
                    record(S["eski_v2_half"],ph,oh); P["eski_v2_half"].append((ts_h,ts_h+bh,ph))
                    record(S["eski_v2_tp1"], p1,o1); P["eski_v2_tp1"].append((ts_h,ts_h+b1,p1))
                    record(S["eski_v2_tp2"], p2,o2); P["eski_v2_tp2"].append((ts_h,ts_h+b2,p2))
                    last["v2"]=ts_h

            # ── 3. Eski Discount ─────────────────────────────────────────
            if (not np.isnan(disc_bot[i]) and
                    price<=disc_bot[i]*(1+ESKI_DISC_DEPTH/100) and
                    ts_h-last["disc"]>=COOLDOWN_H):
                entry=price; stop=entry-atr*4; tp1=entry+atr*4; tp2=entry+atr*6
                if stop<entry and tp1>entry:
                    ph,oh,bh=sim_half(fh,fl,fc,entry,stop,tp1,tp2,EXPIRE_H)
                    p1,o1,b1=sim_tp1(fh,fl,fc,entry,stop,tp1,EXPIRE_H)
                    p2,o2,b2=sim_tp2(fh,fl,fc,entry,stop,tp2,EXPIRE_H)
                    record(S["eski_disc_half"],ph,oh); P["eski_disc_half"].append((ts_h,ts_h+bh,ph))
                    record(S["eski_disc_tp1"], p1,o1); P["eski_disc_tp1"].append((ts_h,ts_h+b1,p1))
                    record(S["eski_disc_tp2"], p2,o2); P["eski_disc_tp2"].append((ts_h,ts_h+b2,p2))
                    last["disc"]=ts_h

            # ── 4. SMC Original CHoCH ────────────────────────────────────
            # Phase 1: deep discount → disc_active=True
            if (not np.isnan(depth_arr[i]) and not np.isnan(disc_top[i]) and
                    price<=disc_top[i] and depth_arr[i]>=PHASE1_DEPTH and
                    not np.isnan(rsi_v[i]) and rsi_v[i]<=PHASE1_RSI and bf_crash):
                disc_active=True

            # Phase 2: BULLISH CHoCH while disc_active
            if (disc_active and is_choch and bf_crash and
                    bool(above_4h_ema.iloc[i]) and
                    ts_h-last["orig"]>=COOLDOWN_H):
                entry=choch_lvl if choch_lvl else price
                stop=sw_low*0.995 if sw_low else entry*0.95
                if stop>=entry: stop=entry*0.95
                risk=entry-stop; risk=max(risk,entry*0.01)
                tp1=entry+risk; tp2=entry+risk*2
                ph,oh,bh=sim_half(fh,fl,fc,entry,stop,tp1,tp2,EXPIRE_H)
                p1,o1,b1=sim_tp1(fh,fl,fc,entry,stop,tp1,EXPIRE_H)
                p2,o2,b2=sim_tp2(fh,fl,fc,entry,stop,tp2,EXPIRE_H)
                record(S["smc_orig_half"],ph,oh); P["smc_orig_half"].append((ts_h,ts_h+bh,ph))
                record(S["smc_orig_tp1"], p1,o1); P["smc_orig_tp1"].append((ts_h,ts_h+b1,p1))
                record(S["smc_orig_tp2"], p2,o2); P["smc_orig_tp2"].append((ts_h,ts_h+b2,p2))
                last["orig"]=ts_h; disc_active=False

            # ── 5. PANİK PUMP ────────────────────────────────────────────
            if (not np.isnan(df["close_prev"].values[i]) and df["close_prev"].values[i]>0
                    and ts_h-last["panik"]>=PANIK_COOL_H):
                ret1=(price/df["close_prev"].values[i]-1)*100
                if (PANIK_MIN<=ret1<=PANIK_MAX and not np.isnan(vr) and
                        PANIK_VOL_MIN<=vr<=PANIK_VOL_MAX and price>=o_arr[i]):
                    c5=c_arr[i-5] if i>=5 else price
                    if c5<=0 or (c5-price)/c5*100<4.0:
                        entry=price; stop=entry*0.97; tp1=entry*1.05; tp2=entry*1.10
                        pa,oa,ba=sim_bot_actual(fh,fl,fc,entry,tp2,PANIK_EXPIRE_H)
                        p1,o1,b1=sim_bot_tp1(fh,fl,fc,entry,stop,tp1,PANIK_EXPIRE_H)
                        record(S["panik_actual"],pa,oa); P["panik_actual"].append((ts_h,ts_h+ba,pa))
                        record(S["panik_tp1"],   p1,o1); P["panik_tp1"].append((ts_h,ts_h+b1,p1))
                        last["panik"]=ts_h

            # ── 6. T72 ───────────────────────────────────────────────────
            if ts_h-last["t72"]>=T72_COOL_H:
                m5=df["mom5_pct"].values[i]; de=df["dist_ema21"].values[i]
                dd=df["coin_drawdown"].values[i]; ms=df["ma200_slope"].values[i]
                if not any(np.isnan(x) for x in [m5,de,dd,ms]):
                    if m5>=2.740 and de<=-2.737 and dd>=-26.796 and ms>=1.028:
                        entry=price; stop=entry*0.95; tp1=entry*1.10; tp2=entry*1.15
                        pa,oa,ba=sim_bot_actual(fh,fl,fc,entry,tp2,T72_EXPIRE_H)
                        p1,o1,b1=sim_bot_tp1(fh,fl,fc,entry,stop,tp1,T72_EXPIRE_H)
                        record(S["t72_actual"],pa,oa); P["t72_actual"].append((ts_h,ts_h+ba,pa))
                        record(S["t72_tp1"],   p1,o1); P["t72_tp1"].append((ts_h,ts_h+b1,p1))
                        last["t72"]=ts_h

            # ── 7. T168 ──────────────────────────────────────────────────
            if ts_h-last["t168"]>=T168_COOL_H:
                dm2=df["dist_ma200"].values[i]; dm5=df["dist_ma50"].values[i]
                m10=df["mom10_pct"].values[i]; bsh=df["bars_since_high"].values[i]
                if not any(np.isnan(x) for x in [dm2,dm5,m10,bsh]):
                    if dm2>=5.657 and dm5<=-5.045 and m10>=3.941 and bsh<=677:
                        entry=price; stop=entry*0.92; tp1=entry*1.25; tp2=entry*1.25
                        pa,oa,ba=sim_bot_actual(fh,fl,fc,entry,tp2,T168_EXPIRE_H)
                        p1,o1,b1=sim_bot_tp1(fh,fl,fc,entry,stop,tp1,T168_EXPIRE_H)
                        record(S["t168_actual"],pa,oa); P["t168_actual"].append((ts_h,ts_h+ba,pa))
                        record(S["t168_tp1"],   p1,o1); P["t168_tp1"].append((ts_h,ts_h+b1,p1))
                        last["t168"]=ts_h

            # ── 8. ROCKET ────────────────────────────────────────────────
            if (i>=25 and ts_h-last["rocket"]>=ROCKET_COOL_H and
                    not np.isnan(adx_arr[i]) and adx_arr[i]>=ROCKET_ADX_MIN and
                    di_p_arr[i]>di_m_arr[i] and bf_down):
                c24=c_arr[i-24]; chg=(price/c24-1)*100 if c24>0 else 0
                if chg>=ROCKET_CHG_MIN:
                    vol_avg=df["volume"].values[max(0,i-20):i].mean()
                    vol_now=df["volume"].values[i]
                    if vol_avg>0 and vol_now>=vol_avg*ROCKET_VOL_MIN:
                        entry=price; stop=entry*0.95; tp1=entry*1.08; tp2=entry*1.15
                        pa,oa,ba=sim_bot_actual(fh,fl,fc,entry,tp2,ROCKET_EXPIRE_H)
                        p1,o1,b1=sim_bot_tp1(fh,fl,fc,entry,stop,tp1,ROCKET_EXPIRE_H)
                        record(S["rocket_actual"],pa,oa); P["rocket_actual"].append((ts_h,ts_h+ba,pa))
                        record(S["rocket_tp1"],   p1,o1); P["rocket_tp1"].append((ts_h,ts_h+b1,p1))
                        last["rocket"]=ts_h

    for k in S: S[k]=finalize(S[k])

    # Portföy simülasyonu
    PORT = {}
    for k in P:
        cap, eq = simulate_portfolio(P[k])
        PORT[k] = {"final": cap, "equity": eq, "n_sigs": len(P[k])}

    return S, PORT


# ═══════════════════════════════════════════════════════════════════════
# HTML ÇIKTI
# ═══════════════════════════════════════════════════════════════════════
SCENARIO_META = [
    ("eski_choch_half", "Eski CHoCH",         "actual (SMC trail)",  "SMC"),
    ("eski_choch_tp1",  "Eski CHoCH",         "TP1 Only",            "SMC"),
    ("eski_choch_tp2",  "Eski CHoCH",         "TP2 Only",            "SMC"),
    ("eski_v2_half",    "Eski CHoCH V2 ≥1.5x","actual (SMC trail)",  "SMC"),
    ("eski_v2_tp1",     "Eski CHoCH V2 ≥1.5x","TP1 Only",           "SMC"),
    ("eski_v2_tp2",     "Eski CHoCH V2 ≥1.5x","TP2 Only",           "SMC"),
    ("eski_disc_half",  "Eski Discount",       "actual (SMC trail)",  "SMC"),
    ("eski_disc_tp1",   "Eski Discount",       "TP1 Only",            "SMC"),
    ("eski_disc_tp2",   "Eski Discount",       "TP2 Only",            "SMC"),
    ("smc_orig_half",   "SMC Original CHoCH",  "actual (SMC trail)",  "SMC"),
    ("smc_orig_tp1",    "SMC Original CHoCH",  "TP1 Only",            "SMC"),
    ("smc_orig_tp2",    "SMC Original CHoCH",  "TP2 Only",            "SMC"),
    ("panik_actual",    "PANİK PUMP",          "actual (Bot trail)",  "BOT"),
    ("panik_tp1",       "PANİK PUMP",          "TP1 Only",            "BOT"),
    ("t72_actual",      "T72",                 "actual (Bot trail)",  "BOT"),
    ("t72_tp1",         "T72",                 "TP1 Only",            "BOT"),
    ("t168_actual",     "T168",                "actual (Bot trail)",  "BOT"),
    ("t168_tp1",        "T168",                "TP1 Only",            "BOT"),
    ("rocket_actual",   "ROCKET",              "actual (Bot trail)",  "BOT"),
    ("rocket_tp1",      "ROCKET",              "TP1 Only",            "BOT"),
]

PALETTE = ["#e63946","#457b9d","#2a9d8f","#e9c46a","#f4a261","#264653","#a8dadc",
           "#6d6875","#b5838d","#e07a5f","#3d405b","#81b29a","#f2cc8f","#118ab2",
           "#06d6a0","#ef476f","#ffd166","#8338ec","#3a86ff","#fb5607"]


def generate_html(S, PORT, n_coins):
    rows = ""
    datasets = []
    for idx,(key,sys_name,mode,grp) in enumerate(SCENARIO_META):
        st=S.get(key,{}); pt=PORT.get(key,{})
        t=st.get("total",0); wr=st.get("wr",0)
        pnl=st.get("pnl",0); avg=st.get("avg_pnl",0)
        final=pt.get("final",INITIAL_CAP)
        ret=(final-INITIAL_CAP)/INITIAL_CAP*100
        color_ret="#00c853" if ret>=0 else "#d32f2f"
        badge="smc" if grp=="SMC" else "bot"
        rows+=(f'<tr><td><span class="badge badge-{badge}">{grp}</span> {sys_name} — {mode}</td>'
               f'<td>{t}</td><td>{wr:.0f}%</td>'
               f'<td style="color:{color_ret}">{avg:+.3f}%</td>'
               f'<td style="color:{color_ret}">{ret:+.1f}%</td>'
               f'<td style="color:{color_ret}">${final:,.0f}</td>'
               f'<td><input type="checkbox" class="tog" data-idx="{idx}" checked></td></tr>')
        eq=pt.get("equity",[])
        if eq:
            pts=[{"x":datetime.utcfromtimestamp(ts*3600).strftime("%Y-%m-%d"),"y":v} for ts,v in eq]
            color=PALETTE[idx%len(PALETTE)]
            label=f"{sys_name} — {mode}"
            datasets.append(f'{{"label":{json.dumps(label)},"data":{json.dumps(pts)},'
                            f'"borderColor":"{color}","backgroundColor":"{color}20",'
                            f'"borderWidth":1.5,"pointRadius":0,"fill":false,"tension":0.1}}')

    ds_js="["+",".join(datasets)+"]"
    run_date=datetime.now().strftime("%Y-%m-%d %H:%M")
    html=f"""<!DOCTYPE html><html lang="tr"><head>
<meta charset="UTF-8"><title>Full Backtest — 20 Senaryo</title>
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
<div class="meta">{run_date} | {n_coins} coin | 1H Binance | $5.000 başlangıç | Maks {MAX_POS} pozisyon | Dinamik boyutlama</div>
<div class="card"><table>
<thead><tr><th>Senaryo</th><th>Sinyal</th><th>WR%</th><th>Ort P&L%</th><th>Portföy Getiri</th><th>Son Sermaye</th><th>Graf.</th></tr></thead>
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
    return html


# ═══════════════════════════════════════════════════════════════════════
# RAPOR
# ═══════════════════════════════════════════════════════════════════════
def report(S, PORT, n_coins):
    W = 110
    print("\n" + "═"*W)
    print(f"  FULL BACKTEST — 20 Senaryo | {n_coins} coin | 1H Binance | $5.000 başlangıç")
    print("═"*W)
    print(f"  {'Senaryo':<42} {'Sinyal':>7} {'WR%':>6} {'Ort P&L':>9} {'Son Sermaye':>12} {'Getiri%':>8}")
    print("─"*W)
    prev_grp = ""
    for key,sys_name,mode,grp in SCENARIO_META:
        if grp!=prev_grp: print("─"*W); prev_grp=grp
        st=S.get(key,{}); pt=PORT.get(key,{})
        t=st.get("total",0); wr=st.get("wr",0); avg=st.get("avg_pnl",0)
        final=pt.get("final",INITIAL_CAP)
        ret=(final-INITIAL_CAP)/INITIAL_CAP*100
        label=f"{sys_name} — {mode}"
        print(f"  [{grp}] {label:<38} {t:7d} {wr:6.1f}% {avg:+9.4f}% ${final:11,.2f} {ret:+8.1f}%")
    print("═"*W+"\n")

    html = generate_html(S, PORT, n_coins)
    out_html = "backtest_results.html"
    with open(out_html,"w",encoding="utf-8") as f: f.write(html)
    print(f"✓ {out_html} kaydedildi")

    out_json = "backtest_results.json"
    combined = {"stats":S,"portfolio":{k:{"final":v["final"],"n_sigs":v["n_sigs"]} for k,v in PORT.items()}}
    with open(out_json,"w") as f: json.dump(combined,f,indent=2)
    print(f"✓ {out_json} kaydedildi")


# ═══════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-fetch", action="store_true")
    ap.add_argument("--coins",    nargs="*")
    ap.add_argument("--n",        type=int, default=50)
    args = ap.parse_args()

    os.makedirs(DATA_DIR, exist_ok=True)

    if args.coins:
        symbols = [s if "/" in s else s+"/USDT" for s in args.coins]
    elif args.no_fetch or (os.path.isdir(DATA_DIR) and
                           any(f.endswith(".pkl") for f in os.listdir(DATA_DIR))):
        pkls = sorted(f for f in os.listdir(DATA_DIR) if f.endswith(".pkl"))
        symbols = [f[:-4].replace("_","/",1) for f in pkls
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

    if not args.no_fetch and not (os.path.isdir(DATA_DIR) and
                                  any(f.endswith(".pkl") for f in os.listdir(DATA_DIR))):
        print(f"\n--- Veri İndirme ({len(symbols)} coin) ---")
        for i, sym in enumerate(symbols, 1):
            if sym == "BTC/USDT": continue
            p = cache_path(sym)
            if os.path.exists(p): print(f"  [{i}] {sym} — cache var"); continue
            print(f"  [{i}] {sym} indiriliyor...", end=" ", flush=True)
            df_tmp = load_or_fetch(sym)
            print(f"✓ ({len(df_tmp)} bar)" if df_tmp is not None else "HATA")
            time.sleep(0.1)

    print(f"\n--- Backtest Başlıyor ({len(symbols)} coin) ---")
    S, PORT = run(symbols, btc_raw)
    report(S, PORT, len(symbols))


if __name__ == "__main__":
    main()
