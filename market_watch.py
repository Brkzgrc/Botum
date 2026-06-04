import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

BINANCE_BASE = "https://api.binance.com"
COINGECKO_BASE = "https://api.coingecko.com/api/v3"

NITTER_INSTANCES = [
    "https://nitter.privacydev.net",
    "https://nitter.cz",
    "https://nitter.1d4.us",
    "https://nitter.net",
]


def fetch_binance_ohlcv(symbol, timeframes, limit=100):
    binance_symbol = symbol.replace("/", "")
    result = {}
    for tf in timeframes:
        try:
            resp = requests.get(
                f"{BINANCE_BASE}/api/v3/klines",
                params={"symbol": binance_symbol, "interval": tf, "limit": limit},
                timeout=10,
            )
            resp.raise_for_status()
            klines = resp.json()
            result[tf] = {
                "timestamps": [k[0] for k in klines],
                "opens":      [float(k[1]) for k in klines],
                "highs":      [float(k[2]) for k in klines],
                "lows":       [float(k[3]) for k in klines],
                "closes":     [float(k[4]) for k in klines],
                "volumes":    [float(k[5]) for k in klines],
            }
        except Exception as e:
            print(f"[MARKET_WATCH] Binance OHLCV hata {symbol} {tf}: {e}", flush=True)
            result[tf] = None
    return result


def fetch_coingecko_global():
    try:
        resp = requests.get(f"{COINGECKO_BASE}/global", timeout=10)
        resp.raise_for_status()
        data = resp.json().get("data", {})

        mcap_pct  = data.get("market_cap_percentage", {})
        total_mcap = data.get("total_market_cap", {}).get("usd", 0)

        btc_d  = mcap_pct.get("btc", 0)
        eth_d  = mcap_pct.get("eth", 0)
        usdt_d = mcap_pct.get("usdt", 0)

        btc_mcap = total_mcap * btc_d  / 100
        eth_mcap = total_mcap * eth_d  / 100

        return {
            "total":            total_mcap,
            "total2":           total_mcap - btc_mcap,
            "total3":           total_mcap - btc_mcap - eth_mcap,
            "btc_dominance":    round(btc_d,  2),
            "eth_dominance":    round(eth_d,  2),
            "usdt_dominance":   round(usdt_d, 2),
            "mcap_change_24h":  data.get("market_cap_change_percentage_24h_usd", 0),
        }
    except Exception as e:
        print(f"[MARKET_WATCH] CoinGecko hata: {e}", flush=True)
        return None


def fetch_analizcoin_tweets(username="AnalizCoin1", count=5):
    for instance in NITTER_INSTANCES:
        try:
            resp = requests.get(
                f"{instance}/{username}/rss",
                timeout=8,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            if resp.status_code != 200:
                continue
            root  = ET.fromstring(resp.text)
            items = root.findall(".//item")[:count]
            tweets = []
            for item in items:
                tweets.append({
                    "title":   item.findtext("title", ""),
                    "content": item.findtext("description", ""),
                    "date":    item.findtext("pubDate", ""),
                    "link":    item.findtext("link", ""),
                })
            if tweets:
                return tweets
        except Exception as e:
            print(f"[MARKET_WATCH] Nitter {instance} hata: {e}", flush=True)
    print("[MARKET_WATCH] Tüm nitter instance'ları başarısız", flush=True)
    return None


def fetch_all():
    return {
        "btc":        fetch_binance_ohlcv("BTC/USDT", ["4h", "1d", "3d", "1w"]),
        "eth":        fetch_binance_ohlcv("ETH/USDT", ["4h", "1d", "3d", "1w"]),
        "global":     fetch_coingecko_global(),
        "tweets":     fetch_analizcoin_tweets(),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


if __name__ == "__main__":
    data = fetch_all()

    print("\n=== GLOBAL ===")
    g = data["global"]
    if g:
        print(f"TOTAL:  ${g['total']/1e12:.3f}T")
        print(f"TOTAL2: ${g['total2']/1e12:.3f}T")
        print(f"TOTAL3: ${g['total3']/1e12:.3f}T")
        print(f"BTC.D:  {g['btc_dominance']}%")
        print(f"ETH.D:  {g['eth_dominance']}%")
        print(f"USDT.D: {g['usdt_dominance']}%")
        print(f"24h:    {g['mcap_change_24h']:+.2f}%")

    print("\n=== BTC OHLCV ===")
    if data["btc"]:
        for tf, d in data["btc"].items():
            if d:
                print(f"  {tf}: {len(d['closes'])} mum | son kapanış: {d['closes'][-1]:.2f}")

    print("\n=== ETH OHLCV ===")
    if data["eth"]:
        for tf, d in data["eth"].items():
            if d:
                print(f"  {tf}: {len(d['closes'])} mum | son kapanış: {d['closes'][-1]:.2f}")

    print("\n=== TWEETS (@AnalizCoin1) ===")
    if data["tweets"]:
        for t in data["tweets"]:
            print(f"  [{t['date'][:16]}] {t['title'][:100]}")
    else:
        print("  Tweet alınamadı")
