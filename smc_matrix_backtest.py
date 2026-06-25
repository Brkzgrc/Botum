#!/usr/bin/env python3
"""
SMC Matrix Backtest — 16 Senaryo × 2 Pozisyon × 3 Çıkış = 96 Senaryo
paper_backtest_chatgpt.py motoru üzerine — komisyon + slippage dahil.

2021'den veri (warmup) → 2022'den sinyal | $20K maks işlem

Çıkış modları:
  half : TP1 %50 + Trail %2.5  (canlı sistem)
  tp1  : Tümü TP1'de kapat
  tp2  : Tümü TP2'de kapat

Kullanım:
  python smc_matrix_backtest.py --no-fetch   # mevcut cache kullan
  python smc_matrix_backtest.py              # Binance'den güncelle

Not: Script çalıştıış dizine kaydeder — Yeni Klasör 2 içinden çalıştır.
"""

import argparse, heapq, json, os, pickle, time
import datetime as _dt
import numpy as np, pandas as pd

# ─── AYARLAR ────────────────────────────────────────────────────────────────────────────
DATA_DIR      = "backtest_data"
START_DATE    = pd.Timestamp("2022-01-01", tz="UTC")
INITIAL_CAP   = 5_000.0
MAX_POS_LIST  = [5, 10]
EXIT_NAMES    = ["half", "tp1", "tp2"]
FEE_RATE      = 0.001
SLIPPAGE      = 0.0005
MAX_POS_SIZE  = 20_000.0
COOLDOWN_H    = 24
EXPIRE_H      = 168
CHOCH_SWING   = 5
MIN_VOL_24H   = 5_000_000
BTC_CRASH_PCT = 3.0
SMC_TRAIL_PCT = 2.5

START_TS = int(_dt.datetime(2021, 1, 1, tzinfo=_dt.timezone.utc).timestamp() * 1000)

# ─── MATRİS TANIMI ──────────────────────────────────────────────────────────────────────────
FILTER_MODES = [
    ("no_filter",    False, False),
    ("crash_only",   True,  False),
    ("trend_only",   False, True),
    ("both_filters", True,  True),
]
VOL_THRESHOLDS = [2.0, 3.0, 5.0, 10.0]

SCENARIO_INFO = {}
SCENARIO_KEYS = []
for _mn, _uc, _ut in FILTER_MODES:
    for _vt in VOL_THRESHOLDS:
        _k = f"{_mn}_v{int(_vt)}"
        SCENARIO_KEYS.append(_k)
        SCENARIO_INFO[_k] = (_mn, _uc, _ut, _vt)

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


# ─── VERİ ───────────────────────────────────────────────────────────────────────────────────
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


# ─── BTC FİLTRELERI ───────────────────────────────────────────────────────────────────────────
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


# ─── CHoCH TESPİTİ ──────────────────────────────────────────────────────────────────────────
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


# ─── ÇIKIŞ FONKSİYONLARI ──────────────────────────────────────────────────────────────────────────
def exit_half(sig, pos_size):
    """TP1 %50 kapat, kalanı trail (canlı sistem)."""
    entry=sig["entry"]; stop=sig["stop"]; tp1=sig["tp1"]; tp2=sig["tp2"]
    rows=sig["future"]; expire_h=sig.get("expire_h", EXPIRE_H)
    eff_entry = entry * (1 + SLIPPAGE)
    sp=(stop-eff_entry)/eff_entry; t1p=(tp1-eff_entry)/eff_entry; t2p=(tp2-eff_entry)/eff_entry
    peak=entry; tp1_hit=False
    for ts, row in rows.iloc[:expire_h].iterrows():
        h=float(row["high"]); l=float(row["low"])
        if not tp1_hit:
            if l<=stop: return ts, pos_size*(1+sp)*(1-FEE_RATE), "stop"
            if h>=tp1: tp1_hit=True; peak=max(entry, h)
        else:
            if h>peak: peak=h
            trail=peak*(1-SMC_TRAIL_PCT/100)
            if h>=tp2: return ts, pos_size*(1+(t1p+t2p)/2)*(1-FEE_RATE), "tp2"
            if l<=trail:
                trail_pct=(trail-eff_entry)/eff_entry
                return ts, pos_size*(1+(t1p+trail_pct)/2)*(1-FEE_RATE), "trail"
    if len(rows)>0:
        idx=min(expire_h-1, len(rows)-1)
        last_pct=(float(rows.iloc[idx]["close"])-eff_entry)/eff_entry
        ret=(t1p+last_pct)/2 if tp1_hit else last_pct
        return rows.index[idx], pos_size*(1+ret)*(1-FEE_RATE), "expire"
    return sig["entry_time"], pos_size*(1-FEE_RATE), "no_data"


def exit_tp1(sig, pos_size):
    """Tümünü TP1'de kapat."""
    entry=sig["entry"]; stop=sig["stop"]; tp1=sig["tp1"]
    rows=sig["future"]; expire_h=sig.get("expire_h", EXPIRE_H)
    eff_entry = entry * (1 + SLIPPAGE)
    sp=(stop-eff_entry)/eff_entry; t1p=(tp1-eff_entry)/eff_entry
    for ts, row in rows.iloc[:expire_h].iterrows():
        h=float(row["high"]); l=float(row["low"])
        if l<=stop: return ts, pos_size*(1+sp)*(1-FEE_RATE), "stop"
        if h>=tp1: return ts, pos_size*(1+t1p)*(1-FEE_RATE), "tp1"
    if len(rows)>0:
        idx=min(expire_h-1, len(rows)-1)
        last_pct=(float(rows.iloc[idx]["close"])-eff_entry)/eff_entry
        return rows.index[idx], pos_size*(1+last_pct)*(1-FEE_RATE), "expire"
    return sig["entry_time"], pos_size*(1-FEE_RATE), "no_data"


def exit_tp2(sig, pos_size):
    """Tümünü TP2'de kapat."""
    entry=sig["entry"]; stop=sig["stop"]; tp1=sig["tp1"]; tp2=sig["tp2"]
    rows=sig["future"]; expire_h=sig.get("expire_h", EXPIRE_H)
    eff_entry = entry * (1 + SLIPPAGE)
    sp=(stop-eff_entry)/eff_entry; t2p=(tp2-eff_entry)/eff_entry
    for ts, row in rows.iloc[:expire_h].iterrows():
        h=float(row["high"]); l=float(row["low"])
        if l<=stop: return ts, pos_size*(1+sp)*(1-FEE_RATE), "stop"
        if h>=tp2: return ts, pos_size*(1+t2p)*(1-FEE_RATE), "tp2"
    if len(rows)>0:
        idx=min(expire_h-1, len(rows)-1)
        last_pct=(float(rows.iloc[idx]["close"])-eff_entry)/eff_entry
        return rows.index[idx], pos_size*(1+last_pct)*(1-FEE_RATE), "expire"
    return sig["entry_time"], pos_size*(1-FEE_RATE), "no_data"


EXIT_FNS = {"half": exit_half, "tp1": exit_tp1, "tp2": exit_tp2}


# ─── SİNYAL TOPLAMA ─────────────────────────────────────────────────────────────────────────────
def collect_signals(symbols, btc_filters, fetch=True):
    scenario_sigs = {key: [] for key in SCENARIO_KEYS}

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

        c_arr  = df["close"].values
        vol24  = df["vol_24h_usd"].values
        volr20 = df["vol_ratio_20"].values
        n = len(df)

        last = {key: 0.0 for key in SCENARIO_KEYS}

        for i in range(250, n-1):
            ts = df.index[i]
            if ts < START_DATE: continue
            price = c_arr[i]
            if np.isnan(price) or price <= 0: continue
            if np.isnan(vol24[i]) or vol24[i] < MIN_VOL_24H: continue
            if not (bts[i] == "CHoCH" and bds[i] == "BULLISH"): continue

            vr        = float(volr20[i]) if not np.isnan(volr20[i]) else 0.0
            crash_ok  = bool(btc_al["crash_ok"].iloc[i])
            trend_ok  = bool(btc_al["downtrend_ok"].iloc[i])
            ts_h      = ts.timestamp() / 3600

            choch_lvl = cls_[i]; sw_low = swls[i]
            entry = choch_lvl if choch_lvl else price
            stop  = sw_low * 0.995 if sw_low else entry * 0.95
            if stop >= entry: stop = entry * 0.95
            risk  = max(entry - stop, entry * 0.01)
            tp1   = entry + risk; tp2 = entry + risk * 2

            future = df.iloc[i+1:i+1+EXPIRE_H][["high","low","close"]]

            for key, (mode_name, use_crash, use_trend, vol_thresh) in SCENARIO_INFO.items():
                if vr < vol_thresh: continue
                if use_crash and not crash_ok: continue
                if use_trend and not trend_ok: continue
                if ts_h - last[key] < COOLDOWN_H: continue

                scenario_sigs[key].append({
                    "symbol":     symbol,
                    "entry_time": ts,
                    "entry":      entry,
                    "stop":       stop,
                    "tp1":        tp1,
                    "tp2":        tp2,
                    "future":     future,
                    "expire_h":   EXPIRE_H,
                    "vol_ratio":  vr,
                    "risk_pct":   round(risk / entry * 100, 2),
                })
                last[key] = ts_h

    for key in scenario_sigs:
        scenario_sigs[key].sort(key=lambda x: x["entry_time"].timestamp())

    return scenario_sigs


# ─── PORTFÖY SİMÜLASYONU ──────────────────────────────────────────────────────────────────────────
def simulate_portfolio(signals, exit_fn, initial_cap=INITIAL_CAP, max_positions=5):
    cash=initial_cap; open_count=0; open_positions={}; max_open=0
    trade_log=[]; equity_pts=[(START_DATE, initial_cap)]
    queue=[]; counter=0
    for sig in signals:
        heapq.heappush(queue, (sig["entry_time"].timestamp(), 1, counter, "signal", sig))
        counter+=1
    while queue:
        unix_ts,_,_,etype,data = heapq.heappop(queue)
        ts = pd.Timestamp(unix_ts, unit="s", tz="UTC")
        if etype == "signal":
            if open_count >= max_positions: continue
            pos_size = min(cash / max_positions, MAX_POS_SIZE)
            if pos_size < 1: continue
            entry_fee = pos_size * FEE_RATE
            cash -= pos_size + entry_fee
            open_count += 1
            if open_count > max_open: max_open = open_count
            trade_id = counter; counter += 1
            open_positions[trade_id] = pos_size
            sig = data
            exit_ts, cash_ret, label = exit_fn(sig, pos_size)
            heapq.heappush(queue, (exit_ts.timestamp(), 0, counter, "exit", {
                "trade_id":  trade_id,
                "cash_ret":  cash_ret,
                "pos_size":  pos_size,
                "entry_fee": entry_fee,
                "label":     label,
            }))
            counter += 1
            equity_pts.append((ts, cash + sum(open_positions.values())))
        elif etype == "exit":
            d = data
            cash += d["cash_ret"]
            open_count -= 1
            open_positions.pop(d["trade_id"], None)
            net_pnl = d["cash_ret"] - (d["pos_size"] + d["entry_fee"])
            trade_log.append({
                "label":    d["label"],
                "net_pnl":  net_pnl,
                "pos_size": d["pos_size"],
            })
            equity_pts.append((ts, cash + sum(open_positions.values())))
    return trade_log, equity_pts, cash, max_open


# ─── İSTATİSTİK ───────────────────────────────────────────────────────────────────────────────────
def compute_stats(trade_log, equity_pts, n_sigs):
    wins    = [t for t in trade_log if t["label"] in ("tp2","tp1","trail")]
    stops   = [t for t in trade_log if t["label"] == "stop"]
    expires = [t for t in trade_log if t["label"] == "expire"]
    dec     = len(wins) + len(stops)
    wr      = len(wins) / dec * 100 if dec > 0 else 0.0
    final   = equity_pts[-1][1] if equity_pts else INITIAL_CAP
    ret     = (final - INITIAL_CAP) / INITIAL_CAP * 100
    peak    = INITIAL_CAP; max_dd = 0.0
    for _, cap in equity_pts:
        if cap > peak: peak = cap
        dd = (cap - peak) / peak * 100
        if dd < max_dd: max_dd = dd
    avg_win  = sum(t["net_pnl"]/t["pos_size"]*100 for t in wins)  / len(wins)  if wins  else 0.0
    avg_loss = sum(t["net_pnl"]/t["pos_size"]*100 for t in stops) / len(stops) if stops else 0.0
    return {
        "n_sigs":   n_sigs,
        "trades":   len(trade_log),
        "wins":     len(wins),
        "losses":   len(stops),
        "expires":  len(expires),
        "wr":       round(wr, 1),
        "final":    round(final, 2),
        "ret":      round(ret, 2),
        "max_dd":   round(max_dd, 2),
        "avg_win":  round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "max_open": 0,
    }


# ─── HTML ÇIKTI ───────────────────────────────────────────────────────────────────────────────────
MODE_COLORS = {
    "no_filter":    "#3a86ff",
    "crash_only":   "#06d6a0",
    "trend_only":   "#ffd166",
    "both_filters": "#ef476f",
}
MODE_LABELS = {
    "no_filter":    "No Filter",
    "crash_only":   "Crash Only",
    "trend_only":   "Trend Only",
    "both_filters": "Both Filters",
}
EXIT_LABELS = {"half": "Half+Trail", "tp1": "TP1", "tp2": "TP2"}
EXIT_COLORS = {"half": "#c9d1d9", "tp1": "#ffd166", "tp2": "#06d6a0"}


def build_grid_html(results, exit_name, mp):
    head = "<tr><th>Filtre \\ Vol</th>"
    for vt in VOL_THRESHOLDS:
        head += f"<th>≥{vt:.0f}x</th>"
    head += "</tr>"
    rows = ""
    for mode_name, _, _ in FILTER_MODES:
        color = MODE_COLORS[mode_name]
        rows += f'<tr><td style="color:{color};font-weight:700">{MODE_LABELS[mode_name]}</td>'
        for vt in VOL_THRESHOLDS:
            rkey = f"{mode_name}_v{int(vt)}_p{mp}_{exit_name}"
            r    = results.get(rkey, {})
            final = r.get("final", INITIAL_CAP)
            wr    = r.get("wr", 0)
            ret   = r.get("ret", 0)
            c     = "#00c853" if ret >= 0 else "#d32f2f"
            rows += (f'<td style="text-align:center">'
                     f'<span style="color:{c};font-weight:700">${final:,.0f}</span><br>'
                     f'<span style="color:#8b949e;font-size:0.75rem">WR {wr:.0f}% | {ret:+.0f}%</span></td>')
        rows += "</tr>"
    return f"<table><thead>{head}</thead><tbody>{rows}</tbody></table>"


def generate_html(results, n_coins):
    run_date = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")

    tab_contents = ""
    for ei, exit_name in enumerate(EXIT_NAMES):
        display = "block" if ei == 0 else "none"
        ec = EXIT_COLORS[exit_name]
        g5  = build_grid_html(results, exit_name, 5)
        g10 = build_grid_html(results, exit_name, 10)
        tab_contents += f"""
<div id="tab_{exit_name}" class="tab-content" style="display:{display}">
  <div class="grids">
    <div class="card">
      <div class="grid-title" style="color:{ec}">5 Pozisyon — {EXIT_LABELS[exit_name]}</div>
      {g5}
    </div>
    <div class="card">
      <div class="grid-title" style="color:{ec}">10 Pozisyon — {EXIT_LABELS[exit_name]}</div>
      {g10}
    </div>
  </div>
</div>"""

    detail_rows = ""
    for exit_name in EXIT_NAMES:
        for mp in MAX_POS_LIST:
            for key in SCENARIO_KEYS:
                mode_name, _, _, vt = SCENARIO_INFO[key]
                rkey = f"{key}_p{mp}_{exit_name}"
                r    = results.get(rkey, {})
                final   = r.get("final", INITIAL_CAP)
                ret     = r.get("ret", 0)
                wr      = r.get("wr", 0)
                n_sigs  = r.get("n_sigs", 0)
                trades  = r.get("trades", 0)
                avg_win = r.get("avg_win", 0)
                max_dd  = r.get("max_dd", 0)
                c   = "#00c853" if ret >= 0 else "#d32f2f"
                mc  = MODE_COLORS[mode_name]
                ec  = EXIT_COLORS[exit_name]
                detail_rows += (
                    f'<tr>'
                    f'<td style="color:{ec};font-weight:700">{EXIT_LABELS[exit_name]}</td>'
                    f'<td><b>p{mp}</b></td>'
                    f'<td><span style="color:{mc}">■</span> {MODE_LABELS[mode_name]}</td>'
                    f'<td>≥{vt:.0f}x</td>'
                    f'<td>{n_sigs}</td><td>{trades}</td><td>{wr:.0f}%</td>'
                    f'<td style="color:{c}">{avg_win:+.2f}%</td>'
                    f'<td style="color:{c}">{max_dd:.1f}%</td>'
                    f'<td style="color:{c}">{ret:+.1f}%</td>'
                    f'<td style="color:{c};font-weight:700">${final:,.0f}</td>'
                    f'<td><input type="checkbox" class="tog" data-key="{rkey}" checked></td>'
                    f'</tr>'
                )

    datasets = []
    for exit_name in EXIT_NAMES:
        for mp in MAX_POS_LIST:
            for key in SCENARIO_KEYS:
                mode_name, _, _, vt = SCENARIO_INFO[key]
                rkey = f"{key}_p{mp}_{exit_name}"
                r    = results.get(rkey, {})
                eq   = r.get("equity", [])
                if not eq: continue
                pts   = [{"x": ts.strftime("%Y-%m-%d"), "y": round(v, 2)}
                         for ts, v in eq if hasattr(ts, "strftime")]
                color = MODE_COLORS[mode_name]
                alpha = {2.0: "ff", 3.0: "cc", 5.0: "88", 10.0: "44"}[vt]
                label = f"{EXIT_LABELS[exit_name]} p{mp} {MODE_LABELS[mode_name]} ≥{vt:.0f}x"
                dash  = "[]" if mp == 5 else "[6,3]"
                datasets.append(
                    f'{{"label":{json.dumps(label)},"data":{json.dumps(pts)},'
                    f'"borderColor":"{color}{alpha}","backgroundColor":"{color}11",'
                    f'"borderWidth":{"2" if mp==5 else "1"},"pointRadius":0,"fill":false,"tension":0.1,'
                    f'"borderDash":{dash},"key":"{rkey}","exit_name":"{exit_name}","pos":{mp}}}'
                )
    ds_js = "[" + ",".join(datasets) + "]"

    tab_btns = ""
    for exit_name in EXIT_NAMES:
        ec = EXIT_COLORS[exit_name]
        active = 'style="border-color:{ec};color:{ec}"'.format(ec=ec) if exit_name == EXIT_NAMES[0] else ""
        tab_btns += f'<button id="tabBtn_{exit_name}" onclick="switchTab(\'{exit_name}\')" {active}>{EXIT_LABELS[exit_name]}</button>'

    return f"""<!DOCTYPE html><html lang="tr"><head>
<meta charset="UTF-8"><title>SMC Matrix — 96 Senaryo</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#0d1117;color:#c9d1d9;font-family:-apple-system,monospace;padding:20px}}
h1{{color:#58a6ff;font-size:1.4rem;margin-bottom:6px}}
h2{{color:#8b949e;font-size:0.95rem;margin:16px 0 8px}}
.meta{{color:#8b949e;font-size:0.78rem;background:#161b22;padding:10px;border-radius:6px;border:1px solid #30363d;margin-bottom:16px}}
.card{{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:14px;margin-bottom:16px;overflow-x:auto}}
.grids{{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:0}}
@media(max-width:900px){{.grids{{grid-template-columns:1fr}}}}
table{{width:100%;border-collapse:collapse;font-size:0.82rem}}
th{{background:#21262d;color:#8b949e;padding:8px 10px;text-align:left}}
td{{padding:7px 10px;border-bottom:1px solid #21262d}}
tr:hover td{{background:#1c2128}}
canvas{{max-height:460px}}
.ctrl{{display:flex;gap:8px;margin-bottom:10px;flex-wrap:wrap}}
button{{background:#21262d;border:1px solid #30363d;color:#c9d1d9;padding:5px 12px;border-radius:5px;cursor:pointer;font-size:0.8rem}}
button:hover{{background:#30363d}}
input[type=checkbox]{{cursor:pointer;accent-color:#58a6ff}}
.legend{{display:flex;gap:16px;flex-wrap:wrap;margin-bottom:10px;font-size:0.78rem}}
.leg-item{{display:flex;align-items:center;gap:5px}}
.leg-dot{{width:12px;height:12px;border-radius:2px}}
.grid-title{{color:#8b949e;font-size:0.85rem;font-weight:700;margin-bottom:8px;padding:6px 0;border-bottom:1px solid #30363d}}
.tab-bar{{display:flex;gap:8px;margin-bottom:12px}}
.tab-bar button{{font-size:0.9rem;padding:6px 18px;border-radius:6px}}
</style></head><body>
<h1>📊 SMC Matrix Backtest — 96 Senaryo (16 × 2pos × 3exit)</h1>
<div class="meta">
  {run_date} | {n_coins} coin | 1H Binance | 2022→bugün |
  ${INITIAL_CAP:,.0f} başlangıç | ${MAX_POS_SIZE:,.0f}/işlem |
  Komisyon %{FEE_RATE*100:.1f} giriş+çıkış | Slippage %{SLIPPAGE*100:.2f}
</div>

<div class="legend">
  <span class="leg-item"><span class="leg-dot" style="background:#3a86ff"></span>No Filter</span>
  <span class="leg-item"><span class="leg-dot" style="background:#06d6a0"></span>Crash Only</span>
  <span class="leg-item"><span class="leg-dot" style="background:#ffd166"></span>Trend Only</span>
  <span class="leg-item"><span class="leg-dot" style="background:#ef476f"></span>Both Filters</span>
  <span style="color:#8b949e;margin-left:8px">Çizgi: düz=5pos | kesik=10pos</span>
</div>

<h2>Özet Grid</h2>
<div class="tab-bar">{tab_btns}</div>
{tab_contents}

<h2>Detay Tablosu</h2>
<div class="card"><table>
<thead><tr><th>Çıkış</th><th>Pos</th><th>Filtre</th><th>Vol</th><th>Sinyal</th><th>Trade</th><th>WR%</th>
<th>Ort Kazanç</th><th>MaxDD</th><th>Getiri%</th><th>Son Sermaye</th><th>Graf.</th></tr></thead>
<tbody>{detail_rows}</tbody>
</table></div>

<h2>Equity Curve</h2>
<div class="card">
<div class="ctrl">
  <button onclick="showAll()">Tümü</button>
  <button onclick="hideAll()">Gizle</button>
  <button onclick="filterExit('half')">Half+Trail</button>
  <button onclick="filterExit('tp1')">TP1</button>
  <button onclick="filterExit('tp2')">TP2</button>
  <button onclick="filterPos(5)">5 Pos</button>
  <button onclick="filterPos(10)">10 Pos</button>
  <button onclick="filterMode('no_filter')">No Filter</button>
  <button onclick="filterMode('crash_only')">Crash Only</button>
  <button onclick="filterMode('trend_only')">Trend Only</button>
  <button onclick="filterMode('both_filters')">Both Filters</button>
</div>
<canvas id="ec"></canvas></div>

<script>
const ds={ds_js};
const ch=new Chart(document.getElementById('ec'),{{type:'line',data:{{datasets:ds}},options:{{
  responsive:true,animation:false,interaction:{{mode:'index',intersect:false}},
  plugins:{{legend:{{display:false}},
    tooltip:{{backgroundColor:'#1c2128',borderColor:'#30363d',borderWidth:1,
      titleColor:'#c9d1d9',bodyColor:'#c9d1d9',
      callbacks:{{label:c=>` ${{c.dataset.label}}: $${{c.parsed.y.toLocaleString()}}`}}}}}},
  scales:{{
    x:{{type:'category',ticks:{{color:'#8b949e',maxTicksLimit:14,maxRotation:0}},grid:{{color:'#21262d'}}}},
    y:{{ticks:{{color:'#8b949e',callback:v=>'$'+v.toLocaleString()}},grid:{{color:'#21262d'}},
       title:{{display:true,text:'Portföy ($)',color:'#8b949e'}}}}
  }}
}}}});

document.querySelectorAll('.tog').forEach(cb=>cb.addEventListener('change',function(){{
  const k=this.dataset.key;
  const idx=ds.findIndex(d=>d.key===k);
  if(idx>=0){{ch.data.datasets[idx].hidden=!this.checked;ch.update();}}
}}));
function showAll(){{ch.data.datasets.forEach(d=>d.hidden=false);document.querySelectorAll('.tog').forEach(c=>c.checked=true);ch.update();}}
function hideAll(){{ch.data.datasets.forEach(d=>d.hidden=true);document.querySelectorAll('.tog').forEach(c=>c.checked=false);ch.update();}}
function filterExit(e){{
  ch.data.datasets.forEach(d=>d.hidden=d.exit_name!==e);
  document.querySelectorAll('.tog').forEach(c=>{{c.checked=c.dataset.key.includes('_'+e);}});
  ch.update();
}}
function filterPos(p){{
  ch.data.datasets.forEach(d=>d.hidden=d.pos!==p);
  document.querySelectorAll('.tog').forEach(c=>{{c.checked=c.dataset.key.includes('_p'+p+'_');}});
  ch.update();
}}
function filterMode(mode){{
  ch.data.datasets.forEach(d=>d.hidden=!d.key.startsWith(mode));
  document.querySelectorAll('.tog').forEach(c=>{{c.checked=c.dataset.key.startsWith(mode);}});
  ch.update();
}}
function switchTab(name){{
  document.querySelectorAll('.tab-content').forEach(el=>el.style.display='none');
  document.getElementById('tab_'+name).style.display='block';
}}
</script></body></html>"""


# ─── MAIN ───────────────────────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-fetch", action="store_true")
    args = ap.parse_args()

    do_fetch = not args.no_fetch

    btc_raw = load_or_fetch("BTC/USDT") if do_fetch else load_pkl("BTC/USDT")
    if btc_raw is None:
        print("HATA: BTC/USDT verisi yok."); return

    print("BTC 4H filtreleri hesaplanıyor...")
    btc_filters = compute_btc_filters(btc_raw)

    if args.no_fetch:
        symbols = get_cached_symbols()
    else:
        print("Binance spot listesi alınıyor...")
        symbols = get_all_binance_symbols() or get_cached_symbols()

    if not symbols:
        print("Geçerli coin bulunamadı."); return

    total = len(SCENARIO_KEYS) * len(MAX_POS_LIST) * len(EXIT_NAMES)
    print(f"\n{len(symbols)} coin | ${INITIAL_CAP:,.0f} sermaye | ${MAX_POS_SIZE:,.0f} maks/işlem")
    print(f"Komisyon: %{FEE_RATE*100:.1f} | Slippage: %{SLIPPAGE*100:.2f}")
    print(f"Matrix: {len(FILTER_MODES)} filtre × {len(VOL_THRESHOLDS)} vol × {len(MAX_POS_LIST)} pos × {len(EXIT_NAMES)} exit = {total} senaryo")
    print(f"Sinyaller toplanıyor ({START_DATE.date()} → bugün)...\n")

    scenario_sigs = collect_signals(symbols, btc_filters, fetch=do_fetch)

    print("\nSinyal sayıları:")
    for key in SCENARIO_KEYS:
        print(f"  {key:<25}: {len(scenario_sigs[key])} sinyal")

    print(f"\nPortföy simülasyonları ({total} adet)...")
    results = {}
    for exit_name in EXIT_NAMES:
        exit_fn = EXIT_FNS[exit_name]
        for mp in MAX_POS_LIST:
            print(f"\n  ── exit={exit_name} | pos={mp} ──")
            for key in SCENARIO_KEYS:
                sigs = scenario_sigs[key]
                log, eq, final_cash, max_open = simulate_portfolio(sigs, exit_fn, max_positions=mp)
                st = compute_stats(log, eq, len(sigs))
                st["max_open"] = max_open
                rkey = f"{key}_p{mp}_{exit_name}"
                results[rkey] = {**st, "equity": eq}
                print(f"    {rkey:<40}: WR {st['wr']:.1f}% | ${st['final']:>12,.0f} | DD {st['max_dd']:.1f}%")

    W = 110
    print("\n" + "═"*W)
    print(f"  ÖZET — {len(symbols)} coin | 2022→bugün | ${INITIAL_CAP:,.0f} başlangıç")
    print("═"*W)
    hdr = f"  {'Senaryo':<28}"
    for exit_name in EXIT_NAMES:
        for mp in MAX_POS_LIST:
            lbl = f"{EXIT_LABELS[exit_name]}/p{mp}"
            hdr += f" {lbl:>14}"
    print(hdr)
    print("─"*W)
    prev_mode = None
    for key in SCENARIO_KEYS:
        mode_name, _, _, _ = SCENARIO_INFO[key]
        if mode_name != prev_mode:
            print("─"*W); prev_mode = mode_name
        row = f"  {key:<28}"
        bests = []
        for exit_name in EXIT_NAMES:
            for mp in MAX_POS_LIST:
                rkey = f"{key}_p{mp}_{exit_name}"
                r = results.get(rkey, {})
                bests.append(r.get("final", 0))
        max_val = max(bests)
        for exit_name in EXIT_NAMES:
            for mp in MAX_POS_LIST:
                rkey = f"{key}_p{mp}_{exit_name}"
                r = results.get(rkey, {})
                val = r.get("final", 0)
                star = "*" if val == max_val else " "
                row += f" ${val:>11,.0f}{star}"
        print(row)
    print("═"*W)
    print("  (* = bu satırda en yüksek)")

    now     = _dt.datetime.now()
    now_str = now.strftime("%Y%m%d_%H%M")
    start_s = START_DATE.strftime("%Y%m%d")
    end_s   = now.strftime("%Y%m%d")
    base    = f"smc_matrix_MULTI_{start_s}_{end_s}_{now_str}"

    out_json = base + ".json"
    summary  = {k: {ek: ev for ek, ev in v.items() if ek != "equity"}
                for k, v in results.items()}
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
    print(f"\n✓ {out_json} kaydedildi")

    out_html = base + ".html"
    with open(out_html, "w", encoding="utf-8") as f:
        f.write(generate_html(results, len(symbols)))
    print(f"✓ {out_html} kaydedildi\n")


if __name__ == "__main__":
    main()
