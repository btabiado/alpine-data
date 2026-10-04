#!/usr/bin/env python3
"""Fetch USAspending.gov obligation series and publish data-usaspending.json.

WHY THIS LIVES HERE (and not in the ADW Worker):
The USAspending.gov Search API blocks / 525s Cloudflare datacenter IP ranges, so
the ADW Worker (Cloudflare) gets a silent failure when it fetches USAspending
directly — two products (ADW-441 Federal Grant Liquidity Monitor and ADW-444
Defense Contract Award Lag Index) served 503 / DATA_UNAVAILABLE on the edge even
though the source is fine. GitHub Actions runners have clean (non-datacenter)
egress, so we fetch here and publish the raw monthly series as a keyless JSON
feed. The Worker reads it via fetchAlpineFeed('usaspending') and does the scoring.

Same block as CFPB (see fetch_cfpb.py) — mirrors that fetcher's structure.

Both products read ONE endpoint:
  POST https://api.usaspending.gov/api/v2/search/spending_over_time/
  - grants   : filters.award_type_codes = 02/03/04/05, use the Grant_Obligations
               field per fiscal-month row (24-month window).
  - dod       : filters.award_type_codes = A/B/C/D + awarding-toptier
               "Department of Defense", use aggregated_amount per fiscal-month row
               (~13-month window). "awards" carries the monthly obligation $ used
               as the cadence variable (see dod_contract_lag.ts for why).

USAspending's time_period.month is a FISCAL month (1 = October); we convert each
row to a calendar "YYYY-MM" so the Worker can feed the series straight into its
existing scoring functions.

Pure stdlib — no pip install. Non-zero exit fails the workflow on purpose so a
dead fetcher is visible in the Actions UI rather than silently stale.
"""
import json
import sys
import urllib.request
import urllib.error
import time
from datetime import datetime, timezone

ENDPOINT = "https://api.usaspending.gov/api/v2/search/spending_over_time/"
# Browser UA + retry mirrors the TS adapters: USAspending intermittently 525s
# (CF<->origin TLS) and 429s; a browser UA with backoff clears it.
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

GRANT_AWARD_TYPE_CODES = ["02", "03", "04", "05"]  # block/formula/project grants + coop agreements
DOD_AWARD_TYPE_CODES = ["A", "B", "C", "D"]          # definitive contracts (excl. IDVs/grants/loans)


def fiscal_month_to_period(fiscal_year, fiscal_month):
    """Fiscal (year, month) -> calendar 'YYYY-MM'. Fiscal month 1 = October."""
    fy = int(fiscal_year)
    fm = int(fiscal_month)
    cal_month = fm + 9 if fm <= 3 else fm - 3
    cal_year = fy - 1 if fm <= 3 else fy
    return f"{cal_year}-{cal_month:02d}"


def post_spending_over_time(filters):
    """POST spending_over_time grouped by month; returns results[] or raises."""
    body = json.dumps({"group": "month", "filters": filters}).encode("utf-8")
    last_err = None
    for attempt in range(4):
        req = urllib.request.Request(
            ENDPOINT, data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "Accept": "application/json",
                     "User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                j = json.loads(r.read().decode("utf-8"))
            results = j.get("results")
            if not isinstance(results, list):
                raise ValueError("malformed response (no results[])")
            return results
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code >= 500 or e.code == 429:
                time.sleep(0.8 * (attempt + 1))
                continue
            raise
        except (urllib.error.URLError, ValueError) as e:
            last_err = e
            time.sleep(0.8 * (attempt + 1))
    raise RuntimeError(f"spending_over_time failed after retries: {last_err}")


def collapse_by_period(results, value_field):
    """Sum a value field per calendar month; returns [(period, value)] oldest->newest."""
    by_period = {}
    for row in results:
        tp = row.get("time_period") or {}
        fy, fm = tp.get("fiscal_year"), tp.get("month")
        if not fy or not fm:
            continue
        try:
            val = float(row.get(value_field))
        except (TypeError, ValueError):
            continue
        period = fiscal_month_to_period(fy, fm)
        by_period[period] = by_period.get(period, 0.0) + val
    return sorted(by_period.items())


def months_ago_iso(now, months):
    """First-of-window date `months` before `now`, as 'YYYY-MM-DD' (mirrors TS)."""
    y = now.year
    m = now.month - months
    while m <= 0:
        m += 12
        y -= 1
    return f"{y}-{m:02d}-{now.day:02d}"


def main():
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")

    out = {
        "generated_utc": now.isoformat(),
        "source": "USAspending.gov Search API — spending_over_time",
        "note": ("Published from GitHub Actions (clean egress) because the "
                 "USAspending API blocks/525s Cloudflare datacenter IPs. "
                 "Consumed by ADW-441 (grants) and ADW-444 (dod_contracts)."),
    }

    # ── ADW-441: grant obligations, 24-month window (Grant_Obligations field) ──
    grants_start = months_ago_iso(now, 24)
    grant_results = post_spending_over_time({
        "award_type_codes": GRANT_AWARD_TYPE_CODES,
        "time_period": [{"start_date": grants_start, "end_date": today}],
    })
    grant_series = [{"period": p, "obligated": round(v, 2)}
                    for p, v in collapse_by_period(grant_results, "Grant_Obligations")]
    if not grant_series:
        print("FATAL: grants series parsed 0 periods", file=sys.stderr)
        return 1
    out["grants"] = {
        "window": [grants_start, today],
        "award_type_codes": GRANT_AWARD_TYPE_CODES,
        "value_field": "Grant_Obligations",
        "series": grant_series,
    }

    # ── ADW-444: DoD contract obligations, ~13-month window (aggregated_amount) ─
    # Mirrors computeADW444: start = first-of-month, 13 months back.
    dod_y, dod_m = now.year, now.month - 1
    while dod_m <= 0:
        dod_m += 12
        dod_y -= 1
    dod_y -= 1  # year-1 per Date.UTC(year-1, month-1, 1)
    dod_start = f"{dod_y}-{dod_m:02d}-01"
    dod_results = post_spending_over_time({
        "award_type_codes": DOD_AWARD_TYPE_CODES,
        "agencies": [{"type": "awarding", "tier": "toptier",
                      "name": "Department of Defense"}],
        "time_period": [{"start_date": dod_start, "end_date": today}],
    })
    dod_pairs = collapse_by_period(dod_results, "aggregated_amount")
    if not dod_pairs:
        print("FATAL: dod_contracts series parsed 0 periods", file=sys.stderr)
        return 1
    dod_series = [{"month": mo, "awards": round(v), "totalAmount": round(v, 2)}
                  for mo, v in dod_pairs]
    out["dod_contracts"] = {
        "window": [dod_start, today],
        "award_type_codes": DOD_AWARD_TYPE_CODES,
        "agency": "Department of Defense (awarding toptier)",
        "value_field": "aggregated_amount",
        "series": dod_series,
    }

    with open("data-usaspending.json", "w") as f:
        json.dump(out, f, indent=1)

    g_last = grant_series[-1]
    d_last = dod_series[-1]
    print(f"wrote data-usaspending.json | grants {len(grant_series)} periods "
          f"(latest {g_last['period']}={g_last['obligated']:.0f}) | "
          f"dod {len(dod_series)} months "
          f"(latest {d_last['month']}={d_last['totalAmount']:.0f})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
