"""Rescore the frozen production-v11 baseline as one $2,500 spot portfolio.

The scanner and Portfolio replay are not changed here.  This only turns their
already recorded signals into Burak's single-position capital ledger:
full capital in one trade, max 24h from the source lifecycle, and no new entry
after a realised +5% net day (TR date).
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

START_CAPITAL = 2500.0
DAILY_GOAL_PCT = 4.0
DAILY_LOCK_PCT = 5.0
TR_TZ = "Europe/Istanbul"
SOURCE = Path(".github/tmp_prod_baseline_trades.csv")


def main():
    if not SOURCE.exists():
        raise RuntimeError(f"missing baseline input: {SOURCE}")
    d = pd.read_csv(SOURCE, parse_dates=["entry_time", "exit_time"])
    d["entry_time"] = pd.to_datetime(d.entry_time, utc=True)
    d["exit_time"] = pd.to_datetime(d.exit_time, utc=True, errors="coerce")
    # Existing CSV order is the scanner's rank order for equal-timestamp finals.
    d = d.sort_values(["entry_time"], kind="stable").reset_index(drop=True)

    capital = START_CAPITAL
    available_at = pd.Timestamp.min.tz_localize("UTC")
    daily_realised: dict[str, float] = {}
    rows = []

    for _, x in d.iterrows():
        entry = x.entry_time
        day = entry.tz_convert(TR_TZ).strftime("%Y-%m-%d")
        if entry < available_at:
            continue
        if daily_realised.get(day, 0.0) >= DAILY_LOCK_PCT:
            continue

        closed = x.status == "closed" and pd.notna(x.exit_time)
        end_time = x.exit_time if closed else entry
        ret = float(x.net_pct)
        before = capital
        capital *= 1.0 + ret / 100.0
        available_at = end_time if closed else pd.Timestamp.max.tz_localize("UTC")
        if closed:
            close_day = end_time.tz_convert(TR_TZ).strftime("%Y-%m-%d")
            daily_realised[close_day] = daily_realised.get(close_day, 0.0) + ret
        rows.append({
            "entry_time": entry, "exit_time": end_time if closed else pd.NaT,
            "symbol": x.symbol, "kind": x.kind, "rank": x["rank"],
            "reason": x.reason, "status": x.status, "hold_h": (
                (end_time-entry).total_seconds()/3600 if closed else None
            ),
            "net_pct": ret, "capital_before": before, "pnl_usdt": capital-before,
            "capital_after": capital,
        })

    ledger = pd.DataFrame(rows)
    daily = pd.DataFrame([
        {"tr_day": day, "realised_net_pct": ret,
         "target_4_hit": ret >= DAILY_GOAL_PCT,
         "locked_after_5": ret >= DAILY_LOCK_PCT}
        for day, ret in sorted(daily_realised.items())
    ])
    closed = ledger[ledger.status == "closed"] if len(ledger) else ledger
    result = {
        "version": "PROD_V11_SINGLE_POSITION_BASELINE",
        "start_capital": START_CAPITAL,
        "end_capital_mark_to_market": capital,
        "return_pct_mark_to_market": (capital / START_CAPITAL - 1.0) * 100,
        "selected_trades": int(len(ledger)),
        "closed_trades": int(len(closed)),
        "open_positions": int((ledger.status == "open").sum()) if len(ledger) else 0,
        "wins": int((closed.net_pct > 0).sum()) if len(closed) else 0,
        "losses": int((closed.net_pct < 0).sum()) if len(closed) else 0,
        "avg_hold_h": float(closed.hold_h.mean()) if len(closed) else None,
        "realised_days": int(len(daily)),
        "days_at_or_above_4pct": int(daily.target_4_hit.sum()) if len(daily) else 0,
        "days_locked_after_5pct": int(daily.locked_after_5.sum()) if len(daily) else 0,
        "daily_goal_pct": DAILY_GOAL_PCT,
        "daily_lock_pct": DAILY_LOCK_PCT,
    }
    ledger.to_csv("/tmp/prod_capital_ledger.csv", index=False)
    daily.to_csv("/tmp/prod_capital_daily.csv", index=False)
    pd.DataFrame([result]).to_csv("/tmp/prod_capital_summary.csv", index=False)
    Path("/tmp/prod_capital_rules.json").write_text(json.dumps({
        "capital": START_CAPITAL, "one_open_position": True,
        "full_capital_per_trade": True, "daily_goal_pct": DAILY_GOAL_PCT,
        "daily_lock_after_realised_pct": DAILY_LOCK_PCT,
        "fee_roundtrip_pct": 0.20,
    }, indent=2), encoding="utf-8")
    print(pd.DataFrame([result]).to_string(index=False))
    print("DAILY")
    print(daily.to_string(index=False))


if __name__ == "__main__":
    main()
