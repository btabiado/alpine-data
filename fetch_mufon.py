"""
UAP / NUFORC sightings fetcher.

The MUFON / UAP tab on the V2 dashboard has three sections:
  1. Latest Updates  — curated static cards, baked into HTML_TEMPLATE.
  2. Document Library — curated static external links, baked into HTML_TEMPLATE.
  3. US Sightings Map — driven by THIS fetcher's output.

This module only powers section 3 (the state heat-map). Sections 1 and 2 do
not change often enough to warrant a fetcher; the operator updates them by
editing v2/app.py directly.

Data source — NUFORC community archive
--------------------------------------
The canonical Renner mirror (``timothyrenner/nuforc_sightings_data``) keeps
its CSVs in a private DVC remote — the GitHub data/ tree is empty by design.
We fall back to other public mirrors of NUFORC's eyewitness reports. The
``planetsig/ufo-reports`` mirror is well-known, MIT-licensed, headerless,
and ships an ~80k-row CSV with columns::

    datetime, city, state, country, shape,
    duration_seconds, duration_text, comments,
    posted, latitude, longitude

Caveat: that mirror was last refreshed in 2014, so anything after 2014 is
absent. We surface ``date_range`` in the payload so the dashboard can be
honest about it.

If any probed URL changes shape, the fetcher's row parser silently drops
bad rows; if EVERY probe fails, we preserve the prior on-disk JSON and
return rc=1 (the V2 build wraps the call in try/except so a fetch failure
never aborts the build).

Output schema (sidecar v2/data-mufon.json) ::

    {
      "generated_at": "2026-05-25T19:30:00Z",
      "source": "planetsig/ufo-reports mirror of NUFORC (MIT)",
      "source_url": "https://raw.githubusercontent.com/.../...csv",
      "total_records": 80332,
      "date_range": ["1949-10-10", "2014-05-08"],
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
        "CA": {"30d": 0, "60d": 0, "90d": 0, "365d": 11},
        ...
      },
      "totals_by_year": {"2014": 1234, "2013": 2345, ...},
      "shape_totals": [
        {"shape": "light", "count": 23456},
        {"shape": "triangle", "count": 8201},
        ...sorted desc; top ~15 + "other" + "unknown"...
      ],
      "shape_by_year": {
        "1906": {"unknown": 2, "light": 0, ...},
        ...
        "2014": {"light": 412, "triangle": 187, ...}
      }
    }

The ``recent_buckets`` field is computed from the CSV's own most-recent
datestamp, NOT from "today" — for a mirror that's stale at 2014, "30d"
means "30 days before the last entry in the dataset." The renderer
displays this honestly.

Shape aggregations
------------------
Two additional top-level keys carry NUFORC shape classifications
(Light, Triangle, Disk, Sphere, Fireball, …). Shape strings are
lowercased and stripped; blanks map to "unknown". To keep the JSON
compact, only the top ~15 shapes are kept as named entries — everything
else collapses into "other". The "unknown" bucket is always preserved
so per-year shape totals sum to ``total_records``::

    "shape_totals": [
      {"shape": "light", "count": 23456},
      {"shape": "triangle", "count": 8201},
      ...sorted desc by count...
    ],
    "shape_by_year": {
      "1906": {"unknown": 2, "light": 0, ...},
      ...
      "2014": {"light": 412, "triangle": 187, ...}
    }

CLI ::

    python fetch_mufon.py                   # default --out v2/data-mufon.json
    python fetch_mufon.py --out PATH
    python fetch_mufon.py --no-network      # offline parser self-test
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any


UA = "Mozilla/5.0 (compatible; etf-flow-dashboard/1.0)"
ROOT = Path(__file__).parent
DEFAULT_OUT = ROOT / "v2" / "data-mufon.json"

# Probed in order. First one that yields >1000 sane rows wins. Adding
# new candidates here is a one-line change; the parser auto-detects the
# shape (header vs no-header) per file.
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
    """Accept the two formats we see in NUFORC mirrors:
      - "M/D/YYYY HH:MM"      (planetsig)
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
    # M/D/YYYY HH:MM (or M/D/YYYY)
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

# Column layouts we know about. Keys are tuples of normalised header names
# (lowercased) when a header IS present; the value is a mapping from logical
# slot -> column index.
KNOWN_HEADER_MAPS: list[tuple[set[str], dict[str, str]]] = [
    # Renner processed shape (header present, columns vary slightly by version).
    (
        {"city", "state", "occurred"},
        {"datetime": "occurred", "city": "city", "state": "state"},
    ),
]


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


def aggregate(rows: list[dict]) -> dict:
    """Roll rows into the dashboard sidecar payload. ``rows`` must be the
    list returned by ``_row_iter``."""
    by_state_year: dict[str, dict[str, int]] = {}
    city_counts: dict[str, dict[str, int]] = {}
    totals_by_year: dict[str, int] = {}
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
        totals_by_year[year] = totals_by_year.get(year, 0) + 1
        shape_totals_raw[shape] = shape_totals_raw.get(shape, 0) + 1
        ybucket = shape_by_year_raw.setdefault(year, {})
        ybucket[shape] = ybucket.get(shape, 0) + 1
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

    # Recent buckets — anchored to the dataset's own latest entry so a stale
    # mirror produces honest "0 in last 30d (since 2014)" answers instead of
    # silently looking like "0 in the last 30 days from today."
    recent_buckets: dict[str, dict[str, int]] = {}
    if max_dt is not None:
        windows = {"30d": 30, "60d": 60, "90d": 90, "365d": 365}
        thresholds = {k: max_dt - timedelta(days=d) for k, d in windows.items()}
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
    }
    return payload


# ----- top-level orchestration ----------------------------------------------

def fetch_all() -> dict | None:
    """Try each candidate URL in order, return the first usable payload (or
    None if every probe failed/returned garbage). Caller wraps in try/except
    + prior-file fallback."""
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
        payload = aggregate(rows)
        payload["source"] = label
        payload["source_url"] = url
        payload["generated_at"] = _now_iso()
        return payload
    return None


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
        (_parse_datetime("bogus") is None, "_parse_datetime rejects garbage"),
        (_norm_state("CA") == "CA", "_norm_state passthrough"),
        (_norm_state("ca") == "CA", "_norm_state lowercases"),
        (_norm_state("XX") is None, "_norm_state rejects unknown"),
        (_norm_state("") is None, "_norm_state rejects empty"),
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
        payload = fetch_all()
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
