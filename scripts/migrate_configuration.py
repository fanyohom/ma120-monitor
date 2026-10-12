"""One-time import of watchlists, positions and ETF configuration into SQLite."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config_store import ConfigurationStore
from market_store import DEFAULT_DATABASE, BASE_DIR


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--root", type=Path, default=BASE_DIR)
    args = parser.parse_args()
    store = ConfigurationStore(args.database, args.root)
    store.initialize()
    data = store.summary()
    print(json.dumps({"database": str(args.database), "watchlist": len(data["watchlist"]),
                      "portfolio": len(data["portfolio"]), "etf_plans": len(data["etf"]["plans"]),
                      "revision": data["revision"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
