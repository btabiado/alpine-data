"""
build_r2_coverage.py — Scan R2 backfill bucket + build a coverage map.

Lists s3://adw-warehouse/raw/alpine-data/{YYYY-MM-DD}/{file} keys, groups
them by source × date, categorises each source, writes coverage JSON for
the static dashboard at /health/r2-coverage.html.

Run by pages.yml CI (after upload_to_r2.py, before deploy) AND by the
r2-backfill.yml workflow_dispatch (so the dashboard reflects the latest
backfill state). Idempotent.

Output schema:
{
  "generated_utc": "2026-06-23T12:34:56Z",
  "bucket": "adw-warehouse",
  "earliest_date": "2026-06-23",
  "latest_date":   "2026-06-23",
  "dates":         ["2026-06-23", ...],
  "categories": {
    "Macro":   [{"file": "data-cpi.json",      "present": {"2026-06-23": true, ...}}, ...],
    "Markets": [{"file": "data-metals.json",   ...}, ...],
    ...
  },
  "totals": {"sources": 18, "dates": 173, "cells_present": 1240, "coverage_pct": 39.8}
}

If R2 creds are missing / bucket empty, writes an empty-but-valid coverage
file with totals.sources=0 so the dashboard renders an empty-state.
"""

import os
import sys
import json
from datetime import datetime, timezone
from collections import defaultdict

try:
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError
except ImportError:
    boto3 = None

ACCOUNT_ID = "d486b561a8eacd568dd8edf9c749ee47"
R2_ENDPOINT_URL = f"https://{ACCOUNT_ID}.r2.cloudflarestorage.com"
PREFIX = "raw/alpine-data/"
OUT_PATH = "health/r2-coverage.json"

# Source categorisation — filename → category. Anything not listed → "Other".
# Order = display order within category.
CATEGORIES = {
    "Macro (rate/inflation/supply)": [
        "data-cpi.json",
        "data-supplies.json",
    ],
    "Markets (equities, commodities, flow)": [
        "data-metals.json",
        "data-stock-money-flow.json",
    ],
    "Crypto (BTC/ETH/whale)": [
        "data-whale.json",
    ],
    "Logistics & Travel (aviation, TSA, freight)": [
        "data-aviation.json",
        "data-opensky.json",
        "data-opensky-positions.json",
        "data-travel.json",
        "data-tsa.json",
    ],
    "Geo (cities, states, regional)": [
        "data-city.json",
        "data-us_states.json",
    ],
    "Long-tail (UAP/MUFON, misc)": [
        "data-mufon.json",
    ],
}


def categorise(filename):
    # Strip v2/ prefix for categorisation; v2/data-cpi.json should land under same Macro row.
    base = filename.replace("v2/", "")
    for cat, files in CATEGORIES.items():
        if base in files:
            return cat
    return "Other (uncategorised)"


def empty_payload(reason):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "generated_utc": now,
        "bucket": os.environ.get("R2_BUCKET_NAME", "(unset)"),
        "earliest_date": None,
        "latest_date": None,
        "dates": [],
        "categories": {},
        "totals": {"sources": 0, "dates": 0, "cells_present": 0, "coverage_pct": 0.0},
        "empty_reason": reason,
    }


def main():
    bucket = os.environ.get("R2_BUCKET_NAME")
    if not bucket or not boto3:
        reason = "R2_BUCKET_NAME unset" if not bucket else "boto3 not installed"
        print(f"[coverage] skipping scan — {reason}; writing empty payload.")
        payload = empty_payload(reason)
        os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
        with open(OUT_PATH, "w") as f:
            json.dump(payload, f, indent=2)
        sys.exit(0)

    client = boto3.client(
        "s3",
        endpoint_url=R2_ENDPOINT_URL,
        region_name="auto",
    )

    print(f"[coverage] scanning s3://{bucket}/{PREFIX} ...")

    # filename → date set
    file_dates = defaultdict(set)
    all_dates = set()
    seen = 0
    total_bytes = 0
    file_bytes = defaultdict(int)   # filename → cumulative bytes across all dates

    try:
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=PREFIX):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                size = obj.get("Size", 0)
                # Strip prefix: raw/alpine-data/{date}/{rest}
                rest = key[len(PREFIX):]
                if "/" not in rest:
                    continue
                date_part, _, file_part = rest.partition("/")
                # Validate date shape
                if len(date_part) != 10 or date_part[4] != "-":
                    continue
                # Skip MANIFEST.json — it's per-day metadata, not a source file
                if file_part == "MANIFEST.json":
                    continue
                file_dates[file_part].add(date_part)
                all_dates.add(date_part)
                file_bytes[file_part] += size
                total_bytes += size
                seen += 1
    except (BotoCoreError, ClientError) as e:
        print(f"[coverage] R2 scan failed: {e}; writing empty payload.")
        payload = empty_payload(f"R2 scan error: {type(e).__name__}")
        os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
        with open(OUT_PATH, "w") as f:
            json.dump(payload, f, indent=2)
        sys.exit(0)

    print(f"[coverage] scanned {seen} keys across {len(file_dates)} sources, {len(all_dates)} dates")

    if not all_dates:
        payload = empty_payload("R2 bucket empty under prefix — backfill not yet run.")
        os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
        with open(OUT_PATH, "w") as f:
            json.dump(payload, f, indent=2)
        print(f"[coverage] wrote empty payload to {OUT_PATH}")
        sys.exit(0)

    sorted_dates = sorted(all_dates)
    earliest, latest = sorted_dates[0], sorted_dates[-1]

    # Calendar span between earliest and latest (inclusive). This is the
    # "should-have" denominator — if backfill is complete, every source has a
    # snapshot for every calendar day in this span.
    from datetime import date as _date
    def _parse(d):
        return _date.fromisoformat(d)
    span_days = (_parse(latest) - _parse(earliest)).days + 1

    # Group by category, with per-source stats
    by_category = defaultdict(list)
    for fname in sorted(file_dates.keys()):
        cat = categorise(fname)
        sdates = sorted(file_dates[fname])
        s_earliest, s_latest = sdates[0], sdates[-1]
        s_span = (_parse(s_latest) - _parse(s_earliest)).days + 1
        present = {d: True for d in file_dates[fname]}
        by_category[cat].append({
            "file": fname,
            "present": present,
            "days_stored": len(sdates),         # how many days we actually have
            "earliest": s_earliest,             # oldest date this source goes back to
            "latest": s_latest,                 # newest date
            "span_days": s_span,                # calendar range of this source
            "completeness_pct": round(100.0 * len(sdates) / s_span, 1) if s_span else 0.0,
            "bytes": file_bytes[fname],
        })

    # Ordered output — preserve CATEGORIES order, then Other last
    ordered_categories = {}
    for cat in CATEGORIES.keys():
        if cat in by_category:
            ordered_categories[cat] = by_category[cat]
    for cat in by_category:
        if cat not in ordered_categories:
            ordered_categories[cat] = by_category[cat]

    cells_present = sum(len(s["present"]) for sources in ordered_categories.values() for s in sources)
    # "Records stored %" = actual cells / (sources × full calendar span).
    # This is the real backfill-completeness number the founder asked for.
    total_possible_cells = len(file_dates) * span_days
    coverage_pct = round(100.0 * cells_present / total_possible_cells, 1) if total_possible_cells else 0.0

    payload = {
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "bucket": bucket,
        "earliest_date": earliest,
        "latest_date": latest,
        "span_days": span_days,
        "dates": sorted_dates,
        "categories": ordered_categories,
        "totals": {
            "sources": len(file_dates),
            "dates": len(all_dates),               # distinct days we have ANY data
            "span_days": span_days,                # calendar range earliest→latest
            "cells_present": cells_present,         # total source×date records stored
            "total_possible_cells": total_possible_cells,
            "coverage_pct": coverage_pct,          # % of the full grid that's filled
            "total_bytes": total_bytes,
            "total_mb": round(total_bytes / 1024 / 1024, 1),
        },
    }

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"[coverage] wrote {OUT_PATH} — {len(file_dates)} sources × {len(all_dates)} dates = {coverage_pct}% coverage")


if __name__ == "__main__":
    main()
