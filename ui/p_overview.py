import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from catalog_intel import analytics
from ui.common import XLSX_MIME, report_builders
from ui.common import (
    cached_audit, data_version, db, fmt_num, insight, kpi_row, now, page_header, section, show_fig, show_flash,
)
from catalog_intel.config import DEFAULT_SEED

STATUS_COLORS = {
    "New": "#94a3b8", "Triaged": "#06b6d4", "In Progress": "#4f46e5", "Waiting on Merchant": "#f59e0b", "In Review": "#8b5cf6",
    "Resolved": "#10b981", "Closed": "#047857", "Rejected": "#ef4444",
}


def render() -> None:
    page_header("Executive Overview", "Operations command center", "Request throughput, SLA health, rework and catalog quality at a glance.")
    show_flash()
    t = now()
    audit = cached_audit(data_version(), 3.5, 0.90)
    with db(readonly=True) as c:
        k = analytics.kpi_summary(c, t)
        sla = analytics.run_query(c, "sla_by_priority", t)
        rework = analytics.run_query(c, "rework_by_type", t)
        proc = analytics.run_query(c, "processing_time_by_type", t)
        pct = analytics.processing_time_percentiles(c)
        weekly = analytics.run_query(c, "weekly_volume", t)
        funnel = analytics.run_query(c, "status_funnel", t)
        board = analytics.run_query(c, "merchant_leaderboard", t)

    s = audit.summary
    sla_ok = (k["sla_compliance_pct"] or 0) >= 90
    kpi_row([
        ("Open requests", fmt_num(k["open_requests"]), f"<b>{k['p1_open']}</b> critical (P1) in queue", "#4f46e5"),
        ("SLA compliance", fmt_num(k["sla_compliance_pct"], "%"), f"Target 90% - <b style='color:{'#047857' if sla_ok else '#b45309'}'>{'on target' if sla_ok else 'below target'}</b>", "#10b981" if sla_ok else "#f59e0b"),
        ("Avg processing time", fmt_num(k["avg_processing_hours"], " h"), f"First response {fmt_num(k['avg_first_response_hours'], ' h')}", "#06b6d4"),
        ("Rework rate", fmt_num(k["rework_rate_pct"], "%"), "Completed, sent back 1x+", "#8b5cf6"),
        ("SLA breached (open)", fmt_num(k["breached_open"]), f"<b>{k['at_risk_open']}</b> more at risk", "#ef4444" if k["breached_open"] else "#10b981"),
        ("Catalog clean rate", fmt_num(s["clean_rate"], "%"), f"Avg quality score <b>{s['avg_quality_score']}</b>/100", "#f59e0b"),
    ])

    section("Insights and charts")
    left, right = st.columns([1.15, 1])
    with left:
        st.markdown("<div class='panel'><h4>Operational insights</h4>" + "".join(
            insight(sev, text) for sev, text in analytics.generate_insights(k, rework, sla)
        ) + "".join(
            insight("warning" if s["by_type"].get(n, 0) else "info", f"{s['by_type'].get(n, 0)} {n.lower()} finding(s) across {s['products_audited']:,} products.")
            for n in ("Duplicate Listing", "Price Anomaly", "Missing Image")
        ) + "</div>", unsafe_allow_html=True)
    with right:
        fig = go.Figure(go.Bar(
            x=funnel["requests"], y=funnel["status"], orientation="h", marker_color=[STATUS_COLORS.get(x, "#64748b") for x in funnel["status"]],
            text=funnel["requests"], textposition="outside", cliponaxis=False,
        ))
        fig.update_yaxes(autorange="reversed")
        fig.update_layout(showlegend=False, xaxis_title=None, yaxis_title=None)
        show_fig(fig, 335, "Requests by lifecycle status")

    c1, c2 = st.columns(2)
    with c1:
        w = weekly.tail(16)
        fig = go.Figure()
        fig.add_bar(x=w["week"], y=w["created"], name="Created", marker_color="#c7d2fe")
        fig.add_scatter(x=w["week"], y=w["resolved"], name="Resolved", mode="lines+markers", line=dict(color="#4f46e5", width=3))
        fig.update_layout(xaxis_title=None, yaxis_title="Requests", xaxis=dict(tickangle=-45))
        show_fig(fig, 320, "Weekly intake vs. completions (last 16 weeks)")
    with c2:
        fig = go.Figure(go.Bar(x=sla["priority"], y=sla["compliance_pct"], marker_color=["#ef4444" if v < 90 else "#10b981" for v in sla["compliance_pct"]],
                               text=[f"{v}%" for v in sla["compliance_pct"]], textposition="outside"))
        fig.add_hline(y=90, line_dash="dash", line_color="#64748b", annotation_text="90% target", annotation_position="top left")
        fig.update_layout(yaxis=dict(range=[0, 108], title="% within SLA"), xaxis_title=None, showlegend=False)
        show_fig(fig, 320, "SLA compliance by priority (completed requests)")

    c3, c4 = st.columns(2)
    with c3:
        d = pct.melt(id_vars="request_type", value_vars=["p50_hours", "p90_hours"], var_name="stat", value_name="hours")
        d["stat"] = d["stat"].map({"p50_hours": "Median (P50)", "p90_hours": "P90"})
        fig = px.bar(d, x="hours", y="request_type", color="stat", barmode="group", orientation="h", color_discrete_map={"Median (P50)": "#4f46e5", "P90": "#c7d2fe"})
        fig.update_layout(xaxis_title="Hours from creation to resolution", yaxis_title=None, legend_title=None)
        fig.update_yaxes(categoryorder="total ascending")
        show_fig(fig, 340, "Processing time by request type")
    with c4:
        fig = go.Figure(go.Bar(x=rework["rework_rate_pct"], y=rework["request_type"], orientation="h", marker_color="#8b5cf6",
                               text=[f"{v}%" for v in rework["rework_rate_pct"]], textposition="outside", cliponaxis=False))
        fig.update_yaxes(autorange="reversed")
        fig.update_layout(xaxis_title="% of completed requests reworked", yaxis_title=None, showlegend=False)
        show_fig(fig, 340, "Rework rate by request type")

    section("Merchants")
    board = board.rename(columns={"sla_compliance_pct": "SLA %", "rework_rate_pct": "Rework %"})
    st.dataframe(
        board, hide_index=True, width="stretch",
        column_config={
            "SLA %": st.column_config.ProgressColumn("SLA %", min_value=0, max_value=100, format="%.1f"),
            "Rework %": st.column_config.NumberColumn("Rework %", format="%.1f"),
        },
    )
    html_report, xlsx_report = report_builders(audit, t)
    d1, d2, _ = st.columns([1, 1, 3])
    d1.download_button("Executive report (HTML)", html_report, "executive_report.html", "text/html", type="primary", key="ov_html", on_click="ignore")
    d2.download_button("Executive report (Excel)", xlsx_report, "executive_report.xlsx", XLSX_MIME, key="ov_xlsx", on_click="ignore")
    st.caption(f"Reference time (UTC): {t:%Y-%m-%d %H:%M}. Default seed: {DEFAULT_SEED}.")
