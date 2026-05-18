"""Tests for lthcs.peer_groups — compound peer-key resolver.

Covers:
* universe.json completeness against peer_groups.json
* curated split (AAPL -> tech_hardware, MSFT -> tech_software, JPM -> banks)
* compound key resolution
* safety-valve fallback chain (compound -> sector_group_only -> maturity_only -> universe)
* candidate_tickers filtering
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List

import pytest

from lthcs.peer_groups import (
    DEFAULT_PEER_GROUPS_PATH,
    DEFAULT_SECTOR_GROUP,
    STRATEGY_COMPOUND,
    STRATEGY_MATURITY_ONLY,
    STRATEGY_SECTOR_GROUP_ONLY,
    STRATEGY_UNIVERSE_FALLBACK,
    get_compound_peer_key,
    get_peer_cohort,
    get_peer_cohort_with_strategy,
    get_sector_group,
    load_peer_groups_config,
)


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
UNIVERSE_PATH = REPO_ROOT / "data" / "lthcs" / "universe.json"
PEER_GROUPS_PATH = REPO_ROOT / "data" / "lthcs" / "peer_groups.json"


# --- Fixtures ---------------------------------------------------------------


@pytest.fixture(scope="module")
def universe() -> Dict:
    with open(UNIVERSE_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture(scope="module")
def peer_groups_config() -> Dict:
    return load_peer_groups_config()


# --- Universe completeness ---------------------------------------------------


def test_every_universe_ticker_resolves_to_exactly_one_sector_group(
    universe: Dict, peer_groups_config: Dict
) -> None:
    """Audit invariant: every universe ticker is in exactly one curated group."""
    universe_tickers = {t["ticker"] for t in universe["tickers"]}

    # Build {ticker: [groups it appears in]} to surface accidental duplicates.
    membership: Dict[str, List[str]] = {}
    for group_name, group in peer_groups_config["sector_groups"].items():
        for tk in group.get("tickers", []):
            membership.setdefault(tk, []).append(group_name)

    duplicates = {tk: grps for tk, grps in membership.items() if len(grps) > 1}
    assert not duplicates, f"Tickers appearing in multiple groups: {duplicates}"

    # Every universe ticker is in at least one group (no "other" leftovers).
    missing = sorted(universe_tickers - set(membership))
    assert not missing, f"Universe tickers missing from any curated group: {missing}"


def test_no_uncurated_tickers_in_other_group(peer_groups_config: Dict) -> None:
    """The "other" group should be empty — every universe ticker is curated."""
    other = peer_groups_config["sector_groups"].get("other", {})
    assert other.get("tickers", []) == [], (
        f"Found uncurated tickers in 'other': {other.get('tickers')}"
    )


# --- Curated split sanity checks --------------------------------------------


def test_aapl_lands_in_tech_hardware(peer_groups_config: Dict) -> None:
    """AAPL is the load-bearing case — the audit-required curation puts it
    in tech_hardware, NOT in a Tech-bimodal cohort."""
    assert get_sector_group("AAPL", peer_groups_config) == "tech_hardware"


def test_msft_lands_in_tech_software(peer_groups_config: Dict) -> None:
    assert get_sector_group("MSFT", peer_groups_config) == "tech_software"


def test_nvda_lands_in_tech_hardware(peer_groups_config: Dict) -> None:
    """NVDA is a semi; goes in tech_hardware with AAPL/AVGO."""
    assert get_sector_group("NVDA", peer_groups_config) == "tech_hardware"


def test_jpm_lands_in_banks(peer_groups_config: Dict) -> None:
    assert get_sector_group("JPM", peer_groups_config) == "banks"


def test_visa_lands_in_financials_non_bank(peer_groups_config: Dict) -> None:
    """V is a payment network, not a bank — must land in the non-bank cohort."""
    assert get_sector_group("V", peer_groups_config) == "financials_non_bank"


def test_amzn_lands_in_tech_internet(peer_groups_config: Dict) -> None:
    """Amazon — internet-native discretionary, NOT consumer_cyclical."""
    assert get_sector_group("AMZN", peer_groups_config) == "tech_internet"


def test_unknown_ticker_falls_to_other(peer_groups_config: Dict) -> None:
    assert get_sector_group("ZZZZ", peer_groups_config) == DEFAULT_SECTOR_GROUP


# --- get_compound_peer_key --------------------------------------------------


def test_compound_key_aapl(universe: Dict, peer_groups_config: Dict) -> None:
    stage, group = get_compound_peer_key("AAPL", universe, peer_groups_config)
    assert stage == "mature_compounder"
    assert group == "tech_hardware"


def test_compound_key_nvda_growth(universe: Dict, peer_groups_config: Dict) -> None:
    """NVDA is growth_compounder (post v1.1.0 reclass) + tech_hardware."""
    stage, group = get_compound_peer_key("NVDA", universe, peer_groups_config)
    assert stage == "growth_compounder"
    assert group == "tech_hardware"


def test_compound_key_unknown_ticker(universe: Dict, peer_groups_config: Dict) -> None:
    stage, group = get_compound_peer_key("ZZZZ", universe, peer_groups_config)
    # Defaults: mature_compounder + other.
    assert stage == "mature_compounder"
    assert group == DEFAULT_SECTOR_GROUP


# --- get_peer_cohort: real universe ----------------------------------------


def test_aapl_compound_cohort_is_tech_hardware_mature(
    universe: Dict, peer_groups_config: Dict
) -> None:
    """AAPL should compare against other tech_hardware names that share its
    maturity stage (a real, curated cohort) rather than the bimodal
    stage-only bucket. Every member must satisfy BOTH axes when the strict
    compound strategy fires."""
    cohort, strategy = get_peer_cohort_with_strategy(
        "AAPL", universe, peer_groups_config
    )
    by_t = {t["ticker"]: t for t in universe["tickers"]}
    focal_stage = by_t["AAPL"]["maturity_stage"]
    hardware_tickers = set(
        peer_groups_config["sector_groups"]["tech_hardware"]["tickers"]
    )
    # When the compound strategy fires, every member shares stage AND group.
    if strategy == STRATEGY_COMPOUND:
        for tk in cohort:
            assert by_t[tk]["maturity_stage"] == focal_stage, (
                f"{tk} stage={by_t[tk]['maturity_stage']} != focal {focal_stage}"
            )
            assert tk in hardware_tickers, f"{tk} is not tech_hardware"
    else:
        # If we fell back, members must at least share the sector_group.
        for tk in cohort:
            assert tk in hardware_tickers, (
                f"{tk} is not tech_hardware (strategy={strategy})"
            )
    assert "AAPL" in cohort
    # Sanity: cohort isn't degenerate (≥6 by the default floor).
    assert len(cohort) >= 6


def test_msft_compound_cohort_is_tech_software(
    universe: Dict, peer_groups_config: Dict
) -> None:
    cohort, strategy = get_peer_cohort_with_strategy(
        "MSFT", universe, peer_groups_config
    )
    by_t = {t["ticker"]: t for t in universe["tickers"]}
    focal_stage = by_t["MSFT"]["maturity_stage"]
    software_tickers = set(
        peer_groups_config["sector_groups"]["tech_software"]["tickers"]
    )
    if strategy == STRATEGY_COMPOUND:
        for tk in cohort:
            assert by_t[tk]["maturity_stage"] == focal_stage
            assert tk in software_tickers
    else:
        for tk in cohort:
            assert tk in software_tickers
    assert "MSFT" in cohort
    assert len(cohort) >= 6


# --- Safety valve: synthetic small cohorts ---------------------------------


def _synthetic_universe(maturity_stages: Dict[str, str]) -> Dict:
    """Build a tiny universe.json-shaped dict for fallback-tests."""
    return {
        "tickers": [
            {"ticker": tk, "maturity_stage": stage, "active": True}
            for tk, stage in maturity_stages.items()
        ]
    }


def test_safety_valve_falls_back_to_sector_group_only() -> None:
    """A stage-of-1 within a sector_group should fall back to sector_group_only
    when the compound cohort is < min_cohort_size."""
    syn_universe = _synthetic_universe(
        {
            "A1": "growth_compounder",  # focal — lone growth in tech_hardware
            "A2": "mature_compounder",
            "A3": "mature_compounder",
            "A4": "mature_compounder",
            "A5": "mature_compounder",
            "A6": "mature_compounder",
            "A7": "mature_compounder",
        }
    )
    syn_config = {
        "min_cohort_size": 6,
        "sector_groups": {
            "tech_hardware": {
                "tickers": ["A1", "A2", "A3", "A4", "A5", "A6", "A7"],
            }
        },
    }
    cohort, strategy = get_peer_cohort_with_strategy(
        "A1", syn_universe, syn_config
    )
    # Compound (growth_compounder + tech_hardware) = 1 -> too small.
    # Fall back to sector_group_only (any maturity in tech_hardware) = 7.
    assert strategy == STRATEGY_SECTOR_GROUP_ONLY
    assert len(cohort) == 7
    assert "A1" in cohort


def test_safety_valve_falls_back_to_maturity_only() -> None:
    """When sector_group is tiny (< floor) but maturity stage is large, fall
    back to maturity_only."""
    syn_universe = _synthetic_universe(
        {
            "X1": "mature_compounder",  # focal in tiny sector
            "X2": "mature_compounder",
            "Y1": "mature_compounder",
            "Y2": "mature_compounder",
            "Y3": "mature_compounder",
            "Y4": "mature_compounder",
            "Y5": "mature_compounder",
            "Y6": "mature_compounder",
            "Y7": "mature_compounder",
        }
    )
    syn_config = {
        "min_cohort_size": 6,
        "sector_groups": {
            "tiny_sector": {"tickers": ["X1", "X2"]},
            "big_sector": {
                "tickers": ["Y1", "Y2", "Y3", "Y4", "Y5", "Y6", "Y7"]
            },
        },
    }
    cohort, strategy = get_peer_cohort_with_strategy(
        "X1", syn_universe, syn_config
    )
    # Compound: stage=mature + group=tiny_sector -> 2 (too small)
    # sector_group_only: tiny_sector -> 2 (still too small)
    # maturity_only: mature_compounder -> 9 (good)
    assert strategy == STRATEGY_MATURITY_ONLY
    assert len(cohort) == 9


def test_safety_valve_universe_fallback() -> None:
    """When even maturity-only is too small, fall back to universe."""
    syn_universe = _synthetic_universe(
        {
            "Z1": "exotic_stage_one",  # focal
            "Z2": "exotic_stage_two",
            "Z3": "exotic_stage_three",
            "Z4": "exotic_stage_four",
            "Z5": "exotic_stage_five",
        }
    )
    syn_config = {
        "min_cohort_size": 6,
        "sector_groups": {
            "tiny": {"tickers": ["Z1", "Z2", "Z3", "Z4", "Z5"]},
        },
    }
    cohort, strategy = get_peer_cohort_with_strategy(
        "Z1", syn_universe, syn_config
    )
    # Compound: 1 (alone) — too small
    # sector_group_only: 5 — too small
    # maturity_only: 1 — too small
    # universe_fallback: all 5.
    assert strategy == STRATEGY_UNIVERSE_FALLBACK
    assert len(cohort) == 5
    assert "Z1" in cohort


# --- candidate_tickers filtering -------------------------------------------


def test_candidate_tickers_restricts_cohort(
    universe: Dict, peer_groups_config: Dict
) -> None:
    """When candidate_tickers is provided, the cohort is restricted to it."""
    # Restrict to a tiny candidate set that still satisfies the floor.
    cands = ["AAPL", "AVGO", "NVDA", "AMD", "INTC", "QCOM", "MU"]  # 7 hardware
    cohort, strategy = get_peer_cohort_with_strategy(
        "AAPL",
        universe,
        peer_groups_config,
        candidate_tickers=cands,
    )
    # Every cohort member must be in the candidate list.
    for tk in cohort:
        assert tk in cands
    # AAPL is mature_compounder; AVGO and INTC are mature; NVDA/AMD/MU are
    # growth_compounder. So compound cohort = {AAPL, AVGO, INTC, QCOM} which
    # is 4 — below floor of 6. Should fall back to sector_group_only.
    assert strategy == STRATEGY_SECTOR_GROUP_ONLY
    assert "AAPL" in cohort
    assert "NVDA" in cohort  # different stage but same sector_group


def test_get_peer_cohort_returns_just_the_list(
    universe: Dict, peer_groups_config: Dict
) -> None:
    """Thin wrapper exercise — no strategy in the return."""
    cohort = get_peer_cohort("AAPL", universe, peer_groups_config)
    assert isinstance(cohort, list)
    assert "AAPL" in cohort


def test_get_peer_cohort_overrides_min_cohort_size() -> None:
    """When min_cohort_size is explicitly higher than the config default,
    use the explicit value."""
    syn_universe = _synthetic_universe(
        {
            "T1": "mature_compounder",
            "T2": "mature_compounder",
            "T3": "mature_compounder",
            "T4": "mature_compounder",
            "T5": "mature_compounder",
        }
    )
    syn_config = {
        "min_cohort_size": 3,  # default
        "sector_groups": {
            "grp": {"tickers": ["T1", "T2", "T3", "T4", "T5"]}
        },
    }
    # With the config default (3) the compound cohort of 5 wins.
    cohort_default, strat_default = get_peer_cohort_with_strategy(
        "T1", syn_universe, syn_config
    )
    assert strat_default == STRATEGY_COMPOUND
    assert len(cohort_default) == 5

    # Bumping the floor to 10 forces fallback (universe).
    cohort_high, strat_high = get_peer_cohort_with_strategy(
        "T1", syn_universe, syn_config, min_cohort_size=10
    )
    assert strat_high == STRATEGY_UNIVERSE_FALLBACK
