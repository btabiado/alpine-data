"""
Global Supplies fetcher.

Sources (all free, no auth required):
  data.lacity.org tsuv-4rgh.json   Port of Los Angeles monthly TEU
  data.ny.gov     629s-5a55.json   Port of NY/NJ monthly TEU (imports/exports)
  FRED            ISRATIO          Total business inventory-to-sales ratio
                                   (only fetched when FRED_API_KEY is set;
                                   otherwise returns available=false)
  NY Fed          gscpi CSV        Global Supply Chain Pressure Index — vintage
                                   matrix; we collapse to the latest published
                                   value per observation month.

Output: v2/data-supplies.json (sidecar for the V2 dashboard's "Supplies" tab).

Schema:
    {
      "generated_at": "2026-05-25T12:00:00Z",
      "port_teu": {
        "los_angeles": {
          "unit": "TEU",
          "observations": [{"month":"YYYY-MM","total":1234567}, ...],
          "source": "data.lacity.org tsuv-4rgh",
          "as_of": "YYYY-MM"
        },
        "ny_nj": {
          "unit": "TEU",
          "observations": [
            {"month":"YYYY-MM","loaded_imports":N,"loaded_exports":N,"total":N},
            ...
          ],
          "source": "data.ny.gov 629s-5a55",
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
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests


UA = "Mozilla/5.0 (compatible; etf-flow-dashboard/1.0)"
H = {"User-Agent": UA}
ROOT = Path(__file__).parent
DEFAULT_OUT = ROOT / "v2" / "data-supplies.json"
# V1 dual-write target — the V1 dashboard lazy-loads /data-supplies.json via
# the same SIDECARS mechanism it already uses for whale/defi (those live at
# data-whale.json / data-defi.json next to dashboard.html). Writing here
# keeps V2's existing wiring untouched while giving V1 a self-contained
# sidecar that the CI stage step picks up automatically (it globs
# `data-*.json` at repo root). Pass --out-v1 '' to disable.
DEFAULT_OUT_V1 = ROOT / "data-supplies.json"

LA_PORT_URL = "https://data.lacity.org/resource/tsuv-4rgh.json"
NYNJ_PORT_URL = "https://data.ny.gov/resource/629s-5a55.json"
GSCPI_CSV_URL = (
    "https://www.newyorkfed.org/medialibrary/research/interactives/"
    "data/gscpi/gscpi_interactive_data.csv"
)
FRED_OBS_URL = "https://api.stlouisfed.org/fred/series/observations"

# 50 years of monthly data is the cap on the Socrata calls. Both datasets are
# capped well below that; this just guarantees we never get truncated by the
# Socrata default of 1,000 rows.
SOCRATA_LIMIT = 600


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


# ----- Port of Los Angeles (Socrata) ----------------------------------------

def fetch_la_port() -> dict | None:
    """Pull monthly TEU rows from the Port of LA Socrata dataset.

    Schema returned by the live endpoint (verified 2026-05-25):
        { "month_year": "Sep-16",
          "date": "2016-09-30T00:00:00.000",
          "monthly_total_teus": "747707.4" }

    The dataset historically also exposed loaded/empty breakdowns, but as of
    2026 only ``monthly_total_teus`` is present. We surface that as ``total``
    and leave the breakdown fields absent — the renderer treats them as
    optional. Source dataset is stale (last reading 2016-09 as of build time);
    we still ship it because (a) the spec called for it, and (b) it provides
    real long-tail history the dashboard can chart.
    """
    rows = _get_json(LA_PORT_URL, {"$limit": SOCRATA_LIMIT, "$order": "date DESC"})
    if not isinstance(rows, list) or not rows:
        return None
    obs: list[dict] = []
    for r in rows:
        date_raw = r.get("date") or ""
        # 'YYYY-MM-DDT...' -> 'YYYY-MM'
        ym = date_raw[:7] if len(date_raw) >= 7 else None
        if not ym:
            # Fall back to parsing 'Sep-16' style month_year if needed.
            my = (r.get("month_year") or "").strip()
            if "-" in my:
                mon3, yy = my.split("-", 1)
                m = _MONTH_MAP.get(mon3.lower())
                if m and yy.isdigit():
                    # 2-digit year heuristic: 00-30 -> 2000s, 31-99 -> 1900s
                    yi = int(yy)
                    full = (2000 + yi) if yi <= 30 else (1900 + yi)
                    ym = f"{full:04d}-{m}"
        if not ym:
            continue
        total = _to_int(r.get("monthly_total_teus"))
        if total is None:
            continue
        obs.append({"month": ym, "total": total})
    if not obs:
        return None
    # Sort oldest-first so the chart consumer can iterate linearly.
    obs.sort(key=lambda o: o["month"])
    return {
        "unit": "TEU",
        "observations": obs,
        "source": "data.lacity.org tsuv-4rgh",
        "as_of": obs[-1]["month"],
    }


# ----- Port of NY/NJ (Socrata) -----------------------------------------------

def fetch_nynj_port() -> dict | None:
    """Pull NY/NJ monthly TEU. Rows are split by type (Imports vs Exports)
    with one row per (year, month, type); we pivot them into a single
    observation per month with loaded_imports + loaded_exports + total.

    Live schema (verified 2026-05-25):
        { "year": "2015", "month": "October", "type": "Imports",
          "volume": "269674" }

    Like the LA dataset, this one is stale (last data 2015-12). We surface
    what's available rather than dropping the entire panel; the dashboard
    notes the as_of date and the source so it's clear the cadence stopped.
    """
    rows = _get_json(NYNJ_PORT_URL, {"$limit": SOCRATA_LIMIT})
    if not isinstance(rows, list) or not rows:
        return None
    pivot: dict[str, dict[str, int]] = {}
    for r in rows:
        ym = _ym_from_year_month(r.get("year"), r.get("month"))
        if not ym:
            continue
        vol = _to_int(r.get("volume"))
        if vol is None:
            continue
        typ = (r.get("type") or "").strip().lower()
        slot = pivot.setdefault(ym, {})
        if typ == "imports":
            slot["loaded_imports"] = vol
        elif typ == "exports":
            slot["loaded_exports"] = vol
        else:
            # Unknown type — stash under a passthrough key so we don't lose it.
            slot[typ or "other"] = vol
    if not pivot:
        return None
    obs: list[dict] = []
    for ym in sorted(pivot.keys()):
        row = {"month": ym}
        row.update(pivot[ym])
        imp = pivot[ym].get("loaded_imports") or 0
        exp = pivot[ym].get("loaded_exports") or 0
        row["total"] = imp + exp
        obs.append(row)
    return {
        "unit": "TEU",
        "observations": obs,
        "source": "data.ny.gov 629s-5a55",
        "as_of": obs[-1]["month"],
        "note": "imports + exports only (loaded containers)",
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

    print("  Supplies: Port of NY/NJ TEU...")
    try:
        nynj = fetch_nynj_port()
    except Exception as e:
        print(f"    [nynj_port] {e}", file=sys.stderr)
        nynj = None
    if nynj is None and prior.get("port_teu", {}).get("ny_nj"):
        nynj = prior["port_teu"]["ny_nj"]
        nynj["_stale"] = True
        print("    -> using prior value (stale)")
    elif nynj:
        print(f"    -> {len(nynj['observations'])} months, latest {nynj['as_of']}")

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
    if nynj:
        payload["port_teu"]["ny_nj"] = nynj
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


def _self_test() -> int:
    obs = parse_gscpi_csv(_SAMPLE_GSCPI)
    checks = [
        (len(obs) == 3, f"expected 3 rows, got {len(obs)}"),
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
    ]
    failed = [m for ok, m in checks if not ok]
    if failed:
        for f in failed:
            print(f"  [self-test FAIL] {f}", file=sys.stderr)
        return 1
    print(f"  [self-test OK] {len(obs)} GSCPI rows; all assertions passed.")
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
