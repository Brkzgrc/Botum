#!/usr/bin/env python3
"""PUMP PROBABILITY Backtest — 2026 (Jan–May)"""
import time, sys
import numpy as np
import pandas as pd
import ccxt
from datetime import datetime, timezone
from collections import defaultdict

# ── Config ──
WARMUP_BARS  = 350
MAX_HOLD     = 72
COOLDOWN_H   = 72
TOP_N        = 50
START_DT     = datetime(2026, 1, 1, tzinfo=timezone.utc)
END_DT       = datetime(2026, 5, 31, 0, 0, tzinfo=timezone.utc)
START_TS     = int(START_DT.timestamp() * 1000)
END_TS       = int(END_DT.timestamp() * 1000)

TP1_F = 1.08
TP2_F = 1.15
TP3_F = 1.25

IGNORED = {
    'UP/USDT','DOWN/USDT','BEAR/USDT','BULL/USDT','USDC/USDT','TUSD/USDT',
    'FDUSD/USDT','DAI/USDT','USDP/USDT','USDE/USDT','UST/USDT','USD/USDT',
    'XUSD/USDT','USD1/USDT','BFUSD/USDT','USTC/USDT','BUSD/USDT','FRAX/USDT',
    'LUSD/USDT','GUSD/USDT','SUSD/USDT','USDS/USDT','USDX/USDT','USDD/USDT',
    'CUSD/USDT','OUSD/USDT','MUSD/USDT','U/USDT','EUR/USDT','TRY/USDT',
    'GBP/USDT','BRL/USDT','RUB/USDT','AUD/USDT','BIDR/USDT','IDRT/USDT',
    'VAI/USDT','PAXG/USDT','WBTC/USDT','WETH/USDT','WBNB/USDT','BETH/USDT',
    'BTCB/USDT','HBTC/USDT',
}

exchange = ccxt.binance({"enableRateLimit": True, "verify": False})
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ── Veri çekme ──
def fetch_1h(symbol):
    since = START_TS - WARMUP_BARS * 3_600_000
    bars  = []
    while since < END_TS:
        try:
            chunk = exchange.fetch_ohlcv(symbol, "1h", since=since, limit=1000)
        except Exception:
            break
        if not chunk:
            break
        bars.extend(chunk)
        since = chunk[-1][0] + 3_600_000
        if len(chunk) < 1000:
            break
        time.sleep(0.08)
    if not bars:
        return None
    df = pd.DataFrame(bars, columns=["ts","open","high","low","close","volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df = df[df["ts"] < pd.Timestamp(END_DT)].drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
    return df if len(df) >= WARMUP_BARS + 100 else None

# ── İndikatörler (bot.py ile aynı) ──
def prepare(df):
    df = df.copy()
    c, h, l, v = df["close"], df["high"], df["low"], df["volume"]
    df["vol_ma"]   = v.rolling(20).mean()
    bb_mid = c.rolling(20).mean()
    bb_std = c.rolling(20).std()
    bb_w   = (bb_mid + 2*bb_std - (bb_mid - 2*bb_std)) / bb_mid.replace(0, np.nan)
    df["bb_width"] = bb_w
    return df

def calc_adx_di(df, period=7):
    if len(df) < period * 4:
        return None, None, None, None
    h = df["high"].values.astype(float)
    l = df["low"].values.astype(float)
    c = df["close"].values.astype(float)
    n = len(c)
    tr = np.zeros(n); dmp = np.zeros(n); dmm = np.zeros(n)
    for i in range(1, n):
        tr[i]  = max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
        up   = h[i]-h[i-1]; dn = l[i-1]-l[i]
        dmp[i] = up  if (up > dn  and up > 0)  else 0.0
        dmm[i] = dn  if (dn > up  and dn > 0)  else 0.0
    def wilder(a, p):
        s = np.zeros(len(a)); s[p] = np.sum(a[1:p+1])
        for i in range(p+1, len(a)): s[i] = s[i-1] - s[i-1]/p + a[i]
        return s
    atr_s = wilder(tr, period); dmp_s = wilder(dmp, period); dmm_s = wilder(dmm, period)
    with np.errstate(divide="ignore", invalid="ignore"):
        dip = np.where(atr_s>0, dmp_s/atr_s*100, 0.)
        dim = np.where(atr_s>0, dmm_s/atr_s*100, 0.)
        dx  = np.where((dip+dim)>0, np.abs(dip-dim)/(dip+dim)*100, 0.)
    adx = wilder(dx, period)
    prev = float(adx[-4]) if len(adx) >= 4 else float(adx[-1])
    return float(adx[-1]), prev, float(dip[-1]), float(dim[-1])

def calc_obv_trend(df, period=20):
    if len(df) < period + 5: return False
    c = df["close"].values.astype(float)
    v = df["volume"].values.astype(float)
    obv = np.zeros(len(c))
    for i in range(1, len(c)):
        obv[i] = obv[i-1] + (v[i] if c[i] > c[i-1] else (-v[i] if c[i] < c[i-1] else 0))
    s   = pd.Series(obv)
    ema = s.ewm(span=period, adjust=False).mean()
    return bool(obv[-1] > float(ema.iloc[-1]) and float(ema.diff().iloc[-1]) > 0)

def calc_sr(df, n_pivot=5):
    if len(df) < n_pivot*2+5: return []
    highs  = df["high"].values
    cur    = float(df["close"].iloc[-1])
    pivots = [float(highs[i]) for i in range(n_pivot, len(df)-n_pivot)
              if highs[i] == max(highs[i-n_pivot:i+n_pivot+1])]
    if not pivots: return []
    pivots = sorted(pivots)
    grps = [[pivots[0]]]
    for v in pivots[1:]:
        if (v - grps[-1][-1]) / max(grps[-1][-1], 1e-12) < 0.015:
            grps[-1].append(v)
        else:
            grps.append([v])
    return sorted([sum(g)/len(g) for g in grps if sum(g)/len(g) > cur * 1.001])[:3]

# ── BTC 4H trend (basit EMA21 kontrolü) ──
def build_btc_trend(btc_df):
    """Her timestamp için BTC downtrend mi → bool Series"""
    c  = btc_df["close"]
    e21 = c.ewm(span=21, adjust=False).mean()
    # Downtrend: close < ema21 ve önceki 2 bar da < ema21
    below = c < e21
    downtrend = below & below.shift(1).fillna(False) & below.shift(2).fillna(False)
    return downtrend  # index: timestamp (4H)

def get_btc_trend_at(ts, btc_4h_downtrend):
    """ts anında BTC downtrend mi?"""
    try:
        idx = btc_4h_downtrend.index.asof(ts)
        if pd.isna(idx): return False
        return bool(btc_4h_downtrend[idx])
    except Exception:
        return False

# ── Sinyal kontrolü ──
def check_signal(sub):
    """sub: prepared DataFrame, son bar'da sinyal var mı?"""
    if len(sub) < 60: return False
    bb_w = sub["bb_width"].dropna()
    if len(bb_w) < 50: return False
    bb_cur = float(bb_w.iloc[-1])
    squeeze_thr = float(bb_w.rolling(50).quantile(0.25).iloc[-1])
    if pd.isna(squeeze_thr) or bb_cur > squeeze_thr: return False

    adx, adx_prev, dip, dim = calc_adx_di(sub)
    if adx is None: return False
    if adx < 15: return False
    if dip <= dim: return False
    if adx <= adx_prev: return False

    if not calc_obv_trend(sub): return False

    res = calc_sr(sub)
    if not res: return False

    bar    = sub.iloc[-1]
    close  = float(bar["close"])
    vol_now = float(bar["volume"])
    vol_ma  = float(bar["vol_ma"]) if not pd.isna(bar.get("vol_ma", np.nan)) else None

    if close <= res[0]: return False
    if vol_ma is None or vol_now < vol_ma * 1.5: return False

    return True

# ── Outcome simülasyonu ──
def simulate(df, sig_idx, entry, stop):
    tp1 = entry * TP1_F
    tp2 = entry * TP2_F
    tp3 = entry * TP3_F
    for j in range(sig_idx+1, min(sig_idx+MAX_HOLD+1, len(df))):
        hi = float(df.at[j, "high"])
        lo = float(df.at[j, "low"])
        if lo <= stop:
            return "STOP", round((stop-entry)/entry*100, 2), j-sig_idx
        if hi >= tp3: return "TP3", round((tp3-entry)/entry*100, 2), j-sig_idx
        if hi >= tp2: return "TP2", round((tp2-entry)/entry*100, 2), j-sig_idx
        if hi >= tp1: return "TP1", round((tp1-entry)/entry*100, 2), j-sig_idx
    last = float(df.at[min(sig_idx+MAX_HOLD, len(df)-1), "close"])
    return "EXPIRED", round((last-entry)/entry*100, 2), MAX_HOLD

# ── Ana backtest ──
def run_backtest(symbols, btc_4h_downtrend):
    results_all      = []  # filtresiz
    results_filtered = []  # btc filtreli

    for si, sym in enumerate(symbols):
        print(f"  [{si+1}/{len(symbols)}] {sym} ...", end=" ", flush=True)
        df = fetch_1h(sym)
        if df is None:
            print("veri yok")
            continue

        df_prep = prepare(df)
        last_sig_bar = {}  # cooldown

        sig_count = 0
        for i in range(WARMUP_BARS, len(df_prep)):
            # Sadece START_DT sonrası sinyaller say
            if df_prep.at[i, "ts"] < pd.Timestamp(START_DT):
                continue

            # Cooldown
            last = last_sig_bar.get(sym, -9999)
            if i - last < COOLDOWN_H:
                continue

            sub = df_prep.iloc[:i+1]
            if not check_signal(sub):
                continue

            bar   = df_prep.iloc[i]
            close = float(bar["close"])
            low   = float(bar["low"])
            stop  = max(low * 0.995, close * 0.95)
            ts    = bar["ts"]

            outcome, pct, bars_held = simulate(df_prep, i, close, stop)
            last_sig_bar[sym] = i
            sig_count += 1

            rec = {
                "symbol":     sym,
                "ts":         ts,
                "entry":      close,
                "outcome":    outcome,
                "pct":        pct,
                "bars_held":  bars_held,
            }
            results_all.append(rec)

            # BTC filtreli: btc downtrend'de sinyal verme
            btc_down = get_btc_trend_at(ts, btc_4h_downtrend)
            if not btc_down:
                results_filtered.append(rec)

        print(f"{sig_count} sinyal")

    return results_all, results_filtered

def summarize(results, label):
    if not results:
        print(f"\n{label}: Sinyal yok")
        return
    total = len(results)
    tp1   = sum(1 for r in results if r["outcome"] == "TP1")
    tp2   = sum(1 for r in results if r["outcome"] == "TP2")
    tp3   = sum(1 for r in results if r["outcome"] == "TP3")
    stop  = sum(1 for r in results if r["outcome"] == "STOP")
    exp   = sum(1 for r in results if r["outcome"] == "EXPIRED")
    wins  = tp1 + tp2 + tp3
    wr    = wins / total * 100
    avg_pct   = sum(r["pct"] for r in results) / total
    avg_held  = sum(r["bars_held"] for r in results) / total

    # Aylık dağılım
    monthly = defaultdict(lambda: {"total": 0, "wins": 0})
    for r in results:
        m = r["ts"].strftime("%Y-%m")
        monthly[m]["total"] += 1
        if r["outcome"] in ("TP1","TP2","TP3"):
            monthly[m]["wins"] += 1

    # En çok sinyal veren coinler
    coin_counts = defaultdict(int)
    for r in results: coin_counts[r["symbol"]] += 1
    top_coins = sorted(coin_counts.items(), key=lambda x: -x[1])[:5]

    print(f"""
{'='*50}
{label}
{'='*50}
Toplam sinyal   : {total}
─────────────────────────────────────
TP1 (+%8)       : {tp1:3d}  (%{tp1/total*100:.1f})
TP2 (+%15)      : {tp2:3d}  (%{tp2/total*100:.1f})
TP3 (+%25)      : {tp3:3d}  (%{tp3/total*100:.1f})
Stop (-%5)      : {stop:3d}  (%{stop/total*100:.1f})
Expired         : {exp:3d}  (%{exp/total*100:.1f})
─────────────────────────────────────
Win Rate (TP hit): %{wr:.1f}
Ort. getiri      : %{avg_pct:+.2f}
Ort. süre        : {avg_held:.0f}h
─────────────────────────────────────
Aylık dağılım:""")
    for m in sorted(monthly):
        d = monthly[m]
        mwr = d["wins"]/d["total"]*100 if d["total"] else 0
        print(f"  {m}: {d['total']:3d} sinyal | {d['wins']:3d} win | WR %{mwr:.0f}")
    print(f"""─────────────────────────────────────
En çok sinyal veren coinler:""")
    for coin, cnt in top_coins:
        c_res = [r for r in results if r["symbol"] == coin]
        c_wins = sum(1 for r in c_res if r["outcome"] in ("TP1","TP2","TP3"))
        print(f"  {coin.replace('/USDT',''):8s}: {cnt} sinyal | WR %{c_wins/cnt*100:.0f}")

# ── Main ──
if __name__ == "__main__":
    print("PUMP PROBABILITY BACKTEST — 2026 (Jan–May)")
    print("Coin evreni çekiliyor...")

    # Top coins
    markets = exchange.load_markets()
    tickers = exchange.fetch_tickers(
        [s for s in markets if s.endswith("/USDT") and s not in IGNORED
         and markets[s].get("spot", True)]
    )
    candidates = sorted(
        [(s, float(t.get("quoteVolume") or 0)) for s, t in tickers.items()
         if float(t.get("quoteVolume") or 0) >= 5_000_000],
        key=lambda x: -x[1]
    )[:TOP_N]
    symbols = [s for s, _ in candidates]
    print(f"{len(symbols)} coin taranacak")

    # BTC 4H veri çek (trend için)
    print("BTC 4H veri çekiliyor...")
    btc_bars = []
    since = START_TS - 350 * 4 * 3_600_000
    while since < END_TS:
        try:
            chunk = exchange.fetch_ohlcv("BTC/USDT", "4h", since=since, limit=1000)
        except Exception:
            break
        if not chunk: break
        btc_bars.extend(chunk)
        since = chunk[-1][0] + 4*3_600_000
        if len(chunk) < 1000: break
        time.sleep(0.1)
    btc_df = pd.DataFrame(btc_bars, columns=["ts","open","high","low","close","volume"])
    btc_df["ts"] = pd.to_datetime(btc_df["ts"], unit="ms", utc=True)
    btc_df = btc_df.drop_duplicates("ts").sort_values("ts").set_index("ts")
    btc_4h_downtrend = build_btc_trend(btc_df)
    print(f"BTC 4H hazır: {len(btc_df)} bar")

    print(f"\nBacktest başlıyor ({len(symbols)} coin, Jan–May 2026)...")
    t0 = time.time()
    all_res, filt_res = run_backtest(symbols, btc_4h_downtrend)
    elapsed = time.time() - t0

    summarize(all_res,  "FİLTRESİZ (BTC trend koşulu yok)")
    summarize(filt_res, "FİLTRELİ  (BTC downtrend'de sinyal yok)")

    btc_blocked = len(all_res) - len(filt_res)
    print(f"\nBTC filtresi {btc_blocked} sinyali engelledi (%{btc_blocked/len(all_res)*100:.1f})")
    print(f"Süre: {elapsed:.0f}s")
