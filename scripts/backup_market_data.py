"""Create a consistent SQLite backup while the local service is running."""

import argparse
import sqlite3
import sys
from contextlib import closing
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from market_store import DEFAULT_DATABASE, TIMEZONE


def backup(source, target):
    source, target = Path(source).resolve(), Path(target).resolve()
    if not source.exists():
        raise ValueError("行情数据库不存在，请先迁移或刷新数据")
    if source == target or target.exists():
        raise ValueError("备份目标必须是尚不存在的新文件")
    target.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True)) as origin:
        with closing(sqlite3.connect(target)) as destination:
            origin.backup(destination)
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    output = args.output or args.source.parent / "backups" / f"market_{datetime.now(TIMEZONE):%Y%m%d_%H%M%S_%f}.sqlite3"
    print(f"备份已保存：{backup(args.source, output)}")


if __name__ == "__main__":
    main()
