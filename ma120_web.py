"""Read-only MA120 dashboard data built from the existing stock monitor."""

import math
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from zoneinfo import ZoneInfo

import stock_dynamic_monitor as monitor
import pandas as pd


TIMEZONE = ZoneInfo("Asia/Shanghai")
HISTORY_FETCH_BARS = 800
HISTORY_WINDOW_BARS = 360
ESTIMATE_INITIAL_CAPITAL = 100000.0


def _positive_number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def build_ma120_history(code, name, hist_loader=None, now=None):
    """Return close-derived historical reference crossings, never actual fills."""
    hist_loader = hist_loader or monitor.get_stock_hist_data
    now = now or datetime.now(TIMEZONE)
    today = now.astimezone(TIMEZONE).date() if now.tzinfo else now.date()
    history = hist_loader(code, days=HISTORY_FETCH_BARS)
    if history is None or history.empty or "close" not in history:
        raise ValueError("没有可用的历史日 K")
    try:
        dates = pd.to_datetime(history.index, errors="raise")
    except (TypeError, ValueError) as exc:
        raise ValueError("历史日 K 日期无效") from exc
    if dates.has_duplicates or dates.isna().any():
        raise ValueError("历史日 K 日期重复或无效")
    if any(day.date() > today for day in dates):
        raise ValueError("历史日 K 包含未来日期")
    bars = history.copy()
    bars.index = dates
    bars = bars.sort_index()
    bars = bars.loc[[day.date() < today for day in bars.index]]
    if len(bars) < monitor.MA_LONG + 1:
        raise ValueError(f"已完成日 K 不足 {monitor.MA_LONG + 1} 根")
    latest_day = bars.index[-1].date()
    if (today - latest_day).days > 15:
        raise ValueError(f"历史日 K 已过期：最新日期 {latest_day.isoformat()}")
    closes = pd.to_numeric(bars["close"], errors="coerce")
    if not closes.map(lambda value: value is not None and math.isfinite(value) and value > 0).all():
        raise ValueError("历史日 K 收盘价无效")
    ma = closes.rolling(window=monitor.MA_LONG).mean()
    buy_line = ma * monitor.BUY_THRESHOLD
    sell_line = ma * monitor.SELL_THRESHOLD
    signals = []
    series = []
    for index in range(len(bars)):
        if index < monitor.MA_LONG - 1:
            continue
        day = bars.index[index].date().isoformat()
        close = float(closes.iloc[index])
        average = float(ma.iloc[index])
        buy = float(buy_line.iloc[index])
        sell = float(sell_line.iloc[index])
        side = None
        if index >= monitor.MA_LONG:
            prev_close = float(closes.iloc[index - 1])
            if close < buy and prev_close >= float(buy_line.iloc[index - 1]):
                side = "buy"
            elif close > sell and prev_close <= float(sell_line.iloc[index - 1]):
                side = "sell"
        series.append({"date": day, "close": close, "ma120": average,
                       "buy_line": buy, "sell_line": sell, "signal": side})
        if side:
            signals.append({"date": day, "side": side, "price": close,
                            "ma120": average, "threshold": buy if side == "buy" else sell})
    series = series[-HISTORY_WINDOW_BARS:]
    start = series[0]["date"]
    signals = [point for point in signals if point["date"] >= start]
    return {"code": code, "name": name,
            "currency": "HKD" if monitor._is_hk_code(code) else "CNY",
            "window_start": start, "window_end": series[-1]["date"],
            "series": series, "signals": signals,
            "buy_count": sum(point["side"] == "buy" for point in signals),
            "sell_count": sum(point["side"] == "sell" for point in signals)}


def estimate_ma120_profit(history, start_date):
    """Simulate next-bar-close fills from the displayed completed-bar signals."""
    if not start_date:
        raise ValueError("请选择起算日期")
    try:
        start = date.fromisoformat(start_date)
    except (TypeError, ValueError) as exc:
        raise ValueError("起算日期格式无效") from exc
    series = history.get("series") or []
    if not series:
        raise ValueError("没有可用于测算的历史日 K")
    first = date.fromisoformat(series[0]["date"])
    last = date.fromisoformat(series[-1]["date"])
    if start < first or start > last:
        raise ValueError(f"起算日期须在 {first.isoformat()} 至 {last.isoformat()} 之间")

    cash = ESTIMATE_INITIAL_CAPITAL
    units = 0.0
    trades = []
    pending = None
    for index, point in enumerate(series):
        close = _positive_number(point.get("close"))
        if close is None:
            raise ValueError("历史日 K 收盘价无效")
        if pending is not None:
            signal_date, side = pending
            if side == "buy":
                units = cash / close
                cash = 0.0
                trade_units = units
            else:
                trade_units = units
                cash = units * close
                units = 0.0
            trades.append({"signal_date": signal_date, "date": point["date"],
                           "side": side, "price": close, "units": trade_units})
            pending = None
        if date.fromisoformat(point["date"]) < start or index == len(series) - 1:
            continue
        side = point.get("signal")
        if (side == "buy" and units == 0) or (side == "sell" and units > 0):
            pending = (point["date"], side)

    estimated_value = round(cash + units * float(series[-1]["close"]), 2)
    profit = round(estimated_value - ESTIMATE_INITIAL_CAPITAL, 2)
    return {"supported": True, "start_date": start.isoformat(),
            "end_date": last.isoformat(), "currency": history.get("currency", "CNY"),
            "initial_capital": ESTIMATE_INITIAL_CAPITAL,
            "estimated_value": estimated_value, "profit": profit,
            "return_pct": round(profit / ESTIMATE_INITIAL_CAPITAL * 100, 2),
            "buy_count": sum(trade["side"] == "buy" for trade in trades),
            "sell_count": sum(trade["side"] == "sell" for trade in trades),
            "open_position": units > 0, "trades": trades}


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
    """Return JSON-ready stock data without sending alerts or modifying source files.

    Optional loaders make it possible to serve cached market data and test without network.
    An item present in both sources appears only in the portfolio.
    """
    source_errors = []
    if portfolio is None:
        try:
            portfolio = monitor.load_portfolio()
        except Exception as exc:
            source_errors.append(f"读取 portfolio.json 失败：{exc}")
    if watchlist is None:
        try:
            watchlist = monitor.load_watchlist()
        except Exception as exc:
            source_errors.append(f"读取 watchlist.json 失败：{exc}")
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
