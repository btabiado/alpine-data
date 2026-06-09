#!/usr/bin/env python3
"""
fetch_tsa.py  —  writes data-tsa.json for the Aviation tab's "TSA Throughput" sub-view.

WHY A FETCHER (not a browser call):
  tsa.gov serves the daily checkpoint table as server-rendered HTML and returns
  403 to non-browser User-Agents (and has no CORS header for a Pages origin), so a
  client-side fetch from GitHub Pages can't reach it. Pulling server-side here, in a
  GitHub Action, sidesteps both — matching the repo's existing "Python fetcher ->
  static JSON snapshot" pattern (see fetch_opensky.py).

SOURCE:
  https://www.tsa.gov/travel/passenger-volumes
  A two-column table: Date | Numbers (passengers screened that day, current period).

OUTPUT:
  data-tsa.json  — { generated, latest:{date,vol}, avg7, series:[{d,v}...], src }
  The client (renderAviationTab -> tsa()) reads this at runtime and falls back to the
  baked-in seed (DATA.aviation.tsa.seed) when the file is missing/empty.

FAILURE CONTRACT:
  On any fetch/parse failure we DO NOT overwrite an existing good data-tsa.json — we
  exit non-zero and leave the previous snapshot (or the seed) in place. Stdlib only.
"""
import os, sys, re, json, datetime, urllib.request

URL = "https://www.tsa.gov/travel/passenger-volumes"
OUT = "data-tsa.json"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
KEEP = 30  # most recent N days to publish for the chart


def fetch_html(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=45) as r:
        return r.read().decode("utf-8", "replace")


def parse_rows(html):
    """Return [(date_str, volume_int), ...] newest-first, as published."""
    # Isolate the first <table>...</table> so stray numbers elsewhere can't match.
    m = re.search(r"<table[^>]*>(.*?)</table>", html, re.S | re.I)
    block = m.group(1) if m else html
    rows = []
    # Each data row: <td ...>M/D/YYYY</td> <td ...>1,234,567</td>
    pat = re.compile(
        r"<td[^>]*>\s*(\d{1,2}/\d{1,2}/\d{4})\s*</td>\s*"
        r"<td[^>]*>\s*([\d,]+)\s*</td>", re.S | re.I)
    for d, v in pat.findall(block):
        try:
            n = int(v.replace(",", ""))
        except ValueError:
            continue
        if n > 0:
            rows.append((d, n))
    return rows


def date_key(d):
    """M/D/YYYY -> sortable date for ordering; tolerant of bad input."""
    try:
        mo, da, yr = (int(x) for x in d.split("/"))
        return datetime.date(yr, mo, da)
    except Exception:
        return datetime.date(1900, 1, 1)


def main():
    try:
        html = fetch_html(URL)
        rows = parse_rows(html)
    except Exception as e:
        print(f"fetch_tsa: fetch/parse failed ({type(e).__name__}: {e}) — "
              f"leaving existing {OUT} untouched", file=sys.stderr)
        return 1

    if not rows:
        print("fetch_tsa: no rows parsed — leaving existing file untouched",
              file=sys.stderr)
        return 1

    # Sort chronologically (oldest first) and de-dupe by date (keep first seen).
    seen, ordered = set(), []
    for d, n in sorted(rows, key=lambda r: date_key(r[0])):
        if d in seen:
            continue
        seen.add(d)
        ordered.append((d, n))

    series = ordered[-KEEP:]
    last7 = [n for _, n in ordered[-7:]]
    avg7 = round(sum(last7) / len(last7)) if last7 else 0
    latest_d, latest_v = ordered[-1]

    payload = {
        "generated": datetime.datetime.now(datetime.timezone.utc)
                     .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "latest": {"date": latest_d, "vol": latest_v},
        "avg7": avg7,
        "series": [{"d": d, "v": n} for d, n in series],
        "src": "TSA checkpoint travel numbers — tsa.gov/travel/passenger-volumes",
    }

    # Skip the rewrite when nothing substantive changed (TSA posts on weekdays;
    # on a no-new-day run the table is identical). The `generated` timestamp is
    # not part of this comparison — rewriting it every run would defeat the
    # workflow's `git diff --quiet` skip-if-unchanged guard and churn a daily
    # signed commit. `generated` is only consumed by the client as a truthiness
    # flag, so preserving the prior value on a no-op run is fine.
    substantive = ("latest", "avg7", "series", "src")
    try:
        with open(OUT) as f:
            prev = json.load(f)
        if all(prev.get(k) == payload[k] for k in substantive):
            print(f"fetch_tsa: {OUT} unchanged (latest {latest_d}) — not rewriting")
            return 0
    except (FileNotFoundError, ValueError):
        pass  # no prior file / corrupt — fall through and write fresh

    with open(OUT, "w") as f:
        json.dump(payload, f, indent=1)
    print(f"fetch_tsa: wrote {OUT} — latest {latest_d} = {latest_v:,} "
          f"(7-day avg {avg7:,}, {len(series)} days)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
