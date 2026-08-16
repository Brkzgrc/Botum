# -*- coding: utf-8 -*-
"""
PERMANENT REGRESSION SUITE -- News RSS Contract (Bitcoinist removed, 6-source)

Ne test ediyor: RSS_FEEDS'in Bitcoinist icermedigini ve tam 6 kaynak oldugunu,
sources_total formulunun dinamik oldugunu (hardcoded 7 degil), classify_risk()'in
6/6-basarili+sifir-eslesme -> "yes" (erisilebilir), 5/6 -> "nodata" (fail-closed)
kritik semantigini, bad-news (items non-empty) dalinin source-count'tan bagimsiz
oldugunu (kod kaniti), ve recent_bad_event/volatility_risk downstream matrisinin
(veto/breadth/ai_mode) RSS kaynak sayisindan etkilenmedigini.

Network GEREKTIRMEZ (gercek RSS fetch/feedparser HIC cagrilmiyor -- classify_risk()
dogrudan sentetik items/meta ile test ediliyor). DB'ye dokunmaz.

Kaynak / provenance: scratchpad/bitcoinist_removal_tests.py -- guncel V6
production'a karsi tekrar dogrulanarak (9/9 PASS) buraya tasindi.
"""
import importlib.util
import inspect
import os
import sys

_PROD_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "kripto_sinyal_sistemi_v6_pro_gui.pyw")
spec = importlib.util.spec_from_file_location('kss', _PROD_FILE)
kss = importlib.util.module_from_spec(spec)
sys.modules['kss'] = kss
spec.loader.exec_module(kss)

EXPECTED_SOURCES = ["CoinDesk", "CoinTelegraph", "The Block", "Decrypt", "CryptoSlate", "Blockworks"]


def test_bitcoinist_removed_and_six_sources():
    nrf = kss.NewsRiskFetcher()
    names = [n for n, _ in nrf.RSS_FEEDS]
    assert "Bitcoinist" not in names, f"names={names}"
    assert len(nrf.RSS_FEEDS) == 6, f"got={len(nrf.RSS_FEEDS)}"
    assert names == EXPECTED_SOURCES, f"names={names}"


def test_sources_total_is_dynamic_not_hardcoded():
    nrf = kss.NewsRiskFetcher()
    fetch_src = inspect.getsource(nrf.fetch_relevant_news)
    assert "len(self.RSS_FEEDS)" in fetch_src, \
        "sources_total artik RSS_FEEDS'ten dinamik turetilmiyor olabilir (hardcoded sayi riski)"


def test_full_source_success_zero_match_yields_yes():
    nrf = kss.NewsRiskFetcher()
    r_full = nrf.classify_risk("BTC", "bitcoin", [], meta={"sources_ok": 6, "sources_total": 6})
    assert r_full["status"] == "yes", f"got={r_full}"


def test_partial_source_failure_zero_match_yields_nodata():
    nrf = kss.NewsRiskFetcher()
    r_partial = nrf.classify_risk("BTC", "bitcoin", [], meta={"sources_ok": 5, "sources_total": 6})
    assert r_partial["status"] == "nodata", f"got={r_partial}"


def test_bad_news_branch_independent_of_source_count():
    """items non-empty (gercek/relevant haber var) dalinda kod akisi sources_ok/
    sources_total/RSS_FEEDS'e HIC referans vermemeli -- yalniz Anthropic cagrisina
    gitmeli, kaynak sayisindan bagimsiz olmali."""
    nrf = kss.NewsRiskFetcher()
    classify_src = inspect.getsource(nrf.classify_risk)
    tail_after_zero_match_branch = classify_src[classify_src.find("if not ANTHROPIC_API_KEY"):]
    assert "sources_ok" not in tail_after_zero_match_branch
    assert "RSS_FEEDS" not in tail_after_zero_match_branch


def _build_answers(rbe, vol):
    ALWAYS_NODATA = {"clear_support", "risk_reward_ratio", "token_unlock_risk", "dxy_trend", "etf_inflow"}
    answers = []
    for stage in kss.STAGES_CONFIG:
        for it in stage.items:
            fid = kss._FACTOR_ID_BY_LABEL.get(it.label, "?")
            if fid == "recent_bad_event":
                ans = rbe
            elif fid == "volatility_risk":
                ans = vol
            elif fid in ALWAYS_NODATA or fid in ("whale_accumulation", "stablecoin_inflow", "mvrv_ratio", "sopr_recovery"):
                ans = "nodata"
            else:
                ans = "yes"
            answers.append({"stage": stage.title, "question": it.label, "answer": ans,
                            "weight": it.weight, "_item": it, "disabled": False})
    return answers


def test_recent_bad_event_no_always_vetoes_regardless_of_volatility():
    se = kss.ScoreEngine(kss.SIGNAL_STAGE_WEIGHTS, kss.RISK_STAGE_WEIGHTS, kss.VetoEngine(kss.VETO_RULES))
    for vol in ("yes", "wait", "no", "nodata"):
        a = _build_answers("no", vol)
        r = se.full_report(a, kss.STAGES_CONFIG, "TESTCOIN")
        assert r["vetos"], f"vol={vol}: veto tetiklenmedi, got vetos={r['vetos']}"


def test_risk_breadth_total_independent_of_rss_source_count():
    se = kss.ScoreEngine(kss.SIGNAL_STAGE_WEIGHTS, kss.RISK_STAGE_WEIGHTS, kss.VetoEngine(kss.VETO_RULES))
    for rbe in ("yes", "wait", "no", "nodata"):
        for vol in ("yes", "wait", "no", "nodata"):
            a = _build_answers(rbe, vol)
            r = se.full_report(a, kss.STAGES_CONFIG, "TESTCOIN")
            assert r["risk_breadth_total"] == 2, f"(rbe={rbe},vol={vol}): got={r['risk_breadth_total']}"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
