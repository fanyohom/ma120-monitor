"""单股深度补充：MA120 体系 + 历史穿越复盘。用法: uv run python inspect_one.py 002714"""
import os
import sys

os.environ.setdefault("PYTHONUTF8", "1")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd
from stock_dynamic_monitor import (  # noqa: E402
    MA_LONG, BUY_THRESHOLD, SELL_THRESHOLD,
    get_stock_hist_data, get_stock_realtime_quote, check_ma120_signal,
)

code = sys.argv[1] if len(sys.argv) > 1 else "002714"
name = sys.argv[2] if len(sys.argv) > 2 else ""
# 第三个参数：历史复盘要拉的日 K 根数（默认取项目口径 200 根）
days = int(sys.argv[3]) if len(sys.argv) > 3 else None

if days:
    # 走新浪日 K 直取长历史，绕过 DATA_DAYS=200 的缓存窗口
    import requests
    from stock_dynamic_monitor import _sina_symbol
    resp = requests.get(
        "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        "CN_MarketData.getKLineData",
        params={"symbol": _sina_symbol(code), "scale": 240, "ma": "no", "datalen": days},
        timeout=20,
    )
    df = pd.DataFrame(resp.json()).rename(columns={"day": "date", "vol": "volume"})
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
else:
    df = get_stock_hist_data(code)

if df is None or len(df) < MA_LONG:
    print("日K不足")
    sys.exit(1)

# 两条路径结构不一致：网络拉取的 df 已 set_index("date")，
# 而 .stock_cache 反序列化出来的 date 仍是普通列、index 是 RangeIndex。这里统一。
# 统一为「date 是普通列」的结构，避免上述不一致影响下游取值
if df.index.name == "date" or not isinstance(df.index, pd.RangeIndex):
    df = df.reset_index()
df["date"] = pd.to_datetime(df["date"])
df["ma120"] = df["close"].rolling(MA_LONG).mean()
df["buy"] = df["ma120"] * BUY_THRESHOLD
df["sell"] = df["ma120"] * SELL_THRESHOLD

q = get_stock_realtime_quote(code)
price = q.get("price") or float(df.iloc[-1]["close"])
last = df.iloc[-1]
ma120 = float(last["ma120"])
name = q.get("name") or name or code

print(f"\n{'=' * 70}")
print(f"{name}({code})  现价 {price:.2f}   行情时间 {q.get('quote_time', '')}")
print(f"昨收基准日 {str(last["date"])[:10]}  昨收 {float(last['close']):.2f}  "
      f"今日涨跌 {(price - float(last['close'])) / float(last['close']) * 100:+.2f}%")
print(f"MA120 {ma120:.2f}   偏离 {(price - ma120) / ma120 * 100:+.2f}%")
print(f"买入线 {ma120 * BUY_THRESHOLD:.2f}   距 {(price - ma120 * BUY_THRESHOLD) / (ma120 * BUY_THRESHOLD) * 100:+.2f}%")
print(f"卖出线 {ma120 * SELL_THRESHOLD:.2f}   距 {(price - ma120 * SELL_THRESHOLD) / (ma120 * SELL_THRESHOLD) * 100:+.2f}%")
print(f"{'=' * 70}")

# 区间位置：过去 250 根的现价分位
window = df.tail(250)
hi = float(window["high"].max()) if "high" in window else float(window["close"].max())
lo = float(window["low"].min()) if "low" in window else float(window["close"].min())
print(f"近 250 交易日 最高 {hi:.2f} / 最低 {lo:.2f} / "
      f"现价分位 {(price - lo) / (hi - lo) * 100:.1f}%")

for n in (5, 10, 20, 60, 120, 250):
    if len(df) > n:
        old = float(df.iloc[-1 - n]["close"])
        print(f"  近 {n:>3} 日涨跌 {(price - old) / old * 100:+.2f}%  (基准 {str(df.iloc[-1-n]["date"])[:10]} {old:.2f})")

# 今日信号判定
sig = check_ma120_signal(df, price)
print(f"\n信号判定: {sig}")

# 历史穿越复盘
print(f"\n{'=' * 70}")
print("历史穿越复盘（近 250 根，按信号日）")
print(f"{'=' * 70}")
print(f"{'日期':<12}{'收盘':>9}{'买入线':>9}{'卖出线':>9}{'偏离MA120':>11}  类型")
sub = df.dropna(subset=["ma120", "buy", "sell"]).reset_index(drop=True)
prev_state = 0
for i in range(1, len(sub)):
    r, p = sub.iloc[i], sub.iloc[i - 1]
    c, b, s = float(r["close"]), float(r["buy"]), float(r["sell"])
    pc, pb, ps = float(p["close"]), float(p["buy"]), float(p["sell"])
    kind = None
    if c < b and pc >= pb:
        kind = "买入信号(跌破买入线)"
    elif c > s and pc <= ps:
        kind = "卖出信号(涨破卖出线)"
    if kind:
        nm = (c - float(r["ma120"])) / float(r["ma120"]) * 100
        fwd = []
        for h in (20, 60):
            if i + h < len(sub):
                later = float(sub.iloc[i + h]["close"])
                fwd.append(f"{h}日{(later - c) / c * 100:+.1f}%")
        print(f"{str(r['date'])[:10]:<12}{c:>9.2f}{b:>9.2f}{s:>9.2f}{nm:>10.2f}%  "
              f"{kind}   {'  '.join(fwd)}")

# 信号后胜率汇总
print(f"\n{'=' * 70}")
print("信号后表现汇总")
print(f"{'=' * 70}")
for label, want in (("买入信号", "买入信号"), ("卖出信号", "卖出信号")):
    hits = []
    for i in range(1, len(sub)):
        r, p = sub.iloc[i], sub.iloc[i - 1]
        c, b_, s_ = float(r["close"]), float(r["buy"]), float(r["sell"])
        pc, pb, ps = float(p["close"]), float(p["buy"]), float(p["sell"])
        fired = (c < b_ and pc >= pb) if want == "买入信号" else (c > s_ and pc <= ps)
        if not fired:
            continue
        row = {"date": str(r["date"])[:10], "c": c}
        for h in (20, 60):
            row[h] = (float(sub.iloc[i + h]["close"]) - c) / c * 100 if i + h < len(sub) else None
        hits.append(row)
    for h in (20, 60):
        vals = [x[h] for x in hits if x[h] is not None]
        if vals:
            win = sum(1 for v in vals if (v > 0) == (want == "买入信号"))
            print(f"  {label} 后 {h:>2} 日：样本 {len(vals)}，"
                  f"平均 {sum(vals) / len(vals):+.2f}%，方向正确 {win}/{len(vals)}"
                  f"（{win / len(vals) * 100:.0f}%）")
