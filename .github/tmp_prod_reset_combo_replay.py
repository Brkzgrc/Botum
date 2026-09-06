"""Seven-day replay: production V11 plus two additional timing templates.

Research only. Production scanner and Portfolio are not changed.
The added templates are:
- MID_40_70: 15m StochRSI K in (40, 70], turning up, with existing HTF structure.
- RESET_TURN: 1h reset/turn plus 15m low reset/turn, KDJ and Williams %R recovery.
COMBINED uses V11 OR either added template; it is not an impossible AND of K<=30 and 40<K<=70.
"""
from __future__ import annotations
import os, sys
from collections import defaultdict
from dataclasses import dataclass
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.abspath(".github"))
import tmp_spot_production_baseline as base
sys.path.insert(0, os.path.abspath("."))
import spot_opportunity_scanner as prod

START_CAPITAL=2500.0
TR_TZ="Europe/Istanbul"

def kdj_wpr(d):
    lo=d.low.rolling(9).min(); hi=d.high.rolling(9).max()
    rsv=(100*(d.close-lo)/(hi-lo).replace(0,np.nan)).fillna(50)
    k=rsv.ewm(com=2,adjust=False).mean()
    dd=k.ewm(com=2,adjust=False).mean()
    j=3*k-2*dd
    wh=d.high.rolling(14).max(); wl=d.low.rolling(14).min()
    wpr=(-100*(wh-d.close)/(wh-wl).replace(0,np.nan)).fillna(-50)
    return k,dd,j,wpr

@dataclass
class Added:
    symbol:str
    rank:float
    snapshot:dict
    decision:dict

class FastSnapCache(base.SnapCache):
    """Reuse full-frame indicators; the original cache recomputed them for every 15m step."""
    def __init__(self,data):
        super().__init__(data); self.ind={}
    def zslice(self,symbol,interval,ts,limit=240):
        key=(symbol,interval)
        if key not in self.ind: self.ind[key]=prod.indicators(self.data[interval][symbol])
        z=self.ind[key]; i=int(z.close_time.searchsorted(ts,side="right"))
        if i<80: raise ValueError("no closed history")
        return z.iloc[max(0,i-limit):i].reset_index(drop=True)
    def get(self,symbol,interval,ts,label):
        x=self.zslice(symbol,interval,ts,240); a=x.iloc[-1]; b=x.iloc[-2]
        p=prod.sf(a.close); highs,lows=prod._swings(x); atr=prod.sf(a.atr)
        c=x.close.tail(6).to_numpy(); l=x.low.tail(6).to_numpy(); rg=max(0.,prod.sf(a.high)-prod.sf(a.low))
        upper=(prod.sf(a.high)-max(prod.sf(a.open),prod.sf(a.close)))/rg if rg else 0.
        lower=(min(prod.sf(a.open),prod.sf(a.close))-prod.sf(a.low))/rg if rg else 0.
        return {"tf":label,"price":p,"bar_id":int(pd.Timestamp(a.open_time).timestamp()),"ret3":prod.pct(p,prod.sf(x.close.iloc[-4])),"ret6":prod.pct(p,prod.sf(x.close.iloc[-7])),"ret24":prod.pct(p,prod.sf(x.close.iloc[-25])),"ema20":prod.sf(a.ema20),"ema50":prod.sf(a.ema50),"ema200":prod.sf(a.ema200),"ema20_slope":prod.pct(prod.sf(a.ema20),prod.sf(x.ema20.iloc[-4])),"ema50_slope":prod.pct(prod.sf(a.ema50),prod.sf(x.ema50.iloc[-4])),"dist_ema20":prod.pct(p,prod.sf(a.ema20)),"dist_ema50":prod.pct(p,prod.sf(a.ema50)),"rsi":prod.sf(a.rsi),"stoch_k":prod.sf(a.stoch_k),"stoch_d":prod.sf(a.stoch_d),"stoch_k_prev":prod.sf(b.stoch_k),"stoch_min3":prod.sf(x.stoch_k.tail(3).min()),"macd_hist":prod.sf(a.macd_hist),"macd_hist_prev":prod.sf(b.macd_hist),"vol_ratio":prod.sf(a.vol_ratio,1),"obv_up":prod.sf(a.obv)>=prod.sf(x.obv.iloc[-6]),"obv_fast_up":prod.sf(a.obv)>=prod.sf(x.obv.iloc[-3]),"taker_buy_ratio":prod.sf(x.taker_buy_ratio.tail(3).mean(),.5),"higher_closes6":int(sum(c[i]>c[i-1] for i in range(1,len(c)))),"higher_lows6":int(sum(l[i]>=l[i-1] for i in range(1,len(l)))),"near_high20_pct":max(0.,-prod.pct(p,prod.sf(x.high.tail(20).max()))),"prev_high6":prod.sf(x.high.iloc[-7:-1].max()),"atr_pct":100*atr/p if p else 0,"upper_wick":upper,"lower_wick":lower,"supports":sorted([v for v in lows if v<p],reverse=True)[:4],"resistances":sorted([v for v in highs if v>p])[:4]}

def new_templates_at(ts, selected, snaps):
    out=[]; regime=base.btc_regime(ts,snaps)
    if regime=="RED": return out
    for symbol,_,pre_rank in selected:
        try:
            day=snaps.get(symbol,"1d",ts,"1D"); four=snaps.get(symbol,"4h",ts,"4H")
            one=snaps.get(symbol,"1h",ts,"1H"); fast=snaps.get(symbol,"15m",ts,"15M")
            raw=base.closed_slice(snaps.data["15m"][symbol],ts,100)
            if raw is None or len(raw)<30: continue
            z=prod.indicators(raw); a=z.iloc[-1]; p=z.iloc[-2]
            k,d,j,wpr=kdj_wpr(raw); i=len(raw)-1
            # Shared structure: this keeps the production multi-timeframe context.
            structure=(day["price"]>=day["ema20"] and day["rsi"]>=50 and
                       four["price"]>=four["ema50"] and four["rsi"]>=45 and
                       one["price"]>=one["ema50"] and one["rsi"]>=38)
            blowoff=(one["ret3"]>14 and one["stoch_k"]>85 and one["dist_ema20"]>15)
            if not structure or blowoff: continue
            fast_turn=(fast["stoch_k"]>fast["stoch_d"] and
                       fast["stoch_k"]>fast["stoch_k_prev"] and
                       fast["rsi"]>=40)
            confirm=(fast["macd_hist"]>fast["macd_hist_prev"] or fast["obv_fast_up"])
            mid=(40 < fast["stoch_k"] <= 70 and fast_turn and confirm and
                 fast["price"]>=fast["ema20"]*.995)
            one_turn=(one["stoch_k"]<=40 and one["stoch_k"]>one["stoch_d"] and
                      one["stoch_k"]>one["stoch_k_prev"])
            reset=(one_turn and fast["stoch_k"]<=30 and fast["stoch_k"]>fast["stoch_d"] and
                   fast["stoch_k"]>fast["stoch_k_prev"] and k.iloc[i]>d.iloc[i] and
                   j.iloc[i]>j.iloc[i-1] and wpr.iloc[i]<=-60 and wpr.iloc[i]>wpr.iloc[i-1])
            ss={"live_price":fast["price"],"1d":day,"4h":four,"1h":one,"15m":fast,
                "btc_regime":regime}
            if mid:
                out.append(Added(symbol, 82+min(8,max(0,pre_rank-55)*.25), ss,
                    {"setup_kind":"MID_40_70","confidence":82}))
            if reset:
                out.append(Added(symbol, 90+min(8,max(0,pre_rank-55)*.25), ss,
                    {"setup_kind":"RESET_TURN","confidence":90}))
        except Exception:
            continue
    return out

def replay_added(symbols,hourly,data):
    snaps=FastSnapCache(data); cache={}; selected_by_hour={}; active=defaultdict(set)
    rows=[]; scans=pd.date_range(base.START.ceil("15min"),base.END,freq="15min",tz="UTC")
    for n,ts in enumerate(scans,1):
        hour=ts.floor("1h")
        if hour not in selected_by_hour:
            selected_by_hour[hour]=base.selected_at(ts,symbols,hourly,cache)
        now=new_templates_at(ts,selected_by_hour[hour],snaps)
        current={(x.symbol,x.decision["setup_kind"]) for x in now}
        # Emit only when a condition newly turns true; a 12h same-template cooldown blocks repeats.
        for x in now:
            key=(x.symbol,x.decision["setup_kind"])
            if key in active[x.symbol]: continue
            entry,stop,tp1,tp2=base.levels(x,ts,data)
            rows.append({"entry_time":ts,"symbol":x.symbol,"kind":x.decision["setup_kind"],
                         "score":x.decision["confidence"],"rank":x.rank,
                         "btc_regime":x.snapshot["btc_regime"],"entry":entry,"stop":stop,
                         "tp1":tp1,"tp2":tp2})
        active=defaultdict(set)
        for symbol,kind in current: active[symbol].add(kind)
        if n%96==0 or n==len(scans):
            print(f"[ADDED] {n}/{len(scans)} signals={len(rows)}",flush=True)
    return pd.DataFrame(rows)

def capital(trades,name):
    if trades.empty:
        return {"variant":name,"signals":0,"selected":0,"end_capital":START_CAPITAL,
                "return_pct":0.0,"closed":0,"wins":0,"losses":0,"avg_hold_h":np.nan},pd.DataFrame()
    d=trades.copy(); d.entry_time=pd.to_datetime(d.entry_time,utc=True)
    d.exit_time=pd.to_datetime(d.exit_time,utc=True,errors="coerce")
    d["_rr"]=(d.tp1-d.entry)/(d.entry-d.stop).replace(0,np.nan)
    cap=START_CAPITAL; available=pd.Timestamp.min.tz_localize("UTC"); daily=defaultdict(float); rows=[]
    for ts,g in d.groupby("entry_time",sort=True):
        x=g.sort_values(["rank","_rr","score"],ascending=False,kind="stable").iloc[0]
        day=ts.tz_convert(TR_TZ).strftime("%Y-%m-%d")
        if ts<available or daily[day]>=5: continue
        closed=x.status=="closed" and pd.notna(x.exit_time)
        end=x.exit_time if closed else pd.Timestamp.max.tz_localize("UTC")
        before=cap; cap*=1+float(x.net_pct)/100
        if closed:
            close_day=x.exit_time.tz_convert(TR_TZ).strftime("%Y-%m-%d")
            daily[close_day]+=float(x.net_pct)
        available=end
        rows.append({"variant":name,"entry_time":ts,"symbol":x.symbol,"kind":x.kind,
                     "reason":x.reason,"status":x.status,"net_pct":x.net_pct,
                     "hold_h":((x.exit_time-ts).total_seconds()/3600 if closed else np.nan),
                     "capital_after":cap,"pnl_usdt":cap-before})
    led=pd.DataFrame(rows); closed=led[led.status=="closed"] if len(led) else led
    return {"variant":name,"signals":len(d),"selected":len(led),"end_capital":cap,
            "return_pct":(cap/START_CAPITAL-1)*100,"closed":len(closed),
            "wins":int((closed.net_pct>0).sum()) if len(closed) else 0,
            "losses":int((closed.net_pct<0).sum()) if len(closed) else 0,
            "avg_hold_h":closed.hold_h.mean() if len(closed) else np.nan},led

def tag(df,variant):
    q=df.copy()
    if len(q): q["variant"]=variant
    return q

def main():
    base.verify_frozen_source()
    symbols=base.current_universe()
    print(f"[START] {base.START} -> {base.END}; universe={len(symbols)}",flush=True)
    hourly=base.parallel_fetch(symbols,"1h",base.START-pd.Timedelta(days=12),base.END)
    symbols=sorted(hourly); cache={}; union=set()
    for ts in pd.date_range(base.START.ceil("1h"),base.END,freq="1h",tz="UTC"):
        union.update(s for s,_,_ in base.selected_at(ts,symbols,hourly,cache))
    union.add("BTCUSDT"); union=sorted(union)
    data={"1h":hourly,
          "15m":base.parallel_fetch(union,"15m",base.START-pd.Timedelta(days=3),base.END),
          "4h":base.parallel_fetch(union,"4h",base.START-pd.Timedelta(days=50),base.END),
          "1d":base.parallel_fetch(union,"1d",base.START-pd.Timedelta(days=270),base.END)}
    usable=sorted(set(union)&set(data["15m"])&set(data["4h"])&set(data["1d"])&set(hourly))
    print(f"[STAGE] usable={len(usable)}",flush=True)
    base.SnapCache=FastSnapCache\n    v11=base.replay_entries(usable,hourly,data)
    added=replay_added(usable,hourly,data)
    mid=added[added.kind=="MID_40_70"].copy() if len(added) else added
    reset=added[added.kind=="RESET_TURN"].copy() if len(added) else added
    variants={"V11_BASE":v11,
              "V11_PLUS_40_70":pd.concat([v11,mid],ignore_index=True),
              "V11_PLUS_RESET_TURN":pd.concat([v11,reset],ignore_index=True),
              "V11_PLUS_BOTH":pd.concat([v11,added],ignore_index=True)}
    reports=[]; alltrades=[]; ledgers=[]
    for name,sigs in variants.items():
        t=base.apply_portfolio(sigs); r,l=capital(t,name); reports.append(r)
        alltrades.append(tag(t,name)); ledgers.append(l)
    pd.DataFrame(reports).sort_values("return_pct",ascending=False).to_csv("/tmp/reset_combo_summary.csv",index=False)
    pd.concat(alltrades,ignore_index=True).to_csv("/tmp/reset_combo_trades.csv",index=False)
    pd.concat(ledgers,ignore_index=True).to_csv("/tmp/reset_combo_ledger.csv",index=False)
    pd.concat([tag(v11,"V11_BASE_SIGNALS"),tag(mid,"MID_40_70_SIGNALS"),tag(reset,"RESET_TURN_SIGNALS")],ignore_index=True).to_csv("/tmp/reset_combo_signals.csv",index=False)
    print(pd.DataFrame(reports).sort_values("return_pct",ascending=False).to_string(index=False),flush=True)

if __name__=="__main__": main()
