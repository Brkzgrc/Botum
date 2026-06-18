#!/usr/bin/env python3
"""
Eski CHoCH Backtest — 6 Senaryo
SMC.py birebir metodoloji (artımlı CHoCH, BTC 4H filtreleri)
2020-01-01'den bugüne, 1H OHLCV, Binance

Kullanım:
  python backtest.py                   # Tüm Binance USDT coinleri
  python backtest.py --no-fetch        # Cache kullan, tekrar indirme
  python backtest.py --coins BTC ETH   # Belirli coinler (otomatik /USDT eklenir)
  python backtest.py --n 50            # İlk N coin test et
"""

import argparse, json, os, pickle, time
from datetime import datetime, timezone
import ccxt, numpy as np, pandas as pd

# ═══════════════════════════════════════════════════════════════════════
# AYARLAR — SMC.py ile birebir
# ═══════════════════════════════════════════════════════════════════════
START_TS      = int(datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
DATA_DIR      = "backtest_data"

CHOCH_SWING   = 5           # SMC.py: CHOCH_SWING = 5
COOLDOWN_H    = 24          # SMC.py: PHASE2_COOLDOWN = 86400 sn = 24 saat
EXPIRE_H      = 168         # 7 gün pozisyon süresi
MIN_VOL_24H   = 5_000_000   # SMC.py: MIN_VOLUME_24H = 5_000_000
BTC_CRASH_PCT = 3.0         # SMC.py: BTC_CRASH_PCT = 3.0

IGNORED_COINS = {
    'UP/USDT','DOWN/USDT','BEAR/USDT','BULL/USDT',
    'USDC/USDT','TUSD/USDT','FDUSD/USDT','DAI/USDT','USDP/USDT',
    'USDE/USDT','UST/USDT','USD/USDT','XUSD/USDT','USD1/USDT','BFUSD/USDT',
    'USTC/USDT','BUSD/USDT','FRAX/USDT','LUSD/USDT','GUSD/USDT','SUSD/USDT',
    'USDS/USDT','USDX/USDT','USDD/USDT','CUSD/USDT','OUSD/USDT','MUSD/USDT',
    'RLUSD/USDT','U/USDT',
    'EUR/USDT','TRY/USDT','GBP/USDT','BRL/USDT','RUB/USDT',
    'AUD/USDT','BIDR/USDT','IDRT/USDT','VAI/USDT',
    'PAXG/USDT','XAUT/USDT','WBTC/USDT','WETH/USDT','WBNB/USDT','BETH/USDT',
    'BTCB/USDT','HBTC/USDT',
}
LEVERAGED_PATTERNS = ["UP","DOWN","BULL","BEAR","3L","3S","2L","2S","5L","5S","10L","10S"]


# ═══════════════════════════════════════════════════════════════════════
# VERİ ÇEKME
# ═══════════════════════════════════════════════════════════════════════
def get_top_coins(n=50):
    """CoinGecko market cap sıralamasına göre top N coin (Binance'ta işlem gören)"""
    import urllib.request
    url = ("https://api.coingecko.com/api/v3/coins/markets"
           "?vs_currency=usd&order=market_cap_desc&per_page=250&page=1&sparkline=false")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = __import__("json").loads(r.read())

    ex = ccxt.binance({"enableRateLimit": True})
    ex.load_markets()
    binance_pairs = set(ex.markets.keys())

    result = []
    for coin in data:
        sym = coin["symbol"].upper() + "/USDT"
        if sym in IGNORED_COINS: continue
        base = coin["symbol"].upper()
        if any(p in base for p in LEVERAGED_PATTERNS): continue
        if sym not in binance_pairs: continue
        result.append(sym)
        if len(result) >= n: break

    if "BTC/USDT" not in result:
        result.insert(0, "BTC/USDT")
    print(f"CoinGecko top {n} (Binance'ta): {len(result)} coin seçildi")
    return result


def fetch_ohlcv_full(symbol):
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
    return df[~df.index.duplicated(keep="first")]


def cache_path(symbol):
    os.makedirs(DATA_DIR, exist_ok=True)
    return os.path.join(DATA_DIR, symbol.replace("/","_") + ".pkl")


def load_or_fetch(symbol, force=False):
    p = cache_path(symbol)
    if not force and os.path.exists(p):
        with open(p,"rb") as f: return pickle.load(f)
    df = fetch_ohlcv_full(symbol)
    if df is not None:
        with open(p,"wb") as f: pickle.dump(df, f)
    return df


# ═══════════════════════════════════════════════════════════════════════
# CHoCH TESPİTİ — SMC.py detect_micro_choch ARTIMLI (O(N) toplam)
#
# SMC.py: detect_micro_choch(df_2500bar, CHOCH_SWING=5) her bar kapanışında
# çağrılır. Artımlı versiyon bunun eşdeğeri: tüm tarihe tek geçişte uygulanır,
# sonuç her bar için üretilir. 2500-bar sınırı yalnızca pratik RAM kısıtıdır;
# artımlı hesaplama istatistiksel olarak eşdeğerdir (eski swing seviyeleri
# aşılmış olarak işaretlenir, durumu etkilemez).
# ═══════════════════════════════════════════════════════════════════════
def run_choch_incremental(df, choch_swing=CHOCH_SWING):
    """
    Tüm bar dizisi için artımlı CHoCH tespiti.
    Bar i sonucu = detect_micro_choch(df[:i+1]) eşdeğeri.

    Döner: (break_types, break_dirs, choch_levels, swing_low_levels)
      break_types[i]      = "CHoCH" / "BOS" / None
      break_dirs[i]       = "BULLISH" / "BEARISH" / None
      choch_levels[i]     = kırılan swing seviyesi  (SMC.py: eski_choch_level → giriş)
      swing_low_levels[i] = en son swing dip        (SMC.py: eski_swing_low  → stop)
    """
    h = df["high"].values
    l = df["low"].values
    c = df["close"].values
    n = len(df)

    # 1. Leg hesaplama — birebir SMC.py detect_micro_choch ilk döngüsü
    legs = np.zeros(n, dtype=int)
    cur = 0
    for i in range(choch_swing, n):
        ph = h[i - choch_swing]
        pl = l[i - choch_swing]
        wh = h[i - choch_swing + 1:i + 1].max()
        wl = l[i - choch_swing + 1:i + 1].min()
        if ph > wh: cur = 0
        elif pl < wl: cur = 1
        legs[i] = cur

    # 2. Durum makinesi — SMC.py ikinci döngüsü, artımlı
    sh = None; shx = True   # swing_high_level, swing_high_crossed
    sl = None; slx = True   # swing_low_level,  swing_low_crossed
    trend = 0

    bts  = [None] * n
    bds  = [None] * n
    cls_ = [None] * n
    swls = [None] * n

    for i in range(choch_swing + 1, n):
        # Leg değişiminde swing seviyesi güncelle (SMC.py birebir)
        if legs[i] != legs[i - 1]:
            if legs[i] == 1:
                sl = l[i - choch_swing]; slx = False
            else:
                sh = h[i - choch_swing]; shx = False

        ci, cp = c[i], c[i - 1]
        bt = None; bd = None; cl = None

        # BULLISH kırılım — SMC.py i==n-1 bloğu eşdeğeri
        if sh is not None and not shx and ci > sh and cp <= sh:
            bt = "CHoCH" if trend == -1 else "BOS"
            bd = "BULLISH"; cl = sh
            shx = True; trend = 1

        # BEARISH kırılım
        if sl is not None and not slx and ci < sl and cp >= sl:
            bt = "CHoCH" if trend == 1 else "BOS"
            bd = "BEARISH"; cl = sl
            slx = True; trend = -1

        bts[i]  = bt
        bds[i]  = bd
        cls_[i] = cl
        swls[i] = sl   # mevcut swing_low_level (stop için)

    return bts, bds, cls_, swls


# ═══════════════════════════════════════════════════════════════════════
# BTC FİLTRELERİ — SMC.py check_btc_crash + check_btc_downtrend_active
# ═══════════════════════════════════════════════════════════════════════
def compute_btc_filters(btc_1h):
    """
    BTC 1H → 4H resample. SMC.py'deki iki filtreyi hesapla:
      crash_ok:     4H son bar değişimi > -3%  (check_btc_crash birebir)
      downtrend_ok: swing yapısı bearish değil  (check_btc_downtrend_active)

    Returns: pd.DataFrame(index=btc_1h.index)
    """
    df4 = btc_1h.resample("4h", label="right", closed="right").agg(
        {"open":"first","high":"max","low":"min","close":"last","volume":"sum"}
    ).dropna(subset=["close"])

    n4 = len(df4)
    h4 = df4["high"].values
    l4 = df4["low"].values
    c4 = df4["close"].values

    # Crash filtresi
    crash_ok = np.ones(n4, dtype=bool)
    for i in range(1, n4):
        crash_ok[i] = (c4[i] / c4[i-1] - 1) * 100 > -BTC_CRASH_PCT

    # Downtrend filtresi — SMC.py _swing_lows_trend artımlı
    legs4 = np.zeros(n4, dtype=int)
    cur4 = 0
    for i in range(CHOCH_SWING, n4):
        ph = h4[i - CHOCH_SWING]; pl = l4[i - CHOCH_SWING]
        wh = h4[i - CHOCH_SWING + 1:i + 1].max()
        wl = l4[i - CHOCH_SWING + 1:i + 1].min()
        if ph > wh: cur4 = 0
        elif pl < wl: cur4 = 1
        legs4[i] = cur4

    sh4 = None; shx4 = True
    sl4 = None; slx4 = True
    prev_sl4 = None
    trend4 = 0
    downtrend_active = np.zeros(n4, dtype=bool)

    for i in range(CHOCH_SWING + 1, n4):
        if legs4[i] != legs4[i - 1]:
            if legs4[i] == 1:
                prev_sl4 = sl4
                sl4 = l4[i - CHOCH_SWING]; slx4 = False
            else:
                sh4 = h4[i - CHOCH_SWING]; shx4 = False

        ci, cp = c4[i], c4[i - 1]
        if sh4 is not None and not shx4 and ci > sh4 and cp <= sh4:
            shx4 = True; trend4 = 1
        if sl4 is not None and not slx4 and ci < sl4 and cp >= sl4:
            slx4 = True; trend4 = -1

        # SMC.py: trend==-1 AND (prev_low is None OR last_low <= prev_low) → aktif düşüş
        if trend4 == -1:
            if prev_sl4 is None or (sl4 is not None and sl4 <= prev_sl4):
                downtrend_active[i] = True

    df4["crash_ok"]         = crash_ok
    df4["downtrend_active"] = downtrend_active

    idx = btc_1h.index
    crash_s     = df4["crash_ok"].reindex(idx, method="ffill").fillna(True).astype(bool)
    downtrend_s = df4["downtrend_active"].reindex(idx, method="ffill").fillna(False).astype(bool)

    return pd.DataFrame({
        "crash_ok":     crash_s,
        "downtrend_ok": ~downtrend_s,
    }, index=idx)


# ═══════════════════════════════════════════════════════════════════════
# SİMÜLASYON — numpy dizileri üzerinde bar-by-bar
# Stop kontrolü TP'den önce (muhafazakâr)
# ═══════════════════════════════════════════════════════════════════════
def sim_half(bh, bl, bc, entry, stop, tp1, tp2, expire_h):
    """½TP1 + ½TP2 çıkış (doğru implementasyon):
       - TP1 öncesi stop → tümü stop (loss)
       - TP1 sonrası stop → (tp1_pct + stop_pct) / 2 (win_partial)
       - TP2 → (tp1_pct + tp2_pct) / 2 (win)
       - Expire → son fiyat ile avg"""
    bh = bh[:expire_h]; bl = bl[:expire_h]; bc = bc[:expire_h]
    if len(bh) == 0: return 0.0, "expired"
    tp1_p  = (tp1 - entry) / entry * 100
    tp2_p  = (tp2 - entry) / entry * 100
    stop_p = (stop - entry) / entry * 100
    tp1_hit = False
    for i in range(len(bh)):
        if not tp1_hit:
            if bl[i] <= stop: return stop_p, "loss"
            if bh[i] >= tp2:  return (tp1_p + tp2_p) / 2, "win"
            if bh[i] >= tp1:  tp1_hit = True
        else:
            if bl[i] <= stop: return (tp1_p + stop_p) / 2, "win_partial"
            if bh[i] >= tp2:  return (tp1_p + tp2_p) / 2, "win"
    last_p = (bc[-1] - entry) / entry * 100 if len(bc) > 0 else 0.0
    if tp1_hit: return (tp1_p + last_p) / 2, "expired"
    return last_p, "expired"


def sim_tp1(bh, bl, bc, entry, stop, tp1, expire_h):
    """Tam TP1: tüm pozisyon TP1'de kapanır."""
    bh = bh[:expire_h]; bl = bl[:expire_h]; bc = bc[:expire_h]
    if len(bh) == 0: return 0.0, "expired"
    tp1_p  = (tp1 - entry) / entry * 100
    stop_p = (stop - entry) / entry * 100
    for i in range(len(bh)):
        if bl[i] <= stop: return stop_p, "loss"
        if bh[i] >= tp1:  return tp1_p, "win"
    return (bc[-1] - entry) / entry * 100 if len(bc) > 0 else 0.0, "expired"


def sim_tp2(bh, bl, bc, entry, stop, tp2, expire_h):
    """Tam TP2: TP1 yok sayılır, tümü TP2'de kapanır."""
    bh = bh[:expire_h]; bl = bl[:expire_h]; bc = bc[:expire_h]
    if len(bh) == 0: return 0.0, "expired"
    tp2_p  = (tp2 - entry) / entry * 100
    stop_p = (stop - entry) / entry * 100
    for i in range(len(bh)):
        if bl[i] <= stop: return stop_p, "loss"
        if bh[i] >= tp2:  return tp2_p, "win"
    return (bc[-1] - entry) / entry * 100 if len(bc) > 0 else 0.0, "expired"


# ═══════════════════════════════════════════════════════════════════════
# SONUÇ YAPISI
# ═══════════════════════════════════════════════════════════════════════
def empty_stats():
    return {"wins":0,"losses":0,"partial":0,"expired":0,"total":0,"pnl":0.0}


def record(stats, pnl, outcome):
    stats["total"] += 1
    stats["pnl"]   += pnl
    if outcome == "win":           stats["wins"]   += 1
    elif outcome == "win_partial": stats["partial"] += 1
    elif outcome == "loss":        stats["losses"] += 1
    else:                          stats["expired"] += 1


def finalize(stats):
    t = stats["total"]
    if t == 0:
        stats["wr"] = 0.0; stats["avg_pnl"] = 0.0
        return stats
    wins = stats["wins"] + stats["partial"]
    dec  = wins + stats["losses"]
    stats["wr"]      = round(wins / dec * 100, 1) if dec > 0 else 0.0
    stats["avg_pnl"] = round(stats["pnl"] / t, 4)
    stats["pnl"]     = round(stats["pnl"], 2)
    return stats


# ═══════════════════════════════════════════════════════════════════════
# ANA BACKTEST
# ═══════════════════════════════════════════════════════════════════════
def run(symbols, btc_df):
    print("BTC 4H filtreleri hesaplanıyor...", flush=True)
    btc_f = compute_btc_filters(btc_df)

    S = {
        "s1_base_half": empty_stats(),  # 1. Baseline ½TP1+½TP2
        "s2_base_tp1":  empty_stats(),  # 2. Baseline Tam TP1
        "s3_base_tp2":  empty_stats(),  # 3. Baseline Tam TP2
        "s4_v2_half":   empty_stats(),  # 4. V2(vol≥1.5) ½TP1+½TP2
        "s5_v2_tp1":    empty_stats(),  # 5. V2(vol≥1.5) Tam TP1
        "s6_v2_tp2":    empty_stats(),  # 6. V2(vol≥1.5) Tam TP2
    }

    total_coins = len(symbols)

    for sym_idx, symbol in enumerate(symbols, 1):
        if symbol == "BTC/USDT":
            continue

        print(f"[{sym_idx}/{total_coins}] {symbol}", flush=True)
        df_raw = load_or_fetch(symbol)
        if df_raw is None or len(df_raw) < 300:
            print("  → Yetersiz veri, atlanıyor."); continue

        # Hacim indikatörleri (SMC.py ile aynı mantık)
        vol = df_raw["volume"]
        df_raw = df_raw.copy()
        df_raw["vol_24h_usd"]  = (df_raw["close"] * vol).rolling(24).sum()
        df_raw["vol_ratio_20"] = vol / vol.rolling(20).mean().shift(1)

        df = df_raw.dropna(subset=["vol_24h_usd"]).copy()
        if len(df) < 300:
            print("  → İndikatör sonrası yetersiz veri, atlanıyor."); continue

        btc_aligned = btc_f.reindex(df.index, method="ffill")
        bts, bds, cls_, swls = run_choch_incremental(df)

        h_arr  = df["high"].values
        l_arr  = df["low"].values
        c_arr  = df["close"].values
        vol24  = df["vol_24h_usd"].values
        volr20 = df["vol_ratio_20"].values
        n      = len(df)

        # Cooldown: baseline ve V2 için bağımsız
        last_base = 0.0
        last_v2   = 0.0

        for i in range(250, n):
            if bts[i] != "CHoCH" or bds[i] != "BULLISH":
                continue

            entry  = cls_[i]
            sw_low = swls[i]
            if entry is None or entry <= 0:
                continue

            # SMC.py: stop = eski_swing_low * 0.995
            stop = sw_low * 0.995 if sw_low is not None else entry * 0.95
            if stop >= entry: stop = entry * 0.95
            risk = entry - stop
            if risk <= 0: risk = entry * 0.05
            tp1 = entry + risk
            tp2 = entry + risk * 2

            # Hacim filtresi — SMC.py MIN_VOLUME_24H
            if pd.isna(vol24[i]) or vol24[i] < MIN_VOL_24H:
                continue

            # BTC filtreleri — SMC.py check_btc_crash + check_btc_downtrend_active
            if i >= len(btc_aligned):
                continue
            bf = btc_aligned.iloc[i]
            if not bf["crash_ok"] or not bf["downtrend_ok"]:
                continue

            ts_h = df.index[i].timestamp() / 3600

            fh = h_arr[i + 1:]
            fl = l_arr[i + 1:]
            fc = c_arr[i + 1:]
            if len(fh) == 0:
                continue

            # Simülasyonları bir kez hesapla (aynı sinyal, farklı çıkış)
            p_half, o_half = sim_half(fh, fl, fc, entry, stop, tp1, tp2, EXPIRE_H)
            p_tp1,  o_tp1  = sim_tp1(fh, fl, fc, entry, stop, tp1, EXPIRE_H)
            p_tp2,  o_tp2  = sim_tp2(fh, fl, fc, entry, stop, tp2, EXPIRE_H)

            # Senaryo 1-3: Baseline (tüm BULLISH CHoCH sinyalleri)
            if ts_h - last_base >= COOLDOWN_H:
                record(S["s1_base_half"], p_half, o_half)
                record(S["s2_base_tp1"],  p_tp1,  o_tp1)
                record(S["s3_base_tp2"],  p_tp2,  o_tp2)
                last_base = ts_h

            # Senaryo 4-6: V2 — sadece hacim spike (vol_ratio_20 >= 1.5)
            vr = volr20[i]
            if not pd.isna(vr) and vr >= 1.5 and ts_h - last_v2 >= COOLDOWN_H:
                record(S["s4_v2_half"], p_half, o_half)
                record(S["s5_v2_tp1"],  p_tp1,  o_tp1)
                record(S["s6_v2_tp2"],  p_tp2,  o_tp2)
                last_v2 = ts_h

    for k in S:
        S[k] = finalize(S[k])
    return S


# ═══════════════════════════════════════════════════════════════════════
# RAPOR
# ═══════════════════════════════════════════════════════════════════════
def report(S, n_coins):
    W = 115
    scenarios = [
        ("s1_base_half", "1. Baseline — ½TP1+½TP2          (tüm CHoCH)"),
        ("s2_base_tp1",  "2. Baseline — Tam TP1             (tüm CHoCH)"),
        ("s3_base_tp2",  "3. Baseline — Tam TP2             (tüm CHoCH)"),
        ("s4_v2_half",   "4. V2 (vol≥1.5x) — ½TP1+½TP2"),
        ("s5_v2_tp1",    "5. V2 (vol≥1.5x) — Tam TP1"),
        ("s6_v2_tp2",    "6. V2 (vol≥1.5x) — Tam TP2"),
    ]

    print("\n" + "═"*W)
    print(f"  ESKI CHOCH BACKTEST — 2020-01-01 → bugün | {n_coins} coin | 1H Binance")
    print(f"  Metodoloji: SMC.py artımlı CHoCH | BTC crash({BTC_CRASH_PCT}%) + "
          f"downtrend filtresi | Cooldown:{COOLDOWN_H}h | Expire:{EXPIRE_H}h")
    print("═"*W)
    print(f"  {'Senaryo':<48} {'Sinyal':>7} {'W':>5} {'L':>5} "
          f"{'Kısmi':>6} {'Exp':>6} {'WR%':>6} {'Toplam P&L':>12} {'Ort P&L':>10}")
    print("─"*W)

    for i, (k, label) in enumerate(scenarios):
        b = S[k]
        t = b["total"]
        if t == 0:
            print(f"  {label:<48} {'sinyal yok':>7}")
        else:
            w  = b["wins"]
            lo = b["losses"]
            pa = b["partial"]
            e  = b["expired"]
            wr = b["wr"]
            pnl = b["pnl"]
            avg = b["avg_pnl"]
            print(f"  {label:<48} {t:7d} {w:5d} {lo:5d} "
                  f"{pa:6d} {e:6d} {wr:6.1f}% {pnl:+12.2f}% {avg:+10.4f}%")
        if i == 2:
            print("─"*W)

    print("═"*W + "\n")

    with open("backtest_results.json", "w") as f:
        json.dump(S, f, indent=2)
    print("✓ backtest_results.json kaydedildi")


# ═══════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-fetch", action="store_true", help="Cache kullan, indirme")
    ap.add_argument("--coins",    nargs="*",           help="Coin sembolleri (ör: BTC ETH SOL)")
    ap.add_argument("--n",        type=int,            help="İlk N coin ile test")
    args = ap.parse_args()

    os.makedirs(DATA_DIR, exist_ok=True)

    N = args.n or 50

    if args.coins:
        symbols = [s if "/" in s else s + "/USDT" for s in args.coins]
    elif args.no_fetch:
        pkls = sorted(f for f in os.listdir(DATA_DIR) if f.endswith(".pkl"))
        symbols = [f[:-4].replace("_", "/", 1) for f in pkls]
        symbols = [s for s in symbols if s.endswith("/USDT") and s not in IGNORED_COINS]
        print(f"Cache'den {len(symbols)} coin yüklendi")
    else:
        symbols = get_top_coins(N)

    if "BTC/USDT" in symbols:
        symbols.remove("BTC/USDT")
    symbols.insert(0, "BTC/USDT")

    print(f"\n--- BTC/USDT yükleniyor ---")
    btc_raw = load_or_fetch("BTC/USDT", force=False)
    if btc_raw is None:
        print("BTC verisi alınamadı!"); return

    if not args.no_fetch:
        print(f"\n--- Veri İndirme ({len(symbols)} coin) ---")
        for i, sym in enumerate(symbols, 1):
            if sym == "BTC/USDT":
                continue
            p = cache_path(sym)
            if os.path.exists(p):
                print(f"  [{i}/{len(symbols)}] {sym} — cache var")
                continue
            print(f"  [{i}/{len(symbols)}] {sym} indiriliyor...", end=" ", flush=True)
            df = load_or_fetch(sym)
            print(f"✓ ({len(df)} bar)" if df is not None else "HATA")
            time.sleep(0.1)

    print(f"\n--- Backtest Başlıyor ({len(symbols)} coin) ---")
    S = run(symbols, btc_raw)
    report(S, len(symbols))


if __name__ == "__main__":
    main()
