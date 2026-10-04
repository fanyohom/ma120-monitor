import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pandas as pd
from openpyxl import Workbook

from ma120_web import build_ma120_dashboard


def history(close=100, count=121):
    dates = pd.date_range("2026-01-01", periods=count, freq="D")
    return pd.DataFrame({"close": [close] * count}, index=dates)


class Ma120WebTests(unittest.TestCase):
    def test_dashboard_preserves_crossing_signal_and_position_pnl(self):
        calls = []

        def hist_loader(code):
            calls.append(code)
            return history()

        quotes = {
            "600001": {"price": 87, "quote_date": "2026-05-01", "quote_time": "10:30:00"},
            "000002": {"price": 113, "quote_date": "2026-05-01", "quote_time": "10:31:00"},
        }
        with patch("stock_dynamic_monitor.send_feishu") as send:
            result = build_ma120_dashboard(
                portfolio=[{"code": "600001", "name": "持仓", "cost": 80, "shares": 100}],
                watchlist=[{"code": "600001", "name": "重复"}, {"code": "000002", "name": "关注"}],
                hist_loader=hist_loader,
                quote_loader=lambda code: quotes[code],
                now=datetime(2026, 5, 1, 10, 35),
            )
        send.assert_not_called()
        self.assertCountEqual(calls, ["600001", "000002"])
        self.assertEqual(len(result["watchlist"]), 1)
        held = result["portfolio"][0]
        self.assertEqual((held["signal"], held["zone"], held["status"]),
                         ("buy", "below_buy", "买入信号"))
        self.assertEqual((held["market_value"], held["cost_basis"], held["unrealized_pnl"]),
                         (8700, 8000, 700))
        self.assertEqual((result["watchlist"][0]["signal"], result["watchlist"][0]["zone"]),
                         ("sell", "above_sell"))
        self.assertEqual(result["summary"]["unrealized_pnl_pct"], 8.75)
        self.assertEqual(held["currency"], "CNY")
        self.assertEqual(result["summary"]["currency_totals"]["CNY"]["unrealized_pnl"], 700)
        self.assertEqual(result["summary"]["buy_signals"], 1)
        self.assertEqual(result["summary"]["sell_signals"], 1)
        json.dumps(result, allow_nan=False)

    def test_stale_quote_falls_back_to_historical_close(self):
        result = build_ma120_dashboard(
            portfolio=[], watchlist=[{"code": "600001", "name": "关注"}],
            hist_loader=lambda code: history(),
            quote_loader=lambda code: {"price": 87, "quote_date": "2026-04-30"},
            now=datetime(2026, 5, 2, 10, 0),
        )
        row = result["watchlist"][0]
        self.assertEqual(row["price"], 100)
        self.assertEqual(row["price_source"], "daily_close")
        self.assertEqual(row["price_date"], "2026-05-01")
        self.assertEqual(row["quote_date"], "2026-04-30")
        self.assertIsNone(row["signal"])
        self.assertEqual(row["zone"], "between")

    def test_holiday_does_not_relabel_prior_day_crossing_as_today_signal(self):
        closes = [100] * 120 + [87]
        dates = pd.date_range("2026-01-01", periods=len(closes), freq="D")
        bars = pd.DataFrame({"close": closes}, index=dates)
        self.assertEqual(dates[-1].date().isoformat(), "2026-05-01")
        result = build_ma120_dashboard(
            portfolio=[], watchlist=[{"code": "600001", "name": "关注"}],
            hist_loader=lambda code: bars,
            quote_loader=lambda code: {"price": 87, "quote_date": "2026-05-01"},
            now=datetime(2026, 5, 3, 10, 0),
        )
        row = result["watchlist"][0]
        self.assertEqual(row["price_source"], "daily_close")
        self.assertEqual(row["price_date"], "2026-05-01")
        self.assertEqual(row["quote_date"], "2026-05-01")
        self.assertIsNone(row["signal"])
        self.assertEqual(row["zone"], "below_buy")
        self.assertEqual(row["status"], "低于买入线")
        self.assertEqual(result["summary"]["buy_signals"], 0)
        self.assertEqual(result["summary"]["buy_zone"], 1)

    def test_current_quote_does_not_repeat_crossing_before_daily_bar_updates(self):
        closes = [100] * 120 + [87]
        dates = pd.date_range("2026-01-01", periods=len(closes), freq="D")
        bars = pd.DataFrame({"close": closes}, index=dates)
        result = build_ma120_dashboard(
            portfolio=[], watchlist=[{"code": "600001", "name": "关注"}],
            hist_loader=lambda code: bars,
            quote_loader=lambda code: {"price": 87, "quote_date": "2026-05-04"},
            now=datetime(2026, 5, 4, 10, 0),
        )
        row = result["watchlist"][0]
        self.assertEqual(row["price_source"], "realtime")
        self.assertEqual(row["price_date"], "2026-05-04")
        self.assertIsNone(row["signal"])
        self.assertEqual(row["zone"], "below_buy")

    def test_today_daily_bar_crossing_survives_quote_failure(self):
        closes = [100] * 120 + [87]
        dates = pd.date_range("2026-01-01", periods=len(closes), freq="D")
        bars = pd.DataFrame({"close": closes}, index=dates)
        result = build_ma120_dashboard(
            portfolio=[], watchlist=[{"code": "600001", "name": "关注"}],
            hist_loader=lambda code: bars,
            quote_loader=lambda code: {},
            now=datetime(2026, 5, 1, 15, 30),
        )
        row = result["watchlist"][0]
        self.assertEqual(row["price_source"], "daily_close")
        self.assertEqual(row["price_date"], "2026-05-01")
        self.assertEqual(row["signal"], "buy")
        self.assertEqual(row["status"], "买入信号")
        self.assertEqual(result["summary"]["buy_signals"], 1)

    def test_unavailable_history_keeps_row_visible_and_excludes_pnl(self):
        result = build_ma120_dashboard(
            portfolio=[{"code": "600001", "name": "持仓", "cost": 10, "shares": 100}],
            watchlist=[],
            hist_loader=lambda code: history(count=20),
            quote_loader=lambda code: self.fail("quote should not be requested"),
        )
        row = result["portfolio"][0]
        self.assertEqual(row["status"], "数据不足")
        self.assertIn("120", row["error"])
        self.assertIsNone(row["market_value"])
        self.assertEqual(result["summary"]["data_unavailable"], 1)
        self.assertEqual(result["summary"]["pnl_coverage"], 0)
        json.dumps(result, allow_nan=False)

    def test_buy_zone_does_not_imply_new_buy_signal(self):
        closes = [100] * 119 + [87, 87]
        dates = pd.date_range("2026-01-01", periods=len(closes), freq="D")
        bars = pd.DataFrame({"close": closes}, index=dates)
        result = build_ma120_dashboard(
            portfolio=[], watchlist=[{"code": "600001", "name": "关注"}],
            hist_loader=lambda code: bars,
            quote_loader=lambda code: {},
            now=datetime(2026, 5, 2, 10, 0),
        )
        row = result["watchlist"][0]
        self.assertIsNone(row["signal"])
        self.assertEqual(row["zone"], "below_buy")
        self.assertEqual(row["status"], "低于买入线")
        self.assertEqual(result["summary"]["buy_signals"], 0)
        self.assertEqual(result["summary"]["buy_zone"], 1)

    def test_mixed_currency_positions_have_separate_totals(self):
        result = build_ma120_dashboard(
            portfolio=[
                {"code": "600001", "name": "A 股", "cost": 80, "shares": 100},
                {"code": "HK01801", "name": "港股", "cost": 50, "shares": 10},
            ],
            watchlist=[],
            hist_loader=lambda code: history(),
            quote_loader=lambda code: {"price": 100, "quote_date": "2026-05-01"},
            now=datetime(2026, 5, 1, 10, 0),
        )
        summary = result["summary"]
        self.assertIsNone(summary["market_value"])
        self.assertIsNone(summary["unrealized_pnl_pct"])
        self.assertEqual(summary["currency_totals"]["CNY"]["market_value"], 10000)
        self.assertEqual(summary["currency_totals"]["HKD"]["market_value"], 1000)
        self.assertEqual(summary["currency_totals"]["CNY"]["unrealized_pnl"], 2000)
        self.assertEqual(summary["currency_totals"]["HKD"]["unrealized_pnl"], 500)
        self.assertEqual([row["currency"] for row in result["portfolio"]], ["CNY", "HKD"])
        json.dumps(result, allow_nan=False)

    def test_stale_or_future_history_is_not_used_for_valuation(self):
        for now in (datetime(2026, 5, 17), datetime(2026, 4, 30)):
            with self.subTest(now=now):
                result = build_ma120_dashboard(
                    portfolio=[{"code": "600001", "name": "持仓", "cost": 80, "shares": 100}],
                    watchlist=[],
                    hist_loader=lambda code: history(),
                    quote_loader=lambda code: self.fail("invalid history must not fetch quote"),
                    now=now,
                )
                row = result["portfolio"][0]
                self.assertEqual(row["last_close_date"], "2026-05-01")
                self.assertIsNone(row["price"])
                self.assertIsNone(row["ma120"])
                self.assertIsNone(row["market_value"])
                self.assertIsNotNone(row["error"])
                self.assertEqual(result["summary"]["data_unavailable"], 1)
                self.assertEqual(result["summary"]["pnl_coverage"], 0)

    def test_missing_or_unreadable_workbook_reports_source_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "stocks.xlsx"
            with patch("stock_dynamic_monitor.STOCKS_FILE", str(path)):
                missing = build_ma120_dashboard(hist_loader=lambda code: self.fail("no rows"))
                self.assertIn("未找到", missing["source_error"])
                self.assertEqual(missing["summary"]["portfolio_count"], 0)

                path.write_text("not an xlsx", encoding="utf-8")
                unreadable = build_ma120_dashboard(hist_loader=lambda code: self.fail("no rows"))
                self.assertIn("读取", unreadable["source_error"])
                self.assertIn("portfolio", unreadable["source_error"])
                self.assertIn("watchlist", unreadable["source_error"])

    def test_missing_required_sheet_reports_source_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "stocks.xlsx"
            workbook = Workbook()
            workbook.active.title = "portfolio"
            workbook.active.append(["code", "name", "cost", "shares"])
            workbook.save(path)
            with patch("stock_dynamic_monitor.STOCKS_FILE", str(path)):
                result = build_ma120_dashboard(hist_loader=lambda code: self.fail("no rows"))
            self.assertIn("watchlist", result["source_error"])
            self.assertEqual(result["portfolio"], [])

    def test_missing_first_sheet_still_loads_watchlist(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "stocks.xlsx"
            workbook = Workbook()
            workbook.active.title = "watchlist"
            workbook.active.append(["code", "name"])
            workbook.active.append(["600001", "关注标的"])
            workbook.save(path)
            with patch("stock_dynamic_monitor.STOCKS_FILE", str(path)):
                result = build_ma120_dashboard(
                    hist_loader=lambda code: history(),
                    quote_loader=lambda code: {},
                    now=datetime(2026, 5, 1, 16, 0),
                )
            self.assertIn("portfolio", result["source_error"])
            self.assertNotIn("watchlist] 失败", result["source_error"])
            self.assertEqual(result["summary"]["watchlist_count"], 1)
            self.assertEqual(result["watchlist"][0]["name"], "关注标的")
            self.assertIsNone(result["watchlist"][0]["error"])


if __name__ == "__main__":
    unittest.main()
