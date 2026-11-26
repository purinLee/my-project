import json
import time

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from catalog_intel import roi
from catalog_intel.automation import Source, ensure_sandbox, load_history, reset_sandbox, run_pipeline
from catalog_intel.config import ROOT, get_db_path
from catalog_intel.db import query_df
from catalog_intel.seed import MERCHANTS
from catalog_intel.workload import generate_workload
from ui.common import db, download_table, fmt_num, kpi_row, page_header, pill, section, show_fig

MEASUREMENTS = ROOT / "docs" / "automation" / "measurements.json"
COLORS = {"manual": "#f59e0b", "python": "#4f46e5", "rpa": "#06b6d4"}


@st.cache_data(show_spinner=False)
def load_measurements(mtime: float) -> dict | None:
    return json.loads(MEASUREMENTS.read_text(encoding="utf-8")) if MEASUREMENTS.exists() else None


def _legend() -> None:
    st.markdown(
        pill("MEASURED = timed by running the code", "good") + " &nbsp; " + pill("ESTIMATED = assumption-based (manual work was never timed)", "warn")
        + " &nbsp; " + pill("SYNTHETIC data only", "info"), unsafe_allow_html=True,
    )


def render() -> None:
    page_header("Automation Performance", "Automation impact dashboard", "Python automation and UI-driven RPA versus an estimated manual workflow, on an identical synthetic workload.")
    _legend()
    m = load_measurements(MEASUREMENTS.stat().st_mtime if MEASUREMENTS.exists() else 0.0)
    with db(readonly=True) as c:
        hist = load_history(c, 200)
    if m is None:
        st.warning("No measurement file found. Run `python scripts/measure_automation.py` to create docs/automation/measurements.json.")

    tab_ba, tab_q, tab_m, tab_run, tab_hist = st.tabs(["Before / after", "Quality and errors", "Monthly estimate", "Run automation", "Run history"])

    with tab_ba:
        if m:
            _before_after(m, hist)
    with tab_q:
        if m:
            _quality(m)
    with tab_m:
        if m:
            _monthly(m)
    with tab_run:
        _run_panel()
    with tab_hist:
        _history(hist)


def _before_after(m: dict, hist: pd.DataFrame) -> None:
    w, E, M = m["workload"], m["ESTIMATED"], m["MEASURED"]
    py, rpa = M["python_automation"], M["ui_rpa"]
    rows_done = int(hist["rows_valid"].sum()) if len(hist) else 0
    kpi_row([
        ("Time saved, end-to-end", fmt_num(E["time_saved_pct_python"], "%"), "<b>ESTIMATED</b> manual vs <b>MEASURED</b> run + est. human review", "#10b981"),
        ("Productivity gain", f"{E['speedup_x_python']:,.1f}x", f"+{E['productivity_gain_pct_python']:,.0f}% - manual time is an <b>estimate</b>", "#4f46e5"),
        ("Pipeline throughput", f"{py['rows_per_hour'] / 1e6:,.2f}M rows/h", "<b>MEASURED</b> machine time, median of %d runs" % py["runs"], "#06b6d4"),
        ("Rows processed (history)", fmt_num(rows_done), f"<b>MEASURED</b> across {len(hist)} logged run(s)", "#8b5cf6"),
        ("Est. monthly time saved", f"{E['monthly_saved_hours_at_4_runs']:,.1f} h", "<b>ESTIMATE</b> at 4 runs/month (see Monthly tab)", "#f59e0b"),
    ])
    st.caption(f"Workload: {w['files']} files, {w['rows_total']:,} rows ({w['xlsx_files']} Excel, rest CSV), synthetic, seed {w['seed']}. Measured {m['environment']['measured_at_utc']} UTC on {m['environment']['platform']}, "
               f"{m['environment']['cpu_count_logical']} logical CPUs.")

    table = pd.DataFrame(E["comparison_table"])
    tasks = table[table["kind"] == "task"]
    fig = go.Figure()
    fig.add_bar(x=tasks["task"], y=tasks["manual_estimated_s"], name="Manual (ESTIMATED)", marker_color=COLORS["manual"], marker_pattern_shape="/")
    if tasks["rpa_measured_s"].notna().any():
        fig.add_bar(x=tasks["task"], y=tasks["rpa_measured_s"], name="UI RPA (MEASURED)", marker_color=COLORS["rpa"])
    fig.add_bar(x=tasks["task"], y=tasks["python_measured_s"], name="Python automation (MEASURED)", marker_color=COLORS["python"])
    fig.update_layout(barmode="group", yaxis=dict(type="log", title="Seconds (log scale)"), xaxis_title=None, legend_title=None)
    show_fig(fig, 380, "Time per task - before vs after (same workload)")

    c1, c2 = st.columns(2)
    with c1:
        tot = table[table["kind"] == "total_incl_review"].iloc[0]
        labels, vals, cols = ["Manual (ESTIMATED)", "Python (MEASURED)"], [tot["manual_estimated_s"], tot["python_measured_s"]], [COLORS["manual"], COLORS["python"]]
        if pd.notna(tot["rpa_measured_s"]):
            labels.insert(1, "UI RPA (MEASURED)")
            vals.insert(1, tot["rpa_measured_s"])
            cols.insert(1, COLORS["rpa"])
        fig = go.Figure(go.Bar(x=labels, y=vals, marker_color=cols, text=[f"{v:,.0f}s" for v in vals], textposition="outside"))
        fig.update_layout(yaxis=dict(type="log", title="Total seconds (log)"), showlegend=False)
        show_fig(fig, 330, "Total workflow time (incl. estimated human review after automation)")
    with c2:
        sv = tasks.copy()
        fig = go.Figure(go.Bar(x=sv["python_time_saved_pct"], y=sv["task"], orientation="h", marker_color=COLORS["python"],
                               text=[f"{v:.1f}%" for v in sv["python_time_saved_pct"]], textposition="outside", cliponaxis=False))
        fig.update_yaxes(autorange="reversed")
        fig.update_layout(xaxis=dict(range=[0, 112], title="% time saved by Python automation (vs ESTIMATED manual)"), yaxis_title=None, showlegend=False)
        show_fig(fig, 330, "Time saved by task")

    show = table.rename(columns={"manual_estimated_s": "Manual (ESTIMATED) s", "python_measured_s": "Python (MEASURED) s", "rpa_measured_s": "UI RPA (MEASURED) s",
                                 "python_time_saved_pct": "Python saved %", "python_speedup_x": "Speed-up x", "rpa_time_saved_pct": "RPA saved %"})
    st.dataframe(show, hide_index=True, width="stretch")
    st.caption(f"Headline numbers include {E['automation_review_s']:.0f} s of ESTIMATED human review per run that remains after automation; machine time alone would show "
               f"{E['time_saved_pct_python_machine_only']}% saved ({E['speedup_x_python_machine_only']:,.0f}x), which overstates the benefit. "
               "UI RPA is browser-driven and excludes human reading/thinking time, so it is a lower bound for a manual click-through. "
               "Manual time = unit-time assumptions x workload size (see the Monthly tab); it is an estimate, not a stopwatch measurement of people.")
    s = M["scale_test_python_automation"]
    if s:
        st.markdown(f"**Scale test (MEASURED, Python):** {s['rows']:,} rows in {s['files']} files in **{s['seconds_total']} s** ({s['rows_per_second']:,.0f} rows/s), peak memory {s['peak_rss_mb']} MB. "
                    f"Manual ESTIMATE for the same rows: {s['manual_estimated_total_s'] / 3600:,.1f} h.")


def _quality(m: dict) -> None:
    acc = m["MEASURED"]["detection_accuracy_vs_ground_truth"]
    tot = acc["TOTAL_omissions"]
    kpi_row([
        ("Omissions: automation", fmt_num(tot["automation_missed"]), "defects missed - <b>MEASURED</b>", "#10b981"),
        ("Omissions: Excel-style baseline", fmt_num(tot["baseline_missed"]), "same data - <b>simulated procedure</b>", "#f59e0b"),
        ("Omission reduction", fmt_num(tot["omission_reduction_pct"], "%"), "vs the baseline procedure (not vs people)", "#4f46e5"),
        ("Injected defects", fmt_num(tot["injected_total"]), "ground truth in the synthetic files", "#06b6d4"),
    ])
    names = {"duplicate": "Duplicate listings", "price_anomaly": "Price anomalies", "missing_image": "Missing images", "invalid_rows": "Invalid rows"}
    rows = []
    for k, label in names.items():
        a, b = acc[k]["automation"], acc[k]["excel_style_baseline"]
        rows.append({"defect": label, "injected": a["injected"], "automation_missed": a["missed"], "baseline_missed": b["missed"],
                     "automation_recall": a.get("recall"), "baseline_recall": b.get("recall"), "automation_precision": a.get("precision"), "baseline_precision": b.get("precision")})
    df = pd.DataFrame(rows)
    fig = go.Figure()
    fig.add_bar(x=df["defect"], y=df["baseline_missed"], name="Excel-style baseline (simulated procedure)", marker_color=COLORS["manual"], marker_pattern_shape="/")
    fig.add_bar(x=df["defect"], y=df["automation_missed"], name="Automation (MEASURED)", marker_color=COLORS["python"])
    fig.update_layout(barmode="group", yaxis_title="Defects missed", xaxis_title=None, legend_title=None)
    show_fig(fig, 340, "Missed defects on the same synthetic files")
    st.dataframe(df, hide_index=True, width="stretch")
    st.caption("The baseline encodes what built-in spreadsheet features can do (blank-cell counts, Remove Duplicates on trimmed titles, an IQR fence over the whole price column, blank-image filter). "
               "It is a deterministic procedure, not a measurement of any person; it shows how much a rule-based pipeline finds that simple spreadsheet checks do not.")


def _monthly(m: dict) -> None:
    w, M = m["workload"], m["MEASURED"]
    st.markdown("**ESTIMATE calculator.** Everything here is assumption-driven; only the automated run time comes from measurement (scaled linearly with rows).")
    c1, c2, c3 = st.columns(3)
    runs = c1.slider("Runs per month", 1, 60, 4)
    rows = c2.number_input("Rows per run", 100, 200_000, int(w["rows_total"]), step=500)
    mult = c3.slider("Manual-time multiplier (sensitivity)", 0.25, 3.0, 1.0, 0.25, help="1.0 = the default unit times; 0.5 = analysts are twice as fast as assumed.")
    with st.expander("Manual unit-time assumptions (seconds) - ESTIMATES, editable"):
        a = {}
        cols = st.columns(3)
        for i, (k, v) in enumerate(roi.DEFAULT_ASSUMPTIONS.items()):
            a[k] = cols[i % 3].number_input(k.replace("_", " "), 0.0, 5000.0, float(m["ESTIMATED"]["manual_time_assumptions_s"].get(k, v)), key=f"asm_{k}")
    files = max(1, round(rows / (w["rows_total"] / w["files"])))
    manual = roi.total(roi.manual_estimate(int(rows), files, a, mult))
    auto = roi.scale_run_time(M["python_automation"]["sum_of_median_steps_s"], w["rows_total"], int(rows)) + a["automation_review_per_run"]
    saved = roi.monthly_savings_hours(manual, auto, runs)
    kpi_row([
        ("Manual time per run", f"{manual / 3600:,.2f} h", "<b>ESTIMATE</b>", "#f59e0b"),
        ("Automated time per run", f"{auto / 60:,.2f} min", "<b>MEASURED</b> machine time (scaled linearly) + <b>est.</b> review", "#4f46e5"),
        ("Time saved / month", f"{saved:,.1f} h", f"<b>ESTIMATE</b> at {runs} runs", "#10b981"),
        ("Time saved %", fmt_num(roi.time_saved_pct(manual, auto), "%"), "<b>ESTIMATE</b>", "#06b6d4"),
    ])
    xs = list(range(1, 61))
    fig = go.Figure(go.Scatter(x=xs, y=[roi.monthly_savings_hours(manual, auto, r) for r in xs], mode="lines", line=dict(color=COLORS["python"], width=3)))
    fig.add_vline(x=runs, line_dash="dash", line_color="#64748b")
    fig.update_layout(xaxis_title="Runs per month", yaxis_title="Estimated hours saved per month", showlegend=False)
    show_fig(fig, 300, "Estimated monthly time saved (assumption-based)")
    st.caption("Linear scaling of the automated time is a simplification; the 100,000-row scale test in the first tab is the measured data point for large runs.")


def _run_panel() -> None:
    main_db = get_db_path()
    sandbox = main_db.parent / "automation_sandbox.db"
    st.markdown("Run the **Python automation** (collect -> validate -> import -> duplicate / price / image checks -> report -> CSV & Excel export) on synthetic files or your own. "
                "The default target is an isolated **sandbox catalog**, so the demo data is never modified.")
    target = st.radio("Target catalog", ["Sandbox (recommended, isolated)", "Demo catalog (modifies demo data)"], horizontal=True)
    use_main = target.startswith("Demo")
    if use_main:
        st.warning("Files will be imported into the demo catalog. Use **Reset demo data** in the sidebar afterwards to restore it.")
    source = st.radio("Input files", ["Generate synthetic files", "Upload my own CSV/XLSX files"], horizontal=True)
    sources: list[Source] = []
    default_merchant = None
    label = None
    if source.startswith("Generate"):
        c1, c2, c3 = st.columns(3)
        n_files = c1.slider("Files", 1, 10, 4)
        rows = c2.slider("Rows per file", 50, 500, 200, 50)
        fresh = c3.checkbox("New random data each run", value=True, help="Off = same seed every time. CSV files are then identical and skipped as already imported (idempotency by file hash); XLSX files embed a creation timestamp, so they are treated as new files and re-imported (an upsert by merchant + SKU, so no duplicate rows).")
        if st.button("Run Python automation", type="primary", key="run_py_gen"):
            seed = int(time.time()) % 1_000_000 if fresh else 777
            wl = generate_workload(n_files, rows, seed=seed)
            sources = [Source(n, d) for n, d in wl.files]
            label = f"synthetic ui run seed={seed} files={n_files} rows/file={rows}"
    else:
        default_merchant = st.selectbox("Merchant for files not named M-001_*.csv", [m[0] for m in MERCHANTS], format_func=lambda x: dict((m[0], f"{m[0]} - {m[1]}") for m in MERCHANTS)[x])
        files = st.file_uploader("Files (name them M-001_anything.csv to route by merchant)", type=["csv", "xlsx"], accept_multiple_files=True, key="auto_files")
        if st.button("Run Python automation", type="primary", key="run_py_upload", disabled=not files):
            sources = [Source(f.name, f.getvalue()) for f in files]
            label = "user-uploaded files"
    if sources:
        try:
            target_db = main_db if use_main else ensure_sandbox(sandbox)
            res = run_pipeline(sources, target_db, history_db=main_db, trigger="ui", target_label="demo" if use_main else "sandbox", workload_label=label, default_merchant=default_merchant)
            st.session_state["auto_result"] = res
            if use_main:
                st.cache_data.clear()
            st.rerun()
        except Exception as exc:  # noqa: BLE001
            st.error(f"Automation could not start: {exc}")
    res = st.session_state.get("auto_result")
    if res is not None:
        (st.success if res.status == "success" else st.warning if res.status in ("partial", "nothing_to_do") else st.error)(
            f"Run #{res.run_id}: **{res.status}** in {res.seconds_total:.2f} s - {res.totals['files_imported']} imported, {res.totals['files_skipped']} skipped (already imported), "
            f"{res.totals['files_failed']} failed; {res.totals['rows_valid']:,} valid rows, {res.totals['rows_rejected']} rejected." + (f" Error: {res.error}" if res.error else "")
        )
        kpi_row([
            ("Duplicates found", fmt_num(res.findings_run_scope["duplicates"]), "this run's rows", "#ef4444"),
            ("Price anomalies", fmt_num(res.findings_run_scope["price_anomalies"]), "this run's rows", "#f59e0b"),
            ("Missing images", fmt_num(res.findings_run_scope["missing_images"]), "this run's rows", "#06b6d4"),
            ("Rejected rows", fmt_num(res.totals["rows_rejected"]), "failed validation", "#8b5cf6"),
        ])
        st.dataframe(pd.DataFrame([{"step": k, "seconds": v} for k, v in res.step_seconds.items()]), hide_index=True, width="stretch")
        st.dataframe(pd.DataFrame([{"file": o.name, "merchant": o.merchant_id, "status": o.status, "rows": o.rows_total, "valid": o.rows_valid, "rejected": o.rows_rejected, "error": o.error} for o in res.files]),
                     hide_index=True, width="stretch")
        if res.outputs:
            st.download_button("Download all outputs (ZIP: HTML report, Excel report, CSV/Excel findings)", lambda: res.zip_bytes(), f"automation_run_{res.run_id}.zip", "application/zip", on_click="ignore", key="auto_zip")
    with st.expander("Sandbox maintenance"):
        st.caption(f"Sandbox database: `{sandbox.name}` (separate from the demo catalog).")
        if st.button("Reset sandbox (empty it)", key="reset_sandbox"):
            reset_sandbox(sandbox)
            st.session_state.pop("auto_result", None)
            st.rerun()


def _history(hist: pd.DataFrame) -> None:
    section("Automation run history")
    if hist.empty:
        st.info("No runs recorded yet. Use **Run automation** (or `python scripts/run_automation.py`, or the RPA bot with `--db`) to create entries.")
        return
    cols = ["run_id", "started_at", "mode", "trigger_source", "target", "status", "files_total", "files_imported", "files_failed", "files_skipped", "rows_valid", "rows_rejected",
            "duplicates", "price_anomalies", "missing_images", "seconds_total", "workload_label", "error"]
    st.dataframe(hist[cols], hide_index=True, width="stretch", height=360)
    fig = px.bar(hist.sort_values("run_id").tail(30), x="run_id", y="seconds_total", color="mode", color_discrete_map={"python": COLORS["python"], "rpa_ui": COLORS["rpa"]})
    fig.update_layout(xaxis_title="Run id", yaxis_title="Seconds", legend_title=None)
    show_fig(fig, 300, "Duration of the latest runs (all values MEASURED)")
    download_table(hist, "automation_runs", "Export history", sheet="Runs")
