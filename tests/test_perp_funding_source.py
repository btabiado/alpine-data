"""Coinbase International perps went PAUSED/DELISTED (verified 2026-10-04:
264 PERPs = 131 PAUSED + 133 DELISTED, quotes frozen at 2026-09-03 and
2026-10-01) but their frozen funding kept feeding the Overview "Crypto Market
Sentiment" composite as live. Offline: HTTP is stubbed.
"""
from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

import fetch_market as fm

REPO_ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 10, 4, 21, 0, tzinfo=timezone.utc)


def _inst(sym, state, ts, funding=0.00005):
    return {"symbol": f"{sym}-PERP", "type": "PERP", "trading_state": state,
            "open_interest": "10", "qty_24hr": "1", "notional_24hr": "100",
            "quote": {"timestamp": ts, "predicted_funding": str(funding),
                      "mark_price": "100", "index_price": "100"}}


def test_paused_delisted_and_stale_quotes_are_dropped(monkeypatch):
    body = [
        _inst("BTC", "PAUSED", "2026-10-01T09:00:29.114Z"),
        _inst("OLD", "DELISTED", "2026-09-03T00:00:00Z"),
        _inst("ETH", "TRADING", "2026-10-02T09:00:00Z"),      # 60h-old quote
        _inst("SOL", "TRADING", "2026-10-04T20:30:00Z", funding=0.0001),
        {"symbol": "BTC-USDC", "type": "SPOT"},
    ]
    monkeypatch.setattr(fm, "_get", lambda url, params=None, **kw: body)
    rows = fm.coinbase_intl_perpetuals(now=NOW)
    assert [r["symbol"] for r in rows] == ["SOL"]
    st = fm._CB_INTL_LAST_STATUS
    assert st["available"] is True
    assert st["excluded_by_state"] == {"PAUSED": 1, "DELISTED": 1}
    assert st["excluded_stale_quote"] == 1


def test_all_paused_yields_empty_with_reason(monkeypatch):
    body = [_inst("BTC", "PAUSED", "2026-10-01T09:00:29.114Z"),
            _inst("ETH", "PAUSED", "2026-10-01T09:00:29.114Z")]
    monkeypatch.setattr(fm, "_get", lambda url, params=None, **kw: body)
    assert fm.coinbase_intl_perpetuals(now=NOW) == []
    st = fm._CB_INTL_LAST_STATUS
    assert st["available"] is False
    assert "2 PAUSED" in st["reason"] and "2026-10-01T09:00:29.114Z" in st["reason"]


def test_perp_funding_summary_uses_fresh_okx_rows_only():
    out = fm.perp_funding_summary({
        "BTC": [{"date": "2026-10-03", "rate": 0.0001}, {"date": "2026-10-04", "rate": 0.0002}],
        "ETH": [{"date": "2026-10-03", "rate": 0.0004}],
        "LTC": [{"date": "2026-09-03", "rate": 0.009}],     # frozen: excluded
        "LINK": [],
    }, now=NOW)
    assert out["available"] is True
    assert {r["symbol"] for r in out["rows"]} == {"BTC", "ETH"}
    assert abs(out["avg_rate"] - 0.0003) < 1e-12
    assert out["as_of"] == "2026-10-03"                    # oldest contributing row
    assert {e["symbol"] for e in out["excluded"]} == {"LTC", "LINK"}


def test_perp_funding_summary_unavailable_when_all_stale():
    out = fm.perp_funding_summary({"BTC": [{"date": "2026-09-03", "rate": 0.0001}]}, now=NOW)
    assert out["available"] is False and out["avg_rate"] is None and out["as_of"] is None


def _sc():
    spec = importlib.util.spec_from_file_location(
        "snapshot_composites_pf", REPO_ROOT / "scripts" / "snapshot_composites.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_archive_uses_perp_funding_and_ignores_frozen_coinbase_rows(tmp_path, monkeypatch):
    sc = _sc()
    monkeypatch.setattr(sc, "CACHE", tmp_path)
    (tmp_path / "market.json").write_text(json.dumps({
        "fetched_at": "2026-10-04T21:00:00Z",
        "fear_greed": [{"date": "2026-10-04", "value": 50}],
        "coinbase_intl_perps": [{"symbol": "BTC", "funding_rate": 0.01,
                                 "as_of": "2026-09-03"}],
        "perp_funding": {"available": True, "avg_rate": 0.0001, "as_of": "2026-10-04",
                         "rows": [{"symbol": "BTC", "rate": 0.0001, "as_of": "2026-10-04"}]},
    }))
    (tmp_path / "whale.json").write_text("{}")
    got = sc.collect()["overview_sentiment"]
    # F&G 50 -> 0, funding 0.0001 -> +20; mean 10. The frozen 0.01 would clamp to +100.
    assert got["score"] == 10
    assert got["as_of"] == "2026-10-04"
    assert got["stale"] is False


def test_archive_flags_stale_when_an_input_is_frozen(tmp_path, monkeypatch):
    sc = _sc()
    monkeypatch.setattr(sc, "CACHE", tmp_path)
    (tmp_path / "market.json").write_text(json.dumps({
        "fetched_at": "2026-10-04T21:00:00Z",
        "fear_greed": [{"date": "2026-10-04", "value": 50}],
        "coinbase_intl_perps": [{"symbol": "BTC", "funding_rate": 0.0001,
                                 "as_of": "2026-09-03"}],
    }))
    (tmp_path / "whale.json").write_text("{}")
    got = sc.collect()["overview_sentiment"]
    assert got["as_of"] == "2026-09-03"
    assert got["stale"] is True
    assert "31d behind the fetch" in got["note"]
