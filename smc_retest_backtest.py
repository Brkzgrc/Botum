#!/usr/bin/env python3
"""
SMC Retest Entry Backtest — Çoklu Senaryo Karşılaştırması
==========================================================
CHoCH sonrası swing_high (choch_level) retestinde giriş.
vol≥5x | ROC filtresi KAPALI | Sabit TP2 yok.

Senaryolar (RETEST_H-EXPIRE_H):
  24-48 | 24-72 | 48-60 | 48-72

Mantık:
  1. CHoCH ateşlendi (close > swing_high)
  2. Hemen giriş YOK — swing_high destek olarak bekleniyor
  3. Sonraki RETEST_H içinde fiyat swing_high'a dönerse → giriş
  4. Dönmezse → sinyal expire, trade açılmaz
  5. Trade açıldıktan sonra: STOP / TRAIL (%2.5) / EXPIRE

Kullanım:
  python smc_retest_backtest.py
  python smc_retest_backtest.py --no-fetch
  python smc_retest_backtest.py --coins BTC ETH SOL
"""

import argparse, heapq, json, os, pickle, time
import datetime as _dt
import numpy as np, pandas as pd

# ─── SENARYO LİSTESİ ─────────────────────────────────────────────────────────
# Her tuple: (RETEST_H, EXPIRE_H)
SCENARIOS = [
    (24, 48),
    (24, 72),
    (48, 60),
    (48, 72),
]
# Sinyal toplama için tüm senaryoların maksimum penceresi
_MAX_FUTURE_H = max(r + e for r, e in SCENARIOS)

# ─── AYARLAR ─────────────────────────────────────────────────────────────────
DATA_DIR      = "backtest_data"
START_DATE    = pd.Timestamp("2022-01-01", tz="UTC")
INITIAL_CAP   = 5_000.0
MAX_POSITIONS = 5
FEE_RATE      = 0.001
SLIPPAGE      = 0.0005
MAX_POS_SIZE  = 20_000.0
COOLDOWN_H    = 24
CHOCH_SWING   = 5
BTC_CRASH_PCT = 3.0
SMC_TRAIL_PCT = 2.5
VOL_RATIO_MIN = 5.0

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


# ─── VERİ ────────────────────────────────────────────────────────────────────
def load_pkl(symbol):
    path = os.path.join(DATA_DIR, symbol.replace("/", "_") + ".pkl")
    if not os.path.exists(path): return None
    with open(path, "rb") as f: return pickle.load(f)

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
        with open(os.path.join(DATA_DIR, symbol.replace("/","_")+".pkl"), "wb") as f:
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
        sym = fname[:-4].replace("_", "/", 1)
        if not sym.endswith("/USDT") or sym in IGNORED_COINS: continue
        base = sym.split("/")[0]
        if any(p in base for p in LEVERAGED_PATTERNS): continue
        try:
            with open(os.path.join(DATA_DIR, fname), "rb") as f: df = pickle.load(f)
            if df is None or len(df) < 300: continue
        except Exception: continue
        symbols.append(sym)
    if "BTC/USDT" in symbols: symbols.remove("BTC/USDT")
    symbols.insert(0, "BTC/USDT")
    return symbols


# ─── BTC FİLTRELERİ ──────────────────────────────────────────────────────────
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
    crash_s     = df4["crash_ok"].reindex(idx, method="ffill").fillna(True).astype(bool)
    downtrend_s = df4["downtrend"].reindex(idx, method="ffill").fillna(False).astype(bool)
    return pd.DataFrame({"crash_ok":crash_s, "downtrend_ok":~downtrend_s}, index=idx)


# ─── CHoCH ───────────────────────────────────────────────────────────────────
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


# ─── SİNYAL TOPLAMA ──────────────────────────────────────────────────────────
def collect_signals(symbols, btc_filters, fetch=True):
    """Sinyalleri toplar. future penceresi tüm senaryoların maksimum ihtiyacına göre."""
    result = []
    for sym_i, symbol in enumerate(symbols, 1):
        if symbol == "BTC/USDT": continue
        print(f"  [{sym_i}/{len(symbols)}] {symbol}", flush=True)

        df_raw = load_or_fetch(symbol) if fetch else load_pkl(symbol)
        if df_raw is None or len(df_raw) < 300: continue

        df_raw = df_raw.copy()
        vol = df_raw["volume"]
        df_raw["vol_ratio_20"] = vol / vol.rolling(20).mean().shift(1)
        df = df_raw.dropna(subset=["vol_ratio_20"]).copy()
        if len(df) < 300: continue

        btc_al = btc_filters.reindex(df.index, method="ffill")
        bts, bds, cls_, swls = run_choch_incremental(df)

        c_arr  = df["close"].values
        volr20 = df["vol_ratio_20"].values
        n      = len(df)
        last_sent = 0.0

        for i in range(250, n-1):
            ts = df.index[i]
            if ts < START_DATE: continue
            price = c_arr[i]
            if np.isnan(price) or price <= 0: continue

            vr = float(volr20[i]) if not np.isnan(volr20[i]) else 0.0
            if vr < VOL_RATIO_MIN: continue

            if not (bts[i] == "CHoCH" and bds[i] == "BULLISH"): continue
            if not bool(btc_al["crash_ok"].iloc[i]): continue
            if not bool(btc_al["downtrend_ok"].iloc[i]): continue

            ts_h = ts.timestamp() / 3600
            if ts_h - last_sent < COOLDOWN_H: continue

            choch_lvl = cls_[i]; sw_low = swls[i]
            if not choch_lvl: continue

            entry = choch_lvl
            stop  = sw_low * 0.995 if sw_low else entry * 0.95
            if stop >= entry: stop = entry * 0.95
            risk  = max(entry - stop, entry * 0.01)
            tp1   = entry + risk

            result.append({
                "symbol":     symbol,
                "entry_time": ts,
                "entry":      entry,
                "stop":       stop,
                "tp1":        tp1,
                # _MAX_FUTURE_H ile topla — tüm senaryolar için yeterli
                "future":     df.iloc[i+1:i+1+_MAX_FUTURE_H][["high","low","close"]].copy(),
                "vol_ratio":  vr,
                "risk_pct":   round(risk / entry * 100, 2),
            })
            last_sent = ts_h

    result.sort(key=lambda x: (x["entry_time"].timestamp(), -x["vol_ratio"]))
    return result


# ─── ÇIKIŞ: RETEST + STOP / TRAIL / EXPIRE ───────────────────────────────────
def exit_trail(sig, pos_size, retest_h, expire_h):
    entry = sig["entry"]
    stop  = sig["stop"]
    tp1   = sig["tp1"]
    rows  = sig["future"]

    retest_bar = None
    for idx_i, (ts, row) in enumerate(rows.iloc[:retest_h].iterrows()):
        if float(row["low"]) <= entry:
            retest_bar = idx_i
            break

    if retest_bar is None:
        last_idx = min(retest_h - 1, len(rows) - 1)
        ret_ts = rows.index[last_idx] if len(rows) > 0 else sig["entry_time"]
        return ret_ts, 0.0, "no_retest"

    eff   = entry * (1 + SLIPPAGE)
    sp    = (stop - eff) / eff
    peak  = entry; tp1_hit = False

    trade_rows = rows.iloc[retest_bar:retest_bar + expire_h]
    for ts, row in trade_rows.iterrows():
        h = float(row["high"]); l = float(row["low"])
        if not tp1_hit:
            if l <= stop:
                return ts, pos_size * (1 + sp) * (1 - FEE_RATE), "stop"
            if h >= tp1:
                tp1_hit = True; peak = max(entry, h)
        else:
            if h > peak: peak = h
            trail = peak * (1 - SMC_TRAIL_PCT / 100)
            if l <= trail:
                trail_pct = (trail - eff) / eff
                return ts, pos_size * (1 + trail_pct) * (1 - FEE_RATE), "trail"

    if len(trade_rows) > 0:
        idx      = min(expire_h - 1, len(trade_rows) - 1)
        last_pct = (float(trade_rows.iloc[idx]["close"]) - eff) / eff
        return trade_rows.index[idx], pos_size * (1 + last_pct) * (1 - FEE_RATE), "expire"

    return sig["entry_time"], pos_size * (1 - FEE_RATE), "no_data"


# ─── PORTFÖY SİMÜLASYONU ─────────────────────────────────────────────────────
def simulate_portfolio(signals, retest_h, expire_h):
    cash=INITIAL_CAP; open_count=0; open_positions={}; max_open=0
    trade_log=[]; equity_pts=[(START_DATE, INITIAL_CAP)]
    no_retest_count = 0
    queue=[]; counter=0
    for sig in signals:
        heapq.heappush(queue, (sig["entry_time"].timestamp(), 1, counter, "signal", sig))
        counter += 1
    while queue:
        unix_ts, _, _, etype, data = heapq.heappop(queue)
        ts = pd.Timestamp(unix_ts, unit="s", tz="UTC")
        if etype == "signal":
            if open_count >= MAX_POSITIONS: continue
            remaining = MAX_POSITIONS - open_count
            pos_size  = min(cash / remaining, MAX_POS_SIZE)
            if pos_size < 1: continue
            sig = data

            exit_ts, cash_ret, label = exit_trail(sig, pos_size, retest_h, expire_h)

            if label == "no_retest":
                no_retest_count += 1
                continue

            entry_fee = pos_size * FEE_RATE
            cash -= pos_size + entry_fee
            open_count += 1
            if open_count > max_open: max_open = open_count
            trade_id = counter; counter += 1
            open_positions[trade_id] = pos_size
            heapq.heappush(queue, (exit_ts.timestamp(), 0, counter, "exit", {
                "trade_id":   trade_id,
                "symbol":     sig["symbol"],
                "entry_time": sig["entry_time"],
                "entry":      sig["entry"],
                "cash_ret":   cash_ret,
                "label":      label,
                "pos_size":   pos_size,
                "entry_fee":  entry_fee,
                "stop_pct":   round((sig["stop"] - sig["entry"]) / sig["entry"] * 100, 2),
                "tp1_pct":    round((sig["tp1"]  - sig["entry"]) / sig["entry"] * 100, 2),
                "risk_pct":   sig.get("risk_pct", 0),
            }))
            counter += 1
            equity_pts.append((ts, cash + sum(open_positions.values())))
        elif etype == "exit":
            d = data; cash += d["cash_ret"]; open_count -= 1
            open_positions.pop(d["trade_id"], None)
            net_pnl = d["cash_ret"] - (d["pos_size"] + d["entry_fee"])
            trade_log.append({
                "type":       "EXIT",
                "trade_id":   d["trade_id"],
                "symbol":     d["symbol"],
                "entry_time": str(d["entry_time"])[:16],
                "exit_time":  str(ts)[:16],
                "label":      d["label"],
                "net_pnl":    round(net_pnl, 2),
                "net_pct":    round(net_pnl / d["pos_size"] * 100, 2),
                "cash_after": round(cash, 2),
                "stop_pct":   d["stop_pct"],
                "tp1_pct":    d["tp1_pct"],
            })
            equity_pts.append((ts, cash + sum(open_positions.values())))
    return trade_log, equity_pts, max_open, no_retest_count


# ─── İSTATİSTİK ──────────────────────────────────────────────────────────────
def calc_stats(trade_log, equity_pts, max_open, no_retest_count, n_sigs):
    exits   = [t for t in trade_log if t["type"] == "EXIT"]
    trails  = [e for e in exits if e["label"] == "trail"]
    stops   = [e for e in exits if e["label"] == "stop"]
    expires = [e for e in exits if e["label"] == "expire"]

    exp_pos = [e for e in expires if e["net_pnl"] > 0]
    exp_neg = [e for e in expires if e["net_pnl"] <= 0]

    dec = trails + stops
    wr  = len(trails) / len(dec) * 100 if dec else 0.0

    final = equity_pts[-1][1] if equity_pts else INITIAL_CAP
    ret   = (final - INITIAL_CAP) / INITIAL_CAP * 100
    peak  = INITIAL_CAP; max_dd = 0.0
    for _, cap in equity_pts:
        if cap > peak: peak = cap
        dd = (cap - peak) / peak * 100
        if dd < max_dd: max_dd = dd

    avg_win  = sum(e["net_pct"] for e in trails) / len(trails) if trails else 0.0
    avg_loss = sum(e["net_pct"] for e in stops)  / len(stops)  if stops  else 0.0

    retest_oran = round((n_sigs - no_retest_count) / n_sigs * 100, 1) if n_sigs else 0.0

    return {
        "trades":           len(exits),
        "trail":            len(trails),
        "stop":             len(stops),
        "expire_toplam":    len(expires),
        "expire_pozitif":   len(exp_pos),
        "expire_negatif":   len(exp_neg),
        "expire_poz_oran":  round(len(exp_pos)/len(expires)*100, 1) if expires else 0,
        "expire_neg_oran":  round(len(exp_neg)/len(expires)*100, 1) if expires else 0,
        "expire_poz_katki": round(sum(e["net_pnl"] for e in exp_pos), 2),
        "expire_neg_katki": round(sum(e["net_pnl"] for e in exp_neg), 2),
        "no_retest":        no_retest_count,
        "retest_oran":      retest_oran,
        "wr":               round(wr, 1),
        "avg_win":          round(avg_win, 2),
        "avg_loss":         round(avg_loss, 2),
        "final":            round(final, 2),
        "ret":              round(ret, 2),
        "max_dd":           round(max_dd, 2),
        "max_open":         max_open,
    }


# ─── TERMINAL RAPOR ──────────────────────────────────────────────────────────
def print_comparison(results, n_sigs, n_coins):
    W = 100
    print("\n" + "═"*W)
    print(f"  SMC RETEST BACKTEST — SENARYO KARŞILAŞTIRMASI")
    print(f"  ${INITIAL_CAP:,.0f} başlangıç | Maks {MAX_POSITIONS} poz | ${MAX_POS_SIZE:,.0f} maks | "
          f"Komisyon %{FEE_RATE*100:.1f} | Slippage %{SLIPPAGE*100:.2f}")
    print(f"  CHoCH + vol≥{VOL_RATIO_MIN}x + BTC crash/downtrend | Cooldown: {COOLDOWN_H}H")
    print(f"  {n_coins} coin | {n_sigs} sinyal")
    print("═"*W)
    hdr = f"  {'Senaryo':<10} {'Trade':>6} {'WR%':>7} {'Trail':>6} {'Stop':>6} {'Expire':>7} {'AvgW%':>8} {'AvgL%':>8} {'MaxDD%':>8} {'Getiri%':>9} {'Son$':>10}"
    print(hdr)
    print("  " + "-"*(W-2))
    for label, st in results:
        flag = "◄" if st["ret"] == max(r["ret"] for _, r in results) else ""
        print(f"  {label:<10} {st['trades']:>6} {st['wr']:>6.1f}% {st['trail']:>6} {st['stop']:>6} "
              f"{st['expire_toplam']:>7} {st['avg_win']:>+7.2f}% {st['avg_loss']:>+7.2f}% "
              f"{st['max_dd']:>7.1f}% {st['ret']:>+8.1f}% ${st['final']:>9,.0f}  {flag}")
    print("═"*W + "\n")


# ─── HTML ÇIKTI ──────────────────────────────────────────────────────────────
CHART_COLORS = [
    ("#2a9d8f", "#2a9d8f20"),
    ("#e9c46a", "#e9c46a20"),
    ("#e76f51", "#e76f5120"),
    ("#a8dadc", "#a8dadc20"),
]

def generate_html(results, equity_map, n_sigs, n_coins):
    datasets = []
    for i, (label, st) in enumerate(results):
        eq = equity_map[label]
        step = max(1, len(eq) // 2000)
        pts  = [{"x": ts.strftime("%Y-%m-%d"), "y": round(v, 2)}
                for ts, v in eq[::step] if hasattr(ts, "strftime")]
        color, fill_color = CHART_COLORS[i % len(CHART_COLORS)]
        datasets.append({
            "label": label,
            "data": pts,
            "borderColor": color,
            "backgroundColor": fill_color,
            "borderWidth": 2,
            "pointRadius": 0,
            "fill": False,
            "tension": 0.1,
        })
    ds_js = json.dumps(datasets)

    run_date = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")

    rows_html = ""
    best_ret = max(st["ret"] for _, st in results)
    for label, st in results:
        wr_cls  = "pos" if st["wr"] >= 50 else "neg"
        ret_cls = "pos" if st["ret"] >= 0 else "neg"
        best_cls = ' style="outline:1px solid #2a9d8f"' if st["ret"] == best_ret else ""
        rows_html += f"""<tr{best_cls}>
  <td><b>{label}</b></td>
  <td>{n_sigs - st['no_retest']} (%{st['retest_oran']})</td>
  <td class="warn">{st['no_retest']}</td>
  <td>{st['trades']}</td>
  <td class="{wr_cls}">{st['wr']}%</td>
  <td class="pos">{st['avg_win']:+.2f}%</td>
  <td class="neg">{st['avg_loss']:+.2f}%</td>
  <td>{st['trail']}</td><td>{st['stop']}</td><td>{st['expire_toplam']}</td>
  <td>{st['expire_pozitif']} (%{st['expire_poz_oran']})</td>
  <td>{st['expire_negatif']} (%{st['expire_neg_oran']})</td>
  <td class="warn">{st['max_dd']:.1f}%</td>
  <td class="{ret_cls}">{st['ret']:+,.1f}%</td>
  <td><b>${st['final']:,.0f}</b></td>
</tr>"""

    return f"""<!DOCTYPE html><html lang="tr"><head>
<meta charset="UTF-8"><title>SMC Retest Backtest — Senaryo Karşılaştırması</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3.0.0/dist/chartjs-adapter-date-fns.bundle.min.js"></script>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#0d1117;color:#c9d1d9;font-family:-apple-system,monospace;padding:20px}}
h1{{color:#58a6ff;font-size:1.4rem;margin-bottom:6px}}
.meta{{color:#8b949e;font-size:0.8rem;background:#161b22;padding:10px;border-radius:6px;border:1px solid #30363d;margin-bottom:16px}}
.card{{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:14px;margin-bottom:16px;overflow-x:auto}}
table{{width:100%;border-collapse:collapse;font-size:0.78rem;min-width:900px}}
th{{background:#21262d;color:#8b949e;padding:8px 10px;text-align:left;white-space:nowrap}}
td{{padding:7px 10px;border-bottom:1px solid #21262d;white-space:nowrap}}
.pos{{color:#3fb950}}.neg{{color:#f85149}}.warn{{color:#e67e22}}
canvas{{max-height:480px}}
</style></head><body>
<h1>📊 SMC Retest Backtest — Senaryo Karşılaştırması</h1>
<div class="meta">
  {run_date} | {n_coins} coin | 1H Binance | 2022→bugün | ${INITIAL_CAP:,.0f} başlangıç<br>
  CHoCH + vol≥{VOL_RATIO_MIN}x + BTC crash/downtrend | {n_sigs} sinyal | Cooldown: {COOLDOWN_H}H<br>
  Giriş: swing_high retest | Çıkış: STOP / TRAIL (%{SMC_TRAIL_PCT}) / EXPIRE<br>
  Senaryolar — RETEST_H-EXPIRE_H: {" | ".join(f"{r}-{e}" for r,e in SCENARIOS)}
</div>

<div class="card">
<table>
<thead><tr>
  <th>Senaryo</th><th>Retest</th><th>No-Retest</th><th>Trade</th>
  <th>WR%</th><th>Ort Trail</th><th>Ort Stop</th>
  <th>TRAIL</th><th>STOP</th><th>EXPIRE</th>
  <th>Exp+</th><th>Exp-</th>
  <th>MaxDD</th><th>Getiri%</th><th>Son Sermaye</th>
</tr></thead>
<tbody>{rows_html}</tbody>
</table>
</div>

<div class="card">
<canvas id="ec"></canvas>
</div>

<script>
const ds={ds_js};
new Chart(document.getElementById('ec'),{{type:'line',data:{{datasets:ds}},options:{{
  responsive:true,animation:false,interaction:{{mode:'index',intersect:false}},
  plugins:{{
    legend:{{position:'bottom',labels:{{color:'#8b949e',boxWidth:12,font:{{size:11}}}}}},
    tooltip:{{backgroundColor:'#1c2128',borderColor:'#30363d',borderWidth:1,
      titleColor:'#c9d1d9',bodyColor:'#c9d1d9',
      callbacks:{{label:c=>` ${{c.dataset.label}}: $${{c.parsed.y.toLocaleString()}}`}}}}
  }},
  scales:{{
    x:{{type:'time',time:{{unit:'month',displayFormats:{{month:'yyyy-MM'}},tooltipFormat:'yyyy-MM-dd'}},
       ticks:{{color:'#8b949e',maxTicksLimit:16,maxRotation:0}},grid:{{color:'#21262d'}}}},
    y:{{ticks:{{color:'#8b949e',callback:v=>'$'+v.toLocaleString()}},grid:{{color:'#21262d'}},
       title:{{display:true,text:'Portföy ($)',color:'#8b949e'}}}}
  }}
}}}});
</script>
</body></html>"""


# ─── MAIN ────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-fetch", action="store_true")
    ap.add_argument("--coins", nargs="*")
    args = ap.parse_args()
    do_fetch = not args.no_fetch

    btc_raw = load_or_fetch("BTC/USDT") if do_fetch else load_pkl("BTC/USDT")
    if btc_raw is None:
        print("HATA: BTC/USDT verisi yok."); return
    print("BTC 4H filtreleri hesaplanıyor...")
    btc_filters = compute_btc_filters(btc_raw)

    if args.coins:
        symbols = list(args.coins)
        if "BTC/USDT" not in symbols: symbols.insert(0, "BTC/USDT")
    elif args.no_fetch:
        symbols = get_cached_symbols()
    else:
        print("Binance spot listesi alınıyor...")
        symbols = get_all_binance_symbols() or get_cached_symbols()

    if not symbols:
        print("Geçerli coin bulunamadı."); return

    n_coins = len([s for s in symbols if s != "BTC/USDT"])
    print(f"\n{n_coins} coin | sinyaller toplanıyor (vol≥{VOL_RATIO_MIN}x | future={_MAX_FUTURE_H}H)...\n")

    signals = collect_signals(symbols, btc_filters, fetch=do_fetch)
    n_sigs  = len(signals)
    print(f"\n{n_sigs} sinyal\n")
    if not signals:
        print("Sinyal yok."); return

    # ── Her senaryo için simülasyon ──────────────────────────────────────────
    results   = []   # [(label, stats), ...]
    equity_map = {}  # {label: equity_pts}

    for retest_h, expire_h in SCENARIOS:
        label = f"{retest_h}-{expire_h}"
        print(f"Simülasyon: RETEST={retest_h}H  EXPIRE={expire_h}H  →  {label}...")
        tlog, eq_pts, max_open, no_retest = simulate_portfolio(signals, retest_h, expire_h)
        st = calc_stats(tlog, eq_pts, max_open, no_retest, n_sigs)
        results.append((label, st))
        equity_map[label] = eq_pts

    print_comparison(results, n_sigs, n_coins)

    # ── Çıktı dosyaları ─────────────────────────────────────────────────────
    now_str = _dt.datetime.now().strftime("%Y%m%d_%H%M")
    out_dir = os.path.dirname(os.path.abspath(__file__))
    base    = f"smc_retest_compare_{now_str}"

    out_json = os.path.join(out_dir, base + ".json")
    payload  = {
        "config": {
            "scenarios": SCENARIOS,
            "vol_ratio_min": VOL_RATIO_MIN,
            "trail_pct": SMC_TRAIL_PCT,
            "cooldown_h": COOLDOWN_H,
            "initial_cap": INITIAL_CAP,
            "max_positions": MAX_POSITIONS,
            "fee_rate": FEE_RATE,
            "slippage": SLIPPAGE,
        },
        "n_coins": n_coins,
        "n_sigs": n_sigs,
        "scenarios": {label: st for label, st in results},
    }
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    print(f"✓ {out_json}")

    out_html = os.path.join(out_dir, base + ".html")
    with open(out_html, "w", encoding="utf-8") as f:
        f.write(generate_html(results, equity_map, n_sigs, n_coins))
    print(f"✓ {out_html}\n")


if __name__ == "__main__":
    main()
