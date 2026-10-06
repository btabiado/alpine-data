"""Travel tab: missing risk indicators must read "unavailable", not "none".

travel.state.gov's advisory TABLE (the only place the T/C/U/H/K/N/D/O/E risk
indicators are published) answers 403 behind a bot challenge, and so do the
per-country advisory pages. fetch_advisories falls back to the RSS feed, which
carries the level, a FIPS country code and prose, but no indicator codes. So
every row ships ``risks: []`` and the dashboard used to print "No specific
risk indicators" under all ~214 destinations (Level 4 included) and a
terrorism count of 0. Both were fabricated readings of missing data.

Pinned here, offline: the payload says ``risks_available: false`` in the
fallback (true when the table parsed), and both dashboards honour it by
saying "Risk indicators unavailable (source blocked)" and hiding the
terrorism counter, sub-tab and filter.
"""

from __future__ import annotations

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


def _fake_get(html_status):
    def _get(url, timeout=25, retries=fa.HTTP_RETRIES):
        if url == fa.ADVISORY_LIST_URL:
            if html_status == 200:
                return _result(url, HTML_FIXTURE, 200)
            return _result(url, None, html_status)
        if url == fa.ADVISORY_RSS_URL:
            return _result(url, RSS_FIXTURE, 200)
        raise AssertionError(f"unexpected URL {url}")
    return _get


def test_blocked_table_payload_says_risks_unavailable(monkeypatch, no_sleep):
    monkeypatch.setattr(fa, "_get", _fake_get(403))
    status: dict = {}
    payload = fa.fetch_live(status)
    assert payload["source"] == "rss-fallback"
    assert payload["risks_available"] is False
    assert payload["advisories"] and all(a["risks"] == [] for a in payload["advisories"])
    assert status["risks_available"] is False


def test_parsed_table_payload_says_risks_available(monkeypatch, no_sleep):
    monkeypatch.setattr(fa, "_get", _fake_get(200))
    payload = fa.fetch_live({})
    assert payload["source"] == "html"
    assert payload["risks_available"] is True
    by = {a["name"]: a for a in payload["advisories"]}
    assert "T" in by["Afghanistan"]["risks"]


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
    assert "t.risks_available !== false && t.source !== 'rss-fallback'" in src
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
