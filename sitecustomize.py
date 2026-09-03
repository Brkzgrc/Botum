# -*- coding: utf-8 -*-
"""Dar kapsamlı Anton Telegram entegrasyon kancası.

Python `site` modülü varsa bu dosyayı startup'ta otomatik import eder. Buradaki
tek değişiklik, adı tam olarak `_manual_analyzer_poll_loop` olan thread target'ini
GPT-aware eşdeğeriyle sarmalamaktır. Diğer servis/thread'ler değişmez.

Aynı Telegram token'i için ikinci getUpdates consumer'i açılmaz. Entegrasyon
import edilemezse fail-open davranılır ve eski Anton poller'i aynen korunur.
"""
from __future__ import annotations

import threading

_ORIGINAL_THREAD_INIT = threading.Thread.__init__


def _anton_thread_init(self, *args, **kwargs):
    target = kwargs.get("target")
    if target is None and len(args) >= 2:
        target = args[1]

    if getattr(target, "__name__", "") == "_manual_analyzer_poll_loop":
        try:
            from anton_market_analyst.anton_integration import gpt_aware_manual_poll_loop

            target_globals = target.__globals__

            def wrapped():
                return gpt_aware_manual_poll_loop(target_globals)

            if "target" in kwargs:
                kwargs["target"] = wrapped
            elif len(args) >= 2:
                args = list(args)
                args[1] = wrapped
                args = tuple(args)
        except Exception as exc:
            print(
                f"[ANTON GPT HOOK] entegrasyon yüklenemedi, eski poller korunuyor: {exc}",
                flush=True,
            )

    return _ORIGINAL_THREAD_INIT(self, *args, **kwargs)


threading.Thread.__init__ = _anton_thread_init
