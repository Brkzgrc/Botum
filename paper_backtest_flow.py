#!/usr/bin/env python3
"""
FLOW — Para Akışı Pump Sistemi Backtest
Hacim spike (4-5x) + erken fiyat hareketi (+1-8%) + OBV birikim

Kullanım:
  python paper_backtest_flow.py              # Cache varsa kullan
  python paper_backtest_flow.py --no-fetch  # Sadece cache
  python paper_backtest_flow.py --coins ETH SOL ADA

NOT: backtest_data/ klasörü ortak — mevcut cache kullanılır.
"""

import argparse, heapq, json, os, pickle, time
import datetime as _dt
import numpy as np, pandas as pd

# ─── AYARLAR ────────────────────────────────────────────────────────────────
DATA_DIR      = "backtest_data"
START_DATE    = pd.Timestamp("2022-01-01", tz="UTC")
INITIAL_CAP   = 5_000.0
MAX_POSITIONS = 5
MAX_POS_SIZE  = 20_000.0
MIN_VOL_24H   = 5_000_000
BTC_CRASH_PCT = 3.0
CHOCH_SWING   = 5

START_TS = int(_dt.datetime(2021, 1, 1, tzinfo=_dt.timezone.utc).timestamp() * 1000)

IGNORED_COINS = {
    "UP/USDT","DOWN/USDT","BEAR/USDT","BULL/USDT",
    "USDC/USDT","TUSD/USDT","FDUSD/USDT","DAI/USDT","USDP/USDT",
    "USDE/USDT","UST/USDT","USD/USDT","XUSD/USDT","USD1/USDT","BFUSD/USDT",
    "USTC/USDT","BUSD/USDT","FRAX/USDT","LUSD/USDT","GUSD/USDT","SUSD/USDT",
    "USDS/USDT","USDX/USDT","USDD/USDT","CUSD/USDT","OUSD/USDT","MUSD/USDT",
    "RLUSD/USDT","U/USDT",
    "EUR/USDT","TRY/USDT","GBP/USDT","BRL/USDT","RUB/USDT",
    "AUD/USDT","BIDR/USDT","IDRT/USDT","VAI/USDT",
    "PAXG/USDT","XAUT/USDT","WBTC/USDT","WETH/USDT","WBNB/USDT","BETH/USDT",
    "BTCB/USDT","HBTC/USDT",
}
LEVERAGED_PATTERNS = ["UP","DOWN","BULL","BEAR","3L","3S","2L","2S","5L","5S","10L","10S"]

# ─── FLOW PARAMETRELERİ ──────────────────────────────────────────────────────
FLOW_VOL_BASE   = 4.0   # Hacim 48-bar ortalamasının kaç katı (base)
FLOW_VOL_STRICT = 5.0   # Sıkı versiyon
FLOW_RET_MIN    = 1.0   # Mevcut bar minimum +%1 (hareket başladı)
FLOW_RET_MAX    = 8.0   # Mevcut bar maksimum +%8 (henüz geç değil)
FLOW_RET_STRICT = 6.0   # Sıkı: max +%6
FLOW_PUMP72_MAX = 20.0  # Son 72 saatte max +%20 (taze coin)
FLOW_HIGH30_PCT = 10.0  # 30 günlük high'ın %10+ altında olmalı
FLOW_OBV_BARS   = 12    # OBV birikim penceresi (bar)
FLOW_STOP_PCT   = 7.0   # Stop -%7
FLOW_TP1_PCT    = 25.0  # TP1 +%25 → trailing başlar
FLOW_TP2_PCT    = 60.0  # TP2 +%60 → tam çıkış
FLOW_TRAIL_PCT  = 8.0   # Peak'ten -%8 trailing
FLOW_COOLDOWN_H = 12    # Per-coin cooldown (saat)
FLOW_EXPIRE_H   = 168   # Maksimum tutma süresi (7 gün)


# ─── VERİ ───────────────────────────────────────────────────────────────────
def load_pkl(symbol):
    path = os.path.join(DATA_DIR, symbol.replace("/","_") + ".pkl")
    if not os.path.exists(path): return None
    with open(path,"rb") as f: return pickle.load(f)


def fetch_and_save(symbol):
    try:
        import ccxt
        ex = ccxt.binance({"enableRateLimit": True})
        bars = []; since = START_TS
        while True:
            batch = ex.fetch_ohlcv(symbol, "1h", since=since, limit=1000)
            if not batch: break
            bars.extend(batch)
            if len(batch) < 1000: break
            since = batch[-1][0] + 1
            time.sleep(0.2)
        if not bars: return None
        df = pd.DataFrame(bars, columns=["timestamp","open","high","low","close","volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df.set_index("timestamp", inplace=True)
        df = df[~df.index.duplicated(keep="first")]
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(os.path.join(DATA_DIR, symbol.replace("/","_")+".pkl"),"wb") as f:
            pickle.dump(df, f)
        return df
    except Exception as e:
        print(f"    ! {symbol}: {e}"); return None


def load_or_fetch(symbol):
    df = load_pkl(symbol)
    if df is not None: return df
    print(f"    ↓ {symbol} indiriliyor...", end=" ", flush=True)
    df = fetch_and_save(symbol)
    if df is not None: print("✓")
    return df


def get_all_binance_symbols():
    try:
        import ccxt
        ex = ccxt.binance({"enableRateLimit": True})
        ex.load_markets()
        syms = [
            s for s,m in ex.markets.items()
            if s.endswith("/USDT") and m.get("active") and m.get("spot")
            and s not in IGNORED_COINS
            and not any(s.replace("/USDT","").endswith(p) for p in LEVERAGED_PATTERNS)
        ]
        print(f"Binance: {len(syms)} USDT spot sembol")
        return syms
    except Exception as e:
        print(f"Sembol listesi alınamadı: {e}"); return []


def get_cached_symbols():
    if not os.path.isdir(DATA_DIR): return []
    symbols = []
    for fname in os.listdir(DATA_DIR):
        if not fname.endswith(".pkl"): continue
        sym = fname[:-4].replace("_","/",1)
        if not sym.endswith("/USDT") or sym in IGNORED_COINS: continue
        base = sym.split("/")[0]
        if any(p in base for p in LEVERAGED_PATTERNS): continue
        try:
            with open(os.path.join(DATA_DIR,fname),"rb") as f: df = pickle.load(f)
            if df is None or len(df) < 300: continue
        except Exception: continue
        symbols.append(sym)
    if "BTC/USDT" in symbols: symbols.remove("BTC/USDT")
    symbols.insert(0, "BTC/USDT")
    return symbols


# ─── BTC FİLTRELERİ ─────────────────────────────────────────────────────────
def compute_btc_filters(btc_1h):
    df4 = btc_1h.resample("4h", label="right", closed="right").agg(
        {"open":"first","high":"max","low":"min","close":"last","volume":"sum"}
    ).dropna(subset=["close"])
    n4=len(df4); h4=df4["high"].values; l4=df4["low"].values; c4=df4["close"].values
    crash_ok = np.ones(n4, dtype=bool)
    for i in range(1, n4):
        crash_ok[i] = (c4[i]/c4[i-1]-1)*100 > -BTC_CRASH_PCT
    legs4=np.zeros(n4,dtype=int); cur4=0
    for i in range(CHOCH_SWING, n4):
        ph=h4[i-CHOCH_SWING]; pl=l4[i-CHOCH_SWING]
        wh=h4[i-CHOCH_SWING+1:i+1].max(); wl=l4[i-CHOCH_SWING+1:i+1].min()
        if ph>wh: cur4=0
        elif pl<wl: cur4=1
        legs4[i]=cur4
    sh4=None; shx4=True; sl4=None; slx4=True; prev_sl4=None; trend4=0
    downtrend=np.zeros(n4,dtype=bool)
    for i in range(CHOCH_SWING+1, n4):
        if legs4[i]!=legs4[i-1]:
            if legs4[i]==1: prev_sl4=sl4; sl4=l4[i-CHOCH_SWING]; slx4=False
            else: sh4=h4[i-CHOCH_SWING]; shx4=False
        ci,cp=c4[i],c4[i-1]
        if sh4 is not None and not shx4 and ci>sh4 and cp<=sh4: shx4=True; trend4=1
        if sl4 is not None and not slx4 and ci<sl4 and cp>=sl4: slx4=True; trend4=-1
        if trend4==-1 and (prev_sl4 is None or (sl4 is not None and sl4<=prev_sl4)):
            downtrend[i]=True
    df4["crash_ok"]=crash_ok; df4["downtrend"]=downtrend
    idx=btc_1h.index
    crash_s     = df4["crash_ok"].reindex(idx,method="ffill").fillna(True).astype(bool)
    downtrend_s = df4["downtrend"].reindex(idx,method="ffill").fillna(False).astype(bool)
    return pd.DataFrame({"crash_ok":crash_s,"downtrend_ok":~downtrend_s},index=idx)


# ─── ÇIKIŞ FONKSİYONU ────────────────────────────────────────────────────────
def exit_trail_flow(sig, pos_size):
    """TP1 +%25 sonrası peak'ten -%8 trailing, TP2 +%60'ta tam çıkış."""
    entry=sig["entry"]; stop=sig["stop"]; tp1=sig["tp1"]; tp2=sig["tp2"]
    rows=sig["future"]; expire_h=sig.get("expire_h", FLOW_EXPIRE_H)
    sp=(stop-entry)/entry; t2p=(tp2-entry)/entry
    peak=entry; tp1_hit=False
    for ts,row in rows.iloc[:expire_h].iterrows():
        h=float(row["high"]); l=float(row["low"])
        if h>peak: peak=h
        if not tp1_hit:
            if l<=stop: return ts,pos_size*(1+sp),"stop"
            if h>=tp1:  tp1_hit=True
        else:
            trail=peak*(1-FLOW_TRAIL_PCT/100)
            if h>=tp2:  return ts,pos_size*(1+t2p),"tp2"
            if l<=trail:
                tpct=(trail-entry)/entry
                return ts,pos_size*(1+tpct),"trail"
    if len(rows)>0:
        idx=min(expire_h-1,len(rows)-1)
        exp_pct=(float(rows.iloc[idx]["close"])-entry)/entry
        return rows.index[idx],pos_size*(1+exp_pct),"expire"
    return sig["entry_time"],pos_size,"no_data"


EXIT_FNS = {"trail_flow": exit_trail_flow}


# ─── SİNYAL TOPLAMA ─────────────────────────────────────────────────────────
def collect_flow_signals(symbols, btc_filters, fetch=True):
    sigs = {k:[] for k in ["flow","flow_strict","flow_btc","flow_v2"]}

    for sym_i, symbol in enumerate(symbols, 1):
        if symbol == "BTC/USDT": continue
        print(f"  [{sym_i}/{len(symbols)}] {symbol}", flush=True)

        df_raw = load_or_fetch(symbol) if fetch else load_pkl(symbol)
        if df_raw is None or len(df_raw) < 800: continue

        df = df_raw.copy()
        c = df["close"]; v = df["volume"]

        # Hacim: 48-bar ortalama (shift(1) — mevcut barı dahil etme)
        vol_ma48       = v.rolling(48).mean().shift(1)
        df["vol_r48"]  = v / vol_ma48.replace(0, np.nan)

        # 24h USD hacim filtresi
        df["vol_24h_usd"] = (c * v).rolling(24).sum()

        # Mevcut bar dönüşü
        df["ret1"] = (c / c.shift(1) - 1) * 100

        # Son 72h değişim — taze mi?
        df["pump72"] = (c / c.shift(72) - 1) * 100

        # 30 günlük high mesafesi (720 bar)
        high_30d       = c.rolling(720, min_periods=200).max().shift(1)
        df["dist_h30"] = (c / high_30d - 1) * 100  # negatif = high altında

        # OBV trend: son 12 barda net alım baskısı
        direction   = np.where(c > c.shift(1), 1, np.where(c < c.shift(1), -1, 0))
        obv         = pd.Series(direction * v.values, index=c.index).cumsum()
        df["obv_d"] = obv - obv.shift(FLOW_OBV_BARS)  # pozitif = birikim

        df.dropna(subset=["vol_r48","ret1","pump72","dist_h30","obv_d"], inplace=True)
        if len(df) < 500: continue

        btc = btc_filters.reindex(df.index, method="ffill")
        n = len(df)
        last = {k: 0.0 for k in sigs}

        for i in range(750, n-1):
            ts = df.index[i]
            if ts < START_DATE: continue

            vol24 = float(df["vol_24h_usd"].iloc[i])
            if np.isnan(vol24) or vol24 < MIN_VOL_24H: continue

            price = float(df["close"].iloc[i])
            if np.isnan(price) or price <= 0: continue

            ret1     = float(df["ret1"].iloc[i])
            vol_r48  = float(df["vol_r48"].iloc[i])
            pump72   = float(df["pump72"].iloc[i])
            dist_h30 = float(df["dist_h30"].iloc[i])
            obv_ok   = float(df["obv_d"].iloc[i]) > 0

            if any(np.isnan(x) for x in [ret1, vol_r48, pump72, dist_h30]): continue

            crash_ok     = bool(btc["crash_ok"].iloc[i])
            downtrend_ok = bool(btc["downtrend_ok"].iloc[i])
            ts_h         = ts.timestamp() / 3600

            pump72_ok = pump72 <= FLOW_PUMP72_MAX        # Taze coin
            room_ok   = dist_h30 <= -FLOW_HIGH30_PCT     # High'ın altında

            base_ok = (FLOW_RET_MIN <= ret1 <= FLOW_RET_MAX
                       and vol_r48 >= FLOW_VOL_BASE
                       and pump72_ok and room_ok and obv_ok)

            strict_ok = (FLOW_RET_MIN <= ret1 <= FLOW_RET_STRICT
                         and vol_r48 >= FLOW_VOL_STRICT
                         and pump72_ok and room_ok and obv_ok)

            if not base_ok and not strict_ok: continue

            entry = price
            stop  = round(entry * (1 - FLOW_STOP_PCT/100), 10)
            tp1   = round(entry * (1 + FLOW_TP1_PCT/100),  10)
            tp2   = round(entry * (1 + FLOW_TP2_PCT/100),  10)
            base_sig = dict(
                symbol=symbol, entry_time=ts, entry=entry,
                stop=stop, tp1=tp1, tp2=tp2,
                future=df.iloc[i+1:i+1+FLOW_EXPIRE_H][["high","low","close"]].copy(),
                expire_h=FLOW_EXPIRE_H,
                vol_r48=round(vol_r48,2), ret1=round(ret1,2),
                pump72=round(pump72,2), dist_h30=round(dist_h30,2),
            )

            if base_ok:
                if ts_h - last["flow"] >= FLOW_COOLDOWN_H:
                    sigs["flow"].append(base_sig.copy()); last["flow"] = ts_h
                if crash_ok:
                    if ts_h - last["flow_btc"] >= FLOW_COOLDOWN_H:
                        sigs["flow_btc"].append(base_sig.copy()); last["flow_btc"] = ts_h

            if strict_ok:
                if ts_h - last["flow_strict"] >= FLOW_COOLDOWN_H:
                    sigs["flow_strict"].append(base_sig.copy()); last["flow_strict"] = ts_h
                if crash_ok and downtrend_ok:
                    if ts_h - last["flow_v2"] >= FLOW_COOLDOWN_H:
                        sigs["flow_v2"].append(base_sig.copy()); last["flow_v2"] = ts_h

    for k in sigs:
        sigs[k].sort(key=lambda x: x["entry_time"].timestamp())
    return sigs


# ─── PORTFÖY SİMÜLASYONU ────────────────────────────────────────────────────
def simulate_portfolio(signals, exit_fn, initial_cap=INITIAL_CAP, max_positions=MAX_POSITIONS):
    cash=initial_cap; open_count=0; open_positions={}; max_open=0
    trade_log=[]; equity_pts=[(START_DATE, initial_cap)]
    queue=[]; counter=0
    for sig in signals:
        heapq.heappush(queue,(sig["entry_time"].timestamp(),1,counter,"signal",sig))
        counter+=1
    while queue:
        unix_ts,_,_,etype,data = heapq.heappop(queue)
        ts = pd.Timestamp(unix_ts,unit="s",tz="UTC")
        if etype=="signal":
            if open_count>=max_positions: continue
            pos_size=min(cash/max_positions, MAX_POS_SIZE)
            if pos_size<1: continue
            sig=data; cash-=pos_size; open_count+=1
            if open_count>max_open: max_open=open_count
            trade_id=counter; counter+=1
            open_positions[trade_id]=pos_size
            exit_ts,cash_ret,label = exit_fn(sig,pos_size)
            heapq.heappush(queue,(exit_ts.timestamp(),0,counter,"exit",{
                "trade_id":trade_id,"symbol":sig["symbol"],
                "entry_time":sig["entry_time"],"entry":sig["entry"],
                "stop":sig["stop"],"tp1":sig["tp1"],"tp2":sig["tp2"],
                "cash_ret":cash_ret,"label":label,"pos_size":pos_size,
                "stop_pct":round((sig["stop"]-sig["entry"])/sig["entry"]*100,2),
                "tp1_pct":round((sig["tp1"]-sig["entry"])/sig["entry"]*100,2),
                "tp2_pct":round((sig["tp2"]-sig["entry"])/sig["entry"]*100,2),
                "vol_r48":sig.get("vol_r48",0),
                "ret1":sig.get("ret1",0),
            }))
            counter+=1
            trade_log.append({
                "type":"ENTRY","trade_id":trade_id,"symbol":sig["symbol"],
                "time":str(ts)[:16],"entry":round(sig["entry"],6),
                "stop_pct":round((sig["stop"]-sig["entry"])/sig["entry"]*100,2),
                "tp1_pct":round((sig["tp1"]-sig["entry"])/sig["entry"]*100,2),
                "tp2_pct":round((sig["tp2"]-sig["entry"])/sig["entry"]*100,2),
                "vol_r48":round(sig.get("vol_r48",0),2),
                "ret1":round(sig.get("ret1",0),2),
                "size":pos_size,"cash_after":round(cash,2),"open":open_count,
            })
            equity_pts.append((ts,cash+sum(open_positions.values())))
        elif etype=="exit":
            d=data; cash+=d["cash_ret"]; open_count-=1
            open_positions.pop(d["trade_id"],None)
            net_pnl=d["cash_ret"]-d["pos_size"]
            trade_log.append({
                "type":"EXIT","trade_id":d["trade_id"],"symbol":d["symbol"],
                "entry_time":str(d["entry_time"])[:16],"time":str(ts)[:16],
                "label":d["label"],"net_pnl":round(net_pnl,2),
                "net_pct":round(net_pnl/d["pos_size"]*100,2),"cash_ret":round(d["cash_ret"],2),
                "cash_after":round(cash,2),"open":open_count,
                "tp1_pct":d["tp1_pct"],"tp2_pct":d["tp2_pct"],"stop_pct":d["stop_pct"],
                "vol_r48":d.get("vol_r48",0),"ret1":d.get("ret1",0),
            })
            equity_pts.append((ts,cash+sum(open_positions.values())))
    return trade_log, equity_pts, cash, max_open


# ─── İSTATİSTİK ─────────────────────────────────────────────────────────────
def system_stats(trade_log, equity_pts):
    exits   = [t for t in trade_log if t["type"]=="EXIT"]
    wins    = [e for e in exits if e["label"] in ("tp2","tp1","trail")]
    stops   = [e for e in exits if e["label"]=="stop"]
    expires = [e for e in exits if e["label"]=="expire"]
    trails  = [e for e in exits if e["label"]=="trail"]
    tp2s    = [e for e in exits if e["label"]=="tp2"]
    dec     = len(wins)+len(stops)
    wr      = len(wins)/dec*100 if dec>0 else 0.0
    final   = equity_pts[-1][1] if equity_pts else INITIAL_CAP
    ret     = (final-INITIAL_CAP)/INITIAL_CAP*100
    peak    = INITIAL_CAP; max_dd=0.0
    for _,cap in equity_pts:
        if cap>peak: peak=cap
        dd=(cap-peak)/peak*100
        if dd<max_dd: max_dd=dd
    avg_win  = sum(e["net_pct"] for e in wins)/len(wins)   if wins  else 0.0
    avg_loss = sum(e["net_pct"] for e in stops)/len(stops) if stops else 0.0
    stop_early=stop_mid=stop_late=0
    for s in stops:
        try:
            hours=(pd.Timestamp(s["time"])-pd.Timestamp(s["entry_time"])).total_seconds()/3600
            if hours<24:   stop_early+=1
            elif hours<48: stop_mid+=1
            else:          stop_late+=1
        except Exception: pass
    return {
        "trades":len([t for t in trade_log if t["type"]=="ENTRY"]),
        "wins":len(wins),"losses":len(stops),
        "trails":len(trails),"tp2s":len(tp2s),"expires":len(expires),
        "wr":round(wr,1),
        "final":round(final,2),"ret":round(ret,2),"max_dd":round(max_dd,2),
        "avg_win":round(avg_win,2),"avg_loss":round(avg_loss,2),
        "stop_early":stop_early,"stop_mid":stop_mid,"stop_late":stop_late,
    }


# ─── SENARYOLAR ─────────────────────────────────────────────────────────────
SCENARIO_META = [
    ("flow",        "flow",        "trail_flow", "FLOW Base",   f"vol {FLOW_VOL_BASE}x, ret +{FLOW_RET_MIN}-{FLOW_RET_MAX}%"),
    ("flow_strict", "flow_strict", "trail_flow", "FLOW Strict", f"vol {FLOW_VOL_STRICT}x, ret +{FLOW_RET_MIN}-{FLOW_RET_STRICT}%"),
    ("flow_btc",    "flow_btc",    "trail_flow", "FLOW + BTC",  f"vol {FLOW_VOL_BASE}x + BTC no crash"),
    ("flow_v2",     "flow_v2",     "trail_flow", "FLOW V2",     f"vol {FLOW_VOL_STRICT}x + BTC no crash/down"),
]

PALETTE = ["#58a6ff","#3fb950","#f78166","#d2a8ff"]


# ─── HTML ÇIKTI ─────────────────────────────────────────────────────────────
def generate_html(results, n_coins):
    rows=""; datasets=[]
    for idx,(key,_sig,_efn,sys_name,mode) in enumerate(SCENARIO_META):
        r=results.get(key,{}); st=r.get("stats",{})
        trades=st.get("trades",0); wr=st.get("wr",0)
        final=st.get("final",INITIAL_CAP); ret=st.get("ret",0)
        avg_win=st.get("avg_win",0); avg_loss=st.get("avg_loss",0)
        max_dd=st.get("max_dd",0); n_sigs=r.get("n_sigs",0)
        wins=st.get("wins",0); stops_n=st.get("losses",0)
        trails=st.get("trails",0); tp2s=st.get("tp2s",0); expires=st.get("expires",0)
        color_ret="#00c853" if ret>=0 else "#d32f2f"
        rows+=(f'<tr>'
               f'<td>{sys_name}</td><td style="color:#8b949e;font-size:0.8em">{mode}</td>'
               f'<td>{n_sigs}</td><td>{trades}</td>'
               f'<td style="color:#3fb950">{wins}</td>'
               f'<td style="color:#f78166">{stops_n}</td>'
               f'<td style="color:#58a6ff">{trails}</td>'
               f'<td style="color:#d2a8ff">{tp2s}</td>'
               f'<td style="color:#8b949e">{expires}</td>'
               f'<td>{wr:.1f}%</td>'
               f'<td style="color:#3fb950">{avg_win:+.1f}%</td>'
               f'<td style="color:#f78166">{avg_loss:+.1f}%</td>'
               f'<td style="color:{color_ret};font-weight:bold">{ret:+.1f}%</td>'
               f'<td style="color:#f78166">{max_dd:.1f}%</td>'
               f'<td style="color:{color_ret};font-weight:bold">${final:,.0f}</td>'
               f'<td><input type="checkbox" class="tog" data-idx="{idx}" checked></td></tr>')
        eq=r.get("equity",[])
        if eq:
            pts=[{"x":ts.strftime("%Y-%m-%d"),"y":round(v,2)} for ts,v in eq if hasattr(ts,"strftime")]
            color=PALETTE[idx%len(PALETTE)]
            datasets.append(f'{{"label":{json.dumps(f"{sys_name} — {mode}")},"data":{json.dumps(pts)},'
                            f'"borderColor":"{color}","backgroundColor":"{color}20",'
                            f'"borderWidth":2,"pointRadius":0,"fill":false,"tension":0.1}}')
    ds_js="["+",".join(datasets)+"]"
    run_date=_dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    return f"""<!DOCTYPE html><html lang="tr"><head>
<meta charset="UTF-8"><title>FLOW Backtest</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#0d1117;color:#c9d1d9;font-family:-apple-system,monospace;padding:20px}}
h1{{color:#58a6ff;font-size:1.4rem;margin-bottom:6px}}
.meta{{color:#8b949e;font-size:0.8rem;background:#161b22;padding:10px;border-radius:6px;border:1px solid #30363d;margin-bottom:16px}}
.card{{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:14px;margin-bottom:16px;overflow-x:auto}}
table{{width:100%;border-collapse:collapse;font-size:0.82rem;min-width:800px}}
th{{background:#21262d;color:#8b949e;padding:8px 10px;text-align:left;position:sticky;top:0;white-space:nowrap}}
td{{padding:7px 10px;border-bottom:1px solid #21262d;white-space:nowrap}}
tr:hover td{{background:#1c2128}}
canvas{{max-height:480px}}
.ctrl{{display:flex;gap:8px;margin-bottom:10px}}
button{{background:#21262d;border:1px solid #30363d;color:#c9d1d9;padding:5px 12px;border-radius:5px;cursor:pointer;font-size:0.8rem}}
button:hover{{background:#30363d}}
input[type=checkbox]{{cursor:pointer;accent-color:#58a6ff}}
</style></head><body>
<h1>FLOW — Para Akışı Pump Sistemi Backtest</h1>
<div class="meta">{run_date} | {n_coins} coin | 1H Binance | ${INITIAL_CAP:,.0f} başlangıç | Maks {MAX_POSITIONS} pozisyon<br>
Stop -%{FLOW_STOP_PCT:.0f} | TP1 +%{FLOW_TP1_PCT:.0f} → Trailing -%{FLOW_TRAIL_PCT:.0f} peak'ten | TP2 +%{FLOW_TP2_PCT:.0f} tam çıkış | OBV {FLOW_OBV_BARS} bar | Cooldown {FLOW_COOLDOWN_H}h</div>
<div class="card"><table>
<thead><tr>
  <th>Senaryo</th><th>Koşullar</th><th>Sinyal</th><th>İşlem</th>
  <th>Kazanç</th><th>Stop</th><th>Trail</th><th>TP2</th><th>Expire</th>
  <th>WR%</th><th>Avg Win</th><th>Avg Loss</th>
  <th>Getiri%</th><th>Max DD</th><th>Son Sermaye</th><th>Graf.</th>
</tr></thead>
<tbody>{rows}</tbody></table></div>
<div class="card">
<div class="ctrl">
  <button onclick="showAll()">Tümü</button>
  <button onclick="hideAll()">Gizle</button>
</div>
<canvas id="ec"></canvas></div>
<script>
const ds={ds_js};
const ch=new Chart(document.getElementById('ec'),{{type:'line',data:{{datasets:ds}},options:{{
  responsive:true,animation:false,interaction:{{mode:'index',intersect:false}},
  plugins:{{legend:{{position:'bottom',labels:{{color:'#8b949e',boxWidth:12,font:{{size:11}}}}}},
    tooltip:{{backgroundColor:'#1c2128',borderColor:'#30363d',borderWidth:1,
      titleColor:'#c9d1d9',bodyColor:'#c9d1d9',
      callbacks:{{label:c=>` ${{c.dataset.label}}: $${{c.parsed.y.toLocaleString()}}`}}}}}},
  scales:{{
    x:{{type:'category',ticks:{{color:'#8b949e',maxTicksLimit:16,maxRotation:0}},grid:{{color:'#21262d'}}}},
    y:{{ticks:{{color:'#8b949e',callback:v=>'$'+v.toLocaleString()}},grid:{{color:'#21262d'}},
       title:{{display:true,text:'Portföy ($)',color:'#8b949e'}}}}
  }}
}}}});
document.querySelectorAll('.tog').forEach(cb=>cb.addEventListener('change',function(){{
  ch.data.datasets[+this.dataset.idx].hidden=!this.checked;ch.update();
}}));
function showAll(){{ch.data.datasets.forEach(d=>d.hidden=false);document.querySelectorAll('.tog').forEach(c=>c.checked=true);ch.update();}}
function hideAll(){{ch.data.datasets.forEach(d=>d.hidden=true);document.querySelectorAll('.tog').forEach(c=>c.checked=false);ch.update();}}
</script></body></html>"""


# ─── KONSOL RAPORU ──────────────────────────────────────────────────────────
def print_report(results, n_coins):
    W=125
    print("\n"+"═"*W)
    print(f"  FLOW BACKTEST | {n_coins} coin | 2022→bugün | ${INITIAL_CAP:,.0f} başlangıç")
    print(f"  Stop -%{FLOW_STOP_PCT:.0f} | TP1 +%{FLOW_TP1_PCT:.0f} (trail -%{FLOW_TRAIL_PCT:.0f}) | TP2 +%{FLOW_TP2_PCT:.0f}")
    print("═"*W)
    print(f"  {'Senaryo':<16} {'Sinyal':>7} {'İşlem':>6} {'WR':>6} {'AvgWin':>8} {'AvgLoss':>8} {'Trail':>6} {'TP2':>5} {'Exp':>5} {'MaxDD':>7} {'Getiri':>9} {'Son Sermaye':>13}")
    print("─"*W)
    for key,_sig,_efn,sys_name,mode in SCENARIO_META:
        r=results.get(key,{}); st=r.get("stats",{})
        ret=st.get("ret",0); final=st.get("final",INITIAL_CAP)
        print(f"  {sys_name:<16} {r.get('n_sigs',0):>7} {st.get('trades',0):>6} "
              f"{st.get('wr',0):>5.1f}% {st.get('avg_win',0):>+7.1f}% {st.get('avg_loss',0):>+7.1f}% "
              f"{st.get('trails',0):>6} {st.get('tp2s',0):>5} {st.get('expires',0):>5} "
              f"{st.get('max_dd',0):>6.1f}% {ret:>+8.1f}% ${final:>12,.0f}")
        se=st.get("stop_early",0); sm=st.get("stop_mid",0); sl_=st.get("stop_late",0)
        mo=st.get("max_open",0)
        print(f"  {'':16}  Koşul: {mode} | MaxEş: {mo} | Stop <24h:{se} 24-48h:{sm} >48h:{sl_}")
    print("═"*W+"\n")


# ─── ANA FONKSİYON ──────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-fetch", action="store_true")
    ap.add_argument("--coins", nargs="*")
    args = ap.parse_args()

    do_fetch = not args.no_fetch

    btc_raw = load_or_fetch("BTC/USDT") if do_fetch else load_pkl("BTC/USDT")
    if btc_raw is None:
        print("HATA: BTC/USDT verisi yok."); return
    print("BTC 4H filtreleri hesaplanıyor...")
    btc_filters = compute_btc_filters(btc_raw)

    if args.coins:
        symbols = list(args.coins)
        if "BTC/USDT" not in symbols: symbols.insert(0,"BTC/USDT")
    elif args.no_fetch:
        symbols = get_cached_symbols()
    else:
        print("Binance spot listesi alınıyor...")
        symbols = get_all_binance_symbols() or get_cached_symbols()

    if not symbols:
        print("Geçerli coin bulunamadı."); return

    print(f"\n{len(symbols)} coin | ${INITIAL_CAP:,.0f} sermaye | maks {MAX_POSITIONS} pozisyon")
    print(f"FLOW sinyalleri toplanıyor ({START_DATE.date()} → bugün)...\n")
    print(f"  Koşullar: vol {FLOW_VOL_BASE}x-{FLOW_VOL_STRICT}x (48h MA) | ret +{FLOW_RET_MIN}-{FLOW_RET_MAX}%")
    print(f"  Filtreler: pump72 <{FLOW_PUMP72_MAX}% | 30d_high -%{FLOW_HIGH30_PCT}+ altı | OBV {FLOW_OBV_BARS}bar pozitif\n")

    sigs = collect_flow_signals(symbols, btc_filters, fetch=do_fetch)
    for k,v in sigs.items():
        print(f"  {k}: {len(v)} sinyal")

    print("\nPortföy simülasyonları çalışıyor...")
    results = {}
    for key, sig_sys, exit_fn_key, sys_name, mode in SCENARIO_META:
        sig_list = sigs[sig_sys]
        fn       = EXIT_FNS[exit_fn_key]
        print(f"  [{sys_name}] {len(sig_list)} sinyal...", end=" ", flush=True)
        log, eq, final_cash, max_open = simulate_portfolio(sig_list, fn)
        st = system_stats(log, eq)
        st["max_open"] = max_open
        results[key] = {"log":log,"equity":eq,"final":final_cash,"stats":st,"n_sigs":len(sig_list)}
        print(f"WR:{st['wr']}% | {st['ret']:+.1f}% | ${st['final']:,.0f}")

    print_report(results, len(symbols))

    html = generate_html(results, len(symbols))
    with open("backtest_results_flow.html","w",encoding="utf-8") as f: f.write(html)
    print("✓ backtest_results_flow.html")

    summary = {k:{"n_sigs":v["n_sigs"],"stats":v["stats"]} for k,v in results.items()}
    with open("backtest_results_flow.json","w",encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
    print("✓ backtest_results_flow.json\n")


if __name__ == "__main__":
    main()
