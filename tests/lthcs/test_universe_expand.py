"""Tests for ``scripts/lthcs_universe_expand.py``.

Network calls (SEC ticker-map fetch, Yahoo smoke, Finnhub smoke) are
stubbed so the suite is deterministic and offline.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Dict

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "lthcs_universe_expand.py"


def _load_module() -> ModuleType:
    """Load the expand script as a module without invoking its CLI."""
    spec = importlib.util.spec_from_file_location("lthcs_universe_expand", SCRIPT_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["lthcs_universe_expand"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def expand() -> ModuleType:
    return _load_module()


# ---------------------------------------------------------------------------
# CSV → record converter (pure)
# ---------------------------------------------------------------------------

def test_validate_row_happy_path(expand: ModuleType) -> None:
    row = {
        "ticker": "ANET",
        "name": "Arista Networks, Inc.",
        "sector": "Technology",
        "sector_group": "Technology Hardware & Equipment",
        "maturity_stage": "growth_compounder",
        "index_membership": "S&P 500|NASDAQ-100",
        "tech_sub_bucket": "Networking",
        "aliases": "ARISTA|Arista",
    }
    result = expand.validate_row(row)
    assert result.passed
    assert result.errors == []
    assert result.record["ticker"] == "ANET"
    assert result.record["index_membership"] == ["S&P 500", "NASDAQ-100"]
    assert result.record["aliases"] == ["ARISTA", "Arista"]
    assert result.record["tech_sub_bucket"] == "Networking"
    assert result.record["active"] is True


@pytest.mark.parametrize(
    "bad",
    [
        "TOOLONG12345",      # 12 chars, max is 10
        "1ABC",              # starts with digit
        "FOO!",              # invalid char
        "FOO BAR",           # space
        "",                  # empty
    ],
)
def test_validate_row_rejects_bad_ticker_format(expand: ModuleType, bad: str) -> None:
    row = {
        "ticker": bad,
        "name": "Bad",
        "sector": "Technology",
        "sector_group": "x",
        "maturity_stage": "standard_compounder",
        "index_membership": "S&P 500",
    }
    result = expand.validate_row(row)
    assert not result.passed
    # Either the format regex fired, or required-column-missing did
    # (empty string short-circuits on the required-cols check).
    err_text = " | ".join(result.errors)
    assert ("format regex" in err_text) or ("missing required columns" in err_text)


def test_validate_row_rejects_unknown_sector(expand: ModuleType) -> None:
    row = {
        "ticker": "FOO",
        "name": "Foo Corp",
        "sector": "Crypto",  # not in taxonomy
        "sector_group": "x",
        "maturity_stage": "standard_compounder",
        "index_membership": "S&P 500",
    }
    result = expand.validate_row(row)
    assert not result.passed
    assert any("sector" in e and "taxonomy" in e for e in result.errors)


def test_validate_row_rejects_unknown_maturity_stage(expand: ModuleType) -> None:
    row = {
        "ticker": "FOO",
        "name": "Foo Corp",
        "sector": "Technology",
        "sector_group": "x",
        "maturity_stage": "rocketship",
        "index_membership": "S&P 500",
    }
    result = expand.validate_row(row)
    assert not result.passed
    assert any("maturity_stage" in e for e in result.errors)


def test_validate_row_missing_required_columns(expand: ModuleType) -> None:
    row = {"ticker": "FOO"}
    result = expand.validate_row(row)
    assert not result.passed
    assert any("missing required columns" in e for e in result.errors)


def test_validate_row_dot_ticker_accepted(expand: ModuleType) -> None:
    row = {
        "ticker": "BRK.B",
        "name": "Berkshire Hathaway B",
        "sector": "Financials",
        "sector_group": "Diversified Financials",
        "maturity_stage": "financial",
        "index_membership": "S&P 500",
    }
    result = expand.validate_row(row)
    assert result.passed


# ---------------------------------------------------------------------------
# process_row — enrichment with stubbed network
# ---------------------------------------------------------------------------

def test_process_row_resolves_cik_from_sec_map(expand: ModuleType) -> None:
    row = {
        "ticker": "FOO",
        "name": "Foo Corp",
        "sector": "Technology",
        "sector_group": "Software & Services",
        "maturity_stage": "growth_compounder",
        "index_membership": "S&P 500",
    }
    sec_map = {"FOO": "0000123456"}
    result = expand.process_row(
        row, sec_map=sec_map, cusip_map={}, do_network_smoke=False
    )
    assert result.passed
    assert result.record["cik"] == "0000123456"
    assert result.checks["cik"] == "resolved"
    assert result.checks["yahoo"]["detail"] == "skipped"
    assert result.checks["finnhub"]["detail"] == "skipped"


def test_process_row_warns_when_cik_missing(expand: ModuleType) -> None:
    row = {
        "ticker": "FOO",
        "name": "Foo Corp",
        "sector": "Technology",
        "sector_group": "Software & Services",
        "maturity_stage": "growth_compounder",
        "index_membership": "S&P 500",
    }
    result = expand.process_row(
        row, sec_map={}, cusip_map={}, do_network_smoke=False
    )
    # Validation still passes (CIK is a warning, not an error).
    assert result.passed
    assert "no CIK match in SEC ticker map" in result.warnings
    assert result.checks["cik"] == "missing"


def test_process_row_dot_ticker_cik_lookup(expand: ModuleType) -> None:
    """``BRK.B`` should resolve via the dot-stripped ``BRKB`` SEC key."""
    row = {
        "ticker": "BRK.B",
        "name": "Berkshire Hathaway B",
        "sector": "Financials",
        "sector_group": "Diversified Financials",
        "maturity_stage": "financial",
        "index_membership": "S&P 500",
    }
    sec_map = {"BRKB": "0001067983"}
    result = expand.process_row(
        row, sec_map=sec_map, cusip_map={}, do_network_smoke=False
    )
    assert result.passed
    assert result.record["cik"] == "0001067983"


def test_process_row_cusip_from_13f_map(expand: ModuleType) -> None:
    row = {
        "ticker": "FOO",
        "name": "Foo Corp",
        "sector": "Technology",
        "sector_group": "Software & Services",
        "maturity_stage": "growth_compounder",
        "index_membership": "S&P 500",
    }
    result = expand.process_row(
        row,
        sec_map={"FOO": "0000123456"},
        cusip_map={"FOO": "12345A678"},
        do_network_smoke=False,
    )
    assert result.record["cusip"] == "12345A678"
    assert result.checks["cusip"] == "resolved_from_13f_map"


# ---------------------------------------------------------------------------
# I/O: read_csv + write_results round-trip
# ---------------------------------------------------------------------------

def test_run_end_to_end_with_csv(tmp_path: Path, expand: ModuleType, monkeypatch) -> None:
    csv_path = tmp_path / "input.csv"
    csv_path.write_text(
        "\n".join(
            [
                "ticker,name,sector,sector_group,maturity_stage,index_membership",
                "ANET,Arista Networks,Technology,Hardware,growth_compounder,S&P 500",
                "PGR,Progressive,Financials,Insurance,financial,S&P 500",
                "junk,bad,Crypto,bad,bad,",  # row that should fail
            ]
        )
        + "\n"
    )

    # Force the SEC map loader to return a deterministic stub (no network).
    monkeypatch.setattr(
        expand,
        "_load_sec_ticker_map",
        lambda refresh=False: {"ANET": "0001596532", "PGR": "0000080661"},
    )
    monkeypatch.setattr(expand, "_load_cusip_map", lambda: {})
    out_dir = tmp_path / "out"
    summary = expand.run(
        input_path=csv_path,
        output_dir=out_dir,
        do_network_smoke=False,
    )
    assert summary["total"] == 3
    assert summary["passed_count"] == 2
    assert summary["failed_count"] == 1

    # Per-ticker JSONs land where the summary says they did.
    anet_json = json.loads((out_dir / "ANET.json").read_text())
    assert anet_json["passed"]
    assert anet_json["record"]["cik"] == "0001596532"

    summary_json = json.loads((out_dir / "_summary.json").read_text())
    assert summary_json["passed_count"] == 2
    assert "junk" not in [p["ticker"] for p in summary_json["passed"]]


def test_run_is_idempotent(tmp_path: Path, expand: ModuleType, monkeypatch) -> None:
    csv_path = tmp_path / "input.csv"
    csv_path.write_text(
        "ticker,name,sector,sector_group,maturity_stage,index_membership\n"
        "ANET,Arista,Technology,Hardware,growth_compounder,S&P 500\n"
    )
    monkeypatch.setattr(expand, "_load_sec_ticker_map", lambda refresh=False: {"ANET": "0001596532"})
    monkeypatch.setattr(expand, "_load_cusip_map", lambda: {})

    out_dir = tmp_path / "out"
    first = expand.run(input_path=csv_path, output_dir=out_dir, do_network_smoke=False)
    first_path = (out_dir / "ANET.json").read_text()

    # Second run on the same CSV should overwrite the same files with
    # the same shape (modulo the generated_at timestamp).
    second = expand.run(input_path=csv_path, output_dir=out_dir, do_network_smoke=False)
    second_path = (out_dir / "ANET.json").read_text()

    first_record = json.loads(first_path)["record"]
    second_record = json.loads(second_path)["record"]
    assert first_record == second_record
    assert first["passed_count"] == second["passed_count"]


# ---------------------------------------------------------------------------
# Optional smoke-fetch stubs
# ---------------------------------------------------------------------------

def test_yahoo_smoke_fetch_skipped_when_yfinance_unavailable(
    expand: ModuleType, monkeypatch
) -> None:
    """When yfinance is not importable, the script falls back to HTTP."""
    # Stub yfinance import and the HTTP fallback to a controllable shape.
    sys.modules.pop("yfinance", None)

    class StubResp:
        status_code = 200

        def json(self) -> Dict[str, Any]:
            return {"chart": {"result": [{"meta": {}}]}}

    class StubRequests:
        @staticmethod
        def get(url: str, **kwargs: Any) -> Any:
            return StubResp()

    monkeypatch.setattr("builtins.__import__", _import_blocking_yfinance(StubRequests))
    ok, detail = expand._yahoo_smoke_fetch("ANET")
    assert ok
    assert "yahoo chart" in detail


def _import_blocking_yfinance(stub_requests):
    """Patch import so ``yfinance`` raises ImportError but everything else works."""
    real_import = __import__

    def fake_import(name, *args, **kwargs):  # type: ignore[no-untyped-def]
        if name == "yfinance":
            raise ImportError("blocked by test")
        if name == "requests":
            return stub_requests
        return real_import(name, *args, **kwargs)

    return fake_import


def test_finnhub_smoke_fetch_skipped_without_key(
    expand: ModuleType, monkeypatch
) -> None:
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    ok, detail = expand._finnhub_smoke_fetch("ANET")
    assert not ok
    assert detail.startswith("skipped")
