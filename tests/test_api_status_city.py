"""The City probes in api_status.py must probe what the City fetchers fetch.

PR #39 moved the City tab off three dead upstreams (SF's data.sfgov.org host,
Miami's token-gated BuildingPermit_gdb layer, and a key gate in front of the
keyless FBI CDE series) but left /health/ probing the old ones. Every one of
those probes still reported "up": data.sfgov.org 301s to data.sf.gov (urllib
follows it), the Socrata catalog ping is platform-wide rather than per-portal,
and ArcGIS answers "Token Required" with HTTP 200. So the status page could not
see the outages that emptied Miami's pillars.

These tests tie each City probe to docs/city/city_registry.resolved.json and to
the constants in city/*.py, so the next upstream move has to update both or the
build fails. All offline: nothing here touches the network.
"""
from __future__ import annotations

import io
import json
import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

import api_status
from city import airnow, bls, census, fbi

REPO_ROOT = Path(__file__).resolve().parent.parent
REGISTRY = json.loads(
    (REPO_ROOT / "docs" / "city" / "city_registry.resolved.json").read_text())
CITY_SRC = "\n".join(p.read_text() for p in sorted((REPO_ROOT / "city").glob("*.py")))

CITY = [t for t in api_status.TARGETS if t["category"] == "City"]


def _city(city_id: str) -> dict:
    return next(c for c in REGISTRY["cities"] if c["id"] == city_id)


def _feed(city_id: str, pillar: str) -> dict:
    return next(f for f in _city(city_id)["feeds"] if f["pillar"] == pillar)


def _target(label: str) -> dict:
    hits = [t for t in CITY if t["label"] == label]
    assert len(hits) == 1, f"expected one City probe labelled {label!r}"
    return hits[0]


# ==========================================================================
# The specific endpoints that went dead must never come back
# ==========================================================================

@pytest.mark.parametrize("dead", [
    "data.sfgov.org",       # SF portal moved to data.sf.gov (301, nginx 403)
    "BuildingPermit_gdb",   # ArcGIS code=499 Token Required since 2026
    "api/catalog/v1",       # platform-wide, so it is not a per-portal ping
])
def test_no_probe_targets_a_retired_endpoint(dead):
    offenders = [t["label"] for t in api_status.TARGETS if dead in t["url"]]
    assert not offenders, f"{offenders} still probe {dead!r}"


def test_fbi_probe_names_no_key():
    """cde.ucr.cjis.gov/LATEST serves the data series keyless (re-verified
    2026-10-04). A key_env here would print 'no key' for a key that gates
    nothing, and force a needless secret into pages.yml via the wiring test."""
    assert _target("FBI Crime Data Explorer")["key_env"] is None
    assert "FBI_CDE_API_KEY" not in api_status.KEY_ENVS


# ==========================================================================
# Every City probe sits on the host and path its fetcher actually calls
# ==========================================================================

def test_socrata_probes_cover_exactly_the_registry_portals():
    """One probe per Socrata city in the registry, and no probe for a portal
    the registry no longer names (the data.sfgov.org shape)."""
    registry_hosts = {c["host"] for c in REGISTRY["cities"] if c["adapter"] == "socrata"}
    probe_hosts = {urlparse(t["url"]).netloc for t in CITY
                   if t["label"].startswith("Socrata")}
    assert probe_hosts == registry_hosts


def test_socrata_probes_read_a_registry_dataset_via_the_resource_path():
    """city/socrata.py reads https://{host}/resource/{dataset}.json, so a 200
    from the same path is evidence the fetch would work; a catalog ping is not."""
    for t in CITY:
        if not t["label"].startswith("Socrata"):
            continue
        u = urlparse(t["url"])
        cfg = next(c for c in REGISTRY["cities"] if c.get("host") == u.netloc)
        m = re.fullmatch(r"/resource/([a-z0-9]{4}-[a-z0-9]{4})\.json", u.path)
        assert m, f"{t['label']}: {u.path!r} is not a /resource/<id>.json read"
        datasets = {f.get("dataset") for f in cfg["feeds"]}
        datasets |= {f.get("baseline_dataset") for f in cfg["feeds"]}
        assert m.group(1) in datasets, (
            f"{t['label']} probes {m.group(1)}, which {cfg['id']}'s registry "
            f"entry does not use")
        assert parse_qs(u.query).get("$limit") == ["1"], "keep it a single row"
        assert t["key_env"] == "SOCRATA_APP_TOKEN"


def test_arcgis_probe_queries_the_registry_permit_layer_and_reads_the_body():
    t = _target("ArcGIS (Miami)")
    layer = _feed("miami", "development_economy")["endpoint"].rstrip("/")
    assert t["url"].startswith(layer + "/query?"), (
        f"probe {t['url']!r} is not a query on the registry layer {layer!r}")
    assert parse_qs(urlparse(t["url"]).query).get("returnCountOnly") == ["true"]
    # Without the body check a token-gated layer reads as HTTP 200 "up".
    assert t.get("body_check") == "arcgis"


def test_fbi_probe_reads_the_cde_data_series_for_the_registry_ori():
    t = _target("FBI Crime Data Explorer")
    base = urlparse(fbi._CDE_BASE)
    u = urlparse(t["url"])
    assert u.netloc == base.netloc
    assert u.path.startswith(base.path.rstrip("/") + "/summarized/agency/")
    ori = _feed("miami", "public_safety")["ori"]
    assert f"/agency/{ori}/" in u.path
    # A fixed, already-published month: tiny and stable. Unbounded, CDE
    # returns its whole window.
    q = parse_qs(u.query)
    assert q.get("from") and q.get("to")


def test_census_probe_uses_the_fetcher_host_and_registry_vintage():
    t = _target("Census ACS")
    vintage = REGISTRY["context_layer"]["acs_vintage"]
    acs_base = census._ACS_BASE.format(vintage=vintage)
    assert t["url"].startswith(acs_base + "/variables/"), (
        f"Census probe should read ACS {vintage} metadata under {acs_base}")
    env = REGISTRY["context_layer"]["sources"]["census_acs"]["env_var"]
    assert t["key_env"] == env


def test_bls_probe_requests_a_laus_series_the_fetcher_uses():
    t = _target("BLS")
    assert t["url"].startswith(bls._API_URL)
    series = t["url"][len(bls._API_URL):].strip("/")
    assert series in bls.CITY_LAUS_SERIES.values()
    assert t["key_env"] == REGISTRY["context_layer"]["sources"]["bls_laus"]["env_var"]


def test_airnow_probe_uses_the_fetcher_endpoint():
    t = _target("EPA AirNow")
    assert t["url"].startswith(airnow._AIRNOW_URL)
    assert t["key_env"] == REGISTRY["context_layer"]["sources"]["epa_airnow"]["env_var"]


def test_city_key_envs_are_env_vars_the_city_fetchers_read():
    """A key_env the fetchers never read reports on a key that does nothing."""
    for t in CITY:
        if t["key_env"]:
            assert f'os.environ.get("{t["key_env"]}")' in CITY_SRC, (
                f"{t['label']} names {t['key_env']}, which no city/*.py reads")


# ==========================================================================
# ArcGIS in-band errors: HTTP 200 must not be read as "up"
# ==========================================================================

TOKEN_REQUIRED = (b'{"error":{"code":499,"message":"Token Required",'
                  b'"messageCode":"GWM_0003","details":["Token Required"]}}')


class _FakeResponse(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _probe_with_body(monkeypatch, body: bytes, target: dict) -> dict:
    monkeypatch.setattr(api_status.urllib.request, "urlopen",
                        lambda *a, **k: _FakeResponse(body))
    return api_status._probe_one(target, timeout=1, attempts=1)


def test_arcgis_error_envelope_is_parsed():
    assert api_status._arcgis_inband_error(TOKEN_REQUIRED) == (499, "Token Required")
    assert api_status._arcgis_inband_error(b'{"count":139627}') is None
    assert api_status._arcgis_inband_error(b"<html>not json</html>") is None
    assert api_status._arcgis_inband_error(b'{"error":{"message":"x"}}')[0] == 500


def test_token_required_with_http_200_is_blocked_not_up(monkeypatch):
    """The exact response the retired BuildingPermit_gdb layer gives."""
    row = _probe_with_body(monkeypatch, TOKEN_REQUIRED, _target("ArcGIS (Miami)"))
    assert row["verdict"] == "blocked"
    assert row["reachable"] is False
    assert row["status"] == 200, "the HTTP column must show the real status"
    assert "499" in row["note"] and "Token Required" in row["note"]


def test_other_arcgis_errors_are_degraded(monkeypatch):
    body = b'{"error":{"code":400,"message":"Invalid query parameters"}}'
    row = _probe_with_body(monkeypatch, body, _target("ArcGIS (Miami)"))
    assert row["verdict"] == "degraded"


def test_a_healthy_arcgis_count_is_up(monkeypatch):
    row = _probe_with_body(monkeypatch, b'{"count":139627}', _target("ArcGIS (Miami)"))
    assert row["verdict"] == "up"
    assert row["note"] == ""


def test_body_is_ignored_without_body_check(monkeypatch):
    """Only opted-in targets pay for reading the body."""
    target = {"label": "T", "category": "C", "url": "https://example.test/x",
              "key_env": None}
    row = _probe_with_body(monkeypatch, TOKEN_REQUIRED, target)
    assert row["verdict"] == "up"
