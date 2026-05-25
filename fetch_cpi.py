"""
FRED Consumer Price Index fetcher.

Sources (all free, requires self-service FRED API key):
  CPIAUCSL         CPI-U All Items (headline, 1982-84=100, monthly, since 1947)
  APU000074714     Retail gasoline ($/gallon, monthly, since 1976)
  APU0000708111    Retail eggs Grade A large ($/dozen, monthly, since 1980)
  CUSR0000SAF11    CPI Food at Home subindex (monthly, since 1952)

Output: v2/data-cpi.json (sidecar for the V2 dashboard's Consumer Price Index tab).

Schema (matches what the front-end consumes):
    {
      "generated_at": "2026-05-25T12:00:00Z",
      "fred_available": true,
      "series": [
        {"id": "CPIAUCSL",
         "label": "CPI-U All Items",
         "unit": "Index 1982-84=100",
         "kind": "index",
         "observations": [{"date": "1947-01-01", "value": 21.48}, ...]},
        ...
      ]
    }

When FRED_API_KEY is not set, writes a clean "unavailable" payload (exit 0)
so the front-end can render an empty-state explainer. Once the key lands the
next run overwrites with real data; the tab automatically activates.

Resilience:
  * Per-series try/except — one series failing does NOT block the others.
    Failed series ship with {"id": ..., "error": "..."} and zero observations.
  * Stale-fallback: if EVERY series fetch errors out AND a prior good
    v2/data-cpi.json exists (fred_available=true with non-empty observations),
    we preserve it (no overwrite) and exit non-zero.
  * Mirrors fetch_advisories.py and fetch_market.py conventions
    (shared UA string, requests session, per-source try/except).

CLI:
    python fetch_cpi.py                 # default --out v2/data-cpi.json
    python fetch_cpi.py --out PATH      # custom output path
    python fetch_cpi.py --no-network    # offline self-test (no HTTP)
"""

from __future__ import annotations

import argparse
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
DEFAULT_OUT = ROOT / "v2" / "data-cpi.json"

FRED_OBS_URL = "https://api.stlouisfed.org/fred/series/observations"

# Series catalog. `kind` drives front-end formatting:
#   - "index"  -> plain number (e.g. 318.4)
#   - "dollar" -> "$3.79" formatting
SERIES_CATALOG: list[dict[str, str]] = [
    {
        "id": "CPIAUCSL",
        "label": "CPI-U All Items",
        "unit": "Index 1982-84=100",
        "kind": "index",
    },
    {
        "id": "APU000074714",
        "label": "Gasoline (regular, retail)",
        "unit": "$/gallon",
        "kind": "dollar",
    },
    {
        "id": "APU0000708111",
        "label": "Eggs (Grade A large)",
        "unit": "$/dozen",
        "kind": "dollar",
    },
    {
        "id": "CUSR0000SAF11",
        "label": "Food at Home",
        "unit": "Index",
        "kind": "index",
    },
]

# Pull full history. 1947 covers CPIAUCSL's earliest observation; FRED safely
# ignores observation_start dates that pre-date a series' first reading.
OBSERVATION_START = "1947-01-01"


# ----- helpers ---------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _get_json(url: str, params: dict | None = None, timeout: int = 25) -> dict | None:
    """GET with shared UA; returns parsed JSON dict or None on any failure."""
    try:
        r = requests.get(url, params=params, headers=H, timeout=timeout)
        if r.status_code != 200:
            print(f"  [skip] {url} -> {r.status_code}", file=sys.stderr)
            return None
        return r.json()
    except Exception as e:
        print(f"  [skip] {url} -> {e}", file=sys.stderr)
        return None


def _parse_observations(j: dict | None) -> list[dict]:
    """FRED encodes missing observations as ``"."`` — those are filtered out.
    Returns rows as [{"date": "YYYY-MM-DD", "value": float}, ...]."""
    rows: list[dict] = []
    if not isinstance(j, dict):
        return rows
    for obs in (j.get("observations") or []):
        date = obs.get("date")
        raw = obs.get("value")
        if not date or raw is None or raw == "." or raw == "":
            continue
        try:
            rows.append({"date": date, "value": float(raw)})
        except (TypeError, ValueError):
            continue
    return rows


def fetch_one(meta: dict, api_key: str) -> dict:
    """Fetch a single series. Returns the series dict whether it succeeded or
    failed — on failure adds an "error" field and leaves observations empty.
    Never raises."""
    out: dict[str, Any] = {
        "id": meta["id"],
        "label": meta["label"],
        "unit": meta["unit"],
        "kind": meta["kind"],
        "observations": [],
    }
    try:
        j = _get_json(
            FRED_OBS_URL,
            {
                "series_id": meta["id"],
                "api_key": api_key,
                "file_type": "json",
                "observation_start": OBSERVATION_START,
            },
        )
        if j is None:
            out["error"] = "fetch failed (see stderr)"
            return out
        rows = _parse_observations(j)
        out["observations"] = rows
        if not rows:
            out["error"] = "no observations returned"
    except Exception as e:
        out["error"] = str(e)
    return out


def fetch_live(api_key: str) -> dict:
    """Drive all 4 series fetches; assemble the output payload."""
    series: list[dict] = []
    for meta in SERIES_CATALOG:
        print(f"  CPI: fetching {meta['id']} ({meta['label']})...")
        row = fetch_one(meta, api_key)
        n = len(row.get("observations") or [])
        if row.get("error"):
            print(f"    -> error: {row['error']}", file=sys.stderr)
        else:
            print(f"    -> {n} observations")
        series.append(row)
    return {
        "generated_at": _now_iso(),
        "fred_available": True,
        "series": series,
    }


def _payload_unavailable(note: str | None = None) -> dict:
    return {
        "generated_at": _now_iso(),
        "fred_available": False,
        "series": [],
        "note": note or "FRED_API_KEY not set — add to .env and re-run",
    }


def _all_series_failed(payload: dict) -> bool:
    """True if every series in the payload came back empty/errored."""
    series = payload.get("series") or []
    if not series:
        return True
    return all(not (s.get("observations") or []) for s in series)


def _prior_is_good(path: Path) -> bool:
    """Existing v2/data-cpi.json is considered good when fred_available is
    True AND at least one series has observations. The empty-state seed
    (fred_available=False) does NOT count and must not be preserved over a
    real-but-broken fetch — we'd rather show the fresh "all failed" payload
    so the user sees the failure surface."""
    if not path.exists():
        return False
    try:
        prior = json.loads(path.read_text())
    except Exception:
        return False
    if not prior.get("fred_available"):
        return False
    return not _all_series_failed(prior)


# ----- offline self-test fixture --------------------------------------------

_SAMPLE_FIXTURE = {
    "observations": [
        {"date": "1947-01-01", "value": "21.48"},
        {"date": "1947-02-01", "value": "21.62"},
        {"date": "1947-03-01", "value": "22.00"},
        # Missing-value sentinel — must be filtered.
        {"date": "1947-04-01", "value": "."},
        {"date": "1947-05-01", "value": "21.95"},
    ]
}


def _self_test() -> int:
    """Offline parser sanity + payload shape check. Returns 0 on pass."""
    rows = _parse_observations(_SAMPLE_FIXTURE)
    # No-key payload shape.
    no_key = _payload_unavailable()

    assertions = [
        (len(rows) == 4, f"expected 4 rows after filtering '.', got {len(rows)}"),
        (rows[0] == {"date": "1947-01-01", "value": 21.48},
         f"row[0]={rows[0]!r}"),
        (isinstance(rows[2]["value"], float),
         "values should be cast to float"),
        (no_key["fred_available"] is False,
         "unavailable payload should set fred_available=False"),
        ("note" in no_key, "unavailable payload should include a note"),
        (no_key["series"] == [], "unavailable payload should have empty series"),
        # _all_series_failed handling
        (_all_series_failed({"series": []}) is True,
         "_all_series_failed should return True for empty series list"),
        (_all_series_failed({"series": [{"id": "X", "observations": []}]}) is True,
         "_all_series_failed should return True when every series is empty"),
        (_all_series_failed({
            "series": [
                {"id": "A", "observations": []},
                {"id": "B", "observations": [{"date": "2026-01-01", "value": 1.0}]},
            ]}) is False,
         "_all_series_failed should return False when any series has rows"),
        # Catalog sanity.
        (len(SERIES_CATALOG) == 4,
         f"expected 4 series, got {len(SERIES_CATALOG)}"),
        (set(s["id"] for s in SERIES_CATALOG) ==
         {"CPIAUCSL", "APU000074714", "APU0000708111", "CUSR0000SAF11"},
         "series IDs don't match the spec"),
    ]
    failed = [msg for ok, msg in assertions if not ok]
    if failed:
        for f in failed:
            print(f"  [self-test FAIL] {f}", file=sys.stderr)
        return 1
    print(f"  [self-test OK] {len(rows)} observations parsed; "
          f"{len(SERIES_CATALOG)} series in catalog; all assertions passed.")
    return 0


# ----- CLI ------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Fetch FRED Consumer Price Index series.")
    ap.add_argument("--out", default=str(DEFAULT_OUT),
                    help=f"Output JSON path (default: {DEFAULT_OUT})")
    ap.add_argument("--no-network", action="store_true",
                    help="Run offline parser self-test and exit (no HTTP).")
    args = ap.parse_args(argv)

    if args.no_network:
        return _self_test()

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    api_key = os.environ.get("FRED_API_KEY", "").strip()
    if not api_key:
        # Clean opt-out branch. Always overwrites with the unavailable payload
        # so the dashboard's empty state is fresh-dated, not stale from a
        # previous broken run.
        payload = _payload_unavailable()
        out_path.write_text(json.dumps(payload, indent=2))
        print(f"  [cpi] FRED_API_KEY not set; wrote {out_path} with empty-state payload.")
        return 0

    payload = fetch_live(api_key)

    # Stale-fallback: if every series failed but a prior good file exists,
    # don't clobber. Surface non-zero so CI logs the failure.
    if _all_series_failed(payload) and _prior_is_good(out_path):
        print(f"  [cpi] every series fetch failed; preserving prior "
              f"{out_path} (no overwrite)", file=sys.stderr)
        return 1

    out_path.write_text(json.dumps(payload, indent=2))
    n_ok = sum(1 for s in payload["series"] if s.get("observations"))
    n_err = sum(1 for s in payload["series"] if s.get("error"))
    total_obs = sum(len(s.get("observations") or []) for s in payload["series"])
    print(f"  Wrote {out_path} ({n_ok}/{len(payload['series'])} series ok, "
          f"{n_err} errored, {total_obs:,} total observations)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
