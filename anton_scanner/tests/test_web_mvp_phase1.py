# -*- coding: utf-8 -*-
"""
PERMANENT REGRESSION SUITE -- Mobile Web MVP Phase 1 (Controlled Implementation)

Ne test ediyor:
1) Import/headless: `.pyw`'nin QApplication/pencere oluşturmadan modül olarak
   import edilebildiğini, `run_level1_core()`'un plain bir modül-seviyesi
   fonksiyon olduğunu.
2) Report parity / canonical execution path: `Level1Worker.run()`'ın artık
   `run_level1_core()`'u ÇAĞIRDIĞINI (kaynak-inceleme regresyon guard'ı --
   iki ayrı Level 1 algoritması oluşmasını engeller), ve `run_level1_core()`'un
   deterministic TEST_GOOD mock profiliyle tam bir canonical report ürettiğini
   (Model D alanları dahil).
3) DB path env/default parity: `KRIPTO_DB_PATH` set/unset durumunda
   `HistoryDB._db_path()` davranışı.
4) Invalid symbol / failure semantics: `run_level1_core()`'un kendi
   try/except'ini KURMADIĞINI (hata/iptal çağırana sessizce yutulmadan
   ulaşıyor), `web_app.py`'nin bunu HTML hata mesajına çevirdiğini, test
   profillerinin web'den reddedildiğini.

Network GEREKTİRMEZ (TEST_GOOD mock profili + monkeypatch kullanılır --
gerçek Binance/CoinGecko/Anthropic çağrıları içeren canlı ETH/BTC testi bu
turda MANUEL doğrulandı, bkz. Final Report, ama CI'da flaky/yavaş olacağı
için kalıcı suite'e alınmadı). Gerçek kullanıcı DB'sine dokunmaz.

Kaynak / provenance: "Mobile Web MVP -- Phase 1" turu (V6 Mobile Web MVP
Deployment Architecture Audit'in kontrollü implementasyonu).
"""
import importlib.util
import inspect
import os
import sys

import pytest

_PROD_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "kripto_sinyal_sistemi_v6_pro_gui.pyw")
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

spec = importlib.util.spec_from_file_location('kss', _PROD_FILE)
kss = importlib.util.module_from_spec(spec)
sys.modules['kss'] = kss
spec.loader.exec_module(kss)


# ---------- 1) Import / headless ----------

def test_module_imports_without_qapplication():
    """.pyw import edildiğinde HİÇBİR QApplication instance'ı oluşmamalı --
    main() yalnız `if __name__ == "__main__":` altında çağrılıyor."""
    from PySide6.QtWidgets import QApplication
    assert QApplication.instance() is None, (
        "modül import'u bir QApplication yaratmamalı -- headless web ortamında "
        "bu, display olmadan crash'e yol açar")


def test_run_level1_core_is_plain_module_level_function():
    assert inspect.isfunction(kss.run_level1_core)
    assert not inspect.ismethod(kss.run_level1_core)
    # Bound bir QThread metodu DEĞİL -- Qt import'undan (satır ~7475) ÖNCE
    # de tanımlanabilecek saf bir fonksiyon (konumu Level1Worker'a yakın
    # olsa da Qt sınıflarına bağımlı değil).
    sig = inspect.signature(kss.run_level1_core)
    assert "symbol" in sig.parameters
    assert "on_progress" in sig.parameters
    assert "should_cancel" in sig.parameters


# ---------- 2) Report parity / canonical execution path ----------

def test_level1worker_run_delegates_to_run_level1_core():
    """REGRESYON GUARD: Level1Worker.run()'ın kaynağı `run_level1_core(`
    çağrısı İÇERMELİ -- STAGES_CONFIG döngüsünü/ScoreEngine çağrısını
    KENDİSİ YENİDEN İMPLEMENTE ETMEMELİ. Bu, masaüstü ve web'in aynı
    canonical execution path'i paylaşmasının garantisidir; ileride biri
    diğerinden bağımsız değiştirilirse bu test FAIL eder."""
    src = inspect.getsource(kss.Level1Worker.run)
    assert "run_level1_core(" in src
    assert "STAGES_CONFIG" not in src, (
        "Level1Worker.run() artık STAGES_CONFIG döngüsünü DOĞRUDAN içermemeli "
        "-- bu mantık run_level1_core()'a taşındı")


def test_run_level1_core_test_profile_produces_full_report():
    """TEST_GOOD mock profiliyle (network YOK, deterministik) uçtan uca
    calistirilir -- STAGES_CONFIG döngüsü, ScoreEngine.full_report(), Model D
    additive blok, is_test analysis_time/price dalı hepsi gerçekten çalışır."""
    mock_fetcher = kss.MockFetcher(seed=42)
    result = kss.run_level1_core("TEST_GOOD", mock_fetcher=mock_fetcher)

    assert result["symbol"] == "TEST_GOOD"
    assert result["is_test"] is True
    report = result["report"]
    for key in ("signal_score", "risk_score", "coverage", "confidence",
                "verdict_title", "verdict_desc", "entry_status", "vetos"):
        assert key in report

    # Model D additive alanları da run_level1_core() içinde eklenmiş olmalı
    # (Level1Worker.run()'da olduğu gibi -- bu davranış TAŞINDI, kaybolmadı).
    for key in ("recent_price_action_class", "btc_7d_return_pct",
                "btc_strong_down", "restricted_candidate", "restricted_reason"):
        assert key in report

    # is_test dalı: analysis_price None, analysis_time dolu (satır satır
    # aynı önceki Level1Worker.run() davranışı).
    assert report["analysis_price"] is None
    assert report["analysis_time"] is not None


def test_run_level1_core_included_onchain_default_excludes_optional_questions():
    """Web'de on-chain checkbox UI'ı yok (Phase 1 kapsamı dışı) --
    included_onchain default'u (bos set) opsiyonel sorulari disabled=True
    ile nodata yapmali, mevcut desktop 'tik edilmedi' davranisiyla ayni."""
    mock_fetcher = kss.MockFetcher(seed=42)
    result = kss.run_level1_core("TEST_GOOD", mock_fetcher=mock_fetcher)
    optional_answers = [a for a in result["report"]["answers"]
                         if a.get("_item") and a["_item"].optional]
    assert optional_answers, "en az bir opsiyonel (on-chain) soru olmali"
    for a in optional_answers:
        assert a["disabled"] is True
        assert a["answer"] == "nodata"


# ---------- 3) DB path env/default parity ----------

def test_db_path_default_when_env_unset(monkeypatch):
    monkeypatch.delenv("KRIPTO_DB_PATH", raising=False)
    path = kss.HistoryDB._db_path()
    assert path.endswith("kripto_sinyal_gecmis.db")
    assert "KRIPTO_DB_PATH" not in path  # sanity


def test_db_path_env_override(monkeypatch):
    monkeypatch.setenv("KRIPTO_DB_PATH", "/data/kripto_sinyal_gecmis.db")
    assert kss.HistoryDB._db_path() == "/data/kripto_sinyal_gecmis.db"


def test_db_path_env_override_then_unset_restores_default(monkeypatch):
    monkeypatch.setenv("KRIPTO_DB_PATH", "/tmp/override.db")
    assert kss.HistoryDB._db_path() == "/tmp/override.db"
    monkeypatch.delenv("KRIPTO_DB_PATH")
    assert kss.HistoryDB._db_path().endswith("kripto_sinyal_gecmis.db")
    assert kss.HistoryDB._db_path() != "/tmp/override.db"


# ---------- 4) Invalid symbol / failure semantics ----------

def test_run_level1_core_does_not_swallow_exceptions(monkeypatch):
    """run_level1_core() KENDİ try/except'ini KURMAZ -- hata cagirana
    (Level1Worker.run() veya web_app.py) sessizce yutulmadan ulasmali.
    Gecersiz sembol/ag hatasi simulasyonu: RealFetcher.fetch() exception
    firlatiyor."""
    class ExplodingFetcher:
        def __init__(self, **kwargs):
            pass

        def fetch(self, symbol, metric):
            raise RuntimeError("simulated network failure")

    monkeypatch.setattr(kss, "RealFetcher", ExplodingFetcher)
    with pytest.raises(RuntimeError, match="simulated network failure"):
        kss.run_level1_core("NOTAREALCOINXYZ")


def test_run_level1_core_cancellation_raises_level1cancelled():
    mock_fetcher = kss.MockFetcher(seed=42)
    with pytest.raises(kss.Level1Cancelled):
        kss.run_level1_core("TEST_GOOD", mock_fetcher=mock_fetcher,
                             should_cancel=lambda: True)


def test_level1cancelled_is_plain_exception_not_qt():
    """Level1Cancelled Qt'ye bagimli olmayan saf bir Exception olmali --
    web tarafinda Qt hic import edilmeden de yakalanabilmeli."""
    assert issubclass(kss.Level1Cancelled, Exception)


# ---------- 5) Flask route layer (web_app.py) -- monkeypatched core, network yok ----------

@pytest.fixture
def client(monkeypatch):
    sys.path.insert(0, _REPO_ROOT)
    import web_app
    yield web_app.app.test_client(), web_app
    sys.path.remove(_REPO_ROOT)


def _fake_report(symbol, restricted=False):
    stage_title = "Temel filtreleme"
    return {
        "report": {
            "signal_score": 72.5, "risk_score": 55.0, "risk_reliable": True,
            "risk_breadth": 2, "risk_coverage": 100.0, "coverage": 80.0,
            "confidence": "Orta", "verdict_title": "İzleme listesi",
            "verdict_desc": "Test açıklaması.", "entry_status": "Onay bekliyor",
            "vetos": [], "restricted_candidate": restricted,
            "restricted_reason": "model_d_candidate_confirmed" if restricted else "no_veto",
            "entry_timing_score": 66.0, "entry_timing": "Uygun",
            "entry_timing_answered": 3, "entry_timing_total": 3,
            "stage_scores": {stage_title: 80.0},
            "stage_answered": {stage_title: 4},
            "stage_items_count": {stage_title: 5},
            "signal_pros": [("Test destekleyen faktör", 8)],
            "signal_cons": [("Test baskılayan faktör", 5)],
            "risk_pros": [], "risk_cons": [],
            "counts": {"yes": 4, "wait": 1, "no": 0, "nodata": 1,
                       "total_valid": 5, "total_all": 6},
            "answers": [
                {"stage": stage_title, "question": "Test soru 1", "answer": "yes",
                 "weight": 8, "value": 1.23, "source": "Binance 1h", "reason": ""},
                {"stage": stage_title, "question": "Test soru 2", "answer": "nodata",
                 "weight": 5, "value": None, "source": "", "reason": "Veri alınamadı"},
            ],
            "analysis_time": "2026-08-16T12:00:00+00:00", "analysis_price": 100.0,
        },
        "symbol": symbol, "source_status": {}, "news_detail": {}, "is_test": False,
        "technical_structure": None,
    }


def test_index_route_ok(client):
    c, _ = client
    r = c.get("/")
    assert r.status_code == 200
    assert b"Analiz Et" in r.data


def test_analyze_route_renders_report_fields(client, monkeypatch):
    c, web_app = client
    monkeypatch.setattr(web_app.kss, "run_level1_core",
                         lambda symbol, **kw: _fake_report(symbol))
    r = c.post("/analyze", data={"symbol": "BTC", "symbol_custom": ""})
    assert r.status_code == 200
    body = r.data.decode("utf-8")
    assert "72.5" in body
    assert "İzleme listesi" in body
    assert "BTC" in body


def test_analyze_route_model_d_two_layer_presentation(client, monkeypatch):
    c, web_app = client
    monkeypatch.setattr(web_app.kss, "run_level1_core",
                         lambda symbol, **kw: _fake_report(symbol, restricted=True))
    r = c.post("/analyze", data={"symbol": "SOL", "symbol_custom": ""})
    body = r.data.decode("utf-8")
    assert "MOTOR KARARI" in body
    assert "POLİTİKA DURUMU" in body
    assert web_app.kss.MODEL_D_RESTRICTED_LABEL in body


def test_analyze_route_rejects_test_profile(client):
    c, _ = client
    r = c.post("/analyze", data={"symbol": "TEST_GOOD", "symbol_custom": ""})
    assert r.status_code == 200
    assert "desteklenmiyor" in r.data.decode("utf-8")


def test_analyze_route_handles_engine_exception_gracefully(client, monkeypatch):
    c, web_app = client

    def _boom(symbol, **kw):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(web_app.kss, "run_level1_core", _boom)
    r = c.post("/analyze", data={"symbol": "ZZZZZ", "symbol_custom": ""})
    assert r.status_code == 200  # crash yok, hata mesajı render edildi
    body = r.data.decode("utf-8")
    assert "Analiz başarısız" in body
    assert "Traceback" not in body  # stack trace kullanıcıya gösterilmemeli


def test_analyze_route_prefers_custom_symbol_over_dropdown(client, monkeypatch):
    c, web_app = client
    captured = {}

    def _capture(symbol, **kw):
        captured["symbol"] = symbol
        return _fake_report(symbol)

    monkeypatch.setattr(web_app.kss, "run_level1_core", _capture)
    c.post("/analyze", data={"symbol": "BTC", "symbol_custom": "near"})
    assert captured["symbol"] == "NEAR"


# ---------- 6) Phase 2: /save, /history, /history/<id>, /history/update-tracking ----------
# İzole temp DB kullanılır -- gerçek kullanıcı History DB'sine dokunulmaz.

@pytest.fixture
def isolated_db_client(client, monkeypatch, tmp_path):
    c, web_app = client
    db_path = str(tmp_path / "phase2_test.db")
    monkeypatch.setattr(web_app.kss.HistoryDB, "_db_path", classmethod(lambda cls: db_path))
    return c, web_app


def test_save_route_persists_via_canonical_save_analysis(isolated_db_client, monkeypatch):
    c, web_app = isolated_db_client
    monkeypatch.setattr(web_app.kss, "run_level1_core",
                         lambda symbol, **kw: _fake_report(symbol))
    r = c.post("/analyze", data={"symbol": "BTC", "symbol_custom": ""})
    body = r.data.decode("utf-8")
    import re
    m = re.search(r'name="token" value="([a-f0-9]+)"', body)
    assert m, "token bulunamadı -- /save akışı kırılmış olabilir"
    token = m.group(1)

    r2 = c.post("/save", data={"token": token})
    assert r2.status_code == 200
    assert "Kaydedildi" in r2.data.decode("utf-8")

    db = web_app.kss.HistoryDB()
    rows = db.get_all_analyses()
    assert len(rows) == 1
    assert rows[0]["symbol"] == "BTC"
    assert rows[0]["level"] == "Level 1 — Otomatik"
    assert rows[0]["data_source"] == "binance+coingecko+cmc+news"


def test_save_route_missing_token_shows_error_no_crash(isolated_db_client):
    c, _ = isolated_db_client
    r = c.post("/save", data={"token": "nonexistent"})
    assert r.status_code == 200
    assert "süresi doldu" in r.data.decode("utf-8")


def test_save_route_rejects_test_profile_even_if_stashed(isolated_db_client, monkeypatch):
    c, web_app = isolated_db_client
    monkeypatch.setattr(web_app.kss, "run_level1_core",
                         lambda symbol, **kw: _fake_report(symbol))
    # TEST_ sembolü /analyze'da zaten reddediliyor -- bu test, pending store'a
    # doğrudan (analyze'i atlayarak) bir TEST_ kaydı koyup /save'in de
    # bağımsız olarak reddettiğini doğrular (defense in depth).
    token = "deadbeef"
    web_app._PENDING_REPORTS[token] = {"symbol": "TEST_GOOD", "result": _fake_report("TEST_GOOD")}
    r = c.post("/save", data={"token": token})
    assert "geçmişe kaydedilmez" in r.data.decode("utf-8")
    db = web_app.kss.HistoryDB()
    assert db.get_all_analyses() == []


def test_history_route_lists_saved_analysis(isolated_db_client, monkeypatch):
    c, web_app = isolated_db_client
    monkeypatch.setattr(web_app.kss, "run_level1_core",
                         lambda symbol, **kw: _fake_report(symbol))
    r = c.post("/analyze", data={"symbol": "ETH", "symbol_custom": ""})
    import re
    token = re.search(r'name="token" value="([a-f0-9]+)"', r.data.decode("utf-8")).group(1)
    c.post("/save", data={"token": token})

    r2 = c.get("/history")
    assert r2.status_code == 200
    body = r2.data.decode("utf-8")
    assert "ETH" in body
    assert "İzleme listesi" in body


def test_history_route_empty_state_no_crash(isolated_db_client):
    c, _ = isolated_db_client
    r = c.get("/history")
    assert r.status_code == 200
    assert "Henüz kayıt yok" in r.data.decode("utf-8")


def test_history_detail_route_shows_stage_and_answers(isolated_db_client, monkeypatch):
    c, web_app = isolated_db_client
    monkeypatch.setattr(web_app.kss, "run_level1_core",
                         lambda symbol, **kw: _fake_report(symbol))
    r = c.post("/analyze", data={"symbol": "SOL", "symbol_custom": ""})
    import re
    token = re.search(r'name="token" value="([a-f0-9]+)"', r.data.decode("utf-8")).group(1)
    c.post("/save", data={"token": token})

    db = web_app.kss.HistoryDB()
    row_id = db.get_all_analyses()[0]["id"]
    r2 = c.get(f"/history/{row_id}")
    assert r2.status_code == 200
    body = r2.data.decode("utf-8")
    assert "SOL" in body
    assert "Test soru 1" in body
    assert "Aşama Skorları" in body


def test_history_detail_route_missing_id_no_crash(isolated_db_client):
    c, _ = isolated_db_client
    r = c.get("/history/99999")
    assert r.status_code == 200
    assert "bulunamadı" in r.data.decode("utf-8")


def test_history_detail_model_d_two_layer_parity_with_table(isolated_db_client, monkeypatch):
    """History detay ve History listesi AYNI Model D bilgisini taşımalı --
    history_table_verdict_text() (liste) ve model_d_reason kontrolü (detay)
    birbirine ters düşmemeli (bkz. Decision Surface Parity Audit, Bulgu 2
    kapanışı, ve History Table Model D Presentation Parity turu)."""
    c, web_app = isolated_db_client
    monkeypatch.setattr(web_app.kss, "run_level1_core",
                         lambda symbol, **kw: _fake_report(symbol, restricted=True))
    r = c.post("/analyze", data={"symbol": "AVAX", "symbol_custom": ""})
    import re
    token = re.search(r'name="token" value="([a-f0-9]+)"', r.data.decode("utf-8")).group(1)
    c.post("/save", data={"token": token})

    db = web_app.kss.HistoryDB()
    row = db.get_all_analyses()[0]
    assert row["model_d_reason"] == "model_d_candidate_confirmed"

    r2 = c.get("/history")
    list_body = r2.data.decode("utf-8")
    assert web_app.kss.MODEL_D_RESTRICTED_LABEL in list_body, (
        "History listesi Model D durumunu kaybetmemeli")

    r3 = c.get(f"/history/{row['id']}")
    detail_body = r3.data.decode("utf-8")
    assert "Motor Kararı" in detail_body
    assert "Politika Durumu" in detail_body
    assert web_app.kss.MODEL_D_RESTRICTED_LABEL in detail_body


def test_update_tracking_route_redirects_and_no_crash(isolated_db_client, monkeypatch):
    c, web_app = isolated_db_client
    monkeypatch.setattr(web_app.kss, "run_level1_core",
                         lambda symbol, **kw: _fake_report(symbol))
    r = c.post("/analyze", data={"symbol": "DOGE", "symbol_custom": ""})
    import re
    token = re.search(r'name="token" value="([a-f0-9]+)"', r.data.decode("utf-8")).group(1)
    c.post("/save", data={"token": token})

    # calculate_tracking gercek network cagirir -- deterministik/hizli olmasi
    # icin bos sonuc donen bir sahte ile degistiriyoruz (sadece route'un
    # crash etmedigini/redirect ettigini dogruluyoruz, tracking hesaplama
    # mantigina hic dokunmadan).
    monkeypatch.setattr(web_app.kss.BinanceClient, "calculate_tracking",
                         staticmethod(lambda *a, **kw: []))
    r2 = c.post("/history/update-tracking")
    assert r2.status_code in (302, 303)


def test_pending_store_evicts_oldest_when_full(monkeypatch):
    import web_app as wa
    wa._PENDING_REPORTS.clear()
    for i in range(wa._PENDING_MAX + 5):
        wa._stash_pending(f"COIN{i}", _fake_report(f"COIN{i}"))
    assert len(wa._PENDING_REPORTS) <= wa._PENDING_MAX


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
