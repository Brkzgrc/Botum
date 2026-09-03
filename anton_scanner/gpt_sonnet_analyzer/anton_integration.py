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
from typing import Optional

import requests

from anton_scanner.gpt_sonnet_analyzer.market_analyst_bot import analyze_symbol, split_telegram

_GPT_INFLIGHT: set[str] = set()
_GPT_INFLIGHT_LOCK = threading.Lock()


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


def _run_gpt_analysis(pair: str, token: str, chat_id: str | int, thread_id: int | None) -> None:
    with _GPT_INFLIGHT_LOCK:
        if pair in _GPT_INFLIGHT:
            return
        _GPT_INFLIGHT.add(pair)
    try:
        base = pair[:-4] if pair.endswith("USDT") else pair
        analysis = analyze_symbol(base)
        full = f"{base}/USDT — GPT Sonnet Analyzer\n\n{analysis}"
        for part in split_telegram(full):
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
