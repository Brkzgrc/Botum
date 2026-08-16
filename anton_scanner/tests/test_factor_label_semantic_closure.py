# -*- coding: utf-8 -*-
"""
PERMANENT REGRESSION SUITE -- RSI + MACD Label-Only Semantic Closure

Ne test ediyor: RSI ve MACD QuestionItem label'larinin, production'in
GERCEKTEN hesapladigindan daha FAZLA vaat eden eski metinden ("...veya
30'dan donus yapiyor", "...veya boga kesisimi") gercek contract'i tarif
eden yeni metne ("RSI (14) 30-50 araliginda", "MACD histogrami pozitif ve
gucleniyor") gecisinin SAF LABEL-ONLY oldugunu: hesaplama motoru
(ThresholdEngine range/bool tipleri, RealFetcher.fetch() 'rsi'/'macd_bullish'
dallari, entry_timing() RSI faktoru) HICBIR SEKILDE degismedi; eski label
metnini tasiyan gecmis History kayitlari `_FACTOR_ID_BY_LABEL` uzerinden
hala dogru factor_id'ye (rsi_zone / macd_signal) cozumleniyor (legacy
alias, silinmedi); yeni label de ayni factor_id'ye cozumleniyor; ucuncu
bagimsiz bir substring-matching mekanizmasi olan entry_timing()'in RSI
eslesmesi ("RSI" in question) hem eski hem yeni metinle calismaya devam
ediyor; skor/veto/verdict tam parity (bit-for-bit) korunuyor.

Kaynak / provenance: "RSI + MACD Label-Only Semantic Closure -- Controlled
Implementation" turu. Onceki (audit-only) turde RSI/MACD icin "davranis
degisikligi" (momentum/crossover/history primitive) ACIKCA REDDEDILDI --
sorunun kok nedeni hesaplama motorunun degil, label'in mevcut davranistan
fazlasini vaat etmesi oldugu icin bu tur SADECE label metnini kapatiyor.
"""
import dataclasses
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

RSI_LABEL_NEW = "RSI (14) 30-50 aralığında"
RSI_LABEL_LEGACY = "RSI (14) 30-50 aralığında veya 30'dan dönüş yapıyor"
MACD_LABEL_NEW = "MACD histogramı pozitif ve güçleniyor"
MACD_LABEL_LEGACY = "MACD histogram yeşile dönüyor veya boğa kesişimi"

ALWAYS_NODATA = {"clear_support", "risk_reward_ratio", "token_unlock_risk", "dxy_trend",
                  "etf_inflow", "mvrv_ratio", "sopr_recovery", "whale_accumulation",
                  "stablecoin_inflow", "recent_bad_event"}


# ---------- 1) STAGES_CONFIG artik yeni metni kullaniyor ----------

def test_stages_config_rsi_item_has_new_label():
    it = next(it for stage in kss.STAGES_CONFIG for it in stage.items
              if kss._FACTOR_ID_BY_LABEL.get(it.label) == "rsi_zone")
    assert it.label == RSI_LABEL_NEW


def test_stages_config_macd_item_has_new_label():
    it = next(it for stage in kss.STAGES_CONFIG for it in stage.items
              if kss._FACTOR_ID_BY_LABEL.get(it.label) == "macd_signal")
    assert it.label == MACD_LABEL_NEW


# ---------- 2) _FACTOR_ID_BY_LABEL: yeni VE eski (legacy alias) cozumleniyor ----------

@pytest.mark.parametrize("label,expected_fid", [
    (RSI_LABEL_NEW, "rsi_zone"),
    (RSI_LABEL_LEGACY, "rsi_zone"),
    (MACD_LABEL_NEW, "macd_signal"),
    (MACD_LABEL_LEGACY, "macd_signal"),
])
def test_factor_id_by_label_resolves_new_and_legacy(label, expected_fid):
    assert kss._FACTOR_ID_BY_LABEL.get(label) == expected_fid


def test_legacy_labels_not_deleted_from_mapping():
    """Eski label'lar dict'te hala BIREBIR anahtar olarak duruyor -- bu,
    History kayitlarinin `answers[].question` alaninin eski metni sonsuza
    kadar tasiyacagi gercegiyle uyumlulugu garanti eder."""
    assert RSI_LABEL_LEGACY in kss._FACTOR_ID_BY_LABEL
    assert MACD_LABEL_LEGACY in kss._FACTOR_ID_BY_LABEL


# ---------- 3) Registry parity korunuyor (30/30/30) ----------

def test_registry_parity_unaffected_by_label_change():
    stages_ids = {kss._FACTOR_ID_BY_LABEL.get(it.label)
                  for stage in kss.STAGES_CONFIG for it in stage.items}
    assert None not in stages_ids
    assert len(stages_ids) == 30
    assert stages_ids == set(kss.FACTOR_ID_TABLE.keys())
    assert stages_ids == set(kss.SEMANTIC_ROLE_BY_FACTOR_ID.keys())


# ---------- 4) Level 1 routing (_metric_for_label): yeni VE eski metin ----------

@pytest.mark.parametrize("label,expected_metric", [
    (RSI_LABEL_NEW, "rsi"),
    (RSI_LABEL_LEGACY, "rsi"),
    (MACD_LABEL_NEW, "macd_bullish"),
    (MACD_LABEL_LEGACY, "macd_bullish"),
])
def test_metric_for_label_routing_unchanged(label, expected_metric):
    assert kss._metric_for_label(label) == expected_metric


# ---------- 5) entry_timing()'in bagimsiz substring-matching'i (ucuncu mekanizma) ----------

def test_entry_timing_rsi_match_works_with_new_label():
    """entry_timing() kendi ayri 'RSI' in question substring kontrolunu
    kullanir (_FACTOR_ID_BY_LABEL'e bagli DEGIL) -- yeni label ile de
    dogru sekilde eslesip skorlamaya katilmali."""
    se = kss.ScoreEngine(kss.SIGNAL_STAGE_WEIGHTS, kss.RISK_STAGE_WEIGHTS, kss.VetoEngine(kss.VETO_RULES))
    answers = [
        {"question": "EMA50'den makul uzaklık", "answer": "yes"},
        {"question": "Son 24 saat fiyat değişimi", "answer": "yes"},
        {"question": RSI_LABEL_NEW, "answer": "yes"},
    ]
    score, verdict, desc, answered, total = se.entry_timing(answers)
    assert answered == 3
    assert score == 100.0


def test_entry_timing_rsi_match_still_works_with_legacy_label():
    se = kss.ScoreEngine(kss.SIGNAL_STAGE_WEIGHTS, kss.RISK_STAGE_WEIGHTS, kss.VetoEngine(kss.VETO_RULES))
    answers = [
        {"question": "EMA50'den makul uzaklık", "answer": "yes"},
        {"question": "Son 24 saat fiyat değişimi", "answer": "yes"},
        {"question": RSI_LABEL_LEGACY, "answer": "yes"},
    ]
    score, verdict, desc, answered, total = se.entry_timing(answers)
    assert answered == 3
    assert score == 100.0


# ---------- 6) Level 2 threshold davranisi degismedi (RSI range, MACD bool) ----------

def test_rsi_range_thresholds_unchanged():
    it = next(it for stage in kss.STAGES_CONFIG for it in stage.items
              if kss._FACTOR_ID_BY_LABEL.get(it.label) == "rsi_zone")
    th = it.thresholds
    assert th.get("type") == "range"
    assert th.get("yes_min") == 30
    assert th.get("yes_max") == 50
    assert th.get("wait_min") == 20
    assert th.get("wait_max") == 70


@pytest.mark.parametrize("rsi_val,expected", [
    (19.999, "no"),
    (20.0, "wait"),
    (29.999, "wait"),
    (30.0, "yes"),
    (50.0, "yes"),
    (50.001, "wait"),
    (70.0, "wait"),
    (70.001, "no"),
])
def test_rsi_boundary_behavior_unchanged(rsi_val, expected):
    it = next(it for stage in kss.STAGES_CONFIG for it in stage.items
              if kss._FACTOR_ID_BY_LABEL.get(it.label) == "rsi_zone")
    result = kss.ThresholdEngine.evaluate(it, [rsi_val])
    assert result == expected, f"rsi={rsi_val}: got={result} expected={expected}"


def test_macd_bool_widget_type_unchanged():
    it = next(it for stage in kss.STAGES_CONFIG for it in stage.items
              if kss._FACTOR_ID_BY_LABEL.get(it.label) == "macd_signal")
    assert it.thresholds.get("type") == "bool"
    assert it.weight == 8


def test_rsi_weight_unchanged():
    it = next(it for stage in kss.STAGES_CONFIG for it in stage.items
              if kss._FACTOR_ID_BY_LABEL.get(it.label) == "rsi_zone")
    assert it.weight == 7


# ---------- 7) History backward compatibility: AI context uzerinden ----------

def _build_answers_with_rsi_label(rsi_label, rsi_answer="wait"):
    answers = []
    for stage in kss.STAGES_CONFIG:
        for it in stage.items:
            fid = kss._FACTOR_ID_BY_LABEL.get(it.label, "?")
            question = it.label
            if fid == "rsi_zone":
                question = rsi_label
                ans = rsi_answer
            elif fid in ALWAYS_NODATA:
                ans = "nodata"
            else:
                ans = "yes"
            answers.append({"stage": stage.title, "question": question, "answer": ans,
                             "weight": it.weight, "_item": it, "disabled": False})
    return answers


def test_ai_context_resolves_factor_id_from_legacy_history_label():
    """Eski bir History kaydinin `answers[].question` alani hala eski
    label metnini tasiyor olabilir -- build_ai_analyst_context()'in bunu
    hala rsi_zone factor_id'sine cozumledigini dogrula."""
    se = kss.ScoreEngine(kss.SIGNAL_STAGE_WEIGHTS, kss.RISK_STAGE_WEIGHTS, kss.VetoEngine(kss.VETO_RULES))
    answers = _build_answers_with_rsi_label(RSI_LABEL_LEGACY, rsi_answer="wait")
    report = se.full_report(answers, kss.STAGES_CONFIG, "TESTCOIN")
    ctx = kss.build_ai_analyst_context(report, "TESTCOIN")
    all_factors = ctx["usable_factors"] + ctx["unavailable_factors"]
    rsi_entry = next(f for f in all_factors if f["factor_id"] == "rsi_zone")
    assert rsi_entry["status"] == "wait"


def test_ai_context_resolves_factor_id_from_new_label():
    se = kss.ScoreEngine(kss.SIGNAL_STAGE_WEIGHTS, kss.RISK_STAGE_WEIGHTS, kss.VetoEngine(kss.VETO_RULES))
    answers = _build_answers_with_rsi_label(RSI_LABEL_NEW, rsi_answer="wait")
    report = se.full_report(answers, kss.STAGES_CONFIG, "TESTCOIN")
    ctx = kss.build_ai_analyst_context(report, "TESTCOIN")
    all_factors = ctx["usable_factors"] + ctx["unavailable_factors"]
    rsi_entry = next(f for f in all_factors if f["factor_id"] == "rsi_zone")
    assert rsi_entry["status"] == "wait"


# ---------- 8) Score/verdict parity: gercek "once/sonra" simulasyonu ----------
#
# ONEMLI KESIF (bu test dosyasi yazilirken bulundu): full_report()'un KENDI
# ic stage-scoring donguleri (satir ~5213, ~5239) `_FACTOR_ID_BY_LABEL`
# KULLANMIYOR -- answer'i item'a eslestirmek icin TAM (exact) string
# karsilastirma yapiyor: `a["question"] == item.label`. Bu, ucuncu (artik
# DORDUNCU) bagimsiz bir label-matching mekanizmasi. Ama production'da bu
# hicbir zaman sorun yaratmaz cunku answers HER ZAMAN o anki calisan kod
# tarafindan, o anki STAGES_CONFIG item.label'i kullanilarak taze
# olusturulur (Level1Worker/run_level2/run_level3 -- hepsi
# "question": item.label seklinde, ayni cagri icinde) -- STORED/History
# label metni full_report()'a asla dogrudan geri beslenmez (yalnizca
# build_ai_analyst_context() ve History detay gorunumu gibi ASIL History
# tuketicileri _FACTOR_ID_BY_LABEL uzerinden gecer, onlar yukarida ayrica
# test edildi). Bu yuzden asagidaki parity testi, eski label metnini
# `question` alanina KARISTIRMAZ (bu, hicbir zaman olmayan bir senaryoyu
# simule ederdi ve full_report()'un exact-match dogasi yuzunden yanlislikla
# RSI/MACD'yi 'nodata'ya dusurup YANLIS bir parity ihlali gibi gorunurdu --
# bu hata bizzat bu dosya yazilirken yakalanip duzeltildi). Bunun yerine
# GERCEK "once" durumunu, RSI/MACD QuestionItem'larinin label'i ESKI metne
# cevrilmis bir golge (shadow) STAGES_CONFIG kopyasiyla (thresholds/weight/
# pro/con AYNI, yalniz label eski) simule ediyoruz -- boylece "once" ve
# "sonra" her ikisi de KENDI ICINDE tutarli (self-consistent) kaliyor,
# tipki gercek production akisinda oldugu gibi.

def _build_shadow_stages_config_with_old_labels():
    """STAGES_CONFIG'in derin kopyasi, yalniz RSI/MACD item.label'i ESKI
    metne cevrilmis -- weight/thresholds/pro/con/desc AYNEN korunuyor."""
    new_stages = []
    for stage in kss.STAGES_CONFIG:
        new_items = []
        for it in stage.items:
            fid = kss._FACTOR_ID_BY_LABEL.get(it.label, "?")
            if fid == "rsi_zone":
                new_items.append(dataclasses.replace(it, label=RSI_LABEL_LEGACY))
            elif fid == "macd_signal":
                new_items.append(dataclasses.replace(it, label=MACD_LABEL_LEGACY))
            else:
                new_items.append(it)
        new_stages.append(dataclasses.replace(stage, items=new_items))
    return new_stages


def _build_full_answers(stages_config, rsi_answer="yes", macd_answer="yes"):
    answers = []
    for stage in stages_config:
        for it in stage.items:
            fid = kss._FACTOR_ID_BY_LABEL.get(it.label, "?")
            if fid == "rsi_zone":
                ans = rsi_answer
            elif fid == "macd_signal":
                ans = macd_answer
            elif fid in ALWAYS_NODATA:
                ans = "nodata"
            else:
                ans = "yes"
            answers.append({"stage": stage.title, "question": it.label, "answer": ans,
                             "weight": it.weight, "_item": it, "disabled": False})
    return answers


@pytest.mark.parametrize("rsi_answer,macd_answer", [
    ("yes", "yes"), ("wait", "wait"), ("no", "no"), ("nodata", "nodata"),
])
def test_score_parity_before_after_label_change(rsi_answer, macd_answer):
    """GERCEK once/sonra simulasyonu: 'once' = eski label'li golge config
    (self-consistent), 'sonra' = bugunku gercek STAGES_CONFIG (self-consistent).
    Ayni classified answer'lar icin signal_score/risk_score/coverage/
    confidence/verdict_title/vetos BIT-FOR-BIT AYNI kalmali -- label metninin
    KENDISI skor motoruna hicbir sekilde girmiyor, yalniz eslestirme icin
    kullaniliyor ve eslestirme her iki tarafta da kendi icinde tutarli."""
    se = kss.ScoreEngine(kss.SIGNAL_STAGE_WEIGHTS, kss.RISK_STAGE_WEIGHTS, kss.VetoEngine(kss.VETO_RULES))
    shadow_stages = _build_shadow_stages_config_with_old_labels()

    answers_before = _build_full_answers(shadow_stages, rsi_answer, macd_answer)
    answers_after = _build_full_answers(kss.STAGES_CONFIG, rsi_answer, macd_answer)

    report_before = se.full_report(answers_before, shadow_stages, "TESTCOIN")
    report_after = se.full_report(answers_after, kss.STAGES_CONFIG, "TESTCOIN")

    assert report_before["signal_score"] == report_after["signal_score"]
    assert report_before["risk_score"] == report_after["risk_score"]
    assert report_before["coverage"] == report_after["coverage"]
    assert report_before["confidence"] == report_after["confidence"]
    assert report_before["verdict_title"] == report_after["verdict_title"]
    assert report_before["vetos"] == report_after["vetos"]


def test_full_report_matching_is_exact_string_not_factor_id():
    """DOKUMANTASYON/REGRESYON GUARD: full_report()'un ic stage-scoring
    donguleri _FACTOR_ID_BY_LABEL DEGIL, tam (exact) `question == item.label`
    esitligi kullanir -- bu yuzden eski (legacy) label metni tasiyan bir
    answer, GUNCEL STAGES_CONFIG'e karsi full_report()'a dogrudan beslenirse
    (production'da hic olmayan bir senaryo) o factor icin sessizce 'nodata'
    davranisina duser. Bu test, bu mekanigin var oldugunu ve gelecekte
    _FACTOR_ID_BY_LABEL'e sessizce tasinmadigini kanitlar (davranis
    degisirse bu test FAIL eder, boylece boyle bir degisiklik BILEREK
    yapilir)."""
    se = kss.ScoreEngine(kss.SIGNAL_STAGE_WEIGHTS, kss.RISK_STAGE_WEIGHTS, kss.VetoEngine(kss.VETO_RULES))
    answers = _build_full_answers(kss.STAGES_CONFIG, rsi_answer="no", macd_answer="no")
    for a in answers:
        if kss._FACTOR_ID_BY_LABEL.get(a["question"]) == "rsi_zone":
            a["question"] = RSI_LABEL_LEGACY  # item.label ile artik eslesmiyor
    report = se.full_report(answers, kss.STAGES_CONFIG, "TESTCOIN")
    report_control = se.full_report(_build_full_answers(kss.STAGES_CONFIG, "no", "no"),
                                     kss.STAGES_CONFIG, "TESTCOIN")
    assert report["signal_score"] != report_control["signal_score"], (
        "beklenen: exact-match kirilinca RSI 'nodata'ya duser, skor degisir "
        "-- bu davranis DEGISIRSE (ornegin _FACTOR_ID_BY_LABEL'e tasinirsa) "
        "bu test bilerek guncellenmeli")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
