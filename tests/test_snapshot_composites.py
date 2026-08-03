"""Composite-index snapshot honesty tests (``scripts/snapshot_composites.py``).

The composites written here are the daily history behind the dashboard's
gauges, so a wrong date in this directory is permanent — it cannot be
recomputed later. Two rules dominate:

  * a composite is only as fresh as its OLDEST contributing input (min);
  * the number of cache-served inputs is disclosed alongside the date,
    because a date alone under-reports a partially-frozen breadth index.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def sc():
    """Load the script by path — scripts/ is not a package."""
    spec = importlib.util.spec_from_file_location(
        "snapshot_composites", REPO_ROOT / "scripts" / "snapshot_composites.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------- helpers ----------

def test_oldest_takes_the_min(sc):
    assert sc._oldest(["2026-08-01", "2026-06-09", "2026-07-15"]) == "2026-06-09"


def test_oldest_ignores_junk_and_empties(sc):
    assert sc._oldest([]) is None
    assert sc._oldest(None) is None
    assert sc._oldest([None, "", 42, "short"]) is None
    assert sc._oldest(["2026-06-09T10:00:00Z"]) == "2026-06-09"


def test_poc_entry_date_prefers_pinned_as_of(sc):
    assert sc._poc_entry_date({"as_of": "2026-06-09",
                               "signal_history": [{"date": "2026-08-01"}]}) == "2026-06-09"


def test_poc_entry_date_falls_back_to_signal_history(sc):
    assert sc._poc_entry_date({"signal_history": [{"date": "2026-07-04"}]}) == "2026-07-04"
    assert sc._poc_entry_date({}) is None
    assert sc._poc_entry_date(None) is None


# ---------- collect() ----------

def _write_caches(tmp_path, market, whale=None):
    (tmp_path / "market.json").write_text(json.dumps(market))
    (tmp_path / "whale.json").write_text(json.dumps(whale or {}))


def test_crypto_signal_sentiment_uses_oldest_row_not_build_stamp(sc, tmp_path, monkeypatch):
    """Regression: as_of used to be market.generated_at — the payload's
    build stamp, current on every run regardless of the coins beneath it."""
    monkeypatch.setattr(sc, "CACHE", tmp_path)
    _write_caches(tmp_path, {
        "generated_at": "2026-08-02T20:40:11Z",
        "signals_top20": [
            {"symbol": "BTC", "score": 60, "as_of": "2026-08-01"},
            {"symbol": "ETH", "score": -60, "as_of": "2026-06-09", "stale": True},
            {"symbol": "SOL", "score": 30, "as_of": "2026-08-01"},
        ],
    })
    got = sc.collect()["crypto_signal_sentiment"]
    assert got["as_of"] == "2026-06-09"
    assert got["as_of"] != "2026-08-02"
    assert got["stale"] is True
    assert "1 of 3 cached" in got["note"]


def test_crypto_signal_sentiment_clean_run_has_no_cache_note(sc, tmp_path, monkeypatch):
    monkeypatch.setattr(sc, "CACHE", tmp_path)
    _write_caches(tmp_path, {
        "generated_at": "2026-08-02T20:40:11Z",
        "signals_top20": [
            {"symbol": "BTC", "score": 60, "as_of": "2026-08-02"},
            {"symbol": "ETH", "score": 30, "as_of": "2026-08-02"},
        ],
    })
    got = sc.collect()["crypto_signal_sentiment"]
    assert got["as_of"] == "2026-08-02"
    assert got["stale"] is False
    assert "cached" not in got["note"]


def test_crypto_signal_sentiment_undated_rows_give_no_date(sc, tmp_path, monkeypatch):
    """Rule 5: no honest date -> None recorded, never the build stamp."""
    monkeypatch.setattr(sc, "CACHE", tmp_path)
    _write_caches(tmp_path, {
        "generated_at": "2026-08-02T20:40:11Z",
        "signals_top20": [{"symbol": "BTC", "score": 60}],
    })
    assert sc.collect()["crypto_signal_sentiment"]["as_of"] is None


def test_poc_breadth_takes_oldest_contributing_coin(sc, tmp_path, monkeypatch):
    """Regression: this took max(dates), so one live coin masked a list of
    carried-forward ones."""
    monkeypatch.setattr(sc, "CACHE", tmp_path)
    _write_caches(tmp_path, {"poc_top": [
        {"coin_id": "bitcoin", "as_of": "2026-08-01",
         "signal_history": [{"date": "2026-08-01", "score": 20}]},
        {"coin_id": "ethereum", "as_of": "2026-06-09", "stale": True,
         "signal_history": [{"date": "2026-06-09", "score": -40}]},
    ]})
    got = sc.collect()["poc_signal_breadth"]
    assert got["as_of"] == "2026-06-09"
    assert got["stale"] is True
    assert "1 of 2 cached" in got["note"]
    assert got["score"] == -10  # mean of 20 and -40


def test_poc_breadth_backfills_date_from_signal_history(sc, tmp_path, monkeypatch):
    monkeypatch.setattr(sc, "CACHE", tmp_path)
    _write_caches(tmp_path, {"poc_top": [
        {"coin_id": "bitcoin", "signal_history": [{"date": "2026-07-04", "score": 10}]},
    ]})
    assert sc.collect()["poc_signal_breadth"]["as_of"] == "2026-07-04"


def test_crypto_signal_sentiment_recomputed_from_markets_top(sc, tmp_path, monkeypatch):
    """`signals_top20` only exists in the builders' in-memory payload, never
    in data/market.json — which is why every committed snapshot recorded
    null for this gauge. Recompute it from markets_top, and the recomputed
    entries must carry the rows' own observation dates."""
    monkeypatch.setattr(sc, "CACHE", tmp_path)
    spark = [100.0 + i * 0.1 for i in range(168)]
    _write_caches(tmp_path, {
        "generated_at": "2026-08-02T20:40:11Z",
        "markets_top": [
            {"symbol": "BTC", "name": "Bitcoin", "rank": 1, "price_usd": 1.0,
             "market_cap_usd": 1e12, "volume_24h_usd": 1e10,
             "change_24h_pct": 1.0, "change_7d_pct": 9.0, "change_30d_pct": 20.0,
             "sparkline_7d": spark, "as_of": "2026-08-01"},
            {"symbol": "ETH", "name": "Ether", "rank": 2, "price_usd": 1.0,
             "market_cap_usd": 5e11, "volume_24h_usd": 1e10,
             "change_24h_pct": -1.0, "change_7d_pct": -9.0, "change_30d_pct": -20.0,
             "sparkline_7d": spark[::-1], "as_of": "2026-06-09", "stale": True},
        ],
    })
    got = sc.collect()["crypto_signal_sentiment"]
    assert got is not None, "gauge must no longer be permanently null"
    assert got["as_of"] == "2026-06-09"
    assert got["stale"] is True
    assert "1 of 2 cached" in got["note"]


def test_crypto_signal_sentiment_recompute_survives_empty_markets_top(sc, tmp_path, monkeypatch):
    monkeypatch.setattr(sc, "CACHE", tmp_path)
    _write_caches(tmp_path, {"markets_top": []})
    assert sc.collect()["crypto_signal_sentiment"] is None


def test_collect_missing_inputs_are_null_not_absent(sc, tmp_path, monkeypatch):
    monkeypatch.setattr(sc, "CACHE", tmp_path)
    _write_caches(tmp_path, {})
    idx = sc.collect()
    assert idx["crypto_signal_sentiment"] is None
    assert idx["poc_signal_breadth"] is None


# ---------- whale composites: the archive must not be poisoned ----------
# whale_sentiment_btc / _eth persisted ``sentiment.as_of``, which at the time
# was ``whale["fetched_at"][:10]`` — the fetch clock. Writing that into the
# composite HISTORY is worse than showing it on a page: the history is the
# only record, so a run that served entirely stale on-chain numbers would be
# archived forever as a fresh observation.

BASIS = "oldest contributing on-chain series"


def test_whale_sentiment_date_refuses_a_pre_fix_payload(sc):
    """No `as_of_basis` ⇒ the as_of is the old clock read ⇒ refuse it."""
    assert sc._whale_sentiment_date({"as_of": "2026-08-02"}) is None
    assert sc._whale_sentiment_date({"as_of": "2026-08-02",
                                     "as_of_basis": BASIS}) == "2026-08-02"
    assert sc._whale_sentiment_date(None) is None
    assert sc._whale_sentiment_date({"as_of_basis": BASIS}) is None


def test_whale_btc_snapshot_uses_the_composite_observation_date(sc, tmp_path, monkeypatch):
    monkeypatch.setattr(sc, "CACHE", tmp_path)
    _write_caches(tmp_path, {}, {
        "fetched_at": "2026-08-02T23:00:00+00:00",
        "sentiment": {"score": -12, "label": "NEUTRAL",
                      "as_of": "2026-06-09", "as_of_basis": BASIS},
        "btc": {"tx_volume_usd": [{"date": "2026-08-01", "value": 1}]},
    })
    got = sc.collect()["whale_sentiment_btc"]
    assert got["as_of"] == "2026-06-09"


def test_whale_btc_snapshot_falls_back_to_the_oldest_series(sc, tmp_path, monkeypatch):
    """Pre-fix payload: refuse its as_of and date it from the raw series —
    the OLDEST of them, not one hand-picked series' last date."""
    monkeypatch.setattr(sc, "CACHE", tmp_path)
    _write_caches(tmp_path, {}, {
        "fetched_at": "2026-08-02T23:00:00+00:00",
        "sentiment": {"score": -12, "label": "NEUTRAL", "as_of": "2026-08-02"},
        "btc": {
            "tx_volume_usd":      [{"date": "2026-08-01", "value": 1}],
            "hash_rate":          [{"date": "2026-08-01", "value": 1}],
            "active_addresses":   [{"date": "2026-06-09", "value": 1}],
        },
    })
    got = sc.collect()["whale_sentiment_btc"]
    assert got["as_of"] == "2026-06-09"
    assert got["as_of"] != "2026-08-02", "fetch clock leaked into the archive"


def test_whale_eth_snapshot_dates_from_its_own_series(sc, tmp_path, monkeypatch):
    monkeypatch.setattr(sc, "CACHE", tmp_path)
    _write_caches(tmp_path, {}, {
        "fetched_at": "2026-08-02T23:00:00+00:00",
        "eth": {
            "sentiment": {"score": 5, "label": "NEUTRAL", "as_of": "2026-08-02"},
            "coin_metrics": {"AdrActCnt": [{"date": "2026-07-30", "value": 1}],
                             "TxCnt":     [{"date": "2026-06-09", "value": 1}]},
            "etherscan_daily": {"series": [{"date": "2026-08-01", "value": 7200}]},
        },
    })
    got = sc.collect()["whale_sentiment_eth"]
    assert got["as_of"] == "2026-06-09"


def test_whale_snapshot_has_no_date_when_nothing_is_datable(sc, tmp_path, monkeypatch):
    monkeypatch.setattr(sc, "CACHE", tmp_path)
    _write_caches(tmp_path, {}, {
        "fetched_at": "2026-08-02T23:00:00+00:00",
        "sentiment": {"score": 3, "label": "NEUTRAL", "as_of": "2026-08-02"},
        "btc": {},
    })
    got = sc.collect()["whale_sentiment_btc"]
    assert got["as_of"] is None
