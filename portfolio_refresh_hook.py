# -*- coding: utf-8 -*-
"""Portfolio dashboard manual refresh support.

Loaded only by the portfolio_tracker.py process through sitecustomize.
The hook keeps the existing tracker untouched and makes the dashboard's
"Yenile" button refresh open-position prices from Binance before reloading.
"""
from __future__ import annotations

import sys


def install_portfolio_refresh_hook() -> None:
    import flask

    if getattr(flask.Flask.run, "_portfolio_refresh_hook", False):
        return

    original_run = flask.Flask.run

    def patched_run(app, *args, **kwargs):
        if "api_refresh_prices" not in app.view_functions:

            @app.route("/api/refresh-prices", methods=["POST"])
            def api_refresh_prices():
                mod = sys.modules.get("__main__")
                if mod is None:
                    return flask.jsonify({"ok": False, "error": "portfolio module unavailable"}), 500

                lock = getattr(mod, "_lock", None)
                db = getattr(mod, "signals_db", None)
                price_fn = getattr(mod, "get_current_price_hl", None)
                save_fn = getattr(mod, "save_signals", None)
                now_fn = getattr(mod, "tr_now", None)
                if lock is None or db is None or not callable(price_fn) or not callable(save_fn) or not callable(now_fn):
                    return flask.jsonify({"ok": False, "error": "portfolio refresh dependencies unavailable"}), 500

                with lock:
                    active = [s for s in db if s.get("status") == "open"]

                updates = []
                for sig in active:
                    symbol = sig.get("symbol")
                    if not symbol:
                        continue
                    data = price_fn(symbol)
                    if not data:
                        continue
                    updates.append((sig, data))

                now = now_fn().isoformat()
                updated = 0
                with lock:
                    for sig, data in updates:
                        if sig.get("status") != "open":
                            continue
                        entry = float(sig.get("entry") or 0)
                        close = float(data["close"])
                        high = float(data["high"])
                        low = float(data["low"])

                        old_peak = float(sig.get("peak_price") or entry or high)
                        old_low = float(sig.get("low_price") or entry or low)
                        new_peak = max(old_peak, high)
                        new_low = min(old_low, low)

                        sig["current_price"] = close
                        sig["peak_price"] = new_peak
                        sig["low_price"] = new_low
                        if entry:
                            sig["current_pct"] = round((close - entry) / entry * 100, 2)
                            sig["peak_pct"] = round((new_peak - entry) / entry * 100, 2)
                            sig["low_pct"] = round((new_low - entry) / entry * 100, 2)
                        sig["last_check"] = now
                        sig["checks"] = int(sig.get("checks", 0) or 0) + 1
                        updated += 1
                    if updated:
                        save_fn()

                return flask.jsonify({"ok": True, "updated": updated})

            @app.after_request
            def _portfolio_refresh_button(response):
                try:
                    if flask.request.path != "/" or response.mimetype != "text/html":
                        return response
                    html = response.get_data(as_text=True)
                    old = '<button class="btn-refresh" onclick="location.reload()">🔄 Yenile</button>'
                    if old not in html:
                        return response
                    new = '<button class="btn-refresh" onclick="portfolioRefresh(this)">🔄 Yenile</button>'
                    script = '''<script>
async function portfolioRefresh(btn){
  var oldText=btn.textContent;
  btn.disabled=true;btn.textContent='⏳ Yenileniyor';
  try{
    var r=await fetch('/api/refresh-prices',{method:'POST'});
    if(!r.ok) throw new Error('HTTP '+r.status);
    location.reload();
  }catch(e){
    btn.disabled=false;btn.textContent='⚠ Tekrar dene';
    setTimeout(function(){btn.textContent=oldText;},1800);
  }
}
</script>'''
                    html = html.replace(old, new, 1).replace("</body>", script + "\n</body>", 1)
                    response.set_data(html)
                    response.headers["Content-Length"] = str(len(response.get_data()))
                except Exception:
                    pass
                return response

        return original_run(app, *args, **kwargs)

    patched_run._portfolio_refresh_hook = True
    flask.Flask.run = patched_run
