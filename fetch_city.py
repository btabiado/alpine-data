#!/usr/bin/env python3
"""fetch_city.py — build ``data-city.json`` for the City tab (Layer A: City Pulse).

Pipeline: read ``docs/city/city_registry.resolved.json`` -> fetch each feed's
monthly series (Socrata for 5 cities, ArcGIS for Miami/Miami-Dade) -> score City
Pulse (``city/pulse.py``, methodology in CITY_TAB_BUILD.md s2) -> write the
schema payload (``docs/city/data-city.schema.json``).

Layer B context (Census/BLS/AirNow + FBI crime) is P1 — ``context`` stays null
here and Miami's Safety pillar is ``not_published`` until the FBI key lands.

Resilience (mirrors the repo's other fetchers): every feed is wrapped in its own
try/except; a fetch failure degrades that ONE feed (status reflects it) and never
aborts the build. Daily cadence: a 24h freshness guard skips work when the prior
output is fresh (the dashboard CI runs hourly; municipal data updates daily).

TWO THINGS THIS FILE GOT WRONG, AND WHAT THEY COST
--------------------------------------------------
1. THE CUTOFF WAS A CONSTANT. ``as_of`` was read from the registry's hand-written
   ``_meta.as_of_complete_month`` ("2026-04", set during recon on 2026-05-31).
   That value becomes every feed's ``complete_through``, and the scorer drops
   every month past it. So the nightly job re-fetched all six cities, received
   May/June/July, discarded it at the cutoff, and rewrote ``generated_at`` with
   the current clock anyway. The FILE moved every night; the DATA inside it had
   not moved since April. See :func:`_resolve_as_of` — the cutoff is now derived
   from the clock on every run and the registry pin is only a warning.

2. A FAILED REQUEST WAS REPORTED AS A CITY THAT PUBLISHES NOTHING. Every adapter
   exception mapped to ``not_published``, which the dashboard renders as "Not
   published by this city". Miami-Dade publishes building permits daily (the
   registry records ISSUDATE through 2026-05) and the committed artifact still
   labelled that feed ``not_published`` because our ArcGIS call was failing.
   There is now a separate ``fetch_error`` status: same exclusion from the
   pillar math, opposite meaning, and the reason travels into the feed's
   ``note`` so it reaches the page instead of dying in a CI log.

Both had the same root: a degradation that produced no output. Every degradation
here now announces itself, and :func:`_report_diagnostics` prints one grouped
summary at the end of the run with missing credentials called out separately —
they are the only class a human can actually fix.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

from city import socrata, arcgis, pulse
from city import context as city_context

from city.redact import redact

ROOT = Path(__file__).resolve().parent
REGISTRY = ROOT / "docs" / "city" / "city_registry.resolved.json"
EXTENDED_REGISTRY = ROOT / "docs" / "city" / "city_registry.extended.json"  # P2, optional
DEFAULT_OUT = ROOT / "data-city.json"
FRESH_SECONDS = 24 * 3600  # daily cadence

# The section-2 caveats, shown in the methodology disclosure panel (a P0 gate).
METHODOLOGY_DISCLOSURES = [
    "Each feed is scored against that city's own trailing-12-month baseline: "
    "50 = on its own baseline, >50 trending favorable, <50 unfavorable.",
    "Not a cross-city ranking. A higher Pulse means a city is improving versus "
    "its own past, not that it is 'better' than another city.",
    "Polarity is an editorial choice. Each feed declares which direction is "
    "favorable (permits up = good; crime / 311 backlog down = good); see each "
    "feed's polarity in the breakdown.",
    "Data-continuity breaks can cause artificial jumps: Seattle PD's 2019 "
    "records-system change, LA's yearly dataset rotation, and SF's 2018 portal "
    "migration are known breakpoints.",
    "Reporting lag: some feeds exclude the most recent days (Chicago crime " +
    "excludes ~7 days), so 'Recent' is aligned to the last complete month.",
    "Coverage honesty: when a city does not publish a pillar's feed, Pulse is " +
    "computed on what is present and labeled 'N of 3 pillars' — missing data is "
    "never treated as zero.",
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _month_minus(ym: str, k: int) -> str:
    y, m = int(ym[:4]), int(ym[5:7])
    idx = (y * 12 + (m - 1)) - k
    return "{:04d}-{:02d}".format(idx // 12, idx % 12 + 1)


def _prev_complete_month(now: datetime) -> str:
    """The last fully-complete calendar month relative to ``now`` (this month - 1)."""
    return _month_minus("{:04d}-{:02d}".format(now.year, now.month), 1)


def _is_lagging(feed_cfg: dict) -> bool:
    note = (feed_cfg.get("note") or "").lower()
    return ("excludes last" in note) or ("lags" in note and "month" in note)


def _city_disclosures(city_cfg: dict) -> list:
    """Per-city caveats: scope note + the real feed notes (deduped)."""
    out = []
    if city_cfg.get("scope") == "county":
        out.append(
            "Footprint = Miami-Dade County (not the City of Miami). County 311 "
            "excludes City-of-Miami-proper requests."
        )
    seen = set()
    for f in city_cfg.get("feeds", []):
        note = f.get("note")
        if note and note not in seen:
            seen.add(note)
            out.append("{}: {}".format(f.get("label", "feed"), note))
    return out


def _fetch_feed_series(feed_cfg: dict, city_cfg: dict, *, as_of: str,
                       since_date: str, session=None, diagnostics=None):
    """Return ``(series, status_hint, reason)``.

    ``status_hint`` OVERRIDES the scorer's data-derived status:

      * ``'ok'``             — normal fetch, let the data decide.
      * ``'stale'``          — the source exists but is a frozen snapshot
                               (Miami's 2023 311 yearly file).
      * ``'not_published'``  — the CITY does not publish this feed. A statement
                               about a third party.
      * ``'fetch_error'``    — OUR request failed, or we have no credential for
                               it. A statement about US.

    That last one is new, and the distinction is the point. Every adapter error
    used to be mapped to ``not_published``, so a proxy hiccup, a schema change,
    a rate limit and a missing API key all rendered on the dashboard as "Not
    published by this city". That is a false claim about a real government
    body, and it is self-concealing: a feed that quietly starts failing looks
    exactly like a feed the city never had, so nothing ever gets investigated.
    Miami-Dade's building-permit layer is live and current (the registry
    records ISSUDATE running to 2026-05) yet ships in the committed artifact as
    ``not_published`` — our ArcGIS call is failing and the label hid it.

    ``reason`` is the human-readable cause for the non-ok hints; the caller
    puts it in the feed's ``note`` so the reason survives into the payload and
    onto the page instead of dying in a CI log nobody reads.
    """
    adapter = feed_cfg.get("adapter") or city_cfg.get("adapter")
    since_ym = since_date[:7]
    label = feed_cfg.get("label", "feed")

    def _fail(kind, reason):
        # See city/redact.py. `reason` is typically str(e) from an adapter and
        # those messages embed the request URL, which carries the API key.
        # Redact before printing AND before storing: the diagnostics list is
        # re-printed by _print_diagnostics below, so storing a raw value would
        # publish it twice.
        reason = redact(reason)
        print("  [{}] {} {} -> {}".format(kind, adapter or "socrata", label, reason),
              file=sys.stderr)
        if diagnostics is not None:
            diagnostics.append({
                "city": city_cfg.get("id", "?"), "source": adapter or "socrata",
                "kind": kind, "detail": reason, "lost": "feed '{}'".format(label),
            })

    if adapter == "fbi":
        # Miami Public Safety via FBI CDE. The FBI DOES publish this series —
        # what we lack is a free api.data.gov key — so an empty result here is
        # 'fetch_error' (on us), never 'not_published' (on them).
        if city_context.fbi_key_missing():
            reason = ("needs FBI_CDE_API_KEY: the FBI publishes this NIBRS "
                      "series, we cannot request it without a free key. "
                      + city_context.fbi_key_help())
            _fail("no key", reason)
            return [], "fetch_error", reason
        try:
            series = city_context.fbi_crime_series(
                feed_cfg, since=since_ym, until=as_of, session=session
            )
            if series:
                return series, "ok", None
            reason = ("FBI CDE returned no offense rows for ORI {} over {}..{}"
                      .format(feed_cfg.get("ori"), since_ym, as_of))
            _fail("empty", reason)
            return [], "fetch_error", reason
        except Exception as e:  # FBI is best-effort; never abort the build
            reason = "FBI CDE request failed: {}".format(e)[:300]
            _fail("failed", reason)
            return [], "fetch_error", reason

    if adapter == "arcgis":
        try:
            series, status = arcgis.feed_series(
                feed_cfg, since=since_ym, until=as_of, session=session
            )
            return series, status, None
        except arcgis.ArcGISError as e:
            reason = "ArcGIS request failed: {}".format(e)[:300]
            _fail("failed", reason)
            return [], "fetch_error", reason

    # default: socrata
    try:
        series = socrata.feed_series(
            feed_cfg, city_cfg["host"], since=since_date, session=session
        )
        return series, "ok", None
    except socrata.SocrataError as e:
        reason = "Socrata request to {} failed: {}".format(
            city_cfg.get("host"), e)[:300]
        _fail("failed", reason)
        return [], "fetch_error", reason


# Status hints that override whatever the scorer derived from the data.
_OVERRIDE_STATUSES = ("stale", "not_published", "fetch_error")


def _apply_status_hint(feed_obj: dict, status_hint: str, reason) -> dict:
    """Apply an adapter status override and preserve its ``reason`` in ``note``.

    The note is where the page gets its copy for a non-scored feed, so a reason
    that stays only in stderr is a reason nobody will ever see.
    """
    if status_hint in _OVERRIDE_STATUSES:
        feed_obj["status"] = status_hint
    if reason:
        existing = feed_obj.get("note")
        feed_obj["note"] = "{}; {}".format(existing, reason) if existing else reason
    return feed_obj


def _score_feed_obj(feed_cfg: dict, city_cfg: dict, *, as_of: str, since_date: str,
                    session=None, diagnostics=None) -> dict:
    """Fetch one feed's series and score it into a schema feed object (with the
    adapter's stale/not_published/fetch_error status override). Shared by the
    pillar backbone and the P2 supplementary KPIs."""
    series, status_hint, reason = _fetch_feed_series(
        feed_cfg, city_cfg, as_of=as_of, since_date=since_date, session=session,
        diagnostics=diagnostics,
    )
    complete_through = _month_minus(as_of, 1) if _is_lagging(feed_cfg) else None
    feed_obj = pulse.score_feed(
        series,
        polarity=feed_cfg.get("polarity", 0),
        as_of=as_of,
        label=feed_cfg.get("label", "feed"),
        dataset=str(feed_cfg.get("dataset") or feed_cfg.get("ori")
                    or feed_cfg.get("endpoint") or ""),
        note=feed_cfg.get("note"),
        complete_through=complete_through,
    )
    _apply_status_hint(feed_obj, status_hint, reason)
    return feed_obj


def _load_extended_feeds() -> dict:
    """P2 supplementary feeds, keyed by city id. Returns {} if the file is absent
    (so P0/P1 builds work unchanged before the extended registry is produced)."""
    try:
        data = json.loads(EXTENDED_REGISTRY.read_text())
        return data.get("extended_feeds", {})
    except Exception:
        return {}


def build_city(city_cfg: dict, *, as_of: str, since_date: str, geo_cfg=None,
               extended_feed_cfgs=None, session=None, diagnostics=None) -> dict:
    """Build one city's schema object.

    ``diagnostics``, when a list, collects every degradation encountered (dead
    feed, failed adapter, missing credential) so ``main`` can print one summary
    at the end of the run. Nothing here is fatal — a single broken feed must
    degrade that feed and nothing else — but nothing here is silent either.
    """
    pillar_feeds = {}  # pillar key -> list of scored feed objs
    for feed_cfg in city_cfg.get("feeds", []):
        series, status_hint, reason = _fetch_feed_series(
            feed_cfg, city_cfg, as_of=as_of, since_date=since_date,
            session=session, diagnostics=diagnostics,
        )
        complete_through = _month_minus(as_of, 1) if _is_lagging(feed_cfg) else None
        feed_obj = pulse.score_feed(
            series,
            polarity=feed_cfg.get("polarity", 0),
            as_of=as_of,
            label=feed_cfg.get("label", "feed"),
            dataset=str(feed_cfg.get("dataset") or feed_cfg.get("ori")
                        or feed_cfg.get("endpoint") or ""),
            note=feed_cfg.get("note"),
            complete_through=complete_through,
        )
        # The adapter knows things the data alone can't say: a 2023 snapshot is
        # 'stale', a feed the city never published is 'not_published', and a
        # request that failed on our side is 'fetch_error'. Honor the override
        # so the feed is excluded from pillar math (coverage honesty), and carry
        # the reason into `note` so the page can say WHY.
        _apply_status_hint(feed_obj, status_hint, reason)
        pillar_feeds.setdefault(feed_cfg["pillar"], []).append(feed_obj)

    pillar_names = {
        "public_safety": "Public Safety",
        "development_economy": "Development & Economy",
        "city_services": "City Services",
    }
    pillar_objs = [
        pulse.score_pillar(key, pillar_names.get(key, key), feeds)
        for key, feeds in pillar_feeds.items()
    ]

    city_obj = pulse.score_city(
        id=city_cfg["id"],
        name=city_cfg["name"],
        scope=city_cfg.get("scope", "city"),
        pillar_objs=pillar_objs,
        disclosures=_city_disclosures(city_cfg),
    )
    # Layer B context (Census/BLS/AirNow). Null-safe: None when no source had a
    # key. build_context isolates each adapter internally and reports every
    # degradation into `diagnostics`, so this handler is now only a backstop for
    # a genuine bug in the composition itself — and a bug deserves a traceback,
    # not a one-line [skip] that reads like a routine missing key.
    try:
        city_obj["context"] = city_context.build_context(
            city_cfg, geo_cfg, session=session, diagnostics=diagnostics
        )
    except Exception as e:  # context is best-effort; never abort the build
        print("  [BUG] context composition raised for {} -> {}: {}".format(
            city_cfg.get("id"), type(e).__name__, e), file=sys.stderr)
        traceback.print_exc()
        if diagnostics is not None:
            diagnostics.append({
                "city": city_cfg.get("id", "?"), "source": "context",
                "kind": "bug", "detail": "{}: {}".format(type(e).__name__, e)[:300],
                "lost": "the entire context block",
            })

    # P2 supplementary KPIs — scored like backbone feeds but DISPLAY-ONLY
    # (never enter the pillar/composite math).
    if extended_feed_cfgs:
        ext = []
        for fc in extended_feed_cfgs:
            try:
                ext.append(_score_feed_obj(
                    fc, city_cfg, as_of=as_of, since_date=since_date,
                    session=session, diagnostics=diagnostics))
            except Exception as e:
                print("  [skip] extended {} {} -> {}".format(
                    city_cfg.get("id"), fc.get("label"), e), file=sys.stderr)
        if ext:
            city_obj["extended"] = ext

    return city_obj


def _resolve_as_of(cli_as_of, registry: dict, now: datetime) -> str:
    """Decide the most-recent-COMPLETE-month cutoff for this build.

    THIS IS THE FIX FOR THE CONTAINER-VS-CONTENTS FREEZE.

    ``as_of`` becomes every feed's ``complete_through``, and ``pulse.score_feed``
    drops every month after that cutoff. So ``as_of`` does not merely label the
    build — it decides how new the newest data point is allowed to be, and
    therefore what ``data_health.last_updated`` can possibly say.

    It used to be read from ``city_registry.resolved.json``'s
    ``_meta.as_of_complete_month``, a constant written by hand during Phase-0
    recon (``resolved_at: 2026-05-31``, value ``"2026-04"``). A registry is
    static configuration; "the last complete month" is a function of today's
    date. Freezing the second inside the first froze the whole payload: the
    nightly job kept re-fetching Chicago, NYC, LA, Seattle and SF, kept
    receiving May, June and July, and kept throwing all of it away at the
    cutoff — while ``generated_at`` was rewritten with ``_now_iso()`` every
    single night. Fresh container, embalmed contents. On 2026-08-03 that is
    three months of already-downloaded municipal data discarded per run.

    Precedence now:

      1. ``--as-of`` on the command line. Explicit and deliberate; needed for
         reproducible backfills and tests, and it stays absolute.
      2. The month before the current one, computed from the clock, every run.

    The registry value is demoted to a recon marker: it is compared, and a
    disagreement is REPORTED, because a stale pin is exactly the kind of thing
    that should be noisy rather than load-bearing. It is never used as the
    cutoff, so it can never clamp the build again.
    """
    live = _prev_complete_month(now)
    pinned = (registry.get("_meta", {}) or {}).get("as_of_complete_month")

    if cli_as_of:
        if pinned and pinned != cli_as_of:
            print("[fetch-city] --as-of {} overrides registry pin {} "
                  "(explicit request wins).".format(cli_as_of, pinned),
                  file=sys.stderr)
        return cli_as_of

    if pinned and pinned != live:
        print("[fetch-city] registry _meta.as_of_complete_month={} is stale "
              "(last complete month is {}); IGNORING the pin and using {}. "
              "A pinned cutoff silently discards every newer month the portals "
              "publish while generated_at keeps moving — the freeze this guard "
              "exists to prevent. Update or delete the pin in "
              "docs/city/city_registry.resolved.json.".format(pinned, live, live),
              file=sys.stderr)
    return live


def _report_diagnostics(diagnostics: list) -> None:
    """Print one build-level summary of everything that degraded.

    Six cities x (3 backbone feeds + extended + 3 context sources) is enough
    output that a single ``[skip]`` line scrolls past unread — which is how
    every ACS field stayed null across every city without anyone noticing. This
    collects the same events into one block at the end, grouped by cause, with
    the missing-credential ones called out separately because those are the
    only ones a human can actually fix.
    """
    if not diagnostics:
        print("[fetch-city] no degradations: every feed and context source "
              "returned data.")
        return

    no_key = [d for d in diagnostics if d.get("kind") == "no key"]
    failed = [d for d in diagnostics if d.get("kind") in ("failed", "bug")]
    other = [d for d in diagnostics
             if d.get("kind") not in ("no key", "failed", "bug")]

    print("")
    print("[fetch-city] DEGRADATION SUMMARY — {} event(s). Nothing below "
          "aborted the build; all of it is missing from the payload."
          .format(len(diagnostics)), file=sys.stderr)

    if no_key:
        print("  MISSING CREDENTIALS ({}) — an operator must act; no request "
              "was even sent:".format(len(no_key)), file=sys.stderr)
        # One line per distinct source, listing which cities it cost us.
        by_source = {}
        for d in no_key:
            by_source.setdefault((d["source"], d["detail"], d["lost"]), []).append(
                d["city"])
        for (source, detail, lost), cities in sorted(by_source.items()):
            # Redacted like the other two loops below. In this branch `detail`
            # is normally the _KEY_HELP signup text rather than an upstream
            # error, so there is nothing secret in it today — but "today" is
            # not a security property, and a third printer that treats the
            # same field differently from its two siblings is how the next
            # leak gets in.
            print("    - {}: null {} for {} ({})".format(
                source, lost, ", ".join(cities), redact(detail)), file=sys.stderr)

    if failed:
        print("  REQUESTS THAT FAILED ({}) — on our side, not the publisher's:"
              .format(len(failed)), file=sys.stderr)
        for d in failed:
            print("    - {} {}: {} ({})".format(
                d["city"], d["source"], d["lost"], redact(d["detail"])), file=sys.stderr)

    if other:
        print("  EMPTY RESPONSES ({}) — the call worked and returned nothing:"
              .format(len(other)), file=sys.stderr)
        for d in other:
            print("    - {} {}: {} ({})".format(
                d["city"], d["source"], d["lost"], redact(d["detail"])), file=sys.stderr)
    print("", file=sys.stderr)


def _fresh(out_path: Path, now: datetime) -> bool:
    try:
        prior = json.loads(out_path.read_text())
        ts = prior.get("generated_at", "")
        gen = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return (now - gen).total_seconds() < FRESH_SECONDS and not prior.get("_mock")
    except Exception:
        return False


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Build data-city.json (City Pulse).")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="output JSON path")
    ap.add_argument("--as-of", default=None, help="most recent complete month YYYY-MM")
    ap.add_argument("--baseline-months-back", type=int, default=37,
                    help="how many months of history to request "
                         "(37 = 36 months of trend series + a boundary month)")
    ap.add_argument("--force", action="store_true",
                    help="ignore the 24h freshness guard")
    args = ap.parse_args(argv)

    out_path = Path(args.out)
    now = datetime.now(timezone.utc)
    if not args.force and _fresh(out_path, now):
        print("[fetch-city] {} is <24h old; skipping (use --force).".format(out_path))
        return 0

    try:
        registry = json.loads(REGISTRY.read_text())
    except Exception as e:
        print("[fetch-city] cannot read registry {}: {}".format(REGISTRY, e),
              file=sys.stderr)
        return 1

    as_of = _resolve_as_of(args.as_of, registry, now)
    since_date = _month_minus(as_of, args.baseline_months_back) + "-01"

    geo_by_city = (registry.get("context_layer", {})
                   .get("sources", {}).get("census_acs", {}).get("geo_by_city", {}))
    extended_feeds = _load_extended_feeds()

    diagnostics: list = []
    cities = []
    for city_cfg in registry.get("cities", []):
        try:
            cities.append(build_city(
                city_cfg, as_of=as_of, since_date=since_date,
                geo_cfg=geo_by_city.get(city_cfg["id"]),
                extended_feed_cfgs=extended_feeds.get(city_cfg["id"]),
                diagnostics=diagnostics,
            ))
        except Exception as e:  # never let one city abort the build
            print("[fetch-city] city {} failed: {}".format(
                city_cfg.get("id"), e), file=sys.stderr)
            traceback.print_exc()
            diagnostics.append({
                "city": city_cfg.get("id", "?"), "source": "build_city",
                "kind": "bug", "detail": "{}: {}".format(type(e).__name__, e)[:300],
                "lost": "the whole city",
            })

    # P2 transparent cross-city Context composite (post-pass: min-max needs all
    # cities' context at once). Optional/guarded — no-ops until the scorer lands.
    disclosures = list(METHODOLOGY_DISCLOSURES)
    try:
        from city import context_score
        scores = context_score.score_context(
            {c["id"]: c.get("context") for c in cities})
        for c in cities:
            ctx = c.get("context")
            if ctx is not None and scores.get(c["id"]) is not None:
                ctx["context_score"] = scores[c["id"]]
        disclosures.extend(context_score.context_score_disclosures())
    except Exception as e:
        print("[fetch-city] context_score skipped: {}".format(e), file=sys.stderr)

    payload = pulse.score_payload(
        cities,
        as_of=as_of,
        methodology_disclosures=disclosures,
        generated_at=_now_iso(),
    )

    out_path.write_text(json.dumps(payload, indent=2, allow_nan=False))
    scored = sum(1 for c in cities if (c.get("pulse") or {}).get("score") is not None)
    print("[fetch-city] wrote {} ({} cities, {} with a Pulse score, as_of={})".format(
        out_path, len(cities), scored, as_of))

    # What the payload actually SAYS about its own age, printed next to the
    # write so a CI log shows the contents' age and not just the build's.
    # generated_at is the age of this process; last_updated is the age of the
    # data, and only the second one means anything.
    for c in cities:
        lu = (c.get("data_health") or {}).get("last_updated")
        print("[fetch-city]   {:<8} data through {}  ({} of {} feeds ok)".format(
            c.get("id", "?"),
            lu[:10] if lu else "NOTHING — no feed produced a reading",
            (c.get("data_health") or {}).get("feeds_ok"),
            (c.get("data_health") or {}).get("feeds_total")))

    _report_diagnostics(diagnostics)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
