"""把关注池日 K 全量重刷为前复权口径（数据源切换配套维护，可重复运行）。

2026-11-09 之前 A 股日 K 来自新浪（不复权），除权分红后历史价不回溯调整，
会系统性抬高 MA120。代码切到腾讯 fqkline(qfq) 后，库里已有的十几万根 bar
仍是旧口径，必须逐支重刷，否则信号依旧是错的。

用法：
    uv run python scripts/rebuild_history.py --dry-run
    uv run python scripts/rebuild_history.py                    # 自动在线备份 + 全量重刷
    uv run python scripts/rebuild_history.py --codes 000333,600519
    uv run python scripts/rebuild_history.py --no-backup --keep-versions 2

重刷成功后默认把该标的的历史版本清到只剩 1 份，避免任何读取路径再取到
旧口径价格；网络失败或返回残缺（不足 MA120 根）的标的会跳过，保留旧数据。
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import stock_dynamic_monitor as monitor  # noqa: E402
import backup_market_data  # noqa: E402
from config_store import ConfigurationStore  # noqa: E402
from market_store import DEFAULT_DATABASE, MarketStore, TIMEZONE  # noqa: E402


def watchlist_codes() -> list[str]:
    return [item["code"] for item in ConfigurationStore().stocks("watchlist")]


def default_backup_path() -> Path:
    stamp = datetime.now(TIMEZONE).strftime("%Y%m%d_%H%M%S")
    return Path(DEFAULT_DATABASE).parent / "backups" / f"market_before_qfq_rebuild_{stamp}.sqlite3"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codes", help="只刷指定代码，逗号分隔；默认整个关注池")
    parser.add_argument("--days", type=int, default=800, help="每支拉取的历史根数")
    parser.add_argument("--dry-run", action="store_true", help="只列出待刷标的，不联网不写库")
    parser.add_argument("--no-backup", action="store_true", help="跳过重刷前的在线备份（不推荐）")
    parser.add_argument("--keep-versions", type=int, default=1,
                        help="成功后该标的保留的版本数，默认 1（清掉旧口径版本）")
    parser.add_argument("--sleep", type=float, default=0.12, help="每支之间的间隔秒数")
    args = parser.parse_args()

    codes = ([c.strip() for c in args.codes.split(",") if c.strip()]
             if args.codes else watchlist_codes())
    print(f"待重刷 {len(codes)} 支标的 · days={args.days}")
    if args.dry_run:
        print(" ".join(codes))
        return 0

    if not args.no_backup:
        print(f"在线备份 → {backup_market_data.backup(DEFAULT_DATABASE, default_backup_path())}")

    store = MarketStore(DEFAULT_DATABASE)
    ok: list[tuple[str, int, str, int]] = []
    failed: list[tuple[str, str]] = []
    for index, code in enumerate(codes, 1):
        try:
            frame = monitor.get_stock_hist_data(
                code, days=args.days, force=True, replace_bars=True)
            rows = 0 if frame is None else len(frame)
            if frame is None or frame.empty or rows < monitor.MA_LONG:
                raise ValueError(f"返回 {rows} 根，不足 MA{monitor.MA_LONG}")
            removed = store.prune_history(
                keep=args.keep_versions, kind="stock", code=code)
            last = frame.index.max().date().isoformat()
            ok.append((code, rows, last, removed))
            print(f"[{index}/{len(codes)}] {code} OK {rows} 根 · 末根 {last} · 清理旧版本 {removed}")
        except Exception as exc:  # noqa: BLE001 - 单支失败不应中断整批
            reason = f"{type(exc).__name__}: {exc}"
            failed.append((code, reason))
            print(f"[{index}/{len(codes)}] {code} FAIL {reason}", file=sys.stderr)
        if args.sleep:
            time.sleep(args.sleep)

    print(f"\n完成：成功 {len(ok)} / 失败 {len(failed)}")
    if failed:
        for code, reason in failed:
            print(f"  {code}: {reason}", file=sys.stderr)
        print("失败标的仍保留旧数据，请检查网络后用 --codes 只补失败项重跑。", file=sys.stderr)
        return 1
    print("全部标的已切换为腾讯前复权口径。请重启 Web 服务或调用 "
          "POST /api/ma120/refresh 重建看板快照。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
