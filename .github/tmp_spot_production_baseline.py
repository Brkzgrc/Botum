"""Historical replay of the production SPOT_SCANNER v11 + Portfolio lifecycle.

This file is deliberately separate from production.  It freezes the scanner blob,
replays the stateful watch/entry rules on closed candles, then applies Portfolio's
24h pre-TP1 expiry and TP1 -> 2.5% close-confirmed trailing behavior.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import timedelta, timezone

import numpy as np
import pandas as pd
import requests

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import spot_opportunity_scanner as prod

EXPECTED_SCANNER_BLOB = "65ad801bf1a9fd6011231d4f56555deff96d33b7"
BINANCE_ENDPOINTS = (
    "https://api.binance.com",
    "https://data-api.binance.vision",
)
TR_TZ = timezone(timedelta(hours=3))
DAYS = int(os.getenv("BASELINE_DAYS", "7"))
WORKERS = int(os.getenv("BASELINE_WORKERS", "8"))
FEE_SIDE_PCT = float(os.getenv("FEE_SIDE_PCT", "0.10"))
END = pd.Timestamp.now(tz="UTC").floor("15min")
START = END - pd.Timedelta(days=DAYS)
HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "Botum-production-replay/1.0"})
COLS = ["open_time","open","high","low","close","volume","close_time",
        "quote_volume","trades","taker_base","taker_quote","ignore"]
STEP_MS = {"5m":300_000,"15m":900_000,"1h":3_600_000,
           "4h":14_400_000,"1d":86_400_000}


def verify_frozen_source():
    blob = subprocess.check_output(
        ["git", "hash-object", "spot_opportunity_scanner.py"], text=True
    ).strip()
    if blob != EXPECTED_SCANNER_BLOB:
        raise RuntimeError(
            f"scanner drift: expected {EXPECTED_SCANNER_BLOB}, got {blob}; "
            "review production changes before replaying"
        )


def api(path, params=None, attempts=7):
    last = None
    for endpoint in BINANCE_ENDPOINTS:
        for i in range(attempts):
            try:
                r = HTTP.get(endpoint + path, params=params, timeout=25)
                if r.status_code == 451:
                    last = RuntimeError(f"451 from {endpoint}")
                    break
                if r.status_code in (418, 429):
                    time.sleep(min(30, 1.5 * 2**i)); continue
                r.raise_for_status()
                return r.json()
            except Exception as exc:
                last = exc
                time.sleep(min(15, .5 * 2**i))
    raise RuntimeError(f"Binance {path}: {last}")


def frame(rows):
    d = pd.DataFrame(rows, columns=COLS)
    if d.empty:
        return d
    for c in ("open","high","low","close","volume","quote_volume","taker_quote"):
        d[c] = pd.to_numeric(d[c], errors="coerce")
    d["open_time"] = pd.to_datetime(d.open_time, unit="ms", utc=True)
    d["close_time"] = pd.to_datetime(d.close_time, unit="ms", utc=True)
    return d.dropna(subset=["open","high","low","close","volume"]).drop_duplicates(
        "open_time"
    ).sort_values("open_time").reset_index(drop=True)


def fetch_klines(symbol, interval, start, end):
    cur = int(pd.Timestamp(start).timestamp() * 1000)
    end_ms = int(pd.Timestamp(end).timestamp() * 1000)
    out = []
    while cur < end_ms:
        rows = api("/api/v3/klines", {
            "symbol": symbol, "interval": interval, "startTime": cur,
            "endTime": end_ms, "limit": 1000,
        })
        if not rows:
            break
        out.extend(rows)
        nxt = int(rows[-1][0]) + STEP_MS[interval]
        if nxt <= cur:
            break
        cur = nxt
        time.sleep(.015)
    d = frame(out)
    if len(d) < 80:
        raise ValueError(f"insufficient candles {symbol} {interval}: {len(d)}")
    return d


def current_universe():
    ex = api("/api/v3/exchangeInfo")
    out = []
    for it in ex.get("symbols", []):
        sym, base = it.get("symbol", ""), it.get("baseAsset", "")
        if it.get("quoteAsset") != "USDT" or it.get("status") != "TRADING":
            continue
        if it.get("isSpotTradingAllowed") is False or base in prod.IGNORED_BASES:
            continue
        if base.endswith(prod.LEVERAGED_SUFFIXES) and base not in prod.CRYPTO_BASES_ENDING_B:
            continue
        out.append(sym)
    return sorted(out)


def parallel_fetch(symbols, interval, start, end):
    out = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        jobs = {pool.submit(fetch_klines, s, interval, start, end): s for s in symbols}
        for n, f in enumerate(as_completed(jobs), 1):
            s = jobs[f]
            try:
                out[s] = f.result()
            except Exception as exc:
                print(f"[DATA] skip {s} {interval}: {exc}", flush=True)
            if n % 50 == 0 or n == len(jobs):
                print(f"[DATA] {interval} {n}/{len(jobs)} usable={len(out)}", flush=True)
    return out


def closed_slice(d, ts, limit=240):
    if d is None or d.empty:
        return None
    i = int(d.close_time.searchsorted(ts, side="right"))
    if i < 80:
        return None
    return d.iloc[max(0, i-limit):i].reset_index(drop=True)


class SnapCache:
    def __init__(self, data):
        self.data = data
        self.cache = {}

    def get(self, symbol, interval, ts, label):
        d = self.data.get(interval, {}).get(symbol)
        x = closed_slice(d, ts, 240)
        if x is None:
            raise ValueError(f"no closed history {symbol} {interval}")
        key = (symbol, interval, int(x.open_time.iloc[-1].timestamp()))
        if key not in self.cache:
            self.cache[key] = prod.snap(x, label)
        return self.cache[key]


def prefilter(symbol, qv, ts, hourly, cache):
    try:
        d = hourly[symbol]
        x = closed_slice(d, ts, 220)
        if x is None:
            return None
        bar = int(x.open_time.iloc[-1].timestamp())
        key = (symbol, bar)
        if key in cache:
            return cache[key]
        z = prod.indicators(x); a=z.iloc[-1]; b=z.iloc[-2]
        p=prod.sf(a.close); r=prod.sf(a.rsi); sk=prod.sf(a.stoch_k); sd=prod.sf(a.stoch_d)
        sk0=prod.sf(b.stoch_k); mh=prod.sf(a.macd_hist); mh0=prod.sf(b.macd_hist)
        e20=prod.sf(a.ema20); e50=prod.sf(a.ema50); vr=prod.sf(a.vol_ratio,1)
        ret3=prod.pct(p,prod.sf(z.close.iloc[-4])); ret6=prod.pct(p,prod.sf(z.close.iloc[-7]))
        obv=prod.sf(a.obv)>=prod.sf(z.obv.iloc[-6]); near=max(0,-prod.pct(p,prod.sf(z.high.tail(20).max())))
        taker=prod.sf(z.taker_buy_ratio.tail(3).mean(),.5); c=z.close.tail(6).to_numpy(); l=z.low.tail(6).to_numpy()
        hc=sum(c[i]>c[i-1] for i in range(1,len(c))); hl=sum(l[i]>=l[i-1] for i in range(1,len(l)))
        reset=sk<=75 or prod.sf(z.stoch_k.tail(3).min())<=30; turn=sk>sd and sk>sk0
        score=(18 if p>=e50 else 0)+(12 if p>=e20 else 5)+(12 if 45<=r<=88 else 5 if 38<=r<=92 else 0)+(13 if reset else 8 if turn else 0)+(10 if mh>mh0 else 5 if mh>0 else 0)+(10 if obv else 0)+(7 if vr>=.8 else 3)+(7 if near<=7 else 3 if near<=12 else 0)+(5 if taker>=.50 else 0)+(4 if ret6>3 else 0)
        retrigger_seed=p>=e50 and 38<=r<=90 and (reset or turn) and -7<=ret3<=10
        pressure_seed=p>=e20 and 45<=r<=88 and hc>=3 and hl>=3 and near<=6 and -1<=ret3<=9 and (taker>=.50 or obv)
        seed="RETRIGGER" if retrigger_seed else "PRESSURE" if pressure_seed else ""
        if ret3>14 and sk>85 and prod.pct(p,e20)>15: score-=18
        ans=(symbol,qv,round(score,2),seed); cache[key]=ans; return ans
    except Exception:
        return None


def selected_at(ts, symbols, hourly, cache):
    rows = [r for s in symbols if (r := prefilter(s, 0.0, ts, hourly, cache))]
    rows.sort(key=lambda z:z[2], reverse=True)
    selected=list(rows[:prod.PREFILTER_CORE_N]); seen={r[0] for r in selected}
    for r in rows[prod.PREFILTER_CORE_N:]:
        if len(selected)>=prod.PYTHON_TOP_N: break
        if r[3] and r[0] not in seen: selected.append(r); seen.add(r[0])
    if len(selected)<prod.PYTHON_TOP_N:
        for r in rows[prod.PREFILTER_CORE_N:]:
            if len(selected)>=prod.PYTHON_TOP_N: break
            if r[0] not in seen: selected.append(r); seen.add(r[0])
    return [(s,q,rank) for s,q,rank,_ in selected]


@dataclass
class HistCandidate:
    symbol: str
    rank: float
    snapshot: dict
    decision: dict


def evaluate(symbol, pre_rank, regime, prior, ts, snaps):
    day=snaps.get(symbol,"1d",ts,"1D"); four=snaps.get(symbol,"4h",ts,"4H")
    one=snaps.get(symbol,"1h",ts,"1H"); fast=snaps.get(symbol,"15m",ts,"15M")
    live=fast["price"]; prior_phase=(prior or {}).get("phase"); prior_bar=int(prod.sf((prior or {}).get("last_bar_15m"),0))
    day_trend=day["price"]>=day["ema20"] and day["ema20_slope"]>=-.8 and day["rsi"]>=50
    four_trend=four["price"]>=four["ema50"] and four["ema50_slope"]>=-.2 and four["rsi"]>=45
    one_structure=one["price"]>=one["ema50"] and one["rsi"]>=38
    one_reset=one["stoch_k"]<=75 or one["stoch_min3"]<=30
    one_turn=one["stoch_k"]>one["stoch_d"] and one["stoch_k"]>one["stoch_k_prev"]
    one_mom=one["macd_hist"]>one["macd_hist_prev"] or one["obv_fast_up"] or one_turn
    audit_score=sum((four["dist_ema50"]>=6.0,four["ema20_slope"]>=1.0,one["dist_ema50"]>=2.0,one["upper_wick"]>=.22,one["stoch_k"]<=75.0,day["rsi"]>=62.0))
    audit_balanced=day_trend and four_trend and one_structure and audit_score>=5
    audit_strict=day_trend and four_trend and four["dist_ema50"]>=6 and one["dist_ema50"]>=2 and one["upper_wick"]>=.22 and one["stoch_k"]<=75
    fast_turn=fast["stoch_k"]>fast["stoch_d"] and fast["stoch_k"]>fast["stoch_k_prev"] and fast["rsi"]>=40
    fast_confirm=fast_turn and (fast["macd_hist"]>fast["macd_hist_prev"] or fast["obv_fast_up"]) and fast["price"]>=fast["ema20"]*.995
    pressure=day_trend and four_trend and audit_score>=4 and one["price"]>=one["ema20"] and one["ema20_slope"]>0 and one["rsi"]>=50 and one["higher_closes6"]>=3 and one["higher_lows6"]>=3 and one["near_high20_pct"]<=4.5
    fast_break=fast["price"]>=fast["prev_high6"]*.998 and fast["rsi"]>=48 and (fast["macd_hist"]>fast["macd_hist_prev"] or fast["obv_fast_up"])
    retrigger=audit_balanced and one_reset
    first_price=prod.sf((prior or {}).get("first_price"),live); chase=prod.pct(live,first_price) if first_price else 0
    structural_break=(day["price"]<day["ema50"] and four["price"]<four["ema50"]) or (four["price"]<four["ema50"] and one["price"]<one["ema50"] and four["ema50_slope"]<0)
    blowoff=one["ret3"]>14 and one["stoch_k"]>85 and one["dist_ema20"]>15; chased=chase>8 and not one_reset
    q=32+audit_score*8+(8 if one_turn else 0)+(7 if one_mom else 0)+(8 if fast_confirm else 0)+(8 if audit_strict else 0)
    pq=24+audit_score*8+(20 if pressure else 0)+(16 if fast_break else 0)+(5 if one["taker_buy_ratio"]>=.52 else 0)
    if regime=="RED": q-=5; pq-=5
    quality=prod.clamp(max(q if retrigger else 0,pq if pressure else 0)); decision="REDDET"; phase="NONE"; kind="NONE"
    if not structural_break and not blowoff and not chased:
        if pressure: decision="TETIK_BEKLE"; phase="PRESSURE"; kind="PRESSURE"
        if retrigger: decision="TETIK_BEKLE"; phase="COOLING" if not one_turn else "ARMED"; kind="RETRIGGER"
        eligible=bool(prior and fast["bar_id"]>prior_bar and prior_phase in {"COOLING","ARMED","PRESSURE","FORMING"})
        retrigger_ready=retrigger and one_mom and fast_confirm; pressure_ready=pressure and fast_break
        if eligible and quality>=prod.FINAL_MIN_QUALITY and (retrigger_ready or pressure_ready):
            decision="ALIM_ADAYI"; phase="ENTRY"; kind="RETRIGGER" if retrigger_ready and q>=pq else "PRESSURE"
        elif decision=="REDDET" and day_trend and four_trend and audit_score>=3:
            decision="TETIK_BEKLE"; phase="FORMING"; kind="FORMING"
    if structural_break: decision="REDDET"; phase="BROKEN"; kind="NONE"
    if blowoff or chased: decision="REDDET"; phase="LATE"; kind="NONE"
    ss={"live_price":live,"1d":day,"4h":four,"1h":one,"15m":fast,"btc_regime":regime}
    dd={"decision":decision,"confidence":round(quality,1),"state":phase,"setup_kind":kind}
    return HistCandidate(symbol,quality+min(8,max(0,pre_rank-55)*.25),ss,dd)


def btc_regime(ts, snaps):
    try:
        h=snaps.get("BTCUSDT","1h",ts,"1H"); f=snaps.get("BTCUSDT","4h",ts,"4H")
        if h["ret3"]<=-3 or f["ret3"]<=-6: return "RED"
        if h["ret3"]<-.9 or f["macd_hist"]<f["macd_hist_prev"]: return "YELLOW"
        return "GREEN"
    except Exception: return "YELLOW"


def levels(c, ts, data):
    p=c.snapshot["live_price"]; h=c.snapshot["1h"]; f=c.snapshot["4h"]
    # snap() already computed swing support/resistance from the exact last 80 bars.
    sups=[prod.sf(x) for x in h["supports"]+f["supports"] if 0<prod.sf(x)<p]
    ress=sorted(set(prod.sf(x) for x in h["resistances"]+f["resistances"] if prod.sf(x)>p))
    support=max(sups) if sups else min(h["ema20"],h["ema50"],p*.96)
    stop=support*.975; tp1=next((r for r in ress if prod.pct(r,p)>=2.5),p*1.035)
    tp2=next((r for r in ress if r>tp1 and prod.pct(r,p)>=5),max(tp1*1.02,p*1.055))
    return p,stop,tp1,tp2


def replay_entries(symbols, hourly, data):
    snaps=SnapCache(data); pre_cache={}; watch={}; day_key=None; day_count=0; signals=[]
    scans=pd.date_range(START.ceil("15min"),END,freq="15min",tz="UTC")
    pre_by_hour={}
    for n,ts in enumerate(scans,1):
        expired=[s for s,r in watch.items() if (ts-r["first_seen"]).total_seconds()>prod.WATCH_TTL_HOURS*3600]
        for s in expired: watch.pop(s,None)
        hour=ts.floor("1h")
        if hour not in pre_by_hour: pre_by_hour[hour]=selected_at(ts,symbols,hourly,pre_cache)
        pre=pre_by_hour[hour]; regime=btc_regime(ts,snaps); finals=[]; waits=[]
        for s,_,rank in pre:
            try:
                c=evaluate(s,rank,regime,watch.get(s),ts,snaps)
                (finals if c.decision["decision"]=="ALIM_ADAYI" else waits if c.decision["decision"]=="TETIK_BEKLE" else []).append(c)
            except Exception:
                pass
        finals.sort(key=lambda c:c.rank,reverse=True)
        now_epoch=ts.timestamp()
        for c in waits:
            rec=watch.get(c.symbol) or {"first_seen":ts,"first_price":c.snapshot["live_price"],"observations":0,"last_bar_15m":0}
            bar=c.snapshot["15m"]["bar_id"]
            if int(rec.get("last_bar_15m",0)) and bar>int(rec.get("last_bar_15m",0)): rec["observations"]+=1
            rec.update({"updated_at":ts,"phase":c.decision["state"],"setup_kind":c.decision["setup_kind"],"price":c.snapshot["live_price"],"score":c.decision["confidence"],"last_bar_15m":bar})
            watch[c.symbol]=rec
        tr_day=ts.tz_convert(TR_TZ).strftime("%Y-%m-%d")
        if tr_day!=day_key: day_key=tr_day; day_count=0
        for c in finals[:max(0,prod.MAX_SIGNALS_PER_DAY-day_count)]:
            entry,stop,tp1,tp2=levels(c,ts,data)
            signals.append({"entry_time":ts,"symbol":c.symbol,"kind":c.decision["setup_kind"],"score":c.decision["confidence"],"rank":c.rank,"btc_regime":regime,"entry":entry,"stop":stop,"tp1":tp1,"tp2":tp2})
            day_count+=1; watch.pop(c.symbol,None)
        if n%96==0 or n==len(scans): print(f"[REPLAY] {n}/{len(scans)} signals={len(signals)} watch={len(watch)}",flush=True)
    return pd.DataFrame(signals)


def apply_portfolio(signals):
    if signals.empty: return pd.DataFrame()
    groups={s:g.sort_values("entry_time") for s,g in signals.groupby("symbol")}
    five={}
    for n,(s,g) in enumerate(groups.items(),1):
        start=g.entry_time.min()-pd.Timedelta(minutes=5); end=END+pd.Timedelta(days=2)
        try: five[s]=fetch_klines(s,"5m",start,end)
        except Exception as exc: print(f"[5M] {s}: {exc}",flush=True)
        print(f"[5M] {n}/{len(groups)}",flush=True)
    rows=[]
    for _,sig in signals.sort_values("entry_time").iterrows():
        d=five.get(sig.symbol); peak=sig.entry; low_seen=sig.entry; tp1_hit=False
        reason="open"; exit_price=np.nan; exit_time=pd.NaT
        if d is None: continue
        path=d[d.open_time>=sig.entry_time]
        for _,bar in path.iterrows():
            peak=max(peak,float(bar.high)); low_seen=min(low_seen,float(bar.low))
            if not tp1_hit and low_seen<=sig.stop:
                reason="stop"; exit_price=sig.stop; exit_time=bar.close_time; break
            if not tp1_hit and peak>=sig.tp1: tp1_hit=True
            if tp1_hit:
                trail=max(sig.entry,peak*(1-prod.SPOT_OPPORTUNITY_TRAIL_PCT/100)) if hasattr(prod,"SPOT_OPPORTUNITY_TRAIL_PCT") else max(sig.entry,peak*.975)
                if float(bar.close)<=trail:
                    reason="trailing"; exit_price=trail; exit_time=bar.close_time; break
            elif bar.close_time-sig.entry_time>=pd.Timedelta(hours=24):
                reason="expired"; exit_price=float(bar.close); exit_time=bar.close_time; break
            if bar.close_time>=END: break
        mark=float(path[path.close_time<=END].close.iloc[-1]) if len(path[path.close_time<=END]) else sig.entry
        price=float(exit_price) if math.isfinite(float(exit_price)) else mark
        gross=(price/sig.entry-1)*100; net=gross-2*FEE_SIDE_PCT
        rows.append({**sig.to_dict(),"status":"closed" if reason!="open" else "open","reason":reason,"tp1_hit":tp1_hit,"peak":peak,"exit_time":exit_time,"exit_price":exit_price,"mark_price":mark,"gross_pct":gross,"net_pct":net})
    return pd.DataFrame(rows)


def summary(trades):
    if trades.empty:
        return pd.DataFrame([{"version":"PROD_V11_BASELINE","days":DAYS,"signals":0}])
    closed=trades[trades.status=="closed"]
    wins=closed[closed.gross_pct>0]; losses=closed[closed.gross_pct<0]
    return pd.DataFrame([{
        "version":"PROD_V11_BASELINE","scanner_blob":EXPECTED_SCANNER_BLOB,"days":DAYS,
        "start":START.isoformat(),"end":END.isoformat(),"signals":len(trades),
        "closed":len(closed),"open":int((trades.status=="open").sum()),
        "wins":len(wins),"losses":len(losses),"win_rate":100*len(wins)/max(1,len(closed)),
        "tp1_hits":int(trades.tp1_hit.sum()),"stops":int((trades.reason=="stop").sum()),
        "expired":int((trades.reason=="expired").sum()),"trailing":int((trades.reason=="trailing").sum()),
        "sum_gross_pct":trades.gross_pct.sum(),"sum_net_pct":trades.net_pct.sum(),
        "avg_gross_pct":trades.gross_pct.mean(),"avg_loss_pct":losses.gross_pct.mean() if len(losses) else np.nan,
        "avg_win_pct":wins.gross_pct.mean() if len(wins) else np.nan,
    }])


def main():
    verify_frozen_source()
    symbols=current_universe(); print(f"[START] {START} -> {END}; universe={len(symbols)}",flush=True)
    hourly=parallel_fetch(symbols,"1h",START-pd.Timedelta(days=12),END)
    symbols=sorted(hourly); hours=pd.date_range(START.ceil("1h"),END,freq="1h",tz="UTC")
    print("[STAGE] build historical prefilter union",flush=True)
    cache={}; union=set()
    for n,ts in enumerate(hours,1):
        union.update(s for s,_,_ in selected_at(ts,symbols,hourly,cache))
        if n%48==0 or n==len(hours): print(f"[PREFILTER] {n}/{len(hours)} union={len(union)}",flush=True)
    union.add("BTCUSDT"); union=sorted(union)
    data={"1h":hourly}
    data["15m"]=parallel_fetch(union,"15m",START-pd.Timedelta(days=3),END)
    data["4h"]=parallel_fetch(union,"4h",START-pd.Timedelta(days=50),END)
    data["1d"]=parallel_fetch(union,"1d",START-pd.Timedelta(days=270),END)
    usable=sorted(set(union)&set(data["15m"])&set(data["4h"])&set(data["1d"])&set(hourly))
    print(f"[STAGE] exact stateful replay usable={len(usable)}",flush=True)
    signals=replay_entries(usable,hourly,data)
    signals.to_csv("/tmp/prod_baseline_signals.csv",index=False)
    trades=apply_portfolio(signals); report=summary(trades)
    trades.to_csv("/tmp/prod_baseline_trades.csv",index=False)
    report.to_csv("/tmp/prod_baseline_summary.csv",index=False)
    with open("/tmp/prod_baseline_meta.json","w",encoding="utf-8") as f:
        json.dump({"scanner_blob":EXPECTED_SCANNER_BLOB,"days":DAYS,"universe":len(symbols),"prefilter_union":len(union),"usable":len(usable),"fee_side_pct":FEE_SIDE_PCT,"notes":["current tradable USDT universe (survivorship limitation)","historical live price approximated by latest closed 15m close","Portfolio lifecycle uses 5m candles and close-confirmed trailing"]},f,indent=2)
    print(report.to_string(index=False),flush=True)


if __name__=="__main__": main()
