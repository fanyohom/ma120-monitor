import json
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
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


class ConcurrentWriteTests(unittest.TestCase):
    """并发写库回归测试（2026-10-22 缺陷）。

    背景：看板刷新用 4 线程并发拉 41 支标的的日 K，每个线程都要写回
    SQLite。WAL 模式下多个连接同时开写事务时，落败连接会被降级为
    readonly，抛 SQLITE_READONLY("attempt to write a readonly database")。
    该异常原本被 get_stock_hist_data() 的宽 except 吞掉，
    静默回退到旧行情 —— 页面显示正常，实际用的是过期数据，
    当日买入/卖出信号被无声清空，日志里没有任何异常。
    """

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.path = self.root / "market.sqlite3"
        self.store = MarketStore(self.path)

    def _rows(self, code):
        dates = pd.bdate_range(end=date(2026, 10, 21), periods=130)
        return {"bars": [[d.date().isoformat(), "10", "10.5", "11", "9", "1000"]
                         for d in dates]}

    def test_parallel_writes_all_succeed(self):
        """4 线程并发写 40 次，全部应成功落库。"""
        codes = [f"{600000 + i:06d}" for i in range(40)]

        def write(code):
            self.store.put_history("stock", code, self._rows(code), {"source": "test"})

        with ThreadPoolExecutor(max_workers=4) as pool:
            errors = [str(exc) for exc in pool.map(
                lambda code: self._capture(code, write), codes)]
        failures = [e for e in errors if e]
        self.assertEqual(failures, [], f"并发写失败: {failures[:5]}")

        with closing(sqlite3.connect(self.path)) as db:
            stored = db.execute(
                "SELECT COUNT(DISTINCT code) FROM history_versions").fetchone()[0]
        self.assertEqual(stored, len(codes))

    def _capture(self, code, action):
        try:
            action(code)
            return ""
        except Exception as exc:  # noqa: BLE001
            return f"{type(exc).__name__}: {exc}"

    def test_reads_still_work_while_writing(self):
        """写锁不应把读路径也串行化到不可用。"""
        self.store.put_history("stock", "600000", self._rows("600000"), {"source": "test"})
        errors = []

        def reader():
            try:
                for _ in range(20):
                    self.store.get_history("stock", "600000", min_rows=120)
            except Exception as exc:  # noqa: BLE001
                errors.append(str(exc))

        def writer():
            try:
                for i in range(20):
                    self.store.put_history("stock", f"60000{i}", self._rows(f"60000{i}"),
                                           {"source": "test"})
            except Exception as exc:  # noqa: BLE001
                errors.append(str(exc))

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _: None, range(2)))
            futures = [pool.submit(reader), pool.submit(writer), pool.submit(reader),
                       pool.submit(writer)]
            for future in futures:
                future.result()
        self.assertEqual(errors, [])

    def test_store_failure_does_not_discard_freshly_fetched_data(self):
        """写库失败时，get_stock_hist_data 仍应返回刚拉到的数据。

        这是「静默降级」缺陷的关键防线：写库只是持久化，
        失败不应让已成功拉取的数据被丢弃。
        """
        frame = pd.DataFrame(
            [[10.0, 10.5, 11.0, 9.0, 1000.0] for _ in range(130)],
            columns=["open", "close", "high", "low", "volume"],
            index=pd.bdate_range(end=date(2026, 10, 21), periods=130),
        )
        frame.index.name = "date"

        with patch.object(monitor, "_write_cache", side_effect=sqlite3.OperationalError(
                "attempt to write a readonly database")):
            with patch.object(monitor, "_read_cache", return_value=None):
                with patch.object(monitor, "requests") as fake_requests:
                    fake_requests.get.return_value.json.return_value = [
                        {"day": d.date().isoformat(), "open": "10", "close": "10.5",
                         "high": "11", "low": "9", "vol": "1000"}
                        for d in pd.bdate_range(end=date(2026, 10, 21), periods=130)
                    ]
                    result = monitor.get_stock_hist_data("600000", days=130, force=True)
        self.assertEqual(len(result), 130)
        self.assertFalse(result.attrs.get("data_stale", False))
        self.assertEqual(result.iloc[-1]["close"], 10.5)


if __name__ == "__main__":
    unittest.main()
