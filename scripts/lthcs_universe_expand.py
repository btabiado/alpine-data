#!/usr/bin/env python3
"""Convert a CSV of new universe candidates into validated per-ticker JSON.

This is the entry point for adding new tickers to the LTHCS universe
ahead of the Phase 3 re-audit (2026-05-26) and the planned S&P 500
expansion (~167 → ~500). It does NOT touch the production
``data/lthcs/universe.json``; the only thing it writes is one record
per candidate ticker under ``data/lthcs/universe_candidate/`` plus a
``_summary.json`` that lists which tickers passed and which failed
(with reasons).

Per-row validation:
    - Ticker format: ``^[A-Z][A-Z0-9.\\-]{0,9}$`` (uppercase ASCII, up
      to 10 chars, must start with a letter).
    - Sector + maturity_stage must be drawn from the LTHCS taxonomy
      already used by the production universe.
    - SEC CIK resolution via the EDGAR ticker→CIK file
      (https://www.sec.gov/files/company_tickers.json), cached locally.
    - Yahoo Finance smoke fetch (best-effort, network failures are
      recorded but do not abort the row).
    - Finnhub /news-sentiment smoke fetch (skipped if FINNHUB_API_KEY
      is not set — still recorded in the summary).
    - CUSIP resolution: best-effort via the local SEC 13F CUSIP map
      (``data/lthcs/13f_cusip_map.json``). Left empty when unknown.

CLI:
    python scripts/lthcs_universe_expand.py \\
        --input new_candidates.csv \\
        --output-dir data/lthcs/universe_candidate/

Required CSV columns:
    ticker, name, sector, sector_group, maturity_stage, index_membership

Optional CSV columns:
    tech_sub_bucket, cik, cusip, aliases

``index_membership`` and ``aliases`` accept a ``|``-separated list
(e.g. ``S&P 500|NASDAQ-100``) — commas would collide with CSV.

Output per ticker:
    data/lthcs/universe_candidate/<TICKER>.json

Output summary (whether any rows existed or not):
    data/lthcs/universe_candidate/_summary.json
"""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_DIR = REPO_ROOT / "data" / "lthcs" / "universe_candidate"
SEC_TICKERS_CACHE = REPO_ROOT / ".cache" / "lthcs" / "sec_ticker_map.json"
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"

# Taxonomy mirrored from data/lthcs/universe.json. Kept here (not
# imported from lthcs.*) to make this script runnable without the full
# pipeline dependency stack.
ALLOWED_SECTORS = {
    "Communication Services",
    "Consumer Discretionary",
    "Consumer Staples",
    "Energy",
    "Financials",
    "Health Care",
    "Industrials",
    "Materials",
    "Real Estate",
    "Technology",
    "Utilities",
}

ALLOWED_MATURITY_STAGES = {
    "pre_revenue_growth",
    "pre_profit_growth",
    "growth_compounder",
    "standard_compounder",
    "mature_compounder",
    "recovery_rerating",
    "recovery_stabilization",
    "financial",
}

TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")

REQUIRED_COLS = (
    "ticker",
    "name",
    "sector",
    "sector_group",
    "maturity_stage",
    "index_membership",
)
OPTIONAL_COLS = ("tech_sub_bucket", "cik", "cusip", "aliases")


# ---------------------------------------------------------------------------
# Per-row result accumulator
# ---------------------------------------------------------------------------

@dataclass
class RowResult:
    ticker: str
    passed: bool = False
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    record: Dict[str, Any] = field(default_factory=dict)
    checks: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# SEC ticker map (cached locally)
# ---------------------------------------------------------------------------

def _load_sec_ticker_map(refresh: bool = False) -> Dict[str, str]:
    """Return a {TICKER: 10-digit-CIK} map.

    Loads from the local cache when present. When the cache is stale
    (>30 days), or ``refresh`` is True, attempts to refetch via the
    public SEC endpoint. Falls back to the stale cache on network error
    so the script still runs in offline mode.
    """
    SEC_TICKERS_CACHE.parent.mkdir(parents=True, exist_ok=True)
    needs_refresh = refresh or not SEC_TICKERS_CACHE.exists()
    if not needs_refresh:
        try:
            age = _dt.datetime.utcnow().timestamp() - SEC_TICKERS_CACHE.stat().st_mtime
            if age > 30 * 86400:
                needs_refresh = True
        except OSError:
            needs_refresh = True

    if needs_refresh:
        try:
            import requests  # local import: tests stub this out
            ua = os.environ.get(
                "SEC_USER_AGENT",
                "lthcs-universe-expand bryan@example.com",
            )
            resp = requests.get(
                SEC_TICKERS_URL,
                headers={"User-Agent": ua, "Accept": "application/json"},
                timeout=30,
            )
            if resp.status_code == 200:
                payload = resp.json()
                SEC_TICKERS_CACHE.write_text(json.dumps(payload))
        except Exception:
            # Stay on the stale cache. Surfaced per-row as a warning.
            pass

    if not SEC_TICKERS_CACHE.exists():
        return {}

    try:
        raw = json.loads(SEC_TICKERS_CACHE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}

    # The SEC payload is {"0": {"cik_str": ..., "ticker": "AAPL", ...}, ...}
    out: Dict[str, str] = {}
    if isinstance(raw, dict):
        for entry in raw.values():
            if not isinstance(entry, dict):
                continue
            sym = str(entry.get("ticker") or "").upper().strip()
            cik = entry.get("cik_str")
            if sym and cik is not None:
                out[sym] = str(int(cik)).zfill(10)
    return out


def _resolve_cik(ticker: str, sec_map: Dict[str, str]) -> Optional[str]:
    """Resolve ticker to 10-digit CIK using the SEC map.

    Tries the exact match first, then dot- and hyphen-stripped variants
    (so ``BRK.B`` resolves via ``BRKB``).
    """
    norm = ticker.strip().upper()
    candidates = [norm, norm.replace(".", ""), norm.replace("-", ""), norm.replace(".", "-")]
    for c in candidates:
        if c in sec_map:
            return sec_map[c]
    return None


# ---------------------------------------------------------------------------
# Local 13F CUSIP map (best-effort)
# ---------------------------------------------------------------------------

def _load_cusip_map() -> Dict[str, str]:
    path = REPO_ROOT / "data" / "lthcs" / "13f_cusip_map.json"
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    # Expected shape: {ticker: cusip} or {ticker: {"cusip": ...}}
    out: Dict[str, str] = {}
    for k, v in raw.items():
        sym = str(k).upper().strip()
        if isinstance(v, str):
            out[sym] = v
        elif isinstance(v, dict):
            cusip = v.get("cusip") or v.get("CUSIP")
            if cusip:
                out[sym] = str(cusip)
    return out


# ---------------------------------------------------------------------------
# Smoke-fetches (optional, network)
# ---------------------------------------------------------------------------

def _yahoo_smoke_fetch(ticker: str) -> Tuple[bool, str]:
    """Lightweight 'does Yahoo know this ticker' probe.

    Returns (ok, detail). We try the real ``yfinance`` only if the
    module imports cleanly; otherwise we fall back to a plain HTTP
    request to Yahoo's chart endpoint so the script remains useful in
    environments without yfinance installed.
    """
    try:
        import yfinance as yf  # type: ignore
        try:
            df = yf.Ticker(ticker).history(period="5d")
            if df is None or len(df) == 0:
                return False, "yfinance returned empty history"
            return True, "yfinance ok (%d rows)" % len(df)
        except Exception as exc:
            return False, "yfinance error: %s" % exc
    except ImportError:
        pass

    # Fallback: plain HTTP probe
    try:
        import requests
        url = (
            "https://query1.finance.yahoo.com/v8/finance/chart/"
            f"{ticker}?range=5d&interval=1d"
        )
        resp = requests.get(url, timeout=15, headers={"User-Agent": "lthcs-prep/1.0"})
        if resp.status_code != 200:
            return False, "yahoo chart HTTP %s" % resp.status_code
        body = resp.json()
        if not (body or {}).get("chart", {}).get("result"):
            return False, "yahoo chart no result"
        return True, "yahoo chart ok"
    except Exception as exc:
        return False, "yahoo http error: %s" % exc


def _finnhub_smoke_fetch(ticker: str) -> Tuple[bool, str]:
    """Probe Finnhub /news-sentiment for ``ticker``.

    Returns (ok, detail). Skips with ``ok=False, detail='skipped'`` when
    ``FINNHUB_API_KEY`` is not set so the script remains usable in
    development.
    """
    key = os.environ.get("FINNHUB_API_KEY", "").strip()
    if not key:
        return False, "skipped (FINNHUB_API_KEY not set)"
    try:
        import requests
        resp = requests.get(
            "https://finnhub.io/api/v1/news-sentiment",
            params={"symbol": ticker, "token": key},
            timeout=15,
        )
        if resp.status_code == 429:
            return False, "finnhub HTTP 429 (rate limit)"
        if resp.status_code != 200:
            return False, "finnhub HTTP %s" % resp.status_code
        body = resp.json()
        if not isinstance(body, dict):
            return False, "finnhub non-dict body"
        # Some unknown tickers return an empty body with HTTP 200; that
        # is still a 'ticker is unknown' signal we want to flag.
        if not body or body.get("symbol", "").upper() != ticker.upper():
            return False, "finnhub returned no data for %s" % ticker
        return True, "finnhub ok"
    except Exception as exc:
        return False, "finnhub error: %s" % exc


# ---------------------------------------------------------------------------
# Per-row processor
# ---------------------------------------------------------------------------

def _split_list(raw: Optional[str]) -> List[str]:
    if not raw:
        return []
    return [s.strip() for s in str(raw).split("|") if s.strip()]


def validate_row(row: Dict[str, str]) -> RowResult:
    """Pure-data validation. No network. Used by the test suite."""
    ticker = (row.get("ticker") or "").strip().upper()
    result = RowResult(ticker=ticker)

    # ---- Required-column presence + format ----
    missing = [c for c in REQUIRED_COLS if not (row.get(c) or "").strip()]
    if missing:
        result.errors.append("missing required columns: %s" % ",".join(missing))

    if not TICKER_RE.match(ticker):
        result.errors.append(
            "ticker %r fails format regex %s" % (ticker, TICKER_RE.pattern)
        )

    sector = (row.get("sector") or "").strip()
    if sector and sector not in ALLOWED_SECTORS:
        result.errors.append(
            "sector %r not in LTHCS taxonomy" % sector
        )

    stage = (row.get("maturity_stage") or "").strip()
    if stage and stage not in ALLOWED_MATURITY_STAGES:
        result.errors.append(
            "maturity_stage %r not in LTHCS taxonomy" % stage
        )

    index_membership = _split_list(row.get("index_membership"))
    if not index_membership:
        result.errors.append("index_membership empty")

    # ---- Compose candidate record (production-universe schema) ----
    record: Dict[str, Any] = {
        "ticker": ticker,
        "name": (row.get("name") or "").strip(),
        "sector": sector,
        "sector_group": (row.get("sector_group") or "").strip(),
        "maturity_stage": stage,
        "index_membership": index_membership,
        "active": True,
        "source": "universe_expand",
    }
    if (row.get("tech_sub_bucket") or "").strip():
        record["tech_sub_bucket"] = row["tech_sub_bucket"].strip()
    if (row.get("cik") or "").strip():
        record["cik"] = str(row["cik"]).strip().zfill(10)
    if (row.get("cusip") or "").strip():
        record["cusip"] = row["cusip"].strip().upper()
    aliases = _split_list(row.get("aliases"))
    if aliases:
        record["aliases"] = aliases

    result.record = record
    result.passed = not result.errors
    return result


def process_row(
    row: Dict[str, str],
    *,
    sec_map: Dict[str, str],
    cusip_map: Dict[str, str],
    do_network_smoke: bool = True,
) -> RowResult:
    """Full per-row pipeline: validate, then enrich with CIK/CUSIP/smoke."""
    result = validate_row(row)
    if not result.passed:
        return result

    ticker = result.ticker

    # ---- CIK resolution ----
    cik = result.record.get("cik") or _resolve_cik(ticker, sec_map)
    if cik:
        result.record["cik"] = str(cik).zfill(10)
        result.checks["cik"] = "resolved"
    else:
        result.warnings.append("no CIK match in SEC ticker map")
        result.checks["cik"] = "missing"

    # ---- CUSIP resolution (best-effort) ----
    if not result.record.get("cusip"):
        cusip = cusip_map.get(ticker)
        if cusip:
            result.record["cusip"] = str(cusip).upper()
            result.checks["cusip"] = "resolved_from_13f_map"
        else:
            result.checks["cusip"] = "missing"

    # ---- Network smoke fetches ----
    if do_network_smoke:
        ok, detail = _yahoo_smoke_fetch(ticker)
        result.checks["yahoo"] = {"ok": ok, "detail": detail}
        if not ok:
            result.warnings.append("yahoo smoke fetch failed: %s" % detail)

        ok, detail = _finnhub_smoke_fetch(ticker)
        result.checks["finnhub"] = {"ok": ok, "detail": detail}
        if not ok and not detail.startswith("skipped"):
            result.warnings.append("finnhub smoke fetch failed: %s" % detail)
    else:
        result.checks["yahoo"] = {"ok": None, "detail": "skipped"}
        result.checks["finnhub"] = {"ok": None, "detail": "skipped"}

    # Warnings do not flip ``passed`` to False — only hard validation
    # errors do. A ticker missing a CIK / failing a Yahoo smoke fetch
    # still gets a candidate JSON written so the operator can decide.
    return result


# ---------------------------------------------------------------------------
# I/O orchestration
# ---------------------------------------------------------------------------

def read_csv(path: Path) -> List[Dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        return [dict(row) for row in reader]


def write_results(
    results: List[RowResult],
    output_dir: Path,
) -> Dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)

    passed: List[Dict[str, Any]] = []
    failed: List[Dict[str, Any]] = []
    for r in results:
        out_path = output_dir / ("%s.json" % (r.ticker or "_UNKNOWN"))
        envelope = {
            "ticker": r.ticker,
            "passed": r.passed,
            "errors": r.errors,
            "warnings": r.warnings,
            "record": r.record,
            "checks": r.checks,
            "generated_at": _dt.datetime.utcnow().isoformat(timespec="seconds") + "Z",
        }
        out_path.write_text(json.dumps(envelope, indent=2, sort_keys=True))
        if r.passed:
            passed.append({"ticker": r.ticker, "warnings": r.warnings})
        else:
            failed.append({"ticker": r.ticker, "errors": r.errors})

    summary = {
        "total": len(results),
        "passed_count": len(passed),
        "failed_count": len(failed),
        "passed": passed,
        "failed": failed,
        "generated_at": _dt.datetime.utcnow().isoformat(timespec="seconds") + "Z",
    }
    (output_dir / "_summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True))
    return summary


def run(
    input_path: Path,
    output_dir: Path,
    *,
    do_network_smoke: bool = True,
    refresh_sec_map: bool = False,
) -> Dict[str, Any]:
    rows = read_csv(input_path)
    sec_map = _load_sec_ticker_map(refresh=refresh_sec_map)
    cusip_map = _load_cusip_map()
    results = [
        process_row(
            row,
            sec_map=sec_map,
            cusip_map=cusip_map,
            do_network_smoke=do_network_smoke,
        )
        for row in rows
    ]
    return write_results(results, output_dir)


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="lthcs_universe_expand",
        description="Validate + enrich a CSV of new LTHCS universe candidates.",
    )
    p.add_argument(
        "--input",
        required=True,
        help="Path to a CSV with columns: %s" % ",".join(REQUIRED_COLS),
    )
    p.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT_DIR),
        help="Directory to write per-ticker JSON + _summary.json (default: %s)"
        % DEFAULT_OUTPUT_DIR,
    )
    p.add_argument(
        "--no-network",
        action="store_true",
        help="Skip Yahoo + Finnhub smoke fetches (faster, useful for dry runs).",
    )
    p.add_argument(
        "--refresh-sec-map",
        action="store_true",
        help="Force-refresh the cached SEC ticker→CIK file.",
    )
    args = p.parse_args(argv)

    input_path = Path(args.input)
    if not input_path.is_file():
        print("error: --input not found: %s" % input_path, file=sys.stderr)
        return 2

    summary = run(
        input_path=input_path,
        output_dir=Path(args.output_dir),
        do_network_smoke=not args.no_network,
        refresh_sec_map=args.refresh_sec_map,
    )

    print(
        "wrote %d candidate JSONs to %s (passed=%d, failed=%d)"
        % (
            summary["total"],
            args.output_dir,
            summary["passed_count"],
            summary["failed_count"],
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
