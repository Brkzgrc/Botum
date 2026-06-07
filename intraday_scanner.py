# -*- coding: utf-8 -*-
"""
Gün İçi Piyasa Nabzı
====================
09:15 / 15:15 / 21:15 TR saatlerinde çalışır.
15m (son 6s) + 1h + 4h + haftalık FBB/SSL/TMA + F&G + Dominans → Claude → Telegram (thread 38)
"""

import gc
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from claude_analyzer import (
    _fear_greed, _dominance, _fetch_btc_macro, _btc_macro_str,
    _tma_3d_btc, _tf_summary, _fetch_klines, _dom_str, _tr_now,
    send_decision, ANTHROPIC_API_KEY,
)

TR_TZ = timezone(timedelta(hours=3))
SCAN_HOURS_TR = {9, 15, 21}
SCAN_MINUTE   = 15


def _movement_str(symbol: str, interval: str, limit: int, label: str) -> str:
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


def run_intraday_scan():
    if not ANTHROPIC_API_KEY:
        print("[NABİZ] ANTHROPIC_API_KEY eksik.", flush=True)
        return

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
GÖREV:
Yukarıdaki verileri kullanarak aşağıdaki yapıyı TAM OLARAK uygula. Köşeli parantezler sana yönelik talimat — metne yazma, numara kullanma, madde işareti koyma.

<b>📊 Son 6 Saat</b>
━━━━━━━━━━━━━━━━━━━━
[Son 6 saatte ne oldu? Açılış fiyatı, şimdiki seviye, önemli seviye test edildi mi, momentum hangi yönde? 2-3 cümle, akıcı paragraf.]

<b>🔍 Piyasa Yorumu</b>
━━━━━━━━━━━━━━━━━━━━
[Kısa vade (1h/4h) ve uzun vade (haftalık FBB/SSL/TMA) birlikte ne anlatıyor? Tepki yükselişi mi, gerçek dönüş mü, düşüş devamı mı? Uzun vade kısa vadeyi nasıl çerçeliyor? 2-3 cümle, trader yorumu.]

<b>💡 Pratik Görüş</b>
━━━━━━━━━━━━━━━━━━━━
[Giriş mantığı var mı? Hangi seviyeler kritik? Yoksa bekle mi? Net konuş — veriye dayanıyorsa kararını söyle. 1-2 cümle.]

DİL KURALI: Alarm dili yok. Trader yorumu var. Net ol, belirsiz ifadeler kullanma.
FORMATLAMA: Yalnızca Telegram HTML — <b></b> ve <i></i> kullan. *, #, _, madde numaraları kullanma."""

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
    finally:
        try:
            del client
        except Exception:
            pass
        gc.collect()

    tr_time = _tr_now()
    msg = (
        f"⚡ <b>GÜN İÇİ PİYASA NABZI</b>\n"
        f"🕐 {tr_time.strftime('%d/%m/%Y %H:%M')}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{text}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>Claude Analyzer · Gün İçi Tarama</i>"
    )
    send_decision(msg, thread_id=None)
    print("[NABİZ] Tamamlandı, Telegram'a gönderildi.", flush=True)


def start_intraday_scanner():
    def _loop():
        last_key = None
        print(f"[NABİZ] Başlatıldı — 09:15/15:15/21:15 TR", flush=True)
        while True:
            try:
                now = datetime.now(TR_TZ)
                if now.hour in SCAN_HOURS_TR and now.minute == SCAN_MINUTE:
                    key = f"{now.date()}_{now.hour}"
                    if last_key != key:
                        last_key = key
                        threading.Thread(
                            target=run_intraday_scan,
                            daemon=True,
                            name="intraday-scan-run"
                        ).start()
            except Exception as e:
                print(f"[NABİZ LOOP] {e}", flush=True)
            time.sleep(30)

    threading.Thread(target=_loop, daemon=True, name="intraday-scanner").start()
