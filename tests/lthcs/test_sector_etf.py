"""Tests for lthcs.sources.sector_etf.

All yfinance traffic is mocked.  Module-level caches are redirected to
``tmp_path``; the underlying ``yahoo`` rate-limit bucket is replaced with
a no-wait bucket.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from lthcs.sources import sector_etf, yahoo
from lthcs.sources._cache import FileCache
from lthcs.sources._ratelimit import TokenBucket


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_caches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fresh cache for both the snapshot layer and the underlying yahoo source."""
    monkeypatch.setattr(
        sector_etf, "_cache", FileCache("sector_etf", root=tmp_path)
    )
    monkeypatch.setattr(yahoo, "_cache", FileCache("yahoo", root=tmp_path))


@pytest.fixture(autouse=True)
def fast_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        yahoo, "_bucket", TokenBucket(capacity=1_000_000, refill_rate=1_000_000)
    )


def _make_df(closes: List[float], *, start: str = "2025-11-01") -> pd.DataFrame:
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


def _ramp(start: float, end: float, n: int) -> List[float]:
    if n <= 1:
        return [end]
    step = (end - start) / (n - 1)
    return [start + step * i for i in range(n)]


def _make_ticker_router(ticker_to_df: Dict[str, Optional[pd.DataFrame]]) -> MagicMock:
    """Build a ``yfinance.Ticker``-shaped mock that dispatches on the ticker
    symbol passed to ``Ticker(...)``.  Each value is the dataframe the
    mock's ``history(...)`` will return; ``None`` makes ``history`` raise
    a RuntimeError to simulate a yfinance failure.
    """
    factory = MagicMock()

    def _build(symbol: str) -> MagicMock:
        inst = MagicMock()
        df = ticker_to_df.get(symbol)
        if df is None and symbol not in ticker_to_df:
            inst.history.return_value = pd.DataFrame(
                columns=["Open", "High", "Low", "Close", "Adj Close", "Volume"]
            )
        elif df is None:
            inst.history.side_effect = RuntimeError(f"yfinance fail for {symbol}")
        else:
            inst.history.return_value = df
        return inst

    factory.side_effect = _build
    return factory


# ---------------------------------------------------------------------------
# fetch_sector_strength: happy path
# ---------------------------------------------------------------------------


def _full_universe_dfs(
    *, spy_end: float = 110.0, etf_returns: Optional[Dict[str, float]] = None
) -> Dict[str, pd.DataFrame]:
    """Build a dict of dataframes covering SPY + all 11 sector ETFs.

    Each ETF's last close is set so its 3-month return is approximately
    ``etf_returns[etf]`` (defaults to a small mixture).  64 closes per
    ticker so 3m (63-bar) lookback returns are well-defined.
    """
    if etf_returns is None:
        etf_returns = {
            "XLK": 0.10,
            "XLF": 0.05,
            "XLE": -0.02,
            "XLI": 0.03,
            "XLY": 0.04,
            "XLP": 0.01,
            "XLV": 0.02,
            "XLB": 0.00,
            "XLU": -0.01,
            "XLRE": 0.06,
            "XLC": 0.08,
        }

    dfs: Dict[str, pd.DataFrame] = {}
    n = 64
    spy_start = 100.0
    dfs["SPY"] = _make_df(_ramp(spy_start, spy_end, n))
    for etf, ret in etf_returns.items():
        start = 100.0
        end = start * (1.0 + ret)
        dfs[etf] = _make_df(_ramp(start, end, n))
    return dfs


def test_fetch_sector_strength_has_expected_top_level_keys() -> None:
    dfs = _full_universe_dfs()
    factory = _make_ticker_router(dfs)
    with patch("yfinance.Ticker", factory):
        snap = sector_etf.fetch_sector_strength()
    for k in ("as_of", "benchmark_return_1m", "benchmark_return_3m", "sectors"):
        assert k in snap


def test_fetch_sector_strength_returns_all_eleven_sectors() -> None:
    dfs = _full_universe_dfs()
    factory = _make_ticker_router(dfs)
    with patch("yfinance.Ticker", factory):
        snap = sector_etf.fetch_sector_strength()
    assert set(snap["sectors"].keys()) == set(sector_etf.SECTOR_ETFS.keys())
    for etf, blk in snap["sectors"].items():
        assert blk["sector_name"] == sector_etf.SECTOR_ETFS[etf]
        for f in ("return_1m", "return_3m", "relative_1m", "relative_3m", "rank_1m", "rank_3m"):
            assert f in blk


def test_relative_return_equals_etf_minus_benchmark() -> None:
    # SPY: 100 -> 110 over 63 bars (one bar = first->last close gap of 1).
    # Use ETF XLK ending at 120 over 63 bars.
    dfs = _full_universe_dfs(spy_end=110.0, etf_returns={
        "XLK": 0.20, "XLF": 0.05, "XLE": 0.0, "XLI": 0.0,
        "XLY": 0.0, "XLP": 0.0, "XLV": 0.0, "XLB": 0.0,
        "XLU": 0.0, "XLRE": 0.0, "XLC": 0.0,
    })
    factory = _make_ticker_router(dfs)
    with patch("yfinance.Ticker", factory):
        snap = sector_etf.fetch_sector_strength()
    bench_3m = snap["benchmark_return_3m"]
    xlk_3m = snap["sectors"]["XLK"]["return_3m"]
    assert xlk_3m - bench_3m == pytest.approx(
        snap["sectors"]["XLK"]["relative_3m"], rel=1e-9
    )


def test_rank_sorts_by_relative_strength() -> None:
    # ETF returns chosen so XLK is best, XLU is worst relative to SPY.
    dfs = _full_universe_dfs(spy_end=110.0, etf_returns={
        "XLK": 0.30, "XLF": 0.20, "XLE": 0.15, "XLI": 0.10,
        "XLY": 0.05, "XLP": 0.00, "XLV": -0.05, "XLB": -0.10,
        "XLU": -0.20, "XLRE": 0.08, "XLC": 0.18,
    })
    factory = _make_ticker_router(dfs)
    with patch("yfinance.Ticker", factory):
        snap = sector_etf.fetch_sector_strength()

    # XLK must be rank 1 on both windows (best relative return).
    assert snap["sectors"]["XLK"]["rank_3m"] == 1
    # XLU must be the worst (last rank).
    n = len(snap["sectors"])
    assert snap["sectors"]["XLU"]["rank_3m"] == n


# ---------------------------------------------------------------------------
# Graceful degradation
# ---------------------------------------------------------------------------


def test_spy_missing_returns_empty_sectors() -> None:
    # SPY history empty; ETFs healthy.  Without a benchmark we can't
    # compute relative strength, so ``sectors`` is empty.
    dfs = _full_universe_dfs()
    dfs["SPY"] = pd.DataFrame(
        columns=["Open", "High", "Low", "Close", "Adj Close", "Volume"]
    )
    factory = _make_ticker_router(dfs)
    with patch("yfinance.Ticker", factory):
        snap = sector_etf.fetch_sector_strength()
    assert snap["benchmark_return_1m"] is None
    assert snap["benchmark_return_3m"] is None
    assert snap["sectors"] == {}


def test_single_etf_failure_drops_only_that_etf() -> None:
    dfs = _full_universe_dfs()
    dfs["XLE"] = None  # ``history`` raises for XLE.
    factory = _make_ticker_router(dfs)
    with patch("yfinance.Ticker", factory):
        snap = sector_etf.fetch_sector_strength()
    # 10 sectors survive; XLE is missing.
    assert "XLE" not in snap["sectors"]
    assert len(snap["sectors"]) == 10


def test_short_history_etf_dropped() -> None:
    # Healthy SPY + most ETFs, but XLB has too few closes for either window.
    dfs = _full_universe_dfs()
    dfs["XLB"] = _make_df([100.0, 101.0])  # only 2 closes
    factory = _make_ticker_router(dfs)
    with patch("yfinance.Ticker", factory):
        snap = sector_etf.fetch_sector_strength()
    assert "XLB" not in snap["sectors"]


# ---------------------------------------------------------------------------
# Cache behaviour
# ---------------------------------------------------------------------------


def test_snapshot_cached_between_calls() -> None:
    dfs = _full_universe_dfs()
    factory = _make_ticker_router(dfs)
    with patch("yfinance.Ticker", factory):
        a = sector_etf.fetch_sector_strength()
        b = sector_etf.fetch_sector_strength()
    assert a == b
    # First call needed SPY + 11 ETFs = 12 yfinance.Ticker constructions.
    # Second call hits the snapshot cache: no additional yfinance calls.
    assert factory.call_count == 12


def test_cache_dir_override_isolates_state(tmp_path: Path) -> None:
    dfs = _full_universe_dfs()
    factory = _make_ticker_router(dfs)
    isolated = tmp_path / "isolated"
    with patch("yfinance.Ticker", factory):
        snap = sector_etf.fetch_sector_strength(cache_dir=isolated)
    assert isinstance(snap, dict)
    assert any(isolated.rglob("*.json"))


# ---------------------------------------------------------------------------
# SECTOR_TO_ETF mapping + get_sector_relative_strength
# ---------------------------------------------------------------------------


def test_sector_to_etf_mapping_includes_aliases() -> None:
    # Spec-required aliases must be present.
    expected_pairs = [
        ("Technology", "XLK"),
        ("Information Technology", "XLK"),
        ("Financials", "XLF"),
        ("Financial Services", "XLF"),
        ("Energy", "XLE"),
        ("Industrials", "XLI"),
        ("Consumer Cyclical", "XLY"),
        ("Consumer Discretionary", "XLY"),
        ("Consumer Defensive", "XLP"),
        ("Consumer Staples", "XLP"),
        ("Healthcare", "XLV"),
        ("Basic Materials", "XLB"),
        ("Materials", "XLB"),
        ("Utilities", "XLU"),
        ("Real Estate", "XLRE"),
        ("Communication Services", "XLC"),
    ]
    for name, etf in expected_pairs:
        assert sector_etf.SECTOR_TO_ETF[name] == etf


def test_get_sector_relative_strength_resolves_canonical_name() -> None:
    dfs = _full_universe_dfs(spy_end=110.0, etf_returns={
        "XLK": 0.30, "XLF": 0.05, "XLE": 0.0, "XLI": 0.0,
        "XLY": 0.0, "XLP": 0.0, "XLV": 0.0, "XLB": 0.0,
        "XLU": 0.0, "XLRE": 0.0, "XLC": 0.0,
    })
    factory = _make_ticker_router(dfs)
    with patch("yfinance.Ticker", factory):
        snap = sector_etf.fetch_sector_strength()

    result = sector_etf.get_sector_relative_strength("Technology", snap)
    assert result is not None
    assert result["rank_1m"] == 1 or result["rank_1m"] is not None
    assert result["relative_3m"] == pytest.approx(
        snap["sectors"]["XLK"]["relative_3m"]
    )


def test_get_sector_relative_strength_resolves_alias() -> None:
    dfs = _full_universe_dfs()
    factory = _make_ticker_router(dfs)
    with patch("yfinance.Ticker", factory):
        snap = sector_etf.fetch_sector_strength()
    # All four pairs of aliases should resolve to the same ETF block.
    by_alias_pairs = [
        ("Information Technology", "Technology"),
        ("Financial Services", "Financials"),
        ("Consumer Discretionary", "Consumer Cyclical"),
        ("Consumer Staples", "Consumer Defensive"),
        ("Materials", "Basic Materials"),
    ]
    for alias, canonical in by_alias_pairs:
        a = sector_etf.get_sector_relative_strength(alias, snap)
        c = sector_etf.get_sector_relative_strength(canonical, snap)
        assert a == c, f"{alias!r} should match {canonical!r}"


def test_get_sector_relative_strength_case_insensitive() -> None:
    dfs = _full_universe_dfs()
    factory = _make_ticker_router(dfs)
    with patch("yfinance.Ticker", factory):
        snap = sector_etf.fetch_sector_strength()
    a = sector_etf.get_sector_relative_strength("technology", snap)
    b = sector_etf.get_sector_relative_strength("TECHNOLOGY", snap)
    c = sector_etf.get_sector_relative_strength("Technology", snap)
    assert a == b == c


def test_get_sector_relative_strength_unknown_sector_returns_none() -> None:
    dfs = _full_universe_dfs()
    factory = _make_ticker_router(dfs)
    with patch("yfinance.Ticker", factory):
        snap = sector_etf.fetch_sector_strength()
    assert sector_etf.get_sector_relative_strength("Crypto", snap) is None
    assert sector_etf.get_sector_relative_strength("", snap) is None
    assert sector_etf.get_sector_relative_strength(None, snap) is None  # type: ignore[arg-type]


def test_get_sector_relative_strength_etf_missing_from_snapshot() -> None:
    # XLU drops out of the snapshot (e.g. ETF history fetch failed).
    dfs = _full_universe_dfs()
    dfs["XLU"] = None
    factory = _make_ticker_router(dfs)
    with patch("yfinance.Ticker", factory):
        snap = sector_etf.fetch_sector_strength()
    assert "XLU" not in snap["sectors"]
    assert sector_etf.get_sector_relative_strength("Utilities", snap) is None


def test_get_sector_relative_strength_malformed_snapshot_returns_none() -> None:
    # Snapshot with no ``sectors`` key, or wrong type.
    assert sector_etf.get_sector_relative_strength("Technology", {}) is None
    assert sector_etf.get_sector_relative_strength(
        "Technology", {"sectors": "garbage"}  # type: ignore[arg-type]
    ) is None
    assert sector_etf.get_sector_relative_strength(
        "Technology", "not even a dict"  # type: ignore[arg-type]
    ) is None


# ---------------------------------------------------------------------------
# as_of support
# ---------------------------------------------------------------------------


def _ramp_full(
    *, n: int = 130, start_date: str = "2025-11-01",
    spy_path: Optional[List[float]] = None,
    etf_paths: Optional[Dict[str, List[float]]] = None,
) -> Dict[str, pd.DataFrame]:
    """Build a wide synthetic universe for as_of tests.

    n=130 business days gives plenty of room for a 63-bar lookback ending
    on any reasonable mid-window as_of.
    """
    spy = spy_path if spy_path is not None else _ramp(100.0, 110.0, n)
    if etf_paths is None:
        etf_paths = {etf: _ramp(100.0, 100.0 + i, n)
                     for i, etf in enumerate(sector_etf.SECTOR_ETFS.keys())}
    dfs: Dict[str, pd.DataFrame] = {"SPY": _make_df(spy, start=start_date)}
    for etf, path in etf_paths.items():
        dfs[etf] = _make_df(path, start=start_date)
    return dfs


def test_fetch_sector_strength_as_of_none_preserves_existing_behavior() -> None:
    dfs = _full_universe_dfs()
    factory = _make_ticker_router(dfs)
    with patch("yfinance.Ticker", factory):
        baseline = sector_etf.fetch_sector_strength()
        explicit_none = sector_etf.fetch_sector_strength(as_of=None)
    assert baseline == explicit_none
    # Both calls share the same snapshot cache entry -> 12 ticker constructions
    # total (SPY + 11 ETFs on the first call), zero on the second.
    assert factory.call_count == 12


def test_fetch_sector_strength_as_of_field_reflects_passed_date() -> None:
    # 130 business days starting 2025-11-01 covers an as_of of 2026-04-15.
    dfs = _ramp_full(n=130, start_date="2025-11-01")
    factory = _make_ticker_router(dfs)
    with patch("yfinance.Ticker", factory):
        snap = sector_etf.fetch_sector_strength(as_of="2026-04-15")
    assert snap["as_of"] == "2026-04-15"


def test_fetch_sector_strength_as_of_measures_1m_to_target_date() -> None:
    # SPY ramps so the 21-bar return ending at as_of is exactly known.
    # Use a SPY ramp where close = 100 + i so 21-bar return = 21 / spot_at_t-21.
    n = 130
    spy_path = [100.0 + i for i in range(n)]
    # Make XLK ramp twice as fast (so its 1m return outpaces SPY's).
    xlk_path = [100.0 + 2 * i for i in range(n)]
    other_paths = {etf: [100.0 + 0.5 * i for i in range(n)]
                   for etf in sector_etf.SECTOR_ETFS.keys() if etf != "XLK"}
    other_paths["XLK"] = xlk_path

    dfs = _ramp_full(n=n, start_date="2025-11-01",
                     spy_path=spy_path, etf_paths=other_paths)
    factory = _make_ticker_router(dfs)
    with patch("yfinance.Ticker", factory):
        snap = sector_etf.fetch_sector_strength(as_of="2026-04-15")

    # Locate the bar at 2026-04-15 in the bdate_range.
    idx = pd.bdate_range(start="2025-11-01", periods=n)
    # 2026-04-15 is a business day (Wednesday).
    target_pos = list(idx).index(pd.Timestamp("2026-04-15"))
    spy_t = spy_path[target_pos]
    spy_t_minus_21 = spy_path[target_pos - 21]
    expected_spy_1m = spy_t / spy_t_minus_21 - 1.0

    assert snap["benchmark_return_1m"] == pytest.approx(expected_spy_1m, rel=1e-9)
    # XLK should also have its 1m return measured to 2026-04-15.
    xlk_block = snap["sectors"]["XLK"]
    xlk_t = xlk_path[target_pos]
    xlk_t_minus_21 = xlk_path[target_pos - 21]
    expected_xlk_1m = xlk_t / xlk_t_minus_21 - 1.0
    assert xlk_block["return_1m"] == pytest.approx(expected_xlk_1m, rel=1e-9)


def test_fetch_sector_strength_as_of_cache_key_isolated(tmp_path: Path) -> None:
    dfs = _ramp_full(n=130, start_date="2025-11-01")
    factory = _make_ticker_router(dfs)
    isolated = tmp_path / "isolated"
    with patch("yfinance.Ticker", factory):
        a = sector_etf.fetch_sector_strength(
            cache_dir=isolated, as_of="2026-04-15"
        )
        b = sector_etf.fetch_sector_strength(
            cache_dir=isolated, as_of="2026-02-15"
        )
        c = sector_etf.fetch_sector_strength(cache_dir=isolated)  # today
    assert a["as_of"] == "2026-04-15"
    assert b["as_of"] == "2026-02-15"
    # Three distinct snapshots cached separately.
    assert a != b
    # Three calls each needed SPY + 11 ETFs unless yahoo cache served some
    # — yahoo cache key includes as_of, so each as_of triggers fresh fetches.
    # We don't assert exact counts because the today-path may reuse cached
    # rows for the as_of paths (different keys, different files).


def test_fetch_sector_strength_as_of_before_data_has_empty_sectors() -> None:
    # If as_of predates the dataset, all per-ETF closes slice to empty -> no
    # benchmark return -> empty sectors map (sector_etf's graceful path).
    dfs = _ramp_full(n=130, start_date="2025-11-01")
    factory = _make_ticker_router(dfs)
    with patch("yfinance.Ticker", factory):
        snap = sector_etf.fetch_sector_strength(as_of="2020-01-01")
    assert snap["as_of"] == "2020-01-01"
    assert snap["benchmark_return_1m"] is None
    assert snap["benchmark_return_3m"] is None
    assert snap["sectors"] == {}


def test_fetch_sector_strength_as_of_weekend_falls_back_to_prior_trading_day() -> None:
    # 2026-04-18 is a Saturday; the snapshot's `as_of` field reflects what
    # the caller passed (we don't rewrite it to a trading day), but the
    # underlying yahoo slice picks the last trading bar <= 2026-04-18.
    dfs = _ramp_full(n=130, start_date="2025-11-01")
    factory = _make_ticker_router(dfs)
    with patch("yfinance.Ticker", factory):
        snap = sector_etf.fetch_sector_strength(as_of="2026-04-18")
    assert snap["as_of"] == "2026-04-18"
    # If there's data, sectors map should be populated.
    assert snap["benchmark_return_1m"] is not None
    assert len(snap["sectors"]) > 0


def test_fetch_sector_strength_as_of_invalid_falls_back_to_today() -> None:
    dfs = _full_universe_dfs()
    factory = _make_ticker_router(dfs)
    with patch("yfinance.Ticker", factory):
        baseline = sector_etf.fetch_sector_strength()
        garbage = sector_etf.fetch_sector_strength(as_of="not-a-date")
    # Invalid as_of degrades to today and hits the same cache key.
    assert baseline == garbage


def test_fetch_sector_strength_as_of_distinct_cache_from_today(tmp_path: Path) -> None:
    # The historical and current snapshots must NOT collide on disk.
    dfs = _ramp_full(n=130, start_date="2025-11-01")
    factory = _make_ticker_router(dfs)
    isolated = tmp_path / "iso"
    with patch("yfinance.Ticker", factory):
        snap_hist = sector_etf.fetch_sector_strength(
            cache_dir=isolated, as_of="2026-04-15"
        )
    # The snapshot cache should contain a file whose name reflects the as_of.
    files = list((isolated / "sector_etf").glob("*.json"))
    assert any("2026-04-15" in p.name for p in files)
    assert snap_hist["as_of"] == "2026-04-15"
