import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openpyxl import Workbook

import portfolio_store
from portfolio_store import PortfolioError, read_portfolio, write_portfolio
from scripts.migrate_portfolio import migrate_portfolio


class PortfolioStoreTests(unittest.TestCase):
    def test_missing_file_fails_clearly_and_round_trip_normalizes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "portfolio.json"
            with self.assertRaisesRegex(PortfolioError, "未找到 portfolio.json"):
                read_portfolio(path)
            items = write_portfolio([
                {"code": "960", "name": " 锡业股份 ", "cost": 10, "shares": 100},
                {"code": "hk1801", "name": "信达生物", "cost": 0.0, "shares": 0},
            ], path)
            self.assertEqual(items, [
                {"code": "000960", "name": "锡业股份", "cost": 10.0, "shares": 100.0},
                {"code": "HK01801", "name": "信达生物", "cost": 0.0, "shares": 0.0},
            ])
            self.assertEqual(read_portfolio(path), items)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["version"], 1)

    def test_rejects_invalid_schema_and_does_not_overwrite_it(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "portfolio.json"
            payloads = (
                "not json",
                '{"version": 2, "items": []}',
                '{"version": true, "items": []}',
                '{"version": 1, "items": [{"code": "600900", "name": "长江电力"}]}',
                '{"version": 1, "items": [{"code": "600900", "name": "", '
                '"cost": 1, "shares": 2}]}',
                '{"version": 1, "items": [{"code": "600900", "name": "长江电力", '
                '"cost": -1, "shares": 2}]}',
                '{"version": 1, "items": [{"code": "600900", "name": "长江电力", '
                '"cost": NaN, "shares": 2}]}',
            )
            for payload in payloads:
                with self.subTest(payload=payload):
                    path.write_text(payload, encoding="utf-8")
                    with self.assertRaises(PortfolioError):
                        read_portfolio(path)
                    with self.assertRaises(PortfolioError):
                        write_portfolio([], path)
                    self.assertEqual(path.read_text(encoding="utf-8"), payload)

    def test_rejects_bad_numbers_and_duplicate_codes_before_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "portfolio.json"
            base = {"code": "600900", "name": "长江电力", "cost": 1, "shares": 2}
            for field, value in (("cost", True), ("shares", "2"), ("cost", float("inf")),
                                 ("shares", -1)):
                with self.subTest(field=field, value=value):
                    with self.assertRaises(PortfolioError):
                        write_portfolio([{**base, field: value}], path)
                    self.assertFalse(path.exists())
            with self.assertRaisesRegex(PortfolioError, "重复"):
                write_portfolio([base, {**base, "code": "600900.0"}], path)
            self.assertFalse(path.exists())

    def test_replacement_backs_up_prior_bytes_and_failed_replace_preserves_target(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "portfolio.json"
            old = [{"code": "600900", "name": "旧", "cost": 1, "shares": 2}]
            write_portfolio(old, path)
            before = path.read_bytes()
            write_portfolio([{**old[0], "name": "新"}], path)
            self.assertEqual(path.with_name("portfolio.json.bak").read_bytes(), before)
            current = path.read_bytes()
            original_replace = portfolio_store.os.replace

            def fail_target(source, destination):
                if Path(destination) == path:
                    raise OSError("disk error")
                return original_replace(source, destination)

            with patch.object(portfolio_store.os, "replace", side_effect=fail_target):
                with self.assertRaises(OSError):
                    write_portfolio([{**old[0], "name": "又改"}], path)
            self.assertEqual(path.read_bytes(), current)
            self.assertEqual(list(Path(directory).glob(".*.tmp")), [])

    def test_migration_is_create_only_and_rejects_bad_legacy_numbers(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "stocks.xlsx"
            target = Path(directory) / "portfolio.json"
            workbook = Workbook()
            workbook.active.title = "portfolio"
            workbook.active.append(["股票代码", "股票名称", "成本价", "持仓数量"])
            workbook.active.append(["960", "锡业股份", 12.5, 100])
            workbook.active.append(["HK1801", "信达生物", None, None])
            workbook.save(source)
            workbook_bytes = source.read_bytes()

            items, created = migrate_portfolio(source, target)
            self.assertTrue(created)
            self.assertEqual([item["code"] for item in items], ["000960", "HK01801"])
            self.assertEqual(items[1]["cost"], 0.0)
            self.assertEqual(source.read_bytes(), workbook_bytes)
            with self.assertRaises(PortfolioError):
                write_portfolio([], target, create_only=True)
            repeated, created = migrate_portfolio(source, target)
            self.assertFalse(created)
            self.assertEqual(repeated, items)
            self.assertEqual(source.read_bytes(), workbook_bytes)

            target.unlink()
            workbook.active["C2"] = "not a number"
            workbook.save(source)
            with self.assertRaisesRegex(PortfolioError, "不是有效数字"):
                migrate_portfolio(source, target)
            self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
