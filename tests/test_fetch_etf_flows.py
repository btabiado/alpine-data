"""Tests for scripts/fetch_etf_flows.py — the crypto spot-ETF flow scraper.

The fetch itself cannot be exercised in CI without hitting Farside, so these
tests pin the two things that actually decide whether this scraper is safe:

  1. it parses Farside's real table shape into the repo's wide CSV schema
     (per-fund columns preserved, '(123.4)' read as negative), and
  2. it REFUSES to write when the parse looks wrong.

(2) matters more than (1). These CSVs are the ETF Flows tab's only history and
are not reconstructible from anywhere else, so a markup change upstream must
fail loudly rather than overwrite 600 rows with a partial table.

A third thing is now pinned as hard as (2): the difference between a zero and
a blank. Farside posts a day's flows overnight, so the CURRENT day sits on the
page with every cell "-". Read as zeros that becomes

    2026-08-03,0,0,0,0,0,0,0,0,0,0,0,0,0

— a cliff to zero on every fund's chart and a fake reading in the ETF
composite. The tests below use the REAL committed column shapes (read from
data/btc_flows.csv and data/eth_flows.csv, not a hand-written stand-in) to pin
both directions at once: an unsettled row is never written, and a genuinely
all-zero trading day still is.
"""
from __future__ import annotations

import importlib.util
import subprocess
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


# ---------- absence is not zero ----------
#
# These use the REAL committed column shape so the fixtures cannot drift away
# from the file the scraper actually writes.

def _real_columns(name: str) -> list[str]:
    """Header of the COMMITTED CSV — the git blob, not the working tree.

    The distinction is load-bearing in CI and invisible locally.
    `.github/workflows/tests.yml` deliberately overwrites both flow CSVs with
    one-row stubs before pytest runs:

        printf 'date,Total\\n2024-01-11,100\\n' > data/btc_flows.csv

    so the build step can prove the aggregator renders a dashboard from minimal
    input. Reading the working tree therefore returned ['date', 'Total'] on a
    runner, which made `n` 1 instead of 13 and malformed every fixture these
    helpers generate — six tests failing in CI while all six passed locally.

    The shape being asserted is a property of what is COMMITTED, so read that.
    """
    proc = subprocess.run(
        ["git", "show", f"HEAD:data/{name}"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        pytest.skip(f"git blob for data/{name} unavailable: "
                    f"{proc.stderr.strip()[:120]}")
    return proc.stdout.splitlines()[0].split(",")


def _real_shape_html(*data_rows: str) -> str:
    """Farside page carrying BTC's real 13 value columns (IBIT…BTC, Total)."""
    cols = _real_columns("btc_flows.csv")[1:]
    return _farside_html("".join(data_rows),
                         funds="</th><th>".join(cols[:-1]))


def test_real_csv_headers_are_the_shape_these_tests_assume():
    """Guard the fixtures above against a silent upstream column change."""
    btc = _real_columns("btc_flows.csv")
    eth = _real_columns("eth_flows.csv")
    # Both files must END in a named Total column. The ETH page's total header
    # cell is EMPTY upstream; the scraper used to name it positionally
    # ("COL11") and that shipped to main, where app.py's ensure_total() —
    # which only recognises a column literally named "total" — treated it as a
    # fund and summed it WITH the funds, doubling every ETH flow on the site.
    assert btc[0] == "date" and btc[-1] == "Total", btc
    assert eth[0] == "date" and eth[-1] == "Total", eth
    # The row shape quoted in the module docstring: date + 13 values.
    assert len(btc) == 14, btc
    assert len(eth) == 12, eth
    for req in BTC_REQUIRE:
        assert req in btc


def test_unsettled_row_of_dashes_is_not_written_as_zero():
    """THE bug: today's not-yet-posted row must not become a row of zeros.

    Farside renders the current day with every cell '-'. Charted as zeros it
    draws a cliff to zero on every fund and feeds the ETF composite a reading
    that was never taken.
    """
    n = len(_real_columns("btc_flows.csv")) - 1          # 13 value cells
    html = _real_shape_html(
        _row("12 May 2026", *(["1.5"] * n)),
        _row("3 Aug 2026", *(["-"] * n)),                # not settled yet
    )
    unsettled: list[str] = []
    header, rows = fef.parse_flow_table(html, BTC_REQUIRE, unsettled)

    assert len(header) == n + 1
    assert [r[0] for r in rows] == ["2026-05-12"], (
        "the unsettled day must not appear in the written rows")
    # ...and it must be disclosed, not silently dropped.
    assert unsettled == ["2026-08-03"]


def test_genuine_all_zero_trading_day_is_kept():
    """Absence is not zero — but zero is still a reading, and must survive."""
    n = len(_real_columns("btc_flows.csv")) - 1
    html = _real_shape_html(
        _row("12 May 2026", *(["1.5"] * n)),
        _row("13 May 2026", *(["0.0"] * n)),             # a real flat day
    )
    unsettled: list[str] = []
    _header, rows = fef.parse_flow_table(html, BTC_REQUIRE, unsettled)

    assert [r[0] for r in rows] == ["2026-05-12", "2026-05-13"]
    assert rows[1][1:] == ["0"] * n
    assert unsettled == []


def test_row_with_one_reading_survives_and_its_dashes_are_zero():
    """A settled row's per-fund '-' means that fund saw no flow: a zero."""
    n = len(_real_columns("btc_flows.csv")) - 1
    cells = ["-"] * n
    cells[0] = "250.0"                                   # IBIT reported
    html = _real_shape_html(_row("12 May 2026", *cells))
    header, rows = fef.parse_flow_table(html, BTC_REQUIRE)

    assert len(rows) == 1
    assert rows[0][header.index("IBIT")] == "250"
    assert rows[0][header.index("FBTC")] == "0"


def test_date_only_row_is_treated_as_no_reading():
    """Farside sometimes emits just the date cell for a day it hasn't posted.

    The old code padded the missing cells with '0', inventing a full row of
    zeros out of markup that contained no numbers at all.
    """
    n = len(_real_columns("btc_flows.csv")) - 1
    unsettled: list[str] = []
    html = _real_shape_html(
        _row("12 May 2026", *(["1.5"] * n)),
        "<tr><td>3 Aug 2026</td></tr>",                  # date, nothing else
    )
    _header, rows = fef.parse_flow_table(html, BTC_REQUIRE, unsettled)

    assert [r[0] for r in rows] == ["2026-05-12"]
    assert unsettled == ["2026-08-03"]


def test_eth_shape_also_rejects_the_unsettled_row():
    """Same contract on the narrower ETH table (11 value columns)."""
    eth_cols = _real_columns("eth_flows.csv")[1:]
    n = len(eth_cols)
    require = ("ETHA", "FETH", "ETHE")
    html = _farside_html(
        _row("11 May 2026", *(["2.0"] * n))
        + _row("3 Aug 2026", *(["-"] * n)),
        funds="</th><th>".join(eth_cols[:-1]),
    )
    unsettled: list[str] = []
    _header, rows = fef.parse_flow_table(html, require, unsettled)
    assert [r[0] for r in rows] == ["2026-05-11"]
    assert unsettled == ["2026-08-03"]


def test_stale_placeholder_row_is_dropped_and_does_not_wedge_the_scraper(
        tmp_path, monkeypatch):
    """End-to-end recovery from a placeholder a pre-fix run already wrote.

    The row is dated PAST anything the source reports, so without the purge
    `new_newest < old_newest` stays true and the scraper refuses to write for
    good — the fake zeros would outlive the fix that prevents them.
    """
    csv_path = tmp_path / "btc_flows.csv"
    csv_path.write_text(
        "date,IBIT,FBTC,GBTC,Total\n"
        "2024-01-11,111.7,227.0,-95.1,243.6\n"
        "2026-08-03,0,0,0,0\n",            # <- the placeholder, exactly as filed
        encoding="utf-8",
    )
    monkeypatch.setitem(fef.SOURCES, "btc", {
        "url": "https://example.invalid/btc", "csv": csv_path,
        "require": BTC_REQUIRE,
    })
    rows = "".join(
        _row(f"{(d % 28) + 1:02d} Jun 2026", "10", "20", "30", "60")
        for d in range(fef.MIN_ROWS + 5)
    )
    monkeypatch.setattr(fef, "fetch_html", lambda _u: _farside_html(rows))

    assert fef.refresh("btc") == 0, "the placeholder must not wedge the guard"

    text = csv_path.read_text()
    assert "2026-08-03" not in text, "the fabricated zero row must be gone"
    assert "2024-01-11" in text, "real history must survive the purge"


def test_purge_never_touches_a_real_row_dated_past_the_fetch(
        tmp_path, monkeypatch):
    """Only information-free rows are eligible; real data is never truncated."""
    csv_path = tmp_path / "btc_flows.csv"
    csv_path.write_text(
        "date,IBIT,FBTC,GBTC,Total\n"
        "2024-01-11,111.7,227.0,-95.1,243.6\n"
        "2026-07-01,5,0,0,5\n",             # newer than the fetch, but REAL
        encoding="utf-8",
    )
    monkeypatch.setitem(fef.SOURCES, "btc", {
        "url": "https://example.invalid/btc", "csv": csv_path,
        "require": BTC_REQUIRE,
    })
    rows = "".join(
        _row(f"{(d % 28) + 1:02d} Jun 2026", "10", "20", "30", "60")
        for d in range(fef.MIN_ROWS + 5)
    )
    monkeypatch.setattr(fef, "fetch_html", lambda _u: _farside_html(rows))

    # Refuses to regress, because the newer row is real and still stands.
    assert fef.refresh("btc") == 1
    assert "2026-07-01,5,0,0,5" in csv_path.read_text()


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
