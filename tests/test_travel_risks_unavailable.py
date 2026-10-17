"""Travel tab: missing risk indicators must read "unavailable", not "none".

travel.state.gov's advisory TABLE (where the T/C/U/H/K/N/D/O/E risk
indicators are published as pills) answers 403 behind a bot challenge, and so
do the per-country advisory pages. The public Consular Affairs JSON feed
names the same indicators in each level sentence, and that path sets
``risks_available`` true. If that feed is also missing, fetch_advisories
falls back to the RSS feed, which carries the level in the title but no
indicator codes. Every RSS row ships ``risks: []``. The dashboard used to
print "No specific risk indicators" under all ~214 destinations (Level 4
included) and a terrorism count of 0. Both were fabricated readings of
missing data.

Pinned here, offline: the payload says ``risks_available: false`` in the
fallback (true when the table parsed), and both dashboards honour it by
saying "Risk indicators unavailable (source blocked)" and hiding the
terrorism counter, sub-tab and filter.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import fetch_advisories as fa

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures"
HTML_FIXTURE = (FIX / "advisories_sample.html").read_text(encoding="utf-8")
RSS_FIXTURE = (FIX / "advisories_rss_sample.xml").read_text(encoding="utf-8")


def _result(url, text, status):
    return fa.FetchResult(url=url, text=text, status=status,
                          nbytes=len(text or ""), attempts=1)


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr(fa.time, "sleep", lambda *_: None)


def _fake_get(html_status, api_text=None, api_status=503):
    """HTML and RSS are fixture-backed. The CA API defaults to a failed GET
    so the RSS-fallback tests stay on that path; pass ``api_text`` to exercise
    the API path. Nothing here contacts the network."""
    def _get(url, timeout=25, retries=fa.HTTP_RETRIES):
        if url == fa.ADVISORY_LIST_URL:
            if html_status == 200:
                return _result(url, HTML_FIXTURE, 200)
            return _result(url, None, html_status)
        if url == fa.ADVISORY_API_URL:
            if api_text is None:
                return _result(url, None, api_status)
            return _result(url, api_text, 200)
        if url == fa.ADVISORY_RSS_URL:
            return _result(url, RSS_FIXTURE, 200)
        raise AssertionError(f"unexpected URL {url}")
    return _get


# Trimmed from the live CA data API shape (Title / Summary / Updated / Link).
# Afghanistan's level sentence names every indicator the HTML pills use.
# Austria is a Level 1 with a later mention of crime that must not become a pill.
API_FIXTURE = json.dumps([
    {
        "Title": "Afghanistan - Level 4: Do Not Travel",
        "Link": "https://travel.state.gov/content/tsg_aem/us/en/home/international-travel/travel-advisories/destination.afg.html",
        "Category": ["AF"],
        "Summary": (
            "<p>Do not travel due to <b>civil unrest</b>, <b>crime, terrorism</b>, "
            "<b>risk of wrongful detention</b>, <b>kidnapping</b>, "
            "<b>natural disasters</b>, and <b>limited health facilities</b>.</p>"
        ),
        "Published": "2026-02-19T19:00:00-05:00",
        "Updated": "2026-02-19T19:00:00-05:00",
    },
    {
        "Title": "Austria - Level 1: Exercise Normal Precautions",
        "Link": "https://travel.state.gov/content/tsg_aem/us/en/home/international-travel/travel-advisories/destination.aut.html",
        "Category": ["AU"],
        "Summary": (
            "<p>Exercise normal precaution in Austria. Austria is generally a "
            "safe destination. Petty crime is most likely in tourist areas.</p>"
        ),
        "Updated": "2026-05-21T16:00:00Z",
    },
    {
        "Title": "Not a country advisory",
        "Link": "https://travel.state.gov/not-an-advisory.html",
        "Summary": "<p>Worldwide caution.</p>",
    },
])


def test_blocked_table_payload_says_risks_unavailable(monkeypatch, no_sleep):
    monkeypatch.setattr(fa, "_get", _fake_get(403))
    status: dict = {}
    payload = fa.fetch_live(status)
    assert payload["source"] == "rss-fallback"
    assert payload["fallback"] is True
    assert payload["fallback_reason"] == "html HTTP 403"
    assert payload["risks_available"] is False
    assert payload["advisories"] and all(a["risks"] == [] for a in payload["advisories"])
    assert status["fallback"] is True
    assert status["ok"] is True
    assert status["failure"] is None
    assert status["risks_available"] is False


def test_blocked_table_uses_cadata_api_and_restores_risks(monkeypatch, no_sleep, capsys, tmp_path):
    """HTML 403, CA API 200. Levels and indicator codes come from the official
    JSON feed, risks_available is true, and the run is a normal success — not
    the RSS fallback and not a red build."""
    monkeypatch.setattr(fa, "_get", _fake_get(403, api_text=API_FIXTURE))
    status: dict = {}
    payload = fa.fetch_live(status)
    assert payload["source"] == "cadata-api"
    assert payload["fallback"] is False
    assert payload["fallback_reason"] is None
    assert payload["risks_available"] is True
    assert status["ok"] is True
    assert status["fallback"] is False
    assert status["failure"] is None
    assert status["risks_available"] is True
    assert status["api"]["http_status"] == 200
    by = {a["name"]: a for a in payload["advisories"]}
    assert set(by) == {"Afghanistan", "Austria"}
    assert by["Afghanistan"]["level"] == 4
    assert by["Afghanistan"]["risks"] == ["U", "C", "T", "D", "K", "N", "H"]
    # 19:00-05:00 is the next UTC day, matching the HTML table's issue date.
    assert by["Afghanistan"]["date"] == "2026-02-20"
    assert by["Afghanistan"]["url"].endswith("destination.afg.html")
    assert by["Austria"]["level"] == 1
    assert by["Austria"]["risks"] == []
    assert by["Austria"]["date"] == "2026-05-21"

    out = tmp_path / "data-travel.json"
    assert fa.main(["--out", str(out)]) == 0
    captured = capsys.readouterr()
    assert "::error" not in captured.out
    assert "::warning" not in captured.out
    written = json.loads(out.read_text())
    assert written["source"] == "cadata-api"
    assert written["risks_available"] is True
    assert written["fallback"] is False


def test_html_table_wins_over_the_api(monkeypatch, no_sleep):
    """A parsed HTML table is the primary path. The API is not consulted."""
    def _get(url, timeout=25, retries=fa.HTTP_RETRIES):
        if url == fa.ADVISORY_API_URL:
            raise AssertionError("CA API must not be fetched when HTML parsed")
        return _fake_get(200)(url, timeout, retries)
    monkeypatch.setattr(fa, "_get", _get)
    status: dict = {}
    payload = fa.fetch_live(status)
    assert payload["source"] == "html"
    assert payload["risks_available"] is True
    assert "api" not in status


def test_api_without_indicator_words_does_not_claim_risks(monkeypatch, no_sleep):
    """Levels from the API with no named indicator stay risks_available false,
    so an empty risks list is not shown as 'none'."""
    bare = json.dumps([{
        "Title": "France - Level 2: Exercise Increased Caution",
        "Link": "https://travel.state.gov/france.html",
        "Summary": "<p>Exercise increased caution in France.</p>",
        "Updated": "2026-01-02T00:00:00Z",
    }])
    monkeypatch.setattr(fa, "_get", _fake_get(403, api_text=bare))
    payload = fa.fetch_live({})
    assert payload["source"] == "cadata-api"
    assert payload["fallback"] is False
    assert payload["risks_available"] is False
    assert payload["advisories"][0]["level"] == 2
    assert payload["advisories"][0]["risks"] == []


def test_unusable_api_falls_through_to_rss(monkeypatch, no_sleep, tmp_path):
    """A JSON body that is not a country list leaves the RSS fallback in
    place, and that fallback still exits 0."""
    monkeypatch.setattr(fa, "_get", _fake_get(403, api_text="not-json"))
    out = tmp_path / "data-travel.json"
    assert fa.main(["--out", str(out)]) == 0
    payload = json.loads(out.read_text())
    assert payload["source"] == "rss-fallback"
    assert payload["risks_available"] is False
    assert payload["fallback"] is True
    assert payload["advisories"]


def test_parsed_table_payload_says_risks_available(monkeypatch, no_sleep):
    monkeypatch.setattr(fa, "_get", _fake_get(200))
    status: dict = {}
    payload = fa.fetch_live(status)
    assert payload["source"] == "html"
    assert payload["fallback"] is False
    assert payload["fallback_reason"] is None
    assert payload["risks_available"] is True
    assert status["fallback"] is False
    assert status["ok"] is True
    by = {a["name"]: a for a in payload["advisories"]}
    assert "T" in by["Afghanistan"]["risks"]


def _both_down(url, timeout=25, retries=fa.HTTP_RETRIES):
    return _result(url, None, 403)


def test_rss_fallback_is_a_warning_and_exits_zero(monkeypatch, no_sleep, capsys, tmp_path):
    """The 2026-10-15 tests.yml jobs got HTTP 403 from the advisory page and
    a usable RSS list. That is a warning, not a failed run."""
    monkeypatch.setattr(fa, "_get", _fake_get(403))
    out = tmp_path / "data-travel.json"
    assert fa.main(["--out", str(out)]) == 0
    captured = capsys.readouterr()
    assert "::warning title=Travel advisories degraded::" in captured.out
    assert "fallback=true" in captured.out
    assert "::error" not in captured.out
    payload = json.loads(out.read_text())
    assert payload["fallback"] is True
    assert payload["source"] == "rss-fallback"
    assert payload["risks_available"] is False
    assert payload["advisories"]
    status = json.loads((tmp_path / "data-travel-fetch-status.json").read_text())
    assert status["fallback"] is True
    assert status["ok"] is True
    assert status["failure"] is None


def test_no_data_from_either_source_fails_and_preserves(monkeypatch, no_sleep, capsys, tmp_path):
    monkeypatch.setattr(fa, "_get", _both_down)
    out = tmp_path / "data-travel.json"
    prior = {"generated_at": "2026-10-01T00:00:00Z",
             "advisories": [{"name": "Kept", "level": 2, "risks": ["C"],
                             "date": "2026-01-01", "url": "https://example.test/kept"}]}
    out.write_text(json.dumps(prior))
    assert fa.main(["--out", str(out)]) == 1
    assert json.loads(out.read_text()) == prior
    captured = capsys.readouterr()
    assert "::error title=Travel advisories not refreshed::" in captured.out
    status = json.loads((tmp_path / "data-travel-fetch-status.json").read_text())
    assert status["ok"] is False
    assert status["failure"] == "no-data"
    assert status["fallback"] is False


def test_malformed_rows_fail_and_are_not_written(monkeypatch, no_sleep, capsys, tmp_path):
    monkeypatch.setattr(fa, "_get", _fake_get(403))
    monkeypatch.setattr(
        fa, "advisories_from_rss",
        lambda xml: [{"name": "Nope", "level": 9, "risks": "T",
                      "date": "yesterday", "url": "notaurl"}],
    )
    out = tmp_path / "data-travel.json"
    prior = {"generated_at": "2026-10-14T00:00:00Z", "advisories": [{"name": "Kept"}]}
    out.write_text(json.dumps(prior))
    assert fa.main(["--out", str(out)]) == 1
    assert json.loads(out.read_text()) == prior
    captured = capsys.readouterr()
    assert "::error title=Travel advisories malformed::" in captured.out
    assert "::warning" not in captured.out
    status = json.loads((tmp_path / "data-travel-fetch-status.json").read_text())
    assert status["ok"] is False
    assert status["failure"] == "malformed"
    assert status["fallback"] is False
    assert "level" in status["malformed"]


def test_malformed_html_falls_back_to_well_formed_rss(monkeypatch, no_sleep, capsys, tmp_path):
    monkeypatch.setattr(fa, "_get", _fake_get(200))
    monkeypatch.setattr(fa, "parse_advisory_table", lambda *a, **k: [{"name": ""}])
    out = tmp_path / "data-travel.json"
    assert fa.main(["--out", str(out)]) == 0
    payload = json.loads(out.read_text())
    assert payload["fallback"] is True
    assert payload["fallback_reason"].startswith("html rows malformed")
    assert payload["advisories"]
    captured = capsys.readouterr()
    assert "::warning title=Travel advisories degraded::" in captured.out
    assert "::error" not in captured.out


def test_invalid_rss_with_blocked_html_is_no_data(monkeypatch, no_sleep, tmp_path):
    def _get(url, timeout=25, retries=fa.HTTP_RETRIES):
        if url == fa.ADVISORY_LIST_URL:
            return _result(url, None, 403)
        return _result(url, "<not xml", 200)
    monkeypatch.setattr(fa, "_get", _get)
    out = tmp_path / "data-travel.json"
    assert fa.main(["--out", str(out)]) == 1
    assert not out.exists()
    status = json.loads((tmp_path / "data-travel-fetch-status.json").read_text())
    assert status["failure"] == "no-data"
    assert status["fallback"] is False


def test_offline_mock_never_calls_requests(monkeypatch, no_sleep, tmp_path):
    """tests.yml sets ALPINE_ADVISORIES_MOCK so the dashboard build cannot
    reach travel.state.gov or the CA data API. The double is an HTML 403,
    an empty API list, and the RSS fixture, so the build stays on the
    RSS fallback without opening a socket."""
    monkeypatch.setenv(fa.ADVISORIES_MOCK_ENV, "1")

    def boom(*_a, **_k):
        raise AssertionError("live HTTP to the State Dept page")

    monkeypatch.setattr(fa.requests, "get", boom)
    html = fa._get(fa.ADVISORY_LIST_URL)
    rss = fa._get(fa.ADVISORY_RSS_URL)
    api = fa._get(fa.ADVISORY_API_URL)
    assert html.status == 403 and not html.ok
    assert "Just a moment" in html.body_snippet
    assert rss.ok and "<rss" in rss.text
    assert api.ok and api.text == "[]"
    out = tmp_path / "data-travel.json"
    assert fa.main(["--out", str(out)]) == 0
    payload = json.loads(out.read_text())
    assert payload["fallback"] is True
    assert payload["risks_available"] is False
    assert payload["advisories"]


def test_tests_workflow_mocks_state_dept_and_pages_does_not():
    tests = (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
    pages = (ROOT / ".github" / "workflows" / "pages.yml").read_text(encoding="utf-8")
    assert f"{fa.ADVISORIES_MOCK_ENV}: \"1\"" in tests
    assert fa.ADVISORIES_MOCK_ENV not in pages


# Trimmed from the live TAsTWs feed (2026-10-04). An item's structured fields
# are the title, link, pubDate, a level category, a FIPS country code ("AF")
# and the item type; the risks exist only as free prose in <description>.
LIVE_SHAPE_ITEM = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/"><channel>
<item>
  <title>Afghanistan - Level 4: Do Not Travel</title>
  <link>https://travel.state.gov/content/travel/en/traveladvisories/traveladvisories/afghanistan-travel-advisory.html</link>
  <pubDate>Fri, 20 Feb 2026</pubDate>
  <description>&lt;p&gt;Do not travel to Afghanistan due to &lt;b&gt;civil unrest, crime,
  terrorism, risk of wrongful detention, kidnapping, natural disasters, and limited
  health facilities.&lt;/b&gt;&lt;/p&gt;</description>
  <category>Level 4: Do Not Travel</category>
  <category>AF</category>
  <category>advisory</category>
  <dc:identifier>AF,advisory</dc:identifier>
</item>
</channel></rss>"""


def test_rss_items_really_carry_no_indicator_codes():
    """Why the fallback cannot recover them: besides the level, the only
    structured per-item fields are a FIPS country code and the item type."""
    cats = re.findall(r"<category>([^<]*)</category>", LIVE_SHAPE_ITEM)
    assert cats == ["Level 4: Do Not Travel", "AF", "advisory"]
    rows = fa.advisories_from_rss(LIVE_SHAPE_ITEM)
    assert rows == [{"name": "Afghanistan", "level": 4, "risks": [], "date": "2026-02-20",
                     "url": fa.build_country_url("Afghanistan")}]


@pytest.mark.parametrize("rel,list_fn,ov_fn", [
    ("app.py", "renderTravelListV1", "renderTravelOverviewV1"),
])
def test_dashboard_says_unavailable_and_hides_the_terror_counter(rel, list_fn, ov_fn):
    src = (ROOT / rel).read_text(encoding="utf-8")
    assert "function travelRisksAvailable(t){" in src
    assert ("t.fallback !== true && t.risks_available !== false "
            "&& t.source !== 'rss-fallback'") in src
    assert "'Risk indicators unavailable (source blocked)'" in src

    ov = src[src.index(f"function {ov_fn}("):]
    ov = ov[:ov.index("\nfunction ")]
    # The terrorism stat card is emitted only when indicators are known.
    i = ov.index("travel-stat--terror")
    assert "(risksOk" in ov[max(0, i - 200):i], "terrorism counter is not gated on risksOk"

    lst = src[src.index(f"function {list_fn}("):]
    lst = lst[:lst.index("\nfunction ")]
    assert "const chips = !risksOk" in lst
    assert "TRAVEL_RISKS_UNAVAILABLE" in lst
    assert "terrorToggle.classList.toggle('hidden', isTerror || !risksOk);" in lst
    assert "if (risksOk && (state.travelTerrorOnly || isTerror)" in lst

    # The Terrorism sub-tab is hidden and a stale 'terror' sub-view falls back.
    assert ".travel-subtab[data-travelsub=\"terror\"]" in src
    assert "if (!risksOk && sub === 'terror') { sub = 'overview';" in src
