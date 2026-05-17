"""Analyst RATING breadth — derived signal over a rolling window.

This module is a **derived** signal: it re-aggregates the analyst
upgrade/downgrade history that :mod:`lthcs.sources.yahoo_events` already
pulls into rolling per-ticker breadth scores over a configurable window
(30d default, 90d available).

NOT to be confused with "earnings estimate revisions breadth" — we do
NOT have forward-EPS estimate revisions data (that would require a paid
feed such as Refinitiv/FactSet). The signal computed here is the count
and direction of analyst RATING actions only.

Source data: the cached output of
``yahoo_events.get_analyst_actions(ticker, days=90)`` — a list of dicts
with keys ``ticker, date, firm, action, from_grade, to_grade, direction``.
This module is **CACHE-READ-ONLY**: it never calls yfinance. The daily
pipeline calls yahoo_events first to populate the cache.

Public API:

    * ``compute_analyst_breadth(ticker, cache_dir=None, window_days=30)``
    * ``compute_universe_breadth(tickers, cache_dir=None, window_days=30)``

Both return dicts (or ``None`` when no cached actions exist for the
ticker). The shape includes ``upgrades``, ``downgrades``,
``initiations_{bullish,bearish}``, ``reiterations_{bullish,bearish}``,
``net_actions`` (weighted sum), ``breadth_score`` (normalised to
[-1, +1]), ``regime`` (5-level label), ``firm_count``, and the
``raw_actions`` that fell inside the window — for downstream
``variable_detail`` transparency.

No mutation of yahoo_events' cache files is performed. If a needed
classification field is missing from a cached action (e.g. a bare
"Initiated" with no ``to_grade``), it falls into the neutral bucket
rather than guessing.
"""

from __future__ import annotations

import datetime as _dt
import math
from pathlib import Path
from typing import Any, Dict, List, Optional

from lthcs.sources._cache import FileCache

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# yahoo_events stores its analyst-actions cache under this source name.
# Must match ``yahoo_events._RECO_CACHE = FileCache("yahoo_reco")``.
_YAHOO_RECO_SOURCE = "yahoo_reco"

# The daily pipeline calls ``get_analyst_actions(sym, days=90)``. Cache
# keys are formatted as ``{TICKER}/analyst_actions/{days}``.
_DEFAULT_PIPELINE_DAYS = 90

# Action-verb classification. yfinance's ``Action`` column is short
# codes ("up", "down", "init", "main", "reit") on the modern path, but
# older yfinance + our own normalised cache often stores verbose forms
# ("Upgrades", "Downgrades", "Initiates", "Maintains", "Reiterates").
# Match case-insensitively on the stem so both shapes work.
_UPGRADE_STEMS = ("up",)              # "up", "upgrade", "upgrades"
_DOWNGRADE_STEMS = ("down",)          # "down", "downgrade", "downgrades"
_INIT_STEMS = ("init",)               # "init", "initiate", "initiates", "initiation"
_REIT_STEMS = ("reit", "main", "reinstate", "resume")

# Bullish / bearish to_grade classifications. Case-insensitive; matched
# against the normalised lowercase grade string. Anything that maps to
# neither set is treated as neutral (Hold / Neutral / Equal-Weight /
# Market Perform fall here naturally; an unknown grade also lands here).
_BULLISH_GRADES = frozenset(
    {
        "buy",
        "strong buy",
        "outperform",
        "overweight",
        "positive",
        "accumulate",
        "add",
        "conviction buy",
        "top pick",
    }
)
_BEARISH_GRADES = frozenset(
    {
        "sell",
        "strong sell",
        "underperform",
        "underweight",
        "negative",
        "reduce",
    }
)

# Direction weights applied to ``net_actions``.
_W_UPGRADE = 1.0
_W_DOWNGRADE = -1.0
_W_INIT_BULL = 0.5
_W_INIT_BEAR = -0.5
_W_REIT_BULL = 0.3
_W_REIT_BEAR = -0.3

# ``breadth_score`` normalisation: net_actions is divided by a soft
# saturation factor so a ticker with a *single* action doesn't pin at
# +/-1.0. Empirically, 4-6 weighted units of net action represents a
# strong consensus shift on a 30-day window; we saturate at 8 to leave
# headroom for the strongly_* regime band.
_SCORE_SATURATION = 8.0

# Regime thresholds on breadth_score.
_REGIME_STRONG_UP = 0.6
_REGIME_UP = 0.2
_REGIME_DOWN = -0.2
_REGIME_STRONG_DOWN = -0.6


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _today_iso() -> str:
    return _dt.date.today().isoformat()


def _today_date() -> _dt.date:
    return _dt.date.today()


def _normalise_grade(raw: Any) -> Optional[str]:
    """Lowercase, strip a grade string. None / empty / NaN -> None."""
    if raw is None:
        return None
    if isinstance(raw, float) and math.isnan(raw):
        return None
    s = str(raw).strip().lower()
    if not s or s == "nan":
        return None
    return s


def _classify_grade(grade: Optional[str]) -> str:
    """Return ``"bullish"``, ``"bearish"``, or ``"neutral"``.

    Unknown / missing grades are neutral — we deliberately do NOT
    interpolate a direction from a free-form string we don't recognise.
    """
    if grade is None:
        return "neutral"
    if grade in _BULLISH_GRADES:
        return "bullish"
    if grade in _BEARISH_GRADES:
        return "bearish"
    # Loose suffix match for compound grades like "buy / outperform" or
    # "market outperform" — pick the first match found in the string.
    for needle in _BULLISH_GRADES:
        if needle in grade:
            return "bullish"
    for needle in _BEARISH_GRADES:
        if needle in grade:
            return "bearish"
    return "neutral"


def _match_stem(action: Optional[str], stems: tuple) -> bool:
    if not action:
        return False
    a = str(action).strip().lower()
    for stem in stems:
        if stem in a:
            return True
    return False


def _classify_action(action_text: Optional[str], to_grade: Optional[str]) -> str:
    """Return one of:

        ``upgrade``, ``downgrade``,
        ``init_bull``, ``init_bear``, ``init_neutral``,
        ``reit_bull``, ``reit_bear``, ``reit_neutral``,
        ``other``
    """
    # Order matters: upgrade/downgrade are unambiguous and beat any
    # downstream grade interpretation.
    if _match_stem(action_text, _UPGRADE_STEMS):
        return "upgrade"
    if _match_stem(action_text, _DOWNGRADE_STEMS):
        return "downgrade"

    grade_norm = _normalise_grade(to_grade)
    grade_cls = _classify_grade(grade_norm)

    if _match_stem(action_text, _INIT_STEMS):
        if grade_cls == "bullish":
            return "init_bull"
        if grade_cls == "bearish":
            return "init_bear"
        return "init_neutral"

    if _match_stem(action_text, _REIT_STEMS):
        if grade_cls == "bullish":
            return "reit_bull"
        if grade_cls == "bearish":
            return "reit_bear"
        return "reit_neutral"

    return "other"


def _parse_iso_date(raw: Any) -> Optional[_dt.date]:
    if not raw:
        return None
    try:
        return _dt.date.fromisoformat(str(raw)[:10])
    except (ValueError, TypeError):
        return None


def _regime_for(score: float) -> str:
    if score > _REGIME_STRONG_UP:
        return "strongly_improving"
    if score >= _REGIME_UP:
        return "improving"
    if score > _REGIME_DOWN:
        return "stable"
    if score > _REGIME_STRONG_DOWN:
        return "deteriorating"
    return "strongly_deteriorating"


# ---------------------------------------------------------------------------
# Cache access
# ---------------------------------------------------------------------------


def _load_cached_actions(
    ticker: str,
    cache_dir: Optional[Path],
    pipeline_days: int = _DEFAULT_PIPELINE_DAYS,
) -> Optional[List[Dict[str, Any]]]:
    """Read the yahoo_events analyst-actions cache for ``ticker``.

    Returns the list of action dicts if a fresh cache entry exists; an
    empty list if the cached value is explicitly empty; or ``None`` if
    no cache file exists (the pipeline has never seen this ticker, or
    the entry expired).

    NEVER calls yfinance. NEVER writes. Only reads from disk.
    """
    if not ticker or not str(ticker).strip():
        return None
    root = Path(cache_dir) if cache_dir is not None else None
    if root is not None:
        cache = FileCache(_YAHOO_RECO_SOURCE, root=root)
    else:
        cache = FileCache(_YAHOO_RECO_SOURCE)
    key = f"{ticker.upper()}/analyst_actions/{int(pipeline_days)}"
    hit = cache.get(key)
    if hit is None:
        return None
    value = hit.value
    if value is None:
        return []
    if not isinstance(value, list):
        return []
    return [dict(row) for row in value if isinstance(row, dict)]


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def _aggregate_actions(
    actions: List[Dict[str, Any]],
    window_days: int,
    today: Optional[_dt.date] = None,
) -> Dict[str, Any]:
    """Bucket actions by classification and compute the breadth score.

    Returns a dict with the per-bucket counts, ``net_actions``,
    ``breadth_score``, ``regime``, ``firm_count``, and ``raw_actions``
    list (the subset that fell inside the window, newest-first).
    """
    today = today or _today_date()
    cutoff = today - _dt.timedelta(days=max(int(window_days), 0))

    upgrades = 0
    downgrades = 0
    init_bull = 0
    init_bear = 0
    reit_bull = 0
    reit_bear = 0
    other = 0
    firms: set = set()
    raw_window: List[Dict[str, Any]] = []

    for row in actions or []:
        if not isinstance(row, dict):
            continue
        d = _parse_iso_date(row.get("date"))
        if d is None:
            continue
        if d < cutoff:
            continue
        # Inclusive of the cutoff date itself (==).

        action_text = row.get("action")
        to_grade = row.get("to_grade")
        cls = _classify_action(action_text, to_grade)

        if cls == "upgrade":
            upgrades += 1
        elif cls == "downgrade":
            downgrades += 1
        elif cls == "init_bull":
            init_bull += 1
        elif cls == "init_bear":
            init_bear += 1
        elif cls == "reit_bull":
            reit_bull += 1
        elif cls == "reit_bear":
            reit_bear += 1
        else:
            other += 1

        firm = row.get("firm") or ""
        if firm:
            firms.add(str(firm))

        raw_window.append(
            {
                "date": d.isoformat(),
                "firm": str(firm) if firm else "",
                "action": str(action_text) if action_text else "",
                "to_grade": str(to_grade) if to_grade else None,
                "from_grade": (
                    str(row.get("from_grade"))
                    if row.get("from_grade")
                    else None
                ),
                "classification": cls,
            }
        )

    raw_window.sort(key=lambda r: r["date"], reverse=True)

    net_actions = (
        _W_UPGRADE * upgrades
        + _W_DOWNGRADE * downgrades
        + _W_INIT_BULL * init_bull
        + _W_INIT_BEAR * init_bear
        + _W_REIT_BULL * reit_bull
        + _W_REIT_BEAR * reit_bear
    )
    # Round to 2dp so the dict survives JSON round-trip cleanly.
    net_actions_rounded = round(net_actions, 4)

    if _SCORE_SATURATION > 0:
        raw_score = net_actions / _SCORE_SATURATION
    else:
        raw_score = 0.0
    if raw_score > 1.0:
        raw_score = 1.0
    elif raw_score < -1.0:
        raw_score = -1.0
    breadth_score = round(raw_score, 4)
    regime = _regime_for(breadth_score)

    return {
        "upgrades": upgrades,
        "downgrades": downgrades,
        "initiations_bullish": init_bull,
        "initiations_bearish": init_bear,
        "reiterations_bullish": reit_bull,
        "reiterations_bearish": reit_bear,
        "net_actions": net_actions_rounded,
        "breadth_score": breadth_score,
        "regime": regime,
        "firm_count": len(firms),
        "raw_actions": raw_window,
        "other_count": other,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compute_analyst_breadth(
    ticker: str,
    cache_dir: Optional[Path] = None,
    window_days: int = 30,
) -> Optional[Dict[str, Any]]:
    """Compute rolling analyst-rating breadth for one ticker.

    Reads the cached output of ``yahoo_events.get_analyst_actions`` from
    disk — never calls yfinance, never writes. Returns ``None`` if no
    cache entry exists for ``ticker`` at all (so the orchestrator can
    distinguish "no data yet" from "data exists but nothing in window").

    Returns a dict with the shape::

        {
          "ticker": "NVDA",
          "as_of": "2026-05-17",
          "window_days": 30,
          "upgrades": 4,
          "downgrades": 1,
          "initiations_bullish": 2,
          "initiations_bearish": 0,
          "reiterations_bullish": 6,
          "reiterations_bearish": 1,
          "net_actions": <weighted sum>,
          "breadth_score": <[-1, +1]>,
          "regime": "<5-level label>",
          "firm_count": 13,
          "raw_actions": [...],
        }
    """
    if not ticker or not str(ticker).strip():
        return None
    sym = ticker.strip().upper()

    actions = _load_cached_actions(sym, cache_dir=cache_dir)
    if actions is None:
        # No cache entry at all — the pipeline hasn't pulled this ticker.
        return None

    agg = _aggregate_actions(actions, window_days=window_days)

    # ``other_count`` is internal bookkeeping; don't surface it in the
    # contract dict but keep it computed for tests if they want it.
    other_count = agg.pop("other_count", 0)

    return {
        "ticker": sym,
        "as_of": _today_iso(),
        "window_days": int(window_days),
        "upgrades": agg["upgrades"],
        "downgrades": agg["downgrades"],
        "initiations_bullish": agg["initiations_bullish"],
        "initiations_bearish": agg["initiations_bearish"],
        "reiterations_bullish": agg["reiterations_bullish"],
        "reiterations_bearish": agg["reiterations_bearish"],
        "net_actions": agg["net_actions"],
        "breadth_score": agg["breadth_score"],
        "regime": agg["regime"],
        "firm_count": agg["firm_count"],
        "raw_actions": agg["raw_actions"],
        "_other_count": other_count,
    }


def compute_universe_breadth(
    tickers: List[str],
    cache_dir: Optional[Path] = None,
    window_days: int = 30,
) -> Dict[str, Dict[str, Any]]:
    """Compute analyst-rating breadth for a list of tickers.

    Convenience wrapper around :func:`compute_analyst_breadth`. Tickers
    with no cached data are simply absent from the output map (callers
    can distinguish "no coverage" by membership). Order of the result is
    not guaranteed.
    """
    out: Dict[str, Dict[str, Any]] = {}
    if not tickers:
        return out
    for t in tickers:
        if not t:
            continue
        result = compute_analyst_breadth(
            t, cache_dir=cache_dir, window_days=window_days
        )
        if result is None:
            continue
        out[result["ticker"]] = result
    return out


__all__ = [
    "compute_analyst_breadth",
    "compute_universe_breadth",
]
