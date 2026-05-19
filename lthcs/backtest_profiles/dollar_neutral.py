"""``dollar_neutral`` — Buy bands long, bottom composite quintile short.

The "long Buy bands" set is the same as ``long_only_buy``. The short
leg is selected daily as the bottom 20% of the composite-score panel
(NaN-safe, deduped against the long leg). Both legs equal-weighted
internally; engine returns ``mean(long) - mean(short)``.

This profile needs ``score_history`` plumbed into ``run_backtest`` --
``backtest_profiles.StrategyProfile.requires_score_history`` is True.
"""

from __future__ import annotations

from lthcs.backtest_engine import DEFAULT_LONG_BANDS, EngineParams


PROFILE_NAME = "dollar_neutral"


def build():
    from lthcs.backtest_profiles import StrategyProfile

    params = EngineParams(
        bands_long=list(DEFAULT_LONG_BANDS),
        bands_short=[],  # short leg comes from the quintile rule, not bands
        top_k=0,
        short_bottom_quintile=True,
        cost_bps=5.0,
        delay_trading_days=1,
        profile_name=PROFILE_NAME,
    )
    return StrategyProfile(
        name=PROFILE_NAME,
        description=(
            "Long the Buy bands, short the bottom composite-score "
            "quintile each day. Score-history driven; dollar-neutral "
            "within each leg."
        ),
        params=params,
        requires_score_history=True,
    )
