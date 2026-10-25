"""把导出的关注池快照导入到本地数据库（用于新机器 / 重装后还原）。

用法：
    # 默认：只把星标与备注合并到当前关注池里已有的标的上
    uv run python scripts/import_watchlist.py

    # 连关注池的清单一起覆盖（会新增/删除条目）
    uv run python scripts/import_watchlist.py --replace

为什么默认是「合并」而不是「覆盖」：
快照可能比本地库旧。直接覆盖会把你在本机新加的标的删掉，
而且这是无声的 —— 所以危险动作必须显式加 --replace。
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config_store import ConfigurationStore  # noqa: E402
from watchlist_store import _validate_items  # noqa: E402

DEFAULT_INPUT = Path(__file__).resolve().parent.parent / "watchlist.snapshot.json"


def load(source=DEFAULT_INPUT):
    source = Path(source)
    if not source.exists():
        raise FileNotFoundError(f"找不到快照文件：{source}")
    raw = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or "items" not in raw:
        raise ValueError("快照格式不正确，应为 {\"version\": 1, \"items\": [...]}")
    # 复用与线上一致的校验，避免把脏数据写进库
    return _validate_items(raw["items"])


def merge_marks(items, store=None):
    """只把 starred / note 合并到已有条目上，不增删标的。"""
    store = store or ConfigurationStore()
    wanted = {item["code"]: item for item in items}
    applied = []

    def operation(rows):
        updated = []
        for row in rows:
            source = wanted.get(row["code"])
            if source is None:
                updated.append(row)
                continue
            entry = {**row,
                     "starred": bool(source.get("starred")),
                     "note": source.get("note") or ""}
            applied.append(row["code"])
            updated.append(entry)
        return updated

    result = store.mutate_stocks("watchlist", operation)
    missing = sorted(set(wanted) - set(applied))
    return result, applied, missing


def replace_all(items, store=None):
    store = store or ConfigurationStore()
    return store.mutate_stocks("watchlist", lambda _: items)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default=str(DEFAULT_INPUT),
                        help="快照文件，默认仓库根目录 watchlist.snapshot.json")
    parser.add_argument("--replace", action="store_true",
                        help="用快照整体替换关注池（会新增/删除条目，慎用）")
    args = parser.parse_args()

    items = load(args.input)
    if args.replace:
        result = replace_all(items)
        marked = sum(1 for item in result if item.get("starred"))
        print(f"已用快照替换关注池：{len(result)} 只，其中 {marked} 只标了特殊关注。")
        return 0

    result, applied, missing = merge_marks(items)
    marked = sum(1 for item in result if item.get("starred"))
    print(f"已合并星标/备注：命中 {len(applied)} 只，当前关注池共 {len(result)} 只，"
          f"其中 {marked} 只标了特殊关注。")
    if missing:
        print(f"以下 {len(missing)} 只标的在本机关注池里不存在，已跳过：")
        print("  " + " ".join(missing))
        print("如需一并加入，请加 --replace 参数。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
