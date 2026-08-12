"""
MA120 持仓 & 关注池监控（Sina 数据源）

数据：从 stocks.xlsx 读取（两个 sheet）
  - sheet "portfolio": 持仓 股票代码/股票名称/成本价/持仓数量 → 浮盈浮亏 + MA120 信号
  - sheet "watchlist": 关注池 股票代码/股票名称                  → 仅 MA120 信号

策略：
  - 买入线：MA120 × 0.88
  - 卖出线：MA120 × 1.12

用法：
  python3 stock_dynamic_monitor.py              # 持仓 + 关注池
  python3 stock_dynamic_monitor.py --portfolio  # 仅持仓
  python3 stock_dynamic_monitor.py --watchlist  # 仅关注池
  python3 stock_dynamic_monitor.py --no-feishu  # 不发飞书
"""

import json
import os
import sys
import time
import warnings
from datetime import datetime

import pandas as pd
import requests

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
    return f"sh{stock_code}" if stock_code.startswith("6") else f"sz{stock_code}"


def _tencent_symbol(stock_code: str) -> str:
    return f"hk{stock_code[2:]}"


def _read_xlsx_rows(path: str, sheet: str) -> list:
    """读取 xlsx 指定 sheet，跳过空行和 # 注释行。"""
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
    if not os.path.exists(STOCKS_FILE):
        print(f"⚠️ 未找到数据文件: {STOCKS_FILE}")
        return []
    try:
        rows = _read_xlsx_rows(STOCKS_FILE, PORTFOLIO_SHEET)
        out = []
        for item in rows:
            if not item.get("code") or not item.get("name"):
                continue
            out.append(
                {
                    "code": item["code"],
                    "name": item["name"],
                    "cost": _safe_float(item.get("cost")),
                    "shares": _safe_float(item.get("shares")),
                }
            )
        return out
    except Exception as exc:
        print(f"⚠️ 读取 {os.path.basename(STOCKS_FILE)}[{PORTFOLIO_SHEET}] 失败: {exc}")
        return []


def load_watchlist() -> list:
    if not os.path.exists(STOCKS_FILE):
        return []
    try:
        rows = _read_xlsx_rows(STOCKS_FILE, WATCHLIST_SHEET)
        out = []
        for item in rows:
            if not item.get("code") or not item.get("name"):
                continue
            out.append({"code": item["code"], "name": item["name"]})
        return out
    except Exception as exc:
        print(f"⚠️ 读取 {os.path.basename(STOCKS_FILE)}[{WATCHLIST_SHEET}] 失败: {exc}")
        return []


# ---------- K 线获取 ----------
def _cache_path(code: str) -> str:
    os.makedirs(CACHE_DIR, exist_ok=True)
    return os.path.join(CACHE_DIR, f"sina_{code}.json")


def _is_cache_fresh(path: str) -> bool:
    modified_at = datetime.fromtimestamp(os.path.getmtime(path))
    if modified_at.date() != datetime.now().date():
        return False
    return time.time() - os.path.getmtime(path) <= CACHE_DAYS * 86400


def _read_cache(code: str) -> pd.DataFrame | None:
    path = _cache_path(code)
    if not os.path.exists(path):
        return None
    try:
        if not _is_cache_fresh(path):
            return None
        with open(path, encoding="utf-8") as f:
            cached = json.load(f)
        df = pd.DataFrame(cached["data"])
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date").sort_index()
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        return df
    except Exception:
        return None


def _write_cache(code: str, df: pd.DataFrame) -> None:
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        data = df.reset_index().to_dict("records")
        for item in data:
            item["date"] = item["date"].isoformat()
        with open(_cache_path(code), "w", encoding="utf-8") as f:
            json.dump({"data": data}, f, ensure_ascii=False)
    except Exception:
        pass


def get_stock_hist_data(stock_code: str, days: int = DATA_DAYS) -> pd.DataFrame:
    """获取前复权日 K：A 股使用新浪，港股使用腾讯。"""
    cached = _read_cache(stock_code)
    if cached is not None and len(cached) >= MA_LONG:
        return cached

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
                return pd.DataFrame()

            df = pd.DataFrame(
                [row[:6] for row in data],
                columns=["date", "open", "close", "high", "low", "volume"],
            )
            df["date"] = pd.to_datetime(df["date"])
            df = df.set_index("date").sort_index()
            for col in ["open", "high", "low", "close", "volume"]:
                df[col] = pd.to_numeric(df[col], errors="coerce")
            _write_cache(stock_code, df)
            return df

        symbol = _sina_symbol(stock_code)
        url = "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
        params = {"symbol": symbol, "scale": 240, "ma": "no", "datalen": days}
        response = requests.get(url, params=params, timeout=15)
        data = response.json()
        if not data:
            return pd.DataFrame()
        df = pd.DataFrame(data)
        df = df.rename(columns={"day": "date", "vol": "volume"})
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date").sort_index()
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        _write_cache(stock_code, df)
        return df
    except Exception:
        return pd.DataFrame()


# ---------- MA120 信号 ----------
def get_stock_realtime_quote(stock_code: str) -> dict:
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
            f"MA120 {ma120:.2f}  偏离 {ma120_pct:.2f}%"
        )
    elif price > ma120 * SELL_THRESHOLD and prev_price <= prev_ma120 * SELL_THRESHOLD:
        result["signal"] = "sell"
        result["message"] = (
            f"📤 卖出：{price:.2f} > 卖出线 {sell_line:.2f}  "
            f"MA120 {ma120:.2f}  偏离 {ma120_pct:.2f}%"
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


def _print_console_table(title: str, items: list) -> None:
    print(f"\n{title} {len(items)} 支")
    if not items:
        print("  无数据")
        return

    headers = ["股票", "代码", "现价", "MA120", "偏离", "买入线", "卖出线", "状态"]
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
                f"{item['sell_line']:.2f}",
                _position_label(item),
            ]
        )

    widths = [max(len(headers[i]), max(len(row[i]) for row in rows)) for i in range(len(headers))]

    def fmt(row):
        return "| " + " | ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(row)) + " |"

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
            f"MA120 {item['ma120']:.2f}  偏离 {item['ma120_pct']:+.2f}%"
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
            f"MA120 {item['ma120']:.2f}  偏离 {item['ma120_pct']:+.2f}%"
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
    return "高于MA120" if item["price"] >= item["ma120"] else "低于MA120"


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
        ("diff", "偏离"),
        ("buy_line", "买入线"),
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
            f"MA120 {item['ma120']:.2f} | 偏离 {item['ma120_pct']:+.2f}%"
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


# ---------- 主流程 ----------
def run(mode: str = "all", feishu: bool = True):
    today = datetime.now().strftime("%Y-%m-%d")
    print(f"\n{'=' * 60}\n📊 MA120 监控 · {today}  mode={mode}\n{'=' * 60}")

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
    mode = "all"
    if "--portfolio" in args:
        mode = "portfolio"
    elif "--watchlist" in args:
        mode = "watchlist"
    feishu = "--no-feishu" not in args
    run(mode=mode, feishu=feishu)
