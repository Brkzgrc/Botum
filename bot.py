import os
import time
import threading
import requests
import numpy as np
from datetime import datetime, timezone
from flask import Flask, jsonify, render_template_string
from collections import deque

app = Flask(__name__)

# ── Environment ───────────────────────────────────────────────────────────────
ACCOUNT_SIZE       = float(os.getenv("ACCOUNT_SIZE",       "5000"))
BINANCE_API_KEY    = os.getenv("BINANCE_API_KEY",          "")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET",       "")
RISK_PERCENT       = float(os.getenv("RISK_PERCENT",       "2"))
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID",         "")
TELEGRAM_TOKEN     = os.getenv("TELEGRAM_TOKEN",           "")

INTERVAL     = os.getenv("INTERVAL",   "1h")
SCAN_EVERY   = int(os.getenv("SCAN_EVERY", "900"))
MAX_SYMBOLS  = int(os.getenv("MAX_SYMBOLS", "0"))   # 0 = tümü
CANDLE_LIMIT = 100

RSI_OVERSOLD      = float(os.getenv("RSI_OVERSOLD",      "32"))
WILLIAMS_OVERSOLD = float(os.getenv("WILLIAMS_OVERSOLD", "-80"))
DROP_PCT          = float(os.getenv("DROP_PCT",          "4"))
STOCHRSI_THRESH   = float(os.getenv("STOCHRSI_THRESH",  "25"))
MIN_SCORE         = int(os.getenv("MIN_SCORE",           "4"))

IGNORED_COINS = {
    'UPUSDT','DOWNUSDT','BEARUSDT','BULLUSDT',
    'USDCUSDT','TUSDUSDT','FDUSDUSDT','DAIUSDT','USDPUSDT',
    'EURUSDT','TRYUSDT','GBPUSDT','BUSDUSTUSDT','USTCUSDT',
    'PAXGUSDT','WBTCUSDT','USDEUSDT','BRLUSDT','RUBUSDT',
    'AUDUSDT','USUSDT','BFUSDUSDT','RLUSDUSDT',
}

# ── State ─────────────────────────────────────────────────────────────────────
signals       = deque(maxlen=200)
last_scan     = {}
scan_log      = deque(maxlen=100)
alerted       = {}
active_symbols = []

# ── Sembol yükleme (Binance'den tüm aktif USDT çiftleri, hacme göre) ─────────
def load_symbols():
    global active_symbols
    try:
        # Tüm sembolleri çek
        r = requests.get("https://api.binance.com/api/v3/exchangeInfo", timeout=15)
        r.raise_for_status()
        all_syms = [
            s["symbol"] for s in r.json()["symbols"]
            if s["symbol"].endswith("USDT")
            and s["status"] == "TRADING"
            and s["symbol"] not in IGNORED_COINS
        ]

        # Hacim verisi çek (24h ticker)
        t = requests.get("https://api.binance.com/api/v3/ticker/24hr", timeout=20)
        t.raise_for_status()
        volumes = {
            item["symbol"]: float(item.get("quoteVolume", 0) or 0)
            for item in t.json()
        }

        # Hacme göre sırala
        sorted_syms = sorted(all_syms, key=lambda x: volumes.get(x, 0), reverse=True)

        if MAX_SYMBOLS and MAX_SYMBOLS > 0:
            active_symbols = sorted_syms[:MAX_SYMBOLS]
        else:
            active_symbols = sorted_syms

        ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        scan_log.appendleft(f"[{ts}] ✓ {len(active_symbols)} sembol yüklendi (hacme göre sıralı)")

    except Exception as e:
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        scan_log.appendleft(f"[{ts}] ❌ Sembol yükleme hatası: {e}")
        # Hata olursa temel coinlerle devam et
        active_symbols = ["SOLUSDT","BTCUSDT","ETHUSDT","BNBUSDT","XRPUSDT"]

# ── Risk hesabı ───────────────────────────────────────────────────────────────
def calc_position(entry_price: float, stop_pct: float = 3.0) -> dict:
    risk_usd     = ACCOUNT_SIZE * (RISK_PERCENT / 100)
    stop_dist    = entry_price * (stop_pct / 100)
    qty          = risk_usd / stop_dist
    position_usd = qty * entry_price
    return {
        "risk_usd":     round(risk_usd, 2),
        "stop_loss":    round(entry_price - stop_dist, 4),
        "stop_pct":     stop_pct,
        "qty":          round(qty, 6),
        "position_usd": round(position_usd, 2),
    }

# ── Telegram ──────────────────────────────────────────────────────────────────
def send_telegram(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={
                "chat_id":                  TELEGRAM_CHAT_ID,
                "text":                     text,
                "parse_mode":               "HTML",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
    except Exception as e:
        scan_log.appendleft(f"[TG HATA] {e}")

def build_tg_message(r: dict) -> str:
    conds = r.get("conditions", {})
    label_map = {
        "drop_candle":       "📉 Sert Düşüş",
        "rsi_oversold":      "RSI Oversold",
        "williams_oversold": "Williams %R Oversold",
        "macd_neg":          "MACD Negatif",
        "stochrsi_low":      "StochRSI Düşük",
    }
    met  = [v for k, v in label_map.items() if conds.get(k)]
    miss = [v for k, v in label_map.items() if not conds.get(k)]
    score = r.get("score", 0)
    stars = "⭐" * score + "☆" * (5 - score)
    pos   = r.get("position", {})

    return (
        f"🚨 <b>ALIM SİNYALİ — {r['symbol']}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 Fiyat:       <b>${r.get('price', 0):.4f}</b>\n"
        f"📊 Skor:        {stars}  ({score}/5)\n"
        f"⏱ Periyot:     <b>{INTERVAL}</b>\n\n"
        f"<b>📈 İndikatörler:</b>\n"
        f"  RSI(14)     → <b>{r.get('rsi')}</b>\n"
        f"  Williams %R → <b>{r.get('williams_r')}</b>\n"
        f"  MACD Hist   → <b>{r.get('macd_hist')}</b>\n"
        f"  StochRSI K  → <b>{r.get('stochrsi')}</b>\n"
        f"  Son mum     → <b>%{r.get('drop_pct')} düşüş</b>\n\n"
        f"<b>💼 Pozisyon ({ACCOUNT_SIZE}$ / %{RISK_PERCENT} risk):</b>\n"
        f"  Risk:        <b>${pos.get('risk_usd')} USDT</b>\n"
        f"  Stop Loss:   <b>${pos.get('stop_loss')}</b>  (%{pos.get('stop_pct')} alt)\n"
        f"  Miktar:      <b>{pos.get('qty')} adet</b>\n"
        f"  Pos. Değer:  <b>${pos.get('position_usd')}</b>\n\n"
        f"✅ {', '.join(met) if met else '—'}\n"
        f"❌ {', '.join(miss) if miss else '—'}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🕐 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
    )

# ── İndikatörler ──────────────────────────────────────────────────────────────
def calc_rsi(closes, period=14):
    deltas = np.diff(closes)
    gains  = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    ag = np.mean(gains[:period])
    al = np.mean(losses[:period])
    for i in range(period, len(gains)):
        ag = (ag * (period - 1) + gains[i]) / period
        al = (al * (period - 1) + losses[i]) / period
    rs = ag / al if al != 0 else np.inf
    return round(100 - 100 / (1 + rs), 2)

def calc_ema(arr, period):
    k = 2 / (period + 1)
    out = [arr[0]]
    for v in arr[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return np.array(out)

def calc_macd(closes):
    ml   = calc_ema(closes, 12) - calc_ema(closes, 26)
    sig  = calc_ema(ml, 9)
    hist = ml - sig
    return round(ml[-1], 6), round(sig[-1], 6), round(hist[-1], 6)

def calc_williams_r(highs, lows, closes, period=14):
    h = np.max(highs[-period:])
    l = np.min(lows[-period:])
    if h == l:
        return -50.0
    return round(-100 * (h - closes[-1]) / (h - l), 2)

def calc_stochrsi(closes, rsi_period=14, stoch_period=14):
    rsi_series = np.array([calc_rsi(closes[:i], rsi_period) for i in range(rsi_period + 1, len(closes) + 1)])
    if len(rsi_series) < stoch_period:
        return 50.0
    rh = np.max(rsi_series[-stoch_period:])
    rl = np.min(rsi_series[-stoch_period:])
    if rh == rl:
        return 50.0
    return round(100 * (rsi_series[-1] - rl) / (rh - rl), 2)

# ── Binance kline ─────────────────────────────────────────────────────────────
def fetch_candles(symbol):
    headers = {"X-MBX-APIKEY": BINANCE_API_KEY} if BINANCE_API_KEY else {}
    r = requests.get(
        "https://api.binance.com/api/v3/klines",
        params={"symbol": symbol, "interval": INTERVAL, "limit": CANDLE_LIMIT},
        headers=headers,
        timeout=10,
    )
    r.raise_for_status()
    data = r.json()
    return (
        np.array([float(c[1]) for c in data]),
        np.array([float(c[2]) for c in data]),
        np.array([float(c[3]) for c in data]),
        np.array([float(c[4]) for c in data]),
        [int(c[0]) for c in data],
    )

# ── Tek sembol tarama ─────────────────────────────────────────────────────────
def scan_symbol(symbol: str) -> dict:
    try:
        opens, highs, lows, closes, times = fetch_candles(symbol)

        drop_pct = round((opens[-2] - closes[-2]) / opens[-2] * 100, 2)

        c = closes[:-1]
        h = highs[:-1]
        l = lows[:-1]

        rsi_val      = calc_rsi(c)
        _, _, hist   = calc_macd(c)
        wr_val       = calc_williams_r(h, l, c)
        srsi_val     = calc_stochrsi(c)
        ma20         = round(float(np.mean(c[-20:])), 4)
        ma50         = round(float(np.mean(c[-50:])), 4)
        recovery     = round((closes[-1] - closes[-2]) / closes[-2] * 100, 2)

        conditions = {
            "drop_candle":       drop_pct  >= DROP_PCT,
            "rsi_oversold":      rsi_val   <= RSI_OVERSOLD,
            "williams_oversold": wr_val    <= WILLIAMS_OVERSOLD,
            "macd_neg":          hist      <  0,
            "stochrsi_low":      srsi_val  <= STOCHRSI_THRESH,
        }
        score     = sum(conditions.values())
        is_signal = score >= MIN_SCORE
        entry     = float(closes[-1])

        def safe(v):
            if isinstance(v, float) and (v != v or v == float("inf") or v == float("-inf")):
                return None
            if hasattr(v, "item"):
                return v.item()
            return v

        return {
            "symbol":      symbol,
            "time":        datetime.now(timezone.utc).isoformat(),
            "candle_time": datetime.fromtimestamp(times[-2] / 1000, tz=timezone.utc).isoformat(),
            "price":       safe(round(entry, 6)),
            "drop_pct":    safe(drop_pct),
            "rsi":         safe(rsi_val),
            "macd_hist":   safe(hist),
            "williams_r":  safe(wr_val),
            "stochrsi":    safe(srsi_val),
            "ma20":        safe(ma20),
            "ma50":        safe(ma50),
            "recovery":    safe(recovery),
            "conditions":  conditions,
            "score":       int(score),
            "signal":      bool(is_signal),
            "position":    calc_position(entry) if is_signal else {},
        }

    except Exception as e:
        return {"symbol": symbol, "error": str(e), "signal": False, "score": 0,
                "time": datetime.now(timezone.utc).isoformat()}

# ── Tarama döngüsü ────────────────────────────────────────────────────────────
def scanner_loop():
    while True:
        if not active_symbols:
            time.sleep(5)
            continue

        ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        scan_log.appendleft(f"[{ts}] 🔍 {len(active_symbols)} sembol taranıyor...")

        for sym in active_symbols:
            result = scan_symbol(sym)
            last_scan[sym] = result

            if result.get("signal"):
                signals.appendleft(result)
                scan_log.appendleft(
                    f"[{ts}] 🚨 SİNYAL → {sym} | "
                    f"Skor:{result['score']}/5 | RSI:{result.get('rsi')} | Düşüş:%{result.get('drop_pct')}"
                )
                candle_key = result.get("candle_time", "")
                if alerted.get(sym) != candle_key:
                    alerted[sym] = candle_key
                    send_telegram(build_tg_message(result))
                    scan_log.appendleft(f"[{ts}] 📨 Telegram → {sym}")

            time.sleep(0.1)  # rate limit koruması

        # Her turda sembolleri yenile (yeni listelemeler vs)
        load_symbols()
        scan_log.appendleft(f"[{ts}] ✓ Tur bitti. Sonraki: {SCAN_EVERY}s")
        time.sleep(SCAN_EVERY)

# ── Flask routes ──────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template_string(DASHBOARD_HTML)

@app.route("/api/status")
def api_status():
    def clean(obj):
        if isinstance(obj, dict):
            return {k: clean(v) for k, v in obj.items()}
        if isinstance(obj, (list, deque)):
            return [clean(i) for i in obj]
        if isinstance(obj, float):
            if obj != obj or obj == float("inf") or obj == float("-inf"):
                return None
            return round(obj, 6)
        if hasattr(obj, "item"):  # numpy scalar
            return clean(obj.item())
        return obj

    try:
        return jsonify(clean({
            "total_symbols": len(active_symbols),
            "interval":      INTERVAL,
            "scan_every":    SCAN_EVERY,
            "account":       ACCOUNT_SIZE,
            "risk_pct":      RISK_PERCENT,
            "last_scan":     dict(last_scan),
            "signals":       list(signals)[:30],
            "log":           list(scan_log)[:30],
        }))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/health")
def health():
    return jsonify({"status": "ok"})

# ── Dashboard ─────────────────────────────────────────────────────────────────
DASHBOARD_HTML = r"""
<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Oversold Scanner</title>
<link href="https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&family=Bebas+Neue&display=swap" rel="stylesheet">
<style>
:root{--bg:#06090d;--surf:#0c1117;--surf2:#111820;--brd:#1c2a36;--acc:#00d4ff;--grn:#00f080;--red:#ff3a5c;--amb:#ffb300;--txt:#b8cdd8;--mut:#3d5a6a}
*{margin:0;padding:0;box-sizing:border-box}
body{background:var(--bg);color:var(--txt);font-family:'Space Mono',monospace;min-height:100vh;overflow-x:hidden}
body::after{content:'';position:fixed;inset:0;pointer-events:none;z-index:9999;background:repeating-linear-gradient(0deg,transparent 0,transparent 3px,rgba(0,0,0,.07) 3px,rgba(0,0,0,.07) 4px)}
header{height:54px;padding:0 20px;display:flex;align-items:center;gap:14px;background:linear-gradient(90deg,#0c1117,#06090d);border-bottom:1px solid var(--brd);position:sticky;top:0;z-index:100}
.logo{font-family:'Bebas Neue';font-size:1.5rem;color:var(--acc);letter-spacing:4px;text-shadow:0 0 16px rgba(0,212,255,.5)}
.hmeta{font-size:.6rem;color:var(--mut)}
.livepill{margin-left:auto;display:flex;align-items:center;gap:6px;background:rgba(0,240,128,.07);border:1px solid rgba(0,240,128,.2);padding:3px 10px;border-radius:20px}
.livedot{width:6px;height:6px;border-radius:50%;background:var(--grn);box-shadow:0 0 8px var(--grn);animation:blink 1.4s ease infinite}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.2}}
.livetxt{font-size:.6rem;color:var(--grn);letter-spacing:1px}
.layout{display:grid;grid-template-columns:1fr 310px;height:calc(100vh - 54px)}
.pmain{display:flex;flex-direction:column;overflow:hidden}
.toolbar{padding:9px 14px;border-bottom:1px solid var(--brd);display:flex;gap:10px;align-items:center;background:var(--surf)}
.btn{padding:5px 14px;background:transparent;border:1px solid var(--acc);color:var(--acc);font-family:'Bebas Neue';font-size:.85rem;letter-spacing:2px;cursor:pointer;transition:.15s}
.btn:hover{background:var(--acc);color:#000}
.stimer{font-size:.6rem;color:var(--mut);margin-left:auto}
.statsrow{display:flex;gap:1px;border-bottom:1px solid var(--brd)}
.stat{flex:1;padding:7px 10px;background:var(--surf2);text-align:center}
.statv{font-family:'Bebas Neue';font-size:1.05rem;color:var(--acc)}
.statl{font-size:.5rem;color:var(--mut);text-transform:uppercase;letter-spacing:.8px;margin-top:1px}
.cards{flex:1;overflow-y:auto;display:grid;grid-template-columns:repeat(auto-fill,minmax(255px,1fr));gap:1px;padding:1px;background:var(--brd);align-content:start}
.card{background:var(--surf);padding:13px;display:flex;flex-direction:column;gap:7px;position:relative;overflow:hidden}
.card.csig{background:#031409;border-left:3px solid var(--grn)}
.card.csig::before{content:'';position:absolute;inset:0;background:radial-gradient(ellipse at 0 0,rgba(0,240,128,.06),transparent 70%)}
.card.cwrn{border-left:3px solid var(--amb)}
.ch{display:flex;justify-content:space-between;align-items:center}
.sym{font-family:'Bebas Neue';font-size:1.35rem;color:var(--acc);letter-spacing:2px}
.badge{font-family:'Bebas Neue';font-size:.68rem;padding:2px 7px;letter-spacing:1px;border-radius:2px}
.bsig{background:var(--grn);color:#000;box-shadow:0 0 10px rgba(0,240,128,.35)}
.bwrn{background:rgba(255,179,0,.14);color:var(--amb);border:1px solid rgba(255,179,0,.3)}
.bidle{background:var(--brd);color:var(--mut)}
.pricerow{display:flex;justify-content:space-between;align-items:baseline}
.price{font-size:.95rem}.drop{font-size:.68rem}
.cred{color:var(--red)}.cgrn{color:var(--grn)}.camb{color:var(--amb)}
.barwrap{height:3px;background:var(--brd);border-radius:2px;overflow:hidden}
.barfill{height:100%;border-radius:2px;transition:width .5s ease}
.inds{display:grid;grid-template-columns:1fr 1fr;gap:4px}
.ind{background:rgba(255,255,255,.02);padding:4px 6px;border-radius:2px}
.indl{font-size:.52rem;color:var(--mut);text-transform:uppercase;letter-spacing:.7px}
.indv{font-size:.82rem;margin-top:1px}
.posbox{background:rgba(0,212,255,.04);border:1px solid rgba(0,212,255,.12);padding:6px 8px;border-radius:2px;font-size:.62rem}
.posrow{display:flex;justify-content:space-between;padding:1px 0}
.posk{color:var(--mut)}.posv{color:var(--acc)}
.conds{display:flex;gap:3px;flex-wrap:wrap}
.cond{font-size:.58rem;padding:2px 4px;border-radius:2px}
.cmet{background:rgba(0,240,128,.11);color:var(--grn);border:1px solid rgba(0,240,128,.22)}
.cmiss{background:rgba(255,255,255,.02);color:var(--mut);border:1px solid rgba(255,255,255,.05)}
.cerr{color:var(--red);font-size:.62rem;word-break:break-all}
.sidebar{border-left:1px solid var(--brd);display:flex;flex-direction:column;overflow:hidden}
.sbhead{padding:11px 13px;font-family:'Bebas Neue';font-size:.95rem;letter-spacing:2px;color:var(--acc);border-bottom:1px solid var(--brd);display:flex;justify-content:space-between;align-items:center}
.cntb{font-family:'Space Mono';font-size:.6rem;background:var(--red);color:#fff;padding:2px 7px;border-radius:2px}
.siglist{flex:1;overflow-y:auto}
.sigitem{padding:9px 13px;border-bottom:1px solid rgba(255,255,255,.03);animation:slid .25s ease}
@keyframes slid{from{opacity:0;transform:translateX(10px)}to{opacity:1;transform:translateX(0)}}
.sisym{font-family:'Bebas Neue';font-size:1.05rem;color:var(--grn)}
.sit{font-size:.58rem;color:var(--mut)}.sim{font-size:.62rem;margin-top:3px}.sipos{font-size:.58rem;color:var(--acc);margin-top:2px}
.loghead{padding:9px 13px;font-family:'Bebas Neue';font-size:.75rem;letter-spacing:2px;color:var(--mut);border-top:1px solid var(--brd);border-bottom:1px solid var(--brd)}
.log{height:145px;overflow-y:auto;padding:5px 9px}
.logline{font-size:.56rem;color:var(--mut);padding:1px 0;border-bottom:1px solid rgba(255,255,255,.02)}
.logs{color:var(--amb)}.logt{color:var(--acc)}
::-webkit-scrollbar{width:3px}::-webkit-scrollbar-track{background:transparent}::-webkit-scrollbar-thumb{background:var(--brd)}
</style>
</head>
<body>
<header>
  <div class="logo">OVERSOLD SCANNER</div>
  <div class="hmeta" id="hm">yükleniyor...</div>
  <div class="livepill"><div class="livedot"></div><span class="livetxt">LIVE</span></div>
</header>
<div class="layout">
  <div class="pmain">
    <div class="toolbar">
      <button class="btn" onclick="load()">⟳ REFRESH</button>
      <div class="stimer" id="timer">—</div>
    </div>
    <div class="statsrow">
      <div class="stat"><div class="statv" id="ss">—</div><div class="statl">Toplam Sembol</div></div>
      <div class="stat"><div class="statv" id="st">—</div><div class="statl">Taranan</div></div>
      <div class="stat"><div class="statv" id="sg">—</div><div class="statl">Sinyal</div></div>
      <div class="stat"><div class="statv" id="sa">—</div><div class="statl">Hesap $</div></div>
      <div class="stat"><div class="statv" id="si">—</div><div class="statl">Periyot</div></div>
    </div>
    <div class="cards" id="cards">
      <div style="padding:40px;color:var(--mut);grid-column:1/-1;text-align:center">Semboller yükleniyor, ilk tarama başlıyor...</div>
    </div>
  </div>
  <div class="sidebar">
    <div class="sbhead">SİNYALLER <span class="cntb" id="scnt">0</span></div>
    <div class="siglist" id="siglist"><div style="padding:14px;color:var(--mut);font-size:.68rem">Henüz sinyal yok.</div></div>
    <div class="loghead">SCAN LOG</div>
    <div class="log" id="logel"></div>
  </div>
</div>
<script>
const LBL={drop_candle:'DROP',rsi_oversold:'RSI',williams_oversold:'W%R',macd_neg:'MACD',stochrsi_low:'SRSI'};
function ic(k,v){
  if(k==='rsi')return v<=30?'cred':v<=45?'camb':'';
  if(k==='williams_r')return v<=-80?'cred':v<=-60?'camb':'';
  if(k==='macd_hist')return v<0?'cred':'cgrn';
  if(k==='stochrsi')return v<=25?'cred':v<=35?'camb':'';
  return '';
}
function sc(s){return['#3d5a6a','#3d5a6a','#ffb300','#ffb300','#00f080','#00f080'][s]||'#00f080'}
function card(d){
  if(d.error)return`<div class="card"><div class="sym">${d.symbol}</div><div class="cerr">HATA: ${d.error}</div></div>`;
  const sig=d.signal,wrn=!sig&&d.score>=3;
  const cls=sig?'card csig':wrn?'card cwrn':'card';
  const bdg=sig?'<span class="badge bsig">SİNYAL</span>':wrn?'<span class="badge bwrn">YAKLAŞIYOR</span>':'<span class="badge bidle">İZLE</span>';
  const dc=d.drop_pct>0?'cred':'cgrn',ds=d.drop_pct>0?'▼':'▲';
  const rc=d.recovery>0?'cgrn':'cred',rs=d.recovery>0?'+':'';
  const pct=(d.score/5*100)+'%';
  const conds=d.conditions||{},pos=d.position;
  const posHtml=sig&&pos?`<div class="posbox">
    <div class="posrow"><span class="posk">Risk</span><span class="posv">$${pos.risk_usd} USDT</span></div>
    <div class="posrow"><span class="posk">Stop</span><span class="posv">$${pos.stop_loss} (%${pos.stop_pct})</span></div>
    <div class="posrow"><span class="posk">Miktar</span><span class="posv">${pos.qty} adet</span></div>
    <div class="posrow"><span class="posk">Pos. $</span><span class="posv">$${pos.position_usd}</span></div>
  </div>`:'';
  return`<div class="${cls}">
    <div class="ch"><span class="sym">${d.symbol}</span>${bdg}</div>
    <div class="pricerow"><span class="price">$${(d.price||0).toFixed(4)}</span>
    <span class="drop ${dc}">${ds}${Math.abs(d.drop_pct||0).toFixed(2)}% <span class="${rc}">${rs}${d.recovery??0}% geri</span></span></div>
    <div class="barwrap"><div class="barfill" style="width:${pct};background:${sc(d.score)}"></div></div>
    <div class="inds">${[['RSI(14)','rsi'],['Williams%R','williams_r'],['MACD Hist','macd_hist'],['StochRSI','stochrsi'],['MA20','ma20'],['MA50','ma50']].map(([l,k])=>
      `<div class="ind"><div class="indl">${l}</div><div class="indv ${ic(k,d[k])}">${d[k]??'—'}</div></div>`).join('')}</div>
    ${posHtml}
    <div class="conds">${Object.entries(LBL).map(([k,l])=>`<span class="cond ${conds[k]?'cmet':'cmiss'}">${l}</span>`).join('')}</div>
  </div>`;
}
function sigItem(s){
  const t=new Date(s.time).toLocaleTimeString('tr-TR'),pos=s.position;
  return`<div class="sigitem">
    <div style="display:flex;justify-content:space-between"><span class="sisym">${s.symbol}</span><span class="sit">${t}</span></div>
    <div class="sim">$${(s.price||0).toFixed(4)} | RSI ${s.rsi} | WR ${s.williams_r} | ▼${s.drop_pct}%</div>
    ${pos?`<div class="sipos">Risk $${pos.risk_usd} · Stop $${pos.stop_loss} · ${pos.qty} adet</div>`:''}
  </div>`;
}
let scanEvery=900;
async function load(){
  const d=await fetch('/api/status').then(r=>r.json()).catch(()=>null);
  if(!d)return;
  scanEvery=d.scan_every||900;
  document.getElementById('ss').textContent=d.total_symbols||0;
  document.getElementById('st').textContent=Object.keys(d.last_scan||{}).length;
  document.getElementById('sg').textContent=(d.signals||[]).length;
  document.getElementById('sa').textContent='$'+(d.account||0);
  document.getElementById('si').textContent=d.interval||'—';
  document.getElementById('hm').textContent=`Binance tüm USDT çiftleri · her ${scanEvery/60}dk`;
  const cards=Object.values(d.last_scan||{});
  document.getElementById('cards').innerHTML=cards.length
    ?cards.sort((a,b)=>(b.score||0)-(a.score||0)).map(card).join('')
    :'<div style="padding:40px;color:var(--mut);grid-column:1/-1;text-align:center">Tarama devam ediyor...</div>';
  const sigs=d.signals||[];
  document.getElementById('scnt').textContent=sigs.length;
  document.getElementById('siglist').innerHTML=sigs.length
    ?sigs.slice(0,40).map(sigItem).join('')
    :'<div style="padding:14px;color:var(--mut);font-size:.68rem">Henüz sinyal yok.</div>';
  document.getElementById('logel').innerHTML=(d.log||[]).map(l=>
    `<div class="logline ${l.includes('SİNYAL')?'logs':l.includes('📨')?'logt':''}">${l}</div>`).join('');
}
function tick(){
  const rem=scanEvery-(Math.floor(Date.now()/1000)%scanEvery);
  const m=Math.floor(rem/60),s=rem%60;
  document.getElementById('timer').textContent=`Sonraki tarama: ${m}:${s.toString().padStart(2,'0')}`;
}
setInterval(load,30000);setInterval(tick,1000);load();
</script>
</body>
</html>
"""

# ── Başlatma ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    def boot():
        time.sleep(2)
        load_symbols()
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        scan_log.appendleft(f"[{ts}] 🟢 Bot başlatıldı — {INTERVAL} periyot | hesap ${ACCOUNT_SIZE}")

    threading.Thread(target=boot,         daemon=True).start()
    threading.Thread(target=scanner_loop, daemon=True).start()

    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
