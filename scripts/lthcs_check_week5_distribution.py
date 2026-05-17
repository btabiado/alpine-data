"""Week 5 distribution check: Thesis Integrity + DES (Demand Environment Score)
across all active universe tickers.

Uses:
- Cached FRED/EIA history (no fresh fetch unless TTL expired)
- ONE live Alpha Vantage NEWS_SENTIMENT batched call (~1 of 25 daily tokens)
- Cached SEC EDGAR sector mapping (none needed — sector comes from universe.json)

Usage:
    python scripts/lthcs_check_week5_distribution.py
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

from lthcs.pillars import des, thesis
from lthcs.sources import alpha_vantage, eia, fred

UNIVERSE_PATH = REPO_ROOT / "data" / "lthcs" / "universe.json"
SECTOR_WEIGHTS_PATH = REPO_ROOT / "data" / "lthcs" / "sector_des_weights.json"


def _yoy_change_pct(series: list, recent_date: str | None = None) -> float | None:
    """Given a sorted-ascending FRED series, compute YoY % change of most recent value."""
    if len(series) < 13:
        return None
    latest = series[-1]
    if latest["value"] is None:
        return None
    # Try same month one year ago.
    for prior in reversed(series[:-1]):
        if prior["value"] is None:
            continue
        try:
            d_now = datetime.fromisoformat(latest["date"])
            d_then = datetime.fromisoformat(prior["date"])
            delta_days = (d_now - d_then).days
            if 350 <= delta_days <= 380:
                return (latest["value"] / prior["value"] - 1.0) * 100.0
        except (ValueError, ZeroDivisionError):
            continue
    return None


def _bp_change_30d(series: list) -> float | None:
    """Given a sorted-ascending FRED daily yield series, compute 30-day change in basis points."""
    if len(series) < 25:
        return None
    latest = series[-1]
    if latest["value"] is None:
        return None
    try:
        d_now = datetime.fromisoformat(latest["date"])
    except ValueError:
        return None
    # Find a value 25-35 days ago.
    for prior in reversed(series[:-1]):
        if prior["value"] is None:
            continue
        try:
            d_then = datetime.fromisoformat(prior["date"])
        except ValueError:
            continue
        delta = (d_now - d_then).days
        if 25 <= delta <= 35:
            return (latest["value"] - prior["value"]) * 100.0  # pct -> bp
    return None


def build_macro_inputs() -> dict:
    """Pull FRED + EIA from cache and compute the macro_inputs dict for DES."""
    cpi_series = fred.get_series("CPIAUCSL")
    ten_y_series = fred.get_series("DGS10")
    ff = fred.get_latest_value("FEDFUNDS")
    unrate = fred.get_latest_value("UNRATE")
    wti = eia.get_latest_value("wti")

    macro = {
        "cpi_yoy_pct": _yoy_change_pct(cpi_series),
        "fed_funds_pct": ff["value"] if ff else None,
        "ten_y_yield_pct": ten_y_series[-1]["value"] if ten_y_series else None,
        "ten_y_30d_change_bp": _bp_change_30d(ten_y_series),
        "unemployment_pct": unrate["value"] if unrate else None,
        "wti_oil_usd": wti["value"] if wti else None,
    }
    return macro


def main() -> int:
    universe = json.loads(UNIVERSE_PATH.read_text())
    sector_weights = json.loads(SECTOR_WEIGHTS_PATH.read_text())
    active = [t for t in universe["tickers"] if t.get("active", True)]
    tickers = [t["ticker"] for t in active]
    sector_by_ticker = {t["ticker"]: t["sector"] for t in active}

    print(f"Active universe: {len(tickers)} tickers")

    # Macro inputs
    macro = build_macro_inputs()
    print("\n=== Macro inputs (driving DES) ===")
    for k, v in macro.items():
        print(f"  {k:<22}  {v}")

    # IMPORTANT V1 LIMITATION:
    # AV's NEWS_SENTIMENT treats multi-ticker as an AND filter (articles must
    # mention ALL tickers simultaneously), not OR. So a batched call for all
    # 74 tickers returns 0 results. Per-ticker calls would burn 74 of 25 daily
    # tokens. For V1, we sample ONE high-coverage ticker (AAPL — likely already
    # in cache from earlier smoke test) to confirm the Thesis pillar works
    # end-to-end on real data. Other tickers get neutral 50 in the daily
    # pipeline until we upgrade AV plan or switch news sources.
    print("\n=== Thesis Integrity sample (1 ticker; AV free-tier limitation, see commit msg) ===")
    sample_ticker = "AAPL"
    try:
        av_response = alpha_vantage.get_news_sentiment([sample_ticker], limit=50)
        feed_count = len(av_response.get("feed", []))
        sample = thesis.compute_thesis(sample_ticker, av_response)
        print(
            f"  {sample_ticker}: sub_score {sample['sub_score']:.1f}  "
            f"(articles {sample['components']['article_count']}, "
            f"mean_sent {sample['components']['mean_sentiment_score']})  "
            f"[AV returned {feed_count} articles]"
        )
        print("  → confirms the pillar code path is working end-to-end on real AV data.")
    except Exception as exc:  # noqa: BLE001
        print(f"  {sample_ticker} thesis check failed: {type(exc).__name__}: {exc}")
        av_response = None  # will skip thesis section below

    # Compute per-ticker DES (cheap, all from cached macro inputs)
    des_results = []
    for ticker in tickers:
        des_results.append(
            des.compute_des(
                ticker=ticker,
                sector=sector_by_ticker[ticker],
                macro_inputs=macro,
                sector_weights=sector_weights,
            )
        )
    des_results.sort(key=lambda r: r["sub_score"], reverse=True)

    def show_table(label, results, extra_fmt):
        print(f"\n========== {label} ==========")
        print("--- top 10 ---")
        for i, r in enumerate(results[:10], 1):
            print(extra_fmt(i, r))
        print("--- bottom 10 ---")
        for i, r in enumerate(results[-10:], len(results) - 9):
            print(extra_fmt(i, r))

    show_table(
        "DES (Demand Environment Score)",
        des_results,
        lambda i, r: (
            f"  {i:>3}  {r['ticker']:<6}  sub_score {r['sub_score']:>5.1f}  "
            f"sector={r['sector']:<25}  "
            f"overrides={r['components']['applied_overrides']}"
        ),
    )

    print("\n========== Distribution stats ==========")
    scores = sorted(r["sub_score"] for r in des_results)
    print(
        f"  DES     min {scores[0]:>5.1f}  q25 {scores[len(scores)//4]:>5.1f}  "
        f"med {scores[len(scores)//2]:>5.1f}  q75 {scores[3*len(scores)//4]:>5.1f}  max {scores[-1]:>5.1f}"
    )

    print("\n========== Spec §11 Week 5 assertions ==========")
    by_ticker_des = {r["ticker"]: r for r in des_results}
    expectations = [
        ("TSLA", "EV — should be oil-sensitive positive via override", "wti_oil_usd"),
        ("LCID", "Pure EV — even more oil-sensitive positive", "wti_oil_usd"),
        ("JPM", "Bank — should be rate-sensitive positive", "ten_y_yield_pct"),
        ("BAC", "Bank — should be rate-sensitive positive", "ten_y_yield_pct"),
        ("XOM", "Energy — should be oil-sensitive positive (baseline)", "wti_oil_usd"),
        ("CVX", "Energy — should be oil-sensitive positive", "wti_oil_usd"),
    ]
    for ticker, note, signal in expectations:
        r = by_ticker_des.get(ticker)
        if r is None:
            print(f"  {ticker}: not in active universe")
            continue
        contrib = r["components"]["signal_contributions"].get(signal, 0.0)
        sign_ok = "✓" if contrib > 0 else "✗"
        print(
            f"  {ticker:<5}  DES {r['sub_score']:>5.1f}  "
            f"{signal} contribution {contrib:+.3f} {sign_ok}  ({note})"
        )

    print("\n========== Teaching cases ==========")
    print("  (Run lthcs_status_report.py for Adoption + Institutional + Financial sub-scores.)")
    for ticker in ["AAPL", "LCID", "INTC", "TSLA", "JPM", "XOM"]:
        d = by_ticker_des.get(ticker)
        if d:
            overrides = d["components"]["applied_overrides"]
            override_str = f" [overrides: {overrides}]" if overrides else ""
            print(
                f"  {ticker:<5}  DES {d['sub_score']:>5.1f}  "
                f"(sector={d['sector']}){override_str}"
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
