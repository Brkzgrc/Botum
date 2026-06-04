import os
import json
import re
import time
import threading
import requests
from datetime import datetime, timedelta, timezone
from anthropic import Anthropic

from market_watch import fetch_all

TR_TZ = timezone(timedelta(hours=3))

ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")
ANALYZER_TOKEN     = os.environ.get("ANALYZER_TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
LEVELS_FILE        = "levels.json"
ALERT_COOLDOWN_SEC = 6 * 3600

_client  = None
_alerted = {}  # (symbol_key, level, threshold) -> last_alert_timestamp


def _tg(msg, thread_id=1):
    if not ANALYZER_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[MARKET_ANALYZER] TG eksik: {msg[:80]}", flush=True)
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{ANALYZER_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": msg,
                "parse_mode": "HTML",
                "message_thread_id": thread_id,
            },
            timeout=10,
        )
    except Exception as e:
        print(f"[MARKET_ANALYZER] Telegram hata: {e}", flush=True)


def _get_client():
    global _client
    if _client is None:
        _client = Anthropic(api_key=ANTHROPIC_API_KEY)
    return _client


def _swing_levels(highs, lows, window=3):
    """Basit swing high/low tespiti — en son 5 noktayı döndür."""
    n = len(highs)
    s_highs, s_lows = [], []
    for i in range(window, n - window):
        if all(highs[i] >= highs[i-j] and highs[i] >= highs[i+j] for j in range(1, window+1)):
            s_highs.append(round(highs[i], 2))
        if all(lows[i] <= lows[i-j] and lows[i] <= lows[i+j] for j in range(1, window+1)):
            s_lows.append(round(lows[i], 2))
    return s_highs[-5:], s_lows[-5:]


def _tf_summary(tf_data, tf_label, n=50):
    """Tek TF için Claude'a gidecek özet metni üret."""
    if not tf_data:
        return f"{tf_label}: veri yok"
    closes = tf_data["closes"][-n:]
    highs  = tf_data["highs"][-n:]
    lows   = tf_data["lows"][-n:]
    ma20   = sum(closes[-20:]) / 20 if len(closes) >= 20 else closes[-1]
    trend  = "Yükseliş" if closes[-1] > ma20 else "Düşüş"
    s_h, s_l = _swing_levels(highs, lows)
    return (
        f"{tf_label}: Güncel=${closes[-1]:,.2f} | "
        f"Range(son {n}): H=${max(highs):,.2f} / L=${min(lows):,.2f} | "
        f"20MA=${ma20:,.2f} ({trend})\n"
        f"  Swing Highs: {', '.join(f'${v:,.2f}' for v in s_h) or '-'}\n"
        f"  Swing Lows:  {', '.join(f'${v:,.2f}' for v in s_l) or '-'}"
    )


def _build_prompt(data, portfolio_context=""):
    g  = data.get("global") or {}
    btc = data.get("btc") or {}
    eth = data.get("eth") or {}
    tweets = data.get("tweets") or []

    btc_price = (btc.get("4h") or {}).get("closes", [0])[-1]
    eth_price = (eth.get("4h") or {}).get("closes", [0])[-1]

    btc_lines = "\n".join(_tf_summary(btc.get(tf), tf) for tf in ["4h", "1d", "3d", "1w"])
    eth_lines = "\n".join(_tf_summary(eth.get(tf), tf) for tf in ["4h", "1d", "3d", "1w"])

    tweet_text = (
        "\n".join(f"- {t['title']}" for t in tweets[:5])
        if tweets else "Tweet alınamadı"
    )

    return f"""Sen deneyimli bir kripto teknik analistisin. Türkçe yanıt ver.

## GLOBAL PİYASA
TOTAL:  ${g.get('total', 0)/1e12:.3f}T | TOTAL2: ${g.get('total2', 0)/1e12:.3f}T | TOTAL3: ${g.get('total3', 0)/1e12:.3f}T
BTC.D: {g.get('btc_dominance', 0):.2f}% | ETH.D: {g.get('eth_dominance', 0):.2f}% | USDT.D: {g.get('usdt_dominance', 0):.2f}%
24h Değişim: {g.get('mcap_change_24h', 0):+.2f}%

## BTC/USDT — ${btc_price:,.2f}
{btc_lines}

## ETH/USDT — ${eth_price:,.2f}
{eth_lines}

## @AnalizCoin1 TWEETLER
{tweet_text}

## PORTFÖY
{portfolio_context or "Portföy verisi yok"}

---
## GÖREVİN

1. BTC için kritik destek/direnç seviyeleri — multi-TF confluence'a göre, max 4 seviye
2. ETH için kritik destek/direnç seviyeleri — max 4 seviye
3. Piyasa rejimi: BTC sezonu mu / alt sezon başlangıcı mı / risk-off mu?
4. BTC.D + USDT.D + TOTAL2/TOTAL3 trend yorumu
5. @AnalizCoin1 kıyaslaması — haklı mı, çelişiyor mu?
6. Portföy için somut uyarı/öneri

Önce seviyeleri şu formatta yaz, başka hiçbir şey olmadan:
<levels>
{{"btc": {{"supports": [sayı, sayı], "resistances": [sayı, sayı]}}, "eth": {{"supports": [sayı, sayı], "resistances": [sayı, sayı]}}}}
</levels>

Sonra Türkçe analiz yaz (max 350 kelime, sade, Telegram'a gidecek).
"""


def _parse_levels(text):
    m = re.search(r"<levels>\s*(.*?)\s*</levels>", text, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except Exception:
        return None


def _save_levels(levels):
    levels["updated_at"] = datetime.now(timezone.utc).isoformat()
    with open(LEVELS_FILE, "w") as f:
        json.dump(levels, f, indent=2)


def _load_levels():
    try:
        with open(LEVELS_FILE) as f:
            return json.load(f)
    except Exception:
        return None


def run_daily_analysis(portfolio_context=""):
    print("[MARKET_ANALYZER] Günlük analiz başlıyor...", flush=True)
    data   = fetch_all()
    prompt = _build_prompt(data, portfolio_context)

    try:
        resp = _get_client().messages.create(
            model="claude-opus-4-8",
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )
        text = resp.content[0].text
    except Exception as e:
        print(f"[MARKET_ANALYZER] Claude API hata: {e}", flush=True)
        return

    levels = _parse_levels(text)
    if levels:
        _save_levels(levels)
        print(f"[MARKET_ANALYZER] Seviyeler kaydedildi: {levels}", flush=True)
    else:
        print("[MARKET_ANALYZER] Seviye parse başarısız, ham yanıt:\n" + text[:200], flush=True)

    clean = re.sub(r"<levels>.*?</levels>", "", text, flags=re.DOTALL).strip()
    g = data.get("global") or {}
    btc_price = (data.get("btc") or {}).get("4h", {}).get("closes", [0])[-1]
    header = (
        f"📊 <b>Günlük Piyasa Analizi</b>\n"
        f"BTC: ${btc_price:,.2f} | BTC.D: {g.get('btc_dominance',0):.1f}% | "
        f"USDT.D: {g.get('usdt_dominance',0):.1f}%\n\n"
    )
    _tg(header + clean)
    print("[MARKET_ANALYZER] Analiz tamamlandı.", flush=True)


_THRESHOLDS = [5.0, 3.0, 1.0]  # büyükten küçüğe — ilk geçilen eşik tetiklenir


def check_price_proximity():
    levels = _load_levels()
    if not levels:
        return

    now = time.time()

    for symbol, key in [("BTCUSDT", "btc"), ("ETHUSDT", "eth")]:
        sym_levels = levels.get(key, {})
        all_levels = (
            [(p, "DESTEK") for p in sym_levels.get("supports", [])] +
            [(p, "DİRENÇ") for p in sym_levels.get("resistances", [])]
        )
        if not all_levels:
            continue
        try:
            r = requests.get(
                "https://api.binance.com/api/v3/ticker/price",
                params={"symbol": symbol},
                timeout=5,
            )
            price = float(r.json()["price"])
        except Exception:
            continue

        for level, level_type in all_levels:
            dist_pct = abs(price - level) / level * 100
            for thr in _THRESHOLDS:
                if dist_pct <= thr:
                    alert_key = (key, level, thr)
                    if now - _alerted.get(alert_key, 0) < ALERT_COOLDOWN_SEC:
                        break  # bu eşik için yakın zamanda zaten uyarıldı
                    _alerted[alert_key] = now
                    direction = "altında" if price < level else "üstünde"
                    coin = symbol.replace("USDT", "")
                    _tg(
                        f"⚠️ <b>{coin} {level_type} YAKINI</b>\n"
                        f"Fiyat: ${price:,.2f}\n"
                        f"Seviye: ${level:,.2f}\n"
                        f"Mesafe: %{dist_pct:.1f} {direction}\n"
                        f"(Multi-TF confluence seviyesi)"
                    )
                    break  # bu seviye için sadece en yakın eşiği tetikle


def _daily_loop():
    while True:
        now    = datetime.now(TR_TZ)
        target = now.replace(hour=8, minute=0, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        sleep_secs = (target - now).total_seconds()
        print(f"[MARKET_ANALYZER] Sonraki günlük analiz: {target.strftime('%d.%m %H:%M')} TR ({sleep_secs/3600:.1f}s)", flush=True)
        time.sleep(sleep_secs)
        try:
            run_daily_analysis()
        except Exception as e:
            print(f"[MARKET_ANALYZER] Daily loop hata: {e}", flush=True)


def _proximity_loop():
    time.sleep(60)  # bot başlarken bir dakika bekle
    while True:
        try:
            check_price_proximity()
        except Exception as e:
            print(f"[MARKET_ANALYZER] Proximity loop hata: {e}", flush=True)
        time.sleep(30 * 60)


def start_market_analyzer():
    threading.Thread(target=_daily_loop,     daemon=True, name="market_daily").start()
    threading.Thread(target=_proximity_loop, daemon=True, name="market_proximity").start()
    print("[MARKET_ANALYZER] Başlatıldı — daily@08:00TR + proximity@30dk", flush=True)


if __name__ == "__main__":
    run_daily_analysis()
