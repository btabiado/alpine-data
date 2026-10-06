"""Tests for app.py — CSV loading, totals, aggregation, streak, payload."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

import app


# ---------- load_csv ----------

def test_load_csv_with_total_column(tmp_path: Path):
    p = tmp_path / "btc_flows.csv"
    p.write_text(
        "date,IBIT,FBTC,Total\n"
        "2024-01-11,100.0,200.0,300.0\n"
        "2024-01-12,-50.0,-25.0,-75.0\n"
    )
    df = app.load_csv(p)
    assert not df.empty
    assert list(df.columns) == ["date", "IBIT", "FBTC", "Total"]
    assert pd.api.types.is_datetime64_any_dtype(df["date"])
    assert df["Total"].iloc[0] == 300.0
    assert df["Total"].iloc[1] == -75.0


def test_load_csv_handles_currency_and_parens(tmp_path: Path):
    p = tmp_path / "btc_flows.csv"
    p.write_text(
        "date,IBIT,Total\n"
        '2024-01-11,"$1,000.5","$1,000.5"\n'
        "2024-01-12,(50.0),(50.0)\n"
    )
    df = app.load_csv(p)
    assert df["Total"].iloc[0] == 1000.5
    assert df["Total"].iloc[1] == -50.0


def test_load_csv_missing_file_returns_empty(tmp_path: Path):
    df = app.load_csv(tmp_path / "does_not_exist.csv")
    assert df.empty


def test_load_csv_renames_capitalized_date(tmp_path: Path):
    p = tmp_path / "btc_flows.csv"
    p.write_text("Date,Total\n2024-01-11,100.0\n")
    df = app.load_csv(p)
    assert "date" in df.columns


# ---------- ensure_total ----------

def test_ensure_total_passthrough_when_present(tmp_path: Path):
    df = pd.DataFrame({
        "date": pd.to_datetime(["2024-01-11", "2024-01-12"]),
        "IBIT": [100.0, -50.0],
        "Total": [300.0, -75.0],
    })
    out = app.ensure_total(df)
    assert "Total" in out.columns
    assert out["Total"].tolist() == [300.0, -75.0]


def test_ensure_total_computes_when_missing():
    df = pd.DataFrame({
        "date": pd.to_datetime(["2024-01-11", "2024-01-12"]),
        "IBIT": [100.0, -50.0],
        "FBTC": [200.0, -25.0],
    })
    out = app.ensure_total(df)
    assert "Total" in out.columns
    assert out["Total"].tolist() == [300.0, -75.0]


def test_ensure_total_renames_lowercase_total():
    df = pd.DataFrame({
        "date": pd.to_datetime(["2024-01-11"]),
        "IBIT": [100.0],
        "total": [100.0],
    })
    out = app.ensure_total(df)
    assert "Total" in out.columns
    assert "total" not in out.columns


def test_ensure_total_empty_passthrough():
    out = app.ensure_total(pd.DataFrame())
    assert out.empty


# ---------- aggregate ----------

def test_aggregate_empty_returns_empty_structure():
    out = app.aggregate(pd.DataFrame())
    for k in ("daily", "weekly", "monthly", "yearly", "cumulative", "by_fund"):
        assert k in out
    assert out["daily"] == []
    assert out["last_date"] is None


def test_aggregate_buckets_and_cumulative():
    dates = pd.date_range("2024-01-01", periods=400, freq="D")
    df = pd.DataFrame({
        "date": dates,
        "IBIT": [10.0] * 400,
        "FBTC": [-5.0] * 400,
        "Total": [5.0] * 400,
    })
    out = app.aggregate(df)

    assert len(out["daily"]) == 400
    # Cumulative grows monotonically with constant +5 flow
    assert out["daily"][-1]["cumulative"] == pytest.approx(5.0 * 400)
    # Weekly / monthly / yearly buckets exist and are non-empty
    assert len(out["weekly"]) > 0
    assert len(out["monthly"]) >= 12
    assert len(out["yearly"]) >= 1
    # Cumulative on the last bucket should equal the running sum of all flows
    assert out["yearly"][-1]["cumulative"] == pytest.approx(5.0 * 400)
    # by_fund has both numeric columns
    funds = {f["fund"] for f in out["by_fund"]}
    assert "IBIT" in funds and "FBTC" in funds
    # Stats reflect the data
    assert out["stats"]["all_time"] == pytest.approx(5.0 * 400)
    assert out["stats"]["last_30d"] == pytest.approx(5.0 * 30)
    assert out["last_date"] == dates[-1].strftime("%Y-%m-%d")


def test_aggregate_leaves_a_partly_reported_day_out_and_marks_it_pending(tmp_path: Path):
    """An empty cell is "not reported yet", never 0.

    The CSV's newest row is half published: IBIT in, FBTC still empty, so the
    Total is empty. It must not become today's flow, a zero in the charts, a
    break in the streak, or a drag on 7d/30d/all-time.
    """
    from datetime import datetime, timezone
    p = tmp_path / "btc_flows.csv"
    p.write_text(
        "date,IBIT,FBTC,MSBT,Total\n"
        "2026-09-30,10.0,5.0,,15.0\n"       # MSBT not launched yet
        "2026-10-01,20.0,0.0,,20.0\n"
        "2026-10-02,30.0,-5.0,1.0,26.0\n"
        "2026-10-05,40.0,,,\n"              # partly published
    )
    df = app.ensure_total(app.load_csv(p))
    out = app.aggregate(df, now=datetime(2026, 10, 6, 15, 0, tzinfo=timezone.utc))

    assert [r["date"] for r in out["daily"]] == ["2026-09-30", "2026-10-01", "2026-10-02"]
    assert all(r["flow"] != 0 for r in out["daily"])
    st = out["stats"]
    assert st["last_date"] == "2026-10-02" and st["last_day_flow"] == 26.0
    assert st["all_time"] == pytest.approx(61.0)
    assert st["streak"] == {"direction": "up", "length": 3}
    assert out["last_date"] == "2026-10-02"
    # 10-05 partly in; 10-06 (session open at 11:00 ET) not reported at all.
    assert st["pending"] == [
        {"date": "2026-10-05", "status": "partial", "reported": ["IBIT"]},
        {"date": "2026-10-06", "status": "unpublished"},
    ]
    # Per fund: a missing cell is None, not 0, and is not someone's last flow.
    fbtc = {r["date"]: r for r in out["by_fund_daily"]["FBTC"]}
    assert fbtc["2026-10-05"]["flow"] is None
    assert fbtc["2026-10-05"]["cumulative"] == pytest.approx(0.0)
    assert fbtc["2026-10-01"]["flow"] == 0.0           # a reported zero stays
    msbt = {r["date"]: r for r in out["by_fund_daily"]["MSBT"]}
    assert msbt["2026-09-30"]["flow"] is None
    by = {f["fund"]: f for f in out["by_fund"]}
    assert by["FBTC"]["last_date"] == "2026-10-02" and by["FBTC"]["last_flow"] == -5.0
    assert by["IBIT"]["total"] == pytest.approx(100.0)  # its 10-05 reading counts
    json.dumps(out, allow_nan=False)                      # no NaN leaks into JSON


def test_aggregate_shows_no_pending_before_the_session_opens():
    from datetime import datetime, timezone
    df = app.ensure_total(pd.DataFrame({
        "date": pd.to_datetime(["2026-10-02", "2026-10-05"]),
        "Total": [1.0, 2.0],
    }))
    # 08:00 ET on Tue 2026-10-06: Monday is in, Tuesday has not opened.
    early = app.aggregate(df, now=datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc))
    assert early["stats"]["pending"] == []
    # A long gap is a stale feed, not "pending": the freshness stamp says so.
    late = app.aggregate(df, now=datetime(2026, 11, 30, 18, 0, tzinfo=timezone.utc))
    assert late["stats"]["pending"] == []


def test_ensure_total_does_not_sum_around_a_missing_fund():
    """No Total column: a row where a reporting fund is empty gets no Total,
    but a fund that has not launched yet does not hold the sum back."""
    df = pd.DataFrame({
        "date": pd.to_datetime(["2026-10-01", "2026-10-02", "2026-10-05"]),
        "IBIT": [100.0, 50.0, 70.0],
        "FBTC": [20.0, 10.0, float("nan")],     # FBTC not in yet on 10-05
        "MSBT": [float("nan"), float("nan"), 3.0],  # launches on 10-05
    })
    out = app.ensure_total(df)
    assert out["Total"].iloc[0] == 120.0 and out["Total"].iloc[1] == 60.0
    assert pd.isna(out["Total"].iloc[2])


# ---------- streak_calc ----------

def test_streak_empty():
    assert app.streak_calc([]) == {"direction": "flat", "length": 0}


def test_streak_up_run():
    out = app.streak_calc([1.0, 2.0, 3.0])
    assert out["direction"] == "up"
    assert out["length"] == 3


def test_streak_down_run_breaks_on_positive():
    out = app.streak_calc([5.0, -1.0, -2.0, -3.0])
    assert out["direction"] == "down"
    assert out["length"] == 3


def test_streak_flat_last_value():
    out = app.streak_calc([1.0, -1.0, 0.0])
    assert out["direction"] == "flat"
    assert out["length"] == 0


def test_streak_breaks_on_zero():
    # current direction is up (last value 1.0); a zero in the middle breaks the streak
    out = app.streak_calc([1.0, 1.0, 0.0, 1.0, 1.0])
    assert out["direction"] == "up"
    assert out["length"] == 2


# ---------- build_payload ----------

def test_build_payload_returns_expected_keys(tmp_path: Path, monkeypatch):
    # Redirect DATA_DIR so we never read real production CSVs
    monkeypatch.setattr(app, "DATA_DIR", tmp_path)

    btc = tmp_path / "btc_flows.csv"
    btc.write_text(
        "date,IBIT,FBTC,Total\n"
        "2024-01-11,100.0,200.0,300.0\n"
        "2024-01-12,-50.0,-25.0,-75.0\n"
        "2024-01-13,30.0,40.0,70.0\n"
    )
    eth = tmp_path / "eth_flows.csv"
    eth.write_text(
        "date,ETHA,Total\n"
        "2024-07-23,5.0,5.0\n"
        "2024-07-24,-2.0,-2.0\n"
    )
    (tmp_path / "market.json").write_text(json.dumps({"btc": {"price": []}, "eth": {"price": []}}))
    (tmp_path / "whale.json").write_text(json.dumps({"btc": {"tx_volume_usd": []}}))

    payload = app.build_payload()

    for k in ("btc", "eth", "market", "whale", "generated_at", "signals"):
        assert k in payload
    assert len(payload["btc"]["daily"]) == 3
    assert len(payload["eth"]["daily"]) == 2
    # signals.compute_all returns dict with btc/eth (None when too little data)
    assert "btc" in payload["signals"] and "eth" in payload["signals"]


def test_render_html_substitutes_payload():
    payload = {"btc": {}, "eth": {}, "generated_at": "2024-01-11T00:00:00"}
    html = app.render_html(payload)
    assert "<!doctype html>" in html
    assert "__DATA_JSON__" not in html
    assert "generated_at" in html


# ---------- data-mufon.json: the committed frozen NUFORC cache ----------
#
# V1 used to copy v2/data-mufon.json (rebuilt hourly by the V2 build, which
# also tried to scrape nuforc.org) over the committed root file. V2 is retired
# and nothing scrapes NUFORC: the committed file IS the published sidecar.

def test_mufon_sidecar_is_the_committed_frozen_cache():
    src = Path(app.__file__).read_text(encoding="utf-8")
    assert 'manifest["mufon"] = "data-mufon.json"' in src
    assert '"v2"' not in src and "fetch_mufon" not in src.split("HTML_TEMPLATE")[0]
    mu = json.loads((Path(app.__file__).parent / "data-mufon.json").read_text())
    # It says what it is: frozen, through 2026-06-09, not refreshed.
    assert mu["data_through"] == mu["date_range"][1] == "2026-06-09"
    assert mu["_stale"] is True and mu["live_refresh"]["ok"] is False
    assert mu["total_records"] > 100_000
