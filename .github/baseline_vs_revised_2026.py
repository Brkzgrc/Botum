import os,json,time,requests,pandas as pd,numpy as np
from concurrent.futures import ThreadPoolExecutor,as_completed
import spot_opportunity_scanner as sc

START=pd.Timestamp(os.getenv("TEST_START","2026-01-01"),tz="UTC")
END=pd.Timestamp(os.getenv("TEST_END","2026-09-10"),tz="UTC")+pd.Timedelta(days=1)
N=int(os.getenv("TEST_SYMBOLS","120"))
BASES="BTC ETH BNB SOL XRP DOGE ADA TRX SUI LINK AVAX TON SHIB LTC HBAR DOT BCH XLM PEPE UNI AAVE NEAR APT ICP FIL ATOM ETC VET ALGO ARB OP INJ SEI RENDER FET TAO WLD TIA JUP ONDO ENA PENDLE PYTH JTO ZRO WIF BONK FLOKI TURBO CAKE ZEC DASH KAS KAVA GALA SAND MANA AXS IMX THETA MKR CRV LDO RUNE FTM KNC SNX COMP SUSHI YFI 1INCH ENS MASK CHZ ENJ FLOW EGLD MINA ROSE KSM ZIL IOTA XTZ EOS NEO QNT GRT STX RAY RNDR ASTR CFX APE BLUR DYDX GMX MAGIC LRC BAT ZRX OCEAN SKL COTI DENT CELR SXP CTSI BAND API3 LPT SSV RPL ARK ARPA ACH C98 DODO HIGH ID ACE BMT TUT GPS ONG COW EUL".split()
SYMS=["BTCUSDT"]+[x+"USDT" for x in BASES[:N] if x!="BTC"]
COLS=["open_time","open","high","low","close","volume","close_time","quote_volume","trades","taker_base","taker_quote","ignore"]
data={}; http=requests.Session()

def fetch(sym,tf):
    start=START-pd.Timedelta(days={"15m":18,"1h":20,"4h":60,"1d":320}[tf])
    cur=int(start.timestamp()*1000); end=int(END.timestamp()*1000); rows=[]
    while cur<end:
        r=http.get("https://data-api.binance.vision/api/v3/klines",params={"symbol":sym,"interval":tf,"startTime":cur,"endTime":end,"limit":1000},timeout=30)
        r.raise_for_status(); part=r.json()
        if not part: break
        rows+=part; nxt=int(part[-1][6])+1
        if nxt<=cur: break
        cur=nxt
    d=pd.DataFrame(rows,columns=COLS)
    for c in ("open","high","low","close","volume","quote_volume","taker_quote"): d[c]=pd.to_numeric(d[c],errors="coerce")
    d["open_time"]=pd.to_datetime(d.open_time,unit="ms",utc=True); d["close_time"]=pd.to_datetime(d.close_time,unit="ms",utc=True)
    return d.dropna(subset=["open","high","low","close","volume"]).drop_duplicates("open_time").reset_index(drop=True)

def load(s):
    try:return s,{tf:fetch(s,tf) for tf in ("15m","1h","4h","1d")}
    except Exception as e: print("[ATLA]",s,e,flush=True); return s,None

def run(revised):
    global cutoff,prices
    old_o,old_g=sc.ohlcv,sc._get
    def hist(s,tf,limit=240):
        d=data[s][tf]; x=d[d.close_time<=cutoff].tail(limit)
        if len(x)<80: raise ValueError("yetersiz kapanmis mum")
        return x.reset_index(drop=True)
    def get(path,params=None,attempts=4):
        if path=="/api/v3/ticker/price": return {"price":str(prices.get(params.get("symbol"),0))}
        return old_g(path,params,attempts)
    sc.ohlcv,sc._get=hist,get; watch={}; pos={}; out=[]; daily={}
    def veto(c):
        f=c.snapshot["15m"]; o=c.snapshot["1h"]; h=c.snapshot["4h"]
        try:
            lv=sc.levels(c); risk=abs(sc.pct(lv["price"],lv["stop"])); target=sc.pct(lv["tp1"],lv["price"]); rr=target/risk if risk else 0
        except: return False,""
        flags=[]
        if rr<.70 and h["rsi"]>=70 and h["dist_ema20"]>=10: flags.append("4H_EXTREME_LATE")
        if f["rsi"]>=80 and f["stoch_k"]>=90 and f["dist_ema20"]>=6 and o["stoch_k"]>=90: flags.append("FAST_BLOWOFF")
        if f["stoch_k"]>=95 and f["rsi"]<=65 and o["rsi"]<=58: flags.append("WEAK_1H_BOUNCE")
        return bool(flags),"|".join(flags)
    try:
        for ts in pd.date_range(START.ceil("15min"),END-pd.Timedelta(minutes=15),freq="15min",tz="UTC"):
            cutoff=ts; prices={s:float(data[s]["15m"][data[s]["15m"].close_time<=ts].tail(1).close.iloc[0]) for s in data}
            for s,p in list(pos.items()):
                b=data[s]["15m"]; b=b[(b.close_time>p["time"])&(b.close_time<=ts)].tail(1)
                if b.empty: continue
                bar=b.iloc[0]; p["peak"]=max(p["peak"],float(bar.high))
                reason=None; exitp=None
                if not p["tp1"] and float(bar.low)<=p["stop"]: reason,exitp="loss",p["stop"]
                elif not p["tp1"] and float(bar.high)>=p["target"]: p["tp1"]=True
                elif p["tp1"] and float(bar.close)<=max(p["entry"],p["peak"]*.975): reason,exitp="win_trail",max(p["entry"],p["peak"]*.975)
                elif ts>=p["expiry"]: reason,exitp="expired",float(bar.close)
                if reason:
                    out.append({"symbol":s,"entry_time":p["time"].isoformat(),"status":reason,"pnl":round((exitp/p["entry"]-1)*100,4),"peak":round((p["peak"]/p["entry"]-1)*100,4),"veto":p["veto"]}); pos.pop(s)
            regime=sc.btc_regime(); rows=[]
            for s in data:
                if s=="BTCUSDT": continue
                try:
                    pre=sc._prefilter(s,1e9)
                    if pre: rows.append(pre)
                except: pass
            rows.sort(key=lambda x:x[2],reverse=True)
            for s,q,r,_ in rows[:sc.PYTHON_TOP_N]:
                try:
                    c=sc.evaluate(s,q,r,regime,watch.get(s))
                    if c.decision["decision"]=="TETIK_BEKLE": watch[s]={"first_seen":ts.timestamp(),"first_price":c.snapshot["live_price"],"last_bar_15m":c.snapshot["15m"]["bar_id"]}
                    if c.decision["decision"]!="ALIM_ADAYI": continue
                    blocked,why=veto(c)
                    if revised and blocked: continue
                    lv=sc.levels(c); entry,stop,target=map(float,(lv["price"],lv["stop"],lv["tp1"]))
                    if stop>=entry or target<=entry or s in pos: continue
                    day=ts.strftime("%Y-%m-%d")
                    if daily.get(day,0)>=sc.MAX_SIGNALS_PER_DAY: continue
                    daily[day]=daily.get(day,0)+1; watch.pop(s,None)
                    pos[s]={"time":ts,"entry":entry,"stop":stop,"target":target,"peak":entry,"tp1":False,"expiry":ts+pd.Timedelta(hours=24),"veto":why}
                except Exception: pass
    finally: sc.ohlcv,sc._get=old_o,old_g
    return out

with ThreadPoolExecutor(max_workers=8) as ex:
    for s,d in ex.map(load,SYMS):
        if d is not None:data[s]=d
if "BTCUSDT" not in data: raise SystemExit("BTC verisi alınamadı")
base=run(False); rev=run(True)
def summary(name,rows):
    wins=[x for x in rows if x["status"]=="win_trail"]; losses=[x for x in rows if x["status"]=="loss"]; exp=[x for x in rows if x["status"]=="expired"]
    return {"system":name,"total_closed":len(rows),"win":len(wins),"loss":len(losses),"expired":len(exp),"net_pct":round(sum(x["pnl"] for x in rows),4),"win_pct":round(sum(x["pnl"] for x in wins),4),"loss_pct":round(sum(x["pnl"] for x in losses),4),"expired_pct":round(sum(x["pnl"] for x in exp),4),"avg_peak_pct":round(sum(x["peak"] for x in rows)/len(rows),4) if rows else 0}
os.makedirs("comparison_output",exist_ok=True)
pd.DataFrame([summary("baseline",base),summary("revised",rev)]).to_csv("comparison_output/comparison_summary.csv",index=False)
pd.DataFrame(base).to_csv("comparison_output/baseline_trades.csv",index=False); pd.DataFrame(rev).to_csv("comparison_output/revised_trades.csv",index=False)
print(json.dumps({"baseline":summary("baseline",base),"revised":summary("revised",rev)},ensure_ascii=False,indent=2))
