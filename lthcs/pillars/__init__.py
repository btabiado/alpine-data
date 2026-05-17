"""LTHCS pillar scorers.

Each module in this package implements one of the five pillars from
``PHASE_1_BUILD_SPEC.md`` and exposes a ``compute_<pillar>(...)`` entry
point that returns a 0-100 sub-score dict.

Phase 1 pillars:
- adoption       — Adoption Momentum (revenue growth + Google Trends slope)
- institutional  — Institutional Confidence (90d momentum + 13F stub)
- financial      — Financial Evolution (rev growth + gross margin trend + OCF)
- thesis         — Thesis Integrity (Alpha Vantage news sentiment)
- des            — Demand Environment Score (FRED + EIA macro with sector tilts)
"""

from __future__ import annotations

from lthcs.pillars import adoption, des, financial, institutional, thesis

__all__ = ["adoption", "des", "financial", "institutional", "thesis"]
