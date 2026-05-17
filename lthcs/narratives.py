"""LTHCS V1 templated narrative generator.

Produces four short narrative paragraphs per snapshot row from a
``compute_lthcs_score``-shaped dict. V1 does NOT call any LLM -- all
prose comes from deterministic templates grounded in the score dict
and (optionally) the prior day's snapshot.

The public surface is intentionally tiny:

* :data:`HUMAN_PILLAR_NAMES`   -- pretty labels for the five pillars
* :data:`BAND_DESCRIPTORS`     -- band-aware action verbs + review tone
* :func:`generate_narratives`  -- the only callable

See PHASE_1_BUILD_SPEC.md sections 7, 9, and 12 for the contract.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Canonical pillar order from lthcs.schemas.weights.PILLAR_ORDER. Duplicated
# here as a plain list to keep this module free of side-effect imports and
# to act as the deterministic tie-break order for top/bottom pillar picks.
PILLAR_ORDER: List[str] = [
    "adoption_momentum",
    "institutional_confidence",
    "financial_evolution",
    "thesis_integrity",
    "des",
]

HUMAN_PILLAR_NAMES: Dict[str, str] = {
    "adoption_momentum": "Adoption Momentum",
    "institutional_confidence": "Institutional Confidence",
    "financial_evolution": "Financial Evolution",
    "thesis_integrity": "Thesis Integrity",
    "des": "Demand Environment",
}

# Band-aware vocabulary used in templates.
BAND_DESCRIPTORS: Dict[str, Dict[str, str]] = {
    "elite":            {"action": "holds in Elite Confidence",       "review_tone": "conviction"},
    "high_confidence":  {"action": "holds in High Confidence",        "review_tone": "conviction"},
    "constructive":     {"action": "remains Constructive",            "review_tone": "conviction"},
    "monitor":          {"action": "sits in Monitor",                 "review_tone": "watch"},
    "weakening":        {"action": "is showing Weakening",            "review_tone": "review"},
    "review":           {"action": "requires Structural Review",      "review_tone": "review"},
}

# Bands ordered from highest to lowest. Mirrors the score_bands ranges in
# data/lthcs/weights.json. Kept inline so this module is I/O free.
_BAND_ORDER_HIGH_TO_LOW: List[str] = [
    "elite",
    "high_confidence",
    "constructive",
    "monitor",
    "weakening",
    "review",
]

# (min, max) inclusive ranges for each band, matching weights.json.
_BAND_RANGES: Dict[str, Tuple[int, int]] = {
    "elite":           (90, 100),
    "high_confidence": (80,  89),
    "constructive":    (70,  79),
    "monitor":         (60,  69),
    "weakening":       (50,  59),
    "review":          (0,   49),
}

# Colloquial label used in the "what would break" template when naming the
# next-band-down. Keeps the prose readable without exposing snake_case.
_BAND_COLLOQUIAL: Dict[str, str] = {
    "elite":           "Elite",
    "high_confidence": "High Confidence",
    "constructive":    "Constructive",
    "monitor":         "Monitor",
    "weakening":       "Weakening",
    "review":          "Structural Review",
}

_FLAT_DRIFT_THRESHOLD = 0.1


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _safe_float(x, default: float = 0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _subscores(score_dict: Dict) -> Dict[str, float]:
    """Return a {pillar: sub_score} mapping covering every PILLAR_ORDER key.

    Missing pillars default to 50.0 (the neutral midpoint), which lets the
    generator stay defensive against partial inputs without raising.
    """
    raw = score_dict.get("subscores") or {}
    return {p: _safe_float(raw.get(p), 50.0) for p in PILLAR_ORDER}


def _rank_pillars(
    subs: Dict[str, float], *, descending: bool
) -> List[Tuple[str, float]]:
    """Sort pillars by sub_score with PILLAR_ORDER as the tie-break.

    Sorting is stable, so we pre-order by PILLAR_ORDER and then use a
    score-only key. Descending sort still preserves PILLAR_ORDER among
    ties because we never compare on the pillar name itself.
    """
    ordered = [(p, subs[p]) for p in PILLAR_ORDER]
    ordered.sort(key=lambda kv: kv[1], reverse=descending)
    return ordered


def _next_band_down(band: str) -> str:
    """Return the band one notch below ``band`` (or ``band`` if already at review)."""
    if band == "review":
        return "review"
    try:
        idx = _BAND_ORDER_HIGH_TO_LOW.index(band)
    except ValueError:
        # Unknown band -- be conservative and stay in place.
        return band
    if idx + 1 >= len(_BAND_ORDER_HIGH_TO_LOW):
        return band
    return _BAND_ORDER_HIGH_TO_LOW[idx + 1]


def _direction_word(drift_1d: float) -> str:
    if abs(drift_1d) < _FLAT_DRIFT_THRESHOLD:
        return "flat"
    return "up" if drift_1d > 0 else "down"


# ---------------------------------------------------------------------------
# Template builders -- one per narrative paragraph
# ---------------------------------------------------------------------------


def _todays_take(score_dict: Dict, ranked_desc: List[Tuple[str, float]]) -> str:
    ticker = score_dict.get("ticker", "?")
    score = _safe_float(score_dict.get("lthcs_score"))
    drift_30d = _safe_float(score_dict.get("drift_30d"))
    band = score_dict.get("band", "monitor")
    action = BAND_DESCRIPTORS.get(band, BAND_DESCRIPTORS["monitor"])["action"]

    top_pillar, top_score = ranked_desc[0]
    top_name = HUMAN_PILLAR_NAMES[top_pillar]

    return (
        "{ticker} {action} at {score:.1f} "
        "({drift_30d:+.1f} over 30 days), supported by {top_name} at {top_score:.1f}."
    ).format(
        ticker=ticker,
        action=action,
        score=score,
        drift_30d=drift_30d,
        top_name=top_name,
        top_score=top_score,
    )


def _why_changed(score_dict: Dict, prior: Optional[Dict]) -> str:
    score = _safe_float(score_dict.get("lthcs_score"))
    drift_1d = _safe_float(score_dict.get("drift_1d"))

    if prior is None:
        return (
            "Score is at {score:.1f} (drift over 1d {drift_1d:+.1f}); "
            "no prior snapshot available for component-delta analysis."
        ).format(score=score, drift_1d=drift_1d)

    today_subs = _subscores(score_dict)
    prior_subs = _subscores(prior)

    # Pillar with the largest absolute one-day delta. PILLAR_ORDER as
    # tie-break (we iterate in PILLAR_ORDER and only replace on strict >).
    top_driver_pillar = PILLAR_ORDER[0]
    top_driver_delta = today_subs[top_driver_pillar] - prior_subs[top_driver_pillar]
    for p in PILLAR_ORDER[1:]:
        delta = today_subs[p] - prior_subs[p]
        if abs(delta) > abs(top_driver_delta):
            top_driver_pillar = p
            top_driver_delta = delta

    driver_name = HUMAN_PILLAR_NAMES[top_driver_pillar]
    direction = _direction_word(drift_1d)

    if direction == "flat":
        return (
            "Score is essentially flat (drift_1d {drift_1d:+.2f}); "
            "the largest component move was {delta:+.1f} in {name}."
        ).format(drift_1d=drift_1d, delta=top_driver_delta, name=driver_name)

    return (
        "Score is {direction} {abs_drift:.1f} today, driven primarily by a "
        "{delta:+.1f} move in {name}."
    ).format(
        direction=direction,
        abs_drift=abs(drift_1d),
        delta=top_driver_delta,
        name=driver_name,
    )


def _why_not_to_sell(
    score_dict: Dict,
    ranked_desc: List[Tuple[str, float]],
    ranked_asc: List[Tuple[str, float]],
) -> str:
    band = score_dict.get("band", "monitor")
    tone = BAND_DESCRIPTORS.get(band, BAND_DESCRIPTORS["monitor"])["review_tone"]

    top_pillar, top_score = ranked_desc[0]
    second_pillar, second_score = ranked_desc[1]
    weakest_pillar, weakest_score = ranked_asc[0]
    second_weakest_pillar, second_weakest_score = ranked_asc[1]

    top_name = HUMAN_PILLAR_NAMES[top_pillar]
    second_top_name = HUMAN_PILLAR_NAMES[second_pillar]
    weakest_name = HUMAN_PILLAR_NAMES[weakest_pillar]
    second_weakest_name = HUMAN_PILLAR_NAMES[second_weakest_pillar]

    if tone == "conviction":
        return (
            "Recent volatility appears noise-driven; {top} remains firmly "
            "in the upper band at {top_score:.1f}, and {second} is also "
            "strong ({second_score:.1f}). No structural breach in the "
            "highest-conviction signals."
        ).format(
            top=top_name,
            top_score=top_score,
            second=second_top_name,
            second_score=second_score,
        )

    if tone == "watch":
        return (
            "Position warrants closer attention but isn't broken: {top} "
            "still leads at {top_score:.1f}, though {weakest} at "
            "{weakest_score:.1f} is dragging the composite. Watch for "
            "confirmation in the next 30 days."
        ).format(
            top=top_name,
            top_score=top_score,
            weakest=weakest_name,
            weakest_score=weakest_score,
        )

    # tone == "review"
    return (
        "Structural concerns: {weakest} at {weakest_score:.1f} is well "
        "below the universe median, and {second_weakest} at "
        "{second_weakest_score:.1f} is not compensating. Position should "
        "be reassessed against the original thesis."
    ).format(
        weakest=weakest_name,
        weakest_score=weakest_score,
        second_weakest=second_weakest_name,
        second_weakest_score=second_weakest_score,
    )


def _what_would_break(
    score_dict: Dict, ranked_asc: List[Tuple[str, float]]
) -> str:
    band = score_dict.get("band", "monitor")
    weakest_pillar, weakest_score = ranked_asc[0]
    weakest_name = HUMAN_PILLAR_NAMES[weakest_pillar]

    if band == "review":
        return (
            "Score is already in structural review territory. A move below "
            "30 on {name}, combined with a continued drift below 40 in the "
            "composite, would force a position exit decision."
        ).format(name=weakest_name)

    next_band = _next_band_down(band)
    next_band_min, _ = _BAND_RANGES.get(next_band, _BAND_RANGES["review"])
    # Threshold = just under the current band's floor, which is the same as
    # (next-band max + 1) - 0.1 == next_band.max + 0.9. We express it as
    # "current band min - 0.1" per the spec.
    current_min, _ = _BAND_RANGES.get(band, _BAND_RANGES["monitor"])
    next_band_threshold = current_min - 0.1

    decline_threshold = max(weakest_score - 10.0, 0.0)
    next_band_action = _BAND_COLLOQUIAL.get(next_band, next_band)

    return (
        "A sustained drop in {name} below {decline:.0f}, or a {next_action} "
        "composite move below {next_threshold:.0f}, would force a "
        "structural review."
    ).format(
        name=weakest_name,
        decline=decline_threshold,
        next_action=next_band_action,
        next_threshold=next_band_threshold,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate_narratives(
    score_dict: Dict,
    *,
    prior_score_dict: Optional[Dict] = None,
) -> Dict:
    """Return four templated narrative paragraphs for a snapshot row.

    Parameters
    ----------
    score_dict
        Output of ``compute_lthcs_score`` (see module docstring for shape).
    prior_score_dict
        Optional yesterday's snapshot. When supplied, ``why_changed``
        names the pillar with the biggest single-day delta; otherwise a
        no-prior-data fallback is used.

    Returns
    -------
    dict
        Keys: ``ticker``, ``todays_take``, ``why_changed``,
        ``why_not_to_sell``, ``what_would_break``, ``confidence_level``.
    """
    subs = _subscores(score_dict)
    ranked_desc = _rank_pillars(subs, descending=True)
    ranked_asc = _rank_pillars(subs, descending=False)

    return {
        "ticker": score_dict.get("ticker", "?"),
        "todays_take": _todays_take(score_dict, ranked_desc),
        "why_changed": _why_changed(score_dict, prior_score_dict),
        "why_not_to_sell": _why_not_to_sell(score_dict, ranked_desc, ranked_asc),
        "what_would_break": _what_would_break(score_dict, ranked_asc),
        "confidence_level": score_dict.get("confidence_level", "unknown"),
    }
