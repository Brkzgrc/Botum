# -*- coding: utf-8 -*-
"""
PERMANENT REGRESSION SUITE -- AI Analyst Controlled Reliability Fix V1/V1.1 (FROZEN)

Ne test ediyor: invalid_overall/decision_token_leak ve used_factors_mismatch/
canonical_mismatch teshis fonksiyonlarini, corrective-retry orkestrasyonunu
(`run_ai_analyst_with_corrective_retry`) FakeClient ile -- whitelist nedenler
retry tetikler, whitelist-disi nedenler tetiklemez, retry basarili olursa final
PASS, basarisiz olursa MAX 1 retry sonrasi orijinal FAIL korunur -- ve whitelist
mimarisinin (4 reason, sub-reason seti, 18e/watchpoint contract'inin whitelist
disi kalmasi) degismedigini.

**Yalniz deterministic/offline subset** kalicilastirildi: validator/diagnostic
fonksiyonlari dogrudan, orkestrasyon FakeClient monkeypatch'iyle test ediliyor.
GERCEK Anthropic API cagrisi, network, reliability kampanyasi bu dosyaya
GIRMEDI ve girmeyecek -- scratchpad'teki `run_real_ai*.py`,
`market_context_v6_reliability_*.py` gibi gercek-cagri dosyalari kasitli olarak
DISI birakildi.

Network GEREKTIRMEZ. Telemetry, izole bir temp dosyaya yonlendirilir (production
log'una asla yazilmaz) ve test sonunda temizlenir.

Kaynak / provenance: scratchpad/test_controlled_reliability_fix_v1_1.py -- guncel
V6 production'a karsi tekrar dogrulanarak (17/17 PASS) buraya tasindi, granuler
pytest fonksiyonlarina bolunerek. Orijinaldeki telemetry temp dizini artik
temizleniyor (bu turda eklenen hijyen duzeltmesi).
"""
import gc
import importlib.util
import os
import shutil
import sys
import tempfile

import pytest

_PROD_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "kripto_sinyal_sistemi_v6_pro_gui.pyw")
spec = importlib.util.spec_from_file_location('kss', _PROD_FILE)
kss = importlib.util.module_from_spec(spec)
sys.modules['kss'] = kss
spec.loader.exec_module(kss)

_TELEMETRY_DIR = tempfile.mkdtemp(prefix="ai_reliability_test_telemetry_")
_TELEMETRY_PATH = os.path.join(_TELEMETRY_DIR, "isolated.jsonl")
kss._ai_reliability_telemetry_log_path = lambda: _TELEMETRY_PATH


def teardown_module(module):
    gc.collect()
    shutil.rmtree(_TELEMETRY_DIR, ignore_errors=True)


DEFAULT_USABLE = [
    {"factor_id": "ema50_distance", "label": "EMA50 mesafesi", "status": "yes",
     "semantic_role": "entry_timing", "decision_critical_to_statuses": [], "decision_critical_effect": None},
    {"factor_id": "macd_signal", "label": "MACD", "status": "no",
     "semantic_role": "momentum", "decision_critical_to_statuses": [], "decision_critical_effect": None},
    {"factor_id": "exchange_listing_count", "label": "Borsa listesi", "status": "yes",
     "semantic_role": "liquidity", "decision_critical_to_statuses": ["no"], "decision_critical_effect": "veto"},
]


def make_hard_restricted_context(structure_evidence=None):
    return {
        "mode": "HARD_RESTRICTED",
        "decision_context": {"vetos": ["btc_daily_trend"]},
        "usable_factors": DEFAULT_USABLE,
        "unavailable_factors": [{"factor_id": "mvrv_ratio", "label": "MVRV"}],
        "_valid_decision_evidence": ["decision:verdict_rejected", "veto:btc_daily_trend",
                                     "decision:risk_coverage_insufficient"],
        "reassessment_eligible_factors": [],
        "preferred_decision_critical_factor": None,
        "structure_context": None,
        "_valid_structure_evidence": structure_evidence or [],
    }


def hard_restricted_response(overall_evidence, data_limitations=None, used_factors=None):
    data_limitations = data_limitations or []
    return {
        "overall": {"text": "t", "evidence": overall_evidence},
        "why_rejected_or_limited": {"text": "reddedildi", "evidence": ["macd_signal"],
                                     "decision_evidence": ["veto:btc_daily_trend", "decision:verdict_rejected"]},
        "decisive_factors": [],
        "positive_but_insufficient_factors": [],
        "technical_watchpoints": [],
        "data_limitations": data_limitations,
        "used_factors": used_factors if used_factors is not None else ["macd_signal"],
        "mentioned_unavailable_factors": sorted({e for it in data_limitations for e in (it.get("evidence") or [])}),
    }


class FakeClient:
    def __init__(self, primary_raw, retry_raw=None):
        self.primary_raw = primary_raw
        self.retry_raw = retry_raw
        self.primary_calls = 0
        self.retry_calls = 0

    def request_analysis(self, context, mode):
        self.primary_calls += 1
        return self.primary_raw

    def request_corrective_retry(self, context, mode, original_raw, top_reason, diag):
        self.retry_calls += 1
        return self.retry_raw


@pytest.fixture
def with_fake_client():
    def _run(fake, fn):
        orig_analysis = kss.AIAnalystClient.request_analysis
        orig_retry = kss.AIAnalystClient.request_corrective_retry
        kss.AIAnalystClient.request_analysis = staticmethod(fake.request_analysis)
        kss.AIAnalystClient.request_corrective_retry = staticmethod(fake.request_corrective_retry)
        try:
            return fn()
        finally:
            kss.AIAnalystClient.request_analysis = orig_analysis
            kss.AIAnalystClient.request_corrective_retry = orig_retry
    return _run


# ---------- 1: invalid_overall / decision_token_leak teshisi ----------

def test_decision_verdict_rejected_leak_detected():
    ctx = make_hard_restricted_context()
    diag = kss._diagnose_invalid_overall_failure(
        hard_restricted_response(["macd_signal", "decision:verdict_rejected"]), ctx)
    assert diag["sub_reason"] == "decision_token_leak"
    assert diag["detail"]["leaked_tokens"] == ["decision:verdict_rejected"]
    assert diag["detail"]["remaining_evidence"] == ["macd_signal"]


def test_veto_leak_detected():
    ctx = make_hard_restricted_context()
    diag = kss._diagnose_invalid_overall_failure(
        hard_restricted_response(["macd_signal", "veto:btc_daily_trend"]), ctx)
    assert diag["sub_reason"] == "decision_token_leak"
    assert diag["detail"]["leaked_tokens"] == ["veto:btc_daily_trend"]


def test_non_leak_invalid_overall_not_retry_eligible():
    ctx = make_hard_restricted_context()
    diag = kss._diagnose_invalid_overall_failure(
        hard_restricted_response(["hicbir_yerde_olmayan_factor"]), ctx)
    assert diag["sub_reason"] not in kss.AI_ANALYST_RETRY_WHITELIST_SUB_REASONS


def test_all_leak_no_remaining_not_retry_eligible():
    ctx = make_hard_restricted_context()
    diag = kss._diagnose_invalid_overall_failure(
        hard_restricted_response(["decision:verdict_rejected"]), ctx)
    assert diag["sub_reason"] not in kss.AI_ANALYST_RETRY_WHITELIST_SUB_REASONS


def test_leak_plus_other_invalid_not_retry_eligible():
    ctx = make_hard_restricted_context()
    diag = kss._diagnose_invalid_overall_failure(
        hard_restricted_response(["decision:verdict_rejected", "gecersiz_factor_id"]), ctx)
    assert diag["sub_reason"] not in kss.AI_ANALYST_RETRY_WHITELIST_SUB_REASONS


# ---------- 2: used_factors_mismatch / canonical_mismatch teshisi ----------

def test_extra_factor_canonical_excludes_it():
    ctx = make_hard_restricted_context()
    diag = kss._diagnose_used_factors_mismatch_failure(
        hard_restricted_response(["macd_signal"], used_factors=["macd_signal", "mvrv_ratio", "exchange_listing_count"]),
        ctx)
    assert diag["sub_reason"] == "canonical_mismatch"
    assert diag["detail"]["canonical_used_factors"] == ["macd_signal"]


def test_missing_factor_canonical_includes_it():
    ctx = make_hard_restricted_context()
    diag = kss._diagnose_used_factors_mismatch_failure(
        hard_restricted_response(["macd_signal"], used_factors=[]), ctx)
    assert diag["sub_reason"] == "canonical_mismatch"
    assert diag["detail"]["canonical_used_factors"] == ["macd_signal"]


# ---------- 3: whitelist orkestrasyonu (FakeClient, gercek API YOK) ----------

def test_invalid_overall_retry_recovers(with_fake_client):
    ctx = make_hard_restricted_context()
    primary_leak = hard_restricted_response(["macd_signal", "decision:verdict_rejected"])
    retry_fixed = hard_restricted_response(["macd_signal"])
    fake = FakeClient(primary_leak, retry_raw=retry_fixed)
    result = with_fake_client(fake, lambda: kss.run_ai_analyst_with_corrective_retry(ctx, "HARD_RESTRICTED"))
    assert result["retry_triggered"] is True
    assert result["retry_top_reason"] == "invalid_overall"
    assert result["retry_sub_reason"] == "decision_token_leak"
    assert result["retry_ok"] is True
    assert result["final_ok"] is True
    assert fake.primary_calls == 1 and fake.retry_calls == 1


def test_non_leak_invalid_overall_no_retry(with_fake_client):
    ctx = make_hard_restricted_context()
    primary_other = hard_restricted_response(["gecersiz_factor_id"])
    fake = FakeClient(primary_other)
    result = with_fake_client(fake, lambda: kss.run_ai_analyst_with_corrective_retry(ctx, "HARD_RESTRICTED"))
    assert result["retry_triggered"] is False
    assert fake.retry_calls == 0
    assert result["final_ok"] is False
    assert result["final_reason"] == "invalid_overall"


def test_used_factors_mismatch_retry_recovers(with_fake_client):
    ctx = make_hard_restricted_context()
    primary_mismatch = hard_restricted_response(["macd_signal"], used_factors=["macd_signal", "mvrv_ratio"])
    retry_canonical = hard_restricted_response(["macd_signal"], used_factors=["macd_signal"])
    fake = FakeClient(primary_mismatch, retry_raw=retry_canonical)
    result = with_fake_client(fake, lambda: kss.run_ai_analyst_with_corrective_retry(ctx, "HARD_RESTRICTED"))
    assert result["retry_triggered"] is True
    assert result["retry_top_reason"] == "used_factors_mismatch"
    assert result["retry_sub_reason"] == "canonical_mismatch"
    assert result["retry_ok"] is True
    assert result["final_ok"] is True
    assert fake.primary_calls == 1 and fake.retry_calls == 1


def test_used_factors_retry_fails_max_one_retry(with_fake_client):
    """Retry de basarisiz olursa orijinal FAIL'e doner -- IKINCI bir retry ASLA denenmez."""
    ctx = make_hard_restricted_context()
    primary_mismatch2 = hard_restricted_response(["macd_signal"], used_factors=["macd_signal", "mvrv_ratio"])
    retry_still_bad = hard_restricted_response(["macd_signal"], used_factors=["macd_signal", "baska_yanlis"])
    fake = FakeClient(primary_mismatch2, retry_raw=retry_still_bad)
    result = with_fake_client(fake, lambda: kss.run_ai_analyst_with_corrective_retry(ctx, "HARD_RESTRICTED"))
    assert result["retry_triggered"] is True
    assert result["retry_ok"] is False
    assert result["final_ok"] is False
    assert result["final_reason"] == "used_factors_mismatch"
    assert result["final_raw"] == primary_mismatch2
    assert fake.primary_calls == 1 and fake.retry_calls == 1


def test_old_whitelist_reason_still_works(with_fake_client):
    ctx = make_hard_restricted_context()
    primary_dl = hard_restricted_response(["macd_signal"], data_limitations=[{"text": "kapsam dusuk", "evidence": []}])
    retry_dl_fixed = hard_restricted_response(["macd_signal"], data_limitations=[])
    fake = FakeClient(primary_dl, retry_raw=retry_dl_fixed)
    result = with_fake_client(fake, lambda: kss.run_ai_analyst_with_corrective_retry(ctx, "HARD_RESTRICTED"))
    assert result["retry_triggered"] is True
    assert result["retry_top_reason"] == "invalid_data_limitations"
    assert result["final_ok"] is True


def test_watchpoint_18e_contract_unaffected(with_fake_client):
    ctx = make_hard_restricted_context(structure_evidence=["structure:nearest_support", "market_map:support_1"])
    primary_wp = hard_restricted_response(["macd_signal"])
    primary_wp["technical_watchpoints"] = [{"category": "weakens", "zone_ref": "nearest_support",
                                             "meaning": "m", "evidence": ["market_map:support_1"]}]
    fake = FakeClient(primary_wp)
    result = with_fake_client(fake, lambda: kss.run_ai_analyst_with_corrective_retry(ctx, "HARD_RESTRICTED"))
    assert result["final_reason"] == "invalid_technical_watchpoints"
    assert result["retry_triggered"] is False
    assert fake.retry_calls == 0


def test_unknown_candidate_id_still_not_whitelisted(with_fake_client):
    eligible_ids = [f["factor_id"] for f in DEFAULT_USABLE]
    candidates = kss._generate_reassessment_candidates(DEFAULT_USABLE, eligible_ids, "exchange_listing_count")
    candidates_by_id = {c["candidate_id"]: c for c in candidates}
    ctx = {
        "mode": "NORMAL", "decision_context": {"vetos": []}, "usable_factors": DEFAULT_USABLE,
        "unavailable_factors": [], "_valid_decision_evidence": [],
        "reassessment_eligible_factors": eligible_ids, "preferred_decision_critical_factor": "exchange_listing_count",
        "structure_context": None, "_valid_structure_evidence": [], "_reassessment_candidates_by_id": candidates_by_id,
    }
    primary_unknown = {
        "overall": {"text": "t", "evidence": ["exchange_listing_count"]},
        "supporting_factors": [], "risks_conflicts": [], "entry_assessment": None,
        "reassessment_triggers": [{"candidate_id": "ps2:macd_signal+hayali", "meaning": "m"}],
        "technical_watchpoints": [], "data_limitations": [],
        "used_factors": ["exchange_listing_count"], "mentioned_unavailable_factors": [],
    }
    fake = FakeClient(primary_unknown)
    result = with_fake_client(fake, lambda: kss.run_ai_analyst_with_corrective_retry(ctx, "NORMAL"))
    assert result["retry_triggered"] is False
    assert fake.retry_calls == 0
    assert result["final_reason"] == "invalid_reassessment_triggers"


# ---------- 4: whitelist mimarisi (dokunulmamis) ----------

def test_whitelist_reasons_exact_set():
    assert kss.AI_ANALYST_RETRY_WHITELIST_REASONS == frozenset({
        "invalid_data_limitations", "invalid_reassessment_triggers",
        "invalid_overall", "used_factors_mismatch"})


def test_whitelist_sub_reasons_include_new_two():
    assert {"decision_token_leak", "canonical_mismatch"} <= kss.AI_ANALYST_RETRY_WHITELIST_SUB_REASONS


def test_watchpoint_reason_never_whitelisted():
    assert "invalid_technical_watchpoints" not in kss.AI_ANALYST_RETRY_WHITELIST_REASONS


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
