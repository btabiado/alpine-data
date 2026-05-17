"""Daily ticker rotation manager for the LTHCS Thesis pillar.

Alpha Vantage's ``NEWS_SENTIMENT`` endpoint is:

* gated to **25 requests/day** on the free tier we're running on, and
* AND-semantic across ``tickers=...`` — a multi-ticker call only returns
  articles mentioning *every* ticker, so batching is useless when we
  want per-ticker sentiment for a 74-ticker universe.

The workaround: spread one single-ticker call per ticker across a rolling
3-day window. This module is the bookkeeper. It tells the daily pipeline
*which* 25 tickers to score today (the 25 with the oldest
``last_scored`` date, never-scored first, alphabetic tiebreak), and it
records the success after each per-ticker AV call.

On-disk layout under ``data/lthcs/``::

    thesis_rotation.json        # global rotation state
    sentiment/<TICKER>.json     # per-ticker sentiment snapshot

Both writers use the ``tempfile.mkstemp`` + ``os.replace`` pattern from
``lthcs.sources._cache``/``lthcs.persist``, so a crash mid-write never
leaves a half-written JSON behind.

The Thesis pillar (sibling module ``lthcs.pillars.thesis``) reads the
per-ticker ``sentiment/<TICKER>.json`` files — this module never imports
the pillar so it can be tested in isolation.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def get_default_data_root() -> Path:
    """Return ``<repo_root>/data/lthcs``.

    Resolved from this file's location so callers don't depend on the
    process cwd. Mirrors ``lthcs.persist.get_default_data_root``.
    """
    # lthcs/sources/thesis_rotation.py -> sources -> lthcs -> repo root
    return Path(__file__).resolve().parent.parent.parent / "data" / "lthcs"


# ---------------------------------------------------------------------------
# Atomic JSON writer (same idiom as lthcs.persist / lthcs.sources._cache)
# ---------------------------------------------------------------------------

def _atomic_write_json(path: Path, payload: Any) -> None:
    """Serialise ``payload`` to ``path`` atomically.

    Uses ``tempfile.mkstemp`` in the destination directory (so the rename
    is on the same filesystem) and ``os.replace`` to swap the file into
    place. Pretty-printed with ``ensure_ascii=False`` so non-ASCII
    characters round-trip cleanly.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=".tmp-", suffix=".json", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=False)
            fh.write("\n")
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _today_iso() -> str:
    return _dt.date.today().isoformat()


def _parse_iso(date_str: Optional[str]) -> Optional[_dt.date]:
    """Best-effort parse of a ``YYYY-MM-DD`` string.

    Returns ``None`` for missing / unparseable input so this never raises
    in normal use (rotation files are user-editable on disk).
    """
    if not date_str or not isinstance(date_str, str):
        return None
    try:
        return _dt.date.fromisoformat(date_str)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# ThesisRotation
# ---------------------------------------------------------------------------

class ThesisRotation:
    """Manage per-day ticker selection + per-ticker sentiment storage.

    Layout under ``data_root``::

        thesis_rotation.json     - global rotation state
        sentiment/<TICKER>.json  - per-ticker sentiment snapshot

    All writes are atomic. See module docstring for the rationale.
    """

    DAILY_BUDGET: int = 25
    DEFAULT_STALENESS_DAYS: int = 3

    def __init__(
        self,
        data_root: Optional[Path] = None,
        model_version: str = "v1.0.0",
    ) -> None:
        self.data_root: Path = (
            Path(data_root) if data_root is not None else get_default_data_root()
        )
        self.model_version: str = str(model_version)
        self.sentiment_dir: Path = self.data_root / "sentiment"
        # Make sure the directory tree exists so writers never have to
        # think about it (mirrors LthcsPersist.__init__).
        self.sentiment_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------

    def state_path(self) -> Path:
        return self.data_root / "thesis_rotation.json"

    def sentiment_path(self, ticker: str) -> Path:
        if not isinstance(ticker, str) or not ticker.strip():
            raise ValueError("ticker must be a non-empty string, got %r" % (ticker,))
        return self.sentiment_dir / ("%s.json" % ticker)

    # ------------------------------------------------------------------
    # Rotation state I/O
    # ------------------------------------------------------------------

    def _empty_state(self) -> Dict[str, Any]:
        return {
            "model_version": self.model_version,
            "last_updated": None,
            "tickers": {},
        }

    def load_state(self) -> Dict[str, Any]:
        """Read ``thesis_rotation.json`` from disk.

        Returns a fresh empty state if the file doesn't exist *or* is
        malformed — the daily pipeline should be able to recover from a
        corrupt rotation file by simply re-scoring everyone.
        """
        path = self.state_path()
        if not path.exists():
            return self._empty_state()
        try:
            with path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return self._empty_state()
        if not isinstance(data, dict):
            return self._empty_state()
        # Defensive normalisation — make sure the shape matches even if
        # someone hand-edited the file.
        data.setdefault("model_version", self.model_version)
        data.setdefault("last_updated", None)
        tickers = data.get("tickers")
        if not isinstance(tickers, dict):
            data["tickers"] = {}
        return data

    def save_state(self, state: Dict[str, Any]) -> Path:
        """Atomically write the rotation state to disk."""
        path = self.state_path()
        _atomic_write_json(path, state)
        return path

    # ------------------------------------------------------------------
    # Selection
    # ------------------------------------------------------------------

    def select_tickers_for_today(
        self,
        universe_tickers: List[str],
        *,
        today: Optional[str] = None,
        budget: Optional[int] = None,
    ) -> List[str]:
        """Pick the next batch of tickers to score today.

        Priority order:

        1. Tickers with ``last_scored is None`` (never scored).
        2. Tickers with the oldest ``last_scored`` date.
        3. Already-scored-today tickers are excluded — don't burn quota.
        4. Alphabetic tiebreak.

        Returns up to ``budget`` tickers (default :pyattr:`DAILY_BUDGET`).
        The state file is **not** mutated; callers drive the per-ticker
        AV calls and then invoke :meth:`record_scored` for each success.
        """
        today_iso = today or _today_iso()
        cap = self.DAILY_BUDGET if budget is None else int(budget)
        if cap <= 0:
            return []

        state = self.load_state()
        ticker_state: Dict[str, Any] = state.get("tickers", {}) or {}

        # De-dup the universe while preserving the caller's input order
        # for determinism if they pass a list with duplicates.
        seen = set()
        deduped: List[str] = []
        for t in universe_tickers or []:
            if not isinstance(t, str) or not t.strip():
                continue
            if t in seen:
                continue
            seen.add(t)
            deduped.append(t)

        candidates = []  # (sort_key_tuple, ticker)
        for ticker in deduped:
            row = ticker_state.get(ticker) or {}
            last_scored = row.get("last_scored") if isinstance(row, dict) else None
            if last_scored == today_iso:
                # Already scored today — skip entirely so we don't burn
                # any of the day's 25-call budget on a redundant fetch.
                continue
            # Sort key: never-scored (None) first by treating it as the
            # empty string (sorts before any "YYYY-..." date). Then
            # alphabetic on ticker.
            if last_scored is None or not isinstance(last_scored, str):
                date_key = ""
            else:
                date_key = last_scored
            candidates.append(((date_key, ticker), ticker))

        candidates.sort(key=lambda x: x[0])
        return [t for _key, t in candidates[:cap]]

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def record_scored(
        self, ticker: str, *, today: Optional[str] = None
    ) -> None:
        """Mark ``ticker`` as scored today and persist.

        Idempotent — calling twice for the same ticker on the same day
        only bumps ``last_updated`` (which would happen anyway because
        the daily pipeline runs a fresh datestamp on every call).
        """
        if not isinstance(ticker, str) or not ticker.strip():
            raise ValueError("ticker must be a non-empty string, got %r" % (ticker,))
        today_iso = today or _today_iso()
        state = self.load_state()
        tickers = state.setdefault("tickers", {})
        if not isinstance(tickers, dict):
            tickers = {}
            state["tickers"] = tickers
        row = tickers.get(ticker)
        if not isinstance(row, dict):
            row = {}
        row["last_scored"] = today_iso
        tickers[ticker] = row
        state["last_updated"] = today_iso
        # Keep the model_version field in sync with this instance — useful
        # if we ever bump the version mid-rotation and need to discriminate
        # stale rows.
        state["model_version"] = self.model_version
        self.save_state(state)

    # ------------------------------------------------------------------
    # Per-ticker sentiment file I/O
    # ------------------------------------------------------------------

    def write_sentiment(
        self,
        ticker: str,
        article_count: int,
        mean_sentiment_score: Optional[float],
        mean_relevance_score: Optional[float],
        label_counts: Dict[str, int],
        *,
        today: Optional[str] = None,
    ) -> Path:
        """Write ``sentiment/<TICKER>.json`` atomically and return its path.

        The schema is the same shape the Thesis pillar consumes:

        .. code-block:: json

            {
              "ticker": "AAPL",
              "last_scored": "2026-05-16",
              "model_version": "v1.0.0",
              "article_count": 50,
              "mean_sentiment_score": 0.248,
              "mean_relevance_score": 0.42,
              "label_counts": {"Bearish": 1, ...}
            }
        """
        if not isinstance(ticker, str) or not ticker.strip():
            raise ValueError("ticker must be a non-empty string, got %r" % (ticker,))
        today_iso = today or _today_iso()
        payload: Dict[str, Any] = {
            "ticker": ticker,
            "last_scored": today_iso,
            "model_version": self.model_version,
            "article_count": int(article_count),
            "mean_sentiment_score": (
                float(mean_sentiment_score)
                if mean_sentiment_score is not None
                else None
            ),
            "mean_relevance_score": (
                float(mean_relevance_score)
                if mean_relevance_score is not None
                else None
            ),
            "label_counts": dict(label_counts or {}),
        }
        path = self.sentiment_path(ticker)
        _atomic_write_json(path, payload)
        return path

    def read_sentiment(self, ticker: str) -> Optional[Dict[str, Any]]:
        """Return parsed sentiment dict for ``ticker`` or ``None``.

        ``None`` is returned both when the file is missing and when it
        exists but is malformed — the daily pipeline treats both as
        "no data, please refetch" rather than crashing.
        """
        path = self.sentiment_path(ticker)
        if not path.exists():
            return None
        try:
            with path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        return data

    # ------------------------------------------------------------------
    # Staleness / coverage
    # ------------------------------------------------------------------

    def is_stale(
        self,
        sentiment_dict: Optional[Dict[str, Any]],
        *,
        today: Optional[str] = None,
        max_staleness_days: int = DEFAULT_STALENESS_DAYS,
    ) -> bool:
        """Return ``True`` if the sentiment record is older than the cap.

        Also returns ``True`` for ``None`` or for records without a
        parseable ``last_scored`` date — both cases mean "treat as
        missing, please refetch".
        """
        if sentiment_dict is None or not isinstance(sentiment_dict, dict):
            return True
        last = _parse_iso(sentiment_dict.get("last_scored"))
        if last is None:
            return True
        today_date = _parse_iso(today) or _dt.date.today()
        age_days = (today_date - last).days
        return age_days > int(max_staleness_days)

    def coverage_stats(
        self,
        universe_tickers: List[str],
        *,
        today: Optional[str] = None,
    ) -> Dict[str, int]:
        """Bucket the universe into never_scored / stale / fresh / scored_today.

        ``never_scored + stale + fresh + scored_today == total``. Useful
        for the daily pipeline's end-of-run report.
        """
        today_iso = today or _today_iso()
        # De-dup so the counts always add up to len(deduped).
        seen = set()
        deduped: List[str] = []
        for t in universe_tickers or []:
            if not isinstance(t, str) or not t.strip():
                continue
            if t in seen:
                continue
            seen.add(t)
            deduped.append(t)

        total = len(deduped)
        never_scored = 0
        stale = 0
        fresh = 0
        scored_today = 0
        for ticker in deduped:
            sent = self.read_sentiment(ticker)
            if sent is None:
                never_scored += 1
                continue
            last = sent.get("last_scored")
            if last == today_iso:
                scored_today += 1
                continue
            if self.is_stale(sent, today=today_iso):
                stale += 1
            else:
                fresh += 1
        return {
            "total": total,
            "never_scored": never_scored,
            "stale": stale,
            "fresh": fresh,
            "scored_today": scored_today,
        }
