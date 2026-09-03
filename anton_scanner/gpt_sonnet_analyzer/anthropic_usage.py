# -*- coding: utf-8 -*-
"""GPT Sonnet Analyzer icin Anthropic token, maliyet ve sure telemetrisi.

Bu modul production market_analyst_bot.analyze_with_claude fonksiyonunu dar bir
wrapper ile degistirir. Analiz metnini veya karar mantigini degistirmez; yalnizca
Anthropic response usage alanlarini toplayip Render loguna yazar.
"""
from __future__ import annotations

import os
import time
from typing import List

_INSTALLED = False


def _price_per_million(model: str) -> tuple[float | None, float | None]:
    """Maliyet tahmini icin env override destekli fiyatlar.

    Sonnet 5 icin mevcut varsayilan fiyatlar input=$2/M, output=$10/M.
    Model degistirilirse yanlis maliyet yazmamak icin ancak env ile fiyat verilirse
    hesap yapilir.
    """
    in_env = os.getenv("MARKET_ANALYST_INPUT_USD_PER_M", "").strip()
    out_env = os.getenv("MARKET_ANALYST_OUTPUT_USD_PER_M", "").strip()
    if in_env and out_env:
        try:
            return float(in_env), float(out_env)
        except ValueError:
            pass

    if model == "claude-sonnet-5":
        return 2.0, 10.0
    return None, None


def install_usage_tracking() -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    from anton_scanner.gpt_sonnet_analyzer import market_analyst_bot as market
    from anthropic import Anthropic

    def tracked_analyze_with_claude(snapshot: dict) -> str:
        key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY tanimli degil.")

        symbol = str(snapshot.get("pair") or snapshot.get("symbol") or "UNKNOWN")
        model = market.DEFAULT_MODEL
        input_rate, output_rate = _price_per_million(model)
        client = Anthropic(api_key=key)
        prompt = market._analysis_prompt(snapshot)
        messages = [{"role": "user", "content": prompt}]
        chunks: List[str] = []
        total_input = 0
        total_output = 0
        calls = 0
        started = time.perf_counter()

        for continuation in range(market.MAX_CONTINUATIONS + 1):
            call_started = time.perf_counter()
            resp = client.messages.create(
                model=model,
                max_tokens=market.DEFAULT_MAX_TOKENS,
                system=market.SYSTEM_PROMPT,
                messages=messages,
            )
            calls += 1
            usage = getattr(resp, "usage", None)
            input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
            output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
            total_input += input_tokens
            total_output += output_tokens
            call_seconds = time.perf_counter() - call_started
            print(
                f"[SONNET USAGE] {symbol} call={calls} model={model} "
                f"input={input_tokens} output={output_tokens} duration={call_seconds:.1f}s "
                f"stop={getattr(resp, 'stop_reason', None)}",
                flush=True,
            )

            text = "".join(
                block.text for block in resp.content
                if getattr(block, "type", None) == "text"
            ).strip()
            if not text:
                raise RuntimeError("AI analist bos yanit dondurdu.")
            chunks.append(text)

            if getattr(resp, "stop_reason", None) != "max_tokens":
                break
            if continuation >= market.MAX_CONTINUATIONS:
                chunks.append("\n[UYARI: Model azami devam sayisina ulasti.]")
                break
            messages.extend([
                {"role": "assistant", "content": text},
                {
                    "role": "user",
                    "content": "Yanit token sinirinda kesildi. Tam kaldigin yerden devam et; tekrar etme ve analizi mutlaka tamamla.",
                },
            ])

        duration = time.perf_counter() - started
        cost_text = "n/a"
        if input_rate is not None and output_rate is not None:
            cost = (total_input / 1_000_000.0) * input_rate + (total_output / 1_000_000.0) * output_rate
            cost_text = f"${cost:.5f}"

        print(
            f"[SONNET TOTAL] {symbol} calls={calls} input={total_input} output={total_output} "
            f"total={total_input + total_output} cost={cost_text} duration={duration:.1f}s",
            flush=True,
        )
        return "\n\n".join(chunks).strip()

    market.analyze_with_claude = tracked_analyze_with_claude
    _INSTALLED = True
    print("[SONNET USAGE] Token/maliyet telemetrisi yuklendi.", flush=True)
