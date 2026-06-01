# -*- coding: utf-8 -*-
"""
Kripto Haber İzleme Modülü
==========================
RSS kaynaklarından haber çeker, Claude ile Türkçe özetler,
Telegram News sohbetine (thread 64) gönderir.

Zamanlama (TR saatiyle): 09:00 / 12:00 / 15:00 / 19:00 / 23:00
"""

import os
import re
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
TELEGRAM_CHAT_ID        = os.getenv("TELEGRAM_CHAT_ID",        "")
NEWS_THREAD_ID          = 64

TR_TZ = timezone(timedelta(hours=3))

RSS_FEEDS = [
    ("CoinDesk",     "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("CoinTelegraph","https://cointelegraph.com/rss"),
    ("The Block",    "https://www.theblock.co/rss.xml"),
    ("Decrypt",      "https://decrypt.co/feed"),
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

SCHEDULE_HOURS_TR = {9, 12, 15, 19, 23}

_state = {
    "last_run_key":     None,
    "sent_hashes":      set(),
    "sent_hashes_date": None,
}
_lock = threading.Lock()


def _tr_now():
    return datetime.now(timezone.utc).astimezone(TR_TZ)


def _item_hash(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()[:16]


def _parse_pub(entry) -> datetime | None:
    try:
        if hasattr(entry, "published_parsed") and entry.published_parsed:
            return datetime.fromtimestamp(calendar.timegm(entry.published_parsed), tz=timezone.utc)
    except Exception:
        pass
    return None


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text).strip()


def _fetch_rss(source_name: str, url: str, cutoff: datetime) -> list[dict]:
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
            if not any(kw in title_lower for kw in KEYWORDS):
                continue

            h = _item_hash(link)
            with _lock:
                if h in _state["sent_hashes"]:
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


def _fetch_all(hours_back: int) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)
    raw = []
    with ThreadPoolExecutor(max_workers=5) as ex:
        futures = [ex.submit(_fetch_rss, name, url, cutoff) for name, url in RSS_FEEDS]
        for f in futures:
            try:
                raw.extend(f.result())
            except Exception:
                pass

    # Sort: yeni → eski
    raw.sort(key=lambda x: x.get("hours_ago") if x.get("hours_ago") is not None else 999)

    # Başlık benzerliği ile tekrar temizle
    seen_norm = []
    unique = []
    for item in raw:
        norm = re.sub(r"\W+", "", item["title"].lower())[:60]
        if norm not in seen_norm:
            seen_norm.append(norm)
            unique.append(item)

    return unique[:20]


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

Aşağıdaki haberleri incele. En önemli 4-5 haberi seç ve Türkçeye çevirerek özetle.

Seçim kriterleri:
- Bitcoin/kripto piyasalarını doğrudan etkileyen haberler öncelikli
- Kurumsal hareketler (BlackRock, JPMorgan vb.), düzenleyici gelişmeler, makro ekonomik haberler (Fed, S&P vb.)
- Tekrarlayan veya benzer haberler yerine farklı konular seç

Her haber için TAM OLARAK bu formatı kullan, fazladan açıklama ekleme:
🔸 <b>[Türkçe başlık]</b>
<i>[Kaynak adı]</i>
[2-3 cümle Türkçe özet — kripto piyasasına olası etkisini belirt]

HABERLER:
{items_text}"""

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=900,
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
        msg = (
            f"📰 <b>KRİPTO HABER ÖZETİ — {tr_time.strftime('%H:%M')}</b>\n"
            f"🗓 {tr_time.strftime('%d/%m/%Y')}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"{summary}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"<i>Claude Analyzer · Haber İzleme</i>"
        )
        _send_telegram(msg)

        with _lock:
            for item in items:
                _state["sent_hashes"].add(item["hash"])
        print(f"[NEWS] {len(items)} haberden özet gönderildi ({tr_time.strftime('%H:%M')})", flush=True)

    except Exception as e:
        print(f"[NEWS FETCH] {e}", flush=True)


def _news_watcher_loop():
    print("[NEWS] Başlatıldı — 09:00/12:00/15:00/19:00/23:00 TR saatlerinde çalışır.", flush=True)
    while True:
        try:
            now_tr = _tr_now()
            today  = now_tr.date()

            # Gece geçişinde sent_hashes temizle
            with _lock:
                if _state["sent_hashes_date"] != today:
                    _state["sent_hashes_date"] = today
                    _state["sent_hashes"].clear()

            if now_tr.hour in SCHEDULE_HOURS_TR and now_tr.minute < 5:
                run_key = f"{today}_{now_tr.hour}"
                if _state["last_run_key"] != run_key:
                    _state["last_run_key"] = run_key
                    hours_back = 10 if now_tr.hour == 9 else 4
                    _fetch_and_send(hours_back)

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
    t = threading.Thread(target=_news_watcher_loop, daemon=True, name="news-watcher")
    t.start()
