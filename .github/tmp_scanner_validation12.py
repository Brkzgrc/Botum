# Validation 12: keep V10 opportunity engine, improve capital efficiency without
# tuning on TEST. TRAIN fits the model; CAL chooses probability/flat-risk gates
# and exit policy; TEST remains untouched until the final evaluation.
exec(open('.github/tmp_scanner_validation10.py').read().split("best=None")[0])

# Features known at entry. V12 deliberately avoids the broad V11 hard-filter grid.
# Instead it learns, on TRAIN only, whether a setup is likely to remain unresolved
# for the whole 12h window (capital-wasting flat).
for a in (tr10,cal10,te10):
 a['abs_e20']=a.m15_e20.abs()
 a['abs_e50']=a.m15_e50.abs()
 a['turn_strength']=a.m15_macdd + .01*(50-a.m15_stoch).clip(-50,50)
 a['htf_strength']=a.f39 + a.f23 + .5*a.f42 + .5*a.f26

X12=X10+['abs_e20','abs_e50','turn_strength','htf_strength']
flat_model=HistGradientBoostingClassifier(max_iter=140,max_leaf_nodes=7,learning_rate=.04,l2_regularization=8,random_state=47).fit(tr10[X12],(tr10.b42<0).astype(int))
for a in (tr10,cal10,te10):
 a['pflat12']=flat_model.predict_proba(a[X12])[:,1]

# Reconstruct an economic exit from the already-computed 12h MFE/MAE labels.
# We cannot know exact intra-path dynamic trailing order from V10's compact data,
# so V12 tests only policies supported by b32/b42/b525. No TEST tuning.
def outcome(r,policy):
 if policy=='3_2': b=r.b32; win=2.998; loss=-2.002
 elif policy=='4_2': b=r.b42; win=3.998; loss=-2.002
 else: b=r.b525; win=4.998; loss=-2.502
 if b==1:return win,'W'
 if b==0:return loss,'L'
 return -.002,'F'

def sim(a,th,pflat,policy,lockh):
 q=a[(a.p10>=th)&(a.pflat12<=pflat)].sort_values(['entry_ts','p10'],ascending=[True,False])
 cap=2500.;peak=cap;dd=0.;free=pd.Timestamp.min.tz_localize('UTC');w=l=f=0;rows=[]
 for _,r in q.iterrows():
  if r.entry_ts<free:continue
  ret,kind=outcome(r,policy)
  if kind=='W':w+=1
  elif kind=='L':l+=1
  else:f+=1
  cap*=1+ret/100;peak=max(peak,cap);dd=min(dd,(cap/peak-1)*100)
  rows.append([r.entry_ts,r.symbol,ret,kind,r.p10,r.pflat12,r.mfe15,r.mae15])
  # Resolved trades release capital sooner than unresolved ones. Compact V10 data
  # has no exact hit timestamp, so use conservative policy-specific lock estimates.
  if kind=='F':hold=lockh
  elif kind=='W':hold={"3_2":6,"4_2":8,"5_25":10}[policy]
  else:hold=6
  free=r.entry_ts+pd.Timedelta(hours=hold)
 return dict(trades=len(rows),wins=w,losses=l,flat=f,win_rate=w/max(1,w+l),end=cap,return_pct=(cap/2500-1)*100,maxdd=dd),rows

# CAL-only economic search. Require enough activity so V11-style over-filtering
# cannot win merely by taking a handful of trades.
grid=[]
for th in np.arange(.38,.57,.02):
 for pf in [.30,.40,.50,.60,.70,.80,.95]:
  for pol in ['3_2','4_2','5_25']:
   for lockh in [6,9,12]:
    r,_=sim(cal10,float(th),pf,pol,lockh)
    if r['trades']<15:continue
    flat_rate=r['flat']/max(1,r['trades'])
    resolved=r['wins']+r['losses']
    # Capital growth dominates; DD and flats are penalties; resolved WR/activity
    # are mild stabilizers rather than the main objective.
    obj=r['return_pct']+.70*r['maxdd']-3.0*flat_rate+2.0*r['win_rate']+.03*r['trades']
    grid.append(dict(th=th,pflat=pf,policy=pol,lockh=lockh,objective=obj,**r))
G=pd.DataFrame(grid).sort_values('objective',ascending=False)
if len(G)==0: raise RuntimeError('V12 CAL grid produced no eligible configuration')
b=G.iloc[0];P=(float(b.th),float(b.pflat),str(b.policy),int(b.lockh))

out=[];rows=[]
for period,a in [('TRAIN',tr10),('CAL',cal10),('TEST',te10)]:
 r,t=sim(a,*P);r.update(period=period,th=P[0],pflat=P[1],policy=P[2],lockh=P[3]);out.append(r)
 for x in t:rows.append([period,*x])

# Robustness diagnostic only after CAL choice is frozen: top CAL configurations
# are shown on TEST, but they do not select the reported V12 result.
rob=[]
for _,g in G.head(20).iterrows():
 pars=(float(g.th),float(g.pflat),str(g.policy),int(g.lockh));r,_=sim(te10,*pars)
 r.update(th=pars[0],pflat=pars[1],policy=pars[2],lockh=pars[3],cal_objective=float(g.objective));rob.append(r)

pd.DataFrame(out).to_csv('/tmp/v12_summary.csv',index=False)
G.head(100).to_csv('/tmp/v12_cal_grid.csv',index=False)
pd.DataFrame(rob).to_csv('/tmp/v12_test_robust.csv',index=False)
pd.DataFrame(rows,columns=['period','entry_ts','symbol','ret','kind','p10','pflat12','mfe','mae']).to_csv('/tmp/v12_trades.csv',index=False)
print('BEST',P,flush=True)
print(pd.DataFrame(out).to_string(index=False),flush=True)
print('ROBUST\n',pd.DataFrame(rob).to_string(index=False),flush=True)
