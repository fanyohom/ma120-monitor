import copy
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import pandas as pd

import etf_dca as dca
import etf_data as data
import stock_dynamic_monitor as monitor


NOW = datetime(2026, 9, 7, 14, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


def config():
    return dca.load_config(Path(__file__).resolve().parents[1] / "etf_plans.example.toml")


def market(code="510300", price=8.0):
    dates = pd.bdate_range(end=NOW.date(), periods=540)
    rows = [[d.date().isoformat(), "10", "10", "10", "10", "1000"] for d in dates]
    return {"code": code, "qfq": rows, "raw": rows, "price": price,
            "quote_at": NOW.isoformat(), "source": "test", "volume": 1000}


class ETFDataTests(unittest.TestCase):
    def test_shanghai_etf_routing(self):
        self.assertEqual(data.etf_symbol("510300"), "sh510300")
        self.assertEqual(data.etf_symbol("159915"), "sz159915")
        self.assertEqual(monitor._sina_symbol("513500"), "sh513500")

    def test_ma_excludes_current_bar_and_rebases_adjustment(self):
        payload = market()
        payload["qfq"] = copy.deepcopy(payload["qfq"])
        for row in payload["qfq"]:
            row[1] = "5"
            row[2] = "5"
        payload["qfq"][-1][2] = "50"
        payload["raw"][-1][2] = "100"
        result = data.market_metrics(payload, 500, NOW)
        self.assertEqual(result["ma"], 10)
        self.assertEqual(result["ma_date"], "2026-09-04")
        self.assertAlmostEqual(result["ma_deviation_pct"], -20)

    def test_async_current_closes_do_not_shift_completed_ma(self):
        payload = market()
        payload["qfq"] = copy.deepcopy(payload["qfq"])
        before = data.market_metrics(payload, 500, NOW)
        payload["qfq"][-1][2] = "11"
        payload["raw"][-1][2] = "12"
        after = data.market_metrics(payload, 500, NOW)
        self.assertEqual(before["ma"], after["ma"])

    def test_stale_quote_cannot_be_actionable(self):
        payload = market()
        payload["quote_at"] = "2026-09-04T15:00:00+08:00"
        self.assertFalse(data.market_metrics(payload, 500, NOW)["tradable"])
        payload["quote_at"] = "2026-09-07T10:00:00+08:00"
        self.assertFalse(data.market_metrics(payload, 500, NOW)["tradable"])
        payload["quote_at"] = NOW.isoformat()
        payload["volume"] = 0
        self.assertFalse(data.market_metrics(payload, 500, NOW)["tradable"])

    def test_invalid_price_and_duplicate_history_rejected(self):
        for value in ("NaN", "inf", "-1", "0"):
            with self.assertRaises(ValueError):
                data.parse_history([["2026-09-04", 1, value]])
        with self.assertRaises(ValueError):
            data.parse_history([["2026-09-04", 1, 1], ["2026-09-04", 1, 1]])

    def test_short_history_disables_ma_not_cost_strategy(self):
        payload = market()
        payload["qfq"], payload["raw"] = payload["qfq"][-100:], payload["raw"][-100:]
        metrics = data.market_metrics(payload, 500, NOW)
        self.assertIsNone(metrics["ma"])
        cfg = config()
        row = dca.evaluate(cfg["plans"][0], "drawdown", metrics, None, "", 0, 0, cfg["settings"])
        self.assertEqual(row["reference_amount"], 1000)

    @patch("etf_data.requests.get")
    def test_raw_data_not_substituted_for_qfq(self, get):
        response = Mock()
        response.json.return_value = {"data": {"sh510300": {"day": market()["raw"]}}}
        get.return_value = response
        with tempfile.TemporaryDirectory() as folder, self.assertRaises(ValueError):
            data.fetch_market("510300", 530, NOW, Path(folder))

    def test_short_cache_not_reused_for_long_ma(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / NOW.date().isoformat()
            target.mkdir()
            path = target / "510300_140000.json"
            path.write_text(json.dumps({"fetched_at": NOW.isoformat(), "requested_days": 130}))
            with patch("etf_data.requests.get", side_effect=OSError("new request")) as get:
                with self.assertRaisesRegex(OSError, "new request"):
                    data.fetch_market("510300", 530, NOW, Path(folder))
                get.assert_called_once()


class ValuationTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "values.csv"
        self.settings = {"lookback_years": 10, "min_samples": 3, "min_span_days": 2, "max_age_days": 7}

    def write(self, prices, dates=None, available=None):
        dates = dates or [date(2026, 9, 1) + timedelta(days=i) for i in range(len(prices))]
        pd.DataFrame({"date": dates, "available_date": available or dates,
                      "index_code": ["000300"] * len(prices), "pe_ttm": prices}).to_csv(self.path, index=False)

    def test_filters_future_and_unpublished_observations(self):
        self.write([10, 20, 15, 1], available=["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-08"])
        result = data.valuation_metrics(self.path, "000300", NOW.date(), self.settings)
        self.assertEqual(result["samples"], 3)
        self.assertEqual(result["pe_ttm"], 15)
        self.assertEqual(result["percentile"], 50)

    def test_ties_neutral_and_wrong_index_rejected(self):
        self.write([10, 10, 10])
        self.assertEqual(data.valuation_metrics(self.path, "000300", NOW.date(), self.settings)["percentile"], 50)
        with self.assertRaisesRegex(ValueError, "No available"):
            data.valuation_metrics(self.path, "NDX", NOW.date(), self.settings)

    def test_stale_short_duplicate_and_negative_data_rejected(self):
        self.write([10, 11, 12])
        with self.assertRaisesRegex(ValueError, "stale"):
            data.valuation_metrics(self.path, "000300", date(2026, 10, 1), self.settings)
        self.write([10, 11])
        with self.assertRaisesRegex(ValueError, "samples"):
            data.valuation_metrics(self.path, "000300", NOW.date(), self.settings)
        self.write([10, -1, 12])
        with self.assertRaises(ValueError):
            data.valuation_metrics(self.path, "000300", NOW.date(), self.settings)
        self.write([10, 11, 12], dates=["2026-09-03"] * 3)
        with self.assertRaisesRegex(ValueError, "duplicate"):
            data.valuation_metrics(self.path, "000300", NOW.date(), self.settings)


class DCATests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.config = config()
        self.config["directory"] = Path(self.folder.name)
        self.db = dca.open_journal(Path(self.folder.name) / "journal.sqlite3")
        self.addCleanup(self.db.close)
        self.plan = self.config["plans"][0]
        self.settings = {**self.config["settings"], **self.config["valuation"]}

    def report(self):
        return dca.build_report(self.config, self.db, NOW, lambda code, *args: market(code))

    def test_alternatives_do_not_triple_budget_and_reports_do_not_execute(self):
        report = self.report()
        self.assertEqual(len(report["rows"]), 12)
        self.assertEqual(sum(row["selected"] for row in report["rows"]), 4)
        self.assertEqual(report["total_action_amount"], sum(row["action_amount"] for row in report["rows"] if row["selected"]))
        self.assertTrue(all(row["action_amount"] == 0 for row in report["rows"] if not row["selected"]))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM fills").fetchone()[0], 0)
        self.assertEqual(self.report()["total_action_amount"], report["total_action_amount"])

    def test_cap_lot_and_cost_based_rule(self):
        metrics = data.market_metrics(market(price=6), 500, NOW)
        row = dca.evaluate(self.plan, "drawdown", metrics, None, "", 10, 100, self.settings)
        self.assertEqual(row["reference_amount"], 2000)
        self.assertEqual(row["reference_shares"], 300)
        self.assertEqual(row["estimated_cost"], 1800)
        initial = dca.evaluate(self.plan, "drawdown", metrics, None, "", 0, 0, self.settings)
        self.assertEqual(initial["reference_amount"], 1000)

    def test_float_noise_does_not_move_band_boundary(self):
        self.assertEqual(dca.multiplier((0.9 - 1) * 100, self.settings), 1.2)
        self.assertEqual(dca.multiplier((1.1 - 1) * 100, self.settings), 0.8)
        self.assertEqual(dca.multiplier(-20, self.settings), 1.5)

    def test_valuation_thresholds_are_percentiles(self):
        metrics = data.market_metrics(market(), 500, NOW)
        for percentile, amount in [(29.99, 1000), (30, 0), (70, 0)]:
            row = dca.evaluate(self.plan, "valuation", metrics,
                               {"percentile": percentile, "pe_ttm": 12, "date": "2026-09-04"},
                               "", 0, 0, self.settings)
            self.assertEqual(row["reference_amount"], amount)

    def test_due_dates_and_holiday_catchup(self):
        self.assertEqual(dca.period(self.plan, date(2026, 9, 8)), ("2026-09-07", True))
        plan = {**self.plan, "weekday": 2}
        self.assertFalse(dca.period(plan, NOW.date())[1])
        plan = {**self.plan, "frequency": "monthly", "monthday": 10}
        self.assertFalse(dca.period(plan, NOW.date())[1])
        self.assertTrue(dca.period(plan, date(2026, 9, 11))[1])

    def test_fill_updates_cost_and_suppresses_repeat_period(self):
        dca.record_trade(self.db, self.plan, "fill-1", "buy", 100, 8, 5, NOW.date(), NOW.date())
        self.assertEqual(dca.position(self.plan, self.db, NOW.date()), (100, 8.05))
        selected = next(row for row in self.report()["rows"] if row["plan_id"] == "csi300" and row["selected"])
        self.assertEqual(selected["status"], "recorded")
        self.assertEqual(selected["action_amount"], 0)
        with self.assertRaisesRegex(ValueError, "duplicate"):
            dca.record_trade(self.db, self.plan, "fill-1", "buy", 100, 8, 5, NOW.date(), NOW.date())
        dca.record_trade(self.db, self.plan, "fill-2", "sell", 50, 9, 1, NOW.date(), NOW.date())
        self.assertEqual(dca.position(self.plan, self.db, NOW.date()), (50, 8.05))
        with self.assertRaisesRegex(ValueError, "larger"):
            dca.record_trade(self.db, self.plan, "fill-3", "sell", 100, 9, 1, NOW.date(), NOW.date())

    def test_missing_valuation_blocks_only_that_strategy_and_native_card(self):
        self.plan["strategy"] = "valuation"
        report = self.report()
        selected = next(row for row in report["rows"] if row["plan_id"] == "csi300" and row["selected"])
        self.assertEqual(selected["status"], "blocked")
        self.assertEqual(selected["action_amount"], 0)
        card = dca.render_card(report, compare=True)
        table = card["card"]["elements"][1]
        self.assertEqual(table["tag"], "table")
        self.assertEqual(len(table["rows"]), 12)


if __name__ == "__main__":
    unittest.main()
