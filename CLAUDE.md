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
bot.py        → sinyal → Telegram (TELEGRAM_TOKEN)
               → HTTP  → claude_analyzer.py → karar → Telegram (ANALYZER_TELEGRAM_TOKEN)

SMC.py        → sinyal → kendi Telegram botu
               → [İLERDE] claude_analyzer.py → karar → Telegram (ANALYZER_TELEGRAM_TOKEN)

ROCKET        → sinyal → Telegram (PUMP_PROBABILITY_TOKEN, ayrı thread)
```

## Environment Variables (Render — bot.py servisi)

| Değişken | Açıklama |
|---|---|
| BINANCE_API_KEY | Binance API |
| BINANCE_API_SECRET | Binance Secret |
| TELEGRAM_TOKEN | Ana sinyal botu |
| TELEGRAM_CHAT_ID | Kullanıcı chat ID (tüm botlar ortak) |
| PUMP_PROBABILITY_TOKEN | ROCKET sinyalleri için ayrı thread tokeni |
| ANALYZER_TELEGRAM_TOKEN | @CLAUDE_ANALYZR_BOT tokeni |
| ANTHROPIC_API_KEY | Claude API |
| PORTFOLIO_URL | Portfolio tracker URL |
| PORTFOLIO_TOKEN | Portfolio auth token |

## Aktif Sistemler (bot.py)

1. **PANİK PUMP** — Kapitülasyon mean reversion | Stop -3% | TP +5/10/15% | WR ~%84
2. **T24** — DEVRE DIŞI
3. **ORTA VADE T72** — Stop -5% | TP +10% | WR %54
4. **UZUN VADE T168** — Stop -8% | TP +25% | WR %40
5. **ROCKET** — Momentum devam + hacim artışı | Stop -5% | TP +8/15/25%

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

Her analiz/backtest çalıştırıldığında sonuç dosyası şu formatta adlandırılır:

```
{strateji}_{sembol}_{başlangıç}_{bitiş}_{timestamp}.json
```

Örnek: `panik_pump_BTCUSDT_20240101_20260624_20260624_1423.json`

**Kurallar:**
- **TXT ÇIKTI KESİNLİKLE YASAK** — `.txt` dosyası hiçbir koşulda üretilmez
- Çıktılar scriptin bulunduğu klasöre kaydedilir — **ayrı alt klasör açılmaz**
- Her çalıştırma en fazla **2 dosya** üretir: `.json` (veri), `.html` (görsel rapor, isteğe bağlı)
- Tarihler `YYYYMMDD` formatında
- Timestamp (çalıştırma anı) `YYYYMMDD_HHMM` formatında — her çalıştırmayı ayırt eder
- Aynı scripti iki kez çalıştırsan iki ayrı dosya seti oluşur, hiçbiri kaybolmaz

## Claude Analyzer (claude_analyzer.py)

- Bağımsız modül — bot.py'nin iç yapısına bağlı değil
- Her sinyali değerlendirip @CLAUDE_ANALYZR_BOT üzerinden karar gönderir
- Kendi TF verisi çeker (Binance REST), F&G, dominans, portfolio bağlamı dahil
- SMC dahil her sistemden `process_and_send(signal_dict)` ile çağrılabilir
