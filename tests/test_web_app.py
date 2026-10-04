import http.client
import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd

from web_app import DashboardHandler, DashboardService, StockDashboardService, allowed_request_host, estimate_profit, replay_history


def market(code, *_):
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    dates = pd.bdate_range(end=now.date(), periods=540)
    rows = [[day.date().isoformat(), "10", "10", "10", "10", "1000"] for day in dates]
    return {"code": code, "qfq": rows, "raw": rows, "price": 8.0,
            "quote_at": now.isoformat(), "source": "test", "volume": 1000}


class DashboardServiceTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.config_path = Path(self.folder.name) / "etf_plans.toml"
        self.service = DashboardService(self.config_path, market_loader=market)

    def test_sample_mode_shows_calculation_without_persisting_trades(self):
        dashboard = self.service.dashboard(refresh=True)
        self.assertTrue(dashboard["sample"])
        self.assertEqual(len(dashboard["plans"]), 4)
        self.assertEqual(dashboard["plans"][0]["weekday"], 0)
        self.assertEqual({plan["code"] for plan in dashboard["plans"] if plan["definition"]},
                         {"510300", "512100", "513500", "513100"})
        self.assertEqual(dashboard["plans"][0]["definition"]["index_name"], "沪深300指数 · 000300")
        self.assertEqual(len(dashboard["report"]["rows"]), 12)
        self.assertEqual(dashboard["history"], [])
        with self.assertRaisesRegex(ValueError, "etf_plans.toml"):
            self.service.add_trade({"plan_id": "csi300"})
        replay = self.service.history("csi300")
        self.assertTrue(replay["replay_supported"])
        self.assertGreater(len(replay["reference_buys"]), 0)
        self.assertEqual(replay["trades"], [])
        self.assertTrue(all(point["ma"] is not None for point in replay["series"]))
        self.assertEqual(replay["window_start"], replay["simulation_start"])
        estimate = self.service.estimate("csi300", replay["simulation_start"])
        self.assertTrue(estimate["supported"])
        self.assertGreater(estimate["buy_count"], 0)

    def test_web_refresh_requests_fresh_market_even_with_cache(self):
        self.service.market_loader = None
        with patch("web_app.fetch_market", side_effect=lambda code, *_args, **_kwargs: market(code)) as fetch:
            self.service.dashboard(refresh=True)
        self.assertEqual(fetch.call_count, 4)
        self.assertTrue(all(call.kwargs["force"] for call in fetch.call_args_list))

    def test_full_history_draws_ma_across_entire_chart_window(self):
        def extended_market(code, days, now, _cache_dir):
            self.assertEqual(days, 800)
            dates = pd.bdate_range(end=now.date(), periods=800)
            rows = [[day.date().isoformat(), "10", "10"] for day in dates]
            return {"code": code, "qfq": rows, "raw": rows}

        service = DashboardService(self.config_path, market_loader=extended_market)
        replay = service.history("csi300")
        self.assertEqual(len(replay["series"]), 240)
        self.assertTrue(all(point["ma"] is not None for point in replay["series"]))
        self.assertEqual(replay["window_start"], replay["series"][0]["date"])

    def test_configured_mode_persists_reports_and_validates_trades(self):
        template = Path(__file__).resolve().parents[1] / "etf_plans.example.toml"
        self.config_path.write_bytes(template.read_bytes())
        refreshed = self.service.dashboard(refresh=True)
        self.assertFalse(refreshed["sample"])
        self.assertEqual(len(refreshed["history"]), 1)
        self.assertEqual(self.service.dashboard()["report"]["run_id"], refreshed["report"]["run_id"])
        trade_day = datetime.now(ZoneInfo("Asia/Shanghai")).date()
        while trade_day.weekday() >= 5:
            trade_day -= timedelta(days=1)
        fill = {"plan_id": "csi300", "side": "buy", "shares": 100, "price": 8.0,
                "fee": 5, "trade_date": trade_day.isoformat(), "fill_id": "test-fill-1"}
        self.assertEqual(self.service.add_trade(fill), {"ok": True})
        self.assertEqual(self.service.dashboard()["fills"][0]["fill_id"], "test-fill-1")
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.service.add_trade(fill)

    def test_replay_excludes_current_bar_and_keeps_actual_sells_distinct(self):
        template = Path(__file__).resolve().parents[1] / "etf_plans.example.toml"
        from etf_dca import load_config

        config = load_config(template)
        plan = {**config["plans"][0], "frequency": "daily"}
        settings = {**config["settings"], "ma_days": 3}
        dates = ["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04", "2026-09-07"]
        closes = [10, 10, 10, 5, 5]
        rows = [[day, str(price), str(price)] for day, price in zip(dates, closes)]
        fills = [{"fill_id": "broker-sell", "side": "sell", "shares": 100,
                  "price": 5.1, "trade_date": "2026-09-04"}]
        replay = replay_history(plan, settings, {"qfq": rows, "raw": rows}, fills,
                                datetime(2026, 9, 8).date())
        self.assertEqual(replay["reference_buys"][0]["date"], "2026-09-04")
        self.assertEqual(replay["reference_buys"][0]["amount"], 2000)
        self.assertEqual(replay["window_start"], "2026-09-04")
        self.assertTrue(all(point["ma"] is not None for point in replay["series"]))
        self.assertEqual(replay["trades"][0]["side"], "sell")
        self.assertTrue(replay["trades"][0]["on_chart"])
        self.assertEqual(replay["series"][-1]["date"], "2026-09-07")
        plan["strategy"] = "drawdown"
        cost_replay = replay_history(plan, settings, {"qfq": rows, "raw": rows}, fills,
                                     datetime(2026, 9, 8).date())
        self.assertFalse(cost_replay["replay_supported"])
        self.assertEqual(cost_replay["reference_buys"], [])

    def test_estimate_uses_only_buys_after_start_and_latest_adjusted_price(self):
        from etf_dca import load_config

        config = load_config(Path(__file__).resolve().parents[1] / "etf_plans.example.toml")
        plan = {**config["plans"][0], "frequency": "daily"}
        settings = {**config["settings"], "ma_days": 3}
        dates = ["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04", "2026-09-07"]
        rows = [[day, str(price), str(price)] for day, price in zip(dates, [10, 10, 10, 5, 6])]
        replay = replay_history(plan, settings, {"qfq": rows, "raw": rows}, [],
                                datetime(2026, 9, 8).date())
        estimate = estimate_profit(plan, settings, replay, "2026-09-04")
        self.assertEqual([buy["date"] for buy in estimate["buys"]], dates[-2:])
        self.assertEqual(estimate["invested"], 3800)
        self.assertEqual(estimate["estimated_value"], 4200)
        self.assertEqual(estimate["profit"], 400)
        later = estimate_profit(plan, settings, replay, "2026-09-05")
        self.assertEqual(later["buy_count"], 1)
        self.assertEqual(later["profit"], 0)
        with self.assertRaisesRegex(ValueError, "起算日期"):
            estimate_profit(plan, settings, replay, "2026-09-08")

    def test_estimate_restarts_weekly_period_from_selected_date(self):
        from etf_dca import load_config

        config = load_config(Path(__file__).resolve().parents[1] / "etf_plans.example.toml")
        plan = config["plans"][0]
        settings = {**config["settings"], "ma_days": 2}
        dates = ["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"]
        rows = [[day, "10", "10"] for day in dates]
        replay = replay_history(plan, settings, {"qfq": rows, "raw": rows}, [],
                                datetime(2026, 9, 7).date())
        self.assertEqual(replay["reference_buys"][0]["date"], "2026-09-03")
        estimate = estimate_profit(plan, settings, replay, "2026-09-04")
        self.assertEqual([buy["date"] for buy in estimate["buys"]], ["2026-09-04"])

    def test_estimate_marks_prior_purchase_with_adjusted_return(self):
        from etf_dca import load_config

        config = load_config(Path(__file__).resolve().parents[1] / "etf_plans.example.toml")
        plan = {**config["plans"][0], "frequency": "daily"}
        settings = {**config["settings"], "ma_days": 2}
        dates = ["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"]
        raw = [[day, "10", "10"] for day in dates]
        adjusted = [[day, str(value), str(value)] for day, value in zip(dates, [5, 5, 5, 10])]
        replay = replay_history(plan, settings, {"qfq": adjusted, "raw": raw}, [],
                                datetime(2026, 9, 7).date())
        estimate = estimate_profit(plan, settings, replay, "2026-09-03")
        self.assertEqual(estimate["buy_count"], 1)
        self.assertEqual(estimate["invested"], 1000)
        self.assertEqual(estimate["estimated_value"], 2000)
        self.assertEqual(estimate["profit"], 1000)

    def test_chart_price_and_ma_share_one_adjusted_scale(self):
        from etf_dca import load_config

        config = load_config(Path(__file__).resolve().parents[1] / "etf_plans.example.toml")
        plan = config["plans"][0]
        settings = {**config["settings"], "ma_days": 2}
        dates = ["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04", "2026-09-07"]
        raw = [[day, str(price), str(price)] for day, price in zip(dates, [10, 10, 10, 9, 9])]
        adjusted = [[day, "9", "9"] for day in dates]
        replay = replay_history(plan, settings, {"qfq": adjusted, "raw": raw}, [],
                                datetime(2026, 9, 8).date())
        before, after = replay["series"][:2]
        self.assertEqual((before["ma"], after["ma"]), (10, 9))
        self.assertEqual((before["chart_ma"], after["chart_ma"]), (9, 9))
        self.assertEqual((before["chart_price"], after["chart_price"]), (9, 9))

    def test_stock_dashboard_snapshot_is_read_only_until_refresh(self):
        expected = {"generated_at": "2026-10-04T12:00:00+08:00", "portfolio": [], "watchlist": []}
        service = StockDashboardService(loader=lambda: expected)
        self.assertEqual(service.current(), {"snapshot": None})
        self.assertEqual(service.refresh(), {"snapshot": expected})
        self.assertEqual(service.current(), {"snapshot": expected})

    def test_local_host_allowlist_rejects_rebinding_domains(self):
        self.assertTrue(allowed_request_host("127.0.0.1:8765", 8765))
        self.assertTrue(allowed_request_host("localhost:8765", 8765))
        self.assertFalse(allowed_request_host("evil.example:8765", 8765))
        self.assertFalse(allowed_request_host("127.0.0.1.evil.example:8765", 8765))
        self.assertFalse(allowed_request_host("localhost:8766", 8765))

    def test_ma120_http_page_refresh_and_foreign_host_rejection(self):
        expected = {"generated_at": "2026-10-04T12:00:00+08:00", "portfolio": [], "watchlist": []}

        class Handler(DashboardHandler):
            stock_service = StockDashboardService(loader=lambda: expected)

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 3)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        self.addCleanup(connection.close)

        connection.request("GET", "/ma120")
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertIn(b"MA120", response.read())

        connection.request("POST", "/api/ma120/refresh", body="{}",
                           headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(response.read())["snapshot"], expected)

        connection.request("GET", "/api/ma120", headers={"Host": f"evil.example:{server.server_port}"})
        response = connection.getresponse()
        self.assertEqual(response.status, 403)
        response.read()


if __name__ == "__main__":
    unittest.main()
