"""Tests for the crypto data adapter (lthcs.sources.crypto_data).

These tests cover the pure helpers (no HTTP) and the file-backed
loaders. The CryptoDataAdapter is tested with the ``offline=True`` flag
so its lazy HTTP-fetching paths short-circuit. The runner-level
end-to-end test (also here) wires the adapter via a stub.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from lthcs.sources import crypto_data
from lthcs.sources.crypto_data import (
    CryptoDataAdapter,
    compute_etf_flow_30d,
    compute_etf_flow_pace,
    load_etf_flows,
    load_whale_payload,
    mean,
    pct_change_30d,
    values_only,
)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def test_pct_change_30d_returns_pct() -> None:
    series = [{"date": "d%d" % i, "value": 100.0} for i in range(31)]
    series[-1]["value"] = 110.0
    assert pct_change_30d(series) == pytest.approx(10.0)


def test_pct_change_30d_short_series_returns_none() -> None:
    series = [{"date": "d%d" % i, "value": 100.0} for i in range(10)]
    assert pct_change_30d(series) is None


def test_pct_change_30d_zero_base_returns_none() -> None:
    series = [{"date": "d%d" % i, "value": 0.0} for i in range(31)]
    series[-1]["value"] = 5.0
    assert pct_change_30d(series) is None


def test_values_only_filters_none() -> None:
    series = [
        {"date": "a", "value": 1.0},
        {"date": "b", "value": None},
        {"date": "c", "value": 2.0},
    ]
    assert values_only(series) == [1.0, 2.0]


def test_mean_empty_is_none() -> None:
    assert mean([]) is None
    assert mean([2.0, 4.0]) == pytest.approx(3.0)


def test_compute_etf_flow_30d_sums_tail() -> None:
    rows = [{"date": "d%d" % i, "total": 100.0} for i in range(30)]
    assert compute_etf_flow_30d(rows) == pytest.approx(3000.0)


def test_compute_etf_flow_30d_short_returns_none() -> None:
    rows = [{"date": "d%d" % i, "total": 100.0} for i in range(5)]
    assert compute_etf_flow_30d(rows) is None


def test_compute_etf_flow_pace_positive() -> None:
    # Prior 30 days: +30 USD each. Recent 30 days: +60 USD each.
    rows = (
        [{"date": "p%d" % i, "total": 30.0} for i in range(30)]
        + [{"date": "r%d" % i, "total": 60.0} for i in range(30)]
    )
    # (1800 - 900) / |900| = 1.0
    assert compute_etf_flow_pace(rows) == pytest.approx(1.0)


def test_compute_etf_flow_pace_short() -> None:
    rows = [{"date": "p%d" % i, "total": 30.0} for i in range(30)]
    assert compute_etf_flow_pace(rows) is None


# ---------------------------------------------------------------------------
# File loaders
# ---------------------------------------------------------------------------

def test_load_etf_flows_reads_csv(tmp_path: Path) -> None:
    csv_path = tmp_path / "btc_flows.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["date", "IBIT", "FBTC", "Total"])
        writer.writerow(["2026-04-18", "100", "50", "150.5"])
        writer.writerow(["2026-04-19", "0", "30", "30.0"])
    rows = load_etf_flows("BTC", data_dir=tmp_path)
    assert len(rows) == 2
    assert rows[0]["date"] == "2026-04-18"
    assert rows[0]["total"] == pytest.approx(150.5)


def test_load_etf_flows_unknown_symbol_empty(tmp_path: Path) -> None:
    assert load_etf_flows("XYZ", data_dir=tmp_path) == []


def test_load_etf_flows_missing_file(tmp_path: Path) -> None:
    assert load_etf_flows("BTC", data_dir=tmp_path) == []


def test_load_etf_flows_skips_bad_rows(tmp_path: Path) -> None:
    csv_path = tmp_path / "eth_flows.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["date", "ETHA", "Total"])
        writer.writerow(["2026-05-01", "10", "20.0"])
        writer.writerow(["", "10", "20.0"])  # missing date -> skipped
        writer.writerow(["2026-05-02", "10", ""])  # missing total -> skipped
        writer.writerow(["2026-05-03", "10", "not-a-number"])  # bad -> skipped
        writer.writerow(["2026-05-04", "10", "5.5"])
    rows = load_etf_flows("ETH", data_dir=tmp_path)
    assert [r["date"] for r in rows] == ["2026-05-01", "2026-05-04"]


def test_load_whale_payload_returns_empty_on_missing(tmp_path: Path) -> None:
    missing = tmp_path / "nope.json"
    assert load_whale_payload(missing) == {}


def test_load_whale_payload_reads_valid(tmp_path: Path) -> None:
    path = tmp_path / "whale.json"
    path.write_text(json.dumps({"btc": {"active_addresses": []}}))
    out = load_whale_payload(path)
    assert "btc" in out


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

def test_adapter_offline_inputs_have_expected_keys(tmp_path: Path) -> None:
    adapter = CryptoDataAdapter(data_dir=tmp_path, offline=True)
    inp = adapter.inputs_for("BTC")
    # All keys present even when data is missing -- pillars rely on
    # this contract.
    expected_keys = {
        "symbol",
        "active_addresses_series",
        "hash_rate_series",
        "tx_volume_usd_series",
        "miners_revenue_usd_series",
        "distribution_series",
        "market",
        "etf_flow_rows",
        "stablecoins",
    }
    assert expected_keys.issubset(inp.keys())
    assert inp["symbol"] == "BTC"


def test_adapter_offline_skips_http(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Setting OFFLINE the adapter must not call HTTP. We monkeypatch the
    # HTTP fetcher to raise so a regression is caught.
    monkeypatch.setattr(
        crypto_data, "_http_get",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("HTTP forbidden")),
    )
    adapter = CryptoDataAdapter(data_dir=tmp_path, offline=True)
    inp = adapter.inputs_for("BTC")
    assert inp["market"] == {}
    assert inp["stablecoins"]["delta_30d_pct"] is None


# ---------------------------------------------------------------------------
# Runner end-to-end (offline, dry-run)
# ---------------------------------------------------------------------------

def test_runner_dry_run_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts import lthcs_crypto_daily as runner

    universe_path = tmp_path / "crypto_universe.json"
    universe_path.write_text(json.dumps({
        "version": "1.0.0",
        "assets": [{"symbol": "BTC", "weight_profile": "btc", "active": True}],
    }))
    weights_path = tmp_path / "weights.json"
    weights_path.write_text(json.dumps({
        "profiles": {"btc": [0.10, 0.30, 0.25, 0.15, 0.20]},
        "score_bands": {"review": {"min": 0, "max": 100}},
    }))

    # Cache + data isolation.
    monkeypatch.setenv("LTHCS_CACHE_DIR", str(tmp_path / "cache"))

    rc = runner.run([
        "--dry-run", "--offline",
        "--universe", str(universe_path),
        "--weights", str(weights_path),
        "--calc-date", "2026-05-18",
    ])
    assert rc == 0
    # No snapshot file created.
    snapshot_dir = tmp_path / "snapshots_crypto"
    assert not snapshot_dir.exists()


def test_runner_writes_snapshot_and_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts import lthcs_crypto_daily as runner
    from lthcs import persist as persist_mod

    universe_path = tmp_path / "crypto_universe.json"
    universe_path.write_text(json.dumps({
        "version": "1.0.0",
        "assets": [
            {"symbol": "BTC", "weight_profile": "btc", "active": True},
            {"symbol": "ETH", "weight_profile": "eth", "active": True},
        ],
    }))
    weights_path = tmp_path / "weights.json"
    weights_path.write_text(json.dumps({
        "profiles": {
            "btc": [0.10, 0.30, 0.25, 0.15, 0.20],
            "eth": [0.25, 0.20, 0.20, 0.20, 0.15],
        },
        "score_bands": {"review": {"min": 0, "max": 100}},
    }))

    # Redirect the snapshot dir + persist data root into tmp_path.
    snap_dir = tmp_path / "snapshots_crypto"
    monkeypatch.setattr(runner, "_DEFAULT_SNAPSHOT_DIR", snap_dir)
    monkeypatch.setattr(persist_mod, "get_default_data_root",
                        lambda: tmp_path / "lthcs_data")
    monkeypatch.setenv("LTHCS_CACHE_DIR", str(tmp_path / "cache"))

    rc = runner.run([
        "--offline",
        "--universe", str(universe_path),
        "--weights", str(weights_path),
        "--calc-date", "2026-05-18",
    ])
    assert rc == 0

    # Snapshot exists with expected shape.
    snap_path = snap_dir / "2026-05-18.json"
    assert snap_path.exists()
    payload = json.loads(snap_path.read_text())
    assert payload["calc_date"] == "2026-05-18"
    assert payload["asset_class"] == "crypto"
    assert len(payload["scores"]) == 2
    syms = {r["ticker"] for r in payload["scores"]}
    assert syms == {"BTC", "ETH"}

    # Per-ticker history written.
    hist_dir = tmp_path / "lthcs_data" / "history" / "by_ticker"
    assert (hist_dir / "BTC.json").exists()
    assert (hist_dir / "ETH.json").exists()
    btc_hist = json.loads((hist_dir / "BTC.json").read_text())
    assert btc_hist["history"][0]["date"] == "2026-05-18"


def test_runner_refuses_to_overwrite_without_force(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts import lthcs_crypto_daily as runner
    from lthcs import persist as persist_mod

    universe_path = tmp_path / "crypto_universe.json"
    universe_path.write_text(json.dumps({
        "version": "1.0.0",
        "assets": [{"symbol": "BTC", "weight_profile": "btc", "active": True}],
    }))
    weights_path = tmp_path / "weights.json"
    weights_path.write_text(json.dumps({
        "profiles": {"btc": [0.10, 0.30, 0.25, 0.15, 0.20]},
        "score_bands": {"review": {"min": 0, "max": 100}},
    }))

    snap_dir = tmp_path / "snapshots_crypto"
    monkeypatch.setattr(runner, "_DEFAULT_SNAPSHOT_DIR", snap_dir)
    monkeypatch.setattr(persist_mod, "get_default_data_root",
                        lambda: tmp_path / "lthcs_data")
    monkeypatch.setenv("LTHCS_CACHE_DIR", str(tmp_path / "cache"))

    args = [
        "--offline",
        "--universe", str(universe_path),
        "--weights", str(weights_path),
        "--calc-date", "2026-05-18",
    ]
    assert runner.run(args) == 0
    # Second run without --force returns the snapshot-exists code.
    assert runner.run(args) == 3
    # With --force the second run succeeds.
    assert runner.run(args + ["--force"]) == 0
