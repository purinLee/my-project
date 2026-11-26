import plotly.graph_objects as go
import streamlit as st

from catalog_intel import analytics
from catalog_intel.classifier import classify_request, recommend_priority
from catalog_intel.config import CHANNELS, OPEN_STATUSES, PRIORITIES, PRIORITY_LABELS, REQUEST_TYPES, SLA_HOURS, STATUSES, TRANSITIONS
from catalog_intel.db import query_df
from catalog_intel.requests_mgmt import ValidationError, allowed_transitions, assign_request, create_request, get_events, load_requests, transition_request
from ui.common import db, download_table, flash, fmt_num, kpi_row, now, page_header, pill, section, show_fig, show_flash

SLA_TONE = {"Breached": "bad", "At Risk": "warn", "On Track": "good", "Met": "good", "Missed": "bad", "N/A": "neutral"}


def _hours(h) -> str:
    if h is None or h != h:
        return "-"
    sign = "-" if h < 0 else ""
    h = abs(h)
    return f"{sign}{h:.1f}h" if h < 48 else f"{sign}{h / 24:.1f}d"


def render() -> None:
    page_header("Merchant Request Management", "Request queue and lifecycle", "Triage, assign and progress merchant requests with SLA tracking and automatic classification.")
    show_flash()
    t = now()
    with db(readonly=True) as c:
        df = load_requests(c, t)
        merchants = query_df(c, "SELECT merchant_id, name, tier FROM merchants ORDER BY merchant_id")
        stages = analytics.run_query(c, "stage_durations", t)
        assignees = [r[0] for r in c.execute("SELECT DISTINCT assignee FROM merchant_requests WHERE assignee IS NOT NULL ORDER BY 1")]
    opn = df[df["status"].isin(OPEN_STATUSES)]
    kpi_row([
        ("Open requests", fmt_num(len(opn)), f"{(opn['status'] == 'New').sum()} awaiting triage", "#4f46e5"),
        ("Breached", fmt_num((opn["sla_state"] == "Breached").sum()), "past SLA due time", "#ef4444"),
        ("At risk", fmt_num((opn["sla_state"] == "At Risk").sum()), "< 25% of SLA window left", "#f59e0b"),
        ("P1 open", fmt_num((opn["priority"] == "P1").sum()), f"SLA window {SLA_HOURS['P1']}h", "#8b5cf6"),
        ("Total requests", fmt_num(len(df)), f"{(df['status'].isin(['Resolved', 'Closed'])).sum()} completed", "#06b6d4"),
    ])
    tab_q, tab_new, tab_life = st.tabs(["Request queue", "New request", "Lifecycle and SLA policy"])

    with tab_q:
        f = st.columns([1.1, 1, 1.1, 1.3, 1.1, 1])
        sel_open = f[0].selectbox("Scope", ["Open only", "All requests", "Completed only"])
        sel_p = f[1].multiselect("Priority", PRIORITIES, placeholder="All")
        sel_t = f[2].multiselect("Type", REQUEST_TYPES, placeholder="All")
        sel_s = f[3].multiselect("SLA state", ["Breached", "At Risk", "On Track", "Met", "Missed"], placeholder="All")
        sel_a = f[4].multiselect("Assignee", assignees, placeholder="All")
        q = f[5].text_input("Search", placeholder="ID / subject")
        view = df
        if sel_open == "Open only":
            view = view[view["status"].isin(OPEN_STATUSES)]
        elif sel_open == "Completed only":
            view = view[view["status"].isin(["Resolved", "Closed"])]
        for col, sel in (("priority", sel_p), ("request_type", sel_t), ("sla_state", sel_s), ("assignee", sel_a)):
            if sel:
                view = view[view[col].isin(sel)]
        if q.strip():
            ql = q.strip().lower()
            view = view[view["request_id"].str.lower().str.contains(ql, regex=False) | view["subject"].str.lower().str.contains(ql, regex=False)]
        view = view.assign(_p=view["priority"].map({p: i for i, p in enumerate(PRIORITIES)})).sort_values(["_p", "hours_to_due"]).drop(columns="_p")
        st.caption(f"{len(view):,} request(s) - select a row to open details")
        cols = ["request_id", "priority", "status", "sla_state", "hours_to_due", "request_type", "merchant_name", "subject", "assignee", "rework_count", "created_at"]
        ev = st.dataframe(
            view[cols], hide_index=True, width="stretch", height=380, on_select="rerun", selection_mode="single-row", key="req_table",
            column_config={"hours_to_due": st.column_config.NumberColumn("Hours to due", format="%.1f"), "merchant_name": "Merchant", "sla_state": "SLA", "rework_count": st.column_config.NumberColumn("Rework", format="%d")},
        )
        download_table(view.drop(columns=["description"]), "requests_export", "Export queue", sheet="Requests")

        rows = ev.selection.rows if ev and ev.selection else []
        if rows and rows[0] < len(view):
            _detail(view.iloc[rows[0]], t)
        else:
            st.info("Select a request in the table to see its timeline and take action.")

    with tab_new:
        _new_request(merchants)

    with tab_life:
        l, r = st.columns([1.2, 1])
        with l:
            section("Lifecycle transitions")
            rows = [{"From": s, "Allowed next states": ", ".join(TRANSITIONS[s]) or "terminal"} for s in STATUSES]
            st.dataframe(rows, hide_index=True, width="stretch")
            st.caption("Moving back to 'In Progress' from 'In Review' or 'Resolved' counts as rework.")
        with r:
            section("SLA policy")
            st.dataframe([{"Priority": PRIORITY_LABELS[p], "Resolution target": f"{SLA_HOURS[p]} h"} for p in PRIORITIES], hide_index=True, width="stretch")
            st.caption("SLA clock runs on calendar hours from creation and is not paused while waiting on the merchant.")
        fig = go.Figure(go.Bar(x=stages["avg_hours"], y=stages["stage"], orientation="h", marker_color="#4f46e5", text=stages["avg_hours"], textposition="outside", cliponaxis=False))
        fig.update_yaxes(autorange="reversed")
        fig.update_layout(xaxis_title="Average hours spent before the next transition", yaxis_title=None, showlegend=False)
        show_fig(fig, 340, "Time spent in each lifecycle stage (from request_events)")


def _detail(row, t) -> None:
    rid = row["request_id"]
    st.markdown(f"### {rid}  &nbsp; {pill(row['priority'], 'bad' if row['priority'] == 'P1' else 'warn' if row['priority'] == 'P2' else 'neutral')} {pill(row['status'], 'info')} {pill(row['sla_state'], SLA_TONE[row['sla_state']])}", unsafe_allow_html=True)
    a, b = st.columns([1.3, 1])
    with a:
        st.markdown(f"**{row['subject']}**")
        st.write(row["description"])
        st.caption(
            f"{row['merchant_name']} ({row['merchant_tier']}) - {row['request_type']} - {row['affected_items']:,} items - channel {row['channel']} - "
            f"classified by {row['classification_source']}" + (f" ({row['classification_confidence']:.0%})" if row["classification_confidence"] == row["classification_confidence"] and row["classification_confidence"] is not None else "")
        )
        st.caption(f"Created {row['created_at']} - due {row['due_at']} ({_hours(row['hours_to_due'])} remaining) - assignee {row['assignee'] or 'unassigned'} - rework x{row['rework_count']}")
        with db(readonly=True) as c:
            events = get_events(c, rid)
        st.dataframe(events, hide_index=True, width="stretch", height=min(300, 40 + 35 * len(events)))
    with b:
        nxt = allowed_transitions(row["status"])
        with st.form(f"act_{rid}"):
            st.markdown("**Update status**")
            if nxt:
                to = st.selectbox("Move to", nxt)
                note = st.text_input("Note (optional)", max_chars=500)
                actor = st.text_input("Acting as", value="ops-analyst", max_chars=80)
                if st.form_submit_button("Apply transition", type="primary"):
                    try:
                        with db() as wc:
                            res = transition_request(wc, rid, to, actor, note)
                        flash("success", f"{rid}: {res['from']} -> {res['to']}" + (" (counted as rework)" if res["rework"] else ""))
                        st.rerun()
                    except ValidationError as exc:
                        st.error(" ".join(exc.errors))
            else:
                st.info(f"'{row['status']}' is a terminal state.")
                st.form_submit_button("Apply transition", disabled=True)
        with st.form(f"assign_{rid}"):
            st.markdown("**Assign**")
            who = st.text_input("Assignee", value=row["assignee"] or "", max_chars=80)
            if st.form_submit_button("Save assignee"):
                try:
                    with db() as wc:
                        assign_request(wc, rid, who)
                    flash("success", f"{rid} assigned to {who.strip()}.")
                    st.rerun()
                except ValidationError as exc:
                    st.error(" ".join(exc.errors))


def _new_request(merchants) -> None:
    names = {r.merchant_id: f"{r.merchant_id} - {r.name}" for r in merchants.itertuples()}
    tiers = dict(zip(merchants["merchant_id"], merchants["tier"]))
    left, right = st.columns([1.2, 1])
    with left:
        mid = st.selectbox("Merchant", list(names), format_func=lambda m: names[m], key="nr_m")
        subject = st.text_input("Subject", max_chars=120, key="nr_s", placeholder="e.g. CSV upload failing with validation errors")
        desc = st.text_area("Description", max_chars=2000, height=130, key="nr_d", placeholder="Describe the issue, affected products and any deadline.")
        c1, c2 = st.columns(2)
        items = c1.number_input("Affected items", min_value=0, max_value=1_000_000, value=0, step=10, key="nr_i")
        channel = c2.selectbox("Channel", CHANNELS, key="nr_c")
        o1, o2 = st.columns(2)
        ov_t = o1.selectbox("Override type (optional)", ["Auto-classify"] + REQUEST_TYPES, key="nr_ot")
        ov_p = o2.selectbox("Override priority (optional)", ["Auto-recommend"] + PRIORITIES, key="nr_op")
    with right:
        st.markdown("**Live analysis**")
        ready = len((subject or "").strip()) >= 5 and len((desc or "").strip()) >= 10
        if ready:
            cls = classify_request(subject, desc, use_llm=False)
            rtype = cls.request_type if ov_t == "Auto-classify" else ov_t
            rec = recommend_priority(subject, desc, rtype, tiers[mid], int(items))
            prio = rec.priority if ov_p == "Auto-recommend" else ov_p
            st.markdown(f"Type: {pill(rtype, 'info')} &nbsp; Confidence: {pill(f'{cls.confidence:.0%}', 'good' if cls.confidence >= 0.7 else 'warn')}<br>"
                        f"Priority: {pill(PRIORITY_LABELS[prio], 'bad' if prio == 'P1' else 'warn' if prio == 'P2' else 'neutral')} &nbsp; SLA: {SLA_HOURS[prio]}h", unsafe_allow_html=True)
            if cls.matched:
                st.caption("Matched keywords: " + ", ".join(dict.fromkeys(cls.matched)))
            st.markdown("**Why this priority**")
            for r in rec.reasons:
                st.markdown(f"- {r}")
            if st.button("Create request", type="primary", key="nr_go"):
                try:
                    with db() as wc:
                        res = create_request(wc, mid, subject, desc, int(items), channel, None if ov_t == "Auto-classify" else ov_t, None if ov_p == "Auto-recommend" else ov_p)
                    for k in ("nr_s", "nr_d"):
                        st.session_state.pop(k, None)
                    flash("success", f"Created {res['request_id']} as {res['request_type']} / {res['priority']} (via {res['source']}).")
                    st.rerun()
                except ValidationError as exc:
                    st.error(" ".join(exc.errors))
        else:
            st.info("Enter a subject (5+ chars) and description (10+ chars) to preview classification and priority.")
