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
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import NamedTuple

import requests


# travel.state.gov sits behind Akamai and 403s bot-shaped User-Agents.
# api_status.py has always probed this exact host with a full browser UA for
# that reason ("Akamai-fronted, so send a browser UA to avoid a bot 403"),
# while this module — the one doing the real work — sent
# "Mozilla/5.0 (compatible; etf-flow-dashboard/1.0)". Two files in this repo
# disagreed about what the host requires; this is the one that mattered.
# Keep in sync with api_status.py's "State Dept advisories" probe.
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/17.0 Safari/605.1.15")
H = {"User-Agent": UA}
ROOT = Path(__file__).parent
DEFAULT_OUT = ROOT / "v2" / "data-travel.json"

# One retry with a short backoff. Akamai occasionally serves a transient
# 403/503 to a cold connection; one retry costs ~2s and removes the cheapest
# failure mode. Anything beyond that is a real outage, not a blip.
HTTP_RETRIES = 1
RETRY_BACKOFF_SEC = 2.0
# Statuses where waiting actually helps. A 404 is not one of them.
RETRYABLE_STATUS = {403, 408, 429, 500, 502, 503, 504}

ADVISORY_LIST_URL = "https://travel.state.gov/en/international-travel/travel-advisories.html"
# Canonical State Dept RSS feed for security alerts + advisory-level changes.
# (Also available at /content/travel/en/_jcr_content.xy.html but this is the
# documented feed URL surfaced in the front-end of travel.state.gov.)
ADVISORY_RSS_URL = "https://travel.state.gov/_res/rss/TAsTWs.xml"

# Valid State Dept risk indicator code letters. Anything outside this set is
# dropped by the parser (defensive against stray punctuation in cells).
VALID_RISK_CODES = set("TCUHKNDOE")


# ----- helpers ---------------------------------------------------------------

class FetchResult(NamedTuple):
    """Outcome of one HTTP GET, with enough detail to name the failure.

    The old `_get` returned `str | None`, which collapsed an Akamai 403, a DNS
    failure, a timeout and an HTTP-200-but-empty body into the same `None`.
    Downstream that became one indistinguishable line — "scrape failed;
    preserving prior" — and data-travel.json then sat frozen for 68 days
    because nothing in the build log could say WHICH of those had happened.
    Every field here exists to answer that question in one line.
    """
    url: str
    text: str | None          # body on HTTP 200, else None
    status: int | None        # HTTP status, or None if no response at all
    error: str = ""           # transport-layer error (DNS/TLS/timeout/proxy)
    nbytes: int = 0           # size of the body we got, even on non-200
    body_snippet: str = ""    # first ~200 chars of the body, incl. error pages
    attempts: int = 0

    @property
    def ok(self) -> bool:
        return self.status == 200 and bool(self.text)

    def describe(self) -> str:
        """One-line human summary — this is what lands in the build log."""
        if self.status is None:
            return f"no HTTP response ({self.error or 'unknown transport error'})"
        bit = f"HTTP {self.status}, {self.nbytes} bytes"
        if self.status != 200 and self.body_snippet:
            bit += f', body="{self.body_snippet}"'
        elif self.status == 200 and not self.nbytes:
            bit += " (empty body)"
        return bit


def _decode_response(r) -> str:
    """Body text decoded with the charset the upstream actually used.

    `requests` follows RFC 2616 and falls back to ISO-8859-1 for ANY `text/*`
    response that omits a charset parameter. The State Dept RSS is served as
    `text/xml` with no charset, so every multi-byte UTF-8 sequence in it came
    back decoded one byte at a time: NBSP (C2 A0) became "Â\xa0" and the curly
    apostrophe (E2 80 99) became "â\x80\x99". That is why the committed
    data-travel.json contains "Reconsider travel due toÂ terrorism" — the
    mojibake is baked into the stored strings, not introduced at render time.

    The body's own XML/HTML declaration says UTF-8, so try that first and only
    fall back to requests' header-derived guess if the bytes genuinely are not
    UTF-8. `utf-8-sig` so a BOM is consumed rather than left as a stray \\ufeff
    in front of the XML declaration (ElementTree rejects that).
    """
    if "charset=" not in (r.headers.get("Content-Type") or "").lower():
        try:
            return (r.content or b"").decode("utf-8-sig")
        except (UnicodeDecodeError, AttributeError):
            pass  # not UTF-8 after all — let requests decide below
    return r.text or ""


def _get(url: str, timeout: int = 25, retries: int = HTTP_RETRIES) -> FetchResult:
    """GET with a browser UA. NEVER raises; always returns a FetchResult.

    Returns a result object rather than `str | None` so the caller can report
    the HTTP status and the upstream's own error body instead of just "failed".
    """
    status: int | None = None
    error = ""
    nbytes = 0
    snippet = ""
    attempt = 0
    for attempt in range(1, retries + 2):
        try:
            r = requests.get(url, headers=H, timeout=timeout)
            status = r.status_code
            body = _decode_response(r)
            nbytes = len(body)
            snippet = " ".join(body.split())[:200]
            if status == 200:
                return FetchResult(url, body, status, "", nbytes, snippet, attempt)
            error = ""
            print(f"  [skip] {url} -> HTTP {status} ({nbytes} bytes)"
                  + (f' body="{snippet[:120]}"' if snippet else ""), file=sys.stderr)
            if status not in RETRYABLE_STATUS or attempt > retries:
                break
        except Exception as e:
            status = None
            error = f"{type(e).__name__}: {e}"
            nbytes, snippet = 0, ""
            print(f"  [skip] {url} -> {error}", file=sys.stderr)
            if attempt > retries:
                break
        print(f"  [retry {attempt}/{retries}] {url} in {RETRY_BACKOFF_SEC:.0f}s",
              file=sys.stderr)
        time.sleep(RETRY_BACKOFF_SEC)
    return FetchResult(url, None, status, error, nbytes, snippet, attempt)


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
    # The second replace was a duplicate of the first (both plain ASCII), so
    # the curly apostrophe it was clearly meant to catch fell through. That
    # never showed while RSS country names arrived as "d&#8217;Ivoire"; now
    # that entities are decoded, U+2019 reaches this function for real.
    no_apos = ascii_.replace("'", "").replace("\u2019", "")
    slug = re.sub(r"[^A-Za-z0-9]+", "-", no_apos).strip("-").lower()
    return slug


def build_country_url(name: str) -> str:
    return f"https://travel.state.gov/en/international-travel/travel-advisories/{slugify_country(name)}.html"


# ----- HTML table parser -----------------------------------------------------

class _AdvisoryTableParser(HTMLParser):
    """Stateful HTMLParser that pulls the advisory rows out of the State Dept
    advisory-list page.

    Page layout (current as of 2026-05-28; State Dept migrated to U.S. Web
    Design System sometime between 2026-05-26 and 2026-05-28):

        <table data-table-type="structTable"
               class="usa-table usa-table--destination ...">
          ...
          <tbody>
            <tr>
              <th scope="row"><a ...>Country Name</a></th>
              <td><p class="level-title level-title-N">Level N: ...</p></td>
              <td>
                <div class="tsg-utility-risk-pill-container">
                  <span class="tsg-utility-risk-pill">UNREST (U)</span>
                  <span class="tsg-utility-risk-pill">CRIME (C)</span>
                  ...
                </div>
              </td>
              <td><p>MM/DD/YYYY</p></td>
            </tr>

    Legacy layout (pre-migration; used by the offline self-test fixture):

        <table id="htmlTable">
          ...
          <tr>
            <th scope="row"><a ...>Country Name</a></th>
            <td><p><span class="level-badge level-badge-N"></span>Level N: ...</p></td>
            ...same 4-cell shape...
          </tr>

    The tbody row layout is identical across both — only the wrapping
    ``<table>`` identifier changed. We lock onto the table on EITHER
    ``id="htmlTable"`` (legacy) OR ``class`` containing
    ``usa-table--destination`` (current USWDS layout) so peripheral tables
    (megamenu, sidebars, footer link-grids, etc.) can't pollute parser
    state. We skip the ``<thead>`` row so its column labels never reach
    ``_flush_row``.

    Anything we can't parse is silently skipped — the State Dept rebuilds
    this page periodically and the exact markup drifts. See the May-2026
    migration note above for the most recent drift.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[dict] = []
        # --- markup diagnostics -------------------------------------------
        # When the page redesigns, `rows` silently becomes []. These three
        # fields are what tell the next reader whether the LOCK failed (the
        # table's identifying class/id changed) or the ROW SHAPE failed (we
        # locked on but the cells no longer parse). Without them both look
        # identical from the build log, which is how a parser break got
        # misfiled as an IP block for 68 days.
        self.lock_engaged = False          # did we ever find the target table?
        self.tables_seen = 0               # how many <table>s on the page at all
        self.table_ids: list[str] = []     # their id / class signatures
        self.rows_locked = 0               # <tr>s seen inside the target table
        self._in_target_table = False
        self._in_thead = False
        self._in_tr = False
        self._in_cell = False
        self._cells: list[str] = []
        self._cur_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            attrs_d = dict(attrs)
            self.tables_seen += 1
            sig = (attrs_d.get("id") and f"id={attrs_d['id']}") or \
                  (attrs_d.get("class") and f"class={attrs_d['class']}") or "<no id/class>"
            if len(self.table_ids) < 12:
                self.table_ids.append(str(sig)[:80])
            # Legacy id-based lock (kept for the offline test fixture and any
            # cached pre-migration HTML the user might pass in).
            if attrs_d.get("id") == "htmlTable":
                self._in_target_table = True
            else:
                # Current USWDS layout: lock on the destination-table class.
                # split() handles the multi-class string defensively (other
                # tables on the page like usa-table--striped wouldn't match
                # without the --destination modifier).
                cls = (attrs_d.get("class") or "").split()
                if "usa-table--destination" in cls:
                    self._in_target_table = True
            if self._in_target_table:
                self.lock_engaged = True
        elif not self._in_target_table:
            return
        elif tag == "thead":
            self._in_thead = True
        elif tag == "tr" and not self._in_thead:
            self._in_tr = True
            self.rows_locked += 1
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


def parse_advisory_table(html_str: str, diag: dict | None = None) -> list[dict]:
    """Pure function: parse the State Dept advisory-list HTML into rows.

    This is the unit-testable entry point — pass any HTML fragment containing
    one or more advisory <tr> rows and get back the parsed list (with `url`
    fields filled in). Used by the offline self-test in __main__ and by the
    live scrape path.

    ``diag``: optional dict, filled in with markup diagnostics (whether the
    table lock engaged, how many tables the page had and what identified them,
    how many <tr>s were inside the locked table). A caller that gets 0 rows
    reads this to learn WHY — lock never engaged (the table's identifier
    changed) vs. locked but rows didn't parse (the cell shape changed).
    """
    p = _AdvisoryTableParser()
    parse_error = ""
    try:
        p.feed(html_str)
    except Exception as e:
        parse_error = f"{type(e).__name__}: {e}"
        print(f"  [parse_advisory_table] {parse_error}", file=sys.stderr)
    if diag is not None:
        diag.update({
            "lock_engaged": p.lock_engaged,
            "tables_seen": p.tables_seen,
            "table_signatures": p.table_ids,
            "rows_inside_locked_table": p.rows_locked,
            "rows_parsed": len(p.rows),
            "html_mentions_usa_table": "usa-table" in (html_str or ""),
            "html_mentions_htmlTable": "htmlTable" in (html_str or ""),
            "parse_error": parse_error,
        })
    if parse_error:
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


# ----- bulletin filter -------------------------------------------------------
#
# The State Dept "TAsTWs.xml" feed publishes ~215 items: one per country
# advisory. The vast majority of these are routine periodic-review reissues
# ("Reissued after periodic review with minor edits", "There are no changes
# to the advisory level...") that should NOT flood the dashboard's "Latest
# Bulletins" panel — those are scheduled republishes, not news.
#
# A real bulletin is one of:
#   (a) Explicit advisory-level change ("The advisory level was increased
#       to 3", "shift to Level 2", "change in overall travel advisory level").
#   (b) Ordered or authorized departure of U.S. government personnel — a
#       very strong "things are bad enough that we're pulling our people"
#       signal.
#   (c) Substantive content rewrite ("Updated to reflect ...") — these
#       always describe a specific change (embassy ops, new threat, etc.).
#
# Plus a level filter: Level 1 ("Exercise Normal Precautions") items are
# dropped UNLESS the change was a meaningful level decrease (e.g. Vanuatu
# from L3 -> L1, which is genuinely newsworthy).
#
# We only look at the first 400 chars of plaintext description — the State
# Dept convention is to put the change-summary header in the lead bold/italic
# paragraph, so we don't need to scan the full body (which would false-match
# on routine boilerplate like "If you decide to travel ... avoid demonstrations").
#
# Empirical: this drops ~215 raw items to ~20-25 genuine bulletins on the
# live feed (May 2026).

_LEVEL_CHANGE_RE = re.compile(
    r"(advisory level (was|has been) (decreased|increased|raised|lowered)"
    r"|advisory level (increased|decreased) from level"
    r"|reissued after periodic review with changes to overall"
    r"|change in overall travel advisory level"
    r"|shift to level"
    r"|lowering the travel advisory level"
    r"|raising the travel advisory level"
    r"|raised the travel advisory level"
    r"|lowered the travel advisory level)",
    re.IGNORECASE,
)
# Ordered/authorized departure can be phrased as either:
#   "the [...] ordered departure of [...]"
#   "the [...] ordered non-emergency US government employees [...] to leave"
# We accept both shapes — the latter is canonical State Dept language for
# a fresh departure declaration in the alert body.
_DEPARTURE_RE = re.compile(
    r"((ordered|authorized) departure"
    r"|ordered (non-emergency )?u\.?s\.? government (employees|personnel)"
    r"|ordered (?:non-emergency )?(?:family members|eligible family))",
    re.IGNORECASE,
)
# "Updated to reflect" may have NBSP / whitespace between the words in the
# State Dept feed; tolerate both.
_UPDATED_REFLECT_RE = re.compile(r"updated to(\s|\xa0)+reflect", re.IGNORECASE)
# Words that signal a Level-DECREASE specifically (used to gate L1 items —
# only L1 advisories whose body explicitly says "lowered from L3" / similar
# are kept; we don't surface routine L1 risk-indicator tweaks).
_LEVEL_DECREASE_RE = re.compile(r"(lower|decreas)", re.IGNORECASE)
_TITLE_LEVEL_RE = re.compile(r"Level\s+(\d)")


def _is_bulletin(title: str, body: str) -> bool:
    """Return True if an RSS item is genuine bulletin-worthy news.

    Filters out the ~200 routine per-country periodic-review reissues that
    pollute the State Dept RSS feed. Pure function — easily unit-testable.
    """
    head = (body or "")[:400]
    has_level_change = bool(_LEVEL_CHANGE_RE.search(head))
    has_departure = bool(_DEPARTURE_RE.search(head))
    has_updated_reflect = bool(_UPDATED_REFLECT_RE.search(head))
    if not (has_level_change or has_departure or has_updated_reflect):
        return False
    m = _TITLE_LEVEL_RE.search(title or "")
    level = int(m.group(1)) if m else 0
    if level == 1:
        # Only keep L1 items if they represent a genuine level-DECREASE
        # (e.g. country recovered from a higher advisory). Other L1 churn
        # — risk indicator tweaks, summary refreshes — is too low-signal
        # for a security-focused bulletins panel.
        return bool(_LEVEL_DECREASE_RE.search(head)) and (
            has_level_change or has_updated_reflect
        )
    return True


def _parse_pubdate(pub: str) -> tuple[str, int]:
    """Parse an RSS pubDate to ('YYYY-MM-DD', unix_ts). ('', 0) if hopeless.

    The State Dept feed is inconsistent: the channel-level pubDate is a full
    RFC-2822 stamp ("Mon, 25 May 2026 17:32:05 GMT") but most per-country items
    omit the time entirely ("Mon, 02 Mar 2026"). `parsedate_to_datetime` raises
    ValueError on the time-less form, and the old handler swallowed it while
    printing only the exception's TYPE NAME — so every bulletin shipped with
    date:"" (confirmed: 37/37 in production data-travel.json) and the
    newest-first sort at the end of parse_advisory_rss silently degraded to
    feed order because every _ts was 0. Retry the date-only form with a
    midnight-UTC suffix before giving up, and log the offending STRING.
    """
    pub = (pub or "").strip()
    if not pub:
        return "", 0
    for candidate in (pub, f"{pub} 00:00:00 +0000"):
        try:
            dt = parsedate_to_datetime(candidate)
        except Exception:
            continue
        if not dt:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.strftime("%Y-%m-%d"), int(dt.timestamp())
    print(f"  [parse_advisory_rss] unparseable pubDate: {pub!r}", file=sys.stderr)
    return "", 0


# ----- entity decoding -------------------------------------------------------
#
# The State Dept RSS carries its bodies DOUBLE-encoded. A <description> holds
# an HTML fragment, and that fragment is itself XML-escaped inside the feed, so
# the raw bytes look like:
#
#     &lt;p&gt;The &amp;#8220;Unrest&amp;#8221; risk indicator was removed.&lt;/p&gt;
#
# ElementTree undoes the XML layer — that is what parsing XML means — leaving
# an ordinary HTML fragment:
#
#     <p>The &#8220;Unrest&#8221; risk indicator was removed.</p>
#
# The old parser stripped the tags and stored whatever was left VERBATIM, so
# `&#8220;Unrest&#8221;` and `risk of&nbsp;crime` went into data-travel.json as
# literal entity text. The dashboard then HTML-escapes every string it renders
# (correctly — it must, or the feed could inject markup), which escapes the
# ampersand a second time and puts the entity source on screen.
#
# The fix is one — EXACTLY one — html.unescape() here, at parse time, so the
# stored text is real characters and the renderer's escaping is a no-op.
#
# EXACTLY ONE, never "unescape until it stops changing": text that is meant to
# display the four characters "&lt;" arrives from the feed as "&amp;amp;lt;",
# becomes "&amp;lt;" after ElementTree, and must end up as "&lt;". A second
# pass would eat it into "<" — corrupting the text and handing the renderer a
# tag opener. `_decode_entities` is the only place that calls html.unescape;
# do not stack another one on top of it.

_TAG_RE = re.compile(r"<[^>]+>")

# Block-level tags mark a BOUNDARY between two runs of text; inline tags do
# not. Deleting both with no separator fuses the words either side, which is
# how the committed feed ended up saying "Do not travelto Lebanon" and
# "Reconsider traveldue to terrorism" — State Dept writes the advisory verb
# and its object in adjacent <p> blocks.
#
# Inline tags (<b>, <em>, <a>, <span>...) must still vanish WITHOUT a
# separator, or "<b>tra</b>vel" would come apart into "tra vel". So the two
# classes are handled separately rather than with one blanket substitution.
_BLOCK_TAG_RE = re.compile(
    r"</?(?:p|div|br|li|ul|ol|h[1-6]|tr|td|th|table|thead|tbody|section"
    r"|article|header|footer|aside|nav|blockquote|hr|dl|dt|dd|pre|figure"
    r"|figcaption|form|fieldset|address|main)\b[^>]*>",
    re.IGNORECASE,
)

# Space characters the feed emits, as entities (&nbsp;, &#8239;) or raw. They
# are real characters, but unlike an ordinary space they do NOT collapse when
# rendered, so the feed's habit of padding a heading with eighteen consecutive
# NBSPs would land on screen as an eighteen-space hole. Fold them to a space.
# Spelled as escapes, not literals, so they stay visible to the next reader:
# U+00A0 NBSP, U+2007 figure space, U+2009 thin space, U+202F narrow NBSP.
_NBSP_RE = re.compile("[\u00a0\u2007\u2009\u202f]")


def _decode_entities(s: str) -> str:
    """Decode HTML entities exactly once, then fold NBSP-family spaces.

    See the block comment above for why this is once and only once.
    """
    if not s:
        return ""
    return _NBSP_RE.sub(" ", html.unescape(s))


def _plain_text(s: str) -> str:
    """Tidy a NON-markup RSS field (e.g. <title>): entities, spaces, strip.

    Deliberately does not strip tags — a "<" in a title is a "<", not the
    start of markup we should delete.
    """
    return re.sub(r"[ \t]+", " ", _decode_entities(s)).strip()


def _html_fragment_to_text(fragment: str, limit: int | None = None) -> str:
    """Turn the HTML fragment inside an RSS <description> into plain text.

    Order matters: tags are stripped FIRST and entities decoded SECOND. The
    other way round would turn a legitimate "&lt;p&gt;" in the visible text
    into a real <p> and then delete it.

    `limit` is applied last, after decoding, so the cap is a count of the
    characters a reader actually sees and can never slice an entity in half.
    """
    if not fragment:
        return ""
    # Block tags become a newline BEFORE the blanket strip, so the words they
    # separated stay separated. Inline tags then vanish with no separator.
    stripped = _TAG_RE.sub("", _BLOCK_TAG_RE.sub("\n", fragment))
    text = _decode_entities(stripped)
    # Collapse horizontal whitespace per line but keep the newlines: after the
    # tags are gone, paragraph breaks are the only structure the body has left.
    text = "\n".join(re.sub(r"[ \t]+", " ", ln).strip() for ln in text.split("\n"))
    # Collapse newline RUNS to a single break. Nested blocks (<div><p>) and a
    # literal newline already followed by a <p> both emit two, and the reader
    # means one break in each case. Single-newline paragraphs also matter on a
    # phone, where a blank line between every paragraph is wasted height.
    text = re.sub(r"\n{2,}", "\n", text)
    text = text.strip()
    return text[:limit] if limit is not None else text


def advisories_from_rss(xml_str: str) -> list[dict]:
    """Fallback: derive the per-country advisory list from the RSS feed.

    The HTML advisory table is a single point of failure — one State Dept
    redesign (as happened 2026-05-26) and the parser returns 0 rows, which
    `fetch_live` treats as a hard failure, which freezes the whole feed
    including the bulletins that fetched fine.

    The same RSS feed we already fetch for bulletins carries one item per
    country titled "<Country> - Level <N>: <label>", so it can rebuild the
    country/level list without touching the HTML page at all. What it cannot
    supply is the risk-indicator codes (U/C/H/K/...), so `risks` comes back
    empty — a shape the schema already supports (see the Andorra self-test
    case). Degrading the risk pills beats freezing the entire tab.
    """
    import xml.etree.ElementTree as ET
    if not xml_str:
        return []
    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError as e:
        print(f"  [advisories_from_rss] xml parse: {e}", file=sys.stderr)
        return []
    title_re = re.compile(r"^(.*?)\s+[-–—]\s+Level\s+([1-4])\b", re.IGNORECASE)
    out: list[dict] = []
    seen: set[str] = set()
    for it in root.findall(".//item"):
        # Entity-decoded before the regex: an accented country name arrives
        # from this feed as "C&#244;te d&#8217;Ivoire" and must be a real name
        # before it reaches slugify_country() and the dashboard.
        title = _plain_text(it.findtext("title") or "")
        m = title_re.match(title)
        if not m:
            continue
        name = m.group(1).strip()
        if not name or name in seen:
            continue
        seen.add(name)
        date_iso, _ = _parse_pubdate((it.findtext("pubDate") or "").strip())
        out.append({
            "name": name,
            "level": int(m.group(2)),
            "risks": [],          # not carried by the RSS feed
            "date": date_iso,
            "url": build_country_url(name),
        })
    return out


def parse_advisory_rss(xml_str: str) -> list[dict]:
    """Pure function: parse the State Dept advisory RSS into bulletin rows.

    The State Dept feed mixes ~5-25 genuine alerts (level changes, ordered
    departures, substantive rewrites) with ~190 routine periodic-review
    reissues. This parser filters down to the genuine alerts via
    `_is_bulletin`; see that helper for the rule definition.

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
    seen_titles: set[str] = set()
    for it in root.findall(".//item"):
        title = _plain_text(it.findtext("title") or "")
        link = (it.findtext("link") or "").strip()
        pub = (it.findtext("pubDate") or "").strip()
        desc = (it.findtext("description") or "").strip()
        # Strip HTML from the description, then decode its entities ONCE, so
        # the stored body is the text a reader is meant to see rather than
        # "&#8220;Unrest&#8221;" / "risk of&nbsp;crime". See _decode_entities.
        body = _html_fragment_to_text(desc, limit=600)
        if not title and not link:
            continue
        # Filter to bulletin-worthy items only.
        if not _is_bulletin(title, body):
            continue
        # Defensive dedupe — feed has been observed to publish the same
        # title twice (e.g. Mainland China appeared as two consecutive
        # items in the May 2026 snapshot).
        if title in seen_titles:
            continue
        seen_titles.add(title)
        # Parse pubDate -> YYYY-MM-DD.
        date_iso, ts = _parse_pubdate(pub)
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

def _endpoint_status(r: FetchResult) -> dict:
    return {
        "url": r.url,
        "http_status": r.status,
        "bytes": r.nbytes,
        "error": r.error,
        "attempts": r.attempts,
        "body_snippet": "" if r.ok else r.body_snippet,
    }


def fetch_live(status: dict | None = None) -> dict | None:
    """Scrape both endpoints and assemble the output payload.

    Returns None if the advisory list comes back empty from BOTH the HTML
    table and the RSS fallback — caller treats that as a hard failure and
    preserves the prior good JSON file.

    ``status``: optional dict, always populated with a machine-readable record
    of what each endpoint did. main() writes it to disk whether the run
    succeeded or failed, because the whole reason this feed froze unnoticed
    for 68 days is that the answer to "was it a 403 or a parser break?"
    existed only as two stdout lines in a discarded build log.
    """
    if status is None:
        status = {}
    status.setdefault("checked_at", _now_iso())
    status["user_agent"] = UA

    print("  Advisories: fetching travel.state.gov HTML...")
    html_r = _get(ADVISORY_LIST_URL)
    status["html"] = _endpoint_status(html_r)
    advisories: list[dict] = []
    table_diag: dict = {}
    if html_r.ok:
        advisories = parse_advisory_table(html_r.text, diag=table_diag)
        print(f"    -> {len(advisories)} advisories parsed "
              f"({html_r.nbytes} bytes, lock_engaged="
              f"{table_diag.get('lock_engaged')})")
    else:
        print(f"    -> HTML fetch failed: {html_r.describe()}", file=sys.stderr)
    status["html"].update(table_diag)

    # HTTP 200 but zero rows is the parser-break signature, and it is the one
    # case a human needs markup detail for. Print it here rather than dumping
    # a file: this must survive in a build log, not in an untracked artifact.
    if html_r.ok and not advisories:
        print(f"    -> PARSE BREAK: {html_r.nbytes} bytes of HTML, "
              f"{table_diag.get('tables_seen', 0)} <table>s, "
              f"lock_engaged={table_diag.get('lock_engaged')}, "
              f"rows_inside_locked_table={table_diag.get('rows_inside_locked_table')}, "
              f"page mentions 'usa-table'={table_diag.get('html_mentions_usa_table')}. "
              f"Tables on page: {table_diag.get('table_signatures')}", file=sys.stderr)

    # Small polite gap before hitting the RSS endpoint.
    time.sleep(0.3)

    print("  Advisories: fetching State Dept RSS bulletins...")
    rss_r = _get(ADVISORY_RSS_URL)
    status["rss"] = _endpoint_status(rss_r)
    bulletins: list[dict] = []
    if rss_r.ok:
        bulletins = parse_advisory_rss(rss_r.text)
        print(f"    -> {len(bulletins)} bulletins parsed")
    else:
        print(f"    -> RSS fetch failed: {rss_r.describe()}", file=sys.stderr)

    source = "html"
    if not advisories and rss_r.ok:
        # The HTML table is gone or unparseable but the RSS feed answered.
        # Rebuild the country list from it rather than freezing the whole tab
        # (see advisories_from_rss for what this loses: the risk pills).
        advisories = advisories_from_rss(rss_r.text)
        if advisories:
            source = "rss-fallback"
            print(f"    -> RSS FALLBACK: rebuilt {len(advisories)} advisories from "
                  f"the RSS feed because the HTML table yielded 0 rows. "
                  f"Risk-indicator pills are unavailable in this mode.",
                  file=sys.stderr)

    status["rss"]["bulletins_parsed"] = len(bulletins)
    status["advisories"] = len(advisories)
    status["source"] = source if advisories else None

    if not advisories:
        # Hard fail — don't write a payload with an empty country list, even
        # if the bulletins came through. The dashboard tab is useless without
        # the country table and the spec explicitly says: never overwrite
        # with empty.
        status["ok"] = False
        return None

    status["ok"] = True
    return {
        "generated_at": _now_iso(),
        # Which path produced the country list. "rss-fallback" means the HTML
        # table broke and `risks` is empty for every row — visible in the data
        # itself, not just in a log line nobody reads.
        "source": source,
        "advisories": advisories,
        "bulletins": bulletins,
    }


# ----- offline self-test fixture --------------------------------------------

# Snippet of the real travel.state.gov advisory table, saved on disk so it
# doubles as an artifact teams can inspect when the page redesigns again.
# Refresh with: curl <ADVISORY_LIST_URL> > /tmp/page.html, then snip the
# <table id="htmlTable">...</table> block down to a handful of rows.
_FIXTURE_PATH = ROOT / "tests" / "fixtures" / "advisories_sample.html"

# Trimmed snapshot of the live State Dept TAsTWs RSS feed (11 hand-picked items
# — 6 bulletin-worthy, 5 routine reissues). Lets the parser bulletin filter
# be exercised offline. Refresh with:
#   curl -A "<UA>" "<ADVISORY_RSS_URL>" > /tmp/full.xml
# then pluck a representative subset.
_RSS_FIXTURE_PATH = ROOT / "tests" / "fixtures" / "advisories_rss_sample.xml"

# The USWDS layout that travel.state.gov migrated to on ~2026-05-26 — i.e. the
# branch that ACTUALLY RUNS in production. The on-disk fixture above is still
# the pre-migration `<table id="htmlTable">` snapshot, so before this existed
# `--no-network` reported "self-test OK" while covering none of the live code
# path. That green check is how an unverified markup guess shipped and froze
# the feed for 68 days.
#
# NOTE: this is built to the layout documented in _AdvisoryTableParser's
# docstring, which was itself written from a description rather than from a
# captured page. It proves the branch is self-consistent; it does NOT prove
# the real page looks like this. The `table_signatures` diagnostic in
# _diagnose() is what will settle that on the next CI run.
_USWDS_SAMPLE = """
<div class="usa-section">
  <table class="usa-table usa-table--borderless">
    <tbody><tr><th scope="row">Decoy nav table</th><td>Level 9</td></tr></tbody>
  </table>
  <table data-table-type="structTable" class="usa-table usa-table--destination usa-table--striped">
    <thead><tr><th>Country</th><th>Advisory Level</th><th>Risk Indicators</th><th>Date</th></tr></thead>
    <tbody>
      <tr>
        <th scope="row"><a href="/afghanistan.html">Afghanistan</a></th>
        <td><p class="level-title level-title-4">Level 4: Do Not Travel</p></td>
        <td><div class="tsg-utility-risk-pill-container">
              <span class="tsg-utility-risk-pill">UNREST (U)</span>
              <span class="tsg-utility-risk-pill">CRIME (C)</span>
            </div></td>
        <td><p>02/20/2026</p></td>
      </tr>
      <tr>
        <th scope="row"><a href="/albania.html">Albania</a></th>
        <td><p class="level-title level-title-2">Level 2: Exercise Increased Caution</p></td>
        <td><div class="tsg-utility-risk-pill-container">
              <span class="tsg-utility-risk-pill">CRIME (C)</span>
            </div></td>
        <td><p>12/31/2024</p></td>
      </tr>
    </tbody>
  </table>
</div>
"""


def _self_test() -> int:
    """Offline parser sanity check. Returns 0 on pass, 1 on failure."""
    sample_html = _FIXTURE_PATH.read_text()
    legacy_diag: dict = {}
    rows = parse_advisory_table(sample_html, diag=legacy_diag)
    by_name = {r["name"]: r for r in rows}

    # USWDS (current live layout) — the branch the legacy fixture never touches.
    uswds_diag: dict = {}
    uswds_rows = parse_advisory_table(_USWDS_SAMPLE, diag=uswds_diag)
    uswds_by_name = {r["name"]: r for r in uswds_rows}

    # RSS-derived advisory fallback (used when the HTML table breaks).
    rss_advisories = advisories_from_rss(_RSS_FIXTURE_PATH.read_text())
    rss_adv_by_name = {r["name"]: r for r in rss_advisories}

    # RSS bulletin-filter fixture round-trip.
    sample_rss = _RSS_FIXTURE_PATH.read_text()
    bulletins = parse_advisory_rss(sample_rss)
    bulletin_titles = [b["title"] for b in bulletins]
    bulletin_country_tags = {t.split(" - ")[0] for t in bulletin_titles}

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

        # --- RSS bulletin filter ---
        # Fixture has 11 items; filter should keep ~6 (within the documented
        # 5-20 ballpark).
        (1 <= len(bulletins) <= 20,
         f"bulletins count {len(bulletins)} outside 1..20 ballpark"),
        # Known-good bulletins (level change / ordered departure / Updated
        # to reflect substantive content) must survive.
        ("Bahrain" in bulletin_country_tags,
         f"expected Bahrain (ordered departure) in bulletins, got "
         f"{sorted(bulletin_country_tags)}"),
        ("United Arab Emirates" in bulletin_country_tags,
         "expected UAE (ordered departure) in bulletins"),
        ("Mozambique" in bulletin_country_tags,
         "expected Mozambique (level 3->2 change) in bulletins"),
        ("Cyprus" in bulletin_country_tags,
         "expected Cyprus (level increased to 3) in bulletins"),
        ("Greenland" in bulletin_country_tags,
         "expected Greenland (Updated to reflect new advisory) in bulletins"),
        ("Vanuatu" in bulletin_country_tags,
         "expected Vanuatu (L3 -> L1 decrease) in bulletins"),
        # Routine periodic-review reissues with no real news content must
        # be filtered OUT.
        ("British Virgin Islands" not in bulletin_country_tags,
         "BVI (no-change reissue) should be filtered out"),
        ("Anguilla" not in bulletin_country_tags,
         "Anguilla (no-change reissue) should be filtered out"),
        ("Mongolia" not in bulletin_country_tags,
         "Mongolia (reissued-without-changes) should be filtered out"),
        ("Armenia" not in bulletin_country_tags,
         "Armenia (reissued-with-minor-edits) should be filtered out"),
        # _is_bulletin unit checks.
        (_is_bulletin("X - Level 3", "Updated to reflect ordered departure of personnel."),
         "_is_bulletin should accept ordered-departure"),
        (_is_bulletin("X - Level 2", "The advisory level was increased to 2. ..."),
         "_is_bulletin should accept explicit level change"),
        (not _is_bulletin("X - Level 1", "Reissued after periodic review with minor edits."),
         "_is_bulletin should reject minor-edits reissue"),
        (not _is_bulletin("X - Level 2", "There are no changes to the advisory level or risk indicators."),
         "_is_bulletin should reject no-changes notice"),

        # --- entity decoding (bulletins shipped literal "&#8220;Unrest&#8221;") ---
        # Post-ElementTree the description is an HTML fragment; one unescape
        # turns it into the text a reader is meant to see.
        (_html_fragment_to_text("<p>The &#8220;Unrest&#8221; indicator</p>")
         == "The “Unrest” indicator",
         f"curly-quote entities not decoded: "
         f"{_html_fragment_to_text('<p>The &#8220;Unrest&#8221; indicator</p>')!r}"),
        (_html_fragment_to_text("risk of&nbsp;crime,&nbsp;terrorism")
         == "risk of crime, terrorism",
         f"&nbsp; not folded: "
         f"{_html_fragment_to_text('risk of&nbsp;crime,&nbsp;terrorism')!r}"),
        (_html_fragment_to_text("travel&#8239;to Cyprus") == "travel to Cyprus",
         "&#8239; (narrow NBSP) not folded"),
        # ONCE, not until-stable: "&amp;lt;" is text that must render as
        # "&lt;". A second unescape would corrupt it into "<".
        (_html_fragment_to_text("Use &amp;lt;brackets&amp;gt; here")
         == "Use &lt;brackets&gt; here",
         f"double-unescaped: "
         f"{_html_fragment_to_text('Use &amp;lt;brackets&amp;gt; here')!r}"),
        (_plain_text("Turks &amp; Caicos") == "Turks & Caicos",
         "&amp; in a title should decode once to &"),
        (_plain_text("C&#244;te d&#8217;Ivoire") == "Côte d’Ivoire",
         "accented country name in an RSS title not decoded"),
        # Tags are stripped BEFORE decoding, so escaped markup in the visible
        # text survives instead of being decoded into a tag and deleted.
        (_html_fragment_to_text("<b>keep</b> &amp;lt;p&amp;gt;") == "keep &lt;p&gt;",
         "escaped markup in body text was eaten as a tag"),
        # The 600-char cap counts characters a reader sees, and is applied
        # after decoding so it can never slice an entity in half.
        (_html_fragment_to_text("&#8220;" * 50, limit=10) == "“" * 10,
         "limit applied before decoding — can bisect an entity"),
        # Whole-fixture lock: nothing entity-shaped may reach the payload.
        (not [b for b in bulletins if re.search(r"&[#a-zA-Z0-9]{1,10};", b["body"])],
         f"bulletins still carrying entity text: "
         f"{[b['tag'] for b in bulletins if re.search(r'&[#a-zA-Z0-9]{1,10};', b['body'])]}"),
        (not [a for a in rss_advisories if re.search(r"&[#a-zA-Z0-9]{1,10};", a["name"])],
         "RSS-fallback advisory names still carrying entity text"),

        # --- USWDS layout (the branch that runs against the live page) ---
        (len(uswds_rows) == 2, f"USWDS: expected 2 rows, got {len(uswds_rows)}"),
        (uswds_diag.get("lock_engaged") is True,
         "USWDS: usa-table--destination lock never engaged"),
        (uswds_by_name.get("Afghanistan", {}).get("level") == 4,
         f"USWDS Afghanistan.level={uswds_by_name.get('Afghanistan', {}).get('level')!r}"),
        (uswds_by_name.get("Afghanistan", {}).get("risks") == ["U", "C"],
         f"USWDS Afghanistan.risks={uswds_by_name.get('Afghanistan', {}).get('risks')!r}"),
        (uswds_by_name.get("Afghanistan", {}).get("date") == "2026-02-20",
         f"USWDS Afghanistan.date={uswds_by_name.get('Afghanistan', {}).get('date')!r}"),
        (uswds_by_name.get("Albania", {}).get("level") == 2,
         f"USWDS Albania.level={uswds_by_name.get('Albania', {}).get('level')!r}"),
        # The decoy usa-table (no --destination modifier) must not contribute.
        ("Decoy nav table" not in uswds_by_name,
         "USWDS: decoy table leaked into rows — lock is too loose"),
        (legacy_diag.get("lock_engaged") is True,
         "legacy: id=htmlTable lock never engaged"),

        # --- markup diagnostics must be able to name a parse break ---
        # A page with no advisory table at all: lock must NOT engage, and the
        # diag must say so. This is the signal that distinguishes a redesign
        # from an IP block.
        (parse_advisory_table("<html><body><p>nope</p></body></html>",
                              diag=(_d := {})) == [] and _d.get("lock_engaged") is False,
         "diag should report lock_engaged=False on a page with no advisory table"),

        # --- pubDate parsing (37/37 production bulletins shipped date:"") ---
        (_parse_pubdate("Mon, 02 Mar 2026")[0] == "2026-03-02",
         f"time-less pubDate should parse, got {_parse_pubdate('Mon, 02 Mar 2026')!r}"),
        (_parse_pubdate("Mon, 25 May 2026 17:32:05 GMT")[0] == "2026-05-25",
         "full RFC-2822 pubDate should still parse"),
        (_parse_pubdate("")[0] == "" and _parse_pubdate("garbage")[0] == "",
         "empty/garbage pubDate should degrade to ''"),
        # Regression lock: every fixture bulletin must carry a real date, and
        # the newest-first sort must therefore be meaningful.
        (all(b.get("date") for b in bulletins),
         f"bulletins with empty date: "
         f"{[b['tag'] for b in bulletins if not b.get('date')]}"),
        ([b["date"] for b in bulletins] == sorted(
            [b["date"] for b in bulletins], reverse=True),
         f"bulletins not sorted newest-first: {[b['date'] for b in bulletins]}"),

        # --- RSS advisory fallback ---
        (len(rss_advisories) == 11,
         f"RSS fallback: expected 11 advisories, got {len(rss_advisories)}"),
        (rss_adv_by_name.get("Bahrain", {}).get("level") == 3,
         f"RSS fallback Bahrain.level={rss_adv_by_name.get('Bahrain', {}).get('level')!r}"),
        (rss_adv_by_name.get("Bahrain", {}).get("date") == "2026-03-02",
         f"RSS fallback Bahrain.date={rss_adv_by_name.get('Bahrain', {}).get('date')!r}"),
        (rss_adv_by_name.get("Bahrain", {}).get("risks") == [],
         "RSS fallback carries no risk codes — risks must be []"),
        (rss_adv_by_name.get("Bahrain", {}).get("url", "").endswith("/bahrain.html"),
         f"RSS fallback Bahrain.url={rss_adv_by_name.get('Bahrain', {}).get('url')!r}"),
        (rss_adv_by_name.get("United Arab Emirates", {}).get("level") == 3,
         "RSS fallback should handle multi-word country names"),
        (advisories_from_rss("") == [] and advisories_from_rss("<not xml") == [],
         "RSS fallback should degrade to [] on empty/invalid XML"),

        # --- _diagnose must separate (a) block from (b) parser break ---
        ("REFUSING" in _diagnose({"html": {"http_status": 403, "body_snippet": "Access Denied"},
                                  "rss": {"http_status": 403}}),
         "_diagnose should call a 403 a block"),
        ("PARSER BREAK" in _diagnose({"html": {"http_status": 200, "bytes": 180000,
                                               "lock_engaged": False, "tables_seen": 4},
                                      "rss": {"http_status": 200}}),
         "_diagnose should call HTTP-200-with-no-lock a parser break"),
        ("no HTTP response" in _diagnose({"html": {"http_status": None, "error": "DNS fail"},
                                          "rss": {"http_status": None}}),
         "_diagnose should call a transport failure a transport failure"),
    ]
    failed = [msg for ok, msg in assertions if not ok]
    if failed:
        for f in failed:
            print(f"  [self-test FAIL] {f}", file=sys.stderr)
        return 1
    print(f"  [self-test OK] {len(rows)} legacy table rows, "
          f"{len(uswds_rows)} USWDS table rows, "
          f"{len(rss_advisories)} RSS-fallback advisories, "
          f"{len(bulletins)} bulletins (of 11 RSS items) parsed; "
          f"{len(assertions)} assertions passed.")
    return 0


# ----- failure reporting -----------------------------------------------------
#
# Everything below exists because of one line: "[advisories] scrape failed;
# preserving prior". That was the entire signal for a 68-day outage, and it
# was swallowed three times over — fetch_live returned None, app.py caught it
# and printed a note, and pages.yml's `|| echo` discarded the exit code. The
# preserve behaviour itself is correct; the silence was the defect.

# Preserving for a day is a blip. Preserving for this long is an outage.
STALE_ALERT_DAYS = 3


def _annotate(level: str, title: str, message: str) -> None:
    """Emit a GitHub Actions annotation *and* a plain line.

    The build invokes this fetcher inside app.py/v2/app.py, whose step in
    pages.yml ends with `|| echo`, so a non-zero exit alone can never surface.
    A ``::error::`` workflow command is picked up from stdout regardless of
    exit code and shows on the run summary page, so the alarm does not depend
    on anyone editing the workflow. Outside CI it is just a printed line.
    """
    one = " ".join(message.split())
    print(f"::{level} title={title}::{one}")
    print(f"  [advisories] {level.upper()}: {one}", file=sys.stderr)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"- **{level.upper()}** {title}: {one}\n")
        except OSError:
            # Best-effort cosmetics. The ::error/::warning workflow command and
            # the stderr line above already carry the alarm, so a summary file
            # that is absent, read-only or full must not raise out of the
            # ALARM path itself — that would swallow the very signal this
            # function exists to emit.
            pass


def _diagnose(status: dict) -> str:
    """Turn the endpoint record into a plain-English cause, in one sentence.

    This is the discriminator the old code threw away. An IP block and a
    parser break both ended at "scrape failed", so nobody could tell them
    apart without re-running the scraper from a machine State Dept would talk
    to. The distinction is now decided by data the run already has.
    """
    html = status.get("html") or {}
    rss = status.get("rss") or {}
    hs, rs = html.get("http_status"), rss.get("http_status")

    def _blocked(ep: dict, name: str) -> str | None:
        st = ep.get("http_status")
        if st is None:
            # Truncate here only — the full exception text is kept verbatim in
            # the status JSON written by _write_status().
            err = " ".join((ep.get("error") or "unknown transport error").split())
            if len(err) > 160:
                err = err[:160] + "…"
            return (f"{name} returned no HTTP response at all ({err}) — "
                    f"network/DNS/proxy, not markup.")
        if st in (401, 403, 407, 429):
            return (f"{name} returned HTTP {st}"
                    + (f' ("{ep.get("body_snippet", "")[:120]}")'
                       if ep.get("body_snippet") else "")
                    + " — the host is REFUSING us (block or throttle), so this is "
                      "not a parser problem. A browser User-Agent is already sent.")
        if st != 200:
            return f"{name} returned HTTP {st} — upstream error, not markup."
        return None

    html_bad = _blocked(html, "The HTML advisory page")
    if html_bad is None and hs == 200:
        # HTTP 200 with zero rows is unambiguous: we got a page and failed to
        # read it. This is the branch the repo previously mislabelled as an
        # IP block.
        if not html.get("lock_engaged"):
            return (f"PARSER BREAK, not a block: the HTML page served HTTP 200 "
                    f"({html.get('bytes', 0)} bytes, {html.get('tables_seen', 0)} "
                    f"<table>s) but the advisory-table lock never engaged — no "
                    f"table matched id='htmlTable' or class 'usa-table--destination'. "
                    f"Page mentions 'usa-table': "
                    f"{html.get('html_mentions_usa_table')}. Tables found: "
                    f"{html.get('table_signatures')}. Fix the table selector.")
        return (f"PARSER BREAK, not a block: the HTML page served HTTP 200 and the "
                f"table lock DID engage over "
                f"{html.get('rows_inside_locked_table', 0)} <tr>s, but 0 rows "
                f"survived cell parsing — the row/cell shape changed, not the "
                f"table selector.")
    parts = [p for p in (html_bad, _blocked(rss, "The RSS feed")) if p]
    if hs is not None and rs == 200:
        parts.append("Note the RSS feed answered fine, so egress works and this "
                     "is specific to the HTML endpoint.")
    return " ".join(parts) or "Cause could not be determined from the endpoint record."


def _prior_stamp(out_path: Path) -> tuple[str, int | None]:
    """(generated_at, age_in_days) of the file we are about to preserve."""
    try:
        prior = json.loads(out_path.read_text())
        stamp = str(prior.get("generated_at") or "")
    except Exception:
        return "", None
    if not stamp:
        return "", None
    try:
        dt = datetime.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        return stamp, max(0, (datetime.now(timezone.utc) - dt).days)
    except Exception:
        return stamp, None


def _write_status(out_path: Path, status: dict) -> None:
    """Persist the machine-readable fetch record next to the output — ALWAYS,
    success or failure.

    On the next build this file answers "(a) blocked or (b) parser break?"
    at a glance: `http_status: 403` vs `http_status: 200, bytes: 180000,
    lock_engaged: false, rows_parsed: 0`. That information already existed as
    two stdout lines; it just never outlived the build log, which is precisely
    why this sat unnoticed for 68 days.
    """
    p = out_path.with_name(out_path.stem + "-fetch-status.json")
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(status, indent=2, sort_keys=True))
        print(f"  [advisories] fetch record -> {p}")
    except Exception as e:
        print(f"  [advisories] could not write {p}: {e}", file=sys.stderr)


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
    status: dict = {}
    payload = fetch_live(status)

    prior_generated, prior_age = _prior_stamp(out_path)
    status["prior_generated_at"] = prior_generated
    status["prior_age_days"] = prior_age

    if payload is None:
        # Fallback-to-last-good: preserve the existing file (if any) and make
        # the preserve LOUD. A silent preserve is what let this feed serve
        # 2026-05-26 data while looking current for 68 days.
        verdict = _diagnose(status)
        stale_bit = ""
        if prior_generated:
            stale_bit = (f" Serving prior {out_path.name} from {prior_generated}"
                         + (f" ({prior_age}d stale)" if prior_age is not None else "")
                         + ".")
        elif out_path.exists():
            stale_bit = f" Serving prior {out_path.name} (no generated_at stamp)."
        else:
            stale_bit = f" No prior {out_path.name} to fall back on — tab will be empty."

        detail = (f"PRESERVED {out_path.name}: 0 advisories parsed. {verdict}"
                  f"{stale_bit}")
        # >= this many days of preserving is not a blip, it is an outage.
        hard = prior_age is None or prior_age >= STALE_ALERT_DAYS
        _annotate("error" if hard else "warning",
                  "Travel advisories not refreshed", detail)
        _write_status(out_path, status)
        return 1

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2))
    _write_status(out_path, status)
    note = ""
    if payload.get("source") == "rss-fallback":
        note = " [via RSS fallback — HTML table broke, risk pills unavailable]"
        _annotate("warning", "Travel advisories degraded",
                  f"HTML advisory table yielded 0 rows; rebuilt "
                  f"{len(payload['advisories'])} advisories from RSS instead. "
                  f"{_diagnose(status)} Risk-indicator pills are empty until the "
                  f"HTML parser is repaired.")
    print(f"  Wrote {out_path} ({len(payload['advisories'])} advisories, "
          f"{len(payload['bulletins'])} bulletins){note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
