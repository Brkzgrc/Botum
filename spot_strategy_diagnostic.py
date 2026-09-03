# -*- coding: utf-8 -*-
"""Hizli strateji diagnostigi: 1H -> 15M -> stop -> target -> entry score.

Canli scanner'i degistirmez. Tarihsel veride hangi filtrenin kac adayi eledigini sayar.

Ornek:
  python spot_strategy_diagnostic.py --days 14 --symbols 20 --stride 4
  python spot_strategy_diagnostic.py --days 30 --symbols 40 --stride 1

stride=1: her 15M (tam)
stride=2: her 30M
stride=4: saatte bir (hizli kaba tarama)
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
        f"n={len(values)} med={statistics.median(values):.1f} "
        f"p75={pctile(values, .75):.1f} p90={pctile(values, .90):.1f} max={max(values):.1f}"
    )


def watch_probe(symbol: str, quote_volume: float, btc: dict[str, Any]) -> tuple[str, float | None, Any | None]:
    state = scanner.h1_state(scanner.fetch_ohlcv(symbol, "1h", 260))
    d = state["df"]
    price = state["price"]
    support = scanner.build_zone(d, "support", 180, 2, "1H")
    resistance = scanner.build_zone(d, "resistance", 180, 2, "1H")
    if not support or not resistance or resistance.low <= price:
        return "H1_ZONE_YOK", None, None

    support_distance = scanner.pct_change(price, support.high)
    target_room = scanner.pct_change(resistance.low, price)
    rel1 = state["ret_1h"] - scanner.safe_float(btc["ret_1h"])
    rel4 = state["ret_4h"] - scanner.safe_float(btc["ret_4h"])
    low_liq_ok = quote_volume >= scanner.PREFERRED_QUOTE_VOLUME or state["vol_ratio"] >= scanner.LOW_LIQ_VOLUME_RATIO
    if not low_liq_ok:
        return "H1_LIKIDITE_HACIM", None, None

    score = 0.0
    reasons: list[str] = []
    risks: list[str] = []
    structure = state["structure"]["trend"]
    if structure == "HH_HL":
        score += 18; reasons.append("1H HH/HL yapisi")
    elif structure in {"HL_BUILDING", "HH_BUILDING"}:
        score += 11; reasons.append("1H yukari yapi olusuyor")
    elif structure == "LH_LL":
        score -= 12; risks.append("1H LH/LL dusus yapisi")
    if state["trend"] == "BULL":
        score += 12; reasons.append("1H fiyat EMA20 ve EMA50 ustunde")
    elif state["trend"] == "BEAR":
        score -= 8; risks.append("1H EMA trendi zayif")
    if rel1 >= 0.8:
        score += 12; reasons.append(f"BTC'ye gore 1H +%{rel1:.2f} goreceli guc")
    elif rel1 >= 0.25:
        score += 7
    elif rel1 < -0.8:
        score -= 8; risks.append("1H BTC'den belirgin zayif")
    if rel4 >= 1.5:
        score += 13; reasons.append(f"BTC'ye gore 4H +%{rel4:.2f} goreceli guc")
    elif rel4 >= 0.5:
        score += 8
    elif rel4 < -1.5:
        score -= 8
    if state["vol_ratio"] >= 2.0:
        score += 10; reasons.append(f"1H hacim anomalisi {state['vol_ratio']:.1f}x")
    elif state["vol_ratio"] >= 1.25:
        score += 6
    if state["taker_buy_ratio"] >= 0.55:
        score += 5
    elif state["taker_buy_ratio"] < 0.43:
        score -= 5
    if 0 <= support_distance <= 2.5:
        score += 10; reasons.append("1H ana destege yakin")
    elif support_distance > 5:
        score -= 7; risks.append("1H ana destege uzak")
    if target_room >= 4:
        score += 10; reasons.append(f"Ilk dirence +%{target_room:.1f} alan")
    elif target_room >= scanner.MIN_TARGET_PCT:
        score += 5
    else:
        score -= 12; risks.append("Ilk dirence alan dar")
    if 48 <= state["rsi"] <= 68:
        score += 5
    elif state["rsi"] > 75:
        score -= 5; risks.append("1H RSI isinmis")
    if state["macd_hist"] > state["macd_hist_prev"]:
        score += 5
    if btc["regime"] == "YELLOW" and rel4 < 0.8:
        score -= 7
    if btc["regime"] == "RED" and rel4 < 2.0:
        score -= 15
    score = scanner.clamp(score)
    if score < scanner.MIN_WATCH_SCORE:
        return "H1_SKOR", score, None

    item = scanner.WatchItem(
        symbol=symbol, quote_volume_24h=quote_volume, watch_score=round(score, 1), h1_price=price,
        h1_atr=state["atr"], support=support, resistance=resistance, h1_trend=state["trend"],
        h1_structure=structure, relative_strength_1h=round(rel1, 3), relative_strength_4h=round(rel4, 3),
        volume_ratio_1h=round(state["vol_ratio"], 2), reasons=reasons[:6], risks=risks[:5],
    )
    return "WATCH_OK", score, item


def execution_probe(item: Any, btc: dict[str, Any]) -> tuple[str, float | None, dict[str, Any]]:
    if btc["regime"] == "RED":
        return "BTC_RED", None, {}
    h1 = scanner.h1_state(scanner.fetch_ohlcv(item.symbol, "1h", 220))
    m15 = scanner.m15_state(scanner.fetch_ohlcv(item.symbol, "15m", 220))
    d = m15["df"]
    price = m15["price"]
    atr = max(m15["atr"], price * 0.0015)
    support = scanner.merge_supports(h1["df"], d)
    resistance = scanner.choose_resistance(h1["df"], d, price)
    if not support or not resistance:
        return "M15_ZONE_YOK", None, {}

    structure = m15["structure"]
    last_swing_high = scanner.safe_float(structure.get("last_swing_high"))
    last_swing_low = scanner.safe_float(structure.get("last_swing_low"))
    prev_swing_low = scanner.safe_float(structure.get("prev_swing_low"))
    bullish_candle = m15["price"] > m15["last_open"]
    body = m15["price"] - m15["last_open"]
    candle_range = max(m15["last_high"] - m15["last_low"], 1e-12)
    body_ratio = body / candle_range
    mss = bullish_candle and price > m15["prev_high"] and last_swing_high > 0 and price >= last_swing_high * 0.999 and (prev_swing_low <= 0 or last_swing_low >= prev_swing_low)
    prior = d.iloc[-10:-2]
    breakout_level = scanner.safe_float(prior["high"].max())
    retest = breakout_level > 0 and scanner.safe_float(d.iloc[-2]["low"]) <= breakout_level * 1.004 and scanner.safe_float(d.iloc[-2]["close"]) >= breakout_level * 0.997 and price > breakout_level and bullish_candle
    pullback = scanner.safe_float(d["low"].tail(6).min()) <= m15["ema20"] * 1.004 and price > m15["ema20"] > m15["ema50"] and price > m15["prev_high"] and bullish_candle
    recent_ranges = (d["high"] - d["low"]).tail(10)
    older_ranges = (d["high"] - d["low"]).iloc[-30:-10]
    compressed = not older_ranges.empty and scanner.safe_float(recent_ranges.iloc[:-1].median()) <= scanner.safe_float(older_ranges.median()) * 0.72
    expansion = compressed and body_ratio >= 0.55 and m15["vol_ratio"] >= 1.6 and price > m15["prev_high"]
    setups = [name for flag, name in [
        (mss, "MSS"), (retest, "RETEST"), (pullback, "PULLBACK"), (expansion, "EXPANSION")
    ] if flag]
    if not setups:
        return "M15_SETUP_YOK", None, {"setups": []}

    score = item.watch_score * 0.45
    if mss: score += 10
    if retest: score += 8
    if pullback: score += 7
    if expansion: score += 8
    if m15["vol_ratio"] >= 2.0: score += 10
    elif m15["vol_ratio"] >= 1.5: score += 7
    elif m15["vol_ratio"] < 0.9: score -= 5
    if m15["taker_buy_ratio"] >= 0.55: score += 2
    if 50 <= m15["rsi"] <= 70: score += 5
    elif m15["rsi"] > 78: score -= 8
    stoch_cross = m15["stoch_k"] > m15["stoch_d"] and m15["stoch_prev_k"] <= m15["stoch_prev_d"]
    if stoch_cross: score += 2
    if m15["macd_hist"] > m15["macd_hist_prev"]: score += 3
    ema_distance_atr = (price - m15["ema20"]) / max(atr, 1e-12)
    if ema_distance_atr > 2.2: score -= 12
    if btc["regime"] == "YELLOW":
        if item.relative_strength_1h < 0.5 or item.relative_strength_4h < 1.0:
            return "BTC_YELLOW_RS", score, {"setups": setups}
        score -= 3

    structural_low = min(support.low, last_swing_low if last_swing_low > 0 else support.low)
    stop = structural_low * (1 - scanner.SUPPORT_BUFFER_PCT / 100)
    stop_pct = (price - stop) / price * 100
    if stop_pct <= 0 or stop_pct > scanner.MAX_STOP_PCT:
        return "STOP_UZAK", score, {"setups": setups, "stop_pct": stop_pct}

    target1 = resistance.low
    target_pct = scanner.pct_change(target1, price)
    if target_pct < scanner.MIN_TARGET_PCT:
        return "HEDEF_DAR", score, {"setups": setups, "target_pct": target_pct, "stop_pct": stop_pct}

    rr = target_pct / stop_pct
    if target_pct >= 4: score += 6
    elif target_pct >= 2.5: score += 4
    if rr >= 1.2: score += 4
    elif rr < 0.65: score -= 8
    score = scanner.clamp(score)
    meta = {"setups": setups, "stop_pct": stop_pct, "target_pct": target_pct, "rr": rr}
    if score < scanner.MIN_ENTRY_SCORE:
        return "ENTRY_SKOR", score, meta
    return "ENTRY_OK", score, meta


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--symbols", type=int, default=20)
    ap.add_argument("--stride", type=int, default=2, help="1=15m, 2=30m, 4=1h")
    args = ap.parse_args()
    if args.days < 3 or args.symbols < 1 or args.stride < 1:
        raise SystemExit("days>=3, symbols>=1, stride>=1 olmali")

    final_end = bt.closed_quarter()
    first_cutoff = final_end - timedelta(days=args.days)
    symbols = bt.current_symbols(args.symbols)
    print(f"[DIAG] {len(symbols)} sembol | {args.days} gun | stride={args.stride} ({15*args.stride} dk)")

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
    exec_reasons = Counter()
    btc_regimes = Counter()
    setup_counts = Counter()
    watch_scores: list[float] = []
    entry_scores: list[float] = []
    stop_pcts: list[float] = []
    target_pcts: list[float] = []
    rrs: list[float] = []
    watch_ok_total = 0
    exec_checks = 0
    entry_ok = 0
    errors = Counter()

    cutoffs = pd.date_range(first_cutoff, final_end, freq=f"{15*args.stride}min").to_pydatetime()
    for n, cutoff in enumerate(cutoffs, 1):
        with bt.historical_fetch(all_data, cutoff):
            try:
                btc = scanner.btc_context()
            except Exception as exc:
                errors["BTC"] += 1
                continue
            btc_regimes[btc["regime"]] += 1
            for symbol in symbols:
                try:
                    qv = bt.historical_quote_volume(all_data[symbol]["1h"], cutoff)
                    w_reason, w_score, item = watch_probe(symbol, qv, btc)
                    watch_reasons[w_reason] += 1
                    if w_score is not None:
                        watch_scores.append(float(w_score))
                    if item is None:
                        continue
                    watch_ok_total += 1
                    exec_checks += 1
                    reason, e_score, meta = execution_probe(item, btc)
                    exec_reasons[reason] += 1
                    if e_score is not None:
                        entry_scores.append(float(e_score))
                    for setup in meta.get("setups", []):
                        setup_counts[setup] += 1
                    if "stop_pct" in meta:
                        stop_pcts.append(float(meta["stop_pct"]))
                    if "target_pct" in meta:
                        target_pcts.append(float(meta["target_pct"]))
                    if "rr" in meta:
                        rrs.append(float(meta["rr"]))
                    if reason == "ENTRY_OK":
                        entry_ok += 1
                except Exception as exc:
                    errors[type(exc).__name__] += 1
        if n % max(1, len(cutoffs)//10) == 0:
            print(f"[PROGRESS] {n}/{len(cutoffs)} | watch_ok={watch_ok_total} | entry_ok={entry_ok}")

    total_watch_checks = sum(watch_reasons.values())
    print("\n" + "="*76)
    print("STRATEJI DARBOGAZ RAPORU")
    print("="*76)
    print(f"Toplam 1H kontrol: {total_watch_checks}")
    print(f"1H watch OK:      {watch_ok_total} (%{100*watch_ok_total/max(1,total_watch_checks):.2f})")
    print(f"15M kontrol:      {exec_checks}")
    print(f"ENTRY OK:         {entry_ok} (%{100*entry_ok/max(1,exec_checks):.2f} / watch)")
    print(f"BTC rejimleri:    {dict(btc_regimes)}")

    print("\n[1H ELEME]")
    for k, v in watch_reasons.most_common():
        print(f"  {k:20} {v:8d}  %{100*v/max(1,total_watch_checks):6.2f}")
    print("  Watch skor dagilimi:", stats(watch_scores))

    print("\n[15M ELEME]")
    for k, v in exec_reasons.most_common():
        print(f"  {k:20} {v:8d}  %{100*v/max(1,exec_checks):6.2f}")
    print("  Entry skor dagilimi:", stats(entry_scores))
    print("  Setup gorulme:       ", dict(setup_counts))
    print("  Stop %:              ", stats(stop_pcts))
    print("  Target %:            ", stats(target_pcts))
    print("  R/R:                 ", stats(rrs))

    if errors:
        print("\n[HATALAR]", dict(errors))

    # Otomatik ilk yorum
    print("\n[OTOMATIK TESPIT]")
    if watch_ok_total == 0:
        print("  ANA DARBOGAZ: 1H katmani hic watch uretmiyor.")
    elif exec_checks and exec_reasons:
        bad = [(k, v) for k, v in exec_reasons.items() if k != "ENTRY_OK"]
        if bad:
            k, v = max(bad, key=lambda x: x[1])
            print(f"  ANA DARBOGAZ: {k} -> {v} kez (%{100*v/max(1,exec_checks):.1f} watch kontrolu).")
        if entry_scores:
            below = [x for x in entry_scores if x < scanner.MIN_ENTRY_SCORE]
            near = [x for x in below if x >= scanner.MIN_ENTRY_SCORE - 5]
            print(f"  Entry<{scanner.MIN_ENTRY_SCORE:g}: {len(below)} | esige 5 puan icinde: {len(near)}")
    print("  Not: Bu rapor stratejiyi DEGISTIRMEZ; sadece nerede elendigini olcer.")


if __name__ == "__main__":
    main()
