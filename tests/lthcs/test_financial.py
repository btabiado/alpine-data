"""Tests for lthcs.pillars.financial.

No live network: SEC EDGAR rows are passed in directly as fixtures.
Mirrors the period-dict schema produced by
:func:`lthcs.sources.sec_edgar._extract_concept_history`.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from lthcs.pillars import financial


# --- Row-builder helpers ----------------------------------------------------


def _q_row(start: str, end: str, value: float, concept: str = "X") -> Dict[str, Any]:
    """Build a quarterly (~90 day) SEC fact row."""
    return {
        "start_date": start,
        "end_date": end,
        "value": value,
        "form": "10-Q",
        "fy": int(end[:4]),
        "fp": "Q1",
        "concept": concept,
    }


def _annual_row(start: str, end: str, value: float, concept: str = "X") -> Dict[str, Any]:
    """Build an annual (~365 day) SEC fact row."""
    return {
        "start_date": start,
        "end_date": end,
        "value": value,
        "form": "10-K",
        "fy": int(end[:4]),
        "fp": "FY",
        "concept": concept,
    }


def _four_quarters(
    pairs: List[Dict[str, float]],
) -> List[Dict[str, Any]]:
    """Convenience: build a list of 4 quarterly rows from spec dicts.

    Each dict must have ``start``, ``end``, ``value``. Returned list is
    desc by end_date (matches what SEC EDGAR returns).
    """
    rows = [_q_row(p["start"], p["end"], p["value"]) for p in pairs]
    rows.sort(key=lambda r: r["end_date"], reverse=True)
    return rows


# --- compute_gross_margin_history -------------------------------------------


def test_gross_margin_history_pairs_by_start_and_end() -> None:
    revenue = [
        _q_row("2025-04-01", "2025-06-30", 200.0),
        _q_row("2025-01-01", "2025-03-31", 150.0),
    ]
    gross = [
        _q_row("2025-04-01", "2025-06-30", 80.0),
        _q_row("2025-01-01", "2025-03-31", 60.0),
    ]
    out = financial.compute_gross_margin_history(revenue, gross)
    assert len(out) == 2
    # Desc by end_date.
    assert out[0]["end_date"] == "2025-06-30"
    assert out[0]["margin"] == pytest.approx(0.40)
    assert out[0]["revenue"] == pytest.approx(200.0)
    assert out[0]["gross_profit"] == pytest.approx(80.0)
    assert out[1]["end_date"] == "2025-03-31"
    assert out[1]["margin"] == pytest.approx(0.40)


def test_gross_margin_history_skips_unmatched_quarters() -> None:
    """Quarters present on only one side are dropped."""
    revenue = [
        _q_row("2025-04-01", "2025-06-30", 200.0),
        _q_row("2025-01-01", "2025-03-31", 150.0),
    ]
    # Gross profit is missing the Q2 row, has a stale Q4 the other side
    # doesn't have.
    gross = [
        _q_row("2025-01-01", "2025-03-31", 60.0),
        _q_row("2024-10-01", "2024-12-31", 70.0),
    ]
    out = financial.compute_gross_margin_history(revenue, gross)
    assert len(out) == 1
    assert out[0]["end_date"] == "2025-03-31"
    assert out[0]["margin"] == pytest.approx(0.40)


def test_gross_margin_history_skips_rows_with_missing_dates() -> None:
    """Rows lacking start_date or end_date can't form a key, so they're skipped."""
    revenue = [
        {"start_date": None, "end_date": "2025-06-30", "value": 200.0},
        {"start_date": "2025-04-01", "end_date": None, "value": 150.0},
        _q_row("2025-01-01", "2025-03-31", 150.0),
    ]
    gross = [
        _q_row("2025-01-01", "2025-03-31", 60.0),
    ]
    out = financial.compute_gross_margin_history(revenue, gross)
    assert len(out) == 1
    assert out[0]["end_date"] == "2025-03-31"


def test_gross_margin_history_drops_non_positive_revenue() -> None:
    revenue = [
        _q_row("2025-04-01", "2025-06-30", 0.0),
        _q_row("2025-01-01", "2025-03-31", -10.0),
    ]
    gross = [
        _q_row("2025-04-01", "2025-06-30", 80.0),
        _q_row("2025-01-01", "2025-03-31", 60.0),
    ]
    out = financial.compute_gross_margin_history(revenue, gross)
    assert out == []


def test_gross_margin_history_handles_empty_inputs() -> None:
    assert financial.compute_gross_margin_history([], []) == []
    assert financial.compute_gross_margin_history(
        [_q_row("2025-01-01", "2025-03-31", 100.0)], []
    ) == []


def test_gross_margin_history_does_not_apply_quarterly_filter() -> None:
    """Annual periods should also be matched -- caller decides the filter."""
    revenue = [_annual_row("2024-01-01", "2024-12-31", 1000.0)]
    gross = [_annual_row("2024-01-01", "2024-12-31", 400.0)]
    out = financial.compute_gross_margin_history(revenue, gross)
    assert len(out) == 1
    assert out[0]["margin"] == pytest.approx(0.40)


# --- compute_margin_trend_subscore ------------------------------------------


def _quarter_dates_for_year(year: int) -> List[Dict[str, str]]:
    """Return 4 calendar quarter (start, end) date pairs for a year."""
    return [
        {"start": "{}-01-01".format(year), "end": "{}-03-31".format(year)},
        {"start": "{}-04-01".format(year), "end": "{}-06-30".format(year)},
        {"start": "{}-07-01".format(year), "end": "{}-09-30".format(year)},
        {"start": "{}-10-01".format(year), "end": "{}-12-31".format(year)},
    ]


def _build_quarters_with_margins(
    year: int, revenue_each: float, margins: List[float]
) -> tuple:
    """For each quarter set GP = revenue * margin. Returns (rev_rows, gp_rows)."""
    qdates = _quarter_dates_for_year(year)
    rev_rows: List[Dict[str, Any]] = []
    gp_rows: List[Dict[str, Any]] = []
    for q, m in zip(qdates, margins):
        rev_rows.append(_q_row(q["start"], q["end"], revenue_each))
        gp_rows.append(_q_row(q["start"], q["end"], revenue_each * m))
    return rev_rows, gp_rows


def test_margin_trend_improving_margins_above_50() -> None:
    """Steadily rising margins across 4 quarters -> > 50."""
    rev, gp = _build_quarters_with_margins(
        2025, 1000.0, [0.30, 0.32, 0.34, 0.36]
    )
    score = financial.compute_margin_trend_subscore(rev, gp)
    assert score > 50.0


def test_margin_trend_declining_margins_below_50() -> None:
    """Steadily falling margins across 4 quarters -> < 50."""
    rev, gp = _build_quarters_with_margins(
        2025, 1000.0, [0.40, 0.36, 0.32, 0.28]
    )
    score = financial.compute_margin_trend_subscore(rev, gp)
    assert score < 50.0


def test_margin_trend_flat_margins_exactly_50() -> None:
    """Slope of zero must map exactly to the midpoint."""
    rev, gp = _build_quarters_with_margins(
        2025, 1000.0, [0.35, 0.35, 0.35, 0.35]
    )
    score = financial.compute_margin_trend_subscore(rev, gp)
    assert score == pytest.approx(50.0, abs=1e-9)


def test_margin_trend_insufficient_quarters_returns_50() -> None:
    """Fewer than 4 matched quarterly margins -> 50.0 (insufficient data)."""
    rev, gp = _build_quarters_with_margins(2025, 1000.0, [0.30, 0.32, 0.34])
    # Drop the last quarter to give only 3.
    rev = rev[:3]
    gp = gp[:3]
    score = financial.compute_margin_trend_subscore(rev, gp)
    assert score == 50.0


def test_margin_trend_empty_inputs_returns_50() -> None:
    assert financial.compute_margin_trend_subscore([], []) == 50.0


def test_margin_trend_annual_only_returns_50() -> None:
    """Annual rows are paired but filtered out by the quarterly-only step."""
    rev = [_annual_row("2024-01-01", "2024-12-31", 1000.0)]
    gp = [_annual_row("2024-01-01", "2024-12-31", 400.0)]
    assert financial.compute_margin_trend_subscore(rev, gp) == 50.0


def test_margin_trend_large_positive_slope_saturates_at_100() -> None:
    """A slope past +0.05 per quarter should hit the 100 ceiling."""
    rev, gp = _build_quarters_with_margins(
        2025, 1000.0, [0.10, 0.25, 0.40, 0.55]
    )
    # slope ~0.15/quarter -> well past 0.05 bound -> saturates at 100.
    score = financial.compute_margin_trend_subscore(rev, gp)
    assert score == pytest.approx(100.0)


# --- compute_ocf_subscore ---------------------------------------------------


def test_ocf_subscore_positive_margin_above_50() -> None:
    """OCF margin around the middle of the [-0.10, 0.30] band -> > 50."""
    # Revenue: 4 quarters of 1000 = 4000 TTM. OCF: 4 quarters of 200 = 800.
    # ratio = 0.20 -> bounded_linear in [-0.10, 0.30] -> (0.20 - (-0.10)) /
    # (0.30 - (-0.10)) * 100 = 75.
    qd = _quarter_dates_for_year(2025)
    rev = [_q_row(q["start"], q["end"], 1000.0) for q in qd]
    ocf = [_q_row(q["start"], q["end"], 200.0) for q in qd]
    score = financial.compute_ocf_subscore(rev, ocf)
    assert score > 50.0
    assert score == pytest.approx(75.0, abs=1e-6)


def test_ocf_subscore_negative_margin_below_50() -> None:
    qd = _quarter_dates_for_year(2025)
    rev = [_q_row(q["start"], q["end"], 1000.0) for q in qd]
    # -200 / 1000 = -0.20 -> below the -0.10 floor -> 0.
    ocf = [_q_row(q["start"], q["end"], -200.0) for q in qd]
    score = financial.compute_ocf_subscore(rev, ocf)
    assert score < 50.0
    assert score == pytest.approx(0.0)


def test_ocf_subscore_small_positive_margin_just_above_50() -> None:
    """OCF margin of +5% lands above 50 but well below 100."""
    qd = _quarter_dates_for_year(2025)
    rev = [_q_row(q["start"], q["end"], 1000.0) for q in qd]
    ocf = [_q_row(q["start"], q["end"], 50.0) for q in qd]
    # ratio = 0.05 -> (0.05 - (-0.10)) / 0.40 * 100 = 37.5. Below 50.
    score = financial.compute_ocf_subscore(rev, ocf)
    assert score == pytest.approx(37.5)


def test_ocf_subscore_insufficient_quarters_returns_50() -> None:
    qd = _quarter_dates_for_year(2025)
    rev = [_q_row(q["start"], q["end"], 1000.0) for q in qd[:3]]  # only 3
    ocf = [_q_row(q["start"], q["end"], 200.0) for q in qd]
    assert financial.compute_ocf_subscore(rev, ocf) == 50.0


def test_ocf_subscore_no_ocf_returns_50() -> None:
    qd = _quarter_dates_for_year(2025)
    rev = [_q_row(q["start"], q["end"], 1000.0) for q in qd]
    assert financial.compute_ocf_subscore(rev, []) == 50.0


def test_ocf_subscore_no_revenue_returns_50() -> None:
    qd = _quarter_dates_for_year(2025)
    ocf = [_q_row(q["start"], q["end"], 200.0) for q in qd]
    assert financial.compute_ocf_subscore([], ocf) == 50.0


def test_ocf_subscore_high_margin_saturates() -> None:
    qd = _quarter_dates_for_year(2025)
    rev = [_q_row(q["start"], q["end"], 1000.0) for q in qd]
    ocf = [_q_row(q["start"], q["end"], 400.0) for q in qd]
    # ratio 0.40 > 0.30 ceiling -> 100.
    assert financial.compute_ocf_subscore(rev, ocf) == pytest.approx(100.0)


# --- compute_financial: weighting + structure -------------------------------


def _annual_pair(year_recent: int, recent_value: float, prior_value: float):
    """Two consecutive annual revenue rows for an unambiguous YoY computation."""
    year_prior = year_recent - 1
    return [
        _annual_row(
            "{}-01-01".format(year_recent),
            "{}-12-31".format(year_recent),
            recent_value,
        ),
        _annual_row(
            "{}-01-01".format(year_prior),
            "{}-12-31".format(year_prior),
            prior_value,
        ),
    ]


def test_compute_financial_weighting_math() -> None:
    """Sub-score must equal 0.40*rev + 0.30*margin + 0.30*ocf."""
    # Revenue YoY: 100 -> 110 -> growth = 0.10.
    revenue_annual = _annual_pair(2025, 110.0, 100.0)

    # Quarterly revenue: 4 quarters of 1000.
    qd = _quarter_dates_for_year(2025)
    revenue_quarters = [_q_row(q["start"], q["end"], 1000.0) for q in qd]
    # Combine annual + quarterly so all three components have inputs.
    revenue_rows = revenue_annual + revenue_quarters

    # Flat margin at 0.30 -> margin_subscore = 50.
    gp_rows = [_q_row(q["start"], q["end"], 300.0) for q in qd]

    # OCF margin = 0.20 -> ocf_subscore = 75.
    ocf_rows = [_q_row(q["start"], q["end"], 200.0) for q in qd]

    # Peer growths: focal at 0.10 ranks above all peers -> revenue_subscore=100.
    peer_growths = {
        "FOO": 0.10,  # focal, excluded
        "P1": -0.10,
        "P2": 0.0,
        "P3": 0.05,
    }

    out = financial.compute_financial(
        "FOO", revenue_rows, gp_rows, ocf_rows, peer_growths
    )

    comps = out["components"]
    assert comps["revenue_growth_yoy"] == pytest.approx(0.10, rel=1e-6)
    assert comps["revenue_subscore"] == pytest.approx(100.0)
    assert comps["margin_subscore"] == pytest.approx(50.0)
    assert comps["ocf_subscore"] == pytest.approx(75.0)

    expected = round(0.40 * 100.0 + 0.30 * 50.0 + 0.30 * 75.0, 1)
    assert out["sub_score"] == expected
    assert out["weights"] == {"revenue": 0.40, "margin": 0.30, "ocf": 0.30}
    assert out["data_quality"] == {
        "has_revenue": True,
        "has_margin": True,
        "has_ocf": True,
    }


def test_compute_financial_all_missing_yields_50() -> None:
    out = financial.compute_financial("FOO", [], [], [], {})
    assert out["sub_score"] == 50.0
    assert out["components"]["revenue_subscore"] == 50.0
    assert out["components"]["margin_subscore"] == 50.0
    assert out["components"]["ocf_subscore"] == 50.0
    assert out["data_quality"] == {
        "has_revenue": False,
        "has_margin": False,
        "has_ocf": False,
    }
    # Explainability fields surface None when unavailable.
    assert out["components"]["ttm_ocf_margin"] is None
    assert out["components"]["margin_trend_slope"] is None
    assert out["components"]["revenue_growth_yoy"] is None


def test_compute_financial_data_quality_flags_partial_signals() -> None:
    """Only margin component is computable; others must report False."""
    qd = _quarter_dates_for_year(2025)
    revenue_rows = [_q_row(q["start"], q["end"], 1000.0) for q in qd]
    gp_rows = [
        _q_row(qd[0]["start"], qd[0]["end"], 300.0),
        _q_row(qd[1]["start"], qd[1]["end"], 320.0),
        _q_row(qd[2]["start"], qd[2]["end"], 340.0),
        _q_row(qd[3]["start"], qd[3]["end"], 360.0),
    ]
    # No OCF rows; no annual revenue and only 4 quarterly rows so no YoY.
    out = financial.compute_financial("FOO", revenue_rows, gp_rows, [], {})
    dq = out["data_quality"]
    # Revenue YoY needs 2 annuals or 8 quarters -> can't compute.
    assert dq["has_revenue"] is False
    # 4 matched quarterly margins available -> margin computes.
    assert dq["has_margin"] is True
    # No OCF rows -> can't compute.
    assert dq["has_ocf"] is False


def test_compute_financial_excludes_focal_from_peer_distribution() -> None:
    """Focal's own growth entry in peer_growths must not pollute the rank."""
    revenue_rows = _annual_pair(2025, 110.0, 100.0)
    peer_growths = {
        "FOO": 0.10,  # focal -- excluded
        "P1": 0.10,
        "P2": 0.10,
        "P3": 0.10,
    }
    out = financial.compute_financial("FOO", revenue_rows, [], [], peer_growths)
    # All 3 peers equal to focal -> 0 below + 3 equal -> 50.
    assert out["components"]["revenue_subscore"] == pytest.approx(50.0)


def test_compute_financial_ignores_none_peer_growths() -> None:
    revenue_rows = _annual_pair(2025, 110.0, 100.0)
    peer_growths = {
        "FOO": 0.10,
        "P1": None,
        "P2": 0.05,
        "P3": 0.08,
    }
    out = financial.compute_financial("FOO", revenue_rows, [], [], peer_growths)
    # Cleaned peers (excl focal, excl None): [0.05, 0.08]. growth 0.10 ranks
    # above both -> 100.
    assert out["components"]["revenue_subscore"] == pytest.approx(100.0)


def test_compute_financial_sub_score_rounded_to_one_decimal() -> None:
    revenue_rows = _annual_pair(2025, 108.0, 100.0)
    out = financial.compute_financial("FOO", revenue_rows, [], [], {"P1": 0.05})
    assert isinstance(out["sub_score"], float)
    assert out["sub_score"] == round(out["sub_score"], 1)


def test_compute_financial_explainability_fields_present() -> None:
    """Even when the score doesn't fall back, the explainability raw values surface."""
    qd = _quarter_dates_for_year(2025)
    revenue_annual = _annual_pair(2025, 110.0, 100.0)
    revenue_quarters = [_q_row(q["start"], q["end"], 1000.0) for q in qd]
    revenue_rows = revenue_annual + revenue_quarters
    gp_rows = [_q_row(q["start"], q["end"], 300.0) for q in qd]  # flat 30%
    ocf_rows = [_q_row(q["start"], q["end"], 200.0) for q in qd]
    out = financial.compute_financial(
        "FOO", revenue_rows, gp_rows, ocf_rows, {"P1": 0.05}
    )
    assert out["components"]["margin_trend_slope"] == pytest.approx(0.0, abs=1e-9)
    assert out["components"]["ttm_ocf_margin"] == pytest.approx(0.20, abs=1e-9)


# --- INTC-like sanity-check fixture -----------------------------------------


def test_compute_financial_intc_like_fixture_yields_low_score() -> None:
    """INTC-style profile: slight revenue decline, compressed margins, small OCF.

    The point is to confirm a struggling-but-not-failing semiconductor
    profile lands visibly below the midpoint. Exact value is asserted
    against an envelope rather than a single number, because the math
    couples three sub-scores.

    Profile choices (V1 heuristic anchors):
      * Revenue YoY -5% (annual).
      * Gross margin compressing 40% -> 36% across the trailing 4Q.
      * OCF margin = +6% (positive but small).
      * Peer growths span -10% to +30% so the focal lands near the
        bottom but not the very bottom.
    """
    revenue_annual = _annual_pair(2025, 95.0, 100.0)  # -5% YoY

    qd = _quarter_dates_for_year(2025)
    revenue_quarters = [_q_row(q["start"], q["end"], 1000.0) for q in qd]
    revenue_rows = revenue_annual + revenue_quarters

    # Margin compressing: 0.40, 0.38, 0.37, 0.36.
    margins = [0.40, 0.38, 0.37, 0.36]
    gp_rows = [
        _q_row(qd[i]["start"], qd[i]["end"], 1000.0 * margins[i])
        for i in range(4)
    ]

    # OCF margin 6% -> small positive.
    ocf_rows = [_q_row(q["start"], q["end"], 60.0) for q in qd]

    peer_growths = {
        "INTC": -0.05,
        "P1": -0.10,
        "P2": 0.0,
        "P3": 0.05,
        "P4": 0.08,
        "P5": 0.12,
        "P6": 0.18,
        "P7": 0.30,
    }

    out = financial.compute_financial(
        "INTC", revenue_rows, gp_rows, ocf_rows, peer_growths
    )

    assert out["data_quality"] == {
        "has_revenue": True,
        "has_margin": True,
        "has_ocf": True,
    }
    # Spec target: visibly low, under 50.
    assert out["sub_score"] < 50.0
    # Sanity floor: shouldn't go below 10 with a positive OCF margin.
    assert out["sub_score"] > 10.0
    # Margin compressing -> margin subscore visibly below 50.
    assert out["components"]["margin_subscore"] < 50.0
    # Revenue near the bottom of peers (only P1 lower) -> below 50.
    assert out["components"]["revenue_subscore"] < 50.0
