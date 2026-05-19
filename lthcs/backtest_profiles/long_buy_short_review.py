"""``long_buy_short_review`` — dollar-neutral via band membership.

Long the Buy bands (elite / high_confidence / constructive), short the
Review band. Both legs are equal-weighted internally; the engine returns
``mean(long_returns) - mean(short_returns)``. Round-trip costs are
charged to both legs.

This profile probes whether the *spread* between the highest- and
lowest-banded names is actually tradable after costs -- the existing
quintile-spread IC chart is suggestive but doesn't pay costs on each
rebalance.
"""

from __future__ import annotations

from lthcs.backtest_engine import DEFAULT_LONG_BANDS, EngineParams


PROFILE_NAME = "long_buy_short_review"


def build():
    from lthcs.backtest_profiles import StrategyProfile

    params = EngineParams(
        bands_long=list(DEFAULT_LONG_BANDS),
        bands_short=["review"],
        top_k=0,
        short_bottom_quintile=False,
        cost_bps=5.0,
        delay_trading_days=1,
        profile_name=PROFILE_NAME,
    )
    return StrategyProfile(
        name=PROFILE_NAME,
        description=(
            "Long Buy bands (elite / high_confidence / constructive), "
            "short the Review band. Dollar-neutral within each leg. "
            "5 bps/side charged on entry + exit of both legs."
        ),
        params=params,
        requires_score_history=False,
    )
