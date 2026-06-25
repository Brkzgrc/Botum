#!/usr/bin/env python3
"""
SMC Exit Karşılaştırma — crash_only | vol≥3.0x | p10
=====================================================
exit_half      : TP1 %50 kapat → kalan %50 için 2.5% trailing (backtest motoru)
exit_half_live : TP1 %50 kapat → kalan %50 için TP2 veya orijinal STOP (canlı sistem)

Kullanım:
  python smc_exit_compare.py --no-fetch   # mevcut cache kullan
  python smc_exit_compare.py              # Binance'den güncelle
"""

import argparse, heapq, json, os, pickle, time
import datetime as _dt
import numpy as np, pandas as pd

# ─── SABİT CONFIG ────────────────────────────────────────────────────────────
DATA_DIR      = "backtest_data"
START_DATE    = pd.Timestamp("2022-01-01", tz="UTC")
INITIAL_CAP   = 5_000.0
MAX_POSITIONS = 10
FEE_RATE      = 0.001
SLIPPAGE      = 0.0005
MAX_POS_SIZE  = 20_000.0
COOLDOWN_H    = 24
EXPIRE_H      = 168
CHOCH_SWING   = 5
MIN_VOL_24H   = 5_000_000
BTC_CRASH_PCT = 3.0
SMC_TRAIL_PCT = 2.5
VOL_THRESH    = 3.0
USE_CRASH     = True

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


# ─── BTC CRASH FİLTRESİ ──────────────────────────────────────────────────────
def compute_btc_crash(btc_1h):
    df4 = btc_1h.resample("4h", label="right", closed="right").agg(
        {"open":"first","high":"max","low":"min","close":"last","volume":"sum"}
    ).dropna(subset=["close"])
    n4 = len(df4); c4 = df4["close"].values
    crash_ok = np.ones(n4, dtype=bool)
    for i in range(1, n4):
        crash_ok[i] = (c4[i]/c4[i-1]-1)*100 > -BTC_CRASH_PCT
    df4["crash_ok"] = crash_ok
    idx = btc_1h.index
    crash_s = df4["crash_ok"].reindex(idx, method="ffill").fillna(True).astype(bool)
    return crash_s


# ─── CHoCH TESPİTİ ───────────────────────────────────────────────────────────
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


# ─── ÇIKIŞ FONKSİYONLARI ─────────────────────────────────────────────────────
def exit_half(sig, pos_size):
    """Backtest motoru: TP1 %50 kapat → kalan %50 için 2.5% trailing."""
    entry=sig["entry"]; stop=sig["stop"]; tp1=sig["tp1"]; tp2=sig["tp2"]
    rows=sig["future"]
    eff_entry = entry * (1 + SLIPPAGE)
    sp=(stop-eff_entry)/eff_entry
    t1p=(tp1-eff_entry)/eff_entry
    t2p=(tp2-eff_entry)/eff_entry
    peak=entry; tp1_hit=False
    for ts, row in rows.iloc[:EXPIRE_H].iterrows():
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
        idx=min(EXPIRE_H-1, len(rows)-1)
        last_pct=(float(rows.iloc[idx]["close"])-eff_entry)/eff_entry
        ret=(t1p+last_pct)/2 if tp1_hit else last_pct
        return rows.index[idx], pos_size*(1+ret)*(1-FEE_RATE), "expire"
    return sig["entry_time"], pos_size*(1-FEE_RATE), "no_data"


def exit_half_live(sig, pos_size):
    """Canlı sistem: TP1 %50 kapat → kalan %50 için TP2 veya orijinal STOP."""
    entry=sig["entry"]; stop=sig["stop"]; tp1=sig["tp1"]; tp2=sig["tp2"]
    rows=sig["future"]
    eff_entry = entry * (1 + SLIPPAGE)
    sp=(stop-eff_entry)/eff_entry
    t1p=(tp1-eff_entry)/eff_entry
    t2p=(tp2-eff_entry)/eff_entry
    tp1_hit=False
    for ts, row in rows.iloc[:EXPIRE_H].iterrows():
        h=float(row["high"]); l=float(row["low"])
        if not tp1_hit:
            if l<=stop: return ts, pos_size*(1+sp)*(1-FEE_RATE), "stop"
            if h>=tp1: tp1_hit=True
        else:
            if h>=tp2: return ts, pos_size*(1+(t1p+t2p)/2)*(1-FEE_RATE), "tp2"
            if l<=stop:
                return ts, pos_size*(1+(t1p+sp)/2)*(1-FEE_RATE), "half_stop"
    if len(rows)>0:
        idx=min(EXPIRE_H-1, len(rows)-1)
        last_pct=(float(rows.iloc[idx]["close"])-eff_entry)/eff_entry
        ret=(t1p+last_pct)/2 if tp1_hit else last_pct
        return rows.index[idx], pos_size*(1+ret)*(1-FEE_RATE), "expire"
    return sig["entry_time"], pos_size*(1-FEE_RATE), "no_data"


# ─── SİNYAL TOPLAMA ──────────────────────────────────────────────────────────
def collect_signals(symbols, crash_series, fetch=True):
    sigs = []
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

        crash_al = crash_series.reindex(df.index, method="ffill").fillna(True)
        bts, bds, cls_, swls = run_choch_incremental(df)

        c_arr  = df["close"].values
        vol24  = df["vol_24h_usd"].values
        volr20 = df["vol_ratio_20"].values
        n = len(df)
        last_ts_h = 0.0

        for i in range(250, n-1):
            ts = df.index[i]
            if ts < START_DATE: continue
            price = c_arr[i]
            if np.isnan(price) or price <= 0: continue
            if np.isnan(vol24[i]) or vol24[i] < MIN_VOL_24H: continue
            if not (bts[i] == "CHoCH" and bds[i] == "BULLISH"): continue
            vr = float(volr20[i]) if not np.isnan(volr20[i]) else 0.0
            if vr < VOL_THRESH: continue
            if USE_CRASH and not bool(crash_al.iloc[i]): continue
            ts_h = ts.timestamp() / 3600
            if ts_h - last_ts_h < COOLDOWN_H: continue
            last_ts_h = ts_h

            choch_lvl = cls_[i]; sw_low = swls[i]
            entry = choch_lvl if choch_lvl else price
            stop  = sw_low * 0.995 if sw_low else entry * 0.95
            if stop >= entry: stop = entry * 0.95
            risk  = max(entry - stop, entry * 0.01)
            tp1   = entry + risk
            tp2   = entry + risk * 2

            future = df.iloc[i+1:i+1+EXPIRE_H][["high","low","close"]]
            sigs.append({
                "symbol":     symbol,
                "entry_time": ts,
                "entry":      entry,
                "stop":       stop,
                "tp1":        tp1,
                "tp2":        tp2,
                "future":     future,
                "vol_ratio":  vr,
                "risk_pct":   round(risk / entry * 100, 2),
            })

    sigs.sort(key=lambda x: x["entry_time"].timestamp())
    return sigs


# ─── PORTFÖY SİMÜLASYONU ─────────────────────────────────────────────────────
def simulate_portfolio(signals, exit_fn):
    cash=INITIAL_CAP; open_count=0; open_positions={}; max_open=0
    trade_log=[]; equity_pts=[(START_DATE, INITIAL_CAP)]
    queue=[]; counter=0
    for sig in signals:
        heapq.heappush(queue, (sig["entry_time"].timestamp(), 1, counter, "signal", sig))
        counter+=1
    while queue:
        unix_ts,_,_,etype,data = heapq.heappop(queue)
        ts = pd.Timestamp(unix_ts, unit="s", tz="UTC")
        if etype == "signal":
            if open_count >= MAX_POSITIONS: continue
            pos_size = min(cash / MAX_POSITIONS, MAX_POS_SIZE)
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
                "trade_id": trade_id,
                "cash_ret": cash_ret,
                "pos_size": pos_size,
                "entry_fee": entry_fee,
                "label":    label,
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
    return trade_log, equity_pts, max_open


# ─── İSTATİSTİK ──────────────────────────────────────────────────────────────
def compute_stats(trade_log, equity_pts):
    by_label = {}
    for t in trade_log:
        lbl = t["label"]
        if lbl not in by_label: by_label[lbl] = {"count": 0, "pnl": 0.0}
        by_label[lbl]["count"] += 1
        by_label[lbl]["pnl"]   += t["net_pnl"] / t["pos_size"] * 100

    wins  = [t for t in trade_log if t["label"] in ("tp2", "trail")]
    stops = [t for t in trade_log if t["label"] in ("stop", "half_stop")]
    exps  = [t for t in trade_log if t["label"] == "expire"]
    dec   = len(wins) + len(stops)
    wr    = len(wins) / dec * 100 if dec > 0 else 0.0

    final = equity_pts[-1][1] if equity_pts else INITIAL_CAP
    ret   = (final - INITIAL_CAP) / INITIAL_CAP * 100
    peak  = INITIAL_CAP; max_dd = 0.0
    for _, cap in equity_pts:
        if cap > peak: peak = cap
        dd = (cap - peak) / peak * 100
        if dd < max_dd: max_dd = dd

    avg_win  = sum(t["net_pnl"]/t["pos_size"]*100 for t in wins)  / len(wins)  if wins  else 0.0
    avg_loss = sum(t["net_pnl"]/t["pos_size"]*100 for t in stops) / len(stops) if stops else 0.0

    return {
        "final":    round(final, 2),
        "ret":      round(ret, 2),
        "max_dd":   round(max_dd, 2),
        "wr":       round(wr, 1),
        "trades":   len(trade_log),
        "wins":     len(wins),
        "stops":    len(stops),
        "expires":  len(exps),
        "avg_win":  round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "by_label": {k: {"count": v["count"], "avg_pnl": round(v["pnl"]/v["count"],2) if v["count"] else 0}
                     for k, v in by_label.items()},
    }


# ─── HTML ÇIKTI ──────────────────────────────────────────────────────────────
LABEL_NAMES = {
    "stop":      "Stop (TP1 öncesi)",
    "tp2":       "TP1+TP2 ✅",
    "trail":     "TP1+Trail ✅",
    "half_stop": "TP1+Stop (half)",
    "expire":    "Expire",
    "no_data":   "No data",
}

def generate_html(res_half, res_live, eq_half, eq_live, n_sigs, n_coins):
    run_date = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")

    def metric_row(label, v_half, v_live, fmt="{}", higher_better=True):
        try:
            vh = float(str(v_half).replace("$","").replace(",","").replace("%",""))
            vl = float(str(v_live).replace("$","").replace(",","").replace("%",""))
        except Exception:
            return f"<tr><td>{label}</td><td>{v_half}</td><td>{v_live}</td><td>—</td></tr>"
        diff = vl - vh
        if higher_better:
            winner = "live" if vl > vh else ("half" if vh > vl else "tie")
        else:
            winner = "live" if vl < vh else ("half" if vh < vl else "tie")
        badge = {"live": "🟢 Live", "half": "🟡 Half", "tie": "—"}.get(winner, "—")
        return (f"<tr><td>{label}</td>"
                f"<td>{fmt.format(v_half)}</td>"
                f"<td>{fmt.format(v_live)}</td>"
                f"<td>{badge} ({diff:+.2f})</td></tr>")

    # Karşılaştırma satırları
    rows = (
        metric_row("Son Sermaye", f"${res_half['final']:,.0f}", f"${res_live['final']:,.0f}", "{}", True) +
        metric_row("Getiri %", res_half["ret"], res_live["ret"], "{:+.1f}%", True) +
        metric_row("Max Drawdown", res_half["max_dd"], res_live["max_dd"], "{:.1f}%", False) +
        metric_row("Win Rate %", res_half["wr"], res_live["wr"], "{:.1f}%", True) +
        metric_row("Toplam Trade", res_half["trades"], res_live["trades"], "{}", True) +
        metric_row("Kazanan", res_half["wins"], res_live["wins"], "{}", True) +
        metric_row("Durdurma", res_half["stops"], res_live["stops"], "{}", False) +
        metric_row("Expire", res_half["expires"], res_live["expires"], "{}", None) +
        metric_row("Ort Kazanç %", res_half["avg_win"], res_live["avg_win"], "{:+.2f}%", True) +
        metric_row("Ort Kayıp %", res_half["avg_loss"], res_live["avg_loss"], "{:+.2f}%", False)
    )

    # Exit dağılımı tabloları
    def label_table(by_label):
        rows = ""
        for lbl, d in sorted(by_label.items(), key=lambda x: -x[1]["count"]):
            name = LABEL_NAMES.get(lbl, lbl)
            rows += f"<tr><td>{name}</td><td>{d['count']}</td><td>{d['avg_pnl']:+.2f}%</td></tr>"
        return f"<table><thead><tr><th>Sonuç</th><th>Adet</th><th>Ort PnL</th></tr></thead><tbody>{rows}</tbody></table>"

    lbl_half = label_table(res_half["by_label"])
    lbl_live = label_table(res_live["by_label"])

    # Equity curve
    def eq_pts(eq):
        return [{"x": ts.strftime("%Y-%m-%d"), "y": round(v, 2)}
                for ts, v in eq if hasattr(ts, "strftime")]

    pts_half = json.dumps(eq_pts(eq_half))
    pts_live = json.dumps(eq_pts(eq_live))

    return f"""<!DOCTYPE html><html lang="tr"><head>
<meta charset="UTF-8"><title>SMC Exit Karşılaştırma</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#0d1117;color:#c9d1d9;font-family:-apple-system,monospace;padding:20px}}
h1{{color:#58a6ff;font-size:1.3rem;margin-bottom:6px}}
h2{{color:#8b949e;font-size:0.9rem;margin:16px 0 8px}}
.meta{{color:#8b949e;font-size:0.78rem;background:#161b22;padding:10px;border-radius:6px;
       border:1px solid #30363d;margin-bottom:16px}}
.card{{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:14px;
       margin-bottom:16px;overflow-x:auto}}
.grid2{{display:grid;grid-template-columns:1fr 1fr;gap:12px}}
@media(max-width:700px){{.grid2{{grid-template-columns:1fr}}}}
table{{width:100%;border-collapse:collapse;font-size:0.82rem}}
th{{background:#21262d;color:#8b949e;padding:8px 10px;text-align:left}}
td{{padding:7px 10px;border-bottom:1px solid #21262d}}
tr:hover td{{background:#1c2128}}
canvas{{max-height:420px}}
.badge-h{{color:#ffd166;font-weight:700}}
.badge-l{{color:#06d6a0;font-weight:700}}
.col-half{{color:#ffd166;font-weight:700}}
.col-live{{color:#06d6a0;font-weight:700}}
</style></head><body>
<h1>📊 SMC Exit Karşılaştırma</h1>
<div class="meta">
  {run_date} | {n_coins} coin | crash_only filtresi | vol≥{VOL_THRESH:.0f}x | p{MAX_POSITIONS} |
  {n_sigs} sinyal | ${INITIAL_CAP:,.0f} başlangıç | Komisyon %{FEE_RATE*100:.1f} | Slippage %{SLIPPAGE*100:.2f}
</div>

<h2>Karşılaştırma</h2>
<div class="card">
<table><thead><tr>
  <th>Metrik</th>
  <th class="col-half">exit_half (2.5% Trail)</th>
  <th class="col-live">exit_half_live (STOP bekler)</th>
  <th>Fark (Live − Half)</th>
</tr></thead><tbody>
{rows}
</tbody></table></div>

<h2>Exit Dağılımı</h2>
<div class="grid2">
  <div class="card">
    <div style="color:#ffd166;font-weight:700;margin-bottom:8px">exit_half — 2.5% Trailing</div>
    {lbl_half}
  </div>
  <div class="card">
    <div style="color:#06d6a0;font-weight:700;margin-bottom:8px">exit_half_live — Canlı Sistem</div>
    {lbl_live}
  </div>
</div>

<h2>Equity Curve</h2>
<div class="card"><canvas id="ec"></canvas></div>

<script>
const ds=[
  {{label:"exit_half (Trail %2.5)",data:{pts_half},borderColor:"#ffd166",
   backgroundColor:"#ffd16622",borderWidth:2,pointRadius:0,fill:true,tension:0.1}},
  {{label:"exit_half_live (Canlı)",data:{pts_live},borderColor:"#06d6a0",
   backgroundColor:"#06d6a022",borderWidth:2,pointRadius:0,fill:true,tension:0.1}}
];
new Chart(document.getElementById('ec'),{{type:'line',data:{{datasets:ds}},options:{{
  responsive:true,animation:false,interaction:{{mode:'index',intersect:false}},
  plugins:{{legend:{{display:true,labels:{{color:'#c9d1d9',font:{{size:12}}}}}},
    tooltip:{{backgroundColor:'#1c2128',borderColor:'#30363d',borderWidth:1,
      titleColor:'#c9d1d9',bodyColor:'#c9d1d9',
      callbacks:{{label:c=>` ${{c.dataset.label}}: $${{c.parsed.y.toLocaleString()}}`}}}}}},
  scales:{{
    x:{{type:'category',ticks:{{color:'#8b949e',maxTicksLimit:14,maxRotation:0}},grid:{{color:'#21262d'}}}},
    y:{{ticks:{{color:'#8b949e',callback:v=>'$'+v.toLocaleString()}},grid:{{color:'#21262d'}},
       title:{{display:true,text:'Portföy ($)',color:'#8b949e'}}}}
  }}
}}}});
</script></body></html>"""


# ─── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-fetch", action="store_true")
    args = ap.parse_args()
    do_fetch = not args.no_fetch

    btc_raw = load_or_fetch("BTC/USDT") if do_fetch else load_pkl("BTC/USDT")
    if btc_raw is None:
        print("HATA: BTC/USDT verisi yok."); return

    print("BTC crash filtresi hesaplanıyor...")
    crash_series = compute_btc_crash(btc_raw)

    if args.no_fetch:
        symbols = get_cached_symbols()
    else:
        print("Binance spot listesi alınıyor...")
        symbols = get_all_binance_symbols() or get_cached_symbols()

    if not symbols:
        print("Geçerli coin bulunamadı."); return

    print(f"\n{len(symbols)} coin | crash_only | vol≥{VOL_THRESH}x | p{MAX_POSITIONS}")
    print(f"Sinyaller toplanıyor ({START_DATE.date()} → bugün)...\n")

    sigs = collect_signals(symbols, crash_series, fetch=do_fetch)
    print(f"\n{len(sigs)} sinyal toplandı.")

    print("\nPortföy simülasyonları...")
    log_half, eq_half, _ = simulate_portfolio(sigs, exit_half)
    log_live, eq_live, _ = simulate_portfolio(sigs, exit_half_live)

    res_half = compute_stats(log_half, eq_half)
    res_live = compute_stats(log_live, eq_live)

    print("\n" + "═"*60)
    print(f"  {'':30} {'Half+Trail':>14} {'Live+Stop':>14}")
    print("═"*60)
    for lbl, kh, kl, fmt in [
        ("Son Sermaye",  "final",   "final",   "${:,.0f}"),
        ("Getiri %",     "ret",     "ret",     "{:+.1f}%"),
        ("Max DD",       "max_dd",  "max_dd",  "{:.1f}%"),
        ("Win Rate",     "wr",      "wr",      "{:.1f}%"),
        ("Ort Kazanç",   "avg_win", "avg_win", "{:+.2f}%"),
        ("Ort Kayıp",    "avg_loss","avg_loss","{:+.2f}%"),
        ("Toplam Trade", "trades",  "trades",  "{}"),
    ]:
        vh = fmt.format(res_half[kh])
        vl = fmt.format(res_live[kl])
        print(f"  {lbl:<30} {vh:>14} {vl:>14}")
    print("═"*60)

    print("\nExit dağılımı (half):")
    for lbl, d in sorted(res_half["by_label"].items(), key=lambda x: -x[1]["count"]):
        print(f"  {LABEL_NAMES.get(lbl,lbl):<25}: {d['count']:>5}  ort {d['avg_pnl']:+.2f}%")
    print("\nExit dağılımı (live):")
    for lbl, d in sorted(res_live["by_label"].items(), key=lambda x: -x[1]["count"]):
        print(f"  {LABEL_NAMES.get(lbl,lbl):<25}: {d['count']:>5}  ort {d['avg_pnl']:+.2f}%")

    now     = _dt.datetime.now()
    now_str = now.strftime("%Y%m%d_%H%M")
    start_s = START_DATE.strftime("%Y%m%d")
    end_s   = now.strftime("%Y%m%d")
    base    = f"smc_exit_compare_{start_s}_{end_s}_{now_str}"

    out_json = base + ".json"
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump({"half": res_half, "live": res_live, "n_sigs": len(sigs)},
                  f, indent=2, ensure_ascii=False)
    print(f"\n✓ {out_json}")

    out_html = base + ".html"
    with open(out_html, "w", encoding="utf-8") as f:
        f.write(generate_html(res_half, res_live, eq_half, eq_live, len(sigs), len(symbols)))
    print(f"✓ {out_html}\n")


if __name__ == "__main__":
    main()
