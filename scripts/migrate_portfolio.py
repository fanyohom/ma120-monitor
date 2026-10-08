"""Copy the legacy stocks.xlsx portfolio sheet into the local JSON store once."""

import argparse
import math
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import stock_dynamic_monitor as monitor
from portfolio_store import DEFAULT_PATH, PortfolioError, read_portfolio, write_portfolio


def _legacy_number(value, field, row_number):
    if value in (None, ""):
        return 0.0
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise PortfolioError(f"Excel 第 {row_number} 项 {field} 不是有效数字：{value}") from exc
    if not math.isfinite(number) or number < 0:
        raise PortfolioError(f"Excel 第 {row_number} 项 {field} 必须是有限的非负数字")
    return number


def migrate_portfolio(source=None, target=None):
    source = Path(source) if source is not None else Path(monitor.STOCKS_FILE)
    target = Path(target) if target is not None else DEFAULT_PATH
    if target.exists():
        return read_portfolio(target), False
    rows = monitor._read_xlsx_rows(str(source), monitor.PORTFOLIO_SHEET)
    items = [
        {
            "code": row.get("code", ""),
            "name": row.get("name", ""),
            "cost": _legacy_number(row.get("cost"), "cost", index),
            "shares": _legacy_number(row.get("shares"), "shares", index),
        }
        for index, row in enumerate(rows, start=1)
    ]
    return write_portfolio(items, target, create_only=True), True


def main(argv=None):
    parser = argparse.ArgumentParser(description="一次性迁移 stocks.xlsx 的持仓到 portfolio.json")
    parser.add_argument("--source", default=monitor.STOCKS_FILE, help="现有 stocks.xlsx 路径")
    parser.add_argument("--target", default=str(DEFAULT_PATH), help="目标 portfolio.json 路径")
    args = parser.parse_args(argv)
    try:
        items, created = migrate_portfolio(args.source, args.target)
    except Exception as exc:
        parser.exit(1, f"迁移失败：{exc}\n")
    status = "已迁移" if created else "已存在，未覆盖"
    print(f"{status}：{len(items)} 只，{args.target}")


if __name__ == "__main__":
    main()
