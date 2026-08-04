"""Tests for server.py — Flask routes, no real network calls."""
from __future__ import annotations

import json
from datetime import datetime, timezone
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


# ---------------------------------------------------------------------------
# POST /api/seed-etf — the dashboard's "Seed BTC (mirror)" button
#
# The route used to answer every failure with "fetch failed; check server
# logs". Today's real answer is not a failure at all: the mirror
# (canadiancode/btc-etf-flows) returns HTTP 200 with 328 rows ending
# 2025-05-02, while the committed data/btc_flows.csv runs to 2026-05-12 with
# 12 per-fund columns the mirror does not carry — so the seed is refused on
# purpose. "Fetch failed" told the user to retry something that can never
# succeed.
#
# These tests pin the three outcomes apart: unreachable (transient),
# older-than-ours (permanent), wrong-shape (permanent).
# ---------------------------------------------------------------------------

# The mirror's real shape: 'Date,Total' header, compact quoted dates.
MIRROR_CSV = (
    'Date,Total\n'
    '"20240111T",655.3\n'
    '"20240112T",203.0\n'
    '"20250502T",-96.4\n'
)

# The real repo's shape, trimmed: per-fund columns, a year newer than the
# mirror can offer.
PER_FUND_NEWER = (
    "date,IBIT,FBTC,Total\n"
    "2026-05-11,-7.4,-3.6,27.2\n"
    "2026-05-12,0,-86.1,-115.2\n"
)


class _FakeResponse:
    def __init__(self, text: str, status_code: int = 200):
        self.text = text
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise server.requests.HTTPError(f"HTTP {self.status_code}")


def _serve_mirror(monkeypatch, text: str = MIRROR_CSV, status_code: int = 200):
    """Answer every mirror GET with `text`.

    `server` and `fetch_live` both did `import requests`, so they share one
    module object: patching `.get` here covers the route's probe AND the real
    writer, which lets the happy path run end-to-end without a network call.
    """
    monkeypatch.setattr(server.requests, "get",
                        lambda *a, **k: _FakeResponse(text, status_code))


def _explode_if_called(monkeypatch):
    """Make the writer fail loudly, to prove a refusal never reached it."""
    def _never(data_dir):  # pragma: no cover - reaching this IS the failure
        raise AssertionError("writer must not run on a refusal")
    monkeypatch.setattr(server.fetch_live, "fetch_btc_from_github_mirror", _never)


def test_seed_etf_uses_mocked_fetch(client, tmp_path: Path, monkeypatch):
    """Mock fetch_btc_from_github_mirror — no real network call."""
    # Total-only and older than the mirror, so the seed is actually allowed.
    (tmp_path / "btc_flows.csv").write_text("date,Total\n2023-12-31,1.0\n")
    _serve_mirror(monkeypatch)

    def fake_fetch(data_dir):
        (data_dir / "btc_flows.csv").write_text("date,Total\n2024-01-01,100.0\n")
        return 1

    monkeypatch.setattr(server.fetch_live, "fetch_btc_from_github_mirror", fake_fetch)
    r = client.post("/api/seed-etf")
    assert r.status_code == 200
    j = r.get_json()
    assert j["ok"] is True
    assert j["rows"] == 1
    assert j["outcome"] == "seeded"


def test_seed_etf_seeds_for_real_when_the_mirror_is_ahead(client, tmp_path: Path,
                                                          monkeypatch):
    """End-to-end through the real writer (HTTP mocked, nothing else)."""
    (tmp_path / "btc_flows.csv").write_text("date,Total\n2023-12-31,1.0\n")
    _serve_mirror(monkeypatch)

    r = client.post("/api/seed-etf")
    assert r.status_code == 200
    j = r.get_json()
    assert j["ok"] is True
    assert j["outcome"] == "seeded"
    assert j["rows"] == 3
    assert j["columns_written"] == ["date", "Total"]
    written = (tmp_path / "btc_flows.csv").read_text().splitlines()
    assert written[0] == "date,Total"
    assert written[-1] == "2025-05-02,-96.4"


def test_seed_etf_reports_an_unreachable_mirror_as_transient(client, tmp_path: Path,
                                                             monkeypatch, capsys):
    """No HTTP response = the one case where retrying is sensible, and the
    only case that still gets a 503."""
    _explode_if_called(monkeypatch)

    def boom(*a, **k):
        raise server.requests.ConnectionError("dns exploded at 10.0.0.1")
    monkeypatch.setattr(server.requests, "get", boom)

    before = (tmp_path / "btc_flows.csv").read_text()
    r = client.post("/api/seed-etf")

    assert r.status_code == 503
    assert r.headers.get("Retry-After") == "60"
    j = r.get_json()
    assert j["ok"] is False
    assert j["outcome"] == "unreachable"
    assert j["retryable"] is True
    assert j["wrote"] is None
    assert j["mirror"]["reachable"] is False
    assert "connection failed" in j["mirror"]["unreachable_reason"]
    assert "retrying is reasonable" in j["error"]
    # Nothing was touched, and the exception text never reaches the client
    # (CodeQL py/stack-trace-exposure) — it goes to stderr instead.
    assert (tmp_path / "btc_flows.csv").read_text() == before
    assert "10.0.0.1" not in r.get_data(as_text=True)
    assert "dns exploded" in capsys.readouterr().err


def test_seed_etf_reports_an_http_error_as_unreachable_with_the_status(
        client, monkeypatch):
    _explode_if_called(monkeypatch)
    _serve_mirror(monkeypatch, text="404: Not Found", status_code=404)

    r = client.post("/api/seed-etf")
    assert r.status_code == 503
    j = r.get_json()
    assert j["outcome"] == "unreachable"
    assert j["retryable"] is True
    assert j["mirror"]["http_status"] == 404
    assert "HTTP 404" in j["error"]


def test_seed_etf_refuses_an_older_mirror_and_says_so_with_both_dates(
        client, tmp_path: Path, monkeypatch):
    """The state of the world today: the mirror answers fine, and is a year
    behind. That is a permanent refusal, not a fetch failure."""
    (tmp_path / "btc_flows.csv").write_text(PER_FUND_NEWER)
    _serve_mirror(monkeypatch)
    _explode_if_called(monkeypatch)

    r = client.post("/api/seed-etf")

    assert r.status_code == 409          # conflict with what we hold, not 503
    assert "Retry-After" not in r.headers
    j = r.get_json()
    assert j["ok"] is False
    assert j["outcome"] == "refused_stale"
    assert j["retryable"] is False
    assert j["wrote"] is None
    # Both sides of the comparison are in the payload, so the claim is
    # checkable rather than assertive.
    assert j["mirror"]["reachable"] is True
    assert j["mirror"]["http_status"] == 200
    assert j["mirror"]["newest_date"] == "2025-05-02"
    assert j["mirror"]["rows"] == 3
    assert j["existing"]["newest_date"] == "2026-05-12"
    assert j["existing"]["rows"] == 2
    assert "2025-05-02" in j["error"] and "2026-05-12" in j["error"]
    # Stale leads, but the shape problem is disclosed too.
    assert j["blockers"] == ["stale", "shape"]
    assert j["columns_lost_if_seeded"] == ["IBIT", "FBTC"]
    # "retry forever" is exactly what the old message invited; say the opposite.
    assert "never help" in j["error"]
    assert ".github/workflows/etf-flows-daily.yml" in j["error"]
    assert (tmp_path / "btc_flows.csv").read_text() == PER_FUND_NEWER


def test_seed_etf_refuses_on_shape_even_when_the_mirror_is_ahead(
        client, tmp_path: Path, monkeypatch):
    """A newer mirror is still not a reason to flatten 13 columns into 2.
    Caught BEFORE the write, so the per-fund data is never destroyed."""
    older_per_fund = ("date,IBIT,FBTC,Total\n"
                      "2024-01-11,50.0,50.0,100.0\n")
    (tmp_path / "btc_flows.csv").write_text(older_per_fund)
    _serve_mirror(monkeypatch)
    _explode_if_called(monkeypatch)

    r = client.post("/api/seed-etf")

    assert r.status_code == 409
    j = r.get_json()
    assert j["outcome"] == "refused_shape"
    assert j["retryable"] is False
    assert j["blockers"] == ["shape"]
    assert j["columns_lost_if_seeded"] == ["IBIT", "FBTC"]
    assert j["mirror"]["writes_columns"] == ["date", "Total"]
    assert j["existing"]["columns"] == ["date", "IBIT", "FBTC", "Total"]
    assert "IBIT" in j["error"] and "never help" in j["error"]
    assert (tmp_path / "btc_flows.csv").read_text() == older_per_fund


def test_seed_etf_three_outcomes_are_told_apart(client, tmp_path: Path, monkeypatch):
    """The point of the change: one glance separates 'try again' from
    'this will never work'."""
    seen = {}

    # 1. unreachable
    def boom(*a, **k):
        raise server.requests.ConnectionError("down")
    monkeypatch.setattr(server.requests, "get", boom)
    seen["unreachable"] = client.post("/api/seed-etf").get_json()

    # 2. older than ours
    (tmp_path / "btc_flows.csv").write_text(PER_FUND_NEWER)
    _serve_mirror(monkeypatch)
    seen["stale"] = client.post("/api/seed-etf").get_json()

    # 3. wrong shape (mirror ahead, per-fund columns on disk)
    (tmp_path / "btc_flows.csv").write_text("date,IBIT,Total\n2024-01-11,1.0,1.0\n")
    seen["shape"] = client.post("/api/seed-etf").get_json()

    outcomes = {k: v["outcome"] for k, v in seen.items()}
    assert outcomes == {
        "unreachable": "unreachable",
        "stale": "refused_stale",
        "shape": "refused_shape",
    }
    # Only the network problem is worth retrying.
    assert [seen[k]["retryable"] for k in ("unreachable", "stale", "shape")] == \
        [True, False, False]
    # Three distinct explanations, none of them "fetch failed".
    messages = {v["error"] for v in seen.values()}
    assert len(messages) == 3
    assert not any("fetch failed" in m for m in messages)


def test_seed_etf_reports_absence_as_absence_not_zero(client, tmp_path: Path,
                                                      monkeypatch):
    """No file on disk is not 'a file with 0 rows dated never'."""
    (tmp_path / "btc_flows.csv").unlink()
    _serve_mirror(monkeypatch)

    r = client.post("/api/seed-etf")
    j = r.get_json()

    assert j["existing"]["exists"] is False
    assert j["existing"]["rows"] is None            # not 0
    assert j["existing"]["newest_date"] is None     # not today, not epoch
    assert j["existing"]["newest_date_age_days"] is None
    assert "nothing to regress" in j["existing"]["absent_reason"]
    # With nothing to protect, the seed is allowed to proceed.
    assert r.status_code == 200 and j["outcome"] == "seeded"


def test_seed_etf_dates_come_from_the_data_never_from_the_clock(
        client, tmp_path: Path, monkeypatch):
    """Report the age of the DATA. The clock read is disclosed separately as
    the instant the ages were computed against — it is never a data date."""
    (tmp_path / "btc_flows.csv").write_text(PER_FUND_NEWER)
    _serve_mirror(monkeypatch)

    j = client.post("/api/seed-etf").get_json()
    today = datetime.now(timezone.utc).date().isoformat()

    assert j["existing"]["newest_date"] == "2026-05-12"   # the CSV's own last row
    assert j["mirror"]["newest_date"] == "2025-05-02"     # the mirror's own last row
    assert today not in (j["existing"]["newest_date"], j["mirror"]["newest_date"])
    # Ages are arithmetic on those dates against a disclosed instant.
    assert j["age_computed_at"].startswith(today)
    for side in ("existing", "mirror"):
        expected = (datetime.now(timezone.utc)
                    - datetime.strptime(j[side]["newest_date"], "%Y-%m-%d")
                    .replace(tzinfo=timezone.utc)).days
        assert j[side]["newest_date_age_days"] == expected


def test_seed_etf_reports_an_unparsable_mirror_body(client, tmp_path: Path,
                                                    monkeypatch):
    """HTTP 200 is not the same as usable data."""
    _explode_if_called(monkeypatch)
    _serve_mirror(monkeypatch, text="Date,Total\ngarbage\n\"20240111T\",not-a-number\n")

    r = client.post("/api/seed-etf")
    assert r.status_code == 502
    j = r.get_json()
    assert j["outcome"] == "mirror_empty"
    assert j["retryable"] is False
    assert j["mirror"]["reachable"] is True
    assert j["mirror"]["rows"] == 0          # we read it: a real zero
    assert j["mirror"]["newest_date"] is None
    assert "no parsable rows" in j["error"]


def test_seed_etf_returns_503_on_failure(client, tmp_path: Path, monkeypatch, capsys):
    """Probe said seeding was safe and the write still blew up."""
    (tmp_path / "btc_flows.csv").write_text("date,Total\n2023-12-31,1.0\n")
    _serve_mirror(monkeypatch)

    def fake_fetch(data_dir):
        raise RuntimeError("boom")
    monkeypatch.setattr(server.fetch_live, "fetch_btc_from_github_mirror", fake_fetch)
    r = client.post("/api/seed-etf")
    assert r.status_code == 503
    j = r.get_json()
    assert j["ok"] is False
    assert j["outcome"] == "write_failed"
    assert j["retryable"] is True
    # Post-CodeQL stack-trace-exposure fix (commit 55ac864): the HTTP
    # response carries a generic message; the full traceback (including
    # "boom") is logged to stderr instead so a network-side attacker
    # can't pivot off internal exception text.
    assert "the write failed" in j["error"]
    assert "Check server logs" in j["error"]
    assert "boom" not in r.get_data(as_text=True)
    # The traceback should still be in stderr for debugging.
    captured = capsys.readouterr()
    assert "boom" in captured.err


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
