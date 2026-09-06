"""Exploratory 15m StochRSI timing audit for frozen production-v11 signals.

Does not alter production.  It measures the StochRSI state at each already
emitted signal, then compares small *entry-timing-only* filters on the same
single-position $2,500 ledger.  Results are diagnostics, not optimisation.
"""
from __future__ import annotations

import os, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pandas as pd
import requests

sys.path.insert(0, os.path.abspath("."))
import spot_opportunity_scanner as prod

SRC = Path(".github/tmp_prod_baseline_trades.csv")
BASE = "https://data-api.binance.vision"
HTTP = requests.Session()
STEP = 900_000
COLS = ["open_time","open","high","low","close","volume","close_time",
        "quote_volume","trades","taker_base","taker_quote","ignore"]

def get(symbol, start, end):
    cur=int(pd.Timestamp(start).timestamp()*1000); stop=int(pd.Timestamp(end).timestamp()*1000); rows=[]
    while cur<stop:
        r=HTTP.get(BASE+"/api/v3/klines",params={"symbol":symbol,"interval":"15m","startTime":cur,"endTime":stop,"limit":1000},timeout=30)
        r.raise_for_status(); q=r.json()
        if not q: break
        rows.extend(q); nxt=int(q[-1][0])+STEP
        if nxt<=cur: break
        cur=nxt
    d=pd.DataFrame(rows,columns=COLS)
    for c in ("open","high","low","close","volume","quote_volume","taker_quote"): d[c]=pd.to_numeric(d[c],errors="coerce")
    d.open_time=pd.to_datetime(d.open_time,unit="ms",utc=True); d.close_time=pd.to_datetime(d.close_time,unit="ms",utc=True)
    return d.dropna().drop_duplicates("open_time").sort_values("open_time").reset_index(drop=True)

def features(symbol, entries):
    start=entries.min()-pd.Timedelta(days=4); end=entries.max()+pd.Timedelta(minutes=15)
    d=get(symbol,start,end); z=prod.indicators(d); out=[]
    for ts in entries:
        q=z[z.close_time<=ts]
        if len(q)<80: continue
        x=q.iloc[-1]; p=q.iloc[-2]
        out.append({"entry_time":ts,"symbol":symbol,"stoch_k":float(x.stoch_k),"stoch_d":float(x.stoch_d),
                    "stoch_prev":float(p.stoch_k),"stoch_min3":float(q.stoch_k.tail(3).min()),
                    "rsi15":float(x.rsi),"macd_up":bool(x.macd_hist>p.macd_hist),
                    "reclaim_ema20":bool(x.close>=x.ema20*.995),"k_slope":float(x.stoch_k-p.stoch_k)})
    return out

def ledger(d, name):
    cap=2500.; available=pd.Timestamp.min.tz_localize("UTC"); rows=[]
    for ts,g in d.groupby("entry_time",sort=True):
        x=g.sort_values(["rank","rr"],ascending=False,kind="stable").iloc[0]
        if ts<available: continue
        ret=float(x.net_pct); before=cap; cap*=1+ret/100
        closed=x.status=="closed" and pd.notna(x.exit_time)
        available=x.exit_time if closed else pd.Timestamp.max.tz_localize("UTC")
        rows.append([name,ts,x.symbol,x.kind,x.stoch_k,x.stoch_min3,ret,x.reason,cap-before,cap])
    q=pd.DataFrame(rows,columns=["variant","entry_time","symbol","kind","stoch_k","stoch_min3","net_pct","reason","pnl_usdt","capital_after"])
    return {"variant":name,"trades":len(q),"end_capital":cap,"return_pct":(cap/2500-1)*100,
            "wins":int((q.net_pct>0).sum()),"losses":int((q.net_pct<0).sum())},q

def main():
    d=pd.read_csv(SRC,parse_dates=["entry_time","exit_time"])
    d.entry_time=pd.to_datetime(d.entry_time,utc=True); d.exit_time=pd.to_datetime(d.exit_time,utc=True,errors="coerce")
    d["rr"]=(d.tp1-d.entry)/(d.entry-d.stop)
    parts=[]; by=d.groupby("symbol")["entry_time"].apply(list).to_dict()
    with ThreadPoolExecutor(max_workers=6) as ex:
        fs={ex.submit(features,s,pd.Series(ts)):s for s,ts in by.items()}
        for i,f in enumerate(as_completed(fs),1):
            try: parts.extend(f.result())
            except Exception as e: print("SKIP",fs[f],e,flush=True)
            print(f"[STOCH] {i}/{len(fs)}",flush=True)
    feat=pd.DataFrame(parts); x=d.merge(feat,on=["entry_time","symbol"],how="inner")
    closed=x[x.status=="closed"].copy(); closed["outcome"]=np.where(closed.net_pct>0,"WIN","LOSS")
    variants={
      "CURRENT": x,
      "LOW_STOCH_K40": x[x.stoch_k<=40],
      "RETRIGGER_DEEP_RESET": x[(x.kind!="RETRIGGER") | (x.stoch_min3<=30)],
      "NO_LATE_STOCH": x[x.stoch_k<=70],
      "DEEP_RETRIGGER_PLUS_PRESSURE": x[((x.kind=="RETRIGGER")&(x.stoch_min3<=30)) | ((x.kind=="PRESSURE")&(x.stoch_k<=65))],
    }
    summary=[]; ledgers=[]
    for n,q in variants.items():
        s,l=ledger(q,n); summary.append(s); ledgers.append(l)
    audit=closed.groupby("outcome").agg(count=("symbol","size"),avg_k=("stoch_k","mean"),avg_min3=("stoch_min3","mean"),avg_slope=("k_slope","mean"),avg_net=("net_pct","mean")).reset_index()
    pd.DataFrame(summary).to_csv("/tmp/prod_stoch_summary.csv",index=False)
    pd.concat(ledgers,ignore_index=True).to_csv("/tmp/prod_stoch_ledgers.csv",index=False)
    x.to_csv("/tmp/prod_stoch_entries.csv",index=False); audit.to_csv("/tmp/prod_stoch_audit.csv",index=False)
    print(pd.DataFrame(summary).to_string(index=False)); print(audit.to_string(index=False))

if __name__=="__main__": main()
