# -*- coding: utf-8 -*-
"""
PERMANENT REGRESSION SUITE -- AI Analyst Raw Context Integrity

Ne test ediyor: `build_ai_analyst_context()`'in GERÇEK production kontratını
kalıcılaştırıyor -- nodata/no/wait ayrımı, factor registry parity, disabled/
unknown factor fail-closed davranışı, decision_context alan seti, Model D
handoff, structure_context handoff (None dahil), mutation safety, determinism,
ve Level 2'nin PARKED (bilinçli, henüz düzeltilmemiş) `display_value=None`
davranışının bir SNAPSHOT'ı olarak.

**ÖNEMLİ**: Bu dosyadaki Level 2 ile ilgili testler "doğru tasarım" testi
DEĞİLDİR -- "AI Raw Context Integrity Audit" turunda tespit edilen, PARKED
bırakılan bir bilgi-kaybı davranışının bilinçli dondurulmuş halidir. Level 2
raw-value preservation ileride uygulanırsa bu testler KASITLI OLARAK
güncellenmelidir (bkz. ilgili testlerin docstring'i).

Network GEREKTIRMEZ. DB/log dosyasına dokunmaz (yalnız in-memory `report`
dict'leri ve gerçek `build_ai_analyst_context()` çağrıları).

Kaynak / provenance: "AI ANALYST RAW CONTEXT / PROMPT INPUT INTEGRITY AUDIT"
turunda doğrulanan gerçek davranışların bu turda kalıcı regression'a
dönüştürülmesi -- production kodu bu turda DEĞİŞMEDİ.
"""
import copy
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

ALWAYS_NODATA = {"clear_support", "risk_reward_ratio", "token_unlock_risk", "dxy_trend",
                  "etf_inflow", "mvrv_ratio", "sopr_recovery", "whale_accumulation",
                  "stablecoin_inflow"}


@pytest.fixture
def se():
    return kss.ScoreEngine(kss.SIGNAL_STAGE_WEIGHTS, kss.RISK_STAGE_WEIGHTS, kss.VetoEngine(kss.VETO_RULES))


def _build_answers(level=1, overrides=None, extra_values=None, disabled_labels=None):
    """STAGES_CONFIG'in tam bir answer seti -- overrides: {factor_id: answer},
    extra_values: {factor_id: (value, source, reason)} yalnız level==1 icin,
    disabled_labels: bu label'lara sahip sorular disabled=True olur."""
    overrides = overrides or {}
    extra_values = extra_values or {}
    disabled_labels = disabled_labels or set()
    answers = []
    for stage in kss.STAGES_CONFIG:
        for it in stage.items:
            fid = kss._FACTOR_ID_BY_LABEL.get(it.label, "?")
            if it.label in disabled_labels:
                ans, disabled = "nodata", True
            elif fid in overrides:
                ans, disabled = overrides[fid], False
            elif fid in ALWAYS_NODATA or fid == "recent_bad_event":
                ans, disabled = "nodata", False
            else:
                ans, disabled = "yes", False
            entry = {"stage": stage.title, "question": it.label, "answer": ans,
                     "weight": it.weight, "_item": it, "disabled": disabled}
            if level == 1 and fid in extra_values:
                value, source, reason = extra_values[fid]
                entry["value"] = value
                entry["source"] = source
                entry["reason"] = reason
            answers.append(entry)
    return answers


def _make_report(se, level=1, overrides=None, extra_values=None, disabled_labels=None,
                  model_d=None):
    answers = _build_answers(level, overrides, extra_values, disabled_labels)
    report = se.full_report(answers, kss.STAGES_CONFIG, "TESTCOIN")
    if model_d:
        report.update(model_d)
    return report


# ---------- 2) nodata vs no contract ----------

def test_no_answer_goes_to_usable_factors_not_unavailable(se):
    report = _make_report(se, overrides={"rsi_zone": "no"})
    ctx = kss.build_ai_analyst_context(report, "TESTCOIN")
    usable_ids = {f["factor_id"] for f in ctx["usable_factors"]}
    unavailable_ids = {f["factor_id"] for f in ctx["unavailable_factors"]}
    assert "rsi_zone" in usable_ids
    assert "rsi_zone" not in unavailable_ids


def test_nodata_answer_goes_to_unavailable_factors_not_usable(se):
    report = _make_report(se, overrides={"rsi_zone": "nodata"})
    ctx = kss.build_ai_analyst_context(report, "TESTCOIN")
    usable_ids = {f["factor_id"] for f in ctx["usable_factors"]}
    unavailable_ids = {f["factor_id"] for f in ctx["unavailable_factors"]}
    assert "rsi_zone" not in usable_ids
    assert "rsi_zone" in unavailable_ids


def test_no_factor_never_appears_in_both_lists(se):
    report = _make_report(se, overrides={"rsi_zone": "no", "macd_signal": "nodata"})
    ctx = kss.build_ai_analyst_context(report, "TESTCOIN")
    usable_ids = {f["factor_id"] for f in ctx["usable_factors"]}
    unavailable_ids = {f["factor_id"] for f in ctx["unavailable_factors"]}
    assert not (usable_ids & unavailable_ids), "hicbir factor iki listede birden olamaz"


# ---------- 3) wait contract ----------

def test_wait_answer_is_usable_with_exact_status(se):
    report = _make_report(se, overrides={"rsi_zone": "wait"})
    ctx = kss.build_ai_analyst_context(report, "TESTCOIN")
    entry = next(f for f in ctx["usable_factors"] if f["factor_id"] == "rsi_zone")
    assert entry["status"] == "wait"
    assert entry["factor_id"] not in {f["factor_id"] for f in ctx["unavailable_factors"]}


# ---------- 4) Factor registry parity ----------

def test_stages_config_factor_id_table_semantic_role_full_parity():
    stages_ids = {kss._FACTOR_ID_BY_LABEL.get(it.label)
                  for stage in kss.STAGES_CONFIG for it in stage.items}
    factor_table_ids = set(kss.FACTOR_ID_TABLE.keys())
    semantic_role_ids = set(kss.SEMANTIC_ROLE_BY_FACTOR_ID.keys())
    assert stages_ids == factor_table_ids
    assert stages_ids == semantic_role_ids


def test_structurally_unavailable_factors_never_appear_in_ai_context(se):
    """clear_support, risk_reward_ratio vb. -- production'da hep nodata --
    ne usable ne unavailable_factors'ta gorunmemeli (sistem prompt madte 3)."""
    report = _make_report(se)  # varsayilan: ALWAYS_NODATA hepsi nodata
    ctx = kss.build_ai_analyst_context(report, "TESTCOIN")
    all_ids = {f["factor_id"] for f in ctx["usable_factors"]} | \
              {f["factor_id"] for f in ctx["unavailable_factors"]}
    assert not (all_ids & kss.STRUCTURALLY_UNAVAILABLE_FACTOR_IDS), \
        f"structurally-unavailable factor(lar) AI context'e sizmis: {all_ids & kss.STRUCTURALLY_UNAVAILABLE_FACTOR_IDS}"


# ---------- 11) Unknown/unmapped factor -- fail-closed, crash yok ----------

def test_unknown_label_answer_silently_skipped_not_crash(se):
    """STAGES_CONFIG disi, tamamen uydurma bir label icin answer verilirse
    (_FACTOR_ID_BY_LABEL.get(...) None doner) context builder crash etmemeli,
    bu answer'i sessizce atlamali (ne usable ne unavailable)."""
    answers = _build_answers(1)
    answers.append({"stage": "Uydurma Stage", "question": "Bu hicbir yerde tanimli olmayan bir soru",
                     "answer": "yes", "weight": 5, "_item": None, "disabled": False})
    report = se.full_report(answers, kss.STAGES_CONFIG, "TESTCOIN")
    ctx = kss.build_ai_analyst_context(report, "TESTCOIN")  # crash ETMEMELI
    all_labels = {f["label"] for f in ctx["usable_factors"]} | {f["label"] for f in ctx["unavailable_factors"]}
    assert "Bu hicbir yerde tanimli olmayan bir soru" not in all_labels


# ---------- 12) disabled factor ----------

def test_disabled_answer_never_appears_in_ai_context(se):
    label = next(it.label for stage in kss.STAGES_CONFIG for it in stage.items if it.optional)
    report = _make_report(se, disabled_labels={label})
    ctx = kss.build_ai_analyst_context(report, "TESTCOIN")
    all_labels = {f["label"] for f in ctx["usable_factors"]} | {f["label"] for f in ctx["unavailable_factors"]}
    assert label not in all_labels, "disabled=True answer AI context'e hic girmemeli"


# ---------- 5) Display value contract (Level 1 vs Level 2 PARKED baseline) ----------

def test_level1_numeric_value_produces_display_value(se):
    report = _make_report(se, overrides={"rsi_zone": "wait"},
                           extra_values={"rsi_zone": (42.3, "Binance 1h", "")})
    ctx = kss.build_ai_analyst_context(report, "TESTCOIN")
    entry = next(f for f in ctx["usable_factors"] if f["factor_id"] == "rsi_zone")
    assert entry["display_value"] == "42.30"


def _inject_l2_value(report, factor_id, value=None, components=None, set_value=True):
    """LEVEL 2 RAW VALUE PROVENANCE fix'inden sonraki GERCEK run_level2()
    sekli: ilgili answer dict'ine 'value' (scalar tipler) veya 'components'
    (volume_spread) eklenir -- source/reason EKLENMEZ (production'da da yok,
    bkz. run_level2()). report mutate edilir, dondurulmez (in-place, mevcut
    _make_report() ciktisi uzerinde). set_value=False -- value anahtarini HIC
    ekleme (yalnizca components icin kullanilir)."""
    for a in report["answers"]:
        if kss._FACTOR_ID_BY_LABEL.get(a["question"]) == factor_id:
            if set_value:
                a["value"] = value
            if components is not None:
                a["components"] = components
    return report


def test_level2_scalar_value_now_propagates_to_display_value(se):
    """GUNCELLENDI (Level 2 Raw Value Provenance fix, onaylı, controlled
    implementation): eskiden bu test Level 2'nin 'value' anahtarini HIC
    tasimadigini (PARKED, lossy davranis) dondururdu. run_level2() artik
    scalar/numeric tipler icin gercek parse edilmis degeri answers[]'e
    ekliyor -- bu test artik GECERLI olan yeni contract'i dondurur: value
    verilirse display_value artik None DEGIL, format_display_value()'nun
    urettigi gercek deger."""
    report = _make_report(se, level=2, overrides={"rsi_zone": "wait"})
    _inject_l2_value(report, "rsi_zone", value=42.3)
    ctx = kss.build_ai_analyst_context(report, "TESTCOIN")
    entry = next(f for f in ctx["usable_factors"] if f["factor_id"] == "rsi_zone")
    assert entry["display_value"] == "42.30"


def test_level2_missing_value_still_produces_none_display_value(se):
    """Legacy/eksik durum HALA dogru ele aliniyor -- 'value' hic verilmezse
    (ör. bool tipi, ya da eski bir History kaydi) display_value None kalir,
    crash olmaz. Bu, YENI contract'in 'value yoksa None' tarafini korur."""
    report = _make_report(se, level=2, overrides={"rsi_zone": "wait"})  # value hic eklenmedi
    ctx = kss.build_ai_analyst_context(report, "TESTCOIN")
    entry = next(f for f in ctx["usable_factors"] if f["factor_id"] == "rsi_zone")
    assert entry["display_value"] is None


def test_source_field_never_appears_in_ai_context_any_level(se):
    """'source' alani hicbir seviyede AI context'e HIC kopyalanmiyor --
    Level 1'de bile. Level 2 Finding 2'nin 'source' bileseni bu yuzden
    zaten moot -- context builder onu Level 1'de de kullanmiyor."""
    report = _make_report(se, overrides={"rsi_zone": "wait"},
                           extra_values={"rsi_zone": (42.3, "Binance 1h", "")})
    ctx = kss.build_ai_analyst_context(report, "TESTCOIN")
    entry = next(f for f in ctx["usable_factors"] if f["factor_id"] == "rsi_zone")
    assert "source" not in entry


# ---------- 6) Same status / different raw value -- PARKED snapshot ----------

def test_level2_same_status_different_raw_value_now_distinguishable(se):
    """GUNCELLENDI (Level 2 Raw Value Provenance fix, onaylı, controlled
    implementation): eskiden bu test, ayni 'wait' siniflandirmasina dusen iki
    KAVRAMSAL OLARAK farkli Level 2 girdisinin (ör. RSI=25 vs RSI=48) AI
    context'te BIREBIR AYNI ciktigini (PARKED, lossy davranis) dondururdu.
    run_level2() artik gercek parse edilmis degeri tasidigi icin bu iki
    senaryo artik AYIRT EDILEBILIR -- ayni 'answer' (karar/skor ETKILENMEDI),
    farkli 'display_value' (provenance geri kazanildi)."""
    report_a = _make_report(se, level=2, overrides={"rsi_zone": "wait"})
    report_b = _make_report(se, level=2, overrides={"rsi_zone": "wait"})
    _inject_l2_value(report_a, "rsi_zone", value=55.0)   # wait bandinda (50-70)
    _inject_l2_value(report_b, "rsi_zone", value=65.0)   # ayni wait bandinda, farkli raw
    ctx_a = kss.build_ai_analyst_context(report_a, "TESTCOIN")
    ctx_b = kss.build_ai_analyst_context(report_b, "TESTCOIN")
    entry_a = next(f for f in ctx_a["usable_factors"] if f["factor_id"] == "rsi_zone")
    entry_b = next(f for f in ctx_b["usable_factors"] if f["factor_id"] == "rsi_zone")
    assert entry_a["status"] == entry_b["status"] == "wait", "karar/status AYNI kalmali"
    assert entry_a["display_value"] != entry_b["display_value"], (
        "raw value artik AI context'te AYIRT EDILEBILIR olmali")
    assert entry_a["display_value"] == "55.00" and entry_b["display_value"] == "65.00"


# ---------- 7) decision_context field contract ----------

def test_decision_context_contains_expected_core_fields(se):
    report = _make_report(se)
    ctx = kss.build_ai_analyst_context(report, "TESTCOIN")
    dc = ctx["decision_context"]
    expected_present = {
        "symbol", "signal_score", "signal_coverage", "confidence", "confidence_desc",
        "risk_score", "risk_coverage", "risk_available", "risk_reliable", "verdict_title", "verdict_desc",
        "verdict_entry_status", "entry_timing_label", "entry_timing_score",
        "entry_timing_answered", "entry_timing_total", "vetos",
        "model_d_restricted_candidate", "model_d_recent_price_action_class",
        "model_d_btc_7d_return_pct", "model_d_btc_strong_down",
    }
    missing = expected_present - set(dc.keys())
    assert not missing, f"beklenen alanlar decision_context'ten kayip: {missing}"


def test_decision_context_exposes_risk_reliable_but_not_risk_breadth(se):
    """GUNCELLENDI (Decision Surface Parity Audit / Bulgu 1 fix, onaylı,
    controlled implementation): eskiden bu test risk_reliable'in de decision_
    context'te YOK olmasini "kasitli davranis" olarak dondurmustu -- audit
    bunun GUI/Clipboard/History'nin ucunun de risk_reliable=False iken ham
    risk_score'u gizledigini, ama AI context'in ayni sayiyi bu provenance
    OLMADAN tasidigini kanitlamasi uzerine, decision_context'e SADECE
    "risk_reliable" additive olarak eklendi. risk_breadth HALA yok (bu turun
    kapsami disinda, eklenmesi istenmedi) -- o kismi hala dondurur."""
    report = _make_report(se)
    ctx = kss.build_ai_analyst_context(report, "TESTCOIN")
    dc = ctx["decision_context"]
    assert "risk_breadth" not in dc
    assert "risk_reliable" in dc
    assert dc["risk_reliable"] == report.get("risk_reliable")


# ---------- 8) Model D context handoff ----------

def test_model_d_fields_absent_when_not_a_candidate(se):
    report = _make_report(se, model_d={"restricted_candidate": False, "recent_price_action_class": None,
                                        "btc_7d_return_pct": None, "btc_strong_down": None})
    ctx = kss.build_ai_analyst_context(report, "TESTCOIN")
    dc = ctx["decision_context"]
    assert dc["model_d_restricted_candidate"] is False
    assert dc["model_d_recent_price_action_class"] is None


def test_model_d_candidate_confirmed_fields_present(se):
    report = _make_report(se, model_d={"restricted_candidate": True,
                                        "recent_price_action_class": "extended_bounce",
                                        "btc_7d_return_pct": -8.2, "btc_strong_down": True})
    ctx = kss.build_ai_analyst_context(report, "TESTCOIN")
    dc = ctx["decision_context"]
    assert dc["model_d_restricted_candidate"] is True
    assert dc["model_d_recent_price_action_class"] == "extended_bounce"
    assert dc["model_d_btc_7d_return_pct"] == -8.2
    assert dc["model_d_btc_strong_down"] is True


def test_model_d_context_does_not_override_verdict(se):
    """Model D alanlari VAR olsun -- verdict_title context builder tarafindan
    DEGISTIRILMEMELI, yalniz report'tan oldugu gibi tasinmali."""
    report = _make_report(se, model_d={"restricted_candidate": True})
    original_verdict = report["verdict_title"]
    ctx = kss.build_ai_analyst_context(report, "TESTCOIN")
    assert ctx["decision_context"]["verdict_title"] == original_verdict


# ---------- 9) Risk reliability semantic closure (legacy %50 metni yok) ----------

def test_partial_risk_coverage_unreliable_does_not_use_legacy_percentage_text(se):
    """breadth=1/2 (risk_coverage=50.0) -- risk_reliable=False -- durumunda
    verdict_desc'in legacy '%50 altinda' esik-tabanli semantigine DONMEDIGINI
    dogrula -- context payload uzerinden (prompt'a dokunmuyoruz). NOT: mevcut
    2-faktorlu breadth modelinde (recent_bad_event + volatility_risk)
    risk_coverage=100 iken risk_reliable=False olan bir kombinasyon
    MATEMATIKSEL OLARAK MUMKUN DEGIL (coverage=100 icin ikisi de yanitlanmis
    olmali, ki bu zaten breadth=2 -> reliable=True demektir) -- bu yuzden
    testin dayandigi kanitlanmis kombinasyon budur (breadth=1, coverage=50)."""
    report = _make_report(se, overrides={"volatility_risk": "yes", "recent_bad_event": "nodata"})
    assert report["risk_coverage"] == 50.0
    assert report["risk_reliable"] is False
    ctx = kss.build_ai_analyst_context(report, "TESTCOIN")
    desc = ctx["decision_context"]["verdict_desc"] or ""
    assert "%50" not in desc
    assert "altında" not in desc


# ---------- 10) Structure context handoff ----------

def test_structure_context_none_when_technical_structure_none(se):
    report = _make_report(se)
    ctx = kss.build_ai_analyst_context(report, "TESTCOIN", technical_structure=None)  # crash ETMEMELI
    assert ctx["structure_context"] is None
    assert ctx["_valid_structure_evidence"] == []


def test_structure_context_insufficient_data_status_passthrough(se):
    report = _make_report(se)
    ts = {"status": "insufficient_data"}
    ctx = kss.build_ai_analyst_context(report, "TESTCOIN", technical_structure=ts)
    assert ctx["structure_context"] == {"status": "insufficient_data"}
    assert ctx["_valid_structure_evidence"] == []


def test_structure_context_ok_status_populates_fields(se):
    report = _make_report(se)
    ts = {
        "status": "ok", "bar_count": 250, "trend_structure": "bullish", "trend_reason": "HH/HL",
        "atr14": 1.5, "current_price": 100.0,
        "nearest_support": {"zone_low": 95.0, "zone_high": 96.0, "touch_count": 3,
                             "last_touched_bars_ago": 5, "confidence": "high"},
        "nearest_resistance": None,
        "distance_to_support_pct": 4.0, "distance_to_resistance_pct": None,
        "recent_price_action": None,
    }
    ctx = kss.build_ai_analyst_context(report, "TESTCOIN", technical_structure=ts)
    sc = ctx["structure_context"]
    assert sc is not None
    assert sc["trend_structure"] == "bullish"
    assert sc["nearest_support"]["zone_low"] == 95.0
    assert sc["nearest_resistance"] is None
    assert "structure:trend_structure" in ctx["_valid_structure_evidence"]
    assert "structure:nearest_support" in ctx["_valid_structure_evidence"]
    assert "structure:nearest_resistance" not in ctx["_valid_structure_evidence"]


# ---------- 13) Level 3 categorical ----------

def test_level3_categorical_answers_partition_correctly(se):
    report = _make_report(se, level=3, overrides={
        "rsi_zone": "yes", "macd_signal": "wait", "obv_trend": "no", "funding_balance": "nodata",
    })
    ctx = kss.build_ai_analyst_context(report, "TESTCOIN")
    usable_by_id = {f["factor_id"]: f for f in ctx["usable_factors"]}
    unavailable_ids = {f["factor_id"] for f in ctx["unavailable_factors"]}
    assert usable_by_id["rsi_zone"]["status"] == "yes"
    assert usable_by_id["macd_signal"]["status"] == "wait"
    assert usable_by_id["obv_trend"]["status"] == "no"
    assert "funding_balance" in unavailable_ids
    # Level 3'te value beklentisi yok -- display_value None olabilir, zorunlu degil
    assert usable_by_id["rsi_zone"].get("display_value") is None


# ---------- 14) Mutation safety ----------

def test_build_ai_analyst_context_does_not_mutate_input_report(se):
    report = _make_report(se, overrides={"rsi_zone": "wait"})
    report_before = copy.deepcopy(report)
    kss.build_ai_analyst_context(report, "TESTCOIN")
    assert report == report_before, "build_ai_analyst_context() input report'u mutate ETMEMELI"


def test_build_ai_analyst_context_does_not_mutate_answers_list(se):
    report = _make_report(se, overrides={"rsi_zone": "wait"})
    answers_before = copy.deepcopy(report["answers"])
    kss.build_ai_analyst_context(report, "TESTCOIN")
    assert report["answers"] == answers_before


# ---------- 15) Determinism ----------

def test_build_ai_analyst_context_deterministic_same_input_same_output(se):
    report = _make_report(se, overrides={"rsi_zone": "wait"},
                           extra_values={"rsi_zone": (42.3, "Binance 1h", "")})
    ctx1 = kss.build_ai_analyst_context(copy.deepcopy(report), "TESTCOIN")
    ctx2 = kss.build_ai_analyst_context(copy.deepcopy(report), "TESTCOIN")
    assert ctx1 == ctx2


# ---------- 16) DECISION SURFACE PARITY AUDIT / BULGU 1 CLOSURE --------------
# decision_context artik report["risk_reliable"]'i kaybetmiyor. Once (audit
# turunde bulunan hal): GUI/Clipboard/History ucu de risk_reliable=False iken
# ham risk_score sayisini gizliyordu ("Yetersiz veri"), ama AI context ayni
# sayiyi risk_reliable provenance'i OLMADAN tasiyordu. Bu tur SADECE
# decision_context'e "risk_reliable" alanini additive olarak ekliyor --
# risk_score/risk_available/risk_coverage/confidence/verdict/vetos/Model D/
# determine_ai_analyst_mode() hicbiri degismedi.

RISK_STAGE_RELIABLE_OVERRIDES = {"recent_bad_event": "yes", "volatility_risk": "yes"}


def test_risk_reliable_true_numeric_score_propagates_true(se):
    report = _make_report(se, overrides=RISK_STAGE_RELIABLE_OVERRIDES)
    assert report["risk_reliable"] is True
    assert report["risk_score"] is not None
    ctx = kss.build_ai_analyst_context(report, "TESTCOIN")
    assert ctx["decision_context"]["risk_reliable"] is True


def test_risk_reliable_false_numeric_score_propagates_false(se):
    """AUDIT KRITIK FIXTURE: risk_score=0.0, risk_reliable=False, risk_breadth=1
    -- Bulgu 1'in tam olarak tespit edildigi senaryo."""
    report = _make_report(se)  # default: recent_bad_event=nodata -> breadth=0/1, reliable=False
    assert report["risk_reliable"] is False
    assert report["risk_score"] is not None  # ham sayi hala var (silinmiyor)
    ctx = kss.build_ai_analyst_context(report, "TESTCOIN")
    dc = ctx["decision_context"]
    assert dc["risk_reliable"] is False
    assert dc["risk_score"] == report["risk_score"], (
        "amac skoru silmek DEGIL, guvenilirlik bilgisini skorla BIRLIKTE tasimak")


def test_risk_score_contract_unchanged_when_unreliable(se):
    """risk_score/risk_available alanlarinin MEVCUT anlami/degeri bu turdan
    ETKILENMEMELI -- yalnizca yeni bir alan EKLENDI."""
    report = _make_report(se)
    ctx = kss.build_ai_analyst_context(report, "TESTCOIN")
    dc = ctx["decision_context"]
    assert dc["risk_score"] == report.get("risk_score")
    assert dc["risk_available"] == (report.get("risk_score") is not None)


def test_missing_risk_reliable_key_no_fail_open_default():
    """Legacy/synthetic bir report'ta 'risk_reliable' anahtari hic YOKSA
    (ör. eski bir call path/mock), context bunu sessizce True/False UYDURMAMALI
    -- report.get("risk_reliable") zaten anahtar yoksa None doner, bu da
    context'e AYNEN (None olarak) gecmeli, fail-open bir varsayilan DEGIL."""
    fake_report = {
        "signal_score": 50.0, "signal_coverage": 50.0, "confidence": "Orta",
        "confidence_desc": "", "risk_score": 42.0, "risk_coverage": 50.0,
        "verdict_title": "Ön aday", "verdict_desc": "", "entry_status": "Bekle",
        "entry_timing": None, "entry_timing_score": None,
        "entry_timing_answered": None, "entry_timing_total": None,
        "vetos": [], "answers": [],
        # KASITLI: "risk_reliable" anahtari YOK.
    }
    ctx = kss.build_ai_analyst_context(fake_report, "TESTCOIN")
    assert ctx["decision_context"]["risk_reliable"] is None, (
        "eksik risk_reliable icin sessiz True/False varsayimi UYDURULMAMALI")


def test_determine_ai_analyst_mode_unaffected_reliable_false(se):
    """determine_ai_analyst_mode() report["risk_reliable"]'i DOGRUDAN okur
    (decision_context'ten DEGIL) -- bu tur mode routing'e HIC DOKUNMADI,
    ikinci bir karar sistemi kurulmadi. Once/sonra ayni mode uretilmeli."""
    report = _make_report(se)
    assert report["risk_reliable"] is False
    mode = kss.determine_ai_analyst_mode(report)
    assert mode == "LIMITED"  # veto yok, verdict!='Elenir', risk_score var, confidence!='Yetersiz', reliable=False


def test_determine_ai_analyst_mode_unaffected_reliable_true(se):
    report = _make_report(se, overrides=RISK_STAGE_RELIABLE_OVERRIDES)
    assert report["risk_reliable"] is True
    mode = kss.determine_ai_analyst_mode(report)
    assert mode == "NORMAL"


@pytest.mark.parametrize("overrides", [
    {},  # default (nodata recent_bad_event) -- reliable False
    RISK_STAGE_RELIABLE_OVERRIDES,  # reliable True
    {"rsi_zone": "no", "macd_signal": "wait"},
])
def test_other_decision_context_fields_before_after_parity(se, overrides):
    """Bulgu 1 fix'i verdict_title/verdict_desc/confidence/vetos alanlarini
    HIC ETKILEMEMELI -- yalnizca 'risk_reliable' EKLENDI, digerleri ayni
    degerde kalmali (report'un kendisiyle birebir)."""
    report = _make_report(se, overrides=overrides)
    ctx = kss.build_ai_analyst_context(report, "TESTCOIN")
    dc = ctx["decision_context"]
    assert dc["verdict_title"] == report.get("verdict_title")
    assert dc["verdict_desc"] == report.get("verdict_desc")
    assert dc["confidence"] == report.get("confidence")
    assert dc["vetos"] == list(report.get("vetos") or [])


def test_model_d_context_fields_before_after_parity(se):
    """Model D additive alanlari (restricted_candidate vb.) Bulgu 1 fix'inden
    ETKILENMEMELI."""
    report = _make_report(se, overrides={"rsi_zone": "no"}, model_d={
        "restricted_candidate": True,
        "recent_price_action_class": "positive",
        "btc_7d_return_pct": -3.2,
        "btc_strong_down": False,
    })
    ctx = kss.build_ai_analyst_context(report, "TESTCOIN")
    dc = ctx["decision_context"]
    assert dc["model_d_restricted_candidate"] is True
    assert dc["model_d_recent_price_action_class"] == "positive"
    assert dc["model_d_btc_7d_return_pct"] == -3.2
    assert dc["model_d_btc_strong_down"] is False
    # yeni alan digerleriyle BIRLIKTE, onlari EZMEDEN var olmali
    assert "risk_reliable" in dc


def test_bulgu1_fix_does_not_mutate_input_report(se):
    report = _make_report(se)
    report_before = copy.deepcopy(report)
    kss.build_ai_analyst_context(report, "TESTCOIN")
    assert report == report_before


def test_bulgu1_fix_deterministic_same_input_same_output(se):
    report = _make_report(se)
    ctx1 = kss.build_ai_analyst_context(copy.deepcopy(report), "TESTCOIN")
    ctx2 = kss.build_ai_analyst_context(copy.deepcopy(report), "TESTCOIN")
    assert ctx1["decision_context"]["risk_reliable"] == ctx2["decision_context"]["risk_reliable"]
    assert ctx1 == ctx2


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
