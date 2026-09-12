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

## VOL_RATIO_MIN Karar Geçmişi (2026-07-19)

19 Temmuz karar zinciri — ileride aynı karışıklık tekrar yaşanmasın diye:

1. **05:04 — TP1-üstü red kaldırıldı** (`e10908b`). O anda `VOL_RATIO_MIN=7.5` iken sonuç (eski 24H-cap backtest motoruyla, `smc_relax_tp1_reject.py`): Final ~$1.361M, Getiri +27.127%, MaxDD -%11.46.
2. **06:08 — VOL_RATIO_MIN 7.5'ten 8.5'e yükseltildi** (`92c87ee`, `smc_rr_vol_sweep.py --mode vol` backtest'ine dayanarak). Aynı (eski 24H-cap) motorla sonuç: Final ~$1.249M, Getiri +24.875%, MaxDD -%9.19.

**Gerekçe:** 8.5 seçimi daha düşük getiri karşılığında daha iyi MaxDD verdi (-%11.46 → -%9.19, aralığın içinde gerçek bir minimum). Gerçek canlı sonuçların backtest'ten hep düşük çıkma eğilimi (WR farkı) göz önüne alınınca, riski azaltmak tercih edildi. **Bilinçli bir risk-getiri takasıdır** — 7.5 daha kârlıydı ama daha riskliydi. `VOL_RATIO_MIN=8.5` SMC.py'de hâlâ aktif, güncel canlı değer.

**Nüans (2026-07-21'de bulundu):** Yukarıdaki her iki rakam da (~$1.361M ve ~$1.249M) eski, metodoloji hatalı 24H-cap backtest motoruna ait (bkz. aşağıdaki "Kademeli ATR Trailing" bölümü). Live-parity motoruyla (TP1-sonrası süresiz trailing) `VOL_RATIO_MIN=8.5` için güncel referans: Final ~$1.257M, Getiri +25.041%, MaxDD -%9.19. `VOL_RATIO_MIN=7.5` live-parity motoruyla yeniden koşulmadı — eğer tekrar karşılaştırma gerekirse önce o da live-parity motoruyla koşulmalı, doğrudan $1.257M ile $1.361M kıyaslanmamalı (farklı motorlar).

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

**Takip hipotezi (henüz test edilmedi) — "post-exit re-entry":** Kademeli trailing reddedildi ama kullanıcının orijinal gözlemi ("+19'a çıktı, trail ile +14'te kapandı, coin sonra +50 yaptı") hâlâ açıklanmamış olabilir — backtest'in aradığı "aynı pozisyon trail yüzünden erken çıktı, sonra AYNI trade penceresinde +50'ye gitti mi" sorusuna cevap 0 çıktı, ama gerçek canlı gözlem farklı bir şey olabilir: pozisyon kârda kapandıktan SONRA, ayrı bir zaman diliminde coin ikinci bir dalga yapıyor olabilir. Bu trailing genişliğiyle değil, kapanmış pozisyonu bir süre "hot watchlist"te tutup ikinci dalga için ayrı bir continuation sinyaliyle yeniden girme (post-exit re-entry) fikriyle ilgili — mevcut çıkışı bozmadan, ayrı ve kontrollü bir modül. Backtest'i yapılmadan canlıya eklenmeyecek (kolayca fazla işlem/chop üretebilir).

**Forward test'te toplanacak veri (henüz canlıya eklenmedi):** SMC pozisyonu `trail_stop` ile kârda kapandığında, aynı coin için kapanıştan sonraki 24h/48h/72h max high ayrıca not edilecek — amaç yukarıdaki re-entry hipotezini tek örnekle değil, biriken gerçek veriyle değerlendirmek. Şu an canlı sistemde hiçbir değişiklik yapılmadı, bu sadece ileride bakılacak bir veri toplama hedefi.

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

**Bulunan hata (2026-07-22, düzeltildi):** `_portfolio_context()`'teki (2026-07-20) aynı sınıf hata, `portfolio_tracker.py`'nin KENDİ dashboard istatistik/gösterim fonksiyonlarında (`calc_performance()`, `status_badge()`) tekrar bulundu — trading bot'un `/api/position-closed` webhook'undan gelen `status="closed"` + `outcome`/`close_reason`/`close_pct` şeması hiç tanınmıyordu, pozisyon kapanıp "closed" toplamına giriyordu ama Win/Loss/Expired sayaçlarına hiç yansımıyordu, kapalı işlem tablosunda da düz gri "CLOSED" rozeti görünüyordu (eskiden renkli WIN/LOSS/EXPIRED yazardı). Ortak bir `classify_signal_outcome(sig)` fonksiyonuna çıkarılıp her iki yerde de kullanıldı. Tasarım: `kind` (win/loss/manual) ve `is_expired` (süre dolarak mı kapandı) birbirinden bağımsız — bir işlem hem "win" hem "expired sebebiyle kapandı" olabilir (kullanıcı talebi: kazançla kapanan expire işlemi de Win sayılmalı). **Expired, Win/Loss'tan ayrı bir sebep kırılımıdır; toplam closed hesabına ayrıca eklenmez** (Win+Loss=closed, Expired bunların bir alt-kümesini işaretleyen üst üste binen bir etiket). Codex ile bağımsız incelemeyle doğrulandı, blocking bulgu çıkmadı. Trading-bot/SMC.py/trade_state.json'a dokunulmadı, sadece dashboard'un salt-okunur istatistik/gösterim katmanı.

## "Dipten Güçlü Yükseliş" Sinyal Araştırması (2026-07-30, ÖLÇÜLDÜ VE REDDEDİLDİ)

**Soru:** Kullanıcının TradingView'de gözlemlediği 5 örnek (COTI, DEXE, XNO, RIF, VANRY —
dipten sert yükselişe geçen coinler) ortak bir gösterge deseni mi taşıyor, bu bağımsız bir
kâr amaçlı sinyal sistemine dönüştürülebilir mi? Hedef: $5.000 sermayeyle ayda $500-1.000
(~%10-20/ay) getiri.

**Yöntem:** Local session (MCP-Jackson) — Binance SPOT 1H verisiyle (2026-05-01 → 2026-07-30,
90 gün), 6 coin evreni (L1, L1+L2, TOP10, TOP50, ALL_USDT, NON_L1L2) ve 8 sinyal tanımı
(osilatör ailesi: RSI<20, RSI+WR, StochRSI, BB Reversal; yapısal/hacim ailesi: SwingLow+Vol,
OBV Diverjans, Morning Star, RSI+SwingLow) üzerinde toplam 192 kombinasyon test edildi.
Referans 5 coin istatistiklerden dışlandı (overfitting kontrolü), sinyaller önceden
sabitlendi, entry = sinyal barından sonraki barın açılışı (look-ahead yok).

**1. tur (v2) — sahte pozitif:** Osilatör sinyalleri her evrende negatif çıktı. Tek pozitif
görünen sonuç, L1 evreni × S4_SwingLow+Vol sinyali, 3%/8% stop/target ile n=132, WR=%46.2,
+$57/ay idi. Doğrulama turunda bu sonucun bir veri indirme bug'ına dayandığı bulundu:
Binance SPOT API tek çağrıda max 1000 bar döndürüyor, script `limit=1500` bekleyip `<1500`
ise durduğundan gerçekte ~41 günlük veri (90 gün sanılarak) indirmiş, sonuçlar buna göre
2.1× hatalı ölçeklenmişti.

**2. tur — düzeltilmiş veriyle train/holdout:** Doğru 90 günlük veriyle 60/30 gün train/holdout
ayrımı yapıldı. L1×S4_SwingLow+Vol, denenen 4 param kombinasyonunun TAMAMINDA hem train hem
holdout'ta negatif expectancy verdi (holdout en iyisi: $/ay=-628, E_net=-0.004). Diğer 7
sinyal/6 evren kombinasyonu (zaten negatif çıkmıştı) düzeltilmiş veriyle yeniden koşulmadı —
düşük öncelik, negatiften pozitife dönme ihtimali düşük.

**Karar:** İki sinyal ailesi de (osilatör bazlı aşırı-satım dönüşü, yapısal swing-low+hacim
kırılımı) 6 evrende, düzgün train/holdout metodolojisiyle rigor'la test edilip reddedildi.
Canlı sisteme hiçbir değişiklik yapılmadı — bu tamamen ayrı, bağımsız bir araştırmaydı, SMC.py/
position_monitor.py/trading_engine.py'a dokunulmadı. Ham JSON çıktıları local'de kaldı, repoya
gitmedi (proje kuralı). Tekrar gündeme gelirse: rastgele yeni gösterge denemek yerine gerçekten
farklı bir bilgi kaynağından (orderbook/likidite, piyasa geneli korelasyon/rejim, ya da mevcut
CHoCH sisteminin kendi sinyallerini filtreleme/geliştirme) somut, gerekçeli tek bir yeni
hipotezle başlanmalı — 192 kombinasyonluk "şans eseri pozitif çıkan var mı" taramaları,
tam da bu turda yakalanan sahte pozitif riskini taşıyor.

## spot_opportunity Sistem Teşhisi (2026-09-11, ÖLÇÜLDÜ — sistem kâr etmiyor)

**Soru:** Canlı `spot_opportunity_scanner` sistemi neden kazanmıyor, kayıplar ve expired'lar
nasıl azaltılır?

**Veri:** 945 backtest işlemi (Ocak–Eylül 2026, iki ayrı evren: mcap-119 ve tüm-spot-469).
Kalibrasyon: simülasyon orijinal backtest sonucunu %0.9 sapmayla yeniden üretiyor.

### Bulgu 1 — Sistem para kaybediyor (panelin kendi hesabıyla bile)

2500$ sırayla (tek lot, pozisyon açıkken yeni işlem alınmaz):

| dönem | panel hesabı | emir dolsa (%0.19 BNB'li) |
|---|---|---|
| Temmuz–Eylül (tüm evren) | 2390$ | 2373$ |
| **Ocak–Haziran** | **1266$** | **1240$** |
| Temmuz–Eylül (mcap) | 1983$ | 1967$ |

Komisyon/dolum tartışması sonucu birkaç puan oynatıyor; zarar %5–50 arası.
**En cömert hesapla bile (hiç kayma yok, tam seviyeden dolum) üç dönemin ikisinde ağır zarar.**

### Bulgu 2 — Sistem GEÇ giriyor (asıl sebep)

945 işlemin sinyal anındaki durumu:

| | 4 saat | günlük | 1 saat | 15 dakika |
|---|---|---|---|---|
| StochRSI medyanı | **73.0** | **85.5** | 42.6 | **69.0** |
| MA20'nin **altında** olan işlem | **%0** | **%0** | %8 | %12 |
| MA20 eğimi yukarı | **%100** | %95 | — | — |
| MA20'ye uzaklık (medyan) | +%5.77 | +%12.77 | +%1.99 | +%0.85 |
| Son 24 mumun getirisi (medyan) | **+%14.01** | — | +%5.57 | +%1.37 |

Yani sistem, coin 4H'de zaten ~%14 yükselmiş ve MA20'nin %5.77 üstündeyken alıyor.
**945 işlemin hiçbirinde 4H'de MA20'nin altında alım yok.** Bu bir momentum/kırılım sistemi,
geri çekilme alıcısı değil.

### Bulgu 3 — Trend hareketlerinden pay alamıyor

En az 4 işlem yapılan, fiyatı %20'den fazla yükselen **28 coin**:

| | |
|---|---|
| Coinlerin ortalama yükselişi | **+%109** |
| Sistemin bu coinlerden aldığı | **−%1.4** |
| Sistemin coinden iyi olduğu | **0 / 28** |
| Sistemin zarar yazdığı | 15 / 28 |

Örnek: **ZEC 245$ → 1223$ (+%399). Sistem 20 işlem yapıp −%6.17.**
币安人生 +%1098 iken sistem −%23.

Sebebi ölçülü: ortalama TP1 hedefi **+%4.15**, ortalama stop riski **−%6.56**. Yani
%6.56 riske edip %4.15 kazanmaya çalışıyor. Medyan risk/ödül **0.64**; işlemlerin **%82'sinde
hedef, riskten küçük**. Ayrıca işlemlerin **%43'ünde TP1 = tam +%3.50**, yani scanner uygun
direnç bulamayınca kullandığı varsayılan `fiyat×1.035` — grafikte bir yere karşılık gelmiyor.

### Bulgu 4 — Çıkış kuralı ZATEN optimal, değiştirilmemeli

Girişler sabit tutulup **47 farklı çıkış kuralı** 5 dakikalık fiyat yolu üzerinde tam hesaplandı
(`exit_lab.py`, scratchpad). 925 işlemde yüzde puan toplamı:

| kural ailesi | mevcuda göre |
|---|---|
| Breakeven stop (+%0.5 … +%3.0) | **−130 … −479 puan** |
| Stop tavanı (%2 … %8) | −38 … −163 |
| Breakeven + tavan (9 kombinasyon) | −198 … −325 |
| Expire süresi (8s / 12s / 18s / 36s) | −20 … −196 |
| TP1'de kısmi satış (%30 / %50) | −82 … −136 |
| Trailing genişliği (1.5 / 2 / 3 / 4 / 5) | −53 … **+35** (zikzak, gürültü) |

**32/33 varyant mevcut kuraldan kötü.** Tek "iyi" olan TRAIL_3.0 işlem başına +%0.038 —
ve trail dizisi zikzak (1.5 iyi, 2.0 kötü, 3.0 iyi, 4.0 kötü), desen yok.

**Breakeven stop neden çöküyor:** giriş zaten `stop = destek × 0.975` ile desteğin hemen
üstünde. Fiyatın girişe geri gelmesi bu sistemde **normal davranış** (destek testi), bozulma
sinyali değil. Stop'u oraya koymak sistemin kendi mantığına ters.

### Bulgu 5 — Girişte ayırt edici hiçbir şey yok

Denenen ve düşen: revize veto sistemi (3 kural, engellediği 63 adayın 27'si kazanan,
net −140 puan), momentum filtresi (`15m.ret24>1.29`), BTC rejimi, skor, stop mesafesi,
RR, KDJ-J bantları, çoklu zaman dilimi osilatör uyumu, TP1'in uydurma mı gerçek direnç mi
olduğu. **Toplam 20'den fazla kural, hiçbiri üç dönemde birden dayanmadı.**
75 sinyal-anı göstergesi taranmıştı: max AUC 0.586.

**UYARI — aynı taramayı tekrar yapma.** 12+ kural aynı veride denenince en az birinin şans
eseri iyi görünme ihtimali ~%72. Bu projede 2026-07-30'da tam olarak bu tuzağa düşüldü
(192 kombinasyon → sahte pozitif). Yeni bir fikir gelirse: önce yaz ve kilitle, sonra
**dokunulmamış bir dönemde tek koşu**, sonucu görünce ayar yapma.

### Karar

- Canlı sisteme **hiçbir değişiklik yapılmadı**. Sadece takip modunda çalışıyor
  (`SMC_MAIN_SOURCES = ("smc-v2",)` — spot sinyalleri trading-bot'a gitmiyor, Binance'e emir
  konulmuyor). **İyi ki öyle** — ölçüm bağlanırsa gerçek para kaybedeceğini söylüyor.
- **Trading-bot bu sinyallere bağlanmamalı.** Bağlanırsa ayrıca çıkış kuralı da değişir
  (`position_monitor` trailing'i `peak − ATR×0.6` kullanır, portfolio_tracker'daki %2.5 değil).
- Kâr için bu sistemi ayarlamak değil, **farklı bir giriş mantığı** gerekiyor (bkz. sonraki bölüm).

### Panel doğruluğu notu (düzeltilmedi, bilinçli)

`portfolio_tracker.py` satır 987–989: trailing `close <= trail_stop` ile tetikleniyor ama
kapanış fiyatı olarak **seviyenin kendisi** kaydediliyor. Bu, o seviyede bekleyen bir Binance
emri varsa doğru; bugün öyle bir emir yok (spot sinyalleri trading-bot'a gitmiyor), sistem
5 dakikada bir bakıp "kapandı" diyor. Ölçüldü: bugünkü haliyle panel **%29–50 iyimser**.
Emir konulursa fark %0.3'e iner. Düzeltme yapılmadı çünkü sistem zaten zararda — rakamın
doğruluğu kararı değiştirmiyor. Trading-bot bağlanmayacaksa düzeltilmeli.

## Gösterge Formülleri — Doğrulanmış Referans (2026-09-11)

Kullanıcının kendi alım yönteminde kullandığı göstergeler, **ZECUSDT 23.08.2026 09:00 (TR)**
anıyla birebir doğrulandı (9 göstergenin 9'u, virgülden sonra 2 hane). İleride yeniden
kurmak gerekirse bu formüller referans alınmalı:

| gösterge | tanım |
|---|---|
| RSI | 14, Wilder yumuşatma |
| StochRSI | 14/14/3/3 — RSI'ın 14'lük stokastiği, %K = SMA3, %D = SMA3(%K) |
| KDJ | 9/3/3 — `RSV=(C−LLV9)/(HHV9−LLV9)×100`, `K=⅔K₋₁+⅓RSV`, `D=⅔D₋₁+⅓K`, `J=3K−2D` |
| W%R | 14 — `(HHV14−C)/(HHV14−LLV14)×−100` |
| MACD | 12/26/9 — `dif=EMA12−EMA26`, `dea=EMA9(dif)`, **`hist=dif−dea`** (2× DEĞİL) |

Doğrulama değerleri (ZEC, 23.08.2026 09:00 TR, 1 saatlik):
RSI 48.57 · StochRSI 1.63/0.75 · KDJ 17.60/22.85/7.12 · W%R −84.31 · MACD 9.12/18.53/−9.42

**Not:** Zaman kritik. Aynı gün 08:00 ile 09:00 arasında StochRSI 10.77 → 1.63 değişiyor.
Bir örneği doğrularken önce doğru dakikayı bul (`an_bul.py`, scratchpad).

## Kullanıcının Kendi Alım Yöntemi — Tarama (2026-09-11, DEVAM EDİYOR)

**Sebep:** Kullanıcı aynı coinlerde (örn. ZEC) elle alım satım yapıp kazanırken sistem
kaybediyor. Fark ölçüldü: **sistem geç giriyor** (yukarıdaki Bulgu 2). Kullanıcının kurulumu
945 sistem işleminin sadece **%0.2–2.4'ünde** var — yani mevcut sinyalleri filtreleyerek
test edilemez, sıfırdan sinyal üretmek gerekiyor.

**Kural (kullanıcının 5 gerçek örneğinden çıkarıldı, uydurulmadı):**

| | 4 SAAT | 1 SAAT | 15 DAKİKA |
|---|---|---|---|
| rol | trend güçlü mü | **giriş** | teyit |
| RSI | ≥ 60 | ≤ 55 | ≤ 50 |
| StochRSI | **≤ 15** | **≤ 10** | ≤ 20 |
| KDJ J | — | ≤ 15 | — |
| W%R | — | ≤ −80 | ≤ −70 |
| MACD histogram | — | negatif (kesişme beklenmez) | — |

Mantığı: **üst zaman dilimi trendi güçlü ama StochRSI dipte** — yani RSI mutlak olarak yüksek
(ZEC: 4H RSI 70.34) ama kendi son 14 barlık aralığının en altında (4H StochRSI 2.41).
Geri çekilme, dönüş değil. Alt zaman dilimleri tükenmiş. Klasik "yükselen trendde dip al".

**Araç:** `dip_tarama.py` (scratchpad — repoya girmez, proje kuralı). Mevcut scanner'a
dokunmaz, ayrı bağımsız tarama. Seviye mantığı (`stop = destek×0.975`, `tp1 = en yakın
direnç ≥ +%2.5`) scanner'ın `levels()` fonksiyonuyla birebir aynı; `_swings` ile yan yana
koşturulup doğrulandı. Çıkış canlı kurallarla (stop / TP1 / %2.5 trailing / 24h).

**İlk sonuçlar (en likit 50 parite, saat başı tarama):**

| | 2025 (dokunulmamış veri) | 2026 |
|---|---|---|
| işlem | 33 | 42 |
| işlem başına | **+%1.13** | **+%0.97** |
| 2500$ → | **3397$** | 2496$ |
| en kötü düşüş | **−%15.1** | −%17.4 |

Çıkış kırılımı (75 işlem): trailing +%5.56 (33 işlem) · stop **−%3.92** (24) · expired
**−%0.63** (18). Mevcut sistemde bu rakamlar −%6.25 ve −%2.83.
**Kazançlar aynı boyutta, kayıplar yarı, expired'lar neredeyse sıfır** — iki dönemde de.
Mekanizma net: desteğin dibinden alınca destek yakın, stop yakın, kayıp küçük.

**AÇIK SORULAR — sonuç kesinleşmedi:**
1. **İstatistiksel olarak kanıtlanmadı.** t = 1.56 (75 işlem). Anlamlılık için > 2 gerekir.
2. **Kâr birkaç işlemden geliyor.** Toplam +78 puan; en iyi 5 işlem çıkarılınca −2.2'ye düşüyor.
   Medyan işlem +%0.29.
3. **Örneklem çok küçük.** 21 ayda 75 işlem = ayda 3.5. Sonraki adım: `EVREN_LIMIT = 0`
   (tüm evren, 400+ parite) ile koşmak → ~600 işlem beklentisi. Bu hem t değerini hem
   kâr dağılımını netleştirecek.

**Bu araştırma bitmeden canlı sisteme hiçbir şey eklenmeyecek.**

## Kural Madenciliği — Otomatik Hipotez Üretimi (2026-09-12, DEVAM EDİYOR)

**Sebep (kullanıcının talebi):** "milyon tane varsayım yaratılabilecekken sen kısıtlı bi
alanda dönüp duruyosun... senin sisteminle milyon tane olasılığı araştırabilmek kolayken
bunu yapmıyor ve normal insan gibi aramalar yapıyorsun." Ayrıca: "benim senden beklentim
araştırma motoru gibi davranman, kodlanmış bir dosya gibi değil."

Elle kural yazmak bırakıldı. `kural_madenci.py` (scratchpad, repoya girmez) kuralları
kendisi üretir: her özellik × verinin kendi yüzdeliklerinden eşikler × 1'li/2'li/3'lü
birleşimler → **~1.7 milyon kural**. Eşikler elle verilmez.

### Metodolojik kazanımlar (hepsi ölçülerek bulundu, tekrar keşfedilmesin)

1. **Gürültü tabanı tek başına YETMEZ.** Aynı milyonluk arama, blok-karıştırılmış
   etiketlerle N kez tekrarlanır ve %95'lik taban çıkarılır. Ama sentetik SAF RASTGELE
   veriyle sınandığında madenci arama döneminde **%86 kazanan** kurallar buldu **ve
   tabanı da geçti**. Aynı kurallar dokunulmamış dönemde %52.5'e (taban %49.4) çöktü.
   → Karar iki şarta birden bağlandı: taban aşılacak **VE** dokunulmamış dönemde ≥2σ.
2. **BAR saymak yanıltır — OLAY (fırsat) saymak gerekir.** 1 saatlik barda 8 saatlik hedef
   penceresi varken arka arkaya gelen barlar neredeyse aynı sonucu paylaşır. Ölçüldü:
   bar bazında "145 bar %74.5 (z +6.7)" görünen kural, olay bazında **"20 olay %55.0
   (z +0.7)"** — yani hiçbir şey. Bir kural bar bazında z +4.3 iken olay bazında z +0.2
   çıkabiliyor. Madenci artık doğrudan olay bazında puanlıyor (`olay_maskesi`).
3. **Üst zaman dilimi hizalaması nedensel.** `dip_tarama.py` ve `deger_kesfet.py` Binance'in
   **kapanış** zamanını (`k[6]`) kullanır; `searchsorted(...)-1` o ana kadar KAPANMIŞ son
   üst barı verir, oluşmakta olan bar görülmez. (Sentetik testte sızıntı çıktı ama sızıntı
   test fixture'ındaydı, araçta değil — üst barın zaman damgası yanlış konmuştu.)

### Sonuçlar — BTC 1h, 2021-01 → 2026-07 (repodaki `btc_data/*.pkl`)

Arama 2021-03 → 2024-06, **dokunulmamış doğrulama 2024-06 → 2026-07**.

| hedef | taban oran | başabaş | sonuç |
|---|---|---|---|
| +%2 / −%2 / 8sa | %48.4 | %50.0 | **hiçbir kural onaylanmadı** (en iyi doğrulama z +1.0) |
| +%1 / −%1 / 3sa | %47.5 | %50.0 | **hiçbir kural onaylanmadı** (%48.8) |
| +%2 / −%1 / 8sa | %25.4 | %33.3 | 2 kural geçti ama **komisyon sonrası ≈0** |

**+%2/−%1 ailesi (tek kural değil, 15'inin tamamı):** doğrulama döneminde ortalama
**%31.0**, taban %22.9. Zayıf ama tutarlı, dışarıda da tutan gerçek bir etki. İçeriği:
**oynaklık artarken fiyat son 20 barın üst tarafındayken al.** Ama %31 < gereken %33.3 →
**kâr etmiyor.** En iyi iki kural: %41.2 (34 fırsat) → +%0.05/işlem; %37.2 (43 fırsat) →
−%0.07/işlem.

**Madenci sürekli aynı yere gidiyor** (en iyi 1000 kuralda beklenenin kaç katı):
`1d_ATR_yuzde` 38x · `MACD_dif_arlk` 36x · `1d_bar_genislik` 20x · `dip20_uzaklik_arlk` 17x.
Hepsi "sakin/oynaklığı artan piyasada güçlü barı al" diyor. **İki koşuda da tek bir
dip-alım kuralı ilk sıralara giremedi** — kullanıcının yönteminin tersi.

### Kritik kısıt ve sonraki adım

**BTC ≠ kullanıcının işlem yaptığı yer.** Kullanıcı altcoinlerde kazanıyor (ZEC'de 1 haftada
500$). BTC saatlik en verimli/en tahmin edilemez piyasa; 1.7M kural bir şey bulamaması
altcoinler için bir şey söylemiyor. `dip_tarama.py` de pozitif sonucu **tüm altcoin
evreninde** vermişti, BTC'de değil.

Bu oturumda Binance'e erişim yoktu (ağ politikası 403). **Kullanıcı ortamın ağ politikasına
`api.binance.com` + `data-api.binance.vision` ekledi** (Network access: Custom). Ayar
**yeni oturumlarda** geçerli.

**SONRAKİ OTURUMDA YAPILACAK:** `kural_madenci.py` + `_arastir.py` (kullanıcıda dosya olarak
var) ile aynı madenciliği **altcoin evreninde** koştur — en likit 30-50 parite, 15m/1h giriş,
1h+4h destek, 2023-2026, son %30 dokunulmamış. Metodoloji aynen korunacak: olay bazlı
puanlama + blok permütasyon tabanı + dokunulmamış dönem onayı.

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

### Emir Akışı — Sinyal Geldiğinde (ESKİ TASARIM — 2026-07-11'de değişti, aşağıya bak)
1. `trading_engine.py` sinyali alır, pozisyon büyüklüğünü hesaplar
2. **İki emir aynı anda** Binance'e gönderilir:
   - Market/limit buy → coin alınır
   - Stop-loss sell emri → Binance'te bekletilir (hard SL)
3. `trade_state.json`'a yazılır: symbol, miktar, giriş, stop, TP1, peak

### Emir Akışı — Güncel Durum (2026-07-11, commit `d67dfd7`)

**Neden değişti:** Yukarıdaki eski tasarımda sinyal gelir gelmez bir slot (5 eş zamanlı pozisyon limitinden biri) hemen tüketiliyordu — fiyat henüz CHoCH seviyesine hiç yaklaşmamış olsa bile. Bu, retest'i asla gerçekleşmeyecek zayıf bir sinyalin slotu haftalarca bloke edip daha iyi bir sinyalin reddedilmesine yol açabiliyordu. Çözüm: emir açma işlemini fiyat gerçekten yaklaşana kadar ertelemek.

**Güncel 3 aşamalı akış:**
1. **`monitoring`** — SMC sinyali `portfolio_tracker`'ın `/api/signal`'ına gelir, kaynağı (`source`) `SMC_MAIN_SOURCES` içindeyse `_forward_to_trading_bot()` arka planda trading-bot'un `/signal`'ına iletir → `trading_engine.execute(signal)` çalışır ama **Binance'e HENÜZ hiçbir emir göndermez** — sadece state'e `status: "monitoring"` yazar (symbol, limit_price=CHoCH+1tick, trigger_price=CHoCH+3tick, stop, tp1, tp2). 48H içinde fiyat trigger'a gelmezse iptal olur (`PENDING_EXPIRE_H`).
2. **`pending`** — `position_monitor.py`'nin `_check_monitoring_entries()` fonksiyonu fiyatı izler; fiyat `trigger_price`'a (CHoCH+3tick) gelince `_place_monitoring_order()` çağrılır: o an slot müsaitse gerçek LIMIT BUY emri Binance'e gönderilir (CHoCH+1tick'ten), müsait değilse sinyal miss olur. Bu emrin de kendi 1 saatlik süresi var (`PENDING_ORDER_EXPIRE_H`).
3. **`active`** — limit emir retest ile dolunca (fiyat gerçekten o seviyeye dönüp emri doldurur) pozisyon aktive olur: stop-loss emri Binance'e konur, `position_monitor.py` websocket/1m-kline takibini (`_process_tick`) başlatır.
4. **Çıkış** — stop, TP1→trailing geçişi, trailing takibi ve nihai satış, hepsi tamamen `position_monitor.py` içinde yönetilir (bkz. aşağıdaki "Fiyat Takibi" bölümleri, hâlâ geçerli).

**Not:** "Entegrasyon noktası ... Henüz bağlı değil" notu (bu bölümün altında, "Yazılan Dosyalar" kısmında) artık **geçersiz** — sistem canlı ve bağlı, `portfolio_tracker.py`'nin `_forward_to_trading_bot()` fonksiyonu üzerinden SMC sinyalleri otomatik trading-bot'a iletiliyor.

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
| `trading_engine.py` | Sinyali `monitoring` statüsüyle state'e yazar — Binance emri BURADA açılmıyor (bkz. yukarıdaki "Emir Akışı — Güncel Durum") |
| `position_monitor.py` | Fiyat trigger'a gelince LIMIT BUY açar, retest/stop/trailing/kapanışın TAMAMINI yönetir |
| `trade_state.json` | Açık pozisyonların kalıcı state dosyası |

**Entegrasyon durumu (güncel):** SMC.py'nin sinyalleri, `portfolio_tracker.py`'nin `/api/signal` endpoint'i üzerinden (`source in SMC_MAIN_SOURCES` ise) `_forward_to_trading_bot()` ile otomatik trading-bot'a iletiliyor → `trading_engine.execute(signal)` çağrılıyor. Sistem **bağlı ve canlı** — aşağıdaki eski not artık geçerli değil: ~~"Henüz bağlı değil — SMC.py onayı bekleniyor."~~ (`claude_analyzer.py`'nin `process_and_send()` → `trading_engine.execute()` zinciri farklı bir konudur, bu zaten aktif olan portfolio_tracker relay yolunu değiştirmez.)

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
