# -*- coding: utf-8 -*-
"""15M ana tarama + 1H ust-timeframe teyitli Binance Spot scanner.

Dogru MTF akisi:
  1) Tum spot evreni 15M'de taranir.
  2) 15M kendi basina setup + skor + stop/hedef geometrisi uretir.
  3) Yalniz 15M adayi olan coin icin 1H okunur.
  4) 1H coin SECMEZ; yalniz teyit, risk notu veya sert veto verir.
  5) BTC rejimi genel piyasa guvenlik katmanidir.

Spot only. Otomatik emir vermez. Acik mum karar hesaplarina dahil edilmez.
Render dis sozlesmeleri korunur: /, /health, PORT, TELEGRAM_*, PORTFOLIO_*.
"""
from __future__ import annotations

import json, logging, math, os, threading, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np
import pandas as pd
import requests
from flask import Flask, jsonify

BINANCE_API = "https://api.binance.com"
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TELEGRAM_THREAD_ID = int(os.getenv("SIGNAL_THREAD_ID", "2"))
PORTFOLIO_URL = os.getenv("PORTFOLIO_URL", "").rstrip("/")
PORTFOLIO_TOKEN = os.getenv("PORTFOLIO_TOKEN", "")
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"
SCAN_ON_START = os.getenv("SCAN_ON_START", "true").lower() == "true"
MAX_WORKERS = max(1, min(8, int(os.getenv("MAX_WORKERS", "4"))))
MIN_QUOTE_VOLUME = float(os.getenv("MIN_QUOTE_VOLUME", "0"))
MIN_15M_SCORE = float(os.getenv("MIN_15M_SCORE", "58"))
MIN_FINAL_SCORE = float(os.getenv("MIN_FINAL_SCORE", "64"))
ALERT_COOLDOWN_HOURS = float(os.getenv("ALERT_COOLDOWN_HOURS", "4"))
ACCOUNT_SIZE = float(os.getenv("ACCOUNT_SIZE", "10000"))
RISK_PER_TRADE_PCT = float(os.getenv("RISK_PER_TRADE_PCT", "1.25"))
MAX_POSITION_PCT = float(os.getenv("MAX_POSITION_PCT", "40"))
MIN_TARGET_PCT = float(os.getenv("MIN_TARGET_PCT", "1.5"))
MAX_STOP_PCT = float(os.getenv("MAX_STOP_PCT", "5.0"))
STATE_FILE = os.getenv("SCANNER_STATE_FILE", "/tmp/spot_15m_primary_state_v1.json")
TR_TZ = timezone(timedelta(hours=3))
HTTP = requests.Session(); HTTP.headers.update({"User-Agent":"Botum-15MPrimaryScanner/1.0"})

IGNORED_BASES = {"USDT","USDC","BUSD","TUSD","DAI","PAX","HUSD","USDP","GUSD","FDUSD","EUR","TRY","GBP","USD","BRL","RUB","AUD","XUSD","USD1","USDE","BFUSD","USDS","USDD","PYUSD","AEUR","EURI","USTC","FRAX","LUSD","SUSD","USDX","CUSD","OUSD","MUSD","RLUSD","BIDR","IDRT","VAI","PAXG","XAUT","WBTC","WETH","WBNB","BETH","BTCB","HBTC","U"}
LEVERAGED_SUFFIXES=("UP","DOWN","BULL","BEAR","2L","2S","3L","3S","5L","5S","10L","10S")
CRYPTO_BASES_ENDING_B={"BNB","DGB","TRB","CKB","SHIB","ARB","BB","YB"}

app=Flask(__name__); logging.getLogger("werkzeug").setLevel(logging.ERROR)
runtime={"status":"BOOT","strategy":"15M primary + 1H confirmation","dry_run":DRY_RUN,"last_scan":None,"symbols":0,"m15_candidates":0,"confirmed":0,"sent":0,"btc_regime":None,"last_error":None}

@dataclass
class Zone:
    low: float; high: float; strength: float; source: str; touches: int=1
    @property
    def center(self): return (self.low+self.high)/2

@dataclass
class PreCandidate:
    symbol: str; quote_volume_24h: float; setup: str; price: float; m15_score: float
    support: Zone; resistance: Zone; stop: float; target1: float; stop_pct: float; target_pct: float; rr: float
    reasons: list[str]=field(default_factory=list); risks: list[str]=field(default_factory=list); metrics: dict[str,Any]=field(default_factory=dict)

@dataclass
class Candidate:
    symbol: str; setup: str; price: float; entry_low: float; entry_high: float; stop: float; target1: float; target2: float
    target_pct: float; stop_pct: float; rr: float; position_size: float; risk_dollars: float; entry_score: float
    btc_regime: str; support: Zone; resistance: Zone; reasons: list[str]; risks: list[str]; metrics: dict[str,Any]

def tr_now(): return datetime.now(timezone.utc).astimezone(TR_TZ)
def safe_float(v:Any, default:float=0.0):
    try:
        x=float(v); return x if math.isfinite(x) else default
    except Exception: return default

def clamp(x,lo=0.0,hi=100.0): return max(lo,min(hi,float(x)))
def pct_change(new,old): return (new/old-1)*100 if old else 0.0
def fmt_price(v):
    if v>=1000:return f"{v:,.2f}"
    if v>=100:return f"{v:.2f}"
    if v>=1:return f"{v:.4f}"
    if v>=.01:return f"{v:.6f}"
    return f"{v:.10f}".rstrip("0")

def load_state():
    try:
        with open(STATE_FILE,encoding="utf-8") as f: d=json.load(f); return d if isinstance(d,dict) else {}
    except Exception:return {}
def save_state(d):
    try:
        tmp=STATE_FILE+".tmp"
        with open(tmp,"w",encoding="utf-8") as f: json.dump(d,f,ensure_ascii=False,indent=2)
        os.replace(tmp,STATE_FILE)
    except Exception as e: print(f"[STATE] {e}",flush=True)

def api_get(path,params=None,attempts=4):
    last=None
    for i in range(attempts):
        try:
            r=HTTP.get(BINANCE_API+path,params=params,timeout=15)
            if r.status_code in (418,429): time.sleep(2**i); continue
            r.raise_for_status(); return r.json()
        except Exception as e: last=e; time.sleep(.4*(2**i))
    raise RuntimeError(f"Binance API basarisiz {path}: {last}")

def fetch_ohlcv(symbol,interval,limit=260):
    rows=api_get("/api/v3/klines",{"symbol":symbol,"interval":interval,"limit":limit})
    if not isinstance(rows,list) or len(rows)<60: raise ValueError(f"Yetersiz mum {symbol} {interval}")
    df=pd.DataFrame(rows,columns=["open_time","open","high","low","close","volume","close_time","quote_volume","trades","taker_base","taker_quote","ignore"])
    for c in ("open","high","low","close","volume","quote_volume","taker_quote"): df[c]=pd.to_numeric(df[c],errors="coerce")
    df["open_time"]=pd.to_datetime(df["open_time"],unit="ms",utc=True); df["close_time"]=pd.to_datetime(df["close_time"],unit="ms",utc=True)
    if rows and int(rows[-1][6])>int(time.time()*1000): df=df.iloc[:-1].copy()
    return df.dropna(subset=["open","high","low","close","volume"]).reset_index(drop=True)

def rsi(s,period=14):
    d=s.diff(); g=d.clip(lower=0); l=-d.clip(upper=0)
    ag=g.ewm(alpha=1/period,adjust=False,min_periods=period).mean(); al=l.ewm(alpha=1/period,adjust=False,min_periods=period).mean()
    rs=ag/al.replace(0,np.nan); return (100-100/(1+rs)).fillna(50)

def add_indicators(df):
    o=df.copy(); o["ema20"]=o.close.ewm(span=20,adjust=False).mean(); o["ema50"]=o.close.ewm(span=50,adjust=False).mean(); o["ema200"]=o.close.ewm(span=200,adjust=False).mean(); o["rsi"]=rsi(o.close)
    lo=o.rsi.rolling(14).min(); hi=o.rsi.rolling(14).max(); st=100*(o.rsi-lo)/(hi-lo).replace(0,np.nan); o["stoch_k"]=st.rolling(3).mean().fillna(50); o["stoch_d"]=o.stoch_k.rolling(3).mean().fillna(50)
    e12=o.close.ewm(span=12,adjust=False).mean(); e26=o.close.ewm(span=26,adjust=False).mean(); o["macd"]=e12-e26; o["macd_signal"]=o.macd.ewm(span=9,adjust=False).mean(); o["macd_hist"]=o.macd-o.macd_signal
    pc=o.close.shift(1); tr=pd.concat([o.high-o.low,(o.high-pc).abs(),(o.low-pc).abs()],axis=1).max(axis=1); o["atr"]=tr.ewm(alpha=1/14,adjust=False,min_periods=14).mean()
    o["vol_ratio"]=o.volume/o.volume.rolling(20).median().replace(0,np.nan); o["taker_buy_ratio"]=o.taker_quote/o.quote_volume.replace(0,np.nan)
    return o

def pivot_indices(df,side,window=2):
    s=df.low if side=="low" else df.high; out=[]
    for i in range(window,len(df)-window):
        c=s.iloc[i-window:i+window+1]; ex=c.min() if side=="low" else c.max()
        if s.iloc[i]==ex: out.append(i)
    return out

def swing_structure(df):
    d=df.tail(100).reset_index(drop=True); li=pivot_indices(d,"low",2); hi=pivot_indices(d,"high",2)
    lv=[safe_float(d.iloc[i].low) for i in li[-3:]]; hv=[safe_float(d.iloc[i].high) for i in hi[-3:]]; t="MIXED"
    if len(lv)>=2 and len(hv)>=2:
        hl=lv[-1]>lv[-2]; hh=hv[-1]>hv[-2]; ll=lv[-1]<lv[-2]; lh=hv[-1]<hv[-2]
        if hl and hh:t="HH_HL"
        elif ll and lh:t="LH_LL"
        elif hl:t="HL_BUILDING"
        elif hh:t="HH_BUILDING"
    return {"trend":t,"last_swing_low":lv[-1] if lv else safe_float(d.low.tail(10).min()),"prev_swing_low":lv[-2] if len(lv)>=2 else None,"last_swing_high":hv[-1] if hv else safe_float(d.high.tail(10).max()),"prev_swing_high":hv[-2] if len(hv)>=2 else None}

def build_zone(df,side,lookback,window,source):
    d=df.tail(lookback).reset_index(drop=True); idx=pivot_indices(d,"low" if side=="support" else "high",window)
    if not idx:return None
    p=safe_float(d.close.iloc[-1]); atr=max(safe_float(d.atr.iloc[-1]),p*.002); levels=[safe_float(d.iloc[i]["low" if side=="support" else "high"]) for i in idx]
    viable=[x for x in levels if x<p] if side=="support" else [x for x in levels if x>p]; viable.sort(reverse=(side=="support"))
    if not viable:return None
    a=viable[0]; near=[x for x in viable[:12] if abs(x-a)<=atr*.8]; center=float(np.mean(near)) if near else a; half=min(atr*.28,p*.006); touches=max(1,len(near))
    return Zone(center-half,center+half,min(100,25+touches*12),source,touches)

def m15_state(df):
    d=add_indicators(df); last=d.iloc[-1]; prev=d.iloc[-2]; p=safe_float(last.close)
    return {"df":d,"price":p,"atr":safe_float(last.atr),"rsi":safe_float(last.rsi),"stoch_k":safe_float(last.stoch_k),"stoch_d":safe_float(last.stoch_d),"stoch_prev_k":safe_float(prev.stoch_k),"stoch_prev_d":safe_float(prev.stoch_d),"vol_ratio":safe_float(last.vol_ratio,1),"taker_buy_ratio":safe_float(last.taker_buy_ratio,.5),"ema20":safe_float(last.ema20),"ema50":safe_float(last.ema50),"macd_hist":safe_float(last.macd_hist),"macd_hist_prev":safe_float(prev.macd_hist),"structure":swing_structure(d),"last_open":safe_float(last.open),"last_high":safe_float(last.high),"last_low":safe_float(last.low),"prev_high":safe_float(prev.high),"prev_low":safe_float(prev.low),"ret_15m":pct_change(p,safe_float(d.close.iloc[-2])),"ret_1h":pct_change(p,safe_float(d.close.iloc[-5])),"bar_id":int(pd.Timestamp(last.open_time).timestamp())}

def h1_state(df):
    d=add_indicators(df); last=d.iloc[-1]; p=safe_float(last.close); e20=safe_float(last.ema20); e50=safe_float(last.ema50); st=swing_structure(d); trend="BULL" if p>e20>e50 else "BEAR" if p<e20<e50 else "MIXED"
    return {"df":d,"price":p,"atr":safe_float(last.atr),"rsi":safe_float(last.rsi),"vol_ratio":safe_float(last.vol_ratio,1),"taker_buy_ratio":safe_float(last.taker_buy_ratio,.5),"ema20":e20,"ema50":e50,"trend":trend,"structure":st,"ret_1h":pct_change(p,safe_float(d.close.iloc[-2])),"ret_4h":pct_change(p,safe_float(d.close.iloc[-5])),"macd_hist":safe_float(last.macd_hist),"macd_hist_prev":safe_float(d.macd_hist.iloc[-2])}

def btc_context():
    m=m15_state(fetch_ohlcv("BTCUSDT","15m",220)); h=h1_state(fetch_ohlcv("BTCUSDT","1h",220)); s=h["structure"]["trend"]
    red=(m["ret_15m"]<=-.8 or h["ret_1h"]<=-1.25 or (h["trend"]=="BEAR" and s=="LH_LL" and h["ret_4h"]<=-1.8))
    yellow=(m["ret_15m"]<-.35 or h["ret_1h"]<-.45 or (h["trend"]=="BEAR" and s in {"LH_LL","MIXED"}))
    return {"regime":"RED" if red else "YELLOW" if yellow else "GREEN","ret_15m":m["ret_15m"],"ret_1h":h["ret_1h"],"ret_4h":h["ret_4h"],"trend":h["trend"],"structure":s}

def get_spot_universe():
    ex=api_get("/api/v3/exchangeInfo"); ticks=api_get("/api/v3/ticker/24hr"); tm={x.get("symbol"):x for x in ticks if isinstance(x,dict)}; out=[]
    for it in ex.get("symbols",[]):
        sym=it.get("symbol",""); base=it.get("baseAsset","")
        if it.get("status")!="TRADING" or it.get("quoteAsset")!="USDT" or not it.get("isSpotTradingAllowed",True):continue
        lev=any(base.endswith(s) and len(base)>len(s)+2 for s in LEVERAGED_SUFFIXES); bst=base.endswith("B") and base not in CRYPTO_BASES_ENDING_B
        if base=="BTC" or base in IGNORED_BASES or lev or bst:continue
        q=safe_float(tm.get(sym,{}).get("quoteVolume"))
        if MIN_QUOTE_VOLUME>0 and q<MIN_QUOTE_VOLUME:continue
        out.append((sym,q))
    return sorted(out,key=lambda x:x[1],reverse=True)

def m15_setup(state):
    d=state["df"]; p=state["price"]; bullish=p>state["last_open"]
    setups=[]; reasons=[]
    breakout_level=safe_float(d.high.iloc[-13:-2].max())
    breakout=breakout_level>0 and p>breakout_level and bullish and state["vol_ratio"]>=1.25
    if breakout: setups.append("15M BREAKOUT"); reasons.append("15M yerel tepe hacimle kirildi")
    recent=d.iloc[-4:]; prior_level=safe_float(d.high.iloc[-16:-5].max())
    retest=prior_level>0 and safe_float(recent.high.max())>prior_level and safe_float(recent.low.min())<=prior_level*1.006 and p>prior_level and p>state["ema20"]
    if retest: setups.append("15M RETEST"); reasons.append("15M kirilim seviyesi korunuyor")
    touched=safe_float(d.low.tail(5).min())<=state["ema20"]*1.006
    pullback=p>state["ema20"]>state["ema50"] and touched and bullish and p>safe_float(d.close.iloc[-2])
    if pullback: setups.append("15M PULLBACK"); reasons.append("15M yukari trendde kontrollu pullback")
    sup=build_zone(d,"support",160,2,"15M")
    near_sup=sup and 0<=pct_change(p,sup.high)<=2.2
    momentum_turn=state["macd_hist"]>state["macd_hist_prev"] and (state["rsi"]>=42 or (state["stoch_k"]>state["stoch_d"] and state["stoch_prev_k"]<=state["stoch_prev_d"]))
    reversal=bool(near_sup and bullish and p>state["prev_high"] and momentum_turn)
    if reversal: setups.append("15M DESTEK-DONUS"); reasons.append("15M destekten yapisal tepki")
    old=(d.high-d.low).iloc[-30:-10]; base=(d.high-d.low).iloc[-10:-1]; body=(p-state["last_open"])/max(state["last_high"]-state["last_low"],1e-12)
    expansion=not old.empty and safe_float(base.median())<=safe_float(old.median())*.78 and body>=.5 and state["vol_ratio"]>=1.5 and p>state["prev_high"]
    if expansion: setups.append("15M GENISLEME"); reasons.append("15M sikisma sonrasi hacimli genisleme")
    return setups,reasons,sup,breakout_level

def scan_15m_symbol(symbol,quote_volume,btc):
    if btc["regime"]=="RED": return None
    s=m15_state(fetch_ohlcv(symbol,"15m",240)); d=s["df"]; p=s["price"]; setups,reasons,support,breakout_level=m15_setup(s); atr=max(s["atr"],p*.0015)
    if not setups or not support:return None
    resistance=build_zone(d,"resistance",180,2,"15M")
    if not resistance or resistance.low<=p:return None
    score=0.0; risks=[]; structure=s["structure"]["trend"]
    if structure=="HH_HL": score+=16; reasons.append("15M HH/HL yapi")
    elif structure in {"HL_BUILDING","HH_BUILDING"}: score+=10
    elif structure=="LH_LL": score-=7; risks.append("15M ana yapi halen LH/LL")
    score += min(24,len(setups)*8)
    if s["vol_ratio"]>=2: score+=14; reasons.append(f"15M hacim {s['vol_ratio']:.1f}x")
    elif s["vol_ratio"]>=1.35: score+=9
    elif s["vol_ratio"]<.8: score-=7; risks.append("15M hacim zayif")
    if s["taker_buy_ratio"]>=.56: score+=6
    elif s["taker_buy_ratio"]<.42: score-=5
    if 48<=s["rsi"]<=70: score+=8
    elif 40<=s["rsi"]<48: score+=3
    elif s["rsi"]>78: score-=9; risks.append("15M RSI fazla isinmis")
    if s["macd_hist"]>s["macd_hist_prev"]: score+=6
    if s["stoch_k"]>s["stoch_d"] and s["stoch_prev_k"]<=s["stoch_prev_d"]: score+=3
    ema_dist=(p-s["ema20"])/max(atr,1e-12)
    if ema_dist>2.4: score-=12; risks.append("15M EMA20'den fazla uzak")
    if quote_volume<5_000_000 and s["vol_ratio"]<1.8: score-=5; risks.append("24H likidite dusuk")
    rel1=s["ret_1h"]-safe_float(btc["ret_1h"])
    if rel1>=.8: score+=8; reasons.append("Son 1 saatte BTC'den guclu")
    elif rel1<-.8: score-=6
    refs=[safe_float(s["structure"].get("last_swing_low")),safe_float(d.low.tail(8).min()),support.high]
    if breakout_level and breakout_level<p: refs.append(breakout_level)
    refs=[x for x in refs if 0<x<p]
    if not refs:return None
    invalid=max(refs); stop=invalid-atr*.35; stop_pct=(p-stop)/p*100
    if stop_pct<=0 or stop_pct>MAX_STOP_PCT:return None
    target1=resistance.low; target_pct=pct_change(target1,p)
    if target_pct<MIN_TARGET_PCT:return None
    rr=target_pct/stop_pct
    if rr>=2: score+=8
    elif rr>=1.3: score+=4
    elif rr<.8: score-=8
    score=clamp(score)
    if score<MIN_15M_SCORE:return None
    return PreCandidate(symbol,quote_volume," + ".join(setups[:3]),p,round(score,1),support,resistance,stop,target1,stop_pct,target_pct,rr,reasons[:7],risks[:5],{"bar_id":s["bar_id"],"m15_structure":structure,"volume_ratio_15m":round(s["vol_ratio"],2),"taker_buy_ratio_15m":round(s["taker_buy_ratio"],3),"rsi_15m":round(s["rsi"],1),"ema20_distance_atr_15m":round(ema_dist,2),"relative_strength_1h_from15m":round(rel1,3)})

def confirm_1h(pre,btc):
    h=h1_state(fetch_ohlcv(pre.symbol,"1h",240)); p=pre.price; structure=h["structure"]["trend"]; score=pre.m15_score; reasons=list(pre.reasons); risks=list(pre.risks)
    rel1=h["ret_1h"]-safe_float(btc["ret_1h"]); rel4=h["ret_4h"]-safe_float(btc["ret_4h"])
    hard_veto=(h["trend"]=="BEAR" and structure=="LH_LL" and h["ret_4h"]<-2.0 and rel4<-1.0)
    h1_res=build_zone(h["df"],"resistance",180,2,"1H")
    if h1_res and h1_res.low>p and pct_change(h1_res.low,p)<MIN_TARGET_PCT: hard_veto=True; risks.append("1H direnc hemen ustte")
    if hard_veto:return None
    if structure=="HH_HL": score+=8; reasons.append("1H yapi 15M hareketi destekliyor")
    elif structure in {"HL_BUILDING","HH_BUILDING"}: score+=4
    elif structure=="LH_LL": score-=5; risks.append("1H yapi ters; teyit zayif")
    if h["trend"]=="BULL": score+=6; reasons.append("1H EMA baglami pozitif")
    elif h["trend"]=="BEAR": score-=4
    if rel4>=1.0: score+=5; reasons.append("1H/4H BTC goreceli guc olumlu")
    elif rel4<-1.0: score-=5
    if btc["regime"]=="YELLOW": score-=3; risks.append("BTC YELLOW")
    score=clamp(score)
    if score<MIN_FINAL_SCORE:return None
    risk=ACCOUNT_SIZE*(RISK_PER_TRADE_PCT/100); pos=min(risk/(pre.stop_pct/100),ACCOUNT_SIZE*(MAX_POSITION_PCT/100)); pad=min(max(h["atr"]*.03,p*.0005),p*.003)
    target2=max(pre.resistance.high,p+(p-pre.stop)*1.5)
    metrics={**pre.metrics,"m15_score":pre.m15_score,"final_score":round(score,1),"h1_trend":h["trend"],"h1_structure":structure,"rsi_1h":round(h["rsi"],1),"relative_strength_1h":round(rel1,3),"relative_strength_4h":round(rel4,3),"btc_ret_15m":round(safe_float(btc["ret_15m"]),2),"btc_ret_1h":round(safe_float(btc["ret_1h"]),2),"btc_ret_4h":round(safe_float(btc["ret_4h"]),2)}
    return Candidate(pre.symbol,pre.setup,p,max(pre.support.high,p-pad),p+pad*.25,pre.stop,pre.target1,target2,pre.target_pct,pre.stop_pct,pre.rr,pos,risk,score,btc["regime"],pre.support,pre.resistance,reasons[:8],risks[:6] or ["Belirgin ek risk yok; manuel grafik kontrolu gerekli"],metrics)

def candidate_message(c):
    sym=c.symbol.removesuffix("USDT"); rs="\n".join(f"✅ {x}" for x in c.reasons); rk="\n".join(f"⚠️ {x}" for x in c.risks)
    return f"🚦 <b>ENTRY READY — #{sym}</b>\n<b>{c.setup}</b>\n━━━━━━━━━━━━━━━━━━━━\n📊 Anlik: <code>{fmt_price(c.price)}</code>\n🟦 Giris: <code>{fmt_price(c.entry_low)}–{fmt_price(c.entry_high)}</code>\n🟩 15M destek: <code>{fmt_price(c.support.low)}–{fmt_price(c.support.high)}</code>\n🛑 Stop: <code>{fmt_price(c.stop)}</code> (-%{c.stop_pct:.2f})\n🎯 TP1: <code>{fmt_price(c.target1)}</code> (+%{c.target_pct:.2f})\n🎯 TP2 ref: <code>{fmt_price(c.target2)}</code>\n⚖️ R/R: <b>{c.rr:.2f}</b>\n━━━━━━━━━━━━━━━━━━━━\n💰 Risk: <b>${c.risk_dollars:,.0f}</b> · Max pozisyon <b>${c.position_size:,.0f}</b>\n🌐 BTC: <b>{c.btc_regime}</b>\n⚡ 15M ana skor: <b>{c.metrics['m15_score']:.1f}</b>\n🔎 1H teyit sonrasi: <b>{c.entry_score:.1f}</b>\n\n<b>Neden?</b>\n{rs}\n\n<b>Riskler</b>\n{rk}\n━━━━━━━━━━━━━━━━━━━━\n<i>15M ana timeframe · 1H sadece teyit · Spot only · Manuel kontrol.</i>"

def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: print("[TELEGRAM] Token/chat id yok",flush=True); return False
    payload={"chat_id":TELEGRAM_CHAT_ID,"text":text,"parse_mode":"HTML","disable_web_page_preview":True}
    if TELEGRAM_THREAD_ID:payload["message_thread_id"]=TELEGRAM_THREAD_ID
    try:r=HTTP.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",json=payload,timeout=15); return r.ok
    except Exception as e:print(f"[TELEGRAM] {e}",flush=True);return False

def send_portfolio(c):
    if not PORTFOLIO_URL:return ""
    payload={"symbol":c.symbol.replace("USDT","/USDT"),"entry":round(c.price,10),"limit_price":round(c.price,10),"signal_price":round(c.price,10),"stop":round(c.stop,10),"tp1":round(c.target1,10),"tp2":round(c.target2,10),"tp3":None,"sig_type":"spot_opportunity","sub_type":c.setup.lower().replace(" ","_"),"source":"spot-scanner","phase":"manual_review","entry_zone":[round(c.entry_low,10),round(c.entry_high,10)],"support_zone":[round(c.support.low,10),round(c.support.high,10)],"resistance_zone":[round(c.resistance.low,10),round(c.resistance.high,10)],"target_pct":round(c.target_pct,2),"stop_pct":round(c.stop_pct,2),"rr":round(c.rr,2),"position_size":round(c.position_size,2),"risk_dollars":round(c.risk_dollars,2),"setup":c.setup,"positives":c.reasons,"risks":c.risks,**c.metrics}
    headers={"Content-Type":"application/json"}
    if PORTFOLIO_TOKEN:headers["Authorization"]=f"Bearer {PORTFOLIO_TOKEN}"
    try:
        r=HTTP.post(f"{PORTFOLIO_URL}/api/signal",json=payload,headers=headers,timeout=12)
        if r.status_code in (200,201):return str((r.json() or {}).get("id",""))
        if r.status_code==409:return ""
        print(f"[PORTFOLIO] HTTP {r.status_code}: {r.text[:180]}",flush=True)
    except Exception as e:print(f"[PORTFOLIO] {e}",flush=True)
    return ""

def request_analyzer(c,pid):
    if not PORTFOLIO_URL or not pid:return
    sig={"symbol":c.symbol.replace("USDT","/USDT"),"type":"spot_opportunity","source":"spot-scanner","entry":round(c.price,10),"stop":round(c.stop,10),"tp1":round(c.target1,10),"tp2":round(c.target2,10),"setup":c.setup,"target_pct":round(c.target_pct,2),"stop_pct":round(c.stop_pct,2),"rr":round(c.rr,2),"positives":c.reasons,"risks":c.risks,**c.metrics}; headers={"Content-Type":"application/json"}
    if PORTFOLIO_TOKEN:headers["Authorization"]=f"Bearer {PORTFOLIO_TOKEN}"
    try:HTTP.post(f"{PORTFOLIO_URL}/api/analyze",json={"signal":sig,"recent_count":0,"sig_num":0,"portfolio_id":pid},headers=headers,timeout=12)
    except Exception:pass

def scan_cycle():
    runtime.update({"status":"SCANNING_15M","last_error":None}); started=time.time(); btc=btc_context(); runtime["btc_regime"]=btc["regime"]; universe=get_spot_universe(); runtime["symbols"]=len(universe)
    if btc["regime"]=="RED":
        runtime.update({"status":"RUNNING","last_scan":tr_now().isoformat(),"m15_candidates":0,"confirmed":0}); print(f"[15M] BTC RED — yeni spot aday taramasi veto | Evren={len(universe)}",flush=True); return []
    pre=[]
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        jobs={pool.submit(scan_15m_symbol,s,q,btc):s for s,q in universe}
        for f in as_completed(jobs):
            try:
                x=f.result()
                if x:pre.append(x)
            except Exception as e: print(f"[15M] {jobs[f]}: {str(e)[:100]}",flush=True)
    pre.sort(key=lambda x:(x.m15_score,x.rr,x.quote_volume_24h),reverse=True); runtime["m15_candidates"]=len(pre)
    confirmed=[]
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS,6)) as pool:
        jobs={pool.submit(confirm_1h,x,btc):x.symbol for x in pre}
        for f in as_completed(jobs):
            try:
                c=f.result()
                if c:confirmed.append(c)
            except Exception as e:print(f"[1H TEYIT] {jobs[f]}: {str(e)[:100]}",flush=True)
    confirmed.sort(key=lambda c:(c.entry_score,c.metrics.get("m15_score",0),c.rr),reverse=True)
    state=load_state(); alerts=state.setdefault("alerts",{}); now=time.time(); emitted=[]; sent=0
    for c in confirmed:
        prev=alerts.get(c.symbol,{}) if isinstance(alerts.get(c.symbol),dict) else {}; same=int(prev.get("bar_id",0) or 0)==int(c.metrics["bar_id"]); cool=safe_float(prev.get("sent_at")) and now-safe_float(prev.get("sent_at"))<ALERT_COOLDOWN_HOURS*3600
        if same or cool:continue
        text=candidate_message(c); print("\n"+text.replace("<b>","").replace("</b>","").replace("<code>","").replace("</code>","").replace("<i>","").replace("</i>",""),flush=True)
        pid=""; tg=False
        if DRY_RUN:print(f"[DRY-RUN] {c.symbol}",flush=True)
        else:
            pid=send_portfolio(c)
            if pid: threading.Thread(target=request_analyzer,args=(c,pid),daemon=True).start()
            tg=send_telegram(text)
        alerts[c.symbol]={"bar_id":int(c.metrics["bar_id"]),"sent_at":now if (DRY_RUN or pid or tg) else 0,"entry":c.price,"stop":c.stop,"tp1":c.target1,"score":c.entry_score}; emitted.append(c); sent+=1 if (pid or tg) else 0
    save_state(state); runtime.update({"status":"RUNNING","last_scan":tr_now().isoformat(),"confirmed":len(confirmed),"sent":sent})
    print(f"[SCAN] Evren={len(universe)} | 15M aday={len(pre)} | 1H teyit={len(confirmed)} | yeni={len(emitted)} | BTC={btc['regime']} | {time.time()-started:.1f}s",flush=True)
    if pre: print("[15M] Top: "+" | ".join(f"{x.symbol}:{x.m15_score:.0f}" for x in pre[:10]),flush=True)
    return emitted

def scheduler_loop():
    if SCAN_ON_START:
        try:scan_cycle()
        except Exception as e:runtime.update({"status":"ERROR","last_error":str(e)});print(f"[START] {e}",flush=True)
    while True:
        now=tr_now(); future=[]
        for m in (1,16,31,46):
            t=now.replace(minute=m,second=15,microsecond=0)
            if t>now:future.append(t)
        nxt=min(future) if future else (now+timedelta(hours=1)).replace(minute=1,second=15,microsecond=0); time.sleep(max(20,(nxt-now).total_seconds()))
        try:scan_cycle()
        except Exception as e:runtime.update({"status":"ERROR","last_error":str(e)});print(f"[LOOP] {e}",flush=True);time.sleep(30)

@app.route("/")
@app.route("/health")
def health():return jsonify({"service":"spot-opportunity-scanner",**runtime}),200

def run_flask():app.run(host="0.0.0.0",port=int(os.getenv("PORT","10000")),use_reloader=False)
def main():
    print("="*72,flush=True);print("SPOT SCANNER — 15M ANA TARAMA + 1H TEYIT",flush=True);print("15M tum evreni tarar | 1H coin secmez, sadece teyit/veto | otomatik emir YOK",flush=True);print(f"15M>={MIN_15M_SCORE:g} | Final>={MIN_FINAL_SCORE:g} | Risk=%{RISK_PER_TRADE_PCT:g} | Max pozisyon=%{MAX_POSITION_PCT:g}",flush=True);print(f"DRY_RUN={DRY_RUN} — "+("Portfolio/Telegram kapali" if DRY_RUN else "Portfolio/Telegram AKTIF"),flush=True);print("="*72,flush=True);runtime["status"]="STARTING";threading.Thread(target=run_flask,daemon=True).start();scheduler_loop()
if __name__=="__main__":main()
