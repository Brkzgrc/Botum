#!/usr/bin/env python3
"""
Paper Trading Backtest — Eski CHoCH + V2 (vol_ratio >= 1.5x)
Çıkış: Tam pozisyon — TP1 veya TP2 hangisi önce gelirse (V2c strateji)
$5000 portföy simülasyonu, 2025-01-01'den bugüne

Kullanım:
  python paper_backtest.py                   # Binance spot tümü (veri indirir)
  python paper_backtest.py --no-fetch        # sadece cache'deki coinler
  python paper_backtest.py --capital 10000 --size 1000
  python paper_backtest.py --coins BTC/USDT ETH/USDT SOL/USDT
"""

import argparse, heapq, json, os, pickle, time
import datetime as _dt
import numpy as np, pandas as pd

# ─── AYARLAR ────────────────────────────────────────────────────────────
DATA_DIR      = "backtest_data"
START_DATE    = pd.Timestamp("2025-01-01", tz="UTC")
INITIAL_CAP   = 5_000.0
POS_SIZE      = 500.0
MAX_POSITIONS = 10
COOLDOWN_H    = 24
EXPIRE_H      = 168
CHOCH_SW      = 5
VOL_PERIOD    = 20

IGNORED_COINS = {
    "UP/USDT", "DOWN/USDT", "BEAR/USDT", "BULL/USDT",
    "USDC/USDT", "TUSD/USDT", "FDUSD/USDT", "DAI/USDT", "USDP/USDT",
    "USDE/USDT", "UST/USDT", "USD/USDT", "XUSD/USDT", "USD1/USDT", "BFUSD/USDT",
    "USTC/USDT", "BUSD/USDT", "FRAX/USDT", "LUSD/USDT", "GUSD/USDT", "SUSD/USDT",
    "USDS/USDT", "USDX/USDT", "USDD/USDT", "CUSD/USDT", "OUSD/USDT", "MUSD/USDT",
    "RLUSD/USDT", "U/USDT",
    "EUR/USDT", "TRY/USDT", "GBP/USDT", "BRL/USDT", "RUB/USDT",
    "XAUT/USDT", "PAXG/USDT",
}
LEVERAGED_PATTERNS = ["UP","DOWN","BULL","BEAR","3L","3S","2L","2S","5L","5S","10L","10S"]


# ─── VERİ ───────────────────────────────────────────────────────────────
START_TS = int(_dt.datetime(2024, 1, 1, tzinfo=_dt.timezone.utc).timestamp() * 1000)


def load_pkl(symbol):
    path = os.path.join(DATA_DIR, symbol.replace("/", "_") + ".pkl")
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        return pickle.load(f)


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
        path = os.path.join(DATA_DIR, symbol.replace("/","_") + ".pkl")
        with open(path, "wb") as f:
            pickle.dump(df, f)
        return df
    except Exception as e:
        print(f"    ! {symbol} indirilemedi: {e}")
        return None


def get_all_binance_symbols():
    """Binance'taki tüm spot USDT çiftlerini döndürür (ignore + leveraged filtreli)."""
    try:
        import ccxt
        ex = ccxt.binance({"enableRateLimit": True})
        ex.load_markets()
        result = []
        for sym in ex.markets:
            if not sym.endswith("/USDT"): continue
            if ex.markets[sym].get("type") != "spot": continue
            if sym in IGNORED_COINS: continue
            base = sym.split("/")[0]
            if any(base.endswith(p) for p in LEVERAGED_PATTERNS): continue
            result.append(sym)
        if "BTC/USDT" in result:
            result.remove("BTC/USDT")
        result.insert(0, "BTC/USDT")
        return result
    except Exception as e:
        print(f"Binance market listesi alınamadı: {e}")
        return []


def get_cached_symbols():
    """Cache'deki geçerli coinleri döndürür (2022 öncesi verisi olanlar)."""
    if not os.path.isdir(DATA_DIR):
        return []
    symbols = []
    for fname in os.listdir(DATA_DIR):
        if not fname.endswith(".pkl"): continue
        sym = fname.replace(".pkl", "").replace("_", "/")
        if not sym.endswith("/USDT"): continue
        if sym in IGNORED_COINS: continue
        base = sym.split("/")[0]
        if any(base.endswith(p) for p in LEVERAGED_PATTERNS): continue
        try:
            with open(os.path.join(DATA_DIR, fname), "rb") as f:
                df = pickle.load(f)
            if df is None or len(df) < 300:
                continue
        except Exception:
            continue
        symbols.append(sym)
    if "BTC/USDT" in symbols:
        symbols.remove("BTC/USDT")
    symbols.insert(0, "BTC/USDT")
    return symbols


def load_or_fetch(symbol):
    df = load_pkl(symbol)
    if df is not None:
        return df
    print(f"    ↓ {symbol} indiriliyor...", end=" ", flush=True)
    df = fetch_and_save(symbol)
    if df is not None:
        print("✓")
    return df


# ─── İNDİKATÖRLER ───────────────────────────────────────────────────────
def prepare_bars(df):
    df = df.copy()
    c, v = df["close"], df["volume"]
    df["vol_ma"]       = v.rolling(VOL_PERIOD).mean()
    h, l = df["high"], df["low"]
    tr                 = pd.concat(
        [h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1
    ).max(axis=1)
    df["atr"]          = tr.ewm(alpha=1 / 14, adjust=False).mean()
    df["vol_ratio_20"] = v / v.rolling(20).mean().shift(1)
    return df.dropna(subset=["vol_ma", "atr"])


def build_btc_crash_filter(btc_df):
    df4h = btc_df.resample("4h", label="right", closed="right").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    ).dropna(subset=["close"])
    df4h["crash_ok"] = (df4h["close"] / df4h["close"].shift(1) - 1) * 100 > -3.0
    return df4h["crash_ok"].reindex(btc_df.index, method="ffill").fillna(True).astype(bool)


def detect_micro_choch(df):
    h = df["high"].values
    l = df["low"].values
    c = df["close"].values
    n = len(df)
    if n < CHOCH_SW + 10:
        return None, None, 0, None, None
    legs = [0] * n
    cur = 0
    for i in range(CHOCH_SW, n):
        ph = h[i - CHOCH_SW]; pl = l[i - CHOCH_SW]
        wh = max(h[i - CHOCH_SW + 1 : i + 1])
        wl = min(l[i - CHOCH_SW + 1 : i + 1])
        if ph > wh:   cur = 0
        elif pl < wl: cur = 1
        legs[i] = cur
    sh = sl = None; shx = slx = True; trend = 0
    bt = bd = cl = sw_low = None
    for i in range(CHOCH_SW + 1, n):
        if legs[i] != legs[i - 1]:
            if legs[i] == 1: sl = l[i - CHOCH_SW]; slx = False; sw_low = sl
            else:             sh = h[i - CHOCH_SW]; shx = False
        if i < n - 1:
            if sh is not None and not shx and c[i] > sh and c[i-1] <= sh: shx=True; trend=1
            if sl is not None and not slx and c[i] < sl and c[i-1] >= sl: slx=True; trend=-1
        if i == n - 1:
            if sh is not None and not shx and c[i] > sh and c[i-1] <= sh:
                bt="CHoCH" if trend==-1 else "BOS"; bd="BULLISH"; trend=1; cl=sh
            if sl is not None and not slx and c[i] < sl and c[i-1] >= sl:
                bt="CHoCH" if trend==1 else "BOS"; bd="BEARISH"; trend=-1; cl=sl
    return bt, bd, trend, cl, sw_low


# ─── SİNYAL TOPLAMA ─────────────────────────────────────────────────────
def collect_signals(symbols, btc_crash, fetch=True):
    all_signals = []
    skipped = 0
    for sym_i, symbol in enumerate(symbols, 1):
        print(f"  [{sym_i}/{len(symbols)}] {symbol}", flush=True)
        df_raw = load_or_fetch(symbol) if fetch else load_pkl(symbol)
        if df_raw is None or len(df_raw) < 300:
            skipped += 1; continue
        df = prepare_bars(df_raw)
        if len(df) < 300:
            continue

        crash_s = btc_crash.reindex(df.index, method="ffill").fillna(True).astype(bool)
        n = len(df)
        last_ts_h = 0.0

        for i in range(250, n):
            ts = df.index[i]
            if ts < START_DATE:
                continue
            ts_h = ts.timestamp() / 3600
            if ts_h - last_ts_h < COOLDOWN_H:
                continue
            if not bool(crash_s.iloc[i]):
                continue

            sl100 = df.iloc[max(0, i - 99) : i + 1]
            try:
                bt_e, bd_e, _, cl_e, sl_e = detect_micro_choch(sl100)
            except Exception:
                continue
            if not (bt_e == "CHoCH" and bd_e == "BULLISH" and cl_e is not None):
                continue

            vr = float(df.iloc[i].get("vol_ratio_20") or 0)
            if vr < 1.5:
                continue

            slp_e  = (sl_e * 0.995) if sl_e else cl_e * 0.95
            risk_e = cl_e - slp_e
            if risk_e <= 0: risk_e = cl_e * 0.05

            tp1_e = cl_e + risk_e
            tp2_e = cl_e + risk_e * 2

            future = df.iloc[i + 1 : i + 1 + EXPIRE_H][["high", "low", "close"]].copy()
            all_signals.append({
                "symbol":     symbol,
                "entry_time": ts,
                "entry":      cl_e,
                "stop":       slp_e,
                "tp1":        tp1_e,
                "tp2":        tp2_e,
                "future":     future,
                "vol_ratio":  vr,
                "risk_pct":   round(risk_e / cl_e * 100, 2),
            })
            last_ts_h = ts_h

    if skipped:
        print(f"  ({skipped} coin atlandı — veri yok / 2022 öncesi yok)")
    all_signals.sort(key=lambda x: x["entry_time"].timestamp())
    return all_signals


# ─── ÇIKIŞ HESAPLA (V2c) ────────────────────────────────────────────────
def compute_exit(sig, pos_size):
    """
    V2c: Tam pozisyon çıkışı.
    Stop → stop fiyatından tam çıkış
    TP1  → TP2'ye ulaşamadan TP1'e gelirse tam çıkış
    TP2  → TP2'ye ulaşırsa tam çıkış
    Expire → 168 saat sonunda market fiyatından çıkış
    """
    entry    = sig["entry"]
    stop     = sig["stop"]
    tp1      = sig["tp1"]
    tp2      = sig["tp2"]
    rows     = sig["future"]
    stop_pct = (stop - entry) / entry
    tp1_pct  = (tp1  - entry) / entry
    tp2_pct  = (tp2  - entry) / entry

    for ts, row in rows.iloc[:EXPIRE_H].iterrows():
        h = float(row["high"])
        l = float(row["low"])

        if l <= stop:
            return (ts, pos_size * (1 + stop_pct), "stop")
        if h >= tp2:
            return (ts, pos_size * (1 + tp2_pct), "tp2")
        if h >= tp1:
            return (ts, pos_size * (1 + tp1_pct), "tp1")

    if len(rows) > 0:
        last_idx = min(EXPIRE_H - 1, len(rows) - 1)
        last_c   = float(rows.iloc[last_idx]["close"])
        exp_pct  = (last_c - entry) / entry
        return (rows.index[last_idx], pos_size * (1 + exp_pct), "expire")

    return (sig["entry_time"], pos_size, "no_data")


# ─── PORTFÖY SİMÜLASYONU ────────────────────────────────────────────────
def simulate_portfolio(signals, initial_cap, pos_size, max_positions):
    cash       = initial_cap
    open_count = 0
    trade_log  = []
    equity_pts = [(START_DATE, initial_cap)]

    # (unix_ts, priority, counter, type, data)
    # priority: 0=exit önce, 1=signal sonra (aynı timestamp'ta)
    queue   = []
    counter = 0
    for sig in signals:
        heapq.heappush(queue, (sig["entry_time"].timestamp(), 1, counter, "signal", sig))
        counter += 1

    while queue:
        unix_ts, _, _, etype, data = heapq.heappop(queue)
        ts = pd.Timestamp(unix_ts, unit="s", tz="UTC")

        if etype == "signal":
            if cash < pos_size or open_count >= max_positions:
                continue
            sig = data
            cash       -= pos_size
            open_count += 1
            trade_id    = counter; counter += 1

            exit_ts, cash_ret, label = compute_exit(sig, pos_size)
            heapq.heappush(queue, (
                exit_ts.timestamp(), 0, counter, "exit",
                {
                    "trade_id":   trade_id,
                    "symbol":     sig["symbol"],
                    "entry_time": sig["entry_time"],
                    "entry":      sig["entry"],
                    "stop":       sig["stop"],
                    "tp1":        sig["tp1"],
                    "tp2":        sig["tp2"],
                    "cash_ret":   cash_ret,
                    "label":      label,
                    "stop_pct":   round((sig["stop"] - sig["entry"]) / sig["entry"] * 100, 2),
                    "tp1_pct":    round((sig["tp1"]  - sig["entry"]) / sig["entry"] * 100, 2),
                    "tp2_pct":    round((sig["tp2"]  - sig["entry"]) / sig["entry"] * 100, 2),
                    "risk_pct":   sig.get("risk_pct", 0),
                }
            ))
            counter += 1

            trade_log.append({
                "type":       "ENTRY",
                "trade_id":   trade_id,
                "symbol":     sig["symbol"],
                "time":       str(ts)[:16],
                "entry":      round(sig["entry"], 6),
                "stop_pct":   round((sig["stop"] - sig["entry"]) / sig["entry"] * 100, 2),
                "tp1_pct":    round((sig["tp1"]  - sig["entry"]) / sig["entry"] * 100, 2),
                "tp2_pct":    round((sig["tp2"]  - sig["entry"]) / sig["entry"] * 100, 2),
                "risk_pct":   sig.get("risk_pct", 0),
                "vol_ratio":  round(sig.get("vol_ratio", 0), 2),
                "size":       pos_size,
                "cash_after": round(cash, 2),
                "open":       open_count,
            })
            equity_pts.append((ts, cash))

        elif etype == "exit":
            d = data
            cash       += d["cash_ret"]
            open_count -= 1
            net_pnl     = d["cash_ret"] - pos_size

            trade_log.append({
                "type":       "EXIT",
                "trade_id":   d["trade_id"],
                "symbol":     d["symbol"],
                "entry_time": str(d["entry_time"])[:16],
                "time":       str(ts)[:16],
                "label":      d["label"],
                "net_pnl":    round(net_pnl, 2),
                "net_pct":    round(net_pnl / pos_size * 100, 2),
                "cash_ret":   round(d["cash_ret"], 2),
                "cash_after": round(cash, 2),
                "open":       open_count,
                "tp1_pct":    d["tp1_pct"],
                "tp2_pct":    d["tp2_pct"],
                "stop_pct":   d["stop_pct"],
                "risk_pct":   d["risk_pct"],
            })
            equity_pts.append((ts, cash))

    return trade_log, equity_pts, cash


# ─── RAPOR ──────────────────────────────────────────────────────────────
def report(trade_log, equity_pts, initial_cap, pos_size, symbols):
    W = 94

    entries = [t for t in trade_log if t["type"] == "ENTRY"]
    exits   = [t for t in trade_log if t["type"] == "EXIT"]

    tp2_exits    = [e for e in exits if e["label"] == "tp2"]
    tp1_exits    = [e for e in exits if e["label"] == "tp1"]
    stop_exits   = [e for e in exits if e["label"] == "stop"]
    expire_exits = [e for e in exits if e["label"] in ("expire", "no_data")]

    wins    = len(tp2_exits) + len(tp1_exits)
    losses  = len(stop_exits)
    expires = len(expire_exits)
    decided = wins + losses
    wr      = wins / decided * 100 if decided > 0 else 0.0

    final_cap    = equity_pts[-1][1] if equity_pts else initial_cap
    total_return = (final_cap - initial_cap) / initial_cap * 100
    total_pnl_usd = final_cap - initial_cap

    # Max drawdown
    peak = initial_cap; max_dd = 0.0
    for _, cap in equity_pts:
        if cap > peak: peak = cap
        dd = (cap - peak) / peak * 100 if peak > 0 else 0.0
        if dd < max_dd: max_dd = dd

    # Ortalama R kazanç / R kayıp
    avg_win_pct  = sum(e["net_pct"] for e in tp2_exits+tp1_exits) / wins if wins else 0
    avg_loss_pct = sum(e["net_pct"] for e in stop_exits) / losses if losses else 0
    avg_tp2_pct  = sum(e["tp2_pct"] for e in tp2_exits) / len(tp2_exits) if tp2_exits else 0
    avg_tp1_pct  = sum(e["tp1_pct"] for e in tp1_exits) / len(tp1_exits) if tp1_exits else 0

    # Aylık ve haftalık son equity
    monthly = {}; weekly = {}
    for ts, cap in equity_pts:
        monthly[str(ts)[:7]] = cap
        iso = ts.isocalendar()
        weekly[f"{iso[0]}-W{iso[1]:02d}"] = cap
    months = sorted(monthly.keys())
    weeks  = sorted(weekly.keys())

    days_total   = (_dt.datetime.utcnow() - _dt.datetime(2025, 1, 1)).days or 1
    sigs_per_day = len(entries) / days_total

    print()
    print("═" * W)
    print("  PAPER TRADING — Eski CHoCH + V2 (vol ≥ 1.5x)")
    print("  Çıkış: TP2 varsa tam TP2, yoksa tam TP1, yoksa stop/expire (V2c)")
    print(f"  Dönem: 2025-01-01 → bugün  |  {len(symbols)} coin  |  1H Binance")
    print("═" * W)
    print(f"  Başlangıç:      ${initial_cap:>9,.2f}")
    print(f"  Bitiş:          ${final_cap:>9,.2f}  ({total_return:+.1f}%)")
    print(f"  Net Kâr:        ${total_pnl_usd:>+9,.2f}")
    print(f"  Maks Drawdown:  {max_dd:>9.1f}%")
    print(f"  İşlem başına:   ${pos_size:>9,.0f}")
    print("─" * W)
    print(f"  Toplam trade:   {len(entries)}  ({sigs_per_day:.1f}/gün ort.)")
    print(f"  TP2 çıkış:      {len(tp2_exits)}  (ort. +{avg_tp2_pct:.1f}%)")
    print(f"  TP1 çıkış:      {len(tp1_exits)}  (ort. +{avg_tp1_pct:.1f}%)")
    print(f"  Stop:           {len(stop_exits)}  (ort. {avg_loss_pct:.1f}%)")
    print(f"  Expire:         {expires}")
    print(f"  WR:             %{wr:.1f}  |  Ort kazanç: {avg_win_pct:+.1f}%  Ort kayıp: {avg_loss_pct:.1f}%")
    print("─" * W)

    # Haftalık özet
    print("  HAFTALIK ÖZET")
    print("─" * W)
    prev_cap = initial_cap
    for w in weeks:
        cap   = weekly[w]
        w_ret = (cap - prev_cap) / prev_cap * 100 if prev_cap > 0 else 0.0
        sign  = "+" if w_ret >= 0 else ""
        bar   = "█" * min(int(abs(w_ret) * 5), 40)
        arrow = "▲" if w_ret >= 0 else "▼"
        print(f"  {w}  ${cap:>9,.2f}  {arrow} {sign}{w_ret:5.1f}%  {bar}")
        prev_cap = cap
    print("─" * W)

    # Aylık özet
    print("  AYLIK ÖZET")
    print("─" * W)
    prev_cap = initial_cap
    for m in months:
        cap   = monthly[m]
        m_ret = (cap - prev_cap) / prev_cap * 100 if prev_cap > 0 else 0.0
        sign  = "+" if m_ret >= 0 else ""
        bar   = "█" * min(int(abs(m_ret) * 3), 45)
        arrow = "▲" if m_ret >= 0 else "▼"
        print(f"  {m}  ${cap:>9,.2f}  {arrow} {sign}{m_ret:5.1f}%  {bar}")
        prev_cap = cap
    print("─" * W)

    # Son 30 giriş
    print("  SON 30 GİRİŞ")
    print("─" * W)
    print(f"  {'Zaman':<17} {'Coin':<12} {'Giriş':>10} {'Stop%':>7} {'TP1%':>6} {'TP2%':>6} {'Risk':>5} {'Vol':>5}  {'$Kalan':>9}")
    for t in entries[-30:]:
        print(
            f"  {t['time']:<17} {t['symbol']:<12} {t['entry']:>10.5f} "
            f"{t['stop_pct']:>7.1f} {t['tp1_pct']:>6.1f} {t['tp2_pct']:>6.1f} "
            f"{t['risk_pct']:>5.1f}% {t['vol_ratio']:>5.1f}x  ${t['cash_after']:>8,.0f}"
        )

    # Son 30 çıkış
    print("─" * W)
    print("  SON 30 ÇIKIŞ")
    print("─" * W)
    emoji_map = {"tp2": "🚀", "tp1": "✅", "stop": "❌", "expire": "⏰", "no_data": "—"}
    print(f"  {'Zaman':<17} {'Coin':<12} {'Sonuç':<6} {'Net%':>7} {'Net$':>8}  {'$Toplam':>9}")
    for e in exits[-30:]:
        em = emoji_map.get(e["label"], "?")
        print(
            f"  {e['time']:<17} {e['symbol']:<12} {em} {e['label']:<4} "
            f"{e['net_pct']:>+7.1f}% ${e['net_pnl']:>+7.2f}  ${e['cash_after']:>8,.0f}"
        )
    print("═" * W)

    # JSON
    out = {
        "strategy":         "Eski CHoCH + V2 (vol>=1.5x) — V2c full exit",
        "initial_capital":  initial_cap,
        "final_capital":    round(final_cap, 2),
        "total_return_pct": round(total_return, 2),
        "total_pnl_usd":    round(total_pnl_usd, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "total_trades":     len(entries),
        "tp2_exits":        len(tp2_exits),
        "tp1_exits":        len(tp1_exits),
        "stops":            len(stop_exits),
        "expires":          expires,
        "win_rate_pct":     round(wr, 1),
        "avg_win_pct":      round(avg_win_pct, 2),
        "avg_loss_pct":     round(avg_loss_pct, 2),
        "avg_signals_per_day": round(sigs_per_day, 2),
        "weekly":           {w: round(weekly[w], 2) for w in weeks},
        "monthly":          {m: round(monthly[m], 2) for m in months},
        "coins_used":       symbols,
        "trade_log":        trade_log,
    }
    with open("paper_results.json", "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    print(f"\n  Kaydedildi → paper_results.json\n")


# ─── MAIN ───────────────────────────────────────────────────────────────
def main():
    global INITIAL_CAP, POS_SIZE, MAX_POSITIONS

    ap = argparse.ArgumentParser()
    ap.add_argument("--no-fetch", action="store_true", help="Sadece cache'deki coinler, indirme")
    ap.add_argument("--capital",  type=float, default=INITIAL_CAP,  help="Başlangıç sermaye ($)")
    ap.add_argument("--size",     type=float, default=POS_SIZE,     help="Trade başına miktar ($)")
    ap.add_argument("--max-pos",  type=int,   default=MAX_POSITIONS,help="Max açık pozisyon")
    ap.add_argument("--coins",    nargs="*",                        help="Belirli coinler")
    args = ap.parse_args()

    INITIAL_CAP   = args.capital
    POS_SIZE      = args.size
    MAX_POSITIONS = args.max_pos
    do_fetch      = not args.no_fetch

    # BTC verisi — önce cache, yoksa indir
    btc_raw = load_or_fetch("BTC/USDT") if do_fetch else load_pkl("BTC/USDT")
    if btc_raw is None:
        print("HATA: BTC/USDT verisi yok ve indirilemedi.")
        return
    btc_crash = build_btc_crash_filter(btc_raw)

    if args.coins:
        symbols = list(args.coins)
        if "BTC/USDT" not in symbols:
            symbols.insert(0, "BTC/USDT")
    elif args.no_fetch:
        symbols = get_cached_symbols()
    else:
        print("Binance spot USDT listesi alınıyor...")
        symbols = get_all_binance_symbols()
        if not symbols:
            print("Binance'a ulaşılamadı, cache kullanılıyor...")
            symbols = get_cached_symbols()

    if not symbols:
        print("Geçerli coin bulunamadı.")
        return

    mode = "cache" if args.no_fetch else "Binance spot tümü"
    print(f"\n{len(symbols)} coin ({mode}) | ${INITIAL_CAP:,.0f} sermaye | "
          f"${POS_SIZE:,.0f}/trade | max {MAX_POSITIONS} pozisyon")
    print(f"Sinyaller toplanıyor ({START_DATE.date()} → bugün)...")
    signals = collect_signals(symbols, btc_crash, fetch=do_fetch)
    print(f"\n{len(signals)} sinyal — portföy simülasyonu başlıyor...")

    trade_log, equity_pts, final_cash = simulate_portfolio(
        signals, INITIAL_CAP, POS_SIZE, MAX_POSITIONS
    )

    report(trade_log, equity_pts, INITIAL_CAP, POS_SIZE, symbols)


if __name__ == "__main__":
    main()
