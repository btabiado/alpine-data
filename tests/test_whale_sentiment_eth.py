"""Tests for ``fetch_market.compute_whale_sentiment_eth`` — the ETH parallel
of the BTC whale sentiment composite. Algorithm: z-score each component over
a 30-day baseline, scale to ±25 per component, sum and clip to ±100.
"""
from __future__ import annotations


import fetch_market as fm


def _series(values: list[float]) -> list[dict]:
    """Build a Coin Metrics-style ``[{date, value}]`` series. The date is a
    fake monotonically-increasing string; the function only reads ``value``."""
    return [{"date": f"2026-01-{i+1:02d}", "value": float(v)} for i, v in enumerate(values)]


def _baseline(mean: float, n: int = 30) -> list[float]:
    return [mean] * n


def _whale_payload(
    aa: list[float] | None,
    tx: list[float] | None,
    vol: list[float] | None = None,
    blocks_7d: list[float] | None = None,
) -> dict:
    cm: dict = {}
    if aa is not None:
        cm["AdrActCnt"] = _series(aa)
    if tx is not None:
        cm["TxCnt"] = _series(tx)
    if vol is not None:
        cm["TxTfrValAdjUSD"] = _series(vol)
    eth: dict = {"coin_metrics": cm}
    if blocks_7d is not None:
        eth["etherscan_daily"] = {"series": _series(blocks_7d)}
    return {"eth": eth, "fetched_at": "2026-05-16T00:00:00+00:00"}


def test_bullish_input_scores_positive():
    """All proxies running 2σ above the 30d mean → score > 0 and label is
    not BEARISH/DUMP. Bullish input: 30 days of flat baseline followed by
    a today-value pushed well above the mean."""
    bull_aa = _baseline(1_000_000) + [1_200_000]   # +20%
    bull_tx = _baseline(1_200_000) + [1_400_000]   # +17%
    bull_vol = _baseline(5_000_000_000) + [7_000_000_000]  # +40%
    # Blocks above the 7,200 target = saturating slots
    bull_blocks = [7400, 7400, 7400, 7400, 7400, 7400, 7400]
    payload = _whale_payload(bull_aa, bull_tx, bull_vol, bull_blocks)

    result = fm.compute_whale_sentiment_eth(payload)
    assert result is not None
    assert result["available"] is True
    assert result["score"] > 0, f"bullish input should score positive, got {result['score']}"
    assert "BEARISH" not in result["label"]
    assert "DUMP" not in result["label"]
    assert isinstance(result["components"], list) and result["components"]
    # Every component contribution should be > 0 in a uniformly bullish setup.
    assert all(c["contribution"] > 0 for c in result["components"])


def test_bearish_input_scores_negative():
    """All proxies running well below the 30d mean → score < 0."""
    bear_aa = _baseline(1_000_000) + [800_000]   # -20%
    bear_tx = _baseline(1_200_000) + [1_000_000] # -17%
    bear_vol = _baseline(5_000_000_000) + [3_000_000_000]
    # Blocks well below the 7,200 target = slots underused
    bear_blocks = [7000, 7000, 7000, 7000, 7000, 7000, 7000]
    payload = _whale_payload(bear_aa, bear_tx, bear_vol, bear_blocks)

    result = fm.compute_whale_sentiment_eth(payload)
    assert result is not None
    assert result["available"] is True
    assert result["score"] < 0, f"bearish input should score negative, got {result['score']}"
    assert all(c["contribution"] < 0 for c in result["components"])


def test_empty_input_returns_empty_state():
    """No ETH data at all → defined empty-state marker, not an exception."""
    result = fm.compute_whale_sentiment_eth({})
    assert isinstance(result, dict)
    assert result.get("available") is False
    assert result.get("components") == []
    assert "label" in result and "score" in result


def test_missing_keys_no_exception():
    """Defensive: weird/partial inputs must not raise."""
    # None
    assert fm.compute_whale_sentiment_eth(None) is None  # type: ignore[arg-type]
    # Non-dict
    assert fm.compute_whale_sentiment_eth("nope") is None  # type: ignore[arg-type]
    # Empty eth subtree
    r = fm.compute_whale_sentiment_eth({"eth": {}})
    assert isinstance(r, dict) and r.get("available") is False
    # eth.coin_metrics present but every series too short
    short = {"eth": {"coin_metrics": {"AdrActCnt": _series([1, 2, 3])}}}
    r = fm.compute_whale_sentiment_eth(short)
    assert isinstance(r, dict) and r.get("available") is False
    # Mixed types — etherscan_daily wrong shape
    weird = {"eth": {"coin_metrics": {}, "etherscan_daily": "garbage"}}
    r = fm.compute_whale_sentiment_eth(weird)
    assert isinstance(r, dict) and r.get("available") is False


def test_score_clipped_to_plus_minus_100():
    """Extreme inputs must still respect the ±100 cap."""
    insane_up = _baseline(100) + [1_000_000_000]
    payload = _whale_payload(insane_up, insane_up, insane_up, [9999] * 7)
    result = fm.compute_whale_sentiment_eth(payload)
    assert result is not None and -100 <= result["score"] <= 100


def test_output_shape_matches_btc_renderer_contract():
    """The ETH renderer reuses the BTC card's pattern, so the dict must
    have the same keys: score, label, components[{name,value,contribution,
    explanation}], as_of, disclaimer."""
    bull = _baseline(1_000_000) + [1_100_000]
    payload = _whale_payload(bull, bull, bull, [7300] * 7)
    result = fm.compute_whale_sentiment_eth(payload)
    assert result is not None
    for key in ("score", "label", "components", "as_of", "disclaimer"):
        assert key in result, f"missing key {key!r} — would break the JS renderer"
    for comp in result["components"]:
        for k in ("name", "value", "contribution", "explanation"):
            assert k in comp, f"component missing key {k!r}"
