"""Tests for ``scripts.lthcs_diff_snapshots``.

The diff helper is pure (no I/O), so fixtures are inline dicts. Each test
isolates one of the contracts the front-end depends on:

    - identical snapshots → all deltas 0, KPI counts zero except unchanged
    - missing ticker in B → marked inactive, doesn't count as "down"
    - new ticker in B → marked new, doesn't count as "up"
    - band promotion / demotion detection respects the canonical band order
    - KPI counts (up, down, unchanged) line up with the per-ticker rows
"""

from __future__ import annotations

import pytest

from scripts.lthcs_diff_snapshots import (
    BAND_ORDER,
    PILLAR_KEYS,
    diff_snapshots,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _row(
    ticker: str,
    *,
    score: float,
    band: str,
    pillars: dict[str, float] | None = None,
    sector: str = "Technology",
) -> dict:
    """Build a minimal score row matching the on-disk snapshot shape."""
    if pillars is None:
        pillars = {key: 50.0 for key in PILLAR_KEYS}
    return {
        "ticker": ticker,
        "lthcs_score": score,
        "band": band,
        "subscores": pillars,
        "sector": sector,
    }


def _snap(date: str, rows: list[dict]) -> dict:
    return {
        "calc_date": date,
        "model_version": "v1.1.0",
        "scores": rows,
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_identical_snapshots_have_zero_deltas() -> None:
    """When A == B every ticker reports delta=0 and band_change=same."""
    rows = [
        _row("AAPL", score=72.0, band="constructive"),
        _row("MSFT", score=88.0, band="elite"),
        _row("XOM", score=45.0, band="review"),
    ]
    snap_a = _snap("2026-05-17", rows)
    snap_b = _snap("2026-05-18", [dict(r) for r in rows])

    result = diff_snapshots(snap_a, snap_b)

    assert result["date_a"] == "2026-05-17"
    assert result["date_b"] == "2026-05-18"
    assert len(result["tickers"]) == 3
    for row in result["tickers"]:
        assert row["delta"] == 0.0
        assert row["band_change"] == "same"
        # Every pillar delta should be exactly 0.0 (not None).
        for key in PILLAR_KEYS:
            assert row["pillar_deltas"][key] == 0.0

    s = result["summary"]
    assert s["tickers_up"] == 0
    assert s["tickers_down"] == 0
    assert s["tickers_unchanged"] == 3
    assert s["avg_composite_shift"] == 0.0
    assert s["total_compared"] == 3
    assert s["tickers_inactive"] == 0
    assert s["tickers_new"] == 0


def test_ticker_missing_from_b_marked_inactive() -> None:
    """A ticker present in A but not B is inactive and carries band_a forward."""
    snap_a = _snap(
        "2026-05-17",
        [
            _row("AAPL", score=72.0, band="constructive"),
            _row("WBA", score=42.0, band="review"),
        ],
    )
    snap_b = _snap(
        "2026-05-18",
        [
            _row("AAPL", score=73.0, band="constructive"),
            # WBA removed
        ],
    )

    result = diff_snapshots(snap_a, snap_b)
    by_ticker = {r["ticker"]: r for r in result["tickers"]}

    wba = by_ticker["WBA"]
    assert wba["band_change"] == "inactive"
    assert wba["band_a"] == "review"
    assert wba["band_b"] is None
    assert wba["score_a"] == 42.0
    assert wba["score_b"] is None
    assert wba["delta"] is None

    # AAPL still tracked normally.
    aapl = by_ticker["AAPL"]
    assert aapl["delta"] == 1.0
    assert aapl["band_change"] == "same"

    s = result["summary"]
    assert s["tickers_inactive"] == 1
    # Inactive ticker must NOT count as "down".
    assert s["tickers_down"] == 0
    assert s["total_compared"] == 1  # only AAPL had both sides


def test_new_ticker_in_b_marked_new() -> None:
    """A ticker present in B but not A is flagged new and excluded from KPI counts."""
    snap_a = _snap("2026-05-17", [_row("AAPL", score=72.0, band="constructive")])
    snap_b = _snap(
        "2026-05-18",
        [
            _row("AAPL", score=72.0, band="constructive"),
            _row("ARM", score=68.0, band="monitor"),
        ],
    )

    result = diff_snapshots(snap_a, snap_b)
    by_ticker = {r["ticker"]: r for r in result["tickers"]}

    arm = by_ticker["ARM"]
    assert arm["band_change"] == "new"
    assert arm["band_a"] is None
    assert arm["band_b"] == "monitor"
    assert arm["score_a"] is None
    assert arm["score_b"] == 68.0
    assert arm["delta"] is None

    s = result["summary"]
    assert s["tickers_new"] == 1
    # New ticker must NOT count as "up".
    assert s["tickers_up"] == 0


def test_band_promotion_detected() -> None:
    """When a ticker moves to a stronger band, band_change is 'promotion'."""
    snap_a = _snap("2026-05-17", [_row("NVDA", score=68.0, band="monitor")])
    snap_b = _snap("2026-05-18", [_row("NVDA", score=72.0, band="constructive")])

    result = diff_snapshots(snap_a, snap_b)
    row = result["tickers"][0]
    assert row["band_change"] == "promotion"
    assert row["band_a"] == "monitor"
    assert row["band_b"] == "constructive"
    assert row["delta"] == pytest.approx(4.0)

    s = result["summary"]
    assert s["tickers_up"] == 1
    assert s["tickers_down"] == 0


def test_band_demotion_detected() -> None:
    """When a ticker moves to a weaker band, band_change is 'demotion'."""
    snap_a = _snap("2026-05-17", [_row("META", score=82.0, band="high_confidence")])
    snap_b = _snap("2026-05-18", [_row("META", score=72.0, band="constructive")])

    result = diff_snapshots(snap_a, snap_b)
    row = result["tickers"][0]
    assert row["band_change"] == "demotion"
    assert row["delta"] == pytest.approx(-10.0)

    s = result["summary"]
    assert s["tickers_up"] == 0
    assert s["tickers_down"] == 1


def test_band_order_canonical() -> None:
    """The BAND_ORDER constant matches the canonical strongest-first ordering.

    Tests below depend on this ordering — pin it so accidental reorder
    breaks loudly.
    """
    assert BAND_ORDER == [
        "elite",
        "high_confidence",
        "constructive",
        "monitor",
        "weakening",
        "review",
    ]


def test_kpi_counts_mixed_population() -> None:
    """End-to-end KPI smoke test: 1 promotion, 1 demotion, 1 flat, 1 inactive, 1 new."""
    snap_a = _snap(
        "2026-05-17",
        [
            _row("PROMO", score=68.0, band="monitor"),
            _row("DEMOTE", score=82.0, band="high_confidence"),
            _row("FLAT", score=55.0, band="weakening"),
            _row("GONE", score=40.0, band="review"),
        ],
    )
    snap_b = _snap(
        "2026-05-18",
        [
            _row("PROMO", score=72.0, band="constructive"),
            _row("DEMOTE", score=72.0, band="constructive"),
            _row("FLAT", score=55.0, band="weakening"),
            _row("NEW", score=60.0, band="monitor"),
        ],
    )

    result = diff_snapshots(snap_a, snap_b)
    s = result["summary"]

    assert s["tickers_up"] == 1
    assert s["tickers_down"] == 1
    assert s["tickers_unchanged"] == 1
    assert s["tickers_inactive"] == 1
    assert s["tickers_new"] == 1
    # Only PROMO, DEMOTE, FLAT had both sides → 3 compared.
    assert s["total_compared"] == 3
    # Average shift across compared = (+4 + -10 + 0) / 3 = -2.0
    assert s["avg_composite_shift"] == pytest.approx(-2.0)


def test_pillar_deltas_computed_per_ticker() -> None:
    """Per-pillar deltas mirror b - a per subscore key."""
    pillars_a = {
        "adoption_momentum": 30.0,
        "institutional_confidence": 50.0,
        "financial_evolution": 60.0,
        "thesis_integrity": 70.0,
        "des": 40.0,
    }
    pillars_b = {
        "adoption_momentum": 35.0,
        "institutional_confidence": 45.0,
        "financial_evolution": 60.0,
        "thesis_integrity": 72.5,
        "des": 30.0,
    }
    snap_a = _snap("2026-05-17", [_row("AAPL", score=50.0, band="weakening", pillars=pillars_a)])
    snap_b = _snap("2026-05-18", [_row("AAPL", score=49.0, band="weakening", pillars=pillars_b)])

    result = diff_snapshots(snap_a, snap_b)
    pd = result["tickers"][0]["pillar_deltas"]
    assert pd["adoption_momentum"] == pytest.approx(5.0)
    assert pd["institutional_confidence"] == pytest.approx(-5.0)
    assert pd["financial_evolution"] == pytest.approx(0.0)
    assert pd["thesis_integrity"] == pytest.approx(2.5)
    assert pd["des"] == pytest.approx(-10.0)


def test_same_date_handled_gracefully() -> None:
    """Diffing the same snapshot against itself should not error."""
    rows = [_row("AAPL", score=72.0, band="constructive")]
    snap = _snap("2026-05-18", rows)
    result = diff_snapshots(snap, snap)
    assert result["summary"]["total_compared"] == 1
    assert result["summary"]["avg_composite_shift"] == 0.0
    assert result["tickers"][0]["delta"] == 0.0
