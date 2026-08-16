# -*- coding: utf-8 -*-
"""
PERMANENT REGRESSION SUITE -- Level 2 Raw Value Provenance (Controlled Implementation)

Ne test ediyor: `run_level2()`'nin artık numeric/composite tipler (range,
min, max, ratio, funding, count, bool_or_min, volume_spread) için kullanıcının
gerçekten girdiği, `ThresholdEngine.evaluate()`'e doğru ulaşan ham (parse
edilmiş) değeri `answers[]`'e (`"value"` veya `"components"` anahtarıyla,
mevcut Level 1/AI context sözleşmesiyle BİREBİR AYNI şekilde) EKLEDİĞİNİ;
sınıflandırmanın (`answer`), skorun, veto/verdict'in BİT-FOR-BİT AYNI
kaldığını; zero/False/empty ayrımının bozulmadığını; History DB round-trip'te
değerin korunduğunu; eski (`value` anahtarı olmayan) History kayıtlarının
crash'siz çalıştığını; Level 3'ün bu turdan HİÇ ETKİLENMEDİĞİNİ.

`run_level2()`'nin bizzat KENDİSİ (Qt widget'ları içerdiği için) burada
çağrılmıyor -- bu dosya, üretim kaynağından (satır ~9413-9513, bu turda
değişen tam kod) BİREBİR kopyalanmış, yalnız Qt widget okuma yerine doğrudan
metin/seçim parametresi alan izole bir harness kullanır. Threshold
sınıflandırması bizzat gerçek `ThresholdEngine.evaluate()` ile yapılır --
yeniden implemente edilmedi.

Network GEREKTİRMEZ. Gerçek kullanıcı DB'sine dokunmaz (izole temp dizin).

Kaynak / provenance: "Level 2 Raw Value Provenance -- Controlled
Implementation" turu (Level 2/3 Raw Manual Input Round-Trip Integrity
Audit'in tek material/B-sınıfı bulgusunun kapanışı).
"""
import gc
import importlib.util
import json
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

TE = kss.ThresholdEngine
ALWAYS_NODATA = {"clear_support", "risk_reward_ratio", "token_unlock_risk", "dxy_trend",
                  "etf_inflow", "mvrv_ratio", "sopr_recovery", "whale_accumulation",
                  "stablecoin_inflow"}


@pytest.fixture
def se():
    return kss.ScoreEngine(kss.SIGNAL_STAGE_WEIGHTS, kss.RISK_STAGE_WEIGHTS, kss.VetoEngine(kss.VETO_RULES))


def _find_item(ttype):
    return next(it for stage in kss.STAGES_CONFIG for it in stage.items
                if (it.thresholds or {}).get("type") == ttype)


# ---------- İzole harness'lar: run_level2()'nin GERÇEK kaynağının (bu turda
# değişen satırlar) birebir kopyası, yalnız Qt widget okuması yerine doğrudan
# metin/seçim parametresi alır. ----------

def l2_generic_numeric(item, texts):
    """range/min/max/ratio/funding/count -- run_level2()'nin `else:` dalı."""
    inputs = []
    has_data = True
    for t in texts:
        if t == "":
            has_data = False
            break
        inputs.append(float(t.replace(",", ".")))
    ans = "nodata" if not has_data else TE.evaluate(item, inputs)
    provenance = {}
    if has_data:
        provenance["value"] = inputs[0]
    return ans, provenance


def l2_volume_spread(item, texts):
    inputs = []
    has_data = True
    for t in texts:
        if t == "":
            has_data = False
            break
        inputs.append(float(t.replace(",", ".")))
    ans = "nodata" if not has_data else TE.evaluate(item, inputs)
    provenance = {}
    if has_data:
        provenance["components"] = {"volume_24h": inputs[0], "spread_pct": inputs[1]}
    return ans, provenance


def l2_bool_or_min(item, vol_text, squeeze_sel):
    th = item.thresholds or {}
    vol_val = float(vol_text.replace(",", ".")) if vol_text not in (None, "") else None
    squeeze_val = float(squeeze_sel) if squeeze_sel is not None else None
    if vol_val is None and squeeze_val is None:
        ans = "nodata"
    elif vol_val is None:
        ans = "wait" if squeeze_val >= 1 else "no"
    elif squeeze_val is None:
        yes_vol = th.get("yes_vol", 999)
        wait_vol = th.get("wait_vol", 0)
        if vol_val >= yes_vol:
            ans = "yes"
        elif vol_val >= wait_vol:
            ans = "wait"
        else:
            ans = "nodata"
    else:
        ans = TE.evaluate(item, [vol_val, squeeze_val])
    return ans, {"value": vol_val}


def l2_bool(group_values, selected):
    """bool tipi: değişmedi, provenance her zaman bos (kategorik)."""
    return (selected or "nodata"), {}


# ---------- 1) Scalar numeric raw value preserved ----------

def test_scalar_value_preserved():
    it = _find_item("range")
    ans, prov = l2_generic_numeric(it, ["35.00"])
    assert ans == "yes"
    assert prov["value"] == 35.0  # "2.30"->2.3 normalizasyonu kabul, semantic value korunuyor


@pytest.mark.parametrize("ttype", ["range", "max", "ratio", "funding", "count"])
def test_all_scalar_types_produce_value_key_when_answered(ttype):
    # NOT: "min" ThresholdEngine.evaluate()'de tanimli ama STAGES_CONFIG'te
    # HALIHAZIRDA kullanan hicbir soru yok (bu turun 1. adimindaki mekanik
    # envanterle dogrulandi) -- parametrize listesine dahil edilmedi.
    it = _find_item(ttype)
    labels = (it.thresholds or {}).get("input_labels", ["Değer"])
    texts = ["1.0"] * len(labels)
    ans, prov = l2_generic_numeric(it, texts)
    assert ans != "nodata"
    assert "value" in prov


# ---------- 2) Zero preserved ----------

def test_zero_preserved_not_missing():
    it = _find_item("range")
    ans, prov = l2_generic_numeric(it, ["0"])
    assert prov["value"] == 0.0
    assert ans == "no"  # RSI=0 -> [20,70] disinda -> 'no', ama 'nodata' DEGIL


def test_bool_or_min_zero_both_preserved():
    it = _find_item("bool_or_min")
    ans, prov = l2_bool_or_min(it, "0", "0")
    assert prov["value"] == 0.0
    assert ans == "no"  # gercek deger var, 'nodata' DEGIL


# ---------- 3) Empty -> nodata, value key absent ----------

def test_empty_input_produces_nodata_no_value_key():
    it = _find_item("range")
    ans, prov = l2_generic_numeric(it, [""])
    assert ans == "nodata"
    assert "value" not in prov


def test_bool_or_min_both_missing_nodata_value_none():
    it = _find_item("bool_or_min")
    ans, prov = l2_bool_or_min(it, None, None)
    assert ans == "nodata"
    assert prov["value"] is None  # anahtar VAR ama deger None -- False/0 ile KARISTIRILMIYOR


def test_unanswered_bool_is_nodata():
    ans, prov = l2_bool(["yes", "wait", "no", "nodata"], None)
    assert ans == "nodata"
    assert prov == {}


# ---------- 4) Same-status / different-value preserved ----------

def test_same_status_different_value_distinguishable():
    it = _find_item("range")
    ans_a, prov_a = l2_generic_numeric(it, ["35"])
    ans_b, prov_b = l2_generic_numeric(it, ["45"])
    assert ans_a == ans_b == "yes"
    assert prov_a["value"] != prov_b["value"]
    assert prov_a["value"] == 35.0 and prov_b["value"] == 45.0


# ---------- 5) Exact threshold raw value preserved ----------

@pytest.mark.parametrize("v,expected_ans", [
    (19.999, "no"), (20.0, "wait"), (29.999, "wait"), (30.0, "yes"),
    (50.0, "yes"), (50.001, "wait"), (70.0, "wait"), (70.001, "no"),
])
def test_boundary_values_preserved_and_classification_unchanged(v, expected_ans):
    it = _find_item("range")
    ans, prov = l2_generic_numeric(it, [str(v)])
    assert ans == expected_ans, "THRESHOLD SINIFLANDIRMASI DEGISMEMELI (frozen)"
    assert prov["value"] == v


# ---------- 6) volume_spread provenance ----------

def test_volume_spread_full_input_components_preserved():
    it = _find_item("volume_spread")
    ans, prov = l2_volume_spread(it, ["8.0", "0.3"])
    assert ans != "nodata"
    assert prov["components"] == {"volume_24h": 8.0, "spread_pct": 0.3}


def test_volume_spread_missing_input_no_fake_components():
    it = _find_item("volume_spread")
    ans, prov = l2_volume_spread(it, ["8.0", ""])
    assert ans == "nodata"
    assert "components" not in prov, "bilinmeyen component icin sahte 0 uretilmemeli"


def test_volume_spread_zero_preserved():
    it = _find_item("volume_spread")
    ans, prov = l2_volume_spread(it, ["0", "0.05"])
    assert prov["components"]["volume_24h"] == 0.0


# ---------- 7/8) bool_or_min full + partial input ----------

def test_bool_or_min_full_input_value_preserved():
    it = _find_item("bool_or_min")
    ans, prov = l2_bool_or_min(it, "2.0", "1")
    assert ans == TE.evaluate(it, [2.0, 1.0])
    assert prov["value"] == 2.0


def test_bool_or_min_partial_squeeze_missing_value_preserved_semantics_unchanged():
    """PARTIAL-INPUT SEMANTICS FIX (onceki turdan, FROZEN) bu turda
    DEGISMEDI -- yalniz value artik ayrica taniniyor."""
    it = _find_item("bool_or_min")
    th = it.thresholds
    yes_vol, wait_vol = th["yes_vol"], th["wait_vol"]
    ans_yes, prov_yes = l2_bool_or_min(it, str(yes_vol), None)
    assert ans_yes == "yes" and prov_yes["value"] == yes_vol
    ans_wait, prov_wait = l2_bool_or_min(it, str(wait_vol), None)
    assert ans_wait == "wait" and prov_wait["value"] == wait_vol
    ans_nodata, prov_nodata = l2_bool_or_min(it, "0.0", None)
    assert ans_nodata == "nodata" and prov_nodata["value"] == 0.0


def test_bool_or_min_partial_vol_missing_value_is_none():
    it = _find_item("bool_or_min")
    ans, prov = l2_bool_or_min(it, None, "1")
    assert ans == "wait"
    assert prov["value"] is None  # vol hic girilmedi -- uydurma deger YOK


# ---------- 9) Level 3 unchanged ----------

def test_level3_run_logic_unaffected_by_this_turn(se):
    """Level 3 kategorik akisi (run_level3()) bu turda hic degismedi --
    answers[] hala value/components tasimiyor, yalniz answer/disabled."""
    answers = []
    for stage in kss.STAGES_CONFIG:
        for it in stage.items:
            fid = kss._FACTOR_ID_BY_LABEL.get(it.label, "?")
            ans = "nodata" if fid in ALWAYS_NODATA or fid == "recent_bad_event" else "yes"
            answers.append({"stage": stage.title, "question": it.label, "answer": ans,
                             "weight": it.weight, "_item": it, "disabled": False})
    report = se.full_report(answers, kss.STAGES_CONFIG, "TESTCOIN")
    for a in report["answers"]:
        assert "value" not in a
        assert "components" not in a


# ---------- 13) Score/verdict parity (before: no value, after: with value) ----------

def _build_full_answers(se_engine, rsi_value, with_value):
    answers = []
    for stage in kss.STAGES_CONFIG:
        for it in stage.items:
            fid = kss._FACTOR_ID_BY_LABEL.get(it.label, "?")
            entry = {"stage": stage.title, "question": it.label, "answer": "yes",
                      "weight": it.weight, "_item": it, "disabled": False}
            if fid == "rsi_zone":
                if with_value:
                    entry["value"] = rsi_value
            elif fid in ALWAYS_NODATA or fid == "recent_bad_event":
                entry["answer"] = "nodata"
            answers.append(entry)
    return answers


def test_score_verdict_parity_before_after_provenance_fix(se):
    r_before = se.full_report(_build_full_answers(se, 35.0, with_value=False), kss.STAGES_CONFIG, "TESTCOIN")
    r_after = se.full_report(_build_full_answers(se, 35.0, with_value=True), kss.STAGES_CONFIG, "TESTCOIN")
    assert r_before["signal_score"] == r_after["signal_score"]
    assert r_before["risk_score"] == r_after["risk_score"]
    assert r_before["coverage"] == r_after["coverage"]
    assert r_before["confidence"] == r_after["confidence"]
    assert r_before["verdict_title"] == r_after["verdict_title"]
    assert r_before["vetos"] == r_after["vetos"]


# ---------- 10/11) History DB round-trip + legacy compatibility ----------

def _rmtree_retry(path, attempts=8, delay=0.2):
    import time as _time
    for i in range(attempts):
        gc.collect()
        try:
            shutil.rmtree(path)
            return
        except OSError:
            if i == attempts - 1:
                return
            _time.sleep(delay)


def test_history_roundtrip_value_preserved(se):
    tmpdir = tempfile.mkdtemp(prefix="l2_provenance_hist_")
    try:
        db = kss.HistoryDB.__new__(kss.HistoryDB)
        db._db_path = (lambda p: (lambda: p))(os.path.join(tmpdir, "t.db"))
        db._init_db()
        report = se.full_report(_build_full_answers(se, 35.0, with_value=True), kss.STAGES_CONFIG, "TESTCOIN")
        rid = db.save_analysis("TESTCOIN", "Level 2 — Yarı Otomatik", report,
                                analysis_price=1.0, data_source="manual",
                                analysis_time="2026-08-16T10:00:00")
        row = db.get_analysis_by_id(rid)
        stored = json.loads(row["answers"])
        rsi_stored = next(a for a in stored if kss._FACTOR_ID_BY_LABEL.get(a["question"]) == "rsi_zone")
        assert rsi_stored.get("value") == 35.0
    finally:
        _rmtree_retry(tmpdir)


def test_history_roundtrip_volume_spread_components_preserved(se):
    tmpdir = tempfile.mkdtemp(prefix="l2_provenance_hist_vs_")
    try:
        db = kss.HistoryDB.__new__(kss.HistoryDB)
        db._db_path = (lambda p: (lambda: p))(os.path.join(tmpdir, "t.db"))
        db._init_db()
        answers = _build_full_answers(se, 35.0, with_value=True)
        for a in answers:
            if kss._FACTOR_ID_BY_LABEL.get(a["question"]) == "volume_spread_combined":
                a["components"] = {"volume_24h": 8.0, "spread_pct": 0.3}
        report = se.full_report(answers, kss.STAGES_CONFIG, "TESTCOIN")
        rid = db.save_analysis("TESTCOIN", "Level 2 — Yarı Otomatik", report,
                                analysis_price=1.0, data_source="manual",
                                analysis_time="2026-08-16T10:00:00")
        row = db.get_analysis_by_id(rid)
        stored = json.loads(row["answers"])
        vs_stored = next(a for a in stored if kss._FACTOR_ID_BY_LABEL.get(a["question"]) == "volume_spread_combined")
        assert vs_stored.get("components") == {"volume_24h": 8.0, "spread_pct": 0.3}
    finally:
        _rmtree_retry(tmpdir)


def test_legacy_history_row_missing_value_no_crash(se):
    """Eski (bu fix'ten ONCEKI) History kayitlarinda 'value' anahtari hic
    yok -- read/AI-context path'i crash ETMEMELI, display_value=None
    dogru sekilde uretilmeli (backward compatibility)."""
    tmpdir = tempfile.mkdtemp(prefix="l2_provenance_hist_legacy_")
    try:
        db = kss.HistoryDB.__new__(kss.HistoryDB)
        db._db_path = (lambda p: (lambda: p))(os.path.join(tmpdir, "t.db"))
        db._init_db()
        report_legacy = se.full_report(_build_full_answers(se, 35.0, with_value=False),
                                        kss.STAGES_CONFIG, "TESTCOIN")
        rid = db.save_analysis("TESTCOIN", "Level 2 — Yarı Otomatik", report_legacy,
                                analysis_price=1.0, data_source="manual",
                                analysis_time="2026-08-16T09:00:00")
        row = db.get_analysis_by_id(rid)
        stored = json.loads(row["answers"])
        rsi_stored = next(a for a in stored if kss._FACTOR_ID_BY_LABEL.get(a["question"]) == "rsi_zone")
        assert "value" not in rsi_stored
        ctx = kss.build_ai_analyst_context(report_legacy, "TESTCOIN")  # crash olmamali
        entry = next(f for f in ctx["usable_factors"] if f["factor_id"] == "rsi_zone")
        assert entry["display_value"] is None
    finally:
        _rmtree_retry(tmpdir)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
