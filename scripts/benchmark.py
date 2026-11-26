"""Performance benchmark on a large SYNTHETIC catalog, isolated from the demo database.

    python scripts/benchmark.py --products 100000 --requests 50000 --label optimized

What is measured (wall-clock seconds via time.perf_counter; memory = process RSS sampled every 20 ms with psutil):
  1. data generation, 2. catalog quality audit (each detector + full run_audit), 3. detector recall/precision vs
  injected ground truth, 4. SQL queries (analytics registry + product-level queries, 5 runs each),
  5. SQL-vs-audit cross-checks, 6. CSV/XLSX upload path (20,000 rows) and 100,000-row exports.

Results are written to docs/performance/benchmark_<label>_<products>.json and .md. The demo database is never used:
the data lives in data/perf/ (git-ignored) and `assert_not_demo_db` refuses the demo path.
"""
from __future__ import annotations

import argparse
import json
import platform
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import psutil

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from catalog_intel import analytics, quality  # noqa: E402
from catalog_intel.config import ROOT  # noqa: E402
from catalog_intel.db import connect, run_readonly_query  # noqa: E402
from catalog_intel.excel import to_xlsx_bytes  # noqa: E402
from catalog_intel.perf import PERF_AS_OF, assert_not_demo_db, default_perf_db, generate_perf_database  # noqa: E402
from catalog_intel.reports import to_csv_bytes  # noqa: E402
from catalog_intel.seed import seed_database  # noqa: E402
from catalog_intel.validation import import_products, validate_upload_bytes  # noqa: E402

MB = 1024 * 1024
PROC = psutil.Process()

PRODUCT_SQL = {
    "category_counts": "SELECT category, COUNT(*) AS n, ROUND(AVG(price), 2) AS avg_price FROM products WHERE status != 'archived' GROUP BY category",
    "merchant_category_avg_price": "SELECT merchant_id, category, ROUND(AVG(price), 2) AS avg_price, COUNT(*) AS n FROM products GROUP BY merchant_id, category",
    "top_100_by_price": "SELECT sku, title, price FROM products ORDER BY price DESC LIMIT 100",
    "title_search_like": "SELECT COUNT(*) FROM products WHERE title LIKE '%Pro 5%'",
    "sku_lookup_unique_index": "SELECT * FROM products WHERE merchant_id = :m AND sku = :s",
    "window_top10_per_category": "SELECT COUNT(*) FROM (SELECT ROW_NUMBER() OVER (PARTITION BY category ORDER BY price DESC) AS rn FROM products) WHERE rn <= 10",
    "join_tier_summary": "SELECT m.tier, COUNT(*) AS products, ROUND(AVG(p.price), 2) AS avg_price FROM products p JOIN merchants m USING (merchant_id) GROUP BY m.tier",
    "missing_image_count": (
        "SELECT COUNT(*) FROM products WHERE status != 'archived' AND (image_url IS NULL OR TRIM(image_url) = '' OR image_url NOT LIKE 'http%://%' "
        "OR LOWER(image_url) LIKE '%placeholder%' OR LOWER(image_url) LIKE '%no-image%')"
    ),
    "exact_title_duplicates": "SELECT COUNT(*) FROM (SELECT merchant_id, LOWER(TRIM(title)) AS t FROM products GROUP BY 1, 2 HAVING COUNT(*) > 1)",
}


class Meter:
    """Wall time plus sampled process RSS (peak) for a block of work."""

    def __init__(self) -> None:
        self.results: dict[str, dict] = {}

    @contextmanager
    def measure(self, name: str):
        stop, peak = threading.Event(), [PROC.memory_info().rss]

        def sample() -> None:
            while not stop.wait(0.02):
                peak[0] = max(peak[0], PROC.memory_info().rss)

        before = PROC.memory_info().rss
        th = threading.Thread(target=sample, daemon=True)
        th.start()
        t0 = time.perf_counter()
        try:
            yield
        finally:
            seconds = time.perf_counter() - t0
            stop.set()
            th.join()
            after = PROC.memory_info().rss
            peak[0] = max(peak[0], after)
            self.results[name] = {
                "seconds": round(seconds, 3), "rss_before_mb": round(before / MB, 1), "rss_peak_mb": round(peak[0] / MB, 1),
                "rss_after_mb": round(after / MB, 1), "rss_peak_growth_mb": round((peak[0] - before) / MB, 1),
            }
            print(f"  {name:<34} {seconds:8.2f}s  peak RSS {peak[0] / MB:8.1f} MB (+{(peak[0] - before) / MB:.1f})", flush=True)


def environment() -> dict:
    try:
        commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, cwd=ROOT, timeout=10).stdout.strip()
    except Exception:  # noqa: BLE001
        commit = ""
    return {
        "measured_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), "git_commit": commit,
        "platform": platform.platform(), "processor": platform.processor(), "cpu_count_logical": psutil.cpu_count(),
        "ram_total_gb": round(psutil.virtual_memory().total / 1024**3, 1), "python": platform.python_version(),
        "pandas": pd.__version__, "numpy": np.__version__, "sqlite": sqlite3.sqlite_version,
    }


def time_sql(conn, sql: str, params: dict | None = None, runs: int = 5) -> dict:
    times, rows = [], 0
    for _ in range(runs):
        t0 = time.perf_counter()
        rows = len(conn.execute(sql, params or {}).fetchall())
        times.append((time.perf_counter() - t0) * 1000)
    return {"median_ms": round(statistics.median(times), 2), "min_ms": round(min(times), 2), "max_ms": round(max(times), 2), "rows": rows}


def truth_scores(conn, issues: pd.DataFrame) -> dict:
    out = {}
    kinds = {"duplicate": ["Duplicate Listing"], "price_anomaly": ["Price Anomaly", "Invalid Price"], "missing_image": ["Missing Image"]}
    for kind, types in kinds.items():
        truth = {(m, s) for m, s in conn.execute("SELECT merchant_id, sku FROM perf_ground_truth WHERE kind = ?", (kind,))}
        found_df = issues[issues["issue_type"].isin(types)]
        found = set(zip(found_df["merchant_id"], found_df["sku"]))
        tp = len(truth & found)
        out[kind] = {"injected": len(truth), "flagged": len(found), "true_positives": tp,
                     "recall": round(tp / len(truth), 3) if truth else None, "precision": round(tp / len(found), 3) if found else None}
    return out


UPLOAD_ROWS = 15_000  # 20,000 rows of this synthetic shape exceed the 5 MB upload size limit (recorded below)


def upload_path(products: pd.DataFrame, meter: Meter) -> dict:
    """Benchmark the real upload path (CSV and XLSX), importing into a scratch DB (never the perf DB)."""
    cols = ["sku", "title", "brand", "category", "price", "currency", "image_url", "description", "status"]
    full20k = products[cols].head(20_000).copy()
    full20k["sku"] = [f"BENCH-{i:06d}" for i in range(len(full20k))]
    csv_20k_bytes = len(to_csv_bytes(full20k))
    sample = full20k.head(UPLOAD_ROWS).copy()
    csv_bytes = to_csv_bytes(sample)
    with meter.measure("upload_build_xlsx_15k"):
        xlsx_bytes = to_xlsx_bytes({"Catalog": sample})
    info = {"rows": len(sample), "csv_bytes": len(csv_bytes), "xlsx_bytes": len(xlsx_bytes), "csv_bytes_at_20000_rows": csv_20k_bytes,
            "csv_20000_rows_exceeds_5mb_limit": csv_20k_bytes > 5 * 1024 * 1024}
    with meter.measure("upload_validate_csv_15k"):
        rep_csv = validate_upload_bytes(csv_bytes, "bench.csv")
    with meter.measure("upload_validate_xlsx_15k"):
        rep_xlsx = validate_upload_bytes(xlsx_bytes, "bench.xlsx")
    info.update(valid_csv=len(rep_csv.valid), valid_xlsx=len(rep_xlsx.valid), rejected_csv=len(rep_csv.rejected), rejected_xlsx=len(rep_xlsx.rejected))
    with tempfile.TemporaryDirectory() as tmp:
        scratch = Path(tmp) / "scratch.db"
        seed_database(scratch, per_merchant=1, n_requests=1)
        with connect(scratch) as conn, meter.measure("upload_import_15k_rows"):
            res = import_products(conn, "M-001", rep_xlsx.valid, "bench.xlsx", len(sample))
        info["import_result"] = res
    return info


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--products", type=int, default=100_000)
    ap.add_argument("--requests", type=int, default=50_000)
    ap.add_argument("--label", default="optimized", help="used in output file names, e.g. baseline / optimized")
    ap.add_argument("--db", default=None, help="performance database path (default data/perf/perf_<n>.db)")
    ap.add_argument("--reuse", action="store_true", help="reuse an existing performance DB instead of regenerating")
    ap.add_argument("--out-dir", default=str(ROOT / "docs" / "performance"))
    args = ap.parse_args()

    db_path = assert_not_demo_db(args.db or default_perf_db(args.products))
    meter = Meter()
    env = environment()
    print(f"Benchmark '{args.label}': {args.products:,} products, {args.requests:,} requests -> {db_path}")
    print(f"Python {env['python']}, pandas {env['pandas']}, SQLite {env['sqlite']}, {env['cpu_count_logical']} logical CPUs, {env['ram_total_gb']} GB RAM")

    if not (args.reuse and db_path.exists()):
        with meter.measure("generate_database"):
            stats, _ = generate_perf_database(db_path, args.products, args.requests)
        print(f"  generated {stats.products:,} products, {stats.requests:,} requests, {stats.events:,} events")
    result: dict = {"label": args.label, "kind": "MEASURED on synthetic data (not simulated timings)", "environment": env, "db_file_mb": round(db_path.stat().st_size / MB, 1)}

    with connect(db_path, readonly=True) as conn:
        result["rows"] = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("products", "merchants", "merchant_requests", "request_events")}
        print("Rows:", result["rows"])

        print("Quality audit")
        with meter.measure("audit_load_products"):
            products = quality.load_products(conn)
        with meter.measure("audit_find_duplicate_pairs"):
            pairs = quality.find_duplicate_pairs(products)
        with meter.measure("audit_detect_duplicates"):
            quality.detect_duplicates(products, pairs)
        with meter.measure("audit_detect_price_anomalies"):
            quality.detect_price_anomalies(products)
        with meter.measure("audit_detect_missing_images"):
            quality.detect_missing_images(products)
        with meter.measure("audit_detect_incomplete_metadata"):
            quality.detect_incomplete_metadata(products)
        with meter.measure("audit_run_audit_total"):
            audit = quality.run_audit(conn)
        result["audit_summary"] = {k: v for k, v in audit.summary.items()}
        result["detector_vs_ground_truth"] = truth_scores(conn, audit.issues)
        print("  recall/precision:", {k: (v["recall"], v["precision"]) for k, v in result["detector_vs_ground_truth"].items()})

        print("SQL")
        sql_results: dict[str, dict] = {}
        now = PERF_AS_OF
        with meter.measure("sql_phase_total"):
            for key in analytics.QUERIES:
                q = analytics.QUERIES[key]
                params = {"now": now.strftime("%Y-%m-%d %H:%M:%S")} if ":now" in q.sql else {}
                sql_results[f"analytics:{key}"] = time_sql(conn, q.sql, params)
            m, s = conn.execute("SELECT merchant_id, sku FROM products ORDER BY product_id LIMIT 1 OFFSET (SELECT COUNT(*) / 2 FROM products)").fetchone()
            for key, sql in PRODUCT_SQL.items():
                sql_results[f"products:{key}"] = time_sql(conn, sql, {"m": m, "s": s} if ":m" in sql else None)
            t0 = time.perf_counter()
            kpis = analytics.kpi_summary(conn, now)
            sql_results["kpi_summary"] = {"median_ms": round((time.perf_counter() - t0) * 1000, 2), "runs": 1}
            t0 = time.perf_counter()
            rec = analytics.reconcile(conn, now)
            sql_results["reconcile_sql_vs_pandas"] = {"median_ms": round((time.perf_counter() - t0) * 1000, 2), "runs": 1, "all_match": bool(rec["match"].all())}
            plan = [r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + PRODUCT_SQL["sku_lookup_unique_index"], {"m": m, "s": s})]
            sql_results["sku_lookup_plan"] = {"plan": plan}
        for k, v in sql_results.items():
            if "median_ms" in v:
                print(f"  {k:<44} median {v['median_ms']:>9.2f} ms")
        result["sql"] = sql_results
        result["kpis"] = kpis

        print("Cross-checks (SQL vs audit)")
        sql_missing = conn.execute(PRODUCT_SQL["missing_image_count"]).fetchone()[0]
        audit_missing = int((audit.issues["issue_type"] == "Missing Image").sum())
        sql_invalid = conn.execute("SELECT COUNT(*) FROM products WHERE status != 'archived' AND (price IS NULL OR price <= 0)").fetchone()[0]
        audit_invalid = int((audit.issues["issue_type"] == "Invalid Price").sum())
        result["cross_checks"] = {"missing_images": {"sql": sql_missing, "audit": audit_missing, "match": sql_missing == audit_missing},
                                  "invalid_prices": {"sql": sql_invalid, "audit": audit_invalid, "match": sql_invalid == audit_invalid},
                                  "reconcile_all_match": bool(rec["match"].all())}
        print(" ", result["cross_checks"])

        print("Upload path / exports")
        result["upload_20k"] = upload_path(products, meter)
        with meter.measure("export_csv_full_catalog"):
            csv_all = to_csv_bytes(products)
        with meter.measure("export_xlsx_full_catalog"):
            xlsx_all = to_xlsx_bytes({"Catalog": products})
        result["exports"] = {"rows": len(products), "csv_mb": round(len(csv_all) / MB, 1), "xlsx_mb": round(len(xlsx_all) / MB, 1)}

    result["steps"] = meter.results
    result["process_peak_rss_mb"] = round(max(v["rss_peak_mb"] for v in meter.results.values()), 1)
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stem = f"benchmark_{args.label}_{args.products}"
    (out / f"{stem}.json").write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    (out / f"{stem}.md").write_text(render_markdown(result), encoding="utf-8")
    print(f"\nWrote {out / (stem + '.json')} and .md  (process peak RSS {result['process_peak_rss_mb']} MB)")
    return 0


def render_markdown(r: dict) -> str:
    e = r["environment"]
    lines = [
        f"# Benchmark `{r['label']}` - {r['rows']['products']:,} products / {r['rows']['merchant_requests']:,} requests",
        "",
        f"**{r['kind']}.** Measured {e['measured_at_utc']} UTC at commit `{e['git_commit']}` on {e['platform']} "
        f"({e['cpu_count_logical']} logical CPUs, {e['ram_total_gb']} GB RAM), Python {e['python']}, pandas {e['pandas']}, SQLite {e['sqlite']}. "
        f"Database file {r['db_file_mb']} MB. Memory = process RSS sampled every 20 ms.",
        "", "## Steps (wall time and memory)", "", "| Step | Seconds | Peak RSS MB | Peak growth MB |", "| --- | ---: | ---: | ---: |",
    ]
    lines += [f"| {k} | {v['seconds']} | {v['rss_peak_mb']} | {v['rss_peak_growth_mb']} |" for k, v in r["steps"].items()]
    lines += ["", "## Detector accuracy vs injected ground truth", "", "| Detector | Injected | Flagged | Recall | Precision |", "| --- | ---: | ---: | ---: | ---: |"]
    lines += [f"| {k} | {v['injected']} | {v['flagged']} | {v['recall']} | {v['precision']} |" for k, v in r["detector_vs_ground_truth"].items()]
    lines += ["", "## SQL (5 runs each)", "", "| Query | Median ms | Min ms | Max ms | Rows |", "| --- | ---: | ---: | ---: | ---: |"]
    for k, v in r["sql"].items():
        if "min_ms" in v:
            lines.append(f"| {k} | {v['median_ms']} | {v['min_ms']} | {v['max_ms']} | {v['rows']} |")
        elif "median_ms" in v:
            lines.append(f"| {k} (single run) | {v['median_ms']} | | | |")
    lines += ["", f"Index plan for the SKU lookup: `{r['sql']['sku_lookup_plan']['plan']}`", "", "## Cross-checks", "", f"`{json.dumps(r['cross_checks'])}`", ""]
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
