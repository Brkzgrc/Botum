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


def _tg_send(msg):
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


def _tg(msg):
    if not ANALYZER_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[MARKET_ANALYZER] TG eksik: {msg[:80]}", flush=True)
        return
    limit = 3500
    if len(msg) <= limit:
        _tg_send(msg)
        return
    # Paragraf sınırından böl
    parts = []
    while len(msg) > limit:
        split_at = msg.rfind("\n\n", 0, limit)
        if split_at == -1:
            split_at = msg.rfind("\n", 0, limit)
        if split_at == -1:
            split_at = limit
        parts.append(msg[:split_at].strip())
        msg = msg[split_at:].strip()
    if msg:
        parts.append(msg)
    for i, part in enumerate(parts):
        _tg_send(part)
        if i < len(parts) - 1:
            time.sleep(0.5)


def _make_client():
    from anthropic import Anthropic
    return Anthropic(api_key=ANTHROPIC_API_KEY)


# ── Fibonacci Bollinger Bands (Rashad) ──────────────────────────
def _vwma(prices, volumes, period):
    pv    = sum(p * v for p, v in zip(prices[-period:], volumes[-period:]))
    v_sum = sum(volumes[-period:])
    return pv / v_sum if v_sum > 0 else sum(prices[-period:]) / period


def _calc_fbb(tf_data, period=200, std_mult=3.0):
    if not tf_data or len(tf_data.get("closes", [])) < period:
        return None
    h, l, c, v = (tf_data[k] for k in ("highs", "lows", "closes", "volumes"))
    hlc3  = [(hi + lo + cl) / 3 for hi, lo, cl in zip(h, l, c)]
    basis = _vwma(hlc3, v, period)
    mean  = sum(hlc3[-period:]) / period
    std   = (sum((x - mean) ** 2 for x in hlc3[-period:]) / period) ** 0.5 * std_mult
    fibs  = [0.236, 0.382, 0.5, 0.618, 0.764, 1.0]
    return {
        "basis": round(basis, 2),
        "upper": [round(basis + f * std, 2) for f in fibs],
        "lower": [round(basis - f * std, 2) for f in fibs],
    }


# ── SSL Hybrid (simplified: SMA high/low channel + HMA baseline) ─
def _wma(data, period):
    w = list(range(1, period + 1))
    return sum(d * wt for d, wt in zip(data[-period:], w)) / sum(w)


def _hma(closes, period):
    half   = max(2, period // 2)
    sqrt_p = max(2, round(period ** 0.5))
    n      = len(closes)
    if n < period + sqrt_p:
        return None
    wma_h = [_wma(closes[max(0, i - half + 1):i + 1],   min(half,   i + 1)) for i in range(n)]
    wma_f = [_wma(closes[max(0, i - period + 1):i + 1], min(period, i + 1)) for i in range(n)]
    diff  = [2 * wma_h[i] - wma_f[i] for i in range(n)]
    if len(diff) < sqrt_p:
        return None
    return _wma(diff, sqrt_p)


def _calc_ssl(tf_data, period=14):
    if not tf_data or len(tf_data.get("closes", [])) < period + 1:
        return None
    h, l, c = tf_data["highs"], tf_data["lows"], tf_data["closes"]
    sma_h = sum(h[-period:]) / period
    sma_l = sum(l[-period:]) / period
    trend = "YUKARI" if c[-1] > sma_h else "AŞAĞI"
    hma   = _hma(c, period)
    return {
        "trend":    trend,
        "sma_high": round(sma_h, 2),
        "sma_low":  round(sma_l, 2),
        "hma":      round(hma, 2) if hma else None,
    }


def _fbb_text(fbb, label, price):
    if not fbb:
        return f"{label} FBB: veri yok"
    basis = fbb["basis"]
    pct   = (price - basis) / basis * 100 if basis else 0
    ab    = "üstünde" if price > basis else "altında"
    lower = fbb["lower"]
    upper = fbb["upper"]
    sup   = [v for v in lower if v < price]
    res   = [v for v in upper if v > price]
    lines = [f"{label} FBB Basis: ${basis:,.2f} (şu an %{abs(pct):.1f} {ab})"]
    if sup:
        lines.append(f"  Alt bandlar (destek): {' | '.join(f'${v:,.2f}' for v in sup[-3:])}")
    if res:
        lines.append(f"  Üst bandlar (direnç): {' | '.join(f'${v:,.2f}' for v in res[:2])}")
    return "\n".join(lines)


def _ssl_text(ssl, label):
    if not ssl:
        return f"{label} SSL: veri yok"
    hma_s = f" | HMA: ${ssl['hma']:,.2f}" if ssl.get("hma") else ""
    return (f"{label} SSL Hybrid: {ssl['trend']} "
            f"| Kanal ${ssl['sma_low']:,.2f} – ${ssl['sma_high']:,.2f}{hma_s}")


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

    # FBB + SSL hesapla
    btc_fbb_w = _calc_fbb(btc.get("1w"))
    btc_fbb_d = _calc_fbb(btc.get("1d"))
    btc_ssl_d = _calc_ssl(btc.get("1d"))
    eth_fbb_w = _calc_fbb(eth.get("1w"))
    eth_fbb_d = _calc_fbb(eth.get("1d"))
    eth_ssl_d = _calc_ssl(eth.get("1d"))

    indicator_section = f"""## TEKNİK İNDİKATÖRLER (FBB + SSL)
{_fbb_text(btc_fbb_w, "BTC Haftalık", btc_price)}
{_fbb_text(btc_fbb_d, "BTC Günlük",   btc_price)}
{_ssl_text(btc_ssl_d, "BTC Günlük")}

{_fbb_text(eth_fbb_w, "ETH Haftalık", eth_price)}
{_fbb_text(eth_fbb_d, "ETH Günlük",   eth_price)}
{_ssl_text(eth_ssl_d, "ETH Günlük")}"""

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

{indicator_section}

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
            max_tokens=5000,
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
    # İlk 3 bölüm (Global/BTC/Altcoin) header ile birlikte gönder, geri kalan ayrı mesaj
    section_starts = [m.start() for m in re.finditer(r"<b>", clean)]
    if len(section_starts) >= 4:
        split_at = section_starts[3]
        _tg(header + clean[:split_at].strip())
        time.sleep(0.5)
        _tg(clean[split_at:].strip())
    else:
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
