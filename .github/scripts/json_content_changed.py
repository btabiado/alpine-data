#!/usr/bin/env python3
"""Did a JSON file's content change against HEAD, ignoring run clocks?

    python .github/scripts/json_content_changed.py PATH --ignore-key generated_at \
        --ignore-key fetched_at

Bot workflows rewrite their JSON every run and stamp it with the time of the
run. When the upstream has not moved, the only difference from the committed
copy is those stamps, and committing it adds a commit that changes nothing
else (real-estate-daily: 27 of 30 daily commits in September 2026). This
compares the working-tree file with HEAD's after dropping the named keys at
every depth.

Exit status:
  0  the content changed, or the file is new, deleted, or not valid JSON:
     commit it
  1  only the ignored keys differ (or nothing at all): skip the commit
  2  usage error

Anything it cannot judge counts as changed, so a mistake here can only cost a
redundant commit, never a lost one. Only HEAD is read, so a depth-1 checkout
is enough.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def strip_keys(obj, keys: frozenset):
    """`obj` with every dict entry whose key is in `keys` removed, at any depth."""
    if isinstance(obj, dict):
        return {k: strip_keys(v, keys) for k, v in obj.items() if k not in keys}
    if isinstance(obj, list):
        return [strip_keys(v, keys) for v in obj]
    return obj


def _committed_bytes(path: str) -> "bytes | None":
    # `HEAD:./<path>` resolves relative to the current directory.
    out = subprocess.run(["git", "show", f"HEAD:./{path}"], capture_output=True)
    return out.stdout if out.returncode == 0 else None


def content_changed(path: str, ignore_keys) -> bool:
    keys = frozenset(ignore_keys)
    committed = _committed_bytes(path)
    p = Path(path)
    if committed is None or not p.is_file():
        return True                      # new or deleted file
    current = p.read_bytes()
    if current == committed:
        return False
    try:
        old = json.loads(committed)
        new = json.loads(current)
    except ValueError:
        return True                      # not JSON: let the commit decide
    return strip_keys(old, keys) != strip_keys(new, keys)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("path", help="repo-relative path of the JSON file")
    ap.add_argument("--ignore-key", action="append", default=[], dest="keys",
                    metavar="KEY", help="key to ignore at any depth (repeatable)")
    args = ap.parse_args(argv)
    if content_changed(args.path, args.keys):
        return 0
    print(f"{args.path}: only {', '.join(args.keys) or 'nothing'} changed")
    return 1


if __name__ == "__main__":
    sys.exit(main())
