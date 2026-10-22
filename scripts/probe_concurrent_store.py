"""并发写库回归探针：验证 41 支并发拉取时是否出现静默降级。

用法：
    uv run python scripts/probe_concurrent_store.py

背景：MA120 看板刷新时用 4 线程并发拉取 41 支标的的日 K，
每个线程都会把结果写回 SQLite。若并发写失败，异常被
`get_stock_hist_data` 的宽 except 吞掉，函数返回旧数据并打上
`data_stale` 标记 —— 页面看起来"正常"，实际用的是过期行情，
且当日买入/卖出信号会被静默清空。

本探针直接跑真实代码路径，统计降级数量，修复后应恒为 0。
"""
import sys
import traceback
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import stock_dynamic_monitor as monitor  # noqa: E402


def probe(codes, days=800, workers=4):
    """跑真实拉取路径，同时统计两类问题：

    1. 降级（data_stale=True）—— 用户可见：页面用旧行情、当日信号被清空
    2. 写库失败 —— 数据没落库，下次运行要重新下载

    第 2 类在 _store_history() 分离后不会导致降级，但仍需独立监控。
    """
    write_failures = []
    original = monitor._write_cache

    def counting_write(code, frame, fetched_at=None):
        try:
            original(code, frame, fetched_at)
        except Exception as exc:  # noqa: BLE001
            write_failures.append((code, f"{type(exc).__name__}: {exc}", traceback.format_exc()))
            raise

    monitor._write_cache = counting_write
    try:
        def one(code):
            try:
                frame = monitor.get_stock_hist_data(code, days=days, force=True)
                stale = bool(frame.attrs.get("data_stale"))
                return code, ("STALE" if stale else "OK"), None
            except Exception as exc:  # noqa: BLE001
                return code, "EXC", f"{type(exc).__name__}: {exc}"

        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(one, codes))
    finally:
        monitor._write_cache = original
    return results, write_failures


def main():
    codes = [item["code"] for item in monitor.load_watchlist()]
    print(f"标的数 {len(codes)}，并发拉取中 ...")
    results, write_failures = probe(codes)
    tally = Counter(status for _, status, _ in results)
    print("状态统计:", dict(tally))
    print("写库失败:", len(write_failures))

    degraded = [r for r in results if r[1] != "OK"]
    if degraded:
        print("\n降级标的（应为 0 条）:")
        for code, status, detail in degraded:
            print(f"  {code}  {status}  {detail or ''}")
    if write_failures:
        print("\n写库失败明细:")
        for code, detail, tb in write_failures:
            print(f"  {code}  {detail}")
            print("    " + tb.strip().replace("\n", "\n    "))
    if not degraded and not write_failures:
        print("全部 OK：41 支全部拿到新行情，且全部成功落库。")
    return 0 if not degraded and not write_failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
