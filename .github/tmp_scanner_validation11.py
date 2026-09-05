# Validation 11: build on V10, keep real 15m retrigger, and attack two weaknesses:
# CAL instability and capital-wasting flat trades. TEST is not used for selection.
exec(open('.github/tmp_scanner_validation10.py').read().split("best=None")[0])

# Additional entry-quality features available at confirmation time only.
for a in (tr10,cal10,te10):
 a['abs_e20']=a.m15_e20.abs(); a['abs_e50']=a.m15_e50.abs()
 a['turn_strength']=a.m15_macdd + .01*(50-a.m15_stoch).clip(-50,50)
 a['htf_strength']=a.f39 + a.f23 + .5*a.f42 + .5*a.f26

# CAL-only search over interpretable confirmation filters + p10 threshold.
def filt(a,th,stmax,e20max,retmax,htfmin):
 return a[(a.p10>=th)&(a.m15_stoch<=stmax)&(a.abs_e20<=e20max)&(a.m15_ret<=retmax)&(a.htf_strength>=htfmin)].copy()

def sim(a,pars):
 q=filt(a,*pars).sort_values(['entry_ts','p10'],ascending=[True,False]);cap=2500.;peak=cap;dd=0.;free=pd.Timestamp.min.tz_localize('UTC');w=l=f=0;rows=[]
 for _,r in q.iterrows():
  if r.entry_ts<free:continue
  if r.b42==1:ret=3.998;w+=1
  elif r.b42==0:ret=-2.002;l+=1
  else:ret=-.002;f+=1
  cap*=1+ret/100;peak=max(peak,cap);dd=min(dd,(cap/peak-1)*100);rows.append([r.entry_ts,r.symbol,ret,r.p10,r.m15_stoch,r.m15_e20,r.m15_ret,r.htf_strength]);free=r.entry_ts+pd.Timedelta(hours=12)
 return dict(trades=len(rows),wins=w,losses=l,flat=f,win_rate=w/max(1,w+l),end=cap,return_pct=(cap/2500-1)*100,maxdd=dd),rows

grid=[]
for th in np.arange(.36,.57,.02):
 for stmax in [45,55,65,75,85,100]:
  for e20max in [1.0,1.5,2.0,3.0,99.0]:
   for retmax in [0.5,1.0,1.5,2.5,99.0]:
    for htfmin in [-99,-2,0,2,5]:
     pars=(float(th),stmax,e20max,retmax,htfmin);r,_=sim(cal10,pars)
     if r['trades']<10:continue
     resolved=r['wins']+r['losses'];flat_rate=r['flat']/r['trades']
     # Prefer capital growth and low DD; penalize excessive flats, require activity.
     obj=r['return_pct']+.55*r['maxdd']+5*r['win_rate']-2*flat_rate+.05*r['trades']
     grid.append(dict(th=th,stmax=stmax,e20max=e20max,retmax=retmax,htfmin=htfmin,objective=obj,**r))
grid=pd.DataFrame(grid).sort_values('objective',ascending=False);best=grid.iloc[0];P=(float(best.th),float(best.stmax),float(best.e20max),float(best.retmax),float(best.htfmin))
out=[];rows=[]
for p,a in [('TRAIN',tr10),('CAL',cal10),('TEST',te10)]:
 r,t=sim(a,P);r.update(period=p,th=P[0],stmax=P[1],e20max=P[2],retmax=P[3],htfmin=P[4]);out.append(r)
 for x in t:rows.append([p,*x])
# Robustness: top 25 CAL choices evaluated on untouched TEST, only after frozen ranking.
rob=[]
for _,g in grid.head(25).iterrows():
 pars=(float(g.th),float(g.stmax),float(g.e20max),float(g.retmax),float(g.htfmin));r,_=sim(te10,pars);r.update(th=pars[0],stmax=pars[1],e20max=pars[2],retmax=pars[3],htfmin=pars[4],cal_objective=float(g.objective));rob.append(r)
pd.DataFrame(out).to_csv('/tmp/v11_summary.csv',index=False);grid.head(100).to_csv('/tmp/v11_cal_grid.csv',index=False);pd.DataFrame(rob).to_csv('/tmp/v11_test_robust.csv',index=False);pd.DataFrame(rows,columns=['period','entry_ts','symbol','ret','p10','stoch15','e20_15','ret15','htf_strength']).to_csv('/tmp/v11_trades.csv',index=False)
print('BEST',P,flush=True);print(pd.DataFrame(out).to_string(index=False),flush=True);print('ROBUST\n',pd.DataFrame(rob).to_string(index=False),flush=True)
