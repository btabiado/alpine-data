#!/usr/bin/env python3
"""Report which API keys actually reach the workflows — presence only.

WHY
---
"Those keys are not missing, they've been updated multiple times" versus a CI
log showing `SOCRATA_APP_TOKEN:` empty. Both can be true at once, and there was
no way to tell which of several stores a key landed in, because GitHub secrets
are write-only: nothing — no person reading the UI, no script, no agent — can
read a secret's value back. The only observable is whether a workflow received
a non-empty string.

So this prints exactly that observable, per key, and nothing else.

*** THIS SCRIPT NEVER PRINTS A SECRET VALUE. ***
It prints only a boolean and a length. Do not "improve" it by echoing values —
workflow logs are retained and, on a public repo, world-readable. GitHub's log
masking is a safety net, not a license.

THE USUAL CAUSE OF A "SET BUT EMPTY" KEY
----------------------------------------
GitHub has several separate secret stores that look nearly identical in the UI:

  Settings -> Secrets and variables -> Actions -> *Repository secrets*
      ^ the only one every job in this repo can see.
  Settings -> Secrets and variables -> Actions -> *Environment secrets*
      ^ visible ONLY to a job that declares `environment: <name>`.
        In this repo just pages.yml's DEPLOY job does that, and the deploy job
        fetches nothing. A key added here is invisible to every fetcher.
  Settings -> Secrets and variables -> *Dependabot* / *Codespaces* tabs
      ^ entirely different stores. Adding here does nothing for Actions.
  Organization secrets not shared with this repository.
  Cloudflare Worker secrets (the ADW Worker) — a different system entirely.

Run it in CI with the same `env:` block as the job you are debugging.
Locally it just reports your shell environment.

Exit code is always 0: this is a diagnostic, not a gate.
"""
from __future__ import annotations

import os
import sys

# (env var, what it unlocks, which workflow(s) map it in)
KEYS: list[tuple[str, str, str]] = [
    ("SOCRATA_APP_TOKEN",     "City: lifts Socrata rate limit (avoids 429)",   "city-daily"),
    ("CENSUS_API_KEY",        "City: Census ACS median income",                 "city-daily"),
    ("BLS_API_KEY",           "City: BLS LAUS unemployment",                    "city-daily"),
    ("FBI_CDE_API_KEY",       "City: FBI Crime Data Explorer",                  "city-daily"),
    ("AIRNOW_API_KEY",        "City: AirNow air quality",                       "city-daily"),
    ("FRED_API_KEY",          "Macro overlay, CPI, metals, real estate",        "pages, lthcs-daily, real-estate-daily"),
    ("CRYPTOCOMPARE_API_KEY", "Per-coin OHLCV -> POC + signal-breadth chart",   "pages, lthcs-crypto-daily"),
    ("GLASSNODE_API_KEY",     "True BTC whale-cohort metrics",                  "pages"),
    ("COINMETRICS_API_KEY",   "ETH whale series on the Whale tab",              "pages"),
    ("ETHERSCAN_API_KEY",     "ETH blocks/day chart on the Whale tab",          "pages"),
    ("COINGLASS_API_KEY",     "Crypto ETF flow history (per-fund)",             "(not yet wired)"),
    ("SOSOVALUE_API_KEY",     "Crypto ETF flow history (alternative)",          "(not yet wired)"),
    ("EIA_API_KEY",           "Energy supplies (V2)",                           "pages"),
    ("ALPHA_VANTAGE_API_KEY", "LTHCS financial pillar",                         "pages, lthcs-daily"),
    ("FINNHUB_API_KEY",       "LTHCS thesis pillar",                            "pages"),
    ("R2_ACCESS_KEY_ID",      "R2 warehouse archive upload",                    "pages, r2-backfill"),
]


def main() -> int:
    present, missing = [], []
    print(f"{'KEY':24} {'STATUS':9} {'LEN':>4}  UNLOCKS")
    print("-" * 100)
    for name, unlocks, where in KEYS:
        raw = os.environ.get(name)
        val = (raw or "").strip()
        if val:
            present.append(name)
            # Length only. Enough to catch a truncated paste or a stray quote,
            # useless to an attacker.
            print(f"{name:24} {'set':9} {len(val):>4}  {unlocks}")
        else:
            missing.append((name, where))
            print(f"{name:24} {'MISSING':9} {'-':>4}  {unlocks}")

    print(f"\n{len(present)} set · {len(missing)} missing")

    if missing:
        print("\nMissing keys and the workflow that expects each:")
        for name, where in missing:
            print(f"  {name:24} -> {where}")
        print(
            "\nIf you believe one of these IS set, it is almost certainly in the\n"
            "wrong store. Check, in this order:\n"
            "  1. Settings > Secrets and variables > Actions > Repository secrets\n"
            "     (the only store every job here can read)\n"
            "  2. ...> Environment secrets — visible ONLY to a job declaring\n"
            "     `environment:`. In this repo that is pages.yml's DEPLOY job\n"
            "     alone, which fetches nothing, so keys parked there never reach\n"
            "     a fetcher.\n"
            "  3. The Dependabot / Codespaces tabs — separate stores; Actions\n"
            "     cannot see them.\n"
            "  4. Organization secrets not shared with this repository.\n"
            "  5. Cloudflare Worker secrets (the ADW Worker) — different system.\n"
            "\nA key must be a REPOSITORY secret here, and the workflow must map it\n"
            "into `env:` for the step that needs it."
        )

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write("## API key presence\n\n")
            fh.write(f"**{len(present)} set · {len(missing)} missing**\n\n")
            if missing:
                fh.write("| Key | Expected by |\n|---|---|\n")
                for name, where in missing:
                    fh.write(f"| `{name}` | {where} |\n")
                fh.write("\nA missing key here means Actions received an empty "
                         "string — most often because it was saved as an "
                         "*Environment* secret (only `pages.yml`'s deploy job "
                         "declares one) or under the Dependabot/Codespaces tab, "
                         "rather than as a **Repository** secret.\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
