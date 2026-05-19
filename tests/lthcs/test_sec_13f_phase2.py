"""Tier 3 #13 Phase 2 tests for AUM-weighted 13F aggregation.

Phase 2 ships:

- Manager list expanded from ~50 to 100+ entries in
  ``data/lthcs/13f_institutions.json`` with per-manager
  ``equity_aum_usd_b`` so the aggregation can weight a Vanguard buy
  more heavily than a small hedge-fund buy.
- New ``MANAGER_AUM_WEIGHTS`` / ``MANAGER_AUM_WEIGHT_FLOOR`` module
  constants populated alongside ``TRACKED_MANAGERS``.
- ``aggregate_holdings_for_ticker`` accepts ``manager_aum_weights`` and
  emits ``weighted_signal_score`` + ``weighted_holders_share`` while
  preserving the legacy ``signal_score`` (Phase 1 invariant).
- The Institutional pillar prefers ``weighted_signal_score`` when
  present and surfaces ``score_used``, ``weighted_signal_score`` and
  ``weighted_holders_share`` under ``components.holdings``.

These tests cover the JSON shape, the weight-loader fallbacks, the
weighted-signal math, and that legacy callers (no weights, no
``weighted_signal_score`` field) still get the Phase 1 behavior.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

from lthcs.pillars import institutional
from lthcs.sources import sec_13f


REPO_ROOT = Path(__file__).resolve().parents[2]
INSTITUTIONS_PATH = REPO_ROOT / "data" / "lthcs" / "13f_institutions.json"
CUSIP_MAP_PATH = REPO_ROOT / "data" / "lthcs" / "13f_cusip_map.json"
UNIVERSE_PATH = REPO_ROOT / "data" / "lthcs" / "universe.json"


# --- JSON shape: Phase 2 manager-list expansion ----------------------------


def test_phase2_manager_list_has_100plus_active_entries() -> None:
    """Phase 2 floor: 100 active managers (vs ~50 in Phase 1)."""
    with INSTITUTIONS_PATH.open() as fh:
        data = json.load(fh)
    active = [m for m in data["managers"] if m.get("active", True)]
    assert len(active) >= 100, (
        "Phase 2 floor is 100 active managers; got {}".format(len(active))
    )


def test_phase2_tracked_aum_pct_in_phase2_band() -> None:
    """Phase 2 tracked_aum_pct lifts from ~0.40 to ~0.65."""
    with INSTITUTIONS_PATH.open() as fh:
        data = json.load(fh)
    pct = data.get("tracked_aum_pct")
    assert isinstance(pct, (int, float))
    assert 0.55 <= float(pct) <= 0.80, (
        "Phase 2 tracked_aum_pct expected ~0.65, got {}".format(pct)
    )


def test_phase2_every_active_manager_has_equity_aum() -> None:
    """Each active manager must carry an ``equity_aum_usd_b`` (>0) for weighting."""
    with INSTITUTIONS_PATH.open() as fh:
        data = json.load(fh)
    bad = []
    for m in data["managers"]:
        if not m.get("active", True):
            continue
        v = m.get("equity_aum_usd_b")
        if not isinstance(v, (int, float)) or float(v) <= 0.0:
            bad.append((m.get("name"), v))
    assert not bad, "active managers missing/zero equity_aum_usd_b: {}".format(bad)


def test_phase2_tracked_aum_total_at_least_5T() -> None:
    """Phase 2 spec target: sum of tracked equity AUM ≳ $5T."""
    with INSTITUTIONS_PATH.open() as fh:
        data = json.load(fh)
    total = sum(
        float(m.get("equity_aum_usd_b") or 0.0)
        for m in data["managers"]
        if m.get("active", True)
    )
    assert total >= 5000.0, (
        "Phase 2 expects ≥ $5T tracked equity AUM, got ${:.0f}B".format(total)
    )


def test_phase2_aum_weighting_block_present_and_well_formed() -> None:
    """Phase 2 added an ``aum_weighting`` config block at the top level."""
    with INSTITUTIONS_PATH.open() as fh:
        data = json.load(fh)
    cfg = data.get("aum_weighting")
    assert isinstance(cfg, dict), "aum_weighting block missing"
    assert cfg.get("enabled") is True
    assert cfg.get("field") == "equity_aum_usd_b"
    floor = cfg.get("min_weight_floor_b")
    assert isinstance(floor, (int, float)) and float(floor) > 0.0


# --- Module-level constants ------------------------------------------------


def test_module_exposes_manager_aum_weights_constant() -> None:
    """``MANAGER_AUM_WEIGHTS`` is populated at import and mirrors TRACKED_MANAGERS."""
    assert hasattr(sec_13f, "MANAGER_AUM_WEIGHTS")
    weights = sec_13f.MANAGER_AUM_WEIGHTS
    assert isinstance(weights, dict) and weights
    # Same key-set as TRACKED_MANAGERS.
    assert set(weights.keys()) == set(sec_13f.TRACKED_MANAGERS.keys())


def test_module_exposes_manager_aum_weight_floor() -> None:
    """``MANAGER_AUM_WEIGHT_FLOOR`` is a positive scalar (USD billions)."""
    assert hasattr(sec_13f, "MANAGER_AUM_WEIGHT_FLOOR")
    floor = sec_13f.MANAGER_AUM_WEIGHT_FLOOR
    assert isinstance(floor, (int, float))
    assert float(floor) > 0.0


def test_manager_aum_weights_sane_ordering() -> None:
    """BlackRock + Vanguard must outweigh every smaller manager by orders of magnitude."""
    weights = sec_13f.MANAGER_AUM_WEIGHTS
    top2 = sorted(weights.values(), reverse=True)[:2]
    # Top-2 each at least $1T (i.e. 1000 in the USD-bn scale).
    assert min(top2) >= 1000.0, "BlackRock / Vanguard weights look wrong: {}".format(top2)
    # And at least one mid-tier hedge fund < $100B for the spread to matter.
    assert min(weights.values()) <= 100.0, (
        "weights look uniform — AUM-weighting won't move the needle"
    )


def test_load_managers_full_returns_four_tuple() -> None:
    """The new loader returns (managers, pct, weights, floor)."""
    out = sec_13f._load_managers_full()
    assert isinstance(out, tuple) and len(out) == 4
    managers, pct, weights, floor = out
    assert isinstance(managers, dict)
    assert isinstance(pct, float)
    assert isinstance(weights, dict)
    assert isinstance(floor, float)
    # Weights/managers keysets match.
    assert set(managers.keys()) == set(weights.keys())


def test_load_managers_full_applies_weight_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A manager with equity_aum_usd_b=0 must come out at the floor, not 0."""
    payload = {
        "tracked_aum_pct": 0.5,
        "aum_weighting": {"enabled": True, "field": "equity_aum_usd_b", "min_weight_floor_b": 1.5},
        "managers": [
            {"name": "ZeroAUM", "cik": "0000000001", "equity_aum_usd_b": 0.0, "active": True},
            {"name": "MidAUM",  "cik": "0000000002", "equity_aum_usd_b": 25.0, "active": True},
        ],
    }
    (tmp_path / "13f_institutions.json").write_text(json.dumps(payload))
    monkeypatch.setenv("LTHCS_13F_DATA_DIR", str(tmp_path))
    managers, pct, weights, floor = sec_13f._load_managers_full()
    assert floor == pytest.approx(1.5)
    assert weights["ZeroAUM"] == pytest.approx(1.5)   # floor applied
    assert weights["MidAUM"] == pytest.approx(25.0)
    assert pct == pytest.approx(0.5)


def test_load_managers_full_falls_back_when_dir_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty data dir -> hard-coded fallback weights, not crash."""
    monkeypatch.setenv("LTHCS_13F_DATA_DIR", str(tmp_path))
    managers, pct, weights, floor = sec_13f._load_managers_full()
    assert managers == sec_13f._FALLBACK_TRACKED_MANAGERS
    assert set(weights.keys()) == set(managers.keys())
    # Every fallback weight is at least the floor.
    assert all(w >= floor for w in weights.values())
    # BlackRock + Vanguard are the heaviest fallback weights.
    top = sorted(weights.items(), key=lambda kv: kv[1], reverse=True)[:2]
    top_names = {name for name, _ in top}
    assert top_names == {"BlackRock", "Vanguard"}


# --- aggregate_holdings_for_ticker: AUM-weighted signal --------------------


def _quarterly(holdings: Dict[str, Dict[str, float]], quarter: str = "2026-Q1") -> Dict[str, Any]:
    return {"quarter": quarter, "holdings": holdings}


def test_weighted_signal_matches_unweighted_when_no_weights_passed() -> None:
    """Phase 1 invariant: legacy callers that don't pass weights see
    ``weighted_signal_score == signal_score``."""
    manager_data = {
        "Alpha":   [_quarterly({"AAPL": {"shares": 200, "value": 2000}}, "2026-Q1"),
                    _quarterly({"AAPL": {"shares": 100, "value": 1000}}, "2025-Q4")],
        "Bravo":   [_quarterly({"AAPL": {"shares": 50,  "value": 500}},  "2026-Q1"),
                    _quarterly({"AAPL": {"shares": 100, "value": 1000}}, "2025-Q4")],
        "Charlie": [_quarterly({"AAPL": {"shares": 80,  "value": 800}},  "2026-Q1"),
                    _quarterly({"AAPL": {"shares": 40,  "value": 400}},  "2025-Q4")],
    }
    out = sec_13f.aggregate_holdings_for_ticker("AAPL", manager_data)
    # No weights -> weighted falls back to unweighted.
    assert "weighted_signal_score" in out
    assert out["weighted_signal_score"] == out["signal_score"]


def test_weighted_signal_dominated_by_largest_manager() -> None:
    """Mega manager buys, small managers sell -> weighted_signal_score
    looks like a buy (positive) even when unweighted reads as a sell."""
    manager_data = {
        # Vanguard-like: buys
        "MegaA": [
            _quarterly({"AAPL": {"shares": 1_000_000, "value": 200_000_000}}, "2026-Q1"),
            _quarterly({"AAPL": {"shares":   500_000, "value": 100_000_000}}, "2025-Q4"),
        ],
        # Small hedge fund #1: sells
        "TinyA": [
            _quarterly({"AAPL": {"shares": 100,  "value": 20_000}}, "2026-Q1"),
            _quarterly({"AAPL": {"shares": 200,  "value": 40_000}}, "2025-Q4"),
        ],
        # Small hedge fund #2: sells
        "TinyB": [
            _quarterly({"AAPL": {"shares": 100,  "value": 20_000}}, "2026-Q1"),
            _quarterly({"AAPL": {"shares": 200,  "value": 40_000}}, "2025-Q4"),
        ],
        # Small hedge fund #3: sells
        "TinyC": [
            _quarterly({"AAPL": {"shares": 100,  "value": 20_000}}, "2026-Q1"),
            _quarterly({"AAPL": {"shares": 200,  "value": 40_000}}, "2025-Q4"),
        ],
    }
    weights = {"MegaA": 5000.0, "TinyA": 5.0, "TinyB": 5.0, "TinyC": 5.0}

    out = sec_13f.aggregate_holdings_for_ticker(
        "AAPL", manager_data, manager_aum_weights=weights
    )
    # Unweighted: 1 buyer, 3 sellers -> negative.
    assert out["signal_score"] < 0
    # Weighted: 5000 (buy) vs 15 (sell) -> sharply positive.
    assert out["weighted_signal_score"] > 0.9


def test_weighted_signal_clamped_to_unit_interval() -> None:
    """All managers in same direction -> weighted_signal_score in {-1, +1}."""
    # All-buyer case:
    manager_data = {
        "M1": [_quarterly({"X": {"shares": 200, "value": 200}}, "2026-Q1"),
               _quarterly({"X": {"shares": 100, "value": 100}}, "2025-Q4")],
        "M2": [_quarterly({"X": {"shares": 200, "value": 200}}, "2026-Q1"),
               _quarterly({"X": {"shares": 100, "value": 100}}, "2025-Q4")],
    }
    weights = {"M1": 1000.0, "M2": 5.0}
    out = sec_13f.aggregate_holdings_for_ticker(
        "X", manager_data, manager_aum_weights=weights
    )
    assert out["weighted_signal_score"] == pytest.approx(1.0)
    # All-seller case:
    manager_data2 = {
        "M1": [_quarterly({"X": {"shares": 50,  "value": 50}}, "2026-Q1"),
               _quarterly({"X": {"shares": 100, "value": 100}}, "2025-Q4")],
        "M2": [_quarterly({"X": {"shares": 50,  "value": 50}}, "2026-Q1"),
               _quarterly({"X": {"shares": 100, "value": 100}}, "2025-Q4")],
    }
    out2 = sec_13f.aggregate_holdings_for_ticker(
        "X", manager_data2, manager_aum_weights=weights
    )
    assert out2["weighted_signal_score"] == pytest.approx(-1.0)


def test_weighted_holders_share_reflects_aum_coverage() -> None:
    """Holders' weight sum / tracked universe weight sum surfaces under
    ``weighted_holders_share`` in [0, 1]."""
    manager_data = {
        # Has position.
        "Big": [_quarterly({"X": {"shares": 100, "value": 100}}, "2026-Q1")],
        # Has position.
        "Mid": [_quarterly({"X": {"shares": 50, "value": 50}}, "2026-Q1")],
        # Tracked, but doesn't hold X.
        "Tiny": [_quarterly({"Y": {"shares": 10, "value": 10}}, "2026-Q1")],
    }
    weights = {"Big": 1000.0, "Mid": 200.0, "Tiny": 50.0}
    out = sec_13f.aggregate_holdings_for_ticker(
        "X", manager_data, manager_aum_weights=weights
    )
    # holders weight = 1200, universe weight = 1250 -> 0.96.
    assert out["weighted_holders_share"] == pytest.approx(0.96, abs=0.01)
    # And cleanly bounded.
    assert 0.0 <= out["weighted_holders_share"] <= 1.0


def test_legacy_signal_score_unchanged_by_weights() -> None:
    """Phase 1 invariant: the unweighted ``signal_score`` math is unchanged
    regardless of whether ``manager_aum_weights`` is passed."""
    manager_data = {
        "M1": [_quarterly({"X": {"shares": 200, "value": 200}}, "2026-Q1"),
               _quarterly({"X": {"shares": 100, "value": 100}}, "2025-Q4")],
        "M2": [_quarterly({"X": {"shares": 50,  "value": 50}},  "2026-Q1"),
               _quarterly({"X": {"shares": 100, "value": 100}}, "2025-Q4")],
    }
    out_no_w = sec_13f.aggregate_holdings_for_ticker("X", manager_data)
    out_with_w = sec_13f.aggregate_holdings_for_ticker(
        "X", manager_data, manager_aum_weights={"M1": 5000.0, "M2": 5.0}
    )
    assert out_no_w["signal_score"] == out_with_w["signal_score"]
    # But weighted_signal_score should diverge.
    assert out_no_w["weighted_signal_score"] != out_with_w["weighted_signal_score"]


def test_aggregate_emits_weighted_signal_score_field_always() -> None:
    """Even without weights, the ``weighted_signal_score`` key must be present
    (set to the unweighted score) so downstream code can rely on its existence."""
    out = sec_13f.aggregate_holdings_for_ticker("X", {"M1": []})
    assert "weighted_signal_score" in out
    assert "weighted_holders_share" in out


# --- fetch_universe_institutional_holdings smoke test -----------------------


def test_fetch_universe_uses_module_weights_by_default(monkeypatch) -> None:
    """When ``managers`` isn't overridden, the aggregation must receive
    ``MANAGER_AUM_WEIGHTS`` so the weighted signal is computed."""

    captured: Dict[str, Any] = {}

    def fake_fetch(cik, *, tickers, as_of=None):
        # Pretend Vanguard accumulated, others held flat.
        if cik == sec_13f.TRACKED_MANAGERS.get("Vanguard"):
            return [
                {"quarter": "2026-Q1", "holdings": {"AAPL": {"shares": 1000, "value": 100000}}},
                {"quarter": "2025-Q4", "holdings": {"AAPL": {"shares": 500,  "value": 50000}}},
            ]
        return []

    monkeypatch.setattr(sec_13f, "fetch_manager_13f_holdings", fake_fetch)

    # Patch aggregate to capture the kwargs.
    orig_aggregate = sec_13f.aggregate_holdings_for_ticker

    def capturing_aggregate(*args, **kwargs):
        captured.update(kwargs)
        return orig_aggregate(*args, **kwargs)

    monkeypatch.setattr(sec_13f, "aggregate_holdings_for_ticker", capturing_aggregate)

    sec_13f.fetch_universe_institutional_holdings(["AAPL"])

    assert "manager_aum_weights" in captured
    weights = captured["manager_aum_weights"]
    assert weights is not None
    assert "Vanguard" in weights
    assert weights["Vanguard"] >= 1000.0


def test_fetch_universe_with_explicit_managers_passes_no_weights(monkeypatch) -> None:
    """Caller-supplied managers map -> weights pass-through is None
    so the aggregation falls back to unweighted behavior cleanly."""

    captured: Dict[str, Any] = {}

    monkeypatch.setattr(sec_13f, "fetch_manager_13f_holdings", lambda *a, **k: [])

    orig_aggregate = sec_13f.aggregate_holdings_for_ticker

    def capturing_aggregate(*args, **kwargs):
        captured.update(kwargs)
        return orig_aggregate(*args, **kwargs)

    monkeypatch.setattr(sec_13f, "aggregate_holdings_for_ticker", capturing_aggregate)

    sec_13f.fetch_universe_institutional_holdings(
        ["AAPL"], managers={"Custom": "0000000001"}
    )
    assert captured.get("manager_aum_weights") is None


# --- Institutional pillar: weighted_signal_score is preferred -------------


def _peer_momentums(focal: str, focal_pct: float) -> Dict[str, float]:
    """Minimal peer momentum map: focal + 9 zero-return peers."""
    out: Dict[str, float] = {focal: focal_pct}
    for i in range(9):
        out["PEER{}".format(i)] = 0.0
    return out


def _holdings_payload(
    *,
    conviction_signal: str = "accumulating",
    signal_score: float = 0.6,
    weighted_signal_score: Optional[float] = None,
    weighted_holders_share: float = 0.5,
    manager_count: int = 15,
    data_quality: str = "good",
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "ticker": "TEST",
        "conviction_signal": conviction_signal,
        "signal_score": signal_score,
        "weighted_signal_score": weighted_signal_score,
        "weighted_holders_share": weighted_holders_share,
        "manager_count": manager_count,
        "manager_universe_size": 113,
        "tracked_aum_pct": 0.65,
        "data_quality": data_quality,
        "quarter_over_quarter": {
            "share_change_pct": 1.5,
            "net_buyers": 8,
            "net_sellers": 2,
            "unchanged": 5,
            "manager_count_change": 1,
            "prior_quarter": "2025-Q4",
        },
    }
    return payload


def test_pillar_prefers_weighted_signal_when_present() -> None:
    """When ``weighted_signal_score`` is present and disagrees with
    ``signal_score``, the pillar bracket uses the weighted value."""
    peers = _peer_momentums("AAPL", 0.0)
    # Unweighted score sits in the mild bracket (+0.4), weighted score
    # sits in the strong bracket (+0.7). With Phase 2 preference the
    # adjustment must be +5 (strong), not +3 (mild).
    holdings = _holdings_payload(
        conviction_signal="accumulating",
        signal_score=0.4,
        weighted_signal_score=0.7,
    )
    result = institutional.compute_institutional(
        "AAPL", 0.0, peers, holdings_data=holdings
    )
    h = result["components"]["holdings"]
    assert h["score_used"] == "weighted_signal_score"
    assert h["weighted_signal_score"] == pytest.approx(0.7)
    assert h["adjustment_pts"] == pytest.approx(5.0)


def test_pillar_falls_back_to_signal_score_when_weighted_missing() -> None:
    """Legacy payloads (no ``weighted_signal_score``) still drive the
    bracket off the unweighted ``signal_score``."""
    peers = _peer_momentums("AAPL", 0.0)
    holdings = _holdings_payload(
        conviction_signal="accumulating",
        signal_score=0.4,
        weighted_signal_score=None,
    )
    result = institutional.compute_institutional(
        "AAPL", 0.0, peers, holdings_data=holdings
    )
    h = result["components"]["holdings"]
    assert h["score_used"] == "signal_score"
    assert h["adjustment_pts"] == pytest.approx(3.0)  # mild accumulating


def test_pillar_surfaces_weighted_holders_share() -> None:
    """``weighted_holders_share`` rides through the pillar detail dict."""
    peers = _peer_momentums("AAPL", 0.0)
    holdings = _holdings_payload(
        weighted_signal_score=0.6, weighted_holders_share=0.72,
    )
    result = institutional.compute_institutional(
        "AAPL", 0.0, peers, holdings_data=holdings
    )
    h = result["components"]["holdings"]
    assert h["weighted_holders_share"] == pytest.approx(0.72)


def test_pillar_weighted_overrides_unweighted_into_distributing() -> None:
    """Mirror test: an unweighted ``+0.4`` (mild buying) overridden by
    a weighted ``-0.6`` (strong selling) flips the bracket sign."""
    peers = _peer_momentums("AAPL", 0.0)
    holdings = _holdings_payload(
        conviction_signal="distributing",
        signal_score=0.4,            # would normally produce +3, but...
        weighted_signal_score=-0.6,  # ...weighted strong-selling drives -3
    )
    result = institutional.compute_institutional(
        "AAPL", 0.0, peers, holdings_data=holdings
    )
    h = result["components"]["holdings"]
    assert h["adjustment_pts"] == pytest.approx(-3.0)


def test_pillar_holdings_detail_has_phase2_keys() -> None:
    """Phase 2 added three keys: weighted_signal_score, score_used,
    weighted_holders_share. They must be present in every holdings detail
    dict — including the no-data path."""
    peers = _peer_momentums("AAPL", 0.0)
    # Path 1: no holdings_data at all.
    r_none = institutional.compute_institutional("AAPL", 0.0, peers)
    for k in ("weighted_signal_score", "score_used", "weighted_holders_share"):
        assert k in r_none["components"]["holdings"]
    # Path 2: holdings present.
    r_full = institutional.compute_institutional(
        "AAPL", 0.0, peers, holdings_data=_holdings_payload(weighted_signal_score=0.5),
    )
    for k in ("weighted_signal_score", "score_used", "weighted_holders_share"):
        assert k in r_full["components"]["holdings"]
