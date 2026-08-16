# -*- coding: utf-8 -*-
"""
ANTON SCANNER MOBILE WEB -- Phase 2 (onaylı, controlled implementation).

İnce Flask katmanı: mevcut V6 motorunu ve History/tracking altyapısını GUI
olmadan, telefon tarayıcısından kullanılabilir hale getirir. Bu dosya hiçbir
karar/skor/tracking mantığı İCAT ETMEZ -- yalnız aşağıdaki canonical
production fonksiyonlarını çağırıp sonucu HTML olarak render eder:

  - run_level1_core()                  -- Level 1 analiz (Level1Worker.run()
                                           ile PAYLAŞILAN tek execution path)
  - HistoryDB.save_analysis()          -- Geçmişe Kaydet (masaüstüyle AYNI
                                           contract: level="Level 1 — Otomatik",
                                           data_source="binance+coingecko+cmc+news")
  - HistoryDB.get_all_analyses()       -- History listesi
  - HistoryDB.get_analysis_by_id()     -- History detay
  - compute_risk_reliable()            -- History risk_reliable read-time
                                           türetme (masaüstüyle AYNI formül)
  - history_table_verdict_text()       -- Model D tablo presentation'ı
                                           (masaüstü History tablosuyla AYNI)
  - MODEL_D_RESTRICTED_LABEL/EXPLANATION -- Model D detay presentation'ı
  - BinanceClient.calculate_tracking() + HistoryDB.save_tracking() -- tracking
                                           güncelleme (masaüstü
                                           refresh_all_tracking() ile AYNI mantık)

Kapsam (Phase 2): Level 1 analiz (tam detay: stage dökümü, factor bazlı
yes/wait/no/nodata + provenance, pros/cons), Geçmişe Kaydet, History listesi,
History detay, tracking güncelleme. Level 2/3, AI Analyst UI, authentication
BU TURDA YOK.

Senkron -- bir /analyze isteği tamamlanana kadar (gerçek network çağrıları
dahil ~26s) tarayıcı bekler (kasıtlı, Phase 1'de onaylandı, gerçek Render
timeout görülmeden queue eklenmedi).

PENDING REPORT STORE: /analyze ile /save arasında (kullanıcı sonucu görüp
KARAR VERENE kadar) tam `report` dict'i (QuestionItem `_item` referansları
dahil, save_analysis()'in zaten kendi içinde yaptığı gibi) sunucu belleğinde,
kısa bir token ile tutulur -- yeni bir DB/cache katmanı YOK. Bu, Render'ın bu
serviste WEB_CONCURRENCY=1 (tek worker, deploy logunda doğrulandı) çalıştığı
için güvenlidir; birden fazla worker/instance'a ölçeklenirse bu basit bellek
içi store paylaşılmaz hale gelir (bilinen, dokümante edilmiş sınır).

Çalıştırma (lokal):
    pip install flask
    python web_app.py
    -> http://127.0.0.1:5000
"""
import importlib.util
import json
import os
import secrets
import sys
from importlib.machinery import SourceFileLoader

from flask import Flask, redirect, render_template, request, url_for

_PROD_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "kripto_sinyal_sistemi_v6_pro_gui.pyw")


def _load_kss_module():
    """Production .pyw'yi Qt penceresi açmadan modül olarak yükler.

    RENDER DEPLOY FIX (onaylı, gerçek crash -- freeze policy madde 1'e göre
    development yeniden açıldı): `.pyw` uzantısı CPython'ın kendi
    importlib._bootstrap_external.SOURCE_SUFFIXES listesine YALNIZ Windows'ta
    ekleniyor (`sys.platform.startswith('win')` koşuluyla). Bu yüzden
    `spec_from_file_location(name, path)` -- loader argümanı VERİLMEDEN --
    Linux'ta (Render) uzantıdan loader çıkaramayıp `None` döner, bu da
    `module_from_spec(None)` -> `AttributeError: 'NoneType' object has no
    attribute 'loader'` ile production'da GERÇEKTEN çöktü (Render deploy
    logu, doğrulandı). Fix: loader'ı AÇIKÇA `SourceFileLoader` olarak
    veriyoruz -- uzantıdan bağımsız, her platformda aynı davranış. Motor
    dosyasına (.pyw) hiç dokunulmadı, yalnız bu yükleme çağrısı düzeltildi."""
    loader = SourceFileLoader("kss", _PROD_FILE)
    spec = importlib.util.spec_from_file_location("kss", _PROD_FILE, loader=loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules["kss"] = module
    spec.loader.exec_module(module)
    return module


kss = _load_kss_module()

app = Flask(__name__)

# Mevcut paper-validation universe (16 coin, PAPER VALIDATION OPERATING
# PROTOCOL turunda sabitlendi) -- dropdown bu listeden, serbest metin
# girişine de izin verilir (Binance'te işlem gören herhangi bir USDT
# paritesi motor tarafından zaten destekleniyor).
VALIDATION_UNIVERSE = [
    "BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "DOGE", "AVAX", "DOT",
    "ARB", "OP", "SUI", "UNI", "LINK", "LTC", "ATOM",
]

# ---------- Pending report store (bkz. modül docstring'i) ----------
_PENDING_REPORTS = {}
_PENDING_MAX = 50


def _stash_pending(symbol, result):
    token = secrets.token_hex(8)
    if len(_PENDING_REPORTS) >= _PENDING_MAX:
        _PENDING_REPORTS.pop(next(iter(_PENDING_REPORTS)))
    _PENDING_REPORTS[token] = {"symbol": symbol, "result": result}
    return token


# ---------- Stage ordering (masaüstü display_report_in_result()'daki
# `all_titles` inşasıyla BİREBİR AYNI, satır ~10104-10105) ----------

def _ordered_stage_titles():
    signal_titles = [s.title for s in kss.STAGES_CONFIG if s.title in kss.SIGNAL_STAGE_WEIGHTS]
    risk_titles = [s.title for s in kss.STAGES_CONFIG if s.title in kss.RISK_STAGE_WEIGHTS]
    return signal_titles, risk_titles


def _build_result_view(report):
    """Masaüstü display_report_in_result()'un gösterdiği TÜM bilgileri
    (stage dökümü, pros/cons, veri kapsamı auto/manual/missing, factor bazlı
    liste) report dict'inden -- YENİ bir hesaplama YAPMADAN -- şablona uygun
    bir yapıya çevirir. Satır satır kaynak: display_report_in_result(),
    satır ~10063-10200 (bu oturumda canlı doğrulandı)."""
    signal_titles, risk_titles = _ordered_stage_titles()
    stage_rows = []
    for title in signal_titles + risk_titles:
        stage_rows.append({
            "title": title,
            "is_risk": title in kss.RISK_STAGE_WEIGHTS,
            "score": report["stage_scores"].get(title, 0),
            "answered": report["stage_answered"].get(title, 0),
            "total": report["stage_items_count"].get(title, 1),
        })

    # Veri Kapsamı: auto/manual/missing -- masaüstüyle BİREBİR AYNI dallanma
    # (satır ~10161-10169).
    auto_count = manual_count = 0
    for a in report.get("answers", []):
        if a.get("answer") == "nodata":
            continue
        src = a.get("source")
        if src and src != "Manuel giriş (kullanıcı)":
            auto_count += 1
        else:
            manual_count += 1

    # Factor bazlı liste, stage'e göre gruplanmış (show_stage_detail()
    # popup'ının mobil karşılığı -- satır ~10176-10187).
    factors_by_stage = {}
    for a in report.get("answers", []):
        stage = a.get("stage", "")
        factors_by_stage.setdefault(stage, []).append(a)

    pros_combined = report.get("signal_pros", []) + report.get("risk_pros", [])
    cons_combined = report.get("signal_cons", []) + report.get("risk_cons", [])

    return {
        "stage_rows": stage_rows,
        "auto_count": auto_count,
        "manual_count": manual_count,
        "factors_by_stage": factors_by_stage,
        "stage_order": signal_titles + risk_titles,
        "pros": pros_combined,
        "cons": cons_combined,
    }


@app.route("/", methods=["GET"])
def index():
    return render_template("index.html", coins=VALIDATION_UNIVERSE)


@app.route("/analyze", methods=["POST"])
def analyze():
    custom = (request.form.get("symbol_custom") or "").strip().upper()
    symbol = custom or (request.form.get("symbol") or "").strip().upper()
    if not symbol:
        return render_template("index.html", coins=VALIDATION_UNIVERSE,
                                error="Coin sembolü girin.")
    if symbol.startswith("TEST_") or symbol == "BTC_MOCK":
        # Test profilleri web'den desteklenmiyor -- History'ye zaten hiç
        # kaydedilmiyorlar (save_to_history() aynı kuralı desktop'ta da
        # uyguluyor) ve web tarafında mock_fetcher hiç sağlanmıyor.
        return render_template("index.html", coins=VALIDATION_UNIVERSE,
                                error="Test profilleri web'den desteklenmiyor.")
    try:
        result = kss.run_level1_core(symbol)
    except Exception as e:
        # run_level1_core() KENDİ try/except'ini kurmaz (bkz. docstring) --
        # hata semantiği burada, çağıran katmanda ele alınır.
        return render_template("index.html", coins=VALIDATION_UNIVERSE,
                                error=f"Analiz başarısız: {symbol} — {e}")

    report = result["report"]
    token = _stash_pending(symbol, result)
    view = _build_result_view(report)
    return render_template("result.html", symbol=symbol, report=report,
                            model_d_label=kss.MODEL_D_RESTRICTED_LABEL,
                            model_d_explanation=kss.MODEL_D_RESTRICTED_EXPLANATION,
                            token=token, **view)


@app.route("/save", methods=["POST"])
def save():
    token = request.form.get("token", "")
    pending = _PENDING_REPORTS.pop(token, None)
    if not pending:
        return render_template("index.html", coins=VALIDATION_UNIVERSE,
                                error="Analiz süresi doldu, tekrar analiz edin.")
    symbol = pending["symbol"]
    report = pending["result"]["report"]

    if symbol.startswith("TEST_") or symbol == "BTC_MOCK":
        return render_template("index.html", coins=VALIDATION_UNIVERSE,
                                error="Test profilleri geçmişe kaydedilmez.")

    # Masaüstü save_to_history() ile BİREBİR AYNI contract (satır
    # ~10537-10569, bu oturumda canlı doğrulandı): level="Level 1 — Otomatik",
    # data_source="binance+coingecko+cmc+news", analysis_time/analysis_price
    # report'ta ZATEN sabitlenmiş (run_level1_core() içinde
    # capture_analysis_snapshot() ile), burada yeniden hesaplanmıyor.
    db = kss.HistoryDB()
    result = db.save_analysis(
        symbol, "Level 1 — Otomatik", report,
        analysis_price=report.get("analysis_price"),
        data_source="binance+coingecko+cmc+news",
        analysis_time=report.get("analysis_time"),
    )
    if result == -1:
        message = "Bu coin için bu dakikada zaten kayıt var."
    else:
        message = f"Kaydedildi (ID: {result})."
    view = _build_result_view(report)
    return render_template("result.html", symbol=symbol, report=report,
                            model_d_label=kss.MODEL_D_RESTRICTED_LABEL,
                            model_d_explanation=kss.MODEL_D_RESTRICTED_EXPLANATION,
                            token=None, saved_message=message, **view)


@app.route("/history", methods=["GET"])
def history():
    db = kss.HistoryDB()
    rows = db.get_all_analyses()
    items = []
    for row in rows:
        try:
            answers = json.loads(row["answers"]) if row["answers"] else []
        except (json.JSONDecodeError, TypeError):
            answers = []
        risk_reliable = kss.compute_risk_reliable(answers)
        if row["risk_score"] is None or not risk_reliable:
            risk_display = "Yetersiz veri"
        else:
            risk_display = f"{row['risk_score']:.1f}%"
        verdict_display = kss.history_table_verdict_text(row["verdict"], row.get("model_d_reason"))
        tr = db.get_tracking(row["id"], horizon_hours=24)
        items.append({
            "id": row["id"],
            "symbol": row["symbol"],
            "time": (row["analysis_time"] or "")[:16].replace("T", " "),
            "level": row["level"],
            "signal_score": row["signal_score"],
            "risk_display": risk_display,
            "verdict_display": verdict_display,
            "veto": bool(row["vetos"] and row["vetos"] != "[]"),
            "coverage": row["coverage"],
            "tracking_24h": tr,
        })
    return render_template("history.html", items=items)


@app.route("/history/<int:analysis_id>", methods=["GET"])
def history_detail(analysis_id):
    db = kss.HistoryDB()
    row = db.get_analysis_by_id(analysis_id)
    if not row:
        return render_template("history.html", items=[], error=f"Kayıt bulunamadı: #{analysis_id}")

    try:
        answers = json.loads(row["answers"]) if row["answers"] else []
    except (json.JSONDecodeError, TypeError):
        answers = []
    risk_reliable = kss.compute_risk_reliable(answers)

    # show_history_detail() ile BİREBİR AYNI mantık (satır ~10597-10700, bu
    # oturumda canlı doğrulandı): Model D iki-katmanlı sunum yalnız
    # model_d_reason == "model_d_candidate_confirmed" iken, entry_timing eski
    # kayıtlarda None olabilir, disabled/normal ayrımı.
    is_model_d = row.get("model_d_reason") == "model_d_candidate_confirmed"
    try:
        vetos = json.loads(row["vetos"]) if row["vetos"] else []
    except (json.JSONDecodeError, TypeError):
        vetos = []
    try:
        stage_scores = json.loads(row["stage_scores"]) if row["stage_scores"] else {}
    except (json.JSONDecodeError, TypeError):
        stage_scores = {}

    disabled_answers = [a for a in answers if a.get("disabled")]
    normal_answers = [a for a in answers if not a.get("disabled")]
    answers_by_stage = {}
    for a in normal_answers:
        answers_by_stage.setdefault(a.get("stage", ""), []).append(a)

    tracking_rows = [db.get_tracking(analysis_id, h) for h in (1, 6, 12, 24, 48)]
    tracking_rows = [t for t in tracking_rows if t]

    return render_template(
        "history_detail.html", row=row, risk_reliable=risk_reliable,
        is_model_d=is_model_d, model_d_label=kss.MODEL_D_RESTRICTED_LABEL,
        vetos=vetos, stage_scores=stage_scores,
        answers_by_stage=answers_by_stage, disabled_answers=disabled_answers,
        tracking_rows=tracking_rows,
    )


@app.route("/history/update-tracking", methods=["POST"])
def update_tracking():
    # refresh_all_tracking() ile BİREBİR AYNI mantık (satır ~10762-10775, bu
    # oturumda canlı doğrulandı): calculate_tracking() + save_tracking(),
    # ayrı bir tracking algoritması YOK.
    db = kss.HistoryDB()
    rows = db.get_all_analyses()
    updated = 0
    for row in rows:
        price, atime = row.get("analysis_price"), row.get("analysis_time")
        if not price or not atime:
            continue
        results = kss.BinanceClient.calculate_tracking(row["symbol"], atime, price)
        if results:
            db.save_tracking(row["id"], results)
            updated += 1
    return redirect(url_for("history", updated=updated))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=False)
