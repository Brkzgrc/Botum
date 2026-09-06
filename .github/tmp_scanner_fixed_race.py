# Fixed-window fair race: V10, V14, V16, V17, V18.
# Window is injected by the workflow into validation3 for this runner only.
# Every version uses the same candles and 0.20 percentage-point round-trip cost.
exec(open('.github/tmp_scanner_validation10.py').read().split("out=[];rows=[]")[0])

COST=.20
PERIODS=[('TRAIN',tr10),('CAL',cal10),('TEST',te10)]
all_summary=[]
all_trades=[]
all_params=[]

def sim10_fair(a):
 q=a[a.p10>=TH].sort_values(['entry_ts','p10'],ascending=[True,False])
 cap=2500.;peak=cap;dd=0.;free=pd.Timestamp.min.tz_localize('UTC');w=l=f=0;rows=[]
 for idx,r in q.iterrows():
  if r.entry_ts<free:continue
  if r.b42==1:ret=4.0-COST;w+=1;kind='W'
  elif r.b42==0:ret=-2.0-COST;l+=1;kind='L'
  else:ret=-COST;f+=1;kind='F'
  cap*=1+ret/100;peak=max(peak,cap);dd=min(dd,(cap/peak-1)*100)
  rows.append([r.entry_ts,r.symbol,ret,kind,float(r.p10)])
  free=r.entry_ts+pd.Timedelta(hours=12)
 return dict(trades=len(rows),wins=w,losses=l,flat=f,end=cap,return_pct=(cap/2500-1)*100,maxdd=dd,avg_exposure=1.0),rows

for period,a in PERIODS:
 r,rr=sim10_fair(a);r.update(version='V10',period=period,threshold=TH,policy='4_2_fixed12h');all_summary.append(r)
 for x in rr:all_trades.append(['V10',period,*x])
all_params.append(dict(version='V10',threshold=TH,policy='4_2_fixed12h'))

def before14(r,policy):
 if policy=='3_2':return r.b32,3.0-COST,-2.0-COST
 if policy=='4_2':return r.b42,4.0-COST,-2.0-COST
 return r.b525,5.0-COST,-2.5-COST

def sim14(a,th,policy):
 q=a[a.p10>=th].sort_values(['entry_ts','p10'],ascending=[True,False])
 cap=2500.;peak=cap;dd=0.;free=pd.Timestamp.min.tz_localize('UTC');w=l=f=0;rows=[]
 for idx,r in q.iterrows():
  if r.entry_ts<free:continue
  b,wr,lr=before14(r,policy)
  if b==1:ret=wr;w+=1;kind='W'
  elif b==0:ret=lr;l+=1;kind='L'
  else:ret=-COST;f+=1;kind='F'
  cap*=1+ret/100;peak=max(peak,cap);dd=min(dd,(cap/peak-1)*100)
  rows.append([r.entry_ts,r.symbol,ret,kind,float(r.p10)])
  free=r.entry_ts+pd.Timedelta(hours=12)
 return dict(trades=len(rows),wins=w,losses=l,flat=f,end=cap,return_pct=(cap/2500-1)*100,maxdd=dd,avg_exposure=1.0),rows

grid14=[]
for th14 in np.arange(.40,.71,.01):
 for pol14 in ['3_2','4_2','5_25']:
  r,_=sim14(cal10,float(th14),pol14)
  if r['trades']<18:continue
  dd=abs(r['maxdd']);wr=r['wins']/max(1,r['wins']+r['losses']);fr=r['flat']/max(1,r['trades'])
  obj=r['return_pct']-(1.5*dd+2.0*max(0,dd-4.0))-fr+.75*wr+.015*r['trades']
  grid14.append(dict(objective=obj,threshold=th14,policy=pol14,**r))
G14=pd.DataFrame(grid14).sort_values('objective',ascending=False)
if len(G14)==0:raise RuntimeError('no eligible fixed V14 policy')
b14=G14.iloc[0];P14=(float(b14.threshold),str(b14.policy))
for period,a in PERIODS:
 r,rr=sim14(a,*P14);r.update(version='V14',period=period,threshold=P14[0],policy=P14[1]);all_summary.append(r)
 for x in rr:all_trades.append(['V14',period,*x])
all_params.append(dict(version='V14',threshold=P14[0],policy=P14[1]))

# Build the exact 15m paths once for V16/V17/V18.
v10_rows=pd.concat([tr10,cal10,te10],axis=0)
paths={}
for i,s in enumerate(SYMS,1):
 print('RACE PATH',i,len(SYMS),s,flush=True)
 try:h15=fetch15(s,START-pd.Timedelta(days=4),NOW+pd.Timedelta(hours=14))
 except Exception as e:print('PATH SKIP',s,e,flush=True);continue
 for idx,r in v10_rows[(v10_rows.symbol==s)&(v10_rows.p10>=TH)].iterrows():
  fut=h15[(h15.open_time>=r.entry_ts)&(h15.open_time<r.entry_ts+pd.Timedelta(hours=12))].copy()
  if len(fut)<40:continue
  entry=float(r.entry15);seq=[]
  for _,x in fut.iterrows():
   seq.append((x.open_time,(float(x.high)/entry-1)*100,(float(x.low)/entry-1)*100,(float(x.close)/entry-1)*100))
  paths[idx]=seq

def path_outcome(r,check_h,fail_close,fail_peak,hard_stop):
 seq=paths.get(r.name)
 if not seq:return None
 peak=-999.;ncheck=int(check_h*4)
 for j,(ts,hr,lr,cr) in enumerate(seq):
  peak=max(peak,hr)
  if lr<=-hard_stop:return -hard_stop-COST,ts,'STOP',j+1
  if hr>=4.0:return 4.0-COST,ts,'TARGET',j+1
  if j+1==ncheck and peak<fail_peak and cr<=fail_close:return cr-COST,ts,'RESCUE',j+1
 ts,hr,lr,cr=seq[-1]
 return cr-COST,ts,'TIME',len(seq)

def sim16(a,pars):
 q=a[a.p10>=TH].sort_values(['entry_ts','p10'],ascending=[True,False])
 cap=2500.;peak=cap;dd=0.;free=pd.Timestamp.min.tz_localize('UTC');rows=[]
 for idx,r in q.iterrows():
  if r.entry_ts<free:continue
  x=path_outcome(r,*pars)
  if x is None:continue
  ret,xt,reason,bars=x;cap*=1+ret/100;peak=max(peak,cap);dd=min(dd,(cap/peak-1)*100)
  rows.append([r.entry_ts,r.symbol,ret,reason,float(r.p10)]);free=xt
 w=sum(x[2]>0 for x in rows);l=sum(x[2]<0 for x in rows)
 return dict(trades=len(rows),wins=w,losses=l,flat=0,end=cap,return_pct=(cap/2500-1)*100,maxdd=dd,avg_exposure=1.0),rows

grid16=[]
for ch in [2,3,4,6]:
 for fc in [-.5,-1.0,-1.5]:
  for fp in [.5,1.0,1.5,2.0]:
   for hs in [2.0,2.25,2.5]:
    P=(ch,fc,fp,hs);r,_=sim16(cal10,P)
    if r['trades']<18:continue
    risk=abs(r['maxdd']);obj=r['return_pct']-1.25*risk-2.0*max(0,risk-4)+.02*r['trades']
    grid16.append(dict(objective=obj,check_h=ch,fail_close=fc,fail_peak=fp,hard_stop=hs,**r))
G16=pd.DataFrame(grid16).sort_values('objective',ascending=False)
if len(G16)==0:raise RuntimeError('no eligible fixed V16 policy')
b16=G16.iloc[0];P16=(float(b16.check_h),float(b16.fail_close),float(b16.fail_peak),float(b16.hard_stop))
for period,a in PERIODS:
 r,rr=sim16(a,P16);r.update(version='V16',period=period,threshold=TH,policy=str(P16));all_summary.append(r)
 for x in rr:all_trades.append(['V16',period,*x])
all_params.append(dict(version='V16',threshold=TH,policy=str(P16)))

BASE=(2.0,-99.0,999.0,2.0)
def sim17(a,pars):
 dd_trigger,reduced_exposure,loss_trigger,cooldown_h=pars
 q=a[a.p10>=TH].sort_values(['entry_ts','p10'],ascending=[True,False])
 cap=2500.;peak=cap;dd=0.;free=pd.Timestamp.min.tz_localize('UTC');cool=pd.Timestamp.min.tz_localize('UTC')
 streak=0;defensive=False;rows=[]
 for idx,r in q.iterrows():
  if r.entry_ts<free or r.entry_ts<cool:continue
  x=path_outcome(r,*BASE)
  if x is None:continue
  raw,xt,reason,bars=x;curdd=(cap/peak-1)*100
  exposure=reduced_exposure if defensive or curdd<=-dd_trigger else 1.0
  ret=raw*exposure;cap*=1+ret/100;peak=max(peak,cap);dd=min(dd,(cap/peak-1)*100)
  rows.append([r.entry_ts,r.symbol,ret,reason,float(r.p10),exposure]);free=xt
  if raw<=-1.0:
   streak+=1
   if streak>=loss_trigger:
    defensive=True
    if cooldown_h>0:cool=xt+pd.Timedelta(hours=cooldown_h)
  elif raw>=.25:streak=0;defensive=False
 w=sum(x[2]>0 for x in rows);l=sum(x[2]<0 for x in rows)
 return dict(trades=len(rows),wins=w,losses=l,flat=0,end=cap,return_pct=(cap/2500-1)*100,maxdd=dd,avg_exposure=float(np.mean([x[5] for x in rows])) if rows else 0),rows

grid17=[]
for dt in [1.5,2.0,3.0,4.0]:
 for re in [.35,.50,.65,.80]:
  for lt in [1,2,3]:
   for cd in [0,6,12,24]:
    P=(dt,re,lt,cd);r,_=sim17(cal10,P)
    if r['trades']<18:continue
    risk=abs(r['maxdd']);obj=r['return_pct']-1.75*risk-3.0*max(0,risk-4)+.01*r['trades']
    grid17.append(dict(objective=obj,dd_trigger=dt,reduced_exposure=re,loss_trigger=lt,cooldown_h=cd,**r))
G17=pd.DataFrame(grid17).sort_values('objective',ascending=False)
if len(G17)==0:raise RuntimeError('no eligible fixed V17 policy')
b17=G17.iloc[0];P17=(float(b17.dd_trigger),float(b17.reduced_exposure),int(b17.loss_trigger),float(b17.cooldown_h))
for period,a in PERIODS:
 r,rr=sim17(a,P17);r.update(version='V17',period=period,threshold=TH,policy=str(P17));all_summary.append(r)
 for x in rr:all_trades.append(['V17',period,*x[:5]])
all_params.append(dict(version='V17',threshold=TH,policy=str(P17)))

def sim18(a,pars):
 dd_trigger,reduced_exposure,loss_trigger,defensive_trades=pars
 q=a[a.p10>=TH].sort_values(['entry_ts','p10'],ascending=[True,False])
 cap=2500.;peak=cap;dd=0.;free=pd.Timestamp.min.tz_localize('UTC');streak=0;risk_left=0;rows=[]
 for idx,r in q.iterrows():
  if r.entry_ts<free:continue
  x=path_outcome(r,*BASE)
  if x is None:continue
  raw,xt,reason,bars=x;curdd=(cap/peak-1)*100
  defensive=(risk_left>0 or curdd<=-dd_trigger);exposure=reduced_exposure if defensive else 1.0
  ret=raw*exposure;cap*=1+ret/100;peak=max(peak,cap);dd=min(dd,(cap/peak-1)*100)
  rows.append([r.entry_ts,r.symbol,ret,reason,float(r.p10),exposure]);free=r.entry_ts+pd.Timedelta(hours=12)
  if defensive and risk_left>0:risk_left-=1
  if raw<=-1.0:
   streak+=1
   if streak>=loss_trigger:risk_left=max(risk_left,defensive_trades)
  elif raw>=.25:streak=0
 w=sum(x[2]>0 for x in rows);l=sum(x[2]<0 for x in rows)
 return dict(trades=len(rows),wins=w,losses=l,flat=0,end=cap,return_pct=(cap/2500-1)*100,maxdd=dd,avg_exposure=float(np.mean([x[5] for x in rows])) if rows else 0),rows

grid18=[]
for dt in [1.5,2.0,3.0,4.0,999.0]:
 for re in [.50,.65,.80]:
  for lt in [1,2,3]:
   for nt in [1,2,3]:
    P=(dt,re,lt,nt);r,_=sim18(cal10,P)
    if r['trades']<18:continue
    risk=abs(r['maxdd']);obj=r['return_pct']-1.5*risk-3.0*max(0,risk-4)+.02*r['trades']
    grid18.append(dict(objective=obj,dd_trigger=dt,reduced_exposure=re,loss_trigger=lt,defensive_trades=nt,**r))
G18=pd.DataFrame(grid18).sort_values('objective',ascending=False)
if len(G18)==0:raise RuntimeError('no eligible fixed V18 policy')
b18=G18.iloc[0];P18=(float(b18.dd_trigger),float(b18.reduced_exposure),int(b18.loss_trigger),int(b18.defensive_trades))
for period,a in PERIODS:
 r,rr=sim18(a,P18);r.update(version='V18',period=period,threshold=TH,policy=str(P18));all_summary.append(r)
 for x in rr:all_trades.append(['V18',period,*x[:5]])
all_params.append(dict(version='V18',threshold=TH,policy=str(P18)))

S=pd.DataFrame(all_summary)
test=S[S.period=='TEST'].copy()
cal=S[S.period=='CAL'][['version','return_pct','maxdd']].rename(columns={'return_pct':'cal_return','maxdd':'cal_maxdd'})
rank=test.merge(cal,on='version')
rank['return_dd']=rank.return_pct/rank.maxdd.abs().replace(0,np.nan)
rank['all_periods_positive']=rank.version.map(S.groupby('version').return_pct.min()>0)
rank=rank.sort_values(['all_periods_positive','return_dd','return_pct'],ascending=[False,False,False])
rank.insert(0,'rank',range(1,len(rank)+1))

S.to_csv('/tmp/fixed_race_summary.csv',index=False)
rank.to_csv('/tmp/fixed_race_ranking.csv',index=False)
pd.DataFrame(all_params).to_csv('/tmp/fixed_race_params.csv',index=False)
pd.DataFrame(all_trades,columns=['version','period','entry_ts','symbol','ret','kind','p10']).to_csv('/tmp/fixed_race_trades.csv',index=False)
G14.head(50).to_csv('/tmp/fixed_race_v14_cal.csv',index=False)
G16.head(50).to_csv('/tmp/fixed_race_v16_cal.csv',index=False)
G17.head(50).to_csv('/tmp/fixed_race_v17_cal.csv',index=False)
G18.head(50).to_csv('/tmp/fixed_race_v18_cal.csv',index=False)
print('FIXED WINDOW',START,NOW,'COST',COST,flush=True)
print(S.to_string(index=False),flush=True)
print('RANKING',flush=True)
print(rank.to_string(index=False),flush=True)
