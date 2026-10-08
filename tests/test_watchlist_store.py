import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openpyxl import Workbook

import stock_dynamic_monitor as monitor
import watchlist_store
from scripts.migrate_watchlist import migrate_watchlist
from watchlist_store import WatchlistError, read_watchlist, write_watchlist


class WatchlistStoreTests(unittest.TestCase):
    def test_missing_file_and_normalized_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "watchlist.json"
            self.assertEqual(read_watchlist(path), [])
            items = write_watchlist([
                {"code": "960", "name": " 锡业股份 "},
                {"code": "hk1801", "name": "信达生物"},
            ], path)
            self.assertEqual(items, [
                {"code": "000960", "name": "锡业股份"},
                {"code": "HK01801", "name": "信达生物"},
            ])
            self.assertEqual(read_watchlist(path), items)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["version"], 1)

    def test_rejects_invalid_schema_and_does_not_overwrite_it(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "watchlist.json"
            for payload in (
                "not json",
                '{"version": 2, "items": []}',
                '{"version": 1, "items": [{"code": "600900", "name": ""}]}',
                '{"version": 1, "items": [{"code": "abc", "name": "错误"}]}',
                '{"version": 1, "items": [{"code": "600900", "name": "甲"}, '
                '{"code": "600900", "name": "乙"}]}',
            ):
                with self.subTest(payload=payload):
                    path.write_text(payload, encoding="utf-8")
                    with self.assertRaises(WatchlistError):
                        read_watchlist(path)
                    with self.assertRaises(WatchlistError):
                        write_watchlist([], path)
                    self.assertEqual(path.read_text(encoding="utf-8"), payload)

    def test_rejects_duplicate_after_normalization_before_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "watchlist.json"
            with self.assertRaisesRegex(WatchlistError, "重复"):
                write_watchlist([
                    {"code": "960", "name": "甲"},
                    {"code": "000960", "name": "乙"},
                ], path)
            self.assertFalse(path.exists())

    def test_replacement_keeps_previous_version_as_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "watchlist.json"
            write_watchlist([{"code": "600900", "name": "旧"}], path)
            before = path.read_bytes()
            write_watchlist([{"code": "600900", "name": "新"}], path)
            self.assertEqual(path.with_name("watchlist.json.bak").read_bytes(), before)
            self.assertEqual(read_watchlist(path)[0]["name"], "新")

    def test_failed_replace_preserves_existing_watchlist(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "watchlist.json"
            write_watchlist([{"code": "600900", "name": "旧"}], path)
            before = path.read_bytes()
            original_replace = watchlist_store.os.replace

            def fail_target(source, destination):
                if Path(destination) == path:
                    raise OSError("disk error")
                return original_replace(source, destination)

            with patch.object(watchlist_store.os, "replace", side_effect=fail_target):
                with self.assertRaises(OSError):
                    write_watchlist([{"code": "600900", "name": "新"}], path)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])

    def test_create_only_does_not_overwrite_and_migration_is_repeatable(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "stocks.xlsx"
            target = Path(directory) / "watchlist.json"
            workbook = Workbook()
            workbook.active.title = "watchlist"
            workbook.active.append(["股票代码", "股票名称"])
            workbook.active.append(["960", "锡业股份"])
            workbook.active.append(["HK1801", "信达生物"])
            workbook.save(source)
            workbook_bytes = source.read_bytes()

            items, created = migrate_watchlist(source, target)
            self.assertTrue(created)
            self.assertEqual([item["code"] for item in items], ["000960", "HK01801"])
            self.assertEqual(source.read_bytes(), workbook_bytes)
            with self.assertRaises(WatchlistError):
                write_watchlist([], target, create_only=True)
            repeated, created = migrate_watchlist(source, target)
            self.assertFalse(created)
            self.assertEqual(repeated, items)
            self.assertEqual(source.read_bytes(), workbook_bytes)

    def test_monitor_loads_json_even_without_workbook(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "watchlist.json"
            write_watchlist([{"code": "600900", "name": "长江电力"}], path)
            with patch.object(monitor, "WATCHLIST_JSON", str(path)), \
                    patch.object(monitor, "STOCKS_FILE", str(Path(directory) / "absent.xlsx")):
                self.assertEqual(monitor.load_watchlist(), read_watchlist(path))


if __name__ == "__main__":
    unittest.main()
