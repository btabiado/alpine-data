#!/usr/bin/env python3
"""
check_catalog_links.py — link-health validator for the Data Sources Catalog.

Reads health/api_catalog.json, HTTP-checks every unique service URL, and
writes health/catalog_health.json (+ a short markdown summary) classifying
each link as:

    ok           2xx / final 3xx — reachable
    gated        401/402/403/429  — the endpoint EXISTS but is auth-walled,
                                     rate-limited, or bot-blocked (NOT dead)
    dead         404/410          — gone; needs review/removal
    unreachable  000 / timeout / 5xx — no response (often transient or a
                                     bot block; review but don't auto-delete)

Design choices:
  * "gated" is deliberately NOT treated as broken — most public API docs sit
    behind Cloudflare/bot walls that 403 a HEAD from CI. Only 404/410 are
    called dead.
  * Never deletes catalog entries. It only FLAGS, so a human (or a follow-up
    PR) decides. Transient 5xx/timeouts shouldn't nuke real services.
  * Exit code is 0 even when dead links exist (flagging is the job); pass
    --fail-on-dead to make CI red instead.

Usage:
    python scripts/check_catalog_links.py [--workers N] [--timeout S]
                                          [--data PATH] [--out PATH]
                                          [--fail-on-dead]
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

try:
    import requests
except ImportError:  # pragma: no cover - requests is installed in CI
    print("ERROR: this script needs `requests` (pip install requests)", file=sys.stderr)
    raise

UA = "Mozilla/5.0 (compatible; alpine-data catalog-linkcheck/1.0; +https://github.com/btabiado/alpine-data)"
GATED = {401, 402, 403, 429}
DEAD = {404, 410}


def classify(status: int | None) -> str:
    if status is None:
        return "unreachable"
    if status in DEAD:
        return "dead"
    if status in GATED:
        return "gated"
    if 200 <= status < 400:
        return "ok"
    if 500 <= status < 600:
        return "unreachable"
    # other 4xx (e.g. 400/405/406/451) — endpoint responded but oddly; treat
    # as gated (exists) rather than dead, to avoid false positives.
    return "gated"


def check(url: str, timeout: float) -> int | None:
    """Return final HTTP status, or None if no response.

    Uses GET (not HEAD): many servers mishandle HEAD — returning 404/405 or
    timing out where a GET succeeds — which would inflate the dead/unreachable
    counts with false positives. `stream=True` + immediate close fetches only
    the response headers, so the body is never downloaded.
    """
    headers = {"User-Agent": UA, "Accept": "*/*"}
    try:
        resp = requests.get(
            url, timeout=timeout, allow_redirects=True, headers=headers, stream=True,
        )
        code = resp.status_code
        resp.close()
        return code
    except requests.RequestException:
        return None


def load_urls(data_path: Path) -> dict[str, list[str]]:
    """Map each unique URL -> list of service names that use it."""
    data = json.loads(data_path.read_text())
    url_to_names: dict[str, list[str]] = defaultdict(list)
    for cat in data.get("categories", []):
        for e in cat.get("entries", []):
            u = (e.get("u") or "").strip()
            if u.startswith("http"):
                url_to_names[u].append(e.get("n", ""))
    return url_to_names


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="health/api_catalog.json")
    ap.add_argument("--out", default="health/catalog_health.json")
    ap.add_argument("--md", default="health/catalog_health.md")
    ap.add_argument("--workers", type=int, default=24)
    ap.add_argument("--timeout", type=float, default=10.0)
    ap.add_argument("--fail-on-dead", action="store_true")
    args = ap.parse_args()

    data_path = Path(args.data)
    url_to_names = load_urls(data_path)
    urls = sorted(url_to_names)
    print(f"Checking {len(urls)} unique URLs with {args.workers} workers…", flush=True)

    results: dict[str, int | None] = {}
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(check, u, args.timeout): u for u in urls}
        done = 0
        for fut in as_completed(futs):
            u = futs[fut]
            results[u] = fut.result()
            done += 1
            if done % 250 == 0:
                print(f"  …{done}/{len(urls)}", flush=True)

    # Retry pass: a "None" (timeout / connection reset) is often transient or a
    # momentary bot-throttle under concurrency, not a real outage — the count
    # swings run-to-run. Re-check those once, slower (lower concurrency, longer
    # timeout), before flagging them unreachable. Cuts false positives sharply.
    retry = [u for u in urls if results[u] is None]
    if retry:
        print(f"Retrying {len(retry)} unreachable (slower)…", flush=True)
        with ThreadPoolExecutor(max_workers=min(args.workers, 8)) as ex:
            futs = {ex.submit(check, u, args.timeout + 6): u for u in retry}
            for fut in as_completed(futs):
                results[futs[fut]] = fut.result()

    buckets: dict[str, list[dict]] = {"ok": [], "gated": [], "dead": [], "unreachable": []}
    for u in urls:
        code = results[u]
        rec = {"url": u, "code": code, "services": url_to_names[u][:5]}
        buckets[classify(code)].append(rec)

    counts = {k: len(v) for k, v in buckets.items()}
    # ISO-8601 UTC; runs in CI so wall-clock is fine here (unlike workflow scripts).
    checked_at = _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()

    out = {
        "checked_at": checked_at,
        "total_urls": len(urls),
        "counts": counts,
        # Only persist the problem lists (keeps the file small); ok/gated are summarized by count.
        "dead": sorted(buckets["dead"], key=lambda r: r["url"]),
        "unreachable": sorted(buckets["unreachable"], key=lambda r: r["url"]),
    }
    Path(args.out).write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n")

    # Human-readable summary for an issue body / PR comment.
    lines = [
        f"# Catalog link health — {checked_at}",
        "",
        f"- **Total URLs:** {len(urls)}",
        f"- ✅ ok: {counts['ok']}  ·  🔒 gated (exists, auth/bot-walled): {counts['gated']}"
        f"  ·  ❌ dead (404/410): {counts['dead']}  ·  ⚠️ unreachable (timeout/5xx): {counts['unreachable']}",
        "",
    ]
    if buckets["dead"]:
        lines.append("## ❌ Dead (404/410) — review/remove")
        for r in out["dead"]:
            lines.append(f"- `{r['code']}` {r['url']} — {', '.join(s for s in r['services'] if s) or '(unnamed)'}")
        lines.append("")
    if buckets["unreachable"]:
        lines.append("## ⚠️ Unreachable (timeout/5xx/000) — often transient or bot-blocked")
        for r in out["unreachable"][:100]:
            lines.append(f"- `{r['code']}` {r['url']} — {', '.join(s for s in r['services'] if s) or '(unnamed)'}")
        if len(out["unreachable"]) > 100:
            lines.append(f"- …and {len(out['unreachable']) - 100} more")
        lines.append("")
    Path(args.md).write_text("\n".join(lines))

    print(f"\nDone. ok={counts['ok']} gated={counts['gated']} dead={counts['dead']} unreachable={counts['unreachable']}")
    print(f"Wrote {args.out} and {args.md}")

    if args.fail_on_dead and counts["dead"] > 0:
        print(f"::error::{counts['dead']} dead links found", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
