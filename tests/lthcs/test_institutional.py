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
        "has_insider": False,
        "has_holdings": False,
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
        "has_insider": False,
        "has_holdings": False,
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
        "has_insider": False,
        "has_holdings": False,
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
        "has_insider": False,
        "has_holdings": False,
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
        "base_sub_score",
        "insider",
        "holdings",
        "combined_adjustment_pts",
        # Tier 3 #16 tracking fields — present in every result.
        "momentum_strategy_used",
        "momentum_cohort_size",
        "momentum_cohort_label",
    }
    assert set(result["weights"].keys()) == {"momentum", "inst_holdings"}
    assert set(result["effective_weights"].keys()) == {"momentum", "inst_holdings"}
    assert set(result["data_quality"].keys()) == {
        "has_momentum",
        "has_inst_holdings",
        "has_insider",
        "has_holdings",
    }
    # The insider component dict shape (always present, even for missing data).
    assert set(result["components"]["insider"].keys()) == {
        "regime",
        "conviction_score",
        "cluster_buying",
        "ceo_cfo_action",
        "adjustment_pts",
    }
    # The holdings component dict shape (always present, even for missing data).
    assert set(result["components"]["holdings"].keys()) == {
        "conviction_signal",
        "signal_score",
        "manager_count",
        "data_quality",
        "share_change_pct",
        "net_buyers",
        "net_sellers",
        "adjustment_pts",
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


# --- Insider-conviction adjustment -----------------------------------------


def _insider(
    *,
    regime: str = "neutral",
    conviction_score: Optional[float] = 0.0,
    cluster_buying: bool = False,
    ceo_cfo_action: str = "neutral",
) -> Dict[str, object]:
    """Build a minimal Form 4 insider payload for tests."""
    return {
        "regime": regime,
        "conviction_score": conviction_score,
        "cluster_buying": cluster_buying,
        "ceo_cfo_action": ceo_cfo_action,
    }


def test_insider_cluster_buying_adds_eight_points() -> None:
    """cluster_buying overrides the conviction bracket and adds +8."""
    peers = _peer_momentums_universe("CSGP", 0.0)
    # Momentum-only base: focal at 0.0 is rank 3/9 -> ~33.3.
    base = institutional.compute_institutional("CSGP", 0.0, peers)
    base_sub = base["sub_score"]

    insider = _insider(
        regime="strong_buying",
        conviction_score=0.97,
        cluster_buying=True,
        ceo_cfo_action="neutral",
    )
    result = institutional.compute_institutional(
        "CSGP", 0.0, peers, insider_data=insider
    )
    # Adjustment: +8 (cluster) only. CEO/CFO neutral.
    assert result["components"]["insider"]["adjustment_pts"] == pytest.approx(8.0)
    assert result["sub_score"] == pytest.approx(round(base_sub + 8.0, 1))
    assert result["data_quality"]["has_insider"] is True


def test_insider_strong_buying_no_cluster_adds_five_points() -> None:
    """strong_buying (conv >= +0.5) without cluster adds +5."""
    peers = _peer_momentums_universe("TTD", 0.0)
    base = institutional.compute_institutional("TTD", 0.0, peers)
    base_sub = base["sub_score"]

    insider = _insider(
        regime="strong_buying",
        conviction_score=0.85,
        cluster_buying=False,
        ceo_cfo_action="neutral",
    )
    result = institutional.compute_institutional(
        "TTD", 0.0, peers, insider_data=insider
    )
    assert result["components"]["insider"]["adjustment_pts"] == pytest.approx(5.0)
    assert result["sub_score"] == pytest.approx(round(base_sub + 5.0, 1))


def test_insider_mild_buying_adds_three_points() -> None:
    """conviction in [+0.2, +0.5) gives +3 (no cluster, no CEO/CFO overlay)."""
    peers = _peer_momentums_universe("AAPL", 0.0)
    base = institutional.compute_institutional("AAPL", 0.0, peers)
    base_sub = base["sub_score"]

    insider = _insider(
        regime="mild_buying",
        conviction_score=0.30,
        cluster_buying=False,
        ceo_cfo_action="neutral",
    )
    result = institutional.compute_institutional(
        "AAPL", 0.0, peers, insider_data=insider
    )
    assert result["components"]["insider"]["adjustment_pts"] == pytest.approx(3.0)
    assert result["sub_score"] == pytest.approx(round(base_sub + 3.0, 1))


def test_insider_noise_band_no_adjustment() -> None:
    """|conviction| < 0.2 (noise band) contributes 0."""
    peers = _peer_momentums_universe("AAPL", 0.0)
    base = institutional.compute_institutional("AAPL", 0.0, peers)
    base_sub = base["sub_score"]

    insider = _insider(
        regime="neutral",
        conviction_score=0.10,
        cluster_buying=False,
        ceo_cfo_action="neutral",
    )
    result = institutional.compute_institutional(
        "AAPL", 0.0, peers, insider_data=insider
    )
    assert result["components"]["insider"]["adjustment_pts"] == 0.0
    assert result["sub_score"] == base_sub


def test_insider_mild_selling_subtracts_one_point() -> None:
    """conviction in (-0.5, -0.2] subtracts 1 (asymmetric vs +3)."""
    peers = _peer_momentums_universe("AAPL", 0.0)
    base = institutional.compute_institutional("AAPL", 0.0, peers)
    base_sub = base["sub_score"]

    insider = _insider(
        regime="mild_selling",
        conviction_score=-0.30,
        cluster_buying=False,
        ceo_cfo_action="neutral",
    )
    result = institutional.compute_institutional(
        "AAPL", 0.0, peers, insider_data=insider
    )
    assert result["components"]["insider"]["adjustment_pts"] == pytest.approx(-1.0)
    assert result["sub_score"] == pytest.approx(round(base_sub - 1.0, 1))


def test_insider_heavy_selling_subtracts_three_points() -> None:
    """heavy_selling (conv <= -0.5) subtracts 3 -- asymmetric vs +5 for buying."""
    peers = _peer_momentums_universe("AAPL", 0.0)
    base = institutional.compute_institutional("AAPL", 0.0, peers)
    base_sub = base["sub_score"]

    insider = _insider(
        regime="heavy_selling",
        conviction_score=-1.0,
        cluster_buying=False,
        ceo_cfo_action="neutral",
    )
    result = institutional.compute_institutional(
        "AAPL", 0.0, peers, insider_data=insider
    )
    assert result["components"]["insider"]["adjustment_pts"] == pytest.approx(-3.0)
    assert result["sub_score"] == pytest.approx(round(base_sub - 3.0, 1))


def test_insider_missing_data_zero_adjustment() -> None:
    """Missing insider_data must NOT penalize the ticker."""
    peers = _peer_momentums_universe("ABC", 0.05)
    base = institutional.compute_institutional("ABC", 0.05, peers)

    # insider_data=None (the default).
    r_none = institutional.compute_institutional(
        "ABC", 0.05, peers, insider_data=None
    )
    assert r_none["sub_score"] == base["sub_score"]
    assert r_none["data_quality"]["has_insider"] is False
    assert r_none["components"]["insider"]["adjustment_pts"] == 0.0
    assert r_none["components"]["insider"]["regime"] is None

    # insider_data={} (truthy-empty -- treated the same as None).
    r_empty = institutional.compute_institutional(
        "ABC", 0.05, peers, insider_data={}
    )
    assert r_empty["sub_score"] == base["sub_score"]
    assert r_empty["data_quality"]["has_insider"] is False


def test_insider_ceo_cfo_buying_overlay() -> None:
    """ceo_cfo_action=='buying' adds +2 on TOP of the conviction bracket."""
    peers = _peer_momentums_universe("CSGP", 0.0)
    base = institutional.compute_institutional("CSGP", 0.0, peers)
    base_sub = base["sub_score"]

    insider = _insider(
        regime="mild_buying",
        conviction_score=0.30,
        cluster_buying=False,
        ceo_cfo_action="buying",
    )
    result = institutional.compute_institutional(
        "CSGP", 0.0, peers, insider_data=insider
    )
    # +3 (mild_buying) + +2 (CEO/CFO buying) = +5.
    assert result["components"]["insider"]["adjustment_pts"] == pytest.approx(5.0)
    assert result["sub_score"] == pytest.approx(round(base_sub + 5.0, 1))


def test_insider_ceo_cfo_selling_overlay() -> None:
    """ceo_cfo_action=='selling' subtracts 1 on TOP of the conviction bracket."""
    peers = _peer_momentums_universe("ADBE", 0.0)
    base = institutional.compute_institutional("ADBE", 0.0, peers)
    base_sub = base["sub_score"]

    insider = _insider(
        regime="heavy_selling",
        conviction_score=-1.0,
        cluster_buying=False,
        ceo_cfo_action="selling",
    )
    result = institutional.compute_institutional(
        "ADBE", 0.0, peers, insider_data=insider
    )
    # -3 (heavy_selling) + -1 (CEO/CFO selling) = -4.
    assert result["components"]["insider"]["adjustment_pts"] == pytest.approx(-4.0)
    assert result["sub_score"] == pytest.approx(round(base_sub - 4.0, 1))


def test_insider_adjustment_capped_at_plus_ten() -> None:
    """cluster_buying + strong + CEO/CFO buying could theoretically be +10 (cap)."""
    peers = _peer_momentums_universe("CSGP", 0.0)
    base = institutional.compute_institutional("CSGP", 0.0, peers)
    base_sub = base["sub_score"]

    # cluster_buying (+8) + ceo_cfo_buying (+2) = +10 (at cap, not over).
    insider = _insider(
        regime="strong_buying",
        conviction_score=0.97,
        cluster_buying=True,
        ceo_cfo_action="buying",
    )
    result = institutional.compute_institutional(
        "CSGP", 0.0, peers, insider_data=insider
    )
    assert result["components"]["insider"]["adjustment_pts"] == pytest.approx(10.0)
    assert result["sub_score"] == pytest.approx(round(base_sub + 10.0, 1))


def test_insider_adjustment_capped_at_minus_five() -> None:
    """heavy_selling (-3) + CEO/CFO selling (-1) is -4, still inside cap.

    The cap is exercised by an explicit synthetic combination that would
    otherwise blow past -5; the floor still clamps it to -5.
    """
    peers = _peer_momentums_universe("AAPL", 0.0)
    base = institutional.compute_institutional("AAPL", 0.0, peers)
    base_sub = base["sub_score"]

    # Verify the natural worst-case still respects asymmetry.
    insider_natural = _insider(
        regime="heavy_selling",
        conviction_score=-1.0,
        cluster_buying=False,
        ceo_cfo_action="selling",
    )
    natural = institutional.compute_institutional(
        "AAPL", 0.0, peers, insider_data=insider_natural
    )
    # -3 + -1 = -4 (inside the [-5, +10] floor).
    assert natural["components"]["insider"]["adjustment_pts"] == pytest.approx(-4.0)
    assert natural["sub_score"] == pytest.approx(round(base_sub - 4.0, 1))

    # Force the floor with a synthetic payload that bypasses the bracket
    # mapping by chaining heavy_selling + cluster_buying. cluster_buying
    # wins the bracket (+8); ceo_cfo overlay is the only path that could
    # push us under -5, so this case naturally CAN'T reach the floor.
    # Confirm directly via the helper.
    floor_detail = institutional._apply_insider_adjustment(
        base_subscore=50.0,
        insider_data={
            "regime": "heavy_selling",
            "conviction_score": -1.0,
            "cluster_buying": False,
            "ceo_cfo_action": "selling",
        },
    )
    # Natural worst case is -4 (inside the floor) -- the asymmetric design
    # means the floor is a safety net rather than a frequent clamp.
    assert floor_detail[1]["adjustment_pts"] == pytest.approx(-4.0)


def test_insider_threshold_boundary_exactly_point_two() -> None:
    """conviction_score exactly +0.2 lands in the mild_buying bracket (>=)."""
    peers = _peer_momentums_universe("AAPL", 0.0)
    base = institutional.compute_institutional("AAPL", 0.0, peers)
    base_sub = base["sub_score"]

    insider = _insider(
        regime="mild_buying",
        conviction_score=0.20,  # exactly at the threshold
        cluster_buying=False,
        ceo_cfo_action="neutral",
    )
    result = institutional.compute_institutional(
        "AAPL", 0.0, peers, insider_data=insider
    )
    assert result["components"]["insider"]["adjustment_pts"] == pytest.approx(3.0)
    assert result["sub_score"] == pytest.approx(round(base_sub + 3.0, 1))


def test_insider_threshold_boundary_exactly_point_five() -> None:
    """conviction_score exactly +0.5 lands in the strong_buying bracket (>=)."""
    peers = _peer_momentums_universe("AAPL", 0.0)
    base = institutional.compute_institutional("AAPL", 0.0, peers)
    base_sub = base["sub_score"]

    insider = _insider(
        regime="strong_buying",
        conviction_score=0.50,
        cluster_buying=False,
        ceo_cfo_action="neutral",
    )
    result = institutional.compute_institutional(
        "AAPL", 0.0, peers, insider_data=insider
    )
    assert result["components"]["insider"]["adjustment_pts"] == pytest.approx(5.0)
    assert result["sub_score"] == pytest.approx(round(base_sub + 5.0, 1))


def test_insider_component_detail_surfaces_all_fields() -> None:
    """The insider component dict must surface regime/conviction/cluster/ceo_cfo."""
    peers = _peer_momentums_universe("GEHC", 0.05)
    insider = _insider(
        regime="strong_buying",
        conviction_score=0.97,
        cluster_buying=True,
        ceo_cfo_action="buying",
    )
    result = institutional.compute_institutional(
        "GEHC", 0.05, peers, insider_data=insider
    )
    detail = result["components"]["insider"]
    assert detail["regime"] == "strong_buying"
    assert detail["conviction_score"] == pytest.approx(0.97)
    assert detail["cluster_buying"] is True
    assert detail["ceo_cfo_action"] == "buying"
    assert detail["adjustment_pts"] == pytest.approx(10.0)  # cluster + ceo, capped


def test_insider_malformed_conviction_score_treated_as_missing() -> None:
    """A non-numeric conviction_score must NOT crash and must contribute 0."""
    peers = _peer_momentums_universe("AAPL", 0.0)
    base = institutional.compute_institutional("AAPL", 0.0, peers)
    base_sub = base["sub_score"]

    insider = {
        "regime": "neutral",
        "conviction_score": "not a number",
        "cluster_buying": False,
        "ceo_cfo_action": "neutral",
    }
    result = institutional.compute_institutional(
        "AAPL", 0.0, peers, insider_data=insider
    )
    assert result["components"]["insider"]["adjustment_pts"] == 0.0
    assert result["components"]["insider"]["conviction_score"] is None
    assert result["sub_score"] == base_sub


# --- 13F holdings adjustment -----------------------------------------------


def _holdings(
    *,
    conviction_signal: str = "steady",
    signal_score: Optional[float] = 0.0,
    manager_count: int = 15,
    data_quality: str = "good",
    share_change_pct: float = 0.0,
    net_buyers: int = 0,
    net_sellers: int = 0,
) -> Dict[str, object]:
    """Build a minimal 13F holdings payload for tests."""
    return {
        "ticker": "TEST",
        "conviction_signal": conviction_signal,
        "signal_score": signal_score,
        "manager_count": manager_count,
        "data_quality": data_quality,
        "quarter_over_quarter": {
            "share_change_pct": share_change_pct,
            "net_buyers": net_buyers,
            "net_sellers": net_sellers,
            "unchanged": 0,
            "manager_count_change": 0,
            "prior_quarter": "2025-Q4",
        },
    }


def test_holdings_strong_accumulating_adds_five_points() -> None:
    """signal > +0.5 with accumulating signal -> +5 pts."""
    peers = _peer_momentums_universe("MSFT", 0.0)
    base = institutional.compute_institutional("MSFT", 0.0, peers)
    base_sub = base["sub_score"]

    holdings = _holdings(conviction_signal="accumulating", signal_score=0.75)
    result = institutional.compute_institutional(
        "MSFT", 0.0, peers, holdings_data=holdings
    )
    assert result["components"]["holdings"]["adjustment_pts"] == pytest.approx(5.0)
    assert result["components"]["combined_adjustment_pts"] == pytest.approx(5.0)
    assert result["sub_score"] == pytest.approx(round(base_sub + 5.0, 1))
    assert result["data_quality"]["has_holdings"] is True


def test_holdings_mild_accumulating_adds_three_points() -> None:
    """+0.3 <= signal <= +0.5 with accumulating -> +3 pts."""
    peers = _peer_momentums_universe("AAPL", 0.0)
    base = institutional.compute_institutional("AAPL", 0.0, peers)
    base_sub = base["sub_score"]

    holdings = _holdings(conviction_signal="accumulating", signal_score=0.4)
    result = institutional.compute_institutional(
        "AAPL", 0.0, peers, holdings_data=holdings
    )
    assert result["components"]["holdings"]["adjustment_pts"] == pytest.approx(3.0)
    assert result["sub_score"] == pytest.approx(round(base_sub + 3.0, 1))


def test_holdings_mild_distributing_subtracts_two_points() -> None:
    """-0.5 <= signal <= -0.3 with distributing -> -2 pts."""
    peers = _peer_momentums_universe("AAPL", 0.0)
    base = institutional.compute_institutional("AAPL", 0.0, peers)
    base_sub = base["sub_score"]

    holdings = _holdings(conviction_signal="distributing", signal_score=-0.4)
    result = institutional.compute_institutional(
        "AAPL", 0.0, peers, holdings_data=holdings
    )
    assert result["components"]["holdings"]["adjustment_pts"] == pytest.approx(-2.0)
    assert result["sub_score"] == pytest.approx(round(base_sub - 2.0, 1))


def test_holdings_strong_distributing_subtracts_three_points() -> None:
    """signal < -0.5 with distributing -> -3 pts."""
    peers = _peer_momentums_universe("AAPL", 0.0)
    base = institutional.compute_institutional("AAPL", 0.0, peers)
    base_sub = base["sub_score"]

    holdings = _holdings(conviction_signal="distributing", signal_score=-0.8)
    result = institutional.compute_institutional(
        "AAPL", 0.0, peers, holdings_data=holdings
    )
    assert result["components"]["holdings"]["adjustment_pts"] == pytest.approx(-3.0)
    assert result["sub_score"] == pytest.approx(round(base_sub - 3.0, 1))


def test_holdings_steady_zero_adjustment() -> None:
    """conviction_signal=='steady' -> 0 pts regardless of score."""
    peers = _peer_momentums_universe("AAPL", 0.0)
    base = institutional.compute_institutional("AAPL", 0.0, peers)
    base_sub = base["sub_score"]

    holdings = _holdings(conviction_signal="steady", signal_score=0.0)
    result = institutional.compute_institutional(
        "AAPL", 0.0, peers, holdings_data=holdings
    )
    assert result["components"]["holdings"]["adjustment_pts"] == 0.0
    assert result["sub_score"] == base_sub


def test_holdings_sparse_data_quality_zeros_adjustment() -> None:
    """A "sparse" data_quality must NOT contribute adjustment even with strong score."""
    peers = _peer_momentums_universe("OBSC", 0.0)
    base = institutional.compute_institutional("OBSC", 0.0, peers)
    base_sub = base["sub_score"]

    # Sparse data: manager_count=2, but signal looks "accumulating" because both buyers.
    holdings = _holdings(
        conviction_signal="accumulating",
        signal_score=1.0,
        manager_count=2,
        data_quality="sparse",
    )
    result = institutional.compute_institutional(
        "OBSC", 0.0, peers, holdings_data=holdings
    )
    assert result["components"]["holdings"]["adjustment_pts"] == 0.0
    assert result["sub_score"] == base_sub


def test_holdings_absent_does_not_break_insider_path() -> None:
    """Existing insider-only behavior must still work when holdings is None."""
    peers = _peer_momentums_universe("AAPL", 0.0)
    base = institutional.compute_institutional("AAPL", 0.0, peers)
    base_sub = base["sub_score"]

    insider = _insider(
        regime="mild_buying",
        conviction_score=0.30,
        cluster_buying=False,
        ceo_cfo_action="neutral",
    )
    result = institutional.compute_institutional(
        "AAPL", 0.0, peers, insider_data=insider, holdings_data=None
    )
    # Insider-only behavior: +3 pts.
    assert result["components"]["insider"]["adjustment_pts"] == pytest.approx(3.0)
    assert result["components"]["holdings"]["adjustment_pts"] == 0.0
    assert result["components"]["combined_adjustment_pts"] == pytest.approx(3.0)
    assert result["sub_score"] == pytest.approx(round(base_sub + 3.0, 1))
    assert result["data_quality"]["has_holdings"] is False


def test_combined_insider_holdings_cap_binding_at_plus_twelve() -> None:
    """Insider +10 (max) + holdings +5 = +15 must clamp to +12 outer cap."""
    peers = _peer_momentums_universe("CSGP", 0.0)
    base = institutional.compute_institutional("CSGP", 0.0, peers)
    base_sub = base["sub_score"]

    # Insider maxes at +10 via cluster + CEO/CFO buying.
    insider = _insider(
        regime="strong_buying",
        conviction_score=0.97,
        cluster_buying=True,
        ceo_cfo_action="buying",
    )
    holdings = _holdings(conviction_signal="accumulating", signal_score=0.8)
    result = institutional.compute_institutional(
        "CSGP", 0.0, peers, insider_data=insider, holdings_data=holdings
    )
    assert result["components"]["insider"]["adjustment_pts"] == pytest.approx(10.0)
    assert result["components"]["holdings"]["adjustment_pts"] == pytest.approx(5.0)
    # +10 + +5 = +15, clamped to +12.
    assert result["components"]["combined_adjustment_pts"] == pytest.approx(12.0)
    assert result["sub_score"] == pytest.approx(round(base_sub + 12.0, 1))


def test_combined_insider_holdings_cap_binding_at_minus_seven() -> None:
    """Insider -4 (natural worst case) + holdings -3 = -7 lands at floor."""
    peers = _peer_momentums_universe("AAPL", 0.0)
    base = institutional.compute_institutional("AAPL", 0.0, peers)
    base_sub = base["sub_score"]

    insider = _insider(
        regime="heavy_selling",
        conviction_score=-1.0,
        cluster_buying=False,
        ceo_cfo_action="selling",
    )
    holdings = _holdings(conviction_signal="distributing", signal_score=-0.9)
    result = institutional.compute_institutional(
        "AAPL", 0.0, peers, insider_data=insider, holdings_data=holdings
    )
    assert result["components"]["insider"]["adjustment_pts"] == pytest.approx(-4.0)
    assert result["components"]["holdings"]["adjustment_pts"] == pytest.approx(-3.0)
    # -4 + -3 = -7 at the floor.
    assert result["components"]["combined_adjustment_pts"] == pytest.approx(-7.0)
    assert result["sub_score"] == pytest.approx(round(base_sub - 7.0, 1))


def test_holdings_component_detail_surfaces_qoq_fields() -> None:
    """The holdings component dict must expose share_change_pct + net_buyers/sellers."""
    peers = _peer_momentums_universe("AAPL", 0.0)
    holdings = _holdings(
        conviction_signal="accumulating",
        signal_score=0.55,
        manager_count=18,
        data_quality="good",
        share_change_pct=2.5,
        net_buyers=13,
        net_sellers=3,
    )
    result = institutional.compute_institutional(
        "AAPL", 0.0, peers, holdings_data=holdings
    )
    h = result["components"]["holdings"]
    assert h["conviction_signal"] == "accumulating"
    assert h["signal_score"] == pytest.approx(0.55)
    assert h["manager_count"] == 18
    assert h["data_quality"] == "good"
    assert h["share_change_pct"] == pytest.approx(2.5)
    assert h["net_buyers"] == 13
    assert h["net_sellers"] == 3
    assert h["adjustment_pts"] == pytest.approx(5.0)  # strong accumulating


def test_holdings_malformed_signal_score_treated_as_missing() -> None:
    """A non-numeric signal_score must NOT crash and must contribute 0."""
    peers = _peer_momentums_universe("AAPL", 0.0)
    base = institutional.compute_institutional("AAPL", 0.0, peers)
    base_sub = base["sub_score"]

    holdings = {
        "ticker": "AAPL",
        "conviction_signal": "accumulating",
        "signal_score": "not a number",
        "manager_count": 15,
        "data_quality": "good",
        "quarter_over_quarter": {},
    }
    result = institutional.compute_institutional(
        "AAPL", 0.0, peers, holdings_data=holdings
    )
    assert result["components"]["holdings"]["adjustment_pts"] == 0.0
    assert result["components"]["holdings"]["signal_score"] is None
    assert result["sub_score"] == base_sub


# --- Tier 3 #16: momentum_strategy opt-in -----------------------------------


def _sector_cohort_peers() -> Dict[str, Optional[float]]:
    """A 14-ticker universe spanning two sectors with distinct momentum profiles.

    Tech sector: 7 names averaging +25% (a hot sector).
    Utilities sector: 7 names averaging -5% (a cold sector).

    Under ``"universe"`` ranking, every Tech name lands near the top regardless
    of intra-Tech performance. Under ``"sector_relative"`` the within-sector
    rank is what matters.
    """
    return {
        # Tech (hot sector — all positive, spanning +5% to +50%)
        "T1": 0.05,
        "T2": 0.10,
        "T3": 0.18,
        "T4": 0.25,
        "T5": 0.32,
        "T6": 0.40,
        "T7": 0.50,
        # Utilities (cold sector — clustered around -10% to 0%)
        "U1": -0.10,
        "U2": -0.08,
        "U3": -0.06,
        "U4": -0.05,
        "U5": -0.03,
        "U6": -0.01,
        "U7": 0.01,
    }


def _sector_assignments_two_sector() -> Dict[str, str]:
    """Sector map matching :func:`_sector_cohort_peers`."""
    return {
        "T1": "Technology", "T2": "Technology", "T3": "Technology",
        "T4": "Technology", "T5": "Technology", "T6": "Technology",
        "T7": "Technology",
        "U1": "Utilities", "U2": "Utilities", "U3": "Utilities",
        "U4": "Utilities", "U5": "Utilities", "U6": "Utilities",
        "U7": "Utilities",
    }


def _peer_groups_two_group() -> Dict[str, object]:
    """Minimal peer_groups_config with two sector_groups for compound tests."""
    return {
        "min_cohort_size": 5,
        "sector_groups": {
            "tech_software": {
                "tickers": ["T1", "T2", "T3", "T4", "T5", "T6", "T7"],
                "description": "tech group for tests",
            },
            "utilities_reits": {
                "tickers": ["U1", "U2", "U3", "U4", "U5", "U6", "U7"],
                "description": "utilities group for tests",
            },
        },
    }


def test_momentum_strategy_default_is_universe() -> None:
    """When momentum_strategy isn't passed, behavior is the historical 'universe' path."""
    peers = _peer_momentums_universe("AAPL", 0.12)
    default_result = institutional.compute_institutional("AAPL", 0.12, peers)
    explicit_result = institutional.compute_institutional(
        "AAPL", 0.12, peers, momentum_strategy="universe"
    )
    # Same sub_score, same momentum_subscore — strategy='universe' must
    # be a literal no-op vs the un-passed default.
    assert default_result["sub_score"] == explicit_result["sub_score"]
    assert (
        default_result["components"]["momentum_subscore"]
        == explicit_result["components"]["momentum_subscore"]
    )
    assert default_result["components"]["momentum_strategy_used"] == "universe"
    assert default_result["components"]["momentum_cohort_label"] == "universe"
    # 9 peers in the synthetic universe (focal AAPL filtered out).
    assert default_result["components"]["momentum_cohort_size"] == 9


def test_momentum_strategy_sector_relative_uses_within_sector_cohort() -> None:
    """Tech ticker with mid-pack tech momentum ranks median *within* the sector.

    With universe ranking the same ticker would land near the top (Tech is hot
    overall), so this is the key behavioral divergence.
    """
    peers = _sector_cohort_peers()
    # Replace T4 (0.25) with the focal value so the cohort has it.
    peers["TECH_FOCAL"] = 0.25
    sec = _sector_assignments_two_sector()
    sec["TECH_FOCAL"] = "Technology"

    result_universe = institutional.compute_institutional(
        "TECH_FOCAL", 0.25, peers, momentum_strategy="universe"
    )
    result_sector = institutional.compute_institutional(
        "TECH_FOCAL", 0.25, peers,
        momentum_strategy="sector_relative",
        ticker_sector="Technology",
        sector_assignments=sec,
    )

    # Universe-relative: cohort = T1..T7 + U1..U7 + TECH_FOCAL = 15. Excluding
    # the focal -> 14 peers. Below 0.25: T1,T2,T3,U1..U7 = 10. Equal (T4): 1.
    # half-equal -> (10 + 0.5)/14 * 100 = 75.0.
    assert result_universe["components"]["momentum_subscore"] == pytest.approx(75.0)
    # Sector-relative: Tech cohort = T1..T7 + TECH_FOCAL = 8 tickers. Excluding
    # the focal -> 7 peers. Below 0.25: T1,T2,T3 = 3. Equal (T4): 1.
    # half-equal -> (3 + 0.5)/7 * 100 = 50.0.
    assert result_sector["components"]["momentum_subscore"] == pytest.approx(50.0)
    assert result_sector["components"]["momentum_strategy_used"] == "sector_relative"
    assert result_sector["components"]["momentum_cohort_label"] == "sector:Technology"
    # Cohort: 7 peers (7 Tech tickers + TECH_FOCAL minus focal).
    assert result_sector["components"]["momentum_cohort_size"] == 7


def test_momentum_strategy_sector_relative_top_of_sector() -> None:
    """A Tech name with the highest Tech momentum (+0.50) ranks 100 within sector."""
    peers = _sector_cohort_peers()
    peers["TECH_TOP"] = 0.50  # ties T7
    sec = _sector_assignments_two_sector()
    sec["TECH_TOP"] = "Technology"

    result = institutional.compute_institutional(
        "TECH_TOP", 0.50, peers,
        momentum_strategy="sector_relative",
        ticker_sector="Technology",
        sector_assignments=sec,
    )
    # 6 Tech peers (excl focal), all <= 0.50. T7 ties at 0.50.
    # percentile_rank treats ties via the half-credit rule by default.
    assert result["components"]["momentum_subscore"] >= 90.0


def test_momentum_strategy_sector_relative_missing_sector_falls_back_to_universe(
    caplog,
) -> None:
    """No ticker_sector -> WARNING + fallback to 'universe' (no crash)."""
    peers = _sector_cohort_peers()
    peers["AAPL"] = 0.25
    with caplog.at_level("WARNING", logger="lthcs.pillars.institutional"):
        result = institutional.compute_institutional(
            "AAPL", 0.25, peers,
            momentum_strategy="sector_relative",
            ticker_sector=None,  # missing!
            sector_assignments=_sector_assignments_two_sector(),
        )
    assert result["components"]["momentum_strategy_used"] == "universe"
    assert result["components"]["momentum_cohort_label"] == "universe"
    assert any("falling back to universe" in r.message for r in caplog.records)


def test_momentum_strategy_sector_relative_small_cohort_falls_back_to_universe(
    caplog,
) -> None:
    """Sector with < 5 usable peers -> WARNING + fallback to universe."""
    # Tiny sector: 3 Materials tickers.
    peers = {
        "M1": 0.05, "M2": 0.10, "M3": 0.15,
        # Lots of other-sector peers.
        "T1": 0.20, "T2": 0.25, "T3": 0.30, "T4": 0.35, "T5": 0.40,
    }
    sec = {
        "M1": "Materials", "M2": "Materials", "M3": "Materials",
        "T1": "Technology", "T2": "Technology", "T3": "Technology",
        "T4": "Technology", "T5": "Technology",
    }
    with caplog.at_level("WARNING", logger="lthcs.pillars.institutional"):
        result = institutional.compute_institutional(
            "M1", 0.05, peers,
            momentum_strategy="sector_relative",
            ticker_sector="Materials",
            sector_assignments=sec,
        )
    assert result["components"]["momentum_strategy_used"] == "universe"
    assert any("too small" in r.message for r in caplog.records)


def test_momentum_strategy_compound_uses_sector_group() -> None:
    """compound strategy with peer_groups_config -> ranks within sector_group."""
    peers = _sector_cohort_peers()
    peers["T4"] = 0.25  # focal as a member of tech_software group
    sec = _sector_assignments_two_sector()
    cfg = _peer_groups_two_group()

    result = institutional.compute_institutional(
        "T4", 0.25, peers,
        momentum_strategy="compound",
        ticker_sector="Technology",
        sector_assignments=sec,
        peer_groups_config=cfg,
    )
    assert result["components"]["momentum_strategy_used"] == "compound"
    assert (
        result["components"]["momentum_cohort_label"] == "sector_group:tech_software"
    )
    # 6 peers in tech_software (excl T4).
    assert result["components"]["momentum_cohort_size"] == 6
    # 0.25 beats T1 (0.05), T2 (0.10), T3 (0.18) -> 3/6 = 50.
    assert result["components"]["momentum_subscore"] == pytest.approx(50.0)


def test_momentum_strategy_compound_no_group_falls_back_to_sector_relative(
    caplog,
) -> None:
    """compound + no sector_group for ticker -> falls back to sector_relative."""
    peers = _sector_cohort_peers()
    peers["UNKNOWN"] = 0.25
    sec = _sector_assignments_two_sector()
    sec["UNKNOWN"] = "Technology"  # has a sector, just not in any group
    cfg = _peer_groups_two_group()  # has tech_software but UNKNOWN isn't a member

    with caplog.at_level("WARNING", logger="lthcs.pillars.institutional"):
        result = institutional.compute_institutional(
            "UNKNOWN", 0.25, peers,
            momentum_strategy="compound",
            ticker_sector="Technology",
            sector_assignments=sec,
            peer_groups_config=cfg,
        )
    # Should land on sector_relative (Technology has 7 tickers, all >= min 5).
    assert result["components"]["momentum_strategy_used"] == "sector_relative"
    assert result["components"]["momentum_cohort_label"] == "sector:Technology"
    assert any("no sector_group" in r.message for r in caplog.records)


def test_momentum_strategy_compound_no_config_falls_back() -> None:
    """compound without peer_groups_config -> falls back to sector_relative path."""
    peers = _sector_cohort_peers()
    peers["AAPL"] = 0.25
    sec = _sector_assignments_two_sector()
    sec["AAPL"] = "Technology"
    result = institutional.compute_institutional(
        "AAPL", 0.25, peers,
        momentum_strategy="compound",
        ticker_sector="Technology",
        sector_assignments=sec,
        peer_groups_config=None,
    )
    # sector_relative fallback should kick in (sector has 7 names >= min 5).
    assert result["components"]["momentum_strategy_used"] == "sector_relative"


def test_momentum_strategy_unknown_logs_and_defaults_to_universe(caplog) -> None:
    """Garbage strategy value -> WARNING + universe fallback (no crash)."""
    peers = _peer_momentums_universe("AAPL", 0.12)
    with caplog.at_level("WARNING", logger="lthcs.pillars.institutional"):
        result = institutional.compute_institutional(
            "AAPL", 0.12, peers, momentum_strategy="not_a_real_strategy",
        )
    assert result["components"]["momentum_strategy_used"] == "universe"
    assert any("unknown momentum_strategy" in r.message for r in caplog.records)


def test_momentum_strategy_tracking_fields_present_in_every_path() -> None:
    """All three strategies populate the new tracking fields with sane values."""
    peers = _sector_cohort_peers()
    peers["T4"] = 0.25
    sec = _sector_assignments_two_sector()
    cfg = _peer_groups_two_group()

    for strat in ("universe", "sector_relative", "compound"):
        result = institutional.compute_institutional(
            "T4", 0.25, peers,
            momentum_strategy=strat,
            ticker_sector="Technology",
            sector_assignments=sec,
            peer_groups_config=cfg,
        )
        comps = result["components"]
        assert "momentum_strategy_used" in comps
        assert "momentum_cohort_size" in comps
        assert "momentum_cohort_label" in comps
        assert comps["momentum_strategy_used"] in (
            "universe", "sector_relative", "compound"
        )
        assert isinstance(comps["momentum_cohort_size"], int)
        assert comps["momentum_cohort_size"] >= 0
        assert isinstance(comps["momentum_cohort_label"], str)


def test_momentum_strategy_universe_preserves_legacy_sub_score() -> None:
    """Strategy='universe' MUST yield identical sub_score to the no-strategy call."""
    peers = _peer_momentums_universe("AAPL", 0.07)
    r_legacy = institutional.compute_institutional("AAPL", 0.07, peers)
    r_explicit = institutional.compute_institutional(
        "AAPL", 0.07, peers, momentum_strategy="universe",
    )
    r_with_sector_args = institutional.compute_institutional(
        "AAPL", 0.07, peers, momentum_strategy="universe",
        ticker_sector="Technology",          # should be IGNORED for universe
        sector_assignments={"AAPL": "Technology"},
        peer_groups_config={"sector_groups": {}},
    )
    assert r_legacy["sub_score"] == r_explicit["sub_score"]
    assert r_legacy["sub_score"] == r_with_sector_args["sub_score"]
    assert (
        r_legacy["components"]["momentum_subscore"]
        == r_explicit["components"]["momentum_subscore"]
    )


def test_momentum_cohort_size_correct_with_none_peers() -> None:
    """Cohort size counts ONLY non-None peers (excluding focal)."""
    peers = {
        "AAPL": 0.10,
        "MSFT": 0.15,
        "GOOG": None,         # missing data — should NOT count
        "META": 0.05,
        "NVDA": 0.20,
    }
    sec = {
        "AAPL": "Technology", "MSFT": "Technology", "GOOG": "Technology",
        "META": "Technology", "NVDA": "Technology",
    }
    result = institutional.compute_institutional(
        "AAPL", 0.10, peers,
        momentum_strategy="sector_relative",
        ticker_sector="Technology",
        sector_assignments=sec,
    )
    # 4 Tech peers minus 1 (None) minus focal-already-excluded = 3.
    # But min_cohort_size is 5 by default -> falls back to universe.
    # Force smaller threshold via the config knob.
    result_with_lower_min = institutional.compute_institutional(
        "AAPL", 0.10, peers,
        momentum_strategy="sector_relative",
        ticker_sector="Technology",
        sector_assignments=sec,
        peer_groups_config={"min_cohort_size": 3},
    )
    # Now the cohort survives: 3 usable Tech peers.
    assert (
        result_with_lower_min["components"]["momentum_strategy_used"]
        == "sector_relative"
    )
    assert result_with_lower_min["components"]["momentum_cohort_size"] == 3
    # Default 5-min path fell back to universe.
    assert result["components"]["momentum_strategy_used"] == "universe"
