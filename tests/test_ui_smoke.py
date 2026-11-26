"""Headless smoke tests: every page renders without raising, using Streamlit's AppTest runner."""
import os

import pytest
from streamlit.testing.v1 import AppTest

PAGES = {
    "overview": "p_overview", "catalog": "p_catalog", "audit": "p_audit",
    "requests": "p_requests", "sql": "p_sql", "assistant": "p_assistant",
}


@pytest.fixture(scope="module", autouse=True)
def _db_env(tmp_path_factory):
    path = tmp_path_factory.mktemp("ui") / "ui.db"
    os.environ["CATALOG_DB_PATH"] = str(path)
    yield
    os.environ.pop("CATALOG_DB_PATH", None)


def _app(module: str) -> AppTest:
    script = f"from ui import common, {module}\ncommon.inject_css()\ncommon.init_demo()\n{module}.render()\n"
    return AppTest.from_string(script, default_timeout=120)


@pytest.mark.parametrize("module", PAGES.values())
def test_page_renders_without_exception(module):
    at = _app(module).run()
    assert not at.exception, [e.value for e in at.exception]


def test_overview_shows_kpis_and_synthetic_notice():
    at = _app("p_overview").run()
    html = " ".join(m.value for m in at.markdown)
    assert "SLA compliance" in html and "Synthetic demo data" in html


def test_requests_new_request_preview_and_create():
    at = _app("p_requests").run()
    # the "New request" tab is rendered in the same script run; fill the form widgets by key
    at.text_input(key="nr_s").set_value("CSV upload failing with errors")
    at.text_area(key="nr_d").set_value("Our bulk CSV upload fails, 120 rows rejected. Please help asap.")
    at.run()
    assert not at.exception
    assert any("Bulk Upload Issue" in m.value for m in at.markdown)
    at.button(key="nr_go").click().run()
    assert not at.exception


def test_assistant_chat_flow():
    at = _app("p_assistant").run()
    at.button(key="ex_1").click().run()
    assert not at.exception
    assert any("breached" in m.value.lower() for m in at.markdown)


def test_assistant_llm_panel_without_key_explains_fallback():
    at = _app("p_assistant").run()
    assert not at.exception
    assert any("No OPENAI_API_KEY" in i.value for i in at.info)


def test_assistant_live_ping_is_logged_with_fake_llm(monkeypatch):
    import json

    from catalog_intel import ai

    class Resp:
        def read(self):
            return json.dumps({"choices": [{"message": {"content": "OK"}}], "usage": {"prompt_tokens": 9, "completion_tokens": 1}}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(ai, "_secret", lambda name: "sk-test" if name == "OPENAI_API_KEY" else None)
    monkeypatch.setattr(ai.urllib.request, "urlopen", lambda req, timeout=0: Resp())
    at = _app("p_assistant").run()
    at.button(key="llm_ping").click().run()
    assert not at.exception
    assert any("status=ok" in s.value for s in at.success)
    assert any(m.label == "Calls logged" and int(m.value) >= 1 for m in at.metric)


def test_automation_dashboard_renders_and_runs_pipeline():
    at = _app("p_automation").run()
    assert not at.exception, [e.value for e in at.exception]
    html = " ".join(m.value for m in at.markdown)
    assert "MEASURED" in html and "ESTIMATED" in html and "SYNTHETIC" in html
    at.button(key="run_py_gen").click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert any("Run #" in s.value and "success" in s.value for s in at.success)
