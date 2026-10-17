"""Pandas 3.0 behavior pinned back to the pandas 2.3 results this engine writes.

Two changes in pandas 3.0 would otherwise move stored values:

* Datetimes take the resolution of their input (seconds, milliseconds, or
  microseconds) instead of always being nanoseconds. ``merge_asof`` rejects
  mixed resolutions, so a spot series built from second epochs no longer
  joins a DVOL series built from millisecond epochs. History parquet is
  ``datetime64[ns, UTC]``.
* ``Series.astype(str)`` uses the new string dtype and keeps missing values.
  ``to_csv`` then writes a blank where pandas 2.3 wrote ``None`` or ``nan``.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def utc_ns(values):
    """UTC timestamps at nanosecond resolution, matching pandas 2.3."""
    if isinstance(values, pd.Timestamp):
        ts = values.tz_localize("UTC") if values.tzinfo is None else values.tz_convert("UTC")
        return ts.as_unit("ns")
    return pd.to_datetime(values, utc=True).astype("datetime64[ns, UTC]")


def as_py_str(values):
    """Elementwise ``str()``, including missing values.

    Python ``None`` becomes ``"None"`` and ``NaN`` becomes ``"nan"``, which is
    what ``astype(str)`` produced on an object column in pandas 2.3.
    """
    return values.map(str)


def as_naive_ns(values):
    """Naive ``datetime64[ns]``, including an all-missing column.

    Pandas 3 stores a column of only ``NaT`` as ``datetime64[s]``. Quote
    timestamps written by pandas 2.3 were ``datetime64[ns]``.
    """
    return pd.to_datetime(values).astype("datetime64[ns]")


def nat_for(index):
    """``NaT`` whose resolution matches ``index``.

    ``np.datetime64('NaT')`` is nanoseconds. Mixing that sentinel into a
    seconds, milliseconds, or microseconds index changes the result unit.
    """
    unit = getattr(index.dtype, "unit", None)
    if not unit:
        name = getattr(index.dtype, "name", "") or str(index.dtype)
        if name.startswith("datetime64[") and name.endswith("]"):
            unit = name[len("datetime64["):-1]
        else:
            unit = "ns"
    return np.datetime64("NaT", unit)


def merge_asof_utc(left, right, on, **kwargs):
    """``merge_asof`` after both keys are nanosecond UTC.

    The copies keep the caller's frames intact (copy-on-write: assigning the
    cast key must not modify a frame the caller still holds).
    """
    left = left.copy()
    right = right.copy()
    left[on] = utc_ns(left[on])
    right[on] = utc_ns(right[on])
    return pd.merge_asof(left.sort_values(on), right.sort_values(on), on=on, **kwargs)
