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

from flask import Blueprint, jsonify, request

DATA_DIR    = os.getenv("DATA_DIR", "/tmp")
EVENTS_FILE = os.path.join(DATA_DIR, "shadow_tp1_events.jsonl")
AUTH_TOKEN  = os.getenv("PORTFOLIO_AUTH_TOKEN", "")

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


# ─── ÖZET / KIRILIM ──────────────────────────────────────────────────────
def _summarize(events):
    closed = [e for e in events if e.get("event") == "SHADOW_LIVE_CLOSED"]
    good = sum(1 for e in closed if e.get("shadow_result") == "resolved" and (e.get("fark_pct") or 0) > 0)
    bad  = sum(1 for e in closed if e.get("shadow_result") == "resolved" and (e.get("fark_pct") or 0) < 0)
    undetermined = len(closed) - good - bad
    return {
        "toplam_izlenen":    len(closed),
        "sanal_trail_aktif": sum(1 for e in events if e.get("event") == "SHADOW_WOULD_ACTIVATE_TRAIL"),
        "sanal_cikis":       sum(1 for e in events if e.get("event") == "SHADOW_WOULD_EXIT_TRAIL"),
        "ayni_mum_riski":    sum(1 for e in events if e.get("event") == "SHADOW_SAME_CANDLE_TOUCH_AND_BREACH"),
        "gercekten_iyi":     good,
        "gercekten_kotu":    bad,
        "belirsiz":          undetermined,
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
    "pre_tp1":           "TP1 öncesi",
    "trailing":          "Trailing",
    "not_trailing":      "Henüz değil",
    "exited":            "Çıktı",
    "closed:stop_hit":   "Kapandı (Stop)",
    "closed:trail_stop": "Kapandı (Trail)",
    "closed:expire":     "Kapandı (Süre)",
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


def _status_label(v):
    # Bilinen bir kod değilse ham değeri gösteriyoruz (event kaynağı
    # position_monitor.py olsa da, HTML'e basılan her string escape edilir).
    return html.escape(str(_STATUS_LABELS.get(v, v or "—")))


def _row_html(e):
    sym = html.escape(str(e.get("symbol", "")).replace("/USDT", ""))
    ts = str(e.get("ts", ""))[:19].replace("T", " ")   # ISO timestamp — serbest metin değil
    event_key = e.get("event", "")
    event_label = html.escape(str(_EVENT_LABELS.get(event_key, event_key)))
    event_color = _EVENT_COLORS.get(event_key, "#7f8c8d")
    note = html.escape(str(e.get("note", "") or ""))

    return f"""<tr>
      <td style="color:#7f8c8d;font-size:.65rem">{ts}</td>
      <td><b>{sym}</b></td>
      <td>{_status_label(e.get("real_status"))}</td>
      <td>{_status_label(e.get("shadow_status"))}</td>
      <td>{_fmt(e.get("real_tp1"))}</td>
      <td>{_fmt(e.get("shadow_tp1"))}</td>
      <td>{_fmt(e.get("shadow_peak"))}</td>
      <td>{_fmt(e.get("shadow_trail"))}</td>
      <td>{_fmt_pct(e.get("shadow_pct"))}</td>
      <td><span style="color:{event_color};font-size:.6rem;white-space:nowrap">{event_label}</span></td>
      <td style="font-size:.65rem;color:#7f8c8d;white-space:normal;max-width:320px">{note}</td>
    </tr>"""


# ─── SAYFA ────────────────────────────────────────────────────────────────
@shadow_bp.route("/shadow")
def shadow_page():
    events = _load_events()
    stats = _summarize(events)
    recent = list(reversed(events[-300:]))
    rows = "".join(_row_html(e) for e in recent) or (
        '<tr><td colspan="11" style="text-align:center;color:#7f8c8d;padding:20px">'
        'Henüz olay yok — position_monitor.py deploy sonrası açılan pozisyonlarda birikmeye başlayacak.</td></tr>'
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
.cards{{display:grid;grid-template-columns:repeat(7,1fr);gap:10px;margin-bottom:24px;}}
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
sekmeleri) bu sayfadan tamamen bağımsızdır.</p>

<div class="cards">
  <div class="card"><span class="val" style="color:var(--accent)">{stats['toplam_izlenen']}</span><span class="lbl">Toplam İzlenen</span></div>
  <div class="card"><span class="val" style="color:#3498db">{stats['sanal_trail_aktif']}</span><span class="lbl">Sanal Trail Aktif</span></div>
  <div class="card"><span class="val" style="color:var(--orange)">{stats['sanal_cikis']}</span><span class="lbl">Sanal Çıkış</span></div>
  <div class="card"><span class="val" style="color:var(--red)">{stats['ayni_mum_riski']}</span><span class="lbl">Aynı Mum Riski</span></div>
  <div class="card"><span class="val" style="color:var(--green)">{stats['gercekten_iyi']}</span><span class="lbl">Gerçekten İyi</span></div>
  <div class="card"><span class="val" style="color:var(--red)">{stats['gercekten_kotu']}</span><span class="lbl">Gerçekten Kötü</span></div>
  <div class="card"><span class="val" style="color:var(--text-dim)">{stats['belirsiz']}</span><span class="lbl">Belirsiz</span></div>
</div>

<div class="table-wrap"><table><thead><tr>
  <th>Zaman</th><th>Sembol</th><th>Gerçek Durum</th><th>Sanal Durum</th>
  <th>Gerçek TP1</th><th>Sanal TP1</th><th>Sanal Peak</th><th>Sanal Trail/Stop</th>
  <th>Sanal Getiri</th><th>Olay</th><th>Not</th>
</tr></thead><tbody>
{rows}
</tbody></table></div>

</body></html>"""
