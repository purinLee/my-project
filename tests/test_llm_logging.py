import json
import subprocess
import sys
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from catalog_intel import ai
from catalog_intel.classifier import classify_request
from catalog_intel.requests_mgmt import create_request
from tests.conftest import AS_OF

ROOT = Path(__file__).resolve().parent.parent
SECRET = "sk-test-SECRET-do-not-log"
GOOD = {"choices": [{"message": {"content": '{"request_type": "Price Update", "confidence": 0.9, "reason": "price"}'}}], "usage": {"prompt_tokens": 100, "completion_tokens": 20}}


class FakeResp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture()
def llm(monkeypatch):
    """Configured LLM with a patched network layer. Returns a dict to customise secrets / the response."""
    state = {"secrets": {"OPENAI_API_KEY": SECRET}, "response": GOOD, "calls": 0}

    def fake_urlopen(req, timeout=0):
        state["calls"] += 1
        r = state["response"]
        if isinstance(r, Exception):
            raise r
        return FakeResp(r)

    monkeypatch.setattr(ai, "_secret", lambda name: state["secrets"].get(name))
    monkeypatch.setattr(ai.urllib.request, "urlopen", fake_urlopen)
    return state


def test_successful_call_is_recorded_with_tokens_latency_and_estimated_cost(llm):
    c = ai.classify_with_llm("Update prices", "price change")
    assert c is not None and c.source == "llm" and c.request_type == "Price Update"
    (call,) = ai.pending_calls()
    assert (call.status, call.purpose, call.model) == ("ok", "classify", "gpt-4.1-mini")
    assert (call.prompt_tokens, call.completion_tokens) == (100, 20)
    assert call.cost_usd == pytest.approx((100 * 0.40 + 20 * 1.60) / 1e6)
    assert call.latency_ms is not None and call.latency_ms >= 0 and call.error is None


def test_price_override_unknown_model_and_missing_usage(llm):
    llm["secrets"].update(OPENAI_PRICE_INPUT_PER_1M="1", OPENAI_PRICE_OUTPUT_PER_1M="2")
    ai.classify_with_llm("a", "b")
    assert ai.pending_calls()[-1].cost_usd == pytest.approx((100 * 1 + 20 * 2) / 1e6)
    llm["secrets"] = {"OPENAI_API_KEY": SECRET, "OPENAI_MODEL": "some-new-model"}
    ai.classify_with_llm("a", "b")
    assert ai.pending_calls()[-1].cost_usd is None  # unknown pricing is never guessed
    llm["response"] = {"choices": GOOD["choices"]}
    ai.classify_with_llm("a", "b")
    last = ai.pending_calls()[-1]
    assert last.status == "ok" and last.prompt_tokens is None and last.cost_usd is None


def test_http_error_is_recorded_without_body_and_falls_back_to_rules(llm):
    llm["response"] = urllib.error.HTTPError("http://x", 401, "Unauthorized", {}, None)
    result = classify_request("Update prices", "price change for 10 items")
    assert result.source == "rules" and result.request_type == "Price Update"
    (call,) = ai.pending_calls()
    assert call.status == "error" and call.error == "HTTP 401" and call.cost_usd is None


@pytest.mark.parametrize("exc,label", [(urllib.error.URLError("boom"), "URLError"), (TimeoutError(), "TimeoutError"), (ConnectionResetError(), "ConnectionResetError")])
def test_network_errors_are_recorded(llm, exc, label):
    llm["response"] = exc
    assert ai.classify_with_llm("a", "b") is None
    assert ai.pending_calls()[-1].error == label


def test_malformed_payload_is_an_error(llm):
    llm["response"] = {"nope": 1}
    assert ai.classify_with_llm("a", "b") is None
    call = ai.pending_calls()[-1]
    assert call.status == "error" and call.error.startswith("bad response")


def test_invalid_model_output_is_flagged_and_falls_back(llm):
    llm["response"] = {"choices": [{"message": {"content": "I think it is about prices"}}], "usage": {"prompt_tokens": 5, "completion_tokens": 5}}
    result = classify_request("Update prices", "price change for 10 items")
    assert result.source == "rules"
    call = ai.pending_calls()[-1]
    assert call.status == "invalid_output" and call.prompt_tokens == 5  # still billed, so tokens/cost are kept


def test_daily_cap_blocks_further_calls_and_is_recorded(llm):
    llm["secrets"]["OPENAI_MAX_CALLS_PER_DAY"] = "2"
    for _ in range(3):
        ai.classify_with_llm("a", "b")
    statuses = [c.status for c in ai.pending_calls()]
    assert statuses == ["ok", "ok", "skipped_daily_cap"] and llm["calls"] == 2
    assert classify_request("Update prices", "price change for 10 items").source == "rules"  # cap => fallback, not failure
    ai.reset_daily_counter()
    ai.classify_with_llm("a", "b")
    assert llm["calls"] == 3


def test_ping_and_unconfigured_ping(llm, monkeypatch):
    llm["response"] = {"choices": [{"message": {"content": "OK"}}], "usage": {"prompt_tokens": 9, "completion_tokens": 1}}
    call = ai.ping()
    assert call.status == "ok" and call.purpose == "ping"
    monkeypatch.setattr(ai, "_secret", lambda name: None)
    assert ai.ping() is None


def test_flush_persists_rows_and_clears_pending(llm, wconn):
    ai.classify_with_llm("a", "b")
    llm["response"] = urllib.error.HTTPError("http://x", 500, "err", {}, None)
    ai.classify_with_llm("a", "b")
    assert ai.flush_call_log(wconn) == 2 and ai.pending_calls() == [] and ai.flush_call_log(wconn) == 0
    rows = wconn.execute("SELECT status, prompt_tokens, cost_usd, error FROM llm_call_log ORDER BY call_id").fetchall()
    assert [r["status"] for r in rows] == ["ok", "error"] and rows[0]["cost_usd"] == pytest.approx(7.2e-5) and rows[1]["error"] == "HTTP 500"


def test_secrets_and_prompts_are_never_logged(llm, wconn):
    ai.classify_with_llm("Very confidential subject line", "Very confidential body text")
    dump = json.dumps([c.__dict__ for c in ai.pending_calls()])
    ai.flush_call_log(wconn)
    dump += json.dumps([dict(r) for r in wconn.execute("SELECT * FROM llm_call_log")])
    assert SECRET not in dump and "confidential" not in dump


def test_create_request_persists_llm_call_log_end_to_end(llm, wconn):
    res = create_request(wconn, "M-001", "Please adjust the price list", "We need new prices on 20 items by Friday.", 20, now=AS_OF)
    assert res["source"] == "llm"
    row = wconn.execute("SELECT * FROM llm_call_log").fetchone()
    assert row["purpose"] == "classify" and row["status"] == "ok" and row["cost_usd"] > 0
    assert ai.pending_calls() == []


def test_without_key_nothing_is_recorded_and_rules_are_used():
    assert not ai.is_configured()
    assert classify_request("Update prices", "price change for 10 items").source == "rules"
    assert ai.pending_calls() == []


# ---- real HTTP round trip against a local OpenAI-compatible server (no mocks in the client path) ----
class _Handler(BaseHTTPRequestHandler):
    seen_auth: list[str] = []

    def do_POST(self):
        self.seen_auth.append(self.headers.get("Authorization", ""))
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        body = json.dumps(GOOD if "classify" in self.path or True else {}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture()
def local_server():
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/v1"
    srv.shutdown()


def test_real_http_round_trip_against_local_server(monkeypatch, local_server):
    secrets = {"OPENAI_API_KEY": SECRET, "OPENAI_BASE_URL": local_server}
    monkeypatch.setattr(ai, "_secret", lambda name: secrets.get(name))
    c = ai.classify_with_llm("Update prices", "price change")
    assert c.source == "llm" and ai.pending_calls()[0].status == "ok"
    assert _Handler.seen_auth[-1] == f"Bearer {SECRET}"


def test_verify_llm_script_with_local_server_records_usage_and_cost(tmp_path, local_server):
    env = {"PATH": "", "SYSTEMROOT": "C:\\Windows", "OPENAI_API_KEY": SECRET, "OPENAI_BASE_URL": local_server}
    proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "verify_llm.py"), "--out-dir", str(tmp_path)], capture_output=True, text=True, timeout=120, env=env, cwd=ROOT)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    (out,) = tmp_path.glob("llm_verification_*_completed.json")
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["status"] == "completed" and data["live_llm_calls_made"] == 17  # ping + 16 held-out examples
    assert data["usage"]["ok"] == 17 and data["usage"]["estimated_cost_usd"] == pytest.approx(17 * 7.2e-5)
    assert data["stored_rows"] == 17 and 0 <= data["llm_accuracy"] <= 1
    assert SECRET not in out.read_text(encoding="utf-8")


def test_verify_llm_script_without_key_records_a_skip_not_a_pass(tmp_path):
    env = {"PATH": "", "SYSTEMROOT": "C:\\Windows"}
    proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "verify_llm.py"), "--out-dir", str(tmp_path)], capture_output=True, text=True, timeout=120, env=env, cwd=ROOT)
    assert proc.returncode == 0 and "SKIPPED" in proc.stdout
    (out,) = tmp_path.glob("llm_verification_*_skipped_no_api_key.json")
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["status"] == "skipped_no_api_key" and data["live_llm_calls_made"] == 0 and data["fallback_check"]["all_classified_by_rules"]
    strict = subprocess.run([sys.executable, str(ROOT / "scripts" / "verify_llm.py"), "--out-dir", str(tmp_path), "--require-key"], capture_output=True, text=True, timeout=120, env=env, cwd=ROOT)
    assert strict.returncode == 2
