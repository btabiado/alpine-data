#!/usr/bin/env python3
"""Emit data/metro_coords.json — Zillow MSA RegionName -> {lat, lon, cbsa}.

Drives the heat-map / bubble-map visualization on the /real-estate/ page by
mapping each Zillow metro (e.g. "New York, NY") to a representative point
from the Census 2024 CBSA Gazetteer's interior-point lat/lon.

Inputs:
  - data/real_estate.json  (authoritative list of Zillow RegionNames)
  - Census 2024 CBSA Gazetteer (TSV inside a .zip). Includes BOTH Metro
    (CBSA_TYPE==1) and Micro (CBSA_TYPE==2) areas: Zillow's RegionType=="MSA"
    bucket actually contains both flavors (894 Zillow entries vs 393 true
    Metros), and filtering to Metro-only loses ~500 small markets we still
    want on the map. Spec called for `LSAD == "M1"` only; this version
    accepts both CBSA types intentionally.

Matching: the gazetteer's NAME is long-form ("New York-Newark-Jersey City,
NY-NJ-PA"); Zillow's RegionName is short-form ("New York, NY"). We bridge
them by comparing Zillow's short_name against the leading hyphenated city
in the CBSA NAME, requiring the Zillow state to appear in the CBSA state
list. Names are normalized to ascii-lowercase before comparison.

Pure stdlib. Run from repo root: python scripts/fetch_metro_coords.py
"""
from __future__ import annotations

import csv
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
IN_PATH = DATA_DIR / "real_estate.json"
OUT_PATH = DATA_DIR / "metro_coords.json"
CACHE_DIR = DATA_DIR / ".cache" / "real_estate"
# We cache the unpacked TSV (not the .zip) so re-runs skip the unzip step.
CACHE_PATH = CACHE_DIR / "census_cbsa_gazetteer.txt"

USER_AGENT = "btc-eth-etf-dashboard/metro-coords-fetcher (+https://github.com/)"
HTTP_TIMEOUT = 120

# Census publishes the CBSA gazetteer as a .zip wrapping a single .txt.
# (The bare .txt URL is a 404 for 2024 — spec was optimistic about that
# being a viable backup. .zip is the canonical and only form.)
GAZETTEER_ZIP = (
    "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/"
    "2024_Gazetteer/2024_Gaz_cbsa_national.zip"
)
GAZETTEER_TXT_INNER = "2024_Gaz_cbsa_national.txt"


def warn(msg: str) -> None:
    print(f"[warn] {msg}", file=sys.stderr)


def info(msg: str) -> None:
    print(f"[info] {msg}", file=sys.stderr)


def fetch_gazetteer_txt(cache_path: Path) -> bytes:
    """Return the unpacked CBSA gazetteer TSV bytes.

    Strategy: GET the .zip with If-Modified-Since against the cached .txt,
    unzip in memory, write the .txt to cache_path. On 304 / network failure
    with a cache hit, returns the cached bytes. Census mostly doesn't return
    Last-Modified — that's fine, we just re-download each run (the zip is
    ~46 KB).
    """
    headers = {"User-Agent": USER_AGENT}
    if cache_path.exists():
        mtime = cache_path.stat().st_mtime
        headers["If-Modified-Since"] = formatdate(mtime, usegmt=True)

    req = urllib.request.Request(GAZETTEER_ZIP, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            zip_bytes = resp.read()
            last_mod = resp.headers.get("Last-Modified")
    except urllib.error.HTTPError as e:
        if e.code == 304 and cache_path.exists():
            info(f"304 not-modified, using cache: {cache_path.name}")
            return cache_path.read_bytes()
        if cache_path.exists():
            warn(f"HTTP {e.code} on {GAZETTEER_ZIP}; falling back to cache {cache_path.name}")
            return cache_path.read_bytes()
        raise
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        if cache_path.exists():
            warn(f"network error on {GAZETTEER_ZIP}: {e}; using cache {cache_path.name}")
            return cache_path.read_bytes()
        raise

    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            # Be tolerant of Census renaming the inner file; pick the only
            # .txt inside if the expected name isn't present.
            names = zf.namelist()
            inner = GAZETTEER_TXT_INNER if GAZETTEER_TXT_INNER in names else next(
                (n for n in names if n.lower().endswith(".txt")), None
            )
            if inner is None:
                raise RuntimeError(f"no .txt inside gazetteer zip: {names!r}")
            txt_bytes = zf.read(inner)
    except (zipfile.BadZipFile, RuntimeError) as e:
        if cache_path.exists():
            warn(f"unzip failed ({e}); falling back to cache {cache_path.name}")
            return cache_path.read_bytes()
        raise

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(txt_bytes)
    if last_mod:
        try:
            ts = parsedate_to_datetime(last_mod).timestamp()
            os.utime(cache_path, (ts, ts))
        except (TypeError, ValueError):
            pass
    return txt_bytes


def norm(s: str) -> str:
    """Lowercase, strip punctuation except space, collapse whitespace."""
    s = s.lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def parse_gazetteer(body: bytes) -> list[dict[str, str]]:
    """Return all CBSA rows (Metro + Micro) with normalized field names.

    Each row dict has lowercase keys and stripped values. Census gazetteer
    fields (especially INTPTLONG) are right-padded with whitespace; csv with
    tab delimiter leaves that whitespace in place, so we strip on read.

    We keep every CBSA row — Zillow's MSA bucket is a superset of Census
    Metros, and the page wants coords for everything Zillow ships.
    """
    text = body.decode("utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(text), delimiter="\t")
    rows: list[dict[str, str]] = []
    for raw in reader:
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
        if not row.get("geoid"):
            continue
        rows.append(row)
    return rows


def index_gazetteer(rows: list[dict[str, str]]) -> list[dict[str, object]]:
    """Pre-compute the search fields for each CBSA row.

    Each entry: {leading_city (normalized), states (set of upper 2-letter),
    cbsa, lat, lon, name}. The leading city is everything before the first
    hyphen in NAME's pre-comma portion; states come from the post-comma
    portion split on hyphen.
    """
    out: list[dict[str, object]] = []
    for r in rows:
        name = r.get("name", "")
        # 2024 schema appends " Metro Area" / " Micro Area" to NAME; strip it
        # so the post-comma portion is just the hyphenated state list.
        clean = re.sub(r"\s+(Metro|Micro)\s+Area\s*$", "", name)
        if "," not in clean:
            continue
        pre, _, post = clean.partition(",")
        # Zillow's RegionName uses one city from the CBSA's multi-city name —
        # usually the leading one, but sometimes the second (e.g. Zillow's
        # "The Villages, FL" maps to "Wildwood-The Villages, FL"). Index ALL
        # hyphen segments so either lookup direction works.
        cities = [c.strip() for c in pre.split("-") if c.strip()]
        if not cities:
            continue
        leading_city = cities[0]
        states = {s.strip().upper() for s in post.split("-") if s.strip()}
        try:
            lat = float(r.get("intptlat", ""))
            lon = float(r.get("intptlong", ""))
        except ValueError:
            continue
        out.append({
            "leading_city_norm": norm(leading_city),
            "leading_city": leading_city,
            "cities_norm": [norm(c) for c in cities],
            "states": states,
            "cbsa": r.get("geoid", ""),
            "lat": lat,
            "lon": lon,
            "name": clean,
        })
    return out


def match_metro(short_name: str, state: str, index: list[dict[str, object]]) -> dict[str, object] | None:
    """Find the CBSA row whose hyphen-cities include short_name and whose
    state list contains state. Returns None when nothing matches.

    Tiers (first hit wins):
      1. exact match on the leading hyphen-city
      2. exact match on any hyphen-city in the CBSA NAME
      3. fuzzy startswith on the leading city (handles minor punctuation
         drift like "Nashville" vs "Nashville-Davidson--Murfreesboro...")
    """
    target_city = norm(short_name)
    state_u = state.strip().upper()
    # Tier 1: leading city, requires state hit.
    for entry in index:
        if state_u not in entry["states"]:  # type: ignore[operator]
            continue
        if entry["leading_city_norm"] == target_city:
            return entry
    # Tier 2: any hyphen segment (handles "Ogdensburg, NY" -> "Massena-Ogdensburg, NY").
    for entry in index:
        if state_u not in entry["states"]:  # type: ignore[operator]
            continue
        if target_city in entry["cities_norm"]:  # type: ignore[operator]
            return entry
    # Tier 3: prefix-style fallback on leading city only.
    for entry in index:
        if state_u not in entry["states"]:  # type: ignore[operator]
            continue
        leading = entry["leading_city_norm"]
        if leading.startswith(target_city + " ") or target_city.startswith(leading + " "):  # type: ignore[union-attr]
            return entry
    return None


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def main() -> int:
    started = time.time()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    if not IN_PATH.exists():
        warn(f"missing input: {IN_PATH.relative_to(REPO_ROOT)} — run fetch_real_estate.py first")
        return 2

    with IN_PATH.open() as f:
        re_payload = json.load(f)
    zillow_metros = re_payload.get("metros") or []
    if not zillow_metros:
        warn(f"{IN_PATH.relative_to(REPO_ROOT)} has no metros[]")
        return 2

    body = fetch_gazetteer_txt(CACHE_PATH)
    rows = parse_gazetteer(body)
    info(f"gazetteer: {len(rows)} CBSA rows (Metro + Micro)")
    index = index_gazetteer(rows)

    coords: dict[str, dict[str, object]] = {}
    unmatched: list[str] = []
    for m in zillow_metros:
        short = str(m.get("short_name") or "").strip()
        state = str(m.get("state") or "").strip()
        if not short or not state:
            continue
        key = f"{short}, {state}"
        hit = match_metro(short, state, index)
        if hit is None:
            unmatched.append(key)
            continue
        coords[key] = {
            "lat": hit["lat"],
            "lon": hit["lon"],
            "cbsa": hit["cbsa"],
        }

    total = len(zillow_metros)
    matched = len(coords)

    for key in unmatched:
        warn(f"unmatched Zillow metro: {key}")

    payload = {
        "generated_at": now_iso(),
        "source": "Census 2024 CBSA Gazetteer",
        "matched": matched,
        "total": total,
        "coords": coords,
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Compact JSON to keep gh-pages payload small; ~894 entries pretty-printed
    # would 3x the file with no readability benefit.
    OUT_PATH.write_text(json.dumps(payload, separators=(",", ":")))

    elapsed = time.time() - started
    print(f"wrote {OUT_PATH.relative_to(REPO_ROOT)} "
          f"({matched}/{total} Zillow metros matched, took {elapsed:.1f}s)")

    if matched < 800:
        warn(f"catastrophic: only {matched}/{total} matched (threshold 800)")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
