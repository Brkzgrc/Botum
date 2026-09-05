# Validation 16: preserve V10 entries and explicitly study what V15 removed.
# We do NOT add broad entry filters. Exact 15m path is rebuilt, then CAL chooses
# a conservative rescue rule intended to cut only obvious failures while keeping
# V10's 12h opportunity window. TEST stays untouched until parameters are frozen.
exec(open('.github/tmp_scanner_validation10.py').read().split("best=None")[0])

# Reproduce V10 CAL threshold exactly.
bt=None
for th in np.arange(.40,.91,.02):
 q=cal10[cal10.p10>=th];dec=q[q.b42>=0]
 if len(q)<20 or len(dec)<10:continue
 prec=float(dec.b42.mean());cov=len(q)/max(1,len(cal10));obj=prec+.12*cov+.02*float(q.mfe15.mean())+.03*float(q.mae15.mean())
 if bt is None or obj>bt[0]:bt=(obj,float(th))
TH=bt[1] if bt else .42
# p10 is assigned to the chronological split copies, not to their source z frame.
# Recombine those copies so the exact same V10-qualified rows keep p10 available.
v10_rows=pd.concat([tr10,cal10,te10],axis=0)

# Exact 15m paths for V10-qualified candidates.
paths={}
for i,s in enumerate(SYMS,1):
 print('V16 PATH',i,len(SYMS),s,flush=True)
 try:h15=fetch15(s,START-pd.Timedelta(days=4),NOW+pd.Timedelta(hours=14))
 except Exception as e:print('PATH SKIP',s,e,flush=True);continue
 for idx,r in v10_rows[(v10_rows.symbol==s)&(v10_rows.p10>=TH)].iterrows():
  fut=h15[(h15.open_time>=r.entry_ts)&(h15.open_time<r.entry_ts+pd.Timedelta(hours=12))].copy()
  if len(fut)<40:continue
  entry=float(r.entry15); seq=[]
  for _,x in fut.iterrows():
   seq.append((x.open_time,(float(x.high)/entry-1)*100,(float(x.low)/entry-1)*100,(float(x.close)/entry-1)*100,float(x.rsi),float(x.stoch),float(x.macd),pct(float(x.close),float(x.ema20))))
  paths[idx]=seq

FRICTION=.20

def outcome(r,check_h,fail_close,fail_peak,hard_stop):
 seq=paths.get(r.name)
 if not seq:return None
 # Keep V10's +4/-2 logic and 12h window. Only rescue before the normal stop when
 # a trade has both failed to progress and is materially below entry at checkpoint.
 peak=-999.; ncheck=int(check_h*4)
 for j,(ts,hr,lr,cr,rsi,stoch,macd,e20d) in enumerate(seq):
  peak=max(peak,hr)
  if lr<=-hard_stop:return -hard_stop-FRICTION,ts,'STOP',j+1
  if hr>=4.0:return 4.0-FRICTION,ts,'TARGET',j+1
  if j+1==ncheck and peak<fail_peak and cr<=fail_close:
   return cr-FRICTION,ts,'RESCUE',j+1
 # unresolved: exit actual 12h close, rather than V10's near-zero synthetic flat.
 ts,hr,lr,cr,rsi,stoch,macd,e20d=seq[-1]
 return cr-FRICTION,ts,'TIME',len(seq)

def sim(a,pars):
 q=a[a.p10>=TH].sort_values(['entry_ts','p10'],ascending=[True,False]);cap=2500.;peakcap=cap;dd=0.;free=pd.Timestamp.min.tz_localize('UTC');rows=[]
 for idx,r in q.iterrows():
  if r.entry_ts<free:continue
  x=outcome(r,*pars)
  if x is None:continue
  ret,xt,reason,bars=x;cap*=1+ret/100;peakcap=max(peakcap,cap);dd=min(dd,(cap/peakcap-1)*100)
  rows.append([r.entry_ts,xt,r.symbol,ret,reason,bars,r.p10,r.mfe15,r.mae15]);free=xt
 wins=sum(x[3]>0 for x in rows);losses=sum(x[3]<0 for x in rows)
 return dict(trades=len(rows),wins=wins,losses=losses,end=cap,return_pct=(cap/2500-1)*100,maxdd=dd,avg_hold_h=np.mean([x[6]*.25 for x in rows]) if rows else 0),rows

# CAL-only conservative rescue grid. Require activity near V10 rather than solving
# DD by suppressing trades. Stop remains close to V10; rescue only acts at 2-6h.
grid=[]
for check_h in [2,3,4,6]:
 for fail_close in [-.5,-1.0,-1.5]:
  for fail_peak in [.5,1.0,1.5,2.0]:
   for hard_stop in [2.0,2.25,2.5]:
    P=(check_h,fail_close,fail_peak,hard_stop);r,_=sim(cal10,P)
    if r['trades']<18:continue
    dd=abs(r['maxdd']);obj=r['return_pct']-1.25*dd-2.0*max(0,dd-4)+.02*r['trades']
    grid.append(dict(objective=obj,check_h=check_h,fail_close=fail_close,fail_peak=fail_peak,hard_stop=hard_stop,**r))
G=pd.DataFrame(grid).sort_values('objective',ascending=False)
if len(G)==0:raise RuntimeError('no eligible V16 CAL policy')
b=G.iloc[0];P=(float(b.check_h),float(b.fail_close),float(b.fail_peak),float(b.hard_stop))
out=[];rows=[]
for period,a in [('TRAIN',tr10),('CAL',cal10),('TEST',te10)]:
 r,rr=sim(a,P);r.update(period=period,threshold=TH,check_h=P[0],fail_close=P[1],fail_peak=P[2],hard_stop=P[3]);out.append(r)
 for x in rr:rows.append([period,*x])
rob=[]
for _,g in G.head(25).iterrows():
 pp=(float(g.check_h),float(g.fail_close),float(g.fail_peak),float(g.hard_stop));r,_=sim(te10,pp);r.update(cal_objective=float(g.objective),check_h=pp[0],fail_close=pp[1],fail_peak=pp[2],hard_stop=pp[3]);rob.append(r)
pd.DataFrame(out).to_csv('/tmp/v16_summary.csv',index=False);G.head(100).to_csv('/tmp/v16_cal_grid.csv',index=False);pd.DataFrame(rob).to_csv('/tmp/v16_test_robust.csv',index=False);pd.DataFrame(rows,columns=['period','entry_ts','exit_ts','symbol','ret','reason','bars','p10','mfe','mae']).to_csv('/tmp/v16_trades.csv',index=False)
print('TH',TH,'BEST',P,flush=True);print(pd.DataFrame(out).to_string(index=False),flush=True)
