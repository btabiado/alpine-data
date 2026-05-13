"""
Tiny store for time-bounded share tokens.

Each token grants read-only access to the dashboard for a fixed window so the
owner can text someone a link like:

    https://<cloudflared-public-host>/share/<token>

After the expiry passes (or the owner explicitly revokes), the token is
rejected and the share URL stops working.

The store lives in `data/shares.json` (gitignored). It is intentionally tiny
and dependency-free: no DB, no Redis. Concurrent writes from the single Flask
process are protected by a module-level lock.

Shape:
    {
      "<token>": {
        "created_at": "2026-05-13T10:11:12+00:00",
        "expires_at": "2026-05-16T10:11:12+00:00",
        "label": "for J. via SMS",
        "created_by": "btabiado"
      },
      ...
    }
"""

from __future__ import annotations

import json
import secrets
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).resolve().parent / "data"
SHARES_PATH = DATA_DIR / "shares.json"

# Token length in URL-safe chars. 32 chars ≈ 192 bits of entropy — unguessable
# by brute force even from a public bucket of known share files.
TOKEN_BYTES = 24

_lock = threading.Lock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _load() -> dict[str, dict[str, Any]]:
    if not SHARES_PATH.exists():
        return {}
    try:
        raw = json.loads(SHARES_PATH.read_text() or "{}")
        return raw if isinstance(raw, dict) else {}
    except Exception:
        # Corrupt file — return empty rather than crash the server.
        return {}


def _save(data: dict[str, dict[str, Any]]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SHARES_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    tmp.replace(SHARES_PATH)


def _parse_iso(s: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def create(days: float = 3.0, label: str = "", created_by: str = "") -> dict[str, Any]:
    """Mint a fresh share token valid for `days` days from now."""
    if days <= 0:
        raise ValueError("days must be positive")
    now = _now()
    token = secrets.token_urlsafe(TOKEN_BYTES)
    entry = {
        "created_at": now.isoformat(timespec="seconds"),
        "expires_at": (now + timedelta(days=days)).isoformat(timespec="seconds"),
        "label": label,
        "created_by": created_by,
    }
    with _lock:
        data = _load()
        data[token] = entry
        _save(data)
    return {"token": token, **entry}


def get(token: str) -> dict[str, Any] | None:
    if not token:
        return None
    with _lock:
        return _load().get(token)


def is_valid(token: str) -> bool:
    """True iff the token exists and has not expired."""
    entry = get(token)
    if not entry:
        return False
    exp = _parse_iso(entry.get("expires_at", ""))
    if exp is None:
        return False
    return _now() < exp


def list_all(include_expired: bool = False) -> list[dict[str, Any]]:
    with _lock:
        data = _load()
    now = _now()
    out: list[dict[str, Any]] = []
    for token, entry in data.items():
        exp = _parse_iso(entry.get("expires_at", ""))
        is_expired = exp is None or now >= exp
        if is_expired and not include_expired:
            continue
        out.append({
            "token": token,
            "expired": is_expired,
            **entry,
        })
    # Newest expiry first
    out.sort(key=lambda r: r.get("expires_at", ""), reverse=True)
    return out


def revoke(token: str) -> bool:
    """Delete a share token. Returns True if it existed."""
    with _lock:
        data = _load()
        if token in data:
            del data[token]
            _save(data)
            return True
        return False


def prune_expired() -> int:
    """Drop all expired tokens. Returns count removed."""
    now = _now()
    with _lock:
        data = _load()
        before = len(data)
        kept: dict[str, dict[str, Any]] = {}
        for token, entry in data.items():
            exp = _parse_iso(entry.get("expires_at", ""))
            if exp is not None and now < exp:
                kept[token] = entry
        if len(kept) != before:
            _save(kept)
        return before - len(kept)
