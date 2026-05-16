"""Tests for the per-tab sidecar split.

dashboard.html used to inline every payload subtree, including ~736KB of
whale data the user pays for even when they never open the Whale tab. The
build now extracts whale (and any other key in ``app.SIDECAR_KEYS``) into a
separate ``data-<key>.json`` file the client fetches lazily on tab-select.

These tests guard:
  1. ``split_payload_for_sidecars`` partitions the payload correctly,
     skips empty values, and produces the right manifest.
  2. ``render_html`` substitutes the ``__SIDECARS_JSON__`` placeholder.
  3. ``app.py main()`` writes the sidecar files next to dashboard.html.
  4. ``/`` serves the trimmed payload + manifest; whale is NOT inlined.
  5. ``/data-<key>.json`` serves the sidecar payload; unknown keys 404.
  6. Share-token holders can fetch sidecars (read-only allowlist).
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

import app
import server
import shares


ROOT = Path(__file__).resolve().parent.parent


# ---------- 1. split helper ----------


def test_split_payload_for_sidecars_extracts_whale():
    payload = {
        "btc": {"daily": []},
        "eth": {"daily": []},
        "whale": {"btc": {"tx_volume_usd": [1.0]}},
        "generated_at": "2026-05-15T00:00:00",
    }
    trimmed, sidecars, manifest = app.split_payload_for_sidecars(payload, keys=("whale",))
    assert "whale" not in trimmed
    assert sidecars["whale"] == {"btc": {"tx_volume_usd": [1.0]}}
    assert manifest == {"whale": "data-whale.json"}
    # Other keys are untouched.
    assert trimmed["btc"] == {"daily": []}
    assert trimmed["generated_at"] == "2026-05-15T00:00:00"


def test_split_payload_for_sidecars_skips_empty_values():
    payload = {"whale": {}, "btc": {"daily": []}}
    trimmed, sidecars, manifest = app.split_payload_for_sidecars(payload, keys=("whale",))
    # Empty dict -> not extracted (manifest entry would point at an empty file).
    assert "whale" in trimmed
    assert sidecars == {}
    assert manifest == {}


def test_split_payload_for_sidecars_skips_missing_keys():
    payload = {"btc": {"daily": []}}
    trimmed, sidecars, manifest = app.split_payload_for_sidecars(payload, keys=("whale",))
    assert trimmed == payload
    assert sidecars == {}
    assert manifest == {}


def test_split_payload_for_sidecars_does_not_mutate_input():
    payload = {"whale": {"btc": {}}, "btc": {"daily": []}}
    snapshot = json.dumps(payload, sort_keys=True)
    app.split_payload_for_sidecars(payload, keys=("whale",))
    assert json.dumps(payload, sort_keys=True) == snapshot


# ---------- 2. render_html ----------


def test_render_html_substitutes_sidecars_manifest():
    payload = {"btc": {}, "eth": {}, "generated_at": "2024-01-11T00:00:00"}
    manifest = {"whale": "data-whale.json"}
    html = app.render_html(payload, sidecars_manifest=manifest)
    assert "__SIDECARS_JSON__" not in html
    # Manifest appears in the JS as `const SIDECARS = {...};`
    assert '"whale"' in html and '"data-whale.json"' in html


def test_render_html_default_sidecars_manifest_is_empty_object():
    payload = {"btc": {}, "eth": {}, "generated_at": "2024-01-11T00:00:00"}
    html = app.render_html(payload)
    assert "__SIDECARS_JSON__" not in html
    # No manifest passed -> SIDECARS is `{}`.
    m = re.search(r"const SIDECARS = (\{[^;]*\});", html)
    assert m, "SIDECARS const not found in rendered HTML"
    assert m.group(1) == "{}"


# ---------- 3. app.py main() writes sidecars ----------


def test_main_writes_sidecar_files(tmp_path: Path, monkeypatch):
    """``python app.py --no-open`` should produce dashboard.html AND
    data-whale.json side-by-side, with the whale data extracted out of the
    inlined payload."""
    # Redirect DATA_DIR and OUT so we never clobber the real artefacts.
    monkeypatch.setattr(app, "DATA_DIR", tmp_path)
    out_path = tmp_path / "dashboard.html"
    monkeypatch.setattr(app, "OUT", out_path)
    monkeypatch.setattr(app, "ROOT", tmp_path)

    (tmp_path / "btc_flows.csv").write_text("date,Total\n2024-01-11,100.0\n")
    (tmp_path / "eth_flows.csv").write_text("date,Total\n2024-07-23,5.0\n")
    (tmp_path / "market.json").write_text(json.dumps({"btc": {"price": []}, "eth": {"price": []}}))
    (tmp_path / "whale.json").write_text(json.dumps({"btc": {"tx_volume_usd": [1.0, 2.0, 3.0]}}))

    # In-process call (no subprocess) so we share the monkeypatched DATA_DIR/OUT.
    # `--no-open` skips webbrowser.open and `--fetch-market` is omitted so no
    # network calls — we use the seeded JSON above as the live data.
    monkeypatch.setattr(sys, "argv", ["app.py", "--no-open"])
    exit_code = app.main()
    assert exit_code == 0

    assert out_path.exists()
    sidecar_path = tmp_path / "data-whale.json"
    assert sidecar_path.exists(), "data-whale.json missing after build"

    html = out_path.read_text()
    # Whale subtree should NOT be inlined in the HTML.
    assert '"tx_volume_usd"' not in html, "whale data leaked into inlined HTML"
    # The manifest must be present so the client knows where to fetch it.
    assert '"whale"' in html and '"data-whale.json"' in html

    sidecar = json.loads(sidecar_path.read_text())
    assert sidecar["btc"]["tx_volume_usd"] == [1.0, 2.0, 3.0]


# ---------- 4. server.py: / serves trimmed payload + manifest ----------


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(app, "DATA_DIR", tmp_path)
    monkeypatch.setattr(server.dash, "DATA_DIR", tmp_path)
    (tmp_path / "btc_flows.csv").write_text("date,IBIT,Total\n2024-01-11,100.0,100.0\n")
    (tmp_path / "eth_flows.csv").write_text("date,ETHA,Total\n2024-07-23,5.0,5.0\n")
    (tmp_path / "market.json").write_text(json.dumps({"btc": {"price": []}, "eth": {"price": []}}))
    # Distinctive sentinel value so the "not in body" assertion can't false-positive.
    (tmp_path / "whale.json").write_text(json.dumps({
        "btc": {"tx_volume_usd": [123456789.0]},
    }))
    server.flask_app.config["TESTING"] = True
    with server.flask_app.test_client() as c:
        c.environ_base["HTTP_X_REQUESTED_WITH"] = "XMLHttpRequest"
        yield c


def test_index_strips_whale_and_emits_sidecar_manifest(client):
    r = client.get("/")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    # The seeded whale value must not appear in the inlined HTML — it now
    # lives only in /data-whale.json. (We can't just grep "tx_volume_usd"
    # because that string appears in the renderer JS that READS the field.)
    assert "123456789" not in body
    # SIDECARS manifest points at the whale endpoint so the client knows
    # where to fetch the data lazily on tab-select.
    assert '"whale"' in body and '"data-whale.json"' in body


def test_sidecar_route_returns_whale_subtree(client):
    r = client.get("/data-whale.json")
    assert r.status_code == 200
    assert r.is_json
    j = r.get_json()
    assert j["btc"]["tx_volume_usd"] == [123456789.0]


def test_sidecar_route_404s_for_unknown_key(client):
    r = client.get("/data-bogus.json")
    assert r.status_code == 404


def test_sidecar_route_404s_for_non_sidecar_payload_key(client):
    # `market` is a real payload key but NOT a registered sidecar — the
    # allowlist must block it so callers can't pull arbitrary subtrees.
    assert "market" not in app.SIDECAR_KEYS
    r = client.get("/data-market.json")
    assert r.status_code == 404


# ---------- 5. share-token access ----------


@pytest.fixture
def shared_client(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(app, "DATA_DIR", tmp_path)
    monkeypatch.setattr(server.dash, "DATA_DIR", tmp_path)
    monkeypatch.setattr(shares, "DATA_DIR", tmp_path)
    monkeypatch.setattr(shares, "SHARES_PATH", tmp_path / "shares.json")
    # Turn auth ON so we can prove the share token bypasses the prompt.
    monkeypatch.setattr(server, "DASH_USER", "u")
    monkeypatch.setattr(server, "DASH_PASS", "p")
    monkeypatch.setattr(server, "AUTH_ENABLED", True)

    (tmp_path / "btc_flows.csv").write_text("date,Total\n2024-01-11,100.0\n")
    (tmp_path / "eth_flows.csv").write_text("date,Total\n2024-07-23,5.0\n")
    (tmp_path / "market.json").write_text(json.dumps({"btc": {"price": []}, "eth": {"price": []}}))
    (tmp_path / "whale.json").write_text(json.dumps({"btc": {"tx_volume_usd": [1.0]}}))

    server.flask_app.config["TESTING"] = True
    with server.flask_app.test_client() as c:
        c.environ_base["HTTP_X_REQUESTED_WITH"] = "XMLHttpRequest"
        yield c


def test_share_token_can_fetch_sidecar(shared_client):
    entry = shares.create(days=1, label="t")
    # Without the share token, auth is required — request should be challenged.
    r_no_tok = shared_client.get("/data-whale.json")
    assert r_no_tok.status_code == 401

    # With the share token in the query string, the read-only allowlist
    # lets it through.
    r = shared_client.get(f"/data-whale.json?share={entry['token']}")
    assert r.status_code == 200
    assert r.get_json()["btc"]["tx_volume_usd"] == [1.0]


# ---------- 6. defi sidecar (DeFi-tab only payload) ----------


def test_defi_is_registered_as_sidecar():
    """``defi`` joins ``whale`` as a tab-specific lazy-loaded payload — the
    DeFi tab is the only consumer (renderDefi / renderDefiChainSection /
    renderDefiSentiment), so the ~86KB subtree shouldn't sit in the
    inlined HTML for users who never open it."""
    assert "defi" in app.SIDECAR_KEYS


def test_build_payload_hoists_defi_to_top_level(tmp_path: Path, monkeypatch):
    """``build_payload`` must promote ``market.defi`` to ``payload.defi`` so
    ``split_payload_for_sidecars`` (which only operates on top-level keys)
    can extract it into the data-defi.json sidecar."""
    monkeypatch.setattr(app, "DATA_DIR", tmp_path)
    (tmp_path / "btc_flows.csv").write_text("date,Total\n2024-01-11,100.0\n")
    (tmp_path / "eth_flows.csv").write_text("date,Total\n2024-07-23,5.0\n")
    (tmp_path / "market.json").write_text(json.dumps({
        "btc": {"price": []},
        "eth": {"price": []},
        "defi": {"chains": [{"name": "Ethereum", "tvl_usd": 1.0}]},
    }))
    (tmp_path / "whale.json").write_text(json.dumps({"btc": {"tx_volume_usd": []}}))

    payload = app.build_payload()
    # Hoisted to top level for sidecar extraction.
    assert payload["defi"] == {"chains": [{"name": "Ethereum", "tvl_usd": 1.0}]}
    # And removed from market so it isn't double-inlined.
    assert "defi" not in payload["market"]


def test_build_payload_defi_defaults_to_empty_dict(tmp_path: Path, monkeypatch):
    """When market.json has no ``defi`` block (e.g. DefiLlama fetch failed),
    payload.defi must be an empty dict — ``split_payload_for_sidecars``
    skips empty values so no manifest entry / sidecar file is written, and
    the client falls back to its in-place ``DATA.defi || {}`` guard."""
    monkeypatch.setattr(app, "DATA_DIR", tmp_path)
    (tmp_path / "btc_flows.csv").write_text("date,Total\n2024-01-11,100.0\n")
    (tmp_path / "eth_flows.csv").write_text("date,Total\n2024-07-23,5.0\n")
    (tmp_path / "market.json").write_text(json.dumps({"btc": {"price": []}, "eth": {"price": []}}))
    (tmp_path / "whale.json").write_text(json.dumps({}))

    payload = app.build_payload()
    assert payload["defi"] == {}
    _, sidecars, manifest = app.split_payload_for_sidecars(payload, keys=("defi",))
    assert "defi" not in sidecars
    assert manifest == {}


def test_main_writes_defi_sidecar(tmp_path: Path, monkeypatch):
    """End-to-end build: DeFi subtree must land in ``data-defi.json`` and
    NOT be inlined in dashboard.html — same contract as the whale sidecar
    test above, with a sentinel value we can grep for."""
    monkeypatch.setattr(app, "DATA_DIR", tmp_path)
    out_path = tmp_path / "dashboard.html"
    monkeypatch.setattr(app, "OUT", out_path)
    monkeypatch.setattr(app, "ROOT", tmp_path)

    (tmp_path / "btc_flows.csv").write_text("date,Total\n2024-01-11,100.0\n")
    (tmp_path / "eth_flows.csv").write_text("date,Total\n2024-07-23,5.0\n")
    # Sentinel TVL so the "not inlined" assertion can't false-positive.
    (tmp_path / "market.json").write_text(json.dumps({
        "btc": {"price": []},
        "eth": {"price": []},
        "defi": {"chains": [{"name": "Ethereum", "tvl_usd": 987654321.0}]},
    }))
    (tmp_path / "whale.json").write_text(json.dumps({"btc": {"tx_volume_usd": []}}))

    monkeypatch.setattr(sys, "argv", ["app.py", "--no-open"])
    assert app.main() == 0

    sidecar_path = tmp_path / "data-defi.json"
    assert sidecar_path.exists(), "data-defi.json missing after build"
    assert json.loads(sidecar_path.read_text())["chains"][0]["tvl_usd"] == 987654321.0

    html = out_path.read_text()
    # The sentinel value must NOT appear in the inlined HTML — defi now
    # lives only in /data-defi.json.
    assert "987654321" not in html, "defi data leaked into inlined HTML"
    # The manifest must include defi so loadSidecar() knows where to fetch.
    assert '"defi"' in html and '"data-defi.json"' in html


# ---------- 7. fear_greed history cap ----------


def test_build_payload_caps_fear_greed_history(tmp_path: Path, monkeypatch):
    """The alternative.me API silently ignores its ``?limit=`` query and
    returns the full ~3000-entry fear-and-greed history back to 2018.
    ``build_payload`` must trim to ``FEAR_GREED_MAX_DAYS`` (matches the
    longest UI range button) before the payload gets inlined — anything
    older than that is unreachable from the range selector."""
    monkeypatch.setattr(app, "DATA_DIR", tmp_path)
    over = app.FEAR_GREED_MAX_DAYS + 500
    # Build a list larger than the cap; date strings don't matter for this
    # test — the slice is positional (last N).
    fng = [{"date": f"2024-{(i % 12) + 1:02d}-01", "value": i % 100, "label": "X"} for i in range(over)]
    (tmp_path / "btc_flows.csv").write_text("date,Total\n2024-01-11,100.0\n")
    (tmp_path / "eth_flows.csv").write_text("date,Total\n2024-07-23,5.0\n")
    (tmp_path / "market.json").write_text(json.dumps({
        "btc": {"price": []},
        "eth": {"price": []},
        "fear_greed": fng,
    }))
    (tmp_path / "whale.json").write_text(json.dumps({}))

    payload = app.build_payload()
    assert len(payload["market"]["fear_greed"]) == app.FEAR_GREED_MAX_DAYS
    # Last entry is preserved (we keep the tail, not the head).
    assert payload["market"]["fear_greed"][-1] == fng[-1]


def test_build_payload_leaves_short_fear_greed_alone(tmp_path: Path, monkeypatch):
    """A short fear_greed list (under the cap) must pass through untouched —
    we slice only when there's something to trim, so a sparse / fresh
    fetch isn't surprisingly mutated."""
    monkeypatch.setattr(app, "DATA_DIR", tmp_path)
    short = [{"date": "2024-01-01", "value": 50, "label": "Neutral"}]
    (tmp_path / "btc_flows.csv").write_text("date,Total\n2024-01-11,100.0\n")
    (tmp_path / "eth_flows.csv").write_text("date,Total\n2024-07-23,5.0\n")
    (tmp_path / "market.json").write_text(json.dumps({
        "btc": {"price": []},
        "eth": {"price": []},
        "fear_greed": short,
    }))
    (tmp_path / "whale.json").write_text(json.dumps({}))

    payload = app.build_payload()
    assert payload["market"]["fear_greed"] == short


def test_fetch_market_fear_greed_slices_to_limit(monkeypatch):
    """``fetch_market.fear_greed`` requests ``?limit=N`` but the API ignores
    it, so the function must enforce the cap client-side before returning.
    Otherwise the on-disk market.json balloons with years of unreachable
    sentiment data."""
    import fetch_market as fm
    # Fake "data" payload from alternative.me — 200 entries (more than our
    # test limit of 5). The function's _get() is monkeypatched to return
    # this directly, dodging any network call.
    fake = {"data": [
        {"timestamp": str(1700000000 + i * 86400), "value": str(i % 100), "value_classification": "X"}
        for i in range(200)
    ]}
    monkeypatch.setattr(fm, "_get", lambda *a, **kw: fake)
    out = fm.fear_greed(limit=5)
    assert len(out) == 5
    # Output is sorted ascending by date; last 5 from a 200-entry input.
    # The slice keeps the tail (most recent), so dates are the LATEST 5.
    assert all("date" in r for r in out)
    # Sorted, monotonic-nondecreasing dates.
    dates = [r["date"] for r in out]
    assert dates == sorted(dates)
