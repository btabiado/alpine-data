"""Tests for server.py — Flask routes, no real network calls."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import app
import server


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    """Flask test client with DATA_DIR redirected to a tmp_path."""
    # Redirect both module references to the same tmp dir so neither route
    # touches the real data/ directory.
    monkeypatch.setattr(app, "DATA_DIR", tmp_path)
    monkeypatch.setattr(server.dash, "DATA_DIR", tmp_path)

    # Seed minimal valid data so build_payload has something to render
    (tmp_path / "btc_flows.csv").write_text(
        "date,IBIT,Total\n"
        "2024-01-11,100.0,100.0\n"
        "2024-01-12,-50.0,-50.0\n"
    )
    (tmp_path / "eth_flows.csv").write_text(
        "date,ETHA,Total\n"
        "2024-07-23,5.0,5.0\n"
    )
    (tmp_path / "market.json").write_text(json.dumps({"btc": {"price": []}, "eth": {"price": []}}))
    (tmp_path / "whale.json").write_text(json.dumps({"btc": {}}))

    server.flask_app.config["TESTING"] = True
    with server.flask_app.test_client() as c:
        # CSRF mitigation in server.py requires this header on POST/DELETE.
        c.environ_base["HTTP_X_REQUESTED_WITH"] = "XMLHttpRequest"
        yield c


def test_index_returns_html(client):
    r = client.get("/")
    assert r.status_code == 200
    assert r.mimetype == "text/html"
    body = r.get_data(as_text=True)
    assert "<!doctype html>" in body
    # Payload was substituted into the HTML
    assert "__DATA_JSON__" not in body


def test_api_data_returns_json_with_expected_keys(client):
    r = client.get("/api/data")
    assert r.status_code == 200
    assert r.is_json
    payload = r.get_json()
    for k in ("btc", "eth", "market", "whale", "generated_at", "signals", "server"):
        assert k in payload
    # btc daily should have our two seeded rows
    assert len(payload["btc"]["daily"]) == 2


def test_healthz_returns_ok(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    j = r.get_json()
    assert j["ok"] is True
    assert "fetching" in j


def test_upload_csv_writes_btc_flows(client, tmp_path: Path):
    csv_text = "date,IBIT,Total\n2024-02-01,123.4,123.4\n2024-02-02,-10.0,-10.0\n"
    r = client.post(
        "/api/upload-csv?asset=btc",
        data=csv_text,
        content_type="text/csv",
    )
    assert r.status_code == 200
    j = r.get_json()
    assert j["ok"] is True
    assert j["rows"] == 2
    assert j["path"] == "btc_flows.csv"
    written = (tmp_path / "btc_flows.csv").read_text()
    assert "2024-02-01" in written and "123.4" in written


def test_upload_csv_writes_eth_flows(client, tmp_path: Path):
    csv_text = "date,ETHA,Total\n2024-07-23,5.0,5.0\n"
    r = client.post(
        "/api/upload-csv?asset=eth",
        data=csv_text,
        content_type="text/csv",
    )
    assert r.status_code == 200
    j = r.get_json()
    assert j["ok"] is True
    assert (tmp_path / "eth_flows.csv").exists()


def test_upload_csv_rejects_invalid_asset(client):
    r = client.post(
        "/api/upload-csv?asset=doge",
        data="date,Total\n2024-01-01,100\n",
        content_type="text/csv",
    )
    assert r.status_code == 400
    j = r.get_json()
    assert j["ok"] is False


def test_upload_csv_rejects_empty_body(client):
    r = client.post(
        "/api/upload-csv?asset=btc",
        data="",
        content_type="text/csv",
    )
    assert r.status_code == 400


def test_upload_csv_rejects_missing_date_header(client):
    r = client.post(
        "/api/upload-csv?asset=btc",
        data="foo,bar\n1,2\n",
        content_type="text/csv",
    )
    assert r.status_code == 400


def test_upload_csv_accepts_tab_separated(client, tmp_path: Path):
    csv_text = "date\tIBIT\tTotal\n2024-02-01\t123.4\t123.4\n"
    r = client.post(
        "/api/upload-csv?asset=btc",
        data=csv_text,
        content_type="text/csv",
    )
    assert r.status_code == 200
    written = (tmp_path / "btc_flows.csv").read_text()
    # Tabs should have been converted to commas
    assert "," in written
    assert "\t" not in written


def test_seed_etf_uses_mocked_fetch(client, tmp_path: Path, monkeypatch):
    """Mock fetch_btc_from_github_mirror — no real network call."""
    def fake_fetch(data_dir):
        (data_dir / "btc_flows.csv").write_text("date,Total\n2024-01-01,100.0\n")
        return 1

    monkeypatch.setattr(server.fetch_live, "fetch_btc_from_github_mirror", fake_fetch)
    r = client.post("/api/seed-etf")
    assert r.status_code == 200
    j = r.get_json()
    assert j["ok"] is True
    assert j["rows"] == 1


def test_seed_etf_returns_503_on_failure(client, monkeypatch):
    def fake_fetch(data_dir):
        raise RuntimeError("boom")
    monkeypatch.setattr(server.fetch_live, "fetch_btc_from_github_mirror", fake_fetch)
    r = client.post("/api/seed-etf")
    assert r.status_code == 503
    j = r.get_json()
    assert j["ok"] is False
    assert "boom" in j["error"]


def test_api_refresh_kicks_off_background_fetch(client, monkeypatch):
    """`/api/refresh` is async — spawns a thread for fetch_all and returns
    `{ok: true, in_progress: true}` immediately so Safari doesn't hit its
    fetch timeout on the ~60s real-world fetch."""
    monkeypatch.setattr(server.fetch_market, "fetch_all", lambda: None)
    r = client.post("/api/refresh")
    assert r.status_code == 200
    j = r.get_json()
    assert j["ok"] is True
    assert j["in_progress"] is True
    # No 'data' key on the immediate response — client polls /api/data for
    # the fresh payload once the background thread finishes.
    assert "data" not in j


def test_api_refresh_returns_in_progress_when_already_fetching(client, monkeypatch):
    """If a fetch is already mid-flight, /api/refresh should NOT queue a
    duplicate — it should just acknowledge that one is running."""
    monkeypatch.setattr(server.fetch_market, "fetch_all", lambda: None)
    # Pretend a fetch is in flight
    server._state["fetching"] = True
    try:
        r = client.post("/api/refresh")
        assert r.status_code == 200
        j = r.get_json()
        assert j["ok"] is True
        assert j["in_progress"] is True
        assert "already running" in j.get("status", "")
    finally:
        server._state["fetching"] = False
