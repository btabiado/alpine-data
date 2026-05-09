"""Shared pytest fixtures for the BTC/ETH ETF dashboard tests."""
from __future__ import annotations

import sys
from pathlib import Path

# Make the project root importable so `import app`, `import server`, etc. work.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
