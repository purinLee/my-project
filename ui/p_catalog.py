import pandas as pd
import streamlit as st

from catalog_intel.config import CURRENCIES
from catalog_intel.quality import load_products
from catalog_intel.reports import to_csv_bytes
from catalog_intel.validation import SAMPLE_CSV, UploadError, import_products, validate_csv_bytes, validate_dataframe
from catalog_intel.db import query_df
from ui.common import db, flash, fmt_num, kpi_row, page_header, section, show_fig, show_flash
import plotly.express as px


def render() -> None:
    page_header("Catalog Management", "Product catalog", "Browse merchant catalogs, upload CSV files with row-level validation, and add products.")
    show_flash()
    with db(readonly=True) as c:
        products = load_products(c, include_archived=True)
        merchants = query_df(c, "SELECT merchant_id, name FROM merchants ORDER BY merchant_id")
        history = query_df(c, "SELECT u.created_at, m.name AS merchant, u.filename, u.rows_total, u.rows_valid, u.rows_rejected, u.rows_inserted, u.rows_updated FROM upload_log u JOIN merchants m USING (merchant_id) ORDER BY u.upload_id DESC LIMIT 50")
    active = products[products["status"] == "active"]
    kpi_row([
        ("Products", fmt_num(len(products)), f"<b>{len(active):,}</b> active", "#4f46e5"),
        ("Merchants", fmt_num(products["merchant_id"].nunique()), "with catalog data", "#06b6d4"),
        ("Categories", fmt_num(products["category"].nunique()), "distinct", "#8b5cf6"),
        ("Median price", f"${products['price'].median():,.2f}", f"Mean ${products['price'].mean():,.2f}", "#10b981"),
    ])
    names = dict(zip(merchants["merchant_id"], merchants["name"]))

    tab_browse, tab_upload, tab_add = st.tabs(["Browse catalog", "Upload CSV", "Add product"])

    with tab_browse:
        f1, f2, f3, f4 = st.columns([1.4, 1.2, 1, 1.4])
        sel_m = f1.multiselect("Merchant", list(names), format_func=lambda m: names[m], placeholder="All merchants")
        sel_c = f2.multiselect("Category", sorted(products["category"].dropna().unique()), placeholder="All categories")
        sel_s = f3.multiselect("Status", ["active", "draft", "archived"], default=["active", "draft"])
        text = f4.text_input("Search title / SKU", placeholder="e.g. Auralis or 002-WIR")
        view = products
        if sel_m:
            view = view[view["merchant_id"].isin(sel_m)]
        if sel_c:
            view = view[view["category"].isin(sel_c)]
        if sel_s:
            view = view[view["status"].isin(sel_s)]
        if text.strip():
            t = text.strip().lower()
            view = view[view["title"].str.lower().str.contains(t, regex=False) | view["sku"].str.lower().str.contains(t, regex=False)]
        st.caption(f"{len(view):,} of {len(products):,} products")
        st.dataframe(
            view[["merchant_name", "sku", "title", "brand", "category", "price", "currency", "status", "image_url", "updated_at"]],
            hide_index=True, width="stretch", height=430,
            column_config={"price": st.column_config.NumberColumn("Price", format="%.2f"), "merchant_name": "Merchant", "image_url": "Image URL"},
        )
        d1, d2 = st.columns([1, 3])
        d1.download_button("Export filtered CSV", to_csv_bytes(view.drop(columns=["product_id"])), "catalog_export.csv", "text/csv")
        if len(view):
            agg = view.groupby("category", as_index=False).agg(products=("sku", "count"), avg_price=("price", "mean"))
            fig = px.bar(agg.sort_values("products"), x="products", y="category", orientation="h", color_discrete_sequence=["#4f46e5"])
            fig.update_layout(xaxis_title=None, yaxis_title=None)
            show_fig(fig, 300, "Products by category (filtered)")

    with tab_upload:
        st.markdown("Upload a CSV with at least **sku, title, price**. Optional: brand, category, currency, image_url, description, status. Max 5 MB / 20,000 rows. Existing SKUs are updated, new SKUs are inserted.")
        c1, c2 = st.columns([2, 1])
        mid = c1.selectbox("Target merchant", list(names), format_func=lambda m: f"{m} - {names[m]}", key="up_merchant")
        c2.download_button("Download CSV template", SAMPLE_CSV, "catalog_template.csv", "text/csv")
        file = st.file_uploader("CSV file", type=["csv"], accept_multiple_files=False, key="up_file")
        if file is not None:
            try:
                report = validate_csv_bytes(file.getvalue())
            except UploadError as exc:
                st.error(f"Upload rejected: {exc}")
                report = None
            if report is not None:
                m1, m2, m3 = st.columns(3)
                m1.metric("Rows read", report.total_rows)
                m2.metric("Valid", len(report.valid))
                m3.metric("Rejected", len(report.rejected))
                for w in report.warnings:
                    st.warning(w)
                if len(report.rejected):
                    st.markdown("**Rejected rows** (row numbers include the header row)")
                    st.dataframe(report.rejected, hide_index=True, width="stretch")
                    st.download_button("Download rejected rows", to_csv_bytes(report.rejected), "rejected_rows.csv", "text/csv")
                if report.ok:
                    st.markdown("**Preview of valid rows**")
                    st.dataframe(report.valid.head(20), hide_index=True, width="stretch")
                    if st.button(f"Import {len(report.valid):,} valid row(s) into {names[mid]}", type="primary"):
                        try:
                            with db() as wc:
                                res = import_products(wc, mid, report.valid, file.name, report.total_rows)
                            st.cache_data.clear()
                            flash("success", f"Imported: {res['inserted']} inserted, {res['updated']} updated, {len(report.rejected)} rejected.")
                            st.rerun()
                        except Exception as exc:  # noqa: BLE001 - surface any DB error to the user
                            st.error(f"Import failed, no changes were saved: {exc}")
                else:
                    st.error("No valid rows to import. Fix the errors above and re-upload.")
        section("Upload history")
        st.dataframe(history, hide_index=True, width="stretch") if len(history) else st.caption("No uploads yet.")

    with tab_add:
        with st.form("add_product", clear_on_submit=False):
            a1, a2 = st.columns(2)
            mid2 = a1.selectbox("Merchant", list(names), format_func=lambda m: f"{m} - {names[m]}")
            sku = a2.text_input("SKU *", max_chars=64)
            title = st.text_input("Title *", max_chars=200)
            b1, b2, b3, b4 = st.columns(4)
            brand = b1.text_input("Brand", max_chars=80)
            category = b2.text_input("Category", max_chars=80)
            price = b3.number_input("Price *", min_value=0.0, value=0.0, step=1.0, format="%.2f")
            currency = b4.selectbox("Currency", CURRENCIES)
            image = st.text_input("Image URL", max_chars=500, placeholder="https://...")
            desc = st.text_area("Description", max_chars=2000, height=90)
            if st.form_submit_button("Add product", type="primary"):
                row = pd.DataFrame([{"sku": sku, "title": title, "price": price, "brand": brand, "category": category, "currency": currency, "image_url": image, "description": desc}])
                try:
                    rep = validate_dataframe(row)
                    if not rep.ok:
                        st.error("Please fix: " + rep.rejected.iloc[0]["errors"])
                    else:
                        with db() as wc:
                            res = import_products(wc, mid2, rep.valid, "manual entry", 1)
                        st.cache_data.clear()
                        flash("success", f"Product {sku.strip()} {'created' if res['inserted'] else 'updated'}.")
                        st.rerun()
                except Exception as exc:  # noqa: BLE001
                    st.error(f"Could not save product: {exc}")
