"""MA120 策略口径的唯一实现（2026-11-09 收敛）。

此前均线、买卖线、首穿判定在 monitor / ma120_web / backfill / today_snapshot /
inspect_one 里各写了一份，出现了「判定用未 round 阈值、zone 用 round 展示线」
以及「inspect_one 用导入时的硬编码常量、主监控用数据库配置」两类分歧。

本模块只负责**与时间点无关**的纯计算：参数、均线、阈值、首次穿越、区间状态。
「算 MA 用哪些 bar」「prev 锚在哪一根」「输出什么文案」仍由各调用方决定，
因为这些是各脚本刻意保留的语义，尤其是 backfill 必须复刻 T-2 偏移。

两条不能改的口径：
1. 判定用未 round 的 ma*factor；round(2) 只用于展示线 / message / zone。
   把判定也换成 round 版会让擦边信号翻转。
2. 策略参数真值来自数据库 ma120_settings（字段名见 load_settings），
   不把 120 / 0.88 / 1.12 写死在离线脚本里。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    ma_days: int = 120
    buy_factor: float = 0.88
    sell_factor: float = 1.12
    history_days: int = 200


DEFAULTS = Settings()


def load_settings(store=None) -> Settings:
    """从配置库读策略参数；配置层不可用时回退默认值。

    延迟 import 是有意的：本模块会被 monitor 在导入期引用，
    若顶层 import config_store 会形成循环依赖。
    """
    try:
        if store is None:
            from config_store import ConfigurationStore
            store = ConfigurationStore()
        raw = store.summary()["ma120"]
        return Settings(
            ma_days=int(raw["ma_days"]),
            buy_factor=float(raw["buy_factor"]),
            sell_factor=float(raw["sell_factor"]),
            history_days=int(raw["history_days"]),
        )
    except Exception:  # noqa: BLE001 - 离线工具不该被配置层拖挂
        return DEFAULTS


def moving_average(closes, window: int):
    """截至最后一根的 window 根收盘均值；样本不足返回 None。"""
    if closes is None or window <= 0:
        return None
    try:
        values = list(closes)
    except TypeError:
        return None
    if len(values) < window:
        return None
    try:
        return sum(float(value) for value in values[-window:]) / window
    except (TypeError, ValueError):
        return None


def levels(ma, settings: Settings = DEFAULTS):
    """返回 (判定买入线, 判定卖出线, 展示买入线, 展示卖出线)。

    前两项**不 round**，用于信号判定；后两项 round(2)，用于展示与 zone。
    ma 无效时四项都是 None。
    """
    if ma is None or ma <= 0:
        return None, None, None, None
    buy = ma * settings.buy_factor
    sell = ma * settings.sell_factor
    return buy, sell, round(buy, 2), round(sell, 2)


def first_cross(price, ma, prev_price, prev_ma, settings: Settings = DEFAULTS) -> str:
    """首次穿越判定，返回 buy / sell / hold。

    prev_price 与 prev_ma **由调用方决定锚在哪一根**：
    - monitor / backfill：prev 是倒数第二根日 K（运行日实为 T-2）
    - 历史回放 / inspect_one：prev 是相邻前一根，且用该根自身的均线
    本函数不猜测时间语义，避免「顺手修正」掉历史行为。
    """
    buy, sell, _, _ = levels(ma, settings)
    if buy is None:
        return "hold"
    prev_buy, prev_sell, _, _ = levels(prev_ma, settings)
    if prev_buy is None:
        return "hold"
    if price < buy and prev_price >= prev_buy:
        return "buy"
    if price > sell and prev_price <= prev_sell:
        return "sell"
    return "hold"


def zone_of(price, ma, settings: Settings = DEFAULTS, rounded: bool = True):
    """区间状态：below_buy / above_sell / between。

    默认用 round(2) 的展示线（与页面 zone 字段一致）；
    rounded=False 时用未 round 的判定阈值（少数离线工具需要）。
    """
    buy, sell, buy_display, sell_display = levels(ma, settings)
    if buy is None:
        return None
    lower, upper = (buy_display, sell_display) if rounded else (buy, sell)
    if price < lower:
        return "below_buy"
    if price > upper:
        return "above_sell"
    return "between"
