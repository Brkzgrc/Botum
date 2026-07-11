# -*- coding: utf-8 -*-
"""
Trading Bot — Ana Giriş Noktası
================================
- position_monitor'ı daemon thread'de başlatır
- /signal  → trading_engine.execute(signal)
- /health  → Render health check
- /status  → açık pozisyonlar (salt okunur)

TRADE_BOT_TOKEN env var ile endpoint'ler korunur.
"""

import json, os, threading
from flask import Flask, request, jsonify
import trading_engine
import position_monitor
from trading_engine import load_state

app = Flask(__name__)

BOT_TOKEN = os.getenv("TRADE_BOT_TOKEN", "")

# Gunicorn ile de çalışması için modül yüklenince monitor başlat
_monitor_thread = threading.Thread(target=lambda: _safe_start_monitor(), daemon=True)


def _safe_start_monitor():
    try:
        position_monitor.start()
    except Exception as e:
        print(f"[MAIN] position_monitor hatası: {e}", flush=True)


_monitor_thread.start()


def _auth(req) -> bool:
    if not BOT_TOKEN:
        return True   # token tanımlanmamışsa koru değil (geliştirme kolaylığı)
    return req.headers.get("X-Bot-Token") == BOT_TOKEN


# ─── ENDPOINTS ───────────────────────────────────────────────────────────────

@app.route("/")
@app.route("/health")
def health():
    return "OK", 200


@app.route("/status")
def status():
    if not _auth(request):
        return jsonify({"error": "unauthorized"}), 401
    state = load_state()
    return jsonify(state.get("positions", {}))


@app.route("/position/<symbol>", methods=["DELETE"])
def delete_position(symbol):
    if not _auth(request):
        return jsonify({"error": "unauthorized"}), 401
    symbol = symbol.upper()
    state = load_state()
    positions = state.get("positions", {})
    if symbol not in positions:
        return jsonify({"error": "not found"}), 404
    removed = positions.pop(symbol)
    state["positions"] = positions
    trading_engine.save_state(state)
    print(f"[MAIN] Pozisyon silindi: {symbol} (status={removed.get('status')})", flush=True)
    return jsonify({"ok": True, "removed": symbol})


@app.route("/signal", methods=["POST"])
def signal():
    if not _auth(request):
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "json body gerekli"}), 400

    # Eksik alan kontrolü burada değil — trading_engine.execute içinde yapılıyor
    try:
        trading_engine.execute(data)
        return jsonify({"ok": True}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
