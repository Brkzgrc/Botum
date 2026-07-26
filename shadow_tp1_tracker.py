# -*- coding: utf-8 -*-
"""
Shadow TP1 Tracker — "+1.0% TP1 tavan" adayının sanal (dry-run) izleme sayfası
================================================================================
Bu modül SADECE görüntüleme ve kayıt yapar. Hiçbir simülasyon/karar mantığı
burada YOK — o mantık position_monitor.py'de (gerçek pozisyonu en doğru bilen
dosya, oradaki _shadow_evaluate/_shadow_build_close_event/_shadow_dispatch).
Bu modül, position_monitor.py'nin POST ettiği olayları
DATA_DIR/shadow_tp1_events.jsonl dosyasına ekler ve /shadow sayfasında
Türkçe bir özet + tablo olarak gösterir.

Ayrı ve sökülebilir: portfolio_tracker.py bu modülü sadece import edip
blueprint'i register ediyor + nav'a bir link ekliyor. Bu dosya silinirse
sadece /shadow sayfası ve /api/shadow-event endpoint'i kaybolur — Portföy/
Piyasa/Al-Sat Bot sekmeleri, gerçek işlem hesapları (win/loss, net P&L,
açık/kapanmış mantığı) hiç etkilenmez.

Shadow sistemi gerçek pozisyon kapatmaz, emir açmaz, stop/trailing değiştirmez
— bu dosya sadece "TP1 +1.0% tavan olsaydı ne olurdu?" gözlemini saklar/gösterir.
"""
import html
import json
import os
import threading
from collections import defaultdict

import requests
from flask import Blueprint, jsonify, request

DATA_DIR    = os.getenv("DATA_DIR", "/tmp")
EVENTS_FILE = os.path.join(DATA_DIR, "shadow_tp1_events.jsonl")
AUTH_TOKEN  = os.getenv("PORTFOLIO_AUTH_TOKEN", "")

# Şu an gerçekten açık olan pozisyonları çekmek için (event üretmemiş ama
# açık olanları da göstermek, ve shadow_valid'i canlı state'ten teyit etmek
# için) trading-bot'un kendi /status'una BAĞIMSIZ bir istek atıyoruz — aynı
# env var'lar portfolio_tracker.py'de de kullanılıyor. Ayrı/sökülebilir
# tasarım gereği kendi HTTP çağrısını yapıyor, portfolio_tracker.py'nin
# içine hiç dokunmuyor/import etmiyor.
TRADING_BOT_URL   = os.getenv("TRADING_BOT_URL", "")
TRADING_BOT_TOKEN = os.getenv("TRADING_BOT_TOKEN", "")

_lock = threading.Lock()

shadow_bp = Blueprint("shadow_tp1", __name__)


# ─── DEPOLAMA ──────────────────────────────────────────────────────────────
def _append_event(event: dict):
    with _lock:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(EVENTS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")


def _load_events():
    if not os.path.exists(EVENTS_FILE):
        return []
    events = []
    with open(EVENTS_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except Exception:
                continue
    return events


def _fetch_real_positions():
    """trading-bot'un CANLI /status'unu çeker -- şu an gerçekten açık olan
    pozisyonları (ve onların ham shadow_valid/shadow_tp1/shadow_trailing
    alanlarını) döndürür. Best-effort: herhangi bir hata/timeout'ta boş dict
    döner, sayfa asla bu yüzden çökmez."""
    if not TRADING_BOT_URL:
        return {}
    try:
        hdrs = {}
        if TRADING_BOT_TOKEN:
            hdrs["X-Bot-Token"] = TRADING_BOT_TOKEN
        r = requests.get(f"{TRADING_BOT_URL}/status", headers=hdrs, timeout=5)
        if r.status_code != 200:
            return {}
        data = r.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


# ─── API ENDPOINT (position_monitor.py buraya POST eder) ───────────────────
@shadow_bp.route("/api/shadow-event", methods=["POST"])
def api_shadow_event():
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    if AUTH_TOKEN and token != AUTH_TOKEN:
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    if not data.get("symbol") or not data.get("event"):
        return jsonify({"error": "missing fields"}), 400
    _append_event(data)
    return jsonify({"ok": True}), 201


# ─── SEMBOL BAZLI ÖZET (üstteki "Aktif Shadow Karşılaştırması" paneli) ─────
# Ham event log'u ("Detaylı Olay Günlüğü") olduğu gibi kalıyor, ama ana
# panelde kullanıcı her sembol için TEK bir "şu an ne durumda" satırı görmek
# istiyor — event bazlı değil, sembol bazlı. Aynı sembolde işlem kapanıp
# yeniden açılabileceği için (sıralı, asla eş zamanlı — position_monitor
# aynı sembolde ikinci pozisyon açmaz), sadece sembole göre gruplamak eski
# bir işlemin olaylarını yeni işlemle karıştırabilirdi; bu yüzden her
# sembolün SADECE en son (devam eden ya da en son kapanan) işlem döngüsü
# alınıyor.
def _latest_cycle_events(evs):
    close_positions = [i for i, e in enumerate(evs) if e.get("event") == "SHADOW_LIVE_CLOSED"]
    if not close_positions:
        return evs   # hiç kapanış yok -- tek (devam eden) döngü, hepsi bu
    last_close_idx = close_positions[-1]
    if last_close_idx == len(evs) - 1:
        # En son olan şey bir kapanış: bu döngü bir ÖNCEKİ kapanıştan (varsa)
        # sonra başlamış, o kapanışa kadar (dahil) alınıyor.
        start = close_positions[-2] + 1 if len(close_positions) >= 2 else 0
        return evs[start:last_close_idx + 1]
    # Son kapanıştan SONRA yeni event'ler var: yeni bir döngü (yeniden açılmış
    # pozisyon) başlamış, sadece ondan sonrasını al.
    return evs[last_close_idx + 1:]


_KARAR_LABELS = {
    "sanal_onde":            "Sanal Önde",
    "gercek_onde":           "Gerçek Önde",
    "belirsiz":              "Belirsiz",
    "sonuclanamadi":         "Sonuçlanamadı",
    "sanal_izleniyor_orphan": "Sanal İzleniyor (Gerçek Kapandı)",
    "shadow_zaman_asimi":    "Shadow Zaman Aşımı",
    "ayni_mum_riski":        "Aynı Mum Riski",
    "izleniyor":             "İzleniyor",
    "gecersiz":              "Geç Başladı / Geçersiz",
}
_KARAR_COLORS = {
    "sanal_onde":            "#2ecc71",
    "gercek_onde":           "#f39c12",
    "belirsiz":              "#8a9bb0",
    "sonuclanamadi":         "#6c7a89",
    "sanal_izleniyor_orphan": "#3498db",
    "shadow_zaman_asimi":    "#95756b",
    "ayni_mum_riski":        "#e74c3c",
    "izleniyor":             "#3498db",
    "gecersiz":              "#5a6472",
}

# Gerçek pozisyon kapandığında sanal (shadow) hâlâ sonuçlanmamışsa, o coin
# state["positions"]'tan tamamen ayrı bir listede (position_monitor.py
# "shadow_orphans") sonuçlanana ya da zaman aşımına uğrayana kadar izlenmeye
# devam eder. Bu event tipleri o takibin NİHAİ sonucunu taşır.
_ORPHAN_EVENT_TYPES = {"SHADOW_ORPHAN_RESOLVED", "SHADOW_ORPHAN_TIMEOUT"}


def _symbol_summary(symbol, cycle_events, orphan_by_id=None):
    """cycle_events: bir sembolün EN SON işlem döngüsüne ait event'leri,
    kronolojik sırada (en az 1 tane). orphan_by_id: orphan_id -> SHADOW_ORPHAN_
    RESOLVED/TIMEOUT event'i (bkz. _group_symbol_summaries — bu event'ler
    cycle_events'İN DIŞINDA tutulur, aksi halde aynı sembolde YENİ bir gerçek
    pozisyon açılmışsa eski orphan'ın geç gelen sonucu yeni pozisyonun
    satırıyla karışır). Dönüş: panelde tek satır olacak özet.

    Karar öncelik sırası:
      1) Gerçek kapandıysa (SHADOW_LIVE_CLOSED):
         a) shadow zaten sonuçlanmış olarak kapandıysa (shadow_exit dolu) ->
            fark_pct'e göre KESİN karar (bu her zaman en güncel/en
            bilgilendirici durum, önceki bir aynı-mum uyarısını bile ezer).
         b) shadow sonuçlanmadan kapandıysa (fark_pct None, "undetermined")
            ama bir orphan_id ile ayrıca izlenmeye alındıysa:
              - orphan_by_id'de bu id için SHADOW_ORPHAN_RESOLVED varsa ->
                ARTIK fark_pct hesaplanabilir, KESİN karara dönüşür (Sanal
                Sonuçlandı — shadow_status="orphan_resolved").
              - SHADOW_ORPHAN_TIMEOUT varsa -> Shadow Zaman Aşımı, hiçbir
                zaman kesin karara dönüşmez.
              - ikisi de yoksa (henüz izleniyor) -> Sanal İzleniyor (Gerçek
                Kapandı) — geçici bir bekleme durumu, "kalıcı belirsiz" değil.
         c) orphan_id hiç yoksa (kapasite dolu, izlenemedi) -> Sonuçlanamadı
            (kalıcı, bir daha asla netleşmeyecek).
      2) Gerçek hâlâ açıksa ve EN SON event aynı-mum riskiyse -> Aynı Mum Riski.
      3) Gerçek hâlâ açık, sanal çoktan sonuçlandıysa (exited) -> Sanal Önde
         ("önde" = zaman olarak sonuca ulaşmış, sayısal üstünlük iddiası değil),
         alt not: gerçek kapanış bekleniyor.
      4) Gerçek hâlâ açık, sanal hâlâ trailing'deyse -> İzleniyor.
      5) Diğer tüm durumlar (henüz hiçbir şey olmadı) -> Belirsiz.
    """
    orphan_by_id = orphan_by_id or {}
    last = cycle_events[-1]
    is_real_closed = last.get("event") == "SHADOW_LIVE_CLOSED"

    # Sanal getiri: son event'te yoksa (örn. sadece ACTIVATE olduysa), bu
    # döngüde geriye doğru en son bilinen değeri ara.
    shadow_pct = last.get("shadow_pct")
    if shadow_pct is None:
        for e in reversed(cycle_events):
            if e.get("shadow_pct") is not None:
                shadow_pct = e.get("shadow_pct")
                break

    real_pct = last.get("live_pct") if is_real_closed else None
    fark_pct = last.get("fark_pct") if is_real_closed else None
    shadow_status = last.get("shadow_status")
    karar_note = ""

    if is_real_closed:
        orphan_event = orphan_by_id.get(last.get("orphan_id")) if last.get("orphan_id") else None
        if orphan_event is not None:
            if orphan_event.get("event") == "SHADOW_ORPHAN_TIMEOUT":
                karar = "shadow_zaman_asimi"
                karar_note = orphan_event.get("note", "")
                shadow_status = "orphan_timeout"
                shadow_pct = None
                fark_pct = None
            else:  # SHADOW_ORPHAN_RESOLVED — sanal artık sonuçlandı
                shadow_pct = orphan_event.get("shadow_pct")
                fark_pct = orphan_event.get("fark_pct")
                shadow_status = "orphan_resolved"
                karar_note = orphan_event.get("note", "")
                if fark_pct is not None and fark_pct > 0:
                    karar = "sanal_onde"
                elif fark_pct is not None and fark_pct < 0:
                    karar = "gercek_onde"
                else:
                    karar = "belirsiz"
        elif last.get("orphan_id"):
            # Orphan kaydedildi ama henüz sonuçlanmadı/zaman aşımına uğramadı —
            # GEÇİCİ bir bekleme, "sonuclanamadi"dan farklı: burada hâlâ bir
            # ihtimal var, orada YOK.
            karar = "sanal_izleniyor_orphan"
            karar_note = "Gerçek kapandı, sanal ayrıca izleniyor — henüz sonuçlanmadı."
            shadow_status = "orphan_tracking"
            shadow_pct = None
        elif fark_pct is None:
            karar = "sonuclanamadi"
            karar_note = "Gerçek kapandı, sanal kendi çıkışına ulaşmadan fiyat takibi kesildi."
            shadow_pct = None   # geriye dönük bulunmuş olsa bile eski/donuk değeri gösterme
        elif fark_pct > 0:
            karar = "sanal_onde"
        elif fark_pct < 0:
            karar = "gercek_onde"
        else:
            karar = "belirsiz"
    elif last.get("event") == "SHADOW_SAME_CANDLE_TOUCH_AND_BREACH":
        karar = "ayni_mum_riski"
    elif shadow_status == "exited":
        # Sanal sonuçlandı ama gerçek HÂLÂ AÇIK — Sanal Önde/Gerçek Önde
        # SADECE ikisi de sonuçlandığında hesaplanır (onaylanan mimari, 8.
        # madde). Gerçek kapanınca is_real_closed dalı devreye girip kesin
        # karara (sanal_onde/gercek_onde/belirsiz) dönüşecek.
        karar = "izleniyor"
        karar_note = "Sanal sonuçlandı, gerçek kapanış bekleniyor."
    elif shadow_status == "trailing":
        karar = "izleniyor"
    else:
        karar = "belirsiz"

    # Adil kıyas şartı: shadow SADECE gerçek fill anından itibaren, kesintisiz
    # takip edildiyse geçerli sayılır (position_monitor.py _activate_position
    # tarafından shadow_valid=True damgalanır). Bu event'te shadow_valid
    # AÇIKÇA True değilse (yok ya da False) — bu deploy'dan önce açılmış eski
    # bir pozisyon ya da doğrulanamayan bir kayıt demektir — Sanal Önde/Gerçek
    # Önde/Belirsiz gibi "kıyaslanabilir" bir karara ASLA dönüştürülmez,
    # istatistiklere de katılmaz.
    if last.get("shadow_valid") is not True:
        karar = "gecersiz"
        karar_note = "Bu pozisyon shadow sistemi devreye girmeden önce açılmış olabilir — kıyas güvenilir değil."

    return {
        "symbol": symbol, "ts": last.get("ts"),
        "real_status": last.get("real_status"), "shadow_status": shadow_status,
        "shadow_pct": shadow_pct, "real_pct": real_pct, "fark_pct": fark_pct,
        "karar": karar, "karar_note": karar_note,
    }


def _group_symbol_summaries(events):
    # Orphan sonuç event'leri (SHADOW_ORPHAN_RESOLVED/TIMEOUT) BİLEREK normal
    # döngü akışının DIŞINDA tutulur: bunlar geç (gerçek kapanıştan günler
    # sonra) gelebilir, ve o arada aynı sembolde YENİ bir gerçek pozisyon
    # açılmış olabilir. Eğer bu event'ler normal listeye karışsaydı,
    # _latest_cycle_events onları "en son event" sanıp YENİ pozisyonun hâlâ
    # devam eden durumunu ESKİ (kapanmış) işlemin sonucuyla EZERDİ. Bunun
    # yerine orphan_id ile eşleştirilip SADECE kendi eski döngüsüne (varsa,
    # hâlâ o döngü "güncel" ise) uygulanıyor — bkz. _symbol_summary.
    orphan_by_id = {}
    normal_events = []
    for e in events:
        if e.get("event") in _ORPHAN_EVENT_TYPES and e.get("orphan_id"):
            orphan_by_id[e["orphan_id"]] = e
        else:
            normal_events.append(e)

    by_symbol = defaultdict(list)
    for e in normal_events:
        sym = e.get("symbol")
        if sym:
            by_symbol[sym].append(e)
    summaries = []
    for sym, evs in by_symbol.items():
        cycle = _latest_cycle_events(evs)
        if cycle:
            summaries.append(_symbol_summary(sym, cycle, orphan_by_id))
    summaries.sort(key=lambda s: s.get("ts") or "", reverse=True)
    return summaries


def _merge_with_real_positions(summaries, real_positions):
    """Event log'undan gelen özetlere, event ÜRETMEMİŞ ama şu an GERÇEKTEN
    açık olan pozisyonları da ekler. Örn. yeni fill olmuş, shadow_tp1'e daha
    hiç ulaşmamış bir pozisyon event log'unda hiç görünmez ama panelde
    "İzleniyor" olarak listelenmeli. Ayrıca canlı state'teki shadow_valid,
    event log'undaki (bazen eski/eksik) veriden daha güncel/güvenilir olduğu
    için varsa öncelik ona verilir."""
    known = {s["symbol"] for s in summaries}
    for sym, pos in (real_positions or {}).items():
        if not isinstance(pos, dict) or sym in known:
            continue
        shadow_valid = pos.get("shadow_valid") is True
        if not pos.get("shadow_tp1") or not shadow_valid:
            karar = "gecersiz"
            karar_note = "Bu pozisyon shadow sistemi devreye girmeden önce açılmış olabilir — kıyas güvenilir değil."
            shadow_status = "not_trailing"
        else:
            karar = "izleniyor"
            karar_note = ""
            shadow_status = "trailing" if pos.get("shadow_trailing") else "not_trailing"
        summaries.append({
            "symbol": sym, "ts": pos.get("open_time") or "",
            "real_status": "trailing" if pos.get("trailing") else "pre_tp1",
            "shadow_status": shadow_status,
            "shadow_pct": None, "real_pct": None, "fark_pct": None,
            "karar": karar, "karar_note": karar_note,
        })
    summaries.sort(key=lambda s: s.get("ts") or "", reverse=True)
    return summaries


def _summarize_symbols(summaries, real_positions=None):
    """Kartlar bilerek "kaç pozisyon var" ile "kaçı gerçekten geçerli test
    örneği" sorularını AYIRIYOR — aksi halde "İzlenen: 10" ile "Geçersiz: 10"
    aynı anda görününce sanki 10 geçerli test varmış gibi yanlış anlaşılıyor.
    Gerçek Açık = trading-bot'un CANLI /status'undaki gerçek açık pozisyon
    sayısı (len(summaries) DEĞİL — summaries event log + real_positions
    birleşimi olduğu için, event log'da kalmış ama gerçekte artık KAPANMIŞ
    bir sembol varsa len(summaries) şişebilirdi). Geçerli İzlenen =
    shadow_valid olanlar (İzleniyor/Sanal Önde/Gerçek Önde/Belirsiz/Aynı Mum
    Riski'nin TOPLAMI). Geçersiz/Eski = ayrı, dışlanmış grup. Belirsiz,
    Sonuçlanamadı ve Shadow Zaman Aşımı (üçü de "kıyaslanabilir kesin bir
    karara varılamadı, bir daha da varılamayacak" anlamına geldiği için) TEK
    bir kartta birlikte sayılıyor. Orphan İzleniyor ayrı: bu GEÇİCİ bir
    bekleme (gerçek kapandı, sanal ayrıca hâlâ izleniyor) — mekanizmanın
    sağlıklı çalıştığını (biriken/unutulan kayıt olmadığını) izlemek için
    ayrı bir kartta gösteriliyor."""
    valid = [s for s in summaries if s["karar"] != "gecersiz"]
    return {
        "gercek_acik":     len(real_positions) if real_positions is not None else len(summaries),
        "gecerli_izlenen": len(valid),
        "gecersiz":        sum(1 for s in summaries if s["karar"] == "gecersiz"),
        "sanal_cikis":     sum(1 for s in valid if s["shadow_status"] == "exited"),
        "sanal_onde":      sum(1 for s in valid if s["karar"] == "sanal_onde"),
        "gercek_onde":     sum(1 for s in valid if s["karar"] == "gercek_onde"),
        "ayni_mum_riski":  sum(1 for s in valid if s["karar"] == "ayni_mum_riski"),
        "orphan_izleniyor": sum(1 for s in valid if s["karar"] == "sanal_izleniyor_orphan"),
        "belirsiz_sonuclanamadi": sum(
            1 for s in valid if s["karar"] in ("belirsiz", "sonuclanamadi", "shadow_zaman_asimi")),
    }


# ─── GÖRÜNTÜLEME YARDIMCILARI ────────────────────────────────────────────
_EVENT_LABELS = {
    "SHADOW_WOULD_ACTIVATE_TRAIL":         "Sanal Trail Aktif",
    "SHADOW_WOULD_EXIT_TRAIL":             "Sanal Çıkış",
    "SHADOW_SAME_CANDLE_TOUCH_AND_BREACH": "Aynı Mum Riski",
    "SHADOW_LIVE_CLOSED":                  "Gerçek Kapandı",
}
_EVENT_COLORS = {
    "SHADOW_WOULD_ACTIVATE_TRAIL":         "#3498db",
    "SHADOW_WOULD_EXIT_TRAIL":             "#f39c12",
    "SHADOW_SAME_CANDLE_TOUCH_AND_BREACH": "#e74c3c",
    "SHADOW_LIVE_CLOSED":                  "#2ecc71",
}
_STATUS_LABELS = {
    "pre_tp1":               "TP1 öncesi",
    "trailing":              "Trailing",
    "not_trailing":          "Henüz değil",
    "exited":                "Çıktı",
    "orphan_tracking":       "İzleniyor (Gerçek Kapandı)",
    "orphan_resolved":       "Sanal Sonuçlandı",
    "orphan_timeout":        "Zaman Aşımı",
    "closed:stop_hit":       "Kapandı (Stop)",
    "closed:trail_stop":     "Kapandı (Trail)",
    "closed:trail_binance":  "Kapandı (Trail/Binance)",
    "closed:expire":         "Kapandı (Süre)",
    "closed:expire_no_tick": "Kapandı (Süre/tick'siz)",
    "closed:sl_binance":     "Kapandı (Stop/Binance)",
}
# Portföy sayfasındaki pct_color/status_badge ile aynı renk dili: yeşil=iyi,
# kırmızı=kötü, turuncu=süre/expire, gri=nötr/henüz belirsiz, mavi=aktif ilerleme.
_STATUS_COLORS = {
    "pre_tp1":               "#8a9bb0",
    "trailing":              "#3498db",
    "not_trailing":          "#8a9bb0",
    "exited":                "#9b59b6",
    "orphan_tracking":       "#3498db",
    "orphan_resolved":       "#9b59b6",
    "orphan_timeout":        "#95756b",
    "closed:stop_hit":       "#e74c3c",
    "closed:trail_stop":     "#2ecc71",
    "closed:trail_binance":  "#2ecc71",
    "closed:expire":         "#f39c12",
    "closed:expire_no_tick": "#f39c12",
    "closed:sl_binance":     "#e74c3c",
}


def _fmt(v, digits=6):
    if v is None:
        return "—"
    try:
        return f"{float(v):.{digits}g}"
    except Exception:
        return str(v)


def _fmt_pct(v):
    if v is None:
        return "—"
    try:
        return f"{float(v):+.2f}%"
    except Exception:
        return str(v)


def _pct_color(v):
    # Portföy sayfasındaki pct_color() ile aynı kural: pozitif yeşil, negatif
    # kırmızı, sıfır/bilinmiyor gri.
    if v is None:
        return "#8a9bb0"
    try:
        v = float(v)
    except Exception:
        return "#8a9bb0"
    return "#2ecc71" if v > 0 else ("#e74c3c" if v < 0 else "#8a9bb0")


def _status_label(v):
    # Bilinen bir kod değilse ham değeri gösteriyoruz (event kaynağı
    # position_monitor.py olsa da, HTML'e basılan her string escape edilir).
    return html.escape(str(_STATUS_LABELS.get(v, v or "—")))


def _status_badge(v):
    color = _STATUS_COLORS.get(v, "#7f8c8d")
    return f'<span style="color:{color};font-size:.68rem;white-space:nowrap">{_status_label(v)}</span>'


def _karar_badge(karar):
    label = html.escape(str(_KARAR_LABELS.get(karar, karar or "—")))
    color = _KARAR_COLORS.get(karar, "#7f8c8d")
    return f'<span style="color:{color};font-weight:bold;font-size:.72rem;white-space:nowrap">{label}</span>'


def _summary_row_html(s):
    sym = html.escape(str(s["symbol"]).replace("/USDT", ""))
    shadow_pct, real_pct, fark_pct = s["shadow_pct"], s["real_pct"], s["fark_pct"]
    karar_note = s.get("karar_note") or ""
    note_html = (f'<br><span style="font-size:.6rem;color:#7f8c8d">{html.escape(karar_note)}</span>'
                 if karar_note else "")
    return f"""<tr>
      <td><b>{sym}</b></td>
      <td>{_status_badge(s["real_status"])}</td>
      <td>{_status_badge(s["shadow_status"])}</td>
      <td style="color:{_pct_color(shadow_pct)};font-weight:bold">{_fmt_pct(shadow_pct)}</td>
      <td style="color:{_pct_color(real_pct)};font-weight:bold">{_fmt_pct(real_pct)}</td>
      <td style="color:{_pct_color(fark_pct)};font-weight:bold">{_fmt_pct(fark_pct)}</td>
      <td>{_karar_badge(s["karar"])}{note_html}</td>
    </tr>"""


def _row_html(e):
    sym = html.escape(str(e.get("symbol", "")).replace("/USDT", ""))
    ts = str(e.get("ts", ""))[:19].replace("T", " ")   # ISO timestamp — serbest metin değil
    event_key = e.get("event", "")
    event_label = html.escape(str(_EVENT_LABELS.get(event_key, event_key)))
    event_color = _EVENT_COLORS.get(event_key, "#7f8c8d")
    note = html.escape(str(e.get("note", "") or ""))
    shadow_pct = e.get("shadow_pct")

    return f"""<tr>
      <td style="color:#7f8c8d;font-size:.65rem">{ts}</td>
      <td><b>{sym}</b></td>
      <td>{_status_badge(e.get("real_status"))}</td>
      <td>{_status_badge(e.get("shadow_status"))}</td>
      <td>{_fmt(e.get("real_tp1"))}</td>
      <td>{_fmt(e.get("shadow_tp1"))}</td>
      <td>{_fmt(e.get("shadow_peak"))}</td>
      <td>{_fmt(e.get("shadow_trail"))}</td>
      <td style="color:{_pct_color(shadow_pct)};font-weight:bold">{_fmt_pct(shadow_pct)}</td>
      <td><span style="color:{event_color};font-size:.6rem;white-space:nowrap">{event_label}</span></td>
      <td style="font-size:.65rem;color:#7f8c8d;white-space:normal;max-width:320px">{note}</td>
    </tr>"""


# ─── SAYFA ────────────────────────────────────────────────────────────────
@shadow_bp.route("/shadow")
def shadow_page():
    events = _load_events()
    summaries = _group_symbol_summaries(events)
    real_positions = _fetch_real_positions()
    summaries = _merge_with_real_positions(summaries, real_positions)
    stats = _summarize_symbols(summaries, real_positions)

    # Ana tabloda SADECE geçerli (shadow_valid=True) pozisyonlar gösterilir —
    # geçersiz/eski pozisyonlar test verisi değil, sadece "eski pozisyon var"
    # bilgisi, o yüzden ayrı ve varsayılan kapalı bir bölüme taşınıyor.
    valid_summaries = [s for s in summaries if s["karar"] != "gecersiz"]
    invalid_summaries = [s for s in summaries if s["karar"] == "gecersiz"]

    if valid_summaries:
        summary_table_html = f"""<div class="table-wrap"><table><thead><tr>
  <th>Sembol</th><th>Gerçek Durum</th><th>Sanal Durum</th>
  <th>Sanal Getiri</th><th>Gerçek Getiri</th><th>Fark</th><th>Karar</th>
</tr></thead><tbody>
{''.join(_summary_row_html(s) for s in valid_summaries)}
</tbody></table></div>"""
    else:
        summary_table_html = """<div class="empty-panel">
  <p>Henüz geçerli shadow işlemi yok.</p>
  <p>Shadow sistemi devreye girdikten sonra fill olan yeni pozisyonlar burada görünecek.</p>
  <p>Eski açık pozisyonlar kıyasa dahil edilmiyor.</p>
</div>"""

    invalid_section_html = ""
    if invalid_summaries:
        invalid_section_html = f"""<details class="detail-log">
  <summary>Eski / Kapsam Dışı Gerçek Pozisyonlar ({len(invalid_summaries)})</summary>
  <div class="table-wrap"><table><thead><tr>
    <th>Sembol</th><th>Gerçek Durum</th><th>Sanal Durum</th>
    <th>Sanal Getiri</th><th>Gerçek Getiri</th><th>Fark</th><th>Karar</th>
  </tr></thead><tbody>
  {''.join(_summary_row_html(s) for s in invalid_summaries)}
  </tbody></table></div>
</details>"""

    recent = list(reversed(events[-300:]))
    rows = "".join(_row_html(e) for e in recent) or (
        '<tr><td colspan="11" style="text-align:center;color:#7f8c8d;padding:20px">'
        'Henüz olay yok.</td></tr>'
    )

    return f"""<!DOCTYPE html>
<html lang="tr"><head>
<meta charset="UTF-8"><title>TP1 Shadow</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="30">
<style>
:root{{--bg:#0a0e14;--card:#0f1319;--border:#1e2a3a;--text:#c9d1d9;--text-dim:#7f8c8d;
  --accent:#00b4d8;--green:#2ecc71;--red:#e74c3c;--orange:#f39c12;}}
*{{box-sizing:border-box;margin:0;padding:0;}}
body{{background:var(--bg);color:var(--text);font-family:'JetBrains Mono','Fira Code','Consolas',monospace;
  padding:20px;max-width:1400px;margin:0 auto;line-height:1.5;}}
.header{{display:flex;justify-content:space-between;align-items:center;margin-bottom:24px;
  padding-bottom:16px;border-bottom:1px solid var(--border);flex-wrap:wrap;gap:10px;}}
.header h1{{color:var(--accent);font-size:1.1rem;letter-spacing:3px;}}
.header .time{{color:var(--text-dim);font-size:.75rem;display:flex;align-items:center;gap:10px;}}
.btn-refresh{{background:#1a472a;color:#2ecc71;border:1px solid #2ecc7166;border-radius:4px;
  padding:3px 10px;font-size:.65rem;cursor:pointer;font-family:inherit;}}
.nav-tab{{background:#0f1319;border:1px solid var(--border);color:var(--text-dim);padding:3px 14px;
  border-radius:4px;text-decoration:none;font-size:.65rem;letter-spacing:.8px;transition:all .15s;}}
.nav-tab:hover,.nav-tab.active{{border-color:var(--accent);color:var(--accent);background:#00b4d811;}}
.cards{{display:grid;grid-template-columns:repeat(9,1fr);gap:10px;margin-bottom:24px;}}
.card{{background:var(--card);border:1px solid var(--border);border-radius:6px;padding:14px;text-align:center;}}
.card .val{{font-size:1.3rem;font-weight:bold;display:block;margin-bottom:4px;}}
.card .lbl{{font-size:.55rem;color:var(--text-dim);text-transform:uppercase;letter-spacing:1px;}}
table{{width:100%;border-collapse:collapse;font-size:.7rem;}}
th{{background:var(--card);color:var(--text-dim);padding:8px 8px;text-align:left;
  font-size:.58rem;letter-spacing:.5px;border-bottom:1px solid var(--border);white-space:nowrap;}}
td{{padding:7px 8px;border-bottom:1px solid #111820;white-space:nowrap;}}
tr:hover td{{background:#0f151d;}}
.table-wrap{{overflow-x:auto;border:1px solid var(--border);border-radius:6px;}}
.note{{color:var(--text-dim);font-size:.65rem;margin-bottom:12px;font-style:italic;}}
.section-title{{font-size:.7rem;letter-spacing:1.5px;color:var(--accent);text-transform:uppercase;
  font-weight:700;margin:22px 0 10px;}}
.empty-panel{{background:var(--card);border:1px solid var(--border);border-radius:6px;
  padding:24px;text-align:center;color:var(--text-dim);font-size:.75rem;line-height:1.8;}}
.empty-panel p{{margin:0;}}
details.detail-log summary{{cursor:pointer;font-size:.7rem;letter-spacing:1.5px;color:var(--accent);
  text-transform:uppercase;font-weight:700;margin:22px 0 10px;list-style:none;}}
details.detail-log summary::-webkit-details-marker{{display:none;}}
details.detail-log summary::before{{content:"▸ ";}}
details.detail-log[open] summary::before{{content:"▾ ";}}
@media(max-width:700px){{.cards{{grid-template-columns:repeat(3,1fr);}}body{{padding:12px;}}}}
</style></head>
<body>

<div class="header">
  <div>
    <h1>🧪 TP1 +1.0 SANAL TEST</h1>
    <div style="display:flex;gap:6px;margin-top:6px">
      <a href="/" class="nav-tab">Portföy</a>
      <a href="/market" class="nav-tab">Piyasa</a>
      <a href="/alsat" class="nav-tab">Al-Sat Bot</a>
      <a href="/shadow" class="nav-tab active">TP1 Shadow</a>
    </div>
  </div>
  <span class="time">30s otomatik yenileme
    <button class="btn-refresh" onclick="location.reload()">🔄 Yenile</button>
  </span>
</div>

<p class="note">"TP1 +%1.0 tavan olsaydı ne olurdu?" sanal takibi — gerçek emirlere hiç karışmaz, sadece gözlem.
Kaynak: position_monitor.py (canlı, kapanmış 1m mum bazlı) → /api/shadow-event. Gerçek işlem hesapları (Portföy/Al-Sat Bot
sekmeleri) bu sayfadan tamamen bağımsızdır. Shadow testi yalnızca sistem devreye girdikten sonra fill olan yeni
pozisyonları değerlendirir — "Gerçek Açık" sayısı trading-bot'taki TÜM açık pozisyonları gösterir, bunların "Eski / Kapsam
Dışı" olanları shadow kıyasına dahil edilmez.</p>

<div class="cards">
  <div class="card"><span class="val" style="color:var(--accent)">{stats['gercek_acik']}</span><span class="lbl">Gerçek Açık</span></div>
  <div class="card"><span class="val" style="color:#3498db">{stats['gecerli_izlenen']}</span><span class="lbl">Geçerli Shadow</span></div>
  <div class="card"><span class="val" style="color:#5a6472">{stats['gecersiz']}</span><span class="lbl">Eski / Kapsam Dışı</span></div>
  <div class="card"><span class="val" style="color:var(--orange)">{stats['sanal_cikis']}</span><span class="lbl">Sanal Çıkış</span></div>
  <div class="card"><span class="val" style="color:var(--green)">{stats['sanal_onde']}</span><span class="lbl">Sanal Önde</span></div>
  <div class="card"><span class="val" style="color:var(--orange)">{stats['gercek_onde']}</span><span class="lbl">Gerçek Önde</span></div>
  <div class="card"><span class="val" style="color:var(--red)">{stats['ayni_mum_riski']}</span><span class="lbl">Aynı Mum Riski</span></div>
  <div class="card"><span class="val" style="color:#3498db">{stats['orphan_izleniyor']}</span><span class="lbl">Orphan İzleniyor</span></div>
  <div class="card"><span class="val" style="color:#6c7a89">{stats['belirsiz_sonuclanamadi']}</span><span class="lbl">Belirsiz / Sonuçlanamadı</span></div>
</div>

<div class="section-title">📊 Aktif Shadow Karşılaştırması</div>
{summary_table_html}

{invalid_section_html}

<details class="detail-log">
  <summary>Detaylı Olay Günlüğü</summary>
  <div class="table-wrap"><table><thead><tr>
    <th>Zaman</th><th>Sembol</th><th>Gerçek Durum</th><th>Sanal Durum</th>
    <th>Gerçek TP1</th><th>Sanal TP1</th><th>Sanal Peak</th><th>Sanal Trail/Stop</th>
    <th>Sanal Getiri</th><th>Olay</th><th>Not</th>
  </tr></thead><tbody>
  {rows}
  </tbody></table></div>
</details>

</body></html>"""
