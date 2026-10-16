"""Single source of truth for LTHCS score-band cutoffs and assignment.

The live cutoffs live in ``data/lthcs/weights.json`` -> ``score_bands``
(integer, inclusive ``min``/``max`` per band; see
``lthcs/schemas/weights.py``). Everything that needs to know "where does
Elite start" — narratives, the LLM system prompt, the audit scripts —
should go through this module instead of hard-coding 85/80/70/60/50, so a
recalibration (``scripts/lthcs_calibrate_bands.py --write``) changes every
consumer consistently.

``assign_band`` is the only score-to-band mapping. The scorer
(``lthcs.score``) re-exports it, and the backfill validator calls it.
Do not reimplement the floor-and-inclusive-range lookup.

A band-threshold change is a minor config bump and is not restated into
older snapshots (``docs/lthcs-tuning-kit.md``). Closed regimes are kept in
``score_bands_history``; ``score_bands_for_date`` returns the cutoffs that
were in effect on a snapshot's calc date. The daily scorer always uses the
live cutoffs. The validator uses the historical ones.

``DEFAULT_SCORE_BANDS`` is only a fallback for when weights.json is missing
or unreadable (unit tests with a tmp data root, a partial checkout); it is
not authoritative and is never written anywhere. It mirrors the live
cutoffs (calibrated 2026-10-06 on the first 515-ticker snapshot) so a
fallback never silently brings back older ones; a test keeps the two equal,
so update it together with a recalibration.
"""

from __future__ import annotations

import json
import math
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_WEIGHTS_PATH = REPO_ROOT / "data" / "lthcs" / "weights.json"

# Canonical band order, highest to lowest. Keys are part of the snapshot
# contract (``band`` field) and must never be renamed by a recalibration.
BAND_ORDER_HIGH_TO_LOW: Tuple[str, ...] = (
    "elite",
    "high_confidence",
    "constructive",
    "monitor",
    "weakening",
    "review",
)

DEFAULT_SCORE_BANDS: Dict[str, Dict[str, Any]] = {
    "elite":           {"min": 70, "max": 100, "color": "#1F3A5F", "label": "Elite Confidence Hold"},
    "high_confidence": {"min": 63, "max": 69,  "color": "#4A8F5F", "label": "High Confidence Hold"},
    "constructive":    {"min": 52, "max": 62,  "color": "#C9A227", "label": "Constructive Hold"},
    "monitor":         {"min": 42, "max": 51,  "color": "#D89148", "label": "Monitor Closely"},
    "weakening":       {"min": 33, "max": 41,  "color": "#B85A3E", "label": "Confidence Weakening"},
    "review":          {"min": 0,  "max": 32,  "color": "#7A2E1F", "label": "Structural Review Required"},
}

# Short names used in prose ("a Constructive composite move below 80").
BAND_COLLOQUIAL: Dict[str, str] = {
    "elite":           "Elite",
    "high_confidence": "High Confidence",
    "constructive":    "Constructive",
    "monitor":         "Monitor",
    "weakening":       "Weakening",
    "review":          "Structural Review",
}


def _copy(bands: Mapping[str, Mapping[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {k: dict(v) for k, v in bands.items()}


@lru_cache(maxsize=8)
def _load_cached(path_str: str, mtime: float) -> Optional[Dict[str, Dict[str, Any]]]:
    try:
        cfg = json.loads(Path(path_str).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    bands = cfg.get("score_bands") if isinstance(cfg, dict) else None
    if not isinstance(bands, dict) or not bands:
        return None
    return _copy(bands)


def load_score_bands(path: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    """Return ``score_bands`` from weights.json (fallback: defaults).

    Cached on (path, mtime) so repeated calls in a daily run are free but
    an in-place rewrite of weights.json is picked up. Always returns a
    fresh copy; callers may mutate it.
    """
    p = Path(path) if path is not None else DEFAULT_WEIGHTS_PATH
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return _copy(DEFAULT_SCORE_BANDS)
    loaded = _load_cached(str(p), mtime)
    return _copy(loaded) if loaded else _copy(DEFAULT_SCORE_BANDS)


def band_ranges(
    score_bands: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> Dict[str, Tuple[int, int]]:
    """``{band: (min, max)}`` inclusive integer ranges."""
    bands = score_bands if score_bands is not None else load_score_bands()
    out: Dict[str, Tuple[int, int]] = {}
    for key, spec in bands.items():
        try:
            out[key] = (int(float(spec["min"])), int(float(spec["max"])))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def ordered_bands(
    score_bands: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> List[Tuple[str, int, int]]:
    """``[(band, min, max), ...]`` sorted highest band first."""
    rng = band_ranges(score_bands)
    return sorted(((k, lo, hi) for k, (lo, hi) in rng.items()), key=lambda t: -t[1])


def assign_band(score: float, score_bands: Optional[Mapping[str, Mapping[str, Any]]]) -> str:
    """Return the band key covering ``score``.

    Bands in weights.json are INTEGER-bounded (e.g. constructive=70..79,
    high_confidence=80..84 — the live numbers are whatever weights.json
    says; see scripts/lthcs_calibrate_bands.py) but composite scores carry
    one decimal place, so fractional scores like 79.4 sit in a gap between
    bands and would otherwise be wrongly assigned. We treat each band as
    covering the half-open interval ``[min, max+1)`` so a score of 79.4
    lands in constructive (since 70 ≤ 79.4 < 80), 84.99 lands in
    high_confidence, 100.0 lands in elite. Equivalent to flooring ``score``
    to its integer part before doing the inclusive lookup.

    Out-of-range inputs are clamped to [0, 100] before lookup.

    This is the single definition shared by the scorer and the validator.
    """
    try:
        s = float(score)
    except (TypeError, ValueError):
        s = 0.0
    if math.isnan(s):  # NaN
        s = 0.0
    s = max(0.0, min(100.0, s))
    floored = int(math.floor(s))
    # Special-case the top: a perfect 100.0 floors to 100, which fits
    # elite's max=100 inclusive check below — no extra handling needed.
    for name, spec in (score_bands or {}).items():
        try:
            lo = int(float(spec["min"]))
            hi = int(float(spec["max"]))
        except (KeyError, TypeError, ValueError):
            continue
        if lo <= floored <= hi:
            return name
    # Fallback: return the band whose min is closest below the score.
    # (Defensive — the standard config tiles [0,100] without gaps.)
    best_name: Optional[str] = None
    best_lo = float("-inf")
    for name, spec in (score_bands or {}).items():
        try:
            lo = float(spec["min"])
        except (KeyError, TypeError, ValueError):
            continue
        if lo <= s and lo > best_lo:
            best_lo = lo
            best_name = name
    return best_name or "review"


def _regime_day(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value)[:10]
    return text or None


def score_bands_for_date(
    weights: Mapping[str, Any],
    on: str,
) -> Dict[str, Dict[str, Any]]:
    """Return the cutoffs in effect on ``on`` (``YYYY-MM-DD``).

    ``score_bands_history`` entries cover ``[effective_from, effective_until)``.
    ``effective_from`` null means the beginning of the record. A date on or
    after every regime's ``effective_until``, and any weights file with no
    history, uses the live ``score_bands``. The first matching regime wins.
    """
    if not isinstance(weights, Mapping):
        return {}
    live = weights.get("score_bands")
    history = weights.get("score_bands_history")
    day = str(on)[:10]
    if isinstance(history, list):
        for regime in history:
            if not isinstance(regime, dict):
                continue
            start = _regime_day(regime.get("effective_from"))
            until = _regime_day(regime.get("effective_until"))
            if start is not None and day < start:
                continue
            if until is not None and day >= until:
                continue
            bands = regime.get("score_bands")
            if isinstance(bands, dict) and bands:
                return _copy(bands)
    if isinstance(live, dict) and live:
        return _copy(live)
    return {}


