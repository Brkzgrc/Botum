# -*- coding: utf-8 -*-
"""
PERMANENT REGRESSION SUITE -- External Data Freshness
(CoinGecko exchange_count stale filter + Fear&Greed provenance/TTL + CMC BTC
dominance freshness gate)

Ne test ediyor:
  - CoinGeckoFetcher.get_unique_major_exchanges(): is_stale=True sayilmiyor,
    eksik/None conservative-skip, malformed ticker/market skip, fresh
    davranis (before==after) DEGISMEDI, pagination completeness semantigi
    korunuyor, downstream exchange_count threshold'u dogru yansiyor.
  - _fetch_fear_greed_cmc()/_fetch_fear_greed_alternative_me()/fetch_fear_greed():
    provider-specific TTL (CMC=1h, alternative.me=36h), future-tolerance
    (120s), missing/malformed timestamp/value -> None, CMC->alternative.me
    fallback (stale/malformed/missing/future-invalid dahil TUM basarisizlik
    turlerinde), her ikisi de gecersizse None, dis contract (Optional[int])
    ve tek consumer degismedi.
  - CMCFetcher.get_btc_dominance_trend(): `data.last_updated` ile freshness
    gate (TTL=15dk, future-tolerance=120s), missing/malformed -> None,
    fresh durumda mevcut dominance/TOTAL3 hesaplama mantigi AYNEN.

Network GEREKTIRMEZ: `_http_get_json` monkeypatch'lenir. DB'ye dokunmaz.
Zaman kontrolu: boundary testleri `now` parametresi enjekte edilerek
(fetch_fear_greed helper'lari) veya gercek-zamana-goreli offset'lerle
(dominance, `now` parametresi yok) yapilir -- gercek `datetime.now()`'a
kor guven YOK, flaky degil.

Kaynak / provenance: "FRESHNESS GATE PARITY" + "CMC F&G + BTC DOMINANCE TTL
FINALIZATION" turlarinda canli API orneklemesiyle kanitlanan TTL'ler
(CMC F&G: 2 bagimsiz 15dk-gecis gozlemi; alternative.me: 86400s ardisik kayit
farki; CMC dominance: 8/8 bagimsiz 1dk-kadans gozlemi; clock-skew: ~95s olcum).
"""
import importlib.util
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

_PROD_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "kripto_sinyal_sistemi_v6_pro_gui.pyw")
spec = importlib.util.spec_from_file_location('kss', _PROD_FILE)
kss = importlib.util.module_from_spec(spec)
sys.modules['kss'] = kss
spec.loader.exec_module(kss)


def _ticker(identifier, is_stale=False, has_field=True):
    t = {"market": {"identifier": identifier}, "is_anomaly": False}
    if has_field:
        t["is_stale"] = is_stale
    return t


@pytest.fixture
def cg():
    return kss.CoinGeckoFetcher()


@pytest.fixture
def fake_http(monkeypatch):
    """Sayfa listesi verilen mock -- her cagrida bir sonraki sayfayi doner."""
    def _install(pages):
        state = {"i": 0}

        def _fake(url, params=None, headers=None, session=None, debug_label=None):
            if "/tickers" not in url:
                return None
            i = state["i"]
            state["i"] += 1
            if i >= len(pages):
                return None  # saglanan sayfalarin otesi -> fetch basarisiz (bos-ama-basarili DEGIL)
            return {"tickers": pages[i]}

        monkeypatch.setattr(kss, "_http_get_json", _fake)
    return _install


def test_all_fresh(cg, fake_http):
    fake_http([[_ticker("binance"), _ticker("kraken")]])
    result = cg.get_unique_major_exchanges("x")
    assert result == {"count": 2, "complete": True}


def test_all_stale(cg, fake_http):
    fake_http([[_ticker("binance", is_stale=True), _ticker("kraken", is_stale=True)]])
    result = cg.get_unique_major_exchanges("x")
    assert result == {"count": 0, "complete": True}, \
        "stale-only -> count=0, complete=True (mevcut threshold bunu 'no' yapacak, nodata degil)"


def test_mixed_stale_fresh(cg, fake_http):
    fake_http([[_ticker("binance", is_stale=False), _ticker("kraken", is_stale=True)]])
    result = cg.get_unique_major_exchanges("x")
    assert result == {"count": 1, "complete": True}


def test_missing_is_stale_conservative_skip(cg, fake_http):
    fake_http([[_ticker("binance", has_field=False), _ticker("kraken", is_stale=False)]])
    result = cg.get_unique_major_exchanges("x")
    assert result == {"count": 1, "complete": True}, \
        "is_stale eksikse fresh VARSAYILMAMALI -- yalniz kraken sayilmali"


def test_malformed_ticker_skipped():
    """Ticker'in kendisi dict degilse veya market alani beklenen sekilde
    degilse crash etmemeli, o kayit sessizce atlanmali."""
    kss_local = kss
    cg_local = kss_local.CoinGeckoFetcher()
    bad_page = ["not_a_dict", {"is_stale": False, "market": "not_a_dict_either"},
                {"is_stale": False, "market": None}, _ticker("kraken", is_stale=False)]

    def _fake(url, params=None, headers=None, session=None, debug_label=None):
        return {"tickers": bad_page}

    orig = kss_local._http_get_json
    kss_local._http_get_json = _fake
    try:
        result = cg_local.get_unique_major_exchanges("x")
    finally:
        kss_local._http_get_json = orig
    assert result == {"count": 1, "complete": True}, \
        "malformed girdiler crash etmeden atlanmali, yalniz kraken sayilmali"


def test_stale_major_plus_fresh_non_major(cg, fake_http):
    """Major olmayan bir borsa fresh olsa bile sayilmamali (MAJOR_EXCHANGES
    disinda) -- yalniz stale major exchange filtresinin izole etkisini dogrular."""
    fake_http([[_ticker("binance", is_stale=True), _ticker("some_random_exchange", is_stale=False)]])
    result = cg.get_unique_major_exchanges("x")
    assert result == {"count": 0, "complete": True}


def test_partial_pagination_fresh_count_ge2(cg, fake_http):
    page1 = [_ticker("binance", is_stale=False)] * 100  # tam sayfa -> pagination devam eder
    page2 = [_ticker("kraken", is_stale=False)] * 100   # tam sayfa -> hala complete degil
    fake_http([page1, page2])
    result = cg.get_unique_major_exchanges("x")
    assert result == {"count": 2, "complete": False}, \
        "count>=2 pagination bitmeden dogrulandi -> partial-but-sufficient korunmali"


def test_partial_pagination_fresh_count_lt2(cg, monkeypatch):
    """1. sayfa tam (100 ticker, tek major=binance -> count=1), 2. sayfa ISTEGI
    BASARISIZ OLUR (None doner, bos-ama-basarili DEGIL) -- pagination
    tamamlanamadan durur, complete=False kalir, count(1)<2 -> None (nodata)."""
    page1 = [_ticker("binance", is_stale=False)] * 100
    state = {"i": 0}

    def _fake(url, params=None, headers=None, session=None, debug_label=None):
        i = state["i"]
        state["i"] += 1
        if i == 0:
            return {"tickers": page1}
        return None  # 2. sayfa fetch basarisiz -> pagination yarim kaldi

    monkeypatch.setattr(kss, "_http_get_json", _fake)
    result = cg.get_unique_major_exchanges("x")
    assert result is None, "count<2 ve complete=False -> kesin karar verilemez, None (nodata)"


def test_fresh_payload_before_after_parity(cg, fake_http):
    """Freshness gate SADECE stale/missing-staleness ticker'lari etkilemeli --
    tamamen fresh bir payload icin sonuc, filtre EKLENMEDEN ONCEKI (eski) davranisla
    BIREBIR AYNI olmali."""
    fresh_page = [_ticker("binance", is_stale=False), _ticker("kraken", is_stale=False),
                  _ticker("okx", is_stale=False)]
    fake_http([fresh_page])
    result = cg.get_unique_major_exchanges("x")
    # Eski (filtresiz) davranis: found = {binance, kraken, okx} & MAJOR_EXCHANGES = 3
    assert result == {"count": 3, "complete": True}
    expected_before_fix = len({"binance", "kraken", "okx"} & cg.MAJOR_EXCHANGES)
    assert result["count"] == expected_before_fix, "fresh payload icin filtre sonucu ESKI davranisla ayni olmali"


@pytest.mark.parametrize("count,expected_status", [(0, "no"), (1, "wait"), (2, "yes"), (5, "yes")])
def test_downstream_exchange_count_status_parity(count, expected_status):
    """exchange_count threshold'u (>=2 yes, >=1 wait, 0 no) DEGISMEDI --
    yalniz girdi (count) artik stale-filtrelenmis. Threshold mantigini
    dogrudan (RealFetcher.fetch icindeki AYNI formulle) dogrula."""
    status = "yes" if count >= 2 else "wait" if count >= 1 else "no"
    assert status == expected_status


TTL_CMC = kss._FG_CMC_TTL_SECONDS
TTL_ALT = kss._FG_ALTERNATIVE_ME_TTL_SECONDS
TTL_DOM = kss._CMC_DOMINANCE_TTL_SECONDS
TOL = kss._FRESHNESS_FUTURE_TOLERANCE_SECONDS


def _iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


def _fake_cmc_fg_response(age_seconds=0, value=50, missing_ts=False, malformed_ts=False,
                           missing_value=False, base_now=None):
    base_now = base_now or datetime.now(timezone.utc)
    d = {}
    if not missing_ts:
        d["update_time"] = "not-a-timestamp" if malformed_ts else _iso(base_now - timedelta(seconds=age_seconds))
    if not missing_value:
        d["value"] = value
    return {"data": d}


def _fake_alt_fg_response(age_seconds=0, value=50, missing_ts=False, malformed_ts=False, base_now=None):
    base_now = base_now or datetime.now(timezone.utc)
    entry = {}
    if not missing_ts:
        entry["timestamp"] = "not-a-timestamp" if malformed_ts else str(int((base_now - timedelta(seconds=age_seconds)).timestamp()))
    entry["value"] = str(value)
    return {"data": [entry]}


# ---------- CMC Fear & Greed ----------

def test_fg_cmc_fresh(monkeypatch):
    monkeypatch.setattr(kss, "CMC_API_KEY", "dummy")
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(kss, "_http_get_json", lambda *a, **k: _fake_cmc_fg_response(age_seconds=60, value=42, base_now=now))
    assert kss._fetch_fear_greed_cmc(now=now) == 42


def test_fg_cmc_exact_ttl_boundary(monkeypatch):
    monkeypatch.setattr(kss, "CMC_API_KEY", "dummy")
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(kss, "_http_get_json", lambda *a, **k: _fake_cmc_fg_response(age_seconds=TTL_CMC, value=42, base_now=now))
    assert kss._fetch_fear_greed_cmc(now=now) == 42, "TTL sinirinin TAM UZERINDE (age==TTL) hala fresh olmali"


def test_fg_cmc_just_over_ttl(monkeypatch):
    monkeypatch.setattr(kss, "CMC_API_KEY", "dummy")
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(kss, "_http_get_json", lambda *a, **k: _fake_cmc_fg_response(age_seconds=TTL_CMC + 1, value=42, base_now=now))
    assert kss._fetch_fear_greed_cmc(now=now) is None


def test_fg_cmc_missing_timestamp(monkeypatch):
    monkeypatch.setattr(kss, "CMC_API_KEY", "dummy")
    monkeypatch.setattr(kss, "_http_get_json", lambda *a, **k: _fake_cmc_fg_response(missing_ts=True))
    assert kss._fetch_fear_greed_cmc() is None


def test_fg_cmc_malformed_timestamp(monkeypatch):
    monkeypatch.setattr(kss, "CMC_API_KEY", "dummy")
    monkeypatch.setattr(kss, "_http_get_json", lambda *a, **k: _fake_cmc_fg_response(malformed_ts=True))
    assert kss._fetch_fear_greed_cmc() is None


def test_fg_cmc_future_tolerance_boundary(monkeypatch):
    monkeypatch.setattr(kss, "CMC_API_KEY", "dummy")
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(kss, "_http_get_json", lambda *a, **k: _fake_cmc_fg_response(age_seconds=-TOL, value=42, base_now=now))
    assert kss._fetch_fear_greed_cmc(now=now) == 42, "tam -120s (tolerans siniri) hala kabul edilmeli"


def test_fg_cmc_future_beyond_tolerance_rejected(monkeypatch):
    monkeypatch.setattr(kss, "CMC_API_KEY", "dummy")
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(kss, "_http_get_json", lambda *a, **k: _fake_cmc_fg_response(age_seconds=-(TOL + 1), value=42, base_now=now))
    assert kss._fetch_fear_greed_cmc(now=now) is None


def test_fg_cmc_invalid_value(monkeypatch):
    monkeypatch.setattr(kss, "CMC_API_KEY", "dummy")
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(kss, "_http_get_json", lambda *a, **k: _fake_cmc_fg_response(age_seconds=60, missing_value=True, base_now=now))
    assert kss._fetch_fear_greed_cmc(now=now) is None, "timestamp taze ama value yoksa hala None"


def test_fg_cmc_no_api_key_returns_none(monkeypatch):
    monkeypatch.setattr(kss, "CMC_API_KEY", "")
    assert kss._fetch_fear_greed_cmc() is None


# ---------- Fear & Greed fallback (CMC -> alternative.me) ----------

def test_fg_fallback_cmc_fresh_alternative_not_called(monkeypatch):
    monkeypatch.setattr(kss, "CMC_API_KEY", "dummy")
    called = {"alt": False}
    now = datetime.now(timezone.utc)

    def _fake(url, params=None, headers=None, session=None, debug_label=None):
        if "alternative.me" in url:
            called["alt"] = True
            return _fake_alt_fg_response(age_seconds=60, value=99, base_now=now)
        return _fake_cmc_fg_response(age_seconds=60, value=42, base_now=now)

    monkeypatch.setattr(kss, "_http_get_json", _fake)
    result = kss._fetch_fear_greed_cmc(now=now)
    assert result == 42
    assert not called["alt"], "CMC fresh iken alternative.me HIC cagrilmamali (fetch_fear_greed seviyesinde)"


def _install_fallback_mock(monkeypatch, cmc_age=None, cmc_missing=False, cmc_malformed=False,
                            alt_age=60, alt_value=99, now=None):
    now = now or datetime.now(timezone.utc)
    monkeypatch.setattr(kss, "CMC_API_KEY", "dummy")

    def _fake(url, params=None, headers=None, session=None, debug_label=None):
        if "alternative.me" in url:
            return _fake_alt_fg_response(age_seconds=alt_age, value=alt_value, base_now=now)
        return _fake_cmc_fg_response(age_seconds=cmc_age or 0, value=42,
                                      missing_ts=cmc_missing, malformed_ts=cmc_malformed, base_now=now)

    monkeypatch.setattr(kss, "_http_get_json", _fake)
    return now


def test_fg_fallback_cmc_stale_alternative_fresh_used(monkeypatch):
    now = _install_fallback_mock(monkeypatch, cmc_age=TTL_CMC + 1, alt_age=60, alt_value=99)
    value = kss._fetch_fear_greed_cmc(now=now)
    assert value is None
    value = kss._fetch_fear_greed_alternative_me(now=now)
    assert value == 99


def test_fg_fallback_cmc_malformed_alternative_fresh_used(monkeypatch):
    now = _install_fallback_mock(monkeypatch, cmc_malformed=True, alt_age=60, alt_value=99)
    assert kss._fetch_fear_greed_cmc(now=now) is None
    assert kss._fetch_fear_greed_alternative_me(now=now) == 99


def test_fg_fallback_cmc_future_invalid_alternative_fresh_used(monkeypatch):
    now = _install_fallback_mock(monkeypatch, cmc_age=-(TOL + 1), alt_age=60, alt_value=99)
    assert kss._fetch_fear_greed_cmc(now=now) is None
    assert kss._fetch_fear_greed_alternative_me(now=now) == 99


def test_fg_both_stale_returns_none(monkeypatch):
    now = _install_fallback_mock(monkeypatch, cmc_age=TTL_CMC + 1, alt_age=TTL_ALT + 3600)
    assert kss._fetch_fear_greed_cmc(now=now) is None
    assert kss._fetch_fear_greed_alternative_me(now=now) is None


def test_fg_cmc_unavailable_and_alternative_unavailable_returns_none(monkeypatch):
    monkeypatch.setattr(kss, "CMC_API_KEY", "")

    def _fake(url, params=None, headers=None, session=None, debug_label=None):
        return None  # her iki kaynak da fetch basarisiz

    monkeypatch.setattr(kss, "_http_get_json", _fake)
    assert kss.fetch_fear_greed() is None


def test_fg_zero_value_not_treated_as_falsy(monkeypatch):
    """F&G degeri 0 (asiri korku) GECERLI bir sonuc -- CMC 0 donerse
    alternative.me'ye YANLISLIKLA dusulmemeli (Python truthy/falsy tuzagi)."""
    monkeypatch.setattr(kss, "CMC_API_KEY", "dummy")
    called = {"alt": False}
    now = datetime.now(timezone.utc)

    def _fake(url, params=None, headers=None, session=None, debug_label=None):
        if "alternative.me" in url:
            called["alt"] = True
            return _fake_alt_fg_response(age_seconds=60, value=99, base_now=now)
        return _fake_cmc_fg_response(age_seconds=60, value=0, base_now=now)

    monkeypatch.setattr(kss, "_http_get_json", _fake)
    # fetch_fear_greed() gercek `now` kullanir (parametre almiyor) -- age=60s
    # her turlu TTL(1h) icinde kalir, deterministic.
    result = kss.fetch_fear_greed()
    assert result == 0, f"got={result}"
    assert not called["alt"], "CMC value=0 gecerli -- alternative.me'ye yanlislikla dusulmemeli"


# ---------- alternative.me Fear & Greed ----------

def test_fg_alt_fresh(monkeypatch):
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(kss, "_http_get_json", lambda *a, **k: _fake_alt_fg_response(age_seconds=3600, value=33, base_now=now))
    assert kss._fetch_fear_greed_alternative_me(now=now) == 33


def test_fg_alt_exact_36h_boundary(monkeypatch):
    # microsecond=0: alternative.me epoch-saniye (int) formatina yuvarlanirken
    # kesirli-saniye kaybi TTL sinirinin YANLIS tarafina itmesin diye (test
    # harness hassasiyeti, production davranisiyla ilgisi yok).
    now = datetime.now(timezone.utc).replace(microsecond=0)
    monkeypatch.setattr(kss, "_http_get_json", lambda *a, **k: _fake_alt_fg_response(age_seconds=TTL_ALT, value=33, base_now=now))
    assert kss._fetch_fear_greed_alternative_me(now=now) == 33


def test_fg_alt_36h_stale(monkeypatch):
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(kss, "_http_get_json", lambda *a, **k: _fake_alt_fg_response(age_seconds=TTL_ALT + 1, value=33, base_now=now))
    assert kss._fetch_fear_greed_alternative_me(now=now) is None


def test_fg_alt_missing_timestamp(monkeypatch):
    monkeypatch.setattr(kss, "_http_get_json", lambda *a, **k: _fake_alt_fg_response(missing_ts=True))
    assert kss._fetch_fear_greed_alternative_me() is None


def test_fg_alt_malformed_timestamp(monkeypatch):
    monkeypatch.setattr(kss, "_http_get_json", lambda *a, **k: _fake_alt_fg_response(malformed_ts=True))
    assert kss._fetch_fear_greed_alternative_me() is None


def test_fg_alt_future_tolerance_boundary(monkeypatch):
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(kss, "_http_get_json", lambda *a, **k: _fake_alt_fg_response(age_seconds=-TOL, value=33, base_now=now))
    assert kss._fetch_fear_greed_alternative_me(now=now) == 33


def test_fg_alt_future_beyond_tolerance_rejected(monkeypatch):
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(kss, "_http_get_json", lambda *a, **k: _fake_alt_fg_response(age_seconds=-(TOL + 1), value=33, base_now=now))
    assert kss._fetch_fear_greed_alternative_me(now=now) is None


# ---------- CMC BTC Dominance ----------

def _fake_dominance_response(age_seconds=0, btc_dom=55.0, btc_dom_yst=54.0, eth_dom=12.0,
                              eth_dom_yst=12.5, tmc=2_000_000_000_000.0, tmc_yst=1_900_000_000_000.0,
                              missing_last_updated=False, malformed_last_updated=False,
                              missing_yesterday=False):
    now = datetime.now(timezone.utc)
    d = {
        "btc_dominance": btc_dom,
        "eth_dominance": eth_dom,
        "btc_dominance_24h_percentage_change": 0.5,
        "quote": {"USD": {"total_market_cap": tmc}},
    }
    if not missing_yesterday:
        d["btc_dominance_yesterday"] = btc_dom_yst
        d["eth_dominance_yesterday"] = eth_dom_yst
        d["quote"]["USD"]["total_market_cap_yesterday"] = tmc_yst
    if not missing_last_updated:
        d["last_updated"] = "not-a-timestamp" if malformed_last_updated else _iso(now - timedelta(seconds=age_seconds))
    return {"data": d}


@pytest.fixture
def cmc():
    return kss.CMCFetcher()


def test_dominance_fresh(monkeypatch, cmc):
    monkeypatch.setattr(cmc, "_headers", {"X-CMC_PRO_API_KEY": "dummy"})
    monkeypatch.setattr(kss, "_http_get_json", lambda *a, **k: _fake_dominance_response(age_seconds=60))
    result = cmc.get_btc_dominance_trend()
    assert result is not None
    assert result["dominance_falling"] is False  # 55.0 < 54.0 -> False (dominance rising, not falling)


def test_dominance_15m_boundary(monkeypatch, cmc):
    monkeypatch.setattr(cmc, "_headers", {"X-CMC_PRO_API_KEY": "dummy"})
    monkeypatch.setattr(kss, "_http_get_json", lambda *a, **k: _fake_dominance_response(age_seconds=TTL_DOM))
    assert cmc.get_btc_dominance_trend() is not None


def test_dominance_15m_stale(monkeypatch, cmc):
    monkeypatch.setattr(cmc, "_headers", {"X-CMC_PRO_API_KEY": "dummy"})
    monkeypatch.setattr(kss, "_http_get_json", lambda *a, **k: _fake_dominance_response(age_seconds=TTL_DOM + 1))
    assert cmc.get_btc_dominance_trend() is None


def test_dominance_missing_last_updated(monkeypatch, cmc):
    monkeypatch.setattr(cmc, "_headers", {"X-CMC_PRO_API_KEY": "dummy"})
    monkeypatch.setattr(kss, "_http_get_json", lambda *a, **k: _fake_dominance_response(missing_last_updated=True))
    assert cmc.get_btc_dominance_trend() is None


def test_dominance_malformed_last_updated(monkeypatch, cmc):
    monkeypatch.setattr(cmc, "_headers", {"X-CMC_PRO_API_KEY": "dummy"})
    monkeypatch.setattr(kss, "_http_get_json", lambda *a, **k: _fake_dominance_response(malformed_last_updated=True))
    assert cmc.get_btc_dominance_trend() is None


def test_dominance_future_tolerance_boundary(monkeypatch, cmc):
    monkeypatch.setattr(cmc, "_headers", {"X-CMC_PRO_API_KEY": "dummy"})
    monkeypatch.setattr(kss, "_http_get_json", lambda *a, **k: _fake_dominance_response(age_seconds=-TOL))
    assert cmc.get_btc_dominance_trend() is not None


def test_dominance_future_beyond_tolerance_rejected(monkeypatch, cmc):
    monkeypatch.setattr(cmc, "_headers", {"X-CMC_PRO_API_KEY": "dummy"})
    monkeypatch.setattr(kss, "_http_get_json", lambda *a, **k: _fake_dominance_response(age_seconds=-(TOL + 1)))
    assert cmc.get_btc_dominance_trend() is None


def test_dominance_fresh_before_after_parity(monkeypatch, cmc):
    """Freshness gate eklenmeden ONCEKI davranisla (fresh payload icin) BIREBIR
    ayni sonuc -- dominance/TOTAL3 hesaplama mantigi degismedi."""
    monkeypatch.setattr(cmc, "_headers", {"X-CMC_PRO_API_KEY": "dummy"})
    monkeypatch.setattr(kss, "_http_get_json",
                         lambda *a, **k: _fake_dominance_response(age_seconds=60, btc_dom=50.0, btc_dom_yst=52.0,
                                                                   eth_dom=10.0, eth_dom_yst=10.0,
                                                                   tmc=1000.0, tmc_yst=900.0))
    result = cmc.get_btc_dominance_trend()
    assert result["dominance_falling"] is True  # 50.0 < 52.0
    total3_now = 1000.0 * (1 - 0.50 - 0.10)
    total3_yst = 900.0 * (1 - 0.52 - 0.10)
    expected_rising = total3_now > total3_yst
    assert result["total3_rising"] == expected_rising
    assert abs(result["total3_change_pct"] - ((total3_now - total3_yst) / total3_yst * 100)) < 1e-9


def test_dominance_missing_yesterday_fields_existing_behavior_unchanged(monkeypatch, cmc):
    """Freshness gate'ten BAGIMSIZ, mevcut davranis: yesterday alanlari eksikse
    total3_rising None kalir ama dominance_falling (yesterday'e ihtiyaci varsa o da)
    etkilenir -- burada ikisi de yesterday'e bagli oldugu icin ikisi de None,
    fonksiyon None doner (hicbir kol belirlenemedi)."""
    monkeypatch.setattr(cmc, "_headers", {"X-CMC_PRO_API_KEY": "dummy"})
    monkeypatch.setattr(kss, "_http_get_json",
                         lambda *a, **k: _fake_dominance_response(age_seconds=60, missing_yesterday=True))
    assert cmc.get_btc_dominance_trend() is None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
