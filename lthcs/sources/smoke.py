"""Smoke test for source clients: `python -m lthcs.sources.smoke <TICKER>`.

Hits each upstream once and prints a one-line health summary. Skips any
source whose API key is unset (with a clear note) so partial setups still
get useful signal.

This is the only place in the codebase that makes live network calls
during development. Everything else uses mocked HTTP in tests.
"""

from __future__ import annotations

import argparse
import os
import sys
import traceback
from typing import Callable, List, Tuple

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from lthcs.sources import alpha_vantage, eia, fred, sec_edgar, yahoo

OK = "✓"
SKIP = "—"
FAIL = "✗"


def _check_yahoo(ticker: str) -> Tuple[str, str]:
    prices = yahoo.get_daily_prices(ticker, period="5d")
    if not prices:
        return FAIL, "no prices returned"
    last = prices[-1]
    return OK, f"{len(prices)} bars, last close {last['close']:.2f} on {last['date']}"


def _check_sec_edgar(ticker: str) -> Tuple[str, str]:
    if not os.environ.get("SEC_USER_AGENT", "").strip():
        return SKIP, "SEC_USER_AGENT not set"
    cik = sec_edgar.get_cik(ticker)
    if cik is None:
        return FAIL, f"no CIK for {ticker}"
    rev = sec_edgar.get_revenue_history(ticker)
    return OK, f"CIK {cik}, {len(rev)} revenue rows"


def _check_fred() -> Tuple[str, str]:
    if not os.environ.get("FRED_API_KEY", "").strip():
        return SKIP, "FRED_API_KEY not set"
    cpi = fred.get_latest_value("CPIAUCSL")
    if cpi is None:
        return FAIL, "no CPI observation"
    return OK, f"CPI {cpi['value']:.2f} on {cpi['date']}"


def _check_eia() -> Tuple[str, str]:
    if not os.environ.get("EIA_API_KEY", "").strip():
        return SKIP, "EIA_API_KEY not set"
    wti = eia.get_latest_value("wti")
    if wti is None:
        return FAIL, "no WTI observation"
    return OK, f"WTI {wti['value']:.2f} on {wti['date']}"


def _check_alpha_vantage(ticker: str) -> Tuple[str, str]:
    if not os.environ.get("ALPHA_VANTAGE_API_KEY", "").strip():
        return SKIP, "ALPHA_VANTAGE_API_KEY not set"
    resp = alpha_vantage.get_news_sentiment([ticker], limit=5)
    summary = alpha_vantage.parse_ticker_sentiment(resp, ticker)
    return OK, (
        f"{summary['article_count']} articles, "
        f"mean sentiment "
        f"{summary['mean_sentiment_score']!r}"
    )


def _run_check(label: str, fn: Callable[[], Tuple[str, str]]) -> bool:
    try:
        marker, detail = fn()
    except Exception as exc:  # noqa: BLE001
        print(f"{FAIL} {label:<14} {type(exc).__name__}: {exc}")
        traceback.print_exc(limit=1, file=sys.stdout)
        return False
    print(f"{marker} {label:<14} {detail}")
    return marker != FAIL


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m lthcs.sources.smoke",
        description="Hit each source once and report. Skips sources without keys.",
    )
    parser.add_argument(
        "ticker",
        nargs="?",
        default="AAPL",
        help="Ticker to probe (default: AAPL)",
    )
    args = parser.parse_args(argv)
    ticker = args.ticker.upper()

    print(f"LTHCS source smoke test — ticker {ticker}\n")
    results = [
        _run_check("yahoo", lambda: _check_yahoo(ticker)),
        _run_check("sec_edgar", lambda: _check_sec_edgar(ticker)),
        _run_check("fred", _check_fred),
        _run_check("eia", _check_eia),
        _run_check("alpha_vantage", lambda: _check_alpha_vantage(ticker)),
    ]
    print()
    if all(results):
        print(f"{OK} all source checks ok (skips counted as ok)")
        return 0
    print(f"{FAIL} at least one source check failed")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
