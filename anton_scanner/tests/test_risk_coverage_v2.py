# -*- coding: utf-8 -*-
"""
PERMANENT REGRESSION SUITE -- Risk Coverage V2 (CLOSED)

Ne test ediyor: global coverage/confidence formulu, structurally-unavailable
faktorlerin denominator disi tutulmasi (clear_support/rr_ratio/token_unlock_risk),
risk_breadth/risk_breadth_total (Phase 2: recent_bad_event + volatility_risk),
risk_reliable gate (coverage yuzdesi TEK BASINA reliability acmiyor), veto
precedence (has_veto her zaman reliability'den once), verdict fail-closed metni
(legacy '%50' ifadesi kalmadi), AI Analyst mode secimi (NORMAL/LIMITED), ve
History legacy safety (volatility_risk sorusu hic olmayan eski kayitlar icin
compute_risk_reliable/fallback_risk_coverage davranisi).

Network GEREKTIRMEZ, DB'ye dokunmaz, tamamen deterministic.

Kaynak / provenance: scratchpad/risk_coverage_v2_offline_tests.py (breadth/
reliability/legacy) + risk_coverage_v2_semantic_closure_tests.py (verdict metni/
AI mode) -- guncel V6 production'a karsi tekrar dogrulanarak (36+13=49/49 PASS)
buraya birlestirilerek tasindi.
"""
import importlib.util
import os
import sys

_PROD_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "kripto_sinyal_sistemi_v6_pro_gui.pyw")
spec = importlib.util.spec_from_file_location('kss', _PROD_FILE)
kss = importlib.util.module_from_spec(spec)
sys.modules['kss'] = kss
spec.loader.exec_module(kss)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
    else:
        FAIL.append((name, detail))
        print(f"  [FAIL] {name}  {detail}")
    return cond


ALWAYS_NODATA_IN_REAL_LEVEL1 = {"clear_support", "risk_reward_ratio", "token_unlock_risk",
                                 "dxy_trend", "etf_inflow", "mvrv_ratio", "sopr_recovery"}


def build_answers(kss_mod, overrides=None, all_optional_on=True):
    overrides = overrides or {}
    answers = []
    for stage in kss_mod.STAGES_CONFIG:
        for item in stage.items:
            fid = kss_mod._FACTOR_ID_BY_LABEL.get(item.label, "?")
            disabled = bool(getattr(item, "optional", False) and not all_optional_on)
            default_ans = "nodata" if fid in ALWAYS_NODATA_IN_REAL_LEVEL1 else "yes"
            ans = "nodata" if disabled else overrides.get(fid, default_ans)
            answers.append({"stage": stage.title, "question": item.label,
                             "answer": ans, "weight": item.weight, "_item": item,
                             "disabled": disabled})
    return answers


def run():
    se = kss.ScoreEngine(kss.SIGNAL_STAGE_WEIGHTS, kss.RISK_STAGE_WEIGHTS, kss.VetoEngine(kss.VETO_RULES))

    print("=" * 90)
    print("A-C) GLOBAL COVERAGE / CONFIDENCE / DISABLED INVARIANT")
    print("=" * 90)
    answers = build_answers(kss)
    report = se.full_report(answers, kss.STAGES_CONFIG, "TESTCOIN")
    in_scope = [a for a in answers if not a.get("disabled")]
    valid = [a for a in in_scope if a.get("answer") != "nodata"]
    manual_coverage = round(len(valid) / len(in_scope) * 100, 1) if in_scope else 0
    check("A) global coverage formulu degismedi (manuel hesap == report['coverage'])",
          report["coverage"] == manual_coverage, f"report={report['coverage']} manual={manual_coverage}")
    check("A) total_all icinde structurally-unavailable risk faktorleri hala sayiliyor (30 soru)",
          len(in_scope) == 30, f"len={len(in_scope)}")

    expected_conf, expected_desc = se.confidence_level(manual_coverage)
    check("B) confidence, saf coverage formulunden degismeden turetiliyor",
          report["confidence"] == expected_conf, f"report={report['confidence']} expected={expected_conf}")

    for fid in ("clear_support", "risk_reward_ratio", "token_unlock_risk"):
        a = next(a for a in report["answers"] if kss._FACTOR_ID_BY_LABEL.get(a["question"]) == fid)
        check(f"C) {fid} disabled == False (dokunulmadi)", a.get("disabled") == False, f"disabled={a.get('disabled')}")

    print("\n" + "=" * 90)
    print("F-I) recent_bad_event yes/wait/no/nodata -- coverage/breadth/reliable")
    print("=" * 90)
    for label, ans, exp_cov, exp_breadth, exp_reliable in [
        ("F) yes", "yes", 50.0, 1, False),
        ("G) wait", "wait", 50.0, 1, False),
        ("H) no", "no", 50.0, 1, False),
        ("I) nodata", "nodata", 0.0, 0, False),
    ]:
        a2 = build_answers(kss, overrides={"recent_bad_event": ans, "volatility_risk": "nodata"})
        r2 = se.full_report(a2, kss.STAGES_CONFIG, "TESTCOIN")
        check(f"{label}: risk_coverage=={exp_cov}", r2["risk_coverage"] == exp_cov, f"got={r2['risk_coverage']}")
        check(f"{label}: risk_breadth=={exp_breadth}", r2["risk_breadth"] == exp_breadth, f"got={r2['risk_breadth']}")
        check(f"{label}: risk_reliable=={exp_reliable}", r2["risk_reliable"] == exp_reliable, f"got={r2['risk_reliable']}")
        check(f"{label}: risk_breadth_total==2", r2["risk_breadth_total"] == 2, f"got={r2['risk_breadth_total']}")

    print("\n" + "=" * 90)
    print("J/K) FUTURE BREADTH=2 SENTETIK CONTRACT (gecici monkeypatch, sonunda restore)")
    print("=" * 90)
    _orig_struct = kss.STRUCTURALLY_UNAVAILABLE_FACTOR_IDS
    try:
        kss.STRUCTURALLY_UNAVAILABLE_FACTOR_IDS = frozenset(_orig_struct - {"clear_support"})
        a3 = build_answers(kss, overrides={"recent_bad_event": "yes", "clear_support": "yes",
                                            "volatility_risk": "nodata"})
        r3 = se.full_report(a3, kss.STAGES_CONFIG, "TESTCOIN")
        check("J) breadth=2 (recent_bad_event+clear_support) -> reliable=True",
              r3["risk_breadth"] == 2 and r3["risk_reliable"] is True,
              f"breadth={r3['risk_breadth']} reliable={r3['risk_reliable']}")
        check("J) coverage=66.7 (2/3)", abs(r3["risk_coverage"] - 66.7) < 0.1, f"got={r3['risk_coverage']}")

        a4 = build_answers(kss, overrides={"recent_bad_event": "yes", "clear_support": "nodata",
                                            "volatility_risk": "nodata"})
        r4 = se.full_report(a4, kss.STAGES_CONFIG, "TESTCOIN")
        check("K) breadth=1/3 -> reliable=False (coverage yuzdesi tek basina reliability acmiyor)",
              r4["risk_reliable"] is False, f"coverage={r4['risk_coverage']} reliable={r4['risk_reliable']}")
    finally:
        kss.STRUCTURALLY_UNAVAILABLE_FACTOR_IDS = _orig_struct

    print("\n" + "=" * 90)
    print("L) RISK_SCORE INVARIANT")
    print("=" * 90)
    scores = {}
    for ans in ("yes", "wait", "no", "nodata"):
        a5 = build_answers(kss, overrides={"recent_bad_event": ans, "volatility_risk": "nodata"})
        r5 = se.full_report(a5, kss.STAGES_CONFIG, "TESTCOIN")
        scores[ans] = r5["risk_score"]
    check("L) risk_score yalniz recent_bad_event'e bagli",
          scores["yes"] is not None and scores["nodata"] is None, f"{scores}")

    print("\n" + "=" * 90)
    print("M) VETO PRECEDENCE")
    print("=" * 90)
    v_title, _, _ = se.verdict(90, 100, True, "Yüksek", 100, True, False)
    check("M) has_veto=True -> 'Elenir' (risk_reliable durumundan tamamen bagimsiz)",
          v_title == "Elenir", f"got={v_title}")

    print("\n" + "=" * 90)
    print("N) VERDICT PHASE-1 PARITY (breadth=1, fail-closed generic aile)")
    print("=" * 90)
    for ans in ("yes", "wait"):
        a7 = build_answers(kss, overrides={"recent_bad_event": ans, "volatility_risk": "nodata"})
        r7 = se.full_report(a7, kss.STAGES_CONFIG, "TESTCOIN2")
        is_generic = r7["verdict_title"] in (
            "Güçlü sinyal — risk verisi yetersiz", "İzleme listesi — risk verisi yetersiz",
            "Sinyal zayıf — risk verisi yetersiz")
        check(f"N) recent_bad_event={ans}: breadth=1 -> generic 'risk verisi yetersiz' ailesi",
              is_generic, f"verdict_title={r7['verdict_title']}")
    a7n = build_answers(kss, overrides={"recent_bad_event": "no"})
    r7n = se.full_report(a7n, kss.STAGES_CONFIG, "TESTCOIN2")
    check("N) recent_bad_event=no: VETO tetikleniyor -> 'Elenir'", r7n["verdict_title"] == "Elenir",
          f"got={r7n['verdict_title']}")

    print("\n" + "=" * 90)
    print("O) AI MODE PHASE-1 PARITY")
    print("=" * 90)
    a8 = build_answers(kss, overrides={"recent_bad_event": "yes", "volatility_risk": "nodata"})
    r8 = se.full_report(a8, kss.STAGES_CONFIG, "TESTCOIN3")
    mode = kss.determine_ai_analyst_mode(r8)
    check("O) breadth=1, veto yok, verdict!='Elenir' -> LIMITED", mode == "LIMITED",
          f"mode={mode} verdict={r8['verdict_title']}")

    v_title_d, _, v_desc_d = se.verdict(90, 100, False, "Yüksek", 100.0, True, True)
    check("D) breadth=2/reliable=True -> 'yetersiz'/'eksik' ailesinde DEGIL",
          "yetersiz" not in v_title_d and "eksik" not in v_title_d, f"got={v_title_d}")
    mode_d = kss.determine_ai_analyst_mode({"vetos": [], "verdict_title": v_title_d, "risk_score": 100,
                                             "confidence": "Yüksek", "risk_coverage": 100.0, "risk_reliable": True})
    check("D) AI mode NORMAL (reliable=True, diger kosullar temiz)", mode_d == "NORMAL", f"got={mode_d}")

    print("\n" + "=" * 90)
    print("P/Q) DENOMINATOR CONTRACT (structurally-unavailable disi, nodata icinde)")
    print("=" * 90)
    check("P) risk_breadth_total==2 (recent_bad_event + volatility_risk)",
          report["risk_breadth_total"] == 2, f"got={report['risk_breadth_total']}")
    a9 = build_answers(kss, overrides={"recent_bad_event": "nodata", "volatility_risk": "nodata"})
    r9 = se.full_report(a9, kss.STAGES_CONFIG, "TESTCOIN4")
    check("Q) ikisi de nodata: breadth_total=2 (denominatorda), breadth=0 (unanswered)",
          r9["risk_breadth_total"] == 2 and r9["risk_breadth"] == 0,
          f"total={r9['risk_breadth_total']} breadth={r9['risk_breadth']}")

    print("\n" + "=" * 90)
    print("R/S) VERDICT METNI (legacy '%50' kalmadi) + HISTORY LEGACY SAFETY")
    print("=" * 90)
    check("R) AI_ANALYST_LIMITED_INSTRUCTION'da '%50' YOK", "%50" not in kss.AI_ANALYST_LIMITED_INSTRUCTION)
    check("R) AI_ANALYST_LIMITED_INSTRUCTION'da yeni 'bağımsız risk boyutu' semantigi VAR",
          "bağımsız risk boyutu" in kss.AI_ANALYST_LIMITED_INSTRUCTION)

    legacy_answers = build_answers(kss, overrides={"recent_bad_event": "yes"})
    legacy_answers = [a for a in legacy_answers
                      if kss._FACTOR_ID_BY_LABEL.get(a["question"]) != "volatility_risk"]
    legacy_reliable = kss.compute_risk_reliable(legacy_answers)
    check("S) legacy answers (breadth=1) icin compute_risk_reliable()==False",
          legacy_reliable is False, f"got={legacy_reliable}")
    legacy_fallback_cov = kss.fallback_risk_coverage(legacy_answers)
    check("S) fallback_risk_coverage() DEGISMEDI (denom=4, 1/4=25.0)",
          abs(legacy_fallback_cov - 25.0) < 0.01, f"got={legacy_fallback_cov}")

    print(f"\n{'=' * 70}\nSONUC: {len(PASS)} PASS, {len(FAIL)} FAIL\n{'=' * 70}")
    if FAIL:
        for name, detail in FAIL:
            print(f"  - {name}: {detail}")
        return 1
    print("TUM TESTLER GECTI.")
    return 0


def test_run():
    assert run() == 0


if __name__ == "__main__":
    sys.exit(run())
