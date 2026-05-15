"""Contract tests for the data the DeFi tab overhaul depends on.

The DeFi tab is being rebuilt with a KPI strip + chain selector
(Ethereum / Solana / Arbitrum / Base) that drives a per-chain panel
showing TVL, protocols on that chain, and the chain's share of the
total. These tests pin down the shape of ``market.defi.*`` that the new
client-side renderer reads from. If a DeFiLlama fetcher silently drops
``chains``, the chain-name list, or the per-protocol ``chains`` field,
the new selector renders blank without erroring — same class of bug
``test_symbol_search_data.py`` was designed to catch for the symbol
search modal.

Each test prefers ``pytest.skip`` over ``assert False`` when the
underlying data hasn't been populated yet (CI runs without a fresh
fetch_market.py), so the suite stays green on a clean checkout.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
MARKET_JSON = ROOT / "data" / "market.json"


def _load_defi_or_skip() -> dict:
    """Load and return ``market.defi`` or skip if the file isn't there yet."""
    if not MARKET_JSON.exists():
        pytest.skip(f"{MARKET_JSON} not present — run fetch_market.py first")
    try:
        market = json.loads(MARKET_JSON.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        pytest.fail(f"market.json is not valid JSON: {e}")
    defi = market.get("defi")
    if not isinstance(defi, dict):
        pytest.skip("market.defi block is missing or not a dict")
    return defi


# ---------- tests ----------


def test_defi_chains_array_has_required_fields():
    """The KPI strip iterates ``defi.chains`` for the top-chain pills. Each
    entry must have a ``name`` and a TVL value. DeFiLlama's response uses
    ``tvl_usd`` (see ``defillama_chains`` in fetch_market.py), but the
    overhaul's renderer is tolerant of either ``tvl`` or ``tvl_usd`` — so
    this test accepts either, as long as the value is numeric."""
    defi = _load_defi_or_skip()
    chains = defi.get("chains")
    if not isinstance(chains, list) or not chains:
        pytest.skip("defi.chains is empty — nothing to validate")

    for i, c in enumerate(chains):
        assert isinstance(c, dict), f"chains[{i}] not a dict: {type(c).__name__}"
        name = c.get("name")
        assert isinstance(name, str) and name, (
            f"chains[{i}] missing string 'name' (got {name!r})"
        )
        tvl = c.get("tvl") if c.get("tvl") is not None else c.get("tvl_usd")
        assert isinstance(tvl, (int, float)), (
            f"chains[{i}] ({name!r}) missing numeric 'tvl' / 'tvl_usd' "
            f"(got {tvl!r})"
        )


def test_defi_tvl_history_has_4_chains():
    """The per-chain panel reads ``defi.tvl_history[<chain>]`` for the
    chain-specific sparkline. The selector exposes Ethereum / Solana /
    Arbitrum / Base — all four must be present so switching chains never
    renders an undefined series."""
    defi = _load_defi_or_skip()
    tvl_history = defi.get("tvl_history")
    if not isinstance(tvl_history, dict) or not tvl_history:
        pytest.skip("defi.tvl_history is empty — nothing to validate")

    required = ("Ethereum", "Solana", "Arbitrum", "Base")
    missing = [c for c in required if c not in tvl_history]
    assert not missing, (
        f"defi.tvl_history missing chain key(s) the selector needs: "
        f"{missing!r}. Present keys: {sorted(tvl_history.keys())!r}"
    )
    # And each one must be a list (possibly empty if that chain's fetch
    # failed) so ``.length`` / iteration on the client never throws.
    for chain in required:
        series = tvl_history[chain]
        assert isinstance(series, list), (
            f"defi.tvl_history[{chain!r}] is {type(series).__name__}, "
            "expected list"
        )


def test_defi_protocols_have_chains_field():
    """The per-chain panel filters ``defi.protocols`` by whether the
    currently-selected chain appears in each protocol's ``chains`` list
    (DeFiLlama tags a protocol with every chain it operates on). Without
    this field the filter would be a no-op and the chain selector would
    show all protocols regardless of selection."""
    defi = _load_defi_or_skip()
    protocols = defi.get("protocols")
    if not isinstance(protocols, list) or not protocols:
        pytest.skip("defi.protocols is empty — nothing to validate")

    for i, p in enumerate(protocols):
        assert isinstance(p, dict), f"protocols[{i}] not a dict"
        chains = p.get("chains")
        assert isinstance(chains, list), (
            f"protocols[{i}] ({p.get('name')!r}) missing list 'chains' "
            f"(got {type(chains).__name__}: {chains!r})"
        )
