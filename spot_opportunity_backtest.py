# -*- coding: utf-8 -*-
"""INTRADAY SPOT SCANNER icin walk-forward hesap simulasyonu.

Yeni scanner ile ayni strateji fonksiyonlarini kullanir:
- 1H watchlist / confirmation
- 15M execution
- kapanmis mum disinda veri gostermez
- komisyon + slippage + dinamik sermaye + max acik pozisyon hesaba katilir

Canli scanner'i degistirmez ve dis servislere gonderim yapmaz.
"""
from __future__ import annotations

import argparse
import json
import statistics
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

import spot_opportunity_scanner as scanner

INTERVAL_MS = {"15m": 900_000, "1h": 3_600_000}
FRAME_LIMITS = {"15m": 260, "1h": 260}

def utc_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)

def closed_quarter() -> datetime:
    now = datetime.now(timezone.utc)
    minute = (now.minute // 15) * 15
    return now.replace(minute=minute, second=0, microsecond=0)

def raw_to_df(rows: list[list]) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=[
        "open_time", "open", "high", "low", "close", "volume", "close_time",
        "quote_volume", "trades", "taker_base", "taker_quote", "ignore",
    ])
    for col in ("open", "high", "low", "close", "volume", "quote_volume", "taker_quote"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
    return df.dropna(subset=["open", "high", "low", "close", "volume"]).reset_index(drop=True)

def fetch_range(symbol: str, interval: str, start: datetime, end: datetime) -> pd.DataFrame:
    rows: list[list] = []
    cursor = utc_ms(start)
    end_ms = utc_ms(end)
    while cursor < end_ms:
        batch = scanner.api_get("/api/v3/klines", {
            "symbol": symbol, "interval": interval, "startTime": cursor,
            "endTime": end_ms - 1, "limit": 1000,
        })
        if not batch:
            break
        rows.extend(batch)
        next_cursor = int(batch[-1][6]) + 1
        if next_cursor <= cursor:
            break
        cursor = next_cursor
        if len(batch) == 1000:
            time.sleep(0.03)
    if not rows:
        raise ValueError(f"Veri yok: {symbol} {interval}")
    unique = {int(row[0]): row for row in rows}
    return raw_to_df([unique[k] for k in sorted(unique)])

def frame_at(df: pd.DataFrame, cutoff: datetime, limit: int) -> pd.DataFrame:
    sliced = df[df["close_time"] < cutoff].tail(limit).copy()
    if len(sliced) < 60:
        raise ValueError("Yetersiz kapanmis mum")
    return sliced.reset_index(drop=True)

def historical_quote_volume(h1: pd.DataFrame, cutoff: datetime) -> float:
    d = h1[h1["close_time"] < cutoff].tail(24)
    return float(d["quote_volume"].sum()) if not d.empty else 0.0

def download_symbol(symbol: str, first_cutoff: datetime, final_end: datetime) -> dict[str, pd.DataFrame]:
    data: dict[str, pd.DataFrame] = {}
    for interval, limit in FRAME_LIMITS.items():
        warmup = timedelta(milliseconds=INTERVAL_MS[interval] * (limit + 20))
        data[interval] = fetch_range(symbol, interval, first_cutoff - warmup, final_end)
    return data

@contextmanager
def historical_fetch(all_data: dict[str, dict[str, pd.DataFrame]], cutoff: datetime):
    original = scanner.fetch_ohlcv
    def replacement(symbol: str, interval: str, limit: int = 260) -> pd.DataFrame:
        if symbol not in all_data or interval not in all_data[symbol]:
            raise ValueError(f"Backtest verisi yok: {symbol} {interval}")
        return frame_at(all_data[symbol][interval], cutoff, limit)
    scanner.fetch_ohlcv = replacement
    try:
        yield
    finally:
        scanner.fetch_ohlcv = original

def current_symbols(limit: int) -> list[str]:
    universe = scanner.get_spot_universe()
    symbols = [symbol for symbol, _ in universe]
    return symbols if limit <= 0 else symbols[:limit]

def mark_to_market(open_positions: dict[str, dict], data: dict[str, dict[str, pd.DataFrame]], cutoff: datetime) -> float:
    value = 0.0
    for symbol, pos in open_positions.items():
        d = data[symbol]["15m"]
        rows = d[d["close_time"] < cutoff]
        price = float(rows["close"].iloc[-1]) if not rows.empty else pos["entry"]
        value += pos["qty"] * price
    return value

def close_trade(symbol: str, pos: dict, exit_price: float, reason: str, cutoff: datetime,
                cash: float, fee_rate: float, slippage: float, records: list[dict]) -> float:
    effective_exit = exit_price * (1 - slippage)
    gross = pos["qty"] * effective_exit
    exit_fee = gross * fee_rate
    cash += gross - exit_fee
    net_pnl = (effective_exit - pos["effective_entry"]) * pos["qty"] - pos["entry_fee"] - exit_fee
    net_pct = net_pnl / pos["capital_used"] * 100 if pos["capital_used"] else 0.0
    records.append({
        "symbol": symbol, "entry_time": pos["entry_time"].isoformat(), "exit_time": cutoff.isoformat(),
        "entry": round(pos["entry"], 10), "exit": round(exit_price, 10), "stop": round(pos["stop"], 10),
        "target": round(pos["target"], 10), "reason": reason, "net_pnl": round(net_pnl, 2),
        "net_pct": round(net_pct, 3), "entry_score": pos["entry_score"], "setup": pos["setup"],
        "btc_regime": pos["btc_regime"], "stop_pct": pos["stop_pct"], "target_pct": pos["target_pct"],
    })
    return cash

def update_open_positions(positions: dict[str, dict], all_data: dict[str, dict[str, pd.DataFrame]],
                          cutoff: datetime, cash: float, fee_rate: float, slippage: float,
                          records: list[dict]) -> float:
    to_close: list[tuple[str, float, str]] = []
    bar_open = cutoff - timedelta(minutes=15)
    for symbol, pos in positions.items():
        d = all_data[symbol]["15m"]
        row = d[d["open_time"] == bar_open]
        if row.empty:
            continue
        r = row.iloc[-1]
        hit_stop = float(r["low"]) <= pos["stop"]
        hit_target = float(r["high"]) >= pos["target"]
        if hit_stop and hit_target:
            to_close.append((symbol, pos["stop"], "STOP_AMBIGUOUS"))
        elif hit_stop:
            to_close.append((symbol, pos["stop"], "STOP"))
        elif hit_target:
            to_close.append((symbol, pos["target"], "TARGET"))
    for symbol, price, reason in to_close:
        pos = positions.pop(symbol)
        cash = close_trade(symbol, pos, price, reason, cutoff, cash, fee_rate, slippage, records)
    return cash

def summarize(records: list[dict], equity_curve: list[dict], start_equity: float) -> dict[str, Any]:
    wins = [r for r in records if r["net_pnl"] > 0]
    losses = [r for r in records if r["net_pnl"] <= 0]
    end_equity = equity_curve[-1]["equity"] if equity_curve else start_equity
    peak = start_equity
    max_dd = 0.0
    for point in equity_curve:
        eq = point["equity"]
        peak = max(peak, eq)
        dd = (eq / peak - 1) * 100 if peak else 0.0
        max_dd = min(max_dd, dd)
    gross_win = sum(r["net_pnl"] for r in wins)
    gross_loss = abs(sum(r["net_pnl"] for r in losses))
    return {
        "start_equity": round(start_equity, 2), "end_equity": round(end_equity, 2),
        "net_pnl": round(end_equity - start_equity, 2),
        "return_pct": round((end_equity / start_equity - 1) * 100, 2),
        "closed_trades": len(records), "wins": len(wins), "losses": len(losses),
        "win_rate_pct": round(100 * len(wins) / len(records), 2) if records else 0.0,
        "avg_win": round(statistics.fmean([r["net_pnl"] for r in wins]), 2) if wins else 0.0,
        "avg_loss": round(statistics.fmean([r["net_pnl"] for r in losses]), 2) if losses else 0.0,
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss else None,
        "max_drawdown_pct": round(max_dd, 2),
    }

def main() -> None:
    parser = argparse.ArgumentParser(description="15M execution + 1H confirmation walk-forward backtest")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--symbols", type=int, default=40)
    parser.add_argument("--only-symbol", default="")
    parser.add_argument("--account", type=float, default=10000.0)
    parser.add_argument("--risk-pct", type=float, default=1.25)
    parser.add_argument("--max-position-pct", type=float, default=40.0)
    parser.add_argument("--max-open", type=int, default=4)
    parser.add_argument("--fee-pct", type=float, default=0.10, help="Tek yon komisyon yuzdesi")
    parser.add_argument("--slippage-pct", type=float, default=0.05, help="Tek yon varsayilan slippage")
    parser.add_argument("--output", default="/tmp/spot_intraday_backtest.json")
    args = parser.parse_args()
    if args.days < 3 or args.account <= 0 or args.max_open < 1:
        raise SystemExit("days>=3, account>0 ve max-open>=1 olmali")
    final_end = closed_quarter()
    first_cutoff = final_end - timedelta(days=args.days)
    if args.only_symbol:
        symbol = args.only_symbol.upper().replace("/", "")
        symbols = [symbol if symbol.endswith("USDT") else symbol + "USDT"]
    else:
        symbols = current_symbols(args.symbols)
    print(f"[TEST] {len(symbols)} sembol | {args.days} gun | 15M karar / 1H watchlist")
    all_data: dict[str, dict[str, pd.DataFrame]] = {}
    for i, symbol in enumerate(["BTCUSDT", *symbols], start=1):
        if symbol in all_data:
            continue
        try:
            all_data[symbol] = download_symbol(symbol, first_cutoff, final_end)
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
    last_watch_hour: datetime | None = None
    cutoffs = pd.date_range(first_cutoff, final_end, freq="15min", tz="UTC").to_pydatetime()
    for n, cutoff in enumerate(cutoffs, start=1):
        cash = update_open_positions(positions, all_data, cutoff, cash, fee_rate, slippage, records)
        with historical_fetch(all_data, cutoff):
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
                        quote_vol = historical_quote_volume(all_data[symbol]["1h"], cutoff)
                        item = scanner.score_watch(symbol, quote_vol, btc)
                        if item:
                            watch_candidates.append(item)
                    except Exception:
                        continue
                watch_candidates.sort(
                    key=lambda x: (x.watch_score, x.relative_strength_4h, x.quote_volume_24h), reverse=True
                )
                watchlist = watch_candidates[:scanner.WATCHLIST_MAX]
            signals = []
            if btc["regime"] != "RED":
                for item in watchlist:
                    if item.symbol in positions:
                        continue
                    try:
                        candidate = scanner.detect_execution(item, btc)
                        if candidate:
                            signals.append(candidate)
                    except Exception:
                        continue
            signals.sort(key=lambda c: (c.entry_score, c.metrics["relative_strength_4h"]), reverse=True)
        for candidate in signals:
            if len(positions) >= args.max_open or candidate.symbol in positions:
                continue
            equity = cash + mark_to_market(positions, all_data, cutoff)
            risk_dollars = equity * (args.risk_pct / 100)
            raw_position = risk_dollars / (candidate.stop_pct / 100)
            max_position = equity * (args.max_position_pct / 100)
            capital = min(raw_position, max_position, cash / (1 + fee_rate))
            if capital < 50:
                continue
            effective_entry = candidate.price * (1 + slippage)
            qty = capital / effective_entry
            entry_fee = capital * fee_rate
            total_cost = capital + entry_fee
            if total_cost > cash:
                continue
            cash -= total_cost
            positions[candidate.symbol] = {
                "entry_time": cutoff, "entry": candidate.price, "effective_entry": effective_entry,
                "qty": qty, "capital_used": capital, "entry_fee": entry_fee,
                "stop": candidate.stop, "target": candidate.target1, "entry_score": candidate.entry_score,
                "setup": candidate.setup, "btc_regime": candidate.btc_regime,
                "stop_pct": round(candidate.stop_pct, 3), "target_pct": round(candidate.target_pct, 3),
            }
        equity = cash + mark_to_market(positions, all_data, cutoff)
        equity_curve.append({"time": cutoff.isoformat(), "equity": round(equity, 2), "open": len(positions)})
        if n % 96 == 0:
            print(f"[PROGRESS] {n}/{len(cutoffs)} | equity=${equity:,.0f} | open={len(positions)} | trades={len(records)}")
    for symbol in list(positions):
        pos = positions.pop(symbol)
        d = all_data[symbol]["15m"]
        rows = d[d["close_time"] < final_end]
        price = float(rows["close"].iloc[-1])
        cash = close_trade(symbol, pos, price, "END", final_end, cash, fee_rate, slippage, records)
    equity_curve.append({"time": final_end.isoformat(), "equity": round(cash, 2), "open": 0})
    summary = summarize(records, equity_curve, args.account)
    result = {
        "config": vars(args), "period": {"start": first_cutoff.isoformat(), "end": final_end.isoformat()},
        "summary": summary, "trades": records, "equity_curve": equity_curve,
    }
    Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n" + "=" * 72)
    print("INTRADAY WALK-FORWARD SONUC")
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
    print(f"JSON:           {args.output}")

if __name__ == "__main__":
    main()
