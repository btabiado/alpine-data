"""Tests for shares.py + server-side share routes + auth bypass."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import app
import server
import shares


@pytest.fixture
def isolated_shares(tmp_path: Path, monkeypatch):
    """Redirect shares.py to a tmp_path so tests can't see / clobber real shares."""
    p = tmp_path / "shares.json"
    monkeypatch.setattr(shares, "DATA_DIR", tmp_path)
    monkeypatch.setattr(shares, "SHARES_PATH", p)
    yield p


@pytest.fixture
def client(tmp_path: Path, monkeypatch, isolated_shares):
    """Flask client with DATA_DIR + shares dir redirected. Auth OFF by default."""
    monkeypatch.setattr(app, "DATA_DIR", tmp_path)
    monkeypatch.setattr(server.dash, "DATA_DIR", tmp_path)

    # Minimum seed data so build_payload doesn't choke.
    (tmp_path / "btc_flows.csv").write_text("date,IBIT,Total\n2024-01-11,100.0,100.0\n")
    (tmp_path / "eth_flows.csv").write_text("date,ETHA,Total\n2024-07-23,5.0,5.0\n")
    (tmp_path / "market.json").write_text(json.dumps({"btc": {"price": []}, "eth": {"price": []}}))
    (tmp_path / "whale.json").write_text(json.dumps({"btc": {}}))

    server.flask_app.config["TESTING"] = True
    with server.flask_app.test_client() as c:
        # CSRF mitigation in server.py requires this header on POST/DELETE.
        c.environ_base["HTTP_X_REQUESTED_WITH"] = "XMLHttpRequest"
        yield c


# ---------- shares.py module ----------

def test_create_returns_token_and_expiry(isolated_shares):
    entry = shares.create(days=3, label="hello")
    assert "token" in entry and len(entry["token"]) >= 24
    assert entry["label"] == "hello"
    exp = datetime.fromisoformat(entry["expires_at"])
    now = datetime.now(timezone.utc)
    delta = exp - now
    # Allow some slack for slow CI but assert "≈ 3 days from now".
    assert timedelta(days=2, hours=23) < delta < timedelta(days=3, minutes=1)


def test_is_valid_true_for_fresh_token(isolated_shares):
    entry = shares.create(days=1)
    assert shares.is_valid(entry["token"]) is True


def test_is_valid_false_for_unknown_token(isolated_shares):
    assert shares.is_valid("not-a-real-token") is False
    assert shares.is_valid("") is False


def test_is_valid_false_for_expired_token(isolated_shares):
    # Manually write a token with an expiry in the past.
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")
    isolated_shares.write_text(json.dumps({
        "stale-token": {
            "created_at": past,
            "expires_at": past,
            "label": "",
        }
    }))
    assert shares.is_valid("stale-token") is False


def test_revoke_removes_token(isolated_shares):
    entry = shares.create(days=1)
    assert shares.revoke(entry["token"]) is True
    assert shares.is_valid(entry["token"]) is False
    # Re-revoke is a no-op
    assert shares.revoke(entry["token"]) is False


def test_prune_expired_drops_only_expired(isolated_shares):
    # Mint one fresh + manually inject one expired.
    fresh = shares.create(days=1)
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
    data = json.loads(isolated_shares.read_text())
    data["expired-tok"] = {"created_at": past, "expires_at": past, "label": ""}
    isolated_shares.write_text(json.dumps(data))

    removed = shares.prune_expired()
    assert removed == 1
    assert shares.is_valid(fresh["token"]) is True
    assert shares.is_valid("expired-tok") is False


def test_list_all_excludes_expired_by_default(isolated_shares):
    fresh = shares.create(days=1)
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
    data = json.loads(isolated_shares.read_text())
    data["expired-tok"] = {"created_at": past, "expires_at": past, "label": "old"}
    isolated_shares.write_text(json.dumps(data))

    active = shares.list_all()
    tokens = {r["token"] for r in active}
    assert fresh["token"] in tokens
    assert "expired-tok" not in tokens

    full = shares.list_all(include_expired=True)
    tokens = {r["token"] for r in full}
    assert "expired-tok" in tokens


def test_create_rejects_non_positive_days(isolated_shares):
    with pytest.raises(ValueError):
        shares.create(days=0)
    with pytest.raises(ValueError):
        shares.create(days=-1)


# ---------- server.py: /share/<token> route ----------

def test_share_route_unknown_token_returns_410(client):
    r = client.get("/share/this-token-does-not-exist")
    assert r.status_code == 410
    assert "expired" in r.get_data(as_text=True).lower()


def test_share_route_valid_token_returns_dashboard(client, isolated_shares):
    entry = shares.create(days=1)
    r = client.get(f"/share/{entry['token']}")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "<!doctype html>" in body
    # SHARE_TOKEN was injected into the JS bundle.
    assert entry["token"] in body
    # Banner payload is in DATA.share — confirm the key was serialised.
    assert '"share"' in body
    # Templating placeholders fully replaced.
    assert "__SHARE_TOKEN__" not in body
    assert "__DATA_JSON__" not in body


# ---------- server.py: /api/share endpoints (auth disabled in test) ----------

def test_api_share_post_mints_token(client):
    r = client.post("/api/share", json={"days": 3, "label": "test"})
    assert r.status_code == 200
    j = r.get_json()
    assert j["ok"] is True
    assert "token" in j["share"]
    assert j["share"]["label"] == "test"


def test_api_share_post_rejects_zero_days(client):
    r = client.post("/api/share", json={"days": 0})
    assert r.status_code == 400


def test_api_share_post_rejects_huge_days(client):
    r = client.post("/api/share", json={"days": 365})
    assert r.status_code == 400


def test_api_share_get_lists_active(client, isolated_shares):
    shares.create(days=1, label="A")
    shares.create(days=2, label="B")
    r = client.get("/api/share")
    assert r.status_code == 200
    j = r.get_json()
    assert j["ok"] is True
    labels = {row["label"] for row in j["shares"]}
    assert labels == {"A", "B"}


def test_api_share_delete_revokes(client, isolated_shares):
    entry = shares.create(days=1)
    r = client.delete(f"/api/share/{entry['token']}")
    assert r.status_code == 200
    assert r.get_json()["removed"] is True
    assert shares.is_valid(entry["token"]) is False


# ---------- auth bypass: share tokens grant read-only access ----------

@pytest.fixture
def authed_client(tmp_path: Path, monkeypatch, isolated_shares):
    """Like `client` but with HTTP Basic Auth enabled so we can exercise the bypass."""
    monkeypatch.setattr(app, "DATA_DIR", tmp_path)
    monkeypatch.setattr(server.dash, "DATA_DIR", tmp_path)
    monkeypatch.setattr(server, "DASH_USER", "u")
    monkeypatch.setattr(server, "DASH_PASS", "p")
    monkeypatch.setattr(server, "AUTH_ENABLED", True)

    (tmp_path / "btc_flows.csv").write_text("date,Total\n2024-01-11,100.0\n")
    (tmp_path / "eth_flows.csv").write_text("date,Total\n2024-07-23,5.0\n")
    (tmp_path / "market.json").write_text(json.dumps({"btc": {"price": []}, "eth": {"price": []}}))
    (tmp_path / "whale.json").write_text(json.dumps({"btc": {}}))

    server.flask_app.config["TESTING"] = True
    with server.flask_app.test_client() as c:
        # CSRF mitigation in server.py requires this header on POST/DELETE.
        c.environ_base["HTTP_X_REQUESTED_WITH"] = "XMLHttpRequest"
        yield c


def test_authed_root_requires_auth(authed_client):
    r = authed_client.get("/")
    assert r.status_code == 401


def test_share_token_bypasses_auth_for_root_path(authed_client):
    entry = shares.create(days=1)
    r = authed_client.get(f"/share/{entry['token']}")
    assert r.status_code == 200


def test_share_token_bypasses_auth_for_api_data_via_query(authed_client):
    entry = shares.create(days=1)
    r = authed_client.get(f"/api/data?share={entry['token']}")
    assert r.status_code == 200
    j = r.get_json()
    assert "btc" in j


def test_share_token_rejected_for_mutating_routes(authed_client, monkeypatch):
    """Even with a valid share token, POSTs to refresh/upload/share-admin must
    fall through to Basic Auth and 401 without creds."""
    entry = shares.create(days=1)
    monkeypatch.setattr(server.fetch_market, "fetch_all", lambda: None)

    # /api/refresh isn't in _SHARE_ALLOWED → still demands auth
    r = authed_client.post(f"/api/refresh?share={entry['token']}")
    assert r.status_code == 401

    # /api/upload-csv isn't allowed either
    r = authed_client.post(
        f"/api/upload-csv?asset=btc&share={entry['token']}",
        data="date,Total\n2024-01-01,1.0\n",
        content_type="text/csv",
    )
    assert r.status_code == 401

    # /api/share (admin) — viewer cannot list / create
    r = authed_client.get(f"/api/share?share={entry['token']}")
    assert r.status_code == 401


def test_invalid_share_token_does_not_bypass_auth(authed_client):
    r = authed_client.get("/api/data?share=bogus-token")
    assert r.status_code == 401


def test_share_root_path_returns_410_for_invalid(authed_client):
    """Unknown share tokens should land on the expired page, not the 401
    auth prompt (better UX for the recipient of a stale link)."""
    r = authed_client.get("/share/totally-fake")
    assert r.status_code == 410
