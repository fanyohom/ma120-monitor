"""配置层 DDL 收敛的回归测试（2026-11-09）。

原本 ConfigurationStore.connect() 每次都 executescript(建表) + PRAGMA/ALTER，
而 stocks()/summary() 等读路径的 connect() 在 _write_lock 之外。
这与 2026-10-22 修掉的 MarketStore 并发写竞态是同一类问题。
收敛后：connect() 只负责连接，DDL 由 initialize() 持锁一次性完成。
"""
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path

from config_store import ConfigurationStore


class ConfigSchemaTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.database = self.root / "market.sqlite3"

    def _tables(self, store):
        with closing(store.connect()) as db:
            return {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}

    def test_connect_no_longer_runs_config_ddl(self):
        store = ConfigurationStore(self.database, root=self.root)
        # MarketStore 会建自己的行情表，但配置表不该由 connect() 顺手建出来。
        self.assertNotIn("watchlist_entries", self._tables(store))

    def test_initialize_creates_config_tables(self):
        store = ConfigurationStore(self.database, root=self.root)
        store.initialize()
        tables = self._tables(store)
        self.assertIn("watchlist_entries", tables)
        self.assertIn("configuration_meta", tables)

    def test_each_database_initializes_independently(self):
        first = ConfigurationStore(self.database, root=self.root)
        first.initialize()
        second_db = self.root / "second.sqlite3"
        second = ConfigurationStore(second_db, root=self.root)
        second.initialize()
        self.assertIn("watchlist_entries", self._tables(second))

    def test_concurrent_reads_and_writes(self):
        ConfigurationStore(self.database, root=self.root).initialize()
        errors = []
        barrier = threading.Barrier(6)

        def reader():
            try:
                barrier.wait()
                for _ in range(8):
                    ConfigurationStore(self.database, root=self.root).stocks("watchlist")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"reader {type(exc).__name__}: {exc}")

        def writer(index):
            try:
                barrier.wait()
                code = f"90{index:04d}"
                ConfigurationStore(self.database, root=self.root).mutate_stocks(
                    "watchlist", lambda items: [*items, {"code": code, "name": f"测试{index}"}])
            except Exception as exc:  # noqa: BLE001
                errors.append(f"writer {type(exc).__name__}: {exc}")

        threads = [threading.Thread(target=reader) for _ in range(4)]
        threads += [threading.Thread(target=writer, args=(i,)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
