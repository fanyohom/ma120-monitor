"""MA120 策略口径收敛后的回归测试。

最重要的两条不变量：
1. 判定阈值不 round、展示线 round(2) —— 混用会让擦边信号翻转；
2. prev 锚点由调用方传入，共享函数不「顺手修正」T-2 偏移。
"""
import unittest
from unittest.mock import patch

import ma120_strategy
from ma120_strategy import Settings


class SettingsTests(unittest.TestCase):
    def test_defaults_are_immutable(self):
        self.assertEqual(ma120_strategy.DEFAULTS.ma_days, 120)
        with self.assertRaises(Exception):
            ma120_strategy.DEFAULTS.ma_days = 60

    def test_load_settings_reads_database_names(self):
        store = type("Store", (), {"summary": lambda self: {"ma120": {
            "ma_days": 60, "buy_factor": 0.9, "sell_factor": 1.1,
            "history_days": 300}}})()
        settings = ma120_strategy.load_settings(store)
        self.assertEqual(settings, Settings(60, 0.9, 1.1, 300))

    def test_load_settings_falls_back_on_error(self):
        broken = type("Store", (), {"summary": lambda self: (_ for _ in ()).throw(
            OSError("database missing"))})()
        self.assertEqual(ma120_strategy.load_settings(broken), ma120_strategy.DEFAULTS)


class CalculationTests(unittest.TestCase):
    def test_moving_average_requires_enough_bars(self):
        self.assertIsNone(ma120_strategy.moving_average([1, 2, 3], 5))
        self.assertEqual(ma120_strategy.moving_average([1, 2, 3], 3), 2.0)
        self.assertEqual(ma120_strategy.moving_average([1, 2, 3, 9], 3), 14 / 3)

    def test_levels_separate_decision_and_display(self):
        buy, sell, buy_display, sell_display = ma120_strategy.levels(100.0)
        self.assertEqual((buy_display, sell_display), (88.0, 112.0))
        self.assertAlmostEqual(buy, 88.0)
        self.assertAlmostEqual(sell, 112.0)  # 100*1.12 有浮点尾差，不做精确相等
        raw_buy, _, shown_buy, _ = ma120_strategy.levels(83.333)
        self.assertNotEqual(raw_buy, shown_buy)
        self.assertEqual(shown_buy, round(raw_buy, 2))

    def test_levels_reject_invalid_ma(self):
        self.assertEqual(ma120_strategy.levels(None), (None, None, None, None))
        self.assertEqual(ma120_strategy.levels(0), (None, None, None, None))

    def test_first_cross_buy_and_sell(self):
        # prev 锚点完全由调用方给：这里模拟 main monitor 的 iloc[-2]。
        self.assertEqual(ma120_strategy.first_cross(80, 100, 90, 100), "buy")
        self.assertEqual(ma120_strategy.first_cross(120, 100, 110, 100), "sell")
        self.assertEqual(ma120_strategy.first_cross(80, 100, 80, 100), "hold")
        self.assertEqual(ma120_strategy.first_cross(100, 100, 100, 100), "hold")

    def test_first_cross_honours_custom_settings(self):
        settings = Settings(ma_days=60, buy_factor=0.9, sell_factor=1.1)
        self.assertEqual(ma120_strategy.first_cross(89, 100, 95, 100, settings), "buy")
        self.assertEqual(ma120_strategy.first_cross(89, 100, 95, 100), "hold")

    def test_first_cross_uses_prev_ma_not_current_ma(self):
        # prev 均线不同时，prev 阈值应随之变化（backfill 的 T-2 语义）。
        self.assertEqual(
            ma120_strategy.first_cross(70, 100, 90, 100), "buy")
        self.assertEqual(
            ma120_strategy.first_cross(70, 100, 90, 200), "hold")

    def test_zone_uses_rounded_display_lines_by_default(self):
        self.assertEqual(ma120_strategy.zone_of(80, 100), "below_buy")
        self.assertEqual(ma120_strategy.zone_of(120, 100), "above_sell")
        self.assertEqual(ma120_strategy.zone_of(100, 100), "between")
        self.assertEqual(ma120_strategy.zone_of(80, 100, rounded=False), "below_buy")


if __name__ == "__main__":
    unittest.main()
