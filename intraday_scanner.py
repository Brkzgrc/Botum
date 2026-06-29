# -*- coding: utf-8 -*-
"""
Gün İçi Piyasa Nabzı
====================
09:15 / 15:15 / 21:15 TR saatlerinde çalışır.
1h + 4h + haftalık FBB/SSL/TMA + F&G + Dominans → Claude → Telegram (General)
"""

import gc
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from market_watch import fetch_binance_ohlcv
from market_analyzer import _calc_fbb, _calc_ssl, _calc_tma, _fbb_text, _ssl_text, _tma_text
from claude_analyzer import _fear_greed, _dominance, _dom_str, _rsi, _ema, _tr_now, send_decision, ANTHROPIC_API_KEY

TR_TZ        = timezone(timedelta(hours=3))
SCAN_HOURS_TR = {9, 15, 21}
SCAN_MINUTE   = 15


def _tf_summary_mw(tf_data, label):
    """market_watch OHLCV dict'inden özet metin üret."""
    if not tf_data:
        return f"{label}: veri yok", None
    closes = tf_data.get("closes", [])
    highs  = tf_data.get("highs",  [])
    lows   = tf_data.get("lows",   [])
    opens  = tf_data.get("opens",  [])
    if not closes:
        return f"{label}: veri yok", None

    price   = closes[-1]
    rsi_val = _rsi(closes)
    ema50   = _ema(closes, 50)
    ema200  = _ema(closes, 200)

    parts = [f"<b>${price:,.2f}</b>"]
    if rsi_val  is not None: parts.append(f"RSI {rsi_val}")
    if ema50    is not None: parts.append(f"EMA50 ${ema50:,.2f}")
    if ema200   is not None: parts.append(f"EMA200 ${ema200:,.2f}")

    return f"{label}: {' | '.join(parts)}", price


def _movement_summary_mw(tf_data, label, candles=6):
    """Son N mum için açılış→şimdi hareketi."""
    if not tf_data:
        return f"{label}: veri yok"
    closes = tf_data.get("closes", [])
    highs  = tf_data.get("highs",  [])
    lows   = tf_data.get("lows",   [])
    opens  = tf_data.get("opens",  [])
    if len(closes) < candles:
        candles = len(closes)
    start = opens[-candles] if opens else closes[-candles]
    now   = closes[-1]
    chg   = (now - start) / start * 100 if start else 0
    h     = max(highs[-candles:]) if highs else now
    l     = min(lows[-candles:])  if lows  else now
    return (
        f"{label}: <b>${now:,.2f}</b> | Açılış ${start:,.2f} ({chg:+.1f}%) | "
        f"H ${h:,.2f} / L ${l:,.2f}"
    )


def run_intraday_scan():
    if not ANTHROPIC_API_KEY:
        print("[NABİZ] ANTHROPIC_API_KEY eksik.", flush=True)
        return

    import anthropic

    print("[NABİZ] Veri çekiliyor...", flush=True)

    # Paralel çek — market_watch proven working
    with ThreadPoolExecutor(max_workers=5) as ex:
        fut_fg   = ex.submit(_fear_greed)
        fut_dom  = ex.submit(_dominance)
        fut_1h   = ex.submit(fetch_binance_ohlcv, "BTCUSDT", ["1h"], 48)
        fut_4h   = ex.submit(fetch_binance_ohlcv, "BTCUSDT", ["4h"], 24)
        fut_1w   = ex.submit(fetch_binance_ohlcv, "BTCUSDT", ["1w", "3d"], None)

    fg_val, fg_label = fut_fg.result()
    dom    = fut_dom.result()
    data_1h = fut_1h.result().get("1h")
    data_4h = fut_4h.result().get("4h")
    data_macro = fut_1w.result()
    data_1w = data_macro.get("1w")
    data_3d = data_macro.get("3d")

    h4_str, btc_price = _tf_summary_mw(data_4h, "4h")
    h1_str, _         = _tf_summary_mw(data_1h, "1h")
    m6h_str           = _movement_summary_mw(data_1h, "Son 6s (1h)", candles=6)

    fbb  = _calc_fbb(data_1w)
    ssl  = _calc_ssl(data_1w)
    tma  = _calc_tma(data_3d)

    macro_lines = []
    if fbb: macro_lines.append(_fbb_text(fbb, "BTC Haftalık", btc_price or 0))
    if ssl: macro_lines.append(_ssl_text(ssl, "BTC Haftalık"))
    if tma: macro_lines.append(_tma_text(tma))
    macro_str = "\n".join(macro_lines) if macro_lines else "veri yok"

    fg_str = f"{fg_val} ({fg_label})" if fg_val is not None else "bilinmiyor"

    prompt = f"""Sen deneyimli bir kripto piyasa analistisisin. Her 6 saatte bir piyasanın nabzını alıyorsun.

Okuyucu: Kripto yatırımcısı, yeni başlayan da anlayabilmeli. Sade Türkçe, teknik terimleri kısa parantez içinde açıkla.

## GÜN İÇİ BTC HAREKETİ (son 6 saat)
{m6h_str}

## SAATLIK GÖRÜNÜM (1h)
{h1_str}

## ORTA VADE (4h)
{h4_str}

## UZUN VADE BİAS (haftalık FBB + SSL + TMA)
{macro_str}

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
        f"<i>🤖🐾 ANTON🐾 · Gün İçi Tarama</i>"
    )
    send_decision(msg, thread_id=None)
    print("[NABİZ] Tamamlandı, Telegram'a gönderildi.", flush=True)


def start_intraday_scanner():
    def _loop():
        last_key = None
        print("[NABİZ] Başlatıldı — 09:15/15:15/21:15 TR", flush=True)
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


if __name__ == "__main__":
    run_intraday_scan()
