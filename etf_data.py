"""ETF quotes and point-in-time index valuations for DCA previews."""

import json
import math
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import requests
from market_store import MarketStore, database_for_cache, database_for_valuation


class DataError(ValueError):
    pass


def etf_symbol(code: str) -> str:
    if len(code) != 6 or not code.isdigit() or not code.startswith(("5", "15", "16")):
        raise DataError(f"Unsupported mainland ETF code: {code}")
    return ("sh" if code.startswith("5") else "sz") + code


def positive(value, label: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise DataError(f"{label} must be finite and positive")
    return number


def parse_history(rows: list) -> pd.Series:
    if not rows:
        raise DataError("Empty ETF history")
    frame = pd.DataFrame([(row[0], row[2]) for row in rows], columns=["date", "close"])
    frame["date"] = pd.to_datetime(frame["date"], format="%Y-%m-%d", errors="raise")
    if frame["date"].duplicated().any():
        raise DataError("Duplicate ETF history dates")
    frame["close"] = frame["close"].map(lambda value: positive(value, "close"))
    return frame.set_index("date")["close"].sort_index()


def _fetch_daily_history(symbol: str, days: int, adjustment: str, as_of: date) -> list:
    field = "qfqday" if adjustment else "day"
    rows = []
    # The caller requests completed days only. Tencent can append a live bar
    # even with an explicit current-day end, and qfq/raw can update separately.
    end = as_of.isoformat()
    while len(rows) < days:
        page_size = min(800, days - len(rows))
        response = requests.get(
            "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
            params={"param": f"{symbol},day,,{end},{page_size},{adjustment}"}, timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("msg"):
            raise DataError("ETF history provider returned an error")
        data = payload.get("data")
        market = data.get(symbol) if isinstance(data, dict) else None
        if not isinstance(market, dict):
            raise DataError("Invalid ETF history response")
        page = market.get(field)
        if page is None and adjustment and market.get("day") == []:
            page = []
        if not isinstance(page, list):
            raise DataError("Missing ETF adjusted or raw history")
        if not page:
            if rows:
                break
            raise DataError("Empty ETF history")
        if len(page) > page_size:
            raise DataError("Invalid ETF history page")
        parsed = parse_history(page)
        page_dates = [stamp.date().isoformat() for stamp in parsed.index]
        if [row[0] for row in page] != page_dates:
            raise DataError("ETF history page is not ordered by date")
        if rows and page_dates[-1] >= rows[0][0]:
            raise DataError("ETF history pages overlap or are out of order")
        if end and page_dates[-1] > end:
            raise DataError("ETF history page exceeds requested end date")
        rows = page + rows
        if len(page) < page_size:
            break
        end = (parsed.index[0].date() - timedelta(days=1)).isoformat()
    return rows


def fetch_market(code: str, days: int, now: datetime, cache_dir: Path, force: bool = False,
                 database_only: bool = False) -> dict:
    """Read durable history; explicit refresh saves a new complete version."""
    if days < 1:
        raise DataError("ETF history length must be positive")
    symbol = etf_symbol(code)
    store = MarketStore(database_for_cache(cache_dir))
    stored = store.get_history("etf", code, min_rows=days) or store.get_history("etf", code, longest=True)
    if stored is not None:
        age = (now - datetime.fromisoformat(stored["fetched_at"])).total_seconds()
        if database_only or (not force and 0 <= age <= 300 and stored["requested_days"] >= days):
            stored["data_stale"] = not 0 <= age <= 300
            return stored
    if database_only:
        raise DataError("数据库尚无该 ETF 行情，请刷新数据")
    folder = cache_dir / now.date().isoformat()
    if stored is None and not force:
        for path in sorted(folder.glob(f"{code}_*.json"), reverse=True):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                age = (now - datetime.fromisoformat(payload["fetched_at"])).total_seconds()
                if 0 <= age <= 300 and payload["requested_days"] >= days:
                    save_market(store, code, payload)
                    return payload
            except (ValueError, TypeError, KeyError, OSError):
                continue

    try:
        payload = _fetch_market_online(symbol, code, days, now)
        save_market(store, code, payload)
        return payload
    except (OSError, ValueError, KeyError, IndexError) as exc:
        if stored is None:
            raise
        return {**stored, "data_stale": True, "refresh_error": str(exc)}


def save_market(store, code, payload):
    metadata = {key: value for key, value in payload.items() if key not in ("qfq", "raw")}
    store.put_history("etf", code, {key: payload[key] for key in ("qfq", "raw")},
                      metadata, payload["fetched_at"])
    store.put_quote("etf", code, {key: payload[key] for key in ("price", "quote_at", "volume", "source")},
                    payload["fetched_at"])


def _fetch_market_online(symbol, code, days, now):
    histories = {}
    for adjustment in ("qfq", ""):
        # A raw series must never silently stand in for adjusted prices.
        histories["qfq" if adjustment else "raw"] = _fetch_daily_history(
            symbol, days, adjustment, now.date() - timedelta(days=1)
        )
    if [row[0] for row in histories["qfq"]] != [row[0] for row in histories["raw"]]:
        raise DataError("Adjusted/raw ETF history dates disagree")

    response = requests.get(f"https://qt.gtimg.cn/q={symbol}", timeout=15)
    response.raise_for_status()
    response.encoding = "gbk"
    fields = response.text.split('="', 1)[1].rsplit('"', 1)[0].split("~")
    quote_at = datetime.strptime(fields[30], "%Y%m%d%H%M%S").replace(tzinfo=now.tzinfo)
    payload = {
        "code": code, "source": "Tencent qfq + raw daily / realtime",
        "fetched_at": now.isoformat(), "requested_days": days,
        "quote_at": quote_at.isoformat(), "price": positive(fields[3], "price"),
        "volume": float(fields[6]), **histories,
    }
    return payload


def market_metrics(payload: dict, ma_days: int, now: datetime) -> dict:
    qfq = parse_history(payload["qfq"])
    raw = parse_history(payload["raw"])
    today = pd.Timestamp(now.date())
    if qfq.index.max() > today or raw.index.max() > today:
        raise DataError("ETF history includes future dates")
    if qfq.index.max() != raw.index.max():
        raise DataError("Adjusted/raw history end dates disagree")
    # Use the latest common bar's open to align the two price scales; cached
    # snapshots from earlier versions may also contain a still-changing live close.
    anchor = qfq.index.max()
    anchor_date = anchor.date().isoformat()
    adjusted_open = next(row[1] for row in payload["qfq"] if row[0] == anchor_date)
    raw_open = next(row[1] for row in payload["raw"] if row[0] == anchor_date)
    qfq = qfq * (positive(raw_open, "raw open") / positive(adjusted_open, "adjusted open"))
    completed = qfq[qfq.index < today]
    if completed.empty:
        raise DataError("No completed ETF bars")
    history_stale = (now.date() - completed.index[-1].date()).days > 15
    quote_at = datetime.fromisoformat(payload["quote_at"])
    if quote_at > now + timedelta(minutes=5):
        raise DataError("Quote timestamp is in the future")
    price = positive(payload["price"], "price")
    ma = float(completed.tail(ma_days).mean()) if len(completed) >= ma_days else None
    clock = now.hour * 60 + now.minute
    quote_clock = quote_at.hour * 60 + quote_at.minute
    in_session = 570 <= clock <= 690 or 780 <= clock <= 900
    recent = (now - quote_at).total_seconds() <= 600 if in_session else quote_clock >= (895 if clock > 900 else 685)
    tradable = (now.weekday() < 5 and quote_at.date() == now.date()
                and (quote_at.hour, quote_at.minute) >= (9, 30)
                and float(payload["volume"]) > 0 and recent
                and not payload.get("data_stale") and not history_stale)
    return {
        "price": price, "quote_at": payload["quote_at"], "ma": ma,
        "ma_days": ma_days, "ma_date": completed.index[-1].date().isoformat(),
        "ma_deviation_pct": (price / ma - 1) * 100 if ma else None,
        "ma_error": "" if ma else f"MA{ma_days} needs {ma_days} completed bars; got {len(completed)}",
        "tradable": tradable, "source": payload["source"],
        "fetched_at": payload.get("fetched_at", ""),
        "read_source": payload.get("read_source", "provider"),
        "data_stale": bool(payload.get("data_stale") or history_stale),
        "refresh_error": payload.get("refresh_error", ""),
    }


def valuation_metrics(path: Path, index_code: str, as_of: date, settings: dict) -> dict:
    """CSV uses only published observations available by as_of; no future-data leakage."""
    frame = load_valuation_history(path, index_code)
    return calculate_valuation(frame, path, index_code, as_of, settings)


def load_valuation_history(path: Path, index_code: str, database_path=None) -> pd.DataFrame:
    path = Path(path)
    store = MarketStore(database_path or database_for_valuation(path))
    series_key = str(path.resolve())
    if not path.exists():
        stored = store.get_valuations(series_key, index_code)
        if stored is None:
            raise DataError(f"Missing valuation CSV: {path.name} (数据库也无对应记录)")
        frame = pd.DataFrame(stored)
    else:
        frame = pd.read_csv(path, dtype={"index_code": str})
    required = {"date", "available_date", "index_code", "pe_ttm"}
    if not required.issubset(frame.columns):
        raise DataError("Valuation CSV needs date,available_date,index_code,pe_ttm")
    frame = frame[frame["index_code"] == index_code].copy()
    frame["date"] = pd.to_datetime(frame["date"], format="%Y-%m-%d", errors="raise")
    frame["available_date"] = pd.to_datetime(frame["available_date"], format="%Y-%m-%d", errors="raise")
    if frame["date"].isna().any() or frame["available_date"].isna().any():
        raise DataError("Invalid valuation or publication date")
    if (frame["available_date"] < frame["date"]).any() or frame["date"].duplicated().any():
        raise DataError("Invalid publication dates or duplicate valuation dates")
    frame["pe_ttm"] = frame["pe_ttm"].map(lambda value: positive(value, "pe_ttm"))
    if not frame.empty:
        records = [{"date": row.date.date().isoformat(),
                    "available_date": row.available_date.date().isoformat(), "pe_ttm": row.pe_ttm}
                   for row in frame.itertuples()]
        store.put_valuations(series_key, index_code, records)
    return frame


def calculate_valuation(frame, path, index_code, as_of, settings):
    cutoff = pd.Timestamp(as_of)
    start = cutoff - pd.DateOffset(years=settings["lookback_years"])
    frame = frame[(frame["date"] <= cutoff) & (frame["date"] >= start)
                  & (frame["available_date"] <= cutoff)].sort_values("date")
    if frame.empty:
        raise DataError("No available index valuation observations")
    latest = frame.iloc[-1]
    if (cutoff - latest["date"]).days > settings["max_age_days"]:
        raise DataError("Index valuation is stale")
    if len(frame) < settings["min_samples"]:
        raise DataError(f"Valuation needs {settings['min_samples']} samples; got {len(frame)}")
    if (latest["date"] - frame.iloc[0]["date"]).days < settings["min_span_days"]:
        raise DataError("Valuation history span is too short")
    values = frame["pe_ttm"]
    current = float(latest["pe_ttm"])
    # Midrank handles ties: a constant history is neutral (50%), not expensive (100%).
    percentile = float(((values < current).sum() + (values == current).sum() / 2) / len(values) * 100)
    return {
        "pe_ttm": current, "percentile": percentile, "index_code": index_code,
        "date": latest["date"].date().isoformat(), "samples": len(frame),
        "start_date": frame.iloc[0]["date"].date().isoformat(), "source": str(path),
    }
