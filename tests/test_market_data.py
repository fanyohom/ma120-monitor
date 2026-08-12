import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

import pandas as pd

import stock_dynamic_monitor as monitor


class MarketDataTests(unittest.TestCase):
    def test_safe_code_normalizes_hk_code(self):
        self.assertEqual(monitor._safe_code("hk1801"), "HK01801")

    @patch("stock_dynamic_monitor.requests.get")
    def test_hk_history_uses_tencent_qfq_data(self, mock_get):
        mock_response = Mock()
        mock_response.json.return_value = {
            "data": {
                "hk01801": {
                    "qfqday": [
                        ["2026-08-10", "96.85", "97.30", "98.50", "95.15", "1000"],
                        ["2026-08-11", "97.30", "95.55", "98.85", "95.20", "1200"],
                    ]
                }
            }
        }
        mock_get.return_value = mock_response

        with tempfile.TemporaryDirectory() as tmpdir:
            original_cache_dir = monitor.CACHE_DIR
            monitor.CACHE_DIR = tmpdir
            try:
                df = monitor.get_stock_hist_data("HK01801", days=2)
            finally:
                monitor.CACHE_DIR = original_cache_dir

        self.assertEqual(list(df["close"]), [97.30, 95.55])
        _, kwargs = mock_get.call_args
        self.assertEqual(kwargs["params"]["param"], "hk01801,day,,,2,qfq")

    @patch("stock_dynamic_monitor.requests.get")
    def test_hk_realtime_quote_uses_tencent_fields(self, mock_get):
        fields = [""] * 31
        fields[1] = "信达生物"
        fields[3] = "95.10"
        fields[30] = "2026/08/12 13:47:52"
        mock_response = Mock()
        mock_response.text = 'v_hk01801="' + "~".join(fields) + '";'
        mock_get.return_value = mock_response

        quote = monitor.get_stock_realtime_quote("HK01801")

        self.assertEqual(quote["price"], 95.10)
        self.assertEqual(quote["quote_date"], "2026-08-12")
        self.assertEqual(quote["quote_time"], "13:47:52")

    def test_signal_uses_realtime_price_when_available(self):
        dates = pd.date_range("2026-01-01", periods=121, freq="D")
        df = pd.DataFrame(
            {
                "open": [100.0] * 121,
                "high": [100.0] * 121,
                "low": [100.0] * 121,
                "close": [100.0] * 121,
                "volume": [1_000] * 121,
            },
            index=dates,
        )

        signal = monitor.check_ma120_signal(df, current_price=87.0)

        self.assertEqual(signal["price"], 87.0)
        self.assertEqual(signal["ma120"], 100.0)
        self.assertEqual(signal["buy_line"], 88.0)
        self.assertEqual(signal["signal"], "buy")

    def test_previous_calendar_day_cache_is_stale_even_within_24_hours(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            original_cache_dir = monitor.CACHE_DIR
            monitor.CACHE_DIR = tmpdir
            try:
                cache_path = monitor._cache_path("000960")
                payload = {
                    "data": [
                        {
                            "date": "2026-08-04T00:00:00",
                            "open": 36.0,
                            "high": 37.0,
                            "low": 35.5,
                            "close": 36.34,
                            "volume": 1000,
                        }
                    ]
                }
                with open(cache_path, "w", encoding="utf-8") as f:
                    json.dump(payload, f)

                stale_mtime = (datetime.now() - timedelta(days=1)).replace(
                    hour=23,
                    minute=59,
                    second=0,
                    microsecond=0,
                )
                os.utime(cache_path, (time.time(), stale_mtime.timestamp()))

                self.assertIsNone(monitor._read_cache("000960"))
            finally:
                monitor.CACHE_DIR = original_cache_dir


if __name__ == "__main__":
    unittest.main()
