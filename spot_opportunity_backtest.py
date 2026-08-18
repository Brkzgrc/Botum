# -*- coding: utf-8 -*-
"""SPOT Opportunity Scanner için geçmişe dönük walk-forward doğrulama.

Canlı tarayıcıyı değiştirmez ve emir üretmez. Karar anında yalnızca kapanmış
mumları gösterir; sonraki 1H mumlarla hedef/stop sonucunu ölçer.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock

import pandas as pd

import spot_opportunity_scanner as scanner


INTERVAL_MS = {
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
    "1w": 604_800_000,
}
FRAME_LIMITS = {"1h": 260, "4h": 260, "1d": 260, "1w": 160}
FRAME_LABELS = {"1h": "1H", "4h": "4H", "1d": "1D", "1w": "1W"}
FETCH_LOCK = Lock()


def utc_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def closed_hour() -> datetime:
    now = datetime.now(timezone.utc)
    return now.replace(minute=0, second=0, microsecond=0)


def raw_to_df(rows: list[list]) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=[
        "open_time", "open", "high", "low", "close", "volume", "close_time",
        "quote_volume", "trades", "taker_base", "taker_quote", "ignore",
    ])
    for col in ("open", "high", "low", "close", "volume", "quote_volume", "taker_quote"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
    return df.dropna(subset=["open", "high", "low", "close", "volume"])


def fetch_range(symbol: str, interval: str, start: datetime, end: datetime) -> pd.DataFrame:
    rows: list[list] = []
    cursor = utc_ms(start)
    end_ms = utc_ms(end)
    while cursor < end_ms:
        batch = scanner.api_get("/api/v3/klines", {
            "symbol": symbol,
            "interval": interval,
            "startTime": cursor,
            "endTime": end_ms - 1,
            "limit": 1000,
        })
        if not batch:
            break
        rows.extend(batch)
        next_cursor = int(batch[-1][6]) + 1
        if next_cursor <= cursor:
            break
        cursor = next_cursor
        if len(batch) == 1000:
            time.sleep(0.04)
    if not rows:
        raise ValueError(f"Veri yok: {symbol} {interval}")
    unique = {int(row[0]): row for row in rows}
    return raw_to_df([unique[k] for k in sorted(unique)])


def current_crypto_symbols(limit: int) -> list[str]:
    universe = scanner.get_spot_universe()
    symbols = [symbol for symbol, _ in universe]
    return symbols if limit <= 0 else symbols[:limit]


def download_symbol(symbol: str, first_cutoff: datetime, final_end: datetime) -> tuple[str, dict[str, pd.DataFrame]]:
    data: dict[str, pd.DataFrame] = {}
    for interval, history_limit in FRAME_LIMITS.items():
        warmup = timedelta(milliseconds=INTERVAL_MS[interval] * (history_limit + 8))
        data[interval] = fetch_range(symbol, interval, first_cutoff - warmup, final_end)
    return symbol, data


def frame_at(df: pd.DataFrame, cutoff: datetime, limit: int) -> pd.DataFrame:
    sliced = df[df["close_time"] < cutoff].tail(limit).copy()
    if len(sliced) < 50:
        raise ValueError("Yetersiz kapanmış mum")
    return sliced


def btc_at(data: dict[str, pd.DataFrame], cutoff: datetime) -> dict[str, float]:
    # frame_at ortak olarak en az 50 kapanmış mum doğrular. BTC getirileri daha
    # az mum kullansa da yeterli geçmiş bulunduğunu aynı kuralla teyit ederiz.
    h1 = frame_at(data["1h"], cutoff, 50)
    h4 = frame_at(data["4h"], cutoff, 50)
    return {
        "ret_1h": (h1["close"].iloc[-1] / h1["close"].iloc[-2] - 1) * 100,
        "ret_6h": (h1["close"].iloc[-1] / h1["close"].iloc[-7] - 1) * 100,
        "ret_24h": (h1["close"].iloc[-1] / h1["close"].iloc[-25] - 1) * 100,
        "ret_4h": (h4["close"].iloc[-1] / h4["close"].iloc[-2] - 1) * 100,
    }


@contextmanager
def historical_fetch(data: dict[str, pd.DataFrame], cutoff: datetime):
    original = scanner.fetch_ohlcv

    def replacement(symbol: str, interval: str, limit: int) -> pd.DataFrame:
        del symbol
        return frame_at(data[interval], cutoff, limit)

    scanner.fetch_ohlcv = replacement
    try:
        yield
    finally:
        scanner.fetch_ohlcv = original


def outcome(candidate, h1: pd.DataFrame, cutoff: datetime, horizon: int) -> dict:
    future = h1[h1["open_time"] >= cutoff].head(horizon)
    result = "OPEN"
    hours = None
    for i, row in enumerate(future.itertuples(), start=1):
        hit_stop = row.low <= candidate.stop
        hit_target = row.high >= candidate.target_low
        if hit_stop and hit_target:
            result, hours = "STOP_AMBIGUOUS", i
            break
        if hit_stop:
            result, hours = "STOP", i
            break
        if hit_target:
            result, hours = "TARGET", i
            break
    if future.empty:
        return {"result": "NO_DATA", "hours": None, "mfe": 0, "mae": 0, "close_return": 0}
    mfe = (future["high"].max() / candidate.price - 1) * 100
    mae = (future["low"].min() / candidate.price - 1) * 100
    close_return = (future["close"].iloc[-1] / candidate.price - 1) * 100
    return {
        "result": result, "hours": hours,
        "mfe": round(float(mfe), 3), "mae": round(float(mae), 3),
        "close_return": round(float(close_return), 3),
    }


def safe_mean(values: list[float]) -> float:
    return round(statistics.fmean(values), 3) if values else 0.0


def summarize(records: list[dict]) -> dict:
    counts = Counter(r["result"] for r in records)
    decided = counts["TARGET"] + counts["STOP"] + counts["STOP_AMBIGUOUS"]
    target_rate = 100 * counts["TARGET"] / decided if decided else 0
    by_setup: dict[str, dict] = {}
    for setup in sorted({r["setup"] for r in records}):
        group = [r for r in records if r["setup"] == setup]
        c = Counter(r["result"] for r in group)
        d = c["TARGET"] + c["STOP"] + c["STOP_AMBIGUOUS"]
        by_setup[setup] = {
            "n": len(group), "target_first_pct": round(100 * c["TARGET"] / d, 2) if d else 0,
            "avg_mfe_pct": safe_mean([r["mfe"] for r in group]),
            "avg_mae_pct": safe_mean([r["mae"] for r in group]),
        }
    return {
        "signals": len(records), "outcomes": dict(counts),
        "target_first_pct": round(target_rate, 2),
        "avg_mfe_pct": safe_mean([r["mfe"] for r in records]),
        "avg_mae_pct": safe_mean([r["mae"] for r in records]),
        "avg_horizon_close_pct": safe_mean([r["close_return"] for r in records]),
        "by_setup": by_setup,
    }


def print_summary(summary: dict) -> None:
    print("\n" + "=" * 72)
    print("WALK-FORWARD SONUÇ")
    print("=" * 72)
    print(f"Sinyal: {summary['signals']}")
    print(f"Sonuçlar: {summary['outcomes']}")
    print(f"Karar verilenlerde hedef önce: %{summary['target_first_pct']:.2f}")
    print(f"Ortalama MFE: %{summary['avg_mfe_pct']:.3f}")
    print(f"Ortalama MAE: %{summary['avg_mae_pct']:.3f}")
    print(f"Ufuk sonu ortalama getiri: %{summary['avg_horizon_close_pct']:.3f}")
    print("\nKurulum türleri:")
    for name, row in summary["by_setup"].items():
        print(f"  {name}: n={row['n']} hedef-önce=%{row['target_first_pct']:.1f} "
              f"MFE=%{row['avg_mfe_pct']:.2f} MAE=%{row['avg_mae_pct']:.2f}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Spot Opportunity Scanner geçmiş testi")
    parser.add_argument("--days", type=int, default=90, help="Test dönemi; varsayılan 90 gün")
    parser.add_argument("--symbols", type=int, default=40, help="Likidite sırasıyla sembol sayısı; 0=tümü")
    parser.add_argument("--only-symbol", default="", help="Yalnız tek sembol; örn. ZECUSDT")
    parser.add_argument("--step-hours", type=int, default=6, help="Karar noktaları arası saat")
    parser.add_argument("--horizon-hours", type=int, default=24, help="Her adayın takip süresi")
    parser.add_argument("--workers", type=int, default=4, help="Veri indirme işçisi")
    parser.add_argument("--output", default="/tmp/spot_opportunity_backtest.json")
    args = parser.parse_args()

    minimum_days = 2 if args.only_symbol else 14
    if args.days < minimum_days or args.step_hours < 1 or args.horizon_hours < 1:
        raise SystemExit(f"days>={minimum_days}, step-hours>=1 ve horizon-hours>=1 olmalı")

    final_end = closed_hour()
    last_cutoff = final_end - timedelta(hours=args.horizon_hours)
    first_cutoff = last_cutoff - timedelta(days=args.days)
    cutoffs = list(pd.date_range(first_cutoff, last_cutoff, freq=f"{args.step_hours}h", tz="UTC").to_pydatetime())
    if args.only_symbol:
        symbol = args.only_symbol.upper().replace("/", "")
        symbol = symbol if symbol.endswith("USDT") else symbol + "USDT"
        symbols = [symbol]
    else:
        symbols = current_crypto_symbols(args.symbols)
    print(f"[TEST] {len(symbols)} sembol | {args.days} gün | {len(cutoffs)} karar noktası")
    print("[TEST] Veriler indiriliyor; canlı tarayıcı ve dış servisler kullanılmaz.")

    all_data: dict[str, dict[str, pd.DataFrame]] = {}
    download_symbols = ["BTCUSDT", *[s for s in symbols if s != "BTCUSDT"]]
    with ThreadPoolExecutor(max_workers=max(1, min(6, args.workers))) as pool:
        jobs = {pool.submit(download_symbol, s, first_cutoff, final_end): s for s in download_symbols}
        for future in as_completed(jobs):
            symbol = jobs[future]
            try:
                key, data = future.result()
                all_data[key] = data
                print(f"[DATA] {key} hazır")
            except Exception as exc:
                print(f"[DATA] {symbol} atlandı: {exc}")

    if "BTCUSDT" not in all_data:
        raise SystemExit("BTC bağlam verisi indirilemedi")

    records: list[dict] = []
    active_events: dict[str, dict] = {}
    for number, cutoff in enumerate(cutoffs, start=1):
        btc = btc_at(all_data["BTCUSDT"], cutoff)
        h1_states: list[tuple[str, dict]] = []
        for symbol in symbols:
            data = all_data.get(symbol)
            if not data:
                continue
            try:
                h1_raw = frame_at(data["1h"], cutoff, 260)
                # Geçmişteki son 24 saatin gerçek quote hacmi; bugünkü ticker kullanılmaz.
                quote_volume = float(h1_raw.tail(24)["quote_volume"].sum())
                if quote_volume < scanner.MIN_QUOTE_VOLUME:
                    continue
                h1_state = scanner.timeframe_state(h1_raw, "1H")
                h1_states.append((symbol, h1_state))
            except Exception:
                continue
        candidates = []
        # Context monkeypatch global olduğu için değerlendirme bu bölümde sıralıdır.
        for symbol, h1_state in h1_states:
            try:
                with FETCH_LOCK, historical_fetch(all_data[symbol], cutoff):
                    candidate = scanner.evaluate_symbol(symbol, h1_state, btc)
                if candidate:
                    candidates.append(candidate)
            except Exception:
                continue
        candidates.sort(key=lambda c: (c.setup, c.symbol))
        next_events = {
            symbol: {**event, "misses": int(event.get("misses", 0)) + 1}
            for symbol, event in active_events.items()
            if int(event.get("misses", 0)) < 1
        }
        for candidate in candidates:
            previous = active_events.get(candidate.symbol, {})
            last_high = float(candidate.metrics.get("last_high_1h", candidate.price))
            last_low = float(candidate.metrics.get("last_low_1h", candidate.price))
            completed = bool(previous) and (
                last_high >= float(previous.get("target", float("inf"))) or
                last_low <= float(previous.get("stop", float("-inf")))
            )
            if completed:
                next_events.pop(candidate.symbol, None)
                continue
            previous_stage = previous.get("stage", "")
            is_new = not previous
            is_upgrade = previous_stage == "EARLY" and candidate.stage == "TURN"
            if previous_stage == "TURN" and candidate.stage == "EARLY":
                next_events[candidate.symbol] = {**previous, "misses": 0}
                continue
            next_events[candidate.symbol] = {
                "stage": candidate.stage, "target": candidate.target_low,
                "stop": candidate.stop, "misses": 0,
            }
            if not (is_new or is_upgrade):
                continue
            measured = outcome(candidate, all_data[candidate.symbol]["1h"], cutoff, args.horizon_hours)
            records.append({
                "time": cutoff.isoformat(), "symbol": candidate.symbol,
                "setup": candidate.setup,
                "stage": candidate.stage,
                "event_key": candidate.event_key,
                "observed_setups": candidate.observed_setups,
                "entry": candidate.price, "stop": candidate.stop,
                "target": candidate.target_low, "target_pct": round(candidate.target_pct, 3),
                "stop_pct": round(candidate.stop_pct, 3), "rr": round(candidate.rr, 3),
                "tf_summary": candidate.tf_summary,
                "positives": candidate.positives, "risks": candidate.risks,
                "historical_notes": candidate.historical_notes,
                "metrics": candidate.metrics,
                **measured,
            })
        active_events = next_events
        if number % 20 == 0 or number == len(cutoffs):
            print(f"[REPLAY] {number}/{len(cutoffs)} | sinyal={len(records)}")

    summary = summarize(records)
    payload = {
        "config": vars(args), "first_cutoff": first_cutoff.isoformat(),
        "last_cutoff": last_cutoff.isoformat(), "symbols_requested": len(symbols),
        "symbols_downloaded": len(all_data) - 1,
        "limitations": [
            "Sembol evreni bugünkü Binance liste durumuna göre kurulur (survivorship bias).",
            "Aynı 1H mumda hedef ve stop görülürse STOP_AMBIGUOUS kabul edilir.",
            "Komisyon ve spread net getiriye uygulanmaz; hedef/stop sırası ölçülür.",
        ],
        "summary": summary, "records": records,
    }
    output = Path(args.output)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print_summary(summary)
    print(f"\nHam sonuç: {output}")


if __name__ == "__main__":
    main()
