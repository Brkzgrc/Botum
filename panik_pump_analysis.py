#!/usr/bin/env python3
"""
PANİK PUMP kapsamlı filtre analizi
F2:  BTC 1H RSI > 40
F3a: Coin RSI 30-50
F3b: Coin RSI 25-55 (geniş)
F4a: Panik mumu > 1.5%
F4b: Panik mumu > 2.0%
F5:  Son 4 barın kümülatif düşüşü > 4%
"""
import os, time, requests
from datetime import datetime, timezone

PORTFOLIO_URL   = os.getenv("PORTFOLIO_URL", "") or "http://localhost:10000"
PORTFOLIO_TOKEN = os.getenv("PORTFOLIO_AUTH_TOKEN", "") or os.getenv("PORTFOLIO_TOKEN", "")
BINANCE_BASE    = "https://api.binance.com"

WIN_STATUSES = ("win_tp1", "win_tp2", "win_trail", "win_partial")
ALL_CLOSED   = WIN_STATUSES + ("loss", "expired", "half_stopped", "half_expired")


def fetch_signals():
    headers = {"Authorization": f"Bearer {PORTFOLIO_TOKEN}"}
    r = requests.get(f"{PORTFOLIO_URL}/api/signals", headers=headers, timeout=15)
    r.raise_for_status()
    data = r.json()
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
    ms_per_bar = {"1h": 3600000, "4h": 14400000, "1d": 86400000}.get(interval, 3600000)
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


def filter_stats(records, key, label):
    passed  = [r for r in records if r.get(key)]
    blocked = [r for r in records if not r.get(key)]
    def wr(g): return (sum(1 for r in g if r["is_win"]), len(g)) if g else (0, 0)
    pw, pt = wr(passed)
    bw, bt = wr(blocked)
    pr = pw / pt * 100 if pt else 0
    br = bw / bt * 100 if bt else 0
    diff = pr - br if (pt > 0 and bt > 0) else None
    marker = ""
    if diff is not None:
        if diff > 10:    marker = f"  ✅ DEĞER KATIYOR (+{diff:.0f}pp)"
        elif diff < -10: marker = f"  ❌ TERS ÇALIŞIYOR ({diff:.0f}pp)"
        else:            marker = f"  — etkisiz ({diff:+.0f}pp)"
    print(f"  {label}", flush=True)
    print(f"    Geçti ({pt:2d}): WR %{pr:.0f}  |  Engel. ({bt:2d}): WR %{br:.0f}{marker}", flush=True)


def combo_stats(records, label, fn):
    total = len(records)
    combo = [r for r in records if fn(r)]
    if not combo:
        print(f"  {label}: sinyal yok", flush=True)
        return
    cw      = sum(1 for r in combo if r["is_win"])
    wr      = cw / len(combo) * 100
    eli     = total - len(combo)
    base_wr = sum(1 for r in records if r["is_win"]) / total * 100
    diff    = wr - base_wr
    marker  = f"  +{diff:.0f}pp ✅" if diff > 10 else (f"  {diff:.0f}pp ❌" if diff < -10 else f"  {diff:+.0f}pp")
    print(f"  {label}: {cw}/{len(combo)} → WR %{wr:.0f}{marker}  (eliyor {eli}/{total})", flush=True)


def main():
    print(f"Portfolio URL: {PORTFOLIO_URL}", flush=True)
    try:
        signals = fetch_signals()
    except Exception as e:
        print(f"API hatası: {e}", flush=True)
        return

    panik = [s for s in signals
             if s.get("sig_type") == "panik_pump" and s.get("status") in ALL_CLOSED]
    all_panik = sum(1 for s in signals if s.get("sig_type") == "panik_pump")
    print(f"PANİK PUMP: toplam={all_panik} | kapalı={len(panik)} / tüm={len(signals)}", flush=True)
    if not panik:
        print("Kapalı PANİK PUMP sinyali bulunamadı.", flush=True)
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

        candle_drop = None
        if coin_1h and len(coin_1h["opens"]) >= 1:
            o, c = coin_1h["opens"][-1], coin_1h["closes"][-1]
            if o > 0:
                candle_drop = (o - c) / o * 100

        cum_drop = None
        if coin_1h and len(coin_1h["closes"]) >= 5:
            c4 = coin_1h["closes"][-5]
            c0 = coin_1h["closes"][-1]
            if c4 > 0:
                cum_drop = (c4 - c0) / c4 * 100

        f2  = (btc_rsi > 40)         if btc_rsi      is not None else None
        f3a = (30 <= coin_rsi <= 50)  if coin_rsi     is not None else None
        f3b = (25 <= coin_rsi <= 55)  if coin_rsi     is not None else None
        f4a = (candle_drop > 1.5)     if candle_drop  is not None else None
        f4b = (candle_drop > 2.0)     if candle_drop  is not None else None
        f5  = (cum_drop > 4.0)        if cum_drop     is not None else None

        tag = "✅" if is_win else "❌"
        crsi = f"cRSI:{coin_rsi:.0f}" if coin_rsi     is not None else "cRSI:?"
        brsi = f"bRSI:{btc_rsi:.0f}"  if btc_rsi      is not None else "bRSI:?"
        cd   = f"drop:{candle_drop:.1f}%" if candle_drop is not None else "drop:?"
        cum  = f"cum:{cum_drop:.1f}%"     if cum_drop    is not None else "cum:?"
        def fs(v): return ("↑" if v else "↓") if v is not None else "?"
        print(f"  {tag} {sym_short:8} | {pnl:+6.1f}% | {crsi} {brsi} {cd} {cum} "
              f"| F2:{fs(f2)} F3a:{fs(f3a)} F3b:{fs(f3b)} F4a:{fs(f4a)} F4b:{fs(f4b)} F5:{fs(f5)}", flush=True)

        records.append({"symbol": sym_short, "is_win": is_win, "pnl": pnl,
                        "f2": f2, "f3a": f3a, "f3b": f3b,
                        "f4a": f4a, "f4b": f4b, "f5": f5})

    if not records:
        print("Analiz edilecek sinyal yok.", flush=True)
        return

    total = len(records)
    wins  = sum(1 for r in records if r["is_win"])
    print(f"\n{'='*60}", flush=True)
    print(f"GENEL: {wins}/{total} → WR %{wins/total*100:.0f}", flush=True)

    print(f"\n── TEK FİLTRELER {'─'*42}", flush=True)
    filter_stats(records, "f2",  "F2  — BTC 1H RSI > 40")
    filter_stats(records, "f3a", "F3a — Coin RSI 30-50")
    filter_stats(records, "f3b", "F3b — Coin RSI 25-55 (geniş)")
    filter_stats(records, "f4a", "F4a — Panik mumu > 1.5%")
    filter_stats(records, "f4b", "F4b — Panik mumu > 2.0%")
    filter_stats(records, "f5",  "F5  — Kümülatif 4 bar > 4%")

    print(f"\n── KOMBİNASYONLAR {'─'*41}", flush=True)
    combo_stats(records, "F2+F3a",     lambda r: r.get("f2") and r.get("f3a"))
    combo_stats(records, "F2+F3b",     lambda r: r.get("f2") and r.get("f3b"))
    combo_stats(records, "F2+F4a",     lambda r: r.get("f2") and r.get("f4a"))
    combo_stats(records, "F2+F4b",     lambda r: r.get("f2") and r.get("f4b"))
    combo_stats(records, "F2+F5",      lambda r: r.get("f2") and r.get("f5"))
    combo_stats(records, "F3a+F4a",    lambda r: r.get("f3a") and r.get("f4a"))
    combo_stats(records, "F3a+F5",     lambda r: r.get("f3a") and r.get("f5"))
    combo_stats(records, "F3b+F5",     lambda r: r.get("f3b") and r.get("f5"))
    combo_stats(records, "F2+F3a+F5",  lambda r: r.get("f2") and r.get("f3a") and r.get("f5"))
    combo_stats(records, "F2+F3b+F5",  lambda r: r.get("f2") and r.get("f3b") and r.get("f5"))
    combo_stats(records, "F2+F3a+F4a", lambda r: r.get("f2") and r.get("f3a") and r.get("f4a"))
    combo_stats(records, "F2+F3b+F4a", lambda r: r.get("f2") and r.get("f3b") and r.get("f4a"))


if __name__ == "__main__":
    main()
