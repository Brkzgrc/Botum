# -*- coding: utf-8 -*-
"""
Trading Engine — SMC CHoCH ROC otomatik emir sistemi
=====================================================
Giriş: market buy + Binance stop-loss limit order
Çıkış: TP1 hit → trailing (position_monitor yönetir)
       Expire 48h → market sell (position_monitor yönetir)

TRADING_ENABLED=false → simülasyon modu, Binance'e emir atılmaz.
"""

import json, math, os, threading
from datetime import datetime, timezone
from binance.client import Client
from binance.exceptions import BinanceAPIException

API_KEY    = os.getenv("BINANCE_API_KEY", "")
API_SECRET = os.getenv("BINANCE_API_SECRET", "")
ENABLED    = os.getenv("TRADING_ENABLED", "false").lower() == "true"

MAX_POSITIONS   = 5
MAX_POS_SIZE    = 20_000.0
SL_LIMIT_BUFFER = 0.003   # SL limit fiyatı = stop * (1 - 0.003)
MAX_ENTRY_DEV   = float(os.getenv("MAX_ENTRY_DEVIATION", "0.02"))  # %2 max sapma
STATE_FILE      = os.getenv("TRADE_STATE_FILE", "/tmp/trade_state.json")

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
    Beklenen alanlar: symbol, entry, stop, tp1
    Opsiyonel: tp2, source
    """
    symbol = _normalize_symbol(signal.get("symbol", ""))
    entry  = float(signal.get("entry", 0))
    stop   = float(signal.get("stop",  0))
    tp1    = float(signal.get("tp1",   0))
    tp2    = float(signal.get("tp2") or 0) or None

    if not symbol or not entry or not stop or not tp1:
        print(f"[TRADE] Eksik alan, atlandı: {signal}", flush=True)
        return

    if not ENABLED:
        print(f"[TRADE] SIMÜLASYON — {symbol} | giriş={entry:.6g} stop={stop:.6g} tp1={tp1:.6g}", flush=True)
        return

    with _lock:
        state     = load_state()
        positions = state.get("positions", {})

        if len(positions) >= MAX_POSITIONS:
            print(f"[TRADE] Reddedildi: max {MAX_POSITIONS} pozisyon dolu", flush=True)
            return

        if symbol in positions:
            print(f"[TRADE] Reddedildi: {symbol} zaten açık", flush=True)
            return

        usdt_balance    = _get_usdt_balance()
        remaining_slots = MAX_POSITIONS - len(positions)
        pos_size        = min(usdt_balance / remaining_slots, MAX_POS_SIZE)

        if pos_size < 10:
            print(f"[TRADE] Reddedildi: yetersiz bakiye ({usdt_balance:.2f} USDT)", flush=True)
            return

        # ── Giriş fiyatı sapma kontrolü ────────────────────────────────────
        try:
            ticker = get_client().get_symbol_ticker(symbol=symbol)
            current_price = float(ticker["price"])
            if current_price > entry * (1 + MAX_ENTRY_DEV):
                dev_pct = (current_price / entry - 1) * 100
                print(f"[TRADE] Reddedildi: fiyat giriş seviyesinden uzak | "
                      f"mevcut={current_price:.6g} sinyal={entry:.6g} sapma=%{dev_pct:.1f}", flush=True)
                return
            if current_price < entry * 0.90:
                dev_pct = (1 - current_price / entry) * 100
                print(f"[TRADE] Reddedildi: fiyat giriş altında çok düştü | "
                      f"mevcut={current_price:.6g} sinyal={entry:.6g} sapma=-%{dev_pct:.1f}", flush=True)
                return
        except BinanceAPIException as e:
            print(f"[TRADE] Reddedildi: fiyat kontrolü hatası {symbol}: {e}", flush=True)
            return

        print(f"[TRADE] {symbol} | giriş={entry:.6g} stop={stop:.6g} tp1={tp1:.6g} "
              f"| boyut=${pos_size:.2f} | kalan_slot={remaining_slots}", flush=True)

        # ── Market Buy ──────────────────────────────────────────────────────
        try:
            buy_order = get_client().order_market_buy(
                symbol=symbol,
                quoteOrderQty=round(pos_size, 2),
            )
            fills = buy_order.get("fills", [])
            if fills:
                total_qty  = sum(float(f["qty"])                         for f in fills)
                avg_price  = sum(float(f["price"]) * float(f["qty"])     for f in fills) / total_qty
            else:
                avg_price = entry
            qty = _round_qty(float(buy_order.get("executedQty", 0)), symbol)
            print(f"[TRADE] BUY OK: {symbol} {qty} @ {avg_price:.6g}", flush=True)
        except BinanceAPIException as e:
            print(f"[TRADE] BUY HATASI {symbol}: {e}", flush=True)
            return

        # ── Stop-Loss Limit Order ───────────────────────────────────────────
        sl_order_id = None
        try:
            sl_stop  = _round_price(stop,                           symbol)
            sl_limit = _round_price(stop * (1 - SL_LIMIT_BUFFER),  symbol)
            sl_order = get_client().create_order(
                symbol=symbol,
                side="SELL",
                type="STOP_LOSS_LIMIT",
                timeInForce="GTC",
                quantity=qty,
                stopPrice=sl_stop,
                price=sl_limit,
            )
            sl_order_id = sl_order["orderId"]
            print(f"[TRADE] SL OK: {symbol} stopPrice={sl_stop} limitPrice={sl_limit}", flush=True)
        except BinanceAPIException as e:
            print(f"[TRADE] SL HATASI {symbol}: {e} — pozisyon açık, SL yok!", flush=True)

        # ── State Kaydet ────────────────────────────────────────────────────
        now = datetime.now(timezone.utc).isoformat()
        positions[symbol] = {
            "symbol":      symbol,
            "qty":         qty,
            "entry":       round(avg_price, 8),
            "stop":        stop,
            "tp1":         tp1,
            "tp2":         tp2,
            "sl_order_id": sl_order_id,
            "tp1_hit":     False,
            "trailing":    False,
            "peak":        round(avg_price, 8),
            "open_time":   now,
            "source":      signal.get("source", "smc-v2"),
        }
        state["positions"] = positions
        save_state(state)
        print(f"[TRADE] Pozisyon kaydedildi: {symbol}", flush=True)
