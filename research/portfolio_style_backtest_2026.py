# -*- coding: utf-8 -*-
"""
Güncel spot_opportunity_scanner.py için yerel, geçmişe dönük Portfolio backtesti.

Örnek:
  pip install -r requirements.txt
  python research/portfolio_style_backtest_2026.py --start 2026-01-01 --end 2026-09-10 --symbols 120

Çıktı: research/output/portfolio_style_backtest_YYYYMMDD.json
Not: Bu test bugünkü scanner kurallarını geçmişte yeniden oynatır.
"""
from __future__ import annotations
import argparse, json, math, os, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta
from pathlib import Path

import pandas as pd
import requests
import spot_opportunity_scanner as scanner

COLS = ["open_time","open","high","low","close","volume","close_time",
        "quote_volume","trades","taker_base","taker_quote","ignore"]
INTERVALS = {"15m": 1200, "1h": 320, "4h": 320, "1d": 320}
LOOKBACK = {"15m": 18, "1h": 20, "4h": 60, "1d": 320}
# Tarihsel evren kusursuz yeniden kurulamıyor; sonuç raporu bu sınırlamayı taşır.
BASES = """BTC ETH BNB SOL XRP DOGE ADA TRX SUI LINK AVAX TON SHIB LTC HBAR DOT BCH XLM
PEPE UNI AAVE NEAR APT ICP FIL ATOM ETC VET ALGO ARB OP INJ SEI RENDER FET TAO WLD TIA JUP
ONDO ENA PENDLE PYTH JTO ZRO WIF BONK FLOKI TURBO CAKE ZEC DASH KAS KAVA GALA SAND MANA AXS
IMX THETA MKR CRV LDO RUNE FTM KNC SNX COMP SUSHI YFI 1INCH ENS MASK CHZ ENJ FLOW EGLD MINA
ROSE KSM ZIL IOTA XTZ EOS NEO QNT GRT STX RAY RNDR ASTR CFX APE BLUR DYDX GMX MAGIC LRC BAT
ZRX OCEAN SKL COTI DENT CELR SXP CTSI BAND API3 LPT SSV RPL ARK ARPA ACH C98 DODO HIGH ID ACE
BMT TUT GPS ONG COW EUL""".split()
API = "https://api.binance.com/api/v3/klines"

def frame(rows):
    d = pd.DataFrame(rows, columns=COLS)
    for c in ("open","high","low","close","volume","quote_volume","taker_quote"):
        d[c] = pd.to_numeric(d[c], errors="coerce")
    d["open_time"] = pd.to_datetime(d["open_time"], unit="ms", utc=True)
    d["close_time"] = pd.to_datetime(d["close_time"], unit="ms", utc=True)
    return d.dropna(subset=["open","high","low","close","volume"]).drop_duplicates("open_time").sort_values("open_time").reset_index(drop=True)

def fetch_range(session, symbol, interval, start, end):
    rows, cur, end_ms = [], int(start.timestamp()*1000), int(end.timestamp()*1000)
    while cur < end_ms:
        r = session.get(API, params={"symbol":symbol,"interval":interval,"startTime":cur,"endTime":end_ms-1,"limit":1000}, timeout=30)
        r.raise_for_status(); batch = r.json()
        if not batch: break
        rows.extend(batch)
        nxt = int(batch[-1][6]) + 1
        if nxt <= cur: break
        cur = nxt
        if len(batch) == 1000: time.sleep(.04)
    if not rows: raise RuntimeError(f"{symbol} {interval}: mum yok")
    return frame(rows)

def load_symbol(symbol, start, end):
    s = requests.Session()
    return symbol, {tf: fetch_range(s, symbol, tf, start-pd.Timedelta(days=LOOKBACK[tf]), end) for tf in INTERVALS}

def outcome_bar(position, bar, now):
    """Portfolio Tracker spot kuralı: stop -> TP1 -> peak%2.5 trailing -> 24s expiry."""
    high, low, close = map(float, (bar.high, bar.low, bar.close))
    position["peak"] = max(position["peak"], high)
    if not position["tp1_hit"] and low <= position["stop"]:
        return "loss", position["stop"]
    if not position["tp1_hit"] and high >= position["tp1"]:
        position["tp1_hit"] = True
        position["tp1_time"] = now.isoformat()
    if position["tp1_hit"]:
        trail = max(position["entry"], position["peak"] * .975)
        if close <= trail:
            return "win", trail
    if not position["tp1_hit"] and now >= position["expiry"]:
        return "expired", close
    return None, None

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2026-01-01")
    ap.add_argument("--end", default=pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d"))
    ap.add_argument("--symbols", type=int, default=120)
    ap.add_argument("--step-minutes", type=int, default=15, choices=(15,30,60))
    ap.add_argument("--output", default="")
    args = ap.parse_args()
    start = pd.Timestamp(args.start, tz="UTC")
    end = pd.Timestamp(args.end, tz="UTC") + pd.Timedelta(days=1)
    names = ["BTCUSDT"] + [x+"USDT" for x in BASES[:args.symbols] if x+"USDT" != "BTCUSDT"]
    print(f"[DATA] {len(names)} coin | {start.date()} -> {end.date()} | bu ilk indirme uzun sürebilir")
    data = {}
    with ThreadPoolExecutor(max_workers=5) as pool:
        fs = [pool.submit(load_symbol, n, start, end) for n in names]
        for i, f in enumerate(as_completed(fs), 1):
            try:
                sym, series = f.result(); data[sym] = series
            except Exception as e:
                print(f"[SKIP] {e}")
            if i % 10 == 0 or i == len(fs): print(f"[DATA] {i}/{len(fs)} usable={len(data)}")
    active = [s for s in names if s != "BTCUSDT" and s in data]
    if "BTCUSDT" not in data or not active: raise SystemExit("Yeterli veri indirilemedi.")

    cutoff = start
    prices = {}
    old_ohlcv, old_get = scanner.ohlcv, scanner._get
    def hist_ohlcv(symbol, interval, limit=240):
        d = data[symbol][interval]
        out = d[d.close_time <= cutoff].tail(limit).copy().reset_index(drop=True)
        if len(out) < 80: raise ValueError(f"{symbol} {interval}: yetersiz kapalı mum")
        return out
    def hist_get(path, params=None, attempts=4):
        if path == "/api/v3/ticker/price":
            return {"price":str(prices.get((params or {}).get("symbol"), 0))}
        return old_get(path, params, attempts)
    scanner.ohlcv, scanner._get = hist_ohlcv, hist_get

    def prepare(ts):
        nonlocal cutoff, prices
        cutoff, prices = ts, {}
        for sym in active + ["BTCUSDT"]:
            prices[sym] = float(hist_ohlcv(sym, "15m", 240).close.iloc[-1])

    def prefilter():
        rows = []
        for sym in active:
            try:
                x = scanner._prefilter(sym, 1_000_000_000)
                if x: rows.append(x)
            except Exception: pass
        rows.sort(key=lambda x:x[2], reverse=True)
        selected, seen = rows[:scanner.PREFILTER_CORE_N], {x[0] for x in rows[:scanner.PREFILTER_CORE_N]}
        for row in rows[scanner.PREFILTER_CORE_N:]:
            if len(selected) >= scanner.PYTHON_TOP_N: break
            if row[0] not in seen: selected.append(row); seen.add(row[0])
        return [(s,q,r) for s,q,r,_ in selected]

    watch, positions, trades, daily = {}, {}, [], {}
    cutoffs = pd.date_range(start.ceil("15min"), (end-pd.Timedelta(minutes=15)).floor("15min"),
                            freq=f"{args.step_minutes}min", tz="UTC")
    for n, ts in enumerate(cutoffs, 1):
        # Önce açık işlemler, bu kapanmış 15dk mumla Portfolio gibi güncellenir.
        for sym in list(positions):
            bars = data[sym]["15m"]
            b = bars[bars.close_time <= ts].tail(1)
            if b.empty: continue
            reason, price = outcome_bar(positions[sym], b.iloc[0], ts)
            if reason:
                p = positions.pop(sym)
                pct = round((price/p["entry"]-1)*100, 2)
                trades.append({**p, "symbol":sym, "exit_time":ts.isoformat(), "exit":round(float(price),10),
                               "close_reason":reason, "close_pct":pct, "peak_pct":round((p["peak"]/p["entry"]-1)*100,2)})
        try:
            prepare(ts); regime = scanner.btc_regime(); finals, waits = [], []
            for sym, qv, rank in prefilter():
                try:
                    c = scanner.evaluate(sym, qv, rank, regime, watch.get(sym))
                    if c.decision["decision"] == "ALIM_ADAYI": finals.append(c)
                    elif c.decision["decision"] == "TETIK_BEKLE": waits.append(c)
                except Exception: pass
            for c in waits:
                watch[c.symbol] = {"first_seen":ts.timestamp(), "first_price":c.snapshot["live_price"],
                    "observations":0, "last_bar_15m":int(c.snapshot["15m"]["bar_id"]),
                    "phase":c.decision["state"], "setup_kind":c.decision["setup_kind"], "updated_at":ts.timestamp()}
            key = ts.strftime("%Y-%m-%d"); room = max(0, scanner.MAX_SIGNALS_PER_DAY-daily.get(key,0))
            for c in sorted(finals, key=lambda x:x.rank, reverse=True)[:room]:
                if c.symbol in positions: continue
                lv = scanner.levels(c); entry, stop, tp1 = map(float,(lv["price"],lv["stop"],lv["tp1"]))
                if not (stop < entry < tp1): continue
                daily[key] = daily.get(key,0)+1; watch.pop(c.symbol,None)
                positions[c.symbol] = {"entry_time":ts.isoformat(), "entry":entry, "stop":stop, "tp1":tp1,
                    "peak":entry, "tp1_hit":False, "expiry":ts+pd.Timedelta(hours=24),
                    "setup_kind":c.decision["setup_kind"], "score":round(float(c.decision["confidence"]),2),
                    "rr":round(((tp1/entry-1)/(1-stop/entry)),3), "btc_regime":regime}
        except Exception: pass
        if n % 192 == 0: print(f"[REPLAY] {n}/{len(cutoffs)} | closed={len(trades)} open={len(positions)}")

    # Dönem sonunda hâlâ açık işlemler sonuç sayılmaz; JSON'da ayrı tutulur.
    scanner.ohlcv, scanner._get = old_ohlcv, old_get
    win = [t for t in trades if t["close_reason"] == "win"]
    loss = [t for t in trades if t["close_reason"] == "loss"]
    exp = [t for t in trades if t["close_reason"] == "expired"]
    result = {"strategy":"current scanner replay + Portfolio spot exit rules",
      "period":{"start":start.isoformat(),"end":end.isoformat()},
      "config":{"symbols_current_universe":len(active),"step_minutes":args.step_minutes,
                "tp1":"TP1 sonrası peakten %2.5 trailing","expiry_hours":24},
      "summary":{"closed":len(trades),"win":len(win),"loss":len(loss),"expired":len(exp),
        "win_rate_pct":round(100*len(win)/len(trades),2) if trades else 0,
        "net_pct_sum":round(sum(t["close_pct"] for t in trades),2),
        "win_pct_sum":round(sum(t["close_pct"] for t in win),2),
        "loss_pct_sum":round(sum(t["close_pct"] for t in loss),2),
        "expired_pct_sum":round(sum(t["close_pct"] for t in exp),2)},
      "trades":trades, "open_at_end":[{"symbol":s,**p} for s,p in positions.items()],
      "limits":["Güncel coin evreni kullanılır; 2026 tarihsel evreni birebir yeniden kurulamıyor.",
                "Bu bugünkü scannerin tarihsel replayidir; eski kod sürümlerinin performansı değildir.",
                "Aynı 15dk mumda TP1/stop sırası kesin bilinemez; bu sürüm stop önceliği uygular."]}
    out = Path(args.output or f"research/output/portfolio_style_backtest_{start:%Y%m%d}_{(end-pd.Timedelta(days=1)):%Y%m%d}.json")
    out.parent.mkdir(parents=True, exist_ok=True); out.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(result["summary"],ensure_ascii=False)); print(f"JSON: {out}")

if __name__ == "__main__":
    main()
