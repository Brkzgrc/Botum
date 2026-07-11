# -*- coding: utf-8 -*-
"""
Trading Engine — SMC CHoCH ROC otomatik emir sistemi
=====================================================
Akış: CHoCH sinyali → izleme listesi (monitoring)
      Fiyat CHoCH+3tick'e gelince → position_monitor limit buy açar (CHoCH+1tick)
      Limit dolunca → pozisyon aktif (position_monitor yönetir)
      TP1 hit → trailing (position_monitor yönetir)
      48h monitoring doldu → iptal

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
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, default=str)
    os.replace(tmp, STATE_FILE)


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
    """CHoCH seviyesi + 1 tick → limit buy fiyatı."""
    return _n_ticks_above(price, symbol, 1)


def _n_ticks_above(price: float, symbol: str, n: int) -> float:
    """CHoCH seviyesi + n tick."""
    info = _get_symbol_info(symbol)
    if not info:
        print(f"[TRADE] _n_ticks_above: symbol info yok {symbol}, price aynen döndü", flush=True)
        return price
    for f in info.get("filters", []):
        if f["filterType"] == "PRICE_FILTER":
            tick = float(f["tickSize"])
            precision = max(0, int(round(-math.log10(tick))))
            floored = math.floor(price / tick) * tick
            result = round(floored + n * tick, precision)
            print(f"[TRADE] _n_ticks_above: {symbol} | choch={price} tick={tick} n={n} → {result}", flush=True)
            return result
    print(f"[TRADE] _n_ticks_above: PRICE_FILTER bulunamadı {symbol}", flush=True)
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
    SMC CHoCH sinyalini izleme listesine al.
    Fiyat CHoCH+3tick'e gelince position_monitor limit buy açar (CHoCH+1tick).

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

    # Fiyatları tick size'a yuvarla — SMC ham değerleri ondalık saçmalık üretebilir
    stop = _round_price(stop, symbol)
    tp1  = _round_price(tp1, symbol)
    if tp2:
        tp2 = _round_price(tp2, symbol)

    limit_price   = _one_tick_above(entry, symbol)    # CHoCH+1tick — emir fiyatı
    trigger_price = _n_ticks_above(entry, symbol, 3)  # CHoCH+3tick — izleme tetikleyici

    if not ENABLED:
        print(f"[TRADE] SIMÜLASYON — {symbol} | trigger≤{trigger_price:.6g} → limit@{limit_price:.6g} stop={stop:.6g} tp1={tp1:.6g}", flush=True)
        return

    with _lock:
        state     = load_state()
        positions = state.get("positions", {})

        if symbol in positions:
            print(f"[TRADE] Reddedildi: {symbol} zaten aktif ({positions[symbol].get('status')})", flush=True)
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

        now = datetime.now(timezone.utc).isoformat()
        positions[symbol] = {
            "status":         "monitoring",
            "symbol":         symbol,
            "limit_price":    limit_price,
            "trigger_price":  trigger_price,
            "stop":           stop,
            "tp1":            tp1,
            "tp2":            tp2,
            "open_time":      now,
            "source":         signal.get("source", "smc-v2"),
        }
        state["positions"] = positions
        save_state(state)
        print(f"[TRADE] MONİTORİNG: {symbol} | fiyat ≤{trigger_price:.6g} bekleniyor → limit@{limit_price:.6g}", flush=True)
