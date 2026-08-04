import unittest

import stock_dynamic_monitor as monitor


class FeishuCardTests(unittest.TestCase):
    def test_watchlist_card_contains_core_fields(self):
        items = [
            {
                "name": "三花智控",
                "code": "002050",
                "price": 38.30,
                "ma120": 45.91,
                "ma120_pct": -16.58,
                "buy_line": 40.41,
                "sell_line": 51.42,
                "signal": "hold",
            }
        ]

        payload = monitor.render_watchlist_card(items, "2026-08-04")

        self.assertEqual(payload["msg_type"], "interactive")
        card = payload["card"]
        self.assertIn("关注池", card["header"]["title"]["content"])
        content = "\n".join(
            element.get("content", "")
            for element in card["elements"]
            if element.get("tag") == "markdown"
        )
        self.assertIn("三花智控", content)
        self.assertIn("现价 38.30", content)
        self.assertIn("MA120 45.91", content)
        self.assertIn("买入线 40.41", content)
        self.assertIn("卖出线 51.42", content)

    def test_card_marks_buy_zone_green(self):
        items = [
            {
                "name": "中国铝业",
                "code": "601600",
                "price": 9.48,
                "ma120": 11.26,
                "ma120_pct": -15.77,
                "buy_line": 9.90,
                "sell_line": 12.61,
                "signal": "hold",
            }
        ]

        payload = monitor.render_watchlist_card(items, "2026-08-04")
        card = payload["card"]
        content = "\n".join(
            element.get("content", "")
            for element in card["elements"]
            if element.get("tag") == "markdown"
        )

        self.assertEqual(card["header"]["template"], "green")
        self.assertIn("[低于买入线]", content)

    def test_card_marks_sell_zone_red(self):
        items = [
            {
                "name": "埃斯顿",
                "code": "002747",
                "price": 31.20,
                "ma120": 27.60,
                "ma120_pct": 13.04,
                "buy_line": 24.29,
                "sell_line": 30.91,
                "signal": "hold",
            }
        ]

        payload = monitor.render_watchlist_card(items, "2026-08-04")
        card = payload["card"]
        content = "\n".join(
            element.get("content", "")
            for element in card["elements"]
            if element.get("tag") == "markdown"
        )

        self.assertEqual(card["header"]["template"], "red")
        self.assertIn("[高于卖出线]", content)


if __name__ == "__main__":
    unittest.main()
