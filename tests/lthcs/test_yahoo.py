"""Tests for lthcs.sources.yahoo.

All tests mock ``yfinance.Ticker`` so no network traffic is generated.
The module-level cache singleton is redirected to ``tmp_path`` via
``monkeypatch`` so each test starts with an empty cache.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import List
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from lthcs.sources import yahoo
from lthcs.sources._cache import FileCache
from lthcs.sources._ratelimit import TokenBucket


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FileCache:
    """Point the module-level cache at a fresh tmp dir for every test."""
    fresh = FileCache("yahoo", root=tmp_path)
    monkeypatch.setattr(yahoo, "_cache", fresh)
    return fresh


@pytest.fixture(autouse=True)
def fast_bucket(monkeypatch: pytest.MonkeyPatch) -> TokenBucket:
    """Replace the rate-limited bucket with a generously-sized one.

    Tests shouldn't burn wall-clock time waiting on a 1 req/sec bucket.
    """
    bucket = TokenBucket(capacity=1_000_000, refill_rate=1_000_000)
    monkeypatch.setattr(yahoo, "_bucket", bucket)
    return bucket


def _make_df(closes: List[float], *, start: str = "2026-01-02") -> pd.DataFrame:
    """Build a synthetic OHLCV dataframe with the requested close series.

    Open/High/Low/Adj Close are derived from ``closes`` and volume is a
    constant — tests that care about a specific value should pass their
    own dataframe directly.
    """
    n = len(closes)
    idx = pd.bdate_range(start=start, periods=n)
    df = pd.DataFrame(
        {
            "Open": [c - 0.5 for c in closes],
            "High": [c + 1.0 for c in closes],
            "Low": [c - 1.0 for c in closes],
            "Close": list(closes),
            "Adj Close": list(closes),
            "Volume": [1_000_000] * n,
        },
        index=idx,
    )
    df.index.name = "Date"
    return df


def _patch_ticker(df: pd.DataFrame) -> MagicMock:
    """Return a configured ``patch`` context for ``yfinance.Ticker``."""
    mock_ticker = MagicMock()
    mock_ticker.return_value.history.return_value = df
    return mock_ticker


# ---------------------------------------------------------------------------
# get_daily_prices
# ---------------------------------------------------------------------------


def test_get_daily_prices_shape() -> None:
    df = pd.DataFrame(
        {
            "Open": [100.0, 101.0, 102.0],
            "High": [101.0, 102.0, 103.0],
            "Low": [99.0, 100.0, 101.0],
            "Close": [100.5, 101.5, 102.5],
            "Adj Close": [100.4, 101.4, 102.4],
            "Volume": [1000, 1100, 1200],
        },
        index=pd.to_datetime(["2026-05-14", "2026-05-15", "2026-05-16"]),
    )
    df.index.name = "Date"

    with patch("yfinance.Ticker", _patch_ticker(df)):
        rows = yahoo.get_daily_prices("AAPL", period="5d")

    assert len(rows) == 3
    assert rows[0] == {
        "date": "2026-05-14",
        "open": 100.0,
        "high": 101.0,
        "low": 99.0,
        "close": 100.5,
        "adj_close": 100.4,
        "volume": 1000,
    }
    assert rows[2]["date"] == "2026-05-16"
    assert rows[2]["volume"] == 1200
    # All numerics are JSON-friendly primitives.
    for row in rows:
        assert isinstance(row["close"], float)
        assert isinstance(row["volume"], int)


def test_get_daily_prices_empty_dataframe_returns_empty_list() -> None:
    empty = pd.DataFrame(
        columns=["Open", "High", "Low", "Close", "Adj Close", "Volume"]
    )
    with patch("yfinance.Ticker", _patch_ticker(empty)):
        rows = yahoo.get_daily_prices("ZZZZ", period="5d")
    assert rows == []


def test_get_daily_prices_falls_back_when_adj_close_missing() -> None:
    # Some yfinance configurations (auto_adjust=True) omit Adj Close.
    df = pd.DataFrame(
        {
            "Open": [10.0],
            "High": [11.0],
            "Low": [9.0],
            "Close": [10.5],
            "Volume": [500],
        },
        index=pd.to_datetime(["2026-05-16"]),
    )
    df.index.name = "Date"
    with patch("yfinance.Ticker", _patch_ticker(df)):
        rows = yahoo.get_daily_prices("AAPL", period="1d")
    assert rows[0]["adj_close"] == 10.5  # mirrors close


# ---------------------------------------------------------------------------
# Cache behaviour
# ---------------------------------------------------------------------------


def test_cache_hit_avoids_second_yfinance_call() -> None:
    df = _make_df([100.0, 101.0, 102.0])

    mock_ticker = _patch_ticker(df)
    with patch("yfinance.Ticker", mock_ticker):
        first = yahoo.get_daily_prices("AAPL", period="5d")
        second = yahoo.get_daily_prices("AAPL", period="5d")

    assert first == second
    # Should have called yfinance exactly once.
    assert mock_ticker.call_count == 1


def test_cache_miss_after_ttl_refetches(
    isolated_cache: FileCache,
) -> None:
    df = _make_df([100.0, 101.0])
    mock_ticker = _patch_ticker(df)

    with patch("yfinance.Ticker", mock_ticker):
        yahoo.get_daily_prices("AAPL", period="5d")
        assert mock_ticker.call_count == 1

        # Simulate the cache entry being older than TTL by deleting it
        # (FileCache treats missing files as a miss, which is exactly what
        # happens once an entry is past its TTL — the get() returns None
        # and the source refetches).
        isolated_cache.delete("AAPL/prices/5d")

        yahoo.get_daily_prices("AAPL", period="5d")
        assert mock_ticker.call_count == 2


def test_different_periods_cached_separately() -> None:
    df = _make_df([100.0, 101.0])
    mock_ticker = _patch_ticker(df)
    with patch("yfinance.Ticker", mock_ticker):
        yahoo.get_daily_prices("AAPL", period="5d")
        yahoo.get_daily_prices("AAPL", period="1mo")
    assert mock_ticker.call_count == 2


# ---------------------------------------------------------------------------
# get_volatility
# ---------------------------------------------------------------------------


def test_get_volatility_matches_hand_computed() -> None:
    # Construct a closes series whose daily returns are known.
    # Returns: +1%, -1%, +1%, -1% (alternating).
    # Sample stdev of [0.01, -0.01, 0.01, -0.01]:
    #   mean = 0
    #   variance = (4 * 0.0001) / (n - 1) = 0.0004 / 3
    #   daily_std = sqrt(0.0004 / 3)
    #   annualized = daily_std * sqrt(252)
    closes = [100.0]
    rets = [0.01, -0.01, 0.01, -0.01]
    for r in rets:
        closes.append(closes[-1] * (1.0 + r))

    df = _make_df(closes)
    with patch("yfinance.Ticker", _patch_ticker(df)):
        vol = yahoo.get_volatility("AAPL", window=4)

    expected_daily_std = math.sqrt(0.0004 / 3.0)
    expected = expected_daily_std * math.sqrt(252)
    assert vol == pytest.approx(expected, rel=1e-9)


def test_get_volatility_insufficient_data_returns_none() -> None:
    # window=30 requires 31 closes; provide only 5.
    df = _make_df([100.0, 101.0, 102.0, 103.0, 104.0])
    with patch("yfinance.Ticker", _patch_ticker(df)):
        assert yahoo.get_volatility("AAPL", window=30) is None


def test_get_volatility_uses_only_trailing_window() -> None:
    # 100 closes total but window=4. The first 96 closes have wild swings
    # that should NOT influence the result.
    wild = [100.0]
    for i in range(95):
        # Big alternating moves.
        wild.append(wild[-1] * (1.10 if i % 2 == 0 else 0.90))
    # Last 5 closes (4 returns): calm +1%/-1%/+1%/-1%
    last5 = [wild[-1]]
    for r in [0.01, -0.01, 0.01, -0.01]:
        last5.append(last5[-1] * (1.0 + r))
    closes = wild + last5[1:]  # 100 closes

    df = _make_df(closes)
    with patch("yfinance.Ticker", _patch_ticker(df)):
        vol = yahoo.get_volatility("AAPL", window=4)

    expected = math.sqrt(0.0004 / 3.0) * math.sqrt(252)
    assert vol == pytest.approx(expected, rel=1e-9)


# ---------------------------------------------------------------------------
# get_momentum_pct
# ---------------------------------------------------------------------------


def test_get_momentum_pct_matches_hand_computed() -> None:
    # days=3 means compare last close to close 3 bars back.
    # Closes: 100, 110, 120, 130 -> momentum = 130/100 - 1 = 0.30
    df = _make_df([100.0, 110.0, 120.0, 130.0])
    with patch("yfinance.Ticker", _patch_ticker(df)):
        m = yahoo.get_momentum_pct("AAPL", days=3)
    assert m == pytest.approx(0.30, rel=1e-12)


def test_get_momentum_pct_negative() -> None:
    # 100 -> 80 over 1 bar => -20%.
    df = _make_df([100.0, 80.0])
    with patch("yfinance.Ticker", _patch_ticker(df)):
        m = yahoo.get_momentum_pct("AAPL", days=1)
    assert m == pytest.approx(-0.20, rel=1e-12)


def test_get_momentum_pct_insufficient_data_returns_none() -> None:
    # days=90 requires 91 closes; provide only 10.
    df = _make_df([100.0 + i for i in range(10)])
    with patch("yfinance.Ticker", _patch_ticker(df)):
        assert yahoo.get_momentum_pct("AAPL", days=90) is None


def test_momentum_and_volatility_share_cached_prices() -> None:
    # 31 closes — enough for both window=30 vol and days=10 momentum.
    closes = [100.0 + i * 0.5 for i in range(31)]
    df = _make_df(closes)
    mock_ticker = _patch_ticker(df)
    with patch("yfinance.Ticker", mock_ticker):
        v = yahoo.get_volatility("AAPL", window=30)
        m = yahoo.get_momentum_pct("AAPL", days=10)
    assert v is not None
    assert m is not None
    # First call fetched + cached; second call hit the cache.
    assert mock_ticker.call_count == 1
