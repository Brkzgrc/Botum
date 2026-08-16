# -*- coding: utf-8 -*-
"""
PERMANENT REGRESSION SUITE -- Entry Timing V2 (3-factor contract, R:R excluded)

Ne test ediyor: entry_timing()'in 3-faktor contract'i (ema50_distance,
price_change_24h, rsi_zone -- R:R skora dahil degil), price_change_24h'nin
abs() ile simetrik degerlendirilmesi (RealFetcher ve MockFetcher call-site'lari
tutarli), missing-data coverage/label davranisi, ve entry_timing'in skor
motorunun geri kalanindan (signal/risk/veto/verdict) izole oldugu.

Network GEREKTIRMEZ, DB'ye dokunmaz, tamamen deterministic.

Kaynak / provenance: scratchpad/test_entry_timing_v2.py -- guncel V6 production'a
karsi tekrar dogrulanarak (54/54 PASS) buraya tasindi, granuler pytest
fonksiyonlarina bolunerek.
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

Q_EMA = "Fiyat EMA50'den makul uzaklıkta (<%5)"
Q_CHG = "Son 24 saat fiyat değişimi sert değil (<%10)"
Q_RSI = "RSI (14) 30-50 aralığında veya 30'dan dönüş yapıyor"
Q_RR = "Yakın direnç hedefi net ve en az %10 yukarıda (R:R ≥ 1:2)"


def _se():
    return kss.ScoreEngine(kss.SIGNAL_STAGE_WEIGHTS, kss.RISK_STAGE_WEIGHTS, kss.VetoEngine(kss.VETO_RULES))


def _answers(ema="yes", chg="yes", rsi="yes", rr=None):
    a = [{"question": Q_EMA, "answer": ema}, {"question": Q_CHG, "answer": chg},
         {"question": Q_RSI, "answer": rsi}]
    if rr is not None:
        a.append({"question": Q_RR, "answer": rr})
    return a


def test_entry_timing_total_factors_is_3():
    se = _se()
    s, l, d, ans_cnt, tot = se.entry_timing(_answers())
    assert tot == 3, tot
    assert ans_cnt == 3, ans_cnt


def test_entry_timing_rr_excluded_from_score():
    se = _se()
    s_no_rr, *_ = se.entry_timing(_answers(rr=None))
    s_rr_yes, _, _, ans_rr_yes, tot_rr_yes = se.entry_timing(_answers(rr="yes"))
    s_rr_no, *_ = se.entry_timing(_answers(rr="no"))
    s_rr_wait, *_ = se.entry_timing(_answers(rr="wait"))
    assert s_rr_yes == s_no_rr
    assert s_rr_no == s_no_rr
    assert s_rr_wait == s_no_rr
    assert ans_rr_yes == 3, ans_rr_yes
    assert tot_rr_yes == 3, tot_rr_yes


def test_entry_timing_labels_and_thresholds():
    se = _se()
    s85, l85, *_ = se.entry_timing(_answers("yes", "yes", "yes"))
    assert s85 == 100.0 and l85 == "Uygun", (s85, l85)

    s_wait_all, l_wait_all, *_ = se.entry_timing(_answers("wait", "wait", "wait"))
    assert l_wait_all in ("Erken Olabilir", "Retest Bekle"), (s_wait_all, l_wait_all)

    s_no_all, l_no_all, *_ = se.entry_timing(_answers("no", "no", "no"))
    assert s_no_all == 0.0 and l_no_all == "Geç Kalınmış", (s_no_all, l_no_all)


def test_entry_timing_missing_data_coverage():
    se = _se()
    s2, l2, d2, ans2, tot2 = se.entry_timing(_answers(ema="nodata"))
    assert ans2 == 2, ans2
    assert s2 is not None, s2
    assert "eksik veri uyarısı" in d2, d2

    s1, l1, d1, ans1, tot1 = se.entry_timing(_answers(ema="nodata", chg="nodata"))
    assert s1 is None and l1 == "Hesaplanamadı", (s1, l1)
    assert ans1 == 1, ans1

    s0, l0, *_ = se.entry_timing(_answers(ema="nodata", chg="nodata", rsi="nodata"))
    assert s0 is None and l0 == "Hesaplanamadı", (s0, l0)


def test_entry_timing_rr_note_always_present():
    se = _se()
    _, _, d_full, *_ = se.entry_timing(_answers())
    assert "R:R" in d_full and "dahil değildir" in d_full, d_full
    _, _, d_partial, *_ = se.entry_timing(_answers(ema="nodata"))
    assert "R:R" in d_partial and "dahil değildir" in d_partial, d_partial


def _chg_status(chg):
    """RealFetcher.fetch icindeki AYNI mantik: ThresholdEngine.eval_max(abs(chg), ...)"""
    return kss.ThresholdEngine.eval_max(abs(chg), {"yes": 5.0, "wait": 10.0})


def test_entry_timing_chg24_abs_symmetry():
    for neg, pos in [(-15, 15), (-10, 10), (-7, 7), (-5, 5), (-3, 3), (0, 0)]:
        assert _chg_status(neg) == _chg_status(pos), (neg, pos, _chg_status(neg), _chg_status(pos))


def test_entry_timing_chg24_boundary_values():
    expected = {-15: "no", -10: "wait", -7: "wait", -5: "wait", -3: "yes", 0: "yes",
                3: "yes", 5: "wait", 7: "wait", 10: "wait", 15: "no"}
    for chg, exp in expected.items():
        assert _chg_status(chg) == exp, (chg, _chg_status(chg), exp)


def test_entry_timing_mockfetcher_realfetcher_parity():
    mf = kss.MockFetcher(seed=42)
    for chg_val in (-15, -10, -7, -5, -3, 0, 3, 5, 7, 10, 15):
        s_real = _chg_status(chg_val)
        s_mock = mf._auto_status("price_change_24h_pct", chg_val, {})
        assert s_real == s_mock, (chg_val, s_real, s_mock)


def _make_full_answers(chg_answer_override=None, rr_answer_override=None):
    out = []
    for stage in kss.STAGES_CONFIG:
        for item in stage.items:
            ans = "yes"
            if item.label == Q_CHG and chg_answer_override is not None:
                ans = chg_answer_override
            if item.label == Q_RR and rr_answer_override is not None:
                ans = rr_answer_override
            out.append({"stage": stage.title, "question": item.label, "answer": ans,
                        "weight": item.weight, "_item": item, "value": None, "source": "test",
                        "reason": "", "disabled": False})
    return out


def test_entry_timing_core_isolation_from_rr():
    se = _se()
    a1 = _make_full_answers(rr_answer_override="nodata")
    a2 = _make_full_answers(rr_answer_override="yes")
    r1 = se.full_report(a1, kss.STAGES_CONFIG, "TESTCOIN")
    r2 = se.full_report(a2, kss.STAGES_CONFIG, "TESTCOIN")

    assert r1["signal_score"] == r2["signal_score"]
    assert r1["risk_score"] == r2["risk_score"]
    assert r1["vetos"] == r2["vetos"]
    assert r1["verdict_title"] == r2["verdict_title"]
    assert r1["entry_status"] == r2["entry_status"]
    assert r1["entry_timing_score"] == r2["entry_timing_score"]
    assert r1["entry_timing_total"] == 3, r1["entry_timing_total"]


def test_entry_timing_chg24_change_actually_moves_score():
    se = _se()
    a1 = _make_full_answers(rr_answer_override="nodata")
    r1 = se.full_report(a1, kss.STAGES_CONFIG, "TESTCOIN")
    a3 = _make_full_answers(chg_answer_override="no", rr_answer_override="nodata")
    r3 = se.full_report(a3, kss.STAGES_CONFIG, "TESTCOIN")
    assert r3["entry_timing_score"] != r1["entry_timing_score"], \
        (r3["entry_timing_score"], r1["entry_timing_score"])


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
