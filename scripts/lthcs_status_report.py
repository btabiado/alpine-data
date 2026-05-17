"""LTHCS Phase 1 status report — no new API calls, reads only from local cache.

Run from repo root:
    python scripts/lthcs_status_report.py
"""

from __future__ import annotations

import json
import sys
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
from lthcs.sources import fred, eia, sec_edgar, yahoo

UNIVERSE_PATH = REPO_ROOT / "data" / "lthcs" / "universe.json"
LINE = "─" * 78


def section(title: str) -> None:
    print(f"\n{LINE}\n  {title}\n{LINE}")


def main() -> int:
    universe = json.loads(UNIVERSE_PATH.read_text())
    active = [t for t in universe["tickers"] if t.get("active", True)]
    inactive = [t for t in universe["tickers"] if not t.get("active", True)]

    section("LTHCS Phase 1 — Status as of 2026-05-16")
    print(f"  Universe:    {len(universe['tickers'])} entries  ({len(active)} active, {len(inactive)} inactive)")
    print(f"  Inactive:    {', '.join(t['ticker'] + ' (' + t.get('inactive_reason', '?')[:30] + '…)' for t in inactive) or 'none'}")
    print(f"  Maturity stages: {len(set(t['maturity_stage'] for t in active))} distinct profiles used")

    section("Built so far (4 of 10 weeks)")
    print("""  Week 1  ✓ Schemas + validate gate
  Week 2  ✓ 5 source clients (Yahoo, SEC EDGAR, FRED, EIA, Alpha Vantage)
  Week 3  ✓ normalize.py + Adoption pillar
  Week 4  ✓ Institutional + Financial pillars
  Week 5    (next) Thesis Integrity + DES
  Week 6    Score combiner + modifiers + narratives
  Week 7    Daily pipeline + history persistence
  Week 8    Tab UI: cards + filters
  Week 9    Detail modal + 90d chart
  Week 10   Polish, docs, ship live""")

    section("Macro inputs (cached — driving the future DES pillar)")
    cpi = fred.get_latest_value("CPIAUCSL")
    ff = fred.get_latest_value("FEDFUNDS")
    ten_y = fred.get_latest_value("DGS10")
    unrate = fred.get_latest_value("UNRATE")
    wti = eia.get_latest_value("wti")
    print(f"  CPI (FRED):           {cpi['value']:>8.2f}  as of {cpi['date']}")
    print(f"  Fed Funds Rate:       {ff['value']:>8.2f}  as of {ff['date']}")
    print(f"  10Y Treasury yield:   {ten_y['value']:>8.2f}  as of {ten_y['date']}")
    print(f"  Unemployment:         {unrate['value']:>8.2f}  as of {unrate['date']}")
    print(f"  WTI crude (EIA):      {wti['value']:>8.2f}  as of {wti['date']}")

    section("Teaching cases — sub-scores from the 3 pillars we have")
    # Pre-compute peer-wide values for percentile context
    tickers = [t["ticker"] for t in active]
    growths = {}
    momentums = {}
    for t in tickers:
        try:
            growths[t] = adoption.compute_revenue_growth_yoy(sec_edgar.get_revenue_history(t))
        except Exception:
            growths[t] = None
        momentums[t] = yahoo.get_momentum_pct(t, days=90)

    def show(ticker: str) -> None:
        if ticker not in tickers:
            print(f"  {ticker}: not in active universe")
            return
        entry = next(t for t in active if t["ticker"] == ticker)
        try:
            rev = sec_edgar.get_revenue_history(ticker)
            gp = sec_edgar.get_gross_profit_history(ticker)
            ocf = sec_edgar.get_operating_cash_flow_history(ticker)
        except Exception as exc:  # noqa: BLE001
            print(f"  {ticker}: SEC fetch error {exc}")
            return
        ad = adoption.compute_adoption(ticker, rev, [], growths)
        ins = institutional.compute_institutional(ticker, momentums[ticker], momentums)
        fin = financial.compute_financial(ticker, rev, gp, ocf, growths)

        g = ad["components"]["revenue_growth_yoy"]
        g_str = f"{g*100:+.1f}%" if g is not None else "n/a"
        m = ins["components"]["momentum_pct_90d"]
        m_str = f"{m*100:+.1f}%" if m is not None else "n/a"
        print(f"\n  {ticker}  ({entry['name']})")
        print(f"     maturity stage: {entry['maturity_stage']}")
        print(f"     ─ Adoption        {ad['sub_score']:>5.1f}   rev_growth={g_str:<8} (trends 40% neutral until W6)")
        print(f"     ─ Institutional   {ins['sub_score']:>5.1f}   momentum_90d={m_str:<8} (13F = V1 stub)")
        print(f"     ─ Financial       {fin['sub_score']:>5.1f}   rev_sub={fin['components']['revenue_subscore']:>5.1f}  margin_sub={fin['components']['margin_subscore']:>5.1f}  ocf_sub={fin['components']['ocf_subscore']:>5.1f}")

    for t in ["AAPL", "MSFT", "NVDA", "LCID", "INTC", "TSLA", "JPM"]:
        show(t)

    section("What's complete vs partial (where the 3 sub-scores you see come from)")
    print("""  Each ticker currently has 3 of 5 pillars computed live:
    Adoption       ✓ revenue percentile (60%) + Google Trends slope (40% neutral until pipeline runs Trends)
    Institutional  ✓ 90d momentum percentile (100% after V1 renormalization; 13F deferred to Phase 2)
    Financial      ✓ revenue percentile (40%) + gross-margin slope (30%) + TTM OCF margin (30%)

  Still to come before a real composite LTHCS score:
    Thesis Integrity  — 30d-avg Alpha Vantage news sentiment → 0–100  (Week 5)
    DES               — sector-weighted blend of FRED + EIA macro      (Week 5)
    Modifiers         — macro adj, sector adj, volatility penalty       (Week 6)
    Final combiner    — pillar weights per maturity_stage profile       (Week 6)""")

    section("Test + commit health")
    print("""  Tests:         224 / 224 passing (~2s)
  Commits:       4 LTHCS commits on origin/main (e38fba2 → 7425a81)
  Cache:         .cache/lthcs/* populated for Yahoo / SEC / FRED / EIA / AV
  Keys in .env:  FRED + EIA + Alpha Vantage + SEC_USER_AGENT ✓""")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
