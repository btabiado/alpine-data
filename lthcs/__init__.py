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
#
# v1.1.1 — 2026-10-05 (PATCH: bug fix, no restatement; docs/lthcs-tuning-kit.md):
#   - Institutional Confidence is clamped to [0, 100]. Its insider + 13F
#     adjustment ([-7, +12]) was added to a [0, 100] base without a clamp, so
#     snapshots up to v1.1.0 carry values outside the scale: 292 of them in
#     data/lthcs/snapshots/2026-05-18..2026-10-05 (min ON -5.1, max PANW
#     107.0; FTNT 100.8 on 10-04/10-05). Those snapshots stay as published;
#     filter on model_version >= v1.1.1 to exclude them. Composites were
#     already clamped, so no published lthcs_score is out of range.
#   - The crypto pipeline shares this constant; nothing in it changed.
__version__ = "1.1.1"
MODEL_VERSION = f"v{__version__}"
