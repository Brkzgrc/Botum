# -*- coding: utf-8 -*-
"""15M-primary + 1H-confirmation scanner icin walk-forward backtest.

Canli scanner ile ayni akisi izler:
  tum secili semboller 15M'de taranir -> sadece 15M adaylar 1H teyide gider.
Sinyal kapanmis mumdan uretilir; giris sonraki 15M mum acilisidir.
"""
from __future__ import annotations
import argparse, json, statistics, time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
import pandas as pd
import spot_opportunity_scanner as scanner

INTERVAL_MS={"15m":900_000,"1h":3_600_000}; FRAME_LIMITS={"15m":260,"1h":260}
def utc_ms(dt): return int(dt.timestamp()*1000)
def closed_quarter():
    now=datetime.now(timezone.utc); m=(now.minute//15)*15; return now.replace(minute=m,second=0,microsecond=0)
def raw_to_df(rows):
    df=pd.DataFrame(rows,columns=["open_time","open","high","low","close","volume","close_time","quote_volume","trades","taker_base","taker_quote","ignore"])
    for c in ("open","high","low","close","volume","quote_volume","taker_quote"):df[c]=pd.to_numeric(df[c],errors="coerce")
    df["open_time"]=pd.to_datetime(df.open_time,unit="ms",utc=True); df["close_time"]=pd.to_datetime(df.close_time,unit="ms",utc=True)
    return df.dropna(subset=["open","high","low","close","volume"]).reset_index(drop=True)
def fetch_range(symbol,interval,start,end):
    rows=[]; cur=utc_ms(start); endms=utc_ms(end)
    while cur<endms:
        b=scanner.api_get("/api/v3/klines",{"symbol":symbol,"interval":interval,"startTime":cur,"endTime":endms-1,"limit":1000})
        if not b:break
        rows.extend(b); nxt=int(b[-1][6])+1
        if nxt<=cur:break
        cur=nxt
        if len(b)==1000:time.sleep(.03)
    if not rows:raise ValueError(f"Veri yok {symbol} {interval}")
    u={int(r[0]):r for r in rows}; return raw_to_df([u[k] for k in sorted(u)])
def frame_at(df,cutoff,limit):
    s=df[df.close_time<cutoff].tail(limit).copy()
    if len(s)<60:raise ValueError("Yetersiz kapanmis mum")
    return s.reset_index(drop=True)
def download_symbol(symbol,first,end):
    out={}
    for interval,limit in FRAME_LIMITS.items():
        warm=timedelta(milliseconds=INTERVAL_MS[interval]*(limit+20)); out[interval]=fetch_range(symbol,interval,first-warm,end)
    return out
@contextmanager
def historical_fetch(all_data,cutoff):
    orig=scanner.fetch_ohlcv
    def repl(symbol,interval,limit=260):
        if symbol not in all_data or interval not in all_data[symbol]:raise ValueError(f"Backtest verisi yok {symbol} {interval}")
        return frame_at(all_data[symbol][interval],cutoff,limit)
    scanner.fetch_ohlcv=repl
    try:yield
    finally:scanner.fetch_ohlcv=orig
def current_symbols(limit):
    syms=[s for s,_ in scanner.get_spot_universe()]; return syms if limit<=0 else syms[:limit]
def historical_quote_volume(h1,cutoff):
    d=h1[h1.close_time<cutoff].tail(24); return float(d.quote_volume.sum()) if not d.empty else 0.0
def next_bar_open(data,symbol,cutoff):
    r=data[symbol]["15m"]; row=r[r.open_time==cutoff]
    if row.empty:return None
    x=float(row.iloc[0].open); return x if x>0 else None
def mark_to_market(pos,data,cutoff):
    v=0.0
    for s,p in pos.items():
        r=data[s]["15m"]; z=r[r.close_time<cutoff]; price=float(z.close.iloc[-1]) if not z.empty else p["entry"]; v+=p["qty"]*price
    return v
def close_trade(symbol,pos,exit_price,reason,cutoff,cash,fee,slip,records):
    eff=exit_price*(1-slip); gross=pos["qty"]*eff; ef=gross*fee; cash+=gross-ef; pnl=(eff-pos["effective_entry"])*pos["qty"]-pos["entry_fee"]-ef; pct=pnl/pos["capital_used"]*100 if pos["capital_used"] else 0
    records.append({"symbol":symbol,"entry_time":pos["entry_time"].isoformat(),"exit_time":cutoff.isoformat(),"signal_price":round(pos["signal_price"],10),"entry":round(pos["entry"],10),"exit":round(exit_price,10),"stop":round(pos["stop"],10),"target":round(pos["target"],10),"reason":reason,"net_pnl":round(pnl,2),"net_pct":round(pct,3),"entry_score":pos["entry_score"],"m15_score":pos["m15_score"],"setup":pos["setup"],"btc_regime":pos["btc_regime"],"stop_pct":pos["stop_pct"],"target_pct":pos["target_pct"],"rr":pos["rr"]})
    return cash
def update_open_positions(pos,data,cutoff,cash,fee,slip,records):
    bar_open=cutoff-timedelta(minutes=15); closing=[]
    for s,p in pos.items():
        r=data[s]["15m"]; row=r[r.open_time==bar_open]
        if row.empty:continue
        x=row.iloc[-1]; hs=float(x.low)<=p["stop"]; ht=float(x.high)>=p["target"]
        if hs and ht:closing.append((s,p["stop"],"STOP_AMBIGUOUS"))
        elif hs:closing.append((s,p["stop"],"STOP"))
        elif ht:closing.append((s,p["target"],"TARGET"))
    for s,price,reason in closing:cash=close_trade(s,pos.pop(s),price,reason,cutoff,cash,fee,slip,records)
    return cash
def summarize(records,curve,start):
    wins=[r for r in records if r["net_pnl"]>0]; losses=[r for r in records if r["net_pnl"]<=0]; end=curve[-1]["equity"] if curve else start; peak=start; dd=0
    for p in curve:peak=max(peak,p["equity"]); dd=min(dd,(p["equity"]/peak-1)*100 if peak else 0)
    gw=sum(r["net_pnl"] for r in wins); gl=abs(sum(r["net_pnl"] for r in losses))
    return {"start_equity":round(start,2),"end_equity":round(end,2),"net_pnl":round(end-start,2),"return_pct":round((end/start-1)*100,2),"closed_trades":len(records),"wins":len(wins),"losses":len(losses),"win_rate_pct":round(100*len(wins)/len(records),2) if records else 0,"avg_win":round(statistics.fmean([r["net_pnl"] for r in wins]),2) if wins else 0,"avg_loss":round(statistics.fmean([r["net_pnl"] for r in losses]),2) if losses else 0,"profit_factor":round(gw/gl,3) if gl else None,"max_drawdown_pct":round(dd,2)}
def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--days",type=int,default=14); ap.add_argument("--symbols",type=int,default=20); ap.add_argument("--account",type=float,default=10000); ap.add_argument("--risk-pct",type=float,default=1.25); ap.add_argument("--max-position-pct",type=float,default=40); ap.add_argument("--max-open",type=int,default=4); ap.add_argument("--fee-pct",type=float,default=.10); ap.add_argument("--slippage-pct",type=float,default=.05); ap.add_argument("--output",default="/tmp/spot_15m_primary_backtest.json"); args=ap.parse_args()
    if args.days<3:raise SystemExit("days>=3 olmali")
    end=closed_quarter(); first=end-timedelta(days=args.days); symbols=current_symbols(args.symbols); print(f"[TEST] {len(symbols)} sembol | {args.days} gun | 15M ANA TARAMA -> 1H TEYIT")
    data={}
    for i,s in enumerate(["BTCUSDT",*symbols],1):
        if s in data:continue
        try:data[s]=download_symbol(s,first,end); print(f"[DATA] {i}/{len(symbols)+1} {s}")
        except Exception as e:print(f"[DATA] {s} atlandi: {e}")
    symbols=[s for s in symbols if s in data]
    if "BTCUSDT" not in data or not symbols:raise SystemExit("Yeterli veri yok")
    scanner.ACCOUNT_SIZE=args.account; scanner.RISK_PER_TRADE_PCT=args.risk_pct; scanner.MAX_POSITION_PCT=args.max_position_pct
    fee=args.fee_pct/100; slip=args.slippage_pct/100; cash=args.account; pos={}; records=[]; curve=[]; funnel={"m15_candidates":0,"h1_rejected":0,"confirmed":0,"next_open_rejected":0,"capacity":0,"entered":0}
    cutoffs=pd.date_range(first,end,freq="15min").to_pydatetime()
    for n,cutoff in enumerate(cutoffs,1):
        cash=update_open_positions(pos,data,cutoff,cash,fee,slip,records)
        with historical_fetch(data,cutoff):
            try:btc=scanner.btc_context()
            except Exception:continue
            pres=[]
            if btc["regime"]!="RED":
                for s in symbols:
                    if s in pos:continue
                    try:
                        q=historical_quote_volume(data[s]["1h"],cutoff); pre=scanner.scan_15m_symbol(s,q,btc)
                        if pre: pres.append(pre); funnel["m15_candidates"]+=1
                    except Exception:continue
            pres.sort(key=lambda x:(x.m15_score,x.rr),reverse=True); signals=[]
            for pre in pres:
                try:
                    c=scanner.confirm_1h(pre,btc)
                    if c:signals.append(c); funnel["confirmed"]+=1
                    else:funnel["h1_rejected"]+=1
                except Exception:funnel["h1_rejected"]+=1
            signals.sort(key=lambda c:(c.entry_score,c.metrics.get("m15_score",0),c.rr),reverse=True)
        for c in signals:
            if len(pos)>=args.max_open or c.symbol in pos:funnel["capacity"]+=1; continue
            entry=next_bar_open(data,c.symbol,cutoff)
            if entry is None or c.stop>=entry or c.target1<=entry:funnel["next_open_rejected"]+=1; continue
            sp=(entry-c.stop)/entry*100; tp=(c.target1/entry-1)*100
            if sp<=0 or sp>scanner.MAX_STOP_PCT or tp<scanner.MIN_TARGET_PCT:funnel["next_open_rejected"]+=1; continue
            rr=tp/sp; eq=cash+mark_to_market(pos,data,cutoff); risk=eq*(args.risk_pct/100); raw=risk/(sp/100); maxp=eq*(args.max_position_pct/100); capital=min(raw,maxp,cash/(1+fee))
            if capital<50:funnel["capacity"]+=1;continue
            eff=entry*(1+slip); qty=capital/eff; ef=capital*fee
            if capital+ef>cash:funnel["capacity"]+=1;continue
            cash-=capital+ef; pos[c.symbol]={"entry_time":cutoff,"signal_price":c.price,"entry":entry,"effective_entry":eff,"qty":qty,"capital_used":capital,"entry_fee":ef,"stop":c.stop,"target":c.target1,"entry_score":round(c.entry_score,2),"m15_score":round(c.metrics.get("m15_score",0),2),"setup":c.setup,"btc_regime":c.btc_regime,"stop_pct":round(sp,3),"target_pct":round(tp,3),"rr":round(rr,3)}; funnel["entered"]+=1
        eq=cash+mark_to_market(pos,data,cutoff); curve.append({"time":cutoff.isoformat(),"equity":round(eq,2),"open":len(pos)})
        if n%192==0:print(f"[PROGRESS] {n}/{len(cutoffs)} | equity=${eq:,.0f} | open={len(pos)} | trades={len(records)} | 15M={funnel['m15_candidates']} | confirmed={funnel['confirmed']}")
    for s in list(pos):
        p=pos.pop(s); r=data[s]["15m"]; z=r[r.close_time<end]; cash=close_trade(s,p,float(z.close.iloc[-1]),"END",end,cash,fee,slip,records)
    curve.append({"time":end.isoformat(),"equity":round(cash,2),"open":0}); summary=summarize(records,curve,args.account); result={"strategy":"15M primary + 1H confirmation","config":vars(args),"period":{"start":first.isoformat(),"end":end.isoformat()},"summary":summary,"funnel":funnel,"trades":records,"equity_curve":curve}; Path(args.output).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    print("\n"+"="*72);print("15M PRIMARY + 1H CONFIRMATION SONUC");print("="*72);print(f"Baslangic:      ${summary['start_equity']:,.2f}");print(f"Bitis:          ${summary['end_equity']:,.2f}");print(f"Net PnL:        ${summary['net_pnl']:,.2f} ({summary['return_pct']:+.2f}%)");print(f"Trade:          {summary['closed_trades']}");print(f"Win rate:       %{summary['win_rate_pct']:.2f}");print(f"Ort. kazanc:    ${summary['avg_win']:,.2f}");print(f"Ort. kayip:     ${summary['avg_loss']:,.2f}");print(f"Profit factor:  {summary['profit_factor']}");print(f"Max drawdown:   %{summary['max_drawdown_pct']:.2f}");print(f"Funnel:         {funnel}");print(f"JSON:           {args.output}")
if __name__=="__main__":main()
