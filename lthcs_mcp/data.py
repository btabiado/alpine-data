"""LTHCS data-access layer for the MCP server.

Pure functions reading JSON files from ``data/lthcs/`` and returning plain
dicts. Every function is defensive: missing files or bad inputs return an
``{"error": ...}`` dict rather than raising — the MCP transport prefers a
shaped response to a stack trace.

These functions are imported both by :mod:`lthcs_mcp.server` (which wraps
them in FastMCP tool decorators) and by the unit tests (which call them
directly to avoid pulling in the ``mcp`` SDK).
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime
from typing import Any, Dict, List, Optional

# --- Paths -----------------------------------------------------------------

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_THIS_DIR)
DEFAULT_DATA_ROOT = os.path.join(_REPO_ROOT, "data", "lthcs")

_BAND_ORDER = [
    "elite",
    "high_confidence",
    "constructive",
    "monitor",
    "weakening",
    "review",
]

_VALID_INSIDER_REGIMES = {
    "cluster_buying",
    "buying",
    "neutral",
    "selling",
    "heavy_selling",
    "mixed",
}

_VALID_BANDS = {
    "elite",
    "high_confidence",
    "constructive",
    "monitor",
    "weakening",
    "review",
}

_VALID_MOVER_DIRECTIONS = {"up", "down"}

# Canonical pillar order — matches lthcs_tab/lthcs-detail.js PILLAR_ORDER and
# the order of weights_used[] in snapshots. Used by get_dragging_pillar to
# break ties on equal sub-scores by highest weight.
_PILLAR_ORDER = [
    "adoption_momentum",
    "institutional_confidence",
    "financial_evolution",
    "thesis_integrity",
    "des",
]

# Bands where a "drag" is worth surfacing. Buy bucket (elite/high_confidence/
# constructive) and Hold (monitor) tickers don't have a real drag problem.
_DRAG_BANDS = {"weakening", "review"}


# --- Helpers ---------------------------------------------------------------


def _data_root(data_root: Optional[str]) -> str:
    return data_root or DEFAULT_DATA_ROOT


def _err(message: str, **extra: Any) -> Dict[str, Any]:
    out = {"error": message}
    out.update(extra)
    return out


def _parse_date(value: Optional[str]) -> Any:
    """Return parsed ``date`` for ``YYYY-MM-DD``, ``None`` if input is ``None``,
    or an ``{"error": ...}`` dict for invalid input. Future dates also error.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        return _err("date must be a string in YYYY-MM-DD format")
    try:
        parsed = datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError:
        return _err(f"invalid date '{value}'; expected YYYY-MM-DD")
    if parsed > date.today():
        return _err(f"date '{value}' is in the future")
    return parsed


def _read_json(path: str) -> Any:
    """Read a JSON file. Returns an ``{"error": ...}`` dict on missing/bad file."""
    if not os.path.exists(path):
        return _err(f"data not available: {os.path.relpath(path, _REPO_ROOT)}")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        return _err(f"failed to read {os.path.basename(path)}: {exc}")


def _latest_snapshot_date(data_root: str) -> Optional[str]:
    """Most recent ``YYYY-MM-DD.json`` filename in ``snapshots/``."""
    snap_dir = os.path.join(data_root, "snapshots")
    if not os.path.isdir(snap_dir):
        return None
    candidates: List[str] = []
    for name in os.listdir(snap_dir):
        if not name.endswith(".json"):
            continue
        stem = name[:-5]
        try:
            datetime.strptime(stem, "%Y-%m-%d")
        except ValueError:
            continue
        candidates.append(stem)
    if not candidates:
        return None
    return sorted(candidates)[-1]


def _resolve_date(
    date_str: Optional[str], data_root: str
) -> Any:
    """Return a ``YYYY-MM-DD`` string (latest if input is None) or an error dict."""
    parsed = _parse_date(date_str)
    if isinstance(parsed, dict):  # error
        return parsed
    if parsed is None:
        latest = _latest_snapshot_date(data_root)
        if latest is None:
            return _err("no snapshots available in data/lthcs/snapshots/")
        return latest
    return parsed.isoformat()


def _normalize_ticker(ticker: str) -> str:
    return ticker.strip().upper()


def _load_snapshot(date_str: str, data_root: str) -> Any:
    path = os.path.join(data_root, "snapshots", f"{date_str}.json")
    return _read_json(path)


# --- Tool functions --------------------------------------------------------


def get_ticker_score(
    ticker: str,
    date: Optional[str] = None,
    data_root: Optional[str] = None,
) -> Dict[str, Any]:
    """Return composite score, band, drift, and 5 pillar sub-scores for one ticker."""
    if not ticker or not isinstance(ticker, str):
        return _err("ticker is required")
    root = _data_root(data_root)
    resolved = _resolve_date(date, root)
    if isinstance(resolved, dict):
        return resolved
    snap = _load_snapshot(resolved, root)
    if isinstance(snap, dict) and "error" in snap:
        return snap

    sym = _normalize_ticker(ticker)
    for row in snap.get("scores", []):
        if row.get("ticker") == sym:
            return {
                "ticker": sym,
                "date": resolved,
                "score": row.get("lthcs_score"),
                "band": row.get("band"),
                "confidence_level": row.get("confidence_level"),
                "drift": {
                    "1d": row.get("drift_1d"),
                    "7d": row.get("drift_7d"),
                    "30d": row.get("drift_30d"),
                    "90d": row.get("drift_90d"),
                },
                "subscores": row.get("subscores", {}),
                "modifiers": row.get("modifiers", {}),
                "maturity_stage": row.get("maturity_stage"),
                "sector": row.get("sector"),
                "data_quality_flags": row.get("data_quality_flags", []),
            }
    return _err(f"ticker '{sym}' not found in snapshot {resolved}")


def get_universe_distribution(
    date: Optional[str] = None,
    data_root: Optional[str] = None,
) -> Dict[str, Any]:
    """Return band counts (Elite / High / Constructive / Monitor / Weakening / Review)."""
    root = _data_root(data_root)
    resolved = _resolve_date(date, root)
    if isinstance(resolved, dict):
        return resolved
    snap = _load_snapshot(resolved, root)
    if isinstance(snap, dict) and "error" in snap:
        return snap

    counts = {band: 0 for band in _BAND_ORDER}
    other: Dict[str, int] = {}
    total = 0
    for row in snap.get("scores", []):
        total += 1
        band = row.get("band")
        if band in counts:
            counts[band] += 1
        elif band:
            other[band] = other.get(band, 0) + 1
    return {
        "date": resolved,
        "total_tickers": total,
        "bands": counts,
        "other_bands": other,
    }


def get_composite_index(
    date: Optional[str] = None,
    data_root: Optional[str] = None,
) -> Dict[str, Any]:
    """Return the LTHCS composite index: score, label, color, components, note."""
    root = _data_root(data_root)
    resolved = _resolve_date(date, root)
    if isinstance(resolved, dict):
        return resolved
    path = os.path.join(root, "index", f"{resolved}.json")
    payload = _read_json(path)
    if isinstance(payload, dict) and "error" in payload:
        return payload
    return {
        "date": payload.get("as_of", resolved),
        "score": payload.get("score"),
        "label": payload.get("label"),
        "band_key": payload.get("band_key"),
        "color": payload.get("color"),
        "components": payload.get("components", []),
        "note": payload.get("note"),
    }


def get_top_movers(
    direction: str = "gainers",
    limit: int = 10,
    period_days: int = 30,
    data_root: Optional[str] = None,
) -> Dict[str, Any]:
    """Return top-N tickers by score delta over the given history window.

    Uses per-ticker history files. If a ticker lacks an observation roughly
    ``period_days`` old we fall back to the oldest available point. Tickers
    with fewer than two points are skipped.
    """
    if direction not in ("gainers", "decliners"):
        return _err("direction must be 'gainers' or 'decliners'")
    if not isinstance(limit, int) or limit < 1 or limit > 100:
        return _err("limit must be an integer between 1 and 100")
    if not isinstance(period_days, int) or period_days < 1 or period_days > 3650:
        return _err("period_days must be an integer between 1 and 3650")

    root = _data_root(data_root)
    hist_dir = os.path.join(root, "history", "by_ticker")
    if not os.path.isdir(hist_dir):
        return _err("history directory not found")

    deltas: List[Dict[str, Any]] = []
    for name in sorted(os.listdir(hist_dir)):
        if not name.endswith(".json"):
            continue
        sym = name[:-5]
        payload = _read_json(os.path.join(hist_dir, name))
        if isinstance(payload, dict) and "error" in payload:
            continue
        history = payload.get("history") or []
        if len(history) < 2:
            continue
        # Sort newest-first
        try:
            history = sorted(
                history,
                key=lambda r: datetime.strptime(r["date"], "%Y-%m-%d"),
                reverse=True,
            )
        except (KeyError, ValueError):
            continue
        latest = history[0]
        latest_date = datetime.strptime(latest["date"], "%Y-%m-%d").date()
        prior = None
        for row in history[1:]:
            try:
                row_date = datetime.strptime(row["date"], "%Y-%m-%d").date()
            except (KeyError, ValueError):
                continue
            if (latest_date - row_date).days >= period_days:
                prior = row
                break
        if prior is None:
            prior = history[-1]  # fallback: oldest available
        try:
            delta = float(latest["score"]) - float(prior["score"])
        except (KeyError, TypeError, ValueError):
            continue
        deltas.append(
            {
                "ticker": sym,
                "latest_date": latest["date"],
                "latest_score": latest["score"],
                "prior_date": prior["date"],
                "prior_score": prior["score"],
                "delta": round(delta, 3),
                "band": latest.get("band"),
            }
        )

    deltas.sort(key=lambda r: r["delta"], reverse=(direction == "gainers"))
    return {
        "direction": direction,
        "period_days": period_days,
        "count": min(limit, len(deltas)),
        "movers": deltas[:limit],
    }


def get_insider_signals(
    ticker: Optional[str] = None,
    regime: Optional[str] = None,
    date: Optional[str] = None,
    data_root: Optional[str] = None,
) -> Dict[str, Any]:
    """Return Form-4 insider conviction. Filter by ticker or by regime."""
    if ticker is None and regime is None:
        return _err("provide either ticker or regime")
    if regime is not None and regime not in _VALID_INSIDER_REGIMES:
        return _err(
            f"regime must be one of {sorted(_VALID_INSIDER_REGIMES)}"
        )
    root = _data_root(data_root)
    resolved = _resolve_date(date, root)
    if isinstance(resolved, dict):
        return resolved
    path = os.path.join(root, "insider", f"{resolved}.json")
    payload = _read_json(path)
    if isinstance(payload, dict) and "error" in payload and not all(
        k in payload for k in ()  # purely an error envelope
    ):
        return payload
    if not isinstance(payload, dict):
        return _err("insider payload malformed")

    if ticker is not None:
        sym = _normalize_ticker(ticker)
        row = payload.get(sym)
        if row is None:
            return _err(f"no insider data for ticker '{sym}' on {resolved}")
        return {"date": resolved, "ticker": sym, "signals": row}

    # Filter by regime — match against ceo_cfo_action, cluster_buying flag, or
    # a 'regime'-style classification computed from conviction_score.
    matches: List[Dict[str, Any]] = []
    for sym, row in payload.items():
        if not isinstance(row, dict):
            continue
        derived = _classify_insider_regime(row)
        if regime == "cluster_buying":
            if row.get("cluster_buying") is True:
                matches.append({"ticker": sym, **row})
        elif derived == regime:
            matches.append({"ticker": sym, **row})
    return {
        "date": resolved,
        "regime": regime,
        "count": len(matches),
        "tickers": matches,
    }


def _classify_insider_regime(row: Dict[str, Any]) -> str:
    """Derive a regime label from raw insider record fields."""
    if row.get("cluster_buying") is True:
        return "cluster_buying"
    score = row.get("conviction_score")
    if score is None:
        return "neutral"
    try:
        s = float(score)
    except (TypeError, ValueError):
        return "neutral"
    if s >= 0.6:
        return "buying"
    if s >= 0.2:
        return "mixed"
    if s <= -0.6:
        return "heavy_selling"
    if s <= -0.2:
        return "selling"
    return "neutral"


def get_holdings(
    ticker: str,
    date: Optional[str] = None,
    data_root: Optional[str] = None,
) -> Dict[str, Any]:
    """Return 13F institutional holdings: conviction_signal, signal_score, holders, count."""
    if not ticker:
        return _err("ticker is required")
    root = _data_root(data_root)
    resolved = _resolve_date(date, root)
    if isinstance(resolved, dict):
        return resolved
    path = os.path.join(root, "holdings", f"{resolved}.json")
    payload = _read_json(path)
    if isinstance(payload, dict) and "error" in payload and len(payload) == 1:
        return payload
    if not isinstance(payload, dict):
        return _err("holdings payload malformed")
    sym = _normalize_ticker(ticker)
    row = payload.get(sym)
    if row is None:
        return _err(f"no holdings data for ticker '{sym}' on {resolved}")
    return {
        "date": resolved,
        "ticker": sym,
        "conviction_signal": row.get("conviction_signal"),
        "signal_score": row.get("signal_score"),
        "data_quality": row.get("data_quality"),
        "latest_quarter": row.get("latest_quarter"),
        "manager_count": row.get("manager_count"),
        "quarter_over_quarter": row.get("quarter_over_quarter", {}),
        "top_holders": row.get("top_holders", []),
    }


def get_pillar_breakdown(
    ticker: str,
    date: Optional[str] = None,
    data_root: Optional[str] = None,
) -> Dict[str, Any]:
    """Return the variable_detail rows for a ticker (5 pillars + per-pillar components)."""
    if not ticker:
        return _err("ticker is required")
    root = _data_root(data_root)
    resolved = _resolve_date(date, root)
    if isinstance(resolved, dict):
        return resolved
    path = os.path.join(root, "variable_detail", f"{resolved}.json")
    payload = _read_json(path)
    if isinstance(payload, dict) and "error" in payload:
        return payload
    sym = _normalize_ticker(ticker)
    rows = [
        v for v in payload.get("variables", []) if v.get("ticker") == sym
    ]
    if not rows:
        return _err(f"no pillar breakdown for '{sym}' on {resolved}")
    return {
        "date": resolved,
        "ticker": sym,
        "model_version": payload.get("model_version"),
        "pillars": rows,
    }


def get_history(
    ticker: str,
    days: int = 30,
    data_root: Optional[str] = None,
) -> Dict[str, Any]:
    """Return the last N daily score points for a ticker."""
    if not ticker:
        return _err("ticker is required")
    if not isinstance(days, int) or days < 1 or days > 3650:
        return _err("days must be an integer between 1 and 3650")
    root = _data_root(data_root)
    sym = _normalize_ticker(ticker)
    path = os.path.join(root, "history", "by_ticker", f"{sym}.json")
    payload = _read_json(path)
    if isinstance(payload, dict) and "error" in payload:
        return payload
    history = payload.get("history") or []
    try:
        history = sorted(
            history,
            key=lambda r: datetime.strptime(r["date"], "%Y-%m-%d"),
            reverse=True,
        )
    except (KeyError, ValueError):
        pass
    trimmed = history[:days]
    return {
        "ticker": sym,
        "model_version": payload.get("model_version"),
        "count": len(trimmed),
        "days_requested": days,
        "history": trimmed,
    }


def get_macro_regime(
    date: Optional[str] = None,
    data_root: Optional[str] = None,
) -> Dict[str, Any]:
    """Return FRED breadth + sector strength + breadth sentiment for the date."""
    root = _data_root(data_root)
    resolved = _resolve_date(date, root)
    if isinstance(resolved, dict):
        return resolved

    breadth = _read_json(
        os.path.join(root, "macro", f"breadth_{resolved}.json")
    )
    sectors = _read_json(
        os.path.join(root, "macro", f"sector_strength_{resolved}.json")
    )
    sentiment = _read_json(
        os.path.join(root, "macro", f"breadth_sentiment_{resolved}.json")
    )

    def _maybe(p: Any) -> Any:
        return None if isinstance(p, dict) and "error" in p and len(p) == 1 else p

    available = [
        name
        for name, payload in (
            ("breadth", breadth),
            ("sector_strength", sectors),
            ("breadth_sentiment", sentiment),
        )
        if _maybe(payload) is not None
    ]
    if not available:
        return _err(f"no macro data available for {resolved}")

    return {
        "date": resolved,
        "available": available,
        "breadth": _maybe(breadth),
        "sector_strength": _maybe(sectors),
        "breadth_sentiment": _maybe(sentiment),
    }


def search_tickers(
    query: str,
    limit: int = 10,
    data_root: Optional[str] = None,
) -> Dict[str, Any]:
    """Fuzzy match against ticker symbol or company name; return current scores."""
    if not query or not isinstance(query, str):
        return _err("query is required")
    if not isinstance(limit, int) or limit < 1 or limit > 50:
        return _err("limit must be an integer between 1 and 50")
    root = _data_root(data_root)
    q = query.strip().lower()
    if not q:
        return _err("query cannot be empty")

    universe_path = os.path.join(root, "universe.json")
    universe = _read_json(universe_path)
    if isinstance(universe, dict) and "error" in universe:
        return universe
    tickers_list: List[Dict[str, Any]] = universe.get("tickers", [])

    latest_date = _latest_snapshot_date(root)
    score_map: Dict[str, Dict[str, Any]] = {}
    if latest_date is not None:
        snap = _load_snapshot(latest_date, root)
        if isinstance(snap, dict) and "scores" in snap:
            for row in snap["scores"]:
                score_map[row.get("ticker", "")] = row

    matches: List[Dict[str, Any]] = []
    for entry in tickers_list:
        sym = (entry.get("ticker") or "").upper()
        name = entry.get("name") or ""
        if not sym:
            continue
        sym_l = sym.lower()
        name_l = name.lower()
        # Rank: exact symbol > symbol prefix > symbol contains > name contains
        if sym_l == q:
            rank = 0
        elif sym_l.startswith(q):
            rank = 1
        elif q in sym_l:
            rank = 2
        elif q in name_l:
            rank = 3
        else:
            continue
        score_row = score_map.get(sym, {})
        matches.append(
            {
                "rank": rank,
                "ticker": sym,
                "name": name,
                "sector": entry.get("sector"),
                "maturity_stage": entry.get("maturity_stage"),
                "score": score_row.get("lthcs_score"),
                "band": score_row.get("band"),
            }
        )

    matches.sort(key=lambda r: (r["rank"], r["ticker"]))
    for m in matches:
        m.pop("rank", None)
    return {
        "query": query,
        "as_of": latest_date,
        "count": min(limit, len(matches)),
        "matches": matches[:limit],
    }


def get_dragging_pillar(
    ticker: str,
    date: Optional[str] = None,
    data_root: Optional[str] = None,
) -> Dict[str, Any]:
    """Return the pillar dragging a Weakening/Review ticker's composite the most.

    Mirrors the detail-modal callout shipped in 014aadc: for tickers in band
    ``weakening`` or ``review``, surface the pillar with the LOWEST sub-score
    across the 5 pillars (adoption_momentum, institutional_confidence,
    financial_evolution, thesis_integrity, des). Ties broken by highest weight
    in ``weights_used`` — i.e. the pillar dragging the composite the most.
    For tickers in Buy bucket (elite/high_confidence/constructive) or Hold
    (monitor), returns ``{"dragging_pillar": null, "reason": "..."}``.

    Source: today's snapshot (canonical subscores + weights_used) with a
    defensive fallback to averaging variable_detail sub_scores per pillar if
    the snapshot lacks them.
    """
    if not ticker or not isinstance(ticker, str):
        return _err("ticker is required")
    root = _data_root(data_root)
    resolved = _resolve_date(date, root)
    if isinstance(resolved, dict):
        return resolved
    snap = _load_snapshot(resolved, root)
    if isinstance(snap, dict) and "error" in snap:
        return snap

    sym = _normalize_ticker(ticker)
    row = None
    for r in snap.get("scores", []):
        if r.get("ticker") == sym:
            row = r
            break
    if row is None:
        return _err(f"ticker '{sym}' not found in snapshot {resolved}")

    band = row.get("band") or ""
    if band not in _DRAG_BANDS:
        return {
            "ticker": sym,
            "band": band,
            "dragging_pillar": None,
            "sub_score": None,
            "rationale": "ticker is in Buy or Hold; no drag to surface",
        }

    # Prefer canonical subscores from the snapshot.
    subs_raw = row.get("subscores") or {}
    subs: Dict[str, float] = {}
    if isinstance(subs_raw, dict):
        for k, v in subs_raw.items():
            try:
                subs[k] = float(v)
            except (TypeError, ValueError):
                continue

    # Defensive fallback: average sub_score per pillar from variable_detail.
    if not subs:
        vd_path = os.path.join(root, "variable_detail", f"{resolved}.json")
        vd_payload = _read_json(vd_path)
        if isinstance(vd_payload, dict) and "variables" in vd_payload:
            buckets: Dict[str, List[float]] = {}
            for vrow in vd_payload.get("variables", []):
                if not isinstance(vrow, dict) or vrow.get("ticker") != sym:
                    continue
                pillar = vrow.get("pillar")
                try:
                    val = float(vrow.get("sub_score"))
                except (TypeError, ValueError):
                    continue
                if not pillar:
                    continue
                buckets.setdefault(pillar, []).append(val)
            for pillar, arr in buckets.items():
                if arr:
                    subs[pillar] = sum(arr) / len(arr)

    if not subs:
        return _err(f"no sub-scores available for '{sym}' on {resolved}")

    weights = row.get("weights_used") if isinstance(row.get("weights_used"), list) else []

    # Find pillar with lowest sub-score; tie-break by highest weight.
    best_key: Optional[str] = None
    best_score = float("inf")
    best_weight = float("-inf")
    for i, key in enumerate(_PILLAR_ORDER):
        if key not in subs:
            continue
        v = subs[key]
        try:
            w = float(weights[i])
        except (IndexError, TypeError, ValueError):
            w = 0.0
        if v < best_score - 1e-9:
            best_score = v
            best_weight = w
            best_key = key
        elif abs(v - best_score) <= 1e-9 and w > best_weight:
            best_weight = w
            best_key = key

    if best_key is None:
        return _err(f"no recognised pillar sub-scores for '{sym}' on {resolved}")

    composite = row.get("lthcs_score")
    try:
        composite_str = f"{float(composite):.1f}"
    except (TypeError, ValueError):
        composite_str = "n/a"

    return {
        "ticker": sym,
        "band": band,
        "dragging_pillar": best_key,
        "sub_score": round(best_score, 3),
        "rationale": (
            f"{best_key} has the lowest sub-score ({best_score:.1f}) of the 5 "
            f"pillars; composite {composite_str}."
        ),
    }


# --- New tools (Tier 5 #26 follow-on) -------------------------------------


def list_band(
    band: str,
    limit: int = 20,
    date: Optional[str] = None,
    data_root: Optional[str] = None,
) -> Dict[str, Any]:
    """List tickers in a given band on a given date, sorted by composite desc.

    Returns the top ``limit`` tickers (default 20) in the requested band.
    Useful for surfacing today's Elite / High-Confidence / Weakening lists
    without pulling the full snapshot.
    """
    if not band or not isinstance(band, str):
        return _err("band is required")
    band_norm = band.strip().lower()
    if band_norm not in _VALID_BANDS:
        return _err(
            f"band must be one of {sorted(_VALID_BANDS)}"
        )
    if not isinstance(limit, int) or limit < 1 or limit > 500:
        return _err("limit must be an integer between 1 and 500")
    root = _data_root(data_root)
    resolved = _resolve_date(date, root)
    if isinstance(resolved, dict):
        return resolved
    snap = _load_snapshot(resolved, root)
    if isinstance(snap, dict) and "error" in snap:
        return snap

    rows: List[Dict[str, Any]] = []
    for r in snap.get("scores", []):
        if r.get("band") != band_norm:
            continue
        try:
            score = float(r.get("lthcs_score"))
        except (TypeError, ValueError):
            continue
        rows.append(
            {
                "ticker": r.get("ticker"),
                "score": score,
                "drift_7d": r.get("drift_7d"),
                "drift_30d": r.get("drift_30d"),
                "sector": r.get("sector"),
                "confidence_level": r.get("confidence_level"),
            }
        )
    rows.sort(key=lambda x: x["score"], reverse=True)
    trimmed = rows[:limit]
    return {
        "date": resolved,
        "band": band_norm,
        "total_in_band": len(rows),
        "count": len(trimmed),
        "limit": limit,
        "tickers": trimmed,
    }


def get_pillar_attribution(
    ticker: str,
    pillar: str,
    date: Optional[str] = None,
    data_root: Optional[str] = None,
) -> Dict[str, Any]:
    """Return one pillar's sub-score and variable_detail evidence for a ticker.

    Unlike ``get_pillar_breakdown`` which returns all 5 pillars, this returns
    only the requested pillar with its raw component signals (the data that
    fed into the sub-score) plus the canonical sub-score from the snapshot.
    """
    if not ticker or not isinstance(ticker, str):
        return _err("ticker is required")
    if not pillar or not isinstance(pillar, str):
        return _err("pillar is required")
    pillar_norm = pillar.strip().lower()
    if pillar_norm not in set(_PILLAR_ORDER):
        return _err(
            f"pillar must be one of {_PILLAR_ORDER}"
        )
    root = _data_root(data_root)
    resolved = _resolve_date(date, root)
    if isinstance(resolved, dict):
        return resolved
    sym = _normalize_ticker(ticker)

    # Canonical sub-score from snapshot.
    snap = _load_snapshot(resolved, root)
    canonical_sub_score: Optional[float] = None
    if isinstance(snap, dict) and "scores" in snap:
        for r in snap.get("scores", []):
            if r.get("ticker") == sym:
                subs = r.get("subscores") or {}
                if isinstance(subs, dict) and pillar_norm in subs:
                    try:
                        canonical_sub_score = float(subs[pillar_norm])
                    except (TypeError, ValueError):
                        canonical_sub_score = None
                break

    # variable_detail evidence rows for this pillar.
    vd_path = os.path.join(root, "variable_detail", f"{resolved}.json")
    vd = _read_json(vd_path)
    if isinstance(vd, dict) and "error" in vd:
        return vd
    evidence: List[Dict[str, Any]] = []
    for v in vd.get("variables", []):
        if not isinstance(v, dict):
            continue
        if v.get("ticker") != sym or v.get("pillar") != pillar_norm:
            continue
        evidence.append(
            {
                "sub_score": v.get("sub_score"),
                "components": v.get("components", {}),
                "data_quality": v.get("data_quality", {}),
                "notes": v.get("notes"),
            }
        )
    if canonical_sub_score is None and not evidence:
        return _err(
            f"no data for pillar '{pillar_norm}' on ticker '{sym}' for {resolved}"
        )
    return {
        "date": resolved,
        "ticker": sym,
        "pillar": pillar_norm,
        "sub_score": canonical_sub_score,
        "evidence": evidence,
    }


def get_recent_movers(
    direction: str = "up",
    limit: int = 10,
    data_root: Optional[str] = None,
    date: Optional[str] = None,
) -> Dict[str, Any]:
    """Top/bottom N tickers by ``drift_7d`` in the most recent snapshot.

    Mirrors the Movers leaderboard from the UI: positive direction returns
    largest 7-day drift gainers; negative returns the largest 7-day drift
    decliners. Uses the canonical ``drift_7d`` field on each ticker — no
    history-file walk required, so it's cheap and matches what the UI shows.
    """
    if direction not in _VALID_MOVER_DIRECTIONS:
        return _err(
            f"direction must be one of {sorted(_VALID_MOVER_DIRECTIONS)}"
        )
    if not isinstance(limit, int) or limit < 1 or limit > 100:
        return _err("limit must be an integer between 1 and 100")
    root = _data_root(data_root)
    resolved = _resolve_date(date, root)
    if isinstance(resolved, dict):
        return resolved
    snap = _load_snapshot(resolved, root)
    if isinstance(snap, dict) and "error" in snap:
        return snap

    rows: List[Dict[str, Any]] = []
    for r in snap.get("scores", []):
        drift = r.get("drift_7d")
        if drift is None:
            continue
        try:
            drift_f = float(drift)
        except (TypeError, ValueError):
            continue
        rows.append(
            {
                "ticker": r.get("ticker"),
                "score": r.get("lthcs_score"),
                "band": r.get("band"),
                "drift_7d": drift_f,
                "sector": r.get("sector"),
            }
        )
    rows.sort(key=lambda x: x["drift_7d"], reverse=(direction == "up"))
    trimmed = rows[:limit]
    return {
        "date": resolved,
        "direction": direction,
        "count": len(trimmed),
        "limit": limit,
        "movers": trimmed,
    }


def _latest_crypto_snapshot_date(data_root: str) -> Optional[str]:
    """Most recent ``YYYY-MM-DD.json`` filename in ``snapshots_crypto/``."""
    snap_dir = os.path.join(data_root, "snapshots_crypto")
    if not os.path.isdir(snap_dir):
        return None
    candidates: List[str] = []
    for name in os.listdir(snap_dir):
        if not name.endswith(".json"):
            continue
        stem = name[:-5]
        try:
            datetime.strptime(stem, "%Y-%m-%d")
        except ValueError:
            continue
        candidates.append(stem)
    if not candidates:
        return None
    return sorted(candidates)[-1]


def get_crypto_universe(
    date: Optional[str] = None,
    data_root: Optional[str] = None,
) -> Dict[str, Any]:
    """Return the latest crypto LTHCS snapshot (BTC, ETH, SOL, etc.).

    Reads ``data/lthcs/snapshots_crypto/<date>.json`` — the parallel scoring
    pipeline shipped in V1 for the 10-asset crypto universe (BTC, ETH, SOL,
    ADA, AVAX, DOT, LINK, POL, XRP, DOGE). When ``date`` is None the most
    recent crypto snapshot is used.
    """
    root = _data_root(data_root)
    parsed = _parse_date(date)
    if isinstance(parsed, dict):
        return parsed
    if parsed is None:
        latest = _latest_crypto_snapshot_date(root)
        if latest is None:
            return _err(
                "no snapshots available in data/lthcs/snapshots_crypto/"
            )
        resolved = latest
    else:
        resolved = parsed.isoformat()

    path = os.path.join(root, "snapshots_crypto", f"{resolved}.json")
    payload = _read_json(path)
    if isinstance(payload, dict) and "error" in payload:
        return payload

    rows: List[Dict[str, Any]] = []
    for r in payload.get("scores", []):
        if not isinstance(r, dict):
            continue
        rows.append(
            {
                "ticker": r.get("ticker"),
                "score": r.get("lthcs_score"),
                "band": r.get("band"),
                "confidence_level": r.get("confidence_level"),
                "subscores": r.get("subscores", {}),
                "dropped_pillars": r.get("dropped_pillars", []),
                "drift_7d": r.get("drift_7d"),
                "drift_30d": r.get("drift_30d"),
                "maturity_stage": r.get("maturity_stage"),
                "data_quality_flags": r.get("data_quality_flags", []),
            }
        )
    rows.sort(
        key=lambda x: (x.get("score") if isinstance(x.get("score"), (int, float)) else -1),
        reverse=True,
    )
    return {
        "date": payload.get("calc_date", resolved),
        "asset_class": payload.get("asset_class", "crypto"),
        "model_version": payload.get("model_version"),
        "count": len(rows),
        "tickers": rows,
    }
