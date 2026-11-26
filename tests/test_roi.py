import json
import statistics
from pathlib import Path

import pytest

from catalog_intel import roi
from catalog_intel.automation import ensure_sandbox, load_history, run_pipeline, Source
from catalog_intel.db import connect
from catalog_intel.workload import generate_workload

MEAS = Path(__file__).resolve().parent.parent / "docs" / "automation" / "measurements.json"


def test_manual_estimate_hand_calculated():
    est = roi.manual_estimate(rows=1000, files=10)
    assert est["collect_validate"] == 10 * 60 + 1000 * 0.5 == 1100
    assert est["import"] == 10 * 20 + 1000 * 0.05 == 250
    assert est["quality"] == 1000 * (0.6 + 0.4 + 0.3) == 1300
    assert est["report"] == 600 and est["export"] == 120
    assert roi.total(est) == 1100 + 250 + 1300 + 600 + 120


def test_multiplier_and_overrides():
    base = roi.manual_estimate(1000, 10)
    assert roi.total(roi.manual_estimate(1000, 10, multiplier=0.5)) == pytest.approx(roi.total(base) / 2, abs=0.05)
    assert roi.manual_estimate(100, 1, {"report_per_run": 5})["report"] == 5
    b = roi.manual_quality_breakdown(1000)
    assert b == {"duplicates": 600.0, "price": 400.0, "image": 300.0}


@pytest.mark.parametrize("before,after,saved,x,gain", [(100, 10, 90.0, 10.0, 900.0), (60, 30, 50.0, 2.0, 100.0), (50, 50, 0.0, 1.0, 0.0), (10, 20, -100.0, 0.5, -50.0)])
def test_savings_speedup_and_gain(before, after, saved, x, gain):
    assert roi.time_saved_pct(before, after) == saved
    assert roi.productivity_gain(before, after) == x
    assert roi.productivity_gain_pct(before, after) == gain


def test_division_by_zero_is_none_not_a_crash():
    assert roi.time_saved_pct(0, 5) is None and roi.productivity_gain(5, 0) is None and roi.productivity_gain_pct(5, 0) is None
    assert roi.rows_per_hour(100, 0) is None and roi.reduction_pct(0, 0) is None and roi.scale_run_time(5, 0, 100) == 0.0


def test_reduction_throughput_monthly_and_scaling():
    assert roi.reduction_pct(200, 10) == 95.0 and roi.reduction_pct(10, 10) == 0.0
    assert roi.rows_per_hour(1000, 36) == 100000.0
    assert roi.monthly_savings_hours(7200, 3600, 4) == 4.0 and roi.monthly_savings_hours(100, 500, 4) == 0.0  # never negative
    assert roi.scale_run_time(10, 1000, 5000) == 50


def test_comparison_table_structure_total_and_review_rows():
    manual = {"collect_validate": 100, "import": 50, "quality": 200, "report": 30, "export": 20}
    py = {"collect_validate": 1, "import": 1, "quality": 2, "report": 0.5, "export": 0.5}
    rpa = {"collect_validate": 10, "import": 5, "quality": 10, "report": 3, "export": 2}
    t = roi.comparison_table(1000, manual, py, rpa)
    assert [r["kind"] for r in t] == ["task"] * 5 + ["total"]
    total = t[-1]
    assert (total["manual_estimated_s"], total["python_measured_s"], total["rpa_measured_s"]) == (400, 5.0, 30)
    assert total["python_time_saved_pct"] == 98.75 and total["python_speedup_x"] == 80.0 and total["rpa_time_saved_pct"] == 92.5
    assert roi.comparison_table(1000, manual, py)[0]["rpa_measured_s"] is None
    with_review = roi.comparison_table(1000, manual, py, rpa, review_s=95)
    assert [r["kind"] for r in with_review][-2:] == ["review", "total_incl_review"]
    end = with_review[-1]
    assert (end["manual_estimated_s"], end["python_measured_s"], end["rpa_measured_s"]) == (400, 100.0, 125)
    assert end["python_time_saved_pct"] == 75.0 and end["python_speedup_x"] == 4.0 and end["rpa_time_saved_pct"] == 68.75


# ---- committed measurement file: every derived number must be reproducible from the raw ones ----
@pytest.fixture(scope="module")
def meas():
    if not MEAS.exists():
        pytest.skip("measurements.json not generated yet")
    return json.loads(MEAS.read_text(encoding="utf-8"))


def test_measurement_file_labels_and_synthetic_notice(meas):
    assert "synthetic" in meas["synthetic_data_notice"].lower()
    assert {"MEASURED", "ESTIMATED"} <= set(meas)
    assert "NOT measured" in meas["ESTIMATED"]["basis"]


def test_measured_medians_recompute_from_raw_runs(meas):
    py = meas["MEASURED"]["python_automation"]
    for step, v in py["median_steps_s"].items():
        assert v == pytest.approx(statistics.median(r["steps"][step] for r in py["per_run"]), abs=1e-3)
    assert py["median_total_s"] == pytest.approx(statistics.median(r["seconds_total"] for r in py["per_run"]), abs=1e-3)
    rpa = meas["MEASURED"]["ui_rpa"]
    ok = [r for r in rpa["per_run"] if r["status"] == "success"]
    if ok:
        for step, v in rpa["median_steps_s"].items():
            assert v == pytest.approx(statistics.median(r["steps"][step] for r in ok), abs=1e-3)


def test_estimates_recompute_from_assumptions_and_measurements(meas):
    E, w = meas["ESTIMATED"], meas["workload"]
    steps = roi.manual_estimate(w["rows_total"], w["files"], E["manual_time_assumptions_s"])
    assert steps == E["manual_steps_s"] and roi.total(steps) == E["manual_total_s"]
    machine = meas["MEASURED"]["python_automation"]["sum_of_median_steps_s"]
    e2e = machine + E["automation_review_s"]
    assert E["python_end_to_end_s"] == pytest.approx(e2e, abs=1e-3) and E["python_machine_total_s"] == pytest.approx(machine, abs=1e-3)
    assert E["time_saved_pct_python"] == roi.time_saved_pct(E["manual_total_s"], E["python_end_to_end_s"])
    assert E["time_saved_pct_python_machine_only"] == roi.time_saved_pct(E["manual_total_s"], E["python_machine_total_s"])
    assert E["speedup_x_python"] == roi.productivity_gain(E["manual_total_s"], E["python_end_to_end_s"])
    assert E["monthly_saved_hours_at_4_runs"] == roi.monthly_savings_hours(E["manual_total_s"], E["python_end_to_end_s"], 4)
    last = E["comparison_table"][-1]
    assert last["kind"] == "total_incl_review" and last["manual_estimated_s"] == E["manual_total_s"] and last["python_measured_s"] == pytest.approx(e2e, abs=1e-2)
    assert E["time_saved_pct_python"] < E["time_saved_pct_python_machine_only"]  # the headline is the conservative one


def test_accuracy_numbers_are_internally_consistent(meas):
    acc = meas["MEASURED"]["detection_accuracy_vs_ground_truth"]
    for k in ("duplicate", "price_anomaly", "missing_image"):
        for who in ("automation", "excel_style_baseline"):
            v = acc[k][who]
            assert v["true_positives"] + v["missed"] == v["injected"] and v["recall"] == pytest.approx(v["true_positives"] / v["injected"], abs=1e-4)
    tot = acc["TOTAL_omissions"]
    assert tot["automation_missed"] == sum(acc[k]["automation"]["missed"] for k in ("duplicate", "price_anomaly", "missing_image", "invalid_rows"))
    assert tot["omission_reduction_pct"] == roi.reduction_pct(tot["baseline_missed"], tot["automation_missed"])
    assert meas["workload"]["injected_defects"]["invalid_rows"] == acc["invalid_rows"]["automation"]["injected"]


def test_history_kpis_match_sql_and_pandas(tmp_path):
    db = ensure_sandbox(tmp_path / "s.db")
    w = generate_workload(3, 120, seed=3)
    for seed in (3, 4):
        run_pipeline([Source(n, d) for n, d in generate_workload(3, 120, seed=seed).files], db)
    with connect(db, readonly=True) as c:
        hist = load_history(c)
        sql = c.execute("SELECT COUNT(*), SUM(rows_valid), SUM(files_imported), ROUND(SUM(seconds_total), 3) FROM automation_runs").fetchone()
        sql_by_status = dict(c.execute("SELECT status, COUNT(*) FROM automation_runs GROUP BY status").fetchall())
    assert (sql[0], sql[1], sql[2]) == (len(hist), hist["rows_valid"].sum(), hist["files_imported"].sum())
    assert sql[3] == pytest.approx(hist["seconds_total"].sum(), abs=1e-2)
    assert sql_by_status == hist["status"].value_counts().to_dict() and w.rows_total > 0
