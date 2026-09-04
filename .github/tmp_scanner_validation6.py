# Validation 6: chronological capital-first search. Reuse v5 dataset/features,
# but evaluate score thresholds + target/stop pairs on CAL by realized sequential
# capital, then freeze that choice and report untouched TEST. One position at a
# time, 12h lock, 0.2% round-trip cost; stop wins ambiguous MFE/MAE ties.
exec(open('.github/tmp_scanner_validation5.py').read().split("# Chronological capital simulations on TEST.")[0])

def candidates(z, threshold, cooldown=12):
 q=z[z.score>=threshold].sort_values(['ts','score'],ascending=[True,False]).copy()
 keep=[];last={}
 for idx,r in q.iterrows():
  prev=last.get(r.symbol)
  if prev is not None and r.ts-prev < pd.Timedelta(hours=cooldown): continue
  keep.append(idx);last[r.symbol]=r.ts
 return q.loc[keep].sort_values(['ts','score'],ascending=[True,False]) if keep else q.iloc[0:0]

def sim(z,threshold,target,stop,hold=12,cost=.002):
 q=candidates(z,threshold,12)
 capital=2500.;peak=capital;maxdd=0.;next_free=pd.Timestamp.min.tz_localize('UTC');wins=losses=flat=0;rets=[];chosen=[]
 for _,r in q.iterrows():
  if r.ts<next_free: continue
  hitT=float(r.mfe12)>=target; hitS=float(r.mae12)<=-stop
  if hitS: ret=-stop-cost;losses+=1
  elif hitT: ret=target-cost;wins+=1
  else: ret=-cost;flat+=1
  capital*=1+ret/100.;peak=max(peak,capital);maxdd=min(maxdd,(capital/peak-1)*100);rets.append(ret);chosen.append((r.ts,r.symbol,ret,float(r.score)))
  next_free=r.ts+pd.Timedelta(hours=hold)
 return dict(threshold=threshold,target=target,stop=stop,trades=len(rets),wins=wins,losses=losses,flat=flat,win_rate=wins/max(1,wins+losses),end=capital,return_pct=(capital/2500-1)*100,maxdd=maxdd,avg_ret=float(np.mean(rets)) if rets else 0),chosen

# Search only CAL. Objective strongly rewards return but penalizes drawdown and
# tiny samples. No TEST values participate in parameter choice.
grid=[]
for threshold in np.arange(.16,.36,.01):
 for target,stop in [(2,1.5),(2.5,1.5),(3,1.5),(3,2),(4,1.5),(4,2),(5,2),(5,2.5),(5,3),(7,2.5),(7,3)]:
  r,_=sim(cal,float(threshold),target,stop)
  if r['trades']<8: continue
  r['objective']=r['return_pct']+.60*r['maxdd']+.10*r['trades']
  grid.append(r)
grid=pd.DataFrame(grid).sort_values('objective',ascending=False)
best=grid.iloc[0].to_dict()

out=[];trades=[]
for period,z in [('TRAIN',tr),('CAL',cal),('TEST',te)]:
 r,t=sim(z,float(best['threshold']),float(best['target']),float(best['stop']))
 r['period']=period;out.append(r)
 for x in t:trades.append([period,*x])
# robustness: top 10 CAL configurations, evaluated untouched TEST
rob=[]
for _,g in grid.head(10).iterrows():
 r,_=sim(te,float(g.threshold),float(g.target),float(g.stop));r['cal_objective']=float(g.objective);rob.append(r)

pd.DataFrame(out).to_csv('/tmp/v6_summary.csv',index=False)
grid.head(30).to_csv('/tmp/v6_cal_grid.csv',index=False)
pd.DataFrame(rob).to_csv('/tmp/v6_test_robust.csv',index=False)
pd.DataFrame(trades,columns=['period','ts','symbol','ret_pct','score']).to_csv('/tmp/v6_trades.csv',index=False)
print('BEST CAL',best,flush=True);print(pd.DataFrame(out).to_string(index=False),flush=True);print('TEST ROBUST\n',pd.DataFrame(rob).to_string(index=False),flush=True)
