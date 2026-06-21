#!/usr/bin/env python3
"""
PANİK PUMP — CSV Backtest Analizi
Kaynak: backtest_capit_stptp_results.csv (252 sinyal, 3 senaryo)
API çağrısı yok — tüm veri CSV'den gelir.
"""
import csv
import sys
from pathlib import Path

CSV_PATH = Path(__file__).parent / "backtest_capit_stptp_results.csv"

SCENARIOS = {
    "REF": {"status": "REF_status", "ret": "REF_close_ret", "tp": "REF_tp_hit"},
    "A":   {"status": "A_status",   "ret": "A_close_ret",   "tp": "A_tp_hit"},
    "B":   {"status": "B_status",   "ret": "B_close_ret",   "tp": "B_tp_hit"},
}


def avg(lst): return sum(lst) / len(lst) if lst else 0.0
def pct(n, d): return n / d * 100 if d else 0.0


def load():
    with open(CSV_PATH, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    records = []
    for r in rows:
        try:
            rec = {
                "symbol":    r["symbol"].replace("/USDT", ""),
                "ts":        r["ts"],
                "year":      int(r["year"]),
                "ret1":      float(r["ret1"]),        # 1h bar düşüşü (negatif = düşüş)
                "vol_ratio": float(r["vol_ratio"]),   # hacim oranı
                "entry":     float(r["entry"]),
            }
            for sc, cols in SCENARIOS.items():
                rec[f"{sc}_status"] = r[cols["status"]]
                rec[f"{sc}_ret"]    = float(r[cols["ret"]]) if r[cols["ret"]] else 0.0
                rec[f"{sc}_tp"]     = r[cols["tp"]]
            records.append(rec)
        except (ValueError, KeyError):
            continue
    return records


def sep(title="", w=72):
    if title:
        print(f"\n{'─'*4} {title} {'─'*(w-6-len(title))}", flush=True)
    else:
        print("─" * w, flush=True)


def scenario_summary(records, sc):
    wins    = [r for r in records if r[f"{sc}_status"] == "win"]
    losses  = [r for r in records if r[f"{sc}_status"] == "loss"]
    expired = [r for r in records if r[f"{sc}_status"] == "expired"]
    total   = len(records)
    closed  = len(wins) + len(losses) + len(expired)
    wr      = pct(len(wins), total)
    pnl     = sum(r[f"{sc}_ret"] for r in records)
    avg_win = avg([r[f"{sc}_ret"] for r in wins])
    avg_los = avg([r[f"{sc}_ret"] for r in losses])
    avg_exp = avg([r[f"{sc}_ret"] for r in expired])
    print(f"  {sc:4}  W:{len(wins):3d} L:{len(losses):3d} E:{len(expired):3d}"
          f"  WR:%{wr:.0f}  PnL:{pnl:+.1f}%"
          f"  avg_W:+{avg_win:.1f}%  avg_L:{avg_los:.1f}%  avg_E:{avg_exp:+.1f}%",
          flush=True)


def filter_row(records, sc, label, fn):
    total    = len(records)
    base_w   = sum(1 for r in records if r[f"{sc}_status"] == "win")
    base_wr  = pct(base_w, total)
    passed   = [r for r in records if fn(r)]
    blocked  = [r for r in records if not fn(r)]
    if not passed:
        print(f"  {label:<40} sinyal yok", flush=True)
        return
    p_wins   = sum(1 for r in passed  if r[f"{sc}_status"] == "win")
    b_wins   = sum(1 for r in blocked if r[f"{sc}_status"] == "win")
    wr_p     = pct(p_wins, len(passed))
    wr_b     = pct(b_wins, len(blocked)) if blocked else 0
    diff     = wr_p - base_wr
    pnl_p    = sum(r[f"{sc}_ret"] for r in passed)
    pnl_b    = sum(r[f"{sc}_ret"] for r in blocked)
    mark     = (f"+{diff:.0f}pp ✅" if diff > 5 else
                (f"{diff:.0f}pp ❌" if diff < -5 else f"{diff:+.0f}pp —"))
    print(f"  {label:<40} {p_wins:3d}/{len(passed):3d} WR%{wr_p:.0f} {mark}"
          f"  PnL:{pnl_p:+.1f}%  |  eliyor:{len(blocked):3d} WR%{wr_b:.0f} PnL:{pnl_b:+.1f}%",
          flush=True)


def year_breakdown(records, sc):
    years = sorted(set(r["year"] for r in records))
    for y in years:
        yr = [r for r in records if r["year"] == y]
        w  = sum(1 for r in yr if r[f"{sc}_status"] == "win")
        l  = sum(1 for r in yr if r[f"{sc}_status"] == "loss")
        e  = sum(1 for r in yr if r[f"{sc}_status"] == "expired")
        pnl = sum(r[f"{sc}_ret"] for r in yr)
        print(f"  {y}  {len(yr):3d} sinyal  W:{w:2d} L:{l:2d} E:{e:2d}"
              f"  WR:%{pct(w,len(yr)):.0f}  PnL:{pnl:+.1f}%", flush=True)


def vol_dist(records):
    buckets = [(0, 1.5), (1.5, 2.0), (2.0, 2.5), (2.5, 3.0), (3.0, 99)]
    labels  = ["<1.5x", "1.5-2x", "2-2.5x", "2.5-3x", ">3x"]
    print("  vol_ratio  |  sinyal  REF_WR%  A_WR%  B_WR%", flush=True)
    for (lo, hi), lbl in zip(buckets, labels):
        grp = [r for r in records if lo <= r["vol_ratio"] < hi]
        if not grp: continue
        def wr(sc): return pct(sum(1 for r in grp if r[f"{sc}_status"]=="win"), len(grp))
        print(f"  {lbl:<10}  {len(grp):5d}    %{wr('REF'):.0f}     %{wr('A'):.0f}   %{wr('B'):.0f}", flush=True)


def drop_dist(records):
    buckets = [(-5, 0), (-7, -5), (-10, -7), (-15, -10), (-99, -15)]
    labels  = ["0 to -5%", "-5 to -7%", "-7 to -10%", "-10 to -15%", "<-15%"]
    print("  ret1 (1h düşüş)  |  sinyal  REF_WR%  A_WR%  B_WR%", flush=True)
    for (lo, hi), lbl in zip(buckets, labels):
        grp = [r for r in records if lo <= r["ret1"] < hi]
        if not grp: continue
        def wr(sc): return pct(sum(1 for r in grp if r[f"{sc}_status"]=="win"), len(grp))
        print(f"  {lbl:<16}  {len(grp):5d}    %{wr('REF'):.0f}     %{wr('A'):.0f}   %{wr('B'):.0f}", flush=True)


def main():
    if not CSV_PATH.exists():
        print(f"CSV bulunamadı: {CSV_PATH}", flush=True)
        print("Dosyayı script ile aynı dizine koyun: backtest_capit_stptp_results.csv", flush=True)
        sys.exit(1)

    records = load()
    total   = len(records)

    print(f"\n{'='*72}", flush=True)
    print(f"PANİK PUMP CSV BACKTEST — {total} sinyal (2022-2026)", flush=True)
    print(f"{'='*72}", flush=True)

    # --- GENEL SENARYO KARŞILAŞTIRMASI ---
    sep("SENARYO KARŞILAŞTIRMASI  (REF=stop yok | A=TP5%/SL-3% | B=TP8%/SL-4%)")
    for sc in ("REF", "A", "B"):
        scenario_summary(records, sc)

    # --- YIL BAZLI (REF) ---
    sep("YIL BAZLI — REF sistemi")
    year_breakdown(records, "REF")

    # --- HACİM DAĞILIMI ---
    sep("HACİM ORANI DAĞILIMI")
    vol_dist(records)

    # --- DÜŞÜŞ DERİNLİĞİ ---
    sep("1H BAR DÜŞÜŞÜ DAĞILIMI")
    drop_dist(records)

    # --- FİLTRE ANALİZİ (REF sistemi üzerinde) ---
    sep("FİLTRE ANALİZİ — REF sistemi (baz: WR%83)")

    print("\n  ── Hacim filtreleri ──", flush=True)
    filter_row(records, "REF", "vol >= 1.5x",              lambda r: r["vol_ratio"] >= 1.5)
    filter_row(records, "REF", "vol >= 2.0x",              lambda r: r["vol_ratio"] >= 2.0)
    filter_row(records, "REF", "vol >= 2.5x",              lambda r: r["vol_ratio"] >= 2.5)
    filter_row(records, "REF", "vol >= 3.0x",              lambda r: r["vol_ratio"] >= 3.0)
    filter_row(records, "REF", "vol < 2.0x (düşük hacim)", lambda r: r["vol_ratio"] < 2.0)

    print("\n  ── Düşüş derinliği filtreleri ──", flush=True)
    filter_row(records, "REF", "ret1 <= -7%",              lambda r: r["ret1"] <= -7.0)
    filter_row(records, "REF", "ret1 <= -9%",              lambda r: r["ret1"] <= -9.0)
    filter_row(records, "REF", "ret1 <= -10%",             lambda r: r["ret1"] <= -10.0)
    filter_row(records, "REF", "ret1 <= -12%",             lambda r: r["ret1"] <= -12.0)
    filter_row(records, "REF", "-7% < ret1 <= -10%",       lambda r: -10 < r["ret1"] <= -7.0)

    print("\n  ── Kombine filtreler ──", flush=True)
    filter_row(records, "REF", "vol>=2x + ret1<=-7%",      lambda r: r["vol_ratio"]>=2.0 and r["ret1"]<=-7.0)
    filter_row(records, "REF", "vol>=2x + ret1<=-9%",      lambda r: r["vol_ratio"]>=2.0 and r["ret1"]<=-9.0)
    filter_row(records, "REF", "vol>=2x + ret1<=-10%",     lambda r: r["vol_ratio"]>=2.0 and r["ret1"]<=-10.0)
    filter_row(records, "REF", "vol>=2.5x + ret1<=-7%",    lambda r: r["vol_ratio"]>=2.5 and r["ret1"]<=-7.0)
    filter_row(records, "REF", "vol>=2.5x + ret1<=-9%",    lambda r: r["vol_ratio"]>=2.5 and r["ret1"]<=-9.0)
    filter_row(records, "REF", "vol>=1.5x + ret1<=-7%",    lambda r: r["vol_ratio"]>=1.5 and r["ret1"]<=-7.0)
    filter_row(records, "REF", "vol>=1.5x + ret1<=-10%",   lambda r: r["vol_ratio"]>=1.5 and r["ret1"]<=-10.0)

    # --- FİLTRE ANALİZİ (A sistemi — hard stop var) ---
    sep("FİLTRE ANALİZİ — A sistemi (TP+5%/SL-3%, baz: WR%48)")

    print("\n  ── Kombine filtreler ──", flush=True)
    filter_row(records, "A", "vol>=2x + ret1<=-7%",        lambda r: r["vol_ratio"]>=2.0 and r["ret1"]<=-7.0)
    filter_row(records, "A", "vol>=2x + ret1<=-9%",        lambda r: r["vol_ratio"]>=2.0 and r["ret1"]<=-9.0)
    filter_row(records, "A", "vol>=2.5x + ret1<=-7%",      lambda r: r["vol_ratio"]>=2.5 and r["ret1"]<=-7.0)
    filter_row(records, "A", "vol>=2.5x + ret1<=-9%",      lambda r: r["vol_ratio"]>=2.5 and r["ret1"]<=-9.0)
    filter_row(records, "A", "vol>=3.0x + ret1<=-7%",      lambda r: r["vol_ratio"]>=3.0 and r["ret1"]<=-7.0)

    # --- EN İYİ 10 VE EN KÖTÜ 10 ---
    sep("EN KÖTÜ 10 — REF expired (büyük kayıp)")
    worst = sorted([r for r in records if r["REF_status"]=="expired"],
                   key=lambda r: r["REF_ret"])[:10]
    for r in worst:
        print(f"  {r['symbol']:8} {r['ts'][:10]}  drop:{r['ret1']:+.1f}%  vol:{r['vol_ratio']:.1f}x"
              f"  ret:{r['REF_ret']:+.1f}%", flush=True)

    print(f"\n{'='*72}", flush=True)
    print(f"ÖZET: REF sistemi {total} sinyalin %83'ünde TP vuruyor.", flush=True)
    ref_wins = sum(1 for r in records if r["REF_status"]=="win")
    ref_exp  = sum(1 for r in records if r["REF_status"]=="expired")
    ref_pnl  = sum(r["REF_ret"] for r in records)
    print(f"  {ref_wins} kazanç × +5% = +{ref_wins*5}%  |  {ref_exp} expire  |  Toplam PnL: {ref_pnl:+.1f}%", flush=True)
    print(f"{'='*72}\n", flush=True)


if __name__ == "__main__":
    main()
