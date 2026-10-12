"""今日实时走势快照（盘中随时可跑，比主监控多一列「今日涨跌幅」）。

用法
----
    uv run python today_snapshot.py

说明
----
- 现价走实时行情接口；MA120 / 买入线 / 卖出线读取 SQLite 的已完成日 K，
  最后一根是**上一交易日**收盘，所以阈值不含今日，只有现价是实时的。
- 今日涨跌幅 = (现价 - 上一交易日收盘) / 上一交易日收盘。
- 只读、**不推送飞书**。正式信号仍以 11:30 定时任务为准。
- 末尾「备注」列会标出：高于卖出线 / 低于买入线 / 贴近买入线或卖出线（±1.5% 内）。
  盘中擦边（如 ±0.2%）不可靠，收盘后需复核。
- 关注池及规则读取 SQLite；历史数据不失效删除，需更新时在工作台刷新。
"""
import os
import sys


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from stock_dynamic_monitor import (
    MA_LONG, BUY_THRESHOLD, SELL_THRESHOLD, get_stock_realtime_quote, get_stock_hist_data, load_watchlist,
)



def last_bars(code, n):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    history = get_stock_hist_data(code, database_only=True)
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    bars = sorted(
        ((stamp.date().isoformat(), float(row["close"])) for stamp, row in history.iterrows() if stamp.date() < today),
        key=lambda x: x[0],
    )
    return bars[-n:] if len(bars) >= n else None


stocks = [(item["code"], item["name"]) for item in load_watchlist()]
import stock_dynamic_monitor as monitor
MA_LONG, BUY_THRESHOLD, SELL_THRESHOLD = monitor.MA_LONG, monitor.BUY_THRESHOLD, monitor.SELL_THRESHOLD

rows = []
for code, name in stocks:
    tail = last_bars(code, MA_LONG)
    if not tail:
        print(f"  跳过 {name}({code})：日K不足 {MA_LONG} 根")
        continue
    prev_close = tail[-1][1]
    prev_date = tail[-1][0]
    ma120 = sum(c for _, c in tail) / MA_LONG
    q = get_stock_realtime_quote(code)
    price = q.get("price") if q else None
    live = price is not None and price > 0
    if not live:
        price = prev_close
    chg = (price - prev_close) / prev_close * 100 if prev_close else 0.0
    rows.append({
        "name": name, "code": code, "price": price, "chg": chg,
        "ma120": ma120, "dev": (price - ma120) / ma120 * 100,
        "buy": ma120 * BUY_THRESHOLD, "sell": ma120 * SELL_THRESHOLD,
        "live": live, "prev_close": prev_close, "prev_date": prev_date,
        "t": (q or {}).get("quote_time", ""),
    })

rows.sort(key=lambda r: -r["chg"])
ma_ref = rows[0]["ma120"] if rows else 0
prev_d = rows[0]["prev_date"] if rows else ""
live_t = next((r["t"] for r in rows if r["t"]), "")
n_live = sum(1 for r in rows if r["live"])

print(f"\n{'=' * 108}")
print(f"今日实时走势快照 · {len(rows)} 支 · 昨收基准 {prev_d} · "
      f"实时价 {n_live}/{len(rows)} 支" + (f" · 行情时间 {live_t}" if live_t else ""))
print(f"{'=' * 108}\n")
print(f"{'股票':<8}{'代码':<8}{'现价':>9}{'今日涨跌':>10}{'MA120':>9}"
      f"{'偏离MA120':>11}{'买入线':>9}{'距买入线':>10}{'卖出线':>9}  备注")
print("-" * 108)

for r in rows:
    buy_pct = (r["price"] - r["buy"]) / r["buy"] * 100
    note = []
    if r["price"] > r["sell"]:
        note.append("高于卖出线")
    elif r["price"] < r["buy"]:
        note.append("低于买入线")
    if -1.5 < buy_pct < 1.5:
        note.append(f"贴近买入线({buy_pct:+.2f}%)")
    sell_pct = (r["price"] - r["sell"]) / r["sell"] * 100
    if -1.5 < sell_pct < 1.5:
        note.append(f"贴近卖出线({sell_pct:+.2f}%)")
    if not r["live"]:
        note.append("无实时价(用昨收)")
    print(f"{r['name']:<8}{r['code']:<8}{r['price']:>9.2f}{r['chg']:>9.2f}%"
          f"{r['ma120']:>9.2f}{r['dev']:>10.2f}%{r['buy']:>9.2f}{buy_pct:>9.2f}%"
          f"{r['sell']:>9.2f}  {'、'.join(note) if note else ''}")

up = [r for r in rows if r["chg"] > 0]
down = [r for r in rows if r["chg"] < 0]
flat = [r for r in rows if r["chg"] == 0]
avg = sum(r["chg"] for r in rows) / len(rows) if rows else 0
print(f"\n{'=' * 108}")
print(f"涨 {len(up)} 家 · 跌 {len(down)} 家 · 平 {len(flat)} 家 · 平均 {avg:+.2f}%")
if up:
    top = "、".join(f"{r['name']} {r['chg']:+.2f}%" for r in up[:3])
    print(f"最强：{top}")
if down:
    bottom = "、".join(f"{r['name']} {r['chg']:+.2f}%" for r in down[-3:][::-1])
    print(f"最弱：{bottom}")
print(f"{'=' * 108}")
