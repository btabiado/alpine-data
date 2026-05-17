"""Data source clients for LTHCS.

Each module here wraps one free-tier upstream API. All requests go through
the shared file cache (`_cache.py`) and a per-source token-bucket rate
limiter (`_ratelimit.py`).

Public clients:
- yahoo        — daily prices, volatility, momentum (no API key)
- sec_edgar    — XBRL company facts, revenue history (User-Agent only)
- fred         — macro series: CPI, Fed Funds, 10Y, unemployment, retail sales
- eia          — WTI, Brent, gasoline
- alpha_vantage — news sentiment (25 req/day cap), daily-price fallback
"""

from __future__ import annotations

from lthcs.sources import (
    alpha_vantage,
    eia,
    fred,
    sec_edgar,
    yahoo,
)

__all__ = [
    "alpha_vantage",
    "eia",
    "fred",
    "sec_edgar",
    "yahoo",
]
