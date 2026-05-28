"""Google Trends acceleration signal (cached-reader).

Google Trends is consumed by the LTHCS Adoption pillar as a
search-interest acceleration signal. Live pytrends queries are
rate-limited so aggressively that the daily pipeline cannot afford to
hit Google directly (we see 429s within minutes once you ramp past a
handful of tickers).

The signal is therefore split into two halves:

* **Weekly batch** — :mod:`scripts.lthcs_trends_weekly` pulls fresh
  trends data for every active universe ticker slowly (1 ticker per
  ~4 s, exponential backoff on 429), and writes an aggregated
  ``data/lthcs/trends/<YYYY-WW>.json`` snapshot to disk.
* **Daily read** — this module reads the most recent snapshot on disk
  and emits the per-ticker acceleration metrics the Adoption pillar
  consumes. Pure I/O against the local filesystem; no network calls.

Public API::

    get_trends_acceleration(ticker, cache_dir=None) -> dict | None
    get_universe_trends_acceleration(tickers, cache_dir=None) -> dict[str, dict]

Plus the ``TICKER_TO_TREND_TERM`` mapping that the weekly batch uses
to translate tickers into either a Google Trends topic ID (preferred,
disambiguates "Apple Inc" from the fruit) or a fallback
``"<TICKER> stock"`` query.
"""

from __future__ import annotations

import datetime as _dt
import json
import math
import os
from pathlib import Path
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default trends snapshot directory, overridable via ``LTHCS_DATA_DIR``.
_DEFAULT_DATA_ROOT = Path("data/lthcs")

# Regime classification boundaries on acceleration_4w_pct (percent).
REGIME_SURGING_PCT = 50.0
REGIME_ACCELERATING_PCT = 15.0
REGIME_FADING_PCT = -15.0
REGIME_COLLAPSING_PCT = -50.0

# Data is considered "stale" if the snapshot week is more than 3 ISO
# weeks behind the current ISO week.
STALE_AFTER_WEEKS = 3

# tanh-style compression scale for the signal score. A +/-30% 4-week
# acceleration maps to ~tanh(1) ≈ +/-0.76; a +/-60% maps to ~tanh(2)
# ≈ +/-0.96. This keeps the score in [-1, +1] while keeping mid-range
# values informative.
_SIGNAL_SCORE_SCALE_PCT = 30.0

# Lookback offsets (weeks) for the 4w and 12w deltas.
_LOOKBACK_4W = 4
_LOOKBACK_12W = 12

# Top-N megacaps where we maintain hand-curated Google Trends topic
# IDs. Everyone else falls back to ``"<TICKER> stock"``. The topic ID
# disambiguates "Apple" the brand/company from "apple" the fruit.
TICKER_TO_TREND_TERM: Dict[str, str] = {
    "AAPL": "/m/0k8z",        # Apple Inc.
    "MSFT": "/m/04sv4",       # Microsoft
    "NVDA": "/m/04rn9k",      # NVIDIA
    "GOOGL": "/m/045c7b",     # Google
    "GOOG": "/m/045c7b",      # Google (alt share class)
    "AMZN": "/m/0mgkg",       # Amazon
    "META": "/m/0hmyfsv",     # Meta Platforms
    "TSLA": "/m/0dr90d",      # Tesla, Inc.
    "BRK.B": "/m/01bm_",      # Berkshire Hathaway
    "AVGO": "/m/047_g0t",     # Broadcom
    "JPM": "/m/0g25kj",       # JPMorgan Chase
    "V": "/m/02jc0t",         # Visa Inc.
    "MA": "/m/02nf1f",        # Mastercard
    "JNJ": "/m/0g25b",        # Johnson & Johnson
    "WMT": "/m/0fkwzs",       # Walmart
    "PG": "/m/02k_4",         # Procter & Gamble
    "ORCL": "/m/05gnf",       # Oracle Corp.
    "NFLX": "/m/017rf_",      # Netflix
    "DIS": "/m/0dwj1",        # The Walt Disney Company
    "KO": "/m/01yx7f",        # The Coca-Cola Company
    "PEP": "/m/0jv01k",       # PepsiCo
    "MCD": "/m/07gyp7",       # McDonald's
    "NKE": "/m/01tjt2",       # Nike
    "INTC": "/m/03nfmq",      # Intel
    "AMD": "/m/0gjk1d",       # Advanced Micro Devices
    "CRM": "/m/0d07p2",       # Salesforce
    "ADBE": "/m/01dx0z",      # Adobe Inc.
    "BA": "/m/0xnzz",         # Boeing
    "GE": "/m/036wy",         # General Electric
    "F": "/m/02xry",          # Ford Motor Company (caution: "F" is too short solo)
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _data_root() -> Path:
    return Path(os.environ.get("LTHCS_DATA_DIR", _DEFAULT_DATA_ROOT))


def _trends_dir(cache_dir: Optional[Path] = None) -> Path:
    if cache_dir is not None:
        return Path(cache_dir)
    return _data_root() / "trends"


def _iso_week_str(d: Optional[_dt.date] = None) -> str:
    """Return ``YYYY-Www`` ISO-week string for a date (default today)."""
    d = d or _dt.date.today()
    iso = d.isocalendar()
    # date.isocalendar() returns a namedtuple in 3.9+; supports .year/.week.
    return f"{iso[0]:04d}-W{iso[1]:02d}"


def _parse_iso_week(week_str: str) -> Optional[_dt.date]:
    """Parse a ``YYYY-Www`` string into the Monday of that ISO week.

    Returns ``None`` if malformed.
    """
    if not week_str or not isinstance(week_str, str):
        return None
    try:
        year_part, week_part = week_str.split("-W", 1)
        year = int(year_part)
        week = int(week_part)
    except (ValueError, AttributeError):
        return None
    if not (1 <= week <= 53):
        return None
    try:
        return _dt.date.fromisocalendar(year, week, 1)
    except (ValueError, AttributeError):
        return None


def _weeks_between(older: _dt.date, newer: _dt.date) -> int:
    """Whole ISO-weeks between two dates (newer - older), floored at 0."""
    if newer <= older:
        return 0
    return (newer - older).days // 7


def resolve_search_term(ticker: str) -> str:
    """Return the Google Trends search term (topic ID or fallback query) for a ticker.

    Megacaps with hand-curated topic IDs use those; everyone else falls
    back to ``"<TICKER> stock"`` (which dodges most fruit/brand
    ambiguity while still picking up share-class chatter).
    """
    if not ticker:
        return ""
    norm = ticker.strip().upper()
    if not norm:
        return ""
    topic = TICKER_TO_TREND_TERM.get(norm)
    if topic:
        return topic
    return f"{norm} stock"


# ---------------------------------------------------------------------------
# Snapshot discovery
# ---------------------------------------------------------------------------


def _list_snapshot_files(directory: Path) -> List[Path]:
    """All ``YYYY-Www.json`` files in ``directory`` (returns [] if missing)."""
    if not directory.exists() or not directory.is_dir():
        return []
    out: List[Path] = []
    for p in directory.iterdir():
        if not p.is_file() or p.suffix != ".json":
            continue
        # Stem should look like "YYYY-Www".
        if _parse_iso_week(p.stem) is None:
            continue
        out.append(p)
    return out


def _most_recent_snapshot(directory: Path) -> Optional[Path]:
    """Pick the snapshot file with the latest ISO week, or None."""
    snaps = _list_snapshot_files(directory)
    if not snaps:
        return None
    # Sort by parsed Monday of the ISO week, descending.
    snaps.sort(key=lambda p: _parse_iso_week(p.stem) or _dt.date.min, reverse=True)
    return snaps[0]


def _load_snapshot(path: Path) -> Optional[Dict[str, Any]]:
    """Read a snapshot JSON file. Returns None on any error."""
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


# ---------------------------------------------------------------------------
# Acceleration math
# ---------------------------------------------------------------------------


def _classify_regime(acc_4w_pct: Optional[float]) -> str:
    """Bucket a 4-week % change into a regime label.

    Ordering of the comparisons defines the boundary semantics:
    >+50 surging, >+15 accelerating, [-15, +15] stable, <-15 fading,
    <-50 collapsing. Exactly +15 is the floor of "accelerating"; exactly
    -15 is the ceiling of "fading".
    """
    if acc_4w_pct is None or (isinstance(acc_4w_pct, float) and math.isnan(acc_4w_pct)):
        return "unknown"
    if acc_4w_pct > REGIME_SURGING_PCT:
        return "surging"
    if acc_4w_pct > REGIME_ACCELERATING_PCT:
        return "accelerating"
    if acc_4w_pct >= REGIME_FADING_PCT:
        return "stable"
    if acc_4w_pct > REGIME_COLLAPSING_PCT:
        return "fading"
    return "collapsing"


def _signal_score_from_acc(acc_4w_pct: Optional[float]) -> float:
    """Tanh-compress a 4-week % change into [-1, +1].

    ``acc_4w_pct=+30`` -> ~+0.76; ``+60`` -> ~+0.96. Saturates smoothly.
    Returns 0.0 if input is missing.
    """
    if acc_4w_pct is None or (isinstance(acc_4w_pct, float) and math.isnan(acc_4w_pct)):
        return 0.0
    try:
        return float(math.tanh(float(acc_4w_pct) / _SIGNAL_SCORE_SCALE_PCT))
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _pct_change(latest: float, past: float) -> Optional[float]:
    """Return ``(latest - past) / past * 100``, or None if past <= 0."""
    if past is None or past <= 0:
        return None
    try:
        return float((float(latest) - float(past)) / float(past)) * 100.0
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _compute_from_series(
    ticker: str,
    series: List[float],
    snapshot_week: str,
    as_of: str,
    data_quality_override: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Compute the acceleration dict from a weekly interest series.

    ``series`` is the trailing weekly Google Trends interest values
    (oldest first, most recent last). At least one trailing value is
    required.

    ``data_quality`` is "good" with 12+ weeks, "partial" with 4-11 weeks
    (4w change computable but not 12w), or "partial" with <4 weeks
    (neither computable, but we still return latest so callers can show
    something). The caller may override via ``data_quality_override``
    (used to mark snapshots > STALE_AFTER_WEEKS old as "stale").
    """
    if not series:
        return None

    # Clean: keep only numeric, non-NaN values, preserving order.
    cleaned: List[float] = []
    for v in series:
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if f != f:  # NaN
            continue
        cleaned.append(f)
    if not cleaned:
        return None

    latest = cleaned[-1]
    n = len(cleaned)

    val_4w_ago: Optional[float] = None
    val_12w_ago: Optional[float] = None
    if n > _LOOKBACK_4W:
        val_4w_ago = cleaned[-1 - _LOOKBACK_4W]
    if n > _LOOKBACK_12W:
        val_12w_ago = cleaned[-1 - _LOOKBACK_12W]

    acc_4w = _pct_change(latest, val_4w_ago) if val_4w_ago is not None else None
    acc_12w = _pct_change(latest, val_12w_ago) if val_12w_ago is not None else None

    if data_quality_override:
        data_quality = data_quality_override
    elif acc_4w is not None and acc_12w is not None:
        data_quality = "good"
    else:
        data_quality = "partial"

    return {
        "ticker": ticker,
        "as_of": as_of,
        "trend_week": snapshot_week,
        "search_interest_latest": _round_int(latest),
        "search_interest_4w_ago": _round_int(val_4w_ago) if val_4w_ago is not None else None,
        "search_interest_12w_ago": _round_int(val_12w_ago) if val_12w_ago is not None else None,
        "acceleration_4w_pct": _round_pct(acc_4w),
        "acceleration_12w_pct": _round_pct(acc_12w),
        "regime": _classify_regime(acc_4w),
        "signal_score": round(_signal_score_from_acc(acc_4w), 4),
        "data_quality": data_quality,
    }


def _round_int(v: float) -> int:
    return int(round(float(v)))


def _round_pct(v: Optional[float]) -> Optional[float]:
    if v is None:
        return None
    return round(float(v), 2)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _extract_series(blob: Any) -> Optional[List[float]]:
    """Pull a weekly interest series out of a per-ticker snapshot blob.

    Accepts two shapes:
    * raw ``list[number]`` — the series itself.
    * ``dict`` with a ``"series"`` key — preferred for forward-compat
      (room for metadata: term used, region, raw pytrends payload, etc.).
    Returns None if neither shape matches.
    """
    if isinstance(blob, list):
        return [v for v in blob]
    if isinstance(blob, dict):
        s = blob.get("series")
        if isinstance(s, list):
            return list(s)
    return None


def get_trends_acceleration(
    ticker: str,
    cache_dir: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    """Read cached trends data for ``ticker`` and compute its acceleration block.

    Locates the most recent ``data/lthcs/trends/<YYYY-Www>.json`` (or
    file inside ``cache_dir`` if supplied), extracts the per-ticker
    weekly interest series, and computes the dict described in the
    module docstring.

    Returns ``None`` if:
    * no snapshot files exist at all, OR
    * the latest snapshot is missing this ticker entirely.

    If the latest snapshot is older than :data:`STALE_AFTER_WEEKS` ISO
    weeks, the returned dict still contains the computed numbers but
    ``data_quality`` is set to ``"stale"``.
    """
    if not ticker:
        return None
    norm = ticker.strip().upper()
    if not norm:
        return None

    directory = _trends_dir(cache_dir)
    snapshot_path = _most_recent_snapshot(directory)
    if snapshot_path is None:
        return None

    snapshot = _load_snapshot(snapshot_path)
    if snapshot is None:
        return None

    tickers_map = snapshot.get("tickers") if isinstance(snapshot.get("tickers"), dict) else snapshot
    if not isinstance(tickers_map, dict):
        return None

    blob = tickers_map.get(norm)
    if blob is None:
        return None

    series = _extract_series(blob)
    if series is None or not series:
        return None

    snapshot_week = snapshot.get("week") if isinstance(snapshot.get("week"), str) else snapshot_path.stem
    snapshot_as_of = snapshot.get("as_of") if isinstance(snapshot.get("as_of"), str) else _dt.date.today().isoformat()

    # Stale check: if the snapshot week is more than STALE_AFTER_WEEKS
    # behind the current ISO week, mark as stale (but still return).
    quality_override: Optional[str] = None
    snap_monday = _parse_iso_week(snapshot_week)
    if snap_monday is not None:
        weeks_old = _weeks_between(snap_monday, _dt.date.today())
        if weeks_old > STALE_AFTER_WEEKS:
            quality_override = "stale"

    return _compute_from_series(
        ticker=norm,
        series=series,
        snapshot_week=snapshot_week,
        as_of=snapshot_as_of,
        data_quality_override=quality_override,
    )


def get_universe_trends_acceleration(
    tickers: List[str],
    cache_dir: Optional[Path] = None,
) -> Dict[str, Dict[str, Any]]:
    """Vectorized helper: returns ``{ticker: acceleration_dict}`` for tickers with cached data.

    Tickers missing from the snapshot are silently dropped from the
    output map (callers should treat absence as "no signal").
    """
    out: Dict[str, Dict[str, Any]] = {}
    if not tickers:
        return out
    # Load the snapshot once and reuse it across tickers — avoids
    # re-reading the JSON file 168 times.
    directory = _trends_dir(cache_dir)
    snapshot_path = _most_recent_snapshot(directory)
    if snapshot_path is None:
        return out
    snapshot = _load_snapshot(snapshot_path)
    if snapshot is None:
        return out
    tickers_map = snapshot.get("tickers") if isinstance(snapshot.get("tickers"), dict) else snapshot
    if not isinstance(tickers_map, dict):
        return out

    snapshot_week = snapshot.get("week") if isinstance(snapshot.get("week"), str) else snapshot_path.stem
    snapshot_as_of = snapshot.get("as_of") if isinstance(snapshot.get("as_of"), str) else _dt.date.today().isoformat()
    quality_override: Optional[str] = None
    snap_monday = _parse_iso_week(snapshot_week)
    if snap_monday is not None:
        weeks_old = _weeks_between(snap_monday, _dt.date.today())
        if weeks_old > STALE_AFTER_WEEKS:
            quality_override = "stale"

    for t in tickers:
        if not t:
            continue
        norm = t.strip().upper()
        blob = tickers_map.get(norm)
        if blob is None:
            continue
        series = _extract_series(blob)
        if not series:
            continue
        acc = _compute_from_series(
            ticker=norm,
            series=series,
            snapshot_week=snapshot_week,
            as_of=snapshot_as_of,
            data_quality_override=quality_override,
        )
        if acc is not None:
            out[norm] = acc
    return out


__all__ = [
    "TICKER_TO_TREND_TERM",
    "REGIME_SURGING_PCT",
    "REGIME_ACCELERATING_PCT",
    "REGIME_FADING_PCT",
    "REGIME_COLLAPSING_PCT",
    "STALE_AFTER_WEEKS",
    "resolve_search_term",
    "get_trends_acceleration",
    "get_universe_trends_acceleration",
]
