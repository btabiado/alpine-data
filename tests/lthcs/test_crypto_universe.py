"""Tests for the expanded crypto universe (Tier 5 #27 Phase 5).

These tests pin the on-disk ``data/lthcs/crypto_universe.json`` contract
that V1 commits ship: a 10-asset large-cap roster with consistent
schema, every asset addressable via the CoinGecko ID map, every weight
profile present in ``weights.json`` summing to 1.0, and every entry
producing a row when the crypto runner iterates the universe offline.

Scoped to this file so a parallel pytest run is cheap::

    pytest tests/lthcs/test_crypto_universe.py -q

Phase 5 expanded the universe from 3 -> 10 assets. If we ever shrink it
or unify schemas, expect this file to update in lockstep.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
UNIVERSE_PATH = REPO_ROOT / "data" / "lthcs" / "crypto_universe.json"
WEIGHTS_PATH = REPO_ROOT / "data" / "lthcs" / "weights.json"


# ---------------------------------------------------------------------------
# Universe schema
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def universe() -> Dict[str, Any]:
    with UNIVERSE_PATH.open("r", encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture(scope="module")
def weights() -> Dict[str, Any]:
    with WEIGHTS_PATH.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def test_universe_has_at_least_eight_assets(universe: Dict[str, Any]) -> None:
    """Phase 5 target: 8-10 mature large-caps. Floor at 8 so accidental
    deletions are caught (we shipped 10)."""
    assets = universe.get("assets") or []
    assert isinstance(assets, list)
    assert len(assets) >= 8, "expected at least 8 large-cap assets; got %d" % len(assets)


def test_universe_required_fields(universe: Dict[str, Any]) -> None:
    """Every asset has the runner's mandatory fields."""
    required = {"symbol", "name", "active", "weight_profile"}
    for a in universe["assets"]:
        missing = required - set(a.keys())
        assert not missing, "asset %r missing fields: %s" % (a.get("symbol"), missing)


def test_universe_symbols_unique_and_uppercase(universe: Dict[str, Any]) -> None:
    seen = set()
    for a in universe["assets"]:
        sym = a["symbol"]
        assert isinstance(sym, str) and sym == sym.upper(), \
            "symbol must be uppercase string: %r" % sym
        assert sym not in seen, "duplicate symbol in universe: %s" % sym
        seen.add(sym)


def test_universe_covers_phase5_targets(universe: Dict[str, Any]) -> None:
    """The user-named Phase 5 additions are all present."""
    syms = {a["symbol"] for a in universe["assets"]}
    expected_baseline = {"BTC", "ETH", "SOL"}
    expected_additions = {"ADA", "AVAX", "DOT", "LINK", "POL", "XRP", "DOGE"}
    assert expected_baseline.issubset(syms), \
        "lost a baseline asset: %s" % (expected_baseline - syms)
    assert expected_additions.issubset(syms), \
        "missing Phase 5 additions: %s" % (expected_additions - syms)


def test_universe_classifications_are_known(universe: Dict[str, Any]) -> None:
    """When a classification is set it must be from the schema enum."""
    schema = universe.get("schema") or {}
    enum = set(schema.get("classification_enum") or [])
    assert enum, "schema.classification_enum must be populated"
    for a in universe["assets"]:
        c = a.get("classification")
        if c is None:
            continue
        assert c in enum, "asset %s has unknown classification %r" % (a["symbol"], c)


def test_universe_coingecko_ids_match_adapter_map(universe: Dict[str, Any]) -> None:
    """Every asset's ``coingecko_id`` is wired in the adapter's
    ``COINGECKO_IDS`` map (so the daily runner can fetch a market block
    for it). Conversely, every symbol that ships in the universe must be
    addressable by the map -- the runner reads the map keys to assemble
    the batched CoinGecko call.
    """
    from lthcs.sources.crypto_data import COINGECKO_IDS

    for a in universe["assets"]:
        sym = a["symbol"]
        assert sym in COINGECKO_IDS, \
            "universe has %s but lthcs.sources.crypto_data.COINGECKO_IDS lacks it" % sym
        cg_id = a.get("coingecko_id")
        if cg_id is not None:
            assert COINGECKO_IDS[sym] == cg_id, (
                "universe lists %s as %r but adapter maps it to %r"
                % (sym, cg_id, COINGECKO_IDS[sym])
            )


def test_universe_weight_profiles_resolve(
    universe: Dict[str, Any], weights: Dict[str, Any]
) -> None:
    """Every named weight_profile is defined in weights.json with a
    5-element vector summing to 1.0 (the equity & crypto pipelines share
    this invariant)."""
    profiles = weights.get("profiles") or {}
    for a in universe["assets"]:
        prof = a["weight_profile"]
        assert prof in profiles, "weight_profile %r for %s missing from weights.json" % (prof, a["symbol"])
        vec = profiles[prof]
        assert isinstance(vec, list) and len(vec) == 5, \
            "profile %s must be a 5-element list" % prof
        s = sum(float(x) for x in vec)
        assert abs(s - 1.0) < 1e-6, "profile %s weights sum to %.6f != 1.0" % (prof, s)


def test_new_phase5_profiles_present(weights: Dict[str, Any]) -> None:
    """The five new crypto profiles introduced in Phase 5 are present."""
    profiles = weights.get("profiles") or {}
    for new_prof in ("layer_1_alt", "oracle_defi", "layer_2", "payments", "meme"):
        assert new_prof in profiles, "missing Phase 5 profile %s" % new_prof


# ---------------------------------------------------------------------------
# End-to-end: pipeline produces one row per universe entry
# ---------------------------------------------------------------------------

def test_runner_scores_all_universe_assets_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    universe: Dict[str, Any], weights: Dict[str, Any],
) -> None:
    """End-to-end: feed the real production universe + weights to the
    runner in offline mode, confirm we get one snapshot row per active
    asset and that the data_quality_flags ratchet down to the expected
    set (``thesis_unavailable`` since no funding/L-S data is wired for
    most coins yet; that's by design).
    """
    from scripts import lthcs_crypto_daily as runner
    from lthcs import persist as persist_mod

    # Pin the runner to the real universe + weights files.
    snap_dir = tmp_path / "snapshots_crypto"
    monkeypatch.setattr(runner, "_DEFAULT_SNAPSHOT_DIR", snap_dir)
    monkeypatch.setattr(persist_mod, "get_default_data_root",
                        lambda: tmp_path / "lthcs_data")
    monkeypatch.setenv("LTHCS_CACHE_DIR", str(tmp_path / "cache"))

    rc = runner.run([
        "--offline",
        "--universe", str(UNIVERSE_PATH),
        "--weights", str(WEIGHTS_PATH),
        "--calc-date", "2026-05-19",
    ])
    assert rc == 0

    snap = json.loads((snap_dir / "2026-05-19.json").read_text())
    rows = snap["scores"]
    active_syms = {
        a["symbol"] for a in universe["assets"] if a.get("active", False)
    }
    row_syms = {r["ticker"] for r in rows}
    assert row_syms == active_syms, (
        "snapshot row set %s does not match active universe %s"
        % (row_syms, active_syms)
    )

    # Every row has the 5 expected pillar subscores.
    expected_pillars = {
        "adoption_momentum", "institutional_confidence",
        "financial_evolution", "thesis_integrity", "des",
    }
    for r in rows:
        assert set(r["subscores"].keys()) == expected_pillars, \
            "row %s missing pillars: %s" % (r["ticker"], r["subscores"])

    # Every offline row carries ``thesis_unavailable`` because
    # data/market.json (offline) yields no funding/L-S data. Younger
    # assets with no whale-cohort proxy / ETF flows still produce a
    # bounded score via the renorm path -- the score must be a finite
    # 0-100 float regardless.
    for r in rows:
        assert "thesis_unavailable" in r["data_quality_flags"], r["ticker"]
        assert 0.0 <= float(r["lthcs_score"]) <= 100.0, r["ticker"]


def test_runner_applies_new_profile_weights_to_new_asset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A new-classification asset (LINK -> oracle_defi) actually picks up
    its profile vector at runtime. Pins the documented_weights -> profile
    dispatch path the runner uses (``get_maturity_weights``).
    """
    from scripts import lthcs_crypto_daily as runner
    from lthcs import persist as persist_mod

    universe_path = tmp_path / "crypto_universe.json"
    universe_path.write_text(json.dumps({
        "version": "test",
        "assets": [
            {"symbol": "LINK", "name": "Chainlink",
             "weight_profile": "oracle_defi", "active": True},
        ],
    }))
    weights_path = tmp_path / "weights.json"
    weights_path.write_text(json.dumps({
        "profiles": {
            "oracle_defi": [0.25, 0.20, 0.25, 0.15, 0.15],
        },
        "score_bands": {"review": {"min": 0, "max": 100}},
    }))

    snap_dir = tmp_path / "snapshots_crypto"
    monkeypatch.setattr(runner, "_DEFAULT_SNAPSHOT_DIR", snap_dir)
    monkeypatch.setattr(persist_mod, "get_default_data_root",
                        lambda: tmp_path / "lthcs_data")
    monkeypatch.setenv("LTHCS_CACHE_DIR", str(tmp_path / "cache"))

    rc = runner.run([
        "--offline",
        "--universe", str(universe_path),
        "--weights", str(weights_path),
        "--calc-date", "2026-05-19",
    ])
    assert rc == 0

    snap = json.loads((snap_dir / "2026-05-19.json").read_text())
    assert len(snap["scores"]) == 1
    row = snap["scores"][0]
    assert row["ticker"] == "LINK"
    assert row["maturity_stage"] == "oracle_defi"
    assert row["weights_used"] == [0.25, 0.20, 0.25, 0.15, 0.15]


def test_supply_inflation_defaults_cover_all_universe_symbols(
    universe: Dict[str, Any],
) -> None:
    """The Financial pillar's supply-inflation fallback must cover every
    symbol in the universe; otherwise the supply component drops and the
    pillar over-rotates onto the realized-cap proxy."""
    from lthcs.pillars.crypto_financial import _DEFAULT_SUPPLY_INFLATION

    for a in universe["assets"]:
        sym = a["symbol"]
        assert sym in _DEFAULT_SUPPLY_INFLATION, \
            "no default supply-inflation for %s" % sym
        v = _DEFAULT_SUPPLY_INFLATION[sym]
        assert isinstance(v, (int, float))
        # 0% is acceptable (LINK, XRP have fixed caps). Reject negatives /
        # > 25% (would be a typo).
        assert 0.0 <= float(v) <= 25.0, "supply-inflation %r out of range" % v
