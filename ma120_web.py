"""Read-only MA120 dashboard data built from the existing stock monitor."""

import math
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from zoneinfo import ZoneInfo

import stock_dynamic_monitor as monitor


TIMEZONE = ZoneInfo("Asia/Shanghai")


def _positive_number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _row(item, *, portfolio, hist_loader, quote_loader, today):
    row = {
        "code": item["code"],
        "name": item["name"],
        "currency": "HKD" if monitor._is_hk_code(item["code"]) else "CNY",
        "price": None,
        "price_source": None,
        "price_date": None,
        "quote_date": None,
        "quote_time": None,
        "last_close": None,
        "last_close_date": None,
        "ma120": None,
        "ma120_pct": None,
        "buy_line": None,
        "sell_line": None,
        "signal": None,
        "zone": None,
        "status": "数据不足",
        "error": None,
    }
    if portfolio:
        row.update({
            "cost": _positive_number(item.get("cost")),
            "shares": _positive_number(item.get("shares")),
            "market_value": None,
            "cost_basis": None,
            "unrealized_pnl": None,
            "unrealized_pnl_pct": None,
        })

    try:
        history = hist_loader(item["code"])
        if history is None or history.empty or len(history) < monitor.MA_LONG:
            row["error"] = f"日 K 不足 {monitor.MA_LONG} 根"
            return row
        history = history.sort_index()
        last_day = history.index[-1].date()
        row["last_close_date"] = last_day.isoformat()
        if last_day > today:
            row["status"] = "日期异常"
            row["error"] = f"最新日 K 日期 {last_day} 晚于今天"
            return row
        if (today - last_day).days > 15:
            row["status"] = "行情过期"
            row["error"] = f"最新日 K 日期 {last_day} 距今超过 15 天"
            return row
        last_close = _positive_number(history.iloc[-1]["close"])
        if last_close is None:
            row["error"] = "最新日 K 收盘价无效"
            return row
        row["last_close"] = last_close

        try:
            quote = quote_loader(item["code"]) or {}
        except Exception:
            quote = {}
        quote_price = _positive_number(quote.get("price"))
        quote_date = str(quote.get("quote_date") or "")
        try:
            parsed_quote_date = date.fromisoformat(quote_date)
            row["quote_date"] = parsed_quote_date.isoformat()
            valid_quote_date = parsed_quote_date == today and parsed_quote_date >= last_day
        except ValueError:
            valid_quote_date = False
        use_quote = quote_price is not None and valid_quote_date
        signal = monitor.check_ma120_signal(history, current_price=quote_price if use_quote else None)
        if signal is None or not all(math.isfinite(float(signal[key])) for key in
                                     ("price", "ma120", "ma120_pct", "buy_line", "sell_line")):
            row["error"] = "MA120 无法计算"
            return row

        today_signal = None
        if last_day == today and signal["signal"] in ("buy", "sell"):
            today_signal = signal["signal"]
        elif use_quote and last_day < today:
            buy_threshold = signal["ma120"] * monitor.BUY_THRESHOLD
            sell_threshold = signal["ma120"] * monitor.SELL_THRESHOLD
            if signal["price"] < buy_threshold <= last_close:
                today_signal = "buy"
            elif signal["price"] > sell_threshold >= last_close:
                today_signal = "sell"

        row.update({
            "price": float(signal["price"]),
            "price_source": "realtime" if use_quote else "daily_close",
            "price_date": quote_date if use_quote else last_day.isoformat(),
            "quote_time": str(quote.get("quote_time") or "") if use_quote else None,
            "ma120": float(signal["ma120"]),
            "ma120_pct": float(signal["ma120_pct"]),
            "buy_line": float(signal["buy_line"]),
            "sell_line": float(signal["sell_line"]),
            "signal": today_signal,
        })
        if row["price"] < row["buy_line"]:
            row["zone"] = "below_buy"
        elif row["price"] > row["sell_line"]:
            row["zone"] = "above_sell"
        else:
            row["zone"] = "between"
        row["status"] = monitor._card_status({**signal, "signal": row["signal"] or "hold"})

        if portfolio and row["shares"] is not None:
            row["market_value"] = round(row["price"] * row["shares"], 2)
            if row["cost"] is not None:
                row["cost_basis"] = round(row["cost"] * row["shares"], 2)
                row["unrealized_pnl"] = round(row["market_value"] - row["cost_basis"], 2)
                row["unrealized_pnl_pct"] = round(row["unrealized_pnl"] / row["cost_basis"] * 100, 2)
        return row
    except Exception as exc:
        row["error"] = f"行情获取失败：{exc}"
        return row


def build_ma120_dashboard(portfolio=None, watchlist=None, hist_loader=None, quote_loader=None, now=None):
    """Return JSON-ready stock data without sending alerts or modifying the workbook.

    Optional loaders make it possible to serve cached market data and test without network.
    An item present in both workbook sheets appears only in the portfolio.
    """
    source_errors = []
    if portfolio is None or watchlist is None:
        if not os.path.isfile(monitor.STOCKS_FILE):
            source_errors.append("未找到 stocks.xlsx 数据文件")
        else:
            if portfolio is None:
                try:
                    rows = monitor._read_xlsx_rows(monitor.STOCKS_FILE, monitor.PORTFOLIO_SHEET)
                    portfolio = [
                        {"code": row["code"], "name": row["name"],
                         "cost": monitor._safe_float(row.get("cost")),
                         "shares": monitor._safe_float(row.get("shares"))}
                        for row in rows if row.get("code") and row.get("name")
                    ]
                except Exception as exc:
                    source_errors.append(f"读取 stocks.xlsx[{monitor.PORTFOLIO_SHEET}] 失败：{exc}")
            if watchlist is None:
                try:
                    rows = monitor._read_xlsx_rows(monitor.STOCKS_FILE, monitor.WATCHLIST_SHEET)
                    watchlist = [
                        {"code": row["code"], "name": row["name"]}
                        for row in rows if row.get("code") and row.get("name")
                    ]
                except Exception as exc:
                    source_errors.append(f"读取 stocks.xlsx[{monitor.WATCHLIST_SHEET}] 失败：{exc}")
    source_error = "；".join(source_errors) if source_errors else None
    portfolio = portfolio or []
    watchlist = watchlist or []
    hist_loader = hist_loader or monitor.get_stock_hist_data
    quote_loader = quote_loader or monitor.get_stock_realtime_quote
    now = now or datetime.now(TIMEZONE)
    today = now.astimezone(TIMEZONE).date() if now.tzinfo else now.date()

    portfolio_codes = {item["code"] for item in portfolio}
    entries = [(item, True) for item in portfolio] + [
        (item, False) for item in watchlist if item["code"] not in portfolio_codes
    ]
    if entries:
        with ThreadPoolExecutor(max_workers=min(4, len(entries))) as pool:
            rows = list(pool.map(
                lambda entry: _row(entry[0], portfolio=entry[1],
                                   hist_loader=hist_loader, quote_loader=quote_loader,
                                   today=today),
                entries,
            ))
    else:
        rows = []
    portfolio_rows = rows[:len(portfolio)]
    watchlist_rows = rows[len(portfolio):]
    all_rows = portfolio_rows + watchlist_rows
    valued_rows = [row for row in portfolio_rows if row["cost_basis"] is not None]
    currency_totals = {}
    for currency in sorted({row["currency"] for row in portfolio_rows}):
        covered = [row for row in valued_rows if row["currency"] == currency]
        market_value = round(sum(row["market_value"] for row in covered), 2)
        cost_basis = round(sum(row["cost_basis"] for row in covered), 2)
        unrealized_pnl = round(sum(row["unrealized_pnl"] for row in covered), 2)
        currency_totals[currency] = {
            "market_value": market_value,
            "cost_basis": cost_basis,
            "unrealized_pnl": unrealized_pnl,
            "unrealized_pnl_pct": round(unrealized_pnl / cost_basis * 100, 2) if cost_basis else None,
            "pnl_coverage": len(covered),
        }
    if len(currency_totals) > 1:
        combined = {key: None for key in
                    ("market_value", "cost_basis", "unrealized_pnl", "unrealized_pnl_pct")}
    else:
        combined = next(iter(currency_totals.values()), {
            "market_value": 0,
            "cost_basis": 0,
            "unrealized_pnl": 0,
            "unrealized_pnl_pct": None,
        })

    return {
        "generated_at": now.isoformat(),
        "source_error": source_error,
        "strategy": {
            "ma_days": monitor.MA_LONG,
            "buy_factor": monitor.BUY_THRESHOLD,
            "sell_factor": monitor.SELL_THRESHOLD,
        },
        "summary": {
            "portfolio_count": len(portfolio_rows),
            "watchlist_count": len(watchlist_rows),
            "market_value": combined["market_value"],
            "cost_basis": combined["cost_basis"],
            "unrealized_pnl": combined["unrealized_pnl"],
            "unrealized_pnl_pct": combined["unrealized_pnl_pct"],
            "pnl_coverage": len(valued_rows),
            "currency_totals": currency_totals,
            "buy_signals": sum(row["signal"] == "buy" for row in all_rows),
            "sell_signals": sum(row["signal"] == "sell" for row in all_rows),
            "buy_zone": sum(row["zone"] == "below_buy" for row in all_rows),
            "sell_zone": sum(row["zone"] == "above_sell" for row in all_rows),
            "data_unavailable": sum(row["error"] is not None for row in all_rows),
        },
        "portfolio": portfolio_rows,
        "watchlist": watchlist_rows,
    }
