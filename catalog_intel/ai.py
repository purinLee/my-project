"""Optional LLM layer (OpenAI-compatible chat completions via the standard library).

Disabled unless an API key is provided through the OPENAI_API_KEY environment variable or Streamlit secrets.
No key => every function returns None and callers use deterministic logic. No third-party SDK is required.

Every attempted call (success, error, invalid output, or skipped by the daily cap) is recorded as an `LLMCall`
with latency, token usage and an ESTIMATED cost, then persisted to the `llm_call_log` table by `flush_call_log`.
Prompts, responses and API keys are never stored.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone

from .classifier import Classification
from .config import REQUEST_TYPES

DEFAULT_MODEL = "gpt-4.1-mini"
DEFAULT_BASE_URL = "https://api.openai.com/v1"
TIMEOUT_SECONDS = 12
DEFAULT_MAX_CALLS_PER_DAY = 200

# USD per 1M tokens (input, output), from OpenAI's public pricing page checked 2026-11-26. These are ESTIMATES:
# override with OPENAI_PRICE_INPUT_PER_1M / OPENAI_PRICE_OUTPUT_PER_1M, which also covers other providers/models.
PRICING_PER_1M: dict[str, tuple[float, float]] = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-4.1-mini": (0.40, 1.60),
}


@dataclass
class LLMCall:
    created_at: str
    purpose: str
    model: str
    status: str  # ok | error | invalid_output | skipped_daily_cap
    latency_ms: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cost_usd: float | None = None  # None when pricing for the model is unknown
    error: str | None = None


_LOCK = threading.Lock()
_PENDING: list[LLMCall] = []
_DAILY = {"date": "", "count": 0}


def _secret(name: str) -> str | None:
    value = os.environ.get(name)
    if value:
        return value
    try:  # Streamlit secrets are optional and may not exist
        import streamlit as st

        return st.secrets.get(name)  # type: ignore[no-any-return]
    except Exception:
        return None


def is_configured() -> bool:
    return bool(_secret("OPENAI_API_KEY"))


def model_name() -> str:
    return _secret("OPENAI_MODEL") or DEFAULT_MODEL


def estimate_cost(model: str, prompt_tokens: int | None, completion_tokens: int | None) -> float | None:
    """Estimated USD cost, or None if token usage or pricing is unknown."""
    if prompt_tokens is None or completion_tokens is None:
        return None
    try:
        override = (_secret("OPENAI_PRICE_INPUT_PER_1M"), _secret("OPENAI_PRICE_OUTPUT_PER_1M"))
        price = (float(override[0]), float(override[1])) if all(override) else PRICING_PER_1M.get(model)
    except (TypeError, ValueError):
        price = PRICING_PER_1M.get(model)
    if price is None:
        return None
    return round((prompt_tokens * price[0] + completion_tokens * price[1]) / 1_000_000, 6)


def max_calls_per_day() -> int:
    try:
        return max(0, int(_secret("OPENAI_MAX_CALLS_PER_DAY") or DEFAULT_MAX_CALLS_PER_DAY))
    except (TypeError, ValueError):
        return DEFAULT_MAX_CALLS_PER_DAY


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _record(call: LLMCall) -> LLMCall:
    with _LOCK:
        _PENDING.append(call)
    return call


def pending_calls() -> list[LLMCall]:
    with _LOCK:
        return list(_PENDING)


def _take_cap_slot() -> bool:
    """Reserve one call under the per-process daily cap (protects a public demo from runaway spend)."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with _LOCK:
        if _DAILY["date"] != today:
            _DAILY.update(date=today, count=0)
        if _DAILY["count"] >= max_calls_per_day():
            return False
        _DAILY["count"] += 1
        return True


def reset_daily_counter() -> None:
    with _LOCK:
        _DAILY.update(date="", count=0)


def flush_call_log(conn) -> int:
    """Persist pending call records using the caller's connection. Returns the number written."""
    with _LOCK:
        batch = list(_PENDING)
        _PENDING.clear()
    if batch:
        conn.executemany(
            "INSERT INTO llm_call_log (created_at, purpose, model, status, latency_ms, prompt_tokens, completion_tokens, cost_usd, error) VALUES (?,?,?,?,?,?,?,?,?)",
            [(c.created_at, c.purpose, c.model, c.status, c.latency_ms, c.prompt_tokens, c.completion_tokens, c.cost_usd, c.error) for c in batch],
        )
    return len(batch)


def _chat(messages: list[dict], max_tokens: int = 300, purpose: str = "chat") -> str | None:
    key = _secret("OPENAI_API_KEY")
    if not key:
        return None
    model = model_name()
    if not _take_cap_slot():
        _record(LLMCall(_now(), purpose, model, "skipped_daily_cap", error=f"cap {max_calls_per_day()} calls/day reached"))
        return None
    base = (_secret("OPENAI_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    body = json.dumps({"model": model, "messages": messages, "temperature": 0, "max_tokens": max_tokens}).encode()
    req = urllib.request.Request(
        f"{base}/chat/completions", data=body, method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    started = time.perf_counter()
    call = LLMCall(_now(), purpose, model, "error")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:  # noqa: S310 - fixed https endpoint
            payload = json.loads(resp.read().decode())
        call.latency_ms = round((time.perf_counter() - started) * 1000)
        usage = payload.get("usage") or {}
        call.prompt_tokens, call.completion_tokens = usage.get("prompt_tokens"), usage.get("completion_tokens")
        call.cost_usd = estimate_cost(model, call.prompt_tokens, call.completion_tokens)
        text = payload["choices"][0]["message"]["content"]
        call.status = "ok"
        return text
    except urllib.error.HTTPError as exc:  # status code only: the body is never logged
        call.error = f"HTTP {exc.code}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        call.error = type(exc).__name__
    except (KeyError, IndexError, ValueError, TypeError) as exc:
        call.error = f"bad response ({type(exc).__name__})"
    finally:
        if call.latency_ms is None:
            call.latency_ms = round((time.perf_counter() - started) * 1000)
        _record(call)
    return None


def _mark_last_invalid(reason: str) -> None:
    with _LOCK:
        if _PENDING and _PENDING[-1].status == "ok":
            _PENDING[-1].status, _PENDING[-1].error = "invalid_output", reason


def parse_classification_json(raw: str | None) -> Classification | None:
    """Strictly validate model output; anything unexpected is rejected (caller falls back)."""
    if not raw:
        return None
    try:
        start, end = raw.index("{"), raw.rindex("}") + 1
        data = json.loads(raw[start:end])
        rtype = data["request_type"]
        conf = float(data.get("confidence", 0.7))
    except (ValueError, KeyError, TypeError):
        return None
    if rtype not in REQUEST_TYPES or not 0.0 <= conf <= 1.0:
        return None
    return Classification(rtype, round(conf, 2), "llm", [], str(data.get("reason", ""))[:200])


def classify_with_llm(subject: str, description: str) -> Classification | None:
    if not is_configured():
        return None
    prompt = (
        "Classify a merchant support request for an e-commerce catalog operations team. "
        f"Allowed request_type values: {REQUEST_TYPES}. "
        'Reply with JSON only: {"request_type": str, "confidence": number 0-1, "reason": str}.'
    )
    raw = _chat(
        [
            {"role": "system", "content": prompt},
            {"role": "user", "content": f"Subject: {subject[:200]}\nBody: {description[:1500]}"},
        ],
        purpose="classify",
    )
    result = parse_classification_json(raw)
    if raw is not None and result is None:
        _mark_last_invalid("output failed validation")
    return result


def answer_with_llm(question: str, context: str) -> str | None:
    """Grounded free-text answer. The model only sees aggregate metrics, never raw rows."""
    if not is_configured():
        return None
    return _chat(
        [
            {
                "role": "system",
                "content": "You are an operations analyst. Answer ONLY from the provided metrics; "
                "if the metrics do not contain the answer, say so. Be concise.",
            },
            {"role": "user", "content": f"Metrics:\n{context[:4000]}\n\nQuestion: {question[:500]}"},
        ],
        max_tokens=350,
        purpose="assistant",
    )


def ping() -> LLMCall | None:
    """Minimal live connectivity check (one tiny call). Returns the recorded call, or None when unconfigured."""
    if not is_configured():
        return None
    text = _chat([{"role": "user", "content": "Reply with the single word: OK"}], max_tokens=5, purpose="ping")
    calls = pending_calls()
    last = calls[-1] if calls else None
    if last is not None and last.status == "ok" and not (text or "").strip():
        _mark_last_invalid("empty reply")
    return last
