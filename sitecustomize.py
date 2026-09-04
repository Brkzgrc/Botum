# -*- coding: utf-8 -*-
"""Compatibility startup hooks shared by repo services.

GPT Sonnet Analyzer keeps its existing idempotent runtime hook. SPOT_SCANNER
gets its independent Telegram switch. Portfolio Tracker gets a process-scoped
manual price refresh hook.
"""
from __future__ import annotations

import os
import sys

_process = os.path.basename(sys.argv[0] or "").lower()

# Only affect the SPOT_SCANNER process. Other services and the manual ZEC GPT
# analyzer keep their existing Telegram configuration untouched.
if _process == "spot_opportunity_scanner.py":
    telegram_enabled = os.getenv("TELEGRAM_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
    if not telegram_enabled:
        os.environ["TELEGRAM_TOKEN"] = ""

# Portfolio's old Yenile button only reloaded stored values. Install a hook only
# for the Portfolio service so manual refresh first fetches live Binance prices.
if _process == "portfolio_tracker.py":
    from portfolio_refresh_hook import install_portfolio_refresh_hook
    install_portfolio_refresh_hook()

from anton_scanner.gpt_sonnet_analyzer.runtime_hook import install_anton_gpt_hook

install_anton_gpt_hook()
