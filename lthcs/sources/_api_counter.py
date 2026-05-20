"""Opt-in API call counter for source clients.

Used by ``scripts/lthcs_universe_scaletest.py`` to instrument the daily
pipeline at S&P 500 scale. Counters are inert unless the environment
variable ``LTHCS_API_COUNTER`` is set to a truthy value, so production
behavior is unchanged.

Usage (in a source module):

    from lthcs.sources._api_counter import bump

    def _http_get(...):
        bump("finnhub", "ok")        # successful upstream hit
        ...
        bump("finnhub", "rate_limit") # HTTP 429

Then in the scaletest script:

    from lthcs.sources import _api_counter
    _api_counter.enable()
    ... run pipeline ...
    print(_api_counter.snapshot())

The counter intentionally uses a process-global ``dict`` with a lock;
the pipeline is single-process so this is fine. The snapshot is a deep
copy so callers can serialize without races.
"""

from __future__ import annotations

import copy
import os
import threading
from typing import Dict


_LOCK = threading.Lock()
_COUNTS: Dict[str, Dict[str, int]] = {}
_ENABLED = False


def _truthy(raw: str) -> bool:
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def is_enabled() -> bool:
    """Return True when the counter is active.

    Activation precedence:
    - Explicit ``enable()`` call (programmatic, used by the scaletest).
    - ``LTHCS_API_COUNTER`` env var set to a truthy value.

    All ``bump()`` calls are a single ``if`` followed by a return when
    the counter is off, so the runtime overhead in production is roughly
    one attribute lookup per call.
    """
    if _ENABLED:
        return True
    raw = os.environ.get("LTHCS_API_COUNTER", "")
    return bool(raw) and _truthy(raw)


def enable() -> None:
    """Programmatically activate the counter (used by the scaletest)."""
    global _ENABLED
    _ENABLED = True


def disable() -> None:
    """Programmatically deactivate the counter (used by tests)."""
    global _ENABLED
    _ENABLED = False


def reset() -> None:
    """Zero all counters. Safe to call when disabled."""
    with _LOCK:
        _COUNTS.clear()


def bump(source: str, bucket: str = "ok", n: int = 1) -> None:
    """Increment ``_COUNTS[source][bucket]`` by ``n`` when active.

    ``source`` is the upstream identifier (``finnhub``, ``yahoo``,
    ``sec_edgar``, ``alpha_vantage``, ``fred``, ``eia``, ...).

    ``bucket`` is one of:
        - ``ok``           - successful 2xx response
        - ``rate_limit``   - HTTP 429 / explicit quota error
        - ``error``        - any other non-2xx, network failure, parse error
        - ``cache_hit``    - served from local file cache (no upstream call)
    """
    if not is_enabled():
        return
    with _LOCK:
        slot = _COUNTS.setdefault(source, {})
        slot[bucket] = slot.get(bucket, 0) + int(n)


def snapshot() -> Dict[str, Dict[str, int]]:
    """Return a deep copy of the current counters.

    Safe to serialize to JSON.
    """
    with _LOCK:
        return copy.deepcopy(_COUNTS)


def totals() -> Dict[str, int]:
    """Return per-source totals across all buckets (ok+rate_limit+error)."""
    out: Dict[str, int] = {}
    with _LOCK:
        for source, buckets in _COUNTS.items():
            out[source] = sum(int(v) for v in buckets.values())
    return out
