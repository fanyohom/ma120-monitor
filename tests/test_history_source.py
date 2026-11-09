"""A 股日 K 复权口径切换（2026-11-09）的回归测试。

锁定四件容易悄悄退化的事：
1. A 股必须走腾讯 fqkline 的 qfq，而不是新浪不复权接口；
2. 盘中返回的当日未完成 K 线必须丢掉，否则 11:30 的 MA120 口径会变；
3. 存量重刷（replace_bars）不能把旧的不复权价格 merge 进新序列；
4. 落库的 source 标注统一为 Tencent qfq。
"""
import unittest
from datetime import datetime
from unittest.mock import patch

import pandas as pd

import stock_dynamic_monitor as monitor


def _dates(count: int, end: str = "2026-10-21") -> list[str]:
    return [d.date().isoformat() for d in pd.bdate_range(end=end, periods=count)]


def _payload(symbol: str, dates: list[str], close: float = 10.5) -> dict:
    rows = [[day, "10", str(close), "11", "9", "1000"] for day in dates]
    return {"data": {symbol: {"qfqday": rows}}}


def _frame(dates: list[str], close: float) -> pd.DataFrame:
    frame = pd.DataFrame(
        [[10.0, close, 11.0, 9.0, 1000.0] for _ in dates],
        columns=["open", "close", "high", "low", "volume"],
        index=pd.to_datetime(dates),
    )
    frame.index.name = "date"
    return frame


class HistorySourceTests(unittest.TestCase):
    def test_a_share_uses_tencent_qfq(self):
        dates = _dates(130)
        with patch.object(monitor, "_read_cache", return_value=None), \
             patch.object(monitor, "_store_history") as store, \
             patch.object(monitor, "requests") as fake:
            fake.get.return_value.json.return_value = _payload("sh600000", dates)
            result = monitor.get_stock_hist_data("600000", days=130, force=True)
        self.assertIn("fqkline/get", fake.get.call_args.args[0])
        self.assertEqual(fake.get.call_args.kwargs["params"]["param"],
                         "sh600000,day,,,130,qfq")
        self.assertEqual(len(result), 130)
        store.assert_called_once()

    def test_sz_a_share_symbol(self):
        dates = _dates(130)
        with patch.object(monitor, "_read_cache", return_value=None), \
             patch.object(monitor, "_store_history"), \
             patch.object(monitor, "requests") as fake:
            fake.get.return_value.json.return_value = _payload("sz000333", dates)
            monitor.get_stock_hist_data("000333", days=130, force=True)
        self.assertIn("sz000333", fake.get.call_args.kwargs["params"]["param"])

    def test_drops_intraday_bar_before_close(self):
        frame = _frame(["2026-11-05", "2026-11-06", "2026-11-09"], 10.5)
        with patch.object(monitor, "datetime") as fake_datetime:
            fake_datetime.now.return_value = datetime(2026, 11, 9, 11, 30)
            intraday = monitor._drop_unfinished_session_bar(frame)
            self.assertEqual(len(intraday), 2)
            self.assertEqual(intraday.index.max().date().isoformat(), "2026-11-06")
            fake_datetime.now.return_value = datetime(2026, 11, 9, 15, 30)
            after_close = monitor._drop_unfinished_session_bar(frame)
            self.assertEqual(len(after_close), 3)

    def test_keeps_bar_when_last_date_is_not_today(self):
        frame = _frame(["2026-11-03", "2026-11-04", "2026-11-06"], 10.5)
        with patch.object(monitor, "datetime") as fake_datetime:
            fake_datetime.now.return_value = datetime(2026, 11, 9, 11, 30)
            self.assertEqual(len(monitor._drop_unfinished_session_bar(frame)), 3)

    def test_replace_bars_does_not_merge_legacy_prices(self):
        old_dates = _dates(300)
        legacy = _frame(old_dates, 99.0)
        new_dates = _dates(130)
        with patch.object(monitor, "_read_cache", return_value=legacy), \
             patch.object(monitor, "_store_history"), \
             patch.object(monitor, "requests") as fake:
            fake.get.return_value.json.return_value = _payload("sh600000", new_dates)
            result = monitor.get_stock_hist_data(
                "600000", days=130, force=True, replace_bars=True)
        self.assertEqual(len(result), 130, "旧的不复权 bar 不应残留")
        self.assertTrue((result["close"] == 10.5).all())

    def test_normal_refresh_still_keeps_history_depth(self):
        old_dates = _dates(300)
        legacy = _frame(old_dates, 99.0)
        new_dates = _dates(130)
        with patch.object(monitor, "_read_cache", return_value=legacy), \
             patch.object(monitor, "_store_history"), \
             patch.object(monitor, "requests") as fake:
            fake.get.return_value.json.return_value = _payload("sh600000", new_dates)
            result = monitor.get_stock_hist_data("600000", days=130, force=True)
        self.assertEqual(len(result), 300, "日常刷新仍应保留 800 根深度历史")

    def test_replace_bars_rejects_incomplete_response(self):
        legacy = _frame(_dates(300), 99.0)
        with patch.object(monitor, "_read_cache", return_value=legacy), \
             patch.object(monitor, "_store_history") as store, \
             patch.object(monitor, "requests") as fake:
            fake.get.return_value.json.return_value = _payload("sh600000", _dates(50))
            result = monitor.get_stock_hist_data(
                "600000", days=130, force=True, replace_bars=True)
        self.assertIs(result, legacy, "残缺响应必须回退，不能覆盖完整历史")
        store.assert_not_called()

    def test_write_cache_marks_qfq_source(self):
        frame = _frame(_dates(130), 10.5)
        with patch.object(monitor, "_market_store") as factory:
            monitor._write_cache("600000", frame)
        metadata = factory.return_value.put_history.call_args.args[3]
        self.assertEqual(metadata, {"source": "Tencent qfq"})


if __name__ == "__main__":
    unittest.main()
