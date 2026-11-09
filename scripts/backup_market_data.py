"""Create a consistent SQLite backup while the local service is running.

用法：
    uv run python scripts/backup_market_data.py
    uv run python scripts/backup_market_data.py --keep 7        # 同时轮转旧备份
    uv run python scripts/backup_market_data.py --sync 'rclone copy {file} remote:ma120'

只删脚本自己生成的 ``market_*.sqlite3``；手工命名的备份
（如 market.sqlite3.bak.before-*）一律不动。
"""
import argparse
import shlex
import sqlite3
import subprocess
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
    # SQLite backup API：服务运行期间也能拿到一致快照，直接 cp 主库会漏 WAL。
    with closing(sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True)) as origin:
        with closing(sqlite3.connect(target)) as destination:
            origin.backup(destination)
    return target


def prune(directory, keep):
    """保留最近 keep 份脚本生成的备份，返回删除数。"""
    if keep <= 0:
        return 0
    candidates = sorted(
        directory.glob("market_*.sqlite3"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    stale = candidates[keep:]
    for path in stale:
        print(f"删除旧备份：{path}")
        path.unlink()
    return len(stale)


def sync(command, target):
    """把备份推到异地；{file} 替换为备份路径，不用 shell=True。"""
    if not command:
        return
    argv = shlex.split(command.replace("{file}", str(target)))
    if not argv:
        raise ValueError("--sync 命令为空")
    subprocess.run(argv, check=True)
    print(f"异地同步完成：{argv[0]}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--keep", type=int, default=0,
                        help="保留最近 N 份 market_*.sqlite3，0 表示不删除任何旧备份")
    parser.add_argument("--sync", help="备份成功后执行的异地同步命令，{file} 为备份路径")
    args = parser.parse_args()
    output = args.output or args.source.parent / "backups" / f"market_{datetime.now(TIMEZONE):%Y%m%d_%H%M%S_%f}.sqlite3"
    target = backup(args.source, output)
    print(f"备份已保存：{target}")
    removed = prune(target.parent, args.keep)
    if removed:
        print(f"已清理 {removed} 份旧备份")
    sync(args.sync, target)


if __name__ == "__main__":
    main()
