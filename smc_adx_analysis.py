#!/usr/bin/env python3
"""
SMC CHoCH sinyal analizi — ADX filtre araştırması
Kullanım: python smc_adx_analysis.py
Render shell'den çalıştır.
"""
import os, json, time, requests
from datetime import datetime, timezone

# Portfolio shell'den çalışırken localhost:10000 fallback
PORTFOLIO_URL   = os.getenv("PORTFOLIO_URL", "") or "http://localhost:10000"
PORTFOLIO_TOKEN = os.getenv("PORTFOLIO_AUTH_TOKEN", "") or os.getenv("PORTFOLIO_TOKEN", "")
BINANCE_BASE    = "https://api.binance.com"
ADX_PERIOD      = 14


def fetch_signals():
    headers = {"Authorization": f"Bearer {PORTFOLIO_TOKEN}"}
    r = requests.get(f"{PORTFOLIO_URL}/api/signals", headers=headers, timeout=15)
    r.raise_for_status()
    data = r.json()
    # API liste ya da {"signals": [...]} dönebilir
    return data if isinstance(data, list) else data.get("signals", [])


def fetch_ohlcv(symbol, open_time_ms, limit=60):
    binance_sym = symbol if symbol.endswith("USDT") else symbol + "USDT"
    start_time  = open_time_ms - limit * 3600000
    end_time    = open_time_ms + 3600000
    try:
        resp = requests.get(
            f"{BINANCE_BASE}/api/v3/klines",
            params={"symbol": binance_sym, "interval": "1h",
                    "startTime": start_time, "endTime": end_time, "limit": limit + 2},
            timeout=10,
        )
        if resp.status_code != 200:
            return None
        klines = resp.json()
        return {
            "highs":  [float(k[2]) for k in klines],
            "lows":   [float(k[3]) for k in klines],
            "closes": [float(k[4]) for k in klines],
        }
    except Exception:
        return None


def calc_adx(highs, lows, closes, period=ADX_PERIOD):
    n = len(closes)
    if n < period * 2 + 2:
        return None
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
    for a, p, n_ in zip(atr_s, pdm_s, ndm_s):
        if a == 0:
            continue
        pdi = 100 * p / a
        ndi = 100 * n_ / a
        dx_list.append(100 * abs(pdi - ndi) / (pdi + ndi) if (pdi + ndi) > 0 else 0)

    if len(dx_list) < period:
        return None
    adx = sum(dx_list[:period]) / period
    for v in dx_list[period:]:
        adx = (adx * (period - 1) + v) / period
    return round(adx, 2)


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


def main():
    print(f"Portfolio URL: {PORTFOLIO_URL}", flush=True)
    print("Portfolio'dan sinyaller çekiliyor...", flush=True)
    try:
        signals = fetch_signals()
    except Exception as e:
        print(f"API hatası: {e}", flush=True)
        return

    # Kapalı SMC CHoCH sinyallerini filtrele
    smc = [
        s for s in signals
        if (s.get("sig_type") == "smc" or s.get("sub_type", "").lower() == "choch")
        and s.get("status") not in ("open", "active", "pending")
        and s.get("result")
    ]

    print(f"Kapalı SMC sinyali: {len(smc)} / toplam: {len(signals)}", flush=True)
    if not smc:
        print("Yeterli kapalı SMC sinyali yok.", flush=True)
        return

    winners, losers = [], []

    for sig in smc:
        symbol      = sig.get("symbol", "").replace("USDT", "")
        result      = sig.get("result", "")
        pnl         = float(sig.get("pnl_pct") or sig.get("return_pct") or sig.get("pnl") or 0)
        open_time   = parse_time_ms(sig)

        if not open_time:
            print(f"  {symbol}: zaman bilgisi yok, atlanıyor", flush=True)
            continue

        ohlcv = fetch_ohlcv(symbol, open_time)
        time.sleep(0.15)

        if not ohlcv or len(ohlcv["closes"]) < ADX_PERIOD * 2 + 2:
            print(f"  {symbol}: OHLCV yetersiz", flush=True)
            continue

        adx = calc_adx(ohlcv["highs"], ohlcv["lows"], ohlcv["closes"])
        if adx is None:
            print(f"  {symbol}: ADX hesaplanamadı", flush=True)
            continue

        is_win = "WIN" in result.upper()
        record = {"symbol": symbol, "adx": adx, "result": result, "pnl": pnl}
        (winners if is_win else losers).append(record)

        tag = "✅" if is_win else "❌"
        print(f"  {tag} {symbol:8} | {result:12} | PNL: {pnl:+6.1f}% | ADX: {adx:.1f}", flush=True)

    print("\n" + "=" * 55, flush=True)

    def stats(group, label):
        if not group:
            print(f"{label}: veri yok", flush=True)
            return
        adxs = [r["adx"] for r in group]
        avg  = sum(adxs) / len(adxs)
        print(f"{label} ({len(group)} sinyal) — Ort ADX: {avg:.1f}", flush=True)
        print(f"  ADX < 20  : {sum(1 for v in adxs if v < 20)}", flush=True)
        print(f"  ADX 20-30 : {sum(1 for v in adxs if 20 <= v < 30)}", flush=True)
        print(f"  ADX 30-40 : {sum(1 for v in adxs if 30 <= v < 40)}", flush=True)
        print(f"  ADX > 40  : {sum(1 for v in adxs if v >= 40)}", flush=True)

    stats(winners, "✅ KAZANANLAR")
    print(flush=True)
    stats(losers,  "❌ KAYBEDENLER")

    print("\n" + "=" * 55, flush=True)
    if winners and losers:
        avg_w = sum(r["adx"] for r in winners) / len(winners)
        avg_l = sum(r["adx"] for r in losers)  / len(losers)
        diff  = avg_w - avg_l
        print(f"ADX farkı (kazan - kaybet): {diff:+.1f}", flush=True)
        if abs(diff) >= 5:
            yon = "YÜKSEK" if diff > 0 else "DÜŞÜK"
            print(f"→ Kazananlarda ADX daha {yon} — filtre mantıklı görünüyor", flush=True)
        else:
            print("→ ADX farkı anlamlı değil, başka filtre dene", flush=True)


if __name__ == "__main__":
    main()
