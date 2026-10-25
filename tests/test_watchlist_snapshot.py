"""关注池快照的导出/导入回归测试。

存在意义：关注池与「特殊关注」星标都存在 data/market.sqlite3，
而 data/ 在 .gitignore 里。换机器 clone 代码后功能在、数据不在，
表现就像「没这个功能」。所以需要一个能跟着仓库走的小快照。

这里守住三条：
1. 导出的格式能被 _validate_items 接受（否则导入会失败）
2. 合并模式只改星标/备注，绝不增删标的（无声删除最危险）
3. 覆盖模式才允许增删
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config_store import ConfigurationStore  # noqa: E402
from scripts.export_watchlist import export  # noqa: E402
from scripts.import_watchlist import load, merge_marks, replace_all  # noqa: E402
from watchlist_store import _validate_items  # noqa: E402


class SnapshotRoundTripTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.database = self.root / "data" / "market.sqlite3"
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self.snapshot = self.root / "watchlist.snapshot.json"

    def store(self):
        return ConfigurationStore(database=self.database, root=self.root)

    def seed(self, items):
        return self.store().mutate_stocks("watchlist", lambda _: items)

    def test_export_output_is_valid_and_preserves_marks(self):
        self.seed([
            {"code": "601998", "name": "中信银行", "starred": True, "note": "红利·银行"},
            {"code": "000960", "name": "锡业股份"},
        ])
        path, total, marked = export(self.snapshot, store=self.store())
        self.assertEqual((total, marked), (2, 1))

        raw = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(raw["version"], 1)
        # 导出的格式必须能直接过校验（导入依赖这一点）
        validated = _validate_items(raw["items"])
        self.assertTrue(next(i for i in validated if i["code"] == "601998")["starred"])

    def test_merge_only_touches_marks_and_never_deletes(self):
        # 快照里只有 A、B，且 A 带星标
        self.seed([{"code": "600900", "name": "长江电力"}])
        snapshot = [
            {"code": "600900", "name": "长江电力", "starred": True, "note": "红利·水电"},
            {"code": "601988", "name": "本机没有的标的", "starred": True},
        ]
        result, applied, missing = merge_marks(snapshot, store=self.store())
        codes = [item["code"] for item in result]

        self.assertEqual(codes, ["600900"], "合并模式不应新增标的")
        self.assertEqual(applied, ["600900"])
        self.assertEqual(missing, ["601988"])
        self.assertTrue(result[0]["starred"])
        self.assertEqual(result[0]["note"], "红利·水电")

    def test_merge_does_not_remove_star_absent_from_snapshot(self):
        """快照比本地旧时，本机新加的星标不能被无声清掉？"""
        # 注意：合并语义是「以快照为准覆盖星标位」，未被快照覆盖的标的保持原样。
        self.seed([
            {"code": "600900", "name": "长江电力", "starred": True, "note": "本地标的"},
            {"code": "000960", "name": "锡业股份", "starred": True, "note": "快照里没有"},
        ])
        snapshot = [{"code": "600900", "name": "长江电力", "starred": False}]
        result, _, _ = merge_marks(snapshot, store=self.store())
        by_code = {item["code"]: item for item in result}

        self.assertFalse(by_code["600900"].get("starred"), "快照里取消的星标要生效")
        self.assertTrue(by_code["000960"].get("starred"),
                        "快照未提及的标的，本机星标不应被清除")

    def test_replace_mode_can_add_and_remove(self):
        self.seed([{"code": "600900", "name": "长江电力"}])
        snapshot = [{"code": "601998", "name": "中信银行", "starred": True, "note": "红利"}]
        result = replace_all(snapshot, store=self.store())
        self.assertEqual([item["code"] for item in result], ["601998"])
        self.assertTrue(result[0]["starred"])

    def test_load_rejects_missing_and_malformed_files(self):
        with self.assertRaises(FileNotFoundError):
            load(self.snapshot)
        self.snapshot.write_text('{"items": [{"code": "bad", "name": "x"}]}',
                                 encoding="utf-8")
        with self.assertRaises(Exception):
            load(self.snapshot)


if __name__ == "__main__":
    unittest.main()
