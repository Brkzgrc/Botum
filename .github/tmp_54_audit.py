import time, urllib.parse, urllib.request, json
import pandas as pd
import numpy as np
CASES='.github/tmp_54_cases.csv'; BASE='https://api.binance.com/api/v3/klines'
def fetch(s,tf,end):
 q=urllib.parse.urlencode({'symbol':s,'interval':tf,'endTime':end,'limit':1000})
 for n in range(6):
  try:
   with urllib.request.urlopen(BASE+'?'+q,timeout=30) as r:return json.loads(r.read())
  except Exception:
   if n==5: raise
   time.sleep(2*(n+1))
def frame(rows):
 c=['open_time','open','high','low','close','volume','close_time','quote_volume','trades','taker_base','taker_quote','ignore'];d=pd.DataFrame(rows,columns=c)
 for x in ['open','high','low','close','volume','quote_volume']:d[x]=pd.to_numeric(d[x])
 d.open_time=pd.to_datetime(d.open_time,unit='ms',utc=True);d.close_time=pd.to_datetime(d.close_time,unit='ms',utc=True);return d
def rsi(s,n=14):
 d=s.diff();u=d.clip(lower=0);dn=-d.clip(upper=0);au=u.ewm(alpha=1/n,adjust=False,min_periods=n).mean();ad=dn.ewm(alpha=1/n,adjust=False,min_periods=n).mean();return 100-100/(1+au/ad.replace(0,np.nan))
def ind(d):
 c=d.close
 for n in (20,50,200):d[f'ema{n}']=c.ewm(span=n,adjust=False).mean()
 d['rsi14']=rsi(c);lo=d.rsi14.rolling(14).min();hi=d.rsi14.rolling(14).max();sr=100*(d.rsi14-lo)/(hi-lo).replace(0,np.nan);d['stoch_k']=sr.rolling(3).mean();d['stoch_d']=d.stoch_k.rolling(3).mean();e12=c.ewm(span=12,adjust=False).mean();e26=c.ewm(span=26,adjust=False).mean();d['macd']=e12-e26;d['macd_sig']=d.macd.ewm(span=9,adjust=False).mean();d['macd_hist']=d.macd-d.macd_sig;pc=c.shift();tr=pd.concat([d.high-d.low,(d.high-pc).abs(),(d.low-pc).abs()],axis=1).max(axis=1);d['atr14']=tr.ewm(alpha=1/14,adjust=False,min_periods=14).mean();d['obv']=(np.sign(c.diff()).fillna(0)*d.volume).cumsum();d['vol_ma20']=d.volume.rolling(20).mean();return d
def snap(d,ts,p):
 z=d[d.close_time<ts];x=z.iloc[-1];pr=z.iloc[-2];o={}
 def put(k,v):o[p+'_'+k]=None if pd.isna(v) else float(v)
 put('close',x.close)
 for n in (20,50,200):put(f'ema{n}_dist_pct',(x.close/x[f'ema{n}']-1)*100);put(f'ema{n}_slope3_pct',(x[f'ema{n}']/z.iloc[-4][f'ema{n}']-1)*100)
 put('rsi14',x.rsi14);put('rsi_delta',x.rsi14-pr.rsi14);put('stoch_k',x.stoch_k);put('stoch_d',x.stoch_d);put('stoch_k_delta',x.stoch_k-pr.stoch_k);put('macd_hist',x.macd_hist);put('macd_hist_delta',x.macd_hist-pr.macd_hist);put('atr_pct',x.atr14/x.close*100);put('vol_ratio20',x.volume/x.vol_ma20);put('obv_delta5_pct',(x.obv/z.iloc[-6].obv-1)*100 if z.iloc[-6].obv else np.nan)
 for n in (3,6,24):put(f'ret{n}_pct',(x.close/z.iloc[-1-n].close-1)*100)
 put('dist_high20_pct',(x.close/z.high.tail(20).max()-1)*100);put('dist_low20_pct',(x.close/z.low.tail(20).min()-1)*100);rg=x.high-x.low;put('body_range',abs(x.close-x.open)/rg if rg else 0);put('upper_wick',(x.high-max(x.open,x.close))/rg if rg else 0);put('lower_wick',(min(x.open,x.close)-x.low)/rg if rg else 0);return o
cases=pd.read_csv(CASES);cases['timestamp_utc']=pd.to_datetime(cases.timestamp_utc,utc=True);end=int(cases.timestamp_utc.max().timestamp()*1000);cache={}
for i,s in enumerate(sorted(cases.symbol.unique()),1):
 print('FETCH',i,cases.symbol.nunique(),s,flush=True)
 for tf in ('1h','4h','1d'):cache[(s,tf)]=ind(frame(fetch(s,tf,end)))
rows=[]
for _,c in cases.iterrows():
 r=c.to_dict()
 for tf in ('1h','4h','1d'):r.update(snap(cache[(c.symbol,tf)],c.timestamp_utc,tf))
 rows.append(r)
out=pd.DataFrame(rows);out.to_csv('/tmp/audit_features.csv',index=False);label='hit_1_5pct';feats=[c for c in out if c.startswith(('1h_','4h_','1d_')) and pd.api.types.is_numeric_dtype(out[c])]
s=[]
for f in feats:
 a=out[out[label]==True][f].dropna();b=out[out[label]==False][f].dropna()
 if len(a)<5 or len(b)<5:continue
 pool=np.sqrt((a.var(ddof=1)+b.var(ddof=1))/2);eff=(a.mean()-b.mean())/pool if pool and np.isfinite(pool) else np.nan;s.append((f,len(a),a.mean(),b.mean(),a.median(),b.median(),eff))
sumdf=pd.DataFrame(s,columns=['feature','win_n','win_mean','loss_mean','win_median','loss_median','effect_d']).sort_values('effect_d',key=lambda x:x.abs(),ascending=False);sumdf.to_csv('/tmp/feature_comparison.csv',index=False)
rules=[]
for f in feats:
 vals=out[f].dropna()
 if len(vals)<30:continue
 for q in (.2,.3,.4,.5,.6,.7,.8):
  t=float(vals.quantile(q))
  for op in ('>=','<='):
   m=(out[f]>=t) if op=='>=' else (out[f]<=t);n=int(m.sum())
   if n>=8:rules.append((f,op,t,n,float(out.loc[m,label].mean()),float(out.loc[m,'clean_hit_1_5pct'].mean()),float(out.loc[m,'hit_3pct'].mean())))
rdf=pd.DataFrame(rules,columns=['feature','op','threshold','n','hit15','clean15','hit3']).sort_values(['hit15','n'],ascending=[False,False]);rdf.to_csv('/tmp/threshold_rules.csv',index=False)
conds=[]
for f in list(dict.fromkeys(rdf.head(30).feature.tolist()))[:12]:
 for _,x in rdf[rdf.feature==f].head(4).iterrows():conds.append((f,x.op,float(x.threshold)))
pairs=[]
for i,a in enumerate(conds):
 for b in conds[i+1:]:
  if a[0]==b[0]:continue
  ma=(out[a[0]]>=a[2]) if a[1]=='>=' else (out[a[0]]<=a[2]);mb=(out[b[0]]>=b[2]) if b[1]=='>=' else (out[b[0]]<=b[2]);m=ma&mb;n=int(m.sum())
  if n>=8:pairs.append((*a,*b,n,float(out.loc[m,label].mean()),float(out.loc[m,'clean_hit_1_5pct'].mean()),float(out.loc[m,'hit_3pct'].mean())))
pdf=pd.DataFrame(pairs,columns=['f1','op1','t1','f2','op2','t2','n','hit15','clean15','hit3']).sort_values(['hit15','n'],ascending=[False,False]);pdf.to_csv('/tmp/pair_rules.csv',index=False)
text='OVERALL\n'+out[[label,'clean_hit_1_5pct','hit_3pct']].mean().to_string()+'\n\nTOP EFFECTS\n'+sumdf.head(25).to_string(index=False)+'\n\nTOP SINGLE RULES\n'+rdf.head(30).to_string(index=False)+'\n\nTOP PAIR RULES\n'+pdf.head(30).to_string(index=False)+'\n\nBY SONNET\n'+out.groupby('sonnet_decision')[[label,'clean_hit_1_5pct','hit_3pct']].mean().to_string();open('/tmp/summary.txt','w').write(text);print(text)
