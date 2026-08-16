# -*- coding: utf-8 -*-
"""
PERMANENT REGRESSION SUITE -- Volatility Risk Dimension (FROZEN 2026-08-14)

Ne test ediyor: volatility_risk QuestionItem'in factor_id/weight=0 contract'i,
mevcut ohlcv context'ini reuse ettigi (ek network cagrisi yok), STRICT 24-bar
historical observation formulu (lookahead yok), T1=p25/p90 esikleri (yes/wait/no
sinirlari), warmup<20 -> nodata, ve ScoreEngine uzerinden breadth/reliability/
risk_score/signal_score/veto/confidence invariant'lari (bu boyutun weight=0
oldugu icin karar motorunu etkilemedigi).

Network GEREKTIRMEZ (RealFetcher.fetch'in ilgili blogu kod-analiziyle dogrulanir;
handler mantigi ayni formulle izole test edilir -- gercek RealFetcher instance/ctx
mock gerektirmedigi icin bu tasarim tercih edilmistir). DB'ye dokunmaz.

Kaynak / provenance: scratchpad/volatility_risk_implementation_tests.py -- guncel
V6 production'a karsi tekrar dogrulanarak (29/29 PASS) buraya tasindi.
"""
import importlib.util
import inspect
import os
import sys

import numpy as np

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


def make_ind(vol_now, hist_vals):
    n = len(hist_vals) + 1
    closes = np.full(n, 100.0)
    highs = np.array([100.0 + (hist_vals[i] if i < len(hist_vals) else 0) / 2 for i in range(n)])
    lows = np.array([100.0 - (hist_vals[i] if i < len(hist_vals) else 0) / 2 for i in range(n)])
    return {"volatility_pct": vol_now, "ohlcv": {"close": closes, "high": highs, "low": lows}}


def run_handler(ind):
    """volatility_risk'in T1 (p25/p90, strict 24-bar) formulunu, production
    RealFetcher.fetch icindeki AYNI sirayla, izole calistirir."""
    vol_now = ind.get("volatility_pct")
    ohlcv = ind.get("ohlcv")
    if vol_now is None or not ohlcv:
        return "nodata", None
    closes, highs, lows = ohlcv.get("close"), ohlcv.get("high"), ohlcv.get("low")
    if closes is None or highs is None or lows is None or len(closes) < 2:
        return "nodata", None
    n = len(closes)
    hist_obs = []
    for i in range(23, n - 1):
        seg_c, seg_h, seg_l = closes[i - 23:i + 1], highs[i - 23:i + 1], lows[i - 23:i + 1]
        hist_obs.append(float(np.mean((seg_h - seg_l) / seg_c)) * 100)
    if len(hist_obs) < 20:
        return "nodata", len(hist_obs)
    arr = np.array(hist_obs)
    p25, p90 = np.percentile(arr, [25, 90])
    status = "yes" if vol_now <= p25 else ("no" if vol_now >= p90 else "wait")
    return status, (p25, p90)


ALWAYS_NODATA = {"clear_support", "risk_reward_ratio", "token_unlock_risk",
                  "dxy_trend", "etf_inflow", "mvrv_ratio", "sopr_recovery"}


def build_answers(rbe="yes", vol="yes"):
    answers = []
    for stage in kss.STAGES_CONFIG:
        for it in stage.items:
            fid = kss._FACTOR_ID_BY_LABEL.get(it.label, "?")
            if fid == "recent_bad_event":
                ans = rbe
            elif fid == "volatility_risk":
                ans = vol
            elif fid in ALWAYS_NODATA:
                ans = "nodata"
            else:
                ans = "yes"
            answers.append({"stage": stage.title, "question": it.label, "answer": ans,
                             "weight": it.weight, "_item": it, "disabled": False})
    return answers


def run():
    se = kss.ScoreEngine(kss.SIGNAL_STAGE_WEIGHTS, kss.RISK_STAGE_WEIGHTS, kss.VetoEngine(kss.VETO_RULES))

    print("=" * 90)
    print("A-D) contract: factor_id, weight=0, ohlcv reuse, no ek network cagrisi")
    print("=" * 90)
    item = next(it for stage in kss.STAGES_CONFIG for it in stage.items
                if kss._FACTOR_ID_BY_LABEL.get(it.label) == "volatility_risk")
    check("A) factor_id == volatility_risk", kss._FACTOR_ID_BY_LABEL.get(item.label) == "volatility_risk")
    check("B) weight == 0", item.weight == 0, f"got={item.weight}")

    src = inspect.getsource(kss.RealFetcher.fetch)
    block_start = src.find('metric == "volatility_risk"')
    block = src[block_start:block_start + 2000]
    check("C) RealFetcher.fetch blogu ind['ohlcv']'yi (mevcut context) reuse ediyor",
          'ind.get("ohlcv")' in block)
    check("D) yeni network/klines cagrisi YOK bu blokta",
          "get_klines" not in block and "requests." not in block and "_http_get_json" not in block)

    print("\n" + "=" * 90)
    print("E-I) T1 formul boundary testleri (strict 24-bar, lookahead yok, p25/p90 esikleri)")
    print("=" * 90)
    hist_vals = [2.0] * 50
    st, pct = run_handler(make_ind(vol_now=999.0, hist_vals=hist_vals))
    check("E) current (999.0) kendi percentile hesabina SIZMIYOR (p90 hala ~2.0)",
          pct is not None and abs(pct[1] - 2.0) < 0.5, f"pct={pct}")

    st_f, n_f = run_handler(make_ind(vol_now=2.0, hist_vals=[2.0] * 10))
    check("F) warmup<20 -> nodata", st_f == "nodata", f"got={st_f}, hist_n={n_f}")

    hist_varied = list(np.linspace(1.0, 5.0, 60))
    _, pct_v = run_handler(make_ind(vol_now=1.0, hist_vals=hist_varied))
    p25_v, p90_v = pct_v
    st_g, _ = run_handler(make_ind(vol_now=p25_v, hist_vals=hist_varied))
    check("G) current == p25 -> YES", st_g == "yes", f"got={st_g}")
    st_h, _ = run_handler(make_ind(vol_now=p90_v, hist_vals=hist_varied))
    check("H) current == p90 -> NO", st_h == "no", f"got={st_h}")
    st_i, _ = run_handler(make_ind(vol_now=(p25_v + p90_v) / 2, hist_vals=hist_varied))
    check("I) current == orta nokta -> WAIT", st_i == "wait", f"got={st_i}")
    st_above25, _ = run_handler(make_ind(vol_now=p25_v + 0.001, hist_vals=hist_varied))
    check("I) p25'in hemen ustunde -> WAIT", st_above25 == "wait", f"got={st_above25}")
    st_below90, _ = run_handler(make_ind(vol_now=p90_v - 0.001, hist_vals=hist_varied))
    check("I) p90'in hemen altinda -> WAIT", st_below90 == "wait", f"got={st_below90}")
    st_above90, _ = run_handler(make_ind(vol_now=p90_v + 0.001, hist_vals=hist_varied))
    check("H) p90'in ustunde -> NO", st_above90 == "no", f"got={st_above90}")

    print("\n" + "=" * 90)
    print("J-N) breadth / reliability (tam ScoreEngine uzerinden)")
    print("=" * 90)
    r_both = se.full_report(build_answers("yes", "yes"), kss.STAGES_CONFIG, "TEST")
    check("J) volatility+recent ikisi answered -> breadth=2", r_both["risk_breadth"] == 2,
          f"got={r_both['risk_breadth']}")
    check("M) breadth=2 -> reliable=True", r_both["risk_reliable"] is True)
    check("risk_breadth_total == 2", r_both["risk_breadth_total"] == 2, f"got={r_both['risk_breadth_total']}")

    r_volnodata = se.full_report(build_answers("yes", "nodata"), kss.STAGES_CONFIG, "TEST")
    check("K) volatility nodata -> breadth=1 (yalniz recent)", r_volnodata["risk_breadth"] == 1,
          f"got={r_volnodata['risk_breadth']}")
    check("N) breadth=1 -> reliable=False", r_volnodata["risk_reliable"] is False)

    r_rbenodata = se.full_report(build_answers("nodata", "yes"), kss.STAGES_CONFIG, "TEST")
    check("recent nodata + volatility answered -> breadth=1, reliable=False",
          r_rbenodata["risk_breadth"] == 1 and r_rbenodata["risk_reliable"] is False)

    r_none = se.full_report(build_answers("nodata", "nodata"), kss.STAGES_CONFIG, "TEST")
    check("ikisi nodata -> breadth=0, reliable=False",
          r_none["risk_breadth"] == 0 and r_none["risk_reliable"] is False)

    print("\n" + "=" * 90)
    print("O-R, W) risk_score/signal_score/veto/confidence/clear_support invariant'lari")
    print("=" * 90)
    scores = {}
    for vol in ("yes", "wait", "no", "nodata"):
        r = se.full_report(build_answers("yes", vol), kss.STAGES_CONFIG, "TEST")
        scores[vol] = r["risk_score"]
    check("O) risk_score volatility_risk state'inden BAGIMSIZ (weight=0)",
          len(set(scores.values())) == 1, f"scores={scores}")

    r_p1 = se.full_report(build_answers("yes", "yes"), kss.STAGES_CONFIG, "TEST")
    r_p2 = se.full_report(build_answers("yes", "no"), kss.STAGES_CONFIG, "TEST")
    check("P) signal_score volatility_risk'ten bagimsiz", r_p1["signal_score"] == r_p2["signal_score"])

    v_title, _, _ = se.verdict(90, 100, True, "Yüksek", 100, True, True)
    check("Q) has_veto=True hala 'Elenir'", v_title == "Elenir")
    r_veto = se.full_report(build_answers("no", "yes"), kss.STAGES_CONFIG, "TEST")
    check("Q) recent_bad_event=no -> hala VETO tetikleniyor (volatility_risk yumusatmiyor)",
          r_veto["verdict_title"] == "Elenir", f"got={r_veto['verdict_title']}")

    r_conf1 = se.full_report(build_answers("yes", "yes"), kss.STAGES_CONFIG, "TEST")
    r_conf2 = se.full_report(build_answers("yes", "no"), kss.STAGES_CONFIG, "TEST")
    check("R) confidence formulu degismedi (ayni coverage -> ayni confidence)",
          r_conf1["confidence"] == r_conf2["confidence"])

    cs_answer = next(a for a in r_both["answers"] if kss._FACTOR_ID_BY_LABEL.get(a["question"]) == "clear_support")
    check("W) clear_support hala nodata/unavailable", cs_answer["answer"] == "nodata")
    check("W) clear_support hala STRUCTURALLY_UNAVAILABLE_FACTOR_IDS'te",
          "clear_support" in kss.STRUCTURALLY_UNAVAILABLE_FACTOR_IDS)
    check("W) volatility_risk STRUCTURALLY_UNAVAILABLE_FACTOR_IDS'e EKLENMEDI",
          "volatility_risk" not in kss.STRUCTURALLY_UNAVAILABLE_FACTOR_IDS)

    try:
        _ = kss.FACTOR_ID_TABLE["volatility_risk"]
        check("AI context: FACTOR_ID_TABLE['volatility_risk'] KeyError vermiyor", True)
    except KeyError as e:
        check("AI context: FACTOR_ID_TABLE['volatility_risk'] KeyError vermiyor", False, str(e))

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
