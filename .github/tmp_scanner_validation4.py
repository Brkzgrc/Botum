exec(open('.github/tmp_scanner_validation3.py').read().split("summary=[]")[0])
from sklearn.ensemble import HistGradientBoostingClassifier
# Risk-aware labels: +1.5% must arrive with controlled 12h adverse excursion.
df['clean15']=((df.hit12_15==1)&(df.mae12>-2.0)).astype(int)
df['clean3']=((df.hit12_3==1)&(df.mae12>-2.5)).astype(int)
train_end=START+pd.Timedelta(days=45);cal_end=START+pd.Timedelta(days=60);tr=df[df.ts<train_end];cal=df[(df.ts>=train_end)&(df.ts<cal_end)];te=df[df.ts>=cal_end];X=[f'f{i}' for i in range(48)]
summary=[]
for target in ['clean15','clean3','tb2']:
 model=HistGradientBoostingClassifier(max_iter=220,max_leaf_nodes=11,learning_rate=.045,l2_regularization=4.0,random_state=11).fit(tr[X],tr[target])
 pc=model.predict_proba(cal[X])[:,1]
 best=None
 for th in np.arange(.35,.91,.01):
  m=pc>=th;n=int(m.sum());cov=n/max(1,len(cal))
  if n<50 or cov<.025:continue
  prec=float(cal.loc[m,target].mean());mae=float(cal.loc[m,'mae12'].mean());mfe=float(cal.loc[m,'mfe12'].mean());rec=float(cal.loc[m,target].sum()/max(1,cal[target].sum()))
  score=prec+.10*rec+.03*mfe+.04*mae
  if best is None or score>best[0]:best=(score,th)
 if best is None:best=(0,.5)
 th=best[1]
 for period,z in [('TRAIN',tr),('CAL',cal),('TEST',te)]:
  p=model.predict_proba(z[X])[:,1];m=p>=th;n=int(m.sum())
  summary.append(dict(target=target,period=period,n_total=len(z),selected=n,coverage=n/max(1,len(z)),threshold=th,precision=float(z.loc[m,target].mean()) if n else None,baseline=float(z[target].mean()),hit15=float(z.loc[m,'hit12_15'].mean()) if n else None,hit3=float(z.loc[m,'hit12_3'].mean()) if n else None,tb2=float(z.loc[m,'tb2'].mean()) if n else None,avg_mfe12=float(z.loc[m,'mfe12'].mean()) if n else None,avg_mae12=float(z.loc[m,'mae12'].mean()) if n else None))
 pt=model.predict_proba(te[X])[:,1]
 for frac in [.05,.10,.15]:
  q=np.quantile(pt,1-frac);m=pt>=q;n=int(m.sum())
  summary.append(dict(target=target,period=f'TEST_TOP{int(frac*100)}',n_total=len(te),selected=n,coverage=float(m.mean()),threshold=float(q),precision=float(te.loc[m,target].mean()),baseline=float(te[target].mean()),hit15=float(te.loc[m,'hit12_15'].mean()),hit3=float(te.loc[m,'hit12_3'].mean()),tb2=float(te.loc[m,'tb2'].mean()),avg_mfe12=float(te.loc[m,'mfe12'].mean()),avg_mae12=float(te.loc[m,'mae12'].mean())))
pd.DataFrame(summary).to_csv('/tmp/v4_summary.csv',index=False);print(pd.DataFrame(summary).to_string(index=False),flush=True)
