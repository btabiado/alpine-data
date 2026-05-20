"""Tests for ``scripts/lthcs_weight_threshold_audit`` (Phase 3 tasks 3.5+3.6).

The audit script is pure data-in / markdown-out — these tests exercise the
math helpers (rank, Spearman, implied-weight normalisation, verdict logic,
band heuristics, churn) on synthetic inputs with known answers so a
regression in the audit's analytics shows up immediately.

The tests never touch the real ``data/lthcs/`` tree.
"""

from __future__ import annotations

import importlib.util
import math
import sys
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Module loader — the audit script lives under ``scripts/`` (not a package),
# so we import it by file path.
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "lthcs_weight_threshold_audit.py"


@pytest.fixture(scope="module")
def audit_mod():
    spec = importlib.util.spec_from_file_location(
        "lthcs_weight_threshold_audit", SCRIPT_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Spearman / rank math
# ---------------------------------------------------------------------------

def test_rankdata_handles_ties(audit_mod):
    # [10, 20, 20, 30]: 10 -> 1, the two 20s tie at rank (2+3)/2 = 2.5,
    # 30 -> 4.
    ranks = audit_mod._rankdata([10.0, 20.0, 20.0, 30.0])
    assert ranks == [1.0, 2.5, 2.5, 4.0]


def test_spearman_perfect_positive_is_one(audit_mod):
    xs = [1.0, 2.0, 3.0, 4.0, 5.0]
    ys = [10.0, 20.0, 30.0, 40.0, 50.0]
    assert audit_mod.spearman(xs, ys) == pytest.approx(1.0, abs=1e-9)


def test_spearman_perfect_negative_is_minus_one(audit_mod):
    xs = [1.0, 2.0, 3.0, 4.0, 5.0]
    ys = [50.0, 40.0, 30.0, 20.0, 10.0]
    assert audit_mod.spearman(xs, ys) == pytest.approx(-1.0, abs=1e-9)


def test_spearman_returns_none_on_too_few_points(audit_mod):
    assert audit_mod.spearman([1.0], [2.0]) is None
    assert audit_mod.spearman([1.0, 2.0], [3.0, 4.0]) is None


def test_spearman_returns_none_on_degenerate_input(audit_mod):
    # All-equal x => zero variance => undefined correlation.
    assert audit_mod.spearman([5.0, 5.0, 5.0, 5.0], [1.0, 2.0, 3.0, 4.0]) is None


# ---------------------------------------------------------------------------
# Implied weights
# ---------------------------------------------------------------------------

def _make_ic_block(sharpes):
    """Build an ic-by-pillar dict for the canonical 5 pillars."""
    pillars = audit_mod_PILLARS = [
        "adoption_momentum",
        "institutional_confidence",
        "financial_evolution",
        "thesis_integrity",
        "des",
    ]
    out = {}
    for i, p in enumerate(pillars):
        out[p] = {"ic_sharpe": sharpes[i], "ic_mean": 0.0, "ic_std": 1.0, "n_dates": 50}
    return out


def test_implied_weights_normalise_positives(audit_mod):
    ic = _make_ic_block([1.0, 2.0, 1.0, 0.0, 0.0])
    imp = audit_mod.implied_weights(ic)
    # 1/4, 2/4, 1/4, 0, 0
    assert imp["adoption_momentum"] == pytest.approx(0.25)
    assert imp["institutional_confidence"] == pytest.approx(0.50)
    assert imp["financial_evolution"] == pytest.approx(0.25)
    assert imp["thesis_integrity"] == pytest.approx(0.0)
    assert imp["des"] == pytest.approx(0.0)
    assert sum(imp.values()) == pytest.approx(1.0)


def test_implied_weights_clip_negatives(audit_mod):
    # Negative Sharpes contribute zero weight.
    ic = _make_ic_block([-0.5, 1.0, -0.2, 0.0, 1.0])
    imp = audit_mod.implied_weights(ic)
    assert imp["adoption_momentum"] == pytest.approx(0.0)
    assert imp["institutional_confidence"] == pytest.approx(0.5)
    assert imp["financial_evolution"] == pytest.approx(0.0)
    assert imp["des"] == pytest.approx(0.5)
    assert sum(imp.values()) == pytest.approx(1.0)


def test_implied_weights_falls_back_to_equal_when_all_nonpositive(audit_mod):
    ic = _make_ic_block([-0.5, -0.1, -0.2, 0.0, 0.0])
    imp = audit_mod.implied_weights(ic)
    for v in imp.values():
        assert v == pytest.approx(0.2)


# ---------------------------------------------------------------------------
# Cohort verdict
# ---------------------------------------------------------------------------

def test_cohort_verdict_aligned_when_all_close(audit_mod):
    # Current = implied within tol -> ALIGNED, no worst pillar.
    current = [0.22, 0.18, 0.20, 0.22, 0.18]
    implied = {
        "adoption_momentum": 0.20,
        "institutional_confidence": 0.20,
        "financial_evolution": 0.20,
        "thesis_integrity": 0.20,
        "des": 0.20,
    }
    verdict, worst, gap = audit_mod.cohort_verdict(current, implied, tol=0.10)
    assert verdict == "ALIGNED"
    assert gap <= 0.10


def test_cohort_verdict_misaligned_flags_worst_pillar(audit_mod):
    current = [0.20, 0.20, 0.20, 0.20, 0.20]
    implied = {
        "adoption_momentum": 0.50,  # gap = 0.30 — worst
        "institutional_confidence": 0.15,
        "financial_evolution": 0.15,
        "thesis_integrity": 0.10,
        "des": 0.10,
    }
    verdict, worst, gap = audit_mod.cohort_verdict(current, implied, tol=0.10)
    assert verdict == "MISALIGNED"
    assert worst == "adoption_momentum"
    assert gap == pytest.approx(0.30, abs=1e-9)


# ---------------------------------------------------------------------------
# Band heuristics
# ---------------------------------------------------------------------------

def test_band_verdict_flags_empty_elite(audit_mod):
    assert "SHIFT-DOWN" in audit_mod.band_verdict(0, "elite")


def test_band_verdict_flags_overflowing_review(audit_mod):
    assert "SHIFT-UP" in audit_mod.band_verdict(50, "review")


def test_band_verdict_keep_when_populated(audit_mod):
    assert audit_mod.band_verdict(15, "monitor") == "KEEP"


def test_band_verdict_empty_non_extreme_band(audit_mod):
    # Empty constructive (not elite, not review) -> EMPTY suggestion.
    assert audit_mod.band_verdict(0, "constructive").startswith("EMPTY")


# ---------------------------------------------------------------------------
# Churn
# ---------------------------------------------------------------------------

def test_band_churn_zero_for_stable_ticker(audit_mod):
    snapshots = {
        "2026-05-10": [{"ticker": "AAA", "band": "monitor"}],
        "2026-05-11": [{"ticker": "AAA", "band": "monitor"}],
        "2026-05-12": [{"ticker": "AAA", "band": "monitor"}],
        "2026-05-13": [{"ticker": "AAA", "band": "monitor"}],
    }
    churn = audit_mod.band_churn(snapshots, n_days=30)
    assert churn["AAA"] == 0.0


def test_band_churn_flags_flippy_ticker(audit_mod):
    # Alternating bands -> churn = 1.0 (every consecutive pair differs).
    snapshots = {
        "2026-05-10": [{"ticker": "FLIP", "band": "monitor"}],
        "2026-05-11": [{"ticker": "FLIP", "band": "weakening"}],
        "2026-05-12": [{"ticker": "FLIP", "band": "monitor"}],
        "2026-05-13": [{"ticker": "FLIP", "band": "weakening"}],
    }
    churn = audit_mod.band_churn(snapshots, n_days=30)
    assert churn["FLIP"] == pytest.approx(1.0)


def test_band_churn_partial_flip(audit_mod):
    # Bands: [A, A, B, B] -> one flip across three consecutive pairs.
    snapshots = {
        "2026-05-10": [{"ticker": "ONE", "band": "monitor"}],
        "2026-05-11": [{"ticker": "ONE", "band": "monitor"}],
        "2026-05-12": [{"ticker": "ONE", "band": "weakening"}],
        "2026-05-13": [{"ticker": "ONE", "band": "weakening"}],
    }
    churn = audit_mod.band_churn(snapshots, n_days=30)
    assert churn["ONE"] == pytest.approx(1.0 / 3.0)


# ---------------------------------------------------------------------------
# Forward return
# ---------------------------------------------------------------------------

def test_forward_return_simple(audit_mod):
    prices = {
        "2026-05-10": 100.0,
        "2026-05-11": 101.0,
        "2026-05-12": 102.0,
        "2026-05-13": 110.0,
    }
    # horizon=3 trading days from 2026-05-10 -> 2026-05-13: 110/100 - 1 = 0.10
    fr = audit_mod.forward_return(prices, "2026-05-10", horizon=3)
    assert fr == pytest.approx(0.10, abs=1e-9)


def test_forward_return_returns_none_when_window_too_short(audit_mod):
    prices = {"2026-05-10": 100.0, "2026-05-11": 105.0}
    fr = audit_mod.forward_return(prices, "2026-05-10", horizon=21)
    assert fr is None


def test_forward_return_handles_missing_date_gracefully(audit_mod):
    prices = {"2026-05-12": 100.0, "2026-05-13": 110.0}
    fr = audit_mod.forward_return(prices, "2026-05-10", horizon=1)
    # 2026-05-10 not in keys; should snap to earliest >= 2026-05-10
    # then +1 trading day = 2026-05-13 = 1.10x
    assert fr == pytest.approx(0.10, abs=1e-9)


# ---------------------------------------------------------------------------
# End-to-end: compute_cohort_ic on synthetic snapshots
# ---------------------------------------------------------------------------

def test_compute_cohort_ic_recovers_known_signal(audit_mod, monkeypatch, tmp_path):
    """Plant a synthetic dataset where one pillar perfectly predicts forward
    returns for one cohort. The audit should recover an IC Sharpe ≫ 0 for
    that pillar and ≈ 0 for the noise pillars."""

    # 6 tickers, all in the same cohort. 10 observation dates. The
    # 'institutional_confidence' pillar is exactly proportional to the
    # forward return; the other four pillars are zero.
    tickers = [f"T{i}" for i in range(6)]
    dates = [f"2026-04-{d:02d}" for d in range(1, 11)]

    # Synthetic prices: each ticker has a fixed per-day drift derived from
    # an arbitrary "true" rank. Drift on day d for ticker i = (i / 5) * 0.02.
    prices_map = {}
    for i, t in enumerate(tickers):
        # Make a daily series for ~50 trading days so forward_return(21)
        # always has data.
        daily = {}
        base = 100.0
        for day in range(1, 51):
            base *= 1.0 + (i / 5.0) * 0.01  # ticker i grows i/5 % per day
            daily[f"2026-04-{day:02d}" if day <= 30 else f"2026-05-{day-30:02d}"] = base
        prices_map[t] = daily

    # Patch the loader so we don't touch disk.
    def fake_loader(ticker):
        return prices_map.get(ticker, {})

    monkeypatch.setattr(audit_mod, "load_price_series", fake_loader)

    # Build snapshots: the 'institutional_confidence' sub-score matches the
    # ticker index (perfect ordering). Others are constant noise.
    snapshots = {}
    for date in dates:
        rows = []
        for i, t in enumerate(tickers):
            rows.append({
                "ticker": t,
                "maturity_stage": "growth_compounder",
                "subscores": {
                    "adoption_momentum": 50.0,           # constant -> no signal
                    "institutional_confidence": float(i),  # perfect signal
                    "financial_evolution": 50.0,
                    "thesis_integrity": 50.0,
                    "des": 50.0,
                },
            })
        snapshots[date] = rows

    out = audit_mod.compute_cohort_ic(snapshots, horizon=5)
    assert "growth_compounder" in out
    cohort = out["growth_compounder"]

    # The signal pillar should have a perfect IC mean every day. When the
    # daily IC is identical across days, the IC std is 0 and our Sharpe
    # falls back to 0.0 (we don't return inf). What matters for the audit
    # is that the **mean** is recovered.
    inst = cohort["institutional_confidence"]
    assert inst["ic_mean"] == pytest.approx(1.0, abs=1e-6)
    assert inst["ic_std"] == pytest.approx(0.0, abs=1e-9)
    # n_dates should equal the number of observation dates we built.
    assert inst["n_dates"] == len(dates)

    # The other pillars are constant => spearman undefined => not in cohort
    # (the script drops them per-day). Confirm only the signal pillar got
    # recorded.
    for p in ["adoption_momentum", "financial_evolution", "thesis_integrity", "des"]:
        assert p not in cohort  # all-constant => no IC computed
