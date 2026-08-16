# -*- coding: utf-8 -*-
"""
PERMANENT REGRESSION SUITE -- History Tracking Temporal Integrity
                               + Internal Continuity / Stale-Row Fix

Ne test ediyor:
  - BinanceClient.get_klines_range()  (pagination: cursor advance, dedup, ordering)
  - BinanceClient.calculate_tracking() (endpoint completeness / target-tolerance /
    directional close selection / MFE-MAE window / internal candle continuity /
    split metric integrity -- explicit "Eksik Veri" sonucu)
  - BinanceClient._window_continuity_ok() (duplicate / missing / unordered candle
    tespiti, non-boundary-aligned analysis_time false-positive testi)
  - HistoryDB.save_tracking() gercek production cagri deseninde -- stale-row
    resolution (valid -> Eksik Veri -> valid recovery), transient fetch failure
    invariant (eski row'a dokunulmaz).
  - GUI None-safe formatting (refresh_history_table pattern'inin izole simulasyonu).

Ag baglantisi GEREKTIRMEZ: BinanceClient.get_klines() (en alt seviye HTTP cagrisi)
sentetik, deterministik bir mum kaynagiyla monkeypatch'lenir; get_klines_range() ve
calculate_tracking()'in GERCEK production kodu bu sentetik veri uzerinde calisir.
Test sonunda monkeypatch geri alinir, temp DB dizinleri silinir.

Calistirma:
    python tests/test_tracking_temporal_integrity.py

Kaynak / provenance:
  - Bu suite 2026-08-15 tarihli "System Gap Audit Round 3" turunda basladi;
    "TRACKING CONTINUITY INTEGRITY CONTROLLED IMPLEMENTATION" turunda internal
    continuity fix (explicit "Eksik Veri" + split metric integrity) production'a
    eklendikten sonra guncellendi. Onceki suru "[BILINEN LIMIT]" olarak
    isaretlenen testler artik DUZELTILMIS davranisi dogruluyor.

DOKUNMA: bu dosya yalniz test icerir, production kodunu (kripto_sinyal_sistemi_v6_pro_gui.pyw)
DEGISTIRMEZ.
"""
import gc
import importlib.util
import io
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timezone, timedelta

_PROD_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "kripto_sinyal_sistemi_v6_pro_gui.pyw")

spec = importlib.util.spec_from_file_location('kss', _PROD_FILE)
kss = importlib.util.module_from_spec(spec)
sys.modules['kss'] = kss
spec.loader.exec_module(kss)

BC = kss.BinanceClient
INTERVAL_MS = 300_000  # 5m

PASS, FAIL = [], []
_TEMP_DIRS = []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
    else:
        FAIL.append((name, detail))
        print(f"  [FAIL] {name}  {detail}")
    return cond


def make_candle(open_time, o, h, l, c, v=1000.0):
    close_time = open_time + INTERVAL_MS - 1
    return [open_time, o, h, l, c, v, close_time]


def build_series(start_ms, end_ms, base=100.0, spike_up_at=None, spike_dn_at=None,
                  gap_ranges=None):
    gap_ranges = gap_ranges or []
    candles = []
    t = start_ms
    while t <= end_ms:
        in_gap = any(gs <= t < ge for gs, ge in gap_ranges)
        if not in_gap:
            o = h = l = c = base
            if spike_up_at is not None and t == spike_up_at:
                h = base * 1.10
                c = base * 1.05
            if spike_dn_at is not None and t == spike_dn_at:
                l = base * 0.90
                c = base * 0.95
            candles.append(make_candle(t, o, h, l, c))
        t += INTERVAL_MS
    return candles


_ACTIVE_SERIES = []


def fake_get_klines(symbol, interval="1h", start_time_ms=None, end_time_ms=None, limit=100):
    rows = [k for k in _ACTIVE_SERIES
            if (start_time_ms is None or k[0] >= start_time_ms)
            and (end_time_ms is None or k[0] <= end_time_ms)]
    rows.sort(key=lambda k: k[0])
    return rows[:limit]


def _rmtree_retry(path, attempts=10, delay=0.2):
    """Windows sqlite3 dosya-kilit gecikmesi icin kisa retry (bkz.
    test_model_d_history_persistence.py'deki ayni yorum)."""
    for i in range(attempts):
        gc.collect()
        try:
            shutil.rmtree(path)
            return
        except OSError:
            if i == attempts - 1:
                return
            time.sleep(delay)


def make_temp_db():
    d = tempfile.mkdtemp(prefix="kss_tracking_test_")
    _TEMP_DIRS.append(d)
    return os.path.join(d, "test_history.db")


def run():
    global _ACTIVE_SERIES
    _ORIG_GET_KLINES = BC.get_klines
    _ORIG_DB_PATH = kss.HistoryDB._db_path
    BC.get_klines = staticmethod(fake_get_klines)

    try:
        NOW = datetime.now(timezone.utc)
        ANALYSIS_DT = NOW - timedelta(hours=50)
        ANALYSIS_ISO = ANALYSIS_DT.isoformat()
        START_MS = int(ANALYSIS_DT.timestamp() * 1000)
        ANALYSIS_PRICE = 100.0
        HORIZONS = [1, 6, 12, 24, 48]
        MAX_H = max(HORIZONS)
        END_MS = START_MS + (MAX_H + 1) * 3600 * 1000

        def target_ms(h):
            return START_MS + h * 3600 * 1000

        print("=" * 90)
        print("TEST 1 -- FULL CONTIGUOUS DATA: tum 5 ufuk uretilmeli, hicbiri Eksik Veri olmamali")
        print("=" * 90)
        _ACTIVE_SERIES = build_series(START_MS - INTERVAL_MS, END_MS, base=100.0)
        results = BC.calculate_tracking("BTC", ANALYSIS_ISO, ANALYSIS_PRICE, HORIZONS)
        check("1) tum 5 ufuk uretildi", sorted(r["horizon_hours"] for r in results) == HORIZONS,
              f"got={sorted(r['horizon_hours'] for r in results)}")
        check("1) hicbiri Eksik Veri degil", all(r["status"] != "Eksik Veri" for r in results))

        print("\n" + "=" * 90)
        print("TEST 2 -- ENDPOINT SHORT (veri ~41.7h'de kesiliyor): 48h dogru SKIP edilmeli")
        print("=" * 90)
        short_end = START_MS + int(41.67 * 3600 * 1000)
        _ACTIVE_SERIES = build_series(START_MS - INTERVAL_MS, short_end, base=100.0)
        results2 = BC.calculate_tracking("BTC", ANALYSIS_ISO, ANALYSIS_PRICE, HORIZONS)
        got2 = sorted(r["horizon_hours"] for r in results2)
        check("2) 48h SKIP edildi (endpoint completeness != continuity)", 48 not in got2, f"got={got2}")
        check("2) 1/6/12/24h hala uretildi", set([1, 6, 12, 24]).issubset(set(got2)), f"got={got2}")

        print("\n" + "=" * 90)
        print("TEST 3 -- POST-TARGET CANDLE ASLA SECILMEZ")
        print("=" * 90)
        t48 = target_ms(48)
        gap_start = t48 - int(3.5 * INTERVAL_MS)
        gap_end = t48 + INTERVAL_MS
        _ACTIVE_SERIES = build_series(START_MS - INTERVAL_MS, END_MS, base=100.0,
                                       gap_ranges=[(gap_start, gap_end)])
        results3 = BC.calculate_tracking("BTC", ANALYSIS_ISO, ANALYSIS_PRICE, HORIZONS)
        r48_3 = next((r for r in results3 if r["horizon_hours"] == 48), None)
        check("3) 48h SKIP edildi (tek aday target sonrasindaydi)", r48_3 is None, f"got={r48_3}")

        print("\n" + "=" * 90)
        print("TEST 4 -- INTERNAL MID-SERIES GAP: artik SESSIZCE yanlis MFE degil,")
        print("EXPLICIT 'Eksik Veri' + split metric integrity uretilmeli")
        print("=" * 90)
        gap_lo = target_ms(24) - int(2 * 3600 * 1000)
        gap_hi = target_ms(24) - int(1 * 3600 * 1000)
        spike_time = gap_lo + INTERVAL_MS * 3
        _ACTIVE_SERIES = build_series(START_MS - INTERVAL_MS, END_MS, base=100.0,
                                       spike_up_at=spike_time, gap_ranges=[(gap_lo, gap_hi)])
        results4 = BC.calculate_tracking("BTC", ANALYSIS_ISO, ANALYSIS_PRICE, HORIZONS)
        r24_gapped = next(r for r in results4 if r["horizon_hours"] == 24)
        _ACTIVE_SERIES = build_series(START_MS - INTERVAL_MS, END_MS, base=100.0,
                                       spike_up_at=spike_time)
        results4b = BC.calculate_tracking("BTC", ANALYSIS_ISO, ANALYSIS_PRICE, HORIZONS)
        r24_full = next(r for r in results4b if r["horizon_hours"] == 24)

        check("4) [FIX] gap'li 24h status == 'Eksik Veri'", r24_gapped["status"] == "Eksik Veri",
              r24_gapped)
        check("4) [FIX] gap'li mfe_pct/mae_pct/high/low None (SESSIZCE yanlis deger YOK)",
              r24_gapped["mfe_pct"] is None and r24_gapped["mae_pct"] is None
              and r24_gapped["high_price"] is None and r24_gapped["low_price"] is None,
              r24_gapped)
        check("4) [SPLIT INTEGRITY] close_return_pct KORUNDU ve gap-free referansla AYNI",
              r24_gapped["close_return_pct"] == r24_full["close_return_pct"],
              f"gapped={r24_gapped['close_return_pct']} full={r24_full['close_return_pct']}")
        check("4) [SPLIT INTEGRITY] close_price de KORUNDU", r24_gapped["close_price"] == r24_full["close_price"])
        check("4) gap-free referansta status Eksik Veri DEGIL (kontrol grubu dogru)",
              r24_full["status"] != "Eksik Veri")

        print("\n" + "=" * 90)
        print("TEST 5 -- PAGINATION BOUNDARY: farkli page_limit ayni sonucu uretmeli")
        print("=" * 90)
        _ACTIVE_SERIES = build_series(START_MS - INTERVAL_MS, END_MS, base=100.0)
        single_page = BC.get_klines_range("BTC", "5m", START_MS - INTERVAL_MS, END_MS,
                                           page_limit=100000, max_pages=1)
        multi_page = BC.get_klines_range("BTC", "5m", START_MS - INTERVAL_MS, END_MS,
                                          page_limit=17, max_pages=200)
        check("5) coklu sayfa == tek sayfa", [k[0] for k in single_page] == [k[0] for k in multi_page])
        open_times = [k[0] for k in multi_page]
        check("5) duplicate open_time yok", len(open_times) == len(set(open_times)))
        diffs = [open_times[i + 1] - open_times[i] for i in range(len(open_times) - 1)]
        check("5) ic bosluk yok", all(d == INTERVAL_MS for d in diffs))

        print("\n" + "=" * 90)
        print("TEST 6 -- EXACT 48h INVARIANT (gap-free): bagimsiz hesap == production ciktisi")
        print("=" * 90)
        spike_up_t = target_ms(48) - INTERVAL_MS * 5
        spike_dn_t = START_MS + INTERVAL_MS * 10
        _ACTIVE_SERIES = build_series(START_MS - INTERVAL_MS, END_MS, base=200.0,
                                       spike_up_at=spike_up_t, spike_dn_at=spike_dn_t)
        results6 = BC.calculate_tracking("BTC", ANALYSIS_ISO, 200.0, [48])
        r48 = results6[0]
        window = [k for k in _ACTIVE_SERIES if k[0] >= START_MS and k[6] <= target_ms(48)]
        exp_high = max(float(k[2]) for k in window)
        exp_low = min(float(k[3]) for k in window)
        closed = [k for k in _ACTIVE_SERIES if k[6] > START_MS and k[6] <= target_ms(48)]
        exp_selected = max(closed, key=lambda k: k[6])
        exp_close = float(exp_selected[4])
        exp_mfe = round((exp_high - 200.0) / 200.0 * 100, 2)
        exp_mae = round((exp_low - 200.0) / 200.0 * 100, 2)
        exp_close_ret = round((exp_close - 200.0) / 200.0 * 100, 2)
        check("6) high/low/mfe/mae/close_return birebir eslesiyor",
              r48["high_price"] == exp_high and r48["low_price"] == exp_low
              and r48["mfe_pct"] == exp_mfe and r48["mae_pct"] == exp_mae
              and r48["close_return_pct"] == exp_close_ret,
              f"got={r48}")

        print("\n" + "=" * 90)
        print("TEST 7 -- CONTINUITY BOUNDARY: analysis_time +0/+1/+2/+4 dakika offsetlerinde")
        print("false-positive continuity rejection OLMAMALI; gercek gap her offsette YAKALANMALI")
        print("=" * 90)
        EPOCH_BASE = 1_700_000_000_000
        EPOCH_BASE -= EPOCH_BASE % INTERVAL_MS
        for offset_min in (0, 1, 2, 4):
            off_start = EPOCH_BASE + offset_min * 60_000
            off_iso = datetime.fromtimestamp(off_start / 1000, tz=timezone.utc).isoformat()
            off_end = off_start + 7 * 3600 * 1000
            ots = list(range(EPOCH_BASE - INTERVAL_MS * 5, off_end + INTERVAL_MS * 5, INTERVAL_MS))
            _ACTIVE_SERIES = [make_candle(t, 100.0, 100.0, 100.0, 100.0) for t in ots]
            r = BC.calculate_tracking("BTC", off_iso, 100.0, [6])
            check(f"7) offset=+{offset_min}min: tam veri -> FALSE POSITIVE YOK",
                  len(r) == 1 and r[0]["status"] != "Eksik Veri", f"got={r}")

            gap_open = off_start + INTERVAL_MS * 20
            gap_open -= gap_open % INTERVAL_MS
            _ACTIVE_SERIES = [make_candle(t, 100.0, 100.0, 100.0, 100.0) for t in ots if t != gap_open]
            r_gap = BC.calculate_tracking("BTC", off_iso, 100.0, [6])
            target6 = off_start + 6 * 3600 * 1000
            if off_start <= gap_open <= target6:
                check(f"7) offset=+{offset_min}min: gercek ic bosluk YAKALANDI",
                      len(r_gap) == 1 and r_gap[0]["status"] == "Eksik Veri", f"got={r_gap}")

        print("\n" + "=" * 90)
        print("TEST 8 -- DUPLICATE / UNORDERED tespiti (dogrudan _window_continuity_ok)")
        print("=" * 90)
        base_ots = list(range(START_MS, START_MS + INTERVAL_MS * 20, INTERVAL_MS))
        clean_window = [make_candle(t, 100, 100, 100, 100) for t in base_ots]
        check("8) temiz seri -> continuity OK", BC._window_continuity_ok(clean_window, INTERVAL_MS))

        dup_window = list(clean_window)
        dup_window.insert(5, dup_window[5])
        check("8) duplicate open_time -> continuity FAIL",
              not BC._window_continuity_ok(dup_window, INTERVAL_MS))

        missing_window = [k for i, k in enumerate(clean_window) if i != 10]
        check("8) eksik mum -> continuity FAIL", not BC._window_continuity_ok(missing_window, INTERVAL_MS))

        unordered_window = list(clean_window)
        unordered_window[3], unordered_window[7] = unordered_window[7], unordered_window[3]
        check("8) sirasi bozuk -> continuity FAIL",
              not BC._window_continuity_ok(unordered_window, INTERVAL_MS))

        print("\n" + "=" * 90)
        print("TEST 9 -- HISTORY PERSISTENCE + STALE-ROW RESOLUTION (gercek save_tracking())")
        print("=" * 90)
        kss.HistoryDB._db_path = lambda self, _p=make_temp_db(): _p
        db = kss.HistoryDB()
        aid = db.save_analysis("BTC", "Level 1", {"verdict_title": "test"},
                                analysis_price=100.0, data_source="Test",
                                analysis_time=ANALYSIS_ISO)

        # A) valid -> DB valid
        valid1 = [{"horizon_hours": 48, "close_return_pct": 1.0, "mfe_pct": 1.0, "mae_pct": -1.0,
                   "high_price": 101.0, "low_price": 99.0, "close_price": 101.0, "status": "Test"}]
        db.save_tracking(aid, valid1)
        check("9A) ilk valid sonuc kaydedildi", db.get_tracking(aid, 48)["mfe_pct"] == 1.0)

        # B) valid -> internal gap -> ayni row 'Eksik Veri'
        invalid1 = [{"horizon_hours": 48, "close_return_pct": 1.5, "mfe_pct": None, "mae_pct": None,
                     "high_price": None, "low_price": None, "close_price": 101.5, "status": "Eksik Veri"}]
        db.save_tracking(aid, invalid1)
        row_b = db.get_tracking(aid, 48)
        check("9B) [STALE-ROW FIX] eski valid satir 'Eksik Veri' ile OVERWRITE edildi (stale kalmadi)",
              row_b["status"] == "Eksik Veri" and row_b["mfe_pct"] is None)
        # C) Eksik Veri: close_return_pct korunuyor, mfe/mae/high/low None
        check("9C) Eksik Veri satirinda close_return_pct KORUNMUS (1.5)", row_b["close_return_pct"] == 1.5)
        check("9C) Eksik Veri satirinda mfe/mae/high/low hepsi None",
              row_b["mfe_pct"] is None and row_b["mae_pct"] is None
              and row_b["high_price"] is None and row_b["low_price"] is None)

        # D) invalid -> later valid -> metrikler geri geliyor
        valid2 = [{"horizon_hours": 48, "close_return_pct": 2.0, "mfe_pct": 3.0, "mae_pct": -0.5,
                   "high_price": 103.0, "low_price": 99.5, "close_price": 102.0, "status": "Test2"}]
        db.save_tracking(aid, valid2)
        row_d = db.get_tracking(aid, 48)
        check("9D) invalid -> valid recovery calisiyor", row_d["mfe_pct"] == 3.0)

        # E) valid -> global fetch failure ([] sonuc) -> save_tracking HIC CAGRILMAZ -> row DEGISMEZ
        empty_results = []
        if empty_results:
            db.save_tracking(aid, empty_results)
        row_e = db.get_tracking(aid, 48)
        check("9E) [TRANSIENT SAFETY] global fetch failure sonrasi VALID row DEGISMEDI",
              row_e["mfe_pct"] == 3.0)

        # F) invalid (Eksik Veri) -> global fetch failure -> invalid row KORUNUR (degismez)
        db.save_tracking(aid, invalid1)  # once tekrar invalid'e cek
        check("9F-setup) row tekrar Eksik Veri", db.get_tracking(aid, 48)["status"] == "Eksik Veri")
        empty_results2 = []
        if empty_results2:
            db.save_tracking(aid, empty_results2)
        row_f = db.get_tracking(aid, 48)
        check("9F) [TRANSIENT SAFETY] global fetch failure sonrasi Eksik Veri row KORUNDU",
              row_f["status"] == "Eksik Veri")

        # G) fiziksel tek satir (UNIQUE constraint)
        with sqlite3.connect(kss.HistoryDB._db_path(db)) as conn:
            cnt = conn.execute("SELECT COUNT(*) FROM tracking_results WHERE analysis_id=? AND horizon_hours=48",
                                (aid,)).fetchone()[0]
        check("9G) fiziksel olarak tek satir var (UNIQUE constraint calisiyor)", cnt == 1)

        print("\n" + "=" * 90)
        print("TEST 10 -- GUI None-SAFE FORMATTING (refresh_history_table pattern'inin izole kopyasi)")
        print("=" * 90)

        def render_row(tr):
            if tr:
                fmt_pct = lambda v: f"{v:+.1f}%" if v is not None else "—"
                mfe24, mae24 = fmt_pct(tr['mfe_pct']), fmt_pct(tr['mae_pct'])
                kap24, dur24 = fmt_pct(tr['close_return_pct']), tr.get("status", "—")
            else:
                mfe24 = mae24 = kap24 = dur24 = "—"
            return mfe24, mae24, kap24, dur24

        try:
            out_valid = render_row({"mfe_pct": 5.0, "mae_pct": -2.0, "close_return_pct": 3.0, "status": "Başarılı"})
            check("10) valid row render basarili", out_valid == ("+5.0%", "-2.0%", "+3.0%", "Başarılı"))
        except Exception as e:
            check("10) valid row render", False, str(e))

        try:
            out_invalid = render_row({"mfe_pct": None, "mae_pct": None, "close_return_pct": 1.5,
                                       "status": "Eksik Veri"})
            check("10) [GUI FIX] Eksik Veri row CRASH ETMEDEN render edildi",
                  out_invalid == ("—", "—", "+1.5%", "Eksik Veri"), out_invalid)
        except Exception as e:
            check("10) [GUI FIX] Eksik Veri row render", False, f"CRASH: {e}")

        print(f"\n{'=' * 70}\nSONUC: {len(PASS)} PASS, {len(FAIL)} FAIL\n{'=' * 70}")
        if FAIL:
            for name, detail in FAIL:
                print(f"  - {name}: {detail}")
            return 1
        print("TUM TESTLER GECTI.")
        return 0
    finally:
        BC.get_klines = _ORIG_GET_KLINES
        kss.HistoryDB._db_path = _ORIG_DB_PATH
        for d in _TEMP_DIRS:
            _rmtree_retry(d)


def test_run():
    """pytest discovery entry point -- `run()` asil test mantigini tasir,
    bu yalniz `python -m pytest tests` ile tek-runner uyumlulugu icin var."""
    assert run() == 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.exit(run())
