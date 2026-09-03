import unittest
from unittest.mock import patch

from anton_scanner.gpt_sonnet_analyzer.anton_integration import parse_gpt_symbol, _send


class AntonIntegrationTests(unittest.TestCase):
    def test_gpt_parser_accepts_expected_forms(self):
        self.assertEqual(parse_gpt_symbol("ZEC GPT"), "ZECUSDT")
        self.assertEqual(parse_gpt_symbol("zec gpt"), "ZECUSDT")
        self.assertEqual(parse_gpt_symbol("Zec GpT"), "ZECUSDT")
        self.assertEqual(parse_gpt_symbol("ZECUSDT GPT"), "ZECUSDT")
        self.assertEqual(parse_gpt_symbol("ZEC/USDT GPT"), "ZECUSDT")
        self.assertEqual(parse_gpt_symbol("#ZEC GPT"), "ZECUSDT")

    def test_plain_symbol_does_not_match_gpt_route(self):
        self.assertIsNone(parse_gpt_symbol("ZEC"))
        self.assertIsNone(parse_gpt_symbol("ZECUSDT"))
        self.assertIsNone(parse_gpt_symbol("GPT"))
        self.assertIsNone(parse_gpt_symbol("ZEC GPT NOW"))

    @patch("anton_scanner.gpt_sonnet_analyzer.anton_integration.requests.post")
    def test_send_preserves_message_thread_id(self, post):
        post.return_value.ok = True
        _send("token", "-1001", 38, "test")
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["chat_id"], "-1001")
        self.assertEqual(payload["message_thread_id"], 38)
        self.assertEqual(payload["text"], "test")


if __name__ == "__main__":
    unittest.main()
