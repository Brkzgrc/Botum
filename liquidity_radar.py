# -*- coding: utf-8 -*-
"""
Likidite Radarı
===============
Binance order book'tan alım/satış duvarlarını tespit eder.
Long/Short oranına göre likidite tasfiye riskini değerlendirir.
5 dakikalık cache, hata durumunda sessizce None döner.
"""

import time
import requests

_cache: dict = {"data": None, "ts": 0}
_TTL = 300  # 5 dakika

_MIN_DIST = 0.003  # en az %0.3 uzaklık — 0.0% duvar göstermeyi önler


def _find_walls(price: float, symbol: str = "BTCUSDT", limit: int = 500) -> dict | None:
    r = requests.get(
        "https://api.binance.com/api/v3/depth",
        params={"symbol": symbol, "limit": limit},
        timeout=10,
    )
    if not r.ok:
        return None
    ob = r.json()

    # ~0.5% genişliğinde bucket'lar — çok küçük olunca anlamsız yakın duvarlar çıkıyor
    bucket_size = price * 0.005

    def bucket_sum(orders):
        buckets: dict[float, float] = {}
        for p_str, q_str in orders:
            p = float(p_str)
            b = round(p / bucket_size) * bucket_size
            buckets[b] = buckets.get(b, 0) + float(q_str)
        return buckets

    bids_b = bucket_sum(ob.get("bids", []))
    asks_b = bucket_sum(ob.get("asks", []))

    bid_walls = sorted(
        [(p, q) for p, q in bids_b.items() if p < price * (1 - _MIN_DIST)],
        key=lambda x: x[1], reverse=True,
    )
    ask_walls = sorted(
        [(p, q) for p, q in asks_b.items() if p > price * (1 + _MIN_DIST)],
        key=lambda x: x[1], reverse=True,
    )

    return {
        "top_bid": bid_walls[0] if bid_walls else None,
        "top_ask": ask_walls[0] if ask_walls else None,
    }


def get_radar(
    price: float,
    long_ratio: float | None = None,
    symbol: str = "BTCUSDT",
) -> dict | None:
    now = time.time()
    if _cache["data"] and now - _cache["ts"] < _TTL:
        return _cache["data"]

    try:
        walls = _find_walls(price, symbol)
        if not walls:
            return _cache["data"]

        result: dict = {"price": price}

        top_bid = walls.get("top_bid")
        top_ask = walls.get("top_ask")

        if top_bid:
            result["support"]     = top_bid[0]
            result["support_pct"] = round((top_bid[0] - price) / price * 100, 1)
            result["support_qty"] = round(top_bid[1], 1)
            result["support_usd"] = round(top_bid[1] * price / 1_000_000, 1)

        if top_ask:
            result["resistance"]     = top_ask[0]
            result["resistance_pct"] = round((top_ask[0] - price) / price * 100, 1)
            result["resistance_qty"] = round(top_ask[1], 1)
            result["resistance_usd"] = round(top_ask[1] * price / 1_000_000, 1)

        # Long/Short oranına göre tasfiye yönü
        if long_ratio is not None:
            if long_ratio >= 70:
                result["risk"], result["risk_dir"] = "yüksek", "aşağı"
            elif long_ratio >= 60:
                result["risk"], result["risk_dir"] = "orta", "aşağı"
            elif long_ratio <= 30:
                result["risk"], result["risk_dir"] = "yüksek", "yukarı"
            elif long_ratio <= 40:
                result["risk"], result["risk_dir"] = "orta", "yukarı"
            else:
                result["risk"], result["risk_dir"] = "düşük", None

        _cache["data"] = result
        _cache["ts"]   = now
        _s = f"${result['support']:,.0f} ({result['support_usd']}M$)" if result.get("support") else "—"
        _r = f"${result['resistance']:,.0f} ({result['resistance_usd']}M$)" if result.get("resistance") else "—"
        print(f"[RADAR] OK — destek {_s} / direnç {_r}", flush=True)
        return result

    except Exception as e:
        print(f"[RADAR] hata: {e}", flush=True)
        return _cache["data"]


def radar_ui_lines(r: dict | None) -> list[str]:
    """Long/Short kartı altı için ayrı satırlar."""
    if not r:
        return []
    lines = []
    if r.get("support"):
        usd = f"{r['support_usd']}M$" if r.get("support_usd") is not None else f"{r['support_qty']} BTC"
        lines.append(f"Destek Duvarı: ${r['support']:,.0f} ({r['support_pct']}%) — {usd}")
    if r.get("resistance"):
        usd = f"{r['resistance_usd']}M$" if r.get("resistance_usd") is not None else f"{r['resistance_qty']} BTC"
        lines.append(f"Direnç Duvarı: ${r['resistance']:,.0f} ({r['resistance_pct']:+.1f}%) — {usd}")
    return lines


def radar_ui_text(r: dict | None) -> str:
    """Geriye dönük uyumluluk için tek satır."""
    lines = radar_ui_lines(r)
    return " | ".join(lines)


def radar_prompt_text(r: dict | None) -> str:
    """ANTON prompt'u için özet metin."""
    if not r:
        return "veri yok"
    lines = []
    if r.get("support"):
        usd = f"{r['support_usd']}M$" if r.get("support_usd") is not None else f"{r['support_qty']} BTC"
        lines.append(f"Alım duvarı: ${r['support']:,.0f} ({r['support_pct']}%) — {usd}")
    if r.get("resistance"):
        usd = f"{r['resistance_usd']}M$" if r.get("resistance_usd") is not None else f"{r['resistance_qty']} BTC"
        lines.append(f"Satış duvarı: ${r['resistance']:,.0f} ({r['resistance_pct']:+.1f}%) — {usd}")
    if r.get("risk"):
        dir_str = f" — {r['risk_dir']} yönlü tasfiye riski" if r.get("risk_dir") else ""
        lines.append(f"Likidite riski: {r['risk']}{dir_str}")
    return "\n".join(lines)
