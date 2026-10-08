"""Local ETF dashboard backed by the existing recommendation engine."""

import argparse
import json
import sqlite3
import threading
from contextlib import closing
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from etf_data import DataError, fetch_market, parse_history
from etf_valuation_replay import ValuationReplay
from etf_catalog import ETF_DEFINITIONS
from etf_dca import (BASE_DIR, STRATEGIES, build_report, evaluate, load_config, open_journal,
                     period, record_trade)
from ma120_web import build_ma120_dashboard, build_ma120_history, estimate_ma120_profit
from scripts.update_csi300_valuation import update_valuation
from watchlist_store import read_watchlist, write_watchlist, normalize_code


WEB_DIR = BASE_DIR / "web"
TIMEZONE = ZoneInfo("Asia/Shanghai")
ETF_HISTORY_BARS = 5000


def allowed_request_host(host, port):
    return host.lower() in {f"127.0.0.1:{port}", f"localhost:{port}"}


def _simulate_buys(plan, settings, series, start):
    """Apply the selected plan to an empty virtual position on completed bars."""
    marked_periods = set()
    buys = []
    held = 0
    cost = 0.0
    for point in series:
        day = date.fromisoformat(point["date"])
        if day < start:
            continue
        period_id, due = period(plan, day)
        if not due or period_id in marked_periods:
            continue
        market = {"price": point["price"], "ma": point["ma"],
                  "ma_days": settings["ma_days"],
                  "ma_deviation_pct": (point["price"] / point["ma"] - 1) * 100
                  if point["ma"] else None}
        try:
            result = evaluate(plan, plan["strategy"], market, point.get("valuation"),
                              point.get("valuation_error", ""), cost, held, settings)
        except (ValueError, KeyError):
            continue
        shares = result["reference_shares"]
        if not shares:
            continue
        marked_periods.add(period_id)
        paid = result["estimated_cost"]
        cost = (held * cost + paid) / (held + shares)
        held += shares
        buys.append({"date": point["date"], "price": point["price"],
                     "adjusted_price": point["adjusted_price"],
                     "amount": result["reference_amount"], "shares": shares,
                     "cost": paid, "reason": result["reason"]})
    return buys


def replay_history(plan, settings, payload, fills, today, display_days=240, valuation_path=None):
    """Replay the selected strategy on completed bars; actual fills remain separate."""
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
    chart_scale = float(raw.loc[completed[-1]]) / float(adjusted.loc[completed[-1]])
    series = []
    valuation_error = ""
    valuation_lookup = None
    if plan["strategy"] == "valuation":
        try:
            if valuation_path is None:
                raise DataError("缺少估值历史路径")
            valuation_lookup = ValuationReplay(Path(valuation_path), plan["index_code"], settings)
        except (ValueError, KeyError, OSError) as exc:
            valuation_error = str(exc)
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
        point = {"date": day.isoformat(), "price": price,
                 "adjusted_price": adjusted_price, "ma": ma,
                 "chart_price": adjusted_price * chart_scale, "chart_ma": chart_ma}
        if plan["strategy"] == "valuation":
            try:
                if valuation_lookup is None:
                    raise DataError(valuation_error)
                point["valuation"] = valuation_lookup.at(day - timedelta(days=1))
            except (ValueError, KeyError, OSError) as exc:
                point["valuation_error"] = str(exc)
                valuation_error = str(exc)
        series.append(point)
    dates = {point["date"] for point in series}
    trades = [{"date": row["trade_date"], "side": row["side"], "price": row["price"],
               "shares": row["shares"], "fill_id": row["fill_id"],
               "on_chart": row["trade_date"] in dates}
              for row in fills]
    simulation_start = next((point["date"] for point in series
                             if plan["strategy"] == "drawdown"
                             or (plan["strategy"] == "ma" and point["ma"] is not None)
                             or (plan["strategy"] == "valuation" and point.get("valuation") is not None)), None)
    reference_buys = (_simulate_buys(plan, settings, series, date.fromisoformat(simulation_start))
                      if simulation_start else [])
    if simulation_start:
        replay_reason = ""
    elif plan["strategy"] == "valuation":
        replay_reason = valuation_error or "没有可用的历史估值数据"
    elif plan["strategy"] == "ma":
        replay_reason = f"MA{ma_days} 需要足够的已完成日 K"
    else:
        replay_reason = "没有可回放的已完成日 K"
    return {"series": series, "reference_buys": reference_buys,
            "trades": trades, "replay_supported": simulation_start is not None,
            "replay_reason": replay_reason,
            "ma_days": ma_days, "window_start": series[0]["date"],
            "window_end": series[-1]["date"], "simulation_start": simulation_start}


def estimate_profit(plan, settings, history, start_date):
    """Mark virtual purchases to the latest adjusted close, excluding real fills."""
    if not history["replay_supported"]:
        return {"supported": False, "reason": history.get("replay_reason") or "当前策略没有可验证的历史买入回放"}
    start = date.fromisoformat(start_date)
    first = date.fromisoformat(history["simulation_start"])
    last = date.fromisoformat(history["window_end"])
    if not first <= start <= last:
        raise ValueError(f"起算日期须在 {first} 至 {last} 之间")

    final_adjusted = history["series"][-1]["adjusted_price"]
    buys = _simulate_buys(plan, settings, history["series"], start)
    invested = 0.0
    estimated_value = 0.0
    for buy in buys:
        invested += buy["cost"]
        estimated_value += buy["cost"] * final_adjusted / buy["adjusted_price"]
    invested = round(invested, 2)
    estimated_value = round(estimated_value, 2)
    profit = round(estimated_value - invested, 2)
    return {"supported": True, "start_date": start.isoformat(), "end_date": last.isoformat(),
            "buy_count": len(buys), "invested": invested, "estimated_value": estimated_value,
            "profit": profit, "return_pct": round(profit / invested * 100, 2) if invested else None,
            "buys": buys}


class DashboardService:
    def __init__(self, config_path=BASE_DIR / "etf_plans.toml", market_loader=None,
                 valuation_updater=None):
        self.config_path = Path(config_path)
        self.market_loader = market_loader
        self.valuation_updater = valuation_updater if valuation_updater is not None else update_valuation

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
        valuation_update_warnings = []
        if refresh:
            now = datetime.now(TIMEZONE)
            updated_paths = set()
            for plan in config["plans"]:
                if plan["strategy"] != "valuation" or plan["index_code"] != "000300":
                    continue
                output = config["directory"] / plan["valuation_csv"]
                if output in updated_paths:
                    continue
                updated_paths.add(output)
                try:
                    self.valuation_updater(output=output, today=now.date(), settings=config["valuation"])
                except (OSError, ValueError) as exc:
                    valuation_update_warnings.append(f"{plan['name']}估值历史更新失败：{exc}")
        with closing(self._journal(config, sample)) as db:
            if refresh:
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
                       "strategy_reason": plan.get("strategy_reason", ""),
                       "definition": ETF_DEFINITIONS.get(plan["code"])}
                      for plan in config["plans"]],
            "strategies": STRATEGIES,
            "strategy_settings": {
                "ma_days": config["settings"]["ma_days"],
                "lot_size": config["settings"]["lot_size"],
                "buy_below": config["valuation"]["buy_below"],
                "high_at": config["valuation"]["high_at"],
            },
            "report": report,
            "history": history,
            "fills": fills,
            "valuation_update_warnings": valuation_update_warnings,
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
        payload = loader(plan["code"], ETF_HISTORY_BARS, now,
                         config["directory"] / ".stock_cache" / "etf")
        fills = []
        if not sample:
            with closing(self._journal(config, False)) as db:
                fills = [dict(row) for row in db.execute(
                    "SELECT fill_id, side, shares, price, trade_date FROM fills WHERE plan_id=? "
                    "ORDER BY trade_date, rowid", (plan_id,))]
        settings = {**config["settings"], **config["valuation"]}
        return replay_history(plan, settings, payload, fills, now.date(),
                              display_days=ETF_HISTORY_BARS,
                              valuation_path=config["directory"] / plan["valuation_csv"])

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
    def __init__(self, loader=build_ma120_dashboard, history_loader=build_ma120_history,
                 watchlist_path=None):
        self.loader = loader
        self.history_loader = history_loader
        self.watchlist_path = watchlist_path
        self.snapshot = None
        self.lock = threading.Lock()

    def current(self):
        return {"snapshot": self.snapshot}

    def refresh(self):
        with self.lock:
            self.snapshot = self.loader()
            return {"snapshot": self.snapshot}

    def watchlist(self):
        with self.lock:
            return {"items": read_watchlist(self.watchlist_path)}

    def _change_watchlist(self, operation):
        with self.lock:
            items = read_watchlist(self.watchlist_path)
            updated = write_watchlist(operation(items), self.watchlist_path)
            try:
                self.snapshot = self.loader()
            except Exception as exc:
                self.snapshot = None
                return {"items": updated, "refresh_warning": f"关注池已保存，行情刷新失败：{exc}"}
            return {"items": updated}

    def add_watchlist(self, payload):
        code = normalize_code(payload.get("code"))
        name = payload.get("name")

        def add(items):
            if any(item["code"] == code for item in items):
                raise ValueError("股票已在关注池中")
            return [*items, {"code": code, "name": name}]

        return self._change_watchlist(add)

    def rename_watchlist(self, code, payload):
        code = normalize_code(code)
        name = payload.get("name")

        def rename(items):
            if not any(item["code"] == code for item in items):
                raise LookupError("股票不在关注池中")
            return [{**item, "name": name} if item["code"] == code else item for item in items]

        return self._change_watchlist(rename)

    def remove_watchlist(self, code):
        code = normalize_code(code)

        def remove(items):
            kept = [item for item in items if item["code"] != code]
            if len(kept) == len(items):
                raise LookupError("股票不在关注池中")
            return kept

        return self._change_watchlist(remove)

    def history(self, code):
        if not code:
            raise ValueError("请选择股票")
        snapshot = self.snapshot
        if snapshot is None:
            raise ValueError("请先刷新 MA120 行情")
        item = next((row for row in snapshot.get("portfolio", []) + snapshot.get("watchlist", [])
                     if row.get("code") == code), None)
        if item is None:
            raise ValueError("股票不在当前持仓或关注池")
        return self.history_loader(code, item["name"])

    def estimate(self, code, start_date):
        return estimate_ma120_profit(self.history(code), start_date)


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
        if path.startswith("/ma120/") and len(path) > len("/ma120/"):
            path = "/ma120"
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
        if path not in ("/api/dashboard", "/api/history", "/api/estimate", "/api/ma120",
                        "/api/ma120/history", "/api/ma120/estimate", "/api/ma120/watchlist"):
            self._static(path)
            return
        try:
            if path == "/api/ma120":
                self._json(200, self.stock_service.current())
            elif path == "/api/ma120/watchlist":
                self._json(200, self.stock_service.watchlist())
            elif path == "/api/ma120/history":
                code = parse_qs(parsed.query).get("code", [None])[0]
                try:
                    self._json(200, self.stock_service.history(code))
                except ValueError as exc:
                    self._json(400, {"error": str(exc)})
            elif path == "/api/ma120/estimate":
                params = parse_qs(parsed.query)
                try:
                    self._json(200, self.stock_service.estimate(
                        params.get("code", [None])[0], params.get("start_date", [None])[0]))
                except ValueError as exc:
                    self._json(400, {"error": str(exc)})
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

    def _mutation_allowed(self):
        if not allowed_request_host(self.headers.get("Host", ""), self.server.server_port):
            self._json(403, {"error": "仅允许本机访问"})
            return False
        origin = self.headers.get("Origin")
        if origin and origin != f"http://{self.headers.get('Host')}":
            self._json(403, {"error": "跨站请求已拒绝"})
            return False
        return True

    def _read_json(self):
        if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
            raise TypeError("需要 JSON 请求")
        length = int(self.headers.get("Content-Length", "0"))
        if not 0 < length <= 16384:
            raise ValueError("请求内容不能为空或过大")
        payload = json.loads(self.rfile.read(length))
        if not isinstance(payload, dict):
            raise ValueError("请求格式无效")
        return payload

    @staticmethod
    def _watchlist_code(query):
        values = parse_qs(query).get("code", [])
        if len(values) != 1:
            raise ValueError("请指定唯一股票代码")
        return values[0]

    def do_POST(self):
        if not self._mutation_allowed():
            return
        path = urlsplit(self.path).path
        if path not in ("/api/refresh", "/api/trades", "/api/ma120/refresh",
                        "/api/ma120/watchlist"):
            self.send_error(404)
            return
        try:
            payload = self._read_json()
            if path == "/api/ma120/refresh":
                result = self.stock_service.refresh()
            elif path == "/api/ma120/watchlist":
                result = self.stock_service.add_watchlist(payload)
            elif path == "/api/refresh":
                result = self.service.dashboard(refresh=True)
            else:
                result = self.service.add_trade(payload)
            self._json(200, result)
        except TypeError as exc:
            self._json(415, {"error": str(exc)})
        except (ValueError, KeyError, sqlite3.Error) as exc:
            self._json(400, {"error": str(exc)})
        except OSError as exc:
            self._json(500, {"error": str(exc)})

    def do_PATCH(self):
        if not self._mutation_allowed():
            return
        parsed = urlsplit(self.path)
        if parsed.path != "/api/ma120/watchlist":
            self.send_error(404)
            return
        try:
            result = self.stock_service.rename_watchlist(
                self._watchlist_code(parsed.query), self._read_json())
            self._json(200, result)
        except TypeError as exc:
            self._json(415, {"error": str(exc)})
        except LookupError as exc:
            self._json(404, {"error": str(exc)})
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
        except OSError as exc:
            self._json(500, {"error": str(exc)})

    def do_DELETE(self):
        if not self._mutation_allowed():
            return
        parsed = urlsplit(self.path)
        if parsed.path != "/api/ma120/watchlist":
            self.send_error(404)
            return
        try:
            if int(self.headers.get("Content-Length", "0")) != 0:
                raise ValueError("删除请求不接受正文")
            self._json(200, self.stock_service.remove_watchlist(
                self._watchlist_code(parsed.query)))
        except LookupError as exc:
            self._json(404, {"error": str(exc)})
        except ValueError as exc:
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
