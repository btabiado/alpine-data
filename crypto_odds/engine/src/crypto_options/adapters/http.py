"""Rate-limited, bounded-retry JSON client that archives every raw response immutably."""
from __future__ import annotations

import gzip
import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import requests


@dataclass
class FetchResult:
    ok: bool
    data: object = None
    error: str | None = None
    retrieved_at: datetime | None = None
    raw_path: str | None = None
    sha256: str | None = None


@dataclass
class RawArchive:
    """Write-once store: one file per response, named by content hash, never overwritten."""

    root: Path
    records: list = field(default_factory=list)

    def save(self, venue: str, endpoint: str, params: dict, body: bytes, retrieved_at: datetime) -> tuple[str, str]:
        digest = hashlib.sha256(body).hexdigest()
        day = retrieved_at.strftime("%Y-%m-%d")
        stamp = retrieved_at.strftime("%Y%m%dT%H%M%S%fZ")
        slug = endpoint.strip("/").replace("/", "_")
        tag = "_".join(f"{k}-{v}" for k, v in sorted(params.items()))
        path = self.root / venue / day / f"{stamp}_{slug}_{tag}_{digest[:12]}.json.gz"
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():  # sha256 is of the original (uncompressed) response bytes
            path.write_bytes(gzip.compress(body, mtime=0))
        self.records.append(
            {"venue": venue, "endpoint": endpoint, "params": params, "path": str(path),
             "sha256": digest, "retrieved_at": retrieved_at.isoformat()}
        )
        return str(path), digest


class JsonClient:
    def __init__(self, base_url: str, archive: RawArchive | None, venue: str,
                 min_interval_s: float = 0.35, max_retries: int = 3, backoff_s: float = 2.0,
                 timeout_s: float = 20.0, session: requests.Session | None = None):
        self.base_url = base_url.rstrip("/")
        self.archive = archive
        self.venue = venue
        self.min_interval_s = min_interval_s
        self.max_retries = max_retries
        self.backoff_s = backoff_s
        self.timeout_s = timeout_s
        self.session = session or requests.Session()
        self._last_call = 0.0
        self.host_down = False
        self.n_calls = 0

    def get(self, endpoint: str, params: dict | None = None) -> FetchResult:
        params = params or {}
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        last_error = None
        if self.host_down:
            return FetchResult(False, error="host unreachable earlier in this run (DNS); skipped",
                               retrieved_at=datetime.now(timezone.utc))
        for attempt in range(self.max_retries):
            wait = self.min_interval_s - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()
            self.n_calls += 1
            try:
                resp = self.session.get(url, params=params, timeout=(5, self.timeout_s))
                retrieved_at = datetime.now(timezone.utc)
                if resp.status_code == 429 or resp.status_code >= 500:
                    last_error = f"HTTP {resp.status_code}"
                    time.sleep(self.backoff_s * (2 ** attempt))
                    continue
                if resp.status_code != 200:
                    return FetchResult(False, error=f"HTTP {resp.status_code}: {resp.text[:200]}",
                                       retrieved_at=retrieved_at)
                body = resp.content
                data = json.loads(body)
                raw_path = digest = None
                if self.archive is not None:
                    raw_path, digest = self.archive.save(self.venue, endpoint, params, body, retrieved_at)
                return FetchResult(True, data=data, retrieved_at=retrieved_at, raw_path=raw_path, sha256=digest)
            except (requests.RequestException, ValueError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if "NameResolution" in str(exc) or "nodename nor servname" in str(exc):
                    self.host_down = True
                    break  # DNS failure: retrying the same name immediately does not help
                time.sleep(self.backoff_s * (2 ** attempt))
        return FetchResult(False, error=f"gave up after {self.max_retries} attempts: {last_error}",
                           retrieved_at=datetime.now(timezone.utc))
