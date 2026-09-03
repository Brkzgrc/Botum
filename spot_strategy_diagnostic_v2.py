# -*- coding: utf-8 -*-
"""V1 vs V2 hizli strateji diagnostigi.

Canli scanner'i DEGISTIRMEZ.
Ayni tarihsel watch adaylarini iki execution mantigiyla yan yana olcer:
- V1: mevcut scanner mantigi
- V2: 3-4 mum setup penceresi, mikro invalidation stopu, anlamli direncleri hedefleme

Ornek:
  python spot_strategy_diagnostic_v2.py --days 14 --symbols 20 --stride 4
"""
from __future__ import annotations

import argparse
import statistics
from collections import Counter
from datetime import timedelta
from typing import Any

import pandas as pd

import spot_opportunity_scanner as scanner
import spot_opportunity_backtest as bt
import spot_strategy_diagnostic as v1


def pctile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    x = (len(s) - 1) * q
    lo = int(x)
    hi = min(lo + 1, len(s) - 1)
    f = x - lo
    return s[lo] * (1 - f) + s[hi] * f


def stats(values: list[float]) -> str:
    if not values:
        return "veri yok"
    return (
        f"n={len(values)} med={statistics.median(values):.2f} "
        f"p75={pctile(values, .75):.2f} p90={pctile(values, .90):.2f} max={max(values):.2f}"
    )


def meaningful_target(h1: pd.DataFrame, m15: pd.DataFrame, price: float, min_pct: float = 1.5) -> float | None:
    """En yakin her pivot yerine, en az min_pct uzaktaki gercek pivot direncini secer."""
    levels: list[float] = []
    for df, lookback in ((m15, 180), (h1, 180)):
        d = df.tail(lookback).reset_index(drop=True)
        for idx in scanner.pivot_indices(d, "high", 2):
            level = scanner.safe_float(d.iloc[idx]["high"])
            if level > price and scanner.pct_change(level, price) >= min_pct:
                levels.append(level)
    if not levels:
        return None
    levels.sort()
    # Birbirine cok yakin mikro pivotlardan daha anlamli olani secmek icin
    # ilk seviyenin hemen ustunde bir kume varsa kume merkezini kullan.
    first = levels[0]
    near = [x for x in levels if x <= first * 1.008][:8]
    return float(sum(near) / len(near)) if near else first


def setup_v2(m15: dict[str, Any]) -> tuple[list[str], float | None]:
    """Setup'i tek son muma kilitlemek yerine son 3-4 mumluk olay zinciri olarak okur."""
    d = m15["df"].copy().reset_index(drop=True)
    if len(d) < 40:
        return [], None
    price = m15["price"]
    ema20 = m15["ema20"]
    ema50 = m15["ema50"]
    atr = max(m15["atr"], price * 0.0015)
    setups: list[str] = []
    breakout_level: float | None = None

    # Son 4 mumun her biri icin, kendisinden onceki 8 mumun tepesini asmis mi?
    breakout_events: list[tuple[int, float]] = []
    for i in range(max(10, len(d) - 4), len(d)):
        prior = d.iloc[max(0, i - 8):i]
        if prior.empty:
            continue
        level = scanner.safe_float(prior["high"].max())
        row = d.iloc[i]
        bullish = scanner.safe_float(row["close"]) > scanner.safe_float(row["open"])
        if level > 0 and bullish and scanner.safe_float(row["close"]) > level:
            breakout_events.append((i, level))

    if breakout_events:
        idx, breakout_level = breakout_events[-1]
        post = d.iloc[idx:]
        held = scanner.safe_float(post["close"].iloc[-1]) >= breakout_level * 0.995
        if held:
            setups.append("MSS_WINDOW")
        if len(post) >= 2:
            touched = scanner.safe_float(post["low"].min()) <= breakout_level * 1.005
            reclaimed = price > breakout_level * 0.998
            if touched and reclaimed:
                setups.append("RETEST_WINDOW")

    # Pullback: son 5 mumda EMA20 civarina temas + trend korunumu + yeniden yukari ivme.
    recent = d.tail(5)
    touched_ema = scanner.safe_float(recent["low"].min()) <= ema20 * 1.006
    momentum_back = price > scanner.safe_float(d["close"].iloc[-2]) and price > ema20
    if touched_ema and price > ema20 > ema50 and momentum_back:
        setups.append("PULLBACK_WINDOW")

    # Sikisma/genisleme: son 3 mumdan herhangi biri genisleme mumu olabilir.
    older_ranges = (d["high"] - d["low"]).iloc[-30:-8]
    recent_base = (d["high"] - d["low"]).iloc[-8:-3]
    compressed = (
        not older_ranges.empty and not recent_base.empty and
        scanner.safe_float(recent_base.median()) <= scanner.safe_float(older_ranges.median()) * 0.80
    )
    if compressed:
        for i in range(max(2, len(d) - 3), len(d)):
            row = d.iloc[i]
            rng = max(scanner.safe_float(row["high"]) - scanner.safe_float(row["low"]), 1e-12)
            body = scanner.safe_float(row["close"]) - scanner.safe_float(row["open"])
            vol_med = scanner.safe_float(d["volume"].iloc[max(0, i-20):i].median(), 1.0)
            vol_ratio = scanner.safe_float(row["volume"]) / max(vol_med, 1e-12)
            prior_high = scanner.safe_float(d["high"].iloc[max(0, i-8):i].max())
            if body > 0 and body / rng >= 0.50 and vol_ratio >= 1.4 and scanner.safe_float(row["close"]) > prior_high:
                setups.append("EXPANSION_WINDOW")
                break

    # Tekrarlari kaldir.
    setups = list(dict.fromkeys(setups))
    return setups, breakout_level


def execution_probe_v2(item: Any, btc: dict[str, Any]) -> tuple[str, float | None, dict[str, Any]]:
    if btc["regime"] == "RED":
        return "BTC_RED", None, {}

    h1 = scanner.h1_state(scanner.fetch_ohlcv(item.symbol, "1h", 220))
    m15 = scanner.m15_state(scanner.fetch_ohlcv(item.symbol, "15m", 220))
    d = m15["df"]
    price = m15["price"]
    atr = max(m15["atr"], price * 0.0015)
    support = scanner.merge_supports(h1["df"], d)
    if not support:
        return "M15_ZONE_YOK", None, {}

    setups, breakout_level = setup_v2(m15)
    if not setups:
        return "M15_SETUP_YOK", None, {"setups": []}

    score = item.watch_score * 0.45
    if "MSS_WINDOW" in setups:
        score += 10
    if "RETEST_WINDOW" in setups:
        score += 9
    if "PULLBACK_WINDOW" in setups:
        score += 8
    if "EXPANSION_WINDOW" in setups:
        score += 8

    if m15["vol_ratio"] >= 2.0:
        score += 10
    elif m15["vol_ratio"] >= 1.4:
        score += 7
    elif m15["vol_ratio"] < 0.8:
        score -= 4
    if m15["taker_buy_ratio"] >= 0.55:
        score += 2
    if 48 <= m15["rsi"] <= 72:
        score += 5
    elif m15["rsi"] > 80:
        score -= 8
    stoch_cross = m15["stoch_k"] > m15["stoch_d"] and m15["stoch_prev_k"] <= m15["stoch_prev_d"]
    if stoch_cross:
        score += 2
    if m15["macd_hist"] > m15["macd_hist_prev"]:
        score += 3
    ema_distance_atr = (price - m15["ema20"]) / max(atr, 1e-12)
    if ema_distance_atr > 2.4:
        score -= 10

    if btc["regime"] == "YELLOW":
        if item.relative_strength_1h < 0.35 or item.relative_strength_4h < 0.75:
            return "BTC_YELLOW_RS", score, {"setups": setups}
        score -= 3

    # V2 STOP: 1H destegin altina sabit %2.5 koymak yerine en yakin mikro invalidation.
    structure = m15["structure"]
    last_swing_low = scanner.safe_float(structure.get("last_swing_low"))
    recent_low = scanner.safe_float(d["low"].tail(6).min())
    refs = [x for x in (last_swing_low, recent_low, scanner.safe_float(support.high)) if 0 < x < price]
    if breakout_level and 0 < breakout_level < price:
        refs.append(breakout_level)
    if not refs:
        return "STOP_REFERANS_YOK", score, {"setups": setups}
    invalidation = max(refs)
    stop = invalidation - atr * 0.35
    stop_pct = (price - stop) / price * 100
    if stop_pct <= 0 or stop_pct > scanner.MAX_STOP_PCT:
        return "STOP_UZAK", score, {"setups": setups, "stop_pct": stop_pct}

    # V2 TARGET: en yakin mikro pivotu degil, en az %1.5 uzaktaki anlamli pivot direncini kullan.
    target1 = meaningful_target(h1["df"], d, price, scanner.MIN_TARGET_PCT)
    if not target1:
        return "HEDEF_YOK", score, {"setups": setups, "stop_pct": stop_pct}
    target_pct = scanner.pct_change(target1, price)
    if target_pct < scanner.MIN_TARGET_PCT:
        return "HEDEF_DAR", score, {"setups": setups, "target_pct": target_pct, "stop_pct": stop_pct}

    rr = target_pct / stop_pct
    if target_pct >= 4:
        score += 6
    elif target_pct >= 2.5:
        score += 4
    if rr >= 1.2:
        score += 4
    elif rr < 0.65:
        score -= 8
    score = scanner.clamp(score)

    meta = {
        "setups": setups,
        "stop_pct": stop_pct,
        "target_pct": target_pct,
        "rr": rr,
    }
    if score < scanner.MIN_ENTRY_SCORE:
        return "ENTRY_SKOR", score, meta
    return "ENTRY_OK", score, meta


def add_result(reason: str, score: float | None, meta: dict[str, Any], reasons: Counter,
               scores: list[float], setups: Counter, stops: list[float], targets: list[float], rrs: list[float]) -> None:
    reasons[reason] += 1
    if score is not None:
        scores.append(float(score))
    for setup in meta.get("setups", []):
        setups[setup] += 1
    if "stop_pct" in meta:
        stops.append(float(meta["stop_pct"]))
    if "target_pct" in meta:
        targets.append(float(meta["target_pct"]))
    if "rr" in meta:
        rrs.append(float(meta["rr"]))


def print_side(name: str, checks: int, reasons: Counter, scores: list[float], setups: Counter,
               stops: list[float], targets: list[float], rrs: list[float]) -> None:
    ok = reasons.get("ENTRY_OK", 0)
    print(f"\n[{name}]")
    print(f"  15M kontrol: {checks} | ENTRY OK: {ok} (%{100*ok/max(1,checks):.2f})")
    for k, v in reasons.most_common():
        print(f"  {k:22} {v:7d}  %{100*v/max(1,checks):6.2f}")
    print("  Entry skor: ", stats(scores))
    print("  Setup:      ", dict(setups))
    print("  Stop %:     ", stats(stops))
    print("  Target %:   ", stats(targets))
    print("  R/R:        ", stats(rrs))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--symbols", type=int, default=20)
    ap.add_argument("--stride", type=int, default=4, help="1=15m, 2=30m, 4=1h")
    args = ap.parse_args()
    if args.days < 3 or args.symbols < 1 or args.stride < 1:
        raise SystemExit("days>=3, symbols>=1, stride>=1 olmali")

    final_end = bt.closed_quarter()
    first_cutoff = final_end - timedelta(days=args.days)
    symbols = bt.current_symbols(args.symbols)
    print(f"[V1/V2 DIAG] {len(symbols)} sembol | {args.days} gun | stride={args.stride} ({15*args.stride} dk)")

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
        raise SystemExit("Yeterli veri yok")

    watch_reasons = Counter()
    btc_regimes = Counter()
    watch_ok = 0
    checks = 0
    v1_reasons, v2_reasons = Counter(), Counter()
    v1_scores, v2_scores = [], []
    v1_setups, v2_setups = Counter(), Counter()
    v1_stops, v2_stops = [], []
    v1_targets, v2_targets = [], []
    v1_rrs, v2_rrs = [], []
    errors = Counter()

    cutoffs = pd.date_range(first_cutoff, final_end, freq=f"{15*args.stride}min").to_pydatetime()
    for n, cutoff in enumerate(cutoffs, 1):
        with bt.historical_fetch(all_data, cutoff):
            try:
                btc = scanner.btc_context()
            except Exception:
                errors["BTC"] += 1
                continue
            btc_regimes[btc["regime"]] += 1
            for symbol in symbols:
                try:
                    qv = bt.historical_quote_volume(all_data[symbol]["1h"], cutoff)
                    w_reason, _, item = v1.watch_probe(symbol, qv, btc)
                    watch_reasons[w_reason] += 1
                    if item is None:
                        continue
                    watch_ok += 1
                    checks += 1

                    r1, s1, m1 = v1.execution_probe(item, btc)
                    add_result(r1, s1, m1, v1_reasons, v1_scores, v1_setups, v1_stops, v1_targets, v1_rrs)

                    r2, s2, m2 = execution_probe_v2(item, btc)
                    add_result(r2, s2, m2, v2_reasons, v2_scores, v2_setups, v2_stops, v2_targets, v2_rrs)
                except Exception as exc:
                    errors[type(exc).__name__] += 1

        if n % max(1, len(cutoffs)//10) == 0:
            print(
                f"[PROGRESS] {n}/{len(cutoffs)} | watch={watch_ok} | "
                f"V1={v1_reasons.get('ENTRY_OK',0)} | V2={v2_reasons.get('ENTRY_OK',0)}"
            )

    total_watch_checks = sum(watch_reasons.values())
    print("\n" + "=" * 80)
    print("V1 vs V2 STRATEJI KARSILASTIRMA")
    print("=" * 80)
    print(f"Toplam 1H kontrol: {total_watch_checks}")
    print(f"1H watch OK:      {watch_ok} (%{100*watch_ok/max(1,total_watch_checks):.2f})")
    print(f"BTC rejimleri:    {dict(btc_regimes)}")

    print_side("V1 MEVCUT", checks, v1_reasons, v1_scores, v1_setups, v1_stops, v1_targets, v1_rrs)
    print_side("V2 DENEYSEL", checks, v2_reasons, v2_scores, v2_setups, v2_stops, v2_targets, v2_rrs)

    print("\n[KISA KIYAS]")
    v1_ok = v1_reasons.get("ENTRY_OK", 0)
    v2_ok = v2_reasons.get("ENTRY_OK", 0)
    print(f"  ENTRY OK: V1={v1_ok} | V2={v2_ok} | fark={v2_ok-v1_ok:+d}")
    if v1_stops and v2_stops:
        print(f"  Median stop:   V1=%{statistics.median(v1_stops):.2f} | V2=%{statistics.median(v2_stops):.2f}")
    if v1_targets and v2_targets:
        print(f"  Median target: V1=%{statistics.median(v1_targets):.2f} | V2=%{statistics.median(v2_targets):.2f}")
    if v1_rrs and v2_rrs:
        print(f"  Median R/R:    V1={statistics.median(v1_rrs):.2f} | V2={statistics.median(v2_rrs):.2f}")
    print("  Not: V2 sadece diagnostiktir; canli scanner ve backtest stratejisi degismedi.")

    if errors:
        print("\n[HATALAR]", dict(errors))


if __name__ == "__main__":
    main()
