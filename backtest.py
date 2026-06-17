#!/usr/bin/env python3
"""
Kripto Sinyal Backtest — 15 Senaryo
2020-01-01'den bugüne, 1H OHLCV, Binance

Kullanım:
  pip install ccxt pandas numpy
  python backtest.py                  # Veriyi indir + backtest çalıştır
  python backtest.py --no-fetch       # Veri cache'de varsa tekrar indirme
  python backtest.py --n 20           # 20 coin ile test et
  python backtest.py --coins BTC/USDT ETH/USDT SOL/USDT
"""

import argparse, json, os, pickle, time
from datetime import datetime, timezone
import ccxt, numpy as np, pandas as pd

# ═══════════════════════════════════════════════════════════════════════
# AYARLAR  (bot.py / SMC.py ile birebir)
# ═══════════════════════════════════════════════════════════════════════
START_TS     = int(datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
DATA_DIR     = "backtest_data"
N_COINS      = 50
MIN_VOL      = 5_000_000

# PANİK PUMP
CRASH_MIN, CRASH_MAX = -15.0, -7.0
VOL_MIN,   VOL_MAX   =   1.5,  3.0
VOL_PERIOD           =  20

# Trailing stop
TRAILING_MIN = 5.0    # peak bu seviyeye ulaşınca trailing başlar
TRAILING_PCT = 0.03   # peak'in %3 altı

# Expire süresi (saat)
EXPIRE_H = {"capit": 24, "t72": 72, "t168": 168, "rocket": 48, "smc": 168, "eski": 168}

# Cooldown (saat) — aynı coin + aynı sistem için minimum bekleme
COOLDOWN_H = {"capit": 24, "t72": 120, "t168": 120, "rocket": 12, "smc": 24, "eski": 24}

# T72 eşikleri
T72_MOM5 = 2.740; T72_EMA = -2.737; T72_DD = -26.796; T72_SLP = 1.028

# T168 eşikleri
T168_MA200 = 5.657; T168_MA50 = -5.045; T168_MOM10 = 3.941; T168_DAYS = 677.0

# SMC
SWING_LEN = 50; CHOCH_SW = 5; P1_RSI = 30; P1_DEPTH = 85; ESKI_DEPTH = 5

# Hayali senaryo (portfolio_tracker.py ile aynı)
SIM_TP1 = 5.0; SIM_TP2 = 10.0; SIM_STOP = -2.5

IGNORED_COINS = {
    "UP/USDT", "DOWN/USDT", "BEAR/USDT", "BULL/USDT",
    "USDC/USDT", "TUSD/USDT", "FDUSD/USDT", "DAI/USDT", "USDP/USDT",
    "USDE/USDT", "UST/USDT", "USD/USDT", "XUSD/USDT", "USD1/USDT", "BFUSD/USDT",
    "USTC/USDT", "BUSD/USDT", "FRAX/USDT", "LUSD/USDT", "GUSD/USDT", "SUSD/USDT",
    "USDS/USDT", "USDX/USDT", "USDD/USDT", "CUSD/USDT", "OUSD/USDT", "MUSD/USDT",
    "RLUSD/USDT", "U/USDT",
    "EUR/USDT", "TRY/USDT", "GBP/USDT", "BRL/USDT", "RUB/USDT",
}
LEVERAGED_PATTERNS = ["UP","DOWN","BULL","BEAR","3L","3S","2L","2S","5L","5S","10L","10S"]

# ═══════════════════════════════════════════════════════════════════════
# VERİ ÇEKME
# ═══════════════════════════════════════════════════════════════════════
def get_top_coins(n=50):
    ex = ccxt.binance({"enableRateLimit": True})
    ex.load_markets()
    tickers = ex.fetch_tickers()
    coins = []
    for sym, t in tickers.items():
        if not sym.endswith("/USDT"): continue
        if sym in IGNORED_COINS: continue
        base = sym.replace("/USDT", "")
        if any(base.endswith(p) for p in LEVERAGED_PATTERNS): continue
        vol = float(t.get("quoteVolume") or 0)
        if vol >= MIN_VOL:
            coins.append((sym, vol))
    coins.sort(key=lambda x: x[1], reverse=True)
    result = [s for s, _ in coins[:n]]
    if "BTC/USDT" not in result:
        result.insert(0, "BTC/USDT")
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
        time.sleep(0.25)
    if not bars: return None
    df = pd.DataFrame(bars, columns=["timestamp","open","high","low","close","volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df.set_index("timestamp", inplace=True)
    return df[~df.index.duplicated(keep="first")]


def load_or_fetch(symbol, force=False):
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, symbol.replace("/", "_") + ".pkl")
    if not force and os.path.exists(path):
        with open(path, "rb") as f:
            return pickle.load(f)
    df = fetch_ohlcv_full(symbol)
    if df is not None:
        with open(path, "wb") as f:
            pickle.dump(df, f)
    return df


# ═══════════════════════════════════════════════════════════════════════
# İNDİKATÖRLER  — bot.py prepare_bars() birebir
# ═══════════════════════════════════════════════════════════════════════
def prepare_bars(df):
    df = df.copy()
    c, h, l, v = df["close"], df["high"], df["low"], df["volume"]

    df["vol_ma"]     = v.rolling(VOL_PERIOD).mean()
    tr               = pd.concat([h-l, (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
    df["atr"]        = tr.ewm(alpha=1/14, adjust=False).mean()
    df["atr_pct"]    = df["atr"] / c * 100
    df["ema50"]      = c.ewm(span=50,  adjust=False).mean()
    df["ema200"]     = c.ewm(span=200, adjust=False).mean()
    df["close_prev"] = c.shift(1)

    ema21 = c.ewm(span=21, adjust=False).mean()
    ma50  = c.rolling(50).mean()
    ma200 = c.rolling(200).mean()
    df["ema21"]       = ema21
    df["dist_ema21"]  = (c - ema21)  / ema21.replace(0, np.nan)  * 100
    df["dist_ma50"]   = (c - ma50)   / ma50.replace(0, np.nan)   * 100
    df["dist_ma200"]  = (c - ma200)  / ma200.replace(0, np.nan)  * 100
    df["ma200_slope"] = (ma200 - ma200.shift(20)) / ma200.shift(20).abs().replace(0, np.nan) * 100

    bb_mid = c.rolling(20).mean(); bb_std = c.rolling(20).std()
    bb_w   = (bb_mid + 2*bb_std - (bb_mid - 2*bb_std)) / bb_mid.replace(0, np.nan)
    df["bb_width"]     = bb_w
    df["bb_width_ch3"] = bb_w.diff(3)

    df["mom5_pct"]  = (c - c.shift(5))  / c.shift(5).abs().replace(0, np.nan)  * 100
    df["mom10_pct"] = (c - c.shift(10)) / c.shift(10).abs().replace(0, np.nan) * 100

    roll_max = c.expanding(min_periods=50).max()
    df["coin_drawdown"]  = (c - roll_max) / roll_max.replace(0, np.nan) * 100
    bar_idx              = pd.Series(np.arange(len(c), dtype=float), index=c.index)
    last_high_pos        = bar_idx.where(c >= roll_max * (1 - 1e-6)).ffill().fillna(0)
    df["days_since_high"]= bar_idx - last_high_pos

    # ROCKET için vektörsel ADX (Wilder yaklaşımı)
    up = h.diff(); dn = -l.diff()
    dm_p = up.where((up > dn) & (up > 0), 0.0)
    dm_m = dn.where((dn > up) & (dn > 0), 0.0)
    alpha = 1/14
    atr_w = tr.ewm(alpha=alpha, adjust=False).mean()
    dmp_w = dm_p.ewm(alpha=alpha, adjust=False).mean()
    dmm_w = dm_m.ewm(alpha=alpha, adjust=False).mean()
    di_p  = (dmp_w / atr_w * 100).fillna(0)
    di_m  = (dmm_w / atr_w * 100).fillna(0)
    denom = (di_p + di_m).replace(0, np.nan)
    dx    = (np.abs(di_p - di_m) / denom * 100).fillna(0)
    df["adx"]      = dx.ewm(alpha=alpha, adjust=False).mean()
    df["di_plus"]  = di_p
    df["di_minus"] = di_m

    # ROCKET: 24h değişim ve hacim oranı
    df["change_24h"]   = (c / c.shift(24) - 1) * 100
    df["vol_ratio_20"] = v / v.rolling(20).mean().shift(1)

    return df.dropna(subset=["vol_ma", "atr", "close_prev"])


# ═══════════════════════════════════════════════════════════════════════
# BTC FİLTRE SERİSİ
# ═══════════════════════════════════════════════════════════════════════
def build_btc_filters(btc_df):
    df = btc_df.copy()
    df["ema21_1h"] = df["close"].ewm(span=21, adjust=False).mean()
    btc_ema21_ok   = (df["close"] > df["ema21_1h"])

    df4h = df.resample("4h", label="right", closed="right").agg(
        {"open":"first","high":"max","low":"min","close":"last","volume":"sum"}
    ).dropna(subset=["close"])
    df4h["ema21"] = df4h["close"].ewm(span=21, adjust=False).mean()
    df4h["ema50"] = df4h["close"].ewm(span=50, adjust=False).mean()
    df4h["ema200"]= df4h["close"].ewm(span=200,adjust=False).mean()

    # ROCKET filtresi: BTC 4H close >= ema200 → OK
    df4h["rocket_ok"]  = df4h["close"] >= df4h["ema200"]

    # SMC crash: son 4H bar değişimi > -3%
    df4h["crash_ok"]   = (df4h["close"] / df4h["close"].shift(1) - 1) * 100 > -3.0

    # SMC 4H structural paused:
    # Son 2 kapanmış 4H bar EMA21 altında, 2. daha düşük,
    # en son mumun çoğunluğu EMA50 altında
    c21 = df4h["ema21"].values; c50 = df4h["ema50"].values
    cls = df4h["close"].values; hi = df4h["high"].values; lo = df4h["low"].values
    n   = len(cls)
    paused = np.zeros(n, dtype=bool)
    for i in range(3, n):
        c1_c=cls[i-1]; c2_c=cls[i-2]
        c1_h=hi[i-1];  c1_l=lo[i-1]
        if c1_c < c21[i-1] and c2_c < c21[i-2] and c1_c < c2_c:
            rng = c1_h - c1_l
            if rng > 0 and (c50[i-1] - c1_l) / rng > 0.5:
                paused[i] = True
    df4h["paused"] = paused

    # 1H serisine yeniden eşle (ffill)
    idx = df.index
    rocket_s  = df4h["rocket_ok"].reindex(idx, method="ffill").fillna(True)
    crash_ok  = df4h["crash_ok"].reindex(idx,  method="ffill").fillna(True)
    paused_s  = df4h["paused"].reindex(idx,    method="ffill").fillna(False)

    return pd.DataFrame({
        "rocket_ok":    rocket_s.astype(bool),
        "smc_ema21_ok": btc_ema21_ok.astype(bool),
        "smc_crash_ok": crash_ok.astype(bool),
        "smc_4h_paused":paused_s.astype(bool),
    }, index=idx)


# ═══════════════════════════════════════════════════════════════════════
# SMC FONKSİYONLARI  — SMC.py birebir
# ═══════════════════════════════════════════════════════════════════════
def luxalgo_smc(df):
    h = df["high"].values; l = df["low"].values; c = df["close"].values
    n = len(df)
    if n < SWING_LEN + 10: return None
    legs = [0]*n; cur = 0
    for i in range(SWING_LEN, n):
        ph = h[i-SWING_LEN]; pl = l[i-SWING_LEN]
        wh = max(h[i-SWING_LEN+1:i+1]); wl = min(l[i-SWING_LEN+1:i+1])
        if ph > wh: cur = 0
        elif pl < wl: cur = 1
        legs[i] = cur
    sh=None; sl=None; shx=True; slx=True; trend=0
    tt=None; tb=None
    for i in range(SWING_LEN, n):
        if legs[i] != (legs[i-1] if i>0 else 0):
            if legs[i]==1: sl=l[i-SWING_LEN]; slx=False; tb=sl
            else:           sh=h[i-SWING_LEN]; shx=False; tt=sh
        if tt is not None and h[i]>tt: tt=h[i]
        if tb is not None and l[i]<tb: tb=l[i]
        if i < n-1:
            if sh is not None and not shx and c[i]>sh and c[i-1]<=sh: shx=True; trend=1
            if sl is not None and not slx and c[i]<sl and c[i-1]>=sl: slx=True; trend=-1
    top = tt if tt is not None else max(h)
    bot = tb if tb is not None else min(l)
    if top == bot: return None
    depth    = (top - c[-1]) / (top - bot) * 100
    disc_top = 0.55*top + 0.45*bot
    return {
        "depth": depth, "in_discount": c[-1] <= disc_top,
        "discount_top": disc_top, "discount_bottom": bot,
        "swing_trend": trend,
    }


def detect_micro_choch(df):
    h = df["high"].values; l = df["low"].values; c = df["close"].values
    n = len(df)
    if n < CHOCH_SW + 10: return None, None, 0, None, None
    legs=[0]*n; cur=0
    for i in range(CHOCH_SW, n):
        ph=h[i-CHOCH_SW]; pl=l[i-CHOCH_SW]
        wh=max(h[i-CHOCH_SW+1:i+1]); wl=min(l[i-CHOCH_SW+1:i+1])
        if ph>wh: cur=0
        elif pl<wl: cur=1
        legs[i]=cur
    sh=None; sl=None; shx=True; slx=True; trend=0
    bt=None; bd=None; cl=None; sw_low=None
    for i in range(CHOCH_SW+1, n):
        if legs[i]!=legs[i-1]:
            if legs[i]==1: sl=l[i-CHOCH_SW]; slx=False; sw_low=sl
            else:           sh=h[i-CHOCH_SW]; shx=False
        if i < n-1:
            if sh is not None and not shx and c[i]>sh and c[i-1]<=sh: shx=True; trend=1
            if sl is not None and not slx and c[i]<sl and c[i-1]>=sl: slx=True; trend=-1
        if i == n-1:
            if sh is not None and not shx and c[i]>sh and c[i-1]<=sh:
                bt="CHoCH" if trend==-1 else "BOS"; bd="BULLISH"; trend=1; cl=sh
            if sl is not None and not slx and c[i]<sl and c[i-1]>=sl:
                bt="CHoCH" if trend==1 else "BOS"; bd="BEARISH"; trend=-1; cl=sl
    return bt, bd, trend, cl, sw_low


def _4h_confirm(df_1h):
    try:
        df4 = df_1h.resample("4h",label="right",closed="right").agg(
            {"open":"first","high":"max","low":"min","close":"last","volume":"sum"}
        ).dropna()
        df4 = df4.iloc[:-1]
        if len(df4) < 21: return True
        _,_,st,_,_ = detect_micro_choch(df4)
        if st < 0: return False
        ema21 = df4["close"].ewm(span=21, adjust=False).mean().iloc[-1]
        return float(df4["close"].iloc[-1]) > ema21
    except Exception:
        return True


def calc_rsi(closes, period=14):
    if len(closes) < period+1: return None
    a = np.array(closes[-(period*3):], dtype=float)
    d = np.diff(a)
    g = np.where(d>0,d,0.0); lo=np.where(d<0,-d,0.0)
    ag=np.mean(g[-period:]); al=np.mean(lo[-period:])
    if al==0: return 100.0
    return round(100-100/(1+ag/al), 1)


# ═══════════════════════════════════════════════════════════════════════
# TİCARET SİMÜLASYONU
# ═══════════════════════════════════════════════════════════════════════
def simulate(df_future, entry, stop, tp1, tp2, tp3, expire_h,
             half_exit=False, trailing=False):
    """
    Gerçekçi bar-by-bar simülasyon.
    Döner: {status, close_pct, peak_pct, low_pct, tp1_hit}
    """
    if entry <= 0:
        return {"status":"expired","close_pct":0,"peak_pct":0,"low_pct":0,"tp1_hit":False}

    peak = 0.0; low_p = 0.0; tp1_hit = False
    trail_stop = stop; trail_active = False
    rows = df_future.iloc[:expire_h]

    for _, row in rows.iterrows():
        bar_h = float(row["high"]); bar_l = float(row["low"]); bar_c = float(row["close"])

        cur_peak = (bar_h - entry) / entry * 100
        if cur_peak > peak: peak = round(cur_peak, 4)
        cur_low = (bar_l - entry) / entry * 100
        if cur_low < low_p: low_p = round(cur_low, 4)

        if tp1 and bar_h >= tp1: tp1_hit = True

        # Trailing stop güncelle
        if trailing and (trail_active or peak >= TRAILING_MIN):
            peak_price = entry * (1 + peak/100)
            new_ts     = peak_price * (1 - TRAILING_PCT)
            if new_ts > trail_stop:
                trail_stop   = new_ts
                trail_active = True

        stp = trail_stop if trail_active else stop

        # Stop kontrolü
        if bar_l <= stp:
            cr = round((stp - entry) / entry * 100, 4)
            return {"status":"win" if cr>0 else "loss", "close_pct":cr,
                    "peak_pct":peak, "low_pct":low_p, "tp1_hit":tp1_hit}

        # TP2/TP3 tam çıkış
        if tp3 and bar_h >= tp3:
            cr = round((tp3 - entry) / entry * 100, 4)
            return {"status":"win","close_pct":cr,"peak_pct":peak,"low_pct":low_p,"tp1_hit":True}
        if tp2 and bar_h >= tp2:
            if half_exit:
                tp1_pct = (tp1 - entry) / entry * 100
                tp2_pct = (tp2 - entry) / entry * 100
                cr = round((tp1_pct + tp2_pct) / 2, 4)
            else:
                cr = round((tp2 - entry) / entry * 100, 4)
            return {"status":"win","close_pct":cr,"peak_pct":peak,"low_pct":low_p,"tp1_hit":True}
        if not half_exit and tp1 and bar_h >= tp1:
            cr = round((tp1 - entry) / entry * 100, 4)
            return {"status":"win","close_pct":cr,"peak_pct":peak,"low_pct":low_p,"tp1_hit":True}

    # Expire
    last_c = float(rows.iloc[-1]["close"]) if len(rows)>0 else entry
    cr = round((last_c - entry) / entry * 100, 4)
    return {"status":"expired","close_pct":cr,"peak_pct":peak,"low_pct":low_p,"tp1_hit":tp1_hit}


# ═══════════════════════════════════════════════════════════════════════
# SONUÇ TOPLAYICILAR
# ═══════════════════════════════════════════════════════════════════════
def empty_b():
    return {"wins":0,"losses":0,"expired":0,"total":0,
            "pnl":0.0,"expired_pnl":0.0,"peaks":[]}

def empty_sim():
    return {"tp2":0,"tp1":0,"stop":0,"open":0,"pnl":0.0,"total":0}

def rec(b, r):
    b["total"] += 1; b["peaks"].append(r["peak_pct"]); pct = r["close_pct"]
    if r["status"]=="expired":  b["expired"]+=1; b["expired_pnl"]+=pct
    elif r["status"]=="win":    b["wins"]+=1;    b["pnl"]+=pct
    else:                        b["losses"]+=1;  b["pnl"]+=pct

def rec_sim(b, r):
    pk=r["peak_pct"]; dp=r["low_pct"]
    if pk >= SIM_TP2:               k="tp2"; p=SIM_TP2
    elif pk >= SIM_TP1 and dp > SIM_STOP: k="tp1"; p=SIM_TP1
    elif dp <= SIM_STOP:            k="stop"; p=SIM_STOP
    else:                           k="open"; p=0.0
    b[k]+=1; b["pnl"]+=p; b["total"]+=1

def fin(b):
    dec=b["wins"]+b["losses"]
    b["wr"]=round(b["wins"]/dec*100,1) if dec>0 else 0
    b["pnl"]=round(b["pnl"],2); b["expired_pnl"]=round(b["expired_pnl"],2)
    b["win_loss_pnl"]=b["pnl"]
    b["total_pnl"]=round(b["pnl"]+b["expired_pnl"],2)
    b["avg_peak"]=round(sum(b["peaks"])/len(b["peaks"]),2) if b["peaks"] else 0
    del b["peaks"]; return b

def fin_sim(b):
    dec=b["tp2"]+b["tp1"]+b["stop"]
    b["wr"]=round((b["tp2"]+b["tp1"])/dec*100,1) if dec>0 else 0
    b["pnl"]=round(b["pnl"],2); return b


# ═══════════════════════════════════════════════════════════════════════
# ANA BACKTEST
# ═══════════════════════════════════════════════════════════════════════
def run(symbols, btc_df):
    print("BTC filtre serisi hazırlanıyor...")
    btc_f = build_btc_filters(btc_df)

    R = {
        "panik_pump": empty_b(), "pump_orta":  empty_b(),
        "pump_uzun":  empty_b(), "rocket":     empty_b(),
        "smc_disc":   {"total":0},              # Phase 1 sayım
        "smc_choch":  empty_b(),                # ½TP1 + ½TP2 (gerçek)
        "smc_tp1":    empty_b(),                # Tam TP1
        "smc_tp2":    empty_b(),                # Tam TP2
        "bot_actual": empty_b(),                # Bot tümü — trailing
        "bot_tp1":    empty_b(),                # Bot tümü — sadece TP1
        "sim_bot":    empty_sim(),
        "sim_smc":    empty_sim(),
        "eski_disc":  empty_b(),
        "eski_choch": empty_b(),
    }

    total = len(symbols)
    for sym_i, symbol in enumerate(symbols, 1):
        print(f"[{sym_i}/{total}] {symbol}", flush=True)
        df_raw = load_or_fetch(symbol)
        if df_raw is None or len(df_raw) < 300:
            print("  → Yetersiz veri, atlanıyor."); continue

        df = prepare_bars(df_raw)
        if len(df) < 300:
            print("  → prepare_bars sonrası yetersiz."); continue

        btc = btc_f.reindex(df.index, method="ffill")
        n   = len(df)

        # Cooldown takibi (unix saat cinsinden)
        last = {"capit":0.0,"t72":0.0,"t168":0.0,"rocket":0.0,
                "smc":0.0,"smc_disc":0.0,"eski_d":0.0,"eski_c":0.0}
        disc_active = False
        disc_ts     = 0.0

        for i in range(250, n):
            ts_h = df.index[i].timestamp() / 3600  # unix saat

            bar = df.iloc[i]
            if pd.isna(bar["vol_ma"]) or pd.isna(bar["atr"]): continue

            bf = btc.iloc[i] if i < len(btc) else None
            rocket_ok = bool(bf["rocket_ok"])    if bf is not None else True
            ema21_ok  = bool(bf["smc_ema21_ok"]) if bf is not None else True
            crash_ok  = bool(bf["smc_crash_ok"]) if bf is not None else True
            paused    = bool(bf["smc_4h_paused"]) if bf is not None else False

            df_future = df.iloc[i+1:]
            sl100     = df.iloc[max(0,i-99):i+1]  # SMC için son 100 bar

            entry_c = float(bar["close"])

            # ── PANİK PUMP ───────────────────────────────────────────
            if ts_h - last["capit"] >= COOLDOWN_H["capit"]:
                cp = float(bar["close_prev"])
                if cp > 0 and float(bar["vol_ma"]) > 0:
                    ret1 = (entry_c / cp - 1) * 100
                    volr = float(bar["volume"]) / float(bar["vol_ma"])
                    if (CRASH_MIN <= ret1 <= CRASH_MAX and
                            VOL_MIN <= volr <= VOL_MAX and
                            entry_c >= float(bar["open"]) and i >= 5):
                        c5 = float(df["close"].iloc[i-4])
                        if c5 <= 0 or (c5 - entry_c) / c5 * 100 < 4.0:
                            stop=entry_c*0.97; tp1=entry_c*1.05; tp2=entry_c*1.10; tp3=entry_c*1.15
                            r = simulate(df_future, entry_c, stop, tp1, tp2, tp3,
                                         EXPIRE_H["capit"], trailing=True)
                            rec(R["panik_pump"], r); rec(R["bot_actual"], r)
                            r1 = simulate(df_future, entry_c, stop, tp1, None, None,
                                          EXPIRE_H["capit"])
                            rec(R["bot_tp1"], r1)
                            rec_sim(R["sim_bot"], r)
                            last["capit"] = ts_h

            # ── T72 ──────────────────────────────────────────────────
            if ts_h - last["t72"] >= COOLDOWN_H["t72"]:
                m5=bar.get("mom5_pct"); de=bar.get("dist_ema21")
                dd=bar.get("coin_drawdown"); ms=bar.get("ma200_slope")
                if all(v is not None and not pd.isna(v) for v in [m5,de,dd,ms]):
                    if (float(m5)>=T72_MOM5 and float(de)<=T72_EMA and
                            float(dd)>=T72_DD and float(ms)>=T72_SLP):
                        stop=entry_c*0.95; tp1=entry_c*1.10; tp2=entry_c*1.15
                        r = simulate(df_future, entry_c, stop, tp1, tp2, None,
                                     EXPIRE_H["t72"], trailing=True)
                        rec(R["pump_orta"], r); rec(R["bot_actual"], r)
                        r1 = simulate(df_future, entry_c, stop, tp1, None, None, EXPIRE_H["t72"])
                        rec(R["bot_tp1"], r1); rec_sim(R["sim_bot"], r)
                        last["t72"] = ts_h

            # ── T168 ─────────────────────────────────────────────────
            if ts_h - last["t168"] >= COOLDOWN_H["t168"]:
                dm=bar.get("dist_ma200"); d50=bar.get("dist_ma50")
                m10=bar.get("mom10_pct"); dh=bar.get("days_since_high")
                if all(v is not None and not pd.isna(v) for v in [dm,d50,m10,dh]):
                    if (float(dm)>=T168_MA200 and float(d50)<=T168_MA50 and
                            float(m10)>=T168_MOM10 and float(dh)<=T168_DAYS):
                        stop=entry_c*0.92; tp1=entry_c*1.25
                        r = simulate(df_future, entry_c, stop, tp1, None, None,
                                     EXPIRE_H["t168"], trailing=True)
                        rec(R["pump_uzun"], r); rec(R["bot_actual"], r)
                        r1 = simulate(df_future, entry_c, stop, tp1, None, None, EXPIRE_H["t168"])
                        rec(R["bot_tp1"], r1); rec_sim(R["sim_bot"], r)
                        last["t168"] = ts_h

            # ── ROCKET ───────────────────────────────────────────────
            if rocket_ok and ts_h - last["rocket"] >= COOLDOWN_H["rocket"]:
                ch24=bar.get("change_24h"); vr20=bar.get("vol_ratio_20")
                adx=bar.get("adx"); dip=bar.get("di_plus"); dim=bar.get("di_minus")
                if all(v is not None and not pd.isna(v) for v in [ch24,vr20,adx,dip,dim]):
                    if (float(ch24)>=10.0 and float(vr20)>=1.2 and
                            float(adx)>=25 and float(dip)>float(dim)):
                        stop=entry_c*0.95; tp1=entry_c*1.08; tp2=entry_c*1.15; tp3=entry_c*1.25
                        r = simulate(df_future, entry_c, stop, tp1, tp2, tp3,
                                     EXPIRE_H["rocket"], trailing=True)
                        rec(R["rocket"], r); rec(R["bot_actual"], r)
                        r1 = simulate(df_future, entry_c, stop, tp1, None, None, EXPIRE_H["rocket"])
                        rec(R["bot_tp1"], r1); rec_sim(R["sim_bot"], r)
                        last["rocket"] = ts_h

            # ── SMC Phase 1 & 2 ──────────────────────────────────────
            try:
                smc = luxalgo_smc(sl100)
                if smc:
                    rsi = calc_rsi(sl100["close"].tolist())
                    # Phase 1: Discount Zone
                    if (smc["in_discount"] and smc["depth"] >= P1_DEPTH and
                            rsi is not None and rsi <= P1_RSI and
                            ema21_ok and crash_ok and not paused and
                            ts_h - last["smc_disc"] >= COOLDOWN_H["smc"]):
                        disc_active  = True
                        disc_ts      = ts_h
                        last["smc_disc"] = ts_h
                        R["smc_disc"]["total"] += 1

                    # Phase 1 çok eskiyse sıfırla (7 gün)
                    if disc_active and ts_h - disc_ts > 168:
                        disc_active = False

                    # Phase 2: CHoCH
                    if disc_active and crash_ok and not paused:
                        bt, bd, _, cl, sw_low = detect_micro_choch(sl100)
                        if bt=="CHoCH" and bd=="BULLISH" and cl is not None:
                            if _4h_confirm(sl100) and ts_h - last["smc"] >= COOLDOWN_H["smc"]:
                                ep   = cl
                                slp  = (sw_low * 0.995) if sw_low else ep * 0.95
                                risk = ep - slp
                                if risk <= 0: risk = ep * 0.05
                                tp1p = ep + risk; tp2p = ep + risk*2

                                # ½TP1 + ½TP2 (gerçek)
                                ra = simulate(df_future, ep, slp, tp1p, tp2p, None,
                                              EXPIRE_H["smc"], half_exit=True)
                                rec(R["smc_choch"], ra); rec_sim(R["sim_smc"], ra)
                                # Tam TP1
                                r1 = simulate(df_future, ep, slp, tp1p, None, None, EXPIRE_H["smc"])
                                rec(R["smc_tp1"], r1)
                                # Tam TP2
                                r2 = simulate(df_future, ep, slp, tp1p, tp2p, None, EXPIRE_H["smc"])
                                rec(R["smc_tp2"], r2)
                                disc_active  = False
                                last["smc"]  = ts_h

                    # ── ESKİ SMC ─────────────────────────────────────
                    # Eski Discount
                    if (smc["discount_bottom"] > 0 and
                            entry_c <= smc["discount_bottom"] * (1 + ESKI_DEPTH/100) and
                            ts_h - last["eski_d"] >= COOLDOWN_H["eski"]):
                        atr_v = float(bar["atr"]) if not pd.isna(bar["atr"]) else entry_c*0.02
                        stop_e=entry_c-atr_v*4; tp1_e=entry_c+atr_v*4; tp2_e=entry_c+atr_v*6
                        if stop_e > 0:
                            re = simulate(df_future, entry_c, stop_e, tp1_e, tp2_e, None,
                                          EXPIRE_H["eski"], trailing=True)
                            rec(R["eski_disc"], re)
                            last["eski_d"] = ts_h

                    # Eski CHoCH
                    bt_e, bd_e, _, cl_e, sl_e = detect_micro_choch(sl100)
                    if (bt_e=="CHoCH" and bd_e=="BULLISH" and cl_e is not None and
                            crash_ok and ts_h - last["eski_c"] >= COOLDOWN_H["eski"]):
                        slp_e = (sl_e * 0.995) if sl_e else cl_e * 0.95
                        risk_e = cl_e - slp_e
                        if risk_e <= 0: risk_e = cl_e * 0.05
                        re = simulate(df_future, cl_e, slp_e,
                                      cl_e+risk_e, cl_e+risk_e*2, None,
                                      EXPIRE_H["eski"], half_exit=True)
                        rec(R["eski_choch"], re)
                        last["eski_c"] = ts_h

            except Exception:
                pass

    # Finalize
    for k in ("panik_pump","pump_orta","pump_uzun","rocket",
              "smc_choch","smc_tp1","smc_tp2","bot_actual","bot_tp1",
              "eski_disc","eski_choch"):
        R[k] = fin(R[k])
    R["sim_bot"] = fin_sim(R["sim_bot"])
    R["sim_smc"] = fin_sim(R["sim_smc"])
    return R


# ═══════════════════════════════════════════════════════════════════════
# RAPOR
# ═══════════════════════════════════════════════════════════════════════
def report(R, symbols):
    W = 100
    def row(label, b):
        t=b.get("total",0)
        if t==0: print(f"  {label:<38} — sinyal yok"); return
        w=b["wins"]; lo=b["losses"]; e=b["expired"]
        wr=b["wr"]; wlp=b["win_loss_pnl"]; ep=b["expired_pnl"]; tp=b["total_pnl"]; pk=b["avg_peak"]
        print(f"  {label:<38} {t:4d} sin | W:{w:3d} L:{lo:3d} E:{e:3d} "
              f"WR:%{wr:5.1f} | W/L:{wlp:+7.2f}% Exp:{ep:+7.2f}% "
              f"TOP:{tp:+7.2f}% | PK:+{pk:.1f}%")

    def srow(label, b):
        t=b.get("total",0)
        if t==0: print(f"  {label:<38} — sinyal yok"); return
        print(f"  {label:<38} {t:4d} sin | TP2:{b['tp2']:3d} TP1:{b['tp1']:3d} "
              f"STOP:{b['stop']:3d} Açık:{b['open']:3d} | WR:%{b['wr']:5.1f} | "
              f"P&L:{b['pnl']:+7.2f}%")

    print("\n" + "═"*W)
    print(f"  BACKTEST — 2020-01-01 → bugün | {len(symbols)} coin | 1H Binance")
    print("═"*W)
    print("─"*W)
    print("  BİREYSEL SİSTEMLER")
    print("─"*W)
    row("1. PANİK PUMP",            R["panik_pump"])
    row("2. T72 (ORTA VADE)",       R["pump_orta"])
    row("3. T168 (UZUN VADE)",      R["pump_uzun"])
    row("4. ROCKET",                R["rocket"])
    print(f"  {'5. SMC Discount (Phase 1)':<38} {R['smc_disc']['total']:4d} uyarı (trade yok)")
    row("6. SMC CHoCH — ½TP1+½TP2",R["smc_choch"])
    print("─"*W)
    print("  FARKLI ÇIKIŞ STRATEJİLERİ")
    print("─"*W)
    row("7. SMC — Tam TP1 (%100)",  R["smc_tp1"])
    row("8. SMC — Tam TP2 (%100)",  R["smc_tp2"])
    row("9. Bot (tümü) — Trailing", R["bot_actual"])
    row("10. Bot (tümü) — Tam TP1", R["bot_tp1"])
    print("─"*W)
    print(f"  HAYALİ SENARYO  (TP1:+{SIM_TP1}% / TP2:+{SIM_TP2}% / Stop:{SIM_STOP}%)")
    print("─"*W)
    srow("11. Bot — Hayali",        R["sim_bot"])
    srow("12. SMC — Hayali",        R["sim_smc"])
    print("─"*W)
    print("  ESKİ SMC (Karşılaştırma)")
    print("─"*W)
    row("13. Eski Discount",        R["eski_disc"])
    row("14. Eski CHoCH",           R["eski_choch"])
    print("═"*W + "\n")


# ═══════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-fetch",  action="store_true", help="Cache'i kullan, tekrar indirme")
    ap.add_argument("--coins",     nargs="*",           help="Belirli coinler")
    ap.add_argument("--n",         type=int, default=N_COINS, help=f"Coin sayısı (default:{N_COINS})")
    args = ap.parse_args()

    os.makedirs(DATA_DIR, exist_ok=True)

    if args.coins:
        symbols = args.coins
        if "BTC/USDT" not in symbols:
            symbols = ["BTC/USDT"] + symbols
    else:
        print("Top coinler alınıyor...")
        symbols = get_top_coins(args.n)
        print(f"{len(symbols)} coin seçildi: {symbols[:5]}...")

    # BTC verisi
    print("\nBTC/USDT yükleniyor...")
    btc_raw = load_or_fetch("BTC/USDT", force=False)
    if btc_raw is None:
        print("BTC verisi alınamadı!"); return
    btc_df = prepare_bars(btc_raw)

    # Coin verilerini indir
    if not args.no_fetch:
        print(f"\n--- Veri İndirme ({len(symbols)} coin) ---")
        for i, sym in enumerate(symbols, 1):
            if sym == "BTC/USDT": continue
            path = os.path.join(DATA_DIR, sym.replace("/","_")+".pkl")
            if os.path.exists(path):
                print(f"  [{i}/{len(symbols)}] {sym} — cache var")
                continue
            print(f"  [{i}/{len(symbols)}] {sym} indiriliyor...", end=" ", flush=True)
            load_or_fetch(sym)
            print("✓")

    print(f"\n--- Backtest Başlıyor ---")
    t0 = time.time()
    R  = run(symbols, btc_df)
    dt = time.time() - t0
    print(f"\nSüre: {dt/60:.1f} dakika")

    with open("backtest_results.json","w") as f:
        json.dump(R, f, indent=2, default=str)
    print("Sonuçlar: backtest_results.json")

    report(R, symbols)


if __name__ == "__main__":
    main()
