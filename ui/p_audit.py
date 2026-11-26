import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from ui.common import SEVERITY_COLORS, cached_audit, data_version, download_table, fmt_num, kpi_row, page_header, section, show_fig

ISSUE_ORDER = ["Duplicate Listing", "Price Anomaly", "Invalid Price", "Missing Image", "Incomplete Metadata"]


def render() -> None:
    page_header("Catalog Quality Audit", "Catalog quality audit", "Automated detection of duplicates, price anomalies, missing images and incomplete metadata.")
    with st.expander("Detection settings", expanded=False):
        s1, s2 = st.columns(2)
        z = s1.slider("Price anomaly sensitivity (modified z-score threshold)", 2.5, 6.0, 3.5, 0.1, help="Lower = more products flagged. Uses median/MAD of log-price per category.")
        dup = s2.slider("Near-duplicate similarity threshold", 0.80, 1.00, 0.90, 0.01, help="Titles with different numbers (model, size, capacity) are never matched.")
    a = cached_audit(data_version(), float(z), float(dup))
    s, iss = a.summary, a.issues
    bt = s["by_type"]
    kpi_row([
        ("Catalog clean rate", fmt_num(s["clean_rate"], "%"), f"<b>{s['products_flagged']:,}</b> of {s['products_audited']:,} products flagged", "#10b981"),
        ("Avg quality score", f"{s['avg_quality_score']}", "0-100, severity-weighted", "#4f46e5"),
        ("Duplicates", fmt_num(bt.get("Duplicate Listing", 0)), f"{len(a.duplicate_pairs)} candidate pairs", "#ef4444"),
        ("Price anomalies", fmt_num(bt.get("Price Anomaly", 0) + bt.get("Invalid Price", 0)), f"{bt.get('Invalid Price', 0)} invalid prices", "#f59e0b"),
        ("Missing images", fmt_num(bt.get("Missing Image", 0)), "none, invalid or placeholder URL", "#06b6d4"),
    ])
    st.caption("Audit scope: active and draft products (archived excluded). Duplicate detection runs within a single merchant.")

    c1, c2 = st.columns(2)
    with c1:
        if len(iss):
            g = iss.groupby(["issue_type", "severity"]).size().reset_index(name="count")
            fig = px.bar(g, x="count", y="issue_type", color="severity", orientation="h", color_discrete_map=SEVERITY_COLORS,
                         category_orders={"issue_type": ISSUE_ORDER, "severity": ["Critical", "High", "Medium", "Low"]})
            fig.update_layout(xaxis_title=None, yaxis_title=None, legend_title=None)
            fig.update_yaxes(autorange="reversed")
            show_fig(fig, 320, "Findings by type and severity")
    with c2:
        ms = a.merchant_scores
        fig = go.Figure(go.Bar(x=ms["avg_quality_score"], y=ms["merchant_name"].str.replace(" (Demo)", "", regex=False), orientation="h",
                               marker_color=["#ef4444" if v < 90 else "#f59e0b" if v < 95 else "#10b981" for v in ms["avg_quality_score"]],
                               text=ms["avg_quality_score"], textposition="outside", cliponaxis=False))
        fig.update_layout(xaxis=dict(range=[min(80, ms["avg_quality_score"].min() - 2), 101], title=None), yaxis_title=None, showlegend=False)
        show_fig(fig, 320, "Merchant quality scorecard (avg score)")

    tabs = st.tabs(["Duplicates", "Price anomalies", "Missing images", "Incomplete metadata", "All findings"])

    with tabs[0]:
        pairs = a.duplicate_pairs
        st.caption("Pairs are compared on normalised, order-insensitive titles. The older listing is kept; the newer one is flagged as the duplicate.")
        if pairs.empty:
            st.success("No duplicates found at this threshold.")
        else:
            mt = st.multiselect("Match type", pairs["match_type"].unique().tolist(), default=pairs["match_type"].unique().tolist(), key="dup_mt")
            view = pairs[pairs["match_type"].isin(mt)]
            st.dataframe(view, hide_index=True, width="stretch", height=380, column_config={"similarity": st.column_config.ProgressColumn("Similarity", min_value=0.8, max_value=1.0, format="%.3f")})
            download_table(view, "duplicate_pairs", "Export duplicate pairs", sheet="Duplicates")

    with tabs[1]:
        pa = iss[iss["issue_type"].isin(["Price Anomaly", "Invalid Price"])]
        prod = a.products.assign(price=pd.to_numeric(a.products["price"], errors="coerce"))
        flagged = set(pa["product_id"])
        prod = prod[prod["price"] > 0].assign(flag=lambda d: np.where(d["product_id"].isin(flagged), "Anomaly", "Normal"))
        fig = px.strip(prod, x="category", y="price", color="flag", log_y=True, color_discrete_map={"Anomaly": "#ef4444", "Normal": "#c7d2fe"}, hover_data=["sku", "merchant_name"])
        fig.update_layout(xaxis_title=None, yaxis_title="Price (log scale)", legend_title=None)
        show_fig(fig, 380, "Price distribution by category - anomalies highlighted")
        st.dataframe(pa.drop(columns=["product_id"]), hide_index=True, width="stretch", height=300)
        download_table(pa, "price_anomalies", "Export price anomalies", sheet="Price anomalies")

    with tabs[2]:
        mi = iss[iss["issue_type"] == "Missing Image"]
        by_m = mi.groupby("merchant_id").size().reset_index(name="missing").merge(a.merchant_scores[["merchant_id", "merchant_name", "products"]], on="merchant_id")
        by_m["missing_pct"] = (100 * by_m["missing"] / by_m["products"]).round(1)
        st.dataframe(by_m.sort_values("missing_pct", ascending=False)[["merchant_name", "products", "missing", "missing_pct"]], hide_index=True, width="stretch")
        st.dataframe(mi.drop(columns=["product_id"]), hide_index=True, width="stretch", height=300)
        download_table(mi, "missing_images", "Export missing images", sheet="Missing images")

    with tabs[3]:
        inc = iss[iss["issue_type"] == "Incomplete Metadata"]
        st.dataframe(inc.drop(columns=["product_id"]), hide_index=True, width="stretch", height=380)

    with tabs[4]:
        f1, f2, f3 = st.columns(3)
        ft = f1.multiselect("Issue type", sorted(iss["issue_type"].unique()), placeholder="All types")
        fs = f2.multiselect("Severity", ["Critical", "High", "Medium", "Low"], placeholder="All severities")
        fm = f3.multiselect("Merchant", sorted(iss["merchant_id"].unique()), placeholder="All merchants")
        view = iss
        if ft:
            view = view[view["issue_type"].isin(ft)]
        if fs:
            view = view[view["severity"].isin(fs)]
        if fm:
            view = view[view["merchant_id"].isin(fm)]
        st.caption(f"{len(view):,} findings")
        st.dataframe(view.drop(columns=["product_id"]), hide_index=True, width="stretch", height=400)
        download_table(view, "quality_findings", "Export findings", primary=True, sheet="Findings")
