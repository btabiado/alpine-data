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

And one hole freshness cannot see at all:

  7. A FRESH NEWEST POINT SAID NOTHING ABOUT THE HISTORY BEHIND IT.
     data/composites/ lost 2026-08-19..21 and archived whale_sentiment_* as
     null in every snapshot for two months, and LTHCS crypto lost four
     September days, all under a green freshness check, because the newest
     file was always fresh.
     -> Feeds carry optional `history` specs (scripts/history_continuity.py).
        The committed-mode run checks the last N days for missing periods,
        duplicates and null required fields; gaps no real source can fill
        are disclosed in health/known_gaps.json instead of failing.

Freshness is judged on file CONTENT (last row date / generated_at), never on
mtime: a stateless CI checkout rewrites every mtime on every run, which would
make every feed look perpetually fresh. This is inherited from
build_health_status._content_age_probe and is the one thing the old script got
unambiguously right.

A feed the deploy regenerates but never commits back (`source=DEPLOYED`) is
judged in `--mode committed` from the copy the live Pages site serves, fetched
over HTTPS, not from its repo file: that file is a stale fallback by
construction, so judging it alarms forever on a feed that is fine. If the
fetch fails the feed is UNKNOWN ("could not check"), never STALE — a network
blip says nothing about the data. In `--mode built` (inside pages.yml, before
the deploy) the freshly built local file is the copy about to be served, so it
is judged from disk.

Usage
-----
    python scripts/data_health.py                     # committed artifacts
    python scripts/data_health.py --mode built        # after a pages.yml build
    python scripts/data_health.py --report json       # machine-readable
    python scripts/data_health.py --report issue      # tracking-issue body
    python scripts/data_health.py --remediate         # try to self-heal first
    python scripts/data_health.py --no-history        # freshness only

Exit codes: 0 = healthy, 1 = at least one feed stale/unknown/unwatched.
`--report text` and `--report json` carry that verdict. `--report issue` exits
0 whenever it printed the body: it is a formatter the workflow runs AFTER the
check step has already decided the run is unhealthy, and a non-zero exit there
failed the step and skipped the step that files the issue (first scheduled
run, 2026-10-04).
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
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
    _select,
    humanize_age,
    nested_age_h,
    resolve_age,
)
from build_health_status import NESTED_DATE_PATHS as _NESTED_DATE_PATHS  # noqa: E402
import history_continuity as hc  # noqa: E402  (path set above)

# Short names for the manifest below; bound from the one module import so the
# module isn't imported with both `import` and `from ... import`.
DAILY, MONTHLY, TRADING, History = hc.DAILY, hc.MONTHLY, hc.TRADING, hc.History

# `_select` and `nested_age_h` are re-exports, not dead imports: the age
# resolution they implement lives in build_health_status so /health/ and this
# watchdog can never disagree about what "stale" means, and the tests exercise
# it through `data_health` because that is the module under test.
# Declared here so that intent is machine-readable — CodeQL correctly flagged
# them as unused when only a `# noqa` comment asserted otherwise.
__all__ = [
    # re-exported age machinery (shared with build_health_status / /health/)
    "AgeProbe", "DEFAULT", "NESTED_DATE_PATHS", "THRESHOLDS",
    "_select", "humanize_age", "nested_age_h", "resolve_age",
    # this module's own surface
    "COMMITTED", "BUILT", "STATIC", "DELEGATED",
    "REPO", "DEPLOYED", "PAGES_BASE_URL", "LiveFetchError", "deployed_url",
    "OK", "STALE", "UNKNOWN", "MISSING", "UNWATCHED", "SUPPRESSED", "EXPIRED",
    "SKIPPED",
    "Feed", "Result", "Suppression", "History",
    "MANIFEST", "SUPPRESSIONS", "ARCHIVES",
    "GAP", "DUPLICATE", "DISCLOSED",
    "discover", "evaluate", "evaluate_history", "remediate",
    "render_text", "render_issue", "main",
]

# Overridable so the monitor can be pointed at a fixture tree in tests without
# a chdir dance. Defaults to the real repo root in every normal invocation.
REPO_ROOT = Path(os.environ.get("ALPINE_REPO_ROOT")
                 or Path(__file__).resolve().parent.parent)


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------

COMMITTED = "committed"   # tracked in git, judged by the daily watchdog (see `source`)
BUILT = "built"           # pages.yml regenerates it at deploy time
SERIES = "series"         # a directory of dated files; the NEWEST one is the feed
STATIC = "static"         # reference data that legitimately does not change
DELEGATED = "delegated"   # watched by a different system; declared so it is not UNWATCHED

# Which COPY of a COMMITTED feed the daily (`--mode committed`) check judges.
REPO = "repo"             # the committed file: its own cron commits it back
DEPLOYED = "deployed"     # the copy the live Pages site serves (see PAGES_BASE_URL)

# Where pages.yml publishes the site. Overridable so a fork, or a test, can
# point the deployed-copy check somewhere else.
PAGES_BASE_URL = os.environ.get("ALPINE_PAGES_URL",
                                "https://btabiado.github.io/alpine-data/")
LIVE_FETCH_TIMEOUT_S = 20
LIVE_FETCH_ATTEMPTS = 2


@dataclass(frozen=True)
class Feed:
    """One monitored artifact.

    `owner` is printed in every alarm so the fix path is obvious rather than
    something to re-derive at 2am. `refresher` is the command the --remediate
    path runs to try to self-heal; None means no safe automatic retry exists.

    `source` says which copy `--mode committed` judges. REPO (the default) is
    the committed file. DEPLOYED is for a feed pages.yml regenerates at deploy
    time and never commits back: its repo file is a stale fallback, so the
    daily check fetches `PAGES_BASE_URL + rel` instead. `built_path` is where a
    DEPLOYED feed's build writes the copy it publishes at `rel`, when that is
    not `rel` itself; `--mode built` judges that local file.
    """
    kind: str
    owner: str
    refresher: str | None = None
    limit_h: float | None = None    # overrides THRESHOLDS when the cadence is unusual
    justification: str = ""         # required for STATIC and DELEGATED, enforced below
    series_glob: str = "*.json"     # SERIES only: which files in the directory count
    source: str = REPO              # COMMITTED only: REPO or DEPLOYED, see above
    built_path: str | None = None   # DEPLOYED only: local file the build publishes at rel
    # Where this feed's ACCUMULATED history lives and how often it must grow
    # (scripts/history_continuity.py). Judged by evaluate_history() in the
    # daily committed-mode run: a missing day/month or a duplicate in the last
    # N days fails, unless health/known_gaps.json discloses it as unfillable.
    history: tuple[History, ...] = ()
    # How often the UNDERLYING data is published, when that is slower than any
    # cron (e.g. "annual"). Reported next to the age wherever the feed is
    # shown, so a long age on a slow source reads as expected, not broken.
    # Documentation only: limit_h is still what fails the feed.
    cadence: str = ""


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
# Composite indexes the dashboard charts from data/composites/. Each must be
# non-null in every daily snapshot (see the data/composites/ entry below).
COMPOSITE_REQUIRED_FIELDS: tuple[str, ...] = (
    "indexes.whale_sentiment_btc",
    "indexes.whale_sentiment_eth",
    "indexes.money_flow_index",
    "indexes.crypto_signal_sentiment",
    "indexes.poc_signal_breadth",
    "indexes.lthcs_composite",
    "indexes.etf_flow_sentiment_btc",
    "indexes.etf_flow_sentiment_eth",
    # first written 2026-10-04 (PR #25 follow-up made these cards clickable)
    "indexes.overview_sentiment@2026-10-04",
    "indexes.defi_sentiment@2026-10-04",
    "indexes.stocks_signal_breadth@2026-10-04",
    "indexes.futures_sentiment_btc@2026-10-04",
    "indexes.futures_sentiment_eth@2026-10-04",
)


MANIFEST: dict[str, Feed] = {
    # --- committed by their own cron ---------------------------------------
    "data-tsa.json": Feed(
        COMMITTED, "aviation-tsa.yml (daily 14:10Z)", "python fetch_tsa.py",
        # The page publishes a trailing window; fetch_tsa rewrites it whole,
        # so continuity here means "no hole inside the window we serve".
        history=(History("data-tsa.json", DAILY, "json", label="checkpoint series",
                         series_key="series", date_field="d"),),
        # Age is measured from the newest checkpoint date, and TSA posts
        # yesterday's count, so a healthy file is already ~24h old at fetch
        # time and the 14:10Z cron routinely starts hours late. 48h tolerates
        # that and still flags a single missed day.
        limit_h=48.0),
    "data-city.json": Feed(
        COMMITTED, "city-daily.yml (daily 06:00Z)", "python fetch_city.py",
        # Upstream (Socrata) monthly counts, re-pulled whole each run. A month
        # missing mid-series is a month the portal returned no rows for.
        history=(
            History("data-city.json", MONTHLY, "json", label="scored feed series",
                    rows="cities[].pulse.pillars[].feeds[]", series_key="series",
                    date_field="month", group="dataset"),
            History("data-city.json", MONTHLY, "json", label="extended feed series",
                    rows="cities[].extended[]", series_key="series",
                    date_field="month", group="dataset"),
            History("data-city.json", DAILY, "git", label="daily commits"),
        )),
    # Overwritten in place each day: git history IS the archive, so a day
    # without a commit is a day whose snapshot was never stored.
    "data-cfpb.json": Feed(
        COMMITTED, "cfpb-daily.yml (daily)", "python scripts/fetch_cfpb.py",
        history=(History("data-cfpb.json", DAILY, "git", label="daily commits"),)),
    "data-usaspending.json": Feed(
        COMMITTED, "usaspending-daily.yml (daily)",
        "python scripts/fetch_usaspending.py",
        history=(History("data-usaspending.json", DAILY, "git",
                         label="daily commits"),)),
    "data-opensky.json": Feed(
        COMMITTED, "aviation-opensky.yml (hourly)", "python fetch_opensky.py",
        history=(History("data-opensky.json", DAILY, "git",
                         label="daily commits"),)),
    # Live Flight Map positions. Fetched by pages.yml at deploy time and never
    # committed (its hourly commits were 46% of repo history); the previous
    # deploy's copy is restored from the Actions cache if a fetch fails.
    # Judged in --mode built, against the file the deploy is about to serve.
    "data-opensky-positions.json": Feed(
        BUILT, "fetch_opensky.py, inside pages.yml (deploy-time, never committed)"),
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
        #
        # Checked 2026-10-05: faa.gov's U.S. Civil Airmen Statistics page calls
        # it "an annual study", lists "2025 Active Civil Airmen Statistics" as
        # the newest roll and was last updated 2026-04-07. So 2025-12-31 IS the
        # current vintage (age ~279d is expected), and if the 2026 roll posts
        # as late as the 2025 one did, this 400d limit fires ~2027-02-04, some
        # weeks before it can be refreshed. That alarm is then "annual roll
        # due: check faa.gov", not rot; it is left on purpose.
        limit_h=400 * 24.0,
        cadence="annual"),
    "data/real_estate.json": Feed(
        COMMITTED, "real-estate-daily.yml (daily)",
        "python scripts/fetch_real_estate.py",
        history=(
            History("data/real_estate.json", MONTHLY, "json",
                    label="metro 5y monthly history",
                    rows="metros[].history_5y_monthly", series_key="labels",
                    date_field=""),
            History("data/real_estate.json", DAILY, "git", label="daily commits"),
        )),
    "data/ai_curated.json": Feed(
        # Corrected owner. Nothing regenerates this file: it is a hand-curated
        # snapshot (compiled_at), and fetch_market.load_ai_curated() only READS
        # it, wiki-enriches it in memory and inlines it into dashboard.html and
        # v2/dashboard.html as DATA.market.ai_curated. It is not published as a
        # file (<site>/data/ai_curated.json is a 404), and the live pages carry
        # the very same compiled_at as the repo copy, so the repo copy IS the
        # deployed data and REPO is the right source. When it is red, the fix
        # is a human re-curating it, not a fetcher.
        #
        # Limit: 90 days, from THRESHOLDS["ai_curated.json"] (shared with
        # /health/). It was the 24h default, which judged a quarterly hand
        # snapshot as if it were an hourly feed. No keyless source publishes
        # private-company valuations, so this cannot be automated without a
        # paid API. Refresh = re-verify every row against its source_url (or a
        # newer primary source), keep the schema, bump compiled_at.
        COMMITTED, "hand-curated snapshot (manual PR, re-curate quarterly); read "
                   "and inlined into the built HTML by "
                   "fetch_market.load_ai_curated, never rewritten"),
    # Trading-day feeds. No limit_h: build_health_status measures their last
    # row on a WEEKDAY clock (TRADING_DAY_FEEDS) and THRESHOLDS gives the
    # budget (56 weekday hours), shared with /health/. The old flat 96h wall-
    # clock limit here disagreed with /health/'s 48h, which read Friday's
    # complete data as "critical 2.8d" every weekend.
    "data/equity_etf_flows.csv": Feed(
        COMMITTED, "money-flow-daily.yml (daily 08:30Z)",
        # One row per (trading day, ticker). There is no free upstream for
        # past shares-outstanding, so a missed day here is lost for good —
        # which is exactly why it must be caught the day it happens.
        history=(History("data/equity_etf_flows.csv", TRADING, "csv",
                         label="daily shares-outstanding rows",
                         key_fields=("ticker",)),)),
    "data/btc_flows.csv": Feed(
        COMMITTED, "etf-flows-daily.yml (daily 09:15Z)",
        "python scripts/fetch_etf_flows.py",
        history=(History("data/btc_flows.csv", TRADING, "csv",
                         label="daily flow rows"),)),
    "data/eth_flows.csv": Feed(
        COMMITTED, "etf-flows-daily.yml (daily 09:15Z)",
        "python scripts/fetch_etf_flows.py",
        history=(History("data/eth_flows.csv", TRADING, "csv",
                         label="daily flow rows"),)),
    # --- daily history of deploy-time sidecars (scripts/snapshot_history.py,
    #     committed by pages.yml) ------------------------------------------
    # The two sidecars below are rebuilt every deploy and never committed, and
    # neither upstream serves history, so before these files each build threw
    # yesterday's reading away. Append-only, one row-set per observation date.
    "data/stock_money_flow_history.csv": Feed(
        COMMITTED, "scripts/snapshot_history.py, committed by pages.yml "
                   "('Commit deploy-time feed history')",
        "python scripts/snapshot_history.py",
        # Same trading-day cadence as the sidecar it records (see its 120h).
        limit_h=120.0,
        history=(History("data/stock_money_flow_history.csv", TRADING, "csv",
                         label="daily ticker rows", key_fields=("symbol",)),)),
    "data/travel_advisory_levels.csv": Feed(
        COMMITTED, "scripts/snapshot_history.py, committed by pages.yml "
                   "('Commit deploy-time feed history')",
        "python scripts/snapshot_history.py", limit_h=48.0,
        history=(History("data/travel_advisory_levels.csv", DAILY, "csv",
                         label="daily level counts"),)),
    "data/travel_advisory_changes.csv": Feed(
        DELEGATED, "scripts/snapshot_history.py, committed by pages.yml",
        justification="Change log: a row only when a country's advisory "
                      "level changes, so its newest row is the last CHANGE, "
                      "not the last observation, and can be weeks old on a "
                      "healthy feed. The same step writes "
                      "data/travel_advisory_levels.csv every day, which is "
                      "watched for freshness and continuity above."),
    "data-travel.json": Feed(
        # DEPLOYED: app.py rewrites the root file on every pages.yml build and
        # the "Stage site directory" step publishes it, but nothing commits it
        # back. The repo copy is the 2026-05-28 baseline kept as a fallback.
        COMMITTED, "fetch_advisories.py, called by app.py inside pages.yml "
                   "(deployed, never committed back)",
        "python fetch_advisories.py", source=DEPLOYED),

    # --- THE GAP THAT CAUSED THE JUNE FREEZE -------------------------------
    # None of these three was watched. All three froze. That is not a
    # coincidence — they froze *because* nothing was watching, so the V2 build
    # timeout that starved them produced no signal for eight weeks.
    "data-mufon.json": Feed(
        # DEPLOYED: v2/app.py writes v2/data-mufon.json and the staging step
        # copies it to the site root; the root repo file is a frozen fallback
        # (last committed 2026-06-08) that the deploy never reads.
        COMMITTED, "fetch_mufon.py, called by v2/app.py inside pages.yml "
                   "(deployed from v2/data-mufon.json, never committed back)",
        "python fetch_mufon.py", source=DEPLOYED,
        built_path="v2/data-mufon.json"),
    "data-stock-money-flow.json": Feed(
        # Corrected owner: the standalone daily cron referenced by .gitignore
        # (stock-money-flow-daily.yml) does not exist. pages.yml line ~113
        # records that it was REMOVED after Yahoo throttled it to 0/219, and
        # the sidecar is now written inside the fetch-market step by
        # fetch_market.fetch_all() -> fetch_stock_money_flow.build_from_signals().
        # Pointing an alarm at a workflow that was deleted is how you get an
        # alarm nobody can act on.
        #
        # DEPLOYED: written to the repo root inside the build and published by
        # the staging step, never committed back (last commit 2026-06-08).
        COMMITTED, "fetch_market.py --fetch-market step in pages.yml (via "
                   "fetch_stock_money_flow.build_from_signals; deployed, never "
                   "committed back)",
        source=DEPLOYED,
        # 120h, not the 24h default: `as_of` is the date of the last DAILY BAR
        # (the oldest across scored tickers), so it only moves on trading days.
        # At the 15:00Z check, Friday's bar is ~87h old on Monday, and ~111h
        # on the Tuesday after a Monday market holiday (or the Monday after
        # Good Friday). 120h covers those without hiding a feed that has
        # really stopped for a full trading week.
        limit_h=120.0),
    "snowflake_summit/news.json": Feed(
        # Owner corrected: pages.yml runs the enricher (Google News RSS) on
        # every deploy and its "Commit Summit news feed" step commits the file
        # back, at most ~daily (when the newest item's day moves, or the copy
        # is >24h old). Judged by `generated`, the newest headline's date.
        COMMITTED, "snowflake_summit/enrich_vendors.py inside pages.yml, "
                   "committed back by its 'Commit Summit news feed' step"),
    "snowflake_summit/vendors.json": Feed(
        # Found by the drift detector this PR added to _content_age_probe, not
        # by reading the directory: its only stamp is nested at
        # `_meta.generated`, so /health/ was scoring it off mtime and this
        # monitor did not cover it at all. Exactly the class of miss the
        # detector exists to surface.
        #
        # Owner corrected: enrich_vendors.py never writes this file (its
        # Wikidata facts go to the gitignored enrichment.json and are merged at
        # build time). It is Bryan's hand-curated partner directory; a manual
        # refresh re-verifies market caps / funding events and bumps
        # `_meta.generated`. 90d limit (THRESHOLDS["vendors.json"]).
        COMMITTED, "hand-curated Summit partner directory (manual PR, "
                   "re-verify quarterly and bump _meta.generated)"),
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
        limit_h=48.0,
        # One file per day, and the indexes the cards chart must actually be
        # IN it: every archived snapshot carried whale_sentiment_* = null for
        # two months behind a green freshness check, because a file with nulls
        # in it is still a fresh file. Keys the snapshotter only started
        # writing on 2026-10-04 are required from that day on.
        history=(History(
            "data/composites/", DAILY, "files", label="daily snapshots",
            required_fields=COMPOSITE_REQUIRED_FIELDS),)),

    # --- regenerated at deploy time (gitignored placeholders in the repo) ---
    # NOT exempt. See hole #3: market-derived files are rewritten hourly while
    # their contents stale-keep, which is the exact shape of the breadth freeze.
    "data-whale.json": Feed(BUILT, "fetch_market.py, inside pages.yml"),
    "data-metals.json": Feed(BUILT, "fetch_metals.py, inside pages.yml"),
    "data-supplies.json": Feed(BUILT, "fetch_supplies.py, inside pages.yml"),
    "data-cpi.json": Feed(BUILT, "fetch_cpi.py, inside pages.yml"),
    # The four build artifacts the built-mode check reported as UNWATCHED.
    "data-defi.json": Feed(
        BUILT, "app.py / v2/app.py lazy sidecar from fetch_market's DeFi subtree"),
    "data-mmf.json": Feed(
        BUILT, "fetch_money_flows.py, via fetch_market.build_money_flow_payload "
               "inside pages.yml (ICI; FRED WRMFNS fallback)"),
    "data-mf-flows.json": Feed(
        BUILT, "fetch_money_flows.py, via fetch_market.build_money_flow_payload "
               "inside pages.yml (ICI; no fallback — available:false when blocked)"),
    "data-equity-etf-flows.json": Feed(
        BUILT, "fetch_equity_etf_flows.py, via fetch_market.build_money_flow_payload "
               "inside pages.yml"),
    "data-travel-fetch-status.json": Feed(
        BUILT, "fetch_advisories.py (status record added in PR #24)"),
    "data/ai_curated_wiki.json": Feed(BUILT, "insights.py / wiki_enrich, inside pages.yml"),
    "data/market.json": Feed(BUILT, "fetch_market.py, inside pages.yml"),
    "data/whale.json": Feed(BUILT, "fetch_market.py, inside pages.yml"),
    "data/coinbase.json": Feed(BUILT, "fetch_coinbase.py, inside pages.yml"),
    # data/cpi.json, data/metals.json and data/supplies.json were listed here
    # and reported MISSING on every build: nothing writes them. fetch_cpi /
    # fetch_metals / fetch_supplies dual-write v2/data-X.json and the root
    # data-X.json (their DEFAULT_OUT_V1), which are the entries above.
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
    "data/shares.json": Feed(
        DELEGATED, "server.py / share.py (shares.create), local runtime only",
        justification="The share-link store written by the local server and "
                      "share.py CLI when someone mints a link. It is gitignored "
                      "and never produced by pages.yml, so as a BUILT feed it "
                      "read MISSING on every build. It is state, not a feed."),
    "data/lthcs/": Feed(
        DELEGATED, "lthcs-daily.yml and friends",
        # Freshness is delegated; continuity is not. LTHCS has its own
        # freshness checks but nothing that notices a MISSING day, and four
        # crypto days in September were lost to a cron that started after
        # midnight UTC and wrote the next day's file instead.
        history=(
            History("data/lthcs/index/", DAILY, "files", label="daily index"),
            History("data/lthcs/snapshots/", DAILY, "files",
                    label="daily equity snapshots"),
            History("data/lthcs/snapshots_crypto/", DAILY, "files",
                    label="daily crypto snapshots"),
            History("data/lthcs/narratives/", DAILY, "files",
                    label="daily narratives"),
        ),
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
    # Frozen on purpose, not broken. Since 2026-06-10 nuforc.org answers every
    # non-browser request with a Cloudflare managed challenge, and NUFORC's
    # terms forbid automated collection without written consent, so
    # fetch_mufon.py serves the committed month cache and the dashboard labels
    # the tab "data through June 9". No code change may route around that
    # gate (see fetch_mufon.py's docstring). The owner emailed NUFORC for
    # permission on 2026-10-04. Three months is time for an answer; on expiry,
    # either wire the sanctioned feed or retire/relabel the map, rather than
    # extending this by reflex.
    "data-mufon.json": Suppression(
        reason="NUFORC blocks automated access (Cloudflare challenge) and its "
               "terms forbid automated collection without written consent, so "
               "the UAP map is deliberately frozen at 2026-06-09 and labelled "
               "that way on the dashboard. Permission requested by email "
               "2026-10-04; revisit on expiry.",
        until=date(2027, 1, 4),
        tracked_in="NUFORC permission request emailed 2026-10-04"),
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


# Archives that are not themselves a MANIFEST artifact. The R2 bucket holds a
# daily copy of every deploy-time data-*.json (upload_to_r2.py in pages.yml),
# which is the ONLY record of feeds the deploy regenerates and never commits.
# Its per-file daily presence is published by the pages build as
# health/r2-coverage.json, so the committed-mode run reads the deployed copy.
ARCHIVES: dict[str, History] = {
    "R2 archive": History("health/r2-coverage.json", DAILY, "r2",
                          label="daily payload copies"),
}


# --------------------------------------------------------------------------
# Evaluation
# --------------------------------------------------------------------------

OK, STALE, UNKNOWN, MISSING, UNWATCHED, SUPPRESSED, EXPIRED, SKIPPED = (
    "ok", "stale", "unknown", "missing", "unwatched", "suppressed", "expired",
    "skipped")
# History continuity (evaluate_history). GAP covers missing periods AND
# required fields that are null; DISCLOSED is a gap health/known_gaps.json
# already explains as unfillable, reported but never failed.
GAP, DUPLICATE, DISCLOSED = "gap", "duplicate", "disclosed"

FAILING_STATUSES = frozenset({STALE, UNKNOWN, MISSING, UNWATCHED, EXPIRED,
                              GAP, DUPLICATE})


@dataclass
class Result:
    path: str
    status: str
    age_h: float | None = None
    limit_h: float | None = None
    owner: str = ""
    detail: str = ""
    source: str = ""     # which date field the age came from
    cadence: str = ""    # Feed.cadence, echoed so reports can say it

    @property
    def fails(self) -> bool:
        return self.status in FAILING_STATUSES

    def line(self) -> str:
        age = humanize_age(self.age_h) if self.age_h is not None else "?"
        lim = humanize_age(self.limit_h) if self.limit_h is not None else "?"
        base = f"{self.path}: {age} old (limit {lim})"
        if self.age_h is None and self.limit_h is None and "[history:" in self.path:
            base = self.path   # continuity has no age; the detail says what broke
        if self.source:
            base += f" via {self.source}"
        if self.cadence:
            base += f" [cadence: {self.cadence}]"
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
        if feed.source not in (REPO, DEPLOYED):
            problems.append(f"{rel}: unknown source {feed.source!r}")
        if feed.source == DEPLOYED and feed.kind != COMMITTED:
            problems.append(f"{rel}: source=DEPLOYED applies to COMMITTED feeds "
                            f"only; a BUILT feed is already judged against the "
                            f"real file in --mode built")
        if feed.built_path and feed.source != DEPLOYED:
            problems.append(f"{rel}: built_path is only meaningful with "
                            f"source=DEPLOYED")
        for h in feed.history:
            problems.extend(f"{rel}: {p}" for p in _verify_history(h))
        names = [h.name for h in feed.history]
        if len(names) != len(set(names)):
            problems.append(f"{rel}: history specs need distinct labels "
                            f"(known_gaps.json refers to them by name)")
    for name, h in ARCHIVES.items():
        problems.extend(f"archive {name}: {p}" for p in _verify_history(h))
    for rel in SUPPRESSIONS:
        if rel not in MANIFEST:
            problems.append(f"suppression for {rel!r} has no MANIFEST entry")
    for rel, specs in NESTED_DATE_PATHS.items():
        if rel not in MANIFEST:
            problems.append(f"nested date path for {rel!r} has no MANIFEST entry")
        if not specs:
            problems.append(f"nested date path for {rel!r} is empty")
    return problems


def _verify_history(h: History) -> list[str]:
    out = []
    if h.cadence not in hc.CADENCES:
        out.append(f"history {h.name!r}: unknown cadence {h.cadence!r}")
    if h.source not in hc.SOURCES:
        out.append(f"history {h.name!r}: unknown source {h.source!r}")
    if h.source == "files" and not h.path.endswith("/"):
        out.append(f"history {h.name!r}: a files history names a directory "
                   f"and must end with '/'")
    if h.source != "files" and h.path.endswith("/"):
        out.append(f"history {h.name!r}: only a files history names a directory")
    if h.required_fields and h.source != "files":
        out.append(f"history {h.name!r}: required_fields applies to files "
                   f"histories only")
    if h.source == "json" and not h.series_key:
        out.append(f"history {h.name!r}: a json history needs series_key")
    return out


def history_specs() -> dict[str, tuple[History, ...]]:
    """Every History spec by owner name: MANIFEST rels plus ARCHIVES."""
    specs = {rel: f.history for rel, f in MANIFEST.items() if f.history}
    specs.update({name: (h,) for name, h in ARCHIVES.items()})
    return specs


class LiveFetchError(Exception):
    """The deployed copy of a feed could not be fetched.

    `not_found` separates "the site answered, and this file is not on it"
    (a real problem: the dashboard tab 404s) from "could not reach the site"
    (no information about the feed at all).
    """

    def __init__(self, reason: str, not_found: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.not_found = not_found


def deployed_url(rel: str) -> str:
    """Where the live Pages site serves a repo-root-relative artifact."""
    return PAGES_BASE_URL.rstrip("/") + "/" + rel.lstrip("/")


def _fetch_deployed(url: str) -> bytes:
    """GET one deployed artifact, or raise LiveFetchError. Never anything else.

    Every transport failure is folded into LiveFetchError so a flaky network
    can only ever make one feed UNKNOWN; it must not abort the run and take
    every other verdict (and the tracking issue) down with it.
    """
    req = urllib.request.Request(url, headers={
        "User-Agent": "alpine-data data_health.py (+https://github.com/btabiado/alpine-data)",
        "Cache-Control": "no-cache",
    })
    reason = "no attempt made"
    for attempt in range(LIVE_FETCH_ATTEMPTS):
        try:
            with urllib.request.urlopen(req, timeout=LIVE_FETCH_TIMEOUT_S) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise LiveFetchError("HTTP 404", not_found=True) from exc
            reason = f"HTTP {exc.code}"
        except (urllib.error.URLError, http.client.HTTPException,
                OSError, ValueError) as exc:
            reason = f"{type(exc).__name__}: {getattr(exc, 'reason', exc)}"
        if attempt + 1 < LIVE_FETCH_ATTEMPTS:
            time.sleep(2)
    raise LiveFetchError(reason)


def _probe_deployed(rel: str, now: float) -> "tuple[Path, AgeProbe, str, str]":
    """Age of the copy the live site serves. Raises LiveFetchError.

    The payload is written under its own name into a scratch directory so the
    exact same resolve_age path (THRESHOLDS by filename, NESTED_DATE_PATHS by
    rel) judges it as would judge a local file — no second parser to drift.
    """
    url = deployed_url(rel)
    payload = _fetch_deployed(url)
    with tempfile.TemporaryDirectory(prefix="data-health-") as tmp:
        judged = Path(tmp) / rel
        judged.parent.mkdir(parents=True, exist_ok=True)
        judged.write_bytes(payload)
        probe = resolve_age(judged, now, rel)
    return judged, probe, f"{probe.key or 'no stamp'} (deployed copy)", url


def _probe_feed(rel: str, feed: Feed, now: float,
                path: "Path | None" = None) -> "tuple[Path, AgeProbe, str]":
    """Resolve one feed to (path judged, age probe, human source label).

    Age comes from build_health_status.resolve_age, which already folds in
    NESTED_DATE_PATHS and takes the older of container and contents — so the
    watchdog and /health/ can never report different ages for the same file.
    `path` overrides where on disk the feed is read (a DEPLOYED feed's
    built_path); it is still judged under `rel`.
    """
    path = path or REPO_ROOT / rel
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
             now_ts: float | None = None, history: bool = True) -> list[Result]:
    now = now_ts if now_ts is not None else datetime.now(timezone.utc).timestamp()
    today = today or datetime.fromtimestamp(now, timezone.utc).date()
    results: list[Result] = []
    if history and mode == COMMITTED:
        # Continuity is judged once a day, by the committed-mode run. Every
        # history it reads is committed (or, for R2, published), so judging
        # it again inside every hourly deploy would only repeat the verdict.
        results.extend(evaluate_history(today))

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

        if feed.source == DEPLOYED and mode == COMMITTED:
            # The repo file is a fallback the deploy never commits back, so
            # judging it reports the age of the fallback, not of the site.
            # Judge what the site actually serves.
            try:
                judged, probe, source, url = _probe_deployed(rel, now)
            except LiveFetchError as exc:
                if exc.not_found:
                    results.append(Result(
                        rel, MISSING, owner=feed.owner, source="deployed copy",
                        detail=f"the live site answered {exc.reason} for "
                               f"{deployed_url(rel)}: the deploy is not "
                               f"publishing this feed at all."))
                else:
                    results.append(Result(
                        rel, UNKNOWN, owner=feed.owner, source="deployed copy",
                        detail=f"could not check: fetching the deployed copy "
                               f"from {deployed_url(rel)} failed ({exc.reason}). "
                               f"Not judged against the repo file, which is a "
                               f"stale fallback pages.yml never commits back. "
                               f"Re-run, or open the URL to see if the site is up."))
                continue
            age_h = probe.age_h
            detail = f"judged from the deployed copy at {url}"
            if age_h is not None and probe.note:
                detail += f"; {probe.note}"
        else:
            local = path
            if feed.source == DEPLOYED and feed.built_path:
                local = REPO_ROOT / feed.built_path
            if not local.exists():
                results.append(Result(rel, MISSING, owner=feed.owner,
                                      detail=f"expected on disk at "
                                             f"{local.relative_to(REPO_ROOT).as_posix()}; "
                                             f"refreshed by {feed.owner}"))
                continue

            # Hole #6 (container vs contents) is already folded in by
            # resolve_age: `probe.age_h` is the OLDER of the file's own stamp
            # and whatever its contents say, and `probe.note` explains it when
            # they disagreed.
            judged, probe, source = _probe_feed(rel, feed, now, local)
            age_h = probe.age_h
            detail = probe.note if age_h is not None else ""

        if probe.unavailable_reason and age_h is None:
            # The payload says its upstream is gone (e.g. ici.org 403). Still
            # a failure — the feed is not being served — but say WHY instead of
            # blaming the parser.
            results.append(Result(
                rel, UNKNOWN, owner=feed.owner, source=source,
                detail=f"payload marks itself unavailable: {probe.unavailable_reason}"))
            continue

        if age_h is None:
            # Hole #4: this used to exit 0.
            results.append(Result(
                rel, UNKNOWN, owner=feed.owner, source=source,
                detail=("no readable date signal inside the file. The monitor "
                        "cannot evaluate this feed, which is worse than stale: "
                        "an upstream schema change looks identical to health. "
                        f"Parser says: {probe.note}")))
            continue

        if probe.unavailable_reason:
            results.append(Result(
                rel, STALE, age_h, None, feed.owner, source=source,
                detail=f"payload marks itself unavailable: {probe.unavailable_reason}"))
            continue

        limit = feed.limit_h or THRESHOLDS.get(judged.name, DEFAULT).stale_h
        if age_h <= limit:
            results.append(Result(rel, OK, age_h, limit, feed.owner, detail, source,
                                  cadence=feed.cadence))
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
            results.append(Result(rel, STALE, age_h, limit, feed.owner, detail, source,
                                  cadence=feed.cadence))

    return results


def _history_path(owner: str, h: History) -> str:
    return f"{owner} [history: {h.name}]"


def evaluate_history(today: date | None = None,
                     r2_coverage: "dict | None" = None) -> list[Result]:
    """History continuity for every MANIFEST feed with `history` specs, plus
    ARCHIVES. One Result per spec: OK, GAP (missing period or null required
    field), DUPLICATE, DISCLOSED (only known, unfillable gaps), UNKNOWN (the
    history could not be read), or SUPPRESSED when the feed is under a live
    mute. See scripts/history_continuity.py for what each source reads."""
    today = today or datetime.now(timezone.utc).date()
    gaps = hc.load_known_gaps(REPO_ROOT)
    out: list[Result] = []
    for problem in hc.verify_known_gaps(gaps, history_specs()):
        out.append(Result(hc.KNOWN_GAPS_REL, UNWATCHED, detail=problem))

    def judge(owner: str, h: History, feed: "Feed | None") -> None:
        coverage = None
        if h.source == "r2":
            coverage = r2_coverage
            if coverage is None:
                try:
                    coverage = json.loads(_fetch_deployed(deployed_url(h.path)))
                except (LiveFetchError, ValueError) as exc:
                    reason = getattr(exc, "reason", str(exc))
                    out.append(Result(
                        _history_path(owner, h), UNKNOWN, source="deployed copy",
                        detail=f"could not check: {deployed_url(h.path)} -> "
                               f"{reason}. Not a gap, a blind spot."))
                    return
        f = hc.check(owner, h, REPO_ROOT, today, gaps, coverage)
        text = hc.summarize(f)
        if f.skipped:
            status = SKIPPED
        elif f.error:
            status = UNKNOWN
        elif f.missing or f.field_gaps:
            status = GAP
        elif f.duplicates:
            status = DUPLICATE
        elif f.disclosed:
            status = DISCLOSED
        else:
            status = OK
        owner_txt = feed.owner if feed else "pages.yml (upload_to_r2.py)"
        sup = SUPPRESSIONS.get(owner)
        if status in FAILING_STATUSES and sup and today <= sup.until:
            text = f"{status}: {text} — muted until {sup.until.isoformat()}: {sup.reason}"
            status = SUPPRESSED
        out.append(Result(_history_path(owner, h), status, owner=owner_txt,
                          detail=text,
                          source=f"{h.source} {h.cadence}, last {h.window()}d"))

    for rel in sorted(MANIFEST):
        feed = MANIFEST[rel]
        for h in feed.history:
            judge(rel, h, feed)
    for name, h in ARCHIVES.items():
        judge(name, h, None)
    return out


def remediate(results: list[Result], mode: str = COMMITTED) -> list[str]:
    """Try once to self-heal a stale feed by re-running its refresher.

    Deliberately conservative: one attempt, only for feeds that declare a
    refresher, and the fetchers themselves all refuse to write a partial or
    regressing parse. A retry that "fixes" the check by writing garbage would
    be far worse than staying red.

    A DEPLOYED feed judged from the live site is not retried in committed
    mode: re-running its fetcher on this runner cannot change what the site
    serves, so the "retry exited 0" note would claim a fix that never shipped.
    """
    notes: list[str] = []
    for r in results:
        if r.status not in (STALE, EXPIRED):
            continue
        feed = MANIFEST.get(r.path)
        if feed and feed.source == DEPLOYED and mode == COMMITTED:
            notes.append(f"{r.path}: judged from the live site; a local re-run "
                         f"cannot refresh it. The next pages.yml deploy does — "
                         f"check that run's logs.")
            continue
        if not feed or not feed.refresher:
            notes.append(f"{r.path}: no safe automatic retry; needs a human.")
            continue
        notes.append(f"{r.path}: retrying `{feed.refresher}` ...")
        # No shell. Every refresher in MANIFEST is a plain argv line
        # ("python fetch_tsa.py") — none uses a pipe, redirect, glob or any
        # other shell feature — so shell=True bought nothing and cost the
        # usual thing: this function runs in CI and executes commands, which
        # makes it the last place worth keeping an interpreter that turns a
        # stray metacharacter in a future MANIFEST entry into arbitrary
        # execution. shlex.split gives the same argv the shell would have
        # produced for these strings, and CodeQL/bandit stop flagging it.
        argv = shlex.split(feed.refresher)
        if not argv:
            notes.append(f"{r.path}: refresher is empty after parsing; skipped.")
            continue
        try:
            proc = subprocess.run(
                argv, cwd=REPO_ROOT,
                capture_output=True, text=True, timeout=600)
        except subprocess.TimeoutExpired:
            notes.append(f"{r.path}: retry TIMED OUT after 600s.")
            continue
        except OSError as exc:
            # Without a shell, an unrunnable refresher RAISES rather than
            # coming back as rc=127, and an uncaught FileNotFoundError here
            # would abort the whole health run — turning "one feed has a bad
            # refresher" into "the monitor produced no report at all". A
            # monitor that dies on the way to telling you something is wrong
            # is the failure mode this file exists to remove, so record it
            # like any other failed retry and keep going.
            notes.append(f"{r.path}: retry could not start "
                         f"({type(exc).__name__}: {exc}).")
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
    section(DISCLOSED, "history: known unfillable gaps (health/known_gaps.json)",
            "KNOWN  ")
    section(GAP, "HISTORY GAP - a day/month is missing or a required field is null",
            "GAP    ")
    section(DUPLICATE, "HISTORY DUPLICATE - one period recorded twice", "DUP    ")

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
    # Explicit `+`, not adjacent-literal concatenation. Inside a LIST, two
    # adjacent string literals are indistinguishable from a forgotten comma:
    # the reader sees two rows, Python builds one. CodeQL flags the shape for
    # exactly that reason and `+` is its documented remediation — it states
    # the intent instead of relying on whitespace.
    lines = [
        "Automated data-health report. This issue is maintained by "
        + "`.github/workflows/data-health.yml` — it updates in place while "
        + "feeds are unhealthy and closes itself once they all recover.",
        "",
        f"**{len(failing)} feed(s) unhealthy** as of "
        + f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC.",
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
                      + "(not failing, but on the clock)</summary>", ""]
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
    ap.add_argument("--no-history", action="store_true",
                    help="skip the history-continuity checks (committed mode "
                         "runs them by default)")
    args = ap.parse_args(argv)
    history = not args.no_history

    results = evaluate(args.mode, history=history)

    notes: list[str] = []
    if args.remediate and any(r.fails for r in results):
        notes = remediate(results, args.mode)
        results = evaluate(args.mode, history=history)   # re-evaluate after the retries

    if args.report == "json":
        print(json.dumps({
            "mode": args.mode,
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "healthy": not any(r.fails for r in results),
            "results": [
                {"path": r.path, "status": r.status, "age_h": r.age_h,
                 "limit_h": r.limit_h, "owner": r.owner, "detail": r.detail,
                 "source": r.source, "cadence": r.cadence}
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

    if args.report == "issue":
        # The issue body is a REPORT, not a verdict. data-health.yml only builds
        # it after the check step has already found the run unhealthy, so an
        # unhealthy result here is the expected input, not a failure. Exiting 1
        # failed the "Build issue body" step, which skipped "Open or update the
        # tracking issue" — the alarm channel itself — on the very first
        # scheduled run. The verdict exit code stays on text/json, where
        # data-health.yml's check step and pages.yml's built step read it.
        return 0
    return 1 if any(r.fails for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
