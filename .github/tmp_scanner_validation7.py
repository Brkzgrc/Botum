# Validation 7: find what separates profitable entries from stop-outs without
# touching TEST for feature/rule selection. Rebuild v5 features, then train a
# direct winner-vs-loser classifier for +4/-2. Selection uses CAL only.
exec(open('.github/tmp_scanner_validation5.py').read().split("# Chronological capital simulations on TEST.")[0])
from sklearn.ensemble import HistGradientBoostingClassifier
X=[f'f{i}' for i in range(48)]
# +4 target vs -2 stop proxy. Ambiguous both-hit is loser conservatively.
def label(z):
 y=[]
 for _,r in z.iterrows():
  ht=r.mfe12>=4.; hs=r.mae12<=-2.
  y.append(1 if ht and not hs else (0 if hs else -1))
 return np.array(y)
for z in (tr,cal,te): z['wl4']=label(z)
fit=tr[tr.wl4>=0].copy()
model=HistGradientBoostingClassifier(max_iter=180,max_leaf_nodes=9,learning_rate=.04,l2_regularization=6.,random_state=23).fit(fit[X],fit.wl4)
for z in (tr,cal,te):z['pwl']=model.predict_proba(z[X])[:,1]
# Combine direct outcome discrimination with v5 opportunity score. CAL only
# chooses thresholds; require useful sample count.
best=None
for pw in np.arange(.55,.91,.05):
 for sc in np.arange(.16,.36,.02):
  q=cal[(cal.pwl>=pw)&(cal.score>=sc)].sort_values(['ts','pwl'],ascending=[True,False])
  # symbol 12h dedup
  keep=[];last={}
  for idx,r in q.iterrows():
   if r.symbol in last and r.ts-last[r.symbol]<pd.Timedelta(hours=12):continue
   keep.append(idx);last[r.symbol]=r.ts
  q=q.loc[keep] if keep else q.iloc[0:0]
  decided=q[q.wl4>=0]
  if len(q)<12 or len(decided)<6:continue
  precision=float(decided.wl4.mean());loss=float((q.wl4==0).mean());mfe=float(q.mfe12.mean());mae=float(q.mae12.mean())
  obj=precision-.35*loss+.025*mfe+.04*mae
  if best is None or obj>best[0]:best=(obj,float(pw),float(sc))
if best is None:best=(0,.6,.2)
_,PW,SC=best

def select(z):
 q=z[(z.pwl>=PW)&(z.score>=SC)].sort_values(['ts','pwl'],ascending=[True,False]);keep=[];last={}
 for idx,r in q.iterrows():
  if r.symbol in last and r.ts-last[r.symbol]<pd.Timedelta(hours=12):continue
  keep.append(idx);last[r.symbol]=r.ts
 return q.loc[keep].sort_values('ts') if keep else q.iloc[0:0]

def capital(z):
 q=select(z);cap=2500.;peak=cap;dd=0.;free=pd.Timestamp.min.tz_localize('UTC');w=l=f=0;rows=[]
 for _,r in q.iterrows():
  if r.ts<free:continue
  if r.mae12<=-2:ret=-2.002;l+=1
  elif r.mfe12>=4:ret=3.998;w+=1
  else:ret=-.002;f+=1
  cap*=1+ret/100;peak=max(peak,cap);dd=min(dd,(cap/peak-1)*100);rows.append([r.ts,r.symbol,ret,r.pwl,r.score]);free=r.ts+pd.Timedelta(hours=12)
 return dict(trades=len(rows),wins=w,losses=l,flat=f,win_rate=w/max(1,w+l),end=cap,return_pct=(cap/2500-1)*100,maxdd=dd),rows
out=[];rows=[]
for p,z in [('TRAIN',tr),('CAL',cal),('TEST',te)]:
 q=select(z);r,t=capital(z);r.update(period=p,selected=len(q),pw=PW,score_threshold=SC,hit3=float(q.hit12_3.mean()) if len(q) else None,hit5=float(q.hit12_5.mean()) if len(q) else None,avg_mfe=float(q.mfe12.mean()) if len(q) else None,avg_mae=float(q.mae12.mean()) if len(q) else None);out.append(r)
 for x in t:rows.append([p,*x])
pd.DataFrame(out).to_csv('/tmp/v7_summary.csv',index=False);pd.DataFrame(rows,columns=['period','ts','symbol','ret','pwl','score']).to_csv('/tmp/v7_trades.csv',index=False)
# Feature separation diagnostic on TRAIN only: winner minus loser standardized mean.
a=fit[fit.wl4==1][X];b=fit[fit.wl4==0][X];effect=(a.mean()-b.mean())/fit[X].std().replace(0,np.nan);pd.DataFrame({'feature':X,'effect':effect}).sort_values('effect',key=lambda s:s.abs(),ascending=False).to_csv('/tmp/v7_features.csv',index=False)
print('BEST',best,flush=True);print(pd.DataFrame(out).to_string(index=False),flush=True);print(pd.DataFrame({'feature':X,'effect':effect}).sort_values('effect',key=lambda s:s.abs(),ascending=False).head(15).to_string(index=False),flush=True)
