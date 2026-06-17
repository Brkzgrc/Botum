#!/usr/bin/env python3
"""
Paper Trading Backtest — Eski CHoCH + V2 (vol_ratio >= 1.5)
$5000 portföy simülasyonu, 2025-01-01'den bugüne

Kullanım:
  python paper_backtest.py              # cache'deki tüm geçerli coinler
  python paper_backtest.py --n 30       # en fazla 30 coin
  python paper_backtest.py --capital 10000 --size 1000
  python paper_backtest.py --coins BTC/USDT ETH/USDT SOL/USDT

Ön koşul:
  Önce backtest.py ile veri çekilmiş olmalı:
    python backtest.py --n 50 --only eski
"""

import argparse, heapq, json, os, pickle
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
def load_pkl(symbol):
    path = os.path.join(DATA_DIR, symbol.replace("/", "_") + ".pkl")
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        return pickle.load(f)


def get_cached_symbols(n):
    if not os.path.isdir(DATA_DIR):
        print(f"HATA: {DATA_DIR}/ bulunamadı. Önce backtest.py ile veri çek.")
        return []
    pkls = [f for f in os.listdir(DATA_DIR) if f.endswith(".pkl")]
    symbols = []
    for fname in pkls:
        sym = fname.replace(".pkl", "").replace("_", "/")
        if not sym.endswith("/USDT"):
            continue
        if sym in IGNORED_COINS:
            continue
        base = sym.split("/")[0]
        if any(base.endswith(p) for p in LEVERAGED_PATTERNS):
            continue
        try:
            with open(os.path.join(DATA_DIR, fname), "rb") as f:
                df = pickle.load(f)
            if df is None or df.index[0] >= pd.Timestamp("2022-01-01", tz="UTC"):
                continue
        except Exception:
            continue
        symbols.append(sym)
    # BTC her zaman başa
    if "BTC/USDT" in symbols:
        symbols.remove("BTC/USDT")
    symbols.insert(0, "BTC/USDT")
    return symbols[:n]


# ─── İNDİKATÖRLER ───────────────────────────────────────────────────────
def prepare_bars(df):
    df = df.copy()
    c, h, l, v = df["close"], df["high"], df["low"], df["volume"]
    df["vol_ma"]       = v.rolling(VOL_PERIOD).mean()
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
        if ph > wh:
            cur = 0
        elif pl < wl:
            cur = 1
        legs[i] = cur
    sh = sl = None; shx = slx = True; trend = 0
    bt = bd = cl = sw_low = None
    for i in range(CHOCH_SW + 1, n):
        if legs[i] != legs[i - 1]:
            if legs[i] == 1:
                sl = l[i - CHOCH_SW]; slx = False; sw_low = sl
            else:
                sh = h[i - CHOCH_SW]; shx = False
        if i < n - 1:
            if sh is not None and not shx and c[i] > sh and c[i - 1] <= sh:
                shx = True; trend = 1
            if sl is not None and not slx and c[i] < sl and c[i - 1] >= sl:
                slx = True; trend = -1
        if i == n - 1:
            if sh is not None and not shx and c[i] > sh and c[i - 1] <= sh:
                bt = "CHoCH" if trend == -1 else "BOS"
                bd = "BULLISH"; trend = 1; cl = sh
            if sl is not None and not slx and c[i] < sl and c[i - 1] >= sl:
                bt = "CHoCH" if trend == 1 else "BOS"
                bd = "BEARISH"; trend = -1; cl = sl
    return bt, bd, trend, cl, sw_low


# ─── SİNYAL TOPLAMA ─────────────────────────────────────────────────────
def collect_signals(symbols, btc_crash):
    all_signals = []
    for sym_i, symbol in enumerate(symbols, 1):
        print(f"  [{sym_i}/{len(symbols)}] {symbol}", flush=True)
        df_raw = load_pkl(symbol)
        if df_raw is None or len(df_raw) < 300:
            continue
        if df_raw.index[0] >= pd.Timestamp("2022-01-01", tz="UTC"):
            continue
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

            bar = df.iloc[i]
            sl100 = df.iloc[max(0, i - 99) : i + 1]
            try:
                bt_e, bd_e, _, cl_e, sl_e = detect_micro_choch(sl100)
            except Exception:
                continue
            if not (bt_e == "CHoCH" and bd_e == "BULLISH" and cl_e is not None):
                continue

            # V2: hacim spike >= 1.5x
            vr = float(bar.get("vol_ratio_20") or 0)
            if vr < 1.5:
                continue

            slp_e  = (sl_e * 0.995) if sl_e else cl_e * 0.95
            risk_e = cl_e - slp_e
            if risk_e <= 0:
                risk_e = cl_e * 0.05
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
            })
            last_ts_h = ts_h

    all_signals.sort(key=lambda x: x["entry_time"].timestamp())
    return all_signals


# ─── NAKİT AKIŞI HESAPLA ────────────────────────────────────────────────
def compute_cash_events(sig, pos_size):
    """
    Bir sinyal için nakit akışı olaylarını hesapla.
    Döner: list of (timestamp, cash_amount, label, is_last_exit)

    Half-exit: TP1'de %50, TP2'de kalan %50 (SMC.py ile birebir)
    """
    entry = sig["entry"]
    stop  = sig["stop"]
    tp1   = sig["tp1"]
    tp2   = sig["tp2"]
    rows  = sig["future"]
    half  = pos_size / 2.0

    tp1_pct  = (tp1 - entry) / entry
    tp2_pct  = (tp2 - entry) / entry
    stop_pct = (stop - entry) / entry

    tp1_hit      = False
    tp1_hit_time = None

    for ts, row in rows.iloc[:EXPIRE_H].iterrows():
        h = float(row["high"])
        l = float(row["low"])

        # Stop — en yüksek öncelik
        if l <= stop:
            if tp1_hit:
                # TP1'de yarısı çıktı, stop'ta kalan yarısı
                return [
                    (tp1_hit_time, half * (1 + tp1_pct),  "tp1",            False),
                    (ts,           half * (1 + stop_pct), "stop_after_tp1", True),
                ]
            else:
                return [(ts, pos_size * (1 + stop_pct), "stop", True)]

        # TP2
        if h >= tp2:
            if tp1_hit:
                return [
                    (tp1_hit_time, half * (1 + tp1_pct), "tp1", False),
                    (ts,           half * (1 + tp2_pct), "tp2", True),
                ]
            else:
                # Aynı bar veya TP1 daha önce fark edilmedi
                return [
                    (ts, half * (1 + tp1_pct), "tp1", False),
                    (ts, half * (1 + tp2_pct), "tp2", True),
                ]

        # TP1
        if not tp1_hit and h >= tp1:
            tp1_hit      = True
            tp1_hit_time = ts

    # Expire
    if len(rows) > 0:
        last_idx = min(EXPIRE_H - 1, len(rows) - 1)
        last_c   = float(rows.iloc[last_idx]["close"])
        exp_ts   = rows.index[last_idx]
        exp_pct  = (last_c - entry) / entry
        if tp1_hit:
            return [
                (tp1_hit_time, half * (1 + tp1_pct), "tp1",        False),
                (exp_ts,       half * (1 + exp_pct), "expire_tp1", True),
            ]
        else:
            return [(exp_ts, pos_size * (1 + exp_pct), "expire", True)]

    return [(sig["entry_time"], pos_size, "no_data", True)]


# ─── PORTFÖY SİMÜLASYONU ────────────────────────────────────────────────
def simulate_portfolio(signals, initial_cap, pos_size, max_positions):
    """
    Event-driven portföy simülasyonu.
    - Exits önce (aynı timestamp'ta), sonra signals
    - Half-exit: TP1'de nakit kısmen serbest, TP2/stop'ta kalan
    """
    cash       = initial_cap
    open_count = 0
    trade_log  = []
    equity_pts = [(START_DATE, initial_cap)]

    # Öncelik: (unix_ts, priority, counter, etype, data)
    # priority: 0=exit (önce), 1=signal (sonra)
    queue   = []
    counter = 0
    for sig in signals:
        heapq.heappush(queue, (sig["entry_time"].timestamp(), 1, counter, "signal", sig))
        counter += 1

    while queue:
        unix_ts, _, _, etype, data = heapq.heappop(queue)
        ts = pd.Timestamp(unix_ts, unit="s", tz="UTC")

        if etype == "signal":
            sig = data
            if cash < pos_size or open_count >= max_positions:
                continue

            # Giriş
            cash       -= pos_size
            open_count += 1
            trade_id    = counter
            counter    += 1

            cash_events = compute_cash_events(sig, pos_size)
            stop_pct_v  = (sig["stop"] - sig["entry"]) / sig["entry"] * 100
            tp1_pct_v   = (sig["tp1"]  - sig["entry"]) / sig["entry"] * 100
            tp2_pct_v   = (sig["tp2"]  - sig["entry"]) / sig["entry"] * 100

            for ev_ts, cash_ret, label, is_last in cash_events:
                heapq.heappush(queue, (
                    ev_ts.timestamp(), 0, counter, "exit",
                    {
                        "trade_id":   trade_id,
                        "symbol":     sig["symbol"],
                        "entry_time": sig["entry_time"],
                        "entry":      sig["entry"],
                        "cash_ret":   cash_ret,
                        "label":      label,
                        "is_last":    is_last,
                        "stop_pct":   stop_pct_v,
                        "tp1_pct":    tp1_pct_v,
                        "tp2_pct":    tp2_pct_v,
                        "pos_size":   pos_size,
                    }
                ))
                counter += 1

            trade_log.append({
                "type":       "ENTRY",
                "trade_id":   trade_id,
                "symbol":     sig["symbol"],
                "time":       str(ts)[:16],
                "entry":      round(sig["entry"], 6),
                "stop":       round(sig["stop"], 6),
                "tp1":        round(sig["tp1"], 6),
                "tp2":        round(sig["tp2"], 6),
                "stop_pct":   round(stop_pct_v, 2),
                "tp1_pct":    round(tp1_pct_v, 2),
                "tp2_pct":    round(tp2_pct_v, 2),
                "size":       pos_size,
                "vol_ratio":  round(sig.get("vol_ratio", 0), 2),
                "cash_after": round(cash, 2),
                "open":       open_count,
            })
            equity_pts.append((ts, cash))

        elif etype == "exit":
            d = data
            cash += d["cash_ret"]
            if d["is_last"]:
                open_count -= 1
            trade_log.append({
                "type":       "EXIT",
                "trade_id":   d["trade_id"],
                "symbol":     d["symbol"],
                "entry_time": str(d["entry_time"])[:16],
                "time":       str(ts)[:16],
                "label":      d["label"],
                "cash_ret":   round(d["cash_ret"], 2),
                "cash_after": round(cash, 2),
                "is_last":    d["is_last"],
                "open":       open_count,
                "pos_size":   d["pos_size"],
            })
            equity_pts.append((ts, cash))

    return trade_log, equity_pts, cash


# ─── RAPOR ──────────────────────────────────────────────────────────────
def report(trade_log, equity_pts, initial_cap, symbols):
    W = 92

    entries = [t for t in trade_log if t["type"] == "ENTRY"]
    exits   = [t for t in trade_log if t["type"] == "EXIT"]

    # Per-trade net P&L
    trade_cf = {}  # trade_id → net cash flow (- ödenen + alınan)
    for t in trade_log:
        if t["type"] == "ENTRY":
            trade_cf[t["trade_id"]] = -t["size"]
        elif t["type"] == "EXIT":
            trade_cf[t["trade_id"]] = trade_cf.get(t["trade_id"], 0) + t["cash_ret"]

    wins = losses = neutral = 0
    for pnl in trade_cf.values():
        if pnl > 0.5:
            wins += 1
        elif pnl < -0.5:
            losses += 1
        else:
            neutral += 1

    total_trades = len(trade_cf)
    decided      = wins + losses
    wr           = wins / decided * 100 if decided > 0 else 0.0

    final_cap     = equity_pts[-1][1] if equity_pts else initial_cap
    total_return  = (final_cap - initial_cap) / initial_cap * 100

    # Max drawdown
    peak = initial_cap; max_dd = 0.0
    for _, cap in equity_pts:
        if cap > peak:
            peak = cap
        dd = (cap - peak) / peak * 100 if peak > 0 else 0.0
        if dd < max_dd:
            max_dd = dd

    # Aylık son equity
    monthly = {}
    for ts, cap in equity_pts:
        key = str(ts)[:7]
        monthly[key] = cap
    months = sorted(monthly.keys())

    # Günde ortalama kaç sinyal (2025-01-01 → bugün)
    import datetime as _dt
    days_total = (_dt.datetime.utcnow() - _dt.datetime(2025, 1, 1)).days or 1
    sigs_per_day = len(entries) / days_total

    print()
    print("═" * W)
    print("  PAPER TRADING — Eski CHoCH + V2 (vol_ratio ≥ 1.5x)  |  ½TP1 + ½TP2 çıkış")
    print(f"  Dönem: 2025-01-01 → bugün  |  {len(symbols)} coin  |  1H Binance")
    print("═" * W)
    print(f"  Başlangıç:     ${initial_cap:>9,.2f}")
    print(f"  Bitiş:         ${final_cap:>9,.2f}  ({total_return:+.1f}%)")
    print(f"  Maks Drawdown: {max_dd:>9.1f}%")
    print(f"  İşlem/trade:   ${POS_SIZE:>9,.0f}")
    print("─" * W)
    print(f"  Toplam trade:  {total_trades}  ({sigs_per_day:.1f}/gün ort)")
    print(f"  Kârlı:         {wins}")
    print(f"  Zararlı:       {losses}")
    print(f"  Nötr/Exp:      {neutral}")
    print(f"  WR:            %{wr:.1f}  (kesinleşen kâr/zarar bazında)")
    print("─" * W)

    # Aylık özet
    print("  AYLIK ÖZET")
    print("─" * W)
    prev_cap = initial_cap
    for m in months:
        cap   = monthly[m]
        m_ret = (cap - prev_cap) / prev_cap * 100 if prev_cap > 0 else 0.0
        sign  = "+" if m_ret >= 0 else ""
        bar   = "█" * min(int(abs(m_ret) * 3), 40)
        arrow = "▲" if m_ret >= 0 else "▼"
        print(f"  {m}  ${cap:>9,.2f}  {arrow} {sign}{m_ret:5.1f}%  {bar}")
        prev_cap = cap
    print("─" * W)

    # Son 30 giriş
    print("  SON 30 GİRİŞ")
    print("─" * W)
    hdr = f"  {'Zaman':<17} {'Coin':<12} {'Giriş':>10} {'Stop%':>7} {'TP1%':>7} {'TP2%':>7} {'Vol':>5}  {'$Kalan':>9}"
    print(hdr)
    for t in entries[-30:]:
        print(
            f"  {t['time']:<17} {t['symbol']:<12} {t['entry']:>10.5f} "
            f"{t['stop_pct']:>7.1f} {t['tp1_pct']:>7.1f} {t['tp2_pct']:>7.1f} "
            f"{t['vol_ratio']:>5.1f}x  ${t['cash_after']:>8,.0f}"
        )
    print("═" * W)

    # JSON kaydet
    out = {
        "initial_capital":  initial_cap,
        "final_capital":    round(final_cap, 2),
        "total_return_pct": round(total_return, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "total_trades":     total_trades,
        "wins":             wins,
        "losses":           losses,
        "neutral":          neutral,
        "win_rate_pct":     round(wr, 1),
        "avg_signals_per_day": round(sigs_per_day, 2),
        "monthly":          {m: round(monthly[m], 2) for m in months},
        "coins_used":       symbols,
        "trade_log":        trade_log,
    }
    with open("paper_results.json", "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    print(f"\n  Detaylar kaydedildi → paper_results.json\n")


# ─── MAIN ───────────────────────────────────────────────────────────────
def main():
    global INITIAL_CAP, POS_SIZE, MAX_POSITIONS

    ap = argparse.ArgumentParser()
    ap.add_argument("--n",       type=int,   default=50,           help="Max coin sayısı")
    ap.add_argument("--capital", type=float, default=INITIAL_CAP,  help="Başlangıç sermaye ($)")
    ap.add_argument("--size",    type=float, default=POS_SIZE,     help="Trade başına miktar ($)")
    ap.add_argument("--max-pos", type=int,   default=MAX_POSITIONS,help="Max açık pozisyon")
    ap.add_argument("--coins",   nargs="*",                        help="Belirli coinler")
    args = ap.parse_args()

    INITIAL_CAP   = args.capital
    POS_SIZE      = args.size
    MAX_POSITIONS = args.max_pos

    # BTC verisi (crash filtresi için şart)
    btc_raw = load_pkl("BTC/USDT")
    if btc_raw is None:
        print("HATA: BTC/USDT cache'de yok.")
        print("Önce şunu çalıştır: python backtest.py --coins BTC/USDT ETH/USDT SOL/USDT --only eski")
        return
    btc_crash = build_btc_crash_filter(btc_raw)

    if args.coins:
        symbols = list(args.coins)
        if "BTC/USDT" not in symbols:
            symbols.insert(0, "BTC/USDT")
    else:
        symbols = get_cached_symbols(args.n)

    if not symbols:
        print("Geçerli coin bulunamadı. Önce backtest.py ile veri çek.")
        return

    print(f"\n{len(symbols)} coin | ${INITIAL_CAP:,.0f} sermaye | "
          f"${POS_SIZE:,.0f}/trade | max {MAX_POSITIONS} pozisyon")
    print(f"Sinyaller toplanıyor ({START_DATE.date()} → bugün)...")
    signals = collect_signals(symbols, btc_crash)
    print(f"\n{len(signals)} sinyal bulundu — portföy simülasyonu başlıyor...")

    trade_log, equity_pts, final_cash = simulate_portfolio(
        signals, INITIAL_CAP, POS_SIZE, MAX_POSITIONS
    )

    report(trade_log, equity_pts, INITIAL_CAP, symbols)


if __name__ == "__main__":
    main()
