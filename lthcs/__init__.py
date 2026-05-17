"""LTHCS — Long-Term Hold Confidence Score, Phase 1."""

from __future__ import annotations

# v1.1.0 — 2026-05-17:
#   - Added mature_compounder + growth_compounder maturity stages so peer-
#     relative percentiles benchmark like-for-like (AAPL among compounders,
#     NVDA among growth names) instead of conflating the two.
#   - Wired real_10y_yield_pct (FRED DFII10), vix_index (VIXCLS), m2_yoy_pct
#     (M2SL) into DES with per-sector sensitivities.
#   - Composite renormalizes away stubbed pillars (Thesis-unavailable) and
#     pillar internals renormalize away missing sub-components (Trends in
#     Adoption, GP/OCF in Financial for banks).
#   - assign_band gap fix (79.4 etc. now correctly assigned to constructive).
__version__ = "1.1.0"
MODEL_VERSION = f"v{__version__}"
