"""Contract tests for the new whale data structures:
- whale.eth.large_transactions (Blockchair top 10 ETH whale txs)
- whale.multichain (LTC, BCH, DOGE 24h network stats + largest single tx)

These tests use pytest.skip rather than asserting presence, because CI may not
yet have populated whale.json with the new keys (parallel agents are adding the
fetcher + UI).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest


WHALE_JSON = Path(__file__).resolve().parent.parent / "data" / "whale.json"

# Renderer-side validation for ETH tx hashes: 0x + 64 hex chars.
ETH_HASH_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")


def _load_whale() -> dict:
    if not WHALE_JSON.exists():
        pytest.skip(f"{WHALE_JSON} not present")
    try:
        with open(WHALE_JSON) as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        pytest.skip(f"could not parse {WHALE_JSON}: {exc}")


def _get_eth_large_transactions(whale: dict):
    eth = whale.get("eth")
    if not isinstance(eth, dict):
        return None
    return eth.get("large_transactions")


def _get_multichain(whale: dict):
    return whale.get("multichain")


def test_whale_eth_large_transactions_shape():
    """If whale.eth.large_transactions exists, it must be a list of dicts with
    a `hash` (string) and at least one of `value_eth` / `value_usd` (number)."""
    whale = _load_whale()
    large = _get_eth_large_transactions(whale)
    if large is None:
        pytest.skip("whale.eth.large_transactions not present yet")

    assert isinstance(large, list), (
        f"whale.eth.large_transactions must be a list, got {type(large).__name__}"
    )

    for i, entry in enumerate(large):
        assert isinstance(entry, dict), (
            f"large_transactions[{i}] must be a dict, got {type(entry).__name__}"
        )
        assert "hash" in entry, f"large_transactions[{i}] missing 'hash'"
        assert isinstance(entry["hash"], str), (
            f"large_transactions[{i}].hash must be a string, "
            f"got {type(entry['hash']).__name__}"
        )

        has_eth = "value_eth" in entry and isinstance(entry["value_eth"], (int, float))
        has_usd = "value_usd" in entry and isinstance(entry["value_usd"], (int, float))
        assert has_eth or has_usd, (
            f"large_transactions[{i}] must have a numeric 'value_eth' or "
            f"'value_usd'; keys present: {sorted(entry.keys())}"
        )


def test_whale_eth_large_transactions_hashes_are_hex():
    """Each entry's `hash` must match the renderer's validator: ^0x[0-9a-fA-F]{64}$.
    Skip if the field isn't populated yet, or if the list is empty."""
    whale = _load_whale()
    large = _get_eth_large_transactions(whale)
    if large is None:
        pytest.skip("whale.eth.large_transactions not present yet")
    if not large:
        pytest.skip("whale.eth.large_transactions is empty")

    for i, entry in enumerate(large):
        if not isinstance(entry, dict):
            continue
        h = entry.get("hash")
        assert isinstance(h, str) and ETH_HASH_RE.match(h), (
            f"large_transactions[{i}].hash {h!r} does not match "
            f"^0x[0-9a-fA-F]{{64}}$"
        )


def test_whale_multichain_chains_have_required_fields():
    """For each chain present under whale.multichain, verify it has a string
    `symbol`. If `largest_tx_24h` is non-None it must be a dict with a string
    `hash`. Skip per-chain if absent or the parent dict isn't there yet."""
    whale = _load_whale()
    multichain = _get_multichain(whale)
    if multichain is None:
        pytest.skip("whale.multichain not present yet")

    assert isinstance(multichain, dict), (
        f"whale.multichain must be a dict, got {type(multichain).__name__}"
    )

    if not multichain:
        pytest.skip("whale.multichain is empty")

    for chain_name, chain in multichain.items():
        if chain is None:
            continue
        assert isinstance(chain, dict), (
            f"whale.multichain[{chain_name!r}] must be a dict, "
            f"got {type(chain).__name__}"
        )

        assert "symbol" in chain, (
            f"whale.multichain[{chain_name!r}] missing 'symbol'"
        )
        assert isinstance(chain["symbol"], str), (
            f"whale.multichain[{chain_name!r}].symbol must be a string, "
            f"got {type(chain['symbol']).__name__}"
        )

        largest = chain.get("largest_tx_24h")
        if largest is None:
            continue
        assert isinstance(largest, dict), (
            f"whale.multichain[{chain_name!r}].largest_tx_24h must be a dict "
            f"or None, got {type(largest).__name__}"
        )
        assert "hash" in largest, (
            f"whale.multichain[{chain_name!r}].largest_tx_24h missing 'hash'"
        )
        assert isinstance(largest["hash"], str), (
            f"whale.multichain[{chain_name!r}].largest_tx_24h.hash must be a "
            f"string, got {type(largest['hash']).__name__}"
        )


def test_whale_multichain_supports_expected_chains():
    """Soft check: at least one of litecoin / bitcoin-cash / dogecoin is
    present under whale.multichain. Skip entirely if whale.multichain doesn't
    exist yet."""
    whale = _load_whale()
    multichain = _get_multichain(whale)
    if multichain is None:
        pytest.skip("whale.multichain not present yet")

    if not isinstance(multichain, dict):
        pytest.skip(
            f"whale.multichain is not a dict ({type(multichain).__name__}); "
            "covered by other tests"
        )

    expected = {"litecoin", "bitcoin-cash", "dogecoin"}
    present = expected.intersection(multichain.keys())
    assert present, (
        "expected at least one of litecoin / bitcoin-cash / dogecoin under "
        f"whale.multichain; got keys: {sorted(multichain.keys())}"
    )
