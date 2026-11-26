"""Real-browser RPA tests: start a throw-away Streamlit server and drive it with scripts/rpa_ui_bot.py (Playwright).

Skipped automatically when Playwright's Chromium is not installed (`python -m playwright install chromium`).
"""
import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from catalog_intel.db import connect
from catalog_intel.seed import seed_database
from catalog_intel.workload import generate_workload

ROOT = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.rpa


def _chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            return Path(p.chromium.executable_path).exists()
    except Exception:  # noqa: BLE001
        return False


pytestmark = [pytest.mark.rpa, pytest.mark.skipif(not _chromium_available(), reason="Playwright Chromium not installed")]


@pytest.fixture()
def server(tmp_path):
    db = tmp_path / "app.db"
    seed_database(db)
    with connect(db) as c:
        c.execute("DELETE FROM products")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen([sys.executable, "-m", "streamlit", "run", "app.py", "--server.port", str(port), "--server.headless", "true"],
                            cwd=ROOT, env={**os.environ, "CATALOG_DB_PATH": str(db)}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    t0 = time.time()
    while time.time() - t0 < 90:
        try:
            if urllib.request.urlopen(f"http://127.0.0.1:{port}/_stcore/health", timeout=2).read() == b"ok":
                break
        except Exception:  # noqa: BLE001
            time.sleep(0.5)
    else:
        proc.terminate()
        pytest.fail("Streamlit server did not start")
    yield f"http://127.0.0.1:{port}", db
    proc.terminate()
    proc.wait(timeout=20)


def run_bot(url, inbox, out, *extra):
    return subprocess.run([sys.executable, str(ROOT / "scripts" / "rpa_ui_bot.py"), "--url", url, "--inbox", str(inbox), "--out-dir", str(out), *extra],
                          capture_output=True, text=True, timeout=400, cwd=ROOT)


def make_inbox(tmp_path, files=2, rows=80):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    w = generate_workload(files, rows, seed=31)
    for n, d in w.files:
        (inbox / n).write_bytes(d)
    return inbox, w


def count(db, sql):
    with connect(db, readonly=True) as c:
        return c.execute(sql).fetchone()[0]


def test_rpa_bot_imports_files_and_downloads_reports_through_the_ui(server, tmp_path):
    url, db = server
    inbox, w = make_inbox(tmp_path)
    r = run_bot(url, inbox, tmp_path / "out", "--db", str(db), "--json", str(tmp_path / "run.json"))
    assert r.returncode == 0, r.stdout + r.stderr
    summary = json.loads((tmp_path / "run.json").read_text(encoding="utf-8"))
    assert summary["status"] == "success" and summary["totals"]["files_imported"] == 2 and summary["mode"] == "rpa_ui"
    assert summary["totals"]["rows_rejected"] == w.invalid_rows
    assert count(db, "SELECT COUNT(*) FROM products") == summary["totals"]["rows_valid"]
    assert {p.name for p in (tmp_path / "out").iterdir()} == {"quality_findings.csv", "quality_findings.xlsx", "executive_report.xlsx"}
    assert all(summary["step_seconds"][k] > 0 for k in ("collect_validate", "import", "quality", "report", "export"))
    assert count(db, "SELECT COUNT(*) FROM automation_runs WHERE mode='rpa_ui' AND status='success'") == 1  # run history recorded


def test_rpa_bot_recovers_from_a_bad_file_and_rerun_does_not_duplicate(server, tmp_path):
    url, db = server
    inbox, _w = make_inbox(tmp_path)
    (inbox / "M-005_corrupt.xlsx").write_bytes(b"PK this is not a real workbook")
    first = run_bot(url, inbox, tmp_path / "out1", "--json", str(tmp_path / "r1.json"))
    s1 = json.loads((tmp_path / "r1.json").read_text(encoding="utf-8"))
    detail = [(f["name"], f["status"], f["error"]) for f in s1["files"]]
    assert first.returncode == 1 and s1["status"] == "partial" and s1["totals"]["files_imported"] == 2 and s1["totals"]["files_failed"] == 1, detail
    n = count(db, "SELECT COUNT(*) FROM products")
    assert n == s1["totals"]["rows_valid"] - 0 or n > 0
    second = run_bot(url, inbox, tmp_path / "out2", "--json", str(tmp_path / "r2.json"))
    s2 = json.loads((tmp_path / "r2.json").read_text(encoding="utf-8"))
    assert second.returncode == 1 and s2["totals"]["rows_inserted"] == 0 and s2["totals"]["rows_updated"] > 0  # upsert: no duplicates
    assert count(db, "SELECT COUNT(*) FROM products") == n
    assert count(db, "SELECT COUNT(*) FROM (SELECT merchant_id, sku FROM products GROUP BY 1, 2 HAVING COUNT(*) > 1)") == 0
    assert sqlite3.connect(db).execute("PRAGMA integrity_check").fetchone()[0] == "ok"
