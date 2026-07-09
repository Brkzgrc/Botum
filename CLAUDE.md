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
- **Backtest/analiz `.py` dosyaları repoya gitmez** — kullanıcıya direkt dosya olarak verilir. Local'de backtest klasörü tanımlanmışsa oraya, tanımlanmamışsa remote session'dan dosya olarak iletilir. Repoda backtest scripti bulundurulmaz.

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

---

## API Maliyet Baseline — Haziran 2026

> Karşılaştırma için referans. 1 ay sonra Temmuz verisiyle kıyaslanacak.

| Metrik | Değer |
|---|---|
| Dönem | 1 Haziran — 29 Haziran 2026 |
| Toplam maliyet | $5.00 |
| Kalan kredi | $20.00 |
| Model dağılımı | Opus 4.8 (baskın, yeşil) + Haiku 4.5 + Sonnet 4.6 |
| Günlük pik | ~$0.85 (7 Haz), ~$0.60 (13 Haz) |
| Son trend (19 Haz+) | $0.05–0.10/gün |

**Bu tarihten sonra yapılan optimizasyonlar:**
- market_analyzer: Opus → Sonnet
- Dedup (aynı haber tek API)
- Ondalık budama (token azaltma)
- Prompt temizliği (boş section'lar kaldırıldı)
- Breaking check: 2 saatte bir → 30 dakikada bir ama impact scoring ile çoğu API'siz
- API logger eklendi (api_usage.jsonl)
- SoSoValue kaldırıldı → Bitbo ETF akışı eklendi (dashboard + ANTON prompt)

## Gözlem Fazı — 30 Haziran 2026'dan İtibaren (GPT Tavsiyesi, Onaylandı)

**Faz 1 (şu an, 3-4 hafta):** Hiçbir yeni özellik eklenmiyor. Sadece bug fix. Amaç: son günlerdeki değişikliklerin (dedup, prompt temizliği, ETF akışı, vs.) etkisini karışmadan ölçmek.

**Faz 2 (3-4 hafta sonra):** Mevcut arşiv sistemine (claude_analyzer.py → `_archive_add_entry`) şu alanlar eklenecek — yeni logger yazılmayacak, var olan arşiv genişletilecek:
```json
{
  "etf_today": ...,
  "etf_5d_avg": ...,
  "news_score": ...
}
```

**Faz 3 (3 ay sonra):** Arşiv verisiyle analiz:
- ETF pozitifken WR kaç? Negatifken kaç?
- News Score 150+ iken TP2 oranı ne?
- Hangi makro koşullarda sistem en iyi performansı veriyor?

**Not (TODO, acil değil):** Arşive `system_version` / `strategy_version` alanı eklenecek (örn. "v3.1") — ileride versiyonlar arası WR karşılaştırması yapılabilsin.

---

## Al-Sat Bot Planı (Henüz Yazılmadı — Onaylanmış Tasarım)

### Genel Kurallar
- Her SMC-v2 sinyali otomatik trade olur — ANTON filtresi YOK, her sinyale giriş
- Binance **spot** (futures değil)
- Expire kullanılmayacak
- Cooldown: SMC sistemi kendi içinde yönetiyor, bot tarafında ek cooldown yok
- Aynı coinde zaten açık pozisyon varsa yeni sinyal reddedilir (portfolio_tracker mantığı aynen geçerli)

### Pozisyon Boyutlandırma
```
pozisyon_büyüklüğü = min(müsait_nakit / kalan_slot_sayısı, 20_000)
```
- Max eş zamanlı pozisyon: **5**
- Max pozisyon başına: **$20.000**
- Kalan slot = 5 − açık_pozisyon_sayısı
- Örnek: 30.000$ var, 3 işlem açık → `min(12.000 / 2, 20.000)` = **$6.000**

### Emir Akışı — Sinyal Geldiğinde
1. `trading_engine.py` sinyali alır, pozisyon büyüklüğünü hesaplar
2. **İki emir aynı anda** Binance'e gönderilir:
   - Market/limit buy → coin alınır
   - Stop-loss sell emri → Binance'te bekletilir (hard SL)
3. `trade_state.json`'a yazılır: symbol, miktar, giriş, stop, TP1, peak

### Fiyat Takibi — TP1 Öncesi
`position_monitor.py` WebSocket ile her tick'i izler:
- `fiyat > peak` → peak güncelle, `trade_state.json`'a yaz
- `fiyat ≤ stop` → Binance hard SL zaten tetikler; bot onaylar, state'den siler
- `fiyat ≥ TP1` → trailing moduna geç (aşağı bak)

### Fiyat Takibi — TP1 Sonrası (Trailing Modu)
1. Binance'teki stop-loss emri **iptal edilir**
2. Bot her tick'te hesaplar: `trail_stop = peak × 0.975` (%2.5 trailing)
3. Peak yükseldikçe trail_stop da yükselir — asla aşağı inmez
4. `fiyat ≤ trail_stop` → bot market sell gönderir → pozisyon kapanır → state'den silinir

### Crash Kurtarma
- **TP1 öncesi crash:** Binance'teki hard SL emri hâlâ ayakta — zarar korunuyor
- **TP1 sonrası crash:** `trade_state.json`'da `"trailing": true` yazıyor — bot yeniden başlayınca kaldığı yerden devam eder

### Yazılacak Dosyalar
| Dosya | Görev |
|---|---|
| `trading_engine.py` | Sinyal alır, Binance'e buy + stop-loss emri gönderir |
| `position_monitor.py` | WebSocket fiyat takibi, trailing yönetimi, kapanış |
| `trade_state.json` | Açık pozisyonların kalıcı state dosyası |

Entegrasyon noktası: `claude_analyzer.py` → `process_and_send()` çağrısından sonra `trading_engine.execute(signal)` çağrılacak.

### Henüz Netleşmeyenler
- Başlangıç sermayesi (ne olursa olsun 5'e bölünerek başlanacak, sabit değer gerekmez)
- **Expire süresi: 48 saat** — backtest sonucuna göre karar verildi (48H: WR %87.5, MaxDD -%6.56 — en iyi senaryo)
- Binance API izinleri: sadece spot trade + order yeterlisi açık olmalı, çekim kapalı
