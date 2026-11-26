"""Optional LLM layer (OpenAI-compatible chat completions via the standard library).

Disabled unless an API key is provided through the OPENAI_API_KEY environment variable or
Streamlit secrets. No key => every function returns None and callers use deterministic logic.
No third-party SDK is required.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from .classifier import Classification
from .config import REQUEST_TYPES

DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_BASE_URL = "https://api.openai.com/v1"
TIMEOUT_SECONDS = 12


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


def _chat(messages: list[dict], max_tokens: int = 300) -> str | None:
    key = _secret("OPENAI_API_KEY")
    if not key:
        return None
    base = (_secret("OPENAI_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    body = json.dumps(
        {"model": _secret("OPENAI_MODEL") or DEFAULT_MODEL, "messages": messages, "temperature": 0, "max_tokens": max_tokens}
    ).encode()
    req = urllib.request.Request(
        f"{base}/chat/completions", data=body, method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:  # noqa: S310 - fixed https endpoint
            payload = json.loads(resp.read().decode())
        return payload["choices"][0]["message"]["content"]
    except (urllib.error.URLError, TimeoutError, KeyError, IndexError, ValueError, OSError):
        return None


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
        ]
    )
    return parse_classification_json(raw)


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
    )
