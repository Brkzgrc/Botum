# Validation 9: regime-aware entry selection. No TEST tuning.
# Reuse v5's fully paginated 90d dataset, features, models and composite score.
exec(open('.github/tmp_scanner_validation5.py').read().split("# Pick a CAL threshold")[0])

# f0..f15 are 1H, f16..f31 4H, f32..f47 1D in validation3.
# Build regime variables only from contemporaneous higher-TF features.
# Feature layout per TF: ret1,ret3,ret6,ret24,rsi,stoch,ema20dist,ema50dist,
# ema200dist,ema20slope,ema50slope,ema200slope,macdh,macdh_delta,volz,rangepos.
for z in (tr,cal,te):
 z['r4_ret6']=z.f18; z['r4_rsi']=z.f20; z['r4_e50']=z.f23; z['r4_s50']=z.f26
 z['r1d_ret6']=z.f34; z['r1d_rsi']=z.f36; z['r1d_e50']=z.f39; z['r1d_s50']=z.f42
 # simple, interpretable regimes: strong trend / neutral / weak.
 z['regime']=np.where((z.r1d_e50>0)&(z.r1d_s50>0)&(z.r4_e50>0)&(z.r4_s50>0),'STRONG',
              np.where((z.r1d_e50<0)&(z.r4_e50<0),'WEAK','NEUTRAL'))

def dedup(q):
 q=q.sort_values(['ts','score'],ascending=[True,False]);keep=[];last={}
 for idx,r in q.iterrows():
  if r.symbol in last and r.ts-last[r.symbol]<pd.Timedelta(hours=12):continue
  keep.append(idx);last[r.symbol]=r.ts
 return q.loc[keep].sort_values('ts') if keep else q.iloc[0:0]

def sim(z,params):
 parts=[]
 for reg,(th,target,stop) in params.items():parts.append(z[(z.regime==reg)&(z.score>=th)].assign(_target=target,_stop=stop))
 q=dedup(pd.concat(parts) if parts else z.iloc[0:0]);cap=2500.;peak=cap;dd=0.;free=pd.Timestamp.min.tz_localize('UTC');w=l=f=0;rows=[]
 for _,r in q.iterrows():
  if r.ts<free:continue
  target=float(r._target);stop=float(r._stop)
  if r.mae12<=-stop:ret=-stop-.002;l+=1
  elif r.mfe12>=target:ret=target-.002;w+=1
  else:ret=-.002;f+=1
  cap*=1+ret/100;peak=max(peak,cap);dd=min(dd,(cap/peak-1)*100);rows.append([r.ts,r.symbol,r.regime,ret,r.score,target,stop]);free=r.ts+pd.Timedelta(hours=12)
 return dict(trades=len(rows),wins=w,losses=l,flat=f,win_rate=w/max(1,w+l),end=cap,return_pct=(cap/2500-1)*100,maxdd=dd),rows

# Tune each regime independently on CAL only, then combine. Require at least 4
# chronological trades per regime candidate to avoid zero-trade overfitting.
choices={};grid=[]
for reg in ['STRONG','NEUTRAL','WEAK']:
 best=None
 for th in np.arange(.14,.46,.02):
  for target,stop in [(3,1.5),(3,2),(4,1.5),(4,2),(5,2),(5,2.5),(5,3),(7,2.5),(7,3)]:
   p={reg:(float(th),target,stop)};r,_=sim(cal,p)
   if r['trades']<4:continue
   obj=r['return_pct']+.55*r['maxdd']+6*r['win_rate']+.05*r['trades']
   grid.append(dict(regime=reg,th=th,target=target,stop=stop,objective=obj,**r))
   if best is None or obj>best[0]:best=(obj,float(th),target,stop)
 if best:choices[reg]=(best[1],best[2],best[3])
# If a regime has insufficient CAL evidence, do not trade it.
out=[];rows=[]
for p,z in [('TRAIN',tr),('CAL',cal),('TEST',te)]:
 r,t=sim(z,choices);r['period']=p;out.append(r)
 for x in t:rows.append([p,*x])
# Also report each frozen regime separately on untouched TEST.
regout=[]
for reg,v in choices.items():
 r,_=sim(te,{reg:v});r.update(regime=reg,threshold=v[0],target=v[1],stop=v[2]);regout.append(r)
pd.DataFrame(out).to_csv('/tmp/v9_summary.csv',index=False);pd.DataFrame(regout).to_csv('/tmp/v9_regimes.csv',index=False);pd.DataFrame(grid).sort_values(['regime','objective'],ascending=[True,False]).to_csv('/tmp/v9_cal_grid.csv',index=False);pd.DataFrame(rows,columns=['period','ts','symbol','regime','ret','score','target','stop']).to_csv('/tmp/v9_trades.csv',index=False)
print('CHOICES',choices,flush=True);print(pd.DataFrame(out).to_string(index=False),flush=True);print(pd.DataFrame(regout).to_string(index=False),flush=True)
