"""AI Operations Assistant.

Questions are routed by a deterministic intent matcher to the same SQL analytics used elsewhere, so every
answer is grounded in the database. When an LLM key is configured, unmatched questions are answered by the
model using only aggregate metrics as context; otherwise a help message is returned.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd

from . import ai
from .analytics import generate_insights, kpi_summary, run_query
from .classifier import classify_request, recommend_priority
from .config import OPEN_STATUSES, utcnow
from .quality import AuditResult
from .requests_mgmt import load_requests

EXAMPLES = [
    "Give me an executive summary",
    "Which requests are breaching SLA?",
    "What is our rework rate?",
    "Which request types take the longest?",
    "How many duplicate listings do we have?",
    "Show price anomalies",
    "Which merchants have the lowest catalog quality?",
    "What is the open backlog by priority?",
    "Classify: Our CSV upload keeps failing and 300 rows were rejected, please help asap",
]


@dataclass
class Reply:
    text: str
    table: pd.DataFrame | None = None
    intent: str = "unknown"
    sources: list[str] = field(default_factory=list)
    mode: str = "deterministic"


INTENTS: list[tuple[str, list[str]]] = [
    ("summary", ["summary", "overview", "health", "status report", "how are we doing", "executive"]),
    ("sla", ["sla", "breach", "overdue", "late", "past due", "at risk"]),
    ("rework", ["rework", "reopen", "sent back"]),
    ("processing", ["processing time", "take the longest", "slowest", "turnaround", "how long", "cycle time", "resolution time"]),
    ("duplicates", ["duplicate", "duplicates"]),
    ("price", ["price anomal", "pricing anomal", "outlier", "price issue", "wrong price"]),
    ("images", ["image", "photo", "picture"]),
    ("quality", ["quality", "catalog health", "lowest catalog"]),
    ("backlog", ["backlog", "open request", "open requests", "workload", "queue"]),
    ("merchants", ["merchant", "customer", "partner"]),
]


def detect_intent(question: str) -> str:
    q = question.lower().strip()
    if re.match(r"^(classify|triage)\s*:", q):
        return "classify"
    best, best_hits = "unknown", 0
    for name, words in INTENTS:
        hits = sum(1 for w in words if w in q)
        if hits > best_hits:
            best, best_hits = name, hits
    return best


def answer(question: str, conn, audit: AuditResult, now: datetime | None = None) -> Reply:
    question = (question or "").strip()
    if not question:
        return Reply("Please type a question.", intent="empty")
    if len(question) > 500:
        return Reply("Please keep questions under 500 characters.", intent="invalid")
    now = now or utcnow()
    intent = detect_intent(question)
    k = kpi_summary(conn, now)

    if intent == "classify":
        body = question.split(":", 1)[1].strip()
        if len(body) < 5:
            return Reply("Add the request text after 'Classify:'.", intent=intent)
        c = classify_request(body, "", use_llm=True)
        p = recommend_priority(body, "", c.request_type, "Standard", 0)
        tbl = pd.DataFrame({"factor": p.reasons})
        return Reply(
            f"**Type:** {c.request_type} (confidence {c.confidence:.0%}, via {c.source})\n\n**Recommended priority:** {p.priority} "
            f"(score {p.score}, assuming a Standard-tier merchant and no item count).",
            tbl, intent, ["classifier"], "llm" if c.source == "llm" else "deterministic",
        )
    if intent == "summary":
        sla, rework = run_query(conn, "sla_by_priority", now), run_query(conn, "rework_by_type", now)
        ins = generate_insights(k, rework, sla)
        text = (
            f"**Operations snapshot** - {k['open_requests']} open requests ({k['p1_open']} P1), SLA compliance **{k['sla_compliance_pct']}%**, "
            f"rework rate **{k['rework_rate_pct']}%**, average processing time **{k['avg_processing_hours']}h**. "
            f"Catalog clean rate is **{audit.summary['clean_rate']}%** across {audit.summary['products_audited']:,} products.\n\n"
            + "\n".join(f"- {t}" for _, t in ins)
        )
        return Reply(text, None, intent, ["kpi_summary", "sla_by_priority", "rework_by_type"])
    if intent == "sla":
        df = load_requests(conn, now)
        hot = df[df["sla_state"].isin(["Breached", "At Risk"])].sort_values("hours_to_due")
        tbl = hot[["request_id", "merchant_name", "request_type", "priority", "status", "assignee", "sla_state", "hours_to_due"]].head(15)
        return Reply(
            f"**{k['breached_open']}** open request(s) have breached SLA and **{k['at_risk_open']}** are at risk. Historical SLA compliance is "
            f"**{k['sla_compliance_pct']}%** over {k['completed_requests']} completed requests.",
            tbl, intent, ["backlog_aging", "sla_by_priority"],
        )
    if intent == "rework":
        df = run_query(conn, "rework_by_type", now)
        top = df.iloc[0]
        return Reply(
            f"Overall rework rate is **{k['rework_rate_pct']}%**. Highest: **{top['request_type']}** at {top['rework_rate_pct']}% ({int(top['reworked'])} of {int(top['completed'])}).",
            df, intent, ["rework_by_type"],
        )
    if intent == "processing":
        df = run_query(conn, "processing_time_by_type", now)
        top = df.iloc[0]
        return Reply(
            f"Average processing time is **{k['avg_processing_hours']}h**. Slowest type: **{top['request_type']}** averaging {top['avg_hours']}h (max {top['max_hours']}h).",
            df, intent, ["processing_time_by_type"],
        )
    if intent in ("duplicates", "price", "images"):
        label = {"duplicates": "Duplicate Listing", "price": "Price Anomaly", "images": "Missing Image"}[intent]
        iss = audit.issues[audit.issues["issue_type"] == label]
        extra = ""
        if intent == "price":
            inv = audit.issues[audit.issues["issue_type"] == "Invalid Price"]
            iss, extra = pd.concat([inv, iss]), f" (including {len(inv)} invalid price(s))"
        by_m = iss.groupby("merchant_id").size().sort_values(ascending=False)
        lead = f" Most affected merchant: **{by_m.index[0]}** ({by_m.iloc[0]})." if len(by_m) else ""
        return Reply(
            f"Found **{len(iss)}** {label.lower()} issue(s){extra} across {audit.summary['products_audited']:,} audited products.{lead}",
            iss[["merchant_id", "sku", "title", "severity", "detail"]].head(15), intent, ["catalog_audit"],
        )
    if intent == "quality":
        ms = audit.merchant_scores[["merchant_name", "products", "avg_quality_score", "clean_rate"]].head(8)
        low = ms.iloc[0]
        return Reply(f"Lowest catalog quality: **{low['merchant_name']}** (avg score {low['avg_quality_score']}, clean rate {low['clean_rate']}%).", ms, intent, ["catalog_audit"])
    if intent == "backlog":
        df = load_requests(conn, now)
        op = df[df["status"].isin(OPEN_STATUSES)]
        tbl = op.groupby(["priority", "sla_state"]).size().unstack(fill_value=0).reset_index()
        return Reply(f"There are **{len(op)}** open requests; **{k['p1_open']}** are P1.", tbl, intent, ["backlog_aging"])
    if intent == "merchants":
        df = run_query(conn, "merchant_leaderboard", now).head(10)
        return Reply("Merchant request load and outcomes (top 10 by volume):", df, intent, ["merchant_leaderboard"])

    if ai.is_configured():
        ctx = "\n".join(f"{a}: {b}" for a, b in k.items()) + "\n" + "\n".join(f"{a}: {b}" for a, b in audit.summary.items())
        text = ai.answer_with_llm(question, ctx)
        if text:
            return Reply(text, None, "llm", ["kpi_summary"], "llm")
    return Reply(
        "I can answer questions about SLA, backlog, rework, processing time, duplicates, price anomalies, images, merchant quality, or classify a "
        "request text (prefix with `Classify:`). Try one of the example prompts.",
        None, "unknown",
    )
