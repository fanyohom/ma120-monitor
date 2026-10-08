"""Import CSI 300 rolling P/E history from the CSI Index official API."""

import argparse
import csv
import json
import os
import tempfile
import tomllib
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen
from zoneinfo import ZoneInfo


BASE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = BASE_DIR / "etf_valuations" / "000300.csv"
SOURCE_URL = "https://www.csindex.com.cn/csindex-home/perf/index-perf"
INDEX_CODE = "000300"
CSV_COLUMNS = ("date", "available_date", "index_code", "pe_ttm")
REPLAY_HISTORY_START = date(2012, 9, 4)


def years_ago(day: date, years: int) -> date:
    try:
        return day.replace(year=day.year - years)
    except ValueError:
        return day.replace(year=day.year - years, day=28)


def valuation_settings() -> dict:
    config = BASE_DIR / "etf_plans.toml"
    if not config.exists():
        config = BASE_DIR / "etf_plans.example.toml"
    with config.open("rb") as handle:
        return tomllib.load(handle)["valuation"]


def fetch_history(start: date, end: date) -> dict:
    params = urlencode({"indexCode": INDEX_CODE, "startDate": start.strftime("%Y%m%d"),
                        "endDate": end.strftime("%Y%m%d")})
    with urlopen(f"{SOURCE_URL}?{params}", timeout=30) as response:
        return json.load(response, parse_float=Decimal)


def parse_history(payload: dict, start: date, end: date) -> list[tuple[date, Decimal]]:
    if not isinstance(payload, dict) or str(payload.get("code")) != "200" or payload.get("success") is not True:
        raise ValueError("CSI Index API did not return success")
    data = payload.get("data")
    if not isinstance(data, list) or not data:
        raise ValueError("CSI Index API returned no history")

    records = []
    previous = None
    for position, row in enumerate(data):
        if not isinstance(row, dict) or row.get("indexCode") != INDEX_CODE:
            raise ValueError("Unexpected index code in CSI Index history")
        raw_date = row.get("tradeDate")
        if not isinstance(raw_date, str) or len(raw_date) != 8 or not raw_date.isascii() or not raw_date.isdigit():
            raise ValueError("Invalid valuation date in CSI Index history")
        trade_date = datetime.strptime(raw_date, "%Y%m%d").date()
        if not start <= trade_date <= end:
            raise ValueError("CSI Index history contains a date outside the requested range")
        if previous is not None and trade_date <= previous:
            raise ValueError("CSI Index history has duplicate or unordered dates")
        previous = trade_date

        raw_pe = row.get("peg")
        if isinstance(raw_pe, bool) or not isinstance(raw_pe, (int, float, str, Decimal)):
            raise ValueError("Invalid rolling P/E in CSI Index history")
        try:
            pe_ttm = Decimal(str(raw_pe))
        except InvalidOperation as exc:
            raise ValueError("Invalid rolling P/E in CSI Index history") from exc
        if not pe_ttm.is_finite() or pe_ttm <= 0:
            raise ValueError("Invalid rolling P/E in CSI Index history")

        if trade_date.weekday() >= 5:
            if position not in (0, len(data) - 1):
                raise ValueError("Unexpected weekend date inside CSI Index history")
            continue
        records.append((trade_date, pe_ttm))

    if not records:
        raise ValueError("CSI Index history has no trading-day observations")
    return records


def validate_history(records: list[tuple[date, Decimal]], as_of: date, settings: dict) -> dict:
    window_start = years_ago(as_of, settings["lookback_years"])
    # The official feed has dates, but no per-observation publication timestamps.
    # Treat each day's value as available on the next calendar day.
    available = [(day, pe) for day, pe in records
                 if window_start <= day <= as_of and day + timedelta(days=1) <= as_of]
    if len(available) < settings["min_samples"]:
        raise ValueError(f"Valuation needs {settings['min_samples']} samples; got {len(available)}")
    first, last = available[0][0], available[-1][0]
    if (last - first).days < settings["min_span_days"]:
        raise ValueError("Valuation history span is too short")
    if (as_of - last).days > settings["max_age_days"]:
        raise ValueError("Index valuation is stale")
    return {"samples": len(available), "start_date": first, "latest_date": last}


def update_valuation(output: Path = DEFAULT_OUTPUT, today: date | None = None,
                     settings: dict | None = None) -> dict:
    today = today or datetime.now(ZoneInfo("Asia/Shanghai")).date()
    settings = settings or valuation_settings()
    as_of = today - timedelta(days=1)
    # Earlier official records contain missing rolling P/E values. This is the
    # first trading day after the last missing value in the historical feed.
    records = parse_history(fetch_history(REPLAY_HISTORY_START, as_of), REPLAY_HISTORY_START, as_of)
    summary = validate_history(records, as_of, settings)

    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="", dir=output.parent,
                                         prefix=f".{output.name}.", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            writer = csv.writer(handle)
            writer.writerow(CSV_COLUMNS)
            for day, pe in records:
                writer.writerow((day.isoformat(), (day + timedelta(days=1)).isoformat(),
                                 INDEX_CODE, format(pe, "f")))
        os.replace(temporary, output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    summary = update_valuation(args.output)
    print(f"Updated {args.output}: {summary['samples']} available observations, "
          f"{summary['start_date']} to {summary['latest_date']}")


if __name__ == "__main__":
    main()
