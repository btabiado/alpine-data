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
    # '-' means IBIT has not reported: an empty cell, not 0 ...
    assert rows[1][header.index("IBIT")] == ""
    # ... thousands separators must survive ...
    assert rows[1][header.index("FBTC")] == "1234.5"
    # ... and Farside's Total (a sum of whatever is filled in) is not the
    # day's total while a fund that reported the day before is missing.
    assert rows[1][header.index("Total")] == ""


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
    # 13, not 12: Farside added MSSE to the ETH table after this test was
    # written (its first flow is 2026-07-28). That is a real fund, not a
    # repeat of the COL11 defect above -- Total equals the per-fund sum on
    # every row of the committed CSV, so MSSE is counted once, inside the
    # total, not added on top of it. This guard tripping on a new listing is
    # it working: update the count after confirming the sum still holds.
    assert len(eth) == 13, eth
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


def test_row_with_one_reading_survives_and_its_dashes_stay_empty():
    """A row with one reading is written, but its '-' cells are NOT zeros.

    This test used to pin the opposite ('-' on a written row means 0). That
    is how 2026-10-02 shipped as IBIT 0 / Total 31.7 while IBIT simply had
    not reported yet; it later came in at 158.2 and the day at 189.9.
    """
    n = len(_real_columns("btc_flows.csv")) - 1
    cells = ["-"] * n
    cells[0] = "250.0"                                   # IBIT reported
    html = _real_shape_html(_row("12 May 2026", *cells))
    header, rows = fef.parse_flow_table(html, BTC_REQUIRE)

    assert len(rows) == 1
    assert rows[0][header.index("IBIT")] == "250"
    assert rows[0][header.index("FBTC")] == ""
    assert rows[0][header.index("Total")] == ""


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
    """Same contract on the narrower ETH table (12 value columns since MSSE)."""
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


# ---------- pre-launch / duplicate-row guard ----------
#
# data/btc_flows.csv carried a hand-pasted 2024-01-01 row that duplicated
# 2024-01-11 (the first day US spot-bitcoin ETFs traded), inflating the
# all-time cumulative by $655.3M. These pin the guard and the committed data.

def test_drop_pre_launch_removes_rows_before_first_trading_day():
    rows = {"2024-01-01": ["2024-01-01", "1"], "2024-01-11": ["2024-01-11", "1"]}
    assert fef.drop_pre_launch(rows, "2024-01-11") == ["2024-01-01"]
    assert list(rows) == ["2024-01-11"]


def test_duplicate_value_rows_flags_multi_fund_copy_but_not_single_fund_repeat():
    rows = [
        ["2024-01-11", "111.7", "227", "-95.1", "243.6"],
        ["2024-01-12", "111.7", "227.0", "-95.1", "243.6"],   # copy-paste
        ["2026-06-18", "-12.8", "0", "0", "-12.8"],
        ["2026-06-26", "-12.8", "0", "0", "-12.8"],           # one fund, plausible
    ]
    assert fef.duplicate_value_rows(rows) == [("2024-01-11", "2024-01-12")]


def test_refresh_drops_a_pre_launch_row_already_on_disk(tmp_path, monkeypatch):
    csv_path = tmp_path / "btc_flows.csv"
    csv_path.write_text(
        "date,IBIT,FBTC,GBTC,Total\n"
        "2024-01-01,111.7,227.0,-95.1,243.6\n"
        "2024-01-11,111.7,227,-95.1,243.6\n",
        encoding="utf-8",
    )
    monkeypatch.setitem(fef.SOURCES, "btc", {
        "url": "https://example.invalid/btc", "csv": csv_path,
        "require": BTC_REQUIRE, "first_trading_day": "2024-01-11",
    })
    rows = "".join(
        _row(f"{(d % 28) + 1:02d} Jun 2026", "10", "20", "30", "60")
        for d in range(fef.MIN_ROWS + 5)
    )
    monkeypatch.setattr(fef, "fetch_html", lambda _u: _farside_html(rows))
    assert fef.refresh("btc") == 0
    text = csv_path.read_text()
    assert "2024-01-01" not in text
    assert "2024-01-11" in text


@pytest.mark.parametrize("name,first_day", [
    ("btc_flows.csv", "2024-01-11"),
    ("eth_flows.csv", "2024-07-23"),
])
def test_committed_csv_has_no_pre_launch_or_duplicate_rows(name, first_day):
    """Read the COMMITTED blob (CI stubs the working-tree CSVs)."""
    proc = subprocess.run(["git", "show", f"HEAD:data/{name}"],
                          cwd=REPO_ROOT, capture_output=True, text=True)
    if proc.returncode != 0:
        pytest.skip(f"git blob for data/{name} unavailable")
    lines = proc.stdout.strip().splitlines()[1:]
    rows = [l.split(",") for l in lines]
    dates = [r[0] for r in rows]
    assert not [d for d in dates if d < first_day], "rows before first ETF trading day"
    assert len(dates) == len(set(dates)), "duplicate dates"
    assert fef.duplicate_value_rows(rows) == [], "copy-pasted multi-fund rows"


# ---------- unreported cells and Farside's running Total ----------
#
# Farside's Total column is a SUM over whatever cells are filled in, so it
# prints a number on a day nobody has reported (0.0) and a partial sum on a
# day still being published. Both reached data/btc_flows.csv as real flows:
#   2026-10-06,0,0,0,0,0,0,0,0,0,0,0,0,0     (committed 15:59Z, before the close)
#   2026-10-02,0,29.3,...,2.4,0,0,31.7       (IBIT not in yet; final day 189.9)
# These pin the shapes that produced them.

from datetime import date, timedelta  # noqa: E402


def _cells(header: list[str], **vals: str) -> list[str]:
    """Value cells in `header` order: every fund '-' unless given."""
    return [vals.get(c, "-") for c in header[1:]]


def _real_header() -> list[str]:
    return _real_columns("btc_flows.csv")


def test_todays_row_with_farside_formula_total_is_not_written():
    """Every fund '-', Total '0.0' (the formula over empty cells): no row."""
    hdr = _real_header()
    settled = _cells(hdr, **{c: "1.5" for c in hdr[1:]})
    today = _cells(hdr, Total="0.0")
    html = _real_shape_html(_row("05 Oct 2026", *settled),
                            _row("06 Oct 2026", *today))
    unsettled: list[str] = []
    withheld: list[str] = []
    _h, rows = fef.parse_flow_table(html, BTC_REQUIRE, unsettled, withheld)

    assert [r[0] for r in rows] == ["2026-10-05"]
    assert unsettled == ["2026-10-06"]
    assert withheld == []          # not written at all, so nothing to withhold


def test_partly_published_day_keeps_its_readings_and_withholds_the_total():
    """The 2026-10-02 shape: FBTC and MSBT in, IBIT and the rest still '-'."""
    hdr = _real_header()
    quiet = {c: "0.0" for c in hdr[1:]}
    settled = _cells(hdr, **{**quiet, "IBIT": "195.6", "FBTC": "(60.7)",
                             "Total": "134.9"})
    partial = _cells(hdr, FBTC="29.3", MSBT="2.4", Total="31.7")
    html = _real_shape_html(_row("01 Oct 2026", *settled),
                            _row("02 Oct 2026", *partial))
    withheld: list[str] = []
    header, rows = fef.parse_flow_table(html, BTC_REQUIRE, withheld=withheld)

    day = dict(zip(header, rows[1]))
    assert day["date"] == "2026-10-02"
    assert day["FBTC"] == "29.3" and day["MSBT"] == "2.4"   # real readings stay
    assert day["IBIT"] == "" and day["GBTC"] == ""          # not reported != 0
    assert day["Total"] == "", "a partial sum must not be published as the day"
    assert withheld == ["2026-10-02"]
    # The complete day before keeps Farside's Total and its literal zeros.
    prev = dict(zip(header, rows[0]))
    assert prev["Total"] == "134.9" and prev["GBTC"] == "0"


def test_dash_for_a_fund_not_launched_yet_does_not_withhold_the_total():
    """MSBT shows '-' until its first session; that is not a missing report."""
    hdr = _real_header()
    base = {c: "1.0" for c in hdr[1:] if c not in ("MSBT", "Total")}
    rows_html = [
        _row(f"{d:02d} Apr 2026", *_cells(hdr, **base, Total="11.0"))
        for d in (6, 7)
    ]
    html = _real_shape_html(*rows_html)
    withheld: list[str] = []
    header, rows = fef.parse_flow_table(html, BTC_REQUIRE, withheld=withheld)

    assert withheld == []
    assert [dict(zip(header, r))["Total"] for r in rows] == ["11", "11"]
    assert dict(zip(header, rows[1]))["MSBT"] == ""


def test_market_holiday_row_is_not_written_even_when_farside_prints_zeros():
    """2025-02-17 was Presidents' Day: no session, so no flow, not a 0 flow."""
    html = _farside_html(
        _row("14 Feb 2025", "10.0", "0.0", "(5.0)", "5.0")
        + _row("17 Feb 2025", "0.0", "0.0", "0.0", "0.0")      # NYSE closed
        + _row("15 Feb 2025", "-", "-", "-", "0.0")            # a Saturday
        + _row("18 Feb 2025", "1.0", "2.0", "3.0", "6.0")
    )
    unsettled: list[str] = []
    _h, rows = fef.parse_flow_table(html, BTC_REQUIRE, unsettled)

    assert [r[0] for r in rows] == ["2025-02-14", "2025-02-18"]
    assert sorted(unsettled) == ["2025-02-15", "2025-02-17"]


def test_withhold_incomplete_totals_does_not_let_a_closed_fund_blank_history():
    """A fund that closes and shows '-' for good is not a gap in every later
    day. On settled rows it never holds a Total back; near the newest edge it
    still does (there it looks exactly like a fund that has not reported), but
    only while it reported within the lookback."""
    rows = [[f"d{i:02d}", "1", "2", "3"] for i in range(3)]
    rows += [[f"d{i:02d}", "1", None, "1"] for i in range(3, 15)]
    withheld = fef.withhold_incomplete_totals(rows, 3, lookback=10, settling=5)

    assert withheld == ["d10", "d11", "d12"]      # edge rows within lookback
    assert all(r[3] == "1" for r in rows[3:10])   # settled history untouched


def test_withhold_incomplete_totals_blanks_a_settled_gap_in_a_live_fund():
    """A fund that reports before AND after a '-' day did not close; that day's
    Total is a sum without it, so it is withheld even deep in history."""
    rows = [[f"d{i:02d}", "1", "2", "3"] for i in range(12)]
    rows[4][2] = None
    withheld = fef.withhold_incomplete_totals(rows, 3, lookback=10, settling=5)
    assert withheld == ["d04"] and rows[4][3] is None


def _trading_days(start: date, n: int) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if not fef.market_closed(d.isoformat()):
            out.append(d)
        d += timedelta(days=1)
    return out


def _source_rows(days: list[date], override: dict | None = None) -> str:
    override = override or {}
    return "".join(
        _row(d.strftime("%d %b %Y"), *override.get(d.isoformat(), ("10", "20", "30", "60")))
        for d in days
    )


def test_refresh_drops_stored_zero_rows_the_source_does_not_back(tmp_path, monkeypatch):
    """History written by the old parser heals on the next run.

    * a stored all-zero row on a NYSE holiday goes (no session, no flow);
    * a stored all-zero row for a day the source lists with no reading goes;
    * a stored partial row is replaced by the source's complete one;
    * a real stored row the source no longer lists is kept.
    """
    days = _trading_days(date(2026, 1, 5), fef.MIN_ROWS + 10)
    unreported = days[20].isoformat()
    partial = days[-1].isoformat()
    csv_path = tmp_path / "btc_flows.csv"
    csv_path.write_text(
        "date,IBIT,FBTC,GBTC,Total\n"
        "2025-02-17,0,0,0,0\n"                 # Presidents' Day, as filed
        "2025-03-03,5,0,0,5\n"                 # real, no longer on the page
        f"{unreported},0,0,0,0\n"              # '-' row the old parser zeroed
        f"{partial},,20,,\n",                  # half published last run
        encoding="utf-8",
    )
    monkeypatch.setitem(fef.SOURCES, "btc", {
        "url": "https://example.invalid/btc", "csv": csv_path,
        "require": BTC_REQUIRE,
    })
    html = _farside_html(_source_rows(days, {unreported: ("-", "-", "-", "0.0")}))
    monkeypatch.setattr(fef, "fetch_html", lambda _u: html)

    assert fef.refresh("btc") == 0
    text = csv_path.read_text()
    assert "2025-02-17" not in text
    assert f"{unreported}," not in text
    assert "2025-03-03,5,0,0,5" in text
    assert f"{partial},10,20,30,60" in text


def test_refresh_refuses_when_settled_history_is_full_of_dashes(tmp_path, monkeypatch):
    """If Farside starts printing '-' for a settled zero, most historical
    Totals would be withheld. That is a convention change: go red, keep the
    file, do not blank the history."""
    days = _trading_days(date(2026, 1, 5), fef.MIN_ROWS + 10)
    flaky = {d.isoformat(): ("10", "-", "30", "40")
             for i, d in enumerate(days) if i % 2}
    csv_path = _seed(tmp_path, "btc_flows.csv", "2026-01-02")
    before = csv_path.read_text()
    monkeypatch.setitem(fef.SOURCES, "btc", {
        "url": "https://example.invalid/btc", "csv": csv_path,
        "require": BTC_REQUIRE,
    })
    monkeypatch.setattr(fef, "fetch_html",
                        lambda _u: _farside_html(_source_rows(days, flaky)))

    assert fef.refresh("btc") == 1
    assert csv_path.read_text() == before


@pytest.mark.parametrize("name", ["btc_flows.csv", "eth_flows.csv"])
def test_committed_csv_has_no_rows_on_market_closed_days(name):
    """The history fix: no zero-flow rows for days with no US session.

    2025-02-17, 2025-04-18, 2025-05-26, 2025-06-19 and twelve more NYSE
    holidays sat in btc_flows.csv as rows of zeros. Read the COMMITTED blob
    (CI stubs the working-tree CSVs)."""
    proc = subprocess.run(["git", "show", f"HEAD:data/{name}"],
                          cwd=REPO_ROOT, capture_output=True, text=True)
    if proc.returncode != 0:
        pytest.skip(f"git blob for data/{name} unavailable")
    rows = [l.split(",") for l in proc.stdout.strip().splitlines()[1:]]
    closed = [r[0] for r in rows if fef.market_closed(r[0])]
    assert closed == [], f"rows dated on weekends / NYSE holidays: {closed}"
