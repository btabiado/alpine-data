#!/usr/bin/env python3
"""Fetch CFPB Consumer Complaint counts and publish data-cfpb.json.

WHY THIS LIVES HERE (and not in the ADW Worker):
The CFPB Consumer Complaint DB API blocks Cloudflare datacenter IP ranges, so the
ADW Worker (Cloudflare) gets a silent failure when it fetches CFPB directly — the
product (ADW-397, Student-Loan Servicer Complaint Velocity) served DATA_UNAVAILABLE
on the edge even though the source is fine. GitHub Actions runners have clean
(non-datacenter) egress, so we fetch here and publish the raw counts as a keyless
JSON feed. The Worker reads it via fetchAlpineFeed('cfpb') and does the scoring.

Pure stdlib — no pip install. Non-zero exit fails the workflow on purpose so a
dead fetcher is visible in the Actions UI rather than silently stale.
"""
import json
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

BASE = ("https://www.consumerfinance.gov/data-research/consumer-complaints/"
        "search/api/v1/")
UA = "AlpineDataWorks-feed/1.0 (+https://alpinedataworks.com)"


# One read of the complaint search API. The 2026-10-12 run (38277091475)
# died on the first request: urllib raised TimeoutError while reading the
# status line, and that is not a URLError (urlopen only wraps an OSError
# raised from the request write). Nothing was written, so the commit step
# never ran and the day's snapshot is gone. A short stall has to be retried
# inside this process; the cron does not come back the same day.
ATTEMPTS = 4
TIMEOUT_S = 90


def _backoff(attempt: int) -> None:
    """Pause before another try. `attempt` is 0-based and already failed."""
    time.sleep(min(8.0, 0.8 * (attempt + 1)))


def cfpb_count(product: str, min_d: str, max_d: str) -> int:
    """Total complaint count for a product in [min_d, max_d] (no aggs).

    Retries a stalled or 5xx/429 response. A 4xx other than 429 is raised
    immediately: repeating a bad query will not make the product name valid.
    The last error is re-raised when every attempt fails, so the caller can
    still fail the workflow instead of publishing a hole.
    """
    q = (f"{BASE}?product={urllib.parse.quote(product)}&size=0&no_aggs=true"
         f"&date_received_min={min_d}&date_received_max={max_d}")
    last: BaseException | None = None
    for attempt in range(ATTEMPTS):
        req = urllib.request.Request(q, headers={"User-Agent": UA,
                                                 "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
                j = json.loads(r.read().decode("utf-8"))
            total = j.get("hits", {}).get("total")
            if isinstance(total, dict):
                return int(total.get("value", 0))
            return int(total or 0)
        except urllib.error.HTTPError as e:
            last = e
            retry = e.code == 429 or e.code >= 500
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            # TimeoutError while getresponse() reads the status line is the
            # 2026-10-12 failure. It is an OSError, not a URLError.
            last = e
            retry = True
        if not retry:
            raise last
        print(f"warn: {product!r} attempt {attempt + 1}/{ATTEMPTS} failed: {last}",
              file=sys.stderr)
        if attempt + 1 < ATTEMPTS:
            _backoff(attempt)
    if last is None:
        raise RuntimeError(f"CFPB count for {product!r} produced no result")
    raise last


# (payload key, CFPB product name). The names must match the CFPB product
# taxonomy EXACTLY: the API answers an unknown/retired name with a count of 0,
# not an error. "Credit card or prepaid card" was retired and split into
# "Credit card" and "Prepaid card" — querying the old name published zeros for
# months (verified live 2026-10-04: 0 vs 92,619 + 6,119 trailing 12 months).
PRODUCTS = [
    ("student_loan", "Student loan"),
    ("mortgage", "Mortgage"),
    ("credit_card", "Credit card"),
    ("prepaid_card", "Prepaid card"),
]


def main() -> int:
    now = datetime.now(timezone.utc)
    iso = lambda d: d.strftime("%Y-%m-%d")
    d90 = now - timedelta(days=90)
    d365 = now - timedelta(days=365)

    out = {
        "generated_utc": now.isoformat(),
        "source": "CFPB Consumer Complaint Database",
        "note": ("Published from GitHub Actions (clean egress) because the CFPB "
                 "API blocks Cloudflare datacenter IPs. Consumed by ADW-397."),
        "windows": {"recent_days": 90, "baseline_days": 365},
        "products": {},
    }

    # Student loan is the ADW-397 signal; fetch a couple of adjacent categories
    # cheaply too so the feed is reusable for future products.
    for key, product in PRODUCTS:
        try:
            r3 = cfpb_count(product, iso(d90), iso(now))
            r12 = cfpb_count(product, iso(d365), iso(now))
            if r12 == 0:
                # A national product category with zero complaints in a year is
                # a renamed/retired taxonomy entry, not a real reading.
                print(f"warn: {key} ({product!r}) returned 0 complaints in 12 "
                      f"months — product name likely retired upstream",
                      file=sys.stderr)
            out["products"][key] = {
                "product": product,
                "complaints_recent_3mo": r3,
                "complaints_trailing_12mo": r12,
                "window_recent": [iso(d90), iso(now)],
                "window_baseline": [iso(d365), iso(now)],
            }
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
                OSError, ValueError) as e:
            # Student loan is required (ADW-397); others are best-effort.
            if key == "student_loan":
                print(f"FATAL: student_loan fetch failed: {e}", file=sys.stderr)
                return 1
            print(f"warn: {key} fetch failed: {e}", file=sys.stderr)

    if "student_loan" not in out["products"]:
        print("FATAL: no student_loan data", file=sys.stderr)
        return 1

    with open("data-cfpb.json", "w") as f:
        json.dump(out, f, indent=1)
    sl = out["products"]["student_loan"]
    print(f"wrote data-cfpb.json | student_loan 3mo={sl['complaints_recent_3mo']} "
          f"12mo={sl['complaints_trailing_12mo']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
