import json
import re
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from catalog_intel import ai
from catalog_intel.automation import Source, ensure_sandbox, run_pipeline
from catalog_intel.workload import generate_workload

ROOT = Path(__file__).resolve().parent.parent
HAS_GIT = shutil.which("git") is not None and (ROOT / ".git").exists()

SECRET_PATTERNS = {
    "OpenAI-style key": re.compile(r"sk-[A-Za-z0-9_\-]{20,}"),
    "AWS access key": re.compile(r"AKIA[0-9A-Z]{16}"),
    "GitHub token": re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),
    "private key block": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "hard-coded password/secret": re.compile(r"(?i)(password|passwd|secret|api[_-]?key|token)\s*[=:]\s*['\"][A-Za-z0-9/+_\-]{12,}['\"]"),
}
EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
PHONE = re.compile(r"(?<!\d)(?:\+?\d{1,3}[ -])?\(?\d{2,4}\)?[ -]\d{3,4}[ -]\d{4}(?!\d)")
FAKE_KEY = "sk-test-0123456789abcdefghijKLMNOP"


def tracked_files() -> list[Path]:
    out = subprocess.run(["git", "ls-files", "-z"], capture_output=True, cwd=ROOT, timeout=30).stdout.decode("utf-8", "replace")
    return [ROOT / p for p in out.split("\0") if p and (ROOT / p).is_file()]


@pytest.mark.skipif(not HAS_GIT, reason="needs a git checkout")
def test_no_secrets_in_tracked_files():
    hits = []
    for p in tracked_files():
        if p.suffix.lower() in (".png", ".jpg", ".xlsx", ".db", ".zip"):
            continue
        text = p.read_text(encoding="utf-8", errors="ignore")
        for name, rx in SECRET_PATTERNS.items():
            for m in rx.finditer(text):
                line = text[: m.start()].count("\n") + 1
                # test fixtures use obviously fake keys and the docs show placeholders like sk-...
                if "test" in m.group(0).lower() or "..." in m.group(0) or p.name in ("test_privacy.py", "test_llm_logging.py"):
                    continue
                hits.append(f"{p.relative_to(ROOT)}:{line} {name}")
    assert not hits, hits


@pytest.mark.skipif(not HAS_GIT, reason="needs a git checkout")
def test_sensitive_paths_are_gitignored_and_untracked():
    for path in (".env", ".env.local", ".streamlit/secrets.toml", "data/catalog.db", "data/perf/perf_1.db", "data/automation/sandbox.db", "data/automation/sandbox.db-wal"):
        r = subprocess.run(["git", "check-ignore", "-q", path], cwd=ROOT)
        assert r.returncode == 0, f"{path} is not ignored"
    tracked = {str(p.relative_to(ROOT)).replace("\\", "/") for p in tracked_files()}
    assert not [t for t in tracked if t.endswith((".db", ".db-wal", ".db-shm")) or t.startswith(("data/", ".env")) or t == ".streamlit/secrets.toml"]
    assert ".streamlit/secrets.toml.example" in tracked


def test_synthetic_workload_contains_no_emails_or_phone_numbers():
    w = generate_workload(4, 300, seed=11)
    blob = "\n".join(df.to_csv() for df in w.raw_frames.values())
    assert not EMAIL.findall(blob) and not PHONE.findall(blob)


def test_seeded_demo_database_contains_no_personal_data(seeded):
    con = sqlite3.connect(seeded[0])
    texts = []
    for table in ("merchants", "products", "merchant_requests", "request_events"):
        cols = [r[1] for r in con.execute(f"PRAGMA table_info({table})") if r[2].upper() == "TEXT"]
        for col in cols:
            texts += [str(r[0]) for r in con.execute(f"SELECT {col} FROM {table} WHERE {col} IS NOT NULL")]
    blob = "\n".join(texts)
    assert not EMAIL.findall(blob) and not PHONE.findall(blob)
    assert all("(Demo)" in n for (n,) in con.execute("SELECT name FROM merchants"))


def test_run_history_and_reports_never_contain_row_contents_or_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(ai, "_secret", lambda name: FAKE_KEY if name == "OPENAI_API_KEY" else None)
    w = generate_workload(2, 120, seed=21)
    db = ensure_sandbox(tmp_path / "s.db")
    r = run_pipeline([Source(n, d) for n, d in w.files], db, out_dir=tmp_path / "out", workload_label="privacy test")
    con = sqlite3.connect(db)
    history = json.dumps([list(row) for row in con.execute("SELECT * FROM automation_runs")])
    details = json.loads(con.execute("SELECT details FROM automation_runs").fetchone()[0])
    assert FAKE_KEY not in history
    assert {k for d in details for k in d} <= {"file", "sha", "merchant", "status", "rows", "valid", "rejected", "inserted", "updated", "error"}
    sample_title = w.raw_frames[w.files[0][0]]["title"].iloc[0]
    assert sample_title not in history  # counts, timings and hashes only
    for p in (tmp_path / "out").iterdir():
        if p.suffix in (".html", ".csv"):
            assert FAKE_KEY not in p.read_text(encoding="utf-8", errors="ignore")
    assert r.status == "success"
    assert ai.pending_calls() == []  # the automation never calls an LLM


def test_automation_never_calls_the_network(tmp_path, monkeypatch):
    import socket

    def no_net(*a, **k):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "create_connection", no_net)
    monkeypatch.setattr(socket.socket, "connect", no_net)
    w = generate_workload(2, 80, seed=2)
    assert run_pipeline([Source(n, d) for n, d in w.files], ensure_sandbox(tmp_path / "s.db")).status == "success"
