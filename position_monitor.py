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

ATR_PERIOD             = 14      # ATR periyodu (1H bar)
ATR_MULT               = 0.6     # trail_stop = peak - ATR_MULT * ATR(14, 1H)
ATR_REFRESH_S          = 1800    # ATR en fazla bu kadar saniyede bir yeniden çekilir
STREAM_STALE_S         = 300     # Bu kadar saniye tick gelmezse stream zombi kabul edilip yeniden başlatılır
FALLBACK_TRAIL_PCT     = 0.9816  # ATR çekilemezse: peak * bu değer (%1.84 sabit trailing)
PENDING_EXPIRE_H       = 48      # Monitoring süresi: CHoCH+3tick bekleme (saat)
PENDING_ORDER_EXPIRE_H = 1       # Limit emir süresi: CHoCH+3tick→+1tick arası (saat)
OPEN_EXPIRE_H          = 24      # Açık trade max süresi: fill sonrası 24H geçince market sell
SL_LIMIT_BUFFER        = 0.003   # SL limit fiyatı = stop * (1 - 0.003)
CHECK_INTERVAL   = 60      # saniye
MAX_POSITIONS    = 5
MAX_POS_SIZE     = 20_000.0

_client: Client | None = None
_lock         = threading.Lock()
_streams_lock = threading.Lock()   # _streams dict erişimi için ayrı kilit
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
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, default=str)
    os.replace(tmp, STATE_FILE)


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


def _notify_portfolio(endpoint: str, data: dict) -> bool:
    if not PORTFOLIO_URL or not PORTFOLIO_TOKEN:
        return False
    try:
        resp = requests.post(
            f"{PORTFOLIO_URL}{endpoint}",
            json=data,
            headers={"Authorization": f"Bearer {PORTFOLIO_TOKEN}"},
            timeout=15,
        )
        return resp.status_code < 400
    except Exception as e:
        print(f"[MONITOR] Portfolio bildirim hatası ({endpoint}): {e}", flush=True)
        return False


def _notify_portfolio_with_retry(endpoint: str, data: dict):
    def _attempt():
        sym = data.get("symbol", "")
        attempt = 0
        while True:
            attempt += 1
            if _notify_portfolio(endpoint, data):
                print(f"[MONITOR] {endpoint} OK (deneme {attempt}) — {sym}", flush=True)
                return
            print(f"[MONITOR] {endpoint} başarısız (deneme {attempt}), 60s sonra tekrar — {sym}", flush=True)
            time.sleep(60)
    threading.Thread(target=_attempt, daemon=True).start()


def _market_sell(symbol: str, qty: float, reason: str):
    """State'teki qty gerçek bakiyeden fazla olabilir (komisyon kesintisi, manuel
    müdahale, eski kayıt drift'i vb.) — satıştan önce gerçek free balance'a kırpılır.
    Aksi halde -2010 (insufficient balance) hatası her tick'te aynı yanlış miktarla
    sonsuza kadar tekrar eder ve pozisyon asla kapanmaz."""
    if not ENABLED:
        print(f"[MONITOR] SELL {symbol} {qty} ({reason}) — SİMÜLASYON", flush=True)
        return True

    base = symbol.replace("USDT", "").replace("BTC", "").replace("ETH", "")
    balance_checked = False
    try:
        bal = _get_client().get_asset_balance(asset=base)
        free = float(bal["free"]) if bal else None
        if free is not None:
            balance_checked = True
            sell_qty = _round_qty(min(qty, free), symbol)
        else:
            sell_qty = _round_qty(qty, symbol)
    except Exception:
        sell_qty = _round_qty(qty, symbol)

    if sell_qty <= 0:
        if balance_checked:
            # Bakiye sıfır/dust görünüyor ama bu YANLIŞ pozitif olabilir: coin'ler
            # hâlâ iptal edilememiş bir emirde kilitli olabilir (ZKC'de yaşandı —
            # cancel_sl başarısız/atlanmış, biz "satılmış" sanıp state'i sildik,
            # oysa emir Binance'te hâlâ aynen duruyordu). Gerçekten açık emir kalmış
            # mı diye sormadan "satılmış" DEME.
            try:
                open_orders = _get_client().get_open_orders(symbol=symbol)
            except Exception as e:
                print(f"[MONITOR] SELL {symbol} ({reason}) — açık emir kontrolü başarısız ({e}), güvenli tarafta kal, tekrar denenecek", flush=True)
                return False
            if open_orders:
                oids = [o.get("orderId") for o in open_orders]
                print(f"[MONITOR] SELL {symbol} ({reason}) — bakiye sıfır AMA hâlâ açık emir var {oids}, satılmış SAYILMIYOR, tekrar denenecek", flush=True)
                return False
            print(f"[MONITOR] SELL {symbol} ({reason}) — bakiye sıfır, açık emir de yok, zaten satılmış kabul ediliyor", flush=True)
            return True
        print(f"[MONITOR] SELL {symbol} ({reason}) — miktar hesaplanamadı, tekrar denenecek", flush=True)
        return False

    tag = f"[MONITOR] SELL {symbol} {sell_qty} ({reason})" + (f" [state qty={qty} idi]" if sell_qty != qty else "")
    try:
        _get_client().order_market_sell(symbol=symbol, quantity=sell_qty)
        print(f"{tag} — OK", flush=True)
        return True
    except BinanceAPIException as e:
        print(f"{tag} — HATA: {e}", flush=True)
        return False


def _get_usdt_balance() -> float:
    try:
        bal = _get_client().get_asset_balance(asset="USDT")
        return float(bal["free"]) if bal else 0.0
    except Exception as e:
        print(f"[MONITOR] Bakiye hatası: {e}", flush=True)
        return 0.0


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


def _compute_atr(symbol: str, period: int = ATR_PERIOD):
    """Binance'ten son 1H mumları çekip Wilder ATR(period) hesaplar. Hata/yetersiz veri → None."""
    try:
        klines = _get_client().get_klines(symbol=symbol, interval=Client.KLINE_INTERVAL_1HOUR, limit=period * 5)
    except BinanceAPIException as e:
        print(f"[MONITOR] ATR kline hatası {symbol}: {e}", flush=True)
        return None
    except Exception as e:
        print(f"[MONITOR] ATR kline hatası {symbol}: {e}", flush=True)
        return None
    if len(klines) < period + 1:
        return None
    highs  = [float(k[2]) for k in klines]
    lows   = [float(k[3]) for k in klines]
    closes = [float(k[4]) for k in klines]
    trs = []
    for i in range(1, len(klines)):
        trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1])))
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


def _trail_stop_price(peak: float, atr) -> float:
    """peak - ATR_MULT*ATR; ATR yoksa/geçersizse sabit %1.84 yedeğe düşer."""
    if atr and atr > 0:
        return peak - ATR_MULT * atr
    return peak * FALLBACK_TRAIL_PCT


def _atr_is_stale(pos: dict) -> bool:
    ts = pos.get("atr_updated_at")
    if not ts:
        return True
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds()
        return age >= ATR_REFRESH_S
    except Exception:
        return True


def _place_trail_sl_order(symbol: str, peak: float, qty: float, atr=None):
    """peak - ATR_MULT*ATR (veya ATR yoksa peak*FALLBACK_TRAIL_PCT) seviyesinde STOP_LOSS_LIMIT emri aç. order_id döndürür."""
    trail_stop_raw = _trail_stop_price(peak, atr)
    if not ENABLED:
        trail_stop = round(trail_stop_raw, 8)
        print(f"[MONITOR] Trail SL SİMÜLASYON — {symbol} stop={trail_stop:.6g} qty={qty}", flush=True)
        return None
    try:
        trail_stop  = _round_price(trail_stop_raw, symbol)
        trail_limit = _round_price(trail_stop_raw * (1 - SL_LIMIT_BUFFER), symbol)
        qty_r = _round_qty(qty, symbol)
        order = _get_client().create_order(
            symbol=symbol, side="SELL", type="STOP_LOSS_LIMIT",
            timeInForce="GTC", quantity=qty_r,
            stopPrice=trail_stop, price=trail_limit,
        )
        oid = order["orderId"]
        print(f"[MONITOR] Trail SL emri: {symbol} stop={trail_stop} limit={trail_limit} qty={qty_r} id={oid}", flush=True)
        return oid
    except BinanceAPIException as e:
        print(f"[MONITOR] Trail SL emir HATA {symbol}: {e}", flush=True)
        return None


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

def _place_retroactive_sl(symbol: str, pos: dict):
    """sl_order_id=None, trailing=False olan open pozisyon için SL emri retroaktif aç."""
    if not ENABLED:
        print(f"[MONITOR] Retroaktif SL SİMÜLASYON — {symbol}", flush=True)
        return
    stored_qty = float(pos.get("qty") or 0)
    base = symbol.replace("USDT", "").replace("BTC", "").replace("ETH", "")
    try:
        bal  = _get_client().get_asset_balance(asset=base)
        free = float(bal["free"]) if bal else 0.0
    except Exception:
        free = 0.0
    # Komisyon kesintisi olabilir: min(stored, free); stored=0 ise free kullan
    qty = free if stored_qty <= 0 else min(stored_qty, free)
    qty = _round_qty(qty, symbol)
    if qty <= 0:
        print(f"[MONITOR] Retroaktif SL: {symbol} bakiye sıfır, atlandı", flush=True)
        return
    try:
        sl_stop  = _round_price(float(pos["stop"]), symbol)
        sl_limit = _round_price(float(pos["stop"]) * (1 - SL_LIMIT_BUFFER), symbol)
        sl_order = _get_client().create_order(
            symbol=symbol, side="SELL", type="STOP_LOSS_LIMIT",
            timeInForce="GTC", quantity=qty, stopPrice=sl_stop, price=sl_limit,
        )
        sl_order_id = sl_order["orderId"]
        with _lock:
            s = _load_state()
            if symbol in s["positions"]:
                s["positions"][symbol]["sl_order_id"] = sl_order_id
                s["positions"][symbol]["qty"] = qty
                _save_state(s)
        print(f"[MONITOR] Retroaktif SL açıldı: {symbol} stop={sl_stop} qty={qty}", flush=True)
        _send_telegram(
            f"🛡 <b>Retroaktif SL — {symbol}</b>\n"
            f"Stop: {sl_stop} | Miktar: {qty}"
        )
    except BinanceAPIException as e:
        print(f"[MONITOR] Retroaktif SL hata {symbol}: {e}", flush=True)
        with _lock:
            s = _load_state()
            if symbol in s["positions"]:
                fail_count = s["positions"][symbol].get("sl_fail_count", 0) + 1
                s["positions"][symbol]["sl_fail_count"] = fail_count
                _save_state(s)
        if fail_count <= 3:
            _send_telegram(
                f"⚠️ <b>SL KOYULAMADI — {symbol}</b> (deneme {fail_count}/3)\n"
                f"Stop: {float(pos.get('stop', 0)):.6g} | Hata: {e}\n"
                f"Coinin Binance Earn/Staking'den free'ye çekili olduğunu kontrol et."
                + ("\n<b>→ Artık tekrar denenmeyecek. Manuel stop koy.</b>" if fail_count == 3 else "")
            )


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
    # Komisyon base asset'ten kesildiyse executedQty > free balance olur → gerçek bakiyeyi al
    sl_order_id = None
    if ENABLED:
        try:
            base = symbol.replace("USDT", "").replace("BTC", "").replace("ETH", "")
            try:
                bal = _get_client().get_asset_balance(asset=base)
                free = float(bal["free"]) if bal else qty
                sl_qty = _round_qty(min(qty, free), symbol)
            except Exception:
                sl_qty = _round_qty(qty, symbol)
            if sl_qty <= 0:
                raise BinanceAPIException(None, -1, f"Kullanılabilir bakiye sıfır ({base})")
            sl_stop  = _round_price(float(pos["stop"]), symbol)
            sl_limit = _round_price(float(pos["stop"]) * (1 - SL_LIMIT_BUFFER), symbol)
            sl_order = _get_client().create_order(
                symbol=symbol,
                side="SELL",
                type="STOP_LOSS_LIMIT",
                timeInForce="GTC",
                quantity=sl_qty,
                stopPrice=sl_stop,
                price=sl_limit,
            )
            sl_order_id = sl_order["orderId"]
            print(f"[MONITOR] SL emri: {symbol} stop={sl_stop} limit={sl_limit} qty={sl_qty}", flush=True)
            with _lock:
                s = _load_state()
                if symbol in s["positions"]:
                    s["positions"][symbol]["sl_order_id"] = sl_order_id
                    s["positions"][symbol]["qty"] = sl_qty  # gerçek miktar
                    _save_state(s)
        except BinanceAPIException as e:
            print(f"[MONITOR] SL emir hatası {symbol}: {e}", flush=True)
            _send_telegram(
                f"⚠️ <b>SL EMRİ BAŞARISIZ — {symbol}</b>\n"
                f"Giriş: {fill_price:.6g} | Stop: {float(pos.get('stop', 0)):.6g}\n"
                f"Hata: {e}\n"
                f"Manuel stop koy!"
            )

    _notify_portfolio_with_retry("/api/retest-filled", {
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
        cancelled = False
        try:
            _get_client().cancel_order(symbol=symbol, orderId=order_id)
            print(f"[MONITOR] Limit emir iptal: {symbol} orderId={order_id}", flush=True)
            cancelled = True
        except BinanceAPIException as e:
            print(f"[MONITOR] Limit emir iptal HATA {symbol}: {e}", flush=True)

        if not cancelled:
            # İptal başarısız: emir expire anında fill olmuş olabilir
            filled, fill_price, qty = _is_limit_filled(symbol, order_id)
            if filled:
                print(f"[MONITOR] Expire anında fill tespit: {symbol} @ {fill_price:.6g}", flush=True)
                _activate_position(symbol, fill_price, qty, pos)
                return

    with _lock:
        state = _load_state()
        state["positions"].pop(symbol, None)
        _save_state(state)

    _notify_portfolio("/api/retest-cancelled", {"symbol": symbol})
    print(f"[MONITOR] Pending süresi doldu: {symbol}", flush=True)


def _check_pending_orders():
    """Pending limit emirleri kontrol et: fill veya 48H expire."""
    state = _load_state()
    for sym, pos in list(state.get("positions", {}).items()):
        if pos.get("status") != "pending":
            continue

        # Fill kontrolü expire'dan önce — expire anında fill varsa aktivasyon yapılır
        filled, fill_price, qty = _is_limit_filled(sym, pos.get("limit_order_id"))
        if filled:
            _activate_position(sym, fill_price, qty, pos)
            continue

        # Anlık fiyatı state'e yaz (UI için)
        try:
            ticker = _get_client().get_symbol_ticker(symbol=sym)
            with _lock:
                s = _load_state()
                if sym in s.get("positions", {}):
                    s["positions"][sym]["current_price"] = float(ticker["price"])
                    _save_state(s)
        except Exception:
            pass

        try:
            open_time = datetime.fromisoformat(pos["open_time"])
            if datetime.now(timezone.utc) - open_time >= timedelta(hours=PENDING_ORDER_EXPIRE_H):
                _cancel_pending(sym, pos)
        except Exception as e:
            print(f"[MONITOR] Pending expire kontrol hatası {sym}: {e}", flush=True)


def _place_monitoring_order(symbol: str, pos: dict, active_count: int):
    """Fiyat trigger'a geldi: Binance'e limit buy gönder, monitoring→pending."""
    limit_price = float(pos.get("limit_price", 0))
    if not limit_price:
        print(f"[MONITOR] {symbol} limit_price yok, atlandı", flush=True)
        return

    usdt_balance    = _get_usdt_balance()
    remaining_slots = MAX_POSITIONS - active_count
    pos_size        = min(usdt_balance / remaining_slots if remaining_slots > 0 else 0, MAX_POS_SIZE)

    if pos_size < 10:
        print(f"[MONITOR] {symbol} yetersiz bakiye ({usdt_balance:.2f} USDT)", flush=True)
        with _lock:
            s = _load_state()
            s["positions"].pop(symbol, None)
            _save_state(s)
        _notify_portfolio("/api/retest-cancelled", {"symbol": symbol})
        return

    qty = _round_qty(pos_size / limit_price, symbol)
    if qty <= 0:
        print(f"[MONITOR] {symbol} hesaplanan miktar sıfır", flush=True)
        return

    lp = _round_price(limit_price, symbol)
    print(f"[MONITOR] TRİGGER: {symbol} | limit={lp:.6g} boyut=${pos_size:.2f} qty={qty}", flush=True)

    # State'e pending yaz (limit_order_id=None) — crash güvenliği
    now = datetime.now(timezone.utc).isoformat()
    with _lock:
        s = _load_state()
        s["positions"][symbol] = {
            "status":         "pending",
            "symbol":         symbol,
            "limit_order_id": None,
            "limit_price":    limit_price,
            "pos_size_usdt":  pos_size,
            "stop":           float(pos["stop"]),
            "tp1":            float(pos["tp1"]),
            "tp2":            pos.get("tp2"),
            "open_time":      now,
            "source":         pos.get("source", "smc-v2"),
        }
        _save_state(s)

    try:
        order = _get_client().create_order(
            symbol=symbol,
            side="BUY",
            type="LIMIT",
            timeInForce="GTC",
            quantity=qty,
            price=lp,
        )
        limit_order_id = order["orderId"]
        print(f"[MONITOR] LİMİT BUY OK: {symbol} {qty} @ {lp}", flush=True)
        with _lock:
            s = _load_state()
            if symbol in s["positions"]:
                s["positions"][symbol]["limit_order_id"] = limit_order_id
                _save_state(s)
    except BinanceAPIException as e:
        print(f"[MONITOR] LİMİT BUY HATASI {symbol}: {e}", flush=True)
        with _lock:
            s = _load_state()
            s["positions"].pop(symbol, None)
            _save_state(s)
        _notify_portfolio("/api/retest-cancelled", {"symbol": symbol})


def _check_monitoring_entries():
    """Monitoring sinyalleri: expire kontrolü veya fiyat trigger → slot kontrolü → limit emir."""
    if not ENABLED:
        return

    state     = _load_state()
    positions = state.get("positions", {})
    monitoring = [(sym, pos) for sym, pos in positions.items() if pos.get("status") == "monitoring"]
    if not monitoring:
        return

    now          = datetime.now(timezone.utc)
    active_count = sum(1 for p in positions.values() if p.get("status") in ("pending", "open"))

    for sym, pos in monitoring:
        # 48H expire
        try:
            open_time = datetime.fromisoformat(pos["open_time"])
            if now - open_time >= timedelta(hours=PENDING_EXPIRE_H):
                with _lock:
                    s = _load_state()
                    s["positions"].pop(sym, None)
                    _save_state(s)
                _notify_portfolio("/api/retest-cancelled", {"symbol": sym})
                print(f"[MONITOR] Monitoring süresi doldu: {sym}", flush=True)
                continue
        except Exception as e:
            print(f"[MONITOR] Monitoring expire hatası {sym}: {e}", flush=True)
            continue

        # Fiyat kontrolü
        trigger_price = float(pos.get("trigger_price", 0))
        if not trigger_price:
            continue
        try:
            ticker        = _get_client().get_symbol_ticker(symbol=sym)
            current_price = float(ticker["price"])
            with _lock:
                s = _load_state()
                if sym in s.get("positions", {}):
                    s["positions"][sym]["current_price"] = current_price
                    _save_state(s)
        except BinanceAPIException as e:
            print(f"[MONITOR] Monitoring fiyat hatası {sym}: {e}", flush=True)
            continue

        if current_price > trigger_price:
            continue  # henüz yaklaşmadı

        # Trigger seviyesine geldi — slot kontrolü
        if active_count >= MAX_POSITIONS:
            print(f"[MONITOR] {sym} trigger @ {current_price:.6g} — slot dolu ({active_count}/{MAX_POSITIONS}) MISS", flush=True)
            with _lock:
                s = _load_state()
                s["positions"].pop(sym, None)
                _save_state(s)
            _notify_portfolio("/api/retest-cancelled", {"symbol": sym})
            _send_telegram(
                f"⚠️ <b>SLOT DOLU — {sym}</b>\n"
                f"Fiyat trigger ({pos.get('trigger_price', 0):.6g}) geldi ama {MAX_POSITIONS}/{MAX_POSITIONS} dolu."
            )
            continue

        # Slot var — limit emir aç
        _place_monitoring_order(sym, pos, active_count)
        active_count += 1  # bu döngüde slot sayacını güncelle


def _reconcile_sl_orders():
    """Startup: open pozisyonlar için Binance'teki mevcut SELL emirlerini tara,
    sl_order_id / trailing_sl_id eksikse eşleştir ve state'e kaydet."""
    if not ENABLED:
        return
    state = _load_state()
    updated = False
    for sym, pos in list(state.get("positions", {}).items()):
        if pos.get("status") != "open":
            continue
        has_sl      = pos.get("sl_order_id") is not None
        has_trail   = pos.get("trailing_sl_id") is not None
        is_trailing = pos.get("trailing", False)
        if (is_trailing and has_trail) or (not is_trailing and has_sl):
            continue  # zaten kayıtlı
        print(f"[MONITOR] SL reconcile: {sym} için Binance sorgulanıyor", flush=True)
        try:
            open_orders = _get_client().get_open_orders(symbol=sym)
            for order in open_orders:
                if order.get("side") == "SELL" and order.get("type") == "STOP_LOSS_LIMIT":
                    oid = order["orderId"]
                    if is_trailing:
                        pos["trailing_sl_id"] = oid
                    else:
                        pos["sl_order_id"] = oid
                    state["positions"][sym] = pos
                    updated = True
                    print(f"[MONITOR] SL reconcile eşleşti: {sym} orderId={oid}", flush=True)
                    break
        except BinanceAPIException as e:
            print(f"[MONITOR] SL reconcile hatası {sym}: {e}", flush=True)
    if updated:
        with _lock:
            _save_state(state)


def _reconcile_pending_orders():
    """Startup: limit_order_id=None olan pending kayıtları için Binance'te eşleştirme.
    Crash güvenliği: state kaydedilip Binance emri oluşturulmadan crash'te kurtarma."""
    if not ENABLED:
        return
    state = _load_state()
    updated = False
    for sym, pos in list(state.get("positions", {}).items()):
        if pos.get("status") != "pending" or pos.get("limit_order_id") is not None:
            continue
        print(f"[MONITOR] Reconcile: {sym} için limit_order_id=None, Binance sorgulanıyor", flush=True)
        try:
            open_orders = _get_client().get_open_orders(symbol=sym)
            limit_price = float(pos.get("limit_price", 0))
            matched = False
            for order in open_orders:
                if order.get("side") == "BUY" and order.get("type") == "LIMIT" and limit_price:
                    order_price = float(order.get("price", 0))
                    if abs(order_price - limit_price) / limit_price < 0.001:
                        pos["limit_order_id"] = order["orderId"]
                        state["positions"][sym] = pos
                        updated = True
                        matched = True
                        print(f"[MONITOR] Reconcile eşleşti: {sym} orderId={order['orderId']}", flush=True)
                        break
            if not matched:
                print(f"[MONITOR] Reconcile: {sym} open order yok, periyodik kontrol yönetir", flush=True)
        except BinanceAPIException as e:
            print(f"[MONITOR] Reconcile hatası {sym}: {e}", flush=True)
    if updated:
        with _lock:
            _save_state(state)


# ─── TICK İŞLEME ─────────────────────────────────────────────────────────────

def _process_tick(symbol: str, close: float, high: float, low: float):
    sell_reason        = None
    close_price        = None
    cancel_sl          = False
    place_trail_sl     = False   # TP1 hit → yeni trail SL emri
    update_trail_sl    = False   # Peak yükseldi → trail SL yenile
    cancel_trail_sl_id = None    # Trail tetiklendi → emri iptal et
    old_trail_sl_id    = None
    trail_sl_peak      = None
    trail_sl_qty       = 0.0
    pos_snap           = None

    with _lock:
        state = _load_state()
        pos = state["positions"].get(symbol)
        if pos is None or pos.get("status") == "pending":
            return

        # Satış işlemi devam ediyorsa bu tick'i atla
        if pos.get("closing"):
            return

        pos["current_price"] = close
        pos["last_tick_at"]  = datetime.now(timezone.utc).isoformat()
        state["positions"][symbol] = pos
        _save_state(state)
        pos_snap = dict(pos)

        # ÖNEMLİ SIRALAMA: TP1/stop kontrolü HER ZAMAN önce çalışır, expire kontrolü
        # SADECE trailing'e hiç geçmemiş ("gelişmeyen") pozisyonlar için, en son
        # çare olarak devreye girer. Eskiden expire en başta koşulsuz çalışıyordu —
        # süre dolunca o pozisyon için bir daha ASLA TP1/stop kontrol edilmiyordu,
        # fiyat TP1'i geçip trailing'e hak kazansa bile bot bunu hiç görmüyordu
        # (DGB'de yaşandı: peak +%9.88 oldu ama bot expire'a takılı kaldığı için
        # trailing'e hiç geçemedi). Trailing'e geçmiş pozisyon artık expire'dan
        # tamamen muaf — sadece trail_stop'a düşünce kapanır, saat sınırı yok.
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
                cancel_sl          = True
                pos["tp1_hit"]     = True
                pos["trailing"]    = True
                pos["peak"]        = max(high, float(pos["peak"]))
                pos["trailing_sl_id"] = None
                state["positions"][symbol] = pos
                _save_state(state)
                pos_snap      = dict(pos)
                place_trail_sl = True
                trail_sl_peak  = float(pos["peak"])
                trail_sl_qty   = float(pos.get("qty", 0))
                print(f"[MONITOR] TP1 HIT — {symbol} @ {high:.6g} | trailing başladı", flush=True)
            else:
                # Ne stop ne TP1 — gelişmeyen işlem, expire burada devreye girer
                fill_time_str = pos.get("open_time")
                if fill_time_str:
                    try:
                        ft = datetime.fromisoformat(fill_time_str)
                        if ft.tzinfo is None:
                            ft = ft.replace(tzinfo=timezone.utc)
                        if datetime.now(timezone.utc) - ft >= timedelta(hours=OPEN_EXPIRE_H):
                            sell_reason = "expire"
                            close_price = close
                            pos["closing"] = True
                            state["positions"][symbol] = pos
                            _save_state(state)
                            # Aktif resting SL emri iptal edilmezse coin'ler o emirde
                            # kilitli kalır — market_sell "insufficient balance" alıp
                            # sonsuza kadar başarısız olur (AWE/ZKC'de yaşandı).
                            cancel_sl = True
                    except Exception:
                        pass

        else:
            # Peak: mumun high'ına göre güncelle
            if high > float(pos["peak"]):
                old_trail_sl_id = pos.get("trailing_sl_id")
                pos["peak"] = high
                pos["trailing_sl_id"] = None   # yeni emir gelene kadar None
                state["positions"][symbol] = pos
                _save_state(state)
                pos_snap       = dict(pos)
                update_trail_sl = True
                trail_sl_peak   = high
                trail_sl_qty    = float(pos.get("qty", 0))

            # Trail kontrolü: mumun low'una göre (cache'li ATR — network çağrısı lock içinde yapılmaz)
            trail_stop = _trail_stop_price(float(pos["peak"]), pos.get("atr"))
            if low <= trail_stop:
                cancel_trail_sl_id = pos.get("trailing_sl_id")
                sell_reason = "trail_stop"
                close_price = trail_stop
                pos["closing"] = True
                state["positions"][symbol] = pos
                _save_state(state)

    # ── Lock dışı işlemler (Binance API çağrıları) ───────────────────────────
    if cancel_sl:
        _cancel_sl(symbol, pos_snap.get("sl_order_id"))

    if place_trail_sl or update_trail_sl:
        if update_trail_sl:
            _cancel_sl(symbol, old_trail_sl_id)

        atr_val = pos_snap.get("atr")
        if place_trail_sl or _atr_is_stale(pos_snap):
            fresh_atr = _compute_atr(symbol)
            if fresh_atr:
                atr_val = fresh_atr

        new_trail_id = _place_trail_sl_order(symbol, trail_sl_peak, trail_sl_qty, atr_val)
        if new_trail_id:
            with _lock:
                s = _load_state()
                if symbol in s["positions"]:
                    s["positions"][symbol]["trailing_sl_id"] = new_trail_id
                    s["positions"][symbol]["atr"] = atr_val
                    s["positions"][symbol]["atr_updated_at"] = datetime.now(timezone.utc).isoformat()
                    _save_state(s)

        if place_trail_sl:
            # Portfolio dashboard'a TP1 vurulduğunu bildir — aksi halde bot devredeyken
            # portfolio bunu hiç öğrenemiyor, "TRAIL AKTİF" hiç görünmüyor, Trail/Stop
            # sütunu orijinal stop'ta donuk kalıyor.
            entry = float(pos_snap.get("entry", 0) or 0)
            peak  = float(pos_snap.get("peak", 0) or 0)
            tp1_pct = round((peak - entry) / entry * 100, 2) if entry else 0
            _notify_portfolio_with_retry("/api/tp1-hit", {
                "symbol": symbol, "peak": peak, "tp1_pct": tp1_pct, "atr": atr_val,
            })

    if cancel_trail_sl_id:
        _cancel_sl(symbol, cancel_trail_sl_id)

    if sell_reason:
        # try/finally: closing=True'dan sonra beklenmeyen bir hata olursa bile
        # bayrak temizlenir — aksi halde pozisyon sessizce sonsuza kadar atlanır.
        closed = False
        try:
            entry = float(pos_snap.get("entry", 0) or 0)
            qty   = _round_qty(float(pos_snap.get("qty", 0) or 0), symbol)
            closed = _market_sell(symbol, qty, sell_reason)
            if closed:
                with _lock:
                    s = _load_state()
                    s["positions"].pop(symbol, None)
                    _save_state(s)
                _stop_stream(symbol)
                pct = round((close_price - entry) / entry * 100, 2) if entry and close_price else 0
                emoji = "⏰" if sell_reason == "expire" else ("💰" if pct > 0 else "🔴")
                _send_telegram(
                    f"{emoji} <b>POZİSYON KAPANDI — {symbol}</b>\n"
                    f"Sebep: {sell_reason}\nGiriş: {entry:.6g} | Çıkış: ~{close_price:.6g}\nP&L: {pct:+.2f}%"
                )
                _notify_portfolio_with_retry("/api/position-closed", {
                    "symbol": symbol, "reason": sell_reason,
                    "close_price": close_price, "pnl_pct": pct,
                })
                print(f"[MONITOR] Pozisyon kapatıldı: {symbol} | {sell_reason} | {pct:+.2f}%", flush=True)
        finally:
            if not closed:
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
            with _streams_lock:
                _streams.pop(symbol, None)   # periyodik kontrol yeniden başlatır
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
    """Tek bir sembolün stream'i başlatılamazsa exception fırlatmaz —
    aksi halde _periodic_check'teki tek try/except TÜM döngüyü (diğer semboller dahil)
    o turda erkenden keser. Hata loglanır, çağıran taraf sonraki turda tekrar dener."""
    global _twm
    with _streams_lock:
        if symbol in _streams:
            return
        try:
            key = _twm.start_kline_socket(callback=_make_handler(symbol), symbol=symbol, interval="1m")
        except Exception as e:
            print(f"[MONITOR] Stream başlatma HATA {symbol}: {e}", flush=True)
            return
        _streams[symbol] = key
    print(f"[MONITOR] WS başladı: {symbol}", flush=True)
    # Watchdog "son tick" referansını open_time'a düşürmesin (pozisyon günlerdir
    # açıksa bu HER ZAMAN bayat görünür, stream'i ilk tick'ini almadan tekrar
    # tekrar yeniden başlatıp kendi kendini sabote eder — DGB'de tam bu yaşandı).
    # Stream (yeniden) başladığı anı "canlı" kabul et, gerçek tick gelince
    # zaten üzerine yazılacak.
    with _lock:
        s = _load_state()
        p = s["positions"].get(symbol)
        if p is not None:
            p["last_tick_at"] = datetime.now(timezone.utc).isoformat()
            s["positions"][symbol] = p
            _save_state(s)


def _stop_stream(symbol: str):
    with _streams_lock:
        key = _streams.pop(symbol, None)
    if key and _twm:
        try:
            _twm.stop_socket(key)
        except Exception:
            pass
    print(f"[MONITOR] WS durdu: {symbol}", flush=True)


# ─── PERİYODİK KONTROL ───────────────────────────────────────────────────────

def _check_expired_positions_no_tick():
    """Expire kontrolü normalde SADECE _process_tick içinde (websocket tick geldiğinde)
    çalışır. Stream hiç veri akıtmazsa (AWE'de yaşandı — 2 günden fazla tek tick
    gelmedi) expire matematiksel olarak asla tetiklenemez, pozisyon sonsuza kadar
    açık kalır. Bu fonksiyon tick'ten tamamen bağımsız, periyodik döngüde çalışan
    bir güvenlik ağı — aynı cancel-önce-sat mantığını tick'siz de uygular."""
    state = _load_state()
    positions = dict(state.get("positions", {}))
    now = datetime.now(timezone.utc)

    for sym, pos in positions.items():
        try:
            if pos.get("status") != "open" or pos.get("closing"):
                continue
            # Trailing'e geçmiş (TP1 vurmuş) pozisyon expire'dan muaf — sadece
            # trail_stop'a düşünce kapanır, saat sınırı yok (DGB'nin yaşadığı
            # "expire trailing'i bloke ediyor" sorunuyla aynı prensip).
            if pos.get("trailing"):
                continue
            fill_time_str = pos.get("open_time")
            if not fill_time_str:
                continue
            try:
                ft = datetime.fromisoformat(fill_time_str)
                if ft.tzinfo is None:
                    ft = ft.replace(tzinfo=timezone.utc)
            except Exception:
                continue
            if now - ft < timedelta(hours=OPEN_EXPIRE_H):
                continue

            with _lock:
                s = _load_state()
                p = s["positions"].get(sym)
                if not p or p.get("status") != "open" or p.get("closing") or p.get("trailing"):
                    continue
                p["closing"] = True
                s["positions"][sym] = p
                _save_state(s)

            # closing=True'dan sonraki her şey try/finally ile korunuyor —
            # aksi halde beklenmeyen bir hata "closing" bayrağını sonsuza kadar
            # takılı bırakır ve pozisyon sessizce (hiçbir log satırı olmadan)
            # her turda atlanır (AWE'de tam bu yaşandı).
            closed = False
            try:
                # trailing=True olan pozisyonlar üstteki kontrolle zaten atlanıyor —
                # buraya gelen her şey her zaman sabit sl_order_id ile korunuyordur.
                _cancel_sl(sym, pos.get("sl_order_id"))

                entry       = float(pos.get("entry", 0) or 0)
                qty         = _round_qty(float(pos.get("qty", 0) or 0), sym)
                close_price = float(pos.get("current_price") or entry or 0)
                closed = _market_sell(sym, qty, "expire_no_tick")
                if closed:
                    with _lock:
                        s = _load_state()
                        s["positions"].pop(sym, None)
                        _save_state(s)
                    _stop_stream(sym)
                    pct = round((close_price - entry) / entry * 100, 2) if entry else 0
                    _send_telegram(
                        f"⏰ <b>POZİSYON KAPANDI (tick akışı yoktu) — {sym}</b>\n"
                        f"Sebep: expire_no_tick\nGiriş: {entry:.6g} | Çıkış: ~{close_price:.6g}\nP&L: {pct:+.2f}%"
                    )
                    _notify_portfolio_with_retry("/api/position-closed", {
                        "symbol": sym, "reason": "expire_no_tick",
                        "close_price": close_price, "pnl_pct": pct,
                    })
                    print(f"[MONITOR] Pozisyon kapatıldı (tick'siz expire): {sym} | {pct:+.2f}%", flush=True)
            finally:
                if not closed:
                    with _lock:
                        s = _load_state()
                        if sym in s["positions"]:
                            s["positions"][sym].pop("closing", None)
                            _save_state(s)
                    print(f"[MONITOR] SATIŞ BAŞARISIZ (tick'siz expire): {sym}, sonraki periyodik turda tekrar dener", flush=True)
        except Exception as e:
            print(f"[MONITOR] Tick'siz expire hatası {sym}: {e}", flush=True)
            continue


def _periodic_check():
    while True:
        time.sleep(CHECK_INTERVAL)
        try:
            _check_monitoring_entries()
            _check_pending_orders()
            _check_expired_positions_no_tick()

            state     = _load_state()
            positions = state.get("positions", {})
            open_syms = {s for s, p in positions.items() if p.get("status", "open") == "open"}

            with _streams_lock:
                current_streams = set(_streams.keys())

            for sym in open_syms:
                if sym not in current_streams:
                    _start_stream(sym)

            for sym in list(current_streams):
                if sym not in open_syms:
                    _stop_stream(sym)

            # Zombi stream tespiti: "aktif" görünüyor ama uzun süredir tick gelmiyor
            with _streams_lock:
                current_streams = set(_streams.keys())
            now_utc = datetime.now(timezone.utc)
            for sym in open_syms:
                if sym not in current_streams:
                    continue  # az önce başlatıldı veya hiç başlamadı, üstteki blok yönetiyor
                pos = positions.get(sym, {})
                if pos.get("closing"):
                    continue  # satış sürüyor, dokunma
                # Tick hiç gelmediyse (yeni açılan pozisyon) open_time referans alınır —
                # stream'e daha ilk tick'i gelme fırsatı bile vermeden yeniden başlatmayı önler
                last_tick_str = pos.get("last_tick_at") or pos.get("open_time")
                is_stale = True
                if last_tick_str:
                    try:
                        last_tick = datetime.fromisoformat(last_tick_str)
                        if last_tick.tzinfo is None:
                            last_tick = last_tick.replace(tzinfo=timezone.utc)
                        is_stale = (now_utc - last_tick).total_seconds() >= STREAM_STALE_S
                    except Exception:
                        is_stale = True
                if is_stale:
                    print(f"[MONITOR] Zombi stream tespit edildi: {sym} (son tick: {pos.get('last_tick_at') or 'hiç'}) — yeniden başlatılıyor", flush=True)
                    _stop_stream(sym)
                    _start_stream(sym)

            for sym in list(open_syms):
                try:
                    pos = positions.get(sym, {})
                    if pos.get("closing"):
                        continue  # _process_tick zaten yönetiyor
                    sl_order_id      = pos.get("sl_order_id")
                    trailing_sl_id   = pos.get("trailing_sl_id")
                    is_trailing      = pos.get("trailing", False)

                    if not is_trailing and not sl_order_id:
                        # Retroaktif SL — 3 başarısız deneme sonrası durur (spam önlemi)
                        if pos.get("sl_fail_count", 0) < 3:
                            _place_retroactive_sl(sym, pos)
                        # else: zaten bildirildi, tekrar deneme yok
                    elif is_trailing and not trailing_sl_id:
                        # Retroaktif trail SL — trailing modunda ama Binance emri yok
                        qty = float(pos.get("qty", 0))
                        peak = float(pos.get("peak", 0))
                        if qty > 0 and peak > 0:
                            atr_val = pos.get("atr") or _compute_atr(sym)
                            new_id = _place_trail_sl_order(sym, peak, qty, atr_val)
                            if new_id:
                                with _lock:
                                    s = _load_state()
                                    if sym in s["positions"]:
                                        s["positions"][sym]["trailing_sl_id"] = new_id
                                        s["positions"][sym]["atr"] = atr_val
                                        s["positions"][sym]["atr_updated_at"] = datetime.now(timezone.utc).isoformat()
                                        _save_state(s)
                                _send_telegram(
                                    f"🛡 <b>Retroaktif Trail SL — {sym}</b>\n"
                                    f"Peak: {peak:.6g} | Trail stop: {_trail_stop_price(peak, atr_val):.6g}"
                                )
                    elif is_trailing and trailing_sl_id and _atr_is_stale(pos):
                        # ATR bayatladı (peak uzun süredir yükselmedi) — yenile, trail SL emrini güncelle
                        qty = float(pos.get("qty", 0))
                        peak = float(pos.get("peak", 0))
                        fresh_atr = _compute_atr(sym)
                        if fresh_atr and qty > 0 and peak > 0:
                            new_id = _place_trail_sl_order(sym, peak, qty, fresh_atr)
                            if new_id:
                                _cancel_sl(sym, trailing_sl_id)
                                with _lock:
                                    s = _load_state()
                                    if sym in s["positions"]:
                                        s["positions"][sym]["trailing_sl_id"] = new_id
                                        s["positions"][sym]["atr"] = fresh_atr
                                        s["positions"][sym]["atr_updated_at"] = datetime.now(timezone.utc).isoformat()
                                        _save_state(s)
                                print(f"[MONITOR] ATR yenilendi: {sym} atr={fresh_atr:.6g}", flush=True)

                    # SL fill kontrolü
                    if sl_order_id and _is_sl_filled(sym, sl_order_id):
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
                        _notify_portfolio_with_retry("/api/position-closed", {
                            "symbol": sym, "reason": "sl_binance",
                            "close_price": sl, "pnl_pct": pct,
                        })
                        print(f"[MONITOR] SL doldu (Binance): {sym} | {pct:+.2f}%", flush=True)

                    # Trail SL fill kontrolü — bot çöküp Binance trailing SL tetiklendiyse
                    elif trailing_sl_id and _is_sl_filled(sym, trailing_sl_id):
                        with _lock:
                            s = _load_state()
                            s["positions"].pop(sym, None)
                            _save_state(s)
                        _stop_stream(sym)
                        entry = float(pos.get("entry", 0))
                        peak  = float(pos.get("peak", 0))
                        cl_price = round(_trail_stop_price(peak, pos.get("atr")), 8)
                        pct   = round((cl_price - entry) / entry * 100, 2) if entry else 0
                        _send_telegram(
                            f"🟡 <b>TRAIL SL TETİKLENDİ (Binance) — {sym}</b>\n"
                            f"Giriş: {entry:.6g} | Trail stop: {cl_price:.6g} | P&L: {pct:+.2f}%"
                        )
                        _notify_portfolio_with_retry("/api/position-closed", {
                            "symbol": sym, "reason": "trail_binance",
                            "close_price": cl_price, "pnl_pct": pct,
                        })
                        print(f"[MONITOR] Trail SL doldu (Binance): {sym} | {pct:+.2f}%", flush=True)

                except Exception as e:
                    print(f"[MONITOR] Periyodik SL/reconcile hatası {sym}: {e}", flush=True)
                    continue

        except Exception as e:
            print(f"[MONITOR] Periyodik kontrol hatası: {e}", flush=True)


# ─── START ───────────────────────────────────────────────────────────────────

def start():
    global _twm
    print("[MONITOR] Başlatılıyor...", flush=True)

    _twm = ThreadedWebsocketManager(api_key=API_KEY, api_secret=API_SECRET)
    _twm.start()

    _reconcile_sl_orders()        # open pozisyonlar için mevcut SL emirlerini eşleştir
    _reconcile_pending_orders()   # limit_order_id=None olan pending kayıtları onar

    state = _load_state()
    for sym, pos in state.get("positions", {}).items():
        # Pending pozisyonlar için WS başlatma; periyodik kontrol yönetir
        if pos.get("status", "open") == "open":
            _start_stream(sym)

    t = threading.Thread(target=_periodic_check, daemon=True)
    t.start()

    print("[MONITOR] Hazır.", flush=True)
    _twm.join()
