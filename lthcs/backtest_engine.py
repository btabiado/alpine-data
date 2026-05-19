"""LTHCS event-driven backtest engine — Phase 1 (Tier 5 #24).

Companion to ``lthcs/backtest.py`` (IC + quintile validator). The IC
side is honest about cross-sectional rank correlation but the existing
``band_portfolio_returns`` uses overlapping forward returns, inflating
Sharpe roughly h-fold (see ``data/lthcs/backtest/2026-05-18_validation/
report.md`` and the ``+18.7 Sharpe`` headline). This engine replaces
that section with a *non-overlapping* simulated trading P&L.

Phase 1 strategy (per ``docs/lthcs-backtest-engine-spec.md`` §3):

- Universe: any ticker present in both ``band_history`` and ``prices``.
- Entry: ticker first appears in a Buy band -> buy at next trading-day
  close.
- Exit: ticker drops out of the Buy set -> sell at next trading-day
  close.
- Sizing: equal-weight across currently-held names. Daily intra-portfolio
  drift is folded back to equal weight on the close, but the model only
  charges costs on entry and exit (the rebalance churn is assumed
  cost-free, otherwise the trades.csv schema breaks).
- Costs: ``cost_bps`` each side (default 5 bps).
- Slippage: 1 trading-day delay between snapshot date and trade execution.
  This is the look-ahead guard. Implemented by reading the band history
  forward-filled and then shifted by one trading day -- ``target_bands``
  at trading day ``t`` reflect the snapshot that was published by close
  of ``t-1``.
- Initial capital: 1.0 normalized.
- Cash: idle capital earns 0%.

The engine is *pure* with respect to the filesystem: it takes two
panels + a params dict and returns the artifact dict. The CLI wrapper
in ``scripts/lthcs_backtest.py`` is responsible for loading panels and
writing outputs.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

import numpy as np
import pandas as pd


TRADING_DAYS_PER_YEAR = 252

DEFAULT_LONG_BANDS = ["elite", "high_confidence", "constructive"]

# Band ordering used for the per-band sub-portfolio sweep. Higher bands
# come first so the UI legend lines up with the headline narrative.
ALL_BANDS_FOR_SWEEP = [
    "elite",
    "high_confidence",
    "constructive",
    "monitor",
    "weakening",
    "review",
]


# ---------------------------------------------------------------------------
# Params
# ---------------------------------------------------------------------------

@dataclass
class EngineParams:
    """Strategy + execution parameters for one engine run.

    Defaults match Phase 1 (long-only Buy band, 5bps each side, 1d delay,
    daily equal-weight). Holding fields as a dataclass instead of a free
    dict lets ``params_hash`` be stable for the cache key.

    Phase 3 extensions:

    - ``bands_short``: optional set of bands that form the short leg of a
      market-neutral portfolio. When non-empty, daily return becomes
      ``mean(long_returns) - mean(short_returns)`` (dollar-neutral within
      each leg). Round-trip costs are charged to both legs.
    - ``short_bottom_quintile``: when True, the short leg is the bottom
      composite-score quintile each day (selected by ``score_history``).
      Used by the ``dollar_neutral`` profile.
    - ``top_k``: when > 0, the long leg is the top ``top_k`` tickers by
      composite score each day, ignoring band membership. Used by the
      ``top_k_by_composite`` profile.
    - ``profile_name``: optional tag carried in ``run_meta`` so artifacts
      record which named profile produced them.
    """

    bands_long: List[str] = field(default_factory=lambda: list(DEFAULT_LONG_BANDS))
    cost_bps: float = 5.0
    delay_trading_days: int = 1
    initial_capital: float = 1.0
    # When True, the per-day equal-weight rebalance is enforced.
    # Phase 1 always rebalances daily so this flag exists mostly for
    # future profiles (e.g. monthly-rebalance variants in Phase 3).
    rebalance_daily: bool = True
    # Phase 3 — strategy variants.
    bands_short: List[str] = field(default_factory=list)
    short_bottom_quintile: bool = False
    top_k: int = 0
    profile_name: Optional[str] = None

    def normalized_long_set(self) -> set:
        return {b.strip().lower() for b in self.bands_long if b and b.strip()}

    def normalized_short_set(self) -> set:
        return {b.strip().lower() for b in self.bands_short if b and b.strip()}

    @property
    def has_short_leg(self) -> bool:
        return bool(self.normalized_short_set()) or bool(self.short_bottom_quintile)

    @property
    def uses_top_k(self) -> bool:
        return int(self.top_k) > 0

    def to_jsonable(self) -> Dict[str, Any]:
        return {
            "bands_long": list(self.bands_long),
            "cost_bps": float(self.cost_bps),
            "delay_trading_days": int(self.delay_trading_days),
            "initial_capital": float(self.initial_capital),
            "rebalance_daily": bool(self.rebalance_daily),
            "bands_short": list(self.bands_short),
            "short_bottom_quintile": bool(self.short_bottom_quintile),
            "top_k": int(self.top_k),
            "profile_name": self.profile_name,
        }


def _params_hash(params: EngineParams) -> str:
    payload = json.dumps(params.to_jsonable(), sort_keys=True).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def _frame_hash(df: pd.DataFrame) -> str:
    """Stable short hash of a wide DataFrame's index, columns, and values."""
    if df is None or df.empty:
        return "empty"
    h = hashlib.sha256()
    h.update(str(sorted(map(str, df.columns))).encode("utf-8"))
    h.update(str([str(i) for i in df.index]).encode("utf-8"))
    # Hash a downcasted byte view of the values. Use a fixed dtype so
    # the hash is deterministic across pandas versions.
    try:
        arr = df.to_numpy(dtype=object, copy=False)
        h.update(str(arr.tolist()).encode("utf-8"))
    except Exception:
        h.update(repr(df.values).encode("utf-8"))
    return h.hexdigest()[:16]


# ---------------------------------------------------------------------------
# Panel preparation
# ---------------------------------------------------------------------------

def _align_bands_to_trading_days(
    band_history: pd.DataFrame,
    trading_days: Sequence[pd.Timestamp],
    delay_trading_days: int,
) -> pd.DataFrame:
    """Return a (trading_days x ticker) frame of *target* bands per day.

    The output at row ``t`` is the band signal that we *act on* at the
    close of trading day ``t``. With ``delay_trading_days == 1`` this is
    the snapshot from trading day ``t - 1`` (the look-ahead guard).
    Calendar snapshots are forward-filled onto the trading-day index
    first, then shifted by ``delay_trading_days`` rows.
    """
    if band_history is None or band_history.empty or not trading_days:
        return pd.DataFrame(index=pd.DatetimeIndex(list(trading_days)))

    bh = band_history.copy()
    bh.index = pd.to_datetime(bh.index)
    bh = bh.sort_index()

    td_idx = pd.DatetimeIndex(sorted(trading_days))
    union = bh.index.union(td_idx).sort_values()
    aligned = bh.reindex(union).ffill()
    on_td = aligned.loc[aligned.index.isin(td_idx)]
    on_td = on_td.reindex(td_idx)

    if delay_trading_days > 0:
        on_td = on_td.shift(delay_trading_days)
    return on_td


def _align_scores_to_trading_days(
    score_history: pd.DataFrame,
    trading_days: Sequence[pd.Timestamp],
    delay_trading_days: int,
) -> pd.DataFrame:
    """Align a date-indexed composite score panel to the trading-day index.

    Same delay/shift treatment as :func:`_align_bands_to_trading_days` --
    the row at trading day ``t`` is the score signal acted on at the close
    of ``t`` (i.e. the snapshot from ``t - delay_trading_days``). Used by
    the top-K and bottom-quintile profiles.
    """
    if score_history is None or score_history.empty or not trading_days:
        return pd.DataFrame(index=pd.DatetimeIndex(list(trading_days)))

    sh = score_history.copy()
    sh.index = pd.to_datetime(sh.index)
    sh = sh.sort_index()

    td_idx = pd.DatetimeIndex(sorted(trading_days))
    union = sh.index.union(td_idx).sort_values()
    aligned = sh.reindex(union).ffill()
    on_td = aligned.loc[aligned.index.isin(td_idx)]
    on_td = on_td.reindex(td_idx)

    if delay_trading_days > 0:
        on_td = on_td.shift(delay_trading_days)
    return on_td


def _prices_to_trading_index(prices: pd.DataFrame) -> pd.DataFrame:
    if prices is None or prices.empty:
        return pd.DataFrame()
    p = prices.copy()
    p.index = pd.to_datetime(p.index)
    p = p.sort_index()
    p = p[~p.index.duplicated(keep="last")]
    return p


def _to_iso_date(ts: pd.Timestamp) -> str:
    if isinstance(ts, str):
        return ts
    return pd.Timestamp(ts).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Stat helpers
# ---------------------------------------------------------------------------

def _max_drawdown(equity: pd.Series) -> float:
    if equity is None or equity.empty:
        return 0.0
    peak = equity.cummax()
    dd = equity / peak - 1.0
    return float(dd.min())


def _annualized_sharpe(daily_returns: pd.Series) -> float:
    if daily_returns is None or daily_returns.empty:
        return 0.0
    r = daily_returns.dropna()
    if r.empty:
        return 0.0
    std = r.std(ddof=1) if len(r) > 1 else 0.0
    if not std or math.isnan(std) or std == 0.0:
        return 0.0
    return float(r.mean() / std * math.sqrt(TRADING_DAYS_PER_YEAR))


def _annualized_sortino(daily_returns: pd.Series) -> float:
    if daily_returns is None or daily_returns.empty:
        return 0.0
    r = daily_returns.dropna()
    if r.empty:
        return 0.0
    neg = r[r < 0.0]
    if len(neg) < 2:
        return 0.0
    downside = float(np.sqrt((neg ** 2).sum() / len(neg)))
    if downside == 0.0 or math.isnan(downside):
        return 0.0
    return float(r.mean() / downside * math.sqrt(TRADING_DAYS_PER_YEAR))


def _hit_rate(daily_returns: pd.Series) -> float:
    if daily_returns is None or daily_returns.empty:
        return 0.0
    r = daily_returns.dropna()
    if r.empty:
        return 0.0
    return float((r > 0).sum() / len(r))


def _annualized_return(equity: pd.Series, trading_days_per_year: int = TRADING_DAYS_PER_YEAR) -> float:
    if equity is None or equity.empty or len(equity) < 2:
        return 0.0
    n = len(equity)
    total = float(equity.iloc[-1] / equity.iloc[0])
    if total <= 0.0:
        return 0.0
    years = (n - 1) / float(trading_days_per_year)
    if years <= 0.0:
        return 0.0
    return float(total ** (1.0 / years) - 1.0)


# ---------------------------------------------------------------------------
# Block-bootstrap CI for Sharpe / Sortino
# ---------------------------------------------------------------------------

def _bootstrap_resample_indices(
    n: int,
    block_len: int,
    rng: np.random.Generator,
) -> np.ndarray:
    """Sample ``n`` integer indices using circular blocks of length
    ``block_len``. Block starts are drawn uniformly from ``[0, n)``;
    within a block indices wrap modulo ``n``.
    """
    if n <= 0:
        return np.empty(0, dtype=np.int64)
    block_len = max(1, int(block_len))
    n_blocks = int(math.ceil(n / float(block_len)))
    starts = rng.integers(low=0, high=n, size=n_blocks)
    # Build (n_blocks, block_len) matrix of indices, wrap modulo n,
    # flatten and truncate to n.
    offsets = np.arange(block_len, dtype=np.int64)
    idx = (starts[:, None] + offsets[None, :]) % n
    return idx.reshape(-1)[:n]


def _bootstrap_sharpe_ci(
    daily_returns: pd.Series,
    n_bootstrap: int = 1000,
    ci: float = 0.95,
    seed: int = 42,
) -> tuple:
    """Block-bootstrap (circular blocks) 95% CI for the annualized Sharpe.

    Block length follows the Hall–Horowitz rule of thumb
    ``L = max(1, ceil(N**(1/3)))`` to preserve serial correlation in daily
    returns. Deterministic for a given ``seed``.

    Returns ``(lower, upper)`` percentile bounds at the requested ``ci``
    (default 95% -> [2.5%, 97.5%]). Falls back to ``(nan, nan)`` if the
    return series is degenerate (too short or zero variance).
    """
    if daily_returns is None:
        return (float("nan"), float("nan"))
    r = daily_returns.dropna()
    n = len(r)
    if n < 2:
        return (float("nan"), float("nan"))
    arr = r.to_numpy(dtype=float, copy=False)
    if not np.isfinite(arr).all():
        arr = arr[np.isfinite(arr)]
        n = len(arr)
        if n < 2:
            return (float("nan"), float("nan"))

    block_len = max(1, int(math.ceil(n ** (1.0 / 3.0))))
    rng = np.random.default_rng(int(seed))
    n_bootstrap = max(1, int(n_bootstrap))

    sharpes = np.empty(n_bootstrap, dtype=float)
    for b in range(n_bootstrap):
        idx = _bootstrap_resample_indices(n=n, block_len=block_len, rng=rng)
        sample = arr[idx]
        std = sample.std(ddof=1)
        if std == 0.0 or not np.isfinite(std):
            sharpes[b] = 0.0
        else:
            sharpes[b] = sample.mean() / std * math.sqrt(TRADING_DAYS_PER_YEAR)

    alpha = (1.0 - float(ci)) / 2.0
    lower = float(np.percentile(sharpes, 100.0 * alpha))
    upper = float(np.percentile(sharpes, 100.0 * (1.0 - alpha)))
    return (lower, upper)


def _bootstrap_sharpe_sortino_ci(
    daily_returns: pd.Series,
    n_bootstrap: int = 1000,
    ci: float = 0.95,
    seed: int = 42,
) -> Dict[str, float]:
    """Joint block-bootstrap for Sharpe AND Sortino on the same resamples.

    Sharing the resampled index across both stats is cheaper than two
    independent loops and keeps the two CIs consistent under the same
    seed. Returns a dict with keys
    ``sharpe_ci_lower``, ``sharpe_ci_upper``,
    ``sortino_ci_lower``, ``sortino_ci_upper``.

    NaN bounds are emitted for degenerate series (n < 2 or all-finite-NaN
    after dropna).
    """
    nan_out = {
        "sharpe_ci_lower": float("nan"),
        "sharpe_ci_upper": float("nan"),
        "sortino_ci_lower": float("nan"),
        "sortino_ci_upper": float("nan"),
    }
    if daily_returns is None:
        return nan_out
    r = daily_returns.dropna()
    n = len(r)
    if n < 2:
        return nan_out
    arr = r.to_numpy(dtype=float, copy=False)
    if not np.isfinite(arr).all():
        arr = arr[np.isfinite(arr)]
        n = len(arr)
        if n < 2:
            return nan_out

    block_len = max(1, int(math.ceil(n ** (1.0 / 3.0))))
    rng = np.random.default_rng(int(seed))
    n_bootstrap = max(1, int(n_bootstrap))

    sharpes = np.empty(n_bootstrap, dtype=float)
    sortinos = np.empty(n_bootstrap, dtype=float)
    sqrt_ann = math.sqrt(TRADING_DAYS_PER_YEAR)
    for b in range(n_bootstrap):
        idx = _bootstrap_resample_indices(n=n, block_len=block_len, rng=rng)
        sample = arr[idx]
        mean = sample.mean()
        std = sample.std(ddof=1)
        if std == 0.0 or not np.isfinite(std):
            sharpes[b] = 0.0
        else:
            sharpes[b] = mean / std * sqrt_ann
        neg = sample[sample < 0.0]
        if len(neg) < 2:
            sortinos[b] = 0.0
        else:
            downside = math.sqrt(float((neg ** 2).sum()) / len(neg))
            if downside == 0.0 or not math.isfinite(downside):
                sortinos[b] = 0.0
            else:
                sortinos[b] = mean / downside * sqrt_ann

    alpha = (1.0 - float(ci)) / 2.0
    return {
        "sharpe_ci_lower": float(np.percentile(sharpes, 100.0 * alpha)),
        "sharpe_ci_upper": float(np.percentile(sharpes, 100.0 * (1.0 - alpha))),
        "sortino_ci_lower": float(np.percentile(sortinos, 100.0 * alpha)),
        "sortino_ci_upper": float(np.percentile(sortinos, 100.0 * (1.0 - alpha))),
    }


# ---------------------------------------------------------------------------
# Core simulation
# ---------------------------------------------------------------------------

def _select_long_targets(
    t: pd.Timestamp,
    valid_tickers: Sequence[str],
    bands_row: pd.Series,
    scores_row: Optional[pd.Series],
    prices_row: pd.Series,
    params: EngineParams,
    long_set: set,
) -> set:
    """Pick the long-leg target set for trading day ``t``.

    Selection rules:

    1. If ``params.uses_top_k`` is True and a score row is available, the
       long leg is the top-``k`` tickers by composite score (NaN-safe).
    2. Otherwise, the long leg is the union of tickers whose band today
       is in ``long_set``.

    In both cases the candidate must have a valid (>0, non-NaN) price on
    ``t`` so we can actually trade it.
    """
    if params.uses_top_k and scores_row is not None:
        s = scores_row.dropna()
        if s.empty:
            return set()
        # Keep only tickers with tradable prices.
        tradable = [
            tkr for tkr in s.index
            if tkr in prices_row.index
            and pd.notna(prices_row.get(tkr))
            and float(prices_row[tkr]) > 0.0
        ]
        if not tradable:
            return set()
        s = s.loc[tradable]
        k = min(int(params.top_k), len(s))
        # Largest k by composite score. Stable: ties broken by ticker name.
        top = s.sort_values(ascending=False, kind="mergesort").iloc[:k]
        return set(top.index)

    target: set = set()
    for tkr in valid_tickers:
        v = bands_row.get(tkr)
        if not isinstance(v, str):
            continue
        if v.strip().lower() in long_set:
            price_t = prices_row.get(tkr)
            if pd.notna(price_t) and float(price_t) > 0.0:
                target.add(tkr)
    return target


def _select_short_targets(
    t: pd.Timestamp,
    valid_tickers: Sequence[str],
    bands_row: pd.Series,
    scores_row: Optional[pd.Series],
    prices_row: pd.Series,
    params: EngineParams,
    short_set: set,
    excluded: set,
) -> set:
    """Pick the short-leg target set for trading day ``t``.

    Rules:

    1. If ``params.short_bottom_quintile`` is True and a score row is
       available, the short leg is the bottom 20% of composite scores
       (after dropping NaN and any ticker already chosen as long).
    2. Otherwise, the short leg is the union of tickers whose band today
       is in ``short_set``.

    Empty short_set (and no quintile flag) -> empty set.
    """
    if params.short_bottom_quintile and scores_row is not None:
        s = scores_row.dropna()
        if s.empty:
            return set()
        # Filter to tradable, excluding long leg to avoid self-cancel.
        tradable = [
            tkr for tkr in s.index
            if tkr in prices_row.index
            and pd.notna(prices_row.get(tkr))
            and float(prices_row[tkr]) > 0.0
            and tkr not in excluded
        ]
        if not tradable:
            return set()
        s = s.loc[tradable]
        n_q = max(1, int(round(len(s) / 5.0)))  # bottom quintile
        bot = s.sort_values(ascending=True, kind="mergesort").iloc[:n_q]
        return set(bot.index)

    if not short_set:
        return set()
    target: set = set()
    for tkr in valid_tickers:
        if tkr in excluded:
            continue
        v = bands_row.get(tkr)
        if not isinstance(v, str):
            continue
        if v.strip().lower() in short_set:
            price_t = prices_row.get(tkr)
            if pd.notna(price_t) and float(price_t) > 0.0:
                target.add(tkr)
    return target


def _avg_close_to_close_return(
    held: set,
    prices: pd.DataFrame,
    t: pd.Timestamp,
    prev_day: pd.Timestamp,
) -> float:
    """Equal-weight average single-name return from ``prev_day`` close to
    ``t`` close. Returns 0.0 if no name has a usable price pair.
    """
    if not held or prev_day is None:
        return 0.0
    acc = 0.0
    n = 0
    for tkr in held:
        if tkr not in prices.columns:
            continue
        p_t = prices.at[t, tkr]
        p_prev = prices.at[prev_day, tkr]
        if pd.isna(p_t) or pd.isna(p_prev) or float(p_prev) == 0.0:
            continue
        acc += float(p_t) / float(p_prev) - 1.0
        n += 1
    return acc / n if n else 0.0


def _simulate(
    target_bands: pd.DataFrame,
    prices: pd.DataFrame,
    params: EngineParams,
    long_set: set,
    score_history_aligned: Optional[pd.DataFrame] = None,
    short_set: Optional[set] = None,
) -> Dict[str, Any]:
    """One event-driven simulation pass.

    Returns a dict with:
      - equity (pd.Series): normalized equity per trading day
      - daily_returns (pd.Series)
      - positions (pd.DataFrame): trading_day x ticker, weight in [0,1]
      - trades (list[dict])
      - turnover_per_day (pd.Series)
      - n_positions (pd.Series, int)

    Phase 3: if ``params.has_short_leg`` is True, the per-day return
    becomes ``mean(long_returns) - mean(short_returns)`` and round-trip
    costs are charged to both legs. Trades on the short leg are emitted
    with ``side="short"`` so the trades file stays auditable.
    """
    cost_one_side = float(params.cost_bps) / 10000.0
    short_set = short_set if short_set is not None else params.normalized_short_set()
    has_short = params.has_short_leg

    trading_days = list(target_bands.index)
    if not trading_days:
        empty = pd.Series(dtype=float)
        return {
            "equity": empty,
            "daily_returns": empty,
            "positions": pd.DataFrame(),
            "trades": [],
            "turnover_per_day": empty,
            "n_positions": pd.Series(dtype=int),
        }

    # Ensure prices align to the trading-day index and have a column
    # for every ticker mentioned in target_bands. Missing tickers (e.g.
    # universe ticker with no Yahoo data) are dropped from membership.
    p = prices.reindex(trading_days)
    valid_tickers = [c for c in target_bands.columns if c in p.columns]
    bh = target_bands[valid_tickers]
    p = p[valid_tickers]

    # Score history is optional. When given (top_k / bottom-quintile
    # profiles) restrict to columns shared with the prices panel.
    if score_history_aligned is not None and not score_history_aligned.empty:
        score_cols = [c for c in score_history_aligned.columns if c in p.columns]
        sh = score_history_aligned[score_cols]
    else:
        sh = None

    held_long: set = set()
    held_short: set = set()
    entry_info: Dict[str, Dict[str, Any]] = {}
    trades: List[Dict[str, Any]] = []
    equity = float(params.initial_capital)
    equity_series: List[float] = []
    daily_returns: List[float] = []
    turnover_series: List[float] = []
    n_positions_series: List[int] = []
    positions_rows: List[Dict[str, Any]] = []

    prev_day: Optional[pd.Timestamp] = None

    for t in trading_days:
        # 1. Realize today's return on yesterday's held portfolio(s).
        long_ret_gross = _avg_close_to_close_return(held_long, p, t, prev_day)
        short_ret_gross = (
            _avg_close_to_close_return(held_short, p, t, prev_day)
            if has_short else 0.0
        )
        if has_short:
            # Dollar-neutral within each leg: long minus short.
            daily_ret_gross = long_ret_gross - short_ret_gross
        else:
            daily_ret_gross = long_ret_gross

        equity *= 1.0 + daily_ret_gross

        # 2. Determine target membership at close of t.
        bands_today = bh.loc[t]
        scores_today = sh.loc[t] if sh is not None and t in sh.index else None
        prices_today = p.loc[t]

        target_long = _select_long_targets(
            t=t,
            valid_tickers=valid_tickers,
            bands_row=bands_today,
            scores_row=scores_today,
            prices_row=prices_today,
            params=params,
            long_set=long_set,
        )
        if has_short:
            target_short = _select_short_targets(
                t=t,
                valid_tickers=valid_tickers,
                bands_row=bands_today,
                scores_row=scores_today,
                prices_row=prices_today,
                params=params,
                short_set=short_set,
                excluded=target_long,
            )
        else:
            target_short = set()

        # 3. Compute entries / exits vs currently held (per leg).
        entries_long = target_long - held_long
        exits_long = held_long - target_long
        entries_short = target_short - held_short
        exits_short = held_short - target_short

        # 4. Apply round-trip costs proportional to traded weight (both legs).
        n_long_prev = len(held_long)
        n_long_target = len(target_long)
        n_short_prev = len(held_short)
        n_short_target = len(target_short)

        traded_weight_long = 0.0
        if entries_long or exits_long:
            avg_w_post = 1.0 / n_long_target if n_long_target else 0.0
            avg_w_pre = 1.0 / n_long_prev if n_long_prev else 0.0
            traded_weight_long = (
                len(entries_long) * avg_w_post + len(exits_long) * avg_w_pre
            )

        traded_weight_short = 0.0
        if entries_short or exits_short:
            avg_w_post = 1.0 / n_short_target if n_short_target else 0.0
            avg_w_pre = 1.0 / n_short_prev if n_short_prev else 0.0
            traded_weight_short = (
                len(entries_short) * avg_w_post + len(exits_short) * avg_w_pre
            )

        total_traded_weight = traded_weight_long + traded_weight_short
        if total_traded_weight > 0.0:
            cost_drag = total_traded_weight * cost_one_side
            equity *= 1.0 - cost_drag
            daily_ret_net = (1.0 + daily_ret_gross) * (1.0 - cost_drag) - 1.0
        else:
            daily_ret_net = daily_ret_gross

        # 5. Record trades for new entries (long + short).
        for tkr in entries_long:
            entry_info[("L", tkr)] = {
                "entry_date": _to_iso_date(t),
                "entry_price": float(p.at[t, tkr]),
                "side": "long",
            }
        for tkr in entries_short:
            entry_info[("S", tkr)] = {
                "entry_date": _to_iso_date(t),
                "entry_price": float(p.at[t, tkr]),
                "side": "short",
            }

        # 6. Record trades for exits.
        def _close_trade(side: str, tkr: str) -> None:
            key = (side[0].upper(), tkr)
            info = entry_info.pop(key, None)
            if info is None:
                return
            exit_price = p.at[t, tkr] if tkr in p.columns else np.nan
            if pd.isna(exit_price) or float(exit_price) <= 0.0:
                trades.append(
                    {
                        "ticker": tkr,
                        "side": side,
                        "entry_date": info["entry_date"],
                        "exit_date": _to_iso_date(t),
                        "entry_price": info["entry_price"],
                        "exit_price": None,
                        "gross_return": None,
                        "net_return": None,
                        "hold_days": _hold_days(info["entry_date"], _to_iso_date(t)),
                    }
                )
                return
            entry_px = info["entry_price"]
            raw = float(exit_price) / float(entry_px) - 1.0
            # Shorts P&L is the inverse direction.
            gross = -raw if side == "short" else raw
            net = (1.0 + gross) * (1.0 - 2.0 * cost_one_side) - 1.0
            trades.append(
                {
                    "ticker": tkr,
                    "side": side,
                    "entry_date": info["entry_date"],
                    "exit_date": _to_iso_date(t),
                    "entry_price": float(entry_px),
                    "exit_price": float(exit_price),
                    "gross_return": float(gross),
                    "net_return": float(net),
                    "hold_days": _hold_days(info["entry_date"], _to_iso_date(t)),
                }
            )

        for tkr in exits_long:
            _close_trade("long", tkr)
        for tkr in exits_short:
            _close_trade("short", tkr)

        # 7. Update held sets; record equity + diagnostics.
        held_long = target_long
        held_short = target_short
        if held_long:
            weight = 1.0 / len(held_long)
            for tkr in held_long:
                positions_rows.append(
                    {
                        "date": _to_iso_date(t),
                        "ticker": tkr,
                        "side": "long",
                        "weight": weight,
                        "entry_date": entry_info.get(("L", tkr), {}).get(
                            "entry_date", _to_iso_date(t)
                        ),
                    }
                )
        if held_short:
            weight = 1.0 / len(held_short)
            for tkr in held_short:
                positions_rows.append(
                    {
                        "date": _to_iso_date(t),
                        "ticker": tkr,
                        "side": "short",
                        "weight": -weight,
                        "entry_date": entry_info.get(("S", tkr), {}).get(
                            "entry_date", _to_iso_date(t)
                        ),
                    }
                )

        equity_series.append(float(equity))
        daily_returns.append(float(daily_ret_net))
        denom = max(n_long_prev + n_short_prev, n_long_target + n_short_target, 1)
        turnover = (
            len(entries_long) + len(exits_long)
            + len(entries_short) + len(exits_short)
        ) / float(denom)
        turnover_series.append(float(turnover))
        n_positions_series.append(int(len(held_long) + len(held_short)))
        prev_day = t

    idx = pd.DatetimeIndex(trading_days)
    return {
        "equity": pd.Series(equity_series, index=idx),
        "daily_returns": pd.Series(daily_returns, index=idx),
        "positions": pd.DataFrame(positions_rows),
        "trades": trades,
        "turnover_per_day": pd.Series(turnover_series, index=idx),
        "n_positions": pd.Series(n_positions_series, index=idx, dtype=int),
    }


def _hold_days(entry_iso: str, exit_iso: str) -> int:
    try:
        a = pd.Timestamp(entry_iso)
        b = pd.Timestamp(exit_iso)
        return int((b - a).days)
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------

def run_backtest(
    band_history: pd.DataFrame,
    prices: pd.DataFrame,
    params: Optional[EngineParams] = None,
    benchmark_prices: Optional[pd.Series] = None,
    per_band_sweep: bool = True,
    score_history: Optional[pd.DataFrame] = None,
    compute_ci: bool = True,
    n_bootstrap: int = 1000,
    ci_seed: int = 42,
) -> Dict[str, Any]:
    """Run the Phase-1 long-only event-driven backtest.

    Parameters
    ----------
    band_history : DataFrame
        Date-indexed wide frame of band assignments, columns = ticker.
        Lower-case banding ('elite', 'high_confidence', ...) expected
        but any case is normalized.
    prices : DataFrame
        Trading-day indexed wide frame of adjusted closes,
        columns = ticker. Used both for the strategy P&L and as the
        canonical trading-day calendar.
    params : EngineParams, optional
        Strategy / execution parameters. Defaults to Phase 1.
    benchmark_prices : Series, optional
        Single-ticker daily close series (e.g. SPY adj_close) used as a
        passive benchmark equity curve. Output normalized to 1.0 at the
        first trading day in the window.
    per_band_sweep : bool
        If True, also run a sub-portfolio simulation for each band in
        ``ALL_BANDS_FOR_SWEEP`` and emit a per-band equity curve series.
    compute_ci : bool
        If True (default), the summary dict gains ``sharpe_ci_lower``,
        ``sharpe_ci_upper``, ``sortino_ci_lower``, ``sortino_ci_upper``
        fields from a circular block-bootstrap of the daily returns
        (Hall–Horowitz block length, 1000 resamples, seed 42). If False,
        those keys are absent — useful for downstream consumers that pin
        the summary schema.
    n_bootstrap : int
        Number of bootstrap resamples used to estimate the CI when
        ``compute_ci`` is True. Default 1000.
    ci_seed : int
        Seed for the bootstrap RNG (``numpy.random.default_rng``). The
        same seed produces identical CI bounds across runs.

    Returns
    -------
    dict
        See ``docs/lthcs-backtest-engine-spec.md`` §4 for the artifact
        schema. Keys: ``equity_curve``, ``positions_daily``, ``trades``,
        ``band_curves``, ``summary``, ``run_meta``.
    """
    if params is None:
        params = EngineParams()

    prices_td = _prices_to_trading_index(prices)
    if prices_td.empty or band_history is None or band_history.empty:
        return _empty_result(params, prices_td, band_history)

    # Determine the trading-day window: every trading day in [first_band_date,
    # last_band_date] that has a price row. We constrain to the band-history
    # bounds so we don't simulate days where the strategy can't decide.
    bh = band_history.copy()
    bh.index = pd.to_datetime(bh.index)
    bh = bh.sort_index()
    bh_start = bh.index.min()
    bh_end = bh.index.max()

    mask = (prices_td.index >= bh_start) & (
        prices_td.index <= bh_end + pd.Timedelta(days=5)
    )
    trading_days = list(prices_td.index[mask])
    if not trading_days:
        return _empty_result(params, prices_td, band_history)

    target_bands = _align_bands_to_trading_days(
        band_history=bh,
        trading_days=trading_days,
        delay_trading_days=params.delay_trading_days,
    )
    aligned_scores = None
    if score_history is not None and not score_history.empty:
        aligned_scores = _align_scores_to_trading_days(
            score_history=score_history,
            trading_days=trading_days,
            delay_trading_days=params.delay_trading_days,
        )

    long_set = params.normalized_long_set()
    headline = _simulate(
        target_bands=target_bands,
        prices=prices_td,
        params=params,
        long_set=long_set,
        score_history_aligned=aligned_scores,
        short_set=params.normalized_short_set(),
    )

    band_curves: Dict[str, Dict[str, float]] = {}
    if per_band_sweep:
        # Per-band sweep is always long-only and ignores the headline
        # profile's short / top-K settings — we want a clean "what would
        # band X alone have done" view for the UI.
        sweep_params = EngineParams(
            bands_long=params.bands_long,
            cost_bps=params.cost_bps,
            delay_trading_days=params.delay_trading_days,
            initial_capital=params.initial_capital,
            rebalance_daily=params.rebalance_daily,
        )
        for band in ALL_BANDS_FOR_SWEEP:
            res = _simulate(
                target_bands=target_bands,
                prices=prices_td,
                params=sweep_params,
                long_set={band},
            )
            band_curves[band] = _series_to_jsonable(res["equity"])

    # Benchmark curve.
    benchmark_curve: Dict[str, float] = {}
    if benchmark_prices is not None and not benchmark_prices.empty:
        bench = benchmark_prices.copy()
        bench.index = pd.to_datetime(bench.index)
        bench = bench.sort_index()
        bench = bench.reindex(headline["equity"].index, method="ffill")
        if not bench.empty:
            first_valid = bench.dropna()
            if not first_valid.empty:
                base = float(first_valid.iloc[0])
                if base > 0.0:
                    norm = bench / base * float(params.initial_capital)
                    benchmark_curve = _series_to_jsonable(norm)

    summary = _build_summary(
        params=params,
        headline=headline,
        trading_days=trading_days,
        valid_tickers=list(target_bands.columns),
        compute_ci=compute_ci,
        n_bootstrap=n_bootstrap,
        ci_seed=ci_seed,
    )

    run_meta = {
        "engine_version": "1.0.0",
        "params": params.to_jsonable(),
        "params_hash": _params_hash(params),
        "band_hash": _frame_hash(bh),
        "price_hash": _frame_hash(prices_td),
        "window": {
            "start": _to_iso_date(trading_days[0]),
            "end": _to_iso_date(trading_days[-1]),
            "n_trading_days": len(trading_days),
        },
        "universe_size": int(len(target_bands.columns)),
        "long_set": sorted(list(long_set)),
        "short_set": sorted(list(params.normalized_short_set())),
        "profile_name": params.profile_name,
        "top_k": int(params.top_k),
        "short_bottom_quintile": bool(params.short_bottom_quintile),
    }

    return {
        "equity_curve": _series_to_jsonable(headline["equity"]),
        "daily_returns": _series_to_jsonable(headline["daily_returns"]),
        "n_positions": _series_to_jsonable(headline["n_positions"].astype(float)),
        "turnover_per_day": _series_to_jsonable(headline["turnover_per_day"]),
        "positions_daily": headline["positions"].to_dict(orient="records")
        if not headline["positions"].empty
        else [],
        "trades": headline["trades"],
        "band_curves": band_curves,
        "benchmark_curve": benchmark_curve,
        "summary": summary,
        "run_meta": run_meta,
    }


def _empty_result(
    params: EngineParams,
    prices_td: pd.DataFrame,
    band_history: Optional[pd.DataFrame],
) -> Dict[str, Any]:
    return {
        "equity_curve": {},
        "daily_returns": {},
        "n_positions": {},
        "turnover_per_day": {},
        "positions_daily": [],
        "trades": [],
        "band_curves": {},
        "benchmark_curve": {},
        "summary": {
            "total_return": 0.0,
            "ann_return": 0.0,
            "max_drawdown": 0.0,
            "sharpe": 0.0,
            "sortino": 0.0,
            "hit_rate": 0.0,
            "avg_hold_days": 0.0,
            "turnover": 0.0,
            "n_trades": 0,
            "n_unique_tkr": 0,
            "n_trading_days": 0,
            "params": params.to_jsonable(),
        },
        "run_meta": {
            "engine_version": "1.0.0",
            "params": params.to_jsonable(),
            "params_hash": _params_hash(params),
            "band_hash": _frame_hash(band_history) if band_history is not None else "empty",
            "price_hash": _frame_hash(prices_td),
            "window": {"start": None, "end": None, "n_trading_days": 0},
            "universe_size": 0,
            "long_set": sorted(list(params.normalized_long_set())),
            "short_set": sorted(list(params.normalized_short_set())),
            "profile_name": params.profile_name,
            "top_k": int(params.top_k),
            "short_bottom_quintile": bool(params.short_bottom_quintile),
        },
    }


def _build_summary(
    params: EngineParams,
    headline: Dict[str, Any],
    trading_days: Sequence[pd.Timestamp],
    valid_tickers: Sequence[str],
    compute_ci: bool = True,
    n_bootstrap: int = 1000,
    ci_seed: int = 42,
) -> Dict[str, Any]:
    equity: pd.Series = headline["equity"]
    daily: pd.Series = headline["daily_returns"]
    trades: List[Dict[str, Any]] = headline["trades"]
    turnover: pd.Series = headline["turnover_per_day"]

    total_return = (
        float(equity.iloc[-1] / equity.iloc[0] - 1.0)
        if len(equity) >= 1 and float(equity.iloc[0]) > 0.0
        else 0.0
    )
    hold_days_vals = [t["hold_days"] for t in trades if t.get("hold_days") is not None]
    avg_hold = float(np.mean(hold_days_vals)) if hold_days_vals else 0.0
    unique_tkr = sorted({t["ticker"] for t in trades})
    summary: Dict[str, Any] = {
        "total_return": total_return,
        "ann_return": _annualized_return(equity),
        "max_drawdown": _max_drawdown(equity),
        "sharpe": _annualized_sharpe(daily),
        "sortino": _annualized_sortino(daily),
        "hit_rate": _hit_rate(daily),
        "avg_hold_days": avg_hold,
        "turnover": float(turnover.mean()) if not turnover.empty else 0.0,
        "n_trades": int(len(trades)),
        "n_unique_tkr": int(len(unique_tkr)),
        "n_trading_days": int(len(trading_days)),
        "params": params.to_jsonable(),
    }
    if compute_ci:
        ci = _bootstrap_sharpe_sortino_ci(
            daily_returns=daily,
            n_bootstrap=n_bootstrap,
            ci=0.95,
            seed=ci_seed,
        )
        summary.update(ci)
    return summary


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def _series_to_jsonable(s: pd.Series) -> Dict[str, float]:
    if s is None or s.empty:
        return {}
    out: Dict[str, float] = {}
    for idx, val in s.items():
        key = idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime") else str(idx)
        out[key] = None if pd.isna(val) else float(val)
    return out


def equity_curve_to_csv(equity_curve: Dict[str, float], path: Path) -> None:
    """Write the equity curve to CSV. Columns: date,equity,daily_return."""
    rows = sorted(equity_curve.items())
    if not rows:
        path.write_text("date,equity\n")
        return
    dates = [d for d, _ in rows]
    values = [v for _, v in rows]
    df = pd.DataFrame({"date": dates, "equity": values})
    df["daily_return"] = df["equity"].pct_change().fillna(0.0)
    df.to_csv(path, index=False)


def positions_daily_to_csv(positions_daily: List[Dict[str, Any]], path: Path) -> None:
    if not positions_daily:
        path.write_text("date,ticker,weight,entry_date\n")
        return
    df = pd.DataFrame(positions_daily)
    df.to_csv(path, index=False)


def trades_to_csv(trades: List[Dict[str, Any]], path: Path) -> None:
    cols = [
        "entry_date",
        "exit_date",
        "ticker",
        "side",
        "entry_price",
        "exit_price",
        "gross_return",
        "net_return",
        "hold_days",
    ]
    if not trades:
        path.write_text(",".join(cols) + "\n")
        return
    df = pd.DataFrame(trades)
    for c in cols:
        if c not in df.columns:
            df[c] = None
    df = df[cols]
    df.to_csv(path, index=False)


def build_engine_report_markdown(
    summary: Dict[str, Any],
    run_meta: Dict[str, Any],
    band_curves: Optional[Dict[str, Dict[str, float]]] = None,
    benchmark_curve: Optional[Dict[str, float]] = None,
) -> str:
    window = run_meta.get("window", {}) or {}
    lines: List[str] = []
    lines.append("# LTHCS Backtest Engine Report")
    lines.append("")
    lines.append("Window: **%s -> %s** (%s trading days)" % (
        window.get("start") or "n/a",
        window.get("end") or "n/a",
        window.get("n_trading_days", 0),
    ))
    lines.append(
        "Universe: **%d tickers** | long bands: %s | cost: %.1f bps/side | delay: %d td"
        % (
            run_meta.get("universe_size", 0),
            run_meta.get("long_set", []),
            float(summary.get("params", {}).get("cost_bps", 0.0)),
            int(summary.get("params", {}).get("delay_trading_days", 1)),
        )
    )
    lines.append("")
    lines.append("## Headline P&L (non-overlapping)")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|:-------|------:|")
    lines.append("| Total return | %+0.4f |" % float(summary.get("total_return", 0.0)))
    lines.append("| Annualized return | %+0.4f |" % float(summary.get("ann_return", 0.0)))
    sharpe_lo = summary.get("sharpe_ci_lower")
    sharpe_hi = summary.get("sharpe_ci_upper")
    if (
        sharpe_lo is not None
        and sharpe_hi is not None
        and isinstance(sharpe_lo, (int, float))
        and isinstance(sharpe_hi, (int, float))
        and math.isfinite(float(sharpe_lo))
        and math.isfinite(float(sharpe_hi))
    ):
        lines.append("| Annualized Sharpe | %+0.3f (95%% CI: %+0.2f ... %+0.2f) |" % (
            float(summary.get("sharpe", 0.0)),
            float(sharpe_lo),
            float(sharpe_hi),
        ))
    else:
        lines.append("| Annualized Sharpe | %+0.3f |" % float(summary.get("sharpe", 0.0)))
    sortino_lo = summary.get("sortino_ci_lower")
    sortino_hi = summary.get("sortino_ci_upper")
    if (
        sortino_lo is not None
        and sortino_hi is not None
        and isinstance(sortino_lo, (int, float))
        and isinstance(sortino_hi, (int, float))
        and math.isfinite(float(sortino_lo))
        and math.isfinite(float(sortino_hi))
    ):
        lines.append("| Annualized Sortino | %+0.3f (95%% CI: %+0.2f ... %+0.2f) |" % (
            float(summary.get("sortino", 0.0)),
            float(sortino_lo),
            float(sortino_hi),
        ))
    else:
        lines.append("| Annualized Sortino | %+0.3f |" % float(summary.get("sortino", 0.0)))
    lines.append("| Max drawdown | %+0.4f |" % float(summary.get("max_drawdown", 0.0)))
    lines.append("| Hit rate (daily) | %0.3f |" % float(summary.get("hit_rate", 0.0)))
    lines.append("| Avg hold days | %.1f |" % float(summary.get("avg_hold_days", 0.0)))
    lines.append("| Avg turnover / day | %0.4f |" % float(summary.get("turnover", 0.0)))
    lines.append("| Total trades | %d |" % int(summary.get("n_trades", 0)))
    lines.append("| Unique tickers | %d |" % int(summary.get("n_unique_tkr", 0)))
    lines.append("")
    lines.append(
        "> Non-overlapping construction: every trading day's return is realized "
        "on the actual close-to-close of held names. No forward-window reuse, "
        "so Sharpe is directly comparable to a passive benchmark."
    )
    lines.append("")

    if band_curves:
        lines.append("## Per-band sub-portfolio total return")
        lines.append("")
        lines.append("| Band | Total return |")
        lines.append("|:-----|------:|")
        for band in ALL_BANDS_FOR_SWEEP:
            curve = band_curves.get(band) or {}
            if not curve:
                lines.append("| %s | n/a |" % band)
                continue
            vals = list(curve.values())
            if len(vals) < 2 or float(vals[0]) <= 0:
                lines.append("| %s | n/a |" % band)
                continue
            tot = float(vals[-1]) / float(vals[0]) - 1.0
            lines.append("| %s | %+0.4f |" % (band, tot))
        lines.append("")

    if benchmark_curve:
        vals = list(benchmark_curve.values())
        if len(vals) >= 2 and float(vals[0]) > 0:
            bench_tot = float(vals[-1]) / float(vals[0]) - 1.0
            lines.append("## Benchmark")
            lines.append("")
            lines.append("Benchmark total return: **%+0.4f**" % bench_tot)
            lines.append("")

    lines.append("## Run metadata")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(run_meta, indent=2, sort_keys=True))
    lines.append("```")
    return "\n".join(lines) + "\n"
