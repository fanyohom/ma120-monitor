"""Import existing market caches and valuation CSVs without removing any files."""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from etf_data import load_valuation_history
from market_store import MarketStore, DEFAULT_DATABASE


def migrate(root: Path, database: Path) -> dict:
    store = MarketStore(database)
    counts = {"stock_files": 0, "etf_files": 0, "valuation_series": 0, "skipped": []}
    for path in sorted((root / ".stock_cache").glob("sina_*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            rows = [[r["date"], r["open"], r["close"], r["high"], r["low"], r["volume"]]
                    for r in payload["data"]]
            rows.sort(key=lambda row: row[0])
            instant = datetime.fromtimestamp(path.stat().st_mtime, ZoneInfo("Asia/Shanghai"))
            store.put_history("stock", path.stem.removeprefix("sina_"), {"bars": rows},
                              {"source": "legacy JSON", "migration_file": str(path)}, instant.isoformat())
            counts["stock_files"] += 1
        except (ValueError, KeyError, OSError) as exc:
            counts["skipped"].append({"file": str(path), "reason": str(exc)})
    for path in sorted((root / ".stock_cache" / "etf").glob("*/*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            metadata = {k: v for k, v in payload.items() if k not in ("qfq", "raw")}
            store.put_history("etf", payload["code"], {k: payload[k] for k in ("qfq", "raw")},
                              metadata, payload["fetched_at"])
            store.put_quote("etf", payload["code"], {k: payload[k] for k in ("price", "quote_at", "volume", "source")},
                            payload["fetched_at"])
            counts["etf_files"] += 1
        except (ValueError, KeyError, OSError) as exc:
            counts["skipped"].append({"file": str(path), "reason": str(exc)})
    # CSV data are imported by their shared reader and matched by index_code.
    import pandas as pd
    for path in sorted((root / "etf_valuations").glob("*.csv")):
        try:
            codes = pd.read_csv(path, dtype={"index_code": str})["index_code"].dropna().unique()
            for code in codes:
                load_valuation_history(path, str(code), database_path=database)
                counts["valuation_series"] += 1
        except (ValueError, KeyError, OSError) as exc:
            counts["skipped"].append({"file": str(path), "reason": str(exc)})
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    args = parser.parse_args()
    print(json.dumps(migrate(args.root, args.database), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
