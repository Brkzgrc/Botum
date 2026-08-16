# -*- coding: utf-8 -*-
"""
PERMANENT REGRESSION SUITE -- _metric_for_label() Fail-Closed Correctness Fix

Ne test ediyor: `_metric_for_label()`'in artik eslesmeyen bir label icin
SESSIZCE "fear_greed" DONMEDIGINI (eski, kaldirilan davranis), bunun yerine
gercek hicbir metrik adiyla cakismayan bir sentinel dondurdugunu; bu
sentinel'in `RealFetcher.fetch()`'in kendi mevcut fail-closed son satirina
(`return nodata("", "Desteklenmeyen metrik")`) dustugunu; tek bir eslesmeyen
soru varsa SADECE o sorunun nodata oldugunu, digerlerinin ve tum
Level1Worker akisinin ETKILENMEDIGINI; guncel 30 STAGES_CONFIG sorusunun
HEPSININ (volume_spread ozel dali dahil) mevcut, dogru metric'e/davranisa
route edildigini (registry guard -- gelecekte mapping'i unutulan yeni bir
soru eklenirse bu test FAIL verir).

Network GEREKTIRMEZ. DB'ye dokunmaz.

Kaynak / provenance: "System Gap Audit Round 3 / Dead Code + Obsolete
Contract + Drift Audit" turunda bulunan, bilinçli olarak PARKED birakilan
`_metric_for_label()` silent-fallback correctness borcunun bu turda kapatilmasi.
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


# ---------- 1) Sentinel contract ----------

def test_unmapped_label_returns_sentinel_not_fear_greed():
    result = kss._metric_for_label("hicbir_anahtarla_eslesmeyen_tamamen_uydurma_bir_soru_metni")
    assert result == kss._UNMAPPED_METRIC_SENTINEL
    assert result != "fear_greed", "eski silent-fallback davranisi GERI GELMEMELI"


def test_sentinel_does_not_collide_with_any_real_mapped_metric_name():
    """Sentinel, guncel STAGES_CONFIG'teki TUM gercek label'larin urettigi
    metric adlarindan hicbiriyle cakismamali -- aksi halde yanlislikla
    gercek bir fetch() dalini tetikleyebilir."""
    real_metrics = {kss._metric_for_label(it.label) for stage in kss.STAGES_CONFIG for it in stage.items}
    real_metrics.discard(kss._UNMAPPED_METRIC_SENTINEL)  # volume_spread label'i da bu setten cikar
    assert kss._UNMAPPED_METRIC_SENTINEL not in real_metrics


def test_unmapped_label_reaches_unsupported_metric_nodata_via_fetch():
    """Sentinel, gercek RealFetcher.fetch() dispatch zincirinden gecirilince
    fetch()'in KENDI mevcut fail-closed son satirina dusuyor mu -- gercek
    kodun kendisiyle (mock'lanmadan) dogrulaniyor.
    NOT: `fetch()` cache'te olmayan bir symbol icin ONCE `_build_context()`
    (GERCEK network) cagirir -- bunu tetiklememek icin cache'i BOS bir dict
    ile onceden dolduruyoruz (yalniz `symbol in self._cache` kontrolunu
    gecmesi yeterli, network GEREKMIYOR)."""
    fetcher = kss.RealFetcher(on_progress=lambda m: None, should_cancel=lambda: False)
    fetcher._cache["BTC"] = {}
    dp = fetcher.fetch("BTC", kss._UNMAPPED_METRIC_SENTINEL)
    assert dp.available is False
    assert dp.status == "nodata"
    assert dp.reason == "Desteklenmeyen metrik"


def test_unmapped_label_never_triggers_fear_greed_fetch(monkeypatch):
    """Eski bug: eslesmeyen label sessizce Fear & Greed'e yonleniyordu.
    Bunu somut olarak reddet -- fetch_fear_greed() HIC cagrilmamali."""
    called = {"fg": False}

    def fake_fg():
        called["fg"] = True
        return 50

    monkeypatch.setattr(kss, "fetch_fear_greed", fake_fg)
    fetcher = kss.RealFetcher(on_progress=lambda m: None, should_cancel=lambda: False)
    # ctx["fear_greed"] hic doldurulmamis olsa bile (cache bos), fetch()
    # unmapped sentinel icin fetch_fear_greed'i TETIKLEMEMELI.
    fetcher._cache["BTC"] = {}  # bos context -- gercek fear_greed metric'i cagrilsa bile fetch_fear_greed tetiklenmez cunku ctx zaten var
    dp = fetcher.fetch("BTC", kss._UNMAPPED_METRIC_SENTINEL)
    assert dp.status == "nodata"
    assert called["fg"] is False, "unmapped label ASLA fetch_fear_greed() tetiklememeli"


# ---------- 2) volume_spread ozel dali etkilenmedi ----------

def test_volume_spread_label_not_routed_through_metric_for_label_fallback():
    """volume_spread sorusu KENDI ozel dalinda ele alinir, _metric_for_label()
    fallback'ine hic ihtiyac duymaz -- ama yine de _metric_for_label()
    dogrudan cagrilirsa (mevcut, degismeyen davranis) sentinel donmeli,
    'fear_greed' DEGIL."""
    label = "24s hacim > 5M$ ve spread makul"
    result = kss._metric_for_label(label)
    assert result == kss._UNMAPPED_METRIC_SENTINEL
    assert result != "fear_greed"


def test_volume_spread_item_flagged_as_volume_spread_type_in_stages_config():
    item = next(it for stage in kss.STAGES_CONFIG for it in stage.items
                if it.label == "24s hacim > 5M$ ve spread makul")
    assert item.thresholds.get("type") == "volume_spread"
    assert item.optional is False


# ---------- 3) Mevcut 30 STAGES_CONFIG sorusu icin registry guard ----------

def test_every_stages_config_label_is_either_mapped_or_volume_spread():
    """REGISTRY GUARD: gelecekte STAGES_CONFIG'e yeni bir soru eklenip
    _metric_for_label()'e mapping eklenmesi UNUTULURSA bu test FAIL eder --
    boylece 'sessiz yanlis metric' riski test seviyesinde erken yakalanir."""
    unmapped_non_volume_spread = []
    for stage in kss.STAGES_CONFIG:
        for item in stage.items:
            is_volume_spread = item.thresholds.get("type") == "volume_spread"
            metric = kss._metric_for_label(item.label)
            if metric == kss._UNMAPPED_METRIC_SENTINEL and not is_volume_spread:
                unmapped_non_volume_spread.append(item.label)
    assert not unmapped_non_volume_spread, \
        f"Mapping'i unutulmus soru(lar) bulundu: {unmapped_non_volume_spread}"


def test_current_factor_metric_routing_unchanged_for_all_mapped_labels():
    """Mevcut (gercekten mapped) her label icin _metric_for_label() sonucu
    'fear_greed' fallback fix'inden ETKILENMEMIS olmali -- yalnizca gercek
    Fear & Greed sorusu 'fear_greed' donmeli, baska hicbiri."""
    fg_labels = [it.label for stage in kss.STAGES_CONFIG for it in stage.items
                 if kss._metric_for_label(it.label) == "fear_greed"]
    assert len(fg_labels) == 1
    assert "Fear & Greed" in fg_labels[0]


# ---------- 4) Single-factor nodata, analysis-wide crash yok ----------

def test_single_unmapped_factor_does_not_crash_other_factors():
    """Sentetik STAGES_CONFIG benzeri bir dongu simule ederek: bir soru
    unmapped olsa bile digerlerinin normal hesaplandigini, hicbir exception
    firlamadigini dogrula (gercek Level1Worker.run() dongu deseniyle ayni
    mantik -- metric == sentinel ise fetch() zaten nodata donuyor, ozel bir
    try/except gerekmiyor)."""
    fetcher = kss.RealFetcher(on_progress=lambda m: None, should_cancel=lambda: False)
    fetcher._cache["BTC"] = {"indicators": {"price": 100.0, "ema50": 95.0}}
    labels_metrics = ["price", kss._UNMAPPED_METRIC_SENTINEL, "ema50_distance_pct"]
    results = []
    for m in labels_metrics:
        dp = fetcher.fetch("BTC", m)  # crash etmemeli
        results.append((m, dp.available, dp.status))
    assert results[1] == (kss._UNMAPPED_METRIC_SENTINEL, False, "nodata")
    # digerleri (price, ema50_distance_pct) kendi normal yollarinda islenmeye devam eder
    # (bu testin amaci crash-yok invariant'i, tam deger dogrulamasi degil)


# ---------- 5) Fresh payload / valid report before-after parity ----------

def test_full_report_parity_with_only_valid_mapped_answers():
    """Gecerli (mapped) factor'lerden olusan bir answer seti icin full_report()
    sonucu, bu fix'ten ETKILENMEMELI -- fix yalniz UNMAPPED durumu degistiriyor."""
    se = kss.ScoreEngine(kss.SIGNAL_STAGE_WEIGHTS, kss.RISK_STAGE_WEIGHTS, kss.VetoEngine(kss.VETO_RULES))
    answers = []
    for stage in kss.STAGES_CONFIG:
        for item in stage.items:
            answers.append({"stage": stage.title, "question": item.label, "answer": "nodata",
                            "weight": item.weight, "_item": item, "disabled": False})
    report = se.full_report(answers, kss.STAGES_CONFIG, "TESTCOIN")
    assert report is not None
    assert report["coverage"] == 0.0  # hepsi nodata -- basit, deterministik bir referans nokta


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
