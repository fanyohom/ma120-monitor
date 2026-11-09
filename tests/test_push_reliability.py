"""推送失败可见化的回归测试（2026-11-09）。

锁定三条最容易退化的语义：
1. 5xx / 网络异常要重试，4xx 与飞书业务错误不重试；
2. 任何推送失败都要体现在 run() 结果与退出码上，且不能上报成功心跳；
3. 非交易日跳过是「正常跳过」（退出码 0、无心跳），不是「成功推送」。
"""
import threading
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

import stock_dynamic_monitor as monitor
from market_store import TIMEZONE
from web_app import StockDashboardService


def _response(status=200, payload=None, text=""):
    response = MagicMock()
    response.status_code = status
    response.text = text or str(payload or "")
    if payload is None:
        response.json.side_effect = ValueError("not json")
    else:
        response.json.return_value = payload
    return response


class SendFeishuTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(monitor, "_get_feishu_webhook_url",
                               return_value="https://example.invalid/hook")
        patcher.start()
        self.addCleanup(patcher.stop)
        sleep_patcher = patch.object(monitor.time, "sleep")
        self.sleep = sleep_patcher.start()
        self.addCleanup(sleep_patcher.stop)

    def test_5xx_retries_then_succeeds(self):
        with patch.object(monitor.requests, "post", side_effect=[
                _response(500, text="gateway error"),
                _response(200, {"code": 0})]) as post:
            self.assertTrue(monitor.send_feishu("hello"))
        self.assertEqual(post.call_count, 2)
        self.sleep.assert_called_once()

    def test_network_error_exhausts_attempts(self):
        with patch.object(monitor.requests, "post",
                          side_effect=OSError("connection reset")) as post:
            self.assertFalse(monitor.send_feishu("hello"))
        self.assertEqual(post.call_count, monitor.FEISHU_MAX_ATTEMPTS)

    def test_4xx_does_not_retry(self):
        with patch.object(monitor.requests, "post",
                          return_value=_response(401, text="unauthorized")) as post:
            self.assertFalse(monitor.send_feishu("hello"))
        self.assertEqual(post.call_count, 1)
        self.sleep.assert_not_called()

    def test_business_error_does_not_retry(self):
        with patch.object(monitor.requests, "post",
                          return_value=_response(200, {"code": 9499, "msg": "bad"})) as post:
            self.assertFalse(monitor.send_feishu("hello"))
        self.assertEqual(post.call_count, 1)

    def test_non_json_success_response_is_accepted(self):
        with patch.object(monitor.requests, "post", return_value=_response(200)):
            self.assertTrue(monitor.send_feishu("hello"))

    def test_missing_webhook_is_a_failure(self):
        with patch.object(monitor, "_get_feishu_webhook_url", return_value=""):
            with patch.object(monitor.requests, "post") as post:
                self.assertFalse(monitor.send_feishu("hello"))
        post.assert_not_called()


class RunResultTests(unittest.TestCase):
    def _run(self, *, send_ok=True, watchlist=True, skip=False, skip_is_trading=True):
        items = [{"code": "000333", "name": "美的集团", "signal": "hold"}] if watchlist else []
        with patch.object(monitor, "load_portfolio", return_value=[]), \
             patch.object(monitor, "load_watchlist", return_value=items), \
             patch.object(monitor, "enrich", side_effect=lambda item: item), \
             patch.object(monitor, "is_trading_day", return_value=skip_is_trading), \
             patch.object(monitor, "send_feishu", return_value=send_ok) as send, \
             patch.object(monitor, "render_watchlist_card", return_value={"msg_type": "text"}), \
             patch.object(monitor, "_ping_healthcheck") as ping, \
             patch.object(monitor, "_print_console_table"):
            result = monitor.run(mode="watchlist", feishu=True,
                                 skip_non_trading_day=skip)
        return result, send, ping

    def test_failed_push_is_reported_and_nonzero_exit(self):
        result, _, ping = self._run(send_ok=False)
        self.assertEqual(result["push_failures"], ["watchlist"])
        self.assertEqual(monitor._exit_code(result), 1)
        ping.assert_not_called()

    def test_successful_push_pings_heartbeat(self):
        result, _, ping = self._run(send_ok=True)
        self.assertEqual(result["push_failures"], [])
        self.assertEqual(monitor._exit_code(result), 0)
        ping.assert_called_once_with()

    def test_no_rows_is_treated_as_failure(self):
        result, send, ping = self._run(watchlist=False)
        self.assertEqual(result["push_failures"], ["no_data"])
        self.assertEqual(monitor._exit_code(result), 1)
        send.assert_not_called()
        ping.assert_not_called()

    def test_non_trading_day_skip_is_not_a_success(self):
        result, send, ping = self._run(skip=True, skip_is_trading=False)
        self.assertTrue(result["skipped"])
        self.assertEqual(result["push_failures"], [])
        self.assertEqual(monitor._exit_code(result), 0)
        send.assert_not_called()
        ping.assert_not_called()


class HealthEndpointTests(unittest.TestCase):
    def _service(self, snapshot=None, refresh_error=None):
        service = StockDashboardService.__new__(StockDashboardService)
        service.snapshot = snapshot
        service._snapshot_lock = threading.Lock()
        service._refresh_error = refresh_error
        return service

    def test_starting_when_no_snapshot(self):
        self.assertEqual(self._service().health()["status"], "starting")

    def test_ok_snapshot(self):
        snapshot = {
            "generated_at": datetime.now(TIMEZONE).isoformat(),
            "portfolio": [], "watchlist": [{"code": "000333"}], "summary": {},
        }
        result = self._service(snapshot).health()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["watchlist_count"], 1)

    def test_degraded_on_source_error(self):
        snapshot = {
            "generated_at": datetime.now(TIMEZONE).isoformat(),
            "portfolio": [], "watchlist": [{"code": "000333"}],
            "source_error": "database locked",
        }
        result = self._service(snapshot).health()
        self.assertEqual(result["status"], "degraded")
        self.assertTrue(result["problems"])

    def test_degraded_on_stale_refresh(self):
        snapshot = {
            "generated_at": datetime.now(TIMEZONE).isoformat(),
            "portfolio": [], "watchlist": [{"code": "000333", "data_stale": True}],
        }
        result = self._service(snapshot, refresh_error="写入失败").health()
        self.assertEqual(result["status"], "degraded")


if __name__ == "__main__":
    unittest.main()
