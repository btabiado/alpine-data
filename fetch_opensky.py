#!/usr/bin/env python3
"""
fetch_opensky.py  —  writes data-opensky.json for the Aviation tab.

WHY A FETCHER (not a browser call):
  OpenSky's REST API returns `access-control-allow-origin: https://opensky-network.org`,
  so a browser fetch from GitHub Pages is blocked by CORS. Pulling server-side (here,
  in a GitHub Action) avoids CORS, keeps you within the free/non-commercial terms, and
  matches the repo's existing "Python fetcher -> static JSON snapshot" pattern.

USAGE:
  python fetch_opensky.py                      # anonymous (~400 calls/day, lower res)
  OPENSKY_CLIENT_ID=... OPENSKY_CLIENT_SECRET=... python fetch_opensky.py   # OAuth2, higher limits

OUTPUT:
  data-opensky.json           (the Live Traffic tile reads this — summary/snapshot)
  data-opensky-positions.json (trimmed airborne positions for the Live Flight Map sub-view)

LICENSE NOTE: OpenSky data is free for research / non-commercial use only.

State-vector index reference (used by positions() and app.py renderer):
  s[0]  icao24        s[1]  callsign       s[2]  origin_country
  s[3]  time_position s[4]  last_contact   s[5]  longitude
  s[6]  latitude      s[7]  baro_altitude  s[8]  on_ground
  s[9]  velocity      s[10] true_track (heading)
  s[11] vertical_rate s[12] sensors        s[13] geo_altitude
  s[14] squawk        s[15] spi            s[16] position_source
"""
import os, json, time, urllib.request, urllib.parse
from collections import Counter

API = "https://opensky-network.org/api/states/all"
TOKEN_URL = "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"
OUT = os.path.join(os.path.dirname(__file__), "data-opensky.json")
OUT_POS = os.path.join(os.path.dirname(__file__), "data-opensky-positions.json")
UA = "alpine-data/aviation-tab (non-commercial)"
MAX_POINTS = 2000
STR_CAP = 24  # max chars for any external string in positions output


def get_token():
    cid, sec = os.getenv("OPENSKY_CLIENT_ID"), os.getenv("OPENSKY_CLIENT_SECRET")
    if not (cid and sec):
        return None
    data = urllib.parse.urlencode({
        "grant_type": "client_credentials", "client_id": cid, "client_secret": sec
    }).encode()
    req = urllib.request.Request(TOKEN_URL, data=data,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r).get("access_token")


def fetch():
    headers = {"User-Agent": UA}
    tok = get_token()
    if tok:
        headers["Authorization"] = "Bearer " + tok
    req = urllib.request.Request(API, headers=headers)
    with urllib.request.urlopen(req, timeout=45) as r:
        # [A1] Guard against malformed or oversized responses.
        # Read raw bytes with a hard cap (64 MB) so a runaway/corrupt
        # response doesn't OOM the runner before json.loads() even runs.
        MAX_BYTES = 64 * 1024 * 1024  # 64 MB; typical full-coverage payload ~10–20 MB
        raw = r.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError(
                f"opensky: response exceeds {MAX_BYTES // (1024*1024)} MB safety cap "
                f"({len(raw)} bytes read) — refusing to parse"
            )
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"opensky: malformed JSON in response — {exc}") from exc


def summarize(d):
    sv = [s for s in (d.get("states") or []) if s]
    if not sv:
        # Degraded/empty OpenSky pull (coverage gap or throttle): do NOT overwrite
        # the last good snapshot with an empty one. Failing here means the workflow
        # step exits non-zero (visibly red in Actions, like city-daily) and the prior
        # committed data-opensky.json is retained; the client also has a seed fallback.
        raise SystemExit("opensky: empty states payload; refusing to write an empty snapshot")
    air = [s for s in sv if len(s) > 8 and s[8] is False]
    ground = [s for s in sv if len(s) > 8 and s[8] is True]
    bands = {"0–10k ft": 0, "10–20k ft": 0, "20–30k ft": 0, "30–40k ft": 0, "40k+ ft": 0}
    for s in air:
        alt = s[7]
        if alt is None:
            continue
        ft = alt * 3.28084
        key = ("0–10k ft" if ft < 10000 else "10–20k ft" if ft < 20000 else
               "20–30k ft" if ft < 30000 else "30–40k ft" if ft < 40000 else "40k+ ft")
        bands[key] += 1
    ts = d.get("time") or int(time.time())
    return {
        "ts": ts,
        "tstr": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ts)),
        "tracked": len(sv),
        "airborne": len(air),
        "ground": len(ground),
        "byCountry": Counter(s[2].strip() for s in sv if s[2]).most_common(8),
        "byAlt": list(bands.items()),
        "note": ("OpenSky coverage is a sample of global traffic from volunteer ADS-B "
                 "receivers, densest over US/Europe."),
    }


def positions(d):
    """
    Build a trimmed list of airborne aircraft positions for the Live Flight Map.

    Each point is:  [lat, lon, alt_ft_or_null, heading_or_null, callsign, origin_country]

    State-vector indices used:
      s[5]  longitude       s[6]  latitude         s[7]  baro_altitude (metres)
      s[8]  on_ground       s[10] true_track       s[1]  callsign
      s[2]  origin_country

    Caps output at MAX_POINTS (2000) and sanitizes external strings to STR_CAP chars.
    """
    sv = [s for s in (d.get("states") or []) if s and len(s) > 10]
    ts = d.get("time") or int(time.time())
    tstr = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ts))

    points = []
    for s in sv:
        # Only airborne aircraft with valid lat/lon
        if s[8] is not False:
            continue
        # OpenSky state vectors order position as s[5]=longitude, s[6]=latitude
        # (longitude first — a well-known OpenSky gotcha). Unpack accordingly so
        # the emitted [lat, lon] rows are geographically correct on the map.
        lon, lat = s[5], s[6]
        if lat is None or lon is None:
            continue

        # Altitude: baro metres → feet, rounded to int; None if missing
        alt_m = s[7]
        alt_ft = int(alt_m * 3.28084) if alt_m is not None else None

        # True track / heading; None if missing
        heading = s[10]
        heading = round(heading) if heading is not None else None

        # Callsign: strip whitespace, cap to STR_CAP chars
        raw_cs = s[1]
        callsign = (raw_cs.strip()[:STR_CAP] if raw_cs else "")

        # Origin country: strip whitespace, cap to STR_CAP chars
        raw_oc = s[2]
        origin_country = (raw_oc.strip()[:STR_CAP] if raw_oc else "")

        points.append([round(lat, 2), round(lon, 2), alt_ft, heading, callsign, origin_country])

        if len(points) >= MAX_POINTS:
            break  # hard cap — already have MAX_POINTS points

    # True count of plottable airborne aircraft in this snapshot, BEFORE the
    # MAX_POINTS cap — so the map can show the real total alongside how many it
    # actually plotted (`count`). Same predicate as the append loop above.
    airborne_total = sum(
        1 for s in sv
        if s[8] is False and s[5] is not None and s[6] is not None
    )

    return {
        "ts": ts,
        "tstr": tstr,
        "count": len(points),       # how many points are plotted (≤ MAX_POINTS)
        "airborne": airborne_total,  # true plottable airborne total (pre-cap)
        "points": points,
    }


def main():
    d = fetch()
    snap = summarize(d)
    with open(OUT, "w") as f:
        json.dump(snap, f)
    print(f"wrote {OUT}: {snap['airborne']:,} airborne / {snap['tracked']:,} tracked @ {snap['tstr']}")

    pos = positions(d)
    with open(OUT_POS, "w") as f:
        json.dump(pos, f)
    print(f"wrote {OUT_POS}: {pos['count']:,} airborne positions @ {pos['tstr']}")


if __name__ == "__main__":
    main()
