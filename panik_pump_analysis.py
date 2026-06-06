#!/usr/bin/env python3
"""
PANİK PUMP filtre analizi — bear market odaklı
F1: BTC 4H 20MA üstünde (bear içinde yerel yükseliş var mı?)
F2: BTC 1H RSI > 40 (BTC aynı anda panikliyor mu?)
F3: Coin RSI 30-50 arası (dip ama kırılmamış)
F4: Panik mumu > 3% düşüş (gerçek panik mi?)
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


def ma20(closes):
    if len(closes) < 20:
        return None
    return sum(closes[-20:]) / 20


def filter_stats(records, key, label):
    passed  = [r for r in records if r.get(key)]
    blocked = [r for r in records if not r.get(key)]
    def wr(g): return (sum(1 for r in g if r["is_win"]), len(g)) if g else (0, 0)
    pw, pt = wr(passed)
    bw, bt = wr(blocked)
    pr = pw / pt * 100 if pt else 0
    br = bw / bt * 100 if bt else 0
    print(f"\n{'─'*52}", flush=True)
    print(f"FİLTRE: {label}", flush=True)
    print(f"  Geçti  ({pt:2d} sinyal): {pw} kazanç / {pt-pw} kayıp → WR %{pr:.0f}", flush=True)
    print(f"  Engel. ({bt:2d} sinyal): {bw} kazanç / {bt-bw} kayıp → WR %{br:.0f}", flush=True)
    if pt > 0 and bt > 0:
        diff = pr - br
        if diff > 10:
            print(f"  → Filtre DEĞER KATIYOR (+{diff:.0f}pp)", flush=True)
        elif diff < -10:
            print(f"  → Filtre TERS ÇALIŞIYOR ({diff:.0f}pp)", flush=True)
        else:
            print(f"  → Filtre etkisiz ({diff:+.0f}pp)", flush=True)


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
            print(f"  {sym_short}: zaman yok", flush=True)
            continue

        coin_1h = fetch_ohlcv(symbol, open_time, "1h", 30)
        time.sleep(0.12)
        btc_1h  = fetch_ohlcv("BTCUSDT", open_time, "1h", 30)
        time.sleep(0.12)
        btc_4h  = fetch_ohlcv("BTCUSDT", open_time, "4h", 25)
        time.sleep(0.12)

        is_win = status in WIN_STATUSES or (status == "half_stopped" and pnl > 0)

        # F1: BTC 4H 20MA üstünde
        btc_ma4h = ma20(btc_4h["closes"]) if btc_4h else None
        f1 = (btc_4h["closes"][-1] > btc_ma4h) if btc_ma4h else None

        # F2: BTC 1H RSI > 40
        btc_rsi = calc_rsi(btc_1h["closes"]) if btc_1h and len(btc_1h["closes"]) >= 15 else None
        f2 = (btc_rsi > 40) if btc_rsi is not None else None

        # F3: Coin RSI 30-50 arası
        coin_rsi = calc_rsi(coin_1h["closes"]) if coin_1h and len(coin_1h["closes"]) >= 15 else None
        f3 = (30 <= coin_rsi <= 50) if coin_rsi is not None else None

        # F4: Panik mumu > 3% düşüş (son barın open→close)
        f4 = None
        if coin_1h and coin_1h["opens"] and coin_1h["closes"]:
            o, c = coin_1h["opens"][-1], coin_1h["closes"][-1]
            if o > 0:
                f4 = (o - c) / o * 100 > 3.0

        tag = "✅" if is_win else "❌"
        def fs(v): return ("↑" if v else "↓") if v is not None else "?"
        coin_rsi_s = f"cRSI:{coin_rsi:.0f}" if coin_rsi is not None else "cRSI:?"
        btc_rsi_s  = f"bRSI:{btc_rsi:.0f}"  if btc_rsi  is not None else "bRSI:?"
        print(f"  {tag} {sym_short:8} | {pnl:+6.1f}% | {coin_rsi_s} {btc_rsi_s} "
              f"| F1:{fs(f1)} F2:{fs(f2)} F3:{fs(f3)} F4:{fs(f4)}", flush=True)

        records.append({"symbol": sym_short, "is_win": is_win, "pnl": pnl,
                        "f1": f1, "f2": f2, "f3": f3, "f4": f4})

    if not records:
        print("Analiz edilecek sinyal yok.", flush=True)
        return

    total = len(records)
    wins  = sum(1 for r in records if r["is_win"])
    print(f"\n{'='*55}", flush=True)
    print(f"GENEL: {wins}/{total} kazanç → WR %{wins/total*100:.0f}", flush=True)

    filter_stats(records, "f1", "F1 — BTC 4H 20MA üstünde (yerel trend)")
    filter_stats(records, "f2", "F2 — BTC 1H RSI > 40 (BTC stabil)")
    filter_stats(records, "f3", "F3 — Coin RSI 30-50 (ideal dip zonu)")
    filter_stats(records, "f4", "F4 — Panik mumu > 3% düşüş")

    combos = [
        ("F1+F2",       lambda r: r.get("f1") and r.get("f2")),
        ("F2+F3",       lambda r: r.get("f2") and r.get("f3")),
        ("F1+F3",       lambda r: r.get("f1") and r.get("f3")),
        ("F2+F4",       lambda r: r.get("f2") and r.get("f4")),
        ("F1+F2+F3",    lambda r: r.get("f1") and r.get("f2") and r.get("f3")),
        ("F2+F3+F4",    lambda r: r.get("f2") and r.get("f3") and r.get("f4")),
        ("F1+F2+F3+F4", lambda r: r.get("f1") and r.get("f2") and r.get("f3") and r.get("f4")),
    ]
    print(f"\n{'='*55}", flush=True)
    print("KOMBİNASYONLAR:", flush=True)
    for name, fn in combos:
        combo = [r for r in records if fn(r)]
        if not combo:
            print(f"  {name}: sinyal yok", flush=True)
            continue
        cw = sum(1 for r in combo if r["is_win"])
        print(f"  {name}: {cw}/{len(combo)} → WR %{cw/len(combo)*100:.0f} "
              f"(eliyor {total-len(combo)}/{total})", flush=True)


if __name__ == "__main__":
    main()
