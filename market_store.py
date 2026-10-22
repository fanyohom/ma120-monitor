"""Durable market data. Complete adjusted-history versions are never mixed."""

import hashlib
import json
import math
import sqlite3
import threading
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


TIMEZONE = ZoneInfo("Asia/Shanghai")
BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DATABASE = BASE_DIR / "data" / "market.sqlite3"

SCHEMA = """
    CREATE TABLE IF NOT EXISTS history_versions (
        id INTEGER PRIMARY KEY, kind TEXT NOT NULL, code TEXT NOT NULL,
        fetched_at TEXT NOT NULL, fetched_epoch REAL NOT NULL,
        bar_count INTEGER NOT NULL, metadata TEXT NOT NULL, digest TEXT NOT NULL,
        UNIQUE(kind,code,fetched_at,digest)
    );
    CREATE INDEX IF NOT EXISTS history_lookup ON history_versions(kind,code,fetched_epoch DESC);
    CREATE TABLE IF NOT EXISTS daily_bars (
        version_id INTEGER NOT NULL REFERENCES history_versions(id),
        adjustment TEXT NOT NULL, trade_date TEXT NOT NULL,
        open REAL NOT NULL, close REAL NOT NULL, high REAL, low REAL, volume REAL,
        row_json TEXT NOT NULL, PRIMARY KEY(version_id,adjustment,trade_date)
    );
    CREATE TABLE IF NOT EXISTS quotes (
        id INTEGER PRIMARY KEY, kind TEXT NOT NULL, code TEXT NOT NULL,
        fetched_at TEXT NOT NULL, quote_at TEXT NOT NULL, payload TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS quote_lookup ON quotes(kind,code,id DESC);
    CREATE TABLE IF NOT EXISTS dashboard_snapshots (
        name TEXT PRIMARY KEY, generated_at TEXT NOT NULL, payload TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS valuation_versions (
        id INTEGER PRIMARY KEY, series_key TEXT NOT NULL, index_code TEXT NOT NULL,
        imported_at TEXT NOT NULL, digest TEXT NOT NULL,
        UNIQUE(series_key,index_code,digest)
    );
    CREATE TABLE IF NOT EXISTS index_valuations (
        version_id INTEGER NOT NULL REFERENCES valuation_versions(id),
        trade_date TEXT NOT NULL, available_date TEXT NOT NULL, pe_ttm REAL NOT NULL,
        PRIMARY KEY(version_id,trade_date)
    );
"""


def database_for_cache(cache_dir):
    path = Path(cache_dir).resolve()
    # Custom cache roots used by callers/tests have their own isolated store.
    if ".stock_cache" in path.parts:
        root = path
        while root.name != ".stock_cache":
            root = root.parent
        return root.parent / "data" / "market.sqlite3"
    return path / "market.sqlite3"


def database_for_valuation(path):
    path = Path(path).resolve()
    root = path.parent.parent if path.parent.name == "etf_valuations" else path.parent
    return root / "data" / "market.sqlite3"


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


class _WriteTransaction:
    """写事务上下文：BEGIN IMMEDIATE 立即取写锁，退出时提交或回滚。

    isolation_level=None 让 sqlite3 进入 autocommit 模式，
    事务边界完全由我们自己控制 —— 避免隐式 COMMIT 与其它连接竞争。
    """

    # 可重试的瞬时错误：readonly / busy / shm 相关。
    # WAL 模式下「最后一个连接关闭」会删除 -wal/-shm，
    # 与之竞争的连接会短暂失去写权限，重建连接即可恢复。
    RETRYABLE = (
        sqlite3.OperationalError,
    )
    RETRY_CODES = {8, 5, 6, 2570, 261, 262}
    RETRY_DELAYS = (0.02, 0.05, 0.15, 0.4)

    def __init__(self, store):
        self._store = store
        self._db = None

    def _open(self):
        self._db = self._store.connect()
        self._db.execute("BEGIN IMMEDIATE")

    def __enter__(self):
        # 进程内串行化写事务：见 MarketStore._write_lock 的说明。
        MarketStore._write_lock.acquire()
        try:
            last = None
            for attempt in range(len(self.RETRY_DELAYS) + 1):
                try:
                    self._open()
                    return self._db
                except self.RETRYABLE as exc:
                    last = exc
                    code = getattr(exc, "sqlite_errorcode", None)
                    if code not in self.RETRY_CODES or attempt == len(self.RETRY_DELAYS):
                        raise
                    if self._db is not None:
                        try:
                            self._db.close()
                        except sqlite3.Error:
                            pass
                        self._db = None
                    time.sleep(self.RETRY_DELAYS[attempt])
            raise last  # pragma: no cover
        except BaseException:
            MarketStore._write_lock.release()
            raise

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self._db.execute("COMMIT")
            else:
                self._db.execute("ROLLBACK")
        finally:
            try:
                self._db.close()
            finally:
                MarketStore._write_lock.release()
        return False


class MarketStore:
    # 建表是 DDL 写操作，且 sqlite3 的 executescript() 会先隐式 COMMIT。
    # 在 WAL 模式下多个连接并发建表时，SQLite 会把落败连接的写权限
    # 降级为 readonly，抛出 SQLITE_READONLY("attempt to write a readonly database")。
    # 这个异常原本被 get_stock_hist_data() 的宽 except 吞掉，
    # 导致部分标的静默回退到旧行情、当日信号被无声清空。
    #
    # 修法：把建表收敛成一次性动作 —— 用类级锁 + 每进程一次的标记，
    # 后续连接只做「表在不在」的轻量检查，不再重复执行 DDL。
    _schema_lock = threading.Lock()
    _schema_ready = set()

    # 写事务的进程内串行锁。
    #
    # 根因（2026-10-22 实测）：SQLite 在 WAL 模式下允许多连接并发读，
    # 但多个连接同时尝试「开写事务」时，落败连接会被降级为 readonly，
    # 抛 SQLITE_READONLY(8) / SQLITE_IOERR_SHMOPEN(2570)。
    # busy_timeout 对这类错误无效（它只处理 SQLITE_BUSY）。
    # 实测：4 线程并发写 160 次 → 约 2~8 次失败；
    #       同一实验加进程内锁后 → 160/160 全过。
    #
    # 看板刷新会用 4 线程并发拉取 41 支标的的日 K，每个线程都要写回
    # SQLite，因此必须串行化写。读路径不加锁，仍可并发。
    _write_lock = threading.RLock()

    def __init__(self, path=DEFAULT_DATABASE):
        self.path = Path(path)

    def _schema_key(self):
        try:
            return str(self.path.resolve())
        except OSError:
            return str(self.path)

    def _ensure_schema(self, db):
        """确保表结构存在。首次调用执行 DDL，之后只做轻量检查。"""
        key = self._schema_key()
        if key in MarketStore._schema_ready:
            return
        with MarketStore._schema_lock:
            if key in MarketStore._schema_ready:
                return
            exists = db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
                "('history_versions','daily_bars','quotes','dashboard_snapshots',"
                "'valuation_versions','index_valuations')"
            ).fetchall()
            if len(exists) < 6:
                # executescript() 会隐式 COMMIT，在 WAL 下与其它连接竞争易失败，
                # 故放在写锁内执行。
                with MarketStore._write_lock:
                    db.executescript(SCHEMA)
                    db.commit()
            MarketStore._schema_ready.add(key)

    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        # journal_mode 已是 wal 时，重复设置需要写锁、且会在并发下偶发失败；
        # 只在确实不是 wal 时才切换。
        try:
            mode = db.execute("PRAGMA journal_mode").fetchone()[0]
        except sqlite3.Error:
            mode = None
        if mode and str(mode).lower() != "wal":
            with MarketStore._write_lock:
                db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=30000")
        self._ensure_schema(db)
        return db

    def write(self):
        """打开一个写事务上下文。显式 BEGIN IMMEDIATE，避免升级锁时的写冲突。"""
        return _WriteTransaction(self)

    def put_history(self, kind, code, histories, metadata, fetched_at=None):
        instant = datetime.fromisoformat(fetched_at) if fetched_at else datetime.now(TIMEZONE)
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=TIMEZONE)
        encoded_rows = encode(histories)
        digest = hashlib.sha256(encoded_rows.encode()).hexdigest()
        count = min(len(rows) for rows in histories.values())
        if not count:
            raise ValueError("Cannot persist empty history")
        records = []
        for adjustment, rows in histories.items():
            dates = []
            for row in rows:
                day = datetime.fromisoformat(row[0]).date().isoformat()
                opening, close = float(row[1]), float(row[2])
                if not all(math.isfinite(value) and value > 0 for value in (opening, close)):
                    raise ValueError("Invalid history price")
                optional = [float(value) if value is not None else None for value in row[3:6]]
                optional += [None] * (3 - len(optional))
                if any(value is not None and not math.isfinite(value) for value in optional):
                    raise ValueError("Invalid history value")
                dates.append(day)
                records.append((adjustment, day, opening, close, *optional, encode(row)))
            if dates != sorted(set(dates)):
                raise ValueError("History dates must be unique and ordered")
        if kind == "etf" and [r[0] for r in histories["qfq"]] != [r[0] for r in histories["raw"]]:
            raise ValueError("Adjusted/raw history dates disagree")
        with self.write() as db:
            db.execute("INSERT OR IGNORE INTO history_versions(kind,code,fetched_at,fetched_epoch,bar_count,metadata,digest) VALUES (?,?,?,?,?,?,?)",
                       (kind, code, instant.isoformat(), instant.timestamp(), count, encode(metadata), digest))
            version = db.execute("SELECT id FROM history_versions WHERE kind=? AND code=? AND fetched_at=? AND digest=?",
                                 (kind, code, instant.isoformat(), digest)).fetchone()[0]
            db.executemany("INSERT OR IGNORE INTO daily_bars VALUES (?,?,?,?,?,?,?,?,?)",
                           [(version, *record) for record in records])
        return version

    def get_history(self, kind, code, min_rows=1, longest=False):
        if not self.path.exists():
            return None
        order = "bar_count DESC,fetched_epoch DESC,id DESC" if longest else "fetched_epoch DESC,id DESC"
        with closing(self.connect()) as db:
            version = db.execute(f"SELECT * FROM history_versions WHERE kind=? AND code=? AND bar_count>=? ORDER BY {order} LIMIT 1",
                                 (kind, code, min_rows)).fetchone()
            if version is None:
                return None
            result = json.loads(version["metadata"])
            for row in db.execute("SELECT adjustment,row_json FROM daily_bars WHERE version_id=? ORDER BY adjustment,trade_date", (version["id"],)):
                result.setdefault(row["adjustment"], []).append(json.loads(row["row_json"]))
            result.update({"fetched_at": version["fetched_at"], "database_version": version["id"],
                           "database_path": str(self.path), "read_source": "database"})
            return result

    def put_quote(self, kind, code, quote, fetched_at=None):
        fetched_at = fetched_at or datetime.now(TIMEZONE).isoformat()
        quote_at = quote.get("quote_at") or (quote.get("quote_date", "") + "T" + quote.get("quote_time", ""))
        if not quote_at or not math.isfinite(float(quote["price"])) or float(quote["price"]) <= 0:
            raise ValueError("Invalid quote")
        with self.write() as db:
            db.execute("INSERT INTO quotes(kind,code,fetched_at,quote_at,payload) VALUES (?,?,?,?,?)",
                       (kind, code, fetched_at, quote_at, encode(quote)))

    def get_quote(self, kind, code):
        if not self.path.exists():
            return None
        with closing(self.connect()) as db:
            row = db.execute("SELECT * FROM quotes WHERE kind=? AND code=? ORDER BY fetched_at DESC,id DESC LIMIT 1", (kind, code)).fetchone()
        if row is None:
            return None
        return {**json.loads(row["payload"]), "fetched_at": row["fetched_at"], "read_source": "database"}

    def put_dashboard(self, name, payload):
        with self.write() as db:
            db.execute("INSERT INTO dashboard_snapshots VALUES (?,?,?) ON CONFLICT(name) DO UPDATE SET generated_at=excluded.generated_at,payload=excluded.payload",
                       (name, payload["generated_at"], encode(payload)))

    def get_dashboard(self, name):
        if not self.path.exists():
            return None
        with closing(self.connect()) as db:
            row = db.execute("SELECT payload FROM dashboard_snapshots WHERE name=?", (name,)).fetchone()
        return json.loads(row[0]) if row else None

    def put_valuations(self, series_key, index_code, records):
        digest = hashlib.sha256(encode(records).encode()).hexdigest()
        with self.write() as db:
            db.execute("INSERT OR IGNORE INTO valuation_versions(series_key,index_code,imported_at,digest) VALUES (?,?,?,?)",
                       (series_key, index_code, datetime.now(TIMEZONE).isoformat(), digest))
            db.execute("UPDATE valuation_versions SET imported_at=? WHERE series_key=? AND index_code=? AND digest=?",
                       (datetime.now(TIMEZONE).isoformat(), series_key, index_code, digest))
            version = db.execute("SELECT id FROM valuation_versions WHERE series_key=? AND index_code=? AND digest=?",
                                 (series_key, index_code, digest)).fetchone()[0]
            db.executemany("INSERT OR IGNORE INTO index_valuations VALUES (?,?,?,?)",
                           [(version, row["date"], row["available_date"], row["pe_ttm"]) for row in records])

    def get_valuations(self, series_key, index_code):
        if not self.path.exists():
            return None
        with closing(self.connect()) as db:
            version = db.execute("SELECT id FROM valuation_versions WHERE series_key=? AND index_code=? ORDER BY imported_at DESC,id DESC LIMIT 1",
                                 (series_key, index_code)).fetchone()
            if version is None:
                return None
            return [{"date": row[0], "available_date": row[1], "index_code": index_code, "pe_ttm": row[2]}
                    for row in db.execute("SELECT trade_date,available_date,pe_ttm FROM index_valuations WHERE version_id=? ORDER BY trade_date", (version[0],))]
