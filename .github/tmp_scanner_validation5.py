# Return-focused validation: measure the objective Burak actually cares about.
# Reuse the fully paginated 90d dataset builder from validation3, then add
# +5/+7/+10 outcomes, risk-aware labels, chronological event de-duplication,
# and a simple $2500 sequential capital simulation on untouched TEST data.
exec(open('.github/tmp_scanner_validation3.py').read().split("summary=[]")[0])
from sklearn.ensemble import HistGradientBoostingClassifier

# The existing builder stores 12h MFE/MAE. These labels deliberately do not
# pretend an exact exit price when only OHLC path extrema are known.
df['hit12_5']=(df.mfe12>=5.0).astype(int)
df['hit12_7']=(df.mfe12>=7.0).astype(int)
df['hit12_10']=(df.mfe12>=10.0).astype(int)
df['clean3']=((df.hit12_3==1)&(df.mae12>-2.5)).astype(int)
df['clean5']=((df.hit12_5==1)&(df.mae12>-3.0)).astype(int)
train_end=START+pd.Timedelta(days=45);cal_end=START+pd.Timedelta(days=60)
tr=df[df.ts<train_end].copy();cal=df[(df.ts>=train_end)&(df.ts<cal_end)].copy();te=df[df.ts>=cal_end].copy()
X=[f'f{i}' for i in range(48)]

# Train several objectives. Selection threshold is chosen only on CAL.
models={}
for target in ['clean3','clean5','hit12_5','hit12_7']:
 models[target]=HistGradientBoostingClassifier(max_iter=220,max_leaf_nodes=11,learning_rate=.045,l2_regularization=4.0,random_state=17).fit(tr[X],tr[target])
 for z in (tr,cal,te): z['p_'+target]=models[target].predict_proba(z[X])[:,1]

# Composite score rewards both probability of a meaningful move and risk-aware
# versions of the same setup. No TEST information is used here.
for z in (tr,cal,te):
 z['score']=.30*z.p_clean3+.30*z.p_clean5+.25*z.p_hit12_5+.15*z.p_hit12_7

# Pick a CAL threshold that favours +5% opportunity precision while retaining
# enough candidates to be useful. Risk is penalized through adverse excursion.
best=None
for th in np.arange(.20,.81,.01):
 m=cal.score>=th;n=int(m.sum());cov=n/max(1,len(cal))
 if n<50 or cov<.02: continue
 hit3=float(cal.loc[m,'hit12_3'].mean());hit5=float(cal.loc[m,'hit12_5'].mean());hit7=float(cal.loc[m,'hit12_7'].mean())
 mae=float(cal.loc[m,'mae12'].mean());mfe=float(cal.loc[m,'mfe12'].mean())
 objective=.25*hit3+.45*hit5+.20*hit7+.04*mfe+.06*mae
 if best is None or objective>best[0]: best=(objective,float(th))
if best is None: best=(0,.35)
th=best[1]

# Event-level de-duplication: overlapping 4h observations of the same trend are
# not counted as independent trades. Keep at most one candidate/symbol/12h.
def dedup(z, threshold):
 q=z[z.score>=threshold].sort_values(['ts','score'],ascending=[True,False]).copy()
 keep=[];last={}
 for idx,r in q.iterrows():
  prev=last.get(r.symbol)
  if prev is not None and r.ts-prev < pd.Timedelta(hours=12): continue
  keep.append(idx);last[r.symbol]=r.ts
 return q.loc[keep].sort_values('ts') if keep else q.iloc[0:0]

summary=[]
for period,z in [('TRAIN',tr),('CAL',cal),('TEST',te)]:
 q=dedup(z,th);n=len(q)
 summary.append(dict(period=period,n_total=len(z),selected=n,coverage=n/max(1,len(z)),threshold=th,
  hit15=float(q.hit12_15.mean()) if n else None,hit3=float(q.hit12_3.mean()) if n else None,
  hit5=float(q.hit12_5.mean()) if n else None,hit7=float(q.hit12_7.mean()) if n else None,hit10=float(q.hit12_10.mean()) if n else None,
  tb2=float(q.tb2.mean()) if n else None,avg_mfe12=float(q.mfe12.mean()) if n else None,avg_mae12=float(q.mae12.mean()) if n else None))

# Also show untouched TEST top score bands, event-deduplicated.
for frac in [.02,.05,.10]:
 qscore=float(te.score.quantile(1-frac));q=dedup(te,qscore);n=len(q)
 summary.append(dict(period=f'TEST_TOP{int(frac*100)}',n_total=len(te),selected=n,coverage=n/max(1,len(te)),threshold=qscore,
  hit15=float(q.hit12_15.mean()) if n else None,hit3=float(q.hit12_3.mean()) if n else None,
  hit5=float(q.hit12_5.mean()) if n else None,hit7=float(q.hit12_7.mean()) if n else None,hit10=float(q.hit12_10.mean()) if n else None,
  tb2=float(q.tb2.mean()) if n else None,avg_mfe12=float(q.mfe12.mean()) if n else None,avg_mae12=float(q.mae12.mean()) if n else None))

# Chronological capital simulations on TEST. This is intentionally conservative:
# one position at a time, whole $2500 capital, 12h lock, fixed target/stop,
# 0.2% round-trip cost. If target and stop are both possible from extrema we
# count the stop (conservative). This is not yet exact candle-path replay.
def capital_sim(z,target,stop,cost=.002):
 q=dedup(z,th).sort_values(['ts','score'],ascending=[True,False])
 capital=2500.0;start=capital;next_free=pd.Timestamp.min.tz_localize('UTC');wins=losses=0;trades=[]
 for _,r in q.iterrows():
  if r.ts<next_free: continue
  # With MFE/MAE only, simultaneous reach order is unknown: stop wins tie.
  hitT=r.mfe12>=target;hitS=r.mae12<=-stop
  if hitS: ret=-stop-cost;losses+=1
  elif hitT: ret=target-cost;wins+=1
  else:
   # unresolved after 12h: mark at a conservative proxy bounded by extrema.
   # Do not award MFE; use 0 minus costs.
   ret=-cost
  before=capital;capital*=1+ret/100.0
  trades.append((r.ts,r.symbol,before,ret,capital,float(r.score)))
  next_free=r.ts+pd.Timedelta(hours=12)
 return dict(target=target,stop=stop,trades=len(trades),wins=wins,losses=losses,start=start,end=capital,return_pct=(capital/start-1)*100),trades

caps=[];alltr=[]
for target,stop in [(3,2),(4,2),(5,2.5),(5,3),(7,3)]:
 c,t=capital_sim(te,target,stop);caps.append(c)
 for x in t: alltr.append([target,stop,*x])

pd.DataFrame(summary).to_csv('/tmp/v5_summary.csv',index=False)
pd.DataFrame(caps).to_csv('/tmp/v5_capital.csv',index=False)
pd.DataFrame(alltr,columns=['target','stop','ts','symbol','capital_before','ret_pct','capital_after','score']).to_csv('/tmp/v5_trades.csv',index=False)
print('THRESHOLD',th,flush=True)
print(pd.DataFrame(summary).to_string(index=False),flush=True)
print(pd.DataFrame(caps).to_string(index=False),flush=True)
print('ROWS',len(df),'TRAIN',len(tr),'CAL',len(cal),'TEST',len(te),flush=True)
