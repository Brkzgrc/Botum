# -*- coding: utf-8 -*-
"""SPOT_SCANNER v10 — stateful Binance Spot opportunity scanner.

Evidence-led model: strong 1D/4H trend is context, not an automatic overextension
penalty. Entry timing comes from a controlled 1H reset and early 15M/1H retrigger.
No first-scan entry. External outputs remain gated by FINAL_OUTPUT_ENABLED.
"""
from __future__ import annotations

import json, logging, math, os, threading, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np
import pandas as pd
import requests
from flask import Flask, jsonify

BINANCE = "https://api.binance.com"
TR_TZ = timezone(timedelta(hours=3))
HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "Botum-SPOT-SCANNER/10.0"})
MAX_WORKERS = max(2, min(10, int(os.getenv("MAX_WORKERS", "6"))))
PYTHON_TOP_N = max(64, min(120, int(os.getenv("VISUAL_TOP_N", "96"))))
PREFILTER_CORE_N = max(48, min(PYTHON_TOP_N, int(os.getenv("PREFILTER_CORE_N", "72"))))
MIN_QUOTE_VOLUME = float(os.getenv("MIN_QUOTE_VOLUME", "0"))
SCAN_INTERVAL_SECONDS = max(300, int(os.getenv("SCAN_INTERVAL_SECONDS", "900")))
WATCH_TTL_HOURS = float(os.getenv("WATCH_TTL_HOURS", "18"))
MAX_SIGNALS_PER_DAY = max(1, min(6, int(os.getenv("MAX_SIGNALS_PER_DAY", "3"))))
FINAL_MIN_QUALITY = float(os.getenv("FINAL_MIN_QUALITY", "72"))
STATE_FILE = os.getenv("SCANNER_STATE_FILE", "/tmp/spot_scanner_state_v10.json")
SCAN_ON_START = os.getenv("SCAN_ON_START", "true").strip().lower() == "true"
FINAL_OUTPUT_ENABLED = os.getenv("FINAL_OUTPUT_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
PORTFOLIO_URL = os.getenv("PORTFOLIO_URL", "").rstrip("/")
PORTFOLIO_TOKEN = os.getenv("PORTFOLIO_TOKEN", "")
TELEGRAM_ENABLED = os.getenv("TELEGRAM_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TELEGRAM_THREAD_ID = int(os.getenv("SIGNAL_THREAD_ID", "2"))
IGNORED_BASES = {"USDT","USDC","BUSD","TUSD","DAI","PAX","HUSD","USDP","GUSD","FDUSD","EUR","TRY","GBP","USD","BRL","RUB","AUD","XUSD","USD1","USDE","BFUSD","USDS","USDD","PYUSD","AEUR","EURI","USTC","FRAX","LUSD","SUSD","USDX","CUSD","OUSD","MUSD","RLUSD","BIDR","IDRT","VAI","PAXG","XAUT","WBTC","WETH","WBNB","BETH","BTCB","HBTC","U"}
LEVERAGED_SUFFIXES = ("UP","DOWN","BULL","BEAR","2L","2S","3L","3S","5L","5S","10L","10S")
CRYPTO_BASES_ENDING_B = {"BNB","DGB","TRB","CKB","SHIB","ARB","BB","YB"}
app = Flask(__name__)
logging.getLogger("werkzeug").setLevel(logging.ERROR)

def now_tr(): return datetime.now(timezone.utc).astimezone(TR_TZ)
def sf(v, default=0.0):
    try:
        x = float(v); return x if math.isfinite(x) else default
    except Exception: return default
def pct(new, old): return (new / old - 1) * 100 if old else 0.0
def clamp(v, lo=0.0, hi=100.0): return max(lo, min(hi, float(v)))

def _get(path, params=None, attempts=4):
    last = None
    for i in range(attempts):
        try:
            r = HTTP.get(BINANCE + path, params=params, timeout=15)
            if r.status_code in (418, 429): time.sleep(1.5 * (2 ** i)); continue
            r.raise_for_status(); return r.json()
        except Exception as exc:
            last = exc; time.sleep(.35 * (2 ** i))
    raise RuntimeError(f"Binance API failed {path}: {last}")

def ohlcv(symbol, interval, limit=240):
    rows = _get("/api/v3/klines", {"symbol": symbol, "interval": interval, "limit": limit})
    if not isinstance(rows, list) or len(rows) < 80: raise ValueError(f"insufficient candles {symbol} {interval}")
    cols = ["open_time","open","high","low","close","volume","close_time","quote_volume","trades","taker_base","taker_quote","ignore"]
    d = pd.DataFrame(rows, columns=cols)
    for c in ("open","high","low","close","volume","quote_volume","taker_quote"): d[c] = pd.to_numeric(d[c], errors="coerce")
    d["open_time"] = pd.to_datetime(d.open_time, unit="ms", utc=True); d["close_time"] = pd.to_datetime(d.close_time, unit="ms", utc=True)
    if rows and int(rows[-1][6]) >= int(time.time() * 1000): d = d.iloc[:-1].copy()
    return d.dropna(subset=["open","high","low","close","volume"]).reset_index(drop=True)

def _rsi(s, n=14):
    delta = s.diff(); gain = delta.clip(lower=0); loss = -delta.clip(upper=0)
    ag = gain.ewm(alpha=1/n, adjust=False, min_periods=n).mean(); al = loss.ewm(alpha=1/n, adjust=False, min_periods=n).mean(); rs = ag / al.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50)

def indicators(d):
    x = d.copy(); x["ema20"] = x.close.ewm(span=20, adjust=False).mean(); x["ema50"] = x.close.ewm(span=50, adjust=False).mean(); x["ema200"] = x.close.ewm(span=200, adjust=False).mean(); x["rsi"] = _rsi(x.close)
    lo = x.rsi.rolling(14).min(); hi = x.rsi.rolling(14).max(); raw = 100 * (x.rsi - lo) / (hi - lo).replace(0, np.nan); x["stoch_k"] = raw.rolling(3).mean().fillna(50); x["stoch_d"] = x.stoch_k.rolling(3).mean().fillna(50)
    e12 = x.close.ewm(span=12, adjust=False).mean(); e26 = x.close.ewm(span=26, adjust=False).mean(); x["macd"] = e12 - e26; x["macd_signal"] = x.macd.ewm(span=9, adjust=False).mean(); x["macd_hist"] = x.macd - x.macd_signal
    pc = x.close.shift(1); tr = pd.concat([x.high-x.low, (x.high-pc).abs(), (x.low-pc).abs()], axis=1).max(axis=1); x["atr"] = tr.ewm(alpha=1/14, adjust=False, min_periods=14).mean(); x["vol_ratio"] = x.volume / x.volume.rolling(20).mean().replace(0, np.nan); x["obv"] = (np.sign(x.close.diff()).fillna(0) * x.volume).cumsum(); x["taker_buy_ratio"] = (x.taker_quote / x.quote_volume.replace(0, np.nan)).clip(0,1).fillna(.5)
    return x

def _swings(d, wing=2, lookback=80):
    x = d.tail(lookback).reset_index(drop=True); highs=[]; lows=[]
    for i in range(wing, len(x)-wing):
        w=x.iloc[i-wing:i+wing+1]
        if x.high.iloc[i] >= w.high.max(): highs.append(float(x.high.iloc[i]))
        if x.low.iloc[i] <= w.low.min(): lows.append(float(x.low.iloc[i]))
    return highs[-8:], lows[-8:]

def snap(d, label):
    x=indicators(d); a=x.iloc[-1]; b=x.iloc[-2]; p=sf(a.close); highs,lows=_swings(x); atr=sf(a.atr); c6=x.close.tail(6).to_numpy(); l6=x.low.tail(6).to_numpy()
    return {"tf":label,"price":p,"bar_id":int(pd.Timestamp(a.open_time).timestamp()),"ret3":pct(p,sf(x.close.iloc[-4])),"ret6":pct(p,sf(x.close.iloc[-7])),"ret24":pct(p,sf(x.close.iloc[-25])),"ema20":sf(a.ema20),"ema50":sf(a.ema50),"ema200":sf(a.ema200),"ema20_slope":pct(sf(a.ema20),sf(x.ema20.iloc[-4])),"ema50_slope":pct(sf(a.ema50),sf(x.ema50.iloc[-4])),"dist_ema20":pct(p,sf(a.ema20)),"dist_ema50":pct(p,sf(a.ema50)),"rsi":sf(a.rsi),"stoch_k":sf(a.stoch_k),"stoch_d":sf(a.stoch_d),"stoch_k_prev":sf(b.stoch_k),"stoch_min3":sf(x.stoch_k.tail(3).min()),"macd_hist":sf(a.macd_hist),"macd_hist_prev":sf(b.macd_hist),"vol_ratio":sf(a.vol_ratio,1),"obv_up":sf(a.obv)>=sf(x.obv.iloc[-6]),"obv_fast_up":sf(a.obv)>=sf(x.obv.iloc[-3]),"taker_buy_ratio":sf(x.taker_buy_ratio.tail(3).mean(),.5),"higher_closes6":int(sum(c6[i]>c6[i-1] for i in range(1,len(c6)))),"higher_lows6":int(sum(l6[i]>=l6[i-1] for i in range(1,len(l6)))),"near_high20_pct":max(0.0,-pct(p,sf(x.high.tail(20).max()))),"prev_high6":sf(x.high.iloc[-7:-1].max()),"atr_pct":100*atr/p if p else 0,"supports":sorted([v for v in lows if v<p],reverse=True)[:4],"resistances":sorted([v for v in highs if v>p])[:4]}

@dataclass
class Candidate:
    symbol:str; base:str; qv:float; rank:float; snapshot:dict[str,Any]; decision:dict[str,Any]

def universe():
    ex=_get("/api/v3/exchangeInfo"); ticks=_get("/api/v3/ticker/24hr"); tm={x.get("symbol"):x for x in ticks if isinstance(x,dict)}; out=[]
    for it in ex.get("symbols",[]):
        sym=it.get("symbol",""); base=it.get("baseAsset","")
        if it.get("quoteAsset")!="USDT" or it.get("status")!="TRADING" or it.get("isSpotTradingAllowed") is False: continue
        if base in IGNORED_BASES or (base.endswith(LEVERAGED_SUFFIXES) and base not in CRYPTO_BASES_ENDING_B): continue
        qv=sf((tm.get(sym) or {}).get("quoteVolume"))
        if qv>=MIN_QUOTE_VOLUME: out.append((sym,qv))
    return out

def _prefilter(symbol,qv):
    try:
        x=indicators(ohlcv(symbol,"1h",220)); a=x.iloc[-1]; b=x.iloc[-2]; p=sf(a.close); r=sf(a.rsi); sk=sf(a.stoch_k); sd=sf(a.stoch_d); sk0=sf(b.stoch_k); mh=sf(a.macd_hist); mh0=sf(b.macd_hist); e20=sf(a.ema20); e50=sf(a.ema50); vr=sf(a.vol_ratio,1); ret3=pct(p,sf(x.close.iloc[-4])); ret6=pct(p,sf(x.close.iloc[-7])); obv=sf(a.obv)>=sf(x.obv.iloc[-6]); high20=sf(x.high.tail(20).max()); near=max(0,-pct(p,high20)); taker=sf(x.taker_buy_ratio.tail(3).mean(),.5); c=x.close.tail(6).to_numpy(); l=x.low.tail(6).to_numpy(); hc=sum(c[i]>c[i-1] for i in range(1,len(c))); hl=sum(l[i]>=l[i-1] for i in range(1,len(l)))
        reset = sk<=50 or min(sf(x.stoch_k.tail(3).min()),sk)<=30
        turn = sk>sd and sk>sk0
        score=(18 if p>=e50 else 0)+(12 if p>=e20 else 5)+(12 if 45<=r<=88 else 5 if 38<=r<=92 else 0)+(15 if reset else 10 if turn else 0)+(10 if mh>mh0 else 5 if mh>0 else 0)+(10 if obv else 0)+(7 if vr>=.8 else 3)+(7 if near<=7 else 3 if near<=12 else 0)+(5 if taker>=.50 else 0)+(4 if ret6>3 else 0)
        retrigger_seed=p>=e50 and 38<=r<=90 and (reset or turn) and -7<=ret3<=10
        pressure_seed=p>=e20 and 45<=r<=88 and hc>=3 and hl>=3 and near<=6 and -1<=ret3<=9 and (taker>=.50 or obv)
        seed="RETRIGGER" if retrigger_seed else "PRESSURE" if pressure_seed else ""
        # Strong prior movement is no longer an automatic penalty; only extreme 1H blow-off is.
        if ret3>14 and sk>85 and pct(p,e20)>15: score-=18
        return symbol,qv,round(score,2),seed
    except Exception: return None

def prefilter_candidates():
    uni=universe(); rows=[]
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        fs=[ex.submit(_prefilter,s,q) for s,q in uni]
        for f in as_completed(fs):
            r=f.result()
            if r: rows.append(r)
    rows.sort(key=lambda z:z[2],reverse=True); selected=list(rows[:PREFILTER_CORE_N]); seen={r[0] for r in selected}
    for r in rows[PREFILTER_CORE_N:]:
        if len(selected)>=PYTHON_TOP_N: break
        if r[3] and r[0] not in seen: selected.append(r); seen.add(r[0])
    if len(selected)<PYTHON_TOP_N:
        for r in rows[PREFILTER_CORE_N:]:
            if len(selected)>=PYTHON_TOP_N: break
            if r[0] not in seen: selected.append(r); seen.add(r[0])
    return [(s,q,score) for s,q,score,_ in selected],len(uni)

def _load_state():
    try:
        with open(STATE_FILE,encoding="utf-8") as f: d=json.load(f); return d if isinstance(d,dict) else {}
    except Exception: return {}
def _save_state(d):
    try:
        os.makedirs(os.path.dirname(STATE_FILE) or ".",exist_ok=True); tmp=STATE_FILE+".tmp"
        with open(tmp,"w",encoding="utf-8") as f: json.dump(d,f,ensure_ascii=False,indent=2)
        os.replace(tmp,STATE_FILE)
    except Exception as exc: print(f"[STATE] {exc}",flush=True)
def _clean_watch(state):
    now=time.time(); watch=state.setdefault("watch",{}); dead=[s for s,r in watch.items() if now-sf((r or {}).get("first_seen",0))>WATCH_TTL_HOURS*3600]
    for s in dead: watch.pop(s,None)
def _daily_room(state):
    day=now_tr().strftime("%Y-%m-%d")
    if state.get("signal_day")!=day: state["signal_day"]=day; state["signal_count"]=0
    return max(0,MAX_SIGNALS_PER_DAY-int(state.get("signal_count",0)))

def btc_regime():
    try:
        h=snap(ohlcv("BTCUSDT","1h"),"1H"); f=snap(ohlcv("BTCUSDT","4h"),"4H")
        if h["ret3"]<=-3 or f["ret3"]<=-6: return "RED"
        if h["ret3"]<-.9 or f["macd_hist"]<f["macd_hist_prev"]: return "YELLOW"
        return "GREEN"
    except Exception: return "YELLOW"

def evaluate(symbol,qv,pre_rank,regime,prior):
    day=snap(ohlcv(symbol,"1d"),"1D"); four=snap(ohlcv(symbol,"4h"),"4H"); one=snap(ohlcv(symbol,"1h"),"1H"); fast=snap(ohlcv(symbol,"15m"),"15M"); live=sf(_get("/api/v3/ticker/price",{"symbol":symbol}).get("price")) or one["price"]
    prior_phase=(prior or {}).get("phase"); prior_bar=int(sf((prior or {}).get("last_bar_15m"),0))

    # 54-case audit: upper-timeframe strength is positive context, not a reason to reject.
    day_trend=day["price"]>=day["ema20"] and day["ema20_slope"]>=-.8 and day["rsi"]>=50
    day_strong=day_trend and (day["price"]>=day["ema50"]) and (day["ema20_slope"]>0 or day["macd_hist"]>0 or day["ret24"]>4)
    four_trend=four["price"]>=four["ema50"] and four["ema50_slope"]>=-.2 and four["rsi"]>=45
    four_strong=four_trend and (four["ema20_slope"]>0 or four["ret6"]>3 or four["macd_hist"]>0) and (four["obv_up"] or four["ret6"]>5 or four["vol_ratio"]>=.8)
    htf_strong=day_strong and four_strong

    # 1H is timing: cooling/reset inside intact 1D/4H strength is desirable.
    one_reset=one["stoch_k"]<=55 or one["stoch_min3"]<=30
    one_turn=one["stoch_k"]>one["stoch_d"] and one["stoch_k"]>one["stoch_k_prev"]
    one_structure=one["price"]>=one["ema50"] and one["rsi"]>=38
    one_mom_improving=one["macd_hist"]>one["macd_hist_prev"] or one["obv_fast_up"] or one_turn
    controlled_reset=one_structure and one_reset and one["ret3"]>=-7

    fast_turn=fast["stoch_k"]>fast["stoch_d"] and fast["stoch_k"]>fast["stoch_k_prev"] and fast["rsi"]>=40
    fast_confirm=fast_turn and (fast["macd_hist"]>fast["macd_hist_prev"] or fast["obv_fast_up"]) and fast["price"]>=fast["ema20"]*.995

    # Keep pressure/breakout path so momentum continuation is not lost.
    pressure=day_trend and four_trend and one["price"]>=one["ema20"] and one["ema20_slope"]>0 and one["rsi"]>=50 and one["higher_closes6"]>=3 and one["higher_lows6"]>=3 and one["near_high20_pct"]<=4.5
    fast_break=fast["price"]>=fast["prev_high6"]*.998 and fast["rsi"]>=48 and (fast["macd_hist"]>fast["macd_hist_prev"] or fast["obv_fast_up"])

    retrigger=htf_strong and controlled_reset
    first_price=sf((prior or {}).get("first_price"),live); chase=pct(live,first_price) if first_price else 0
    structural_break=(day["price"]<day["ema50"] and four["price"]<four["ema50"]) or (four["price"]<four["ema50"] and one["price"]<one["ema50"] and four["ema50_slope"]<0)
    blowoff=one["ret3"]>14 and one["stoch_k"]>85 and one["dist_ema20"]>15
    chased=chase>8 and not one_reset

    q=(20 if day_strong else 10 if day_trend else 0)+(24 if four_strong else 12 if four_trend else 0)+(14 if controlled_reset else 0)+(8 if one_turn else 0)+(7 if one_mom_improving else 0)+(8 if fast_confirm else 0)+(6 if four["ret6"]>5 else 0)+(5 if day["rsi"]>=65 else 0)
    pq=(18 if day_trend else 0)+(20 if four_trend else 0)+(24 if pressure else 0)+(16 if fast_break else 0)+(7 if one["obv_up"] else 0)+(5 if one["taker_buy_ratio"]>=.52 else 0)
    if regime=="RED": q-=5; pq-=5
    quality=clamp(max(q,pq)); decision="REDDET"; phase="NONE"; kind="NONE"; why="Yeterli kurulum yok"

    if not structural_break and not blowoff and not chased:
        if pressure:
            decision="TETIK_BEKLE"; phase="PRESSURE"; kind="PRESSURE"; why="Üst zaman yapısı güçlü; momentum devam tetiği izleniyor"
        if retrigger:
            decision="TETIK_BEKLE"; phase="COOLING" if not one_turn else "ARMED"; kind="RETRIGGER"; why="Güçlü 1D/4H içinde 1H kontrollü reset; yeniden tetik izleniyor"
        observed_new_bar=bool(prior and fast["bar_id"]>prior_bar)
        eligible_history=bool(prior and observed_new_bar and prior_phase in {"COOLING","ARMED","PRESSURE","FORMING"})
        retrigger_ready=retrigger and one_mom_improving and fast_confirm
        pressure_ready=pressure and fast_break
        if eligible_history and quality>=FINAL_MIN_QUALITY and (retrigger_ready or pressure_ready):
            decision="ALIM_ADAYI"; phase="ENTRY"; kind="RETRIGGER" if retrigger_ready and q>=pq else "PRESSURE"; why="İzleme sonrası yeni kapalı 15M mumda giriş tetiği doğrulandı"
        elif decision=="REDDET" and (day_trend or four_trend):
            decision="TETIK_BEKLE"; phase="FORMING"; kind="FORMING"; why="Üst zaman yapısı korunuyor; 1H reset/tetik oluşumu bekleniyor"
    if structural_break: decision="REDDET"; phase="BROKEN"; kind="NONE"; why="Üst zaman yapısı bozulmuş"
    if blowoff or chased: decision="REDDET"; phase="LATE"; kind="NONE"; why="1H hareketi gerçek blow-off/chase bölgesinde"

    ss={"symbol":symbol,"live_price":live,"1d":day,"4h":four,"1h":one,"15m":fast,"btc_regime":regime}; dd={"decision":decision,"confidence":round(quality,1),"state":phase,"setup_kind":kind,"why_now":why,"risk_flags":(["BTC sert baskı altında"] if regime=="RED" else [])}; return Candidate(symbol,symbol[:-4],qv,quality+min(8,max(0,pre_rank-55)*.25),ss,dd)

def levels(c):
    p=sf(c.snapshot["live_price"]); h=c.snapshot["1h"]; f=c.snapshot["4h"]; sups=[sf(x) for x in h["supports"]+f["supports"] if 0<sf(x)<p]; ress=sorted(set(sf(x) for x in h["resistances"]+f["resistances"] if sf(x)>p)); support=max(sups) if sups else min(h["ema20"],h["ema50"],p*.96); stop=support*.975; tp1=next((r for r in ress if pct(r,p)>=2.5),p*1.035); tp2=next((r for r in ress if r>tp1 and pct(r,p)>=5),max(tp1*1.02,p*1.055)); return {"price":p,"support":support,"stop":stop,"tp1":tp1,"tp2":tp2}

def discover(state):
    pre,total=prefilter_candidates(); regime=btc_regime(); watch=state.setdefault("watch",{}); out=[]
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS,6)) as ex:
        fs={ex.submit(evaluate,s,q,r,regime,watch.get(s)):s for s,q,r in pre}
        for f in as_completed(fs):
            try: out.append(f.result())
            except Exception as exc: print(f"[SCAN] {fs[f]}: {exc}",flush=True)
    out.sort(key=lambda c:c.rank,reverse=True); return [c for c in out if c.decision["decision"]=="ALIM_ADAYI"],[c for c in out if c.decision["decision"]=="TETIK_BEKLE"],{"universe":total,"evaluated":len(out),"btc_regime":regime}

def _payload(c):
    lv=levels(c); p=lv["price"]; stop_pct=max(0,pct(p,lv["stop"])); target_pct=max(0,pct(lv["tp1"],p)); rr=target_pct/stop_pct if stop_pct else 0
    return {"symbol":c.symbol.replace("USDT","/USDT"),"entry":round(p,10),"limit_price":round(p,10),"signal_price":round(p,10),"stop":round(lv["stop"],10),"tp1":round(lv["tp1"],10),"tp2":round(lv["tp2"],10),"tp3":None,"sig_type":"spot_opportunity","sub_type":"","source":"spot-scanner","phase":"manual_review","target_pct":round(target_pct,2),"stop_pct":round(stop_pct,2),"rr":round(rr,2),"setup":c.decision["state"],"setup_kind":c.decision["setup_kind"],"score":c.decision["confidence"],"btc_regime":c.snapshot["btc_regime"]}

def _send_portfolio(c):
    if not FINAL_OUTPUT_ENABLED or not PORTFOLIO_URL: return ""
    h={"Content-Type":"application/json"}
    if PORTFOLIO_TOKEN: h["Authorization"]=f"Bearer {PORTFOLIO_TOKEN}"
    try:
        r=HTTP.post(f"{PORTFOLIO_URL}/api/signal",json=_payload(c),headers=h,timeout=15); return "ok" if r.status_code in (200,201,409) else ""
    except Exception: return ""

def _send_telegram(c):
    if not FINAL_OUTPUT_ENABLED or not TELEGRAM_ENABLED or not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return False
    lv=levels(c); text=f"#{c.base} SPOT ADAYI\nFiyat: {lv['price']}\nStop: {lv['stop']}\nTP1: {lv['tp1']}\nTP2: {lv['tp2']}\nKalite: %{c.decision['confidence']:.0f}"; payload={"chat_id":TELEGRAM_CHAT_ID,"text":text}
    if TELEGRAM_THREAD_ID: payload["message_thread_id"]=TELEGRAM_THREAD_ID
    try: return HTTP.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",json=payload,timeout=15).ok
    except Exception: return False

runtime={"status":"BOOT","version":"v10","outputs_enabled":FINAL_OUTPUT_ENABLED,"last_scan":None,"symbols":0,"evaluated":0,"watching":0,"signals":0,"btc_regime":None,"last_error":None}
def scan_cycle():
    runtime.update({"status":"SCANNING","signals":0,"last_error":None}); state=_load_state(); _clean_watch(state)
    try:
        finals,waits,stats=discover(state); watch=state.setdefault("watch",{}); now=time.time()
        for c in waits:
            rec=watch.get(c.symbol) or {"first_seen":now,"first_price":c.snapshot["live_price"],"observations":0,"last_bar_15m":0}; bar=c.snapshot["15m"]["bar_id"]
            if int(sf(rec.get("last_bar_15m"),0)) and bar>int(sf(rec.get("last_bar_15m"),0)): rec["observations"]=int(sf(rec.get("observations",0)))+1
            rec.update({"updated_at":now,"phase":c.decision["state"],"setup_kind":c.decision["setup_kind"],"price":c.snapshot["live_price"],"score":c.decision["confidence"],"last_bar_15m":bar}); watch[c.symbol]=rec
        room=_daily_room(state); selected=finals[:room]; emitted=0
        for c in selected:
            lv=levels(c); print(f"[FINAL CANDIDATE] {c.symbol} {c.decision['setup_kind']} q={c.decision['confidence']} entry={lv['price']} stop={lv['stop']} tp1={lv['tp1']} tp2={lv['tp2']}",flush=True)
            if FINAL_OUTPUT_ENABLED:
                portfolio_ok=bool(_send_portfolio(c)); telegram_ok=bool(_send_telegram(c)); ok=portfolio_ok or telegram_ok
                if ok: state["signal_count"]=int(state.get("signal_count",0))+1; watch.pop(c.symbol,None); emitted+=1
        _save_state(state); runtime.update({"status":"RUNNING","last_scan":now_tr().isoformat(),"symbols":stats["universe"],"evaluated":stats["evaluated"],"watching":len(watch),"signals":emitted,"btc_regime":stats["btc_regime"]}); print(f"[SCAN DONE] evren={stats['universe']} evaluated={stats['evaluated']} watch={len(watch)} final={len(selected)} sent={emitted} BTC={stats['btc_regime']}",flush=True)
    except Exception as exc:
        runtime.update({"status":"ERROR","last_error":f"{type(exc).__name__}: {exc}","last_scan":now_tr().isoformat()}); _save_state(state); print(f"[SCAN ERROR] {exc}",flush=True)

def scan_loop():
    if not SCAN_ON_START: time.sleep(SCAN_INTERVAL_SECONDS)
    while True: scan_cycle(); time.sleep(SCAN_INTERVAL_SECONDS)
@app.route("/")
def index(): return jsonify({"service":"SPOT_SCANNER",**runtime})
@app.route("/health")
def health(): return jsonify(runtime),200
if __name__=="__main__":
    print("SPOT_SCANNER v10 — HTF STRENGTH + 1H RESET/RETRIGGER",flush=True); print(f"FINAL_OUTPUT_ENABLED={FINAL_OUTPUT_ENABLED} | top_n={PYTHON_TOP_N} | core={PREFILTER_CORE_N} | max/day={MAX_SIGNALS_PER_DAY}",flush=True); threading.Thread(target=scan_loop,daemon=True,name="spot-scanner").start(); app.run(host="0.0.0.0",port=int(os.getenv("PORT","10000")),threaded=True)
