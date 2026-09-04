# Validation 8: search the middle ground between v6 (too many losers) and v7
# (too selective). Rebuild v7 probabilities, choose only on CAL, then freeze.
exec(open('.github/tmp_scanner_validation7.py').read().split("best=None")[0])

def dedup(z,pw,sc):
 q=z[(z.pwl>=pw)&(z.score>=sc)].sort_values(['ts','pwl'],ascending=[True,False]);keep=[];last={}
 for idx,r in q.iterrows():
  if r.symbol in last and r.ts-last[r.symbol]<pd.Timedelta(hours=12):continue
  keep.append(idx);last[r.symbol]=r.ts
 return q.loc[keep].sort_values('ts') if keep else q.iloc[0:0]

def sim(z,pw,sc,target,stop):
 q=dedup(z,pw,sc);cap=2500.;peak=cap;dd=0.;free=pd.Timestamp.min.tz_localize('UTC');w=l=f=0;rows=[]
 for _,r in q.iterrows():
  if r.ts<free:continue
  if r.mae12<=-stop:ret=-stop-.002;l+=1
  elif r.mfe12>=target:ret=target-.002;w+=1
  else:ret=-.002;f+=1
  cap*=1+ret/100;peak=max(peak,cap);dd=min(dd,(cap/peak-1)*100);rows.append([r.ts,r.symbol,ret,r.pwl,r.score]);free=r.ts+pd.Timedelta(hours=12)
 return dict(trades=len(rows),wins=w,losses=l,flat=f,win_rate=w/max(1,w+l),end=cap,return_pct=(cap/2500-1)*100,maxdd=dd),rows

# CAL-only search. Prefer positive capital growth, high resolved precision and
# manageable DD, but require enough trades so v7's near-zero-trade solution
# cannot win. Target/stop is searched jointly with entry selectivity.
grid=[]
for pw in np.arange(.30,.66,.05):
 for sc in np.arange(.12,.31,.02):
  for target,stop in [(3,1.5),(3,2),(4,1.5),(4,2),(5,2),(5,2.5),(5,3),(7,2.5),(7,3)]:
   r,_=sim(cal,float(pw),float(sc),target,stop)
   if r['trades']<8:continue
   r.update(pw=float(pw),sc=float(sc),target=target,stop=stop)
   r['objective']=r['return_pct']+.45*r['maxdd']+8*r['win_rate']+.08*r['trades']
   grid.append(r)
grid=pd.DataFrame(grid).sort_values('objective',ascending=False);best=grid.iloc[0]
out=[];rows=[]
for p,z in [('TRAIN',tr),('CAL',cal),('TEST',te)]:
 r,t=sim(z,float(best.pw),float(best.sc),float(best.target),float(best.stop));r.update(period=p,pw=float(best.pw),sc=float(best.sc),target=float(best.target),stop=float(best.stop));out.append(r)
 for x in t:rows.append([p,*x])
# Robustness: top 20 CAL configs on untouched TEST.
rob=[]
for _,g in grid.head(20).iterrows():
 r,_=sim(te,float(g.pw),float(g.sc),float(g.target),float(g.stop));r.update(pw=float(g.pw),sc=float(g.sc),target=float(g.target),stop=float(g.stop),cal_objective=float(g.objective));rob.append(r)
pd.DataFrame(out).to_csv('/tmp/v8_summary.csv',index=False);grid.head(50).to_csv('/tmp/v8_cal_grid.csv',index=False);pd.DataFrame(rob).to_csv('/tmp/v8_test_robust.csv',index=False);pd.DataFrame(rows,columns=['period','ts','symbol','ret','pwl','score']).to_csv('/tmp/v8_trades.csv',index=False)
print('BEST',best.to_dict(),flush=True);print(pd.DataFrame(out).to_string(index=False),flush=True);print('ROBUST',pd.DataFrame(rob).to_string(index=False),flush=True)
