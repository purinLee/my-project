import hashlib
import sqlite3
from datetime import timedelta

import pandas as pd

from catalog_intel.assistant import answer, detect_intent
from catalog_intel.config import DEMO_DATA_NOTICE
from catalog_intel.db import connect
from catalog_intel.quality import run_audit
from catalog_intel.reports import executive_report_html, sanitize_cell, to_csv_bytes
from catalog_intel.seed import ensure_demo_data, seed_database
from tests.conftest import AS_OF, SEED


def _digest(path):
    c = sqlite3.connect(path)
    h = hashlib.sha256()
    for t in ("merchants", "products", "merchant_requests", "request_events"):
        for row in c.execute(f"SELECT * FROM {t} ORDER BY 1"):
            h.update(repr(row).encode())
    c.close()
    return h.hexdigest()


def test_seed_is_reproducible(tmp_path):
    a, b, c = tmp_path / "a.db", tmp_path / "b.db", tmp_path / "c.db"
    seed_database(a, SEED, AS_OF)
    seed_database(b, SEED, AS_OF)
    seed_database(c, SEED + 1, AS_OF)
    assert _digest(a) == _digest(b) != _digest(c)


def test_seed_volumes_and_labels(seeded):
    stats = seeded[1]
    assert stats.merchants == 12 and stats.products > 1500 and stats.requests == 700
    assert all("(Demo)" in n for (n,) in sqlite3.connect(seeded[0]).execute("SELECT name FROM merchants"))


def test_ensure_demo_data_initialises_once_and_refreshes_when_stale(tmp_path):
    p = tmp_path / "auto.db"
    assert ensure_demo_data(p, now=AS_OF) is True  # empty -> seeded (as_of is real "now" inside seed; fine for first run)
    assert ensure_demo_data(p) is False  # fresh -> untouched
    with connect(p) as c:
        c.execute("UPDATE app_meta SET value='2000-01-01 00:00:00'")
    assert ensure_demo_data(p) is True  # stale -> refreshed


def test_csv_formula_injection_is_neutralised():
    assert sanitize_cell("=HYPERLINK(\"http://evil\")") == "'=HYPERLINK(\"http://evil\")"
    assert sanitize_cell("+1") == "'+1" and sanitize_cell("@cmd") == "'@cmd" and sanitize_cell("-2+3") == "'-2+3"
    assert sanitize_cell("normal") == "normal" and sanitize_cell(5) == 5
    out = to_csv_bytes(pd.DataFrame({"t": ["=1+1", "ok"], "n": [-5, 3]})).decode("utf-8-sig")
    assert "'=1+1" in out and "-5" in out and "'-5" not in out


def test_executive_report_html(conn):
    html = executive_report_html(conn, run_audit(conn), AS_OF)
    assert "<html" in html and "SLA compliance" in html and DEMO_DATA_NOTICE.split(".")[0][:30] in html
    assert "<script" not in html.lower()


def test_assistant_intents():
    cases = {
        "Give me an executive summary": "summary", "Which requests are breaching SLA?": "sla", "What is our rework rate?": "rework",
        "Which request types take the longest?": "processing", "How many duplicate listings?": "duplicates", "Show price anomalies": "price",
        "Which merchants have the lowest catalog quality?": "quality", "What is the open backlog by priority?": "backlog",
        "Classify: my csv upload failed": "classify", "what is the weather": "unknown",
    }
    for q, intent in cases.items():
        assert detect_intent(q) == intent, q


def test_assistant_answers_are_grounded(conn):
    audit = run_audit(conn)
    for q in ["Give me an executive summary", "Which requests are breaching SLA?", "What is our rework rate?", "Which request types take the longest?",
              "How many duplicate listings do we have?", "Show price anomalies", "Show missing images", "Which merchants have the lowest catalog quality?",
              "open backlog", "Which merchant has the most requests?"]:
        r = answer(q, conn, audit, AS_OF)
        assert r.text and r.intent != "unknown", q
    r = answer("What is our rework rate?", conn, audit, AS_OF)
    assert "%" in r.text and r.table is not None and not r.table.empty


def test_assistant_classify_and_edge_cases(conn):
    audit = run_audit(conn)
    r = answer("Classify: Our CSV upload keeps failing and 300 rows rejected, help asap", conn, audit, AS_OF)
    assert "Bulk Upload Issue" in r.text and "P" in r.text
    assert "Add the request text" in answer("Classify:", conn, audit, AS_OF).text
    assert answer("", conn, audit, AS_OF).intent == "empty"
    assert answer("x" * 501, conn, audit, AS_OF).intent == "invalid"
    assert answer("tell me a joke", conn, audit, AS_OF).intent == "unknown"
