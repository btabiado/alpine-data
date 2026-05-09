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
