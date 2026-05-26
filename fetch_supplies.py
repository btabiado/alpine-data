"""
Global Supplies fetcher.

Sources (all free, no auth required):
  portoflosangeles.org             Port of L.A. monthly TEU — historical
    /business/statistics/         per-year HTML pages (scraped). Replaces the
    /container-statistics/        old data.lacity.org Socrata feed (tsuv-4rgh)
    /historical-teu-statistics-   which the publisher stopped updating in
    {YEAR}                        Sep-2016. New source publishes the prior
                                   month in the second half of the following
                                   month, has stable per-year URLs back to
                                   1995, and reports loaded/empty breakdowns
                                   for both imports and exports.
  FRED            ISRATIO          Total business inventory-to-sales ratio
                                   (only fetched when FRED_API_KEY is set;
                                   otherwise returns available=false)
  NY Fed          gscpi CSV        Global Supply Chain Pressure Index — vintage
                                   matrix; we collapse to the latest published
                                   value per observation month.

Note on port coverage: we previously also pulled NY/NJ monthly TEU from
data.ny.gov (629s-5a55). That dataset stopped updating in Dec-2015 and PANYNJ
now only publishes monthly figures via JS-rendered press releases that are
not scrapeable from CI. BTS rd72-aq8r also halted at Oct-2022. With no
reliable current NY/NJ feed, we now show two L.A. series: total monthly TEU
plus loaded-imports-only (the leading-indicator subset for US consumer
demand). L.A. alone moves ~10M TEU/yr — about a quarter of US container
traffic — and is the largest single port indicator we have.

Output: v2/data-supplies.json (sidecar for the V2 dashboard's "Supplies" tab).

Schema:
    {
      "generated_at": "2026-05-25T12:00:00Z",
      "port_teu": {
        "los_angeles": {
          "unit": "TEU",
          "observations": [
            {
              "month": "YYYY-MM",
              "total": N,
              "loaded_imports": N,
              "empty_imports": N,
              "total_imports": N,
              "loaded_exports": N,
              "empty_exports": N,
              "total_exports": N
            }, ...
          ],
          "source": "portoflosangeles.org historical-teu-statistics",
          "as_of": "YYYY-MM"
        }
      },
      "inventory_ratio": {
        "id": "ISRATIO",
        "label": "Total business inventory-to-sales ratio",
        "unit": "ratio",
        "observations": [{"date":"YYYY-MM-DD","value":1.36}, ...],
        "source": "FRED",
        "available": true|false
      },
      "gscpi": {
        "unit": "std deviations from mean",
        "observations": [{"date":"YYYY-MM-DD","value":0.45}, ...],
        "source": "NY Fed",
        "as_of": "YYYY-MM-DD"
      }
    }

Resilience: each source is wrapped in its own try/except. On failure we keep
the prior value for that key (read from the existing output file). The other
sources continue. Worst case the file is unchanged.

CLI:
    python fetch_supplies.py                  # default --out v2/data-supplies.json
    python fetch_supplies.py --out PATH       # custom output path
    python fetch_supplies.py --no-network     # offline parser self-test only
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests


# Port of L.A. blocks the generic library UA on Cloudflare; a desktop browser
# string sails through. Keep it specific enough to be honest about what we are
# (not pretending to be Chrome) while still passing their bot heuristics.
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0 Safari/537.36 "
    "etf-flow-dashboard/1.0"
)
H = {"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,application/json"}
ROOT = Path(__file__).parent
DEFAULT_OUT = ROOT / "v2" / "data-supplies.json"
# V1 dual-write target — the V1 dashboard lazy-loads /data-supplies.json via
# the same SIDECARS mechanism it already uses for whale/defi (those live at
# data-whale.json / data-defi.json next to dashboard.html). Writing here
# keeps V2's existing wiring untouched while giving V1 a self-contained
# sidecar that the CI stage step picks up automatically (it globs
# `data-*.json` at repo root). Pass --out-v1 '' to disable.
DEFAULT_OUT_V1 = ROOT / "data-supplies.json"

# Port of L.A. publishes one HTML page per calendar year with a monthly table.
# URL pattern is stable back to 1995; the current-year page is updated in the
# second half of the month following the observation month.
POLA_YEAR_URL = (
    "https://portoflosangeles.org/business/statistics/container-statistics/"
    "historical-teu-statistics-{year}"
)
# How many years back we attempt to fetch. We want enough history for the
# dashboard's 5-year window plus a safety buffer if the most recent calendar
# year hasn't published any month yet (we walk back until we get data).
POLA_YEARS_BACK = 8

GSCPI_CSV_URL = (
    "https://www.newyorkfed.org/medialibrary/research/interactives/"
    "data/gscpi/gscpi_interactive_data.csv"
)
FRED_OBS_URL = "https://api.stlouisfed.org/fred/series/observations"


# ----- helpers ---------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _get_json(url: str, params: dict | None = None, timeout: int = 25) -> Any:
    try:
        r = requests.get(url, headers=H, params=params, timeout=timeout)
        if r.status_code != 200:
            print(f"  [skip] {url} -> {r.status_code}", file=sys.stderr)
            return None
        return r.json()
    except Exception as e:
        print(f"  [skip] {url} -> {e}", file=sys.stderr)
        return None


def _get_text(url: str, timeout: int = 25, encoding: str | None = None) -> str | None:
    """GET and decode body. When ``encoding`` is given we override whatever
    `requests` guessed (it falls back to ISO-8859-1 when the server omits a
    charset, which mangles UTF-8 BOMs into ï»¿)."""
    try:
        r = requests.get(url, headers=H, timeout=timeout)
        if r.status_code != 200:
            print(f"  [skip] {url} -> {r.status_code}", file=sys.stderr)
            return None
        if encoding:
            r.encoding = encoding
        return r.text
    except Exception as e:
        print(f"  [skip] {url} -> {e}", file=sys.stderr)
        return None


def _to_float(x: Any) -> float | None:
    if x is None or x == "" or x == ".":
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _to_int(x: Any) -> int | None:
    f = _to_float(x)
    if f is None:
        return None
    return int(round(f))


# Month-name -> 2-digit month string. NY/NJ rows use written-out month names
# (January, February, ...) so we need this map. Tolerates 3-letter abbreviations
# too in case the dataset ever flips format on us.
_MONTH_MAP = {
    "january": "01", "february": "02", "march": "03", "april": "04",
    "may": "05", "june": "06", "july": "07", "august": "08",
    "september": "09", "october": "10", "november": "11", "december": "12",
    "jan": "01", "feb": "02", "mar": "03", "apr": "04", "jun": "06",
    "jul": "07", "aug": "08", "sep": "09", "sept": "09", "oct": "10",
    "nov": "11", "dec": "12",
}


def _ym_from_year_month(year: Any, month_name: Any) -> str | None:
    """Build 'YYYY-MM' from {year:'2015', month:'October'}-style cells."""
    if year is None or month_name is None:
        return None
    y = str(year).strip()
    m = _MONTH_MAP.get(str(month_name).strip().lower())
    if not (len(y) == 4 and y.isdigit() and m):
        return None
    return f"{y}-{m}"


# ----- Port of Los Angeles (HTML scrape per year) ---------------------------

# Strip HTML tags + collapse whitespace; used both for column-header detection
# and for cell-text extraction.
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _clean_cell(cell_html: str) -> str:
    """Reduce a <td>...</td> inner HTML to a plain trimmed string.

    Handles &nbsp;, &ensp;, line breaks, nested spans, and the trailing
    non-breaking spaces the publisher sometimes appends to values."""
    txt = _TAG_RE.sub("", cell_html)
    txt = txt.replace("&nbsp;", " ").replace("&ensp;", " ")
    txt = txt.replace(" ", " ").replace(" ", " ")
    return _WS_RE.sub(" ", txt).strip()


def _parse_teu_value(text: str) -> float | None:
    """Parse cells like '459,825.25', '(668.50)', '&nbsp;', '#N/A'.

    Returns None when the cell is blank or non-numeric. The publisher uses
    accounting-style parentheses for negatives in change columns, but the
    raw volume columns we care about are always non-negative; we accept
    parentheses regardless to keep the parser forgiving."""
    s = (text or "").strip()
    if not s or s in {"-", "—", "N/A", "#N/A", "in progress"}:
        return None
    neg = s.startswith("(") and s.endswith(")")
    if neg:
        s = s[1:-1]
    s = s.replace(",", "").replace(" ", "").strip()
    if not s:
        return None
    try:
        v = float(s)
    except (TypeError, ValueError):
        return None
    return -v if neg else v


def _extract_first_data_table(html: str) -> str | None:
    """Return the inner HTML of the first <table> that contains 'Loaded
    Imports' in a header cell. The per-year POLA page has two tables (the
    monthly breakdown and a multi-decade calendar-year summary). We want the
    first one. Returns None if no matching table is found."""
    for m in re.finditer(r"<table[^>]*>(.*?)</table>", html, re.S | re.I):
        body = m.group(1)
        # Cheap header sniff — the monthly table always has "Loaded Imports"
        # as its first numeric column header.
        if "Loaded Imports" in body or "Loaded&nbsp;Imports" in body:
            return body
    return None


# Mapping from cleaned header text -> field key in the observation dict.
# Header text is checked after _clean_cell, so &nbsp; collapses to space.
_POLA_HEADER_KEYS = {
    "loaded imports":   "loaded_imports",
    "empty imports":    "empty_imports",
    "total imports":    "total_imports",
    "loaded exports":   "loaded_exports",
    "empty exports":    "empty_exports",
    "total exports":    "total_exports",
    "total teus":       "total",
}


def parse_pola_year_table(html: str, year: int) -> list[dict]:
    """Parse a single per-year POLA stats page's monthly table.

    Returns observations sorted oldest-first. Each observation has at minimum
    a ``month`` key and a ``total``; the loaded/empty/exports breakdowns are
    included when present. Months still flagged 'in progress' (the
    publisher leaves blank/nbsp cells for future months in the current year)
    are skipped, not zeroed."""
    table = _extract_first_data_table(html)
    if not table:
        return []
    # Split into rows. The publisher's HTML wraps each row in <tr>...</tr>.
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", table, re.S | re.I)
    if len(rows) < 2:
        return []
    # First row is the header. Build a column-index -> field-key map.
    header_cells = re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", rows[0], re.S | re.I)
    header_texts = [_clean_cell(c).lower() for c in header_cells]
    # The first column is the month label (no header text other than a
    # spacer). Skip it. We map each remaining header to a known field key.
    col_keys: list[str | None] = [None]  # idx 0 = month label
    for h in header_texts[1:]:
        col_keys.append(_POLA_HEADER_KEYS.get(h))

    out: list[dict] = []
    # Subsequent rows: each starts with a month label like 'January'. Skip
    # totals rows that begin with 'Total ' or 'Total&nbsp;'.
    for raw_row in rows[1:]:
        cells = re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", raw_row, re.S | re.I)
        if not cells:
            continue
        label = _clean_cell(cells[0]).lower()
        # Skip aggregate rows: 'Total Calendar Year ...', 'Total Fiscal ...'.
        if label.startswith("total ") or label.startswith("total "):
            continue
        mm = _MONTH_MAP.get(label) or _MONTH_MAP.get(label[:3])
        if not mm:
            continue
        ym = f"{year:04d}-{mm}"
        rec: dict[str, Any] = {"month": ym}
        for idx, cell in enumerate(cells):
            if idx == 0:
                continue
            if idx >= len(col_keys):
                break
            key = col_keys[idx]
            if not key:
                continue
            v = _parse_teu_value(_clean_cell(cell))
            if v is None:
                continue
            # Round to whole TEU — the publisher's sub-unit precision (the
            # .25 / .15 fragments from partial-day reporting) is noise for a
            # macro indicator.
            rec[key] = int(round(v))
        # Skip rows where we got no data at all (future months in current
        # calendar year render as blank/&nbsp; cells).
        if "total" not in rec and "loaded_imports" not in rec:
            continue
        out.append(rec)
    out.sort(key=lambda o: o["month"])
    return out


def fetch_la_port() -> dict | None:
    """Walk back through per-year POLA stats pages and assemble the full
    monthly observation series. Returns None only if every year fetch fails.

    We start from the current calendar year and walk backward until we have
    at least one observation. We always fetch ``POLA_YEARS_BACK`` years to
    keep the dashboard's 5-year chart well-populated even early in a calendar
    year. Individual year failures are non-fatal — we log and continue, so a
    transient 5xx on one year doesn't blow away the whole series."""
    now_year = datetime.now(timezone.utc).year
    all_obs: list[dict] = []
    fetched_any = False
    fetch_failures: list[int] = []
    for back in range(POLA_YEARS_BACK + 1):
        year = now_year - back
        url = POLA_YEAR_URL.format(year=year)
        text = _get_text(url, timeout=25)
        if not text:
            # 404 is expected for the current year before the page lives, or
            # for very old years. Log and continue; only abort if EVERY
            # fetch fails.
            fetch_failures.append(year)
            continue
        try:
            rows = parse_pola_year_table(text, year)
        except Exception as e:
            print(f"    [la_port] parse {year} failed: {e}", file=sys.stderr)
            rows = []
        if rows:
            fetched_any = True
            all_obs.extend(rows)
        elif back == 0:
            # Current-year page exists but had no rows yet (very early in
            # the year, or table not yet populated). Not a hard failure.
            pass
    if not fetched_any:
        return None
    # Dedupe by month — if a year page somehow returns overlapping rows the
    # latest one (last in list) wins. all_obs is built newest-first because
    # we iterate from this year backward, then each year's table is
    # internally oldest-first; for dedupe we sort by month and keep last.
    by_month: dict[str, dict] = {}
    for o in sorted(all_obs, key=lambda r: r["month"]):
        by_month[o["month"]] = o
    obs = list(by_month.values())
    obs.sort(key=lambda o: o["month"])
    if not obs:
        return None
    return {
        "unit": "TEU",
        "observations": obs,
        "source": "portoflosangeles.org historical-teu-statistics",
        "as_of": obs[-1]["month"],
        "note": (
            "Monthly TEU scraped from per-year POLA stats pages "
            "(replaces data.lacity.org tsuv-4rgh, which stopped Sep-2016)."
        ),
    }


# ----- Inventory/Sales ratio (FRED) -----------------------------------------

def fetch_inventory_ratio() -> dict:
    """Pull FRED series ISRATIO. Returns a dict with `available: false` when
    the FRED_API_KEY env var isn't set — the dashboard renders an empty state
    explaining the key is required. We DO NOT consider that an error."""
    api_key = os.environ.get("FRED_API_KEY", "").strip()
    payload = {
        "id": "ISRATIO",
        "label": "Total business inventory-to-sales ratio",
        "unit": "ratio",
        "source": "FRED",
    }
    if not api_key:
        payload["available"] = False
        payload["observations"] = []
        payload["reason"] = "FRED_API_KEY not set"
        return payload

    j = _get_json(FRED_OBS_URL, {
        "series_id": "ISRATIO",
        "api_key": api_key,
        "file_type": "json",
    })
    obs: list[dict] = []
    if j and isinstance(j, dict):
        for o in (j.get("observations") or []):
            d = o.get("date")
            v = _to_float(o.get("value"))
            if not d or v is None:
                continue
            obs.append({"date": d, "value": v})
    payload["available"] = bool(obs)
    payload["observations"] = obs
    if obs:
        payload["as_of"] = obs[-1]["date"]
    return payload


# ----- NY Fed GSCPI (CSV vintage matrix) ------------------------------------

def parse_gscpi_csv(text: str) -> list[dict]:
    """Reduce the GSCPI vintage matrix CSV to a flat monthly time series.

    The CSV layout (verified 2026-05-25):
        Date            | Jan-22 | Feb-22 | ... | May-26
        30-Sep-1997     | -0.49  | -0.43  | ... | -0.48
        ...
        30-Apr-2026     | #N/A   | #N/A   | ... | 1.82

    Each row's "Date" is the observation month; columns are vintages
    (publication months). For each row we pick the right-most non-#N/A value,
    which is the most recently published estimate for that observation month
    — the headline series. Returns observations sorted oldest-first.
    """
    out: list[dict] = []
    if not text:
        return out
    # BOM-tolerant DictReader
    if text.startswith("﻿"):
        text = text.lstrip("﻿")
    reader = csv.DictReader(io.StringIO(text))
    fieldnames = reader.fieldnames or []
    # Vintage columns are everything except the first (Date).
    vintages = [c for c in fieldnames if c and c.lower() != "date"]
    if not vintages:
        return out
    for row in reader:
        date_raw = (row.get("Date") or row.get("date") or "").strip()
        if not date_raw:
            continue
        # Parse '30-Sep-1997' -> '1997-09-30'.
        date_iso = ""
        for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%m/%d/%Y"):
            try:
                date_iso = datetime.strptime(date_raw, fmt).strftime("%Y-%m-%d")
                break
            except ValueError:
                continue
        if not date_iso:
            continue
        latest_val: float | None = None
        for col in vintages:
            cell = (row.get(col) or "").strip()
            if not cell or cell == "#N/A" or cell.upper() == "NA":
                continue
            v = _to_float(cell)
            if v is None:
                continue
            latest_val = v  # iterate left-to-right; final assignment = right-most
        if latest_val is None:
            continue
        out.append({"date": date_iso, "value": latest_val})
    out.sort(key=lambda o: o["date"])
    return out


def fetch_gscpi() -> dict | None:
    # NY Fed serves the CSV with no charset header so requests guesses
    # ISO-8859-1 — force UTF-8 so the leading BOM strips cleanly.
    text = _get_text(GSCPI_CSV_URL, encoding="utf-8-sig")
    if not text:
        return None
    obs = parse_gscpi_csv(text)
    if not obs:
        return None
    return {
        "unit": "std deviations from mean",
        "observations": obs,
        "source": "NY Fed",
        "as_of": obs[-1]["date"],
    }


# ----- orchestration --------------------------------------------------------

def fetch_all(prior: dict | None = None) -> dict:
    """Run all four fetchers, falling back to ``prior`` for any that fail."""
    prior = prior or {}

    print("  Supplies: Port of LA TEU...")
    try:
        la = fetch_la_port()
    except Exception as e:
        print(f"    [la_port] {e}", file=sys.stderr)
        la = None
    if la is None and prior.get("port_teu", {}).get("los_angeles"):
        la = prior["port_teu"]["los_angeles"]
        la["_stale"] = True
        print("    -> using prior value (stale)")
    elif la:
        print(f"    -> {len(la['observations'])} months, latest {la['as_of']}")

    print("  Supplies: FRED ISRATIO...")
    try:
        inv = fetch_inventory_ratio()
    except Exception as e:
        print(f"    [inv] {e}", file=sys.stderr)
        inv = None
    if inv is None and prior.get("inventory_ratio"):
        inv = prior["inventory_ratio"]
        inv["_stale"] = True
        print("    -> using prior value (stale)")
    elif inv:
        status = "live" if inv.get("available") else "empty (no key)"
        print(f"    -> {status}, {len(inv.get('observations') or [])} obs")

    print("  Supplies: NY Fed GSCPI...")
    try:
        gscpi = fetch_gscpi()
    except Exception as e:
        print(f"    [gscpi] {e}", file=sys.stderr)
        gscpi = None
    if gscpi is None and prior.get("gscpi"):
        gscpi = prior["gscpi"]
        gscpi["_stale"] = True
        print("    -> using prior value (stale)")
    elif gscpi:
        print(f"    -> {len(gscpi['observations'])} months, latest {gscpi['as_of']}")

    payload: dict = {
        "generated_at": _now_iso(),
        "port_teu": {},
    }
    if la:
        payload["port_teu"]["los_angeles"] = la
    if inv:
        payload["inventory_ratio"] = inv
    if gscpi:
        payload["gscpi"] = gscpi
    return payload


# ----- offline self-test ----------------------------------------------------

_SAMPLE_GSCPI = (
    "﻿Date,Jan-22,Feb-22,Mar-22\n"
    "30-Sep-1997,-0.49,-0.43,-0.40\n"
    "31-Oct-1997,#N/A,-0.24,-0.23\n"
    "30-Apr-2026,#N/A,#N/A,1.82\n"
)


# Fixture mirrors the POLA per-year monthly table shape (verified 2026-05-26):
# header row + 12 monthly rows (some empty for future months in current year)
# + total rows we expect to skip. Whitespace and &nbsp; are intentional —
# they match what the publisher actually emits.
_SAMPLE_POLA_HTML = """
<html><body>
<table>
<tr><td>&nbsp;</td><td>Loaded Imports</td><td>Empty Imports</td><td>Total Imports</td>
    <td>Loaded Exports</td><td>Empty Exports</td><td>Total Exports</td>
    <td>Total&nbsp;TEUs</td><td>Prior Year Change</td></tr>
<tr><td>January</td><td>421,593.75</td><td>115.00</td><td>421,708.75</td>
    <td>104,297.00</td><td>285,994.50</td><td>390,291.50</td>
    <td>812,000.25</td><td>-12.14%</td></tr>
<tr><td>February</td><td>433,812.25</td><td>17.25</td><td>433,829.50</td>
    <td>116,633.25</td><td>273,860.50</td><td>390,493.75</td>
    <td>824,323.25</td><td>2.86%</td></tr>
<tr><td>March</td><td>380,732.50</td><td>60.00</td><td>380,792.50</td>
    <td>132,129.00</td><td>239,598.00</td><td>371,727.00</td>
    <td>752,519.50</td><td>-3.33%</td></tr>
<tr><td>May</td><td>&nbsp;</td><td>&nbsp;</td><td>&nbsp;</td>
    <td>&nbsp;</td><td>&nbsp;</td><td>&nbsp;</td>
    <td>&nbsp;</td><td>&nbsp;</td></tr>
<tr><td>Total&nbsp;Calendar Year 2026</td><td>&nbsp;</td><td>1,236,138.50</td>
    <td>192.25</td><td>1,236,330.75</td><td>353,059.25</td>
    <td>799,453.00</td><td>1,152,512.25</td><td>2,388,843.00</td></tr>
</table>
<table><tr><td>some other unrelated table</td></tr></table>
</body></html>
"""


def _self_test() -> int:
    obs = parse_gscpi_csv(_SAMPLE_GSCPI)
    pola = parse_pola_year_table(_SAMPLE_POLA_HTML, 2026)
    pola_jan = next((r for r in pola if r["month"] == "2026-01"), {})
    pola_mar = next((r for r in pola if r["month"] == "2026-03"), {})
    checks = [
        (len(obs) == 3, f"expected 3 GSCPI rows, got {len(obs)}"),
        (obs[0]["date"] == "1997-09-30", f"row[0].date={obs[0].get('date')!r}"),
        (obs[0]["value"] == -0.40, f"row[0].value={obs[0].get('value')!r}"),
        (obs[1]["value"] == -0.23, f"row[1].value={obs[1].get('value')!r}"),
        (obs[2]["value"] == 1.82, f"row[2].value={obs[2].get('value')!r}"),
        (_ym_from_year_month("2015", "October") == "2015-10",
         "year+month -> YYYY-MM"),
        (_ym_from_year_month("2024", "Feb") == "2024-02",
         "abbreviated month name"),
        (_ym_from_year_month("bogus", "October") is None, "bad year rejected"),
        (_to_int("123.5") == 124, "_to_int rounds"),
        (_to_float(".") is None, "FRED missing-value marker handled"),
        # POLA HTML parser
        (len(pola) == 3, f"POLA expected 3 monthly rows, got {len(pola)}: {[r['month'] for r in pola]}"),
        (pola_jan.get("month") == "2026-01", f"POLA Jan month={pola_jan.get('month')!r}"),
        (pola_jan.get("total") == 812000, f"POLA Jan total={pola_jan.get('total')!r}"),
        (pola_jan.get("loaded_imports") == 421594,
         f"POLA Jan loaded_imports={pola_jan.get('loaded_imports')!r}"),
        (pola_jan.get("loaded_exports") == 104297,
         f"POLA Jan loaded_exports={pola_jan.get('loaded_exports')!r}"),
        (pola_jan.get("empty_imports") == 115,
         f"POLA Jan empty_imports={pola_jan.get('empty_imports')!r}"),
        (pola_mar.get("total") == 752520,
         f"POLA Mar total={pola_mar.get('total')!r}"),
        (_parse_teu_value("459,825.25") == 459825.25, "comma+decimal parsed"),
        (_parse_teu_value("(668.50)") == -668.50, "parentheses negative"),
        (_parse_teu_value("&nbsp;") is None, "nbsp returns None"),
        (_parse_teu_value("#N/A") is None, "N/A returns None"),
    ]
    failed = [m for ok, m in checks if not ok]
    if failed:
        for f in failed:
            print(f"  [self-test FAIL] {f}", file=sys.stderr)
        return 1
    print(f"  [self-test OK] {len(obs)} GSCPI rows + {len(pola)} POLA rows; "
          f"all assertions passed.")
    return 0


# ----- CLI ------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Fetch Global Supplies indicators.")
    ap.add_argument("--out", default=str(DEFAULT_OUT),
                    help=f"Output JSON path (default: {DEFAULT_OUT})")
    ap.add_argument("--out-v1", default=str(DEFAULT_OUT_V1),
                    help=f"V1 dashboard dual-write path (default: {DEFAULT_OUT_V1}; "
                         f"pass empty string '' to disable)")
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

    payload = fetch_all(prior=prior)

    # Refuse to overwrite a non-empty prior with a completely empty payload.
    if not payload.get("port_teu") and not payload.get("inventory_ratio") \
            and not payload.get("gscpi"):
        if prior:
            print("  [supplies] every source failed; preserving prior file.",
                  file=sys.stderr)
            return 1

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2))
    print(f"  Wrote {out_path} "
          f"({out_path.stat().st_size:,} bytes)")

    # V1 dual-write — non-fatal on failure. V1 reads /data/supplies.json
    # lazily; if the mirror fails the tab just shows its empty state until
    # the next run.
    v1_out_arg = (args.out_v1 or "").strip()
    if v1_out_arg:
        v1_out_path = Path(v1_out_arg)
        try:
            v1_out_path.parent.mkdir(parents=True, exist_ok=True)
            v1_out_path.write_text(json.dumps(payload, indent=2))
            print(f"  [supplies] mirrored payload to {v1_out_path}")
        except Exception as e:
            print(f"  [supplies] v1 dual-write failed ({v1_out_path}): {e}",
                  file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
