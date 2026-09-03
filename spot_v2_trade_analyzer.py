# -*- coding: utf-8 -*-
"""V2 backtest JSON icindeki gercek trade'leri segmentlere ayirir.

Yeni piyasa verisi indirmez; /tmp/spot_intraday_backtest_v2.json dosyasini okur.
Amac: hangi setup/skor/BTC/stop/target/RR gruplari para kazandiriyor veya kaybettiriyor?

Kullanim:
  python spot_v2_trade_analyzer.py
  python spot_v2_trade_analyzer.py --input /tmp/spot_intraday_backtest_v2.json
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable


def pf(rows: list[dict[str, Any]]) -> float | None:
    win = sum(float(r.get("net_pnl", 0)) for r in rows if float(r.get("net_pnl", 0)) > 0)
    loss = abs(sum(float(r.get("net_pnl", 0)) for r in rows if float(r.get("net_pnl", 0)) <= 0))
    return win / loss if loss else (999.0 if win > 0 else None)


def metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    wins = [r for r in rows if float(r.get("net_pnl", 0)) > 0]
    losses = [r for r in rows if float(r.get("net_pnl", 0)) <= 0]
    pnl = sum(float(r.get("net_pnl", 0)) for r in rows)
    return {
        "n": n,
        "wr": 100 * len(wins) / n if n else 0,
        "pnl": pnl,
        "avg": pnl / n if n else 0,
        "avg_win": sum(float(r["net_pnl"]) for r in wins) / len(wins) if wins else 0,
        "avg_loss": sum(float(r["net_pnl"]) for r in losses) / len(losses) if losses else 0,
        "pf": pf(rows),
    }


def label_num(x: float, cuts: list[tuple[float, str]], last: str) -> str:
    for ceiling, label in cuts:
        if x < ceiling:
            return label
    return last


def score_bucket(r: dict[str, Any]) -> str:
    x = float(r.get("entry_score", 0))
    return label_num(x, [(72, "68-<72"), (76, "72-<76"), (80, "76-<80")], "80+")


def stop_bucket(r: dict[str, Any]) -> str:
    x = float(r.get("stop_pct", 0))
    return label_num(x, [(1, "<1%"), (2, "1-<2%"), (3, "2-<3%")], "3%+")


def target_bucket(r: dict[str, Any]) -> str:
    x = float(r.get("target_pct", 0))
    return label_num(x, [(2, "1.5-<2%"), (3, "2-<3%"), (4, "3-<4%")], "4%+")


def rr_value(r: dict[str, Any]) -> float:
    s = float(r.get("stop_pct", 0))
    t = float(r.get("target_pct", 0))
    return t / s if s > 0 else 0


def rr_bucket(r: dict[str, Any]) -> str:
    x = rr_value(r)
    return label_num(x, [(1.2, "<1.2"), (1.5, "1.2-<1.5"), (2, "1.5-<2"), (3, "2-<3")], "3+")


def setup_tokens(r: dict[str, Any]) -> list[str]:
    raw = str(r.get("setup", "UNKNOWN"))
    return [x for x in raw.split("+") if x] or ["UNKNOWN"]


def print_table(title: str, groups: dict[str, list[dict[str, Any]]], min_n: int = 1) -> None:
    print("\n" + title)
    print("-" * 94)
    print(f"{'Grup':30} {'N':>4} {'WR%':>7} {'PnL$':>10} {'Avg$':>9} {'PF':>7} {'AvgWin':>9} {'AvgLoss':>9}")
    print("-" * 94)
    items = []
    for name, rows in groups.items():
        if len(rows) < min_n:
            continue
        m = metrics(rows)
        items.append((m["pnl"], name, m))
    for _, name, m in sorted(items, reverse=True):
        pf_text = "-" if m["pf"] is None else (">99" if m["pf"] >= 99 else f"{m['pf']:.2f}")
        print(f"{name[:30]:30} {m['n']:4d} {m['wr']:7.2f} {m['pnl']:10.2f} {m['avg']:9.2f} {pf_text:>7} {m['avg_win']:9.2f} {m['avg_loss']:9.2f}")


def grouped(rows: list[dict[str, Any]], fn: Callable[[dict[str, Any]], str]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        out[fn(r)].append(r)
    return dict(out)


def token_groups(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        for token in setup_tokens(r):
            out[token].append(r)
    return dict(out)


def combo_groups(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    return grouped(rows, lambda r: str(r.get("setup", "UNKNOWN")))


def pair_groups(rows: list[dict[str, Any]], a: Callable[[dict[str, Any]], str], b: Callable[[dict[str, Any]], str]) -> dict[str, list[dict[str, Any]]]:
    return grouped(rows, lambda r: f"{a(r)} | {b(r)}")


def threshold_scan(rows: list[dict[str, Any]]) -> None:
    print("\n[ESIK TARAMASI - sadece mevcut 73 trade uzerinde ex-post filtre]")
    print("Bu tablo yeni backtest degildir; hangi filtrelerin umut verici oldugunu bulmak icindir.")
    candidates: list[tuple[float, int, float, float, str]] = []
    for score_min in (68, 70, 72, 74, 76, 78, 80):
        for rr_min in (0, 1.0, 1.2, 1.5, 2.0):
            for stop_max in (1.0, 1.5, 2.0, 2.5, 3.5, 6.5):
                selected = [r for r in rows if float(r.get("entry_score", 0)) >= score_min and rr_value(r) >= rr_min and float(r.get("stop_pct", 99)) <= stop_max]
                if len(selected) < 8:
                    continue
                m = metrics(selected)
                p = m["pf"] if m["pf"] is not None else 0
                candidates.append((p, len(selected), m["pnl"], m["wr"], f"score>={score_min}, RR>={rr_min:g}, stop<={stop_max:g}%"))
    candidates.sort(key=lambda x: (x[0], x[2], x[1]), reverse=True)
    print(f"{'Filtre':43} {'N':>4} {'WR%':>7} {'PnL$':>10} {'PF':>7}")
    print("-" * 76)
    for p, n, pnl, wr, name in candidates[:15]:
        print(f"{name:43} {n:4d} {wr:7.2f} {pnl:10.2f} {p:7.2f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default="/tmp/spot_intraday_backtest_v2.json")
    args = ap.parse_args()
    path = Path(args.input)
    if not path.exists():
        raise SystemExit(f"JSON bulunamadi: {path}\nBacktest'i ayni Render container/deployment Shell'inde calistirdigindan emin ol.")
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data.get("trades", [])
    if not rows:
        raise SystemExit("JSON icinde trade yok")

    overall = metrics(rows)
    print("=" * 94)
    print("V2 TRADE SEGMENT ANALIZI")
    print("=" * 94)
    print(f"Trade={overall['n']} | WR=%{overall['wr']:.2f} | PnL=${overall['pnl']:.2f} | PF={overall['pf']:.3f}")

    print_table("[SETUP ICERIGI] (kombinasyonda setup varsa trade o gruba dahil)", token_groups(rows))
    print_table("[SETUP KOMBINASYONU]", combo_groups(rows))
    print_table("[BTC REJIMI]", grouped(rows, lambda r: str(r.get("btc_regime", "?"))))
    print_table("[ENTRY SCORE]", grouped(rows, score_bucket))
    print_table("[STOP MESAFESI]", grouped(rows, stop_bucket))
    print_table("[TARGET MESAFESI]", grouped(rows, target_bucket))
    print_table("[R/R]", grouped(rows, rr_bucket))
    print_table("[SCORE + BTC]", pair_groups(rows, score_bucket, lambda r: str(r.get("btc_regime", "?"))))
    print_table("[SETUP + BTC]", pair_groups(rows, lambda r: str(r.get("setup", "?")), lambda r: str(r.get("btc_regime", "?"))))
    print_table("[COIN] (en az 2 trade)", grouped(rows, lambda r: str(r.get("symbol", "?"))), min_n=2)
    print_table("[CIKIS NEDENI]", grouped(rows, lambda r: str(r.get("reason", "?"))))
    threshold_scan(rows)

    print("\n[NOT]")
    print("Bu analiz ayni 14 gunluk orneklem uzerinde ex-post incelemedir. En iyi gorunen filtreyi dogrudan canliya tasimayacagiz;")
    print("once bagimsiz/daha uzun donemde V3 walk-forward backtest ile dogrulayacagiz.")


if __name__ == "__main__":
    main()
