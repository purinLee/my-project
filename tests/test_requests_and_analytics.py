from datetime import datetime, timedelta

import pandas as pd
import pytest

from catalog_intel import analytics
from catalog_intel.config import OPEN_STATUSES, TRANSITIONS
from catalog_intel.db import UnsafeSQLError, run_readonly_query, validate_readonly_sql
from catalog_intel.requests_mgmt import (
    ValidationError, add_sla_columns, assign_request, create_request, get_events, load_requests, transition_request,
)
from tests.conftest import AS_OF

NOW = AS_OF + timedelta(hours=3)


# ---------- lifecycle ----------
def test_create_request_classifies_prioritises_and_sets_due(wconn):
    res = create_request(wconn, "M-002", "Fix wrong price on listings", "Several listings show a wrong price, 300 products affected asap", 300, now=NOW)
    assert res["request_type"] == "Price Update" and res["priority"] in ("P1", "P2")
    row = wconn.execute("SELECT * FROM merchant_requests WHERE request_id=?", (res["request_id"],)).fetchone()
    assert row["status"] == "New" and row["classification_source"] == "rules"
    hours = {"P1": 8, "P2": 24}[row["priority"]]
    assert datetime.strptime(row["due_at"], "%Y-%m-%d %H:%M:%S") - NOW == timedelta(hours=hours)
    assert len(get_events(wconn, res["request_id"])) == 1


def test_manual_override_respected(wconn):
    res = create_request(wconn, "M-001", "Some subject here", "A long enough description", 0, request_type="Image Update", priority="P4", now=NOW)
    assert (res["request_type"], res["priority"], res["source"]) == ("Image Update", "P4", "manual")


@pytest.mark.parametrize(
    "kwargs",
    [dict(merchant_id="NOPE"), dict(subject="abc"), dict(subject="x" * 121), dict(description="short"), dict(description="d" * 2001),
     dict(affected_items=-1), dict(affected_items=2_000_000), dict(affected_items="5"), dict(channel="Pigeon"), dict(request_type="Bogus"), dict(priority="P9")],
)
def test_create_request_validation(wconn, kwargs):
    base = dict(merchant_id="M-001", subject="Valid subject", description="A valid long description", affected_items=1)
    with pytest.raises(ValidationError):
        create_request(wconn, **{**base, **kwargs})


def test_full_lifecycle_with_rework(wconn):
    rid = create_request(wconn, "M-003", "Onboard new products", "Please add 20 new products to our catalog", 20, now=NOW)["request_id"]
    t = NOW
    for to in ["Triaged", "In Progress", "In Review", "In Progress", "In Review", "Resolved", "Closed"]:
        t += timedelta(hours=1)
        transition_request(wconn, rid, to, "tester", now=t)
    row = wconn.execute("SELECT * FROM merchant_requests WHERE request_id=?", (rid,)).fetchone()
    assert row["status"] == "Closed" and row["rework_count"] == 1
    assert row["first_response_at"] is not None and row["resolved_at"] is not None
    events = get_events(wconn, rid)
    assert len(events) == 8 and (events["event_type"] == "rework").sum() == 1


def test_reopen_after_resolved_counts_as_rework_and_clears_resolved_at(wconn):
    rid = create_request(wconn, "M-003", "Update prices now", "Price update for 10 items", 10, now=NOW)["request_id"]
    for i, to in enumerate(["Triaged", "In Progress", "In Review", "Resolved", "In Progress"], start=1):
        transition_request(wconn, rid, to, now=NOW + timedelta(hours=i))
    row = wconn.execute("SELECT * FROM merchant_requests WHERE request_id=?", (rid,)).fetchone()
    assert row["rework_count"] == 1 and row["resolved_at"] is None


def test_invalid_transitions_rejected(wconn):
    rid = create_request(wconn, "M-003", "Update prices now", "Price update for 10 items", 10, now=NOW)["request_id"]
    with pytest.raises(ValidationError, match="Cannot move"):
        transition_request(wconn, rid, "Resolved")
    with pytest.raises(ValidationError, match="Unknown status"):
        transition_request(wconn, rid, "Nonsense")
    with pytest.raises(ValidationError, match="not found"):
        transition_request(wconn, "REQ-99999", "Triaged")
    transition_request(wconn, rid, "Rejected")
    with pytest.raises(ValidationError, match="terminal"):
        transition_request(wconn, rid, "Triaged")


def test_transition_table_is_closed_and_acyclic_exits():
    assert all(t in TRANSITIONS for targets in TRANSITIONS.values() for t in targets)
    assert TRANSITIONS["Closed"] == [] and TRANSITIONS["Rejected"] == []


def test_assign_request(wconn):
    rid = wconn.execute("SELECT request_id FROM merchant_requests LIMIT 1").fetchone()[0]
    assign_request(wconn, rid, "New Person")
    assert wconn.execute("SELECT assignee FROM merchant_requests WHERE request_id=?", (rid,)).fetchone()[0] == "New Person"
    with pytest.raises(ValidationError):
        assign_request(wconn, rid, "x")
    with pytest.raises(ValidationError):
        assign_request(wconn, "REQ-99999", "Valid Name")


def test_request_id_sequence_is_unique(wconn):
    ids = {create_request(wconn, "M-001", "Valid subject", "A valid long description", 1, now=NOW)["request_id"] for _ in range(3)}
    assert len(ids) == 3


# ---------- SLA states ----------
def test_sla_states_unit():
    now = datetime(2026, 1, 10, 12)
    rows = [
        # id, created, due, resolved, status
        ("a", "2026-01-10 10:00:00", "2026-01-10 18:00:00", None, "In Progress"),   # 6h left of 8h -> on track
        ("b", "2026-01-10 04:30:00", "2026-01-10 12:30:00", None, "In Progress"),   # 0.5h left < 2h -> at risk
        ("c", "2026-01-09 00:00:00", "2026-01-09 08:00:00", None, "New"),           # breached
        ("d", "2026-01-09 00:00:00", "2026-01-09 08:00:00", "2026-01-09 07:00:00", "Closed"),  # met
        ("e", "2026-01-09 00:00:00", "2026-01-09 08:00:00", "2026-01-09 09:00:00", "Resolved"),  # missed
        ("f", "2026-01-09 00:00:00", "2026-01-09 08:00:00", None, "Rejected"),      # n/a
    ]
    df = pd.DataFrame(rows, columns=["request_id", "created_at", "due_at", "resolved_at", "status"])
    out = add_sla_columns(df, now).set_index("request_id")["sla_state"].to_dict()
    assert out == {"a": "On Track", "b": "At Risk", "c": "Breached", "d": "Met", "e": "Missed", "f": "N/A"}


# ---------- SQL verification: SQL results == independent pandas computation ----------
@pytest.fixture()
def raw(conn):
    df = pd.read_sql_query("SELECT * FROM merchant_requests", conn)
    for c in ("created_at", "due_at", "resolved_at"):
        df[c] = pd.to_datetime(df[c])
    return df


def test_kpis_match_pandas(conn, raw):
    k = analytics.kpi_summary(conn, AS_OF)
    done = raw[raw["resolved_at"].notna()]
    assert k["completed_requests"] == len(done)
    assert k["sla_compliance_pct"] == round(100 * (done["resolved_at"] <= done["due_at"]).mean(), 1)
    assert k["rework_rate_pct"] == round(100 * (done["rework_count"] > 0).mean(), 1)
    assert k["avg_processing_hours"] == round(((done["resolved_at"] - done["created_at"]).dt.total_seconds() / 3600).mean(), 1)
    open_ = raw[raw["status"].isin(OPEN_STATUSES)]
    assert k["open_requests"] == len(open_)
    assert k["breached_open"] == int((open_["due_at"] < pd.Timestamp(AS_OF)).sum())
    assert k["total_requests"] == len(raw)


def test_breached_and_at_risk_match_python_sla_logic(conn):
    k = analytics.kpi_summary(conn, AS_OF)
    df = load_requests(conn, AS_OF)
    assert k["breached_open"] == (df["sla_state"] == "Breached").sum()
    assert k["at_risk_open"] == (df["sla_state"] == "At Risk").sum()


def test_processing_time_by_type_matches_pandas(conn, raw):
    sql = analytics.run_query(conn, "processing_time_by_type", AS_OF).set_index("request_type").sort_index()
    done = raw[raw["resolved_at"].notna()].assign(h=lambda d: (d["resolved_at"] - d["created_at"]).dt.total_seconds() / 3600)
    exp = done.groupby("request_type")["h"].agg(["count", "mean", "max"])
    assert (sql["completed"] == exp["count"]).all()
    assert ((sql["avg_hours"] - exp["mean"]).abs() <= 0.051).all()
    assert ((sql["max_hours"] - exp["max"]).abs() <= 0.051).all()


def test_rework_and_sla_by_group_match_pandas(conn, raw):
    done = raw[raw["resolved_at"].notna()]
    rw = analytics.run_query(conn, "rework_by_type").set_index("request_type").sort_index()
    exp = done.groupby("request_type").apply(lambda g: round(100 * (g["rework_count"] > 0).mean(), 1), include_groups=False)
    assert (rw["rework_rate_pct"] == exp).all()
    sla = analytics.run_query(conn, "sla_by_priority").set_index("priority").sort_index()
    exp = done.groupby("priority").apply(lambda g: round(100 * (g["resolved_at"] <= g["due_at"]).mean(), 1), include_groups=False)
    assert (sla["compliance_pct"] == exp).all()
    assert (sla["met"] + sla["missed"] == sla["completed"]).all()


def test_sql_totals_reconcile(conn):
    assert analytics.run_query(conn, "status_funnel")["requests"].sum() == conn.execute("SELECT COUNT(*) FROM merchant_requests").fetchone()[0]
    wk = analytics.run_query(conn, "weekly_volume")
    assert wk["created"].sum() == conn.execute("SELECT COUNT(*) FROM merchant_requests").fetchone()[0]
    assert wk["resolved"].sum() == conn.execute("SELECT COUNT(*) FROM merchant_requests WHERE resolved_at IS NOT NULL").fetchone()[0]
    lb = analytics.run_query(conn, "merchant_leaderboard")
    assert lb["requests"].sum() == conn.execute("SELECT COUNT(*) FROM merchant_requests").fetchone()[0]
    assert analytics.run_query(conn, "assignee_workload", AS_OF)["open_requests"].sum() == analytics.kpi_summary(conn, AS_OF)["open_requests"]


def test_percentiles_ordering(conn):
    p = analytics.processing_time_percentiles(conn)
    assert (p["p90_hours"] >= p["p50_hours"]).all()


def test_every_registered_query_runs(conn):
    for key in analytics.QUERIES:
        assert isinstance(analytics.run_query(conn, key, AS_OF), pd.DataFrame)


def test_events_are_consistent_with_requests(conn):
    bad = conn.execute(
        """SELECT COUNT(*) FROM merchant_requests r WHERE r.status !=
           (SELECT to_status FROM request_events e WHERE e.request_id = r.request_id ORDER BY created_at DESC, event_id DESC LIMIT 1)"""
    ).fetchone()[0]
    assert bad == 0
    rework = conn.execute(
        "SELECT COUNT(*) FROM (SELECT r.request_id FROM merchant_requests r WHERE r.rework_count != (SELECT COUNT(*) FROM request_events e WHERE e.request_id=r.request_id AND e.event_type='rework'))"
    ).fetchone()[0]
    assert rework == 0


def test_seed_realism_ranges(conn):
    k = analytics.kpi_summary(conn, AS_OF)
    assert 75 <= k["sla_compliance_pct"] <= 97 and 5 <= k["rework_rate_pct"] <= 30 and k["open_requests"] >= 20


# ---------- read-only SQL console ----------
@pytest.mark.parametrize(
    "sql",
    ["DROP TABLE products", "DELETE FROM products", "SELECT 1; DROP TABLE products", "UPDATE products SET price=0", "PRAGMA table_info(products)",
     "ATTACH DATABASE 'x' AS y", "", "   ", "-- just a comment", "INSERT INTO merchants VALUES (1)", "WITH x AS (SELECT 1) DELETE FROM products"],
)
def test_sql_console_rejects_unsafe(sql):
    with pytest.raises(UnsafeSQLError):
        validate_readonly_sql(sql)


def test_sql_console_runs_select_and_limits_rows(seeded):
    df = run_readonly_query("SELECT * FROM products", max_rows=5, path=seeded[0])
    assert len(df) == 5
    df = run_readonly_query("-- c\nWITH t AS (SELECT status FROM merchant_requests) SELECT COUNT(*) AS n FROM t;", path=seeded[0])
    assert df["n"][0] == 700


def test_sql_console_reports_sql_errors_gracefully(seeded):
    with pytest.raises(UnsafeSQLError, match="SQL error"):
        run_readonly_query("SELECT * FROM missing_table", path=seeded[0])
    with pytest.raises(UnsafeSQLError):
        run_readonly_query("SELECT :now", path=seeded[0])
