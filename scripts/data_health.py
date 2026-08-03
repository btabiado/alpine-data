#!/usr/bin/env python3
"""Data-feed watchdog with derived coverage, expiring suppressions, and real alarms.

Why this replaces `check_data_freshness.py`
-------------------------------------------
That script was written the day we found TSA 45 days stale, and its own
docstring names the failure it was fixing:

    "its cron had failed on 30+ consecutive days ... and nothing surfaced
     that: the workflow just went red on a page nobody opens."

It then fixed that by adding *another workflow that goes red on a page nobody
opens*. It also shipped four structural holes that let the June freeze cluster
(MUFON, stock money-flow, POC/breadth, Summit news) happen underneath a green
check. All five are addressed here.

  1. COVERAGE WAS CURATED, NOT DERIVED.
     `TRACKED` was a hand-written dict of 10 paths. The repo has ~23 data
     artifacts. MUFON, stock money-flow and Summit news froze for two months
     *precisely because they were never added to the list* — and nothing
     noticed the list was incomplete, because an unlisted feed is
     indistinguishable from a healthy one.
     -> Every artifact on disk must be classified in MANIFEST. An unclassified
        file, or an unclassified data/ subdirectory, is itself a FAILURE
        ("unwatched"). Adding a feed without adding monitoring now breaks the
        check instead of silently escaping it.

  2. SUPPRESSIONS NEVER EXPIRED.
     `KNOWN_BLOCKED` muted a feed forever with a free-text reason and no owner,
     no ticket, no review date. Both crypto-flow entries still read "needs
     COINGLASS_API_KEY; free mirror is dead" — but that path was replaced by
     scripts/fetch_etf_flows.py (Farside, keyless) with its own cron. The
     suppression outlived its cause and would have masked the *new* fetcher
     failing, indefinitely.
     -> A Suppression carries a hard `until` date. Past it, the feed fails
        normally and the alarm says the suppression expired. Muting is a loan,
        not a grant. tests/test_data_health.py fails the build on an expired
        entry, so a mute cannot be renewed by inattention.

  3. THE DEPLOY-TIME EXEMPTION RESTED ON A FALSE PREMISE.
     The old comment excluded market.json / whale.json because they are
     "regenerated at deploy time (fresh by construction on the live site)".
     That is exactly backwards, and it is the motivating incident: fetch_market
     preserves last-known-good on failure, so market.json was rewritten every
     single hour while the crypto breadth series inside it sat frozen at
     2026-06-09. The FILE was fresh by construction; the DATA was three months
     old. Freshness of a container says nothing about freshness of contents.
     -> BUILT artifacts are not exempt. They are checked in `--mode built`
        against the real generated file, not against the committed placeholder.

  4. "UNDETERMINED" EXITED ZERO.
     A file whose date signal could not be parsed landed in `unknown` and the
     script still returned 0. So a feed that becomes structurally unreadable —
     the exact symptom of an upstream schema change — reported as not-a-problem.
     -> UNKNOWN is a failure. If a feed cannot be evaluated, the monitor has
        lost the ability to monitor it, which is strictly worse than stale.

  5. NOTHING PAGED ANYONE.
     Exit 1 turns an Actions run red. That is the same void that swallowed 30
     consecutive TSA failures.
     -> `--report issue` emits a body the workflow turns into an auto-filed
        GitHub issue: opened on first failure, edited in place while it
        persists (so it never spams), closed automatically when everything
        recovers. An issue notifies, appears on the repo home, and survives
        being ignored for a week.

And one hole the old script shared with the dashboard it was guarding:

  6. A CONTAINER'S DATE SAID NOTHING ABOUT ITS CONTENTS.
     data-city.json reads 13.6h fresh from its top-level `generated_at` while
     every `cities[].data_health.last_updated` inside it says 2026-04-01 —
     four months old. Identical in shape to the breadth freeze, in a feed the
     old watchdog called healthy.
     -> build_health_status.NESTED_DATE_PATHS declares, per feed, where the
        contents state their own age. The reported age is the OLDER of
        container and contents, because a composite is only as fresh as its
        oldest input. It lives in build_health_status, not here, so /health/
        and this watchdog resolve age through identical code.

Freshness is judged on file CONTENT (last row date / generated_at), never on
mtime: a stateless CI checkout rewrites every mtime on every run, which would
make every feed look perpetually fresh. This is inherited from
build_health_status._content_age_probe and is the one thing the old script got
unambiguously right.

Usage
-----
    python scripts/data_health.py                     # committed artifacts
    python scripts/data_health.py --mode built        # after a pages.yml build
    python scripts/data_health.py --report json       # machine-readable
    python scripts/data_health.py --remediate         # try to self-heal first

Exit codes: 0 = healthy, 1 = at least one feed stale/unknown/unwatched.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
# Age resolution lives in build_health_status so the watchdog and /health/
# cannot disagree about what "stale" means. `_select` and `nested_age_h` are
# imported purely to RE-EXPORT them under this module's name: they are the
# nested-date primitives, they are exercised by tests/test_data_health.py, and
# a reader looking for "how does this monitor read a nested date" should find
# them here rather than having to know they were hoisted.
from build_health_status import (  # noqa: E402  (path set above)
    DEFAULT,
    THRESHOLDS,
    AgeProbe,
    _select,          # noqa: F401  re-export
    humanize_age,
    nested_age_h,     # noqa: F401  re-export
    resolve_age,
)
from build_health_status import NESTED_DATE_PATHS as _NESTED_DATE_PATHS  # noqa: E402

# Overridable so the monitor can be pointed at a fixture tree in tests without
# a chdir dance. Defaults to the real repo root in every normal invocation.
REPO_ROOT = Path(os.environ.get("ALPINE_REPO_ROOT")
                 or Path(__file__).resolve().parent.parent)


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------

COMMITTED = "committed"   # a cron refreshes it and commits it back
BUILT = "built"           # pages.yml regenerates it at deploy time
SERIES = "series"         # a directory of dated files; the NEWEST one is the feed
STATIC = "static"         # reference data that legitimately does not change
DELEGATED = "delegated"   # watched by a different system; declared so it is not UNWATCHED


@dataclass(frozen=True)
class Feed:
    """One monitored artifact.

    `owner` is printed in every alarm so the fix path is obvious rather than
    something to re-derive at 2am. `refresher` is the command the --remediate
    path runs to try to self-heal; None means no safe automatic retry exists.
    """
    kind: str
    owner: str
    refresher: str | None = None
    limit_h: float | None = None    # overrides THRESHOLDS when the cadence is unusual
    justification: str = ""         # required for STATIC and DELEGATED, enforced below
    series_glob: str = "*.json"     # SERIES only: which files in the directory count


@dataclass(frozen=True)
class Suppression:
    """A time-boxed mute. Expires on purpose."""
    reason: str
    until: date
    tracked_in: str = ""


# Every data artifact in the repo must appear here. See hole #1 above: the
# point is that adding a feed WITHOUT adding monitoring is a failure.
#
# COMMITTED vs BUILT was re-derived from `git ls-files` against the tree at
# f28cbdb, not from intuition and not from the previous revision of this file.
# Getting this backwards is not cosmetic: a COMMITTED feed marked BUILT stops
# being checked in the daily cron (which is how a feed rots), and a BUILT feed
# marked COMMITTED fails forever against a placeholder (which trains everyone
# to ignore the alarm). Both failure modes have already happened in this repo.
MANIFEST: dict[str, Feed] = {
    # --- committed by their own cron ---------------------------------------
    "data-tsa.json": Feed(
        COMMITTED, "aviation-tsa.yml (daily 14:10Z)", "python fetch_tsa.py"),
    "data-city.json": Feed(
        COMMITTED, "city-daily.yml (daily 06:00Z)", "python fetch_city.py"),
    "data-cfpb.json": Feed(
        COMMITTED, "cfpb-daily.yml (daily)", "python scripts/fetch_cfpb.py"),
    "data-usaspending.json": Feed(
        COMMITTED, "usaspending-daily.yml (daily)",
        "python scripts/fetch_usaspending.py"),
    "data-opensky.json": Feed(
        COMMITTED, "aviation-opensky.yml (hourly)", "python fetch_opensky.py"),
    "data-opensky-positions.json": Feed(
        COMMITTED, "aviation-opensky.yml (hourly)", "python fetch_opensky.py"),
    "data-aviation.json": Feed(
        # No dedicated fetch_aviation.py exists; the file is consumed by app.py
        # and health/build_r2_coverage.py. Refresher intentionally left None
        # rather than guessed — an auto-retry that runs the wrong script is
        # worse than no auto-retry.
        COMMITTED, "hand-refreshed; no fetch_aviation.py at root — identify the "
                   "owner before enabling auto-remediation",
        # 400 DAYS, not the 24h default. This file is a COMPOSITE of three
        # vintages and data_date is the OLDEST of them (rule 2) — the FAA
        # airman roll, which FAA publishes ANNUALLY (currently 2025-12-31).
        # Under the default it reports 215d/24h STALE today and every day
        # after, forever. That is not vigilance, it is a permanently red light
        # that teaches everyone to ignore the monitor — the precise failure
        # this file's docstring blames for 30 unnoticed TSA failures.
        # 400d gives the annual roll a ~5-week grace window before alarming.
        #
        # ACCEPTED COST, stated plainly: because the composite takes the oldest
        # component, a 400d budget also means the two ~monthly components
        # (registry, market snapshot) could freeze for over a year without
        # tripping this check. Fixing that properly means watching the three
        # components separately, which needs an owner for the file first.
        limit_h=400 * 24.0),
    "data/real_estate.json": Feed(
        COMMITTED, "real-estate-daily.yml (daily)",
        "python scripts/fetch_real_estate.py"),
    "data/ai_curated.json": Feed(
        COMMITTED, "insights.py, inside pages.yml"),
    "data/equity_etf_flows.csv": Feed(
        COMMITTED, "money-flow-daily.yml (daily 08:30Z)",
        # Trading-day feed: a 24h limit red-flags it every Saturday and Sunday,
        # which is noise. Friday's row is ~3d old by Monday; a Monday holiday
        # pushes it to ~4d.
        limit_h=96.0),
    "data/btc_flows.csv": Feed(
        COMMITTED, "etf-flows-daily.yml (daily 09:15Z)",
        "python scripts/fetch_etf_flows.py", limit_h=96.0),
    "data/eth_flows.csv": Feed(
        COMMITTED, "etf-flows-daily.yml (daily 09:15Z)",
        "python scripts/fetch_etf_flows.py", limit_h=96.0),
    "data-travel.json": Feed(
        COMMITTED, "fetch_advisories.py, inside pages.yml",
        "python fetch_advisories.py"),

    # --- THE GAP THAT CAUSED THE JUNE FREEZE -------------------------------
    # None of these three was watched. All three froze. That is not a
    # coincidence — they froze *because* nothing was watching, so the V2 build
    # timeout that starved them produced no signal for eight weeks.
    "data-mufon.json": Feed(
        COMMITTED, "fetch_mufon.py, inside pages.yml (V2 step)",
        "python fetch_mufon.py"),
    "data-stock-money-flow.json": Feed(
        # Corrected owner: the standalone daily cron referenced by .gitignore
        # (stock-money-flow-daily.yml) does not exist. pages.yml line ~113
        # records that it was REMOVED after Yahoo throttled it to 0/219, and
        # the sidecar is now written inside the fetch-market step by
        # fetch_market.fetch_all() -> fetch_stock_money_flow.build_from_signals().
        # Pointing an alarm at a workflow that was deleted is how you get an
        # alarm nobody can act on.
        COMMITTED, "fetch_market.py --fetch-market step in pages.yml (via "
                   "fetch_stock_money_flow.build_from_signals)"),
    "snowflake_summit/news.json": Feed(
        COMMITTED, "snowflake_summit/enrich_vendors.py (manual)"),
    "snowflake_summit/vendors.json": Feed(
        # Found by the drift detector this PR added to _content_age_probe, not
        # by reading the directory: its only stamp is nested at
        # `_meta.generated`, so /health/ was scoring it off mtime and this
        # monitor did not cover it at all. Exactly the class of miss the
        # detector exists to surface.
        COMMITTED, "snowflake_summit/enrich_vendors.py (manual)"),
    "snowflake_summit/floorplan.json": Feed(
        STATIC, "hand-authored from the Summit venue map",
        justification="Booth geometry for a conference that already happened: "
                      "canvas dimensions and region polygons. There is no "
                      "upstream to refresh from, which is why the file carries "
                      "no date."),

    # --- a committed time series (one file per day) -------------------------
    # Added since the previous revision of this manifest: PR #23 landed
    # scripts/snapshot_composites.py and pages.yml commits data/composites/
    # back as composites-bot. A directory feed rots differently from a file
    # feed — the newest file simply stops appearing — so it is judged on its
    # newest member, not on the directory's mtime.
    "data/composites/": Feed(
        SERIES, "scripts/snapshot_composites.py, committed by pages.yml "
                "(composites-bot)",
        "python scripts/snapshot_composites.py",
        # One snapshot per pages build. 48h tolerates a quiet weekend without
        # tolerating a genuinely dead snapshotter.
        limit_h=48.0),

    # --- regenerated at deploy time (gitignored placeholders in the repo) ---
    # NOT exempt. See hole #3: market-derived files are rewritten hourly while
    # their contents stale-keep, which is the exact shape of the breadth freeze.
    "data-whale.json": Feed(BUILT, "fetch_market.py, inside pages.yml"),
    "data-metals.json": Feed(BUILT, "fetch_metals.py, inside pages.yml"),
    "data-supplies.json": Feed(BUILT, "fetch_supplies.py, inside pages.yml"),
    "data-cpi.json": Feed(BUILT, "fetch_cpi.py, inside pages.yml"),
    "data-travel-fetch-status.json": Feed(
        BUILT, "fetch_advisories.py (status record added in PR #24)"),
    "data/ai_curated_wiki.json": Feed(BUILT, "insights.py / wiki_enrich, inside pages.yml"),
    "data/market.json": Feed(BUILT, "fetch_market.py, inside pages.yml"),
    "data/whale.json": Feed(BUILT, "fetch_market.py, inside pages.yml"),
    "data/coinbase.json": Feed(BUILT, "fetch_coinbase.py, inside pages.yml"),
    "data/cpi.json": Feed(BUILT, "fetch_cpi.py, inside pages.yml"),
    "data/metals.json": Feed(BUILT, "fetch_metals.py, inside pages.yml"),
    "data/supplies.json": Feed(BUILT, "fetch_supplies.py, inside pages.yml"),
    "data/shares.json": Feed(BUILT, "shares.py, inside pages.yml"),
    "data/insights_history.json": Feed(BUILT, "insights.py, inside pages.yml"),

    # --- legitimately static ------------------------------------------------
    "data/metro_coords.json": Feed(
        STATIC, "scripts/fetch_metro_coords.py (manual)",
        justification="Census CBSA gazetteer centroids for US metros. Changes "
                      "only when the Census publishes a new annual gazetteer, "
                      "which is a deliberate PR, never a feed refresh."),
    "data-us_states.json": Feed(
        # RECLASSIFIED from COMMITTED. The previous revision of this manifest
        # listed it as a city-daily.yml feed. It is not: city-daily.yml's
        # commit step does `git add data-city.json` and nothing else, and the
        # file contains no date field of any kind — 51 SVG path strings and
        # nothing else. Marked COMMITTED it would have reported UNKNOWN
        # forever, which is exactly the cry-wolf red that teaches people to
        # ignore the monitor.
        STATIC, "scripts/build_us_state_paths.py (one-off, from us-atlas)",
        justification="Pre-projected Albers USA SVG paths for 50 states + DC. "
                      "State boundaries do not move; the file has no date "
                      "field because there is no refresh to date."),

    # --- watched by a different system --------------------------------------
    "data/lthcs/": Feed(
        DELEGATED, "lthcs-daily.yml and friends",
        justification="LTHCS has its own freshness pipeline "
                      "(scripts/lthcs_audit_data_quality.py, "
                      "tests/test_lthcs_freshness.py) and ~thousands of dated "
                      "snapshot files. Declared here so a NEW data/ "
                      "subdirectory still trips the unwatched check, without "
                      "this monitor duplicating that one."),
    "data/health/": Feed(
        DELEGATED, "scripts/build_health_status.py, inside pages.yml",
        justification="This monitor's sibling output, regenerated from scratch "
                      "on every build and gitignored. Watching the watchdog's "
                      "own report for staleness measures nothing."),
    "snowflake_summit/enrichment.json": Feed(
        DELEGATED, "snowflake_summit/enrich_vendors.py",
        justification="Gitignored intermediate the enricher writes on its way "
                      "to vendors.json/news.json, never deployed and never "
                      "read by the dashboard. Its age is a property of when "
                      "someone last ran the enricher locally, which the two "
                      "committed outputs already report."),
    "data/.stale/": Feed(
        DELEGATED, "the individual fetchers' stale-keep caches",
        justification="Last-known-good caches for flaky upstreams, plus the "
                      "committed NUFORC subndx backfill. Their age is reported "
                      "on /health/ by build_health_status.collect_stale(); "
                      "alarming on a fallback cache being old says nothing "
                      "about whether the live feed is working."),
}


# The table of "where does this feed's CONTENT state its own age" is defined in
# build_health_status, NOT here. Keeping it in the shared module is what makes
# /health/ and this watchdog resolve age through identical code — two monitors
# that can disagree about "stale" is worse than one, because the disagreement
# becomes the thing people argue about instead of the feed. Bound to a local
# name so this module reads normally and verify_manifest() can cross-check that
# every declared nested path belongs to a feed the manifest actually knows.
NESTED_DATE_PATHS = _NESTED_DATE_PATHS


# Time-boxed mutes. Every entry MUST justify itself and MUST expire.
# Re-validate on expiry rather than extending reflexively — an entry that has
# been renewed three times is telling you the fix is never coming, and should
# either be fixed properly or the feed retired from the dashboard.
SUPPRESSIONS: dict[str, Suppression] = {
    "data-tsa.json": Suppression(
        reason="tsa.gov 403s GitHub Actions IPs even with a browser UA. Needs a "
               "non-datacenter egress path (residential proxy or a self-hosted "
               "runner); no code change can fix it from CI.",
        until=date(2026, 9, 15),
        tracked_in="needs a decision on proxy vs. dropping the feed"),
    # NOTE: the crypto-flow suppressions that used to live here were REMOVED,
    # not renewed. Their stated blocker ("needs COINGLASS_API_KEY; free mirror
    # is dead") stopped being true when scripts/fetch_etf_flows.py landed with
    # its own keyless Farside path and a cron. Leaving them would have muted
    # the new fetcher's failures too — which is hole #2 in miniature.
    #
    # data-travel.json is likewise NOT suppressed any more: PR #24 added an RSS
    # fallback plus data-travel-fetch-status.json, so a continued failure is now
    # diagnosable and should be loud.
    #
    # data-aviation.json is deliberately NOT suppressed either. Its `asOf` is
    # prose ("FAA airman data Dec 31 2025 · ..."), so the monitor genuinely
    # cannot evaluate it and reports UNKNOWN. That is the correct, loud answer:
    # the fix is a machine-readable stamp in the file, and muting it would
    # re-create hole #4 by hand.
}


# --------------------------------------------------------------------------
# Evaluation
# --------------------------------------------------------------------------

OK, STALE, UNKNOWN, MISSING, UNWATCHED, SUPPRESSED, EXPIRED, SKIPPED = (
    "ok", "stale", "unknown", "missing", "unwatched", "suppressed", "expired",
    "skipped")

FAILING_STATUSES = frozenset({STALE, UNKNOWN, MISSING, UNWATCHED, EXPIRED})


@dataclass
class Result:
    path: str
    status: str
    age_h: float | None = None
    limit_h: float | None = None
    owner: str = ""
    detail: str = ""
    source: str = ""     # which date field the age came from

    @property
    def fails(self) -> bool:
        return self.status in FAILING_STATUSES

    def line(self) -> str:
        age = humanize_age(self.age_h) if self.age_h is not None else "?"
        lim = humanize_age(self.limit_h) if self.limit_h is not None else "?"
        base = f"{self.path}: {age} old (limit {lim})"
        if self.source:
            base += f" via {self.source}"
        if self.owner:
            base += f" - {self.owner}"
        if self.detail:
            base += f"\n      {self.detail}"
        return base


def discover() -> list[str]:
    """Every data artifact actually present, so coverage is derived not curated.

    Returns files AND data/ subdirectories (with a trailing slash), because a
    whole new directory of feeds escaping the manifest is the same hole as a
    single file escaping it, only bigger.
    """
    found: set[str] = set()
    for pattern in ("data-*.json", "data/*.json", "data/*.csv",
                    "snowflake_summit/*.json"):
        for p in REPO_ROOT.glob(pattern):
            # Dot-prefixed FILES are tool scratch by universal convention
            # (snowflake_summit/.enrich_cache.json); build_health_status.scan
            # skips them for the same reason. Dot-prefixed DIRECTORIES are NOT
            # skipped below, because data/.stale/ holds committed data.
            if p.is_file() and not p.name.startswith("."):
                found.add(p.relative_to(REPO_ROOT).as_posix())
    data_dir = REPO_ROOT / "data"
    if data_dir.is_dir():
        for p in data_dir.iterdir():
            if p.is_dir():
                found.add(p.relative_to(REPO_ROOT).as_posix() + "/")
    return sorted(found)


def verify_manifest() -> list[str]:
    """Structural problems in the manifest itself, independent of any data.

    Kept as a pure function so tests can assert on it without touching disk;
    a manifest that contradicts itself is a monitoring outage in waiting.
    """
    problems: list[str] = []
    for rel, feed in MANIFEST.items():
        if feed.kind not in (COMMITTED, BUILT, SERIES, STATIC, DELEGATED):
            problems.append(f"{rel}: unknown kind {feed.kind!r}")
        if feed.kind in (STATIC, DELEGATED) and not feed.justification.strip():
            problems.append(
                f"{rel}: declared {feed.kind.upper()} without a justification. "
                f"Exempting a feed from monitoring requires saying why in "
                f"writing, so the next reader can disagree.")
        if feed.kind == SERIES and not rel.endswith("/"):
            problems.append(f"{rel}: SERIES entries name a directory of dated "
                            f"files and must end with '/'")
        if feed.kind not in (SERIES, DELEGATED) and rel.endswith("/"):
            problems.append(f"{rel}: only SERIES/DELEGATED entries may name a "
                            f"directory")
        if not feed.owner.strip():
            problems.append(f"{rel}: no owner — an alarm nobody owns is noise")
    for rel in SUPPRESSIONS:
        if rel not in MANIFEST:
            problems.append(f"suppression for {rel!r} has no MANIFEST entry")
    for rel, specs in NESTED_DATE_PATHS.items():
        if rel not in MANIFEST:
            problems.append(f"nested date path for {rel!r} has no MANIFEST entry")
        if not specs:
            problems.append(f"nested date path for {rel!r} is empty")
    return problems


def _probe_feed(rel: str, feed: Feed, now: float) -> "tuple[Path, AgeProbe, str]":
    """Resolve one feed to (path judged, age probe, human source label).

    Age comes from build_health_status.resolve_age, which already folds in
    NESTED_DATE_PATHS and takes the older of container and contents — so the
    watchdog and /health/ can never report different ages for the same file.
    """
    path = REPO_ROOT / rel
    if feed.kind == SERIES:
        members = sorted(p for p in path.glob(feed.series_glob) if p.is_file())
        if not members:
            return path, AgeProbe(note="directory exists but holds no members"), ""
        # Lexicographic == chronological for the YYYY-MM-DD.json naming this
        # repo uses for every dated series.
        newest = members[-1]
        probe = resolve_age(newest, now, f"{rel}{newest.name}")
        return newest, probe, (f"newest of {len(members)} in {rel} "
                               f"({newest.name}){f' {probe.key}' if probe.key else ''}")
    probe = resolve_age(path, now, rel)
    return path, probe, probe.key or ""


def evaluate(mode: str, today: date | None = None,
             now_ts: float | None = None) -> list[Result]:
    now = now_ts if now_ts is not None else datetime.now(timezone.utc).timestamp()
    today = today or datetime.fromtimestamp(now, timezone.utc).date()
    results: list[Result] = []

    for problem in verify_manifest():
        results.append(Result("MANIFEST", UNWATCHED, detail=problem))

    on_disk = set(discover())
    declared = set(MANIFEST)

    # Hole #1: anything present but unclassified is a finding in its own right.
    for rel in sorted(on_disk - declared):
        results.append(Result(
            rel, UNWATCHED,
            detail="present on disk but absent from MANIFEST. Classify it "
                   "(committed/built/series/static/delegated) so it cannot rot "
                   "unobserved."))

    for rel in sorted(declared):
        feed = MANIFEST[rel]
        path = REPO_ROOT / rel

        if feed.kind in (STATIC, DELEGATED):
            results.append(Result(rel, SKIPPED, owner=feed.owner,
                                  detail=f"{feed.kind}: {feed.justification}"))
            continue

        # A BUILT artifact's committed copy is a placeholder (or absent
        # entirely); judging it in committed mode is meaningless. It is judged
        # in --mode built instead, against the real post-build file. It is NOT
        # skipped in both modes — that was hole #3.
        if feed.kind == BUILT and mode != BUILT:
            continue

        if not path.exists():
            results.append(Result(rel, MISSING, owner=feed.owner,
                                  detail=f"expected on disk; refreshed by {feed.owner}"))
            continue

        # Hole #6 (container vs contents) is already folded in by resolve_age:
        # `probe.age_h` is the OLDER of the file's own stamp and whatever its
        # contents say, and `probe.note` explains it when they disagreed.
        judged, probe, source = _probe_feed(rel, feed, now)
        age_h = probe.age_h
        detail = probe.note if age_h is not None else ""

        if age_h is None:
            # Hole #4: this used to exit 0.
            results.append(Result(
                rel, UNKNOWN, owner=feed.owner, source=source,
                detail=("no readable date signal inside the file. The monitor "
                        "cannot evaluate this feed, which is worse than stale: "
                        "an upstream schema change looks identical to health. "
                        f"Parser says: {probe.note}")))
            continue

        limit = feed.limit_h or THRESHOLDS.get(judged.name, DEFAULT).stale_h
        if age_h <= limit:
            results.append(Result(rel, OK, age_h, limit, feed.owner, detail, source))
            continue

        sup = SUPPRESSIONS.get(rel)
        if sup and today <= sup.until:
            results.append(Result(
                rel, SUPPRESSED, age_h, limit, feed.owner,
                detail=f"muted until {sup.until.isoformat()}: {sup.reason}",
                source=source))
        elif sup:
            # Hole #2: the mute ran out. Fail, and say why it is failing now.
            results.append(Result(
                rel, EXPIRED, age_h, limit, feed.owner, source=source,
                detail=f"SUPPRESSION EXPIRED {sup.until.isoformat()}. Original "
                       f"blocker: {sup.reason} — re-validate that this is still "
                       f"true, then fix it or consciously extend the mute."))
        else:
            results.append(Result(rel, STALE, age_h, limit, feed.owner, detail, source))

    return results


def remediate(results: list[Result]) -> list[str]:
    """Try once to self-heal a stale feed by re-running its refresher.

    Deliberately conservative: one attempt, only for feeds that declare a
    refresher, and the fetchers themselves all refuse to write a partial or
    regressing parse. A retry that "fixes" the check by writing garbage would
    be far worse than staying red.
    """
    notes: list[str] = []
    for r in results:
        if r.status not in (STALE, EXPIRED):
            continue
        feed = MANIFEST.get(r.path)
        if not feed or not feed.refresher:
            notes.append(f"{r.path}: no safe automatic retry; needs a human.")
            continue
        notes.append(f"{r.path}: retrying `{feed.refresher}` ...")
        try:
            proc = subprocess.run(
                feed.refresher, shell=True, cwd=REPO_ROOT,
                capture_output=True, text=True, timeout=600)
        except subprocess.TimeoutExpired:
            notes.append(f"{r.path}: retry TIMED OUT after 600s.")
            continue
        if proc.returncode == 0:
            notes.append(f"{r.path}: retry exited 0 — re-checking.")
        else:
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-5:]
            notes.append(f"{r.path}: retry FAILED rc={proc.returncode}: "
                         + " / ".join(tail))
    return notes


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

def _group(results: list[Result]) -> dict[str, list[Result]]:
    out: dict[str, list[Result]] = {}
    for r in results:
        out.setdefault(r.status, []).append(r)
    return out


def render_text(results: list[Result], notes: list[str]) -> str:
    g = _group(results)
    parts: list[str] = []

    def section(status: str, header: str, prefix: str) -> None:
        rows = g.get(status)
        if not rows:
            return
        parts.append(f"\n{header} ({len(rows)}):")
        for r in rows:
            parts.append(f"  {prefix} {r.line()}")

    section(OK, "fresh", "OK     ")
    section(SKIPPED, "not monitored here - declared and justified", "SKIP   ")
    section(SUPPRESSED, "suppressed - reported, not failing", "MUTED  ")
    section(STALE, "STALE - regressed, needs attention", "STALE  ")
    section(EXPIRED, "EXPIRED SUPPRESSION - mute ran out", "EXPIRED")
    section(UNKNOWN, "UNEVALUABLE - monitor is blind here", "UNKNOWN")
    section(MISSING, "MISSING - expected on disk", "MISSING")
    section(UNWATCHED, "UNWATCHED - not classified in MANIFEST", "UNWATCH")

    if notes:
        parts.append("\nremediation:")
        parts.extend(f"  {n}" for n in notes)

    failing = [r for r in results if r.fails]
    parts.append("")
    if failing:
        parts.append(f"FAIL: {len(failing)} feed(s) unhealthy.")
    else:
        parts.append("OK: all feeds healthy.")
    return "\n".join(parts)


def render_issue(results: list[Result], notes: list[str]) -> str:
    """Body for the auto-filed tracking issue.

    Written to be actionable at a glance: what broke, how old, who owns it.
    Edited in place on every run while the problem persists, so the issue is a
    live status board rather than a pile of duplicate notifications.
    """
    failing = [r for r in results if r.fails]
    lines = [
        "Automated data-health report. This issue is maintained by "
        "`.github/workflows/data-health.yml` — it updates in place while feeds "
        "are unhealthy and closes itself once they all recover.",
        "",
        f"**{len(failing)} feed(s) unhealthy** as of "
        f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC.",
        "",
        "| feed | status | age | limit | owner |",
        "| --- | --- | --- | --- | --- |",
    ]
    for r in sorted(failing, key=lambda x: -(x.age_h or 0)):
        age = humanize_age(r.age_h) if r.age_h is not None else "?"
        lim = humanize_age(r.limit_h) if r.limit_h is not None else "?"
        lines.append(f"| `{r.path}` | **{r.status}** | {age} | {lim} | {r.owner or '?'} |")

    detailed = [r for r in failing if r.detail]
    if detailed:
        lines += ["", "<details><summary>Details</summary>", ""]
        for r in detailed:
            lines.append(f"- **`{r.path}`** — {r.detail}")
        lines += ["", "</details>"]

    muted = [r for r in results if r.status == SUPPRESSED]
    if muted:
        lines += ["", f"<details><summary>{len(muted)} suppressed "
                      f"(not failing, but on the clock)</summary>", ""]
        for r in muted:
            lines.append(f"- **`{r.path}`** — {r.detail}")
        lines += ["", "</details>"]

    if notes:
        lines += ["", "<details><summary>Auto-remediation attempts</summary>", ""]
        lines += [f"- {n}" for n in notes]
        lines += ["", "</details>"]

    lines += ["", "---", "_Generated by [Claude Code](https://claude.ai/code)_"]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--mode", choices=[COMMITTED, BUILT], default=COMMITTED,
                    help="committed: check files a cron commits back (daily "
                         "watchdog). built: additionally check post-build "
                         "artifacts, run after a pages.yml-style build.")
    ap.add_argument("--report", choices=["text", "json", "issue"], default="text")
    ap.add_argument("--remediate", action="store_true",
                    help="attempt one self-heal per stale feed before reporting")
    args = ap.parse_args(argv)

    results = evaluate(args.mode)

    notes: list[str] = []
    if args.remediate and any(r.fails for r in results):
        notes = remediate(results)
        results = evaluate(args.mode)   # re-evaluate after the retries

    if args.report == "json":
        print(json.dumps({
            "mode": args.mode,
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "healthy": not any(r.fails for r in results),
            "results": [
                {"path": r.path, "status": r.status, "age_h": r.age_h,
                 "limit_h": r.limit_h, "owner": r.owner, "detail": r.detail,
                 "source": r.source}
                for r in results
            ],
            "remediation": notes,
        }, indent=2))
    elif args.report == "issue":
        print(render_issue(results, notes))
    else:
        print(render_text(results, notes))

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path and args.report == "text":
        with open(summary_path, "a", encoding="utf-8") as fh:
            fh.write(f"## Data health ({args.mode})\n\n```\n")
            fh.write(render_text(results, notes))
            fh.write("\n```\n")

    return 1 if any(r.fails for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
