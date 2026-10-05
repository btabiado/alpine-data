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

# Annotation prefixes that assert "no workflow maps this key". They are checked
# by prefix (not equality) so each row can explain itself in its own words while
# still making a machine-checkable claim.
#
#   (not yet wired  — aspirational. Nothing reads it YET; wiring is pending.
#   (retired        — dead. Something used to read it, or was supposed to, and
#                     nothing does now. Setting it will never do anything again.
#
# The distinction is not pedantry. Both print as MISSING, but "not yet wired"
# invites you to wait and "retired" tells you to stop waiting — and three keys
# in this list spent months in the first category while actually being in the
# second.
UNWIRED_PREFIXES: tuple[str, ...] = ("(not yet wired", "(retired")


def _is_unwired(where: str) -> bool:
    return where.startswith(UNWIRED_PREFIXES)


# (env var, what it unlocks, which workflow(s) map it in)
#
# The third column is the honest answer to "who would notice if this key were
# empty?", derived by grepping .github/workflows for each name — NOT by
# guessing. An UNWIRED_PREFIXES value means the key appears in no workflow
# except secrets-check.yml itself, so setting it changes nothing. Keeping that
# annotation accurate is the entire value of the column: a key labelled with a
# workflow that does not map it sends the reader to the wrong file, which is
# worse than no label at all.
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
    ("FBI_CDE_API_KEY",       "City: FBI Crime Data Explorer - OPTIONAL, the "
                              "cde.ucr.cjis.gov series is keyless; forwarded if set",
                                                                                "city-daily"),
    ("AIRNOW_API_KEY",        "City: AirNow air quality",                       "city-daily"),
    ("FRED_API_KEY",          "Macro overlay, CPI, metals, real estate",        "pages, lthcs-daily, real-estate-daily"),
    # RETIRED 2026-10. CryptoCompare runs on CoinDesk Data now and every host
    # (min-api / data-api.cryptocompare.com, data-api.coindesk.com) 401s keyless
    # requests; the owner chose not to buy a plan. Everything it fed moved to
    # free sources: top-50 POC + signal breadth -> CoinGecko market_chart with
    # Coinbase/Kraken/Binance.US fallbacks; CADLI chart -> Alpine Data's own
    # Large-Cap Crypto Index (CoinGecko market caps); social/dev stats ->
    # CoinGecko + GitHub; news sentiment -> Google News RSS scored by our own
    # keyword rule. No workflow maps it and no code reads it, so setting it
    # does nothing. Kept in the report so a leftover secret reads as dead.
    ("CRYPTOCOMPARE_API_KEY", "NOTHING since 2026-10. Was per-coin OHLCV, the "
                              "CADLI chart and CC social/news; all replaced "
                              "with free sources",
                                                                                "(retired — replaced by free sources)"),
    ("GLASSNODE_API_KEY",     "True BTC whale-cohort metrics",                  "pages"),
    ("COINMETRICS_API_KEY",   "ETH whale series on the Whale tab",              "pages"),
    ("ETHERSCAN_API_KEY",     "ETH blocks/day chart on the Whale tab",          "pages"),
    # --- retired: kept in the report on purpose, see the note below the list ---
    ("COINGLASS_API_KEY",     "NOTHING. Was an ETF-flow fallback; BTC ETF flows "
                              "come from the keyless Farside mirror CSV instead",
                                                                                "(retired — dead path)"),
    ("SOSOVALUE_API_KEY",     "NOTHING. api.sosovalue.com no longer resolves "
                              "(NXDOMAIN); the API subdomain was decommissioned",
                                                                                "(retired — upstream gone)"),
    ("EIA_API_KEY",           "Energy supplies (V2)",                           "pages"),
    ("ALPHA_VANTAGE_API_KEY", "LTHCS financial pillar",                         "pages, lthcs-daily"),
    ("FINNHUB_API_KEY",       "LTHCS thesis pillar",                            "pages"),
    ("R2_ACCESS_KEY_ID",      "R2 warehouse archive upload",                    "pages, r2-backfill"),
    # Live, not retired: fetch_market.py and lthcs/sources/crypto_data.py both
    # read it (wired in by #29), and pages.yml + lthcs-crypto-daily.yml pass it.
    ("COINGECKO_API_KEY",     "Crypto prices/markets_top - lifts the keyless "
                              "~30 req/min CoinGecko limit",
                                                                                "pages, lthcs-crypto-daily"),
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

# =========================================================================
# THE THREE KNOWN FALSE ALARMS. READ THIS BEFORE "FIXING" ANY OF THEM.
# =========================================================================
# Three keys in the list above are invisible to a literal grep for their own
# name in the code that consumes them, because they are RENAMED on the way in.
# They work. They have always worked. Written down here because the obvious
# "fix" — renaming the secret, or rewiring the workflow to pass the name the
# code appears to want — breaks a working path to silence a report that was
# never describing a real problem.
#
#   R2_SECRET_ACCESS_KEY   pages.yml and r2-backfill.yml map it into the job as
#                          AWS_SECRET_ACCESS_KEY, and its partner
#                          R2_ACCESS_KEY_ID as AWS_ACCESS_KEY_ID, because
#                          upload_to_r2.py talks to Cloudflare R2 through boto3,
#                          which only ever reads the AWS_* names. Grepping the
#                          uploader for "R2_SECRET_ACCESS_KEY" finds nothing and
#                          proves nothing.
#   R2_BUCKET_NAME         travels under its own name, but is read inside an
#                          inline `python3 - <<PY` heredoc in r2-backfill.yml
#                          and by upload_to_r2.py — not anywhere a search of
#                          "the fetchers" would look. Also genuinely fine.
#   SECURITY_AUDIT_TOKEN   security-audit.yml maps it as
#                          `GH_TOKEN: ${{ secrets.SECURITY_AUDIT_TOKEN ||
#                          github.token }}`, because the `gh` CLI reads GH_TOKEN
#                          and nothing else. The fallback is the subtle part: an
#                          unset SECURITY_AUDIT_TOKEN does not fail, it quietly
#                          drops to github.token, which cannot read Dependabot
#                          or secret-scanning alerts. So "MISSING" here is real
#                          and worth acting on — but the ALIAS is not a bug.
#
# The ALIASES footnote below prints the mapping next to the key, so a MISSING
# row never sends someone to re-paste a secret that was never the problem.
ALIASES: dict[str, str] = {
    "R2_ACCESS_KEY_ID": "AWS_ACCESS_KEY_ID (boto3 speaks to R2 over the S3 API)",
    "R2_SECRET_ACCESS_KEY": "AWS_SECRET_ACCESS_KEY (same reason)",
    "R2_BUCKET_NAME": "R2_BUCKET_NAME (same name; read by upload_to_r2.py and "
                      "an inline heredoc in r2-backfill.yml, not by a fetcher)",
    "SECURITY_AUDIT_TOKEN": "GH_TOKEN (the gh CLI reads only GH_TOKEN; it falls "
                            "back to github.token, which cannot read Dependabot "
                            "or secret-scanning alerts)",
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
        unwired = [(n, w) for n, w in missing if _is_unwired(w)]
        wired = [(n, w) for n, w in missing if not _is_unwired(w)]
        if wired:
            print("\nMissing keys and the workflow that expects each:")
            for name, where in wired:
                print(f"  {name:24} -> {where}")
                if name in ALIASES:
                    print(f"  {'':24}    NOTE: mapped into the job as "
                          f"{ALIASES[name]}")
        if unwired:
            print("\nMissing and referenced by no workflow — setting these "
                  "changes NOTHING. '(not yet wired)' means wiring is pending; "
                  "'(retired)' means it is never coming back:")
            for name, where in unwired:
                print(f"  {name:24} -> {where}")
    # Same split for the keys that ARE set: a set-but-retired key is money and
    # attention spent on nothing, and this report is the only place that says so.
    retired_but_set = [(n, w) for n, u, w in KEYS
                       if n in present and w.startswith("(retired")]
    if retired_but_set:
        print("\nSet, but RETIRED — these reach no code at all. Nothing breaks "
              "if you delete them from repository secrets:")
        for name, where in retired_but_set:
            print(f"  {name:24} -> {where}")
    if any(not _is_unwired(w) for _, w in missing):
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
                         "all, so setting them changes nothing yet; rows marked "
                         "`(retired…)` are dead and setting them will never "
                         "change anything again.\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
