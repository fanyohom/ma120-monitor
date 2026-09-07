"""ETF quotes and point-in-time index valuations for DCA previews."""

import json
import math
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import requests


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


def fetch_market(code: str, days: int, now: datetime, cache_dir: Path) -> dict:
    """Keep dated snapshots; reuse only a complete snapshot from the last 5 minutes."""
    symbol = etf_symbol(code)
    folder = cache_dir / now.date().isoformat()
    for path in sorted(folder.glob(f"{code}_*.json"), reverse=True):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            age = (now - datetime.fromisoformat(payload["fetched_at"])).total_seconds()
            if 0 <= age <= 300 and payload["requested_days"] >= days:
                return payload
        except (ValueError, TypeError, KeyError, OSError):
            continue

    histories = {}
    for adjustment in ("qfq", ""):
        response = requests.get(
            "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
            params={"param": f"{symbol},day,,,{days},{adjustment}"}, timeout=20,
        )
        response.raise_for_status()
        data = response.json().get("data", {}).get(symbol, {})
        # A raw series must never silently stand in for adjusted prices.
        rows = data.get("qfqday" if adjustment else "day")
        histories["qfq" if adjustment else "raw"] = rows or []
        parse_history(rows or [])

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
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{code}_{now.strftime('%H%M%S_%f')}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    return payload


def market_metrics(payload: dict, ma_days: int, now: datetime) -> dict:
    qfq = parse_history(payload["qfq"])
    raw = parse_history(payload["raw"])
    today = pd.Timestamp(now.date())
    if qfq.index.max() > today or raw.index.max() > today:
        raise DataError("ETF history includes future dates")
    if qfq.index.max() != raw.index.max():
        raise DataError("Adjusted/raw history end dates disagree")
    # Today's two close snapshots can differ between requests. Open is fixed intraday
    # and also carries today's adjustment factor on ex-distribution/split dates.
    anchor = qfq.index.max()
    anchor_date = anchor.date().isoformat()
    adjusted_open = next(row[1] for row in payload["qfq"] if row[0] == anchor_date)
    raw_open = next(row[1] for row in payload["raw"] if row[0] == anchor_date)
    qfq = qfq * (positive(raw_open, "raw open") / positive(adjusted_open, "adjusted open"))
    completed = qfq[qfq.index < today]
    if completed.empty:
        raise DataError("No completed ETF bars")
    if (now.date() - completed.index[-1].date()).days > 15:
        raise DataError("ETF daily history is stale")
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
                and float(payload["volume"]) > 0 and recent)
    return {
        "price": price, "quote_at": payload["quote_at"], "ma": ma,
        "ma_days": ma_days, "ma_date": completed.index[-1].date().isoformat(),
        "ma_deviation_pct": (price / ma - 1) * 100 if ma else None,
        "ma_error": "" if ma else f"MA{ma_days} needs {ma_days} completed bars; got {len(completed)}",
        "tradable": tradable, "source": payload["source"],
        "fetched_at": payload.get("fetched_at", ""),
    }


def valuation_metrics(path: Path, index_code: str, as_of: date, settings: dict) -> dict:
    """CSV uses only published observations available by as_of; no future-data leakage."""
    if not path.exists():
        raise DataError(f"Missing valuation CSV: {path.name}")
    frame = pd.read_csv(path, dtype={"index_code": str})
    required = {"date", "available_date", "index_code", "pe_ttm"}
    if not required.issubset(frame.columns):
        raise DataError("Valuation CSV needs date,available_date,index_code,pe_ttm")
    frame = frame[frame["index_code"] == index_code].copy()
    frame["date"] = pd.to_datetime(frame["date"], format="%Y-%m-%d", errors="raise")
    frame["available_date"] = pd.to_datetime(frame["available_date"], format="%Y-%m-%d", errors="raise")
    if (frame["available_date"] < frame["date"]).any() or frame["date"].duplicated().any():
        raise DataError("Invalid publication dates or duplicate valuation dates")
    cutoff = pd.Timestamp(as_of)
    start = cutoff - pd.DateOffset(years=settings["lookback_years"])
    frame = frame[(frame["date"] <= cutoff) & (frame["date"] >= start)
                  & (frame["available_date"] <= cutoff)].sort_values("date")
    if frame.empty:
        raise DataError("No available index valuation observations")
    frame["pe_ttm"] = frame["pe_ttm"].map(lambda value: positive(value, "pe_ttm"))
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
