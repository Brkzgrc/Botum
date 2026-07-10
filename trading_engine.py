# -*- coding: utf-8 -*-
"""
Trading Engine — SMC CHoCH ROC otomatik emir sistemi
=====================================================
Giriş: CHoCH seviyesi + 1 tick → LIMIT BUY (retest bekler)
Çıkış: TP1 hit → trailing (position_monitor yönetir)
       Expire 48h → limit iptal (position_monitor yönetir)

TRADING_ENABLED=false → simülasyon modu, Binance'e emir atılmaz.
"""

import json, math, os, threading
from datetime import datetime, timezone
from binance.client import Client
from binance.exceptions import BinanceAPIException

API_KEY    = os.getenv("BINANCE_API_KEY", "")
API_SECRET = os.getenv("BINANCE_API_SECRET", "")
ENABLED    = os.getenv("TRADING_ENABLED", "false").lower() == "true"

MAX_POSITIONS    = 5
MAX_POS_SIZE     = 20_000.0
SL_LIMIT_BUFFER  = 0.003   # SL limit fiyatı = stop * (1 - 0.003)
PENDING_EXPIRE_H = 48      # Retest bekleme süresi (saat)
STATE_FILE       = os.getenv("TRADE_STATE_FILE", "/tmp/trade_state.json")

_client: Client | None = None
_lock   = threading.Lock()
_symbol_info_cache: dict = {}


# ─── CLIENT ──────────────────────────────────────────────────────────────────

def get_client() -> Client:
    global _client
    if _client is None:
        _client = Client(API_KEY, API_SECRET)
    return _client


# ─── STATE ───────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"positions": {}}


def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, default=str)


# ─── YARDIMCI ────────────────────────────────────────────────────────────────

def _normalize_symbol(symbol: str) -> str:
    return symbol.replace("/", "").upper()


def _get_symbol_info(symbol: str) -> dict | None:
    if symbol in _symbol_info_cache:
        return _symbol_info_cache[symbol]
    try:
        info = get_client().get_symbol_info(symbol)
        if info:
            _symbol_info_cache[symbol] = info
        return info
    except Exception as e:
        print(f"[TRADE] Symbol info hatası {symbol}: {e}", flush=True)
        return None


def _round_qty(qty: float, symbol: str) -> float:
    info = _get_symbol_info(symbol)
    if not info:
        return round(qty, 6)
    for f in info.get("filters", []):
        if f["filterType"] == "LOT_SIZE":
            step = float(f["stepSize"])
            precision = max(0, int(round(-math.log10(step))))
            qty = math.floor(qty / step) * step
            return round(qty, precision)
    return round(qty, 6)


def _round_price(price: float, symbol: str) -> float:
    info = _get_symbol_info(symbol)
    if not info:
        return round(price, 8)
    for f in info.get("filters", []):
        if f["filterType"] == "PRICE_FILTER":
            tick = float(f["tickSize"])
            precision = max(0, int(round(-math.log10(tick))))
            price = math.floor(price / tick) * tick
            return round(price, precision)
    return round(price, 8)


def _one_tick_above(price: float, symbol: str) -> float:
    """CHoCH seviyesi + 1 tick → limit buy garantisi için."""
    info = _get_symbol_info(symbol)
    if not info:
        return price
    for f in info.get("filters", []):
        if f["filterType"] == "PRICE_FILTER":
            tick = float(f["tickSize"])
            precision = max(0, int(round(-math.log10(tick))))
            floored = math.floor(price / tick) * tick
            return round(floored + tick, precision)
    return price


def _get_usdt_balance() -> float:
    try:
        bal = get_client().get_asset_balance(asset="USDT")
        return float(bal["free"]) if bal else 0.0
    except Exception as e:
        print(f"[TRADE] Bakiye hatası: {e}", flush=True)
        return 0.0


# ─── ANA FONKSİYON ───────────────────────────────────────────────────────────

def execute(signal: dict):
    """
    SMC CHoCH ROC sinyalini işleme al.
    CHoCH seviyesine (entry) 1 tick üzerine LIMIT BUY koyar — retest bekler.
    SL emri yalnızca limit dolduğunda (position_monitor'da) verilir.

    Beklenen alanlar: symbol, entry, stop, tp1
    Opsiyonel: tp2, source
    """
    symbol = _normalize_symbol(signal.get("symbol", ""))
    entry  = float(signal.get("entry", 0))   # CHoCH seviyesi (swing_high)
    stop   = float(signal.get("stop",  0))
    tp1    = float(signal.get("tp1",   0))
    tp2    = float(signal.get("tp2") or 0) or None

    if not symbol or not entry or not stop or not tp1:
        print(f"[TRADE] Eksik alan, atlandı: {signal}", flush=True)
        return

    # Limit buy fiyatı: CHoCH + 1 tick (retest garantisi)
    limit_price = _one_tick_above(entry, symbol)

    if not ENABLED:
        print(f"[TRADE] SIMÜLASYON — {symbol} | limit={limit_price:.6g} stop={stop:.6g} tp1={tp1:.6g}", flush=True)
        return

    with _lock:
        state     = load_state()
        positions = state.get("positions", {})

        if len(positions) >= MAX_POSITIONS:
            print(f"[TRADE] Reddedildi: max {MAX_POSITIONS} pozisyon dolu", flush=True)
            return

        if symbol in positions:
            print(f"[TRADE] Reddedildi: {symbol} zaten aktif (pending/open)", flush=True)
            return

        usdt_balance    = _get_usdt_balance()
        remaining_slots = MAX_POSITIONS - len(positions)
        pos_size        = min(usdt_balance / remaining_slots, MAX_POS_SIZE)

        if pos_size < 10:
            print(f"[TRADE] Reddedildi: yetersiz bakiye ({usdt_balance:.2f} USDT)", flush=True)
            return

        # ── Anlık fiyat kontrolleri ─────────────────────────────────────────
        try:
            ticker = get_client().get_symbol_ticker(symbol=symbol)
            current_price = float(ticker["price"])
            if current_price >= tp1:
                print(f"[TRADE] Reddedildi: fiyat TP1 üzerinde | "
                      f"mevcut={current_price:.6g} tp1={tp1:.6g}", flush=True)
                return
            if current_price <= stop:
                print(f"[TRADE] Reddedildi: fiyat stop seviyesinin altında | "
                      f"mevcut={current_price:.6g} stop={stop:.6g}", flush=True)
                return
            if current_price < entry * 0.80:
                print(f"[TRADE] Reddedildi: fiyat CHoCH'un çok altında | "
                      f"mevcut={current_price:.6g} choch={entry:.6g}", flush=True)
                return
        except BinanceAPIException as e:
            print(f"[TRADE] Reddedildi: fiyat kontrolü hatası {symbol}: {e}", flush=True)
            return

        # ── Limit Buy emri ─────────────────────────────────────────────────
        # Miktar tahmini: pos_size / limit_price (gerçek fill qty farklı olabilir)
        qty_estimate = _round_qty(pos_size / limit_price, symbol)
        if qty_estimate <= 0:
            print(f"[TRADE] Reddedildi: hesaplanan miktar sıfır ({symbol})", flush=True)
            return

        print(f"[TRADE] {symbol} | limit={limit_price:.6g} stop={stop:.6g} tp1={tp1:.6g} "
              f"| boyut=${pos_size:.2f} | tahmini_qty={qty_estimate}", flush=True)

        limit_order_id = None
        if ENABLED:
            try:
                lp = _round_price(limit_price, symbol)
                limit_order = get_client().create_order(
                    symbol=symbol,
                    side="BUY",
                    type="LIMIT",
                    timeInForce="GTC",
                    quantity=qty_estimate,
                    price=lp,
                )
                limit_order_id = limit_order["orderId"]
                print(f"[TRADE] LİMİT BUY OK: {symbol} {qty_estimate} @ {lp}", flush=True)
            except BinanceAPIException as e:
                print(f"[TRADE] LİMİT BUY HATASI {symbol}: {e}", flush=True)
                return

        # ── Pending State Kaydet ────────────────────────────────────────────
        now = datetime.now(timezone.utc).isoformat()
        positions[symbol] = {
            "status":          "pending",
            "symbol":          symbol,
            "limit_order_id":  limit_order_id,
            "limit_price":     limit_price,
            "pos_size_usdt":   pos_size,
            "stop":            stop,
            "tp1":             tp1,
            "tp2":             tp2,
            "open_time":       now,   # CHoCH ateşlenme zamanı (48H sayacı buradan başlar)
            "source":          signal.get("source", "smc-v2"),
        }
        state["positions"] = positions
        save_state(state)
        print(f"[TRADE] PENDING kaydedildi: {symbol} | limit @ {limit_price:.6g} | "
              f"48H retest bekleniyor", flush=True)
