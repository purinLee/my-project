import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from catalog_intel.db import connect  # noqa: E402
from catalog_intel.seed import seed_database  # noqa: E402

AS_OF = datetime(2026, 11, 26, 12, 0, 0)
SEED = 20260101


@pytest.fixture(autouse=True)
def _no_llm(monkeypatch, request):
    """Tests must never call an external API, except tests explicitly marked `live_llm` (which also need a real key)."""
    from catalog_intel import ai

    ai._PENDING.clear()
    ai.reset_daily_counter()
    if request.node.get_closest_marker("live_llm"):
        yield
        return
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr("catalog_intel.ai._secret", lambda name: None)
    yield


@pytest.fixture(scope="session")
def seeded(tmp_path_factory):
    path = tmp_path_factory.mktemp("db") / "test.db"
    stats = seed_database(path, seed=SEED, as_of=AS_OF)
    return path, stats


@pytest.fixture()
def conn(seeded):
    """Read-only connection to the shared seeded database."""
    with connect(seeded[0], readonly=True) as c:
        yield c


@pytest.fixture()
def wconn(tmp_path):
    """Fresh writable database per test, for tests that mutate data."""
    path = tmp_path / "w.db"
    seed_database(path, seed=SEED, as_of=AS_OF)
    with connect(path) as c:
        yield c
