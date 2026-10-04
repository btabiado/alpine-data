"""Registry of LTHCS methodology breaks.

A methodology break is a date on which scores moved because the MODEL or its
INPUT COVERAGE changed, not because the companies did. Score changes that
straddle such a date are not comparable and must not be presented as a
market signal (movers, drift, "N tickers shifted band overnight").

2026-10-04 is the first recorded break: SEC EDGAR data was restored to the
financial pillar (its cross-sectional standard deviation went from 0 to ~17
overnight), 73 tickers moved more than 10 points and 98 changed band in one
day. Nothing about those companies changed that day.

Consumers:

* ``LthcsPersist.read_prior_scores(..., breaks=...)`` anchors drift windows
  at the break instead of across it, so drift after a break measures change
  under one methodology only (on the break day itself drift reads 0).
* The daily snapshot carries ``methodology_breaks`` (recent entries) so the
  UI and any reader can annotate.
* ``app.compute_lthcs_insights`` / ``build_lthcs_payload`` relabel the
  band-shift and composite-delta insights and annotate movers whose window
  spans a break.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Dict, List, Optional

METHODOLOGY_BREAKS: List[Dict[str, Any]] = [
    {
        "date": "2026-10-04",
        "pillars": ["financial_evolution"],
        "summary": "SEC financial data restored",
        "detail": (
            "SEC EDGAR fundamentals were restored to the Financial Evolution "
            "pillar (cross-sectional sd 0 -> ~17). Score and band changes "
            "between 2026-10-03 and 2026-10-04 reflect the data restoration, "
            "not a change in the companies; drift windows are re-anchored at "
            "this date."
        ),
    },
]


def _d(value: Any) -> Optional[date]:
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def break_dates() -> List[str]:
    return sorted(b["date"] for b in METHODOLOGY_BREAKS)


def breaks_between(start_exclusive: Any, end_inclusive: Any) -> List[Dict[str, Any]]:
    """Breaks with ``start < date <= end`` — i.e. a change measured from
    ``start`` to ``end`` straddles them."""
    lo, hi = _d(start_exclusive), _d(end_inclusive)
    if lo is None or hi is None:
        return []
    return [b for b in METHODOLOGY_BREAKS if lo < _d(b["date"]) <= hi]


def recent_breaks(as_of: Any, days: int = 90) -> List[Dict[str, Any]]:
    """Breaks within the last ``days`` days of ``as_of`` (inclusive) — the
    ones that can still sit inside a drift window."""
    end = _d(as_of)
    if end is None:
        return []
    return breaks_between(end - timedelta(days=days + 1), end)


__all__ = ["METHODOLOGY_BREAKS", "break_dates", "breaks_between", "recent_breaks"]
