"""Tests for the ArcGIS adapter (city/arcgis.py).

All HTTP is mocked via a tiny fake session that records each ``.get(...)`` and
returns a canned JSON payload — no live network in pytest. The live smoke test
is run separately (see the adapter docstring / handoff notes).
"""
from __future__ import annotations

import json

import pytest

from city import arcgis
from city.arcgis import (
    ArcGISError,
    feed_series,
    permits_monthly,
    snapshot_311_monthly,
)


# ---------------------------------------------------------------------------
# Fake HTTP session
# ---------------------------------------------------------------------------
class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeSession:
    """Records GET calls and replays a queue (or callable) of responses."""

    def __init__(self, responses):
        # ``responses`` may be a list (consumed in order) or a callable
        # (params -> payload) for content-addressed replies.
        self._responses = responses
        self.calls = []  # list of (url, params) tuples

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        if callable(self._responses):
            payload = self._responses(params or {})
            return FakeResponse(payload)
        payload = self._responses.pop(0)
        if isinstance(payload, FakeResponse):
            return payload
        return FakeResponse(payload)


PERMITS_URL = (
    "https://services.arcgis.com/8Pc9XBTAsYuxx9Ny/arcgis/rest/services/"
    "BuildingPermit_gdb/FeatureServer/0"
)
T311_URL = (
    "https://services.arcgis.com/8Pc9XBTAsYuxx9Ny/arcgis/rest/services/"
    "data_311_2023/FeatureServer/0"
)


# ---------------------------------------------------------------------------
# permits_monthly
# ---------------------------------------------------------------------------
def test_permits_monthly_three_month_range_queries_and_ascending():
    """One query per month over a 3-month range, correct boundaries, ascending."""
    session = FakeSession([{"count": 100}, {"count": 200}, {"count": 300}])
    result = permits_monthly(
        PERMITS_URL, "ISSUDATE", since="2026-02", until="2026-04", session=session
    )

    assert result == [
        {"month": "2026-02", "n": 100},
        {"month": "2026-03", "n": 200},
        {"month": "2026-04", "n": 300},
    ]
    # Exactly one query per month.
    assert len(session.calls) == 3

    # Each query hits the layer's /query endpoint with returnCountOnly + f=json.
    for url, params in session.calls:
        assert url == PERMITS_URL + "/query"
        assert params["returnCountOnly"] == "true"
        assert params["f"] == "json"

    # Month boundaries: [month-01, next-month-01), and IS NOT NULL present.
    w_feb = session.calls[0][1]["where"]
    assert "ISSUDATE >= TIMESTAMP '2026-02-01 00:00:00'" in w_feb
    assert "ISSUDATE < TIMESTAMP '2026-03-01 00:00:00'" in w_feb
    assert "ISSUDATE IS NOT NULL" in w_feb

    w_mar = session.calls[1][1]["where"]
    assert "ISSUDATE >= TIMESTAMP '2026-03-01 00:00:00'" in w_mar
    assert "ISSUDATE < TIMESTAMP '2026-04-01 00:00:00'" in w_mar

    w_apr = session.calls[2][1]["where"]
    assert "ISSUDATE >= TIMESTAMP '2026-04-01 00:00:00'" in w_apr
    assert "ISSUDATE < TIMESTAMP '2026-05-01 00:00:00'" in w_apr


def test_permits_monthly_december_to_january_rollover():
    """Dec upper bound must roll the YEAR forward to next-Jan, not month 13."""
    session = FakeSession([{"count": 42}])
    result = permits_monthly(
        PERMITS_URL, "ISSUDATE", since="2025-12", until="2025-12", session=session
    )

    assert result == [{"month": "2025-12", "n": 42}]
    where = session.calls[0][1]["where"]
    assert "ISSUDATE >= TIMESTAMP '2025-12-01 00:00:00'" in where
    # Critical: next month is 2026-01, NOT 2025-13.
    assert "ISSUDATE < TIMESTAMP '2026-01-01 00:00:00'" in where
    assert "2025-13" not in where


def test_permits_monthly_range_spanning_year_boundary():
    """A Nov->Feb range exercises the rollover mid-loop and stays ascending."""
    session = FakeSession(
        [{"count": 1}, {"count": 2}, {"count": 3}, {"count": 4}]
    )
    result = permits_monthly(
        PERMITS_URL, "ISSUDATE", since="2025-11", until="2026-02", session=session
    )

    assert [r["month"] for r in result] == [
        "2025-11",
        "2025-12",
        "2026-01",
        "2026-02",
    ]
    assert [r["n"] for r in result] == [1, 2, 3, 4]
    # Spot-check the seam: Dec query ends at 2026-01, Jan query ends at 2026-02.
    assert "TIMESTAMP '2026-01-01 00:00:00'" in session.calls[1][1]["where"]
    assert "ISSUDATE >= TIMESTAMP '2026-01-01 00:00:00'" in session.calls[2][1]["where"]
    assert "ISSUDATE < TIMESTAMP '2026-02-01 00:00:00'" in session.calls[2][1]["where"]


def test_permits_monthly_single_month():
    session = FakeSession([{"count": 5353}])
    result = permits_monthly(
        PERMITS_URL, "ISSUDATE", since="2026-04", until="2026-04", session=session
    )
    assert result == [{"month": "2026-04", "n": 5353}]
    assert len(session.calls) == 1


def test_permits_monthly_trailing_slash_normalized():
    """A layer_url with a trailing slash should not produce '//query'."""
    session = FakeSession([{"count": 7}])
    permits_monthly(
        PERMITS_URL + "/", "ISSUDATE", since="2026-04", until="2026-04",
        session=session,
    )
    assert session.calls[0][0] == PERMITS_URL + "/query"


def test_permits_monthly_uses_provided_date_field_name():
    """The where-clause is built from the passed date_field, not hardcoded."""
    session = FakeSession([{"count": 1}])
    permits_monthly(
        PERMITS_URL, "SOME_OTHER_DATE", since="2026-04", until="2026-04",
        session=session,
    )
    where = session.calls[0][1]["where"]
    assert where.count("SOME_OTHER_DATE") == 3  # >=, <, IS NOT NULL
    assert "ISSUDATE" not in where


def test_permits_monthly_since_after_until_raises():
    session = FakeSession([{"count": 1}])
    with pytest.raises(ArcGISError):
        permits_monthly(
            PERMITS_URL, "ISSUDATE", since="2026-04", until="2026-02",
            session=session,
        )


def test_permits_monthly_missing_count_raises():
    session = FakeSession([{"not_count": 1}])
    with pytest.raises(ArcGISError):
        permits_monthly(
            PERMITS_URL, "ISSUDATE", since="2026-04", until="2026-04",
            session=session,
        )


def test_permits_monthly_inband_error_envelope_raises():
    """ArcGIS returns errors inside an HTTP 200 body — must still raise."""
    session = FakeSession(
        [{"error": {"code": 400, "message": "", "details": ["'where' parameter is invalid"]}}]
    )
    with pytest.raises(ArcGISError) as exc:
        permits_monthly(
            PERMITS_URL, "ISSUDATE", since="2026-04", until="2026-04",
            session=session,
        )
    assert "where" in str(exc.value)


def test_permits_monthly_bad_month_string_raises():
    session = FakeSession([{"count": 1}])
    with pytest.raises(ArcGISError):
        permits_monthly(
            PERMITS_URL, "ISSUDATE", since="2026-13", until="2026-13",
            session=session,
        )


# ---------------------------------------------------------------------------
# snapshot_311_monthly
# ---------------------------------------------------------------------------
def _grouped_311_payload():
    """Canned grouped-statistics payload mirroring the live 2023 snapshot.

    Note the unpadded yyyymm ints for Jan-Sep (20231..20239) — exactly what the
    live server returns — and deliberately out-of-order rows to prove sorting.
    """
    months = {
        20231: 27338, 20232: 23584, 20233: 26821, 20234: 27716,
        20235: 29850, 20236: 33550, 20237: 31254, 20238: 32898,
        20239: 28549, 202310: 28901, 202311: 27149, 202312: 26241,
    }
    items = list(months.items())
    # Shuffle-ish: reverse so the function must sort ascending.
    items.reverse()
    return {
        "features": [
            {"attributes": {"created_year_month": ym, "n": n}}
            for ym, n in items
        ]
    }


def test_snapshot_311_monthly_maps_yyyymm_ascending():
    session = FakeSession([_grouped_311_payload()])
    result = snapshot_311_monthly(T311_URL, session=session)

    months = [r["month"] for r in result]
    # 12 months, zero-padded, ascending — note Jan-Sep get the leading zero
    # back even though the source ints (20231..20239) were unpadded.
    assert months == [
        "2023-01", "2023-02", "2023-03", "2023-04", "2023-05", "2023-06",
        "2023-07", "2023-08", "2023-09", "2023-10", "2023-11", "2023-12",
    ]
    assert result[0] == {"month": "2023-01", "n": 27338}
    assert result[5] == {"month": "2023-06", "n": 33550}
    assert result[-1] == {"month": "2023-12", "n": 26241}
    assert sum(r["n"] for r in result) == 343851


def test_snapshot_311_monthly_query_shape():
    session = FakeSession([_grouped_311_payload()])
    snapshot_311_monthly(T311_URL, session=session)

    assert len(session.calls) == 1  # single grouped query, no per-month loop
    url, params = session.calls[0]
    assert url == T311_URL + "/query"
    assert params["where"] == "1=1"
    assert params["groupByFieldsForStatistics"] == "created_year_month"
    assert params["f"] == "json"
    # outStatistics is valid JSON requesting a count into "n".
    stats = json.loads(params["outStatistics"])
    assert stats == [
        {
            "statisticType": "count",
            "onStatisticField": "ObjectId",
            "outStatisticFieldName": "n",
        }
    ]


def test_snapshot_311_monthly_custom_fields():
    """month_bucket_field and object_id_field are honored in the query."""
    payload = {"features": [{"attributes": {"yyyymm_alt": 202304, "n": 9}}]}
    session = FakeSession([payload])
    result = snapshot_311_monthly(
        T311_URL,
        month_bucket_field="yyyymm_alt",
        object_id_field="OBJECTID",
        session=session,
    )
    assert result == [{"month": "2023-04", "n": 9}]
    params = session.calls[0][1]
    assert params["groupByFieldsForStatistics"] == "yyyymm_alt"
    stats = json.loads(params["outStatistics"])
    assert stats[0]["onStatisticField"] == "OBJECTID"
    assert json.loads(params["outStatistics"])[0]["outStatisticFieldName"] == "n"


def test_snapshot_311_monthly_skips_null_bucket():
    """A null created_year_month group is dropped, not emitted as 'None'."""
    payload = {
        "features": [
            {"attributes": {"created_year_month": None, "n": 5}},
            {"attributes": {"created_year_month": 202307, "n": 31254}},
        ]
    }
    session = FakeSession([payload])
    result = snapshot_311_monthly(T311_URL, session=session)
    assert result == [{"month": "2023-07", "n": 31254}]


def test_snapshot_311_monthly_missing_features_raises():
    session = FakeSession([{"not_features": []}])
    with pytest.raises(ArcGISError):
        snapshot_311_monthly(T311_URL, session=session)


def test_snapshot_311_monthly_empty_features_ok():
    session = FakeSession([{"features": []}])
    assert snapshot_311_monthly(T311_URL, session=session) == []


# ---------------------------------------------------------------------------
# feed_series dispatch
# ---------------------------------------------------------------------------
# Real Miami feed blocks from city_registry.resolved.json.
PERMITS_FEED = {
    "pillar": "development_economy",
    "label": "MDC Building Permit",
    "endpoint": PERMITS_URL,
    "arcgis_entity": "layer",
    "date_col": "ISSUDATE",
    "date_col_status": "confirmed",
    "date_col_type": "esriFieldTypeDate",
    "object_id_field": "OBJECTID",
    "supports_statistics": True,
    "polarity": 1,
    "metric": "count",
}

FEED_311 = {
    "pillar": "city_services",
    "label": "Miami-Dade 311",
    "endpoint": T311_URL,
    "arcgis_entity": "table",
    "date_col": "ticket_created_date_time",
    "date_col_status": "stale_source",
    "date_col_type": "esriFieldTypeDate",
    "month_bucket_field": "created_year_month",
    "object_id_field": "ObjectId",
    "polarity": -1,
    "metric": "count",
}

FBI_FEED = {
    "pillar": "public_safety",
    "label": "FBI CDE (fallback)",
    "adapter": "fbi",
    "ori": "FL0130000",
    "polarity": -1,
    "metric": "count",
}


def test_feed_series_permits_returns_ok():
    session = FakeSession([{"count": 5270}, {"count": 5353}])
    series, status = feed_series(
        PERMITS_FEED, since="2026-03", until="2026-04", session=session
    )
    assert status == "ok"
    assert series == [
        {"month": "2026-03", "n": 5270},
        {"month": "2026-04", "n": 5353},
    ]
    # Confirms it used endpoint + date_col from the feed dict.
    assert session.calls[0][0] == PERMITS_URL + "/query"
    assert "ISSUDATE" in session.calls[0][1]["where"]


def test_feed_series_311_returns_stale():
    session = FakeSession([_grouped_311_payload()])
    series, status = feed_series(FEED_311, session=session)
    assert status == "stale"
    assert len(series) == 12
    assert series[0] == {"month": "2023-01", "n": 27338}
    # Used the table endpoint + grouped query (object id from the feed dict).
    assert session.calls[0][0] == T311_URL + "/query"
    stats = json.loads(session.calls[0][1]["outStatistics"])
    assert stats[0]["onStatisticField"] == "ObjectId"


def test_feed_series_fbi_returns_not_published_without_network():
    session = FakeSession([])  # would IndexError if any .get fired
    series, status = feed_series(FBI_FEED, session=session)
    assert series == []
    assert status == "not_published"
    assert session.calls == []  # never touched the network


def test_feed_series_permits_requires_since_until():
    session = FakeSession([{"count": 1}])
    with pytest.raises(ArcGISError):
        feed_series(PERMITS_FEED, session=session)  # missing since/until


def test_feed_series_311_ignores_since_until():
    """The 311 snapshot is a fixed year; since/until are accepted but unused."""
    session = FakeSession([_grouped_311_payload()])
    series, status = feed_series(
        FEED_311, since="2030-01", until="2030-12", session=session
    )
    assert status == "stale"
    assert series[0]["month"] == "2023-01"
    # No date predicate at all — just the grouped where=1=1.
    assert session.calls[0][1]["where"] == "1=1"


def test_feed_series_unknown_shape_raises():
    session = FakeSession([])
    with pytest.raises(ArcGISError):
        feed_series({"date_col_status": "something_else"}, session=session)


# ---------------------------------------------------------------------------
# Default session wiring
# ---------------------------------------------------------------------------
def test_default_session_is_a_requests_session():
    import requests

    assert isinstance(arcgis._SESSION, requests.Session)


def test_permits_monthly_uses_module_session_when_none(monkeypatch):
    """When session= is omitted, calls go through the module-level session."""
    recorded = {}

    def fake_get(url, params=None, timeout=None):
        recorded["url"] = url
        recorded["params"] = dict(params or {})
        return FakeResponse({"count": 11})

    monkeypatch.setattr(arcgis._SESSION, "get", fake_get)
    result = permits_monthly(PERMITS_URL, "ISSUDATE", since="2026-04", until="2026-04")
    assert result == [{"month": "2026-04", "n": 11}]
    assert recorded["url"] == PERMITS_URL + "/query"
