# -*- coding: utf-8 -*-
"""
Kripto Haber İzleme Modülü
==========================
RSS kaynaklarından haber çeker, Claude ile Türkçe özetler,
Telegram News sohbetine (thread 64) gönderir.

Scheduled (TR): 09:00 / 12:00 / 15:00 / 19:00 / 23:00  — her haber ayrı mesaj
Breaking:       saatte bir kontrol — gerçekten kritikse tek alert
"""

import os
import re
import json
import threading
import time
import calendar
import hashlib
import requests
import feedparser
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor

ANTHROPIC_API_KEY       = os.getenv("ANTHROPIC_API_KEY",       "")
ANALYZER_TELEGRAM_TOKEN = os.getenv("ANALYZER_TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID        = os.getenv("ANALYZER_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID", "")
NEWS_THREAD_ID          = 64

TR_TZ = timezone(timedelta(hours=3))

RSS_FEEDS = [
    ("CoinDesk",     "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("CoinTelegraph","https://cointelegraph.com/rss"),
    ("The Block",    "https://www.theblock.co/rss.xml"),
    ("Decrypt",      "https://decrypt.co/feed"),
    ("Bitcoinist",   "https://bitcoinist.com/feed/"),
    ("CryptoSlate",  "https://cryptoslate.com/feed/"),
    ("Blockworks",   "https://blockworks.co/feed/"),
    ("Google News",  "https://news.google.com/rss/search?q=bitcoin+cryptocurrency+crypto+blackrock+jpmorgan+%22federal+reserve%22&hl=en-US&gl=US&ceid=US:en"),
]

KEYWORDS = [
    "bitcoin", "btc", "crypto", "cryptocurrency",
    "blackrock", "jpmorgan", "j.p. morgan",
    "federal reserve", " fed ", "fed's", "fed rate",
    "sec ", "etf", "altcoin", "ethereum",
    "binance", "coinbase", "regulation", "stablecoin",
    "trump", "tariff", "s&p", "nasdaq",
    "treasury", "inflation", "interest rate",
]

BREAK_KEYWORDS = [
    # Güvenlik / çöküş
    "ban", "banned", "bans", "hack", "hacked", "breach", "exploit",
    "crash", "collapse", "bankrupt", "insolvent", "seized", "arrest",
    "charges", "sues", "indicted", "doj", "emergency",
    # Makro
    "rate cut", "rate hike", "rate increase", "rate decrease",
    "etf approved", "etf rejected", "etf denied",
    "all-time high", "record high", "ath",
    "liquidated", "halted", "suspended",
    "executive order", "trump signs", "sanction",
    "war", "default", "crisis",
    # Kurumsal BTC hareketleri
    "microstrategy", "strategy buys", "strategy sells",
    "buys bitcoin", "sells bitcoin", "buys btc", "sells btc",
    "purchases bitcoin", "acquires bitcoin",
    "blackrock buys", "blackrock sells", "fidelity buys",
    "$100 million", "$200 million", "$500 million", "$1 billion", "$2 billion",
]

SCHEDULE_HOURS_TR = {9, 19}

_STOP_WORDS = {
    "the", "and", "for", "with", "that", "from", "this", "has", "are",
    "was", "will", "have", "been", "its", "also", "but", "not", "after",
    "before", "about", "their", "they", "more", "over", "into", "than",
    "then", "some", "when", "what", "says", "said", "amid", "first",
    "time", "since", "just", "year", "could", "would", "week", "month",
    "report", "data", "shows", "news", "update",
}

_CACHE_TTL    = 48 * 3600   # 48 saat — bu süreden eski girişler silinir
_state = {
    "last_run_key":   None,
    "last_break_ts":  0,
    "sent_hashes":       {},   # hash → timestamp (float)
    "sent_fingerprints": [],   # list[{"words": list, "ts": float}]
}
_lock = threading.Lock()
_DATA_DIR        = os.getenv("DATA_DIR", "/tmp")
_SENT_CACHE_FILE = os.path.join(_DATA_DIR, "news_sent_cache.json")


def _prune_sent_cache():
    """48 saatten eski girişleri temizle — lock dışından çağrılmalı."""
    cutoff = time.time() - _CACHE_TTL
    with _lock:
        _state["sent_hashes"]       = {k: v for k, v in _state["sent_hashes"].items() if v > cutoff}
        _state["sent_fingerprints"] = [e for e in _state["sent_fingerprints"] if e.get("ts", 0) > cutoff]


def _save_sent_cache():
    _prune_sent_cache()
    try:
        with open(_SENT_CACHE_FILE, "w") as f:
            json.dump({
                "hashes": _state["sent_hashes"],
                "fps":    _state["sent_fingerprints"],
            }, f)
    except Exception:
        pass


def _load_sent_cache():
    try:
        if os.path.exists(_SENT_CACHE_FILE):
            with open(_SENT_CACHE_FILE) as f:
                data = json.load(f)
            _state["sent_hashes"]       = {k: float(v) for k, v in data.get("hashes", {}).items()}
            _state["sent_fingerprints"] = data.get("fps", [])
            _prune_sent_cache()
            print(f"[NEWS] Sent cache yüklendi: {len(_state['sent_hashes'])} hash, "
                  f"{len(_state['sent_fingerprints'])} fingerprint", flush=True)
    except Exception as e:
        print(f"[NEWS] Sent cache yüklenemedi: {e}", flush=True)


def _tr_now():
    return datetime.now(timezone.utc).astimezone(TR_TZ)


def _item_hash(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()[:16]


def _title_fp(title: str) -> frozenset:
    words = re.sub(r"[^\w\s]", " ", title.lower()).split()
    return frozenset(w for w in words if len(w) > 3 and w not in _STOP_WORDS)


def _is_topic_duplicate(title: str) -> bool:
    fp = _title_fp(title)
    if len(fp) < 2:
        return False
    with _lock:
        for entry in _state["sent_fingerprints"]:
            sent_fp = frozenset(entry["words"]) if isinstance(entry, dict) else entry
            if len(fp & sent_fp) >= 2:
                return True
    return False


def _parse_pub(entry) -> datetime | None:
    try:
        if hasattr(entry, "published_parsed") and entry.published_parsed:
            return datetime.fromtimestamp(calendar.timegm(entry.published_parsed), tz=timezone.utc)
    except Exception:
        pass
    return None


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text).strip()


def _fetch_rss(source_name: str, url: str, cutoff: datetime, keywords: list) -> list[dict]:
    items = []
    try:
        resp = requests.get(
            url, timeout=12,
            headers={"User-Agent": "Mozilla/5.0 (compatible; NewsBot/1.0)"},
        )
        if resp.status_code != 200:
            print(f"[NEWS] {source_name}: HTTP {resp.status_code}", flush=True)
            return items
        feed = feedparser.parse(resp.content)
        for entry in feed.entries[:25]:
            pub   = _parse_pub(entry)
            title = _strip_html(getattr(entry, "title",   "")).strip()
            link  = getattr(entry, "link", "").strip()
            desc  = _strip_html(getattr(entry, "summary", ""))[:300]

            if pub and pub < cutoff:
                continue
            if not title or not link:
                continue

            title_lower = title.lower()
            if not any(kw in title_lower for kw in keywords):
                continue

            h = _item_hash(link)
            with _lock:
                if h in _state["sent_hashes"]:
                    continue

            if _is_topic_duplicate(title):
                continue

            hours_ago = None
            if pub:
                hours_ago = int((datetime.now(timezone.utc) - pub).total_seconds() / 3600)

            items.append({
                "hash":      h,
                "source":    source_name,
                "title":     title,
                "desc":      desc,
                "link":      link,
                "pub":       pub,
                "hours_ago": hours_ago,
            })
    except Exception as e:
        print(f"[NEWS] {source_name} hatası: {e}", flush=True)
    return items


def _fetch_all(hours_back: int, keywords: list = None) -> list[dict]:
    if keywords is None:
        keywords = KEYWORDS
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)
    raw = []
    with ThreadPoolExecutor(max_workers=5) as ex:
        futures = [ex.submit(_fetch_rss, name, url, cutoff, keywords) for name, url in RSS_FEEDS]
        for f in futures:
            try:
                raw.extend(f.result())
            except Exception:
                pass

    raw.sort(key=lambda x: x.get("hours_ago") if x.get("hours_ago") is not None else 999)

    seen_norm = []
    unique = []
    for item in raw:
        norm = re.sub(r"\W+", "", item["title"].lower())[:60]
        if norm not in seen_norm:
            seen_norm.append(norm)
            unique.append(item)

    print(f"[NEWS] RSS'ten {len(unique)} haber alındı", flush=True)
    return unique[:20]


# ============================================================
# SCHEDULED — ayrı mesajlar
# ============================================================

def _summarize_with_claude(items: list[dict]) -> str:
    if not ANTHROPIC_API_KEY or not items:
        return ""
    import anthropic

    items_text = ""
    for i, item in enumerate(items, 1):
        age = f" · {item['hours_ago']}s önce" if item.get("hours_ago") is not None else ""
        items_text += f"{i}. [{item['source']}]{age}\n"
        items_text += f"   {item['title']}\n"
        if item.get("desc"):
            items_text += f"   {item['desc'][:200]}\n"
        items_text += "\n"

    prompt = f"""Sen kripto para piyasalarını takip eden bir haber analistisisin.

Aşağıdaki haberleri incele ve Türkçeye çevirerek özetle.
Kaç haber gelirse gelsin hepsini değerlendir — az sayıda (1-3) haber varsa mevcut olanları özetle.
Sayı hakkında yorum yapma, daha fazla haber isteme, bu otomatik bir sistemdir.

Seçim kriterleri (çok sayıda haber varsa öncelik sırası):
- Bitcoin/kripto piyasalarını doğrudan etkileyen haberler
- Kurumsal hareketler (BlackRock, JPMorgan vb.), düzenleyici gelişmeler, makro haberler (Fed, S&P vb.)
- Tekrarlayan veya benzer haberler yerine farklı konular

Her haber için TAM OLARAK bu formatı kullan. Haberler arasına sadece "---" koy, başka hiçbir şey ekleme:

🔸 <b>[Türkçe başlık]</b>
<i>[Kaynak adı]</i>
[2-3 cümle Türkçe özet — kripto piyasasına olası etkisini belirt]
---
🔸 <b>[Türkçe başlık]</b>
...

Son haberden sonra --- koyma.

HABERLER:
{items_text}"""

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1400,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text.strip()
    except Exception as e:
        print(f"[NEWS CLAUDE] {e}", flush=True)
        return ""


def _send_telegram(text: str):
    if not ANALYZER_TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{ANALYZER_TELEGRAM_TOKEN}/sendMessage",
            json={
                "chat_id":                TELEGRAM_CHAT_ID,
                "text":                   text,
                "parse_mode":             "HTML",
                "disable_web_page_preview": True,
                "message_thread_id":      NEWS_THREAD_ID,
            },
            timeout=10,
        )
        if r.status_code != 200:
            print(f"[NEWS TG] {r.status_code}: {r.text[:120]}", flush=True)
    except Exception as e:
        print(f"[NEWS TG] {e}", flush=True)


def _fetch_and_send(hours_back: int):
    try:
        items = _fetch_all(hours_back)
        if not items:
            print(f"[NEWS] Son {hours_back}s için eşleşen haber bulunamadı.", flush=True)
            return

        summary = _summarize_with_claude(items)
        if not summary:
            print("[NEWS] Claude özeti boş döndü.", flush=True)
            return

        tr_time = _tr_now()
        parts = [p.strip() for p in summary.split("---") if p.strip()]
        for i, part in enumerate(parts):
            if i == 0:
                msg = (
                    f"📰 <b>KRİPTO HABER ÖZETİ — {tr_time.strftime('%H:%M')}</b>  "
                    f"🗓 {tr_time.strftime('%d/%m/%Y')}\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"{part}\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"<i>Claude Analyzer · Haber İzleme</i>"
                )
            else:
                msg = (
                    f"{part}\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"<i>Claude Analyzer · Haber İzleme</i>"
                )
            _send_telegram(msg)
            time.sleep(0.8)

        with _lock:
            for item in items:
                _state["sent_hashes"][item["hash"]] = time.time()
                _state["sent_fingerprints"].append({"words": list(_title_fp(item["title"])), "ts": time.time()})
        _save_sent_cache()
        print(f"[NEWS] {len(parts)} haber gönderildi ({tr_time.strftime('%H:%M')})", flush=True)

    except Exception as e:
        print(f"[NEWS FETCH] {e}", flush=True)


# ============================================================
# BREAKING NEWS — saatlik kontrol
# ============================================================

def _breaking_check_claude(items: list[dict]) -> str:
    if not ANTHROPIC_API_KEY or not items:
        return ""
    import anthropic

    items_text = ""
    for i, item in enumerate(items, 1):
        age = f" · {item['hours_ago']}s önce" if item.get("hours_ago") is not None else ""
        items_text += f"{i}. [{item['source']}]{age}\n"
        items_text += f"   {item['title']}\n"
        if item.get("desc"):
            items_text += f"   {item['desc'][:200]}\n"
        items_text += "\n"

    prompt = f"""Sen kripto piyasalarını takip eden bir analistsin.

Aşağıdaki haberleri incele. Bunlar arasında kripto piyasalarını GERÇEKTEN önemli ölçüde etkileyebilecek, anlık dikkat gerektiren bir haber var mı?

Kritik sayılan haberler: büyük borsalarda hack/çöküş, SEC/DOJ büyük davası, ülke yasağı, Fed acil faiz kararı, büyük kurumsal satış/alış hareketi, ETF onay/red, borsa iflası gibi gelişmeler.

Eğer kritik haber YOKSA sadece "YOK" yaz, başka hiçbir şey yazma.

Eğer kritik haber VARSA TAM OLARAK şu formatı kullan:
🔴 <b>[Türkçe başlık]</b>
<i>[Kaynak adı]</i>
[3-4 net Türkçe cümle: ne oldu, neden önemli, kripto piyasasına olası etkisi nasıl olabilir]

HABERLER:
{items_text}"""

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        result = resp.content[0].text.strip()
        if result.upper().startswith("YOK"):
            return ""
        return result
    except Exception as e:
        print(f"[NEWS BREAK CLAUDE] {e}", flush=True)
        return ""


def _check_breaking_news():
    try:
        items = _fetch_all(hours_back=2)
        if not items:
            return

        result = _breaking_check_claude(items)
        if not result:
            return

        tr_time = _tr_now()
        msg = (
            f"⚡ <b>ÖNEMLİ HABER</b>\n"
            f"🕐 {tr_time.strftime('%d/%m/%Y %H:%M')}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"{result}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"<i>Claude Analyzer · Anlık İzleme</i>"
        )
        _send_telegram(msg)

        with _lock:
            for item in items:
                _state["sent_hashes"][item["hash"]] = time.time()
                _state["sent_fingerprints"].append({"words": list(_title_fp(item["title"])), "ts": time.time()})
        _save_sent_cache()
        print(f"[NEWS BREAK] Kritik haber alarmı gönderildi ({tr_time.strftime('%H:%M')})", flush=True)

    except Exception as e:
        print(f"[NEWS BREAK] {e}", flush=True)


# ============================================================
# ANA DÖNGÜ
# ============================================================

def _news_watcher_loop():
    print("[NEWS] Başlatıldı — özet 09/19 TR | breaking 2 saatte bir.", flush=True)
    while True:
        try:
            now_tr = _tr_now()
            now_ts = time.time()
            today  = now_tr.date()

            # 6 saatte bir eski girişleri temizle (48h TTL)
            if now_ts % 21600 < 60:
                _prune_sent_cache()

            # Scheduled haber özeti — 09:00 ve 19:00
            if now_tr.hour in SCHEDULE_HOURS_TR and now_tr.minute < 5:
                run_key = f"{today}_{now_tr.hour}"
                if _state["last_run_key"] != run_key:
                    _state["last_run_key"] = run_key
                    threading.Thread(
                        target=_fetch_and_send, args=(10,),
                        daemon=True, name="news-scheduled"
                    ).start()

            # Breaking news — 2 saatte bir (günde 12 kontrol)
            if now_ts - _state["last_break_ts"] >= 7200:
                _state["last_break_ts"] = now_ts
                threading.Thread(
                    target=_check_breaking_news,
                    daemon=True, name="news-break-check"
                ).start()

        except Exception as e:
            print(f"[NEWS LOOP] {e}", flush=True)

        time.sleep(60)


def start_news_watcher():
    if not ANALYZER_TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[NEWS] ANALYZER_TELEGRAM_TOKEN veya CHAT_ID eksik.", flush=True)
        return
    if not ANTHROPIC_API_KEY:
        print("[NEWS] ANTHROPIC_API_KEY eksik.", flush=True)
        return
    _load_sent_cache()
    t = threading.Thread(target=_news_watcher_loop, daemon=True, name="news-watcher")
    t.start()
