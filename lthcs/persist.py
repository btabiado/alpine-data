"""LTHCS V1 persistence layer.

Writes daily snapshot, variable_detail, narratives, and rolling per-ticker
history JSON files under ``data/lthcs/``, plus a ``snapshots/index.json``
that the dashboard reads to discover available dates.

All filesystem writes are atomic: the payload is serialised to a
``.tmp-*.json`` sibling file via :func:`tempfile.mkstemp` and then
``os.replace``-d into place, so a crash mid-write never leaves a
half-written JSON on disk. This mirrors the idiom used by
``lthcs.sources._cache.FileCache``.

The public surface is a single :class:`LthcsPersist` class plus the
:func:`get_default_data_root` helper. See ``PHASE_1_BUILD_SPEC.md``
sections 3 and 7 for the storage layout and snapshot row schema.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import date as _date_cls, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Module constants
# ---------------------------------------------------------------------------

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TICKER_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def get_default_data_root() -> Path:
    """Return the default ``data/lthcs`` directory inside the repo.

    Resolved relative to this file's location so callers don't depend on
    the process cwd. The result is ``<repo_root>/data/lthcs``.
    """
    # lthcs/persist.py -> parent = lthcs/ -> parent = repo root
    return Path(__file__).resolve().parent.parent / "data" / "lthcs"


def _validate_calc_date(calc_date: str) -> str:
    if not isinstance(calc_date, str) or not _DATE_RE.match(calc_date):
        raise ValueError(
            "calc_date must be a 'YYYY-MM-DD' string, got %r" % (calc_date,)
        )
    return calc_date


def _safe_ticker(ticker: str) -> str:
    """Sanitize a ticker symbol for use as a single path segment.

    Keeps the symbol human-readable (e.g. ``BRK.B`` stays ``BRK.B``).
    Anything outside ``[A-Za-z0-9._-]`` is collapsed to an underscore.
    """
    if not isinstance(ticker, str) or not ticker.strip():
        raise ValueError("ticker must be a non-empty string, got %r" % (ticker,))
    cleaned = _TICKER_SAFE_RE.sub("_", ticker).strip("._-")
    if not cleaned:
        raise ValueError("ticker %r reduces to an empty filename" % (ticker,))
    return cleaned


def _atomic_write_json(path: Path, payload: Any) -> None:
    """Serialise ``payload`` to ``path`` atomically.

    Uses ``tempfile.mkstemp`` in the destination directory so the rename
    is on the same filesystem, then ``os.replace`` to swap the file into
    place. UTF-8, indented for git-diff friendliness, with
    ``ensure_ascii=False`` so non-ASCII characters round-trip cleanly.
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


def _read_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# LthcsPersist
# ---------------------------------------------------------------------------

class LthcsPersist:
    """Filesystem layer for the LTHCS daily pipeline.

    All paths are computed relative to ``data_root``. The four required
    subdirectories (``snapshots/``, ``variable_detail/``, ``narratives/``,
    ``history/by_ticker/``) are created on construction if missing.
    """

    def __init__(self, data_root: Optional[Path] = None) -> None:
        self.data_root: Path = Path(data_root) if data_root is not None else get_default_data_root()
        self.snapshots_dir: Path = self.data_root / "snapshots"
        self.variable_detail_dir: Path = self.data_root / "variable_detail"
        self.narratives_dir: Path = self.data_root / "narratives"
        # Shadow directory for LLM narratives (Tier 5 #23). Sibling of
        # narratives/; populated only when LTHCS_LLM_NARRATIVES_ENABLED=1.
        self.narratives_llm_dir: Path = self.data_root / "narratives_llm"
        self.history_dir: Path = self.data_root / "history" / "by_ticker"
        for d in (
            self.snapshots_dir,
            self.variable_detail_dir,
            self.narratives_dir,
            self.narratives_llm_dir,
            self.history_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Snapshot
    # ------------------------------------------------------------------

    def snapshot_path(self, calc_date: str) -> Path:
        _validate_calc_date(calc_date)
        return self.snapshots_dir / ("%s.json" % calc_date)

    def snapshot_exists(self, calc_date: str) -> bool:
        return self.snapshot_path(calc_date).exists()

    def write_snapshot(
        self,
        calc_date: str,
        model_version: str,
        weights_profile_default: str,
        scores: List[Dict],
        *,
        overwrite: bool = False,
    ) -> Path:
        """Write ``snapshots/<calc_date>.json`` atomically.

        Raises ``FileExistsError`` if the file already exists and
        ``overwrite`` is False. ``scores`` is the list of dicts returned
        by :func:`lthcs.score.compute_lthcs_score`.
        """
        if not isinstance(scores, list) or not all(isinstance(r, dict) for r in scores):
            raise TypeError("scores must be a list of dicts")
        path = self.snapshot_path(calc_date)
        if path.exists() and not overwrite:
            raise FileExistsError(
                "snapshot for %s already exists at %s (pass overwrite=True to replace)"
                % (calc_date, path)
            )
        payload: Dict[str, Any] = {
            "calc_date": calc_date,
            "model_version": str(model_version),
            "weights_profile_default": str(weights_profile_default),
            "scores": scores,
        }
        _atomic_write_json(path, payload)
        return path

    def read_snapshot(self, calc_date: str) -> Dict:
        return _read_json(self.snapshot_path(calc_date))

    # ------------------------------------------------------------------
    # Variable detail
    # ------------------------------------------------------------------

    def variable_detail_path(self, calc_date: str) -> Path:
        _validate_calc_date(calc_date)
        return self.variable_detail_dir / ("%s.json" % calc_date)

    def write_variable_detail(
        self,
        calc_date: str,
        model_version: str,
        variables: List[Dict],
        *,
        overwrite: bool = False,
    ) -> Path:
        if not isinstance(variables, list) or not all(
            isinstance(v, dict) for v in variables
        ):
            raise TypeError("variables must be a list of dicts")
        path = self.variable_detail_path(calc_date)
        if path.exists() and not overwrite:
            raise FileExistsError(
                "variable_detail for %s already exists at %s" % (calc_date, path)
            )
        payload: Dict[str, Any] = {
            "calc_date": calc_date,
            "model_version": str(model_version),
            "variables": variables,
        }
        _atomic_write_json(path, payload)
        return path

    # ------------------------------------------------------------------
    # Narratives
    # ------------------------------------------------------------------

    def narratives_path(self, calc_date: str) -> Path:
        _validate_calc_date(calc_date)
        return self.narratives_dir / ("%s.json" % calc_date)

    def write_narratives(
        self,
        calc_date: str,
        model_version: str,
        narratives: List[Dict],
        *,
        overwrite: bool = False,
    ) -> Path:
        if not isinstance(narratives, list) or not all(
            isinstance(n, dict) for n in narratives
        ):
            raise TypeError("narratives must be a list of dicts")
        path = self.narratives_path(calc_date)
        if path.exists() and not overwrite:
            raise FileExistsError(
                "narratives for %s already exists at %s" % (calc_date, path)
            )
        payload: Dict[str, Any] = {
            "calc_date": calc_date,
            "model_version": str(model_version),
            "narratives": narratives,
        }
        _atomic_write_json(path, payload)
        return path

    # ------------------------------------------------------------------
    # Narratives -- LLM shadow (Tier 5 #23)
    # ------------------------------------------------------------------

    def narratives_llm_path(self, calc_date: str) -> Path:
        _validate_calc_date(calc_date)
        return self.narratives_llm_dir / ("%s.json" % calc_date)

    def write_narratives_llm(
        self,
        calc_date: str,
        model_version: str,
        narratives: List[Dict],
        *,
        meta: Optional[Dict[str, Any]] = None,
        overwrite: bool = False,
    ) -> Path:
        """Write the shadow LLM narratives file.

        Mirrors :meth:`write_narratives` shape so the UI can swap
        sources with a one-line branch. ``meta`` carries run-level
        telemetry (model, total_cost_usd, fallback_count, etc.) and is
        stamped into the payload under ``meta`` for ops visibility.
        ``narratives`` is a list of dicts in the four-section shape
        (``todays_take``, ``why_changed``, ``why_not_to_sell``,
        ``what_would_break``, ``confidence_level``) plus telemetry
        fields. Atomic write.
        """
        if not isinstance(narratives, list) or not all(
            isinstance(n, dict) for n in narratives
        ):
            raise TypeError("narratives must be a list of dicts")
        path = self.narratives_llm_path(calc_date)
        if path.exists() and not overwrite:
            raise FileExistsError(
                "narratives_llm for %s already exists at %s" % (calc_date, path)
            )
        payload: Dict[str, Any] = {
            "calc_date": calc_date,
            "model_version": str(model_version),
            "meta": dict(meta or {}),
            "narratives": narratives,
        }
        _atomic_write_json(path, payload)
        return path

    # ------------------------------------------------------------------
    # History
    # ------------------------------------------------------------------

    def history_path(self, ticker: str) -> Path:
        return self.history_dir / ("%s.json" % _safe_ticker(ticker))

    def read_history(self, ticker: str) -> Dict:
        """Return the parsed history JSON, or a fresh empty shell.

        Empty shell: ``{'ticker': ticker, 'model_version': '', 'history': []}``.
        """
        path = self.history_path(ticker)
        if not path.exists():
            return {"ticker": ticker, "model_version": "", "history": []}
        try:
            return _read_json(path)
        except (OSError, json.JSONDecodeError):
            # Corrupt file -> behave as empty so a re-run can recover.
            return {"ticker": ticker, "model_version": "", "history": []}

    def append_history_entry(
        self,
        ticker: str,
        date: str,
        score: float,
        band: str,
        model_version: str,
        *,
        max_entries: int = 365,
    ) -> Path:
        """Insert / replace ``date``'s row at the top of the ticker history.

        If a row with the same ``date`` already exists it is REPLACED (so
        ``--force`` re-runs are idempotent). The history is then sorted
        descending by date and truncated to ``max_entries``. Written
        atomically.
        """
        _validate_calc_date(date)
        if max_entries <= 0:
            raise ValueError("max_entries must be > 0")
        try:
            score_val = float(score)
        except (TypeError, ValueError):
            raise ValueError("score must be a float, got %r" % (score,))

        current = self.read_history(ticker)
        history: List[Dict[str, Any]] = list(current.get("history") or [])
        # Drop any existing row for this date so the new value wins.
        history = [row for row in history if row.get("date") != date]
        history.append(
            {
                "date": date,
                "score": score_val,
                "band": str(band),
            }
        )
        history.sort(key=lambda r: r.get("date") or "", reverse=True)
        if len(history) > max_entries:
            history = history[:max_entries]

        payload: Dict[str, Any] = {
            "ticker": ticker,
            "model_version": str(model_version),
            "history": history,
        }
        path = self.history_path(ticker)
        _atomic_write_json(path, payload)
        return path

    def rebuild_history_for_all_tickers(
        self,
        snapshot_rows: List[Dict],
        calc_date: str,
        model_version: str,
        *,
        max_entries: int = 365,
    ) -> int:
        """Append one history entry per row in ``snapshot_rows``.

        Returns the number of history files written. Rows missing a
        ``ticker`` key are skipped silently (defensive — the daily
        pipeline always sets it). Score defaults to 0.0 and band to
        ``'review'`` if either is missing/None, mirroring the safe
        fallbacks elsewhere in the codebase.
        """
        _validate_calc_date(calc_date)
        count = 0
        for row in snapshot_rows or []:
            if not isinstance(row, dict):
                continue
            ticker = row.get("ticker")
            if not isinstance(ticker, str) or not ticker.strip():
                continue
            score_val = row.get("lthcs_score")
            if score_val is None:
                score_val = 0.0
            band = row.get("band") or "review"
            self.append_history_entry(
                ticker=ticker,
                date=calc_date,
                score=float(score_val),
                band=str(band),
                model_version=model_version,
                max_entries=max_entries,
            )
            count += 1
        return count

    # ------------------------------------------------------------------
    # Catch-up / gap-fill
    # ------------------------------------------------------------------

    def fill_history_gaps(self, today: str, *, max_entries: int = 365) -> int:
        """Forward-fill missing days between each ticker's last entry and ``today``.

        For every per-ticker history file under ``history/by_ticker/``:
          * Read the file and find the most recent real entry (latest date).
          * If that date is already ``today - 1`` (or later), do nothing.
          * Otherwise, for each calendar day strictly between
            ``last_date + 1`` and ``today - 1`` inclusive, append a
            synthetic entry copying the most recent entry's score + band
            and marked ``synthetic: True``. ``today``'s own entry is NOT
            written here — the caller (Stage 8) writes that via
            :meth:`rebuild_history_for_all_tickers`.

        Idempotent: a ticker whose history already runs up to ``today - 1``
        (real or synthetic) gets no new writes. A ticker whose history is
        empty (never scored) is skipped — there's nothing to forward-fill
        from. Each ticker is written atomically so a crash mid-loop never
        leaves a half-rewritten file.

        Returns the total number of synthetic entries written across all
        tickers. Use the count to detect when catch-up was active (>0)
        vs. a quiet no-op (==0).
        """
        _validate_calc_date(today)
        try:
            today_dt = _date_cls.fromisoformat(today)
        except ValueError as exc:
            raise ValueError("today must parse as ISO date: %s" % exc) from exc

        total_synthetic = 0
        affected_tickers = 0

        if not self.history_dir.exists():
            return 0

        for entry in sorted(self.history_dir.iterdir()):
            if not entry.is_file():
                continue
            if entry.suffix != ".json":
                continue
            if entry.name.startswith(".tmp-"):
                continue

            try:
                payload = _read_json(entry)
            except (OSError, json.JSONDecodeError):
                # Corrupt file — let a real --force re-run rebuild it.
                continue

            history: List[Dict[str, Any]] = list(payload.get("history") or [])
            if not history:
                # Nothing to forward-fill from; first real entry will be
                # written by rebuild_history_for_all_tickers in Stage 8.
                continue

            # Latest entry (already kept sorted desc by append_history_entry).
            # Defensive re-sort in case a legacy file is ordered differently.
            history_sorted = sorted(
                history,
                key=lambda r: r.get("date") or "",
                reverse=True,
            )
            latest = history_sorted[0]
            latest_date_str = latest.get("date")
            if not isinstance(latest_date_str, str):
                continue
            try:
                latest_dt = _date_cls.fromisoformat(latest_date_str)
            except ValueError:
                continue

            # Only forward-fill strictly into the past relative to today.
            # If the latest entry is already today or later, the schedule
            # is up-to-date and nothing to do.
            gap_end = today_dt - timedelta(days=1)
            if latest_dt >= gap_end:
                continue

            last_score = latest.get("score")
            last_band = latest.get("band") or "review"

            existing_dates = {row.get("date") for row in history_sorted}
            new_entries: List[Dict[str, Any]] = []
            cursor = latest_dt + timedelta(days=1)
            while cursor <= gap_end:
                cursor_str = cursor.isoformat()
                # Idempotency: skip dates that already have an entry —
                # important so running --catch-up twice doesn't duplicate
                # synthetic rows.
                if cursor_str not in existing_dates:
                    new_entries.append(
                        {
                            "date": cursor_str,
                            "score": last_score,
                            "band": last_band,
                            "synthetic": True,
                        }
                    )
                cursor += timedelta(days=1)

            if not new_entries:
                continue

            merged = history_sorted + new_entries
            merged.sort(key=lambda r: r.get("date") or "", reverse=True)
            if len(merged) > max_entries:
                merged = merged[:max_entries]

            payload["history"] = merged
            _atomic_write_json(entry, payload)
            total_synthetic += len(new_entries)
            affected_tickers += 1

        if total_synthetic:
            print(
                "✓ Catch-up: filled %d synthetic entries across %d tickers"
                % (total_synthetic, affected_tickers)
            )
        return total_synthetic

    def clear_synthetic_entries(self, ticker: str) -> int:
        """Remove every entry marked ``synthetic: true`` from a ticker's history.

        Useful for testing / cleanup when you want to undo a catch-up
        backfill on a specific ticker. Returns the number of entries
        removed. A no-op for tickers with no synthetic entries (or no
        history file at all).
        """
        path = self.history_path(ticker)
        if not path.exists():
            return 0
        try:
            payload = _read_json(path)
        except (OSError, json.JSONDecodeError):
            return 0
        history: List[Dict[str, Any]] = list(payload.get("history") or [])
        kept = [row for row in history if not row.get("synthetic")]
        removed = len(history) - len(kept)
        if removed == 0:
            return 0
        payload["history"] = kept
        _atomic_write_json(path, payload)
        return removed

    # ------------------------------------------------------------------
    # Index
    # ------------------------------------------------------------------

    def index_path(self) -> Path:
        return self.snapshots_dir / "index.json"

    def list_snapshot_dates(self) -> List[str]:
        """All dates with a snapshot file on disk, sorted descending."""
        dates: List[str] = []
        if not self.snapshots_dir.exists():
            return dates
        for entry in self.snapshots_dir.iterdir():
            if not entry.is_file():
                continue
            if entry.suffix != ".json":
                continue
            stem = entry.stem
            if not _DATE_RE.match(stem):
                # Skip index.json and any stray .tmp-*.json files.
                continue
            dates.append(stem)
        dates.sort(reverse=True)
        return dates

    def rebuild_index(self, model_version: str) -> Path:
        """Rewrite ``snapshots/index.json`` from the snapshots directory.

        Scans for ``YYYY-MM-DD.json`` files (ignoring ``index.json`` and
        any ``.tmp-*.json`` left over from a crashed write), sorts the
        dates descending, and writes the canonical index payload.
        """
        dates = self.list_snapshot_dates()
        payload: Dict[str, Any] = {
            "model_version": str(model_version),
            "dates": dates,
            "latest": dates[0] if dates else None,
            "count": len(dates),
        }
        path = self.index_path()
        _atomic_write_json(path, payload)
        return path
