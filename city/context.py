"""city/context.py — Layer B (City Context) composition.

Combines the independent national-API adapters (Census ACS, BLS LAUS, EPA AirNow)
into the schema ``context`` object, and exposes the FBI CDE crime series that fills
Miami's Public Safety pillar. Every source is independent and null-safe: a missing
key or a failed call yields ``None`` for that field and never raises out of
``build_context`` — so the City Context strip degrades gracefully one KPI at a time.

Keys are read from the environment by each adapter (CENSUS_API_KEY, BLS_API_KEY,
AIRNOW_API_KEY, FBI_CDE_API_KEY). BLS works keyless; the others need a free key for
live data.
"""

from __future__ import annotations

from typing import Optional

from city import census, bls, airnow, fbi


def build_context(city_cfg: dict, geo_cfg: Optional[dict], *, session=None) -> Optional[dict]:
    """Assemble the schema ``context`` object for one city (Census ACS levels +
    effective property tax, BLS unemployment, EPA AQI). Returns ``None`` when no
    source produced any value, so the card shows the 'Context coming' state rather
    than a row of dashes."""
    acs = {}
    if geo_cfg:
        try:
            acs = census.fetch_acs(geo_cfg, session=session)
        except census.CensusError:
            acs = {}

    try:
        unemployment = bls.fetch_unemployment(city_cfg["id"], session=session)
    except bls.BLSError:
        unemployment = None

    try:
        aqi = airnow.fetch_aqi(city_cfg["id"], session=session)
    except airnow.AirNowError:
        aqi = None

    ctx = {
        "median_income": acs.get("median_income"),
        "median_rent": acs.get("median_rent"),
        "median_home_value": acs.get("median_home_value"),
        "median_real_estate_taxes": acs.get("median_real_estate_taxes"),
        "effective_property_tax_rate": acs.get("effective_property_tax_rate"),
        "unemployment_rate": unemployment,
        "aqi": aqi,
        "context_score": None,  # optional transparent composite — P2
    }
    if all(v is None for k, v in ctx.items() if k != "context_score"):
        return None
    return ctx


def fbi_crime_series(feed_cfg: dict, *, since: str, until: str, session=None) -> list:
    """Monthly offense counts for a feed whose adapter == 'fbi' (Miami's Public
    Safety pillar). Returns ``[]`` when ``FBI_CDE_API_KEY`` is unset (the feed then
    stays ``not_published``) or when the feed has no resolved ORI."""
    ori = feed_cfg.get("ori")
    if not ori:
        return []
    return fbi.monthly_offenses(ori, since=since, until=until, session=session)
