"""Summit news source (Google News RSS) — parsing, query shape, merge bounds.

No network: every test feeds a fixture or a stub fetcher.
"""
from __future__ import annotations

import sys
import time
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "snowflake_summit"))

import enrich_vendors as ev  # noqa: E402

TODAY = date(2026, 10, 4)


def _item(title, source="Business Wire", pub="Wed, 16 Sep 2026 07:00:00 GMT",
          link="https://news.google.com/rss/articles/CBMiAAA?oc=5"):
    return (f"<item><title>{title}</title><link>{link}</link>"
            f"<pubDate>{pub}</pubDate>"
            f'<source url="https://example.com">{source}</source></item>')


def _rss(*items):
    return ('<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>'
            "<title>q - Google News</title>" + "".join(items) + "</channel></rss>")


def test_parse_strips_publisher_suffix_and_shapes_item():
    xml = _rss(_item("Fivetran Launches Agent-Ready Pipelines for Snowflake - Business Wire"))
    [it] = ev.parse_gnews_rss(xml, "Fivetran", today=TODAY)
    assert it == {
        "vendor": "Fivetran",
        "headline": "Fivetran Launches Agent-Ready Pipelines for Snowflake",
        "date": "2026-09-16",
        "url": "https://news.google.com/rss/articles/CBMiAAA?oc=5",
        "source": "Business Wire",
        "summary": "",
        "relevance": "high",  # mentions Snowflake
    }


def test_headline_must_name_the_vendor():
    # Google matches article bodies; a headline about someone else is dropped.
    xml = _rss(_item("Modern data stack: layers, tools and design guide - netguru",
                     source="netguru"))
    assert ev.parse_gnews_rss(xml, "Fivetran", today=TODAY) == []


def test_ambiguous_name_needs_data_context_in_headline():
    xml = _rss(
        _item("Coastal flooding expected this weekend - NOAA", source="NOAA",
              link="https://news.google.com/a"),
        _item("Coastal expands its Snowflake data practice - PR Newswire",
              source="PR Newswire", link="https://news.google.com/b"),
    )
    out = ev.parse_gnews_rss(xml, "Coastal", today=TODAY)
    assert [i["headline"] for i in out] == ["Coastal expands its Snowflake data practice"]


def test_negative_terms_and_case_sensitive_match():
    xml = _rss(
        _item("Sigma Lithium posts record AI-driven quarter - Reuters", source="Reuters",
              link="https://news.google.com/a"),
        _item("SiGMA World Rome adds AI keynote - SoloAzar", source="SoloAzar",
              link="https://news.google.com/b"),
        _item("Sigma signs AWS deal for AI apps and analytics - Business Wire",
              link="https://news.google.com/c"),
    )
    out = ev.parse_gnews_rss(xml, "Sigma", today=TODAY)
    assert [i["url"] for i in out] == ["https://news.google.com/c"]


def test_blocklisted_auto_generated_sources_are_dropped():
    xml = _rss(_item("Fivetran - 2026 Funding Rounds &amp; List of Investors - Tracxn",
                     source="Tracxn"))
    assert ev.parse_gnews_rss(xml, "Fivetran", today=TODAY) == []


def test_window_dedupe_sort_and_limit():
    items = [
        _item("Fivetran old news - A", source="A", pub="Mon, 01 Jun 2026 07:00:00 GMT",
              link="https://news.google.com/old"),
        _item("Fivetran raises again - B", source="B", pub="Fri, 02 Oct 2026 07:00:00 GMT",
              link="https://news.google.com/1"),
        # same story syndicated elsewhere: collapses to one
        _item("Fivetran raises again - C", source="C", pub="Fri, 02 Oct 2026 09:00:00 GMT",
              link="https://news.google.com/2"),
    ] + [
        _item(f"Fivetran update {n} - D", source="D",
              pub=f"Tue, {10 + n:02d} Sep 2026 07:00:00 GMT",
              link=f"https://news.google.com/u{n}")
        for n in range(5)
    ]
    out = ev.parse_gnews_rss(_rss(*items), "Fivetran", today=TODAY)
    assert len(out) == ev.NEWS_MAX_PER_VENDOR
    assert out[0]["headline"] == "Fivetran raises again"
    assert [i["date"] for i in out] == sorted((i["date"] for i in out), reverse=True)
    assert all(i["date"] >= "2026-09-04" for i in out)  # 30-day window


def test_non_rss_body_raises_so_it_counts_as_a_failure():
    with pytest.raises(ValueError):
        ev.parse_gnews_rss("<html><body>Sorry...</body></html>", "Fivetran", today=TODAY)
    with pytest.raises(ValueError):
        ev.parse_gnews_rss("not xml at all", "Fivetran", today=TODAY)


def test_query_shapes():
    assert ev.gnews_query("Fivetran") == '"Fivetran" when:30d'
    assert ev.gnews_query("Microsoft") == '"Microsoft" Snowflake when:30d'
    assert ev.gnews_query("Hakkoda (an IBM Company)") == '"Hakkoda" when:30d'
    q = ev.gnews_query("Monte Carlo")
    assert q.startswith('"Monte Carlo" (Snowflake OR "data observability"')
    assert '-"Monte Carlo simulation"' in q
    assert ev.gnews_query("Coastal").startswith('"Coastal" Snowflake')
    url = ev.gnews_url("Fivetran")
    assert url.startswith("https://news.google.com/rss/search?q=")
    assert "ceid=US%3Aen" in url


def test_every_override_names_a_real_vendor():
    import json
    names = {v["name"] for v in json.loads(ev.VENDORS_PATH.read_text())["vendors"]}
    for table in (ev.SEARCH_TERMS, ev.CONTEXT_TERMS, ev.NEGATIVE_TERMS):
        assert set(table) <= names, set(table) - names
    assert ev.AMBIGUOUS <= names, ev.AMBIGUOUS - names
    assert ev.SNOWFLAKE_ONLY <= names, ev.SNOWFLAKE_ONLY - names
    assert set(ev.CONTEXT_TERMS) <= ev.AMBIGUOUS


def _auto(vendor, day, n):
    return {"vendor": vendor, "headline": f"{vendor} {n}", "date": day,
            "url": f"https://news.google.com/rss/articles/{vendor}{n}",
            "source": "x", "summary": "", "relevance": "medium"}


def test_bound_auto_rotates_per_vendor_and_never_touches_curated():
    curated = [{"vendor": "Atlan", "headline": "Atlan at Summit", "date": "2026-06-03",
                "url": "https://atlan.com/summit", "source": "Atlan",
                "summary": "curated", "relevance": "high"}]
    auto = [_auto("Atlan", f"2026-09-{d:02d}", d) for d in range(1, 10)]
    kept, evicted = ev._bound_auto(curated + auto, per_vendor=3)
    assert kept[0] is curated[0]
    assert [i["date"] for i in kept[1:]] == ["2026-09-07", "2026-09-08", "2026-09-09"]
    assert len(evicted) == 6
    assert not ev._is_auto(curated[0]) and ev._is_auto(auto[0])


def test_enrich_one_caches_empty_success_but_stale_keeps_on_failure():
    now = time.time()
    far = now + 3600
    old_items = [_auto("Hex", "2026-09-01", 1)]

    # Failure (None): previous items are kept and the cache is NOT refreshed.
    cache = {"Hex": {ev.NEWS_CACHE_KEY: {"ts": 0, "items": old_items}}}
    news, _, live = ev.enrich_one({"name": "Hex"}, cache, now, far,
                                  fetch_news=lambda n: None, fetch_wd=lambda n: {})
    assert news == old_items and live is False
    assert cache["Hex"][ev.NEWS_CACHE_KEY]["ts"] == 0

    # Successful empty answer: cached, so the next run does not re-query.
    news, _, live = ev.enrich_one({"name": "Hex"}, cache, now, far,
                                  fetch_news=lambda n: [], fetch_wd=lambda n: {})
    assert news == [] and live is True
    assert cache["Hex"][ev.NEWS_CACHE_KEY] == {"ts": now, "items": []}

    calls = []
    ev.enrich_one({"name": "Hex"}, cache, now + 60, far,
                  fetch_news=lambda n: calls.append(n) or [], fetch_wd=lambda n: {})
    assert calls == []  # fresh cache hit


def test_retired_gdelt_cache_slot_is_ignored_and_dropped():
    now = time.time()
    cache = {"Chalk": {"news": {"ts": now, "items": [_auto("Chalk", "2026-09-01", 1)]}}}
    news, _, _ = ev.enrich_one({"name": "Chalk"}, cache, now, now + 60,
                               fetch_news=lambda n: [], fetch_wd=lambda n: {})
    assert news == []
    assert "news" not in cache["Chalk"]


def test_pages_workflow_commits_the_news_feed_back():
    wf = (ROOT / ".github" / "workflows" / "pages.yml").read_text()
    enrich = wf.index("python snowflake_summit/enrich_vendors.py")
    commit = wf.index("git add snowflake_summit/news.json")
    assert enrich < commit
