import unittest

import stock_dynamic_monitor as monitor


def _table_element(payload):
    return next(
        element
        for element in payload["card"]["elements"]
        if element.get("tag") == "table"
    )


def _markdown_content(payload):
    return "\n".join(
        element.get("content", "")
        for element in payload["card"]["elements"]
        if element.get("tag") == "markdown"
    )


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
        table = _table_element(payload)
        self.assertEqual(table["tag"], "table")
        self.assertEqual(
            [column["display_name"] for column in table["columns"]],
            ["状态", "股票", "现价", "MA120", "偏离", "买入线", "卖出线"],
        )
        self.assertEqual(table["rows"][0]["status"], "低于买入线")
        self.assertEqual(table["rows"][0]["stock"], "三花智控 002050")
        self.assertEqual(table["rows"][0]["price"], "38.30")
        self.assertEqual(table["rows"][0]["ma120"], "45.91")

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
        table = _table_element(payload)

        self.assertEqual(card["header"]["template"], "green")
        self.assertEqual(table["rows"][0]["status"], "低于买入线")
        self.assertEqual(table["rows"][0]["stock"], "中国铝业 601600")

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
        table = _table_element(payload)

        self.assertEqual(card["header"]["template"], "red")
        self.assertEqual(table["rows"][0]["status"], "高于卖出线")
        self.assertEqual(table["rows"][0]["stock"], "埃斯顿 002747")


    def test_watchlist_card_renders_table_sorted_by_action_priority(self):
        items = [
            {
                "name": "普通高于MA",
                "code": "600584",
                "price": 68.10,
                "ma120": 62.89,
                "ma120_pct": 8.28,
                "buy_line": 55.35,
                "sell_line": 70.44,
                "signal": "hold",
            },
            {
                "name": "低于买入线",
                "code": "601600",
                "price": 9.69,
                "ma120": 11.23,
                "ma120_pct": -13.70,
                "buy_line": 9.88,
                "sell_line": 12.58,
                "signal": "hold",
            },
            {
                "name": "普通低于MA",
                "code": "601601",
                "price": 31.90,
                "ma120": 35.50,
                "ma120_pct": -10.15,
                "buy_line": 31.24,
                "sell_line": 39.76,
                "signal": "hold",
            },
            {
                "name": "高于卖出线",
                "code": "002747",
                "price": 34.46,
                "ma120": 27.67,
                "ma120_pct": 24.54,
                "buy_line": 24.35,
                "sell_line": 30.99,
                "signal": "hold",
            },
        ]

        payload = monitor.render_watchlist_card(items, "2026-08-05")
        table = _table_element(payload)

        self.assertEqual(
            [row["stock"].split()[-1] for row in table["rows"]],
            ["002747", "601600", "601601", "600584"],
        )

    def test_portfolio_card_table_includes_cost_and_profit_columns(self):
        items = [
            {
                "name": "紫金矿业",
                "code": "601899",
                "price": 33.69,
                "ma120": 32.68,
                "ma120_pct": 3.10,
                "buy_line": 28.76,
                "sell_line": 36.60,
                "signal": "hold",
                "cost": 33.39,
                "shares": 100,
            }
        ]

        payload = monitor.render_portfolio_card(items, "2026-08-05")
        table = _table_element(payload)

        self.assertEqual(
            [column["display_name"] for column in table["columns"]],
            ["状态", "股票", "现价", "MA120", "偏离", "买入线", "卖出线", "成本", "浮盈"],
        )
        self.assertEqual(table["rows"][0]["status"], "高于MA120")
        self.assertEqual(table["rows"][0]["stock"], "紫金矿业 601899")
        self.assertEqual(table["rows"][0]["cost"], "33.39")
        self.assertEqual(table["rows"][0]["pnl"], "+0.90%")

    def test_card_prints_alerts_for_triggered_signals_and_sell_zone(self):
        items = [
            {
                "name": "中国太保",
                "code": "601601",
                "price": 30.59,
                "ma120": 35.07,
                "ma120_pct": -12.77,
                "buy_line": 30.86,
                "sell_line": 39.28,
                "signal": "buy",
            },
            {
                "name": "长电科技",
                "code": "600584",
                "price": 78.52,
                "ma120": 63.89,
                "ma120_pct": 22.91,
                "buy_line": 56.22,
                "sell_line": 71.55,
                "signal": "sell",
            },
            {
                "name": "埃斯顿",
                "code": "002747",
                "price": 36.18,
                "ma120": 28.03,
                "ma120_pct": 29.06,
                "buy_line": 24.67,
                "sell_line": 31.40,
                "signal": "hold",
            },
        ]

        payload = monitor.render_watchlist_card(items, "2026-08-11")
        content = _markdown_content(payload)

        self.assertIn("重点提醒", content)
        self.assertIn("买入信号：中国太保 601601", content)
        self.assertIn("现价 30.59", content)
        self.assertIn("买入线 30.86", content)
        self.assertIn("卖出信号：长电科技 600584", content)
        self.assertIn("现价 78.52", content)
        self.assertIn("卖出线 71.55", content)
        self.assertIn("高于卖出线：埃斯顿 002747", content)
        self.assertIn("卖出线 31.40", content)

    def test_card_hides_triggered_signal_section_when_no_signal(self):
        items = [
            {
                "name": "紫金矿业",
                "code": "601899",
                "price": 33.69,
                "ma120": 32.68,
                "ma120_pct": 3.10,
                "buy_line": 28.76,
                "sell_line": 36.60,
                "signal": "hold",
            }
        ]

        payload = monitor.render_watchlist_card(items, "2026-08-11")
        content = _markdown_content(payload)

        self.assertNotIn("重点提醒", content)


if __name__ == "__main__":
    unittest.main()
