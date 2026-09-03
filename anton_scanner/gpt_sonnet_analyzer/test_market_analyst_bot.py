import unittest
from unittest.mock import patch

from anton_scanner.gpt_sonnet_analyzer.market_analyst_bot import (
    Candle,
    build_timeframe_snapshot,
    extract_symbol,
    split_telegram,
    analyze_symbol,
)


class AnalystTests(unittest.TestCase):
    def test_extract_symbol(self):
        self.assertEqual(extract_symbol("ZEC"), "ZEC")
        self.assertEqual(extract_symbol("zecusdt"), "ZEC")
        self.assertEqual(extract_symbol("/analiz ETH"), "ETH")
        self.assertIsNone(extract_symbol("hello world"))

    def test_split_preserves_text(self):
        text = ("Paragraf bir.\n\n" + "x" * 2000 + "\n\n" + "Paragraf iki. " + "y" * 2000)
        parts = split_telegram(text, 1200)
        rebuilt = "\n\n".join(p.split("\n", 1)[1] if p.startswith("(") else p for p in parts)
        self.assertEqual("".join(rebuilt.split()), "".join(text.split()))
        self.assertTrue(all(len(p) < 1400 for p in parts))

    def test_snapshot_has_expected_sections(self):
        candles = []
        price = 100.0
        for i in range(260):
            close = price + (0.3 if i % 3 else -0.1)
            candles.append(Candle(i, price, max(price, close) + 1, min(price, close) - 1, close, 1000 + i, i + 1))
            price = close
        snap = build_timeframe_snapshot(candles)
        self.assertIn("momentum", snap)
        self.assertIn("trend_structure", snap)
        self.assertIn("volatility_volume", snap)
        self.assertIn("short_behavior", snap)
        self.assertIn("stochrsi", snap["momentum"])

    @patch("anton_scanner.gpt_sonnet_analyzer.market_analyst_bot.analyze_with_claude", return_value="ok")
    @patch("anton_scanner.gpt_sonnet_analyzer.market_analyst_bot.build_market_snapshot", return_value={"symbol": "ZEC"})
    def test_analyze_symbol(self, build, analyze):
        self.assertEqual(analyze_symbol("ZEC"), "ok")
        build.assert_called_once_with("ZEC")
        analyze.assert_called_once_with({"symbol": "ZEC"})


if __name__ == "__main__":
    unittest.main()
