"""Fast, reproducible audit of frozen live-system entries.

It downloads each historical candle frame once (or restores it from REPLAY_CACHE_DIR),
then writes the raw baseline trades and simple loss-feature splits.  It never changes
the production scanner or Portfolio.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.abspath(".github"))
import tmp_spot_production_baseline as base
from tmp_prod_reset_combo_replay import FastSnapCache, capital


def feature_report(trades: pd.DataFrame) -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame()
    d = trades.copy()
    d["closed"] = d["status"].eq("closed")
    d["win"] = d["closed"] & d["net_pct"].gt(0)
    d["loss"] = d["closed"] & d["net_pct"].lt(0)
    checks = {
        "BTC_YELLOW_OR_RED": d["btc_regime"].isin(["YELLOW", "RED"]),
        "DAY_RSI_GE_62": d["day_rsi"].ge(62),
        "FOURH_EXTENDED_GE_6": d["four_dist_ema50"].ge(6),
        "FOURH_EMA20_SLOPE_GE_1": d["four_ema20_slope"].ge(1),
        "ONEH_EXTENDED_GE_2": d["one_dist_ema20"].ge(2),
        "ONEH_UPPER_WICK_GE_22": d["one_upper_wick"].ge(.22),
        "ONEH_STOCH_LE_75": d["one_stoch_k"].le(75),
        "FAST_STOCH_40_70": d["fast_stoch_k"].gt(40) & d["fast_stoch_k"].le(70),
    }
    rows = []
    for name, mask in checks.items():
        for bucket, selected in ((name, d[mask]), ("NOT_" + name, d[~mask])):
            closed = selected[selected["closed"]]
            rows.append({
                "feature_bucket": bucket,
                "signals": len(selected),
                "closed": len(closed),
                "wins": int(closed["win"].sum()),
                "losses": int(closed["loss"].sum()),
                "win_rate_pct": round(float(closed["win"].mean() * 100), 2) if len(closed) else np.nan,
                "avg_net_pct": round(float(closed["net_pct"].mean()), 3) if len(closed) else np.nan,
                "total_net_pct": round(float(closed["net_pct"].sum()), 3) if len(closed) else np.nan,
            })
    return pd.DataFrame(rows)


def main():
    base.verify_frozen_source()
    universe = base.current_universe()
    ticks = base.api("/api/v3/ticker/24hr")
    quote = {x.get("symbol"): base.prod.sf(x.get("quoteVolume"))
             for x in ticks if isinstance(x, dict)}
    top100 = sorted(universe, key=lambda s: quote.get(s, 0), reverse=True)[:100]
    print(f"[START] {base.START} -> {base.END}; top100={len(top100)}", flush=True)

    hourly = base.parallel_fetch(top100, "1h", base.START - pd.Timedelta(days=12), base.END)
    hourly_symbols = sorted(hourly)
    selection_cache, union = {}, set()
    for ts in pd.date_range(base.START.ceil("1h"), base.END, freq="1h", tz="UTC"):
        union.update(s for s, _, _ in base.selected_at(ts, hourly_symbols, hourly, selection_cache))
    union.add("BTCUSDT")
    union = sorted(union)

    data = {
        "1h": hourly,
        "15m": base.parallel_fetch(union, "15m", base.START - pd.Timedelta(days=3), base.END),
        "4h": base.parallel_fetch(union, "4h", base.START - pd.Timedelta(days=50), base.END),
        "1d": base.parallel_fetch(union, "1d", base.START - pd.Timedelta(days=270), base.END),
    }
    usable = sorted(set(union) & set(data["15m"]) & set(data["4h"]) & set(data["1d"]) & set(hourly))
    print(f"[STAGE] usable={len(usable)}", flush=True)

    base.SnapCache = FastSnapCache
    signals = base.replay_entries(usable, hourly, data)
    trades = base.apply_portfolio(signals)
    report = feature_report(trades)

    # Candidate identified only on the two poor weeks.  It is reported here,
    # but will be accepted/rejected solely by its separate holdout weeks.
    baseline_portfolio, _ = capital(trades, "LIVE_BASELINE")
    veto_trades = trades[trades["one_dist_ema20"].lt(4.0)].copy()
    veto_portfolio, _ = capital(veto_trades, "VETO_1H_EMA20_DIST_LT_4")
    portfolio_report = pd.DataFrame([baseline_portfolio, veto_portfolio])

    outputs = {
        "risk_audit_signals.csv": signals,
        "risk_audit_trades.csv": trades,
        "risk_audit_feature_report.csv": report,
        "risk_audit_portfolio_report.csv": portfolio_report,
    }
    for filename, frame in outputs.items():
        frame.to_csv("/tmp/" + filename, index=False)

    # Persist the expensive baseline result alongside the candle cache.  Later
    # veto rules only read this snapshot and finish in seconds.
    cache_root = os.getenv("REPLAY_CACHE_DIR", "").strip()
    if cache_root:
        anchor = os.getenv("REPLAY_END_UTC", "moving").replace(":", "").replace("-", "")
        snapshot_dir = Path(cache_root) / "audit-snapshots" / f"offset-{base.WEEK_OFFSET_DAYS}d-{anchor}"
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        for filename, frame in outputs.items():
            frame.to_csv(snapshot_dir / filename, index=False)
        print(f"[SNAPSHOT] saved {snapshot_dir}", flush=True)

    print("\n=== RISK FEATURE REPORT ===", flush=True)
    print(report.to_string(index=False), flush=True)


if __name__ == "__main__":
    main()
