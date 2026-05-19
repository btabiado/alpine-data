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


# --- Bank code path: routing + sub-components -------------------------------


def _eight_consecutive_quarters(
    start_year: int, values: List[float]
) -> List[Dict[str, Any]]:
    """Build 8 contiguous quarterly rows starting Q1 of ``start_year``.

    ``values`` is length 8 in ascending-period order (oldest first); the
    returned list is the SEC desc-by-end_date convention.
    """
    if len(values) != 8:
        raise ValueError("need exactly 8 quarterly values")
    quarters = []
    qdates = _quarter_dates_for_year(start_year) + _quarter_dates_for_year(start_year + 1)
    for q, v in zip(qdates, values):
        quarters.append(_q_row(q["start"], q["end"], v))
    quarters.sort(key=lambda r: r["end_date"], reverse=True)
    return quarters


def _four_consecutive_quarters(
    year: int, values: List[float]
) -> List[Dict[str, Any]]:
    """Build 4 quarterly rows for the calendar year, oldest-first values."""
    if len(values) != 4:
        raise ValueError("need exactly 4 quarterly values")
    qdates = _quarter_dates_for_year(year)
    rows = [_q_row(qdates[i]["start"], qdates[i]["end"], values[i]) for i in range(4)]
    rows.sort(key=lambda r: r["end_date"], reverse=True)
    return rows


def test_compute_financial_bank_path_strong_growth_diversified() -> None:
    """JPM-like profile: NII growing, low PCL/Rev, high noninterest mix -> elevated score."""
    # 8 quarters of NII: prior-year total = 4000, this-year total = 4800 -> 20% YoY.
    nii = _eight_consecutive_quarters(
        2024, [950, 990, 1020, 1040, 1150, 1190, 1220, 1240]
    )
    # 4 quarters of noninterest income summing to 4800 -> total rev 9600.
    noninterest = _four_consecutive_quarters(2025, [1100, 1180, 1240, 1280])
    # 4 quarters of PCL summing to 960 -> ratio 0.10 (mid-band, score ~80).
    pcl = _four_consecutive_quarters(2025, [200, 230, 250, 280])

    # Peer growths: focal's 20% NII growth is the top of the peer cohort.
    peer_growths = {"JPM": 0.20, "P1": -0.05, "P2": 0.02, "P3": 0.06}

    out = financial.compute_financial(
        "JPM",
        revenue_rows=[],
        gross_profit_rows=[],
        ocf_rows=[],
        peer_growths=peer_growths,
        sector="Financials",
        nii_rows=nii,
        noninterest_rows=noninterest,
        pcl_rows=pcl,
    )

    assert out["sector_path"] == "bank"
    # NII growth ranks above all peers -> 100.
    assert out["components"]["revenue_subscore"] == pytest.approx(100.0)
    # Noninterest ratio = 4800 / 9600 = 0.50 -> mid-band (.20 to .60) -> 75.
    assert out["components"]["ocf_subscore"] == pytest.approx(75.0, abs=1e-6)
    # PCL ratio = 960 / 9600 = 0.10 -> in [.05, .30] inverted -> 80.
    assert out["components"]["margin_subscore"] == pytest.approx(80.0, abs=1e-6)
    # Sub-score should be well above 50 (strong on all three axes).
    assert out["sub_score"] > 75.0
    # Explainability keys for bank path.
    assert out["components"]["nii_growth_yoy"] == pytest.approx(0.20, rel=1e-6)
    assert out["components"]["pcl_to_revenue_ratio"] == pytest.approx(0.10, abs=1e-6)
    assert out["components"]["noninterest_to_revenue_ratio"] == pytest.approx(
        0.50, abs=1e-6
    )
    # Standard explainability fields should be None (bank path doesn't compute them).
    assert out["components"]["ttm_ocf_margin"] is None
    assert out["components"]["margin_trend_slope"] is None


def test_compute_financial_bank_path_high_pcl_ratio_drags_score() -> None:
    """A bank with a crisis-era PCL accrual (rising ratio) should land low."""
    nii = _eight_consecutive_quarters(
        2024, [1000, 1000, 1000, 1000, 1000, 1000, 1000, 1000]
    )
    # Noninterest small -> total rev 4 * 1100 = 4400.
    noninterest = _four_consecutive_quarters(2025, [100, 100, 100, 100])
    # PCL summing to 4 * 350 = 1400; total rev = 5400; ratio ~0.259.
    # bounded_linear(0.259, .05, .30, invert=True) -> 100 - (.259-.05)/.25*100 ~ 16.
    pcl = _four_consecutive_quarters(2025, [350, 350, 350, 350])

    peer_growths = {"JPM": 0.0, "P1": 0.0, "P2": 0.0, "P3": 0.0}

    out = financial.compute_financial(
        "JPM",
        revenue_rows=[],
        gross_profit_rows=[],
        ocf_rows=[],
        peer_growths=peer_growths,
        sector="Financials",
        nii_rows=nii,
        noninterest_rows=noninterest,
        pcl_rows=pcl,
    )

    assert out["sector_path"] == "bank"
    # PCL subscore visibly below 50 (high ratio).
    assert out["components"]["margin_subscore"] < 30.0
    # The PCL ratio explainability matches.
    assert out["components"]["pcl_to_revenue_ratio"] > 0.20


def test_compute_financial_bank_path_diversified_noninterest_boost() -> None:
    """A universal-bank-style mix (>60% noninterest) saturates the ratio score."""
    nii = _eight_consecutive_quarters(
        2024, [500, 500, 500, 500, 500, 500, 500, 500]
    )
    # Noninterest much bigger than NII -> total rev 4 * (500+2000) = 10000;
    # noninterest ratio = 8000/10000 = 0.80 -> saturates at 100.
    noninterest = _four_consecutive_quarters(2025, [2000, 2000, 2000, 2000])
    # Low PCL ratio so PCL subscore stays high.
    pcl = _four_consecutive_quarters(2025, [100, 100, 100, 100])

    peer_growths = {"GS": 0.0, "P1": 0.0}

    out = financial.compute_financial(
        "GS",
        revenue_rows=[],
        gross_profit_rows=[],
        ocf_rows=[],
        peer_growths=peer_growths,
        sector="Financials",
        nii_rows=nii,
        noninterest_rows=noninterest,
        pcl_rows=pcl,
    )

    assert out["sector_path"] == "bank"
    # Noninterest ratio saturates -> 100.
    assert out["components"]["ocf_subscore"] == pytest.approx(100.0)
    assert out["components"]["noninterest_to_revenue_ratio"] == pytest.approx(
        0.80, abs=1e-6
    )


def test_compute_financial_bank_path_missing_pcl_renormalizes() -> None:
    """When PCL rows are absent the score should fall back to neutral on that axis
    AND renormalize the weighting away from it (same semantics as the standard
    path's data-quality renorm)."""
    nii = _eight_consecutive_quarters(
        2024, [950, 990, 1020, 1040, 1150, 1190, 1220, 1240]
    )
    noninterest = _four_consecutive_quarters(2025, [1100, 1180, 1240, 1280])
    # No PCL rows.
    peer_growths = {"JPM": 0.20, "P1": -0.05, "P2": 0.02, "P3": 0.06}

    out = financial.compute_financial(
        "JPM",
        revenue_rows=[],
        gross_profit_rows=[],
        ocf_rows=[],
        peer_growths=peer_growths,
        sector="Financials",
        nii_rows=nii,
        noninterest_rows=noninterest,
        pcl_rows=[],
    )

    assert out["sector_path"] == "bank"
    assert out["data_quality"]["has_margin"] is False
    assert out["data_quality"]["has_revenue"] is True
    assert out["data_quality"]["has_ocf"] is True
    # Effective margin weight should be 0.
    assert out["effective_weights"]["margin"] == pytest.approx(0.0)
    # The neutral 50 should not be polluting the score: subscore should be the
    # weight-renormalized blend of revenue (100) and ocf (75) only.
    # 40/70 * 100 + 30/70 * 75 ~ 89.3.
    assert out["sub_score"] == pytest.approx(
        round((40.0 / 70.0) * 100.0 + (30.0 / 70.0) * 75.0, 1),
        abs=0.2,
    )


def test_compute_financial_bank_path_falls_back_to_standard_without_nii() -> None:
    """A strict-bank ticker without bank inputs supplied must NOT route through
    the bank path -- the caller may not have plumbed bank fetches yet."""
    revenue_rows = _annual_pair(2025, 110.0, 100.0)
    out = financial.compute_financial(
        "JPM",
        revenue_rows=revenue_rows,
        gross_profit_rows=[],
        ocf_rows=[],
        peer_growths={"P1": 0.05},
        sector="Financials",
        # nii_rows not supplied -> standard path.
    )
    assert out.get("sector_path") != "bank"
    # Standard path: still computed off revenue_rows.
    assert out["components"]["revenue_growth_yoy"] == pytest.approx(0.10)


def test_compute_financial_non_bank_ticker_unchanged_by_new_signature() -> None:
    """Regression: a non-Financials ticker must score identically to pre-change.

    Even if a caller accidentally passes bank rows for a non-bank ticker, the
    standard path runs.
    """
    revenue_annual = _annual_pair(2025, 110.0, 100.0)
    qd = _quarter_dates_for_year(2025)
    revenue_quarters = [_q_row(q["start"], q["end"], 1000.0) for q in qd]
    revenue_rows = revenue_annual + revenue_quarters
    gp_rows = [_q_row(q["start"], q["end"], 300.0) for q in qd]
    ocf_rows = [_q_row(q["start"], q["end"], 200.0) for q in qd]

    # Spurious bank-row inputs that would route a bank ticker but must be
    # ignored for AAPL.
    spurious_nii = _eight_consecutive_quarters(
        2024, [1, 1, 1, 1, 1, 1, 1, 1]
    )

    peer_growths = {"AAPL": 0.10, "P1": -0.10, "P2": 0.0, "P3": 0.05}

    baseline = financial.compute_financial(
        "AAPL", revenue_rows, gp_rows, ocf_rows, peer_growths
    )
    with_bank_inputs = financial.compute_financial(
        "AAPL",
        revenue_rows,
        gp_rows,
        ocf_rows,
        peer_growths,
        sector="Technology",
        nii_rows=spurious_nii,
    )
    assert with_bank_inputs.get("sector_path") != "bank"
    assert with_bank_inputs["sub_score"] == baseline["sub_score"]
    assert with_bank_inputs["components"]["revenue_subscore"] == baseline[
        "components"
    ]["revenue_subscore"]


def test_compute_financial_bank_path_all_missing_yields_50() -> None:
    """If a strict-bank ticker is routed in but all bank inputs are insufficient
    (e.g. only 4 quarters of NII, no others) we still get the neutral fallback
    -- and the renorm doesn't try to divide by zero."""
    # 4 quarters only -> can't compute YoY (needs 8), can't compute ratios
    # (PCL absent).
    nii = _four_consecutive_quarters(2025, [1000, 1000, 1000, 1000])
    out = financial.compute_financial(
        "WFC",
        revenue_rows=[],
        gross_profit_rows=[],
        ocf_rows=[],
        peer_growths={"WFC": 0.0, "P1": 0.0},
        sector="Financials",
        nii_rows=nii,
        noninterest_rows=[],
        pcl_rows=[],
    )
    assert out["sector_path"] == "bank"
    assert out["sub_score"] == 50.0
    # Legacy data_quality keys must remain (downstream consumers check these).
    assert out["data_quality"]["has_revenue"] is False
    assert out["data_quality"]["has_margin"] is False
    assert out["data_quality"]["has_ocf"] is False


def test_is_bank_ticker_helper() -> None:
    """Allowlist semantics: strict membership, case-insensitive, trims whitespace."""
    assert financial.is_bank_ticker("JPM") is True
    assert financial.is_bank_ticker("jpm") is True
    assert financial.is_bank_ticker("  JPM  ") is True
    assert financial.is_bank_ticker("BAC") is True
    # Audit-driven expansion (May 2026): these are now in the cohort.
    assert financial.is_bank_ticker("BK") is True
    assert financial.is_bank_ticker("COF") is True
    assert financial.is_bank_ticker("SCHW") is True
    assert financial.is_bank_ticker("BLK") is True
    # Not in allowlist (payments / conglomerate / insurance / non-financials).
    assert financial.is_bank_ticker("AAPL") is False
    assert financial.is_bank_ticker("V") is False
    assert financial.is_bank_ticker("MET") is False
    assert financial.is_bank_ticker("") is False
    assert financial.is_bank_ticker(None) is False
    # Sector argument is accepted but allowlist is authoritative.
    assert financial.is_bank_ticker("AAPL", sector="Financials") is False
    assert financial.is_bank_ticker("JPM", sector=None) is True


# --- Bank cohort path (Tier-3 #15 audit fix) --------------------------------


def _build_bank_cohort_dicts(
    bank_data: Dict[str, Dict[str, List[Dict[str, Any]]]],
) -> tuple:
    """Helper: turn a {ticker: {"nii": rows, "nint": rows, "pcl": rows}} dict
    into the three cohort dicts that compute_financial expects.
    """
    nii_dict: Dict[str, List[Dict[str, Any]]] = {}
    nint_dict: Dict[str, List[Dict[str, Any]]] = {}
    pcl_dict: Dict[str, List[Dict[str, Any]]] = {}
    for t, d in bank_data.items():
        nii_dict[t] = d.get("nii", [])
        nint_dict[t] = d.get("nint", [])
        pcl_dict[t] = d.get("pcl", [])
    return nii_dict, nint_dict, pcl_dict


def _jpm_like_inputs(
    nii_yoy_pct: float = 0.20,
    noninterest_each: float = 1200.0,
    pcl_each: float = 240.0,
) -> Dict[str, List[Dict[str, Any]]]:
    """Build a bank's 4Q NII / Noninterest / PCL series.

    Year prior to ``start_year`` (=2024) NII = 1000/qtr. This-year (2025)
    NII = 1000 * (1 + nii_yoy_pct) / qtr.
    """
    prior = [1000.0] * 4
    current = [1000.0 * (1.0 + nii_yoy_pct)] * 4
    nii = _eight_consecutive_quarters(2024, prior + current)
    noninterest = _four_consecutive_quarters(2025, [noninterest_each] * 4)
    pcl = _four_consecutive_quarters(2025, [pcl_each] * 4)
    return {"nii": nii, "nint": noninterest, "pcl": pcl}


def test_bank_cohort_path_ranks_revenue_within_banks_not_universe() -> None:
    """Tier-3 #15 fix: JPM revenue % rank vs banks ≠ JPM vs full universe.

    With NVDA-like outliers in the universe (+65%) JPM (+3%) would land at
    the bottom on universe peer_growths. Inside the bank cohort it's mid-pack.
    """
    jpm = _jpm_like_inputs(nii_yoy_pct=0.05)
    bac = _jpm_like_inputs(nii_yoy_pct=0.02)
    wfc = _jpm_like_inputs(nii_yoy_pct=0.01)
    cohort = {"JPM": jpm, "BAC": bac, "WFC": wfc}
    nii_d, nint_d, pcl_d = _build_bank_cohort_dicts(cohort)

    # peer_growths: JPM rev = 0.03; tech megacaps far above.
    peer_growths = {
        "JPM": 0.03,
        "BAC": 0.02,
        "WFC": 0.01,
        "NVDA": 0.65,
        "AAPL": 0.06,
        "MSFT": 0.12,
    }

    out = financial.compute_financial(
        "JPM",
        revenue_rows=[],
        gross_profit_rows=[],
        ocf_rows=[],
        peer_growths=peer_growths,
        sector="Financials",
        nii_rows=jpm["nii"],
        noninterest_rows=jpm["nint"],
        pcl_rows=jpm["pcl"],
        bank_cohort_nii_rows=nii_d,
        bank_cohort_noninterest_rows=nint_d,
        bank_cohort_pcl_rows=pcl_d,
    )

    assert out["sector_path"] == "bank"
    assert out["data_quality"]["is_bank_cohort"] is True
    # JPM rev 0.03 vs BAC 0.02, WFC 0.01 (cohort) -> ranks above both -> 100.
    assert out["components"]["revenue_subscore"] == pytest.approx(100.0)
    # NII growth 0.05 same as cohort peers (since identical YoY structure),
    # but JPM's NII growth (0.05) is also above BAC (0.02) and WFC (0.01) -> 100.
    assert out["components"]["nii_subscore"] == pytest.approx(100.0)


def test_bank_cohort_path_nii_is_primary_signal() -> None:
    """NII subscore drives most of the score (50% weight)."""
    # Focal has weak NII growth but strong revenue growth & diversification.
    jpm = _jpm_like_inputs(nii_yoy_pct=-0.05)  # focal: -5% NII
    bac = _jpm_like_inputs(nii_yoy_pct=0.20)
    wfc = _jpm_like_inputs(nii_yoy_pct=0.20)
    c   = _jpm_like_inputs(nii_yoy_pct=0.20)
    cohort = {"JPM": jpm, "BAC": bac, "WFC": wfc, "C": c}
    nii_d, nint_d, pcl_d = _build_bank_cohort_dicts(cohort)

    peer_growths = {"JPM": 0.0, "BAC": 0.0, "WFC": 0.0, "C": 0.0}

    out = financial.compute_financial(
        "JPM",
        revenue_rows=[],
        gross_profit_rows=[],
        ocf_rows=[],
        peer_growths=peer_growths,
        sector="Financials",
        nii_rows=jpm["nii"],
        noninterest_rows=jpm["nint"],
        pcl_rows=jpm["pcl"],
        bank_cohort_nii_rows=nii_d,
        bank_cohort_noninterest_rows=nint_d,
        bank_cohort_pcl_rows=pcl_d,
    )

    # JPM is at the bottom of NII growth -> nii_subscore = 0.
    assert out["components"]["nii_subscore"] == pytest.approx(0.0)
    # NII has weight 0.50 -> sub_score should be well below 50 even if
    # the other axes are neutral.
    assert out["sub_score"] < 50.0


def test_bank_cohort_credit_subscore_inverted_lower_is_better() -> None:
    """Credit subscore: focal with low PCL/NII ratio ranks high (inverted)."""
    # Focal: low PCL -> low PCL/NII ratio (=0.05).
    jpm = _jpm_like_inputs(nii_yoy_pct=0.05, pcl_each=50.0)  # PCL/NII = 200/4000=0.05
    # Other banks: high PCL -> high PCL/NII ratio (=0.30).
    bac = _jpm_like_inputs(nii_yoy_pct=0.05, pcl_each=300.0)
    wfc = _jpm_like_inputs(nii_yoy_pct=0.05, pcl_each=350.0)
    cohort = {"JPM": jpm, "BAC": bac, "WFC": wfc}
    nii_d, nint_d, pcl_d = _build_bank_cohort_dicts(cohort)

    peer_growths = {"JPM": 0.05, "BAC": 0.05, "WFC": 0.05}

    out = financial.compute_financial(
        "JPM",
        revenue_rows=[],
        gross_profit_rows=[],
        ocf_rows=[],
        peer_growths=peer_growths,
        sector="Financials",
        nii_rows=jpm["nii"],
        noninterest_rows=jpm["nint"],
        pcl_rows=jpm["pcl"],
        bank_cohort_nii_rows=nii_d,
        bank_cohort_noninterest_rows=nint_d,
        bank_cohort_pcl_rows=pcl_d,
    )

    # JPM has lowest PCL/NII -> inverted percentile = 100.
    assert out["components"]["credit_subscore"] == pytest.approx(100.0)
    # Also surfaces in legacy "margin" slot for back-compat.
    assert out["components"]["margin_subscore"] == pytest.approx(100.0)


def test_bank_cohort_diversification_higher_is_better() -> None:
    """Diversification subscore: focal with high noninterest mix ranks high."""
    # Focal: noninterest=2000 vs NII=1000 -> mix = 2000/3000 = 0.667.
    jpm = _jpm_like_inputs(nii_yoy_pct=0.05, noninterest_each=2000.0)
    # Other banks: low noninterest.
    bac = _jpm_like_inputs(nii_yoy_pct=0.05, noninterest_each=100.0)
    wfc = _jpm_like_inputs(nii_yoy_pct=0.05, noninterest_each=200.0)
    cohort = {"JPM": jpm, "BAC": bac, "WFC": wfc}
    nii_d, nint_d, pcl_d = _build_bank_cohort_dicts(cohort)

    peer_growths = {"JPM": 0.05, "BAC": 0.05, "WFC": 0.05}

    out = financial.compute_financial(
        "JPM",
        revenue_rows=[],
        gross_profit_rows=[],
        ocf_rows=[],
        peer_growths=peer_growths,
        sector="Financials",
        nii_rows=jpm["nii"],
        noninterest_rows=jpm["nint"],
        pcl_rows=jpm["pcl"],
        bank_cohort_nii_rows=nii_d,
        bank_cohort_noninterest_rows=nint_d,
        bank_cohort_pcl_rows=pcl_d,
    )

    assert out["components"]["diversification_subscore"] == pytest.approx(100.0)
    # Surfaces in legacy "ocf" slot.
    assert out["components"]["ocf_subscore"] == pytest.approx(100.0)


def test_bank_cohort_path_missing_pcl_renormalizes_weights() -> None:
    """No PCL data -> credit subscore drops out, weights renormalize."""
    jpm = _jpm_like_inputs(nii_yoy_pct=0.05)
    jpm["pcl"] = []  # remove focal PCL data
    bac = _jpm_like_inputs(nii_yoy_pct=0.02)
    wfc = _jpm_like_inputs(nii_yoy_pct=0.01)
    cohort = {"JPM": jpm, "BAC": bac, "WFC": wfc}
    nii_d, nint_d, pcl_d = _build_bank_cohort_dicts(cohort)

    peer_growths = {"JPM": 0.03, "BAC": 0.02, "WFC": 0.01}

    out = financial.compute_financial(
        "JPM",
        revenue_rows=[],
        gross_profit_rows=[],
        ocf_rows=[],
        peer_growths=peer_growths,
        sector="Financials",
        nii_rows=jpm["nii"],
        noninterest_rows=jpm["nint"],
        pcl_rows=jpm["pcl"],
        bank_cohort_nii_rows=nii_d,
        bank_cohort_noninterest_rows=nint_d,
        bank_cohort_pcl_rows=pcl_d,
    )

    assert out["data_quality"]["has_pcl"] is False
    # Credit weight should renorm to 0.
    assert out["effective_weights"]["credit"] == pytest.approx(0.0)
    # NII + revenue + diversification should renorm to 1.0 total.
    total = (
        out["effective_weights"]["nii"]
        + out["effective_weights"]["revenue"]
        + out["effective_weights"]["diversification"]
    )
    assert total == pytest.approx(1.0)


def test_bank_cohort_path_missing_nii_falls_back_to_revenue_only() -> None:
    """When focal NII can't be computed but revenue can, score still works."""
    # Focal: only 4 quarters NII -> no YoY computable. Still pass empty PCL.
    jpm_nii_short = _four_consecutive_quarters(2025, [1000.0, 1000.0, 1000.0, 1000.0])
    jpm_nint = _four_consecutive_quarters(2025, [200.0] * 4)
    jpm = {"nii": jpm_nii_short, "nint": jpm_nint, "pcl": []}
    bac = _jpm_like_inputs(nii_yoy_pct=0.02)
    wfc = _jpm_like_inputs(nii_yoy_pct=0.01)
    cohort = {"JPM": jpm, "BAC": bac, "WFC": wfc}
    nii_d, nint_d, pcl_d = _build_bank_cohort_dicts(cohort)

    peer_growths = {"JPM": 0.03, "BAC": 0.02, "WFC": 0.01}

    out = financial.compute_financial(
        "JPM",
        revenue_rows=[],
        gross_profit_rows=[],
        ocf_rows=[],
        peer_growths=peer_growths,
        sector="Financials",
        nii_rows=jpm["nii"],
        noninterest_rows=jpm["nint"],
        pcl_rows=jpm["pcl"],
        bank_cohort_nii_rows=nii_d,
        bank_cohort_noninterest_rows=nint_d,
        bank_cohort_pcl_rows=pcl_d,
    )

    assert out["data_quality"]["is_bank_cohort"] is True
    assert out["data_quality"]["has_nii"] is False
    assert out["data_quality"]["has_rev_pct"] is True
    # Effective NII weight should be 0; revenue weight should be > 0.
    assert out["effective_weights"]["nii"] == pytest.approx(0.0)
    assert out["effective_weights"]["revenue"] > 0.0


def test_bank_cohort_path_bank_ticker_membership_case_insensitive() -> None:
    """Defensive: cohort dicts with mixed-case bank tickers are handled."""
    jpm = _jpm_like_inputs(nii_yoy_pct=0.05)
    # Cohort dict keys deliberately lower / mixed case.
    cohort = {"jpm": jpm, "Bac": _jpm_like_inputs(nii_yoy_pct=0.02)}
    nii_d, nint_d, pcl_d = _build_bank_cohort_dicts(cohort)

    peer_growths = {"JPM": 0.05, "BAC": 0.02}

    out = financial.compute_financial(
        "JPM",
        revenue_rows=[],
        gross_profit_rows=[],
        ocf_rows=[],
        peer_growths=peer_growths,
        sector="Financials",
        nii_rows=jpm["nii"],
        noninterest_rows=jpm["nint"],
        pcl_rows=jpm["pcl"],
        bank_cohort_nii_rows=nii_d,
        bank_cohort_noninterest_rows=nint_d,
        bank_cohort_pcl_rows=pcl_d,
    )

    # Cohort filtering didn't drop the mixed-case banks: JPM still has a peer.
    assert out["components"]["nii_subscore"] == pytest.approx(100.0)


def test_bank_cohort_path_non_bank_in_cohort_dicts_ignored() -> None:
    """Cohort dicts may accidentally contain non-bank tickers; they must be filtered."""
    jpm = _jpm_like_inputs(nii_yoy_pct=0.05)
    bac = _jpm_like_inputs(nii_yoy_pct=0.02)
    # Spurious non-bank entry (AAPL) in the cohort dict -- should be ignored.
    aapl_fake = _jpm_like_inputs(nii_yoy_pct=0.99)
    cohort = {"JPM": jpm, "BAC": bac, "AAPL": aapl_fake}
    nii_d, nint_d, pcl_d = _build_bank_cohort_dicts(cohort)

    peer_growths = {"JPM": 0.05, "BAC": 0.02, "AAPL": 0.99}

    out = financial.compute_financial(
        "JPM",
        revenue_rows=[],
        gross_profit_rows=[],
        ocf_rows=[],
        peer_growths=peer_growths,
        sector="Financials",
        nii_rows=jpm["nii"],
        noninterest_rows=jpm["nint"],
        pcl_rows=jpm["pcl"],
        bank_cohort_nii_rows=nii_d,
        bank_cohort_noninterest_rows=nint_d,
        bank_cohort_pcl_rows=pcl_d,
    )

    # AAPL ignored -> JPM ranks vs BAC only. NII growth 0.05 > 0.02 -> 100.
    assert out["components"]["nii_subscore"] == pytest.approx(100.0)
    # Revenue: JPM 0.05 vs BAC 0.02 (AAPL 0.99 dropped) -> 100.
    assert out["components"]["revenue_subscore"] == pytest.approx(100.0)


def test_non_bank_ticker_path_completely_unchanged_with_cohort_dicts() -> None:
    """Regression guard: passing bank cohort dicts must not affect a non-bank."""
    revenue_annual = _annual_pair(2025, 110.0, 100.0)
    qd = _quarter_dates_for_year(2025)
    revenue_quarters = [_q_row(q["start"], q["end"], 1000.0) for q in qd]
    revenue_rows = revenue_annual + revenue_quarters
    gp_rows = [_q_row(q["start"], q["end"], 300.0) for q in qd]
    ocf_rows = [_q_row(q["start"], q["end"], 200.0) for q in qd]
    peer_growths = {"AAPL": 0.10, "P1": -0.10, "P2": 0.0, "P3": 0.05}

    baseline = financial.compute_financial(
        "AAPL", revenue_rows, gp_rows, ocf_rows, peer_growths
    )

    # Pretend the caller naively passes bank cohort dicts -- AAPL must ignore them.
    jpm = _jpm_like_inputs(nii_yoy_pct=0.05)
    bac = _jpm_like_inputs(nii_yoy_pct=0.02)
    cohort = {"JPM": jpm, "BAC": bac}
    nii_d, nint_d, pcl_d = _build_bank_cohort_dicts(cohort)

    with_cohort = financial.compute_financial(
        "AAPL",
        revenue_rows,
        gp_rows,
        ocf_rows,
        peer_growths,
        sector="Technology",
        bank_cohort_nii_rows=nii_d,
        bank_cohort_noninterest_rows=nint_d,
        bank_cohort_pcl_rows=pcl_d,
    )

    assert with_cohort["sub_score"] == baseline["sub_score"]
    assert with_cohort["components"]["revenue_subscore"] == baseline[
        "components"
    ]["revenue_subscore"]
    assert with_cohort["components"]["margin_subscore"] == baseline[
        "components"
    ]["margin_subscore"]
    assert with_cohort["components"]["ocf_subscore"] == baseline[
        "components"
    ]["ocf_subscore"]
    assert with_cohort.get("sector_path") != "bank"


def test_bank_cohort_weights_sum_to_one() -> None:
    """All four bank-cohort weights must sum to 1.0."""
    total = (
        financial._BANK_NII_WEIGHT
        + financial._BANK_REVENUE_WEIGHT
        + financial._BANK_CREDIT_WEIGHT
        + financial._BANK_DIVERSIFICATION_WEIGHT
    )
    assert total == pytest.approx(1.0)


def test_bank_cohort_data_quality_flags_all_present_path() -> None:
    """When all bank inputs are present and cohort is supplied, all flags = True."""
    jpm = _jpm_like_inputs(nii_yoy_pct=0.05)
    bac = _jpm_like_inputs(nii_yoy_pct=0.02)
    wfc = _jpm_like_inputs(nii_yoy_pct=0.01)
    cohort = {"JPM": jpm, "BAC": bac, "WFC": wfc}
    nii_d, nint_d, pcl_d = _build_bank_cohort_dicts(cohort)

    peer_growths = {"JPM": 0.03, "BAC": 0.02, "WFC": 0.01}

    out = financial.compute_financial(
        "JPM",
        revenue_rows=[],
        gross_profit_rows=[],
        ocf_rows=[],
        peer_growths=peer_growths,
        sector="Financials",
        nii_rows=jpm["nii"],
        noninterest_rows=jpm["nint"],
        pcl_rows=jpm["pcl"],
        bank_cohort_nii_rows=nii_d,
        bank_cohort_noninterest_rows=nint_d,
        bank_cohort_pcl_rows=pcl_d,
    )

    dq = out["data_quality"]
    assert dq["is_bank_cohort"] is True
    assert dq["has_nii"] is True
    assert dq["has_pcl"] is True
    assert dq["has_noninterest"] is True
    assert dq["has_rev_pct"] is True


def test_bank_legacy_path_without_cohort_still_works() -> None:
    """A bank ticker without cohort dicts uses the legacy 40/30/30 absolute path."""
    jpm = _jpm_like_inputs(nii_yoy_pct=0.20)
    peer_growths = {"JPM": 0.20, "P1": -0.05, "P2": 0.02}

    out = financial.compute_financial(
        "JPM",
        revenue_rows=[],
        gross_profit_rows=[],
        ocf_rows=[],
        peer_growths=peer_growths,
        sector="Financials",
        nii_rows=jpm["nii"],
        noninterest_rows=jpm["nint"],
        pcl_rows=jpm["pcl"],
        # No cohort dicts -> legacy path.
    )

    assert out["sector_path"] == "bank"
    assert out["data_quality"]["is_bank_cohort"] is False
    # Legacy path uses universe-wide percentile via peer_growths.
    assert out["components"]["revenue_subscore"] == pytest.approx(100.0)


def test_bank_cohort_revenue_subscore_differs_from_universe_subscore() -> None:
    """The whole point of the fix: cohort-rank ≠ universe-rank for a bank near a tech-heavy universe."""
    # Build a small cohort where JPM ranks high.
    jpm = _jpm_like_inputs(nii_yoy_pct=0.03)
    bac = _jpm_like_inputs(nii_yoy_pct=0.01)
    wfc = _jpm_like_inputs(nii_yoy_pct=0.005)
    cohort = {"JPM": jpm, "BAC": bac, "WFC": wfc}
    nii_d, nint_d, pcl_d = _build_bank_cohort_dicts(cohort)

    # Universe: banks plus hyper-growth tech.
    peer_growths = {
        "JPM": 0.03,
        "BAC": 0.01,
        "WFC": 0.005,
        "NVDA": 0.65,
        "AAPL": 0.06,
        "MSFT": 0.12,
    }

    # Cohort path: JPM cohort-rank on revenue.
    cohort_out = financial.compute_financial(
        "JPM",
        revenue_rows=[],
        gross_profit_rows=[],
        ocf_rows=[],
        peer_growths=peer_growths,
        sector="Financials",
        nii_rows=jpm["nii"],
        noninterest_rows=jpm["nint"],
        pcl_rows=jpm["pcl"],
        bank_cohort_nii_rows=nii_d,
        bank_cohort_noninterest_rows=nint_d,
        bank_cohort_pcl_rows=pcl_d,
    )

    # Standard-path: JPM ranked vs full universe (the legacy buggy behavior).
    # We force the standard path by NOT routing through bank (use a non-bank
    # ticker with the same growth, ranked against the same universe).
    rev_rows = _annual_pair(2025, 103.0, 100.0)  # 3% YoY
    pg_for_universe = dict(peer_growths)
    pg_for_universe["FOO"] = 0.03  # focal explicitly excluded
    universe_out = financial.compute_financial(
        "FOO", rev_rows, [], [], pg_for_universe
    )

    cohort_rev = cohort_out["components"]["revenue_subscore"]
    universe_rev = universe_out["components"]["revenue_subscore"]
    # Tier-3 #15: cohort rank must be visibly HIGHER than universe rank.
    assert cohort_rev > universe_rev
    # Sanity: cohort rank should be > 50 (JPM at top of bank cohort).
    assert cohort_rev > 50.0
    # And universe rank should be < 50 (NVDA/MSFT push JPM down).
    assert universe_rev < 50.0


# --- compute_financial with compound peer-key (Tier 2 #7) ------------------


def _pg_config_two_groups() -> Dict[str, Any]:
    return {
        "min_cohort_size": 3,
        "sector_groups": {
            "group_a": {"tickers": ["FOO", "P1", "P2", "P3"]},
            "group_b": {"tickers": ["P4", "P5", "P6", "P7", "P8", "P9"]},
        },
    }


def _synthetic_universe_two_groups() -> Dict[str, Any]:
    return {
        "tickers": [
            {"ticker": tk, "maturity_stage": "mature_compounder", "active": True}
            for tk in ["FOO", "P1", "P2", "P3", "P4", "P5", "P6", "P7", "P8", "P9"]
        ]
    }


def test_compute_financial_peer_groups_config_none_preserves_legacy() -> None:
    """peer_groups_config=None -> identical to legacy behaviour."""
    revenue_rows = _annual_pair(2025, 110.0, 100.0)
    peer_growths = {
        "FOO": 0.10,
        "P1": -0.10,
        "P2": -0.05,
        "P3": 0.00,
        "P4": 0.05,
        "P5": 0.08,
        "P6": 0.12,
        "P7": 0.18,
        "P8": 0.25,
        "P9": 0.30,
    }
    out = financial.compute_financial("FOO", revenue_rows, [], [], peer_growths)
    # 9 peers, 5 strictly below growth=0.10, 0 equal, 4 above -> 5/9 ≈ 55.56.
    assert out["components"]["revenue_subscore"] == pytest.approx(55.5556, abs=1e-3)
    assert out["components"]["peer_cohort_strategy"] == "maturity_only"
    assert "peer_cohort_size" not in out["components"]


def test_compute_financial_peer_groups_config_restricts_cohort() -> None:
    """When peer_groups_config + universe provided, restrict to compound cohort."""
    revenue_rows = _annual_pair(2025, 110.0, 100.0)
    peer_growths = {
        "FOO": 0.10,
        "P1": -0.10,
        "P2": -0.05,
        "P3": 0.00,
        "P4": 0.05,
        "P5": 0.08,
        "P6": 0.12,
        "P7": 0.18,
        "P8": 0.25,
        "P9": 0.30,
    }
    out = financial.compute_financial(
        "FOO",
        revenue_rows,
        [],
        [],
        peer_growths,
        peer_groups_config=_pg_config_two_groups(),
        universe=_synthetic_universe_two_groups(),
    )
    # Compound cohort = group_a (4 incl FOO). Peers excl focal = [-0.10, -0.05, 0.00].
    # growth 0.10 above all 3 -> 100.
    assert out["components"]["revenue_subscore"] == pytest.approx(100.0)
    assert out["components"]["peer_cohort_strategy"] == "compound"
    assert out["components"]["peer_cohort_size"] == 4


def test_compute_financial_compound_changes_rank_vs_universe() -> None:
    """The compound cohort should produce a different rank than the universe.

    Mirrors the AAPL spot-check in adoption: focal lives with weaker peers
    in its sector_group, scores higher than against the broad universe."""
    revenue_rows = _annual_pair(2025, 110.0, 100.0)  # growth 10%
    peer_growths = {
        "FOO": 0.10,
        "P1": -0.10,
        "P2": -0.05,
        "P3": 0.00,
        "P4": 0.05,
        "P5": 0.08,
        "P6": 0.12,
        "P7": 0.18,
        "P8": 0.25,
        "P9": 0.30,
    }
    legacy = financial.compute_financial("FOO", revenue_rows, [], [], peer_growths)
    compound = financial.compute_financial(
        "FOO",
        revenue_rows,
        [],
        [],
        peer_growths,
        peer_groups_config=_pg_config_two_groups(),
        universe=_synthetic_universe_two_groups(),
    )
    assert (
        compound["components"]["revenue_subscore"]
        > legacy["components"]["revenue_subscore"]
    )


def test_compute_financial_safety_valve_fires_when_cohort_too_thin() -> None:
    """Force a universe fallback by setting min_cohort_size higher than any
    bucket can satisfy."""
    revenue_rows = _annual_pair(2025, 110.0, 100.0)
    peer_growths = {
        "FOO": 0.10,
        "P1": 0.05,
        "P2": 0.08,
        "P3": 0.12,
        "P4": 0.20,
        "P5": 0.30,
    }
    syn_config = {
        "min_cohort_size": 10,
        "sector_groups": {
            "tiny": {"tickers": ["FOO"]},
            "other": {"tickers": ["P1", "P2", "P3", "P4", "P5"]},
        },
    }
    syn_universe = {
        "tickers": [
            {"ticker": tk, "maturity_stage": "mature_compounder", "active": True}
            for tk in ["FOO", "P1", "P2", "P3", "P4", "P5"]
        ]
    }
    out = financial.compute_financial(
        "FOO",
        revenue_rows,
        [],
        [],
        peer_growths,
        peer_groups_config=syn_config,
        universe=syn_universe,
    )
    assert out["components"]["peer_cohort_strategy"] == "universe_fallback"


# ---------------------------------------------------------------------------
# Gross-margin XBRL fallback chain (P3 audit fix-up, May 2026)
# ---------------------------------------------------------------------------
#
# Test that when ``GrossProfit`` is missing the Financial pillar walks the
# fallback chain in priority order and surfaces the chosen source via
# ``components.margin_source``.


def _build_quarters_with_values(
    year: int, revenue_each: float, numerator_each: List[float]
) -> tuple:
    """Build paired revenue + numerator quarterly rows. Returns (rev_rows, num_rows)."""
    qdates = _quarter_dates_for_year(year)
    rev_rows: List[Dict[str, Any]] = []
    num_rows: List[Dict[str, Any]] = []
    for q, n in zip(qdates, numerator_each):
        rev_rows.append(_q_row(q["start"], q["end"], revenue_each))
        num_rows.append(_q_row(q["start"], q["end"], n))
    return rev_rows, num_rows


def test_resolve_gross_margin_source_prefers_canonical_gross_profit() -> None:
    """When GrossProfit is present and has 4 quarters, source is gross_profit."""
    rev, gp = _build_quarters_with_margins(2025, 1000.0, [0.30, 0.32, 0.34, 0.36])
    _, srg = _build_quarters_with_margins(2025, 900.0, [0.50, 0.50, 0.50, 0.50])
    _, cor = _build_quarters_with_values(
        2025, 1000.0, [600.0, 600.0, 600.0, 600.0]
    )
    _, op_inc = _build_quarters_with_values(
        2025, 1000.0, [200.0, 200.0, 200.0, 200.0]
    )

    rows, source = financial.resolve_gross_margin_source(
        rev, gp,
        sales_revenue_gross_rows=srg,
        cost_of_revenue_rows=cor,
        operating_income_rows=op_inc,
    )
    assert source == financial.MARGIN_SOURCE_GROSS_PROFIT
    assert rows is gp


def test_resolve_gross_margin_source_falls_back_to_revenue_minus_cost() -> None:
    """No GrossProfit -> compute synthetic GP from Revenue - CostOfRevenue."""
    qd = _quarter_dates_for_year(2025)
    rev = [_q_row(q["start"], q["end"], 1000.0) for q in qd]
    cor = [_q_row(q["start"], q["end"], 700.0) for q in qd]

    rows, source = financial.resolve_gross_margin_source(
        rev, [],
        cost_of_revenue_rows=cor,
    )
    assert source == financial.MARGIN_SOURCE_REVENUE_MINUS_COST
    assert len(rows) == 4
    assert all(r["value"] == 300.0 for r in rows)
    assert all(r["concept"] == "_RevenueMinusCostOfRevenue" for r in rows)


def test_resolve_gross_margin_source_falls_back_to_sales_revenue_gross() -> None:
    """No GrossProfit and no modern revenue, but legacy SRG + cost present."""
    qd = _quarter_dates_for_year(2024)
    srg = [_q_row(q["start"], q["end"], 800.0) for q in qd]
    cor = [_q_row(q["start"], q["end"], 500.0) for q in qd]
    rev_one = [_q_row("2024-10-01", "2024-12-31", 1500.0)]

    rows, source = financial.resolve_gross_margin_source(
        rev_one, [],
        sales_revenue_gross_rows=srg,
        cost_of_revenue_rows=cor,
    )
    assert source == financial.MARGIN_SOURCE_SALES_REVENUE_GROSS
    assert len(rows) == 4
    assert all(r["value"] == 300.0 for r in rows)


def test_resolve_gross_margin_source_falls_back_to_operating_income() -> None:
    """No GrossProfit, no CostOfRevenue, but OperatingIncomeLoss present."""
    qd = _quarter_dates_for_year(2025)
    rev = [_q_row(q["start"], q["end"], 1000.0) for q in qd]
    op_inc = [_q_row(q["start"], q["end"], 200.0) for q in qd]

    rows, source = financial.resolve_gross_margin_source(
        rev, [],
        operating_income_rows=op_inc,
    )
    assert source == financial.MARGIN_SOURCE_OPERATING_INCOME
    assert rows is op_inc


def test_resolve_gross_margin_source_returns_none_when_all_missing() -> None:
    """No data anywhere -> ([], MARGIN_SOURCE_NONE)."""
    rows, source = financial.resolve_gross_margin_source([], [])
    assert source == financial.MARGIN_SOURCE_NONE
    assert rows == []


def test_resolve_gross_margin_source_requires_min_four_quarters() -> None:
    """A candidate with only 3 quarterly pairs cannot win -- falls through."""
    qd = _quarter_dates_for_year(2025)
    rev = [_q_row(q["start"], q["end"], 1000.0) for q in qd]
    gp_thin = [_q_row(q["start"], q["end"], 300.0) for q in qd[:3]]
    op_inc = [_q_row(q["start"], q["end"], 200.0) for q in qd]

    rows, source = financial.resolve_gross_margin_source(
        rev, gp_thin,
        operating_income_rows=op_inc,
    )
    assert source == financial.MARGIN_SOURCE_OPERATING_INCOME
    assert rows is op_inc


def test_compute_financial_surfaces_margin_source_gross_profit() -> None:
    """Standard happy path -- canonical GrossProfit -> source tag exposed."""
    rev, gp = _build_quarters_with_margins(
        2025, 1000.0, [0.30, 0.32, 0.34, 0.36]
    )
    rev = _annual_pair(2025, 4400.0, 4000.0) + rev

    out = financial.compute_financial(
        "AAPL", rev, gp, [], peer_growths={"AAPL": 0.10, "P1": 0.05},
    )
    assert out["components"]["margin_source"] == financial.MARGIN_SOURCE_GROSS_PROFIT
    assert out["components"]["margin_subscore"] > 50.0


def test_compute_financial_uses_revenue_minus_cost_fallback() -> None:
    """When GP is missing the Financial pillar reaches CostOfRevenue."""
    qd = _quarter_dates_for_year(2025)
    rev = [_q_row(q["start"], q["end"], 1000.0) for q in qd]
    rev = _annual_pair(2025, 4400.0, 4000.0) + rev
    cor = [
        _q_row(qd[0]["start"], qd[0]["end"], 700.0),
        _q_row(qd[1]["start"], qd[1]["end"], 680.0),
        _q_row(qd[2]["start"], qd[2]["end"], 660.0),
        _q_row(qd[3]["start"], qd[3]["end"], 640.0),
    ]

    out = financial.compute_financial(
        "FOO", rev, [], [], peer_growths={"FOO": 0.10, "P1": 0.05},
        cost_of_revenue_rows=cor,
    )
    assert (
        out["components"]["margin_source"]
        == financial.MARGIN_SOURCE_REVENUE_MINUS_COST
    )
    assert out["components"]["margin_subscore"] > 50.0
    assert out["data_quality"]["has_margin"] is True


def test_compute_financial_falls_back_to_operating_income() -> None:
    """When neither GP nor CostOfRevenue exist, OperatingIncomeLoss is used."""
    qd = _quarter_dates_for_year(2025)
    rev = [_q_row(q["start"], q["end"], 1000.0) for q in qd]
    rev = _annual_pair(2025, 4400.0, 4000.0) + rev
    op_inc = [
        _q_row(qd[0]["start"], qd[0]["end"], 150.0),
        _q_row(qd[1]["start"], qd[1]["end"], 170.0),
        _q_row(qd[2]["start"], qd[2]["end"], 190.0),
        _q_row(qd[3]["start"], qd[3]["end"], 210.0),
    ]

    out = financial.compute_financial(
        "SVCS", rev, [], [], peer_growths={"SVCS": 0.10, "P1": 0.05},
        operating_income_rows=op_inc,
    )
    assert (
        out["components"]["margin_source"]
        == financial.MARGIN_SOURCE_OPERATING_INCOME
    )
    assert out["components"]["margin_subscore"] > 50.0
    assert out["data_quality"]["has_margin"] is True


def test_compute_financial_margin_source_none_keeps_neutral_score() -> None:
    """No GP and no fallback data -> margin_source=none, subscore=50, has_margin=False."""
    rev = _annual_pair(2025, 110.0, 100.0)
    out = financial.compute_financial(
        "EMPTY", rev, [], [], peer_growths={"EMPTY": 0.10, "P1": 0.05},
    )
    assert out["components"]["margin_source"] == financial.MARGIN_SOURCE_NONE
    assert out["components"]["margin_subscore"] == 50.0
    assert out["data_quality"]["has_margin"] is False


def test_compute_financial_fallback_chain_priority_order() -> None:
    """When *all* fallback sources are present but GP is missing, the chain
    must pick the higher-priority candidate (Revenues - CostOfRevenue),
    not OperatingIncome."""
    qd = _quarter_dates_for_year(2025)
    rev = [_q_row(q["start"], q["end"], 1000.0) for q in qd]
    rev = _annual_pair(2025, 4400.0, 4000.0) + rev
    cor = [_q_row(q["start"], q["end"], 700.0) for q in qd]
    op_inc = [_q_row(q["start"], q["end"], 200.0) for q in qd]

    out = financial.compute_financial(
        "FOO", rev, [], [], peer_growths={"FOO": 0.10, "P1": 0.05},
        cost_of_revenue_rows=cor,
        operating_income_rows=op_inc,
    )
    assert (
        out["components"]["margin_source"]
        == financial.MARGIN_SOURCE_REVENUE_MINUS_COST
    )


def test_compute_financial_bank_path_margin_source_is_bank() -> None:
    """Bank path stamps ``margin_source = "bank"`` so it's never confused
    with a missing-margin neutral on the standard path."""
    nii = _eight_consecutive_quarters(
        2024, [950, 990, 1020, 1040, 1150, 1190, 1220, 1240]
    )
    out = financial.compute_financial(
        "JPM",
        revenue_rows=[],
        gross_profit_rows=[],
        ocf_rows=[],
        peer_growths={"JPM": 0.20, "P1": 0.0},
        sector="Financials",
        nii_rows=nii,
        noninterest_rows=[],
        pcl_rows=[],
    )
    assert out["sector_path"] == "bank"
    assert out["components"]["margin_source"] == "bank"


def test_synthesize_gross_profit_drops_negative_synthetic_gp() -> None:
    """If cost > revenue for a period, the synthetic GP would be negative.
    Drop the period rather than feed garbage to the slope estimator."""
    qd = _quarter_dates_for_year(2025)
    rev = [_q_row(q["start"], q["end"], 1000.0) for q in qd]
    cor = [
        _q_row(qd[0]["start"], qd[0]["end"], 700.0),
        _q_row(qd[1]["start"], qd[1]["end"], 1500.0),
        _q_row(qd[2]["start"], qd[2]["end"], 600.0),
        _q_row(qd[3]["start"], qd[3]["end"], 500.0),
    ]
    out = financial._synthesize_gross_profit_rows(rev, cor)
    assert len(out) == 3
    end_dates = {r["end_date"] for r in out}
    assert "2025-06-30" not in end_dates


# ---------------------------------------------------------------------------
# Bank cohort expansion (P3 audit fix-up, May 2026)
# ---------------------------------------------------------------------------


def test_bank_tickers_expanded_to_twelve() -> None:
    """The audit-driven cohort expansion adds BK, COF, SCHW, BLK to the
    original 8-ticker strict-bank list."""
    assert "BK" in financial.BANK_TICKERS
    assert "COF" in financial.BANK_TICKERS
    assert "SCHW" in financial.BANK_TICKERS
    assert "BLK" in financial.BANK_TICKERS
    for sym in ("JPM", "BAC", "WFC", "C", "GS", "MS", "USB", "TFC"):
        assert sym in financial.BANK_TICKERS
    assert len(financial.BANK_TICKERS) == 12


def test_bank_cohort_percentile_includes_expansion_members() -> None:
    """A focal bank's NII percentile must be ranked against the EXPANDED
    cohort, not just the original 7-ticker active set."""
    jpm_nii = _eight_consecutive_quarters(
        2024, [1000, 1020, 1040, 1060, 1200, 1230, 1260, 1290]
    )
    bac_nii = _eight_consecutive_quarters(
        2024, [800, 820, 840, 860, 880, 900, 920, 940]
    )
    bk_nii = _eight_consecutive_quarters(
        2024, [500, 510, 520, 530, 540, 550, 560, 570]
    )
    cof_nii = _eight_consecutive_quarters(
        2024, [400, 410, 420, 430, 440, 450, 460, 470]
    )
    schw_nii = _eight_consecutive_quarters(
        2024, [300, 310, 320, 330, 340, 350, 360, 370]
    )

    bank_cohort_nii = {
        "JPM": jpm_nii,
        "BAC": bac_nii,
        "BK": bk_nii,
        "COF": cof_nii,
        "SCHW": schw_nii,
    }
    peer_growths = {
        "JPM": 0.20, "BAC": 0.10, "BK": 0.05, "COF": 0.05, "SCHW": 0.05,
    }

    out = financial.compute_financial(
        "JPM",
        revenue_rows=[],
        gross_profit_rows=[],
        ocf_rows=[],
        peer_growths=peer_growths,
        sector="Financials",
        nii_rows=jpm_nii,
        noninterest_rows=[],
        pcl_rows=[],
        bank_cohort_nii_rows=bank_cohort_nii,
    )

    assert out["sector_path"] == "bank"
    assert out["components"]["nii_subscore"] >= 75.0


def test_bank_cohort_expansion_blk_skipped_when_no_nii() -> None:
    """BLK is in the cohort but is an asset manager — when its NII rows
    are missing, it should be silently excluded from the NII-percentile
    cohort (and the focal still scores cleanly off the remaining banks)."""
    jpm_nii = _eight_consecutive_quarters(
        2024, [1000, 1020, 1040, 1060, 1100, 1130, 1160, 1190]
    )
    bac_nii = _eight_consecutive_quarters(
        2024, [800, 820, 840, 860, 880, 900, 920, 940]
    )
    bank_cohort_nii = {
        "JPM": jpm_nii,
        "BAC": bac_nii,
        "BLK": [],
    }
    out = financial.compute_financial(
        "JPM",
        revenue_rows=[],
        gross_profit_rows=[],
        ocf_rows=[],
        peer_growths={"JPM": 0.10, "BAC": 0.05, "BLK": 0.07},
        sector="Financials",
        nii_rows=jpm_nii,
        noninterest_rows=[],
        pcl_rows=[],
        bank_cohort_nii_rows=bank_cohort_nii,
    )
    assert out["sector_path"] == "bank"
    assert out["components"]["nii_subscore"] >= 50.0


# ---------------------------------------------------------------------------
# SEC EDGAR fallback fetch functions (P3 audit fix-up, May 2026)
# ---------------------------------------------------------------------------


def test_sec_edgar_has_fallback_concept_helpers() -> None:
    """The three fallback fetch functions must exist on the module."""
    from lthcs.sources import sec_edgar
    assert callable(getattr(sec_edgar, "get_sales_revenue_gross_history", None))
    assert callable(getattr(sec_edgar, "get_cost_of_revenue_history", None))
    assert callable(getattr(sec_edgar, "get_operating_income_history", None))


# ---------------------------------------------------------------------------
# Bank-cohort revenue ranking on the *standard* and *legacy bank* paths
# (Tier-2 #15 fix, May 2026): a bank ticker that falls through to the
# standard path (no NII rows plumbed) -- or one that runs the legacy bank
# path without cohort dicts -- must still rank revenue growth bank-relative,
# not against the full universe (tech-megacap +60% would crush JPM +3%).
# ---------------------------------------------------------------------------


def test_bank_ticker_standard_path_ranks_revenue_within_bank_cohort() -> None:
    """JPM with no NII rows still routes revenue % rank through bank cohort.

    Without this fix, JPM's +3% revenue YoY would land in the bottom
    decile of a universe whose tail is NVDA-class +65%. With the cohort
    filter, JPM ranks against the other banks in peer_growths only.
    """
    revenue_rows = _annual_pair(2025, 103.0, 100.0)  # JPM +3%
    peer_growths = {
        "JPM": 0.03,
        "BAC": 0.02,
        "WFC": 0.01,
        "C": 0.00,
        # Universe outliers (would dominate without cohort filter).
        "NVDA": 0.65,
        "AMD": 0.45,
        "META": 0.30,
        "GOOGL": 0.18,
    }

    out = financial.compute_financial(
        "JPM",
        revenue_rows=revenue_rows,
        gross_profit_rows=[],
        ocf_rows=[],
        peer_growths=peer_growths,
        sector="Financials",
        # No nii_rows -> standard path with bank-cohort revenue fallback.
    )

    # Should NOT route through the bank pillar (no NII).
    assert out.get("sector_path") != "bank"
    # JPM 0.03 vs BAC 0.02 / WFC 0.01 / C 0.00 -> ranks above all 3 bank
    # peers -> 100. Universe-wide it'd be ~12% (1 of 8 peers below it).
    assert out["components"]["revenue_subscore"] == pytest.approx(100.0)
    # Strategy tag surfaces the bank-cohort fallback.
    assert out["components"].get("peer_cohort_strategy") == "bank_cohort"


def test_bank_ticker_standard_path_revenue_bottom_of_bank_cohort() -> None:
    """A bank with the *worst* growth in its cohort still scores 0 against
    banks (not against the universe). Sanity check that the cohort filter
    is *real*, not just clamping at 100."""
    revenue_rows = _annual_pair(2025, 100.0, 100.0)  # JPM 0% YoY
    peer_growths = {
        "JPM": 0.00,
        "BAC": 0.04,
        "WFC": 0.03,
        "C": 0.02,
        # Tech outliers would dominate without cohort filter.
        "NVDA": 0.65,
        "AAPL": 0.10,
    }

    out = financial.compute_financial(
        "JPM",
        revenue_rows=revenue_rows,
        gross_profit_rows=[],
        ocf_rows=[],
        peer_growths=peer_growths,
        sector="Financials",
    )

    assert out.get("sector_path") != "bank"
    # JPM 0.00 vs BAC 0.04 / WFC 0.03 / C 0.02 -> bottom of bank cohort
    # -> 0. Universe-wide it'd be ~33% (2 peers below or equal).
    assert out["components"]["revenue_subscore"] == pytest.approx(0.0)


def test_non_bank_ticker_standard_path_unchanged_by_bank_peers() -> None:
    """Control: NVDA's revenue percentile uses the full universe (banks
    + tech), not a bank cohort. Banks in peer_growths must NOT shift its
    ranking."""
    revenue_annual = _annual_pair(2025, 165.0, 100.0)  # NVDA +65%
    qd = _quarter_dates_for_year(2025)
    revenue_quarters = [_q_row(q["start"], q["end"], 1000.0) for q in qd]
    revenue_rows = revenue_annual + revenue_quarters
    gp_rows = [_q_row(q["start"], q["end"], 300.0) for q in qd]
    ocf_rows = [_q_row(q["start"], q["end"], 200.0) for q in qd]

    # Baseline: universe with mixed banks + tech.
    peer_growths = {
        "NVDA": 0.65,
        "AAPL": 0.10,
        "MSFT": 0.12,
        "JPM": 0.03,
        "BAC": 0.02,
        "WFC": 0.01,
    }

    out = financial.compute_financial(
        "NVDA",
        revenue_rows,
        gp_rows,
        ocf_rows,
        peer_growths,
        sector="Technology",
    )

    assert out.get("sector_path") != "bank"
    # NVDA tops the universe -> revenue_subscore = 100. Bank cohort
    # filtering is gated on is_bank_ticker(NVDA), which is False.
    assert out["components"]["revenue_subscore"] == pytest.approx(100.0)
    # Cohort strategy should NOT be bank_cohort (NVDA isn't a bank).
    assert out["components"].get("peer_cohort_strategy") != "bank_cohort"


def test_bank_ticker_standard_path_falls_back_when_no_bank_peers() -> None:
    """If JPM is the *only* bank in peer_growths, the cohort filter
    yields zero peers -- fall back to the full universe rather than
    returning a degenerate ranking."""
    revenue_rows = _annual_pair(2025, 110.0, 100.0)  # JPM +10%
    peer_growths = {
        "JPM": 0.10,
        "AAPL": 0.20,
        "MSFT": 0.15,
        # No other banks.
    }

    out = financial.compute_financial(
        "JPM",
        revenue_rows=revenue_rows,
        gross_profit_rows=[],
        ocf_rows=[],
        peer_growths=peer_growths,
        sector="Financials",
    )

    assert out.get("sector_path") != "bank"
    # No bank peers -> universe-wide rank. JPM 0.10 vs AAPL 0.20 / MSFT
    # 0.15 -> bottom -> 0. Strategy tag should NOT claim bank_cohort
    # since the fallback kicked in.
    assert out["components"]["revenue_subscore"] == pytest.approx(0.0)
    assert out["components"].get("peer_cohort_strategy") != "bank_cohort"


def test_bank_legacy_path_no_cohort_dicts_ranks_within_bank_universe() -> None:
    """Legacy bank path (has NII but no cohort dicts plumbed) now also
    filters peer_growths to BANK_TICKERS for the NII-growth subscore.

    Without this, an older caller that fetched NII but skipped the
    cohort dicts would rank JPM NII growth vs universe revenue growth
    -- the very bug Tier-2 #15 calls out."""
    # JPM NII +5% YoY (4 prior + 4 current quarters).
    jpm_nii = _eight_consecutive_quarters(
        2024, [1000, 1000, 1000, 1000, 1050, 1050, 1050, 1050]
    )
    # peer_growths with bank peers + tech outliers.
    peer_growths = {
        "JPM": 0.05,
        "BAC": 0.02,
        "WFC": 0.01,
        # Universe outliers.
        "NVDA": 0.65,
        "AAPL": 0.20,
        "MSFT": 0.15,
    }

    out = financial.compute_financial(
        "JPM",
        revenue_rows=[],
        gross_profit_rows=[],
        ocf_rows=[],
        peer_growths=peer_growths,
        sector="Financials",
        nii_rows=jpm_nii,
        # No bank cohort dicts -> legacy path.
    )

    assert out["sector_path"] == "bank"
    assert out["data_quality"]["is_bank_cohort"] is False
    # JPM NII growth 0.05 ranked vs banks only (BAC 0.02, WFC 0.01).
    # -> tops the bank cohort -> 100.
    assert out["components"]["revenue_subscore"] == pytest.approx(100.0)


def test_bank_legacy_path_no_cohort_dicts_falls_back_to_universe_if_no_bank_peers() -> None:
    """Legacy bank path with NO bank peers in peer_growths must fall back
    to the unfiltered universe rather than yielding an empty distribution."""
    jpm_nii = _eight_consecutive_quarters(
        2024, [1000, 1000, 1000, 1000, 1050, 1050, 1050, 1050]
    )
    peer_growths = {
        "JPM": 0.05,
        "AAPL": 0.20,
        "MSFT": 0.15,
    }

    out = financial.compute_financial(
        "JPM",
        revenue_rows=[],
        gross_profit_rows=[],
        ocf_rows=[],
        peer_growths=peer_growths,
        sector="Financials",
        nii_rows=jpm_nii,
    )

    assert out["sector_path"] == "bank"
    # JPM 0.05 vs AAPL 0.20 / MSFT 0.15 -> 0% (bottom of universe).
    # Fallback kicked in because no other bank peers exist.
    assert out["components"]["revenue_subscore"] == pytest.approx(0.0)
