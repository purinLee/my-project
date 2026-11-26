"""Merchant request management: creation, lifecycle transitions, SLA status."""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from .classifier import classify_request, recommend_priority
from .config import (
    AT_RISK_FRACTION, OPEN_STATUSES, PRIORITIES, REQUEST_TYPES, REWORK_SOURCES, SLA_HOURS, STATUSES, TRANSITIONS,
    CHANNELS, fmt, parse_dt, utcnow,
)
from .db import query_df


class ValidationError(ValueError):
    """User input failed validation. `.errors` holds every problem found."""

    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def validate_new_request(conn, merchant_id, subject, description, affected_items, channel="Portal") -> list[str]:
    errors = []
    if not conn.execute("SELECT 1 FROM merchants WHERE merchant_id=?", (merchant_id,)).fetchone():
        errors.append("Select a valid merchant.")
    subject, description = (subject or "").strip(), (description or "").strip()
    if not 5 <= len(subject) <= 120:
        errors.append("Subject must be 5-120 characters.")
    if not 10 <= len(description) <= 2000:
        errors.append("Description must be 10-2000 characters.")
    if not isinstance(affected_items, int) or isinstance(affected_items, bool) or not 0 <= affected_items <= 1_000_000:
        errors.append("Affected items must be a whole number between 0 and 1,000,000.")
    if channel not in CHANNELS:
        errors.append("Unknown channel.")
    return errors


def create_request(
    conn, merchant_id: str, subject: str, description: str, affected_items: int = 0, channel: str = "Portal",
    request_type: str | None = None, priority: str | None = None, use_llm: bool = True, now: datetime | None = None,
) -> dict:
    """Create a request. Type and priority are auto-recommended unless explicitly supplied."""
    errors = validate_new_request(conn, merchant_id, subject, description, affected_items, channel)
    if request_type is not None and request_type not in REQUEST_TYPES:
        errors.append("Unknown request type.")
    if priority is not None and priority not in PRIORITIES:
        errors.append("Unknown priority.")
    if errors:
        raise ValidationError(errors)
    subject, description = subject.strip(), description.strip()
    now = now or utcnow()

    cls = classify_request(subject, description, use_llm=use_llm)
    rtype = request_type or cls.request_type
    source = "manual" if request_type else cls.source
    tier = conn.execute("SELECT tier FROM merchants WHERE merchant_id=?", (merchant_id,)).fetchone()[0]
    rec = recommend_priority(subject, description, rtype, tier, affected_items)
    prio = priority or rec.priority

    next_no = (conn.execute("SELECT COALESCE(MAX(CAST(SUBSTR(request_id, 5) AS INTEGER)), 0) FROM merchant_requests").fetchone()[0]) + 1
    rid = f"REQ-{next_no:05d}"
    conn.execute(
        """INSERT INTO merchant_requests (request_id, merchant_id, subject, description, request_type, priority, status, channel,
           affected_items, created_at, due_at, classification_source, classification_confidence)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (rid, merchant_id, subject, description, rtype, prio, "New", channel, affected_items, fmt(now),
         fmt(now + timedelta(hours=SLA_HOURS[prio])), source, None if request_type else cls.confidence),
    )
    conn.execute(
        "INSERT INTO request_events (request_id, event_type, from_status, to_status, actor, note, created_at) VALUES (?,?,?,?,?,?,?)",
        (rid, "created", None, "New", "merchant", "Request submitted", fmt(now)),
    )
    return {"request_id": rid, "request_type": rtype, "priority": prio, "confidence": cls.confidence, "source": source, "reasons": rec.reasons}


def allowed_transitions(status: str) -> list[str]:
    return list(TRANSITIONS.get(status, []))


def transition_request(conn, request_id: str, to_status: str, actor: str = "ops", note: str = "", now: datetime | None = None) -> dict:
    """Move a request through the lifecycle. Enforces TRANSITIONS and maintains timestamps/rework."""
    now = now or utcnow()
    row = conn.execute("SELECT status, assignee FROM merchant_requests WHERE request_id=?", (request_id,)).fetchone()
    if row is None:
        raise ValidationError([f"Request '{request_id}' not found."])
    current = row["status"]
    if to_status not in STATUSES:
        raise ValidationError([f"Unknown status '{to_status}'."])
    if to_status not in TRANSITIONS.get(current, []):
        allowed = ", ".join(TRANSITIONS.get(current, [])) or "none (terminal)"
        raise ValidationError([f"Cannot move from '{current}' to '{to_status}'. Allowed: {allowed}."])
    note = (note or "").strip()[:500]
    is_rework = to_status == "In Progress" and current in REWORK_SOURCES
    sets, params = ["status = ?"], [to_status]
    if is_rework:
        sets.append("rework_count = rework_count + 1")
        sets.append("resolved_at = NULL")
    if to_status == "Triaged":
        sets.append("first_response_at = COALESCE(first_response_at, ?)")
        params.append(fmt(now))
    if to_status == "Resolved":
        sets.append("resolved_at = ?")
        params.append(fmt(now))
    conn.execute(f"UPDATE merchant_requests SET {', '.join(sets)} WHERE request_id = ?", (*params, request_id))
    conn.execute(
        "INSERT INTO request_events (request_id, event_type, from_status, to_status, actor, note, created_at) VALUES (?,?,?,?,?,?,?)",
        (request_id, "rework" if is_rework else "status_change", current, to_status, (actor or "ops")[:80], note, fmt(now)),
    )
    return {"request_id": request_id, "from": current, "to": to_status, "rework": is_rework}


def assign_request(conn, request_id: str, assignee: str) -> None:
    assignee = (assignee or "").strip()
    if not 2 <= len(assignee) <= 80:
        raise ValidationError(["Assignee must be 2-80 characters."])
    if conn.execute("UPDATE merchant_requests SET assignee=? WHERE request_id=?", (assignee, request_id)).rowcount == 0:
        raise ValidationError([f"Request '{request_id}' not found."])


def load_requests(conn, now: datetime | None = None) -> pd.DataFrame:
    """All requests joined to merchants, enriched with SLA state computed against `now`."""
    df = query_df(
        conn,
        "SELECT r.*, m.name AS merchant_name, m.tier AS merchant_tier FROM merchant_requests r JOIN merchants m USING (merchant_id) ORDER BY r.created_at DESC",
    )
    return add_sla_columns(df, now or utcnow())


def add_sla_columns(df: pd.DataFrame, now: datetime) -> pd.DataFrame:
    if df.empty:
        return df.assign(sla_state=pd.Series(dtype=str), hours_to_due=pd.Series(dtype=float), age_hours=pd.Series(dtype=float), processing_hours=pd.Series(dtype=float))
    df = df.copy()
    created, due = pd.to_datetime(df["created_at"]), pd.to_datetime(df["due_at"])
    resolved = pd.to_datetime(df["resolved_at"])
    now_ts = pd.Timestamp(now)
    is_open = df["status"].isin(OPEN_STATUSES)
    window = (due - created).dt.total_seconds() / 3600
    df["age_hours"] = ((now_ts - created).dt.total_seconds() / 3600).round(1)
    df["hours_to_due"] = ((due - now_ts).dt.total_seconds() / 3600).round(1)
    df["processing_hours"] = ((resolved - created).dt.total_seconds() / 3600).round(2)
    state = pd.Series("N/A", index=df.index)
    state[is_open & (due < now_ts)] = "Breached"
    state[is_open & (due >= now_ts) & (df["hours_to_due"] < window * AT_RISK_FRACTION)] = "At Risk"
    state[is_open & (due >= now_ts) & (df["hours_to_due"] >= window * AT_RISK_FRACTION)] = "On Track"
    done = resolved.notna()
    state[done & (resolved <= due)] = "Met"
    state[done & (resolved > due)] = "Missed"
    df["sla_state"] = state
    return df


def get_events(conn, request_id: str) -> pd.DataFrame:
    return query_df(
        conn, "SELECT created_at, event_type, from_status, to_status, actor, note FROM request_events WHERE request_id=? ORDER BY created_at, event_id",
        (request_id,),
    )
