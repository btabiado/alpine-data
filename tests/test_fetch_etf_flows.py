"""Tests for scripts/fetch_etf_flows.py — the crypto spot-ETF flow scraper.

The fetch itself cannot be exercised in CI without hitting Farside, so these
tests pin the two things that actually decide whether this scraper is safe:

  1. it parses Farside's real table shape into the repo's wide CSV schema
     (per-fund columns preserved, '(123.4)' read as negative, '-' as zero), and
  2. it REFUSES to write when the parse looks wrong.

(2) matters more than (1). These CSVs are the ETF Flows tab's only history and
are not reconstructible from anywhere else, so a markup change upstream must
fail loudly rather than overwrite 600 rows with a partial table.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "fetch_etf_flows", REPO_ROOT / "scripts" / "fetch_etf_flows.py"
)
fef = importlib.util.module_from_spec(_spec)
sys.modules["fetch_etf_flows"] = fef
_spec.loader.exec_module(fef)


BTC_REQUIRE = ("IBIT", "FBTC", "GBTC")


def _farside_html(rows: str, funds: str = "IBIT</th><th>FBTC</th><th>GBTC") -> str:
    """Minimal page in Farside's shape: a header row of tickers, then date rows."""
    return f"""
    <html><body>
      <table>
        <tr><th>Date</th><th>{funds}</th><th>Total</th></tr>
        {rows}
      </table>
    </body></html>
    """


def _row(date: str, *cells: str) -> str:
    tds = "".join(f"<td>{c}</td>" for c in cells)
    return f"<tr><td>{date}</td>{tds}</tr>"


# ---------- parsing ----------

def test_parses_farside_shape_into_wide_csv():
    html = _farside_html(
        _row("11 Jan 2024", "111.7", "227.0", "(95.1)", "243.6")
        + _row("12 Jan 2024", "-", "1,234.5", "(17.6)", "1216.9")
    )
    header, rows = fef.parse_flow_table(html, BTC_REQUIRE)

    assert header[0] == "date"
    assert "IBIT" in header and "FBTC" in header and "GBTC" in header
    assert len(rows) == 2
    assert rows[0][0] == "2024-01-11"
    # '(95.1)' is Farside's negative notation
    assert rows[0][header.index("GBTC")] == "-95.1"
    # '-' means no flow, and thousands separators must survive
    assert rows[1][header.index("IBIT")] == "0"
    assert rows[1][header.index("FBTC")] == "1234.5"


def test_skips_non_date_footer_rows():
    """Farside appends Total/Average rows; they must not become data."""
    html = _farside_html(
        _row("11 Jan 2024", "1", "2", "3", "6")
        + _row("Total", "100", "200", "300", "600")
        + _row("Average", "50", "100", "150", "300")
    )
    _header, rows = fef.parse_flow_table(html, BTC_REQUIRE)
    assert [r[0] for r in rows] == ["2024-01-11"]


def test_returns_empty_when_expected_funds_are_absent():
    """A page that isn't the flow table must not be parsed as one."""
    html = _farside_html(_row("11 Jan 2024", "1", "2", "3", "6"),
                         funds="FOO</th><th>BAR</th><th>BAZ")
    header, rows = fef.parse_flow_table(html, BTC_REQUIRE)
    assert header == [] and rows == []


# ---------- the safety contract ----------

def _seed(tmp_path: Path, name: str, last_date: str) -> Path:
    p = tmp_path / name
    p.write_text(
        "date,IBIT,FBTC,GBTC,Total\n"
        "2024-01-11,111.7,227.0,-95.1,243.6\n"
        f"{last_date},1,2,3,6\n",
        encoding="utf-8",
    )
    return p


@pytest.fixture()
def btc_cfg(tmp_path, monkeypatch):
    csv_path = _seed(tmp_path, "btc_flows.csv", "2026-05-12")
    monkeypatch.setitem(fef.SOURCES, "btc", {
        "url": "https://example.invalid/btc",
        "csv": csv_path,
        "require": BTC_REQUIRE,
    })
    return csv_path


def test_preserves_csv_when_fetch_fails(btc_cfg, monkeypatch):
    before = btc_cfg.read_text()

    def boom(_url):
        raise OSError("connection reset")
    monkeypatch.setattr(fef, "fetch_html", boom)

    assert fef.refresh("btc") == 1
    assert btc_cfg.read_text() == before, "a failed fetch must not touch the CSV"


def test_refuses_partial_table(btc_cfg, monkeypatch):
    """A handful of parsed rows means the markup changed — do not overwrite."""
    before = btc_cfg.read_text()
    html = _farside_html("".join(
        _row(f"{d:02d} Jan 2024", "1", "2", "3", "6") for d in range(1, 6)
    ))
    monkeypatch.setattr(fef, "fetch_html", lambda _u: html)

    assert fef.refresh("btc") == 1
    assert btc_cfg.read_text() == before


def test_refuses_to_regress_to_older_data(btc_cfg, monkeypatch):
    """The dead mirror bug: a source ending BEFORE what we have must be rejected."""
    before = btc_cfg.read_text()
    html = _farside_html("".join(
        _row("01 Jan 2025", "1", "2", "3", "6") for _ in range(fef.MIN_ROWS + 10)
    ))
    monkeypatch.setattr(fef, "fetch_html", lambda _u: html)

    assert fef.refresh("btc") == 1
    assert btc_cfg.read_text() == before


def test_writes_and_merges_without_truncating_history(btc_cfg, monkeypatch):
    """A good fetch appends new dates and keeps every historical row."""
    rows = "".join(
        _row(f"{(d % 28) + 1:02d} Jun 2026", "10", "20", "30", "60")
        for d in range(fef.MIN_ROWS + 5)
    )
    monkeypatch.setattr(fef, "fetch_html", lambda _u: _farside_html(rows))

    assert fef.refresh("btc") == 0

    text = btc_cfg.read_text()
    assert "2024-01-11" in text, "pre-existing history must survive the merge"
    assert "2026-06-" in text, "new rows must be written"
    dates = [l.split(",")[0] for l in text.strip().splitlines()[1:]]
    assert dates == sorted(dates), "output must stay date-sorted"
