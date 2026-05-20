"""Tests for the LTHCS position-sizing helper.

Covers the algorithm contract documented in
``scripts/lthcs_position_sizing.py``:

* Band weight ordering — higher bands get more $ for equal composite.
* Per-cap enforcement — no single position exceeds the risk-profile cap.
* Review-skip — Review band rows are excluded and the spec note matches.
* Single-ticker edge case — one allocatable ticker fills up to the cap.
* All-Review edge case — full bankroll stays in cash.

The helper is pure-Python with zero external dependencies; no fixtures or
mocking infrastructure is needed.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


# Import the script module by path so the test works even though
# ``scripts/`` is not a real package (no __init__.py).
_SCRIPT_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "lthcs_position_sizing.py"
)
_spec = importlib.util.spec_from_file_location(
    "lthcs_position_sizing", _SCRIPT_PATH
)
assert _spec and _spec.loader, "could not load lthcs_position_sizing"
_mod = importlib.util.module_from_spec(_spec)
sys.modules["lthcs_position_sizing"] = _mod
_spec.loader.exec_module(_mod)

TickerInput = _mod.TickerInput
suggest_allocations = _mod.suggest_allocations
band_multiplier = _mod.band_multiplier
per_position_cap = _mod.per_position_cap


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_band_multiplier_ordering():
    """Higher-confidence bands must yield strictly larger multipliers."""
    assert band_multiplier("elite") > band_multiplier("high")
    assert band_multiplier("high") > band_multiplier("constructive")
    assert band_multiplier("constructive") > band_multiplier("monitor")
    assert band_multiplier("monitor") > band_multiplier("weakening")
    assert band_multiplier("weakening") > band_multiplier("review")
    assert band_multiplier("review") == 0.0


def test_band_multiplier_normalisation():
    """Band lookup should be case-insensitive and tolerate aliases/spaces."""
    assert band_multiplier("Elite") == 1.5
    assert band_multiplier("HIGH_CONFIDENCE") == 1.2
    assert band_multiplier("High Confidence") == 1.2
    assert band_multiplier("high-confidence") == 1.2
    # Unknown band falls through to 0 (skipped).
    assert band_multiplier("mystery") == 0.0


def test_per_position_cap_values():
    assert per_position_cap("conservative") == 0.05
    assert per_position_cap("balanced") == 0.08
    assert per_position_cap("aggressive") == 0.12
    with pytest.raises(ValueError):
        per_position_cap("yolo")


# ---------------------------------------------------------------------------
# Algorithm — band ordering preserved in allocations
# ---------------------------------------------------------------------------


def test_band_weight_ordering_in_allocations():
    """Equal composites should still rank by band multiplier.

    Use a 20-ticker universe (one Elite, one Monitor, eighteen Constructive
    fillers) at equal composite 60, with the Aggressive cap (12%). The
    Elite share normalises to ~1.5 / (1.5 + 0.7 + 18*1.0) ≈ 7.4% — under
    the 12% cap — so the unclipped Elite vs. Monitor ratio is preserved.
    """
    fillers = [TickerInput(f"F{i}", 60, "constructive") for i in range(18)]
    out = suggest_allocations(
        [TickerInput("ALPHA", 60, "elite"), TickerInput("BETA", 60, "monitor")]
        + fillers,
        total_dollars=100_000,
        risk_profile="aggressive",  # 12% cap — Elite share lands under it
    )
    rows = {r["ticker"]: r for r in out["rows"]}
    assert rows["ALPHA"]["dollars"] > rows["BETA"]["dollars"]
    # The ratio should track the multiplier ratio (1.5 / 0.7).
    ratio = rows["ALPHA"]["dollars"] / rows["BETA"]["dollars"]
    assert ratio == pytest.approx(1.5 / 0.7, rel=0.02)
    # And nothing should have been clipped.
    assert "Clipped" not in rows["ALPHA"]["note"]


def test_per_cap_enforcement():
    """No single position may exceed the per-position cap.

    Construct a universe where one ticker would naturally absorb 50%+
    (single high-composite Elite with weaker peers). The Balanced cap (8%)
    must clip it.
    """
    out = suggest_allocations(
        [
            TickerInput("BIG", 95, "elite"),
            TickerInput("MID1", 30, "weakening"),
            TickerInput("MID2", 30, "weakening"),
            TickerInput("MID3", 30, "weakening"),
            TickerInput("MID4", 30, "weakening"),
            TickerInput("MID5", 30, "weakening"),
            TickerInput("MID6", 30, "weakening"),
            TickerInput("MID7", 30, "weakening"),
            TickerInput("MID8", 30, "weakening"),
            TickerInput("MID9", 30, "weakening"),
            TickerInput("MID10", 30, "weakening"),
            TickerInput("MID11", 30, "weakening"),
            TickerInput("MID12", 30, "weakening"),
            TickerInput("MID13", 30, "weakening"),
            TickerInput("MID14", 30, "weakening"),
        ],
        total_dollars=100_000,
        risk_profile="balanced",
    )
    cap_dollars = 100_000 * 0.08
    for r in out["rows"]:
        assert r["dollars"] <= cap_dollars + 0.5  # 50¢ rounding tolerance
    # BIG should have been clipped — its note must call that out.
    big = next(r for r in out["rows"] if r["ticker"] == "BIG")
    assert "Clipped" in big["note"], big


def test_review_band_skipped_with_note():
    """Review-band rows must be flagged and excluded from $ allocation."""
    out = suggest_allocations(
        [
            TickerInput("GOOD", 70, "high"),
            TickerInput("BAD", 20, "review"),
        ],
        total_dollars=10_000,
        risk_profile="balanced",
    )
    rows = {r["ticker"]: r for r in out["rows"]}
    assert rows["BAD"]["skipped"] is True
    assert rows["BAD"]["dollars"] == 0
    assert rows["BAD"]["note"] == "Excluded per Review band"
    # GOOD picks up the full eligible allocation (subject to cap).
    assert rows["GOOD"]["dollars"] > 0


def test_single_ticker_fills_up_to_cap():
    """One ticker + Balanced cap (8%) → that one ticker gets 8% of bankroll."""
    out = suggest_allocations(
        [TickerInput("SOLO", 75, "high")],
        total_dollars=100_000,
        risk_profile="balanced",
    )
    solo = out["rows"][0]
    # With one ticker, normalised weight = 1.0 → clipped to cap = 8%.
    assert solo["dollars"] == pytest.approx(8_000, abs=1)
    # The remaining 92% must be flagged as cash.
    assert out["cash_remaining"] == pytest.approx(92_000, abs=1)
    assert out["total_allocated"] == pytest.approx(8_000, abs=1)


def test_all_review_returns_full_cash():
    """If every ticker is Review-band, the entire bankroll stays in cash."""
    out = suggest_allocations(
        [
            TickerInput("R1", 20, "review"),
            TickerInput("R2", 25, "review"),
            TickerInput("R3", 30, "review"),
        ],
        total_dollars=50_000,
        risk_profile="conservative",
    )
    assert out["total_allocated"] == 0.0
    assert out["cash_remaining"] == pytest.approx(50_000)
    for r in out["rows"]:
        assert r["skipped"] is True
        assert r["dollars"] == 0
        assert r["note"] == "Excluded per Review band"


# ---------------------------------------------------------------------------
# Misc input validation
# ---------------------------------------------------------------------------


def test_invalid_total_dollars_raises():
    with pytest.raises(ValueError):
        suggest_allocations(
            [TickerInput("A", 50, "high")],
            total_dollars=0,
            risk_profile="balanced",
        )


def test_data_quality_flag_surfaces_as_note():
    """A row with data_quality_flags should carry a low-confidence note.

    The row is still allocated — the spec says flag inline but don't auto-
    exclude. We pick a non-clipping universe so the note isn't pre-empted
    by the "Clipped to X% cap" message.
    """
    out = suggest_allocations(
        [
            TickerInput("FLAGGED", 50, "constructive", ("missing_thesis",)),
            TickerInput("OTHER1", 50, "constructive"),
            TickerInput("OTHER2", 50, "constructive"),
            TickerInput("OTHER3", 50, "constructive"),
            TickerInput("OTHER4", 50, "constructive"),
            TickerInput("OTHER5", 50, "constructive"),
            TickerInput("OTHER6", 50, "constructive"),
            TickerInput("OTHER7", 50, "constructive"),
            TickerInput("OTHER8", 50, "constructive"),
            TickerInput("OTHER9", 50, "constructive"),
            TickerInput("OTHER10", 50, "constructive"),
            TickerInput("OTHER11", 50, "constructive"),
            TickerInput("OTHER12", 50, "constructive"),
            TickerInput("OTHER13", 50, "constructive"),
        ],
        total_dollars=100_000,
        risk_profile="aggressive",
    )
    flagged = next(r for r in out["rows"] if r["ticker"] == "FLAGGED")
    assert flagged["dollars"] > 0
    # Either the cap-clip note OR the low-confidence note is fine; the
    # important contract is the user sees *something* about the flag if no
    # cap-clip occurred. With 14 tickers at equal composite and an
    # aggressive (12%) cap, no clipping happens — so the note must be the
    # low-confidence one.
    assert "Low confidence" in flagged["note"]


def test_dollar_totals_balance():
    """total_allocated + cash_remaining should equal total_dollars."""
    out = suggest_allocations(
        [
            TickerInput("A", 80, "elite"),
            TickerInput("B", 70, "high"),
            TickerInput("C", 60, "constructive"),
            TickerInput("D", 50, "monitor"),
            TickerInput("E", 30, "weakening"),
        ],
        total_dollars=100_000,
        risk_profile="balanced",
    )
    summed = out["total_allocated"] + out["cash_remaining"]
    # Allow $1 of rounding slack across 5 rows.
    assert summed == pytest.approx(100_000, abs=1)
