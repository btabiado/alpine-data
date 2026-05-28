"""Tier 3 #13 Phase 1 tests for the 13F coverage expansion.

Covers:

- The externalized manager list (``data/lthcs/13f_institutions.json``)
  is loaded at import time and produces the expected ~50-manager band.
- The externalized CUSIP map (``data/lthcs/13f_cusip_map.json``)
  covers the full LTHCS universe.
- ``_load_managers`` / ``_load_cusip_map`` correctly fall back to the
  hard-coded constants when the JSON files are missing / malformed.
- ``aggregate_holdings_for_ticker`` emits the new additive fields
  (``manager_universe_size``, ``tracked_aum_pct``) without breaking
  existing consumers.
- The institutional pillar applies coverage-aware scaling to the
  holdings adjustment per ``_HOLDINGS_COVERAGE_FLOOR_FOR_FULL_PTS``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest

from lthcs.pillars import institutional
from lthcs.sources import sec_13f


REPO_ROOT = Path(__file__).resolve().parents[2]
INSTITUTIONS_PATH = REPO_ROOT / "data" / "lthcs" / "13f_institutions.json"
CUSIP_MAP_PATH = REPO_ROOT / "data" / "lthcs" / "13f_cusip_map.json"
UNIVERSE_PATH = REPO_ROOT / "data" / "lthcs" / "universe.json"


# --- JSON data file shape --------------------------------------------------

def test_institutions_json_exists_and_has_phase2_manager_band() -> None:
    """Phase 2 expands the manager list to 100+ entries.

    Originally a Phase 1 ~50-manager guard; loosened in Phase 2 to a
    floor of 100 active managers so the per-AUM weighting work has
    enough breadth to matter.
    """
    assert INSTITUTIONS_PATH.exists(), "13f_institutions.json missing"
    with INSTITUTIONS_PATH.open() as fh:
        data = json.load(fh)
    managers = data.get("managers", [])
    assert isinstance(managers, list)
    active = [m for m in managers if m.get("active", True)]
    # Phase 2: ≥100 active managers (was 40-60 in Phase 1).
    assert 100 <= len(active) <= 200, (
        "expected 100+ active managers in Phase 2, got {}".format(len(active))
    )
    # tracked_aum_pct lifts from ~0.40 (Phase 1) to ~0.65 (Phase 2).
    pct = data.get("tracked_aum_pct")
    assert isinstance(pct, (int, float))
    assert 0.25 <= float(pct) <= 0.80


def test_institutions_json_entries_are_well_formed() -> None:
    with INSTITUTIONS_PATH.open() as fh:
        data = json.load(fh)
    seen_ciks: Dict[str, str] = {}
    seen_names: set = set()
    for entry in data["managers"]:
        assert isinstance(entry, dict), entry
        name = entry.get("name")
        cik = entry.get("cik")
        assert isinstance(name, str) and name, entry
        assert isinstance(cik, str) and cik.isdigit() and len(cik) == 10, entry
        # No duplicate (name, cik) pairs.
        assert name not in seen_names, "duplicate manager name: {}".format(name)
        seen_names.add(name)
        # Different managers can share a CIK only as a deliberate
        # multi-sleeve split (Capital Research / World) — but we don't
        # ship such a split in Phase 1 so reject dupes.
        assert cik not in seen_ciks, "duplicate CIK: {} ({} vs {})".format(
            cik, seen_ciks[cik], name
        )
        seen_ciks[cik] = name
        # aum_band / type are free-form metadata for Phase 2 weighting;
        # just sanity-check the values aren't empty when present.
        for k in ("aum_band", "type"):
            v = entry.get(k)
            if v is not None:
                assert isinstance(v, str) and v


def test_cusip_map_covers_full_universe() -> None:
    """Phase 1 spec §6.1: backfill all 168 LTHCS tickers into the CUSIP map."""
    with UNIVERSE_PATH.open() as fh:
        universe = json.load(fh)
    with CUSIP_MAP_PATH.open() as fh:
        cusip_map = json.load(fh)
    universe_tickers = {t["ticker"] for t in universe["tickers"]}
    mapped_tickers = set(cusip_map["tickers"].keys())
    missing = universe_tickers - mapped_tickers
    assert not missing, "tickers missing from CUSIP map: {}".format(sorted(missing))


def test_cusip_map_entries_have_cusips_or_aliases() -> None:
    """Every entry must give the parser something to match on."""
    with CUSIP_MAP_PATH.open() as fh:
        cusip_map = json.load(fh)
    for ticker, entry in cusip_map["tickers"].items():
        cusips = entry.get("cusips") or []
        aliases = entry.get("name_aliases") or []
        assert isinstance(cusips, list)
        assert isinstance(aliases, list)
        assert cusips or aliases, "ticker {} has no cusips OR aliases".format(ticker)
        for c in cusips:
            assert isinstance(c, str) and c.strip()
        for a in aliases:
            assert isinstance(a, str) and a.strip()


# --- _load_managers fallback behavior --------------------------------------

def test_load_managers_uses_fallback_when_dir_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pointing LTHCS_13F_DATA_DIR at an empty dir falls back gracefully."""
    monkeypatch.setenv("LTHCS_13F_DATA_DIR", str(tmp_path))
    managers, pct = sec_13f._load_managers()
    assert managers == sec_13f._FALLBACK_TRACKED_MANAGERS
    assert pct == pytest.approx(sec_13f._FALLBACK_TRACKED_AUM_PCT)


def test_load_managers_uses_fallback_on_malformed_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "13f_institutions.json").write_text("{ not valid json")
    monkeypatch.setenv("LTHCS_13F_DATA_DIR", str(tmp_path))
    managers, pct = sec_13f._load_managers()
    assert managers == sec_13f._FALLBACK_TRACKED_MANAGERS
    assert pct == pytest.approx(sec_13f._FALLBACK_TRACKED_AUM_PCT)


def test_load_managers_skips_inactive_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = {
        "version": 2,
        "tracked_aum_pct": 0.42,
        "managers": [
            {"name": "ActiveOne", "cik": "0000000001", "active": True},
            {"name": "InactiveTwo", "cik": "0000000002", "active": False},
            {"name": "DefaultActive", "cik": "0000000003"},  # active key omitted
        ],
    }
    (tmp_path / "13f_institutions.json").write_text(json.dumps(payload))
    monkeypatch.setenv("LTHCS_13F_DATA_DIR", str(tmp_path))
    managers, pct = sec_13f._load_managers()
    assert managers == {
        "ActiveOne": "0000000001",
        "DefaultActive": "0000000003",
    }
    assert pct == pytest.approx(0.42)


def test_load_managers_pads_short_ciks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CIKs from the JSON should always come out as 10-digit zero-padded."""
    payload = {
        "managers": [
            {"name": "Mgr", "cik": "12345", "active": True},
        ],
    }
    (tmp_path / "13f_institutions.json").write_text(json.dumps(payload))
    monkeypatch.setenv("LTHCS_13F_DATA_DIR", str(tmp_path))
    managers, _ = sec_13f._load_managers()
    assert managers == {"Mgr": "0000012345"}


def test_load_cusip_map_uses_fallback_when_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LTHCS_13F_DATA_DIR", str(tmp_path))
    cusips, aliases = sec_13f._load_cusip_map()
    assert "AAPL" in cusips
    assert cusips["AAPL"] == ("037833100",)
    # Aliases also fall back to the constant map.
    assert "AAPL" in aliases


def test_load_cusip_map_normalizes_ticker_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = {
        "tickers": {
            "  aapl ": {"cusips": ["037833100"], "name_aliases": ["apple"]},
        }
    }
    (tmp_path / "13f_cusip_map.json").write_text(json.dumps(payload))
    monkeypatch.setenv("LTHCS_13F_DATA_DIR", str(tmp_path))
    cusips, aliases = sec_13f._load_cusip_map()
    assert "AAPL" in cusips
    assert aliases["AAPL"] == ("apple",)


# --- Module-level constants populated at import ----------------------------

def test_tracked_managers_constant_matches_json_count() -> None:
    """TRACKED_MANAGERS at module level should reflect the JSON's active
    entries (modulo any tests running with their own LTHCS_13F_DATA_DIR
    override)."""
    expected, _ = sec_13f._load_managers()
    assert sec_13f.TRACKED_MANAGERS == expected


def test_ticker_to_cusip_constant_matches_json() -> None:
    expected, _ = sec_13f._load_cusip_map()
    assert sec_13f.TICKER_TO_CUSIP == expected


def test_tracked_aum_pct_in_expected_band() -> None:
    """Phase 1 ~0.40, fallback 0.25, Phase 2 ~0.65 — accept the loaded value."""
    assert 0.20 <= sec_13f.TRACKED_AUM_PCT <= 0.80


# --- aggregate_holdings_for_ticker additive fields -------------------------

def test_aggregate_emits_manager_universe_size_default() -> None:
    """When called without explicit ``manager_universe_size``, the
    aggregate should fall back to ``len(manager_data)`` so old callers
    keep producing sensible values."""
    manager_data = {
        "Alpha":   [{"quarter": "2026-Q1", "holdings": {"AAPL": {"shares": 100, "value": 1000}}}],
        "Bravo":   [{"quarter": "2026-Q1", "holdings": {"AAPL": {"shares": 50, "value": 500}}}],
        "Charlie": [],
    }
    out = sec_13f.aggregate_holdings_for_ticker("AAPL", manager_data)
    assert out["manager_universe_size"] == 3
    assert out["manager_count"] == 2
    # tracked_aum_pct defaults to None when not passed.
    assert out["tracked_aum_pct"] is None


def test_aggregate_passes_through_explicit_universe_and_aum_pct() -> None:
    manager_data = {
        "Alpha": [{"quarter": "2026-Q1", "holdings": {"AAPL": {"shares": 100, "value": 1000}}}],
    }
    out = sec_13f.aggregate_holdings_for_ticker(
        "AAPL", manager_data,
        manager_universe_size=50,
        tracked_aum_pct=0.42,
    )
    assert out["manager_universe_size"] == 50
    assert out["tracked_aum_pct"] == pytest.approx(0.42)


def test_aggregate_clamps_malformed_universe_size() -> None:
    manager_data = {"Alpha": []}
    out = sec_13f.aggregate_holdings_for_ticker(
        "AAPL", manager_data,
        manager_universe_size="banana",  # type: ignore[arg-type]
        tracked_aum_pct="banana",        # type: ignore[arg-type]
    )
    # Malformed -> fall back to len(manager_data) for universe_size,
    # None for aum_pct.
    assert out["manager_universe_size"] == 1
    assert out["tracked_aum_pct"] is None


# --- Pillar: coverage-aware scaling ----------------------------------------

def _peers(focal: str, focal_pct: float) -> Dict[str, float]:
    """Minimal peer-momentum map: focal + 9 zero-return peers."""
    out: Dict[str, float] = {focal: focal_pct}
    for i in range(9):
        out["PEER{}".format(i)] = 0.0
    return out


def _holdings_payload(
    *,
    conviction_signal: str = "accumulating",
    signal_score: float = 0.6,
    manager_count: int = 12,
    data_quality: str = "good",
    manager_universe_size: int = 50,
    tracked_aum_pct: float = 0.4,
) -> Dict[str, Any]:
    return {
        "ticker": "TEST",
        "conviction_signal": conviction_signal,
        "signal_score": signal_score,
        "manager_count": manager_count,
        "manager_universe_size": manager_universe_size,
        "tracked_aum_pct": tracked_aum_pct,
        "data_quality": data_quality,
        "quarter_over_quarter": {
            "share_change_pct": 1.5,
            "net_buyers": 8,
            "net_sellers": 2,
            "unchanged": 2,
            "manager_count_change": 1,
            "prior_quarter": "2025-Q4",
        },
    }


def test_coverage_scale_full_when_manager_count_at_floor() -> None:
    """manager_count == 10 (the floor) -> coverage_scale = 1.0."""
    peers = _peers("AAPL", 0.0)
    holdings = _holdings_payload(
        conviction_signal="accumulating",
        signal_score=0.7,
        manager_count=10,
    )
    result = institutional.compute_institutional(
        "AAPL", 0.0, peers, holdings_data=holdings
    )
    h = result["components"]["holdings"]
    assert h["coverage_scale"] == pytest.approx(1.0)
    assert h["adjustment_pts"] == pytest.approx(5.0)


def test_coverage_scale_full_when_manager_count_above_floor() -> None:
    peers = _peers("AAPL", 0.0)
    holdings = _holdings_payload(
        conviction_signal="accumulating",
        signal_score=0.7,
        manager_count=25,
    )
    result = institutional.compute_institutional(
        "AAPL", 0.0, peers, holdings_data=holdings
    )
    h = result["components"]["holdings"]
    assert h["coverage_scale"] == pytest.approx(1.0)
    assert h["adjustment_pts"] == pytest.approx(5.0)


def test_coverage_scale_proportional_below_floor() -> None:
    """manager_count == 7 -> scale = 0.7 -> 3 pts * 0.7 = 2.1."""
    peers = _peers("AAPL", 0.0)
    holdings = _holdings_payload(
        conviction_signal="accumulating",
        signal_score=0.4,
        manager_count=7,
    )
    result = institutional.compute_institutional(
        "AAPL", 0.0, peers, holdings_data=holdings
    )
    h = result["components"]["holdings"]
    assert h["coverage_scale"] == pytest.approx(0.7, abs=1e-6)
    # 3 pts (mild accumulating) * 0.7 = 2.1
    assert h["adjustment_pts"] == pytest.approx(2.1)


def test_coverage_scale_proportional_for_strong_distributing() -> None:
    """Negative side scales symmetrically: -3 * 0.6 = -1.8."""
    peers = _peers("AAPL", 0.0)
    holdings = _holdings_payload(
        conviction_signal="distributing",
        signal_score=-0.8,
        manager_count=6,
    )
    result = institutional.compute_institutional(
        "AAPL", 0.0, peers, holdings_data=holdings
    )
    h = result["components"]["holdings"]
    assert h["coverage_scale"] == pytest.approx(0.6, abs=1e-6)
    assert h["adjustment_pts"] == pytest.approx(-1.8)


def test_coverage_scale_default_when_manager_count_missing() -> None:
    """Legacy payloads (no manager_count) keep coverage_scale at 1.0
    so we don't regress old call-sites."""
    peers = _peers("AAPL", 0.0)
    holdings = _holdings_payload(
        conviction_signal="accumulating",
        signal_score=0.7,
    )
    holdings["manager_count"] = None
    # data_quality must stay non-sparse so the path is exercised.
    holdings["data_quality"] = "good"
    result = institutional.compute_institutional(
        "AAPL", 0.0, peers, holdings_data=holdings
    )
    h = result["components"]["holdings"]
    assert h["coverage_scale"] == pytest.approx(1.0)
    assert h["adjustment_pts"] == pytest.approx(5.0)


def test_coverage_scale_zero_does_not_introduce_division_or_negatives() -> None:
    """manager_count=0 with a steady label stays at 0 adj; the scale
    is technically 0 but the adjustment is zero so the visible scale
    surfaces as the computed 0 (no multiplication happened)."""
    peers = _peers("AAPL", 0.0)
    holdings = _holdings_payload(
        conviction_signal="steady",
        signal_score=0.0,
        manager_count=0,
        data_quality="partial",
    )
    result = institutional.compute_institutional(
        "AAPL", 0.0, peers, holdings_data=holdings
    )
    h = result["components"]["holdings"]
    assert h["adjustment_pts"] == 0.0
    # Steady -> adj==0 short-circuits before the scale multiplication,
    # so scale stays at the initial 1.0.
    assert h["coverage_scale"] == pytest.approx(1.0)


def test_pillar_surfaces_manager_universe_size_and_aum_pct() -> None:
    """The Phase 1 additive fields ride through to the component detail
    so the evidence-modal can show breadth context."""
    peers = _peers("AAPL", 0.0)
    holdings = _holdings_payload(
        manager_universe_size=50,
        tracked_aum_pct=0.4,
    )
    result = institutional.compute_institutional(
        "AAPL", 0.0, peers, holdings_data=holdings
    )
    h = result["components"]["holdings"]
    assert h["manager_universe_size"] == 50
    assert h["tracked_aum_pct"] == pytest.approx(0.4)


# --- End-to-end: name-alias fallback uses JSON-loaded aliases --------------

def test_build_name_lookup_uses_json_aliases() -> None:
    """The Phase 1 alias for a non-default ticker should now resolve."""
    # MRVL is not in the Phase-0 fallback name map but IS in the JSON.
    lookup = sec_13f._build_name_lookup(["MRVL", "MELI"])
    assert any(t == "MRVL" for t in lookup.values())
    assert any(t == "MELI" for t in lookup.values())


def test_build_cusip_lookup_covers_full_universe_from_json() -> None:
    """Every universe ticker should have a CUSIP-keyed entry — modulo
    legitimate share-class siblings (GOOG/GOOGL) that share a Class-C
    CUSIP. The CUSIP lookup is last-write-wins on collisions, so for
    each cusip we accept any of the share-class siblings as a valid
    resolver. We assert per ticker that AT LEAST ONE of its declared
    CUSIPs resolves back to that ticker OR to a known sibling pair.
    """
    with UNIVERSE_PATH.open() as fh:
        universe = json.load(fh)
    with CUSIP_MAP_PATH.open() as fh:
        cusip_map = json.load(fh)["tickers"]
    tickers = [t["ticker"] for t in universe["tickers"]]
    cusip_lookup = sec_13f._build_cusip_lookup(tickers)

    # Known legitimate share-class siblings: tickers that intentionally
    # share a CUSIP because they're the same legal class of stock.
    siblings = {
        "GOOG": {"GOOGL"},   # Class C CUSIP appears on both
        "GOOGL": {"GOOG"},
    }
    unresolved: list = []
    for ticker in tickers:
        entry = cusip_map.get(ticker, {})
        cusips = entry.get("cusips") or []
        if not cusips:
            # Tickers with no CUSIPs depend on name-alias fallback;
            # that's tested separately.
            continue
        resolved_for_ticker = False
        for c in cusips:
            norm = sec_13f._normalize_cusip(c)
            if norm and cusip_lookup.get(norm) == ticker:
                resolved_for_ticker = True
                break
            if norm and cusip_lookup.get(norm) in siblings.get(ticker, set()):
                resolved_for_ticker = True
                break
        if not resolved_for_ticker:
            unresolved.append(ticker)
    assert not unresolved, "tickers without CUSIP coverage: {}".format(unresolved)
