"""File-based response cache with per-key TTL.

Layout on disk:
    .cache/lthcs/<source>/<safe_key>.json

Each entry is a JSON envelope:
    {"fetched_at": <unix>, "ttl_seconds": <int>, "value": <payload>}

`value` is whatever JSON-serialisable thing the caller wants to store
(usually the raw API response body, parsed).

The cache is single-process safe (writes are atomic via rename) but does
not guard against concurrent writers across processes. V1 runs one
pipeline at a time on Bryan's laptop, so that's acceptable.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

DEFAULT_CACHE_ROOT = Path(".cache/lthcs")

_SAFE_KEY = re.compile(r"[^a-zA-Z0-9._-]+")


def _sanitize(name: str) -> str:
    """Make a string safe for use as a single path segment."""
    return _SAFE_KEY.sub("_", name).strip("._-") or "_"


def _hash_key(key: str) -> str:
    """Stable short hash for long / unsafe keys."""
    return hashlib.sha256(key.encode("utf-8"), usedforsecurity=False).hexdigest()[:16]


@dataclass(frozen=True)
class CacheHit:
    value: Any
    fetched_at: float
    age_seconds: float


class FileCache:
    """Per-source file cache keyed by an arbitrary string.

    Keys are sanitized + hashed before being used as filenames, so callers
    can use natural keys like ``"AAPL/balance_sheet/2026-Q1"``.
    """

    def __init__(self, source: str, root: Path | str = DEFAULT_CACHE_ROOT) -> None:
        self.source = _sanitize(source)
        self.root = Path(root) / self.source
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        # Combine a sanitized prefix with a stable hash so long/unsafe keys
        # still round-trip cleanly.
        prefix = _sanitize(key)[:48]
        return self.root / f"{prefix}.{_hash_key(key)}.json"

    def get(self, key: str, *, now: Optional[float] = None) -> Optional[CacheHit]:
        path = self._path(key)
        if not path.exists():
            return None
        try:
            with path.open("r", encoding="utf-8") as fh:
                env = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return None
        fetched_at = float(env.get("fetched_at", 0.0))
        ttl = float(env.get("ttl_seconds", 0.0))
        now = now if now is not None else time.time()
        age = now - fetched_at
        if ttl > 0 and age > ttl:
            return None
        return CacheHit(value=env.get("value"), fetched_at=fetched_at, age_seconds=age)

    def set(
        self,
        key: str,
        value: Any,
        ttl_seconds: float,
        *,
        now: Optional[float] = None,
    ) -> None:
        if ttl_seconds < 0:
            raise ValueError("ttl_seconds must be >= 0")
        path = self._path(key)
        envelope = {
            "fetched_at": float(now if now is not None else time.time()),
            "ttl_seconds": float(ttl_seconds),
            "value": value,
        }
        # Atomic write so a crash mid-write never leaves a partial JSON.
        fd, tmp_path = tempfile.mkstemp(
            prefix=".tmp-", suffix=".json", dir=str(self.root)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(envelope, fh)
            os.replace(tmp_path, path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def delete(self, key: str) -> bool:
        path = self._path(key)
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False

    def clear(self) -> int:
        count = 0
        for p in self.root.glob("*.json"):
            try:
                p.unlink()
                count += 1
            except FileNotFoundError:
                pass
        return count
