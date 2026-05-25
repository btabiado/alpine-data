"""
U.S. State Department travel-advisory fetcher.

Sources (all free, no auth required):
  travel.state.gov HTML  per-country advisory level, risk codes, date
  travel.state.gov RSS   security alerts / advisory-update bulletins

Output: v2/data-travel.json (sidecar for the V2 dashboard's Travel Advisories tab).

Schema (matches what the front-end consumes):
    {
      "generated_at": "2026-05-25T12:00:00Z",
      "advisories": [
        {"name": "Afghanistan", "level": 4, "risks": ["U","C","H","K","T","D","N"],
         "date": "2026-02-20",
         "url": "https://travel.state.gov/en/international-travel/travel-advisories/afghanistan.html"}
      ],
      "bulletins": [
        {"tag": "Worldwide Caution", "severity": "red", "date": "2026-03-22",
         "title": "...", "body": "...", "href": "https://..."}
      ]
    }

Cadence: SAFE to run hourly alongside the other fetchers (State Dept data
moves on a scale of days to weeks), but ideally this would be daily-gated by
whoever wires up CI — there's no benefit to scraping the HTML page 24 times
a day. We do NOT enforce that here; leaving the cadence decision to the
caller keeps this module a pure pipeline step.

Resilience: on any scrape failure (or zero-advisory result) we read the
existing v2/data-travel.json, log a warning, and exit non-zero WITHOUT
overwriting it. The dashboard never sees an empty advisories list.

CLI:
    python fetch_advisories.py                 # default --out v2/data-travel.json
    python fetch_advisories.py --out PATH      # custom output path
    python fetch_advisories.py --no-network    # offline parser self-test only
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import requests


UA = "Mozilla/5.0 (compatible; etf-flow-dashboard/1.0)"
H = {"User-Agent": UA}
ROOT = Path(__file__).parent
DEFAULT_OUT = ROOT / "v2" / "data-travel.json"

ADVISORY_LIST_URL = "https://travel.state.gov/en/international-travel/travel-advisories.html"
# Canonical State Dept RSS feed for security alerts + advisory-level changes.
# (Also available at /content/travel/en/_jcr_content.xy.html but this is the
# documented feed URL surfaced in the front-end of travel.state.gov.)
ADVISORY_RSS_URL = "https://travel.state.gov/_res/rss/TAsTWs.xml"

# Valid State Dept risk indicator code letters. Anything outside this set is
# dropped by the parser (defensive against stray punctuation in cells).
VALID_RISK_CODES = set("TCUHKNDOE")


# ----- helpers ---------------------------------------------------------------

def _get(url: str, timeout: int = 25) -> str | None:
    """GET with shared UA. Returns response.text or None on any failure."""
    try:
        r = requests.get(url, headers=H, timeout=timeout)
        if r.status_code != 200:
            print(f"  [skip] {url} -> {r.status_code}", file=sys.stderr)
            return None
        return r.text
    except Exception as e:
        print(f"  [skip] {url} -> {e}", file=sys.stderr)
        return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ----- slug map (country name -> travel.state.gov page slug) -----------------
#
# The naive transform "lowercase, strip non-alphanumerics, hyphenate" works
# for ~90% of names but breaks on names with parentheses, apostrophes,
# accented characters, and multi-word destinations. The override map below
# handles the known-bad cases. Unverified slugs are flagged with a comment;
# whoever first hits a 404 should confirm the live URL and remove the flag.
SLUG_OVERRIDES: dict[str, str] = {
    # VERIFIED (cross-checked against live travel.state.gov in May 2026):
    "Burma (Myanmar)": "burma-myanmar",
    "Côte d'Ivoire (Ivory Coast)": "cote-divoire-ivory-coast",
    "Democratic Republic of the Congo (D.R.C.)": "democratic-republic-of-the-congo",
    "North Korea (Democratic People's Republic of Korea)":
        "korea-democratic-peoples-republic-of-korea-",
    "Republic of North Macedonia": "north-macedonia",
    "United Kingdom of Great Britain and Northern Ireland": "united-kingdom",
    "Federated States of Micronesia": "micronesia",
    "Israel, The West Bank and Gaza": "israel-the-west-bank-and-gaza",

    # UNVERIFIED — best guesses based on State Dept URL conventions. If any
    # 404 in production, update from the live travel.state.gov URL and
    # delete the warning comment. Listed for completeness because the naive
    # slug builder would otherwise emit something obviously wrong (e.g.
    # "the-gambia" actually appears as "gambia-the").
    "The Gambia": "gambia-the",                          # UNVERIFIED
    "Eswatini (Swaziland)": "eswatini",                  # UNVERIFIED
    "Cabo Verde": "cabo-verde",                          # UNVERIFIED
    "Vatican City (Holy See)": "holy-see",               # UNVERIFIED
    "Bonaire, Sint Eustatius, and Saba": "bonaire-sint-eustatius-and-saba",  # UNVERIFIED
    "French West Indies": "french-west-indies",          # UNVERIFIED
    "Guadeloupe (French West Indies)": "guadeloupe",     # UNVERIFIED
    "Martinique (French West Indies)": "martinique",     # UNVERIFIED
    "Saint Barthélemy (French West Indies)": "saint-barthelemy",  # UNVERIFIED
    "Saint Martin (French West Indies)": "saint-martin",  # UNVERIFIED
}

# Plain names that need explicit overrides because the naive transform
# produces a slug that doesn't actually exist on travel.state.gov.
SLUG_OVERRIDES.update({
    "Burkina Faso": "burkina-faso",                      # naive works, listed per spec
    "Republic of the Congo": "republic-of-the-congo",    # UNVERIFIED — naive would collide w/ DRC
    "Sao Tome and Principe": "sao-tome-and-principe",    # UNVERIFIED
    "Trinidad and Tobago": "trinidad-and-tobago",
})


def slugify_country(name: str) -> str:
    """Build the per-country page slug for travel.state.gov URLs.

    Honors SLUG_OVERRIDES first, then falls back to a conservative
    transform: lowercase, strip diacritics, drop apostrophes, replace
    everything non-alphanumeric with hyphens, collapse runs, strip ends.
    """
    if name in SLUG_OVERRIDES:
        return SLUG_OVERRIDES[name]
    # Best-effort diacritic strip — keep this dependency-free.
    import unicodedata
    nfkd = unicodedata.normalize("NFKD", name)
    ascii_ = "".join(c for c in nfkd if not unicodedata.combining(c))
    # Drop apostrophes entirely (Côte d'Ivoire-style); replace everything
    # else non-alphanumeric with hyphens.
    no_apos = ascii_.replace("'", "").replace("'", "")
    slug = re.sub(r"[^A-Za-z0-9]+", "-", no_apos).strip("-").lower()
    return slug


def build_country_url(name: str) -> str:
    return f"https://travel.state.gov/en/international-travel/travel-advisories/{slugify_country(name)}.html"


# ----- HTML table parser -----------------------------------------------------

class _AdvisoryTableParser(HTMLParser):
    """Stateful HTMLParser that pulls the advisory rows out of the State Dept
    advisory-list page.

    Page layout (as of May 2026) — a single ``<table id="htmlTable">``
    whose ``<tbody>`` rows look like:

        <tr>
          <th scope="row"><a ...>Country Name</a></th>
          <td><p><span class="level-badge level-badge-N"></span>Level N: ...</p></td>
          <td>
            <div class="tsg-utility-risk-pill-container">
              <span class="tsg-utility-risk-pill">UNREST (U)</span>
              <span class="tsg-utility-risk-pill">CRIME (C)</span>
              ...
            </div>
          </td>
          <td><p>MM/DD/YYYY</p></td>
        </tr>

    We lock onto the table by ``id="htmlTable"`` so peripheral tables (the
    megamenu, sidebars, etc.) can't pollute parser state, and we skip the
    ``<thead>`` row so its column labels never reach ``_flush_row``.

    Anything we can't parse is silently skipped — the State Dept rebuilds
    this page periodically and the exact markup drifts.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[dict] = []
        self._in_target_table = False
        self._in_thead = False
        self._in_tr = False
        self._in_cell = False
        self._cells: list[str] = []
        self._cur_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            if dict(attrs).get("id") == "htmlTable":
                self._in_target_table = True
        elif not self._in_target_table:
            return
        elif tag == "thead":
            self._in_thead = True
        elif tag == "tr" and not self._in_thead:
            self._in_tr = True
            self._cells = []
        elif tag in ("td", "th") and self._in_tr:
            self._in_cell = True
            self._cur_text = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "table" and self._in_target_table:
            self._in_target_table = False
        elif not self._in_target_table:
            return
        elif tag == "thead":
            self._in_thead = False
        elif tag == "tr" and self._in_tr:
            self._in_tr = False
            self._flush_row()
        elif tag in ("td", "th") and self._in_cell:
            self._in_cell = False
            self._cells.append(" ".join("".join(self._cur_text).split()))

    def handle_data(self, data: str) -> None:
        if self._in_cell:
            self._cur_text.append(data)

    def _flush_row(self) -> None:
        # Real data rows have 4 cells (name, level, risks, date). Tolerate 3
        # so a future redesign that collapses the risk-pills column still
        # ships dates and levels.
        if len(self._cells) < 3:
            return
        name = self._cells[0].strip()
        level_cell = self._cells[1].strip()
        if len(self._cells) >= 4:
            risks_cell = self._cells[2].strip()
            date_cell = self._cells[3].strip()
        else:
            risks_cell = ""
            date_cell = self._cells[-1].strip()

        if not name:
            return

        m_level = re.search(r"Level\s+([1-4])", level_cell, re.IGNORECASE)
        if not m_level:
            return
        level = int(m_level.group(1))

        # Each pill ends with "(X)" where X is a single uppercase letter.
        # Preserve scan order, dedupe defensively, drop anything outside the
        # known code set.
        risks: list[str] = []
        for m in re.finditer(r"\(([A-Z])\)", risks_cell):
            ch = m.group(1)
            if ch in VALID_RISK_CODES and ch not in risks:
                risks.append(ch)

        self.rows.append({
            "name": name,
            "level": level,
            "risks": risks,
            "date": _normalize_date(date_cell),
        })


def _normalize_date(s: str) -> str:
    """Parse 'May 21, 2026' / '2026-05-21' / 'May 21 2026' to 'YYYY-MM-DD'.
    Returns '' if unparseable so the row still ships with a blank date."""
    s = (s or "").strip()
    if not s:
        return ""
    # Already ISO?
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return s
    for fmt in ("%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%b %d %Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


def parse_advisory_table(html_str: str) -> list[dict]:
    """Pure function: parse the State Dept advisory-list HTML into rows.

    This is the unit-testable entry point — pass any HTML fragment containing
    one or more advisory <tr> rows and get back the parsed list (with `url`
    fields filled in). Used by the offline self-test in __main__ and by the
    live scrape path.
    """
    p = _AdvisoryTableParser()
    try:
        p.feed(html_str)
    except Exception as e:
        print(f"  [parse_advisory_table] {e}", file=sys.stderr)
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for row in p.rows:
        if row["name"] in seen:
            continue  # Defensive dedupe — page sometimes ships header echoes.
        seen.add(row["name"])
        row["url"] = build_country_url(row["name"])
        out.append(row)
    return out


# ----- RSS parser ------------------------------------------------------------

# Keyword cues for severity mapping. Order matters: red wins over amber.
_RED_KEYWORDS = (
    "do not travel", "level 4", "worldwide caution", "evacuation",
    "active conflict", "war zone", "level four",
)
_AMBER_KEYWORDS = (
    "reconsider", "level 3", "increased caution", "level 2",
    "exercise increased", "level three", "level two",
    "security alert", "demonstrations",
)


def _severity_from_text(title: str, body: str) -> str:
    """Map an RSS item's title+body to red/amber/green.

    Rule order:
      1. Any red-keyword hit  -> "red"
      2. Any amber-keyword hit -> "amber"
      3. Default               -> "green"
    Designed to be conservative: when in doubt we surface as informational
    (green) rather than spooking the user with a false-positive red bulletin.
    """
    text = f"{title} {body}".lower()
    if any(kw in text for kw in _RED_KEYWORDS):
        return "red"
    if any(kw in text for kw in _AMBER_KEYWORDS):
        return "amber"
    return "green"


def _tag_from_title(title: str) -> str:
    """Short tag (e.g. 'Worldwide Caution', 'Bahamas', 'L3 Reissue') derived
    from the title. Uses the prefix before the first ' - ' or ':'; falls back
    to first three words."""
    t = (title or "").strip()
    if not t:
        return "Bulletin"
    for sep in (" - ", " – ", ": ", " — "):
        if sep in t:
            head = t.split(sep, 1)[0].strip()
            if head:
                return head[:60]
    return " ".join(t.split()[:3])[:60]


def parse_advisory_rss(xml_str: str) -> list[dict]:
    """Pure function: parse the State Dept advisory RSS into bulletin rows.

    Returns items sorted newest first. RSS 2.0 only (the State Dept feed is
    RSS 2.0); if the feed flips to Atom we'd need a sibling parser like
    `ai_news_rss` in fetch_market.py does.
    """
    import xml.etree.ElementTree as ET
    if not xml_str:
        return []
    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError as e:
        print(f"  [parse_advisory_rss] xml parse: {e}", file=sys.stderr)
        return []
    out: list[dict] = []
    for it in root.findall(".//item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        pub = (it.findtext("pubDate") or "").strip()
        desc = (it.findtext("description") or "").strip()
        # Strip HTML from description for the body field.
        body = re.sub(r"<[^>]+>", "", desc).strip()[:600]
        # Parse pubDate -> YYYY-MM-DD.
        date_iso = ""
        ts = 0
        if pub:
            try:
                dt = parsedate_to_datetime(pub)
                if dt:
                    ts = int(dt.timestamp())
                    date_iso = dt.strftime("%Y-%m-%d")
            except Exception:
                pass
        if not title and not link:
            continue
        out.append({
            "tag": _tag_from_title(title),
            "severity": _severity_from_text(title, body),
            "date": date_iso,
            "title": title,
            "body": body,
            "href": link,
            "_ts": ts,  # internal sort key, popped below
        })
    out.sort(key=lambda x: x.get("_ts") or 0, reverse=True)
    for row in out:
        row.pop("_ts", None)
    return out


# ----- live fetch orchestration ---------------------------------------------

def fetch_live() -> dict | None:
    """Scrape both endpoints and assemble the output payload.

    Returns None if the advisory list comes back empty — caller is expected
    to treat that as a hard failure and preserve the prior good JSON file.
    """
    print("  Advisories: fetching travel.state.gov HTML...")
    html = _get(ADVISORY_LIST_URL)
    advisories: list[dict] = []
    if html:
        advisories = parse_advisory_table(html)
        print(f"    -> {len(advisories)} advisories parsed")
    else:
        print("    -> HTML fetch failed", file=sys.stderr)

    # Small polite gap before hitting the RSS endpoint.
    time.sleep(0.3)

    print("  Advisories: fetching State Dept RSS bulletins...")
    rss = _get(ADVISORY_RSS_URL)
    bulletins: list[dict] = []
    if rss:
        bulletins = parse_advisory_rss(rss)
        print(f"    -> {len(bulletins)} bulletins parsed")
    else:
        print("    -> RSS fetch failed", file=sys.stderr)

    if not advisories:
        # Hard fail — don't write a payload with an empty country list, even
        # if the bulletins came through. The dashboard tab is useless without
        # the country table and the spec explicitly says: never overwrite
        # with empty.
        return None

    return {
        "generated_at": _now_iso(),
        "advisories": advisories,
        "bulletins": bulletins,
    }


# ----- offline self-test fixture --------------------------------------------

# Snippet of the real travel.state.gov advisory table, saved on disk so it
# doubles as an artifact teams can inspect when the page redesigns again.
# Refresh with: curl <ADVISORY_LIST_URL> > /tmp/page.html, then snip the
# <table id="htmlTable">...</table> block down to a handful of rows.
_FIXTURE_PATH = ROOT / "tests" / "fixtures" / "advisories_sample.html"


def _self_test() -> int:
    """Offline parser sanity check. Returns 0 on pass, 1 on failure."""
    sample_html = _FIXTURE_PATH.read_text()
    rows = parse_advisory_table(sample_html)
    by_name = {r["name"]: r for r in rows}
    assertions = [
        (len(rows) == 6, f"expected 6 rows, got {len(rows)}"),
        ("Afghanistan" in by_name, "Afghanistan row missing"),
        (by_name["Afghanistan"]["level"] == 4,
         f"Afghanistan.level={by_name['Afghanistan'].get('level')!r}"),
        (set(by_name["Afghanistan"]["risks"]) == set("UCHKTDN"),
         f"Afghanistan.risks={by_name['Afghanistan'].get('risks')!r}"),
        (by_name["Afghanistan"]["date"] == "2026-02-20",
         f"Afghanistan.date={by_name['Afghanistan'].get('date')!r}"),
        (by_name["Albania"]["level"] == 2,
         f"Albania.level={by_name['Albania'].get('level')!r}"),
        (by_name["Albania"]["risks"] == ["C"],
         f"Albania.risks={by_name['Albania'].get('risks')!r}"),
        # Empty risk-pill container -> no risks.
        (by_name["Andorra"]["risks"] == [],
         f"Andorra.risks={by_name['Andorra'].get('risks')!r}"),
        (by_name["Andorra"]["level"] == 1,
         f"Andorra.level={by_name['Andorra'].get('level')!r}"),
        (by_name["Andorra"]["date"] == "2026-05-21",
         f"Andorra.date={by_name['Andorra'].get('date')!r}"),
        # Algeria has K then T in scan order; verify ordering is preserved.
        (by_name["Algeria"]["risks"] == ["K", "T"],
         f"Algeria.risks={by_name['Algeria'].get('risks')!r}"),
        # url field added by parse_advisory_table.
        (by_name["Afghanistan"]["url"].endswith("/afghanistan.html"),
         f"Afghanistan.url={by_name['Afghanistan'].get('url')!r}"),
        # slugify_country sanity
        (slugify_country("Côte d'Ivoire (Ivory Coast)") == "cote-divoire-ivory-coast",
         "Côte d'Ivoire slug override failed"),
        (slugify_country("Japan") == "japan", "Japan naive slug failed"),
        # RSS severity helper
        (_severity_from_text("Worldwide Caution", "...") == "red",
         "Worldwide Caution should be red"),
        (_severity_from_text("Reconsider travel to X", "Level 3") == "amber",
         "Level 3 + Reconsider should be amber"),
        (_severity_from_text("Demonstrations in X", "minor unrest") == "amber",
         "demonstrations cue should be amber"),
        (_severity_from_text("Routine update", "informational") == "green",
         "no keyword cue should be green"),
    ]
    failed = [msg for ok, msg in assertions if not ok]
    if failed:
        for f in failed:
            print(f"  [self-test FAIL] {f}", file=sys.stderr)
        return 1
    print(f"  [self-test OK] {len(rows)} rows parsed; all assertions passed.")
    return 0


# ----- CLI ------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Fetch U.S. State Dept travel advisories.")
    ap.add_argument("--out", default=str(DEFAULT_OUT),
                    help=f"Output JSON path (default: {DEFAULT_OUT})")
    ap.add_argument("--no-network", action="store_true",
                    help="Run offline parser self-test and exit (no HTTP).")
    args = ap.parse_args(argv)

    if args.no_network:
        return _self_test()

    out_path = Path(args.out)
    payload = fetch_live()
    if payload is None:
        # Fallback-to-last-good: preserve the existing file (if any), log
        # loudly, and exit non-zero so the caller knows the scrape failed.
        if out_path.exists():
            print(f"  [advisories] scrape failed; preserving prior "
                  f"{out_path} (no overwrite)", file=sys.stderr)
        else:
            print(f"  [advisories] scrape failed and no prior {out_path} to "
                  f"fall back on", file=sys.stderr)
        return 1

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2))
    print(f"  Wrote {out_path} ({len(payload['advisories'])} advisories, "
          f"{len(payload['bulletins'])} bulletins)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
