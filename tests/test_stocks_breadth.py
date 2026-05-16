"""Contract tests for the Stocks breadth feature.

The Stocks tab grew a 90-day stacked-bar "signal breadth" chart that counts
how many of the top-50 most-active US stocks sit in each bucket
(STRONG BUY → STRONG SELL) per day. The chart binds directly to two payload
slices:

* ``market.stocks_signals`` — one entry per stock with a ``history`` series
  carrying the per-day score + bucket label.
* ``market.markets_top`` — the source for the Crypto Signals breadth chart.
  The dashboard joins this with ``signals.compute_all_top20({"market": ...})``
  at payload-build time to derive ``DATA.signals_top20``; the *shape* the
  breadth view depends on still lives in ``market.json``.

These tests guard the contract — they skip when the data isn't refetched
(CI runs against a snapshotted market.json that may pre-date the breadth
feature) so the suite stays green on stale fixtures.
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
MARKET_JSON = ROOT / "data" / "market.json"

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _load_market_or_skip() -> dict:
    """Mirror the ``_load_json_or_skip`` helper from test_dashboard_integration."""
    if not MARKET_JSON.exists():
        pytest.skip(f"{MARKET_JSON.name} not present — run fetch_market.py first")
    try:
        return json.loads(MARKET_JSON.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        pytest.fail(f"{MARKET_JSON.name} is not valid JSON: {e}")


def _stocks_signals_or_skip(market: dict) -> list[dict]:
    ss = market.get("stocks_signals")
    if not isinstance(ss, list) or not ss:
        pytest.skip(
            "market.stocks_signals missing or empty — fetch_market.py hasn't "
            "been run since the breadth feature landed"
        )
    return ss


def _markets_top_or_skip(market: dict) -> list[dict]:
    mt = market.get("markets_top")
    if not isinstance(mt, list) or not mt:
        pytest.skip(
            "market.markets_top missing or empty — nothing to feed the crypto "
            "breadth chart"
        )
    return mt


# ---------- stocks_signals.history contract ----------


def test_stocks_signals_have_90d_history():
    """Each stocks_signals entry should carry a multi-month history so the
    90-day breadth chart has data to bucket. SMA50 isn't computable for the
    first ~50 days of any series, so be lenient — require >= 60 entries
    rather than a strict 90.
    """
    market = _load_market_or_skip()
    signals = _stocks_signals_or_skip(market)

    short_tails: list[tuple[str, int]] = []
    for entry in signals:
        if not isinstance(entry, dict):
            continue
        sym = entry.get("symbol") or "<no-symbol>"
        history = entry.get("history")
        if not isinstance(history, list):
            short_tails.append((sym, -1))
            continue
        if len(history) < 60:
            short_tails.append((sym, len(history)))

    assert not short_tails, (
        "stocks_signals entries with history < 60 days "
        "(breadth chart can't render 90d view): "
        f"{short_tails[:10]}{'...' if len(short_tails) > 10 else ''}"
    )


def test_stocks_signals_count_is_at_least_30():
    """We aim for the top-50 most-active but some symbols can be dropped on
    chart-fetch failures. Be lenient at 30 — below that the breadth bars get
    statistically noisy and the tab promise of 'top 50' is misleading.
    """
    market = _load_market_or_skip()
    signals = _stocks_signals_or_skip(market)
    assert len(signals) >= 30, (
        f"Only {len(signals)} stocks_signals entries — breadth chart needs "
        f">=30 to be statistically meaningful (target is 50)"
    )


def test_signal_history_score_is_numeric():
    """Every history entry's ``score`` must be a finite number. NaN/inf will
    silently break the stacked bar by skipping rows; strings will explode the
    bucket reducer."""
    market = _load_market_or_skip()
    signals = _stocks_signals_or_skip(market)

    bad: list[tuple[str, int, object]] = []
    for entry in signals:
        if not isinstance(entry, dict):
            continue
        sym = entry.get("symbol") or "<no-symbol>"
        history = entry.get("history")
        if not isinstance(history, list):
            continue
        for i, point in enumerate(history):
            if not isinstance(point, dict):
                bad.append((sym, i, point))
                continue
            score = point.get("score")
            if isinstance(score, bool) or not isinstance(score, (int, float)):
                bad.append((sym, i, score))
                continue
            if not math.isfinite(score):
                bad.append((sym, i, score))
        if len(bad) > 20:
            break

    assert not bad, (
        f"stocks_signals history has non-finite/non-numeric scores: {bad[:10]}"
        f"{'...' if len(bad) > 10 else ''}"
    )


def test_signal_history_dates_are_iso():
    """Every history entry's ``date`` must be a YYYY-MM-DD string. The
    breadth chart bins by date — anything else (Date objects in JSON?
    timestamps? localised strings?) breaks the per-day group-by."""
    market = _load_market_or_skip()
    signals = _stocks_signals_or_skip(market)

    bad: list[tuple[str, int, object]] = []
    for entry in signals:
        if not isinstance(entry, dict):
            continue
        sym = entry.get("symbol") or "<no-symbol>"
        history = entry.get("history")
        if not isinstance(history, list):
            continue
        for i, point in enumerate(history):
            if not isinstance(point, dict):
                bad.append((sym, i, point))
                continue
            date = point.get("date")
            if not isinstance(date, str) or not _ISO_DATE.match(date):
                bad.append((sym, i, date))
        if len(bad) > 20:
            break

    assert not bad, (
        f"stocks_signals history has non-ISO dates: {bad[:10]}"
        f"{'...' if len(bad) > 10 else ''}"
    )


# ---------- markets_top shape supports crypto breadth ----------


def test_signals_top20_has_history_for_breadth():
    """``DATA.signals_top20`` is computed at payload-build time but its source
    rows live in ``market.markets_top``. The crypto breadth chart needs a
    score per entry plus, on at least the top entries, a history series to
    plot 90 days of buckets.

    The breadth chart is forgiving — it'll truncate to whatever the shortest
    common history is — so this test just confirms the *shape* is right: every
    markets_top entry has a symbol, and at least one of the top entries has
    a history-like field we can plot.
    """
    market = _load_market_or_skip()
    markets_top = _markets_top_or_skip(market)

    # Every entry needs a symbol (join key for downstream signal computation).
    for i, entry in enumerate(markets_top):
        assert isinstance(entry, dict), (
            f"markets_top[{i}] is not a dict: {type(entry).__name__}"
        )
        sym = entry.get("symbol")
        assert isinstance(sym, str) and sym, (
            f"markets_top[{i}] missing string 'symbol' (got {sym!r})"
        )

    # At least one top entry should carry a history-like series the breadth
    # chart can plot. We accept any of the conventional names — the renderer
    # falls back across them — so this is a soft existence check, not a
    # strict schema lock.
    history_keys = ("history", "signal_history", "sparkline_7d")
    top_n = min(10, len(markets_top))
    have_history = 0
    for entry in markets_top[:top_n]:
        for k in history_keys:
            v = entry.get(k)
            if isinstance(v, list) and len(v) > 0:
                have_history += 1
                break

    assert have_history > 0, (
        f"None of the top {top_n} markets_top entries carry any of "
        f"{history_keys!r} — crypto breadth chart has no series to plot"
    )
