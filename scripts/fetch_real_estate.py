#!/usr/bin/env python3
"""Emit data/real_estate.json — top-50 US metro housing market snapshot.

Pulls monthly metro-level housing data from three sources and normalizes them
into a single payload the real-estate dashboard page consumes:

  - Zillow Research CSVs (ZHVI, ZORI, sales count, median sale price,
    new listings, % price cut, for-sale inventory).
  - Redfin Data Center metro market tracker (median DOM, sale-to-list,
    % above list — Redfin-only KPIs).
  - FRED PERMIT series (national housing permits YoY, attached as a single
    top-level number; per-metro permits are emitted as null in v1).

Pure stdlib. HTTP caching is If-Modified-Since against data/.cache/real_estate/.

Also writes data/real_estate_check.json on every successful run. The snapshot
is recommitted only when its substance changes (the upstreams publish
monthly); the check file is how data-health knows the job ran anyway.
A run that would replace a good snapshot with an empty one, a regressed
housing month, or a source that lost more than half its metros exits 1
and writes nothing. Redfin is the exception that can degrade: when its
download contributes 0 rows, the last good Redfin KPIs are carried forward
with an explicit stale flag and their original date, other sources still
update, and the job warns instead of failing. That carry expires; past
the limit the run goes red again. An unlabeled collapse is still refused.

Run from repo root: python scripts/fetch_real_estate.py [--no-cache]
                     python scripts/fetch_real_estate.py --verify-check
"""
from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from email.utils import formatdate, parsedate_to_datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
OUT_PATH = DATA_DIR / "real_estate.json"
CHECK_PATH = DATA_DIR / "real_estate_check.json"
CACHE_DIR = DATA_DIR / ".cache" / "real_estate"
# Clocks the daily workflow ignores when deciding whether the snapshot
# itself is worth a commit. Kept in sync with real-estate-daily.yml's
# --ignore-key list.
_CLOCK_KEYS = frozenset({"generated_at", "fetched_at"})
# A source that previously had at least this many metros and then loses
# more than half of them is a broken fetch, not a quieter housing market.
_COLLAPSE_MIN_PREV_ROWS = 50

USER_AGENT = "alpine-data/real-estate-fetcher (+https://github.com/)"
HTTP_TIMEOUT = 60

ZILLOW_BASE = "https://files.zillowstatic.com/research/public_csvs/"
ZILLOW_URLS: dict[str, str] = {
    "zhvi":         "zhvi/Metro_zhvi_uc_sfrcondo_tier_0.33_0.67_sm_sa_month.csv",
    "zori":         "zori/Metro_zori_uc_sfrcondomfr_sm_month.csv",
    "homes_sold":   "sales_count_now/Metro_sales_count_now_uc_sfrcondo_month.csv",
    "median_sale":  "median_sale_price/Metro_median_sale_price_uc_sfrcondo_sm_sa_month.csv",
    "new_listings": "new_listings/Metro_new_listings_uc_sfrcondo_sm_month.csv",
    "pct_cut":      "perc_listings_price_cut/Metro_perc_listings_price_cut_uc_sfrcondo_sm_month.csv",
    "inventory":    "invt_fs/Metro_invt_fs_uc_sfrcondo_sm_month.csv",
}

# Monthly all-metro housing tracker published by Redfin's Data Center
# downloads page (https://www.redfin.com/news/data-center/downloads/), which
# builds this URL as
#   S3 = https://redfin-public-data.s3.us-west-2.amazonaws.com/redfin_data_center
#   housing_market/monthly/all_metros.csv
# and confirms the same path in redfin_data_center/index.json.
#
# The previous object, redfin_market_tracker/redfin_metro_market_tracker.tsv000.gz
# on the same bucket, has returned S3 AccessDenied (HTTP 403) since 2026-10-27.
# That is a bucket denial, not a renamed key we can guess around: every legacy
# market-tracker key and a bucket listing answer AccessDenied. This CSV is the
# file the downloads page actually links, and it answers 200 to the same
# non-browser User-Agent this fetcher already sends.
REDFIN_URL = (
    "https://redfin-public-data.s3.us-west-2.amazonaws.com/"
    "redfin_data_center/housing_market/monthly/all_metros.csv"
)
# Last successful Redfin fetch we will reuse after a blocked download.
# Measured from that fetch's own timestamp (or the original as_of when the
# rows were already carried), not from the vendor file's Last-Modified,
# which can sit still for months between publishes.
REDFIN_CARRY_MAX_DAYS = 45
REDFIN_KPI_KEYS = ("days_on_market", "sale_to_list", "pct_above_list")

FRED_PERMIT_URL = (
    "https://api.stlouisfed.org/fred/series/observations"
    "?series_id=PERMIT&api_key={key}&file_type=json"
    "&observation_start=2021-01-01"
)

# Fallback Census long-form MSA names, keyed by Zillow's "City, ST" RegionName.
# The primary source for these is the Census Bureau gazetteer fetched at
# runtime by load_census_msa_names() — this dict is only used when that fetch
# is unavailable (e.g. in restricted CI environments). Kept comprehensive
# enough that common principal-city searches ("Fort Myers", "Sarasota",
# "Daytona Beach", "Springdale", etc.) still resolve in fallback mode.
METRO_LONG_NAME_OVERRIDES: dict[str, str] = {
    "New York, NY":        "New York-Newark-Jersey City, NY-NJ-PA",
    "Los Angeles, CA":     "Los Angeles-Long Beach-Anaheim, CA",
    "Chicago, IL":         "Chicago-Naperville-Elgin, IL-IN-WI",
    "Dallas, TX":          "Dallas-Fort Worth-Arlington, TX",
    "Houston, TX":         "Houston-The Woodlands-Sugar Land, TX",
    "Washington, DC":      "Washington-Arlington-Alexandria, DC-VA-MD-WV",
    "Philadelphia, PA":    "Philadelphia-Camden-Wilmington, PA-NJ-DE-MD",
    "Miami, FL":           "Miami-Fort Lauderdale-Pompano Beach, FL",
    "Atlanta, GA":         "Atlanta-Sandy Springs-Alpharetta, GA",
    "Boston, MA":          "Boston-Cambridge-Newton, MA-NH",
    "Phoenix, AZ":         "Phoenix-Mesa-Chandler, AZ",
    "San Francisco, CA":   "San Francisco-Oakland-Berkeley, CA",
    "Riverside, CA":       "Riverside-San Bernardino-Ontario, CA",
    "Detroit, MI":         "Detroit-Warren-Dearborn, MI",
    "Seattle, WA":         "Seattle-Tacoma-Bellevue, WA",
    "Minneapolis, MN":     "Minneapolis-St. Paul-Bloomington, MN-WI",
    "San Diego, CA":       "San Diego-Chula Vista-Carlsbad, CA",
    "Tampa, FL":           "Tampa-St. Petersburg-Clearwater, FL",
    "Denver, CO":          "Denver-Aurora-Lakewood, CO",
    "Baltimore, MD":       "Baltimore-Columbia-Towson, MD",
    "St. Louis, MO":       "St. Louis, MO-IL",
    "Charlotte, NC":       "Charlotte-Concord-Gastonia, NC-SC",
    "Orlando, FL":         "Orlando-Kissimmee-Sanford, FL",
    "San Antonio, TX":     "San Antonio-New Braunfels, TX",
    "Portland, OR":        "Portland-Vancouver-Hillsboro, OR-WA",
    "Sacramento, CA":      "Sacramento-Roseville-Folsom, CA",
    "Pittsburgh, PA":      "Pittsburgh, PA",
    "Las Vegas, NV":       "Las Vegas-Henderson-Paradise, NV",
    "Cincinnati, OH":      "Cincinnati, OH-KY-IN",
    "Austin, TX":          "Austin-Round Rock-Georgetown, TX",
    "Kansas City, MO":     "Kansas City, MO-KS",
    "Columbus, OH":        "Columbus, OH",
    "Cleveland, OH":       "Cleveland-Elyria, OH",
    "Indianapolis, IN":    "Indianapolis-Carmel-Anderson, IN",
    "San Jose, CA":        "San Jose-Sunnyvale-Santa Clara, CA",
    "Nashville, TN":       "Nashville-Davidson--Murfreesboro--Franklin, TN",
    "Virginia Beach, VA":  "Virginia Beach-Norfolk-Newport News, VA-NC",
    "Providence, RI":      "Providence-Warwick, RI-MA",
    "Jacksonville, FL":    "Jacksonville, FL",
    "Milwaukee, WI":       "Milwaukee-Waukesha, WI",
    "Oklahoma City, OK":   "Oklahoma City, OK",
    "Raleigh, NC":         "Raleigh-Cary, NC",
    "Memphis, TN":         "Memphis, TN-MS-AR",
    "Richmond, VA":        "Richmond, VA",
    "Louisville, KY":      "Louisville/Jefferson County, KY-IN",
    "New Orleans, LA":     "New Orleans-Metairie, LA",
    "Salt Lake City, UT":  "Salt Lake City, UT",
    "Hartford, CT":        "Hartford-East Hartford-Middletown, CT",
    "Buffalo, NY":         "Buffalo-Cheektowaga, NY",
    "Birmingham, AL":      "Birmingham-Hoover, AL",
    # Florida — commonly searched principal-city aliases not in the short name.
    "Cape Coral, FL":      "Cape Coral-Fort Myers, FL",
    "North Port, FL":      "North Port-Sarasota-Bradenton, FL",
    "Deltona, FL":         "Deltona-Daytona Beach-Ormond Beach, FL",
    "Palm Bay, FL":        "Palm Bay-Melbourne-Titusville, FL",
    "Lakeland, FL":        "Lakeland-Winter Haven, FL",
    "Naples, FL":          "Naples-Marco Island, FL",
    "Crestview, FL":       "Crestview-Fort Walton Beach-Destin, FL",
    "Sebastian, FL":       "Sebastian-Vero Beach, FL",
    "Homosassa Springs, FL": "Homosassa Springs, FL",
    "Port St. Lucie, FL":  "Port St. Lucie, FL",
    "Pensacola, FL":       "Pensacola-Ferry Pass-Brent, FL",
    # Northeast / Mid-Atlantic
    "Bridgeport, CT":      "Bridgeport-Stamford-Norwalk, CT",
    "New Haven, CT":       "New Haven-Milford, CT",
    "Allentown, PA":       "Allentown-Bethlehem-Easton, PA-NJ",
    "Harrisburg, PA":      "Harrisburg-Carlisle, PA",
    "Scranton, PA":        "Scranton--Wilkes-Barre, PA",
    "York, PA":            "York-Hanover, PA",
    # Carolinas
    "Greensboro, NC":      "Greensboro-High Point, NC",
    "Durham, NC":          "Durham-Chapel Hill, NC",
    "Hickory, NC":         "Hickory-Lenoir-Morganton, NC",
    "Greenville, SC":      "Greenville-Anderson, SC",
    # Deep South
    "Daphne, AL":          "Daphne-Fairhope-Foley, AL",
    "Gulfport, MS":        "Gulfport-Biloxi, MS",
    "Augusta, GA":         "Augusta-Richmond County, GA-SC",
    "Athens, GA":          "Athens-Clarke County, GA",
    # Texas
    "Beaumont, TX":        "Beaumont-Port Arthur, TX",
    "McAllen, TX":         "McAllen-Edinburg-Mission, TX",
    "Brownsville, TX":     "Brownsville-Harlingen, TX",
    "Killeen, TX":         "Killeen-Temple, TX",
    "Bryan, TX":           "Bryan-College Station, TX",
    "Sherman, TX":         "Sherman-Denison, TX",
    # Louisiana / Arkansas
    "Shreveport, LA":      "Shreveport-Bossier City, LA",
    "Houma, LA":           "Houma-Thibodaux, LA",
    "Fayetteville, AR":    "Fayetteville-Springdale-Rogers, AR",
    "Little Rock, AR":     "Little Rock-North Little Rock-Conway, AR",
    "Fort Smith, AR":      "Fort Smith, AR-OK",
    # Midwest
    "Davenport, IA":       "Davenport-Moline-Rock Island, IA-IL",
    "Des Moines, IA":      "Des Moines-West Des Moines, IA",
    "Sioux City, IA":      "Sioux City, IA-NE-SD",
    "Waterloo, IA":        "Waterloo-Cedar Falls, IA",
    "Omaha, NE":           "Omaha-Council Bluffs, NE-IA",
    "Lexington, KY":       "Lexington-Fayette, KY",
    # Mountain / Southwest
    "Provo, UT":           "Provo-Orem, UT",
    "Ogden, UT":           "Ogden-Clearfield, UT",
    "Prescott Valley, AZ": "Prescott Valley-Prescott, AZ",
    "Lake Havasu City, AZ": "Lake Havasu City-Kingman, AZ",
    # California
    "Santa Maria, CA":     "Santa Maria-Santa Barbara, CA",
    "San Luis Obispo, CA": "San Luis Obispo-Paso Robles, CA",
    "Santa Cruz, CA":      "Santa Cruz-Watsonville, CA",
    "Santa Rosa, CA":      "Santa Rosa-Petaluma, CA",
    "Hanford, CA":         "Hanford-Corcoran, CA",
    "Eureka, CA":          "Eureka-Arcata, CA",
    # Pacific NW
    "Eugene, OR":          "Eugene-Springfield, OR",
    "Olympia, WA":         "Olympia-Lacey-Tumwater, WA",
    "Spokane, WA":         "Spokane-Spokane Valley, WA",
    "Kennewick, WA":       "Kennewick-Richland, WA",
    "Mount Vernon, WA":    "Mount Vernon-Anacortes, WA",
    "Bremerton, WA":       "Bremerton-Silverdale-Port Orchard, WA",
    # Hawaii
    "Kahului, HI":         "Kahului-Wailuku-Lahaina, HI",
}


def load_census_msa_names(no_cache: bool) -> list[dict[str, object]] | None:
    """Return the indexed CBSA gazetteer (one entry per Census CBSA), or
    None on fetch/parse failure — callers fall through to
    METRO_LONG_NAME_OVERRIDES.

    Delegates fetch + parse + index to ``scripts/fetch_metro_coords.py``,
    which already handles three Census-quirks we'd otherwise re-implement:

      1. The 2024 schema appends ' Metro Area' / ' Micro Area' to NAME —
         needs stripping or the resulting `name` field is ugly.
      2. Zillow's "MSA" RegionType is a superset of Census Metros — many
         Zillow metros are actually Micropolitan (CBSA_TYPE 2) and would
         be dropped by a naive ``LSAD == 'M1'`` filter.
      3. Three-tier matching for cases where Zillow's RegionName uses
         the second hyphen-city (e.g. Zillow's "The Villages, FL" ->
         CBSA "Wildwood-The Villages, FL") or has punctuation drift
         (e.g. "Nashville-Davidson--Murfreesboro--Franklin, TN").

    ``scripts/`` is auto-prepended to sys.path when either fetcher is run
    directly, so the sibling import resolves without extra hooks.
    """
    try:
        import fetch_metro_coords as fmc
    except ImportError as e:
        warn(f"could not import fetch_metro_coords ({e}); using built-in overrides only")
        return None
    cache_path = CACHE_DIR / "census_cbsa_gazetteer.txt"
    if no_cache and cache_path.exists():
        try:
            cache_path.unlink()
        except OSError:
            pass  # best-effort; at worst the existing cache file is reused
    try:
        body = fmc.fetch_gazetteer_txt(cache_path)
        rows = fmc.parse_gazetteer(body)
        return fmc.index_gazetteer(rows)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
            OSError, zipfile.BadZipFile, RuntimeError) as e:
        warn(f"census gazetteer fetch/parse failed ({e}); using built-in overrides only")
        return None


# Populated by load_metros_from_zillow() at startup. Each entry is a dict:
# {rank: int, full: str, short: str, state: str}. Sorted by rank ascending.
# Replaces the previous hardcoded METROS list — now data-driven off Zillow's
# SizeRank column so coverage matches the source instead of a maintained list.
METROS: list[dict[str, object]] = []


def load_metros_from_zillow(
    max_metros: int | None,
    no_cache: bool,
    census_index: list[dict[str, object]] | None = None,
) -> list[dict[str, object]]:
    """Read Zillow ZHVI CSV, return the metro list ordered by SizeRank.

    Returns one dict per US MSA with rank, full (canonical Census MSA name
    when resolvable, otherwise Zillow's RegionName), short (city portion),
    state (2-letter code). If max_metros is set, truncates after the top N
    by SizeRank. Excludes Puerto Rico to keep focus on US + AK/HI markets.

    Name resolution priority for `full`:
        1. Census Bureau gazetteer via fetch_metro_coords.match_metro().
        2. Hardcoded METRO_LONG_NAME_OVERRIDES (safety net).
        3. Zillow's short-form RegionName.
    """
    cache_path = CACHE_DIR / "zillow_zhvi.csv"
    body, _ = http_get(ZILLOW_BASE + ZILLOW_URLS["zhvi"], cache_path, no_cache)
    text = body.decode("utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    rows: list[dict[str, str]] = []
    for row in reader:
        if (row.get("RegionType") or "").lower() != "msa":
            continue
        if (row.get("StateName") or "").strip() == "Puerto Rico":
            continue
        rows.append(row)

    # Sort by SizeRank (ascending = most populous first).
    def _rank(r: dict[str, str]) -> int:
        try:
            return int(r.get("SizeRank") or 99999)
        except ValueError:
            return 99999
    rows.sort(key=_rank)

    if max_metros and max_metros > 0:
        rows = rows[:max_metros]

    # Only import fetch_metro_coords if we actually have a Census index to
    # look up against — keeps the module decoupled when running in fallback.
    match_fn = None
    if census_index:
        try:
            import fetch_metro_coords as fmc
            match_fn = fmc.match_metro
        except ImportError:
            pass  # matcher is optional; fall back to built-in overrides

    out: list[dict[str, object]] = []
    for i, row in enumerate(rows, start=1):
        region_name = (row.get("RegionName") or "").strip()
        # Zillow format is "City, ST" — split on the last comma to get state.
        if "," in region_name:
            short, _, state = region_name.rpartition(",")
            short = short.strip()
            state = state.strip()
        else:
            short = region_name
            state = ""
        full = region_name
        if match_fn is not None and census_index:
            hit = match_fn(short, state, census_index)
            if hit:
                full = str(hit["name"])
        if full == region_name:
            full = METRO_LONG_NAME_OVERRIDES.get(region_name, region_name)
        out.append({
            "rank": i,
            "full": full,
            "short": short,
            "state": state,
        })
    return out

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def warn(msg: str) -> None:
    print(f"[warn] {msg}", file=sys.stderr)


def info(msg: str) -> None:
    print(f"[info] {msg}", file=sys.stderr)


def http_get(url: str, cache_path: Path | None, no_cache: bool, *,
             allow_error_cache: bool = True) -> tuple[bytes, str | None]:
    """GET url with If-Modified-Since against cache_path. Returns (body, last_modified).

    On 304, reads bytes from cache_path. On network failure with a cache hit
    available, falls back to cached bytes and warns — unless
    ``allow_error_cache`` is false. Redfin sets that false: a 403 served from
    an old cache would look like a fresh download, and a blocked source has
    to be labeled stale instead.
    """
    headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"}
    if cache_path and cache_path.exists() and not no_cache:
        mtime = cache_path.stat().st_mtime
        headers["If-Modified-Since"] = formatdate(mtime, usegmt=True)

    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            body = resp.read()
            last_mod = resp.headers.get("Last-Modified")
            if cache_path:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_bytes(body)
                if last_mod:
                    try:
                        ts = parsedate_to_datetime(last_mod).timestamp()
                        os.utime(cache_path, (ts, ts))
                    except (TypeError, ValueError):
                        pass  # unparseable Last-Modified; keep the file's current mtime
            return body, last_mod
    except urllib.error.HTTPError as e:
        if e.code == 304 and cache_path and cache_path.exists():
            info(f"304 not-modified, using cache: {cache_path.name}")
            return cache_path.read_bytes(), formatdate(cache_path.stat().st_mtime, usegmt=True)
        if allow_error_cache and cache_path and cache_path.exists():
            warn(f"HTTP {e.code} on {url}; falling back to cache {cache_path.name}")
            return cache_path.read_bytes(), formatdate(cache_path.stat().st_mtime, usegmt=True)
        raise
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        if allow_error_cache and cache_path and cache_path.exists():
            warn(f"network error on {url}: {e}; using cache {cache_path.name}")
            return cache_path.read_bytes(), formatdate(cache_path.stat().st_mtime, usegmt=True)
        raise


def parse_zillow_csv(body: bytes) -> tuple[list[str], list[dict[str, str]]]:
    """Return (sorted date columns, rows filtered to MSA region type)."""
    text = body.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    rows: list[dict[str, str]] = []
    fieldnames = reader.fieldnames or []
    for row in reader:
        rtype = (row.get("RegionType") or "").lower()
        if rtype and rtype != "msa":
            continue
        rows.append(row)
    date_cols = sorted(f for f in fieldnames if DATE_RE.match(f))
    return date_cols, rows


def _norm(s: str) -> str:
    s = s.lower()
    s = re.sub(r"[^a-z0-9, ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def match_zillow_row(rows: list[dict[str, str]], short_name: str, state: str) -> dict[str, str] | None:
    """Zillow uses 'New York, NY', 'Los Angeles, CA', etc. — exact short-form match."""
    target = _norm(f"{short_name}, {state}")
    for r in rows:
        if _norm(r.get("RegionName", "")) == target:
            return r
    # second pass: leading-city + state match (handles odd spellings)
    city = _norm(short_name)
    for r in rows:
        name = _norm(r.get("RegionName", ""))
        st = (r.get("StateName") or "").strip().upper()
        if name.startswith(city + ",") and (st == state or name.endswith(f", {state.lower()}")):
            return r
    return None


def last_n_values(row: dict[str, str], date_cols: list[str], n: int = 12) -> list[float | None]:
    """Pull the last n date columns as floats, oldest first; missing → None."""
    tail = date_cols[-n:] if len(date_cols) >= n else date_cols
    out: list[float | None] = []
    # left-pad with Nones if fewer cols than n
    pad = n - len(tail)
    out.extend([None] * pad)
    for d in tail:
        v = row.get(d, "")
        if v in ("", None):
            out.append(None)
        else:
            try:
                out.append(float(v))
            except ValueError:
                out.append(None)
    return out


def latest_value(row: dict[str, str], date_cols: list[str]) -> tuple[float | None, str | None]:
    for d in reversed(date_cols):
        v = row.get(d, "")
        if v not in ("", None):
            try:
                return float(v), d
            except ValueError:
                continue
    return None, None


def value_at_offset(row: dict[str, str], date_cols: list[str], latest_date: str, months_back: int) -> float | None:
    if latest_date not in date_cols:
        return None
    idx = date_cols.index(latest_date) - months_back
    if idx < 0:
        return None
    v = row.get(date_cols[idx], "")
    if v in ("", None):
        return None
    try:
        return float(v)
    except ValueError:
        return None


def yoy_pct(latest: float | None, year_ago: float | None) -> float | None:
    if latest is None or year_ago is None or year_ago == 0:
        return None
    return round((latest / year_ago - 1.0) * 100.0, 2)


def yoy_pp(latest: float | None, year_ago: float | None) -> float | None:
    """Both inputs are decimals (0.22 = 22%). Output is percentage points."""
    if latest is None or year_ago is None:
        return None
    return round((latest - year_ago) * 100.0, 2)


def yoy_delta(latest: float | None, year_ago: float | None) -> int | None:
    if latest is None or year_ago is None:
        return None
    return int(round(latest - year_ago))


def history_monthly(row: dict[str, str], date_cols: list[str], n: int = 60) -> tuple[list[str], list[float | None]]:
    tail = date_cols[-n:] if len(date_cols) >= n else date_cols
    labels = [d[:7] for d in tail]  # YYYY-MM
    vals: list[float | None] = []
    for d in tail:
        v = row.get(d, "")
        if v in ("", None):
            vals.append(None)
        else:
            try:
                vals.append(float(v))
            except ValueError:
                vals.append(None)
    # left-pad if short
    pad = n - len(tail)
    if pad > 0:
        labels = [""] * pad + labels
        vals = [None] * pad + vals
    return labels, vals


# ---- Zillow loader -----------------------------------------------------------

def load_zillow(no_cache: bool) -> dict:
    """Returns a dict keyed by metric → {date_cols, rows_by_rank, latest_date_overall}."""
    out: dict[str, dict] = {}
    last_modified_any: str | None = None
    rows_count = 0
    for metric, suffix in ZILLOW_URLS.items():
        url = ZILLOW_BASE + suffix
        cache_path = CACHE_DIR / f"zillow_{metric}.csv"
        try:
            body, last_mod = http_get(url, cache_path, no_cache)
        except Exception as e:
            warn(f"zillow {metric} fetch failed: {e}")
            continue
        if last_mod and (not last_modified_any or last_mod > last_modified_any):
            last_modified_any = last_mod
        try:
            date_cols, rows = parse_zillow_csv(body)
        except Exception as e:
            warn(f"zillow {metric} parse failed: {e}")
            continue
        rows_by_rank: dict[int, dict[str, str]] = {}
        for m in METROS:
            row = match_zillow_row(rows, str(m["short"]), str(m["state"]))
            if row:
                rows_by_rank[int(m["rank"])] = row
        out[metric] = {"date_cols": date_cols, "rows": rows_by_rank}
        rows_count = max(rows_count, len(rows_by_rank))
        info(f"zillow {metric}: {len(rows_by_rank)}/{len(METROS)} metros matched, {len(date_cols)} months")
    out["_meta"] = {"last_modified": last_modified_any, "rows": rows_count}
    return out


# ---- Redfin loader -----------------------------------------------------------

REDFIN_PCT_RE = re.compile(r"[%,$]")
_REDFIN_NA = frozenset({"na", "n/a", "null", "none", "-"})
# Internal KPI name → headers the two published schemas have used.
# The legacy TSV used the short names; the Data Center CSV uses the long ones.
_REDFIN_COL_ALIASES = {
    "median_dom": ("median_dom", "median days on market (days)"),
    "avg_sale_to_list": ("avg_sale_to_list", "average sale to list ratio (%)"),
    "sold_above_list": ("sold_above_list", "share sold above original list (%)"),
}


def _redfin_float(v: str) -> float | None:
    if v in ("", None):
        return None
    v = REDFIN_PCT_RE.sub("", str(v)).strip()
    if not v or v.lower() in _REDFIN_NA:
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _empty_redfin(error: str | None = None, last_modified: str | None = None) -> dict:
    return {
        "by_rank": {},
        "last_modified": last_modified,
        "dates": [],
        "stale": False,
        "error": error,
    }


def _redfin_header_key(fieldnames: list[str], *candidates: str) -> str | None:
    have = set(fieldnames)
    for name in candidates:
        if name in have:
            return name
    return None


def parse_redfin_body(body: bytes, last_modified: str | None = None) -> dict:
    """Parse a Redfin metro table into ``{by_rank, dates, last_modified}``.

    Accepts the current Data Center CSV and the legacy gzipped TSV. Both
    identify a metro as ``"<city>, <ST> metro area"`` and a month as
    ``period_end``. The legacy file also carried a property-type column;
    only ``All Residential`` was kept. The CSV is already that series and
    has no property-type column.
    """
    raw = body
    if raw[:2] == b"\x1f\x8b":
        try:
            raw = gzip.decompress(raw)
        except OSError as e:
            return _empty_redfin(f"gunzip failed: {e}", last_modified)

    text = raw.decode("utf-8-sig", errors="replace")
    if not text.strip():
        return _empty_redfin("redfin body was empty", last_modified)

    first = text.splitlines()[0]
    delimiter = "\t" if first.count("\t") > first.count(",") else ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    if reader.fieldnames:
        reader.fieldnames = [h.strip().lower() for h in reader.fieldnames]
    fieldnames = list(reader.fieldnames or [])
    columns = {}
    for internal, aliases in _REDFIN_COL_ALIASES.items():
        key = _redfin_header_key(fieldnames, *aliases)
        if key:
            columns[internal] = key
    if len(columns) != len(_REDFIN_COL_ALIASES):
        missing = sorted(set(_REDFIN_COL_ALIASES) - set(columns))
        return _empty_redfin(
            f"unrecognized redfin header, missing {', '.join(missing)}",
            last_modified)

    region_key = _redfin_header_key(fieldnames, "region", "region name")
    period_key = _redfin_header_key(fieldnames, "period_end", "period end")
    rtype_key = _redfin_header_key(fieldnames, "region_type", "region type")
    if region_key is None or period_key is None:
        return _empty_redfin("unrecognized redfin header, missing region or period", last_modified)

    metro_index: dict[str, int] = {}
    for m in METROS:
        key = _norm(f"{m['short']}, {m['state']}")
        metro_index[key] = int(m["rank"])

    by_rank: dict[int, dict[str, dict[str, float | None]]] = {}
    all_dates: set[str] = set()
    for row in reader:
        if rtype_key is not None and (row.get(rtype_key) or "").lower() != "metro":
            continue
        ptype = row.get("property_type")
        if ptype not in (None, "", "All Residential"):
            continue
        region = row.get(region_key) or ""
        # "New York, NY metro area" → "New York, NY"
        m = re.match(r"^(.*?)\s+metro area\s*$", region, re.IGNORECASE)
        short_state = m.group(1) if m else region
        rank = metro_index.get(_norm(short_state))
        if rank is None:
            head = _norm(short_state).split(",")[0].strip()
            for k, v in metro_index.items():
                if k.startswith(head + ","):
                    rank = v
                    break
        if rank is None:
            continue
        period_end = (row.get(period_key) or "").strip()
        if not period_end:
            continue
        all_dates.add(period_end)
        rec = by_rank.setdefault(rank, {}).setdefault(period_end, {})
        for internal, header in columns.items():
            rec[internal] = _redfin_float(row.get(header, ""))

    dates = sorted(all_dates)
    info(f"redfin: {len(by_rank)}/{len(METROS)} metros matched, {len(dates)} period_end dates")
    parsed = _empty_redfin(last_modified=last_modified)
    parsed["by_rank"] = by_rank
    parsed["dates"] = dates
    if not by_rank:
        parsed["error"] = "parsed 0 metro rows"
    return parsed


def load_redfin(no_cache: bool) -> dict:
    """Returns ``{by_rank, last_modified, dates}``. ``error`` is set on failure.

    A failure leaves ``by_rank`` empty so the caller can carry the last good
    rows forward. This does not invent a fresh fetch.
    """
    cache_path = CACHE_DIR / "redfin_housing_market_monthly_metros.csv"
    try:
        # A cached copy of a previous 200 must not satisfy a 403. Conditional
        # 304 is still a real answer from the bucket; an error is not.
        body, last_mod = http_get(
            REDFIN_URL, cache_path, no_cache, allow_error_cache=False)
    except Exception as e:
        warn(f"redfin fetch failed entirely: {e}")
        return _empty_redfin(f"fetch failed: {e}")
    parsed = parse_redfin_body(body, last_mod)
    if parsed.get("error"):
        warn(f"redfin parse failed: {parsed['error']}")
    return parsed


def redfin_series(redfin: dict, rank: int, col: str, n: int = 12) -> tuple[list[float | None], float | None, float | None]:
    """Return (last-n monthly values, latest, year-ago) for a column."""
    by_date = redfin.get("by_rank", {}).get(rank, {})
    dates = redfin.get("dates", [])
    if not by_date or not dates:
        return [None] * n, None, None
    # Restrict to dates present for this metro, sorted.
    metro_dates = sorted(d for d in dates if d in by_date)
    if not metro_dates:
        return [None] * n, None, None
    tail = metro_dates[-n:]
    spark: list[float | None] = []
    pad = n - len(tail)
    spark.extend([None] * pad)
    for d in tail:
        spark.append(by_date.get(d, {}).get(col))
    latest_date = metro_dates[-1]
    latest = by_date.get(latest_date, {}).get(col)
    # find date roughly 12 months earlier
    year_ago_target = _shift_month(latest_date, -12)
    year_ago_val = None
    if year_ago_target:
        # nearest available date <= target
        for d in reversed(metro_dates):
            if d <= year_ago_target:
                year_ago_val = by_date.get(d, {}).get(col)
                break
    return spark, latest, year_ago_val


def _shift_month(date_str: str, months: int) -> str | None:
    try:
        dt = datetime.strptime(date_str[:10], "%Y-%m-%d")
    except ValueError:
        return None
    y = dt.year + (dt.month - 1 + months) // 12
    m = (dt.month - 1 + months) % 12 + 1
    # clamp day
    from calendar import monthrange
    d = min(dt.day, monthrange(y, m)[1])
    return f"{y:04d}-{m:02d}-{d:02d}"


# ---- FRED loader -------------------------------------------------------------

def load_fred() -> dict:
    key = os.environ.get("FRED_API_KEY", "").strip()
    if not key:
        info("FRED_API_KEY not set; skipping FRED")
        return {"national_permits_yoy_pct": None, "fetched_at": None}
    url = FRED_PERMIT_URL.format(key=key)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        warn(f"FRED fetch failed: {e}")
        return {"national_permits_yoy_pct": None, "fetched_at": None}
    obs = payload.get("observations", [])
    # values "." mean missing
    points = [(o["date"], float(o["value"])) for o in obs if o.get("value") not in (".", "", None)]
    if len(points) < 13:
        return {"national_permits_yoy_pct": None, "fetched_at": _now_iso()}
    latest_date, latest = points[-1]
    target = _shift_month(latest_date, -12)
    year_ago = None
    if target:
        for d, v in reversed(points):
            if d <= target:
                year_ago = v
                break
    yoy = yoy_pct(latest, year_ago)
    return {"national_permits_yoy_pct": yoy, "fetched_at": _now_iso()}


# ---- Assembly ----------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def build_metro(metro_def: dict[str, object], zillow: dict, redfin: dict) -> dict:
    rank = int(metro_def["rank"])
    short = str(metro_def["short"])
    state = str(metro_def["state"])
    # `full` is the canonical Census MSA name pre-resolved by
    # load_metros_from_zillow() (Census gazetteer -> overrides -> Zillow).
    full_name = str(metro_def["full"])

    def z_get(metric: str) -> tuple[dict[str, str] | None, list[str]]:
        m = zillow.get(metric)
        if not m:
            return None, []
        return m["rows"].get(rank), m["date_cols"]

    region_id: int | None = None

    # ZHVI block (includes five_year_pct).
    zhvi_row, zhvi_dates = z_get("zhvi")
    zhvi_spark = last_n_values(zhvi_row, zhvi_dates) if zhvi_row else [None] * 12
    zhvi_latest, zhvi_latest_date = (latest_value(zhvi_row, zhvi_dates) if zhvi_row else (None, None))
    zhvi_year_ago = (value_at_offset(zhvi_row, zhvi_dates, zhvi_latest_date, 12)
                     if zhvi_row and zhvi_latest_date else None)
    zhvi_5y = (value_at_offset(zhvi_row, zhvi_dates, zhvi_latest_date, 60)
               if zhvi_row and zhvi_latest_date else None)
    if zhvi_row:
        try:
            region_id = int(zhvi_row.get("RegionID") or 0) or None
        except ValueError:
            region_id = None

    kpis: dict[str, dict] = {}
    kpis["zhvi"] = {
        "value": zhvi_latest,
        "yoy_pct": yoy_pct(zhvi_latest, zhvi_year_ago),
        "five_year_pct": yoy_pct(zhvi_latest, zhvi_5y),
        "spark": zhvi_spark,
    }

    # Simple Zillow KPIs that share the same shape.
    for key, metric in (
        ("median_sale",  "median_sale"),
        ("homes_sold",   "homes_sold"),
        ("new_listings", "new_listings"),
    ):
        row, dates = z_get(metric)
        spark = last_n_values(row, dates) if row else [None] * 12
        latest, latest_d = (latest_value(row, dates) if row else (None, None))
        year_ago = (value_at_offset(row, dates, latest_d, 12) if row and latest_d else None)
        kpis[key] = {"value": latest, "yoy_pct": yoy_pct(latest, year_ago), "spark": spark}
        if region_id is None and row:
            try:
                region_id = int(row.get("RegionID") or 0) or None
            except ValueError:
                pass  # non-numeric RegionID; leave region_id unset

    # Active listings (inventory; best-effort URL).
    inv_row, inv_dates = z_get("inventory")
    inv_spark = last_n_values(inv_row, inv_dates) if inv_row else [None] * 12
    inv_latest, inv_latest_d = (latest_value(inv_row, inv_dates) if inv_row else (None, None))
    inv_year_ago = (value_at_offset(inv_row, inv_dates, inv_latest_d, 12) if inv_row and inv_latest_d else None)
    kpis["active_listings"] = {
        "value": inv_latest, "yoy_pct": yoy_pct(inv_latest, inv_year_ago), "spark": inv_spark,
    }

    # % price cut — Zillow series is a 0..100 percentage in some files and
    # 0..1 in others; normalize to a decimal (0.31).
    cut_row, cut_dates = z_get("pct_cut")
    cut_spark_raw = last_n_values(cut_row, cut_dates) if cut_row else [None] * 12
    cut_spark = _normalize_pct(cut_spark_raw)
    cut_latest_raw, cut_latest_d = (latest_value(cut_row, cut_dates) if cut_row else (None, None))
    cut_year_ago_raw = (value_at_offset(cut_row, cut_dates, cut_latest_d, 12)
                        if cut_row and cut_latest_d else None)
    cut_latest = _normalize_pct_one(cut_latest_raw)
    cut_year_ago = _normalize_pct_one(cut_year_ago_raw)
    kpis["pct_price_cut"] = {
        "value": cut_latest,
        "yoy_pp": yoy_pp(cut_latest, cut_year_ago),
        "spark": cut_spark,
    }

    # Redfin-sourced KPIs.
    dom_spark, dom_latest, dom_year_ago = redfin_series(redfin, rank, "median_dom")
    kpis["days_on_market"] = {
        "value": int(dom_latest) if dom_latest is not None else None,
        "yoy_delta": yoy_delta(dom_latest, dom_year_ago),
        "spark": [int(v) if v is not None else None for v in dom_spark],
    }

    s2l_spark, s2l_latest, s2l_year_ago = redfin_series(redfin, rank, "avg_sale_to_list")
    s2l_spark_n = [_normalize_pct_one(v) for v in s2l_spark]
    s2l_latest_n = _normalize_pct_one(s2l_latest)
    s2l_year_ago_n = _normalize_pct_one(s2l_year_ago)
    kpis["sale_to_list"] = {
        "value": s2l_latest_n,
        "yoy_pp": yoy_pp(s2l_latest_n, s2l_year_ago_n),
        "spark": s2l_spark_n,
    }

    abv_spark, abv_latest, abv_year_ago = redfin_series(redfin, rank, "sold_above_list")
    abv_spark_n = [_normalize_pct_one(v) for v in abv_spark]
    abv_latest_n = _normalize_pct_one(abv_latest)
    abv_year_ago_n = _normalize_pct_one(abv_year_ago)
    kpis["pct_above_list"] = {
        "value": abv_latest_n,
        "yoy_pp": yoy_pp(abv_latest_n, abv_year_ago_n),
        "spark": abv_spark_n,
    }

    carried = carried_redfin_kpis(redfin, metro_def)
    if carried:
        for key in REDFIN_KPI_KEYS:
            if key in carried:
                kpis[key] = carried[key]

    # Permits — null at metro level in v1 (national value lives in sources.fred).
    kpis["permits"] = {"value": None, "yoy_pct": None, "spark": []}

    # 5y monthly history for the timeline chart.
    if zhvi_row:
        labels, zhvi_60 = history_monthly(zhvi_row, zhvi_dates, 60)
    else:
        labels, zhvi_60 = [], [None] * 60
    ms_row, ms_dates = z_get("median_sale")
    if ms_row and ms_dates:
        ms_labels, ms_60 = history_monthly(ms_row, ms_dates, 60)
        if not labels:
            labels = ms_labels
    else:
        ms_60 = [None] * 60

    # Bonus: ZORI + rent-to-price ratio (annualized rent / ZHVI).
    zori_row, zori_dates = z_get("zori")
    zori_latest, zori_latest_d = (latest_value(zori_row, zori_dates) if zori_row else (None, None))
    zori_year_ago = (value_at_offset(zori_row, zori_dates, zori_latest_d, 12)
                     if zori_row and zori_latest_d else None)
    rent_to_price = None
    if zori_latest and zhvi_latest:
        try:
            rent_to_price = round((zori_latest * 12.0) / zhvi_latest, 4)
        except ZeroDivisionError:
            rent_to_price = None

    return {
        "rank": rank,
        "name": full_name,
        "short_name": short,
        "state": state,
        "zillow_region_id": region_id,
        "kpis": kpis,
        "history_5y_monthly": {
            "labels": labels,
            "zhvi": zhvi_60,
            "median_sale": ms_60,
        },
        "bonus": {
            "zori_rent": {
                "value": zori_latest,
                "yoy_pct": yoy_pct(zori_latest, zori_year_ago),
            },
            "rent_to_price": rent_to_price,
        },
    }


def _normalize_pct_one(v: float | None) -> float | None:
    """Coerce a value that may be 0..1 or 0..100 into a 0..1 decimal."""
    if v is None:
        return None
    if v > 1.5:
        return round(v / 100.0, 4)
    return round(v, 4)


def _normalize_pct(vals: list[float | None]) -> list[float | None]:
    return [_normalize_pct_one(v) for v in vals]


KPI_KEYS = (
    "zhvi", "median_sale", "homes_sold", "active_listings", "new_listings",
    "days_on_market", "sale_to_list", "pct_above_list", "pct_price_cut", "permits",
)


def populated_kpi_count(metro: dict) -> int:
    return sum(1 for k in KPI_KEYS if metro["kpis"].get(k, {}).get("value") is not None)


def _monitor():
    """build_health_status, loaded by path so this script does not depend on
    cwd or sys.path. The housing-month helpers live there so the watchdog
    and this writer cannot disagree about what month the file is as of."""
    name = "build_health_status"
    mod = sys.modules.get(name)
    if mod is not None and hasattr(mod, "newest_housing_month"):
        return mod
    import importlib.util
    path = Path(__file__).resolve().parent / "build_health_status.py"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _strip_clocks(obj):
    """obj with generated_at / fetched_at removed at every depth."""
    if isinstance(obj, dict):
        return {k: _strip_clocks(v) for k, v in obj.items() if k not in _CLOCK_KEYS}
    if isinstance(obj, list):
        return [_strip_clocks(v) for v in obj]
    return obj


def _source_rows(source: object) -> int:
    if not isinstance(source, dict):
        return 0
    try:
        return int(source.get("rows") or 0)
    except (TypeError, ValueError):
        return 0


def carried_redfin_kpis(redfin: dict, metro_def: dict) -> dict | None:
    """Last-good Redfin KPI blocks for one metro, or None on a fresh fetch."""
    if not redfin.get("stale"):
        return None
    by_name = redfin.get("carried_kpis_by_name") or {}
    hit = by_name.get((str(metro_def.get("short")), str(metro_def.get("state"))))
    if hit:
        return hit
    by_rank = redfin.get("carried_kpis_by_rank") or {}
    try:
        return by_rank.get(int(metro_def["rank"]))
    except (KeyError, TypeError, ValueError):
        return None


def redfin_source_as_of(src: object) -> str | None:
    """Date the Redfin rows actually belong to.

    A fresh source is dated by ``fetched_at``. A source that is already a
    carry is dated by ``as_of`` / ``carried_forward_from`` only, so a later
    run cannot reset the clock by rewriting ``fetched_at``.
    """
    if not isinstance(src, dict):
        return None
    if src.get("stale"):
        keys = ("as_of", "carried_forward_from")
    else:
        keys = ("fetched_at",)
    for key in keys:
        val = src.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def redfin_carry_age_days(as_of: str, now: datetime) -> float | None:
    dt = _monitor()._parse_date_value(as_of)
    if dt is None:
        return None
    return (now - dt).total_seconds() / 86400.0


def carry_forward_redfin(previous: dict | None, *, reason: str,
                         now: datetime | None = None) -> dict | None:
    """Last good Redfin KPIs, labeled stale, or None when they cannot be reused.

    The returned dict has no fresh ``by_rank`` series. ``build_metro`` copies
    the KPI blocks instead, each marked ``stale``. ``rows`` counts metros
    that still have a Redfin value, which is what the collapse check compares.
    """
    if not isinstance(previous, dict):
        return None
    sources = previous.get("sources") if isinstance(previous.get("sources"), dict) else {}
    prev_src = sources.get("redfin") if isinstance(sources.get("redfin"), dict) else {}
    if _source_rows(prev_src) <= 0:
        return None
    as_of = redfin_source_as_of(prev_src)
    if not as_of:
        return None
    now = now or datetime.now(timezone.utc)
    age = redfin_carry_age_days(as_of, now)
    if age is None or age < -1 or age > REDFIN_CARRY_MAX_DAYS:
        return None
    metros = previous.get("metros")
    if not isinstance(metros, list):
        return None

    by_name: dict[tuple[str, str], dict] = {}
    by_rank: dict[int, dict] = {}
    n = 0
    for metro in metros:
        if not isinstance(metro, dict):
            continue
        kpis = metro.get("kpis") if isinstance(metro.get("kpis"), dict) else {}
        copied: dict[str, dict] = {}
        any_val = False
        for key in REDFIN_KPI_KEYS:
            block = kpis.get(key)
            if not isinstance(block, dict):
                continue
            cloned = json.loads(json.dumps(block))
            cloned["stale"] = True
            copied[key] = cloned
            if block.get("value") is not None:
                any_val = True
        if not any_val:
            continue
        n += 1
        by_name[(str(metro.get("short_name") or ""), str(metro.get("state") or ""))] = copied
        try:
            by_rank[int(metro["rank"])] = copied
        except (KeyError, TypeError, ValueError):
            pass
    if n <= 0:
        return None
    return {
        "by_rank": {},
        "dates": [],
        "last_modified": prev_src.get("last_modified"),
        "fetched_at": as_of,
        "rows": n,
        "stale": True,
        "as_of": as_of,
        "carried_forward_from": as_of,
        "stale_reason": reason,
        "error": None,
        "carried_kpis_by_name": by_name,
        "carried_kpis_by_rank": by_rank,
    }


def carry_refusal_reason(previous: dict | None, now: datetime | None = None) -> str | None:
    """Why a 0-row Redfin fetch was not allowed to reuse the previous snapshot.

    Returned only when the previous snapshot had Redfin rows and those rows
    are too old, undated, or undatable. A young previous snapshot returns
    None: refusing the empty payload is still correct, and the caller is
    expected to have carried those rows forward before it got here.
    """
    if not isinstance(previous, dict):
        return None
    sources = previous.get("sources") if isinstance(previous.get("sources"), dict) else {}
    prev_src = sources.get("redfin") if isinstance(sources.get("redfin"), dict) else {}
    if _source_rows(prev_src) <= 0:
        return None
    as_of = redfin_source_as_of(prev_src)
    now = now or datetime.now(timezone.utc)
    if not as_of:
        return "redfin carry-forward refused: last good snapshot has no original date"
    age = redfin_carry_age_days(as_of, now)
    if age is None:
        return f"redfin carry-forward refused: last good date {as_of!r} is not a timestamp"
    if age < -1:
        return f"redfin carry-forward refused: last good date {as_of} is in the future"
    if age > REDFIN_CARRY_MAX_DAYS:
        return (
            f"redfin carry-forward expired: last good fetch {as_of} is {age:.0f}d old "
            f"(limit {REDFIN_CARRY_MAX_DAYS}d)")
    return None


def warn_degraded_redfin(redfin: dict) -> None:
    """Actions warning. The run still exits 0; the annotation is the signal."""
    msg = (
        f"Redfin contributed 0 fresh rows ({redfin.get('stale_reason') or 'fetch failed'}); "
        f"carried forward {redfin.get('rows')} rows from {redfin.get('as_of')} "
        f"(stale, limit {REDFIN_CARRY_MAX_DAYS}d). Other sources still update."
    )
    warn(msg)
    # Workflow commands are read from stdout.
    print(f"::warning title=Redfin source degraded::{msg}")


def redfin_source_block(redfin: dict) -> dict:
    """``sources.redfin`` for the payload. Stale carries keep the original date."""
    if redfin.get("stale"):
        return {
            "fetched_at": redfin.get("fetched_at"),
            "last_modified": redfin.get("last_modified"),
            "rows": redfin.get("rows", 0),
            "stale": True,
            "as_of": redfin.get("as_of"),
            "carried_forward_from": redfin.get("carried_forward_from"),
            "stale_reason": redfin.get("stale_reason"),
        }
    return {
        "fetched_at": _now_iso(),
        "last_modified": redfin.get("last_modified"),
        "rows": len(redfin.get("by_rank") or {}),
    }


def _redfin_stale_problems(sources: dict, now: datetime) -> list[str]:
    """A stale label is only valid when it names a date inside the carry window.

    Rows that are simply old are not this check. This check is the backstop
    that refuses a carry with the flag set and the date missing or expired,
    so a stale slice cannot be published as if the date did not matter.
    """
    src = sources.get("redfin") if isinstance(sources.get("redfin"), dict) else {}
    if not src.get("stale"):
        return []
    problems: list[str] = []
    as_of = src.get("as_of") or src.get("carried_forward_from")
    if not isinstance(as_of, str) or not as_of.strip():
        problems.append("redfin is marked stale but has no original as_of date")
        return problems
    age = redfin_carry_age_days(as_of.strip(), now)
    if age is None:
        problems.append(f"redfin stale as_of is not a timestamp ({as_of!r})")
    elif age < -1 or age > REDFIN_CARRY_MAX_DAYS:
        problems.append(
            f"redfin carry-forward is {age:.0f}d from the run clock "
            f"(limit {REDFIN_CARRY_MAX_DAYS}d from {as_of}); refusing to publish it")
    if _source_rows(src) <= 0:
        problems.append("redfin is marked stale but contributed 0 rows")
    return problems


def _load_json_object(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def payload_problems(payload: dict, previous: dict | None,
                     now: datetime | None = None) -> list[str]:
    """Reasons this payload must not replace the committed snapshot.

    An empty or collapsed fetch used to exit 0 as long as one of the two
    primary sources returned anything, and the workflow would then commit
    the nulls over last-known-good. That is an update, but not one we want.

    A Redfin slice carried forward from the last good fetch is allowed only
    when it is labeled ``stale`` and names its original date, and that date
    is inside ``REDFIN_CARRY_MAX_DAYS``. Zero rows are still refused, labeled
    or not: the carry must bring the rows with it, not just the flag.
    """
    problems: list[str] = []
    bhs = _monitor()
    now = now or datetime.now(timezone.utc)
    metros = payload.get("metros")
    if not isinstance(metros, list) or not metros:
        problems.append("payload has no metros")
    month = bhs.newest_housing_month(payload)
    if isinstance(metros, list) and metros and not month:
        problems.append("payload has metros but no YYYY-MM housing month")
    sources = payload.get("sources") if isinstance(payload.get("sources"), dict) else {}
    for name in ("zillow", "redfin"):
        if _source_rows(sources.get(name)) <= 0:
            problems.append(f"{name} contributed 0 rows")
    if _source_rows(sources.get("redfin")) <= 0:
        refusal = carry_refusal_reason(previous, now)
        if refusal:
            problems.append(refusal)
    problems.extend(_redfin_stale_problems(sources, now))
    if not isinstance(previous, dict):
        return problems
    prev_month = bhs.newest_housing_month(previous)
    if prev_month and month and month < prev_month:
        problems.append(f"housing month regressed from {prev_month} to {month}")
    prev_sources = previous.get("sources") if isinstance(previous.get("sources"), dict) else {}
    for name in ("zillow", "redfin"):
        prev_rows = _source_rows(prev_sources.get(name))
        rows = _source_rows(sources.get(name))
        if prev_rows < _COLLAPSE_MIN_PREV_ROWS:
            continue
        if rows == 0:
            problems.append(f"{name} rows collapsed from {prev_rows} to 0")
        elif rows * 2 < prev_rows:
            problems.append(
                f"{name} rows fell from {prev_rows} to {rows} "
                "(more than half the metros disappeared)")
    return problems


def substance_changed(previous: dict | None, payload: dict) -> bool:
    """True when the snapshot differs from `previous` by more than run clocks.

    Compared after a JSON round-trip, which is what ``json_content_changed.py``
    sees once the file is written. In-memory floats and the on-disk encoding
    of those same floats are not always ``==``.
    """
    if previous is None:
        return True
    normalized = json.loads(json.dumps(payload, separators=(",", ":")))
    return _strip_clocks(previous) != _strip_clocks(normalized)


def write_check_record(payload: dict, *, payload_changed: bool,
                       checked_at: str | None = None,
                       snapshot_generated_at: str | None = None) -> dict:
    """Record that this run succeeded. Committed even when the snapshot is not.

    `generated_at` is the check time on purpose: data-health dates this file
    from that key, and the key must move on a run whose snapshot did not.
    `snapshot_generated_at` is the stamp that stays in git — the previous
    one when this run only moved clocks and the workflow restores the
    snapshot, the new one when the substance is committed.
    """
    bhs = _monitor()
    sources = payload.get("sources") if isinstance(payload.get("sources"), dict) else {}

    def brief(name: str) -> dict:
        src = sources.get(name) if isinstance(sources.get(name), dict) else {}
        keep = ("rows", "last_modified", "national_permits_yoy_pct",
                "stale", "as_of", "stale_reason")
        return {k: src[k] for k in keep if k in src}

    rec = {
        "generated_at": checked_at or payload.get("generated_at") or _now_iso(),
        "outcome": "updated" if payload_changed else "unchanged",
        "housing_month": bhs.newest_housing_month(payload),
        "snapshot_generated_at": (
            payload.get("generated_at") if snapshot_generated_at is None
            else snapshot_generated_at),
        "sources": {name: brief(name) for name in ("zillow", "redfin", "fred")},
    }
    redfin_src = sources.get("redfin") if isinstance(sources.get("redfin"), dict) else {}
    if redfin_src.get("stale"):
        rec["warnings"] = [{
            "source": "redfin",
            "level": "warning",
            "as_of": redfin_src.get("as_of"),
            "message": redfin_src.get("stale_reason") or "carried forward; upstream unavailable",
        }]
    CHECK_PATH.parent.mkdir(parents=True, exist_ok=True)
    CHECK_PATH.write_text(json.dumps(rec, indent=2) + "\n", encoding="utf-8")
    return rec


def check_record_problems(check: object, payload: dict, *,
                          now: datetime | None = None,
                          max_age_h: float = 6.0) -> list[str]:
    """Why a check record does not prove that THIS run looked at THIS payload.

    The workflow runs this after the fetch. A fetch that exits 0 without
    refreshing the record — or that records a different month than the
    snapshot it just wrote — is a failed run, not a quiet success.
    """
    if not isinstance(check, dict):
        return ["check record is not a JSON object"]
    problems: list[str] = []
    bhs = _monitor()
    raw = check.get("generated_at")
    dt = bhs._parse_date_value(raw) if isinstance(raw, str) else None
    if dt is None:
        problems.append(f"check record generated_at is missing or not a timestamp ({raw!r})")
    else:
        now = now or datetime.now(timezone.utc)
        age_h = (now - dt).total_seconds() / 3600.0
        if age_h > max_age_h or age_h < -1:
            problems.append(
                f"check record generated_at is {age_h:.1f}h off the clock "
                f"(limit {max_age_h:.0f}h); this run did not record a check")
    month = bhs.newest_housing_month(payload)
    if check.get("housing_month") != month:
        problems.append(
            f"check record housing_month is {check.get('housing_month')!r}, "
            f"snapshot month is {month!r}")
    if check.get("outcome") not in ("updated", "unchanged"):
        problems.append(f"check record outcome is {check.get('outcome')!r}")
    return problems


def reject_payload(problems: list[str]) -> int:
    """Exit path for a fetch that must not touch the committed files."""
    detail = "; ".join(problems)
    print(f"::error title=Real estate snapshot rejected::{detail}", file=sys.stderr)
    for problem in problems:
        print(f"real-estate: refusing to write: {problem}", file=sys.stderr)
    return 1


def verify_check_record(now: datetime | None = None) -> int:
    """``--verify-check``: the on-disk check record must describe this snapshot."""
    payload = _load_json_object(OUT_PATH)
    check = _load_json_object(CHECK_PATH)
    problems: list[str] = []
    if payload is None:
        problems.append(f"{OUT_PATH.name} is missing or not a JSON object")
    if check is None:
        problems.append(f"{CHECK_PATH.name} is missing or not a JSON object")
    if payload is not None and check is not None:
        problems.extend(check_record_problems(check, payload, now=now))
    if problems:
        detail = "; ".join(problems)
        print(f"::error title=Real estate check record rejected::{detail}", file=sys.stderr)
        for problem in problems:
            print(f"real-estate: {problem}", file=sys.stderr)
        return 1
    print(f"real-estate: check record ok "
          f"(outcome={check.get('outcome')}, housing_month={check.get('housing_month')}, "
          f"checked_at={check.get('generated_at')})")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--verify-check", action="store_true",
                        help="Validate data/real_estate_check.json against the "
                             "snapshot and exit. Does not fetch.")
    parser.add_argument("--no-cache", action="store_true", help="Bypass HTTP cache")
    parser.add_argument("--max-metros", type=int, default=0,
                        help="Cap number of metros (default 0 = all US MSAs from Zillow). "
                             "Useful for fast local iteration with e.g. --max-metros 20.")
    args = parser.parse_args()
    if args.verify_check:
        return verify_check_record()

    started = time.time()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # Pull the Census Bureau gazetteer first — gives us the full hyphenated
    # MSA name (e.g. "Cape Coral-Fort Myers, FL") for every US MSA, which is
    # what the dashboard search greps over to find principal-city aliases.
    census_index = load_census_msa_names(args.no_cache)
    if census_index:
        info(f"census gazetteer: {len(census_index)} CBSAs indexed")

    # Bootstrap the metro list off Zillow's SizeRank column. Mutates the
    # module-level METROS so downstream loaders pick up the same set.
    global METROS
    METROS = load_metros_from_zillow(args.max_metros or None, args.no_cache, census_index)
    info(f"metros: loaded {len(METROS)} from Zillow"
         + (f" (capped to top {args.max_metros})" if args.max_metros else " (all US MSAs)"))

    previous = _load_json_object(OUT_PATH)
    zillow = load_zillow(args.no_cache)
    redfin = load_redfin(args.no_cache)
    if not redfin.get("by_rank"):
        carried = carry_forward_redfin(
            previous,
            reason=redfin.get("error") or "redfin contributed 0 rows")
        if carried:
            warn_degraded_redfin(carried)
            redfin = carried
    fred = load_fred()

    zillow_has_data = any(k for k in zillow.keys() if k != "_meta")
    if not zillow_has_data and not redfin.get("by_rank") and not redfin.get("stale"):
        warn("all primary sources failed (zillow + redfin); aborting")
        return 1

    metros: list[dict] = []
    for m in METROS:
        rank = int(m["rank"])
        try:
            metros.append(build_metro(m, zillow, redfin))
        except Exception as e:
            warn(f"build_metro rank={rank} failed: {e}")
            metros.append({
                "rank": rank,
                "name": str(m["full"]),
                "short_name": str(m["short"]),
                "state": str(m["state"]),
                "zillow_region_id": None,
                "kpis": {k: {"value": None, "spark": []} for k in KPI_KEYS},
                "history_5y_monthly": {"labels": [], "zhvi": [], "median_sale": []},
                "bonus": {"zori_rent": {"value": None, "yoy_pct": None}, "rent_to_price": None},
            })

    metros.sort(key=lambda m: m["rank"])

    payload = {
        "generated_at": _now_iso(),
        "sources": {
            "zillow": {
                "fetched_at": _now_iso(),
                "last_modified": zillow.get("_meta", {}).get("last_modified"),
                "rows": zillow.get("_meta", {}).get("rows", 0),
            },
            "redfin": redfin_source_block(redfin),
            "fred": {
                "fetched_at": fred.get("fetched_at"),
                "national_permits_yoy_pct": fred.get("national_permits_yoy_pct"),
            },
        },
        "metros": metros,
    }

    problems = payload_problems(payload, previous)
    if problems:
        return reject_payload(problems)
    changed = substance_changed(previous, payload)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Compact JSON — at 894 metros, indent=2 inflates the file by ~3x with
    # no readability benefit (no human's reading 7 MB by hand). gh-pages
    # auto-gzips text/* responses so over-the-wire size is ~25% of disk.
    OUT_PATH.write_text(json.dumps(payload, separators=(",", ":")))
    # The check record is the freshness signal for "this run succeeded".
    # It is written even when the snapshot's substance did not move, and
    # the workflow commits it on those days instead of the 3MB file.
    # When the substance did not move, the workflow restores the snapshot,
    # so the stamp that remains committed is the previous one.
    kept_stamp = payload.get("generated_at") if changed else (previous or {}).get("generated_at")
    write_check_record(payload, payload_changed=changed, snapshot_generated_at=kept_stamp)

    avg_pop = sum(populated_kpi_count(m) for m in metros) / max(len(metros), 1)
    elapsed = time.time() - started
    disposition = "substance changed" if changed else (
        "substance unchanged; snapshot clocks moved in the working tree only, "
        "check record is the commit")
    print(f"wrote {OUT_PATH.relative_to(REPO_ROOT)} "
          f"({len(metros)} metros, {avg_pop:.1f}/10 KPIs populated on avg, "
          f"{disposition}, took {elapsed:.1f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
