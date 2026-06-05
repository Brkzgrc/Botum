import os
import gc
import json
import re
import time
import threading
import requests
from datetime import datetime, timedelta, timezone

from market_watch import fetch_all

TR_TZ = timezone(timedelta(hours=3))

ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")
ANALYZER_TOKEN     = os.environ.get("ANALYZER_TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("ANALYZER_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID", "")
LEVELS_FILE = "levels.json"


def _tg(msg):
    if not ANALYZER_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[MARKET_ANALYZER] TG eksik: {msg[:80]}", flush=True)
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{ANALYZER_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=10,
        )
        if r.status_code != 200:
            print(f"[MARKET_ANALYZER] TG hata {r.status_code}: {r.text[:120]}", flush=True)
    except Exception as e:
        print(f"[MARKET_ANALYZER] TG exception: {e}", flush=True)


def _make_client():
    from anthropic import Anthropic
    return Anthropic(api_key=ANTHROPIC_API_KEY)


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

    tweet_section = (
        f"## @AnalizCoin1 SON TWEETLER\n" +
        "\n".join(f"- {t['title']}" for t in tweets[:5])
    ) if tweets else ""

    SEP = "─────────────────────────"

    return f"""Sen deneyimli bir kripto analistisin. Türkçe yaz, okuyucu kripto yatırımcısı ama teknik analist değil.
DİL KURALI: Teknik terimleri parantez içinde açıkla. Örnek: "fiyatlardaki sert oynaklık (volatilite)", "piyasadan kaçış (risk-off)", "baskın coin oranı (dominans)" gibi.

## VERİ
TOTAL:  ${g.get('total', 0)/1e12:.3f}T | TOTAL2: ${g.get('total2', 0)/1e12:.3f}T | TOTAL3: ${g.get('total3', 0)/1e12:.3f}T
BTC.D: {g.get('btc_dominance', 0):.2f}% | ETH.D: {g.get('eth_dominance', 0):.2f}% | USDT.D: {g.get('usdt_dominance', 0):.2f}%
24h Değişim: {g.get('mcap_change_24h', 0):+.2f}%

## BTC/USDT — ${btc_price:,.2f}
{btc_lines}

## ETH/USDT — ${eth_price:,.2f}
{eth_lines}

{tweet_section}

## PORTFÖY
{portfolio_context or "Portföy verisi yok"}

---
## GÖREVİN

Aşağıdaki yapıyı TAM OLARAK uygula. Köşeli parantezler sana yönelik talimat, metne yazma.

<b>🌍 Global Piyasa</b>
{SEP}
[TOTAL, TOTAL2, TOTAL3 rakamlarını ver ve ne anlama geldiğini açıkla. BTC.D, ETH.D, USDT.D'yi yorumla — para nereye akıyor, piyasadan çıkış var mı? 3-4 cümle, akıcı paragraf.]

<b>₿ Bitcoin</b>
{SEP}
[Teknik tablo: trend, önemli ortalamalar. Ardından kritik destek ve direnç seviyeleri — güncel fiyata % mesafe ile. Max 4 seviye.]

<b>Ξ Alternatif Coinler (ETH öncülüğünde)</b>
{SEP}
[ETH teknik tablo ve kritik seviyeleri, ama asıl mesele şu: alt coin sezonu (altseason) geliyor mu, gecikiyor mu, uzak mı? ETH/BTC paritesi, ETH.D, TOTAL3 birlikte değerlendir. ETH burada tek bir coin olarak değil, tüm alt coinlerin termometresi olarak ele alınacak.]

[ORTA KISIM — ÖZGÜR: Burada ne dahil edeceğine sen karar ver. Aşağıdakilerden uygun olanları ekle, uygun olmayanı ekleme:
  • Piyasa rejimi analizi (boğa/ayı/yatay, risk iştahı durumu) — varsa
  • Para akışı detayı (dominans hareketleri anlamlıysa)
  • Tweet yorumu — SADECE tweet verisi geldiyse ekle, gelmediyse bu bölümü aç bile
  • Öne çıkan başka bir teknik veya makro gözlem — varsa
  Her eklediğin konu için uygun bir emoji + başlık + {SEP} kullan. Yoksa hiç ekleme.]

<b>💡 Görüş</b>
{SEP}
[İki ayrı tahmin/fikir: "Gün içi:" ve "Haftalık:" olarak ikiye böl. Kesin değil, fikir sun. Somut fiyat seviyeleri veya senaryo ver.]

Önce seviyeleri JSON olarak yaz:
<levels>
{{"btc": {{"supports": [sayı, sayı], "resistances": [sayı, sayı]}}, "eth": {{"supports": [sayı, sayı], "resistances": [sayı, sayı]}}}}
</levels>

Sonra yukarıdaki analizi yaz. Max 500 kelime.
FORMATLAMA: Yalnızca Telegram HTML — <b></b> kullan, *, #, _ işaretleri kullanma.
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

    client = _make_client()
    try:
        resp = client.messages.create(
            model="claude-opus-4-8",
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )
        text = resp.content[0].text
    except Exception as e:
        print(f"[MARKET_ANALYZER] Claude API hata: {e}", flush=True)
        return
    finally:
        del client
        gc.collect()

    levels = _parse_levels(text)
    if levels:
        _save_levels(levels)
        print(f"[MARKET_ANALYZER] Seviyeler kaydedildi: {levels}", flush=True)
    else:
        print("[MARKET_ANALYZER] Seviye parse başarısız, ham yanıt:\n" + text[:200], flush=True)

    clean = re.sub(r"<levels>.*?</levels>", "", text, flags=re.DOTALL).strip()
    # Allowed HTML tags for Telegram: keep <b>, <i>, <code>, <pre>
    clean = re.sub(r"<(?!/?(b|i|code|pre)(?:\s[^>]*)?>)[^>]+>", "", clean)
    # Strip any remaining markdown symbols
    clean = re.sub(r"\*\*(.+?)\*\*", r"\1", clean)
    clean = re.sub(r"__(.+?)__", r"\1", clean)
    clean = re.sub(r"^#{1,4}\s*", "", clean, flags=re.MULTILINE)
    g = data.get("global") or {}
    btc_price = (data.get("btc") or {}).get("4h", {}).get("closes", [0])[-1]
    now_tr = datetime.now(TR_TZ)
    SEP = "─────────────────────────"
    header = (
        f"📊 <b>GÜNLÜK PİYASA RAPORU</b>\n"
        f"🕐 {now_tr.strftime('%d/%m/%Y %H:%M')}\n"
        f"{SEP}\n"
        f"BTC: <b>${btc_price:,.2f}</b> | BTC.D: {g.get('btc_dominance',0):.1f}% | "
        f"USDT.D: {g.get('usdt_dominance',0):.1f}%\n"
        f"{SEP}\n\n"
    )
    _tg(header + clean)
    del data
    gc.collect()
    print("[MARKET_ANALYZER] Analiz tamamlandı.", flush=True)


def start_market_analyzer():
    def _daily_loop():
        while True:
            now = datetime.now(TR_TZ)
            target = now.replace(hour=8, minute=0, second=0, microsecond=0)
            if now >= target:
                target += timedelta(days=1)
            time.sleep((target - now).total_seconds())
            try:
                run_daily_analysis()
            except Exception as e:
                print(f"[MARKET_ANALYZER] Daily loop hata: {e}", flush=True)
    threading.Thread(target=_daily_loop, daemon=True, name="market_daily").start()
    print("[MARKET_ANALYZER] Başlatıldı — daily@08:00TR", flush=True)


if __name__ == "__main__":
    run_daily_analysis()
