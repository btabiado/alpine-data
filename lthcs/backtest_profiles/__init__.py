"""LTHCS strategy profiles for the backtest engine (Tier 5 #24, Phase 3).

A *profile* is a named bundle of :class:`lthcs.backtest_engine.EngineParams`
plus a few engine-runtime flags (whether ``score_history`` is required,
whether the engine should treat this as a market-neutral run, etc.).
Profiles let the CLI / nightly cron pick a strategy by name without
plumbing per-strategy keyword arguments through every layer.

Currently shipped profiles (per
``docs/lthcs-backtest-engine-spec.md`` §8):

- ``long_only_buy`` — baseline, mirrors Phase 1 (long the Buy bands).
- ``long_buy_short_review`` — long Buy bands, short the Review band;
  dollar-neutral within each leg.
- ``dollar_neutral`` — long Buy bands, short the bottom composite-score
  quintile each day.
- ``top_k_by_composite`` — long the top K composite-score names each day
  regardless of band (default K=20).

A profile is a callable that returns a :class:`StrategyProfile`. The
registry maps profile name -> factory so JSON profiles can be added
later by registering a loader.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from lthcs.backtest_engine import EngineParams

# Re-exports for convenience.
from lthcs.backtest_profiles.long_only_buy import build as build_long_only_buy
from lthcs.backtest_profiles.long_buy_short_review import (
    build as build_long_buy_short_review,
)
from lthcs.backtest_profiles.dollar_neutral import build as build_dollar_neutral
from lthcs.backtest_profiles.top_k_by_composite import (
    build as build_top_k_by_composite,
)


@dataclass
class StrategyProfile:
    """One named strategy variant the engine knows how to run.

    Attributes
    ----------
    name : str
        Stable identifier, e.g. ``"long_only_buy"``. Matches the file
        name under ``lthcs/backtest_profiles/``.
    description : str
        One-sentence summary; surfaces in reports / the UI.
    params : EngineParams
        Pre-built params dataclass. ``params.profile_name`` is set to
        ``name`` so it round-trips into ``run_meta``.
    requires_score_history : bool
        True if the simulation needs the composite-score panel (top_k /
        bottom-quintile profiles). The CLI uses this to decide whether
        to pass ``score_history`` to ``run_backtest``.
    """

    name: str
    description: str
    params: EngineParams
    requires_score_history: bool = False


ProfileFactory = Callable[[], StrategyProfile]


_REGISTRY: Dict[str, ProfileFactory] = {
    "long_only_buy": build_long_only_buy,
    "long_buy_short_review": build_long_buy_short_review,
    "dollar_neutral": build_dollar_neutral,
    "top_k_by_composite": build_top_k_by_composite,
}


def available_profiles() -> List[str]:
    """Return the list of registered profile names."""
    return sorted(_REGISTRY.keys())


def get_profile(name: str) -> StrategyProfile:
    """Load a registered strategy profile by name.

    Raises ``KeyError`` with a helpful list if ``name`` is unknown.
    """
    if name not in _REGISTRY:
        raise KeyError(
            "Unknown profile %r. Available: %s"
            % (name, ", ".join(available_profiles()))
        )
    return _REGISTRY[name]()


def register_profile(name: str, factory: ProfileFactory) -> None:
    """Register a custom profile factory (e.g. from a JSON loader).

    Intentionally permissive: overwrites silently so tests can re-bind
    names without leaking state between cases.
    """
    _REGISTRY[name] = factory


__all__ = [
    "StrategyProfile",
    "available_profiles",
    "get_profile",
    "register_profile",
    "build_long_only_buy",
    "build_long_buy_short_review",
    "build_dollar_neutral",
    "build_top_k_by_composite",
]
