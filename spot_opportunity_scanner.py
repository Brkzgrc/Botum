# -*- coding: utf-8 -*-
"""SPOT_SCANNER production service — deterministic multi-timeframe scanner.

Goal:
  Reproduce the manual chart-reading workflow directly from Binance data:
  1D setup -> 4H trigger -> 1H timing.

No Gemini/Claude final veto, no paid AI call, no auto orders.
The scanner searches Binance Spot itself and emits only strong manual-review candidates.
"""
from __future__ import annotations

import html
import json
import logging
import math
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import numpy as np
import pandas as pd
import requests
from flask import Flask, jsonify

BINANCE = "https://api.binance.com"
TR_TZ = timezone(timedelta(hours=3))
HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "Botum-VisualSpotScanner/3.0"})

MAX_WORKERS = max(2, min(10, int(os.getenv("MAX_WORKERS", "6"))))
PYTHON_TOP_N = max(20, min(60, int(os.getenv("VISUAL_TOP_N", "36"))))
MIN_QUOTE_VOLUME = float(os.getenv("MIN_QUOTE_VOLUME", "0"))
SIGNAL_SCORE = float(os.getenv("VISUAL_SIGNAL_SCORE", "68"))
WATCH_SCORE = float(os.getenv("VISUAL_WATCH_SCORE", "58"))
SCAN_INTERVAL_SECONDS = max(300, int(os.getenv("SCAN_INTERVAL_SECONDS", "900")))
ALERT_COOLDOWN_HOURS = float(os.getenv("ALERT_COOLDOWN_HOURS", "4"))
DRY_RUN = os.getenv("DRY_RUN", "true").strip().lower() == "true"
SCAN_ON_START = os.getenv("SCAN_ON_START", "true").strip().lower() == "true"
STATE_FILE = os.getenv("SCANNER_STATE_FILE", "/tmp/spot_visual_scanner_state_v3.json")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TELEGRAM_THREAD_ID = int(os.getenv("SIGNAL_THREAD_ID", "2"))
PORTFOLIO_URL = os.getenv("PORTFOLIO_URL", "").rstrip("/")
PORTFOLIO_TOKEN = os.getenv("PORTFOLIO_TOKEN", "")

IGNORED_BASES = {
    "USDT","USDC","BUSD","TUSD","DAI","PAX","HUSD","USDP","GUSD","FDUSD","EUR","TRY","GBP","USD",
    "BRL","RUB","AUD","XUSD","USD1","USDE","BFUSD","USDS","USDD","PYUSD","AEUR","EURI","USTC","FRAX",
    "LUSD","SUSD","USDX","CUSD","OUSD","MUSD","RLUSD","BIDR","IDRT","VAI","PAXG","XAUT","WBTC","WETH",
    "WBNB","BETH","BTCB","HBTC","U",
}
LEVERAGED_SUFFIXES = ("UP","DOWN","BULL","BEAR","2L","2S","3L","3S","5L","5S","10L","10S")
CRYPTO_BASES_ENDING_B = {"BNB","DGB","TRB","CKB","SHIB","ARB","BB","YB"}

app = Flask(__name__)
logging.getLogger("werkzeug").setLevel(logging.ERROR)


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


def clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, float(v)))


def _get(path: str, params: Optional[dict[str, Any]] = None, attempts: int = 4) -> Any:
    last = None
    for i in range(attempts):
        try:
            r = HTTP.get(BINANCE + path, params=params, timeout=15)
            if r.status_code in (418, 429):
                time.sleep(1.5 * (2 ** i))
                continue
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            last = exc
            time.sleep(.3 * (2 ** i))
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
    x["ema20"] = x.close.ewm(span=20, adjust=False).mean()
    x["ema50"] = x.close.ewm(span=50, adjust=False).mean()
    x["ema200"] = x.close.ewm(span=200, adjust=False).mean()
    x["rsi"] = _rsi(x.close)
    lo = x.rsi.rolling(14).min(); hi = x.rsi.rolling(14).max()
    raw = 100 * (x.rsi-lo) / (hi-lo).replace(0, np.nan)
    x["stoch_k"] = raw.rolling(3).mean().fillna(50)
    x["stoch_d"] = x.stoch_k.rolling(3).mean().fillna(50)
    e12 = x.close.ewm(span=12, adjust=False).mean(); e26 = x.close.ewm(span=26, adjust=False).mean()
    x["macd"] = e12-e26; x["macd_signal"] = x.macd.ewm(span=9, adjust=False).mean(); x["macd_hist"] = x.macd-x.macd_signal
    pc = x.close.shift(1)
    tr = pd.concat([x.high-x.low, (x.high-pc).abs(), (x.low-pc).abs()], axis=1).max(axis=1)
    x["atr"] = tr.ewm(alpha=1/14, adjust=False, min_periods=14).mean()
    x["vol_ratio"] = x.volume / x.volume.rolling(20).mean().replace(0, np.nan)
    x["obv"] = (np.sign(x.close.diff()).fillna(0) * x.volume).cumsum()
    return x


def _swings(d: pd.DataFrame, wing: int = 2, lookback: int = 80) -> tuple[list[float], list[float]]:
    x = d.tail(lookback).reset_index(drop=True); highs, lows = [], []
    for i in range(wing, len(x)-wing):
        w = x.iloc[i-wing:i+wing+1]
        if x.high.iloc[i] >= w.high.max(): highs.append(float(x.high.iloc[i]))
        if x.low.iloc[i] <= w.low.min(): lows.append(float(x.low.iloc[i]))
    return highs[-8:], lows[-8:]


def tf_snapshot(d: pd.DataFrame, label: str) -> dict[str, Any]:
    x = indicators(d); a=x.iloc[-1]; b=x.iloc[-2]; p=sf(a.close); highs,lows=_swings(x)
    supports=sorted([v for v in lows if v<p],reverse=True)[:4]
    resistances=sorted([v for v in highs if v>p])[:4]
    atr=sf(a.atr); rng=max(sf(a.high)-sf(a.low),1e-12)
    return {
        "tf": label, "price": p, "bar_id": int(pd.Timestamp(a.open_time).timestamp()),
        "ret3": pct(p,sf(x.close.iloc[-4])), "ret6": pct(p,sf(x.close.iloc[-7])), "ret24": pct(p,sf(x.close.iloc[-25])),
        "ema20": sf(a.ema20), "ema50": sf(a.ema50), "ema200": sf(a.ema200),
        "ema20_slope": pct(sf(a.ema20),sf(x.ema20.iloc[-4])),
        "rsi": sf(a.rsi), "rsi_prev": sf(b.rsi),
        "stoch_k": sf(a.stoch_k), "stoch_d": sf(a.stoch_d), "stoch_k_prev": sf(b.stoch_k),
        "macd_hist": sf(a.macd_hist), "macd_hist_prev": sf(b.macd_hist),
        "vol_ratio": sf(a.vol_ratio,1), "obv_up": sf(a.obv)>=sf(x.obv.iloc[-6]),
        "atr_pct": 100*atr/p if p else 0,
        "lower_wick": (min(sf(a.open),sf(a.close))-sf(a.low))/rng,
        "upper_wick": (sf(a.high)-max(sf(a.open),sf(a.close)))/rng,
        "candle_pct": pct(sf(a.close),sf(a.open)),
        "high20": sf(x.high.tail(20).max()), "low20": sf(x.low.tail(20).min()),
        "supports": supports, "resistances": resistances,
    }


@dataclass
class Candidate:
    symbol: str
    base: str
    quote_volume_24h: float
    pre_score: float
    snapshot: dict[str, Any]
    decision: dict[str, Any]


def universe() -> list[tuple[str,float]]:
    ex=_get("/api/v3/exchangeInfo"); ticks=_get("/api/v3/ticker/24hr")
    tm={x.get("symbol"):x for x in ticks if isinstance(x,dict)}; out=[]
    for it in ex.get("symbols",[]):
        sym=it.get("symbol",""); base=it.get("baseAsset","")
        if it.get("quoteAsset")!="USDT" or it.get("status")!="TRADING" or it.get("isSpotTradingAllowed") is False: continue
        if base in IGNORED_BASES: continue
        if base.endswith(LEVERAGED_SUFFIXES) and base not in CRYPTO_BASES_ENDING_B: continue
        qv=sf((tm.get(sym) or {}).get("quoteVolume"))
        if qv>=MIN_QUOTE_VOLUME: out.append((sym,qv))
    return out


def _prefilter(symbol: str, qv: float) -> Optional[tuple[str,float,float]]:
    try:
        x=indicators(ohlcv(symbol,"1h",240)); a=x.iloc[-1]; b=x.iloc[-2]; p=sf(a.close)
        r=sf(a.rsi); vr=sf(a.vol_ratio,1); mh=sf(a.macd_hist); mh0=sf(b.macd_hist)
        sk=sf(a.stoch_k); sd=sf(a.stoch_d); sk0=sf(b.stoch_k)
        e20=sf(a.ema20); e50=sf(a.ema50); e200=sf(a.ema200)
        ret3=pct(p,sf(x.close.iloc[-4])); ret6=pct(p,sf(x.close.iloc[-7])); ret24=pct(p,sf(x.close.iloc[-25]))
        high20=sf(x.high.tail(20).max()); near_high=max(0.0, -pct(p,high20))
        score=0.0
        score += 18 if p>=e20 else 7 if p>=e50 else 0
        score += 10 if e20>=e50 else 3
        score += 7 if e50>=e200 else 0
        score += 12 if 42<=r<=78 else 5 if 78<r<=86 else 0
        score += 12 if mh>mh0 else 0
        score += 10 if sk>sd and sk>sk0 else 5 if sk<35 else 0
        score += 8 if sf(a.obv)>=sf(x.obv.iloc[-6]) else 0
        score += 7 if vr>=0.8 else 2
        score += 8 if near_high<=4.0 else 2 if near_high<=8.0 else 0
        score += 5 if -3.5<=ret3<=6 else 0
        if ret3>8 or ret6>16: score-=18
        if r>90: score-=15
        if p<e50 and mh<mh0 and r<40: score-=25
        return symbol,qv,round(score,2)
    except Exception as exc:
        if "insufficient candles" not in str(exc): print(f"[PREFILTER] {symbol}: {exc}",flush=True)
        return None


def prefilter_candidates() -> tuple[list[tuple[str,float,float]],int]:
    uni=universe(); out=[]
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        fs=[ex.submit(_prefilter,s,q) for s,q in uni]
        for f in as_completed(fs):
            r=f.result()
            if r: out.append(r)
    out.sort(key=lambda x:x[2],reverse=True)
    return out[:PYTHON_TOP_N],len(uni)


def btc_regime() -> str:
    try:
        h=tf_snapshot(ohlcv("BTCUSDT","1h"),"1H"); f=tf_snapshot(ohlcv("BTCUSDT","4h"),"4H")
        if h["ret3"]<=-2.2 or f["ret3"]<=-5 or (h["price"]<h["ema50"] and h["macd_hist"]<h["macd_hist_prev"] and h["rsi"]<40): return "RED"
        if h["ret3"]<-.9 or f["macd_hist"]<f["macd_hist_prev"]: return "YELLOW"
        return "GREEN"
    except Exception:
        return "YELLOW"


def _score_tf(t: dict[str,Any], kind: str) -> tuple[float,list[str],list[str]]:
    p=t["price"]; e20=t["ema20"]; e50=t["ema50"]; e200=t["ema200"]
    r=t["rsi"]; mh=t["macd_hist"]; mh0=t["macd_hist_prev"]
    sk=t["stoch_k"]; sd=t["stoch_d"]; sk0=t["stoch_k_prev"]
    good=[]; bad=[]; score=0.0
    if p>=e20: score+=16; good.append(f"{kind} fiyat EMA20 üstünde")
    elif p>=e50: score+=7; good.append(f"{kind} EMA50 üstünde reset")
    else: bad.append(f"{kind} kısa trend zayıf")
    if e20>=e50: score+=10; good.append(f"{kind} EMA20>EMA50")
    if e50>=e200: score+=6
    if t["ema20_slope"]>0: score+=7
    if 45<=r<=76: score+=12; good.append(f"{kind} RSI sağlıklı {r:.0f}")
    elif 38<=r<45 or 76<r<=84: score+=6
    elif r>90: score-=10; bad.append(f"{kind} RSI aşırı uzamış")
    if mh>mh0: score+=12; good.append(f"{kind} MACD ivmesi artıyor")
    elif mh>0: score+=5
    else: bad.append(f"{kind} MACD ivmesi zayıf")
    if sk>sd and sk>sk0: score+=12; good.append(f"{kind} Stoch RSI yukarı tetik")
    elif sk<35: score+=6; good.append(f"{kind} momentum reset")
    if t["obv_up"]: score+=10; good.append(f"{kind} OBV yukarı")
    else: bad.append(f"{kind} OBV desteklemiyor")
    if t["vol_ratio"]>=1.1: score+=8; good.append(f"{kind} hacim destekli")
    elif t["vol_ratio"]>=.7: score+=4
    if t["lower_wick"]>=.22: score+=5; good.append(f"{kind} alt fitil alıcı savunması")
    return clamp(score),good,bad


def visual_decision(symbol: str, qv: float, pre_score: float, regime: str) -> Candidate:
    one=tf_snapshot(ohlcv(symbol,"1h"),"1H"); four=tf_snapshot(ohlcv(symbol,"4h"),"4H"); day=tf_snapshot(ohlcv(symbol,"1d"),"1D")
    live=sf(_get("/api/v3/ticker/price",{"symbol":symbol}).get("price")) or one["price"]
    s1,g1,b1=_score_tf(day,"1D"); s4,g4,b4=_score_tf(four,"4H"); sh,gh,bh=_score_tf(one,"1H")

    # Manual-chart weighting: setup matters, but timing is decisive.
    score=.25*s1+.34*s4+.41*sh
    positives=(g1+g4+gh); risks=(b1+b4+bh)

    # Continuation pattern: strong higher TF + consolidation/holding near highs.
    near1=max(0.0,-pct(one["price"],one["high20"]))
    near4=max(0.0,-pct(four["price"],four["high20"]))
    continuation=(day["price"]>=day["ema20"] and four["price"]>=four["ema20"] and one["price"]>=one["ema20"] and near1<=3.0 and near4<=5.0 and one["obv_up"])
    if continuation:
        score+=7; positives.append("1D/4H güçlü, 1H zirve yakınında kontrollü tutunuyor")

    # Retrigger after cooling; this is the preferred entry pattern.
    retrigger=(one["stoch_k"]>one["stoch_d"] and one["stoch_k"]>one["stoch_k_prev"] and one["macd_hist"]>one["macd_hist_prev"] and one["price"]>=one["ema20"])
    if retrigger:
        score+=7; positives.append("1H soğuma sonrası yeniden tetik")

    # Do not equate overbought with automatic rejection; only reject late vertical chasing.
    late=(one["ret3"]>8 or one["ret6"]>16 or (one["rsi"]>88 and pct(one["price"],one["ema20"])>6))
    broken=(day["price"]<day["ema50"] and day["macd_hist"]<day["macd_hist_prev"] and four["price"]<four["ema50"] and four["macd_hist"]<four["macd_hist_prev"])
    if late:
        score-=18; risks.append("1H hareket fazla dik; geç kovalamaya dönüşmüş")
    if broken:
        score-=30; risks.append("1D+4H yapı birlikte bozuk")
    if regime=="RED":
        score-=20; risks.append("BTC kısa vadeli rejim RED")
    elif regime=="YELLOW":
        score-=3

    score=clamp(score)
    if not broken and not late and regime!="RED" and score>=SIGNAL_SCORE and (retrigger or continuation):
        decision="ALIM_ADAYI"
    elif score>=WATCH_SCORE and not broken:
        decision="TETIK_BEKLE"
    else:
        decision="REDDET"

    state="RETRIGGER" if retrigger else "CONTINUATION" if continuation else "COOLING" if one["stoch_k"]<35 else "WARMING"
    snapshot={"symbol":symbol,"live_price":live,"1d":day,"4h":four,"1h":one,"btc_regime":regime}
    d={
        "decision":decision,"confidence":round(score,1),"state":state,
        "thesis":"; ".join(positives[:4]) or "Çoklu zaman diliminde yeterli pozitif kanıt yok",
        "why_now":"; ".join([x for x in positives if x.startswith("1H")][:3]) or "1H tetik henüz tam oluşmadı",
        "risk_flags":risks[:4],
    }
    return Candidate(symbol,symbol[:-4],qv,pre_score,snapshot,d)


def levels(c: Candidate, decision: dict[str,Any]) -> dict[str,float]:
    p=sf(c.snapshot["live_price"]); h=c.snapshot["1h"]; f=c.snapshot["4h"]
    sups=[sf(x) for x in h["supports"] if 0<sf(x)<p] + [sf(x) for x in f["supports"] if 0<sf(x)<p]
    ress=[sf(x) for x in h["resistances"] if sf(x)>p] + [sf(x) for x in f["resistances"] if sf(x)>p]
    support=max(sups) if sups else min(h["ema20"],p*.96)
    stop=support*.975
    ress=sorted(set(ress))
    tp1=next((r for r in ress if pct(r,p)>=1.2), p*1.025)
    tp2=next((r for r in ress if r>tp1 and pct(r,p)>=2.5), max(tp1*1.02,p*1.045))
    atrp=max(.15,h["atr_pct"])
    entry_low=p*(1-min(0.012,atrp/100*.35)); entry_high=p*(1+min(0.006,atrp/100*.15))
    return {"price":p,"entry_low":entry_low,"entry_high":entry_high,"stop":stop,"tp1":tp1,"tp2":tp2}


def discover() -> tuple[list[Candidate],dict[str,Any]]:
    started=time.time(); pre,total=prefilter_candidates(); regime=btc_regime(); evaluated=[]
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS,6)) as ex:
        fs={ex.submit(visual_decision,s,q,sc,regime):(s,sc) for s,q,sc in pre}
        for f in as_completed(fs):
            try: evaluated.append(f.result())
            except Exception as exc: print(f"[VISUAL] {fs[f][0]}: {type(exc).__name__}: {exc}",flush=True)
    evaluated.sort(key=lambda c:c.decision["confidence"],reverse=True)
    finals=[c for c in evaluated if c.decision["decision"]=="ALIM_ADAYI"]
    for c in evaluated:
        d=c.decision
        print(f"[VISUAL] {c.symbol} -> {d['decision']} %{d['confidence']:.0f} {d['state']} | {d['thesis']}",flush=True)
    stats={"universe":total,"prefilter":len(pre),"evaluated":len(evaluated),"signals":len(finals),"btc_regime":regime,"duration_s":round(time.time()-started,1)}
    return finals,stats


def _load_state() -> dict[str,Any]:
    try:
        with open(STATE_FILE,encoding="utf-8") as f:
            d=json.load(f); return d if isinstance(d,dict) else {}
    except Exception: return {}


def _save_state(d: dict[str,Any]) -> None:
    try:
        os.makedirs(os.path.dirname(STATE_FILE) or ".",exist_ok=True); tmp=STATE_FILE+".tmp"
        with open(tmp,"w",encoding="utf-8") as f: json.dump(d,f,ensure_ascii=False,indent=2)
        os.replace(tmp,STATE_FILE)
    except Exception as exc: print(f"[STATE] {exc}",flush=True)


def _alert_due(symbol: str, state: dict[str,Any]) -> bool:
    rec=(state.setdefault("alerts",{}).get(symbol) or {})
    return time.time()-sf(rec.get("sent_at"))>=ALERT_COOLDOWN_HOURS*3600


def _fmt(v: float) -> str:
    v=sf(v)
    if v>=1000:return f"{v:,.2f}"
    if v>=100:return f"{v:.2f}"
    if v>=1:return f"{v:.4f}"
    if v>=.01:return f"{v:.6f}"
    return f"{v:.10f}".rstrip("0")


def _portfolio_payload(c: Candidate) -> dict[str,Any]:
    d=c.decision; lv=levels(c,d); p=lv["price"]
    stop_pct=max(0,pct(p,lv["stop"])); target_pct=max(0,pct(lv["tp1"],p)); rr=target_pct/stop_pct if stop_pct else 0
    return {
        "symbol":c.symbol.replace("USDT","/USDT"),"entry":round(p,10),"limit_price":round(p,10),"signal_price":round(p,10),
        "stop":round(lv["stop"],10),"tp1":round(lv["tp1"],10),"tp2":round(lv["tp2"],10),"tp3":None,
        "sig_type":"spot_opportunity","sub_type":f"visual_{d['state'].lower()}","source":"spot-scanner","phase":"manual_review",
        "entry_zone":[round(lv["entry_low"],10),round(lv["entry_high"],10)],"target_pct":round(target_pct,2),"stop_pct":round(stop_pct,2),"rr":round(rr,2),
        "setup":d["state"],"positives":[d["thesis"],d["why_now"]],"risks":d["risk_flags"],"ai_pipeline":"none-deterministic-1d4h1h",
        "visual_score":d["confidence"],"btc_regime":c.snapshot["btc_regime"],
    }


def _send_portfolio(c: Candidate) -> str:
    if not PORTFOLIO_URL:return ""
    headers={"Content-Type":"application/json"}
    if PORTFOLIO_TOKEN:headers["Authorization"]=f"Bearer {PORTFOLIO_TOKEN}"
    try:
        r=HTTP.post(f"{PORTFOLIO_URL}/api/signal",json=_portfolio_payload(c),headers=headers,timeout=15)
        if r.status_code in (200,201):
            try:return str((r.json() or {}).get("id","")) or "ok"
            except Exception:return "ok"
        if r.status_code==409:return "duplicate"
        print(f"[PORTFOLIO] HTTP {r.status_code}: {r.text[:220]}",flush=True)
    except Exception as exc:print(f"[PORTFOLIO] {exc}",flush=True)
    return ""


def _telegram_text(c: Candidate) -> str:
    d=c.decision; lv=levels(c,d); risks=d.get("risk_flags") or []
    risk_text="\n".join(f"• {html.escape(str(x))}" for x in risks[:3]) or "• Belirgin ek risk yok; yapısal stop izlenir."
    return (
        f"<b>🚨 #{c.base} SPOT ADAYI</b>\n🕐 {now_tr().strftime('%d/%m/%Y %H:%M')}\n━━━━━━━━━━━━━━━━━━━━\n\n"
        f"<b>Karar:</b> {d['decision']} | <b>Skor:</b> %{sf(d['confidence']):.0f}\n<b>Durum:</b> {d['state']}\n<b>BTC:</b> {c.snapshot['btc_regime']}\n\n"
        f"<b>Neden?</b>\n{html.escape(d['thesis'])}\n\n<b>Neden şimdi?</b>\n{html.escape(d['why_now'])}\n\n"
        f"<b>Fiyat:</b> {_fmt(lv['price'])}\n<b>Giriş bölgesi:</b> {_fmt(lv['entry_low'])} – {_fmt(lv['entry_high'])}\n"
        f"<b>Yapısal stop:</b> {_fmt(lv['stop'])}\n<b>TP1:</b> {_fmt(lv['tp1'])}\n<b>TP2:</b> {_fmt(lv['tp2'])}\n\n"
        f"<b>Riskler</b>\n{risk_text}\n\n<i>Spot only • Otomatik emir yok • 1D/4H/1H deterministik tarama</i>"
    )


def _send_telegram(c: Candidate) -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:return False
    payload={"chat_id":TELEGRAM_CHAT_ID,"text":_telegram_text(c),"parse_mode":"HTML","disable_web_page_preview":True}
    if TELEGRAM_THREAD_ID:payload["message_thread_id"]=TELEGRAM_THREAD_ID
    try:
        r=HTTP.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",json=payload,timeout=15)
        if not r.ok:print(f"[TELEGRAM] HTTP {r.status_code}: {r.text[:180]}",flush=True)
        return r.ok
    except Exception as exc:print(f"[TELEGRAM] {exc}",flush=True);return False


runtime={"status":"BOOT","strategy":"Binance -> 1D setup -> 4H trigger -> 1H timing","dry_run":DRY_RUN,"last_scan":None,"symbols":0,"evaluated":0,"signals":0,"btc_regime":None,"last_error":None}


def scan_cycle() -> None:
    runtime.update({"status":"SCANNING","last_error":None,"signals":0}); state=_load_state()
    try:
        finals,stats=discover(); sent=0
        runtime.update({"symbols":stats["universe"],"evaluated":stats["evaluated"],"btc_regime":stats["btc_regime"]})
        for c in finals:
            if not _alert_due(c.symbol,state):
                print(f"[COOLDOWN] {c.symbol} tekrar sinyali bastırıldı",flush=True);continue
            lv=levels(c,c.decision)
            print(f"[SIGNAL] {c.symbol} score={c.decision['confidence']} entry={lv['price']} stop={lv['stop']} tp1={lv['tp1']}",flush=True)
            if DRY_RUN:
                print(f"[DRY-RUN] {c.symbol} Portfolio/Telegram gönderilmedi",flush=True);emitted=True
            else:
                pid=_send_portfolio(c); tg=_send_telegram(c); emitted=bool(pid or tg)
            if emitted:
                state.setdefault("alerts",{})[c.symbol]={"sent_at":time.time(),"price":lv["price"],"stop":lv["stop"],"tp1":lv["tp1"],"score":c.decision["confidence"],"state":c.decision["state"]};sent+=1
        _save_state(state); runtime.update({"status":"RUNNING","last_scan":now_tr().isoformat(),"signals":sent})
        print(f"[SCAN DONE] evren={stats['universe']} prefilter={stats['prefilter']} evaluated={stats['evaluated']} sinyal={sent} BTC={stats['btc_regime']} süre={stats['duration_s']}s",flush=True)
    except Exception as exc:
        runtime.update({"status":"ERROR","last_error":f"{type(exc).__name__}: {exc}","last_scan":now_tr().isoformat()});print(f"[SCAN ERROR] {type(exc).__name__}: {exc}",flush=True);_save_state(state)


def scan_loop() -> None:
    if not SCAN_ON_START:time.sleep(SCAN_INTERVAL_SECONDS)
    while True:
        scan_cycle();time.sleep(SCAN_INTERVAL_SECONDS)


@app.route("/")
def index(): return jsonify({"service":"SPOT_SCANNER","engine":"deterministic visual-equivalent 1D/4H/1H",**runtime})

@app.route("/health")
def health(): return jsonify(runtime),200


if __name__=="__main__":
    print("="*72,flush=True)
    print("SPOT_SCANNER — DETERMINISTIC 1D / 4H / 1H",flush=True)
    print("Pipeline: Binance Spot -> broad 1H prefilter -> 1D setup -> 4H trigger -> 1H timing",flush=True)
    print(f"DRY_RUN={DRY_RUN} | scan={SCAN_INTERVAL_SECONDS}s | top_n={PYTHON_TOP_N} | signal_score>={SIGNAL_SCORE:.0f}",flush=True)
    print("Paid AI: OFF (no Gemini/Claude final veto)",flush=True)
    print("="*72,flush=True)
    threading.Thread(target=scan_loop,daemon=True,name="spot-visual-scanner").start()
    app.run(host="0.0.0.0",port=int(os.getenv("PORT","10000")),threaded=True)
