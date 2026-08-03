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
#
# The third column is the honest answer to "who would notice if this key were
# empty?", derived by grepping .github/workflows for each name — NOT by
# guessing. "(not yet wired)" means the key appears in no workflow except
# secrets-check.yml itself, so setting it changes nothing until someone wires
# it. Keeping that annotation accurate is the entire value of the column: a key
# labelled with a workflow that does not map it sends the reader to the wrong
# file, which is worse than no label at all.
# tests/test_data_health.py::test_secret_workflow_annotations_match_reality
# re-derives the column from the workflow files, so it cannot drift silently.
#
# Ten keys the workflows DO reference were missing from this list entirely.
# Every one of them sat outside the audit: a rotated-out OPENSKY_CLIENT_SECRET
# or an emptied ANTHROPIC_API_KEY would have surfaced as a mysteriously
# degraded feed with nothing in this report to explain it.
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
    # --- previously unaudited; every one is referenced by a real workflow ----
    ("COINGECKO_API_KEY",     "LTHCS crypto: Demo/Pro tier lifts the CoinGecko rate limit",
                                                                                "lthcs-crypto-daily"),
    ("REDDIT_CLIENT_ID",      "Reddit OAuth for the research/sentiment pull",   "pages"),
    ("REDDIT_CLIENT_SECRET",  "Reddit OAuth (pairs with REDDIT_CLIENT_ID)",     "pages"),
    ("OPENSKY_CLIENT_ID",     "OpenSky OAuth2 - higher limits for the hourly flight snapshot",
                                                                                "aviation-opensky"),
    ("OPENSKY_CLIENT_SECRET", "OpenSky OAuth2 (pairs with OPENSKY_CLIENT_ID)",  "aviation-opensky"),
    ("ANTHROPIC_API_KEY",     "chat.py assistant; LTHCS narrative generation",  "lthcs-daily"),
    ("R2_SECRET_ACCESS_KEY",  "R2 archive upload - secret half of R2_ACCESS_KEY_ID",
                                                                                "pages, r2-backfill"),
    ("R2_BUCKET_NAME",        "R2 archive upload - destination bucket",         "pages, r2-backfill"),
    ("SEC_USER_AGENT",        "SEC EDGAR demands a contact UA; without it Form 4/D fetches skip",
                                                                                "lthcs-daily, lthcs-news-hourly"),
    ("SECURITY_AUDIT_TOKEN",  "PAT for Dependabot + secret-scanning alerts (falls back to "
                              "github.token, which cannot read either)",        "security-audit"),
]

# Keys the workflows hand to the job under a DIFFERENT env name. Printed as a
# footnote because a secret that IS set but arrives under an alias is the most
# confusing possible way for this report to say "MISSING" — it invites someone
# to re-paste a key that was never the problem.
ALIASES: dict[str, str] = {
    "R2_ACCESS_KEY_ID": "AWS_ACCESS_KEY_ID (boto3 speaks to R2 over the S3 API)",
    "R2_SECRET_ACCESS_KEY": "AWS_SECRET_ACCESS_KEY (same reason)",
}


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
        # Split the report: a key no workflow maps is a TODO, not an outage,
        # and mixing the two is how a real gap gets lost in a list of
        # aspirational ones.
        unwired = [(n, w) for n, w in missing if w.startswith("(not yet wired")]
        wired = [(n, w) for n, w in missing if not w.startswith("(not yet wired")]
        if wired:
            print("\nMissing keys and the workflow that expects each:")
            for name, where in wired:
                print(f"  {name:24} -> {where}")
                if name in ALIASES:
                    print(f"  {'':24}    NOTE: mapped into the job as "
                          f"{ALIASES[name]}")
        if unwired:
            print("\nMissing but referenced by no workflow — setting these "
                  "changes nothing until they are wired:")
            for name, where in unwired:
                print(f"  {name:24} -> {where}")
    if any(not w.startswith("(not yet wired") for _, w in missing):
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
                fh.write("| Key | Expected by | Alias in the job |\n|---|---|---|\n")
                for name, where in missing:
                    fh.write(f"| `{name}` | {where} | {ALIASES.get(name, '—')} |\n")
                fh.write("\nA missing key here means Actions received an empty "
                         "string — most often because it was saved as an "
                         "*Environment* secret (only `pages.yml`'s deploy job "
                         "declares one) or under the Dependabot/Codespaces tab, "
                         "rather than as a **Repository** secret. Rows marked "
                         "`(not yet wired)` are referenced by no workflow at "
                         "all, so setting them changes nothing yet.\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
