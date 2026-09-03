# GPT Sonnet Analyzer

Bu modül Anton Scanner'ın mevcut manuel Telegram analizini bozmadan ikinci bir niteliksel analiz yolu ekler.

## Konum

`anton_scanner/gpt_sonnet_analyzer/`

Bu konum özellikle Anton Scanner Render servisinin Included Paths kapsamına girmesi ve özelliğin Anton Scanner'a ait olduğunun repoda açıkça görünmesi için kullanılır.

## Telegram kullanımı

- `ZEC` → mevcut Anton manuel analizi aynen devam eder.
- `ZEC GPT` → GPT Sonnet Analyzer çalışır; 1D + 4H + 1H birlikte yorumlanır.
- `zec gpt`, `ZECUSDT GPT`, `ZEC/USDT GPT` ve `#ZEC GPT` kabul edilir.
- Yanıt mevcut Anton Telegram topic/thread'ine gönderilir.
- Aynı Telegram bot token'i için ikinci bir `getUpdates` poller başlatılmaz.

## Analiz verileri

Binance Spot kapanmış mumlarından 1D, 4H ve 1H için RSI(14), StochRSI + MA-StochRSI, MACD(12,26,9), KDJ, Williams %R, OBV, EMA20/50/200, ATR14, Bollinger(20,2), hacim, yakın swing destek/direnç ve son mum davranışı hesaplanır. Altcoinlerde BTC'nin aynı üç zaman dilimindeki bağlamı da eklenir.

Sonnet'e mekanik puanlama yaptırılmaz. Prompt 1D ana yapı/reset → 4H dönüş/trigger → 1H timing/soğuma/yeniden tetik ilişkisini kurdurur ve momentumun fiyat düşerek mi yoksa yatay konsolidasyonla mı boşaldığını ayrıca değerlendirir.

## Model

Varsayılan model `claude-sonnet-5`'tir. `MARKET_ANALYST_MODEL` ile değiştirilebilir. API anahtarı mevcut `ANTHROPIC_API_KEY` değişkeninden okunur.

## Uzun yanıtlar

Model `max_tokens` nedeniyle kesilirse continuation çağrıları otomatik yapılır. Tam metin birleştirildikten sonra Telegram sınırına göre bölünür. Tüm parçalar aynı `message_thread_id` ile gönderilir.

## Production entegrasyonu

`anton_integration.py`, mevcut manuel poller'ın config/yetkilendirme değerlerini kullanır. Root `sitecustomize.py` yalnızca target adı `_manual_analyzer_poll_loop` olan thread'i GPT-aware eşdeğeriyle sarar. Entegrasyon yüklenemezse fail-open davranır ve eski Anton poller'ı aynen çalışır.

## Test

Testler normal sembol ile GPT route ayrımını, büyük/küçük harf varyasyonlarını, Telegram thread ID korunmasını, uzun yanıt bölme bütünlüğünü, indikatör snapshot şemasını ve tek-sembol Sonnet helper'ını kapsar.
