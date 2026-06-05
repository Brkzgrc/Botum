#!/usr/bin/env python3
"""
SMC sinyal analizi — 3 filtre karşılaştırması
  F1: +DI > -DI (yön)
  F2: BTC 20MA üstünde
  F3: 4H trend hizalaması (coin 4H 20MA üstünde)
Kullanım: python smc_adx_analysis.py
"""
import os, time, requests
from datetime import datetime, timezone

PORTFOLIO_URL   = os.getenv("PORTFOLIO_URL", "") or "http://localhost:10000"
PORTFOLIO_TOKEN = os.getenv("PORTFOLIO_AUTH_TOKEN", "") or os.getenv("PORTFOLIO_TOKEN", "")
BINANCE_BASE    = "https://api.binance.com"
ADX_PERIOD      = 14


def fetch_signals():
    headers = {"Authorization": f"Bearer {PORTFOLIO_TOKEN}"}
    r = requests.get(f"{PORTFOLIO_URL}/api/signals", headers=headers, timeout=15)
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else data.get("signals", [])


def fetch_ohlcv(symbol, open_time_ms, interval="1h", limit=60):
    binance_sym = symbol if symbol.endswith("USDT") else symbol + "USDT"
    start_time  = open_time_ms - limit * (3600000 if interval == "1h" else 14400000)
    end_time    = open_time_ms + (3600000 if interval == "1h" else 14400000)
    try:
        resp = requests.get(
            f"{BINANCE_BASE}/api/v3/klines",
            params={"symbol": binance_sym, "interval": interval,
                    "startTime": start_time, "endTime": end_time, "limit": limit + 2},
            timeout=10,
        )
        if resp.status_code != 200:
            return None
        klines = resp.json()
        if not klines:
            return None
        return {
            "highs":  [float(k[2]) for k in klines],
            "lows":   [float(k[3]) for k in klines],
            "closes": [float(k[4]) for k in klines],
        }
    except Exception:
        return None


def calc_indicators(highs, lows, closes, period=ADX_PERIOD):
    """ADX + son bar +DI/-DI döndür."""
    n = len(closes)
    if n < period * 2 + 2:
        return None, None, None
    tr_list, pdm_list, ndm_list = [], [], []
    for i in range(1, n):
        h, l, pc = highs[i], lows[i], closes[i - 1]
        tr  = max(h - l, abs(h - pc), abs(l - pc))
        up  = highs[i] - highs[i - 1]
        dn  = lows[i - 1] - lows[i]
        pdm = up if up > dn and up > 0 else 0
        ndm = dn if dn > up and dn > 0 else 0
        tr_list.append(tr); pdm_list.append(pdm); ndm_list.append(ndm)

    def wilder(data, p):
        s = [sum(data[:p])]
        for v in data[p:]:
            s.append(s[-1] - s[-1] / p + v)
        return s

    atr_s = wilder(tr_list,  period)
    pdm_s = wilder(pdm_list, period)
    ndm_s = wilder(ndm_list, period)

    dx_list = []
    for a, p_, n_ in zip(atr_s, pdm_s, ndm_s):
        if a == 0:
            continue
        pdi = 100 * p_ / a
        ndi = 100 * n_ / a
        dx_list.append(100 * abs(pdi - ndi) / (pdi + ndi) if (pdi + ndi) > 0 else 0)

    if len(dx_list) < period:
        return None, None, None

    adx = sum(dx_list[:period]) / period
    for v in dx_list[period:]:
        adx = (adx * (period - 1) + v) / period

    # Son bar için +DI / -DI
    last_atr = atr_s[-1]
    last_pdi = round(100 * pdm_s[-1] / last_atr, 2) if last_atr else 0
    last_ndi = round(100 * ndm_s[-1] / last_atr, 2) if last_atr else 0

    return round(adx, 2), last_pdi, last_ndi


def ma20(closes):
    if len(closes) < 20:
        return closes[-1]
    return sum(closes[-20:]) / 20


def parse_time_ms(sig):
    for key in ("open_time", "created_at", "timestamp", "entry_time"):
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


def filter_stats(records, key, label):
    passed  = [r for r in records if r.get(key)]
    blocked = [r for r in records if not r.get(key)]
    def wr(group):
        if not group: return 0, 0
        w = sum(1 for r in group if r["is_win"])
        return w, len(group)
    pw, pt = wr(passed)
    bw, bt = wr(blocked)
    pr = pw/pt*100 if pt else 0
    br = bw/bt*100 if bt else 0
    print(f"\n{'─'*50}", flush=True)
    print(f"FİLTRE: {label}", flush=True)
    print(f"  Geçti  ({pt:2d} sinyal): {pw} kazanç / {pt-pw} kayıp → WR %{pr:.0f}", flush=True)
    print(f"  Engel. ({bt:2d} sinyal): {bw} kazanç / {bt-bw} kayıp → WR %{br:.0f}", flush=True)
    if pt > 0:
        diff = pr - br
        if diff > 10:
            print(f"  → Filtre DEĞER KATIY OR (+{diff:.0f}pp)", flush=True)
        elif diff < -10:
            print(f"  → Filtre TERS ÇALIŞIYOR ({diff:.0f}pp)", flush=True)
        else:
            print(f"  → Filtre etkisiz ({diff:+.0f}pp)", flush=True)


def main():
    print(f"Portfolio URL: {PORTFOLIO_URL}", flush=True)
    print("Sinyaller çekiliyor...", flush=True)
    try:
        signals = fetch_signals()
    except Exception as e:
        print(f"API hatası: {e}", flush=True)
        return

    SMC_SOURCES     = ("smc", "smc-original", "smc-trailing", "smc-momentum")
    CLOSED_STATUSES = ("loss", "win_tp1", "win_tp2", "win_trail", "win_partial",
                       "half_stopped", "half_expired", "expired")
    WIN_STATUSES    = ("win_tp1", "win_tp2", "win_trail", "win_partial")

    smc = [s for s in signals
           if s.get("source") in SMC_SOURCES and s.get("status") in CLOSED_STATUSES]

    all_smc = sum(1 for s in signals if s.get("source") in SMC_SOURCES)
    print(f"SMC: toplam={all_smc} | kapalı={len(smc)} / tüm={len(signals)}", flush=True)
    if not smc:
        print("Kapalı SMC sinyali bulunamadı.", flush=True)
        return

    records = []

    for sig in smc:
        symbol    = sig.get("symbol", "").replace("/USDT", "").replace("USDT", "")
        status    = sig.get("status", "")
        pnl       = float(sig.get("close_pct") or 0)
        open_time = parse_time_ms(sig)

        if not open_time:
            print(f"  {symbol}: zaman bilgisi yok", flush=True)
            continue

        # 1H OHLCV — ADX + +DI/-DI
        ohlcv_1h = fetch_ohlcv(symbol, open_time, "1h", 60)
        time.sleep(0.15)

        if not ohlcv_1h or len(ohlcv_1h["closes"]) < ADX_PERIOD * 2 + 2:
            print(f"  {symbol}: 1H OHLCV yetersiz", flush=True)
            continue

        adx, pdi, ndi = calc_indicators(ohlcv_1h["highs"], ohlcv_1h["lows"], ohlcv_1h["closes"])
        if adx is None:
            print(f"  {symbol}: ADX hesaplanamadı", flush=True)
            continue

        # BTC 1H — 20MA kontrolü
        btc_1h = fetch_ohlcv("BTC", open_time, "1h", 30)
        time.sleep(0.15)
        btc_above_ma = None
        if btc_1h and len(btc_1h["closes"]) >= 20:
            btc_ma = ma20(btc_1h["closes"])
            btc_above_ma = btc_1h["closes"][-1] > btc_ma

        # 4H OHLCV — 20MA kontrolü
        ohlcv_4h = fetch_ohlcv(symbol, open_time, "4h", 30)
        time.sleep(0.15)
        above_4h_ma = None
        if ohlcv_4h and len(ohlcv_4h["closes"]) >= 20:
            ma_4h = ma20(ohlcv_4h["closes"])
            above_4h_ma = ohlcv_4h["closes"][-1] > ma_4h

        is_win   = status in WIN_STATUSES or (status == "half_stopped" and pnl > 0)
        f1_pass  = pdi > ndi if pdi is not None else None
        f2_pass  = btc_above_ma
        f3_pass  = above_4h_ma

        tag = "✅" if is_win else "❌"
        f1s = ("↑" if f1_pass else "↓") if f1_pass is not None else "?"
        f2s = ("↑" if f2_pass else "↓") if f2_pass is not None else "?"
        f3s = ("↑" if f3_pass else "↓") if f3_pass is not None else "?"
        print(f"  {tag} {symbol:8} | {pnl:+6.1f}% | ADX:{adx:4.1f} "
              f"| F1(+DI>-DI):{f1s} +{pdi:.1f}/-{ndi:.1f} "
              f"| F2(BTC-MA):{f2s} | F3(4H-MA):{f3s}", flush=True)

        records.append({
            "symbol": symbol, "is_win": is_win, "pnl": pnl,
            "adx": adx, "pdi": pdi, "ndi": ndi,
            "f1": f1_pass, "f2": f2_pass, "f3": f3_pass,
        })

    if not records:
        print("Analiz edilecek sinyal yok.", flush=True)
        return

    total  = len(records)
    wins   = sum(1 for r in records if r["is_win"])
    print(f"\n{'='*55}", flush=True)
    print(f"GENEL: {wins}/{total} kazanç → WR %{wins/total*100:.0f}", flush=True)

    filter_stats(records, "f1", "F1 — +DI > -DI (1H yön yukarı)")
    filter_stats(records, "f2", "F2 — BTC 1H 20MA üstünde")
    filter_stats(records, "f3", "F3 — Coin 4H 20MA üstünde")

    # Kombinasyon: 3 filtre birden
    combo = [r for r in records if r.get("f1") and r.get("f2") and r.get("f3")]
    if combo:
        cw = sum(1 for r in combo if r["is_win"])
        print(f"\n{'─'*50}", flush=True)
        print(f"KOMBİNASYON F1+F2+F3: {cw}/{len(combo)} → WR %{cw/len(combo)*100:.0f}", flush=True)
        print(f"  (Filtre {total - len(combo)}/{total} sinyali eliyor)", flush=True)


if __name__ == "__main__":
    main()
