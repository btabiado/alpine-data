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
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from city import socrata, arcgis, pulse
from city import context as city_context

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
                       since_date: str, session=None):
    """Return ``(series, status_hint)``. ``status_hint`` is 'ok' for a normal
    fetch, or 'stale' / 'not_published' to OVERRIDE the scorer's data-derived
    status (Miami 311 snapshot / FBI-pending / hard fetch failure)."""
    adapter = feed_cfg.get("adapter") or city_cfg.get("adapter")
    since_ym = since_date[:7]

    if adapter == "fbi":
        # Miami Public Safety via FBI CDE. Returns [] (-> not_published) when
        # FBI_CDE_API_KEY is unset; otherwise the offense series scores normally.
        try:
            series = city_context.fbi_crime_series(
                feed_cfg, since=since_ym, until=as_of, session=session
            )
            return series, ("ok" if series else "not_published")
        except Exception as e:  # FBI is best-effort; never abort the build
            print("  [skip] fbi {} -> {}".format(feed_cfg.get("label"), e),
                  file=sys.stderr)
            return [], "not_published"

    if adapter == "arcgis":
        try:
            series, status = arcgis.feed_series(
                feed_cfg, since=since_ym, until=as_of, session=session
            )
            return series, status
        except arcgis.ArcGISError as e:
            print("  [skip] arcgis {} -> {}".format(feed_cfg.get("label"), e),
                  file=sys.stderr)
            return [], "not_published"

    # default: socrata
    try:
        series = socrata.feed_series(
            feed_cfg, city_cfg["host"], since=since_date, session=session
        )
        return series, "ok"
    except socrata.SocrataError as e:
        print("  [skip] socrata {} -> {}".format(feed_cfg.get("label"), e),
              file=sys.stderr)
        return [], "not_published"


def _score_feed_obj(feed_cfg: dict, city_cfg: dict, *, as_of: str, since_date: str,
                    session=None) -> dict:
    """Fetch one feed's series and score it into a schema feed object (with the
    adapter's stale/not_published status override). Shared by the pillar backbone
    and the P2 supplementary KPIs."""
    series, status_hint = _fetch_feed_series(
        feed_cfg, city_cfg, as_of=as_of, since_date=since_date, session=session
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
    if status_hint in ("stale", "not_published"):
        feed_obj["status"] = status_hint
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
               extended_feed_cfgs=None, session=None) -> dict:
    pillar_feeds = {}  # pillar key -> list of scored feed objs
    for feed_cfg in city_cfg.get("feeds", []):
        series, status_hint = _fetch_feed_series(
            feed_cfg, city_cfg, as_of=as_of, since_date=since_date, session=session
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
        # 'stale', an FBI-pending feed is 'not_published'. Honor that override so
        # the feed is excluded from pillar math (coverage honesty).
        if status_hint in ("stale", "not_published"):
            feed_obj["status"] = status_hint
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
    # Layer B context (Census/BLS/AirNow). Null-safe: None when no source had a key.
    try:
        city_obj["context"] = city_context.build_context(
            city_cfg, geo_cfg, session=session
        )
    except Exception as e:  # context is best-effort; never abort the build
        print("  [skip] context {} -> {}".format(city_cfg.get("id"), e),
              file=sys.stderr)

    # P2 supplementary KPIs — scored like backbone feeds but DISPLAY-ONLY
    # (never enter the pillar/composite math).
    if extended_feed_cfgs:
        ext = []
        for fc in extended_feed_cfgs:
            try:
                ext.append(_score_feed_obj(
                    fc, city_cfg, as_of=as_of, since_date=since_date, session=session))
            except Exception as e:
                print("  [skip] extended {} {} -> {}".format(
                    city_cfg.get("id"), fc.get("label"), e), file=sys.stderr)
        if ext:
            city_obj["extended"] = ext

    return city_obj


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

    as_of = (args.as_of
             or registry.get("_meta", {}).get("as_of_complete_month")
             or _prev_complete_month(now))
    since_date = _month_minus(as_of, args.baseline_months_back) + "-01"

    geo_by_city = (registry.get("context_layer", {})
                   .get("sources", {}).get("census_acs", {}).get("geo_by_city", {}))
    extended_feeds = _load_extended_feeds()

    cities = []
    for city_cfg in registry.get("cities", []):
        try:
            cities.append(build_city(
                city_cfg, as_of=as_of, since_date=since_date,
                geo_cfg=geo_by_city.get(city_cfg["id"]),
                extended_feed_cfgs=extended_feeds.get(city_cfg["id"]),
            ))
        except Exception as e:  # never let one city abort the build
            print("[fetch-city] city {} failed: {}".format(
                city_cfg.get("id"), e), file=sys.stderr)

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

    out_path.write_text(json.dumps(payload, indent=2))
    scored = sum(1 for c in cities if (c.get("pulse") or {}).get("score") is not None)
    print("[fetch-city] wrote {} ({} cities, {} with a Pulse score, as_of={})".format(
        out_path, len(cities), scored, as_of))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
