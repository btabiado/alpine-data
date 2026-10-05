"""Single source of truth for LTHCS score-band cutoffs at runtime.

The live cutoffs live in ``data/lthcs/weights.json`` -> ``score_bands``
(integer, inclusive ``min``/``max`` per band; see
``lthcs/schemas/weights.py``). Everything that needs to know "where does
Elite start" — narratives, the LLM system prompt, the audit scripts —
should go through this module instead of hard-coding 85/80/70/60/50, so a
recalibration (``scripts/lthcs_calibrate_bands.py --write``) changes every
consumer consistently.

``DEFAULT_SCORE_BANDS`` is only a fallback for when weights.json is missing
or unreadable (unit tests with a tmp data root, a partial checkout); it is
not authoritative and is never written anywhere.
"""

from __future__ import annotations

import json
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
    "elite":           {"min": 85, "max": 100, "color": "#1F3A5F", "label": "Elite Confidence Hold"},
    "high_confidence": {"min": 80, "max": 84,  "color": "#4A8F5F", "label": "High Confidence Hold"},
    "constructive":    {"min": 70, "max": 79,  "color": "#C9A227", "label": "Constructive Hold"},
    "monitor":         {"min": 60, "max": 69,  "color": "#D89148", "label": "Monitor Closely"},
    "weakening":       {"min": 50, "max": 59,  "color": "#B85A3E", "label": "Confidence Weakening"},
    "review":          {"min": 0,  "max": 49,  "color": "#7A2E1F", "label": "Structural Review Required"},
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


def band_min(
    band: str,
    score_bands: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> Optional[int]:
    rng = band_ranges(score_bands)
    return rng[band][0] if band in rng else None


def format_range(lo: int, hi: int, *, dash: str = "-") -> str:
    return f"{lo}{dash}{hi}"
