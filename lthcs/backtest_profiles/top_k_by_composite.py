"""``top_k_by_composite`` — hold the top K composite-score names daily.

Ignores band membership entirely. Each day the long leg is the top K
tickers by composite score (NaN-safe; ties broken by ticker name for
stability). K defaults to 20 per the spec but the factory accepts an
override for tuning runs.
"""

from __future__ import annotations

from lthcs.backtest_engine import EngineParams


PROFILE_NAME = "top_k_by_composite"
DEFAULT_K = 20


def build(k: int = DEFAULT_K):
    from lthcs.backtest_profiles import StrategyProfile

    params = EngineParams(
        bands_long=[],  # band-agnostic; selection is by composite score
        bands_short=[],
        top_k=int(k),
        short_bottom_quintile=False,
        cost_bps=5.0,
        delay_trading_days=1,
        profile_name=PROFILE_NAME,
    )
    return StrategyProfile(
        name=PROFILE_NAME,
        description=(
            "Hold the top %d composite-score names each day, ignoring "
            "band membership. Long-only; equal-weighted." % int(k)
        ),
        params=params,
        requires_score_history=True,
    )
