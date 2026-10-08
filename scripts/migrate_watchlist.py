"""Copy the legacy stocks.xlsx watchlist sheet into the local JSON store once."""

import argparse
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import stock_dynamic_monitor as monitor
from watchlist_store import read_watchlist, write_watchlist


def migrate_watchlist(source=None, target=None):
    source = Path(source) if source is not None else Path(monitor.STOCKS_FILE)
    target = Path(target) if target is not None else Path(monitor.WATCHLIST_JSON)
    if target.exists():
        return read_watchlist(target), False
    rows = monitor._read_xlsx_rows(str(source), monitor.WATCHLIST_SHEET)
    items = [{"code": row["code"], "name": row["name"]} for row in rows]
    return write_watchlist(items, target, create_only=True), True


def main(argv=None):
    parser = argparse.ArgumentParser(description="一次性迁移 stocks.xlsx 的关注池到 watchlist.json")
    parser.add_argument("--source", default=monitor.STOCKS_FILE, help="现有 stocks.xlsx 路径")
    parser.add_argument("--target", default=monitor.WATCHLIST_JSON, help="目标 watchlist.json 路径")
    args = parser.parse_args(argv)
    try:
        items, created = migrate_watchlist(args.source, args.target)
    except Exception as exc:
        parser.exit(1, f"迁移失败：{exc}\n")
    status = "已迁移" if created else "已存在，未覆盖"
    print(f"{status}：{len(items)} 只，{args.target}")


if __name__ == "__main__":
    main()
