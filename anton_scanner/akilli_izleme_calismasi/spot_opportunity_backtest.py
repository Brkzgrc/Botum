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


def download_symbol(
    symbol: str, first_cutoff: datetime, final_end: datetime, cache_dir: str = ""
) -> tuple[str, dict[str, pd.DataFrame]]:
    cache_path = Path(cache_dir) / f"{symbol}.pkl" if cache_dir else None
    if cache_path and cache_path.exists():
        try:
            cached = pd.read_pickle(cache_path)
            if (
                cached.get("first_cutoff") == first_cutoff.isoformat() and
                cached.get("final_end") == final_end.isoformat()
            ):
                return symbol, cached["data"]
        except Exception:
            pass
    data: dict[str, pd.DataFrame] = {}
    for interval, history_limit in FRAME_LIMITS.items():
        warmup = timedelta(milliseconds=INTERVAL_MS[interval] * (history_limit + 8))
        data[interval] = fetch_range(symbol, interval, first_cutoff - warmup, final_end)
    if cache_path:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        pd.to_pickle({
            "first_cutoff": first_cutoff.isoformat(),
            "final_end": final_end.isoformat(),
            "data": data,
        }, cache_path)
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
    d1 = scanner.add_indicators(frame_at(data["1d"], cutoff, 200))
    h1i = scanner.add_indicators(h1)
    h4i = scanner.add_indicators(h4)
    price = float(h1i["close"].iloc[-1])
    h4_up = price > float(h4i["ema20"].iloc[-1]) and float(h4i["ema20"].iloc[-1]) > float(h4i["ema50"].iloc[-1])
    d1_up = float(d1["close"].iloc[-1]) > float(d1["ema20"].iloc[-1]) > float(d1["ema50"].iloc[-1])
    d1_down = float(d1["close"].iloc[-1]) < float(d1["ema20"].iloc[-1]) < float(d1["ema50"].iloc[-1])
    if d1_up and h4_up: regime = "YÜKSELİŞ"
    elif d1_down and not h4_up: regime = "DÜŞÜŞ"
    elif d1_up and not h4_up: regime = "YÜKSELİŞ İÇİ DÜZELTME"
    elif not d1_up and h4_up: regime = "TOPARLANMA"
    else: regime = "YATAY/GEÇİŞ"
    ret7d = scanner._pct_change(h4i["close"], 42)
    if regime == "YÜKSELİŞ" and ret7d >= 14 and scanner._pct_change(h1i["close"], 24) < 1:
        regime = "YÜKSELİŞ SONU YORGUNLUK"
    return {
        "ret_1h": (h1["close"].iloc[-1] / h1["close"].iloc[-2] - 1) * 100,
        "ret_6h": (h1["close"].iloc[-1] / h1["close"].iloc[-7] - 1) * 100,
        "ret_24h": (h1["close"].iloc[-1] / h1["close"].iloc[-25] - 1) * 100,
        "ret_4h": (h4["close"].iloc[-1] / h4["close"].iloc[-2] - 1) * 100,
        "ret_7d": ret7d,
        "regime": regime,
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
    event_pos = None
    for i, row in enumerate(future.itertuples(), start=1):
        hit_stop = row.low <= candidate.stop
        hit_target = row.high >= candidate.target_low
        if hit_stop and hit_target:
            result, hours, event_pos = "STOP_AMBIGUOUS", i, i - 1
            break
        if hit_stop:
            result, hours, event_pos = "STOP", i, i - 1
            break
        if hit_target:
            result, hours, event_pos = "TARGET", i, i - 1
            break
    if future.empty:
        return {"result": "NO_DATA", "hours": None, "mfe": 0, "mae": 0, "close_return": 0}
    mfe = (future["high"].max() / candidate.price - 1) * 100
    mae = (future["low"].min() / candidate.price - 1) * 100
    close_return = (future["close"].iloc[-1] / candidate.price - 1) * 100
    close_returns = (future["close"] / candidate.price - 1) * 100
    best_close_return = float(close_returns.max())
    trail_return = float(close_returns.iloc[-1])
    running_peak = float(close_returns.iloc[0])
    for value in close_returns.iloc[1:]:
        running_peak = max(running_peak, float(value))
        if running_peak > 0 and running_peak - float(value) >= 2.5:
            trail_return = float(value)
            break
    missed_after_event = 0.0
    if event_pos is not None and event_pos + 1 < len(future):
        after_peak = (future.iloc[event_pos + 1:]["high"].max() / candidate.price - 1) * 100
        realized = candidate.target_pct if result == "TARGET" else -candidate.stop_pct
        missed_after_event = max(0.0, float(after_peak) - float(realized))
    return {
        "result": result, "hours": hours,
        "mfe": round(float(mfe), 3), "mae": round(float(mae), 3),
        "close_return": round(float(close_return), 3),
        "best_close_return": round(best_close_return, 3),
        "trail_2_5_close_return": round(trail_return, 3),
        "missed_after_event": round(missed_after_event, 3),
    }


def smart_watch_outcome(candidate, h1: pd.DataFrame, btc_h1: pd.DataFrame, cutoff: datetime) -> dict:
    """İzleme kalitesini 24/48/72 saatte, geleceği birbirine karıştırmadan ölçer."""
    result: dict = {}
    for hours in (24, 48, 72):
        measured = outcome(candidate, h1, cutoff, hours)
        btc_future = btc_h1[btc_h1["open_time"] >= cutoff].head(hours)
        btc_before = btc_h1[btc_h1["open_time"] <= cutoff]
        if not btc_future.empty and not btc_before.empty:
            btc_return = (float(btc_future["close"].iloc[-1]) / float(btc_before["close"].iloc[-1]) - 1) * 100
            measured["btc_close_return"] = round(btc_return, 3)
            measured["relative_to_btc"] = round(measured["close_return"] - btc_return, 3)
        else:
            measured["btc_close_return"] = 0.0
            measured["relative_to_btc"] = measured["close_return"]
        result[f"h{hours}"] = measured
    return result


def advance_idea_lifecycle(idea: dict, bar_low: float, bar_high: float) -> str | None:
    """Aktif fikrin tek kapalı mumdaki kronolojik olarak çözülebilen sonucunu döndürür."""
    if idea.get("lifecycle") != "TAŞI":
        return None
    hit_stop = float(bar_low) <= float(idea["stop"])
    hit_target = float(bar_high) >= float(idea["target"])
    if hit_stop and hit_target:
        idea["lifecycle"] = "FİKİR BOZULDU"
        return "HEDEF_STOP_AYNI_MUM"
    if hit_stop:
        idea["lifecycle"] = "FİKİR BOZULDU"
        return "FİKİR BOZULDU"
    if hit_target:
        idea["lifecycle"] = "ÇIKIŞ"
        return "ÇIKIŞ"
    return None


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


def summarize_smart(records: list[dict], lifecycle_events: list[dict] | None = None) -> dict:
    def horizon(group: list[dict], key: str) -> dict:
        rows = [r[key] for r in group if key in r]
        counts = Counter(x["result"] for x in rows)
        decided = counts["TARGET"] + counts["STOP"] + counts["STOP_AMBIGUOUS"]
        return {
            "n": len(rows), "outcomes": dict(counts),
            "target_first_pct": round(100 * counts["TARGET"] / decided, 2) if decided else 0,
            "avg_mfe_pct": safe_mean([x["mfe"] for x in rows]),
            "avg_mae_pct": safe_mean([x["mae"] for x in rows]),
            "avg_close_pct": safe_mean([x["close_return"] for x in rows]),
            "avg_best_close_pct": safe_mean([x.get("best_close_return", 0) for x in rows]),
            "avg_trail_2_5_pct": safe_mean([x.get("trail_2_5_close_return", 0) for x in rows]),
            "avg_missed_after_event_pct": safe_mean([x.get("missed_after_event", 0) for x in rows]),
            "avg_relative_to_btc_pct": safe_mean([x.get("relative_to_btc", 0) for x in rows]),
        }

    actions = {}
    for action in sorted({r["action_state"] for r in records}):
        group = [r for r in records if r["action_state"] == action]
        actions[action] = {
            "count": len(group),
            "h24": horizon(group, "h24"),
            "h48": horizon(group, "h48"),
            "h72": horizon(group, "h72"),
        }
    triggers = [r for r in records if r.get("record_kind") == "TRIGGER"]
    lifecycle_events = lifecycle_events or []
    return {
        "observations": len(records),
        "triggers": len(triggers),
        "reentries": sum(bool(r.get("is_reentry")) for r in records),
        "lifecycle": dict(Counter(x.get("event", "BELİRSİZ") for x in lifecycle_events)),
        "all": {key: horizon(records, key) for key in ("h24", "h48", "h72")},
        "trigger_only": {key: horizon(triggers, key) for key in ("h24", "h48", "h72")},
        "by_action": actions,
    }


def print_smart_summary(summary: dict) -> None:
    print("\n" + "=" * 72)
    print("AKILLI İZLEME — WALK-FORWARD SONUÇ")
    print("=" * 72)
    print(f"Değişen hikâye gözlemi: {summary['observations']}")
    print(f"Giriş tetiği: {summary['triggers']} | Yeniden giriş: {summary['reentries']}")
    print(f"Fikir yaşam döngüsü: {summary.get('lifecycle', {})}")
    for key, label in (("h24", "24S"), ("h48", "48S"), ("h72", "72S")):
        row = summary["all"][key]
        print(
            f"{label}: MFE %{row['avg_mfe_pct']:.2f} | MAE %{row['avg_mae_pct']:.2f} | "
            f"kapanış %{row['avg_close_pct']:.2f} | BTC farkı %{row['avg_relative_to_btc_pct']:.2f} | "
            f"taşınabilir(2,5) %{row['avg_trail_2_5_pct']:.2f} | hedef-önce %{row['target_first_pct']:.1f}"
        )
    print("\nKarar durumları (72S):")
    for action, data in summary["by_action"].items():
        row = data["h72"]
        print(
            f"  {action}: n={data['count']} | MFE %{row['avg_mfe_pct']:.2f} | "
            f"MAE %{row['avg_mae_pct']:.2f} | kapanış %{row['avg_close_pct']:.2f}"
        )


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
    parser.add_argument("--cache-dir", default="/tmp/spot_watch_cache", help="İndirilen OHLCV önbelleği")
    parser.add_argument("--progress-file", default="/tmp/spot_watch_progress.json", help="Tek satırlık ilerleme dosyası")
    parser.add_argument(
        "--checkpoint-dir", default="",
        help="Tamamlanan sembol gruplarını saklar; boşsa output yanında .checkpoints",
    )
    parser.add_argument(
        "--no-resume", action="store_true",
        help="Var olan uyumlu checkpoint'leri kullanmadan baştan hesapla",
    )
    args = parser.parse_args()

    progress_path = Path(args.progress_file)
    output_path = Path(args.output)
    checkpoint_path = Path(args.checkpoint_dir) if args.checkpoint_dir else Path(str(output_path) + ".checkpoints")
    progress_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint_path.mkdir(parents=True, exist_ok=True)

    def atomic_json(path: Path, payload: dict) -> None:
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(path)

    def write_progress(stage: str, **extra) -> None:
        atomic_json(progress_path, {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "stage": stage,
            **extra,
        })

    minimum_days = 2 if args.only_symbol else 3
    if args.days < minimum_days or args.step_hours < 1 or args.horizon_hours < 1:
        raise SystemExit(f"days>={minimum_days}, step-hours>=1 ve horizon-hours>=1 olmalı")

    final_end = closed_hour()
    evaluation_horizon = 72 if scanner.SMART_WATCH_ENABLED else args.horizon_hours
    last_cutoff = final_end - timedelta(hours=evaluation_horizon)
    first_cutoff = last_cutoff - timedelta(days=args.days)
    if args.only_symbol:
        symbol = args.only_symbol.upper().replace("/", "")
        symbol = symbol if symbol.endswith("USDT") else symbol + "USDT"
        symbols = [symbol]
    else:
        symbols = current_crypto_symbols(args.symbols)

    manifest_path = checkpoint_path / "run_manifest.json"
    requested_config = {
        "days": args.days,
        "symbols_limit": args.symbols,
        "only_symbol": args.only_symbol,
        "step_hours": args.step_hours,
        "horizon_hours": evaluation_horizon,
        "batch_size": max(1, min(25, args.batch_size)),
        "smart_watch": bool(scanner.SMART_WATCH_ENABLED),
    }
    if manifest_path.exists() and not args.no_resume:
        saved = json.loads(manifest_path.read_text(encoding="utf-8"))
        if saved.get("config") != requested_config:
            raise SystemExit(
                "Checkpoint ayarları bu komutla uyuşmuyor. Farklı bir --checkpoint-dir "
                "kullanın veya bilinçli olarak --no-resume verin."
            )
        symbols = list(saved["symbols"])
        first_cutoff = datetime.fromisoformat(saved["first_cutoff"])
        last_cutoff = datetime.fromisoformat(saved["last_cutoff"])
        final_end = datetime.fromisoformat(saved["final_end"])
        print(f"[RESUME] Aynı koşu korunuyor | {len(symbols)} sembol")
    else:
        atomic_json(manifest_path, {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "config": requested_config,
            "symbols": symbols,
            "first_cutoff": first_cutoff.isoformat(),
            "last_cutoff": last_cutoff.isoformat(),
            "final_end": final_end.isoformat(),
        })

    cutoffs = list(pd.date_range(first_cutoff, last_cutoff, freq=f"{args.step_hours}h", tz="UTC").to_pydatetime())
    print(f"[TEST] {len(symbols)} sembol | {args.days} gün | {len(cutoffs)} karar noktası")
    print("[TEST] Veriler indiriliyor; canlı tarayıcı ve dış servisler kullanılmaz.")
    write_progress("START", symbols=len(symbols), cutoffs=len(cutoffs))

    # BTC bağlamı bütün gruplarda ortaktır; yalnız bir kez bellekte tutulur.
    try:
        _, btc_data = download_symbol("BTCUSDT", first_cutoff, final_end, args.cache_dir)
        print("[DATA] BTCUSDT hazır")
        write_progress("BTC_READY", symbols=len(symbols), cutoffs=len(cutoffs))
    except Exception as exc:
        raise SystemExit(f"BTC bağlam verisi indirilemedi: {exc}")

    batch_size = max(1, min(25, args.batch_size))
    batches = [
        symbols[i:i + batch_size]
        for i in range(0, len(symbols), batch_size)
    ]
    records: list[dict] = []
    lifecycle_events: list[dict] = []
    downloaded_count = 0

    for batch_no, batch_symbols in enumerate(batches, start=1):
        batch_checkpoint = checkpoint_path / f"batch_{batch_no:03d}.json"
        if batch_checkpoint.exists() and not args.no_resume:
            saved_batch = json.loads(batch_checkpoint.read_text(encoding="utf-8"))
            if saved_batch.get("symbols") != batch_symbols:
                raise SystemExit(f"Checkpoint grup {batch_no} sembolleri uyuşmuyor")
            batch_records = saved_batch.get("records", [])
            records.extend(batch_records)
            lifecycle_events.extend(saved_batch.get("lifecycle_events", []))
            downloaded_count += int(saved_batch.get("symbols_downloaded", len(batch_symbols)))
            print(
                f"[RESUME] BATCH {batch_no}/{len(batches)} atlandı | "
                f"kayıt={len(batch_records)}"
            )
            write_progress(
                "BATCH_RESUMED", batch=batch_no, batches=len(batches),
                observations=len(records),
            )
            continue

        batch_record_start = len(records)
        batch_lifecycle_start = len(lifecycle_events)
        batch_download_start = downloaded_count
        print(
            f"\n[BATCH] {batch_no}/{len(batches)} | "
            f"{len(batch_symbols)} sembol yükleniyor"
        )
        write_progress(
            "BATCH_DOWNLOAD", batch=batch_no, batches=len(batches),
            observations=len(records),
        )
        all_data: dict[str, dict[str, pd.DataFrame]] = {"BTCUSDT": btc_data}
        to_download = [symbol for symbol in batch_symbols if symbol != "BTCUSDT"]
        with ThreadPoolExecutor(max_workers=max(1, min(4, args.workers))) as pool:
            jobs = {
                pool.submit(download_symbol, symbol, first_cutoff, final_end, args.cache_dir): symbol
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

            # Daha önce tetiklenen fikirlerin sonraki kapalı 1H mumda hedefe
            # mi geçersizlik seviyesine mi önce ulaştığını saat saat izle.
            # Aynı mumda ikisi de görülürse sırayı uydurmak yerine belirsiz yaz.
            h1_state_map = {symbol: state for symbol, state in h1_states}
            for symbol, idea in list(market_states.items()):
                if idea.get("lifecycle") != "TAŞI":
                    continue
                current = h1_state_map.get(symbol)
                if not current:
                    continue
                last_bar = current["df"].iloc[-1]
                event = advance_idea_lifecycle(
                    idea, float(last_bar["low"]), float(last_bar["high"])
                )
                if not event:
                    continue
                lifecycle_events.append({
                    "time": cutoff.isoformat(), "symbol": symbol,
                    "event": event, "entry": idea.get("entry"),
                    "stop": idea.get("stop"), "target": idea.get("target"),
                    "is_reentry_cycle": bool(idea.get("trigger_count", 0) > 1),
                })

            candidates = []

            def evaluate_at_cutoff(item: tuple[str, dict]):
                symbol, h1_state = item
                data = all_data[symbol]
                overrides = {
                    label: scanner.timeframe_state(
                        frame_at(data[interval], cutoff, FRAME_LIMITS[interval]), label
                    )
                    for interval, label in (("4h", "4H"), ("1d", "1D"), ("1w", "1W"))
                }
                return scanner.evaluate_symbol(symbol, h1_state, btc, overrides)

            with ThreadPoolExecutor(max_workers=max(1, min(8, args.workers))) as pool:
                jobs = [pool.submit(evaluate_at_cutoff, item) for item in h1_states]
                for future in as_completed(jobs):
                    try:
                        candidate = future.result()
                        if candidate:
                            candidates.append(candidate)
                    except Exception:
                        continue

            candidates.sort(key=lambda candidate: (candidate.setup, candidate.symbol))
            if scanner.SMART_WATCH_ENABLED:
                selected = scanner.select_smart_watchlist(candidates)
                selected_symbols = {c.symbol for c in selected}
                for symbol, previous in market_states.items():
                    if symbol not in selected_symbols and isinstance(previous, dict):
                        previous["last_action"] = "LİSTE DIŞI"

                for candidate in selected:
                    previous = market_states.get(candidate.symbol, {})
                    fingerprint = scanner.story_fingerprint(candidate)
                    last_action = previous.get("last_action", "")
                    changed = fingerprint != previous.get("last_fingerprint", "")
                    # Aynı hikâye değişmediyse her saat yeni gözlem üretme.
                    if not changed:
                        continue
                    is_trigger = candidate.action_state == "GİRİŞE HAZIR"
                    is_reentry = bool(
                        is_trigger and previous.get("ever_triggered") and
                        previous.get("lifecycle") in {"ÇIKIŞ", "FİKİR BOZULDU"}
                    )
                    measured = smart_watch_outcome(
                        candidate, all_data[candidate.symbol]["1h"],
                        btc_data["1h"], cutoff,
                    )
                    records.append({
                        "time": cutoff.isoformat(), "symbol": candidate.symbol,
                        "record_kind": "TRIGGER" if is_trigger else "WATCH",
                        "is_reentry": is_reentry,
                        "action_state": candidate.action_state,
                        "story_score": candidate.story_score,
                        "extension_state": candidate.extension_state,
                        "market_regime": candidate.market_regime,
                        "setup": candidate.setup,
                        "entry": candidate.price, "stop": candidate.stop,
                        "target": candidate.target_low,
                        "target_pct": round(candidate.target_pct, 3),
                        "stop_pct": round(candidate.stop_pct, 3),
                        "rr": round(candidate.rr, 3),
                        "price_story": candidate.price_story,
                        "trigger_text": candidate.trigger_text,
                        "invalidation_text": candidate.invalidation_text,
                        "metrics": candidate.metrics,
                        **measured,
                    })
                    market_states[candidate.symbol] = {
                        "last_fingerprint": fingerprint,
                        "last_action": candidate.action_state,
                        "ever_triggered": bool(previous.get("ever_triggered") or is_trigger),
                        "lifecycle": "TAŞI" if is_trigger else previous.get("lifecycle", "İZLE"),
                        "entry": candidate.price if is_trigger else previous.get("entry"),
                        "stop": candidate.stop if is_trigger else previous.get("stop"),
                        "target": candidate.target_low if is_trigger else previous.get("target"),
                        "trigger_count": int(previous.get("trigger_count", 0)) + (1 if is_trigger else 0),
                    }
                    if is_trigger:
                        lifecycle_events.append({
                            "time": cutoff.isoformat(), "symbol": candidate.symbol,
                            "event": "YENİDEN GİRİŞE HAZIR" if is_reentry else "GİRİŞE HAZIR",
                            "entry": candidate.price, "stop": candidate.stop,
                            "target": candidate.target_low,
                            "is_reentry_cycle": is_reentry,
                        })

                if number % 20 == 0 or number == len(cutoffs):
                    print(
                        f"[SMART REPLAY B{batch_no}] {number}/{len(cutoffs)} | "
                        f"gözlem={len(records)}"
                    )
                    write_progress(
                        "REPLAY", batch=batch_no, batches=len(batches),
                        cutoff=number, cutoffs=len(cutoffs), observations=len(records),
                    )
                continue

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
        atomic_json(batch_checkpoint, {
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "batch": batch_no,
            "symbols": batch_symbols,
            "symbols_downloaded": downloaded_count - batch_download_start,
            "records": records[batch_record_start:],
            "lifecycle_events": lifecycle_events[batch_lifecycle_start:],
        })
        write_progress(
            "BATCH_COMPLETED", batch=batch_no, batches=len(batches),
            observations=len(records), checkpoint=str(batch_checkpoint),
        )

    summary = summarize_smart(records, lifecycle_events) if scanner.SMART_WATCH_ENABLED else summarize(records)
    payload = {
        "config": vars(args), "first_cutoff": first_cutoff.isoformat(),
        "last_cutoff": last_cutoff.isoformat(), "symbols_requested": len(symbols),
        "symbols_downloaded": downloaded_count,
        "limitations": [
            "Sembol evreni bugünkü Binance liste durumuna göre kurulur (survivorship bias).",
            "Aynı 1H mumda hedef ve stop görülürse STOP_AMBIGUOUS kabul edilir.",
            "Komisyon ve spread net getiriye uygulanmaz; hedef/stop sırası ölçülür.",
            "Akıllı izleme replay'i yalnız karar anında kapanmış mumları kullanır; 24/48/72 saat sonuçları sonradan ölçülür.",
        ],
        "summary": summary, "records": records,
        "lifecycle_events": lifecycle_events,
    }
    atomic_json(output_path, payload)
    write_progress("COMPLETED", output=str(output_path), observations=len(records))
    if scanner.SMART_WATCH_ENABLED:
        print_smart_summary(summary)
    else:
        print_summary(summary)
    print(f"\nHam sonuç: {output_path}")


if __name__ == "__main__":
    main()
