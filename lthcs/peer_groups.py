"""Compound peer-key `(maturity_stage, sector_group)` resolver.

Implements Tier 2 #7 from ``docs/lthcs-open-items-audit.md`` — the compound
peer-key system that the audit deferred because "naive split makes AAPL worse."
The prerequisite was a curated Hardware/Software/Other-Tech split; that lives
in ``data/lthcs/peer_groups.json`` and this module is the runtime resolver.

The pipeline (``lthcs_daily.py`` Stage 4) builds a ``peer_growths`` dict keyed
by maturity stage today. With ``peer_groups_config`` plumbed in, pillar callers
(Adoption + Financial) can ask "give me the cohort of tickers that share BOTH
maturity_stage AND sector_group with this ticker" instead of the broader stage-
only cohort. The safety valve falls back when the compound cohort is too thin.

Why this lives at the pillar layer, not the pipeline layer:

* The pipeline already produces a full universe-wide ``peer_growths`` map.
* The pillar is the place that has the focal ticker context AND can decide
  which subset of that universe map to use for percentile-ranking.
* Keeping this here means we don't need to rebuild the maturity-bucketed map
  in the pipeline — we just filter the universe map at percentile-rank time.

Audit references:

* ``docs/peer-group-audit.md`` §3.4 + §4 — the original proposal (rec A').
* ``docs/lthcs-open-items-audit.md`` Tier 2 row 7 — deferred prerequisite.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

__all__ = [
    "DEFAULT_PEER_GROUPS_PATH",
    "DEFAULT_SECTOR_GROUP",
    "DEFAULT_MIN_COHORT_SIZE",
    "load_peer_groups_config",
    "get_sector_group",
    "get_compound_peer_key",
    "get_peer_cohort",
    "get_peer_cohort_with_strategy",
]


# Default path is relative to the repo root: data/lthcs/peer_groups.json.
DEFAULT_PEER_GROUPS_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "lthcs" / "peer_groups.json"
)

# Sentinel returned when a ticker isn't in any curated sector_group.
DEFAULT_SECTOR_GROUP = "other"

# Minimum compound-cohort size before we fall back. The peer_groups.json
# config can override this; this is the safety default if the config doesn't
# specify a ``min_cohort_size`` key.
DEFAULT_MIN_COHORT_SIZE = 6

# Strategy tags surfaced in variable_detail so consumers can see which
# fallback level produced the cohort used.
STRATEGY_COMPOUND = "compound"
STRATEGY_SECTOR_GROUP_ONLY = "sector_group_only"
STRATEGY_MATURITY_ONLY = "maturity_only"
STRATEGY_UNIVERSE_FALLBACK = "universe_fallback"


def load_peer_groups_config(
    path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Load the peer_groups.json config from disk.

    Returns the parsed JSON dict. Callers that want a default config when the
    file is missing should catch ``FileNotFoundError`` themselves — this
    function intentionally fails loudly so misconfiguration is obvious in
    pipeline runs.
    """
    target = Path(path) if path is not None else DEFAULT_PEER_GROUPS_PATH
    with open(target, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _build_ticker_to_group(config: Dict[str, Any]) -> Dict[str, str]:
    """Build a {ticker: sector_group} reverse index from the config dict.

    Defensive: tolerates a config missing ``sector_groups`` (returns empty).
    Ticker matching is case-sensitive on the keys in peer_groups.json — by
    convention the file uses upper-case tickers matching universe.json.
    """
    out: Dict[str, str] = {}
    groups = (config or {}).get("sector_groups") or {}
    for group_name, group in groups.items():
        if not isinstance(group, dict):
            continue
        tickers = group.get("tickers") or []
        for tk in tickers:
            if not isinstance(tk, str):
                continue
            out[tk] = group_name
    return out


def get_sector_group(
    ticker: str,
    peer_groups_config: Dict[str, Any],
) -> str:
    """Return the sector_group key for a ticker.

    Returns ``DEFAULT_SECTOR_GROUP`` ("other") when the ticker isn't curated.
    Ticker matching is case-sensitive — uppercase tickers required to match
    the universe.json convention.
    """
    if not ticker:
        return DEFAULT_SECTOR_GROUP
    reverse = _build_ticker_to_group(peer_groups_config or {})
    return reverse.get(ticker, DEFAULT_SECTOR_GROUP)


def _universe_as_index(
    universe: Any,
) -> Dict[str, Dict[str, Any]]:
    """Coerce either a list-of-dicts (universe.json schema) or a dict-of-dicts
    into a flat ``{ticker: entry}`` index.

    Accepted shapes:

    * ``{"tickers": [{"ticker": "AAPL", ...}, ...]}`` (universe.json file)
    * ``[{"ticker": "AAPL", ...}, ...]`` (raw list)
    * ``{"AAPL": {...}, ...}`` (already indexed)
    """
    if isinstance(universe, dict) and "tickers" in universe:
        rows = universe.get("tickers") or []
    elif isinstance(universe, list):
        rows = universe
    elif isinstance(universe, dict):
        # Already indexed by ticker.
        return {str(k): v for k, v in universe.items() if isinstance(v, dict)}
    else:
        rows = []
    out: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        tk = row.get("ticker")
        if not tk:
            continue
        out[str(tk)] = row
    return out


def get_compound_peer_key(
    ticker: str,
    universe: Any,
    peer_groups_config: Dict[str, Any],
) -> Tuple[str, str]:
    """Return the compound peer key ``(maturity_stage, sector_group)`` for a ticker.

    ``maturity_stage`` comes from the universe entry's ``maturity_stage``
    field (default: ``"mature_compounder"`` if missing). ``sector_group``
    comes from the peer_groups config (default: ``"other"`` if not curated).
    """
    by_ticker = _universe_as_index(universe)
    entry = by_ticker.get(ticker, {}) if ticker else {}
    stage = (entry.get("maturity_stage") if isinstance(entry, dict) else None) or "mature_compounder"
    grp = get_sector_group(ticker, peer_groups_config or {})
    return stage, grp


def get_peer_cohort_with_strategy(
    ticker: str,
    universe: Any,
    peer_groups_config: Dict[str, Any],
    *,
    min_cohort_size: Optional[int] = None,
    candidate_tickers: Optional[List[str]] = None,
) -> Tuple[List[str], str]:
    """Return ``(cohort_tickers, strategy)`` for a focal ticker.

    The strategy tag is one of:

    * ``"compound"`` — both ``maturity_stage`` AND ``sector_group`` matched.
    * ``"sector_group_only"`` — compound cohort was too small; fell back to
      every maturity stage within the same sector_group.
    * ``"maturity_only"`` — sector_group cohort also too small; fell back to
      the legacy maturity-stage-only cohort (current behaviour).
    * ``"universe_fallback"`` — even the maturity-only cohort was too small;
      use the full candidate set.

    ``candidate_tickers`` lets callers restrict the cohort to tickers for
    which they actually have data (e.g. only tickers with a non-None
    revenue_growth). When omitted, every ticker in the universe is a
    candidate. Either way the returned cohort INCLUDES the focal ticker —
    the caller decides whether to exclude self before percentile-ranking
    (the pillars already do via ``include_self=False``).
    """
    if min_cohort_size is None:
        min_cohort_size = int(
            (peer_groups_config or {}).get("min_cohort_size", DEFAULT_MIN_COHORT_SIZE)
        )

    by_ticker = _universe_as_index(universe)
    reverse = _build_ticker_to_group(peer_groups_config or {})

    # The "candidates" universe — tickers we're allowed to consider. When the
    # caller passes a restricted list (e.g. only tickers with valid growth
    # data), we filter to that intersection. Default = every universe entry.
    if candidate_tickers is None:
        candidates = set(by_ticker.keys())
    else:
        candidates = {tk for tk in candidate_tickers if tk}

    focal_stage, focal_group = get_compound_peer_key(
        ticker, universe, peer_groups_config or {}
    )

    # Level 1: compound key (stage AND sector_group).
    compound = [
        tk for tk in candidates
        if (by_ticker.get(tk, {}) or {}).get("maturity_stage") == focal_stage
        and reverse.get(tk, DEFAULT_SECTOR_GROUP) == focal_group
    ]
    if len(compound) >= min_cohort_size:
        return sorted(compound), STRATEGY_COMPOUND

    # Level 2: sector_group only (any maturity stage in the same group).
    sector_only = [
        tk for tk in candidates
        if reverse.get(tk, DEFAULT_SECTOR_GROUP) == focal_group
    ]
    if len(sector_only) >= min_cohort_size:
        return sorted(sector_only), STRATEGY_SECTOR_GROUP_ONLY

    # Level 3: maturity_stage only (legacy behaviour).
    maturity_only = [
        tk for tk in candidates
        if (by_ticker.get(tk, {}) or {}).get("maturity_stage") == focal_stage
    ]
    if len(maturity_only) >= min_cohort_size:
        return sorted(maturity_only), STRATEGY_MATURITY_ONLY

    # Level 4: universe fallback — last resort.
    return sorted(candidates), STRATEGY_UNIVERSE_FALLBACK


def get_peer_cohort(
    ticker: str,
    universe: Any,
    peer_groups_config: Dict[str, Any],
    *,
    min_cohort_size: Optional[int] = None,
    candidate_tickers: Optional[List[str]] = None,
) -> List[str]:
    """Thin wrapper around :func:`get_peer_cohort_with_strategy`.

    Returns just the cohort list. Most callers want the strategy too so they
    can surface it in ``variable_detail`` — prefer the ``_with_strategy``
    flavour for those paths.
    """
    cohort, _strategy = get_peer_cohort_with_strategy(
        ticker,
        universe,
        peer_groups_config or {},
        min_cohort_size=min_cohort_size,
        candidate_tickers=candidate_tickers,
    )
    return cohort
