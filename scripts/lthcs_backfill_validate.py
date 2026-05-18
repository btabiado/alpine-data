#!/usr/bin/env python3
"""LTHCS backfill validation runner (script wrapper).

Thin wrapper around :mod:`lthcs.backfill_validate`. All real logic lives
in the importable module so dashboards, the MCP server, and tests can use
it without falling back to ``importlib.util.spec_from_file_location``
(which trips a Python 3.9 dataclass-on-dynamic-module quirk).

Usage
-----
    python scripts/lthcs_backfill_validate.py \\
        [--start YYYY-MM-DD] [--end YYYY-MM-DD] \\
        [--data-root data/lthcs] [--repair] [--verbose]

Exit codes
----------
    0 - all checks pass (no warnings, no failures)
    1 - at least one warning, no failures
    2 - at least one hard failure (missing snapshot, NaN scores, etc.)
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure the repo root is importable so ``import lthcs.backfill_validate``
# works when the script is invoked directly from anywhere.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from lthcs.backfill_validate import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
