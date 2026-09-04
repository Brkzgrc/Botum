import json,math,time,urllib.parse,urllib.request
import numpy as np,pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import precision_score,recall_score
BASE='https://data-api.binance.vision/api/v3/klines'
SYMS='BTCUSDT ETHUSDT BNBUSDT SOLUSDT XRPUSDT DOGEUSDT ADAUSDT AVAXUSDT LINKUSDT TRXUSDT DOTUSDT LTCUSDT BCHUSDT HBARUSDT XLMUSDT ETCUSDT NEARUSDT APTUSDT ARBUSDT OPUSDT INJUSDT ATOMUSDT FILUSDT RENDERUSDT SEIUSDT TIAUSDT WLDUSDT UNIUSDT AAVEUSDT PENDLEUSDT RAYUSDT ZECUSDT DASHUSDT ZENUSDT ARKUSDT EDUUSDT ZROUSDT FORMUSDT ACHUSDT ENJUSDT'.split()
NOW=pd.Timestamp.now(tz='UTC').floor('1h'); START=NOW-pd.Timedelta(days=90)
def get(q):
 u=BASE+'?'+urllib.parse.urlencode(q)
 for n in range(7):
  try:
   with urllib.request.urlopen(u,timeout=30) as r:return json.loads(r.read())
  except Exception:
   if n==6:raise
   time.sleep(1.2*(n+1))
def fetch_range(sym,tf,start,end):
 step={'1h':3600000,'4h':14400000,'1d':86400000}[tf];cur=int(pd.Timestamp(start).timestamp()*1000);endms=int(pd.Timestamp(end).timestamp()*1000);out=[]
 while cur<endms:
  rows=get({'symbol':sym,'interval':tf,'startTime':cur,'endTime':endms,'limit':1000})
  if not rows:break
  out.extend(rows);nxt=int(rows[-1][0])+step
  if nxt<=cur:break
  cur=nxt;time.sleep(.03)
 return out
def frame(rows):
 c=['open_time','open','high','low','close','volume','close_time','quote_volume','trades','taker_base','taker_quote','ignore'];d=pd.DataFrame(rows,columns=c)
 for x in ['open','high','low','close','volume','quote_volume','taker_quote']:d[x]=pd.to_numeric(d[x],errors='coerce')
 d.open_time=pd.to_datetime(d.open_time,unit='ms',utc=True);d.close_time=pd.to_datetime(d.close_time,unit='ms',utc=True);return d.drop_duplicates('open_time').dropna().reset_index(drop=True)
def rsi(s,n=14):
 z=s.diff();g=z.clip(lower=0);l=-z.clip(upper=0);ag=g.ewm(alpha=1/n,adjust=False,min_periods=n).mean();al=l.ewm(alpha=1/n,adjust=False,min_periods=n).mean();return (100-100/(1+ag/al.replace(0,np.nan))).fillna(50)
def ind(x):
 x=x.copy();c=x.close
 for n in (20,50,200):x[f'ema{n}']=c.ewm(span=n,adjust=False).mean()
 x['rsi']=rsi(c);lo=x.rsi.rolling(14).min();hi=x.rsi.rolling(14).max();raw=100*(x.rsi-lo)/(hi-lo).replace(0,np.nan);x['stoch']=raw.rolling(3).mean().fillna(50)
 e12=c.ewm(span=12,adjust=False).mean();e26=c.ewm(span=26,adjust=False).mean();x['macd']=(e12-e26)-(e12-e26).ewm(span=9,adjust=False).mean();x['obv']=(np.sign(c.diff()).fillna(0)*x.volume).cumsum();x['volr']=x.volume/x.volume.rolling(20).mean().replace(0,np.nan);return x
def pct(a,b):return (a/b-1)*100 if b else 0
def snap(x,ts):
 z=x[x.close_time<ts]
 if len(z)<205:return None
 a=z.iloc[-1];b=z.iloc[-2];p=float(a.close);rg=float(a.high-a.low)
 return [pct(p,float(a.ema20)),pct(p,float(a.ema50)),pct(p,float(a.ema200)),pct(float(a.ema20),float(z.ema20.iloc[-4])),pct(float(a.ema50),float(z.ema50.iloc[-4])),float(a.rsi),float(a.stoch),float(a.macd),float(a.macd-b.macd),pct(p,float(z.close.iloc[-4])),pct(p,float(z.close.iloc[-7])),pct(p,float(z.close.iloc[-25])),1.0 if float(a.obv)>=float(z.obv.iloc[-6]) else 0.0,float(a.volr) if math.isfinite(float(a.volr)) else 1.0,(float(a.high)-max(float(a.open),float(a.close)))/rg if rg else 0,(min(float(a.open),float(a.close))-float(a.low))/rg if rg else 0,p]
rows=[];warm=START-pd.Timedelta(days=230)
for i,s in enumerate(SYMS,1):
 try:
  print('FETCH',i,len(SYMS),s,flush=True);h1=ind(frame(fetch_range(s,'1h',warm,NOW+pd.Timedelta(hours=2))));h4=ind(frame(fetch_range(s,'4h',warm,NOW+pd.Timedelta(hours=2))));d1=ind(frame(fetch_range(s,'1d',warm,NOW+pd.Timedelta(hours=2))))
 except Exception as e:print('SKIP',s,e,flush=True);continue
 for ts in pd.date_range(START,NOW-pd.Timedelta(hours=13),freq='4h'):
  o=snap(h1,ts);f=snap(h4,ts);d=snap(d1,ts)
  if not o or not f or not d:continue
  fut=h1[(h1.open_time>=ts)&(h1.open_time<ts+pd.Timedelta(hours=12))]
  if len(fut)<12:continue
  entry=o[-1];hi2=float(fut.iloc[:2].high.max());hi6=float(fut.iloc[:6].high.max());hi12=float(fut.high.max());lo12=float(fut.low.min())
  mfe2=(hi2/entry-1)*100;mfe6=(hi6/entry-1)*100;mfe12=(hi12/entry-1)*100;mae12=(lo12/entry-1)*100
  # target-before-stop for +2% / -3% within 12h
  outcome=0
  for _,r in fut.iterrows():
   hitT=float(r.high)>=entry*1.02;hitS=float(r.low)<=entry*.97
   if hitT and hitS: outcome=-1;break
   if hitT:outcome=1;break
   if hitS:outcome=-1;break
  feat=d[:-1]+f[:-1]+o[:-1]
  rows.append([ts,s,entry,mfe2,mfe6,mfe12,mae12,int(mfe2>=1.5),int(mfe6>=1.5),int(mfe12>=1.5),int(mfe12>=3),int(outcome==1)]+feat)
cols=['ts','symbol','entry','mfe2','mfe6','mfe12','mae12','hit2_15','hit6_15','hit12_15','hit12_3','tb2']+[f'f{i}' for i in range(48)]
df=pd.DataFrame(rows,columns=cols);df.to_csv('/tmp/v3_rows.csv',index=False)
train_end=START+pd.Timedelta(days=45);cal_end=START+pd.Timedelta(days=60);tr=df[df.ts<train_end];cal=df[(df.ts>=train_end)&(df.ts<cal_end)];te=df[df.ts>=cal_end];X=[f'f{i}' for i in range(48)]
summary=[]
for target in ['hit12_15','hit12_3','tb2']:
 model=HistGradientBoostingClassifier(max_iter=180,max_leaf_nodes=15,learning_rate=.06,l2_regularization=2.0,random_state=7).fit(tr[X],tr[target])
 pc=model.predict_proba(cal[X])[:,1]
 # choose threshold on CAL only, requiring at least 5% coverage; objective precision then recall
 best=None
 for th in np.arange(.40,.91,.02):
  m=pc>=th;n=int(m.sum());cov=n/max(1,len(cal))
  if n<60 or cov<.05:continue
  prec=float(cal.loc[m,target].mean());rec=float(cal.loc[m,target].sum()/max(1,cal[target].sum()));score=prec+.15*rec
  if best is None or score>best[0]:best=(score,th,n,prec,rec)
 if best is None:best=(0,.5,0,0,0)
 th=best[1]
 for period,z in [('TRAIN',tr),('CAL',cal),('TEST',te)]:
  p=model.predict_proba(z[X])[:,1];m=p>=th;n=int(m.sum())
  summary.append(dict(target=target,period=period,n_total=len(z),selected=n,coverage=n/max(1,len(z)),threshold=th,precision=float(z.loc[m,target].mean()) if n else None,recall=float(z.loc[m,target].sum()/max(1,z[target].sum())) if n else 0,baseline=float(z[target].mean()),avg_mfe12=float(z.loc[m,'mfe12'].mean()) if n else None,avg_mae12=float(z.loc[m,'mae12'].mean()) if n else None))
 # top decile sanity check on TEST independent of threshold
 pt=model.predict_proba(te[X])[:,1];q=np.quantile(pt,.90);m=pt>=q
 summary.append(dict(target=target,period='TEST_TOP10',n_total=len(te),selected=int(m.sum()),coverage=float(m.mean()),threshold=float(q),precision=float(te.loc[m,target].mean()),recall=float(te.loc[m,target].sum()/max(1,te[target].sum())),baseline=float(te[target].mean()),avg_mfe12=float(te.loc[m,'mfe12'].mean()),avg_mae12=float(te.loc[m,'mae12'].mean())))
pd.DataFrame(summary).to_csv('/tmp/v3_summary.csv',index=False);print(pd.DataFrame(summary).to_string(index=False),flush=True)
print('ROWS',len(df),'TRAIN',len(tr),'CAL',len(cal),'TEST',len(te),flush=True)
