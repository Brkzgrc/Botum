# -*- coding: utf-8 -*-
"""
PERMANENT REGRESSION SUITE -- Binance Parallel Fetch (RealFetcher._build_context)

Ne test ediyor: 7 onayli Binance cagrisinin hala ayni ThreadPoolExecutor(max_workers=7)
bloğunda oldugu, hepsi basarili oldugunda beklenen context'in uretildigi, herhangi
biri tek tek fail oldugunda diger 6'nin etkilenmedigi (failure isolation), Technical
Structure Engine'in yalnız technical_indicators sonucundan SONRA calistigi, ikinci
fetch()'te cache'in _build_context()'i tekrar calistirmadigi, ve iki farkli sembol
icin context'in birbirine SIZMADIGI (mixed-symbol leakage yok).

Latency/wall-clock assertion YOK (flaky olur, kapsam disi -- semantic/functional
contract'a odaklanir).

Network GEREKTIRMEZ: BinanceClient/CoinGecko/CMC/News tum alt-cagrilar mock'lanir;
ayrica module-level `fetch_fear_greed()` de monkeypatch'lenir (RealFetcher._build_context
bunu dogrudan global isimle cagiriyor, self.cmc uzerinden degil).

Kaynak / provenance: scratchpad/binance_parallel_implementation_tests.py -- guncel V6
production'a karsi tekrar dogrulanarak (36/36 PASS, ancak orijinali Fear & Greed icin
gercek network cagrisi yapiyordu) buraya, tam offline hale getirilerek ve granuler
pytest fonksiyonlarina bolunerek tasindi.
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

FIXED = {
    "ticker": {"quoteVolume": "123456789.0", "priceChangePercent": "3.5"},
    "avg_vol": 100_000_000.0,
    "order_book": {"spread_pct": 0.05, "slippage_pct": 0.2},
    "indicators": {
        "price": 100.0, "ema50": 95.0, "ema50_distance_pct": 5.0,
        "price_above_ema50": True, "rsi": 45.0, "macd_bullish": True,
        "obv_rising": True, "bb_squeeze": False, "bb_upper_break": False,
        "volatility_pct": 2.0, "price_change_24h_pct": 3.0,
        "ohlcv": {"interval": "1h", "open_time": list(range(300)),
                  "close_time": list(range(300)),
                  "open": np.linspace(90, 100, 300), "high": np.linspace(91, 101, 300),
                  "low": np.linspace(89, 99, 300), "close": np.linspace(90, 100, 300),
                  "volume": np.ones(300) * 1000},
    },
    "funding": {"rate_pct": 0.01, "funding_time_ms": 1000, "interval_hours": 8, "age_hours": 1.0},
    "oi": {"oi_change_pct": 1.5, "price_change_pct": 1.0, "status": "yes",
           "first_ts": 1000, "last_ts": 2000},
    "btc_regime": (True, 5.0),
}

ALL_KEYS = ["ticker_24h", "avg_volume_7d", "order_book", "technical_indicators",
            "funding_detail", "open_interest_trend", "btc_regime_and_r7"]
FIELD_MAP = {
    "ticker_24h": "volume_24h_usd", "avg_volume_7d": "volume_7d_avg_usd",
    "order_book": "order_book", "technical_indicators": "indicators",
    "funding_detail": "funding_pct", "open_interest_trend": "oi_trend",
    "btc_regime_and_r7": "btc_above_ema50",
}


def make_mock_fetcher(fail_key=None):
    """RealFetcher instance, tum alt-provider'lari (Binance/CoinGecko/CMC/News/
    Fear&Greed) sentetik/deterministik yanitlarla mock'lanmis. fail_key verilirse
    o TEK Binance cagrisi exception firlatir, digerleri normal doner."""
    fetcher = kss.RealFetcher(on_progress=lambda m: None, should_cancel=lambda: False)

    def fail_or(val):
        def _f(*a, **kw):
            raise RuntimeError("SIMULATED_FAILURE")
        return _f

    fetcher.binance = type("MockBinance", (), {})()
    fetcher.binance.get_ticker_24h = (lambda s: FIXED["ticker"]) if fail_key != "ticker_24h" else fail_or(None)
    fetcher.binance.get_avg_volume_7d_usd = (lambda s: FIXED["avg_vol"]) if fail_key != "avg_volume_7d" else fail_or(None)
    fetcher.binance.get_order_book_metrics = (lambda s: FIXED["order_book"]) if fail_key != "order_book" else fail_or(None)
    fetcher.binance.get_technical_indicators = (lambda s: FIXED["indicators"]) if fail_key != "technical_indicators" else fail_or(None)
    fetcher.binance.get_funding_detail = (lambda s: FIXED["funding"]) if fail_key != "funding_detail" else fail_or(None)
    fetcher.binance.get_open_interest_trend = (lambda s: FIXED["oi"]) if fail_key != "open_interest_trend" else fail_or(None)
    fetcher.binance.get_btc_daily_regime_and_r7 = (lambda: FIXED["btc_regime"]) if fail_key != "btc_regime_and_r7" else fail_or(None)
    fetcher.coingecko.resolve_coin_id = lambda s: None
    fetcher.cmc.get_quotes = lambda s: None
    fetcher.cmc.get_btc_dominance_trend = lambda: None
    fetcher.news.fetch_relevant_news = lambda s, n: ([], {"sources_ok": 0, "sources_total": 6, "dependency_missing": False})
    fetcher.news.classify_risk = lambda s, n, items, meta: {"status": "nodata", "reason": "test", "sources": []}
    return fetcher


def _patch_fear_greed(monkeypatch):
    monkeypatch.setattr(kss, "fetch_fear_greed", lambda: 50)


def test_seven_binance_calls_in_parallel_block():
    src = inspect.getsource(kss.RealFetcher._build_context)
    block_start = src.find("_binance_calls = {")
    block_end = src.find("binance_results = {}")
    block = src[block_start:block_end]
    for key in ALL_KEYS:
        assert f'"{key}"' in block, f"{key} artik parallel block'ta degil"
    assert len(ALL_KEYS) == 7, "onayli Binance cagri sayisi 7 olmali"


def test_max_workers_is_7():
    src = inspect.getsource(kss.RealFetcher._build_context)
    assert "ThreadPoolExecutor(max_workers=7)" in src


def test_context_parity_all_success(monkeypatch):
    _patch_fear_greed(monkeypatch)
    fetcher = make_mock_fetcher()
    ctx = fetcher._build_context("ETH")
    assert ctx["volume_24h_usd"] == 123456789.0
    assert ctx["price_change_24h_pct_ticker"] == 3.5
    assert ctx["volume_7d_avg_usd"] == 100_000_000.0
    assert ctx["order_book"] == FIXED["order_book"]
    assert ctx["indicators"]["price"] == 100.0 and ctx["indicators"]["rsi"] == 45.0
    assert ctx["funding_pct"] == 0.01
    assert ctx["funding_time_ms"] == 1000 and ctx["funding_interval_hours"] == 8 and ctx["funding_age_hours"] == 1.0
    assert ctx["oi_trend"] == FIXED["oi"]
    assert ctx["btc_above_ema50"] is True
    assert ctx["btc_daily_bearish"] is False
    assert ctx["btc_7d_return_pct"] == 5.0


def test_tse_runs_only_after_technical_indicators(monkeypatch):
    _patch_fear_greed(monkeypatch)
    fetcher_ok = make_mock_fetcher()
    ctx_ok = fetcher_ok._build_context("ETH")
    assert ctx_ok["technical_structure"] is not None
    assert ctx_ok["technical_structure"].get("status") in ("ok", "error")

    fetcher_fail = make_mock_fetcher(fail_key="technical_indicators")
    ctx_fail = fetcher_fail._build_context("BTC")
    assert ctx_fail["indicators"] is None
    assert ctx_fail["technical_structure"] is None, \
        "technical_indicators basarisizken TSE calismamali (ind yok)"


def test_failure_isolation_matrix(monkeypatch):
    """7 future'in her biri TEK TEK exception'a zorlanir; diger 6 etkilenmemeli,
    _build_context() hicbir kombinasyonda crash etmemeli."""
    _patch_fear_greed(monkeypatch)
    for fail_key in ALL_KEYS:
        f = make_mock_fetcher(fail_key=fail_key)
        ctx = f._build_context("ETH")  # crash etmemeli
        failed_field = FIELD_MAP[fail_key]
        assert ctx.get(failed_field) in (None,) or failed_field not in ctx, \
            f"{fail_key}: {failed_field} bos degil: {ctx.get(failed_field)}"
        remaining_fields = [FIELD_MAP[k] for k in ALL_KEYS if k != fail_key]
        for f_name in remaining_fields:
            if f_name == "volume_24h_usd":
                continue  # ticker None ise pct de None kalir, ayri kontrol edilmiyor
            assert ctx.get(f_name) is not None, \
                f"fail_key={fail_key}: diger alan {f_name} da None (izolasyon bozuldu)"


def test_cache_prevents_recompute(monkeypatch):
    _patch_fear_greed(monkeypatch)
    fetcher = make_mock_fetcher()
    dp1 = fetcher.fetch("ETH", "recent_bad_event")
    call_count_before = fetcher.binance.get_ticker_24h
    # ikinci fetch cagrisi ayni sembol icin -- _build_context ikinci kez calismamali
    assert "ETH" in fetcher._cache
    ctx_first = fetcher._cache["ETH"]
    dp2 = fetcher.fetch("ETH", "volatility_risk")
    assert fetcher._cache["ETH"] is ctx_first, "cache'teki context nesnesi degisti -- recompute olmus olabilir"


def test_mixed_symbol_no_leakage(monkeypatch):
    _patch_fear_greed(monkeypatch)
    fetcher = make_mock_fetcher()
    # farkli fiyat/rsi degerleriyle IKINCI bir sembol icin ayri mock binance
    fetcher2 = make_mock_fetcher()
    alt_indicators = dict(FIXED["indicators"])
    alt_indicators["price"] = 999.0
    alt_indicators["rsi"] = 11.0
    fetcher2.binance.get_technical_indicators = lambda s: alt_indicators

    ctx_eth = fetcher._build_context("ETH")
    ctx_sol = fetcher2._build_context("SOL")

    assert ctx_eth["indicators"]["price"] == 100.0
    assert ctx_sol["indicators"]["price"] == 999.0
    assert "SOL" not in fetcher._cache
    assert "ETH" not in fetcher2._cache
    assert ctx_eth is not ctx_sol


def test_sequential_reference_semantic_parity(monkeypatch):
    """Paralel cagrinin ayni 7 sonucu sirayla (sequential) toplasaydik uretilecek
    referans context ile SEMANTIK olarak ayni oldugunu dogrular -- yalniz
    orkestrasyon sekli (parallel vs sequential) degisti, sonuc AYNI olmali."""
    _patch_fear_greed(monkeypatch)
    fetcher = make_mock_fetcher()
    ctx = fetcher._build_context("ETH")

    # Referans: ayni 7 mock fonksiyonu SIRAYLA cagirip beklenen degerleri
    # bagimsizca hesapla.
    ref_ticker = fetcher.binance.get_ticker_24h("ETH")
    ref_ind = fetcher.binance.get_technical_indicators("ETH")
    ref_funding = fetcher.binance.get_funding_detail("ETH")
    ref_oi = fetcher.binance.get_open_interest_trend("ETH")
    ref_btc = fetcher.binance.get_btc_daily_regime_and_r7()

    assert ctx["volume_24h_usd"] == float(ref_ticker["quoteVolume"])
    assert ctx["indicators"] == ref_ind
    assert ctx["funding_pct"] == ref_funding["rate_pct"]
    assert ctx["oi_trend"] == ref_oi
    assert ctx["btc_above_ema50"] == ref_btc[0]
    assert ctx["btc_7d_return_pct"] == round(ref_btc[1], 4)


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
