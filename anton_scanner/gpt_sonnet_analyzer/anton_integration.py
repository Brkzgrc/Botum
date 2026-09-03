# -*- coding: utf-8 -*-
"""Anton manuel Telegram thread'i için GPT Sonnet Analyzer entegrasyonu.

Bu modül mevcut portfolio_tracker.py dosyasını değiştirmeden aynı getUpdates
consumer'i içinde iki yolu ayırır:
- `ZEC`     -> mevcut Anton manuel analyzer
- `ZEC GPT` -> GPT Sonnet Analyzer (1D/4H/1H)

Aynı Telegram bot token'i için ikinci bir poller başlatılmaz.
"""
from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

# Modülü nesne olarak import ediyoruz: production entegrasyonuna özel karar/timing
# talimatını tek yerde ekleyip standalone veri/indikatör motoruna dokunmuyoruz.
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

TELEGRAM ÇIKTI KURALI:
- Markdown #/## işaretleri kullanma; başlıkları düz metin ve uygun emojiyle yaz.
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


def _send(token: str, chat_id: str | int, thread_id: int | None, text: str) -> None:
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    if thread_id is not None:
        payload["message_thread_id"] = thread_id
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json=payload,
        timeout=30,
    )
    if not r.ok:
        raise RuntimeError(f"Telegram sendMessage HTTP {r.status_code}: {r.text[:160]}")


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
        first = header + analysis_part
        first_parts = split_telegram(first)
        action_parts = split_telegram(action_part)
        # Prompt ilk kısmı kısa tutacak şekilde ayarlı. Olağan dışı uzunlukta veri
        # kaybetmek yerine güvenli Telegram parçalama davranışını koruyoruz.
        return first_parts + action_parts

    # Model başlığı beklenmedik biçimde atladıysa içerik kaybolmasın.
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
            _send(token, chat_id, thread_id, part)
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
    """portfolio_tracker manuel poller'inin GPT-aware eşdeğeri.

    `g`, orijinal `_manual_analyzer_poll_loop.__globals__` sözlüğüdür. Bu sayede
    mevcut env/config, yetkilendirme ve eski `_run_manual_analyzer` yolu aynen
    kullanılır; yalnızca `COIN GPT` komutu ek bir route olarak ayrılır.
    """
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
