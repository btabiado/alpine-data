"""Tests for the City tab Socrata adapter (``city/socrata.py``).

All HTTP is mocked — NO live network calls. A ``FakeSession`` captures the
request (url / params / headers) and returns a canned ``FakeResp`` so we can
assert both the outgoing query shape and the parsed result.

Conventions follow tests/conftest.py (repo-root on sys.path) and tests/test_fred.py
(monkeypatch + a tiny fake-response object).
"""
from __future__ import annotations

import pytest

from city import socrata


# --------------------------------------------------------------------------- #
# fakes
# --------------------------------------------------------------------------- #
class FakeResp:
    def __init__(self, payload, status_code=200, text=""):
        self._payload = payload
        self.status_code = status_code
        self.text = text

    def json(self):
        if self._payload is _MALFORMED:
            raise ValueError("No JSON object could be decoded")
        return self._payload


_MALFORMED = object()  # sentinel: .json() should raise


class FakeSession:
    """Records each GET and replays canned responses.

    ``responses`` may be a single FakeResp (reused for every call) or a list
    consumed in order (one per call, by URL-agnostic FIFO).
    """

    def __init__(self, responses):
        self._responses = responses
        self.calls = []  # list of dicts: {url, params, headers, timeout}

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(
            {"url": url, "params": params or {}, "headers": headers or {}, "timeout": timeout}
        )
        if isinstance(self._responses, list):
            return self._responses[len(self.calls) - 1]
        return self._responses


def _agg_rows(pairs):
    """Build Socrata date_trunc_ym aggregation rows from (month, n) pairs.

    date_trunc_ym returns a floating timestamp like '2026-04-01T00:00:00.000'.
    """
    return [{"m": f"{m}-01T00:00:00.000", "n": str(n)} for m, n in pairs]


# --------------------------------------------------------------------------- #
# 1. monthly_counts: query shape + parsing
# --------------------------------------------------------------------------- #
def test_monthly_counts_builds_date_trunc_query_and_parses_ascending():
    rows = _agg_rows([("2026-02", 10), ("2026-01", 7), ("2026-03", 13)])  # out of order
    sess = FakeSession(FakeResp(rows))

    out = socrata.monthly_counts(
        "data.cityofchicago.org", "ijzp-q8t2", "date", since="2024", session=sess
    )

    # Parsed, summed, and sorted ascending into the {month,n} contract.
    assert out == [
        {"month": "2026-01", "n": 7},
        {"month": "2026-02", "n": 10},
        {"month": "2026-03", "n": 13},
    ]

    call = sess.calls[0]
    assert call["url"] == "https://data.cityofchicago.org/resource/ijzp-q8t2.json"
    params = call["params"]
    # date_trunc_ym month bucket aliased m, count(*) AS n.
    assert params["$select"] == "date_trunc_ym(date) AS m, count(*) AS n"
    assert params["$group"] == "m"
    assert params["$order"] == "m"
    # IS NOT NULL always present; since normalized to Jan-1 of the bare year.
    assert "date IS NOT NULL" in params["$where"]
    assert "date >= '2024-01-01'" in params["$where"]
    # High limit so all monthly buckets fit one page.
    assert int(params["$limit"]) >= 50000


def test_monthly_counts_since_full_date_passed_through():
    sess = FakeSession(FakeResp(_agg_rows([("2026-01", 1)])))
    socrata.monthly_counts(
        "data.sfgov.org", "vw6y-z8j6", "requested_datetime",
        since="2024-03-01", session=sess,
    )
    where = sess.calls[0]["params"]["$where"]
    assert "requested_datetime >= '2024-03-01'" in where


def test_monthly_counts_no_since_only_not_null():
    sess = FakeSession(FakeResp(_agg_rows([("2026-01", 1)])))
    socrata.monthly_counts("data.sfgov.org", "vw6y-z8j6", "requested_datetime", session=sess)
    where = sess.calls[0]["params"]["$where"]
    assert where == "requested_datetime IS NOT NULL"


def test_monthly_counts_extra_where_appended():
    sess = FakeSession(FakeResp(_agg_rows([("2026-01", 1)])))
    socrata.monthly_counts(
        "data.sfgov.org", "vw6y-z8j6", "requested_datetime",
        extra_where="status = 'Closed'", session=sess,
    )
    where = sess.calls[0]["params"]["$where"]
    assert "requested_datetime IS NOT NULL" in where
    assert "status = 'Closed'" in where
    assert " AND " in where


# --------------------------------------------------------------------------- #
# 2. text-date path (NYC DOB issuance_date, MM/DD/YYYY)
# --------------------------------------------------------------------------- #
def test_text_date_path_emits_substring_bucket_not_date_trunc():
    # Text path returns the YYYY-MM key directly (no timestamp suffix).
    rows = [{"m": "2026-03", "n": "7180"}, {"m": "2026-02", "n": "6900"}]
    sess = FakeSession(FakeResp(rows))

    out = socrata.monthly_counts(
        "data.cityofnewyork.us", "ipu4-2q9a", "issuance_date",
        date_is_text=True, text_fmt="MM/DD/YYYY", session=sess,
    )

    assert out == [{"month": "2026-02", "n": 6900}, {"month": "2026-03", "n": 7180}]

    select = sess.calls[0]["params"]["$select"]
    # Substring bucket: year at pos 7 (len 4), month at pos 1 (len 2).
    assert "substring(issuance_date,7,4)" in select
    assert "substring(issuance_date,1,2)" in select
    assert "AS m" in select
    # date_trunc_ym must NOT appear (it errors on a text column).
    assert "date_trunc_ym" not in select


def test_text_date_since_filters_on_year_substring_not_lexicographic():
    # A plain `col >= '2026-...'` against MM/DD/YYYY text is a meaningless string
    # compare; the text path must instead year-floor via the parsed year substring.
    sess = FakeSession(FakeResp([{"m": "2026-04", "n": "7180"}]))
    socrata.monthly_counts(
        "data.cityofnewyork.us", "ipu4-2q9a", "issuance_date",
        date_is_text=True, since="2025-03-01", session=sess,
    )
    where = sess.calls[0]["params"]["$where"]
    assert "issuance_date IS NOT NULL" in where
    assert "substring(issuance_date,7,4) >= '2025'" in where
    # Must NOT emit the broken lexicographic full-date compare on the raw column.
    assert "issuance_date >= '2025-03-01'" not in where


# --------------------------------------------------------------------------- #
# 3. app token -> X-App-Token header
# --------------------------------------------------------------------------- #
def test_app_token_sets_x_app_token_header():
    sess = FakeSession(FakeResp(_agg_rows([("2026-01", 1)])))
    socrata.monthly_counts(
        "data.cityofnewyork.us", "erm2-nwe9", "created_date",
        app_token="tok-abc123", since="2024", session=sess,
    )
    headers = sess.calls[0]["headers"]
    assert headers.get("X-App-Token") == "tok-abc123"


def test_no_app_token_means_no_header():
    sess = FakeSession(FakeResp(_agg_rows([("2026-01", 1)])))
    socrata.monthly_counts("data.sfgov.org", "vw6y-z8j6", "requested_datetime", session=sess)
    assert "X-App-Token" not in sess.calls[0]["headers"]


# --------------------------------------------------------------------------- #
# 4. SocrataError on 429 + non-200 + malformed
# --------------------------------------------------------------------------- #
def test_monthly_counts_raises_on_429():
    sess = FakeSession(FakeResp({"errorCode": "too-many-requests"}, status_code=429,
                                text='{"errorCode":"too-many-requests"}'))
    with pytest.raises(socrata.SocrataError) as exc:
        socrata.monthly_counts("data.cityofnewyork.us", "erm2-nwe9", "created_date",
                               since="2024", session=sess)
    assert "429" in str(exc.value)


def test_monthly_counts_raises_on_non_200():
    sess = FakeSession(FakeResp({"error": "boom"}, status_code=500, text="server error"))
    with pytest.raises(socrata.SocrataError) as exc:
        socrata.monthly_counts("data.sfgov.org", "vw6y-z8j6", "requested_datetime", session=sess)
    assert "500" in str(exc.value)


def test_monthly_counts_raises_on_malformed_json():
    sess = FakeSession(FakeResp(_MALFORMED, status_code=200))
    with pytest.raises(socrata.SocrataError):
        socrata.monthly_counts("data.sfgov.org", "vw6y-z8j6", "requested_datetime", session=sess)


def test_monthly_counts_raises_on_non_list_payload():
    # A SoQL error often returns a JSON object, not a list of rows.
    sess = FakeSession(FakeResp({"error": True, "message": "type-mismatch"}, status_code=200))
    with pytest.raises(socrata.SocrataError):
        socrata.monthly_counts("data.cityofnewyork.us", "ipu4-2q9a", "issuance_date",
                               session=sess)


# --------------------------------------------------------------------------- #
# 5. feed_series: baseline union sums months
# --------------------------------------------------------------------------- #
def test_feed_series_merges_baseline_union_by_summing_months():
    # LA crime: live k7nn-b2ep (2026-01..) unioned with baseline y8y3-fqfu (..2025-12).
    primary_rows = _agg_rows([("2026-01", 13059), ("2026-02", 12000)])
    baseline_rows = _agg_rows([("2025-12", 11950), ("2026-01", 5)])  # overlap month -> summed
    sess = FakeSession([FakeResp(primary_rows), FakeResp(baseline_rows)])

    feed_cfg = {
        "dataset": "k7nn-b2ep",
        "baseline_dataset": "y8y3-fqfu",
        "date_col": "date_occ",
        "date_col_status": "confirmed",
    }
    out = socrata.feed_series(feed_cfg, "data.lacity.org", since="2025", session=sess)

    assert out == [
        {"month": "2025-12", "n": 11950},
        {"month": "2026-01", "n": 13064},  # 13059 + 5 summed at the (designed) overlap
        {"month": "2026-02", "n": 12000},
    ]
    # Both datasets were queried, primary first.
    assert sess.calls[0]["url"].endswith("/resource/k7nn-b2ep.json")
    assert sess.calls[1]["url"].endswith("/resource/y8y3-fqfu.json")


def test_feed_series_single_dataset_passthrough():
    rows = _agg_rows([("2026-02", 2846), ("2026-01", 2600)])
    sess = FakeSession(FakeResp(rows))
    feed_cfg = {"dataset": "ydr8-5enu", "date_col": "issue_date", "date_col_status": "confirmed"}

    out = socrata.feed_series(feed_cfg, "data.cityofchicago.org", session=sess)
    assert out == [{"month": "2026-01", "n": 2600}, {"month": "2026-02", "n": 2846}]
    assert len(sess.calls) == 1


def test_feed_series_text_date_feed_uses_substring_bucket():
    # NYC DOB: date_col_status text_not_date -> substring bucket, no date_trunc.
    rows = [{"m": "2026-04", "n": "7180"}]
    sess = FakeSession(FakeResp(rows))
    feed_cfg = {
        "dataset": "ipu4-2q9a",
        "date_col": "issuance_date",
        "date_col_status": "text_not_date",
        "date_text_format": "MM/DD/YYYY",
    }
    out = socrata.feed_series(feed_cfg, "data.cityofnewyork.us", session=sess)
    assert out == [{"month": "2026-04", "n": 7180}]
    select = sess.calls[0]["params"]["$select"]
    assert "substring(issuance_date,7,4)" in select
    assert "date_trunc_ym" not in select


def test_feed_series_reads_token_from_env_when_not_passed(monkeypatch):
    monkeypatch.setenv("SOCRATA_APP_TOKEN", "env-token-xyz")
    sess = FakeSession(FakeResp(_agg_rows([("2026-01", 1)])))
    feed_cfg = {"dataset": "vw6y-z8j6", "date_col": "requested_datetime",
                "date_col_status": "confirmed"}
    socrata.feed_series(feed_cfg, "data.sfgov.org", session=sess)
    assert sess.calls[0]["headers"].get("X-App-Token") == "env-token-xyz"


def test_feed_series_la_rotation_resolves_then_unions(monkeypatch):
    # LA 311 rotates yearly: feed_series must catalog-resolve the current dataset, then
    # union it with baseline_dataset. Catalog call is response #1, then the two queries.
    catalog_payload = {
        "results": [
            {"resource": {"id": "2cy6-i7zn", "name": "MyLA311 Cases 2026"}},
        ]
    }
    primary_rows = _agg_rows([("2026-04", 199584)])
    baseline_rows = _agg_rows([("2025-12", 180000)])
    sess = FakeSession([
        FakeResp(catalog_payload),   # la_current_311_dataset catalog GET
        FakeResp(primary_rows),      # monthly_counts(2cy6-i7zn)
        FakeResp(baseline_rows),     # monthly_counts(73a2-6ar5 baseline)
    ])

    feed_cfg = {
        "dataset": "2cy6-i7zn",
        "baseline_dataset": "73a2-6ar5",
        "dataset_rotates_yearly": True,
        "date_col": "createddate",
        "date_col_status": "confirmed",
    }
    out = socrata.feed_series(feed_cfg, "data.lacity.org", since="2025", session=sess)

    assert out == [{"month": "2025-12", "n": 180000}, {"month": "2026-04", "n": 199584}]
    # First call hit the catalog endpoint; the resolved id was queried next.
    assert sess.calls[0]["url"] == socrata._LA_CATALOG_URL
    assert sess.calls[1]["url"].endswith("/resource/2cy6-i7zn.json")
    assert sess.calls[2]["url"].endswith("/resource/73a2-6ar5.json")


# --------------------------------------------------------------------------- #
# 6. la_current_311_dataset: catalog pick + fallback
# --------------------------------------------------------------------------- #
def test_la_current_311_picks_most_recent_cases_year():
    payload = {
        "results": [
            {"resource": {"id": "dead-2025", "name": "MyLA311 Service Request Data 2025"}},
            {"resource": {"id": "bridge-1", "name": "MyLA311 Cases March 2025 to December 2025"}},
            {"resource": {"id": "cases-2025", "name": "MyLA311 Cases 2025"}},
            {"resource": {"id": "2cy6-i7zn", "name": "MyLA311 Cases 2026"}},
        ]
    }
    sess = FakeSession(FakeResp(payload))
    out = socrata.la_current_311_dataset(session=sess)
    # Picks 'MyLA311 Cases 2026' (most recent), not the retired Service Request Data
    # series and not the date-range bridge file.
    assert out == "2cy6-i7zn"
    assert sess.calls[0]["url"] == socrata._LA_CATALOG_URL
    assert sess.calls[0]["params"]["q"] == "MyLA311 Cases"


def test_la_current_311_fallback_on_http_error():
    sess = FakeSession(FakeResp({"err": "x"}, status_code=503, text="down"))
    out = socrata.la_current_311_dataset(session=sess, fallback="2cy6-i7zn")
    assert out == "2cy6-i7zn"


def test_la_current_311_fallback_on_no_match():
    # No 'Cases {year}' item -> fallback (don't construct a Service Request Data id).
    payload = {"results": [
        {"resource": {"id": "dead", "name": "MyLA311 Service Request Data 2025"}},
    ]}
    sess = FakeSession(FakeResp(payload))
    out = socrata.la_current_311_dataset(session=sess, fallback="fallback-id")
    assert out == "fallback-id"


def test_la_current_311_fallback_on_malformed_payload():
    sess = FakeSession(FakeResp(_MALFORMED, status_code=200))
    out = socrata.la_current_311_dataset(session=sess, fallback="fb")
    assert out == "fb"


def test_la_current_311_passes_app_token_header():
    payload = {"results": [{"resource": {"id": "2cy6-i7zn", "name": "MyLA311 Cases 2026"}}]}
    sess = FakeSession(FakeResp(payload))
    socrata.la_current_311_dataset(app_token="tok-1", session=sess)
    assert sess.calls[0]["headers"].get("X-App-Token") == "tok-1"
