"""REAL LLM calls. Skipped automatically unless OPENAI_API_KEY is set in the environment.

    OPENAI_API_KEY=sk-... python -m pytest tests/test_llm_live.py -v -s

Costs a few cents at most (about 10 tiny calls). Without a key this file is reported as skipped, never as passed.
"""
import os

import pytest

from catalog_intel import ai
from catalog_intel.classifier import classify_request
from catalog_intel.config import REQUEST_TYPES
from catalog_intel.evalset import HELD_OUT

pytestmark = [pytest.mark.live_llm, pytest.mark.skipif(not os.environ.get("OPENAI_API_KEY"), reason="OPENAI_API_KEY not set: live LLM test not executed")]


def test_live_ping_records_latency_tokens_and_cost():
    call = ai.ping()
    assert call is not None and call.status == "ok", f"ping failed: {call}"
    assert call.latency_ms > 0 and call.prompt_tokens > 0 and call.completion_tokens >= 0
    print(f"\nping: model={call.model} latency={call.latency_ms}ms tokens={call.prompt_tokens}+{call.completion_tokens} est_cost=${call.cost_usd}")


def test_live_classification_returns_valid_types_and_logs_every_call(tmp_path):
    from catalog_intel.db import connect, init_schema

    sample = HELD_OUT[::2]  # 8 examples
    results = [classify_request(s, b, use_llm=True) for s, b, _ in sample]
    llm_results = [r for r in results if r.source == "llm"]
    assert llm_results, "no request was classified by the LLM (check key/model/base URL); see the call log"
    assert all(r.request_type in REQUEST_TYPES and 0 <= r.confidence <= 1 for r in results)
    calls = ai.pending_calls()
    assert len(calls) >= len(sample) and all(c.latency_ms is not None for c in calls)
    with connect(tmp_path / "log.db") as conn:
        init_schema(conn)
        assert ai.flush_call_log(conn) == len(calls)
        total, cost = conn.execute("SELECT COUNT(*), COALESCE(SUM(cost_usd), 0) FROM llm_call_log").fetchone()
    print(f"\n{len(llm_results)}/{len(sample)} classified by LLM; {total} calls logged; estimated cost ${cost:.6f}")
    correct = sum(r.request_type == t for r, (_, _, t) in zip(results, sample))
    print(f"accuracy on {len(sample)} held-out examples (LLM where available, rules otherwise): {correct}/{len(sample)}")
