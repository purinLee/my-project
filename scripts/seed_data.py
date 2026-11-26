"""CLI: (re)generate the synthetic demo database.

Examples:
    python scripts/seed_data.py                       # default seed, as-of now
    python scripts/seed_data.py --seed 7 --as-of "2026-01-15 12:00:00" --db data/test.db
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from catalog_intel.config import DATE_FMT, DEFAULT_SEED, DEMO_DATA_NOTICE, get_db_path  # noqa: E402
from catalog_intel.seed import seed_database  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description="Generate SYNTHETIC demo data for Merchant Catalog Intelligence.")
    p.add_argument("--seed", type=int, default=DEFAULT_SEED, help="random seed (default: %(default)s)")
    p.add_argument("--as-of", type=str, default=None, help=f"reference time, format '{DATE_FMT}' (default: now, UTC)")
    p.add_argument("--db", type=str, default=None, help="SQLite path (default: data/catalog.db)")
    args = p.parse_args()
    try:
        as_of = datetime.strptime(args.as_of, DATE_FMT) if args.as_of else None
    except ValueError:
        print(f"Invalid --as-of value; expected format '{DATE_FMT}'", file=sys.stderr)
        return 2
    db = args.db or get_db_path()
    stats = seed_database(db, seed=args.seed, as_of=as_of)
    print(DEMO_DATA_NOTICE)
    print(f"Database: {db}\nSeed: {stats.seed}  As-of: {stats.as_of}")
    print(f"Merchants: {stats.merchants}  Products: {stats.products}  Requests: {stats.requests}  Events: {stats.events}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
