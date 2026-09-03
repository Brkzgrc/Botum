# -*- coding: utf-8 -*-
"""GPT Sonnet Analyzer runtime entegrasyon kancası.

`portfolio_tracker.py` bu kancayı, kendi Telegram manuel analyzer thread'ini
başlatmadan önce açıkça yükler. Böylece `sitecustomize.py` gibi Python startup
sihirlerine güvenilmez.

Kanca yalnız target adı `_manual_analyzer_poll_loop` olan Thread'i değiştirir.
Diğer thread'lere dokunmaz. GPT entegrasyonu yüklenemezse mevcut Anton poller'i
aynen çalışmaya devam eder.
"""
from __future__ import annotations

import threading

_INSTALLED = False
_ORIGINAL_THREAD_INIT = None


def install_anton_gpt_hook() -> None:
    global _INSTALLED, _ORIGINAL_THREAD_INIT
    if _INSTALLED:
        return

    _ORIGINAL_THREAD_INIT = threading.Thread.__init__

    def _anton_thread_init(self, *args, **kwargs):
        target = kwargs.get("target")
        if target is None and len(args) >= 2:
            target = args[1]

        if getattr(target, "__name__", "") == "_manual_analyzer_poll_loop":
            try:
                from anton_scanner.gpt_sonnet_analyzer.anton_integration import (
                    gpt_aware_manual_poll_loop,
                )

                target_globals = target.__globals__

                def wrapped():
                    return gpt_aware_manual_poll_loop(target_globals)

                if "target" in kwargs:
                    kwargs["target"] = wrapped
                elif len(args) >= 2:
                    args = list(args)
                    args[1] = wrapped
                    args = tuple(args)

                print(
                    "[ANTON GPT] Manuel Telegram poller GPT Sonnet Analyzer ile bağlandı.",
                    flush=True,
                )
            except Exception as exc:
                print(
                    f"[ANTON GPT] Entegrasyon yüklenemedi; eski poller korunuyor: {exc}",
                    flush=True,
                )

        return _ORIGINAL_THREAD_INIT(self, *args, **kwargs)

    threading.Thread.__init__ = _anton_thread_init
    _INSTALLED = True
    print("[ANTON GPT] Runtime hook yüklendi.", flush=True)
