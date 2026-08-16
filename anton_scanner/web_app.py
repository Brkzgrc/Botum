# -*- coding: utf-8 -*-
"""
MOBILE WEB MVP -- Phase 1 (onaylı, controlled implementation).

İnce Flask katmanı: mevcut V6 motorunu (`run_level1_core()`, Level1Worker.run()
ile PAYLAŞILAN aynı canonical execution path -- bkz. kripto_sinyal_sistemi_v6_pro_gui.pyw)
GUI olmadan, telefon tarayıcısından çağırır. Motor koduna (.pyw) hiçbir yeni
karar/skor mantığı eklenmedi -- bu dosya yalnız run_level1_core()'u çağırıp
sonucu HTML olarak render eder.

Kapsam (Phase 1, kasıtlı dar): yalnız GET / (coin seçimi) ve POST /analyze
(Level 1 çalıştır, sonucu göster). /save, History, tracking, Level 2/3,
AI Analyst UI, authentication, queue/polling YOK -- bunlar sonraki fazlar.

İlk implementasyon SENKRON -- bir /analyze isteği tamamlanana kadar (gerçek
Level 1 analizi ağ çağrıları dahil canlı ölçümle ~26s sürebiliyor, bkz. Final
Report) tarayıcı bekler. Render'a deploy edilmeden gerçek timeout davranışı
görülmeden queue/polling altyapısı EKLENMEDİ (kasıtlı, talimat gereği).

Çalıştırma (lokal):
    pip install flask
    python web_app.py
    -> http://127.0.0.1:5000
"""
import importlib.util
import os
import sys

from flask import Flask, render_template, request

_PROD_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "kripto_sinyal_sistemi_v6_pro_gui.pyw")


def _load_kss_module():
    """Production .pyw'yi Qt penceresi açmadan modül olarak yükler -- bu
    oturumdaki 273 permanent testin tamamının kullandığı AYNI teknik
    (importlib.util.spec_from_file_location). main()'in yalnız
    `if __name__ == "__main__":` altında çağrıldığı doğrulanmıştı (Final
    Report, madde 1) -- bu yüzden import hiçbir QApplication/pencere
    oluşturmaz."""
    spec = importlib.util.spec_from_file_location("kss", _PROD_FILE)
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
        # Phase 1: test profilleri web'den desteklenmiyor -- History'ye zaten
        # hiç kaydedilmiyorlar (save_to_history() aynı kuralı desktop'ta da
        # uyguluyor) ve web tarafında mock_fetcher hiç sağlanmıyor.
        return render_template("index.html", coins=VALIDATION_UNIVERSE,
                                error="Test profilleri web'den desteklenmiyor.")
    try:
        result = kss.run_level1_core(symbol)
    except Exception as e:
        # run_level1_core() KENDİ try/except'ini kurmaz (bkz. docstring) --
        # hata semantiği burada, çağıran katmanda ele alınır. Geçersiz
        # sembol/ağ hatası kullanıcıya kısa bir mesajla, stack trace
        # GÖSTERİLMEDEN bildirilir.
        return render_template("index.html", coins=VALIDATION_UNIVERSE,
                                error=f"Analiz başarısız: {symbol} — {e}")

    report = result["report"]
    return render_template("result.html", symbol=symbol, report=report,
                            model_d_label=kss.MODEL_D_RESTRICTED_LABEL)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=False)
