#!/usr/bin/env python3
"""
PANİK PUMP — Genişletilmiş Filtre Analizi v4
--file: yerel JSON dosyasından sinyal yükle
Yeni: hacim eşik sweep, çok-barlı yeşil, reversal pattern
"""
import os, time, requests, argparse, json
from datetime import datetime, timezone

PORTFOLIO_URL   = os.getenv("PORTFOLIO_URL", "") or "http://localhost:10000"
PORTFOLIO_TOKEN = os.getenv("PORTFOLIO_AUTH_TOKEN", "") or os.getenv("PORTFOLIO_TOKEN", "")
BINANCE_BASE    = "https://api.binance.com"

WIN_STATUSES = ("win_tp1", "win_tp2", "win_trail", "win_partial")
ALL_CLOSED   = WIN_STATUSES + ("loss", "expired", "half_stopped", "win_partial")


def fetch_signals():
    headers = {"Authorization": f"Bearer {PORTFOLIO_TOKEN}"}
    r = requests.get(f"{PORTFOLIO_URL}/api/signals", headers=headers, timeout=15)
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else data.get("signals", [])


def load_signals_from_file(filepath):
    with open(filepath) as f:
        data = json.load(f)
    return data if isinstance(data, list) else data.get("signals", [])


def parse_time_ms(sig):
    for key in ("open_time", "created_at", "timestamp"):
        val = sig.get(key)
        if not val:
            continue
        try:
            if isinstance(val, (int, float)):
                return int(val)
            dt = datetime.fromisoformat(str(val).replace("Z", "+00:00"))
            return int(dt.timestamp() * 1000)
        except Exception:
            continue
    return None


def fetch_ohlcv(symbol, open_time_ms, interval, limit):
    binance_sym = symbol.replace("/", "")
    if not binance_sym.endswith("USDT"):
        binance_sym += "USDT"
    ms_per_bar = {"1h": 3600000, "4h": 14400000}.get(interval, 3600000)
    start_time = open_time_ms - limit * ms_per_bar
    try:
        resp = requests.get(
            f"{BINANCE_BASE}/api/v3/klines",
            params={"symbol": binance_sym, "interval": interval,
                    "startTime": start_time, "endTime": open_time_ms, "limit": limit},
            timeout=10,
        )
        if resp.status_code != 200:
            return None
        klines = resp.json()
        if not klines:
            return None
        return {
            "opens":   [float(k[1]) for k in klines],
            "closes":  [float(k[4]) for k in klines],
            "volumes": [float(k[5]) for k in klines],
        }
    except Exception:
        return None


def calc_rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    return round(100 - 100 / (1 + avg_gain / avg_loss), 1)


def bar_drop(opens, closes, idx):
    o, c = opens[idx], closes[idx]
    return (o - c) / o * 100 if o > 0 else None


def row(records, label, fn):
    total   = len(records)
    base_wr = sum(1 for r in records if r["is_win"]) / total * 100
    combo   = [r for r in records if fn(r)]
    if not combo:
        print(f"  {label:<36} sinyal yok", flush=True)
        return
    cw   = sum(1 for r in combo if r["is_win"])
    wr   = cw / len(combo) * 100
    diff = wr - base_wr
    eli  = total - len(combo)
    mark = f"+{diff:.0f}pp ✅" if diff > 10 else (f"{diff:.0f}pp ❌" if diff < -10 else f"{diff:+.0f}pp —")
    print(f"  {label:<36} {cw}/{len(combo):2d} → WR %{wr:2.0f}  {mark}  (eliyor {eli}/{total})", flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", help="Sinyalleri yerel JSON dosyasından yükle")
    args = parser.parse_args()

    print(f"Portfolio URL: {PORTFOLIO_URL}", flush=True)

    if args.file:
        print(f"[--file] {args.file} dosyasından yükleniyor...", flush=True)
        try:
            signals = load_signals_from_file(args.file)
        except Exception as e:
            print(f"Dosya hatası: {e}", flush=True)
            return
    else:
        try:
            signals = fetch_signals()
        except Exception as e:
            print(f"API hatası: {e}", flush=True)
            return

    panik = [s for s in signals
             if s.get("sig_type") == "panik_pump" and s.get("status") in ALL_CLOSED]
    all_panik = sum(1 for s in signals if s.get("sig_type") == "panik_pump")
    print(f"PANİK PUMP: toplam={all_panik} | kapalı={len(panik)}", flush=True)
    if not panik:
        print("Kapalı sinyal yok.", flush=True)
        return

    records = []
    for sig in panik:
        symbol    = sig.get("symbol", "")
        sym_short = symbol.replace("/USDT", "")
        status    = sig.get("status", "")
        pnl       = float(sig.get("close_pct") or 0)
        open_time = parse_time_ms(sig)
        if not open_time:
            continue

        coin_1h = fetch_ohlcv(symbol,    open_time, "1h", 30)
        time.sleep(0.12)
        btc_1h  = fetch_ohlcv("BTCUSDT", open_time, "1h", 30)
        time.sleep(0.12)

        is_win   = status in WIN_STATUSES or (status == "half_stopped" and pnl > 0)
        coin_rsi = calc_rsi(coin_1h["closes"]) if coin_1h and len(coin_1h["closes"]) >= 15 else None
        btc_rsi  = calc_rsi(btc_1h["closes"])  if btc_1h  and len(btc_1h["closes"])  >= 15 else None

        opens  = coin_1h["opens"]  if coin_1h else []
        closes = coin_1h["closes"] if coin_1h else []
        vols   = coin_1h.get("volumes", []) if coin_1h else []

        # Son 3 barın düşüşü: pozitif = kırmızı, negatif/sıfır = yeşil
        d1 = bar_drop(opens, closes, -1) if len(opens) >= 1 else None  # son bar
        d2 = bar_drop(opens, closes, -2) if len(opens) >= 2 else None  # bir önceki
        d3 = bar_drop(opens, closes, -3) if len(opens) >= 3 else None  # iki önceki

        # 4-bar kümülatif düşüş (coin)
        cum_drop = None
        if len(closes) >= 5:
            c4, c0 = closes[-5], closes[-1]
            if c4 > 0:
                cum_drop = (c4 - c0) / c4 * 100

        # Hacim oranı
        vol_ratio = None
        if len(vols) >= 11:
            avg_vol = sum(vols[-11:-1]) / 10
            if avg_vol > 0:
                vol_ratio = vols[-1] / avg_vol

        # ── Filtreler ──────────────────────────────────────────
        # Bağlam
        f2  = (btc_rsi > 40)        if btc_rsi  is not None else None
        f3a = (30 <= coin_rsi <= 50) if coin_rsi is not None else None

        # v2: coin mum büyüklüğü
        f4t_neg = (d1 <= 0)   if d1 is not None else None
        f4t_05  = (d1 < 0.5)  if d1 is not None else None
        f4t_10  = (d1 < 1.0)  if d1 is not None else None
        f4t_15  = (d1 < 1.5)  if d1 is not None else None

        # v2: kümülatif düşüş
        f5t_4 = (cum_drop < 4.0) if cum_drop is not None else None
        f5t_6 = (cum_drop < 6.0) if cum_drop is not None else None

        # v4: hacim eşik sweep
        f8_05  = (vol_ratio < 0.5) if vol_ratio is not None else None
        f8_08  = (vol_ratio < 0.8) if vol_ratio is not None else None
        f8_10  = (vol_ratio < 1.0) if vol_ratio is not None else None
        f8_12  = (vol_ratio < 1.2) if vol_ratio is not None else None
        f8_15  = (vol_ratio < 1.5) if vol_ratio is not None else None
        f8_hi2 = (vol_ratio > 2.0) if vol_ratio is not None else None
        f8_hi3 = (vol_ratio > 3.0) if vol_ratio is not None else None

        # v4: çok-barlı yön
        f9_bar2g = (d2 <= 0) if d2 is not None else None  # bar[-2] yeşil
        f9_bar3g = (d3 <= 0) if d3 is not None else None  # bar[-3] yeşil
        f9_2cons = (d1 <= 0 and d2 <= 0) if (d1 is not None and d2 is not None) else None  # son 2 bar yeşil
        f9_1of2  = (d1 <= 0 or  d2 <= 0) if (d1 is not None and d2 is not None) else None  # 2 bardan biri yeşil

        # v4: reversal pattern — önceki bar kırmızı, son bar yeşil (klasik dönüş)
        f9_rev1  = (d2 > 1.0 and d1 <= 0) if (d2 is not None and d1 is not None) else None
        f9_rev2  = (d2 > 2.0 and d1 <= 0) if (d2 is not None and d1 is not None) else None
        f9_rev3  = (d2 > 3.0 and d1 <= 0) if (d2 is not None and d1 is not None) else None
        # sadece son bar yeşil, önceki kırmızıydı (dönüş ilk barı)
        f9_fresh = (d2 > 0 and d1 <= 0)   if (d2 is not None and d1 is not None) else None

        tag = "✅" if is_win else "❌"
        d1s = f"d1:{d1:+.1f}%" if d1 is not None else "d1:?"
        d2s = f"d2:{d2:+.1f}%" if d2 is not None else "d2:?"
        vrs = f"vr:{vol_ratio:.1f}x" if vol_ratio is not None else "vr:?"
        print(f"  {tag} {sym_short:8} | {pnl:+6.1f}% | {d1s} {d2s} {vrs}", flush=True)

        records.append({
            "symbol": sym_short, "is_win": is_win, "pnl": pnl,
            "f2": f2, "f3a": f3a,
            "f4t_neg": f4t_neg, "f4t_05": f4t_05, "f4t_10": f4t_10, "f4t_15": f4t_15,
            "f5t_4": f5t_4, "f5t_6": f5t_6,
            "f8_05": f8_05, "f8_08": f8_08, "f8_10": f8_10,
            "f8_12": f8_12, "f8_15": f8_15,
            "f8_hi2": f8_hi2, "f8_hi3": f8_hi3,
            "f9_bar2g": f9_bar2g, "f9_bar3g": f9_bar3g,
            "f9_2cons": f9_2cons, "f9_1of2": f9_1of2,
            "f9_rev1": f9_rev1, "f9_rev2": f9_rev2, "f9_rev3": f9_rev3,
            "f9_fresh": f9_fresh,
        })

    if not records:
        print("Analiz edilecek sinyal yok.", flush=True)
        return

    total = len(records)
    wins  = sum(1 for r in records if r["is_win"])
    print(f"\n{'='*72}", flush=True)
    print(f"GENEL: {wins}/{total} → WR %{wins/total*100:.0f}", flush=True)

    print(f"\n── BAĞLAM {'─'*62}", flush=True)
    row(records, "F2  BTC RSI>40",               lambda r: r.get("f2"))
    row(records, "F3a Coin RSI 30-50",            lambda r: r.get("f3a"))

    print(f"\n── v2: COIN MUMU {'─'*54}", flush=True)
    row(records, "F4t mumu ≤ 0% (yeşil)",        lambda r: r.get("f4t_neg"))
    row(records, "F4t mumu < 0.5%",              lambda r: r.get("f4t_05"))
    row(records, "F4t mumu < 1.0%",              lambda r: r.get("f4t_10"))
    row(records, "F4t mumu < 1.5%",              lambda r: r.get("f4t_15"))
    row(records, "F5t cum < 4%",                 lambda r: r.get("f5t_4"))

    print(f"\n── v4: HACİM EŞİK SWEEP {'─'*46}", flush=True)
    row(records, "F8 vol < 0.5x (çok düşük)",   lambda r: r.get("f8_05"))
    row(records, "F8 vol < 0.8x",               lambda r: r.get("f8_08"))
    row(records, "F8 vol < 1.0x",               lambda r: r.get("f8_10"))
    row(records, "F8 vol < 1.2x",               lambda r: r.get("f8_12"))
    row(records, "F8 vol < 1.5x",               lambda r: r.get("f8_15"))
    row(records, "F8 vol > 2.0x (yüksek)",      lambda r: r.get("f8_hi2"))
    row(records, "F8 vol > 3.0x (çok yüksek)",  lambda r: r.get("f8_hi3"))

    print(f"\n── v4: ÇOK BARLI YEŞİL {'─'*47}", flush=True)
    row(records, "F9 bar[-2] yeşil",             lambda r: r.get("f9_bar2g"))
    row(records, "F9 bar[-3] yeşil",             lambda r: r.get("f9_bar3g"))
    row(records, "F9 son 2 bar yeşil (ardışık)", lambda r: r.get("f9_2cons"))
    row(records, "F9 son 2 bardan biri yeşil",   lambda r: r.get("f9_1of2"))

    print(f"\n── v4: REVERSAL PATTERN {'─'*46}", flush=True)
    row(records, "F9 rev: önceki>0% + son yeşil", lambda r: r.get("f9_fresh"))
    row(records, "F9 rev: önceki>1% + son yeşil", lambda r: r.get("f9_rev1"))
    row(records, "F9 rev: önceki>2% + son yeşil", lambda r: r.get("f9_rev2"))
    row(records, "F9 rev: önceki>3% + son yeşil", lambda r: r.get("f9_rev3"))

    print(f"\n── KOMBİNASYONLAR (v2 referans) {'─'*38}", flush=True)
    row(records, "F4t_neg + F5t_4",               lambda r: r.get("f4t_neg") and r.get("f5t_4"))
    row(records, "F4t_neg + F5t_4 + F2",          lambda r: r.get("f4t_neg") and r.get("f5t_4") and r.get("f2"))
    row(records, "F2 + F3a (önceki en iyi)",      lambda r: r.get("f2") and r.get("f3a"))

    print(f"\n── KOMBİNASYONLAR (hacim) {'─'*44}", flush=True)
    row(records, "F4t_neg + F8_05",               lambda r: r.get("f4t_neg") and r.get("f8_05"))
    row(records, "F4t_neg + F8_08",               lambda r: r.get("f4t_neg") and r.get("f8_08"))
    row(records, "F4t_neg + F8_10",               lambda r: r.get("f4t_neg") and r.get("f8_10"))
    row(records, "F4t_neg + F8_12",               lambda r: r.get("f4t_neg") and r.get("f8_12"))
    row(records, "F4t_neg + F8_15",               lambda r: r.get("f4t_neg") and r.get("f8_15"))
    row(records, "F5t_4 + F8_10",                 lambda r: r.get("f5t_4") and r.get("f8_10"))
    row(records, "F4t_neg + F5t_4 + F8_10",       lambda r: r.get("f4t_neg") and r.get("f5t_4") and r.get("f8_10"))

    print(f"\n── KOMBİNASYONLAR (multi-bar) {'─'*40}", flush=True)
    row(records, "F4t_neg + F9_bar2g",            lambda r: r.get("f4t_neg") and r.get("f9_bar2g"))
    row(records, "F9_2cons (her iki bar yeşil)",  lambda r: r.get("f9_2cons"))
    row(records, "F9_2cons + F5t_4",              lambda r: r.get("f9_2cons") and r.get("f5t_4"))
    row(records, "F9_2cons + F2",                 lambda r: r.get("f9_2cons") and r.get("f2"))
    row(records, "F9_1of2 + F5t_4",              lambda r: r.get("f9_1of2") and r.get("f5t_4"))

    print(f"\n── KOMBİNASYONLAR (reversal) {'─'*41}", flush=True)
    row(records, "F9_rev1 (panik>1% → yeşil)",   lambda r: r.get("f9_rev1"))
    row(records, "F9_rev2 (panik>2% → yeşil)",   lambda r: r.get("f9_rev2"))
    row(records, "F9_rev1 + F5t_4",              lambda r: r.get("f9_rev1") and r.get("f5t_4"))
    row(records, "F9_rev1 + F2",                 lambda r: r.get("f9_rev1") and r.get("f2"))
    row(records, "F9_rev2 + F2",                 lambda r: r.get("f9_rev2") and r.get("f2"))
    row(records, "F9_fresh + F5t_4",             lambda r: r.get("f9_fresh") and r.get("f5t_4"))
    row(records, "F9_fresh + F8_10",             lambda r: r.get("f9_fresh") and r.get("f8_10"))


if __name__ == "__main__":
    main()
