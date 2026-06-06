#!/usr/bin/env python3
"""
PANİK PUMP — Genişletilmiş Filtre Analizi v3
--file: yerel JSON dosyasından sinyal yükle
Yeni filtreler: BTC yönü (F6t), RSI momentum (F7), hacim (F8)
"""
import os, time, requests, argparse, json
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


def row(records, label, fn):
    total   = len(records)
    base_wr = sum(1 for r in records if r["is_win"]) / total * 100
    combo   = [r for r in records if fn(r)]
    if not combo:
        print(f"  {label:<34} sinyal yok", flush=True)
        return
    cw   = sum(1 for r in combo if r["is_win"])
    wr   = cw / len(combo) * 100
    diff = wr - base_wr
    eli  = total - len(combo)
    mark = f"+{diff:.0f}pp ✅" if diff > 10 else (f"{diff:.0f}pp ❌" if diff < -10 else f"{diff:+.0f}pp —")
    print(f"  {label:<34} {cw}/{len(combo):2d} → WR %{wr:2.0f}  {mark}  (eliyor {eli}/{total})", flush=True)


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

        # RSI momentum: RSI şu an vs 3 bar önceki RSI
        coin_rsi_3ago = calc_rsi(coin_1h["closes"][:-3]) if coin_1h and len(coin_1h["closes"]) >= 18 else None
        rsi_rising    = (coin_rsi > coin_rsi_3ago) if (coin_rsi is not None and coin_rsi_3ago is not None) else None

        # Coin son mum ve 4-bar kümülatif
        candle_drop = None
        if coin_1h and coin_1h["opens"]:
            o, c = coin_1h["opens"][-1], coin_1h["closes"][-1]
            if o > 0:
                candle_drop = (o - c) / o * 100

        cum_drop = None
        if coin_1h and len(coin_1h["closes"]) >= 5:
            c4, c0 = coin_1h["closes"][-5], coin_1h["closes"][-1]
            if c4 > 0:
                cum_drop = (c4 - c0) / c4 * 100

        # BTC son mum ve 4-bar kümülatif
        btc_candle_drop = None
        if btc_1h and btc_1h["opens"]:
            o, c = btc_1h["opens"][-1], btc_1h["closes"][-1]
            if o > 0:
                btc_candle_drop = (o - c) / o * 100

        btc_cum_drop = None
        if btc_1h and len(btc_1h["closes"]) >= 5:
            c4, c0 = btc_1h["closes"][-5], btc_1h["closes"][-1]
            if c4 > 0:
                btc_cum_drop = (c4 - c0) / c4 * 100

        # Hacim oranı: son bar / önceki 10 bar ortalaması
        vol_ratio = None
        vols = coin_1h.get("volumes", []) if coin_1h else []
        if len(vols) >= 11:
            avg_vol = sum(vols[-11:-1]) / 10
            if avg_vol > 0:
                vol_ratio = vols[-1] / avg_vol

        # ── Filtreler ──────────────────────────────────────────────
        # Bağlam (v2)
        f2   = (btc_rsi > 40)         if btc_rsi     is not None else None
        f3a  = (30 <= coin_rsi <= 50)  if coin_rsi    is not None else None

        # Ters: coin mum büyüklüğü (v2)
        f4t_neg = (candle_drop <= 0)   if candle_drop is not None else None
        f4t_05  = (candle_drop < 0.5)  if candle_drop is not None else None
        f4t_10  = (candle_drop < 1.0)  if candle_drop is not None else None
        f4t_15  = (candle_drop < 1.5)  if candle_drop is not None else None

        # Ters: coin kümülatif düşüş (v2)
        f5t_neg = (cum_drop <= 0)      if cum_drop    is not None else None
        f5t_2   = (cum_drop < 2.0)     if cum_drop    is not None else None
        f5t_4   = (cum_drop < 4.0)     if cum_drop    is not None else None
        f5t_6   = (cum_drop < 6.0)     if cum_drop    is not None else None

        # YENİ: BTC yönü (F6t)
        f6t_neg = (btc_candle_drop <= 0) if btc_candle_drop is not None else None
        f6t_2   = (btc_cum_drop < 2.0)   if btc_cum_drop    is not None else None
        f6t_4   = (btc_cum_drop < 4.0)   if btc_cum_drop    is not None else None

        # YENİ: RSI momentum (F7)
        f7_rising = rsi_rising

        # YENİ: Hacim (F8)
        f8_hi  = (vol_ratio > 1.5) if vol_ratio is not None else None  # yüksek panik hacmi
        f8_lo  = (vol_ratio < 1.5) if vol_ratio is not None else None  # normal hacim
        f8_lo1 = (vol_ratio < 1.0) if vol_ratio is not None else None  # ortalamanın altı

        tag = "✅" if is_win else "❌"
        cr = f"cRSI:{coin_rsi:.0f}"   if coin_rsi    is not None else "cRSI:?"
        br = f"bRSI:{btc_rsi:.0f}"    if btc_rsi     is not None else "bRSI:?"
        cd = f"cd:{candle_drop:.1f}%" if candle_drop is not None else "cd:?"
        cm = f"cm:{cum_drop:.1f}%"    if cum_drop    is not None else "cm:?"
        vr = f"vr:{vol_ratio:.1f}x"   if vol_ratio   is not None else "vr:?"
        print(f"  {tag} {sym_short:8} | {pnl:+6.1f}% | {cr} {br} {cd} {cm} {vr}", flush=True)

        records.append({
            "symbol": sym_short, "is_win": is_win, "pnl": pnl,
            "f2": f2, "f3a": f3a,
            "f4t_neg": f4t_neg, "f4t_05": f4t_05, "f4t_10": f4t_10, "f4t_15": f4t_15,
            "f5t_neg": f5t_neg, "f5t_2": f5t_2, "f5t_4": f5t_4, "f5t_6": f5t_6,
            "f6t_neg": f6t_neg, "f6t_2": f6t_2, "f6t_4": f6t_4,
            "f7_rising": f7_rising,
            "f8_hi": f8_hi, "f8_lo": f8_lo, "f8_lo1": f8_lo1,
        })

    if not records:
        print("Analiz edilecek sinyal yok.", flush=True)
        return

    total = len(records)
    wins  = sum(1 for r in records if r["is_win"])
    print(f"\n{'='*72}", flush=True)
    print(f"GENEL: {wins}/{total} → WR %{wins/total*100:.0f}", flush=True)

    print(f"\n── BAĞLAM {'─'*62}", flush=True)
    row(records, "F2  BTC RSI>40",             lambda r: r.get("f2"))
    row(records, "F3a Coin RSI 30-50",          lambda r: r.get("f3a"))

    print(f"\n── TERS: COIN MUMU {'─'*52}", flush=True)
    row(records, "F4t mumu ≤ 0% (yeşil)",      lambda r: r.get("f4t_neg"))
    row(records, "F4t mumu < 0.5%",            lambda r: r.get("f4t_05"))
    row(records, "F4t mumu < 1.0%",            lambda r: r.get("f4t_10"))
    row(records, "F4t mumu < 1.5%",            lambda r: r.get("f4t_15"))

    print(f"\n── TERS: COIN KÜMÜLATİF {'─'*46}", flush=True)
    row(records, "F5t cum ≤ 0% (yükselen)",    lambda r: r.get("f5t_neg"))
    row(records, "F5t cum < 2%",               lambda r: r.get("f5t_2"))
    row(records, "F5t cum < 4%",               lambda r: r.get("f5t_4"))
    row(records, "F5t cum < 6%",               lambda r: r.get("f5t_6"))

    print(f"\n── YENİ: BTC YÖNÜ (F6t) {'─'*45}", flush=True)
    row(records, "F6t BTC mumu yeşil",          lambda r: r.get("f6t_neg"))
    row(records, "F6t BTC cum < 2%",            lambda r: r.get("f6t_2"))
    row(records, "F6t BTC cum < 4%",            lambda r: r.get("f6t_4"))

    print(f"\n── YENİ: RSI MOMENTUM (F7) {'─'*43}", flush=True)
    row(records, "F7  RSI yükseliyor",          lambda r: r.get("f7_rising"))

    print(f"\n── YENİ: HACİM (F8) {'─'*50}", flush=True)
    row(records, "F8  vol > 1.5x (yüksek)",    lambda r: r.get("f8_hi"))
    row(records, "F8  vol < 1.5x (normal)",    lambda r: r.get("f8_lo"))
    row(records, "F8  vol < 1.0x (düşük)",     lambda r: r.get("f8_lo1"))

    print(f"\n── KOMBİNASYONLAR (v2 güçlüler) {'─'*37}", flush=True)
    row(records, "F4t_neg + F5t_4",             lambda r: r.get("f4t_neg") and r.get("f5t_4"))
    row(records, "F4t_neg + F5t_4 + F2",        lambda r: r.get("f4t_neg") and r.get("f5t_4") and r.get("f2"))
    row(records, "F4t_neg + F2 + F3a",          lambda r: r.get("f4t_neg") and r.get("f2") and r.get("f3a"))
    row(records, "F5t_4 + F2 + F3a",            lambda r: r.get("f5t_4") and r.get("f2") and r.get("f3a"))
    row(records, "F2 + F3a (önceki en iyi)",    lambda r: r.get("f2") and r.get("f3a"))

    print(f"\n── KOMBİNASYONLAR (yeni F6/F7/F8) {'─'*34}", flush=True)
    row(records, "F4t_neg + F6t_neg",            lambda r: r.get("f4t_neg") and r.get("f6t_neg"))
    row(records, "F4t_neg + F6t_neg + F2",       lambda r: r.get("f4t_neg") and r.get("f6t_neg") and r.get("f2"))
    row(records, "F5t_4 + F6t_4",               lambda r: r.get("f5t_4") and r.get("f6t_4"))
    row(records, "F4t_neg + F7_rising",          lambda r: r.get("f4t_neg") and r.get("f7_rising"))
    row(records, "F5t_4 + F7_rising",            lambda r: r.get("f5t_4") and r.get("f7_rising"))
    row(records, "F4t_neg + F8_hi",              lambda r: r.get("f4t_neg") and r.get("f8_hi"))
    row(records, "F4t_neg + F8_lo",              lambda r: r.get("f4t_neg") and r.get("f8_lo"))
    row(records, "F4t_neg + F5t_4 + F6t_neg",   lambda r: r.get("f4t_neg") and r.get("f5t_4") and r.get("f6t_neg"))
    row(records, "F4t_neg + F5t_4 + F7",        lambda r: r.get("f4t_neg") and r.get("f5t_4") and r.get("f7_rising"))
    row(records, "F4t_neg + F5t_4 + F8_hi",     lambda r: r.get("f4t_neg") and r.get("f5t_4") and r.get("f8_hi"))
    row(records, "F4t+F5t+F6t+F2",              lambda r: r.get("f4t_neg") and r.get("f5t_4") and r.get("f6t_neg") and r.get("f2"))
    row(records, "F4t+F5t+F7+F2",               lambda r: r.get("f4t_neg") and r.get("f5t_4") and r.get("f7_rising") and r.get("f2"))


if __name__ == "__main__":
    main()
