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
┌─── SİNYAL KAYNAKLARI ───────────────────────────────────────────────┐
│                                                                      │
│  SMC.py ──────────────────────────────────────► Telegram (kendi botu)
│    │ send_to_portfolio(signal_price=price)                           │
│    └──────────────────────────────────────────► portfolio_tracker    │
│                                                  /api/signal         │
│  bot.py ──────────────────────────────────────► Telegram (thread 5) │
│    │ HTTP                                                            │
│    └──────────────────────────────────────────► portfolio_tracker    │
│                                                  /api/analyze        │
└──────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─── portfolio_tracker.py  (Render — her zaman çalışır) ──────────────┐
│                                                                      │
│  /api/signal   → signals_db'ye ekle (status: pending_retest)        │
│  /api/analyze  → claude_analyzer.process_and_send() [thread]        │
│                                                                      │
│  position_checker_loop() [5dk]:                                      │
│    ├── check_pending_retests()                                       │
│    │     ├── 48H doldu   → no_retest  + Telegram bildirimi          │
│    │     └── low ≤ limit → open       + Telegram bildirimi          │
│    └── check_open_positions()                                        │
│          ├── stop/trail/tp2/expire → closed + Telegram bildirimi    │
│          └── _update_archive_outcome() → learning_archive.json      │
│                                                                      │
│  /api/retest-filled    ← position_monitor (bot açıkken)             │
│  /api/retest-cancelled ← position_monitor (bot açıkken)             │
│  /api/position-closed  ← position_monitor (bot açıkken)             │
└───────────────┬─────────────────────────────────────────────────────┘
                │
        ┌───────┴───────────────────────────┐
        ▼                                   ▼
┌─── claude_analyzer.py ──────┐   ┌─── trading_engine.py ────────────┐
│                              │   │                                   │
│ process_and_send(signal)     │   │ execute(signal)                   │
│ → Binance TF verisi çeker    │   │ → state'e pending yaz (ID=None)  │
│ → F&G, dominans, portfolio  │   │ → Binance LIMIT BUY emri         │
│ → Claude API karar üretir    │   │ → orderId state'e güncelle       │
│ → ANALYZER_TELEGRAM_TOKEN   │   │ → hata olursa state temizle      │
│ → /api/signal/{id}/analyzer  │   │                                   │
│   PATCH ile DB'yi güncelle   │   │ TRADING_ENABLED=false →          │
└──────────────────────────────┘   │ simülasyon (Binance'e git yok)   │
                                   └──────────────┬────────────────────┘
                                                  │ trade_state.json
                                                  ▼
                                   ┌─── position_monitor.py ──────────┐
                                   │  (bot servisiyle çalışır)        │
                                   │                                   │
                                   │ _check_pending_orders() [60s]:   │
                                   │   ├── limit fill → _activate     │
                                   │   │   → SL emri + WS başlat     │
                                   │   │   → /api/retest-filled       │
                                   │   └── 48H → _cancel_pending      │
                                   │       → /api/retest-cancelled    │
                                   │                                   │
                                   │ _process_tick(close, high, low): │
                                   │   ├── peak güncelle (high)        │
                                   │   ├── SL hit (low)               │
                                   │   │   closing=True → sell        │
                                   │   │   → /api/position-closed     │
                                   │   ├── TP1 hit → trailing modu    │
                                   │   └── trail stop (low)           │
                                   │       closing=True → sell        │
                                   │       → /api/position-closed     │
                                   └───────────────────────────────────┘

Kalıcı Dosyalar:
  trade_state.json       → position_monitor ↔ trading_engine (ortak)
  portfolio_signals.json → portfolio_tracker signals_db (tüm geçmiş)
  learning_archive.json  → claude_analyzer arşiv (sonuç öğrenme)

## Kalıcı Dosya Yolları (Render — bot servisi)

| Dosya | Yol | Erişim |
|---|---|---|
| `trade_state.json` | `/var/data/trade_state.json` | Render Dashboard → bot servisi → **Shell** sekmesi |
| Env var | `TRADE_STATE_FILE=/var/data/trade_state.json` | Render Dashboard → bot servisi → Environment |

**Shell'den okuma:**
```bash
cat /var/data/trade_state.json | python3 -m json.tool
```

**Shell'den belirli pozisyon silme (örn. OGUSDT):**
```bash
python3 -c "
import json, os
f = '/var/data/trade_state.json'
with open(f) as fp: s = json.load(fp)
removed = s.get('positions', {}).pop('OGUSDT', None)
print('Silindi' if removed else 'Bulunamadi')
tmp = f + '.tmp'
with open(tmp, 'w') as fp: json.dump(s, fp, indent=2, default=str)
os.replace(tmp, f)
"
```

Not: Bot servisi suspend iken portfolio_tracker kendi döngüsüyle
     fiyat bazlı retest/kapanış takibini devam ettirir.

## Trading-Bot Render Servisi

**URL:** https://trading-bot-06wp.onrender.com  
**Repo dosyaları (included paths):**

| Dosya | Görev |
|---|---|
| `trading_main.py` | Flask entry point — `/signal`, `/status`, `/position/<sym>` (DELETE), `/health` |
| `trading_engine.py` | Sinyali alır, monitoring state'e yazar |
| `position_monitor.py` | Fiyat izleme, limit emir açma, trailing, kapanış |
| `requirements_trading.txt` | Bağımlılıklar |

**Auth:** `TRADE_BOT_TOKEN` env var (header: `X-Bot-Token`). Boşsa auth açık.  
**State dosyası:** `/var/data/trade_state.json` (persistent disk, `TRADE_STATE_FILE` env var)

**Portfolio tracker'dan erişim:**
- `GET /api/trade-positions` → trading-bot `/status` proxy
- `POST /api/trade-positions/<symbol>/delete` → trading-bot `DELETE /position/<symbol>` proxy
- UI: Dashboard'da **AL-SAT BOT POZİSYONLARI** bölümü (Sil butonu Binance emrini silmez, sadece state'den siler)

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

## Rejim/Çöküş Filtresi Araştırması (2026-07-21, ÖLÇÜLDÜ VE REDDEDİLDİ)

**Soru:** FTX çöküşü / "Trump manipülasyonu" tarzı ani, keskin küresel düşüşlere karşı sistemin (mevcut 4H BTC crash filtresi dışında) ek bir koruması olmalı mı? Endişe: sistem hem (a) böyle bir dönemde yeni kötü sinyaller alabilir hem (b) o dönem başladığında zaten açık olan pozisyonlar ağır zarar edebilir.

**Yöntem:** `smc_regime_damage_v2_velocity.py` (scratchpad, repoya gitmedi) — BTC fiyatından (haber etiketi kullanmadan) sabit W-saatlik ROC ile "ani şok" pencereleri tespit edip (haber etiketsiz, sadece fiyattan gerçek FTX Kasım 2022 ve LUNA/Celsius Haziran 2022 olaylarını doğru buldu), CHoCH sisteminin (canlı ayarlar) bu pencerelerdeki gerçek net PnL'ini iki ayrı kalemde (pencere içinde açılan yeni işlemler / pencereden önce açılıp içinde kapanan pozisyonlar) ölçtü.

**Sonuç:**
- **Önceden-açık pozisyon hasarı** (4 farklı hız-eşiği kombinasyonunda): +$1.334 ile -$662 arası — sistemin $1.58M'lik toplam brüt kaybının **%0.04'ü**. "Çöküş başladığında içeride yakalanıp ezildik" hipotezi desteklenmedi — hibrit stop (ATR×2.75) + TP1-sonrası breakeven-altına-inmeyen trailing bu riski zaten büyük ölçüde absorbe ediyor.
- **Kriz penceresinde açılan yeni işlemler**: bazı pencerelerde net zarar yazıyor ama toplamı sistemin tüm-zaman toplam kaybının **%0.48-%1.58'i** — küçük. Ayrıca daha gevşek bir rejim tanımıyla (v1, rolling 30 günlük zirveden düşüş) yapılan ilk denemede, "kriz" sayılan dönemlerde yeni sinyalleri bloklamanın sistemden **%36 kâr keseceği** görüldü (o dönemlerde CHoCH çoğunlukla kârlıydı) — yanlış filtreleme riski, kurtarılacak tutardan kat kat büyük çıktı.

**Karar:** Canlı sisteme yeni bir crash/regime filtresi (yeni giriş engelleme, açık pozisyon stop sıkma/erken kapatma, çok değişkenli "risk regime manager") **eklenmiyor**. Sezgisel olarak korkutucu görünen bu risk, rigorous ölçüldüğünde sistem için anlamlı bir iyileştirme alanı çıkarmadı. Codex ile bağımsız çapraz incelemeyle doğrulandı. Tekrar gündeme gelirse bu bölüme bakılsın — yeni bir kanıt (örn. gerçekten büyük bir kriz penceresi canlıda yaşanırsa) olmadıkça tekrar açılmasın.

## Kademeli ATR Trailing (Winner Extension) Araştırması (2026-07-21, ÖLÇÜLDÜ VE REDDEDİLDİ)

**Soru:** Bazı pozisyonlar TP1 sonrası peak +%19-20'ye kadar çıkıp, sabit ATR×0.6 trailing ile +%14-15'te kapanıyor — sonra fiyat +%50'lere gidiyor gözlemi yapıldı. Sabit ATR_MULT=0.6 yerine TP1-sonrası çarpanı peak_pct'e göre kademeli genişletmek (küçük kazananlarda sıkı 0.6, büyük koşanlarda 0.9-1.5) büyük koşanları erken boğulmaktan kurtarır mı?

**1. tur — metodoloji hatası bulundu:** İlk backtest scripti (`smc_tiered_trailing_test.py`) TP1-öncesi VE TP1-sonrasını TEK sabit `EXPIRE_H=24H` penceresiyle sınırlıyordu. Ama canlıda (`position_monitor.py`) TP1 vurup trailing'e geçen pozisyon expire'dan **tamamen muaf** — sadece `trail_stop`'a düşünce kapanır, saat sınırı yok. Bu ilk test sonucu (3 varyant da kötü) bu yüzden canlıya genellenemedi — sadece "24H cap altında trailing genişletmek kötü" derdi, asıl "büyük koşanları yakalama" sorusunu hiç test etmemişti.

**2. tur — canlı-parite düzeltmesiyle doğru test:** `smc_tiered_trailing_live_parity.py` (scratchpad, repoya gitmedi) — TP1-öncesi 24H sınırlı kaldı (canlı `OPEN_EXPIRE_H`), TP1-sonrası trailing SÜRESİZ yapıldı (canlı muafiyetle birebir), veri penceresi 45 güne genişletildi.

**Sonuç:**
- **Live-parity düzeltmesi doğru ve gerekliymiş** — eski canonical backtest TP1-sonrası süre muafiyetini tam modellemiyordu. Etki küçük ama gerçek: sabit ATR_MULT=0.6 ile Final $1,248,756 → $1,257,033 (+%0.66), MaxDD aynı (-%9.19) — bazı trade'ler eskiden 24H'te zorla "expire" ile kapanıyordu, artık gerçek trail seviyesine kadar bekleyip biraz daha fazlasını yakalıyor.
- **Kademeli ATR_MULT (Variant A/B/C) doğru motorla da kesin reddedildi** — final equity farkı: Variant A ≈ **-$158.971**, Variant B ≈ **-$200.902**, Variant C ≈ **-$81.774**. `data_end_n=0` (45 günlük pencere hiç yetersiz kalmadı, veri kısıtı sonucu bozmadı). `runner_pattern_caught_n=0` — hem Aşama-1 hem 3 Aşama-2 varyantında, ~5.589 ortak trade üzerinde, aranan "+19'a çıkıp geri çekilip sonra +40/+50'ye giden" sınıf **bir kere bile** yakalanmadı. En büyük kötüleşen trade'lerde (CVX, CYBER, OG, ASR, ACE, RARE, ARDR, JUV, FTT, POND, AUCTION, OSMO) `peak_pct_diff=0.0` — varyantlar AYNI tepe noktasına ulaşıyor, sadece daha gevşek trail yüzünden oradan daha düşük fiyattan çıkıyor. Yani trail genişletmek pozisyona ek yükseliş yakalatmıyor, sadece aynı tepeden inişte daha fazla kâr kaybettiriyor.

**Karar:**
1. **Backtest canonical referansı güncellendi:** Bundan sonraki tüm backtest'lerde canlı-parity motor kullanılmalı — TP1-öncesi 24H expire, TP1-sonrası süresiz trailing (`smc_tiered_trailing_live_parity.py`'deki `run_trade_from_retest_live_parity()` referans alınabilir).
2. **Kademeli ATR_MULT hipotezi reddedildi** — 24H cap itirazı tamamen giderildikten sonra da tüm varyantlar Baseline'dan kötü çıktı. Canlı koda **hiçbir değişiklik yapılmıyor**. Bu testin en değerli çıktısı: mevcut sabit `ATR_MULT=0.6` trailing zaten oldukça iyi ayarlanmış. Kullanıcının gözlemlediği "+50'ye giden" hareketler muhtemelen aynı pozisyonun devamı değil, ayrı bir sinyalin sonucu — trail genişliğiyle çözülecek bir problem değil. Tekrar gündeme gelirse (örn. gerçekten aynı pozisyonun devamı olan büyük bir runner canlıda yakalanırsa) bu bölüme bakılsın.

## EVE GELİNCE YAPILACAKLAR (Hatırlatma)

- **PANİK PUMP JSON dosyası** — Bilgisayarda ~150-200 sinyalli eski portfolio export'u var. Bu dosyayı buraya upload et veya Render shell'inde `python panik_pump_analysis.py --file /path/to/file.json` ile çalıştır. Şu an 32 sinyal ile çalışıyoruz, 150-200 ile sonuçlar çok daha anlamlı olur.
- **panik_pump_analysis.py'ye --file flag ekle** — JSON dosyasından okuma desteği henüz yok, eklenecek.
- **Özgür Analiz Sistemi** — GitHub Actions kurulumu: workflow dosyası + GitHub secrets (PORTFOLIO_URL, PORTFOLIO_AUTH_TOKEN). Ben tetikleyip sonucu okuyabilirim, kullanıcı müdahalesi gerekmez.
- **GİR/DİKKAT dağılımını tekrar kontrol et** — 2026-07-20'de analyzer prompt'u DİKKAT için somut veri şartı arayacak şekilde değiştirildi (bkz. "Uzun Vadeli Sistem Hedefi" altındaki not). Birkaç gün/hafta sonra, yeterli yeni kapanmış işlem birikince, portfolio-tracker Shell'de şu script tekrar çalıştırılıp GİR oranının gerçekten arttığı doğrulanmalı:
  ```bash
  python3 -c "
  import json, os
  from collections import defaultdict
  f = os.path.join(os.getenv('DATA_DIR', '/tmp'), 'portfolio_signals.json')
  with open(f) as fp: sigs = json.load(fp)
  stats = defaultdict(lambda: [0,0])
  for s in sigs:
      if s.get('status') != 'closed': continue
      pct = s.get('close_pct')
      if pct is None: continue
      v = (s.get('analyzer_decision') or 'YOK').split(' ',1)[-1] if s.get('analyzer_decision') else 'YOK'
      stats[v][0] += 1
      stats[v][1] += 1 if pct > 0 else 0
  for v, (n, w) in sorted(stats.items()):
      print(f'{v:<12} n={n:4d}  WR=%{round(w/n*100,1) if n else 0}')
  "
  ```
  Bu ortamdan (remote session) portfolio-tracker'a ağ politikası gereği doğrudan erişilemiyor (403) — script'i kullanıcının Render Shell'inden çalıştırıp sonucu buraya yapıştırması gerekiyor.

## Uzun Vadeli Sistem Hedefi (Kullanıcının Vizyonu)

**"Sürekli izleyen, bağlam biriktiren sistem"** — şu anki analyzer reaktif (sinyal gelince çalışır). Hedef: proaktif, hafızalı, trader gibi düşünen sistem.

- Sinyal gelmeden önce piyasayı zaten analiz etmiş olsun
- "Bu düşüş likidite temizliğinden miydi?" sorusunu cevaplayabilsin
- Haber etkisini, Twitter'daki analist yorumlarını bağlama katabileceği bir yapı
- Her indikatörü en iyi çalıştığı timeframe'de kullansın (FBB haftalık, TMA 3 günlük, SSL haftalık)
- Tek seferlik değerlendirme değil, sürekli hafıza gerektiriyor

**Şu an yapılan (adım adım):** claude_analyzer.py'ye FBB + SSL + TMA + likidite tespiti ekleniyor.
**Sonraki aşama:** Proaktif tarama + hafıza mimarisi (GitHub Actions + portfolio tracker entegrasyonu).

**Bulunan hata (2026-07-20, düzeltildi):** `claude_analyzer.py` → `_portfolio_context()`, Claude'un kararına geçmiş performansı ("[BU COİN GEÇMİŞİ]") katmak için yazılmıştı, ama trading bot canlıya geçtikten sonra smc-v2 kapanışları `/api/position-closed` webhook'undan `status="closed"` + `outcome`/`close_pct` şemasıyla geliyor — fonksiyon hâlâ eski `status` değerlerine (`win_tp1` vb.) bakıyordu, hiç eşleşmiyordu. Sonuç: geçmiş kaydı tutuluyordu ama analyzer'a hiç ulaşmıyordu, "geçmiş veri yok" hep dönüyordu. `_CLOSED_ST`'ye `"closed"` eklendi, `_is_win()` `outcome`/`close_pct` alanlarına bakacak şekilde genişletildi.

**Bulunan hata (2026-07-20, düzeltildi):** Kapanmış işlemlerin ~%50'sinde dashboard'da hiç analiz görünmüyordu — `claude_analyzer.py`'deki zincirin (Claude API çağrısı, portfolio'ya PATCH ile sonucu bildirme) her adımı tek seferlikti, geçici bir ağ/rate-limit hatasında sessizce vazgeçiyordu, retry yoktu. `evaluate()`'teki Claude çağrısına ve `_update_portfolio_analyzer()`'daki PATCH'e 3 denemeye kadar (2sn/4sn backoff) retry eklendi. Not: `SMC.py`'deki ilk `/api/analyze` POST'u (zincirin başlangıcı) kasıtlı olarak dokunulmadı — SMC.py'ye değişiklik ayrı onay gerektiriyor.

**Bulunan sorun (2026-07-20, düzeltildi):** Analiz üretilen sinyallerin ~%94'ü "DİKKAT" çıkıyordu, "GİR" pratikte hiç verilmiyordu (34 kapanmış işlemde 16 DİKKAT'e karşı 1 GİR) — verdict ayırt edici olmaktan çıkmıştı. Sebep: prompt'ta "Belirsizlik varsa DİKKAT yeterli" talimatı, piyasa her zaman bir miktar belirsizlik taşıdığı için modeli sürekli güvenli tarafa (DİKKAT) itiyordu. Talimat, RİSKLİ için zaten var olan "somut neden şart" kuralının DİKKAT'e de uygulanacağı şekilde değiştirildi — artık DİKKAT de referans verilebilir somut bir veri noktasına dayanmak zorunda, yoksa GİR veriliyor.

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

## Al-Sat Bot Planı (Yazıldı — Aktif)

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

### Yazılan Dosyalar
| Dosya | Görev |
|---|---|
| `trading_engine.py` | Sinyal alır, Binance'e LIMIT BUY emri gönderir (CHoCH+1tick) |
| `position_monitor.py` | WebSocket fiyat takibi, trailing yönetimi, kapanış |
| `trade_state.json` | Açık pozisyonların kalıcı state dosyası |

Entegrasyon noktası: `claude_analyzer.py` → `process_and_send()` çağrısından sonra `trading_engine.execute(signal)` çağrılacak. (Henüz bağlı değil — SMC.py onayı bekleniyor.)

### Henüz Netleşmeyenler
- Başlangıç sermayesi (ne olursa olsun 5'e bölünerek başlanacak, sabit değer gerekmez)
- **Expire süresi: 48 saat** — backtest sonucuna göre karar verildi (48H: WR %87.5, MaxDD -%6.56 — en iyi senaryo)
- Binance API izinleri: sadece spot trade + order yeterlisi açık olmalı, çekim kapalı

### TP1 Sonrası Trailing — Güncel Durum (2026-07-16)

**Not:** Yukarıdaki "Fiyat Takibi — TP1 Sonrası" bölümü eski (%2.5 sabit) — artık geçerli değil. Güncel sistem:

- **Trail formülü:** `trail_stop = peak - ATR(14, 1H) × 0.6` — backtestteki `smc_atr_trail.py`'nin `exit_trail()` fonksiyonuyla birebir aynı olacak şekilde kuruldu (amaç: canlı sonuçlar backtestten sapmasın, sapma olursa backtest anlamsız kalır — kullanıcının açık talebi).
- **Entry floor:** Trail seviyesi entry'nin altına inemez (`_trail_stop_price` içinde clamp, 2026-07-16'da eklendi) — backtestteki `if trail < entry: trail = entry` ile eşleşiyor.
- **İki farklı trailing mekanizması var, davranışları FARKLI:**
  1. **ATR cancel-replace** (`position_monitor.py` — `_place_trail_sl_order` / `_trail_stop_price`): her tick'te / ATR yenilendikçe (30dk) yeniden hesaplanır, gerçek dinamik ATR'yi takip eder → backtestle birebir örtüşür.
  2. **Native Binance trailing** (`trailingDelta`, `_place_trailing_delta_order`): TP1 anında BİR KERE kuruluyor, mesafe (%1.80-1.84 bips aralığı) Binance sunucusunda hiç güncellenmiyor ("kur, unut" — bot dokunmuyor). Avantajı: bot çökse/tick kaybetse bile emir ayakta kalır. Dezavantajı: backtestteki gibi ATR her bar'da yenilenmiyor, pozisyon boyunca o ilk anki mesafede sabit kalıyor → zamanla backtestten sapabilir.
- **Kullanıcının net tercihi:** "%1.84 sabit değil, hep ATR×0.6 kullan" — 180-184 bips clamp'i sadece o anki ATR'nin yaklaşık karşılığı olsun diye konmuştu, kalıcı sabit olarak kastedilmemişti.
- **Bekleyen karar (henüz uygulanmadı):** Native trailing'in bu "sabit kalma" sorunu nasıl çözülecek?
  - **Seçenek 1 (basit):** 180-184 bips clamp'i kaldır, native'e girilen mesafeyi o anki gerçek ATR×0.6 yüzdesi yap — ama yine de TP1 anındaki tek seferlik ATR'de sabit kalır, sonrasında güncellenmez.
  - **Seçenek 2 (tam çözüm):** Native trailing emrini de ATR değiştikçe periyodik olarak iptal edip yeni mesafeyle yeniden kur (ATR cancel-replace'in native'e uygulanmış hali). Backtestle tam örtüşür ama native'in "kur unut" avantajını azaltır (daha sık emir iptal/yeniden kurma).
  - Sorun tekrar gündeme gelirse (örn. bir pozisyon backtestten belirgin saparsa) buraya bakılacak — kullanıcı: "md'ye kayıt yapmıştık sorun oldu mesele neydi derim sen de konuyu anlarsın."
