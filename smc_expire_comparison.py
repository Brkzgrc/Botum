#!/usr/bin/env python3
"""
SMC Expire Suresi Karsilastirma Backtesti
==========================================
Production sistem: 16H ROC >= %7.5 + vol >= 3x + BTC crash filtresi
3 farkli expire suresi karsilastirilir: 48H / 72H / 168H

Sinyaller bir kez toplanir (168H future data), 3 simulasyon calisir.

Kullanim:
  python smc_expire_comparison.py --no-fetch
"""

import argparse, heapq, json, os, pickle
import datetime as _dt
import numpy as np, pandas as pd

DATA_DIR      = r"D:\CLAUDE\CLAUDECODE\Yeni klasör (2)\backtest_data"
OUT_DIR       = r"D:\CLAUDE\CLAUDECODE"
START_DATE    = pd.Timestamp("2022-01-01", tz="UTC")
INITIAL_CAP   = 5_000.0
MAX_POSITIONS = 5
FEE_RATE      = 0.001
SLIPPAGE      = 0.0005
MAX_POS_SIZE  = 20_000.0
COOLDOWN_H    = 24
MAX_EXPIRE_H  = 168          # future data penceresi — her zaman max alinir
CHOCH_SWING   = 5
BTC_CRASH_PCT = 3.0
SMC_TRAIL_PCT = 2.5
VOL_RATIO_MIN = 3.0
ROC_16H_MIN   = 7.5          # production degeri (SMC.py ile ayni)

EXPIRE_SCENARIOS = [48, 72, 168]


# ─── VERI ────────────────────────────────────────────────────────────────────
def load_pkl(symbol):
    path = os.path.join(DATA_DIR, symbol.replace("/", "_") + ".pkl")
    if not os.path.exists(path): return None
    with open(path, "rb") as f: return pickle.load(f)

def get_cached_symbols():
    if not os.path.isdir(DATA_DIR): return []
    symbols = []
    for fname in os.listdir(DATA_DIR):
        if not fname.endswith(".pkl"): continue
        sym = fname[:-4].replace("_", "/", 1)
        if not sym.endswith("/USDT"): continue
        base = sym.split("/")[0]
        ignored = {"UP","DOWN","BEAR","BULL","USDC","TUSD","FDUSD","DAI","USDP","USDE",
                   "UST","USD","XUSD","USD1","BFUSD","USTC","BUSD","FRAX","LUSD","GUSD",
                   "SUSD","USDS","USDX","USDD","CUSD","OUSD","MUSD","RLUSD","U",
                   "EUR","TRY","GBP","BRL","RUB","AUD","BIDR","IDRT","VAI",
                   "PAXG","XAUT","WBTC","WETH","WBNB","BETH","BTCB","HBTC"}
        lev = ["UP","DOWN","BULL","BEAR","3L","3S","2L","2S","5L","5S","10L","10S"]
        if base in ignored or any(p in base for p in lev): continue
        try:
            with open(os.path.join(DATA_DIR, fname), "rb") as f: df = pickle.load(f)
            if df is None or len(df) < 300: continue
        except Exception: continue
        symbols.append(sym)
    if "BTC/USDT" in symbols: symbols.remove("BTC/USDT")
    symbols.insert(0, "BTC/USDT")
    return symbols


# ─── BTC FILTRELERI ──────────────────────────────────────────────────────────
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


# ─── CHOCH ───────────────────────────────────────────────────────────────────
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


# ─── SINYAL TOPLAMA (production: ROC>=7.5%, vol>=3x) ────────────────────────
def collect_signals(symbols, btc_filters):
    result = []
    total = len([s for s in symbols if s != "BTC/USDT"])
    done = 0
    for symbol in symbols:
        if symbol == "BTC/USDT": continue
        df_raw = load_pkl(symbol)
        if df_raw is None or len(df_raw) < 300:
            done += 1; continue
        df_raw = df_raw.copy()
        vol = df_raw["volume"]
        df_raw["vol_ratio_20"] = vol / vol.rolling(20).mean().shift(1)
        df = df_raw.dropna(subset=["vol_ratio_20"]).copy()
        if len(df) < 300:
            done += 1; continue
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
            roc_16h = 0.0
            if i >= 16:
                prev = c_arr[i - 16]
                if prev > 0:
                    roc_16h = (price - prev) / prev * 100.0
            if roc_16h < ROC_16H_MIN: continue
            if not (bts[i] == "CHoCH" and bds[i] == "BULLISH"): continue
            if not bool(btc_al["crash_ok"].iloc[i]): continue
            if not bool(btc_al["downtrend_ok"].iloc[i]): continue
            ts_h = ts.timestamp() / 3600
            if ts_h - last_sent < COOLDOWN_H: continue
            choch_lvl = cls_[i]; sw_low = swls[i]
            entry = choch_lvl if choch_lvl else price
            stop  = sw_low * 0.995 if sw_low else entry * 0.95
            if stop >= entry: stop = entry * 0.95
            risk  = max(entry - stop, entry * 0.01)
            tp1   = entry + risk
            tp2   = entry + risk * 2
            result.append({
                "symbol":     symbol,
                "entry_time": ts,
                "entry":      entry,
                "stop":       stop,
                "tp1":        tp1,
                "tp2":        tp2,
                "future":     df.iloc[i+1:i+1+MAX_EXPIRE_H][["high","low","close"]].copy(),
                "vol_ratio":  vr,
                "roc_16h":    round(roc_16h, 2),
                "risk_pct":   round(risk / entry * 100, 2),
            })
            last_sent = ts_h
        done += 1
        if done % 50 == 0 or done == total:
            print(f"  {done}/{total} islendi", flush=True)
    result.sort(key=lambda x: (x["entry_time"].timestamp(), -x["vol_ratio"]))
    return result


# ─── CIKIS ───────────────────────────────────────────────────────────────────
def exit_full_trail(sig, pos_size, expire_h):
    entry=sig["entry"]; stop=sig["stop"]; tp1=sig["tp1"]; tp2=sig["tp2"]
    rows=sig["future"]
    eff_entry = entry * (1 + SLIPPAGE)
    sp  = (stop - eff_entry) / eff_entry
    t2p = (tp2  - eff_entry) / eff_entry
    peak = entry; tp1_hit = False
    for ts, row in rows.iloc[:expire_h].iterrows():
        h=float(row["high"]); l=float(row["low"])
        if not tp1_hit:
            if l <= stop: return ts, pos_size*(1+sp)*(1-FEE_RATE), "stop"
            if h >= tp1:  tp1_hit=True; peak=max(entry, h)
        else:
            if h > peak: peak=h
            trail = peak*(1-SMC_TRAIL_PCT/100)
            if h >= tp2:  return ts, pos_size*(1+t2p)*(1-FEE_RATE), "tp2"
            if l <= trail:
                return ts, pos_size*(1+(trail-eff_entry)/eff_entry)*(1-FEE_RATE), "trail"
    if len(rows) > 0:
        idx = min(expire_h-1, len(rows)-1)
        last_pct = (float(rows.iloc[idx]["close"]) - eff_entry) / eff_entry
        return rows.index[idx], pos_size*(1+last_pct)*(1-FEE_RATE), "expire"
    return sig["entry_time"], pos_size*(1-FEE_RATE), "no_data"


# ─── PORTFOY SIMULASYONU ─────────────────────────────────────────────────────
def simulate_portfolio(signals, expire_h):
    cash=INITIAL_CAP; open_count=0; open_positions={}; max_open=0
    trade_log=[]; equity_pts=[(START_DATE, INITIAL_CAP)]
    queue=[]; counter=0
    for sig in signals:
        heapq.heappush(queue,(sig["entry_time"].timestamp(),1,counter,"signal",sig))
        counter+=1
    while queue:
        unix_ts,_,_,etype,data = heapq.heappop(queue)
        ts = pd.Timestamp(unix_ts, unit="s", tz="UTC")
        if etype == "signal":
            if open_count >= MAX_POSITIONS: continue
            remaining_slots = MAX_POSITIONS - open_count
            pos_size = min(cash / remaining_slots, MAX_POS_SIZE)
            if pos_size < 1: continue
            sig=data; entry_fee=pos_size*FEE_RATE
            cash -= pos_size+entry_fee; open_count+=1
            if open_count > max_open: max_open=open_count
            trade_id=counter; counter+=1
            open_positions[trade_id]=pos_size
            exit_ts, cash_ret, label = exit_full_trail(sig, pos_size, expire_h)
            heapq.heappush(queue,(exit_ts.timestamp(),0,counter,"exit",{
                "trade_id":trade_id,"symbol":sig["symbol"],
                "entry_time":sig["entry_time"],"entry":sig["entry"],
                "stop":sig["stop"],"tp1":sig["tp1"],"tp2":sig["tp2"],
                "cash_ret":cash_ret,"label":label,"pos_size":pos_size,"entry_fee":entry_fee,
                "stop_pct":round((sig["stop"]-sig["entry"])/sig["entry"]*100,2),
                "tp1_pct": round((sig["tp1"]-sig["entry"])/sig["entry"]*100,2),
            })); counter+=1
            trade_log.append({"type":"ENTRY","trade_id":trade_id,"symbol":sig["symbol"],
                "time":str(ts)[:16],"entry":round(sig["entry"],6),
                "size":pos_size,"cash_after":round(cash,2),"open":open_count})
            equity_pts.append((ts, cash+sum(open_positions.values())))
        elif etype == "exit":
            d=data; cash+=d["cash_ret"]; open_count-=1
            open_positions.pop(d["trade_id"],None)
            net_pnl=d["cash_ret"]-(d["pos_size"]+d["entry_fee"])
            trade_log.append({"type":"EXIT","trade_id":d["trade_id"],"symbol":d["symbol"],
                "entry_time":str(d["entry_time"])[:16],"time":str(ts)[:16],
                "label":d["label"],"net_pnl":round(net_pnl,2),
                "net_pct":round(net_pnl/d["pos_size"]*100,2),
                "cash_after":round(cash,2),"open":open_count})
            equity_pts.append((ts, cash+sum(open_positions.values())))
    return trade_log, equity_pts, cash, max_open


# ─── ISTATISTIK ──────────────────────────────────────────────────────────────
def calc_stats(trade_log, equity_pts, max_open):
    exits   = [t for t in trade_log if t["type"]=="EXIT"]
    wins    = [e for e in exits if e["label"] in ("tp2","tp1","trail","win")]
    stops   = [e for e in exits if e["label"]=="stop"]
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
    avg_win  = sum(e["net_pct"] for e in wins)/len(wins)   if wins  else 0.0
    avg_loss = sum(e["net_pct"] for e in stops)/len(stops) if stops else 0.0
    return {"trades":len([t for t in trade_log if t["type"]=="ENTRY"]),
            "wins":len(wins),"losses":len(stops),"expires":len(expires),
            "wr":round(wr,1),"final":round(final,2),"ret":round(ret,2),
            "max_dd":round(max_dd,2),"avg_win":round(avg_win,2),"avg_loss":round(avg_loss,2),
            "max_open":max_open}


# ─── HTML ────────────────────────────────────────────────────────────────────
def generate_html(results, n_coins, n_sigs):
    PALETTE = ["#457b9d","#2a9d8f","#e9c46a"]
    rows=""; datasets=[]
    for idx, eh in enumerate(EXPIRE_SCENARIOS):
        key=f"expire_{eh}h"; r=results.get(key,{}); st=r.get("stats",{})
        trades=st.get("trades",0); wr=st.get("wr",0)
        wins=st.get("wins",0); losses=st.get("losses",0); expires=st.get("expires",0)
        final=st.get("final",INITIAL_CAP); ret=st.get("ret",0)
        avg_win=st.get("avg_win",0); avg_loss=st.get("avg_loss",0); max_dd=st.get("max_dd",0)
        color_ret="#00c853" if ret>=0 else "#d32f2f"
        wr_color="#2ecc71" if wr>=82 else ("#f39c12" if wr>=75 else "#e74c3c")
        label=f"Expire {eh}H"
        rows+=(f'<tr><td><b>{label}</b></td>'
               f'<td style="text-align:center">{trades}</td>'
               f'<td style="text-align:center;color:#2ecc71">{wins}</td>'
               f'<td style="text-align:center;color:#e74c3c">{losses}</td>'
               f'<td style="text-align:center;color:#f39c12">{expires}</td>'
               f'<td style="text-align:center;color:{wr_color};font-weight:bold">{wr:.1f}%</td>'
               f'<td style="color:#2ecc71">{avg_win:+.2f}%</td>'
               f'<td style="color:#e74c3c">{avg_loss:+.2f}%</td>'
               f'<td style="color:#e67e22">{max_dd:.1f}%</td>'
               f'<td style="color:{color_ret};font-weight:bold">{ret:+.1f}%</td>'
               f'<td style="color:{color_ret}">${final:,.0f}</td>'
               f'<td style="text-align:center"><input type="checkbox" class="tog" data-idx="{idx}" checked></td></tr>')
        eq=r.get("equity",[])
        if eq:
            pts=[{"x":ts.strftime("%Y-%m-%d"),"y":round(v,2)} for ts,v in eq if hasattr(ts,"strftime")]
            color=PALETTE[idx%len(PALETTE)]
            datasets.append(f'{{"label":{json.dumps(label)},"data":{json.dumps(pts)},'
                            f'"borderColor":"{color}","backgroundColor":"{color}20",'
                            f'"borderWidth":2,"pointRadius":0,"fill":false,"tension":0.1}}')
    ds_js="["+",".join(datasets)+"]"
    run_date=_dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    return f"""<!DOCTYPE html><html lang="tr"><head>
<meta charset="UTF-8"><title>SMC Expire Karsilastirma</title>
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
</style></head><body>
<h1>SMC Expire Suresi Karsilastirmasi (48H / 72H / 168H)</h1>
<div class="meta">
  {run_date} | {n_coins} coin | {n_sigs} sinyal | 1H Binance | 2022→bugun | ${INITIAL_CAP:,.0f} baslangic<br>
  Sistem: 16H ROC >= {ROC_16H_MIN}% + vol >= {VOL_RATIO_MIN}x + BTC crash filtresi<br>
  Cikis: TP1 hit → %{SMC_TRAIL_PCT} trailing | Cooldown: {COOLDOWN_H}H | Maks {MAX_POSITIONS} poz | ${MAX_POS_SIZE:,.0f} maks
</div>
<div class="card"><table>
<thead><tr>
  <th>Expire</th><th style="text-align:center">Trade</th>
  <th style="text-align:center">Win</th><th style="text-align:center">Loss</th><th style="text-align:center">Expire</th>
  <th style="text-align:center">WR%</th><th>Ort Kazanc</th><th>Ort Zarar</th>
  <th>MaxDD</th><th>Getiri%</th><th>Son Sermaye</th><th style="text-align:center">Graf.</th>
</tr></thead>
<tbody>{rows}</tbody></table></div>
<div class="card">
<div class="ctrl">
  <button onclick="showAll()">Tumunu Goster</button>
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
       title:{{display:true,text:'Portfoy ($)',color:'#8b949e'}}}}
  }}
}}}});
document.querySelectorAll('.tog').forEach(cb=>cb.addEventListener('change',function(){{
  ch.data.datasets[+this.dataset.idx].hidden=!this.checked;ch.update();
}}));
function showAll(){{ch.data.datasets.forEach(d=>d.hidden=false);document.querySelectorAll('.tog').forEach(c=>c.checked=true);ch.update();}}
function hideAll(){{ch.data.datasets.forEach(d=>d.hidden=true);document.querySelectorAll('.tog').forEach(c=>c.checked=false);ch.update();}}
</script></body></html>"""


# ─── MAIN ────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-fetch", action="store_true")
    args = ap.parse_args()

    btc_raw = load_pkl("BTC/USDT")
    if btc_raw is None:
        print("HATA: BTC/USDT verisi bulunamadi."); return
    print("BTC 4H filtreleri hesaplaniyor...")
    btc_filters = compute_btc_filters(btc_raw)

    symbols = get_cached_symbols()
    if not symbols:
        print("Gecerli coin bulunamadi."); return

    n_coins = len([s for s in symbols if s != "BTC/USDT"])
    print(f"\n{n_coins} coin | ROC>={ROC_16H_MIN}% + vol>={VOL_RATIO_MIN}x | Expire: {EXPIRE_SCENARIOS}\n")

    print("Sinyaller toplanıyor (bir kez, 168H future data)...")
    signals = collect_signals(symbols, btc_filters)
    print(f"\n→ {len(signals)} sinyal toplandı\n")

    if not signals:
        print("Sinyal yok, cikiliyor."); return

    print("Portfoy simulasyonlari calistiriliyor...")
    results = {}
    for eh in EXPIRE_SCENARIOS:
        key = f"expire_{eh}h"
        print(f"  {key}...", end=" ", flush=True)
        log, eq, _, max_open = simulate_portfolio(signals, eh)
        st = calc_stats(log, eq, max_open)
        results[key] = {"equity": eq, "stats": st}
        print(f"trades={st['trades']} wr={st['wr']}% ret={st['ret']:+.1f}%")

    print()
    W = 110
    print("="*W)
    print(f"  {'Expire':>10} {'Trade':>7} {'Win':>6} {'Loss':>6} {'Exp':>6} {'WR%':>6} {'MaxDD':>7} {'Getiri%':>8} {'Son $':>12}")
    print("-"*W)
    for eh in EXPIRE_SCENARIOS:
        key=f"expire_{eh}h"; st=results[key]["stats"]
        print(f"  {eh}H{'':<8} {st['trades']:7d} {st['wins']:6d} {st['losses']:6d} {st['expires']:6d} "
              f"{st['wr']:6.1f}% {st['max_dd']:7.1f}% {st['ret']:+8.1f}% ${st['final']:11,.0f}")
    print("="*W + "\n")

    os.makedirs(OUT_DIR, exist_ok=True)
    now_str  = _dt.datetime.now().strftime("%Y%m%d_%H%M")

    out_html = os.path.join(OUT_DIR, f"smc_expire_comparison_{now_str}.html")
    with open(out_html, "w", encoding="utf-8") as f:
        f.write(generate_html(results, n_coins, len(signals)))
    print(f"HTML: {out_html}")

    summary  = {f"expire_{eh}h": {"n_sigs": len(signals), "stats": results[f'expire_{eh}h']['stats']}
                for eh in EXPIRE_SCENARIOS}
    out_json = os.path.join(OUT_DIR, f"smc_expire_comparison_{now_str}.json")
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
    print(f"JSON: {out_json}\n")


if __name__ == "__main__":
    main()
