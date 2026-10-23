"""
MA120 持仓 & 关注池监控（Sina 数据源）

数据：
  - SQLite portfolio_positions：持仓、成本价、持仓数量
  - SQLite watchlist_entries：关注池股票代码和名称

策略：
  - 买入线：MA120 × 0.88
  - 卖出线：MA120 × 1.12

用法：
  python3 stock_dynamic_monitor.py              # 持仓 + 关注池
  python3 stock_dynamic_monitor.py --portfolio  # 仅持仓
  python3 stock_dynamic_monitor.py --watchlist  # 仅关注池
  python3 stock_dynamic_monitor.py --no-feishu  # 不发飞书
  python3 stock_dynamic_monitor.py --skip-non-trading-day
          # 非 A 股交易日直接跳过（法定节假日调休时不会空推卡片）
          # 注意：只在 09:30 之后调用判断才可靠，开盘前无法区分「未开盘」和「非交易日」
"""

import json
import logging
import os
import sys
import time
import unicodedata
import warnings
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from portfolio_store import PortfolioError, read_portfolio
from watchlist_store import read_watchlist
from market_store import MarketStore, database_for_cache

warnings.filterwarnings("ignore")

# ============ 策略参数 ============
MA_LONG = 120
BUY_THRESHOLD = 0.88
SELL_THRESHOLD = 1.12
DATA_DAYS = 200

# ============ 缓存 ============
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(BASE_DIR, ".stock_cache")
CACHE_DAYS = 1

STOCKS_FILE = os.path.join(BASE_DIR, "stocks.xlsx")
PORTFOLIO_JSON = os.path.join(BASE_DIR, "portfolio.json")
WATCHLIST_JSON = os.path.join(BASE_DIR, "watchlist.json")
PORTFOLIO_SHEET = "portfolio"
WATCHLIST_SHEET = "watchlist"

# ============ 飞书 ============
FEISHU_WEBHOOK_URL_ENV = "FEISHU_WEBHOOK_URL"
FEISHU_WEBHOOK_URL_TEMPLATE = "{{FEISHU_WEBHOOK_URL}}"
LOCAL_ENV_FILE = os.path.join(BASE_DIR, ".env.local")


# ---------- 数据文件加载 ----------
COLUMN_ALIASES = {
    "code": ["code", "股票代码", "代码"],
    "name": ["name", "股票名称", "名称"],
    "cost": ["cost", "成本价", "成本"],
    "shares": ["shares", "持仓数量", "数量"],
}


def _normalize_row(row: dict) -> dict:
    """把中文/英文列名统一成内部键名。"""
    out = {}
    for canonical, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in row and row[alias] not in (None, ""):
                out[canonical] = row[alias]
                break
        else:
            out[canonical] = row.get(canonical, "")
    return out


def _safe_float(value) -> float:
    if value in (None, ""):
        return 0.0
    try:
        return float(value)
    except Exception:
        return 0.0


def _safe_code(value) -> str:
    """Excel 可能把纯数字代码读成数值；港股使用 HKxxxxx 格式。"""
    if value is None:
        return ""
    text = str(value).strip().upper()
    if text.endswith(".0"):
        text = text[:-2]
    if text.startswith("HK"):
        digits = text[2:]
        return f"HK{digits.zfill(5)}" if digits.isdigit() else text
    if text.isdigit() and len(text) < 6:
        text = text.zfill(6)
    return text


def _is_hk_code(stock_code: str) -> bool:
    return stock_code.upper().startswith("HK")


def _sina_symbol(stock_code: str) -> str:
    return f"sh{stock_code}" if stock_code.startswith(("5", "6")) else f"sz{stock_code}"


def _tencent_symbol(stock_code: str) -> str:
    return f"hk{stock_code[2:]}"


def _read_xlsx_rows(path: str, sheet: str) -> list:
    """Read legacy workbook rows for one-time migration scripts only."""
    df = pd.read_excel(path, sheet_name=sheet, dtype=str, engine="openpyxl")
    df.columns = [str(c).strip() for c in df.columns]
    rows = []

    for _, record in df.iterrows():
        raw = {k: ("" if pd.isna(v) else str(v).strip()) for k, v in record.items()}
        if not any(raw.values()):
            continue
        normalized = _normalize_row(raw)
        if str(normalized.get("code", "")).lstrip().startswith("#"):
            continue
        normalized["code"] = _safe_code(normalized.get("code"))
        rows.append(normalized)
    return rows


def load_portfolio() -> list:
    if Path(PORTFOLIO_JSON) != Path(BASE_DIR) / "portfolio.json":
        return read_portfolio(PORTFOLIO_JSON)
    _load_strategy_settings()
    return read_portfolio()


def load_watchlist() -> list:
    if Path(WATCHLIST_JSON) != Path(BASE_DIR) / "watchlist.json":
        return read_watchlist(WATCHLIST_JSON)
    _load_strategy_settings()
    return read_watchlist()


def _load_strategy_settings():
    from config_store import ConfigurationStore
    global MA_LONG, BUY_THRESHOLD, SELL_THRESHOLD, DATA_DAYS
    settings = ConfigurationStore().summary()["ma120"]
    MA_LONG, BUY_THRESHOLD, SELL_THRESHOLD, DATA_DAYS = (
        settings["ma_days"], settings["buy_factor"], settings["sell_factor"], settings["history_days"])


# ---------- K 线获取 ----------
def _cache_path(code: str) -> str:
    os.makedirs(CACHE_DIR, exist_ok=True)
    return os.path.join(CACHE_DIR, f"sina_{code}.json")


def _is_cache_fresh(path: str) -> bool:
    modified_at = datetime.fromtimestamp(os.path.getmtime(path))
    if modified_at.date() != datetime.now().date():
        return False
    return time.time() - os.path.getmtime(path) <= CACHE_DAYS * 86400


def _market_store():
    return MarketStore(database_for_cache(CACHE_DIR))


def _history_frame(payload):
    rows = payload["bars"]
    frame = pd.DataFrame([row[:6] for row in rows], columns=["date", "open", "close", "high", "low", "volume"])
    frame["date"] = pd.to_datetime(frame["date"])
    frame = frame.set_index("date").sort_index()
    for column in ["open", "close", "high", "low", "volume"]:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame.attrs.update({"fetched_at": payload["fetched_at"], "read_source": "database"})
    return frame


def _read_cache(code: str, allow_stale: bool = False) -> pd.DataFrame | None:
    store = _market_store()
    payload = store.get_history("stock", code)
    if payload is not None:
        fresh = datetime.fromisoformat(payload["fetched_at"]).date() == datetime.now().date()
        if fresh or allow_stale:
            frame = _history_frame(payload)
            frame.attrs["data_stale"] = not fresh
            return frame
        return None
    path = _cache_path(code)
    if not os.path.exists(path):
        return None
    try:
        fresh = _is_cache_fresh(path)
        if not fresh and not allow_stale:
            return None
        with open(path, encoding="utf-8") as f:
            cached = json.load(f)
        df = pd.DataFrame(cached["data"])
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date").sort_index()
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        fetched_at = datetime.fromtimestamp(os.path.getmtime(path), ZoneInfo("Asia/Shanghai")).isoformat()
        _write_cache(code, df, fetched_at=fetched_at)
        df.attrs.update({"data_stale": not fresh, "fetched_at": fetched_at, "read_source": "database"})
        return df
    except Exception:
        return None


def _write_cache(code: str, df: pd.DataFrame, fetched_at=None) -> None:
    rows = [[stamp.isoformat(), float(row["open"]), float(row["close"]),
             float(row["high"]), float(row["low"]), float(row["volume"])]
            for stamp, row in df.iterrows()]
    source = "Tencent qfq" if _is_hk_code(code) else "Sina daily"
    _market_store().put_history("stock", code, {"bars": rows}, {"source": source}, fetched_at)


def _last_bar_date(df: pd.DataFrame):
    """取DataFrame 的最后一根日 K 日期，兼容 DatetimeIndex 与 RangeIndex。

    不能直接用 `df.index.max()`：回退数据可能来自不同代码路径，
    索引未必是日期类型（本地记忆里记录过 `get_stock_hist_data` 两条
    返回路径结构不一致：联网拉取的 df 已set_index("date")，
    而缓存反序列化出来的 date 只是普通列、index 是 RangeIndex）。
    直接比较 `index.max()` 会在 RangeIndex 上与 DatetimeIndex 比较失败，
    导致深历史请求被误判为「不新」而回退旧数据。
    """
    if df is None or df.empty:
        return None
    if isinstance(df.index, pd.DatetimeIndex):
        return pd.Timestamp(df.index.max())
    # 索引不是日期时，尝试从常见日期列取值；都没有则返回 None，
    # 表示「无法判断日期」，交由调用方按行数等其它依据决定。
    for column in ("date", "day", "trade_date"):
        if column in df.columns:
            try:
                parsed = pd.to_datetime(df[column], errors="coerce").max()
            except (TypeError, ValueError):
                continue
            if not pd.isna(parsed):
                return pd.Timestamp(parsed)
    return None


def _newer_than(df: pd.DataFrame, fallback: pd.DataFrame) -> bool:
    """判断新拉的日 K 是否比回退数据更值得采用。

    旧逻辑是 `len(df) < len(fallback)` 就回退，一旦库里存过 800 根深度历史，
    日常请求的 200 根恒被判为「更差」，日 K 从此冻结、MA120 持续落后。
    改为看最后一根日期：日期不落后即可用（行数只需够算 MA120）。

    `days` 由调用方按请求传入，短请求（如 days=2）不该因为不足 MA_LONG 被否。
    """
    if fallback is None or fallback.empty:
        return True
    if len(df) < MA_LONG and len(fallback) < MA_LONG:
        # 双方都不够 MA120，无法用均线判定；新数据有内容即可采用。
        return not df.empty
    if len(df) < MA_LONG:
        return False
    new_date = _last_bar_date(df)
    old_date = _last_bar_date(fallback)
    if new_date is None or old_date is None:
        # 日期不可判定时，回退到「行数不比旧数据少即视为更新」的保守策略。
        return len(df) >= len(fallback)
    return new_date >= old_date


def _merge_bars(fallback: pd.DataFrame, df: pd.DataFrame) -> pd.DataFrame:
    """新数据覆盖旧数据同一日期，同时保留旧数据更长的历史深度。"""
    if fallback is None or fallback.empty or df is None or df.empty:
        return df
    if not set(df.columns).issubset(set(fallback.columns)):
        return df
    try:
        merged = pd.concat([fallback, df])
        if isinstance(merged.index, pd.DatetimeIndex):
            merged = merged[~merged.index.duplicated(keep="last")].sort_index()
        else:
            # 索引无法用于去重时，保留新数据原样，避免产出错误的行数。
            return df
    except Exception:
        return df
    return merged


def _store_history(stock_code: str, df: pd.DataFrame) -> None:
    """把日 K 写入本地库。写失败不应影响本次返回值。

    历史教训（2026-10-22）：写库失败曾被外层宽 except 吞掉，
    导致「联网拉到了新数据」却被当成「拉取失败」，
    静默回退到旧行情 —— 页面显示正常，实际用的是过期数据，
    当日买入/卖出信号被无声清空，且日志里没有任何异常。

    写库只是持久化，失败时保留内存中的新数据继续用，
    同时记一条 warning 让问题可见。
    """
    try:
        _write_cache(stock_code, df)
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).warning(
            "日K 写库失败（%s）：%s: %s，本次仍使用已拉取的数据", stock_code,
            type(exc).__name__, exc)


def get_stock_hist_data(stock_code: str, days: int | None = None, force: bool = False,
                        database_only: bool = False) -> pd.DataFrame:
    """获取前复权日 K：A 股使用新浪，港股使用腾讯。"""
    days = DATA_DAYS if days is None else days
    cached = _read_cache(stock_code, allow_stale=True) if database_only else _read_cache(stock_code)
    # 注意：database_only 只表示「不联网」，**不代表数据陈旧**。
    # 是否陈旧由 _read_cache() 按 fetched_at 是否当天判定（它会设置
    # data_stale）。
    #
    # 这里曾有 `if database_only and cached is not None: cached.attrs["data_stale"] = True`
    # 的无条件覆盖，后果很隐蔽：只要服务重启，即使库里的日 K 就是当天
    # 11:30 刚抓的，整表也被判成「数据库历史行情」，当日买卖信号被全部清空，
    # 用户必须手动点刷新才恢复。2026-10-23 修复。
    required_rows = MA_LONG if days <= DATA_DAYS else days
    if cached is not None and len(cached) >= required_rows and not force:
        return cached
    fallback = cached if cached is not None else _read_cache(stock_code, allow_stale=True)
    fallback = fallback if fallback is not None and len(fallback) >= MA_LONG else pd.DataFrame()
    if database_only:
        if len(fallback) < days:
            deeper = _market_store().get_history("stock", stock_code, min_rows=days)
            if deeper:
                fallback = _history_frame(deeper)
                fallback.attrs["data_stale"] = True
        return fallback
    if not fallback.empty:
        fallback.attrs["data_stale"] = True

    try:
        if _is_hk_code(stock_code):
            symbol = _tencent_symbol(stock_code)
            url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
            params = {"param": f"{symbol},day,,,{days},qfq"}
            response = requests.get(url, params=params, timeout=15)
            response.raise_for_status()
            payload = response.json()
            market_data = payload.get("data", {}).get(symbol, {})
            data = market_data.get("qfqday") or market_data.get("day") or []
            if not data:
                return fallback

            df = pd.DataFrame(
                [row[:6] for row in data],
                columns=["date", "open", "close", "high", "low", "volume"],
            )
            df["date"] = pd.to_datetime(df["date"])
            df = df.set_index("date").sort_index()
            for col in ["open", "high", "low", "close", "volume"]:
                df[col] = pd.to_numeric(df[col], errors="coerce")
            if not _newer_than(df, fallback):
                return fallback
            df = _merge_bars(fallback, df)
            _store_history(stock_code, df)
            return df

        symbol = _sina_symbol(stock_code)
        url = "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
        params = {"symbol": symbol, "scale": 240, "ma": "no", "datalen": days}
        response = requests.get(url, params=params, timeout=15)
        data = response.json()
        if not data:
            return fallback
        df = pd.DataFrame(data)
        df = df.rename(columns={"day": "date", "vol": "volume"})
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date").sort_index()
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        if not _newer_than(df, fallback):
            return fallback
        df = _merge_bars(fallback, df)
        _store_history(stock_code, df)
        return df
    except Exception as exc:
        # 只有「联网拉取本身」失败才回退旧数据。
        # 写库失败已由 _store_history() 单独兜住，不会走到这里。
        logging.getLogger(__name__).warning(
            "日K 拉取失败（%s）：%s: %s，回退到本地缓存", stock_code,
            type(exc).__name__, exc)
        if not fallback.empty:
            fallback.attrs["data_stale"] = True
        return fallback


# ---------- MA120 信号 ----------
def _fetch_stock_realtime_quote(stock_code: str) -> dict:
    """获取实时行情；失败时返回空字典，由调用方回退至日线收盘价。"""
    try:
        if _is_hk_code(stock_code):
            symbol = _tencent_symbol(stock_code)
            response = requests.get(f"https://qt.gtimg.cn/q={symbol}", timeout=10)
            response.raise_for_status()
            response.encoding = "gbk"
            text = response.text.strip()
            if '="' not in text:
                return {}

            raw = text.split('="', 1)[1].rsplit('";', 1)[0]
            fields = raw.split("~")
            if len(fields) < 31 or not fields[1]:
                return {}

            price = _safe_float(fields[3])
            if price <= 0:
                return {}

            quote_at = fields[30].split()
            return {
                "price": price,
                "quote_date": quote_at[0].replace("/", "-") if quote_at else "",
                "quote_time": quote_at[1] if len(quote_at) > 1 else "",
                "price_source": "realtime",
            }

        symbol = _sina_symbol(stock_code)
        url = f"https://hq.sinajs.cn/list={symbol}"
        response = requests.get(
            url,
            headers={"Referer": "https://finance.sina.com.cn"},
            timeout=10,
        )
        response.encoding = response.apparent_encoding or "gbk"
        text = response.text.strip()
        if '="' not in text:
            return {}

        raw = text.split('="', 1)[1].rsplit('";', 1)[0]
        fields = raw.split(",")
        if len(fields) < 32 or not fields[0]:
            return {}

        price = _safe_float(fields[3])
        if price <= 0:
            return {}

        return {
            "price": price,
            "quote_date": fields[30],
            "quote_time": fields[31],
            "price_source": "realtime",
        }
    except Exception:
        return {}


def get_stock_realtime_quote(stock_code: str, database_only: bool = False) -> dict:
    store = _market_store()
    if not database_only:
        quote = _fetch_stock_realtime_quote(stock_code)
        if quote:
            store.put_quote("stock", stock_code, quote)
            return quote
    saved = store.get_quote("stock", stock_code)
    if saved:
        return {**saved, "price_source": "database", "data_stale": True}
    return {}


def check_ma120_signal(df: pd.DataFrame, current_price: float | None = None) -> dict | None:
    if len(df) < MA_LONG:
        return None

    df = df.copy()
    df["ma120"] = df["close"].rolling(window=MA_LONG).mean()
    latest = df.iloc[-1]
    prev = df.iloc[-2] if len(df) > 1 else latest

    price = current_price if current_price and current_price > 0 else latest["close"]
    ma120 = latest["ma120"]
    if pd.isna(ma120) or ma120 == 0:
        return None

    ma120_pct = (price - ma120) / ma120 * 100
    prev_price = prev["close"]
    prev_ma120 = prev["ma120"] if not pd.isna(prev["ma120"]) else ma120

    buy_line = round(ma120 * BUY_THRESHOLD, 2)
    sell_line = round(ma120 * SELL_THRESHOLD, 2)

    result = {
        "price": price,
        "ma120": ma120,
        "ma120_pct": ma120_pct,
        "signal": "hold",
        "buy_line": buy_line,
        "sell_line": sell_line,
        "message": "",
    }

    if price < ma120 * BUY_THRESHOLD and prev_price >= prev_ma120 * BUY_THRESHOLD:
        result["signal"] = "buy"
        result["message"] = (
            f"🔥 买入：{price:.2f} < 买入线 {buy_line:.2f}  "
            f"MA120 {ma120:.2f}  偏离MA120 {ma120_pct:.2f}%"
        )
    elif price > ma120 * SELL_THRESHOLD and prev_price <= prev_ma120 * SELL_THRESHOLD:
        result["signal"] = "sell"
        result["message"] = (
            f"📤 卖出：{price:.2f} > 卖出线 {sell_line:.2f}  "
            f"MA120 {ma120:.2f}  偏离MA120 {ma120_pct:.2f}%"
        )
    elif price >= ma120:
        result["message"] = f"📈 高于 MA120 {ma120:.2f}  {ma120_pct:+.2f}%  卖出线 {sell_line:.2f}"
    else:
        result["message"] = f"📊 低于 MA120 {ma120:.2f}  {ma120_pct:+.2f}%  买入线 {buy_line:.2f}"
    return result


def enrich(item: dict) -> dict | None:
    """补充 K 线和 MA120 信号。"""
    df = get_stock_hist_data(item["code"])
    if df.empty or len(df) < MA_LONG:
        return None
    quote = get_stock_realtime_quote(item["code"])
    signal = check_ma120_signal(df, current_price=quote.get("price"))
    if not signal:
        return None
    if df.attrs.get("data_stale") or quote.get("data_stale"):
        signal["signal"] = "hold"
    return {**item, **signal, **quote}


# ---------- 飞书发送 ----------
def _read_local_env_value(key: str) -> str:
    """Read a KEY=value pair from .env.local without adding a dotenv dependency."""
    if not os.path.exists(LOCAL_ENV_FILE):
        return ""

    try:
        with open(LOCAL_ENV_FILE, encoding="utf-8-sig") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                name, value = line.split("=", 1)
                if name.strip() != key:
                    continue
                return value.strip().strip('"').strip("'")
    except Exception as exc:
        print(f"     ⚠️ 读取 .env.local 失败: {exc}")
    return ""


def _get_feishu_webhook_url() -> str:
    webhook_url = os.environ.get(FEISHU_WEBHOOK_URL_ENV, "").strip()
    if webhook_url:
        return webhook_url

    webhook_url = _read_local_env_value(FEISHU_WEBHOOK_URL_ENV)
    if webhook_url:
        return webhook_url

    template = FEISHU_WEBHOOK_URL_TEMPLATE.strip()
    if template and template not in {"{{FEISHU_WEBHOOK_URL}}", "${FEISHU_WEBHOOK_URL}"}:
        return template
    return ""


def _feishu_payload(message) -> dict:
    if isinstance(message, dict):
        return message
    return {"msg_type": "text", "content": {"text": str(message)}}


def send_feishu(message) -> bool:
    webhook_url = _get_feishu_webhook_url()
    if not webhook_url:
        print(f"     ⚠️ 未配置 {FEISHU_WEBHOOK_URL_ENV}，跳过飞书推送")
        return False

    try:
        response = requests.post(
            webhook_url,
            json=_feishu_payload(message),
            timeout=15,
        )
        if response.status_code != 200:
            print(f"     HTTP {response.status_code}: {response.text[:200]}")
            return False

        try:
            data = response.json()
        except ValueError:
            return True

        code = data.get("code", data.get("StatusCode", 0))
        if code not in (0, "0"):
            print(f"     飞书返回异常: {str(data)[:200]}")
            return False
        return True
    except Exception as exc:
        print(f"     ⚠️ 飞书发送异常: {exc}")
        return False


# ---------- 文本渲染 ----------
def _signal_tag(r: dict) -> str:
    if r["signal"] == "buy":
        return "🔥 买入"
    if r["signal"] == "sell":
        return "📤 卖出"
    if r["price"] >= r["ma120"]:
        return "📈 高于 MA120"
    return "📊 低于 MA120"


def _sort_items(items: list) -> list:
    def sort_key(r):
        prio = _card_status_priority(r)
        pct_sort = -r["ma120_pct"] if prio in (0, 3) else r["ma120_pct"]
        return (prio, pct_sort, r["code"])

    return sorted(items, key=sort_key)


def _position_label(r: dict) -> str:
    if r["signal"] == "buy":
        return "买入信号"
    if r["signal"] == "sell":
        return "卖出信号"
    return "高于MA120" if r["ma120_pct"] >= 0 else "低于MA120"


def _buy_line_pct(r: dict) -> float:
    """现价相对**买入线**的距离（%），负数表示已跌破买入线。

    注意与 `ma120_pct` 区分：后者的基准是 MA120。买入线 = MA120 × 0.88，
    所以现价在买入线下方时，到 MA120 的距离必然比到买入线的距离大得多。
    """
    return (r["price"] - r["buy_line"]) / r["buy_line"] * 100


def _display_width(text: str) -> int:
    """终端显示宽度：中文等全角字符占 2 格，ASCII 占 1 格。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in str(text))


def _pad(text: str, width: int) -> str:
    """按显示宽度右侧补空格，保证中文列也能对齐。"""
    text = str(text)
    return text + " " * max(0, width - _display_width(text))


def _print_console_table(title: str, items: list) -> None:
    print(f"\n{title} {len(items)} 支")
    if not items:
        print("  无数据")
        return

    headers = ["股票", "代码", "现价", "MA120", "偏离MA120", "买入线", "距买入线", "卖出线", "状态"]
    rows = []
    for item in _sort_items(items):
        rows.append(
            [
                item["name"],
                item["code"],
                f"{item['price']:.2f}",
                f"{item['ma120']:.2f}",
                f"{item['ma120_pct']:+.2f}%",
                f"{item['buy_line']:.2f}",
                f"{_buy_line_pct(item):+.2f}%",
                f"{item['sell_line']:.2f}",
                _position_label(item),
            ]
        )

    widths = [
        max(_display_width(headers[i]), max(_display_width(row[i]) for row in rows))
        for i in range(len(headers))
    ]

    def fmt(row):
        return "| " + " | ".join(_pad(cell, widths[i]) for i, cell in enumerate(row)) + " |"

    print(fmt(headers))
    print("|-" + "-|-".join("-" * width for width in widths) + "-|")
    for row in rows:
        print(fmt(row))


def render_portfolio_text(items: list, today: str) -> str:
    """持仓报告：优先展示信号股票，再按偏离排序。"""
    if not items:
        return f"📦 持仓监控 · {today}\n\n无数据"

    items = _sort_items(items)
    lines = [f"📦 持仓监控 · {today}", ""]
    total_cost = total_value = 0.0
    signal_count = {"buy": 0, "sell": 0}

    for item in items:
        tag = _signal_tag(item)
        if item["signal"] in signal_count:
            signal_count[item["signal"]] += 1
        line = (
            f"{tag}  {item['name']} ({item['code']})  现价 {item['price']:.2f}  "
            f"MA120 {item['ma120']:.2f}  偏离MA120 {item['ma120_pct']:+.2f}%"
        )
        if item.get("cost") and item.get("shares"):
            pnl_pct = (item["price"] - item["cost"]) / item["cost"] * 100
            value = item["price"] * item["shares"]
            cost_amt = item["cost"] * item["shares"]
            total_cost += cost_amt
            total_value += value
            line += f"  |  成本 {item['cost']:.2f}  浮盈 {pnl_pct:+.2f}%"
        if item["signal"] == "buy":
            line += f"  → 买入线 {item['buy_line']:.2f}"
        elif item["signal"] == "sell":
            line += f"  → 卖出线 {item['sell_line']:.2f}"
        else:
            line += f"  → 买 {item['buy_line']:.2f} / 卖 {item['sell_line']:.2f}"
        lines.append(line)

    summary = []
    if signal_count["buy"] or signal_count["sell"]:
        summary.append(f"今日信号：🔥买入 {signal_count['buy']} · 📤卖出 {signal_count['sell']}")
    if total_cost > 0:
        pnl = total_value - total_cost
        pnl_pct = pnl / total_cost * 100
        summary.append(f"💰 持仓市值 ¥{total_value:,.0f}  浮盈 ¥{pnl:+,.0f} ({pnl_pct:+.2f}%)")
    if summary:
        lines.append("")
        lines.extend(summary)
    return "\n".join(lines)


def render_watchlist_text(items: list, today: str) -> str:
    if not items:
        return f"👀 关注池 · {today}\n\n无数据"

    items = _sort_items(items)
    lines = [f"👀 关注池 · {today}", ""]
    signal_count = {"buy": 0, "sell": 0}
    for item in items:
        tag = _signal_tag(item)
        if item["signal"] in signal_count:
            signal_count[item["signal"]] += 1
        line = (
            f"{tag}  {item['name']} ({item['code']})  现价 {item['price']:.2f}  "
            f"MA120 {item['ma120']:.2f}  偏离MA120 {item['ma120_pct']:+.2f}%"
        )
        if item["signal"] == "buy":
            line += f"  → 买入线 {item['buy_line']:.2f}"
        elif item["signal"] == "sell":
            line += f"  → 卖出线 {item['sell_line']:.2f}"
        else:
            line += f"  → 买 {item['buy_line']:.2f} / 卖 {item['sell_line']:.2f}"
        lines.append(line)
    if signal_count["buy"] or signal_count["sell"]:
        lines.append("")
        lines.append(f"今日信号：🔥买入 {signal_count['buy']} · 📤卖出 {signal_count['sell']}")
    return "\n".join(lines)


def _card_header_template(items: list) -> str:
    if any(item["signal"] == "sell" or item["price"] > item["sell_line"] for item in items):
        return "red"
    if any(item["signal"] == "buy" or item["price"] < item["buy_line"] for item in items):
        return "green"
    return "blue"


def _card_status(item: dict) -> str:
    if item["signal"] == "sell":
        return "卖出信号"
    if item["price"] > item["sell_line"]:
        return "高于卖出线"
    if item["signal"] == "buy":
        return "买入信号"
    if item["price"] < item["buy_line"]:
        return "低于买入线"
    return f"高于MA{MA_LONG}" if item["price"] >= item["ma120"] else f"低于MA{MA_LONG}"


def _card_status_priority(item: dict) -> int:
    if item["signal"] == "sell" or item["price"] > item["sell_line"]:
        return 0
    if item["signal"] == "buy" or item["price"] < item["buy_line"]:
        return 1
    if item["price"] < item["ma120"]:
        return 2
    return 3


def _escape_table_cell(value) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _card_table_row(item: dict, include_portfolio: bool = False) -> dict:
    row = {
        "status": _card_status(item),
        "stock": f"{_escape_table_cell(item['name'])} {item['code']}",
        "price": f"{item['price']:.2f}",
        "ma120": f"{item['ma120']:.2f}",
        "diff": f"{item['ma120_pct']:+.2f}%",
        "buy_line": f"{item['buy_line']:.2f}",
        "buy_diff": f"{_buy_line_pct(item):+.2f}%",
        "sell_line": f"{item['sell_line']:.2f}",
    }
    if include_portfolio:
        if item.get("cost") and item.get("shares"):
            pnl_pct = (item["price"] - item["cost"]) / item["cost"] * 100
            row.update({"cost": f"{item['cost']:.2f}", "pnl": f"{pnl_pct:+.2f}%"})
        else:
            row.update({"cost": "-", "pnl": "-"})
    return row


def _card_table_columns(include_portfolio: bool = False) -> list:
    columns = [
        ("status", "状态"),
        ("stock", "股票"),
        ("price", "现价"),
        ("ma120", "MA120"),
        ("diff", "偏离MA120"),
        ("buy_line", "买入线"),
        ("buy_diff", "距买入线"),
        ("sell_line", "卖出线"),
    ]
    if include_portfolio:
        columns.extend([("cost", "成本"), ("pnl", "浮盈")])

    return [
        {
            "name": name,
            "display_name": display_name,
            "data_type": "text",
            "horizontal_align": "left",
            "width": "auto",
        }
        for name, display_name in columns
    ]


def _card_table_component(items: list, include_portfolio: bool = False) -> dict:
    return {
        "tag": "table",
        "page_size": min(max(len(items), 1), 10),
        "row_height": "low",
        "header_style": {
            "background_style": "grey",
            "bold": True,
        },
        "columns": _card_table_columns(include_portfolio=include_portfolio),
        "rows": [
            _card_table_row(item, include_portfolio=include_portfolio)
            for item in _sort_items(items)
        ],
    }


def _signal_summary(items: list) -> str:
    buy_count = sum(1 for item in items if item["signal"] == "buy")
    sell_count = sum(1 for item in items if item["signal"] == "sell")
    buy_zone_count = sum(1 for item in items if item["price"] < item["buy_line"])
    sell_zone_count = sum(1 for item in items if item["price"] > item["sell_line"])
    return (
        f"今日信号：买入 {buy_count} / 卖出 {sell_count}\n"
        f"区间状态：低于买入线 {buy_zone_count} / 高于卖出线 {sell_zone_count}"
    )


def _triggered_signal_summary(items: list) -> str:
    alert_items = [
        item
        for item in _sort_items(items)
        if item["signal"] in {"buy", "sell"}
        or item["price"] < item["buy_line"]
        or item["price"] > item["sell_line"]
    ]
    if not alert_items:
        return ""

    lines = ["**重点提醒**"]
    for item in alert_items:
        if item["signal"] == "sell":
            label = "卖出信号"
            comparator = ">"
            line_name = "卖出线"
            line_value = item["sell_line"]
        elif item["price"] > item["sell_line"]:
            label = "高于卖出线"
            comparator = ">"
            line_name = "卖出线"
            line_value = item["sell_line"]
        elif item["signal"] == "buy":
            label = "买入信号"
            comparator = "<"
            line_name = "买入线"
            line_value = item["buy_line"]
        else:
            label = "低于买入线"
            comparator = "<"
            line_name = "买入线"
            line_value = item["buy_line"]

        lines.append(
            f"{label}：{item['name']} {item['code']} | "
            f"现价 {item['price']:.2f} {comparator} {line_name} {line_value:.2f} | "
            f"MA120 {item['ma120']:.2f} | 偏离MA120 {item['ma120_pct']:+.2f}% | "
            f"距买入线 {_buy_line_pct(item):+.2f}%"
        )
    return "\n".join(lines)


def _portfolio_summary(items: list) -> str:
    total_cost = 0.0
    total_value = 0.0
    for item in items:
        if item.get("cost") and item.get("shares"):
            total_cost += item["cost"] * item["shares"]
            total_value += item["price"] * item["shares"]
    if total_cost <= 0:
        return _signal_summary(items)

    pnl = total_value - total_cost
    pnl_pct = pnl / total_cost * 100
    return (
        f"{_signal_summary(items)}\n"
        f"持仓市值 ¥{total_value:,.0f} | 浮盈 ¥{pnl:+,.0f} ({pnl_pct:+.2f}%)"
    )


def _render_card(title: str, items: list, today: str, summary: str, include_portfolio: bool = False) -> dict:
    detail_element = (
        _card_table_component(items, include_portfolio=include_portfolio)
        if items
        else {"tag": "markdown", "content": "无数据"}
    )
    elements = [{"tag": "markdown", "content": summary}]
    signal_summary = _triggered_signal_summary(items)
    if signal_summary:
        elements.extend(
            [
                {"tag": "hr"},
                {"tag": "markdown", "content": signal_summary},
            ]
        )
    elements.extend(
        [
            {"tag": "hr"},
            detail_element,
        ]
    )

    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": _card_header_template(items),
                "title": {"tag": "plain_text", "content": f"{title} · {today}"},
            },
            "elements": elements,
        },
    }


def render_portfolio_card(items: list, today: str) -> dict:
    return _render_card(
        "持仓监控",
        items,
        today,
        _portfolio_summary(items),
        include_portfolio=True,
    )


def render_watchlist_card(items: list, today: str) -> dict:
    return _render_card(
        "关注池",
        items,
        today,
        _signal_summary(items),
        include_portfolio=False,
    )


# ---------- 交易日判断 ----------
TRADING_DAY_PROBES = ("600015", "601318", "000651")


def is_trading_day() -> bool:
    """用几支流动性好的大盘股探测今天是不是 A 股交易日。

    只在 09:30 之后调用才可靠：交易日内这个时点实时行情必然取得到
    （午休时段返回的是上午收盘价）；开盘前取不到价，无法区分
    「还没开盘」和「非交易日」。
    """
    today = datetime.now().strftime("%Y-%m-%d")
    for code in TRADING_DAY_PROBES:
        quote = get_stock_realtime_quote(code)
        if quote.get("price_source") == "realtime" and quote.get("quote_date") == today:
            return True
    return False


# ---------- 主流程 ----------
def run(mode: str = "all", feishu: bool = True, skip_non_trading_day: bool = False):
    today = datetime.now().strftime("%Y-%m-%d")
    print(f"\n{'=' * 60}\n📊 MA120 监控 · {today}  mode={mode}\n{'=' * 60}")

    if skip_non_trading_day and not is_trading_day():
        print("\n⏭️  今天不是 A 股交易日（探测不到当日实时行情），已跳过监控与飞书推送。")
        return {"portfolio": [], "watchlist": []}

    portfolio_items = []
    watchlist_items = []
    portfolio_skipped = []
    watchlist_skipped = []

    if mode in ("all", "portfolio"):
        portfolio = load_portfolio()
        for item in portfolio:
            enriched = enrich(item)
            if enriched is None:
                portfolio_skipped.append(item)
                continue
            portfolio_items.append(enriched)
            time.sleep(0.2)
        _print_console_table("📦 持仓", portfolio_items)
        for item in portfolio_skipped:
            print(f"  ⚠️ 数据不足: {item['name']} ({item['code']})")

    if mode in ("all", "watchlist"):
        watchlist = load_watchlist()
        if mode == "all":
            portfolio_codes = {item["code"] for item in portfolio_items}
            watchlist = [item for item in watchlist if item["code"] not in portfolio_codes]
        for item in watchlist:
            enriched = enrich(item)
            if enriched is None:
                watchlist_skipped.append(item)
                continue
            watchlist_items.append(enriched)
            time.sleep(0.2)
        _print_console_table("👀 关注池", watchlist_items)
        for item in watchlist_skipped:
            print(f"  ⚠️ 数据不足: {item['name']} ({item['code']})")

    if feishu and (portfolio_items or watchlist_items):
        print("\n📨 发送飞书...")
        if portfolio_items:
            ok = send_feishu(render_portfolio_card(portfolio_items, today))
            print(f"  持仓: {'✅' if ok else '⚠️ 失败'}")
        if watchlist_items:
            ok = send_feishu(render_watchlist_card(watchlist_items, today))
            print(f"  关注池: {'✅' if ok else '⚠️ 失败'}")

    print(f"\n{'=' * 60}\n✅ 完成\n{'=' * 60}\n")
    return {"portfolio": portfolio_items, "watchlist": watchlist_items}


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--etf" in args:
        from etf_dca import main as etf_main

        etf_args = [arg for arg in args if arg not in ("--etf", "--no-feishu")]
        if "--no-feishu" not in args and "--feishu" not in etf_args:
            etf_args.append("--feishu")
        sys.exit(etf_main(etf_args))
    mode = "all"
    if "--portfolio" in args:
        mode = "portfolio"
    elif "--watchlist" in args:
        mode = "watchlist"
    feishu = "--no-feishu" not in args
    skip_non_trading_day = "--skip-non-trading-day" in args
    try:
        run(mode=mode, feishu=feishu, skip_non_trading_day=skip_non_trading_day)
    except PortfolioError as exc:
        print(f"持仓读取失败：{exc}", file=sys.stderr)
        sys.exit(1)
