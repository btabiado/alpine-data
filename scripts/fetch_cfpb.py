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
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

BASE = ("https://www.consumerfinance.gov/data-research/consumer-complaints/"
        "search/api/v1/")
UA = "AlpineDataWorks-feed/1.0 (+https://alpinedataworks.com)"


def cfpb_count(product: str, min_d: str, max_d: str) -> int:
    """Total complaint count for a product in [min_d, max_d] (no aggs, fast)."""
    q = (f"{BASE}?product={urllib.parse.quote(product)}&size=0&no_aggs=true"
         f"&date_received_min={min_d}&date_received_max={max_d}")
    req = urllib.request.Request(q, headers={"User-Agent": UA,
                                             "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=45) as r:
        j = json.loads(r.read().decode("utf-8"))
    total = j.get("hits", {}).get("total")
    if isinstance(total, dict):
        return int(total.get("value", 0))
    return int(total or 0)


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
    for key, product in [
        ("student_loan", "Student loan"),
        ("mortgage", "Mortgage"),
        ("credit_card", "Credit card or prepaid card"),
    ]:
        try:
            r3 = cfpb_count(product, iso(d90), iso(now))
            r12 = cfpb_count(product, iso(d365), iso(now))
            out["products"][key] = {
                "product": product,
                "complaints_recent_3mo": r3,
                "complaints_trailing_12mo": r12,
                "window_recent": [iso(d90), iso(now)],
                "window_baseline": [iso(d365), iso(now)],
            }
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError) as e:
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
    import urllib.parse  # noqa: E402 (kept local so the top stays stdlib-obvious)
    sys.exit(main())
