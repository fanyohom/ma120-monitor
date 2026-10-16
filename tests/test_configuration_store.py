import copy
import http.client
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from http.server import ThreadingHTTPServer
from datetime import date

from config_store import ConfigurationStore
from etf_dca import open_journal, record_trade
from web_app import ConfigurationService, DashboardHandler, StockDashboardService
from unittest.mock import patch


class ConfigurationStoreTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.database = self.root / "data" / "market.sqlite3"
        template = Path(__file__).resolve().parents[1] / "etf_plans.example.toml"
        (self.root / "etf_plans.toml").write_bytes(template.read_bytes())
        (self.root / "watchlist.json").write_text(json.dumps({"version": 1, "items": [
            {"code": "600001", "name": "测试股票"}]}), encoding="utf-8")
        (self.root / "portfolio.json").write_text(json.dumps({"version": 1, "items": [
            {"code": "HK01801", "name": "测试港股", "cost": 20, "shares": 100}]}), encoding="utf-8")
        self.store = ConfigurationStore(self.database, self.root)

    def test_migration_survives_restart_and_legacy_file_removal(self):
        before = {name: (self.root / name).read_bytes() for name in ("watchlist.json", "portfolio.json", "etf_plans.toml")}
        original = self.store.summary()
        self.assertEqual((len(original["watchlist"]), len(original["portfolio"]), len(original["etf"]["plans"])), (1, 1, 4))
        self.assertFalse(original["sample"])
        for name, content in before.items():
            self.assertEqual((self.root / name).read_bytes(), content)
            (self.root / name).unlink()
        restarted = ConfigurationStore(self.database, self.root)
        self.assertEqual(restarted.summary(), original)
        with closing(restarted.connect()) as db:
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM configuration_imports").fetchone()[0], 3)

    def test_empty_watchlist_not_reseeded_and_invalid_edit_rolls_back(self):
        self.store.summary()
        self.store.mutate_stocks("watchlist", lambda _: [])
        self.assertEqual(ConfigurationStore(self.database, self.root).stocks("watchlist"), [])
        before = self.store.summary()
        with self.assertRaises(ValueError):
            self.store.mutate_stocks("portfolio", lambda _: [{"code": "600001", "name": "bad", "cost": -1, "shares": 1}])
        self.assertEqual(self.store.summary(), before)

    def test_default_monitor_and_etf_cli_use_database_without_files(self):
        import stock_dynamic_monitor as monitor
        from etf_dca import load_config
        expected = self.store.summary()
        for name in ("watchlist.json", "portfolio.json", "etf_plans.toml"):
            (self.root / name).unlink()
        with patch("config_store.ConfigurationStore", return_value=self.store):
            self.assertEqual(monitor.load_watchlist(), expected["watchlist"])
            self.assertEqual(monitor.load_portfolio(), expected["portfolio"])
            self.assertEqual(len(load_config()["plans"]), 4)

    def test_invalid_import_atomic_and_source_preserved(self):
        (self.root / "etf_plans.toml").write_text('version = 1\n', encoding="utf-8")
        with self.assertRaises(ValueError):
            self.store.initialize()
        with closing(self.store.connect()) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM watchlist_entries").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM configuration_imports").fetchone()[0], 0)
        self.assertTrue((self.root / "watchlist.json").exists())

    def test_concurrent_updates_do_not_lose_entries(self):
        self.store.initialize()
        def add(code):
            self.store.mutate_stocks("watchlist", lambda rows: rows + [{"code": code, "name": code}])
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(add, ["600002", "600003", "600004", "600005"]))
        self.assertEqual(len(self.store.stocks("watchlist")), 5)

    def test_concurrent_updates_across_store_instances_do_not_lose_entries(self):
        """每个线程各自新建 store，模拟 Web 工作台「每请求一个实例」的真实用法。

        `ConfigurationService.__init__` 每次请求都 new 一个 ConfigurationStore，
        所以互斥必须是**类级**的。若锁挂在实例上，每个请求拿到不同的锁，
        跨请求并发写会互相覆盖，关注池条目静默丢失
        （异常表现为 `attempt to write a readonly database`）。
        上一条测试共用一个实例，恰好绕过了这个缺陷，故两条必须并存。

        用栅栏同步起跑并让每个线程反复进出事务，拉长竞争窗口；
        否则线程太快、窗口太窄，缺陷下也可能侥幸通过。
        """
        self.store.initialize()
        threads, rounds = 4, 20
        barrier = threading.Barrier(threads)

        def add(t):
            store = ConfigurationStore(self.database, self.root)  # 每线程独立实例
            barrier.wait(timeout=10)  # 同步起跑，最大化冲突
            for r in range(rounds):  # 反复进出事务，累积丢数据概率
                code = f"{600100 + t * rounds + r:06d}"  # 每轮唯一，避免被去重拒绝
                store.mutate_stocks("watchlist", lambda rows: rows + [{"code": code, "name": code}])

        with ThreadPoolExecutor(max_workers=threads) as pool:
            list(pool.map(add, range(threads)))
        # 初始 1 条 + threads*rounds 条；丢条目会少于该值
        self.assertEqual(len(ConfigurationStore(self.database, self.root).stocks("watchlist")),
                         1 + threads * rounds)

    def test_plan_money_constraints_and_disable_preserves_identity(self):
        plan = self.store.config()["plans"][0]
        changed = {**plan, "base_amount": 123.45, "strategy": "drawdown"}
        self.store.save_plan(changed, plan["id"])
        with closing(self.store.connect()) as db:
            self.assertEqual(db.execute("SELECT base_amount_cents FROM etf_plans WHERE id=?", (plan["id"],)).fetchone()[0], 12345)
        self.store.disable_plan(plan["id"])
        self.assertFalse(any(p["id"] == plan["id"] for p in self.store.config()["plans"]))
        self.assertTrue(any(p["id"] == plan["id"] for p in self.store.config(True)["plans"]))
        before = self.store.summary()
        for bad in ({**changed, "base_amount": 10.001}, {**changed, "code": "510050"},
                    {**changed, "frequency": "weekly", "weekday": 9}, {**changed, "base_amount": float("inf")}):
            with self.assertRaises(ValueError):
                self.store.save_plan(bad, plan["id"])
            self.assertEqual(self.store.summary(), before)

    def test_initial_holdings_locked_after_fill_and_disable_keeps_ledger(self):
        plan = self.store.config()["plans"][0]
        with closing(open_journal(self.root / "dca_results" / "journal.sqlite3")) as db:
            record_trade(db, plan, "fill-1", "buy", 100, 10, 0, date(2026, 10, 9), date(2026, 10, 12))
        with self.assertRaisesRegex(ValueError, "初始持仓"):
            self.store.save_plan({**plan, "cost": 10, "shares": 100}, plan["id"])
        self.store.disable_plan(plan["id"])
        with closing(open_journal(self.root / "dca_results" / "journal.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM fills").fetchone()[0], 1)

    def test_rules_atomic_audited_and_validated(self):
        original = self.store.summary()
        rules = copy.deepcopy(original["etf"])
        rules["settings"]["ma_days"] = 250
        self.store.save_rules(rules["settings"], rules["valuation"], original["ma120"])
        self.assertEqual(self.store.config()["settings"]["ma_days"], 250)
        with closing(self.store.connect()) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM configuration_changes").fetchone()[0], 2)
        before = self.store.summary()
        rules["valuation"]["high_at"] = 20
        with self.assertRaises(ValueError):
            self.store.save_rules(rules["settings"], rules["valuation"], original["ma120"])
        self.assertEqual(self.store.summary(), before)

    def test_shared_security_name_and_currency(self):
        self.store.summary()
        self.store.mutate_stocks("watchlist", lambda rows: rows + [{"code": "HK01801", "name": "统一名称"}])
        self.assertEqual(self.store.stocks("portfolio")[0]["name"], "统一名称")
        with closing(self.store.connect()) as db:
            self.assertEqual(db.execute("SELECT currency FROM instruments WHERE code='HK01801'").fetchone()[0], "HKD")


class ConfigurationHTTPTests(unittest.TestCase):
    setUp = ConfigurationStoreTests.setUp

    def test_management_crud_validation_and_origin_guard(self):
        service = ConfigurationService(self.store)
        class Handler(DashboardHandler):
            configuration_service = service
            stock_service = StockDashboardService(loader=lambda: {})
            def log_message(self, *_args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 3)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=4)
        self.addCleanup(connection.close)
        def request(method, path, payload=None, headers=None):
            body = json.dumps(payload).encode() if payload is not None else None
            connection.request(method, path, body=body, headers=headers or {"Content-Type": "application/json"})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        self.assertEqual(request("GET", "/api/configuration")[0], 200)
        plan = {"code": "510050", "name": "测试 ETF", "index_code": "000016", "strategy": "ma",
                "base_amount": 100, "max_amount": 200, "frequency": "monthly", "monthday": 10}
        status, result = request("POST", "/api/configuration/plans", plan)
        self.assertEqual((status, len(result["etf"]["plans"])), (200, 5))
        self.assertEqual(request("POST", "/api/configuration/plans", plan)[0], 400)
        self.assertEqual(request("PATCH", "/api/configuration/plans?id=etf-510050", {"strategy": "drawdown"})[0], 200)
        before = self.store.summary()
        item = {"code": "600010", "name": "股票", "cost": 1, "shares": 10}
        self.assertEqual(request("POST", "/api/configuration/portfolio", item,
                                 {"Content-Type": "application/json", "Origin": "https://foreign.example"})[0], 403)
        self.assertEqual(self.store.summary(), before)
        self.assertEqual(request("POST", "/api/configuration/portfolio", item)[0], 200)
        self.assertEqual(request("PATCH", "/api/configuration/portfolio?code=600010", {"cost": 2})[0], 200)
        self.assertEqual(request("DELETE", "/api/configuration/portfolio?code=600010")[0], 200)
        self.assertEqual(request("DELETE", "/api/configuration/plans?id=etf-510050")[0], 200)
        self.assertFalse(next(p for p in self.store.config(True)["plans"] if p["id"] == "etf-510050")["enabled"])
        self.assertEqual(request("PATCH", "/api/configuration/plans?id=missing", {"name": "bad"})[0], 404)


if __name__ == "__main__":
    unittest.main()
