"""Large synthetic dataset support for performance testing.

Everything here writes ONLY to a dedicated performance database (default: data/perf/perf_<n>.db, git-ignored).
The demo database used by the app (data/catalog.db) is never touched; `assert_not_demo_db` enforces that.
"""
from __future__ import annotations

import math
import time
from datetime import datetime
from pathlib import Path

from .config import ROOT, get_db_path
from .db import connect
from .seed import MERCHANTS, SeedStats, seed_database

PERF_DIR = ROOT / "data" / "perf"
PERF_AS_OF = datetime(2026, 11, 26, 12, 0, 0)  # fixed reference time: results are reproducible
PERF_SEED = 424242


def default_perf_db(n_products: int) -> Path:
    return PERF_DIR / f"perf_{n_products}.db"


def assert_not_demo_db(path: str | Path) -> Path:
    """Refuse to run performance tooling against the demo database."""
    p = Path(path).resolve()
    if p == get_db_path().resolve() or p.name == "catalog.db":
        raise ValueError(f"Refusing to use '{p}': performance data must live in a separate database file (default: {PERF_DIR}).")
    return p


def generate_perf_database(path: str | Path, n_products: int = 100_000, n_requests: int = 50_000, seed: int = PERF_SEED) -> tuple[SeedStats, float]:
    """Create (overwrite) a performance database with exactly `n_products` synthetic products.

    Also stores the injected-defect ground truth in table `perf_ground_truth` (perf database only) so detector
    recall/precision can be measured at scale. Returns (stats, generation_seconds).
    """
    p = assert_not_demo_db(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        p.unlink()
    per_merchant = math.ceil(n_products / len(MERCHANTS))
    t0 = time.perf_counter()
    for _attempt in range(4):
        try:
            stats = seed_database(p, seed=seed, as_of=PERF_AS_OF, per_merchant=per_merchant, n_requests=n_requests, n_products=n_products)
            break
        except ValueError:  # generated fewer rows than requested (title collisions): add headroom and retry
            per_merchant = math.ceil(per_merchant * 1.05)
    else:
        raise RuntimeError("Could not generate the requested number of products.")
    with connect(p) as conn:
        conn.execute("CREATE TABLE perf_ground_truth (kind TEXT NOT NULL, merchant_id TEXT NOT NULL, sku TEXT NOT NULL)")
        rows = (
            [("duplicate", m, s) for m, s in stats.injected_duplicate_skus]
            + [("price_anomaly", m, s) for m, s in stats.injected_price_anomaly_skus]
            + [("missing_image", m, s) for m, s in stats.injected_missing_image_skus]
        )
        conn.executemany("INSERT INTO perf_ground_truth VALUES (?,?,?)", rows)
        conn.execute("CREATE TABLE perf_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.executemany("INSERT INTO perf_meta VALUES (?,?)", [("n_products", str(stats.products)), ("n_requests", str(stats.requests)), ("seed", str(seed)), ("as_of", stats.as_of)])
    return stats, time.perf_counter() - t0
