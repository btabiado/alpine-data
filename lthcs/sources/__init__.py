"""Data source clients for LTHCS.

Each module here wraps one free-tier upstream API. All requests go through
the shared file cache (`_cache.py`) and a per-source token-bucket rate
limiter (`_ratelimit.py`).
"""

from __future__ import annotations
