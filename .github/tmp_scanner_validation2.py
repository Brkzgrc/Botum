import json,math,time,urllib.parse,urllib.request,itertools
import numpy as np,pandas as pd
BASE='https://data-api.binance.vision/api/v3/klines'
SYMS='BTCUSDT ETHUSDT BNBUSDT SOLUSDT XRPUSDT DOGEUSDT ADAUSDT AVAXUSDT LINKUSDT TRXUSDT DOTUSDT LTCUSDT BCHUSDT HBARUSDT XLMUSDT ETCUSDT NEARUSDT APTUSDT ARBUSDT OPUSDT INJUSDT ATOMUSDT FILUSDT RENDERUSDT SEIUSDT TIAUSDT WLDUSDT UNIUSDT AAVEUSDT PENDLEUSDT RAYUSDT ZECUSDT DASHUSDT ZENUSDT ARKUSDT EDUUSDT ZROUSDT FORMUSDT ACHUSDT ENJUSDT'.split()
NOW=pd.Timestamp.now(tz='UTC').floor('1h');START=NOW-pd.Timedelta(days=60)
def fetch(s,tf,limit=1000,end=None):
 q={'symbol':s,'interval':tf,'limit':limit};
 if end is not None:q['endTime']=int(pd.Timestamp(end).timestamp()*1000)
 u=BASE+'?'+urllib.parse.urlencode(q)
 for n in range(6):
  try:
   with urllib.request.urlopen(u,timeout=30) as r:return json.loads(r.read())
  except Exception:
   if n==5:raise
   time.sleep(1+n)
def frame(rows):
 c=['open_time','open','high','low','close','volume','close_time','quote_volume','trades','taker_base','taker_quote','ignore'];d=pd.DataFrame(rows,columns=c)
 for x in ['open','high','low','close','volume','quote_volume','taker_quote']:d[x]=pd.to_numeric(d[x],errors='coerce')
 d.open_time=pd.to_datetime(d.open_time,unit='ms',utc=True);d.close_time=pd.to_datetime(d.close_time,unit='ms',utc=True);return d.dropna().reset_index(drop=True)
def rsi(s,n=14):
 z=s.diff();g=z.clip(lower=0);l=-z.clip(upper=0);ag=g.ewm(alpha=1/n,adjust=False,min_periods=n).mean();al=l.ewm(alpha=1/n,adjust=False,min_periods=n).mean();return (100-100/(1+ag/al.replace(0,np.nan))).fillna(50)
def ind(x):
 x=x.copy();c=x.close
 for n in (20,50,200):x[f'ema{n}']=c.ewm(span=n,adjust=False).mean()
 x['rsi']=rsi(c);lo=x.rsi.rolling(14).min();hi=x.rsi.rolling(14).max();raw=100*(x.rsi-lo)/(hi-lo).replace(0,np.nan);x['stoch_k']=raw.rolling(3).mean().fillna(50)
 e12=c.ewm(span=12,adjust=False).mean();e26=c.ewm(span=26,adjust=False).mean();x['macd_hist']=(e12-e26)-(e12-e26).ewm(span=9,adjust=False).mean();x['obv']=(np.sign(c.diff()).fillna(0)*x.volume).cumsum();x['vol_ratio']=x.volume/x.volume.rolling(20).mean().replace(0,np.nan);return x
def pct(a,b):return (a/b-1)*100 if b else 0
def snap(x,ts):
 z=x[x.close_time<ts]
 if len(z)<205:return None
 a=z.iloc[-1];p=float(a.close);rg=float(a.high-a.low)
 return dict(dist20=pct(p,float(a.ema20)),dist50=pct(p,float(a.ema50)),s20=pct(float(a.ema20),float(z.ema20.iloc[-4])),s50=pct(float(a.ema50),float(z.ema50.iloc[-4])),rsi=float(a.rsi),stoch=float(a.stoch_k),ret3=pct(p,float(z.close.iloc[-4])),ret6=pct(p,float(z.close.iloc[-7])),ret24=pct(p,float(z.close.iloc[-25])),macd=float(a.macd_hist),obv=float(a.obv)>=float(z.obv.iloc[-6]),vol=float(a.vol_ratio) if math.isfinite(float(a.vol_ratio)) else 1,wick=(float(a.high)-max(float(a.open),float(a.close)))/rg if rg else 0,price=p)
rows=[]
for si,s in enumerate(SYMS,1):
 try:
  print('FETCH',si,len(SYMS),s,flush=True);h1=ind(frame(fetch(s,'1h',1000,NOW+pd.Timedelta(hours=2))));h4=ind(frame(fetch(s,'4h',1000,NOW+pd.Timedelta(hours=2))));d1=ind(frame(fetch(s,'1d',1000,NOW+pd.Timedelta(hours=2))))
 except Exception as e:print('SKIP',s,e,flush=True);continue
 for ts in pd.date_range(START,NOW-pd.Timedelta(hours=3),freq='4h'):
  o=snap(h1,ts);f=snap(h4,ts);d=snap(d1,ts)
  if not o or not f or not d:continue
  fut=h1[(h1.open_time>=ts)&(h1.open_time<ts+pd.Timedelta(hours=2))]
  if len(fut)<2:continue
  mfe=(float(fut.high.max())/o['price']-1)*100;mae=(float(fut.low.min())/o['price']-1)*100
  rows.append(dict(ts=ts,symbol=s,mfe=mfe,mae=mae,hit15=mfe>=1.5,hit3=mfe>=3,clean=mfe>=1.5 and mae>-1.5,d_rsi=d['rsi'],d_s20=d['s20'],d_d20=d['dist20'],f_rsi=f['rsi'],f_s50=f['s50'],f_d50=f['dist50'],f_s20=f['s20'],f_ret6=f['ret6'],o_rsi=o['rsi'],o_stoch=o['stoch'],o_d50=o['dist50'],o_wick=o['wick'],o_ret3=o['ret3']))
df=pd.DataFrame(rows);df.to_csv('/tmp/v2_rows.csv',index=False)
# Train first 40d, validate last 20d. Grid chooses precision with minimum validation-like sample density, no lookahead into validation.
cut=START+pd.Timedelta(days=40);tr=df[df.ts<cut];va=df[df.ts>=cut]
res=[]
for fd,fs,od,st,dr,k in itertools.product([2,4,6,8],[.3,.6,.9,1.2],[0,1,2,3],[45,55,65,75],[55,60,65], [4,5]):
 base=(tr.d_rsi>=50)&(tr.d_s20>=-.8)&(tr.f_rsi>=45)&(tr.f_s50>=-.2)&(tr.o_rsi>=38)
 score=(tr.f_d50>=fd).astype(int)+(tr.f_s20>=fs).astype(int)+(tr.o_d50>=od).astype(int)+(tr.o_stoch<=st).astype(int)+(tr.d_rsi>=dr).astype(int)+(tr.o_wick>=.18).astype(int)
 m=base&(score>=k);n=int(m.sum())
 if n<80:continue
 res.append((float(tr.loc[m,'hit15'].mean()),float(tr.loc[m,'clean'].mean()),float(tr.loc[m,'hit3'].mean()),n,fd,fs,od,st,dr,k))
res=sorted(res,reverse=True)[:25]
out=[]
for hp,cp,h3,n,fd,fs,od,st,dr,k in res:
 for name,x in [('TRAIN',tr),('VALID',va)]:
  base=(x.d_rsi>=50)&(x.d_s20>=-.8)&(x.f_rsi>=45)&(x.f_s50>=-.2)&(x.o_rsi>=38)
  score=(x.f_d50>=fd).astype(int)+(x.f_s20>=fs).astype(int)+(x.o_d50>=od).astype(int)+(x.o_stoch<=st).astype(int)+(x.d_rsi>=dr).astype(int)+(x.o_wick>=.18).astype(int);m=base&(score>=k);nn=int(m.sum())
  out.append(dict(rank=res.index((hp,cp,h3,n,fd,fs,od,st,dr,k))+1,period=name,n=nn,hit15=x.loc[m,'hit15'].mean() if nn else None,clean=x.loc[m,'clean'].mean() if nn else None,hit3=x.loc[m,'hit3'].mean() if nn else None,avg_mfe=x.loc[m,'mfe'].mean() if nn else None,avg_mae=x.loc[m,'mae'].mean() if nn else None,fd=fd,fs=fs,od=od,st=st,dr=dr,k=k))
pd.DataFrame(out).to_csv('/tmp/v2_summary.csv',index=False);print(pd.DataFrame(out).to_string(index=False),flush=True)
