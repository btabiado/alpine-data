"""Tests for lthcs.narratives.

These are pure unit tests -- no I/O, no network. The narrative generator
takes a ``compute_lthcs_score``-shaped dict and (optionally) yesterday's
snapshot and returns four templated paragraphs, so every fixture here is
just a hand-built dict.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Dict, Optional

import pytest

from lthcs.narratives import (
    BAND_DESCRIPTORS,
    HUMAN_PILLAR_NAMES,
    generate_narratives,
)


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


REQUIRED_KEYS = {
    "ticker",
    "todays_take",
    "why_changed",
    "why_not_to_sell",
    "what_would_break",
    "confidence_level",
}


def _score_dict(
    *,
    ticker: str = "AAPL",
    score: float = 82.4,
    band: str = "high_confidence",
    drift_1d: float = 0.3,
    drift_7d: float = 1.2,
    drift_30d: float = -0.8,
    drift_90d: float = 2.4,
    confidence_level: str = "high",
    subscores: Optional[Dict[str, float]] = None,
    sector: str = "Technology",
) -> Dict:
    if subscores is None:
        subscores = {
            "adoption_momentum": 78.0,
            "institutional_confidence": 85.0,
            "financial_evolution": 80.0,
            "thesis_integrity": 76.0,
            "des": 88.0,
        }
    return {
        "ticker": ticker,
        "lthcs_score": score,
        "band": band,
        "drift_1d": drift_1d,
        "drift_7d": drift_7d,
        "drift_30d": drift_30d,
        "drift_90d": drift_90d,
        "confidence_level": confidence_level,
        "data_quality_flags": [],
        "subscores": dict(subscores),
        "modifiers": {"macro_adj": 0.0, "sector_adj": 1.0, "volatility_mod": 0.0},
        "maturity_stage": "standard_compounder",
        "weights_used": [0.25, 0.20, 0.15, 0.20, 0.20],
        "weighted_components": [19.5, 17.0, 12.0, 15.2, 17.6],
        "sector": sector,
    }


# ---------------------------------------------------------------------------
# Output-shape contract
# ---------------------------------------------------------------------------


def test_output_has_exactly_required_keys():
    out = generate_narratives(_score_dict())
    assert set(out.keys()) == REQUIRED_KEYS


def test_ticker_propagated():
    out = generate_narratives(_score_dict(ticker="LCID"))
    assert out["ticker"] == "LCID"
    assert "LCID" in out["todays_take"]


def test_confidence_level_copied():
    out = generate_narratives(_score_dict(confidence_level="medium"))
    assert out["confidence_level"] == "medium"


def test_all_narratives_end_with_period():
    out = generate_narratives(_score_dict())
    for key in ("todays_take", "why_changed", "why_not_to_sell", "what_would_break"):
        assert out[key].endswith("."), f"{key} did not end with period: {out[key]!r}"


def test_all_narratives_are_strings_and_nonempty():
    out = generate_narratives(_score_dict())
    for key in ("todays_take", "why_changed", "why_not_to_sell", "what_would_break"):
        assert isinstance(out[key], str)
        assert len(out[key]) > 10


# ---------------------------------------------------------------------------
# todays_take -- band action + top pillar
# ---------------------------------------------------------------------------


def test_todays_take_uses_band_action_and_top_pillar_for_aapl():
    out = generate_narratives(_score_dict())
    take = out["todays_take"]

    # Band action phrase for high_confidence.
    assert BAND_DESCRIPTORS["high_confidence"]["action"] in take
    # Score formatted with 1 decimal.
    assert "82.4" in take
    # 30-day drift with explicit sign.
    assert "-0.8 over 30 days" in take
    # Top pillar is Demand Environment (88.0).
    assert HUMAN_PILLAR_NAMES["des"] in take
    assert "88.0" in take


def test_todays_take_picks_highest_subscore():
    subs = {
        "adoption_momentum": 91.0,
        "institutional_confidence": 70.0,
        "financial_evolution": 70.0,
        "thesis_integrity": 70.0,
        "des": 70.0,
    }
    out = generate_narratives(_score_dict(subscores=subs))
    assert HUMAN_PILLAR_NAMES["adoption_momentum"] in out["todays_take"]
    assert "91.0" in out["todays_take"]


def test_todays_take_tie_break_prefers_pillar_order():
    # Two pillars tied for top; PILLAR_ORDER says adoption_momentum wins.
    subs = {
        "adoption_momentum": 85.0,
        "institutional_confidence": 70.0,
        "financial_evolution": 70.0,
        "thesis_integrity": 70.0,
        "des": 85.0,  # tied with adoption_momentum
    }
    out = generate_narratives(_score_dict(subscores=subs))
    assert HUMAN_PILLAR_NAMES["adoption_momentum"] in out["todays_take"]
    # Demand Environment must not be named as the supporting pillar here.
    # (It could only legitimately appear if it were the unique top, which
    # it isn't.) We check the *supporting* clause explicitly.
    assert "supported by Adoption Momentum" in out["todays_take"]


# ---------------------------------------------------------------------------
# why_changed -- prior present / absent / flat
# ---------------------------------------------------------------------------


def test_why_changed_without_prior_uses_fallback():
    out = generate_narratives(_score_dict())
    why = out["why_changed"]
    assert "no prior snapshot" in why
    # Score and drift should still be mentioned.
    assert "82.4" in why
    assert "+0.3" in why


def test_why_changed_with_prior_names_largest_delta_pillar():
    today = _score_dict(
        drift_1d=1.5,
        subscores={
            "adoption_momentum": 78.0,
            "institutional_confidence": 85.0,
            "financial_evolution": 80.0,
            "thesis_integrity": 90.0,  # +12 jump vs prior
            "des": 88.0,
        },
    )
    prior = _score_dict(
        drift_1d=0.0,
        subscores={
            "adoption_momentum": 78.0,
            "institutional_confidence": 85.0,
            "financial_evolution": 80.0,
            "thesis_integrity": 78.0,  # was 78
            "des": 88.0,
        },
    )
    out = generate_narratives(today, prior_score_dict=prior)
    why = out["why_changed"]

    assert "up 1.5 today" in why
    assert HUMAN_PILLAR_NAMES["thesis_integrity"] in why
    assert "+12.0" in why


def test_why_changed_flat_branch_fires_for_tiny_drift():
    today = _score_dict(
        drift_1d=0.05,
        subscores={
            "adoption_momentum": 80.0,
            "institutional_confidence": 85.0,
            "financial_evolution": 80.0,
            "thesis_integrity": 76.0,
            "des": 88.0,
        },
    )
    prior = _score_dict(
        drift_1d=0.0,
        subscores={
            "adoption_momentum": 78.0,  # +2 today
            "institutional_confidence": 85.0,
            "financial_evolution": 80.0,
            "thesis_integrity": 76.0,
            "des": 88.0,
        },
    )
    out = generate_narratives(today, prior_score_dict=prior)
    why = out["why_changed"]

    assert "essentially flat" in why
    assert "+0.05" in why
    assert HUMAN_PILLAR_NAMES["adoption_momentum"] in why
    assert "+2.0" in why


def test_why_changed_down_direction():
    today = _score_dict(
        drift_1d=-0.7,
        subscores={
            "adoption_momentum": 78.0,
            "institutional_confidence": 80.0,  # -5 vs prior
            "financial_evolution": 80.0,
            "thesis_integrity": 76.0,
            "des": 88.0,
        },
    )
    prior = _score_dict(
        subscores={
            "adoption_momentum": 78.0,
            "institutional_confidence": 85.0,
            "financial_evolution": 80.0,
            "thesis_integrity": 76.0,
            "des": 88.0,
        },
    )
    out = generate_narratives(today, prior_score_dict=prior)
    assert "down 0.7 today" in out["why_changed"]
    assert "-5.0" in out["why_changed"]
    assert HUMAN_PILLAR_NAMES["institutional_confidence"] in out["why_changed"]


# ---------------------------------------------------------------------------
# why_not_to_sell -- band-tone branching
# ---------------------------------------------------------------------------


def test_why_not_to_sell_conviction_tone_for_high_confidence():
    out = generate_narratives(_score_dict(band="high_confidence"))
    body = out["why_not_to_sell"]
    assert "noise-driven" in body
    assert HUMAN_PILLAR_NAMES["des"] in body  # top pillar
    assert HUMAN_PILLAR_NAMES["institutional_confidence"] in body  # 2nd top


def test_why_not_to_sell_conviction_tone_for_elite():
    out = generate_narratives(_score_dict(band="elite", score=92.0))
    assert "noise-driven" in out["why_not_to_sell"]


def test_why_not_to_sell_watch_tone_for_monitor():
    subs = {
        "adoption_momentum": 65.0,
        "institutional_confidence": 70.0,  # top
        "financial_evolution": 55.0,  # weakest
        "thesis_integrity": 62.0,
        "des": 68.0,
    }
    out = generate_narratives(
        _score_dict(band="monitor", score=64.0, subscores=subs)
    )
    body = out["why_not_to_sell"]
    assert "warrants closer attention" in body
    assert HUMAN_PILLAR_NAMES["institutional_confidence"] in body  # top
    assert HUMAN_PILLAR_NAMES["financial_evolution"] in body      # weakest
    assert "next 30 days" in body


def test_why_not_to_sell_review_tone_for_weakening_lcid():
    subs = {
        "adoption_momentum": 45.0,
        "institutional_confidence": 55.0,
        "financial_evolution": 35.0,  # weakest
        "thesis_integrity": 40.0,     # 2nd weakest
        "des": 60.0,
    }
    out = generate_narratives(
        _score_dict(ticker="LCID", band="weakening", score=55.0, subscores=subs)
    )
    body = out["why_not_to_sell"]
    assert "Structural concerns" in body
    assert HUMAN_PILLAR_NAMES["financial_evolution"] in body
    assert HUMAN_PILLAR_NAMES["thesis_integrity"] in body
    assert "reassessed" in body


def test_why_not_to_sell_review_tone_for_review_intc():
    subs = {
        "adoption_momentum": 40.0,
        "institutional_confidence": 45.0,
        "financial_evolution": 25.0,  # weakest
        "thesis_integrity": 30.0,     # 2nd weakest
        "des": 50.0,
    }
    out = generate_narratives(
        _score_dict(ticker="INTC", band="review", score=35.0, subscores=subs)
    )
    body = out["why_not_to_sell"]
    assert "Structural concerns" in body
    assert HUMAN_PILLAR_NAMES["financial_evolution"] in body
    assert HUMAN_PILLAR_NAMES["thesis_integrity"] in body


# ---------------------------------------------------------------------------
# what_would_break -- weakest pillar + next-band-down threshold
# ---------------------------------------------------------------------------


def test_what_would_break_high_confidence_references_constructive_floor():
    # high_confidence band min is 80, so next_band_threshold = 79.9.
    out = generate_narratives(_score_dict(band="high_confidence"))
    body = out["what_would_break"]
    # Weakest pillar in the default fixture is thesis_integrity (76.0).
    assert HUMAN_PILLAR_NAMES["thesis_integrity"] in body
    # decline_threshold = 76 - 10 = 66 -> formatted as "66".
    assert "below 66" in body
    # Composite floor of 79.9 -> "{:.0f}" => "80" (banker-ish rounding ok,
    # Python uses half-to-even; 79.9 rounds to 80).
    assert "below 80" in body
    assert "Constructive" in body
    assert "structural review" in body


def test_what_would_break_monitor_band_next_is_weakening():
    subs = {
        "adoption_momentum": 65.0,
        "institutional_confidence": 70.0,
        "financial_evolution": 55.0,  # weakest
        "thesis_integrity": 62.0,
        "des": 68.0,
    }
    out = generate_narratives(
        _score_dict(band="monitor", score=64.0, subscores=subs)
    )
    body = out["what_would_break"]
    assert HUMAN_PILLAR_NAMES["financial_evolution"] in body
    assert "Weakening" in body  # next band down from monitor


def test_what_would_break_review_band_uses_exit_phrasing():
    subs = {
        "adoption_momentum": 30.0,
        "institutional_confidence": 40.0,
        "financial_evolution": 25.0,
        "thesis_integrity": 35.0,
        "des": 45.0,
    }
    out = generate_narratives(
        _score_dict(band="review", score=35.0, subscores=subs)
    )
    body = out["what_would_break"]
    assert "already in structural review" in body
    assert "below 30" in body
    assert HUMAN_PILLAR_NAMES["financial_evolution"] in body
    assert "exit decision" in body


def test_what_would_break_decline_threshold_clamped_at_zero():
    # If weakest sub-score is already < 10, threshold must not go negative.
    subs = {
        "adoption_momentum": 30.0,
        "institutional_confidence": 40.0,
        "financial_evolution": 5.0,  # weakest, would underflow
        "thesis_integrity": 35.0,
        "des": 45.0,
    }
    out = generate_narratives(
        _score_dict(band="weakening", score=55.0, subscores=subs)
    )
    body = out["what_would_break"]
    # Should not contain a negative number for the decline threshold.
    assert "-" not in body.split("below")[1].split(",")[0]


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_all_equal_subscores_and_zero_drift_produces_sensible_output():
    subs = {p: 50.0 for p in HUMAN_PILLAR_NAMES}
    sd = _score_dict(
        band="weakening",
        score=50.0,
        drift_1d=0.0,
        drift_7d=0.0,
        drift_30d=0.0,
        drift_90d=0.0,
        subscores=subs,
        confidence_level="low",
    )
    out = generate_narratives(sd)

    # No exceptions, all four narratives present and well-formed.
    assert set(out.keys()) == REQUIRED_KEYS
    for key in ("todays_take", "why_changed", "why_not_to_sell", "what_would_break"):
        assert isinstance(out[key], str)
        assert out[key].endswith(".")
    # Tie-break -> top pillar is adoption_momentum.
    assert HUMAN_PILLAR_NAMES["adoption_momentum"] in out["todays_take"]
    # Weakest pillar tie also broken by PILLAR_ORDER -> adoption_momentum
    # is also the *weakest* when all tie. Test that what_would_break still
    # names a pillar.
    assert HUMAN_PILLAR_NAMES["adoption_momentum"] in out["what_would_break"]


def test_missing_subscores_default_to_neutral_without_crash():
    sd = _score_dict()
    sd["subscores"] = {}  # remove all
    out = generate_narratives(sd)
    assert set(out.keys()) == REQUIRED_KEYS
    # Top pillar tie-break with all-50s = adoption_momentum.
    assert HUMAN_PILLAR_NAMES["adoption_momentum"] in out["todays_take"]


def test_does_not_mutate_inputs():
    sd = _score_dict()
    sd_snapshot = deepcopy(sd)
    prior = _score_dict(
        subscores={
            "adoption_momentum": 70.0,
            "institutional_confidence": 80.0,
            "financial_evolution": 75.0,
            "thesis_integrity": 70.0,
            "des": 85.0,
        }
    )
    prior_snapshot = deepcopy(prior)

    generate_narratives(sd, prior_score_dict=prior)

    assert sd == sd_snapshot
    assert prior == prior_snapshot


def test_unknown_band_falls_back_gracefully():
    sd = _score_dict(band="totally_made_up")
    out = generate_narratives(sd)
    # Should still return a complete dict.
    assert set(out.keys()) == REQUIRED_KEYS
    for key in ("todays_take", "why_changed", "why_not_to_sell", "what_would_break"):
        assert out[key].endswith(".")
