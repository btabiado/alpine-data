"""Shared pytest fixtures for the BTC/ETH ETF dashboard tests."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make the project root importable so `import app`, `import server`, etc. work.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _isolate_insights_history(tmp_path, monkeypatch):
    """Redirect the rolling insights-history file to a tmp path for every
    test so calling ``insights.build_insights`` doesn't write to the real
    ``data/insights_history.json`` or read stale rows left by a prior run.

    Autouse so individual tests don't need to remember the fixture; the
    cost is one ``import insights`` per test (cheap, already cached after
    the first hit). Tests that *want* to seed prior days can write to the
    same path with ``insights._HISTORY_PATH.write_text(...)``.
    """
    try:
        import insights
    except Exception:
        # Some tests don't import insights at all — skip rebinding rather
        # than failing the test collection phase.
        return
    monkeypatch.setattr(insights, "_HISTORY_PATH", tmp_path / "insights_history.json")
