# Validation 14: preserve V10 opportunity model and explicitly optimize CAL for
# lower drawdown without destroying return. TEST remains untouched until frozen.
exec(open('.github/tmp_scanner_validation10.py').read().split("best=None")[0])

# V10 model is unchanged. Only the CAL probability threshold and fixed target/stop
# pair are selected. This directly tests whether V10's ~4% TEST DD can be reduced
# while retaining its economic edge, without V11/V12-style extra entry filters.

def before_row(r,policy):
 if policy=='3_2': return r.b32,2.998,-2.002
 if policy=='4_2': return r.b42,3.998,-2.002
 return r.b525,4.998,-2.502

def sim(a,th,policy):
 q=a[a.p10>=th].sort_values(['entry_ts','p10'],ascending=[True,False])
 cap=2500.;peak=cap;dd=0.;free=pd.Timestamp.min.tz_localize('UTC');w=l=f=0;rows=[]
 for _,r in q.iterrows():
  if r.entry_ts<free:continue
  b,wr,lr=before_row(r,policy)
  if b==1:ret=wr;w+=1;kind='W'
  elif b==0:ret=lr;l+=1;kind='L'
  else:ret=-.002;f+=1;kind='F'
  cap*=1+ret/100;peak=max(peak,cap);dd=min(dd,(cap/peak-1)*100)
  rows.append([r.entry_ts,r.symbol,ret,kind,r.p10,r.mfe15,r.mae15])
  free=r.entry_ts+pd.Timedelta(hours=12)
 return dict(trades=len(rows),wins=w,losses=l,flat=f,win_rate=w/max(1,w+l),end=cap,return_pct=(cap/2500-1)*100,maxdd=dd),rows

# CAL-only Pareto search. Return remains primary, but drawdown above 4% receives
# an explicit penalty. Activity floor prevents a V11-like solution that obtains
# low DD simply by almost never trading.
grid=[]
for th in np.arange(.40,.71,.01):
 for pol in ['3_2','4_2','5_25']:
  r,_=sim(cal10,float(th),pol)
  if r['trades']<18:continue
  dd=abs(r['maxdd']);flat_rate=r['flat']/max(1,r['trades'])
  dd_pen=1.5*dd + 2.0*max(0,dd-4.0)
  obj=r['return_pct']-dd_pen-1.0*flat_rate+.75*r['win_rate']+.015*r['trades']
  grid.append(dict(th=th,policy=pol,objective=obj,dd_abs=dd,**r))
G=pd.DataFrame(grid).sort_values('objective',ascending=False)
if len(G)==0:raise RuntimeError('V14 CAL grid produced no eligible configuration')
b=G.iloc[0];P=(float(b.th),str(b.policy))

out=[];rows=[]
for period,a in [('TRAIN',tr10),('CAL',cal10),('TEST',te10)]:
 r,t=sim(a,*P);r.update(period=period,threshold=P[0],policy=P[1]);out.append(r)
 for x in t:rows.append([period,*x])

# Frozen-choice robustness diagnostic. TEST does not select the reported result.
rob=[]
for _,g in G.head(30).iterrows():
 pars=(float(g.th),str(g.policy));r,_=sim(te10,*pars)
 r.update(th=pars[0],policy=pars[1],cal_objective=float(g.objective),cal_return=float(g.return_pct),cal_dd=float(g.maxdd));rob.append(r)

pd.DataFrame(out).to_csv('/tmp/v14_summary.csv',index=False)
G.head(100).to_csv('/tmp/v14_cal_grid.csv',index=False)
pd.DataFrame(rob).to_csv('/tmp/v14_test_robust.csv',index=False)
pd.DataFrame(rows,columns=['period','entry_ts','symbol','ret','kind','p10','mfe','mae']).to_csv('/tmp/v14_trades.csv',index=False)
print('BEST',P,flush=True)
print(pd.DataFrame(out).to_string(index=False),flush=True)
print('ROBUST\n',pd.DataFrame(rob).to_string(index=False),flush=True)
