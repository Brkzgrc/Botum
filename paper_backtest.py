#!/usr/bin/env python3
"""
Paper Trading Backtest — 2 Bağımsız Sistem
  Sistem A: Baseline Tam TP2  — tüm BULLISH CHoCH sinyalleri
  Sistem B: V2 Tam TP2        — BULLISH CHoCH + vol_ratio >= 1.5x

Her sistem $5,000 sermaye ile bağımsız çalışır.
Metodoloji: SMC.py artımlı CHoCH, BTC crash + downtrend filtresi.
Dönem: 2025-01-01 → bugün

Kullanım:
  python paper_backtest.py
  python paper_backtest.py --no-fetch
  python paper_backtest.py --capital 5000 --size 500
"""

import argparse, heapq, json, os, pickle, time
import datetime as _dt
import numpy as np, pandas as pd

# ─── AYARLAR ────────────────────────────────────────────────────────────────
DATA_DIR      = "backtest_data"
START_DATE    = pd.Timestamp("2025-01-01", tz="UTC")
INITIAL_CAP   = 5_000.0
POS_SIZE      = 500.0
MAX_POSITIONS = 10
COOLDOWN_H    = 24
EXPIRE_H      = 168
CHOCH_SWING   = 5
MIN_VOL_24H   = 5_000_000
BTC_CRASH_PCT = 3.0

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

START_TS = int(_dt.datetime(2024, 1, 1, tzinfo=_dt.timezone.utc).timestamp() * 1000)


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


# ─── BTC FİLTRELERİ (SMC.py birebir) ───────────────────────────────────────
def compute_btc_filters(btc_1h):
    """crash_ok + downtrend_ok — SMC.py check_btc_crash + check_btc_downtrend_active"""
    df4 = btc_1h.resample("4h", label="right", closed="right").agg(
        {"open":"first","high":"max","low":"min","close":"last","volume":"sum"}
    ).dropna(subset=["close"])

    n4  = len(df4)
    h4  = df4["high"].values
    l4  = df4["low"].values
    c4  = df4["close"].values

    crash_ok = np.ones(n4, dtype=bool)
    for i in range(1, n4):
        crash_ok[i] = (c4[i] / c4[i-1] - 1) * 100 > -BTC_CRASH_PCT

    legs4 = np.zeros(n4, dtype=int); cur4 = 0
    for i in range(CHOCH_SWING, n4):
        ph = h4[i-CHOCH_SWING]; pl = l4[i-CHOCH_SWING]
        wh = h4[i-CHOCH_SWING+1:i+1].max(); wl = l4[i-CHOCH_SWING+1:i+1].min()
        if ph > wh: cur4 = 0
        elif pl < wl: cur4 = 1
        legs4[i] = cur4

    sh4=None; shx4=True; sl4=None; slx4=True; prev_sl4=None; trend4=0
    downtrend = np.zeros(n4, dtype=bool)
    for i in range(CHOCH_SWING+1, n4):
        if legs4[i] != legs4[i-1]:
            if legs4[i] == 1: prev_sl4=sl4; sl4=l4[i-CHOCH_SWING]; slx4=False
            else: sh4=h4[i-CHOCH_SWING]; shx4=False
        ci, cp = c4[i], c4[i-1]
        if sh4 is not None and not shx4 and ci>sh4 and cp<=sh4: shx4=True; trend4=1
        if sl4 is not None and not slx4 and ci<sl4 and cp>=sl4: slx4=True; trend4=-1
        if trend4 == -1 and (prev_sl4 is None or (sl4 is not None and sl4<=prev_sl4)):
            downtrend[i] = True

    df4["crash_ok"]  = crash_ok
    df4["downtrend"] = downtrend
    idx = btc_1h.index
    crash_s    = df4["crash_ok"].reindex(idx,method="ffill").fillna(True).astype(bool)
    downtrend_s = df4["downtrend"].reindex(idx,method="ffill").fillna(False).astype(bool)
    return pd.DataFrame({"crash_ok": crash_s, "downtrend_ok": ~downtrend_s}, index=idx)


# ─── CHoCH TESPİTİ (artımlı) ────────────────────────────────────────────────
def run_choch_incremental(df):
    h = df["high"].values; l = df["low"].values; c = df["close"].values
    n = len(df)
    legs = np.zeros(n, dtype=int); cur = 0
    for i in range(CHOCH_SWING, n):
        ph=h[i-CHOCH_SWING]; pl=l[i-CHOCH_SWING]
        wh=h[i-CHOCH_SWING+1:i+1].max(); wl=l[i-CHOCH_SWING+1:i+1].min()
        if ph>wh: cur=0
        elif pl<wl: cur=1
        legs[i]=cur
    sh=None; shx=True; sl=None; slx=True; trend=0
    bts=[None]*n; bds=[None]*n; cls_=[None]*n; swls=[None]*n
    for i in range(CHOCH_SWING+1, n):
        if legs[i] != legs[i-1]:
            if legs[i]==1: sl=l[i-CHOCH_SWING]; slx=False
            else: sh=h[i-CHOCH_SWING]; shx=False
        ci, cp = c[i], c[i-1]
        bt=None; bd=None; cl=None
        if sh is not None and not shx and ci>sh and cp<=sh:
            bt="CHoCH" if trend==-1 else "BOS"; bd="BULLISH"; cl=sh; shx=True; trend=1
        if sl is not None and not slx and ci<sl and cp>=sl:
            bt="CHoCH" if trend==1 else "BOS"; bd="BEARISH"; cl=sl; slx=True; trend=-1
        bts[i]=bt; bds[i]=bd; cls_[i]=cl; swls[i]=sl
    return bts, bds, cls_, swls


# ─── SİNYAL TOPLAMA ─────────────────────────────────────────────────────────
def collect_signals(symbols, btc_filters, fetch=True):
    """
    İki bağımsız sinyal listesi döner:
      sigs_a: Baseline — tüm BULLISH CHoCH (S3)
      sigs_b: V2       — BULLISH CHoCH + vol_ratio >= 1.5x (S6)
    Her iki sistem için cooldown bağımsız takip edilir.
    """
    sigs_a = []   # Sistem A: Baseline Tam TP2
    sigs_b = []   # Sistem B: V2 Tam TP2

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

        vol24  = df["vol_24h_usd"].values
        volr20 = df["vol_ratio_20"].values
        n      = len(df)

        last_a = 0.0   # cooldown Sistem A
        last_b = 0.0   # cooldown Sistem B

        for i in range(250, n):
            ts = df.index[i]
            if ts < START_DATE: continue
            if bts[i] != "CHoCH" or bds[i] != "BULLISH": continue

            entry  = cls_[i]
            sw_low = swls[i]
            if entry is None or entry <= 0: continue

            stop = sw_low * 0.995 if sw_low else entry * 0.95
            if stop >= entry: stop = entry * 0.95
            risk = entry - stop
            if risk <= 0: risk = entry * 0.05
            tp1 = entry + risk
            tp2 = entry + risk * 2

            if pd.isna(vol24[i]) or vol24[i] < MIN_VOL_24H: continue
            if i >= len(btc_al): continue
            bf = btc_al.iloc[i]
            if not bf["crash_ok"] or not bf["downtrend_ok"]: continue

            ts_h   = ts.timestamp() / 3600
            future = df.iloc[i+1:i+1+EXPIRE_H][["high","low","close"]].copy()
            vr     = float(volr20[i]) if not pd.isna(volr20[i]) else 0.0

            sig = {
                "symbol":     symbol,
                "entry_time": ts,
                "entry":      entry,
                "stop":       stop,
                "tp1":        tp1,
                "tp2":        tp2,
                "future":     future,
                "vol_ratio":  vr,
                "risk_pct":   round(risk / entry * 100, 2),
            }

            if ts_h - last_a >= COOLDOWN_H:
                sigs_a.append(sig.copy())
                last_a = ts_h

            if vr >= 1.5 and ts_h - last_b >= COOLDOWN_H:
                sigs_b.append(sig.copy())
                last_b = ts_h

    sigs_a.sort(key=lambda x: x["entry_time"].timestamp())
    sigs_b.sort(key=lambda x: x["entry_time"].timestamp())
    return sigs_a, sigs_b


# ─── ÇIKIŞ (Tam TP2) ────────────────────────────────────────────────────────
def compute_exit(sig, pos_size):
    """Stop → stop | TP2 → TP2 | TP1 → TP1 | Expire → market"""
    entry = sig["entry"]; stop = sig["stop"]
    tp1 = sig["tp1"];     tp2 = sig["tp2"]
    rows = sig["future"]
    stop_pct = (stop - entry) / entry
    tp1_pct  = (tp1  - entry) / entry
    tp2_pct  = (tp2  - entry) / entry
    for ts, row in rows.iloc[:EXPIRE_H].iterrows():
        h = float(row["high"]); l = float(row["low"])
        if l <= stop: return ts, pos_size*(1+stop_pct), "stop"
        if h >= tp2:  return ts, pos_size*(1+tp2_pct),  "tp2"
        if h >= tp1:  return ts, pos_size*(1+tp1_pct),  "tp1"
    if len(rows) > 0:
        last_c   = float(rows.iloc[min(EXPIRE_H-1, len(rows)-1)]["close"])
        exp_pct  = (last_c - entry) / entry
        return rows.index[min(EXPIRE_H-1, len(rows)-1)], pos_size*(1+exp_pct), "expire"
    return sig["entry_time"], pos_size, "no_data"


# ─── PORTFÖY SİMÜLASYONU ────────────────────────────────────────────────────
def simulate_portfolio(signals, initial_cap, pos_size, max_positions):
    cash = initial_cap; open_count = 0
    trade_log = []; equity_pts = [(START_DATE, initial_cap)]
    queue = []; counter = 0
    for sig in signals:
        heapq.heappush(queue, (sig["entry_time"].timestamp(), 1, counter, "signal", sig))
        counter += 1
    while queue:
        unix_ts, _, _, etype, data = heapq.heappop(queue)
        ts = pd.Timestamp(unix_ts, unit="s", tz="UTC")
        if etype == "signal":
            if cash < pos_size or open_count >= max_positions: continue
            sig = data; cash -= pos_size; open_count += 1
            trade_id = counter; counter += 1
            exit_ts, cash_ret, label = compute_exit(sig, pos_size)
            heapq.heappush(queue, (exit_ts.timestamp(), 0, counter, "exit", {
                "trade_id":   trade_id,   "symbol":     sig["symbol"],
                "entry_time": sig["entry_time"], "entry": sig["entry"],
                "stop":       sig["stop"], "tp1": sig["tp1"], "tp2": sig["tp2"],
                "cash_ret":   cash_ret,   "label":  label,
                "stop_pct":   round((sig["stop"]-sig["entry"])/sig["entry"]*100,2),
                "tp1_pct":    round((sig["tp1"]-sig["entry"])/sig["entry"]*100,2),
                "tp2_pct":    round((sig["tp2"]-sig["entry"])/sig["entry"]*100,2),
                "risk_pct":   sig.get("risk_pct",0),
            }))
            counter += 1
            trade_log.append({
                "type":"ENTRY","trade_id":trade_id,"symbol":sig["symbol"],
                "time":str(ts)[:16],"entry":round(sig["entry"],6),
                "stop_pct":round((sig["stop"]-sig["entry"])/sig["entry"]*100,2),
                "tp1_pct":round((sig["tp1"]-sig["entry"])/sig["entry"]*100,2),
                "tp2_pct":round((sig["tp2"]-sig["entry"])/sig["entry"]*100,2),
                "risk_pct":sig.get("risk_pct",0),"vol_ratio":round(sig.get("vol_ratio",0),2),
                "size":pos_size,"cash_after":round(cash,2),"open":open_count,
            })
            equity_pts.append((ts, cash))
        elif etype == "exit":
            d = data; cash += d["cash_ret"]; open_count -= 1
            net_pnl = d["cash_ret"] - pos_size
            trade_log.append({
                "type":"EXIT","trade_id":d["trade_id"],"symbol":d["symbol"],
                "entry_time":str(d["entry_time"])[:16],"time":str(ts)[:16],
                "label":d["label"],"net_pnl":round(net_pnl,2),
                "net_pct":round(net_pnl/pos_size*100,2),"cash_ret":round(d["cash_ret"],2),
                "cash_after":round(cash,2),"open":open_count,
                "tp1_pct":d["tp1_pct"],"tp2_pct":d["tp2_pct"],
                "stop_pct":d["stop_pct"],"risk_pct":d["risk_pct"],
            })
            equity_pts.append((ts, cash))
    return trade_log, equity_pts, cash


# ─── RAPOR ──────────────────────────────────────────────────────────────────
def system_stats(trade_log, equity_pts, initial_cap, pos_size):
    entries      = [t for t in trade_log if t["type"]=="ENTRY"]
    exits        = [t for t in trade_log if t["type"]=="EXIT"]
    tp2_exits    = [e for e in exits if e["label"]=="tp2"]
    tp1_exits    = [e for e in exits if e["label"]=="tp1"]
    stop_exits   = [e for e in exits if e["label"]=="stop"]
    expire_exits = [e for e in exits if e["label"] in ("expire","no_data")]
    wins    = len(tp2_exits) + len(tp1_exits)
    losses  = len(stop_exits)
    decided = wins + losses
    wr      = wins / decided * 100 if decided > 0 else 0.0
    final   = equity_pts[-1][1] if equity_pts else initial_cap
    ret     = (final - initial_cap) / initial_cap * 100
    peak = initial_cap; max_dd = 0.0
    for _, cap in equity_pts:
        if cap > peak: peak = cap
        dd = (cap - peak) / peak * 100
        if dd < max_dd: max_dd = dd
    avg_win  = sum(e["net_pct"] for e in tp2_exits+tp1_exits)/wins if wins else 0
    avg_loss = sum(e["net_pct"] for e in stop_exits)/losses if losses else 0
    monthly = {}; weekly = {}
    for ts, cap in equity_pts:
        monthly[str(ts)[:7]] = cap
        iso = ts.isocalendar()
        weekly[f"{iso[0]}-W{iso[1]:02d}"] = cap
    days = (_dt.datetime.utcnow() - _dt.datetime(2025,1,1)).days or 1
    return {
        "entries":entries,"exits":exits,"tp2":tp2_exits,"tp1":tp1_exits,
        "stops":stop_exits,"expires":expire_exits,
        "wins":wins,"losses":losses,"wr":wr,
        "final":final,"ret":ret,"max_dd":max_dd,
        "avg_win":avg_win,"avg_loss":avg_loss,
        "monthly":monthly,"weekly":weekly,
        "sigs_per_day":len(entries)/days,
    }


def print_system(label, st, initial_cap, pos_size):
    W = 96
    print()
    print("═"*W)
    print(f"  {label}")
    print(f"  Başlangıç: ${initial_cap:,.2f}  →  Bitiş: ${st['final']:,.2f}  "
          f"({st['ret']:+.1f}%)  |  MaxDD: {st['max_dd']:.1f}%")
    print("─"*W)
    print(f"  Toplam trade: {len(st['entries'])}  ({st['sigs_per_day']:.1f}/gün)  |  "
          f"TP2: {len(st['tp2'])}  TP1: {len(st['tp1'])}  "
          f"Stop: {len(st['stops'])}  Expire: {len(st['expires'])}")
    print(f"  WR: %{st['wr']:.1f}  |  Ort kazanç: {st['avg_win']:+.1f}%  "
          f"Ort kayıp: {st['avg_loss']:.1f}%")
    print("─"*W)

    # Haftalık
    weeks = sorted(st["weekly"].keys())
    prev = initial_cap
    print("  HAFTALIK")
    for w in weeks:
        cap = st["weekly"][w]; r = (cap-prev)/prev*100 if prev else 0
        bar = "█"*min(int(abs(r)*4),40); arrow = "▲" if r>=0 else "▼"
        print(f"  {w}  ${cap:>9,.2f}  {arrow} {r:+6.1f}%  {bar}")
        prev = cap

    # Aylık
    months = sorted(st["monthly"].keys())
    prev = initial_cap
    print("─"*W)
    print("  AYLIK")
    for m in months:
        cap = st["monthly"][m]; r = (cap-prev)/prev*100 if prev else 0
        bar = "█"*min(int(abs(r)*3),45); arrow = "▲" if r>=0 else "▼"
        print(f"  {m}  ${cap:>9,.2f}  {arrow} {r:+6.1f}%  {bar}")
        prev = cap
    print("═"*W)


def print_comparison(st_a, st_b, initial_cap):
    W = 96
    print()
    print("═"*W)
    print("  KARŞILAŞTIRMA")
    print(f"  {'':40} {'Sistem A':>16} {'Sistem B':>16}")
    print(f"  {'Strateji':40} {'Baseline TP2':>16} {'V2 TP2':>16}")
    print("─"*W)
    print(f"  {'Başlangıç':40} ${initial_cap:>14,.2f} ${initial_cap:>14,.2f}")
    print(f"  {'Bitiş':40} ${st_a['final']:>14,.2f} ${st_b['final']:>14,.2f}")
    print(f"  {'Net getiri':40} {st_a['ret']:>+15.1f}% {st_b['ret']:>+15.1f}%")
    print(f"  {'Max Drawdown':40} {st_a['max_dd']:>15.1f}% {st_b['max_dd']:>15.1f}%")
    print(f"  {'Toplam sinyal':40} {len(st_a['entries']):>16} {len(st_b['entries']):>16}")
    print(f"  {'Win Rate':40} {st_a['wr']:>15.1f}% {st_b['wr']:>15.1f}%")
    print(f"  {'TP2 çıkış':40} {len(st_a['tp2']):>16} {len(st_b['tp2']):>16}")
    print(f"  {'TP1 çıkış':40} {len(st_a['tp1']):>16} {len(st_b['tp1']):>16}")
    print(f"  {'Stop':40} {len(st_a['stops']):>16} {len(st_b['stops']):>16}")
    print(f"  {'Ort kazanç/trade':40} {st_a['avg_win']:>+15.1f}% {st_b['avg_win']:>+15.1f}%")
    print(f"  {'Ort kayıp/trade':40} {st_a['avg_loss']:>+15.1f}% {st_b['avg_loss']:>+15.1f}%")
    print("═"*W)


# ─── MAIN ───────────────────────────────────────────────────────────────────
def main():
    global INITIAL_CAP, POS_SIZE, MAX_POSITIONS

    ap = argparse.ArgumentParser()
    ap.add_argument("--no-fetch", action="store_true")
    ap.add_argument("--capital",  type=float, default=INITIAL_CAP)
    ap.add_argument("--size",     type=float, default=POS_SIZE)
    ap.add_argument("--max-pos",  type=int,   default=MAX_POSITIONS)
    ap.add_argument("--coins",    nargs="*")
    args = ap.parse_args()

    INITIAL_CAP   = args.capital
    POS_SIZE      = args.size
    MAX_POSITIONS = args.max_pos
    do_fetch      = not args.no_fetch

    btc_raw = load_or_fetch("BTC/USDT") if do_fetch else load_pkl("BTC/USDT")
    if btc_raw is None:
        print("HATA: BTC/USDT verisi yok."); return
    btc_filters = compute_btc_filters(btc_raw)

    if args.coins:
        symbols = list(args.coins)
        if "BTC/USDT" not in symbols: symbols.insert(0, "BTC/USDT")
    elif args.no_fetch:
        symbols = get_cached_symbols()
    else:
        print("Binance spot USDT listesi alınıyor...")
        symbols = get_all_binance_symbols() or get_cached_symbols()

    if not symbols:
        print("Geçerli coin bulunamadı."); return

    mode = "cache" if args.no_fetch else "Binance spot"
    print(f"\n{len(symbols)} coin ({mode}) | "
          f"${INITIAL_CAP:,.0f} sermaye (her sistem bağımsız) | "
          f"${POS_SIZE:,.0f}/trade | max {MAX_POSITIONS} pozisyon")
    print(f"Sinyaller toplanıyor ({START_DATE.date()} → bugün)...")

    sigs_a, sigs_b = collect_signals(symbols, btc_filters, fetch=do_fetch)
    print(f"\nSistem A (Baseline): {len(sigs_a)} sinyal")
    print(f"Sistem B (V2):       {len(sigs_b)} sinyal")

    print("\nSistem A simüle ediliyor...")
    log_a, eq_a, _ = simulate_portfolio(sigs_a, INITIAL_CAP, POS_SIZE, MAX_POSITIONS)

    print("Sistem B simüle ediliyor...")
    log_b, eq_b, _ = simulate_portfolio(sigs_b, INITIAL_CAP, POS_SIZE, MAX_POSITIONS)

    st_a = system_stats(log_a, eq_a, INITIAL_CAP, POS_SIZE)
    st_b = system_stats(log_b, eq_b, INITIAL_CAP, POS_SIZE)

    print_system(
        "SİSTEM A — Baseline Tam TP2  (tüm BULLISH CHoCH + BTC filtresi)",
        st_a, INITIAL_CAP, POS_SIZE
    )
    print_system(
        "SİSTEM B — V2 Tam TP2  (BULLISH CHoCH + vol≥1.5x + BTC filtresi)",
        st_b, INITIAL_CAP, POS_SIZE
    )
    print_comparison(st_a, st_b, INITIAL_CAP)

    # JSON
    out = {
        "sistem_a": {
            "strateji": "Baseline Tam TP2",
            "initial": INITIAL_CAP, "final": round(st_a["final"],2),
            "return_pct": round(st_a["ret"],2), "max_dd": round(st_a["max_dd"],2),
            "trades": len(st_a["entries"]), "wr": round(st_a["wr"],1),
            "weekly": {w:round(v,2) for w,v in st_a["weekly"].items()},
            "monthly": {m:round(v,2) for m,v in st_a["monthly"].items()},
            "trade_log": log_a,
        },
        "sistem_b": {
            "strateji": "V2 (vol>=1.5x) Tam TP2",
            "initial": INITIAL_CAP, "final": round(st_b["final"],2),
            "return_pct": round(st_b["ret"],2), "max_dd": round(st_b["max_dd"],2),
            "trades": len(st_b["entries"]), "wr": round(st_b["wr"],1),
            "weekly": {w:round(v,2) for w,v in st_b["weekly"].items()},
            "monthly": {m:round(v,2) for m,v in st_b["monthly"].items()},
            "trade_log": log_b,
        },
    }
    with open("paper_results.json","w",encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    print("\n  → paper_results.json kaydedildi\n")


if __name__ == "__main__":
    main()
