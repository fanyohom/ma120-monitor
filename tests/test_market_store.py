import json
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timedelta
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd

import etf_data
import stock_dynamic_monitor as monitor
from etf_dca import build_report, load_config, open_journal
from market_store import MarketStore, database_for_cache
from ma120_web import build_ma120_dashboard
from web_app import DashboardService, StockDashboardService
from scripts.migrate_market_data import migrate


NOW = datetime(2026, 10, 12, 11, tzinfo=ZoneInfo("Asia/Shanghai"))


def payload(now=NOW):
    dates = pd.bdate_range(end=now.date() - timedelta(days=1), periods=550)
    rows = [[d.date().isoformat(), "10", "10", "11", "9", "1000"] for d in dates]
    return {"code": "510300", "source": "test", "requested_days": 550,
            "fetched_at": now.isoformat(), "quote_at": now.isoformat(),
            "price": 8, "volume": 1000, "qfq": rows, "raw": rows}


class MarketStoreTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.cache = self.root / ".stock_cache" / "etf"
        self.store = MarketStore(database_for_cache(self.cache))

    def save(self, item):
        etf_data.save_market(self.store, "510300", item)

    def test_database_survives_cache_removal_and_reads_without_network(self):
        item = payload()
        self.save(item)
        self.assertFalse(self.cache.exists())
        with patch("etf_data.requests.get", side_effect=AssertionError("No network")):
            result = etf_data.fetch_market("510300", 530, NOW + timedelta(days=30), self.cache,
                                           database_only=True)
        self.assertEqual(result["qfq"], item["qfq"])
        self.assertEqual(result["quote_at"], item["quote_at"])
        self.assertTrue(result["data_stale"])
        metrics = etf_data.market_metrics(result, 500, NOW + timedelta(days=30))
        self.assertEqual(metrics["ma"], 10)
        self.assertFalse(metrics["tradable"])

    def test_refresh_failure_preserves_last_valid_version_and_blocks_buy(self):
        self.save(payload())
        with patch("etf_data.requests.get", side_effect=OSError("offline")):
            result = etf_data.fetch_market("510300", 530, NOW, self.cache, force=True)
        self.assertIn("offline", result["refresh_error"])
        self.assertFalse(etf_data.market_metrics(result, 500, NOW)["tradable"])
        config = load_config(Path(__file__).resolve().parents[1] / "etf_plans.example.toml")
        config["directory"] = self.root
        config["plans"] = [{**config["plans"][0], "strategy": "ma"}]
        with closing(open_journal(self.root / "journal.sqlite3")) as db:
            report = build_report(config, db, NOW, market_loader=lambda *args: result)
        self.assertEqual(report["total_action_amount"], 0)
        self.assertEqual(next(r for r in report["rows"] if r["selected"])["status"], "stored")

    def test_complete_adjustment_versions_not_combined_and_long_history_retained(self):
        old = payload(NOW - timedelta(days=3))
        self.save(old)
        new = payload()
        new["qfq"], new["raw"] = new["qfq"][-130:], new["raw"][-130:]
        new["qfq"] = [[r[0], "5", "5", "6", "4", "1000"] for r in new["qfq"]]
        self.save(new)
        latest = self.store.get_history("etf", "510300")
        self.assertEqual(len(latest["qfq"]), 130)
        self.assertEqual(latest["qfq"][0][2], "5")
        deep = self.store.get_history("etf", "510300", min_rows=500)
        self.assertEqual(len(deep["qfq"]), 550)
        self.assertEqual(deep["qfq"][0][2], "10")
        invalid = payload()
        invalid["raw"] = invalid["raw"][:-1]
        with self.assertRaises(ValueError):
            self.save(invalid)
        self.assertEqual(self.store.get_history("etf", "510300")["qfq"], latest["qfq"])

    def test_valuation_remains_available_after_csv_removed(self):
        path = self.root / "etf_valuations" / "000300.csv"
        path.parent.mkdir()
        frame = pd.DataFrame({"date": ["2026-10-07", "2026-10-08", "2026-10-09"],
                              "available_date": ["2026-10-08", "2026-10-09", "2026-10-10"],
                              "index_code": ["000300"] * 3, "pe_ttm": [12, 11, 10]})
        frame.to_csv(path, index=False)
        settings = {"lookback_years": 10, "min_samples": 3, "min_span_days": 2, "max_age_days": 7}
        before = etf_data.valuation_metrics(path, "000300", date(2026, 10, 11), settings)
        path.unlink()
        after = etf_data.valuation_metrics(path, "000300", date(2026, 10, 11), settings)
        self.assertEqual(before, after)

    def test_stock_history_quote_and_snapshot_survive_service_restart(self):
        item = payload(NOW - timedelta(days=30))
        self.store.put_history("stock", "600001", {"bars": item["raw"]}, {"source": "test"}, item["fetched_at"])
        self.store.put_quote("stock", "600001", {"price": 8, "quote_date": "2026-09-11", "quote_time": "15:00:00"})
        with patch("stock_dynamic_monitor._market_store", return_value=self.store), patch(
            "stock_dynamic_monitor.requests.get", side_effect=AssertionError("No network")
        ):
            bars = monitor.get_stock_hist_data("600001", database_only=True)
            self.assertEqual(len(bars), 550)
            snapshot = build_ma120_dashboard(portfolio=[], watchlist=[{"code": "600001", "name": "test"}],
                hist_loader=lambda _: bars,
                quote_loader=lambda code: monitor.get_stock_realtime_quote(code, database_only=True), now=NOW)
        row = snapshot["watchlist"][0]
        self.assertEqual(row["ma120"], 10)
        self.assertIsNone(row["signal"])
        self.assertTrue(row["data_stale"])
        service = StockDashboardService(loader=lambda: snapshot, database_path=self.store.path)
        service.refresh()
        restarted = StockDashboardService(loader=lambda: {}, database_path=self.store.path)
        self.assertEqual(restarted.current()["snapshot"], snapshot)

    def test_etf_page_can_read_market_database_with_empty_journal(self):
        self.save(payload(NOW - timedelta(days=1)))
        config_path = self.root / "etf_plans.toml"
        template = Path(__file__).resolve().parents[1] / "etf_plans.example.toml"
        config_path.write_bytes(template.read_bytes())
        service = DashboardService(config_path)
        with patch("etf_data.requests.get", side_effect=AssertionError("No network")):
            result = service.dashboard()
        row = next(r for r in result["report"]["rows"] if r["plan_id"] == "csi300" and r["selected"])
        self.assertIsNotNone(row["market"])
        self.assertTrue(row["market"]["data_stale"])
        self.assertEqual(result["report"]["total_action_amount"], 0)

    def test_migration_is_repeatable_and_preserves_files(self):
        folder = self.cache / "2026-10-12"
        folder.mkdir(parents=True)
        source = folder / "510300_test.json"
        source.write_text(json.dumps(payload()))
        first = migrate(self.root, self.store.path)
        second = migrate(self.root, self.store.path)
        self.assertEqual(first["etf_files"], 1)
        self.assertEqual(second["etf_files"], 1)
        self.assertTrue(source.exists())
        with closing(sqlite3.connect(self.store.path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM history_versions").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
