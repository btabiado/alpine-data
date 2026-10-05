#!/usr/bin/env python3
"""Prune .cache/lthcs down to the entries that are safe to carry between runs.

The daily workflow persists the response cache with actions/cache so the
~520-ticker run does not re-download immutable SEC documents every night.
Only long-lived entries are kept: anything whose own TTL is longer than
``--min-ttl-days`` (default 7) and that has not expired yet. In practice that
is SEC 13F filing extracts / quarterly indexes (365d TTL) and SEC Form 4
filing bodies (30d TTL) — documents that never change once filed.

Everything else is deleted before the cache is saved: prices, news,
recommendations, SEC submissions feeds and companyfacts (24h–7d TTLs). A
persisted copy of those could be served on the next run while still inside
its TTL, i.e. data up to a day old presented as today's. Files that are not
FileCache envelopes (progress files, ticker maps) are deleted too.

Usage::

    python scripts/lthcs_prune_cache.py [--root .cache/lthcs] [--min-ttl-days 7]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Optional, Tuple


def keep_entry(path: Path, *, min_ttl_s: float, now: float) -> bool:
    try:
        with path.open("r", encoding="utf-8") as fh:
            env = json.load(fh)
    except (OSError, ValueError):
        return False
    if not isinstance(env, dict) or "ttl_seconds" not in env or "fetched_at" not in env:
        return False
    try:
        ttl = float(env["ttl_seconds"])
        fetched = float(env["fetched_at"])
    except (TypeError, ValueError):
        return False
    if ttl <= min_ttl_s:
        return False
    return (now - fetched) <= ttl


def prune(root: Path, *, min_ttl_days: float = 7.0, now: Optional[float] = None) -> Tuple[int, int]:
    """Delete non-persistable files under ``root``. Returns (kept, deleted)."""
    if not root.is_dir():
        return 0, 0
    now = time.time() if now is None else now
    min_ttl_s = min_ttl_days * 86400.0
    kept = deleted = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix == ".json" and keep_entry(path, min_ttl_s=min_ttl_s, now=now):
            kept += 1
            continue
        try:
            path.unlink()
            deleted += 1
        except OSError:
            pass
    # Drop directories left empty.
    for d in sorted((p for p in root.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        try:
            d.rmdir()
        except OSError:
            pass
    return kept, deleted


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", type=Path, default=Path(".cache/lthcs"))
    ap.add_argument("--min-ttl-days", type=float, default=7.0)
    args = ap.parse_args(argv)
    kept, deleted = prune(args.root, min_ttl_days=args.min_ttl_days)
    print("[lthcs-prune-cache] kept %d long-lived entries, deleted %d" % (kept, deleted))
    return 0


if __name__ == "__main__":
    sys.exit(main())
