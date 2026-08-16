# -*- coding: utf-8 -*-
"""
PERMANENT REGRESSION SUITE -- AI Analyst Reliability Telemetry (offline contract)

Ne test ediyor: run_ai_analyst_with_corrective_retry()'in her cagrida TAM BIR
telemetry event yazdigini (primary_ok/retry_triggered/retry_top_reason/
retry_sub_reason/retry_ok/final_ok/attempt_count alanlari dogru), retry
yaşanmadiginda retry alanlarinin None kaldigini, telemetry yazma hatasinin
AI orchestration sonucunu ETKILEMEDIGINI (fail-open -- gecersiz bir path
verilse bile exception production akisina sizmaz), _log_ai_validation_failure
ile _log_ai_reliability_telemetry'nin BAGIMSIZ iki fonksiyon oldugunu, ve
concurrent/atomic append'in satir bozulmasi URETMEDIGINI (30 thread, ayni dosya).

**Yalniz production kodunu dogrudan test eden kisim** kalicilastirildi.
scratchpad/ai_reliability_summary.py (harici offline-analiz araci, production'in
parcasi degil) ve tarihsel fixture cross-check testi bu dosyaya BILINCLI OLARAK
alinmadi -- onlar bir scratchpad yardimci aracini ve gecmis bir anlik goruntuyu
dogruluyor, canli production contract'ini degil.

Network GEREKTIRMEZ. Telemetry HER ZAMAN izole bir temp dosyaya yonlendirilir --
production log'una (`ai_analyst_reliability.jsonl`) ASLA yazilmaz. Test sonunda
temp dizin temizlenir.

Kaynak / provenance: scratchpad/test_ai_reliability_telemetry.py (bolum A-H +
concurrent) -- guncel V6 production'a karsi tekrar dogrulanarak buraya tasindi,
granuler pytest fonksiyonlarina bolunerek.
"""
import gc
import importlib.util
import inspect
import json
import os
import shutil
import sys
import tempfile
import threading

import pytest

_PROD_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "kripto_sinyal_sistemi_v6_pro_gui.pyw")
spec = importlib.util.spec_from_file_location('kss', _PROD_FILE)
kss = importlib.util.module_from_spec(spec)
sys.modules['kss'] = kss
spec.loader.exec_module(kss)

_TMP_DIR = tempfile.mkdtemp(prefix="ai_reliability_telemetry_test_")
_TELEMETRY_PATH = os.path.join(_TMP_DIR, "isolated.jsonl")


def teardown_module(module):
    gc.collect()
    shutil.rmtree(_TMP_DIR, ignore_errors=True)


def _reset():
    if os.path.exists(_TELEMETRY_PATH):
        os.remove(_TELEMETRY_PATH)


def _read_events():
    if not os.path.exists(_TELEMETRY_PATH):
        return []
    with open(_TELEMETRY_PATH, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


DEFAULT_USABLE = [
    {"factor_id": "ema50_distance", "label": "EMA50 mesafesi", "status": "yes",
     "semantic_role": "entry_timing", "decision_critical_to_statuses": [], "decision_critical_effect": None},
    {"factor_id": "macd_signal", "label": "MACD", "status": "no",
     "semantic_role": "momentum", "decision_critical_to_statuses": [], "decision_critical_effect": None},
]


def _make_ctx(symbol="BTC"):
    return {
        "mode": "HARD_RESTRICTED",
        "decision_context": {"vetos": ["btc_daily_trend"], "symbol": symbol},
        "usable_factors": DEFAULT_USABLE,
        "unavailable_factors": [{"factor_id": "mvrv_ratio", "label": "MVRV"}],
        "_valid_decision_evidence": ["decision:verdict_rejected", "veto:btc_daily_trend",
                                     "decision:risk_coverage_insufficient"],
        "reassessment_eligible_factors": [],
        "preferred_decision_critical_factor": None,
        "structure_context": None,
        "_valid_structure_evidence": [],
    }


def _response(overall_evidence=("macd_signal",), data_limitations=None, used_factors=None):
    data_limitations = data_limitations or []
    return {
        "overall": {"text": "t", "evidence": list(overall_evidence)},
        "why_rejected_or_limited": {"text": "reddedildi", "evidence": ["macd_signal"],
                                     "decision_evidence": ["veto:btc_daily_trend", "decision:verdict_rejected"]},
        "decisive_factors": [], "positive_but_insufficient_factors": [], "technical_watchpoints": [],
        "data_limitations": data_limitations,
        "used_factors": used_factors if used_factors is not None else ["macd_signal"],
        "mentioned_unavailable_factors": sorted({e for it in data_limitations for e in (it.get("evidence") or [])}),
    }


class FakeClient:
    def __init__(self, primary_raw, retry_raw=None):
        self.primary_raw = primary_raw
        self.retry_raw = retry_raw
        self.retry_calls = 0

    def request_analysis(self, context, mode):
        return self.primary_raw

    def request_corrective_retry(self, context, mode, original_raw, top_reason, diag):
        self.retry_calls += 1
        return self.retry_raw


@pytest.fixture
def with_fake_client():
    def _run(fake, fn):
        orig_a = kss.AIAnalystClient.request_analysis
        orig_r = kss.AIAnalystClient.request_corrective_retry
        kss.AIAnalystClient.request_analysis = staticmethod(fake.request_analysis)
        kss.AIAnalystClient.request_corrective_retry = staticmethod(fake.request_corrective_retry)
        try:
            return fn()
        finally:
            kss.AIAnalystClient.request_analysis = orig_a
            kss.AIAnalystClient.request_corrective_retry = orig_r
    return _run


@pytest.fixture
def with_isolated_telemetry_path():
    def _run(fn):
        orig = kss._ai_reliability_telemetry_log_path
        kss._ai_reliability_telemetry_log_path = lambda: _TELEMETRY_PATH
        try:
            return fn()
        finally:
            kss._ai_reliability_telemetry_log_path = orig
    return _run


def test_primary_pass_no_retry_single_event(with_fake_client, with_isolated_telemetry_path):
    _reset()
    fake = FakeClient(_response())
    with_isolated_telemetry_path(lambda: with_fake_client(
        fake, lambda: kss.run_ai_analyst_with_corrective_retry(_make_ctx("BTC"), "HARD_RESTRICTED", request_id="rid-A")))
    events = _read_events()
    assert len(events) == 1
    e = events[0]
    assert e["symbol"] == "BTC" and e["mode"] == "HARD_RESTRICTED" and e["request_id"] == "rid-A"
    assert e["primary_ok"] is True and e["retry_triggered"] is False
    assert e["retry_top_reason"] is None and e["retry_sub_reason"] is None and e["retry_ok"] is None
    assert e["final_ok"] is True and e["attempt_count"] == 1


def test_primary_fail_non_whitelisted_no_retry(with_fake_client, with_isolated_telemetry_path):
    _reset()
    bad_overall = _response(overall_evidence=("hicbir_yerde_olmayan_factor",))
    fake = FakeClient(bad_overall)
    with_isolated_telemetry_path(lambda: with_fake_client(
        fake, lambda: kss.run_ai_analyst_with_corrective_retry(_make_ctx("ZEC"), "HARD_RESTRICTED", request_id="rid-B")))
    events = _read_events()
    assert len(events) == 1
    e = events[0]
    assert e["primary_ok"] is False and e["retry_triggered"] is False
    assert e["final_ok"] is False and e["attempt_count"] == 1


def test_retry_recovers_single_event(with_fake_client, with_isolated_telemetry_path):
    _reset()
    primary_leak = _response(overall_evidence=("macd_signal", "decision:verdict_rejected"))
    retry_fixed = _response()
    fake = FakeClient(primary_leak, retry_raw=retry_fixed)
    with_isolated_telemetry_path(lambda: with_fake_client(
        fake, lambda: kss.run_ai_analyst_with_corrective_retry(_make_ctx("ETH"), "HARD_RESTRICTED", request_id="rid-C")))
    events = _read_events()
    assert len(events) == 1
    e = events[0]
    assert e["primary_ok"] is False and e["retry_triggered"] is True
    assert e["retry_top_reason"] == "invalid_overall" and e["retry_sub_reason"] == "decision_token_leak"
    assert e["retry_ok"] is True and e["final_ok"] is True and e["attempt_count"] == 2


def test_retry_fails_single_event(with_fake_client, with_isolated_telemetry_path):
    _reset()
    primary_leak2 = _response(overall_evidence=("macd_signal", "decision:verdict_rejected"))
    retry_still_bad = _response(overall_evidence=("macd_signal", "veto:btc_daily_trend"))
    fake = FakeClient(primary_leak2, retry_raw=retry_still_bad)
    with_isolated_telemetry_path(lambda: with_fake_client(
        fake, lambda: kss.run_ai_analyst_with_corrective_retry(_make_ctx("XRP"), "HARD_RESTRICTED", request_id="rid-D")))
    events = _read_events()
    assert len(events) == 1
    e = events[0]
    assert e["retry_triggered"] is True and e["retry_ok"] is False
    assert e["final_ok"] is False and e["final_reason"] == "invalid_overall"
    assert e["attempt_count"] == 2 and fake.retry_calls == 1


def test_unknown_candidate_id_visible_no_retry(with_fake_client, with_isolated_telemetry_path):
    _reset()
    eligible_ids = [f["factor_id"] for f in DEFAULT_USABLE]
    candidates = kss._generate_reassessment_candidates(DEFAULT_USABLE, eligible_ids, None)
    candidates_by_id = {c["candidate_id"]: c for c in candidates}
    ctx = {
        "mode": "NORMAL", "decision_context": {"vetos": [], "symbol": "SOL"}, "usable_factors": DEFAULT_USABLE,
        "unavailable_factors": [], "_valid_decision_evidence": [],
        "reassessment_eligible_factors": eligible_ids, "preferred_decision_critical_factor": None,
        "structure_context": None, "_valid_structure_evidence": [], "_reassessment_candidates_by_id": candidates_by_id,
    }
    primary_unknown = {
        "overall": {"text": "t", "evidence": ["macd_signal"]},
        "supporting_factors": [], "risks_conflicts": [], "entry_assessment": None,
        "reassessment_triggers": [{"candidate_id": "ps2:macd_signal+hayali", "meaning": "m"}],
        "technical_watchpoints": [], "data_limitations": [],
        "used_factors": ["macd_signal"], "mentioned_unavailable_factors": [],
    }
    fake = FakeClient(primary_unknown)
    with_isolated_telemetry_path(lambda: with_fake_client(
        fake, lambda: kss.run_ai_analyst_with_corrective_retry(ctx, "NORMAL", request_id="rid-E")))
    events = _read_events()
    assert len(events) == 1
    e = events[0]
    assert e["primary_reason"] == "invalid_reassessment_triggers"
    assert e["retry_triggered"] is False and fake.retry_calls == 0
    assert e["final_reason"] == "invalid_reassessment_triggers"

    diag = kss._diagnose_reassessment_triggers_failure(primary_unknown, ctx)
    assert diag["sub_reason"] == "unknown_candidate_id"
    assert diag["sub_reason"] not in kss.AI_ANALYST_RETRY_WHITELIST_SUB_REASONS


def test_telemetry_write_failure_is_fail_open(with_fake_client):
    """Telemetry yazma HATA verirse (ornegin gecersiz path), AI orchestration
    sonucu ETKILENMEMELI -- exception production akisina asla sizmamali."""
    _reset()
    orig_path_fn = kss._ai_reliability_telemetry_log_path
    kss._ai_reliability_telemetry_log_path = lambda: "Z:\\bu_surucu_yok\\hic_olmayan_dizin\\telemetry.jsonl"
    fake = FakeClient(_response())
    try:
        result = with_fake_client(fake, lambda: kss.run_ai_analyst_with_corrective_retry(
            _make_ctx("BTC"), "HARD_RESTRICTED", request_id="rid-F"))
        assert result["final_ok"] is True and result["primary_ok"] is True
    finally:
        kss._ai_reliability_telemetry_log_path = orig_path_fn


def test_log_ai_validation_failure_signature_and_independence():
    sig = inspect.signature(kss._log_ai_validation_failure)
    assert list(sig.parameters) == ["symbol", "request_id", "mode", "reason", "raw", "context"]
    assert callable(kss._log_ai_validation_failure)
    assert kss._log_ai_validation_failure is not kss._log_ai_reliability_telemetry


def test_concurrent_writes_no_corruption(with_isolated_telemetry_path):
    _reset()
    n_threads = 30

    def worker_write(i):
        ev = kss._build_ai_reliability_telemetry_event(
            "SYM", "HARD_RESTRICTED", f"rid-conc-{i}",
            {"primary_ok": True, "primary_reason": "ok", "retry_triggered": False, "retry_top_reason": None,
             "retry_sub_reason": None, "retry_ok": None, "final_ok": True, "final_reason": "ok"}, 0.1)
        kss._log_ai_reliability_telemetry(ev)

    def _run():
        threads = [threading.Thread(target=worker_write, args=(i,)) for i in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    with_isolated_telemetry_path(_run)
    events = _read_events()
    assert len(events) == n_threads, f"beklenen={n_threads}, gercek={len(events)}"
    request_ids = {e.get("request_id") for e in events}
    assert len(request_ids) == n_threads, "satir bozulmasi/karisma tespit edildi"


def test_production_log_not_touched_by_isolated_path():
    """_ai_reliability_telemetry_log_path izole edilmeden, gercek default'un
    production dosya adini dondurdugunu (ama BU TESTIN ona hic yazmadigini)
    dogrular -- yalniz sozlesme kontrolu, gercekten yazma yapilmaz."""
    default_path = kss._ai_reliability_telemetry_log_path()
    assert isinstance(default_path, str) and default_path.endswith(".jsonl")
    assert default_path != _TELEMETRY_PATH


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
