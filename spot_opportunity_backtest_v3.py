# -*- coding: utf-8 -*-
"""V3 bagimsiz dogrulama backtesti.

Canli scanner'i DEGISTIRMEZ.
V2 execution geometrisini kullanir, fakat V2'nin 14 gunluk trade analizinden cikan
baslangic kalite filtresini sinyal aninda uygular:
- entry score >= 72
- gercek next-open girisinden sonra R/R >= 2.0
- gercek next-open girisinden sonra stop <= %2.0
- target en az scanner MIN_TARGET_PCT

Varsayilan test 30 gun / 40 semboldur. Amac V2'nin ayni 14 gunluk ornekleminde
bulunan filtrenin daha uzun donemde ayakta kalip kalmadigini gormektir.
"""
from __future__ import annotations

import argparse
import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pandas as pd

import spot_opportunity_scanner as scanner
import spot_opportunity_backtest as bt
import spot_strategy_diagnostic_v2 as v2

MIN_V3_SCORE = 72.0
MIN_V3_RR = 2.0
MAX_V3_STOP_PCT = 2.0


def build_v2_signal(item: Any, btc: dict[str, Any]) -> dict[str, Any] | None:
    reason, score, _ = v2.execution_probe_v2(item, btc)
    if reason != "ENTRY_OK" or score is None or float(score) < MIN_V3_SCORE:
        return None
    h1 = scanner.h1_state(scanner.fetch_ohlcv(item.symbol, "1h", 220))
    m15 = scanner.m15_state(scanner.fetch_ohlcv(item.symbol, "15m", 220))
    d = m15["df"]
    price = m15["price"]
    atr = max(m15["atr"], price * 0.0015)
    support = scanner.merge_supports(h1["df"], d)
    setups, breakout_level = v2.setup_v2(m15)
    if not support or not setups:
        return None
    structure = m15["structure"]
    last_swing_low = scanner.safe_float(structure.get("last_swing_low"))
    recent_low = scanner.safe_float(d["low"].tail(6).min())
    refs = [x for x in (last_swing_low, recent_low, scanner.safe_float(support.high)) if 0 < x < price]
    if breakout_level and 0 < breakout_level < price:
        refs.append(breakout_level)
    if not refs:
        return None
    stop = max(refs) - atr * 0.35
    target = v2.meaningful_target(h1["df"], d, price, scanner.MIN_TARGET_PCT)
    if not target:
        return None
    return {
        "symbol": item.symbol, "signal_price": price, "stop": stop, "target": target,
        "entry_score": float(score), "setup": "+".join(setups), "btc_regime": btc["regime"],
        "relative_strength_4h": item.relative_strength_4h,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="V3 quality-filter validation backtest")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--symbols", type=int, default=40)
    ap.add_argument("--account", type=float, default=10000.0)
    ap.add_argument("--risk-pct", type=float, default=1.25)
    ap.add_argument("--max-position-pct", type=float, default=40.0)
    ap.add_argument("--max-open", type=int, default=4)
    ap.add_argument("--fee-pct", type=float, default=0.10)
    ap.add_argument("--slippage-pct", type=float, default=0.05)
    ap.add_argument("--output", default="/tmp/spot_intraday_backtest_v3.json")
    args = ap.parse_args()
    if args.days < 3 or args.account <= 0 or args.max_open < 1:
        raise SystemExit("days>=3, account>0 ve max-open>=1 olmali")

    final_end = bt.closed_quarter()
    first_cutoff = final_end - timedelta(days=args.days)
    symbols = bt.current_symbols(args.symbols)
    print(f"[V3 TEST] {len(symbols)} sembol | {args.days} gun | score>={MIN_V3_SCORE:g} | RR>={MIN_V3_RR:g} | stop<=%{MAX_V3_STOP_PCT:g}")

    all_data: dict[str, dict[str, pd.DataFrame]] = {}
    for i, symbol in enumerate(["BTCUSDT", *symbols], 1):
        if symbol in all_data:
            continue
        try:
            all_data[symbol] = bt.download_symbol(symbol, first_cutoff, final_end)
            print(f"[DATA] {i}/{len(symbols)+1} {symbol}")
        except Exception as exc:
            print(f"[DATA] {symbol} atlandi: {exc}")
    symbols = [s for s in symbols if s in all_data]
    if "BTCUSDT" not in all_data or not symbols:
        raise SystemExit("Yeterli veri indirilemedi")

    scanner.ACCOUNT_SIZE = args.account
    scanner.RISK_PER_TRADE_PCT = args.risk_pct
    scanner.MAX_POSITION_PCT = args.max_position_pct
    fee_rate = args.fee_pct / 100
    slippage = args.slippage_pct / 100
    cash = args.account
    positions: dict[str, dict] = {}
    records: list[dict] = []
    equity_curve: list[dict] = []
    watchlist: list[Any] = []
    last_watch_hour = None
    counters = {"v2_score72": 0, "reject_next_open": 0, "reject_stop": 0, "reject_rr": 0, "capacity": 0, "entered": 0}

    cutoffs = pd.date_range(first_cutoff, final_end, freq="15min").to_pydatetime()
    for n, cutoff in enumerate(cutoffs, 1):
        cash = bt.update_open_positions(positions, all_data, cutoff, cash, fee_rate, slippage, records)
        with bt.historical_fetch(all_data, cutoff):
            try:
                btc = scanner.btc_context()
            except Exception:
                continue
            hour_key = cutoff.replace(minute=0, second=0, microsecond=0)
            if last_watch_hour != hour_key:
                last_watch_hour = hour_key
                candidates = []
                for symbol in symbols:
                    try:
                        qv = bt.historical_quote_volume(all_data[symbol]["1h"], cutoff)
                        item = scanner.score_watch(symbol, qv, btc)
                        if item:
                            candidates.append(item)
                    except Exception:
                        continue
                candidates.sort(key=lambda x: (x.watch_score, x.relative_strength_4h, x.quote_volume_24h), reverse=True)
                watchlist = candidates[:scanner.WATCHLIST_MAX]

            signals = []
            if btc["regime"] != "RED":
                for item in watchlist:
                    if item.symbol in positions:
                        continue
                    try:
                        signal = build_v2_signal(item, btc)
                        if signal:
                            signals.append(signal)
                            counters["v2_score72"] += 1
                    except Exception:
                        continue
            signals.sort(key=lambda x: (x["entry_score"], x["relative_strength_4h"]), reverse=True)

        for signal in signals:
            symbol = signal["symbol"]
            if len(positions) >= args.max_open or symbol in positions:
                counters["capacity"] += 1
                continue
            entry = bt.next_bar_open(all_data, symbol, cutoff)
            if entry is None or signal["stop"] >= entry or signal["target"] <= entry:
                counters["reject_next_open"] += 1
                continue
            stop_pct = (entry - signal["stop"]) / entry * 100
            target_pct = (signal["target"] / entry - 1) * 100
            if stop_pct <= 0 or stop_pct > MAX_V3_STOP_PCT:
                counters["reject_stop"] += 1
                continue
            if target_pct < scanner.MIN_TARGET_PCT:
                counters["reject_next_open"] += 1
                continue
            rr = target_pct / stop_pct
            if rr < MIN_V3_RR:
                counters["reject_rr"] += 1
                continue

            equity = cash + bt.mark_to_market(positions, all_data, cutoff)
            risk_dollars = equity * (args.risk_pct / 100)
            raw_position = risk_dollars / (stop_pct / 100)
            max_position = equity * (args.max_position_pct / 100)
            capital = min(raw_position, max_position, cash / (1 + fee_rate))
            if capital < 50:
                counters["capacity"] += 1
                continue
            effective_entry = entry * (1 + slippage)
            qty = capital / effective_entry
            entry_fee = capital * fee_rate
            if capital + entry_fee > cash:
                counters["capacity"] += 1
                continue
            cash -= capital + entry_fee
            positions[symbol] = {
                "entry_time": cutoff, "signal_price": signal["signal_price"], "entry": entry,
                "effective_entry": effective_entry, "qty": qty, "capital_used": capital,
                "entry_fee": entry_fee, "stop": signal["stop"], "target": signal["target"],
                "entry_score": round(signal["entry_score"], 2), "setup": signal["setup"],
                "btc_regime": signal["btc_regime"], "stop_pct": round(stop_pct, 3),
                "target_pct": round(target_pct, 3), "rr": round(rr, 3),
            }
            counters["entered"] += 1

        equity = cash + bt.mark_to_market(positions, all_data, cutoff)
        equity_curve.append({"time": cutoff.isoformat(), "equity": round(equity, 2), "open": len(positions)})
        if n % 192 == 0:
            print(f"[PROGRESS] {n}/{len(cutoffs)} | equity=${equity:,.0f} | open={len(positions)} | trades={len(records)} | entered={counters['entered']}")

    for symbol in list(positions):
        pos = positions.pop(symbol)
        rows = all_data[symbol]["15m"]
        rows = rows[rows["close_time"] < final_end]
        cash = bt.close_trade(symbol, pos, float(rows["close"].iloc[-1]), "END", final_end, cash, fee_rate, slippage, records)
    equity_curve.append({"time": final_end.isoformat(), "equity": round(cash, 2), "open": 0})
    summary = bt.summarize(records, equity_curve, args.account)
    result = {"strategy": "V3 score>=72 RR>=2 stop<=2%", "config": vars(args), "filters": {"min_score": MIN_V3_SCORE, "min_rr": MIN_V3_RR, "max_stop_pct": MAX_V3_STOP_PCT}, "period": {"start": first_cutoff.isoformat(), "end": final_end.isoformat()}, "summary": summary, "funnel": counters, "trades": records, "equity_curve": equity_curve}
    Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "=" * 72)
    print("V3 VALIDATION SONUC")
    print("=" * 72)
    print(f"Baslangic:      ${summary['start_equity']:,.2f}")
    print(f"Bitis:          ${summary['end_equity']:,.2f}")
    print(f"Net PnL:        ${summary['net_pnl']:,.2f} ({summary['return_pct']:+.2f}%)")
    print(f"Trade:          {summary['closed_trades']}")
    print(f"Win rate:       %{summary['win_rate_pct']:.2f}")
    print(f"Ort. kazanc:    ${summary['avg_win']:,.2f}")
    print(f"Ort. kayip:     ${summary['avg_loss']:,.2f}")
    print(f"Profit factor:  {summary['profit_factor']}")
    print(f"Max drawdown:   %{summary['max_drawdown_pct']:.2f}")
    print(f"Funnel:         {counters}")
    print(f"JSON:           {args.output}")


if __name__ == "__main__":
    main()
