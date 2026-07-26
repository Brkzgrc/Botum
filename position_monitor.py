# -*- coding: utf-8 -*-
"""
Position Monitor — WebSocket fiyat takibi + trailing + pending order yönetimi
=============================================================================
Pending : Limit buy doldu mu? 48H geçti mi?
Open    : peak güncelle | SL doldu mu? | TP1 → trailing
"""

import fcntl, json, math, os, time, threading, requests
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
CLOSING_STUCK_S        = 300     # closing=True bu kadar saniyeden uzun takılıysa (sebep ne olursa olsun) otomatik temizlenir
FALLBACK_TRAIL_PCT     = 0.9816  # ATR çekilemezse: peak * bu değer (%1.84 sabit trailing)
TRAILING_DELTA_MIN_BIPS = 10     # Binance platform alt sınırı (%0.10) — sadece API güvenliği, ATR'yi kırpmaz
TRAILING_DELTA_MAX_BIPS = 2000   # Binance platform üst sınırı (%20.0) — sadece API güvenliği, ATR'yi kırpmaz
PENDING_EXPIRE_H       = 48      # Monitoring süresi: CHoCH+3tick bekleme (saat)
PENDING_ORDER_EXPIRE_H = 1       # Limit emir süresi: CHoCH+3tick→+1tick arası (saat)
OPEN_EXPIRE_H          = 24      # Açık trade max süresi: fill sonrası 24H geçince market sell
SL_LIMIT_BUFFER        = 0.003   # SL limit fiyatı = stop * (1 - 0.003)
CHECK_INTERVAL   = 60      # saniye
MAX_POSITIONS    = 5
MAX_POS_SIZE     = 20_000.0

# ── SHADOW / DRY-RUN: "+1.0% TP1 tavan" adayı (backtest doğrulaması: bkz.
# CLAUDE.md "Emir Akışı" bölümü) — SADECE gözlem, hiçbir gerçek emri etkilemez.
# Kullanıcı onayı: 2026-07-25. Amaç: 1-2 hafta canlı log biriktirip
# backtest'in 1H/15m tahminini gerçek 1m-kapalı-mum davranışıyla doğrulamak.
SHADOW_TP1_CAP_PCT  = float(os.getenv("SHADOW_TP1_CAP_PCT", "1.0"))
SHADOW_TP1_LOG_FILE = os.getenv("SHADOW_TP1_LOG_FILE", "/tmp/shadow_tp1_cap.jsonl")

# ── SHADOW ORPHAN TAKİBİ: gerçek pozisyon kapandığında sanal (shadow) hâlâ
# kendi sonucuna ulaşmamışsa, o coin'i AYRI ve salt-okunur bir listede
# (state["shadow_orphans"], gerçek pozisyon state'inden tamamen izole)
# izlemeye devam eder — hiçbir gerçek emre dokunmaz, SADECE kapanmış 1m
# mumlarla (REST) çalışır. Kullanıcı onayı: 2026-07-26.
SHADOW_ORPHAN_TIMEOUT_H = float(os.getenv("SHADOW_ORPHAN_TIMEOUT_H", "72"))
SHADOW_ORPHAN_MAX       = int(os.getenv("SHADOW_ORPHAN_MAX", "20"))

class _StateLock:
    """Dosya kilidi (fcntl.flock) — SADECE Render/Linux hedefli, fcntl POSIX-only
    (Windows'ta import hatası verir). Bu dosya zaten yalnızca Render'daki
    trading-bot servisinde çalışıyor, lokalde (Windows) hiç çalıştırılmıyor —
    kasıtlı olarak cross-platform fallback eklenmedi.

    trading_engine.py ve position_monitor.py
    AYNI state dosyasını, ikisi de kendi threading.Lock()'uyla koruyordu; bu
    iki farklı kilit nesnesi birbirini hiç görmüyordu (aynı process içinde bile),
    yani biri state'i okuyup yazarken diğeri araya girip "lost update" ile bir
    yazmayı sessizce kaybedebiliyordu. flock() dosya bazlı olduğu için hem
    aynı process'teki thread'leri hem FARKLI process'leri (örn. gunicorn çoklu
    worker) aynı anda kapsar — iki modül de aynı .lock dosyasını kilitlediği
    için ayrı nesne olmaları sorun değil."""
    def __init__(self, path):
        self._path = path
        self._fd = None

    def __enter__(self):
        self._fd = open(self._path, "a")
        fcntl.flock(self._fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        fcntl.flock(self._fd, fcntl.LOCK_UN)
        self._fd.close()
        self._fd = None


_client: Client | None = None
_lock         = _StateLock(STATE_FILE + ".lock")
_streams_lock = threading.Lock()   # _streams dict erişimi için ayrı kilit — state dosyasıyla ilgisiz, thread-lock yeterli
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
                "message_thread_id": 4,
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


def _market_sell(symbol: str, qty: float, reason: str) -> tuple[bool, float, float]:
    """State'teki qty gerçek bakiyeden fazla olabilir (komisyon kesintisi, manuel
    müdahale, eski kayıt drift'i vb.) — satıştan önce gerçek free balance'a kırpılır.
    Aksi halde -2010 (insufficient balance) hatası her tick'te aynı yanlış miktarla
    sonsuza kadar tekrar eder ve pozisyon asla kapanmaz.

    Döner: (closed, executed_qty, executed_quote_qty). Son ikisi bu ÇAĞRIDA
    gerçekten dolan miktar/tutar — çağıran taraf art arda gelen kısmi
    dolumları toplayıp gerçek ortalama satış fiyatını hesaplayabilsin diye.
    Market emri düşük likiditeli bir coinde (ANKR'de yaşandığı gibi) kısmen
    dolup exception fırlatmadan dönebilir — bu durumda executedQty istenenden
    az olur, "closed" False döner ve kalan miktar bir sonraki denemede
    (fonksiyon başındaki gerçek bakiye kontrolü sayesinde) otomatik satılır."""
    if not ENABLED:
        print(f"[MONITOR] SELL {symbol} {qty} ({reason}) — SİMÜLASYON", flush=True)
        return True, 0.0, 0.0

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
                return False, 0.0, 0.0
            if open_orders:
                oids = [o.get("orderId") for o in open_orders]
                print(f"[MONITOR] SELL {symbol} ({reason}) — bakiye sıfır AMA hâlâ açık emir var {oids}, satılmış SAYILMIYOR, tekrar denenecek", flush=True)
                return False, 0.0, 0.0
            print(f"[MONITOR] SELL {symbol} ({reason}) — bakiye sıfır, açık emir de yok, zaten satılmış kabul ediliyor", flush=True)
            return True, 0.0, 0.0
        print(f"[MONITOR] SELL {symbol} ({reason}) — miktar hesaplanamadı, tekrar denenecek", flush=True)
        return False, 0.0, 0.0

    tag = f"[MONITOR] SELL {symbol} {sell_qty} ({reason})" + (f" [state qty={qty} idi]" if sell_qty != qty else "")
    try:
        order    = _get_client().order_market_sell(symbol=symbol, quantity=sell_qty)
        executed = float(order.get("executedQty", 0) or 0)
        quote    = float(order.get("cummulativeQuoteQty", 0) or 0)
        if executed < sell_qty * 0.999:
            print(f"{tag} — KISMİ DOLDU ({executed}/{sell_qty}), tekrar denenecek", flush=True)
            return False, executed, quote
        print(f"{tag} — OK", flush=True)
        return True, executed, quote
    except Exception as e:
        print(f"{tag} — HATA: {e}", flush=True)
        return False, 0.0, 0.0


def _get_usdt_balance() -> float:
    try:
        bal = _get_client().get_asset_balance(asset="USDT")
        return float(bal["free"]) if bal else 0.0
    except Exception as e:
        print(f"[MONITOR] Bakiye hatası: {e}", flush=True)
        return 0.0


def _cancel_sl(symbol: str, sl_order_id):
    """Her türlü hatayı (sadece BinanceAPIException değil) yutar — bu fonksiyon
    _process_tick'in "closing=True" try/finally koruması BAŞLAMADAN ÖNCE
    çağrılıyor (cancel_sl bloğu sell_reason bloğundan önce). Buradan sızan
    beklenmedik bir exception (network timeout vb.) closing bayrağının
    ASLA temizlenememesine yol açar — tam da DGB'de tekrar yaşandı."""
    if not sl_order_id:
        return
    if not ENABLED:
        print(f"[MONITOR] SL iptali SİMÜLASYON — {symbol} orderId={sl_order_id}", flush=True)
        return
    try:
        _get_client().cancel_order(symbol=symbol, orderId=sl_order_id)
        print(f"[MONITOR] SL iptal — {symbol} orderId={sl_order_id}", flush=True)
    except Exception as e:
        print(f"[MONITOR] SL iptal HATA {symbol}: {e}", flush=True)


def _compute_atr(symbol: str, period: int = ATR_PERIOD):
    """Binance'ten son 1H mumları çekip Wilder ATR(period) hesaplar. Hata/yetersiz veri → None."""
    try:
        klines = _get_client().get_klines(symbol=symbol, interval=Client.KLINE_INTERVAL_1HOUR, limit=period * 5)
    except Exception as e:
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


def _trail_stop_price(peak: float, atr, entry: float = 0) -> float:
    """peak - ATR_MULT*ATR; ATR yoksa/geçersizse sabit %1.84 yedeğe düşer.
    Backtestteki gibi entry'nin altına düşmez (smc_atr_trail.py exit_trail)."""
    if atr and atr > 0:
        trail = peak - ATR_MULT * atr
    else:
        trail = peak * FALLBACK_TRAIL_PCT
    if entry and trail < entry:
        trail = entry
    return trail


def _atr_is_stale(pos: dict) -> bool:
    ts = pos.get("atr_updated_at")
    if not ts:
        return True
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds()
        return age >= ATR_REFRESH_S
    except Exception:
        return True


def _place_trail_sl_order(symbol: str, peak: float, qty: float, atr=None, entry: float = 0):
    """peak - ATR_MULT*ATR (veya ATR yoksa peak*FALLBACK_TRAIL_PCT) seviyesinde STOP_LOSS_LIMIT emri aç. order_id döndürür."""
    trail_stop_raw = _trail_stop_price(peak, atr, entry)
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
    except Exception as e:
        print(f"[MONITOR] Trail SL emir HATA {symbol}: {e}", flush=True)
        return None


def _place_trailing_delta_order(symbol: str, peak: float, qty: float, atr=None):
    """Binance NATIVE trailing stop (trailingDelta, sunucu tarafında) — TP1 sonrası
    tercih edilen yöntem. Mesafe, o anki gerçek ATR_MULT*ATR yüzdesi — backtestin
    (smc_atr_trail.py exit_trail) formülüyle birebir aynı. Sadece Binance'in
    platform sınırlarına (10-2000 bips) kırpılır, sabit dar banda değil.
    Sunucu tarafında çalıştığı için bot çökse/tick kaybetse bile emir kendi
    kendine güncellenir — zombi-stream/cancel-replace sınıfı sorunları ortadan
    kaldırır. Ancak mesafe kuruluşta sabitlenir (Binance kendisi güncellemez) —
    bu yüzden çağıran taraf ATR bayatladıkça bu emri periyodik olarak iptal edip
    güncel ATR ile yeniden kurar (bkz. _periodic_check reconcile döngüsü).
    Başarısız olursa None döner, çağıran taraf eski ATR cancel-replace
    yöntemine (_place_trail_sl_order) düşer."""
    if not ENABLED:
        print(f"[MONITOR] TrailingDelta SİMÜLASYON — {symbol} qty={qty}", flush=True)
        return None
    try:
        if atr and atr > 0 and peak > 0:
            pct = (ATR_MULT * atr / peak) * 100
        else:
            pct = (1 - FALLBACK_TRAIL_PCT) * 100
        pct  = max(TRAILING_DELTA_MIN_BIPS / 100, min(TRAILING_DELTA_MAX_BIPS / 100, pct))
        bips = int(round(pct * 100))
        qty_r = _round_qty(qty, symbol)
        order = _get_client().create_order(
            symbol=symbol, side="SELL", type="STOP_LOSS",
            quantity=qty_r, trailingDelta=bips,
        )
        oid = order["orderId"]
        print(f"[MONITOR] Native trailing emri: {symbol} delta={bips}bips (%{pct:.2f}) qty={qty_r} id={oid}", flush=True)
        return oid
    except Exception as e:
        print(f"[MONITOR] Native trailing emir HATA {symbol}: {e} — ATR cancel-replace'e düşülüyor", flush=True)
        return None


def _is_sl_filled(symbol: str, sl_order_id) -> bool:
    if not sl_order_id or not ENABLED:
        return False
    try:
        order = _get_client().get_order(symbol=symbol, orderId=sl_order_id)
        return order.get("status") == "FILLED"
    except Exception as e:
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
    except Exception as e:
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
    except Exception as e:
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
            # Shadow/dry-run: gerçek sistemden bağımsız, sadece gözlem amaçlı.
            # shadow_peak KENDİ alanı olarak tutuluyor (pos["peak"]'ten TÜRETİLMİYOR)
            # — ölçüm temizliği için: gerçek sistemin peak güncelleme mantığı
            # ileride değişse bile shadow'un doğruluğu buna bağımlı olmasın.
            #
            # shadow_valid=True SADECE bu fonksiyon (gerçek fill anı) çalıştığında
            # set edilir. Eğer bir pozisyon bu shadow kodu devreye girmeden ÖNCE
            # zaten açılmışsa, onun state kaydında shadow_valid hiç olmaz (eski
            # kod bu alanları yazmıyordu) — _shadow_evaluate bunu görüp o pozisyonu
            # asla değerlendirmeye almaz. Adil karşılaştırma için şart: shadow
            # SADECE gerçek fill anından itibaren, kendi başına başlamış
            # pozisyonlarda geçerli sayılmalı, sonradan "yapıştırılmış" olamaz.
            "shadow_tp1_cap_pct": SHADOW_TP1_CAP_PCT,
            "shadow_tp1":         min(float(pos["tp1"]), fill_price * (1 + SHADOW_TP1_CAP_PCT / 100.0)),
            "shadow_trailing":    False,
            "shadow_peak":        fill_price,
            "shadow_exit":        None,
            "shadow_started_at":  now,
            "shadow_valid":       True,
            "shadow_origin":      "fill_time",
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
        except Exception as e:
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
    """48H doldu: limit emri iptal et, state'den sil, bildir.

    cancel_order()'ın kendi yanıtına güvenmek yerine (kısmi dolum + geçici
    hata bir araya geldiğinde yanlış "iptal edildi" sonucuna varmak riskli),
    iptal denemesinden SONRA her zaman get_order() ile emrin GERÇEK, o anki
    durumunu sorup ona göre karar veriyoruz — dört olası durum:
      1) executedQty>0 VE emir artık kapalı (FILLED/CANCELED/EXPIRED)
         → kısmen/tamamen dolmuş, gerçek pozisyon olarak aktive et.
      2) executedQty=0 VE emir kapalı → normal iptal, pozisyon yok.
      3) emir HÂLÂ AÇIK (NEW/PARTIALLY_FILLED) → iptal gerçekte gitmemiş
         (geçici hata) — state'e DOKUNMA, bir sonraki turda tekrar denenir.
      4) get_order() sorgusu da başarısız → aynı şekilde state'e dokunma."""
    order_id = pos.get("limit_order_id")
    if order_id and ENABLED:
        try:
            _get_client().cancel_order(symbol=symbol, orderId=order_id)
            print(f"[MONITOR] Limit emir iptal: {symbol} orderId={order_id}", flush=True)
        except Exception as e:
            print(f"[MONITOR] Limit emir iptal HATA {symbol}: {e} — gerçek durum sorgulanacak", flush=True)

        try:
            order = _get_client().get_order(symbol=symbol, orderId=order_id)
        except Exception as e:
            print(f"[MONITOR] {symbol} iptal-sonrası durum sorgusu başarısız: {e} — "
                  f"state'e dokunulmadı, bir sonraki turda tekrar denenecek", flush=True)
            return

        status       = order.get("status")
        executed_qty = float(order.get("executedQty", 0) or 0)

        if executed_qty > 0 and status in ("FILLED", "CANCELED", "EXPIRED"):
            quote_qty  = float(order.get("cummulativeQuoteQty", 0) or 0)
            fill_price = quote_qty / executed_qty if executed_qty else 0.0
            print(f"[MONITOR] {symbol} iptal-sonrası dolum tespit: {executed_qty}@{fill_price:.6g} "
                  f"(status={status}) — pozisyon aktive ediliyor", flush=True)
            _activate_position(symbol, fill_price, executed_qty, pos)
            return

        if status in ("NEW", "PARTIALLY_FILLED"):
            # Emir hâlâ Binance'te AÇIK — iptal denemesi gerçekte işlememiş
            # (geçici hata). State'i SİLERSEK bu emri bir daha asla izlemeyiz,
            # coin'ler ileride sessizce dolabilir. Dokunmadan bırak, periyodik
            # döngü bir sonraki turda tekrar iptal dener.
            print(f"[MONITOR] {symbol} emri hâlâ açık (status={status}) — iptal başarısız, "
                  f"tekrar denenecek", flush=True)
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
    except Exception as e:
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
        except Exception as e:
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
        except Exception as e:
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
        except Exception as e:
            print(f"[MONITOR] Reconcile hatası {sym}: {e}", flush=True)
    if updated:
        with _lock:
            _save_state(state)


# ─── SHADOW / DRY-RUN (TP1 tavan adayı — sadece gözlem) ──────────────────────
# Bu bölüm gerçek emir akışına DOKUNMAZ: sadece _trail_stop_price() (salt
# okunur hesaplama) çağırır ve dosyaya JSONL log yazar. _cancel_sl,
# _place_trail_sl_order, _place_trailing_delta_order, _market_sell —
# bunların HİÇBİRİ shadow kod yolundan çağrılmaz.

_shadow_log_lock = threading.Lock()

def _shadow_dispatch(event: dict):
    """TEK giriş noktası: local JSONL'e yazar + portfolio_tracker'a best-effort
    POST eder (arka plan thread, TEK deneme — shadow olayları kritik değil,
    local dosya zaten kalıcı kayıt; portfolio_tracker'a POST sadece dashboard
    için "iyi olsun" niteliğinde, kaybolursa dashboard'da o satır eksik kalır,
    başka hiçbir şeyi etkilemez). Bu fonksiyon LOCK DIŞINDA çağrılmalı —
    ne dosya I/O'su ne network çağrısı _lock tutulurken yapılmamalı, aksi
    halde TÜM sembollerin gerçek stop/TP1 tespiti gecikebilir."""
    event = {"ts": datetime.now(timezone.utc).isoformat(), **event}
    try:
        line = json.dumps(event, ensure_ascii=False, default=str)
        with _shadow_log_lock:
            with open(SHADOW_TP1_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception as e:
        print(f"[SHADOW] local log hatası {event.get('symbol')}: {e}", flush=True)

    def _send():
        try:
            _notify_portfolio("/api/shadow-event", event)
        except Exception as e:
            print(f"[SHADOW] portfolio POST hatası {event.get('symbol')}: {e}", flush=True)
    threading.Thread(target=_send, daemon=True).start()


_STATUS_REAL_PRE_TP1  = "pre_tp1"
_STATUS_REAL_TRAILING = "trailing"


def _shadow_evaluate(symbol: str, pos: dict, high: float, low: float):
    """pos'u yerinde (in-place) günceller. HİÇBİR I/O YAPMAZ — sadece hesaplar
    ve event dict'leri üretir; gerçek I/O çağıran taraf tarafından LOCK
    DIŞINDA (_shadow_dispatch ile) yapılmalı. Gerçek sistemin trailing/stop/
    TP1 durumundan tamamen bağımsız çalışır — kendi shadow_tp1'ine ve KENDİ
    shadow_peak'ine göre karar verir (pos["peak"]'ten TÜRETİLMİYOR — gerçek
    sistemin peak güncelleme mantığı ileride değişse bile shadow'un doğruluğu
    buna bağımlı olmasın diye), sadece stop/ATR-trail formülünü paylaşır.
    Dönüş: (pos değişti mi, event listesi)."""
    events = []
    if pos.get("shadow_exit") is not None:
        return False, events
    shadow_tp1 = float(pos.get("shadow_tp1", 0) or 0)
    if not shadow_tp1:
        return False, events   # bu deploy'dan önce açılmış eski pozisyon — shadow alanı yok
    if pos.get("shadow_valid") is not True:
        # shadow_valid SADECE _activate_position (gerçek fill anı) tarafından
        # set edilir. Burada True değilse (yok/False) bu pozisyon shadow kodu
        # devreye girmeden önce açılmış demektir — adil kıyas için bu
        # pozisyonu HİÇ değerlendirmeye almıyoruz, yeni event üretmiyoruz.
        return False, events

    entry_px = float(pos.get("entry", 0) or 0)
    real_tp1 = float(pos.get("tp1", 0) or 0)
    real_status = _STATUS_REAL_TRAILING if pos.get("trailing") else _STATUS_REAL_PRE_TP1
    dirty = False

    old_shadow_peak = float(pos.get("shadow_peak", entry_px) or entry_px)
    shadow_peak_now = max(old_shadow_peak, high)
    if shadow_peak_now > old_shadow_peak:
        pos["shadow_peak"] = shadow_peak_now
        dirty = True

    def _mk(event, shadow_status, shadow_trail=None, shadow_pct=None, note="", **extra):
        return {
            "event": event, "symbol": symbol,
            "real_status": real_status, "shadow_status": shadow_status,
            "real_tp1": real_tp1, "shadow_tp1": shadow_tp1,
            "shadow_peak": shadow_peak_now, "shadow_trail": shadow_trail,
            "shadow_pct": shadow_pct, "note": note,
            "shadow_valid": True, "shadow_origin": pos.get("shadow_origin", "fill_time"),
            **extra,
        }

    if not pos.get("shadow_trailing"):
        if high >= shadow_tp1:
            pos["shadow_trailing"] = True
            dirty = True
            shadow_trail_now = _trail_stop_price(shadow_peak_now, pos.get("atr"), entry_px)
            events.append(_mk(
                "SHADOW_WOULD_ACTIVATE_TRAIL", "trailing", shadow_trail=shadow_trail_now,
                note="Sanal TP1'e ulaşıldı, sanal trailing başladı."))
            if low <= shadow_trail_now:
                # Aynı kapanmış mumda hem TP1 dokundu hem hesaplanan trail seviyesi
                # kırıldı. BİLEREK shadow_exit SET EDİLMİYOR: bu trail seviyesi bu
                # mumun kendi high'ından türetildiği için, gerçek mum-içi sıra
                # (önce mi dokundu sonra mı düştü, yoksa tam tersi mi) bilinmiyor —
                # trail seviyesi belki fiyat düşerken henüz hiç var olmamıştı. Bunu
                # KESİN bir çıkış saymak yanıltıcı olur; sadece ayrı bir risk sinyali
                # olarak loglanıyor. shadow_trailing=True kalır, gerçek sonuç
                # sonraki mumlarda (trail seviyesi o mum başlamadan ÖNCE zaten sabit
                # olduğu için orada güvenilir) normal yoldan belirlenecek.
                shadow_pct_risk = round((shadow_trail_now - entry_px) / entry_px * 100, 2) if entry_px else 0
                events.append(_mk(
                    "SHADOW_SAME_CANDLE_TOUCH_AND_BREACH", "trailing", shadow_trail=shadow_trail_now,
                    shadow_pct=shadow_pct_risk,
                    note="Aynı kapanmış mumda hem sanal TP1'e dokundu hem hesaplanan trail seviyesi "
                         "kırıldı — mum-içi gerçek sıra bilinmediği için KESİN çıkış sayılmadı, sadece "
                         "risk sinyali. Gerçek sonuç sonraki mumlarda belirlenecek."))
    else:
        shadow_trail_now = _trail_stop_price(shadow_peak_now, pos.get("atr"), entry_px)
        if low <= shadow_trail_now:
            shadow_pct = round((shadow_trail_now - entry_px) / entry_px * 100, 2) if entry_px else 0
            pos["shadow_exit"] = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "price": shadow_trail_now, "reason": "trail", "same_candle": False,
            }
            dirty = True
            events.append(_mk(
                "SHADOW_WOULD_EXIT_TRAIL", "exited", shadow_trail=shadow_trail_now,
                shadow_pct=shadow_pct, same_candle=False,
                note="Sanal trailing seviyesi kırıldı, sanal pozisyon kapanmış olurdu."))

    return dirty, events


def _shadow_build_close_event(symbol: str, pos_snap: dict, live_reason: str, live_price: float, live_pct: float) -> dict:
    """Gerçek pozisyon kapanınca shadow karşılaştırma event'ini ÜRETİR (I/O
    yapmaz — çağıran _shadow_dispatch ile göndermeli)."""
    entry_px = float(pos_snap.get("entry", 0) or 0)
    real_tp1 = float(pos_snap.get("tp1", 0) or 0)
    shadow_tp1 = float(pos_snap.get("shadow_tp1", 0) or 0) or None
    shadow_exit = pos_snap.get("shadow_exit")
    shadow_trailing = bool(pos_snap.get("shadow_trailing"))

    if shadow_exit:
        shadow_price = float(shadow_exit.get("price", 0) or 0)
        shadow_pct = round((shadow_price - entry_px) / entry_px * 100, 2) if entry_px else 0
        shadow_result = "resolved"
        fark_pct = round(shadow_pct - live_pct, 2)
        shadow_exit_reason = shadow_exit.get("reason")
        note = (f"Gerçek {live_reason} ile kapandı ({live_pct:+.2f}%), sanal {shadow_exit_reason} ile "
                f"kapanmış olurdu ({shadow_pct:+.2f}%) — fark {fark_pct:+.2f} puan.")
        shadow_status = "exited"
    else:
        # Gerçek pozisyon shadow hiç sonuçlanmadan kapandı (shadow trailing'e
        # hiç geçmedi VEYA geçti ama kendi trail'i tetiklenmeden gerçek
        # pozisyon kapandı) — bu durumu ayrı bir sonuç olarak işaretle,
        # "aynı" ya da "sıfır fark" gibi varsayılan bir değere düşürme.
        shadow_price = None
        shadow_pct = None
        shadow_result = "undetermined"
        fark_pct = None
        shadow_exit_reason = None
        note = "Gerçek pozisyon kapandı ama sanal sonuçlanmadan — karşılaştırma belirsiz."
        shadow_status = _STATUS_REAL_TRAILING if shadow_trailing else "not_trailing"

    return {
        "event": "SHADOW_LIVE_CLOSED", "symbol": symbol,
        "real_status": f"closed:{live_reason}", "shadow_status": shadow_status,
        "real_tp1": real_tp1, "shadow_tp1": shadow_tp1,
        "shadow_peak": pos_snap.get("shadow_peak"), "shadow_trail": shadow_price,
        "shadow_pct": shadow_pct, "note": note,
        "live_reason": live_reason, "live_price": live_price, "live_pct": live_pct,
        "shadow_result": shadow_result, "shadow_exit_reason": shadow_exit_reason,
        "shadow_trailing": shadow_trailing, "fark_pct": fark_pct,
        "shadow_valid": pos_snap.get("shadow_valid") is True,
        "shadow_origin": pos_snap.get("shadow_origin", "fill_time"),
    }


# ─── SHADOW ORPHAN TAKİBİ ─────────────────────────────────────────────────────
# Gerçek pozisyon kapandığında sanal (shadow) hâlâ kendi sonucuna ulaşmamışsa
# (yukarıdaki _shadow_build_close_event'in "undetermined" dalı), fiyat takibi
# TAMAMEN durur ve sanal bir daha asla sonuçlanamaz — bu bölüm bunu çözer.
# Tamamen ayrı bir liste (state["shadow_orphans"]), gerçek pozisyon state'ine
# ("positions") hiç dokunmaz, hiçbir gerçek emir fonksiyonunu çağırmaz, SADECE
# kapanmış 1m mumlarla (REST) çalışır. Kullanıcı onayı: 2026-07-26.

def _register_shadow_orphan(state: dict, symbol: str, pos_snap: dict, live_reason: str,
                             live_price: float, live_pct: float):
    """Gerçek pozisyon kapanırken sanal hâlâ sonuçlanmamışsa, onu
    state['positions']'tan TAMAMEN AYRI bir listeye (state['shadow_orphans'])
    kaydeder ki _check_shadow_orphans() fiyatını izlemeye devam edebilsin.
    HİÇBİR I/O yapmaz — state parametresini YERİNDE değiştirir, kaydetmek
    (_save_state) çağıranın sorumluluğunda, aynı kilit içinde olmalı. Aynı
    sembol daha önce de orphan olmuş olsa bile HER ZAMAN yeni, benzersiz bir
    orphan_id alır (zaman damgalı) — sembol yeniden açılıp kapanırsa eski
    orphan ile yenisi asla karışmaz. Kapasite (SHADOW_ORPHAN_MAX) doluysa
    hiçbir şey eklemeden None döner — sanal o durumda hiç izlenemez."""
    orphans = state.setdefault("shadow_orphans", {})
    if len(orphans) >= SHADOW_ORPHAN_MAX:
        print(f"[SHADOW-ORPHAN] limit doldu ({SHADOW_ORPHAN_MAX}), {symbol} artık izlenemeyecek", flush=True)
        return None
    now_iso = datetime.now(timezone.utc).isoformat()
    orphan_id = f"{symbol}:{now_iso}"
    orphans[orphan_id] = {
        "orphan_id": orphan_id, "symbol": symbol,
        "open_time": pos_snap.get("open_time"), "closed_time": now_iso,
        "entry": pos_snap.get("entry"), "shadow_tp1": pos_snap.get("shadow_tp1"),
        "shadow_peak": pos_snap.get("shadow_peak"), "shadow_trailing": bool(pos_snap.get("shadow_trailing")),
        "atr": pos_snap.get("atr"), "real_tp1": pos_snap.get("tp1"),
        "live_reason": live_reason, "live_price": live_price, "live_pct": live_pct,
        "shadow_valid": True, "shadow_origin": pos_snap.get("shadow_origin", "fill_time"),
    }
    return orphan_id


def _orphan_is_timed_out(orph: dict, now: datetime) -> bool:
    """orph['closed_time']'tan itibaren SHADOW_ORPHAN_TIMEOUT_H saat geçti mi?
    Saf fonksiyon, I/O yapmaz."""
    try:
        created = datetime.fromisoformat(orph["closed_time"])
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
    except Exception:
        return False
    return (now - created).total_seconds() / 3600 >= SHADOW_ORPHAN_TIMEOUT_H


def _shadow_build_orphan_event(orph: dict, event_name: str, shadow_status: str,
                                shadow_pct=None, shadow_trail=None, fark_pct=None) -> dict:
    """Orphan takibi sonuçlanınca (SHADOW_ORPHAN_RESOLVED) ya da zaman aşımına
    uğrayınca (SHADOW_ORPHAN_TIMEOUT) gönderilecek event'i üretir (I/O yapmaz).
    orphan_id/open_time/closed_time HER ZAMAN taşınır — aynı sembolde bu arada
    yeni bir gerçek pozisyon açılmış olsa bile shadow_tp1_tracker.py bu
    event'i doğru (eski) döngüyle eşleştirebilsin diye."""
    live_pct = orph.get("live_pct")
    if event_name == "SHADOW_ORPHAN_TIMEOUT":
        note = (f"Gerçek pozisyon kapandıktan {SHADOW_ORPHAN_TIMEOUT_H:.0f} saat sonra sanal hâlâ "
                f"kendi çıkışına ulaşmadı, izleme bırakıldı.")
    else:
        note = (f"Gerçek kapandıktan sonra sanal ayrıca izlendi ve sonuçlandı: "
                f"sanal {shadow_pct:+.2f}%, gerçek {live_pct:+.2f}% — fark {fark_pct:+.2f} puan.")
    return {
        "event": event_name, "symbol": orph.get("symbol"),
        "orphan_id": orph.get("orphan_id"), "open_time": orph.get("open_time"),
        "closed_time": orph.get("closed_time"),
        "real_status": f"closed:{orph.get('live_reason')}", "shadow_status": shadow_status,
        "real_tp1": orph.get("real_tp1"), "shadow_tp1": orph.get("shadow_tp1"),
        "shadow_peak": orph.get("shadow_peak"), "shadow_trail": shadow_trail,
        "shadow_pct": shadow_pct, "note": note,
        "live_reason": orph.get("live_reason"), "live_price": orph.get("live_price"), "live_pct": live_pct,
        "fark_pct": fark_pct,
        "shadow_valid": True, "shadow_origin": orph.get("shadow_origin", "fill_time"),
    }


def _orphan_evaluate(orph: dict, high: float, low: float):
    """orph'u YERİNDE günceller (shadow_peak/shadow_trailing) — _shadow_evaluate
    ile birebir aynı peak/trail formülünü kullanır; tek fark, gerçek pozisyon
    zaten kapandığı için stop/TP1/expire tarafı hiç yok, sadece sanalın kendi
    kaderi takip ediliyor. HİÇBİR I/O yapmaz. Dönüş: (dirty, event_or_None) —
    event SADECE sanal kendi trail seviyesini KIRDIĞINDA dolu döner."""
    entry_px = float(orph.get("entry", 0) or 0)
    shadow_tp1 = float(orph.get("shadow_tp1", 0) or 0)
    old_peak = float(orph.get("shadow_peak", entry_px) or entry_px)
    peak = max(old_peak, high)
    dirty = peak > old_peak
    if dirty:
        orph["shadow_peak"] = peak

    if not orph.get("shadow_trailing"):
        if high >= shadow_tp1:
            orph["shadow_trailing"] = True
            dirty = True
        return dirty, None

    trail = _trail_stop_price(peak, orph.get("atr"), entry_px)
    if low <= trail:
        shadow_pct = round((trail - entry_px) / entry_px * 100, 2) if entry_px else 0
        live_pct = float(orph.get("live_pct", 0) or 0)
        fark_pct = round(shadow_pct - live_pct, 2)
        event = _shadow_build_orphan_event(
            orph, "SHADOW_ORPHAN_RESOLVED", "orphan_resolved",
            shadow_pct=shadow_pct, shadow_trail=trail, fark_pct=fark_pct)
        return dirty, event
    return dirty, None


def _close_position_in_state(symbol: str, pos_snap: dict, live_reason: str,
                              live_price: float, live_pct: float):
    """4 kapanış noktasının (tick, tick'siz expire, Binance'te SL/trail fill)
    HEPSİNDE ortak kullanılır: pozisyonu state['positions']'tan siler, shadow
    hâlâ sonuçlanmadıysa (undetermined) AYNI kilit içinde state['shadow_orphans']'a
    kaydeder — tek dosya kaydı, tek kilit. Dönüş: dispatch edilecek close_event
    (I/O YOK burada — _shadow_dispatch çağıranın sorumluluğunda, kilit dışında
    çağrılmalı, mevcut kural aynen korunuyor)."""
    close_event = None
    with _lock:
        s = _load_state()
        s["positions"].pop(symbol, None)
        if pos_snap.get("shadow_tp1") and pos_snap.get("shadow_valid") is True:
            close_event = _shadow_build_close_event(symbol, pos_snap, live_reason, live_price, live_pct)
            if close_event.get("shadow_result") == "undetermined":
                orphan_id = _register_shadow_orphan(s, symbol, pos_snap, live_reason, live_price, live_pct)
                close_event["orphan_id"] = orphan_id
                if orphan_id is None:
                    close_event["note"] = (close_event.get("note", "") +
                        " Orphan izleme limiti dolu olduğu için sanal ayrıca takip edilemedi.")
        _save_state(s)
    return close_event


def _check_shadow_orphans():
    """Periyodik: gerçek pozisyonu kapanmış ama sanal tarafı hâlâ sonuçlanmamış
    coin'leri (state['shadow_orphans']) SADECE kapanmış 1m mumlarla (REST,
    tick değil) izlemeye devam eder. Hiçbir gerçek emir çağrısı yapmaz,
    state['positions']'a hiç dokunmaz. Sanal kendi trail seviyesini kırınca ya
    da SHADOW_ORPHAN_TIMEOUT_H saat geçince kayıt silinir ve nihai event
    dispatch edilir."""
    state = _load_state()
    orphans = dict(state.get("shadow_orphans", {}))
    if not orphans:
        return
    now = datetime.now(timezone.utc)

    for oid, orph in orphans.items():
        try:
            if _orphan_is_timed_out(orph, now):
                _shadow_dispatch(_shadow_build_orphan_event(orph, "SHADOW_ORPHAN_TIMEOUT", "orphan_timeout"))
                with _lock:
                    s = _load_state()
                    s.get("shadow_orphans", {}).pop(oid, None)
                    _save_state(s)
                continue

            try:
                klines = _get_client().get_klines(symbol=orph["symbol"], interval="1m", limit=2)
            except Exception as e:
                print(f"[SHADOW-ORPHAN] fiyat çekme hatası {orph.get('symbol')}: {e}", flush=True)
                continue
            if not klines:
                continue
            k = klines[-1]
            try:
                high, low = float(k[2]), float(k[3])
            except Exception:
                continue

            dirty, event = _orphan_evaluate(orph, high, low)
            if event:
                _shadow_dispatch(event)
                with _lock:
                    s = _load_state()
                    s.get("shadow_orphans", {}).pop(oid, None)
                    _save_state(s)
            elif dirty:
                with _lock:
                    s = _load_state()
                    if oid in s.get("shadow_orphans", {}):
                        s["shadow_orphans"][oid]["shadow_peak"] = orph["shadow_peak"]
                        s["shadow_orphans"][oid]["shadow_trailing"] = orph["shadow_trailing"]
                        _save_state(s)
        except Exception as e:
            print(f"[SHADOW-ORPHAN] genel hata {oid}: {e}", flush=True)
            continue


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
    shadow_events      = []   # lock dışında dispatch edilecek (bkz. aşağıdaki "Lock dışı işlemler")

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

        # Shadow/dry-run: gerçek sistemin trailing/stop/TP1 kararından TAMAMEN
        # bağımsız, kendi (daha düşük) TP1 tavanına göre değerlendirir. Sadece
        # gözlem — burada HİÇBİR I/O yapılmaz (dosya/network), sadece pos
        # mutasyonu + event üretimi. Gerçek dispatch lock dışında olur.
        shadow_dirty, shadow_events = _shadow_evaluate(symbol, pos, high, low)
        if shadow_dirty:
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

        elif pos.get("trail_native"):
            # Native Binance trailing (trailingDelta) — sunucu tarafında otomatik
            # yönetiliyor. Emri iptal edip yeniden koymuyoruz, sadece peak'i
            # görüntüleme için takip ediyoruz. Gerçek tetiklenme periyodik
            # _is_sl_filled reconciliation ile yakalanır.
            if high > float(pos["peak"]):
                pos["peak"] = high
                state["positions"][symbol] = pos
                _save_state(state)

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
            trail_stop = _trail_stop_price(float(pos["peak"]), pos.get("atr"), float(pos.get("entry", 0)))
            if low <= trail_stop:
                cancel_trail_sl_id = pos.get("trailing_sl_id")
                sell_reason = "trail_stop"
                close_price = trail_stop
                pos["closing"] = True
                state["positions"][symbol] = pos
                _save_state(state)

    # ── Lock dışı işlemler (Binance API çağrıları) ───────────────────────────
    for _ev in shadow_events:
        _shadow_dispatch(_ev)

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

        new_trail_id = None
        is_native = False
        if place_trail_sl:
            # TP1 ilk vurulduğunda önce Binance'in kendi (sunucu taraflı) trailing
            # emrini dene — başarılı olursa bot çökse/tick kaybetse bile emir
            # kendi kendine güncellenmeye devam eder.
            new_trail_id = _place_trailing_delta_order(symbol, trail_sl_peak, trail_sl_qty, atr_val)
            is_native = new_trail_id is not None
        if new_trail_id is None:
            new_trail_id = _place_trail_sl_order(symbol, trail_sl_peak, trail_sl_qty, atr_val, float(pos_snap.get("entry", 0)))

        if new_trail_id:
            with _lock:
                s = _load_state()
                if symbol in s["positions"]:
                    s["positions"][symbol]["trailing_sl_id"] = new_trail_id
                    s["positions"][symbol]["atr"] = atr_val
                    s["positions"][symbol]["atr_updated_at"] = datetime.now(timezone.utc).isoformat()
                    if place_trail_sl:
                        s["positions"][symbol]["trail_native"] = is_native
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
        filled_qty_total   = float(pos_snap.get("sell_filled_qty", 0) or 0)
        filled_quote_total = float(pos_snap.get("sell_filled_quote", 0) or 0)
        try:
            entry = float(pos_snap.get("entry", 0) or 0)
            qty   = _round_qty(float(pos_snap.get("qty", 0) or 0), symbol)
            closed, exec_qty, exec_quote = _market_sell(symbol, qty, sell_reason)
            filled_qty_total   += exec_qty
            filled_quote_total += exec_quote
            if closed:
                # Gerçek ortalama satış fiyatı (birden fazla kısmi dolumun ağırlıklı
                # ortalaması) — hiç gerçek dolum yakalanamadıysa (simülasyon, ya da
                # zaten sıfır bakiye/açık emir yok yolu) tetikleyici hedef fiyata düş.
                if filled_qty_total > 0 and filled_quote_total > 0:
                    real_price = filled_quote_total / filled_qty_total
                else:
                    real_price = close_price
                pct = round((real_price - entry) / entry * 100, 2) if entry and real_price else 0
                close_event = _close_position_in_state(symbol, pos_snap, sell_reason, real_price, pct)
                _stop_stream(symbol)
                emoji = "⏰" if sell_reason == "expire" else ("💰" if pct > 0 else "🔴")
                _send_telegram(
                    f"{emoji} <b>POZİSYON KAPANDI — {symbol}</b>\n"
                    f"Sebep: {sell_reason}\nGiriş: {entry:.6g} | Çıkış: ~{real_price:.6g}\nP&L: {pct:+.2f}%"
                )
                _notify_portfolio_with_retry("/api/position-closed", {
                    "symbol": symbol, "reason": sell_reason,
                    "close_price": real_price, "pnl_pct": pct,
                })
                print(f"[MONITOR] Pozisyon kapatıldı: {symbol} | {sell_reason} | {pct:+.2f}%", flush=True)
                if close_event:
                    _shadow_dispatch(close_event)
        finally:
            if not closed:
                # Satış başarısız/kısmi: closing bayrağını kaldır, sonraki tick'te
                # kalan miktar tekrar denenir. Bu ana kadar gerçekten dolan kısmı
                # (varsa) state'e yaz ki kapanışta gerçek ortalama fiyata dahil olsun.
                with _lock:
                    s = _load_state()
                    if symbol in s["positions"]:
                        s["positions"][symbol].pop("closing", None)
                        s["positions"][symbol]["sell_filled_qty"] = filled_qty_total
                        s["positions"][symbol]["sell_filled_quote"] = filled_quote_total
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
            filled_qty_total   = float(pos.get("sell_filled_qty", 0) or 0)
            filled_quote_total = float(pos.get("sell_filled_quote", 0) or 0)
            try:
                # trailing=True olan pozisyonlar üstteki kontrolle zaten atlanıyor —
                # buraya gelen her şey her zaman sabit sl_order_id ile korunuyordur.
                _cancel_sl(sym, pos.get("sl_order_id"))

                entry       = float(pos.get("entry", 0) or 0)
                qty         = _round_qty(float(pos.get("qty", 0) or 0), sym)
                close_price = float(pos.get("current_price") or entry or 0)
                closed, exec_qty, exec_quote = _market_sell(sym, qty, "expire_no_tick")
                filled_qty_total   += exec_qty
                filled_quote_total += exec_quote
                if closed:
                    if filled_qty_total > 0 and filled_quote_total > 0:
                        real_price = filled_quote_total / filled_qty_total
                    else:
                        real_price = close_price
                    pct = round((real_price - entry) / entry * 100, 2) if entry else 0
                    close_event = _close_position_in_state(sym, pos, "expire_no_tick", real_price, pct)
                    _stop_stream(sym)
                    _send_telegram(
                        f"⏰ <b>POZİSYON KAPANDI (tick akışı yoktu) — {sym}</b>\n"
                        f"Sebep: expire_no_tick\nGiriş: {entry:.6g} | Çıkış: ~{real_price:.6g}\nP&L: {pct:+.2f}%"
                    )
                    _notify_portfolio_with_retry("/api/position-closed", {
                        "symbol": sym, "reason": "expire_no_tick",
                        "close_price": real_price, "pnl_pct": pct,
                    })
                    print(f"[MONITOR] Pozisyon kapatıldı (tick'siz expire): {sym} | {pct:+.2f}%", flush=True)
                    if close_event:
                        _shadow_dispatch(close_event)
            finally:
                if not closed:
                    with _lock:
                        s = _load_state()
                        if sym in s["positions"]:
                            s["positions"][sym].pop("closing", None)
                            s["positions"][sym]["sell_filled_qty"] = filled_qty_total
                            s["positions"][sym]["sell_filled_quote"] = filled_quote_total
                            _save_state(s)
                    print(f"[MONITOR] SATIŞ BAŞARISIZ (tick'siz expire): {sym}, sonraki periyodik turda tekrar dener", flush=True)
        except Exception as e:
            print(f"[MONITOR] Tick'siz expire hatası {sym}: {e}", flush=True)
            continue


def _check_price_conditions_no_tick():
    """Stop-hit ve TP1-hit tespiti normalde SADECE _process_tick (websocket tick)
    içinde çalışır. Websocket'in güvenilmez olduğu bugün defalarca kanıtlandı
    (AWE, ZKC, DGB — hepsinde saatlerce/günlerce tek tick gelmedi). Bu fonksiyon,
    websocket'in son CHECK_INTERVAL*2 saniyede tick vermediği pozisyonlar için
    REST üzerinden (1m kline) high/low/close çekip _process_tick'i besliyor —
    aynı stop/TP1/trailing/expire mantığı, tick kaynağı fark etmiyor. Websocket
    artık sadece HIZ kazandırıyor, olmasa da sistem çalışmaya devam ediyor."""
    state = _load_state()
    positions = dict(state.get("positions", {}))
    now = datetime.now(timezone.utc)
    stale_after = CHECK_INTERVAL * 2

    for sym, pos in positions.items():
        try:
            if pos.get("status") != "open" or pos.get("closing"):
                continue
            last_tick_str = pos.get("last_tick_at")
            if last_tick_str:
                try:
                    lt = datetime.fromisoformat(last_tick_str)
                    if lt.tzinfo is None:
                        lt = lt.replace(tzinfo=timezone.utc)
                    if (now - lt).total_seconds() < stale_after:
                        continue  # websocket zaten çalışıyor, tekrar sorgulamaya gerek yok
                except Exception:
                    pass
            print(f"[MONITOR] Tick'siz fiyat kontrolü: {sym} REST'ten sorgulanıyor (websocket sessiz)", flush=True)
            try:
                klines = _get_client().get_klines(symbol=sym, interval="1m", limit=2)
            except Exception as e:
                print(f"[MONITOR] Tick'siz fiyat kontrolü hatası {sym}: {e}", flush=True)
                continue
            if not klines:
                continue
            k = klines[-1]
            try:
                high, low, close = float(k[2]), float(k[3]), float(k[4])
            except Exception:
                continue
            _process_tick(sym, close, high, low)
        except Exception as e:
            # Tek bir sembolün beklenmeyen hatası diğer sembollerin kontrolünü
            # engellemesin — bugün aynı sınıf hatayı SL reconciliation'da bulup
            # düzeltmiştik, burayı unutmuşum. Bu satır olmadan tek bir sembol
            # çökünce TÜM fonksiyon (DGB dahil, sırası ne olursa olsun) o turda
            # sessizce durabiliyordu.
            print(f"[MONITOR] Tick'siz fiyat kontrolü genel hata {sym}: {e}", flush=True)
            continue


def _unstick_closing_flags():
    """Genel güvenlik ağı — belirli bir hata kaynağını avlamak yerine (bu
    sonsuz bir liste; bugün AYNI "closing takıldı" semptomunu 2 farklı kök
    nedenden yaşadık) SEMPTOMUN kendisini periyodik olarak tedavi ediyoruz.
    closing=True, CLOSING_STUCK_S'den (5dk) uzun süredir takılıysa — sebep
    ne olursa olsun, bilinen ya da bilinmeyen — otomatik temizlenir ve
    pozisyon bir sonraki turda normal şekilde tekrar denenir. Elle Shell
    müdahalesi gerekliliğini ortadan kaldırır."""
    state = _load_state()
    positions = dict(state.get("positions", {}))
    now = datetime.now(timezone.utc)
    for sym, pos in positions.items():
        try:
            if not pos.get("closing"):
                continue
            ref_str = pos.get("last_tick_at") or pos.get("open_time")
            if not ref_str:
                continue
            try:
                ref = datetime.fromisoformat(ref_str)
                if ref.tzinfo is None:
                    ref = ref.replace(tzinfo=timezone.utc)
            except Exception:
                continue
            if (now - ref).total_seconds() < CLOSING_STUCK_S:
                continue
            with _lock:
                s = _load_state()
                p = s["positions"].get(sym)
                if p and p.get("closing"):
                    p.pop("closing", None)
                    s["positions"][sym] = p
                    _save_state(s)
            print(f"[MONITOR] closing bayrağı {CLOSING_STUCK_S}s'den uzun süredir takılıydı, otomatik temizlendi: {sym}", flush=True)
            _send_telegram(
                f"⚠️ <b>Otomatik kurtarma — {sym}</b>\n"
                f"closing kilidi takılı kalmıştı, temizlendi, bir sonraki turda tekrar denenecek."
            )
        except Exception as e:
            print(f"[MONITOR] closing kurtarma hatası {sym}: {e}", flush=True)
            continue


def _periodic_check():
    while True:
        time.sleep(CHECK_INTERVAL)
        try:
            _check_monitoring_entries()
            _check_pending_orders()
            _unstick_closing_flags()
            _check_price_conditions_no_tick()
            _check_expired_positions_no_tick()
            _check_shadow_orphans()

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
                        # Retroaktif trail SL — trailing modunda ama Binance emri yok.
                        # trail_native ise önce native trailing dene, yoksa ATR yöntemi.
                        qty = float(pos.get("qty", 0))
                        peak = float(pos.get("peak", 0))
                        if qty > 0 and peak > 0:
                            atr_val = pos.get("atr") or _compute_atr(sym)
                            new_id = None
                            is_native = False
                            if pos.get("trail_native"):
                                new_id = _place_trailing_delta_order(sym, peak, qty, atr_val)
                                is_native = new_id is not None
                            entry_val = float(pos.get("entry", 0))
                            if new_id is None:
                                new_id = _place_trail_sl_order(sym, peak, qty, atr_val, entry_val)
                            if new_id:
                                with _lock:
                                    s = _load_state()
                                    if sym in s["positions"]:
                                        s["positions"][sym]["trailing_sl_id"] = new_id
                                        s["positions"][sym]["atr"] = atr_val
                                        s["positions"][sym]["atr_updated_at"] = datetime.now(timezone.utc).isoformat()
                                        s["positions"][sym]["trail_native"] = is_native
                                        _save_state(s)
                                _send_telegram(
                                    f"🛡 <b>Retroaktif Trail SL — {sym}</b>\n"
                                    f"Peak: {peak:.6g} | Trail stop: {_trail_stop_price(peak, atr_val, entry_val):.6g}"
                                )
                    elif is_trailing and trailing_sl_id and pos.get("trail_native") and _atr_is_stale(pos):
                        # Native trailing'in mesafesi kuruluşta sabitleniyor, backtestteki
                        # gibi bar-bar yenilenmiyor — ATR bayatladıysa iptal edip güncel
                        # ATR ile (mümkünse yine native) yeniden kuruyoruz.
                        qty  = float(pos.get("qty", 0))
                        peak = float(pos.get("peak", 0))
                        fresh_atr = _compute_atr(sym)
                        if fresh_atr and qty > 0 and peak > 0:
                            new_id = _place_trailing_delta_order(sym, peak, qty, fresh_atr)
                            is_native = new_id is not None
                            if new_id is None:
                                new_id = _place_trail_sl_order(sym, peak, qty, fresh_atr, float(pos.get("entry", 0)))
                            if new_id:
                                _cancel_sl(sym, trailing_sl_id)
                                with _lock:
                                    s = _load_state()
                                    if sym in s["positions"]:
                                        s["positions"][sym]["trailing_sl_id"] = new_id
                                        s["positions"][sym]["atr"] = fresh_atr
                                        s["positions"][sym]["atr_updated_at"] = datetime.now(timezone.utc).isoformat()
                                        s["positions"][sym]["trail_native"] = is_native
                                        _save_state(s)
                                print(f"[MONITOR] Native trailing yenilendi: {sym} atr={fresh_atr:.6g} native={is_native}", flush=True)
                    elif is_trailing and trailing_sl_id and pos.get("trail_native"):
                        pass  # ATR henüz bayatlamadı — dokunma
                    elif is_trailing and trailing_sl_id and _atr_is_stale(pos):
                        # ATR bayatladı (peak uzun süredir yükselmedi) — yenile, trail SL emrini güncelle
                        qty = float(pos.get("qty", 0))
                        peak = float(pos.get("peak", 0))
                        fresh_atr = _compute_atr(sym)
                        if fresh_atr and qty > 0 and peak > 0:
                            new_id = _place_trail_sl_order(sym, peak, qty, fresh_atr, float(pos.get("entry", 0)))
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
                        entry = float(pos.get("entry", 0))
                        sl    = float(pos.get("stop", 0))
                        pct   = round((sl - entry) / entry * 100, 2) if entry else 0
                        close_event = _close_position_in_state(sym, pos, "sl_binance", sl, pct)
                        _stop_stream(sym)
                        _send_telegram(
                            f"🔴 <b>SL TETİKLENDİ (Binance) — {sym}</b>\n"
                            f"Giriş: {entry:.6g} | Stop: {sl:.6g} | P&L: {pct:+.2f}%"
                        )
                        _notify_portfolio_with_retry("/api/position-closed", {
                            "symbol": sym, "reason": "sl_binance",
                            "close_price": sl, "pnl_pct": pct,
                        })
                        print(f"[MONITOR] SL doldu (Binance): {sym} | {pct:+.2f}%", flush=True)
                        if close_event:
                            _shadow_dispatch(close_event)

                    # Trail SL fill kontrolü — bot çöküp Binance trailing SL tetiklendiyse
                    elif trailing_sl_id and _is_sl_filled(sym, trailing_sl_id):
                        entry = float(pos.get("entry", 0))
                        peak  = float(pos.get("peak", 0))
                        cl_price = round(_trail_stop_price(peak, pos.get("atr"), entry), 8)
                        pct   = round((cl_price - entry) / entry * 100, 2) if entry else 0
                        close_event = _close_position_in_state(sym, pos, "trail_binance", cl_price, pct)
                        _stop_stream(sym)
                        _send_telegram(
                            f"🟡 <b>TRAIL SL TETİKLENDİ (Binance) — {sym}</b>\n"
                            f"Giriş: {entry:.6g} | Trail stop: {cl_price:.6g} | P&L: {pct:+.2f}%"
                        )
                        _notify_portfolio_with_retry("/api/position-closed", {
                            "symbol": sym, "reason": "trail_binance",
                            "close_price": cl_price, "pnl_pct": pct,
                        })
                        print(f"[MONITOR] Trail SL doldu (Binance): {sym} | {pct:+.2f}%", flush=True)
                        if close_event:
                            _shadow_dispatch(close_event)

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
