import unittest
from market_analyst_bot import Candle, build_timeframe_snapshot, extract_symbol, split_telegram


class AnalystTests(unittest.TestCase):
    def test_extract_symbol(self):
        self.assertEqual(extract_symbol('zec'), 'ZEC')
        self.assertEqual(extract_symbol('/analiz ZEC'), 'ZEC')
        self.assertEqual(extract_symbol('ZECUSDT'), 'ZEC')
        self.assertIsNone(extract_symbol('/help'))

    def test_split_preserves_text(self):
        text = ('Paragraf bir. ' * 300) + '\n\n' + ('Paragraf iki. ' * 300)
        parts = split_telegram(text, 500)
        self.assertGreater(len(parts), 1)
        stripped = [p.split('\n', 1)[1] for p in parts]
        joined = ' '.join(stripped).replace('\n\n', ' ').replace('\n', ' ')
        original = text.replace('\n\n', ' ').replace('\n', ' ')
        self.assertEqual(' '.join(joined.split()), ' '.join(original.split()))

    def test_snapshot_has_expected_sections(self):
        candles = []
        price = 100.0
        for i in range(260):
            close = price * (1 + 0.001 * ((i % 7) - 2))
            candles.append(Candle(i*1000, price, max(price, close)*1.01,
                                  min(price, close)*0.99, close,
                                  1000 + i*3, i*1000+999))
            price = close
        snap = build_timeframe_snapshot(candles)
        self.assertIn('momentum', snap)
        self.assertIn('stochrsi', snap['momentum'])
        self.assertIn('ma_stochrsi', snap['momentum'])
        self.assertIn('trend_structure', snap)
        self.assertIn('short_behavior', snap)


if __name__ == '__main__':
    unittest.main()
