"""city/context.py — Layer B (City Context) composition.

Combines the independent national-API adapters (Census ACS, BLS LAUS, EPA AirNow)
into the schema ``context`` object, and exposes the FBI CDE crime series that fills
Miami's Public Safety pillar. Every source is independent and null-safe: a missing
key or a failed call yields ``None`` for that field and never raises out of
``build_context`` — so the City Context strip degrades gracefully one KPI at a time.

Keys are read from the environment by each adapter (CENSUS_API_KEY, BLS_API_KEY,
AIRNOW_API_KEY, FBI_CDE_API_KEY). BLS and the FBI CDE series work keyless (a BLS
key only raises its daily cap; the CDE host this repo calls does not check one);
Census ACS and AirNow need a free key for live data (both re-verified 2026-10-04:
keyless ACS 302-redirects to missing_key.html, keyless AirNow answers HTTP 401).

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

import json
import os
import subprocess
import sys
from pathlib import Path
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
# key that was never created. Keyed by source rather than by env-var name: this
# is fixed public help text, and a lookup like ["CENSUS_API_KEY"] reads to
# static analysis (CodeQL py/clear-text-logging-sensitive-data) as a secret.
_SIGNUP_HELP = {
    "census": (
        "free key: https://api.census.gov/data/key_signup.html — then add it to "
        "the repo secrets AND to the env: block of .github/workflows/city-daily.yml"
    ),
    "airnow": (
        "free key: https://docs.airnowapi.org/account/request/ — then add it to "
        "the repo secrets AND to the env: block of .github/workflows/city-daily.yml"
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
              + _SIGNUP_HELP["census"], _ACS_FIELDS)
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
              "a request. " + _SIGNUP_HELP["airnow"], "aqi")
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
    Safety pillar). Returns ``[]`` only when the feed has no resolved ORI or CDE
    reports no actuals; the request is sent with or without ``FBI_CDE_API_KEY``
    because the CDE endpoint serves this series keyless (see ``city/fbi.py``)."""
    ori = feed_cfg.get("ori")
    if not ori:
        return []
    return fbi.monthly_offenses(ori, since=since, until=until, session=session)


# ACS dollar fields written by one Census call. When that call comes back
# empty, a later successful rent must not be overwritten by an older rent,
# but a field that is null alongside median_income is the same failed lookup
# and is carried with it. Unemployment and AQI are different sources.
_ACS_CARRY_FIELDS = (
    "median_income",
    "median_rent",
    "median_home_value",
    "median_real_estate_taxes",
    "effective_property_tax_rate",
)

# Schema context keys (docs/city/data-city.schema.json). The context object
# has additionalProperties: false and no as-of / stale property, so a carried
# value cannot grow a marker field. The as-of date goes on the city's
# disclosures list, which the schema already defines for source-vintage caveats.
_CONTEXT_KEYS = (
    "median_income",
    "median_rent",
    "median_home_value",
    "median_real_estate_taxes",
    "effective_property_tax_rate",
    "unemployment_rate",
    "aqi",
    "context_score",
)

# How many previous commits of data-city.json to scan when the file on disk
# already lost the value (the 2026-10-23 12:50Z commit did exactly that).
_GIT_LOOKBACK = 40


def good_median_income(value) -> bool:
    """True for a usable ACS median household income.

    ``bool`` is an ``int`` subclass and is not an income. Zero and the ACS
    negative sentinels are not incomes either; those already arrive as
    ``None`` from :func:`city.census.fetch_acs`, but a raw ``0`` must not be
    treated as a value worth keeping.
    """
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _blank_context() -> dict:
    return {key: None for key in _CONTEXT_KEYS}


def apply_median_income_carry_forward(city_obj: dict, prior: Optional[dict],
                                      *, diagnostics=None) -> bool:
    """Fill an empty ``median_income`` from the last good snapshot.

    ``prior`` is ``{"as_of": <generated_at or None>, "context": {...}}`` from
    :func:`load_last_good_contexts`, or ``None`` when no earlier income
    exists. Sibling ACS fields that are also empty are copied from that same
    snapshot; a field the new run actually returned is left alone.
    Unemployment, AQI, and ``context_score`` are never copied — they belong
    to other adapters, and the score is recomputed after this pass.

    Logs a warning and appends a disclosure (the schema's as-of marker).
    Returns True when income was carried forward.
    """
    if not prior or not isinstance(prior.get("context"), dict):
        return False
    prior_ctx = prior["context"]
    prior_income = prior_ctx.get("median_income")
    if not good_median_income(prior_income):
        return False

    ctx = city_obj.get("context")
    if isinstance(ctx, dict) and good_median_income(ctx.get("median_income")):
        return False
    if not isinstance(ctx, dict):
        ctx = _blank_context()
        city_obj["context"] = ctx

    filled = []
    for field in _ACS_CARRY_FIELDS:
        if ctx.get(field) is None and prior_ctx.get(field) is not None:
            ctx[field] = prior_ctx[field]
            filled.append(field)
    if "median_income" not in filled:
        return False

    city_id = city_obj.get("id", "?")
    as_of = prior.get("as_of") or "the previous snapshot"
    others = [field for field in filled if field != "median_income"]
    note = (
        "Median household income carried forward from {} (Census ACS returned "
        "no value this run; last good value {}).".format(as_of, prior_income)
    )
    if others:
        note += " Also kept {} from that snapshot.".format(", ".join(others))
    disclosures = city_obj.get("disclosures")
    if not isinstance(disclosures, list):
        disclosures = []
        city_obj["disclosures"] = disclosures
    if note not in disclosures:
        disclosures.append(note)

    print(
        "  [context] {} census: warning -> median_income came back empty; "
        "carrying forward {} as of {}".format(city_id, prior_income, as_of),
        file=sys.stderr,
    )
    if diagnostics is not None:
        diagnostics.append({
            "city": city_id,
            "source": "census",
            "kind": "carried-forward",
            "detail": "as of {}".format(as_of),
            "lost": "median_income",
        })
    return True


def _absorb_incomes(payload, need: set, found: dict) -> None:
    if not isinstance(payload, dict):
        return
    as_of = payload.get("generated_at")
    for city in payload.get("cities") or []:
        if not isinstance(city, dict):
            continue
        city_id = city.get("id")
        if city_id not in need or city_id in found:
            continue
        ctx = city.get("context")
        if isinstance(ctx, dict) and good_median_income(ctx.get("median_income")):
            found[city_id] = {"as_of": as_of, "context": ctx}


def _git_payloads(path: Path, repo_root: Path, limit: int):
    """Newest-first parsed JSON snapshots of ``path`` from git history.

    Yields nothing when ``path`` is outside ``repo_root`` (a test ``--out``)
    or git cannot be read. A failure here must not fail the city build.
    """
    try:
        rel = path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return
    try:
        log = subprocess.run(
            ["git", "log", "-n", str(limit), "--format=%H", "--", rel],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return
    if log.returncode != 0:
        return
    for sha in log.stdout.split():
        try:
            show = subprocess.run(
                ["git", "show", "{}:{}".format(sha, rel)],
                cwd=str(repo_root),
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return
        if show.returncode != 0 or not show.stdout:
            continue
        try:
            yield json.loads(show.stdout)
        except json.JSONDecodeError:
            continue


def load_last_good_contexts(path: Path, city_ids, *, repo_root: Path) -> dict:
    """Last snapshot in which each city still had a median income.

    Prefers the file about to be overwritten. When that file already stored
    null (the 2026-10-23 afternoon commit), walks up to ``_GIT_LOOKBACK``
    commits of the same path. Returns ``{city_id: {"as_of", "context"}}``
    and omits cities with no earlier income.
    """
    need = {city_id for city_id in city_ids if city_id}
    found: dict = {}
    if not need:
        return found
    if path.is_file():
        try:
            _absorb_incomes(json.loads(path.read_text()), need, found)
        except (OSError, json.JSONDecodeError):
            pass
    if len(found) < len(need):
        for payload in _git_payloads(path, repo_root, _GIT_LOOKBACK):
            _absorb_incomes(payload, need, found)
            if len(found) == len(need):
                break
    return found
