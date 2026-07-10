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

import pandas as pd
from datetime import datetime
import warnings
import time
import requests
import os
import json
import sys
warnings.filterwarnings('ignore')

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
FEISHU_USER_ID = "ou_2eaab8f758ac6973826b4cf591791afb"


# ---------- 数据文件加载 ----------
# 列名别名：让表头可以用中文或英文
COLUMN_ALIASES = {
    "code": ["code", "股票代码", "代码"],
    "name": ["name", "股票名称", "名称"],
    "cost": ["cost", "成本价", "成本"],
    "shares": ["shares", "持仓数量", "数量"],
}


def _normalize_row(row: dict) -> dict:
    """把中文/英文列名统一成内部键名"""
    out = {}
    for canonical, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in row and row[alias] not in (None, ""):
                out[canonical] = row[alias]
                break
        else:
            out[canonical] = row.get(canonical, "")
    return out


def _safe_float(v) -> float:
    if v is None or v == "":
        return 0.0
    try:
        return float(v)
    except Exception:
        return 0.0


def _safe_code(v) -> str:
    """Excel 会把纯数字代码读成 int（600015 → 600015），补齐到 6 位"""
    if v is None:
        return ""
    s = str(v).strip()
    if s.endswith(".0"):
        s = s[:-2]
    if s.isdigit() and len(s) < 6:
        s = s.zfill(6)
    return s


def _read_xlsx_rows(path: str, sheet: str) -> list:
    """读 xlsx 指定 sheet，返回列表[dict]，跳过空行和代码以 # 开头的注释行"""
    df = pd.read_excel(path, sheet_name=sheet, dtype=str, engine="openpyxl")
    df.columns = [str(c).strip() for c in df.columns]
    rows = []
    for _, r in df.iterrows():
        raw = {k: ("" if pd.isna(v) else str(v).strip()) for k, v in r.items()}
        # 全空行跳过
        if not any(raw.values()):
            continue
        norm = _normalize_row(raw)
        # 代码以 # 开头作为注释行
        if str(norm.get("code", "")).lstrip().startswith("#"):
            continue
        norm["code"] = _safe_code(norm.get("code"))
        rows.append(norm)
    return rows


def load_portfolio() -> list:
    if not os.path.exists(STOCKS_FILE):
        print(f"⚠️ 未找到数据文件: {STOCKS_FILE}")
        return []
    try:
        rows = _read_xlsx_rows(STOCKS_FILE, PORTFOLIO_SHEET)
        out = []
        for it in rows:
            if not it.get("code") or not it.get("name"):
                continue
            out.append({
                "code": it["code"],
                "name": it["name"],
                "cost": _safe_float(it.get("cost")),
                "shares": _safe_float(it.get("shares")),
            })
        return out
    except Exception as e:
        print(f"⚠️ 读取 {os.path.basename(STOCKS_FILE)}[{PORTFOLIO_SHEET}] 失败: {e}")
        return []


def load_watchlist() -> list:
    if not os.path.exists(STOCKS_FILE):
        return []
    try:
        rows = _read_xlsx_rows(STOCKS_FILE, WATCHLIST_SHEET)
        out = []
        for it in rows:
            if not it.get("code") or not it.get("name"):
                continue
            out.append({"code": it["code"], "name": it["name"]})
        return out
    except Exception as e:
        print(f"⚠️ 读取 {os.path.basename(STOCKS_FILE)}[{WATCHLIST_SHEET}] 失败: {e}")
        return []


# ---------- K线获取 ----------
def _cache_path(code: str) -> str:
    os.makedirs(CACHE_DIR, exist_ok=True)
    return os.path.join(CACHE_DIR, f"sina_{code}.json")


def _read_cache(code: str) -> pd.DataFrame | None:
    p = _cache_path(code)
    if not os.path.exists(p):
        return None
    try:
        if time.time() - os.path.getmtime(p) > CACHE_DAYS * 86400:
            return None
        with open(p) as f:
            cached = json.load(f)
        df = pd.DataFrame(cached["data"])
        df['date'] = pd.to_datetime(df['date'])
        df = df.set_index('date').sort_index()
        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = pd.to_numeric(df[col], errors='coerce')
        return df
    except Exception:
        return None


def _write_cache(code: str, df: pd.DataFrame) -> None:
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        data = df.reset_index().to_dict('records')
        for d in data:
            d['date'] = d['date'].isoformat()
        with open(_cache_path(code), "w") as f:
            json.dump({"data": data}, f)
    except Exception:
        pass


def get_stock_hist_data(stock_code: str, days: int = DATA_DAYS) -> pd.DataFrame:
    """Sina K线（前复权）"""
    cached = _read_cache(stock_code)
    if cached is not None and len(cached) >= MA_LONG:
        return cached
    try:
        symbol = f"sh{stock_code}" if stock_code.startswith('6') else f"sz{stock_code}"
        url = "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
        params = {"symbol": symbol, "scale": 240, "ma": "no", "datalen": days}
        r = requests.get(url, params=params, timeout=15)
        data = r.json()
        if not data:
            return pd.DataFrame()
        df = pd.DataFrame(data)
        df = df.rename(columns={'day': 'date', 'vol': 'volume'})
        df['date'] = pd.to_datetime(df['date'])
        df = df.set_index('date').sort_index()
        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = pd.to_numeric(df[col], errors='coerce')
        _write_cache(stock_code, df)
        return df
    except Exception:
        return pd.DataFrame()


# ---------- MA120 信号 ----------
def check_ma120_signal(df: pd.DataFrame) -> dict | None:
    if len(df) < MA_LONG:
        return None
    df = df.copy()
    df['ma120'] = df['close'].rolling(window=MA_LONG).mean()
    latest = df.iloc[-1]
    prev = df.iloc[-2] if len(df) > 1 else latest

    price = latest['close']
    ma120 = latest['ma120']
    if pd.isna(ma120) or ma120 == 0:
        return None

    ma120_pct = (price - ma120) / ma120 * 100
    prev_price = prev['close']
    prev_ma120 = prev['ma120'] if not pd.isna(prev['ma120']) else ma120

    buy_line = round(ma120 * BUY_THRESHOLD, 2)
    sell_line = round(ma120 * SELL_THRESHOLD, 2)

    result = {
        'price': price, 'ma120': ma120, 'ma120_pct': ma120_pct,
        'signal': 'hold', 'buy_line': buy_line, 'sell_line': sell_line,
        'message': '',
    }

    if price < ma120 * BUY_THRESHOLD and prev_price >= prev_ma120 * BUY_THRESHOLD:
        result['signal'] = 'buy'
        result['message'] = f"🔥 买入！{price:.2f} < 买入线 {buy_line:.2f}  MA120 {ma120:.2f}  偏离 {ma120_pct:.2f}%"
    elif price > ma120 * SELL_THRESHOLD and prev_price <= prev_ma120 * SELL_THRESHOLD:
        result['signal'] = 'sell'
        result['message'] = f"📤 卖出！{price:.2f} > 卖出线 {sell_line:.2f}  MA120 {ma120:.2f}  偏离 {ma120_pct:.2f}%"
    elif price >= ma120:
        result['message'] = f"📈 高于 MA120 {ma120:.2f}  +{ma120_pct:.2f}%  卖出线 {sell_line:.2f}"
    else:
        result['message'] = f"📊 低于 MA120 {ma120:.2f}  {ma120_pct:.2f}%  买入线 {buy_line:.2f}"
    return result


def enrich(item: dict) -> dict | None:
    """取K线 + 算信号，返回 None 表示数据不够"""
    df = get_stock_hist_data(item["code"])
    if df.empty or len(df) < MA_LONG:
        return None
    sig = check_ma120_signal(df)
    if not sig:
        return None
    return {**item, **sig}


# ---------- 飞书发送 ----------
def send_feishu(text: str) -> bool:
    try:
        import subprocess
        args = ["openclaw", "message", "send", "--channel", "feishu",
                "--target", FEISHU_USER_ID, "--message", text]
        result = subprocess.run(args, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            print(f"     stderr: {result.stderr[:200]}")
        return result.returncode == 0
    except Exception as e:
        print(f"     ⚠️ 飞书发送异常: {e}")
        return False


# ---------- 文本渲染 ----------
def _signal_tag(r: dict) -> str:
    if r['signal'] == 'buy':
        return "🔥 买入"
    if r['signal'] == 'sell':
        return "📤 卖出"
    if r['price'] >= r['ma120']:
        return "📈 高于 MA120"
    return "📊 低于 MA120"


def render_portfolio_text(items: list, today: str) -> str:
    """持仓报告：优先出信号股票，再按 MA120 偏离排序"""
    if not items:
        return f"📦 持仓监控 · {today}\n\n无数据"

    # 按信号优先级排序： buy/sell > 偏离绝对值
    def sort_key(r):
        prio = {'buy': 0, 'sell': 0}.get(r['signal'], 1)
        return (prio, -abs(r['ma120_pct']))
    items = sorted(items, key=sort_key)

    lines = [f"📦 持仓监控 · {today}", ""]
    total_cost = total_value = 0.0
    signal_count = {'buy': 0, 'sell': 0}

    for r in items:
        tag = _signal_tag(r)
        if r['signal'] in signal_count:
            signal_count[r['signal']] += 1
        line = f"{tag}  {r['name']} ({r['code']})  现价 {r['price']:.2f}  MA120 {r['ma120']:.2f}  偏离 {r['ma120_pct']:+.2f}%"
        if r.get('cost') and r.get('shares'):
            pnl_pct = (r['price'] - r['cost']) / r['cost'] * 100
            value = r['price'] * r['shares']
            cost_amt = r['cost'] * r['shares']
            total_cost += cost_amt
            total_value += value
            line += f"  |  成本 {r['cost']:.2f}  浮盈 {pnl_pct:+.2f}%"
        if r['signal'] == 'buy':
            line += f"  → 买入线 {r['buy_line']:.2f}"
        elif r['signal'] == 'sell':
            line += f"  → 卖出线 {r['sell_line']:.2f}"
        else:
            line += f"  → 买 {r['buy_line']:.2f} / 卖 {r['sell_line']:.2f}"
        lines.append(line)

    # 汇总
    summary = []
    if signal_count['buy'] or signal_count['sell']:
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

    def sort_key(r):
        prio = {'buy': 0, 'sell': 0}.get(r['signal'], 1)
        return (prio, -abs(r['ma120_pct']))
    items = sorted(items, key=sort_key)

    lines = [f"👀 关注池 · {today}", ""]
    signal_count = {'buy': 0, 'sell': 0}
    for r in items:
        tag = _signal_tag(r)
        if r['signal'] in signal_count:
            signal_count[r['signal']] += 1
        line = f"{tag}  {r['name']} ({r['code']})  现价 {r['price']:.2f}  MA120 {r['ma120']:.2f}  偏离 {r['ma120_pct']:+.2f}%"
        if r['signal'] == 'buy':
            line += f"  → 买入线 {r['buy_line']:.2f}"
        elif r['signal'] == 'sell':
            line += f"  → 卖出线 {r['sell_line']:.2f}"
        else:
            line += f"  → 买 {r['buy_line']:.2f} / 卖 {r['sell_line']:.2f}"
        lines.append(line)
    if signal_count['buy'] or signal_count['sell']:
        lines.append("")
        lines.append(f"今日信号：🔥买入 {signal_count['buy']} · 📤卖出 {signal_count['sell']}")
    return "\n".join(lines)


# ---------- 主流程 ----------
def run(mode: str = "all", feishu: bool = True):
    today = datetime.now().strftime("%Y-%m-%d")
    print(f"\n{'='*60}\n📊 MA120 监控 · {today}  mode={mode}\n{'='*60}")

    portfolio_items = []
    watchlist_items = []

    if mode in ("all", "portfolio"):
        portfolio = load_portfolio()
        print(f"\n📦 持仓 {len(portfolio)} 支")
        for it in portfolio:
            r = enrich(it)
            if r is None:
                print(f"  ⚠️ {it['name']} ({it['code']}): 数据不足")
                continue
            portfolio_items.append(r)
            print(f"  {r['message']} [{r['name']}]")
            time.sleep(0.2)

    if mode in ("all", "watchlist"):
        watchlist = load_watchlist()
        # 去重：跳过已在持仓里的
        if mode == "all":
            portfolio_codes = {x["code"] for x in portfolio_items}
            watchlist = [w for w in watchlist if w["code"] not in portfolio_codes]
        print(f"\n👀 关注池 {len(watchlist)} 支")
        for it in watchlist:
            r = enrich(it)
            if r is None:
                print(f"  ⚠️ {it['name']} ({it['code']}): 数据不足")
                continue
            watchlist_items.append(r)
            print(f"  {r['message']} [{r['name']}]")
            time.sleep(0.2)

    # 发飞书
    if feishu and (portfolio_items or watchlist_items):
        print(f"\n📤 发送飞书...")
        if portfolio_items:
            ok = send_feishu(render_portfolio_text(portfolio_items, today))
            print(f"  持仓: {'✅' if ok else '⚠️ 失败'}")
        if watchlist_items:
            ok = send_feishu(render_watchlist_text(watchlist_items, today))
            print(f"  关注池: {'✅' if ok else '⚠️ 失败'}")

    print(f"\n{'='*60}\n✅ 完成\n{'='*60}\n")
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
