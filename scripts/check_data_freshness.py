#!/usr/bin/env python3
"""Fail loudly when a data feed stops updating.

Why this exists
---------------
On 2026-08-02 the TSA feed was found 45 days stale. Its cron
(`aviation-tsa.yml`) had failed on **30+ consecutive days** with an upstream
403, and nothing surfaced that: the workflow just went red on a page nobody
opens. Travel advisories (68 days) and the crypto ETF CSVs (82 days) had
rotted the same quiet way.

`build_health_status.py` already computes all of this and renders it to
/health/ — but that is a *pull* surface. This script is the *push* half: it
runs on its own daily cron and exits non-zero when a feed is past its
threshold, so the failure lands in your inbox instead of waiting to be
noticed.

It deliberately reuses `build_health_status`'s thresholds and content-age
logic rather than restating them, so the watchdog and the health page can
never disagree about what "stale" means.

Freshness is judged on file CONTENT (last CSV row date / generated_at), not
mtime — a stateless CI checkout rewrites mtimes every run, which would make
every feed look perpetually fresh.

Exit codes: 0 = all tracked feeds within threshold, 1 = at least one stale.
Run from the repo root:  python scripts/check_data_freshness.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_health_status import (  # noqa: E402  (path set above)
    DEFAULT,
    THRESHOLDS,
    _content_age_h,
    humanize_age,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

# Feeds this watchdog is responsible for, and who refreshes them. Only files
# whose refresher COMMITS back to the repo belong here — this job runs off a
# plain checkout, so a deploy-time file (market.json, whale.json, ...) either
# is not present at all or is a committed placeholder, and either way it would
# false-positive here.
#
# That exclusion is a checkout constraint, NOT a clean bill of health. It was
# previously written down as "fresh by construction on the live site", which is
# false: those files are restored from the Actions cache, so a failed upstream
# fetch silently reproduces the previous run's file. market.json rode that gap
# for 16 days with a frozen BTC price while this watchdog stayed green, because
# it was never in scope to look.
#
# Deploy-time feeds are monitored inside pages.yml instead, where the file
# actually exists: build_health_status.py scores them (using per-row `as_of`,
# since their envelope stamp is rewritten every run) and alert_stale_feeds.py
# raises the annotation. Add deploy-time feeds THERE, not here.
#
# `owner` is printed in the alert so the fix path is obvious rather than
# something to go re-derive at 2am.
TRACKED: dict[str, str] = {
    "data-tsa.json":            "aviation-tsa.yml (daily 14:10Z)",
    "data-city.json":           "city-daily.yml (daily 06:00Z)",
    "data-cfpb.json":           "cfpb-daily.yml (daily)",
    "data-usaspending.json":    "usaspending-daily.yml (daily)",
    "data-opensky.json":        "aviation-opensky.yml (hourly)",
    "data/real_estate.json":    "real-estate-daily.yml (daily)",
    "data/equity_etf_flows.csv": "money-flow-daily.yml (daily 08:30Z)",
    "data-travel.json":         "fetch_advisories.py, inside pages.yml",
    "data/btc_flows.csv":       "MANUAL (parse_farside.py) - no cron exists",
    "data/eth_flows.csv":       "MANUAL (parse_farside.py) - no cron exists",
}

# Feeds already known to be broken for reasons a code change cannot fix
# (upstream IP-blocks GitHub runners; missing paid API key). They are still
# checked and still reported, but they do NOT fail the build — otherwise the
# watchdog is red forever and everyone learns to ignore it, which is exactly
# the failure mode it exists to prevent. Remove an entry once its blocker is
# cleared so it becomes hard-enforced again.
# Feeds that only move on trading days. A 24h limit red-flags these every
# Saturday and Sunday, which is noise, not signal — Friday's row is already
# ~3 days old by Monday morning, and a Monday holiday pushes it to ~4.
WEEKDAY_ONLY_LIMIT_H: dict[str, float] = {
    "data/equity_etf_flows.csv": 96.0,
}

KNOWN_BLOCKED: dict[str, str] = {
    "data-tsa.json":      "tsa.gov 403s GitHub Actions IPs (browser UA already sent)",
    "data-travel.json":   "travel.state.gov scrape fails from CI; root cause unconfirmed",
    "data/btc_flows.csv": "needs COINGLASS_API_KEY or SOSOVALUE_API_KEY; free mirror is dead (ends 2025-05-02)",
    "data/eth_flows.csv": "needs COINGLASS_API_KEY or SOSOVALUE_API_KEY (mirror is BTC-only anyway)",
}


def main() -> int:
    now = datetime.now(timezone.utc).timestamp()
    stale: list[str] = []
    blocked: list[str] = []
    ok: list[str] = []
    unknown: list[str] = []

    for rel, owner in TRACKED.items():
        path = REPO_ROOT / rel
        if not path.exists():
            unknown.append(f"{rel}: MISSING on disk (refreshed by {owner})")
            continue

        age_h = _content_age_h(path, now)
        if age_h is None:
            unknown.append(f"{rel}: no readable date signal inside the file")
            continue

        limit = WEEKDAY_ONLY_LIMIT_H.get(
            rel, THRESHOLDS.get(path.name, DEFAULT).stale_h
        )
        line = f"{rel}: {humanize_age(age_h)} old (limit {humanize_age(limit)}) - {owner}"
        if age_h <= limit:
            ok.append(line)
        elif rel in KNOWN_BLOCKED:
            blocked.append(f"{line}\n      blocker: {KNOWN_BLOCKED[rel]}")
        else:
            stale.append(line)

    print(f"fresh ({len(ok)}):")
    for line in ok:
        print(f"  OK    {line}")

    if blocked:
        print(f"\nknown-blocked ({len(blocked)}) - reported, not failing:")
        for line in blocked:
            print(f"  BLOCKED {line}")

    if unknown:
        print(f"\nundetermined ({len(unknown)}):")
        for line in unknown:
            print(f"  ?     {line}")

    if stale:
        print(f"\nSTALE ({len(stale)}) - these regressed and need attention:")
        for line in stale:
            print(f"  STALE {line}")

    # GitHub Actions job summary — renders on the run page so the alert email
    # links somewhere that actually explains itself.
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as fh:
            fh.write("## Data freshness\n\n")
            if stale:
                fh.write(f"**{len(stale)} feed(s) went stale.**\n\n")
                for line in stale:
                    fh.write(f"- `{line}`\n")
                fh.write("\n")
            else:
                fh.write("No new staleness. \n\n")
            if blocked:
                fh.write(f"<details><summary>{len(blocked)} known-blocked "
                         f"(not failing)</summary>\n\n")
                for line in blocked:
                    fh.write(f"- `{line}`\n")
                fh.write("\n</details>\n\n")
            fh.write(f"{len(ok)} feed(s) fresh.\n")

    if stale:
        print(f"\nFAIL: {len(stale)} feed(s) past threshold.")
        return 1

    print("\nOK: no unexpected staleness.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
