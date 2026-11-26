"""Python automation pipeline: collect -> validate -> import -> quality checks -> report -> export.

This is *API-level* automation (library calls, no browser): fast, testable and scriptable. UI-driven RPA lives in
scripts/rpa_ui_bot.py and is meant for steps that can only be done through a screen. See docs/automation.md.

Safety properties (all covered by tests):
  * idempotent: a file whose SHA-256 was already imported is skipped, so re-running never duplicates data;
  * transactional: each file is imported in one transaction together with its hash record, so a failure leaves no
    partial import and a crash after commit cannot cause a double import;
  * isolated failures: one bad file is recorded and the remaining files still run;
  * recoverable: inbox files are only moved after success/failure is known; transient SQLite lock errors are retried;
  * private: history stores counts, timings, file names and short hashes only - never row contents or secrets.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import time
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

import pandas as pd

from .config import fmt, utcnow
from .db import connect, init_schema, query_df
from .excel import to_xlsx_bytes
from .quality import run_audit
from .reports import executive_report_html, executive_report_xlsx, to_csv_bytes
from .seed import MERCHANTS
from .validation import UploadError, import_products, validate_upload_bytes

FILE_MERCHANT_RE = re.compile(r"^(M-\d{3})(?:[_\-\s.]|$)", re.IGNORECASE)
SUPPORTED_EXT = (".csv", ".xlsx")
STEPS = ["collect", "validate", "import", "quality", "report", "export"]


@dataclass
class Source:
    name: str
    data: bytes
    path: Path | None = None


@dataclass
class FileOutcome:
    name: str
    sha256: str
    merchant_id: str | None = None
    status: str = "pending"  # imported | skipped_duplicate_file | failed
    rows_total: int = 0
    rows_valid: int = 0
    rows_rejected: int = 0
    inserted: int = 0
    updated: int = 0
    error: str | None = None


@dataclass
class RunResult:
    run_id: int | None
    status: str
    mode: str
    started_at: str
    seconds_total: float
    step_seconds: dict[str, float]
    quality_seconds: dict[str, float]
    files: list[FileOutcome]
    totals: dict[str, int]
    findings_run_scope: dict[str, int]
    findings_catalog: dict[str, int]
    error: str | None = None
    outputs: dict[str, bytes] = field(default_factory=dict)
    flagged_run_scope: dict[str, set] = field(default_factory=dict)
    rejected_rows: pd.DataFrame | None = None

    def summary(self) -> dict:
        d = asdict(self)
        for k in ("outputs", "flagged_run_scope", "rejected_rows"):
            d.pop(k)
        return d

    def zip_bytes(self) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, data in self.outputs.items():
                zf.writestr(name, data)
        return buf.getvalue()


def merchant_from_filename(name: str, default: str | None = None) -> str | None:
    m = FILE_MERCHANT_RE.match(Path(name).name)
    return m.group(1).upper() if m else default


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _short_error(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {str(exc)[:200]}".replace("\n", " ")


def ensure_sandbox(path: str | Path) -> Path:
    """Create an isolated catalog database (schema + merchants, no products) used as the safe automation target."""
    p = Path(path)
    with connect(p) as conn:
        init_schema(conn)
        if conn.execute("SELECT COUNT(*) FROM merchants").fetchone()[0] == 0:
            conn.executemany("INSERT INTO merchants VALUES (?,?,?,?,?,?)", [(m[0], m[1], m[2], m[3], m[4], fmt(utcnow())) for m in MERCHANTS])
    return p


def reset_sandbox(path: str | Path) -> Path:
    p = Path(path)
    for suffix in ("", "-wal", "-shm"):
        Path(str(p) + suffix).unlink(missing_ok=True)
    return ensure_sandbox(p)


def _retry(fn: Callable, retries: int, base_delay: float = 0.2):
    """Retry transient SQLite lock errors with exponential backoff."""
    for attempt in range(retries + 1):
        try:
            return fn()
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                raise
            if attempt == retries:
                raise
            time.sleep(base_delay * (2 ** attempt))


def run_pipeline(
    sources: list[Source], db_path: str | Path, *, history_db: str | Path | None = None, out_dir: str | Path | None = None,
    default_merchant: str | None = None, trigger: str = "cli", target_label: str = "main", mode: str = "python",
    workload_label: str | None = None, max_retries: int = 2, fail_hook: Callable[[str, str], None] | None = None,
    build_outputs: bool = True,
) -> RunResult:
    """Run the whole pipeline over `sources`. Never raises for data problems; failures are recorded per file / per run."""
    t_run = time.perf_counter()
    started = fmt(utcnow())
    steps = {s: 0.0 for s in STEPS}
    quality_t: dict[str, float] = {}
    outcomes: list[FileOutcome] = []
    reports: dict[str, object] = {}
    rejected_frames: list[pd.DataFrame] = []
    imported_keys: set[tuple[str, str]] = set()
    outputs: dict[str, bytes] = {}
    error: str | None = None
    audit = None
    flagged_scope: dict[str, set] = {}
    run_scope = {"duplicates": 0, "price_anomalies": 0, "missing_images": 0, "incomplete_metadata": 0}
    catalog_counts = dict(run_scope)

    def hook(stage: str, name: str) -> None:
        if fail_hook:
            fail_hook(stage, name)

    try:
        with connect(db_path) as c:
            init_schema(c)

        # 1. collect: hash, route to a merchant, skip files that were already imported
        t0 = time.perf_counter()
        with connect(db_path, readonly=True) as c:
            known = {r[0] for r in c.execute("SELECT sha256 FROM automation_files")}
        todo: list[tuple[Source, FileOutcome]] = []
        for s in sources:
            o = FileOutcome(s.name, sha256_hex(s.data), merchant_from_filename(s.name, default_merchant))
            outcomes.append(o)
            if o.sha256 in known:
                o.status = "skipped_duplicate_file"
            elif o.merchant_id is None:
                o.status, o.error = "failed", "Cannot determine merchant: name the file M-001_*.csv or choose a default merchant."
            elif not s.name.lower().endswith(SUPPORTED_EXT):
                o.status, o.error = "failed", "Unsupported file type (only .csv and .xlsx)."
            else:
                todo.append((s, o))
        steps["collect"] = time.perf_counter() - t0

        # 2. validate
        t0 = time.perf_counter()
        validated: list[tuple[Source, FileOutcome, object]] = []
        for s, o in todo:
            try:
                hook("validate", s.name)
                rep = validate_upload_bytes(s.data, s.name)
                o.rows_total, o.rows_valid, o.rows_rejected = rep.total_rows, len(rep.valid), len(rep.rejected)
                if len(rep.rejected):
                    rejected_frames.append(rep.rejected.assign(file=s.name))
                if rep.ok:
                    validated.append((s, o, rep))
                else:
                    o.status, o.error = "failed", "No valid rows in file."
            except UploadError as exc:
                o.status, o.error = "failed", _short_error(exc)
            except Exception as exc:  # noqa: BLE001 - one bad file must not stop the run
                o.status, o.error = "failed", _short_error(exc)
        steps["validate"] = time.perf_counter() - t0

        # 3. import (one transaction per file, together with its hash record)
        t0 = time.perf_counter()
        for s, o, rep in validated:
            def do_import(s=s, o=o, rep=rep):
                with connect(db_path) as conn:
                    hook("import", s.name)
                    res = import_products(conn, o.merchant_id, rep.valid, s.name, rep.total_rows)
                    conn.execute(
                        "INSERT INTO automation_files (sha256, filename, merchant_id, rows_valid, run_id, imported_at) VALUES (?,?,?,?,NULL,?)",
                        (o.sha256, s.name[:200], o.merchant_id, len(rep.valid), fmt(utcnow())),
                    )
                    return res

            try:
                res = _retry(do_import, max_retries)
                o.status, o.inserted, o.updated = "imported", res["inserted"], res["updated"]
                imported_keys.update((o.merchant_id, sku) for sku in rep.valid["sku"])
            except Exception as exc:  # noqa: BLE001
                o.status, o.error = "failed", _short_error(exc)
        steps["import"] = time.perf_counter() - t0

        # 4. quality checks (duplicates, price anomalies, missing images, metadata) over the target catalog
        if imported_keys:
            with connect(db_path, readonly=True) as conn:
                hook("quality", "")
                t0 = time.perf_counter()
                audit = run_audit(conn, timings=quality_t)
                steps["quality"] = time.perf_counter() - t0
                iss = audit.issues
                names = {"duplicates": ["Duplicate Listing"], "price_anomalies": ["Price Anomaly", "Invalid Price"], "missing_images": ["Missing Image"], "incomplete_metadata": ["Incomplete Metadata"]}
                key = pd.MultiIndex.from_frame(iss[["merchant_id", "sku"]]) if len(iss) else None
                in_scope = pd.Series(key.isin(list(imported_keys)), index=iss.index) if key is not None else pd.Series(dtype=bool)
                for k, types in names.items():
                    sel = iss["issue_type"].isin(types)
                    catalog_counts[k] = int(sel.sum())
                    scoped = iss[sel & in_scope] if len(iss) else iss
                    run_scope[k] = int(len(scoped))
                    flagged_scope[k] = set(zip(scoped["merchant_id"], scoped["sku"])) if len(scoped) else set()

                # 5. report
                t0 = time.perf_counter()
                hook("report", "")
                stamp = utcnow().strftime("%Y%m%dT%H%M%SZ")
                if build_outputs:
                    outputs[f"executive_report_{stamp}.html"] = executive_report_html(conn, audit).encode("utf-8")
                    outputs[f"executive_report_{stamp}.xlsx"] = executive_report_xlsx(conn, audit)
                steps["report"] = time.perf_counter() - t0

            # 6. export tables as CSV + Excel
            t0 = time.perf_counter()
            if build_outputs:
                hook("export", "")
                scoped_issues = iss[in_scope] if len(iss) else iss
                rejected = pd.concat(rejected_frames, ignore_index=True) if rejected_frames else pd.DataFrame(columns=["row", "sku", "errors", "file"])
                outputs[f"quality_findings_run_{stamp}.csv"] = to_csv_bytes(scoped_issues)
                outputs[f"quality_findings_run_{stamp}.xlsx"] = to_xlsx_bytes({"Findings (this run)": scoped_issues})
                outputs[f"rejected_rows_{stamp}.csv"] = to_csv_bytes(rejected)
                for kind, label in (("Duplicate Listing", "duplicates"), ("Price Anomaly", "price_anomalies"), ("Missing Image", "missing_images")):
                    part = scoped_issues[scoped_issues["issue_type"] == kind] if len(scoped_issues) else scoped_issues
                    outputs[f"{label}_{stamp}.csv"] = to_csv_bytes(part)
                if out_dir:
                    od = Path(out_dir)
                    od.mkdir(parents=True, exist_ok=True)
                    for name, data in outputs.items():
                        tmp = od / (name + ".part")
                        tmp.write_bytes(data)
                        os.replace(tmp, od / name)  # atomic: readers never see a half-written file
            steps["export"] = time.perf_counter() - t0
    except Exception as exc:  # noqa: BLE001 - recorded, not raised, so schedulers get a result
        error = _short_error(exc)

    rejected_df = pd.concat(rejected_frames, ignore_index=True) if rejected_frames else None
    totals = {
        "files_total": len(outcomes), "files_imported": sum(o.status == "imported" for o in outcomes),
        "files_failed": sum(o.status == "failed" for o in outcomes), "files_skipped": sum(o.status == "skipped_duplicate_file" for o in outcomes),
        "rows_total": sum(o.rows_total for o in outcomes), "rows_valid": sum(o.rows_valid for o in outcomes),
        "rows_rejected": sum(o.rows_rejected for o in outcomes), "rows_inserted": sum(o.inserted for o in outcomes), "rows_updated": sum(o.updated for o in outcomes),
    }
    if error:
        status = "failed"
    elif not outcomes:
        status = "nothing_to_do"
    elif totals["files_failed"] == 0:
        status = "success"
    elif totals["files_imported"] + totals["files_skipped"] == 0:
        status = "failed"
    else:
        status = "partial"
    seconds = time.perf_counter() - t_run
    result = RunResult(None, status, mode, started, round(seconds, 3), {k: round(v, 4) for k, v in steps.items()}, {k: round(v, 4) for k, v in quality_t.items()},
                       outcomes, totals, run_scope, catalog_counts, error, outputs, flagged_scope, rejected_df)
    try:
        result.run_id = record_run(history_db or db_path, result, trigger, target_label, workload_label)
        if imported_keys and (history_db is None or str(history_db) == str(db_path)):
            with connect(db_path) as c:
                c.execute("UPDATE automation_files SET run_id = ? WHERE run_id IS NULL", (result.run_id,))
    except Exception as exc:  # noqa: BLE001 - history problems must not hide the run result
        result.error = result.error or f"history not recorded: {_short_error(exc)}"
    return result


def record_run(history_db: str | Path, r: RunResult, trigger: str, target: str, workload_label: str | None = None) -> int:
    details = [{"file": o.name, "sha": o.sha256[:12], "merchant": o.merchant_id, "status": o.status, "rows": o.rows_total, "valid": o.rows_valid,
                "rejected": o.rows_rejected, "inserted": o.inserted, "updated": o.updated, "error": o.error} for o in r.files]
    with connect(history_db) as conn:
        init_schema(conn)
        cur = conn.execute(
            """INSERT INTO automation_runs (started_at, finished_at, mode, trigger_source, target, status, files_total, files_imported, files_failed, files_skipped,
               rows_total, rows_valid, rows_rejected, rows_inserted, rows_updated, duplicates, price_anomalies, missing_images, incomplete_metadata,
               seconds_total, step_seconds, details, error, workload_label) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (r.started_at, fmt(utcnow()), r.mode, trigger, target, r.status, r.totals["files_total"], r.totals["files_imported"], r.totals["files_failed"],
             r.totals["files_skipped"], r.totals["rows_total"], r.totals["rows_valid"], r.totals["rows_rejected"], r.totals["rows_inserted"], r.totals["rows_updated"],
             r.findings_run_scope["duplicates"], r.findings_run_scope["price_anomalies"], r.findings_run_scope["missing_images"], r.findings_run_scope["incomplete_metadata"],
             r.seconds_total, json.dumps(r.step_seconds), json.dumps(details), r.error, workload_label),
        )
        return int(cur.lastrowid)


def load_history(conn, limit: int = 100) -> pd.DataFrame:
    return query_df(conn, "SELECT * FROM automation_runs ORDER BY run_id DESC LIMIT ?", (limit,))


# ---- inbox folder automation ------------------------------------------------
def collect_inbox(inbox: str | Path, min_age_seconds: float = 0.0) -> list[Source]:
    """Read supported files from a folder, skipping temp/partial files and files still being written."""
    out = []
    now = time.time()
    for p in sorted(Path(inbox).glob("*")):
        if not p.is_file() or p.name.startswith(("~$", ".")) or p.suffix.lower() not in SUPPORTED_EXT:
            continue
        if min_age_seconds and now - p.stat().st_mtime < min_age_seconds:
            continue
        out.append(Source(p.name, p.read_bytes(), p))
    return out


def _unique_dest(folder: Path, name: str) -> Path:
    dest = folder / name
    n = 1
    while dest.exists():
        dest = folder / f"{Path(name).stem}.{n}{Path(name).suffix}"
        n += 1
    return dest


def run_inbox(
    inbox: str | Path, db_path: str | Path, *, processed_dir: str | Path | None = None, failed_dir: str | Path | None = None,
    out_dir: str | Path | None = None, min_age_seconds: float = 0.0, **kwargs,
) -> RunResult:
    """Collect an inbox folder, run the pipeline, then move files to processed/ or failed/ (with an .error.txt)."""
    inbox = Path(inbox)
    processed_dir = Path(processed_dir or inbox / "processed")
    failed_dir = Path(failed_dir or inbox / "failed")
    sources = collect_inbox(inbox, min_age_seconds)
    result = run_pipeline(sources, db_path, out_dir=out_dir or inbox.parent / "outbox", **kwargs)
    by_name = {s.name: s for s in sources}
    for o in result.files:
        src = by_name.get(o.name)
        if src is None or src.path is None or not src.path.exists():
            continue
        if o.status in ("imported", "skipped_duplicate_file"):
            processed_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src.path), _unique_dest(processed_dir, o.name))
        elif o.status == "failed":
            failed_dir.mkdir(parents=True, exist_ok=True)
            dest = _unique_dest(failed_dir, o.name)
            shutil.move(str(src.path), dest)
            Path(str(dest) + ".error.txt").write_text(f"{o.error}\n", encoding="utf-8")
    return result
