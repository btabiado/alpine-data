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


def test_refresh_refuses_when_settled_history_is_full_of_dashes(tmp_path, monkeypatch, capsys):
    """If Farside starts printing '-' for a settled zero, most historical
    Totals would be withheld. That is a convention change: go red, keep the
    file, do not blank the history. The annotation names the check and dates
    so the audit status feed is not only 'exit code 1'."""
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
    monkeypatch.setenv("GITHUB_ACTIONS", "true")

    assert fef.refresh("btc") == 1
    assert csv_path.read_text() == before
    line = next(ln for ln in capsys.readouterr().out.splitlines()
                if ln.startswith("::error "))
    assert line.startswith("::error title=ETF flows failed (btc)::")
    assert "settled-dash convention:" in line
    assert "showing '-'" in line
    # The first withheld date is in the message; a generic exit line is not.
    assert "2026-" in line
    assert "exit code" not in line


# Dates from the 2026-10-16 audit log (actions run 38953183213). Farside showed
# '-' on these settled rows; a cap of 5 then failed ETH and blocked new rows.
ETH_SETTLED_DASHES = (
    "2024-11-20", "2026-01-21", "2026-01-28",
    "2026-03-24", "2026-06-05", "2026-07-27",
)
BTC_SETTLED_DASHES = ("2024-11-20", "2026-07-27", "2026-08-07")
_MON = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()


def _fs_date(iso: str) -> str:
    """Farside's date cell. Not strftime: %b follows the process locale."""
    y, m, d = iso.split("-")
    return f"{int(d)} {_MON[int(m) - 1]} {y}"


def test_six_settled_eth_dashes_keep_committed_totals_and_still_append(
        tmp_path, monkeypatch, capsys):
    """Six settled '-' rows are a warning, not a failed asset.

    Totals already on disk stay. A settled '-' day that is not on disk yet is
    appended with an empty Total. A newer complete day is appended too. The
    run stays green: no ::error annotation.
    """
    days = _trading_days(date(2026, 1, 5), fef.MIN_ROWS + 10)
    kept_days = [days[i] for i in (10, 20, 30, 40, 50, 60)]
    new_dash = days[70]
    assert len(kept_days) == 6
    edge = set(days[-fef.SETTLING_ROWS:])
    assert edge.isdisjoint(kept_days) and new_dash not in edge

    csv_path = tmp_path / "eth_flows.csv"
    zero_fund_day = kept_days[0]
    seeded = ["date,ETHA,FETH,ETHE,Total"]
    for d in days:
        if d == new_dash:
            continue
        # One settled dash day stored FETH as 0. '-' there is absence, not a
        # flow, so that cell may become empty. The Total must not.
        if d == zero_fund_day:
            seeded.append(f"{d.isoformat()},10,0,30,60")
        else:
            seeded.append(f"{d.isoformat()},10,20,30,60")
    csv_path.write_text("\n".join(seeded) + "\n", encoding="utf-8")

    newer = _trading_days(days[-1] + timedelta(days=1), 1)[0]
    parts = []
    for d in days:
        if d == zero_fund_day:
            cells = ("-", "-", "30", "50")   # ETHA real, FETH was a stored 0
        elif d in kept_days or d == new_dash:
            cells = ("-", "20", "30", "50")
        else:
            cells = ("10", "20", "30", "60")
        parts.append(_row(_fs_date(d.isoformat()), *cells))
    parts.append(_row(_fs_date(newer.isoformat()), "10", "20", "30", "60"))
    monkeypatch.setitem(fef.SOURCES, "eth", {
        "url": "https://example.invalid/eth", "csv": csv_path,
        "require": ("ETHA", "FETH", "ETHE"),
        "first_trading_day": "2024-07-23",
    })
    _silence_manual(monkeypatch, tmp_path)
    monkeypatch.setattr(fef, "fetch_html", lambda _u: _farside_html(
        "".join(parts), funds="ETHA</th><th>FETH</th><th>ETHE"))
    monkeypatch.setenv("GITHUB_ACTIONS", "true")

    assert fef.refresh("eth") == 0
    text = csv_path.read_text()
    # Non-zero ETHA restored, stored FETH 0 becomes empty, Total unchanged.
    assert f"{zero_fund_day.isoformat()},10,,30,60" in text
    for d in kept_days[1:]:
        assert f"{d.isoformat()},10,20,30,60" in text
    # Not on disk before this run: appended, Total withheld, ETHA left blank.
    assert f"{new_dash.isoformat()},,20,30," in text
    assert f"{newer.isoformat()},10,20,30,60" in text
    captured = capsys.readouterr()
    assert "::error" not in captured.out
    assert "kept the committed Total" in captured.err
    assert "no Total to keep" in captured.err
    for d in kept_days:
        assert d.isoformat() in captured.err
    assert new_dash.isoformat() in captured.err


def test_october_2026_dash_counts_are_not_a_convention_change():
    """The cap of 5 was wrong for these tables. A large share still is."""
    cases = (("eth_flows.csv", 6), ("btc_flows.csv", 3))
    for name, n_dash in cases:
        proc = subprocess.run(["git", "show", f"HEAD:data/{name}"],
                              cwd=REPO_ROOT, capture_output=True, text=True)
        if proc.returncode != 0:
            pytest.skip(f"git blob for data/{name} unavailable")
        n = len(proc.stdout.splitlines()) - 1
        n_settled = n - fef.SETTLING_ROWS
        assert n_settled > 400
        assert not fef.settled_dashes_are_a_convention_change(n_dash, n_settled)
        assert fef.settled_dashes_are_a_convention_change(n_settled // 2, n_settled)


def _git_csv(name: str) -> str:
    proc = subprocess.run(["git", "show", f"HEAD:data/{name}"],
                          cwd=REPO_ROOT, capture_output=True, text=True)
    if proc.returncode != 0:
        pytest.skip(f"git blob for data/{name} unavailable")
    return proc.stdout


def _html_replaying_csv(text: str, dash_dates: set[str], dash_col: str,
                        extra: list[str]) -> str:
    """The committed table as a Farside page, with '-' forced on dash_dates."""
    import csv
    parsed = list(csv.reader(text.splitlines()))
    header = parsed[0]
    dash_i = header.index(dash_col) - 1
    body = []
    for r in parsed[1:]:
        cells = list(r[1:])
        while len(cells) < len(header) - 1:
            cells.append("")
        if r[0] in dash_dates:
            cells[dash_i] = "-"
            # Farside still prints a numeric Total on a partial day. An empty
            # Total in the CSV would otherwise skip the withhold path.
            if cells[-1].strip() == "":
                cells[-1] = "1"
        shown = ["-" if c.strip() == "" else c for c in cells]
        body.append(_row(_fs_date(r[0]), *shown))
    body.append(_row(_fs_date(extra[0]), *extra[1:]))
    funds = "</th><th>".join(header[1:-1])
    return _farside_html("".join(body), funds=funds)


def test_committed_csvs_keep_totals_when_the_audit_dates_show_dashes(
        tmp_path, monkeypatch, capsys):
    """Dry run against the committed ETH and BTC tables.

    Replays the 2026-10-16 failure: those settled rows show '-', and one new
    session is available. Existing rows, including their Totals, stay. The
    new session is appended. ETH does not fail.
    """
    import csv
    _silence_manual(monkeypatch, tmp_path)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setattr(fef, "fetch_json",
                        lambda _u: (_ for _ in ()).throw(AssertionError("no network")))

    def replay(asset: str, filename: str, dashes: tuple[str, ...], dash_col: str):
        original = _git_csv(filename)
        parsed = list(csv.reader(original.splitlines()))
        header, rows = parsed[0], {r[0]: r for r in parsed[1:] if r}
        for d in dashes:
            assert d in rows, d
        newest_iso = max(rows)
        nxt = date.fromisoformat(newest_iso) + timedelta(days=1)
        while fef.market_closed(nxt.isoformat()):
            nxt += timedelta(days=1)
        n_funds = len(header) - 2
        extra = [nxt.isoformat()] + ["1"] * n_funds + [str(n_funds)]
        csv_path = tmp_path / filename
        csv_path.write_text(original, encoding="utf-8")
        cfg = dict(fef.SOURCES[asset])
        cfg["csv"] = csv_path
        cfg["url"] = f"https://example.invalid/{asset}"
        monkeypatch.setitem(fef.SOURCES, asset, cfg)
        html = _html_replaying_csv(original, set(dashes), dash_col, extra)
        monkeypatch.setattr(fef, "fetch_html", lambda _u, html=html: html)
        assert fef.refresh(asset) == 0, asset
        after = {r[0]: r for r in csv.reader(csv_path.read_text().splitlines())
                 if r and r[0] != "date"}
        for d, row in rows.items():
            assert after[d] == row, (asset, d, after.get(d), row)
        assert extra[0] not in rows and after[extra[0]] == extra
        return newest_iso, extra[0]

    eth_newest, eth_new = replay("eth", "eth_flows.csv", ETH_SETTLED_DASHES, "ETHA")
    btc_newest, btc_new = replay("btc", "btc_flows.csv", BTC_SETTLED_DASHES, "IBIT")
    captured = capsys.readouterr()
    assert "::error" not in captured.out
    assert "kept the committed Total" in captured.err
    for d in ETH_SETTLED_DASHES:
        assert d in captured.err
    assert "no Total to keep" in captured.err
    for d in BTC_SETTLED_DASHES:
        assert d in captured.err
    assert eth_new not in ETH_SETTLED_DASHES
    assert btc_new not in BTC_SETTLED_DASHES
    assert eth_newest < eth_new and btc_newest < btc_new


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


# ---------- Farside HTTP 403: log it, fall back, do not evade ----------
#
# Daily audit 2026-10-15: etf-flows-daily had been failing on
# "Fetch BTC + ETH spot-ETF flows" and both CSVs ended 2026-10-05 because
# Farside answers GitHub runners with HTTP 403. The response is a warning
# and an untouched CSV when nothing else can fill the gap — not a crash,
# and not a second request with a different client.

import io
import json
from email.message import Message


def _http_error(url: str, code: int, reason: str = "Forbidden",
                headers: dict | None = None) -> "fef.urllib.error.HTTPError":
    msg = Message()
    for k, v in (headers or {}).items():
        msg[k] = v
    return fef.urllib.error.HTTPError(url, code, reason, msg, io.BytesIO(b""))


def _silence_manual(monkeypatch, tmp_path: Path) -> None:
    """Keep the committed override and the dispatch env out of a test."""
    monkeypatch.setattr(fef, "MANUAL_PATH", tmp_path / "no-manual.json")
    monkeypatch.setattr(fef, "MANUAL_EXTRA_TEXT", "")
    monkeypatch.delenv(fef.MANUAL_ENV, raising=False)


def test_http_403_logs_the_block_and_warns_instead_of_crashing(btc_cfg, monkeypatch, capsys, tmp_path):
    """The audit failure: 403 must not exit 1, must not write zeros, must say why."""
    _silence_manual(monkeypatch, tmp_path)
    before = btc_cfg.read_text()
    url = fef.SOURCES["btc"]["url"]

    def blocked(_url):
        raise _http_error(url, 403, headers={"cf-mitigated": "challenge"})
    monkeypatch.setattr(fef, "fetch_html", blocked)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")

    assert fef.refresh("btc") == 0
    assert btc_cfg.read_text() == before

    err = capsys.readouterr()
    assert "HTTP 403" in err.err
    assert url in err.err
    assert "Not retrying" in err.err
    assert "cf-mitigated=challenge" in err.err
    assert "2026-05-12" in err.err          # the seeded CSV's newest day
    assert "etf_flows_manual.json" in err.err
    warning = next(ln for ln in err.out.splitlines() if ln.startswith("::warning "))
    assert warning.startswith("::warning title=ETF flows not refreshed (btc)::")
    assert "HTTP 403" in warning
    assert "2026-05-12" in warning
    assert "User-Agent" not in warning or "Not retrying" in warning
    assert "::error" not in err.out


def test_non_403_fetch_error_still_fails_when_nothing_else_fills(btc_cfg, monkeypatch, tmp_path):
    """A timeout is not a block. With no backup and no override, the job stays red."""
    _silence_manual(monkeypatch, tmp_path)
    before = btc_cfg.read_text()

    def boom(_url):
        raise OSError("connection reset")
    monkeypatch.setattr(fef, "fetch_html", boom)

    assert fef.refresh("btc") == 1
    assert btc_cfg.read_text() == before


def test_http_error_annotation_names_asset_check_and_status(btc_cfg, monkeypatch, capsys, tmp_path):
    """A non-zero fetch must say which asset, which check, and the HTTP status."""
    _silence_manual(monkeypatch, tmp_path)
    before = btc_cfg.read_text()
    url = fef.SOURCES["btc"]["url"]
    monkeypatch.setattr(
        fef, "fetch_html",
        lambda _u: (_ for _ in ()).throw(_http_error(url, 500, "Internal Server Error")))
    monkeypatch.setenv("GITHUB_ACTIONS", "true")

    assert fef.refresh("btc") == 1
    assert btc_cfg.read_text() == before
    line = next(ln for ln in capsys.readouterr().out.splitlines()
                if ln.startswith("::error "))
    assert line.startswith("::error title=ETF flows failed (btc)::")
    assert "fetch:" in line
    assert "HTTP 500" in line
    assert url in line
    assert "exit code" not in line


def test_error_annotation_is_omitted_outside_actions(btc_cfg, monkeypatch, capsys, tmp_path):
    """Local runs keep the reason on stderr and do not emit a workflow command."""
    _silence_manual(monkeypatch, tmp_path)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(fef, "fetch_html", lambda _u: (_ for _ in ()).throw(OSError("connection reset")))

    assert fef.refresh("btc") == 1
    captured = capsys.readouterr()
    assert "::error" not in captured.out
    assert "fetch:" in captured.err
    assert "OSError" in captured.err
    assert "connection reset" in captured.err


def test_manual_json_fills_a_blocked_day_and_keeps_blanks_empty(btc_cfg, monkeypatch, tmp_path):
    """Override merges on a 403. '-' and a missing fund stay empty; 0 stays 0."""
    manual = tmp_path / "manual.json"
    manual.write_text(json.dumps({
        "btc": [
            {"date": "2026-05-13", "IBIT": "10", "FBTC": "0", "GBTC": "-", "Total": "10"},
            {"date": "2026-05-14", "IBIT": "-", "FBTC": "-", "GBTC": "-", "Total": "-"},
        ],
        "eth": [],
    }), encoding="utf-8")
    monkeypatch.setattr(fef, "MANUAL_PATH", manual)
    monkeypatch.setattr(fef, "MANUAL_EXTRA_TEXT", "")
    monkeypatch.delenv(fef.MANUAL_ENV, raising=False)
    monkeypatch.setattr(fef, "fetch_html",
                        lambda _u: (_ for _ in ()).throw(_http_error("https://farside.example/btc", 403)))

    assert fef.refresh("btc") == 0
    text = btc_cfg.read_text()
    assert "2026-05-13,10,0,," in text          # GBTC blank, Total withheld (IBIT was reporting)
    assert "2026-05-14" not in text             # no reading at all: not a row of zeros
    assert "2024-01-11,111.7,227.0,-95.1,243.6" in text


def test_manual_partial_day_does_not_publish_farsides_running_total(btc_cfg, monkeypatch, tmp_path):
    manual = tmp_path / "manual.json"
    manual.write_text(json.dumps({
        "btc": [{"date": "2026-05-13", "FBTC": "29.3", "Total": "31.7"}],
    }), encoding="utf-8")
    monkeypatch.setattr(fef, "MANUAL_PATH", manual)
    monkeypatch.setattr(fef, "MANUAL_EXTRA_TEXT", "")
    monkeypatch.delenv(fef.MANUAL_ENV, raising=False)
    monkeypatch.setattr(fef, "fetch_html",
                        lambda _u: (_ for _ in ()).throw(_http_error("https://farside.example/btc", 403)))

    assert fef.refresh("btc") == 0
    line = next(l for l in btc_cfg.read_text().splitlines() if l.startswith("2026-05-13"))
    cells = line.split(",")
    # date, IBIT, FBTC, GBTC, Total
    assert cells[1] == ""          # IBIT not reported, not 0
    assert cells[2] == "29.3"
    assert cells[4] == ""          # 31.7 was a partial sum


def test_dispatch_input_wins_over_the_committed_file(btc_cfg, monkeypatch, tmp_path):
    manual = tmp_path / "manual.json"
    manual.write_text(json.dumps({
        "btc": [{"date": "2026-05-13", "IBIT": "1", "FBTC": "1", "GBTC": "1", "Total": "3"}],
    }), encoding="utf-8")
    monkeypatch.setattr(fef, "MANUAL_PATH", manual)
    monkeypatch.setattr(fef, "MANUAL_EXTRA_TEXT", "")
    monkeypatch.setenv(fef.MANUAL_ENV, json.dumps({
        "btc": [{"date": "2026-05-13", "IBIT": "9", "FBTC": "9", "GBTC": "9", "Total": "27"}],
    }))
    monkeypatch.setattr(fef, "fetch_html",
                        lambda _u: (_ for _ in ()).throw(_http_error("https://farside.example/btc", 403)))

    assert fef.refresh("btc") == 0
    assert "2026-05-13,9,9,9,27" in btc_cfg.read_text()


def test_manual_csv_and_unknown_column_refuses_the_write(btc_cfg, monkeypatch, tmp_path):
    before = btc_cfg.read_text()
    monkeypatch.setattr(fef, "MANUAL_PATH", tmp_path / "absent.json")
    monkeypatch.setattr(fef, "MANUAL_EXTRA_TEXT", "")
    monkeypatch.setenv(fef.MANUAL_ENV,
                       "asset,date,IBIT,FBTC,GBTC,NOT_A_FUND,Total\n"
                       "btc,2026-05-13,1,2,3,4,10\n")
    monkeypatch.setattr(fef, "fetch_html",
                        lambda _u: (_ for _ in ()).throw(_http_error("https://farside.example/btc", 403)))

    assert fef.refresh("btc") == 1
    assert btc_cfg.read_text() == before


def test_farside_row_wins_over_a_manual_row_for_the_same_date(btc_cfg, monkeypatch, tmp_path):
    manual = tmp_path / "manual.json"
    manual.write_text(json.dumps({
        "btc": [
            {"date": "2026-06-15", "IBIT": "1", "FBTC": "1", "GBTC": "1", "Total": "3"},
            {"date": "2026-07-01", "IBIT": "4", "FBTC": "5", "GBTC": "6", "Total": "15"},
        ],
    }), encoding="utf-8")
    monkeypatch.setattr(fef, "MANUAL_PATH", manual)
    monkeypatch.setattr(fef, "MANUAL_EXTRA_TEXT", "")
    monkeypatch.delenv(fef.MANUAL_ENV, raising=False)
    rows = "".join(
        _row(f"{(d % 28) + 1:02d} Jun 2026", "10", "20", "30", "60")
        for d in range(fef.MIN_ROWS + 5)
    )
    monkeypatch.setattr(fef, "fetch_html", lambda _u: _farside_html(rows))

    assert fef.refresh("btc") == 0
    text = btc_cfg.read_text()
    assert "2026-06-15,10,20,30,60" in text     # the fetch, not the override
    assert "2026-07-01,4,5,6,15" in text        # a date the fetch did not have


def _tftc_days(n: int, start: date, tweak: dict | None = None) -> list[dict]:
    tweak = tweak or {}
    out, d = [], start
    while len(out) < n:
        if not fef.market_closed(d.isoformat()):
            per = {"IBIT": 1_000_000, "FBTC": 2_000_000, "GBTC": 3_000_000}
            net = 6_000_000
            if d.isoformat() in tweak:
                per, net = tweak[d.isoformat()]
            out.append({"date": d.isoformat(), "netFlowUsd": net, "perEtfUsd": per})
        d += timedelta(days=1)
    return out


def test_tftc_backup_gap_fills_and_does_not_rewrite_history(btc_cfg, monkeypatch, tmp_path):
    """CC BY backup fills holes after a 403. A stored reading is not replaced."""
    _silence_manual(monkeypatch, tmp_path)
    monkeypatch.setitem(fef.SOURCES["btc"], "backup", {
        "name": "TFTC",
        "url": "https://tftc.example/data.json",
        "license": "https://creativecommons.org/licenses/by/4.0/",
        "attribution": "TFTC — tftc.io/bitcoin-etf-flows",
    })
    # 2024-01-11 is in the seed with real numbers. The backup must not replace it.
    # One later day reports IBIT as a literal zero and omits FBTC.
    days = _tftc_days(fef.MIN_ROWS + 5, date(2024, 1, 11))
    zero_day = days[10]["date"]
    days[10] = {"date": zero_day, "netFlowUsd": 3_000_000,
                "perEtfUsd": {"IBIT": 0, "GBTC": 3_000_000}}
    payload = {
        "license": "https://creativecommons.org/licenses/by/4.0/",
        "units": "USD",
        "days": days,
    }
    monkeypatch.setattr(fef, "fetch_html",
                        lambda _u: (_ for _ in ()).throw(_http_error("https://farside.example/btc", 403)))
    monkeypatch.setattr(fef, "fetch_json", lambda _u: payload)

    assert fef.refresh("btc") == 0
    text = btc_cfg.read_text()
    assert "2024-01-11,111.7,227.0,-95.1,243.6" in text
    assert f"{zero_day},0,,3,3" in text or f"{zero_day},0,,3," in text
    # 1e6 + 3e6 = 4e6, but FBTC was reporting and is missing, so Total is withheld.
    zero_line = next(l for l in text.splitlines() if l.startswith(zero_day))
    assert zero_line.split(",")[1] == "0"       # a reported zero, not a blank
    assert zero_line.split(",")[2] == ""        # FBTC omitted by the source
    assert zero_line.endswith(",") or zero_line.split(",")[-1] == ""


def test_tftc_license_change_is_refused(btc_cfg, monkeypatch, tmp_path):
    _silence_manual(monkeypatch, tmp_path)
    before = btc_cfg.read_text()
    monkeypatch.setitem(fef.SOURCES["btc"], "backup", {
        "name": "TFTC",
        "url": "https://tftc.example/data.json",
        "license": "https://creativecommons.org/licenses/by/4.0/",
        "attribution": "TFTC — tftc.io/bitcoin-etf-flows",
    })
    monkeypatch.setattr(fef, "fetch_html",
                        lambda _u: (_ for _ in ()).throw(_http_error("https://farside.example/btc", 403)))
    monkeypatch.setattr(fef, "fetch_json", lambda _u: {
        "license": "https://example.invalid/all-rights-reserved",
        "units": "USD",
        "days": _tftc_days(fef.MIN_ROWS + 1, date(2024, 1, 11)),
    })

    assert fef.refresh("btc") == 1
    assert btc_cfg.read_text() == before


def test_backup_403_after_farside_403_warns(btc_cfg, monkeypatch, capsys, tmp_path):
    _silence_manual(monkeypatch, tmp_path)
    before = btc_cfg.read_text()
    monkeypatch.setitem(fef.SOURCES["btc"], "backup", {
        "name": "TFTC",
        "url": "https://tftc.example/data.json",
        "license": "https://creativecommons.org/licenses/by/4.0/",
        "attribution": "TFTC — tftc.io/bitcoin-etf-flows",
    })
    monkeypatch.setattr(fef, "fetch_html",
                        lambda _u: (_ for _ in ()).throw(_http_error("https://farside.example/btc", 403)))
    monkeypatch.setattr(fef, "fetch_json",
                        lambda _u: (_ for _ in ()).throw(
                            _http_error("https://tftc.example/data.json", 403)))
    monkeypatch.setenv("GITHUB_ACTIONS", "true")

    assert fef.refresh("btc") == 0
    assert btc_cfg.read_text() == before
    err = capsys.readouterr().err
    assert "https://farside.example/btc" in err or "HTTP 403" in err
    assert "https://tftc.example/data.json" in err
    assert err.count("HTTP 403") >= 2


# ---------- Farside HTTP 521 / timeouts: retry, then soft-warn ----------
#
# 2026-10-21: etf-flows-daily failed twice (runs 39588030660 and 39617299214)
# because Farside returned HTTP 521. ETH has no backup, so the job exited 1
# and the BTC rows TFTC had merged were never committed. A 5xx or a timeout
# is retried with backoff on the same User-Agent. If it still fails, the job
# warns and exits 0 while the CSV is under 2 trading days old.


class _HtmlResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _patch_urlopen(monkeypatch, fn):
    slept = []
    monkeypatch.setattr(fef, "_sleep", lambda s: slept.append(s))
    monkeypatch.setattr(fef.urllib.request, "urlopen", fn)
    return slept


def test_fetch_html_retries_521_with_backoff_and_the_same_user_agent(monkeypatch):
    calls = []

    def urlopen(req, timeout=0):
        calls.append((req.full_url, req.get_header("User-agent"), timeout))
        if len(calls) < 3:
            raise _http_error(req.full_url, 521, "Web Server Is Down")
        return _HtmlResponse(b"<html>ok</html>")

    slept = _patch_urlopen(monkeypatch, urlopen)
    assert fef.fetch_html("https://farside.co.uk/bitcoin-etf-flow-all-data/") == "<html>ok</html>"
    assert len(calls) == 3
    assert slept == [1.0, 2.0]
    assert {ua for _url, ua, _t in calls} == {fef.UA}
    assert {t for _url, _ua, t in calls} == {fef.FARSIDE_TIMEOUT_S}
    assert all("Mozilla" not in (ua or "") for _url, ua, _t in calls)


def test_fetch_html_does_not_retry_a_403(monkeypatch):
    calls = []

    def urlopen(req, timeout=0):
        calls.append(req.get_header("User-agent"))
        raise _http_error(req.full_url, 403, "Forbidden", headers={"cf-mitigated": "challenge"})

    slept = _patch_urlopen(monkeypatch, urlopen)
    with pytest.raises(fef.urllib.error.HTTPError) as ei:
        fef.fetch_html("https://farside.co.uk/bitcoin-etf-flow-all-data/")
    assert ei.value.code == 403
    assert calls == [fef.UA]
    assert slept == []


def test_fetch_html_retries_timeouts_then_raises(monkeypatch):
    calls = []

    def urlopen(req, timeout=0):
        calls.append(type(TimeoutError("The read operation timed out")))
        raise TimeoutError("The read operation timed out")

    slept = _patch_urlopen(monkeypatch, urlopen)
    with pytest.raises(TimeoutError):
        fef.fetch_html("https://farside.co.uk/ethereum-etf-flow-all-data/")
    assert len(calls) == fef.FARSIDE_ATTEMPTS
    assert slept == [1.0, 2.0, 4.0]


def test_fetch_html_does_not_retry_a_connection_reset(monkeypatch):
    calls = []

    def urlopen(req, timeout=0):
        calls.append(1)
        raise OSError("connection reset")

    slept = _patch_urlopen(monkeypatch, urlopen)
    with pytest.raises(OSError, match="connection reset"):
        fef.fetch_html("https://farside.co.uk/bitcoin-etf-flow-all-data/")
    assert calls == [1]
    assert slept == []


def test_soft_window_is_nyse_sessions_before_today():
    """2026-10-21 incident: Monday's row is one session behind, not two.

    Today is not a session Farside has published yet. 2026-07-03 is an NYSE
    holiday, so it is not a missed session either.
    """
    header = ["date", "IBIT", "Total"]

    def judge(day, today):
        rows = {day: [day, "1", "1"]}
        return fef.within_soft_window(header, rows, today=date.fromisoformat(today))

    ok, text = judge("2026-10-19", "2026-10-21")
    assert ok is True
    assert "less than 2 trading days old" in text
    assert "2026-10-20" in text
    ok, text = judge("2026-10-16", "2026-10-21")
    assert ok is False
    assert "at least 2 trading days old" in text
    assert judge("2026-10-20", "2026-10-21")[0] is True
    # Thursday 2026-07-02 through Monday 2026-07-06: Friday was a holiday.
    assert judge("2026-07-02", "2026-07-06")[0] is True
    # An empty Total does not make the file fresh.
    rows = {
        "2026-10-20": ["2026-10-20", "1", ""],
        "2026-10-16": ["2026-10-16", "1", "5"],
    }
    ok, text = fef.within_soft_window(header, rows, today=date(2026, 10, 21))
    assert ok is False
    assert "2026-10-16" in text


def _recent_csv(path: Path, last: str) -> None:
    path.write_text(
        "date,IBIT,FBTC,GBTC,Total\n"
        "2024-01-11,111.7,227.0,-95.1,243.6\n"
        f"{last},1,2,3,6\n",
        encoding="utf-8",
    )


def test_http_521_soft_warns_when_the_csv_is_under_two_trading_days(
        btc_cfg, monkeypatch, capsys, tmp_path):
    """The 2026-10-21 failure: Monday's CSV on Wednesday must not go red."""
    _silence_manual(monkeypatch, tmp_path)
    _recent_csv(btc_cfg, "2026-10-19")
    before = btc_cfg.read_text()
    url = fef.SOURCES["btc"]["url"]
    monkeypatch.setattr(fef, "_today", lambda: date(2026, 10, 21))
    monkeypatch.setattr(
        fef, "fetch_html",
        lambda _u: (_ for _ in ()).throw(_http_error(url, 521, "Web Server Is Down")))
    monkeypatch.setenv("GITHUB_ACTIONS", "true")

    assert fef.refresh("btc") == 0
    assert btc_cfg.read_text() == before
    captured = capsys.readouterr()
    assert "::error" not in captured.out
    warning = next(ln for ln in captured.out.splitlines() if ln.startswith("::warning "))
    assert warning.startswith("::warning title=ETF flows delayed (btc)::")
    assert warning.lower().count("soft warning") >= 1 or "soft warning:" in warning
    assert "HTTP 521" in warning
    assert "2026-10-19" in warning
    assert "less than 2 trading days old" in warning
    assert "Mozilla" not in warning


def test_http_521_fails_once_two_trading_days_are_already_missing(
        btc_cfg, monkeypatch, capsys, tmp_path):
    _silence_manual(monkeypatch, tmp_path)
    _recent_csv(btc_cfg, "2026-10-16")  # Friday; Mon 19 and Tue 20 are due by Wednesday
    before = btc_cfg.read_text()
    url = fef.SOURCES["btc"]["url"]
    monkeypatch.setattr(fef, "_today", lambda: date(2026, 10, 21))
    monkeypatch.setattr(
        fef, "fetch_html",
        lambda _u: (_ for _ in ()).throw(_http_error(url, 521, "Web Server Is Down")))
    monkeypatch.setenv("GITHUB_ACTIONS", "true")

    assert fef.refresh("btc") == 1
    assert btc_cfg.read_text() == before
    line = next(ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("::error "))
    assert line.startswith("::error title=ETF flows failed (btc)::")
    assert "fetch:" in line
    assert "HTTP 521" in line
    assert "at least 2 trading days old" in line
    assert "soft warning" not in line


def test_timeout_soft_warns_like_a_5xx(btc_cfg, monkeypatch, capsys, tmp_path):
    _silence_manual(monkeypatch, tmp_path)
    _recent_csv(btc_cfg, "2026-10-20")
    before = btc_cfg.read_text()
    monkeypatch.setattr(fef, "_today", lambda: date(2026, 10, 21))
    monkeypatch.setattr(
        fef, "fetch_html",
        lambda _u: (_ for _ in ()).throw(TimeoutError("The read operation timed out")))
    monkeypatch.setenv("GITHUB_ACTIONS", "true")

    assert fef.refresh("btc") == 0
    assert btc_cfg.read_text() == before
    out = capsys.readouterr().out
    assert "::error" not in out
    assert "::warning title=ETF flows delayed (btc)::" in out
    assert "soft warning:" in out


def test_http_404_still_fails_when_the_csv_is_fresh(btc_cfg, monkeypatch, capsys, tmp_path):
    """A 4xx other than the known 403 block is not a transient origin outage."""
    _silence_manual(monkeypatch, tmp_path)
    _recent_csv(btc_cfg, "2026-10-20")
    before = btc_cfg.read_text()
    url = fef.SOURCES["btc"]["url"]
    monkeypatch.setattr(fef, "_today", lambda: date(2026, 10, 21))
    monkeypatch.setattr(
        fef, "fetch_html",
        lambda _u: (_ for _ in ()).throw(_http_error(url, 404, "Not Found")))
    monkeypatch.setenv("GITHUB_ACTIONS", "true")

    assert fef.refresh("btc") == 1
    assert btc_cfg.read_text() == before
    line = next(ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("::error "))
    assert "HTTP 404" in line
    assert "ETF flows delayed" not in line


def test_521_still_gap_fills_from_the_backup_before_warning(btc_cfg, monkeypatch, tmp_path):
    """A dead Farside origin must not skip TFTC. A merged row exits 0 and writes."""
    _silence_manual(monkeypatch, tmp_path)
    monkeypatch.setitem(fef.SOURCES["btc"], "backup", {
        "name": "TFTC",
        "url": "https://tftc.example/data.json",
        "license": "https://creativecommons.org/licenses/by/4.0/",
        "attribution": "TFTC — tftc.io/bitcoin-etf-flows",
    })
    monkeypatch.setattr(fef, "_today", lambda: date(2026, 10, 21))
    days = _tftc_days(fef.MIN_ROWS + 5, date(2024, 1, 11))
    monkeypatch.setattr(
        fef, "fetch_html",
        lambda _u: (_ for _ in ()).throw(
            _http_error("https://farside.example/btc", 521, "Web Server Is Down")))
    monkeypatch.setattr(fef, "fetch_json", lambda _u: {
        "license": "https://creativecommons.org/licenses/by/4.0/",
        "units": "USD",
        "days": days,
    })

    assert fef.refresh("btc") == 0
    text = btc_cfg.read_text()
    assert days[1]["date"] in text          # a date the seed did not have
    assert "2024-01-11,111.7,227.0,-95.1,243.6" in text


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_usd_to_millions_rejects_non_finite(bad):
    """TFTC dollars must be a real number. NaN and +/-inf are not a flow."""
    with pytest.raises(ValueError, match="not a number"):
        fef.usd_to_millions(bad)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_manual_cell_rejects_non_finite(bad):
    """A manual override cell rejects NaN and +/-inf the same way."""
    with pytest.raises(ValueError, match="unsupported value"):
        fef._manual_cell(bad)


def test_finite_flows_still_convert():
    """The non-finite guard must not reject a real zero or a finite reading."""
    assert fef.usd_to_millions(None) is None
    assert fef.usd_to_millions(0) == "0"
    assert fef.usd_to_millions(1_500_000) == "1.5"
    assert fef.usd_to_millions(-2_000_000) == "-2"
    assert fef._manual_cell(None) is None
    assert fef._manual_cell(0) == "0"
    assert fef._manual_cell(0.0) == "0"
    assert fef._manual_cell(-3.5) == "-3.5"


def test_committed_manual_override_parses_and_workflow_wires_the_input():
    text = (REPO_ROOT / "data" / "etf_flows_manual.json").read_text(encoding="utf-8")
    parsed, err = fef.parse_manual_document(text, "data/etf_flows_manual.json")
    assert err is None
    assert parsed["btc"] == [] and parsed["eth"] == []
    workflow = (REPO_ROOT / ".github/workflows/etf-flows-daily.yml").read_text(encoding="utf-8")
    assert "manual_entry:" in workflow
    assert "ETF_FLOWS_MANUAL:" in workflow
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "data/etf_flows_manual.json" in readme
    assert "manual_entry" in readme
    assert "HTTP 403" in readme
    assert "never 0" in readme or "never `0`" in readme or "not a zero" in readme or "not reported" in readme
    assert "backoff" in workflow
    assert "2 trading days" in workflow
    assert "HTTP 521" in readme or "5xx" in readme
    assert "soft warning" in readme
    assert "proxy" in readme
