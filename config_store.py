"""Transactional SQLite configuration and one-time legacy imports."""

import hashlib
import json
import sqlite3
import threading
import time
import tomllib
from contextlib import closing
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from config_validation import validate_config, number
from market_store import MarketStore, DEFAULT_DATABASE, BASE_DIR, TIMEZONE, encode


SCHEMA = BASE_DIR / "doc" / "configuration-schema.sql"
MA_DEFAULTS = {"ma_days": 120, "buy_factor": 0.88, "sell_factor": 1.12, "history_days": 200}


def cents(value):
    result = Decimal(str(value)) * 100
    if result != result.to_integral_value():
        raise ValueError("定投金额最多保留两位小数")
    if result > 9223372036854775807:
        raise ValueError("定投金额超出可用范围")
    return int(result)


class ConfigurationStore:
    # 保护「读-改-写」复合操作，避免同进程多线程互相覆盖。
    # 必须是**类级**锁：`ConfigurationService` 每个请求都新建一个
    # ConfigurationStore 实例（见 web_app.py），若锁挂在实例上，
    # 每次请求拿到的是不同锁，跨请求的并发写仍会互相覆盖 —— 那正是
    # 关注池条目丢失的真实场景。数据库的 BEGIN IMMEDIATE 只能保证
    # 单次事务原子，无法保证 operation 基于最新快照计算。
    _write_lock = threading.RLock()

    def __init__(self, database=DEFAULT_DATABASE, root=None):
        self.database = Path(database)
        self.root = Path(root) if root is not None else self.database.resolve().parent.parent
        self.market = MarketStore(self.database)
        self._write_lock = self.__class__._write_lock

    def connect(self):
        db = self.market.connect()
        db.executescript(SCHEMA.read_text(encoding="utf-8"))
        return db

    @staticmethod
    def _meta(db, key):
        row = db.execute("SELECT value FROM configuration_meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    @staticmethod
    def _set_meta(db, key, value):
        db.execute("INSERT INTO configuration_meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (key, str(value)))

    @staticmethod
    def _instrument(db, code, name, kind="stock"):
        now = datetime.now(TIMEZONE).isoformat()
        currency = "HKD" if code.startswith("HK") else "CNY"
        db.execute("INSERT INTO instruments VALUES (?,?,?,?,?,?) ON CONFLICT(code) DO UPDATE SET name=excluded.name,updated_at=excluded.updated_at",
                   (code, name.strip(), kind, currency, now, now))
        if kind == "etf":
            db.execute("UPDATE instruments SET kind='etf' WHERE code=?", (code,))

    def _legacy_stocks(self, section):
        from watchlist_store import read_watchlist, _validate_items as validate_watchlist
        from portfolio_store import read_portfolio, _validate_items as validate_portfolio
        path = self.root / f"{section}.json"
        if path.exists():
            return (read_watchlist(path) if section == "watchlist" else read_portfolio(path)), path
        workbook = self.root / "stocks.xlsx"
        if not workbook.exists():
            return [], None
        from openpyxl import load_workbook
        wb = load_workbook(workbook, read_only=True, data_only=True)
        try:
            if section not in wb.sheetnames:
                return [], workbook
            rows = list(wb[section].iter_rows(values_only=True))
            if not rows:
                return [], workbook
            aliases = {"股票代码": "code", "代码": "code", "股票名称": "name", "名称": "name",
                       "成本价": "cost", "成本": "cost", "持仓数量": "shares", "数量": "shares"}
            headings = [aliases.get(str(value).strip(), str(value).strip()) for value in rows[0]]
            items = []
            for values in rows[1:]:
                row = dict(zip(headings, values))
                if not row.get("code") or str(row["code"]).startswith("#"):
                    continue
                code = str(row["code"]).removesuffix(".0")
                item = {"code": code, "name": str(row.get("name") or "").strip()}
                if section == "portfolio":
                    item.update(cost=float(row.get("cost") or 0), shares=float(row.get("shares") or 0))
                items.append(item)
            return (validate_watchlist(items) if section == "watchlist" else validate_portfolio(items)), workbook
        finally:
            wb.close()

    @staticmethod
    def _import_record(db, source, section):
        if source is not None:
            db.execute("INSERT OR IGNORE INTO configuration_imports VALUES (?,?,?)",
                       (f"{source.resolve()}:{section}", hashlib.sha256(source.read_bytes()).hexdigest(),
                        datetime.now(TIMEZONE).isoformat()))

    def _needs_seed(self, db) -> bool:
        """只读判断是否还有未播种的段，不开写事务。

        `initialize()` 被每个读写入口调用，若像原先那样无条件
        `BEGIN IMMEDIATE`，纯读取也会占写锁；多线程并发时后到的连接
        会拿到 `attempt to write a readonly database`，导致关注池/持仓
        条目丢失。先用只读探测，确需播种时才开写事务。
        """
        for section in ("watchlist", "portfolio"):
            if not self._meta(db, f"{section}_initialized"):
                return True
        if not self._meta(db, "etf_initialized"):
            return True
        return self._meta(db, "revision") is None

    def initialize(self):
        """Seed each section once. A deliberately emptied database stays empty."""
        # connect() 本身就会 executescript 执行建表 DDL，属于写操作，
        # 必须整段持锁；否则这里仍会与并发写抢 SQLite 写锁。
        with self._write_lock:
            with closing(self.connect()) as db:
                if not self._needs_seed(db):
                    return
                with closing(self.connect()) as db2, db2:
                    self._seed_with(db2)

    def _seed_with(self, db):
        """在已开启的写事务内补齐未播种的段，幂等。"""
        db.execute("BEGIN IMMEDIATE")
        # 取得写锁后重新判断：可能已被并发的另一个线程播种完成。
        for section in ("watchlist", "portfolio"):
            if self._meta(db, f"{section}_initialized"):
                continue
            items, source = self._legacy_stocks(section)
            self._replace_stocks(db, section, items)
            self._set_meta(db, f"{section}_initialized", 1)
            self._import_record(db, source, section)
        if not self._meta(db, "etf_initialized"):
            source = self.root / "etf_plans.toml"
            sample = not source.exists()
            if sample:
                source = BASE_DIR / "etf_plans.example.toml"
            config = validate_config(tomllib.loads(source.read_text(encoding="utf-8-sig")), self.root)
            self._save_rules(db, config["settings"], config["valuation"], MA_DEFAULTS)
            for index, plan in enumerate(config["plans"]):
                self._put_plan(db, plan, index)
            self._set_meta(db, "etf_initialized", 1)
            self._set_meta(db, "sample_mode", int(sample))
            self._import_record(db, source, "etf")
        if self._meta(db, "revision") is None:
            self._set_meta(db, "revision", 1)
            self._audit(db, "initial_import")

    def _replace_stocks(self, db, section, items):
        table = "watchlist_entries" if section == "watchlist" else "portfolio_positions"
        db.execute(f"DELETE FROM {table}")
        for index, item in enumerate(items):
            self._instrument(db, item["code"], item["name"])
            if section == "watchlist":
                db.execute("INSERT INTO watchlist_entries VALUES (?,?)", (item["code"], index))
            else:
                db.execute("INSERT INTO portfolio_positions VALUES (?,?,?,?)",
                           (item["code"], item["cost"], item["shares"], index))

    @staticmethod
    def _stocks(db, section):
        if section == "watchlist":
            sql = "SELECT i.code,i.name FROM watchlist_entries e JOIN instruments i ON i.code=e.code ORDER BY e.sort_order,i.code"
        else:
            sql = "SELECT i.code,i.name,p.cost,p.shares FROM portfolio_positions p JOIN instruments i ON i.code=p.code ORDER BY p.sort_order,i.code"
        return [dict(row) for row in db.execute(sql)]

    def stocks(self, section):
        self.initialize()
        with closing(self.connect()) as db:
            return self._stocks(db, section)

    def mutate_stocks(self, section, operation):
        from watchlist_store import _validate_items as validate_watchlist
        from portfolio_store import _validate_items as validate_portfolio
        self.initialize()
        # 同一进程内多个线程共享一个 store 实例时（例如 Web 工作台的
        # ThreadingHTTPServer），必须把「读当前列表 → 追加 → 全量替换」串行化，
        # 否则后到的线程会基于过期的快照覆盖前者的结果，表现为关注池条目丢失。
        # 锁只解决本进程竞争；跨进程（如GitHub Actions 与本机同时写库）时
        # SQLite 仍可能拒绝写，此时重试而不是丢弃用户的操作。
        with self._write_lock:
            for attempt in range(4):
                try:
                    with closing(self.connect()) as db, db:
                        db.execute("BEGIN IMMEDIATE")
                        items = operation(self._stocks(db, section))
                        items = (validate_watchlist(items) if section == "watchlist"
                                 else validate_portfolio(items))
                        self._replace_stocks(db, section, items)
                        self._changed(db, f"update_{section}")
                        return items
                except sqlite3.OperationalError:
                    if attempt == 3:
                        raise
                    time.sleep(0.2 * (attempt + 1))

    @staticmethod
    def _plans(db, include_disabled=False):
        sql = "SELECT p.*,i.name FROM etf_plans p JOIN instruments i ON i.code=p.code"
        if not include_disabled:
            sql += " WHERE p.enabled=1"
        result = []
        for row in db.execute(sql + " ORDER BY p.sort_order,p.id"):
            plan = {"id": row["id"], "code": row["code"], "name": row["name"],
                    "index_code": row["index_code"], "strategy": row["strategy"],
                    "base_amount": row["base_amount_cents"] / 100, "max_amount": row["max_amount_cents"] / 100,
                    "frequency": row["frequency"], "cost": row["initial_cost"], "shares": row["initial_shares"],
                    "valuation_csv": row["valuation_source"], "strategy_reason": row["strategy_reason"],
                    "enabled": bool(row["enabled"])}
            if row["weekday"] is not None:
                plan["weekday"] = row["weekday"]
            if row["monthday"] is not None:
                plan["monthday"] = row["monthday"]
            result.append(plan)
        return result

    def _config(self, db, include_disabled=False):
        settings = dict(db.execute("SELECT ma_days,lot_size,above_multiplier FROM dca_settings WHERE id=1").fetchone())
        settings["bands"] = [dict(row) for row in db.execute("SELECT below,multiplier FROM dca_amount_bands ORDER BY sort_order")]
        valuation = dict(db.execute("SELECT lookback_years,min_samples,min_span_days,max_age_days,buy_below,high_at FROM valuation_policy WHERE id=1").fetchone())
        return {"version": 1, "settings": settings, "valuation": valuation,
                "plans": self._plans(db, include_disabled), "directory": self.root}

    def config(self, include_disabled=False):
        self.initialize()
        with closing(self.connect()) as db:
            return validate_config(self._config(db, include_disabled), self.root, True)

    def summary(self):
        self.initialize()
        with closing(self.connect()) as db:
            config = self._config(db, True)
            config.pop("directory")
            return {"revision": int(self._meta(db, "revision")), "watchlist": self._stocks(db, "watchlist"),
                    "portfolio": self._stocks(db, "portfolio"), "etf": config,
                    "ma120": dict(db.execute("SELECT ma_days,buy_factor,sell_factor,history_days FROM ma120_settings WHERE id=1").fetchone()),
                    "sample": self._meta(db, "sample_mode") == "1"}

    def revision(self):
        self.initialize()
        with closing(self.connect()) as db:
            return int(self._meta(db, "revision"))

    def _audit(self, db, operation):
        config = self._config(db, True)
        config.pop("directory")
        snapshot = {"etf": config, "watchlist": self._stocks(db, "watchlist"), "portfolio": self._stocks(db, "portfolio"),
                    "ma120": dict(db.execute("SELECT ma_days,buy_factor,sell_factor,history_days FROM ma120_settings WHERE id=1").fetchone())}
        db.execute("INSERT INTO configuration_changes(changed_at,operation,snapshot_json) VALUES (?,?,?)",
                   (datetime.now(TIMEZONE).isoformat(), operation, encode(snapshot)))

    def _changed(self, db, operation):
        self._set_meta(db, "revision", int(self._meta(db, "revision") or 0) + 1)
        self._audit(db, operation)

    @staticmethod
    def _save_rules(db, settings, valuation, ma):
        db.execute("INSERT OR REPLACE INTO dca_settings VALUES (1,?,?,?)",
                   (settings["ma_days"], settings["lot_size"], settings["above_multiplier"]))
        db.execute("DELETE FROM dca_amount_bands")
        db.executemany("INSERT INTO dca_amount_bands VALUES (?,?,?)",
                       [(index, band["below"], band["multiplier"]) for index, band in enumerate(settings["bands"])])
        db.execute("INSERT OR REPLACE INTO valuation_policy VALUES (1,?,?,?,?,?,?)",
                   tuple(valuation[k] for k in ("lookback_years", "min_samples", "min_span_days", "max_age_days", "buy_below", "high_at")))
        db.execute("INSERT OR REPLACE INTO ma120_settings VALUES (1,?,?,?,?)",
                   tuple(ma[k] for k in ("ma_days", "buy_factor", "sell_factor", "history_days")))

    def save_rules(self, settings, valuation, ma):
        self.initialize()
        validate_config({"version": 1, "settings": settings, "valuation": valuation, "plans": []}, self.root, True)
        if type(ma["ma_days"]) is not int or not 2 <= ma["ma_days"] <= 600 or type(ma["history_days"]) is not int or ma["history_days"] < ma["ma_days"]:
            raise ValueError("股票均线和历史长度无效")
        number(ma["buy_factor"], "买入线倍数", 0.000001)
        number(ma["sell_factor"], "卖出线倍数", 1.000001)
        if ma["buy_factor"] >= 1:
            raise ValueError("买入线倍数须小于 1")
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            self._save_rules(db, settings, valuation, ma)
            self._set_meta(db, "sample_mode", 0)
            self._changed(db, "update_rules")

    def _put_plan(self, db, plan, order):
        now = datetime.now(TIMEZONE).isoformat()
        source = plan.get("valuation_csv") or f"etf_valuations/{plan['index_code']}.csv"
        if Path(source).is_absolute() or not (self.root / source).resolve().is_relative_to(self.root.resolve()) or Path(source).suffix.lower() != ".csv":
            raise ValueError("估值来源须为项目内的 CSV 标识")
        self._instrument(db, plan["code"], plan["name"], "etf")
        db.execute("INSERT OR IGNORE INTO tracked_indices VALUES (?)", (plan["index_code"],))
        values = (plan["id"], plan["code"], plan["index_code"], plan["strategy"], cents(plan["base_amount"]),
                  cents(plan["max_amount"]), plan["frequency"], plan.get("weekday") if plan["frequency"] == "weekly" else None,
                  plan.get("monthday") if plan["frequency"] == "monthly" else None, plan.get("cost", 0),
                  plan.get("shares", 0), source, plan.get("strategy_reason", ""), int(plan.get("enabled", True)), order, now, now)
        db.execute("""INSERT INTO etf_plans VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
            index_code=excluded.index_code,strategy=excluded.strategy,base_amount_cents=excluded.base_amount_cents,
            max_amount_cents=excluded.max_amount_cents,frequency=excluded.frequency,weekday=excluded.weekday,
            monthday=excluded.monthday,initial_cost=excluded.initial_cost,initial_shares=excluded.initial_shares,
            valuation_source=excluded.valuation_source,strategy_reason=excluded.strategy_reason,enabled=excluded.enabled,updated_at=excluded.updated_at""", values)

    def save_plan(self, plan, existing_id=None):
        self.initialize()
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            config = self._config(db, True)
            existing = next((p for p in config["plans"] if p["id"] == existing_id), None)
            if existing_id is not None and existing is None:
                raise LookupError("计划不存在")
            if existing and (plan["id"] != existing_id or plan["code"] != existing["code"]):
                raise ValueError("已有计划的 ID 和 ETF 代码不能更换，请新建计划")
            if not existing and any(p["id"] == plan["id"] or p["code"] == plan["code"] for p in config["plans"]):
                raise ValueError("重复计划 ID 或 ETF 代码")
            journal = self.root / "dca_results" / "journal.sqlite3"
            if existing and (plan.get("cost", 0), plan.get("shares", 0)) != (existing["cost"], existing["shares"]) and journal.exists():
                with closing(sqlite3.connect(f"{journal.resolve().as_uri()}?mode=ro", uri=True)) as ledger:
                    if ledger.execute("SELECT 1 FROM fills WHERE plan_id=? LIMIT 1", (existing_id,)).fetchone():
                        raise ValueError("已有成交登记，不能修改初始持仓；请通过成交账本维护")
            plan = {**plan, "valuation_csv": plan.get("valuation_csv") or f"etf_valuations/{plan['index_code']}.csv"}
            config["plans"] = [p for p in config["plans"] if p["id"] != existing_id] + [plan]
            validate_config(config, self.root)
            order = db.execute("SELECT sort_order FROM etf_plans WHERE id=?", (existing_id,)).fetchone() if existing else None
            self._put_plan(db, plan, order[0] if order else len(config["plans"]) - 1)
            self._set_meta(db, "sample_mode", 0)
            self._changed(db, "update_plan" if existing else "create_plan")

    def disable_plan(self, plan_id):
        self.initialize()
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            result = db.execute("UPDATE etf_plans SET enabled=0,updated_at=? WHERE id=?", (datetime.now(TIMEZONE).isoformat(), plan_id))
            if result.rowcount == 0:
                raise LookupError("计划不存在")
            self._set_meta(db, "sample_mode", 0)
            self._changed(db, "disable_plan")
