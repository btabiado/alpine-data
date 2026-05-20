"""LTHCS position-sizing helper (paper-money mode, Phase 4).

Given a portfolio total ($), a list of tickers with their LTHCS composite
score + band, and a risk-profile selection, produce a suggested $-allocation
per ticker. The algorithm mirrors the JS implementation at
``lthcs_position/lthcs-position.js`` — both files MUST stay in sync.

This module is intentionally pure-Python with zero external dependencies so
it can be unit-tested in CI without touching the network or filesystem.

Algorithm summary
-----------------
1. Filter out Review-band tickers (weight 0.0 — they are skipped with a
   note rather than allocated).
2. For each remaining ticker, compute::

       raw_weight = composite_score * band_multiplier

   where ``band_multiplier`` is:

   * elite        1.5
   * high         1.2
   * constructive 1.0
   * monitor      0.7
   * weakening    0.3
   * review       0.0  (skipped before this step)

3. Normalise raw weights so they sum to 1.0 (a.k.a. 100 %).
4. Apply a per-position cap derived from the risk profile:

   * conservative 5 %
   * balanced     8 %
   * aggressive   12 %

   Any weight above the cap is clipped; the overflow is redistributed
   proportionally to the remaining (sub-cap) tickers. Iterate until either
   no overflow remains OR every ticker is at the cap (in which case the
   leftover stays as uninvested cash — this only happens when the cap is so
   tight the universe can't absorb the full bankroll).
5. Multiply the final normalised weights by ``total_dollars`` to get the
   per-ticker dollar allocation.

The output of :func:`suggest_allocations` is a dict with keys ``rows``,
``total_allocated``, ``total_skipped``, and ``cash_remaining``. ``rows`` is
a list of per-ticker dicts in the same order as the input list (Review-band
tickers included, marked as skipped) so the UI can render a stable table.

Edge cases
~~~~~~~~~~
* **Single ticker** — allocation is min(100 %, cap) of the total; the
  remainder is cash.
* **All Review-band** — every ticker is skipped; the whole bankroll is
  flagged as ``cash_remaining``.
* **Sum of raw weights is zero** — treated like all-skipped (e.g. every
  ticker has composite 0). Rare in practice but the math has to be safe.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Mapping, Sequence


# ---------------------------------------------------------------------------
# Constants — keep in sync with lthcs_position/lthcs-position.js
# ---------------------------------------------------------------------------

BAND_MULTIPLIERS: Mapping[str, float] = {
    "elite": 1.5,
    "high": 1.2,
    "high_confidence": 1.2,
    "constructive": 1.0,
    "monitor": 0.7,
    "weakening": 0.3,
    "review": 0.0,
}

RISK_PROFILES: Mapping[str, float] = {
    "conservative": 0.05,
    "balanced": 0.08,
    "aggressive": 0.12,
}

# Numerical tolerance for floating-point comparisons during cap
# redistribution. 1e-9 of the bankroll is 1/100th of a cent on a $100M
# portfolio — well below anything a user could meaningfully act on.
_EPS = 1e-9


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TickerInput:
    """Per-ticker input to the sizing algorithm.

    ``composite`` is the LTHCS composite score (typically 0–100). ``band``
    is the band label (case-insensitive). ``data_quality_flags`` is an
    optional list of strings; if non-empty the row will carry a "low
    confidence" note but is still allocated (the spec is explicit about
    not auto-excluding low-quality rows — let the user decide).
    """

    ticker: str
    composite: float
    band: str
    data_quality_flags: Sequence[str] = ()


def _normalize_band(band: str) -> str:
    """Map a band string to the canonical key used in ``BAND_MULTIPLIERS``."""
    return (band or "").strip().lower().replace("-", "_").replace(" ", "_")


def band_multiplier(band: str) -> float:
    """Return the multiplier for a band, defaulting to 0.0 (skipped)."""
    return BAND_MULTIPLIERS.get(_normalize_band(band), 0.0)


def per_position_cap(risk_profile: str) -> float:
    """Return the per-position fractional cap for the given risk profile."""
    key = (risk_profile or "").strip().lower()
    if key not in RISK_PROFILES:
        raise ValueError(
            f"unknown risk profile {risk_profile!r}; "
            f"expected one of {sorted(RISK_PROFILES)}"
        )
    return RISK_PROFILES[key]


def _apply_cap(weights: List[float], cap: float) -> List[float]:
    """Clip weights to ``cap`` and redistribute the overflow proportionally.

    Operates on a copy; iterates until no further redistribution is
    possible. Returns a new list. Weights are expected to be non-negative
    and (initially) sum to ~1.0 or less. The final sum is <= 1.0; the
    leftover (if any) is the caller's cash-remaining bucket.
    """
    w = list(weights)
    n = len(w)
    if n == 0:
        return w
    # If the cap can't physically accommodate the bankroll, the best we can
    # do is set every position to the cap and leave the rest as cash.
    if cap * n <= 1.0 + _EPS:
        return [min(cap, x) for x in w]

    # Iterative redistribution. Each pass: clip overcapped positions, take
    # the freed-up weight, and redistribute proportionally to positions
    # that still have headroom. Repeat until either no overflow is left
    # or no position has any headroom.
    locked = [False] * n
    for _ in range(64):  # bounded loop — practical convergence is <10
        overflow = 0.0
        for i in range(n):
            if locked[i]:
                continue
            if w[i] > cap + _EPS:
                overflow += w[i] - cap
                w[i] = cap
                locked[i] = True
        if overflow <= _EPS:
            break
        # Pool of receivers = unlocked tickers with sub-cap weights.
        recv_idx = [i for i in range(n) if not locked[i] and w[i] < cap - _EPS]
        if not recv_idx:
            break  # every remaining ticker is already at the cap
        recv_total = sum(w[i] for i in recv_idx)
        if recv_total <= _EPS:
            # All receivers are at zero; split the overflow evenly.
            share = overflow / len(recv_idx)
            for i in recv_idx:
                w[i] = min(cap, w[i] + share)
        else:
            for i in recv_idx:
                w[i] = min(cap, w[i] + overflow * (w[i] / recv_total))
    return w


def suggest_allocations(
    tickers: Iterable[TickerInput],
    total_dollars: float,
    risk_profile: str = "balanced",
) -> dict:
    """Compute suggested $-allocations across ``tickers``.

    Parameters
    ----------
    tickers:
        Iterable of :class:`TickerInput`. Order is preserved in the
        output rows.
    total_dollars:
        Bankroll to allocate. Must be > 0.
    risk_profile:
        One of ``"conservative"``, ``"balanced"``, ``"aggressive"``.

    Returns
    -------
    dict with keys:

    * ``rows`` — list of per-ticker dicts (ticker, band, composite,
      band_multiplier, raw_weight, capped_weight, dollars, skipped, note)
    * ``total_allocated`` — sum of allocated dollars
    * ``total_skipped`` — sum of dollars NOT allocated to Review-band rows
      (always 0 because Review rows get $0 and the bankroll flows to others
      via normalisation; kept for output symmetry)
    * ``cash_remaining`` — total_dollars - total_allocated (the spillover
      when the per-position cap can't absorb the whole bankroll)
    * ``risk_profile`` — echoed back for the renderer
    * ``per_position_cap`` — the cap fraction actually applied
    """
    if total_dollars <= 0:
        raise ValueError("total_dollars must be > 0")
    cap = per_position_cap(risk_profile)
    inputs = list(tickers)

    # 1. Tag each row with band multiplier + raw (band-weighted) score.
    raw_scores: List[float] = []
    eligible_idx: List[int] = []
    for i, t in enumerate(inputs):
        mult = band_multiplier(t.band)
        score = max(0.0, float(t.composite)) * mult
        raw_scores.append(score)
        if mult > 0 and score > 0:
            eligible_idx.append(i)

    # 2. Normalise eligible scores to sum to 1.0.
    total_raw = sum(raw_scores[i] for i in eligible_idx)
    if total_raw <= _EPS or not eligible_idx:
        # All-skipped (or all-zero) case — full bankroll stays in cash.
        rows = []
        for t in inputs:
            mult = band_multiplier(t.band)
            note = (
                "Excluded per Review band"
                if _normalize_band(t.band) == "review"
                else "No allocation (zero band-weighted score)"
            )
            rows.append(
                {
                    "ticker": t.ticker,
                    "band": t.band,
                    "composite": float(t.composite),
                    "band_multiplier": mult,
                    "raw_weight": 0.0,
                    "capped_weight": 0.0,
                    "dollars": 0.0,
                    "skipped": True,
                    "note": note,
                }
            )
        return {
            "rows": rows,
            "total_allocated": 0.0,
            "total_skipped": 0.0,
            "cash_remaining": float(total_dollars),
            "risk_profile": risk_profile.lower(),
            "per_position_cap": cap,
        }

    normalized = [0.0] * len(inputs)
    for i in eligible_idx:
        normalized[i] = raw_scores[i] / total_raw

    # 3. Cap + redistribute.
    eligible_weights = [normalized[i] for i in eligible_idx]
    capped = _apply_cap(eligible_weights, cap)
    final_weights = [0.0] * len(inputs)
    for idx, w in zip(eligible_idx, capped):
        final_weights[idx] = w

    # 4. Build output rows.
    rows = []
    total_alloc = 0.0
    for i, t in enumerate(inputs):
        mult = band_multiplier(t.band)
        raw_w = normalized[i]
        cap_w = final_weights[i]
        dollars = round(cap_w * total_dollars, 2)
        skipped = mult == 0.0 or raw_w == 0.0
        if skipped:
            note = (
                "Excluded per Review band"
                if _normalize_band(t.band) == "review"
                else "Skipped (band multiplier 0)"
            )
        elif cap_w + _EPS < raw_w:
            note = f"Clipped to {cap*100:.0f}% cap"
        elif t.data_quality_flags:
            note = "Low confidence: " + ", ".join(t.data_quality_flags)
        else:
            note = ""
        rows.append(
            {
                "ticker": t.ticker,
                "band": t.band,
                "composite": float(t.composite),
                "band_multiplier": mult,
                "raw_weight": raw_w,
                "capped_weight": cap_w,
                "dollars": dollars,
                "skipped": skipped,
                "note": note,
            }
        )
        total_alloc += dollars

    cash_remaining = round(float(total_dollars) - total_alloc, 2)
    return {
        "rows": rows,
        "total_allocated": round(total_alloc, 2),
        "total_skipped": 0.0,
        "cash_remaining": cash_remaining,
        "risk_profile": risk_profile.lower(),
        "per_position_cap": cap,
    }


__all__ = [
    "BAND_MULTIPLIERS",
    "RISK_PROFILES",
    "TickerInput",
    "band_multiplier",
    "per_position_cap",
    "suggest_allocations",
]
