"""Tests for the CSV export route on server.py."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import app
import server


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    """Flask test client with DATA_DIR redirected to tmp_path.

    Mirrors the fixture in tests/test_server.py so the export route sees a
    real (but minimal) payload.
    """
    monkeypatch.setattr(app, "DATA_DIR", tmp_path)
    monkeypatch.setattr(server.dash, "DATA_DIR", tmp_path)

    # 5-row BTC ETF series — enough to exercise date filtering.
    (tmp_path / "btc_flows.csv").write_text(
        "date,IBIT,Total\n"
        "2024-01-11,100.0,100.0\n"
        "2024-01-12,-50.0,-50.0\n"
        "2024-01-13,25.0,25.0\n"
        "2024-01-14,75.0,75.0\n"
        "2024-01-15,-10.0,-10.0\n"
    )
    (tmp_path / "eth_flows.csv").write_text(
        "date,ETHA,Total\n2024-07-23,5.0,5.0\n"
    )
    (tmp_path / "market.json").write_text(json.dumps({"btc": {"price": []}, "eth": {"price": []}}))
    (tmp_path / "whale.json").write_text(json.dumps({"btc": {}}))

    server.flask_app.config["TESTING"] = True
    with server.flask_app.test_client() as c:
        yield c


def test_export_btc_daily_returns_csv(client):
    r = client.get("/api/export/csv?series=btc.daily")
    assert r.status_code == 200
    assert r.mimetype == "text/csv"
    cd = r.headers.get("Content-Disposition", "")
    assert "attachment" in cd
    assert "btc.daily_" in cd
    assert cd.endswith('.csv"')

    body = r.get_data(as_text=True)
    lines = body.strip().split("\n")
    assert lines[0] == "date,cumulative,flow"  # 'date' first, rest alphabetical
    # At least one data row
    assert len(lines) >= 2
    assert lines[1].startswith("2024-01-11,")


def test_export_rejects_unknown_series(client):
    r = client.get("/api/export/csv?series=does.not.exist")
    assert r.status_code == 400
    j = r.get_json()
    assert j["ok"] is False
    assert j["error"] == "series not in allowlist"


def test_export_rejects_missing_series_param(client):
    r = client.get("/api/export/csv")
    assert r.status_code == 400
    j = r.get_json()
    assert j["ok"] is False
    assert j["error"] == "series not in allowlist"


def test_export_date_filter(client):
    """Seeded payload has 5 BTC rows; ask for the middle 3."""
    r = client.get("/api/export/csv?series=btc.daily&from=2024-01-12&to=2024-01-14")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    lines = [ln for ln in body.strip().split("\n") if ln]
    # 1 header + 3 data rows
    assert len(lines) == 4
    dates = [ln.split(",")[0] for ln in lines[1:]]
    assert dates == ["2024-01-12", "2024-01-13", "2024-01-14"]


def test_export_empty_series_returns_header_only(client):
    """Unknown-but-allowlisted-shape series should still return a valid CSV."""
    # market.btc.price is allowlisted; seeded market.json has price=[]
    r = client.get("/api/export/csv?series=market.btc.price")
    assert r.status_code == 200
    assert r.mimetype == "text/csv"
    body = r.get_data(as_text=True)
    lines = [ln for ln in body.strip().split("\n") if ln]
    assert lines == ["date,value"]


def test_export_works_via_share_token(tmp_path: Path, monkeypatch):
    """With auth enabled, a valid share token should bypass Basic Auth for the
    export route (it's in `_SHARE_ALLOWED`)."""
    monkeypatch.setattr(app, "DATA_DIR", tmp_path)
    monkeypatch.setattr(server.dash, "DATA_DIR", tmp_path)
    (tmp_path / "btc_flows.csv").write_text(
        "date,IBIT,Total\n2024-01-11,100.0,100.0\n"
    )
    (tmp_path / "eth_flows.csv").write_text("date,ETHA,Total\n2024-07-23,5.0,5.0\n")
    (tmp_path / "market.json").write_text(json.dumps({"btc": {"price": []}, "eth": {"price": []}}))
    (tmp_path / "whale.json").write_text(json.dumps({"btc": {}}))

    # Enable auth + redirect the share store to tmp_path so we don't touch the
    # real data/shares.json.
    monkeypatch.setattr(server, "AUTH_ENABLED", True)
    monkeypatch.setattr(server, "DASH_USER", "tester")
    monkeypatch.setattr(server, "DASH_PASS", "hunter2")
    monkeypatch.setattr(server.shares, "DATA_DIR", tmp_path)
    monkeypatch.setattr(server.shares, "SHARES_PATH", tmp_path / "shares.json")

    entry = server.shares.create(days=1.0, label="test", created_by="tester")
    token = entry["token"]

    server.flask_app.config["TESTING"] = True
    with server.flask_app.test_client() as c:
        # Without auth or share token: should challenge.
        r = c.get("/api/export/csv?series=btc.daily")
        assert r.status_code == 401

        # With share token: succeeds.
        r = c.get(f"/api/export/csv?series=btc.daily&share={token}")
        assert r.status_code == 200
        assert r.mimetype == "text/csv"
        body = r.get_data(as_text=True)
        assert "2024-01-11" in body
