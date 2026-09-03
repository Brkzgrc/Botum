# -*- coding: utf-8 -*-
"""Deprecated compatibility shim.

GPT Sonnet Analyzer artık `intraday_scanner.py` tarafından açıkça kurulan
runtime hook üzerinden bağlanır. Bu dosya startup mekanizması olarak kullanılmaz;
eski deploy kalıntıları bu modülü import ederse aynı idempotent hook'u kurar.
"""
from anton_scanner.gpt_sonnet_analyzer.runtime_hook import install_anton_gpt_hook

install_anton_gpt_hook()
