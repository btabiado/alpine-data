#!/usr/bin/env python3
"""Turn data/health/status.json into GitHub Actions annotations.

Why this exists: /health/ has correctly reported per-feed freshness for a
while, but reading it requires someone to open the page and look. Nobody did,
so a frozen crypto feed sat on the front page for 16 days behind a green
build. This makes the same information show up on the workflow run itself,
where a failure is already being looked at.

Deliberately does NOT fail the deploy by default (see pages.yml: production at
`/` is the source of truth and /health/ is observability-only). `--strict`
exits non-zero for anything critical, so a scheduled watchdog job can use the
same logic to actually go red.

Only the headline feeds are alerted on; the LTHCS/nested inventory is hundreds
of files and annotating all of them is the same as annotating none.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
STATUS = REPO_ROOT / "data" / "health" / "status.json"

# Feeds worth interrupting someone over, by `name` in status.json.
ALERT_ON = {
    "market.json",
    "whale.json",
    "ai_curated.json",
    "real_estate.json",
    "insights_history.json",
    "btc_flows.csv",
    "eth_flows.csv",
    "equity_etf_flows.csv",
    "data-defi.json",
    "data-whale.json",
}


def annotate(level: str, name: str, row: dict) -> None:
    """Emit one GitHub Actions annotation (falls back to a plain line locally)."""
    msg = (
        f"{name}: {row.get('age_human', '?')} old "
        f"(status={row.get('status')}, threshold fresh<{row.get('fresh_h')}h "
        f"stale<{row.get('stale_h')}h)"
    )
    print(f"::{level} title=Stale data feed::{msg}")


def main() -> int:
    if not STATUS.exists():
        # The generator is continue-on-error upstream, so a missing file means
        # it did not run. Say so rather than reporting a clean bill of health.
        print(f"::warning title=Health status missing::{STATUS.relative_to(REPO_ROOT)} "
              "not found - build_health_status.py did not produce output")
        return 0

    try:
        payload = json.loads(STATUS.read_text())
    except (OSError, json.JSONDecodeError) as e:
        print(f"::warning title=Health status unreadable::{type(e).__name__}: {e}")
        return 0

    rows = payload.get("rendered") or []
    critical: list[str] = []
    stale: list[str] = []

    for row in rows:
        if not isinstance(row, dict):
            continue
        name = row.get("name")
        if name not in ALERT_ON:
            continue
        status = row.get("status")
        if status == "critical":
            annotate("error", name, row)
            critical.append(name)
        elif status == "stale":
            annotate("warning", name, row)
            stale.append(name)

    checked = sum(1 for r in rows if isinstance(r, dict) and r.get("name") in ALERT_ON)
    print(f"[feed-alert] checked {checked} headline feed(s): "
          f"{len(critical)} critical, {len(stale)} stale")

    if critical and "--strict" in sys.argv:
        print(f"[feed-alert] --strict: failing on {', '.join(sorted(critical))}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
