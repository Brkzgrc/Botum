#!/usr/bin/env python3
"""
Özgür Analiz — GitHub Actions üzerinden çalışır.
Portfolio + piyasa verisini çeker, Claude ile analiz eder, Telegram'a gönderir.
"""
import os
import json
import requests
from datetime import datetime
from zoneinfo import ZoneInfo

PORTFOLIO_URL   = os.environ.get("PORTFOLIO_URL", "").rstrip("/")
PORTFOLIO_TOKEN = os.environ.get("PORTFOLIO_AUTH_TOKEN", "")
ANTHROPIC_KEY   = os.environ.get("ANTHROPIC_API_KEY", "")
TG_TOKEN        = os.environ.get("ANALYZER_TELEGRAM_TOKEN", "")
TG_CHAT         = os.environ.get("TELEGRAM_CHAT_ID", "")
TRIGGER_NOTE    = os.environ.get("TRIGGER_NOTE", "")


def fetch(path):
    headers = {"Authorization": f"Bearer {PORTFOLIO_TOKEN}"} if PORTFOLIO_TOKEN else {}
    r = requests.get(f"{PORTFOLIO_URL}{path}", headers=headers, timeout=15)
    r.raise_for_status()
    return r.json()


def send_telegram(text):
    if not (TG_TOKEN and TG_CHAT):
        print("[TG] Token/chat yok — sadece log.")
        return
    chunks = [text[i:i+4096] for i in range(0, len(text), 4096)]
    for chunk in chunks:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": chunk, "parse_mode": "HTML"},
            timeout=10,
        )
        print(f"[TG] status={r.status_code}")


def main():
    now = datetime.now(ZoneInfo("Europe/Istanbul")).strftime("%Y-%m-%d %H:%M TR")
    print(f"=== Özgür Analiz — {now} ===\n")

    if not PORTFOLIO_URL:
        print("HATA: PORTFOLIO_URL ayarlanmamış.")
        return

    # --- Veri çek ---
    perf     = fetch("/api/performance")
    open_pos = fetch("/api/open")
    market   = fetch("/api/market-data")

    print("--- PERFORMANS ---")
    print(json.dumps({k: v for k, v in perf.items()
                      if k in ("total", "open", "wins", "losses", "expired",
                               "win_partial", "total_pnl", "win_rate", "real_win_rate")},
                     indent=2, ensure_ascii=False))

    print(f"\n--- AÇIK POZİSYONLAR ({len(open_pos)}) ---")
    for p in open_pos:
        current = float(p.get("current_price") or 0)
        current_pct = float(p.get("current_pct") or 0)
        print(f"  {p.get('symbol','?')} | giriş:{p.get('entry',0):.5g} "
              f"| güncel:{current:.5g} ({current_pct:+.2f}%) "
              f"| TP1:{p.get('tp1',0):.5g} TP2:{p.get('tp2',0):.5g} "
              f"| stop:{p.get('stop',0):.5g} | tip:{p.get('sig_type','?')}")

    print("\n--- PİYASA ---")
    print(json.dumps({k: v for k, v in market.items()
                      if k in ("btc_price", "btc_change", "eth_price", "eth_change",
                               "fng_value", "fng_class", "btc_dominance", "total_mcap")},
                     indent=2, ensure_ascii=False))

    # --- Claude Analizi ---
    if not ANTHROPIC_KEY:
        print("\n[CLAUDE] ANTHROPIC_API_KEY yok — analiz atlandı.")
        return

    import anthropic

    open_count  = len(open_pos)
    total_pnl   = perf.get("total_pnl", 0.0)
    win_rate    = perf.get("win_rate", 0.0)
    wins        = perf.get("wins", 0)
    losses      = perf.get("losses", 0)
    expired     = perf.get("expired", 0)
    btc_price   = market.get("btc_price") or 0
    btc_change  = market.get("btc_change") or 0
    eth_price   = market.get("eth_price") or 0
    eth_change  = market.get("eth_change") or 0
    fng_val     = market.get("fng_value") or 50
    fng_cls     = market.get("fng_class") or "—"
    btc_dom     = market.get("btc_dominance") or 0
    total_mc    = market.get("total_mcap") or 0

    open_lines = ""
    for p in open_pos[:8]:
        sym  = p.get("symbol", "?")
        tip  = p.get("sig_type", "?")
        ent  = p.get("entry", 0)
        tp1  = p.get("tp1", 0)
        tp2  = p.get("tp2", 0)
        stp  = p.get("stop", 0)
        cur  = float(p.get("current_price") or 0)
        cur_pct = float(p.get("current_pct") or 0)
        tp1_hit = bool(p.get("tp1_hit"))
        trailing = bool(p.get("trailing_active") or p.get("trailing"))
        last_check = p.get("last_check") or "bilinmiyor"
        open_lines += (
            f"  {sym} ({tip}): giriş={ent:.5g} güncel={cur:.5g} "
            f"getiri={cur_pct:+.2f}% TP1={tp1:.5g} TP2={tp2:.5g} stop={stp:.5g} "
            f"TP1_vuruldu={tp1_hit} trailing={trailing} son_kontrol={last_check}\n"
        )

    mc_str = f"${total_mc/1e12:.2f}T" if total_mc >= 1e12 else f"${total_mc/1e9:.1f}B"

    prompt = f"""Sen bir kripto portföy analistisin. Aşağıdaki portföy ve piyasa verilerine göre kısa, net ve aksiyon odaklı bir değerlendirme yap.

PORTFÖY DURUMU:
- Açık pozisyon: {open_count}
- Toplam P&L: {total_pnl:+.2f}%
- Win/Loss/Expired: {wins}/{losses}/{expired}
- Win Rate: %{win_rate:.0f}

AÇIK POZİSYONLAR:
{open_lines if open_lines else "  (yok)"}

PİYASA:
- BTC: ${btc_price:,.0f} ({btc_change:+.2f}%)
- ETH: ${eth_price:,.2f} ({eth_change:+.2f}%)
- Korku/Açgözlülük Endeksi: {fng_val}/100 ({fng_cls})
- BTC Dominans: %{btc_dom}
- Toplam Piyasa Değeri: {mc_str}

{f"Tetikleyici not: {TRIGGER_NOTE}" if TRIGGER_NOTE else ""}

Çıktı formatı:
- Her nokta ayrı satırda olsun
- Maksimum 4 satır
- Her satır tek cümle, kısa ve net
- Yalnızca verilen sayısal verilere dayan; eksik bilgiyi tahmin etme
- Bir pozisyonun TP1'e yakınlığını yalnızca güncel fiyat alanı sıfırdan büyükse hesapla
- Güncel fiyat yoksa o pozisyon için hedefe yakınlık veya kâr realizasyonu önerme
- TP1_vuruldu veya trailing durumu açıkça true değilse gerçekleşmiş gibi yazma
- Varsa aksiyon önerisi son satırda
- Markdown yok, sembol yok, düz metin"""

    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}],
    )
    analysis = resp.content[0].text.strip()
    print(f"\n=== CLAUDE ANALİZİ ===\n{analysis}\n")

    # --- Telegram ---
    fng_emoji = "😱" if fng_val < 25 else ("😨" if fng_val < 45 else
                ("😐" if fng_val < 55 else ("😏" if fng_val < 75 else "🤑")))

    msg = (
        f"🤖 <b>Özgür Analiz</b> — {now}\n\n"
        f"📊 <b>Portföy:</b> {open_count} açık | P&amp;L {total_pnl:+.2f}% | "
        f"W/L {wins}/{losses}\n"
        f"₿ <b>BTC:</b> ${btc_price:,.0f} ({btc_change:+.2f}%) | "
        f"D:%{btc_dom}\n"
        f"{fng_emoji} <b>F&amp;G:</b> {fng_val}/100 ({fng_cls})\n\n"
        f"{analysis}"
    )
    send_telegram(msg)
    print("=== TAMAMLANDI ===")


if __name__ == "__main__":
    main()
