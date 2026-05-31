"""City Context composite scorer — Layer B cross-city level comparison.

PURE Python: no network, no file I/O, stdlib only. This module builds a
*transparent* 0-100 "City Context" composite from the Layer-B KPIs defined in
``docs/city/data-city.schema.json`` (``#/definitions/context``) and described
in ``docs/city/CITY_TAB_BUILD.md`` section 4.

WHY THIS EXISTS (CITY_TAB_BUILD.md section 4)
---------------------------------------------
No free, reusable city-index API exists (AARP Livability, Numbeo, EIU, IMD are
proprietary web tools). So instead of importing an opaque third-party ranking,
we compute our own 0-100 composite from the cross-city KPIs we already fetch,
with the SAME disclosure discipline as City Pulse.

CONTEXT vs PULSE — TWO SEPARATE NUMBERS, NEVER MERGED
-----------------------------------------------------
City Pulse (Layer A) scores each city against *its own* trailing history
(momentum). City Context (this module) is the opposite axis: a CROSS-CITY
comparison of *levels* — rent, income, taxes, AQI, unemployment are legitimately
comparable across cities (unlike raw operational counts). The spec is explicit:
**keep Pulse and Context as two separate numbers, shown side by side; never
merge them.** This module therefore touches only the ``context`` KPIs and emits
an integer that lands in the schema's ``context.context_score`` field — it never
reads or blends a Pulse value.

THE METHOD (every editorial choice documented; all are disclosable)
-------------------------------------------------------------------
For each KPI, across the cities that HAVE that KPI:

1. **Min-max normalize to [0, 100]** across available cities. The city with the
   minimum raw value scores 0 on that component; the maximum scores 100; others
   scale linearly in between. (Min-max, not z-score: with only ~6 cities a
   z-score is noisy and unbounded, and min-max gives an interpretable "this
   city sits X% of the way between the cheapest and priciest" reading that is
   easy to disclose.)
2. **Flip per direction** so that "better" always means a higher component.
   ``component = scaled`` for ``+`` KPIs (higher raw = better) and
   ``component = 100 - scaled`` for ``-`` KPIs (lower raw = better).
3. **Equal-weighted MEAN** of the components the city actually has. No KPI is
   ever imputed: a city missing a KPI is scored on the mean of the KPIs it does
   have (coverage honesty), it is NOT penalized and the gap is NOT filled with a
   neutral/zero value. (Equal weight by default — the weighting is an explicit
   editorial choice; ``weights`` lets a caller override it.)

KPI DIRECTIONS (the editorial polarity — disclosed, mirrors Pulse's polarity idea)
----------------------------------------------------------------------------------
* ``median_income``                 -> ``+`` (higher income = better)
* ``median_rent``                    -> ``-`` (lower rent = more affordable)
* ``median_real_estate_taxes``       -> ``-`` (lower tax bill = better)
* ``effective_property_tax_rate``    -> ``-`` (lower rate = better)
* ``unemployment_rate``              -> ``-`` (lower unemployment = better)
* ``aqi``                            -> ``-`` (lower AQI = cleaner air = better)
* ``median_home_value``              -> EXCLUDED (direction is genuinely
  ambiguous: high home value reads as "desirable/wealthy" to a buyer-owner but
  "unaffordable" to a renter/first-time buyer — it has no single defensible
  "better" direction, and ``median_rent`` already carries the affordability
  signal, so including it would double-count cost. We keep the value visible in
  the Context strip but leave it OUT of the composite. Documented, not silently
  dropped.)

Note ``median_real_estate_taxes`` (absolute dollars) and
``effective_property_tax_rate`` (taxes / home value) are *both* included and
both point ``-``. They are correlated but not redundant — the dollar figure
captures the raw bill, the rate captures the burden relative to value — so each
is one equal-weighted voice in the mean. A caller who considers that
double-counting can drop either via ``weights``.

COVERAGE / MISSING-DATA RULES
-----------------------------
* A KPI present for **fewer than 2 cities** cannot be min-max normalized (no
  spread / a single point is always both min and max) -> that KPI is **skipped
  entirely** for every city. Documented.
* If after skipping under-covered KPIs a city has **no usable KPI**, its score
  is ``None`` (not 0). An all-``None`` city is likewise ``None``.
* When a KPI's available cities are all equal (zero spread, but >=2 cities),
  min-max is undefined (divide-by-zero). We treat every such city as exactly
  mid-scale on that component (50) — they are genuinely tied, so neither best
  nor worst. Documented.

OUTPUT
------
``score_context`` returns ``{city_id: int | None}`` — the final composite per
city rounded to the nearest integer (banker's rounding via ``round``; values are
spread across [0,100] so half-way ties are vanishingly rare), or ``None`` for a
city with no usable KPI. This integer is what the producer writes into
``context.context_score``.
"""
from __future__ import annotations

import statistics

__all__ = [
    "score_context",
    "context_score_disclosures",
    "KPI_DIRECTIONS",
    "EXCLUDED_KPIS",
]

# --- editorial polarity of each composite KPI -------------------------------
# +1: higher raw value is "better" (higher composite component).
# -1: lower raw value is "better".
# KPIs NOT listed here are excluded from the composite by design (see
# EXCLUDED_KPIS and the module docstring).
KPI_DIRECTIONS = {
    "median_income": +1,
    "median_rent": -1,
    "median_real_estate_taxes": -1,
    "effective_property_tax_rate": -1,
    "unemployment_rate": -1,
    "aqi": -1,
}

# KPIs deliberately kept out of the composite, with the reason (for disclosure).
EXCLUDED_KPIS = {
    "median_home_value": (
        "ambiguous direction (desirable wealth vs unaffordability) and would "
        "double-count cost already captured by median_rent"
    ),
    # context_score is the output field itself, never an input.
    "context_score": "this is the composite's own output field, not an input KPI",
}

# Component value assigned when a KPI's available cities are all tied (zero
# spread): genuinely neither best nor worst -> mid-scale.
_TIED_COMPONENT = 50.0


def _coerce_number(value):
    """Return ``value`` as a float if it is a real, finite number; else None.

    Guards against ``None`` (missing KPI), bools (``True``/``False`` are ints in
    Python but are never valid KPI readings), and non-numeric junk. Keeps the
    'never impute, never crash on bad data' contract.
    """
    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, (int, float)):
        return None
    f = float(value)
    # Reject NaN / +-inf (NaN != NaN).
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def score_context(cities_context: dict, *, weights=None) -> dict:
    """Compute a transparent cross-city 0-100 City Context composite per city.

    Parameters
    ----------
    cities_context : dict
        ``{city_id: context_dict_or_None}``. Each ``context_dict`` is a schema
        ``context`` object (the Layer-B KPIs). A value of ``None`` means the
        city has no context at all.
    weights : dict | None
        Optional ``{kpi_name: weight}`` to override the default equal weighting
        of the composite KPIs. Unlisted KPIs keep weight 1.0; a weight of 0
        drops that KPI from the mean. Negative weights are not meaningful and
        are treated as 0. ``None`` (default) = equal weights.

    Returns
    -------
    dict
        ``{city_id: int | None}`` — the rounded composite per city, or ``None``
        if the city has no usable KPI after under-covered KPIs are skipped.

    Method (see module docstring for the full rationale): each KPI is min-max
    scaled to [0,100] across the cities that HAVE it, flipped so "better" is
    higher per :data:`KPI_DIRECTIONS`, then a city's score is the equal-weighted
    mean over the KPIs it actually has. KPIs present for <2 cities are skipped;
    KPIs are never imputed; missing KPIs are never treated as 0.
    """
    # Stable iteration order over cities (preserves caller's dict order).
    city_ids = list(cities_context.keys())

    # 1) Gather, per KPI, the finite numeric values for the cities that have it.
    #    {kpi: {city_id: float}}
    kpi_values: dict = {kpi: {} for kpi in KPI_DIRECTIONS}
    for cid in city_ids:
        ctx = cities_context.get(cid)
        if not isinstance(ctx, dict):
            continue  # None or junk -> city contributes no KPI.
        for kpi in KPI_DIRECTIONS:
            val = _coerce_number(ctx.get(kpi))
            if val is not None:
                kpi_values[kpi][cid] = val

    # 2) For each KPI usable across >=2 cities, min-max scale + flip to a
    #    per-city component in [0,100]. {kpi: {city_id: component}}
    kpi_components: dict = {}
    for kpi, direction in KPI_DIRECTIONS.items():
        per_city = kpi_values[kpi]
        if len(per_city) < 2:
            # <2 cities -> can't normalize a spread -> skip this KPI entirely.
            continue
        lo = min(per_city.values())
        hi = max(per_city.values())
        spread = hi - lo
        comp_for_city = {}
        for cid, raw in per_city.items():
            if spread == 0:
                # All available cities tied -> mid-scale (neither best/worst).
                comp = _TIED_COMPONENT
            else:
                scaled = (raw - lo) / spread * 100.0  # 0 at min, 100 at max
                # Flip so "better" is always higher.
                comp = scaled if direction > 0 else (100.0 - scaled)
            comp_for_city[cid] = comp
        kpi_components[kpi] = comp_for_city

    # 3) Per-city equal-weighted (or caller-weighted) mean over present KPIs.
    def _weight(kpi):
        if weights is None:
            return 1.0
        w = weights.get(kpi, 1.0)
        try:
            w = float(w)
        except (TypeError, ValueError):
            return 1.0
        return w if w > 0 else 0.0

    out: dict = {}
    for cid in city_ids:
        num = 0.0
        den = 0.0
        for kpi, comp_for_city in kpi_components.items():
            if cid not in comp_for_city:
                continue  # city lacks this KPI -> not imputed, just absent.
            w = _weight(kpi)
            if w == 0.0:
                continue
            num += w * comp_for_city[cid]
            den += w
        if den == 0.0:
            out[cid] = None  # no usable KPI for this city.
        else:
            out[cid] = int(round(num / den))
    return out


def context_score_disclosures() -> list:
    """Caveat strings for the City Context composite (the section-2-style
    disclosure discipline applied to Layer B).

    Returns a fresh list each call so callers can mutate it safely.
    """
    included = ", ".join(
        "{} ({})".format(kpi, "higher=better" if d > 0 else "lower=better")
        for kpi, d in KPI_DIRECTIONS.items()
    )
    return [
        "City Context is a cross-city comparison of LEVELS, not a "
        "quality-of-life ranking and not a verdict on which city is 'best' to "
        "live in.",
        "City Context and City Pulse are two separate numbers shown side by "
        "side and are never merged: Pulse measures a city against its own "
        "history (momentum); Context compares cities to each other (levels).",
        "The composite is an editorial construction: each included KPI is "
        "equal-weighted by default. Included KPIs and their 'better' direction: "
        + included + ".",
        "median_home_value is intentionally EXCLUDED: its direction is "
        "ambiguous (a high value reads as desirable to owners but unaffordable "
        "to renters/buyers) and affordability is already captured by "
        "median_rent. It is shown in the Context strip but left out of the "
        "score.",
        "Each KPI is min-max normalized to 0-100 across only the cities that "
        "report it (best value scores 100, worst scores 0; on a '-' KPI the "
        "scale is flipped so lower is better).",
        "Coverage honesty: a city is scored on the mean of the KPIs it has and "
        "is never penalized or imputed for a missing KPI; a KPI reported by "
        "fewer than two cities can't be normalized and is dropped for everyone; "
        "a city with no usable KPI has no Context score.",
    ]
