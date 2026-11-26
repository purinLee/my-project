"""Merchant Catalog Intelligence - Streamlit entry point (Streamlit Community Cloud: main file = app.py)."""
import streamlit as st

st.set_page_config(page_title="Merchant Catalog Intelligence", page_icon=":material/insights:", layout="wide", initial_sidebar_state="expanded")

from catalog_intel import ai  # noqa: E402
from catalog_intel.config import ROOT, get_db_path  # noqa: E402
from catalog_intel.seed import seed_database  # noqa: E402
from ui import common  # noqa: E402
from ui import p_assistant, p_audit, p_catalog, p_overview, p_requests, p_sql  # noqa: E402

common.inject_css()
try:
    common.init_demo()
except Exception as exc:  # fail visibly but gracefully
    st.error("The demo database could not be initialised.")
    st.exception(exc)
    st.stop()

st.logo(str(ROOT / "assets" / "logo.svg"), size="large")

pages = [
    st.Page(p_overview.render, title="Executive Overview", icon=":material/dashboard:", url_path="overview", default=True),
    st.Page(p_catalog.render, title="Catalog Management", icon=":material/inventory_2:", url_path="catalog"),
    st.Page(p_audit.render, title="Catalog Quality Audit", icon=":material/fact_check:", url_path="audit"),
    st.Page(p_requests.render, title="Merchant Requests", icon=":material/support_agent:", url_path="requests"),
    st.Page(p_sql.render, title="SQL Operations Analytics", icon=":material/database:", url_path="sql"),
    st.Page(p_assistant.render, title="AI Operations Assistant", icon=":material/smart_toy:", url_path="assistant"),
]
nav = st.navigation(pages, position="sidebar")

with st.sidebar:
    st.divider()
    st.caption("DATA MODE")
    st.markdown(common.pill("Synthetic demo data", "info"), unsafe_allow_html=True)
    st.caption("AI MODE")
    st.markdown(common.pill("LLM + rules fallback" if ai.is_configured() else "Rules engine (no API key)", "good" if ai.is_configured() else "neutral"), unsafe_allow_html=True)
    st.caption(f"Database: `{get_db_path().name}`")
    with st.expander("Reset demo data"):
        st.caption("Regenerates the synthetic dataset with the default seed. Uploaded or edited data will be lost.")
        if st.button("Reset to seed data", key="reset_demo"):
            seed_database()
            st.cache_data.clear()
            common.flash("success", "Demo data regenerated from seed.")
            st.rerun()

nav.run()
