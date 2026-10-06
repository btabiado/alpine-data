"""Tests for the MUFON / UAP (NUFORC) feed: loud failure, honest freshness,
and no frozen partial months.

Background: since ~2026-06-10 nuforc.org answers every non-browser request
with a Cloudflare managed challenge (HTTP 403, ``cf-mitigated: challenge``,
"Just a moment..."). The fetcher kept serving its committed month cache, the
page kept rebuilding with a fresh ``generated_at``, and the only alarm was a
vague "NUFORC unreachable". Meanwhile the June 2026 cache — written mid-month
with 40 rows — was being served as if June were complete.

Every network call here is mocked; nothing touches nuforc.org.
"""
from __future__ import annotations

import io
import json
import re
import urllib.error
from datetime import datetime, timezone
from email.message import Message
from pathlib import Path

import pytest

import fetch_mufon as fm


ROOT = Path(__file__).resolve().parent.parent
REAL_CACHE = ROOT / "data" / ".stale"

NONCE_HTML = ('<input type="hidden" id="wdtNonceFrontendServerSide_1" '
              'name="wdtNonceFrontendServerSide_1" value="abcdef1234">')
CF_BODY = (b'<!DOCTYPE html><html lang="en-US"><head><title>Just a moment...'
           b'</title></head><body>challenge</body></html>')


# ------------------------------------------------------------- helpers ---

def _fixed_now(monkeypatch, iso: str) -> None:
    """Pin fetch_mufon's clock. The module does ``from datetime import
    datetime``, so swapping that name pins every now() it makes."""
    pinned = datetime.fromisoformat(iso).replace(tzinfo=timezone.utc)

    class _Pinned(datetime):
        @classmethod
        def now(cls, tz=None):
            return pinned if tz is not None else pinned.replace(tzinfo=None)

    monkeypatch.setattr(fm, "datetime", _Pinned)
    monkeypatch.setattr(fm.time, "sleep", lambda *_a, **_k: None)


def _row(occurred: str, reported: str, state: str = "CA",
         city: str = "Fresno", shape: str = "Light") -> list:
    return ["<a href='/sighting/?id=1'>Open</a>", occurred, city, state, "USA",
            shape, "summary", reported, None, None]


def _month_payload(ym: str, n: int, reported: str | None = None) -> dict:
    y, m = ym[:4], ym[4:]
    rows = [_row(f"{m}/{(i % 27) + 1:02d}/{y} 21:00",
                 reported or f"{m}/{(i % 27) + 1:02d}/{y}") for i in range(n)]
    return {"draw": "1", "recordsTotal": n, "recordsFiltered": n, "data": rows}


def _write_cache(cache_dir: Path, ym: str, payload: dict) -> Path:
    p = cache_dir / f"nuforc_subndx_{ym}.json"
    p.write_text(json.dumps(payload))
    return p


def _http_error(code: int, body: bytes, headers: dict[str, str]):
    msg = Message()
    for k, v in headers.items():
        msg[k] = v
    return urllib.error.HTTPError("https://nuforc.org/x", code, "Forbidden",
                                  msg, io.BytesIO(body))


# ------------------------------------- 1. Cloudflare challenge detection ---

def test_http_fetch_names_cloudflare_challenge(monkeypatch):
    def boom(req, timeout=60):
        raise _http_error(403, CF_BODY, {"Server": "cloudflare",
                                         "cf-mitigated": "challenge"})
    monkeypatch.setattr(fm.urllib.request, "urlopen", boom)
    text, cause = fm._http_fetch("https://nuforc.org/subndx/?id=e202609")
    assert text is None
    assert cause == "Cloudflare challenge (403)"


def test_http_fetch_detects_challenge_from_title_alone(monkeypatch):
    def boom(req, timeout=60):
        raise _http_error(403, CF_BODY, {"Server": "cloudflare"})
    monkeypatch.setattr(fm.urllib.request, "urlopen", boom)
    assert fm._http_fetch("https://nuforc.org/")[1] == "Cloudflare challenge (403)"


def test_plain_403_is_not_reported_as_cloudflare(monkeypatch):
    def boom(req, timeout=60):
        raise _http_error(403, b"<html><title>Forbidden</title></html>", {})
    monkeypatch.setattr(fm.urllib.request, "urlopen", boom)
    assert fm._http_fetch("https://nuforc.org/")[1] == "HTTP 403"


def test_http_get_text_keeps_its_quiet_contract(monkeypatch):
    def boom(req, timeout=60):
        raise _http_error(403, CF_BODY, {"cf-mitigated": "challenge"})
    monkeypatch.setattr(fm.urllib.request, "urlopen", boom)
    assert fm._http_get_text("https://nuforc.org/") is None


def test_challenged_bootstrap_degrades_to_cache_and_records_cause(
        monkeypatch, tmp_path):
    _fixed_now(monkeypatch, "2026-10-04T03:00:00")
    for ym in ("202605", "202604"):
        _write_cache(tmp_path, ym, _month_payload(ym, 5,
                                                  reported="06/09/2026"))
    calls = []

    def fake_fetch(url, **kw):
        calls.append(url)
        return None, "Cloudflare challenge (403)"
    monkeypatch.setattr(fm, "_http_fetch", fake_fetch)

    out = fm._fetch_nuforc_live(months_back=7, cache_dir=tmp_path)
    meta = out["meta"]
    assert meta["cloudflare_challenge"] is True
    assert meta["failure_cause"].startswith("Cloudflare challenge (403) on ")
    assert meta["months_refreshed"] == 0
    assert meta["months_cached"] == 2 and len(out["rows"]) == 10
    # One request proves the gate; it must not be retried once per month.
    assert len(calls) == 1


def test_challenge_on_ajax_post_also_stops_the_network(monkeypatch, tmp_path):
    _fixed_now(monkeypatch, "2026-10-04T03:00:00")
    monkeypatch.setattr(fm, "_http_fetch", lambda url, **kw: (NONCE_HTML, None))
    posts = []

    def fake_month(ym, nonce, timeout=60):
        posts.append(ym)
        return None, "Cloudflare challenge (403)"
    monkeypatch.setattr(fm, "_nuforc_fetch_month_ex", fake_month)

    meta = fm._fetch_nuforc_live(months_back=6, cache_dir=tmp_path)["meta"]
    assert posts == ["202610"]
    assert meta["cloudflare_challenge"] is True
    assert "admin-ajax" in meta["failure_cause"]
    assert meta["network_disabled"] is True


# ----------------------------------------------- 2. the loud annotation ---

def test_annotation_names_cloudflare_and_frozen_date(monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    meta = {"months_refreshed": 0, "months_cached": 140,
            "cloudflare_challenge": True,
            "failure_cause": "Cloudflare challenge (403) on "
                             "https://nuforc.org/subndx/?id=e202610",
            "incomplete_months_served": ["202606"]}
    msg = fm._report_nuforc_refresh(meta, "2026-06-09",
                                    today=datetime(2026, 10, 4))
    out = capsys.readouterr()
    lines = [l for l in out.out.splitlines()
             if l.startswith("::warning title=MUFON not refreshed::")]
    assert len(lines) == 1, out.out
    ann = lines[0]
    assert "Cloudflare challenge (403)" in ann
    assert "frozen through 2026-06-09 (117 days old)" in ann
    assert "202606" in ann
    assert msg and "[MUFON-NOT-REFRESHED]" in out.err


def test_annotation_escapes_workflow_command_characters(monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    fm._report_nuforc_refresh(
        {"months_refreshed": 0, "failure_cause": "100% broken\r\nsecond line"},
        "2026-06-09", today=datetime(2026, 10, 4))
    ann = [l for l in capsys.readouterr().out.splitlines()
           if l.startswith("::warning")]
    assert len(ann) == 1
    assert "100%25 broken%0D%0Asecond line" in ann[0]


def test_no_workflow_command_outside_actions(monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    msg = fm._report_nuforc_refresh(
        {"months_refreshed": 0, "failure_cause": "HTTP 500"}, "2026-06-09")
    out = capsys.readouterr()
    assert msg is not None
    assert "::warning" not in out.out
    assert "[MUFON-NOT-REFRESHED]" in out.err


def test_no_alert_when_a_month_was_refreshed(monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert fm._report_nuforc_refresh({"months_refreshed": 1}, "2026-10-03") is None
    assert "::warning" not in capsys.readouterr().out


def test_zero_months_fetched_trips_the_alert_even_when_reachable(
        monkeypatch, tmp_path, capsys):
    """The old gap: bootstrap OK, AJAX answers 200 with no rows (stale or
    rejected nonce). months_pulled counts those empty answers, so the only
    other check (too MANY months) can never fire, and network_disabled is
    False — nothing was raised at all."""
    _fixed_now(monkeypatch, "2026-10-04T03:00:00")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.setattr(fm, "_http_fetch", lambda url, **kw: (NONCE_HTML, None))
    monkeypatch.setattr(fm, "_nuforc_fetch_month_ex",
                        lambda ym, nonce, timeout=60: ({"data": []}, None))

    meta = fm._fetch_nuforc_live(months_back=2, cache_dir=tmp_path)["meta"]
    assert meta["months_pulled"] == 2 and meta["months_refreshed"] == 0
    assert meta["network_disabled"] is False
    assert fm._emit_nuforc_health_marker(meta) is None  # the pre-existing check
    msg = fm._report_nuforc_refresh(meta, "2026-06-09",
                                    today=datetime(2026, 10, 4))
    assert msg and "0 rows" in msg
    assert "::warning title=MUFON not refreshed::" in capsys.readouterr().out


def test_main_emits_the_annotation_end_to_end(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    _fixed_now(monkeypatch, "2026-10-04T03:00:00")
    rows = fm._nuforc_parse_data_rows(_month_payload("202606", 4)["data"])
    meta = {"months_pulled": 0, "months_404": 0, "months_cached": 1,
            "months_refreshed": 0, "months_skipped_offline": 4,
            "network_disabled": True, "cloudflare_challenge": True,
            "failure_cause": "Cloudflare challenge (403) on "
                             "https://nuforc.org/subndx/?id=e202610",
            "incomplete_months_served": ["202606"],
            "wall_clock_sec": 0.1, "stopped_reason": "x"}
    monkeypatch.setattr(fm, "_fetch_planetsig_rows", lambda: ([], None, None))
    monkeypatch.setattr(fm, "_fetch_nuforc_live",
                        lambda months_back=144: {"rows": rows, "meta": meta})
    out_path = tmp_path / "data-mufon.json"
    assert fm.main(["--out", str(out_path)]) == 0
    out = capsys.readouterr().out
    assert "::warning title=MUFON not refreshed::" in out
    assert "Cloudflare challenge (403)" in out
    # The generic "degraded" marker no longer duplicates it.
    assert "MUFON/NUFORC feed degraded" not in out
    payload = json.loads(out_path.read_text())
    assert payload["data_through"] == payload["date_range"][1]


def test_main_alerts_when_every_source_failed(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    out_path = tmp_path / "data-mufon.json"
    out_path.write_text(json.dumps({"total_records": 5000,
                                    "date_range": ["1906-11-11", "2026-06-09"],
                                    "generated_at": "2026-10-04T00:00:00Z"}))
    monkeypatch.setattr(fm, "fetch_all", lambda **kw: None)
    assert fm.main(["--out", str(out_path)]) == 1
    assert "::warning title=MUFON not refreshed::" in capsys.readouterr().out
    prior = json.loads(out_path.read_text())
    assert prior["_stale"] is True
    assert prior["data_through"] == "2026-06-09"
    assert prior["live_refresh"]["ok"] is False


# ----------------------------------------- 3. honest freshness payload ---

def _live_result(n_rows: int, refreshed: int, cf: bool = False) -> dict:
    rows = fm._nuforc_parse_data_rows(_month_payload("202606", n_rows)["data"])
    return {"rows": rows, "meta": {
        "months_pulled": refreshed, "months_404": 0, "months_cached": 1,
        "months_refreshed": refreshed, "months_skipped_offline": 0,
        "network_disabled": cf, "cloudflare_challenge": cf,
        "failure_cause": "Cloudflare challenge (403) on x" if cf else None,
        "incomplete_months_served": [], "wall_clock_sec": 0.0,
        "stopped_reason": "x"}}


def test_cache_only_run_is_marked_stale_and_anchored_to_data(monkeypatch):
    _fixed_now(monkeypatch, "2026-10-04T03:00:00")
    monkeypatch.setattr(fm, "_fetch_planetsig_rows", lambda: ([], None, None))
    monkeypatch.setattr(fm, "_fetch_nuforc_live",
                        lambda months_back=144: _live_result(6, 0, cf=True))
    p = fm.fetch_all(months_back=6)
    assert p["generated_at"].startswith("2026-10-04")   # build clock
    assert p["data_through"] == p["date_range"][1] == "2026-06-06"
    assert p["_stale"] is True
    assert p["recent_buckets_anchor"] == "2026-06-06"
    # Anchored to the data, the windows count the sightings on file instead
    # of reading 0 "in the last 30 days" as if nobody saw anything.
    assert p["recent_buckets"]["CA"]["30d"] == 6
    assert p["live_refresh"]["ok"] is False
    assert p["live_refresh"]["blocked_by"] == "cloudflare_challenge"
    assert "Cloudflare challenge (403)" in p["live_refresh"]["cause"]


def test_fresh_run_is_not_stale_and_anchors_to_today(monkeypatch):
    _fixed_now(monkeypatch, "2026-10-04T03:00:00")
    monkeypatch.setattr(fm, "_fetch_planetsig_rows", lambda: ([], None, None))
    monkeypatch.setattr(fm, "_fetch_nuforc_live",
                        lambda months_back=144: _live_result(6, 2))
    p = fm.fetch_all(months_back=6)
    assert "_stale" not in p
    assert p["recent_buckets_anchor"] == "2026-10-04"
    assert p["live_refresh"] == {"ok": True, "months_refreshed": 2,
                                 "cause": None, "blocked_by": None,
                                 "attempted_at": p["generated_at"]}


# -------------------------------------- 4. partial months are refetched ---

def test_committed_june_2026_cache_is_recognised_as_partial():
    """The real file: 40 rows, written 2026-06-10, newest report 06/09."""
    june = REAL_CACHE / "nuforc_subndx_202606.json"
    may = REAL_CACHE / "nuforc_subndx_202605.json"
    if not june.exists() or not may.exists():
        pytest.skip("committed NUFORC cache not present")
    assert fm._month_cache_complete(json.loads(june.read_text()), "202606") is False
    assert fm._month_cache_complete(json.loads(may.read_text()), "202605") is True


def test_month_cache_complete_uses_the_fetch_stamp_when_present():
    p = _month_payload("202607", 3, reported="07/14/2026")
    p[fm.CACHE_FETCHED_AT_KEY] = "2026-07-15T10:00:00Z"
    assert fm._month_cache_complete(p, "202607") is False
    p[fm.CACHE_FETCHED_AT_KEY] = "2026-08-01T00:30:00Z"
    assert fm._month_cache_complete(p, "202607") is True
    # December rolls the year.
    d = _month_payload("202512", 1, reported="12/31/2025")
    assert fm._month_cache_complete(d, "202512") is False
    d[fm.CACHE_FETCHED_AT_KEY] = "2026-01-01T00:00:00Z"
    assert fm._month_cache_complete(d, "202512") is True


def test_partial_month_outside_refresh_window_is_refetched(monkeypatch, tmp_path):
    _fixed_now(monkeypatch, "2026-10-04T03:00:00")
    # June cached mid-month (legacy file, no stamp), May cached after it ended.
    _write_cache(tmp_path, "202606", _month_payload("202606", 4,
                                                    reported="06/09/2026"))
    _write_cache(tmp_path, "202605", _month_payload("202605", 5,
                                                    reported="06/09/2026"))
    monkeypatch.setattr(fm, "_http_fetch", lambda url, **kw: (NONCE_HTML, None))
    requested = []

    def fake_month(ym, nonce, timeout=60):
        requested.append(ym)
        return _month_payload(ym, 9, reported="09/30/2026"), None
    monkeypatch.setattr(fm, "_nuforc_fetch_month_ex", fake_month)

    out = fm._fetch_nuforc_live(months_back=6, cache_dir=tmp_path)
    # 202610/202609 = current/prior; 202608/202607 uncached; 202606 PARTIAL.
    assert "202606" in requested
    assert "202605" not in requested          # complete month: cache only
    june = json.loads((tmp_path / "nuforc_subndx_202606.json").read_text())
    assert len(june["data"]) == 9              # backfilled, no longer 4
    assert june[fm.CACHE_FETCHED_AT_KEY].startswith("2026-10-04")
    assert fm._month_cache_complete(june, "202606") is True
    assert out["meta"]["incomplete_months_served"] == []
    assert out["meta"]["months_refreshed"] == 5


def test_partial_month_falls_back_to_cache_when_refetch_fails(
        monkeypatch, tmp_path):
    _fixed_now(monkeypatch, "2026-10-04T03:00:00")
    _write_cache(tmp_path, "202606", _month_payload("202606", 4,
                                                    reported="06/09/2026"))
    monkeypatch.setattr(fm, "_http_fetch",
                        lambda url, **kw: (None, "Cloudflare challenge (403)"))
    out = fm._fetch_nuforc_live(months_back=6, cache_dir=tmp_path)
    assert len(out["rows"]) == 4               # served, not dropped
    assert out["meta"]["incomplete_months_served"] == ["202606"]
    # And the partial file is left exactly as it was for a later retry.
    june = json.loads((tmp_path / "nuforc_subndx_202606.json").read_text())
    assert fm.CACHE_FETCHED_AT_KEY not in june and len(june["data"]) == 4


def test_current_month_cached_while_network_down_is_still_served(
        monkeypatch, tmp_path):
    """Regression: the first month of the window used to be skipped outright
    when the bootstrap failed, even with a cached copy on disk."""
    _fixed_now(monkeypatch, "2026-10-04T03:00:00")
    _write_cache(tmp_path, "202610", _month_payload("202610", 3,
                                                    reported="10/03/2026"))
    monkeypatch.setattr(fm, "_http_fetch", lambda url, **kw: (None, "HTTP 500"))
    out = fm._fetch_nuforc_live(months_back=1, cache_dir=tmp_path)
    assert len(out["rows"]) == 3
    assert out["meta"]["failure_cause"].startswith("HTTP 500")


def test_new_cache_files_carry_the_fetch_stamp(monkeypatch, tmp_path):
    _fixed_now(monkeypatch, "2026-10-04T03:00:00")
    monkeypatch.setattr(fm, "_http_fetch", lambda url, **kw: (NONCE_HTML, None))
    monkeypatch.setattr(fm, "_nuforc_fetch_month_ex",
                        lambda ym, nonce, timeout=60:
                        (_month_payload(ym, 2, reported="10/03/2026"), None))
    fm._fetch_nuforc_live(months_back=1, cache_dir=tmp_path)
    cur = json.loads((tmp_path / "nuforc_subndx_202610.json").read_text())
    assert cur[fm.CACHE_FETCHED_AT_KEY] == "2026-10-04T03:00:00Z"
    # Fetched during October, so October stays incomplete until November.
    assert fm._month_cache_complete(cur, "202610") is False


# ------------------------------------------- 5. dashboard freshness (V8) ---

def _template_js(rel: str) -> str:
    src = (ROOT / rel).read_text(encoding="utf-8")
    marker = 'HTML_TEMPLATE = r"""'
    return src[src.index(marker) + len(marker):]


def _extract_function(js: str, name: str) -> str:
    m = re.search(r"^function %s\s*\(" % re.escape(name), js, re.M)
    assert m, f"function {name}() not found"
    i = js.index("{", m.end() - 1)
    depth = 0
    for j in range(i, len(js)):
        if js[j] == "{":
            depth += 1
        elif js[j] == "}":
            depth -= 1
            if depth == 0:
                return js[m.start():j + 1]
    raise AssertionError("unbalanced braces")  # pragma: no cover


@pytest.fixture(scope="module", params=["app.py"])
def mufon_freshness(request):
    py_mini_racer = pytest.importorskip(
        "py_mini_racer", reason="V8 needed to execute the shipped JS")
    js = _template_js(request.param)
    ctx = py_mini_racer.MiniRacer()
    bodies = "\n".join(_extract_function(js, n) for n in
                       ("freshnessDayUTC", "freshnessYmd", "fDay",
                        "mufonFreshness"))
    ctx.eval("var DATA = {};\n" + bodies + """
    function __run(mu){ DATA = {mufon: mu}; const r = mufonFreshness();
                        return r == null ? null : r; }""")
    return lambda mu: ctx.call("__run", mu)


def test_dashboard_freshness_is_date_range_end_not_build_time(mufon_freshness):
    r = mufon_freshness({"generated_at": "2026-10-04T03:00:00Z",
                         "date_range": ["1906-11-11", "2026-06-09"]})
    assert r["date"] == "2026-06-09"


def test_dashboard_freshness_prefers_data_through(mufon_freshness):
    r = mufon_freshness({"generated_at": "2026-10-04T03:00:00Z",
                         "data_through": "2026-06-09",
                         "date_range": ["1906-11-11", "2026-06-09"],
                         "live_refresh": {"ok": True}})
    assert r["date"] == "2026-06-09"
    assert r["label"] == "data through"


def test_dashboard_says_not_refreshed_in_text(mufon_freshness):
    r = mufon_freshness({"generated_at": "2026-10-04T03:00:00Z",
                         "data_through": "2026-06-09",
                         "date_range": ["1906-11-11", "2026-06-09"],
                         "_stale": True,
                         "live_refresh": {"ok": False,
                                          "cause": "Cloudflare challenge (403) on x"}})
    assert r["date"] == "2026-06-09"
    assert r["label"].startswith("not refreshed")
    assert "Cloudflare challenge (403)" in r["title"]


def test_dashboard_freshness_null_without_a_data_date(mufon_freshness):
    assert mufon_freshness({"generated_at": "2026-10-04T03:00:00Z",
                            "date_range": [None, None]}) is None


@pytest.mark.parametrize("rel", ["app.py"])
def test_map_and_trend_notes_use_the_real_bucket_anchor(rel):
    js = _template_js(rel)
    assert "historical mirror cutoff" not in js
    for fn in ("renderMufonMap", "renderMufonTrend"):
        body = _extract_function(js, fn)
        assert "m.recent_buckets_anchor ||" in body, fn
    # The "they ARE the last 30/60/90/365 days from now" claim must be gated
    # on the payload saying this build actually refreshed.
    trend = _extract_function(js, "renderMufonTrend")
    i = trend.index("they ARE the last 30/60/90/365 days from now")
    assert "m._stale" in trend[max(0, i - 900):i]
