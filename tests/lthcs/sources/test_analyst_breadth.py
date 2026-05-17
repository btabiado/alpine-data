"""Tests for ``lthcs.sources.analyst_breadth``.

This module is a derived signal — it reads the on-disk cache that
``yahoo_events.get_analyst_actions`` writes and aggregates it into a
rolling breadth score. Every test below seeds an isolated tmp_path
cache via the same ``FileCache`` API the production module uses, so we
exercise the real cache layout without hitting the network.

Fixture JSON files live under ``tests/fixtures/analyst_breadth/`` and
mirror the cache envelope ``yahoo_events`` writes: a list of action
dicts shaped like::

    {
      "ticker": "NVDA",
      "date": "2026-05-10",
      "firm": "Goldman Sachs",
      "action": "Upgrades",
      "from_grade": "Hold",
      "to_grade": "Buy",
      "direction": 1.0,
    }
"""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from lthcs.sources import analyst_breadth
from lthcs.sources._cache import FileCache


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "analyst_breadth"


def _today() -> _dt.date:
    return _dt.date.today()


def _days_ago(n: int) -> str:
    return (_today() - _dt.timedelta(days=n)).isoformat()


def _seed_cache(
    cache_root: Path,
    ticker: str,
    actions: List[Dict[str, Any]],
    pipeline_days: int = 90,
) -> None:
    """Write a cache envelope under the ``yahoo_reco`` source dir.

    Uses the same FileCache the production module reads, so the
    sanitised-hash filename matches what ``_load_cached_actions``
    expects to find on the lookup side.
    """
    cache = FileCache("yahoo_reco", root=cache_root)
    key = f"{ticker.upper()}/analyst_actions/{int(pipeline_days)}"
    cache.set(key, actions, ttl_seconds=24 * 60 * 60)


def _load_fixture(name: str) -> List[Dict[str, Any]]:
    """Load a fixture JSON file. Dates inside are encoded relative to today
    via the substitution ``"__DAYS_AGO_<n>__"`` so the suite is reproducible
    regardless of the current date.
    """
    raw = json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))
    return [_substitute_dates(row) for row in raw]


def _substitute_dates(row: Dict[str, Any]) -> Dict[str, Any]:
    """Replace ``__DAYS_AGO_<n>__`` tokens in a row's ``date`` field."""
    out = dict(row)
    date_val = out.get("date")
    if isinstance(date_val, str) and date_val.startswith("__DAYS_AGO_"):
        # Token shape: __DAYS_AGO_<n>__
        try:
            n = int(date_val[len("__DAYS_AGO_") : -2])
        except ValueError:
            return out
        out["date"] = _days_ago(n)
    return out


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_happy_path_rich_history(tmp_path: Path) -> None:
    """A ticker with a mix of upgrades, downgrades, inits, and reits in the
    30-day window produces all the expected counts and a positive regime."""
    actions = _load_fixture("rich_history.json")
    _seed_cache(tmp_path, "NVDA", actions)

    out = analyst_breadth.compute_analyst_breadth(
        "NVDA", cache_dir=tmp_path, window_days=30
    )
    assert out is not None
    assert out["ticker"] == "NVDA"
    assert out["window_days"] == 30
    # Counts (only events <=30 days old land in the window).
    assert out["upgrades"] == 3
    assert out["downgrades"] == 1
    assert out["initiations_bullish"] == 1
    assert out["initiations_bearish"] == 0
    assert out["reiterations_bullish"] == 2
    assert out["reiterations_bearish"] == 1
    # Net = 3 - 1 + 0.5 + (2*0.3) - 0.3 = 3 - 1 + 0.5 + 0.6 - 0.3 = 2.8
    assert out["net_actions"] == pytest.approx(2.8, abs=1e-6)
    # breadth_score = 2.8 / 8.0 = 0.35 -> "improving"
    assert out["breadth_score"] == pytest.approx(0.35, abs=1e-4)
    assert out["regime"] == "improving"
    assert out["firm_count"] >= 5
    # 3 up + 1 down + 1 init_bull + 2 reit_bull + 1 reit_bear = 8 in-window
    # (the day-120 row is filtered out as too old).
    assert len(out["raw_actions"]) == 8
    # Newest-first ordering.
    assert out["raw_actions"][0]["date"] >= out["raw_actions"][-1]["date"]


def test_returns_none_when_no_cache_entry(tmp_path: Path) -> None:
    """No file on disk at all -> None (distinct from zeroed)."""
    out = analyst_breadth.compute_analyst_breadth(
        "UNKNOWN", cache_dir=tmp_path, window_days=30
    )
    assert out is None


def test_empty_cache_returns_zeroed_breadth(tmp_path: Path) -> None:
    """A cached empty list (yahoo_events found no actions) -> zeroed dict."""
    _seed_cache(tmp_path, "ABBV", [])

    out = analyst_breadth.compute_analyst_breadth(
        "ABBV", cache_dir=tmp_path, window_days=30
    )
    assert out is not None
    assert out["upgrades"] == 0
    assert out["downgrades"] == 0
    assert out["net_actions"] == pytest.approx(0.0)
    assert out["breadth_score"] == pytest.approx(0.0)
    assert out["regime"] == "stable"
    assert out["firm_count"] == 0
    assert out["raw_actions"] == []


def test_actions_outside_window_are_excluded(tmp_path: Path) -> None:
    """Only actions inside ``window_days`` should count; older are dropped."""
    actions = [
        {
            "ticker": "MSFT",
            "date": _days_ago(5),
            "firm": "Fresh",
            "action": "Upgrades",
            "from_grade": "Hold",
            "to_grade": "Buy",
            "direction": 1.0,
        },
        {
            "ticker": "MSFT",
            "date": _days_ago(60),  # outside 30d window
            "firm": "Stale",
            "action": "Downgrades",
            "from_grade": "Buy",
            "to_grade": "Hold",
            "direction": -1.0,
        },
    ]
    _seed_cache(tmp_path, "MSFT", actions)

    out = analyst_breadth.compute_analyst_breadth(
        "MSFT", cache_dir=tmp_path, window_days=30
    )
    assert out is not None
    assert out["upgrades"] == 1
    assert out["downgrades"] == 0
    assert out["firm_count"] == 1
    assert len(out["raw_actions"]) == 1
    assert out["raw_actions"][0]["firm"] == "Fresh"


def test_mixed_up_and_down_net_arithmetic(tmp_path: Path) -> None:
    """2 upgrades vs 3 downgrades -> negative net actions, deteriorating regime."""
    actions = [
        _action(_days_ago(1), "Upgrades", "Buy", "Hold", "F1"),
        _action(_days_ago(2), "Upgrades", "Buy", "Hold", "F2"),
        _action(_days_ago(3), "Downgrades", "Hold", "Buy", "F3"),
        _action(_days_ago(4), "Downgrades", "Hold", "Buy", "F4"),
        _action(_days_ago(5), "Downgrades", "Sell", "Hold", "F5"),
    ]
    _seed_cache(tmp_path, "TSLA", actions)

    out = analyst_breadth.compute_analyst_breadth(
        "TSLA", cache_dir=tmp_path, window_days=30
    )
    assert out is not None
    # net = 2*1.0 + 3*(-1.0) = -1.0
    assert out["net_actions"] == pytest.approx(-1.0)
    assert out["breadth_score"] == pytest.approx(-0.125, abs=1e-4)
    # -0.125 is in (-0.2, 0.2) -> stable
    assert out["regime"] == "stable"


# ---------------------------------------------------------------------------
# Regime classification boundaries
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "n_up,n_down,expected_regime",
    [
        (0, 0, "stable"),
        # net = 1 -> score = 0.125 -> stable
        (1, 0, "stable"),
        # net = 2 -> 0.25 -> improving
        (2, 0, "improving"),
        # net = 5 -> 0.625 -> strongly_improving (just past 0.6)
        (5, 0, "strongly_improving"),
        (0, 2, "deteriorating"),
        (0, 5, "strongly_deteriorating"),
    ],
)
def test_regime_classification_boundaries(
    tmp_path: Path, n_up: int, n_down: int, expected_regime: str
) -> None:
    actions: List[Dict[str, Any]] = []
    for i in range(n_up):
        actions.append(_action(_days_ago(i + 1), "Upgrades", "Buy", "Hold", f"U{i}"))
    for i in range(n_down):
        actions.append(_action(_days_ago(i + 1), "Downgrades", "Hold", "Buy", f"D{i}"))
    _seed_cache(tmp_path, "X", actions)

    out = analyst_breadth.compute_analyst_breadth(
        "X", cache_dir=tmp_path, window_days=30
    )
    assert out is not None
    assert out["regime"] == expected_regime


# ---------------------------------------------------------------------------
# Grade classification edge cases
# ---------------------------------------------------------------------------


def test_unknown_to_grade_treated_as_neutral_init(tmp_path: Path) -> None:
    """An init with an unknown ``to_grade`` string -> neither bull nor bear."""
    actions = [
        _action(_days_ago(1), "Initiates", "Mystery Rating", None, "Firm1"),
        # A standard bullish init for contrast.
        _action(_days_ago(2), "Initiates", "Buy", None, "Firm2"),
    ]
    _seed_cache(tmp_path, "ABC", actions)

    out = analyst_breadth.compute_analyst_breadth(
        "ABC", cache_dir=tmp_path, window_days=30
    )
    assert out is not None
    # Only the "Buy" init counts as init_bullish.
    assert out["initiations_bullish"] == 1
    assert out["initiations_bearish"] == 0
    # Net should only reflect the one bullish init: +0.5
    assert out["net_actions"] == pytest.approx(0.5)


def test_neutral_init_grade_is_not_counted(tmp_path: Path) -> None:
    """Hold/Neutral/Equal-Weight inits are NOT counted in either bullish or
    bearish init buckets (per spec)."""
    actions = [
        _action(_days_ago(1), "Initiates", "Hold", None, "F1"),
        _action(_days_ago(2), "Initiates", "Neutral", None, "F2"),
        _action(_days_ago(3), "Initiates", "Equal-Weight", None, "F3"),
        _action(_days_ago(4), "Initiates", "Market Perform", None, "F4"),
    ]
    _seed_cache(tmp_path, "XYZ", actions)

    out = analyst_breadth.compute_analyst_breadth(
        "XYZ", cache_dir=tmp_path, window_days=30
    )
    assert out is not None
    assert out["initiations_bullish"] == 0
    assert out["initiations_bearish"] == 0
    assert out["net_actions"] == pytest.approx(0.0)


def test_short_action_codes_are_classified(tmp_path: Path) -> None:
    """Modern yfinance emits short codes ("up", "down", "init", "main"); the
    classifier must handle those alongside the verbose forms."""
    actions = [
        _action(_days_ago(1), "up", "Buy", "Hold", "F1"),
        _action(_days_ago(2), "down", "Hold", "Buy", "F2"),
        _action(_days_ago(3), "init", "Buy", None, "F3"),
        _action(_days_ago(4), "main", "Buy", "Buy", "F4"),
        _action(_days_ago(5), "reit", "Sell", "Sell", "F5"),
    ]
    _seed_cache(tmp_path, "SHORT", actions)

    out = analyst_breadth.compute_analyst_breadth(
        "SHORT", cache_dir=tmp_path, window_days=30
    )
    assert out is not None
    assert out["upgrades"] == 1
    assert out["downgrades"] == 1
    assert out["initiations_bullish"] == 1
    assert out["reiterations_bullish"] == 1
    assert out["reiterations_bearish"] == 1


# ---------------------------------------------------------------------------
# Window comparison
# ---------------------------------------------------------------------------


def test_window_90_includes_more_than_window_30(tmp_path: Path) -> None:
    actions = [
        _action(_days_ago(5), "Upgrades", "Buy", "Hold", "Recent"),
        _action(_days_ago(60), "Upgrades", "Buy", "Hold", "Older"),  # outside 30d, inside 90d
        _action(_days_ago(120), "Upgrades", "Buy", "Hold", "Stale"),  # outside 90d too
    ]
    _seed_cache(tmp_path, "MSFT", actions)

    out_30 = analyst_breadth.compute_analyst_breadth(
        "MSFT", cache_dir=tmp_path, window_days=30
    )
    out_90 = analyst_breadth.compute_analyst_breadth(
        "MSFT", cache_dir=tmp_path, window_days=90
    )
    assert out_30 is not None and out_90 is not None
    assert out_30["upgrades"] == 1
    assert out_90["upgrades"] == 2
    assert out_30["window_days"] == 30
    assert out_90["window_days"] == 90


# ---------------------------------------------------------------------------
# Batch helper
# ---------------------------------------------------------------------------


def test_compute_universe_breadth_skips_uncached(tmp_path: Path) -> None:
    _seed_cache(
        tmp_path,
        "NVDA",
        [_action(_days_ago(1), "Upgrades", "Buy", "Hold", "GS")],
    )
    _seed_cache(tmp_path, "ABBV", [])  # cached-but-empty

    out = analyst_breadth.compute_universe_breadth(
        ["NVDA", "ABBV", "NOCACHE"], cache_dir=tmp_path, window_days=30
    )
    # NOCACHE has no file on disk so it's absent.
    assert "NVDA" in out
    assert "ABBV" in out
    assert "NOCACHE" not in out
    assert out["NVDA"]["upgrades"] == 1
    assert out["ABBV"]["upgrades"] == 0


def test_compute_universe_breadth_empty_input() -> None:
    assert analyst_breadth.compute_universe_breadth([]) == {}


# ---------------------------------------------------------------------------
# Cache-read-only guarantee
# ---------------------------------------------------------------------------


def test_no_yfinance_call_made(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """If yfinance is even *imported and called*, blow up. The module under
    test must be 100% cache-read-only."""

    def _boom(*args, **kwargs):  # pragma: no cover - safety net
        raise RuntimeError("analyst_breadth must not call yfinance")

    # If yfinance is available, monkeypatch its Ticker constructor. If not,
    # the test is still meaningful (importing the module didn't crash).
    try:
        import yfinance as yf

        monkeypatch.setattr(yf, "Ticker", _boom)
    except ImportError:  # pragma: no cover
        pass

    _seed_cache(
        tmp_path,
        "AAPL",
        [_action(_days_ago(1), "Upgrades", "Buy", "Hold", "GS")],
    )
    out = analyst_breadth.compute_analyst_breadth(
        "AAPL", cache_dir=tmp_path, window_days=30
    )
    assert out is not None
    assert out["upgrades"] == 1


def test_ticker_is_uppercased(tmp_path: Path) -> None:
    _seed_cache(
        tmp_path,
        "NVDA",
        [_action(_days_ago(1), "Upgrades", "Buy", "Hold", "GS")],
    )
    out = analyst_breadth.compute_analyst_breadth(
        "nvda", cache_dir=tmp_path, window_days=30
    )
    assert out is not None
    assert out["ticker"] == "NVDA"


def test_raw_actions_carry_classification(tmp_path: Path) -> None:
    actions = [
        _action(_days_ago(1), "Upgrades", "Buy", "Hold", "GS"),
        _action(_days_ago(2), "Initiates", "Buy", None, "MS"),
    ]
    _seed_cache(tmp_path, "ABC", actions)

    out = analyst_breadth.compute_analyst_breadth(
        "ABC", cache_dir=tmp_path, window_days=30
    )
    assert out is not None
    by_firm = {r["firm"]: r for r in out["raw_actions"]}
    assert by_firm["GS"]["classification"] == "upgrade"
    assert by_firm["MS"]["classification"] == "init_bull"


def test_malformed_rows_are_skipped(tmp_path: Path) -> None:
    """Rows missing a date or shape we don't understand should be skipped
    quietly, not crash the aggregator."""
    actions: List[Dict[str, Any]] = [
        {"ticker": "ZZZZ"},  # no date
        {"ticker": "ZZZZ", "date": "not-a-date"},
        {
            "ticker": "ZZZZ",
            "date": _days_ago(1),
            "firm": "Real",
            "action": "Upgrades",
            "from_grade": "Hold",
            "to_grade": "Buy",
        },
        "this is a string row, not a dict",  # type: ignore[list-item]
    ]
    _seed_cache(tmp_path, "ZZZZ", actions)
    out = analyst_breadth.compute_analyst_breadth(
        "ZZZZ", cache_dir=tmp_path, window_days=30
    )
    assert out is not None
    assert out["upgrades"] == 1
    assert len(out["raw_actions"]) == 1


def test_fixture_file_loaded_from_disk(tmp_path: Path) -> None:
    """Confirm the fixture-loader / substitution path works end-to-end."""
    actions = _load_fixture("rich_history.json")
    assert len(actions) > 0
    # All dates must be ISO YYYY-MM-DD after substitution.
    for row in actions:
        d = row.get("date")
        assert isinstance(d, str) and len(d) == 10
        _dt.date.fromisoformat(d)


# ---------------------------------------------------------------------------
# Local helpers
# ---------------------------------------------------------------------------


def _action(
    date_iso: str,
    action: str,
    to_grade: str | None,
    from_grade: str | None,
    firm: str,
) -> Dict[str, Any]:
    return {
        "ticker": "TEST",
        "date": date_iso,
        "firm": firm,
        "action": action,
        "from_grade": from_grade,
        "to_grade": to_grade,
        "direction": 0.0,
    }
