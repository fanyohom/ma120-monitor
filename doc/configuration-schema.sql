-- Configuration tables share data/market.sqlite3 with durable market data.
-- PRAGMA foreign_keys=ON is required on every connection.
CREATE TABLE IF NOT EXISTS instruments (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL CHECK(length(trim(name)) > 0),
    kind TEXT NOT NULL CHECK(kind IN ('stock','etf')),
    currency TEXT NOT NULL CHECK(currency IN ('CNY','HKD')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS watchlist_entries (
    code TEXT PRIMARY KEY REFERENCES instruments(code) ON DELETE RESTRICT,
    sort_order INTEGER NOT NULL CHECK(sort_order >= 0),
    -- 特殊关注：1=已标记。仅关注池使用，持仓表不加。
    starred INTEGER NOT NULL DEFAULT 0 CHECK(starred IN (0,1)),
    -- 标记理由，让日后的自己知道当初为什么盯它。
    note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS portfolio_positions (
    code TEXT PRIMARY KEY REFERENCES instruments(code) ON DELETE RESTRICT,
    cost REAL NOT NULL CHECK(cost >= 0),
    shares REAL NOT NULL CHECK(shares >= 0),
    sort_order INTEGER NOT NULL CHECK(sort_order >= 0)
);
CREATE TABLE IF NOT EXISTS tracked_indices (
    index_code TEXT PRIMARY KEY CHECK(length(trim(index_code)) > 0)
);
CREATE TABLE IF NOT EXISTS etf_plans (
    id TEXT PRIMARY KEY CHECK(length(trim(id)) > 0),
    code TEXT NOT NULL UNIQUE REFERENCES instruments(code) ON DELETE RESTRICT,
    index_code TEXT NOT NULL REFERENCES tracked_indices(index_code),
    strategy TEXT NOT NULL CHECK(strategy IN ('ma','valuation','drawdown')),
    base_amount_cents INTEGER NOT NULL CHECK(base_amount_cents > 0),
    max_amount_cents INTEGER NOT NULL CHECK(max_amount_cents > 0),
    frequency TEXT NOT NULL CHECK(frequency IN ('daily','weekly','monthly')),
    weekday INTEGER CHECK(weekday BETWEEN 0 AND 4),
    monthday INTEGER CHECK(monthday BETWEEN 1 AND 28),
    initial_cost REAL NOT NULL CHECK(initial_cost >= 0),
    initial_shares INTEGER NOT NULL CHECK(initial_shares >= 0),
    valuation_source TEXT NOT NULL,
    strategy_reason TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
    sort_order INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK(initial_shares = 0 OR initial_cost > 0),
    CHECK((frequency = 'daily' AND weekday IS NULL AND monthday IS NULL)
       OR (frequency = 'weekly' AND weekday IS NOT NULL AND monthday IS NULL)
       OR (frequency = 'monthly' AND weekday IS NULL AND monthday IS NOT NULL))
);
CREATE TABLE IF NOT EXISTS dca_settings (
    id INTEGER PRIMARY KEY CHECK(id = 1),
    ma_days INTEGER NOT NULL CHECK(ma_days BETWEEN 1 AND 600),
    lot_size INTEGER NOT NULL CHECK(lot_size > 0),
    above_multiplier REAL NOT NULL CHECK(above_multiplier >= 0)
);
CREATE TABLE IF NOT EXISTS dca_amount_bands (
    sort_order INTEGER PRIMARY KEY CHECK(sort_order >= 0),
    below REAL NOT NULL UNIQUE CHECK(below >= -100),
    multiplier REAL NOT NULL CHECK(multiplier >= 0)
);
CREATE TABLE IF NOT EXISTS valuation_policy (
    id INTEGER PRIMARY KEY CHECK(id = 1),
    lookback_years INTEGER NOT NULL CHECK(lookback_years > 0),
    min_samples INTEGER NOT NULL CHECK(min_samples > 0),
    min_span_days INTEGER NOT NULL CHECK(min_span_days > 0),
    max_age_days INTEGER NOT NULL CHECK(max_age_days > 0),
    buy_below REAL NOT NULL CHECK(buy_below > 0),
    high_at REAL NOT NULL CHECK(high_at <= 100 AND high_at > buy_below)
);
CREATE TABLE IF NOT EXISTS ma120_settings (
    id INTEGER PRIMARY KEY CHECK(id = 1),
    ma_days INTEGER NOT NULL CHECK(ma_days BETWEEN 2 AND 600),
    buy_factor REAL NOT NULL CHECK(buy_factor > 0 AND buy_factor < 1),
    sell_factor REAL NOT NULL CHECK(sell_factor > 1),
    history_days INTEGER NOT NULL CHECK(history_days >= ma_days)
);
CREATE TABLE IF NOT EXISTS configuration_meta (
    key TEXT PRIMARY KEY, value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS configuration_imports (
    source TEXT PRIMARY KEY, digest TEXT NOT NULL, imported_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS configuration_changes (
    id INTEGER PRIMARY KEY, changed_at TEXT NOT NULL,
    operation TEXT NOT NULL, snapshot_json TEXT NOT NULL
);
