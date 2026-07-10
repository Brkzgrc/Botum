# -*- coding: utf-8 -*-
"""
Position Monitor — WebSocket fiyat takibi + trailing + pending order yönetimi
=============================================================================
Pending : Limit buy doldu mu? 48H geçti mi?
Open    : peak güncelle | SL doldu mu? | TP1 → trailing
"""

import json, math, os, time, threading, requests
from datetime import datetime, timezone, timedelta
from binance.client import Client
from binance.exceptions import BinanceAPIException
from binance import ThreadedWebsocketManager

API_KEY          = os.getenv("BINANCE_API_KEY", "")
API_SECRET       = os.getenv("BINANCE_API_SECRET", "")
ENABLED          = os.getenv("TRADING_ENABLED", "false").lower() == "true"
STATE_FILE       = os.getenv("TRADE_STATE_FILE", "/tmp/trade_state.json")
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
PORTFOLIO_URL    = os.getenv("PORTFOLIO_URL", "")
PORTFOLIO_TOKEN  = os.getenv("PORTFOLIO_TOKEN", "")

TRAIL_PCT        = 0.975   # %2.5 trailing
PENDING_EXPIRE_H = 48      # Retest bekleme süresi (saat)
SL_LIMIT_BUFFER  = 0.003   # SL limit fiyatı = stop * (1 - 0.003)
CHECK_INTERVAL   = 60      # saniye

_client: Client | None = None
_lock   = threading.Lock()
_twm: ThreadedWebsocketManager | None = None
_streams: dict[str, str] = {}   # symbol → stream_key


# ─── CLIENT ──────────────────────────────────────────────────────────────────

def _get_client() -> Client:
    global _client
    if _client is None:
        _client = Client(API_KEY, API_SECRET)
    return _client


# ─── STATE ───────────────────────────────────────────────────────────────────

def _load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"positions": {}}


def _save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, default=str)


# ─── YARDIMCI ────────────────────────────────────────────────────────────────

def _round_qty(qty: float, symbol: str) -> float:
    try:
        info = _get_client().get_symbol_info(symbol)
        if not info:
            return round(qty, 6)
        for f in info.get("filters", []):
            if f["filterType"] == "LOT_SIZE":
                step = float(f["stepSize"])
                precision = max(0, int(round(-math.log10(step))))
                qty = math.floor(qty / step) * step
                return round(qty, precision)
    except Exception:
        pass
    return round(qty, 6)


def _round_price(price: float, symbol: str) -> float:
    try:
        info = _get_client().get_symbol_info(symbol)
        if not info:
            return round(price, 8)
        for f in info.get("filters", []):
            if f["filterType"] == "PRICE_FILTER":
                tick = float(f["tickSize"])
                precision = max(0, int(round(-math.log10(tick))))
                price = math.floor(price / tick) * tick
                return round(price, precision)
    except Exception:
        pass
    return round(price, 8)


def _send_telegram(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "message_thread_id": 2,
                "parse_mode": "HTML",
            },
            timeout=10,
        )
    except Exception as e:
        print(f"[MONITOR] Telegram hata: {e}", flush=True)


def _notify_portfolio(endpoint: str, data: dict):
    if not PORTFOLIO_URL or not PORTFOLIO_TOKEN:
        return
    try:
        requests.post(
            f"{PORTFOLIO_URL}{endpoint}",
            json=data,
            headers={"Authorization": f"Bearer {PORTFOLIO_TOKEN}"},
            timeout=15,
        )
    except Exception as e:
        print(f"[MONITOR] Portfolio bildirim hatası ({endpoint}): {e}", flush=True)


def _market_sell(symbol: str, qty: float, reason: str):
    tag = f"[MONITOR] SELL {symbol} {qty} ({reason})"
    if not ENABLED:
        print(f"{tag} — SİMÜLASYON", flush=True)
        return True
    try:
        _get_client().order_market_sell(symbol=symbol, quantity=qty)
        print(f"{tag} — OK", flush=True)
        return True
    except BinanceAPIException as e:
        print(f"{tag} — HATA: {e}", flush=True)
        return False


def _cancel_sl(symbol: str, sl_order_id):
    if not sl_order_id:
        return
    if not ENABLED:
        print(f"[MONITOR] SL iptali SİMÜLASYON — {symbol} orderId={sl_order_id}", flush=True)
        return
    try:
        _get_client().cancel_order(symbol=symbol, orderId=sl_order_id)
        print(f"[MONITOR] SL iptal — {symbol} orderId={sl_order_id}", flush=True)
    except BinanceAPIException as e:
        print(f"[MONITOR] SL iptal HATA {symbol}: {e}", flush=True)


def _is_sl_filled(symbol: str, sl_order_id) -> bool:
    if not sl_order_id or not ENABLED:
        return False
    try:
        order = _get_client().get_order(symbol=symbol, orderId=sl_order_id)
        return order.get("status") == "FILLED"
    except BinanceAPIException as e:
        print(f"[MONITOR] Order kontrol hatası {symbol}: {e}", flush=True)
        return False


def _is_limit_filled(symbol: str, order_id) -> tuple[bool, float, float]:
    """Limit buy emri doldu mu? → (filled, fill_price, qty)"""
    if not order_id or not ENABLED:
        return False, 0.0, 0.0
    try:
        order = _get_client().get_order(symbol=symbol, orderId=order_id)
        if order.get("status") == "FILLED":
            executed_qty = float(order.get("executedQty", 0))
            quote_qty    = float(order.get("cummulativeQuoteQty", 0))
            fill_price   = quote_qty / executed_qty if executed_qty else 0.0
            return True, fill_price, executed_qty
    except BinanceAPIException as e:
        print(f"[MONITOR] Limit order kontrol hatası {symbol}: {e}", flush=True)
    return False, 0.0, 0.0


# ─── PENDING ORDER YÖNETİMİ ──────────────────────────────────────────────────

def _activate_position(symbol: str, fill_price: float, qty: float, pos: dict):
    """Limit doldu: state'i open'a çevir, SL koy, WS başlat, bildir."""
    now = datetime.now(timezone.utc).isoformat()

    # Önce state'i kaydet (sl_order_id=None) — crash güvenliği
    with _lock:
        state = _load_state()
        state["positions"][symbol] = {
            "status":      "open",
            "symbol":      symbol,
            "entry":       fill_price,
            "qty":         qty,
            "stop":        float(pos["stop"]),
            "tp1":         float(pos["tp1"]),
            "tp2":         pos.get("tp2"),
            "peak":        fill_price,
            "sl_order_id": None,
            "trailing":    False,
            "open_time":   now,
            "source":      pos.get("source", "smc-v2"),
        }
        _save_state(state)

    # Sonra SL emri gönder, ID'yi state'e yaz
    sl_order_id = None
    if ENABLED:
        try:
            sl_stop  = _round_price(float(pos["stop"]), symbol)
            sl_limit = _round_price(float(pos["stop"]) * (1 - SL_LIMIT_BUFFER), symbol)
            sl_order = _get_client().create_order(
                symbol=symbol,
                side="SELL",
                type="STOP_LOSS_LIMIT",
                timeInForce="GTC",
                quantity=qty,
                stopPrice=sl_stop,
                price=sl_limit,
            )
            sl_order_id = sl_order["orderId"]
            print(f"[MONITOR] SL emri: {symbol} stop={sl_stop} limit={sl_limit}", flush=True)
            with _lock:
                s = _load_state()
                if symbol in s["positions"]:
                    s["positions"][symbol]["sl_order_id"] = sl_order_id
                    _save_state(s)
        except BinanceAPIException as e:
            print(f"[MONITOR] SL emir hatası {symbol}: {e}", flush=True)

    _notify_portfolio("/api/retest-filled", {
        "symbol":     symbol,
        "fill_price": fill_price,
        "qty":        qty,
    })
    _start_stream(symbol)

    sl_status = "aktif" if sl_order_id else "YOK (hata!)"
    msg = (
        f"✅ <b>RETEST DOLDU — {symbol}</b>\n"
        f"Giriş: <b>{fill_price:.6g}</b>\n"
        f"Miktar: {qty}\n"
        f"Stop: {float(pos['stop']):.6g} | TP1: {float(pos['tp1']):.6g}\n"
        f"SL emri {sl_status}."
    )
    _send_telegram(msg)
    print(f"[MONITOR] Pozisyon aktif: {symbol} giriş={fill_price:.6g} qty={qty}", flush=True)


def _cancel_pending(symbol: str, pos: dict):
    """48H doldu: limit emri iptal et, state'den sil, bildir."""
    order_id = pos.get("limit_order_id")
    if order_id and ENABLED:
        try:
            _get_client().cancel_order(symbol=symbol, orderId=order_id)
            print(f"[MONITOR] Limit emir iptal: {symbol} orderId={order_id}", flush=True)
        except BinanceAPIException as e:
            # Zaten dolmuş veya iptal edilmiş olabilir
            print(f"[MONITOR] Limit emir iptal HATA {symbol}: {e}", flush=True)

    with _lock:
        state = _load_state()
        state["positions"].pop(symbol, None)
        _save_state(state)

    _notify_portfolio("/api/retest-cancelled", {"symbol": symbol})

    limit_price = pos.get("limit_price", 0)
    msg = (
        f"⏰ <b>RETEST ZAMANI DOLDU — {symbol}</b>\n"
        f"48 saat içinde limit ({limit_price:.6g}) dolmadı.\n"
        f"Emir iptal edildi."
    )
    _send_telegram(msg)
    print(f"[MONITOR] Pending süresi doldu: {symbol}", flush=True)


def _check_pending_orders():
    """Pending limit emirleri kontrol et: fill veya 48H expire."""
    state = _load_state()
    for sym, pos in list(state.get("positions", {}).items()):
        if pos.get("status") != "pending":
            continue

        try:
            open_time = datetime.fromisoformat(pos["open_time"])
            if datetime.now(timezone.utc) - open_time >= timedelta(hours=PENDING_EXPIRE_H):
                _cancel_pending(sym, pos)
                continue
        except Exception as e:
            print(f"[MONITOR] Pending expire kontrol hatası {sym}: {e}", flush=True)
            continue

        filled, fill_price, qty = _is_limit_filled(sym, pos.get("limit_order_id"))
        if filled:
            _activate_position(sym, fill_price, qty, pos)


# ─── TICK İŞLEME ─────────────────────────────────────────────────────────────

def _process_tick(symbol: str, close: float, high: float, low: float):
    sell_reason  = None
    close_price  = None
    cancel_sl    = False
    pos_snap     = None

    with _lock:
        state = _load_state()
        pos = state["positions"].get(symbol)
        if pos is None or pos.get("status") == "pending":
            return

        # Satış işlemi devam ediyorsa bu tick'i atla
        if pos.get("closing"):
            return

        pos_snap = dict(pos)

        if not pos.get("trailing"):
            # Peak: mumun high'ına göre güncelle
            if high > float(pos["peak"]):
                pos["peak"] = high
                state["positions"][symbol] = pos
                _save_state(state)

            # SL kontrolü: mumun low'una göre
            if not pos.get("sl_order_id") and low <= float(pos["stop"]):
                sell_reason = "stop_hit"
                close_price = float(pos["stop"])
                pos["closing"] = True
                state["positions"][symbol] = pos
                _save_state(state)
            # TP1 kontrolü: mumun high'ına göre
            elif high >= float(pos["tp1"]):
                cancel_sl       = True
                pos["tp1_hit"]  = True
                pos["trailing"] = True
                pos["peak"]     = max(high, float(pos["peak"]))
                state["positions"][symbol] = pos
                _save_state(state)
                pos_snap = dict(pos)
                print(f"[MONITOR] TP1 HIT — {symbol} @ {high:.6g} | trailing başladı", flush=True)

        else:
            # Peak: mumun high'ına göre güncelle
            if high > float(pos["peak"]):
                pos["peak"] = high
                state["positions"][symbol] = pos
                _save_state(state)
                pos_snap = dict(pos)

            # Trail kontrolü: mumun low'una göre
            trail_stop = float(pos["peak"]) * TRAIL_PCT
            if low <= trail_stop:
                sell_reason = "trail_stop"
                close_price = trail_stop
                pos["closing"] = True
                state["positions"][symbol] = pos
                _save_state(state)

    # ── Lock dışı işlemler (Binance API çağrıları) ───────────────────────────
    if cancel_sl:
        _cancel_sl(symbol, pos_snap.get("sl_order_id"))

    if sell_reason:
        entry = float(pos_snap.get("entry", 0))
        qty   = _round_qty(float(pos_snap.get("qty", 0)), symbol)
        ok    = _market_sell(symbol, qty, sell_reason)
        if ok:
            with _lock:
                s = _load_state()
                s["positions"].pop(symbol, None)
                _save_state(s)
            _stop_stream(symbol)
            pct = round((close_price - entry) / entry * 100, 2) if entry and close_price else 0
            emoji = "💰" if pct > 0 else "🔴"
            _send_telegram(
                f"{emoji} <b>POZİSYON KAPANDI — {symbol}</b>\n"
                f"Sebep: {sell_reason}\nGiriş: {entry:.6g} | Çıkış: ~{close_price:.6g}\nP&L: {pct:+.2f}%"
            )
            _notify_portfolio("/api/position-closed", {
                "symbol": symbol, "reason": sell_reason,
                "close_price": close_price, "pnl_pct": pct,
            })
            print(f"[MONITOR] Pozisyon kapatıldı: {symbol} | {sell_reason} | {pct:+.2f}%", flush=True)
        else:
            # Satış başarısız: closing bayrağını kaldır, sonraki tick'te tekrar dene
            with _lock:
                s = _load_state()
                if symbol in s["positions"]:
                    s["positions"][symbol].pop("closing", None)
                    _save_state(s)
            print(f"[MONITOR] SATIŞ BAŞARISIZ: {symbol} ({sell_reason}), sonraki tick tekrar dener", flush=True)


# ─── WEBSOCKET ───────────────────────────────────────────────────────────────

def _make_handler(symbol: str):
    def handler(msg):
        if msg.get("e") == "error":
            print(f"[MONITOR] WS hata {symbol}: {msg}", flush=True)
            return
        if msg.get("e") != "kline":
            return
        k = msg["k"]
        if not k.get("x"):   # sadece kapanan mum
            return
        # Peak için high, SL/trail için low, bilgi için close
        _process_tick(symbol, float(k["c"]), float(k["h"]), float(k["l"]))
    return handler


def _start_stream(symbol: str):
    global _twm
    if symbol in _streams:
        return
    key = _twm.start_kline_socket(callback=_make_handler(symbol), symbol=symbol, interval="1m")
    _streams[symbol] = key
    print(f"[MONITOR] WS başladı: {symbol}", flush=True)


def _stop_stream(symbol: str):
    key = _streams.pop(symbol, None)
    if key and _twm:
        try:
            _twm.stop_socket(key)
        except Exception:
            pass
    print(f"[MONITOR] WS durdu: {symbol}", flush=True)


# ─── PERİYODİK KONTROL ───────────────────────────────────────────────────────

def _periodic_check():
    while True:
        time.sleep(CHECK_INTERVAL)
        try:
            _check_pending_orders()

            state     = _load_state()
            positions = state.get("positions", {})
            open_syms = {s for s, p in positions.items() if p.get("status", "open") == "open"}

            for sym in open_syms:
                if sym not in _streams:
                    _start_stream(sym)

            for sym in list(_streams.keys()):
                if sym not in open_syms:
                    _stop_stream(sym)

            for sym in list(open_syms):
                pos = positions.get(sym, {})
                if not pos.get("trailing") and _is_sl_filled(sym, pos.get("sl_order_id")):
                    with _lock:
                        s = _load_state()
                        s["positions"].pop(sym, None)
                        _save_state(s)
                    _stop_stream(sym)
                    entry = float(pos.get("entry", 0))
                    sl    = float(pos.get("stop", 0))
                    pct   = round((sl - entry) / entry * 100, 2) if entry else 0
                    _send_telegram(
                        f"🔴 <b>SL TETİKLENDİ (Binance) — {sym}</b>\n"
                        f"Giriş: {entry:.6g} | Stop: {sl:.6g} | P&L: {pct:+.2f}%"
                    )
                    _notify_portfolio("/api/position-closed", {
                        "symbol": sym, "reason": "sl_binance",
                        "close_price": sl, "pnl_pct": pct,
                    })
                    print(f"[MONITOR] SL doldu (Binance): {sym} | {pct:+.2f}%", flush=True)

        except Exception as e:
            print(f"[MONITOR] Periyodik kontrol hatası: {e}", flush=True)


# ─── START ───────────────────────────────────────────────────────────────────

def start():
    global _twm
    print("[MONITOR] Başlatılıyor...", flush=True)

    _twm = ThreadedWebsocketManager(api_key=API_KEY, api_secret=API_SECRET)
    _twm.start()

    state = _load_state()
    for sym, pos in state.get("positions", {}).items():
        # Pending pozisyonlar için WS başlatma; periyodik kontrol yönetir
        if pos.get("status", "open") == "open":
            _start_stream(sym)

    t = threading.Thread(target=_periodic_check, daemon=True)
    t.start()

    print("[MONITOR] Hazır.", flush=True)
    _twm.join()
