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
        self.history_dir: Path = self.data_root / "history" / "by_ticker"
        for d in (
            self.snapshots_dir,
            self.variable_detail_dir,
            self.narratives_dir,
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
