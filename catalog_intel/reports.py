"""Report exports (CSV with spreadsheet-formula protection, HTML executive report)."""
from __future__ import annotations

import html
from datetime import datetime

import pandas as pd

from .analytics import generate_insights, kpi_summary, run_query
from .config import DEMO_DATA_NOTICE, fmt, utcnow
from .quality import AuditResult

_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def sanitize_cell(value):
    """Neutralise CSV/spreadsheet formula injection (OWASP): prefix risky text cells with a quote."""
    if isinstance(value, str) and value.startswith(_FORMULA_PREFIXES):
        return "'" + value
    return value


def to_csv_bytes(df: pd.DataFrame) -> bytes:
    safe = df.copy()
    for col in safe.select_dtypes(include=["object", "string"]).columns:
        safe[col] = safe[col].map(sanitize_cell)
    return safe.to_csv(index=False).encode("utf-8-sig")


def _table(df: pd.DataFrame, max_rows: int = 15) -> str:
    if df.empty:
        return "<p><em>No data.</em></p>"
    head = "".join(f"<th>{html.escape(str(c))}</th>" for c in df.columns)
    body = "".join(
        "<tr>" + "".join(f"<td>{html.escape(str(v))}</td>" for v in row) + "</tr>" for row in df.head(max_rows).itertuples(index=False)
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def executive_report_html(conn, audit: AuditResult, now: datetime | None = None) -> str:
    now = now or utcnow()
    k = kpi_summary(conn, now)
    sla = run_query(conn, "sla_by_priority", now)
    rework = run_query(conn, "rework_by_type", now)
    proc = run_query(conn, "processing_time_by_type", now)
    insights = generate_insights(k, rework, sla)
    s = audit.summary
    cards = [
        ("Open requests", k["open_requests"]), ("SLA compliance", f"{k['sla_compliance_pct']}%"),
        ("Rework rate", f"{k['rework_rate_pct']}%"), ("Avg processing (h)", k["avg_processing_hours"]),
        ("Breached open", k["breached_open"]), ("Catalog clean rate", f"{s['clean_rate']}%"),
    ]
    card_html = "".join(f"<div class='card'><div class='v'>{html.escape(str(v))}</div><div class='l'>{html.escape(l)}</div></div>" for l, v in cards)
    ins_html = "".join(f"<li><b>{html.escape(sev.title())}:</b> {html.escape(text)}</li>" for sev, text in insights)
    issues = pd.DataFrame(list(s["by_type"].items()), columns=["issue_type", "count"])
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Catalog Operations Executive Report</title>
<style>body{{font-family:Segoe UI,Arial,sans-serif;margin:32px;color:#0f172a}}h1{{margin-bottom:0}}.sub{{color:#64748b;margin-bottom:24px}}
.cards{{display:flex;gap:12px;flex-wrap:wrap}}.card{{border:1px solid #e2e8f0;border-radius:10px;padding:14px 18px;min-width:140px}}
.v{{font-size:24px;font-weight:700;color:#4338ca}}.l{{color:#64748b;font-size:12px}}table{{border-collapse:collapse;margin:8px 0 24px}}
td,th{{border:1px solid #e2e8f0;padding:6px 10px;font-size:13px;text-align:left}}th{{background:#f1f5f9}}.note{{background:#fffbeb;border:1px solid #fde68a;padding:10px;border-radius:8px;font-size:13px}}</style></head>
<body><h1>Merchant Catalog Intelligence - Executive Report</h1><div class="sub">Generated {fmt(now)} UTC</div>
<div class="note">{html.escape(DEMO_DATA_NOTICE)}</div><h2>Key metrics</h2><div class="cards">{card_html}</div>
<h2>Operational insights</h2><ul>{ins_html}</ul><h2>SLA compliance by priority</h2>{_table(sla)}
<h2>Processing time by request type</h2>{_table(proc)}<h2>Rework rate by request type</h2>{_table(rework)}
<h2>Catalog quality findings</h2>{_table(issues)}</body></html>"""
