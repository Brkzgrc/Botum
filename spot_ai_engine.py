# -*- coding: utf-8 -*-
"""AI spot discovery engine: Python -> Gemini Flash-Lite -> Sonnet final.

No orders are placed here. Only closed Binance spot candles are used for decisions.
The production service wrapper lives in spot_opportunity_scanner.py.
"""
from __future__ import annotations

import json
import math
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import numpy as np
import pandas as pd
import requests

BINANCE = "https://api.binance.com"
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
TR_TZ = timezone(timedelta(hours=3))
HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "Botum-AISpotScanner/1.0"})

GEMINI_API_KEY = (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or "").strip()
GEMINI_MODEL = os.getenv("GEMINI_SCANNER_MODEL", "gemini-3.5-flash-lite").strip()
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
SONNET_MODEL = os.getenv("SCANNER_SONNET_MODEL", "claude-sonnet-5").strip()
SONNET_MAX_TOKENS = max(600, int(os.getenv("AI_SONNET_MAX_TOKENS", "1800")))
SONNET_INPUT_USD_PER_M = float(os.getenv("SONNET_INPUT_USD_PER_M", "2"))
SONNET_OUTPUT_USD_PER_M = float(os.getenv("SONNET_OUTPUT_USD_PER_M", "10"))
MAX_WORKERS = max(1, min(10, int(os.getenv("MAX_WORKERS", "5"))))
MIN_QUOTE_VOLUME = float(os.getenv("MIN_QUOTE_VOLUME", "0"))
PYTHON_TOP_N = max(6, int(os.getenv("AI_PYTHON_TOP_N", "24")))
GEMINI_BATCH_SIZE = max(2, min(12, int(os.getenv("AI_GEMINI_BATCH_SIZE", "8"))))
GEMINI_MIN_QUALITY = float(os.getenv("AI_GEMINI_MIN_QUALITY", "72"))
SONNET_MIN_GEMINI_QUALITY = float(os.getenv("AI_SONNET_MIN_GEMINI_QUALITY", "82"))
SONNET_MAX_FINALISTS = max(1, min(5, int(os.getenv("AI_SONNET_MAX_FINALISTS", "2"))))
SONNET_MIN_CONFIDENCE = float(os.getenv("AI_SONNET_MIN_CONFIDENCE", "72"))

IGNORED_BASES = {
    "USDT","USDC","BUSD","TUSD","DAI","PAX","HUSD","USDP","GUSD","FDUSD","EUR","TRY","GBP","USD",
    "BRL","RUB","AUD","XUSD","USD1","USDE","BFUSD","USDS","USDD","PYUSD","AEUR","EURI","USTC","FRAX",
    "LUSD","SUSD","USDX","CUSD","OUSD","MUSD","RLUSD","BIDR","IDRT","VAI","PAXG","XAUT","WBTC","WETH",
    "WBNB","BETH","BTCB","HBTC","U",
}
LEVERAGED_SUFFIXES = ("UP","DOWN","BULL","BEAR","2L","2S","3L","3S","5L","5S","10L","10S")
CRYPTO_BASES_ENDING_B = {"BNB","DGB","TRB","CKB","SHIB","ARB","BB","YB"}


@dataclass
class Candidate:
    symbol: str
    base: str
    quote_volume_24h: float
    rank_score: float
    setup_hint: str
    one_h: dict[str, Any]
    snapshot: dict[str, Any] = field(default_factory=dict)
    gemini: dict[str, Any] = field(default_factory=dict)


def now_tr() -> datetime:
    return datetime.now(timezone.utc).astimezone(TR_TZ)


def sf(v: Any, default: float = 0.0) -> float:
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except Exception:
        return default


def pct(new: float, old: float) -> float:
    return (new / old - 1.0) * 100.0 if old else 0.0


def clamp(v: float, lo: float = 0, hi: float = 100) -> float:
    return max(lo, min(hi, float(v)))


def _get(path: str, params: Optional[dict[str, Any]] = None, attempts: int = 4) -> Any:
    last = None
    for i in range(attempts):
        try:
            r = HTTP.get(BINANCE + path, params=params, timeout=15)
            if r.status_code in (418, 429):
                time.sleep(2 ** i)
                continue
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            last = exc
            time.sleep(.35 * (2 ** i))
    raise RuntimeError(f"Binance API failed {path}: {last}")


def ohlcv(symbol: str, interval: str, limit: int = 240) -> pd.DataFrame:
    rows = _get("/api/v3/klines", {"symbol": symbol, "interval": interval, "limit": limit})
    if not isinstance(rows, list) or len(rows) < 80:
        raise ValueError(f"insufficient candles {symbol} {interval}")
    cols = ["open_time","open","high","low","close","volume","close_time","quote_volume","trades","taker_base","taker_quote","ignore"]
    d = pd.DataFrame(rows, columns=cols)
    for c in ("open","high","low","close","volume","quote_volume","taker_quote"):
        d[c] = pd.to_numeric(d[c], errors="coerce")
    d["open_time"] = pd.to_datetime(d.open_time, unit="ms", utc=True)
    d["close_time"] = pd.to_datetime(d.close_time, unit="ms", utc=True)
    if rows and int(rows[-1][6]) >= int(time.time() * 1000):
        d = d.iloc[:-1].copy()
    return d.dropna(subset=["open","high","low","close","volume"]).reset_index(drop=True)


def _rsi(s: pd.Series, n: int = 14) -> pd.Series:
    delta = s.diff(); gain = delta.clip(lower=0); loss = -delta.clip(upper=0)
    ag = gain.ewm(alpha=1/n, adjust=False, min_periods=n).mean()
    al = loss.ewm(alpha=1/n, adjust=False, min_periods=n).mean()
    rs = ag / al.replace(0, np.nan)
    return (100 - 100/(1+rs)).fillna(50)


def indicators(d: pd.DataFrame) -> pd.DataFrame:
    x = d.copy()
    x["ema20"] = x.close.ewm(span=20, adjust=False).mean(); x["ema50"] = x.close.ewm(span=50, adjust=False).mean(); x["ema200"] = x.close.ewm(span=200, adjust=False).mean()
    x["rsi"] = _rsi(x.close)
    lo = x.rsi.rolling(14).min(); hi = x.rsi.rolling(14).max(); raw = 100*(x.rsi-lo)/(hi-lo).replace(0, np.nan)
    x["stoch_k"] = raw.rolling(3).mean().fillna(50); x["stoch_d"] = x.stoch_k.rolling(3).mean().fillna(50)
    e12 = x.close.ewm(span=12, adjust=False).mean(); e26 = x.close.ewm(span=26, adjust=False).mean(); x["macd"] = e12-e26
    x["macd_signal"] = x.macd.ewm(span=9, adjust=False).mean(); x["macd_hist"] = x.macd-x.macd_signal
    pc = x.close.shift(1); tr = pd.concat([x.high-x.low,(x.high-pc).abs(),(x.low-pc).abs()], axis=1).max(axis=1)
    x["atr"] = tr.ewm(alpha=1/14, adjust=False, min_periods=14).mean(); x["vol_ratio"] = x.volume/x.volume.rolling(20).mean().replace(0,np.nan)
    x["obv"] = (np.sign(x.close.diff()).fillna(0)*x.volume).cumsum()
    mid = x.close.rolling(20).mean(); std = x.close.rolling(20).std(ddof=0); x["bb_mid"] = mid; x["bb_upper"] = mid+2*std; x["bb_lower"] = mid-2*std
    return x


def _swings(d: pd.DataFrame, wing: int = 2, lookback: int = 80) -> tuple[list[float], list[float]]:
    x = d.tail(lookback).reset_index(drop=True); highs: list[float] = []; lows: list[float] = []
    for i in range(wing, len(x)-wing):
        w = x.iloc[i-wing:i+wing+1]
        if x.high.iloc[i] >= w.high.max(): highs.append(float(x.high.iloc[i]))
        if x.low.iloc[i] <= w.low.min(): lows.append(float(x.low.iloc[i]))
    return highs[-5:], lows[-5:]


def tf_snapshot(d: pd.DataFrame, label: str) -> dict[str, Any]:
    x = indicators(d); a = x.iloc[-1]; b = x.iloc[-2]; p = sf(a.close); highs, lows = _swings(x)
    supports = sorted([v for v in lows if v < p], reverse=True)[:3]; resistances = sorted([v for v in highs if v > p])[:3]
    atr = sf(a.atr); rng = max(sf(a.high)-sf(a.low), 1e-12); body = abs(sf(a.close)-sf(a.open))
    return {
        "tf": label, "closed_at": str(a.close_time), "price": p, "bar_id": int(pd.Timestamp(a.open_time).timestamp()),
        "returns_pct": {"3bars": round(pct(p,sf(x.close.iloc[-4])),3), "6bars": round(pct(p,sf(x.close.iloc[-7])),3), "24bars": round(pct(p,sf(x.close.iloc[-25])),3)},
        "ema": {"20": round(sf(a.ema20),10), "50": round(sf(a.ema50),10), "200": round(sf(a.ema200),10), "price_vs_20_pct": round(pct(p,sf(a.ema20)),3), "price_vs_50_pct": round(pct(p,sf(a.ema50)),3), "price_vs_200_pct": round(pct(p,sf(a.ema200)),3)},
        "momentum": {"rsi": round(sf(a.rsi),2), "rsi_prev": round(sf(b.rsi),2), "stoch_k": round(sf(a.stoch_k),2), "stoch_d": round(sf(a.stoch_d),2), "stoch_k_prev": round(sf(b.stoch_k),2), "macd_hist": round(sf(a.macd_hist),10), "macd_hist_prev": round(sf(b.macd_hist),10)},
        "volume": {"ratio_20": round(sf(a.vol_ratio,1),3), "obv_5bar_direction": "up" if sf(a.obv)>=sf(x.obv.iloc[-6]) else "down"},
        "volatility": {"atr": round(atr,10), "atr_pct": round(100*atr/p,3) if p else 0, "bb_upper": round(sf(a.bb_upper),10), "bb_mid": round(sf(a.bb_mid),10), "bb_lower": round(sf(a.bb_lower),10)},
        "candle": {"change_pct": round(pct(sf(a.close),sf(a.open)),3), "body_ratio": round(body/rng,3), "lower_wick_ratio": round((min(sf(a.open),sf(a.close))-sf(a.low))/rng,3), "upper_wick_ratio": round((sf(a.high)-max(sf(a.open),sf(a.close)))/rng,3)},
        "levels": {"supports": [round(v,10) for v in supports], "resistances": [round(v,10) for v in resistances], "high_20": round(sf(x.high.tail(20).max()),10), "low_20": round(sf(x.low.tail(20).min()),10)},
    }


def universe() -> list[tuple[str,float]]:
    ex = _get("/api/v3/exchangeInfo"); ticks = _get("/api/v3/ticker/24hr"); tm = {x.get("symbol"):x for x in ticks if isinstance(x,dict)}; out=[]
    for it in ex.get("symbols",[]):
        sym=it.get("symbol",""); base=it.get("baseAsset","")
        if it.get("quoteAsset")!="USDT" or it.get("status")!="TRADING" or it.get("isSpotTradingAllowed") is False: continue
        if base in IGNORED_BASES: continue
        if base.endswith(LEVERAGED_SUFFIXES) and base not in CRYPTO_BASES_ENDING_B: continue
        qv=sf((tm.get(sym) or {}).get("quoteVolume"))
        if qv>=MIN_QUOTE_VOLUME: out.append((sym,qv))
    return out


def _prefilter(symbol: str, qv: float) -> Optional[Candidate]:
    try:
        x=indicators(ohlcv(symbol,"1h",240)); a=x.iloc[-1]; b=x.iloc[-2]; p=sf(a.close); e20=sf(a.ema20); e50=sf(a.ema50); e200=sf(a.ema200)
        r=sf(a.rsi); sk=sf(a.stoch_k); sd=sf(a.stoch_d); sk0=sf(b.stoch_k); mh=sf(a.macd_hist); mh0=sf(b.macd_hist); vr=sf(a.vol_ratio,1)
        ret3=pct(p,sf(x.close.iloc[-4])); ret6=pct(p,sf(x.close.iloc[-7])); ret24=pct(p,sf(x.close.iloc[-25])); h20=sf(x.high.tail(20).max()); dh=pct(h20,p)
        score=0.0
        score += 12 if p>e20 else 0; score += 11 if e20>e50 else 0; score += 7 if e50>e200 else 0
        score += 12 if 45<=r<=72 else 5 if 72<r<=80 else 0
        retrigger=sk>sd and sk>sk0; cooling=sk<sd and p>=e20 and ret3>-3
        score += 13 if retrigger else 8 if cooling else 0; score += 10 if mh>mh0 else 0; score += 8 if sf(a.obv)>=sf(x.obv.iloc[-6]) else 0
        score += 7 if .7<=vr<=4.5 else 2 if vr>4.5 else 0; score += 9 if -3.5<=ret6<=8 else 3 if 8<ret6<=14 else 0
        score += 5 if -8<=ret24<=18 else 0; score += 6 if 0<=dh<=6 else 0
        if ret6>14: score-=min(20,(ret6-14)*1.5)
        if r>86: score-=12
        if p<e50 and mh<mh0 and r<42: score-=18
        setup="RETRIGGER" if retrigger and p>e20 else "TIME_COOLING" if cooling else "EARLY_BREAKOUT" if dh<=2 and mh>mh0 else "MOMENTUM_BUILD"
        one={"price":p,"rsi":r,"stoch_k":sk,"stoch_d":sd,"macd_hist":mh,"macd_hist_prev":mh0,"vol_ratio":vr,"ret3h":round(ret3,3),"ret6h":round(ret6,3),"ret24h":round(ret24,3),"bar_id":int(pd.Timestamp(a.open_time).timestamp())}
        return Candidate(symbol,symbol[:-4],qv,round(score,2),setup,one)
    except Exception as exc:
        print(f"[PYTHON] {symbol}: {exc}",flush=True); return None


def python_candidates() -> tuple[list[Candidate],int]:
    uni=universe(); out=[]
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        fs={ex.submit(_prefilter,s,q):s for s,q in uni}
        for f in as_completed(fs):
            c=f.result()
            if c: out.append(c)
    out.sort(key=lambda c:c.rank_score,reverse=True)
    return out[:PYTHON_TOP_N], len(uni)


def enrich(c: Candidate) -> Candidate:
    price=sf(_get("/api/v3/ticker/price",{"symbol":c.symbol}).get("price"))
    c.snapshot={"symbol":c.symbol,"generated_at":now_tr().isoformat(),"live_price":price,"python_rank_score":c.rank_score,"python_setup_hint":c.setup_hint,"quote_volume_24h":c.quote_volume_24h,"timeframes":{"1d":tf_snapshot(ohlcv(c.symbol,"1d"),"1D"),"4h":tf_snapshot(ohlcv(c.symbol,"4h"),"4H"),"1h":tf_snapshot(ohlcv(c.symbol,"1h"),"1H")}}
    return c


def btc_context() -> dict[str,Any]:
    t={"1d":tf_snapshot(ohlcv("BTCUSDT","1d"),"1D"),"4h":tf_snapshot(ohlcv("BTCUSDT","4h"),"4H"),"1h":tf_snapshot(ohlcv("BTCUSDT","1h"),"1H")}
    one=t["1h"]; four=t["4h"]; regime="GREEN"
    if one["returns_pct"]["3bars"]<=-2 or four["returns_pct"]["3bars"]<=-4.5: regime="RED"
    elif one["returns_pct"]["3bars"]<-.8 or four["momentum"]["macd_hist"]<four["momentum"]["macd_hist_prev"]: regime="YELLOW"
    return {"regime":regime,"timeframes":t}


def _extract_json(text: str) -> Any:
    raw=(text or "").strip(); raw=re.sub(r"^```(?:json)?\s*","",raw,flags=re.I); raw=re.sub(r"\s*```$","",raw)
    try: return json.loads(raw)
    except Exception: pass
    for l,r in (("[","]"),("{","}")):
        a=raw.find(l); b=raw.rfind(r)
        if a>=0 and b>a:
            try: return json.loads(raw[a:b+1])
            except Exception: pass
    raise ValueError("model did not return JSON")


GEMINI_SYSTEM="""You are the cheap FILTER layer for Binance spot opportunities, not the final trader. Review closed-candle 1D/4H/1H snapshots. Prefer healthy 1D structure, 4H reset/early trigger/volume-OBV support, and especially 1H momentum cooling while price holds followed by retrigger. Penalize already vertical/late moves. Do not use overbought=must fall. Low volume alone is not an automatic veto. Never invent price levels. Return JSON array only, one object per symbol: {\"symbol\":\"...\",\"verdict\":\"PASS|WATCH|REJECT\",\"quality\":0-100,\"state\":\"PREP|REVERSAL|WARMING|COOLING|RETRIGGER|BREAKOUT_EARLY|BROKEN\",\"reason\":\"short\",\"risk\":\"short\"}. PASS means worth paid Sonnet review; WATCH is close but incomplete."""


def _gemini(prompt: str) -> str:
    if not GEMINI_API_KEY: raise RuntimeError("GEMINI_API_KEY/GOOGLE_API_KEY missing")
    payload={"system_instruction":{"parts":[{"text":GEMINI_SYSTEM}]},"contents":[{"role":"user","parts":[{"text":prompt}]}],"generationConfig":{"responseMimeType":"application/json","maxOutputTokens":2400}}
    r=HTTP.post(f"{GEMINI_BASE}/{GEMINI_MODEL}:generateContent",params={"key":GEMINI_API_KEY},json=payload,timeout=45)
    if not r.ok: raise RuntimeError(f"Gemini HTTP {r.status_code}: {r.text[:220]}")
    data=r.json(); cs=data.get("candidates") or []
    if not cs: raise RuntimeError("Gemini returned no candidate")
    return "".join(str(p.get("text","")) for p in ((cs[0].get("content") or {}).get("parts") or []))


def gemini_filter(items: list[Candidate], btc: dict[str,Any]) -> list[Candidate]:
    out=[]
    for start in range(0,len(items),GEMINI_BATCH_SIZE):
        batch=items[start:start+GEMINI_BATCH_SIZE]
        try:
            data=_extract_json(_gemini(json.dumps({"btc_context":btc,"candidates":[c.snapshot for c in batch]},ensure_ascii=False,separators=(",",":"))))
            if isinstance(data,dict): data=data.get("results") or data.get("candidates") or []
            by={str(x.get("symbol","")).upper():x for x in data if isinstance(x,dict)}
            for c in batch:
                g=by.get(c.symbol,{})
                c.gemini={"verdict":str(g.get("verdict","REJECT")).upper(),"quality":clamp(sf(g.get("quality"))),"state":str(g.get("state","PREP")).upper(),"reason":str(g.get("reason",""))[:500],"risk":str(g.get("risk",""))[:500]}
                if c.gemini["verdict"] in {"PASS","WATCH"} and c.gemini["quality"]>=GEMINI_MIN_QUALITY: out.append(c)
        except Exception as exc: print(f"[GEMINI] batch {start//GEMINI_BATCH_SIZE+1}: {exc}",flush=True)
    out.sort(key=lambda c:(c.gemini.get("verdict")=="PASS",sf(c.gemini.get("quality")),c.rank_score),reverse=True)
    return out


SONNET_SYSTEM="""You are the FINAL DECISION layer for spot crypto. Python and Gemini only discovered candidates; independently verify them from real Binance snapshots. Chain: 1D SETUP -> 4H TRIGGER -> 1H TIMING. Price behavior matters more than mechanical indicator thresholds. A strong pattern is momentum cooling while price gives back little, then 1H retriggers. Reject late vertical chasing. Separate 1H timing invalidation from 4H/1D structural failure. Spot only. Return JSON object only: {\"decision\":\"ALIM_ADAYI|TETIK_BEKLE|REDDET\",\"confidence\":0-100,\"state\":\"PREP|REVERSAL|WARMING|COOLING|RETRIGGER|BREAKOUT_EARLY|BROKEN\",\"thesis\":\"short\",\"why_now\":\"short\",\"entry_mode\":\"NOW|RETRIGGER|BREAKOUT|NONE\",\"entry_low\":number|null,\"entry_high\":number|null,\"near_invalidation\":number|null,\"structure_stop\":number|null,\"target1\":number|null,\"target2\":number|null,\"risk_flags\":[\"...\"]}. Never invent levels not grounded in supplied support/resistance/EMA/ATR/live price. ALIM_ADAYI only if timing is sufficiently formed; otherwise TETIK_BEKLE."""


def _opt(v: Any) -> Optional[float]:
    if v is None: return None
    x=sf(v,float("nan")); return x if math.isfinite(x) and x>0 else None


def sonnet(c: Candidate, btc: dict[str,Any]) -> dict[str,Any]:
    if not ANTHROPIC_API_KEY: raise RuntimeError("ANTHROPIC_API_KEY missing")
    from anthropic import Anthropic
    started=time.time(); client=Anthropic(api_key=ANTHROPIC_API_KEY)
    resp=client.messages.create(model=SONNET_MODEL,max_tokens=SONNET_MAX_TOKENS,system=SONNET_SYSTEM,messages=[{"role":"user","content":json.dumps({"candidate":c.snapshot,"gemini_filter":c.gemini,"btc_context":btc},ensure_ascii=False,separators=(",",":"))}])
    text="".join(b.text for b in resp.content if getattr(b,"type",None)=="text").strip(); usage=getattr(resp,"usage",None); inp=int(getattr(usage,"input_tokens",0) or 0); out=int(getattr(usage,"output_tokens",0) or 0)
    cost=inp/1_000_000*SONNET_INPUT_USD_PER_M+out/1_000_000*SONNET_OUTPUT_USD_PER_M; duration=time.time()-started
    print(f"[SONNET SCANNER USAGE] {c.symbol} model={SONNET_MODEL} input={inp} output={out} total={inp+out} cost=${cost:.5f} duration={duration:.1f}s",flush=True)
    d=_extract_json(text)
    if not isinstance(d,dict): raise ValueError("Sonnet did not return object")
    return {"decision":str(d.get("decision","REDDET")).upper(),"confidence":clamp(sf(d.get("confidence"))),"state":str(d.get("state",c.gemini.get("state","PREP"))).upper(),"thesis":str(d.get("thesis",""))[:800],"why_now":str(d.get("why_now",""))[:800],"entry_mode":str(d.get("entry_mode","NONE")).upper(),"entry_low":_opt(d.get("entry_low")),"entry_high":_opt(d.get("entry_high")),"near_invalidation":_opt(d.get("near_invalidation")),"structure_stop":_opt(d.get("structure_stop")),"target1":_opt(d.get("target1")),"target2":_opt(d.get("target2")),"risk_flags":[str(x)[:300] for x in (d.get("risk_flags") or [])][:6],"usage":{"input":inp,"output":out,"cost_usd":round(cost,6),"duration_s":round(duration,2)}}


def levels(c: Candidate, decision: dict[str,Any]) -> dict[str,float]:
    snap=c.snapshot; p=sf(snap.get("live_price")) or sf(snap["timeframes"]["1h"]["price"]); h=snap["timeframes"]["1h"]
    sups=[sf(x) for x in h["levels"].get("supports",[]) if 0<sf(x)<p]; ress=[sf(x) for x in h["levels"].get("resistances",[]) if sf(x)>p]
    support=sups[0] if sups else sf(h["levels"].get("low_20"),p*.95); stop_default=support*.975; stop=decision.get("structure_stop") or stop_default
    if stop>=p or pct(p,stop)<.5: stop=stop_default
    t1=decision.get("target1"); t1=t1 if t1 and t1>p else ress[0] if ress else p*1.03
    t2=decision.get("target2"); t2=t2 if t2 and t2>t1 else max(t1*1.02,p*1.05)
    lo=decision.get("entry_low") or p*.997; hi=decision.get("entry_high") or p*1.003
    if lo>hi: lo,hi=hi,lo
    if abs(pct(lo,p))>6 or abs(pct(hi,p))>6: lo,hi=p*.997,p*1.003
    return {"price":p,"entry_low":lo,"entry_high":hi,"stop":stop,"tp1":t1,"tp2":t2}


def discover() -> tuple[list[tuple[Candidate,dict[str,Any]]],dict[str,Any]]:
    started=time.time(); pre,total=python_candidates(); btc=btc_context(); enriched=[]
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS,6)) as ex:
        fs={ex.submit(enrich,c):c for c in pre}
        for f in as_completed(fs):
            try: enriched.append(f.result())
            except Exception as exc: print(f"[SNAPSHOT] {fs[f].symbol}: {exc}",flush=True)
    enriched.sort(key=lambda c:c.rank_score,reverse=True)
    g=gemini_filter(enriched,btc)
    finalists=[c for c in g if sf(c.gemini.get("quality"))>=SONNET_MIN_GEMINI_QUALITY and c.gemini.get("state") in {"RETRIGGER","BREAKOUT_EARLY","REVERSAL","WARMING","COOLING"}]
    finalists.sort(key=lambda c:(c.gemini.get("verdict")=="PASS",sf(c.gemini.get("quality")),c.rank_score),reverse=True)
    finals=[]
    for c in finalists[:SONNET_MAX_FINALISTS]:
        try:
            d=sonnet(c,btc)
            if d["decision"]=="ALIM_ADAYI" and d["confidence"]>=SONNET_MIN_CONFIDENCE: finals.append((c,d))
            else: print(f"[SONNET] {c.symbol} -> {d['decision']} %{d['confidence']:.0f}",flush=True)
        except Exception as exc: print(f"[SONNET] {c.symbol}: {type(exc).__name__}: {exc}",flush=True)
    stats={"universe":total,"python":len(pre),"gemini":len(g),"sonnet":min(len(finalists),SONNET_MAX_FINALISTS),"signals":len(finals),"btc_regime":btc["regime"],"duration_s":round(time.time()-started,1)}
    return finals,stats
