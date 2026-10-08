import io
import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from scripts import update_csi300_valuation as importer
from etf_data import valuation_metrics


TODAY = date(2026, 10, 5)
SETTINGS = {"lookback_years": 10, "min_samples": 2, "min_span_days": 1, "max_age_days": 7}


def payload(rows):
    return {"code": "200", "msg": "Success", "success": True,
            "data": [{"tradeDate": day, "indexCode": code, "peg": pe} for day, code, pe in rows]}


class CSI300ValuationTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.output = Path(self.folder.name) / "etf_valuations" / "000300.csv"

    def mock_http(self, response):
        return patch.object(importer, "urlopen", return_value=io.BytesIO(json.dumps(response).encode()))

    def test_imports_official_pe_with_next_day_availability_and_filters_weekend_boundary(self):
        response = payload([("20260926", "000300", 12.9),
                            ("20260928", "000300", 13.0),
                            ("20260930", "000300", 13.15)])
        with self.mock_http(response) as request:
            summary = importer.update_valuation(self.output, TODAY, SETTINGS)
        self.assertIn("indexCode=000300", request.call_args.args[0])
        self.assertEqual(summary["samples"], 2)
        self.assertEqual(self.output.read_text().splitlines(), [
            "date,available_date,index_code,pe_ttm",
            "2026-09-28,2026-09-29,000300,13.0",
            "2026-09-30,2026-10-01,000300,13.15",
        ])
        result = valuation_metrics(self.output, "000300", TODAY - timedelta(days=1), SETTINGS)
        self.assertEqual(result["pe_ttm"], 13.15)

    def test_rejects_bad_api_and_history_values(self):
        valid = [("20260928", "000300", 13.0), ("20260930", "000300", 13.15)]
        bad_payloads = [
            {**payload(valid), "success": False},
            {**payload(valid), "code": "500"},
            {**payload(valid), "data": []},
            payload([valid[0], ("20260930", "000852", 13.15)]),
            payload([valid[0], ("20260928", "000300", 13.15)]),
            payload([valid[0], ("20260930", "000300", 0)]),
            payload([valid[0], ("20260930", "000300", "NaN")]),
            payload([valid[0], ("20261330", "000300", 13.15)]),
            payload([valid[0], ("20261003", "000300", 13.15),
                     ("20261004", "000300", 13.15)]),
            payload([valid[0], ("20261001", "000300", 13.15), valid[1]]),
        ]
        for response in bad_payloads:
            with self.subTest(response=response), self.mock_http(response):
                with self.assertRaises(ValueError):
                    importer.update_valuation(self.output, TODAY, SETTINGS)
                self.assertFalse(self.output.exists())

    def test_invalid_refresh_preserves_existing_file(self):
        self.output.parent.mkdir()
        self.output.write_text("prior validated history\n")
        with self.mock_http(payload([("20260930", "000300", 13.15)])):
            with self.assertRaisesRegex(ValueError, "samples"):
                importer.update_valuation(self.output, TODAY, SETTINGS)
        self.assertEqual(self.output.read_text(), "prior validated history\n")

    def test_retains_complete_available_history_for_point_in_time_replay(self):
        response = payload([("20120904", "000300", 9.8),
                            ("20151005", "000300", 10.1),
                            ("20260928", "000300", 13.0),
                            ("20260930", "000300", 13.15)])
        with self.mock_http(response) as request:
            summary = importer.update_valuation(self.output, TODAY, SETTINGS)
        self.assertIn("startDate=20120904", request.call_args.args[0])
        self.assertEqual(summary["samples"], 2)
        lines = self.output.read_text().splitlines()
        self.assertEqual(lines[1], "2012-09-04,2012-09-05,000300,9.8")
        self.assertIn("2015-10-05,2015-10-06,000300,10.1", lines)

    def test_stale_or_short_history_preserves_existing_file(self):
        self.output.parent.mkdir()
        self.output.write_text("prior validated history\n")
        response = payload([("20260928", "000300", 13.0),
                            ("20260930", "000300", 13.15)])
        for settings, message in [({**SETTINGS, "max_age_days": 1}, "stale"),
                                  ({**SETTINGS, "min_span_days": 10}, "span")]:
            with self.subTest(message=message), self.mock_http(response):
                with self.assertRaisesRegex(ValueError, message):
                    importer.update_valuation(self.output, TODAY, settings)
                self.assertEqual(self.output.read_text(), "prior validated history\n")

    def test_default_constraints_accept_full_ten_year_history(self):
        as_of = TODAY - timedelta(days=1)
        start = importer.years_ago(as_of, 10)
        rows = []
        day = start
        while day <= date(2026, 9, 30):
            if day.weekday() < 5:
                rows.append((day.strftime("%Y%m%d"), "000300", 13.15))
            day += timedelta(days=1)
        settings = {"lookback_years": 10, "min_samples": 2000,
                    "min_span_days": 3285, "max_age_days": 7}
        with self.mock_http(payload(rows)):
            summary = importer.update_valuation(self.output, TODAY, settings)
        self.assertGreaterEqual(summary["samples"], 2000)
        self.assertGreaterEqual((summary["latest_date"] - summary["start_date"]).days, 3285)
        self.assertEqual(valuation_metrics(self.output, "000300", as_of, settings)["samples"],
                         summary["samples"])


if __name__ == "__main__":
    unittest.main()
