# -*- coding: utf-8 -*-
"""Anton manuel Telegram thread'i için GPT Sonnet Analyzer entegrasyonu."""
from __future__ import annotations

import html
import json
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

from anton_scanner.gpt_sonnet_analyzer import market_analyst_bot as _market

_GPT_PRODUCTION_PROMPT = r"""

ANTON GPT PRODUCTION KARAR KURALLARI:
- Analizin asıl avantajı 1D setup -> 4H trigger -> 1H timing zinciridir. Son aksiyon
  bölümünde klasik "desteğe gelsin" yaklaşımına geri dönme.
- Yeni alım için BİRİNCİ ve tercih edilen senaryo: 1H momentum soğurken fiyatın
  anlamlı ölçüde düşmemesi (yatay/zaman düzeltmesi) ve ardından 1H momentumun
  yeniden yukarı dönmesi. Bunu "YENİDEN TETİK -> ALIM ADAYI" olarak değerlendir.
- İKİNCİ senaryo: yakın bir fiyat düzeltmesi sonrası 1H yeniden tetik.
- Derin 4H/1D destekleri ilk alım beklentisi değildir; alternatif düzeltme ve
  bozulma haritasıdır. Fiyat güçlü kalıyorsa sırf bu desteklere inmedi diye fırsatı
  yok sayma.
- Eski tepe/direnç üzeri kapanışı "breakout/devam teyidi" olarak ayrı değerlendir;
  bunu 1H yeniden-tetik girişinin zorunlu şartı yapma. Aksi halde giriş gereksiz
  biçimde pahalılaşabilir.
- "Şu An Ne Yapardım?" kısmında önce tek satırda net aksiyon yaz, sonra gerekçeyi
  ve aksiyonun hangi koşulda değişeceğini anlat.
- Pozisyonun giriş fiyatı verilmediyse "mevcut pozisyon tutulur/satılır" gibi
  koşulsuz bir karar verme. Bunun yerine mevcut pozisyon kararının giriş fiyatı,
  kâr marjı ve yapısal seviyeye bağlı olduğunu açıkça belirt.
- Yeniden tetik için RSI'nin mutlaka 50-60 gibi sabit bir banda inmesini şart koşma.
  Güçlü trendde RSI daha yüksek seviyede resetlenip yeniden yukarı dönebilir.
  StochRSI/MA, RSI, MACD ve fiyat davranışını bağlama göre birlikte değerlendir.
- Yakın 1H timing bozulması, 1H yapısal bozulma ve 4H ana kırılım bozulmasını aynı
  seviyede anlatma. BOZULMA / TEYİT bölümünde mümkünse bunları kademelendir.
- Mevcut fiyattan yaklaşık %%10 veya daha uzaktaki 4H/1D desteklerini "sığ/yakın
  düzeltme" diye adlandırma; bunlar derin alternatif düzeltme/yapı testi olarak
  ele alınmalı. Yüzdeyi snapshot'taki gerçek seviyelere göre bağlamsal değerlendir.
- 1H yeniden tetikte yalnızca indikatör kesişimlerine bakma: momentum boşalırken
  fiyatın ne kadar geri verdiği, satıcının fiyatı aşağı itip itemediği ve kısa
  vadeli fiyat yapısının tekrar yukarı dönmesi öncelikli kanıttır.

TELEGRAM ÇIKTI KURALI:
- Markdown #/## işaretleri veya ** kalın işaretleri kullanma; başlıkları düz metin
  ve uygun emojiyle yaz. Telegram kalın biçimlendirmesini entegrasyon katmanı yapar.
- İlk bölüm şu sırada olsun: "🔍 Tek Bakışta Sonuç", "📅 1D — SETUP",
  "🕓 4H — TRIGGER", "🕐 1H — TIMING", "🔗 Birlikte Okuma".
- "🌌 ŞU AN NE YAPARDIM?" başlığı MUTLAKA ayrı aksiyon bölümünün başlangıcı olsun.
- Ardından "🎯 ALIM ADAYI NE ZAMAN?" ve "⚠️ BOZULMA / TEYİT" başlıklarını kullan.
- "🌌 ŞU AN NE YAPARDIM?" öncesindeki analiz mümkün olduğunca öz ve yaklaşık
  3000 karakteri geçmeyecek biçimde yaz; ayrıntıyı aksiyon bölümüne taşıma.
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
    """`ZEC GPT`, `zec gpt`, `ZECUSDT GPT`, `ZEC/USDT GPT` -> `ZECUSDT`."""
    value = (text or "").strip().upper()
    m = re.fullmatch(
        r"#?([A-Z0-9]{2,15})(?:/|-|_)?(USDT)?\s+GPT",
        value,
        flags=re.IGNORECASE,
    )
    if not m:
        return None
    base = m.group(1).upper()
    if base.endswith("USDT"):
        base = base[:-4]
    if not re.fullmatch(r"[A-Z0-9]{2,15}", base):
        return None
    return base + "USDT"


def _send(token: str, chat_id: str | int, thread_id: int | None, text: str, *, html_mode: bool = False) -> None:
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    if html_mode:
        payload["parse_mode"] = "HTML"
    if thread_id is not None:
        payload["message_thread_id"] = thread_id
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json=payload,
        timeout=30,
    )
    if not r.ok:
        raise RuntimeError(f"Telegram sendMessage HTTP {r.status_code}: {r.text[:160]}")


def _format_telegram_html(text: str) -> str:
    """Model metnini güvenle escape eder; yalnızca tanımlı bölüm başlıklarını kalın yapar."""
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
    """Sonnet'in aksiyon bölümünü, ufak başlık varyasyonlarına toleransla bul."""
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
    """Telegram sunumu: 1. mesaj analiz, 2. mesaj mutlaka aksiyonla başlar."""
    stamp = datetime.now(timezone.utc).astimezone(TR_TZ).strftime("%d/%m/%Y %H:%M")
    header = (
        f"🧠 #{base} GPT SONNET ANALİZİ\n"
        f"🕐 {stamp}\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
    )
    clean = (analysis or "").strip()
    action_at = _find_action_start(clean)

    if action_at >= 0:
        analysis_part = clean[:action_at].strip()
        action_part = clean[action_at:].strip()
        first_parts = split_telegram(header + analysis_part)
        action_parts = split_telegram(action_part)
        return first_parts + action_parts

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
            _send(
                token,
                chat_id,
                thread_id,
                f"{base} GPT analizi tamamlanamadı: {type(exc).__name__}: {exc}",
            )
        except Exception as send_exc:
            print(f"[GPT SONNET ANALYZER] hata bildirimi de gönderilemedi: {send_exc}", flush=True)
        print(f"[GPT SONNET ANALYZER] {pair}: {type(exc).__name__}: {exc}", flush=True)
    finally:
        with _GPT_INFLIGHT_LOCK:
            _GPT_INFLIGHT.discard(pair)


def gpt_aware_manual_poll_loop(g: dict) -> None:
    """portfolio_tracker manuel poller'inin GPT-aware eşdeğeri."""
    enabled = bool(g.get("MANUAL_ANALYZER_ENABLED"))
    if not enabled:
        print("[MANUEL ANALYZER] Devre dışı.", flush=True)
        return

    token = g.get("ANALYZER_TELEGRAM_TOKEN") or ""
    chat_id_cfg = g.get("ANALYZER_CHAT_ID")
    thread_id_cfg = g.get("ANALYZER_THREAD_ID")
    allowed_user = str(g.get("ANALYZER_ALLOWED_USER_ID") or "")
    old_runner = g.get("_run_manual_analyzer")
    old_parser = g.get("_parse_manual_analyzer_symbol")

    provider = g.get("_manual_analyzer_v2_model") if g.get("_manual_analyzer_mode") == "v2" else "Haiku 4.5"
    key_ready = bool(g.get("_manual_gemini_key")) if g.get("_manual_analyzer_mode") == "v2" else bool(g.get("_manual_anthropic_key"))
    print(
        f"[MANUEL ANALYZER CONFIG] mod={g.get('_manual_analyzer_mode')} | sağlayıcı={provider} | "
        f"API anahtarı={'hazır' if key_ready else 'eksik'} | GPT route=GPT Sonnet Analyzer",
        flush=True,
    )

    if not token or not chat_id_cfg:
        print("[MANUEL ANALYZER] Token veya chat id eksik; dinleyici başlamadı.", flush=True)
        return
    if not callable(old_runner) or not callable(old_parser):
        print("[MANUEL ANALYZER] Eski analyzer fonksiyonları bulunamadı; dinleyici başlamadı.", flush=True)
        return

    url = f"https://api.telegram.org/bot{token}/getUpdates"
    offset = None
    try:
        first = requests.get(url, params={"timeout": 0, "limit": 100}, timeout=10).json()
        updates = first.get("result", []) if first.get("ok") else []
        if updates:
            offset = max(int(u["update_id"]) for u in updates) + 1
    except Exception as exc:
        print(f"[MANUEL ANALYZER] Başlangıç offset hatası: {exc}", flush=True)

    print(
        f"[MANUEL ANALYZER] Thread {thread_id_cfg} dinleniyor | normal=mevcut Anton | `COIN GPT`=GPT Sonnet Analyzer.",
        flush=True,
    )

    while True:
        try:
            params = {"timeout": 25, "limit": 50, "allowed_updates": json.dumps(["message"])}
            if offset is not None:
                params["offset"] = offset
            r = requests.get(url, params=params, timeout=35)
            data = r.json()
            if not data.get("ok"):
                print(f"[MANUEL ANALYZER] getUpdates HTTP {r.status_code}: {r.text[:120]}", flush=True)
                time.sleep(5)
                continue

            for update in data.get("result", []):
                offset = int(update["update_id"]) + 1
                msg = update.get("message") or {}
                chat_id = str((msg.get("chat") or {}).get("id", ""))
                thread_id = msg.get("message_thread_id")
                sender_id = str((msg.get("from") or {}).get("id", ""))

                if chat_id != str(chat_id_cfg) or thread_id != thread_id_cfg:
                    continue
                if (msg.get("from") or {}).get("is_bot"):
                    continue
                if allowed_user and sender_id != allowed_user:
                    print(f"[MANUEL ANALYZER] Yetkisiz kullanıcı yok sayıldı: {sender_id}", flush=True)
                    continue

                text = msg.get("text", "")
                gpt_pair = parse_gpt_symbol(text)
                if gpt_pair:
                    threading.Thread(
                        target=_run_gpt_analysis,
                        args=(gpt_pair, token, chat_id, thread_id),
                        daemon=True,
                        name=f"gpt-sonnet-analyzer-{gpt_pair}",
                    ).start()
                    continue

                pair = old_parser(text)
                if not pair:
                    continue
                threading.Thread(
                    target=old_runner,
                    args=(pair,),
                    daemon=True,
                    name=f"manual-analyzer-{pair}",
                ).start()
        except Exception as exc:
            print(f"[MANUEL ANALYZER] Dinleme hatası: {exc}", flush=True)
            time.sleep(5)
