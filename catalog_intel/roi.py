"""Before/after automation KPIs. Pure functions shared by the measurement script, the dashboard and the tests.

Basis of every number is explicit:
  * automated times (Python, UI RPA) are MEASURED by running the pipeline/bot (see scripts/measure_automation.py);
  * manual times are ESTIMATES = unit-time assumptions x workload size. Nobody was timed doing the work by hand.
Change the assumptions (docs/automation/manual_time_assumptions.json or the dashboard sliders) and every estimate follows.
"""
from __future__ import annotations

# Seconds an Excel-assisted analyst is ASSUMED to need. Deliberately conservative (low) so savings are not overstated.
DEFAULT_ASSUMPTIONS: dict[str, float] = {
    "collect_open_per_file": 60.0,      # find the file, open it, check the layout, copy into the master sheet
    "validate_per_row": 0.5,            # eyeball required fields / formats with filters
    "import_per_file": 20.0,            # paste / upload one file into the catalog system
    "import_per_row": 0.05,             # fix-ups per imported row
    "dedupe_per_row": 0.6,              # sort + scan titles for duplicates
    "price_per_row": 0.4,               # sort by price, scan outliers per category
    "image_per_row": 0.3,               # filter blank/odd image cells
    "report_per_run": 600.0,            # build and format the summary report
    "export_per_run": 120.0,            # save/export CSV and Excel deliverables
    "automation_review_per_run": 300.0,  # AFTER automation a person still reads the report and triages flagged items
}

TASKS = ["collect_validate", "import", "quality", "report", "export"]
TASK_LABELS = {
    "collect_validate": "Collect & validate files", "import": "Import / consolidate", "quality": "Quality checks (duplicates, price, images)",
    "report": "Report creation", "export": "Excel/CSV export",
}


def manual_estimate(rows: int, files: int, assumptions: dict[str, float] | None = None, multiplier: float = 1.0) -> dict[str, float]:
    """ESTIMATED manual seconds per task for a workload of `rows` rows in `files` files."""
    a = {**DEFAULT_ASSUMPTIONS, **(assumptions or {})}
    est = {
        "collect_validate": files * a["collect_open_per_file"] + rows * a["validate_per_row"],
        "import": files * a["import_per_file"] + rows * a["import_per_row"],
        "quality": rows * (a["dedupe_per_row"] + a["price_per_row"] + a["image_per_row"]),
        "report": a["report_per_run"],
        "export": a["export_per_run"],
    }
    return {k: round(v * multiplier, 2) for k, v in est.items()}


def manual_quality_breakdown(rows: int, assumptions: dict[str, float] | None = None, multiplier: float = 1.0) -> dict[str, float]:
    a = {**DEFAULT_ASSUMPTIONS, **(assumptions or {})}
    return {"duplicates": rows * a["dedupe_per_row"] * multiplier, "price": rows * a["price_per_row"] * multiplier, "image": rows * a["image_per_row"] * multiplier}


def total(steps: dict[str, float]) -> float:
    return round(sum(steps.get(t, 0.0) for t in TASKS), 3)


def time_saved_pct(before: float, after: float) -> float | None:
    """(before - after) / before * 100."""
    return round((before - after) / before * 100, 2) if before > 0 else None


def productivity_gain(before: float, after: float) -> float | None:
    """Speed-up factor before/after (x). 'Productivity gain %' is (factor - 1) * 100."""
    return round(before / after, 2) if after > 0 else None


def productivity_gain_pct(before: float, after: float) -> float | None:
    f = productivity_gain(before, after)
    return round((f - 1) * 100, 1) if f is not None else None


def rows_per_hour(rows: int, seconds: float) -> float | None:
    return round(rows / seconds * 3600, 1) if seconds > 0 else None


def reduction_pct(before_errors: float, after_errors: float) -> float | None:
    """Reduction in missed/erroneous items: (before - after) / before * 100."""
    return round((before_errors - after_errors) / before_errors * 100, 2) if before_errors > 0 else None


def comparison_table(rows: int, manual: dict[str, float], python: dict[str, float], rpa: dict[str, float] | None = None, review_s: float = 0.0) -> list[dict]:
    """Per-task rows with before/after seconds and savings. Python/RPA columns are measured machine time; manual is estimated.

    `review_s` is the ESTIMATED human time that remains after automation (reading the report, triaging flagged items). It is
    added in a separate row and in the last row ("Total incl. human review"), which is the honest end-to-end comparison.
    """
    def row(label, kind, m, p, r):
        return {"task": label, "kind": kind, "manual_estimated_s": round(m, 2), "python_measured_s": round(p, 3), "rpa_measured_s": None if r is None else round(r, 3),
                "python_time_saved_pct": time_saved_pct(m, p), "python_speedup_x": productivity_gain(m, p), "rpa_time_saved_pct": time_saved_pct(m, r) if r is not None else None}

    out = [row(TASK_LABELS[t], "task", manual.get(t, 0.0), python.get(t, 0.0), rpa.get(t) if rpa else None) for t in TASKS]
    m_tot, p_tot, r_tot = total(manual), total(python), (total(rpa) if rpa else None)
    out.append(row("Total (machine time)", "total", m_tot, p_tot, r_tot))
    if review_s:
        out.append({"task": "Human review of automated report (ESTIMATED, remains after automation)", "kind": "review", "manual_estimated_s": 0.0, "python_measured_s": review_s,
                    "rpa_measured_s": review_s if rpa else None, "python_time_saved_pct": None, "python_speedup_x": None, "rpa_time_saved_pct": None})
        out.append(row("Total incl. human review", "total_incl_review", m_tot, p_tot + review_s, None if r_tot is None else r_tot + review_s))
    return out


def monthly_savings_hours(manual_run_s: float, automated_run_s: float, runs_per_month: float) -> float:
    """ESTIMATED hours saved per month = runs/month x (manual - automated) per run."""
    return round(runs_per_month * max(manual_run_s - automated_run_s, 0.0) / 3600, 2)


def scale_run_time(measured_seconds: float, measured_rows: int, rows: int) -> float:
    """ESTIMATE of an automated run's duration at another size: linear scaling of the measured time (a simplification)."""
    return measured_seconds * rows / measured_rows if measured_rows > 0 else 0.0
