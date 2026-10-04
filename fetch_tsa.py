#!/usr/bin/env python3
"""
fetch_tsa.py  —  writes data-tsa.json for the Aviation tab's "TSA Throughput" sub-view.

WHY A FETCHER (not a browser call):
  tsa.gov serves the daily checkpoint table as server-rendered HTML and returns
  403 to non-browser User-Agents (and has no CORS header for a Pages origin), so a
  client-side fetch from GitHub Pages can't reach it. Pulling server-side here, in a
  GitHub Action, sidesteps both — matching the repo's existing "Python fetcher ->
  static JSON snapshot" pattern (see fetch_opensky.py).

SOURCES (tried in order):
  1. https://www.tsa.gov/travel/passenger-volumes
     A two-column table: Date | Numbers (passengers screened that day, current period).
  2. The Internet Archive's copy of that same page.
     Since mid-June 2026 www.tsa.gov's Akamai edge answers GitHub Actions runners
     (and other datacenter IPs) with a plain "403 Access Denied" whatever the
     User-Agent — an IP-reputation block, so the direct fetch fails every day and
     the feed went ~107 days stale. The Wayback Machine captures this page roughly
     daily, so we ask the availability API for the newest capture and read its raw
     original bytes (the `id_` form: no Wayback toolbar), which the same
     parse_rows() handles unchanged. The availability API sometimes answers 429 or
     an empty result from shared IPs; then we ask Wayback for the capture nearest
     to now, which it redirects to the newest one. We do not try to get past
     Akamai itself.

OUTPUT:
  data-tsa.json  — { generated, latest:{date,vol}, avg7, series:[{d,v}...], src,
                     source, as_of, snapshot_url, snapshot_timestamp }
  The client (renderAviationTab -> tsa()) reads this at runtime and falls back to the
  baked-in seed (DATA.aviation.tsa.seed) when the file is missing/empty.
  Provenance fields (added; nothing the client reads was renamed):
    source              "tsa.gov" or "web.archive.org" — where this table was read
    as_of               newest date IN THE TABLE (YYYY-MM-DD), never a clock reading,
                        so an old snapshot cannot pass itself off as fresh
    snapshot_url        the Wayback URL actually read (null when source is tsa.gov)
    snapshot_timestamp  when the Archive captured it, UTC (null when source is tsa.gov)

FAILURE CONTRACT:
  On any fetch/parse failure of BOTH sources we DO NOT overwrite an existing good
  data-tsa.json — we exit non-zero, leave the previous snapshot (or the seed) in
  place, and (in GitHub Actions) raise a `TSA not refreshed` warning annotation that
  names the cause. A snapshot whose table ends before the data already on disk never
  replaces it. Stdlib only.
"""
import os, sys, re, json, time, datetime, urllib.request, urllib.error

URL = "https://www.tsa.gov/travel/passenger-volumes"
OUT = "data-tsa.json"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
KEEP = 30  # most recent N days to publish for the chart

# --- Internet Archive fallback ------------------------------------------------
# Requests to archive.org carry a descriptive User-Agent with a contact URL.
ARCHIVE_UA = ("alpine-data-tsa-fetcher/1.0 "
              "(+https://github.com/btabiado/alpine-data)")
AVAILABILITY_URL = ("https://archive.org/wayback/available"
                    "?url=tsa.gov/travel/passenger-volumes")
SNAPSHOT_URL = "https://web.archive.org/web/{ts}id_/" + URL
ARCHIVE_TIMEOUT = 60       # seconds per request
ARCHIVE_RETRY_PAUSE = 20   # seconds before the single retry of a transient error

SRC_DIRECT = "TSA checkpoint travel numbers — tsa.gov/travel/passenger-volumes"
SRC_ARCHIVE = SRC_DIRECT + " (read from the Internet Archive's copy of that page)"


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


def _describe(e):
    return f"{type(e).__name__}: {e}"[:200]


def archive_get(url):
    """GET from archive.org -> (text, final_url after redirects).

    Identifies the project in the User-Agent. Retries ONCE, after a pause, and
    only on errors that are plausibly transient (5xx, connection reset,
    timeout); a 4xx — including 429 rate limiting — is raised straight away.
    """
    for attempt in (1, 2):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": ARCHIVE_UA})
            with urllib.request.urlopen(req, timeout=ARCHIVE_TIMEOUT) as r:
                return r.read().decode("utf-8", "replace"), r.geturl()
        except urllib.error.HTTPError as e:
            if attempt == 2 or e.code < 500:
                raise
        except OSError:  # URLError, ConnectionResetError, socket timeout
            if attempt == 2:
                raise
        time.sleep(ARCHIVE_RETRY_PAUSE)


def wayback_latest_timestamp():
    """Ask the Wayback availability API for the newest 200 capture of URL.

    Returns its 14-digit timestamp (YYYYMMDDhhmmss), or raises.
    """
    text, _ = archive_get(AVAILABILITY_URL)
    snaps = json.loads(text).get("archived_snapshots") or {}
    closest = snaps.get("closest") or {}
    ts = str(closest.get("timestamp") or "")
    if (closest.get("available") is not True
            or str(closest.get("status")) != "200"
            or not re.fullmatch(r"\d{14}", ts)):
        raise ValueError(f"availability API returned no usable capture: {closest!r}")
    return ts


def fetch_wayback():
    """Fetch the newest Wayback capture of URL -> (html, snapshot_url, timestamp).

    If the availability API itself is down or rate-limited, ask Wayback for the
    capture nearest to *now*: it redirects to the newest one, and the timestamp
    we record is taken from the URL we actually ended up reading.
    """
    try:
        ts = wayback_latest_timestamp()
    except Exception as e:
        print(f"fetch_tsa: Wayback availability API failed ({_describe(e)}) — "
              f"requesting the capture nearest to now instead", file=sys.stderr)
        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d%H%M%S")
    html, final_url = archive_get(SNAPSHOT_URL.format(ts=ts))
    m = re.search(r"/web/(\d{14})id_/", final_url)
    if not m:
        raise ValueError(f"unexpected snapshot URL after redirects: {final_url}")
    return html, final_url, m.group(1)


def _gh_escape(s):
    """Workflow-command escaping: % CR LF must be encoded or the annotation is
    truncated at the first newline (same rule as lthcs_daily._report_sec_errors)."""
    return s.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def warn_not_refreshed(cause):
    print(f"fetch_tsa: NOT refreshed — {cause} — leaving existing {OUT} untouched",
          file=sys.stderr)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print("::warning title=TSA not refreshed::" + _gh_escape(cause))


def collect():
    """Try tsa.gov, then the Internet Archive copy.

    Returns (rows, provenance) on success, or ([], causes) when both fail.
    """
    causes = []
    try:
        rows = parse_rows(fetch_html(URL))
        if not rows:
            raise ValueError("no rows parsed from the page")
        return rows, {"source": "tsa.gov", "src": SRC_DIRECT,
                      "snapshot_url": None, "snapshot_timestamp": None}
    except Exception as e:
        causes.append(f"tsa.gov: {_describe(e)}")
        print(f"fetch_tsa: direct fetch failed ({_describe(e)}) — "
              f"trying the Internet Archive copy", file=sys.stderr)
    try:
        html, snap_url, ts = fetch_wayback()
        rows = parse_rows(html)
        if not rows:
            raise ValueError(f"no rows parsed from snapshot {ts}")
        captured = (datetime.datetime.strptime(ts, "%Y%m%d%H%M%S")
                    .strftime("%Y-%m-%dT%H:%M:%SZ"))
        print(f"fetch_tsa: using Internet Archive snapshot captured {captured}")
        return rows, {"source": "web.archive.org", "src": SRC_ARCHIVE,
                      "snapshot_url": snap_url, "snapshot_timestamp": captured}
    except Exception as e:
        causes.append(f"web.archive.org: {_describe(e)}")
    return [], causes


def main():
    rows, info = collect()
    if not rows:
        warn_not_refreshed("; ".join(info))
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
        "src": info["src"],
        "source": info["source"],
        # Freshness comes from the data itself, not from when we ran.
        "as_of": date_key(latest_d).isoformat(),
        "snapshot_url": info["snapshot_url"],
        "snapshot_timestamp": info["snapshot_timestamp"],
    }

    try:
        with open(OUT) as f:
            prev = json.load(f)
        if not isinstance(prev, dict):
            prev = None
    except (FileNotFoundError, ValueError):
        prev = None  # no prior file / corrupt — fall through and write fresh

    # Never step backwards: an archive capture can lag the data already on disk.
    prev_latest = ((prev or {}).get("latest") or {}).get("date")
    if prev_latest and date_key(prev_latest) > date_key(latest_d):
        print(f"fetch_tsa: {info['source']} table ends {latest_d}, older than "
              f"{prev_latest} already in {OUT} — not overwriting")
        return 0

    # Skip the rewrite when nothing substantive changed (TSA posts on weekdays;
    # on a no-new-day run the table is identical). The `generated` timestamp is
    # not part of this comparison — rewriting it every run would defeat the
    # workflow's `git diff --quiet` skip-if-unchanged guard and churn a daily
    # signed commit. `generated` is only consumed by the client as a truthiness
    # flag, so preserving the prior value on a no-op run is fine. The snapshot
    # URL/timestamp are left out for the same reason: a newer capture of an
    # identical table is not new data.
    substantive = ("latest", "avg7", "series", "src", "source", "as_of")
    if prev and all(prev.get(k) == payload[k] for k in substantive):
        print(f"fetch_tsa: {OUT} unchanged (latest {latest_d}) — not rewriting")
        return 0

    with open(OUT, "w") as f:
        json.dump(payload, f, indent=1)
    print(f"fetch_tsa: wrote {OUT} from {info['source']} — latest {latest_d} = "
          f"{latest_v:,} (7-day avg {avg7:,}, {len(series)} days)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
