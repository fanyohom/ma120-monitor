"""Reusable point-in-time index valuation lookup for ETF history replay."""

from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from etf_data import DataError, positive


class ValuationReplay:
    def __init__(self, path: Path, index_code: str, settings: dict):
        self.path = Path(path)
        self.index_code = index_code
        self.settings = settings.copy()
        if not self.path.exists():
            raise DataError(f"Missing valuation CSV: {self.path.name}")

        frame = pd.read_csv(self.path, dtype={"index_code": str})
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
        frame = frame.sort_values("date")

        self.dates = frame["date"].to_numpy(dtype="datetime64[D]")
        self.available_dates = frame["available_date"].to_numpy(dtype="datetime64[D]")
        self.pe_ttm = frame["pe_ttm"].to_numpy(dtype=float)

    def at(self, as_of: date) -> dict:
        cutoff = np.datetime64(as_of, "D")
        start = np.datetime64((pd.Timestamp(as_of) - pd.DateOffset(
            years=self.settings["lookback_years"])).date(), "D")
        left = np.searchsorted(self.dates, start, side="left")
        right = np.searchsorted(self.dates, cutoff, side="right")
        published = self.available_dates[left:right] <= cutoff
        dates = self.dates[left:right][published]
        if not len(dates):
            raise DataError("No available index valuation observations")
        values = self.pe_ttm[left:right][published]
        latest = dates[-1]
        if int((cutoff - latest) / np.timedelta64(1, "D")) > self.settings["max_age_days"]:
            raise DataError("Index valuation is stale")
        if len(values) < self.settings["min_samples"]:
            raise DataError(f"Valuation needs {self.settings['min_samples']} samples; got {len(values)}")
        if int((latest - dates[0]) / np.timedelta64(1, "D")) < self.settings["min_span_days"]:
            raise DataError("Valuation history span is too short")
        current = float(values[-1])
        percentile = float((np.count_nonzero(values < current)
                            + np.count_nonzero(values == current) / 2) / len(values) * 100)
        return {
            "pe_ttm": current, "percentile": percentile, "index_code": self.index_code,
            "date": np.datetime_as_string(latest, unit="D"), "samples": len(values),
            "start_date": np.datetime_as_string(dates[0], unit="D"), "source": str(self.path),
        }
