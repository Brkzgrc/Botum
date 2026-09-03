# Anton Çoklu Zaman Dilimi Market Analyst

Bu modül Anton'un mevcut manuel Telegram analizini bozmadan yeni bir niteliksel analiz yolu ekler.

## Telegram kullanımı

- `ZEC` → mevcut Anton manuel analizi aynen devam eder.
- `ZEC GPT` → yeni Sonnet tabanlı 1D + 4H + 1H analizini çalıştırır.
- `zec gpt`, `ZECUSDT GPT`, `ZEC/USDT GPT` de kabul edilir.
- Yanıt mevcut Anton Telegram topic/thread'ine gönderilir.
- Aynı Telegram bot token'i için ikinci bir `getUpdates` poller başlatılmaz.

## Yeni GPT analizinin verileri

Binance Spot kapanmış mumlarından 1D, 4H ve 1H için RSI(14), StochRSI + MA-StochRSI, MACD(12,26,9), KDJ, Williams %R, OBV, EMA20/50/200, ATR14, Bollinger(20,2), hacim, yakın swing destek/direnç ve son mum davranışı hesaplanır. Altcoinlerde BTC'nin aynı üç zaman dilimindeki bağlamı da eklenir.

Sonnet'e ham eşik puanlaması yaptırılmaz. Prompt özellikle şu ilişkiyi kurdurur: 1D ana yapı/reset → 4H dönüş/trigger → 1H timing/soğuma/yeniden tetik. Momentumun fiyat düşerek mi yoksa fiyat yatay kalırken mi boşaldığı ayrıca değerlendirilir.

## Model

Varsayılan model `claude-sonnet-5`'tir. `MARKET_ANALYST_MODEL` ile değiştirilebilir. API anahtarı mevcut `ANTHROPIC_API_KEY` değişkeninden okunur.

## Uzun yanıtlar

Model `max_tokens` nedeniyle kesilirse continuation çağrıları otomatik yapılır. Tam metin birleştirildikten sonra Telegram sınırına göre bölünür. Tüm parçalar aynı `message_thread_id` ile gönderilir.

## Production entegrasyonu

`anton_integration.py`, mevcut manuel poller'ın config/yetkilendirme değerlerini kullanır. `sitecustomize.py` yalnızca target adı tam olarak `_manual_analyzer_poll_loop` olan thread'i GPT-aware eşdeğeriyle sarar. Entegrasyon import edilemezse fail-open davranır ve eski Anton poller'ı aynen çalışır.

Bu nedenle merge sonrası normal portfolio-tracker servisi yeniden deploy olduğunda ek bir Render worker gerekmeksizin `COIN GPT` yolu aktif olur. Mevcut `COIN` yolu korunur.

## Test

Yerel testler şu davranışları kapsar: normal sembol ile GPT route ayrımı, büyük/küçük harf varyasyonları, Telegram thread ID korunması, uzun yanıt bölme bütünlüğü, indikatör snapshot şeması ve tek-sembol Sonnet helper'ı.
