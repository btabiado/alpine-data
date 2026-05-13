"""Tests for the rule-based insights engine and its per-tab tagging."""
from __future__ import annotations

import insights


# ---------- per-tab tagging ----------

def test_etf_insights_tagged_etf():
    """Anything coming out of _etf_insights should end up on the ETF tab."""
    payload = {
        "btc": {
            "daily": [
                {"date": "2024-01-10", "flow": 100.0, "cumulative": 100.0},
                {"date": "2024-01-11", "flow": -50.0, "cumulative": 50.0},
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
                "components": [{"name": "RSI", "contribution": 15},
                               {"name": "MACD", "contribution": 10},
                               {"name": "Funding", "contribution": 10}],
                "history": [{"score": -10}, {"score": 65}],
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

    assert f({"headline": "DXY +1.2% today — typically inverse to risk assets including crypto"}) == "markets"
    assert f({"headline": "10Y Treasury yield crossed above 5.0% (5.02%)"}) == "markets"
    assert f({"headline": "Gold at 30-day high ($2,420.50/oz)"}) == "markets"
    assert f({"headline": "S&P 500 -2.4% today — risk-off may pressure crypto"}) == "markets"
    assert f({"headline": "📰 CoinDesk: Some headline goes here"}) == "markets"
    assert f({"headline": "ZANO (Zano) is trending #1 on CoinGecko"}) == "markets"
    assert f({"headline": "BTC price divergence: CoinGecko $43,200 vs CryptoCompare $43,512"}) == "markets"


def test_market_insight_tab_classifier_falls_back_to_markets():
    f = insights._market_insight_tab
    assert f({"headline": "something nobody recognises"}) == "markets"
    assert f({"headline": ""}) == "markets"


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


# ---------- regression: existing behaviour still holds ----------

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
