"""city/context.py — Layer B (City Context) composition.

Combines the independent national-API adapters (Census ACS, BLS LAUS, EPA AirNow)
into the schema ``context`` object, and exposes the FBI CDE crime series that fills
Miami's Public Safety pillar. Every source is independent and null-safe: a missing
key or a failed call yields ``None`` for that field and never raises out of
``build_context`` — so the City Context strip degrades gracefully one KPI at a time.

Keys are read from the environment by each adapter (CENSUS_API_KEY, BLS_API_KEY,
AIRNOW_API_KEY, FBI_CDE_API_KEY). BLS works keyless; the others need a free key for
live data.

WHY THIS FILE LOGS SO LOUDLY
----------------------------
Graceful degradation and silent degradation are not the same thing, and this
module used to do the second while claiming the first. ``except CensusError:
acs = {}`` with no message meant that every ACS-derived KPI — median_income,
median_rent, median_home_value, median_real_estate_taxes,
effective_property_tax_rate — was null for all six cities, forever, and the
build printed nothing at all. Census does not short-circuit on a missing key
the way AirNow does: it sends the request keyless, gets back an HTML "Missing
Key" page, ``resp.json()`` raises, and the ``except`` erased the evidence. The
dashboard showed a tidy row of em-dashes, which reads as "this city has no
data" rather than "we never sent a usable request".

So: every degradation below announces itself on stderr, names the field(s) it
just nulled out, and — when the cause is a missing credential — names the exact
environment variable and where to put it. Nothing here is fatal; a missing
OPTIONAL key must never break the nightly build. But it must never be quiet
either. The distinction the messages preserve:

  * ``no key``  — an operator action is required. We never sent a real request.
  * ``failed``  — we sent a request and it did not work (upstream down, schema
    change, proxy, rate limit). A bug or an outage, not a missing secret.

BLS is the control: it is keyless and it works, which is how you can tell the
pipeline itself runs and the null ACS/AQI fields are a credential problem
rather than a dead job.
"""

from __future__ import annotations

import os
import sys
from typing import Optional

from city import census, bls, airnow, fbi

from .redact import redact

# Fields that go null when the Census ACS call does not produce data. Named
# explicitly so the log says what was lost instead of just what failed.
_ACS_FIELDS = (
    "median_income, median_rent, median_home_value, "
    "median_real_estate_taxes, effective_property_tax_rate"
)

# Where an operator actually fixes a missing key. These are referenced by
# .github/workflows/city-daily.yml, so a key that exists in the repo secrets but
# is not passed through in that workflow's `env:` block presents exactly like a
# key that was never created.
_KEY_HELP = {
    "CENSUS_API_KEY": (
        "free key: https://api.census.gov/data/key_signup.html — then add it to "
        "the repo secrets AND to the env: block of .github/workflows/city-daily.yml"
    ),
    "AIRNOW_API_KEY": (
        "free key: https://docs.airnowapi.org/account/request/ — then add it to "
        "the repo secrets AND to the env: block of .github/workflows/city-daily.yml"
    ),
    "FBI_CDE_API_KEY": (
        "free key: https://api.data.gov/signup/ — then add it to the repo secrets "
        "AND to the env: block of .github/workflows/city-daily.yml"
    ),
}


def _note(diagnostics, city_id, source, kind, detail, lost):
    """Record ONE degradation: print it, and append it to ``diagnostics``.

    ``kind`` is 'no key' (operator action needed, no request was sent) or
    'failed' (a request was sent and did not work). ``lost`` names the schema
    fields that are null as a result, because "Census failed" tells a reader
    nothing about what is missing from the page.
    """
    # Redact BEFORE the string is built, and store the redacted copy — the
    # diagnostics list is re-printed by fetch_city's build summary, so an
    # unredacted value here would leak twice. `detail` is usually str(e)[:300]
    # from an adapter, and those adapters put the request URL in their message;
    # the URL carries the API key as a query parameter.
    detail = redact(detail) if detail else detail
    msg = "  [context] {} {}: {} -> {} is null".format(city_id, source, kind, lost)
    if detail:
        msg += " ({})".format(detail)
    print(msg, file=sys.stderr)
    if diagnostics is not None:
        diagnostics.append({
            "city": city_id, "source": source, "kind": kind,
            "detail": detail, "lost": lost,
        })


def build_context(city_cfg: dict, geo_cfg: Optional[dict], *, session=None,
                  diagnostics=None) -> Optional[dict]:
    """Assemble the schema ``context`` object for one city (Census ACS levels +
    effective property tax, BLS unemployment, EPA AQI). Returns ``None`` when no
    source produced any value, so the card shows the 'Context coming' state rather
    than a row of dashes.

    Never raises: each adapter is isolated so one failure cannot take the other
    two down with it. Each failure is reported on stderr and, when
    ``diagnostics`` is a list, appended to it so the caller can print one
    build-level summary instead of making a human scroll six cities of output.
    """
    city_id = city_cfg.get("id", "?")
    acs = {}

    if not geo_cfg:
        _note(diagnostics, city_id, "census",
              "no geography in registry (context_layer.sources.census_acs."
              "geo_by_city)", "", _ACS_FIELDS)
    elif not os.environ.get("CENSUS_API_KEY"):
        # Census does NOT short-circuit on its own: keyless it returns an HTML
        # "Missing Key" page that fails to parse as JSON. Say so up front rather
        # than reporting the resulting parse error as if it were an outage.
        _note(diagnostics, city_id, "census", "no key",
              "CENSUS_API_KEY unset; the ACS DATA endpoint returns an HTML "
              "'Missing Key' page keyless, so no request is worth sending. "
              + _KEY_HELP["CENSUS_API_KEY"], _ACS_FIELDS)
    else:
        try:
            acs = census.fetch_acs(geo_cfg, session=session)
        except census.CensusError as e:
            _note(diagnostics, city_id, "census", "failed", str(e)[:300], _ACS_FIELDS)
            acs = {}
        except Exception as e:  # an adapter bug must not cost us BLS + AQI too
            _note(diagnostics, city_id, "census", "failed",
                  "unexpected {}: {}".format(type(e).__name__, e)[:300], _ACS_FIELDS)
            acs = {}

    # BLS is keyless-capable and is the control for "does this pipeline run at
    # all": if unemployment_rate is populated while the keyed sources are null,
    # the job is healthy and the keys are missing.
    try:
        unemployment = bls.fetch_unemployment(city_cfg["id"], session=session)
        if unemployment is None:
            _note(diagnostics, city_id, "bls", "no value",
                  "keyless BLS returned no usable observation", "unemployment_rate")
    except bls.BLSError as e:
        _note(diagnostics, city_id, "bls", "failed", str(e)[:300], "unemployment_rate")
        unemployment = None
    except Exception as e:
        _note(diagnostics, city_id, "bls", "failed",
              "unexpected {}: {}".format(type(e).__name__, e)[:300], "unemployment_rate")
        unemployment = None

    if not os.environ.get("AIRNOW_API_KEY"):
        # airnow.fetch_aqi short-circuits to None without a key and raises
        # nothing, so the old `except AirNowError` never fired and this
        # degradation produced literally no output anywhere.
        _note(diagnostics, city_id, "airnow", "no key",
              "AIRNOW_API_KEY unset; the adapter short-circuits without sending "
              "a request. " + _KEY_HELP["AIRNOW_API_KEY"], "aqi")
        aqi = None
    else:
        try:
            aqi = airnow.fetch_aqi(city_cfg["id"], session=session)
            if aqi is None:
                _note(diagnostics, city_id, "airnow", "no value",
                      "AirNow returned no usable AQI observation for this city",
                      "aqi")
        except airnow.AirNowError as e:
            _note(diagnostics, city_id, "airnow", "failed", str(e)[:300], "aqi")
            aqi = None
        except Exception as e:
            _note(diagnostics, city_id, "airnow", "failed",
                  "unexpected {}: {}".format(type(e).__name__, e)[:300], "aqi")
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


def fbi_key_missing() -> bool:
    """True when ``FBI_CDE_API_KEY`` is unset.

    Lets the caller label Miami's Public Safety pillar with the reason it is
    empty ('needs FBI_CDE_API_KEY') instead of the bare 'not published by this
    city', which is false — the FBI publishes the series, we just cannot ask
    for it.
    """
    return not os.environ.get("FBI_CDE_API_KEY")


def fbi_key_help() -> str:
    """Operator-facing instruction for the missing FBI key."""
    return _KEY_HELP["FBI_CDE_API_KEY"]
