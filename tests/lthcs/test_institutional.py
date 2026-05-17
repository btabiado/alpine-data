"""Tests for lthcs.pillars.institutional.

No live network -- the pillar's compute path takes momentum as a scalar
parameter, so tests don't need to touch Yahoo at all.
"""

from __future__ import annotations

from typing import Dict, Optional

import pytest

from lthcs.pillars import institutional


# --- Fixtures / helpers -----------------------------------------------------


def _peer_momentums_universe(
    focal_ticker: str, focal_mom: Optional[float]
) -> Dict[str, Optional[float]]:
    """Build a peer-momentum map: focal ticker plus 9 peers spanning -20% to +40%."""
    peers: Dict[str, Optional[float]] = {
        "P1": -0.20,
        "P2": -0.10,
        "P3": -0.02,
        "P4": 0.00,
        "P5": 0.05,
        "P6": 0.10,
        "P7": 0.18,
        "P8": 0.25,
        "P9": 0.40,
    }
    peers[focal_ticker] = focal_mom
    return peers


# --- compute_momentum_subscore ---------------------------------------------


def test_momentum_subscore_basic_percentile() -> None:
    """Focal momentum 0.12 ranks between P6 (0.10) and P7 (0.18) among 9 peers."""
    peers = _peer_momentums_universe("AAPL", 0.12)
    score = institutional.compute_momentum_subscore("AAPL", 0.12, peers)
    # 9 peers excluding self; 6 below (P1..P6), 0 equal, 3 above -> 6/9 * 100.
    assert score == pytest.approx(66.6667, abs=1e-3)


def test_momentum_subscore_none_returns_neutral() -> None:
    """A focal momentum of None falls back to the neutral 50."""
    peers = _peer_momentums_universe("AAPL", None)
    assert institutional.compute_momentum_subscore("AAPL", None, peers) == 50.0


def test_momentum_subscore_ignores_none_peers() -> None:
    """None entries in the peer map must be filtered before ranking."""
    peers: Dict[str, Optional[float]] = {
        "AAPL": 0.10,
        "P1": None,
        "P2": None,
        "P3": 0.05,
        "P4": 0.08,
    }
    score = institutional.compute_momentum_subscore("AAPL", 0.10, peers)
    # Cleaned peers (excl self, excl None): [0.05, 0.08]. 0.10 ranks above
    # both -> 100.
    assert score == pytest.approx(100.0)


def test_momentum_subscore_excludes_self_from_peer_distribution() -> None:
    """Focal ticker's own momentum must not appear in the comparison set."""
    peers = {
        "AAPL": 0.10,  # focal -- should be excluded
        "P1": 0.10,
        "P2": 0.10,
        "P3": 0.10,
    }
    score = institutional.compute_momentum_subscore("AAPL", 0.10, peers)
    # 3 peers, all equal to 0.10: 0 below, 3 equal -> 50.
    assert score == pytest.approx(50.0)


def test_momentum_subscore_empty_peers_returns_neutral() -> None:
    """Empty peer distribution -> neutral 50 (from percentile_rank)."""
    assert institutional.compute_momentum_subscore("AAPL", 0.05, {}) == 50.0


def test_momentum_subscore_top_of_pack() -> None:
    """Focal momentum well above every peer -> 100."""
    peers = _peer_momentums_universe("AAPL", 1.00)
    score = institutional.compute_momentum_subscore("AAPL", 1.00, peers)
    assert score == pytest.approx(100.0)


def test_momentum_subscore_bottom_of_pack() -> None:
    """Focal momentum well below every peer -> 0."""
    peers = _peer_momentums_universe("AAPL", -1.00)
    score = institutional.compute_momentum_subscore("AAPL", -1.00, peers)
    assert score == pytest.approx(0.0)


# --- compute_institutional: V1 path (no 13F) -------------------------------


def test_compute_institutional_momentum_present_inst_none() -> None:
    """V1 default: momentum present, 13F None -> sub_score == momentum_subscore."""
    peers = _peer_momentums_universe("AAPL", 0.12)
    result = institutional.compute_institutional("AAPL", 0.12, peers)

    assert result["ticker"] == "AAPL"
    assert result["weights"] == {"momentum": 0.70, "inst_holdings": 0.30}
    # Renormalization: momentum carries the full 100%.
    assert result["effective_weights"] == {"momentum": 1.0, "inst_holdings": 0.0}
    assert result["data_quality"] == {
        "has_momentum": True,
        "has_inst_holdings": False,
    }

    comps = result["components"]
    assert comps["momentum_pct_90d"] == pytest.approx(0.12)
    assert comps["inst_holdings_change_qoq"] is None
    # 13F shown as the neutral placeholder, but its effective weight is 0.
    assert comps["inst_holdings_subscore"] == 50.0
    assert comps["momentum_subscore"] == pytest.approx(66.6667, abs=1e-3)

    expected = round(comps["momentum_subscore"], 1)
    assert result["sub_score"] == expected
    # Sanity: sub_score equals momentum_subscore (rounded), NOT
    # the diluted 0.70 * mom + 0.30 * 50.
    diluted = round(0.70 * comps["momentum_subscore"] + 0.30 * 50.0, 1)
    assert result["sub_score"] != diluted


def test_compute_institutional_momentum_none_inst_none() -> None:
    """Both signals missing -> sub_score is the neutral 50.0."""
    result = institutional.compute_institutional("AAPL", None, {})

    assert result["sub_score"] == 50.0
    assert result["data_quality"] == {
        "has_momentum": False,
        "has_inst_holdings": False,
    }
    assert result["components"]["momentum_pct_90d"] is None
    assert result["components"]["momentum_subscore"] == 50.0
    assert result["components"]["inst_holdings_subscore"] == 50.0
    assert result["effective_weights"] == {"momentum": 1.0, "inst_holdings": 0.0}


def test_compute_institutional_momentum_none_with_peers() -> None:
    """Focal momentum None even when peers exist -> momentum subscore is 50."""
    peers = _peer_momentums_universe("AAPL", None)
    result = institutional.compute_institutional("AAPL", None, peers)

    assert result["components"]["momentum_subscore"] == 50.0
    assert result["sub_score"] == 50.0
    assert result["data_quality"]["has_momentum"] is False


# --- compute_institutional: Phase-2 path (13F present) ---------------------


def test_compute_institutional_both_components_present() -> None:
    """Both momentum and 13F present -> 70/30 weighted sum (not renormalized)."""
    peers = _peer_momentums_universe("AAPL", 0.12)
    # 13F change of +0.025 maps via bounded_linear(-0.05, 0.05) to 75.
    result = institutional.compute_institutional(
        "AAPL", 0.12, peers, inst_holdings_change_qoq=0.025
    )

    assert result["effective_weights"] == {"momentum": 0.70, "inst_holdings": 0.30}
    assert result["data_quality"] == {
        "has_momentum": True,
        "has_inst_holdings": True,
    }

    comps = result["components"]
    assert comps["inst_holdings_change_qoq"] == pytest.approx(0.025)
    assert comps["inst_holdings_subscore"] == pytest.approx(75.0)
    assert comps["momentum_subscore"] == pytest.approx(66.6667, abs=1e-3)

    expected = round(
        0.70 * comps["momentum_subscore"] + 0.30 * comps["inst_holdings_subscore"],
        1,
    )
    assert result["sub_score"] == expected


def test_compute_institutional_inst_present_momentum_none() -> None:
    """13F present, momentum missing -> momentum falls back to 50, 70/30 applies."""
    result = institutional.compute_institutional(
        "AAPL", None, {}, inst_holdings_change_qoq=0.05
    )

    assert result["effective_weights"] == {"momentum": 0.70, "inst_holdings": 0.30}
    assert result["data_quality"] == {
        "has_momentum": False,
        "has_inst_holdings": True,
    }
    assert result["components"]["momentum_subscore"] == 50.0
    # +0.05 hits the upper bound -> 100.
    assert result["components"]["inst_holdings_subscore"] == pytest.approx(100.0)
    # 0.70 * 50 + 0.30 * 100 = 65.0.
    assert result["sub_score"] == pytest.approx(65.0)


def test_compute_institutional_inst_zero_change_is_neutral_50() -> None:
    """A 13F QoQ change of exactly 0 maps to the midpoint 50."""
    peers = _peer_momentums_universe("AAPL", 0.0)
    result = institutional.compute_institutional(
        "AAPL", 0.0, peers, inst_holdings_change_qoq=0.0
    )
    assert result["components"]["inst_holdings_subscore"] == pytest.approx(50.0)


def test_compute_institutional_inst_negative_change_below_50() -> None:
    """Negative 13F change drags the 13F subscore below 50."""
    peers = _peer_momentums_universe("AAPL", 0.0)
    result = institutional.compute_institutional(
        "AAPL", 0.0, peers, inst_holdings_change_qoq=-0.025
    )
    # bounded_linear(-0.025, -0.05, 0.05) -> 25.
    assert result["components"]["inst_holdings_subscore"] == pytest.approx(25.0)


# --- Output shape / contract checks ----------------------------------------


def test_compute_institutional_sub_score_rounded_to_one_decimal() -> None:
    """Spec requires the sub_score be rounded to 1 decimal place."""
    peers = _peer_momentums_universe("AAPL", 0.07)
    result = institutional.compute_institutional("AAPL", 0.07, peers)
    assert isinstance(result["sub_score"], float)
    assert result["sub_score"] == round(result["sub_score"], 1)


def test_compute_institutional_returns_expected_keys() -> None:
    """Full output dict has the keys the downstream aggregator expects."""
    peers = _peer_momentums_universe("AAPL", 0.10)
    result = institutional.compute_institutional("AAPL", 0.10, peers)

    assert set(result.keys()) == {
        "ticker",
        "sub_score",
        "components",
        "weights",
        "effective_weights",
        "data_quality",
    }
    assert set(result["components"].keys()) == {
        "momentum_pct_90d",
        "momentum_subscore",
        "inst_holdings_change_qoq",
        "inst_holdings_subscore",
    }
    assert set(result["weights"].keys()) == {"momentum", "inst_holdings"}
    assert set(result["effective_weights"].keys()) == {"momentum", "inst_holdings"}
    assert set(result["data_quality"].keys()) == {
        "has_momentum",
        "has_inst_holdings",
    }


def test_compute_institutional_effective_weights_sum_to_one() -> None:
    """In every path, effective weights must sum to 1.0."""
    peers = _peer_momentums_universe("AAPL", 0.10)

    # 13F missing path.
    r1 = institutional.compute_institutional("AAPL", 0.10, peers)
    ew1 = r1["effective_weights"]
    assert ew1["momentum"] + ew1["inst_holdings"] == pytest.approx(1.0)

    # 13F present path.
    r2 = institutional.compute_institutional(
        "AAPL", 0.10, peers, inst_holdings_change_qoq=0.01
    )
    ew2 = r2["effective_weights"]
    assert ew2["momentum"] + ew2["inst_holdings"] == pytest.approx(1.0)
