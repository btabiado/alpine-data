"""Week 6 end-to-end demo: full LTHCS scoring on AAPL, LCID, INTC.

Runs all 5 pillars + the score combiner + templated narratives on real,
cached data. No fresh API calls beyond what the Week-3/4/5 scripts already
populated (Yahoo prices, SEC EDGAR, FRED, EIA, one cached AAPL AV response).

Per Week 6 spec gate ("Done when"): produces three validated snapshot rows
with their band placements. Reports any deviation from spec §11 expected
bands and explains why.

Usage:
    python scripts/lthcs_check_week6_endtoend.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env")
except ImportError:
    pass

from lthcs import narratives, score
from lthcs.pillars import adoption, des, financial, institutional, thesis
from lthcs.sources import alpha_vantage, eia, fred, sec_edgar, yahoo

UNIVERSE_PATH = REPO_ROOT / "data" / "lthcs" / "universe.json"
WEIGHTS_PATH = REPO_ROOT / "data" / "lthcs" / "weights.json"
SECTOR_WEIGHTS_PATH = REPO_ROOT / "data" / "lthcs" / "sector_des_weights.json"

# Spec §11 Week 6 teaching cases.
DEMO_TICKERS = ["AAPL", "LCID", "INTC"]
SPEC_EXPECTED = {
    "AAPL": ("high_confidence", "High Confidence (80-89)"),
    "LCID": ("monitor", "Monitor or Weakening"),
    "INTC": ("review", "Review (0-49)"),
}


def _yoy_change_pct(series, _days=(350, 380)):
    if len(series) < 13:
        return None
    latest = series[-1]
    if latest["value"] is None:
        return None
    for prior in reversed(series[:-1]):
        if prior["value"] is None:
            continue
        try:
            delta = (datetime.fromisoformat(latest["date"]) - datetime.fromisoformat(prior["date"])).days
        except ValueError:
            continue
        if _days[0] <= delta <= _days[1]:
            try:
                return (latest["value"] / prior["value"] - 1.0) * 100.0
            except ZeroDivisionError:
                return None
    return None


def _bp_change_30d(series):
    if len(series) < 25:
        return None
    latest = series[-1]
    if latest["value"] is None:
        return None
    for prior in reversed(series[:-1]):
        if prior["value"] is None:
            continue
        try:
            d = (datetime.fromisoformat(latest["date"]) - datetime.fromisoformat(prior["date"])).days
        except ValueError:
            continue
        if 25 <= d <= 35:
            return (latest["value"] - prior["value"]) * 100.0
    return None


def build_macro_inputs():
    cpi_series = fred.get_series("CPIAUCSL")
    ten_y_series = fred.get_series("DGS10")
    ff = fred.get_latest_value("FEDFUNDS")
    unrate = fred.get_latest_value("UNRATE")
    wti = eia.get_latest_value("wti")
    return {
        "cpi_yoy_pct": _yoy_change_pct(cpi_series),
        "fed_funds_pct": ff["value"] if ff else None,
        "ten_y_yield_pct": ten_y_series[-1]["value"] if ten_y_series else None,
        "ten_y_30d_change_bp": _bp_change_30d(ten_y_series),
        "unemployment_pct": unrate["value"] if unrate else None,
        "wti_oil_usd": wti["value"] if wti else None,
    }


def main() -> int:
    universe = json.loads(UNIVERSE_PATH.read_text())
    weights_config = json.loads(WEIGHTS_PATH.read_text())
    sector_weights = json.loads(SECTOR_WEIGHTS_PATH.read_text())
    by_ticker = {t["ticker"]: t for t in universe["tickers"]}

    print("=" * 78)
    print("  LTHCS Week 6 end-to-end demo — full pipeline on 3 teaching cases")
    print("=" * 78)

    macro = build_macro_inputs()
    print(
        f"\nMacro inputs: CPI YoY {macro['cpi_yoy_pct']:.2f}%, Fed Funds "
        f"{macro['fed_funds_pct']:.2f}%, 10Y {macro['ten_y_yield_pct']:.2f}% "
        f"(30d Δ {macro['ten_y_30d_change_bp']:+.1f}bp), WTI ${macro['wti_oil_usd']:.2f}"
    )

    # Build universe-wide context for peer-relative pillar inputs (revenue growth, momentum, volatility).
    print("\nPulling universe data for peer-relative context...")
    universe_tickers = [t["ticker"] for t in universe["tickers"] if t.get("active", True)]
    peer_growths = {}
    peer_momentums = {}
    peer_vols = []
    for t in universe_tickers:
        try:
            rev = sec_edgar.get_revenue_history(t)
            peer_growths[t] = adoption.compute_revenue_growth_yoy(rev)
        except Exception:
            peer_growths[t] = None
        peer_momentums[t] = yahoo.get_momentum_pct(t, days=90)
        v = yahoo.get_volatility(t, window=30)
        if v is not None:
            peer_vols.append(v)
    print(f"  growths: {sum(1 for v in peer_growths.values() if v is not None)}/{len(universe_tickers)}")
    print(f"  momentums: {sum(1 for v in peer_momentums.values() if v is not None)}/{len(universe_tickers)}")
    print(f"  volatilities: {len(peer_vols)}/{len(universe_tickers)}")

    # Try to use real AV data for AAPL only (already cached from earlier).
    av_response = None
    try:
        av_response = alpha_vantage.get_news_sentiment(["AAPL"], limit=50)
    except Exception as exc:  # noqa: BLE001
        print(f"  AV fetch skipped: {type(exc).__name__}")

    rows = []
    for ticker in DEMO_TICKERS:
        entry = by_ticker[ticker]
        rev = sec_edgar.get_revenue_history(ticker)
        gp = sec_edgar.get_gross_profit_history(ticker)
        ocf = sec_edgar.get_operating_cash_flow_history(ticker)
        vol = yahoo.get_volatility(ticker, window=30)

        ad = adoption.compute_adoption(ticker, rev, [], peer_growths)
        ins = institutional.compute_institutional(ticker, peer_momentums[ticker], peer_momentums)
        fin = financial.compute_financial(ticker, rev, gp, ocf, peer_growths)
        # Thesis: real AV data only if ticker is AAPL (cached); otherwise neutral 50.
        if ticker == "AAPL" and av_response is not None:
            th = thesis.compute_thesis(ticker, av_response)
        else:
            th = {
                "ticker": ticker,
                "sub_score": 50.0,
                "components": {"article_count": 0, "mean_sentiment_score": None,
                               "mean_relevance_score": None, "label_counts": {}},
                "data_quality": {"has_sentiment": False, "article_count_sufficient": False},
            }
        de = des.compute_des(
            ticker=ticker, sector=entry["sector"], macro_inputs=macro,
            sector_weights=sector_weights,
        )

        pillar_subscores = {
            "adoption_momentum": ad["sub_score"],
            "institutional_confidence": ins["sub_score"],
            "financial_evolution": fin["sub_score"],
            "thesis_integrity": th["sub_score"],
            "des": de["sub_score"],
        }
        flags = []
        if not th["data_quality"].get("has_sentiment"):
            flags.append("thesis_unavailable")
        if not ad["data_quality"].get("has_trends"):
            flags.append("trends_unavailable")

        snapshot = score.compute_lthcs_score(
            ticker=ticker, sector=entry["sector"], maturity_stage=entry["maturity_stage"],
            pillar_subscores=pillar_subscores, weights_config=weights_config,
            ten_y_30d_change_bp=macro["ten_y_30d_change_bp"],
            ticker_volatility=vol, universe_volatilities=peer_vols,
            data_quality_flags=flags,
        )
        narr = narratives.generate_narratives(snapshot)
        rows.append((entry, snapshot, narr, pillar_subscores))

    # Print each
    for entry, snap, narr, subs in rows:
        ticker = entry["ticker"]
        print("\n" + "-" * 78)
        print(f"  {ticker}  ({entry['name']})")
        print("-" * 78)
        print(f"  sector: {entry['sector']} · maturity: {entry['maturity_stage']}")
        print(
            f"  weights: {[round(w, 2) for w in snap['weights_used']]}  (PILLAR_ORDER: "
            "adopt, instit, finev, thesis, des)"
        )
        print(f"\n  Sub-scores:")
        for k, v in subs.items():
            print(f"    {k:<28} {v:>5.1f}")
        print(f"\n  Weighted components:")
        labels = ["adoption", "institutional", "financial", "thesis", "des"]
        for label, w, comp in zip(labels, snap["weights_used"], snap["weighted_components"]):
            print(f"    {label:<14} {w:.2f} × {subs[score.PILLAR_ORDER[labels.index(label)]]:>5.1f} = {comp:>5.2f}")
        print(f"\n  Modifiers:")
        for k, v in snap["modifiers"].items():
            print(f"    {k:<14} {v:+.1f}")
        print(f"\n  >>> LTHCS = {snap['lthcs_score']:>5.1f}  →  BAND = {snap['band']}")
        print(f"      confidence: {snap['confidence_level']}   flags: {snap['data_quality_flags']}")

        # Spec expectation check
        expected_band, expected_label = SPEC_EXPECTED[ticker]
        match = (
            snap["band"] == expected_band
            or (ticker == "LCID" and snap["band"] in ("monitor", "weakening"))
        )
        marker = "✓" if match else "≠"
        print(f"\n  Spec §11 expected: {expected_label}   {marker}")

        print(f"\n  Narratives:")
        print(f"    today's take:     {narr['todays_take']}")
        print(f"    why changed:      {narr['why_changed']}")
        print(f"    why not to sell:  {narr['why_not_to_sell']}")
        print(f"    what would break: {narr['what_would_break']}")

    # Summary
    print("\n" + "=" * 78)
    print("  Spec §11 Week 6 gate summary")
    print("=" * 78)
    for entry, snap, narr, subs in rows:
        ticker = entry["ticker"]
        expected_band, expected_label = SPEC_EXPECTED[ticker]
        match = (
            snap["band"] == expected_band
            or (ticker == "LCID" and snap["band"] in ("monitor", "weakening"))
        )
        marker = "✓" if match else "≠"
        print(
            f"  {ticker:<5}  expected {expected_label:<30} got {snap['band']:<18} {marker}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
