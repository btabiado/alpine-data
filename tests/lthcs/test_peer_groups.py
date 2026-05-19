"""Tests for lthcs.peer_groups — compound peer-key resolver.

Covers:
* universe.json completeness against peer_groups.json
* curated split (AAPL -> tech_hardware, NVDA -> tech_semiconductors,
  MSFT -> tech_software, ACN -> tech_it_services, JPM -> banks)
* compound key resolution (3-tuple for Tech, 2-tuple for non-Tech)
* safety-valve fallback chain (compound -> sector_group_only -> maturity_only -> universe)
* candidate_tickers filtering
* Hardware/Software split regression guard (docs/lthcs-tech-hardware-software-split.md)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List

import pytest

from lthcs.peer_groups import (
    ALLOWED_TECH_SUB_BUCKETS,
    DEFAULT_PEER_GROUPS_PATH,
    DEFAULT_SECTOR_GROUP,
    STRATEGY_COMPOUND,
    STRATEGY_MATURITY_ONLY,
    STRATEGY_SECTOR_GROUP_ONLY,
    STRATEGY_UNIVERSE_FALLBACK,
    TECH_SECTORS,
    get_compound_peer_key,
    get_peer_cohort,
    get_peer_cohort_with_strategy,
    get_sector_group,
    get_tech_sub_bucket,
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


def test_nvda_lands_in_tech_semiconductors(peer_groups_config: Dict) -> None:
    """Post v1.1.0 split: NVDA is in tech_semiconductors, peeled out of the
    legacy tech_hardware cohort that lumped semis with AAPL. See
    docs/lthcs-tech-hardware-software-split.md §4."""
    assert get_sector_group("NVDA", peer_groups_config) == "tech_semiconductors"


def test_acn_lands_in_tech_it_services(peer_groups_config: Dict) -> None:
    """Post v1.1.0 split: ACN (and CDW, CTSH, IBM) is in the curated
    tech_it_services cohort, peeled out of tech_software."""
    assert get_sector_group("ACN", peer_groups_config) == "tech_it_services"


def test_csco_lands_in_tech_hardware(peer_groups_config: Dict) -> None:
    """Post v1.1.0: CSCO migrates from tech_software (where it lived for
    scale-economics reasons) to tech_hardware alongside AAPL and SMCI."""
    assert get_sector_group("CSCO", peer_groups_config) == "tech_hardware"


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
    """AAPL is Tech → 3-tuple ending in 'Hardware' (spec §7 case 5)."""
    key = get_compound_peer_key("AAPL", universe, peer_groups_config)
    assert len(key) == 3
    stage, group, sub_bucket = key
    assert stage == "mature_compounder"
    assert group == "tech_hardware"
    assert sub_bucket == "Hardware"


def test_compound_key_nvda_growth(universe: Dict, peer_groups_config: Dict) -> None:
    """NVDA is Tech / growth_compounder → 3-tuple in tech_semiconductors."""
    key = get_compound_peer_key("NVDA", universe, peer_groups_config)
    assert len(key) == 3
    stage, group, sub_bucket = key
    assert stage == "growth_compounder"
    assert group == "tech_semiconductors"
    assert sub_bucket == "Semiconductors"


def test_compound_key_jpm_is_two_tuple(universe: Dict, peer_groups_config: Dict) -> None:
    """Non-Tech focals keep the legacy 2-tuple (spec §7 case 5)."""
    key = get_compound_peer_key("JPM", universe, peer_groups_config)
    assert len(key) == 2
    stage, group = key
    assert group == "banks"


def test_compound_key_unknown_ticker(universe: Dict, peer_groups_config: Dict) -> None:
    """Unknown ticker → 2-tuple (sector unknown, can't be Tech)."""
    key = get_compound_peer_key("ZZZZ", universe, peer_groups_config)
    assert len(key) == 2
    stage, group = key
    # Defaults: mature_compounder + other.
    assert stage == "mature_compounder"
    assert group == DEFAULT_SECTOR_GROUP


# --- get_peer_cohort: real universe ----------------------------------------


def test_aapl_cohort_cascades_through_split(
    universe: Dict, peer_groups_config: Dict
) -> None:
    """Post v1.1.0 split: AAPL's tech_hardware cohort is n=3
    {AAPL, CSCO, SMCI} — intentionally below the n=6 floor (spec §3-4).
    Compound (Hardware × mature) collapses to {AAPL, CSCO} → fails. The
    resolver cascades through STRATEGY_SECTOR_GROUP_ONLY (still 3) → lands
    at STRATEGY_MATURITY_ONLY. This is the audit-prescribed behaviour that
    closes the AAPL-vs-NVDA bimodality."""
    cohort, strategy = get_peer_cohort_with_strategy(
        "AAPL", universe, peer_groups_config
    )
    # Either maturity_only or universe_fallback is acceptable depending on
    # how many mature_compounders are in the universe — but it MUST NOT be
    # compound or sector_group_only (those have <6 members for Hardware).
    assert strategy in {STRATEGY_MATURITY_ONLY, STRATEGY_UNIVERSE_FALLBACK}
    assert "AAPL" in cohort
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
    """When candidate_tickers is provided, the cohort is restricted to it.

    Post v1.1.0 split: MSFT is mature_compounder + tech_software. Pick a
    candidate set of all mature Software tickers — compound clears the
    n=6 floor without falling through."""
    cands = ["MSFT", "ORCL", "ADBE", "CRM", "INTU", "NOW", "PANW", "FTNT", "CRWD"]
    cohort, strategy = get_peer_cohort_with_strategy(
        "MSFT",
        universe,
        peer_groups_config,
        candidate_tickers=cands,
    )
    # Every cohort member must be in the candidate list.
    for tk in cohort:
        assert tk in cands
    # Compound (mature_compounder × tech_software × Software) clears n=6.
    assert strategy == STRATEGY_COMPOUND
    assert "MSFT" in cohort
    assert len(cohort) >= 6


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


# --- Hardware/Software split: spec §7 coverage ------------------------------
# docs/lthcs-tech-hardware-software-split.md §7 test plan.


def test_schema_every_tech_ticker_has_allowed_tech_sub_bucket(
    universe: Dict,
) -> None:
    """Spec §7.1 — every Tech ticker carries a curated ``tech_sub_bucket``
    in the allowed set; no non-Tech ticker has the field.

    The HW/SW split keys on this universe.json field — a missing or stray
    value silently collapses the 3-tuple key back to the 2-tuple legacy
    path, defeating the split. This test guards the schema invariant.
    """
    missing: List[str] = []
    bad: List[str] = []
    leaked: List[str] = []
    for entry in universe["tickers"]:
        ticker = entry["ticker"]
        sector = entry.get("sector")
        bucket = entry.get("tech_sub_bucket")
        if sector in TECH_SECTORS:
            if not bucket:
                missing.append(ticker)
            elif bucket not in ALLOWED_TECH_SUB_BUCKETS:
                bad.append(f"{ticker}={bucket!r}")
        else:
            if "tech_sub_bucket" in entry:
                leaked.append(f"{ticker}({sector})={bucket!r}")
    assert not missing, f"Tech tickers missing tech_sub_bucket: {missing}"
    assert not bad, (
        f"Tech tickers with disallowed tech_sub_bucket "
        f"(expected one of {sorted(ALLOWED_TECH_SUB_BUCKETS)}): {bad}"
    )
    assert not leaked, (
        f"Non-Tech tickers carry tech_sub_bucket (should be Tech-only): {leaked}"
    )


def test_schema_universe_version_bumped_for_hw_sw_split(universe: Dict) -> None:
    """Spec §5 — universe.json version must be ≥2.2.0 once the
    tech_sub_bucket field is added. Guards against silent reverts."""
    version = universe.get("version", "0.0.0")
    parts = tuple(int(x) for x in version.split("."))
    assert parts >= (2, 2, 0), (
        f"universe.json version {version} predates the HW/SW split (need ≥2.2.0)"
    )


def test_get_tech_sub_bucket_for_each_split_bucket(universe: Dict) -> None:
    """Spec §2 — representative ticker from each sub-bucket resolves."""
    assert get_tech_sub_bucket("AAPL", universe) == "Hardware"
    assert get_tech_sub_bucket("CSCO", universe) == "Hardware"
    assert get_tech_sub_bucket("SMCI", universe) == "Hardware"
    assert get_tech_sub_bucket("NVDA", universe) == "Semiconductors"
    assert get_tech_sub_bucket("AMD", universe) == "Semiconductors"
    assert get_tech_sub_bucket("AVGO", universe) == "Semiconductors"
    assert get_tech_sub_bucket("MSFT", universe) == "Software"
    assert get_tech_sub_bucket("CRWD", universe) == "Software"  # cyber → software
    assert get_tech_sub_bucket("CDNS", universe) == "Software"  # EDA → software
    assert get_tech_sub_bucket("ACN", universe) == "IT Services"
    assert get_tech_sub_bucket("IBM", universe) == "IT Services"
    # Non-Tech ticker returns None.
    assert get_tech_sub_bucket("JPM", universe) is None
    assert get_tech_sub_bucket("AMZN", universe) is None  # Consumer Discretionary
    # Unknown ticker returns None (no entry → no sector → not Tech).
    assert get_tech_sub_bucket("ZZZZ", universe) is None


def test_cohort_size_floor_semiconductors_and_software_clear(
    peer_groups_config: Dict,
) -> None:
    """Spec §7.2 — Semiconductors (18) and Software (18) clear the n=6
    floor; Hardware (3) and IT Services (4) intentionally fall below and
    cascade to ``STRATEGY_MATURITY_ONLY``."""
    groups = peer_groups_config["sector_groups"]
    floor = int(peer_groups_config.get("min_cohort_size", 6))
    assert len(groups["tech_semiconductors"]["tickers"]) >= floor
    assert len(groups["tech_software"]["tickers"]) >= floor
    # Document the by-design sub-floor cases so a future reshuffling that
    # grows these cohorts breaks loudly and forces re-thinking the cascade.
    assert len(groups["tech_hardware"]["tickers"]) < floor, (
        "Hardware is expected to fall below floor (n=3 per spec §3); "
        "growing it past 6 would change the cascade behaviour — re-check."
    )
    assert len(groups["tech_it_services"]["tickers"]) < floor, (
        "IT Services is expected to fall below floor (n=4 per spec §3); "
        "growing it past 6 would change the cascade behaviour — re-check."
    )


def test_distribution_software_subbucket_tighter_than_parent_tech() -> None:
    """Spec §7.3 — the Software sub-bucket's adoption_momentum stdev should
    be tighter than the parent ALL-TECH stdev once the split lands.

    We read the latest snapshot (2026-05-18 per spec) and compute the
    deterministic stdev. Software (sd 22.8) < ALL TECH (sd 25.8) ✓ per
    spec §3. Semiconductors (sd 29.2 ✗) is intentionally NOT asserted —
    the cycle bimodality is handled by the maturity_stage axis cascade.
    """
    import statistics

    snapshot_path = REPO_ROOT / "data" / "lthcs" / "snapshots" / "2026-05-18.json"
    if not snapshot_path.exists():
        pytest.skip(f"snapshot {snapshot_path.name} not present")

    with open(snapshot_path, "r", encoding="utf-8") as fh:
        snapshot = json.load(fh)
    with open(UNIVERSE_PATH, "r", encoding="utf-8") as fh:
        universe = json.load(fh)
    by_t = {t["ticker"]: t for t in universe["tickers"]}

    parent: List[float] = []
    software: List[float] = []
    for row in snapshot["scores"]:
        info = by_t.get(row["ticker"], {})
        if info.get("sector") not in TECH_SECTORS:
            continue
        adopt = row["subscores"].get("adoption_momentum")
        if adopt is None:
            continue
        parent.append(float(adopt))
        if info.get("tech_sub_bucket") == "Software":
            software.append(float(adopt))

    # Need enough sample to compute stdev meaningfully.
    assert len(parent) >= 10, f"parent Tech sample too thin: {len(parent)}"
    assert len(software) >= 6, f"Software sample too thin: {len(software)}"

    parent_sd = statistics.stdev(parent)
    software_sd = statistics.stdev(software)
    assert software_sd < parent_sd, (
        f"Software sub-bucket stdev {software_sd:.2f} should be tighter than "
        f"parent Tech stdev {parent_sd:.2f} per spec §3 verdict"
    )


def test_aapl_cohort_excludes_peak_cycle_growth_semis(
    universe: Dict, peer_groups_config: Dict
) -> None:
    """Spec §7.4 — AAPL's resolved cohort must NOT contain the peak-cycle
    *growth_compounder* semis that drove the original bimodality
    (NVDA, AMD, MU, MRVL, SMCI). Strategy lands at ``maturity_only``
    after cascading through the n=2 compound cell and the n=3 Hardware
    sector_group cell (spec §3).

    Note: the maturity_only cascade legitimately includes
    ``mature_compounder`` semis (AVGO, QCOM, INTC). The split's job is
    not to exclude every semi from AAPL's cohort — it's to drop the
    +40%-growth bimodal tail that was pulling AAPL's percentile to 13.2.
    That tail is exactly the growth_compounder semis listed in spec §1.
    """
    cohort, strategy = get_peer_cohort_with_strategy(
        "AAPL", universe, peer_groups_config
    )
    assert strategy == STRATEGY_MATURITY_ONLY, (
        f"expected AAPL to land at maturity_only after cascade; got {strategy}"
    )
    # Growth-compounder peak-cycle semis must NOT pollute AAPL's cohort.
    forbidden_growth_semis = {"NVDA", "AMD", "MU", "MRVL", "SMCI"}
    overlap = forbidden_growth_semis & set(cohort)
    assert not overlap, (
        f"AAPL cohort leaks growth_compounder peak-cycle semis (bimodality "
        f"tail that the split is supposed to remove): {overlap}"
    )
    assert "AAPL" in cohort


def test_aapl_resolves_to_three_tuple_smoke(
    universe: Dict, peer_groups_config: Dict
) -> None:
    """Spec §7.5 smoke — ``get_compound_peer_key("AAPL", ...)`` returns a
    3-tuple ending in ``"Hardware"``; ``"JPM"`` returns a 2-tuple."""
    aapl_key = get_compound_peer_key("AAPL", universe, peer_groups_config)
    assert isinstance(aapl_key, tuple) and len(aapl_key) == 3
    assert aapl_key[-1] == "Hardware"

    jpm_key = get_compound_peer_key("JPM", universe, peer_groups_config)
    assert isinstance(jpm_key, tuple) and len(jpm_key) == 2
