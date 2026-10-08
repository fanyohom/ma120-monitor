import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pandas as pd
from openpyxl import Workbook

from ma120_web import build_ma120_dashboard, build_ma120_history, estimate_ma120_profit
from portfolio_store import write_portfolio
from watchlist_store import write_watchlist


def history(close=100, count=121):
    dates = pd.date_range("2026-01-01", periods=count, freq="D")
    return pd.DataFrame({"close": [close] * count}, index=dates)


class Ma120WebTests(unittest.TestCase):
    def test_history_marks_only_completed_close_crossings(self):
        dates = pd.bdate_range(end="2026-10-02", periods=126)
        closes = [100] * 120 + [87, 87, 115, 115, 87, 100]
        bars = pd.DataFrame({"close": closes}, index=dates)
        calls = []

        def load(code, *, days):
            calls.append((code, days))
            return bars

        result = build_ma120_history("600001", "测试股票", hist_loader=load,
                                     now=datetime(2026, 10, 5, 10))
        self.assertEqual(calls, [("600001", 800)])
        self.assertEqual(result["currency"], "CNY")
        self.assertEqual((result["buy_count"], result["sell_count"]), (2, 1))
        self.assertEqual([point["side"] for point in result["signals"]],
                         ["buy", "sell", "buy"])
        self.assertEqual(result["signals"][0]["price"], 87)
        self.assertEqual(result["signals"][0]["threshold"],
                         result["series"][1]["buy_line"])
        self.assertIsNone(result["series"][0]["signal"])
        self.assertIsNone(result["series"][2]["signal"])
        self.assertEqual(result["window_end"], "2026-10-02")
        json.dumps(result, allow_nan=False)

    def test_history_excludes_current_unfinished_daily_bar(self):
        dates = pd.bdate_range(end="2026-10-05", periods=122)
        bars = pd.DataFrame({"close": [100] * 121 + [87]}, index=dates)
        result = build_ma120_history("HK01801", "港股", hist_loader=lambda *_args, **_kw: bars,
                                     now=datetime(2026, 10, 5, 10))
        self.assertEqual(result["currency"], "HKD")
        self.assertEqual(result["window_end"], "2026-10-02")
        self.assertEqual(result["signals"], [])

    def test_history_rejects_stale_future_duplicate_and_invalid_bars(self):
        base = pd.DataFrame({"close": [100] * 121},
                            index=pd.bdate_range(end="2026-10-02", periods=121))
        cases = {
            "过期": base.set_axis(pd.bdate_range(end="2026-09-01", periods=121)),
            "未来": pd.concat([base, pd.DataFrame({"close": [100]},
                                                    index=pd.to_datetime(["2026-10-06"]))]),
            "重复": pd.concat([base, base.iloc[[-1]]]),
            "无效": base.assign(close=[100] * 120 + [float("nan")]),
        }
        for label, bars in cases.items():
            with self.subTest(label=label), self.assertRaises(ValueError):
                build_ma120_history("600001", "测试", hist_loader=lambda *_a, **_k: bars,
                                    now=datetime(2026, 10, 5, 10))

    def test_history_bounds_visible_window_after_full_history_calculation(self):
        dates = pd.bdate_range(end="2026-10-02", periods=500)
        bars = pd.DataFrame({"close": [100] * 500}, index=dates)
        result = build_ma120_history("600001", "测试", hist_loader=lambda *_a, **_k: bars,
                                     now=datetime(2026, 10, 5, 10))
        self.assertEqual(len(result["series"]), 360)
        self.assertEqual(result["window_start"], result["series"][0]["date"])
        self.assertEqual(result["series"][0]["ma120"], 100)

    def test_estimate_uses_next_close_and_independent_position(self):
        points = [
            ("2026-09-28", 100, None), ("2026-09-29", 90, "buy"),
            ("2026-09-30", 80, None), ("2026-10-01", 95, "sell"),
            ("2026-10-02", 100, None), ("2026-10-05", 50, "buy"),
            ("2026-10-06", 40, None), ("2026-10-07", 45, "sell"),
        ]
        replay = {"currency": "CNY", "series": [
            {"date": day, "close": close, "signal": signal}
            for day, close, signal in points]}
        result = estimate_ma120_profit(replay, "2026-09-28")
        self.assertEqual([(trade["signal_date"], trade["date"], trade["side"])
                          for trade in result["trades"]], [
                              ("2026-09-29", "2026-09-30", "buy"),
                              ("2026-10-01", "2026-10-02", "sell"),
                              ("2026-10-05", "2026-10-06", "buy")])
        self.assertEqual(result["trades"][0]["units"], 1250)
        self.assertEqual((result["estimated_value"], result["profit"], result["return_pct"]),
                         (140625, 40625, 40.62))
        self.assertEqual((result["buy_count"], result["sell_count"], result["open_position"]),
                         (2, 1, True))
        self.assertEqual(result["initial_capital"], 100000)
        json.dumps(result, allow_nan=False)

        later = estimate_ma120_profit(replay, "2026-09-30")
        self.assertEqual([trade["side"] for trade in later["trades"]], ["buy"])
        self.assertEqual(later["estimated_value"], 112500)
        self.assertEqual(estimate_ma120_profit(replay, "2026-10-07")["trades"], [])

    def test_estimate_rejects_invalid_or_out_of_window_start(self):
        replay = {"series": [{"date": "2026-10-01", "close": 100, "signal": "buy"},
                             {"date": "2026-10-02", "close": 90, "signal": None}]}
        for start in (None, "bad-date", "2026-09-30", "2026-10-03"):
            with self.subTest(start=start), self.assertRaises(ValueError):
                estimate_ma120_profit(replay, start)

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

    def test_missing_or_unreadable_portfolio_json_reports_source_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "portfolio.json"
            watchlist = Path(tmpdir) / "watchlist.json"
            with patch("stock_dynamic_monitor.PORTFOLIO_JSON", str(path)), \
                    patch("stock_dynamic_monitor.WATCHLIST_JSON", str(watchlist)):
                missing = build_ma120_dashboard(hist_loader=lambda code: self.fail("no rows"))
                self.assertIn("未找到", missing["source_error"])
                self.assertIn("portfolio.json", missing["source_error"])
                self.assertEqual(missing["summary"]["portfolio_count"], 0)

                path.write_text("not json", encoding="utf-8")
                watchlist.write_text("not json", encoding="utf-8")
                unreadable = build_ma120_dashboard(hist_loader=lambda code: self.fail("no rows"))
                self.assertIn("读取", unreadable["source_error"])
                self.assertIn("portfolio.json", unreadable["source_error"])
                self.assertIn("watchlist.json", unreadable["source_error"])
                self.assertEqual(unreadable["portfolio"], [])

    def test_legacy_workbook_is_not_read_when_json_exists(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workbook_path = Path(tmpdir) / "stocks.xlsx"
            portfolio = Path(tmpdir) / "portfolio.json"
            watchlist = Path(tmpdir) / "watchlist.json"
            workbook = Workbook()
            workbook.active.title = "portfolio"
            workbook.active.append(["code", "name", "cost", "shares"])
            workbook.active.append(["600002", "旧表持仓", "1", "2"])
            workbook.save(workbook_path)
            write_portfolio([], portfolio)
            write_watchlist([{"code": "600001", "name": "关注标的"}], watchlist)
            with patch("stock_dynamic_monitor.STOCKS_FILE", str(workbook_path)), \
                    patch("stock_dynamic_monitor.PORTFOLIO_JSON", str(portfolio)), \
                    patch("stock_dynamic_monitor.WATCHLIST_JSON", str(watchlist)), \
                    patch("stock_dynamic_monitor._read_xlsx_rows", side_effect=AssertionError("legacy read")):
                result = build_ma120_dashboard(
                    hist_loader=lambda code: history(), quote_loader=lambda code: {},
                    now=datetime(2026, 5, 1, 16, 0))
            self.assertIsNone(result["source_error"])
            self.assertEqual(result["portfolio"], [])
            self.assertEqual(result["summary"]["watchlist_count"], 1)

    def test_missing_portfolio_json_still_loads_watchlist_without_excel_fallback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workbook_path = Path(tmpdir) / "stocks.xlsx"
            portfolio = Path(tmpdir) / "portfolio.json"
            watchlist = Path(tmpdir) / "watchlist.json"
            workbook = Workbook()
            workbook.active.title = "portfolio"
            workbook.active.append(["code", "name", "cost", "shares"])
            workbook.active.append(["600002", "旧表持仓", "1", "2"])
            workbook.save(workbook_path)
            write_watchlist([{"code": "600001", "name": "关注标的"}], watchlist)
            with patch("stock_dynamic_monitor.STOCKS_FILE", str(workbook_path)), \
                    patch("stock_dynamic_monitor.PORTFOLIO_JSON", str(portfolio)), \
                    patch("stock_dynamic_monitor.WATCHLIST_JSON", str(watchlist)), \
                    patch("stock_dynamic_monitor._read_xlsx_rows", side_effect=AssertionError("legacy read")):
                result = build_ma120_dashboard(
                    hist_loader=lambda code: history(),
                    quote_loader=lambda code: {},
                    now=datetime(2026, 5, 1, 16, 0),
                )
            self.assertIn("portfolio.json", result["source_error"])
            self.assertNotIn("watchlist.json", result["source_error"])
            self.assertEqual(result["portfolio"], [])
            self.assertEqual(result["summary"]["watchlist_count"], 1)
            self.assertEqual(result["watchlist"][0]["name"], "关注标的")
            self.assertIsNone(result["watchlist"][0]["error"])

    def test_runtime_portfolio_json_preserves_cost_and_shares(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            portfolio = Path(tmpdir) / "portfolio.json"
            watchlist = Path(tmpdir) / "watchlist.json"
            write_portfolio([{"code": "600001", "name": "持仓标的", "cost": 80,
                              "shares": 100}], portfolio)
            write_watchlist([{"code": "600001", "name": "重复关注"}], watchlist)
            with patch("stock_dynamic_monitor.PORTFOLIO_JSON", str(portfolio)), \
                    patch("stock_dynamic_monitor.WATCHLIST_JSON", str(watchlist)):
                result = build_ma120_dashboard(
                    hist_loader=lambda code: history(),
                    quote_loader=lambda code: {"price": 100, "quote_date": "2026-05-01"},
                    now=datetime(2026, 5, 1, 16, 0),
                )
            self.assertIsNone(result["source_error"])
            self.assertEqual(result["watchlist"], [])
            self.assertEqual(result["portfolio"][0]["name"], "持仓标的")
            self.assertEqual(result["portfolio"][0]["cost_basis"], 8000)
            self.assertEqual(result["portfolio"][0]["market_value"], 10000)


if __name__ == "__main__":
    unittest.main()
