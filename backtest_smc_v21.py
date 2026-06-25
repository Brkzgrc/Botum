#!/usr/bin/env python3
"""
SMC v21 Backtest — Güncel canlı sistem parametreleri
CHoCH V2 + vol≥2.0x (SMC.py ile birebir)

Backtest dosya kuralı:
  smc_v21_ALL_{start}_{end}_{timestamp}.txt → YeniKlasör2/
"""

import sys, os, json, warnings
from datetime import datetime, timezone

warnings.filterwarnings("ignore")

BACKTEST_DIR = r"C:\Users\BRKZGRC\Desktop\Yeni klasör (2)"
OUTPUT_DIR   = BACKTEST_DIR  # backtest dosya kuralı: YeniKlasör2/

sys.path.insert(0, BACKTEST_DIR)
os.chdir(BACKTEST_DIR)

import numpy as np
import backtest as bt

# ── Parametreler (gerçek sistem — read/write API için) ────────────────────
VOL_RATIO_MIN = 2.0
COOLDOWN_H    = 24
EXPIRE_H      = bt.EXPIRE_H
MIN_VOL_24H   = bt.MIN_VOL_24H
INITIAL_CAP   = 5_000.0   # gerçek başlangıç
MAX_POS       = 5          # gerçek: max 5 eş zamanlı pozisyon
CAPS          = [None, 10_000, 20_000]  # limitsiz / max 10K / max 20K

START_DATE = "2020-01-01"
END_DATE   = datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ── Cap'li simulate_portfolio ──────────────────────────────────────────────
def capped_simulate(signals, initial=INITIAL_CAP, max_pos=MAX_POS, max_trade=None):
    if not signals:
        return initial, [(0, initial)]
    signals = sorted(signals, key=lambda x: x[0])
    cash = initial; positions = []; equity = [(signals[0][0], initial)]

    def close_expired(before_h):
        nonlocal cash
        remain = []
        for pos in positions:
            if pos[0] <= before_h:
                cash += pos[1] * (1 + pos[2] / 100)
                equity.append((pos[0], round(cash, 2)))
            else:
                remain.append(pos)
        positions[:] = remain

    for entry_h, exit_h, pnl in signals:
        close_expired(entry_h)
        if len(positions) < max_pos and cash > 0:
            cost = cash / max_pos
            if max_trade is not None:
                cost = min(cost, max_trade)
            cost = min(cost, cash)
            cash -= cost
            positions.append((exit_h, cost, pnl))

    for pos in sorted(positions, key=lambda x: x[0]):
        cash += pos[1] * (1 + pos[2] / 100)
        equity.append((pos[0], round(cash, 2)))

    equity.sort(key=lambda x: x[0])
    return round(cash, 2), equity


# ── SMC v21 sinyal üretimi ─────────────────────────────────────────────────
def run_smc_v21(symbols, btc_df):
    print("BTC 4H filtreleri hesaplanıyor...", flush=True)
    btc_f = bt.compute_btc_filters(btc_df)

    scenarios = ["half", "tp1", "tp2"]
    S = {k: bt.empty_stats() for k in scenarios}
    P = {k: [] for k in scenarios}
    signals_detail = []  # per-sinyal log (loss analizi için)

    total = len(symbols)
    for sym_idx, symbol in enumerate(symbols, 1):
        if symbol == "BTC/USDT":
            continue
        print(f"[{sym_idx}/{total}] {symbol}", flush=True)

        df_raw = bt.load_or_fetch(symbol)
        if df_raw is None or len(df_raw) < 300:
            print("  → Yetersiz veri"); continue

        vol = df_raw["volume"]
        df_raw = df_raw.copy()
        df_raw["vol_24h_usd"]  = (df_raw["close"] * vol).rolling(24).sum()
        df_raw["vol_ratio_20"] = vol / vol.rolling(20).mean().shift(1)

        df = df_raw.dropna(subset=["vol_24h_usd"]).copy()
        if len(df) < 300:
            continue

        try:
            df = bt.compute_extra_indicators(df)
        except Exception as e:
            print(f"  → İndikatör hatası: {e}"); continue

        btc_a = btc_f.reindex(df.index, method="ffill").fillna(
            {"crash_ok": True, "downtrend_ok": True})

        bts, bds, cls_, swls = bt.run_choch_incremental(df)

        c_arr  = df["close"].values
        vol24  = df["vol_24h_usd"].values
        volr20 = df["vol_ratio_20"].values
        h_arr  = df["high"].values
        l_arr  = df["low"].values
        n = len(df)
        ts_arr = np.array([t.timestamp() / 3600 for t in df.index])

        last_v21 = 0.0

        for i in range(250, n - 1):
            ts_h  = ts_arr[i]
            price = c_arr[i]
            if np.isnan(price) or price <= 0: continue
            if np.isnan(vol24[i]) or vol24[i] < MIN_VOL_24H: continue

            bf_crash = bool(btc_a["crash_ok"].iloc[i])
            bf_down  = bool(btc_a["downtrend_ok"].iloc[i])
            if not (bf_crash and bf_down): continue

            vr       = volr20[i]
            is_choch = (bts[i] == "CHoCH" and bds[i] == "BULLISH")
            if not is_choch: continue

            # ── VOL≥2.0x (canlı SMC.py ile birebir) ──────────────────────
            if np.isnan(vr) or vr < VOL_RATIO_MIN: continue
            if ts_h - last_v21 < COOLDOWN_H: continue

            choch_lvl = cls_[i]; sw_low = swls[i]
            entry = choch_lvl if choch_lvl else price
            stop  = sw_low * 0.995 if sw_low else entry * 0.95
            if stop >= entry: stop = entry * 0.95
            risk = max(entry - stop, entry * 0.01)
            tp1 = entry + risk; tp2 = entry + risk * 2

            fh = h_arr[i+1:]; fl = l_arr[i+1:]; fc = c_arr[i+1:]
            if len(fh) == 0: continue

            ph, oh, bh = bt.sim_half(fh, fl, fc, entry, stop, tp1, tp2, EXPIRE_H)
            p1, o1, b1 = bt.sim_tp1(fh, fl, fc, entry, stop, tp1, EXPIRE_H)
            p2, o2, b2 = bt.sim_tp2(fh, fl, fc, entry, stop, tp2, EXPIRE_H)

            bt.record(S["half"], ph, oh); P["half"].append((ts_h, ts_h + bh, ph))
            bt.record(S["tp1"],  p1, o1); P["tp1"].append((ts_h, ts_h + b1, p1))
            bt.record(S["tp2"],  p2, o2); P["tp2"].append((ts_h, ts_h + b2, p2))
            last_v21 = ts_h

            # Per-sinyal kayıt (loss pattern analizi)
            entry_dt = datetime.utcfromtimestamp(ts_h * 3600)
            signals_detail.append({
                "symbol":       symbol,
                "entry_time":   entry_dt.strftime("%Y-%m-%dT%H:00"),
                "hour":         entry_dt.hour,
                "weekday":      entry_dt.weekday(),  # 0=Pzt, 6=Paz
                "vol_ratio":    round(float(vr), 2),
                "half_outcome": oh,  "half_pnl": round(ph, 3), "half_bars": bh,
                "tp1_outcome":  o1,  "tp1_pnl":  round(p1, 3),
                "tp2_outcome":  o2,  "tp2_pnl":  round(p2, 3),
            })

    for k in S:
        S[k] = bt.finalize(S[k])
    return S, P, signals_detail


# ── Ana akış ──────────────────────────────────────────────────────────────
def main():
    now_str  = datetime.now().strftime("%Y%m%d_%H%M")
    start_f  = START_DATE.replace("-", "")
    end_f    = END_DATE.replace("-", "")
    basename = f"smc_v21_ALL_{start_f}_{end_f}_{now_str}"
    out_txt  = os.path.join(OUTPUT_DIR, basename + ".txt")
    out_json = os.path.join(OUTPUT_DIR, basename + ".json")
    out_html = os.path.join(OUTPUT_DIR, basename + ".html")
    out_signals = os.path.join(OUTPUT_DIR, basename + "_signals.json")

    print(f"=== SMC v21 Backtest — {datetime.now().strftime('%Y-%m-%d %H:%M')} ===\n")
    print(f"Parametreler: CHoCH + vol≥{VOL_RATIO_MIN}x + BTC filtre + 24h cooldown")
    print(f"TP: risk×1.0 (TP1) / risk×2.0 (TP2) | Stop: swing×0.995\n")

    pkls    = sorted(f for f in os.listdir(bt.DATA_DIR) if f.endswith(".pkl"))
    symbols = [
        f[:-4].replace("_", "/", 1)
        for f in pkls
        if f[:-4].replace("_", "/", 1).endswith("/USDT")
        and f[:-4].replace("_", "/", 1) not in bt.IGNORED_COINS
    ]
    print(f"Cache: {len(symbols)} coin")

    if "BTC/USDT" in symbols:
        symbols.remove("BTC/USDT")
    symbols.insert(0, "BTC/USDT")

    btc_raw = bt.load_or_fetch("BTC/USDT")
    if btc_raw is None:
        print("HATA: BTC verisi yok"); return

    print("--- Sinyal üretimi başlıyor ---\n")
    S, P, signals_detail = run_smc_v21(symbols, btc_raw)

    # İstatistikler
    cap_labels = {None: "limitsiz", 10_000: "max_10K", 20_000: "max_20K"}
    results = {}
    for scenario in ["half", "tp1", "tp2"]:
        signals = P[scenario]
        results[scenario] = {
            "stats":  S[scenario],
            "caps":   {}
        }
        for cap in CAPS:
            lbl = cap_labels[cap]
            final, _ = capped_simulate(signals, max_trade=cap)
            results[scenario]["caps"][lbl] = {
                "final":      final,
                "return_pct": round((final - INITIAL_CAP) / INITIAL_CAP * 100, 1)
            }

    # Çıktı oluştur
    lines = []
    header = f"=== smc_v21 | ALL | {START_DATE} → {END_DATE} | Çalıştırma: {datetime.now().strftime('%Y-%m-%d %H:%M')} ==="
    lines.append(header)
    lines.append("")
    lines.append(f"Parametreler  : CHoCH Bullish + vol≥{VOL_RATIO_MIN}x + BTC crash/downtrend filtre + 24h cooldown")
    lines.append(f"TP yapısı     : TP1=risk×1.0 | TP2=risk×2.0 | Stop=swing×0.995 (fallback entry×0.95)")
    lines.append(f"Başlangıç     : ${INITIAL_CAP:,.0f} | Max pozisyon: {MAX_POS}")
    lines.append(f"Veri          : {len(symbols)} coin, 1H OHLCV, Binance")
    lines.append("")

    sep = "─" * 72
    lines.append(sep)
    lines.append(f"{'Senaryo':<20} {'Sinyal':>8} {'WR%':>6} {'AvgP&L':>8} {'Limitsiz':>14} {'Max 10K':>14} {'Max 20K':>14}")
    lines.append(sep)

    mode_label = {"half": "actual (SMC trail)", "tp1": "TP1 Only", "tp2": "TP2 Only"}
    for sc in ["half", "tp1", "tp2"]:
        st  = S[sc]
        cap = results[sc]["caps"]
        lines.append(
            f"{'smc_v21_' + sc:<20}"
            f" {st['total']:>8}"
            f" {st['wr']:>5.1f}%"
            f" {st['avg_pnl']:>+7.3f}%"
            f" ${cap['limitsiz']['final']:>12,.0f}"
            f" ${cap['max_10K']['final']:>12,.0f}"
            f" ${cap['max_20K']['final']:>12,.0f}"
        )

    lines.append(sep)
    lines.append("")
    lines.append("--- Detay ---")
    for sc in ["half", "tp1", "tp2"]:
        st = S[sc]
        lines.append(f"\n[{mode_label[sc]}]")
        lines.append(f"  Toplam sinyal : {st['total']}")
        lines.append(f"  Win Rate      : %{st['wr']:.1f}")
        lines.append(f"  Kazanan       : {st['wins']} | Kısmi: {st.get('partial',0)} | Kaybeden: {st['losses']} | Süreli: {st['expired']}")
        lines.append(f"  Ort P&L       : {st['avg_pnl']:+.3f}%")
        for cap in CAPS:
            lbl = cap_labels[cap]
            r = results[sc]["caps"][lbl]
            lines.append(f"  {lbl:<12}: ${r['final']:>12,.0f}  ({r['return_pct']:+.1f}%)")

    output = "\n".join(lines)
    print("\n" + output)

    with open(out_txt, "w", encoding="utf-8") as f:
        f.write(output)
    print(f"\n✓ {out_txt}")

    with open(out_json, "w", encoding="utf-8") as f:
        json.dump({"run_date": datetime.now().isoformat(),
                   "params": {"vol_ratio_min": VOL_RATIO_MIN, "cooldown_h": COOLDOWN_H,
                              "initial_cap": INITIAL_CAP, "max_pos": MAX_POS},
                   "results": results}, f, indent=2, ensure_ascii=False)
    print(f"✓ {out_json}")

    # Per-sinyal detay (loss pattern analizi)
    with open(out_signals, "w", encoding="utf-8") as f:
        json.dump(signals_detail, f, indent=2, ensure_ascii=False)
    print(f"✓ {out_signals}  ({len(signals_detail)} sinyal)")

    # HTML çıktı
    run_date_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    cap_labels_list = [None, 10_000, 20_000]
    cap_names = {None: "Limitsiz", 10_000: "Max 10K", 20_000: "Max 20K"}
    cap_keys  = {None: "limitsiz", 10_000: "max_10K", 20_000: "max_20K"}
    mode_label = {"half": "actual (SMC trail)", "tp1": "TP1 Only", "tp2": "TP2 Only"}
    colors = {"half": "#2a9d8f", "tp1": "#457b9d", "tp2": "#e9c46a"}

    rows = ""
    for sc in ["half", "tp1", "tp2"]:
        st = S[sc]
        cap = results[sc]["caps"]
        for c in cap_labels_list:
            lbl = cap_keys[c]
            final = cap[lbl]["final"]
            ret   = cap[lbl]["return_pct"]
            color = "#00c853" if ret >= 0 else "#d32f2f"
            cap_tag = cap_names[c]
            rows += (f'<tr><td>smc_v21_{sc}</td><td>{mode_label[sc]}</td>'
                     f'<td>{cap_tag}</td><td>{st["total"]}</td>'
                     f'<td>{st["wr"]:.1f}%</td>'
                     f'<td style="color:{color}">{st["avg_pnl"]:+.3f}%</td>'
                     f'<td style="color:{color}">{ret:+.1f}%</td>'
                     f'<td style="color:{color}">${final:,.0f}</td></tr>')

    # Equity chart verisi (limitsiz, half senaryosu)
    eq_datasets = []
    palette = {"half": "#2a9d8f", "tp1": "#457b9d", "tp2": "#e9c46a"}
    for sc in ["half", "tp1", "tp2"]:
        _, eq = capped_simulate(P[sc], max_trade=None)
        pts = [{"x": datetime.utcfromtimestamp(ts * 3600).strftime("%Y-%m-%d"), "y": round(v)}
               for ts, v in eq]
        col = palette[sc]
        eq_datasets.append(
            f'{{"label":"smc_v21_{sc} (limitsiz)","data":{json.dumps(pts)},'
            f'"borderColor":"{col}","backgroundColor":"{col}20",'
            f'"borderWidth":1.5,"pointRadius":0,"fill":false,"tension":0.1}}'
        )
    ds_js = "[" + ",".join(eq_datasets) + "]"

    html = f"""<!DOCTYPE html><html lang="tr"><head>
<meta charset="UTF-8"><title>SMC v21 Backtest</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#0d1117;color:#c9d1d9;font-family:-apple-system,monospace;padding:20px}}
h1{{color:#58a6ff;font-size:1.4rem;margin-bottom:6px}}
.meta{{color:#8b949e;font-size:0.8rem;background:#161b22;padding:10px;border-radius:6px;border:1px solid #30363d;margin-bottom:16px}}
.card{{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:14px;margin-bottom:16px;overflow-x:auto}}
table{{width:100%;border-collapse:collapse;font-size:0.82rem;min-width:700px}}
th{{background:#21262d;color:#8b949e;padding:8px 10px;text-align:left;position:sticky;top:0}}
td{{padding:7px 10px;border-bottom:1px solid #21262d}}
tr:hover td{{background:#1c2128}}
canvas{{max-height:440px}}
</style></head><body>
<h1>SMC v21 Backtest — CHoCH + vol≥{VOL_RATIO_MIN}x</h1>
<div class="meta">{run_date_str} | {len(symbols)} coin | 1H Binance | ${INITIAL_CAP:,.0f} başlangıç | Maks {MAX_POS} pozisyon | {START_DATE} → {END_DATE}<br>
Sizing: sermaye÷{MAX_POS} (cap 10K/20K) | BTC filtre | 24h cooldown | Stop=swing×0.995 | TP1=risk×1 | TP2=risk×2</div>
<div class="card"><table>
<thead><tr><th>Senaryo</th><th>Mod</th><th>Cap</th><th>Sinyal</th><th>WR%</th><th>Ort P&L%</th><th>Getiri%</th><th>Son Sermaye</th></tr></thead>
<tbody>{rows}</tbody></table></div>
<div class="card"><canvas id="ec"></canvas></div>
<script>
const ds={ds_js};
new Chart(document.getElementById('ec'),{{type:'line',data:{{datasets:ds}},options:{{
  responsive:true,interaction:{{mode:'index',intersect:false}},
  scales:{{x:{{type:'category',ticks:{{maxTicksLimit:12,color:'#8b949e'}},grid:{{color:'#21262d'}}}},
           y:{{ticks:{{color:'#8b949e',callback:v=>'$'+v.toLocaleString()}},grid:{{color:'#21262d'}}}}}},
  plugins:{{legend:{{labels:{{color:'#c9d1d9'}}}}}}
}}}});
</script></body></html>"""

    with open(out_html, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"✓ {out_html}")


if __name__ == "__main__":
    main()
