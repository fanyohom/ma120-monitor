"""Validation shared by legacy imports and database configuration edits."""

import math
from etf_data import etf_symbol


STRATEGY_KEYS = ("ma", "valuation", "drawdown")


def number(value, label, minimum=0):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} 必须是数字")
    if not math.isfinite(value) or value < minimum:
        raise ValueError(f"{label} 数值无效：{value}")
    return value


def validate_config(config, directory, allow_empty=False):
    if type(config.get("version")) is not int or config.get("version") != 1 or (not config.get("plans") and not allow_empty):
        raise ValueError("配置需包含 version=1 和定投计划")
    settings = config["settings"]
    for key in ("ma_days", "lot_size"):
        if type(settings[key]) is not int or settings[key] <= 0:
            raise ValueError(f"{key} 必须是正整数")
    if settings["ma_days"] > 600:
        raise ValueError("ma_days 不能超过 600")
    bands = settings["bands"]
    if not bands or [b["below"] for b in bands] != sorted({b["below"] for b in bands}):
        raise ValueError("金额阶梯上界须唯一且递增")
    for band in bands:
        number(band["below"], "阶梯上界", -100)
        number(band["multiplier"], "阶梯倍数")
    number(settings["above_multiplier"], "最高档倍数")
    valuation = config["valuation"]
    for key in ("lookback_years", "min_samples", "min_span_days", "max_age_days"):
        if type(valuation[key]) is not int or valuation[key] <= 0:
            raise ValueError(f"{key} 必须是正整数")
    number(valuation["buy_below"], "低估分位")
    number(valuation["high_at"], "高估分位")
    if not 0 < valuation["buy_below"] < valuation["high_at"] <= 100:
        raise ValueError("分位须满足 0 < 低估 < 高估 <= 100")
    ids, codes = set(), set()
    for plan in config["plans"]:
        if not isinstance(plan["id"], str) or not plan["id"].strip() or plan["id"] in ids:
            raise ValueError("计划 ID 必须是唯一非空字符串")
        if not isinstance(plan.get("name"), str) or not plan["name"].strip():
            raise ValueError("ETF 名称不能为空")
        etf_symbol(plan["code"])
        if plan["code"] in codes:
            raise ValueError("同一 ETF 只能有一个计划")
        ids.add(plan["id"])
        codes.add(plan["code"])
        if plan["strategy"] not in STRATEGY_KEYS:
            raise ValueError("未知定投策略")
        if not isinstance(plan.get("strategy_reason", ""), str):
            raise ValueError("策略说明须为文字")
        number(plan["base_amount"], "基础金额", 0.01)
        number(plan["max_amount"], "单期上限", 0.01)
        number(plan.get("cost", 0), "初始成本")
        if type(plan.get("shares", 0)) is not int or plan.get("shares", 0) < 0:
            raise ValueError("初始份额须为非负整数")
        if plan.get("shares", 0) and plan.get("cost", 0) <= 0:
            raise ValueError("已有持仓需要有效成本")
        if plan.get("enabled", True) not in (True, False) or type(plan.get("enabled", True)) is not bool:
            raise ValueError("enabled 须为布尔值")
        if plan["frequency"] not in ("daily", "weekly", "monthly"):
            raise ValueError("定投周期无效")
        if plan["frequency"] == "weekly" and (type(plan.get("weekday")) is not int or not 0 <= plan["weekday"] <= 4):
            raise ValueError("每周定投日须为 0..4")
        if plan["frequency"] == "monthly" and (type(plan.get("monthday")) is not int or not 1 <= plan["monthday"] <= 28):
            raise ValueError("每月定投日须为 1..28")
        if not isinstance(plan.get("index_code"), str) or not plan["index_code"].strip() or not plan.get("valuation_csv"):
            raise ValueError("计划需要跟踪指数和估值来源标识")
    config["directory"] = directory
    return config
