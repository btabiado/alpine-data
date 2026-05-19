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
    """

    bands_long: List[str] = field(default_factory=lambda: list(DEFAULT_LONG_BANDS))
    cost_bps: float = 5.0
    delay_trading_days: int = 1
    initial_capital: float = 1.0
    # When True, the per-day equal-weight rebalance is enforced.
    # Phase 1 always rebalances daily so this flag exists mostly for
    # future profiles (e.g. monthly-rebalance variants in Phase 3).
    rebalance_daily: bool = True

    def normalized_long_set(self) -> set:
        return {b.strip().lower() for b in self.bands_long if b and b.strip()}

    def to_jsonable(self) -> Dict[str, Any]:
        return {
            "bands_long": list(self.bands_long),
            "cost_bps": float(self.cost_bps),
            "delay_trading_days": int(self.delay_trading_days),
            "initial_capital": float(self.initial_capital),
            "rebalance_daily": bool(self.rebalance_daily),
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
# Core simulation
# ---------------------------------------------------------------------------

def _simulate(
    target_bands: pd.DataFrame,
    prices: pd.DataFrame,
    params: EngineParams,
    long_set: set,
) -> Dict[str, Any]:
    """One event-driven simulation pass.

    Returns a dict with:
      - equity (pd.Series): normalized equity per trading day
      - daily_returns (pd.Series)
      - positions (pd.DataFrame): trading_day x ticker, weight in [0,1]
      - trades (list[dict])
      - turnover_per_day (pd.Series)
      - n_positions (pd.Series, int)
    Used both by the headline strategy and per-band sub-portfolio runs.
    """
    cost_one_side = float(params.cost_bps) / 10000.0

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

    held: set = set()
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
        # 1. Realize today's return on yesterday's held portfolio.
        if prev_day is not None and held:
            day_ret_accumulator = 0.0
            n_with_price = 0
            for tkr in held:
                p_t = p.at[t, tkr] if tkr in p.columns else np.nan
                p_prev = p.at[prev_day, tkr] if tkr in p.columns else np.nan
                if pd.isna(p_t) or pd.isna(p_prev) or float(p_prev) == 0.0:
                    continue
                day_ret_accumulator += float(p_t) / float(p_prev) - 1.0
                n_with_price += 1
            daily_ret_gross = (
                day_ret_accumulator / n_with_price if n_with_price else 0.0
            )
        else:
            daily_ret_gross = 0.0

        equity *= 1.0 + daily_ret_gross

        # 2. Determine target membership at close of t. NaN bands fall
        # through to "not held".
        bands_today = bh.loc[t]
        # Normalize bands to lowercase strings; non-strings (NaN) -> "".
        target_today: set = set()
        for tkr in valid_tickers:
            v = bands_today.get(tkr)
            if not isinstance(v, str):
                continue
            if v.strip().lower() in long_set:
                # Only consider tickers with a valid close price today
                # (otherwise we can't trade them).
                price_t = p.at[t, tkr]
                if pd.notna(price_t) and float(price_t) > 0.0:
                    target_today.add(tkr)

        # 3. Compute entries / exits vs currently held.
        entries = target_today - held
        exits = held - target_today

        # 4. Apply round-trip costs proportional to traded weight.
        # Approximation: each entry buys the average weight of the new
        # portfolio (1 / n_target), each exit sells the average weight of
        # the prior portfolio (1 / n_held_prev). Sum of |dw| approximated
        # by the simpler heuristic below, which produces a daily cost
        # drag roughly proportional to turnover.
        n_held_prev = len(held)
        n_target = len(target_today)
        if entries or exits:
            # Average weight implied by post-trade portfolio
            avg_w_post = 1.0 / n_target if n_target else 0.0
            avg_w_pre = 1.0 / n_held_prev if n_held_prev else 0.0
            traded_weight = (
                len(entries) * avg_w_post + len(exits) * avg_w_pre
            )
            cost_drag = traded_weight * cost_one_side
            equity *= 1.0 - cost_drag
            daily_ret_net = (1.0 + daily_ret_gross) * (1.0 - cost_drag) - 1.0
        else:
            daily_ret_net = daily_ret_gross

        # 5. Record trades for new entries.
        for tkr in entries:
            entry_info[tkr] = {
                "entry_date": _to_iso_date(t),
                "entry_price": float(p.at[t, tkr]),
                "entry_equity": equity,
            }
        # 6. Record trades for exits.
        for tkr in exits:
            info = entry_info.pop(tkr, None)
            if info is None:
                continue
            exit_price = p.at[t, tkr] if tkr in p.columns else np.nan
            if pd.isna(exit_price) or float(exit_price) <= 0.0:
                # Carry forward; we'll close at the next valid price.
                # For simplicity, register the trade anyway with NaN
                # return so the trades list is complete.
                trades.append(
                    {
                        "ticker": tkr,
                        "entry_date": info["entry_date"],
                        "exit_date": _to_iso_date(t),
                        "entry_price": info["entry_price"],
                        "exit_price": None,
                        "gross_return": None,
                        "net_return": None,
                        "hold_days": _hold_days(info["entry_date"], _to_iso_date(t)),
                    }
                )
                continue
            entry_px = info["entry_price"]
            gross = float(exit_price) / float(entry_px) - 1.0
            # Net return for the trade approximates as gross minus
            # round-trip cost on the average weight at which it traded.
            net = (1.0 + gross) * (1.0 - 2.0 * cost_one_side) - 1.0
            trades.append(
                {
                    "ticker": tkr,
                    "entry_date": info["entry_date"],
                    "exit_date": _to_iso_date(t),
                    "entry_price": float(entry_px),
                    "exit_price": float(exit_price),
                    "gross_return": float(gross),
                    "net_return": float(net),
                    "hold_days": _hold_days(info["entry_date"], _to_iso_date(t)),
                }
            )

        # 7. Update held set; record equity + diagnostics.
        held = target_today
        if held:
            weight = 1.0 / len(held)
            for tkr in held:
                positions_rows.append(
                    {
                        "date": _to_iso_date(t),
                        "ticker": tkr,
                        "weight": weight,
                        "entry_date": entry_info[tkr]["entry_date"]
                        if tkr in entry_info
                        else _to_iso_date(t),
                    }
                )
        equity_series.append(float(equity))
        daily_returns.append(float(daily_ret_net))
        union_size = len(held | (held ^ entries) | (held | exits))  # rough
        denom = max(n_held_prev, n_target, 1)
        turnover = (len(entries) + len(exits)) / float(denom)
        turnover_series.append(float(turnover))
        n_positions_series.append(int(len(held)))
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

    long_set = params.normalized_long_set()
    headline = _simulate(target_bands=target_bands, prices=prices_td, params=params, long_set=long_set)

    band_curves: Dict[str, Dict[str, float]] = {}
    if per_band_sweep:
        for band in ALL_BANDS_FOR_SWEEP:
            res = _simulate(
                target_bands=target_bands,
                prices=prices_td,
                params=params,
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
        },
    }


def _build_summary(
    params: EngineParams,
    headline: Dict[str, Any],
    trading_days: Sequence[pd.Timestamp],
    valid_tickers: Sequence[str],
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
    return {
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
    lines.append("| Annualized Sharpe | %+0.3f |" % float(summary.get("sharpe", 0.0)))
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
