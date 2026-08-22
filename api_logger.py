# -*- coding: utf-8 -*-
"""
API Kullanım Loglama — Tüm Claude API çağrıları buradan loglanır.

Log formatı (Render + JSONL dosyası):
  [API_USAGE] module=claude_analyzer model=haiku prompt_v=1.0
               in=1247 out=298 total=1545 cost=$0.0013 dur=2.3s ts=14:35
"""

import os
import json
import time
from datetime import datetime, timezone, timedelta

TR_TZ    = timezone(timedelta(hours=3))
_DATA_DIR = os.getenv("DATA_DIR", "/tmp")
_LOG_FILE = os.path.join(_DATA_DIR, "api_usage.jsonl")

# Fiyatlar: $/1M token (Anthropic 2026 — güncel fiyatlara göre güncelle)
_PRICES = {
    "haiku":  {"in": 0.80,  "out": 4.00},   # claude-haiku-4-5
    "sonnet": {"in": 3.00,  "out": 15.00},  # claude-sonnet-4-6
    "opus":   {"in": 15.00, "out": 75.00},  # claude-opus-*
    "gemini_flash_lite": {"in": 0.00, "out": 0.00},  # ücretsiz Gemini katmanı
}


def _model_key(model: str) -> str:
    m = model.lower().replace("-", "_")
    if "gemini" in m and "flash_lite" in m: return "gemini_flash_lite"
    if "haiku"  in m: return "haiku"
    if "sonnet" in m: return "sonnet"
    if "opus"   in m: return "opus"
    return "unknown"


def log_usage(
    module:       str,
    model:        str,
    prompt_v:     str,
    in_tok:       int,
    out_tok:      int,
    duration:     float,
    prompt_chars: int = 0,
):
    """Her Claude API çağrısından sonra çağrılır."""
    mk     = _model_key(model)
    prices = _PRICES.get(mk, {"in": 0.0, "out": 0.0})
    cost   = (in_tok * prices["in"] + out_tok * prices["out"]) / 1_000_000
    total  = in_tok + out_tok
    ts_tr  = datetime.now(timezone.utc).astimezone(TR_TZ)
    ts_str = ts_tr.strftime("%Y-%m-%d %H:%M")
    cpt    = round(prompt_chars / in_tok, 2) if in_tok > 0 and prompt_chars > 0 else None

    # Render log satırı
    chars_part = f" chars={prompt_chars} cpt={cpt}" if cpt is not None else ""
    print(
        f"[API_USAGE] module={module} model={mk} prompt_v={prompt_v} "
        f"in={in_tok} out={out_tok} total={total}"
        f"{chars_part} cost=${cost:.4f} dur={duration:.1f}s ts={ts_str}",
        flush=True,
    )

    # JSONL dosyası
    record = {
        "ts":           ts_str,
        "module":       module,
        "model":        mk,
        "prompt_v":     prompt_v,
        "in_tok":       in_tok,
        "out_tok":      out_tok,
        "total_tok":    total,
        "prompt_chars": prompt_chars or None,
        "chars_per_tok": cpt,
        "cost_usd":     round(cost, 6),
        "dur_s":        round(duration, 2),
    }
    try:
        with open(_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[API_LOGGER] JSONL yazma hatası: {e}", flush=True)
