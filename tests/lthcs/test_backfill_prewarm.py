"""Tests for ``scripts/lthcs_backfill_prewarm.py``.

Every test mocks the source modules' public API so we never actually
hit Yahoo, FRED, EIA, SEC, or Finnhub. The script's only contract with
the source modules is "I will call these functions"; the source modules
own their own caching, so the warmer doesn't need to assert anything
about cache files directly.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import threading
import time
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest import mock

import pytest


# ---------------------------------------------------------------------------
# Module loader — the script lives under scripts/ so we import via spec
# ---------------------------------------------------------------------------

SCRIPT_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "lthcs_backfill_prewarm.py"
)


@pytest.fixture(scope="module")
def prewarm():
    spec = importlib.util.spec_from_file_location(
        "lthcs_backfill_prewarm", SCRIPT_PATH
    )
    assert spec and spec.loader, "could not locate prewarm script"
    mod = importlib.util.module_from_spec(spec)
    # Register in sys.modules BEFORE exec — dataclasses introspection in
    # 3.9 walks sys.modules[cls.__module__] when resolving string-form
    # annotations, and missing it raises AttributeError.
    sys.modules["lthcs_backfill_prewarm"] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Per-source mock factory — builds a stub module object suitable for patching
# ---------------------------------------------------------------------------


class _StubCache:
    """Tiny FileCache stand-in. Returns a CacheHit-ish object when key exists."""

    def __init__(self, hits: Optional[set] = None) -> None:
        self._hits = set(hits or set())
        self._set_calls: List[tuple] = []

    def get(self, key: str):
        if key in self._hits:
            # The real FileCache.get returns a CacheHit object with .value;
            # the prewarm code only checks "is not None", so any sentinel works.
            return object()
        return None

    def set(self, key, value, ttl_seconds, **_kw):
        self._set_calls.append((key, ttl_seconds))


def _install_stub_module(name: str, attrs: Dict[str, Any]):
    """Install a module under ``name`` in sys.modules with the given attrs.

    ``from pkg import submod`` first checks for the ``submod`` attribute on
    the already-imported ``pkg`` module, falling back to sys.modules only if
    the attribute is missing. So we have to BOTH register the stub in
    sys.modules AND clobber the attribute on the parent package, otherwise
    the warmer's ``from lthcs.sources import yahoo`` will keep picking up
    the real yahoo module that was loaded before this fixture ran.
    """
    mod = mock.MagicMock(name=name)
    for attr, value in attrs.items():
        setattr(mod, attr, value)
    sys.modules[name] = mod
    # Also bind on the parent package so ``from lthcs.sources import xxx``
    # resolves to our stub.
    if "." in name:
        parent_name, leaf = name.rsplit(".", 1)
        parent = sys.modules.get(parent_name)
        if parent is not None:
            setattr(parent, leaf, mod)
    return mod


@pytest.fixture
def stubbed_sources(monkeypatch):
    """Replace the lthcs.sources.* modules used by the warmer with stubs.

    Returns the dict of installed mock modules so individual tests can
    introspect / further configure them.
    """
    # Snapshot whatever was previously in sys.modules / parent-package
    # attributes for the source modules we're about to stub. The teardown
    # restores these so other tests see the real modules.
    fqnames = [
        "lthcs.sources.yahoo",
        "lthcs.sources.fred",
        "lthcs.sources.eia",
        "lthcs.sources.sec_edgar",
        "lthcs.sources.sec_8k",
        "lthcs.sources.sec_form4",
        "lthcs.sources.sec_13f",
        "lthcs.sources.finnhub",
    ]
    saved_modules: Dict[str, Any] = {n: sys.modules.get(n) for n in fqnames}
    saved_parent_attrs: Dict[str, Any] = {}
    parent = sys.modules.get("lthcs.sources")
    if parent is not None:
        for n in fqnames:
            leaf = n.rsplit(".", 1)[-1]
            saved_parent_attrs[leaf] = getattr(parent, leaf, None)

    stubs: Dict[str, Any] = {}

    # Yahoo
    yahoo_calls: List[str] = []
    yahoo_cache = _StubCache()

    def yahoo_cache_key(ticker, period, as_of):
        return f"{ticker}/{period}/{as_of}"

    def yahoo_get_daily_prices(ticker, period="1y", as_of=None):
        yahoo_calls.append(ticker)
        return [{"date": "2026-05-17", "close": 100.0}]

    stubs["yahoo"] = _install_stub_module(
        "lthcs.sources.yahoo",
        {
            "_cache": yahoo_cache,
            "_cache_key": yahoo_cache_key,
            "get_daily_prices": yahoo_get_daily_prices,
            "_calls": yahoo_calls,
        },
    )

    # FRED
    fred_calls: List[str] = []
    fred_cache = _StubCache()

    def fred_cache_key(series_id, observation_start, as_of):
        return f"{series_id}/all/latest"

    def fred_get_series(series_id, **_kw):
        fred_calls.append(series_id)
        return [{"date": "2026-05-17", "value": 1.0}]

    stubs["fred"] = _install_stub_module(
        "lthcs.sources.fred",
        {
            "_cache": fred_cache,
            "_cache_key": fred_cache_key,
            "get_series": fred_get_series,
            "_calls": fred_calls,
        },
    )

    # EIA
    eia_calls: List[int] = []

    def eia_get_wti():
        eia_calls.append(1)
        return [{"date": "2026-05-17", "value": 80.0}]

    stubs["eia"] = _install_stub_module(
        "lthcs.sources.eia",
        {"get_wti": eia_get_wti, "_calls": eia_calls},
    )

    # SEC EDGAR
    sec_edgar_calls: List[str] = []
    sec_edgar_cache = _StubCache()

    def sec_edgar_get_cik(ticker):
        return f"000{abs(hash(ticker)) % 10_000_000:07d}"

    def sec_edgar_get_company_facts(ticker):
        sec_edgar_calls.append(ticker)
        return {"facts": {"us-gaap": {}}}

    stubs["sec_edgar"] = _install_stub_module(
        "lthcs.sources.sec_edgar",
        {
            "_cache": sec_edgar_cache,
            "get_cik": sec_edgar_get_cik,
            "get_company_facts": sec_edgar_get_company_facts,
            "_calls": sec_edgar_calls,
        },
    )

    # SEC 8-K
    sec_8k_calls: List[str] = []
    sec_8k_cache = _StubCache()

    def sec_8k_get_submissions_json(cik):
        sec_8k_calls.append(cik)
        return {"filings": {"recent": {"form": []}}}

    stubs["sec_8k"] = _install_stub_module(
        "lthcs.sources.sec_8k",
        {
            "_cache": sec_8k_cache,
            "_get_submissions_json": sec_8k_get_submissions_json,
            "_calls": sec_8k_calls,
        },
    )

    # SEC Form 4
    sec_form4_calls: List[str] = []
    sec_form4_cache = _StubCache()

    def sec_form4_get_submissions_json(cik):
        sec_form4_calls.append(cik)
        return {"filings": {"recent": {"form": []}}}

    stubs["sec_form4"] = _install_stub_module(
        "lthcs.sources.sec_form4",
        {
            "_cache": sec_form4_cache,
            "_get_submissions_json": sec_form4_get_submissions_json,
            "_calls": sec_form4_calls,
        },
    )

    # SEC 13F
    sec_13f_calls: List[str] = []
    sec_13f_cache = _StubCache()

    def sec_13f_get_submissions_json(cik):
        sec_13f_calls.append(cik)
        return {"filings": {"recent": {"form": []}}}

    stubs["sec_13f"] = _install_stub_module(
        "lthcs.sources.sec_13f",
        {
            "_cache": sec_13f_cache,
            "_get_submissions_json": sec_13f_get_submissions_json,
            "TRACKED_MANAGERS": {
                "BlackRock": "0002012383",
                "Vanguard": "0000102909",
                "State Street": "0000093751",
            },
            "_calls": sec_13f_calls,
        },
    )

    # Finnhub
    finnhub_calls: List[str] = []
    finnhub_cache = _StubCache()

    def finnhub_reco_cache_key(ticker, as_of=None):
        return f"{ticker}/{as_of or 'all'}"

    def finnhub_get_recommendation_trends(ticker, as_of=None):
        finnhub_calls.append(ticker)
        return [{"period": "2026-05-01", "buy": 5}]

    stubs["finnhub"] = _install_stub_module(
        "lthcs.sources.finnhub",
        {
            "_RECO_CACHE": finnhub_cache,
            "_reco_cache_key": finnhub_reco_cache_key,
            "get_recommendation_trends": finnhub_get_recommendation_trends,
            "_calls": finnhub_calls,
        },
    )

    yield stubs

    # Restore whatever was previously in sys.modules so other test files
    # see the REAL source modules.
    for fqname, original in saved_modules.items():
        if original is None:
            sys.modules.pop(fqname, None)
        else:
            sys.modules[fqname] = original
    if parent is not None:
        for leaf, original in saved_parent_attrs.items():
            if original is None:
                try:
                    delattr(parent, leaf)
                except AttributeError:
                    pass
            else:
                setattr(parent, leaf, original)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_load_universe_filters_inactive(prewarm, tmp_path):
    """Active=False entries are dropped; ticker strings are upper-cased."""
    universe_path = tmp_path / "universe.json"
    universe_path.write_text(
        json.dumps(
            {
                "tickers": [
                    {"ticker": "aapl", "active": True},
                    {"ticker": "OLD", "active": False},
                    {"ticker": "MSFT"},  # no active key -> defaults to True
                    {"not_a_ticker": "garbage"},
                ]
            }
        )
    )
    out = prewarm.load_universe(universe_path)
    assert out == ["AAPL", "MSFT"]


def test_dry_run_reports_planned_without_making_calls(
    prewarm, stubbed_sources, tmp_path
):
    """Dry-run mode tallies what WOULD be fetched but doesn't fire any calls."""
    tickers = ["AAPL", "MSFT", "NVDA"]
    report = prewarm.run_prewarm(
        tickers=tickers,
        days=30,
        dry_run=True,
        write_status_file=False,
        status_path=tmp_path / "status.json",
    )
    # No source module function should have been called.
    assert stubbed_sources["yahoo"]._calls == []
    assert stubbed_sources["fred"]._calls == []
    assert stubbed_sources["sec_edgar"]._calls == []
    assert stubbed_sources["finnhub"]._calls == []

    # Planned counts are populated.
    assert report.results["yahoo"].planned == 3
    assert report.results["yahoo"].live == 0
    assert report.results["sec_edgar"].planned == 3
    # FRED has 11 hardcoded series.
    assert report.results["fred"].planned == 11
    # 13F has 3 stubbed managers.
    assert report.results["sec_13f"].planned == 3

    # No status file written when write_status_file=False.
    assert not (tmp_path / "status.json").exists()


def test_skip_flags_skip_the_right_sources(prewarm, stubbed_sources, tmp_path):
    """--skip-yahoo / --skip-fred / --skip-sec / --skip-finnhub mute each group."""
    report = prewarm.run_prewarm(
        tickers=["AAPL", "MSFT"],
        days=30,
        skip_yahoo=True,
        skip_fred=True,
        skip_sec=True,
        skip_finnhub=True,
        write_status_file=False,
        status_path=tmp_path / "status.json",
    )
    # Every source is marked skipped.
    for name in ("yahoo", "fred", "eia", "sec_edgar", "sec_8k", "sec_form4", "sec_13f", "finnhub"):
        assert report.results[name].skipped, name

    # And no upstream calls happened.
    assert stubbed_sources["yahoo"]._calls == []
    assert stubbed_sources["fred"]._calls == []
    assert stubbed_sources["eia"]._calls == []
    assert stubbed_sources["sec_edgar"]._calls == []
    assert stubbed_sources["finnhub"]._calls == []


def test_failure_in_one_source_does_not_abort_others(
    prewarm, stubbed_sources, tmp_path
):
    """One source raising mid-warm leaves the others still completing."""
    # Make Yahoo raise for one ticker, succeed for the other.
    fail_calls: List[str] = []

    def yahoo_fail(ticker, period="1y", as_of=None):
        fail_calls.append(ticker)
        if ticker == "BAD":
            raise RuntimeError("simulated upstream blow-up")
        return [{"date": "2026-05-17", "close": 100.0}]

    stubbed_sources["yahoo"].get_daily_prices = yahoo_fail

    # Make FRED raise on one series.
    def fred_partial_fail(series_id, **_kw):
        if series_id == "CPIAUCSL":
            raise RuntimeError("FRED 500")
        return []

    stubbed_sources["fred"].get_series = fred_partial_fail

    report = prewarm.run_prewarm(
        tickers=["GOOD", "BAD"],
        days=30,
        write_status_file=False,
        status_path=tmp_path / "status.json",
    )

    # Yahoo: one good, one failure.
    assert report.results["yahoo"].failures == 1
    assert report.results["yahoo"].live == 1
    assert any("BAD" in e for e in report.results["yahoo"].errors)

    # FRED: 10 successes + 1 failure (CPIAUCSL).
    assert report.results["fred"].failures == 1
    assert report.results["fred"].live == 10

    # Other sources still ran and succeeded.
    assert report.results["sec_edgar"].live + report.results["sec_edgar"].cached_prior == 2
    assert report.results["finnhub"].live + report.results["finnhub"].cached_prior == 2


def test_cached_prior_is_counted_separately_from_live(
    prewarm, stubbed_sources, tmp_path
):
    """When a cache hit pre-exists, the ticker counts as cached_prior not live."""
    # Pre-populate the yahoo stub cache for one ticker.
    yahoo_cache = stubbed_sources["yahoo"]._cache
    yahoo_cache._hits.add("AAPL/1y/None")  # matches our stub _cache_key format

    report = prewarm.run_prewarm(
        tickers=["AAPL", "MSFT"],
        days=30,
        write_status_file=False,
        status_path=tmp_path / "status.json",
    )
    assert report.results["yahoo"].cached_prior == 1
    assert report.results["yahoo"].live == 1


def test_progress_logging_emits_lines(prewarm, stubbed_sources, tmp_path):
    """Progress output appears on stdout while the warmer runs."""
    buf = io.StringIO()
    # Print directly via update — that's the contract under test.
    progress = prewarm.ProgressPrinter(throttle_seconds=0.0)
    with redirect_stdout(buf):
        prewarm.run_prewarm(
            tickers=["AAPL", "MSFT", "NVDA"],
            days=30,
            skip_sec=True,  # smaller test, less log noise
            skip_finnhub=True,
            progress=progress,
            write_status_file=False,
            status_path=tmp_path / "status.json",
        )
    output = buf.getvalue()
    assert "Yahoo" in output
    assert "FRED" in output
    # Should reflect the progress format ``done/total``.
    assert "3/3" in output or "11/11" in output


def test_status_file_written_after_successful_run(
    prewarm, stubbed_sources, tmp_path
):
    """A small JSON status file lands at the requested path."""
    status_path = tmp_path / "data" / "lthcs" / "prewarm_status.json"
    report = prewarm.run_prewarm(
        tickers=["AAPL", "MSFT"],
        days=42,
        end_date=date(2026, 5, 17),
        write_status_file=True,
        status_path=status_path,
    )
    assert status_path.exists()
    payload = json.loads(status_path.read_text())
    assert payload["window_days"] == 42
    assert payload["end_date"] == "2026-05-17"
    assert payload["universe_size"] == 2
    assert "yahoo" in payload["sources_warmed"]
    assert "fred" in payload["sources_warmed"]
    assert "sec_edgar" in payload["sources_warmed"]
    assert payload["sources_skipped"] == []
    assert "duration_seconds" in payload
    assert payload["dry_run"] is False


def test_skipped_sources_appear_in_status_file(prewarm, stubbed_sources, tmp_path):
    """Skipped sources show up under ``sources_skipped`` for backfill detection."""
    status_path = tmp_path / "prewarm_status.json"
    prewarm.run_prewarm(
        tickers=["AAPL"],
        days=30,
        skip_sec=True,
        write_status_file=True,
        status_path=status_path,
    )
    payload = json.loads(status_path.read_text())
    for sec_source in ("sec_edgar", "sec_8k", "sec_form4", "sec_13f"):
        assert sec_source in payload["sources_skipped"], sec_source
        assert sec_source not in payload["sources_warmed"], sec_source


def test_yahoo_concurrency_cap_respected(prewarm, stubbed_sources, tmp_path):
    """The Yahoo thread pool never has more than ``max_concurrency`` workers in flight."""
    inflight = {"current": 0, "peak": 0}
    lock = threading.Lock()

    def slow_yahoo(ticker, period="1y", as_of=None):
        with lock:
            inflight["current"] += 1
            inflight["peak"] = max(inflight["peak"], inflight["current"])
        # Hold the slot long enough that the executor will queue work.
        time.sleep(0.02)
        with lock:
            inflight["current"] -= 1
        return [{"date": "2026-05-17", "close": 100.0}]

    stubbed_sources["yahoo"].get_daily_prices = slow_yahoo

    tickers = [f"T{i:03d}" for i in range(20)]
    prewarm.run_prewarm(
        tickers=tickers,
        days=30,
        max_concurrency=3,
        skip_fred=True,
        skip_sec=True,
        skip_finnhub=True,
        write_status_file=False,
        status_path=tmp_path / "status.json",
    )
    # Peak must never exceed the requested max_concurrency.
    assert inflight["peak"] <= 3
    # And we DID actually parallelize (otherwise peak would be 1).
    assert inflight["peak"] >= 2


def test_cli_dry_run_exits_zero(prewarm, stubbed_sources, tmp_path, monkeypatch):
    """`main(['--dry-run', '--tickers', 'AAPL,MSFT'])` returns 0 and prints planned counts."""
    # Point the status file path to tmp so we don't pollute the repo.
    monkeypatch.setattr(prewarm, "_STATUS_PATH", tmp_path / "status.json")

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = prewarm.main(
            ["--dry-run", "--tickers", "AAPL,MSFT", "--days", "30"]
        )
    output = buf.getvalue()
    assert rc == 0
    assert "DRY RUN" in output
    assert "Universe: 2 tickers" in output
    assert "planned=" in output
    # No status file on dry run.
    assert not (tmp_path / "status.json").exists()


def test_cli_rejects_bad_end_date(prewarm, capsys):
    """Garbage --end value exits non-zero with a clear message."""
    rc = prewarm.main(["--end", "not-a-date", "--tickers", "AAPL", "--dry-run"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "ISO" in err
