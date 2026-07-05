# -*- coding: utf-8 -*-
"""
Position Monitor — WebSocket fiyat takibi + trailing + expire
=============================================================
Her açık pozisyon için {symbol}@kline_1m WebSocket dinlenir.
TP1 öncesi : peak güncelle | Binance SL dolduğu kontrol edilir
TP1 sonrası: trail_stop = peak × 0.975 → fiyat altına düşünce market sell
Expire 48h : market sell
"""

import json, math, os, time, threading
from datetime import datetime, timezone, timedelta
from binance.client import Client
from binance.exceptions import BinanceAPIException
from binance import ThreadedWebsocketManager

API_KEY    = os.getenv("BINANCE_API_KEY", "")
API_SECRET = os.getenv("BINANCE_API_SECRET", "")
ENABLED    = os.getenv("TRADING_ENABLED", "false").lower() == "true"
STATE_FILE = os.getenv("TRADE_STATE_FILE", "/tmp/trade_state.json")

TRAIL_PCT      = 0.975   # %2.5 trailing
EXPIRE_H       = 48
CHECK_INTERVAL = 60      # saniye — SL doldu mu? periyodik kontrol

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


# ─── TICK İŞLEME ─────────────────────────────────────────────────────────────

def _process_tick(symbol: str, price: float):
    sell_reason = None
    cancel_sl   = False
    pos_snap    = None   # lock dışında kullanılacak snapshot

    with _lock:
        state = _load_state()
        pos = state["positions"].get(symbol)
        if pos is None:
            return

        pos_snap = dict(pos)   # lock dışı işlemler için kopya

        open_time = datetime.fromisoformat(pos["open_time"])
        expired   = datetime.now(timezone.utc) - open_time >= timedelta(hours=EXPIRE_H)

        if expired:
            # Expire: pozisyonu kaldır, lock dışında sat
            state["positions"].pop(symbol)
            _save_state(state)
            sell_reason = f"expire_{EXPIRE_H}h"

        elif not pos.get("trailing"):
            # ── TP1 Öncesi ──────────────────────────────────────────────────
            if price > float(pos["peak"]):
                pos["peak"] = price
                state["positions"][symbol] = pos
                _save_state(state)

            # SL emri Binance'te yoksa (yerleştirme hatası) stop seviyesini bot izler
            if not pos.get("sl_order_id") and price <= float(pos["stop"]):
                sell_reason = f"stop_hit={float(pos['stop']):.6g}"
                state["positions"].pop(symbol)
                _save_state(state)
            elif price >= float(pos["tp1"]):
                # TP1 vuruldu: trailing moda geç, lock dışında SL iptal et
                cancel_sl       = True
                pos["tp1_hit"]  = True
                pos["trailing"] = True
                pos["peak"]     = max(price, float(pos["peak"]))
                state["positions"][symbol] = pos
                _save_state(state)
                pos_snap = dict(pos)
                print(f"[MONITOR] TP1 HIT — {symbol} @ {price:.6g} | trailing başladı", flush=True)

        else:
            # ── TP1 Sonrası Trailing ─────────────────────────────────────────
            if price > float(pos["peak"]):
                pos["peak"] = price
                state["positions"][symbol] = pos
                _save_state(state)
                pos_snap = dict(pos)

            trail_stop = float(pos["peak"]) * TRAIL_PCT
            if price <= trail_stop:
                sell_reason = f"trail_stop={trail_stop:.6g}"
                state["positions"].pop(symbol)
                _save_state(state)

    # ── Lock dışı işlemler (Binance API çağrıları) ───────────────────────────
    if cancel_sl:
        _cancel_sl(symbol, pos_snap.get("sl_order_id"))

    if sell_reason:
        qty = _round_qty(float(pos_snap.get("qty", 0)), symbol)
        _market_sell(symbol, qty, sell_reason)
        _stop_stream(symbol)
        print(f"[MONITOR] Pozisyon kapatıldı: {symbol} ({sell_reason})", flush=True)


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
        _process_tick(symbol, float(k["c"]))
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
    """
    Her CHECK_INTERVAL saniyede:
    - Yeni pozisyonlar için stream başlat
    - Kapalı pozisyonların stream'ini durdur
    - Binance SL dolduğunda state temizle
    """
    while True:
        time.sleep(CHECK_INTERVAL)
        try:
            state = _load_state()
            positions = state.get("positions", {})

            for sym in list(positions.keys()):
                if sym not in _streams:
                    _start_stream(sym)

            for sym in list(_streams.keys()):
                if sym not in positions:
                    _stop_stream(sym)

            for sym, pos in list(positions.items()):
                if not pos.get("trailing") and _is_sl_filled(sym, pos.get("sl_order_id")):
                    with _lock:
                        s = _load_state()
                        s["positions"].pop(sym, None)
                        _save_state(s)
                    _stop_stream(sym)
                    print(f"[MONITOR] SL doldu (Binance): {sym}", flush=True)

        except Exception as e:
            print(f"[MONITOR] Periyodik kontrol hatası: {e}", flush=True)


# ─── START ───────────────────────────────────────────────────────────────────

def start():
    global _twm
    print("[MONITOR] Başlatılıyor...", flush=True)

    _twm = ThreadedWebsocketManager(api_key=API_KEY, api_secret=API_SECRET)
    _twm.start()

    state = _load_state()
    for sym in state.get("positions", {}):
        _start_stream(sym)

    t = threading.Thread(target=_periodic_check, daemon=True)
    t.start()

    print("[MONITOR] Hazır.", flush=True)
    _twm.join()
