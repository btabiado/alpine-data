"""``top_k_by_composite`` — hold the top K composite-score names daily.

Ignores band membership entirely. Each day the long leg is the top K
tickers by composite score (NaN-safe; ties broken by ticker name for
stability). K defaults to 20 per the spec, and a K-sweep on the
2026-02-17..2026-05-18 validation window (167-name universe) confirmed
K=20 as the risk-adjusted optimum:

    K     total_return   Sharpe   Sortino   turnover/day   MDD
    5     +0.189         +2.51    +2.55     0.153          -0.142
    10    +0.114         +1.99    +2.06     0.205          -0.107
    15    +0.109         +2.15    +2.23     0.207          -0.103
    20    +0.126         +2.80    +3.17     0.188          -0.096   <- chosen
    25    +0.098         +2.41    +2.77     0.158          -0.089
    30    +0.099         +2.39    +2.71     0.156          -0.087
    40    +0.057         +1.51    +1.64     0.118          -0.094
    50    +0.033         +0.98    +1.05     0.127          -0.089
    167   +0.034         +1.08    n/a       0.031          -0.074

K=5 has higher absolute return but a worse Sharpe and a 14% drawdown;
K=20 sits at the Sharpe/Sortino peak with the smallest MDD in the
small-K cohort, so we keep it as the production default. See
``docs/lthcs-topk-profile-investigation.md`` and
``data/lthcs/backtest/2026-05-18_validation/profiles/top_k_sweep/``
for the raw numbers. The factory still accepts an override for tuning
runs.
"""

from __future__ import annotations

from lthcs.backtest_engine import EngineParams


PROFILE_NAME = "top_k_by_composite"
# Default K=20 selected by K-sweep on the 2026-05-18 validation window
# (Sharpe peak, Sortino peak, smallest MDD inside the small-K cohort).
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
