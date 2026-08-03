"""Tests for fetch_live.py after the dead ETF-flow paths were removed.

WHAT THIS PINS
--------------
fetch_live.py used to carry three ETF-flow integrations that could not run:
CoinGlass (gated on COINGLASS_API_KEY), SoSoValue (gated on SOSOVALUE_API_KEY,
whose host no longer resolves), and a `fetch_all` dispatcher reachable only via
`app.py --fetch` / `v2/app.py --fetch` — a flag no workflow passes. Two repo
secrets existed for that dead code.

What survives is the one reachable function: `fetch_btc_from_github_mirror`,
called by `server.py`'s POST /api/seed-etf, which the dashboard's "Seed BTC
(mirror)" button fires.

The tests below cover (a) that the removed names are really gone, (b) that
NOTHING in the repo still reaches for them — the mistake that made this dead
code survive so long was never checking the call sites — and (c) that the
surviving mirror path cannot silently regress data/btc_flows.csv, since the
mirror is abandoned (last row 2025-05-02) and the committed CSV runs to
2026-05-12 with 13 per-fund columns against the mirror's Total-only shape.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import fetch_live  # noqa: E402


REMOVED = ["fetch_coinglass", "fetch_sosovalue", "_normalize_provider_rows",
           "_to_millions", "write_csv"]

# Directories that are not this repo's source.
SKIP_DIRS = {".venv", ".git", "node_modules", "__pycache__", ".pytest_cache",
             ".cache", "MagicMock"}


def _repo_py_files():
    for p in ROOT.rglob("*.py"):
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        yield p


class _FakeResponse:
    def __init__(self, text: str):
        self.text = text

    def raise_for_status(self):
        return None


MIRROR_CSV = '\n'.join([
    'Date,Total',
    '"20240111T",655.3',
    '"20240112T",203.0',
    '"20250502T",-96.4',
    '',
])


# --------------------------------------------------------------------------
# The removed paths
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name", REMOVED)
def test_dead_provider_helpers_are_gone(name):
    assert not hasattr(fetch_live, name), f"{name} should have been removed"


def test_the_mirror_is_the_only_host_this_module_can_reach():
    """CoinGlass and SoSoValue are named in the docstring as history; neither
    survives as something the code can actually call."""
    hosts = set(re.findall(r"https?://([^/\s\"']+)", (ROOT / "fetch_live.py").read_text()))
    assert hosts == {"raw.githubusercontent.com"}, hosts


def test_module_can_no_longer_read_an_api_key():
    """`import os` went with the provider paths, so COINGLASS_API_KEY and
    SOSOVALUE_API_KEY are unreadable from here — not merely unused."""
    assert not hasattr(fetch_live, "os")
    assert not hasattr(fetch_live, "csv")


def test_every_fetch_live_attribute_used_in_the_repo_still_exists():
    """The check that should have been run before this code was written.

    Anything in the repo that says `fetch_live.<name>` must resolve, otherwise
    a deletion here becomes an AttributeError somewhere else.
    """
    ref_re = re.compile(r"\bfetch_live\.([A-Za-z_][A-Za-z0-9_]*)")
    missing = []
    for path in _repo_py_files():
        for attr in set(ref_re.findall(path.read_text(errors="ignore"))):
            if attr == "py":          # prose/paths, e.g. "fetch_live.py"
                continue
            if not hasattr(fetch_live, attr):
                missing.append(f"{path.relative_to(ROOT)} -> fetch_live.{attr}")
    assert not missing, missing


def test_fetch_all_is_a_tombstone_that_explains_itself(tmp_path, monkeypatch):
    """`app.py --fetch` and `v2/app.py --fetch` still call this. They are in
    files this change may not touch, so the retirement has to answer them with
    a sentence rather than an AttributeError."""
    def explode(*a, **k):  # pragma: no cover - must never be reached
        raise AssertionError("fetch_all must not touch the network")
    monkeypatch.setattr(fetch_live.requests, "get", explode)

    with pytest.raises(RuntimeError) as ei:
        fetch_live.fetch_all(tmp_path)
    msg = str(ei.value)
    assert "retired" in msg
    assert "scripts/fetch_etf_flows.py" in msg
    assert not list(tmp_path.iterdir()), "tombstone must not write files"


# --------------------------------------------------------------------------
# The surviving path
# --------------------------------------------------------------------------

def test_server_seed_route_still_binds_to_the_surviving_function():
    """This is why the mirror function was kept: it is reachable."""
    import server
    assert server.fetch_live.fetch_btc_from_github_mirror is \
        fetch_live.fetch_btc_from_github_mirror


def test_mirror_writes_total_only_csv(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch_live.requests, "get",
                        lambda *a, **k: _FakeResponse(MIRROR_CSV))
    n = fetch_live.fetch_btc_from_github_mirror(tmp_path)
    assert n == 3
    written = (tmp_path / "btc_flows.csv").read_text()
    assert written.splitlines()[0] == "date,Total"
    assert "2024-01-11,655.3" in written
    assert written.splitlines()[-1] == "2025-05-02,-96.4"


def test_mirror_skips_unparsable_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch_live.requests, "get", lambda *a, **k: _FakeResponse(
        'Date,Total\n"20240111T",655.3\ngarbage\n"20240112T",not-a-number\n'))
    assert fetch_live.fetch_btc_from_github_mirror(tmp_path) == 1


def test_mirror_refuses_to_regress_newer_local_data(tmp_path, monkeypatch):
    """The shape of the real repo: data/btc_flows.csv runs to 2026-05-12 with a
    per-fund breakdown; the mirror stops at 2025-05-02 and carries Total only.
    One button click must not throw away a year of data."""
    existing = ("date,IBIT,FBTC,Total\n"
                "2026-05-11,-7.4,-3.6,27.2\n"
                "2026-05-12,0,-86.1,-115.2\n")
    (tmp_path / "btc_flows.csv").write_text(existing)
    monkeypatch.setattr(fetch_live.requests, "get",
                        lambda *a, **k: _FakeResponse(MIRROR_CSV))

    with pytest.raises(RuntimeError) as ei:
        fetch_live.fetch_btc_from_github_mirror(tmp_path)
    msg = str(ei.value)
    assert "2026-05-12" in msg and "2025-05-02" in msg
    # And the file really is untouched.
    assert (tmp_path / "btc_flows.csv").read_text() == existing


def test_mirror_still_writes_when_it_is_actually_ahead(tmp_path, monkeypatch):
    (tmp_path / "btc_flows.csv").write_text("date,Total\n2023-12-31,1.0\n")
    monkeypatch.setattr(fetch_live.requests, "get",
                        lambda *a, **k: _FakeResponse(MIRROR_CSV))
    assert fetch_live.fetch_btc_from_github_mirror(tmp_path) == 3


def test_committed_btc_flows_is_newer_than_the_mirror_can_offer():
    """Documents the premise of the guard against the real committed file."""
    csv = (ROOT / "data" / "btc_flows.csv").read_text()
    assert fetch_live._newest_date(csv) > "2025-05-02"
    assert csv.splitlines()[0].count(",") > 1, "per-fund columns, not Total-only"


@pytest.mark.parametrize("body,expected", [
    ("date,Total\n2024-01-11,1\n2026-05-12,2\n", "2026-05-12"),
    ("date,Total\n", ""),
    ("", ""),
    ("date,Total\nnot-a-date,5\n", ""),
])
def test_newest_date(body, expected):
    assert fetch_live._newest_date(body) == expected
