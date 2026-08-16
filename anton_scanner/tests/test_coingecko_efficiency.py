# -*- coding: utf-8 -*-
"""
PERMANENT REGRESSION SUITE -- CoinGecko Verified Coin-ID Process Cache (Model D)

Ne test ediyor: `CoinGeckoFetcher.resolve_coin_id()`'nin yeni process-level
`_verified_id_cache` katmanini -- ilk cagri (cache miss) mevcut resolver
algoritmasini (candidate siralama, Binance dogrulama, ambiguity tespiti)
DEGISTIRMEDEN calistirir; ikinci cagri (ayni symbol, FARKLI bir
CoinGeckoFetcher instance'indan bile olsa) SIFIR HTTP istegiyle cache'ten
doner. Yalniz UNAMBIGUOUS VERIFIED pozitif sonuclar cache'lenir --
unsupported/ambiguous/inconclusive(429/timeout)/malformed hicbiri
cache'lenmez (negatif cache YOK). Normalization (lower/upper) mevcut
kontratla ayni. Concurrency: es zamanli okuma/yazma cache corruption
uretmez. Stale-ticker filtresi ve exchange_count taze davranisi resolver
cache'inden ETKILENMEZ (ayri katman, her zaman taze cekilir).

Network GEREKTIRMEZ: `_http_get_json` monkeypatch'lenir. DB'ye dokunmaz.
Testler arasinda class-level cache'ler (`_coins_list_cache`,
`_verified_id_cache`) autouse fixture ile SIFIRLANIR -- test-order
bagimliligi yok.

Kaynak / provenance: "COINGECKO LATENCY / REQUEST-EFFICIENCY AUDIT" turunda
canli olculen resolver darbogazina (toplam surenin %64-88'i) karsi onaylanan
Model D (process cache, yalniz dogrulanmis pozitif sonuc, negatif cache yok)
tasarimi buraya implement edildi.
"""
import importlib.util
import os
import sys
import threading

import pytest

_PROD_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "kripto_sinyal_sistemi_v6_pro_gui.pyw")
spec = importlib.util.spec_from_file_location('kss', _PROD_FILE)
kss = importlib.util.module_from_spec(spec)
sys.modules['kss'] = kss
spec.loader.exec_module(kss)

CG = kss.CoinGeckoFetcher


@pytest.fixture(autouse=True)
def reset_class_caches():
    """Her testten once/sonra class-level cache'leri temizler -- test-order
    bagimliligi yaratmamak icin ZORUNLU."""
    orig_coins_list = CG._coins_list_cache
    orig_verified = dict(CG._verified_id_cache)
    CG._coins_list_cache = None
    CG._verified_id_cache = {}
    yield
    CG._coins_list_cache = orig_coins_list
    CG._verified_id_cache = orig_verified


# ---------- Sentetik CoinGecko backend ----------

def _ticker(coin_id, identifier, base, target="USDT", is_stale=False):
    return {"market": {"identifier": identifier}, "base": base, "target": target,
            "is_stale": is_stale, "coin_id": coin_id}


CANDIDATES = {
    # btc/eth BILINCLI OLARAK 2 aday tasir (gercek dunya davranisi: coins/list'te
    # ayni sembolu paylasan wrapped/bridged/spam varyantlar cok yaygin -- audit
    # turunda BTC icin 11, ETH icin 14 aday gozlenmisti). Bu, tek-aday
    # kisa-yolun (Binance dogrulamasini hic cagirmadan direkt donen) HTTP
    # failure/429/malformed senaryolarini test edememesini onlemek icin de
    # gerekli -- tek adayli sembollerde dogrulama hic yapilmiyor.
    "btc": [
        {"id": "bitcoin", "symbol": "btc", "name": "Bitcoin"},
        {"id": "btc-wrapped-fake", "symbol": "btc", "name": "Fake Wrapped BTC"},
    ],
    "eth": [
        {"id": "ethereum", "symbol": "eth", "name": "Ethereum"},
        {"id": "eth-wrapped-fake", "symbol": "eth", "name": "Fake Wrapped ETH"},
    ],
    "meme": [
        {"id": "memecoin-2", "symbol": "meme", "name": "Memecoin"},
        {"id": "memetoon", "symbol": "meme", "name": "Memetoon"},
    ],
    "sol": [{"id": "solana", "symbol": "sol", "name": "Solana"}],  # gercek tek-aday kisa-yol senaryosu
    "nope": [],  # unsupported: coins_list'te hic yok
}

TICKERS = {
    "bitcoin": [_ticker("bitcoin", "binance", "BTC"), _ticker("bitcoin", "kraken", "BTC")],
    "btc-wrapped-fake": [_ticker("btc-wrapped-fake", "some_other_exchange", "BTC")],
    "ethereum": [_ticker("ethereum", "binance", "ETH")],
    "eth-wrapped-fake": [_ticker("eth-wrapped-fake", "some_other_exchange", "ETH")],
    # MEME: yalniz memecoin-2 gercekten Binance'te (canli auditte gozlenen gercek senaryo)
    "memecoin-2": [_ticker("memecoin-2", "binance", "MEME")],
    "memetoon": [_ticker("memetoon", "some_other_exchange", "MEME")],
    "solana": [_ticker("solana", "binance", "SOL")],
}

MARKET_CAPS = {"memecoin-2": 100.0, "memetoon": 999999.0,
               "bitcoin": 999999.0, "btc-wrapped-fake": 1.0,
               "ethereum": 999999.0, "eth-wrapped-fake": 1.0}


class FakeBackend:
    """Kontrollu, aci senaryolar uretebilen sentetik CoinGecko backend'i."""

    def __init__(self):
        self.request_log = []  # (url, params) listesi
        self.force_429_for = set()       # bu coin_id'lerin ticker istegi 429 (None) donsun
        self.force_inconclusive_list = False  # /coins/list basarisiz olsun

    def handler(self, url, params=None, headers=None, session=None, debug_label=None):
        self.request_log.append((url, dict(params or {}), debug_label))
        if "/coins/list" in url:
            if self.force_inconclusive_list:
                return None
            out = []
            for sym, cands in CANDIDATES.items():
                out.extend(cands)
            return out
        if "/coins/markets" in url:
            ids = (params or {}).get("ids", "").split(",")
            return [{"id": cid, "market_cap": MARKET_CAPS.get(cid, 0)} for cid in ids]
        if "/tickers" in url:
            # url formatı: .../coins/{coin_id}/tickers
            coin_id = url.split("/coins/")[1].split("/tickers")[0]
            if coin_id in self.force_429_for:
                return None  # 429/inconclusive simülasyonu
            tickers = TICKERS.get(coin_id, [])
            exch_filter = (params or {}).get("exchange_ids", "")
            allowed = set(exch_filter.split(",")) if exch_filter else None
            if allowed is not None:
                tickers = [t for t in tickers if t["market"]["identifier"] in allowed]
            return {"tickers": tickers}
        return None


@pytest.fixture
def backend(monkeypatch):
    b = FakeBackend()
    monkeypatch.setattr(kss, "_http_get_json", b.handler)
    return b


def _resolver_request_count(backend_obj):
    return sum(1 for (url, params, label) in backend_obj.request_log
               if "/coins/list" in url or "/coins/markets" in url
               or ("/tickers" in url and "unique_major_exchanges" not in (label or "")))


# ---------- 1) Cache miss / hit temel davranis ----------

def test_first_call_cache_miss_resolves_via_full_algorithm(backend):
    cg = CG()
    coin_id = cg.resolve_coin_id("btc")
    assert coin_id == "bitcoin"
    assert len(backend.request_log) > 0, "ilk cagri gercek HTTP istegi uretmeli"


def test_second_call_same_symbol_cache_hit_zero_resolver_requests(backend):
    cg = CG()
    cg.resolve_coin_id("btc")
    n_before = len(backend.request_log)
    result = cg.resolve_coin_id("btc")
    n_after = len(backend.request_log)
    assert result == "bitcoin"
    assert n_after == n_before, "cache hit'te SIFIR yeni HTTP istegi olmali"


def test_fresh_instance_sees_same_process_cache(backend):
    cg1 = CG()
    cg1.resolve_coin_id("btc")
    n_before = len(backend.request_log)

    cg2 = CG()  # tamamen YENI instance (Level1Worker'daki gercek davranis)
    result = cg2.resolve_coin_id("btc")
    n_after = len(backend.request_log)

    assert result == "bitcoin"
    assert n_after == n_before, "farkli instance process cache'i gormeli, resolver tekrar calismamali"


def test_different_symbol_independent_miss(backend):
    cg = CG()
    cg.resolve_coin_id("btc")
    n_before = len(backend.request_log)
    result = cg.resolve_coin_id("eth")
    n_after = len(backend.request_log)
    assert result == "ethereum"
    assert n_after > n_before, "farkli sembol icin resolver tekrar calismali (kendi cache miss'i)"


def test_normalization_lowercase_uppercase_parity(backend):
    cg1 = CG()
    cg1.resolve_coin_id("BTC")
    n_before = len(backend.request_log)
    cg2 = CG()
    result = cg2.resolve_coin_id("btc")
    n_after = len(backend.request_log)
    assert result == "bitcoin"
    assert n_after == n_before, "BTC/btc ayni normalize edilmis cache anahtarini kullanmali"


# ---------- 2) Negatif/inconclusive sonuclar cache'lenmez ----------

def test_unsupported_symbol_not_cached(backend):
    """'nope' coins/list'te hic yok -- None doner. `_coins_list_cache` (mevcut,
    Model D'den bagimsiz bir katman) ilk cagridan sonra zaten dolu oldugu icin
    ikinci cagri HTTP istegi uretmeyebilir -- asil dogrulanmasi gereken,
    Model D'nin YENI `_verified_id_cache` katmanina bu negatif sonucun HIC
    yazilmadigi (request sayaci degil, cache state'in kendisi)."""
    cg = CG()
    result1 = cg.resolve_coin_id("nope")
    assert result1 is None
    assert "nope" not in CG._verified_id_cache, "unsupported sonuc _verified_id_cache'e YAZILMAMALI"
    cg2 = CG()
    result2 = cg2.resolve_coin_id("nope")
    assert result2 is None
    assert "nope" not in CG._verified_id_cache


def test_meme_style_collision_resolves_to_correct_candidate_not_the_ambiguous_one(backend):
    """Gercek MEME senaryosu (audit'te gozlendi): birden fazla aday var, ama
    yalniz BIRI gercekten Binance'te islem goruyor -- market cap'i daha buyuk
    olan (memetoon) YANLIS aday, dogru sonuc (memecoin-2) verilmeli. Bu
    ambiguous DEGIL (net 1 dogrulanmis aday), o yuzden CACHE'LENMELI."""
    cg = CG()
    result = cg.resolve_coin_id("meme")
    assert result == "memecoin-2", "market cap'i daha kucuk ama GERCEKTEN Binance'te olan aday secilmeli"
    assert CG._verified_id_cache.get("meme") == "memecoin-2"


def test_ambiguous_two_valid_candidates_not_cached(monkeypatch):
    """Iki adayin da GERCEKTEN Binance'te ayni sembolle islem gordugu (gercek
    ambiguity) senaryosu -- None donmeli VE cache'lenmemeli."""
    local_tickers = dict(TICKERS)
    local_tickers["memetoon"] = [_ticker("memetoon", "binance", "MEME")]  # artik o da binance'te

    def handler(url, params=None, headers=None, session=None, debug_label=None):
        if "/coins/list" in url:
            out = []
            for cands in CANDIDATES.values():
                out.extend(cands)
            return out
        if "/coins/markets" in url:
            ids = (params or {}).get("ids", "").split(",")
            return [{"id": cid, "market_cap": MARKET_CAPS.get(cid, 0)} for cid in ids]
        if "/tickers" in url:
            coin_id = url.split("/coins/")[1].split("/tickers")[0]
            tickers = local_tickers.get(coin_id, [])
            exch_filter = (params or {}).get("exchange_ids", "")
            allowed = set(exch_filter.split(",")) if exch_filter else None
            if allowed is not None:
                tickers = [t for t in tickers if t["market"]["identifier"] in allowed]
            return {"tickers": tickers}
        return None

    log = []
    def counting_handler(*a, **k):
        log.append(1)
        return handler(*a, **k)

    monkeypatch.setattr(kss, "_http_get_json", counting_handler)
    cg = CG()
    result1 = cg.resolve_coin_id("meme")
    assert result1 is None, "iki gecerli aday -> ambiguous -> None"
    n_before = len(log)
    cg2 = CG()
    result2 = cg2.resolve_coin_id("meme")
    n_after = len(log)
    assert result2 is None
    assert n_after > n_before, "ambiguous sonuc cache'lenmemeli -- ikinci cagri tekrar resolver calistirmali"


def test_transient_inconclusive_429_not_cached(backend):
    """Adaylardan biri 429/inconclusive donerse (backend.handler None doner,
    _verify_binance_listing bunu 'belirsiz' sayar) sonuc cache'lenmemeli."""
    backend.force_429_for.add("bitcoin")
    cg = CG()
    result1 = cg.resolve_coin_id("btc")
    assert result1 is None, "tek aday inconclusive donerse (429 simulasyonu) None donmeli"
    n_before = len(backend.request_log)
    backend.force_429_for.discard("bitcoin")  # "429 gecti", coin artik saglikli
    cg2 = CG()
    result2 = cg2.resolve_coin_id("btc")
    n_after = len(backend.request_log)
    assert result2 == "bitcoin", "429 gectikten sonra ayri bir cagri basariyla resolve edebilmeli"
    assert n_after > n_before, "429/inconclusive sonuc cache'lenmemis olmali -- yeniden resolver calisti"


def test_coins_list_fetch_failure_not_cached(backend):
    backend.force_inconclusive_list = True
    cg = CG()
    result1 = cg.resolve_coin_id("btc")
    assert result1 is None
    backend.force_inconclusive_list = False
    cg2 = CG()
    result2 = cg2.resolve_coin_id("btc")
    assert result2 == "bitcoin", "coins/list basarisizligi gectikten sonra normal resolve calismali"


def test_malformed_response_not_cached(monkeypatch):
    def handler(url, params=None, headers=None, session=None, debug_label=None):
        if "/coins/list" in url:
            # 2 aday -- tek-aday kisa-yolu (dogrulamasiz) atlamak icin ZORUNLU,
            # aksi halde ticker endpoint'i hic cagrilmaz.
            return [{"id": "bitcoin", "symbol": "btc", "name": "Bitcoin"},
                    {"id": "btc-fake", "symbol": "btc", "name": "Fake"}]
        if "/coins/markets" in url:
            return [{"id": "bitcoin", "market_cap": 100.0}, {"id": "btc-fake", "market_cap": 1.0}]
        if "/tickers" in url:
            return {"not_tickers_key": []}  # malformed -- 'tickers' anahtari yok
        return None

    monkeypatch.setattr(kss, "_http_get_json", handler)
    cg = CG()
    result1 = cg.resolve_coin_id("btc")
    assert result1 is None, "malformed ticker yaniti None uretmeli (verify_binance_listing 'tickers' bekliyor)"
    assert "btc" not in CG._verified_id_cache, "malformed sonuc _verified_id_cache'e YAZILMAMALI"


def test_first_failure_then_second_healthy_call_reresolves(backend):
    """429 sonrasi coin duzelirse, SONRAKI BAGIMSIZ analiz resolver'i yeniden
    deneyebilmeli (negatif/inconclusive cache YOK invariant'inin somut kaniti)."""
    backend.force_429_for.add("ethereum")
    cg = CG()
    assert cg.resolve_coin_id("eth") is None
    backend.force_429_for.discard("ethereum")
    cg2 = CG()
    assert cg2.resolve_coin_id("eth") == "ethereum"


# ---------- 3) Verified pozitif sonuc dogru cache'leniyor ----------

def test_successful_verified_id_is_cached_exact(backend):
    cg = CG()
    coin_id = cg.resolve_coin_id("btc")
    assert coin_id == "bitcoin"
    assert CG._verified_id_cache.get("btc") == "bitcoin"


# ---------- 4) Concurrency ----------

def test_concurrent_reads_same_symbol_safe(backend):
    cg = CG()
    cg.resolve_coin_id("btc")  # onceden cache'le

    results = []
    errors = []

    def worker():
        try:
            results.append(CG().resolve_coin_id("btc"))
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"concurrent read hatasi: {errors}"
    assert all(r == "bitcoin" for r in results), f"tum sonuclar 'bitcoin' olmali: {set(results)}"


def test_concurrent_writes_same_symbol_converge_safely(backend):
    """Birden fazla thread AYNI, henuz cache'lenmemis sembolu es zamanli
    resolve etmeye calisirsa -- hepsi ayni (dogru) sonuca ulasmali, cache
    corruption/partial-value olmamali."""
    results = []
    errors = []

    def worker():
        try:
            results.append(CG().resolve_coin_id("eth"))
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(15)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"concurrent write hatasi: {errors}"
    assert all(r == "ethereum" for r in results), f"tum sonuclar 'ethereum' olmali: {set(results)}"
    assert CG._verified_id_cache.get("eth") == "ethereum"


def test_different_symbols_do_not_corrupt_each_others_cache_state(backend):
    results = {}
    errors = []
    lock = threading.Lock()

    def worker(sym, key):
        try:
            r = CG().resolve_coin_id(sym)
            with lock:
                results[key] = r
        except Exception as e:
            errors.append(e)

    threads = []
    for i in range(10):
        threads.append(threading.Thread(target=worker, args=("btc", f"btc{i}")))
        threads.append(threading.Thread(target=worker, args=("eth", f"eth{i}")))
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    for key, val in results.items():
        if key.startswith("btc"):
            assert val == "bitcoin"
        else:
            assert val == "ethereum"


# ---------- 5) Downstream davranis degismedi ----------

def test_stale_ticker_filter_behavior_unchanged_after_cache(backend):
    """Resolver cache'i devrede olsa bile get_unique_major_exchanges() HER
    ZAMAN taze veri ceker -- stale-ticker filtresi resolver cache'inden
    hic etkilenmemeli."""
    cg = CG()
    coin_id = cg.resolve_coin_id("btc")
    assert coin_id == "bitcoin"
    # TICKERS['bitcoin'] icinde binance+kraken var, MAJOR_EXCHANGES kesisimi ile say
    result = cg.get_unique_major_exchanges(coin_id)
    assert result is not None
    assert result["count"] >= 1  # binance + kraken major kumede


def test_exchange_count_fresh_behavior_unchanged_across_cache_hit(backend):
    cg1 = CG()
    coin_id1 = cg1.resolve_coin_id("btc")
    result1 = cg1.get_unique_major_exchanges(coin_id1)

    cg2 = CG()  # cache hit ile gelen instance
    coin_id2 = cg2.resolve_coin_id("btc")
    result2 = cg2.get_unique_major_exchanges(coin_id2)

    assert coin_id1 == coin_id2 == "bitcoin"
    assert result1 == result2, "cold/warm cache sonrasi exchange_count sonucu AYNI olmali (ayri, taze cekim)"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
