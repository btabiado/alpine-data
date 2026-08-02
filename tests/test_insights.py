"""Tests for the rule-based insights engine and its per-tab tagging."""
from __future__ import annotations

from datetime import datetime, timedelta

import insights


def _recent(days_ago: int) -> str:
    """YYYY-MM-DD `days_ago` days before today (UTC).

    Every insight generator is gated behind a freshness check sized to its
    feed's cadence (see insights.FRESHNESS_MAX_AGE_DAYS), so fixtures that want
    a present-tense rule to fire must use dates relative to *now* — a
    hard-coded date silently rots past the cutoff and the rule stops firing.
    """
    return (datetime.utcnow() - timedelta(days=days_ago)).strftime("%Y-%m-%d")


def _stale(days_ago: int = 68) -> str:
    """A date old enough to trip every freshness guard in insights.py.

    68 days is the gap the real 2026 feed freeze produced (2026-05-26 →
    2026-08-02) and is comfortably beyond the largest per-feed budget
    (``stocks``, at 5 days).
    """
    return _recent(days_ago)


def _dated(values, *, newest_days_ago: int = 0, key: str = "value") -> list[dict]:
    """Turn a list of numbers into a ``[{date, <key>}]`` daily series.

    The LAST entry lands ``newest_days_ago`` days before today and earlier
    entries step back one day each, so the same fixture can be made fresh or
    frozen by moving that one argument. The generators derive a feed's age
    from exactly these ``date`` fields.
    """
    n = len(values)
    return [
        {"date": _recent(newest_days_ago + (n - 1 - i)), key: v}
        for i, v in enumerate(values)
    ]


def _score_history(n: int = 3, *, newest_days_ago: int = 0) -> list[dict]:
    """A ``[{date, score}]`` rolling-signal history whose newest entry is
    ``newest_days_ago`` days old. Only the dates matter to the guards.
    """
    return _dated([0] * n, newest_days_ago=newest_days_ago, key="score")


# ---------- per-tab tagging ----------

def test_etf_insights_tagged_etf():
    """Anything coming out of _etf_insights should end up on the ETF tab."""
    payload = {
        "btc": {
            "daily": [
                {"date": _recent(2), "flow": 100.0, "cumulative": 100.0},
                {"date": _recent(1), "flow": -50.0, "cumulative": 50.0},
            ],
            "stats": {"all_time": 50.0},
            "by_fund_daily": {},
        },
        "eth": {},
        "market": {},
        "signals": {},
    }
    out = insights.build_insights(payload)
    etf = [i for i in out if i.get("kind") == "etf" or i.get("headline", "").startswith("BTC ETF")]
    assert etf, "expected at least one ETF-flow insight"
    for i in etf:
        assert i["tab"] == "etf", f"expected tab=etf but got {i.get('tab')!r} for {i.get('headline')!r}"


def test_etf_insights_suppressed_when_feed_is_stale():
    """A stale ETF feed must not surface present-tense 'today' headlines.

    Regression guard: `_etf_insights` computed a `fresh` flag but only applied
    it to the flow-pace rule, so a CSV that stopped updating months ago still
    produced "BTC ETF outflow of $X on <old date>" as a current insight — the
    last row is just the newest one on file, not news.
    """
    stale_payload = {
        "btc": {
            "daily": [
                {"date": "2026-05-11", "flow": 100.0, "cumulative": 100.0},
                {"date": "2026-05-12", "flow": -115.2, "cumulative": -15.2},
            ],
            "stats": {"all_time": -15.2},
            "by_fund_daily": {},
        },
        "eth": {}, "market": {}, "signals": {},
    }
    out = insights.build_insights(stale_payload)
    offenders = [
        i for i in out
        if "ETF outflow" in i.get("headline", "")
        or "ETF inflow" in i.get("headline", "")
        or "top mover today" in i.get("headline", "")
        or "last 7d" in i.get("headline", "")
    ]
    assert not offenders, f"stale ETF feed still emitted current-sounding insights: {offenders}"


def test_etf_cumulative_milestone_survives_a_stale_feed():
    """Cumulative milestones are deliberately NOT gated on freshness.

    They describe an all-time total, which stays true even when the last row
    is old — so the staleness guard must not suppress them.
    """
    payload = {
        "btc": {
            "daily": [
                {"date": "2026-05-11", "flow": 100.0, "cumulative": 9_950.0},
                {"date": "2026-05-12", "flow": 100.0, "cumulative": 10_050.0},
            ],
            "stats": {"all_time": 10_050.0},
            "by_fund_daily": {},
        },
        "eth": {}, "market": {}, "signals": {},
    }
    out = insights.build_insights(payload)
    milestones = [i for i in out if "cumulative inflows" in i.get("headline", "")]
    assert milestones, "cumulative milestone should still fire on a stale feed"


def test_signal_insights_tagged_signals():
    """STRONG BUY/SELL and signal flips belong on the Signals tab."""
    payload = {
        "btc": {},
        "eth": {},
        "market": {},
        "signals": {
            "btc": {
                "label": "STRONG BUY",
                "score": 65,
                "as_of": _recent(0),
                "components": [{"name": "RSI", "contribution": 15},
                               {"name": "MACD", "contribution": 10},
                               {"name": "Funding", "contribution": 10}],
                "history": [{"date": _recent(1), "score": -10},
                            {"date": _recent(0), "score": 65}],
            }
        },
    }
    out = insights.build_insights(payload)
    sig = [i for i in out if i.get("kind") == "signal"]
    assert sig, "expected at least one signal insight"
    for i in sig:
        assert i["tab"] == "signals", f"expected tab=signals for {i.get('headline')!r}"


def test_market_insight_tab_classifier_known_patterns():
    """Headline-based market insight classifier must hit every known tab."""
    f = insights._market_insight_tab
    assert f({"headline": "Fear & Greed at 18 — extreme fear (contrarian buy zone)"}) == "trading"
    assert f({"headline": "BTC funding flipped negative (-0.0010%)"}) == "trading"
    assert f({"headline": "BTC DVOL crushed (-1.8σ vs 30d mean)"}) == "trading"
    assert f({"headline": "ETH/BTC at ~6-month low (0.04212)"}) == "trading"

    assert f({"headline": "ETH gas spike: base fee 88 gwei"}) == "defi"
    assert f({"headline": "ETH gas near zero (0.40 gwei)"}) == "defi"
    assert f({"headline": "Stablecoin supply +$2.10B over the last 7d"}) == "defi"
    assert f({"headline": "DEX 24h volume: $6.20B  ·  protocol fees: $48.5M"}) == "defi"
    assert f({"headline": "Base TVL +5.3% today ($2.1B)"}) == "defi"

    assert f({"headline": "BTC mempool congested: 120 sat/vB fastest fee"}) == "whale"
    assert f({"headline": "BTC mempool quiet (2 sat/vB)"}) == "whale"
    assert f({"headline": "BTC hashrate at 30-day high (640 EH/s)"}) == "whale"
    assert f({"headline": "BTC difficulty retarget in ~2.1 days: +4.8% (harder for miners)"}) == "whale"
    assert f({"headline": "BTC mining concentration high: top 2 pools = 58.0% of blocks"}) == "whale"
    assert f({"headline": "BTC on-chain transfer volume spike: Whale tx volume +2.4σ vs 30d mean"}) == "whale"
    assert f({"headline": "BTC active addresses +1.8σ vs 30d"}) == "whale"

    # Trading: open interest + long/short crowding
    assert f({"headline": "BTC open interest +2.1σ above 30d mean"}) == "trading"
    assert f({"headline": "ETH L/S ratio crowded long (2.85)"}) == "trading"
    assert f({"headline": "LINK L/S ratio crowded short (0.55)"}) == "trading"

    # "Markets" tab no longer exists — these now route to real tabs that
    # have an insights bar. Traditional indices + macro → Stocks (where the
    # Traditional Indices card lives now). Crypto-wide moves → Crypto Signals.
    # News → Research (social tab). DEX hot pool → DeFi.
    assert f({"headline": "DXY +1.2% today — typically inverse to risk assets including crypto"}) == "stocks"
    assert f({"headline": "10Y Treasury yield crossed above 5.0% (5.02%)"}) == "stocks"
    assert f({"headline": "Gold at 30-day high ($2,420.50/oz)"}) == "stocks"
    assert f({"headline": "S&P 500 -2.4% today — risk-off may pressure crypto"}) == "stocks"
    assert f({"headline": "📰 CoinDesk: Some headline goes here"}) == "social"
    assert f({"headline": "ZANO (Zano) is trending #1 on CoinGecko"}) == "signals"
    assert f({"headline": "BTC price divergence: CoinGecko $79,200 vs Coinbase $79,500"}) == "signals"
    assert f({"headline": "DEX hot pool: PEPE/WETH on ethereum +45% with $80M volume"}) == "defi"
    assert f({"headline": "NASDAQ +1.80% on the day"}) == "stocks"
    assert f({"headline": "Dow Jones -2.10% on the day"}) == "stocks"
    assert f({"headline": "VIX crossed above 20 (22.4) — calm→fear"}) == "stocks"
    assert f({"headline": "VIX fell below 30 (28.1) — panic→fear"}) == "stocks"
    # Top-25 movers + BTC dominance + market-cap milestones
    assert f({"headline": "Top-25 24h gainer: SOL +7.4% (rank #5)"}) == "signals"
    assert f({"headline": "Top-25 24h loser: ADA -6.1% (rank #11)"}) == "signals"
    assert f({"headline": "Top-25 7d momentum: AVAX +18.2% week (rank #14)"}) == "signals"
    assert f({"headline": "Top-25 7d laggard: DOT -16.4% week (rank #16)"}) == "signals"
    assert f({"headline": "BTC dominance high: 61.2% — alt season unlikely"}) == "signals"
    assert f({"headline": "BTC dominance low: 43.8% — alt rotation in play"}) == "signals"
    assert f({"headline": "Total crypto market cap above $4T (now $4.12T)"}) == "signals"


def test_market_insight_tab_classifier_falls_back_to_signals():
    # Default now lands on Crypto Signals (was "markets" — a dropped tab).
    f = insights._market_insight_tab
    assert f({"headline": "something nobody recognises"}) == "signals"
    assert f({"headline": ""}) == "signals"


def test_every_insight_has_a_valid_tab():
    """Black-box: whatever build_insights emits, every item must carry a tab
    in the allowed vocabulary so the JS filter never receives an unknown."""
    payload = {
        "btc": {
            "daily": [
                {"date": "2024-01-10", "flow": 200.0, "cumulative": 200.0},
                {"date": "2024-01-11", "flow": -50.0, "cumulative": 150.0},
            ],
            "stats": {"all_time": 150.0},
        },
        "eth": {
            "daily": [{"date": "2024-07-23", "flow": 5.0, "cumulative": 5.0}],
            "stats": {"all_time": 5.0},
        },
        "market": {
            "fear_greed": [{"value": 18, "label": "Extreme Fear"}],
            "btc": {
                "funding": [{"rate": 0.0005}, {"rate": -0.0010}],
                "price": [{"value": 43200}],
            },
            "eth_gas": {"base_fee_gwei": 0.4, "fast_gwei": 0.6},
            "mempool": {"fees_sat_vb": {"fastestFee": 2}},
            "defillama": {"stablecoin_7d_change_usd": 2_100_000_000, "stablecoin_mcap_usd": 180_000_000_000,
                          "dex_volume_24h_usd": 6_200_000_000, "fees_24h_usd": 48_500_000},
        },
        "signals": {
            "btc": {"label": "STRONG SELL", "score": -55,
                    "components": [{"name": "SMA50", "contribution": -20}],
                    "history": [{"score": 10}, {"score": -55}]},
        },
    }
    out = insights.build_insights(payload)
    assert out, "expected the seeded payload to produce at least one insight"
    for i in out:
        assert "tab" in i, f"insight missing tab: {i!r}"
        assert i["tab"] in insights.VALID_TABS, \
            f"insight tab {i['tab']!r} not in VALID_TABS for {i.get('headline')!r}"


# ---------- whale: network velocity rule ----------

def _whale_payload(velocity_series: list[float], *, newest_days_ago: int = 0) -> dict:
    """Build a payload whose tx_volume_usd / active_addresses ratio matches
    the provided ``velocity_series`` (one entry per day, oldest first).
    active_addresses is held constant so the ratio = tx_volume directly,
    which keeps the test math simple.

    ``newest_days_ago`` shifts the whole series back in time so the same
    fixture can exercise the whale freshness guard from both sides.
    """
    addrs = 1_000_000.0
    n = len(velocity_series)
    rows_vol = []
    rows_addr = []
    for i, v in enumerate(velocity_series):
        # Newest bar is `newest_days_ago` days old; earlier bars step back one
        # day each. Hard-coded 2024 dates here used to make the whole series
        # read as ~2 years stale once the whale freshness guard landed.
        date = _recent(newest_days_ago + n - 1 - i)
        rows_vol.append({"date": date, "value": v * addrs})
        rows_addr.append({"date": date, "value": addrs})
    return {
        "btc": {}, "eth": {}, "market": {}, "signals": {},
        "whale": {"btc": {
            "tx_volume_usd": rows_vol,
            "active_addresses": rows_addr,
        }},
    }


def test_network_velocity_spike_fires_and_tagged_whale():
    """A flat ratio for 30d then a big jump on day 31 should trip the
    network-velocity-spike rule and the insight must be tagged tab='whale'."""
    series = [1000.0] * 30 + [5000.0]  # day 31 is a huge ratio spike
    payload = _whale_payload(series)
    out = insights.build_insights(payload, limit=100)
    hits = [i for i in out if "network velocity" in i.get("headline", "").lower()]
    assert hits, "expected network velocity spike insight to fire"
    for i in hits:
        assert i["tab"] == "whale", f"expected tab=whale, got {i.get('tab')!r}"
        assert i["kind"] == "anomaly"
        assert i["asset"] == "btc"


def test_network_velocity_no_spike_when_flat():
    """A flat ratio (no variance, latest equals mean) must not emit the
    velocity-spike anomaly."""
    series = [1000.0] * 31
    payload = _whale_payload(series)
    out = insights.build_insights(payload, limit=100)
    hits = [i for i in out if "network velocity" in i.get("headline", "").lower()]
    assert not hits, f"did not expect velocity spike for flat ratio, got {hits!r}"


def test_network_velocity_skipped_when_series_too_short():
    """Fewer than 31 daily ratios → zscore unavailable → rule must stay silent."""
    series = [1000.0] * 10 + [9999.0]  # latest spikes but only 11 days total
    payload = _whale_payload(series)
    out = insights.build_insights(payload, limit=100)
    hits = [i for i in out if "network velocity" in i.get("headline", "").lower()]
    assert not hits, "rule should be silent with <31 daily ratios"


# ---------- AI News tab insights ----------

def _ainews_payload(
    *,
    summary: dict | None = None,
    items: list[dict] | None = None,
    stocks: list[dict] | None = None,
    curated: dict | None = None,
    available: bool = True,
) -> dict:
    """Build a minimal payload that exercises only the AI insight rules.

    Other generators (etf/signals/whale) receive empty structures so they
    don't emit anything that could clutter the assertions.
    """
    market = {}
    market["ai_news"] = {
        "available": available,
        "items": items or [],
        "summary": summary or {},
    }
    if stocks is not None:
        market["stocks_signals"] = stocks
    if curated is not None:
        market["ai_curated"] = curated
    return {
        "btc": {}, "eth": {},
        "market": market,
        "signals": {},
    }


def test_ainews_sentiment_skew_positive_fires_and_tagged_ainews():
    summary = {"positive": 38, "negative": 6, "neutral": 16, "total": 60,
               "net_score": 32, "sentiment_label": "POSITIVE"}
    payload = _ainews_payload(summary=summary)
    out = insights.build_insights(payload, limit=100)
    hits = [i for i in out if "sentiment skews POSITIVE" in i.get("headline", "")]
    assert hits, f"expected POSITIVE sentiment-skew insight, got {[i['headline'] for i in out]!r}"
    for i in hits:
        assert i["tab"] == "ainews"
        assert i["severity"] == "good"


def test_ainews_sentiment_skew_negative_fires():
    summary = {"positive": 5, "negative": 28, "neutral": 12, "total": 45,
               "net_score": -23, "sentiment_label": "NEGATIVE"}
    payload = _ainews_payload(summary=summary)
    out = insights.build_insights(payload, limit=100)
    hits = [i for i in out if "sentiment skews NEGATIVE" in i.get("headline", "")]
    assert hits
    for i in hits:
        assert i["tab"] == "ainews"
        assert i["severity"] == "bad"


def test_ainews_sentiment_skew_silent_when_total_too_low():
    """Below the 15-article floor the rule must stay silent (avoid noise)."""
    summary = {"positive": 6, "negative": 0, "neutral": 2, "total": 8,
               "net_score": 6, "sentiment_label": "POSITIVE"}
    payload = _ainews_payload(summary=summary)
    out = insights.build_insights(payload, limit=100)
    hits = [i for i in out if "sentiment skews" in i.get("headline", "")]
    assert not hits


def test_ainews_volume_surge_fires_when_total_gte_50():
    summary = {"positive": 20, "negative": 18, "neutral": 16, "total": 54,
               "net_score": 2, "sentiment_label": "NEUTRAL"}
    payload = _ainews_payload(summary=summary)
    out = insights.build_insights(payload, limit=100)
    hits = [i for i in out if "AI news flow heavy" in i.get("headline", "")]
    assert hits, "expected volume-surge insight to fire at total=54"
    assert hits[0]["tab"] == "ainews"


def test_ainews_volume_surge_silent_below_threshold():
    summary = {"positive": 10, "negative": 10, "neutral": 10, "total": 30,
               "net_score": 0, "sentiment_label": "NEUTRAL"}
    payload = _ainews_payload(summary=summary)
    out = insights.build_insights(payload, limit=100)
    hits = [i for i in out if "AI news flow heavy" in i.get("headline", "")]
    assert not hits


def test_ainews_source_dominance_fires_when_one_source_gt_40pct():
    items = (
        [{"title": f"tc {i}", "url": "u", "source": "TechCrunch AI"} for i in range(12)]
        + [{"title": f"v {i}", "url": "u", "source": "The Verge AI"} for i in range(5)]
        + [{"title": f"vb {i}", "url": "u", "source": "VentureBeat AI"} for i in range(4)]
        + [{"title": f"o {i}", "url": "u", "source": "OpenAI"} for i in range(4)]
    )
    summary = {"positive": 10, "negative": 8, "neutral": 7, "total": len(items),
               "net_score": 2, "sentiment_label": "NEUTRAL"}
    payload = _ainews_payload(summary=summary, items=items)
    out = insights.build_insights(payload, limit=100)
    hits = [i for i in out if "AI news flow concentrated" in i.get("headline", "")]
    assert hits, f"expected source-dominance insight; got {[i['headline'] for i in out]!r}"
    assert "TechCrunch AI" in hits[0]["headline"]
    assert hits[0]["tab"] == "ainews"


def test_ainews_source_dominance_silent_when_spread_even():
    items = [{"title": f"t {i}", "url": "u", "source": s}
             for s in ("A", "B", "C", "D", "E") for i in range(5)]  # 25 items, 5/source
    summary = {"positive": 10, "negative": 8, "neutral": 7, "total": len(items),
               "net_score": 2, "sentiment_label": "NEUTRAL"}
    payload = _ainews_payload(summary=summary, items=items)
    out = insights.build_insights(payload, limit=100)
    hits = [i for i in out if "AI news flow concentrated" in i.get("headline", "")]
    assert not hits


def test_ainews_top_ticker_strong_score_fires_for_ai_exposed_ticker():
    stocks = [
        {"symbol": "NVDA", "name": "Nvidia", "score": 62, "label": "STRONG BUY"},
        {"symbol": "GOOGL", "name": "Alphabet", "score": 10, "label": "HOLD"},
        {"symbol": "XYZ", "name": "Not AI", "score": 95, "label": "STRONG BUY"},  # filtered out
    ]
    payload = _ainews_payload(stocks=stocks)
    out = insights.build_insights(payload, limit=100)
    hits = [i for i in out if i.get("tab") == "ainews" and "NVDA" in i.get("headline", "")
            and "STRONG BUY" in i.get("headline", "")]
    assert hits, f"expected NVDA STRONG BUY ainews insight; got {[i['headline'] for i in out]!r}"
    assert hits[0]["asset"] == "NVDA"
    assert hits[0]["severity"] == "good"


def test_ainews_top_ticker_silent_when_no_strong_signal():
    stocks = [
        {"symbol": "NVDA", "name": "Nvidia", "score": 25, "label": "BUY"},
        {"symbol": "MSFT", "name": "Microsoft", "score": -10, "label": "HOLD"},
    ]
    payload = _ainews_payload(stocks=stocks)
    out = insights.build_insights(payload, limit=100)
    hits = [i for i in out if i.get("tab") == "ainews" and "AI-exposed ticker" in i.get("headline", "")]
    assert not hits


def test_ainews_ticker_flip_via_history_fires():
    """Sign flip from -40 → +45 across the 7d history window must trigger."""
    history = [{"date": f"2024-01-{d:02d}", "score": -40} for d in range(1, 8)]
    history.append({"date": "2024-01-08", "score": 45})
    stocks = [{"symbol": "AMD", "name": "Advanced Micro", "score": 45,
               "label": "BUY", "history": history}]
    payload = _ainews_payload(stocks=stocks)
    out = insights.build_insights(payload, limit=100)
    hits = [i for i in out if i.get("tab") == "ainews" and "AMD signal flipped" in i.get("headline", "")]
    assert hits, f"expected AMD flip insight; got {[i['headline'] for i in out]!r}"
    assert "positive" in hits[0]["headline"]


def test_ainews_ticker_flip_silent_when_near_zero():
    """A flip from -5 → +5 should NOT fire — too noisy near zero."""
    history = [{"date": f"2024-01-{d:02d}", "score": -5} for d in range(1, 8)]
    history.append({"date": "2024-01-08", "score": 5})
    stocks = [{"symbol": "AMD", "name": "Advanced Micro", "score": 5,
               "label": "HOLD", "history": history}]
    payload = _ainews_payload(stocks=stocks)
    out = insights.build_insights(payload, limit=100)
    hits = [i for i in out if "AMD signal flipped" in i.get("headline", "")]
    assert not hits


def test_ainews_sentiment_price_divergence_fires():
    summary = {"positive": 30, "negative": 5, "neutral": 5, "total": 40,
               "net_score": 25, "sentiment_label": "POSITIVE"}
    stocks = [
        {"symbol": "NVDA", "name": "Nvidia", "score": -20, "label": "SELL"},
        {"symbol": "AMD",  "name": "AMD",    "score": -15, "label": "SELL"},
        {"symbol": "MSFT", "name": "MSFT",   "score": -10, "label": "HOLD"},
    ]
    payload = _ainews_payload(summary=summary, stocks=stocks)
    out = insights.build_insights(payload, limit=100)
    hits = [i for i in out if "AI sentiment / price divergence" in i.get("headline", "")]
    assert hits, f"expected divergence insight; got {[i['headline'] for i in out]!r}"
    assert hits[0]["tab"] == "ainews"
    assert hits[0]["severity"] == "alert"


def test_ainews_mega_round_fires_for_fresh_billion_dollar_round():
    from datetime import datetime, timedelta
    recent_date = (datetime.utcnow() - timedelta(days=3)).strftime("%Y-%m-%d")
    curated = {
        "top_funded_companies": [
            {"name": "Anthropic", "valuation_usd": 185_000_000_000,
             "last_round_size_usd": 5_000_000_000,
             "last_round_date": recent_date,
             "last_round_stage": "Series F"},
            # Stale round — must be filtered out.
            {"name": "OldCo",     "valuation_usd": 10_000_000_000,
             "last_round_size_usd": 2_000_000_000,
             "last_round_date": "2024-01-01",
             "last_round_stage": "Series D"},
        ],
    }
    payload = _ainews_payload(curated=curated)
    out = insights.build_insights(payload, limit=100)
    hits = [i for i in out if "AI mega-round" in i.get("headline", "")]
    assert hits, f"expected mega-round insight; got {[i['headline'] for i in out]!r}"
    assert "Anthropic" in hits[0]["headline"]
    assert hits[0]["tab"] == "ainews"
    assert hits[0]["severity"] == "good"
    assert "OldCo" not in hits[0]["headline"]


def test_ainews_mega_round_silent_when_round_too_small():
    from datetime import datetime, timedelta
    recent_date = (datetime.utcnow() - timedelta(days=2)).strftime("%Y-%m-%d")
    curated = {
        "top_funded_companies": [
            {"name": "SmallCo", "valuation_usd": 1_000_000_000,
             "last_round_size_usd": 100_000_000,
             "last_round_date": recent_date,
             "last_round_stage": "Series B"},
        ],
    }
    payload = _ainews_payload(curated=curated)
    out = insights.build_insights(payload, limit=100)
    hits = [i for i in out if "AI mega-round" in i.get("headline", "")]
    assert not hits


def test_ainews_rules_defensive_on_empty_payload():
    """A wholly empty market section must not crash the AI generator."""
    payload = {"btc": {}, "eth": {}, "market": {}, "signals": {}}
    out = insights._ainews_insights(payload)  # call generator directly
    assert out == []


def test_ainews_rules_defensive_on_malformed_summary():
    """Garbage in summary (None, strings) shouldn't raise."""
    payload = _ainews_payload(summary={"positive": None, "negative": "x", "total": "?",
                                       "net_score": None, "sentiment_label": None})
    out = insights._ainews_insights(payload)
    # No insight should fire, but no exception either.
    assert isinstance(out, list)


def test_ainews_tab_added_to_valid_tabs():
    """The VALID_TABS allowlist must include 'ainews' so the renderer accepts it."""
    assert "ainews" in insights.VALID_TABS
# ---------- new POC-tab rules ----------

def _empty_payload(**overrides):
    """Tiny payload skeleton — every test fills in only what it needs."""
    base = {"btc": {}, "eth": {}, "market": {}, "signals": {}}
    base.update(overrides)
    return base


def test_poc_strong_migration_fires_and_tagged_poc():
    payload = _empty_payload(market={"poc": {
        "btc": {
            "d30": {"poc": 80_000, "current": 79_000},
            "d90": {"poc": 70_000, "current": 79_000},
            "migration": {"delta_pct": 14.28, "direction": "UP",
                          "magnitude": "STRONG", "between_pocs": False,
                          "explanation": "..."},
            "naked": [],
            "migration_series": _dated([79_000, 80_000], key="poc"),
        }
    }})
    out = insights.build_insights(payload, limit=100)
    hits = [i for i in out if "value migrating up" in i.get("headline", "").lower()]
    assert hits, "expected POC strong-migration insight"
    for i in hits:
        assert i["tab"] == "poc"
        assert i["asset"] == "btc"


def test_poc_no_migration_when_flat():
    payload = _empty_payload(market={"poc": {
        "btc": {"migration": {"direction": "FLAT", "magnitude": "WEAK",
                              "delta_pct": 0.3, "between_pocs": False}}
    }})
    out = insights.build_insights(payload, limit=100)
    hits = [i for i in out if "value migrating" in i.get("headline", "").lower()]
    assert not hits, f"expected no migration insight, got {hits!r}"


def test_poc_price_between_pocs_fires():
    payload = _empty_payload(market={"poc": {
        "eth": {
            "d30": {"poc": 3500, "current": 3400},
            "d90": {"poc": 3200, "current": 3400},
            "migration": {"delta_pct": 9.4, "direction": "UP",
                          "magnitude": "STRONG", "between_pocs": True,
                          "explanation": "..."},
            "naked": [],
            "migration_series": _dated([3400, 3500], key="poc"),
        }
    }})
    out = insights.build_insights(payload, limit=100)
    hits = [i for i in out if "sits between" in i.get("headline", "").lower()]
    assert hits, "expected price-between-POCs insight"
    for i in hits:
        assert i["tab"] == "poc"


def test_poc_naked_cluster_fires_across_two_assets():
    """Two assets with ≥3 naked weekly POCs each → cluster insight."""
    naked3 = [{"poc": 50_000, "days_ago": 30, "distance_pct": -1.0, "week_start": "2024-01-01"}] * 3
    fresh_series = _dated([50_000, 50_100], key="poc")
    payload = _empty_payload(market={"poc": {
        "btc": {"naked": naked3, "migration": {"direction": "FLAT"},
                "migration_series": fresh_series},
        "eth": {"naked": naked3, "migration": {"direction": "FLAT"},
                "migration_series": fresh_series},
    }})
    out = insights.build_insights(payload, limit=100)
    hits = [i for i in out if "naked poc cluster" in i.get("headline", "").lower()]
    assert hits, "expected naked-POC cluster insight"
    for i in hits:
        assert i["tab"] == "poc"


def test_poc_dense_single_asset_fires():
    """Single asset with ≥5 naked POCs → dense magnet structure insight."""
    naked5 = [{"poc": 60_000 + i * 1000, "days_ago": 30 + i,
               "distance_pct": -2.0 - i, "week_start": "2024-01-01"} for i in range(5)]
    payload = _empty_payload(market={"poc": {
        "btc": {"naked": naked5, "migration": {"direction": "FLAT"},
                "migration_series": _dated([60_000, 61_000], key="poc")},
    }})
    out = insights.build_insights(payload, limit=100)
    hits = [i for i in out if "naked weekly pocs" in i.get("headline", "").lower()]
    assert hits, "expected dense-naked-POC insight"


# ---------- new social-tab rules ----------

def test_social_cc_news_sentiment_skew_fires():
    payload = _empty_payload(market={"social": {
        "cc_news": {"fetched_at": _recent(0) + "T12:00:00+00:00", "coins": {
            "btc": {"net_score": 8, "article_count": 30,
                    "positive": 20, "negative": 12, "neutral": 18},
        }},
    }})
    out = insights.build_insights(payload, limit=100)
    hits = [i for i in out if "news sentiment skews" in i.get("headline", "").lower()]
    assert hits, "expected CC news sentiment skew insight"
    for i in hits:
        assert i["tab"] == "social"


def test_social_cc_news_silent_when_balanced():
    payload = _empty_payload(market={"social": {
        "cc_news": {"fetched_at": _recent(0) + "T12:00:00+00:00", "coins": {
            "btc": {"net_score": 2, "article_count": 30,
                    "positive": 12, "negative": 10, "neutral": 28},
        }},
    }})
    out = insights.build_insights(payload, limit=100)
    hits = [i for i in out if "news sentiment skews" in i.get("headline", "").lower()]
    assert not hits, f"expected no skew insight for balanced sentiment, got {hits!r}"


def test_social_reddit_active_user_spike_fires():
    """Three subs with normal active/subs ratios, plus one outlier ≥3× median."""
    payload = _empty_payload(market={"social": {
        "reddit": {"fetched_at": _recent(0) + "T12:00:00+00:00", "subreddits": {
            "CryptoCurrency": {"subscribers": 1_000_000, "active_users": 5_000,
                               "label": "All crypto", "sub": "CryptoCurrency"},
            "Bitcoin":        {"subscribers": 4_000_000, "active_users": 12_000,
                               "label": "BTC", "sub": "Bitcoin"},
            "ethereum":       {"subscribers": 1_500_000, "active_users": 4_500,
                               "label": "ETH", "sub": "ethereum"},
            "Chainlink":      {"subscribers": 100_000, "active_users": 8_000,
                               "label": "LINK", "sub": "Chainlink"},  # 8% — way above the others (~0.3%)
        }},
    }})
    out = insights.build_insights(payload, limit=100)
    hits = [i for i in out if "active-user spike" in i.get("headline", "").lower()]
    assert hits, "expected Reddit active-user spike insight"
    for i in hits:
        assert i["tab"] == "social"


def test_social_santiment_daa_surge_fires():
    payload = _empty_payload(market={"social": {
        "santiment": {"fetched_at": _recent(0) + "T00:05:00+00:00", "coins": {
            "btc": {"daily_active_addresses_delta_pct": 35.0,
                    "daily_active_addresses_latest": 1_200_000,
                    "daily_active_addresses": _dated([900_000, 1_200_000])},
        }},
    }})
    out = insights.build_insights(payload, limit=100)
    hits = [i for i in out if "on-chain attention" in i.get("headline", "").lower()]
    assert hits, "expected Santiment DAA surge insight"
    for i in hits:
        assert i["tab"] == "social"


# ---------- new signals: RSI / MACD rules ----------

def test_signals_rsi_overbought_fires():
    payload = _empty_payload(signals={"btc": {
        "label": "BUY", "score": 25, "as_of": _recent(0),
        "components": [{"name": "RSI(14)", "value": "78.5", "contribution": -15,
                        "explanation": "overbought"}],
        "history": [{"date": _recent(1), "score": 25},
                    {"date": _recent(0), "score": 25}],
    }})
    out = insights.build_insights(payload, limit=100)
    hits = [i for i in out if "rsi overbought" in i.get("headline", "").lower()]
    assert hits, "expected RSI overbought insight"
    for i in hits:
        assert i["tab"] == "signals"
        assert i["asset"] == "btc"


def test_signals_rsi_oversold_fires():
    payload = _empty_payload(signals={"eth": {
        "label": "SELL", "score": -25, "as_of": _recent(0),
        "components": [{"name": "RSI(14)", "value": "22.4", "contribution": 15,
                        "explanation": "oversold"}],
        "history": [{"date": _recent(1), "score": -25},
                    {"date": _recent(0), "score": -25}],
    }})
    out = insights.build_insights(payload, limit=100)
    hits = [i for i in out if "rsi oversold" in i.get("headline", "").lower()]
    assert hits, "expected RSI oversold insight"


def test_signals_rsi_silent_when_neutral():
    payload = _empty_payload(signals={"btc": {
        "label": "HOLD", "score": 5, "as_of": _recent(0),
        "components": [{"name": "RSI(14)", "value": "55.0", "contribution": 0}],
    }})
    out = insights.build_insights(payload, limit=100)
    hits = [i for i in out if "rsi overbought" in i.get("headline", "").lower()
            or "rsi oversold" in i.get("headline", "").lower()]
    assert not hits


def test_signals_macd_histogram_fires():
    payload = _empty_payload(signals={"btc": {
        "label": "BUY", "score": 30, "as_of": _recent(0),
        "components": [{"name": "MACD histogram", "value": "+1.50", "contribution": 10,
                        "explanation": "momentum up"}],
    }})
    out = insights.build_insights(payload, limit=100)
    hits = [i for i in out if "macd histogram" in i.get("headline", "").lower()]
    assert hits, "expected MACD histogram insight"
    for i in hits:
        assert i["tab"] == "signals"


# ---------- new whale (ETH-side) rules ----------

def test_whale_eth_large_transactions_fires():
    payload = _empty_payload(whale={
        "eth": {"large_transactions": [
            {"hash": f"0x{i}", "time": f"{_recent(0)} 0{i % 10}:00:00"}
            for i in range(35)]},
    })
    out = insights.build_insights(payload, limit=100)
    hits = [i for i in out if "large-transaction surge" in i.get("headline", "").lower()]
    assert hits, "expected ETH large-tx surge insight"
    for i in hits:
        assert i["tab"] == "whale"
        assert i["asset"] == "eth"


def test_whale_eth_large_transactions_silent_under_threshold():
    payload = _empty_payload(whale={
        "eth": {"large_transactions": [
            {"hash": f"0x{i}", "time": f"{_recent(0)} 0{i % 10}:00:00"}
            for i in range(10)]},
    })
    out = insights.build_insights(payload, limit=100)
    hits = [i for i in out if "large-transaction surge" in i.get("headline", "").lower()]
    assert not hits


def test_whale_eth_coin_metrics_zscore_fires():
    """30 days with normal variance then a big spike on day 31 trips ETH CM
    whale-transfer z-score."""
    # Add small variance so pstdev is finite.
    series = _dated([1.0e9 + (d % 4) * 5e7 for d in range(1, 31)] + [5.0e9])
    payload = _empty_payload(whale={
        "eth": {"coin_metrics": {"transfer_value_adj_usd": series}},
    })
    out = insights.build_insights(payload, limit=100)
    hits = [i for i in out if "eth whale transfer value" in i.get("headline", "").lower()]
    assert hits, "expected ETH whale transfer z-score insight"
    for i in hits:
        assert i["tab"] == "whale"


# ---------- new stocks rules ----------

def test_stocks_news_alignment_fires():
    """Buy-biased stocks + crypto news cluster → richer combined insight."""
    stocks = [{"symbol": f"T{i}", "name": f"Co{i}", "score": 25,
               "history": _score_history()} for i in range(20)]
    payload = _empty_payload(market={
        "stocks_signals": stocks,
        "news": [
            {"title": "BTC rally hits new high", "source": "x"},
            {"title": "Bitcoin surge continues", "source": "x"},
            {"title": "Ethereum joins the rally", "source": "x"},
            {"title": "Unrelated tech story", "source": "x"},
            {"title": "Markets quiet today", "source": "x"},
        ],
    })
    out = insights.build_insights(payload, limit=100)
    hits = [i for i in out if "risk-on alignment" in i.get("headline", "").lower()]
    assert hits, "expected stocks+news risk-on alignment insight"
    for i in hits:
        assert i["tab"] == "stocks"


def test_stocks_single_name_dispersion_fires():
    """A clear outlier among 20 stocks → dispersion insight."""
    stocks = [{"symbol": f"T{i}", "name": f"Co{i}", "score": 10,
               "history": _score_history()} for i in range(19)]
    stocks.append({"symbol": "MEGA", "name": "Mega Inc", "score": 70,
                   "history": _score_history()})
    payload = _empty_payload(market={"stocks_signals": stocks})
    out = insights.build_insights(payload, limit=100)
    hits = [i for i in out if "single-name dispersion" in i.get("headline", "").lower()]
    assert hits, "expected single-name dispersion insight"


# ---------- new ETF rules ----------

def test_etf_extended_streak_fires():
    """10+ positive-flow days in a row → extended streak milestone."""
    daily = [{"date": f"2024-01-{d:02d}", "flow": 50.0, "cumulative": 50.0 * d}
             for d in range(1, 12)]  # 11 positive days
    payload = _empty_payload(btc={"daily": daily, "stats": {"all_time": 550.0}})
    out = insights.build_insights(payload, limit=100)
    hits = [i for i in out if "extended" in i.get("headline", "").lower()
            and "inflow streak" in i.get("headline", "").lower()]
    assert hits, "expected extended inflow streak insight"
    for i in hits:
        assert i["tab"] == "etf"


def test_etf_flow_with_news_cluster_fires():
    """Large flow + ≥3 BTC-keyword headlines → composite insight."""
    from datetime import datetime, timedelta
    today = datetime.utcnow().strftime("%Y-%m-%d")
    yesterday = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")
    daily = [
        {"date": yesterday, "flow": 10.0, "cumulative": 10.0},
        {"date": today, "flow": 250.0, "cumulative": 260.0},
    ]
    payload = _empty_payload(btc={"daily": daily, "stats": {"all_time": 260.0}},
                             market={"news": [
                                 {"title": "Bitcoin ETF inflows surge", "source": "x"},
                                 {"title": "BTC hits new all-time high", "source": "x"},
                                 {"title": "Bitcoin rally continues", "source": "x"},
                             ]})
    out = insights.build_insights(payload, limit=100)
    hits = [i for i in out if "alongside" in i.get("headline", "").lower()
            and "headlines on btc" in i.get("headline", "").lower()]
    assert hits, "expected ETF flow+news cluster insight"
    for i in hits:
        assert i["tab"] == "etf"


# ---------- new DeFi rules ----------

def test_defi_chain_tvl_zscore_fires():
    """TVL history with normal variance then a spike → chain TVL z-score anomaly."""
    # Add small variance so pstdev is non-zero and the resulting z-score is
    # finite. Day-31 value is well above the 30-day mean.
    series = [{"date": f"2024-01-{d:02d}", "value": 5.0e9 + (d % 5) * 1e8}
              for d in range(1, 31)]
    series.append({"date": "2024-01-31", "value": 9.0e9})
    payload = _empty_payload(market={"defi": {"tvl_history": {"Solana": series}}})
    out = insights.build_insights(payload, limit=100)
    hits = [i for i in out if "tvl" in i.get("headline", "").lower()
            and "σ vs 30d" in i.get("headline", "")]
    assert hits, "expected chain TVL z-score insight"
    for i in hits:
        assert i["tab"] == "defi"


def test_defi_bridge_flow_leader_fires():
    payload = _empty_payload(market={"defi": {
        "bridges": [
            {"name": "Across", "volume_24h_usd": 250_000_000},
            {"name": "Stargate", "volume_24h_usd": 80_000_000},
        ],
    }})
    out = insights.build_insights(payload, limit=100)
    hits = [i for i in out if "bridge flow leader" in i.get("headline", "").lower()]
    assert hits, "expected bridge-flow leader insight"
    for i in hits:
        assert i["tab"] == "defi"


def test_defi_bridge_flow_silent_under_threshold():
    payload = _empty_payload(market={"defi": {
        "bridges": [{"name": "Tiny", "volume_24h_usd": 10_000_000}],
    }})
    out = insights.build_insights(payload, limit=100)
    hits = [i for i in out if "bridge flow leader" in i.get("headline", "").lower()]
    assert not hits


# ---------- new trading rules ----------

def test_trading_fng_threshold_crossing_fires():
    """F&G crosses 75 going up → milestone insight on the trading tab."""
    payload = _empty_payload(market={"fear_greed": [
        {"value": 70, "label": "Greed"},
        {"value": 78, "label": "Extreme Greed"},
    ]})
    out = insights.build_insights(payload, limit=100)
    hits = [i for i in out if "fear & greed crossed above 75" in i.get("headline", "").lower()]
    assert hits, "expected F&G crossing-75 insight"
    for i in hits:
        assert i["tab"] == "trading"


def test_trading_fng_threshold_silent_when_no_cross():
    payload = _empty_payload(market={"fear_greed": [
        {"value": 80, "label": "Extreme Greed"},
        {"value": 82, "label": "Extreme Greed"},
    ]})
    out = insights.build_insights(payload, limit=100)
    hits = [i for i in out if "crossed above" in i.get("headline", "").lower()
            or "crossed below" in i.get("headline", "").lower()]
    # The original F&G ≥75 absolute rule is allowed to fire elsewhere; we
    # explicitly check there's no *crossing* insight when the threshold isn't
    # transitioned.
    assert not [h for h in hits if "fear & greed" in h["headline"].lower()]


def test_trading_oi_vs_price_divergence_fires():
    """OI +10% over 7d while price -10% → divergence anomaly."""
    oi = [{"date": f"2024-01-{d:02d}", "oi_usd": 10e9} for d in range(1, 8)]
    oi.append({"date": "2024-01-08", "oi_usd": 11.5e9})
    price = [{"date": f"2024-01-{d:02d}", "value": 70_000} for d in range(1, 8)]
    price.append({"date": "2024-01-08", "value": 63_000})
    payload = _empty_payload(market={"btc": {
        "open_interest_usd": oi,
        "price": price,
    }})
    out = insights.build_insights(payload, limit=100)
    hits = [i for i in out if "oi vs price divergence" in i.get("headline", "").lower()]
    assert hits, "expected OI-vs-price divergence insight"
    for i in hits:
        assert i["tab"] == "trading"


# ---------- regression: existing behaviour still holds ----------

def test_new_rules_idempotent_on_repeat_invocation():
    """Running build_insights() twice on the same payload must return the
    same insights set (same headlines, same tabs, same count). Guards against
    accidental mutation of payload-derived state."""
    payload = _empty_payload(
        market={
            "fear_greed": [{"value": 70}, {"value": 78}],
            "stocks_signals": [{"symbol": f"T{i}", "name": f"Co{i}", "score": 25,
                                "history": _score_history()}
                               for i in range(20)],
            "poc": {"btc": {"d30": {"poc": 80_000, "current": 79_000},
                            "d90": {"poc": 70_000, "current": 79_000},
                            "migration": {"delta_pct": 14.28, "direction": "UP",
                                          "magnitude": "STRONG", "between_pocs": False},
                            "naked": [],
                            "migration_series": _dated([79_000, 80_000], key="poc")}},
            "social": {"cc_news": {"fetched_at": _recent(0) + "T12:00:00+00:00",
                                   "coins": {"btc": {"net_score": 8, "article_count": 30,
                                                     "positive": 20, "negative": 12,
                                                     "neutral": 18}}}},
        },
        whale={"eth": {"large_transactions": [
            {"hash": f"0x{i}", "time": f"{_recent(0)} 0{i % 10}:00:00"}
            for i in range(35)]}},
    )
    first = insights.build_insights(payload, limit=100)
    second = insights.build_insights(payload, limit=100)
    # Guard the guard: if the fixture ever goes stale the freshness gates
    # empty this out and the idempotence assertion becomes vacuous.
    assert first, "fixture must actually produce insights for this to test anything"
    h1 = sorted((i["headline"], i["tab"]) for i in first)
    h2 = sorted((i["headline"], i["tab"]) for i in second)
    assert h1 == h2


def test_build_insights_respects_limit():
    """Verify the explicit limit argument truncates the output."""
    payload = {
        "btc": {
            "daily": [{"date": f"2024-01-{d:02d}", "flow": 100.0 if d % 2 else -50.0,
                       "cumulative": 0} for d in range(1, 31)],
            "stats": {"all_time": 1000.0},
        },
        "eth": {}, "market": {}, "signals": {},
    }
    out = insights.build_insights(payload, limit=3)
    assert len(out) <= 3


# ---------- freshness guards: a frozen feed must go quiet ----------
#
# Background: every fetcher in this repo preserves last-known-good on failure,
# so a dead upstream keeps serving its final good day while the build stamps a
# current `generated_at`. Six feeds froze between 2026-05-26 and 2026-06-09 and
# nothing noticed for over two months. The insight generators made it worse:
# they rendered those frozen rows as present-tense claims ("BTC active
# addresses at 30-day high", "price $X sits between 30d POC and 90d POC"),
# indistinguishable on the page from a real one.
#
# Each test below feeds a generator a payload that is IDENTICAL to a
# known-firing fixture except that the dates are 68 days old (the real freeze
# gap), and asserts the present-tense headline disappears. The paired
# "…_still_fires_when_fresh" coverage is the existing rule tests above — the
# guard is only useful if it discriminates.


def test_signals_stale_feed_emits_no_present_tense_insight():
    """A composite signal scored off a frozen price series must go silent.

    `signals.compute_signal` stamps `as_of` with the last close it scored;
    when CryptoCompare/CoinGecko fail the previous payload is preserved
    wholesale, so `as_of` stops advancing while the label and score still read
    as today's call.
    """
    stale = {
        "label": "STRONG BUY", "score": 65, "as_of": _stale(),
        "components": [
            {"name": "RSI(14)", "value": "78.5", "contribution": -15},
            {"name": "MACD histogram", "value": "+1.50", "contribution": 10},
        ],
        "history": [{"date": _stale(69), "score": -10},
                    {"date": _stale(68), "score": 65}],
    }
    out = insights.build_insights(_empty_payload(signals={"btc": stale}), limit=100)
    assert not [i for i in out if i.get("kind") in ("signal", "anomaly")], \
        f"stale signal feed still emitted insights: {[i['headline'] for i in out]!r}"

    # Control: the very same payload with today's dates DOES fire, so the
    # assertion above is testing the guard and not a broken fixture.
    fresh = dict(stale, as_of=_recent(0),
                 history=[{"date": _recent(1), "score": -10},
                          {"date": _recent(0), "score": 65}])
    assert insights.build_insights(_empty_payload(signals={"btc": fresh}), limit=100)


def test_signals_freshness_is_per_asset():
    """One frozen coin must not silence the coins that are still updating."""
    def _sig(day: str, score: int) -> dict:
        return {"label": "STRONG BUY" if score > 0 else "STRONG SELL",
                "score": score, "as_of": day,
                "components": [{"name": "SMA50", "contribution": score // 4}],
                "history": [{"date": day, "score": score}]}

    out = insights.build_insights(_empty_payload(signals={
        "btc": _sig(_stale(), 80),      # frozen
        "eth": _sig(_recent(0), -80),   # live
    }), limit=100)
    assets = {i["asset"] for i in out if i.get("kind") == "signal"}
    assert "eth" in assets, "the live asset should still produce a signal insight"
    assert "btc" not in assets, "the frozen asset must be suppressed"


def test_stocks_stale_feed_emits_no_present_tense_insight():
    """A frozen Yahoo `stocks_signals` array must not assert today's breadth.

    The array is preserved verbatim on a Yahoo outage, so every rule here
    ("Broad stock-market buy bias", "Strong-buy signal", "single-name
    dispersion") would keep describing a month-old tape as the current one.
    """
    stocks = [{"symbol": f"T{i}", "name": f"Co{i}", "score": 25,
               "history": _score_history(newest_days_ago=68)}
              for i in range(19)]
    stocks.append({"symbol": "MEGA", "name": "Mega Inc", "score": 70,
                   "history": _score_history(newest_days_ago=68)})
    out = insights.build_insights(_empty_payload(market={"stocks_signals": stocks}),
                                  limit=100)
    assert not [i for i in out if i.get("kind") == "stocks"], \
        f"stale stocks feed still emitted insights: {[i['headline'] for i in out]!r}"


def test_stocks_crypto_divergence_ignores_a_frozen_crypto_signal():
    """Rule 3 claims stocks and crypto DISAGREE. If the crypto side is frozen
    the 'disagreement' is just one dead feed, so the rule must stay quiet even
    though the stocks side is perfectly current.
    """
    stocks = [{"symbol": f"T{i}", "name": f"Co{i}", "score": 40,
               "history": _score_history()} for i in range(20)]
    frozen_crypto = {"btc": {"label": "SELL", "score": -40, "as_of": _stale(),
                             "components": [{"name": "SMA50", "contribution": -10}],
                             "history": [{"date": _stale(), "score": -40}]}}
    out = insights.build_insights(
        _empty_payload(market={"stocks_signals": stocks}, signals=frozen_crypto),
        limit=100,
    )
    assert not [i for i in out if "disagree" in i.get("headline", "").lower()], \
        "divergence rule fired against a frozen crypto signal"

    # Control: unfreeze crypto and the same 80-point gap does fire.
    live_crypto = {"btc": dict(frozen_crypto["btc"], as_of=_recent(0),
                               history=[{"date": _recent(0), "score": -40}])}
    live = insights.build_insights(
        _empty_payload(market={"stocks_signals": stocks}, signals=live_crypto),
        limit=100,
    )
    assert [i for i in live if "disagree" in i.get("headline", "").lower()]


def test_whale_stale_feed_emits_no_present_tense_insight():
    """A frozen blockchain.info series must not claim a 30-day extreme.

    "at 30-day high" means the 30 days ending today. Served off a series that
    stopped 68 days ago it is false, and it used to render identically to a
    real one — this generator's docstring previously waived freshness entirely.
    """
    stale_payload = _whale_payload([1000.0] * 30 + [5000.0], newest_days_ago=68)
    # Give it every BTC series so all seven BTC rules have data to chew on.
    btc = stale_payload["whale"]["btc"]
    btc["tx_count"] = _dated([100.0] * 30 + [900.0], newest_days_ago=68)
    btc["avg_tx_usd"] = _dated([1e5 + (d % 4) * 1e3 for d in range(30)] + [9e5],
                               newest_days_ago=68)
    btc["miners_revenue_usd"] = _dated([4e7 + (d % 3) * 1e6 for d in range(30)] + [9e7],
                                       newest_days_ago=68)
    out = insights.build_insights(stale_payload, limit=100)
    assert not [i for i in out if i.get("asset") == "btc"], \
        f"stale whale feed still emitted insights: {[i['headline'] for i in out]!r}"


def test_whale_eth_stale_legs_emit_no_present_tense_insight():
    """Blockchair's large-tx scan and Coin Metrics' daily series are separate
    upstreams behind the same `whale.eth` blob; both replay cached data on
    failure, so both need their own guard. "in the latest scan" is the tell.
    """
    cm_series = _dated([1.0e9 + (d % 4) * 5e7 for d in range(30)] + [5.0e9],
                       newest_days_ago=68)
    out = insights.build_insights(_empty_payload(whale={"eth": {
        "large_transactions": [{"hash": f"0x{i}", "time": f"{_stale()} 0{i % 10}:00:00"}
                               for i in range(35)],
        "coin_metrics": {"transfer_value_adj_usd": cm_series},
        "etherscan_daily": {"active_addresses":
                            _dated([100.0] * 30 + [900.0], newest_days_ago=68)},
    }}), limit=100)
    assert not [i for i in out if i.get("asset") == "eth"], \
        f"stale ETH whale legs still emitted insights: {[i['headline'] for i in out]!r}"


def test_poc_stale_feed_emits_no_present_tense_insight():
    """The worst offender: POC quotes a PRICE as current.

    `market.poc` is recomputed each build from `market[asset].price`, the
    series that froze at 2026-06-09 behind CryptoCompare's silent failure. All
    four rules are anchored to that last close — rule 2 prints "price $X sits
    between…" and rule 4 prints "% away" — so a frozen profile does not merely
    go quiet, it publishes a two-month-old price as today's.
    """
    naked5 = [{"poc": 60_000 + i * 1000, "days_ago": 30 + i,
               "distance_pct": -2.0 - i, "week_start": _stale(90 + i * 7)}
              for i in range(5)]
    asset_poc = {
        "d30": {"poc": 80_000, "current": 79_000},
        "d90": {"poc": 70_000, "current": 79_000},
        "migration": {"delta_pct": 14.28, "direction": "UP",
                      "magnitude": "STRONG", "between_pocs": True},
        "naked": naked5,
        "migration_series": _dated([79_000, 80_000], newest_days_ago=68, key="poc"),
    }
    out = insights.build_insights(
        _empty_payload(market={"poc": {"btc": asset_poc, "eth": dict(asset_poc)}}),
        limit=100,
    )
    assert not out, f"stale POC feed still emitted insights: {[i['headline'] for i in out]!r}"


def test_poc_falls_back_to_the_market_price_series_for_its_date():
    """With 10-30 aligned days `poc_migration_series` returns [] while the
    timeframes still populate, so the POC block carries no date of its own.
    The guard then reads `market[asset].price`, the series POC is computed
    from — and must still suppress when THAT is frozen.
    """
    asset_poc = {
        "d30": {"poc": 80_000, "current": 79_000},
        "d90": {"poc": 70_000, "current": 79_000},
        "migration": {"delta_pct": 14.28, "direction": "UP",
                      "magnitude": "STRONG", "between_pocs": False},
        "naked": [],
        "migration_series": [],
    }
    stale = insights.build_insights(_empty_payload(market={
        "poc": {"btc": asset_poc},
        "btc": {"price": _dated([78_000, 79_000], newest_days_ago=68)},
    }), limit=100)
    assert not [i for i in stale if "value migrating" in i.get("headline", "").lower()]

    fresh = insights.build_insights(_empty_payload(market={
        "poc": {"btc": asset_poc},
        "btc": {"price": _dated([78_000, 79_000])},
    }), limit=100)
    assert [i for i in fresh if "value migrating" in i.get("headline", "").lower()], \
        "the price-series fallback should let a live feed through"


def test_social_stale_legs_emit_no_present_tense_insight():
    """`fetch_social` stamps the OUTER dict with a build-time `fetched_at`
    that is always "now"; only the per-leg timestamps survive stale-keep
    (`{**prev, "stale": True}`). This asserts the guard reads the leg, not the
    wrapper — a fresh outer timestamp must not rescue three frozen legs.
    """
    out = insights.build_insights(_empty_payload(market={"social": {
        "fetched_at": _recent(0) + "T19:06:48+00:00",   # build time — a lie
        "cc_news": {"fetched_at": _stale() + "T12:00:00+00:00", "stale": True,
                    "coins": {"btc": {"net_score": 14, "article_count": 40,
                                      "positive": 27, "negative": 13, "neutral": 10}}},
        "reddit": {"fetched_at": _stale() + "T12:00:00+00:00", "stale": True,
                   "subreddits": {
                       "CryptoCurrency": {"subscribers": 1_000_000, "active_users": 5_000,
                                          "label": "All crypto"},
                       "Bitcoin": {"subscribers": 4_000_000, "active_users": 12_000,
                                   "label": "BTC"},
                       "ethereum": {"subscribers": 1_500_000, "active_users": 4_500,
                                    "label": "ETH"},
                       "Chainlink": {"subscribers": 100_000, "active_users": 8_000,
                                     "label": "LINK"},
                   }},
        "santiment": {"fetched_at": _stale() + "T00:05:00+00:00", "stale": True,
                      "coins": {"btc": {
                          "daily_active_addresses_delta_pct": 35.0,
                          "daily_active_addresses_latest": 1_200_000,
                          "daily_active_addresses": _dated([900_000, 1_200_000],
                                                           newest_days_ago=68)}}},
    }}), limit=100)
    assert not out, f"stale social legs still emitted insights: {[i['headline'] for i in out]!r}"


def test_social_santiment_series_date_beats_a_fresh_fetched_at():
    """Santiment is stale-kept 23 hours a day BY DESIGN, so `fetched_at` alone
    is a weak signal. When the DAA series itself is frozen the rule must stay
    quiet even though the leg was re-fetched today — a re-fetch that returns
    the same old points is precisely the failure this guards.
    """
    out = insights.build_insights(_empty_payload(market={"social": {
        "santiment": {"fetched_at": _recent(0) + "T00:05:00+00:00", "coins": {
            "btc": {"daily_active_addresses_delta_pct": 35.0,
                    "daily_active_addresses_latest": 1_200_000,
                    "daily_active_addresses": _dated([900_000, 1_200_000],
                                                     newest_days_ago=68)},
        }},
    }}), limit=100)
    assert not [i for i in out if "on-chain attention" in i.get("headline", "").lower()], \
        "a fresh fetched_at rescued a frozen DAA series"


def test_market_generator_whale_rules_use_the_whale_budget_not_the_etf_one():
    """`_market_insights` carries two BTC on-chain σ rules that render on the
    Whale tab off the same blockchain.info series `_whale_insights` reads.

    They already had a guard, but at ``max_age_days=14`` — the ETF number
    copied onto a daily on-chain feed. A 10-day-old series therefore still
    published "+2.4σ vs 30d mean" as current, and the two generators
    disagreed about whether the very same data was fresh. This asserts one
    feed now gets one answer.
    """
    def _payload(age: int) -> dict:
        spiky = [1.0e9 + (i % 4) * 5e7 for i in range(30)] + [9.0e9]
        return _empty_payload(whale={"btc": {
            "tx_volume_usd": _dated(spiky, newest_days_ago=age),
            "active_addresses": _dated(spiky, newest_days_ago=age),
        }})

    def _sigma_hits(age):
        return [i for i in insights.build_insights(_payload(age), limit=100)
                if "σ vs 30d" in i.get("headline", "")]

    assert _sigma_hits(0), "a live series must still produce the σ anomalies"
    # 10 days is inside the old 14-day budget and well outside the whale one.
    assert not _sigma_hits(10), \
        "10-day-old on-chain series still emitted a present-tense σ anomaly"


def test_freshness_budgets_are_documented_and_sane():
    """Every budget must exist, be a positive int, and stay tight enough to
    catch a multi-week freeze. 14 days is the ETF budget (a weekly-ish CSV);
    nothing on a daily/hourly cadence should ever need more.
    """
    budgets = insights.FRESHNESS_MAX_AGE_DAYS
    expected = {"signals", "stocks", "whale_btc", "whale_eth_daily",
                "whale_eth_scan", "poc", "social_news", "social_reddit",
                "social_santiment"}
    assert expected <= set(budgets)
    for name, days in budgets.items():
        assert isinstance(days, int) and 1 <= days <= 14, f"{name}={days!r}"


# ---------- rolling history: sentiment flip + volume σ ----------
#
# Both rules read ``data/insights_history.json``. The conftest fixture
# redirects ``_HISTORY_PATH`` to a tmp file per test, so we can seed prior
# days by writing JSON there directly.

import json
from datetime import datetime, timedelta


def _seed_history(rows):
    """Write rows to the (tmp) insights history path."""
    insights._HISTORY_PATH.write_text(json.dumps({"history": rows}))


def _yday_iso(days_ago: int = 1) -> str:
    return (datetime.utcnow() - timedelta(days=days_ago)).strftime("%Y-%m-%d")


def test_ainews_sentiment_flip_positive_to_negative_fires():
    """POSITIVE yesterday → NEGATIVE today emits a flip anomaly with severity
    'bad' (today's labelled mood is the negative side)."""
    _seed_history([{
        "date": _yday_iso(1),
        "ai_news_sentiment_label": "POSITIVE",
        "ai_news_total": 30,
    }])
    summary = {"positive": 5, "negative": 28, "neutral": 8, "total": 41,
               "net_score": -23, "sentiment_label": "NEGATIVE"}
    payload = _ainews_payload(summary=summary)
    out = insights.build_insights(payload, limit=100)
    hits = [i for i in out if "sentiment flipped POSITIVE → NEGATIVE" in i.get("headline", "")]
    assert hits, f"expected flip insight, got {[i['headline'] for i in out]!r}"
    assert hits[0]["tab"] == "ainews"
    assert hits[0]["severity"] == "bad"


def test_ainews_sentiment_flip_silent_when_no_history():
    """First run with no prior days → flip rule stays quiet."""
    summary = {"positive": 30, "negative": 5, "neutral": 7, "total": 42,
               "net_score": 25, "sentiment_label": "POSITIVE"}
    payload = _ainews_payload(summary=summary)
    out = insights.build_insights(payload, limit=100)
    hits = [i for i in out if "sentiment flipped" in i.get("headline", "")]
    assert not hits, f"flip rule fired without prior history: {hits!r}"


def test_ainews_sentiment_flip_silent_on_neutral_transition():
    """Yesterday NEUTRAL (no label) → today POSITIVE must not fire. We only
    care about POSITIVE ↔ NEGATIVE transitions; NEUTRAL → labelled is a
    cadence change, not a sentiment flip."""
    _seed_history([{
        "date": _yday_iso(1),
        "ai_news_sentiment_label": None,
        "ai_news_total": 25,
    }])
    summary = {"positive": 30, "negative": 5, "neutral": 7, "total": 42,
               "net_score": 25, "sentiment_label": "POSITIVE"}
    payload = _ainews_payload(summary=summary)
    out = insights.build_insights(payload, limit=100)
    hits = [i for i in out if "sentiment flipped" in i.get("headline", "")]
    assert not hits


def test_ainews_volume_sigma_surge_fires_above_2sigma():
    """Seed 7 prior days with totals tightly around 20; today's 60 is far
    enough above mean+2σ to fire and also clears the mean+5 floor."""
    rows = []
    base_totals = [18, 22, 20, 19, 21, 20, 23]
    for i, t in enumerate(base_totals, start=1):
        rows.append({
            "date": _yday_iso(8 - i),
            "ai_news_sentiment_label": "POSITIVE",
            "ai_news_total": t,
        })
    _seed_history(rows)
    summary = {"positive": 30, "negative": 12, "neutral": 18, "total": 60,
               "net_score": 18, "sentiment_label": "POSITIVE"}
    payload = _ainews_payload(summary=summary)
    out = insights.build_insights(payload, limit=100)
    hits = [i for i in out if "AI news volume surge" in i.get("headline", "")]
    assert hits, f"expected volume σ insight, got {[i['headline'] for i in out]!r}"
    assert hits[0]["tab"] == "ainews"


def test_ainews_volume_sigma_silent_when_within_band():
    """Today's total within the 7-day natural band → no σ insight."""
    rows = []
    base_totals = [40, 42, 38, 41, 39, 43, 40]
    for i, t in enumerate(base_totals, start=1):
        rows.append({
            "date": _yday_iso(8 - i),
            "ai_news_sentiment_label": "POSITIVE",
            "ai_news_total": t,
        })
    _seed_history(rows)
    summary = {"positive": 25, "negative": 10, "neutral": 7, "total": 41,
               "net_score": 15, "sentiment_label": "POSITIVE"}
    payload = _ainews_payload(summary=summary)
    out = insights.build_insights(payload, limit=100)
    hits = [i for i in out if "AI news volume surge" in i.get("headline", "")]
    assert not hits, f"σ rule fired inside natural band: {hits!r}"


def test_ainews_volume_sigma_silent_when_history_too_short():
    """Need ≥4 prior days for stats. With 3 days, the σ rule stays quiet."""
    rows = []
    for i, t in enumerate([18, 20, 19], start=1):
        rows.append({
            "date": _yday_iso(4 - i),
            "ai_news_sentiment_label": "POSITIVE",
            "ai_news_total": t,
        })
    _seed_history(rows)
    summary = {"positive": 30, "negative": 12, "neutral": 18, "total": 60,
               "net_score": 18, "sentiment_label": "POSITIVE"}
    payload = _ainews_payload(summary=summary)
    out = insights.build_insights(payload, limit=100)
    hits = [i for i in out if "AI news volume surge" in i.get("headline", "")]
    assert not hits


def test_build_insights_persists_today_snapshot():
    """After build_insights, today's row must land in the history file so
    the next build can compare day-over-day."""
    summary = {"positive": 20, "negative": 10, "neutral": 14, "total": 44,
               "net_score": 10, "sentiment_label": "POSITIVE"}
    payload = _ainews_payload(summary=summary)
    insights.build_insights(payload, limit=100)
    rows = insights._load_insights_history()
    assert rows, "expected history file populated after build_insights"
    today = datetime.utcnow().strftime("%Y-%m-%d")
    assert rows[-1]["date"] == today
    assert rows[-1]["ai_news_total"] == 44
    assert rows[-1]["ai_news_sentiment_label"] == "POSITIVE"


def test_build_insights_history_trims_to_max_days():
    """Seed 20 old rows; after a build the file should hold ≤14 rows
    (_HISTORY_MAX_DAYS), keeping the most recent ones plus today."""
    rows = [
        {"date": (datetime.utcnow() - timedelta(days=d)).strftime("%Y-%m-%d"),
         "ai_news_sentiment_label": "POSITIVE",
         "ai_news_total": 20 + d}
        for d in range(2, 22)  # 20 days ago through 2 days ago
    ]
    _seed_history(rows)
    summary = {"positive": 20, "negative": 10, "neutral": 14, "total": 44,
               "net_score": 10, "sentiment_label": "POSITIVE"}
    insights.build_insights(_ainews_payload(summary=summary), limit=100)
    final = insights._load_insights_history()
    assert len(final) <= insights._HISTORY_MAX_DAYS
    # Oldest rows must have been dropped first.
    dates = [r["date"] for r in final]
    assert dates == sorted(dates)
