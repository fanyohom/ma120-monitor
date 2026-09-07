"""ETF DCA recommendations, explicit trade journal and Feishu reports (no orders)."""

import argparse
import json
import math
import sqlite3
import sys
import tomllib
import uuid
from contextlib import closing
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from zoneinfo import ZoneInfo

from etf_data import DataError, etf_symbol, fetch_market, market_metrics, valuation_metrics
from stock_dynamic_monitor import _display_width, _pad, send_feishu


BASE_DIR = Path(__file__).resolve().parent
STRATEGIES = {"ma": "均线定投", "valuation": "估值定投", "drawdown": "涨跌幅定投"}
STATUSES = {
    "buy": "计划买入", "pause": "暂停定投", "wait": "未到定投日",
    "recorded": "本期已买入", "market_closed": "无当日交易行情",
    "blocked": "数据待补", "small": "不足一手", "compare": "仅供对比",
}


def number(value, label, minimum=0):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a number")
    if not math.isfinite(value) or value < minimum:
        raise ValueError(f"Invalid {label}: {value}")
    return value


def load_config(path: Path) -> dict:
    with path.open("rb") as stream:
        config = tomllib.load(stream)
    if config.get("version") != 1 or not config.get("plans"):
        raise ValueError("Config needs version=1 and at least one [[plans]] entry")
    settings = config["settings"]
    for key in ("ma_days", "lot_size"):
        value = settings[key]
        if type(value) is not int or value <= 0:
            raise ValueError(f"Invalid {key}")
    if settings["ma_days"] > 600:
        raise ValueError("ma_days must be <= 600 for the current data provider")
    bands = settings["bands"]
    if not bands or [b["below"] for b in bands] != sorted({b["below"] for b in bands}):
        raise ValueError("bands must have unique increasing below thresholds")
    for band in bands:
        number(band["below"], "band below", -100)
        number(band["multiplier"], "band multiplier")
    number(settings["above_multiplier"], "above_multiplier")
    valuation = config["valuation"]
    for key in ("lookback_years", "min_samples", "min_span_days", "max_age_days"):
        if type(valuation[key]) is not int or valuation[key] <= 0:
            raise ValueError(f"Invalid valuation {key}")
    number(valuation["buy_below"], "buy_below")
    number(valuation["high_at"], "high_at")
    if not 0 < valuation["buy_below"] < valuation["high_at"] <= 100:
        raise ValueError("Valuation thresholds must satisfy 0 < buy_below < high_at <= 100")
    ids, codes = set(), set()
    for plan in config["plans"]:
        if not isinstance(plan["id"], str) or not plan["id"] or plan["id"] in ids:
            raise ValueError("Plan IDs must be unique nonempty strings")
        etf_symbol(plan["code"])
        if plan["code"] in codes:
            raise ValueError("Use one plan per ETF; --compare compares alternative strategies")
        ids.add(plan["id"])
        codes.add(plan["code"])
        if plan["strategy"] not in STRATEGIES:
            raise ValueError(f"Unknown strategy: {plan['strategy']}")
        number(plan["base_amount"], "base_amount", 0.01)
        number(plan["max_amount"], "max_amount", 0.01)
        number(plan.get("cost", 0), "cost")
        if type(plan.get("shares", 0)) is not int or plan.get("shares", 0) < 0:
            raise ValueError("shares must be a nonnegative integer")
        if plan.get("shares", 0) and plan.get("cost", 0) <= 0:
            raise ValueError("Existing holdings need a positive cost")
        if plan.get("frequency") not in ("daily", "weekly", "monthly"):
            raise ValueError("frequency must be daily, weekly or monthly")
        if plan["frequency"] == "weekly" and (type(plan.get("weekday")) is not int or not 0 <= plan["weekday"] <= 4):
            raise ValueError("weekday must be 0..4 (Monday..Friday)")
        if plan["frequency"] == "monthly" and (type(plan.get("monthday")) is not int or not 1 <= plan["monthday"] <= 28):
            raise ValueError("monthday must be 1..28")
        if not plan.get("index_code") or not plan.get("valuation_csv"):
            raise ValueError("Every ETF needs index_code and valuation_csv")
    config["directory"] = path.resolve().parent
    return config


def period(plan: dict, day: date) -> tuple[str, bool]:
    frequency = plan["frequency"]
    if frequency == "daily":
        return day.isoformat(), True
    if frequency == "weekly":
        monday = day - timedelta(days=day.weekday())
        return monday.isoformat(), day.weekday() >= plan["weekday"]
    return day.strftime("%Y-%m"), day.day >= plan["monthday"]


def open_journal(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE IF NOT EXISTS fills (
            fill_id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, code TEXT NOT NULL,
            side TEXT NOT NULL, shares INTEGER NOT NULL, price REAL NOT NULL,
            fee REAL NOT NULL, trade_date TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS reports (
            run_id TEXT PRIMARY KEY, generated_at TEXT NOT NULL, payload TEXT NOT NULL
        );
    """)
    return db


def position(plan: dict, db: sqlite3.Connection, as_of: date) -> tuple[int, float]:
    shares = plan.get("shares", 0)
    basis = shares * plan.get("cost", 0)
    rows = db.execute("SELECT * FROM fills WHERE plan_id=? AND trade_date<=? ORDER BY trade_date,rowid",
                      (plan["id"], as_of.isoformat()))
    for row in rows:
        if row["code"] != plan["code"]:
            raise ValueError("Plan ETF code changed; use a new plan ID for a different ETF")
        if row["side"] == "buy":
            basis += row["shares"] * row["price"] + row["fee"]
            shares += row["shares"]
        else:
            if row["shares"] > shares:
                raise ValueError("Journal sells exceed recorded holdings")
            basis *= (shares - row["shares"]) / shares
            shares -= row["shares"]
    return shares, basis / shares if shares else 0.0


def record_trade(db, plan, fill_id, side, shares, price, fee, trade_date, today):
    if not fill_id or side not in ("buy", "sell") or type(shares) is not int or shares <= 0:
        raise ValueError("Trade requires fill_id, side, and positive integer shares")
    number(price, "trade price", 0.000001)
    number(fee, "trade fee")
    if trade_date > today or trade_date.weekday() >= 5:
        raise ValueError("Trade date must be a past/current weekday")
    with db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT 1 FROM fills WHERE fill_id=?", (fill_id,)).fetchone():
            raise ValueError("fill_id already recorded; duplicate trade rejected")
        latest = db.execute("SELECT MAX(trade_date) FROM fills WHERE plan_id=?", (plan["id"],)).fetchone()[0]
        if latest and trade_date.isoformat() < latest:
            raise ValueError("Record fills in chronological order")
        held, _ = position(plan, db, today)
        if side == "sell" and shares > held:
            raise ValueError("Cannot record a sale larger than holdings")
        db.execute("INSERT INTO fills VALUES (?,?,?,?,?,?,?,?)",
                   (fill_id, plan["id"], plan["code"], side, shares, price, fee, trade_date.isoformat()))


def already_bought(plan, db, day):
    current_period, _ = period(plan, day)
    dates = db.execute("SELECT trade_date FROM fills WHERE plan_id=? AND side='buy' AND trade_date<=?",
                       (plan["id"], day.isoformat()))
    return any(period(plan, date.fromisoformat(row[0]))[0] == current_period for row in dates)


def multiplier(deviation: float, settings: dict) -> float:
    deviation = round(deviation, 10)
    for band in settings["bands"]:
        if deviation < band["below"]:
            return float(band["multiplier"])
    return float(settings["above_multiplier"])


def evaluate(plan, strategy, market, valuation, valuation_error, cost, held, settings):
    factor = 0.0
    if strategy == "ma":
        if market["ma"] is None:
            raise DataError(market["ma_error"])
        deviation = market["ma_deviation_pct"]
        factor = multiplier(deviation, settings)
        reason = f"MA{market['ma_days']} {market['ma']:.3f} / 偏离 {deviation:+.2f}%"
    elif strategy == "drawdown":
        if held == 0:
            factor, reason = 1.0, "尚无持仓，首期按基础金额"
        elif cost > 0:
            deviation = (market["price"] / cost - 1) * 100
            factor = multiplier(deviation, settings)
            reason = f"成本 {cost:.3f} / 浮盈亏 {deviation:+.2f}%"
        else:
            raise DataError("Existing holdings need a valid cost")
    else:
        if valuation is None:
            raise DataError(valuation_error or "Missing index valuation")
        percentile = valuation["percentile"]
        factor = 1.0 if percentile < settings["buy_below"] else 0.0
        zone = "低估" if factor else "高估，减仓观察" if percentile >= settings["high_at"] else "中性，暂停"
        reason = f"PE {valuation['pe_ttm']:.2f} / 分位 {percentile:.2f}% / {zone} / {valuation['date']}"
    amount = min(plan["base_amount"] * factor, plan["max_amount"])
    amount = float(Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_DOWN))
    lot = settings["lot_size"]
    lots = int(Decimal(str(amount)) / (Decimal(str(market["price"])) * lot))
    shares = lots * lot
    return {"multiplier": factor, "reference_amount": amount, "reference_shares": shares,
            "estimated_cost": round(shares * market["price"], 2), "reason": reason}


def build_report(config, db, now, market_loader=fetch_market):
    rows = []
    directory = config["directory"]
    settings = {**config["settings"], **config["valuation"]}
    for plan in config["plans"]:
        market, market_error, valuation, valuation_error = None, "", None, ""
        try:
            payload = market_loader(plan["code"], settings["ma_days"] + 30, now,
                                    directory / ".stock_cache" / "etf")
            market = market_metrics(payload, settings["ma_days"], now)
        except (ValueError, KeyError, IndexError, OSError) as exc:
            market_error = str(exc)
        try:
            # Daily publication dates cannot tell whether today's value was available intraday.
            valuation = valuation_metrics(directory / plan["valuation_csv"], plan["index_code"],
                                          now.date() - timedelta(days=1), config["valuation"])
        except (ValueError, KeyError, OSError) as exc:
            valuation_error = str(exc)
        held, cost = position(plan, db, now.date())
        period_id, due = period(plan, now.date())
        bought = already_bought(plan, db, now.date())
        for strategy in STRATEGIES:
            selected = strategy == plan["strategy"]
            row = {"plan_id": plan["id"], "code": plan["code"], "name": plan["name"],
                   "strategy": strategy, "selected": selected, "period": period_id,
                   "holdings": held, "cost": cost, "market": market, "valuation": valuation,
                   "action_amount": 0, "action_shares": 0, "action_cost": 0, "reference_amount": 0,
                   "reference_shares": 0, "reason": "", "status": "blocked"}
            try:
                if market is None:
                    raise DataError(market_error)
                row.update(evaluate(plan, strategy, market, valuation, valuation_error, cost, held, settings))
                if not selected:
                    row["status"] = "compare"
                elif bought:
                    row["status"] = "recorded"
                elif not market["tradable"]:
                    row["status"] = "market_closed"
                elif not due:
                    row["status"] = "wait"
                elif row["reference_amount"] == 0:
                    row["status"] = "pause"
                elif row["reference_shares"] == 0:
                    row["status"] = "small"
                else:
                    row["status"] = "buy"
                    row["action_amount"] = row["reference_amount"]
                    row["action_shares"] = row["reference_shares"]
                    row["action_cost"] = row["estimated_cost"]
            except (ValueError, KeyError) as exc:
                row["reason"] = str(exc)
            rows.append(row)
    priorities = {"buy": 0, "blocked": 1, "pause": 2, "small": 3, "recorded": 4,
                  "wait": 5, "market_closed": 6, "compare": 7}
    rows.sort(key=lambda row: (not row["selected"], priorities[row["status"]], row["plan_id"], row["strategy"]))
    return {"run_id": uuid.uuid4().hex, "generated_at": now.isoformat(),
            "mode": "recommendations_only", "config": {k: v for k, v in config.items() if k != "directory"},
            "total_action_amount": round(sum(r["action_amount"] for r in rows), 2),
            "total_action_cost": round(sum(r["action_cost"] for r in rows), 2), "rows": rows}


def display_rows(report, compare=False):
    result = []
    for row in report["rows"]:
        if not compare and not row["selected"]:
            continue
        market = row["market"]
        result.append({
            "status": STATUSES[row["status"]], "etf": f"{row['name']} {row['code']}",
            "strategy": STRATEGIES[row["strategy"]] + (" [生效]" if row["selected"] else " [对比]"),
            "price": f"{market['price']:.3f}" if market else "--",
            "reference": f"{row['reference_amount']:.2f}", "amount": f"{row['action_amount']:.2f}",
            "shares": str(row["action_shares"]), "estimate": f"{row['action_cost']:.2f}", "reason": row["reason"],
            "quote": market["quote_at"] if market else "--",
        })
    return result


COLUMNS = [("status", "状态"), ("etf", "ETF"), ("strategy", "策略"), ("price", "现价"),
           ("reference", "参考预算"), ("amount", "本期预算"), ("shares", "本期份额"),
           ("estimate", "整手估算金额"), ("reason", "依据")]


def render_text(report, compare=False):
    rows = display_rows(report, compare)
    lines = [f"ETF 定投建议 | {report['generated_at']}",
             f"生效计划预算: {report['total_action_amount']:.2f} 元 / 整手估算: {report['total_action_cost']:.2f} 元 (未含佣金；对比策略不累计；未下单)"]
    widths = {key: max(_display_width(label), *(_display_width(row[key]) for row in rows))
              for key, label in COLUMNS}
    lines.append(" | ".join(_pad(label, widths[key]) for key, label in COLUMNS))
    lines.append("-+-".join("-" * widths[key] for key, _ in COLUMNS))
    for row in rows:
        lines.append(" | ".join(_pad(row[key], widths[key]) for key, _ in COLUMNS))
    quote_times = sorted({row["quote"] for row in rows if row["quote"] != "--"})
    if quote_times:
        lines.append(f"行情时间范围: {quote_times[0]} 至 {quote_times[-1]}")
    return "\n".join(lines)


def render_card(report, compare=False):
    rows = display_rows(report, compare)
    return {"msg_type": "interactive", "card": {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": f"ETF 定投建议 · {report['generated_at'][:10]}"},
                   "template": "green" if report["total_action_amount"] else "blue"},
        "elements": [
            {"tag": "markdown", "content": f"生效计划预算 **{report['total_action_amount']:.2f} 元** / 整手估算 **{report['total_action_cost']:.2f} 元**；仅建议，未下单。\n对比策略不重复计入预算；金额未含佣金。"},
            {"tag": "table", "page_size": 10, "row_height": "low",
             "header_style": {"background_style": "grey", "bold": True},
             "columns": [{"name": key, "display_name": label, "data_type": "text",
                          "width": "auto"} for key, label in COLUMNS + [("quote", "行情时间")]], "rows": rows},
        ],
    }}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=BASE_DIR / "etf_plans.toml")
    parser.add_argument("--compare", action="store_true", help="Show all three alternatives per ETF")
    parser.add_argument("--feishu", action="store_true", help="Send the preview to the configured webhook")
    parser.add_argument("--record-trade", metavar="PLAN_ID", help="Record an actual fill; does not place an order")
    parser.add_argument("--side", choices=["buy", "sell"])
    parser.add_argument("--shares", type=int)
    parser.add_argument("--price", type=float)
    parser.add_argument("--fee", type=float, default=0)
    parser.add_argument("--trade-date", type=date.fromisoformat)
    parser.add_argument("--fill-id")
    args = parser.parse_args(argv)
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    try:
        config = load_config(args.config)
        output = config["directory"] / "dca_results"
        with closing(open_journal(output / "journal.sqlite3")) as db:
            if args.record_trade:
                plans = {plan["id"]: plan for plan in config["plans"]}
                if args.record_trade not in plans:
                    raise ValueError("Unknown PLAN_ID")
                if not all(value is not None for value in (args.side, args.shares, args.price, args.fill_id, args.trade_date)):
                    raise ValueError("Recording needs --side --shares --price --fill-id --trade-date")
                record_trade(db, plans[args.record_trade], args.fill_id, args.side, args.shares,
                             args.price, args.fee, args.trade_date, now.date())
                print("成交已登记；持仓成本已更新。本操作未下单。")
                return 0
            if any(value is not None for value in (args.side, args.shares, args.price, args.fill_id, args.trade_date)) or args.fee:
                raise ValueError("Trade fields require --record-trade")
            report = build_report(config, db, now)
            serialized = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
            db.execute("INSERT INTO reports VALUES (?,?,?)", (report["run_id"], report["generated_at"], serialized))
            db.commit()
            stem = f"{now.strftime('%Y%m%d_%H%M%S')}_{report['run_id'][:8]}"
            (output / f"{stem}.json").write_text(serialized, encoding="utf-8")
            text = render_text(report, args.compare)
            (output / f"{stem}.txt").write_text(text, encoding="utf-8")
            print(text)
            print(f"\n结果已保存: {output / (stem + '.json')}")
            if args.feishu and not send_feishu(render_card(report, args.compare)):
                return 1
            return 2 if any(row["selected"] and row["status"] == "blocked" for row in report["rows"]) else 0
    except (ValueError, KeyError, OSError, sqlite3.Error) as exc:
        print(f"ETF 定投失败: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
