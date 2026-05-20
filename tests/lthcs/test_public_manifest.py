"""Tests for ``scripts.lthcs_build_public_manifest``.

Every test builds against a synthetic ``data/lthcs/`` tree under ``tmp_path``,
so we never touch the committed manifest. The script is intentionally
pure-I/O — fixtures lay down the on-disk inputs, the test asserts the
outputs match.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import lthcs_build_public_manifest as mod


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2))


@pytest.fixture()
def data_root(tmp_path: Path) -> Path:
    """Synthetic data/lthcs/ tree with a universe, crypto universe, and
    three dated snapshots. Sufficient for every assertion in this file.
    """
    root = tmp_path / "lthcs"
    # Universe: 3 active + 1 inactive — active count is what should land
    # in the manifest.
    _write_json(
        root / "universe.json",
        {
            "version": "test",
            "tickers": [
                {"ticker": "AAPL", "active": True},
                {"ticker": "MSFT", "active": True},
                {"ticker": "GOOG", "active": True},
                {"ticker": "DEAD", "active": False},
            ],
        },
    )
    _write_json(
        root / "crypto_universe.json",
        {
            "version": "test",
            "assets": [
                {"symbol": "BTC", "active": True},
                {"symbol": "ETH", "active": True},
            ],
        },
    )
    # Three dated snapshots so the latest-picker has something non-trivial
    # to choose from.
    for date in ("2026-05-15", "2026-05-16", "2026-05-17"):
        _write_json(
            root / "snapshots" / f"{date}.json",
            {"calc_date": date, "scores": [{"ticker": "AAPL"}]},
        )
    # Decoy file under snapshots/ — must NOT be picked as latest.
    _write_json(root / "snapshots" / "index.json", {"dates": []})
    return root


# ---------------------------------------------------------------------------
# Manifest shape
# ---------------------------------------------------------------------------


def test_manifest_has_all_expected_top_level_keys(data_root: Path):
    m = mod.build_manifest(data_root, frozen_time="2026-05-20T23:00:00Z")
    expected = {
        "generated_at",
        "latest_snapshot_date",
        "universe_size",
        "crypto_universe_size",
        "pillars",
        "bands",
        "data_endpoints",
        "version",
        "license",
    }
    assert set(m.keys()) == expected


def test_manifest_picks_latest_dated_snapshot(data_root: Path):
    m = mod.build_manifest(data_root, frozen_time="2026-05-20T23:00:00Z")
    assert m["latest_snapshot_date"] == "2026-05-17"


def test_manifest_counts_only_active_universe_entries(data_root: Path):
    m = mod.build_manifest(data_root, frozen_time="2026-05-20T23:00:00Z")
    assert m["universe_size"] == 3  # 3 active, 1 inactive in fixture
    assert m["crypto_universe_size"] == 2


def test_manifest_pillars_and_bands_are_canonical(data_root: Path):
    m = mod.build_manifest(data_root, frozen_time="2026-05-20T23:00:00Z")
    assert m["pillars"] == [
        "adoption_momentum",
        "institutional_confidence",
        "financial_evolution",
        "thesis_integrity",
        "des",
    ]
    assert len(m["pillars"]) == 5
    assert m["bands"] == [
        "elite",
        "high_confidence",
        "constructive",
        "monitor",
        "weakening",
        "review",
    ]
    assert len(m["bands"]) == 6


def test_manifest_version_is_present_and_v_prefixed(data_root: Path):
    m = mod.build_manifest(data_root, frozen_time="2026-05-20T23:00:00Z")
    assert isinstance(m["version"], str)
    assert m["version"].startswith("v")


def test_manifest_license_is_non_empty(data_root: Path):
    m = mod.build_manifest(data_root, frozen_time="2026-05-20T23:00:00Z")
    assert "informational" in m["license"].lower()
    assert "not investment advice" in m["license"].lower()


def test_manifest_generated_at_is_iso_z(data_root: Path):
    m = mod.build_manifest(data_root, frozen_time="2026-05-20T23:00:00Z")
    assert m["generated_at"] == "2026-05-20T23:00:00Z"


# ---------------------------------------------------------------------------
# Endpoints — well-formedness
# ---------------------------------------------------------------------------


def test_endpoints_cover_expected_paths(data_root: Path):
    m = mod.build_manifest(data_root, frozen_time="2026-05-20T23:00:00Z")
    paths = [row["endpoint"] for row in m["data_endpoints"]]
    # Every required endpoint is covered. We use 'any startswith' because
    # dated paths have the current latest_snapshot_date interpolated.
    required = [
        "/data/lthcs/snapshots/",
        "/data/lthcs/snapshots_crypto/",
        "/data/lthcs/history/by_ticker/",
        "/data/lthcs/narratives/",
        "/data/lthcs/narratives_llm/",
        "/data/lthcs/variable_detail/",
        "/data/lthcs/backtest/",
        "/data/lthcs/weights.json",
    ]
    for prefix in required:
        assert any(p.startswith(prefix) for p in paths), (
            f"missing endpoint with prefix {prefix!r}; got {paths!r}"
        )


def test_endpoint_urls_are_well_formed(data_root: Path):
    m = mod.build_manifest(data_root, frozen_time="2026-05-20T23:00:00Z")
    for row in m["data_endpoints"]:
        url = row["endpoint"]
        # Absolute site-relative path, no scheme, no '..', no double slashes.
        assert url.startswith("/data/lthcs/"), url
        assert ".." not in url
        assert "//" not in url
        # Every endpoint must have a non-empty description + shape.
        assert row.get("description"), row
        assert row.get("shape"), row


def test_dated_endpoints_use_latest_snapshot_date(data_root: Path):
    m = mod.build_manifest(data_root, frozen_time="2026-05-20T23:00:00Z")
    # snapshots/, snapshots_crypto/, narratives/, narratives_llm/,
    # variable_detail/ — five dated endpoints all reference today.
    dated = [
        row["endpoint"]
        for row in m["data_endpoints"]
        if "2026-05-17" in row["endpoint"]
    ]
    assert len(dated) == 5


# ---------------------------------------------------------------------------
# Latest-snapshot mirror
# ---------------------------------------------------------------------------


def test_latest_snapshot_mirror_is_byte_equal_copy(data_root: Path):
    mod.build_and_write(data_root, frozen_time="2026-05-20T23:00:00Z")
    src = data_root / "snapshots" / "2026-05-17.json"
    dst = data_root / "public" / "latest_snapshot.json"
    assert dst.is_file()
    assert dst.read_bytes() == src.read_bytes()


def test_latest_snapshot_mirror_skipped_when_no_dated_snapshot(tmp_path: Path):
    # Empty data_root with no snapshots/ — builder should still write
    # manifest but skip the mirror copy.
    root = tmp_path / "lthcs"
    root.mkdir()
    mod.build_and_write(root, frozen_time="2026-05-20T23:00:00Z")
    assert (root / "public" / "manifest.json").is_file()
    assert not (root / "public" / "latest_snapshot.json").exists()


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_build_is_idempotent_on_repeated_runs(data_root: Path):
    """Two back-to-back runs with the same frozen_time produce byte-identical
    output. ``generated_at`` is the only non-deterministic field — pinning
    it makes the rest of the manifest reproducible.
    """
    mod.build_and_write(data_root, frozen_time="2026-05-20T23:00:00Z")
    manifest_path = data_root / "public" / "manifest.json"
    snap_path = data_root / "public" / "latest_snapshot.json"
    first_manifest = manifest_path.read_bytes()
    first_snap = snap_path.read_bytes()

    mod.build_and_write(data_root, frozen_time="2026-05-20T23:00:00Z")
    assert manifest_path.read_bytes() == first_manifest
    assert snap_path.read_bytes() == first_snap


def test_manifest_json_is_sorted_keys(data_root: Path):
    """Stable key order is what makes idempotency hold across Python
    versions / dict insertion order quirks. Verify json output is sorted.
    """
    mod.build_and_write(data_root, frozen_time="2026-05-20T23:00:00Z")
    raw = (data_root / "public" / "manifest.json").read_text()
    parsed = json.loads(raw)
    # Re-dump with sort_keys=True and confirm round-trip is byte-equal
    # (modulo trailing newline our writer adds).
    expected = json.dumps(parsed, indent=2, sort_keys=True) + "\n"
    assert raw == expected


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def test_cli_writes_files_under_data_root(data_root: Path, capsys):
    rc = mod.main(
        [
            "--data-root",
            str(data_root),
            "--frozen-time",
            "2026-05-20T23:00:00Z",
        ]
    )
    assert rc == 0
    assert (data_root / "public" / "manifest.json").is_file()
    assert (data_root / "public" / "latest_snapshot.json").is_file()
    out = capsys.readouterr().out
    assert "wrote" in out
