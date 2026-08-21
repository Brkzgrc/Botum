# -*- coding: utf-8 -*-
"""SPOT Opportunity Scanner için geçmişe dönük walk-forward doğrulama.

Canlı tarayıcıyı değiştirmez ve emir üretmez. Karar anında yalnızca kapanmış
mumları gösterir; sonraki 1H mumlarla hedef/stop sonucunu ölçer.
"""

from __future__ import annotations

import argparse
import gc
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
    parser.add_argument("--workers", type=int, default=3, help="Veri indirme işçisi")
    parser.add_argument(
        "--batch-size", type=int, default=15,
        help="RAM kullanımını sınırlamak için aynı anda tutulacak sembol sayısı",
    )
    parser.add_argument(
        "--trace-hours", type=int, default=0,
        help="Son N karar saatinde aday/geçiş teşhisini yazdır; 0=kapalı",
    )
    parser.add_argument("--output", default="/tmp/spot_opportunity_backtest.json")
    args = parser.parse_args()

    minimum_days = 2 if args.only_symbol else 3
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

    # BTC bağlamı bütün gruplarda ortaktır; yalnız bir kez bellekte tutulur.
    try:
        _, btc_data = download_symbol("BTCUSDT", first_cutoff, final_end)
        print("[DATA] BTCUSDT hazır")
    except Exception as exc:
        raise SystemExit(f"BTC bağlam verisi indirilemedi: {exc}")

    batch_size = max(1, min(25, args.batch_size))
    batches = [
        symbols[i:i + batch_size]
        for i in range(0, len(symbols), batch_size)
    ]
    records: list[dict] = []
    downloaded_count = 0

    for batch_no, batch_symbols in enumerate(batches, start=1):
        print(
            f"\n[BATCH] {batch_no}/{len(batches)} | "
            f"{len(batch_symbols)} sembol yükleniyor"
        )
        all_data: dict[str, dict[str, pd.DataFrame]] = {"BTCUSDT": btc_data}
        to_download = [symbol for symbol in batch_symbols if symbol != "BTCUSDT"]
        with ThreadPoolExecutor(max_workers=max(1, min(4, args.workers))) as pool:
            jobs = {
                pool.submit(download_symbol, symbol, first_cutoff, final_end): symbol
                for symbol in to_download
            }
            for future in as_completed(jobs):
                symbol = jobs[future]
                try:
                    key, data = future.result()
                    all_data[key] = data
                    downloaded_count += 1
                    print(f"[DATA] {key} hazır")
                except Exception as exc:
                    print(f"[DATA] {symbol} atlandı: {exc}")
        if "BTCUSDT" in batch_symbols:
            downloaded_count += 1

        # Durum yaşam döngüsü her sembol için bağımsızdır. Seçici artık
        # çapraz-sembol yüzdelik sıralaması kullanmadığından gruplama sonucu
        # değiştirmez; yalnız RAM tüketimini sınırlar.
        market_states: dict[str, dict] = {}
        last_cycle_at: dict[str, datetime] = {}

        for number, cutoff in enumerate(cutoffs, start=1):
            btc = btc_at(btc_data, cutoff)
            h1_states: list[tuple[str, dict]] = []
            for symbol in batch_symbols:
                data = all_data.get(symbol)
                if not data:
                    continue
                try:
                    h1_raw = frame_at(data["1h"], cutoff, 260)
                    quote_volume = float(h1_raw.tail(24)["quote_volume"].sum())
                    if quote_volume < scanner.MIN_QUOTE_VOLUME:
                        continue
                    h1_state = scanner.timeframe_state(h1_raw, "1H")
                    h1_states.append((symbol, h1_state))
                except Exception:
                    continue

            candidates = []
            # Context monkeypatch global olduğu için değerlendirme sıralıdır.
            for symbol, h1_state in h1_states:
                try:
                    with FETCH_LOCK, historical_fetch(all_data[symbol], cutoff):
                        candidate = scanner.evaluate_symbol(symbol, h1_state, btc)
                    if candidate:
                        candidates.append(candidate)
                except Exception:
                    continue

            candidates.sort(key=lambda candidate: (candidate.setup, candidate.symbol))
            candidate_debug: dict[str, dict] = {}
            h1_map = {symbol: state for symbol, state in h1_states}
            next_states = {
                symbol: {
                    "market": scanner.compact_market_state(h1_state),
                    "candidate_stage": "",
                    "last_event_id": market_states.get(symbol, {}).get("last_event_id", ""),
                }
                for symbol, h1_state in h1_states
            }
            event_pool: list[tuple] = []

            for candidate in candidates:
                previous = market_states.get(candidate.symbol, {})
                current = next_states[candidate.symbol]["market"]
                ready, transition_reasons = scanner.transition_ready(
                    previous, candidate, current
                )
                next_states[candidate.symbol]["candidate_stage"] = candidate.stage
                last_time = last_cycle_at.get(candidate.symbol)
                rearmed = (
                    last_time is None or
                    (cutoff - last_time).total_seconds() >=
                    scanner.EVENT_REARM_HOURS * 3600
                )
                candidate_debug[candidate.symbol] = {
                    "setup": candidate.setup,
                    "stage": candidate.stage,
                    "ready": ready,
                    "rearmed": rearmed,
                    "selected": False,
                    "reasons": transition_reasons,
                    "previous_stage": previous.get("candidate_stage", ""),
                    "selector_gates": {
                        "support_role": candidate.metrics.get("support_role_state"),
                        "support_width": candidate.metrics.get("support_zone_width_pct"),
                        "support_tf": candidate.metrics.get("support_timeframe_count"),
                        "support_strength": candidate.metrics.get("support_strength"),
                        "support_distance": candidate.metrics.get("support_distance_pct"),
                        "target_pct": candidate.target_pct,
                        "family_count": candidate.metrics.get("confirmation_family_count"),
                        "families": candidate.metrics.get("confirmation_families", []),
                        "metric_fresh": candidate.metrics.get("h1_fresh_turn_count"),
                        "metric_up": candidate.metrics.get("h1_upward_count"),
                        "metric_weak": candidate.metrics.get("h1_weakening_count"),
                    },
                }
                if not market_states or not ready or not rearmed:
                    continue
                event_pool.append((candidate, transition_reasons))

            selected_events = scanner.select_distinct_events(event_pool, h1_map)
            for candidate, transition_reasons in selected_events:
                candidate_debug[candidate.symbol]["selected"] = True
                last_cycle_at[candidate.symbol] = cutoff
                next_states[candidate.symbol]["last_event_id"] = (
                    next_states[candidate.symbol]["market"].get(
                        "structure_event_id", ""
                    )
                )
                candidate.observed_setups.insert(
                    0, "Saatlik ilerleme: " + "; ".join(transition_reasons)
                )
                measured = outcome(
                    candidate, all_data[candidate.symbol]["1h"],
                    cutoff, args.horizon_hours
                )
                records.append({
                    "time": cutoff.isoformat(), "symbol": candidate.symbol,
                    "setup": candidate.setup,
                    "stage": candidate.stage,
                    "event_key": candidate.event_key,
                    "observed_setups": candidate.observed_setups,
                    "entry": candidate.price, "stop": candidate.stop,
                    "target": candidate.target_low,
                    "target_pct": round(candidate.target_pct, 3),
                    "stop_pct": round(candidate.stop_pct, 3),
                    "rr": round(candidate.rr, 3),
                    "tf_summary": candidate.tf_summary,
                    "positives": candidate.positives,
                    "risks": candidate.risks,
                    "historical_notes": candidate.historical_notes,
                    "metrics": candidate.metrics,
                    "transition_reasons": transition_reasons,
                    **measured,
                })

            if (
                args.trace_hours > 0 and
                cutoff >= last_cutoff - timedelta(hours=args.trace_hours)
            ):
                tr_time = cutoff.astimezone(scanner.TR_TZ).strftime(
                    "%Y-%m-%d %H:%M"
                )
                for symbol, current_state in next_states.items():
                    market = current_state["market"]
                    debug = candidate_debug.get(symbol)
                    if debug:
                        print(
                            f"[TRACE] {tr_time} {symbol} | "
                            f"aday={debug['setup']} stage={debug['stage']} "
                            f"prev={debug['previous_stage'] or '-'} "
                            f"ready={debug['ready']} rearm={debug['rearmed']} "
                            f"selected={debug['selected']} "
                            f"up={market['upward_count']} "
                            f"fresh={market['fresh_count']} "
                            f"weak={market['weakening_count']} "
                            f"low={market['relative_low_count']} "
                            f"price={market['price']:.8g} | "
                            f"neden={debug['reasons'] or '-'} | "
                            f"seçim_kapıları={debug['selector_gates']}"
                        )
                    else:
                        print(
                            f"[TRACE] {tr_time} {symbol} | ADAY_YOK "
                            f"up={market['upward_count']} "
                            f"fresh={market['fresh_count']} "
                            f"weak={market['weakening_count']} "
                            f"low={market['relative_low_count']} "
                            f"price={market['price']:.8g}"
                        )

            market_states = next_states
            if number % 20 == 0 or number == len(cutoffs):
                print(
                    f"[REPLAY B{batch_no}] {number}/{len(cutoffs)} | "
                    f"toplam sinyal={len(records)}"
                )

        # Bu grubun veri çerçevelerini sonraki gruptan önce serbest bırak.
        del all_data, market_states, last_cycle_at
        gc.collect()
        print(
            f"[BATCH] {batch_no}/{len(batches)} tamamlandı | "
            f"toplam sinyal={len(records)}"
        )

    summary = summarize(records)
    payload = {
        "config": vars(args), "first_cutoff": first_cutoff.isoformat(),
        "last_cutoff": last_cutoff.isoformat(), "symbols_requested": len(symbols),
        "symbols_downloaded": downloaded_count,
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
