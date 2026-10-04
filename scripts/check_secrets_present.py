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
    # Highest-impact entry in this table. Keyless CoinGecko is ~30 req/min; the
    # top-50 sweep in fetch_trading exceeds that, and a 429 returns [] which
    # sends markets_top down stale-keep. This key was missing from the audit
    # entirely, so "no CoinGecko key" was unreportable while it froze the
    # front-page BTC price for 16 days.
    ("COINGECKO_API_KEY",     "Crypto prices/markets_top - lifts 30 req/min",   "pages, lthcs-crypto-daily"),
    ("GLASSNODE_API_KEY",     "True BTC whale-cohort metrics",                  "pages"),
    ("COINMETRICS_API_KEY",   "ETH whale series on the Whale tab",              "pages"),
    ("ETHERSCAN_API_KEY",     "ETH blocks/day chart on the Whale tab",          "pages"),
    ("COINGLASS_API_KEY",     "Crypto ETF flow history (per-fund)",             "(not yet wired)"),
    ("SOSOVALUE_API_KEY",     "Crypto ETF flow history (alternative)",          "(not yet wired)"),
    ("EIA_API_KEY",           "Energy supplies (V2)",                           "pages"),
    ("ALPHA_VANTAGE_API_KEY", "LTHCS financial pillar",                         "pages, lthcs-daily"),
    ("FINNHUB_API_KEY",       "LTHCS thesis pillar",                            "pages"),
    ("R2_ACCESS_KEY_ID",      "R2 warehouse archive upload",                    "pages, r2-backfill"),
    # ---- Added 2026-10-04 ---------------------------------------------------
    # These nine are referenced by `secrets.X` in a workflow but were never in
    # this table, so the audit printed "17 set/missing" and looked complete
    # while nine inputs were unreportable. That is not cosmetic: the first row
    # below silently flattened a whole scoring pillar for four months.
    #
    # SEC_USER_AGENT has no default. lthcs/sources/sec_edgar.py raises
    # SECEdgarError the moment it is empty, every call, and the financial
    # pillar falls back to its neutral 50 for all ~215 tickers. The snapshots
    # show that exact signature: per-ticker stdev 17.44 on 2026-06-04, then
    # 0.00 on 2026-06-05 and every day since. Nothing reported it, because the
    # one tool that answers "did this reach Actions?" wasn't watching it.
    ("SEC_USER_AGENT",        "LTHCS financial pillar via SEC EDGAR; SEC REQUIRES a contact-email UA and the fetcher raises without it", "lthcs-daily, lthcs-news-hourly"),
    ("ANTHROPIC_API_KEY",     "LLM sentiment + narrative generation",           "lthcs-daily"),
    ("OPENSKY_CLIENT_ID",     "OpenSky OAuth - live aircraft positions",        "aviation-opensky"),
    ("OPENSKY_CLIENT_SECRET", "OpenSky OAuth - live aircraft positions",        "aviation-opensky"),
    ("REDDIT_CLIENT_ID",      "Reddit breadth sentiment",                       "pages"),
    ("REDDIT_CLIENT_SECRET",  "Reddit breadth sentiment",                       "pages"),
    # The last three reach their consumers RENAMED (boto3 reads AWS_*, gh reads
    # GH_TOKEN), which is why a literal-name grep never found them. Renaming
    # happens downstream; whether the secret is set at all is still this
    # table's question, and it is the question people actually ask.
    ("R2_SECRET_ACCESS_KEY",  "R2 upload - reaches boto3 as AWS_SECRET_ACCESS_KEY", "pages, r2-backfill"),
    ("R2_BUCKET_NAME",        "R2 upload - target bucket",                      "pages, r2-backfill"),
    ("SECURITY_AUDIT_TOKEN",  "security-audit Dependabot + secret-scanning reads - reaches gh as GH_TOKEN", "security-audit"),
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
