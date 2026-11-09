"""在线备份 → 完整性校验 → 恢复，以及轮转策略的回归测试。

这是「三组回归测试」里的第三组：备份不是生成个文件就算完，
必须能真的恢复出一致的数据；轮转也不能误删手工命名的历史备份。
"""
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import backup_market_data as backup_tool  # noqa: E402


class BackupRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.source = self.root / "market.sqlite3"
        with closing(sqlite3.connect(self.source)) as db:
            db.execute("CREATE TABLE watchlist_entries(code TEXT PRIMARY KEY, starred INTEGER)")
            db.executemany("INSERT INTO watchlist_entries VALUES (?,?)",
                           [("000333", 1), ("601318", 0), ("601225", 1)])
            db.commit()

    def _rows(self, path):
        with closing(sqlite3.connect(path)) as db:
            return db.execute("SELECT code,starred FROM watchlist_entries ORDER BY code").fetchall()

    def test_backup_is_consistent_and_restorable(self):
        target = backup_tool.backup(self.source, self.root / "backups" / "market_test.sqlite3")
        with closing(sqlite3.connect(target)) as db:
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        # 模拟恢复：把备份当成主库读，数据必须完整一致。
        self.assertEqual(self._rows(target), self._rows(self.source))

    def test_backup_rejects_existing_target(self):
        target = self.root / "market_test.sqlite3"
        backup_tool.backup(self.source, target)
        with self.assertRaises(ValueError):
            backup_tool.backup(self.source, target)

    def test_prune_keeps_newest_and_never_touches_manual_backups(self):
        folder = self.root / "backups"
        folder.mkdir()
        paths = []
        for index in range(5):
            path = folder / f"market_2026010{index}.sqlite3"
            path.write_bytes(b"x")
            os_time = 1_700_000_000 + index
            import os
            os.utime(path, (os_time, os_time))
            paths.append(path)
        manual = folder / "market.sqlite3.bak.before-prune-20261022"
        manual.write_bytes(b"keep me")
        removed = backup_tool.prune(folder, 2)
        self.assertEqual(removed, 3)
        self.assertTrue(paths[-1].exists())
        self.assertTrue(paths[-2].exists())
        self.assertFalse(paths[0].exists())
        self.assertTrue(manual.exists(), "手工命名的备份不能被轮转删掉")

    def test_prune_disabled_by_default(self):
        folder = self.root / "backups"
        folder.mkdir()
        for index in range(3):
            (folder / f"market_2026010{index}.sqlite3").write_bytes(b"x")
        self.assertEqual(backup_tool.prune(folder, 0), 0)
        self.assertEqual(len(list(folder.glob("market_*.sqlite3"))), 3)


if __name__ == "__main__":
    unittest.main()
