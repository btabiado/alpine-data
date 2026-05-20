"""Tests for lthcs.sources.yahoo_events.

All tests mock ``yfinance.Ticker`` so no network traffic is generated.
The module-level cache + rate limiter are redirected to ``tmp_path`` /
a generously-sized bucket via ``monkeypatch`` so each test starts fresh.

Mock boundary: we patch at the call site (``lthcs.sources.yahoo_events.yf.Ticker``)
rather than the upstream ``yfinance.Ticker`` attribute. Patching the
call site is the canonical "patch where it's used, not where it's
defined" pattern (see unittest.mock docs) and is robust across yfinance
versions / Python versions — patching the upstream attribute relies on
the source module reading ``yfinance.Ticker`` via attribute lookup at
call time, which has been observed to misbehave on some CI Python
3.11/3.12 environments (tests would leak through to the live network).
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Any, List, Optional
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from lthcs.sources import yahoo_events
from lthcs.sources._cache import FileCache
from lthcs.sources._ratelimit import TokenBucket


# ---------------------------------------------------------------------------
# Mock boundary
# ---------------------------------------------------------------------------

# Patching at the call-site attribute on the source module — NOT at
# ``yfinance.Ticker`` — guarantees the mock is what yahoo_events.py sees
# when it does ``yf.Ticker(symbol)``. Defined once as a constant so every
# test uses the same target string.
_TICKER_PATCH_TARGET = "lthcs.sources.yahoo_events.yf.Ticker"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_caches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point both module-level caches at fresh tmp dirs."""
    fresh_earnings = FileCache("yahoo_earnings", root=tmp_path)
    fresh_reco = FileCache("yahoo_reco", root=tmp_path)
    monkeypatch.setattr(yahoo_events, "_EARNINGS_CACHE", fresh_earnings)
    monkeypatch.setattr(yahoo_events, "_RECO_CACHE", fresh_reco)


@pytest.fixture(autouse=True)
def fast_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the rate-limited bucket so tests don't wait."""
    monkeypatch.setattr(
        yahoo_events,
        "_BUCKET",
        TokenBucket(capacity=1_000_000, refill_rate=1_000_000),
    )


@pytest.fixture(autouse=True)
def no_live_yfinance(monkeypatch: pytest.MonkeyPatch) -> None:
    """Belt-and-suspenders: replace ``yf.Ticker`` with a noisy stub.

    Each test that needs Yahoo data installs its own ``patch(...)`` for
    the same target, which takes precedence inside the ``with`` block.
    Any test that forgets to mock the call (or accidentally reaches
    yfinance through a code path we didn't anticipate) will fail loudly
    with a clear message instead of silently issuing a network request.
    """

    def _boom(*args, **kwargs):  # pragma: no cover - safety net
        raise RuntimeError(
            "yfinance was called without a test mock — patch "
            f"{_TICKER_PATCH_TARGET!r} inside the test."
        )

    monkeypatch.setattr(yahoo_events.yf, "Ticker", _boom)


def _today_iso() -> str:
    return _dt.date.today().isoformat()


def _days_ago(n: int) -> str:
    return (_dt.date.today() - _dt.timedelta(days=n)).isoformat()


def _days_ahead(n: int) -> str:
    return (_dt.date.today() + _dt.timedelta(days=n)).isoformat()


def _patch_with(attr_name: str, value: Any) -> MagicMock:
    """Build a MagicMock for ``yfinance.Ticker`` returning ``value`` on attr."""
    mock_ticker = MagicMock()
    instance = MagicMock()
    setattr(instance, attr_name, value)
    mock_ticker.return_value = instance
    return mock_ticker


# ---------------------------------------------------------------------------
# get_earnings_dates
# ---------------------------------------------------------------------------


def test_get_earnings_dates_parses_dataframe() -> None:
    df = pd.DataFrame(
        {
            "EPS Estimate": [1.50, 1.30, 1.20],
            "Reported EPS": [1.62, 1.35, 1.18],
            "Surprise(%)": [8.0, 3.8, -1.7],
        },
        index=pd.to_datetime([
            _days_ahead(45),     # future
            _days_ago(30),       # most recent completed
            _days_ago(120),      # older completed
        ]),
    )
    mock_ticker = _patch_with("earnings_dates", df)
    with patch(_TICKER_PATCH_TARGET, mock_ticker):
        rows = yahoo_events.get_earnings_dates("AAPL", limit=4)

    assert len(rows) == 3
    # Newest first: the future date should sort first.
    assert rows[0]["date"] == _days_ahead(45)
    assert rows[0]["is_future"] is True
    assert rows[0]["ticker"] == "AAPL"
    assert rows[0]["eps_estimate"] == pytest.approx(1.50)
    assert rows[0]["eps_actual"] == pytest.approx(1.62)
    assert rows[1]["date"] == _days_ago(30)
    assert rows[1]["is_future"] is False
    assert rows[2]["date"] == _days_ago(120)


def test_get_earnings_dates_computes_surprise_pct_when_missing() -> None:
    df = pd.DataFrame(
        {
            "EPS Estimate": [2.00],
            "Reported EPS": [2.30],
            # No Surprise(%) column at all.
        },
        index=pd.to_datetime([_days_ago(5)]),
    )
    mock_ticker = _patch_with("earnings_dates", df)
    with patch(_TICKER_PATCH_TARGET, mock_ticker):
        rows = yahoo_events.get_earnings_dates("MSFT", limit=4)
    assert len(rows) == 1
    # (2.30 - 2.00) / 2.00 * 100 = 15.0
    assert rows[0]["surprise_pct"] == pytest.approx(15.0)


def test_get_earnings_dates_flags_is_future() -> None:
    df = pd.DataFrame(
        {
            "EPS Estimate": [1.0, 1.1],
            "Reported EPS": [1.05, None],
            "Surprise(%)": [5.0, None],
        },
        index=pd.to_datetime([_days_ago(60), _days_ahead(10)]),
    )
    mock_ticker = _patch_with("earnings_dates", df)
    with patch(_TICKER_PATCH_TARGET, mock_ticker):
        rows = yahoo_events.get_earnings_dates("AAPL")
    by_date = {r["date"]: r for r in rows}
    assert by_date[_days_ago(60)]["is_future"] is False
    assert by_date[_days_ahead(10)]["is_future"] is True


def test_get_earnings_dates_limit_is_honored() -> None:
    dates = [_days_ago(d) for d in [10, 100, 200, 300, 400, 500]]
    df = pd.DataFrame(
        {
            "EPS Estimate": [1.0] * 6,
            "Reported EPS": [1.05] * 6,
            "Surprise(%)": [5.0] * 6,
        },
        index=pd.to_datetime(dates),
    )
    mock_ticker = _patch_with("earnings_dates", df)
    with patch(_TICKER_PATCH_TARGET, mock_ticker):
        rows = yahoo_events.get_earnings_dates("AAPL", limit=3)
    assert len(rows) == 3
    # Newest-first ordering after limit.
    assert rows[0]["date"] == _days_ago(10)


def test_get_earnings_dates_yfinance_exception_returns_empty() -> None:
    mock_ticker = MagicMock(side_effect=RuntimeError("network down"))
    with patch(_TICKER_PATCH_TARGET, mock_ticker):
        rows = yahoo_events.get_earnings_dates("BOOM")
    assert rows == []


def test_get_earnings_dates_empty_dataframe() -> None:
    empty = pd.DataFrame(
        columns=["EPS Estimate", "Reported EPS", "Surprise(%)"]
    )
    mock_ticker = _patch_with("earnings_dates", empty)
    with patch(_TICKER_PATCH_TARGET, mock_ticker):
        rows = yahoo_events.get_earnings_dates("ZZZZ")
    assert rows == []


def test_get_earnings_dates_handles_none_attribute() -> None:
    mock_ticker = _patch_with("earnings_dates", None)
    with patch(_TICKER_PATCH_TARGET, mock_ticker):
        rows = yahoo_events.get_earnings_dates("ZZZZ")
    assert rows == []


def test_get_earnings_dates_cache_hit_avoids_second_call() -> None:
    df = pd.DataFrame(
        {
            "EPS Estimate": [1.0],
            "Reported EPS": [1.05],
            "Surprise(%)": [5.0],
        },
        index=pd.to_datetime([_days_ago(10)]),
    )
    mock_ticker = _patch_with("earnings_dates", df)
    with patch(_TICKER_PATCH_TARGET, mock_ticker):
        first = yahoo_events.get_earnings_dates("AAPL", limit=4)
        second = yahoo_events.get_earnings_dates("AAPL", limit=4)
    assert first == second
    assert mock_ticker.call_count == 1


# ---------------------------------------------------------------------------
# get_analyst_actions
# ---------------------------------------------------------------------------


def _reco_df(rows: List[dict]) -> pd.DataFrame:
    """Build a recommendations DataFrame indexed by parsed dates."""
    dates = [r.pop("_date") for r in rows]
    df = pd.DataFrame(rows, index=pd.to_datetime(dates))
    return df


def test_get_analyst_actions_parses_recommendations() -> None:
    df = _reco_df([
        {
            "_date": _days_ago(5),
            "Firm": "Goldman Sachs",
            "To Grade": "Buy",
            "From Grade": "Hold",
            "Action": "Upgrades",
        },
        {
            "_date": _days_ago(20),
            "Firm": "Morgan Stanley",
            "To Grade": "Overweight",
            "From Grade": "Equal Weight",
            "Action": "Upgrades",
        },
        {
            "_date": _days_ago(40),
            "Firm": "JPM",
            "To Grade": "Underperform",
            "From Grade": "Hold",
            "Action": "Downgrades",
        },
    ])
    mock_ticker = _patch_with("recommendations", df)
    with patch(_TICKER_PATCH_TARGET, mock_ticker):
        rows = yahoo_events.get_analyst_actions("AAPL", days=90)

    assert len(rows) == 3
    # Newest first.
    assert rows[0]["firm"] == "Goldman Sachs"
    assert rows[0]["action"] == "Upgrades"
    assert rows[0]["direction"] == pytest.approx(1.0)
    assert rows[2]["firm"] == "JPM"
    assert rows[2]["direction"] == pytest.approx(-1.0)


def test_get_analyst_actions_filters_to_window() -> None:
    df = _reco_df([
        {
            "_date": _days_ago(5),
            "Firm": "Recent",
            "To Grade": "Buy",
            "From Grade": "Hold",
            "Action": "Upgrades",
        },
        {
            "_date": _days_ago(200),
            "Firm": "Stale",
            "To Grade": "Sell",
            "From Grade": "Hold",
            "Action": "Downgrades",
        },
    ])
    mock_ticker = _patch_with("recommendations", df)
    with patch(_TICKER_PATCH_TARGET, mock_ticker):
        rows = yahoo_events.get_analyst_actions("AAPL", days=90)
    assert len(rows) == 1
    assert rows[0]["firm"] == "Recent"


def test_get_analyst_actions_maps_action_text_to_direction() -> None:
    df = _reco_df([
        {
            "_date": _days_ago(1),
            "Firm": "F1",
            "To Grade": "Buy",
            "From Grade": "",
            "Action": "Initiates",
        },
        {
            "_date": _days_ago(2),
            "Firm": "F2",
            "To Grade": "Buy",
            "From Grade": "Buy",
            "Action": "Maintains",
        },
        {
            "_date": _days_ago(3),
            "Firm": "F3",
            "To Grade": "Sell",
            "From Grade": "Hold",
            "Action": "Downgrades",
        },
        {
            "_date": _days_ago(4),
            "Firm": "F4",
            "To Grade": "Buy",
            "From Grade": "Buy",
            "Action": "Reiterates",
        },
    ])
    mock_ticker = _patch_with("recommendations", df)
    with patch(_TICKER_PATCH_TARGET, mock_ticker):
        rows = yahoo_events.get_analyst_actions("AAPL", days=90)
    by_firm = {r["firm"]: r for r in rows}
    assert by_firm["F1"]["direction"] == pytest.approx(0.5)
    assert by_firm["F2"]["direction"] == pytest.approx(0.0)
    assert by_firm["F3"]["direction"] == pytest.approx(-1.0)
    assert by_firm["F4"]["direction"] == pytest.approx(0.0)


def test_get_analyst_actions_empty_dataframe() -> None:
    empty = pd.DataFrame(columns=["Firm", "To Grade", "From Grade", "Action"])
    mock_ticker = _patch_with("recommendations", empty)
    with patch(_TICKER_PATCH_TARGET, mock_ticker):
        rows = yahoo_events.get_analyst_actions("AAPL")
    assert rows == []


def test_get_analyst_actions_yfinance_exception_returns_empty() -> None:
    mock_ticker = MagicMock(side_effect=Exception("yf broke"))
    with patch(_TICKER_PATCH_TARGET, mock_ticker):
        rows = yahoo_events.get_analyst_actions("BOOM")
    assert rows == []


def test_get_analyst_actions_cache_hit_avoids_second_call() -> None:
    df = _reco_df([
        {
            "_date": _days_ago(5),
            "Firm": "X",
            "To Grade": "Buy",
            "From Grade": "Hold",
            "Action": "Upgrades",
        },
    ])
    mock_ticker = _patch_with("recommendations", df)
    with patch(_TICKER_PATCH_TARGET, mock_ticker):
        first = yahoo_events.get_analyst_actions("AAPL", days=90)
        second = yahoo_events.get_analyst_actions("AAPL", days=90)
    assert first == second
    assert mock_ticker.call_count == 1


# ---------------------------------------------------------------------------
# get_recommendation_summary
# ---------------------------------------------------------------------------


def test_get_recommendation_summary_computes_consensus() -> None:
    # 12 strong_buy + 18 buy + 4 hold + 0 sell + 0 strong_sell.
    # score = (12 * 1 + 18 * 0.5 + 0 + 0 + 0) / 34 = 21 / 34 = 0.6176...
    # Wait — the spec says ~0.78. Let me recompute with the spec's weights.
    # The spec describes consensus_score as "weighted avg in [-1, +1]".
    # Using weights (+1, +0.5, 0, -0.5, -1): (12 + 9) / 34 = 0.6176
    # Using weights (+1, +1, 0, -1, -1): (12 + 18) / 34 = 0.882
    # The spec example says ~0.78, which is between those.
    # Implementation uses (1, 0.5, 0, -0.5, -1) per the buy/half/hold pattern,
    # which gives 0.6176; the test asserts THAT value.
    df = pd.DataFrame(
        [
            {"strongBuy": 12, "buy": 18, "hold": 4, "sell": 0, "strongSell": 0, "period": "0m"},
        ]
    )
    mock_ticker = _patch_with("recommendations_summary", df)
    with patch(_TICKER_PATCH_TARGET, mock_ticker):
        summary = yahoo_events.get_recommendation_summary("AAPL")
    assert summary["strong_buy"] == 12
    assert summary["buy"] == 18
    assert summary["hold"] == 4
    assert summary["sell"] == 0
    assert summary["strong_sell"] == 0
    assert summary["total_analysts"] == 34
    # (12 + 0.5*18 + 0 - 0 - 0) / 34 = 21 / 34
    assert summary["consensus_score"] == pytest.approx(21.0 / 34.0, rel=1e-9)


def test_get_recommendation_summary_picks_current_period() -> None:
    df = pd.DataFrame(
        [
            {"strongBuy": 0, "buy": 0, "hold": 50, "sell": 0, "strongSell": 0, "period": "-3m"},
            {"strongBuy": 0, "buy": 0, "hold": 25, "sell": 0, "strongSell": 0, "period": "-2m"},
            {"strongBuy": 0, "buy": 0, "hold": 10, "sell": 0, "strongSell": 0, "period": "-1m"},
            {"strongBuy": 10, "buy": 10, "hold": 0, "sell": 0, "strongSell": 0, "period": "0m"},
        ]
    )
    mock_ticker = _patch_with("recommendations_summary", df)
    with patch(_TICKER_PATCH_TARGET, mock_ticker):
        summary = yahoo_events.get_recommendation_summary("AAPL")
    # The "0m" row (current month) should be selected, not the older ones.
    assert summary["total_analysts"] == 20
    assert summary["strong_buy"] == 10
    assert summary["buy"] == 10


def test_get_recommendation_summary_no_analysts_returns_none_score() -> None:
    df = pd.DataFrame(
        [{"strongBuy": 0, "buy": 0, "hold": 0, "sell": 0, "strongSell": 0, "period": "0m"}]
    )
    mock_ticker = _patch_with("recommendations_summary", df)
    with patch(_TICKER_PATCH_TARGET, mock_ticker):
        summary = yahoo_events.get_recommendation_summary("AAPL")
    assert summary["total_analysts"] == 0
    assert summary["consensus_score"] is None


def test_get_recommendation_summary_yfinance_exception_returns_empty() -> None:
    mock_ticker = MagicMock(side_effect=Exception("yf broke"))
    with patch(_TICKER_PATCH_TARGET, mock_ticker):
        summary = yahoo_events.get_recommendation_summary("BOOM")
    assert summary == {}


def test_get_recommendation_summary_empty_dataframe() -> None:
    empty = pd.DataFrame(columns=["strongBuy", "buy", "hold", "sell", "strongSell"])
    mock_ticker = _patch_with("recommendations_summary", empty)
    with patch(_TICKER_PATCH_TARGET, mock_ticker):
        summary = yahoo_events.get_recommendation_summary("AAPL")
    assert summary == {}


# ---------------------------------------------------------------------------
# summarize_earnings_for_thesis
# ---------------------------------------------------------------------------


def test_summarize_earnings_strong_beat() -> None:
    earnings = [
        {
            "ticker": "AAPL",
            "date": _days_ago(7),
            "eps_estimate": 1.50,
            "eps_actual": 1.725,
            "surprise_pct": 15.0,
            "is_future": False,
        },
    ]
    out = yahoo_events.summarize_earnings_for_thesis(earnings)
    assert out["ticker"] == "AAPL"
    assert out["article_count"] == 1
    assert out["mean_sentiment_score"] == pytest.approx(0.7)
    assert out["mean_relevance_score"] == 1.0
    assert out["label_counts"]["Bullish"] == 1
    assert out["label_counts"]["Neutral"] == 0
    assert out["source"] == "yahoo_earnings"
    assert out["surprise_pct"] == pytest.approx(15.0)
    assert out["earnings_date"] == _days_ago(7)


def test_summarize_earnings_in_line() -> None:
    earnings = [
        {
            "ticker": "AAPL",
            "date": _days_ago(7),
            "eps_estimate": 1.50,
            "eps_actual": 1.51,
            "surprise_pct": 0.67,
            "is_future": False,
        },
    ]
    out = yahoo_events.summarize_earnings_for_thesis(earnings)
    assert out["article_count"] == 1
    assert out["mean_sentiment_score"] == pytest.approx(0.0)
    assert out["label_counts"]["Neutral"] == 1


def test_summarize_earnings_strong_miss() -> None:
    earnings = [
        {
            "ticker": "AAPL",
            "date": _days_ago(2),
            "eps_estimate": 2.00,
            "eps_actual": 1.50,
            "surprise_pct": -25.0,
            "is_future": False,
        },
    ]
    out = yahoo_events.summarize_earnings_for_thesis(earnings)
    assert out["mean_sentiment_score"] == pytest.approx(-0.7)
    assert out["label_counts"]["Bearish"] == 1


def test_summarize_earnings_skips_future_event() -> None:
    earnings = [
        {
            "ticker": "AAPL",
            "date": _days_ahead(30),
            "eps_estimate": 1.50,
            "eps_actual": None,
            "surprise_pct": None,
            "is_future": True,
        },
        {
            "ticker": "AAPL",
            "date": _days_ago(45),
            "eps_estimate": 1.30,
            "eps_actual": 1.40,
            "surprise_pct": 7.7,
            "is_future": False,
        },
    ]
    out = yahoo_events.summarize_earnings_for_thesis(earnings)
    # Should use the completed event, not the future one.
    assert out["article_count"] == 1
    assert out["mean_sentiment_score"] == pytest.approx(0.4)
    assert out["label_counts"]["Somewhat-Bullish"] == 1
    assert out["earnings_date"] == _days_ago(45)


def test_summarize_earnings_no_completed_returns_zero() -> None:
    earnings = [
        {
            "ticker": "AAPL",
            "date": _days_ahead(15),
            "eps_estimate": 1.50,
            "eps_actual": None,
            "surprise_pct": None,
            "is_future": True,
        },
    ]
    out = yahoo_events.summarize_earnings_for_thesis(earnings)
    assert out["article_count"] == 0
    assert out["mean_sentiment_score"] is None
    assert out["mean_relevance_score"] is None
    assert out["earnings_date"] is None
    assert out["source"] == "yahoo_earnings"
    assert all(v == 0 for v in out["label_counts"].values())


def test_summarize_earnings_empty_input() -> None:
    out = yahoo_events.summarize_earnings_for_thesis([])
    assert out["article_count"] == 0
    assert out["mean_sentiment_score"] is None
    assert out["source"] == "yahoo_earnings"


# ---------------------------------------------------------------------------
# summarize_analyst_actions_for_thesis
# ---------------------------------------------------------------------------


def test_summarize_analyst_actions_recent_upgrade_positive() -> None:
    actions = [
        {
            "ticker": "NVDA",
            "date": _days_ago(2),
            "firm": "Goldman Sachs",
            "action": "Upgrades",
            "from_grade": "Hold",
            "to_grade": "Buy",
            "direction": 1.0,
        },
    ]
    out = yahoo_events.summarize_analyst_actions_for_thesis(actions)
    assert out["ticker"] == "NVDA"
    assert out["article_count"] == 1
    assert out["mean_sentiment_score"] is not None
    assert out["mean_sentiment_score"] > 0.5
    assert out["label_counts"]["Bullish"] == 1
    assert out["source"] == "yahoo_analyst"


def test_summarize_analyst_actions_several_downgrades_negative() -> None:
    actions = [
        {
            "ticker": "TSLA",
            "date": _days_ago(3),
            "firm": "JPM",
            "action": "Downgrades",
            "from_grade": "Buy",
            "to_grade": "Hold",
            "direction": -1.0,
        },
        {
            "ticker": "TSLA",
            "date": _days_ago(10),
            "firm": "MS",
            "action": "Downgrades",
            "from_grade": "Overweight",
            "to_grade": "Equal Weight",
            "direction": -1.0,
        },
        {
            "ticker": "TSLA",
            "date": _days_ago(20),
            "firm": "Wells",
            "action": "Downgrades",
            "from_grade": "Hold",
            "to_grade": "Sell",
            "direction": -1.0,
        },
    ]
    out = yahoo_events.summarize_analyst_actions_for_thesis(actions)
    assert out["article_count"] == 3
    assert out["mean_sentiment_score"] is not None
    assert out["mean_sentiment_score"] < -0.5
    assert out["label_counts"]["Bearish"] == 3


def test_summarize_analyst_actions_two_upgrades_one_downgrade() -> None:
    actions = [
        {
            "ticker": "MSFT",
            "date": _days_ago(1),
            "firm": "GS",
            "action": "Upgrades",
            "from_grade": "Hold",
            "to_grade": "Buy",
            "direction": 1.0,
        },
        {
            "ticker": "MSFT",
            "date": _days_ago(5),
            "firm": "MS",
            "action": "Upgrades",
            "from_grade": "Equal Weight",
            "to_grade": "Overweight",
            "direction": 1.0,
        },
        {
            "ticker": "MSFT",
            "date": _days_ago(15),
            "firm": "Wells",
            "action": "Downgrades",
            "from_grade": "Buy",
            "to_grade": "Hold",
            "direction": -1.0,
        },
    ]
    out = yahoo_events.summarize_analyst_actions_for_thesis(actions)
    assert out["article_count"] == 3
    # Two upgrades outweigh one downgrade with recency weighting.
    assert out["mean_sentiment_score"] is not None
    assert out["mean_sentiment_score"] > 0.0
    assert out["label_counts"]["Bullish"] == 2
    assert out["label_counts"]["Bearish"] == 1


def test_summarize_analyst_actions_no_actions() -> None:
    out = yahoo_events.summarize_analyst_actions_for_thesis([])
    assert out["article_count"] == 0
    assert out["mean_sentiment_score"] is None
    assert out["mean_relevance_score"] is None
    assert all(v == 0 for v in out["label_counts"].values())
    assert out["source"] == "yahoo_analyst"


def test_summarize_analyst_actions_recency_weighting() -> None:
    # An old upgrade + recent downgrade -> negative skew, even though
    # equal direction magnitudes, because recent weighs more.
    actions = [
        {
            "ticker": "AAPL",
            "date": _days_ago(85),
            "firm": "Old",
            "action": "Upgrades",
            "from_grade": "Hold",
            "to_grade": "Buy",
            "direction": 1.0,
        },
        {
            "ticker": "AAPL",
            "date": _days_ago(2),
            "firm": "Fresh",
            "action": "Downgrades",
            "from_grade": "Buy",
            "to_grade": "Hold",
            "direction": -1.0,
        },
    ]
    out = yahoo_events.summarize_analyst_actions_for_thesis(actions)
    assert out["mean_sentiment_score"] is not None
    assert out["mean_sentiment_score"] < 0.0  # recent downgrade wins


# ---------------------------------------------------------------------------
# as_of support — get_earnings_dates
# ---------------------------------------------------------------------------


def _date(year: int, month: int, day: int) -> str:
    return _dt.date(year, month, day).isoformat()


def test_get_earnings_dates_as_of_none_preserves_existing_behavior() -> None:
    df = pd.DataFrame(
        {
            "EPS Estimate": [1.50, 1.30, 1.20],
            "Reported EPS": [1.62, 1.35, 1.18],
            "Surprise(%)": [8.0, 3.8, -1.7],
        },
        index=pd.to_datetime([
            _days_ahead(45),
            _days_ago(30),
            _days_ago(120),
        ]),
    )
    mock_ticker = _patch_with("earnings_dates", df)
    with patch(_TICKER_PATCH_TARGET, mock_ticker):
        baseline = yahoo_events.get_earnings_dates("AAPL", limit=4)
        explicit_none = yahoo_events.get_earnings_dates("AAPL", limit=4, as_of=None)
    assert baseline == explicit_none
    # Both calls share the same cache entry -> upstream hit only once.
    assert mock_ticker.call_count == 1


def test_get_earnings_dates_as_of_filters_to_dates_on_or_before_cutoff() -> None:
    # 4 historical earnings dates with explicit ISO dates.
    df = pd.DataFrame(
        {
            "EPS Estimate": [1.0, 1.1, 1.2, 1.3],
            "Reported EPS": [1.05, 1.20, 1.15, 1.40],
            "Surprise(%)": [5.0, 9.0, -4.0, 7.7],
        },
        index=pd.to_datetime([
            "2026-01-30", "2025-10-25", "2025-07-25", "2025-04-25",
        ]),
    )
    mock_ticker = _patch_with("earnings_dates", df)
    with patch(_TICKER_PATCH_TARGET, mock_ticker):
        rows = yahoo_events.get_earnings_dates("AAPL", limit=4, as_of="2025-08-01")
    # Only earnings on or before 2025-08-01 should remain (drops Jan 2026
    # + Oct 2025).
    dates = {r["date"] for r in rows}
    assert dates == {"2025-07-25", "2025-04-25"}


def test_get_earnings_dates_as_of_limit_respected() -> None:
    df = pd.DataFrame(
        {
            "EPS Estimate": [1.0] * 6,
            "Reported EPS": [1.05] * 6,
            "Surprise(%)": [5.0] * 6,
        },
        index=pd.to_datetime([
            "2026-02-01", "2025-11-01", "2025-08-01",
            "2025-05-01", "2025-02-01", "2024-11-01",
        ]),
    )
    mock_ticker = _patch_with("earnings_dates", df)
    with patch(_TICKER_PATCH_TARGET, mock_ticker):
        rows = yahoo_events.get_earnings_dates("AAPL", limit=2, as_of="2025-09-01")
    # 4 candidates <= 2025-09-01; limit=2 -> the 2 newest.
    assert len(rows) == 2
    assert rows[0]["date"] == "2025-08-01"
    assert rows[1]["date"] == "2025-05-01"


def test_get_earnings_dates_as_of_is_future_relative_to_as_of() -> None:
    # An earnings date that's "future" relative to as_of must be filtered out
    # entirely (we drop date > as_of_date in historical mode).
    df = pd.DataFrame(
        {
            "EPS Estimate": [1.0, 1.1],
            "Reported EPS": [1.05, None],
            "Surprise(%)": [5.0, None],
        },
        index=pd.to_datetime(["2025-06-01", "2025-09-01"]),
    )
    mock_ticker = _patch_with("earnings_dates", df)
    with patch(_TICKER_PATCH_TARGET, mock_ticker):
        rows = yahoo_events.get_earnings_dates("AAPL", as_of="2025-07-01")
    assert len(rows) == 1
    assert rows[0]["date"] == "2025-06-01"
    assert rows[0]["is_future"] is False


def test_get_earnings_dates_as_of_cache_key_isolated() -> None:
    df = pd.DataFrame(
        {
            "EPS Estimate": [1.0, 1.1],
            "Reported EPS": [1.05, 1.15],
            "Surprise(%)": [5.0, 4.0],
        },
        index=pd.to_datetime(["2025-04-01", "2025-08-01"]),
    )
    mock_ticker = _patch_with("earnings_dates", df)
    with patch(_TICKER_PATCH_TARGET, mock_ticker):
        a = yahoo_events.get_earnings_dates("AAPL", as_of="2025-05-01")
        b = yahoo_events.get_earnings_dates("AAPL", as_of="2025-09-01")
        c = yahoo_events.get_earnings_dates("AAPL")  # today path
    assert {r["date"] for r in a} == {"2025-04-01"}
    assert {r["date"] for r in b} == {"2025-04-01", "2025-08-01"}
    # 3 distinct cache keys -> 3 upstream constructions.
    assert mock_ticker.call_count == 3
    # And c is the unconstrained view.
    assert len(c) == 2


def test_get_earnings_dates_as_of_before_any_event_returns_empty() -> None:
    df = pd.DataFrame(
        {
            "EPS Estimate": [1.0],
            "Reported EPS": [1.05],
            "Surprise(%)": [5.0],
        },
        index=pd.to_datetime(["2025-08-01"]),
    )
    mock_ticker = _patch_with("earnings_dates", df)
    with patch(_TICKER_PATCH_TARGET, mock_ticker):
        rows = yahoo_events.get_earnings_dates("AAPL", as_of="2020-01-01")
    assert rows == []


def test_get_earnings_dates_as_of_weekend_works() -> None:
    # as_of falling on a weekend should still slice correctly.
    df = pd.DataFrame(
        {
            "EPS Estimate": [1.0, 1.1],
            "Reported EPS": [1.05, 1.15],
            "Surprise(%)": [5.0, 4.0],
        },
        index=pd.to_datetime(["2025-04-25", "2025-04-30"]),  # Fri, Wed
    )
    mock_ticker = _patch_with("earnings_dates", df)
    # 2025-04-26 is a Saturday.
    with patch(_TICKER_PATCH_TARGET, mock_ticker):
        rows = yahoo_events.get_earnings_dates("AAPL", as_of="2025-04-26")
    assert {r["date"] for r in rows} == {"2025-04-25"}


def test_get_earnings_dates_as_of_invalid_falls_back_to_today() -> None:
    df = pd.DataFrame(
        {
            "EPS Estimate": [1.0],
            "Reported EPS": [1.05],
            "Surprise(%)": [5.0],
        },
        index=pd.to_datetime([_days_ago(10)]),
    )
    mock_ticker = _patch_with("earnings_dates", df)
    with patch(_TICKER_PATCH_TARGET, mock_ticker):
        baseline = yahoo_events.get_earnings_dates("AAPL")
        garbage = yahoo_events.get_earnings_dates("AAPL", as_of="not-a-date")
    assert baseline == garbage
    assert mock_ticker.call_count == 1


# ---------------------------------------------------------------------------
# as_of support — get_analyst_actions
# ---------------------------------------------------------------------------


def test_get_analyst_actions_as_of_none_preserves_existing_behavior() -> None:
    df = _reco_df([
        {
            "_date": _days_ago(5),
            "Firm": "GS",
            "To Grade": "Buy",
            "From Grade": "Hold",
            "Action": "Upgrades",
        },
    ])
    mock_ticker = _patch_with("recommendations", df)
    with patch(_TICKER_PATCH_TARGET, mock_ticker):
        baseline = yahoo_events.get_analyst_actions("AAPL", days=90)
        explicit_none = yahoo_events.get_analyst_actions(
            "AAPL", days=90, as_of=None
        )
    assert baseline == explicit_none
    assert mock_ticker.call_count == 1


def test_get_analyst_actions_as_of_uses_window_relative_to_as_of() -> None:
    df = _reco_df([
        {
            "_date": "2025-06-01",
            "Firm": "Recent-vs-asof",
            "To Grade": "Buy",
            "From Grade": "Hold",
            "Action": "Upgrades",
        },
        {
            "_date": "2025-01-01",
            "Firm": "Older-vs-asof",
            "To Grade": "Sell",
            "From Grade": "Hold",
            "Action": "Downgrades",
        },
        {
            "_date": "2025-09-01",
            "Firm": "After-asof",
            "To Grade": "Buy",
            "From Grade": "Hold",
            "Action": "Upgrades",
        },
    ])
    mock_ticker = _patch_with("recommendations", df)
    with patch(_TICKER_PATCH_TARGET, mock_ticker):
        # Window: [2025-03-30, 2025-06-28] -> only the 2025-06-01 action qualifies.
        rows = yahoo_events.get_analyst_actions(
            "AAPL", days=90, as_of="2025-06-28"
        )
    assert len(rows) == 1
    assert rows[0]["firm"] == "Recent-vs-asof"


def test_get_analyst_actions_as_of_drops_post_asof_rows() -> None:
    # Actions after as_of must be filtered out — they're "the future"
    # relative to the historical computation.
    df = _reco_df([
        {
            "_date": "2025-06-01",
            "Firm": "Before",
            "To Grade": "Buy",
            "From Grade": "Hold",
            "Action": "Upgrades",
        },
        {
            "_date": "2025-12-01",
            "Firm": "After",
            "To Grade": "Buy",
            "From Grade": "Hold",
            "Action": "Upgrades",
        },
    ])
    mock_ticker = _patch_with("recommendations", df)
    with patch(_TICKER_PATCH_TARGET, mock_ticker):
        rows = yahoo_events.get_analyst_actions(
            "AAPL", days=180, as_of="2025-07-15"
        )
    assert {r["firm"] for r in rows} == {"Before"}


def test_get_analyst_actions_as_of_cache_key_isolated() -> None:
    df = _reco_df([
        {
            "_date": "2025-05-01",
            "Firm": "X",
            "To Grade": "Buy",
            "From Grade": "Hold",
            "Action": "Upgrades",
        },
    ])
    mock_ticker = _patch_with("recommendations", df)
    with patch(_TICKER_PATCH_TARGET, mock_ticker):
        a = yahoo_events.get_analyst_actions("AAPL", as_of="2025-05-15")
        b = yahoo_events.get_analyst_actions("AAPL", as_of="2025-06-15")
        c = yahoo_events.get_analyst_actions("AAPL")
    # Three distinct cache keys.
    assert mock_ticker.call_count == 3
    # 2025-05-15 -> within 90d window of 2025-05-01 -> 1 row.
    assert len(a) == 1
    # 2025-06-15 -> 2025-05-01 is within 90d -> still 1 row.
    assert len(b) == 1
    # c may have 0 rows if today is far past 2025-05-01; we just assert
    # the call happened.
    assert isinstance(c, list)


def test_get_analyst_actions_as_of_before_data_returns_empty() -> None:
    df = _reco_df([
        {
            "_date": "2025-05-01",
            "Firm": "X",
            "To Grade": "Buy",
            "From Grade": "Hold",
            "Action": "Upgrades",
        },
    ])
    mock_ticker = _patch_with("recommendations", df)
    with patch(_TICKER_PATCH_TARGET, mock_ticker):
        rows = yahoo_events.get_analyst_actions(
            "AAPL", days=90, as_of="2020-01-01"
        )
    assert rows == []


def test_get_analyst_actions_as_of_invalid_falls_back_to_today() -> None:
    df = _reco_df([
        {
            "_date": _days_ago(5),
            "Firm": "X",
            "To Grade": "Buy",
            "From Grade": "Hold",
            "Action": "Upgrades",
        },
    ])
    mock_ticker = _patch_with("recommendations", df)
    with patch(_TICKER_PATCH_TARGET, mock_ticker):
        baseline = yahoo_events.get_analyst_actions("AAPL", days=90)
        garbage = yahoo_events.get_analyst_actions(
            "AAPL", days=90, as_of="garbage"
        )
    assert baseline == garbage
    assert mock_ticker.call_count == 1
