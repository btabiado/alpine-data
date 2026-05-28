#!/usr/bin/env python3
"""Structural validator for a built dashboard.html — V1 or V2.

Thin wrapper around tools/validate_v2_dashboard.py so the same JS structural
checks (duplicate top-level consts, V2.empty take-both conflict pattern,
git conflict markers, leftover __DATA_JSON__ placeholder, brace/paren
balance, state-keys-unique, SIDECARS↔SIDECAR_FOR_TAB coverage) can be run
against either build:

    python3 tools/validate_dashboard.py v2/dashboard.html   # V2 (was the original target)
    python3 tools/validate_dashboard.py dashboard.html      # V1
    python3 tools/validate_dashboard.py                     # defaults to v2/dashboard.html

The original `validate_v2_dashboard.py` is kept as-is so existing
pages.yml callers and any in-flight parallel work that touches it keep
working. This entrypoint just forwards into the same `main(argv)`.

Why a wrapper instead of a rename: a parallel agent (`fix/v8-in-ci-guard`)
is also editing validate_v2_dashboard.py. Renaming would compound the
merge conflict for no functional gain — the wrapper here is one import +
one call and adds the V1-friendly entrypoint name pages.yml will use.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Import sibling module by path so this script works whether it's invoked as
# `python tools/validate_dashboard.py` (script-mode, sys.path has tools/) or
# from a different cwd. Same-directory import is the simple case.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from validate_v2_dashboard import main as _main  # noqa: E402


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
