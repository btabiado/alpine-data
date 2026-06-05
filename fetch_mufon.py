"""
UAP / NUFORC sightings fetcher.

The MUFON / UAP tab on the V2 dashboard has three sections:
  1. Latest Updates  — curated static cards, baked into HTML_TEMPLATE.
  2. Document Library — curated static external links, baked into HTML_TEMPLATE.
  3. US Sightings Map — driven by THIS fetcher's output.

This module only powers section 3 (the state heat-map). Sections 1 and 2 do
not change often enough to warrant a fetcher; the operator updates them by
editing v2/app.py directly.

Data sources — historical mirror PLUS live NUFORC scrape
--------------------------------------------------------
**Historical (1906–2014):** the ``planetsig/ufo-reports`` GitHub mirror is
well-known, MIT-licensed, headerless, and ships an ~80k-row CSV with columns::

    datetime, city, state, country, shape,
    duration_seconds, duration_text, comments,
    posted, latitude, longitude

That mirror was last refreshed in 2014, so it does not cover 2014-05 onward.

**Live (2014-05–today):** we scrape NUFORC's own monthly subndx pages
(``https://nuforc.org/subndx/?id=eYYYYMM``). Each page renders a wpDataTables
table whose backing AJAX endpoint accepts a ``YearMonth=YYYYMM`` placeholder
and returns JSON with columns ``[link, occurred, city, state, country, shape,
summary, reported, media, explanation]``. The scrape is polite:

  * one User-Agent string identifying the dashboard + link to the public site,
  * single-threaded,
  * 2-second minimum delay between month requests,
  * monthly responses cached on disk (``data/.stale/nuforc_subndx_YYYYMM.json``)
    so subsequent runs only re-fetch the current and prior month (which can
    still get new entries),
  * a 5-minute hard wall-clock cap — if the scrape doesn't finish in time we
    ship whatever we got and merge with the historical mirror,
  * stops early if 3 consecutive months 404 (i.e. we've scrolled past the
    earliest archive).

Where the two sources overlap (2014-05–2014-09 in the wild), the NUFORC live
scrape WINS (more accurate; it IS the upstream).

Output schema (sidecar v2/data-mufon.json) ::

    {
      "generated_at": "2026-05-25T19:30:00Z",
      "source": "planetsig/ufo-reports (1906-2014) + nuforc.org direct (2014+)",
      "source_url": "https://nuforc.org/subndx/",
      "total_records": 160000,
      "date_range": ["1906-11-11", "2026-05-23"],
      "by_state_year": {
        "CA": {"2014": 432, "2013": 511, ...},
        "TX": {...},
        ...
      },
      "top_cities_by_state": {
        "CA": [{"city": "Los Angeles", "count": 1234}, ...],
        ...
      },
      "recent_buckets": {
        "CA": {"30d": 7, "60d": 12, "90d": 18, "365d": 215},
        ...
      },
      "totals_by_year": {"2026": 1066, "2025": 4500, ...},
      "shape_totals": [
        {"shape": "light", "count": 23456},
        {"shape": "triangle", "count": 8201},
        ...sorted desc; top ~15 + "other" + "unknown"...
      ],
      "shape_by_year": {
        "1906": {"unknown": 2, "light": 0, ...},
        ...
        "2026": {"light": 412, "triangle": 187, ...}
      },
      "totals_by_month": {
        "1906-11": 1, "1906-12": 0, ...,
        "2026-05": 1066
      },
      "shape_by_month": {
        "2023-06": {"light": 41, "triangle": 18, ...},
        ...
        "2026-05": {"light": 412, "triangle": 187, ...}
      }
    }

The ``totals_by_month`` series spans the entire date range (one entry per
calendar month from min to max, gaps filled with 0). ``shape_by_month`` is
capped at the trailing 36 months only — pre-2024 monthly shape detail is
rarely useful and the payload bloat isn't justified. Both feed the V2 UAP
trend / shapes charts' sub-yearly toggles (30d / 90d / YTD).

The ``recent_buckets`` field is anchored to **today** (UTC) once the live
scrape contributes data — so "30d" really means the last 30 days. If the
live scrape produces zero rows AND we fall back to pure planetsig, we set
``_stale: True`` and the buckets reset to be anchored to the dataset's last
entry (2014-05-08) so the dashboard renders an honest "no recent data".

Shape aggregations
------------------
Two top-level keys carry NUFORC shape classifications (Light, Triangle,
Disk, Sphere, Fireball, …). Shape strings are lowercased and stripped;
blanks map to "unknown". To keep the JSON compact, only the top ~15
shapes are kept as named entries — everything else collapses into "other".

CLI ::

    python fetch_mufon.py                       # default --out v2/data-mufon.json
    python fetch_mufon.py --out PATH
    python fetch_mufon.py --no-network          # offline parser self-test
    python fetch_mufon.py --months-back N       # how many months of NUFORC to pull (default 144)
    python fetch_mufon.py --no-live             # skip live scrape, planetsig only
"""

from __future__ import annotations

import argparse
import csv
import html
import io
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


UA = ("Mozilla/5.0 (compatible; alpine-data/1.0 "
      "+https://btabiado.github.io/alpine-data/)")
ROOT = Path(__file__).parent
DEFAULT_OUT = ROOT / "v2" / "data-mufon.json"

# NUFORC scrape configuration. The wdtNonce lives in a hidden input on any
# /subndx/?id=e<YYYYMM> page (the /ndx/?id=event index renders the
# month-list table BUT not the same wpDataTables instance, so no nonce is
# emitted there). We bootstrap by loading the current-month subndx page.
NUFORC_BASE = "https://nuforc.org"
NUFORC_MONTH_PAGE = f"{NUFORC_BASE}/subndx/?id=e"  # + YYYYMM
NUFORC_AJAX_URL = f"{NUFORC_BASE}/wp-admin/admin-ajax.php"
NUFORC_REQUEST_DELAY_SEC = 2.0      # polite floor between requests
NUFORC_WALL_CLOCK_CAP_SEC = 900     # 15 min total (cold-start no-cache needs ~9min; raised from 300s after partial-pull in CI on 2026-05-25)
NUFORC_404_STREAK_CAP = 3           # stop after this many consecutive misses
NUFORC_CACHE_DIR = ROOT / "data" / ".stale"

# Probed in order. First one that yields >1000 sane rows wins. The historical
# planetsig mirror is the load-bearing fallback; the Renner candidates are
# kept first so a future re-publish would automatically win.
CSV_CANDIDATES: list[dict[str, str]] = [
    {
        # Canonical (DVC-backed, usually 404 — keep first so a future enable wins).
        "url": "https://raw.githubusercontent.com/timothyrenner/nuforc_sightings_data/main/data/processed/nuforc_reports.csv",
        "label": "timothyrenner/nuforc_sightings_data (Renner, MIT)",
    },
    {
        # The maintainer historically used `master`. Try that too.
        "url": "https://raw.githubusercontent.com/timothyrenner/nuforc_sightings_data/master/data/processed/nuforc_reports.csv",
        "label": "timothyrenner/nuforc_sightings_data (Renner master branch, MIT)",
    },
    {
        # planetsig mirror — headerless, ~80k rows through ~2014. Reliable.
        "url": "https://raw.githubusercontent.com/planetsig/ufo-reports/master/csv-data/ufo-scrubbed-geocoded-time-standardized.csv",
        "label": "planetsig/ufo-reports mirror of NUFORC (MIT)",
    },
]


# US state abbreviations (50 + DC) for filtering. Anything outside this set
# (CA territories, lowercase mistakes, blank, two-letter UK provinces) is
# dropped from the state aggregation but still counted in totals.
US_STATES: set[str] = {
    "AL","AK","AZ","AR","CA","CO","CT","DE","DC","FL","GA","HI","ID","IL",
    "IN","IA","KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE",
    "NV","NH","NJ","NM","NY","NC","ND","OH","OK","OR","PA","RI","SC","SD",
    "TN","TX","UT","VT","VA","WA","WV","WI","WY",
}


# ----- helpers ---------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _http_get_text(url: str, timeout: int = 60) -> str | None:
    """GET text via stdlib urllib (no requests dependency). Returns None on
    any failure (404, timeout, decode error). Stays quiet — callers log."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            raw = resp.read()
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
        return None
    # NUFORC text is usually latin-1 (some legacy escapes); fall back to
    # replace errors so a bad byte never aborts the parse.
    for enc in ("utf-8", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _parse_datetime(s: str) -> datetime | None:
    """Accept the formats we see in NUFORC mirrors AND the live subndx feed:
      - "M/D/YYYY HH:MM"      (planetsig + live)
      - "MM/DD/YYYY HH:MM"    (live, zero-padded)
      - "MM/DD/YYYY"          (live, no time)
      - "YYYY-MM-DDTHH:MM:SS" (Renner processed)
    Returns a naive UTC-equivalent datetime, or None.
    """
    if not s:
        return None
    s = s.strip()
    if not s:
        return None
    # ISO first
    try:
        # tolerate trailing 'Z'
        return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        pass
    # M/D/YYYY HH:MM (or M/D/YYYY). Same parser handles 1- and 2-digit M/D.
    for fmt in ("%m/%d/%Y %H:%M", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _norm_state(raw: str) -> str | None:
    """Uppercase 2-letter US state abbreviation, or None if not a US state."""
    if not raw:
        return None
    code = raw.strip().upper()
    if len(code) != 2:
        return None
    if code not in US_STATES:
        return None
    return code


def _norm_city(raw: str) -> str:
    """Title-case a NUFORC city string, stripping common parenthetical
    annotations like '(continued)' or '(uk/england)'. Returns the original
    if nothing useful remains."""
    if not raw:
        return ""
    c = raw.strip()
    # Drop any '(...)' suffix.
    paren = c.find("(")
    if paren > 0:
        c = c[:paren].strip()
    # Title-case but preserve all-caps acronyms (AFB, NYC).
    out_parts: list[str] = []
    for part in c.split():
        if part.isupper() and len(part) <= 4:
            out_parts.append(part)
        else:
            out_parts.append(part.capitalize())
    return " ".join(out_parts).strip()


# ----- core aggregation -----------------------------------------------------


def _norm_shape(raw: str) -> str:
    """Lowercase + strip a NUFORC shape string. Blank/missing becomes
    "unknown" so it has a named bucket in the aggregation (rather than
    silently dropping the row from the shape view)."""
    if not raw:
        return "unknown"
    s = raw.strip().lower()
    if not s:
        return "unknown"
    # NUFORC uses a handful of tokens that look like noise. Map them to
    # "unknown" so the front-end legend stays meaningful.
    if s in {"n/a", "na", "none", "-", "?"}:
        return "unknown"
    return s


def _row_iter(text: str) -> "tuple[list[dict], dict]":
    """Parse a NUFORC CSV (headered or not) into a list of dicts with keys
    {datetime: datetime|None, city: str, state: str|None, shape: str}.
    Also returns a meta dict with raw_row_count and skipped_count for
    diagnostics.
    """
    rows: list[dict] = []
    skipped = 0
    raw_count = 0
    reader = csv.reader(io.StringIO(text))
    first = next(reader, None)
    if first is None:
        return [], {"raw_row_count": 0, "skipped": 0, "had_header": False}

    # Header detection: if any non-numeric cell in the first row matches a
    # common NUFORC column name, treat row 0 as a header.
    first_lower = [c.strip().lower() for c in first]
    header_terms = {"city", "state", "occurred", "datetime", "summary",
                    "shape", "duration", "country"}
    had_header = any(c in header_terms for c in first_lower)

    if had_header:
        col = {name: i for i, name in enumerate(first_lower)}
        dt_idx = col.get("occurred") or col.get("datetime") or col.get("date_time") or 0
        city_idx = col.get("city", 1)
        state_idx = col.get("state", 2)
        shape_idx = col.get("shape", -1)
    else:
        # planetsig layout: datetime, city, state, country, shape, ...
        dt_idx, city_idx, state_idx, shape_idx = 0, 1, 2, 4
        # Treat the "first" row we already pulled as data.
        raw_count += 1
        out = _extract_row(first, dt_idx, city_idx, state_idx, shape_idx)
        if out is None:
            skipped += 1
        else:
            rows.append(out)

    for r in reader:
        raw_count += 1
        out = _extract_row(r, dt_idx, city_idx, state_idx, shape_idx)
        if out is None:
            skipped += 1
            continue
        rows.append(out)

    return rows, {"raw_row_count": raw_count, "skipped": skipped,
                  "had_header": had_header}


def _extract_row(r: list[str], dt_idx: int, city_idx: int,
                 state_idx: int, shape_idx: int = -1) -> dict | None:
    """Pull (datetime, city, state, shape) from a CSV row using the supplied
    column indexes. ``shape_idx < 0`` (or out-of-range) yields "unknown".
    Returns a dict or None if the row is unusable."""
    needed = max(dt_idx, city_idx, state_idx)
    if len(r) <= needed:
        return None
    dt = _parse_datetime(r[dt_idx])
    if dt is None:
        return None
    state = _norm_state(r[state_idx])
    # Rows without a US state are kept (they count toward total_records and
    # date_range) but bypass the per-state aggregation.
    city = _norm_city(r[city_idx])
    if shape_idx >= 0 and shape_idx < len(r):
        shape = _norm_shape(r[shape_idx])
    else:
        shape = "unknown"
    return {"dt": dt, "city": city, "state": state, "shape": shape}


def aggregate(rows: list[dict], anchor_dt: datetime | None = None) -> dict:
    """Roll rows into the dashboard sidecar payload. ``rows`` must be the
    list returned by ``_row_iter``.

    ``anchor_dt`` controls the reference date for the ``recent_buckets``
    windows. When None (the legacy planetsig-only path) we anchor to the
    dataset's own most-recent entry — honest for a frozen mirror but
    misleading for a live feed. The orchestrator passes ``datetime.utcnow()``
    once the live scrape has contributed any data so the buckets describe
    "the last 30/60/90/365 days from today."
    """
    by_state_year: dict[str, dict[str, int]] = {}
    city_counts: dict[str, dict[str, int]] = {}
    totals_by_year: dict[str, int] = {}
    # Monthly granularity series — drives the sub-yearly (30d / 90d / YTD)
    # toggles on the V2 trend + shapes charts. ``totals_by_month`` is the
    # full historical span (one entry per month from min..max, zeros filled
    # post-loop). ``shape_by_month_raw`` is the parallel shape breakdown;
    # we cap that to the last 36 months downstream to keep payload small.
    totals_by_month_raw: dict[str, int] = {}
    shape_by_month_raw: dict[str, dict[str, int]] = {}
    # Shape aggregations — used by the "sightings by classification" chart
    # at the bottom of the UAP tab. Per-row shape comes from _norm_shape
    # (blanks already mapped to "unknown").
    shape_totals_raw: dict[str, int] = {}
    shape_by_year_raw: dict[str, dict[str, int]] = {}
    min_dt: datetime | None = None
    max_dt: datetime | None = None

    for r in rows:
        dt: datetime = r["dt"]
        state: str | None = r["state"]
        city: str = r["city"]
        shape: str = r.get("shape", "unknown")
        year = str(dt.year)
        ym = f"{dt.year:04d}-{dt.month:02d}"
        totals_by_year[year] = totals_by_year.get(year, 0) + 1
        totals_by_month_raw[ym] = totals_by_month_raw.get(ym, 0) + 1
        shape_totals_raw[shape] = shape_totals_raw.get(shape, 0) + 1
        ybucket = shape_by_year_raw.setdefault(year, {})
        ybucket[shape] = ybucket.get(shape, 0) + 1
        mbucket = shape_by_month_raw.setdefault(ym, {})
        mbucket[shape] = mbucket.get(shape, 0) + 1
        if min_dt is None or dt < min_dt:
            min_dt = dt
        if max_dt is None or dt > max_dt:
            max_dt = dt
        if state is None:
            continue
        ys = by_state_year.setdefault(state, {})
        ys[year] = ys.get(year, 0) + 1
        if city:
            cs = city_counts.setdefault(state, {})
            cs[city] = cs.get(city, 0) + 1

    # Top-10 cities per state.
    top_cities_by_state: dict[str, list[dict]] = {}
    for state, counts in city_counts.items():
        ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:10]
        top_cities_by_state[state] = [
            {"city": c, "count": n} for c, n in ranked
        ]

    # Recent buckets — pick an anchor. ``anchor_dt`` wins when supplied
    # (live-scrape merged path); otherwise we use the dataset's own most-
    # recent entry so a frozen mirror produces honest "0 in last 30d (since
    # 2014)" answers instead of misleadingly looking like "0 last 30 days."
    recent_buckets: dict[str, dict[str, int]] = {}
    bucket_anchor: datetime | None = anchor_dt if anchor_dt is not None else max_dt
    if bucket_anchor is not None:
        windows = {"30d": 30, "60d": 60, "90d": 90, "365d": 365}
        thresholds = {k: bucket_anchor - timedelta(days=d) for k, d in windows.items()}
        for state in by_state_year:
            recent_buckets[state] = {k: 0 for k in windows}
        for r in rows:
            state = r["state"]
            if not state:
                continue
            dt = r["dt"]
            slot = recent_buckets.setdefault(state, {k: 0 for k in windows})
            for k, thr in thresholds.items():
                if dt >= thr:
                    slot[k] += 1

    # Compact the shape view to keep the sidecar small. Keep the top ~15
    # named shapes; everything outside that set rolls into "other". Both
    # "unknown" (blanks) and "other" (NUFORC's literal classification) are
    # ALWAYS preserved as named buckets — and the collapsed tail merges
    # INTO the existing "other" bucket rather than duplicating it. That
    # preserves the invariant: per-year shape sums == totals_by_year[year].
    SHAPE_TOP_N = 15
    ranked = sorted(shape_totals_raw.items(), key=lambda kv: kv[1], reverse=True)
    kept = {name for name, _ in ranked[:SHAPE_TOP_N]}
    kept.add("unknown")
    kept.add("other")

    # Roll the collapsed tail into "other" by mutating the raw totals — that
    # way the final list/dict construction below has a single source of truth.
    tail_total = sum(c for n, c in shape_totals_raw.items() if n not in kept)
    if tail_total:
        shape_totals_raw["other"] = shape_totals_raw.get("other", 0) + tail_total

    shape_totals_list = sorted(
        ({"shape": n, "count": c} for n, c in shape_totals_raw.items()
         if n in kept and c > 0),
        key=lambda d: d["count"],
        reverse=True,
    )

    # Year buckets — same collapse rule (tail merges into "other"). Drop
    # zero-valued shape entries so the JSON stays compact (renderer treats
    # missing as 0 anyway).
    shape_by_year: dict[str, dict[str, int]] = {}
    for year, buckets in shape_by_year_raw.items():
        out: dict[str, int] = {}
        tail = 0
        for name, count in buckets.items():
            if name in kept:
                out[name] = out.get(name, 0) + count
            else:
                tail += count
        if tail:
            out["other"] = out.get("other", 0) + tail
        if out:
            shape_by_year[year] = out

    # Build `totals_by_month` as a dense series (zeros filled) from the
    # earliest seen month to the latest. The dashboard's sub-yearly toggles
    # rely on consecutive months for windowing math (last-N-months slicing
    # would skip gaps otherwise).
    totals_by_month: dict[str, int] = {}
    if min_dt is not None and max_dt is not None:
        cy, cm = min_dt.year, min_dt.month
        end_y, end_m = max_dt.year, max_dt.month
        while (cy, cm) <= (end_y, end_m):
            key = f"{cy:04d}-{cm:02d}"
            totals_by_month[key] = totals_by_month_raw.get(key, 0)
            cm += 1
            if cm > 12:
                cm = 1
                cy += 1

    # `shape_by_month` — same compaction rule as `shape_by_year` (top-N kept,
    # tail rolled into "other"), and capped at the trailing 36 months so the
    # JSON payload doesn't balloon. Older months are still represented in
    # `totals_by_year` / `shape_by_year` for the long-range views.
    SHAPE_BY_MONTH_CAP = 36
    shape_by_month: dict[str, dict[str, int]] = {}
    recent_month_keys = sorted(shape_by_month_raw.keys())[-SHAPE_BY_MONTH_CAP:]
    for ym in recent_month_keys:
        buckets = shape_by_month_raw[ym]
        out: dict[str, int] = {}
        tail = 0
        for name, count in buckets.items():
            if name in kept:
                out[name] = out.get(name, 0) + count
            else:
                tail += count
        if tail:
            out["other"] = out.get("other", 0) + tail
        if out:
            shape_by_month[ym] = out

    payload: dict[str, Any] = {
        "total_records": len(rows),
        "date_range": [
            min_dt.date().isoformat() if min_dt else None,
            max_dt.date().isoformat() if max_dt else None,
        ],
        "by_state_year": by_state_year,
        "top_cities_by_state": top_cities_by_state,
        "recent_buckets": recent_buckets,
        "totals_by_year": totals_by_year,
        "shape_totals": shape_totals_list,
        "shape_by_year": shape_by_year,
        "totals_by_month": totals_by_month,
        "shape_by_month": shape_by_month,
    }
    return payload


# ----- live NUFORC scrape ---------------------------------------------------

# Regex that pulls every <tr>...</tr> from the AJAX JSON's `data` array. The
# JSON itself is structured, so we DO use json.loads — these helpers operate
# on the post-parsed cell strings.
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(s: str) -> str:
    """NUFORC cell values sometimes contain anchor tags ('<a ...>City</a>')
    or `<br>`. Strip every tag, decode HTML entities, collapse whitespace."""
    if not s:
        return ""
    cleaned = _HTML_TAG_RE.sub(" ", s)
    cleaned = html.unescape(cleaned)
    return " ".join(cleaned.split()).strip()


def _nuforc_extract_nonce(html_text: str) -> str | None:
    """Pull the wpDataTables CSRF nonce from a /ndx/ or /subndx/ page.

    The primary field is named ``wdtNonceFrontendServerSide_1`` and ships
    as a hidden input. We try a series of progressively more tolerant
    patterns so that minor markup churn (attribute reordering, whitespace,
    table_id increments after a plugin update, etc.) doesn't break the
    bootstrap. Returns the hex token or None on miss.
    """
    # Try patterns in order of specificity. Each is a (label, regex) pair —
    # the label is purely for debug logging by the caller if it wants it.
    patterns = (
        # Original strict pattern: name="..."<sp>value="<hex>"
        r'wdtNonceFrontendServerSide_1"\s+value="([a-f0-9]+)"',
        # Attribute order flipped: value="<hex>" ... name="wdtNonceFrontendServerSide_1"
        r'value="([a-f0-9]+)"[^>]*name="wdtNonceFrontendServerSide_1"',
        # Any wdtNonceFrontendServerSide_N (plugin sometimes bumps the suffix
        # when more than one wpDataTables instance lands on a page).
        r'name="wdtNonceFrontendServerSide_\d+"\s+value="([a-f0-9]+)"',
        r'value="([a-f0-9]+)"[^>]*name="wdtNonceFrontendServerSide_\d+"',
        # Most tolerant: any input whose name contains "wdtNonce" anywhere
        # near a value="<hex>" attribute on the same tag.
        r'<input[^>]*name="[^"]*wdtNonce[^"]*"[^>]*value="([a-f0-9]+)"',
        r'<input[^>]*value="([a-f0-9]+)"[^>]*name="[^"]*wdtNonce[^"]*"',
    )
    for pat in patterns:
        m = re.search(pat, html_text)
        if m:
            tok = m.group(1)
            # Sanity-check: real WP nonces are 10-char hex. Accept anything
            # 8-20 chars to leave a small margin for future format changes.
            if 8 <= len(tok) <= 20:
                return tok
    return None


def _nuforc_fetch_month(yyyymm: str, nonce: str,
                        timeout: int = 60) -> dict | None:
    """POST the AJAX request that returns one month's sightings. Returns the
    raw JSON dict (with keys recordsTotal, recordsFiltered, data:[...rows...])
    or None if the HTTP layer fails. A successful 200 with ``data == []`` IS
    returned as the dict — the caller decides whether that means 404 or
    "month exists but empty".
    """
    body = urllib.parse.urlencode({
        "draw": "1",
        "start": "0",
        # NUFORC's busiest month ever was ~9k reports (late 2012). Cap well
        # over that so we never have to paginate. Server caps internally.
        "length": "20000",
        "wdtNonce": nonce,
    }).encode()
    qs = urllib.parse.urlencode({
        "action": "get_wdtable",
        "table_id": "1",
        "wdt_var1": "YearMonth",
        "wdt_var2": yyyymm,
    })
    url = f"{NUFORC_AJAX_URL}?{qs}"
    headers = {
        "User-Agent": UA,
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": f"{NUFORC_MONTH_PAGE}{yyyymm}",
        "Accept": "application/json, text/javascript, */*; q=0.01",
    }
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
        return None
    try:
        return json.loads(raw.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, ValueError):
        return None


def _nuforc_parse_data_rows(data: list) -> list[dict]:
    """Turn the AJAX `data` array (list of 10-cell row lists) into our
    normalized dict shape: {dt, city, state, shape}. Skips bad rows quietly.

    Column layout (NUFORC subndx, verified May 2026):
      0=link, 1=occurred ("MM/DD/YYYY HH:MM"), 2=city, 3=state, 4=country,
      5=shape, 6=summary, 7=reported, 8=media, 9=explanation
    """
    rows: list[dict] = []
    for r in data:
        if not isinstance(r, list) or len(r) < 6:
            continue
        dt = _parse_datetime(_strip_html(str(r[1] or "")))
        if dt is None:
            continue
        state = _norm_state(_strip_html(str(r[3] or "")))
        city = _norm_city(_strip_html(str(r[2] or "")))
        shape = _norm_shape(_strip_html(str(r[5] or "")))
        rows.append({"dt": dt, "city": city, "state": state, "shape": shape})
    return rows


def _months_back_iter(n: int) -> list[str]:
    """Return the last ``n`` YYYYMM strings, newest first. Anchored to UTC."""
    today = datetime.now(timezone.utc).date().replace(day=1)
    out: list[str] = []
    for i in range(n):
        # Subtract i months by going to day=1 then back-stepping.
        y = today.year
        m = today.month - i
        while m <= 0:
            m += 12
            y -= 1
        out.append(f"{y:04d}{m:02d}")
    return out


def _fetch_nuforc_live(months_back: int = 144,
                       wall_clock_cap_sec: float = NUFORC_WALL_CLOCK_CAP_SEC,
                       cache_dir: Path = NUFORC_CACHE_DIR) -> dict:
    """Scrape the last ``months_back`` months of NUFORC's subndx index.

    Returns a dict ``{"rows": [...], "meta": {...}}``. ``rows`` is the
    normalized per-row list (same shape as ``_row_iter`` output). ``meta``
    carries diagnostics: ``months_pulled``, ``months_404``, ``months_cached``,
    ``wall_clock_sec``, ``stopped_reason``.

    Cache policy: every successfully-fetched month is written to
    ``cache_dir / nuforc_subndx_YYYYMM.json`` and re-loaded on subsequent
    runs WITHOUT a network call — *except* the current and prior month,
    which are always re-fetched (they accumulate new entries).
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    today_ym = datetime.now(timezone.utc).strftime("%Y%m")
    # Compute the "prior" month string by going to day=1 then minus one.
    today_d = datetime.now(timezone.utc).date().replace(day=1)
    prior_d = (today_d - timedelta(days=1)).replace(day=1)
    prior_ym = prior_d.strftime("%Y%m")
    always_refresh = {today_ym, prior_ym}

    start = time.monotonic()
    rows: list[dict] = []
    months_pulled = 0
    months_404 = 0
    months_cached = 0
    consecutive_misses = 0
    stopped_reason: str | None = None

    nonce: str | None = None  # lazy-fetch on first network request

    for ym in _months_back_iter(months_back):
        elapsed = time.monotonic() - start
        if elapsed > wall_clock_cap_sec:
            stopped_reason = f"wall_clock_cap ({wall_clock_cap_sec}s)"
            break

        cache_path = cache_dir / f"nuforc_subndx_{ym}.json"

        # Cache hit (and not in the always-refresh window)?
        if cache_path.exists() and ym not in always_refresh:
            try:
                cached = json.loads(cache_path.read_text())
                month_rows = _nuforc_parse_data_rows(cached.get("data", []))
                rows.extend(month_rows)
                months_cached += 1
                # A cached month resets the consecutive-miss counter because
                # we have evidence the era is populated.
                consecutive_misses = 0
                continue
            except (OSError, json.JSONDecodeError, ValueError):
                # Corrupt cache — fall through to refetch.
                pass

        # First network call — bootstrap the wdtNonce. The nonce only
        # appears on /subndx/ pages where the wpDataTables instance is
        # rendered (NOT on /ndx/?id=event), so we use the very same page
        # we're about to scrape: the current month.
        if nonce is None:
            try:
                bootstrap_html = _http_get_text(f"{NUFORC_MONTH_PAGE}{ym}")
            except Exception:
                bootstrap_html = None
            if not bootstrap_html:
                stopped_reason = "could_not_load_bootstrap_page"
                break
            nonce = _nuforc_extract_nonce(bootstrap_html)
            if not nonce:
                # Diagnostic dump — surface enough context in CI logs that a
                # future regression is debuggable without re-fetching the page.
                has_wdt = "wdtNonce" in bootstrap_html
                has_input = '<input' in bootstrap_html
                size = len(bootstrap_html)
                stopped_reason = (
                    f"could_not_extract_nonce "
                    f"(bootstrap={size}B, has_wdtNonce_token={has_wdt}, "
                    f"has_input_tag={has_input}, url={NUFORC_MONTH_PAGE}{ym})"
                )
                print(f"  NUFORC: {stopped_reason}", file=sys.stderr)
                break

        # Polite delay between actual network requests. Skip on the very
        # first network call so we don't pay it for nothing.
        if months_pulled + months_404 > 0:
            time.sleep(NUFORC_REQUEST_DELAY_SEC)

        try:
            payload = _nuforc_fetch_month(ym, nonce)
        except Exception:
            payload = None
        if payload is None:
            months_404 += 1
            consecutive_misses += 1
            print(f"  NUFORC: {ym} request failed (will continue)",
                  file=sys.stderr)
            if consecutive_misses >= NUFORC_404_STREAK_CAP:
                stopped_reason = (f"hit {NUFORC_404_STREAK_CAP} consecutive "
                                  f"failures (likely past earliest archive)")
                break
            continue

        data = payload.get("data", []) or []
        # A "no rows" response IS a valid response (NUFORC has zero reports
        # for that month in some 1700s/1800s archives). Distinguish it from a
        # real failure by treating it as zero-but-cached.
        try:
            cache_path.write_text(json.dumps(payload))
        except OSError:
            pass  # cache write failure isn't fatal

        month_rows = _nuforc_parse_data_rows(data)
        if month_rows:
            rows.extend(month_rows)
            months_pulled += 1
            consecutive_misses = 0
            print(f"  NUFORC: {ym} -> {len(month_rows)} rows",
                  file=sys.stderr)
        else:
            # Empty month — likely beyond the archive's depth. Count toward
            # the 404 streak so the cap kicks in eventually.
            months_pulled += 1  # we DID get a response, just empty
            consecutive_misses += 1
            print(f"  NUFORC: {ym} -> 0 rows (empty month)",
                  file=sys.stderr)
            if consecutive_misses >= NUFORC_404_STREAK_CAP:
                stopped_reason = (f"hit {NUFORC_404_STREAK_CAP} consecutive "
                                  f"empty months (likely past earliest archive)")
                break

    wall_clock_sec = time.monotonic() - start
    if stopped_reason is None:
        stopped_reason = "all_months_processed"
    meta = {
        "months_pulled": months_pulled,
        "months_404": months_404,
        "months_cached": months_cached,
        "wall_clock_sec": round(wall_clock_sec, 1),
        "stopped_reason": stopped_reason,
    }
    return {"rows": rows, "meta": meta}


# ----- top-level orchestration ----------------------------------------------

def _fetch_planetsig_rows() -> tuple[list[dict], str | None, str | None]:
    """Try each historical-mirror candidate in order. Returns
    (rows, label, url) on success, ([], None, None) on total failure.
    """
    for cand in CSV_CANDIDATES:
        url = cand["url"]
        label = cand["label"]
        print(f"  UAP: probing {url}", file=sys.stderr)
        text = _http_get_text(url)
        if not text:
            print(f"    -> unreachable / 404", file=sys.stderr)
            continue
        if len(text) < 10_000:
            # A real NUFORC CSV is at least several MB. Anything tiny is a
            # GitHub 404 HTML page or a redirect notice — skip without parse.
            print(f"    -> too small ({len(text)} bytes); not a CSV",
                  file=sys.stderr)
            continue
        rows, meta = _row_iter(text)
        if len(rows) < 1000:
            print(f"    -> only {len(rows)} usable rows; skipping",
                  file=sys.stderr)
            continue
        print(f"    -> {len(rows)} rows (raw {meta['raw_row_count']}, "
              f"skipped {meta['skipped']}, header={meta['had_header']})",
              file=sys.stderr)
        return rows, label, url
    return [], None, None


def fetch_all(months_back: int = 144,
              live_scrape: bool = True) -> dict | None:
    """Orchestrate the two-source merge: planetsig (1906-2014) + NUFORC live
    (2014+). The live scrape is the load-bearing improvement; planetsig
    remains the historical backbone.

    Returns the aggregated dashboard payload (with ``source`` / ``source_url``
    / ``generated_at`` populated) or None if BOTH sources failed.
    """
    historical_rows, hist_label, hist_url = _fetch_planetsig_rows()

    live_rows: list[dict] = []
    live_meta: dict = {"months_pulled": 0, "months_404": 0,
                       "months_cached": 0, "wall_clock_sec": 0.0,
                       "stopped_reason": "skipped"}
    if live_scrape:
        try:
            live = _fetch_nuforc_live(months_back=months_back)
            live_rows = live["rows"]
            live_meta = live["meta"]
        except Exception as e:
            print(f"  NUFORC: live scrape crashed: {e}", file=sys.stderr)
            live_rows = []

    if not historical_rows and not live_rows:
        return None

    # Build a deduplication key for the overlap region (2014 onward). The
    # planetsig mirror has the same NUFORC entries we'd be scraping live, so
    # if we naively concat we'd double-count May 2014. Live scrape wins:
    # build a set of (yyyymmdd) anchors that the live scrape covers and drop
    # planetsig rows that fall inside that window.
    live_cover_min: datetime | None = None
    if live_rows:
        live_cover_min = min(r["dt"] for r in live_rows)

    merged_rows: list[dict]
    if live_rows and historical_rows and live_cover_min is not None:
        # Drop planetsig rows >= live_cover_min — live wins for any month
        # the live scrape touched.
        merged_rows = [r for r in historical_rows if r["dt"] < live_cover_min]
        merged_rows.extend(live_rows)
    elif live_rows:
        merged_rows = list(live_rows)
    else:
        merged_rows = list(historical_rows)

    # Anchor recent_buckets to "now" when the live scrape contributed data —
    # otherwise we keep the legacy max-dt anchor so a frozen dataset stays
    # honest.
    anchor_dt: datetime | None = None
    if live_rows:
        anchor_dt = datetime.now(timezone.utc).replace(tzinfo=None)

    payload = aggregate(merged_rows, anchor_dt=anchor_dt)

    # Source attribution. Three modes:
    #   1. live + planetsig both worked     -> combined string
    #   2. only planetsig worked            -> legacy string + _stale=True
    #   3. only live worked (no historical) -> nuforc.org direct only
    if historical_rows and live_rows:
        payload["source"] = (
            f"{hist_label} (1906-2014) + nuforc.org direct (2014+)"
        )
        payload["source_url"] = NUFORC_BASE + "/subndx/"
    elif historical_rows:
        payload["source"] = hist_label or "planetsig/ufo-reports mirror of NUFORC (MIT)"
        payload["source_url"] = hist_url
        payload["_stale"] = True  # no live data this run
    else:
        payload["source"] = "nuforc.org direct (subndx scrape)"
        payload["source_url"] = NUFORC_BASE + "/subndx/"

    payload["generated_at"] = _now_iso()
    payload["_nuforc_live_meta"] = live_meta
    return payload


# ----- self-test -------------------------------------------------------------

_SAMPLE_CSV = """10/10/1949 20:30,san marcos,tx,us,cylinder,2700,45 minutes,"",4/27/2004,29.88,-97.94
10/10/1949 21:00,lackland afb,tx,,light,7200,1-2 hrs,"",12/16/2005,29.38,-98.58
1/1/2014 12:00,los angeles,ca,us,circle,30,30 seconds,"",1/1/2014,34.05,-118.24
2/1/2014 13:00,los angeles,ca,us,circle,30,30 seconds,"",2/1/2014,34.05,-118.24
3/1/2014 14:00,san francisco,ca,us,light,60,1 minute,"",3/1/2014,37.77,-122.41
3/1/2014 15:00,san francisco,ca,us,light,60,1 minute,"",3/1/2014,37.77,-122.41
4/1/2014 16:00,seattle,wa,us,light,60,1 minute,"",4/1/2014,47.60,-122.33
5/1/2014 17:00,chester (uk/england),,gb,disk,20,20s,"",5/1/2014,53.2,-2.91
6/1/2014 18:00,denver,co,us,fireball,5,5 seconds,"",6/1/2014,39.74,-104.99
7/1/2014 19:00,austin,tx,us,triangle,120,2 minutes,"",7/1/2014,30.27,-97.74
"""


def _self_test() -> int:
    rows, meta = _row_iter(_SAMPLE_CSV)
    payload = aggregate(rows)

    # Synthetic NUFORC-style AJAX data array — exercise the live-feed parser.
    sample_ajax = [
        ["<a href='/sighting/?id=1'>Open</a>", "05/15/2026 21:30",
         "Brooklyn", "NY", "USA", "Light", "Bright orb seen", "05/15/2026",
         "Y", ""],
        ["<a href='/sighting/?id=2'>Open</a>", "05/16/2026 22:00",
         "Phoenix", "AZ", "USA", "Triangle", "Three lights in V", "05/16/2026",
         None, ""],
        # Bad row (no date)
        ["<a>Open</a>", "", "Nowhere", "ZZ", "??", "circle", "", "", "", ""],
        # Non-US — kept for total, dropped from state-aggregation
        ["<a>Open</a>", "05/17/2026 03:00", "London", "GLA", "UK",
         "Disk", "Hover over Thames", "05/17/2026", "", ""],
    ]
    live_rows = _nuforc_parse_data_rows(sample_ajax)

    checks = [
        (len(rows) == 10, f"expected 10 parsed rows, got {len(rows)}"),
        (payload["total_records"] == 10,
         f"total_records {payload['total_records']}"),
        # 1 row has no US state (chester uk/england) so by_state_year covers 4.
        (set(payload["by_state_year"].keys()) == {"TX", "CA", "WA", "CO"},
         f"by_state_year keys: {set(payload['by_state_year'].keys())}"),
        (payload["by_state_year"]["TX"]["1949"] == 2,
         f"TX 1949: {payload['by_state_year']['TX'].get('1949')}"),
        (payload["by_state_year"]["TX"]["2014"] == 1,
         f"TX 2014: {payload['by_state_year']['TX'].get('2014')}"),
        (payload["by_state_year"]["CA"]["2014"] == 4,
         f"CA 2014: {payload['by_state_year']['CA'].get('2014')}"),
        (payload["date_range"] == ["1949-10-10", "2014-07-01"],
         f"date_range: {payload['date_range']}"),
        (payload["totals_by_year"]["2014"] == 8,
         f"totals_by_year[2014]: {payload['totals_by_year'].get('2014')}"),
        # Top-cities aggregation rolls "San Francisco" twice.
        (any(c["city"] == "San Francisco" and c["count"] == 2
             for c in payload["top_cities_by_state"]["CA"]),
         f"CA top cities: {payload['top_cities_by_state']['CA']}"),
        # "Chester (uk/england)" had no state, so it shouldn't appear in
        # any state's top-cities list.
        (all("Chester" not in c["city"]
             for cs in payload["top_cities_by_state"].values()
             for c in cs),
         "Chester (non-US) leaked into top_cities_by_state"),
        # Shape aggregations — 10 rows: light×4, circle×2, cylinder, disk,
        # fireball, triangle. All 6 shapes fit comfortably inside the top-15
        # cap so nothing rolls into "other"; "unknown" never gets a count
        # because every sample row carries a shape.
        (any(s["shape"] == "light" and s["count"] == 4
             for s in payload["shape_totals"]),
         f"shape_totals light=4 missing: {payload['shape_totals']}"),
        (any(s["shape"] == "circle" and s["count"] == 2
             for s in payload["shape_totals"]),
         f"shape_totals circle=2 missing: {payload['shape_totals']}"),
        (sum(s["count"] for s in payload["shape_totals"]) == 10,
         f"shape_totals sum should equal total_records, "
         f"got {sum(s['count'] for s in payload['shape_totals'])}"),
        (all(s not in {"other"} for s in
             (d["shape"] for d in payload["shape_totals"])),
         "unexpected 'other' bucket in shape_totals"),
        (payload["shape_by_year"].get("2014", {}).get("light") == 3,
         f"shape_by_year 2014 light=3: "
         f"{payload['shape_by_year'].get('2014')}"),
        (payload["shape_by_year"].get("1949", {}).get("light") == 1,
         f"shape_by_year 1949 light=1: "
         f"{payload['shape_by_year'].get('1949')}"),
        # Per-year shape sums must equal totals_by_year for that year (this
        # is the invariant the UI footnote relies on).
        (all(sum(payload["shape_by_year"].get(y, {}).values())
             == payload["totals_by_year"][y]
             for y in payload["totals_by_year"]),
         "shape_by_year per-year sum != totals_by_year"),
        # Monthly series must exist and be a dense span (every month from
        # min..max present, zeros filled). The sample CSV has rows in
        # 1949-10, 2014-01, 2014-02, 2014-03, 2014-04, 2014-05, 2014-06,
        # 2014-07 — so totals_by_month should span 1949-10..2014-07
        # inclusive (which is 778 months).
        ("totals_by_month" in payload,
         "totals_by_month missing from payload"),
        (payload["totals_by_month"].get("1949-10") == 2,
         f"totals_by_month 1949-10: "
         f"{payload['totals_by_month'].get('1949-10')}"),
        (payload["totals_by_month"].get("2014-01") == 1,
         f"totals_by_month 2014-01: "
         f"{payload['totals_by_month'].get('2014-01')}"),
        (payload["totals_by_month"].get("2014-03") == 2,
         f"totals_by_month 2014-03: "
         f"{payload['totals_by_month'].get('2014-03')}"),
        # Dense fill: a month with no rows still gets a 0 entry.
        (payload["totals_by_month"].get("1949-11") == 0,
         f"totals_by_month 1949-11 should be 0 (dense fill), got "
         f"{payload['totals_by_month'].get('1949-11')}"),
        # Per-month sums must equal total_records (every row contributes
        # exactly one month bucket).
        (sum(payload["totals_by_month"].values()) == payload["total_records"],
         f"totals_by_month sum {sum(payload['totals_by_month'].values())} "
         f"!= total_records {payload['total_records']}"),
        # shape_by_month present, capped at 36 months. The sample has at
        # most 8 distinct months, so it should be 8 entries (well under cap).
        ("shape_by_month" in payload,
         "shape_by_month missing from payload"),
        (len(payload["shape_by_month"]) <= 36,
         f"shape_by_month exceeded 36-month cap: "
         f"{len(payload['shape_by_month'])} entries"),
        (payload["shape_by_month"].get("2014-03", {}).get("light") == 2,
         f"shape_by_month 2014-03 light=2: "
         f"{payload['shape_by_month'].get('2014-03')}"),
        (_norm_shape("LIGHT") == "light", "_norm_shape lowercases"),
        (_norm_shape("  Triangle  ") == "triangle", "_norm_shape strips"),
        (_norm_shape("") == "unknown", "_norm_shape blank → unknown"),
        (_norm_shape("?") == "unknown", "_norm_shape ? → unknown"),
        # _norm_city dropped the parenthetical and title-cased.
        (_norm_city("san marcos") == "San Marcos", "_norm_city basic"),
        (_norm_city("lackland AFB") == "Lackland AFB",
         f"_norm_city AFB: {_norm_city('lackland AFB')!r}"),
        (_norm_city("chester (uk/england)") == "Chester",
         f"_norm_city paren: {_norm_city('chester (uk/england)')!r}"),
        (_parse_datetime("10/10/1949 20:30") is not None,
         "_parse_datetime planetsig format"),
        (_parse_datetime("2014-01-15T12:00:00") is not None,
         "_parse_datetime ISO format"),
        (_parse_datetime("05/15/2026 21:30") is not None,
         "_parse_datetime NUFORC live format"),
        (_parse_datetime("bogus") is None, "_parse_datetime rejects garbage"),
        (_norm_state("CA") == "CA", "_norm_state passthrough"),
        (_norm_state("ca") == "CA", "_norm_state lowercases"),
        (_norm_state("XX") is None, "_norm_state rejects unknown"),
        (_norm_state("") is None, "_norm_state rejects empty"),
        # NUFORC live parser tests
        (len(live_rows) == 3,
         f"live parser should keep 3 of 4 rows, got {len(live_rows)}"),
        (live_rows[0]["state"] == "NY" and live_rows[0]["city"] == "Brooklyn",
         f"live row 0: {live_rows[0]}"),
        (live_rows[0]["shape"] == "light",
         f"live row 0 shape: {live_rows[0]['shape']}"),
        (live_rows[2]["state"] is None,
         f"London row should have state=None, got {live_rows[2]}"),
        # HTML stripping
        (_strip_html("<a href='x'>City</a>") == "City",
         f"_strip_html anchor: {_strip_html('<a>City</a>')!r}"),
        (_strip_html("a&amp;b") == "a&b",
         "_strip_html decodes entities"),
        # Months-back iterator: should produce strictly decreasing YYYYMM
        (_months_back_iter(3) == sorted(_months_back_iter(3), reverse=True),
         "_months_back_iter not descending"),
        (len(_months_back_iter(12)) == 12,
         "_months_back_iter wrong length"),
    ]
    failed = [m for ok, m in checks if not ok]
    if failed:
        for f in failed:
            print(f"  [self-test FAIL] {f}", file=sys.stderr)
        return 1
    print(f"  [self-test OK] {len(rows)} rows aggregated; "
          f"{len(checks)} assertions passed.")
    return 0


# ----- CLI -------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Fetch NUFORC UAP sightings.")
    ap.add_argument("--out", default=str(DEFAULT_OUT),
                    help=f"Output JSON path (default: {DEFAULT_OUT})")
    ap.add_argument("--no-network", action="store_true",
                    help="Run offline parser self-test and exit (no HTTP).")
    ap.add_argument("--months-back", type=int, default=144,
                    help="How many months of NUFORC data to scrape "
                         "(default 144 = 12 years). Set lower if the first "
                         "run is too slow.")
    ap.add_argument("--no-live", action="store_true",
                    help="Skip the NUFORC live scrape (planetsig only).")
    args = ap.parse_args(argv)

    if args.no_network:
        return _self_test()

    out_path = Path(args.out)
    prior: dict | None = None
    if out_path.exists():
        try:
            prior = json.loads(out_path.read_text())
        except Exception:
            prior = None

    try:
        payload = fetch_all(months_back=args.months_back,
                            live_scrape=not args.no_live)
    except Exception as e:
        print(f"  [mufon] unexpected fetch error: {e}", file=sys.stderr)
        payload = None

    if payload is None:
        if prior and prior.get("total_records", 0) > 1000:
            print("  [mufon] every probe failed; preserving prior file.",
                  file=sys.stderr)
            # Mark stale so the renderer can show a "data not refreshed" chip.
            prior["_stale"] = True
            out_path.write_text(json.dumps(prior))
            return 1
        # No prior, no fresh data — write a minimal placeholder so the
        # client gets a clean empty-state instead of a 404.
        empty = {
            "generated_at": _now_iso(),
            "source": "unavailable",
            "source_url": None,
            "total_records": 0,
            "date_range": [None, None],
            "by_state_year": {},
            "top_cities_by_state": {},
            "recent_buckets": {},
            "totals_by_year": {},
            "shape_totals": [],
            "shape_by_year": {},
            "totals_by_month": {},
            "shape_by_month": {},
            "_error": "all CSV probes failed",
        }
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(empty))
        return 1

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload))
    print(f"  Wrote {out_path} ({out_path.stat().st_size:,} bytes, "
          f"{payload['total_records']:,} records, "
          f"range {payload['date_range'][0]}..{payload['date_range'][1]})")
    if "_nuforc_live_meta" in payload:
        m = payload["_nuforc_live_meta"]
        print(f"  NUFORC live: {m['months_pulled']} months fetched, "
              f"{m['months_cached']} cached, {m['months_404']} failed, "
              f"{m['wall_clock_sec']}s ({m['stopped_reason']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
