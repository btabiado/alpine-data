"""LTHCS backtest engine — P&L attribution for the daily score history.

Reads the per-day score history under ``data/lthcs/`` (snapshots +
per-ticker history files) and joins it to Yahoo daily closes so we can:

  * Form long/short portfolios by LTHCS band and compute realized P&L.
  * Rank tickers each day by a pillar sub-score, sort into quintiles,
    and measure top-minus-bottom spread.
  * Compute the information coefficient (rank correlation between score
    and forward return) per pillar.

Design notes:
  * Score history is loaded from ``snapshots/<date>.json`` (which carries
    sub_scores) rather than the rolling per-ticker history file, which
    only stores ``date / score / band``.
  * Yahoo price fetches go through ``lthcs.sources.yahoo.get_daily_prices``
    which already caches under ``.cache/lthcs/yahoo`` for 24h. We add a
    second, indefinite cache layer under ``.cache/lthcs/backtest/prices``
    so historical prices are pinned (Yahoo's 24h cache could otherwise
    expire and re-fetch on every run).
  * All filesystem reads tolerate missing files: a ticker with no
    history simply drops out of the universe for that day.

This module is read-only with respect to ``data/lthcs/``; it never
mutates snapshots or history.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
import warnings
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TRADING_DAYS_PER_YEAR = 252

DEFAULT_HORIZONS = [1, 5, 21, 63]
DEFAULT_LONG_BANDS = ["elite", "high_confidence", "constructive"]
DEFAULT_SHORT_BANDS = ["review"]

PILLAR_NAMES = [
    "adoption_momentum",
    "institutional_confidence",
    "financial_evolution",
    "thesis_integrity",
    "des",
]


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _repo_root() -> Path:
    """Return the repository root (the parent of this package)."""
    return Path(__file__).resolve().parent.parent


def _default_data_root() -> Path:
    return _repo_root() / "data" / "lthcs"


def _default_cache_root() -> Path:
    return _repo_root() / ".cache" / "lthcs" / "backtest"


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=False)
            fh.write("\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# Snapshot loading
# ---------------------------------------------------------------------------

def _iter_snapshot_files(data_root: Path) -> List[Path]:
    """Yield all ``YYYY-MM-DD.json`` snapshot files, ascending."""
    snap_dir = data_root / "snapshots"
    if not snap_dir.exists():
        return []
    out: List[Path] = []
    for p in snap_dir.iterdir():
        if not p.is_file() or p.suffix != ".json":
            continue
        stem = p.stem
        # Skip index.json + tmp files. Date check is YYYY-MM-DD shaped.
        if len(stem) != 10 or stem[4] != "-" or stem[7] != "-":
            continue
        out.append(p)
    out.sort()
    return out


def _load_all_snapshots(data_root: Path) -> Dict[str, List[Dict[str, Any]]]:
    """Return a mapping date -> list of score rows."""
    out: Dict[str, List[Dict[str, Any]]] = {}
    for p in _iter_snapshot_files(data_root):
        try:
            payload = _read_json(p)
        except (OSError, json.JSONDecodeError):
            continue
        date = payload.get("calc_date") or p.stem
        scores = payload.get("scores") or []
        if isinstance(scores, list):
            out[date] = scores
    return out


def _filter_universe(
    snapshots: Dict[str, List[Dict[str, Any]]],
    tickers: Optional[Iterable[str]],
) -> Optional[set]:
    if tickers is None:
        return None
    return {str(t) for t in tickers}


def _build_wide_frame(
    snapshots: Dict[str, List[Dict[str, Any]]],
    value_fn,
    tickers: Optional[set],
) -> pd.DataFrame:
    """Pivot a per-row attribute into a wide date x ticker DataFrame."""
    records: List[Dict[str, Any]] = []
    for date, rows in snapshots.items():
        for r in rows:
            t = r.get("ticker")
            if not isinstance(t, str) or not t:
                continue
            if tickers is not None and t not in tickers:
                continue
            v = value_fn(r)
            if v is None:
                continue
            records.append({"date": date, "ticker": t, "value": v})
    if not records:
        return pd.DataFrame()
    df = pd.DataFrame.from_records(records)
    df["date"] = pd.to_datetime(df["date"])
    wide = df.pivot_table(index="date", columns="ticker", values="value", aggfunc="last")
    wide.sort_index(inplace=True)
    return wide


# ---------------------------------------------------------------------------
# Public history loaders
# ---------------------------------------------------------------------------

def load_score_history(
    tickers: Optional[List[str]] = None,
    data_root: Optional[Path] = None,
) -> pd.DataFrame:
    """Return wide-form daily composite score (date index, ticker columns).

    Reads ``data/lthcs/snapshots/*.json`` rather than the rolling
    per-ticker history file, since snapshots are the canonical source
    that also carry sub-scores.
    """
    root = Path(data_root) if data_root is not None else _default_data_root()
    snapshots = _load_all_snapshots(root)
    universe = _filter_universe(snapshots, tickers)
    return _build_wide_frame(
        snapshots, lambda r: r.get("lthcs_score"), universe
    )


def load_band_history(
    tickers: Optional[List[str]] = None,
    data_root: Optional[Path] = None,
) -> pd.DataFrame:
    """Return wide-form daily band assignment (date index, ticker cols)."""
    root = Path(data_root) if data_root is not None else _default_data_root()
    snapshots = _load_all_snapshots(root)
    universe = _filter_universe(snapshots, tickers)
    return _build_wide_frame(snapshots, lambda r: r.get("band"), universe)


def load_pillar_history(
    pillar: str,
    tickers: Optional[List[str]] = None,
    data_root: Optional[Path] = None,
) -> pd.DataFrame:
    """Return wide-form daily sub_score for a given pillar.

    Recognised pillars match ``PILLAR_NAMES``. Raises ``ValueError`` for
    an unknown pillar so typos surface fast instead of silently producing
    an empty frame.
    """
    if pillar not in PILLAR_NAMES:
        raise ValueError(
            "Unknown pillar %r; expected one of %s" % (pillar, PILLAR_NAMES)
        )
    root = Path(data_root) if data_root is not None else _default_data_root()
    snapshots = _load_all_snapshots(root)
    universe = _filter_universe(snapshots, tickers)

    def _val(row: Dict[str, Any]) -> Optional[float]:
        sub = row.get("subscores") or row.get("sub_scores")
        if not isinstance(sub, dict):
            return None
        return sub.get(pillar)

    return _build_wide_frame(snapshots, _val, universe)


# ---------------------------------------------------------------------------
# Yahoo price fetch + forward returns
# ---------------------------------------------------------------------------

def _price_cache_path(cache_root: Path, ticker: str) -> Path:
    safe = ticker.replace("/", "_")
    return cache_root / "prices" / ("%s.json" % safe)


def _fetch_prices(
    ticker: str,
    cache_root: Path,
    period: str = "2y",
    yahoo_module=None,
) -> List[Dict[str, Any]]:
    """Pull daily bars for ``ticker`` and cache them indefinitely.

    Cache layout: ``<cache_root>/prices/<ticker>.json`` storing the
    yfinance-shaped list. Historical prices don't change for past dates,
    so we don't TTL these. To refresh, delete the file.
    """
    cache_path = _price_cache_path(cache_root, ticker)
    if cache_path.exists():
        try:
            return _read_json(cache_path) or []
        except (OSError, json.JSONDecodeError):
            # Corrupt cache file -> ignore and re-fetch.
            pass

    if yahoo_module is None:
        from lthcs.sources import yahoo as yahoo_module  # local import

    try:
        rows = yahoo_module.get_daily_prices(ticker, period=period)
    except Exception as exc:  # network / parse failure
        warnings.warn(
            "backtest: yahoo fetch failed for %s: %s" % (ticker, exc),
            RuntimeWarning,
        )
        rows = []

    if rows:
        _atomic_write_json(cache_path, rows)
    return rows


def _prices_to_close_series(rows: List[Dict[str, Any]]) -> pd.Series:
    """Convert yfinance row list to a pd.Series of adj_close indexed by date."""
    if not rows:
        return pd.Series(dtype=float)
    dates: List[Any] = []
    values: List[float] = []
    for r in rows:
        d = r.get("date")
        # Prefer adj_close (handles splits/dividends).
        v = r.get("adj_close")
        if v is None:
            v = r.get("close")
        if d is None or v is None:
            continue
        try:
            values.append(float(v))
        except (TypeError, ValueError):
            continue
        dates.append(d)
    if not dates:
        return pd.Series(dtype=float)
    s = pd.Series(values, index=pd.to_datetime(dates))
    s = s[~s.index.duplicated(keep="last")]
    s.sort_index(inplace=True)
    return s


def fetch_forward_returns(
    tickers: List[str],
    start_date: str,
    end_date: str,
    horizons_days: Optional[List[int]] = None,
    data_root: Optional[Path] = None,
    cache_root: Optional[Path] = None,
    yahoo_module=None,
) -> Dict[int, pd.DataFrame]:
    """Fetch daily closes and compute forward returns at each horizon.

    Returns a dict keyed by horizon-in-trading-days. Each value is a
    wide DataFrame indexed by date (the observation date — i.e. the day
    of the score) with columns = ticker and values = the realized
    log-return-style simple return from ``close[t]`` to ``close[t + h]``
    (skipping over non-trading days; index aligns to trading days only).

    ``start_date`` and ``end_date`` are the observation-date bounds. We
    actually need price data through ``end_date + max(horizons)`` trading
    days to compute the longest-horizon return on the last observation.
    The Yahoo fetch uses a ``2y`` period which comfortably covers the
    LTHCS history (few weeks) plus a 63-day forward window.

    Tickers with no price data return an empty column in each frame.
    """
    if horizons_days is None:
        horizons_days = list(DEFAULT_HORIZONS)
    if not horizons_days:
        return {}

    cache_root = Path(cache_root) if cache_root is not None else _default_cache_root()

    start_ts = pd.Timestamp(start_date)
    end_ts = pd.Timestamp(end_date)

    # Build close-price panel.
    close_by_ticker: Dict[str, pd.Series] = {}
    for t in tickers:
        rows = _fetch_prices(t, cache_root=cache_root, yahoo_module=yahoo_module)
        close_by_ticker[t] = _prices_to_close_series(rows)

    if not close_by_ticker:
        return {h: pd.DataFrame() for h in horizons_days}

    closes = pd.DataFrame(close_by_ticker)
    # Ensure the index is datetime even when every column was empty
    # (an all-empty DataFrame defaults to a RangeIndex, which breaks
    # the timestamp comparisons below).
    if closes.empty:
        closes.index = pd.DatetimeIndex([])
    else:
        closes.index = pd.to_datetime(closes.index)
    closes.sort_index(inplace=True)

    out: Dict[int, pd.DataFrame] = {}
    for h in horizons_days:
        if h <= 0:
            raise ValueError("horizon must be > 0, got %r" % (h,))
        # Forward return computed on the trading-day index:
        # close[t + h] / close[t] - 1.
        fwd_trading = closes.shift(-h) / closes - 1.0
        if fwd_trading.empty:
            out[h] = fwd_trading
            continue
        # Align to a calendar-day observation index spanning [start, end]
        # so callers can join scores stamped on weekends/holidays. For
        # each calendar day, snap to the most recent prior trading-day
        # entry (so a Sun score uses Fri's entry price and Fri's
        # h-trading-days-forward return). Only forward-fill from
        # trading-day rows whose close[t+h] is known (so we never
        # synthesise future returns).
        cal_idx = pd.date_range(start=start_ts, end=end_ts, freq="D")
        if cal_idx.empty:
            mask = (fwd_trading.index >= start_ts) & (fwd_trading.index <= end_ts)
            out[h] = fwd_trading.loc[mask].copy()
            continue
        # Only keep trading-day rows where the forward return is fully
        # computable, i.e. close[t+h] exists. ``fwd_trading`` already has
        # NaN for rows where the forward close is missing, so dropping
        # entirely-NaN rows is conservative when only some tickers are
        # missing; instead we just align then forward-fill per-column.
        union = fwd_trading.index.union(cal_idx).sort_values()
        aligned = fwd_trading.reindex(union).ffill()
        aligned = aligned.loc[aligned.index.isin(cal_idx)]
        mask = (aligned.index >= start_ts) & (aligned.index <= end_ts)
        out[h] = aligned.loc[mask].copy()
    return out


# ---------------------------------------------------------------------------
# Band portfolio P&L
# ---------------------------------------------------------------------------

def _max_drawdown(equity: pd.Series) -> float:
    """Largest peak-to-trough drawdown on an equity curve, as a negative float."""
    if equity.empty:
        return 0.0
    peak = equity.cummax()
    dd = equity / peak - 1.0
    return float(dd.min())


def _annualized_sharpe(daily_returns: pd.Series) -> float:
    """Annualized Sharpe assuming 252 trading days; 0 if undefined."""
    if daily_returns.empty:
        return 0.0
    r = daily_returns.dropna()
    if r.empty:
        return 0.0
    std = r.std(ddof=1) if len(r) > 1 else 0.0
    if not std or math.isnan(std) or std == 0.0:
        return 0.0
    return float(r.mean() / std * math.sqrt(TRADING_DAYS_PER_YEAR))


def band_portfolio_returns(
    band_history: pd.DataFrame,
    forward_returns: pd.DataFrame,
    bands_to_long: Optional[List[str]] = None,
    bands_to_short: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Form equal-weight long/short portfolios by band on each rebalance date.

    For each row in ``band_history`` (the observation date), tickers
    whose band is in ``bands_to_long`` go long and those in
    ``bands_to_short`` go short. The portfolio return for that rebalance
    is the average forward return of the longs minus the average of the
    shorts (if either side is empty we treat its leg as zero).

    Turnover is the average fraction of names that change between
    consecutive rebalance dates (Jaccard distance on the union of
    long+short legs).
    """
    if bands_to_long is None:
        bands_to_long = list(DEFAULT_LONG_BANDS)
    if bands_to_short is None:
        bands_to_short = list(DEFAULT_SHORT_BANDS)
    long_set = {b.lower() for b in bands_to_long}
    short_set = {b.lower() for b in bands_to_short}

    if band_history.empty or forward_returns.empty:
        return {
            "daily_returns": pd.Series(dtype=float),
            "cumulative_return": 0.0,
            "sharpe": 0.0,
            "max_drawdown": 0.0,
            "turnover_per_rebalance": 0.0,
            "n_rebalances": 0,
            "n_long_avg": 0.0,
            "n_short_avg": 0.0,
        }

    # Align both frames on date index and column set.
    common_cols = band_history.columns.intersection(forward_returns.columns)
    common_idx = band_history.index.intersection(forward_returns.index)
    bh = band_history.loc[common_idx, common_cols]
    fr = forward_returns.loc[common_idx, common_cols]

    daily_returns: Dict[Any, float] = {}
    n_long_list: List[int] = []
    n_short_list: List[int] = []
    prev_members: Optional[set] = None
    turnover_samples: List[float] = []

    for date in bh.index:
        bands_row = bh.loc[date]
        rets_row = fr.loc[date]

        # Membership masks. Lowercase compare so 'Elite' vs 'elite' both work.
        bands_str = bands_row.dropna().astype(str).str.lower()
        long_members = bands_str.index[bands_str.isin(long_set)].tolist()
        short_members = bands_str.index[bands_str.isin(short_set)].tolist()

        # Drop members whose forward return is NaN (missing prices etc).
        long_valid = [t for t in long_members if not pd.isna(rets_row.get(t, np.nan))]
        short_valid = [t for t in short_members if not pd.isna(rets_row.get(t, np.nan))]

        long_ret = float(np.mean([rets_row[t] for t in long_valid])) if long_valid else 0.0
        short_ret = float(np.mean([rets_row[t] for t in short_valid])) if short_valid else 0.0
        port_ret = long_ret - short_ret

        daily_returns[date] = port_ret
        n_long_list.append(len(long_valid))
        n_short_list.append(len(short_valid))

        # Turnover: fraction of names that changed vs previous rebalance.
        members = set(long_valid) | set(short_valid)
        if prev_members is not None:
            union = members | prev_members
            if union:
                changed = len(members.symmetric_difference(prev_members))
                turnover_samples.append(changed / len(union))
        prev_members = members

    daily_series = pd.Series(daily_returns).sort_index()
    # Cumulative compounded return.
    if daily_series.empty:
        cum = 0.0
    else:
        cum = float((1.0 + daily_series.fillna(0.0)).prod() - 1.0)
    equity = (1.0 + daily_series.fillna(0.0)).cumprod()

    return {
        "daily_returns": daily_series,
        "cumulative_return": cum,
        "sharpe": _annualized_sharpe(daily_series),
        "max_drawdown": _max_drawdown(equity),
        "turnover_per_rebalance": float(np.mean(turnover_samples)) if turnover_samples else 0.0,
        "n_rebalances": int(len(daily_series)),
        "n_long_avg": float(np.mean(n_long_list)) if n_long_list else 0.0,
        "n_short_avg": float(np.mean(n_short_list)) if n_short_list else 0.0,
    }


# ---------------------------------------------------------------------------
# Quintile sort
# ---------------------------------------------------------------------------

def _quintile_buckets(n: int, q: int = 5) -> List[int]:
    """Return a list of bucket sizes summing to ``n``.

    Quintiles are as equal as possible; the LAST bucket gets the
    remainder (so Q5 carries the extras when ``n`` is not divisible by
    5). This matches the spec.
    """
    if n <= 0:
        return [0] * q
    base = n // q
    sizes = [base] * q
    rem = n - base * q
    sizes[-1] += rem
    return sizes


def pillar_quintile_returns(
    pillar_history: pd.DataFrame,
    forward_returns: pd.DataFrame,
    horizon_days: int = 21,
) -> pd.DataFrame:
    """Cross-sectional quintile sort by pillar sub-score.

    For each observation date, rank tickers by pillar score ascending,
    split into 5 buckets (Q1 = lowest scores, Q5 = highest), then take
    the mean forward return per bucket. Appends a ``Q5-Q1`` row showing
    top-minus-bottom spread.

    Output DataFrame is indexed by quintile label
    ``['Q1','Q2','Q3','Q4','Q5','Q5-Q1']`` with one column per
    observation date.
    """
    if pillar_history.empty or forward_returns.empty:
        return pd.DataFrame(index=["Q1", "Q2", "Q3", "Q4", "Q5", "Q5-Q1"])

    common_idx = pillar_history.index.intersection(forward_returns.index)
    common_cols = pillar_history.columns.intersection(forward_returns.columns)
    ph = pillar_history.loc[common_idx, common_cols]
    fr = forward_returns.loc[common_idx, common_cols]

    out: Dict[Any, Dict[str, float]] = {}
    labels = ["Q1", "Q2", "Q3", "Q4", "Q5"]

    for date in ph.index:
        scores = ph.loc[date].dropna()
        rets = fr.loc[date]
        # Drop tickers with no forward return.
        valid_idx = scores.index.intersection(rets.dropna().index)
        scores = scores.loc[valid_idx]
        rets = rets.loc[valid_idx]
        if len(scores) < 5:
            # Not enough names to form 5 quintiles → emit NaNs.
            out[date] = {lab: float("nan") for lab in labels + ["Q5-Q1"]}
            continue

        # Stable sort by score ascending; tie-broken by ticker name.
        order = scores.sort_values(kind="mergesort").index.tolist()
        sizes = _quintile_buckets(len(order), q=5)
        bucket_returns: List[float] = []
        cursor = 0
        for size in sizes:
            members = order[cursor : cursor + size]
            cursor += size
            if not members:
                bucket_returns.append(float("nan"))
            else:
                bucket_returns.append(float(rets.loc[members].mean()))
        row = {labels[i]: bucket_returns[i] for i in range(5)}
        row["Q5-Q1"] = bucket_returns[4] - bucket_returns[0]
        out[date] = row

    df = pd.DataFrame(out).reindex(["Q1", "Q2", "Q3", "Q4", "Q5", "Q5-Q1"])
    return df


# ---------------------------------------------------------------------------
# Information coefficient attribution
# ---------------------------------------------------------------------------

def _spearman_ic(scores: pd.Series, rets: pd.Series) -> Optional[float]:
    """Rank correlation between two aligned series.

    Returns None if there's no overlap or insufficient variance.
    """
    s = scores.dropna()
    r = rets.dropna()
    common = s.index.intersection(r.index)
    if len(common) < 3:
        return None
    s = s.loc[common]
    r = r.loc[common]
    # Spearman == Pearson on ranks.
    s_rank = s.rank()
    r_rank = r.rank()
    if s_rank.std(ddof=1) == 0 or r_rank.std(ddof=1) == 0:
        return None
    corr = float(s_rank.corr(r_rank))
    if math.isnan(corr):
        return None
    return corr


def attribute_returns(
    score_history: pd.DataFrame,
    pillar_histories: Dict[str, pd.DataFrame],
    forward_returns: pd.DataFrame,
) -> pd.DataFrame:
    """Per-pillar information coefficient over the backtest window.

    For each pillar, computes a Spearman rank correlation between the
    pillar score and the forward return on every observation date, then
    summarises across dates.

    Returns DataFrame with columns:
      * pillar        — pillar name (or 'composite' for the overall score)
      * ic_mean       — mean of daily ICs
      * ic_std        — sample stdev of daily ICs (0 when n_obs<2)
      * ic_sharpe     — ic_mean / ic_std, annualized by sqrt(252); 0 if undefined
      * n_obs         — number of dates with a computable IC
    """
    # Always include the composite score as one row so the caller can
    # see how the headline number compares to its constituents.
    series_map: Dict[str, pd.DataFrame] = {"composite": score_history}
    series_map.update(pillar_histories or {})

    rows: List[Dict[str, Any]] = []
    common_dates = forward_returns.index
    for name, sh in series_map.items():
        if sh is None or sh.empty:
            rows.append(
                {"pillar": name, "ic_mean": 0.0, "ic_std": 0.0,
                 "ic_sharpe": 0.0, "n_obs": 0}
            )
            continue
        dates = sh.index.intersection(common_dates)
        ics: List[float] = []
        for date in dates:
            ic = _spearman_ic(sh.loc[date], forward_returns.loc[date])
            if ic is not None:
                ics.append(ic)
        if not ics:
            rows.append(
                {"pillar": name, "ic_mean": 0.0, "ic_std": 0.0,
                 "ic_sharpe": 0.0, "n_obs": 0}
            )
            continue
        arr = np.array(ics, dtype=float)
        mean = float(arr.mean())
        std = float(arr.std(ddof=1)) if len(arr) > 1 else 0.0
        if std > 0:
            sharpe = mean / std * math.sqrt(TRADING_DAYS_PER_YEAR)
        else:
            sharpe = 0.0
        rows.append(
            {
                "pillar": name,
                "ic_mean": mean,
                "ic_std": std,
                "ic_sharpe": float(sharpe),
                "n_obs": int(len(arr)),
            }
        )
    df = pd.DataFrame(rows, columns=["pillar", "ic_mean", "ic_std", "ic_sharpe", "n_obs"])
    # Sort with composite first, pillars by descending IC mean.
    if not df.empty:
        composite_mask = df["pillar"] == "composite"
        composite = df[composite_mask]
        others = df[~composite_mask].sort_values("ic_mean", ascending=False)
        df = pd.concat([composite, others], ignore_index=True)
    return df


# ---------------------------------------------------------------------------
# JSON-friendly serializers (used by the CLI)
# ---------------------------------------------------------------------------

def _series_to_jsonable(s: pd.Series) -> Dict[str, float]:
    return {
        (idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime") else str(idx)): (
            None if pd.isna(val) else float(val)
        )
        for idx, val in s.items()
    }


def _frame_to_jsonable(df: pd.DataFrame) -> Dict[str, Dict[str, float]]:
    out: Dict[str, Dict[str, float]] = {}
    for col in df.columns:
        key = col.strftime("%Y-%m-%d") if hasattr(col, "strftime") else str(col)
        out[key] = {
            str(idx): (None if pd.isna(val) else float(val))
            for idx, val in df[col].items()
        }
    return out


def serialize_portfolio_result(result: Dict[str, Any]) -> Dict[str, Any]:
    """Convert ``band_portfolio_returns`` output to JSON-serialisable dict."""
    return {
        "daily_returns": _series_to_jsonable(result["daily_returns"]),
        "cumulative_return": float(result["cumulative_return"]),
        "sharpe": float(result["sharpe"]),
        "max_drawdown": float(result["max_drawdown"]),
        "turnover_per_rebalance": float(result["turnover_per_rebalance"]),
        "n_rebalances": int(result["n_rebalances"]),
        "n_long_avg": float(result["n_long_avg"]),
        "n_short_avg": float(result["n_short_avg"]),
    }


# ---------------------------------------------------------------------------
# Markdown report emitter
# ---------------------------------------------------------------------------

def _fmt_num(v: Any, fmt: str = "%+.4f", na: str = "n/a") -> str:
    """Format a number; tolerate None / NaN / non-finite."""
    if v is None:
        return na
    try:
        x = float(v)
    except (TypeError, ValueError):
        return na
    if not math.isfinite(x):
        return na
    return fmt % x


def _quintile_spread_means(quintile_payload: Optional[Dict[str, Any]]) -> Dict[str, Dict[str, float]]:
    """Reduce on-disk quintile payload to {pillar: {mean, n}} for Q5-Q1.

    Tolerates the legacy ``quintile_spreads.json`` flat shape and the
    current ``quintile_returns.json`` nested shape.
    """
    out: Dict[str, Dict[str, float]] = {}
    if not quintile_payload:
        return out
    for pillar, per_q in quintile_payload.items():
        if not isinstance(per_q, dict):
            continue
        spreads: List[float] = []
        q51 = per_q.get("Q5-Q1")
        if isinstance(q51, dict):
            for v in q51.values():
                if v is None:
                    continue
                try:
                    x = float(v)
                except (TypeError, ValueError):
                    continue
                if math.isfinite(x):
                    spreads.append(x)
        if spreads:
            out[pillar] = {"mean": sum(spreads) / len(spreads), "n": len(spreads)}
        else:
            out[pillar] = {"mean": float("nan"), "n": 0}
    return out


def build_report_markdown(
    summary: Dict[str, Any],
    quintile_payload: Optional[Dict[str, Any]] = None,
) -> str:
    """Render a human-readable markdown report from a ``summary.json`` payload.

    Inputs match what :func:`scripts/lthcs_backtest.py` writes to disk so the
    report can be regenerated from artifacts alone (see ``--from-json``).
    """
    run_id = summary.get("run_id", "unknown")
    generated_at = summary.get("generated_at", "")
    start = summary.get("start") or summary.get("window", {}).get("start", "")
    end = summary.get("end") or summary.get("window", {}).get("end", "")
    horizon = summary.get("horizon_days", summary.get("horizon", ""))
    bands_long = summary.get("bands_long", [])
    bands_short = summary.get("bands_short", [])
    n_tickers = summary.get("n_tickers", 0)
    n_dates = summary.get("n_observation_dates", 0)
    port = summary.get("portfolio", {}) or {}
    pillar_ic = summary.get("pillar_ic", []) or []
    spread_means = _quintile_spread_means(quintile_payload)

    lines: List[str] = []
    lines.append("# LTHCS Backtest — %s" % run_id)
    lines.append("")
    if generated_at:
        lines.append("Generated: **%s**" % generated_at)
    lines.append("- Window: **%s -> %s**" % (start, end))
    lines.append("- Horizon: **%s trading days**" % horizon)
    lines.append("- Universe: **%d** tickers across **%d** observation dates" %
                 (int(n_tickers or 0), int(n_dates or 0)))
    lines.append("- Long bands: %s" % (list(bands_long) or "n/a"))
    lines.append("- Short bands: %s" % (list(bands_short) or "n/a"))
    lines.append("")

    lines.append("## Band-portfolio P&L")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|:-------|------:|")
    lines.append("| Rebalances | %s |" % port.get("n_rebalances", "n/a"))
    lines.append("| Cumulative return | %s |" % _fmt_num(port.get("cumulative_return")))
    lines.append("| Sharpe (annualised) | %s |" % _fmt_num(port.get("sharpe"), "%+.3f"))
    lines.append("| Max drawdown | %s |" % _fmt_num(port.get("max_drawdown")))
    lines.append("| Hit rate | %s |" % _fmt_num(port.get("hit_rate"), "%.3f"))
    lines.append("| Turnover / rebalance | %s |" % _fmt_num(port.get("turnover_per_rebalance"), "%.4f"))
    lines.append("| Avg n_long | %s |" % _fmt_num(port.get("n_long_avg"), "%.1f"))
    lines.append("| Avg n_short | %s |" % _fmt_num(port.get("n_short_avg"), "%.1f"))
    lines.append("")
    lines.append("> NOTE: at horizons > 1d, forward returns are overlapping so Sharpe and")
    lines.append("> cumulative return are inflated by serial correlation. Treat the IC")
    lines.append("> numbers and 1-day Sharpe (if computed) as the honest readings.")
    lines.append("")

    lines.append("## Pillar Information Coefficient (Spearman vs forward return)")
    lines.append("")
    if pillar_ic:
        lines.append("| Pillar | IC mean | IC std | IC Sharpe (ann.) | n_obs |")
        lines.append("|:-------|--------:|-------:|-----------------:|------:|")
        for row in pillar_ic:
            lines.append("| %s | %s | %s | %s | %s |" % (
                row.get("pillar", "?"),
                _fmt_num(row.get("ic_mean")),
                _fmt_num(row.get("ic_std"), "%.4f"),
                _fmt_num(row.get("ic_sharpe"), "%+.3f"),
                row.get("n_obs", "n/a"),
            ))
    else:
        lines.append("_No pillar IC data._")
    lines.append("")

    lines.append("## Quintile Q5-Q1 spread (mean across dates)")
    lines.append("")
    if spread_means:
        lines.append("| Pillar | mean spread | n |")
        lines.append("|:-------|------------:|--:|")
        for pillar in PILLAR_NAMES:
            entry = spread_means.get(pillar)
            if entry is None or entry.get("n", 0) == 0:
                lines.append("| %s | n/a | 0 |" % pillar)
            else:
                lines.append("| %s | %s | %d |" % (
                    pillar, _fmt_num(entry.get("mean")), int(entry.get("n", 0)),
                ))
    else:
        lines.append("_No quintile spread data (run produced no quintile_returns.json)._")
    lines.append("")

    return "\n".join(lines) + "\n"


def _atomic_write_text(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".md", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_report(
    out_root: Path,
    summary: Dict[str, Any],
    quintile_payload: Optional[Dict[str, Any]] = None,
) -> Path:
    """Write a markdown report to ``<out_root>/report.md`` and return the path."""
    out_root = Path(out_root)
    md = build_report_markdown(summary, quintile_payload=quintile_payload)
    target = out_root / "report.md"
    _atomic_write_text(target, md)
    return target


def write_report_from_dir(out_root: Path) -> Path:
    """Regenerate ``report.md`` from the JSON artifacts already on disk.

    Useful for backfilling a run whose original report wasn't emitted; reads
    ``summary.json`` and (if present) ``quintile_returns.json`` / legacy
    ``quintile_spreads.json``.
    """
    out_root = Path(out_root)
    summary_path = out_root / "summary.json"
    if not summary_path.exists():
        raise FileNotFoundError(
            "cannot regenerate report: %s missing" % summary_path
        )
    summary = _read_json(summary_path)
    quintile_payload: Optional[Dict[str, Any]] = None
    for fname in ("quintile_returns.json", "quintile_spreads.json"):
        p = out_root / fname
        if p.exists():
            try:
                quintile_payload = _read_json(p)
            except Exception:
                quintile_payload = None
            break
    return write_report(out_root, summary, quintile_payload=quintile_payload)


__all__ = [
    "PILLAR_NAMES",
    "DEFAULT_HORIZONS",
    "DEFAULT_LONG_BANDS",
    "DEFAULT_SHORT_BANDS",
    "load_score_history",
    "load_band_history",
    "load_pillar_history",
    "fetch_forward_returns",
    "band_portfolio_returns",
    "pillar_quintile_returns",
    "attribute_returns",
    "serialize_portfolio_result",
    "build_report_markdown",
    "write_report",
    "write_report_from_dir",
]
