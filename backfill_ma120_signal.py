"""回算指定交易日（或最近 N 个交易日）MA120 监控「本来会报什么信号」。

用途
----
定时任务偶尔漏跑（例如 2026-09-22 没有执行记录）。本脚本用 SQLite 里的
日 K 反推那一天的判定结果，离线、不联网、不推送。

口径（严格复刻 stock_dynamic_monitor.check_ma120_signal 的逻辑）
----------------------------------------------------------------
运行日 T（11:30 定时任务）时，日 K 最后一根是前一交易日 bar1，倒数第二根是 bar2。
脚本用：

    ma120        = mean(close[bar1-119 : bar1])     # 截至 bar1
    ma120_prev   = mean(close[bar2-119 : bar2])     # 截至 bar2
    price        = T 的实时价（本脚本以 T 的收盘价代替）
    prev_price   = bar2 的收盘价

    卖出信号： price > ma120*1.12  and  prev_price <= ma120_prev*1.12
    买入信号： price < ma120*0.88  and  prev_price >= ma120_prev*0.88

**已知偏差，务必知悉：**
1. 脚本把 prev 锚在 bar2（= T-2）而非 bar1（= T-1），所以一次穿越会在
   连续两个运行日各报一次（历史记录中 2026-09-17/09-18 的重复即此因）。
   本脚本忠实保留该行为，不做"修正"。
2. 本脚本用**收盘价**代替 11:30 的实时价。若某标的当日高低点跨越了阈值而收盘
   回到区间内，会标记为「盘中曾穿越」——这类标的在 11:30 那次跑是否报信号，
   取决于当时的价格，本脚本无法还原。非擦边标的不受影响。

用法
----
    uv run python backfill_ma120_signal.py                    # 最近 1 个交易日
    uv run python backfill_ma120_signal.py --last 5           # 最近 5 个交易日
    uv run python backfill_ma120_signal.py --date 2026-09-22  # 指定日期
    uv run python backfill_ma120_signal.py --last 5 --summary # 只看每日结论
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import date as _date

import stock_dynamic_monitor as monitor

WINDOW = 120
BUY_RATIO = 0.88
SELL_RATIO = 1.12

ABOVE_SELL = ">卖出线"
BELOW_BUY = "<买入线"
INSIDE = "区间内"


@dataclass
class Bar:
    date: str
    open: float
    high: float
    low: float
    close: float


@dataclass
class Result:
    name: str
    code: str
    date: str
    price: float
    ma120: float
    buy_line: float
    sell_line: float
    dev_pct: float
    prev_close: float
    prev_date: str
    prev_buy_line: float
    prev_sell_line: float
    prev_zone: str
    zone: str
    signal: str | None
    intraday: str | None


def load_watchlist() -> list[tuple[str, str]]:
    global WINDOW, BUY_RATIO, SELL_RATIO
    items = monitor.load_watchlist()
    WINDOW, BUY_RATIO, SELL_RATIO = monitor.MA_LONG, monitor.BUY_THRESHOLD, monitor.SELL_THRESHOLD
    return [(item["code"], item["name"]) for item in items]


def load_bars(code: str) -> list[Bar] | None:
    history = monitor.get_stock_hist_data(code, days=800, database_only=True)
    if history.empty:
        return None
    items = [{"date": stamp.isoformat(), **row.to_dict()} for stamp, row in history.iterrows()]
    bars: list[Bar] = []
    for it in items:
        d = str(it.get("date", ""))[:10]
        close = it.get("close")
        if not d or close is None:
            continue
        close = float(close)
        bars.append(Bar(
            date=d,
            open=float(it.get("open") or close),
            high=float(it.get("high") or close),
            low=float(it.get("low") or close),
            close=close,
        ))
    bars.sort(key=lambda b: b.date)
    return bars or None


def zone(price: float, buy_line: float, sell_line: float) -> str:
    if price > sell_line:
        return ABOVE_SELL
    if price < buy_line:
        return BELOW_BUY
    return INSIDE


def analyse(bars: list[Bar], code: str, name: str, target: str) -> Result | None:
    """target 为目标交易日；只用 target 之前的日 K（模拟 11:30 跑）。"""
    past = [b for b in bars if b.date < target]
    today = next((b for b in bars if b.date == target), None)
    if today is None or len(past) < WINDOW + 1:
        return None

    bar1, bar2 = past[-1], past[-2]
    ma120 = sum(b.close for b in past[-WINDOW:]) / WINDOW
    ma120_prev = sum(b.close for b in past[-WINDOW - 1:-1]) / WINDOW

    buy_line, sell_line = round(ma120 * BUY_RATIO, 2), round(ma120 * SELL_RATIO, 2)
    prev_buy = ma120_prev * BUY_RATIO
    prev_sell = ma120_prev * SELL_RATIO

    price = today.close
    prev_close = bar2.close

    signal = None
    if price < ma120 * BUY_RATIO and prev_close >= prev_buy:
        signal = "买入信号"
    elif price > ma120 * SELL_RATIO and prev_close <= prev_sell:
        signal = "卖出信号"

    intraday = None
    if signal is None:
        hits = []
        if today.high > sell_line and prev_close <= prev_sell:
            hits.append(f"最高 {today.high:.2f} 上穿卖出线 {sell_line:.2f}")
        if today.low < buy_line and prev_close >= prev_buy:
            hits.append(f"最低 {today.low:.2f} 下穿买入线 {buy_line:.2f}")
        if hits:
            intraday = "；".join(hits)

    return Result(
        name=name, code=code, date=target, price=price, ma120=ma120,
        buy_line=buy_line, sell_line=sell_line,
        dev_pct=(price - ma120) / ma120 * 100,
        prev_close=prev_close, prev_date=bar2.date,
        prev_buy_line=prev_buy, prev_sell_line=prev_sell,
        prev_zone=zone(prev_close, prev_buy, prev_sell),
        zone=zone(price, buy_line, sell_line),
        signal=signal, intraday=intraday,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="回算 MA120 监控在指定交易日会报的信号。")
    ap.add_argument("--date", action="append", help="目标交易日，可重复")
    ap.add_argument("--last", type=int, help="回算最近 N 个交易日")
    ap.add_argument("--summary", action="store_true", help="只输出每日结论")
    args = ap.parse_args()

    watchlist = load_watchlist()
    bars_by_code: dict[str, list[Bar]] = {}
    for code, _ in watchlist:
        bars = load_bars(code)
        if bars:
            bars_by_code[code] = bars
    if not bars_by_code:
        print("行情数据库为空，请先在工作台刷新数据。",
              file=sys.stderr)
        return 2

    dates: set[str] = set()
    for bars in bars_by_code.values():
        dates.update(b.date for b in bars)
    all_dates = sorted(dates)

    if args.date:
        targets = [d for d in args.date if d <= _date.today().isoformat()]
    else:
        targets = all_dates[-(args.last or 1):]

    grand_signals = 0
    for target in targets:
        if target not in all_dates:
            print(f"\n【{target}】缓存中无此日期的日 K（非交易日或缓存未刷新），跳过。")
            continue

        results, skipped = [], []
        for code, name in watchlist:
            bars = bars_by_code.get(code)
            if not bars:
                skipped.append(name)
                continue
            r = analyse(bars, code, name, target)
            if r:
                results.append(r)
            else:
                skipped.append(name)

        signals = [r for r in results if r.signal]
        intraday = [r for r in results if r.intraday]
        grand_signals += len(signals)

        print("\n" + "=" * 100)
        print(f"回算 {target}　参与标的 {len(results)} 支　"
              f"信号 {len(signals)} 个　盘中曾穿越 {len(intraday)} 支")
        if skipped:
            print(f"未参与 {len(skipped)} 支（无缓存 / 历史不足 121 根）："
                  f"{'、'.join(sorted(set(skipped)))}")
        print("=" * 100)

        if signals:
            print(f"\n{'股票':<8}{'代码':<8}{'收盘':>9}{'买入线':>9}{'卖出线':>9}"
                  f"{'MA120':>9}{'偏离MA120':>11}  信号")
            print("-" * 100)
            for r in signals:
                print(f"{r.name:<8}{r.code:<8}{r.price:>9.2f}{r.buy_line:>9.2f}"
                      f"{r.sell_line:>9.2f}{r.ma120:>9.2f}{r.dev_pct:>10.2f}%"
                      f"  {r.signal}")
        else:
            print("\n无买入/卖出信号。")

        if intraday:
            print("\n盘中曾穿越阈值（收盘未站住，是否报信号取决于 11:30 时点价格）：")
            for r in intraday:
                print(f"  {r.name}({r.code})：{r.intraday}")

        if not args.summary and len(results) <= 60:
            print("\n全部标的明细（按偏离率降序）：")
            print(f"{'股票':<8}{'代码':<8}{'收盘':>9}{'买入线':>9}{'卖出线':>9}"
                  f"{'MA120':>9}{'偏离MA120':>11}  前日({results[0].prev_date if results else ''})状态 → 当日状态")
            print("-" * 100)
            for r in sorted(results, key=lambda x: -x.dev_pct):
                print(f"{r.name:<8}{r.code:<8}{r.price:>9.2f}{r.buy_line:>9.2f}"
                      f"{r.sell_line:>9.2f}{r.ma120:>9.2f}{r.dev_pct:>10.2f}%"
                      f"  {r.prev_zone} → {r.zone}")

    print("\n" + "=" * 100)
    print(f"合计回算 {len(targets)} 个交易日，命中信号 {grand_signals} 个。")
    print("=" * 100)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
