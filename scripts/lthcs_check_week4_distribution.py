"""Week 4 distribution check: compute Institutional Confidence + Financial Evolution
sub-scores for all active universe tickers using live SEC EDGAR + live Yahoo data.

Re-runs against the cache that the Week 3 check already populated, so SEC pulls
are essentially free on the second invocation. Yahoo prices may take ~30s for
75 tickers on first run (1 req/sec polite limit + 24h cache).

Usage:
    python scripts/lthcs_check_week4_distribution.py
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

from lthcs.pillars import adoption, financial, institutional
from lthcs.sources import sec_edgar, yahoo

UNIVERSE_PATH = REPO_ROOT / "data" / "lthcs" / "universe.json"


def _safe_call(label: str, fn):
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001
        print(f"  ! {label} failed: {type(exc).__name__}: {exc}")
        return None


def main() -> int:
    universe = json.loads(UNIVERSE_PATH.read_text())
    tickers = [t["ticker"] for t in universe["tickers"] if t.get("active", True)]
    print(f"Active universe: {len(tickers)} tickers\n")

    print("[1/3] Pulling momentum from Yahoo for all tickers...")
    t0 = time.time()
    momentums: dict[str, float | None] = {}
    for i, ticker in enumerate(tickers, 1):
        momentums[ticker] = _safe_call(ticker, lambda t=ticker: yahoo.get_momentum_pct(t, days=90))
        if i % 15 == 0:
            print(f"  ...{i}/{len(tickers)} ({time.time() - t0:.1f}s)")
    have_mom = sum(1 for v in momentums.values() if v is not None)
    print(f"  done in {time.time() - t0:.1f}s — {have_mom}/{len(tickers)} have 90d momentum\n")

    print("[2/3] Pulling revenue + gross profit + OCF from SEC EDGAR...")
    t0 = time.time()
    rev_history: dict[str, list] = {}
    gp_history: dict[str, list] = {}
    ocf_history: dict[str, list] = {}
    for i, ticker in enumerate(tickers, 1):
        rev_history[ticker] = _safe_call(ticker, lambda t=ticker: sec_edgar.get_revenue_history(t)) or []
        gp_history[ticker] = _safe_call(ticker, lambda t=ticker: sec_edgar.get_gross_profit_history(t)) or []
        ocf_history[ticker] = _safe_call(ticker, lambda t=ticker: sec_edgar.get_operating_cash_flow_history(t)) or []
        if i % 15 == 0:
            print(f"  ...{i}/{len(tickers)} ({time.time() - t0:.1f}s)")
    print(f"  done in {time.time() - t0:.1f}s\n")

    growths = {t: adoption.compute_revenue_growth_yoy(rev_history[t]) for t in tickers}

    print("[3/3] Computing pillar sub-scores...")
    inst_results = []
    fin_results = []
    for ticker in tickers:
        inst_results.append(
            institutional.compute_institutional(
                ticker=ticker,
                momentum_pct=momentums[ticker],
                peer_momentums=momentums,
            )
        )
        fin_results.append(
            financial.compute_financial(
                ticker=ticker,
                revenue_rows=rev_history[ticker],
                gross_profit_rows=gp_history[ticker],
                ocf_rows=ocf_history[ticker],
                peer_growths=growths,
            )
        )

    inst_results.sort(key=lambda r: r["sub_score"], reverse=True)
    fin_results.sort(key=lambda r: r["sub_score"], reverse=True)

    print("\n========== Institutional Confidence ==========")
    print(f"{'rank':>4}  {'ticker':<6}  {'sub_score':>9}  {'mom_90d':>9}  {'mom_sub':>9}")
    print("--- top 10 ---")
    for i, r in enumerate(inst_results[:10], 1):
        m = r["components"]["momentum_pct_90d"]
        m_str = f"{m*100:+.1f}%" if m is not None else "  n/a"
        print(
            f"{i:>4}  {r['ticker']:<6}  {r['sub_score']:>9.1f}  "
            f"{m_str:>9}  {r['components']['momentum_subscore']:>9.1f}"
        )
    print("--- bottom 10 ---")
    for i, r in enumerate(inst_results[-10:], len(inst_results) - 9):
        m = r["components"]["momentum_pct_90d"]
        m_str = f"{m*100:+.1f}%" if m is not None else "  n/a"
        print(
            f"{i:>4}  {r['ticker']:<6}  {r['sub_score']:>9.1f}  "
            f"{m_str:>9}  {r['components']['momentum_subscore']:>9.1f}"
        )

    print("\n========== Financial Evolution ==========")
    print(f"{'rank':>4}  {'ticker':<6}  {'sub_score':>9}  {'rev':>7}  {'margin':>7}  {'ocf':>7}")
    print("--- top 10 ---")
    for i, r in enumerate(fin_results[:10], 1):
        c = r["components"]
        print(
            f"{i:>4}  {r['ticker']:<6}  {r['sub_score']:>9.1f}  "
            f"{c['revenue_subscore']:>7.1f}  {c['margin_subscore']:>7.1f}  {c['ocf_subscore']:>7.1f}"
        )
    print("--- bottom 10 ---")
    for i, r in enumerate(fin_results[-10:], len(fin_results) - 9):
        c = r["components"]
        print(
            f"{i:>4}  {r['ticker']:<6}  {r['sub_score']:>9.1f}  "
            f"{c['revenue_subscore']:>7.1f}  {c['margin_subscore']:>7.1f}  {c['ocf_subscore']:>7.1f}"
        )

    print("\n========== Distribution stats ==========")
    for label, results in [("Institutional", inst_results), ("Financial", fin_results)]:
        scores = sorted(r["sub_score"] for r in results)
        print(
            f"  {label:<13}  min {scores[0]:>5.1f}  q25 {scores[len(scores)//4]:>5.1f}  "
            f"med {scores[len(scores)//2]:>5.1f}  q75 {scores[3*len(scores)//4]:>5.1f}  max {scores[-1]:>5.1f}"
        )

    print("\n========== Three teaching cases (spec §11 expectation) ==========")
    inst_by_ticker = {r["ticker"]: r for r in inst_results}
    fin_by_ticker = {r["ticker"]: r for r in fin_results}
    for ticker in ["AAPL", "LCID", "INTC"]:
        i_r = inst_by_ticker.get(ticker)
        f_r = fin_by_ticker.get(ticker)
        if i_r and f_r:
            print(
                f"  {ticker:<5}  Institutional {i_r['sub_score']:>5.1f}  "
                f"|  Financial {f_r['sub_score']:>5.1f}  "
                f"(rev {f_r['components']['revenue_subscore']:>5.1f}, "
                f"margin {f_r['components']['margin_subscore']:>5.1f}, "
                f"ocf {f_r['components']['ocf_subscore']:>5.1f})"
            )

    # INTC FinEvol spec gate
    intc_fin = fin_by_ticker.get("INTC")
    if intc_fin:
        print(
            f"\n  INTC Financial Evolution sub_score: {intc_fin['sub_score']:.1f}  "
            f"({'WEAK (gate ✓)' if intc_fin['sub_score'] < 45 else 'NOT visibly weak (gate ?)'})"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
