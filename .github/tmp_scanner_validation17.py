# Validation 17: keep V10-qualified entries and apply a portfolio-level risk brake.
# CAL alone selects the drawdown trigger, reduced exposure and loss-streak pause.
# TEST remains untouched until the policy is frozen.
exec(open('.github/tmp_scanner_validation16.py').read().split("# CAL-only conservative rescue grid.")[0])

# Use the real 15m path with V10's +4/-2 structure and actual 12h close.
# Rescue is disabled here; V17 isolates portfolio risk management.
BASE=(2.0,-99.0,999.0,2.0)
FRICTION=.20

def sim17(a,pars):
 dd_trigger,reduced_exposure,loss_trigger,cooldown_h=pars
 q=a[a.p10>=TH].sort_values(['entry_ts','p10'],ascending=[True,False])
 cap=2500.;peakcap=cap;dd=0.
 free=pd.Timestamp.min.tz_localize('UTC')
 cooldown_until=pd.Timestamp.min.tz_localize('UTC')
 loss_streak=0;defensive=False;rows=[]
 for idx,r in q.iterrows():
  if r.entry_ts<free or r.entry_ts<cooldown_until:continue
  x=outcome(r,*BASE)
  if x is None:continue
  raw_ret,xt,reason,bars=x
  current_dd=(cap/peakcap-1)*100
  exposure=reduced_exposure if defensive or current_dd<=-dd_trigger else 1.0
  portfolio_ret=raw_ret*exposure
  cap*=1+portfolio_ret/100
  peakcap=max(peakcap,cap)
  dd=min(dd,(cap/peakcap-1)*100)
  hold_h=max(0.,(xt-r.entry_ts).total_seconds()/3600)
  rows.append([r.entry_ts,xt,r.symbol,raw_ret,portfolio_ret,exposure,reason,bars,hold_h,r.p10,r.mfe15,r.mae15])
  free=xt
  if raw_ret<=-1.0:
   loss_streak+=1
   if loss_streak>=loss_trigger:
    defensive=True
    if cooldown_h>0:cooldown_until=xt+pd.Timedelta(hours=cooldown_h)
  elif raw_ret>=.25:
   loss_streak=0;defensive=False
 wins=sum(x[4]>0 for x in rows);losses=sum(x[4]<0 for x in rows)
 return dict(trades=len(rows),wins=wins,losses=losses,end=cap,return_pct=(cap/2500-1)*100,maxdd=dd,avg_hold_h=float(np.mean([x[8] for x in rows])) if rows else 0,avg_exposure=float(np.mean([x[5] for x in rows])) if rows else 0),rows

# CAL-only search. Keep meaningful participation; risk control must not solve DD
# merely by suppressing nearly every trade.
grid=[]
for dd_trigger in [1.5,2.0,3.0,4.0]:
 for reduced_exposure in [.35,.50,.65,.80]:
  for loss_trigger in [1,2,3]:
   for cooldown_h in [0,6,12,24]:
    P=(dd_trigger,reduced_exposure,loss_trigger,cooldown_h)
    r,_=sim17(cal10,P)
    if r['trades']<18:continue
    risk=abs(r['maxdd'])
    obj=r['return_pct']-1.75*risk-3.0*max(0,risk-4.0)+.01*r['trades']
    grid.append(dict(objective=obj,dd_trigger=dd_trigger,reduced_exposure=reduced_exposure,loss_trigger=loss_trigger,cooldown_h=cooldown_h,**r))
G=pd.DataFrame(grid).sort_values('objective',ascending=False)
if len(G)==0:raise RuntimeError('no eligible V17 CAL policy')
b=G.iloc[0]
P=(float(b.dd_trigger),float(b.reduced_exposure),int(b.loss_trigger),float(b.cooldown_h))

out=[];rows=[]
for period,a in [('TRAIN',tr10),('CAL',cal10),('TEST',te10)]:
 r,rr=sim17(a,P)
 r.update(period=period,threshold=TH,dd_trigger=P[0],reduced_exposure=P[1],loss_trigger=P[2],cooldown_h=P[3])
 out.append(r)
 for x in rr:rows.append([period,*x])

benchmark=[]
for period,a in [('TRAIN',tr10),('CAL',cal10),('TEST',te10)]:
 r,_=sim17(a,(999.,1.0,999,0.))
 r.update(period=period)
 benchmark.append(r)

rob=[]
for _,g in G.head(25).iterrows():
 pp=(float(g.dd_trigger),float(g.reduced_exposure),int(g.loss_trigger),float(g.cooldown_h))
 r,_=sim17(te10,pp)
 r.update(cal_objective=float(g.objective),dd_trigger=pp[0],reduced_exposure=pp[1],loss_trigger=pp[2],cooldown_h=pp[3])
 rob.append(r)

pd.DataFrame(out).to_csv('/tmp/v17_summary.csv',index=False)
pd.DataFrame(benchmark).to_csv('/tmp/v17_benchmark.csv',index=False)
G.head(100).to_csv('/tmp/v17_cal_grid.csv',index=False)
pd.DataFrame(rob).to_csv('/tmp/v17_test_robust.csv',index=False)
pd.DataFrame(rows,columns=['period','entry_ts','exit_ts','symbol','raw_ret','portfolio_ret','exposure','reason','bars','hold_h','p10','mfe','mae']).to_csv('/tmp/v17_trades.csv',index=False)
print('TH',TH,'BEST',P,flush=True)
print(pd.DataFrame(out).to_string(index=False),flush=True)
print('NO RISK BRAKE',flush=True)
print(pd.DataFrame(benchmark).to_string(index=False),flush=True)
