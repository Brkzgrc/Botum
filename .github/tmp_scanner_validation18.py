# Validation 18: preserve V10's 12h capital cadence while testing a temporary
# portfolio risk brake. Early exits do not unlock an extra trade; this isolates
# risk sizing from the higher-turnover effect seen in V16/V17. CAL selects policy.
exec(open('.github/tmp_scanner_validation16.py').read().split("# CAL-only conservative rescue grid.")[0])

# Real 15m +4/-2 path and actual 12h close; rescue disabled.
BASE=(2.0,-99.0,999.0,2.0)
FRICTION=.20

def sim18(a,pars):
 dd_trigger,reduced_exposure,loss_trigger,defensive_trades=pars
 q=a[a.p10>=TH].sort_values(['entry_ts','p10'],ascending=[True,False])
 cap=2500.;peakcap=cap;dd=0.
 free=pd.Timestamp.min.tz_localize('UTC')
 loss_streak=0;risk_left=0;rows=[]
 for idx,r in q.iterrows():
  if r.entry_ts<free:continue
  x=outcome(r,*BASE)
  if x is None:continue
  raw_ret,xt,reason,bars=x
  current_dd=(cap/peakcap-1)*100
  was_defensive=(risk_left>0 or current_dd<=-dd_trigger)
  exposure=reduced_exposure if was_defensive else 1.0
  portfolio_ret=raw_ret*exposure
  cap*=1+portfolio_ret/100
  peakcap=max(peakcap,cap)
  dd=min(dd,(cap/peakcap-1)*100)
  hold_h=max(0.,(xt-r.entry_ts).total_seconds()/3600)
  rows.append([r.entry_ts,xt,r.symbol,raw_ret,portfolio_ret,exposure,reason,bars,hold_h,r.p10,r.mfe15,r.mae15])
  # Preserve V10 cadence even if target/stop is reached earlier.
  free=r.entry_ts+pd.Timedelta(hours=12)
  if was_defensive and risk_left>0:risk_left-=1
  if raw_ret<=-1.0:
   loss_streak+=1
   if loss_streak>=loss_trigger:risk_left=max(risk_left,defensive_trades)
  elif raw_ret>=.25:
   loss_streak=0
 wins=sum(x[4]>0 for x in rows);losses=sum(x[4]<0 for x in rows)
 return dict(trades=len(rows),wins=wins,losses=losses,end=cap,return_pct=(cap/2500-1)*100,maxdd=dd,avg_hold_h=float(np.mean([x[8] for x in rows])) if rows else 0,avg_exposure=float(np.mean([x[5] for x in rows])) if rows else 0),rows

grid=[]
for dd_trigger in [1.5,2.0,3.0,4.0,999.0]:
 for reduced_exposure in [.50,.65,.80]:
  for loss_trigger in [1,2,3]:
   for defensive_trades in [1,2,3]:
    P=(dd_trigger,reduced_exposure,loss_trigger,defensive_trades)
    r,_=sim18(cal10,P)
    if r['trades']<18:continue
    risk=abs(r['maxdd'])
    obj=r['return_pct']-1.5*risk-3.0*max(0,risk-4.0)+.02*r['trades']
    grid.append(dict(objective=obj,dd_trigger=dd_trigger,reduced_exposure=reduced_exposure,loss_trigger=loss_trigger,defensive_trades=defensive_trades,**r))
G=pd.DataFrame(grid).sort_values('objective',ascending=False)
if len(G)==0:raise RuntimeError('no eligible V18 CAL policy')
b=G.iloc[0]
P=(float(b.dd_trigger),float(b.reduced_exposure),int(b.loss_trigger),int(b.defensive_trades))

out=[];rows=[]
for period,a in [('TRAIN',tr10),('CAL',cal10),('TEST',te10)]:
 r,rr=sim18(a,P)
 r.update(period=period,threshold=TH,dd_trigger=P[0],reduced_exposure=P[1],loss_trigger=P[2],defensive_trades=P[3])
 out.append(r)
 for x in rr:rows.append([period,*x])

benchmark=[]
for period,a in [('TRAIN',tr10),('CAL',cal10),('TEST',te10)]:
 r,_=sim18(a,(999.,1.0,999,0))
 r.update(period=period)
 benchmark.append(r)

rob=[]
for _,g in G.head(25).iterrows():
 pp=(float(g.dd_trigger),float(g.reduced_exposure),int(g.loss_trigger),int(g.defensive_trades))
 r,_=sim18(te10,pp)
 r.update(cal_objective=float(g.objective),dd_trigger=pp[0],reduced_exposure=pp[1],loss_trigger=pp[2],defensive_trades=pp[3])
 rob.append(r)

pd.DataFrame(out).to_csv('/tmp/v18_summary.csv',index=False)
pd.DataFrame(benchmark).to_csv('/tmp/v18_benchmark.csv',index=False)
G.head(100).to_csv('/tmp/v18_cal_grid.csv',index=False)
pd.DataFrame(rob).to_csv('/tmp/v18_test_robust.csv',index=False)
pd.DataFrame(rows,columns=['period','entry_ts','exit_ts','symbol','raw_ret','portfolio_ret','exposure','reason','bars','hold_h','p10','mfe','mae']).to_csv('/tmp/v18_trades.csv',index=False)
print('TH',TH,'BEST',P,flush=True)
print(pd.DataFrame(out).to_string(index=False),flush=True)
print('LOCKED-CADENCE NO RISK BRAKE',flush=True)
print(pd.DataFrame(benchmark).to_string(index=False),flush=True)
