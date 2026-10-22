"""「特殊关注」星标 + 备注：存储、迁移、透传与 API 的回归测试。

背景：关注池原本只有 code + name，没有任何承载「标记」的字段。
本次给 watchlist_entries 加了 starred / note 两列，并暴露到看板。

这里重点守住两个容易悄悄坏掉的地方：
1. `_replace_stocks` 是「全删全插」，`_stocks` 漏读字段就会在任意一次
   改名/增删时把标记静默抹掉 —— 用户不会收到任何报错。
2. `CREATE TABLE IF NOT EXISTS` 不会给已有表加列，线上库必须走 ALTER 迁移。
"""

import json
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

import pandas as pd

import ma120_web
from config_store import ConfigurationStore
from watchlist_store import WatchlistError, _validate_items


def _legal_code(index):
    """生成合法 6 位 A 股代码（600xxx 段，避开真实标的）。"""
    return f"{601300 + index:06d}"


class WatchlistStarValidationTests(unittest.TestCase):
    """_validate_items 对新字段的接受度与校验。"""

    def test_optional_fields_are_accepted_and_defaults_omitted(self):
        items = _validate_items([
            {"code": "600900", "name": "长江电力"},
            {"code": "601998", "name": "中信银行", "starred": True, "note": " 股息稳定 "},
            {"code": "000651", "name": "格力电器", "starred": False, "note": ""},
        ])
        # 未标记/空备注不写出，保持 watchlist.json 旧格式兼容
        self.assertEqual(items[0], {"code": "600900", "name": "长江电力"})
        self.assertEqual(items[1], {"code": "601998", "name": "中信银行",
                                    "starred": True, "note": "股息稳定"})
        self.assertEqual(items[2], {"code": "000651", "name": "格力电器"})

    def test_invalid_types_and_overlong_note_are_rejected(self):
        for item, message in (
            ({"code": "600900", "name": "甲", "starred": "yes"}, "布尔"),
            ({"code": "600900", "name": "甲", "note": 123}, "字符串"),
            ({"code": "600900", "name": "甲", "note": "长" * 201}, "200"),
        ):
            with self.subTest(item=item):
                with self.assertRaisesRegex(WatchlistError, message):
                    _validate_items([item])

    def test_unknown_field_is_still_rejected(self):
        """不能因为放宽 schema 就让任意脏字段混进来。"""
        with self.assertRaises(WatchlistError):
            _validate_items([{"code": "600900", "name": "甲", "evil": 1}])

    def test_missing_required_fields_still_rejected(self):
        with self.assertRaises(WatchlistError):
            _validate_items([{"code": "600900"}])


class WatchlistStarStorageTests(unittest.TestCase):
    """数据库层的迁移与持久化。"""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.database = self.root / "data" / "market.sqlite3"
        self.database.parent.mkdir(parents=True, exist_ok=True)

    def store(self):
        return ConfigurationStore(database=self.database, root=self.root)

    def test_migration_adds_columns_to_existing_table(self):
        """模拟线上库：旧结构表 + 已播种的 meta，再让 store 打开。

        meta 里的 `*_initialized` 必须一起造出来 —— 真实生产库有这些标记，
        否则 initialize() 会当成新库去从 stocks.xlsx 重新播种，
        把表里的数据全删掉（那是「一次性播种」的既定语义，不是 bug）。
        """
        with closing(sqlite3.connect(self.database)) as db:
            db.executescript(
                "CREATE TABLE instruments (code TEXT PRIMARY KEY, name TEXT NOT NULL,"
                " kind TEXT NOT NULL, currency TEXT NOT NULL, created_at TEXT NOT NULL,"
                " updated_at TEXT NOT NULL);"
                "CREATE TABLE watchlist_entries (code TEXT PRIMARY KEY,"
                " sort_order INTEGER NOT NULL);"
                "CREATE TABLE configuration_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
                "INSERT INTO instruments VALUES ('600900','长江电力','stock','CNY','t','t');"
                "INSERT INTO watchlist_entries VALUES ('600900',0);"
                "INSERT INTO configuration_meta VALUES ('watchlist_initialized','1'),"
                " ('portfolio_initialized','1'),('etf_initialized','1'),('revision','1');")
            db.commit()

        items = self.store().stocks("watchlist")
        self.assertEqual(len(items), 1, "迁移不能丢数据")
        self.assertEqual(items[0], {"code": "600900", "name": "长江电力",
                                    "starred": False, "note": ""})
        with closing(sqlite3.connect(self.database)) as db:
            columns = [row[1] for row in db.execute("PRAGMA table_info(watchlist_entries)")]
        self.assertIn("starred", columns)
        self.assertIn("note", columns)

    def test_migration_is_idempotent(self):
        ConfigurationStore(database=self.database, root=self.root).stocks("watchlist")
        # 第二次、第三次打开都应正常
        ConfigurationStore(database=self.database, root=self.root).stocks("watchlist")
        ConfigurationStore(database=self.database, root=self.root).stocks("watchlist")
        with closing(sqlite3.connect(self.database)) as db:
            columns = [row[1] for row in db.execute("PRAGMA table_info(watchlist_entries)")]
        self.assertEqual(columns.count("starred"), 1)

    def test_mark_survives_rename_and_add(self):
        """核心回归：全删全插不能把星标抹掉。"""
        store = self.store()
        store.mutate_stocks("watchlist", lambda _: [
            {"code": "600900", "name": "长江电力"},
            {"code": "601998", "name": "中信银行"},
        ])
        store.mutate_stocks("watchlist", lambda rows: [
            {**row, "starred": True, "note": "股息稳定"} if row["code"] == "601998" else row
            for row in rows
        ])

        # 改另一只的名字
        store.mutate_stocks("watchlist", lambda rows: [
            {**row, "name": "长江电力(改)"} if row["code"] == "600900" else row
            for row in rows
        ])
        marked = next(i for i in store.stocks("watchlist") if i["code"] == "601998")
        self.assertTrue(marked["starred"], "改名后星标被抹掉了")
        self.assertEqual(marked["note"], "股息稳定")

        # 新增一只
        store.mutate_stocks("watchlist", lambda rows: rows + [
            {"code": "000651", "name": "格力电器"}])
        marked = next(i for i in store.stocks("watchlist") if i["code"] == "601998")
        self.assertTrue(marked["starred"], "新增标的后星标被抹掉了")

    def test_parallel_marks_do_not_lose_entries(self):
        """并发打星不应丢条目（沿用每线程新建 store 的生产形态）。"""
        self.store().mutate_stocks("watchlist", lambda _: [
            {"code": _legal_code(i), "name": f"标的{i}"} for i in range(8)])

        def mark(index):
            code = _legal_code(index)
            ConfigurationStore(database=self.database, root=self.root).mutate_stocks(
                "watchlist",
                lambda rows: [{**row, "starred": True, "note": f"n{index}"}
                              if row["code"] == code else row for row in rows])

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(mark, range(8)))

        items = self.store().stocks("watchlist")
        self.assertEqual(len(items), 8, "并发打星丢了条目")
        self.assertTrue(all(item["starred"] for item in items),
                        f"有星标丢失: {[i['code'] for i in items if not i['starred']]}")


class WatchlistStarDashboardTests(unittest.TestCase):
    """看板行必须透传 starred/note，且持仓行恒为未标记。"""

    def _frame(self, days=130):
        dates = pd.bdate_range(end="2026-10-21", periods=days)
        frame = pd.DataFrame(
            [[10.0, 10.5, 11.0, 9.0, 1000.0] for _ in range(days)],
            columns=["open", "close", "high", "low", "volume"], index=dates)
        frame.index.name = "date"
        return frame

    def test_rows_carry_starred_and_note(self):
        dashboard = ma120_web.build_ma120_dashboard(
            portfolio=[],
            watchlist=[
                {"code": "601998", "name": "中信银行", "starred": True, "note": "股息稳定"},
                {"code": "600900", "name": "长江电力"},
            ],
            hist_loader=lambda code: self._frame(),
            quote_loader=lambda code: {},
        )
        rows = {row["code"]: row for row in dashboard["watchlist"]}
        self.assertTrue(rows["601998"]["starred"])
        self.assertEqual(rows["601998"]["note"], "股息稳定")
        self.assertFalse(rows["600900"]["starred"])
        self.assertEqual(rows["600900"]["note"], "")

    def test_portfolio_rows_never_starred(self):
        dashboard = ma120_web.build_ma120_dashboard(
            portfolio=[{"code": "601998", "name": "中信银行", "cost": 9.0, "shares": 100,
                        "starred": True, "note": "不该出现"}],
            watchlist=[],
            hist_loader=lambda code: self._frame(),
            quote_loader=lambda code: {},
        )
        row = dashboard["portfolio"][0]
        self.assertFalse(row["starred"], "持仓行不应带星标")
        self.assertEqual(row["note"], "")


if __name__ == "__main__":
    unittest.main()
