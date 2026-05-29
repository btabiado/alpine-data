#!/usr/bin/env python3
"""Build data-us_states.json — SVG path strings for the 50 states + DC.

Source: us-atlas@2 (npm) `us/10m.json`, which ships TopoJSON geometry that is
ALREADY projected (geoAlbersUsa) into a ~960x600 frame — Alaska and Hawaii are
positioned as the usual lower-left insets. Because it's pre-projected we only
need to decode the quantized/delta-encoded TopoJSON arcs into absolute (x, y)
points and emit one SVG <path> per state. No projection math required.

Output: <repo-root>/data-us_states.json  →  { "CA": "M..Z", "TX": "M..Z", ... }

This file is consumed by the V1 Real Estate tab's geographic heat map. It is a
root-level "data-*.json" sidecar so the pages.yml staging glob copies it into
_site/ automatically (no workflow edit). It's git-ignored by the blanket
`data-*.json` rule, so it's committed via a `!data-us_states.json` carve-out in
.gitignore (same pattern as data-travel.json).

Re-run only when the underlying boundaries change (essentially never):
    python scripts/build_us_state_paths.py

Pure stdlib — no third-party deps. Network access required for the one fetch.
"""
from __future__ import annotations

import json
import os
import ssl
import sys
import urllib.request

ATLAS_URL = "https://cdn.jsdelivr.net/npm/us-atlas@2/us/10m.json"

# FIPS state code -> USPS 2-letter abbreviation. 50 states + DC (id "11").
# Territories present in us-atlas (60/66/69/72/78) are intentionally omitted —
# the housing-heat data only covers states + DC.
FIPS_TO_USPS = {
    "01": "AL", "02": "AK", "04": "AZ", "05": "AR", "06": "CA", "08": "CO",
    "09": "CT", "10": "DE", "11": "DC", "12": "FL", "13": "GA", "15": "HI",
    "16": "ID", "17": "IL", "18": "IN", "19": "IA", "20": "KS", "21": "KY",
    "22": "LA", "23": "ME", "24": "MD", "25": "MA", "26": "MI", "27": "MN",
    "28": "MS", "29": "MO", "30": "MT", "31": "NE", "32": "NV", "33": "NH",
    "34": "NJ", "35": "NM", "36": "NY", "37": "NC", "38": "ND", "39": "OH",
    "40": "OK", "41": "OR", "42": "PA", "44": "RI", "45": "SC", "46": "SD",
    "47": "TN", "48": "TX", "49": "UT", "50": "VT", "51": "VA", "53": "WA",
    "54": "WV", "55": "WI", "56": "WY",
}


def _fetch(url: str) -> bytes:
    ctx = ssl.create_default_context()
    req = urllib.request.Request(url, headers={"User-Agent": "build_us_state_paths"})
    return urllib.request.urlopen(req, timeout=60, context=ctx).read()


def build() -> dict[str, str]:
    topo = json.loads(_fetch(ATLAS_URL))
    sx, sy = topo["transform"]["scale"]
    tx, ty = topo["transform"]["translate"]

    # Decode every arc once: TopoJSON arcs are delta-encoded quantized ints;
    # cumulative-sum then de-quantize into the pre-projected screen frame.
    def decode_arc(arc: list[list[int]]) -> list[tuple[float, float]]:
        x = y = 0
        pts: list[tuple[float, float]] = []
        for dx, dy in arc:
            x += dx
            y += dy
            pts.append((x * sx + tx, y * sy + ty))
        return pts

    arcs = [decode_arc(a) for a in topo["arcs"]]

    def arc_points(i: int) -> list[tuple[float, float]]:
        # Negative index => reversed arc at ~i (i.e. -i-1).
        return arcs[i] if i >= 0 else arcs[~i][::-1]

    def ring_points(ring: list[int]) -> list[tuple[float, float]]:
        pts: list[tuple[float, float]] = []
        for idx in ring:
            ap = arc_points(idx)
            if pts:  # drop the duplicated join point shared with the prior arc
                ap = ap[1:]
            pts.extend(ap)
        return pts

    def geom_to_path(geom: dict) -> str:
        polys = geom["arcs"] if geom["type"] == "MultiPolygon" else [geom["arcs"]]
        out: list[str] = []
        for poly in polys:
            for ring in poly:
                pts = ring_points(ring)
                if len(pts) < 2:
                    continue
                out.append("M" + " ".join(f"{x:.1f},{y:.1f}" for x, y in pts) + "Z")
        return "".join(out)

    paths: dict[str, str] = {}
    for geom in topo["objects"]["states"]["geometries"]:
        code = FIPS_TO_USPS.get(geom.get("id"))
        if code:
            paths[code] = geom_to_path(geom)
    return paths


def main() -> int:
    paths = build()
    if len(paths) != 51:
        print(f"ERROR: expected 51 states+DC, got {len(paths)}", file=sys.stderr)
        return 1
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out_path = os.path.join(root, "data-us_states.json")
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(paths, fh, separators=(",", ":"), sort_keys=True)
    size = os.path.getsize(out_path)
    print(f"Wrote {out_path} — {len(paths)} states, {size:,} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
