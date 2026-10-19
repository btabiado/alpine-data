"""bitinfocharts retired 2026-10-19 (daily audit, HTTP 403).

The whale job must not request that host. Cohort supply has no equivalent on
blockchain.info, mempool.space, or Blockchair, so the fetcher keeps the last
good rows and flags them stale/unavailable. The probe reports the host as
retired, which the daily audit does not treat as a new block.
"""
from __future__ import annotations

import json
from pathlib import Path

import fetch_market
import api_status
from lthcs.sources.crypto_data import whale_distribution

ROOT = Path(__file__).resolve().parent.parent


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(fetch_market, "CACHE", tmp_path)
    monkeypatch.setattr(fetch_market, "_STALE_DIR", tmp_path / ".stale")

    def boom(*args, **kwargs):
        raise AssertionError(f"unexpected HTTP {args!r} {kwargs!r}")

    monkeypatch.setattr(fetch_market.requests, "get", boom)


def _bucket(day: str, whale: float = 1000.0) -> dict:
    return {
        "date": day,
        "b0_01": 1.0, "b01_1": 2.0, "b1_10": 3.0, "b10_100": 4.0,
        "b100_1k": 5.0, "b1k_10k": whale, "b10k_100k": 50.0, "b100k_1m": 10.0,
    }


def test_cohort_fetch_does_not_call_bitinfocharts_and_records_the_gap(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    out = fetch_market.btc_cohort_distribution()
    assert out["status"] == "known_gap"
    assert out["unavailable"] is True
    assert out["stale"] is True
    assert out["source"] == "known-gap"
    assert out["buckets"] == []
    assert out["as_of"] is None
    assert "2026-10-19" in out["reason"]
    assert "blockchain.info" in out["reason"]
    src = (ROOT / "fetch_market.py").read_text(encoding="utf-8")
    assert "https://bitinfocharts.com" not in src


def test_last_good_rows_are_kept_flagged_and_seeded_once(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    (tmp_path / "whale.json").write_text(json.dumps({
        "distribution": {
            "source": "bitinfocharts.com",
            "buckets": [_bucket("2026-10-17"), _bucket("2026-10-18", 1100.0)],
        }
    }))
    saves = []
    real_save = fetch_market._stale_save

    def counting_save(name, value):
        saves.append(name)
        return real_save(name, value)

    monkeypatch.setattr(fetch_market, "_stale_save", counting_save)
    out = fetch_market.btc_cohort_distribution()
    assert [r["date"] for r in out["buckets"]] == ["2026-10-17", "2026-10-18"]
    assert out["as_of"] == "2026-10-18"
    assert out["historical_source"] == "bitinfocharts.com"
    assert out["stale"] is True and out["unavailable"] is True
    assert saves == [fetch_market.BTC_COHORT_STALE_KEY]

    (tmp_path / "whale.json").unlink()
    out2 = fetch_market.btc_cohort_distribution()
    assert out2["as_of"] == "2026-10-18"
    assert out2["buckets"][-1]["b1k_10k"] == 1100.0
    assert saves == [fetch_market.BTC_COHORT_STALE_KEY], "a later run must not refresh stale_age"


def test_live_sentiment_omits_a_known_gap_cohort():
    cohort_days = [f"2026-07-{d:02d}" for d in range(1, 32)]
    live_days = [f"2026-08-{d:02d}" for d in range(1, 32)]
    series = [{"date": d, "value": 100.0 + i} for i, d in enumerate(live_days)]
    whale = {
        "fetched_at": "2026-10-19T12:00:00+00:00",
        "distribution": fetch_market._cohort_gap(
            [_bucket(d, 1000.0 + i) for i, d in enumerate(cohort_days)],
            "bitinfocharts.com",
        ),
        "btc": {k: series for k in (
            "hash_rate", "miners_revenue_usd", "avg_tx_usd",
            "output_volume_btc", "active_addresses")},
    }
    # The gap envelope's as_of is the last cohort day. The composite must
    # ignore it and date itself from the live blockchain.info series.
    assert whale["distribution"]["as_of"] == "2026-07-31"
    out = fetch_market.compute_whale_sentiment(whale)
    assert out is not None
    names = [c["name"] for c in out["components"]]
    assert "Whale supply Δ30d" not in names
    assert out["as_of"] == "2026-08-31"
    assert "known gap" in out["disclaimer"]
    assert whale_distribution(whale) == []


def test_historical_payload_still_scores_its_cohort_table():
    """Backfill recomputes days from a whale payload that predates the gap flag."""
    dist = [_bucket(f"2026-07-{d:02d}", 1000.0 + d) for d in range(1, 32)]
    series = [{"date": r["date"], "value": 10.0} for r in dist]
    whale = {
        "fetched_at": "2026-08-01T00:00:00+00:00",
        "distribution": {"buckets": dist, "source": "bitinfocharts.com"},
        "btc": {"hash_rate": series},
    }
    out = fetch_market.compute_whale_sentiment(whale)
    assert any(c["name"] == "Whale supply Δ30d" for c in out["components"])
    assert whale_distribution(whale)


def test_probe_does_not_request_the_retired_host(monkeypatch):
    target = next(t for t in api_status.TARGETS if t["label"] == "bitinfocharts")
    assert target.get("retired") is True

    def refuse(*args, **kwargs):
        raise AssertionError(f"retired host was probed: {args!r} {kwargs!r}")

    monkeypatch.setattr(api_status.urllib.request, "urlopen", refuse)
    if api_status.requests is not None:
        monkeypatch.setattr(api_status.requests, "get", refuse)

    row = api_status._probe_one(target, timeout=1, attempts=1)
    assert row["verdict"] == "retired"
    assert row["status"] is None
    assert row["reachable"] is False
    assert "Not probed" in row["note"]
    assert "2026-10-19" in row["note"]


def test_committed_snapshot_marks_bitinfocharts_retired():
    snap = json.loads((ROOT / "data" / "health" / "api_status.json").read_text())
    row = next(s for s in snap["sources"] if s["label"] == "bitinfocharts")
    assert row["verdict"] == "retired"
    assert row["status"] is None
    blocked = sum(1 for s in snap["sources"] if s["verdict"] == "blocked")
    retired = sum(1 for s in snap["sources"] if s["verdict"] == "retired")
    assert snap["summary"]["blocked"] == blocked
    assert snap["summary"]["retired"] == retired == 1


def test_dashboard_names_the_cohort_gap():
    src = (ROOT / "app.py").read_text(encoding="utf-8")
    assert 'id="whaleCohortGap"' in src
    assert "Stale / unavailable." in src
    assert ">bitinfocharts</span>" not in src
