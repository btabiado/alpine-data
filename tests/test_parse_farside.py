"""Tests for parse_farside.py — column-major Farside paste detection + parsing."""

import io
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import parse_farside as pf


VERTICAL_SAMPLE = textwrap.dedent("""\
Date,
IBIT
FBTC
BITB
Total
11 Jan 2024,
111.7
227.0
237.9
655.3
12 Jan 2024,
386.0
195.3
(484.1)
203.0
15 Jan 2024,
-
-
-
-
""")


def test_looks_like_vertical_farside_detects_sample():
    assert pf.looks_like_vertical_farside(VERTICAL_SAMPLE)


def test_looks_like_vertical_farside_rejects_wide_csv():
    wide = "date,IBIT,FBTC,Total\n2024-01-11,111.7,227.0,655.3\n"
    assert not pf.looks_like_vertical_farside(wide)


def test_looks_like_vertical_farside_rejects_empty():
    assert not pf.looks_like_vertical_farside("")


def test_parse_farside_vertical_produces_wide_csv():
    out = pf.parse_farside_vertical(VERTICAL_SAMPLE)
    lines = out.strip().splitlines()
    assert lines[0] == "date,IBIT,FBTC,BITB,Total"
    assert lines[1].startswith("2024-01-11,")


def test_parse_farside_vertical_handles_negatives_in_parens():
    out = pf.parse_farside_vertical(VERTICAL_SAMPLE)
    rows = [ln for ln in out.strip().splitlines()[1:]]
    # 2024-01-12 row: BITB cell was "(484.1)" → -484.1
    row = next(r for r in rows if r.startswith("2024-01-12"))
    parts = row.split(",")
    assert float(parts[3]) == pytest.approx(-484.1)


def test_parse_farside_vertical_does_not_write_an_all_dash_row():
    """15 Jan 2024 is all '-' (it was MLK Day): nothing was reported, so no
    row. It used to be written as a row of zeros, i.e. a day of zero flow."""
    out = pf.parse_farside_vertical(VERTICAL_SAMPLE)
    dates = [ln.split(",")[0] for ln in out.strip().splitlines()[1:]]
    assert dates == ["2024-01-11", "2024-01-12"]


PARTIAL_SAMPLE = textwrap.dedent("""\
Date,
IBIT
FBTC
BITB
Total
01 Oct 2026,
195.6
(60.7)
0.0
134.9
02 Oct 2026,
-
29.3
0.0
29.3
05 Oct 2026,
-
-
-
0.0
""")


def test_parse_farside_vertical_keeps_dashes_empty_and_withholds_partial_total():
    out = pf.parse_farside_vertical(PARTIAL_SAMPLE)
    lines = out.strip().splitlines()
    assert lines[0] == "date,IBIT,FBTC,BITB,Total"
    assert lines[1] == "2026-10-01,195.6,-60.7,0,134.9"   # a printed 0.0 stays 0
    # IBIT not in yet: empty, and Farside's 29.3 is not the day's total.
    assert lines[2] == "2026-10-02,,29.3,0,"
    # Nothing reported (Total is the formula's 0.0): no row at all.
    assert len(lines) == 3


def test_parse_value_rules():
    assert pf._parse_value("-") is None
    assert pf._parse_value("") is None
    assert pf._parse_value("n/a") is None
    assert pf._parse_value("0.0") == 0.0
    assert pf._parse_value("(123.4)") == pytest.approx(-123.4)
    assert pf._parse_value("1,234.5") == pytest.approx(1234.5)
    assert pf._parse_value("$50") == pytest.approx(50.0)
    assert pf._parse_value("garbage") is None


def test_parse_date_iso():
    assert pf._parse_date("11 Jan 2024,") == "2024-01-11"
    assert pf._parse_date("3 Mar 2025") == "2025-03-03"


def test_main_round_trip_via_stdin(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(VERTICAL_SAMPLE))
    rc = pf.main(["parse_farside.py"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "date,IBIT,FBTC,BITB,Total" in captured.out
    assert "2024-01-11" in captured.out


def test_main_rejects_non_vertical(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("date,IBIT\n2024-01-11,100"))
    rc = pf.main(["parse_farside.py"])
    assert rc == 1
