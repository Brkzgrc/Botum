# Validation 13: preserve V10 entries and improve what happens AFTER entry.
# No new entry-quality filters. TRAIN fits V10; CAL selects only exit/hold policy;
# TEST remains untouched until the final evaluation.
exec(open('.github/tmp_scanner_validation10.py').read().split("best=None")[0])

# Reuse V10's CAL threshold selection exactly; this keeps opportunity capture
# comparable and prevents V11/V12-style entry choking.
best=None
for th in np.arange(.40,.91,.02):
 q=cal10[cal10.p10>=th];dec=q[q.b42>=0]
 if len(q)<20 or len(dec)<10:continue
 prec=float(dec.b42.mean());cov=len(q)/max(1,len(cal10));obj=prec+.12*cov+.02*float(q.mfe15.mean())+.03*float(q.mae15.mean())
 if best is None or obj>best[0]:best=(obj,float(th))
if best is None:best=(0,.5)
TH=best[1]

# Compact V10 dataset stores ordered target/stop outcomes for 3/2, 4/2, 5/2.5.
# V13 uses these to compare management policies without changing which setups
# qualify. Fee/slippage remains the same explicit 0.002% placeholder as V10 so
# comparison is apples-to-apples; it is NOT asserted as a real Binance fee.
def resolved(r,pol):
 if pol=='3_2': b=r.b32; wr=2.998; lr=-2.002
 elif pol=='4_2': b=r.b42; wr=3.998; lr=-2.002
 else: b=r.b525; wr=4.998; lr=-2.502
 if b==1:return wr,'W'
 if b==0:return lr,'L'
 return None,'F'

# Capital release policy: resolved trades are allowed to release the single slot
# earlier than the old forced 12h lock. Unresolved trades can be released at a
# CAL-selected stale timeout. Because V10 compact data lacks exact hit timestamps,
# these are conservative management assumptions and are reported as such.
def sim(a,pol,stale_h,win_h,loss_h):
 q=a[a.p10>=TH].sort_values(['entry_ts','p10'],ascending=[True,False])
 cap=2500.;peak=cap;dd=0.;free=pd.Timestamp.min.tz_localize('UTC');w=l=f=0;rows=[]
 for _,r in q.iterrows():
  if r.entry_ts<free:continue
  ret,kind=resolved(r,pol)
  if kind=='W': hold=win_h; w+=1
  elif kind=='L': hold=loss_h; l+=1
  else: ret=-.002;hold=stale_h;f+=1
  cap*=1+ret/100;peak=max(peak,cap);dd=min(dd,(cap/peak-1)*100)
  rows.append([r.entry_ts,r.symbol,ret,kind,r.p10,r.mfe15,r.mae15,hold])
  free=r.entry_ts+pd.Timedelta(hours=hold)
 return dict(trades=len(rows),wins=w,losses=l,flat=f,win_rate=w/max(1,w+l),end=cap,return_pct=(cap/2500-1)*100,maxdd=dd),rows

# CAL chooses management only. Keep at least V10 CAL's 20-trade activity level.
grid=[]
for pol in ['3_2','4_2','5_25']:
 for stale in [3,4,6,8,12]:
  for wh in [3,4,6,8]:
   for lh in [2,3,4,6]:
    r,_=sim(cal10,pol,stale,wh,lh)
    if r['trades']<20:continue
    flat_rate=r['flat']/max(1,r['trades'])
    obj=r['return_pct']+.65*r['maxdd']-1.5*flat_rate+1.5*r['win_rate']+.02*r['trades']
    grid.append(dict(policy=pol,stale_h=stale,win_h=wh,loss_h=lh,objective=obj,**r))
G=pd.DataFrame(grid).sort_values('objective',ascending=False)
if len(G)==0:raise RuntimeError('V13 CAL grid produced no eligible management policy')
b=G.iloc[0];P=(str(b.policy),int(b.stale_h),int(b.win_h),int(b.loss_h))

out=[];rows=[]
for period,a in [('TRAIN',tr10),('CAL',cal10),('TEST',te10)]:
 r,t=sim(a,*P);r.update(period=period,threshold=TH,policy=P[0],stale_h=P[1],win_h=P[2],loss_h=P[3]);out.append(r)
 for x in t:rows.append([period,*x])

# Diagnostic only: top CAL management policies on TEST after the winner is frozen.
rob=[]
for _,g in G.head(25).iterrows():
 pars=(str(g.policy),int(g.stale_h),int(g.win_h),int(g.loss_h));r,_=sim(te10,*pars)
 r.update(policy=pars[0],stale_h=pars[1],win_h=pars[2],loss_h=pars[3],cal_objective=float(g.objective));rob.append(r)

pd.DataFrame(out).to_csv('/tmp/v13_summary.csv',index=False)
G.head(100).to_csv('/tmp/v13_cal_grid.csv',index=False)
pd.DataFrame(rob).to_csv('/tmp/v13_test_robust.csv',index=False)
pd.DataFrame(rows,columns=['period','entry_ts','symbol','ret','kind','p10','mfe','mae','hold_h']).to_csv('/tmp/v13_trades.csv',index=False)
print('V10 TH',TH,'BEST MANAGEMENT',P,flush=True)
print(pd.DataFrame(out).to_string(index=False),flush=True)
print('ROBUST\n',pd.DataFrame(rob).to_string(index=False),flush=True)
