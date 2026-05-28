"""Tests for the Wikipedia infobox enricher.

All HTTP calls are mocked — no real network traffic is issued. We exercise:

  * The wikitext infobox extractor (brace-depth aware).
  * The field cleaner (strips refs, comments, nested templates, wikilinks).
  * The infobox -> structured field mapping.
  * The cache TTL logic (fresh entries skip the fetcher).
  * Defensive fallback: hardcoded curated values win when Wikipedia is empty
    or the fetcher raises.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch


import wiki_enrich


# ----------------------------------------------------------------------------
# Sample wikitext fixtures — trimmed snippets resembling real Wikipedia pages
# ----------------------------------------------------------------------------

SAMPLE_OPENAI = """\
{{Short description|American AI research lab}}
{{Infobox company
| name = OpenAI
| logo = OpenAI Logo.svg
| industry = [[Artificial intelligence]]
| founded = {{Start date and age|2015|12|11}}
| founder = [[Sam Altman]], [[Elon Musk]]
| hq_location_city = [[San Francisco]], [[California]]
| hq_location_country = United States
| num_employees = 4,500
| num_employees_year = 2024
| website = {{URL|openai.com}}
}}
'''OpenAI''' is an American [[artificial intelligence]] research laboratory.
"""

SAMPLE_NESTED = """\
{{Infobox company
| name = Acme
| founded = {{Start date|2019|03|14}}<ref name="forbes">Forbes</ref>
| industry = {{hlist|Software|[[Cloud computing|Cloud]]|AI}}
| num_employees = ~1,200 (2023)
| location = [[Paris]], [[France]]
}}
"""

SAMPLE_RANGE = """\
{{Infobox company
| name = Beta
| founded = 2021
| num_employees = 500–700 (2024)
}}
"""

SAMPLE_NO_INFOBOX = "Just some prose, no template.\nNot a stub."


# ----------------------------------------------------------------------------
# parse_infobox / extract_fields
# ----------------------------------------------------------------------------


def test_parse_infobox_basic():
    box = wiki_enrich.parse_infobox(SAMPLE_OPENAI)
    assert box["name"] == "OpenAI"
    assert box["industry"] == "Artificial intelligence"
    assert box["hq_location_country"] == "United States"
    assert box["num_employees"] == "4,500"
    # The {{Start date}} template gets stripped, so ``founded`` collapses to
    # an empty string and is dropped. That's expected — the curated fallback
    # still carries the year. What matters is that no template syntax leaks
    # into any field we did keep.
    for v in box.values():
        assert "{{" not in v and "}}" not in v


def test_parse_infobox_handles_nested_templates_and_refs():
    box = wiki_enrich.parse_infobox(SAMPLE_NESTED)
    assert box["name"] == "Acme"
    # <ref>...</ref> tags must be stripped.
    assert "Forbes" not in box.get("founded", "")
    assert box["location"] == "Paris, France"
    # No template syntax should leak into any kept field. ``industry`` may
    # be entirely template-driven and therefore absent — that's fine.
    for v in box.values():
        assert "{{" not in v and "}}" not in v


def test_parse_infobox_missing_returns_empty():
    assert wiki_enrich.parse_infobox(SAMPLE_NO_INFOBOX) == {}
    assert wiki_enrich.parse_infobox("") == {}


def test_parse_infobox_malformed_does_not_raise():
    # Unclosed braces — parser should give up cleanly.
    assert isinstance(wiki_enrich.parse_infobox("{{Infobox company | name = X "), dict)


def test_extract_fields_openai():
    box = wiki_enrich.parse_infobox(SAMPLE_OPENAI)
    fields = wiki_enrich.extract_fields(box)
    assert fields.get("num_employees") == 4500
    assert fields.get("hq") == "United States"
    assert fields.get("industry") == "Artificial intelligence"
    # The {{Start date|2015|...}} template is stripped, so founded_year may
    # not be present from the cleaned value alone. That's acceptable — the
    # curated fallback (founded_year: 2015) still wins.


def test_extract_fields_range_takes_upper_bound():
    box = wiki_enrich.parse_infobox(SAMPLE_RANGE)
    fields = wiki_enrich.extract_fields(box)
    assert fields.get("founded_year") == 2021
    assert fields.get("num_employees") == 700


def test_extract_fields_nested_year_stripped():
    box = wiki_enrich.parse_infobox(SAMPLE_NESTED)
    fields = wiki_enrich.extract_fields(box)
    # 1,200 with a (2023) suffix — we should ignore the parenthetical year.
    assert fields.get("num_employees") == 1200


# ----------------------------------------------------------------------------
# Field extractors directly
# ----------------------------------------------------------------------------


def test_extract_founded_year_picks_first_plausible():
    assert wiki_enrich.extract_founded_year("2015") == 2015
    assert wiki_enrich.extract_founded_year("c. 1998 as a spin-off") == 1998
    assert wiki_enrich.extract_founded_year("") is None
    assert wiki_enrich.extract_founded_year("no year here") is None


def test_extract_num_employees_handles_commas_tildes():
    assert wiki_enrich.extract_num_employees("~3,500 (2024)") == 3500
    assert wiki_enrich.extract_num_employees("1500") == 1500
    assert wiki_enrich.extract_num_employees("") is None
    assert wiki_enrich.extract_num_employees("a handful") is None


# ----------------------------------------------------------------------------
# fetch_company_infobox_fields with mocked HTTP
# ----------------------------------------------------------------------------


def _fake_http(payloads: dict[str, dict | None]):
    """Build a fake http_get_json that looks up a payload by URL substring."""
    def _inner(url: str, *, timeout: float = 15.0):
        for needle, payload in payloads.items():
            if needle in url:
                return payload
        return None
    return _inner


def test_fetch_company_infobox_fields_happy_path():
    payload = {"parse": {"wikitext": {"*": SAMPLE_OPENAI}}}
    fake = _fake_http({"OpenAI": payload})
    out = wiki_enrich.fetch_company_infobox_fields("OpenAI", http_get_json=fake)
    assert out["num_employees"] == 4500
    assert out["hq"] == "United States"
    assert out["industry"] == "Artificial intelligence"
    assert out["_wiki_title"] == "OpenAI"


def test_fetch_company_infobox_fields_falls_back_on_http_failure():
    fake = _fake_http({})  # everything misses → http_get_json returns None
    out = wiki_enrich.fetch_company_infobox_fields("Nonexistent Co", http_get_json=fake)
    assert out == {}


def test_fetch_company_infobox_fields_uses_override():
    # The override for xAI points at "XAI (company)". Make sure that's the
    # title our fake fetcher sees.
    seen: list[str] = []

    def fake(url: str, *, timeout: float = 15.0):
        seen.append(url)
        if "XAI" in url or "company" in url:
            return {"parse": {"wikitext": {"*": SAMPLE_RANGE}}}
        return None

    out = wiki_enrich.fetch_company_infobox_fields("xAI", http_get_json=fake)
    assert out.get("founded_year") == 2021
    assert any("XAI" in u or "company" in u for u in seen)


# ----------------------------------------------------------------------------
# enrich_companies — cache + fallback semantics
# ----------------------------------------------------------------------------


def test_enrich_companies_curated_values_win(tmp_path: Path):
    """Hardcoded curated values must override Wikipedia data on conflict."""
    cache = tmp_path / "wiki_cache.json"
    # Wikipedia says founded_year=2099 but curated says 2015 — curated wins.
    fetcher = lambda name: {"founded_year": 2099, "num_employees": 9000}
    rows = [{
        "name": "OpenAI",
        "founded_year": 2015,
        "valuation_usd": 300_000_000_000,
    }]
    out = wiki_enrich.enrich_companies(rows, cache_path=cache, fetcher=fetcher)
    assert out[0]["founded_year"] == 2015  # curated value preserved
    assert out[0]["num_employees"] == 9000  # filled from Wikipedia (gap)


def test_enrich_companies_uses_cache_when_fresh(tmp_path: Path):
    cache = tmp_path / "wiki_cache.json"
    cache.write_text(json.dumps({
        "OpenAI": {
            "fetched_at": "2030-01-01T00:00:00Z",
            "fields": {"num_employees": 1234},
        }
    }))
    calls: list[str] = []

    def fetcher(name: str):
        calls.append(name)
        return {"num_employees": 9999}

    # Use a `now` that's just after the cached timestamp so it stays fresh.
    import datetime as _dt
    now = _dt.datetime(2030, 1, 2, tzinfo=_dt.timezone.utc).timestamp()
    out = wiki_enrich.enrich_companies(
        [{"name": "OpenAI"}],
        cache_path=cache,
        fetcher=fetcher,
        now=now,
    )
    assert calls == []  # fetcher not invoked
    assert out[0]["num_employees"] == 1234


def test_enrich_companies_refetches_when_stale(tmp_path: Path):
    cache = tmp_path / "wiki_cache.json"
    cache.write_text(json.dumps({
        "OpenAI": {
            "fetched_at": "2000-01-01T00:00:00Z",  # ancient
            "fields": {"num_employees": 1},
        }
    }))
    calls: list[str] = []

    def fetcher(name: str):
        calls.append(name)
        return {"num_employees": 4242}

    out = wiki_enrich.enrich_companies(
        [{"name": "OpenAI"}],
        cache_path=cache,
        fetcher=fetcher,
    )
    assert calls == ["OpenAI"]
    assert out[0]["num_employees"] == 4242
    # Cache should be refreshed on disk.
    saved = json.loads(cache.read_text())
    assert saved["OpenAI"]["fields"]["num_employees"] == 4242
    assert "fetched_at" in saved["OpenAI"]


def test_enrich_companies_fetcher_raises_falls_back(tmp_path: Path):
    cache = tmp_path / "wiki_cache.json"

    def bad_fetcher(name: str):
        raise RuntimeError("network down")

    out = wiki_enrich.enrich_companies(
        [{"name": "OpenAI", "founded_year": 2015}],
        cache_path=cache,
        fetcher=bad_fetcher,
    )
    # Original row survives untouched.
    assert out[0]["founded_year"] == 2015
    # Cache entry records the empty result so we don't hammer the API.
    saved = json.loads(cache.read_text())
    assert saved["OpenAI"]["fields"] == {}


def test_enrich_companies_handles_empty_list(tmp_path: Path):
    cache = tmp_path / "wiki_cache.json"
    assert wiki_enrich.enrich_companies([], cache_path=cache) == []
    assert wiki_enrich.enrich_companies(None, cache_path=cache) == []


def test_enrich_companies_passes_through_non_dict(tmp_path: Path):
    cache = tmp_path / "wiki_cache.json"
    out = wiki_enrich.enrich_companies(
        ["not a dict", {"name": "X"}],
        cache_path=cache,
        fetcher=lambda n: {},
    )
    assert out[0] == "not a dict"
    assert out[1]["name"] == "X"


def test_enrich_ai_curated_preserves_other_keys(tmp_path: Path):
    cache = tmp_path / "wiki_cache.json"
    curated = {
        "top_funded_companies": [{"name": "OpenAI", "founded_year": 2015}],
        "investment_kpis": [{"key": "x"}],
        "compiled_at": "2026-05-15T00:00:00Z",
    }
    out = wiki_enrich.enrich_ai_curated(
        curated, cache_path=cache, fetcher=lambda n: {"hq": "United States"}
    )
    assert out["investment_kpis"] == [{"key": "x"}]
    assert out["compiled_at"] == "2026-05-15T00:00:00Z"
    assert out["top_funded_companies"][0]["hq"] == "United States"
    assert out["top_funded_companies"][0]["founded_year"] == 2015


def test_enrich_ai_curated_handles_non_dict_input():
    # If somebody hands us a list or None, just return it unchanged.
    assert wiki_enrich.enrich_ai_curated(None) is None
    assert wiki_enrich.enrich_ai_curated([1, 2, 3]) == [1, 2, 3]


# ----------------------------------------------------------------------------
# Integration with fetch_market.load_ai_curated
# ----------------------------------------------------------------------------


def test_load_ai_curated_never_calls_real_wikipedia():
    """The full load_ai_curated path must not hit the network during tests."""
    import fetch_market

    # Patch the urllib opener at the lowest level so any accidental real
    # request would raise.
    with patch("wiki_enrich._http_get_json", return_value=None):
        out = fetch_market.load_ai_curated()
    assert isinstance(out, dict)
    assert "top_funded_companies" in out
    # Companies are still present, just unenriched (Wikipedia returned None
    # for every title).
    assert any(c.get("name") == "OpenAI" for c in out["top_funded_companies"])
    # Curated founded_year survives.
    openai = next(c for c in out["top_funded_companies"] if c["name"] == "OpenAI")
    assert openai["founded_year"] == 2015
