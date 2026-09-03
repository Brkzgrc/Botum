# -*- coding: utf-8 -*-
"""V2 execution mantigi icin gercekci walk-forward hesap simulasyonu.

Canli scanner'i DEGISTIRMEZ. 1H watch secimi mevcut scanner ile aynidir.
15M execution ise spot_strategy_diagnostic_v2 icindeki deneysel V2 mantigini kullanir.

- sadece kapanmis mumlarla sinyal
- sinyalden sonraki 15M mum acilisinda giris
- V2 mikro invalidation stopu ve anlamli pivot hedefi
- komisyon + slippage
- dinamik sermaye / risk sizing / max acik pozisyon
- stop ve hedef ayni mumda ise konservatif STOP_AMBIGUOUS
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


def main() -> None:
    parser = argparse.ArgumentParser(description="V2 15M execution + 1H confirmation walk-forward backtest")
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--symbols", type=int, default=20)
    parser.add_argument("--only-symbol", default="")
    parser.add_argument("--account", type=float, default=10000.0)
    parser.add_argument("--risk-pct", type=float, default=1.25)
    parser.add_argument("--max-position-pct", type=float, default=40.0)
    parser.add_argument("--max-open", type=int, default=4)
    parser.add_argument("--fee-pct", type=float, default=0.10)
    parser.add_argument("--slippage-pct", type=float, default=0.05)
    parser.add_argument("--output", default="/tmp/spot_intraday_backtest_v2.json")
    args = parser.parse_args()
    if args.days < 3 or args.account <= 0 or args.max_open < 1:
        raise SystemExit("days>=3, account>0 ve max-open>=1 olmali")

    final_end = bt.closed_quarter()
    first_cutoff = final_end - timedelta(days=args.days)
    if args.only_symbol:
        symbol = args.only_symbol.upper().replace("/", "")
        symbols = [symbol if symbol.endswith("USDT") else symbol + "USDT"]
    else:
        symbols = bt.current_symbols(args.symbols)

    print(f"[V2 TEST] {len(symbols)} sembol | {args.days} gun | 15M karar / 1H watchlist")
    all_data: dict[str, dict[str, pd.DataFrame]] = {}
    for i, symbol in enumerate(["BTCUSDT", *symbols], start=1):
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
    raw_v2_signals = 0
    rejected_next_open = 0
    skipped_capacity = 0

    cutoffs = pd.date_range(first_cutoff, final_end, freq="15min").to_pydatetime()
    for n, cutoff in enumerate(cutoffs, start=1):
        cash = bt.update_open_positions(positions, all_data, cutoff, cash, fee_rate, slippage, records)

        with bt.historical_fetch(all_data, cutoff):
            try:
                btc = scanner.btc_context()
            except Exception:
                continue

            hour_key = cutoff.replace(minute=0, second=0, microsecond=0)
            if last_watch_hour != hour_key:
                last_watch_hour = hour_key
                watch_candidates = []
                for symbol in symbols:
                    try:
                        quote_vol = bt.historical_quote_volume(all_data[symbol]["1h"], cutoff)
                        item = scanner.score_watch(symbol, quote_vol, btc)
                        if item:
                            watch_candidates.append(item)
                    except Exception:
                        continue
                watch_candidates.sort(
                    key=lambda x: (x.watch_score, x.relative_strength_4h, x.quote_volume_24h), reverse=True
                )
                watchlist = watch_candidates[:scanner.WATCHLIST_MAX]

            signals: list[dict[str, Any]] = []
            if btc["regime"] != "RED":
                for item in watchlist:
                    if item.symbol in positions:
                        continue
                    try:
                        reason, score, meta = v2.execution_probe_v2(item, btc)
                        if reason != "ENTRY_OK" or score is None:
                            continue
                        # Probe stop/target fiyatlarini meta'dan vermedigi icin ayni kapanmis veride
                        # deterministik olarak V2 geometrisini yeniden kuruyoruz.
                        h1 = scanner.h1_state(scanner.fetch_ohlcv(item.symbol, "1h", 220))
                        m15 = scanner.m15_state(scanner.fetch_ohlcv(item.symbol, "15m", 220))
                        d = m15["df"]
                        price = m15["price"]
                        atr = max(m15["atr"], price * 0.0015)
                        support = scanner.merge_supports(h1["df"], d)
                        setups, breakout_level = v2.setup_v2(m15)
                        if not support or not setups:
                            continue
                        structure = m15["structure"]
                        last_swing_low = scanner.safe_float(structure.get("last_swing_low"))
                        recent_low = scanner.safe_float(d["low"].tail(6).min())
                        refs = [x for x in (last_swing_low, recent_low, scanner.safe_float(support.high)) if 0 < x < price]
                        if breakout_level and 0 < breakout_level < price:
                            refs.append(breakout_level)
                        if not refs:
                            continue
                        invalidation = max(refs)
                        stop = invalidation - atr * 0.35
                        target = v2.meaningful_target(h1["df"], d, price, scanner.MIN_TARGET_PCT)
                        if not target:
                            continue
                        signals.append({
                            "symbol": item.symbol,
                            "signal_price": price,
                            "stop": stop,
                            "target": target,
                            "entry_score": float(score),
                            "setup": "+".join(setups),
                            "btc_regime": btc["regime"],
                            "relative_strength_4h": item.relative_strength_4h,
                        })
                        raw_v2_signals += 1
                    except Exception:
                        continue
            signals.sort(key=lambda x: (x["entry_score"], x["relative_strength_4h"]), reverse=True)

        for signal in signals:
            symbol = signal["symbol"]
            if len(positions) >= args.max_open or symbol in positions:
                skipped_capacity += 1
                continue
            entry_price = bt.next_bar_open(all_data, symbol, cutoff)
            if entry_price is None or signal["stop"] >= entry_price or signal["target"] <= entry_price:
                rejected_next_open += 1
                continue
            actual_stop_pct = (entry_price - signal["stop"]) / entry_price * 100
            actual_target_pct = (signal["target"] / entry_price - 1) * 100
            if actual_stop_pct <= 0 or actual_stop_pct > scanner.MAX_STOP_PCT:
                rejected_next_open += 1
                continue
            if actual_target_pct < scanner.MIN_TARGET_PCT:
                rejected_next_open += 1
                continue

            equity = cash + bt.mark_to_market(positions, all_data, cutoff)
            risk_dollars = equity * (args.risk_pct / 100)
            raw_position = risk_dollars / (actual_stop_pct / 100)
            max_position = equity * (args.max_position_pct / 100)
            capital = min(raw_position, max_position, cash / (1 + fee_rate))
            if capital < 50:
                skipped_capacity += 1
                continue
            effective_entry = entry_price * (1 + slippage)
            qty = capital / effective_entry
            entry_fee = capital * fee_rate
            total_cost = capital + entry_fee
            if total_cost > cash:
                skipped_capacity += 1
                continue
            cash -= total_cost
            positions[symbol] = {
                "entry_time": cutoff,
                "signal_price": signal["signal_price"],
                "entry": entry_price,
                "effective_entry": effective_entry,
                "qty": qty,
                "capital_used": capital,
                "entry_fee": entry_fee,
                "stop": signal["stop"],
                "target": signal["target"],
                "entry_score": round(signal["entry_score"], 2),
                "setup": signal["setup"],
                "btc_regime": signal["btc_regime"],
                "stop_pct": round(actual_stop_pct, 3),
                "target_pct": round(actual_target_pct, 3),
            }

        equity = cash + bt.mark_to_market(positions, all_data, cutoff)
        equity_curve.append({"time": cutoff.isoformat(), "equity": round(equity, 2), "open": len(positions)})
        if n % 96 == 0:
            print(
                f"[PROGRESS] {n}/{len(cutoffs)} | equity=${equity:,.0f} | open={len(positions)} "
                f"| trades={len(records)} | rawV2={raw_v2_signals}"
            )

    for symbol in list(positions):
        pos = positions.pop(symbol)
        d = all_data[symbol]["15m"]
        rows = d[d["close_time"] < final_end]
        price = float(rows["close"].iloc[-1])
        cash = bt.close_trade(symbol, pos, price, "END", final_end, cash, fee_rate, slippage, records)
    equity_curve.append({"time": final_end.isoformat(), "equity": round(cash, 2), "open": 0})

    summary = bt.summarize(records, equity_curve, args.account)
    result = {
        "strategy": "V2 experimental execution",
        "config": vars(args),
        "period": {"start": first_cutoff.isoformat(), "end": final_end.isoformat()},
        "summary": summary,
        "diagnostics": {
            "raw_v2_signals": raw_v2_signals,
            "rejected_at_next_open": rejected_next_open,
            "skipped_capacity_or_cash": skipped_capacity,
        },
        "trades": records,
        "equity_curve": equity_curve,
    }
    Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "=" * 72)
    print("V2 INTRADAY WALK-FORWARD SONUC")
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
    print(f"Ham V2 sinyal:  {raw_v2_signals}")
    print(f"Next-open red:  {rejected_next_open}")
    print(f"Kapasite/cash:  {skipped_capacity}")
    print(f"JSON:           {args.output}")


if __name__ == "__main__":
    main()
