"""Live verification of the optional LLM classification path.

    OPENAI_API_KEY=sk-... python scripts/verify_llm.py            # real calls (a few cents at most)
    python scripts/verify_llm.py                                  # no key: records a SKIPPED result and checks the fallback

With a key it sends a connectivity ping plus the 16 held-out requests (catalog_intel/evalset.py), compares the model with
the expected labels and with the rule-based classifier, and records latency, token usage and ESTIMATED cost per call.
Without a key nothing is sent anywhere; the script verifies that the deterministic fallback is used instead and writes
a result file whose status is `skipped_no_api_key`, so documentation can never claim a live verification that did not run.

Never stored: API keys, prompts, model responses. Output: docs/performance/llm_verification_<UTC timestamp>.json
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from catalog_intel import ai  # noqa: E402
from catalog_intel.classifier import classify_request, classify_rules  # noqa: E402
from catalog_intel.config import ROOT  # noqa: E402
from catalog_intel.db import connect, init_schema  # noqa: E402
from catalog_intel.evalset import HELD_OUT  # noqa: E402


def summarize_calls(calls: list[ai.LLMCall]) -> dict:
    ok = [c for c in calls if c.status == "ok"]
    costs = [c.cost_usd for c in calls if c.cost_usd is not None]
    lat = sorted(c.latency_ms for c in calls if c.latency_ms is not None)
    return {
        "calls": len(calls), "ok": len(ok), "errors": sum(c.status == "error" for c in calls),
        "invalid_output": sum(c.status == "invalid_output" for c in calls), "skipped_daily_cap": sum(c.status == "skipped_daily_cap" for c in calls),
        "prompt_tokens": sum(c.prompt_tokens or 0 for c in calls), "completion_tokens": sum(c.completion_tokens or 0 for c in calls),
        "estimated_cost_usd": round(sum(costs), 6) if costs else None, "cost_known_for_calls": len(costs),
        "latency_ms_avg": round(statistics.mean(lat)) if lat else None, "latency_ms_p95": lat[min(len(lat) - 1, int(len(lat) * 0.95))] if lat else None,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out-dir", default=str(ROOT / "docs" / "performance"))
    ap.add_argument("--require-key", action="store_true", help="exit with status 2 instead of recording a skip when no key is set")
    args = ap.parse_args()
    started = datetime.now(timezone.utc)
    result: dict = {"started_utc": started.strftime("%Y-%m-%d %H:%M:%S"), "model": ai.model_name(), "examples": len(HELD_OUT)}
    rules_acc = sum(classify_rules(s, b).request_type == t for s, b, t in HELD_OUT) / len(HELD_OUT)

    if not ai.is_configured():
        print("SKIPPED: no OPENAI_API_KEY configured, so no real LLM call was made.")
        fallback = [classify_request(s, b, use_llm=True) for s, b, _ in HELD_OUT]
        result.update(
            status="skipped_no_api_key", live_llm_calls_made=0,
            fallback_check={"all_classified_by_rules": all(c.source == "rules" for c in fallback), "rules_accuracy": round(rules_acc, 3)},
            note="No real LLM verification has been performed for this record.",
        )
        print(f"Fallback check: all {len(fallback)} requests handled by rules (accuracy {rules_acc:.0%}).")
        code = 2 if args.require_key else 0
    else:
        ai.reset_daily_counter()
        ping = ai.ping()
        rows, llm_correct, agree = [], 0, 0
        for subject, body, expected in HELD_OUT:
            c = ai.classify_with_llm(subject, body)
            rules = classify_rules(subject, body).request_type
            llm_type = c.request_type if c else None
            llm_correct += llm_type == expected
            agree += llm_type == rules
            rows.append({"expected": expected, "rules": rules, "llm": llm_type, "llm_confidence": c.confidence if c else None})
            print(f"  expected={expected:<20} rules={rules:<20} llm={llm_type}")
        calls = ai.pending_calls()
        with tempfile.TemporaryDirectory() as tmp, connect(Path(tmp) / "log.db") as conn:  # proves the cost log persists
            init_schema(conn)
            persisted = ai.flush_call_log(conn)
            stored = conn.execute("SELECT COUNT(*), COALESCE(SUM(cost_usd), 0) FROM llm_call_log").fetchone()
        result.update(
            status="completed", live_llm_calls_made=len(calls), ping={"status": ping.status, "latency_ms": ping.latency_ms} if ping else None,
            llm_accuracy=round(llm_correct / len(HELD_OUT), 3), rules_accuracy=round(rules_acc, 3), llm_rules_agreement=round(agree / len(HELD_OUT), 3),
            usage=summarize_calls(calls), persisted_log_rows=persisted, stored_rows=stored[0], stored_cost_usd=round(stored[1], 6), per_example=rows,
            note="Cost is an estimate from public per-token prices (override with OPENAI_PRICE_INPUT_PER_1M / OPENAI_PRICE_OUTPUT_PER_1M).",
        )
        print(json.dumps({k: result[k] for k in ("llm_accuracy", "rules_accuracy", "usage")}, indent=2))
        code = 0 if result["usage"]["ok"] > 0 else 1

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"llm_verification_{started.strftime('%Y%m%dT%H%M%SZ')}_{result['status']}.json"
    path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"Wrote {path}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
