#!/usr/bin/env python3
"""
Proaktif piyasa taraması — GitHub Actions tarafından tetiklenir.
FBB (haftalık) + SSL (haftalık) + TMA (3G) + F&G + Dominans → Claude yorumu → Telegram
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from concurrent.futures import ThreadPoolExecutor
from claude_analyzer import (
    _fear_greed, _dominance, _fetch_btc_macro, _btc_macro_str,
    _tma_3d_btc, _tf_summary, _dom_str, _tr_now, send_decision,
    ANTHROPIC_API_KEY, ANALYZER_TELEGRAM_TOKEN, TELEGRAM_CHAT_ID,
)


def run_proactive_scan():
    if not ANTHROPIC_API_KEY or not ANALYZER_TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[SCAN] Eksik env var (ANTHROPIC_API_KEY / ANALYZER_TELEGRAM_TOKEN / TELEGRAM_CHAT_ID). Çıkılıyor.", flush=True)
        sys.exit(1)

    import anthropic

    print("[SCAN] Veri çekiliyor...", flush=True)
    with ThreadPoolExecutor(max_workers=4) as ex:
        fut_fg    = ex.submit(_fear_greed)
        fut_dom   = ex.submit(_dominance)
        fut_macro = ex.submit(_fetch_btc_macro)
        fut_tma   = ex.submit(_tma_3d_btc)
    fg_val, fg_label = fut_fg.result()
    dom              = fut_dom.result()
    macro            = fut_macro.result()
    tma              = fut_tma.result()
    btc_4h           = _tf_summary("BTC/USDT", "4h", 100)
    btc_price        = (btc_4h or {}).get("close")

    fg_str    = f"{fg_val} ({fg_label})" if fg_val is not None else "bilinmiyor"
    macro_str = _btc_macro_str(macro, btc_price) if macro else "veri yok"

    tma_str = ""
    if tma:
        tma_str = f"\nBTC 3G TMA: {tma['trend']}"
        if tma["cross"]:
            tma_str += f"  ⚠️ {tma['cross']}"

    btc_rsi    = (btc_4h or {}).get("rsi")
    btc_ema50  = (btc_4h or {}).get("ema50")
    btc_ema200 = (btc_4h or {}).get("ema200")

    prompt = f"""Sen deneyimli bir kripto piyasa analistisisin.

[BTC — 4 SAATLİK]
Fiyat: {btc_price} | RSI: {btc_rsi} | EMA50: {btc_ema50} | EMA200: {btc_ema200}

[BTC MAKRO — UZUN VADE]
{macro_str}{tma_str}

[MARKET]
Fear & Greed: {fg_str}
{_dom_str(dom)}

GÖREV: Periyodik proaktif piyasa taraması.
Önce haftalık yapıya bak (FBB zonu hangi seviye, SSL yönü ne diyor), sonra 3 günlük TMA durumunu değerlendir, sonra anlık koşulları yorumla.
Piyasa hangi fazda? Bu dönemde trader olarak ne yapılmalı — bekle mi, al mı, dikkatli mi?
Sade Türkçe, 3-4 cümle, teknik jargon yok, markdown başlık yok."""

    print("[SCAN] Claude'a gönderiliyor...", flush=True)
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        resp   = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )
        text = resp.content[0].text.strip()
    except Exception as e:
        print(f"[SCAN] Claude API hatası: {e}", flush=True)
        text = f"BTC: {btc_price} | F&G: {fg_str}\n<i>(Claude API yanıt vermedi — ham veri)</i>"

    tr_time = _tr_now()
    msg = (
        f"🔍 <b>PROAKTİF PİYASA TARAMASI</b>\n"
        f"🕐 {tr_time.strftime('%d/%m/%Y %H:%M')}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{text}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>Claude Analyzer · GitHub Actions</i>"
    )
    send_decision(msg)
    print("[SCAN] Tarama tamamlandı, Telegram'a gönderildi.", flush=True)


if __name__ == "__main__":
    run_proactive_scan()
