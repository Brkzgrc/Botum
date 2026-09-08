"""Audit the actual Portfolio entries, not generic scanner candidates.

Research only: reads the committed portfolio snapshot, reconstructs closed-candle
context from Binance around every real entry, and emits a per-trade entry-quality
table. It never changes scanner or Portfolio behaviour.
"""
from __future__ import annotations
import json, time
from datetime import timedelta
from pathlib import Path
import numpy as np
import pandas as pd
import requests

ROOT=Path(__file__).resolve().parents[1]
OUT=Path("/tmp")
HTTP=requests.Session()
HTTP.headers.update({"User-Agent":"Botum-portfolio-entry-audit/1.0"})
BASES=("https://api.binance.com","https://data-api.binance.vision")
COLS=["open_time","open","high","low","close","volume","close_time","quote_volume","trades","taker_base","taker_quote","ignore"]
MS={"15m":900000,"1h":3600000,"4h":14400000}

def api(path,params):
    last=None
    for base in BASES:
        for i in range(5):
            try:
                r=HTTP.get(base+path,params=params,timeout=25)
                if r.status_code in (418,429):
                    time.sleep(min(20,1.5*(2**i))); continue
                r.raise_for_status(); return r.json()
            except Exception as exc:
                last=exc; time.sleep(.5*(i+1))
    raise RuntimeError(str(last))

def fetch(symbol,interval,start,end):
    cur=int(pd.Timestamp(start).timestamp()*1000); stop=int(pd.Timestamp(end).timestamp()*1000); rows=[]
    while cur<stop:
        part=api("/api/v3/klines",{"symbol":symbol,"interval":interval,"startTime":cur,"endTime":stop,"limit":1000})
        if not part: break
        rows.extend(part); nxt=int(part[-1][0])+MS[interval]
        if nxt<=cur: break
        cur=nxt
    d=pd.DataFrame(rows,columns=COLS)
    if d.empty: return d
    for c in ("open","high","low","close","volume"): d[c]=pd.to_numeric(d[c],errors="coerce")
    d["open_time"]=pd.to_datetime(d.open_time,unit="ms",utc=True)
    d["close_time"]=pd.to_datetime(d.close_time,unit="ms",utc=True)
    return d.dropna().drop_duplicates("open_time").sort_values("open_time").reset_index(drop=True)

def ind(d):
    x=d.copy()
    x["ema20"]=x.close.ewm(span=20,adjust=False).mean()
    x["ema50"]=x.close.ewm(span=50,adjust=False).mean()
    delta=x.close.diff(); gain=delta.clip(lower=0); loss=-delta.clip(upper=0)
    ag=gain.ewm(alpha=1/14,adjust=False,min_periods=14).mean()
    al=loss.ewm(alpha=1/14,adjust=False,min_periods=14).mean()
    x["rsi"]=(100-100/(1+ag/al.replace(0,np.nan))).fillna(50)
    lo=x.rsi.rolling(14).min(); hi=x.rsi.rolling(14).max()
    x["stoch_k"]=(100*(x.rsi-lo)/(hi-lo).replace(0,np.nan)).rolling(3).mean().fillna(50)
    return x

def at(d,ts):
    x=ind(d[d.close_time<=ts].copy())
    return x.iloc[-1] if len(x)>=30 else None

def sf(v,default=np.nan):
    try: return float(v)
    except Exception: return default

def pct(a,b):
    return (a/b-1)*100 if b else np.nan

def main():
    snap=json.loads((ROOT/"portfolio_snapshot.json").read_text(encoding="utf-8"))
    items=snap.get("closed",[])+snap.get("open",[])
    rows=[]
    for n,item in enumerate(items,1):
        symbol=item["symbol"].replace("/","")
        ts=pd.Timestamp(item["open_time"]).tz_convert("UTC")
        try:
            d15=fetch(symbol,"15m",ts-pd.Timedelta(days=3),ts+pd.Timedelta(days=2))
            d1=fetch(symbol,"1h",ts-pd.Timedelta(days=20),ts+pd.Timedelta(hours=2))
            d4=fetch(symbol,"4h",ts-pd.Timedelta(days=80),ts+pd.Timedelta(hours=4))
            a15,a1,a4=at(d15,ts),at(d1,ts),at(d4,ts)
            if any(x is None for x in (a15,a1,a4)): raise ValueError("insufficient closed context")
            entry=sf(item["entry"]); tp=sf(item["tp1"]); stop=sf(item["stop"])
            hist=d15[d15.close_time<=ts].tail(96)
            high24=sf(hist.high.max()); low6=sf(hist.tail(24).low.min())
            flags=[]
            rr=pct(tp,entry)/abs(pct(stop,entry)) if entry and stop else np.nan
            if rr<.70: flags.append("RR_LT_070")
            if pct(high24,entry)<=3: flags.append("NEAR_24H_HIGH")
            if sf(a15.rsi)>=75 and sf(a15.stoch_k)>=80: flags.append("FAST_EXHAUSTION")
            if sf(a4.rsi)>=70: flags.append("4H_OVERBOUGHT")
            if sf(item.get("stop_pct"))>sf(item.get("target_pct"))*1.5: flags.append("ASYMMETRIC_RISK")
            rows.append({
                "symbol":item["symbol"],"open_time_tr":item["open_time"],"status":item["status"],
                "close_pct":sf(item.get("close_pct")),"peak_pct":sf(item.get("peak_pct")),
                "setup_kind":item.get("extra",{}).get("setup_kind"),"scanner_score":sf(item.get("extra",{}).get("score")),
                "target_pct":sf(item.get("extra",{}).get("target_pct")),"stop_pct":sf(item.get("extra",{}).get("stop_pct")),
                "rr":rr,"entry_to_24h_high_pct":pct(high24,entry),"entry_from_6h_low_pct":pct(entry,low6),
                "rsi_15m":sf(a15.rsi),"stoch_15m":sf(a15.stoch_k),"dist_ema20_15m":pct(entry,sf(a15.ema20)),
                "rsi_1h":sf(a1.rsi),"stoch_1h":sf(a1.stoch_k),"dist_ema20_1h":pct(entry,sf(a1.ema20)),
                "rsi_4h":sf(a4.rsi),"stoch_4h":sf(a4.stoch_k),"dist_ema20_4h":pct(entry,sf(a4.ema20)),
                "risk_flags":"|".join(flags),"risk_flag_count":len(flags)
            })
            print(f"[{n}/{len(items)}] {symbol} ok",flush=True)
        except Exception as exc:
            print(f"[{n}/{len(items)}] {symbol} skipped: {exc}",flush=True)
    report=pd.DataFrame(rows)
    report.to_csv(OUT/"portfolio_entry_audit.csv",index=False)
    closed=report[report.status.isin(["loss","expired","win_trail"])]
    summary=(closed.assign(loss=closed.close_pct<0)
             .groupby("risk_flag_count",dropna=False)
             .agg(trades=("symbol","size"),losses=("loss","sum"),avg_close_pct=("close_pct","mean"),avg_peak_pct=("peak_pct","mean"))
             .reset_index())
    summary.to_csv(OUT/"portfolio_entry_audit_summary.csv",index=False)
    print(summary.to_string(index=False),flush=True)

if __name__=="__main__": main()
