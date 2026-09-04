import json, math, time, urllib.parse, urllib.request
from datetime import datetime, timezone, timedelta
import numpy as np, pandas as pd

BASE='https://data-api.binance.vision/api/v3/klines'
SYMS='BTCUSDT ETHUSDT BNBUSDT SOLUSDT XRPUSDT DOGEUSDT ADAUSDT AVAXUSDT LINKUSDT TRXUSDT DOTUSDT LTCUSDT BCHUSDT HBARUSDT XLMUSDT ETCUSDT NEARUSDT APTUSDT ARBUSDT OPUSDT INJUSDT ATOMUSDT FILUSDT RENDERUSDT SEIUSDT TIAUSDT WLDUSDT UNIUSDT AAVEUSDT PENDLEUSDT RAYUSDT ZECUSDT DASHUSDT ZENUSDT ARKUSDT EDUUSDT ZROUSDT FORMUSDT ACHUSDT ENJUSDT'.split()
NOW=pd.Timestamp.now(tz='UTC').floor('1h')
START=NOW-pd.Timedelta(days=30)

def fetch(sym,tf,limit=1000,end=None):
    q={'symbol':sym,'interval':tf,'limit':limit}
    if end is not None:q['endTime']=int(pd.Timestamp(end).timestamp()*1000)
    u=BASE+'?'+urllib.parse.urlencode(q)
    for n in range(6):
        try:
            with urllib.request.urlopen(u,timeout=30) as r:return json.loads(r.read())
        except Exception:
            if n==5: raise
            time.sleep(1.5*(n+1))

def frame(rows):
    c=['open_time','open','high','low','close','volume','close_time','quote_volume','trades','taker_base','taker_quote','ignore']
    d=pd.DataFrame(rows,columns=c)
    for x in ['open','high','low','close','volume','quote_volume','taker_quote']:d[x]=pd.to_numeric(d[x],errors='coerce')
    d['open_time']=pd.to_datetime(d.open_time,unit='ms',utc=True);d['close_time']=pd.to_datetime(d.close_time,unit='ms',utc=True)
    return d.dropna().reset_index(drop=True)

def rsi(s,n=14):
    z=s.diff();g=z.clip(lower=0);l=-z.clip(upper=0);ag=g.ewm(alpha=1/n,adjust=False,min_periods=n).mean();al=l.ewm(alpha=1/n,adjust=False,min_periods=n).mean();return (100-100/(1+ag/al.replace(0,np.nan))).fillna(50)

def ind(d):
    x=d.copy();c=x.close
    for n in (20,50,200):x[f'ema{n}']=c.ewm(span=n,adjust=False).mean()
    x['rsi']=rsi(c);lo=x.rsi.rolling(14).min();hi=x.rsi.rolling(14).max();raw=100*(x.rsi-lo)/(hi-lo).replace(0,np.nan);x['stoch_k']=raw.rolling(3).mean().fillna(50);x['stoch_d']=x.stoch_k.rolling(3).mean().fillna(50)
    e12=c.ewm(span=12,adjust=False).mean();e26=c.ewm(span=26,adjust=False).mean();x['macd_hist']=(e12-e26)-(e12-e26).ewm(span=9,adjust=False).mean();x['obv']=(np.sign(c.diff()).fillna(0)*x.volume).cumsum();x['vol_ratio']=x.volume/x.volume.rolling(20).mean().replace(0,np.nan);x['taker_buy_ratio']=(x.taker_quote/x.quote_volume.replace(0,np.nan)).clip(0,1).fillna(.5)
    return x

def pct(a,b):return (a/b-1)*100 if b else 0

def snap(x,ts):
    z=x[x.close_time<ts]
    if len(z)<205:return None
    a=z.iloc[-1];b=z.iloc[-2];p=float(a.close);rg=float(a.high-a.low)
    return dict(price=p,ema20=float(a.ema20),ema50=float(a.ema50),ema200=float(a.ema200),ema20_slope=pct(float(a.ema20),float(z.ema20.iloc[-4])),ema50_slope=pct(float(a.ema50),float(z.ema50.iloc[-4])),dist_ema20=pct(p,float(a.ema20)),dist_ema50=pct(p,float(a.ema50)),rsi=float(a.rsi),stoch_k=float(a.stoch_k),stoch_d=float(a.stoch_d),stoch_prev=float(b.stoch_k),macd_hist=float(a.macd_hist),macd_prev=float(b.macd_hist),ret3=pct(p,float(z.close.iloc[-4])),ret6=pct(p,float(z.close.iloc[-7])),ret24=pct(p,float(z.close.iloc[-25])),obv_up=float(a.obv)>=float(z.obv.iloc[-6]),obv_fast=float(a.obv)>=float(z.obv.iloc[-3]),vol_ratio=float(a.vol_ratio) if math.isfinite(float(a.vol_ratio)) else 1.0,upper_wick=(float(a.high)-max(float(a.open),float(a.close)))/rg if rg else 0,lower_wick=(min(float(a.open),float(a.close))-float(a.low))/rg if rg else 0)

def classify(day,four,one):
    day_trend=day['dist_ema20']>=0 and day['ema20_slope']>=-.8 and day['rsi']>=50
    day_strong=day_trend and day['dist_ema50']>=0 and (day['ema20_slope']>0 or day['macd_hist']>0 or day['ret24']>4)
    four_trend=four['dist_ema50']>=0 and four['ema50_slope']>=-.2 and four['rsi']>=45
    four_strong=four_trend and (four['ema20_slope']>0 or four['ret6']>3 or four['macd_hist']>0) and (four['obv_up'] or four['ret6']>5 or four['vol_ratio']>=.8)
    one_reset=one['stoch_k']<=55
    one_structure=one['dist_ema50']>=0 and one['rsi']>=38
    v10=day_strong and four_strong and one_structure and one_reset and one['ret3']>=-7
    strict=day_trend and four_trend and four['dist_ema50']>=6 and one['dist_ema50']>=2 and one['upper_wick']>=.22 and one['stoch_k']<=75
    balanced=day_trend and four_trend and sum([four['dist_ema50']>=6,four['ema20_slope']>=1,one['dist_ema50']>=2,one['upper_wick']>=.22,one['stoch_k']<=75,day['rsi']>=62])>=5
    broad=day_trend and four_trend and sum([four['dist_ema50']>=5,four['ema20_slope']>=.8,one['dist_ema50']>=1.5,one['upper_wick']>=.18,one['stoch_k']<=78,day['rsi']>=60])>=4
    return v10,strict,balanced,broad

rows=[]
for si,s in enumerate(SYMS,1):
    try:
        print('FETCH',si,len(SYMS),s,flush=True)
        h1=ind(frame(fetch(s,'1h',1000,NOW+pd.Timedelta(hours=3))))
        h4=ind(frame(fetch(s,'4h',1000,NOW+pd.Timedelta(hours=3))))
        d1=ind(frame(fetch(s,'1d',1000,NOW+pd.Timedelta(hours=3))))
    except Exception as e:
        print('SKIP',s,e,flush=True);continue
    times=pd.date_range(START,NOW-pd.Timedelta(hours=3),freq='4h')
    for ts in times:
        one=snap(h1,ts);four=snap(h4,ts);day=snap(d1,ts)
        if not one or not four or not day:continue
        fut=h1[(h1.open_time>=ts)&(h1.open_time<ts+pd.Timedelta(hours=2))]
        if len(fut)<2:continue
        entry=one['price'];mfe=(float(fut.high.max())/entry-1)*100;mae=(float(fut.low.min())/entry-1)*100
        v10,strict,balanced,broad=classify(day,four,one)
        rows.append(dict(ts=ts,symbol=s,entry=entry,mfe2=mfe,mae2=mae,hit15=mfe>=1.5,clean15=(mfe>=1.5 and mae>-1.5),hit3=mfe>=3,v10=v10,strict=strict,balanced=balanced,broad=broad))
out=pd.DataFrame(rows);out.to_csv('/tmp/validation_rows.csv',index=False)
summary=[]
for period,dd in [('ALL',out),('FIRST_HALF',out[out.ts<START+pd.Timedelta(days=15)]),('SECOND_HALF',out[out.ts>=START+pd.Timedelta(days=15)])]:
    for name in ['v10','strict','balanced','broad']:
        m=dd[name]==True;n=int(m.sum())
        summary.append(dict(period=period,variant=name,n=n,hit15=float(dd.loc[m,'hit15'].mean()) if n else None,clean15=float(dd.loc[m,'clean15'].mean()) if n else None,hit3=float(dd.loc[m,'hit3'].mean()) if n else None,avg_mfe=float(dd.loc[m,'mfe2'].mean()) if n else None,avg_mae=float(dd.loc[m,'mae2'].mean()) if n else None))
s=pd.DataFrame(summary);s.to_csv('/tmp/validation_summary.csv',index=False)
print('\nSUMMARY\n',s.to_string(index=False),flush=True)
print('\nBASELINE',len(out),'hit15',out.hit15.mean(),'clean',out.clean15.mean(),'hit3',out.hit3.mean(),flush=True)
# trigger marker 2026-09-04
