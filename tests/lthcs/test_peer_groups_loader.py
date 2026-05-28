"""Tests for lthcs.peer_groups_loader.

Covers:
* Loading + schema validation of ``data/lthcs/peer_groups.json``.
* ``get_pillar_strategy`` lookup + default-strategy fallback.
* ``is_active`` kill-switch default behaviour.
* ``resolve_cohort`` for every strategy: ``compound``,
  ``sector_group_only``, ``maturity_only``, ``universe``.
* Fallback chain: when the primary cohort underflows
  ``min_cohort_size`` we walk to the next step.
* ``bank_override``: focal banks always return the bank cohort
  regardless of the compound rule.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest

from lthcs.peer_groups_loader import DEFAULT_STRATEGY, get_pillar_strategy, is_active, load_peer_groups_config, resolve_cohort


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _write(tmp_path: Path, payload: Dict[str, Any]) -> Path:
    p = tmp_path / "peer_groups.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


@pytest.fixture
def synthetic_config() -> Dict[str, Any]:
    """Tiny self-contained config — enough peers per group to clear floors."""

    return {
        "version": "1.0.0",
        "sector_groups": {
            "tech_hardware": {
                "tickers": [
                    "AAPL", "NVDA", "AMD", "INTC", "MU",
                    "AVGO", "QCOM", "TXN",
                ],
            },
            "tech_software": {
                "tickers": [
                    "MSFT", "ORCL", "CRM", "ADBE", "NOW",
                    "CRWD", "PANW", "DDOG",
                ],
            },
            "banks": {
                "tickers": ["JPM", "BAC", "C", "WFC", "GS", "MS", "USB", "BK"],
            },
            "tiny": {
                "tickers": ["LIN", "SHW"],
            },
        },
        "pillar_strategies": {
            "adoption_momentum": {
                "strategy": "compound",
                "primary_axis": "sector_group",
                "fallback_chain": [
                    "sector_group_only",
                    "maturity_only",
                    "universe",
                ],
                "min_cohort_size": 6,
            },
            "institutional_confidence": {"strategy": "universe"},
            "financial_evolution": {
                "strategy": "compound",
                "fallback_chain": [
                    "sector_group_only",
                    "maturity_only",
                    "universe",
                ],
                "bank_override": "bank_cohort",
                "min_cohort_size": 6,
            },
            "des": {"strategy": "sector_group_only"},
            "by_maturity": {"strategy": "maturity_only"},
        },
        "active_overrides": {"enabled": False},
    }


@pytest.fixture
def synthetic_universe() -> Dict[str, Any]:
    """Mirrors the cohort layout of ``synthetic_config``.

    Maturity-stage layout chosen so that:
      * AAPL: mature_compounder, tech_hardware  -> compound cohort = 4
        (NVDA growth, AMD growth, INTC recovery, MU growth would NOT
         be mature_compounder — keep most mature so cohort underflows
         the n=6 floor and we exercise the fallback chain).
      * MSFT: mature_compounder, tech_software  -> compound cohort big.
      * JPM:  mature_compounder, banks          -> bank_override fires.
      * LIN:  mature_compounder, "tiny"         -> tiny cohort, falls
        through everything until maturity_only.
    """

    return {
        "tickers": [
            # tech_hardware
            {"ticker": "AAPL", "maturity_stage": "mature_compounder"},
            {"ticker": "AVGO", "maturity_stage": "mature_compounder"},
            {"ticker": "TXN",  "maturity_stage": "mature_compounder"},
            {"ticker": "QCOM", "maturity_stage": "mature_compounder"},
            {"ticker": "INTC", "maturity_stage": "recovery_stabilization"},
            {"ticker": "NVDA", "maturity_stage": "growth_compounder"},
            {"ticker": "AMD",  "maturity_stage": "growth_compounder"},
            {"ticker": "MU",   "maturity_stage": "growth_compounder"},
            # tech_software (all mature so compound clears the n=6 floor)
            {"ticker": "MSFT", "maturity_stage": "mature_compounder"},
            {"ticker": "ORCL", "maturity_stage": "mature_compounder"},
            {"ticker": "CRM",  "maturity_stage": "mature_compounder"},
            {"ticker": "ADBE", "maturity_stage": "mature_compounder"},
            {"ticker": "NOW",  "maturity_stage": "mature_compounder"},
            {"ticker": "CRWD", "maturity_stage": "mature_compounder"},
            {"ticker": "PANW", "maturity_stage": "mature_compounder"},
            {"ticker": "DDOG", "maturity_stage": "growth_compounder"},
            # banks
            {"ticker": "JPM", "maturity_stage": "mature_compounder"},
            {"ticker": "BAC", "maturity_stage": "mature_compounder"},
            {"ticker": "C",   "maturity_stage": "mature_compounder"},
            {"ticker": "WFC", "maturity_stage": "mature_compounder"},
            {"ticker": "GS",  "maturity_stage": "mature_compounder"},
            {"ticker": "MS",  "maturity_stage": "mature_compounder"},
            {"ticker": "USB", "maturity_stage": "mature_compounder"},
            {"ticker": "BK",  "maturity_stage": "mature_compounder"},
            # tiny
            {"ticker": "LIN", "maturity_stage": "mature_compounder"},
            {"ticker": "SHW", "maturity_stage": "mature_compounder"},
        ]
    }


# ---------------------------------------------------------------------------
# 1. load_peer_groups_config — real file + schema errors
# ---------------------------------------------------------------------------


def test_load_real_repo_config_smoke() -> None:
    """The real ``data/lthcs/peer_groups.json`` parses + validates."""

    cfg = load_peer_groups_config()
    assert "sector_groups" in cfg
    assert "pillar_strategies" in cfg
    # Owned-by-#10 blocks are present
    assert "active_overrides" in cfg
    assert isinstance(cfg["pillar_strategies"], dict)
    # All five pillars wired
    expected_pillars = {
        "adoption_momentum",
        "institutional_confidence",
        "financial_evolution",
        "thesis_integrity",
        "des",
    }
    assert expected_pillars.issubset(set(cfg["pillar_strategies"].keys()))


def test_load_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_peer_groups_config(tmp_path / "does_not_exist.json")


def test_load_missing_required_key_raises(
    tmp_path: Path, synthetic_config: Dict[str, Any]
) -> None:
    cfg = dict(synthetic_config)
    cfg.pop("pillar_strategies")
    path = _write(tmp_path, cfg)
    with pytest.raises(ValueError, match="pillar_strategies"):
        load_peer_groups_config(path)


def test_load_unknown_strategy_raises(
    tmp_path: Path, synthetic_config: Dict[str, Any]
) -> None:
    cfg = json.loads(json.dumps(synthetic_config))
    cfg["pillar_strategies"]["adoption_momentum"]["strategy"] = "bogus"
    path = _write(tmp_path, cfg)
    with pytest.raises(ValueError, match="bogus"):
        load_peer_groups_config(path)


def test_load_unknown_fallback_step_raises(
    tmp_path: Path, synthetic_config: Dict[str, Any]
) -> None:
    cfg = json.loads(json.dumps(synthetic_config))
    cfg["pillar_strategies"]["adoption_momentum"]["fallback_chain"] = ["nope"]
    path = _write(tmp_path, cfg)
    with pytest.raises(ValueError, match="fallback_chain"):
        load_peer_groups_config(path)


# ---------------------------------------------------------------------------
# 2. get_pillar_strategy
# ---------------------------------------------------------------------------


def test_get_pillar_strategy_returns_configured_block(
    synthetic_config: Dict[str, Any],
) -> None:
    s = get_pillar_strategy("adoption_momentum", synthetic_config)
    assert s["strategy"] == "compound"
    assert s["min_cohort_size"] == 6
    assert s["fallback_chain"][0] == "sector_group_only"


def test_get_pillar_strategy_unknown_pillar_defaults_to_universe(
    synthetic_config: Dict[str, Any],
) -> None:
    s = get_pillar_strategy("not_a_pillar", synthetic_config)
    assert s == DEFAULT_STRATEGY
    assert s["strategy"] == "universe"


# ---------------------------------------------------------------------------
# 3. is_active default behaviour
# ---------------------------------------------------------------------------


def test_is_active_default_false_when_section_missing() -> None:
    assert is_active({}) is False


def test_is_active_default_false_when_enabled_missing() -> None:
    assert is_active({"active_overrides": {}}) is False


def test_is_active_honours_enabled_flag(
    synthetic_config: Dict[str, Any],
) -> None:
    assert is_active(synthetic_config) is False
    synthetic_config["active_overrides"]["enabled"] = True
    assert is_active(synthetic_config) is True


# ---------------------------------------------------------------------------
# 4. resolve_cohort — one test per strategy
# ---------------------------------------------------------------------------


def test_resolve_cohort_compound_returns_stage_x_sector_group(
    synthetic_config: Dict[str, Any], synthetic_universe: Dict[str, Any],
) -> None:
    """MSFT is in tech_software with 7 mature peers — compound clears n=6."""

    cohort, label = resolve_cohort(
        "MSFT", "adoption_momentum", synthetic_universe, synthetic_config
    )
    # mature_compounder & tech_software, excluding MSFT
    expected = {"ORCL", "CRM", "ADBE", "NOW", "CRWD", "PANW"}
    assert set(cohort) == expected
    assert label == "compound"
    assert "MSFT" not in cohort


def test_resolve_cohort_sector_group_only(
    synthetic_config: Dict[str, Any], synthetic_universe: Dict[str, Any],
) -> None:
    """DES pillar is sector_group_only → every tech_software peer except self."""

    cohort, label = resolve_cohort(
        "MSFT", "des", synthetic_universe, synthetic_config
    )
    expected = {"ORCL", "CRM", "ADBE", "NOW", "CRWD", "PANW", "DDOG"}
    assert set(cohort) == expected
    assert label == "sector_group_only"


def test_resolve_cohort_maturity_only(
    synthetic_config: Dict[str, Any], synthetic_universe: Dict[str, Any],
) -> None:
    cohort, label = resolve_cohort(
        "AAPL", "by_maturity", synthetic_universe, synthetic_config
    )
    # All mature_compounders except AAPL: 3 tech_hardware (AVGO, TXN,
    # QCOM) + 7 tech_software (MSFT, ORCL, CRM, ADBE, NOW, CRWD, PANW)
    # + 8 banks + 2 tiny (LIN, SHW) = 20
    assert label == "maturity_only"
    assert "AAPL" not in cohort
    assert "NVDA" not in cohort  # NVDA is growth_compounder
    assert "INTC" not in cohort  # INTC is recovery_stabilization
    assert "MSFT" in cohort
    assert "JPM" in cohort
    assert len(cohort) == 20


def test_resolve_cohort_universe_strategy(
    synthetic_config: Dict[str, Any], synthetic_universe: Dict[str, Any],
) -> None:
    cohort, label = resolve_cohort(
        "AAPL",
        "institutional_confidence",
        synthetic_universe,
        synthetic_config,
    )
    assert label == "universe"
    assert "AAPL" not in cohort
    assert len(cohort) == len(synthetic_universe["tickers"]) - 1


def test_resolve_cohort_universe_fallback_when_unknown_pillar(
    synthetic_config: Dict[str, Any], synthetic_universe: Dict[str, Any],
) -> None:
    """Unknown pillar should land at universe with the 'universe' label."""

    cohort, label = resolve_cohort(
        "AAPL", "totally_new_pillar", synthetic_universe, synthetic_config
    )
    assert label == "universe"
    assert len(cohort) == len(synthetic_universe["tickers"]) - 1


# ---------------------------------------------------------------------------
# 5. fallback_chain — primary underflow walks to next step
# ---------------------------------------------------------------------------


def test_resolve_cohort_compound_underflow_falls_to_sector_group(
    synthetic_config: Dict[str, Any], synthetic_universe: Dict[str, Any],
) -> None:
    """AAPL (mature, tech_hardware) compound cohort = 3 mature peers
    (AVGO, TXN, QCOM) — below the n=6 floor.  We MUST fall to
    sector_group_only which yields all 7 hardware peers (excl AAPL)."""

    cohort, label = resolve_cohort(
        "AAPL", "adoption_momentum", synthetic_universe, synthetic_config
    )
    assert label == "sector_group_only"
    assert set(cohort) == {
        "NVDA", "AMD", "INTC", "MU", "AVGO", "QCOM", "TXN",
    }


def test_resolve_cohort_full_fallback_to_universe(
    synthetic_config: Dict[str, Any],
) -> None:
    """LIN is in 'tiny' (2 tickers) and has no maturity peers in this
    minimal universe — every step underflows except the terminal one,
    yielding strategy_used='universe_fallback'."""

    tiny_universe = {
        "tickers": [
            {"ticker": "LIN", "maturity_stage": "unique_stage"},
            {"ticker": "SHW", "maturity_stage": "other_stage"},
            {"ticker": "AAPL", "maturity_stage": "another"},
            {"ticker": "MSFT", "maturity_stage": "yet_another"},
        ]
    }
    cohort, label = resolve_cohort(
        "LIN", "adoption_momentum", tiny_universe, synthetic_config
    )
    assert label == "universe_fallback"
    assert set(cohort) == {"SHW", "AAPL", "MSFT"}


# ---------------------------------------------------------------------------
# 6. bank_override
# ---------------------------------------------------------------------------


def test_resolve_cohort_bank_override_for_financial_pillar(
    synthetic_config: Dict[str, Any], synthetic_universe: Dict[str, Any],
) -> None:
    """A bank focal must return the bank cohort regardless of compound."""

    cohort, label = resolve_cohort(
        "JPM", "financial_evolution", synthetic_universe, synthetic_config
    )
    assert label == "bank_cohort"
    assert "JPM" not in cohort
    assert set(cohort) == {"BAC", "C", "WFC", "GS", "MS", "USB", "BK"}


def test_resolve_cohort_bank_override_skipped_for_non_bank(
    synthetic_config: Dict[str, Any], synthetic_universe: Dict[str, Any],
) -> None:
    """Non-bank focal under financial_evolution falls through to the
    standard compound resolution path."""

    cohort, label = resolve_cohort(
        "MSFT", "financial_evolution", synthetic_universe, synthetic_config
    )
    assert label == "compound"
    assert set(cohort) == {"ORCL", "CRM", "ADBE", "NOW", "CRWD", "PANW"}


# ---------------------------------------------------------------------------
# 7. Universe accepts both list-of-dicts AND universe.json wrapper
# ---------------------------------------------------------------------------


def test_resolve_cohort_accepts_bare_list_universe(
    synthetic_config: Dict[str, Any], synthetic_universe: Dict[str, Any],
) -> None:
    cohort_dict, label_dict = resolve_cohort(
        "MSFT", "adoption_momentum", synthetic_universe, synthetic_config
    )
    cohort_list, label_list = resolve_cohort(
        "MSFT",
        "adoption_momentum",
        synthetic_universe["tickers"],
        synthetic_config,
    )
    assert sorted(cohort_dict) == sorted(cohort_list)
    assert label_dict == label_list
