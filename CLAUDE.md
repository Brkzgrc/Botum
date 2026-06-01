# Botum — Proje Notları

## Kritik Kurallar

- **SMC.py'ye kesinlikle dokunma** — kullanıcı açıkça söylemedikçe
- **Onay almadan kod yazma** — her implementasyon öncesi onay gerekli
- **portfolio_tracker_PASİF.py** — sadece referans, üretim kodu değil
- **claude_analyzer.py** — sadece referans, üretim kodu değil (PASİF)

## Geliştirme Branch

`claude/github-bot-file-changes-5u6V5` — her değişiklik buraya + main'e push

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

PUMP_PROBABILITY → sinyal → Telegram (PUMP_PROBABILITY_TOKEN)
```

## Environment Variables (Render — bot.py servisi)

| Değişken | Açıklama |
|---|---|
| BINANCE_API_KEY | Binance API |
| BINANCE_API_SECRET | Binance Secret |
| TELEGRAM_TOKEN | Ana sinyal botu |
| TELEGRAM_CHAT_ID | Kullanıcı chat ID (tüm botlar ortak) |
| PUMP_PROBABILITY_TOKEN | @PUMP_PROBABILITY_BOT tokeni |
| ANALYZER_TELEGRAM_TOKEN | @CLAUDE_ANALYZR_BOT tokeni |
| ANTHROPIC_API_KEY | Claude API |
| PORTFOLIO_URL | Portfolio tracker URL |
| PORTFOLIO_TOKEN | Portfolio auth token |

## Aktif Sistemler (bot.py)

1. **PANİK PUMP** — Kapitülasyon mean reversion | Stop -3% | TP +5/10/15% | WR ~%84
2. **T24** — DEVRE DIŞI
3. **ORTA VADE T72** — Stop -5% | TP +10% | WR %54
4. **UZUN VADE T168** — Stop -8% | TP +25% | WR %40
5. **PUMP PROBABILITY** — BB sıkışma + ADX(7) + OBV + Direnç kırılımı | Stop ~-5% | TP +8/15/25%

## Bekleyen Fikirler (İleride Değerlendir)

- **Claude Tarama Kanalı** — Bot sinyallerinden bağımsız olarak Claude'un kendi coin taraması yapacağı ayrı bir Telegram kanalı/botu. Önce bot sinyallerinin 2-3 aylık gerçek verisi biriksin, sonra karşılaştırmalı değerlendirme yapılsın. Haziran 2026'dan itibaren veri toplanıyor.
- **PUMP PROBABILITY Backtest** — Script hazır (bilgisayarda çalıştırılacak): filtresiz vs BTC filtreli karşılaştırma, Jan-May 2026 dönemi.

## Claude Analyzer (claude_analyzer.py)

- Bağımsız modül — bot.py'nin iç yapısına bağlı değil
- Her sinyali değerlendirip @CLAUDE_ANALYZR_BOT üzerinden karar gönderir
- Kendi TF verisi çeker (Binance REST), F&G, dominans, portfolio bağlamı dahil
- SMC dahil her sistemden `process_and_send(signal_dict)` ile çağrılabilir
