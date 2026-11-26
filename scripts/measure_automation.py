"""Measure the automation on an identical, seeded, SYNTHETIC workload and record MEASURED vs ESTIMATED figures.

    python scripts/measure_automation.py                      # 10 files x 500 rows; 5 Python runs; 2 UI-RPA runs; 100k-row scale test
    python scripts/measure_automation.py --skip-rpa --skip-scale --python-runs 3      # quick

What is MEASURED (timed by running it): Python automation (inbox -> validate -> import -> quality -> report -> export),
UI RPA (Playwright bot through a real browser against a throw-away Streamlit server), peak memory, throughput, detector
accuracy against injected ground truth for both the automation and an Excel-style baseline PROCEDURE.
What is ESTIMATED: manual (human) time = unit-time assumptions x workload size; monthly savings. Nobody was timed by hand.

Outputs: docs/automation/measurements.json and measurements.md. Demo/real databases are never touched: every run uses
temporary databases, and the demo DB is not opened.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import platform
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from catalog_intel import baseline, roi  # noqa: E402
from catalog_intel.automation import ensure_sandbox, run_inbox  # noqa: E402
from catalog_intel.db import connect  # noqa: E402
from catalog_intel.seed import seed_database  # noqa: E402
from catalog_intel.workload import generate_workload  # noqa: E402

MB = 1024 * 1024
PROC = psutil.Process()
DETECTORS = {"duplicate": "duplicates", "price_anomaly": "price_anomalies", "missing_image": "missing_images"}


class PeakRSS:
    def __enter__(self):
        self.peak, self.stop = PROC.memory_info().rss, threading.Event()
        self.base = self.peak

        def sample():
            while not self.stop.wait(0.02):
                self.peak = max(self.peak, PROC.memory_info().rss)

        self.t = threading.Thread(target=sample, daemon=True)
        self.t.start()
        return self

    def __exit__(self, *a):
        self.stop.set()
        self.t.join()
        self.peak = max(self.peak, PROC.memory_info().rss)

    @property
    def peak_mb(self) -> float:
        return round(self.peak / MB, 1)


def write_inbox(files, folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    for name, data in files:
        (folder / name).write_bytes(data)
    return folder


def python_run(files, tmp: Path, label: str) -> dict:
    inbox = write_inbox(files, tmp / "inbox")
    db = ensure_sandbox(tmp / "sandbox.db")
    with PeakRSS() as mem:
        r = run_inbox(inbox, db, out_dir=tmp / "outbox", trigger="measure", target_label="sandbox", workload_label=label)
    s = r.step_seconds
    return {
        "status": r.status, "seconds_total": r.seconds_total, "peak_rss_mb": mem.peak_mb, "totals": r.totals,
        "steps": {"collect_validate": round(s["collect"] + s["validate"], 4), "import": s["import"], "quality": s["quality"], "report": s["report"], "export": s["export"]},
        "step_detail": s, "quality_detail": r.quality_seconds, "findings_run_scope": r.findings_run_scope, "flagged": r.flagged_run_scope, "result": r,
    }


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_server(db: Path):
    port = free_port()
    env = {**os.environ, "CATALOG_DB_PATH": str(db)}
    proc = subprocess.Popen([sys.executable, "-m", "streamlit", "run", "app.py", "--server.port", str(port), "--server.headless", "true"],
                            cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    t0 = time.time()
    while time.time() - t0 < 90:
        try:
            if urllib.request.urlopen(f"http://127.0.0.1:{port}/_stcore/health", timeout=2).read() == b"ok":
                return proc, port
        except Exception:  # noqa: BLE001
            time.sleep(0.5)
    proc.terminate()
    raise RuntimeError("Streamlit test server did not start")


def rpa_run(files, tmp: Path) -> dict:
    spec = importlib.util.spec_from_file_location("rpa_ui_bot", ROOT / "scripts" / "rpa_ui_bot.py")
    bot = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bot)
    inbox = write_inbox(files, tmp / "rpa_inbox")
    db = tmp / "rpa_app.db"
    seed_database(db)  # demo requests so every page renders; products removed so the workload starts from an empty catalog
    with connect(db) as c:
        c.execute("DELETE FROM products")
    proc, port = start_server(db)
    try:
        r = bot.run_bot(f"http://127.0.0.1:{port}", inbox, tmp / "rpa_out", None, True, 2)
    finally:
        proc.terminate()
        proc.wait(timeout=20)
    return {"status": r.status, "seconds_total": r.seconds_total, "steps": r.step_seconds, "totals": r.totals, "error": r.error, "findings_catalog_ui": r.findings_catalog,
            "files": [{"file": o.name, "status": o.status, "rows": o.rows_total, "valid": o.rows_valid, "error": o.error} for o in r.files]}


def med(runs: list[dict], key: str) -> dict:
    keys = runs[0][key].keys()
    return {k: round(statistics.median(r[key][k] for r in runs), 4) for k in keys}


def accuracy(work, run_result, raw) -> dict:
    out = {}
    rejected = run_result["totals"]["rows_rejected"]
    for kind, key in DETECTORS.items():
        flagged_auto = run_result["flagged"][key]
        if kind == "duplicate":
            auto, base = baseline.score_pairs(work.dup_pairs, flagged_auto), baseline.score_pairs(work.dup_pairs, baseline.naive_duplicates(raw))
        else:
            truth = work.truth[kind]
            fn = baseline.naive_price_outliers if kind == "price_anomaly" else baseline.naive_missing_images
            auto, base = baseline.score_sets(truth, flagged_auto), baseline.score_sets(truth, fn(raw))
        out[kind] = {"automation": auto, "excel_style_baseline": base}
    inj = work.invalid_rows
    naive = baseline.naive_invalid_rows(raw)
    out["invalid_rows"] = {
        "automation": {"injected": inj, "caught": min(rejected, inj), "missed": inj - min(rejected, inj), "rejected_rows_reported": rejected},
        "excel_style_baseline": {"injected": inj, "caught": min(naive, inj), "missed": inj - min(naive, inj)},
    }
    a_miss = sum(v["automation"]["missed"] for v in out.values())
    b_miss = sum(v["excel_style_baseline"]["missed"] for v in out.values())
    out["TOTAL_omissions"] = {"automation_missed": a_miss, "baseline_missed": b_miss, "omission_reduction_pct": roi.reduction_pct(b_miss, a_miss),
                              "injected_total": sum(v["automation"]["injected"] for v in out.values() if "injected" in v.get("automation", {}))}
    return out


def estimates_block(w: dict, M: dict, assumptions: dict | None = None) -> dict:
    """Everything ESTIMATED, derived from the workload size, the MEASURED automated times and the manual-time assumptions."""
    a = {**roi.DEFAULT_ASSUMPTIONS, **(assumptions or {})}
    manual = roi.manual_estimate(w["rows_total"], w["files"], a)
    manual_total = roi.total(manual)
    py_steps = M["python_automation"]["median_steps_s"]
    rpa_steps = M["ui_rpa"]["median_steps_s"]
    review = a["automation_review_per_run"]
    py_machine = roi.total(py_steps)
    py_e2e = round(py_machine + review, 3)
    out = {
        "basis": "Unit-time assumptions x workload size (Excel-assisted analyst). NOT measured on people. 'End-to-end' adds the ESTIMATED human review that remains after automation.",
        "manual_time_assumptions_s": a, "manual_steps_s": manual, "manual_total_s": manual_total, "automation_review_s": review,
        "python_machine_total_s": py_machine, "python_end_to_end_s": py_e2e,
        "comparison_table": roi.comparison_table(w["rows_total"], manual, py_steps, rpa_steps, review),
        "time_saved_pct_python": roi.time_saved_pct(manual_total, py_e2e), "time_saved_pct_python_machine_only": roi.time_saved_pct(manual_total, py_machine),
        "speedup_x_python": roi.productivity_gain(manual_total, py_e2e), "speedup_x_python_machine_only": roi.productivity_gain(manual_total, py_machine),
        "productivity_gain_pct_python": roi.productivity_gain_pct(manual_total, py_e2e),
        "manual_rows_per_hour": roi.rows_per_hour(w["rows_total"], manual_total), "python_end_to_end_rows_per_hour": roi.rows_per_hour(w["rows_total"], py_e2e),
        "monthly_saved_hours_at_4_runs": roi.monthly_savings_hours(manual_total, py_e2e, 4), "monthly_saved_hours_at_20_runs": roi.monthly_savings_hours(manual_total, py_e2e, 20),
    }
    if rpa_steps:
        rpa_e2e = round(roi.total(rpa_steps) + review, 3)
        out.update(rpa_end_to_end_s=rpa_e2e, time_saved_pct_rpa=roi.time_saved_pct(manual_total, rpa_e2e), speedup_x_rpa=roi.productivity_gain(manual_total, rpa_e2e))
    sc = M["scale_test_python_automation"]
    if sc:
        sman = roi.total(roi.manual_estimate(sc["rows"], sc["files"], a))
        sc["manual_estimated_total_s"] = sman
        sc["estimated_time_saved_pct"] = roi.time_saved_pct(sman, sc["seconds_total"] + review)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--files", type=int, default=10)
    ap.add_argument("--rows", type=int, default=500, help="rows per file")
    ap.add_argument("--python-runs", type=int, default=5)
    ap.add_argument("--rpa-runs", type=int, default=2)
    ap.add_argument("--scale-rows", type=int, default=100_000)
    ap.add_argument("--skip-rpa", action="store_true")
    ap.add_argument("--skip-scale", action="store_true")
    ap.add_argument("--out-dir", default=str(ROOT / "docs" / "automation"))
    ap.add_argument("--recompute", action="store_true", help="keep the stored MEASURED values; rebuild the ESTIMATED block and markdown from the current assumptions")
    args = ap.parse_args()
    if args.recompute:
        od = Path(args.out_dir)
        o = json.loads((od / "measurements.json").read_text(encoding="utf-8"))
        o["ESTIMATED"] = estimates_block(o["workload"], o["MEASURED"])
        (od / "measurements.json").write_text(json.dumps(o, indent=2, default=str), encoding="utf-8")
        (od / "measurements.md").write_text(render_md(o), encoding="utf-8")
        print("Recomputed ESTIMATED block from stored MEASURED values.")
        return 0

    work = generate_workload(args.files, args.rows)
    raw = work.combined_raw()
    label = f"synthetic workload seed=777 files={args.files} rows/file={args.rows}"
    print(f"Workload: {args.files} files, {work.rows_total:,} rows, injected {work.injected_counts}")

    print("Python automation runs")
    py_runs, first = [], None
    for i in range(args.python_runs):
        with tempfile.TemporaryDirectory() as tmp:
            r = python_run(work.files, Path(tmp), label)
        first = first or r
        py_runs.append({k: v for k, v in r.items() if k not in ("flagged", "result")})
        print(f"  run {i + 1}: {r['seconds_total']}s  steps {r['steps']}  peak RSS {r['peak_rss_mb']} MB")
    py_med = med(py_runs, "steps")
    py_total_med = round(statistics.median(r["seconds_total"] for r in py_runs), 3)

    rpa_runs, rpa_med = [], None
    if not args.skip_rpa:
        print("UI RPA runs (real browser against a throw-away Streamlit server)")
        for i in range(args.rpa_runs):
            with tempfile.TemporaryDirectory() as tmp:
                r = rpa_run(work.files, Path(tmp))
            rpa_runs.append(r)
            print(f"  run {i + 1}: {r['status']} {r['seconds_total']}s steps {r['steps']} imported={r['totals']['files_imported']}")
        ok = [r for r in rpa_runs if r["status"] == "success"]
        rpa_med = med(ok, "steps") if ok else None

    acc = accuracy(work, {"flagged": first["flagged"], "totals": first["totals"]}, raw)

    scale = None
    if not args.skip_scale:
        print(f"Scale test: {args.scale_rows:,} rows in 8 files")
        sw = generate_workload(8, args.scale_rows // 8, seed=778, xlsx_share=0.25)
        with tempfile.TemporaryDirectory() as tmp:
            sr = python_run(sw.files, Path(tmp), f"synthetic scale test rows={sw.rows_total}")
        scale = {
            "rows": sw.rows_total, "files": 8, "largest_file_mb": round(max(len(d) for _, d in sw.files) / MB, 2), "status": sr["status"], "seconds_total": sr["seconds_total"],
            "peak_rss_mb": sr["peak_rss_mb"], "steps": sr["steps"], "quality_detail": sr["quality_detail"], "totals": sr["totals"],
            "rows_per_second": round(sw.rows_total / sr["seconds_total"], 1), "rows_per_hour": roi.rows_per_hour(sw.rows_total, sr["seconds_total"]),
        }
        print(f"  {scale['seconds_total']}s, {scale['rows_per_second']} rows/s, peak RSS {scale['peak_rss_mb']} MB")

    out = {
        "synthetic_data_notice": "ALL data is synthetic (generated, fictional merchants/products). Results do not describe real catalogs or real people.",
        "environment": {"measured_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), "platform": platform.platform(), "cpu_count_logical": psutil.cpu_count(),
                        "ram_total_gb": round(psutil.virtual_memory().total / 1024**3, 1), "python": platform.python_version()},
        "workload": {"files": args.files, "rows_total": work.rows_total, "injected_defects": work.injected_counts, "seed": 777, "xlsx_files": sum(n.endswith(".xlsx") for n, _ in work.files)},
        "MEASURED": {
            "python_automation": {"runs": len(py_runs), "per_run": py_runs, "median_steps_s": py_med, "median_total_s": py_total_med, "sum_of_median_steps_s": roi.total(py_med),
                                  "rows_per_hour": roi.rows_per_hour(work.rows_total, py_total_med), "peak_rss_mb_max": max(r["peak_rss_mb"] for r in py_runs)},
            "ui_rpa": {"runs": len(rpa_runs), "per_run": rpa_runs, "median_steps_s": rpa_med, "sum_of_median_steps_s": roi.total(rpa_med) if rpa_med else None,
                       "rows_per_hour": roi.rows_per_hour(work.rows_total, roi.total(rpa_med)) if rpa_med else None, "note": "Browser-driven; excludes human reading/thinking time, so it is a LOWER BOUND for a manual click-through."},
            "detection_accuracy_vs_ground_truth": acc,
            "scale_test_python_automation": scale,
        },
        "ESTIMATED": None,
    }
    out["ESTIMATED"] = estimates_block(out["workload"], out["MEASURED"])
    od = Path(args.out_dir)
    od.mkdir(parents=True, exist_ok=True)
    (od / "measurements.json").write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    (od / "measurements.md").write_text(render_md(out), encoding="utf-8")
    print(f"Wrote {od / 'measurements.json'}")
    return 0


def render_md(o: dict) -> str:
    m, e, w = o["MEASURED"], o["ESTIMATED"], o["workload"]
    L = ["# Automation measurements", "", f"> {o['synthetic_data_notice']}", "",
         f"Workload: {w['files']} files ({w['xlsx_files']} xlsx), {w['rows_total']:,} rows, injected defects {w['injected_defects']}.",
         f"Machine: {o['environment']['platform']}, {o['environment']['cpu_count_logical']} logical CPUs, {o['environment']['ram_total_gb']} GB, Python {o['environment']['python']}; measured {o['environment']['measured_at_utc']} UTC.", "",
         "Legend: **MEASURED** = timed by running the code; **ESTIMATED** = assumption-based (manual work was never timed).", "",
         "## Time per task (seconds)", "", "| Task | Manual (ESTIMATED) | Python automation (MEASURED, median; review row ESTIMATED) | UI RPA (MEASURED, median; review row ESTIMATED) | Time saved by Python | Speed-up |", "| --- | ---: | ---: | ---: | ---: | ---: |"]
    for r in e["comparison_table"]:
        sv = f"{r['python_time_saved_pct']}%" if r["python_time_saved_pct"] is not None else ""
        sp = f"{r['python_speedup_x']}x" if r["python_speedup_x"] is not None else ""
        L.append(f"| {r['task']} | {r['manual_estimated_s']} | {r['python_measured_s']} | {r['rpa_measured_s'] if r['rpa_measured_s'] is not None else 'n/a'} | {sv} | {sp} |")
    L += ["", f"Machine-only pipeline throughput: {m['python_automation']['rows_per_hour']:,} rows/hour (MEASURED). End-to-end including the estimated {e['automation_review_s']:.0f} s human review: "
          f"{e['python_end_to_end_rows_per_hour']:,} rows/hour vs manual {e['manual_rows_per_hour']:,} rows/hour (both ESTIMATED).",
          f"Estimated end-to-end time saved: {e['time_saved_pct_python']}% ({e['speedup_x_python']}x, productivity gain {e['productivity_gain_pct_python']}%). Machine-time-only comparison would be {e['time_saved_pct_python_machine_only']}% / {e['speedup_x_python_machine_only']}x and is NOT the headline because it ignores the human review.",
          f"Estimated monthly time saved: {e['monthly_saved_hours_at_4_runs']} h at 4 runs/month, {e['monthly_saved_hours_at_20_runs']} h at 20 runs/month.", "",
          "## Detection accuracy vs injected ground truth (MEASURED; the baseline is a simulated Excel-style procedure, not people)", "", "| Defect | Injected | Automation recall / precision | Excel-style baseline recall / precision | Missed (auto vs baseline) |", "| --- | ---: | ---: | ---: | ---: |"]
    for k, v in m["detection_accuracy_vs_ground_truth"].items():
        if k == "TOTAL_omissions":
            continue
        a, b = v["automation"], v["excel_style_baseline"]
        L.append(f"| {k} | {a['injected']} | {a.get('recall', '')} / {a.get('precision', '')} | {b.get('recall', '')} / {b.get('precision', '')} | {a['missed']} vs {b['missed']} |")
    t = m["detection_accuracy_vs_ground_truth"]["TOTAL_omissions"]
    L += ["", f"Total omissions: automation {t['automation_missed']} vs baseline {t['baseline_missed']} -> omission reduction {t['omission_reduction_pct']}%.", ""]
    s_ = m["scale_test_python_automation"]
    if s_:
        L += ["## Scale test (MEASURED, Python automation)", "", f"{s_['rows']:,} rows in {s_['files']} files (largest {s_['largest_file_mb']} MB): {s_['seconds_total']} s total, {s_['rows_per_second']} rows/s, peak RSS {s_['peak_rss_mb']} MB. "
              f"Manual ESTIMATE for the same work: {s_['manual_estimated_total_s']:,} s.", ""]
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
