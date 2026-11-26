"""Server-side render time of each Streamlit page on a large performance database (Streamlit AppTest).

    python scripts/benchmark_ui.py --db data/perf/perf_100000.db

Measures script execution time on the server (data loading, audit, SQL, chart/table construction). It does NOT include
browser rendering or network transfer. The database is COPIED to a temp file first, so the perf DB is never modified,
and the demo database is never touched.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

import psutil

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from catalog_intel.config import ROOT  # noqa: E402
from catalog_intel.perf import assert_not_demo_db  # noqa: E402

PAGES = {"Executive Overview": "p_overview", "Catalog Management": "p_catalog", "Catalog Quality Audit": "p_audit",
         "Merchant Requests": "p_requests", "SQL Operations Analytics": "p_sql", "AI Operations Assistant": "p_assistant"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--out", default=str(ROOT / "docs" / "performance" / "ui_render_times.json"))
    args = ap.parse_args()
    src = assert_not_demo_db(args.db)
    tmp = Path(tempfile.mkdtemp()) / "perf_copy.db"
    shutil.copy(src, tmp)
    os.environ["CATALOG_DB_PATH"] = str(tmp)
    from streamlit.testing.v1 import AppTest

    proc = psutil.Process()
    rows = {}
    for title, module in PAGES.items():
        script = f"from ui import common, {module}\ncommon.inject_css()\ncommon.init_demo()\n{module}.render()\n"
        timings = []
        for _ in range(2):  # run 1 = cold (fills caches), run 2 = warm (cache_data hit)
            t0 = time.perf_counter()
            at = AppTest.from_string(script, default_timeout=600).run()
            timings.append(round(time.perf_counter() - t0, 2))
            if at.exception:
                timings[-1] = f"EXCEPTION: {at.exception[0].value}"
                break
        rows[title] = {"cold_seconds": timings[0], "warm_seconds": timings[1] if len(timings) > 1 else None, "rss_mb_after": round(proc.memory_info().rss / 1024**2, 1)}
        print(f"{title:<28} cold {timings[0]}  warm {rows[title]['warm_seconds']}  RSS {rows[title]['rss_mb_after']} MB", flush=True)
    out = {"kind": "MEASURED server-side script time (AppTest), excludes browser rendering", "source_db": src.name, "pages": rows}
    Path(args.out).write_text(json.dumps(out, indent=2), encoding="utf-8")
    shutil.rmtree(tmp.parent, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
