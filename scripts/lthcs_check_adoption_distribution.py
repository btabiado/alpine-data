"""Week 3 distribution check: compute revenue growth + adoption sub-score for all 75 universe tickers.

Pulls revenue history from live SEC EDGAR (cached 7d, so re-runs are free).
Skips Google Trends (too slow / rate-limited for 75 tickers in V1) — uses
empty trends series so trends_subscore lands at neutral 50.0 for everyone.

Once Google Trends is exercised in the daily pipeline (Week 6+), the 40%
trends component will add real signal. For now this validates the 60% revenue
percentile end-to-end against real data.

Usage:
    python scripts/lthcs_check_adoption_distribution.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env")
except ImportError:
    pass

from lthcs.pillars import adoption
from lthcs.sources import sec_edgar

UNIVERSE_PATH = REPO_ROOT / "data" / "lthcs" / "universe.json"


def main() -> int:
    universe = json.loads(UNIVERSE_PATH.read_text())
    tickers = [t["ticker"] for t in universe["tickers"] if t.get("active", True)]
    print(f"Pulling revenue history for {len(tickers)} tickers from SEC EDGAR...")
    t0 = time.time()

    growths: dict[str, float | None] = {}
    failed: list[tuple[str, str]] = []
    for i, ticker in enumerate(tickers, 1):
        try:
            rows = sec_edgar.get_revenue_history(ticker)
            growths[ticker] = adoption.compute_revenue_growth_yoy(rows)
        except Exception as exc:  # noqa: BLE001
            growths[ticker] = None
            failed.append((ticker, f"{type(exc).__name__}: {exc}"))
        if i % 10 == 0:
            print(f"  ...{i}/{len(tickers)} ({time.time() - t0:.1f}s elapsed)")

    elapsed = time.time() - t0
    have_growth = sum(1 for v in growths.values() if v is not None)
    print(
        f"\nFetched in {elapsed:.1f}s — {have_growth}/{len(tickers)} have computable YoY growth"
    )
    if failed:
        print(f"\n{len(failed)} fetch failures:")
        for ticker, msg in failed[:10]:
            print(f"  {ticker}: {msg}")
        if len(failed) > 10:
            print(f"  ...and {len(failed) - 10} more")

    # Compute adoption sub-score per ticker (trends empty -> trends_subscore=50)
    results = []
    for ticker in tickers:
        rows = []
        try:
            rows = sec_edgar.get_revenue_history(ticker)
        except Exception as e:
            print(f"  [check] sec_edgar.get_revenue_history({ticker}) suppressed: {type(e).__name__}", file=sys.stderr)
        result = adoption.compute_adoption(
            ticker=ticker,
            revenue_rows=rows,
            interest_series=[],  # neutral; tested by Week 6 end-to-end
            peer_growths=growths,
        )
        results.append(result)

    results.sort(key=lambda r: r["sub_score"], reverse=True)

    print("\n=== Top 15 by adoption sub-score (revenue 60% + trends 50.0 neutral 40%) ===")
    print(f"{'rank':>4}  {'ticker':<6}  {'sub_score':>9}  {'rev_growth':>11}  {'rev_subscore':>13}")
    for i, r in enumerate(results[:15], 1):
        rev_growth = r["components"]["revenue_growth_yoy"]
        rev_growth_str = f"{rev_growth*100:+.1f}%" if rev_growth is not None else "  n/a"
        print(
            f"{i:>4}  {r['ticker']:<6}  {r['sub_score']:>9.1f}  "
            f"{rev_growth_str:>11}  {r['components']['revenue_subscore']:>13.1f}"
        )

    print("\n=== Bottom 15 ===")
    for i, r in enumerate(results[-15:], len(results) - 14):
        rev_growth = r["components"]["revenue_growth_yoy"]
        rev_growth_str = f"{rev_growth*100:+.1f}%" if rev_growth is not None else "  n/a"
        print(
            f"{i:>4}  {r['ticker']:<6}  {r['sub_score']:>9.1f}  "
            f"{rev_growth_str:>11}  {r['components']['revenue_subscore']:>13.1f}"
        )

    print("\n=== Sub-score distribution ===")
    scores = [r["sub_score"] for r in results]
    print(f"  min  {min(scores):.1f}")
    print(f"  q25  {sorted(scores)[len(scores)//4]:.1f}")
    print(f"  med  {sorted(scores)[len(scores)//2]:.1f}")
    print(f"  q75  {sorted(scores)[3*len(scores)//4]:.1f}")
    print(f"  max  {max(scores):.1f}")

    print("\n=== Three teaching cases (spec §11 expectation) ===")
    for ticker in ["AAPL", "LCID", "INTC"]:
        match = next((r for r in results if r["ticker"] == ticker), None)
        if match:
            rev = match["components"]["revenue_growth_yoy"]
            rev_str = f"{rev*100:+.1f}%" if rev is not None else "n/a"
            print(
                f"  {ticker}: sub_score {match['sub_score']:.1f}, "
                f"rev_growth {rev_str}, rev_subscore {match['components']['revenue_subscore']:.1f}"
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
