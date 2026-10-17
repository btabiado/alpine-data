"""Origins, outcomes and label maturity for the walk-forward backtest (brief s.5, s.8).

A cohort series is one daily price series per asset. Origin t is a date in that series. Its information
time is the series' obs_time at t (08:00 UTC for settlement prices; 00:00 UTC next day for spot closes).
For horizon h, the outcome is the price dated exactly t + h, and that label becomes available at the
outcome's obs_time. A label may be used by a model at origin u only if it is available by u's
information time, which for one daily series means date(t) + h <= date(u).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from ..pandas_compat import nat_for, utc_ns


@dataclass
class Series:
    asset: str
    cohort: str
    source: str
    dates: pd.DatetimeIndex     # daily, strictly increasing (gaps allowed)
    info_time: pd.DatetimeIndex
    price: np.ndarray
    excluded_outcome_from: pd.Timestamp | None = None   # methodology break: outcomes on/after are not strict

    def __len__(self):
        return len(self.price)

    def truncate(self, i: int) -> "Series":
        """The series as it existed at origin i (for leakage tests)."""
        return Series(self.asset, self.cohort, self.source, self.dates[: i + 1], self.info_time[: i + 1],
                      self.price[: i + 1].copy(), self.excluded_outcome_from)

    def log_returns(self) -> np.ndarray:
        """r[i] = ln(P_i / P_{i-1}) per calendar day; NaN where the previous day is missing."""
        r = np.full(len(self), np.nan)
        consecutive = (self.dates[1:] - self.dates[:-1]) == pd.Timedelta(days=1)
        r[1:] = np.where(consecutive, np.log(self.price[1:] / self.price[:-1]), np.nan)
        return r

    def outcome_index(self, h: int) -> np.ndarray:
        """Index of the price dated exactly date_i + h, or -1 if absent (gap or not yet observed)."""
        target = self.dates + pd.Timedelta(days=h)
        j = self.dates.searchsorted(target)
        ok = (j < len(self)) & (self.dates[np.minimum(j, len(self) - 1)] == target)
        return np.where(ok, j, -1)

    def matured_through(self, h: int) -> np.ndarray:
        """For each origin i, the largest origin j whose h-day label is known at i (date_j + h <= date_i); -1 if none."""
        cutoff = self.dates - pd.Timedelta(days=h)
        return self.dates.searchsorted(cutoff, side="right") - 1


def load_series(cfg: dict, asset: str, cohort: str) -> Series | None:
    path = Path(cfg["paths"]["history"]) / f"{cohort}_{asset}.parquet"
    if not path.exists():
        return None
    df = pd.read_parquet(path).sort_values("date")
    bt = cfg["backtest"]
    if cohort == "settlement" and asset in bt.get("settlement_daily_from", {}):
        df = df[df["date"] >= pd.Timestamp(bt["settlement_daily_from"][asset], tz="UTC")]
    brk = bt.get("methodology_breaks", {}).get(cohort, {}).get(asset)
    return Series(asset, cohort, df["source"].iat[0], pd.DatetimeIndex(utc_ns(df["date"])),
                  pd.DatetimeIndex(utc_ns(df["available_time"])), df["price"].to_numpy(float),
                  utc_ns(pd.Timestamp(brk, tz="UTC")) if brk else None)


def events(s: Series, h: int, targets: list[float]) -> pd.DataFrame:
    """One row per (origin, target): threshold, event direction, outcome (NaN if not observed/strict)."""
    j = s.outcome_index(h)
    has = j >= 0
    out_price = np.where(has, s.price[np.maximum(j, 0)], np.nan)
    out_date = pd.DatetimeIndex(
        np.where(has, s.dates[np.maximum(j, 0)].values, nat_for(s.dates)), tz="UTC")
    strict = has.copy()
    if s.excluded_outcome_from is not None:
        strict &= ~(out_date >= s.excluded_outcome_from)
    rows = []
    for p in targets:
        up = p > 0
        thr = s.price * (1 + p)
        y = np.where(up, out_price > thr, out_price < thr).astype(float)
        y[~strict] = np.nan
        rows.append(pd.DataFrame({
            "origin_idx": np.arange(len(s)), "origin_date": s.dates, "info_time": s.info_time,
            "outcome_date": out_date,
            "label_available": np.where(has, s.info_time[np.maximum(j, 0)].values, nat_for(s.info_time)),
            "h": h, "target": p, "event": "S_T > K" if up else "S_T < K", "threshold": thr,
            "y": y, "outcome_excluded_methodology": has & ~strict,
        }))
    return pd.concat(rows, ignore_index=True)
