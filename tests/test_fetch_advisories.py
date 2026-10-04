"""Tests for fetch_advisories.py — text fidelity of the stored advisory feed.

WHY THIS FILE EXISTS
--------------------
data-travel.json shipped bulletin excerpts containing literal
`&#8220;Unrest&#8221;` and `risk of&nbsp;crime`. The State Dept RSS is
double-encoded (an XML-escaped HTML fragment inside <description>), so
ElementTree undoes the XML layer and the parser used to store the resulting
HTML fragment verbatim after a tag strip. The dashboard then HTML-escapes
everything it renders — correctly — which escaped the ampersand a second time
and put the entity source on screen.

The fix is one html.unescape() at parse time. These tests pin BOTH halves of
that: entities really are decoded, and they are decoded exactly ONCE (text
that legitimately reads "&lt;" must survive as "&lt;", not collapse to "<").

The entity shapes exercised here are not invented — ENTITIES_IN_COMMITTED_DATA
is the complete set found in the committed data-travel.json.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest
import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import fetch_advisories as fa  # noqa: E402


ENTITY_RE = re.compile(r"&[#a-zA-Z0-9]{1,10};")

# UTF-8 bytes decoded one at a time as Latin-1 always land in this shape: a
# leading C2/C3/E2 followed by a continuation byte. See fa._decode_response.
MOJIBAKE_RE = re.compile("[\u00c2\u00c3\u00e2][\u0080-\u00bf]")

# Every distinct entity found in the committed data-travel.json bulletin
# bodies (2026-05-26 snapshot), with the character each one must become.
# Counts at the time of writing: &nbsp; x119, &#8239; x12, &#8220; x10,
# &#8221; x10, &#8217; x4, &quot; x4, &#8212; x2, &#7897; x1, &#233; x1.
ENTITIES_IN_COMMITTED_DATA = {
    "&nbsp;": " ",         # folded: NBSP does not collapse when rendered
    "&#8239;": " ",        # narrow NBSP, same treatment
    "&#8220;": "“",   # left curly double quote
    "&#8221;": "”",   # right curly double quote
    "&#8217;": "’",   # curly apostrophe
    "&quot;": '"',
    "&#8212;": "—",   # em dash
    "&#7897;": "ộ",   # Vietnamese o-circumflex-dot (Da Nang, Ho Chi Minh)
    "&#233;": "é",    # e-acute
}


def _rss(description: str, title: str = "Testland - Level 3: Reconsider Travel") -> str:
    """Minimal single-item RSS 2.0 document.

    `description` is inserted as-is, so a test passes the XML-escaped form the
    real feed uses and ElementTree performs the same first-level unescape it
    performs in production.
    """
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0"><channel><item>'
        f"<title>{title}</title>"
        "<link>https://travel.state.gov/x.html</link>"
        "<pubDate>Mon, 02 Mar 2026</pubDate>"
        f"<description>{description}</description>"
        "</item></channel></rss>"
    )


# --------------------------------------------------------------------------
# The decoding helpers
# --------------------------------------------------------------------------

@pytest.mark.parametrize("entity,expected", sorted(ENTITIES_IN_COMMITTED_DATA.items()))
def test_every_entity_in_committed_data_decodes(entity, expected):
    """Each real entity shape becomes a real character, not entity text."""
    out = fa._html_fragment_to_text(f"<p>before{entity}after</p>")
    assert out == f"before{expected}after", out
    assert not ENTITY_RE.search(out)


def test_decodes_the_two_shapes_readers_complained_about():
    assert fa._html_fragment_to_text(
        "<p>The &#8220;Unrest&#8221; risk indicator was removed.</p>"
    ) == "The “Unrest” risk indicator was removed."
    assert fa._html_fragment_to_text(
        "Do Not Travel to Haiti due to the risk of&nbsp;crime,&nbsp;terrorism"
    ) == "Do Not Travel to Haiti due to the risk of crime, terrorism"


def test_unescape_is_applied_exactly_once():
    """The trap in the other direction.

    Text that is MEANT to display "&lt;" arrives from the feed as
    "&amp;amp;lt;" and reaches this helper (post-ElementTree) as "&amp;lt;".
    One unescape yields "&lt;" — correct. A second would yield "<", corrupting
    the text and handing the renderer a tag opener.
    """
    assert fa._html_fragment_to_text("Use &amp;lt;brackets&amp;gt;") == "Use &lt;brackets&gt;"
    assert fa._plain_text("Turks &amp; Caicos") == "Turks & Caicos"
    # And it is NOT a fixed point: running it twice would change the answer,
    # which is exactly why the implementation must never loop.
    once = fa._html_fragment_to_text("Use &amp;lt;brackets&amp;gt;")
    assert fa._html_fragment_to_text(once) != once


def test_tags_are_stripped_before_entities_are_decoded():
    """Order matters: decoding first would create a <p> and then delete it."""
    assert fa._html_fragment_to_text("<b>keep</b> &amp;lt;p&amp;gt;") == "keep &lt;p&gt;"


def test_limit_is_applied_after_decoding():
    """The cap counts characters a reader sees and cannot bisect an entity."""
    out = fa._html_fragment_to_text("&#8220;" * 50, limit=10)
    assert out == "“" * 10
    assert not ENTITY_RE.search(out)


def test_nbsp_runs_collapse_but_newlines_survive():
    """The feed pads headings with long NBSP runs; those must not become a
    hole on screen. Paragraph breaks are the only structure left after the
    tags are stripped, so newlines stay."""
    out = fa._html_fragment_to_text("Advisory summary" + "&nbsp; " * 8 + "\n<p>Next line</p>")
    assert out == "Advisory summary\nNext line", repr(out)


def test_empty_input():
    assert fa._html_fragment_to_text("") == ""
    assert fa._plain_text("") == ""


# --------------------------------------------------------------------------
# The parsers that store the text
# --------------------------------------------------------------------------

def test_parse_advisory_rss_stores_real_characters():
    xml = _rss(
        "&lt;p&gt;The &amp;#8220;Unrest&amp;#8221; risk indicator was removed. "
        "The advisory level was increased to 3.&lt;/p&gt;"
        "&lt;p&gt;Reconsider travel&amp;#8239;to Testland due to risk "
        "of&amp;nbsp;crime.&lt;/p&gt;"
    )
    rows = fa.parse_advisory_rss(xml)
    assert len(rows) == 1
    body = rows[0]["body"]
    assert "“Unrest”" in body
    assert "risk of crime" in body
    assert "Reconsider travel to Testland" in body
    assert not ENTITY_RE.search(body), body


def test_parse_advisory_rss_decodes_titles():
    xml = _rss("&lt;p&gt;The advisory level was increased to 3.&lt;/p&gt;",
               title="C&amp;#244;te d&amp;#8217;Ivoire - Level 3: Reconsider Travel")
    rows = fa.parse_advisory_rss(xml)
    assert rows[0]["title"] == "Côte d’Ivoire - Level 3: Reconsider Travel"
    assert not ENTITY_RE.search(rows[0]["title"])


def test_advisories_from_rss_decodes_country_names():
    """The RSS fallback derives country names — and therefore per-country
    URLs — from the same double-encoded titles. Before the fix this produced
    a name of "C&#244;te d&#8217;Ivoire" and a slug to match."""
    xml = _rss("&lt;p&gt;whatever&lt;/p&gt;",
               title="C&amp;#244;te d&amp;#8217;Ivoire - Level 3: Reconsider Travel")
    rows = fa.advisories_from_rss(xml)
    assert len(rows) == 1
    assert rows[0]["name"] == "Côte d’Ivoire"
    assert not ENTITY_RE.search(rows[0]["url"])
    assert rows[0]["url"].endswith("/cote-divoire.html"), rows[0]["url"]


def test_nbsp_entity_no_longer_hides_a_real_bulletin():
    """`_UPDATED_REFLECT_RE` tolerates a real NBSP between the words but could
    never match the literal text "Updated to&nbsp;reflect" the feed actually
    sent — so decoding is what makes that filter branch reachable at all."""
    body_raw = "Updated to&nbsp;reflect the new advisory."
    assert not fa._is_bulletin("Testland - Level 2", body_raw)
    assert fa._is_bulletin("Testland - Level 2", fa._html_fragment_to_text(body_raw))


def test_fixture_roundtrip_has_no_entity_text_anywhere():
    """Whole-feed lock against the real offline fixture."""
    xml = fa._RSS_FIXTURE_PATH.read_text()
    rows = fa.parse_advisory_rss(xml)
    assert rows, "fixture should still yield bulletins"
    for row in rows:
        for field in ("title", "body", "tag"):
            assert not ENTITY_RE.search(row[field]), (field, row[field])
    for row in fa.advisories_from_rss(xml):
        assert not ENTITY_RE.search(row["name"]), row["name"]


def test_self_test_still_passes():
    """The module's own offline self-test is the parser's regression suite."""
    assert fa._self_test() == 0


# --------------------------------------------------------------------------
# Charset: the other source of garbage characters in the same excerpts
# --------------------------------------------------------------------------

def _fake_response(body: bytes, content_type: str) -> requests.Response:
    """A Response wired up the way requests wires a real one, so the
    ISO-8859-1 default for charset-less text/* responses is reproduced rather
    than assumed."""
    r = requests.Response()
    r.status_code = 200
    r.headers = requests.structures.CaseInsensitiveDict({"Content-Type": content_type})
    r._content = body
    r.encoding = requests.utils.get_encoding_from_headers(r.headers)
    return r


def test_charsetless_xml_is_decoded_as_utf8(monkeypatch):
    """travel.state.gov serves the RSS as text/xml with no charset, and
    requests then defaults to ISO-8859-1 — which is how "due toÂ terrorism"
    got into the committed data-travel.json."""
    want = "Reconsider travel due to terrorism — café"
    resp = _fake_response(want.encode("utf-8"), "text/xml")
    # Guard the premise: without the fix this really is mojibake.
    assert MOJIBAKE_RE.search(resp.text)

    monkeypatch.setattr(fa.requests, "get", lambda *a, **k: resp)
    got = fa._get("https://travel.state.gov/_res/rss/TAsTWs.xml")
    assert got.ok
    assert got.text == want
    assert not MOJIBAKE_RE.search(got.text)


def test_declared_charset_is_respected(monkeypatch):
    """When the upstream does declare a charset we must not second-guess it."""
    resp = _fake_response("café".encode("iso-8859-1"), "text/html; charset=ISO-8859-1")
    monkeypatch.setattr(fa.requests, "get", lambda *a, **k: resp)
    assert fa._get("https://travel.state.gov/x.html").text == "café"


def test_non_utf8_body_without_charset_falls_back(monkeypatch):
    """Bytes that are not valid UTF-8 must not raise; requests' own guess wins."""
    resp = _fake_response(b"caf\xe9", "text/html")
    monkeypatch.setattr(fa.requests, "get", lambda *a, **k: resp)
    got = fa._get("https://travel.state.gov/x.html")
    assert got.ok
    assert got.text == "café"


def test_utf8_bom_is_consumed(monkeypatch):
    """A BOM left in front of an XML declaration makes ElementTree throw."""
    resp = _fake_response("\ufeff<?xml version='1.0'?><rss/>".encode("utf-8"), "text/xml")
    monkeypatch.setattr(fa.requests, "get", lambda *a, **k: resp)
    assert fa._get("https://travel.state.gov/f.xml").text.startswith("<?xml")


# --------------------------------------------------------------------------
# The committed payload itself — what readers actually load
# --------------------------------------------------------------------------

@pytest.mark.parametrize("rel", ["data-travel.json", "v2/data-travel.json"])
def test_committed_payload_carries_no_entity_text(rel):
    """Regenerating this file is a network operation, so the parser fix alone
    would not clear what is already on disk. This locks the one-off
    normalisation that was applied alongside it."""
    path = ROOT / rel
    if not path.exists():
        pytest.skip(f"{rel} not present")
    payload = json.loads(path.read_text())
    offenders = []
    for section in ("advisories", "bulletins"):
        for row in payload.get(section, []):
            for key, value in row.items():
                if isinstance(value, str) and ENTITY_RE.search(value):
                    offenders.append(f"{section}.{key}: {value[:80]}")
    assert not offenders, offenders[:5]


@pytest.mark.parametrize("rel", ["data-travel.json", "v2/data-travel.json"])
def test_committed_payload_carries_no_mojibake(rel):
    path = ROOT / rel
    if not path.exists():
        pytest.skip(f"{rel} not present")
    payload = json.loads(path.read_text())
    offenders = []
    for section in ("advisories", "bulletins"):
        for row in payload.get(section, []):
            for key, value in row.items():
                if isinstance(value, str) and MOJIBAKE_RE.search(value):
                    offenders.append(f"{section}.{key}: {value[:80]}")
    assert not offenders, offenders[:5]
