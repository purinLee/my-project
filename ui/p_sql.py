import plotly.express as px
import streamlit as st

from catalog_intel import analytics
from catalog_intel.db import UnsafeSQLError, run_readonly_query, validate_readonly_sql
from catalog_intel.reports import to_csv_bytes
from catalog_intel.requests_mgmt import load_requests
from ui.common import XLSX_MIME, cached_audit, data_version, db, download_table, now, report_builders, page_header, section, show_fig

SCHEMA_REF = """merchants(merchant_id, name, tier, region, vertical, onboarded_at)
products(product_id, merchant_id, sku, title, brand, category, price, currency, image_url, description, status, created_at, updated_at)
merchant_requests(request_id, merchant_id, subject, description, request_type, priority, status, channel, assignee, affected_items,
                  created_at, due_at, first_response_at, resolved_at, rework_count, classification_source, classification_confidence)
request_events(event_id, request_id, event_type, from_status, to_status, actor, note, created_at)
upload_log(upload_id, merchant_id, filename, rows_total, rows_valid, rows_rejected, rows_inserted, rows_updated, created_at)
llm_call_log(call_id, created_at, purpose, model, status, latency_ms, prompt_tokens, completion_tokens, cost_usd, error)"""

DEFAULT_SQL = """SELECT request_type,
       COUNT(*) AS completed,
       ROUND(AVG((julianday(resolved_at) - julianday(created_at)) * 24), 1) AS avg_hours,
       ROUND(100.0 * SUM(rework_count > 0) / COUNT(*), 1) AS rework_pct
FROM merchant_requests
WHERE resolved_at IS NOT NULL
GROUP BY request_type
ORDER BY avg_hours DESC"""

CHARTS = {  # query key -> (x, y, kind)
    "processing_time_by_type": ("request_type", "avg_hours", "bar"), "rework_by_type": ("request_type", "rework_rate_pct", "bar"),
    "sla_by_priority": ("priority", "compliance_pct", "bar"), "weekly_volume": ("week", ["created", "resolved"], "line"),
    "status_funnel": ("status", "requests", "bar"), "stage_durations": ("stage", "avg_hours", "bar"),
    "assignee_workload": ("assignee", ["open_requests", "past_due"], "bar"), "catalog_by_merchant": ("merchant", "products", "bar"),
    "merchant_leaderboard": ("merchant", "requests", "bar"),
}


def render() -> None:
    page_header("SQL Operations Analytics", "SQL analytics workbench", "Every metric is backed by a visible SQL query. Run your own read-only SQL and verify KPIs against an independent calculation.")
    t = now()
    tab_lib, tab_con, tab_ver, tab_exp = st.tabs(["Query library", "SQL console", "Verification", "Export center"])

    with tab_lib:
        key = st.selectbox("Analytics query", list(analytics.QUERIES), format_func=lambda k: analytics.QUERIES[k].title)
        q = analytics.QUERIES[key]
        st.caption(q.description)
        left, right = st.columns([1, 1.15])
        with left:
            st.code(q.sql, language="sql")
        with db(readonly=True) as c:
            df = analytics.run_query(c, key, t)
        with right:
            if key in CHARTS and not df.empty:
                x, y, kind = CHARTS[key]
                fig = (px.line(df, x=x, y=y, markers=True) if kind == "line" else px.bar(df, x=x, y=y, barmode="group"))
                fig.update_layout(xaxis_title=None, yaxis_title=None, legend_title=None)
                show_fig(fig, 300, q.title)
        st.dataframe(df, hide_index=True, width="stretch", height=300)
        download_table(df, key, "Download result", key=f"lib_{key}", sheet=key[:31])

    with tab_con:
        st.caption("Read-only console: a single SELECT / WITH statement; executed on a read-only SQLite connection; results capped at 1,000 rows. Bind `:now` is available.")
        sql = st.text_area("SQL", value=DEFAULT_SQL, height=200, max_chars=4000, key="console_sql")
        with st.expander("Schema reference"):
            st.code(SCHEMA_REF, language="text")
        if st.button("Run query", type="primary"):
            try:
                validate_readonly_sql(sql)
                res = run_readonly_query(sql, {"now": t.strftime("%Y-%m-%d %H:%M:%S")})
                st.success(f"{len(res):,} row(s)")
                st.dataframe(res, hide_index=True, width="stretch")
                download_table(res, "query_result", "Download result", key="console_dl", sheet="Result")
            except UnsafeSQLError as exc:
                st.error(str(exc))

    with tab_ver:
        st.markdown("Each headline KPI is computed twice: once by a SQL aggregate and once by an independent pandas calculation over the raw rows.")
        with db(readonly=True) as c:
            rec = analytics.reconcile(c, t)
        rec["status"] = rec["match"].map({True: "match", False: "MISMATCH"})
        st.dataframe(rec[["metric", "sql_value", "pandas_value", "status"]], hide_index=True, width="stretch")
        if rec["match"].all():
            st.success(f"All {len(rec)} reconciliation checks match.")
        else:
            st.error("Reconciliation mismatch detected - investigate before trusting these KPIs.")
        section("Referential integrity checks")
        with db(readonly=True) as c:
            checks = {
                "Requests without a merchant": c.execute("SELECT COUNT(*) FROM merchant_requests r LEFT JOIN merchants m USING (merchant_id) WHERE m.merchant_id IS NULL").fetchone()[0],
                "Products without a merchant": c.execute("SELECT COUNT(*) FROM products p LEFT JOIN merchants m USING (merchant_id) WHERE m.merchant_id IS NULL").fetchone()[0],
                "Events without a request": c.execute("SELECT COUNT(*) FROM request_events e LEFT JOIN merchant_requests r USING (request_id) WHERE r.request_id IS NULL").fetchone()[0],
                "Resolved requests with due_at before created_at": c.execute("SELECT COUNT(*) FROM merchant_requests WHERE due_at < created_at").fetchone()[0],
            }
        st.dataframe([{"check": k, "violations": v, "result": "pass" if v == 0 else "FAIL"} for k, v in checks.items()], hide_index=True, width="stretch")

    with tab_exp:
        st.markdown("One-click report exports. CSV cells that start with `=`, `+`, `-` or `@` are escaped to prevent spreadsheet formula injection.")
        with db(readonly=True) as c:
            reqs = load_requests(c, t)
            sla = analytics.run_query(c, "sla_by_priority", t)
            audit = cached_audit(data_version(), 3.5, 0.90)
        html_report, xlsx_report = report_builders(audit, t)
        sla_report = reqs[reqs["sla_state"] != "N/A"][["request_id", "merchant_name", "request_type", "priority", "status", "created_at", "due_at", "resolved_at", "processing_hours", "sla_state"]]
        download_table(reqs.drop(columns=["description"]), "requests", "Requests", sheet="Requests")
        download_table(sla_report, "sla_report", "SLA report", sheet="SLA")
        download_table(audit.issues, "quality_findings", "Quality findings", sheet="Findings")
        e1, e2, _ = st.columns([1, 1, 3])
        e1.download_button("Executive report (HTML)", html_report, "executive_report.html", "text/html", type="primary", key="ex_html", on_click="ignore")
        e2.download_button("Executive report (Excel)", xlsx_report, "executive_report.xlsx", XLSX_MIME, type="primary", key="ex_xlsx", on_click="ignore")
        st.dataframe(sla, hide_index=True, width="stretch")
