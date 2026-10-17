"""Pandas 3.0 compatibility for the crypto-odds engine.

Pandas 3 infers datetime resolution from the input and makes ``astype(str)``
keep missing values. Those two changes would break the DVOL ``merge_asof`` and
would write blanks where pandas 2.3 wrote ``None`` / ``nan``. The helpers in
``pandas_compat`` pin the stored values back, and these tests do not import
the pricing or model modules (those pull in scipy, which the default test
environment does not install).
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "crypto_odds" / "engine" / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from crypto_options.backtest.panel import Series, events  # noqa: E402
from crypto_options.pandas_compat import (  # noqa: E402
    as_naive_ns,
    as_py_str,
    merge_asof_utc,
    nat_for,
    utc_ns,
)


def test_utc_ns_collapses_second_and_millisecond_epochs():
    seconds = pd.to_datetime([1_700_000_000], unit="s", utc=True)
    millis = pd.to_datetime([1_700_000_000_000], unit="ms", utc=True)
    a = utc_ns(seconds)
    b = utc_ns(millis)
    assert str(a.dtype) == "datetime64[ns, UTC]"
    assert str(b.dtype) == "datetime64[ns, UTC]"
    assert a.astype("int64").tolist() == b.astype("int64").tolist()
    stamp = utc_ns(pd.Timestamp(1_700_000_000, unit="s", tz="UTC"))
    assert stamp.as_unit("ns").value == int(a.astype("int64")[0])


def test_merge_asof_utc_joins_mixed_resolutions_without_touching_inputs():
    left = pd.DataFrame({
        "t": pd.to_datetime(["2024-01-02", "2024-01-05"], utc=True).astype("datetime64[s, UTC]"),
        "i": [0, 1],
    })
    right = pd.DataFrame({
        "t": pd.to_datetime(["2024-01-01", "2024-01-04"], utc=True).astype("datetime64[ms, UTC]"),
        "dvol": [40.0, 55.0],
    })
    right = right.assign(t_obs=right["t"])
    left_before = str(left["t"].dtype)
    right_before = str(right["t"].dtype)

    if pd.__version__.split(".")[0] >= "3":
        with pytest.raises(pd.errors.MergeError):
            pd.merge_asof(left.sort_values("t"), right.sort_values("t"), on="t", direction="backward")

    merged = merge_asof_utc(left, right, on="t", direction="backward").sort_values("i")
    assert str(left["t"].dtype) == left_before
    assert str(right["t"].dtype) == right_before
    assert str(merged["t"].dtype) == "datetime64[ns, UTC]"
    assert merged["dvol"].tolist() == [40.0, 55.0]
    age_days = (merged["t"] - merged["t_obs"]).dt.total_seconds() / 86400
    assert age_days.tolist() == [1.0, 1.0]


def test_as_py_str_writes_missing_values_the_way_pandas_2_did(tmp_path):
    frame = pd.DataFrame({
        "params": [{"C": 0.0001}, None, "x"],
        "level": [0.1, np.nan, 1.0],
    })
    text = as_py_str(frame).to_csv(index=False)
    (tmp_path / "refits.csv").write_text(text)
    lines = text.splitlines()
    assert lines[2] == "None,nan"
    # pandas 3's string dtype would leave the missing cells blank
    assert '""' not in text
    assert as_py_str(frame["params"]).tolist()[0] == "{'C': 0.0001}"


def test_all_missing_quote_timestamps_stay_nanoseconds():
    raw = pd.Series([pd.NaT, pd.NaT])
    assert str(raw.dtype).startswith("datetime64[s]")
    pinned = as_naive_ns(raw)
    assert str(pinned.dtype) == "datetime64[ns]"
    assert pinned.isna().all()


def test_events_keep_outcome_labels_on_nanosecond_and_second_clocks():
    days = pd.date_range("2024-01-01", periods=5, freq="D", tz="UTC")
    price = np.array([100.0, 101.0, 130.0, 90.0, 100.0])
    ns = Series("BTC", "spot", "test", utc_ns(days), utc_ns(days + pd.Timedelta(days=1)), price, None)
    seconds = Series(
        "BTC", "spot", "test",
        pd.DatetimeIndex(days.astype("datetime64[s, UTC]")),
        pd.DatetimeIndex((days + pd.Timedelta(days=1)).astype("datetime64[s, UTC]")),
        price, None,
    )
    ev_ns = events(ns, h=1, targets=[0.10, -0.10])
    ev_s = events(seconds, h=1, targets=[0.10, -0.10])
    assert str(ev_ns["outcome_date"].dtype) == "datetime64[ns, UTC]"
    assert str(ev_s["label_available"].dtype).startswith("datetime64[s]")
    assert np.array_equal(ev_ns["y"].to_numpy(), ev_s["y"].to_numpy(), equal_nan=True)
    assert utc_ns(ev_ns["outcome_date"]).astype("int64").tolist() == utc_ns(ev_s["outcome_date"]).astype("int64").tolist()
    # last origin has no t+1 print, so its label is missing and unscored
    assert ev_ns["y"].iloc[-1] != ev_ns["y"].iloc[-1]
    assert nat_for(seconds.dates).dtype.name.endswith("[s]")


def test_dvol_joins_go_through_the_resolution_helper():
    root = SRC / "crypto_options" / "backtest"
    for name in ("features.py", "models.py"):
        text = (root / name).read_text()
        assert "merge_asof_utc(" in text
        assert "pd.merge_asof(" not in text
