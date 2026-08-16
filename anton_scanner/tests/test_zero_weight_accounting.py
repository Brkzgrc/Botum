# -*- coding: utf-8 -*-
"""
PERMANENT REGRESSION SUITE -- Zero-Weight Accounting Fix

Ne test ediyor: ScoreEngine.calculate_stage_score() zero-weight (volatility_risk,
weight=0) sorularin answered_count'a dogru sekilde dahil edildigini, risk_score'u
etkilemedigini, normalized/effective weight hesaplarini ve breadth/reliability'nin
bundan bagimsiz oldugunu.

Network GEREKTIRMEZ, DB'ye dokunmaz, tamamen deterministic (ScoreEngine dogrudan
cagirilir).

Kaynak / provenance: scratchpad/zero_weight_accounting_fix_tests.py -- guncel V6
production'a karsi tekrar dogrulanarak (22/22 PASS) buraya tasindi, kor kopya degil.
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


ALWAYS_NODATA = {"clear_support", "risk_reward_ratio", "token_unlock_risk",
                  "dxy_trend", "etf_inflow"}


def build_answers(stages_config, factor_map, rbe="yes", vol="yes"):
    answers = []
    for stage in stages_config:
        for it in stage.items:
            fid = factor_map.get(it.label, "?")
            if fid == "recent_bad_event":
                ans = rbe
            elif fid == "volatility_risk":
                ans = vol
            elif fid in ALWAYS_NODATA or fid in ("whale_accumulation", "stablecoin_inflow",
                                                  "mvrv_ratio", "sopr_recovery"):
                ans = "nodata"
            else:
                ans = "yes"
            answers.append({"stage": stage.title, "question": it.label, "answer": ans,
                             "weight": it.weight, "_item": it, "disabled": False})
    return answers


def run():
    se = kss.ScoreEngine(kss.SIGNAL_STAGE_WEIGHTS, kss.RISK_STAGE_WEIGHTS, kss.VetoEngine(kss.VETO_RULES))

    print("=" * 90)
    print("1) calculate_stage_score() direkt unit testleri (A-D, G)")
    print("=" * 90)
    score, count, wtotal = se.calculate_stage_score(["yes"], [type("I", (), {"weight": 0})()])
    check("A) zero-weight yes: answered_count=1 (preserved)", count == 1, f"got={count}")
    check("A) zero-weight yes: score=0.0", score == 0.0, f"got={score}")

    score, count, wtotal = se.calculate_stage_score(["wait"], [type("I", (), {"weight": 0})()])
    check("B) zero-weight wait: answered_count=1", count == 1, f"got={count}")

    score, count, wtotal = se.calculate_stage_score(["no"], [type("I", (), {"weight": 0})()])
    check("C) zero-weight no: answered_count=1", count == 1, f"got={count}")

    score, count, wtotal = se.calculate_stage_score(["nodata"], [type("I", (), {"weight": 0})()])
    check("D) zero-weight nodata: answered_count=0", count == 0, f"got={count}")

    score, count, wtotal = se.calculate_stage_score(["yes"], [type("I", (), {"weight": 10})()])
    check("E) positive-weight yes: answered_count=1, score=100.0", count == 1 and score == 100.0,
          f"count={count} score={score}")

    score, count, wtotal = se.calculate_stage_score(
        ["yes", "yes"], [type("I", (), {"weight": 0})(), type("I", (), {"weight": 10})()])
    check("F) mixed weight0+positive: answered_count=2, score=100.0 (weight0 skoru etkilemiyor)",
          count == 2 and score == 100.0, f"count={count} score={score}")

    score, count, wtotal = se.calculate_stage_score(["nodata", "nodata"],
                                                      [type("I", (), {"weight": 0})(), type("I", (), {"weight": 10})()])
    check("G) all nodata: answered_count=0, score=0.0", count == 0 and score == 0.0, f"count={count} score={score}")

    print("\n" + "=" * 90)
    print("2) RISK_SCORE INVARIANT -- recent_bad_event=nodata + volatility_risk=yes/wait/no/nodata")
    print("=" * 90)
    scores = {}
    for vol in ("yes", "wait", "no", "nodata"):
        a = build_answers(kss.STAGES_CONFIG, kss._FACTOR_ID_BY_LABEL, rbe="nodata", vol=vol)
        r = se.full_report(a, kss.STAGES_CONFIG, "TESTCOIN")
        scores[vol] = (r["risk_score"], r["stage_answered"].get("Risk & pozisyon yönetimi"))
    check("H) risk_score AYNI (yes/wait/no'da) volatility_risk state'inden bagimsiz",
          len(set(s[0] for s in scores.values() if s[0] is not None)) <= 1)
    check("H) yes/wait/no: risk_score=0.0 (weight=0 tek faktor -> stage_score=0.0)",
          scores["yes"][0] == 0.0 and scores["wait"][0] == 0.0 and scores["no"][0] == 0.0, f"{scores}")
    check("H) nodata: risk_score=None (risk_available=False)", scores["nodata"][0] is None, f"{scores}")

    print("\n" + "=" * 90)
    print("3) stage_answered FIX + BREADTH/RELIABILITY INVARIANT")
    print("=" * 90)
    check("I) stage_answered['Risk & pozisyon yönetimi']==1 (zero-weight yes de sayiliyor)",
          scores["yes"][1] == 1, f"got={scores['yes'][1]}")

    a_full = build_answers(kss.STAGES_CONFIG, kss._FACTOR_ID_BY_LABEL, rbe="nodata", vol="yes")
    r_full = se.full_report(a_full, kss.STAGES_CONFIG, "TESTCOIN")
    check("J) risk_breadth==1 (fix'ten etkilenmedi, ayri yoldan hesaplaniyor)",
          r_full["risk_breadth"] == 1, f"got={r_full['risk_breadth']}")
    check("J) risk_breadth_total==2", r_full["risk_breadth_total"] == 2, f"got={r_full['risk_breadth_total']}")
    check("J) risk_reliable==False", r_full["risk_reliable"] is False)
    check("J) risk_coverage==50.0", r_full["risk_coverage"] == 50.0, f"got={r_full['risk_coverage']}")

    print("\n" + "=" * 90)
    print("4) NORMALIZED / EFFECTIVE WEIGHTS")
    print("=" * 90)
    check("K) normalized_weights['Risk & pozisyon yönetimi'] DOLU",
          "Risk & pozisyon yönetimi" in r_full["normalized_weights"],
          f"keys={list(r_full['normalized_weights'].keys())}")
    check("K) normalized_weights['Risk...']==100.0",
          r_full["normalized_weights"].get("Risk & pozisyon yönetimi") == 100.0,
          f"got={r_full['normalized_weights'].get('Risk & pozisyon yönetimi')}")
    check("L) effective_weights['Risk...']==3.0 (weight=15 * coverage=1/5=0.2)",
          abs(r_full["effective_weights"].get("Risk & pozisyon yönetimi", -1) - 3.0) < 0.01,
          f"got={r_full['effective_weights'].get('Risk & pozisyon yönetimi')}")
    check("L) risk_score numeric deger degismedi (0.0)", r_full["risk_score"] == 0.0, f"got={r_full['risk_score']}")

    print("\n" + "=" * 90)
    print("5) OTHER STAGES -- Signal stage'lerde weight=0 QuestionItem var mi?")
    print("=" * 90)
    zero_weight_items = [(s.title, it.label) for s in kss.STAGES_CONFIG for it in s.items if it.weight == 0]
    check("M) yalniz Risk stage'de volatility_risk weight=0 (baska stage'de yok)",
          len(zero_weight_items) == 1 and zero_weight_items[0][0] == "Risk & pozisyon yönetimi",
          f"{zero_weight_items}")

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
