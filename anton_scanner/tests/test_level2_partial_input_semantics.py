# -*- coding: utf-8 -*-
"""
PERMANENT REGRESSION SUITE -- Level 2 bool_or_min Partial-Input Semantics Fix

Ne test ediyor: `run_level2()`'nin "Günlük volatilite > %3 veya Bollinger
squeeze aktif" (bool_or_min) sorusundaki squeeze-unknown durumunu artık
`False/0` VARSAYMADIĞINI, bunun yerine gerçek `ThresholdEngine.evaluate()`
formülünü (`vol>=yes_vol -> yes; vol>=wait_vol OR squeeze>=1 -> wait; aksi
-> no`) esas alan üç-bantlı bir OR mantığıyla ele aldığını: vol tek başına
zaten yes/wait eşiğini geçiyorsa squeeze bilinmeden de sonuç kesinleşir
(squeeze etkisiz kalır -- OR zaten sağlanmış); yalnız vol HER İKİ eşiğin de
altındaysa (gerçek belirsizlik) `nodata` üretilir.

Bu test dosyası `run_level2()`'nin TAM UI/Qt akışını değil, üretim kodundaki
AYNI dallanma mantığını (satır satır ayrıştırılmış, gerçek `ThresholdEngine`
ve gerçek `item.thresholds` ile) izole şekilde çalıştırır -- Qt widget
kurulumu gerektirmez, tamamen deterministic/network-free.

Kaynak / provenance: "Level 2 / Level 3 Input & Decision Integrity Audit"
turunda bulunan Finding 1 (squeeze-unknown -> sessizce False varsayımı) bu
turda düzeltildi. Kullanıcının ilk önerdiği "squeeze bilinmiyorsa vol tek
başına yes değilse hep nodata" contract'ı REDDEDİLDİ -- gerçek
ThresholdEngine formülü incelendiğinde squeeze'in YALNIZ "wait" seviyesine
kadar etki edebildiği (asla tek başına "yes" üretemediği) kanıtlandı; bu
yüzden "vol wait_vol eşiğini de geçiyorsa squeeze zaten etkisiz" ek bandı
eklendi -- kullanıcının 2-bantlı taslağından DAHA DAR bir nodata alanı.
"""
import importlib.util
import os
import sys

import pytest

_PROD_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "kripto_sinyal_sistemi_v6_pro_gui.pyw")
spec = importlib.util.spec_from_file_location('kss', _PROD_FILE)
kss = importlib.util.module_from_spec(spec)
sys.modules['kss'] = kss
spec.loader.exec_module(kss)

TE = kss.ThresholdEngine


@pytest.fixture
def item():
    return next(it for stage in kss.STAGES_CONFIG for it in stage.items
                if (it.thresholds or {}).get("type") == "bool_or_min")


def _run_level2_bool_or_min_logic(item, vol_val, squeeze_val):
    """run_level2()'deki GERCEK dallanma mantığının izole kopyası (satır
    satır ayni, yalniz Qt widget okumasi yerine dogrudan parametre alıyor)."""
    if vol_val is None and squeeze_val is None:
        return "nodata"
    elif vol_val is None:
        return "wait" if squeeze_val >= 1 else "no"
    elif squeeze_val is None:
        yes_vol = item.thresholds.get("yes_vol", 999)
        wait_vol = item.thresholds.get("wait_vol", 0)
        if vol_val >= yes_vol:
            return "yes"
        elif vol_val >= wait_vol:
            return "wait"
        else:
            return "nodata"
    else:
        return TE.evaluate(item, [vol_val, squeeze_val])


def test_current_thresholds_are_yes_3_wait_1_5(item):
    """Contract'ın dayandığı gerçek üretim eşiklerini doğrula -- eski rapor
    yerine güncel koddan."""
    assert item.thresholds.get("yes_vol") == 3.0
    assert item.thresholds.get("wait_vol") == 1.5


def test_evaluate_squeeze_alone_never_produces_yes(item):
    """KRİTİK doğrulama: kullanıcının ilk taslağındaki 'squeeze=True tek
    başına yes üretir' varsayımı YANLIŞ -- gerçek ThresholdEngine.evaluate()
    formülünde squeeze yalnız 'wait' seviyesine kadar çıkarabiliyor, asla
    'yes' üretemiyor (yalnız vol>=yes_vol 'yes' üretir)."""
    result = TE.evaluate(item, [0.0, 1.0])  # vol=0 (cok dusuk), squeeze=True
    assert result == "wait", f"squeeze tek basina asla 'yes' uretmemeli, got={result}"


# ---------- Truth table: tam bilgi (before/after PARITY -- degismemeli) ----------

@pytest.mark.parametrize("vol,squeeze,expected", [
    (5.0, 1.0, "yes"),   # vol tek basina yes
    (5.0, 0.0, "yes"),   # vol tek basina yes, squeeze ilgisiz
    (2.0, 1.0, "wait"),  # vol wait bandinda VEYA squeeze true -> wait
    (2.0, 0.0, "wait"),  # vol tek basina wait bandinda
    (0.5, 1.0, "wait"),  # vol dusuk ama squeeze true -> wait
    (0.5, 0.0, "no"),    # ikisi de yetersiz -> no
])
def test_full_data_parity_unchanged(item, vol, squeeze, expected):
    """Tam veri (vol VE squeeze biliniyor) durumunda sonuc bu fix'ten
    ETKILENMEMELI -- dogrudan ThresholdEngine.evaluate() cagriliyor, hic
    degismedi."""
    result = _run_level2_bool_or_min_logic(item, vol, squeeze)
    assert result == expected, f"vol={vol} squeeze={squeeze}: got={result} expected={expected}"


# ---------- Truth table: partial input (squeeze unknown -- FIX burada) ----------

@pytest.mark.parametrize("vol,expected,reason", [
    (5.0, "yes", "vol tek basina yes_vol(3.0) esigini gecmis, squeeze bilinmese de sonuc kesin"),
    (3.0, "yes", "tam yes_vol siniri, squeeze bilinmese de yes"),
    (2.0, "wait", "vol wait_vol(1.5) esigini gecmis (yes_vol'u degil), OR zaten saglanmis"),
    (1.5, "wait", "tam wait_vol siniri, squeeze bilinmese de wait"),
    (1.0, "nodata", "vol HER IKI esigin de altinda -- squeeze bilinmeden karar VERILEMEZ"),
    (0.0, "nodata", "vol sifir, gercek belirsizlik"),
])
def test_squeeze_unknown_three_tier_semantics(item, vol, expected, reason):
    result = _run_level2_bool_or_min_logic(item, vol, None)
    assert result == expected, f"vol={vol} squeeze=None: got={result} expected={expected} ({reason})"


def test_squeeze_unknown_never_silently_becomes_false(item):
    """REGRESYON GUARD: eski bug -- squeeze=None otomatik squeeze=0 (Hayir)
    varsayilip dogrudan evaluate(item,[vol,0]) cagriliyordu. Bu ARTIK
    dogru degil -- vol=1.0 (wait_vol=1.5 altinda) icin eski kod 'no'
    uretirdi (evaluate([1.0,0])='no'), yeni kod 'nodata' uretmeli."""
    old_buggy_result = TE.evaluate(item, [1.0, 0])  # eski (kaldirilan) davranisin simulasyonu
    new_result = _run_level2_bool_or_min_logic(item, 1.0, None)
    assert old_buggy_result == "no", f"eski simulasyon 'no' vermeliydi, got={old_buggy_result}"
    assert new_result == "nodata", f"yeni davranis 'nodata' olmali, got={new_result}"
    assert new_result != old_buggy_result, "fix gercekten davranisi degistirmis olmali"


# ---------- Numeric boundary (epsilon) ----------

@pytest.mark.parametrize("vol,expected", [
    (2.999, "wait"),   # yes_vol - epsilon
    (3.0, "yes"),       # exact yes_vol
    (3.001, "yes"),      # yes_vol + epsilon
    (1.499, "nodata"),  # wait_vol - epsilon
    (1.5, "wait"),        # exact wait_vol
    (1.501, "wait"),       # wait_vol + epsilon
])
def test_boundary_epsilon_squeeze_unknown(item, vol, expected):
    result = _run_level2_bool_or_min_logic(item, vol, None)
    assert result == expected, f"vol={vol} (boundary): got={result} expected={expected}"


# ---------- vol missing (DEĞİŞTİRİLMEDİ -- mevcut davranış korunuyor) ----------

@pytest.mark.parametrize("squeeze,expected", [
    (1.0, "wait"),
    (0.0, "no"),
])
def test_vol_missing_behavior_unchanged(item, squeeze, expected):
    """Bu tur YALNIZ squeeze-unknown dalini duzeltti -- vol-missing dali
    (DOKUNMA kapsaminda, degismedi) mevcut davranisiyla aynen kalmali."""
    result = _run_level2_bool_or_min_logic(item, None, squeeze)
    assert result == expected


def test_both_missing_still_nodata(item):
    assert _run_level2_bool_or_min_logic(item, None, None) == "nodata"


# ---------- Downstream: full_report() uzerinden gercek skor/coverage etkisi ----------

def _build_answers_with_volatility_answer(vol_answer):
    ALWAYS_NODATA = {"clear_support", "risk_reward_ratio", "token_unlock_risk", "dxy_trend",
                      "etf_inflow", "mvrv_ratio", "sopr_recovery", "whale_accumulation",
                      "stablecoin_inflow", "recent_bad_event"}
    answers = []
    for stage in kss.STAGES_CONFIG:
        for it in stage.items:
            th = it.thresholds or {}
            if th.get("type") == "bool_or_min":
                ans = vol_answer
            else:
                fid = kss._FACTOR_ID_BY_LABEL.get(it.label, "?")
                ans = "nodata" if fid in ALWAYS_NODATA else "yes"
            answers.append({"stage": stage.title, "question": it.label, "answer": ans,
                            "weight": it.weight, "_item": it, "disabled": False})
    return answers


def test_downstream_impact_no_to_nodata_transition():
    """BEFORE (eski bug): squeeze unknown + vol=1.0 -> 'no' (yanlış-kesin).
    AFTER (fix): ayni girdi -> 'nodata'. Bu degisimin signal_score/coverage/
    confidence/verdict uzerindeki GERCEK etkisini olc, varsaymadan."""
    se = kss.ScoreEngine(kss.SIGNAL_STAGE_WEIGHTS, kss.RISK_STAGE_WEIGHTS, kss.VetoEngine(kss.VETO_RULES))

    answers_before = _build_answers_with_volatility_answer("no")  # eski buggy sonucun simulasyonu
    answers_after = _build_answers_with_volatility_answer("nodata")  # yeni dogru sonuc

    report_before = se.full_report(answers_before, kss.STAGES_CONFIG, "TESTCOIN")
    report_after = se.full_report(answers_after, kss.STAGES_CONFIG, "TESTCOIN")

    print(f"\n  BEFORE (no):    signal_score={report_before['signal_score']} "
          f"coverage={report_before['coverage']} confidence={report_before['confidence']} "
          f"verdict={report_before['verdict_title']}")
    print(f"  AFTER (nodata): signal_score={report_after['signal_score']} "
          f"coverage={report_after['coverage']} confidence={report_after['confidence']} "
          f"verdict={report_after['verdict_title']}")

    # Yalniz olcuyoruz, sonucu onceden varsaymiyoruz -- ama en azindan crash
    # olmadigini ve rapor uretildigini dogruluyoruz.
    assert report_before is not None and report_after is not None
    assert report_before["coverage"] != report_after["coverage"] or \
           report_before["signal_score"] != report_after["signal_score"], \
        "en az bir metrik degismis olmali (nodata->no gecisi olcum yapilabilir bir fark yaratmali)"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "-s"]))
