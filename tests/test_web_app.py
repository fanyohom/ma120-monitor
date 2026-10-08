import csv
import http.client
import json
import tempfile
import threading
import unittest
from datetime import date, datetime, timedelta
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
        self.service = DashboardService(self.config_path, market_loader=market,
                                        valuation_updater=lambda **_kwargs: None)

    def test_sample_mode_shows_calculation_without_persisting_trades(self):
        dashboard = self.service.dashboard(refresh=True)
        self.assertTrue(dashboard["sample"])
        self.assertEqual(len(dashboard["plans"]), 4)
        self.assertEqual(dashboard["plans"][0]["weekday"], 0)
        self.assertEqual(dashboard["strategy_settings"]["ma_days"], 500)
        self.assertEqual((dashboard["strategy_settings"]["buy_below"],
                          dashboard["strategy_settings"]["high_at"]), (30, 70))
        self.assertEqual({plan["code"] for plan in dashboard["plans"] if plan["definition"]},
                         {"510300", "512100", "513500", "513100"})
        self.assertEqual(dashboard["plans"][0]["definition"]["index_name"], "沪深300指数 · 000300")
        self.assertEqual({plan["id"]: plan["strategy"] for plan in dashboard["plans"]}, {
            "csi300": "valuation", "csi1000": "ma", "sp500": "ma", "nasdaq100": "drawdown",
        })
        self.assertTrue(all(plan["strategy_reason"] for plan in dashboard["plans"]))
        nasdaq = next(row for row in dashboard["report"]["rows"]
                      if row["plan_id"] == "nasdaq100" and row["selected"])
        self.assertIn("尚无持仓", nasdaq["reason"])
        self.assertEqual(len(dashboard["report"]["rows"]), 12)
        self.assertEqual(dashboard["history"], [])
        with self.assertRaisesRegex(ValueError, "etf_plans.toml"):
            self.service.add_trade({"plan_id": "csi300"})
        replay = self.service.history("csi1000")
        self.assertTrue(replay["replay_supported"])
        self.assertGreater(len(replay["reference_buys"]), 0)
        self.assertEqual(replay["trades"], [])
        self.assertTrue(any(point["ma"] is not None for point in replay["series"]))
        self.assertLess(replay["window_start"], replay["simulation_start"])
        estimate = self.service.estimate("csi1000", replay["simulation_start"])
        self.assertTrue(estimate["supported"])
        self.assertGreater(estimate["buy_count"], 0)

    def test_web_refresh_requests_fresh_market_even_with_cache(self):
        self.service.market_loader = None
        with patch("web_app.fetch_market", side_effect=lambda code, *_args, **_kwargs: market(code)) as fetch:
            self.service.dashboard(refresh=True)
        self.assertEqual(fetch.call_count, 4)
        self.assertTrue(all(call.kwargs["force"] for call in fetch.call_args_list))

    def test_refresh_updates_selected_csi300_valuation_before_report(self):
        template = Path(__file__).resolve().parents[1] / "etf_plans.example.toml"
        self.config_path.write_bytes(template.read_bytes())
        calls = []

        def update(**kwargs):
            calls.append(kwargs)

        self.service.valuation_updater = update
        dashboard = self.service.dashboard(refresh=True)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["output"].resolve(),
                         (Path(self.folder.name) / "etf_valuations/000300.csv").resolve())
        self.assertEqual(calls[0]["today"], datetime.now(ZoneInfo("Asia/Shanghai")).date())
        self.assertEqual(calls[0]["settings"]["lookback_years"], 10)
        self.assertEqual(dashboard["valuation_update_warnings"], [])

    def test_failed_valuation_update_keeps_existing_csv_and_report(self):
        template = Path(__file__).resolve().parents[1] / "etf_plans.example.toml"
        self.config_path.write_bytes(template.read_bytes())
        output = Path(self.folder.name) / "etf_valuations/000300.csv"
        output.parent.mkdir(parents=True)
        today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
        dates = pd.bdate_range(end=today - timedelta(days=1), periods=2600)
        with output.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(("date", "available_date", "index_code", "pe_ttm"))
            for timestamp in dates:
                day: date = timestamp.date()
                writer.writerow((day.isoformat(), (day + timedelta(days=1)).isoformat(), "000300", "12"))
        before = output.read_bytes()

        def fail(**_kwargs):
            raise OSError("估值接口暂不可用")

        self.service.valuation_updater = fail
        dashboard = self.service.dashboard(refresh=True)
        selected = next(row for row in dashboard["report"]["rows"]
                        if row["plan_id"] == "csi300" and row["selected"])
        self.assertEqual(output.read_bytes(), before)
        self.assertEqual(selected["valuation"]["pe_ttm"], 12)
        self.assertNotEqual(selected["status"], "blocked")
        self.assertIn("估值接口暂不可用", dashboard["valuation_update_warnings"][0])

    def test_full_history_draws_ma_across_entire_chart_window(self):
        def extended_market(code, days, now, _cache_dir):
            self.assertEqual(days, 5000)
            dates = pd.bdate_range(end=now.date(), periods=800)
            rows = [[day.date().isoformat(), "10", "10"] for day in dates]
            return {"code": code, "qfq": rows, "raw": rows}

        service = DashboardService(self.config_path, market_loader=extended_market)
        replay = service.history("csi1000")
        self.assertEqual(len(replay["series"]), 799)
        self.assertEqual(sum(point["ma"] is not None for point in replay["series"]), 299)
        self.assertEqual(replay["window_start"], replay["series"][0]["date"])

    def test_all_sample_plans_replay_with_available_valuation_history(self):
        template = Path(__file__).resolve().parents[1] / "etf_plans.example.toml"
        self.config_path.write_bytes(template.read_bytes())
        path = Path(self.folder.name) / "etf_valuations/000300.csv"
        path.parent.mkdir()
        days = pd.bdate_range(end=date.today() - timedelta(days=1), periods=2600)
        with path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(("date", "available_date", "index_code", "pe_ttm"))
            for stamp in days:
                day = stamp.date()
                writer.writerow((day.isoformat(), (day + timedelta(days=1)).isoformat(),
                                 "000300", "10"))
        for plan_id in ("csi300", "csi1000", "sp500", "nasdaq100"):
            with self.subTest(plan_id=plan_id):
                replay = self.service.history(plan_id)
                self.assertTrue(replay["replay_supported"], replay["replay_reason"])
                estimate = self.service.estimate(plan_id, replay["simulation_start"])
                self.assertTrue(estimate["supported"])
                self.assertEqual(estimate["buy_count"], len(replay["reference_buys"]))

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
        plan = {**config["plans"][1], "frequency": "daily"}
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
        self.assertEqual(replay["window_start"], "2026-09-01")
        self.assertTrue(any(point["ma"] is not None for point in replay["series"]))
        self.assertEqual(replay["trades"][0]["side"], "sell")
        self.assertTrue(replay["trades"][0]["on_chart"])
        self.assertEqual(replay["series"][-1]["date"], "2026-09-07")
        plan["strategy"] = "drawdown"
        cost_replay = replay_history(plan, settings, {"qfq": rows, "raw": rows}, fills,
                                     datetime(2026, 9, 8).date())
        self.assertTrue(cost_replay["replay_supported"])
        self.assertEqual(cost_replay["reference_buys"][0]["date"], "2026-09-01")
        self.assertEqual(cost_replay["trades"][0]["side"], "sell")

    def test_valuation_replay_uses_only_valuations_available_before_bar(self):
        from etf_dca import load_config

        config = load_config(Path(__file__).resolve().parents[1] / "etf_plans.example.toml")
        plan = {**config["plans"][0], "frequency": "daily"}
        settings = {**config["settings"], **config["valuation"], "ma_days": 2,
                    "lookback_years": 1, "min_samples": 3, "min_span_days": 2}
        rows = [[day, "10", "10"] for day in
                ("2026-09-03", "2026-09-04", "2026-09-07", "2026-09-08")]
        path = Path(self.folder.name) / "valuation.csv"
        with path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(("date", "available_date", "index_code", "pe_ttm"))
            writer.writerows([
                ("2026-09-01", "2026-09-02", "000300", 30),
                ("2026-09-02", "2026-09-03", "000300", 20),
                ("2026-09-03", "2026-09-03", "000300", 10),
                ("2026-09-04", "2026-09-05", "000300", 100),
                ("2026-09-07", "2026-09-08", "000300", 1),
            ])
        replay = replay_history(plan, settings, {"qfq": rows, "raw": rows}, [],
                                date(2026, 9, 8), valuation_path=path)
        self.assertEqual([point["date"] for point in replay["series"]],
                         ["2026-09-03", "2026-09-04", "2026-09-07"])
        self.assertEqual(replay["simulation_start"], "2026-09-04")
        self.assertEqual(replay["series"][1]["valuation"]["date"], "2026-09-03")
        self.assertEqual(replay["series"][-1]["valuation"]["date"], "2026-09-04")
        self.assertEqual(replay["series"][-1]["valuation"]["pe_ttm"], 100)
        self.assertEqual([buy["date"] for buy in replay["reference_buys"]], ["2026-09-04"])
        self.assertEqual(estimate_profit(plan, settings, replay, "2026-09-07")["buy_count"], 0)

    def test_valuation_replay_waits_for_publication_and_full_window(self):
        from etf_dca import load_config

        config = load_config(Path(__file__).resolve().parents[1] / "etf_plans.example.toml")
        plan = {**config["plans"][0], "frequency": "daily"}
        settings = {**config["settings"], **config["valuation"], "ma_days": 2,
                    "lookback_years": 1, "min_samples": 3, "min_span_days": 2}
        rows = [[day, "10", "10"] for day in
                ("2026-09-04", "2026-09-07", "2026-09-08")]
        path = Path(self.folder.name) / "valuation.csv"
        with path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(("date", "available_date", "index_code", "pe_ttm"))
            writer.writerows([
                ("2026-09-01", "2026-09-02", "000300", 30),
                ("2026-09-02", "2026-09-03", "000300", 20),
                ("2026-09-03", "2026-09-04", "000300", 10),
                ("2026-09-04", "2026-09-05", "000300", 100),
                ("2026-09-07", "2026-09-08", "000300", 1),
            ])
        replay = replay_history(plan, settings, {"qfq": rows, "raw": rows}, [],
                                date(2026, 9, 8), valuation_path=path)
        self.assertEqual(replay["simulation_start"], "2026-09-07")
        self.assertEqual(replay["reference_buys"], [])
        self.assertEqual(replay["series"][0]["valuation_error"], "Valuation needs 3 samples; got 2")

    def test_drawdown_replay_updates_virtual_cost_and_resets_at_selected_start(self):
        from etf_dca import load_config

        config = load_config(Path(__file__).resolve().parents[1] / "etf_plans.example.toml")
        plan = {**config["plans"][3], "frequency": "daily", "base_amount": 2000,
                "max_amount": 5000, "shares": 1000, "cost": 50}
        settings = {**config["settings"], "ma_days": 2}
        rows = [[day, str(price), str(price)] for day, price in zip(
            ("2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"), (10, 8, 7, 1))]
        fills = [{"fill_id": "actual-sell", "side": "sell", "shares": 100,
                  "price": 8, "trade_date": "2026-09-02"}]
        replay = replay_history(plan, settings, {"qfq": rows, "raw": rows}, fills,
                                date(2026, 9, 4))
        self.assertEqual([buy["amount"] for buy in replay["reference_buys"]], [2000, 3000, 4000])
        self.assertIn("成本 8.800", replay["reference_buys"][-1]["reason"])
        self.assertEqual(replay["trades"][0]["side"], "sell")
        self.assertEqual(replay["window_end"], "2026-09-03")
        estimate = estimate_profit(plan, settings, replay, "2026-09-02")
        self.assertEqual([buy["amount"] for buy in estimate["buys"]], [2000, 3000])
        self.assertEqual(estimate["buy_count"], 2)

    def test_estimate_uses_only_buys_after_start_and_latest_adjusted_price(self):
        from etf_dca import load_config

        config = load_config(Path(__file__).resolve().parents[1] / "etf_plans.example.toml")
        plan = {**config["plans"][1], "frequency": "daily"}
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
        plan = config["plans"][1]
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
        plan = {**config["plans"][1], "frequency": "daily"}
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
        plan = config["plans"][1]
        settings = {**config["settings"], "ma_days": 2}
        dates = ["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04", "2026-09-07"]
        raw = [[day, str(price), str(price)] for day, price in zip(dates, [10, 10, 10, 9, 9])]
        adjusted = [[day, "9", "9"] for day in dates]
        replay = replay_history(plan, settings, {"qfq": adjusted, "raw": raw}, [],
                                datetime(2026, 9, 8).date())
        before, after = [point for point in replay["series"] if point["ma"] is not None][:2]
        self.assertEqual((before["ma"], after["ma"]), (10, 9))
        self.assertEqual((before["chart_ma"], after["chart_ma"]), (9, 9))
        self.assertEqual((before["chart_price"], after["chart_price"]), (9, 9))

    def test_stock_dashboard_snapshot_is_read_only_until_refresh(self):
        expected = {"generated_at": "2026-10-04T12:00:00+08:00", "portfolio": [], "watchlist": []}
        service = StockDashboardService(loader=lambda: expected)
        self.assertEqual(service.current(), {"snapshot": None})
        self.assertEqual(service.refresh(), {"snapshot": expected})
        self.assertEqual(service.current(), {"snapshot": expected})

    def test_ma120_history_requires_code_in_current_snapshot(self):
        snapshot = {"portfolio": [{"code": "600001", "name": "持仓"}],
                    "watchlist": [{"code": "HK01801", "name": "关注"}]}
        calls = []
        service = StockDashboardService(loader=lambda: snapshot,
                                        history_loader=lambda code, name: calls.append((code, name)) or
                                        {"code": code, "name": name})
        with self.assertRaisesRegex(ValueError, "刷新"):
            service.history("600001")
        service.refresh()
        with self.assertRaisesRegex(ValueError, "请选择"):
            service.history(None)
        with self.assertRaisesRegex(ValueError, "不在"):
            service.history("999999")
        self.assertEqual(service.history("HK01801"), {"code": "HK01801", "name": "关注"})
        self.assertEqual(calls, [("HK01801", "关注")])

    def test_ma120_history_http_route(self):
        snapshot = {"portfolio": [{"code": "600001", "name": "持仓"}], "watchlist": []}

        class Handler(DashboardHandler):
            stock_service = StockDashboardService(
                loader=lambda: snapshot,
                history_loader=lambda code, name: {"code": code, "name": name, "series": []},
            )

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
        connection.request("GET", "/api/ma120/history?code=600001")
        response = connection.getresponse()
        self.assertEqual(response.status, 400)
        response.read()
        connection.request("POST", "/api/ma120/refresh", body="{}",
                           headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        response.read()
        connection.request("GET", "/api/ma120/history?code=999999")
        response = connection.getresponse()
        self.assertEqual(response.status, 400)
        response.read()
        connection.request("GET", "/api/ma120/history?code=600001")
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(response.read()),
                         {"code": "600001", "name": "持仓", "series": []})
        connection.request("GET", "/ma120/600001")
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertIn(b"MA120", response.read())

    def test_ma120_estimate_http_route(self):
        snapshot = {"portfolio": [{"code": "600001", "name": "持仓"}], "watchlist": []}
        history = {"currency": "CNY", "series": [
            {"date": "2026-10-01", "close": 100, "signal": "buy"},
            {"date": "2026-10-02", "close": 80, "signal": None},
            {"date": "2026-10-05", "close": 90, "signal": None},
        ]}

        class Handler(DashboardHandler):
            stock_service = StockDashboardService(
                loader=lambda: snapshot, history_loader=lambda code, name: history)

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
        connection.request("GET", "/api/ma120/estimate?code=600001&start_date=2026-10-01")
        response = connection.getresponse()
        self.assertEqual(response.status, 400)
        response.read()
        Handler.stock_service.refresh()
        connection.request("GET", "/api/ma120/estimate?code=600001&start_date=2026-10-01")
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        result = json.loads(response.read())
        self.assertEqual(result["trades"][0]["date"], "2026-10-02")
        self.assertEqual(result["estimated_value"], 112500)
        connection.request("GET", "/api/ma120/estimate?code=600001&start_date=2026-09-30")
        response = connection.getresponse()
        self.assertEqual(response.status, 400)
        response.read()
        connection.request("GET", "/api/ma120/estimate?code=999999&start_date=2026-10-01")
        response = connection.getresponse()
        self.assertEqual(response.status, 400)
        response.read()

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

    def test_ma120_watchlist_http_crud_and_validation(self):
        path = Path(self.folder.name) / "watchlist.json"

        def stock_dashboard():
            from watchlist_store import read_watchlist

            return {"portfolio": [], "watchlist": read_watchlist(path)}

        class Handler(DashboardHandler):
            stock_service = StockDashboardService(loader=stock_dashboard, watchlist_path=path)

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

        def request(method, target, payload=None, headers=None):
            body = json.dumps(payload, ensure_ascii=False) if isinstance(payload, dict) else payload
            if isinstance(body, str):
                body = body.encode("utf-8")
            connection.request(method, target, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, json.loads(response.read())

        json_headers = {"Content-Type": "application/json"}
        self.assertEqual(request("GET", "/api/ma120/watchlist"), (200, {"items": []}))
        status, result = request("POST", "/api/ma120/watchlist",
                                 {"code": "600001", "name": "测试股票"}, json_headers)
        self.assertEqual((status, result),
                         (200, {"items": [{"code": "600001", "name": "测试股票"}]}))
        self.assertEqual(request("GET", "/api/ma120")[1]["snapshot"]["watchlist"], result["items"])
        self.assertEqual(request("GET", "/api/ma120/watchlist"), (200, result))

        before = path.read_bytes()
        for payload in ({"code": "600001", "name": "重复"},
                        {"code": "bad", "name": "无效代码"},
                        {"code": "000001", "name": ""},
                        {"code": "000001", "name": 10}):
            status, _ = request("POST", "/api/ma120/watchlist", payload, json_headers)
            self.assertEqual(status, 400)
            self.assertEqual(path.read_bytes(), before)
        self.assertEqual(request("POST", "/api/ma120/watchlist", "{", json_headers)[0], 400)
        self.assertEqual(request("POST", "/api/ma120/watchlist", "{}", {})[0], 415)
        self.assertEqual(request("POST", "/api/ma120/watchlist", " " * 16385,
                                 json_headers)[0], 400)
        self.assertEqual(path.read_bytes(), before)

        foreign = {**json_headers, "Origin": "http://evil.example"}
        self.assertEqual(request("PATCH", "/api/ma120/watchlist?code=600001",
                                 {"name": "恶意改名"}, foreign)[0], 403)
        self.assertEqual(request("DELETE", "/api/ma120/watchlist?code=600001",
                                 headers={"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(path.read_bytes(), before)

        self.assertEqual(request("PATCH", "/api/ma120/watchlist?code=999999",
                                 {"name": "不存在"}, json_headers)[0], 404)
        self.assertEqual(request("DELETE", "/api/ma120/watchlist?code=999999")[0], 404)
        self.assertEqual(request("PATCH", "/api/ma120/watchlist?code=600001",
                                 {"name": "新名称"}, json_headers),
                         (200, {"items": [{"code": "600001", "name": "新名称"}]}))
        self.assertEqual(request("GET", "/api/ma120")[1]["snapshot"]["watchlist"][0]["name"],
                         "新名称")
        self.assertEqual(request("DELETE", "/api/ma120/watchlist?code=600001"),
                         (200, {"items": []}))
        self.assertEqual(request("GET", "/api/ma120")[1]["snapshot"]["watchlist"], [])

    def test_ma120_watchlist_save_survives_quote_refresh_failure(self):
        path = Path(self.folder.name) / "watchlist.json"

        def fail():
            raise OSError("行情暂不可用")

        service = StockDashboardService(loader=fail, watchlist_path=path)
        result = service.add_watchlist({"code": "1", "name": "测试"})
        self.assertEqual(result["items"], [{"code": "000001", "name": "测试"}])
        self.assertIn("行情暂不可用", result["refresh_warning"])
        self.assertEqual(service.current(), {"snapshot": None})
        self.assertEqual(service.watchlist(),
                         {"items": [{"code": "000001", "name": "测试"}]})


if __name__ == "__main__":
    unittest.main()
