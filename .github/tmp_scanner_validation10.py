# Validation 10: add real 15m timing/retrigger information to the existing
# 1H/4H/1D setup. Candidate setup is observed on the hour; entry may occur only
# after a closed 15m candle confirms a retrigger during the next 60 minutes.
# TRAIN/CAL select rules; TEST remains untouched.
exec(open('.github/tmp_scanner_validation5.py').read().split("# Pick a CAL threshold")[0])

# Fetch 15m only for the 90d study window (+3d indicator warmup), then build
# contemporaneous timing features. Binance max 1000 rows requires pagination.
def fetch15(sym,start,end):
 step=900000;cur=int(pd.Timestamp(start).timestamp()*1000);endms=int(pd.Timestamp(end).timestamp()*1000);out=[]
 while cur<endms:
  rows=get({'symbol':sym,'interval':'15m','startTime':cur,'endTime':endms,'limit':1000})
  if not rows:break
  out.extend(rows);nxt=int(rows[-1][0])+step
  if nxt<=cur:break
  cur=nxt;time.sleep(.02)
 return ind(frame(out))

extra=[]
for i,s in enumerate(SYMS,1):
 print('15M',i,len(SYMS),s,flush=True)
 try:h15=fetch15(s,START-pd.Timedelta(days=4),NOW+pd.Timedelta(hours=2))
 except Exception as e:print('15M SKIP',s,e,flush=True);continue
 base=df[df.symbol==s]
 for idx,r in base.iterrows():
  # Only candles fully closed after setup timestamp and within next hour.
  q=h15[(h15.close_time>=r.ts)&(h15.close_time<r.ts+pd.Timedelta(hours=1))]
  hist=h15[h15.close_time<r.ts]
  if len(q)<3 or len(hist)<30:continue
  prev=hist.iloc[-1];entry=None;feat=None
  for _,a in q.iterrows():
   # early retrigger: Stoch turn or MACD histogram improving, while price is
   # reclaiming EMA20 / prior 15m close. MACD need not be positive.
   st_turn=float(a.stoch)>float(prev.stoch)+2
   macd_improve=float(a.macd)>float(prev.macd)
   reclaim=float(a.close)>=float(a.ema20) or float(a.close)>float(prev.close)
   not_spike=pct(float(a.close),float(prev.close))<2.5
   if reclaim and not_spike and (st_turn or macd_improve):
    entry=float(a.close);feat=[float(a.rsi),float(a.stoch),pct(float(a.close),float(a.ema20)),pct(float(a.close),float(a.ema50)),float(a.macd),float(a.macd-prev.macd),float(a.volr),pct(float(a.close),float(prev.close)),float(a.close_time.value/1e9)];break
   prev=a
  if entry is None:continue
  # Outcomes start from the actual 15m confirmation entry, not the earlier
  # hourly snapshot price. Use subsequent 15m path for order of target/stop.
  ets=pd.to_datetime(feat[-1],unit='s',utc=True);fut=h15[(h15.open_time>=ets)&(h15.open_time<ets+pd.Timedelta(hours=12))]
  if len(fut)<40:continue
  hi=float(fut.high.max());lo=float(fut.low.min());mfe=(hi/entry-1)*100;mae=(lo/entry-1)*100
  def before(tgt,stp):
   for _,c in fut.iterrows():
    ht=float(c.high)>=entry*(1+tgt/100);hs=float(c.low)<=entry*(1-stp/100)
    if ht and hs:return 0
    if hs:return 0
    if ht:return 1
   return -1
  extra.append([idx,ets,entry,mfe,mae,before(3,2),before(4,2),before(5,2.5)]+feat[:-1])
E=pd.DataFrame(extra,columns=['idx','entry_ts','entry15','mfe15','mae15','b32','b42','b525','m15_rsi','m15_stoch','m15_e20','m15_e50','m15_macd','m15_macdd','m15_volr','m15_ret'])
z=df.join(E.set_index('idx'),how='inner')
# Preserve original chronological split.
train_end=START+pd.Timedelta(days=45);cal_end=START+pd.Timedelta(days=60);tr10=z[z.ts<train_end].copy();cal10=z[(z.ts>=train_end)&(z.ts<cal_end)].copy();te10=z[z.ts>=cal_end].copy()
X10=[f'f{i}' for i in range(48)]+['m15_rsi','m15_stoch','m15_e20','m15_e50','m15_macd','m15_macdd','m15_volr','m15_ret']
# Directly learn +4 before -2 only on decided TRAIN cases. CAL chooses threshold.
fit=tr10[tr10.b42>=0]
model=HistGradientBoostingClassifier(max_iter=180,max_leaf_nodes=9,learning_rate=.04,l2_regularization=6,random_state=31).fit(fit[X10],fit.b42)
for a in (tr10,cal10,te10):a['p10']=model.predict_proba(a[X10])[:,1]
best=None
for th in np.arange(.40,.91,.02):
 q=cal10[cal10.p10>=th];dec=q[q.b42>=0]
 if len(q)<20 or len(dec)<10:continue
 prec=float(dec.b42.mean());cov=len(q)/max(1,len(cal10));obj=prec+.12*cov+.02*float(q.mfe15.mean())+.03*float(q.mae15.mean())
 if best is None or obj>best[0]:best=(obj,float(th))
if best is None:best=(0,.5)
TH=best[1]

def sim(a):
 q=a[a.p10>=TH].sort_values(['entry_ts','p10'],ascending=[True,False]);cap=2500.;peak=cap;dd=0.;free=pd.Timestamp.min.tz_localize('UTC');w=l=f=0;rows=[]
 for _,r in q.iterrows():
  if r.entry_ts<free:continue
  if r.b42==1:ret=3.998;w+=1
  elif r.b42==0:ret=-2.002;l+=1
  else:ret=-.002;f+=1
  cap*=1+ret/100;peak=max(peak,cap);dd=min(dd,(cap/peak-1)*100);rows.append([r.entry_ts,r.symbol,ret,r.p10,r.mfe15,r.mae15]);free=r.entry_ts+pd.Timedelta(hours=12)
 return dict(trades=len(rows),wins=w,losses=l,flat=f,win_rate=w/max(1,w+l),end=cap,return_pct=(cap/2500-1)*100,maxdd=dd),rows
out=[];rows=[]
for p,a in [('TRAIN',tr10),('CAL',cal10),('TEST',te10)]:
 q=a[a.p10>=TH];r,t=sim(a);r.update(period=p,threshold=TH,candidates=len(q),avg_mfe=float(q.mfe15.mean()) if len(q) else None,avg_mae=float(q.mae15.mean()) if len(q) else None);out.append(r)
 for x in t:rows.append([p,*x])
pd.DataFrame(out).to_csv('/tmp/v10_summary.csv',index=False);pd.DataFrame(rows,columns=['period','entry_ts','symbol','ret','p10','mfe','mae']).to_csv('/tmp/v10_trades.csv',index=False);pd.DataFrame({'feature':X10,'importance_proxy':np.nan}).to_csv('/tmp/v10_features.csv',index=False)
print('15M MATCHED',len(z),'TH',TH,flush=True);print(pd.DataFrame(out).to_string(index=False),flush=True)
