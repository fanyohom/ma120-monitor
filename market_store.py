"""Durable market data. Complete adjusted-history versions are never mixed."""

import hashlib
import json
import math
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


TIMEZONE = ZoneInfo("Asia/Shanghai")
BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DATABASE = BASE_DIR / "data" / "market.sqlite3"


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


class MarketStore:
    def __init__(self, path=DEFAULT_DATABASE):
        self.path = Path(path)

    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript("""
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
        """)
        return db

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
        with closing(self.connect()) as db, db:
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
        with closing(self.connect()) as db, db:
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
        with closing(self.connect()) as db, db:
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
        with closing(self.connect()) as db, db:
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
