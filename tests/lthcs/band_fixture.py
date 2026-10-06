"""A fixed score-band set for unit tests of band logic.

The cutoffs are the ones production used from the 2026-05-18 recalibration
until 2026-10-06 (elite 85-100, high confidence 80-84, constructive 70-79,
monitor 60-69, weakening 50-59, review 0-49). They are deliberately NOT read
from data/lthcs/weights.json: recalibrating the live bands
(scripts/lthcs_calibrate_bands.py --write) must not move the expectations of
tests that check how a score is assigned to a band. Tests that check the live
config itself read weights.json and derive their expectations from it.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List, Tuple

FIXTURE_SCORE_BANDS: Dict[str, Dict[str, Any]] = {
    "elite":           {"min": 85, "max": 100, "color": "#1F3A5F", "label": "Elite Confidence Hold"},
    "high_confidence": {"min": 80, "max": 84,  "color": "#4A8F5F", "label": "High Confidence Hold"},
    "constructive":    {"min": 70, "max": 79,  "color": "#C9A227", "label": "Constructive Hold"},
    "monitor":         {"min": 60, "max": 69,  "color": "#D89148", "label": "Monitor Closely"},
    "weakening":       {"min": 50, "max": 59,  "color": "#B85A3E", "label": "Confidence Weakening"},
    "review":          {"min": 0,  "max": 49,  "color": "#7A2E1F", "label": "Structural Review Required"},
}


def fixture_score_bands() -> Dict[str, Dict[str, Any]]:
    """A fresh copy (callers may mutate it)."""
    return copy.deepcopy(FIXTURE_SCORE_BANDS)


def fixture_band_ranges() -> Dict[str, Tuple[int, int]]:
    """``{band: (min, max)}``, the shape ``lthcs.bands.band_ranges`` returns."""
    return {k: (v["min"], v["max"]) for k, v in FIXTURE_SCORE_BANDS.items()}


def fixture_bands_low_to_high() -> List[Tuple[str, int, int]]:
    """``[(band, min, max), ...]`` lowest band first (the audit scripts' shape)."""
    return sorted(((k, lo, hi) for k, (lo, hi) in fixture_band_ranges().items()),
                  key=lambda t: t[1])


def with_fixture_bands(weights_config: Dict[str, Any]) -> Dict[str, Any]:
    """A copy of ``weights_config`` whose ``score_bands`` is the fixture set."""
    cfg = copy.deepcopy(weights_config)
    cfg["score_bands"] = fixture_score_bands()
    cfg.pop("score_bands_calibration", None)
    return cfg
