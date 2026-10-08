import csv
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from etf_data import valuation_metrics
from etf_valuation_replay import ValuationReplay


SETTINGS = {"lookback_years": 10, "min_samples": 3, "min_span_days": 2, "max_age_days": 7}


class ValuationReplayTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "000300.csv"

    def write(self, rows):
        with self.path.open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(("date", "available_date", "index_code", "pe_ttm"))
            writer.writerows(rows)

    def test_reads_once_and_matches_existing_metrics_across_dates(self):
        first = date(2016, 9, 1)
        last = date(2026, 9, 30)
        days = pd.bdate_range(first, last)
        self.write((day.date().isoformat(), (day.date() + timedelta(days=1)).isoformat(),
                    "000300", 10 + position % 80 / 10) for position, day in enumerate(days))
        settings = {**SETTINGS, "min_samples": 2000, "min_span_days": 3285}
        with patch("etf_valuation_replay.pd.read_csv", wraps=pd.read_csv) as read_csv:
            replay = ValuationReplay(self.path, "000300", settings)
            actual = [replay.at(day) for day in (date(2025, 10, 15), date(2026, 3, 30),
                                                 date(2026, 10, 1))]
            read_csv.assert_called_once()
        for day, result in zip((date(2025, 10, 15), date(2026, 3, 30), date(2026, 10, 1)), actual):
            with self.subTest(day=day):
                self.assertEqual(result, valuation_metrics(self.path, "000300", day, settings))

    def test_excludes_unpublished_and_future_valuations(self):
        self.write([("2026-09-01", "2026-09-02", "000300", 10),
                    ("2026-09-02", "2026-09-03", "000300", 20),
                    ("2026-09-03", "2026-09-04", "000300", 15),
                    ("2026-09-04", "2026-09-08", "000300", 1),
                    ("2026-09-08", "2026-09-09", "000300", 100)])
        replay = ValuationReplay(self.path, "000300", SETTINGS)
        self.assertEqual(replay.at(date(2026, 9, 7)),
                         valuation_metrics(self.path, "000300", date(2026, 9, 7), SETTINGS))
        self.assertEqual(replay.at(date(2026, 9, 7))["pe_ttm"], 15)

    def test_invalid_csv_and_missing_path(self):
        with self.assertRaisesRegex(ValueError, "Missing valuation CSV"):
            ValuationReplay(self.path, "000300", SETTINGS)
        for rows, message in [
            ([("2026-09-01", "2026-09-01", "000300", 10),
              ("2026-09-01", "2026-09-02", "000300", 11)], "duplicate"),
            ([("2026-09-02", "2026-09-01", "000300", 10)], "publication"),
            ([("", "2026-09-01", "000300", 10)], "date"),
            ([("2026-09-01", "", "000300", 10)], "date"),
            ([("2026-09-01", "2026-09-01", "000300", "NaN")], "finite"),
            ([("2026-09-01", "2026-09-01", "000300", -1)], "finite"),
        ]:
            with self.subTest(message=message):
                self.write(rows)
                with self.assertRaisesRegex(ValueError, message):
                    ValuationReplay(self.path, "000300", SETTINGS)
        self.path.write_text("date,index_code,pe_ttm\n2026-09-01,000300,10\n")
        with self.assertRaisesRegex(ValueError, "needs date,available_date"):
            ValuationReplay(self.path, "000300", SETTINGS)

    def test_stale_minimum_samples_and_span_match_existing_errors(self):
        self.write([("2026-09-01", "2026-09-02", "000300", 10),
                    ("2026-09-02", "2026-09-03", "000300", 11),
                    ("2026-09-03", "2026-09-04", "000300", 12)])
        for as_of, settings in [
            (date(2026, 10, 1), SETTINGS),
            (date(2026, 9, 7), {**SETTINGS, "min_samples": 4}),
            (date(2026, 9, 7), {**SETTINGS, "min_span_days": 3}),
            (date(2026, 9, 1), SETTINGS),
        ]:
            with self.subTest(as_of=as_of, settings=settings):
                replay = ValuationReplay(self.path, "000300", settings)
                with self.assertRaises(ValueError) as expected:
                    valuation_metrics(self.path, "000300", as_of, settings)
                with self.assertRaises(type(expected.exception)) as actual:
                    replay.at(as_of)
                self.assertEqual(str(actual.exception), str(expected.exception))

    def test_real_csi_300_csv_matches_when_available(self):
        path = Path(__file__).resolve().parents[1] / "etf_valuations" / "000300.csv"
        if not path.exists():
            self.skipTest("Local CSI 300 history has not been imported")
        settings = {**SETTINGS, "min_samples": 2000, "min_span_days": 3285}
        replay = ValuationReplay(path, "000300", settings)
        latest = date.fromisoformat(str(replay.dates[-1]))
        for as_of in (latest - timedelta(days=270), latest - timedelta(days=90),
                      latest + timedelta(days=1)):
            with self.subTest(as_of=as_of):
                self.assertEqual(replay.at(as_of), valuation_metrics(path, "000300", as_of, settings))


if __name__ == "__main__":
    unittest.main()
