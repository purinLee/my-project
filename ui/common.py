"""Shared Streamlit UI helpers: theme, components, cached data access."""
from __future__ import annotations

import html
import numbers
from contextlib import contextmanager

import pandas as pd
import plotly.graph_objects as go
import plotly.io as pio
import streamlit as st

from catalog_intel.config import DEMO_DATA_NOTICE, utcnow
from catalog_intel.db import connect
from catalog_intel.quality import run_audit
from catalog_intel.reports import executive_report_html, executive_report_xlsx, to_csv_bytes, to_excel_bytes
from catalog_intel.seed import ensure_demo_data

PALETTE = ["#4f46e5", "#06b6d4", "#f59e0b", "#10b981", "#ef4444", "#8b5cf6", "#64748b", "#ec4899"]
TONES = {
    "good": ("#047857", "#ecfdf5"), "warn": ("#b45309", "#fffbeb"), "bad": ("#b91c1c", "#fef2f2"),
    "info": ("#1d4ed8", "#eff6ff"), "neutral": ("#475569", "#f1f5f9"),
}
SEVERITY_COLORS = {"Critical": "#b91c1c", "High": "#ef4444", "Medium": "#f59e0b", "Low": "#94a3b8"}

pio.templates["ops"] = go.layout.Template(
    layout=go.Layout(
        colorway=PALETTE, font=dict(family="Segoe UI, Inter, Arial, sans-serif", size=12, color="#334155"),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)", margin=dict(l=8, r=8, t=36, b=8),
        title=dict(font=dict(size=14, color="#0f172a"), x=0.0, xanchor="left"),
        xaxis=dict(gridcolor="#e8ebf3", linecolor="#cbd5e1", zeroline=False),
        yaxis=dict(gridcolor="#e8ebf3", linecolor="#cbd5e1", zeroline=False),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1, font=dict(size=11)),
        hoverlabel=dict(bgcolor="#0f172a", font_color="#f8fafc"),
    )
)
pio.templates.default = "ops"

CSS = """
<style>
html, body, [class*="css"], .stApp { font-family: "Segoe UI", Inter, -apple-system, Roboto, Arial, sans-serif; }
.block-container { padding-top: 1.6rem; padding-bottom: 3rem; max-width: 1500px; }
[data-testid="stHeader"] { background: transparent; }
[data-testid="stSidebar"] { background: linear-gradient(180deg, #0b1220 0%, #111a2e 100%); border-right: 1px solid #1e293b; }
[data-testid="stSidebar"] * { color: #cbd5e1; }
[data-testid="stSidebar"] [data-testid="stSidebarNavLink"] { border-radius: 8px; margin: 2px 6px; padding: 6px 10px; }
[data-testid="stSidebar"] [data-testid="stSidebarNavLink"]:hover { background: #1e293b; }
[data-testid="stSidebar"] [data-testid="stSidebarNavLink"][aria-current="page"] { background: #312e81; }
[data-testid="stSidebar"] [data-testid="stSidebarNavLink"][aria-current="page"] * { color: #fff; font-weight: 600; }
[data-testid="stSidebar"] hr { border-color: #1e293b; }
[data-testid="stSidebar"] .stButton button { background: #1e293b; border: 1px solid #334155; color: #e2e8f0; }
[data-testid="stSidebar"] .stButton button:hover { border-color: #6366f1; color: #fff; }

.ph { display:flex; align-items:flex-end; justify-content:space-between; margin-bottom: 14px; gap: 16px; }
.ph h1 { font-size: 1.65rem; font-weight: 700; color:#0f172a; margin: 0; padding: 0; letter-spacing: -0.02em; }
.ph .sub { color:#64748b; font-size: .92rem; margin-top: 2px; }
.crumb { color:#6366f1; font-size:.72rem; font-weight:700; letter-spacing:.12em; text-transform:uppercase; margin-bottom:2px; }
.pill { border:1px solid rgba(100,116,139,.25); display:inline-block; padding: 3px 10px; border-radius: 999px; font-size:.72rem; font-weight:600; white-space:nowrap; }
.demo-strip { background:#eef2ff; border:1px solid #c7d2fe; color:#3730a3; border-radius:10px; padding:7px 14px; font-size:.8rem; margin-bottom:14px; }

.kpi { background:#fff; border:1px solid #e5e8f0; border-radius:14px; padding:16px 18px 14px; box-shadow:0 1px 2px rgba(15,23,42,.04); height:100%; position:relative; overflow:hidden; }
.kpi::before { content:""; position:absolute; left:0; top:0; bottom:0; width:4px; background:var(--accent,#4f46e5); }
.kpi .l { color:#64748b; font-size:.74rem; font-weight:600; letter-spacing:.06em; text-transform:uppercase; }
.kpi .v { color:#0f172a; font-size:1.85rem; font-weight:700; letter-spacing:-0.02em; line-height:1.25; margin-top:2px; }
.kpi .s { color:#64748b; font-size:.78rem; margin-top:2px; }
.kpi .s b { font-weight:600; }

.panel { background:#fff; border:1px solid #e5e8f0; border-radius:14px; padding:14px 18px; box-shadow:0 1px 2px rgba(15,23,42,.04); }
.panel h4 { margin:0 0 8px 0; font-size:.95rem; color:#0f172a; }
.insight { display:flex; gap:10px; align-items:flex-start; padding:9px 12px; border-radius:10px; margin-bottom:8px; font-size:.86rem; border:1px solid; }
.insight .dot { width:8px; height:8px; border-radius:50%; margin-top:6px; flex:0 0 8px; }
.sectionlabel { font-size:.74rem; font-weight:700; letter-spacing:.1em; text-transform:uppercase; color:#64748b; margin: 18px 0 8px; }

[data-testid="stTabs"] [role="tablist"] { gap: 4px; border-bottom: 1px solid #e2e8f0; }
[data-testid="stTab"] { padding: 10px 18px !important; font-weight: 600; color:#64748b; }
[data-testid="stTab"][aria-selected="true"] { color:#4338ca; }
[data-testid="stSidebar"] code { background:#1e293b !important; color:#e2e8f0 !important; }
[data-testid="stDataFrame"] { border:1px solid #e5e8f0; border-radius:10px; overflow:hidden; }
[data-testid="stForm"] { background:#fff; border:1px solid #e5e8f0; border-radius:14px; }
.stButton button[kind="primary"], .stDownloadButton button[kind="primary"] { background:#4f46e5; border-color:#4f46e5; }
.stButton button, .stDownloadButton button { border-radius: 9px; font-weight: 600; }
footer { visibility: hidden; }
</style>
"""


def inject_css() -> None:
    st.markdown(CSS, unsafe_allow_html=True)


def page_header(section: str, title: str, subtitle: str) -> None:
    st.markdown(
        f"<div class='ph'><div><div class='crumb'>{html.escape(section)}</div><h1>{html.escape(title)}</h1>"
        f"<div class='sub'>{html.escape(subtitle)}</div></div></div>",
        unsafe_allow_html=True,
    )
    st.markdown(f"<div class='demo-strip'><b>Synthetic demo data.</b> {html.escape(DEMO_DATA_NOTICE)}</div>", unsafe_allow_html=True)


def pill(text: str, tone: str = "neutral") -> str:
    fg, bg = TONES[tone]
    return f"<span class='pill' style='color:{fg};background:{bg}'>{html.escape(str(text))}</span>"


def kpi_card(label: str, value, sub: str = "", accent: str = "#4f46e5") -> str:
    return (
        f"<div class='kpi' style='--accent:{accent}'><div class='l'>{html.escape(label)}</div>"
        f"<div class='v'>{html.escape(str(value))}</div><div class='s'>{sub}</div></div>"
    )


def kpi_row(cards: list[tuple]) -> None:
    cols = st.columns(len(cards))
    for col, c in zip(cols, cards):
        col.markdown(kpi_card(*c), unsafe_allow_html=True)


def insight(severity: str, text: str) -> str:
    fg, bg = {"critical": TONES["bad"], "warning": TONES["warn"], "positive": TONES["good"], "info": TONES["info"]}.get(severity, TONES["neutral"])
    return f"<div class='insight' style='background:{bg};border-color:{fg}33;color:#1e293b'><span class='dot' style='background:{fg}'></span><div>{html.escape(text)}</div></div>"


def section(label: str) -> None:
    st.markdown(f"<div class='sectionlabel'>{html.escape(label)}</div>", unsafe_allow_html=True)


def style_fig(fig: go.Figure, height: int = 320, title: str | None = None) -> go.Figure:
    fig.update_layout(height=height, **({"title_text": title} if title else {}))
    return fig


def show_fig(fig: go.Figure, height: int = 320, title: str | None = None) -> None:
    st.plotly_chart(style_fig(fig, height, title), width="stretch", config={"displayModeBar": False})


def fmt_num(v, suffix: str = "", none: str = "n/a") -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return none
    return f"{int(v):,}{suffix}" if isinstance(v, numbers.Integral) else f"{v:,.1f}{suffix}"


# --- Data access ------------------------------------------------------------
@st.cache_resource(show_spinner="Initialising synthetic demo database...")
def init_demo() -> bool:
    return ensure_demo_data()


@contextmanager
def db(readonly: bool = False):
    with connect(readonly=readonly) as conn:
        yield conn


def data_version() -> str:
    """Cheap fingerprint of mutable data; used as a cache key so edits invalidate cached audits."""
    with db(readonly=True) as c:
        return str(tuple(c.execute("SELECT COUNT(*), COALESCE(MAX(updated_at), '') FROM products").fetchone())) + str(
            c.execute("SELECT COALESCE(MAX(value), '') FROM app_meta").fetchone()[0]
        )


@st.cache_data(ttl=600, show_spinner="Running catalog quality audit...")
def cached_audit(version: str, z: float, dup: float):
    with db(readonly=True) as c:
        return run_audit(c, z, dup)


XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
EXCEL_UI_MAX_ROWS = 50_000
TABLE_DISPLAY_ROWS = 2_000


def download_table(df: pd.DataFrame, stem: str, label: str = "Export", key: str | None = None, primary: bool = False, sheet: str = "Data") -> None:
    """CSV and Excel download buttons. Data is generated on click (deferred), not on every rerun."""
    kind = "primary" if primary else "secondary"
    c1, c2, _ = st.columns([1, 1, 3])
    c1.download_button(f"{label} (CSV)", lambda: to_csv_bytes(df), f"{stem}.csv", "text/csv", key=f"{key or stem}_csv", type=kind, on_click="ignore")
    too_big = len(df) > EXCEL_UI_MAX_ROWS
    c2.download_button(
        f"{label} (Excel)", lambda: to_excel_bytes(df, sheet), f"{stem}.xlsx", XLSX_MIME, key=f"{key or stem}_xlsx", type=kind, on_click="ignore",
        disabled=too_big, help=f"Excel export is limited to {EXCEL_UI_MAX_ROWS:,} rows (about 25 s per 50,000 rows); use CSV for larger exports." if too_big else None,
    )


def report_builders(audit, t):
    """Deferred executive-report generators (run on click, on a worker thread with their own DB connection)."""

    def html_report() -> str:
        with db(readonly=True) as c:
            return executive_report_html(c, audit, t)

    def xlsx_report() -> bytes:
        with db(readonly=True) as c:
            return executive_report_xlsx(c, audit, t)

    return html_report, xlsx_report


def flash(kind: str, message: str) -> None:
    st.session_state["_flash"] = (kind, message)


def show_flash() -> None:
    item = st.session_state.pop("_flash", None)
    if item:
        {"success": st.success, "error": st.error, "warning": st.warning}.get(item[0], st.info)(item[1])


def now():
    return utcnow()
