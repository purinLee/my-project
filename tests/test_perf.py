import json
import subprocess
import sys
from itertools import combinations
from pathlib import Path

import pandas as pd
import pytest

from catalog_intel import perf, quality
from catalog_intel.config import get_db_path
from catalog_intel.db import connect
from catalog_intel.seed import SeedStats, seed_database, trim_products
from tests.conftest import AS_OF

ROOT = Path(__file__).resolve().parent.parent


def test_perf_tooling_refuses_the_demo_database(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="separate database"):
        perf.assert_not_demo_db(get_db_path())
    with pytest.raises(ValueError):
        perf.assert_not_demo_db(tmp_path / "catalog.db")
    with pytest.raises(ValueError):
        perf.generate_perf_database(tmp_path / "catalog.db", 1200, 100)
    assert perf.assert_not_demo_db(tmp_path / "perf_x.db").name == "perf_x.db"
    assert perf.default_perf_db(100000).parent.name == "perf"


def test_generate_perf_database_exact_size_ground_truth_and_isolation(tmp_path):
    demo = tmp_path / "demo.db"
    seed_database(demo, as_of=AS_OF)
    demo_products = sqlite_count(demo, "products")
    path = tmp_path / "perf_small.db"
    stats, seconds = perf.generate_perf_database(path, n_products=1500, n_requests=300)
    assert stats.products == 1500 and stats.requests == 300 and seconds > 0
    assert sqlite_count(path, "products") == 1500 and sqlite_count(path, "merchant_requests") == 300
    with connect(path, readonly=True) as c:
        kinds = dict(c.execute("SELECT kind, COUNT(*) FROM perf_ground_truth GROUP BY kind").fetchall())
        assert set(kinds) == {"duplicate", "price_anomaly", "missing_image"}
        # every ground-truth SKU still exists after trimming
        orphans = c.execute("SELECT COUNT(*) FROM perf_ground_truth g LEFT JOIN products p ON p.merchant_id = g.merchant_id AND p.sku = g.sku WHERE p.sku IS NULL").fetchone()[0]
        assert orphans == 0
        recall = quality.run_audit(c).issues
        found = {(m, s) for m, s in zip(recall["merchant_id"], recall["sku"])}
        dup_truth = {(m, s) for m, s in c.execute("SELECT merchant_id, sku FROM perf_ground_truth WHERE kind='duplicate'")}
        assert len(dup_truth & found) / len(dup_truth) >= 0.95
    assert sqlite_count(demo, "products") == demo_products  # demo database untouched


def sqlite_count(path, table) -> int:
    with connect(path, readonly=True) as c:
        return c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_trim_products_keeps_ground_truth_and_rejects_undersized():
    rows = [{"merchant_id": "M", "sku": f"S{i}"} for i in range(10)]
    stats = SeedStats(seed=1, as_of="x")
    stats.injected_price_anomaly_skus = {("M", "S0"), ("M", "S1")}
    stats.injected_duplicate_skus = {("M", "S9-D1")}
    rows.append({"merchant_id": "M", "sku": "S9-D1"})
    out = trim_products(rows, 5, stats, seed=3)
    skus = {r["sku"] for r in out}
    assert len(out) == 5 and {"S0", "S1", "S9", "S9-D1"} <= skus
    assert trim_products(rows, len(rows), stats, 3) == rows
    with pytest.raises(ValueError):
        trim_products(rows, 99, stats, 3)


def test_default_seed_output_unchanged_by_size_parameters(tmp_path):
    a, b = tmp_path / "a.db", tmp_path / "b.db"
    sa = seed_database(a, as_of=AS_OF)
    sb = seed_database(b, as_of=AS_OF, per_merchant=150, n_requests=700, n_products=None)
    assert (sa.products, sa.requests, sa.events) == (sb.products, sb.requests, sb.events)


def _naive_pairs(df: pd.DataFrame, threshold: float) -> set[tuple[int, int]]:
    """Reference implementation: compare every pair within (merchant, brand, category) with the same similarity function."""
    work = df.assign(norm=df["title"].map(quality.normalize_title))
    out = set()
    for _k, grp in work.groupby(["merchant_id", work["brand"].fillna(""), work["category"].fillna("")]):
        for a, b in combinations(grp.itertuples(), 2):
            if quality._similarity(a.norm, b.norm) >= threshold:
                out.add(tuple(sorted((a.product_id, b.product_id))))
    return out


def test_blocked_duplicate_search_equals_exhaustive_search(conn):
    products = quality.load_products(conn)
    fast = quality.find_duplicate_pairs(products)
    got = {tuple(sorted(p)) for p in zip(fast["product_id_a"], fast["product_id_b"])}
    assert got == _naive_pairs(products, quality.NEAR_DUPLICATE_THRESHOLD)
    assert len(got) >= 50


def test_different_words_are_not_near_duplicates_but_typos_are():
    assert quality._similarity("auralis pro 794 wireless headphones black", "auralis max 794 wireless headphones black") == 0.0
    assert quality._similarity("auralis pro 794 wireless headphones black", "auralis pro 794 wirelss headphones black") >= 0.9
    assert quality._similarity("a b 1 c d e f", "a b 1") == 0.0  # more than one word missing


def test_benchmark_script_end_to_end_small(tmp_path):
    out = tmp_path / "out"
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "benchmark.py"), "--products", "1300", "--requests", "300", "--label", "ci",
         "--db", str(tmp_path / "perf_ci.db"), "--out-dir", str(out)],
        capture_output=True, text=True, timeout=300, cwd=ROOT,
    )
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    data = json.loads((out / "benchmark_ci_1300.json").read_text(encoding="utf-8"))
    assert data["rows"]["products"] == 1300 and data["rows"]["merchant_requests"] == 300
    assert data["cross_checks"]["missing_images"]["match"] and data["cross_checks"]["reconcile_all_match"]
    for step in ("audit_run_audit_total", "export_xlsx_full_catalog", "upload_import_15k_rows"):
        assert data["steps"][step]["seconds"] >= 0 and data["steps"][step]["rss_peak_mb"] > 0
    assert data["detector_vs_ground_truth"]["duplicate"]["recall"] >= 0.9
    assert (out / "benchmark_ci_1300.md").read_text(encoding="utf-8").startswith("# Benchmark `ci`")


def test_benchmark_script_refuses_demo_db(tmp_path):
    proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "benchmark.py"), "--db", str(tmp_path / "catalog.db"), "--out-dir", str(tmp_path)],
                          capture_output=True, text=True, timeout=120, cwd=ROOT)
    assert proc.returncode != 0 and "separate database" in proc.stderr
