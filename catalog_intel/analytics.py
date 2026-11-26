"""SQL-driven analytics. Every metric is computed by a documented SQL query (see QUERIES).

`:now` is always a bound parameter so results are reproducible for a given reference time.
Medians / percentiles are not available in SQLite, so those are derived with pandas from SQL output.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import pandas as pd

from .config import AT_RISK_FRACTION, fmt, utcnow
from .db import query_df

OPEN_SQL = "('New','Triaged','In Progress','Waiting on Merchant','In Review')"
DONE_SQL = "resolved_at IS NOT NULL"  # Resolved or Closed (rework reopens clear resolved_at)


@dataclass(frozen=True)
class Query:
    key: str
    title: str
    description: str
    sql: str


QUERIES: dict[str, Query] = {
    q.key: q
    for q in [
        Query(
            "processing_time_by_type", "Processing time by request type",
            "Average and maximum hours from creation to resolution, completed requests only.",
            """SELECT request_type,
       COUNT(*)                                                        AS completed,
       ROUND(AVG((julianday(resolved_at) - julianday(created_at)) * 24), 1) AS avg_hours,
       ROUND(MAX((julianday(resolved_at) - julianday(created_at)) * 24), 1) AS max_hours
FROM merchant_requests
WHERE resolved_at IS NOT NULL
GROUP BY request_type
ORDER BY avg_hours DESC""",
        ),
        Query(
            "rework_by_type", "Rework rate by request type",
            "Share of completed requests that were sent back to 'In Progress' at least once after review.",
            """SELECT request_type,
       COUNT(*)                                             AS completed,
       SUM(CASE WHEN rework_count > 0 THEN 1 ELSE 0 END)    AS reworked,
       ROUND(100.0 * SUM(CASE WHEN rework_count > 0 THEN 1 ELSE 0 END) / COUNT(*), 1) AS rework_rate_pct
FROM merchant_requests
WHERE resolved_at IS NOT NULL
GROUP BY request_type
ORDER BY rework_rate_pct DESC""",
        ),
        Query(
            "sla_by_priority", "SLA compliance by priority",
            "A completed request meets SLA when resolved_at <= due_at (due_at = created_at + priority SLA window).",
            """SELECT priority,
       COUNT(*)                                                   AS completed,
       SUM(CASE WHEN resolved_at <= due_at THEN 1 ELSE 0 END)     AS met,
       SUM(CASE WHEN resolved_at >  due_at THEN 1 ELSE 0 END)     AS missed,
       ROUND(100.0 * SUM(CASE WHEN resolved_at <= due_at THEN 1 ELSE 0 END) / COUNT(*), 1) AS compliance_pct
FROM merchant_requests
WHERE resolved_at IS NOT NULL
GROUP BY priority
ORDER BY priority""",
        ),
        Query(
            "weekly_volume", "Weekly request intake vs. completions",
            "Requests created and resolved per ISO-style week (SQLite %Y-%W).",
            """SELECT w.week, COALESCE(c.created, 0) AS created, COALESCE(r.resolved, 0) AS resolved
FROM (SELECT DISTINCT strftime('%Y-W%W', created_at) AS week FROM merchant_requests) w
LEFT JOIN (SELECT strftime('%Y-W%W', created_at) AS week, COUNT(*) AS created FROM merchant_requests GROUP BY 1) c ON c.week = w.week
LEFT JOIN (SELECT strftime('%Y-W%W', resolved_at) AS week, COUNT(*) AS resolved FROM merchant_requests WHERE resolved_at IS NOT NULL GROUP BY 1) r ON r.week = w.week
ORDER BY w.week""",
        ),
        Query(
            "status_funnel", "Requests by lifecycle status",
            "Current distribution of requests across lifecycle states.",
            """SELECT status, COUNT(*) AS requests
FROM merchant_requests
GROUP BY status
ORDER BY CASE status WHEN 'New' THEN 1 WHEN 'Triaged' THEN 2 WHEN 'In Progress' THEN 3 WHEN 'Waiting on Merchant' THEN 4
                     WHEN 'In Review' THEN 5 WHEN 'Resolved' THEN 6 WHEN 'Closed' THEN 7 ELSE 8 END""",
        ),
        Query(
            "backlog_aging", "Open backlog with SLA state",
            "Open requests ordered by urgency. hours_to_due is negative once the SLA has been breached.",
            f"""SELECT request_id, merchant_id, request_type, priority, status, assignee, created_at, due_at,
       ROUND((julianday(:now) - julianday(created_at)) * 24, 1) AS age_hours,
       ROUND((julianday(due_at) - julianday(:now)) * 24, 1)     AS hours_to_due
FROM merchant_requests
WHERE status IN {OPEN_SQL}
ORDER BY hours_to_due ASC""",
        ),
        Query(
            "merchant_leaderboard", "Merchant request load and outcomes",
            "Per-merchant volume, SLA compliance and rework for completed requests.",
            """SELECT m.name AS merchant, m.tier,
       COUNT(r.request_id)                                                          AS requests,
       SUM(CASE WHEN r.resolved_at IS NOT NULL THEN 1 ELSE 0 END)                   AS completed,
       ROUND(100.0 * SUM(CASE WHEN r.resolved_at <= r.due_at THEN 1 ELSE 0 END)
             / NULLIF(SUM(CASE WHEN r.resolved_at IS NOT NULL THEN 1 ELSE 0 END), 0), 1) AS sla_compliance_pct,
       ROUND(100.0 * SUM(CASE WHEN r.resolved_at IS NOT NULL AND r.rework_count > 0 THEN 1 ELSE 0 END)
             / NULLIF(SUM(CASE WHEN r.resolved_at IS NOT NULL THEN 1 ELSE 0 END), 0), 1) AS rework_rate_pct
FROM merchants m
LEFT JOIN merchant_requests r ON r.merchant_id = m.merchant_id
GROUP BY m.merchant_id
ORDER BY requests DESC""",
        ),
        Query(
            "assignee_workload", "Open workload by assignee",
            "Open requests per assignee, and how many are already past due.",
            f"""SELECT COALESCE(assignee, '(unassigned)') AS assignee,
       COUNT(*) AS open_requests,
       SUM(CASE WHEN due_at < :now THEN 1 ELSE 0 END) AS past_due
FROM merchant_requests
WHERE status IN {OPEN_SQL}
GROUP BY 1
ORDER BY open_requests DESC""",
        ),
        Query(
            "catalog_by_merchant", "Catalog size and price by merchant",
            "Non-archived products and average price per merchant.",
            """SELECT m.name AS merchant, COUNT(p.product_id) AS products, ROUND(AVG(p.price), 2) AS avg_price,
       SUM(CASE WHEN p.image_url IS NULL OR TRIM(p.image_url) = '' THEN 1 ELSE 0 END) AS blank_image_url
FROM merchants m LEFT JOIN products p ON p.merchant_id = m.merchant_id AND p.status != 'archived'
GROUP BY m.merchant_id
ORDER BY products DESC""",
        ),
    ]
}


def run_query(conn, key: str, now: datetime | None = None) -> pd.DataFrame:
    q = QUERIES[key]
    return query_df(conn, q.sql, {"now": fmt(now or utcnow())} if ":now" in q.sql else {})


def _scalar(conn, sql: str, params: dict | tuple = ()):
    return conn.execute(sql, params).fetchone()[0]


def kpi_summary(conn, now: datetime | None = None) -> dict:
    """Headline KPIs. Every figure is a single SQL aggregate (verified against pandas in the tests)."""
    now_s = fmt(now or utcnow())
    p = {"now": now_s}
    completed = _scalar(conn, f"SELECT COUNT(*) FROM merchant_requests WHERE {DONE_SQL}")
    met = _scalar(conn, f"SELECT COUNT(*) FROM merchant_requests WHERE {DONE_SQL} AND resolved_at <= due_at")
    reworked = _scalar(conn, f"SELECT COUNT(*) FROM merchant_requests WHERE {DONE_SQL} AND rework_count > 0")
    avg_h = _scalar(conn, f"SELECT AVG((julianday(resolved_at) - julianday(created_at)) * 24) FROM merchant_requests WHERE {DONE_SQL}")
    open_n = _scalar(conn, f"SELECT COUNT(*) FROM merchant_requests WHERE status IN {OPEN_SQL}")
    breached = _scalar(conn, f"SELECT COUNT(*) FROM merchant_requests WHERE status IN {OPEN_SQL} AND due_at < :now", p)
    at_risk = _scalar(
        conn,
        f"""SELECT COUNT(*) FROM merchant_requests WHERE status IN {OPEN_SQL} AND due_at >= :now
            AND (julianday(due_at) - julianday(:now)) < {AT_RISK_FRACTION} * (julianday(due_at) - julianday(created_at))""",
        p,
    )
    first_resp = _scalar(conn, "SELECT AVG((julianday(first_response_at) - julianday(created_at)) * 24) FROM merchant_requests WHERE first_response_at IS NOT NULL")
    return {
        "merchants": _scalar(conn, "SELECT COUNT(*) FROM merchants"),
        "products": _scalar(conn, "SELECT COUNT(*) FROM products WHERE status != 'archived'"),
        "total_requests": _scalar(conn, "SELECT COUNT(*) FROM merchant_requests"),
        "open_requests": open_n,
        "completed_requests": completed,
        "sla_compliance_pct": round(100.0 * met / completed, 1) if completed else None,
        "rework_rate_pct": round(100.0 * reworked / completed, 1) if completed else None,
        "avg_processing_hours": round(avg_h, 1) if avg_h is not None else None,
        "avg_first_response_hours": round(first_resp, 1) if first_resp is not None else None,
        "breached_open": breached,
        "at_risk_open": at_risk,
        "p1_open": _scalar(conn, f"SELECT COUNT(*) FROM merchant_requests WHERE priority='P1' AND status IN {OPEN_SQL}"),
    }


def processing_time_percentiles(conn) -> pd.DataFrame:
    """P50/P90 processing hours by type (computed in pandas: SQLite has no percentile function)."""
    df = query_df(conn, "SELECT request_type, (julianday(resolved_at) - julianday(created_at)) * 24 AS hours FROM merchant_requests WHERE resolved_at IS NOT NULL")
    if df.empty:
        return pd.DataFrame(columns=["request_type", "p50_hours", "p90_hours"])
    g = df.groupby("request_type")["hours"]
    out = pd.DataFrame({"p50_hours": g.quantile(0.5), "p90_hours": g.quantile(0.9)}).round(1).reset_index()
    return out.sort_values("p90_hours", ascending=False, ignore_index=True)


def generate_insights(kpis: dict, by_type_rework: pd.DataFrame, sla_priority: pd.DataFrame) -> list[tuple[str, str]]:
    """Rule-based operational insights as (severity, text). Severity: critical | warning | info | positive."""
    out: list[tuple[str, str]] = []
    if kpis["breached_open"]:
        out.append(("critical", f"{kpis['breached_open']} open request(s) have already breached SLA - escalate to the owning assignees."))
    if kpis["at_risk_open"]:
        out.append(("warning", f"{kpis['at_risk_open']} open request(s) will breach within the final 25% of their SLA window."))
    sla = kpis["sla_compliance_pct"]
    if sla is not None:
        out.append(("positive" if sla >= 90 else "warning", f"Overall SLA compliance is {sla}% ({'meets' if sla >= 90 else 'below'} the 90% target)."))
    if not sla_priority.empty:
        worst = sla_priority.sort_values("compliance_pct").iloc[0]
        if worst["compliance_pct"] < 90:
            out.append(("warning", f"Weakest SLA tier is {worst['priority']} at {worst['compliance_pct']}% across {int(worst['completed'])} completed requests."))
    if not by_type_rework.empty:
        top = by_type_rework.iloc[0]
        out.append(("info", f"'{top['request_type']}' has the highest rework rate ({top['rework_rate_pct']}%) - review intake checklists for this type."))
    return out
