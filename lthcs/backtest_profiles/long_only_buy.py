"""``long_only_buy`` — baseline LTHCS strategy profile.

Mirrors Phase 1 of ``docs/lthcs-backtest-engine-spec.md`` §3: long the
Buy bands (elite, high_confidence, constructive), 5 bps per side,
1-day execution delay, daily equal-weight rebalance. Formalised here so
the CLI can pick "the default" by name without duplicating defaults.
"""

from __future__ import annotations

from lthcs.backtest_engine import DEFAULT_LONG_BANDS, EngineParams


PROFILE_NAME = "long_only_buy"


def build():
    """Return a :class:`~lthcs.backtest_profiles.StrategyProfile`."""
    # Local import to avoid a circular reference with the package init.
    from lthcs.backtest_profiles import StrategyProfile

    params = EngineParams(
        bands_long=list(DEFAULT_LONG_BANDS),
        bands_short=[],
        top_k=0,
        short_bottom_quintile=False,
        cost_bps=5.0,
        delay_trading_days=1,
        profile_name=PROFILE_NAME,
    )
    return StrategyProfile(
        name=PROFILE_NAME,
        description=(
            "Long-only baseline: hold elite / high_confidence / "
            "constructive bands; sell on exit. 5 bps/side, 1-day delay."
        ),
        params=params,
        requires_score_history=False,
    )
