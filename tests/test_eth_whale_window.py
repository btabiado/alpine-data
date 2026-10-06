"""Whale tab: "Recent ETH whale transactions" may only show the last 24h.

The card is labelled "≥ $1M · last 24h" but showed 2015-2022 transfers:
  * Blockchair rejects the relative filter ``time(24h)..`` with HTTP 400
    "Wrong filtering expression" (confirmed live), so the primary query
    always failed;
  * the fallback ``s=value(desc)`` had no time filter at all, i.e. the
    all-time largest ETH transfers;
  * the stale-cache wrapper then kept replaying that result.

Pinned here, offline: the query uses an absolute UTC datetime range, there
is no unfiltered fallback request, every row (live or cached) is re-checked
against the window, the poisoned legacy cache is never replayed, and both
dashboards filter client-side and show an honest empty state.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

import fetch_market as fm

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 4, 22, 0, 0, tzinfo=timezone.utc)
HASH = "0x" + "ab" * 32


def _row(time_str, usd, h=HASH):
    return {"hash": h, "value": str(int(usd / 2000 * 1e18)), "value_usd": usd,
            "time": time_str, "fee": "21000000000000"}


@pytest.fixture
def stale_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(fm, "_STALE_DIR", tmp_path)
    return tmp_path


class _FakeGet:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, params=None, timeout=25):
        self.calls.append((url, dict(params or {})))
        return self.responses.pop(0) if self.responses else None


def test_query_uses_an_absolute_utc_window_not_the_rejected_relative_form(monkeypatch):
    fake = _FakeGet([{"data": [_row("2026-10-04 21:23:35", 134_369_470.2)]}])
    monkeypatch.setattr(fm, "_get", fake)
    rows = fm._blockchair_eth_large_transactions_impl(1_000_000, 10, now=NOW)
    assert len(fake.calls) == 1
    q = fake.calls[0][1]["q"]
    assert q == "time(2026-10-03 22:00:00..),value_usd(1000000..)"
    assert "24h" not in q
    assert fake.calls[0][1]["s"] == "value_usd(desc)"
    assert rows and rows[0]["time"] == "2026-10-04 21:23:35"


def test_no_unfiltered_fallback_request_on_failure(monkeypatch):
    """The old code retried with `s=value(desc)` and no time filter."""
    fake = _FakeGet([None, {"data": [_row("2022-08-17 13:26:10", 2.8e9)]}])
    monkeypatch.setattr(fm, "_get", fake)
    assert fm._blockchair_eth_large_transactions_impl(1_000_000, 10, now=NOW) is None
    assert len(fake.calls) == 1, "a second (unfiltered) request was made"


def test_rows_outside_the_window_or_below_threshold_are_dropped(monkeypatch):
    fake = _FakeGet([{"data": [
        _row("2026-10-04 21:00:00", 5_000_000),     # keep
        _row("2026-10-03 21:59:59", 9_000_000),     # 24h + 1s ago: drop
        _row("2018-12-01 02:13:35", 174_877_680),   # all-time record: drop
        _row("2026-10-04 12:00:00", 999_999),       # under $1M: drop
        _row(None, 50_000_000),                     # undated: drop
    ]}])
    monkeypatch.setattr(fm, "_get", fake)
    rows = fm._blockchair_eth_large_transactions_impl(1_000_000, 10, now=NOW)
    assert [r["time"] for r in rows] == ["2026-10-04 21:00:00"]


def test_live_success_with_nothing_qualifying_is_an_honest_empty(monkeypatch, stale_dir):
    monkeypatch.setattr(fm, "_get", _FakeGet([{"data": []}]))
    out = fm.blockchair_eth_large_transactions_with_status(1_000_000, 10, now=NOW)
    assert out["rows"] == []
    assert out["status"]["source"] == "live"
    assert out["status"]["window_hours"] == 24


def test_poisoned_legacy_cache_is_deleted_and_never_replayed(monkeypatch, stale_dir):
    legacy = stale_dir / "blockchair_eth_large_transactions.json"
    legacy.write_text(json.dumps({"saved_at": 1, "value": [
        {"hash": HASH, "value_eth": 1.49e6, "value_usd": 2.831e9,
         "time": "2022-08-17 13:26:10", "fee_eth": 0.0002}]}))
    monkeypatch.setattr(fm, "_get", _FakeGet([None]))
    out = fm.blockchair_eth_large_transactions_with_status(1_000_000, 10, now=NOW)
    assert out == {"rows": [], "status": {**out["status"], "source": "unavailable"}}
    assert not legacy.exists()


def test_stale_cache_replay_is_trimmed_to_the_window(monkeypatch, stale_dir):
    (stale_dir / "blockchair_eth_large_transactions_24h.json").write_text(json.dumps({
        "saved_at": 1, "value": [
            {"hash": HASH, "value_eth": 10, "value_usd": 3e6, "time": "2026-10-04 08:00:00", "fee_eth": 0},
            {"hash": HASH, "value_eth": 10, "value_usd": 9e6, "time": "2026-10-02 08:00:00", "fee_eth": 0},
        ]}))
    monkeypatch.setattr(fm, "_get", _FakeGet([None]))
    out = fm.blockchair_eth_large_transactions_with_status(1_000_000, 10, now=NOW)
    assert out["status"]["source"] == "stale-cache"
    assert [r["time"] for r in out["rows"]] == ["2026-10-04 08:00:00"]


def test_successful_fetch_is_cached_under_the_new_key(monkeypatch, stale_dir):
    monkeypatch.setattr(fm, "_get", _FakeGet([{"data": [_row("2026-10-04 20:00:00", 2e6)]}]))
    fm.blockchair_eth_large_transactions_with_status(1_000_000, 10, now=NOW)
    assert (stale_dir / "blockchair_eth_large_transactions_24h.json").exists()
    assert not (stale_dir / "blockchair_eth_large_transactions.json").exists()


def test_fetch_whale_ships_rows_and_status(monkeypatch):
    for name in ("whale_proxies_btc", "bitinfocharts_btc_distribution",
                 "glassnode_btc_whale_metrics", "blockchair_eth_stats",
                 "coin_metrics_eth_whale_metrics", "fetch_multichain_whale_stats",
                 "etherscan_eth_daily"):
        monkeypatch.setattr(fm, name, lambda *a, **k: {})
    monkeypatch.setattr(fm, "mempool_whale_transactions", lambda *a, **k: [])
    monkeypatch.setattr(fm, "blockchair_eth_large_transactions_with_status",
                        lambda *a, **k: {"rows": [{"hash": HASH, "time": "2026-10-04 20:00:00"}],
                                         "status": {"source": "live", "window_hours": 24}})
    eth = fm.fetch_whale()["eth"]
    assert eth["large_transactions"][0]["time"] == "2026-10-04 20:00:00"
    assert eth["large_transactions_status"]["source"] == "live"


@pytest.mark.parametrize("rel", ["app.py"])
def test_dashboards_filter_client_side_and_show_an_empty_state(rel):
    src = (ROOT / rel).read_text(encoding="utf-8")
    i = src.index("function renderEthWhaleAlerts(){")
    body = src[i:src.index("\nfunction ", i + 10)]
    assert "recentEthWhaleTxs(raw)" in body, "rows are not re-checked against the 24h window"
    assert "const ETH_WHALE_WINDOW_MS = 24 * 3600 * 1000;" in src
    # Blockchair times carry no zone marker and are UTC.
    assert re.search(r"hasZone \? '' : 'Z'", src)
    # Empty state instead of a silently hidden card / mislabelled rows.
    assert "No ETH transactions of $1M or more in the last 24 hours." in body
    assert "Blockchair was unreachable" in body
    assert "card.classList.add('hidden'); return; }" in body  # only when no whale payload
    assert "if (!txs.length){ card.classList.add('hidden')" not in body
