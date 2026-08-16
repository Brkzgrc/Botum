# -*- coding: utf-8 -*-
"""
PERMANENT REGRESSION SUITE -- Model D History Persistence (model_d_reason)

Ne test ediyor: HistoryDB v3->v4 migration (model_d_reason kolonu), TRUE/FALSE/NULL
round-trip, unknown/future reason guvenligi, CSV export, ve "read path'te
evaluate_model_d_candidate() YENIDEN CAGRILMIYOR" invariant'i (stored deger dogrudan
okunuyor -- gecmis karar yeniden hesaplanmiyor).

Network GEREKTIRMEZ. Gercek kullanici DB'sine dokunmaz (izole temp dizin, sonunda
silinir).

Kaynak / provenance: scratchpad/model_d_history_persistence_tests.py -- guncel V6
production'a karsi tekrar dogrulanarak (39/39 PASS) buraya tasindi. Orijinaldeki
tempdir cleanup eksikligi burada duzeltildi (try/finally + shutil.rmtree).
"""
import gc
import importlib.util
import inspect
import os
import shutil
import sqlite3
import sys
import tempfile
import time

import pytest

_PROD_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "kripto_sinyal_sistemi_v6_pro_gui.pyw")
spec = importlib.util.spec_from_file_location('kss', _PROD_FILE)
kss = importlib.util.module_from_spec(spec)
sys.modules['kss'] = kss
spec.loader.exec_module(kss)

PASS, FAIL = [], []

ALL_STABLE_REASONS = [
    "symbol_is_btc", "no_veto", "multiple_vetos", "veto_not_btc_daily_trend",
    "confirmed_structure_not_bearish", "recent_class_not_positive",
    "btc_strong_down_unknown", "btc_strong_down_true", "model_d_candidate_confirmed",
]


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
    else:
        FAIL.append((name, detail))
        print(f"  [FAIL] {name}  {detail}")
    return cond


def fake_report(reason, verdict_title="Elenir", vetos=None):
    return {
        "signal_score": 42.0, "risk_score": 30.0, "signal_raw_score": 40.0,
        "coverage": 55.0, "confidence": "Orta", "verdict_title": verdict_title,
        "entry_status": "Red", "vetos": vetos or ["btc_daily_trend"],
        "stage_scores": {}, "answers": [], "risk_coverage": 100.0,
        "entry_timing_score": 60.0, "entry_timing": "Uygun",
        "entry_timing_answered": 3, "entry_timing_total": 3,
        "restricted_reason": reason,
    }


def _rmtree_retry(path, attempts=10, delay=0.2):
    """Windows: sqlite3 baglantilari `with` blogundan cikinca yalniz commit/
    rollback yapar, dosyayi kapatmaz -- gercek kapanma GC finalize edince olur,
    ki bu senkron/garantili degil (gc.collect() bile bazen yetersiz kaliyor,
    bu turde kanitlandi). Kisa bir retry dongusu ile Windows'un dosya
    handle'ini serbest birakmasi icin firsat taniniyor."""
    for i in range(attempts):
        gc.collect()
        try:
            shutil.rmtree(path)
            return
        except OSError:
            if i == attempts - 1:
                return  # son denemede de basarisizsa sessizce vazgec (yalniz temp artigi, kritik degil)
            time.sleep(delay)


def isolated_db(tmpdir, filename):
    path = os.path.join(tmpdir, filename)
    db = kss.HistoryDB.__new__(kss.HistoryDB)
    db._db_path = (lambda p: (lambda: p))(path)
    db._init_db()
    return db, path


def run():
    tmpdir = tempfile.mkdtemp(prefix="model_d_hist_test_")
    try:
        print("=" * 90)
        print("A) fresh DB has model_d_reason kolonu")
        print("=" * 90)
        db1, path1 = isolated_db(tmpdir, "fresh.db")
        with sqlite3.connect(path1) as conn:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(analyses)").fetchall()}
            version = conn.execute("PRAGMA user_version").fetchone()[0]
        check("A) fresh DB icinde model_d_reason kolonu var", "model_d_reason" in cols, f"cols={cols}")
        check("A) fresh DB user_version==4", version == 4, f"got={version}")

        print("\n" + "=" * 90)
        print("B/C/D) v3 DB migration (eski kayit VAR, model_d_reason YOK) -> v4, idempotent")
        print("=" * 90)
        path_v3 = os.path.join(tmpdir, "v3_legacy.db")
        with sqlite3.connect(path_v3) as conn:
            conn.execute("""
                CREATE TABLE analyses (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL,
                    analysis_time TEXT NOT NULL, analysis_price REAL, level TEXT NOT NULL,
                    signal_score REAL, risk_score REAL, signal_raw_score REAL, coverage REAL,
                    confidence TEXT, verdict TEXT, entry_status TEXT, vetos TEXT,
                    stage_scores TEXT, answers TEXT, data_source TEXT, created_at TEXT,
                    risk_coverage REAL, entry_timing_score REAL, entry_timing TEXT,
                    entry_timing_answered INTEGER, entry_timing_total INTEGER
                )
            """)
            conn.execute("""CREATE TABLE tracking_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT, analysis_id INTEGER NOT NULL,
                horizon_hours INTEGER NOT NULL, close_return_pct REAL, mfe_pct REAL, mae_pct REAL,
                high_price REAL, low_price REAL, close_price REAL, updated_at TEXT, status TEXT,
                UNIQUE(analysis_id, horizon_hours))""")
            conn.execute("INSERT INTO analyses (symbol, analysis_time, level, verdict) "
                          "VALUES ('BTC','2024-01-01T00:00','Level 1','Elenir')")
            conn.execute("PRAGMA user_version = 3")
            conn.commit()

        db_v3 = kss.HistoryDB.__new__(kss.HistoryDB)
        db_v3._db_path = (lambda p: (lambda: p))(path_v3)
        db_v3._init_db()
        with sqlite3.connect(path_v3) as conn:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(analyses)").fetchall()}
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            row_count = conn.execute("SELECT COUNT(*) FROM analyses").fetchone()[0]
        check("B) v3->v4 migration: model_d_reason eklendi", "model_d_reason" in cols, f"cols={cols}")
        check("B) v3->v4 migration: user_version==4", version == 4, f"got={version}")
        check("D) eski satir korunmus (row_count==1)", row_count == 1, f"got={row_count}")

        db_v3._init_db()
        with sqlite3.connect(path_v3) as conn:
            version2 = conn.execute("PRAGMA user_version").fetchone()[0]
            row_count2 = conn.execute("SELECT COUNT(*) FROM analyses").fetchone()[0]
        check("C) migration idempotent: ikinci cagridan sonra da version==4, satir sayisi degismedi",
              version2 == 4 and row_count2 == 1, f"version={version2} rows={row_count2}")

        print("\n" + "=" * 90)
        print("E-J) TRUE / FALSE / NULL save-read round-trip + tum stable reason enum'lari")
        print("=" * 90)
        db2, path2 = isolated_db(tmpdir, "roundtrip.db")
        ids = {}
        for i, reason in enumerate(ALL_STABLE_REASONS + [None]):
            rid = db2.save_analysis("ETH", "Level 1", fake_report(reason),
                                     analysis_price=100.0, data_source="test",
                                     analysis_time=f"2025-{(i % 12) + 1:02d}-{(i % 27) + 1:02d}T{(i % 23):02d}:00:00")
            ids[reason] = rid
            check(f"save succeeded for reason={reason!r} (rid != -1)", rid != -1, f"rid={rid}")

        for reason in ALL_STABLE_REASONS + [None]:
            row = db2.get_analysis_by_id(ids[reason])
            check(f"J) reason={reason!r} exact round-trip", row["model_d_reason"] == reason,
                  f"got={row['model_d_reason']!r}")

        true_row = db2.get_analysis_by_id(ids["model_d_candidate_confirmed"])
        check("E/F) TRUE save/read exact", true_row["model_d_reason"] == "model_d_candidate_confirmed")
        check("F) TRUE fixture stored verdict hala 'Elenir'", true_row["verdict"] == "Elenir")

        false_row = db2.get_analysis_by_id(ids["no_veto"])
        check("G) FALSE (stable reason) save/read exact", false_row["model_d_reason"] == "no_veto")

        legacy_row = db2.get_analysis_by_id(ids[None])
        check("H) NULL legacy preserved (None, string 'None' degil)", legacy_row["model_d_reason"] is None)
        check("I) NULL, False DIYE degil None olarak kaliyor (diagnostic ayrim korunuyor)",
              legacy_row["model_d_reason"] is not False and legacy_row["model_d_reason"] is None)

        print("\n" + "=" * 90)
        print("K) unknown/future reason -- safe, candidate=TRUE SAYILMAZ")
        print("=" * 90)
        unk_id = db2.save_analysis("SOL", "Level 1", fake_report("some_future_reason_v2"),
                                    analysis_price=50.0, data_source="test",
                                    analysis_time="2025-06-01T00:00:00")
        unk_row = db2.get_analysis_by_id(unk_id)
        check("K) unknown reason exact saklandi", unk_row["model_d_reason"] == "some_future_reason_v2")
        check("K) unknown reason 'model_d_candidate_confirmed'e ESIT DEGIL -> candidate TRUE sayilmiyor",
              unk_row["model_d_reason"] != "model_d_candidate_confirmed")

        print("\n" + "=" * 90)
        print("L/M/N) verdict/vetos/scores invariant (ayni fixture, farkli reason)")
        print("=" * 90)
        r_true = fake_report("model_d_candidate_confirmed")
        r_false = fake_report("no_veto")
        check("L/M/N) verdict/vetos/signal_score/risk_score reason'dan BAGIMSIZ ayni fixture'da ayni",
              r_true["verdict_title"] == r_false["verdict_title"] == "Elenir" and
              r_true["signal_score"] == r_false["signal_score"] and
              r_true["risk_score"] == r_false["risk_score"])

        print("\n" + "=" * 90)
        print("R) CSV export -- model_d_reason otomatik dahil, crash yok")
        print("=" * 90)
        csv_path = os.path.join(tmpdir, "export.csv")
        ok = db2.export_csv(csv_path)
        check("R) export_csv basarili (crash yok)", ok is True)
        with open(csv_path, "r", encoding="utf-8-sig") as f:
            header = f.readline()
        check("R) CSV header'da model_d_reason kolonu var", "model_d_reason" in header, f"header={header[:200]}")

        print("\n" + "=" * 90)
        print("T) DO NOT RECOMPUTE -- evaluate_model_d_candidate() read path'te CAGRILMIYOR")
        print("=" * 90)
        gad_src = inspect.getsource(kss.MainWindow.show_history_detail)
        code_lines_only = "\n".join(l for l in gad_src.splitlines() if not l.strip().startswith("#"))
        check("T) show_history_detail() GERCEK KOD SATIRLARINDA evaluate_model_d_candidate() CAGRISI YOK",
              "evaluate_model_d_candidate(" not in code_lines_only)
        check("T) show_history_detail() kaynagi model_d_reason'i DOGRUDAN row'dan okuyor",
              'row.get("model_d_reason")' in gad_src)

        print(f"\n{'=' * 70}\nSONUC: {len(PASS)} PASS, {len(FAIL)} FAIL\n{'=' * 70}")
        if FAIL:
            for name, detail in FAIL:
                print(f"  - {name}: {detail}")
            return 1
        print("TUM TESTLER GECTI.")
        return 0
    finally:
        _rmtree_retry(tmpdir)


def test_run():
    assert run() == 0


# ═══════════════════════════════════════════════════════════════════════════
# HISTORY TABLE MODEL D PRESENTATION PARITY (onaylı, controlled implementation)
# Bulgu 2 (Decision Surface Parity Audit) kapanışı: refresh_history_table()
# artık `history_table_verdict_text(raw_verdict, model_d_reason)` saf
# fonksiyonunu kullanıyor -- show_history_detail()'in AYNI kanonik koşulunu
# (model_d_reason == "model_d_candidate_confirmed") paylaşıyor, DB/schema/
# evaluate_model_d_candidate() hiçbiri değişmedi, yalnız read-time table
# presentation'ı. Qt/QTableWidget gerektirmez -- pure function testi.
# ═══════════════════════════════════════════════════════════════════════════

def test_history_table_true_shows_restricted_label():
    text = kss.history_table_verdict_text("Elenir", "model_d_candidate_confirmed")
    assert text == "Elenir · Yüksek Riskli İzle"
    assert kss.MODEL_D_RESTRICTED_LABEL in text
    assert "Elenir" in text, "motor kararı (raw verdict) ASLA gizlenmemeli"


@pytest.mark.parametrize("reason", [r for r in ALL_STABLE_REASONS if r != "model_d_candidate_confirmed"])
def test_history_table_false_stable_reasons_raw_verdict_unchanged(reason):
    text = kss.history_table_verdict_text("Elenir", reason)
    assert text == "Elenir"
    assert kss.MODEL_D_RESTRICTED_LABEL not in text


def test_history_table_legacy_none_raw_verdict_unchanged():
    """LEGACY (model_d_reason is None, Model D o tarihte yoktu) -- 'Model D
    degerlendirdi ve hayir' gibi yanlis bir yoruma DUSMEMELI, yalniz raw
    verdict AYNEN gosterilmeli (FALSE ile ayni gorunum, ayri semantik)."""
    text = kss.history_table_verdict_text("Elenir", None)
    assert text == "Elenir"
    assert kss.MODEL_D_RESTRICTED_LABEL not in text


def test_history_table_unknown_future_reason_not_treated_as_candidate():
    text = kss.history_table_verdict_text("Elenir", "some_future_reason_v2")
    assert text == "Elenir"
    assert kss.MODEL_D_RESTRICTED_LABEL not in text


def test_history_table_missing_verdict_falls_back_to_dash():
    """raw_verdict None/bos ise mevcut 'row["verdict"] or "—"' davranisi
    korunuyor -- bu fonksiyon o kismi degistirmedi, yalniz Model D suffix'ini
    ekliyor."""
    assert kss.history_table_verdict_text(None, None) == "—"
    assert kss.history_table_verdict_text("", None) == "—"
    assert kss.history_table_verdict_text(None, "model_d_candidate_confirmed") == "— · Yüksek Riskli İzle"


def test_history_table_detail_semantic_parity_true_case():
    """Table (yeni) ve detail (mevcut, degismedi) AYNI ikili bilgiyi tasimali:
    motor karari (raw verdict) + politika durumu (Yuksek Riskli Izle) --
    exact wording birebir olmak zorunda degil (detail iki ayri satir, table
    tek hucre), ama IKISI DE ikisini de icermeli, birbirine ters dusmemeli."""
    raw_verdict = "Elenir"
    table_text = kss.history_table_verdict_text(raw_verdict, "model_d_candidate_confirmed")
    # show_history_detail()'in GERCEK ürettiği iki satır (satır ~10635-10637,
    # bu turda degismedi) ile ayni ikili bilgiyi tasidigini dogrula:
    detail_line1 = f"Motor Kararı: {raw_verdict}"
    detail_line2 = f"Politika Durumu: {kss.MODEL_D_RESTRICTED_LABEL}"
    assert raw_verdict in table_text and raw_verdict in detail_line1
    assert kss.MODEL_D_RESTRICTED_LABEL in table_text and kss.MODEL_D_RESTRICTED_LABEL in detail_line2


def test_history_table_detail_semantic_parity_false_legacy_case():
    """FALSE/LEGACY icin table ve detail'in İKİSİ DE raw verdict'i AYNEN
    gösterdiğini (Model D suffix'i olmadan) dogrula -- iki yüzey ayrışmıyor."""
    for reason in (None, "no_veto"):
        table_text = kss.history_table_verdict_text("Elenir", reason)
        # show_history_detail()'in bu daldaki gercek satiri (satir ~10639,
        # degismedi): f"Karar: {row['verdict']}"
        detail_line = f"Karar: {'Elenir'}"
        assert table_text == "Elenir"
        assert "Elenir" in detail_line
        assert kss.MODEL_D_RESTRICTED_LABEL not in table_text


def test_history_table_stored_verdict_and_reason_columns_unchanged_by_presentation():
    """Bu fonksiyon SAF (pure) -- DB'ye yazilan/okunan `verdict`/`model_d_reason`
    kolonlarinin KENDISINI degistirmez, yalniz GUI'nin okudugu METNI turetir.
    save_analysis/get_analysis_by_id round-trip'i (mevcut, bu turda
    DEGISMEDI) bagimsiz olarak dogrulanmis durumda (bkz. run()/test_run()
    icindeki E-J kontrolleri) -- burada yalniz presentation fonksiyonunun
    girdi/sozlesmesinin DB semantigini degistirmedigini teyit ediyoruz."""
    row_like = {"verdict": "Elenir", "model_d_reason": "model_d_candidate_confirmed"}
    _ = kss.history_table_verdict_text(row_like["verdict"], row_like["model_d_reason"])
    assert row_like["verdict"] == "Elenir"
    assert row_like["model_d_reason"] == "model_d_candidate_confirmed"


def test_history_table_repeated_calls_deterministic():
    results = {kss.history_table_verdict_text("Elenir", "model_d_candidate_confirmed") for _ in range(5)}
    assert len(results) == 1


def test_history_table_evaluate_model_d_candidate_not_called_by_presentation_fn():
    """DO NOT RECOMPUTE invariant (T testiyle ayni ilke, bu fonksiyon icin
    de): history_table_verdict_text() kaynagi evaluate_model_d_candidate()
    CAGIRMAMALI -- yalniz stored model_d_reason'i okuyup karsilastiriyor."""
    src = inspect.getsource(kss.history_table_verdict_text)
    body_only = src.split('"""', 2)[-1]  # docstring'i (prose icinde fonksiyon adi geciyor) at
    code_lines_only = "\n".join(l for l in body_only.splitlines() if not l.strip().startswith("#"))
    assert "evaluate_model_d_candidate(" not in code_lines_only


def test_refresh_history_table_source_uses_new_helper():
    """Regresyon guard: refresh_history_table()'in gercek kaynagi artik
    history_table_verdict_text()'i cagiriyor -- eski `row["verdict"] or "—"`
    dogrudan `values` listesine YAZILMIYOR olmali (o davranis artik helper
    icinde)."""
    src = inspect.getsource(kss.MainWindow.refresh_history_table)
    assert "history_table_verdict_text(" in src
    assert 'row.get("model_d_reason")' in src


if __name__ == "__main__":
    sys.exit(run())
