"""Declarative peer-cohort configuration loader for LTHCS scoring.

This module is the **loader / orchestration layer** that consumes
``data/lthcs/peer_groups.json``.  The JSON file is split into two
co-owned sections:

* ``sector_groups`` — curated ticker lists per sector_group key
  (e.g. ``tech_hardware``, ``healthcare_pharma``, ``banks``).
  Owned by Tier 2 #7; the loader here treats it as read-only.
* ``pillar_strategies`` — per-pillar peer-cohort *strategy*
  declarations (``compound`` / ``sector_group_only`` /
  ``maturity_only`` / ``universe``) with an optional
  ``fallback_chain`` and ``min_cohort_size`` guardrail.
* ``active_overrides.enabled`` — global kill-switch.  When
  ``False`` (the V1 default) every pillar keeps its current
  hard-coded grouping.  When ``True`` the daily pipeline routes
  peer-cohort lookups through this config.

Cohort resolution honours the strategy + fallback chain:

* ``compound``           — intersect ``sector_group`` ∩ ``maturity_stage``
* ``sector_group_only``  — every ticker in the focal sector_group
* ``maturity_only``      — every ticker that shares the focal maturity_stage
* ``universe``           — the full universe ticker list

If a strategy resolves to a cohort below ``min_cohort_size`` the loader
walks the ``fallback_chain`` in order, returning the first cohort that
clears the floor.  If none do, the universe is returned with strategy
label ``universe_fallback``.

The focal ticker is always excluded from the returned cohort so callers
can drop the value straight into ``peer_relative_percentile`` with
``include_self=False`` semantics.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

# Tier 2 #7 ships ``lthcs.peer_groups`` with ``get_peer_cohort`` /
# ``get_peer_cohort_with_strategy`` helpers that implement a hard-coded
# fallback chain (compound -> sector_group_only -> maturity_only ->
# universe_fallback).  This loader is intentionally configuration-driven
# instead so a Bryan-authored ``fallback_chain`` in peer_groups.json can
# diverge from #7's default (e.g. skip ``maturity_only``).  We import the
# module only for the side effect of asserting it lives in the package
# (and to keep the docstring's promise to "use" it discoverable);
# concrete cohort math lives below for full config-driven control.
try:
    from lthcs import peer_groups as _peer_groups_module  # noqa: F401
except ImportError:  # pragma: no cover - exercised only pre-#7-landing
    _peer_groups_module = None  # type: ignore[assignment]

__all__ = [
    "DEFAULT_PEER_GROUPS_PATH",
    "DEFAULT_STRATEGY",
    "VALID_STRATEGIES",
    "VALID_FALLBACK_STEPS",
    "load_peer_groups_config",
    "get_pillar_strategy",
    "is_active",
    "resolve_cohort",
]


DEFAULT_PEER_GROUPS_PATH: Path = (
    Path(__file__).resolve().parent.parent
    / "data"
    / "lthcs"
    / "peer_groups.json"
)

# Default strategy applied to any pillar not explicitly configured.
DEFAULT_STRATEGY: Dict[str, Any] = {"strategy": "universe"}

VALID_STRATEGIES = frozenset(
    {"compound", "sector_group_only", "maturity_only", "universe"}
)

# Steps allowed in fallback_chain.  These are a superset of
# VALID_STRATEGIES so that pillars CAN'T compound-fallback-to-compound
# (which would be a no-op) but CAN fall to a simpler key or to universe.
VALID_FALLBACK_STEPS = frozenset(
    {"sector_group_only", "maturity_only", "universe"}
)

_REQUIRED_TOP_LEVEL_KEYS = ("sector_groups", "pillar_strategies")


# ---------------------------------------------------------------------------
# Loader + schema validation
# ---------------------------------------------------------------------------


def load_peer_groups_config(path: Optional[Path] = None) -> Dict[str, Any]:
    """Load and validate the peer-groups configuration file.

    Parameters
    ----------
    path:
        Optional override.  Defaults to ``DEFAULT_PEER_GROUPS_PATH``
        (i.e. ``<repo>/data/lthcs/peer_groups.json``).

    Returns
    -------
    dict
        The parsed JSON document.  Schema validation is shallow: we
        require ``sector_groups`` and ``pillar_strategies`` to exist
        and each pillar block to carry a ``strategy`` key.

    Raises
    ------
    FileNotFoundError
        If the config file does not exist.
    ValueError
        If the JSON is malformed, required top-level keys are missing,
        or any pillar strategy is unknown.
    """

    cfg_path = Path(path) if path is not None else DEFAULT_PEER_GROUPS_PATH
    if not cfg_path.exists():
        raise FileNotFoundError(
            f"peer_groups.json not found at {cfg_path}"
        )

    try:
        with cfg_path.open("r", encoding="utf-8") as fh:
            cfg = json.load(fh)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"peer_groups.json at {cfg_path} is not valid JSON: {exc}"
        ) from exc

    if not isinstance(cfg, dict):
        raise ValueError(
            f"peer_groups.json must be a JSON object at top level "
            f"(got {type(cfg).__name__})"
        )

    for key in _REQUIRED_TOP_LEVEL_KEYS:
        if key not in cfg:
            raise ValueError(
                f"peer_groups.json missing required top-level key: '{key}'"
            )

    sector_groups = cfg["sector_groups"]
    if not isinstance(sector_groups, dict):
        raise ValueError(
            "peer_groups.json: 'sector_groups' must be a JSON object"
        )

    pillar_strategies = cfg["pillar_strategies"]
    if not isinstance(pillar_strategies, dict):
        raise ValueError(
            "peer_groups.json: 'pillar_strategies' must be a JSON object"
        )

    for pillar_name, strat in pillar_strategies.items():
        if not isinstance(strat, dict):
            raise ValueError(
                f"peer_groups.json: pillar_strategies['{pillar_name}'] "
                f"must be a JSON object (got {type(strat).__name__})"
            )
        strategy = strat.get("strategy")
        if strategy not in VALID_STRATEGIES:
            raise ValueError(
                f"peer_groups.json: pillar_strategies['{pillar_name}'].strategy "
                f"= {strategy!r} is not one of {sorted(VALID_STRATEGIES)}"
            )
        chain = strat.get("fallback_chain", [])
        if not isinstance(chain, list):
            raise ValueError(
                f"peer_groups.json: pillar_strategies['{pillar_name}'].fallback_chain "
                f"must be a list (got {type(chain).__name__})"
            )
        for step in chain:
            if step not in VALID_FALLBACK_STEPS:
                raise ValueError(
                    f"peer_groups.json: pillar_strategies['{pillar_name}'] "
                    f"fallback_chain step {step!r} is not one of "
                    f"{sorted(VALID_FALLBACK_STEPS)}"
                )

    return cfg


def get_pillar_strategy(
    pillar_name: str, config: Mapping[str, Any]
) -> Dict[str, Any]:
    """Return the strategy block for ``pillar_name``.

    Falls back to ``DEFAULT_STRATEGY`` (``{"strategy": "universe"}``) if
    the pillar is not configured.  The returned dict is a shallow copy
    so callers can mutate it freely.
    """

    pillars = config.get("pillar_strategies") or {}
    block = pillars.get(pillar_name)
    if not isinstance(block, dict):
        return dict(DEFAULT_STRATEGY)
    return dict(block)


def is_active(config: Mapping[str, Any]) -> bool:
    """Return whether the declarative config should be honoured.

    Defaults to ``False`` when ``active_overrides`` is missing or
    malformed so the V1 hard-coded grouping is preserved.
    """

    overrides = config.get("active_overrides")
    if not isinstance(overrides, dict):
        return False
    return bool(overrides.get("enabled", False))


# ---------------------------------------------------------------------------
# Cohort resolution
# ---------------------------------------------------------------------------


def _normalise_universe(universe: Any) -> List[Dict[str, Any]]:
    """Accept universe-as-list-of-dicts OR the full ``universe.json`` dict."""

    if isinstance(universe, dict) and "tickers" in universe:
        tickers = universe.get("tickers") or []
    elif isinstance(universe, list):
        tickers = universe
    else:
        tickers = []
    return [t for t in tickers if isinstance(t, dict)]


def _ticker_to_sector_group(
    sector_groups: Mapping[str, Any],
) -> Dict[str, str]:
    """Invert the sector_groups block into ticker -> group-key map."""

    out: Dict[str, str] = {}
    for group_name, block in (sector_groups or {}).items():
        if not isinstance(block, dict):
            continue
        for sym in block.get("tickers") or []:
            if isinstance(sym, str):
                out[sym.upper()] = group_name
    return out


def _cohort_sector_group(
    ticker: str,
    tickers_in_universe: List[str],
    ticker_to_group: Mapping[str, str],
) -> List[str]:
    grp = ticker_to_group.get(ticker.upper())
    if not grp:
        return []
    return [
        sym
        for sym in tickers_in_universe
        if sym.upper() != ticker.upper()
        and ticker_to_group.get(sym.upper()) == grp
    ]


def _cohort_maturity(
    ticker: str,
    by_ticker: Mapping[str, Mapping[str, Any]],
) -> List[str]:
    focal = by_ticker.get(ticker.upper()) or {}
    stage = focal.get("maturity_stage")
    if not stage:
        return []
    return [
        sym
        for sym, entry in by_ticker.items()
        if sym.upper() != ticker.upper()
        and (entry or {}).get("maturity_stage") == stage
    ]


def _cohort_compound(
    ticker: str,
    by_ticker: Mapping[str, Mapping[str, Any]],
    ticker_to_group: Mapping[str, str],
) -> List[str]:
    focal = by_ticker.get(ticker.upper()) or {}
    stage = focal.get("maturity_stage")
    grp = ticker_to_group.get(ticker.upper())
    if not stage or not grp:
        return []
    return [
        sym
        for sym, entry in by_ticker.items()
        if sym.upper() != ticker.upper()
        and (entry or {}).get("maturity_stage") == stage
        and ticker_to_group.get(sym.upper()) == grp
    ]


def _cohort_universe(
    ticker: str, tickers_in_universe: List[str]
) -> List[str]:
    return [
        sym for sym in tickers_in_universe if sym.upper() != ticker.upper()
    ]


def _cohort_bank(
    ticker: str, sector_groups: Mapping[str, Any]
) -> List[str]:
    """Return the bank cohort (minus the focal ticker)."""

    bank_block = (sector_groups or {}).get("banks") or {}
    syms = bank_block.get("tickers") or []
    return [
        sym
        for sym in syms
        if isinstance(sym, str) and sym.upper() != ticker.upper()
    ]


def _by_ticker(universe_list: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for entry in universe_list:
        sym = entry.get("ticker")
        if isinstance(sym, str):
            out[sym.upper()] = entry
    return out


_STRATEGY_LABEL_MAP = {
    "compound": "compound",
    "sector_group_only": "sector_group_only",
    "maturity_only": "maturity_only",
    "universe": "universe",
}


def _resolve_step(
    step: str,
    *,
    ticker: str,
    by_ticker: Mapping[str, Mapping[str, Any]],
    tickers_in_universe: List[str],
    ticker_to_group: Mapping[str, str],
) -> List[str]:
    if step == "compound":
        return _cohort_compound(ticker, by_ticker, ticker_to_group)
    if step == "sector_group_only":
        return _cohort_sector_group(ticker, tickers_in_universe, ticker_to_group)
    if step == "maturity_only":
        return _cohort_maturity(ticker, by_ticker)
    if step == "universe":
        return _cohort_universe(ticker, tickers_in_universe)
    return []


def resolve_cohort(
    ticker: str,
    pillar_name: str,
    universe: Any,
    config: Mapping[str, Any],
) -> Tuple[List[str], str]:
    """Resolve the peer cohort for ``ticker`` under ``pillar_name``.

    Parameters
    ----------
    ticker:
        Focal ticker (case-insensitive).  Excluded from the returned
        cohort.
    pillar_name:
        Key into ``pillar_strategies``.  Unknown pillars fall back to
        ``universe`` strategy.
    universe:
        Either the ``universe.json`` dict (with a ``"tickers"`` list) or
        a bare list of ticker entries.  Each entry must carry at least
        ``ticker`` and (ideally) ``maturity_stage``.
    config:
        Loaded ``peer_groups.json`` mapping.

    Returns
    -------
    (cohort_tickers, strategy_used)
        ``strategy_used`` is one of ``compound``, ``sector_group_only``,
        ``maturity_only``, ``universe`` (when the pillar's primary
        strategy is universe), ``universe_fallback`` (when every
        configured step underflowed and we ended at universe), or
        ``bank_cohort`` (when ``bank_override`` fired).
    """

    universe_list = _normalise_universe(universe)
    tickers_in_universe = [
        t["ticker"] for t in universe_list if isinstance(t.get("ticker"), str)
    ]
    by_tick = _by_ticker(universe_list)
    sector_groups = config.get("sector_groups") or {}
    ticker_to_group = _ticker_to_sector_group(sector_groups)

    strat = get_pillar_strategy(pillar_name, config)
    strategy = strat.get("strategy", "universe")
    min_cohort_size = int(strat.get("min_cohort_size", 0) or 0)

    # Bank override fires BEFORE the strategy resolution: it is the
    # contract for financial_evolution (banks always get the bank
    # cohort).
    bank_override = strat.get("bank_override")
    if bank_override == "bank_cohort":
        bank_block = sector_groups.get("banks") or {}
        bank_tickers = {
            sym.upper()
            for sym in (bank_block.get("tickers") or [])
            if isinstance(sym, str)
        }
        if ticker.upper() in bank_tickers:
            return _cohort_bank(ticker, sector_groups), "bank_cohort"

    # Primary strategy first, then fallback_chain in order.
    chain: List[str] = [strategy]
    chain.extend(strat.get("fallback_chain") or [])

    last_cohort: List[str] = []
    last_label = "universe"
    for idx, step in enumerate(chain):
        cohort = _resolve_step(
            step,
            ticker=ticker,
            by_ticker=by_tick,
            tickers_in_universe=tickers_in_universe,
            ticker_to_group=ticker_to_group,
        )
        last_cohort = cohort
        last_label = _STRATEGY_LABEL_MAP.get(step, step)
        if len(cohort) >= min_cohort_size or step == "universe":
            # Universe is the terminal step — accept it even if below
            # min_cohort_size (it's already the biggest available pool).
            if idx > 0 and step == "universe":
                last_label = "universe_fallback"
            return cohort, last_label

    # Chain exhausted without hitting min_cohort_size and never reaching
    # universe.  Fall through to a universe rescue.
    cohort = _cohort_universe(ticker, tickers_in_universe)
    return cohort, "universe_fallback"
