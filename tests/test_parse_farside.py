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


def test_parse_farside_vertical_handles_dashes_as_zero():
    out = pf.parse_farside_vertical(VERTICAL_SAMPLE)
    rows = [ln for ln in out.strip().splitlines()[1:]]
    row = next(r for r in rows if r.startswith("2024-01-15"))
    parts = row.split(",")
    # all cells were "-" → 0
    assert all(float(p) == 0.0 for p in parts[1:])


def test_parse_value_rules():
    assert pf._parse_value("-") == 0.0
    assert pf._parse_value("") == 0.0
    assert pf._parse_value("(123.4)") == pytest.approx(-123.4)
    assert pf._parse_value("1,234.5") == pytest.approx(1234.5)
    assert pf._parse_value("$50") == pytest.approx(50.0)
    assert pf._parse_value("garbage") == 0.0


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
