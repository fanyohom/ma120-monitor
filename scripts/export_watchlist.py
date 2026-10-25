"""把关注池（含「特殊关注」星标与备注）导出成可提交的快照文件。

为什么需要它：
关注池和星标都存放在 `data/market.sqlite3`，而 `data/` 在 .gitignore 里
—— 换一台机器 clone 代码后功能在、**数据不在**，看起来就像「没这个功能」。
把关注池导出成仓库里的小 JSON，就能跟着代码一起走。

用法：
    uv run python scripts/export_watchlist.py
    uv run python scripts/export_watchlist.py --output my_watchlist.json
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config_store import ConfigurationStore  # noqa: E402
from watchlist_store import _validate_items  # noqa: E402

DEFAULT_OUTPUT = Path(__file__).resolve().parent.parent / "watchlist.snapshot.json"


def export(output=DEFAULT_OUTPUT, store=None):
    store = store or ConfigurationStore()
    items = store.stocks("watchlist")
    # 用与 watchlist.json 相同的 schema，导入时可以直接复用校验逻辑。
    payload = {"version": 1, "items": items}
    output = Path(output)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                      encoding="utf-8")
    marked = sum(1 for item in items if item.get("starred"))
    return output, len(items), marked


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT),
                        help="输出路径，默认仓库根目录 watchlist.snapshot.json")
    args = parser.parse_args()
    path, total, marked = export(args.output)
    print(f"已导出 {total} 只标的（其中 {marked} 只标了特殊关注）-> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
