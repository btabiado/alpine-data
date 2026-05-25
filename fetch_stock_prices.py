"""
Per-ticker hourly price fetcher for the V2 dashboard's Stocks tab.

Source (free, no auth required):
  Yahoo Finance   /v8/finance/chart/<TICKER>?range=7d&interval=1h
                  Same public endpoint pattern fetch_metals.py uses for
                  GC=F / SI=F. Returns up to ~168 hourly points (fewer
                  during partial weeks / off-market hours).

Output: v2/data-stock-prices.json (sidecar consumed by the V2 dashboard's
stock ticker modal via the existing SIDECARS lazy-load mechanism).

The fetcher pulls the list of tickers from the existing data/market.json
(``stocks_signals[].symbol``) so we automatically follow whatever the
top-50 most-active US equity ranking surfaces today — no separate ticker
list to drift out of sync.

Schema:
    {
      "generated_at": "2026-05-25T17:30:00Z",
      "interval": "1h",
      "range": "7d",
      "by_symbol": {
        "INTC": {"points": [{"t": "2026-05-19T14:30:00Z", "p": 21.45}, ...],
                  "pct_change": -3.21},
        "NVDA": {"points": [...], "pct_change": 1.84},
        ...
      }
    }

Resilience:
  * Per-ticker try/except — one bad ticker doesn't kill the rest.
  * Stale-fallback: if EVERY ticker fails this run, preserve the prior
    data-stock-prices.json on disk intact (don't blank it).
  * Partial-update: when some tickers fail and a prior file exists, the
    prior per-symbol payload is kept for those failing tickers so the
    modal never shows "unavailable" for a ticker that worked yesterday
    just because Yahoo blipped on this run.
  * 200ms inter-call delay to be polite to Yahoo's public endpoint and
    avoid trip rate limits.

CLI:
    python fetch_stock_prices.py                 # default --out v2/data-stock-prices.json
    python fetch_stock_prices.py --out PATH      # custom output path
    python fetch_stock_prices.py --no-network    # offline self-test (mock ticker)
    python fetch_stock_prices.py --limit N       # cap ticker count (default 50)
    python fetch_stock_prices.py --tickers SYM,SYM   # explicit list (overrides market.json)
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

UA = "Mozilla/5.0 (compatible; etf-flow-dashboard/1.0; +stock-price-fetcher)"
H = {"User-Agent": UA}
ROOT = Path(__file__).parent
DEFAULT_OUT = ROOT / "v2" / "data-stock-prices.json"
DEFAULT_MARKET_JSON = ROOT / "data" / "market.json"

# Yahoo's public chart endpoint. Same shape fetch_metals._yahoo_daily uses
# for GC=F / SI=F, but with interval=1h instead of 1d.
_YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"

# Default cap. Yahoo is reasonably permissive but we still keep the per-build
# fanout bounded so a future "top-200 stocks" expansion doesn't accidentally
# fire 200 requests on every Pages build.
DEFAULT_LIMIT = 50

# Polite delay between Yahoo calls. The endpoint isn't documented as
# rate-limited but 5 req/sec is well under what fetch_market.py already
# pings Yahoo with via its index fetcher.
INTER_CALL_DELAY_SEC = 0.2


# ----- ticker list ----------------------------------------------------------

def _load_tickers_from_market(path: Path, limit: int) -> list[str]:
    """Pull up to ``limit`` ticker symbols from ``data/market.json``'s
    ``stocks_signals`` array. Returns an empty list (rather than raising)
    when the file is missing or malformed so a fresh checkout without a
    pre-built market.json doesn't break the pipeline — the caller can
    fall through to a stale-fallback or explicit --tickers list.
    """
    if not path.exists():
        print(f"  [stock-prices] {path} not found; no tickers to fetch.",
              file=sys.stderr)
        return []
    try:
        data = json.loads(path.read_text())
    except Exception as e:
        print(f"  [stock-prices] could not parse {path}: {e}", file=sys.stderr)
        return []
    rows = data.get("stocks_signals") or []
    if not isinstance(rows, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for r in rows:
        if not isinstance(r, dict):
            continue
        sym = str(r.get("symbol") or "").strip().upper()
        if not sym or sym in seen:
            continue
        seen.add(sym)
        out.append(sym)
        if len(out) >= limit:
            break
    return out


# ----- Yahoo fetch ----------------------------------------------------------

def _fetch_yahoo_hourly(symbol: str, timeout: int = 20) -> dict | None:
    """Hourly closes for ``symbol`` over the last 7 days from Yahoo's
    public chart endpoint. Returns a dict ``{"points": [...], "pct_change": float}``
    or None on any failure / empty payload. ``pct_change`` is computed
    from the first to the last non-null close in the window.
    """
    try:
        r = requests.get(
            _YAHOO_URL.format(symbol=symbol),
            params={"range": "7d", "interval": "1h"},
            headers=H,
            timeout=timeout,
        )
    except Exception as e:
        print(f"  [stock-prices] {symbol}: request failed: {e}",
              file=sys.stderr)
        return None
    if r.status_code != 200:
        print(f"  [stock-prices] {symbol}: HTTP {r.status_code}", file=sys.stderr)
        return None
    try:
        j = r.json()
    except ValueError as e:
        print(f"  [stock-prices] {symbol}: bad JSON: {e}", file=sys.stderr)
        return None
    return _parse_yahoo_chart(j)


def _parse_yahoo_chart(j: Any) -> dict | None:
    """Pure parser for Yahoo's /v8/finance/chart response. Split out from
    the network call so the offline self-test can exercise it directly.
    Returns the same shape as _fetch_yahoo_hourly, or None on shape error.
    """
    if not isinstance(j, dict):
        return None
    try:
        result_list = (j.get("chart") or {}).get("result") or []
        if not result_list:
            return None
        result = result_list[0] or {}
        ts = result.get("timestamp") or []
        quote_list = ((result.get("indicators") or {}).get("quote") or [])
        closes = (quote_list[0] if quote_list else {}).get("close") or []
    except (AttributeError, TypeError, IndexError):
        return None
    points: list[dict] = []
    for t, c in zip(ts, closes):
        if t is None or c is None:
            continue
        try:
            t_iso = (datetime.fromtimestamp(int(t), tz=timezone.utc)
                     .strftime("%Y-%m-%dT%H:%M:%SZ"))
        except (TypeError, ValueError, OverflowError):
            continue
        try:
            p = float(c)
        except (TypeError, ValueError):
            continue
        points.append({"t": t_iso, "p": round(p, 4)})
    if len(points) < 2:
        return None
    first = points[0]["p"]
    last = points[-1]["p"]
    pct = round((last - first) / first * 100, 2) if first else 0.0
    return {"points": points, "pct_change": pct}


# ----- main build -----------------------------------------------------------

def build_payload(
    tickers: list[str],
    prior: dict | None = None,
    delay: float = INTER_CALL_DELAY_SEC,
) -> dict:
    """Fetch hourly prices for each ticker (serial, with a small delay
    between calls). Returns the full output payload.

    When ``prior`` is provided, per-ticker failures fall back to the prior
    payload for that ticker so a transient Yahoo blip doesn't drop a row
    that worked yesterday.
    """
    prior_by_sym: dict[str, dict] = {}
    if isinstance(prior, dict):
        prior_by_sym = prior.get("by_symbol") or {}
        if not isinstance(prior_by_sym, dict):
            prior_by_sym = {}

    by_symbol: dict[str, dict] = {}
    fresh = 0
    stale = 0
    failed_no_prior = 0
    for i, sym in enumerate(tickers):
        if i > 0 and delay > 0:
            time.sleep(delay)
        blob = _fetch_yahoo_hourly(sym)
        if blob and blob.get("points"):
            by_symbol[sym] = blob
            fresh += 1
            continue
        # Fall back to prior payload for THIS ticker if available so we don't
        # show a fresh "unavailable" state for a ticker that worked yesterday.
        prior_blob = prior_by_sym.get(sym)
        if isinstance(prior_blob, dict) and prior_blob.get("points"):
            by_symbol[sym] = prior_blob
            stale += 1
        else:
            failed_no_prior += 1

    print(f"  [stock-prices] {fresh} fresh, {stale} stale-fallback, "
          f"{failed_no_prior} dropped of {len(tickers)} requested.")

    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "interval": "1h",
        "range": "7d",
        "by_symbol": by_symbol,
    }


# ----- self test ------------------------------------------------------------

_SAMPLE_YAHOO = {
    "chart": {
        "result": [{
            "meta": {"symbol": "MOCK"},
            # Two timestamps an hour apart (epoch seconds).
            "timestamp": [1700000000, 1700003600],
            "indicators": {"quote": [{"close": [10.0, 11.5]}]},
        }],
        "error": None,
    }
}


def _self_test() -> int:
    failed: list[str] = []

    # _parse_yahoo_chart happy path.
    parsed = _parse_yahoo_chart(_SAMPLE_YAHOO)
    if not parsed:
        failed.append("_parse_yahoo_chart returned None on valid sample")
    else:
        pts = parsed.get("points") or []
        if len(pts) != 2:
            failed.append(f"expected 2 points, got {len(pts)}")
        if pts and (pts[0]["p"] != 10.0 or pts[-1]["p"] != 11.5):
            failed.append(f"point values wrong: {pts}")
        if parsed.get("pct_change") != 15.0:
            failed.append(f"pct_change wrong: {parsed.get('pct_change')}")
        if pts and not pts[0]["t"].endswith("Z"):
            failed.append(f"timestamp not ISO-Z: {pts[0]['t']}")

    # Reject None / empty / malformed.
    if _parse_yahoo_chart(None) is not None:
        failed.append("_parse_yahoo_chart should reject None")
    if _parse_yahoo_chart({}) is not None:
        failed.append("_parse_yahoo_chart should reject empty dict")
    if _parse_yahoo_chart({"chart": {"result": []}}) is not None:
        failed.append("_parse_yahoo_chart should reject empty result")

    # Reject all-None closes (would yield 0 points).
    none_closes = {
        "chart": {"result": [{
            "timestamp": [1700000000, 1700003600],
            "indicators": {"quote": [{"close": [None, None]}]},
        }]}
    }
    if _parse_yahoo_chart(none_closes) is not None:
        failed.append("_parse_yahoo_chart should reject all-None closes")

    # Single point isn't enough to compute pct_change — reject.
    single = {
        "chart": {"result": [{
            "timestamp": [1700000000],
            "indicators": {"quote": [{"close": [10.0]}]},
        }]}
    }
    if _parse_yahoo_chart(single) is not None:
        failed.append("_parse_yahoo_chart should reject single-point series")

    # build_payload with a fake ticker via prior-fallback path (network-free).
    # Pass empty tickers list so no requests fire; result should be an empty
    # by_symbol but still a valid payload shape.
    pay = build_payload([], delay=0)
    if not isinstance(pay.get("by_symbol"), dict):
        failed.append("build_payload missing by_symbol dict")
    if pay.get("interval") != "1h" or pay.get("range") != "7d":
        failed.append("build_payload metadata wrong")
    if not str(pay.get("generated_at", "")).endswith("Z"):
        failed.append("build_payload generated_at not ISO-Z")

    if failed:
        for f in failed:
            print(f"  [self-test FAIL] {f}", file=sys.stderr)
        return 1
    print("  [self-test OK] all parser assertions passed.")
    return 0


# ----- CLI ------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Fetch 7d hourly Yahoo Finance prices for top US stocks.")
    ap.add_argument("--out", default=str(DEFAULT_OUT),
                    help=f"Output JSON path (default: {DEFAULT_OUT})")
    ap.add_argument("--market-json", default=str(DEFAULT_MARKET_JSON),
                    help="Path to market.json (for stocks_signals ticker list)")
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                    help=f"Max tickers to fetch (default: {DEFAULT_LIMIT})")
    ap.add_argument("--tickers", default="",
                    help="Comma-separated ticker override (skips market.json)")
    ap.add_argument("--no-network", action="store_true",
                    help="Run offline parser self-test and exit (no HTTP).")
    args = ap.parse_args(argv)

    if args.no_network:
        return _self_test()

    # Ticker list — explicit override wins, else read from market.json.
    if args.tickers.strip():
        tickers = [t.strip().upper() for t in args.tickers.split(",")
                   if t.strip()]
        # Apply --limit to the explicit list too so a typo (1000 tickers)
        # doesn't accidentally fan out.
        tickers = tickers[: args.limit]
    else:
        tickers = _load_tickers_from_market(Path(args.market_json), args.limit)

    out_path = Path(args.out)

    # Load prior payload for both per-ticker stale-fallback AND total-fail
    # fallback (keep the file on disk untouched if everything dies).
    prior: dict | None = None
    if out_path.exists():
        try:
            prior = json.loads(out_path.read_text())
        except Exception as e:
            print(f"  [stock-prices] could not read prior {out_path}: {e}",
                  file=sys.stderr)

    if not tickers:
        # No tickers to work with — preserve prior file if it exists, else
        # bail with a non-zero exit so the caller can log it without
        # blanking a (possibly good) prior payload.
        if prior:
            print("  [stock-prices] no tickers from market.json; "
                  "leaving prior data-stock-prices.json intact.",
                  file=sys.stderr)
            return 0
        print("  [stock-prices] no tickers and no prior file — nothing to do.",
              file=sys.stderr)
        return 1

    payload = build_payload(tickers, prior=prior)

    if not payload.get("by_symbol"):
        # Every ticker failed AND no prior to mix into. Don't write an empty
        # file — that would convert a transient outage into a permanent
        # "unavailable" experience in the modal.
        if prior:
            print("  [stock-prices] every ticker failed; "
                  "preserving prior data-stock-prices.json intact.",
                  file=sys.stderr)
            return 0
        print("  [stock-prices] every ticker failed and no prior to fall "
              "back on.", file=sys.stderr)
        return 1

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload))
    print(f"  Wrote {out_path} ({out_path.stat().st_size:,} bytes, "
          f"{len(payload['by_symbol'])} symbols)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
