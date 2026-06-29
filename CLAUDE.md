# GPT Mimari Analizi ve Optimizasyon Önerileri
> Tarih: 29 Haziran 2026 — Sistemin mevcut haliyle ilgili GPT-4o değerlendirmesi

## Genel Puan

| Başlık            | Puan  |
|-------------------|------:|
| Modüler yapı      | 10/10 |
| Haber sistemi     |  8/10 |
| API optimizasyonu | 8.5/10|
| Token verimliliği |  7/10 |
| Ölçeklenebilirlik |  9/10 |

Hedef: **9.8/10**

---

## 1) market_analyzer.py — Ham Veri Yerine Özet Veri Gönder

Claude bu modülde hesaplamıyor, sadece yorum yapıyor. Input'u şöyle sadeleştir:

```
# Şu an (pahalı):
200W MA: 92345 | Current: 108123 | Distance: 17.4%

# Öneri (ucuz):
200W MA: Far Above (+17%)
```

**Beklenen kazanım:** Input 3.000 token → 800-1.200 token

---

## 2) Haber Sistemi — Daha İnce Skorlama Katmanı

Şu an: `<80 → API yok | 80-149 → Haiku | ≥150 → Sonnet`

Öneri:
```
<40      → Yok say
40-80    → Arşivle (API yok)
80-120   → Haiku
120-170  → Sonnet
170+     → Yüksek öncelik
```

---

## 3) Aynı Haber Problemi — Dedup Eksik

Reuters, CoinDesk, Cointelegraph, Decrypt, TheBlock aynı haberi veriyor.
Şu an muhtemelen 4 API çağrısı → olması gereken 1.

**Öneri:** `RapidFuzz` veya sentence similarity ile başlık benzerliği hesapla,
`%80+ benzerlik → aynı haber → birini at`.
Beklenen tasarruf: **%20-30**.

---

## 4) claude_analyzer.py — Token Sıkıştırma

Şu an: 1.500-2.000 token input. Öneri: 600-900 token.

```
# Şu an:
EMA21: 109342 | EMA50: 108443 | EMA200: 106553

# Öneri:
EMA: Bullish Alignment
RSI: Neutral (61)
MACD: Bullish Cross
Fear & Greed: 61 (Greed)
BTC.D: Increasing
Liquidity: Above High
```

---

## 5) Market Watcher — Çoklu Koşul Tetikleyici

Şu an: BTC ±5% → Claude çağrılır.

Öneri:
```
BTC +4% VE OI +12% VE Funding >0.03 → Claude çağır
aksi halde → sessiz kal
```
Yüksek volatilite ama düşük OI/Funding = zayıf hareket, Claude'a gerek yok.

---

## 6) Context Memory — Bir Önceki Analize Referans

Her seferinde tüm veriyi sıfırdan gönderiyorsun.
30 dakika önce gönderilen analizle şimdiki çok benzer olabilir.

**Öneri:** Son analiz sonucunu cache'le, sadece değişimi gönder:
```
Previous: Bullish | Now: Neutral
Changed: Funding ↑, OI ↓, Volume ↓
```

---

## 7) En Büyük Potansiyel — AI Decision Gateway

**Mevcut akış:**
```
Binance Veri → Claude (her şeyi değerlendir)
```

**Önerilen akış:**
```
Binance Veri
    │
    ▼
Python Analiz Motoru
(EMA, RSI, ADX, Hacim, Likidite, Makro, Haber Skoru)
    │
    ▼
Güven Skoru
    │
    ├── ≥95 → Claude YOK — Python kararı uygulanır
    ├── 80-94 → Haiku — sadece doğrulama
    └── <80 veya çelişkili sinyaller → Sonnet — detaylı analiz
```

**İki avantajı:**
1. Yüksek güvenli durumlarda API sıfır.
2. Claude artık hesap yapan değil, Python kararını denetleyen hakem olur.

---

> *Not: Bu öneriler uygulanmadan önce her biri ayrıca değerlendirilmeli.*
> *Öncelik sırası: 3 → 4 → 7 → 1 → 6 → 2 → 5*

---
---

# Botum — Proje Notları

## GitHub Repo
- **URL:** https://github.com/brkzgrc/Botum
- **Branch:** main (tüm değişiklikler direkt main'e)

## Kritik Kurallar

- **SMC.py'ye kesinlikle dokunma** — SMC tek düzgün çalışan ve sistemin temel parçası; dokunmak için ayrı, açık onay şart
- **SMC_original.py'ye kesinlikle dokunma** — referans kopya, hiçbir koşulda değiştirilemez
- **Onay almadan kod yazma** — her implementasyon öncesi onay gerekli
- **portfolio_tracker_PASİF.py** — sadece referans, üretim kodu değil
- **claude_analyzer.py** — AKTİF üretim kodu (portfolio_tracker.py içinde çalışır, `/api/analyze` üzerinden bot.py sinyallerini değerlendirir)
- **Değişiklikleri direkt main'e push et** — başka branch kesinlikle kullanılmaz

## Session İş Bölümü

| Session | Görev |
|---|---|
| **Remote (bu session)** | Kod değişikliği, GitHub push, akıl danışma |
| **Local (bilgisayar)** | TradingView analizi (MCP-Jackson), backtest çalıştırma |

**Local session kesinlikle:** Kod değişikliği yapmaz, dosya düzenlemez, GitHub'a push etmez. Sadece okur, analiz eder, test çalıştırır.

## Servis URL'leri

| Servis | URL |
|---|---|
| Portfolio Tracker | https://portfolio-tracker-xzvw.onrender.com |

## Geliştirme Branch

Her değişiklik direkt **main**'e push edilir. Başka branch kullanılmaz, açılmaz.

## SMC Entegrasyonu (Bekleyen Görev)

SMC.py'ye Claude Analyzer bağlamak için **sadece şu iki satır** eklenecek:

```python
from claude_analyzer import process_and_send
process_and_send(signal_dict)
```

`signal_dict` zorunlu alanlar: `symbol`, `type`, `entry`, `stop`, `tp1`
Opsiyonel: `source` ("smc"), `tp2`, `tp3`, sistem-spesifik metrikler

**NOT:** SMC.py'ye dokunmak için kullanıcıdan açık onay gerekli.

## Sistem Mimarisi

```
bot.py        → sinyal → Telegram thread 5 (TELEGRAM_TOKEN)
               → HTTP  → claude_analyzer.py → karar → Telegram (ANALYZER_TELEGRAM_TOKEN)

SMC.py        → sinyal → kendi Telegram botu
               → [İLERDE] claude_analyzer.py → karar → Telegram (ANALYZER_TELEGRAM_TOKEN)
```

## Environment Variables (Render — bot.py servisi)

### Aktif / Gerekli

| Değişken | Açıklama |
|---|---|
| BINANCE_API_KEY | Binance API |
| BINANCE_API_SECRET | Binance Secret |
| TELEGRAM_TOKEN | Ana sinyal botu (thread 5) |
| TELEGRAM_CHAT_ID | Kullanıcı chat ID |
| ANALYZER_TELEGRAM_TOKEN | @CLAUDE_ANALYZR_BOT tokeni (portfolio tracker kullanır) |
| ANTHROPIC_API_KEY | Claude API (ileride kullanım için) |
| PORTFOLIO_URL | Portfolio tracker URL |
| PORTFOLIO_TOKEN | Portfolio auth token |

### Render'dan SİLİNECEK (artık kullanılmıyor)

| Değişken | Neden |
|---|---|
| PUMP_PROBABILITY_TOKEN | ROCKET sistemi kaldırıldı |
| SIGNAL_COOLDOWN_HOURS | Kapitülasyon sistemi kaldırıldı |
| CRASH_MIN | Kapitülasyon sistemi kaldırıldı |
| CRASH_MAX | Kapitülasyon sistemi kaldırıldı |
| VOL_MIN | Kapitülasyon sistemi kaldırıldı |
| VOL_MAX | Kapitülasyon sistemi kaldırıldı |
| MIN_LIQUIDITY | Kaldırıldı — backtest'te hacim filtresi yoktu |

## Aktif Sistemler (bot.py)

1. **PUMP** (2026-06-28) — 15m spike ≥15x + 1h hacim ≥5x + 4h trend + ROC ≥24% | Stop -5% | TP +20% | Backtest WR ~%91
   - Eski sistemler (PANİK PUMP, T24, T72, T168, ROCKET) tamamen kaldırıldı

## EVE GELİNCE YAPILACAKLAR (Hatırlatma)

- **PANİK PUMP JSON dosyası** — Bilgisayarda ~150-200 sinyalli eski portfolio export'u var. Bu dosyayı buraya upload et veya Render shell'inde `python panik_pump_analysis.py --file /path/to/file.json` ile çalıştır. Şu an 32 sinyal ile çalışıyoruz, 150-200 ile sonuçlar çok daha anlamlı olur.
- **panik_pump_analysis.py'ye --file flag ekle** — JSON dosyasından okuma desteği henüz yok, eklenecek.
- **Özgür Analiz Sistemi** — GitHub Actions kurulumu: workflow dosyası + GitHub secrets (PORTFOLIO_URL, PORTFOLIO_AUTH_TOKEN). Ben tetikleyip sonucu okuyabilirim, kullanıcı müdahalesi gerekmez.

## Uzun Vadeli Sistem Hedefi (Kullanıcının Vizyonu)

**"Sürekli izleyen, bağlam biriktiren sistem"** — şu anki analyzer reaktif (sinyal gelince çalışır). Hedef: proaktif, hafızalı, trader gibi düşünen sistem.

- Sinyal gelmeden önce piyasayı zaten analiz etmiş olsun
- "Bu düşüş likidite temizliğinden miydi?" sorusunu cevaplayabilsin
- Haber etkisini, Twitter'daki analist yorumlarını bağlama katabileceği bir yapı
- Her indikatörü en iyi çalıştığı timeframe'de kullansın (FBB haftalık, TMA 3 günlük, SSL haftalık)
- Tek seferlik değerlendirme değil, sürekli hafıza gerektiriyor

**Şu an yapılan (adım adım):** claude_analyzer.py'ye FBB + SSL + TMA + likidite tespiti ekleniyor.
**Sonraki aşama:** Proaktif tarama + hafıza mimarisi (GitHub Actions + portfolio tracker entegrasyonu).

## Bekleyen Fikirler (İleride Değerlendir)

- **Claude Tarama Kanalı** — Bot sinyallerinden bağımsız olarak Claude'un kendi coin taraması yapacağı ayrı bir Telegram kanalı/botu. Önce bot sinyallerinin 2-3 aylık gerçek verisi biriksin, sonra karşılaştırmalı değerlendirme yapılsın. Haziran 2026'dan itibaren veri toplanıyor.

## İnteraktif Analiz Botu (İleride — Acil Değil)

Telegram kanalına coin + timeframe yazınca anında analiz gelsin:
- `btc 1h` → BTC 1 saatlik analiz
- `zec 1h` → ZEC 1 saatlik analiz
- `uni 1w` → UNI haftalık analiz

**Kapsam:** Binance spot'taki **herhangi bir coin**, herhangi bir timeframe  
**Mimari:** Telegram webhook → Python handler → claude_analyzer → yanıt  
**Durum:** Bekleyen fikir — önce mevcut sistemler olgunlaşsın

## Backtest / Analiz Çıktı Kuralı

Her analiz/backtest çalıştırıldığında şu 2 dosya üretilir:

```
{strateji}_{sembol}_{başlangıç}_{bitiş}_{timestamp}.json
{strateji}_{sembol}_{başlangıç}_{bitiş}_{timestamp}.html
```

Örnek:
```
panik_pump_BTCUSDT_20240101_20260624_20260624_1423.json
panik_pump_BTCUSDT_20240101_20260624_20260624_1423.html
```

**Kurallar:**
- **TXT ÇIKTI KESİNLİKLE YASAK** — `.txt` dosyası hiçbir koşulda üretilmez
- Çıktılar scriptin bulunduğu klasöre kaydedilir — **ayrı alt klasör açılmaz**
- Tarihler `YYYYMMDD` formatında
- Timestamp (çalıştırma anı) `YYYYMMDD_HHMM` formatında — her çalıştırmayı ayırt eder
- Aynı scripti iki kez çalıştırsan iki ayrı dosya seti oluşur, hiçbiri kaybolmaz

## Claude Analyzer (claude_analyzer.py)

- Bağımsız modül — bot.py'nin iç yapısına bağlı değil
- Her sinyali değerlendirip @CLAUDE_ANALYZR_BOT üzerinden karar gönderir
- Kendi TF verisi çeker (Binance REST), F&G, dominans, portfolio bağlamı dahil
- SMC dahil her sistemden `process_and_send(signal_dict)` ile çağrılabilir
