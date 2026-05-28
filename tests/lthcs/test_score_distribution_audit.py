"""Tests for ``scripts/lthcs_score_distribution_audit``.

We exercise the audit script's math on synthetic universes with known
distributions so the assertions don't depend on the current real
snapshot. These tests intentionally bypass the script's file I/O and
hit the pure helpers directly.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "lthcs_score_distribution_audit.py"


def _load_audit_module():
    """Import the audit script as a module without it being on sys.path."""
    spec = importlib.util.spec_from_file_location(
        "lthcs_score_distribution_audit", SCRIPT_PATH
    )
    assert spec and spec.loader, "could not build module spec for audit script"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


audit = _load_audit_module()


# ---------------------------------------------------------------------------
# Synthetic universe builders
# ---------------------------------------------------------------------------

def _row(
    ticker: str,
    composite: float,
    pillars: Dict[str, float],
    maturity: str = "mature_compounder",
    drift_30d: float = 0.0,
    flags: List[str] | None = None,
    band: str = "weakening",
) -> Dict[str, Any]:
    return {
        "ticker": ticker,
        "lthcs_score": float(composite),
        "band": band,
        "drift_30d": float(drift_30d),
        "drift_90d": 0.0,
        "confidence_level": "high",
        "data_quality_flags": list(flags or []),
        "subscores": dict(pillars),
        "maturity_stage": maturity,
        "sector": "Technology",
    }


def _uniform_universe(n: int = 20) -> List[Dict[str, Any]]:
    """Composite scores evenly spaced 10..95 inclusive."""
    out: List[Dict[str, Any]] = []
    if n < 2:
        n = 2
    step = (95.0 - 10.0) / (n - 1)
    for i in range(n):
        s = round(10.0 + step * i, 2)
        out.append(_row(
            ticker=f"T{i:02d}",
            composite=s,
            pillars={p: s for p in audit.PILLAR_ORDER},
        ))
    return out


# ---------------------------------------------------------------------------
# percentile / summary_stats
# ---------------------------------------------------------------------------

def test_percentile_matches_known_values():
    # Standard sample with known quartiles.
    sample = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
    assert audit.percentile(sample, 50.0) == pytest.approx(55.0)
    assert audit.percentile(sample, 0.0) == 10
    assert audit.percentile(sample, 100.0) == 100
    # p25 with linear interp: rank = 0.25*9 = 2.25 → 30 + 0.25*(40-30) = 32.5
    assert audit.percentile(sample, 25.0) == pytest.approx(32.5)


def test_percentile_filters_nones_and_nans():
    sample = [1.0, 2.0, float("nan"), None, 3.0]
    assert audit.percentile(sample, 50.0) == pytest.approx(2.0)


def test_summary_stats_uniform():
    s = audit.summary_stats(list(range(0, 101, 10)))  # 0,10,..,100  (n=11)
    assert s["count"] == 11
    assert s["mean"] == pytest.approx(50.0)
    assert s["min"] == 0
    assert s["max"] == 100
    # Population stdev of {0,10,...,100} (n=11): variance = sum((x-50)^2)/11
    # = (2500+1600+900+400+100+0+100+400+900+1600+2500)/11 = 11000/11 = 1000
    # → stdev = sqrt(1000) ≈ 31.62.
    assert s["stdev"] == pytest.approx(31.62, abs=0.02)


def test_summary_stats_empty():
    s = audit.summary_stats([])
    assert s["count"] == 0
    assert s["mean"] is None
    assert s["stdev"] is None


# ---------------------------------------------------------------------------
# histogram + band_counts
# ---------------------------------------------------------------------------

def test_histogram_bins_full_range_inclusive_at_100():
    values = [0, 9, 10, 19, 99, 100, 100]
    hist = audit.histogram(values)
    # Bin 0..9 should have 2 (0, 9), bin 10..19 should have 2 (10, 19),
    # bin 90..100 (last bin) should have 3 (99, 100, 100).
    counts = {b: c for b, c in hist}
    assert counts[(0, 9)] == 2
    assert counts[(10, 19)] == 2
    assert counts[(90, 100)] == 3
    # All other bins zero.
    for (lo, hi), c in hist:
        if (lo, hi) not in {(0, 9), (10, 19), (90, 100)}:
            assert c == 0


def test_histogram_clamps_out_of_range():
    # Values >100 should land in the last bin, <0 in the first.
    hist = audit.histogram([-5, 150])
    counts = {b: c for b, c in hist}
    assert counts[(0, 9)] == 1
    assert counts[(90, 100)] == 1


def test_band_counts_lines_up_with_documented_thresholds():
    scores = [25, 49, 50, 59, 60, 69, 70, 79, 80, 84, 85, 100]
    bands = audit.band_counts(scores)
    by_name = {name: count for name, _, _, count in bands}
    assert by_name["review"] == 2  # 25, 49
    assert by_name["weakening"] == 2  # 50, 59
    assert by_name["monitor"] == 2  # 60, 69
    assert by_name["constructive"] == 2  # 70, 79
    assert by_name["high_confidence"] == 2  # 80, 84
    assert by_name["elite"] == 2  # 85, 100


def test_band_counts_handle_fractional_scores_via_floor():
    # 79.4 should floor to 79 → constructive; 80.0 → high_confidence.
    bands = audit.band_counts([79.4, 80.0, 84.9, 85.0])
    by_name = {name: count for name, _, _, count in bands}
    assert by_name["constructive"] == 1
    assert by_name["high_confidence"] == 2  # 80.0 and 84.9
    assert by_name["elite"] == 1


# ---------------------------------------------------------------------------
# per_cohort_distribution + top/bottom
# ---------------------------------------------------------------------------

def test_per_cohort_distribution_splits_by_maturity():
    universe = [
        _row("A", 80.0, {p: 80 for p in audit.PILLAR_ORDER}, maturity="growth_compounder"),
        _row("B", 90.0, {p: 90 for p in audit.PILLAR_ORDER}, maturity="growth_compounder"),
        _row("C", 30.0, {p: 30 for p in audit.PILLAR_ORDER}, maturity="mature_compounder"),
        _row("D", 40.0, {p: 40 for p in audit.PILLAR_ORDER}, maturity="mature_compounder"),
    ]
    out = audit.per_cohort_distribution(universe)
    assert set(out.keys()) == {"growth_compounder", "mature_compounder"}
    assert out["growth_compounder"]["summary"]["mean"] == pytest.approx(85.0)
    assert out["mature_compounder"]["summary"]["mean"] == pytest.approx(35.0)


def test_top_bottom_returns_correct_rows():
    universe = _uniform_universe(n=10)
    top, bot = audit.top_bottom_n(universe, n=3)
    assert [r["ticker"] for r in top] == ["T09", "T08", "T07"]
    assert [r["ticker"] for r in bot] == ["T00", "T01", "T02"]


# ---------------------------------------------------------------------------
# Pillar z-score outliers
# ---------------------------------------------------------------------------

def test_pillar_zscore_outliers_detects_planted_outlier():
    # 10 tickers in a cohort with adoption_momentum=50 except one at 100.
    cohort_rows: List[Dict[str, Any]] = []
    for i in range(9):
        cohort_rows.append(_row(
            ticker=f"T{i}",
            composite=50.0,
            pillars={p: 50.0 for p in audit.PILLAR_ORDER},
            maturity="mature_compounder",
        ))
    cohort_rows.append(_row(
        ticker="WILD",
        composite=70.0,
        pillars={**{p: 50.0 for p in audit.PILLAR_ORDER}, "adoption_momentum": 100.0},
        maturity="mature_compounder",
    ))
    outliers = audit.pillar_zscore_outliers(cohort_rows, threshold=2.0, top_n=5)
    assert any(o["ticker"] == "WILD" and o["pillar"] == "adoption_momentum" for o in outliers)
    # Verify the z computation is sane: with 9 fifties and one 100, mean=55, sd~15
    wild = next(o for o in outliers if o["ticker"] == "WILD")
    assert wild["z"] > 2.0


def test_pillar_zscore_outliers_skips_small_cohorts():
    rows = [
        _row("A", 50, {p: 50 for p in audit.PILLAR_ORDER}, maturity="recovery_rerating"),
        _row("B", 90, {**{p: 50 for p in audit.PILLAR_ORDER}, "adoption_momentum": 100}, maturity="recovery_rerating"),
    ]
    # Cohort size <3 AND universe-wide sample is also <3 → no outliers
    # reported even though sample is wild. (Universe-wide fallback also
    # requires at least 3 samples to compute a stdev.)
    assert audit.pillar_zscore_outliers(rows, threshold=2.0) == []


# ---------------------------------------------------------------------------
# Per-sector DES outlier z-scoring (Phase 3 hotfix)
# ---------------------------------------------------------------------------

def _row_with_sector(
    ticker: str,
    pillars: Dict[str, float],
    sector: str,
    maturity: str = "mature_compounder",
    composite: float = 50.0,
) -> Dict[str, Any]:
    """Build a row with a custom sector. Wraps ``_row`` and overrides ``sector``."""
    row = _row(
        ticker=ticker,
        composite=composite,
        pillars=pillars,
        maturity=maturity,
    )
    row["sector"] = sector
    return row


def test_des_outlier_uses_sector_not_cohort_grouping():
    # Two tickers in the SAME sector (Financials) but DIFFERENT cohorts.
    # If grouping were per-cohort, the two financials would be split into
    # two different buckets (each of size 1) and skipped. With per-sector
    # grouping for DES they share a Financials bucket (size 2) — still
    # below the 3-min so we add 1 more Financials ticker to make a real
    # sector bucket, and plant the outlier among them.
    rows = []
    # 5 Technology tickers, DES = 50 (sector baseline).
    for i in range(5):
        rows.append(_row_with_sector(
            f"TECH{i}",
            {**{p: 50.0 for p in audit.PILLAR_ORDER}, "des": 50.0},
            sector="Technology",
            maturity="mature_compounder" if i < 3 else "standard_compounder",
        ))
    # 5 Financials tickers, DES = 70 (sector baseline — much higher than Tech).
    # If we grouped by maturity_stage, these 5 financials + 5 tech in
    # standard_compounder/mature_compounder would mix, and the financials
    # would look like outliers vs the tech-dominated cohort.
    for i in range(4):
        rows.append(_row_with_sector(
            f"FIN{i}",
            {**{p: 50.0 for p in audit.PILLAR_ORDER}, "des": 70.0},
            sector="Financials",
            # Spread across cohorts on purpose:
            maturity="mature_compounder" if i < 2 else "standard_compounder",
        ))
    # The 5th financial is a true within-sector outlier (DES = 95).
    rows.append(_row_with_sector(
        "FIN_WILD",
        {**{p: 50.0 for p in audit.PILLAR_ORDER}, "des": 95.0},
        sector="Financials",
        maturity="standard_compounder",
    ))

    outliers = audit.pillar_zscore_outliers(rows, threshold=2.0, top_n=10)
    des_outliers = [o for o in outliers if o["pillar"] == "des"]

    # The 4 "baseline" financials should NOT show up as DES outliers —
    # they're at-the-mean within their sector (Financials). Only FIN_WILD
    # is a true within-sector outlier.
    baseline_fin_outliers = [o for o in des_outliers if o["ticker"].startswith("FIN") and o["ticker"] != "FIN_WILD"]
    assert baseline_fin_outliers == [], (
        f"baseline Financials should NOT be flagged as DES outliers "
        f"under per-sector grouping; got: {baseline_fin_outliers}"
    )
    # And the planted outlier should show up with bucket = Financials.
    fin_wild = [o for o in des_outliers if o["ticker"] == "FIN_WILD"]
    assert len(fin_wild) == 1
    assert fin_wild[0]["cohort"] == "Financials"


def test_des_singleton_sector_falls_back_to_universe():
    # Singleton-sector ticker should still be evaluated against the
    # universe-wide DES sample, not silently dropped.
    rows = []
    # 9 Tech tickers, DES = 50.
    for i in range(9):
        rows.append(_row_with_sector(
            f"T{i}",
            {**{p: 50.0 for p in audit.PILLAR_ORDER}, "des": 50.0},
            sector="Technology",
        ))
    # 1 Real Estate ticker with DES wildly out of step.
    rows.append(_row_with_sector(
        "LONELY",
        {**{p: 50.0 for p in audit.PILLAR_ORDER}, "des": 100.0},
        sector="Real Estate",
    ))

    outliers = audit.pillar_zscore_outliers(rows, threshold=2.0, top_n=10)
    lonely = [o for o in outliers if o["ticker"] == "LONELY" and o["pillar"] == "des"]
    assert len(lonely) == 1, "singleton-sector ticker should fall back to universe-wide"
    # The cohort field should record the universe-wide fallback path.
    assert lonely[0]["cohort"] == "_universe"


def test_financials_no_longer_cluster_in_des_top10():
    # Reproduces the failure mode FF's audit surfaced: 6+ Financials
    # tickers all flagged as DES outliers because the standard_compounder
    # cohort means is dragged down by non-Financials. Under per-sector
    # grouping, financials are evaluated against other financials — so
    # the cluster bias should disappear.
    rows = []
    # 50 standard_compounder tickers spread across sectors with DES=45.
    non_fin_sectors = ["Technology", "Health Care", "Industrials", "Consumer Discretionary"]
    for i in range(50):
        rows.append(_row_with_sector(
            f"NF{i}",
            {**{p: 50.0 for p in audit.PILLAR_ORDER}, "des": 45.0},
            sector=non_fin_sectors[i % len(non_fin_sectors)],
            maturity="standard_compounder",
        ))
    # 6 financials with DES=71.6 (matches the snapshot we're fixing).
    for tk in ("BK", "BLK", "COF", "MET", "SCHW", "USB"):
        rows.append(_row_with_sector(
            tk,
            {**{p: 50.0 for p in audit.PILLAR_ORDER}, "des": 71.6},
            sector="Financials",
            maturity="standard_compounder",
        ))

    outliers = audit.pillar_zscore_outliers(rows, threshold=2.0, top_n=10)
    des_outliers = [o for o in outliers if o["pillar"] == "des"]
    fin_tickers = {"BK", "BLK", "COF", "MET", "SCHW", "USB"}
    fin_in_des_top10 = [o["ticker"] for o in des_outliers if o["ticker"] in fin_tickers]
    # Under per-sector grouping, all 6 financials sit at the same DES=71.6
    # which IS their sector mean — stdev=0 and no outliers flagged.
    # (The pre-fix behaviour would have flagged all 6 as |z|=2.99 outliers.)
    assert len(fin_in_des_top10) == 0, (
        f"under per-sector grouping, financials at-the-sector-mean should not "
        f"cluster as DES outliers; got: {fin_in_des_top10}"
    )


# ---------------------------------------------------------------------------
# Stuck tickers
# ---------------------------------------------------------------------------

def test_stuck_tickers_filters_by_drift_threshold():
    rows = [
        _row("STUCK", 50, {p: 50 for p in audit.PILLAR_ORDER}, drift_30d=0.5),
        _row("MOVED", 60, {p: 60 for p in audit.PILLAR_ORDER}, drift_30d=10.0),
        _row("EDGE", 70, {p: 70 for p in audit.PILLAR_ORDER}, drift_30d=4.9),
    ]
    stuck = audit.stuck_tickers(rows, drift_threshold=5.0)
    names = [s["ticker"] for s in stuck]
    assert "STUCK" in names
    assert "EDGE" in names
    assert "MOVED" not in names
    # Most-stuck first.
    assert stuck[0]["ticker"] == "STUCK"


# ---------------------------------------------------------------------------
# Correlation
# ---------------------------------------------------------------------------

def test_pearson_corr_matches_manual_value():
    xs = [1.0, 2.0, 3.0, 4.0, 5.0]
    ys = [2.0, 4.0, 6.0, 8.0, 10.0]
    assert audit.pearson_corr(xs, ys) == pytest.approx(1.0)
    assert audit.pearson_corr(xs, list(reversed(ys))) == pytest.approx(-1.0)


def test_pearson_corr_handles_zero_variance():
    assert audit.pearson_corr([5.0] * 5, [1, 2, 3, 4, 5]) is None


def test_pillar_correlation_matrix_identifies_perfect_redundancy():
    # adoption_momentum and financial_evolution made perfectly correlated.
    rows: List[Dict[str, Any]] = []
    for i in range(20):
        s = 30 + i * 3
        rows.append(_row(
            ticker=f"T{i:02d}",
            composite=s,
            pillars={
                "adoption_momentum": float(s),
                "institutional_confidence": float(50 + (i % 5)),
                "financial_evolution": float(s),  # perfectly tracks adoption
                "thesis_integrity": float(40 + (i % 7)),
                "des": float(45 + (i % 3)),
            },
        ))
    corr = audit.pillar_correlation_matrix(rows)
    assert corr[("adoption_momentum", "financial_evolution")] == pytest.approx(1.0)
    classes = audit.classify_pillar_pairs(corr, high_thresh=0.7, low_thresh=0.2)
    redundant_pairs = {tuple(sorted((a, b))) for a, b, _ in classes["redundant"]}
    assert ("adoption_momentum", "financial_evolution") in redundant_pairs


def test_pillar_correlation_skips_rows_with_missing_pillars():
    rows = [
        _row("A", 50, {p: 50 for p in audit.PILLAR_ORDER}),
        _row("B", 60, {p: 60 for p in audit.PILLAR_ORDER}),
        # Row C is missing 'des'.
        {
            "ticker": "C",
            "lthcs_score": 70.0,
            "subscores": {p: 70 for p in audit.PILLAR_ORDER if p != "des"},
            "maturity_stage": "mature_compounder",
        },
    ]
    corr = audit.pillar_correlation_matrix(rows)
    # Should have used only A and B → perfect +1 across all pairs (constant slope).
    assert corr[("adoption_momentum", "financial_evolution")] == pytest.approx(1.0)


def test_classify_pillar_pairs_buckets_correctly():
    # Build a fake correlation dict.
    corr = {
        ("a", "a"): 1.0,
        ("b", "b"): 1.0,
        ("c", "c"): 1.0,
        ("a", "b"): 0.85,
        ("b", "a"): 0.85,
        ("a", "c"): 0.15,
        ("c", "a"): 0.15,
        ("b", "c"): 0.50,
        ("c", "b"): 0.50,
    }
    cls = audit.classify_pillar_pairs(corr, high_thresh=0.7, low_thresh=0.2)
    redundant_pairs = {tuple(sorted((a, b))) for a, b, _ in cls["redundant"]}
    orthogonal_pairs = {tuple(sorted((a, b))) for a, b, _ in cls["orthogonal"]}
    assert ("a", "b") in redundant_pairs
    assert ("a", "c") in orthogonal_pairs
    # (b, c) is neither redundant nor orthogonal.
    assert ("b", "c") not in redundant_pairs
    assert ("b", "c") not in orthogonal_pairs


# ---------------------------------------------------------------------------
# ASCII rendering / report assembly smoke tests
# ---------------------------------------------------------------------------

def test_ascii_histogram_renders_each_bin_line():
    hist = audit.histogram([5, 5, 15, 50, 50, 99])
    rendered = audit.ascii_histogram(hist)
    assert "  0-9" in rendered
    # Last bin is 90-100 to make 100 land in it.
    assert " 90-100" in rendered
    # The bin that holds the maximum count should have the widest bar.
    lines = rendered.splitlines()
    bar_lengths = [(line.split("|", 1)[1].count("#"), line) for line in lines]
    assert max(bar_lengths)[0] > 0


def test_render_distribution_report_contains_required_sections():
    universe = _uniform_universe(n=20)
    text = audit.render_distribution_report("2026-05-18", "2026-05-19", universe)
    assert "## Distribution summary" in text
    assert "## Histogram (10-point bins)" in text
    assert "## Band cohorts vs documented thresholds" in text
    assert "## Per-cohort distribution" in text
    assert "## Top 5 / bottom 5 by composite" in text
    assert "## Pillar-vs-peer-group z-score outliers" in text
    assert "## Stuck tickers" in text


def test_render_correlation_report_includes_matrix_and_classification():
    universe = _uniform_universe(n=20)
    text = audit.render_correlation_report("2026-05-18", "2026-05-19", universe, stability_window_dates=[])
    assert "## 5x5 Pearson correlation matrix" in text
    assert "## Near-redundant pillar pairs" in text
    assert "## Near-orthogonal pillar pairs" in text


# ---------------------------------------------------------------------------
# extract_scores accepts both envelopes
# ---------------------------------------------------------------------------

def test_extract_scores_envelope_dict_and_list():
    inner = [_row("X", 50, {p: 50 for p in audit.PILLAR_ORDER})]
    assert audit.extract_scores({"scores": inner, "calc_date": "2026-05-18"}) == inner
    assert audit.extract_scores(inner) == inner
    assert audit.extract_scores({}) == []
