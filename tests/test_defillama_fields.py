"""DeFiLlama free-API field drift (verified 2026-10-04).

``/v2/chains`` no longer returns change_1d/7d/1m (null on 20/20 chains) and
``/protocols`` no longer returns change_1m (null on 25/25) and reports mcap
only for protocols owning a CoinGecko id (null on 19/25). Offline: ``_get``
is stubbed with bodies in the live shapes.
"""
from __future__ import annotations

import fetch_market as fm

DAY = 86400
LAST = 20_365 * DAY        # 2025-10-04 00:00Z, any day boundary works


def _hist(values_by_offset):
    return [{"date": LAST - off * DAY, "tvl": v} for off, v in values_by_offset.items()]


def test_chain_changes_derived_from_history_when_snapshot_lacks_them(monkeypatch):
    bodies = {
        "https://api.llama.fi/v2/chains": [
            {"name": "Ethereum", "tvl": 53.9e9, "tokenSymbol": "ETH", "cmcId": "1027"},
            {"name": "NewChain", "tvl": 1e9, "tokenSymbol": "NEW"},
        ],
        "https://api.llama.fi/v2/historicalChainTvl/Ethereum":
            _hist({0: 110.0, 1: 100.0, 7: 88.0, 30: 55.0}),
        # Only 3 days of history: 7d/30d must stay None, not 0.
        "https://api.llama.fi/v2/historicalChainTvl/NewChain":
            _hist({0: 50.0, 1: 40.0, 2: 30.0}),
    }
    monkeypatch.setattr(fm, "_get", lambda url, params=None, **kw: bodies.get(url))
    out = {c["name"]: c for c in fm.defillama_chains(20)}
    eth = out["Ethereum"]
    assert round(eth["change_1d_pct"], 6) == 10.0
    assert round(eth["change_7d_pct"], 6) == 25.0
    assert round(eth["change_1m_pct"], 6) == 100.0
    assert eth["change_source"] == "historicalChainTvl"
    new = out["NewChain"]
    assert round(new["change_1d_pct"], 6) == 25.0
    assert new["change_7d_pct"] is None and new["change_1m_pct"] is None


def test_chain_history_failure_leaves_null(monkeypatch):
    bodies = {"https://api.llama.fi/v2/chains": [{"name": "Ethereum", "tvl": 1.0}]}
    monkeypatch.setattr(fm, "_get", lambda url, params=None, **kw: bodies.get(url))
    eth = fm.defillama_chains(20)[0]
    assert eth["change_7d_pct"] is None and "change_source" not in eth


def test_protocol_change_1m_and_parent_mcap_filled_from_protocols2(monkeypatch):
    bodies = {
        "https://api.llama.fi/protocols": [
            {"name": "Binance CEX", "symbol": "BNB", "category": "CEX", "tvl": 180e9,
             "change_1d": 0.5, "change_7d": 0.01, "mcap": None},
            {"name": "Aave V3", "symbol": "AAVE", "category": "Lending", "tvl": 18.3e9,
             "change_1d": 0.7, "change_7d": 0.3, "mcap": None,
             "parentProtocol": "parent#aave"},
            {"name": "WBTC", "symbol": "-", "category": "Bridge", "tvl": 9.9e9,
             "change_1d": 0.6, "change_7d": 0.9, "mcap": None},
            {"name": "Lido", "symbol": "LDO", "category": "Liquid Staking", "tvl": 26.7e9,
             "change_1d": 0.75, "change_7d": 0.75, "mcap": 3.8e8},
        ],
        "https://api.llama.fi/lite/protocols2": {
            "protocols": [
                {"name": "Aave V3", "tvl": 110.0, "tvlPrevMonth": 100.0},
                {"name": "WBTC", "tvl": 105.0, "tvlPrevMonth": 100.0},
                {"name": "Lido", "tvl": 120.0, "tvlPrevMonth": 100.0},
            ],
            "parentProtocols": [{"id": "parent#aave", "symbol": "AAVE", "mcap": 2.77e9}],
        },
    }
    monkeypatch.setattr(fm, "_get", lambda url, params=None, **kw: bodies.get(url))
    out = {p["name"]: p for p in fm.defillama_protocols(25)}

    aave = out["Aave V3"]
    assert round(aave["change_1m_pct"], 6) == 10.0
    assert aave["mcap_usd"] == 2.77e9
    assert aave["mcap_source"] == "parent token (AAVE)"
    assert "unavailable" not in aave

    assert out["Lido"]["mcap_usd"] == 3.8e8 and "mcap_source" not in out["Lido"]
    assert round(out["Lido"]["change_1m_pct"], 6) == 20.0

    cex = out["Binance CEX"]
    assert cex["change_1m_pct"] is None
    assert "CEX" in cex["unavailable"]["change_1m_pct"]
    assert out["WBTC"]["unavailable"] == {"mcap_usd": "no token"}
    assert all("_parent" not in p for p in out.values())
