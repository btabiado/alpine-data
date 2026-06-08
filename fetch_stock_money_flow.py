"""Stock Flows — per-stock money-flow scoring for index constituents.

This is the compute module behind the **Stock Flows** subtab (under the Markets
dropdown). It applies the SAME accumulation/distribution lens as the index-level
Money Flow Index gauge — :func:`money_flow.mfi` / :func:`money_flow.cmf` — but to
the *individual companies* that make up the three major indexes, grouped by
index membership.

Distinct from the existing **Stocks** tab (top-50 most-active scored on technical
momentum: SMA/RSI/MACD). Here every name in the LTHCS universe that belongs to
the Dow (DJIA), Nasdaq-100, or S&P 500 is fetched (~6mo daily OHLCV from Yahoo),
run through MFI(14) + CMF(20), and blended into a single ±100 money-flow score
with the same band labels as the index gauge.

Score blend (mapped to -100..100):

    score = clamp( round( 0.6*((mfi-50)*2) + 0.4*(cmf*200), 1 ), -100, 100 )

when both MFI and CMF are present. If only MFI is available the CMF term drops
and ``score = (mfi-50)*2``. A name with neither indicator is skipped (counts as a
fetch/score failure rather than a fake neutral).

Band labels (identical to the index Money Flow Index gauge):

    score <= -60   "Heavy Outflow"
    -60 < s <= -30 "Outflow"
    -30 < s <  30  "Neutral"
    30 <= s < 60   "Inflow"
    s >= 60        "Heavy Inflow"

Emits ``data-stock-money-flow.json`` per the shared data contract.

Defensive throughout: per-ticker fetch failures are caught and counted; the build
never crashes on a bad/empty Yahoo response.
"""

from __future__ import annotations

import json
import os
import random
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from money_flow import cmf, mfi

__all__ = ["build_stock_money_flow", "main"]

# ---------------------------------------------------------------------------
# Paths / constants
# ---------------------------------------------------------------------------

_HERE = os.path.dirname(os.path.abspath(__file__))
UNIVERSE_PATH = os.path.join(_HERE, "data", "lthcs", "universe.json")
OUTPUT_PATH = os.path.join(_HERE, "data-stock-money-flow.json")

# The three index buckets we score. A stock can belong to several; we keep the
# raw membership values (including "S&P 100") in the emitted record, but only
# these three decide whether a stock is in-scope at all.
SCOPE_INDICES = ("DJIA", "NASDAQ-100", "S&P 500")
# Memberships we surface in the output's "indices" field (scope + S&P 100).
KEEP_INDICES = ("DJIA", "NASDAQ-100", "S&P 500", "S&P 100")

# 8 concurrent workers in production. Overridable via env for environments whose
# IP is being rate-limited by Yahoo (lets verification run at lower concurrency
# without changing the shipped default).
try:
    _MAX_WORKERS = max(1, int(os.environ.get("SMF_WORKERS", "8")))
except ValueError:
    _MAX_WORKERS = 8
_FETCH_TIMEOUT = 25
_MAX_RETRIES = 5  # retry transient throttling (HTTP 429) with backoff
_JITTER_MIN = 0.10  # per-request startup jitter (seconds) to de-burst the pool
_JITTER_MAX = 0.60
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# Band labels — IDENTICAL to money_flow._band_label / the index gauge.
# ---------------------------------------------------------------------------

def _band_label(score: float) -> str:
    if score <= -60:
        return "Heavy Outflow"
    if score <= -30:
        return "Outflow"
    if score < 30:
        return "Neutral"
    if score < 60:
        return "Inflow"
    return "Heavy Inflow"


def _clip(value: float, lo: float, hi: float) -> float:
    if value < lo:
        return lo
    if value > hi:
        return hi
    return value


# ---------------------------------------------------------------------------
# Universe loading
# ---------------------------------------------------------------------------

def _load_universe() -> List[Dict[str, Any]]:
    """Return the raw ticker records from ``data/lthcs/universe.json``.

    Returns ``[]`` if the file is missing/unreadable rather than raising.
    """
    try:
        with open(UNIVERSE_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    tickers = data.get("tickers") if isinstance(data, dict) else None
    return tickers if isinstance(tickers, list) else []


def _in_scope(rec: Dict[str, Any]) -> bool:
    """True when the record belongs to at least one of the three scope indexes."""
    membership = rec.get("index_membership") or []
    if not isinstance(membership, list):
        return False
    return any(m in SCOPE_INDICES for m in membership)


def _scope_indices(rec: Dict[str, Any]) -> List[str]:
    """The record's memberships filtered to scope-3 + S&P 100, preserving order."""
    membership = rec.get("index_membership") or []
    if not isinstance(membership, list):
        return []
    return [m for m in membership if m in KEEP_INDICES]


# ---------------------------------------------------------------------------
# Yahoo OHLCV fetch (same v8 chart endpoint money_flow's self-test used; works
# without a crumb). One ticker at a time; called from the thread pool.
# ---------------------------------------------------------------------------

def _fetch_ohlcv(ticker: str) -> List[Dict[str, Any]]:
    """Fetch ~6mo daily OHLCV bars for one ticker from Yahoo, or ``[]`` on failure.

    A small random jitter is added before the request to avoid hammering Yahoo
    when many workers fire at once.
    """
    time.sleep(random.uniform(_JITTER_MIN, _JITTER_MAX))
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
        "?range=6mo&interval=1d"
    )
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})

    payload = None
    for attempt in range(_MAX_RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT) as resp:
                payload = json.load(resp)
            break
        except urllib.error.HTTPError as exc:
            # 429 (rate limit) / 5xx are transient: back off and retry. Other
            # HTTP errors (404 delisted, etc.) are permanent -> give up.
            if exc.code in (429, 500, 502, 503, 504) and attempt < _MAX_RETRIES - 1:
                time.sleep((2.0 ** attempt) + random.uniform(0.5, 1.5))
                continue
            return []
        except Exception:  # noqa: BLE001 - URLError/timeout/parse -> skip ticker
            return []
    if payload is None:
        return []
    try:
        result = payload["chart"]["result"][0]
        timestamps = result.get("timestamp") or []
        quote = result["indicators"]["quote"][0]
        o, h, l, c, v = (
            quote.get(k) or [] for k in ("open", "high", "low", "close", "volume")
        )
    except (KeyError, IndexError, TypeError):
        return []
    bars: List[Dict[str, Any]] = []
    for i, t in enumerate(timestamps):
        if t is None or i >= len(c) or c[i] is None:
            continue
        bars.append({
            "date": datetime.fromtimestamp(int(t), tz=timezone.utc).strftime("%Y-%m-%d"),
            "open": o[i] if i < len(o) else None,
            "high": h[i] if i < len(h) else None,
            "low": l[i] if i < len(l) else None,
            "close": c[i],
            "volume": v[i] if i < len(v) else 0,
        })
    return bars


# ---------------------------------------------------------------------------
# Per-stock scoring
# ---------------------------------------------------------------------------

def _score_stock(rec: Dict[str, Any], bars: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Score one stock from its OHLCV bars, or ``None`` when unscoreable.

    ``None`` is returned when neither MFI nor CMF can be computed (insufficient
    bars / bad data) — the caller counts that as a failure rather than emitting a
    misleading neutral.
    """
    m = mfi(bars, 14)
    cm = cmf(bars, 20)

    if m is not None and cm is not None:
        raw = 0.6 * ((m - 50.0) * 2.0) + 0.4 * (cm * 200.0)
    elif m is not None:
        raw = (m - 50.0) * 2.0
    else:
        # Neither indicator available -> not scoreable.
        return None

    score = float(_clip(round(raw, 1), -100.0, 100.0))

    return {
        "symbol": rec.get("ticker"),
        "name": rec.get("name"),
        "score": score,
        "label": _band_label(score),
        "mfi": round(m, 2) if m is not None else None,
        "cmf": round(cm, 4) if cm is not None else None,
        "indices": _scope_indices(rec),
        "sector": rec.get("sector"),
    }


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def build_stock_money_flow(limit: Optional[int] = None, write: bool = True) -> Dict[str, Any]:
    """Build the per-stock money-flow payload for the Stock Flows subtab.

    Parameters
    ----------
    limit:
        Cap the number of in-scope tickers fetched/scored (fast testing). ``None``
        scores every in-scope name.
    write:
        When True, write the payload to ``data-stock-money-flow.json``.

    Returns
    -------
    dict
        ``{as_of, universe_count, scored_count, stocks:[...]}`` with ``stocks``
        sorted by ``score`` descending. Conforms to the shared data contract.
    """
    universe = _load_universe()
    universe_count = len(universe)

    in_scope = [rec for rec in universe if _in_scope(rec) and rec.get("ticker")]
    if limit is not None:
        in_scope = in_scope[:limit]

    # Fetch all in-scope tickers concurrently (polite: 8 workers + jitter + UA).
    bars_by_ticker: Dict[str, List[Dict[str, Any]]] = {}
    if in_scope:
        with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
            futures = {
                pool.submit(_fetch_ohlcv, rec["ticker"]): rec["ticker"]
                for rec in in_scope
            }
            for fut in as_completed(futures):
                ticker = futures[fut]
                try:
                    bars_by_ticker[ticker] = fut.result()
                except Exception:  # noqa: BLE001 - defensive; treat as empty
                    bars_by_ticker[ticker] = []

    stocks: List[Dict[str, Any]] = []
    fetch_fail = 0
    score_fail = 0
    for rec in in_scope:
        bars = bars_by_ticker.get(rec["ticker"]) or []
        if not bars:
            fetch_fail += 1
            continue
        scored = _score_stock(rec, bars)
        if scored is None:
            score_fail += 1
            continue
        stocks.append(scored)

    stocks.sort(key=lambda s: s["score"], reverse=True)

    payload: Dict[str, Any] = {
        "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "universe_count": universe_count,
        "scored_count": len(stocks),
        "stocks": stocks,
    }

    if write:
        try:
            with open(OUTPUT_PATH, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
                fh.write("\n")
        except OSError:
            pass

    return payload


def main() -> None:
    """CLI entrypoint: build the full per-stock payload and write the sidecar."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Build data-stock-money-flow.json (per-stock money-flow scores)."
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Cap the number of in-scope tickers (fast testing).",
    )
    parser.add_argument(
        "--no-write", action="store_true",
        help="Compute only; do not write the sidecar JSON.",
    )
    args = parser.parse_args()

    started = time.time()
    payload = build_stock_money_flow(limit=args.limit, write=not args.no_write)
    elapsed = time.time() - started

    print(
        f"Stock Flows: scored {payload['scored_count']} of "
        f"{payload['universe_count']} universe names "
        f"(in-scope fetched, {elapsed:.1f}s)."
    )
    if not args.no_write:
        print(f"Wrote {OUTPUT_PATH}")
    for s in payload["stocks"][:5]:
        print(f"  {s['symbol']:6s} {s['score']:6.1f}  {s['label']}")


if __name__ == "__main__":
    main()
