# -*- coding: utf-8 -*-
"""Anton manuel Telegram thread'i için GPT Sonnet Analyzer entegrasyonu."""
from __future__ import annotations

import html
import re
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

from anton_scanner.gpt_sonnet_analyzer import market_analyst_bot as _market

_GPT_PRODUCTION_PROMPT = r"""
ANTON GPT PRODUCTION KARAR KURALLARI:
- Analizin asıl avantajı 1D setup -> 4H trigger -> 1H timing zinciridir. Son aksiyon bölümünde klasik "desteğe gelsin" yaklaşımına geri dönme.
- Yeni alım için BİRİNCİ ve tercih edilen senaryo: 1H momentum soğurken fiyatın anlamlı ölçüde düşmemesi (yatay/zaman düzeltmesi) ve ardından 1H momentumun yeniden yukarı dönmesi. Bunu "YENİDEN TETİK -> ALIM ADAYI" olarak değerlendir.
- İKİNCİ senaryo: yakın ve kontrollü bir fiyat düzeltmesi sonrası 1H yeniden tetik. Düzeltme kontrollüyse, yakın dip/yapı korunuyorsa ve üst zaman dilimi trendi güçlüyse bunu yatay resetten otomatik olarak daha düşük kalite sayma.
- Derin 4H/1D destekleri ilk alım beklentisi değildir; alternatif düzeltme ve bozulma haritasıdır. Fiyat güçlü kalıyorsa sırf bu desteklere inmedi diye fırsatı yok sayma.
- Eski tepe/direnç üzeri kapanışı breakout/devam teyidi olarak ayrı değerlendir; bunu 1H yeniden-tetik girişinin zorunlu şartı yapma.
- "Şu An Ne Yapardım?" kısmında önce tek satırda net aksiyon yaz, sonra gerekçeyi ve aksiyonun hangi koşulda değişeceğini anlat.
- Pozisyonun giriş fiyatı verilmediyse mevcut pozisyon için koşulsuz tut/sat kararı verme; giriş fiyatı, kâr marjı ve yapısal seviyeye bağla.
- Yeniden tetik için RSI'nin mutlaka 50-60 gibi sabit bir banda inmesini şart koşma. Güçlü trendde RSI daha yüksek seviyede resetlenip yeniden yukarı dönebilir.
- StochRSI/MA, RSI, KDJ, MACD ve fiyat davranışını bağlama göre birlikte değerlendir.
- 1H yeniden tetikte yalnızca indikatör kesişimlerine bakma: momentum boşalırken fiyatın ne kadar geri verdiği, satıcının fiyatı aşağı itip itemediği, yakın dip/yapının korunması ve kısa vadeli fiyatın tekrar yukarı dönmesi öncelikli kanıttır.
- Güçlü 1D/4H trend bağlamında 1H MACD histogramının pozitife dönmesini ZORUNLU giriş koşulu yapma. MACD gecikmeli teyittir; pozitife geçmesi teyit gücünü artırır ama tek başına giriş kapısı değildir.
- Erken yeniden tetik sırasını bağlama göre oku: kısa vadeli fiyat/momentum dönüşü -> 1H StochRSI/KDJ yukarı kıvrılması -> negatif 1H MACD histogramının küçülmesi -> daha sonra MACD'nin pozitife geçmesi. İlk aşamalar güçlü fiyat yapısıyla birlikte oluşmuşsa sırf MACD henüz sıfırı geçmedi diye ALIM ADAYI'nı gereksiz geciktirme.
- TETİK BEKLE ile ALIM ADAYI ayrımında bütün gecikmeli göstergelerin aynı anda dönmesini bekleme. Fiyat yapısı korunmuşken öncü momentum göstergeleri dönüyor ve negatif momentum belirgin biçimde zayıflıyorsa bunu YENİDEN TETİK / ALIM ADAYI olarak değerlendirebilirsin.
- Snapshot'ta 15m veri yoksa 15m hakkında veri uydurma veya 15m teyidini zorunlu şart yapma. Mevcut 1H verisini kullan.
- Yakın 1H timing bozulması, 1H yapısal bozulma ve 4H ana kırılım bozulmasını aynı seviyede anlatma; BOZULMA / TEYİT bölümünde kademelendir.
- Mevcut fiyattan yaklaşık %10 veya daha uzaktaki 4H/1D desteklerini sığ/yakın düzeltme diye adlandırma; bunlar derin alternatif düzeltme/yapı testidir.

TELEGRAM ÇIKTI KURALI:
- Markdown #/## veya ** kullanma; başlıkları düz metin ve uygun emojiyle yaz. Telegram kalın biçimlendirmesini entegrasyon katmanı yapar.
- İlk bölüm sırası: "🔍 Tek Bakışta Sonuç", "📅 1D — SETUP", "🕓 4H — TRIGGER", "🕐 1H — TIMING", "🔗 Birlikte Okuma".
- "🌌 ŞU AN NE YAPARDIM?" başlığı ayrı aksiyon bölümünün başlangıcı olsun.
- Ardından "🎯 ALIM ADAYI NE ZAMAN?" ve "⚠️ BOZULMA / TEYİT" başlıklarını kullan.
- "🌌 ŞU AN NE YAPARDIM?" öncesindeki analiz mümkün olduğunca öz ve yaklaşık 3000 karakteri geçmeyecek biçimde yaz.
""".strip()

if _GPT_PRODUCTION_PROMPT not in _market.SYSTEM_PROMPT:
    _market.SYSTEM_PROMPT = _market.SYSTEM_PROMPT + "\n\n" + _GPT_PRODUCTION_PROMPT

analyze_symbol = _market.analyze_symbol
split_telegram = _market.split_telegram

_GPT_INFLIGHT: set[str] = set()
_GPT_INFLIGHT_LOCK = threading.Lock()
TR_TZ = timezone(timedelta(hours=3))

_BOLD_HEADINGS = (
    re.compile(r"^🧠\s+#?[A-Z0-9]+\s+GPT SONNET ANALİZİ$", re.IGNORECASE),
    re.compile(r"^🔍\s+Tek Bakışta Sonuç$", re.IGNORECASE),
    re.compile(r"^📅\s+1D\s+—\s+SETUP$", re.IGNORECASE),
    re.compile(r"^🕓\s+4H\s+—\s+TRIGGER$", re.IGNORECASE),
    re.compile(r"^🕐\s+1H\s+—\s+TIMING$", re.IGNORECASE),
    re.compile(r"^🔗\s+Birlikte Okuma$", re.IGNORECASE),
    re.compile(r"^🌌\s+ŞU AN NE YAPARDIM\?$", re.IGNORECASE),
    re.compile(r"^🎯\s+ALIM ADAYI NE ZAMAN\?$", re.IGNORECASE),
    re.compile(r"^⚠️\s+BOZULMA / TEYİT$", re.IGNORECASE),
)


def parse_gpt_symbol(text: str) -> Optional[str]:
    value = (text or "").strip().upper()
    m = re.fullmatch(r"#?([A-Z0-9]{2,15})(?:/|-|_)?(USDT)?\s+GPT", value, flags=re.IGNORECASE)
    if not m:
        return None
    base = m.group(1).upper()
    if base.endswith("USDT"):
        base = base[:-4]
    if not re.fullmatch(r"[A-Z0-9]{2,15}", base):
        return None
    return base + "USDT"


def _send(token: str, chat_id: str | int, thread_id: int | None, text: str, *, html_mode: bool = False) -> None:
    payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    if html_mode:
        payload["parse_mode"] = "HTML"
    if thread_id is not None:
        payload["message_thread_id"] = thread_id
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json=payload, timeout=30)
    if not r.ok:
        raise RuntimeError(f"Telegram sendMessage HTTP {r.status_code}: {r.text[:160]}")


def _format_telegram_html(text: str) -> str:
    out = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        escaped = html.escape(raw_line, quote=False)
        if line and any(pattern.fullmatch(line) for pattern in _BOLD_HEADINGS):
            out.append(f"<b>{html.escape(line, quote=False)}</b>")
        else:
            out.append(escaped)
    return "\n".join(out)


def _find_action_start(text: str) -> int:
    patterns = (
        r"(?im)^\s*🌌\s*ŞU AN NE YAPARDIM\??\s*$",
        r"(?im)^\s*#{0,3}\s*Şu [Aa]n [Nn]e [Yy]apardım\??\s*$",
    )
    for pattern in patterns:
        m = re.search(pattern, text)
        if m:
            return m.start()
    return -1


def _gpt_messages(base: str, analysis: str) -> list[str]:
    stamp = datetime.now(timezone.utc).astimezone(TR_TZ).strftime("%d/%m/%Y %H:%M")
    header = f"🧠 #{base} GPT SONNET ANALİZİ\n🕐 {stamp}\n━━━━━━━━━━━━━━━━━━━━\n\n"
    clean = (analysis or "").strip()
    action_at = _find_action_start(clean)
    if action_at >= 0:
        analysis_part = clean[:action_at].strip()
        action_part = clean[action_at:].strip()
        return split_telegram(header + analysis_part) + split_telegram(action_part)
    return split_telegram(header + clean)


def _run_gpt_analysis(pair: str, token: str, chat_id: str | int, thread_id: int | None) -> None:
    with _GPT_INFLIGHT_LOCK:
        if pair in _GPT_INFLIGHT:
            return
        _GPT_INFLIGHT.add(pair)
    try:
        base = pair[:-4] if pair.endswith("USDT") else pair
        analysis = analyze_symbol(base)
        for part in _gpt_messages(base, analysis):
            _send(token, chat_id, thread_id, _format_telegram_html(part), html_mode=True)
    except Exception as exc:
        base = pair[:-4] if pair.endswith("USDT") else pair
        try:
            _send(token, chat_id, thread_id, f"{base} GPT analizi tamamlanamadı: {type(exc).__name__}: {exc}")
        except Exception as send_exc:
            print(f"[GPT SONNET ANALYZER] hata bildirimi de gönderilemedi: {send_exc}", flush=True)
        print(f"[GPT SONNET ANALYZER] {pair}: {type(exc).__name__}: {exc}", flush=True)
    finally:
        with _GPT_INFLIGHT_LOCK:
            _GPT_INFLIGHT.discard(pair)
