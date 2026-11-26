import io
import json
import sqlite3
import subprocess
import sys
import zipfile
from pathlib import Path

import pandas as pd
import pytest
from openpyxl import load_workbook

from catalog_intel import automation, baseline
from catalog_intel.automation import Source, ensure_sandbox, load_history, merchant_from_filename, run_inbox, run_pipeline
from catalog_intel.db import connect
from catalog_intel.seed import seed_database
from catalog_intel.workload import generate_workload
from tests.conftest import AS_OF

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def wl():
    return generate_workload(3, 150, seed=5)


@pytest.fixture()
def sandbox(tmp_path):
    return ensure_sandbox(tmp_path / "sandbox.db")


def sources(wl):
    return [Source(n, d) for n, d in wl.files]


def count(db, table, where=""):
    with connect(db, readonly=True) as c:
        return c.execute(f"SELECT COUNT(*) FROM {table} {where}").fetchone()[0]


# ---- happy path -----------------------------------------------------------------------------
def test_pipeline_end_to_end_outputs_history_and_counts(wl, sandbox, tmp_path):
    r = run_pipeline(sources(wl), sandbox, out_dir=tmp_path / "out", trigger="test", workload_label="unit")
    assert r.status == "success" and r.error is None and r.run_id == 1
    assert r.totals["files_imported"] == 3 and r.totals["rows_total"] == wl.rows_total
    assert r.totals["rows_rejected"] == wl.invalid_rows  # every injected invalid row is rejected, nothing else
    assert count(sandbox, "products") == r.totals["rows_inserted"] == r.totals["rows_valid"]
    names = sorted(p.name for p in (tmp_path / "out").iterdir())
    assert any(n.endswith(".html") for n in names) and any(n.startswith("executive_report") and n.endswith(".xlsx") for n in names)
    assert any(n.startswith("quality_findings_run") and n.endswith(".csv") for n in names) and not any(n.endswith(".part") for n in names)
    assert set(r.step_seconds) == {"collect", "validate", "import", "quality", "report", "export"} and all(v >= 0 for v in r.step_seconds.values())
    with connect(sandbox, readonly=True) as c:
        h = load_history(c).iloc[0]
    assert (h["mode"], h["trigger_source"], h["status"], h["files_imported"], h["workload_label"]) == ("python", "test", "success", 3, "unit")
    assert json.loads(h["step_seconds"]).keys() == r.step_seconds.keys()


def test_detection_recall_against_injected_ground_truth(wl, sandbox):
    r = run_pipeline(sources(wl), sandbox)
    dup = baseline.score_pairs(wl.dup_pairs, r.flagged_run_scope["duplicates"])
    price = baseline.score_sets(wl.truth["price_anomaly"], r.flagged_run_scope["price_anomalies"])
    img = baseline.score_sets(wl.truth["missing_image"], r.flagged_run_scope["missing_images"])
    assert dup["recall"] >= 0.95 and dup["precision"] >= 0.9
    assert price["recall"] >= 0.9 and price["precision"] >= 0.8
    assert img["recall"] >= 0.95 and img["precision"] >= 0.95


def test_automation_beats_excel_style_baseline_on_omissions(wl, sandbox):
    r = run_pipeline(sources(wl), sandbox)
    raw = wl.combined_raw()
    assert baseline.score_sets(wl.truth["missing_image"], r.flagged_run_scope["missing_images"])["missed"] < baseline.score_sets(wl.truth["missing_image"], baseline.naive_missing_images(raw))["missed"]
    assert baseline.score_sets(wl.truth["price_anomaly"], r.flagged_run_scope["price_anomalies"])["missed"] < baseline.score_sets(wl.truth["price_anomaly"], baseline.naive_price_outliers(raw))["missed"]
    assert r.totals["rows_rejected"] > baseline.naive_invalid_rows(raw)


def test_workload_is_reproducible_and_synthetic():
    a, b = generate_workload(2, 100, seed=9), generate_workload(2, 100, seed=9)
    assert [d for _, d in a.files] == [d for _, d in b.files] and a.injected_counts == b.injected_counts
    assert generate_workload(2, 100, seed=10).files[0][1] != a.files[0][1]
    assert all(n.startswith("M-") for n, _ in a.files)


# ---- idempotency, repeated runs ---------------------------------------------------------------
def test_repeated_runs_do_not_duplicate_data(wl, sandbox):
    first = run_pipeline(sources(wl), sandbox)
    n = count(sandbox, "products")
    for _ in range(3):
        again = run_pipeline(sources(wl), sandbox)
        assert again.status == "success" and again.totals["files_skipped"] == 3 and again.totals["files_imported"] == 0
        assert not again.outputs  # nothing new -> no audit/report churn
    assert count(sandbox, "products") == n == first.totals["rows_inserted"]
    assert count(sandbox, "automation_files") == 3 and count(sandbox, "automation_runs") == 4 and count(sandbox, "upload_log") == 3


def test_changed_file_content_is_reimported_as_update_not_duplicate(wl, sandbox):
    run_pipeline(sources(wl), sandbox)
    n = count(sandbox, "products")
    name, data = wl.files[0]
    csv_name = next(n_ for n_, _ in wl.files if n_.endswith(".csv"))
    data = dict(wl.files)[csv_name] + b"\n"  # whitespace-only change -> different hash, same SKUs
    r = run_pipeline([Source(csv_name, data)], sandbox)
    assert r.totals["files_imported"] == 1 and r.totals["rows_inserted"] == 0 and r.totals["rows_updated"] == r.totals["rows_valid"]
    assert count(sandbox, "products") == n


# ---- failures and recovery ---------------------------------------------------------------------
def test_bad_files_fail_individually_and_good_files_still_import(wl, sandbox):
    good = sources(wl)[0]
    bad = [
        Source("M-002_empty.csv", b""), Source("M-003_nocols.csv", b"a,b\n1,2\n"), Source("M-004_corrupt.xlsx", b"PK not really a zip"),
        Source("no_merchant.csv", good.data), Source("M-005_notes.txt", b"hello"), Source("M-006_allbad.csv", b"sku,title,price\n,x,abc\n"),
    ]
    r = run_pipeline([good, *bad], sandbox)
    status = {o.name: o.status for o in r.files}
    assert r.status == "partial" and status[good.name] == "imported"
    assert all(status[s.name] == "failed" for s in bad)
    assert "Cannot determine merchant" in next(o.error for o in r.files if o.name == "no_merchant.csv")
    assert all(o.error for o in r.files if o.status == "failed")
    assert count(sandbox, "products") == r.totals["rows_inserted"] > 0


def test_default_merchant_routes_unprefixed_files(wl, sandbox):
    r = run_pipeline([Source("weekly_upload.csv", sources(wl)[0].data)], sandbox, default_merchant="M-007")
    assert r.status == "success" and count(sandbox, "products", "WHERE merchant_id='M-007'") > 0
    assert merchant_from_filename("m-010-x.csv") == "M-010" and merchant_from_filename("xM-010.csv") is None and merchant_from_filename("M-0101.csv") is None


def test_all_files_failed_is_a_failed_run_with_no_audit(sandbox):
    r = run_pipeline([Source("M-001_bad.csv", b"x,y\n1,2\n")], sandbox)
    assert r.status == "failed" and not r.outputs and r.quality_seconds == {}
    assert run_pipeline([], sandbox).status == "nothing_to_do"
    assert count(sandbox, "automation_runs", "WHERE status='nothing_to_do'") == 1


def test_crash_mid_import_leaves_no_partial_data_and_rerun_recovers(wl, sandbox):
    srcs = sources(wl)

    def boom(stage, name):
        if stage == "import" and name == srcs[1].name:
            raise RuntimeError("simulated crash")

    r = run_pipeline(srcs, sandbox, fail_hook=boom)
    st = {o.name: o.status for o in r.files}
    assert r.status == "partial" and st[srcs[1].name] == "failed" and st[srcs[0].name] == st[srcs[2].name] == "imported"
    failed_merchant = next(o.merchant_id for o in r.files if o.name == srcs[1].name)
    assert count(sandbox, "products", f"WHERE merchant_id='{failed_merchant}'") == 0  # rolled back: no partial rows
    assert count(sandbox, "automation_files") == 2 and count(sandbox, "upload_log") == 2
    again = run_pipeline(srcs, sandbox)  # same inputs, no failure: only the failed file is processed
    assert again.status == "success" and again.totals["files_imported"] == 1 and again.totals["files_skipped"] == 2
    assert count(sandbox, "products") == sum(o.rows_valid for o in r.files if o.status != "failed") + again.totals["rows_inserted"]


def test_crash_in_quality_stage_is_recorded_not_raised(wl, sandbox):
    def boom(stage, name):
        if stage == "quality":
            raise RuntimeError("audit exploded")

    r = run_pipeline(sources(wl), sandbox, fail_hook=boom)
    assert r.status == "failed" and "audit exploded" in r.error
    assert count(sandbox, "products") == r.totals["rows_inserted"] > 0  # imports are committed and kept
    assert count(sandbox, "automation_runs", "WHERE status='failed'") == 1
    assert run_pipeline(sources(wl), sandbox).status == "success"


def test_transient_sqlite_lock_is_retried(wl, sandbox, monkeypatch):
    real, calls = automation.import_products, {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise sqlite3.OperationalError("database is locked")
        return real(*a, **k)

    monkeypatch.setattr(automation, "import_products", flaky)
    monkeypatch.setattr(automation.time, "sleep", lambda s: None)
    r = run_pipeline([sources(wl)[0]], sandbox, max_retries=2)
    assert r.status == "success" and calls["n"] == 3
    calls["n"] = 0
    r2 = run_pipeline([sources(wl)[1]], sandbox, max_retries=1)
    assert r2.status == "failed" and "locked" in r2.files[0].error
    assert count(sandbox, "automation_files") == 1  # the failed file left no hash record


def test_non_lock_database_errors_are_not_retried(wl, sandbox, monkeypatch):
    calls = {"n": 0}

    def broken(*a, **k):
        calls["n"] += 1
        raise sqlite3.OperationalError("no such table: nope")

    monkeypatch.setattr(automation, "import_products", broken)
    r = run_pipeline([sources(wl)[0]], sandbox, max_retries=3)
    assert r.status == "failed" and calls["n"] == 1


# ---- inbox folder -----------------------------------------------------------------------------
def test_inbox_moves_files_and_ignores_temp_and_unsupported(wl, sandbox, tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    for n, d in wl.files:
        (inbox / n).write_bytes(d)
    (inbox / "M-002_broken.csv").write_bytes(b"nothing useful")
    (inbox / "~$M-001_lock.xlsx").write_bytes(b"office lock file")
    (inbox / "M-001_half.csv.part").write_bytes(b"partial")
    (inbox / "readme.txt").write_text("hi")
    r = run_inbox(inbox, sandbox)
    assert r.status == "partial"
    assert sorted(p.name for p in (inbox / "processed").iterdir()) == sorted(n for n, _ in wl.files)
    assert [p.name for p in (inbox / "failed").iterdir() if not p.name.endswith(".error.txt")] == ["M-002_broken.csv"]
    assert "M-002_broken.csv.error.txt" in [p.name for p in (inbox / "failed").iterdir()]
    assert {p.name for p in inbox.iterdir() if p.is_file()} == {"~$M-001_lock.xlsx", "M-001_half.csv.part", "readme.txt"}
    assert (tmp_path / "outbox").exists()


def test_inbox_min_age_skips_files_still_being_written(wl, sandbox, tmp_path):
    inbox = tmp_path / "in"
    inbox.mkdir()
    (inbox / wl.files[0][0]).write_bytes(wl.files[0][1])
    assert run_inbox(inbox, sandbox, min_age_seconds=3600).status == "nothing_to_do"
    assert (inbox / wl.files[0][0]).exists()


def test_crash_after_commit_before_move_is_recovered_without_double_import(wl, sandbox, tmp_path):
    inbox = tmp_path / "in"
    inbox.mkdir()
    name, data = wl.files[0]
    (inbox / name).write_bytes(data)
    run_pipeline(automation.collect_inbox(inbox), sandbox)  # imported, but the process "died" before moving the file
    n = count(sandbox, "products")
    r = run_inbox(inbox, sandbox)
    assert r.totals["files_skipped"] == 1 and count(sandbox, "products") == n
    assert (inbox / "processed" / name).exists() and not (inbox / name).exists()


def test_name_collisions_in_processed_folder_do_not_overwrite(wl, sandbox, tmp_path):
    inbox = tmp_path / "in"
    (inbox / "processed").mkdir(parents=True)
    name, data = wl.files[0]
    (inbox / "processed" / name).write_bytes(b"older file with the same name")
    (inbox / name).write_bytes(data)
    run_inbox(inbox, sandbox)
    assert (inbox / "processed" / name).read_bytes() == b"older file with the same name" and len(list((inbox / "processed").iterdir())) == 2


# ---- database integrity -------------------------------------------------------------------------
def test_database_integrity_after_mixed_and_repeated_runs(wl, sandbox):
    run_pipeline([*sources(wl), Source("M-009_bad.csv", b"junk")], sandbox)
    run_pipeline(sources(wl), sandbox)
    with connect(sandbox, readonly=True) as c:
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []
        assert c.execute("SELECT COUNT(*) FROM products p LEFT JOIN merchants m USING (merchant_id) WHERE m.merchant_id IS NULL").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM (SELECT merchant_id, sku FROM products GROUP BY 1, 2 HAVING COUNT(*) > 1)").fetchone()[0] == 0
        assert c.execute("SELECT SUM(rows_inserted) FROM upload_log").fetchone()[0] == c.execute("SELECT COUNT(*) FROM products").fetchone()[0]
        assert c.execute("SELECT COUNT(*) FROM automation_files f WHERE f.run_id IS NULL").fetchone()[0] == 0
        assert c.execute("SELECT SUM(files_imported) FROM automation_runs").fetchone()[0] == c.execute("SELECT COUNT(*) FROM automation_files").fetchone()[0]


def test_history_survives_demo_reseed_but_file_hashes_do_not(wl, tmp_path):
    db = tmp_path / "demo.db"
    seed_database(db, as_of=AS_OF)
    run_pipeline(sources(wl), db, target_label="demo")
    assert count(db, "automation_runs") == 1 and count(db, "automation_files") == 3
    seed_database(db, as_of=AS_OF)  # reseed = drop_all + recreate
    assert count(db, "automation_runs") == 1 and count(db, "automation_files") == 0
    assert run_pipeline(sources(wl), db).totals["files_imported"] == 3  # hashes were reset together with the data


def test_pipeline_never_touches_other_database_files(wl, sandbox, tmp_path):
    demo = tmp_path / "demo.db"
    seed_database(demo, as_of=AS_OF)
    before = count(demo, "products")
    run_pipeline(sources(wl), sandbox, history_db=demo)  # history goes to demo DB, catalog data does not
    assert count(demo, "products") == before and count(demo, "automation_runs") == 1 and count(demo, "automation_files") == 0
    assert count(sandbox, "automation_runs") == 0 and count(sandbox, "products") > 0


# ---- outputs ------------------------------------------------------------------------------------
def test_zip_outputs_are_valid_and_excel_opens(wl, sandbox):
    r = run_pipeline(sources(wl), sandbox)
    zf = zipfile.ZipFile(io.BytesIO(r.zip_bytes()))
    assert zf.testzip() is None and len(zf.namelist()) == len(r.outputs) >= 7
    xlsx = next(n for n in zf.namelist() if n.startswith("executive_report") and n.endswith(".xlsx"))
    assert "KPIs" in load_workbook(io.BytesIO(zf.read(xlsx))).sheetnames
    findings = pd.read_csv(io.BytesIO(zf.read(next(n for n in zf.namelist() if n.startswith("quality_findings_run") and n.endswith(".csv")))))
    assert set(findings["issue_type"]) >= {"Duplicate Listing", "Missing Image"} and len(findings) > 0
    assert (r.rejected_rows is not None) and len(r.rejected_rows) == r.totals["rows_rejected"]


# ---- CLI ----------------------------------------------------------------------------------------
def test_cli_refuses_demo_database_and_runs_on_sandbox(wl, tmp_path):
    main = tmp_path / "main.db"
    seed_database(main, as_of=AS_OF)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    for n, d in wl.files:
        (inbox / n).write_bytes(d)
    env = {"PATH": "", "SYSTEMROOT": "C:\\Windows", "CATALOG_DB_PATH": str(main)}
    cmd = [sys.executable, str(ROOT / "scripts" / "run_automation.py"), "--inbox", str(inbox), "--min-age", "0"]
    refused = subprocess.run([*cmd, "--db", str(main)], capture_output=True, text=True, env=env, cwd=ROOT, timeout=120)
    assert refused.returncode == 2 and "Refusing" in refused.stderr and count(main, "products") == count(main, "products")
    ok = subprocess.run([*cmd, "--db", str(tmp_path / "sandbox.db"), "--history-db", str(main)], capture_output=True, text=True, env=env, cwd=ROOT, timeout=180)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    out = json.loads(ok.stdout[ok.stdout.index("{"):])
    assert out["status"] == "success" and out["totals"]["files_imported"] == 3
    assert count(main, "automation_runs") == 1 and (tmp_path / "outbox").exists()
    again = subprocess.run([*cmd, "--db", str(tmp_path / "sandbox.db")], capture_output=True, text=True, env=env, cwd=ROOT, timeout=120)
    assert again.returncode == 0 and "nothing_to_do" in again.stdout  # files were moved to processed/
