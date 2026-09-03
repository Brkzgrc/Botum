# -*- coding: utf-8 -*-
"""Dar kapsamlı Anton Telegram entegrasyon kancası.

Python `site` modulu varsa bu dosyayi startup'ta otomatik import eder. Buradaki
tek degisiklik, adi tam olarak `_manual_analyzer_poll_loop` olan thread target'ini
GPT-aware esdegeriyle sarmalamaktir. Diger servis/thread'ler degismez.

Neden: portfolio_tracker.py cok buyuk ve production'da aktif; bu feature branch'te
mevcut dosyanin binlerce satirini yeniden yazmadan geri alinabilir bir entegrasyon
saglanir. Ayni Telegram token'i icin ikinci getUpdates consumer'i acilmaz.
"""
from __future__ import annotations

import threading

_ORIGINAL_THREAD_INIT = threading.Thread.__init__


def _anton_thread_init(self, *args, **kwargs):
    target = kwargs.get("target")
    # Thread(target=...) positional verilirse CPython imzasinda ikinci positional
    # arg target'tir. Mevcut portfolio_tracker keyword kullaniyor; bu fallback
    # yalniz savunmaci uyumluluk icindir.
    if target is None and len(args) >= 2:
        target = args[1]

    if getattr(target, "__name__", "") == "_manual_analyzer_poll_loop":
        try:
            from anton_market_analyst.anton_integration import gpt_aware_manual_poll_loop

            wrapped = lambda: gpt_aware_manual_poll_loop(target.__globals__)
            if "target" in kwargs:
                kwargs["target"] = wrapped
            elif len(args) >= 2:
                args = list(args)
                args[1] = wrapped
                args = tuple(args)
        except Exception as exc:
            # Entegrasyon import edilemezse fail-open: mevcut Anton poller'i
            # aynen calisir; GPT route devreye girmez ama production bozulmaz.
            print(f"[ANTON GPT HOOK] entegrasyon yüklenemedi, eski poller korunuyor: {exc}", flush=True)

    return _ORIGINAL_THREAD_INIT(self, *args, **kwargs)


threading.Thread.__init__ = _anton_thread_init
