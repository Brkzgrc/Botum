# Anton MTF Telegram Analyst

Existing Anton production code is intentionally untouched. This is a standalone,
additive analyst that turns a Telegram symbol such as `ZEC` into a 1D + 4H + 1H
market interpretation.

## What it reads

For the requested Binance Spot USDT pair, and BTC as market context, it uses only
fully closed candles and computes:

- RSI(14)
- StochRSI(14) + 3-period MA-StochRSI
- MACD(12,26,9)
- KDJ(9,3,3-style smoothing)
- Williams %R(14)
- OBV
- EMA20 / EMA50 / EMA200
- ATR(14)
- Bollinger(20,2)
- volume vs 20-bar average
- recent swing support / resistance
- compact candle body/wick and 3-bar price-behavior context

Claude Sonnet receives the structured snapshot and is instructed to reason in
1D -> 4H -> 1H order. It is specifically asked to distinguish momentum cooling
through price decline from cooling through sideways consolidation.

## No-truncation behavior

The model is called with a generous output budget. If Anthropic returns
`stop_reason=max_tokens`, the bot automatically requests a continuation and
stitches it to the first response. Telegram's per-message size limit is handled
separately by splitting the completed analysis at paragraph/sentence boundaries.
So a long analysis is delivered as `(1/N)`, `(2/N)`, etc., instead of being cut.

## Required environment variables

- `ANTHROPIC_API_KEY`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_ALLOWED_CHAT_IDS` — comma-separated Telegram chat IDs. The bot refuses
  to start without this allow-list to avoid exposing paid AI calls publicly.

Optional:

- `MARKET_ANALYST_MODEL` (default `claude-sonnet-5`)
- `MARKET_ANALYST_MAX_TOKENS` (default `7000`)

## Test before Telegram

```bash
pip install -r requirements.txt
python market_analyst_bot.py --symbol ZEC --snapshot-only
python market_analyst_bot.py --symbol ZEC
```

## Telegram mode

```bash
python market_analyst_bot.py
```

Send `ZEC` (or `/analiz ZEC`) to the bot.

## Deployment isolation

Deploy this directory as a separate Render Background Worker. Do not point the
existing Anton web service at this file. This keeps the current Anton engine,
web app, database and history behavior unchanged and makes rollback simply a
matter of stopping/removing the separate worker.
