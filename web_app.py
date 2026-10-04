"""Local ETF dashboard backed by the existing recommendation engine."""

import argparse
import json
import sqlite3
import threading
from contextlib import closing
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from etf_data import fetch_market, parse_history
from etf_catalog import ETF_DEFINITIONS
from etf_dca import (BASE_DIR, STRATEGIES, build_report, evaluate, load_config, open_journal,
                     period, record_trade)
from ma120_web import build_ma120_dashboard


WEB_DIR = BASE_DIR / "web"
TIMEZONE = ZoneInfo("Asia/Shanghai")


def allowed_request_host(host, port):
    return host.lower() in {f"127.0.0.1:{port}", f"localhost:{port}"}


def replay_history(plan, settings, payload, fills, today, display_days=240):
    """Replay MA buy dates on completed bars; recorded fills remain separate events."""
    adjusted = parse_history(payload["qfq"])
    raw = parse_history(payload["raw"])
    if not adjusted.index.equals(raw.index):
        raise ValueError("历史前复权和未复权日期不一致")
    if adjusted.index.max().date() > today:
        raise ValueError("历史行情包含未来日期")
    completed = adjusted.index[adjusted.index.date < today]
    if completed.empty:
        raise ValueError("没有已完成的日 K")
    if (today - completed[-1].date()).days > 15:
        raise ValueError("历史行情已过期")
    ma_days = settings["ma_days"]
    start = max(0, len(completed) - display_days)
    if len(completed) > ma_days:
        start = max(start, ma_days)
    chart_scale = float(raw.loc[completed[-1]]) / float(adjusted.loc[completed[-1]])
    series = []
    reference_buys = []
    marked_periods = set()
    for index, timestamp in enumerate(completed):
        if index < start:
            continue
        day = timestamp.date()
        price = float(raw.loc[timestamp])
        adjusted_price = float(adjusted.loc[timestamp])
        ma = None
        chart_ma = None
        if index >= ma_days:
            adjusted_ma = float(adjusted.iloc[index - ma_days:index].mean())
            ma = adjusted_ma * price / adjusted_price
            chart_ma = adjusted_ma * chart_scale
        series.append({"date": day.isoformat(), "price": price,
                       "adjusted_price": adjusted_price, "ma": ma,
                       "chart_price": adjusted_price * chart_scale, "chart_ma": chart_ma})
        if plan["strategy"] != "ma" or ma is None:
            continue
        period_id, due = period(plan, day)
        if not due or period_id in marked_periods:
            continue
        market = {"price": price, "ma": ma, "ma_days": ma_days,
                  "ma_deviation_pct": (price / ma - 1) * 100}
        result = evaluate(plan, "ma", market, None, "", 0, 0, settings)
        if result["reference_shares"]:
            marked_periods.add(period_id)
            reference_buys.append({"date": day.isoformat(), "price": price,
                                   "amount": result["reference_amount"],
                                   "shares": result["reference_shares"],
                                   "reason": result["reason"]})
    dates = {point["date"] for point in series}
    trades = [{"date": row["trade_date"], "side": row["side"], "price": row["price"],
               "shares": row["shares"], "fill_id": row["fill_id"],
               "on_chart": row["trade_date"] in dates}
              for row in fills]
    simulation_start = next((point["date"] for point in series if point["ma"] is not None), None)
    return {"series": series, "reference_buys": reference_buys,
            "trades": trades, "replay_supported": plan["strategy"] == "ma" and simulation_start is not None,
            "ma_days": ma_days, "window_start": series[0]["date"],
            "window_end": series[-1]["date"], "simulation_start": simulation_start}


def estimate_profit(plan, settings, history, start_date):
    """Mark hypothetical MA purchases to the latest adjusted close; never treat them as fills."""
    if plan["strategy"] != "ma" or not history["replay_supported"]:
        return {"supported": False, "reason": "当前策略没有可验证的历史买入回放"}
    start = date.fromisoformat(start_date)
    first = date.fromisoformat(history["simulation_start"])
    last = date.fromisoformat(history["window_end"])
    if not first <= start <= last:
        raise ValueError(f"起算日期须在 {first} 至 {last} 之间")

    final_adjusted = history["series"][-1]["adjusted_price"]
    marked_periods = set()
    buys = []
    invested = 0.0
    estimated_value = 0.0
    for point in history["series"]:
        day = date.fromisoformat(point["date"])
        if day < start or point["ma"] is None:
            continue
        period_id, due = period(plan, day)
        if not due or period_id in marked_periods:
            continue
        market = {"price": point["price"], "ma": point["ma"], "ma_days": settings["ma_days"],
                  "ma_deviation_pct": (point["price"] / point["ma"] - 1) * 100}
        result = evaluate(plan, "ma", market, None, "", 0, 0, settings)
        if result["reference_shares"] == 0:
            continue
        marked_periods.add(period_id)
        cost = result["estimated_cost"]
        invested += cost
        estimated_value += cost * final_adjusted / point["adjusted_price"]
        buys.append({"date": point["date"], "price": point["price"],
                     "amount": result["reference_amount"], "shares": result["reference_shares"],
                     "cost": cost, "reason": result["reason"]})
    invested = round(invested, 2)
    estimated_value = round(estimated_value, 2)
    profit = round(estimated_value - invested, 2)
    return {"supported": True, "start_date": start.isoformat(), "end_date": last.isoformat(),
            "buy_count": len(buys), "invested": invested, "estimated_value": estimated_value,
            "profit": profit, "return_pct": round(profit / invested * 100, 2) if invested else None,
            "buys": buys}


class DashboardService:
    def __init__(self, config_path=BASE_DIR / "etf_plans.toml", market_loader=None):
        self.config_path = Path(config_path)
        self.market_loader = market_loader

    def _config(self):
        sample = not self.config_path.exists()
        path = BASE_DIR / "etf_plans.example.toml" if sample else self.config_path
        return load_config(path), sample

    @staticmethod
    def _journal(config, sample):
        if not sample:
            return open_journal(config["directory"] / "dca_results" / "journal.sqlite3")
        db = sqlite3.connect(":memory:")
        db.executescript("CREATE TABLE fills (fill_id TEXT, plan_id TEXT, code TEXT, side TEXT, "
                         "shares INTEGER, price REAL, fee REAL, trade_date TEXT);")
        return db

    def dashboard(self, refresh=False):
        config, sample = self._config()
        with closing(self._journal(config, sample)) as db:
            if refresh:
                now = datetime.now(TIMEZONE)
                loader = self.market_loader or (
                    lambda code, days, instant, cache: fetch_market(code, days, instant, cache, force=True)
                )
                report = build_report(config, db, now, market_loader=loader)
                if not sample:
                    db.execute("INSERT INTO reports VALUES (?,?,?)", (report["run_id"], report["generated_at"],
                                                                    json.dumps(report, ensure_ascii=False, allow_nan=False)))
                    db.commit()
            elif sample:
                report = None
            else:
                stored = db.execute("SELECT payload FROM reports ORDER BY generated_at DESC LIMIT 1").fetchone()
                report = json.loads(stored[0]) if stored else None

            history = [] if sample else [
                {"run_id": row[0], "generated_at": row[1], "total_action_amount": row[2]}
                for row in db.execute("SELECT run_id, generated_at, json_extract(payload, '$.total_action_amount') "
                                      "FROM reports ORDER BY generated_at DESC LIMIT 12")
            ]
            fills = [] if sample else [
                {"fill_id": row[0], "plan_id": row[1], "side": row[2], "shares": row[3],
                 "price": row[4], "fee": row[5], "trade_date": row[6]}
                for row in db.execute("SELECT fill_id, plan_id, side, shares, price, fee, trade_date "
                                      "FROM fills ORDER BY trade_date DESC, rowid DESC LIMIT 20")
            ]

        return {
            "sample": sample,
            "plans": [{**{key: plan[key] for key in ("id", "name", "code", "strategy", "frequency",
                                                       "base_amount", "max_amount")},
                       **{key: plan[key] for key in ("weekday", "monthday") if key in plan},
                       "definition": ETF_DEFINITIONS.get(plan["code"])}
                      for plan in config["plans"]],
            "strategies": STRATEGIES,
            "report": report,
            "history": history,
            "fills": fills,
        }

    def add_trade(self, payload):
        config, sample = self._config()
        if sample:
            raise ValueError("请先建立 etf_plans.toml，再登记真实成交")
        plans = {plan["id"]: plan for plan in config["plans"]}
        plan_id = payload.get("plan_id")
        if plan_id not in plans:
            raise ValueError("无效的计划")
        try:
            shares = int(payload["shares"])
            price = float(payload["price"])
            fee = float(payload.get("fee", 0))
            trade_date = date.fromisoformat(payload["trade_date"])
            fill_id = str(payload["fill_id"]).strip()
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("成交编号、日期、份额和价格均为必填项") from exc
        if not fill_id or not isinstance(payload["shares"], int) or isinstance(payload["shares"], bool):
            raise ValueError("成交编号不能为空，份额必须为整数")
        with closing(self._journal(config, False)) as db:
            record_trade(db, plans[plan_id], fill_id, payload.get("side"), shares, price, fee,
                         trade_date, datetime.now(TIMEZONE).date())
        return {"ok": True}

    def history(self, plan_id):
        config, sample = self._config()
        plans = {plan["id"]: plan for plan in config["plans"]}
        if plan_id not in plans:
            raise ValueError("无效的计划")
        plan = plans[plan_id]
        now = datetime.now(TIMEZONE)
        loader = self.market_loader or fetch_market
        payload = loader(plan["code"], 800, now, config["directory"] / ".stock_cache" / "etf")
        fills = []
        if not sample:
            with closing(self._journal(config, False)) as db:
                fills = [dict(row) for row in db.execute(
                    "SELECT fill_id, side, shares, price, trade_date FROM fills WHERE plan_id=? "
                    "ORDER BY trade_date, rowid", (plan_id,))]
        settings = {**config["settings"], **config["valuation"]}
        return replay_history(plan, settings, payload, fills, now.date())

    def estimate(self, plan_id, start_date):
        config, _ = self._config()
        plans = {plan["id"]: plan for plan in config["plans"]}
        if plan_id not in plans:
            raise ValueError("无效的计划")
        if not start_date:
            raise ValueError("请选择起算日期")
        settings = {**config["settings"], **config["valuation"]}
        return estimate_profit(plans[plan_id], settings, self.history(plan_id), start_date)


class StockDashboardService:
    def __init__(self, loader=build_ma120_dashboard):
        self.loader = loader
        self.snapshot = None
        self.lock = threading.Lock()

    def current(self):
        return {"snapshot": self.snapshot}

    def refresh(self):
        with self.lock:
            self.snapshot = self.loader()
            return {"snapshot": self.snapshot}


class DashboardHandler(BaseHTTPRequestHandler):
    service = DashboardService()
    stock_service = StockDashboardService()

    def _json(self, status, data):
        body = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _static(self, path):
        assets = {"/": ("index.html", "text/html"), "/app.css": ("app.css", "text/css"),
                  "/app.js": ("app.js", "text/javascript"),
                  "/ma120": ("ma120.html", "text/html"),
                  "/ma120/": ("ma120.html", "text/html"),
                  "/ma120.css": ("ma120.css", "text/css"),
                  "/ma120.js": ("ma120.js", "text/javascript")}
        if path.startswith("/etf/") and len(path) > len("/etf/"):
            path = "/"
        if path not in assets:
            self.send_error(404)
            return
        filename, content_type = assets[path]
        body = (WEB_DIR / filename).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not allowed_request_host(self.headers.get("Host", ""), self.server.server_port):
            self._json(403, {"error": "仅允许本机访问"})
            return
        parsed = urlsplit(self.path)
        path = parsed.path
        if path not in ("/api/dashboard", "/api/history", "/api/estimate", "/api/ma120"):
            self._static(path)
            return
        try:
            if path == "/api/ma120":
                self._json(200, self.stock_service.current())
            elif path in ("/api/history", "/api/estimate"):
                params = parse_qs(parsed.query)
                plan_id = params.get("plan_id", [None])[0]
                if path == "/api/estimate":
                    self._json(200, self.service.estimate(plan_id, params.get("start_date", [None])[0]))
                else:
                    self._json(200, self.service.history(plan_id))
            else:
                self._json(200, self.service.dashboard())
        except (OSError, ValueError, KeyError, sqlite3.Error) as exc:
            self._json(500, {"error": str(exc)})

    def do_POST(self):
        if not allowed_request_host(self.headers.get("Host", ""), self.server.server_port):
            self._json(403, {"error": "仅允许本机访问"})
            return
        path = urlsplit(self.path).path
        if path not in ("/api/refresh", "/api/trades", "/api/ma120/refresh"):
            self.send_error(404)
            return
        origin = self.headers.get("Origin")
        if origin and origin != f"http://{self.headers.get('Host')}":
            self._json(403, {"error": "跨站请求已拒绝"})
            return
        if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
            self._json(415, {"error": "需要 JSON 请求"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 <= length <= 16384:
                raise ValueError("请求内容过大")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("请求格式无效")
            if path == "/api/ma120/refresh":
                result = self.stock_service.refresh()
            elif path == "/api/refresh":
                result = self.service.dashboard(refresh=True)
            else:
                result = self.service.add_trade(payload)
            self._json(200, result)
        except (ValueError, TypeError, KeyError, sqlite3.Error) as exc:
            self._json(400, {"error": str(exc)})
        except OSError as exc:
            self._json(500, {"error": str(exc)})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if args.host not in ("127.0.0.1", "localhost"):
        parser.error("无登录服务只允许监听 127.0.0.1 或 localhost")
    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    print(f"ETF dashboard: http://{args.host}:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
