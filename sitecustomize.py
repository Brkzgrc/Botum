# -*- coding: utf-8 -*-
"""Compatibility startup hooks shared by repo services.

GPT Sonnet Analyzer keeps its existing idempotent runtime hook.  SPOT_SCANNER
also gets an independent Telegram switch without changing Portfolio delivery.
"""
from __future__ import annotations

import os
import sys

# Only affect the SPOT_SCANNER process. Other services and the manual ZEC GPT
# analyzer keep their existing Telegram configuration untouched.
if os.path.basename(sys.argv[0] or "").lower() == "spot_opportunity_scanner.py":
    telegram_enabled = os.getenv("TELEGRAM_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
    if not telegram_enabled:
        os.environ["TELEGRAM_TOKEN"] = ""

from anton_scanner.gpt_sonnet_analyzer.runtime_hook import install_anton_gpt_hook

install_anton_gpt_hook()
