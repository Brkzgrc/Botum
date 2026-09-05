# Validation 15: preserve V10 entry model, but rebuild exact post-entry 15m paths.
# Goal: reduce V10 drawdown without choking opportunity count. TRAIN fits the
# unchanged V10 classifier, CAL alone selects exit management, TEST is untouched.
exec(open('.github/tmp_scanner_validation10.py').read().split("best=None")[0])

# Re-fetch 15m path only for V10 candidates and store exact 15m OHLC after entry.
paths={}
for i,s in enumerate(SYMS,1):
 print('PATH15',i,len(SYMS),s,flush=True)
 try:h15=fetch15(s,START-pd.Timedelta(days=4),NOW+pd.Timedelta(hours=14))
 except Exception as e:print('PATH SKIP',s,e,flush=True);continue
 for idx,r in z[z.symbol==s].iterrows():
  ets=r.entry_ts; entry=float(r.entry15)
  fut=h15[(h15.open_time>=ets)&(h15.open_time<ets+pd.Timedelta(hours=12))].copy()
  if len(fut)<40:continue
  paths[idx]=[(x.open_time,float(x.open),float(x.high),float(x.low),float(x.close),float(x.ema20),float(x.rsi),float(x.stoch),float(x.macd)) for _,x in fut.iterrows()]

# Management grid: actual candle chronology. No entry filtering beyond V10 p10.
# friction is an explicit assumed round-trip cost, not claimed Binance actual fee.
FRICTION=.20

def manage(r,stop,activate,trail,stale_h,progress):
 p=paths.get(r.name); entry=float(r.entry15)
 if not p:return None
 peak=entry; peakret=0.; activated=False
 for j,(ts,o,h,l,c,e20,rsi,stoch,macd) in enumerate(p):
  hr=(h/entry-1)*100; lr=(l/entry-1)*100; cr=(c/entry-1)*100
  peak=max(peak,h);peakret=max(peakret,hr)
  # Conservative same-candle ordering: stop has priority.
  if lr<=-stop:return -stop-FRICTION,ts,'STOP',j+1
  if hr>=activate:activated=True
  if activated:
   trail_px=peak*(1-trail/100)
   if l<=trail_px:
    rr=(trail_px/entry-1)*100-FRICTION
    return rr,ts,'TRAIL',j+1
  # Stale exit uses actual close after elapsed time and only when trade failed
  # to make the configured favorable progress. Never exits an already activated runner.
  hours=(j+1)*.25
  if not activated and hours>=stale_h and peakret<progress:
   return cr-FRICTION,ts,'STALE',j+1
 # 12h time exit at actual final close.
 ts,o,h,l,c,e20,rsi,stoch,macd=p[-1]
 return (c/entry-1)*100-FRICTION,ts,'TIME',len(p)

def sim(a,th,pars):
 stop,activate,trail,stale_h,progress=pars
 q=a[a.p10>=th].sort_values(['entry_ts','p10'],ascending=[True,False])
 cap=2500.;peakcap=cap;dd=0.;free=pd.Timestamp.min.tz_localize('UTC');rows=[]
 for idx,r in q.iterrows():
  if r.entry_ts<free:continue
  x=manage(r,*pars)
  if x is None:continue
  ret,xt,reason,bars=x;cap*=1+ret/100;peakcap=max(peakcap,cap);dd=min(dd,(cap/peakcap-1)*100)
  rows.append([r.entry_ts,xt,r.symbol,ret,reason,bars,r.p10,r.mfe15,r.mae15]);free=xt
 wins=sum(x[3]>0 for x in rows);losses=sum(x[3]<0 for x in rows)
 return dict(trades=len(rows),wins=wins,losses=losses,end=cap,return_pct=(cap/2500-1)*100,maxdd=dd,avg_hold_h=np.mean([x[6]*.25 for x in rows]) if rows else 0),rows

# Preserve V10 probability threshold selection exactly; optimize only exits on CAL.
TH=best[1] if 'best' in globals() and best else .42
# Because execution stopped before V10's best loop, reproduce its CAL threshold selection.
bt=None
for th in np.arange(.40,.91,.02):
 q=cal10[cal10.p10>=th];dec=q[q.b42>=0]
 if len(q)<20 or len(dec)<10:continue
 prec=float(dec.b42.mean());cov=len(q)/max(1,len(cal10));obj=prec+.12*cov+.02*float(q.mfe15.mean())+.03*float(q.mae15.mean())
 if bt is None or obj>bt[0]:bt=(obj,float(th))
TH=bt[1] if bt else .42

grid=[]
for stop in [1.5,2.0,2.5]:
 for activate in [3.0,4.0,5.0]:
  for trail in [1.0,1.5,2.0]:
   for stale_h in [3,4,6]:
    for progress in [.5,1.0,1.5]:
     pars=(stop,activate,trail,stale_h,progress);r,_=sim(cal10,TH,pars)
     if r['trades']<18:continue
     dd=abs(r['maxdd']);obj=r['return_pct']-1.75*dd-2.5*max(0,dd-4)+.02*r['trades']
     grid.append(dict(objective=obj,stop=stop,activate=activate,trail=trail,stale_h=stale_h,progress=progress,**r))
G=pd.DataFrame(grid).sort_values('objective',ascending=False)
if len(G)==0:raise RuntimeError('no eligible V15 CAL policy')
b=G.iloc[0];P=(float(b.stop),float(b.activate),float(b.trail),float(b.stale_h),float(b.progress))

out=[];allrows=[]
for period,a in [('TRAIN',tr10),('CAL',cal10),('TEST',te10)]:
 r,rr=sim(a,TH,P);r.update(period=period,threshold=TH,stop=P[0],activate=P[1],trail=P[2],stale_h=P[3],progress=P[4]);out.append(r)
 for x in rr:allrows.append([period,*x])
rob=[]
for _,g in G.head(25).iterrows():
 pars=(float(g.stop),float(g.activate),float(g.trail),float(g.stale_h),float(g.progress));r,_=sim(te10,TH,pars);r.update(cal_objective=float(g.objective),stop=pars[0],activate=pars[1],trail=pars[2],stale_h=pars[3],progress=pars[4]);rob.append(r)
pd.DataFrame(out).to_csv('/tmp/v15_summary.csv',index=False)
G.head(100).to_csv('/tmp/v15_cal_grid.csv',index=False)
pd.DataFrame(rob).to_csv('/tmp/v15_test_robust.csv',index=False)
pd.DataFrame(allrows,columns=['period','entry_ts','exit_ts','symbol','ret','reason','bars','p10','mfe','mae']).to_csv('/tmp/v15_trades.csv',index=False)
print('TH',TH,'BEST',P,flush=True);print(pd.DataFrame(out).to_string(index=False),flush=True)
