#!/usr/bin/env python3
"""
Gün İçi Piyasa Nabzı — GitHub Actions tarafından tetiklenir (her 6 saatte bir).
15m (son 6s) + 1h + 4h + haftalık FBB/SSL + TMA + F&G + Dominans → Claude yorumu → Telegram
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from concurrent.futures import ThreadPoolExecutor, as_completed
from claude_analyzer import (
    _fear_greed, _dominance, _fetch_btc_macro, _btc_macro_str,
    _tma_3d_btc, _tf_summary, _fetch_klines, _dom_str, _tr_now, send_decision,
    ANTHROPIC_API_KEY, ANALYZER_TELEGRAM_TOKEN, TELEGRAM_CHAT_ID,
)


def _movement_str(symbol: str, interval: str, limit: int, label: str) -> str:
    """Son X mum için fiyat hareketi özeti: açılış→şimdi, high/low, değişim %."""
    data = _fetch_klines(symbol, interval, limit)
    if not data or not data.get("closes"):
        return f"{label}: veri yok"
    opens  = data["opens"]
    closes = data["closes"]
    highs  = data["highs"]
    lows   = data["lows"]
    start  = opens[0]
    now    = closes[-1]
    chg    = (now - start) / start * 100
    return (
        f"{label}: <b>${now:,.2f}</b> | Açılış ${start:,.2f} ({chg:+.1f}%) | "
        f"H ${max(highs):,.2f} / L ${min(lows):,.2f}"
    )


def _tf_str(data: dict | None, label: str) -> str:
    if not data:
        return f"{label}: veri yok"
    parts = [f"<b>${data['close']:,.2f}</b>"]
    if data.get("rsi")    is not None: parts.append(f"RSI {data['rsi']}")
    if data.get("ema50")  is not None: parts.append(f"EMA50 ${data['ema50']:,.2f}")
    if data.get("ema200") is not None: parts.append(f"EMA200 ${data['ema200']:,.2f}")
    return f"{label}: {' | '.join(parts)}"


def run_scan():
    if not ANTHROPIC_API_KEY or not ANALYZER_TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[NABİZ] Eksik env var. Çıkılıyor.", flush=True)
        sys.exit(1)

    import anthropic

    print("[NABİZ] Veri çekiliyor...", flush=True)

    with ThreadPoolExecutor(max_workers=7) as ex:
        futs = {
            "fg":    ex.submit(_fear_greed),
            "dom":   ex.submit(_dominance),
            "macro": ex.submit(_fetch_btc_macro),
            "tma":   ex.submit(_tma_3d_btc),
            "m15":   ex.submit(_movement_str, "BTC/USDT", "15m", 24, "Son 6s (15m)"),
            "h1":    ex.submit(_tf_summary,   "BTC/USDT", "1h",  48),
            "h4":    ex.submit(_tf_summary,   "BTC/USDT", "4h",  24),
        }

    fg_val, fg_label = futs["fg"].result()
    dom              = futs["dom"].result()
    macro            = futs["macro"].result()
    tma              = futs["tma"].result()
    m15_str          = futs["m15"].result()
    h1_data          = futs["h1"].result()
    h4_data          = futs["h4"].result()

    btc_price = (h4_data or {}).get("close")
    fg_str    = f"{fg_val} ({fg_label})" if fg_val is not None else "bilinmiyor"
    macro_str = _btc_macro_str(macro, btc_price) if macro else "veri yok"

    tma_str = ""
    if tma:
        tma_str = f"\nBTC 3G TMA: {tma['trend']}"
        if tma.get("cross"):
            tma_str += f"  ⚠️ {tma['cross']}"

    prompt = f"""Sen deneyimli bir kripto piyasa analistisisin. Her 6 saatte bir piyasanın nabzını alıyorsun.

Okuyucu: Kripto yatırımcısı, yeni başlayan da anlayabilmeli. Sade Türkçe, teknik terimleri kısa parantez içinde açıkla.

## GÜN İÇİ BTC HAREKETİ (son 6 saat)
{m15_str}

## SAATLIK GÖRÜNÜM (1h)
{_tf_str(h1_data, "1h")}

## ORTA VADE (4h)
{_tf_str(h4_data, "4h")}

## UZUN VADE BİAS (haftalık FBB + SSL + TMA)
{macro_str}{tma_str}

## PİYASA
Fear & Greed: {fg_str}
{_dom_str(dom)}

---
GÖREV: Gün içi anlık tarama — şunları sırayla değerlendir ve kısa, net yaz:

1. Son 6 saatte ne oldu? Fiyat nerede açıldı, nereye geldi, önemli seviye test edildi mi?
2. Kısa vade (1h/4h) momentum: alıcı mı satıcı mı baskın, RSI ve EMA'lar ne söylüyor?
3. Uzun vade bağlamı: Haftalık tablo bu kısa vadeli hareketi nasıl çerçeveler? Tepki yükselişi mi, gerçek dönüş mü, düşüş devamı mı?
4. Pratik yorum: Giriş mantığı var mı? Hangi seviyeleri izle? Yoksa uzak mı durulmalı?

DİL KURALI:
- Net konuş. Veriye dayanıyorsa kararını söyle: "tepki yükselişi olabilir" değil, eğer öyleyse "büyük ihtimalle tepki yükselişi çünkü [neden]"
- "Takip edilmeli", "ayırt edilmeli" gibi belirsiz ifadeler yok — görüşünü ver
- Alarm dili yok, trader yorumu var
- 4-6 cümle, kısa ve özlü
FORMATLAMA: Yalnızca Telegram HTML — <b></b> ve <i></i> kullan, *, #, _ işaretleri kullanma."""

    print("[NABİZ] Claude'a gönderiliyor...", flush=True)
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        resp   = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=700,
            messages=[{"role": "user", "content": prompt}],
        )
        text = resp.content[0].text.strip()
    except Exception as e:
        print(f"[NABİZ] Claude API hatası: {e}", flush=True)
        text = f"BTC: {btc_price or '—'} | F&G: {fg_str}\n<i>(Claude API yanıt vermedi)</i>"

    tr_time = _tr_now()
    msg = (
        f"⚡ <b>GÜN İÇİ PİYASA NABZI</b>\n"
        f"🕐 {tr_time.strftime('%d/%m/%Y %H:%M')}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{text}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>Claude Analyzer · Gün İçi Tarama</i>"
    )
    send_decision(msg)
    print("[NABİZ] Tamamlandı, Telegram'a gönderildi.", flush=True)


if __name__ == "__main__":
    run_scan()
