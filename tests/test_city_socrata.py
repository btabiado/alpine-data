"""Tests for the City tab Socrata adapter (``city/socrata.py``).

All HTTP is mocked — NO live network calls. A ``FakeSession`` captures the
request (url / params / headers) and returns a canned ``FakeResp`` so we can
assert both the outgoing query shape and the parsed result.

Conventions follow tests/conftest.py (repo-root on sys.path) and tests/test_fred.py
(monkeypatch + a tiny fake-response object).
"""
from __future__ import annotations

import calendar
import json
from datetime import date
from pathlib import Path

import pytest

from city import pulse, socrata


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


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch):
    """The transient-failure retry path must not make the suite wait."""
    monkeypatch.setattr(socrata, "_sleep", lambda seconds: None)


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
    # ...plus the newest record per bucket, used to spot an unfinished month.
    assert params["$select"] == "date_trunc_ym(date) AS m, count(*) AS n, max(date) AS last"
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
# 1b. an unfinished newest month is not handed to the scorer
# --------------------------------------------------------------------------- #
def _rows_with_last(triples):
    return [{"m": f"{m}-01T00:00:00.000", "n": str(n), "last": last}
            for m, n, last in triples]


def test_newest_month_cut_short_upstream_is_dropped():
    """Live LAPD NIBRS shape on 2026-10-04: September stops on the 19th.

    10,097 offenses against a ~18,300 baseline would score as a 45% crime drop.
    """
    rows = _rows_with_last([
        ("2026-07", 19324, "2026-07-31T23:55:00.000"),
        ("2026-08", 18313, "2026-08-31T23:50:00.000"),
        ("2026-09", 10097, "2026-09-19T00:00:00.000"),
    ])
    out = socrata.monthly_counts("data.lacity.org", "k7nn-b2ep", "date_occ",
                                 session=FakeSession(FakeResp(rows)))
    assert out == [{"month": "2026-07", "n": 19324}, {"month": "2026-08", "n": 18313}]


def test_newest_month_running_to_month_end_is_kept():
    # Last record two days before month-end is inside the slack (weekend, holiday).
    rows = _rows_with_last([
        ("2026-08", 900, "2026-08-31T00:00:00.000"),
        ("2026-09", 950, "2026-09-28T00:00:00.000"),
    ])
    out = socrata.monthly_counts("data.cityofchicago.org", "ydr8-5enu", "issue_date",
                                 session=FakeSession(FakeResp(rows)))
    assert [r["month"] for r in out] == ["2026-08", "2026-09"]


def test_only_the_newest_month_is_ever_judged():
    """An earlier month with a short `last` is complete by definition: the
    portal already published later data. Only the tail can be unfinished."""
    rows = _rows_with_last([
        ("2026-08", 900, "2026-08-12T00:00:00.000"),   # sparse, but followed
        ("2026-09", 950, "2026-09-30T00:00:00.000"),
        ("2026-10", 40, "2026-10-03T00:00:00.000"),    # current month: dropped
    ])
    out = socrata.monthly_counts("data.sf.gov", "vw6y-z8j6", "requested_datetime",
                                 session=FakeSession(FakeResp(rows)))
    assert [r["month"] for r in out] == ["2026-08", "2026-09"]


def test_text_date_last_value_is_understood():
    """NYC DOB issuance_date is TEXT MM/DD/YYYY; max() on it is per-bucket text."""
    rows = [{"m": "2026-09", "n": "560", "last": "09/30/2026"},
            {"m": "2026-10", "n": "13", "last": "10/01/2026"},
            {"m": "2-03-19", "n": "240", "last": "1999-12-03"}]   # junk bucket
    out = socrata.monthly_counts("data.cityofnewyork.us", "ipu4-2q9a", "issuance_date",
                                 date_is_text=True, session=FakeSession(FakeResp(rows)))
    assert out == [{"month": "2026-09", "n": 560}]


def test_rows_without_last_are_never_trimmed():
    rows = _agg_rows([("2026-08", 1), ("2026-09", 2)])
    out = socrata.monthly_counts("data.sf.gov", "vw6y-z8j6", "requested_datetime",
                                 session=FakeSession(FakeResp(rows)))
    assert [r["month"] for r in out] == ["2026-08", "2026-09"]


# --------------------------------------------------------------------------- #
# 1c. transient failures are retried; permanent ones are not
# --------------------------------------------------------------------------- #
def test_throttle_then_success_is_retried():
    rows = _agg_rows([("2026-08", 5)])
    sess = FakeSession([FakeResp({}, status_code=429, text="slow down"), FakeResp(rows)])
    out = socrata.monthly_counts("data.sf.gov", "vw6y-z8j6", "requested_datetime",
                                 session=sess)
    assert out == [{"month": "2026-08", "n": 5}]
    assert len(sess.calls) == 2


def test_timeout_then_success_is_retried():
    import requests

    rows = _agg_rows([("2026-08", 5)])

    class FlakySession(FakeSession):
        def get(self, url, params=None, headers=None, timeout=None):
            if not self.calls:
                self.calls.append({"url": url})
                raise requests.ReadTimeout("Read timed out. (read timeout=120)")
            return super().get(url, params=params, headers=headers, timeout=timeout)

    sess = FlakySession(FakeResp(rows))
    out = socrata.monthly_counts("data.lacity.org", "73a2-6ar5", "createddate",
                                 session=sess)
    assert out == [{"month": "2026-08", "n": 5}]


def test_persistent_gateway_error_gives_up_after_three_attempts():
    sess = FakeSession(FakeResp({}, status_code=503, text="unavailable"))
    with pytest.raises(socrata.SocrataError) as exc:
        socrata.monthly_counts("data.sf.gov", "vw6y-z8j6", "requested_datetime",
                               session=sess)
    assert "503" in str(exc.value)
    assert len(sess.calls) == 3


def test_retired_dataset_403_is_not_retried():
    """LA's y8y3-fqfu answers 403 'You must be logged in' — permanent."""
    sess = FakeSession(FakeResp({}, status_code=403,
                                text='{"message":"You must be logged in"}'))
    with pytest.raises(socrata.SocrataError) as exc:
        socrata.monthly_counts("data.lacity.org", "y8y3-fqfu", "date_occ", session=sess)
    assert "403" in str(exc.value)
    assert len(sess.calls) == 1


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
        FakeResp(catalog_payload),   # la_311_datasets_by_year catalog GET
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
# 6. la_311_datasets_by_year: catalog parsing + failure modes
# --------------------------------------------------------------------------- #
def test_la_311_by_year_keeps_only_cases_year_titles():
    payload = {
        "results": [
            {"resource": {"id": "dead-2025", "name": "MyLA311 Service Request Data 2025"}},
            {"resource": {"id": "bridge-1", "name": "MyLA311 Cases March 2025 to December 2025"}},
            {"resource": {"id": "cases-2025", "name": "MyLA311 Cases 2025"}},
            {"resource": {"id": "2cy6-i7zn", "name": "MyLA311 Cases 2026"}},
        ]
    }
    sess = FakeSession(FakeResp(payload))
    out = socrata.la_311_datasets_by_year(session=sess)
    # Only 'MyLA311 Cases {year}': not the retired Service Request Data series
    # and not the date-range bridge file.
    assert out == {2025: "cases-2025", 2026: "2cy6-i7zn"}
    assert sess.calls[0]["url"] == socrata._LA_CATALOG_URL
    assert sess.calls[0]["params"]["q"] == "MyLA311 Cases"


def test_la_311_by_year_is_none_on_http_error():
    # None (not {}) tells feed_series to fall back to the registry ids.
    sess = FakeSession(FakeResp({"err": "x"}, status_code=503, text="down"))
    assert socrata.la_311_datasets_by_year(session=sess) is None


def test_la_311_by_year_is_empty_on_no_match():
    # No 'Cases {year}' item -> {} (don't construct a Service Request Data id).
    payload = {"results": [
        {"resource": {"id": "dead", "name": "MyLA311 Service Request Data 2025"}},
    ]}
    sess = FakeSession(FakeResp(payload))
    assert socrata.la_311_datasets_by_year(session=sess) == {}


def test_la_311_by_year_is_none_on_malformed_payload():
    sess = FakeSession(FakeResp(_MALFORMED, status_code=200))
    assert socrata.la_311_datasets_by_year(session=sess) is None


def test_la_311_by_year_passes_app_token_header():
    payload = {"results": [{"resource": {"id": "2cy6-i7zn", "name": "MyLA311 Cases 2026"}}]}
    sess = FakeSession(FakeResp(payload))
    socrata.la_311_datasets_by_year(app_token="tok-1", session=sess)
    assert sess.calls[0]["headers"].get("X-App-Token") == "tok-1"


# --------------------------------------------------------------------------- #
# 7. LA 311 across the year boundary: one dataset per calendar year
# --------------------------------------------------------------------------- #
# The bug this guards: feed_series used to union only the NEWEST 'MyLA311 Cases
# {year}' with the Mar..Dec 2025 bridge file. Once 'MyLA311 Cases 2027' exists
# that is 2027 + 2025 with all of 2026 missing, so LA City Services can't form
# a 12-month baseline. Real catalog shape (2026-10-04): both items are
# type 'filter' views, ids 2cy6-i7zn (2026) and 73a2-6ar5 (Mar..Dec 2025).
_REGISTRY = Path(__file__).resolve().parent.parent / "docs" / "city" / "city_registry.resolved.json"
_BRIDGE_ITEM = {"resource": {"id": "73a2-6ar5", "type": "filter",
                             "name": "MyLA311 Cases March 2025 to December 2025"}}
_CASES_2026 = {"resource": {"id": "2cy6-i7zn", "type": "filter", "name": "MyLA311 Cases 2026"}}


def _la_311_feed():
    """The real LA 311 feed block from the resolved registry."""
    reg = json.loads(_REGISTRY.read_text())
    la = next(c for c in reg["cities"] if c["id"] == "la")
    return next(f for f in la["feeds"] if f.get("dataset_rotates_yearly"))


def _months(first, last):
    y, m = int(first[:4]), int(first[5:7])
    out = []
    while f"{y:04d}-{m:02d}" <= last:
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def _rows(first, last, n, *, partial_last_day=None):
    """Aggregation rows (with max(date) AS last) for complete months first..last.

    ``partial_last_day`` makes the final month stop on that day (still publishing).
    """
    rows = []
    months = _months(first, last)
    for i, mo in enumerate(months):
        day = calendar.monthrange(int(mo[:4]), int(mo[5:7]))[1]
        if partial_last_day and i == len(months) - 1:
            day = partial_last_day
        rows.append({"m": f"{mo}-01T00:00:00.000", "n": str(n),
                     "last": f"{mo}-{day:02d}T23:59:00.000"})
    return rows


class CatalogSession:
    """Answers the LA catalog and /resource/<id>.json by URL; unknown ids 404."""

    def __init__(self, catalog, datasets):
        self.catalog = catalog
        self.datasets = datasets
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": params or {}})
        if url == socrata._LA_CATALOG_URL:
            return self.catalog
        ds = url.rsplit("/", 1)[-1][: -len(".json")]
        if ds in self.datasets:
            canned = self.datasets[ds]
            return canned if isinstance(canned, FakeResp) else FakeResp(canned)
        return FakeResp({"error": True}, status_code=404, text="not found")

    def queried(self):
        return [c["url"].rsplit("/", 1)[-1][: -len(".json")]
                for c in self.calls if "/resource/" in c["url"]]

    def where_for(self, ds):
        return next(c["params"]["$where"] for c in self.calls
                    if c["url"].endswith(f"/resource/{ds}.json"))


def _catalog(*items):
    return FakeResp({"results": list(items), "resultSetSize": len(items)})


def _by_month(series):
    return {row["month"]: row["n"] for row in series}


def _score(series, as_of):
    return pulse.score_feed(series, polarity=-1, as_of=as_of, label="MyLA311 Cases",
                            dataset="2cy6-i7zn")


def test_la_311_registry_names_the_year_of_each_fallback_id():
    feed = _la_311_feed()
    assert (feed["dataset"], feed["dataset_year"]) == ("2cy6-i7zn", 2026)
    assert (feed["baseline_dataset"], feed["baseline_dataset_year"]) == ("73a2-6ar5", 2025)


def test_la_311_october_2026_is_unchanged():
    """today=2026-10-04 (as_of 2026-09, since 2023-08-01): 2026 + the 2025 bridge."""
    sess = CatalogSession(_catalog(_CASES_2026, _BRIDGE_ITEM), {
        "2cy6-i7zn": _rows("2026-01", "2026-10", 200, partial_last_day=4),
        "73a2-6ar5": _rows("2025-03", "2025-12", 190),
    })
    out = socrata.feed_series(_la_311_feed(), "data.lacity.org", since="2023-08-01",
                              session=sess, today=date(2026, 10, 4))

    assert sess.calls[0]["url"] == socrata._LA_CATALOG_URL
    assert sess.queried() == ["2cy6-i7zn", "73a2-6ar5"]   # nothing for 2023/2024
    assert [r["month"] for r in out] == _months("2025-03", "2026-09")  # Oct partial trimmed
    assert _by_month(out)["2025-12"] == 190 and _by_month(out)["2026-09"] == 200
    scored = _score(out, "2026-09")
    assert scored["status"] == "ok" and scored["recent_period"] == "2026-09"


def test_la_311_january_2027_before_the_2027_dataset_is_published(capsys):
    """today=2027-01-05, catalog has no 'MyLA311 Cases 2027' yet: use 2026 + 2025."""
    sess = CatalogSession(_catalog(_CASES_2026, _BRIDGE_ITEM), {
        "2cy6-i7zn": _rows("2026-01", "2026-12", 200),
        "73a2-6ar5": _rows("2025-03", "2025-12", 190),
    })
    out = socrata.feed_series(_la_311_feed(), "data.lacity.org", since="2023-11-01",
                              session=sess, today=date(2027, 1, 5))

    assert sess.queried() == ["2cy6-i7zn", "73a2-6ar5"]
    assert [r["month"] for r in out] == _months("2025-03", "2026-12")
    scored = _score(out, "2026-12")
    assert scored["status"] == "ok" and scored["recent_period"] == "2026-12"
    assert "'MyLA311 Cases 2027' is not in the LA catalog yet" in capsys.readouterr().err


def test_la_311_january_2027_with_2027_published_keeps_2026():
    """today=2027-01-05 with 'MyLA311 Cases 2027' live: 2027 + 2026 + 2025, never 2027 + 2025."""
    sess = CatalogSession(
        _catalog({"resource": {"id": "abcd-2027", "name": "MyLA311 Cases 2027"}},
                 _CASES_2026, _BRIDGE_ITEM),
        {
            "abcd-2027": _rows("2027-01", "2027-01", 30, partial_last_day=4),
            # The fake ignores $where, so these stray rows reach the parser: the
            # 2026 file must not contribute to 2025-12 or 2027-01.
            "2cy6-i7zn": (_rows("2025-12", "2025-12", 7) + _rows("2026-01", "2026-12", 200)
                          + _rows("2027-01", "2027-01", 9, partial_last_day=2)),
            "73a2-6ar5": _rows("2025-03", "2025-12", 190),
        },
    )
    out = socrata.feed_series(_la_311_feed(), "data.lacity.org", since="2023-11-01",
                              session=sess, today=date(2027, 1, 5))

    assert sess.queried() == ["abcd-2027", "2cy6-i7zn", "73a2-6ar5"]
    got = _by_month(out)
    assert all(got[m] == 200 for m in _months("2026-01", "2026-12"))   # 2026 is present
    assert got["2025-12"] == 190                 # from the bridge file only
    assert "2027-01" not in got                  # in progress -> trimmed, not scored
    # Each yearly dataset is asked for its own calendar year only.
    where_2026 = sess.where_for("2cy6-i7zn")
    assert "createddate >= '2026-01-01'" in where_2026
    assert "createddate < '2027-01-01'" in where_2026
    scored = _score(out, "2026-12")
    assert scored["status"] == "ok" and scored["recent_period"] == "2026-12"


def test_la_311_old_bug_shape_would_have_lost_the_baseline():
    """The pre-fix union (2027 + Mar..Dec 2025) cannot score December 2026."""
    old_union = socrata._merge_series([
        socrata._parse_rows(_rows("2027-01", "2027-01", 30)),
        socrata._parse_rows(_rows("2025-03", "2025-12", 190)),
    ])
    assert _score(old_union, "2026-12")["status"] == "insufficient_history"


def test_la_311_march_2029_resolves_every_prior_year_by_title():
    """Each year in the window comes from its own catalog title; 2025 is out of range."""
    years = {2026: "2cy6-i7zn", 2027: "aaaa-2027", 2028: "bbbb-2028", 2029: "cccc-2029"}
    items = [{"resource": {"id": ds, "name": f"MyLA311 Cases {y}"}} for y, ds in years.items()]
    sess = CatalogSession(_catalog(*items, _BRIDGE_ITEM), {
        "2cy6-i7zn": _rows("2026-01", "2026-12", 200),
        "aaaa-2027": _rows("2027-01", "2027-12", 210),
        "bbbb-2028": _rows("2028-01", "2028-12", 220),
        "cccc-2029": _rows("2029-01", "2029-03", 230, partial_last_day=2),
    })
    # as_of 2029-02 -> since 2026-01-01
    out = socrata.feed_series(_la_311_feed(), "data.lacity.org", since="2026-01-01",
                              session=sess, today=date(2029, 3, 2))

    assert sess.queried() == ["cccc-2029", "bbbb-2028", "aaaa-2027", "2cy6-i7zn"]
    assert [r["month"] for r in out] == _months("2026-01", "2029-02")
    assert _score(out, "2029-02")["status"] == "ok"


@pytest.mark.parametrize("today, as_of, since", [
    (date(2026, 10, 4), "2026-09", "2023-08-01"),
    (date(2027, 1, 5), "2026-12", "2023-11-01"),
])
def test_la_311_catalog_failure_falls_back_to_registry_ids(capsys, today, as_of, since):
    sess = CatalogSession(FakeResp({"err": "x"}, status_code=503, text="down"), {
        "2cy6-i7zn": _rows("2026-01", as_of, 200),
        "73a2-6ar5": _rows("2025-03", "2025-12", 190),
    })
    out = socrata.feed_series(_la_311_feed(), "data.lacity.org", since=since,
                              session=sess, today=today)

    catalog_calls = [c for c in sess.calls if c["url"] == socrata._LA_CATALOG_URL]
    assert len(catalog_calls) == 3                    # the usual retries, then fallback
    assert sess.queried() == ["2cy6-i7zn", "73a2-6ar5"]
    assert _score(out, as_of)["status"] == "ok"
    assert "falling back to the registry dataset ids" in capsys.readouterr().err


def test_la_311_catalog_beats_registry_id_but_never_replaces_the_bridge_file():
    """A catalog 2026 id wins over the registry's; a 'Cases 2025' item is ignored
    because 2025 belongs to the bridge file (no double counting)."""
    sess = CatalogSession(
        _catalog({"resource": {"id": "cases-2025", "name": "MyLA311 Cases 2025"}},
                 {"resource": {"id": "new2-2026", "name": "MyLA311 Cases 2026"}},
                 _BRIDGE_ITEM),
        {
            "new2-2026": _rows("2026-01", "2026-09", 200),
            "73a2-6ar5": _rows("2025-03", "2025-12", 190),
        },
    )
    socrata.feed_series(_la_311_feed(), "data.lacity.org", since="2023-08-01",
                        session=sess, today=date(2026, 10, 4))
    assert sess.queried() == ["new2-2026", "73a2-6ar5"]


def test_la_311_dataset_error_still_raises_after_retries():
    """A failing yearly dataset keeps the existing behavior: 3 attempts, then
    SocrataError (which fetch_city turns into fetch_error + the 'City data not
    refreshed' warning)."""
    sess = CatalogSession(_catalog(_CASES_2026, _BRIDGE_ITEM), {
        "2cy6-i7zn": _rows("2026-01", "2026-09", 200),
        "73a2-6ar5": FakeResp({"err": "x"}, status_code=503, text="down"),
    })
    with pytest.raises(socrata.SocrataError, match="HTTP 503"):
        socrata.feed_series(_la_311_feed(), "data.lacity.org", since="2023-08-01",
                            session=sess, today=date(2026, 10, 4))
    assert sess.queried().count("73a2-6ar5") == 3
