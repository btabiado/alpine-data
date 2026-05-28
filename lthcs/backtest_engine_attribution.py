"""LTHCS backtest engine — Phase 2: per-pillar attribution (Tier 5 #24).

Companion to ``lthcs/backtest_engine.py``. Phase 1 produces one
non-overlapping equity curve from a ``band_history`` panel. Phase 2
answers the question "what was each pillar's marginal contribution to
that equity curve?".

Approach B (per ``docs/lthcs-backtest-engine-spec.md`` §5):

  For each pillar p (5 total):
    1. Take every snapshot row's ``subscores`` + ``weights_used``.
    2. Set ``weights_used[p] := 0`` and renormalize the remaining
       four weights to sum to 1.0.
    3. Recompute the composite (= sum(subscores * new_weights) + the
       row's modifier sum) and re-derive the band via
       ``lthcs.score.assign_band``.
    4. This produces an alternate ``band_history`` panel for the same
       window. Feed it back into ``run_backtest`` with the same prices
       + params.
    5. The Δ-equity vs the baseline run is "P&L if pillar p had zero
       weight". Reported as Δ-Sharpe, Δ-total-return, and Δ-max-DD per
       pillar.

The engine in ``lthcs/backtest_engine.py`` is pure / stateless, so we
just call it 5 + 1 times. No mutation of production code. ``score.py``
is read-only — we only use ``assign_band``.

**Caveat documented in the JSON output**: pillar attributions are *not
additive*. Removing pillar A and pillar B independently and summing the
two Δ's is NOT the same as removing both at once — the renormalization
and the resulting band transitions interact non-linearly.

The module is pure: it takes in-memory data and returns a dict. The
``scripts/lthcs_backtest.py`` wrapper does the I/O.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pandas as pd

from lthcs import backtest_engine
from lthcs.backtest_engine import EngineParams, run_backtest
from lthcs.score import PILLAR_ORDER, assign_band


# Pillar names in canonical order. ``score.PILLAR_ORDER`` is the source
# of truth; we re-export here so callers don't have to import score.py.
PILLARS: Tuple[str, ...] = tuple(PILLAR_ORDER)


ATTRIBUTION_NOTE = (
    "Pillar attributions are NOT additive. Removing two pillars is "
    "not the sum of removing each — the renormalization step (and "
    "the resulting band transitions) interact non-linearly. Each "
    "delta_* field below is the marginal effect of zeroing that one "
    "pillar relative to the baseline (Approach B in the spec §5)."
)


# ---------------------------------------------------------------------------
# Re-banding a snapshot panel with one pillar's weight set to zero
# ---------------------------------------------------------------------------

def _renormalize_with_pillar_zeroed(
    weights: Sequence[float],
    pillar_idx: int,
) -> Optional[List[float]]:
    """Return a copy of ``weights`` with ``weights[pillar_idx] := 0`` and
    the remaining weights renormalized so the sum is 1.0.

    Returns ``None`` if the remaining weights all sum to 0 (pathological
    snapshot — a row whose only non-zero weight was on the zeroed pillar).
    """
    if not weights or pillar_idx < 0 or pillar_idx >= len(weights):
        return None
    new = [float(w) for w in weights]
    new[pillar_idx] = 0.0
    s = sum(new)
    if s <= 0.0 or not math.isfinite(s):
        return None
    return [w / s for w in new]


def _recompute_score(
    subscores: Dict[str, Any],
    weights: Sequence[float],
    modifier_sum: float,
) -> Optional[float]:
    """Composite = sum(subscores[pillar] * weights[i]) + modifier_sum,
    clamped to [0, 100]. Returns None if any subscore is missing.
    """
    if not isinstance(subscores, dict):
        return None
    total = 0.0
    for i, pillar in enumerate(PILLARS):
        v = subscores.get(pillar)
        if v is None:
            return None
        try:
            total += float(v) * float(weights[i])
        except (TypeError, ValueError, IndexError):
            return None
    total += float(modifier_sum or 0.0)
    # Clamp to [0, 100], matching lthcs/score.py:compute_lthcs_score.
    if not math.isfinite(total):
        return None
    return max(0.0, min(100.0, total))


def _modifier_sum_from_row(row: Dict[str, Any]) -> float:
    """Sum the per-row modifier deltas (macro + sector + volatility).

    Snapshots store them under a ``modifiers`` dict; defensive defaults
    let missing keys yield 0.0 rather than crashing the re-banding step.
    """
    mods = row.get("modifiers") or {}
    if not isinstance(mods, dict):
        return 0.0
    out = 0.0
    for k in ("macro_adj", "sector_adj", "volatility_mod"):
        v = mods.get(k)
        if v is None:
            continue
        try:
            out += float(v)
        except (TypeError, ValueError):
            continue
    return out


def rebanded_history_for_pillar(
    snapshots_by_date: Dict[str, List[Dict[str, Any]]],
    pillar_idx: int,
    score_bands: Dict[str, Dict[str, Any]],
) -> pd.DataFrame:
    """Build a wide (date x ticker) band frame for the "pillar p zeroed"
    counterfactual, using each row's own ``weights_used`` as the base.

    Rows that can't be re-banded (missing subscores, all-zero weights)
    are silently dropped — the resulting ticker/date cell falls back to
    NaN, and the engine treats that as "not held" for that day.
    """
    if pillar_idx < 0 or pillar_idx >= len(PILLARS):
        raise ValueError(
            "pillar_idx %d out of range; must be in 0..%d"
            % (pillar_idx, len(PILLARS) - 1)
        )

    records: List[Dict[str, Any]] = []
    for date, rows in snapshots_by_date.items():
        if not isinstance(rows, list):
            continue
        for row in rows:
            t = row.get("ticker")
            if not isinstance(t, str) or not t:
                continue
            subscores = row.get("subscores") or row.get("sub_scores")
            weights = row.get("weights_used") or row.get("effective_weights")
            if not subscores or not weights:
                continue
            new_w = _renormalize_with_pillar_zeroed(weights, pillar_idx)
            if new_w is None:
                continue
            mod_sum = _modifier_sum_from_row(row)
            new_score = _recompute_score(subscores, new_w, mod_sum)
            if new_score is None:
                continue
            new_band = assign_band(new_score, score_bands)
            if not new_band:
                continue
            records.append({"date": date, "ticker": t, "band": new_band})

    if not records:
        return pd.DataFrame()
    df = pd.DataFrame.from_records(records)
    df["date"] = pd.to_datetime(df["date"])
    wide = df.pivot_table(
        index="date", columns="ticker", values="band", aggfunc="last"
    )
    wide.sort_index(inplace=True)
    return wide


# ---------------------------------------------------------------------------
# Summary delta helpers
# ---------------------------------------------------------------------------

def _safe_float(v: Any) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _summary_deltas(
    baseline: Dict[str, Any],
    variant: Dict[str, Any],
) -> Dict[str, float]:
    """Compute Δ-metrics (variant minus baseline) on the headline stats."""
    out: Dict[str, float] = {}
    for k in ("sharpe", "sortino", "total_return", "ann_return",
              "max_drawdown", "hit_rate", "turnover", "avg_hold_days"):
        b = _safe_float(baseline.get(k))
        v = _safe_float(variant.get(k))
        if b is None or v is None:
            out["delta_" + k] = None
        else:
            out["delta_" + k] = v - b
    # Also keep the variant absolute values for the UI tooltip.
    for k in ("sharpe", "total_return"):
        v = _safe_float(variant.get(k))
        out["variant_" + k] = v
    return out


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------

def run_attribution(
    snapshots_by_date: Dict[str, List[Dict[str, Any]]],
    prices: pd.DataFrame,
    score_bands: Dict[str, Dict[str, Any]],
    params: Optional[EngineParams] = None,
    benchmark_prices: Optional[pd.Series] = None,
    baseline_band_history: Optional[pd.DataFrame] = None,
) -> Dict[str, Any]:
    """Run Phase-2 per-pillar attribution.

    Parameters
    ----------
    snapshots_by_date : dict
        ``{date_str: [score_row, ...]}`` — the raw scores list from
        ``data/lthcs/snapshots/<date>.json``. Each row must contain
        ``ticker``, ``subscores`` (dict per pillar), ``weights_used``
        (list aligned to ``score.PILLAR_ORDER``), and a ``modifiers``
        dict.
    prices : DataFrame
        (trading_day x ticker) wide adjusted closes panel — same as
        what Phase 1 consumes.
    score_bands : dict
        ``weights_config["score_bands"]`` from ``data/lthcs/weights.json``.
    params : EngineParams, optional
        Engine params; defaults to Phase 1.
    benchmark_prices : Series, optional
        Benchmark series for the baseline run only (not threaded into
        per-pillar runs — Δ-metrics already cancel the benchmark).
    baseline_band_history : DataFrame, optional
        Pre-built baseline band panel. If omitted, we derive one from
        ``snapshots_by_date`` using each row's existing band field
        (lossless: no recompute).

    Returns
    -------
    dict
        See ``docs/lthcs-backtest-engine-spec.md`` §4 — the
        ``pillar_attribution.json`` schema. Keys: ``note``,
        ``baseline``, ``per_pillar``, ``params``, ``run_meta``.
    """
    if params is None:
        params = EngineParams()

    if baseline_band_history is None:
        baseline_band_history = _baseline_band_history(snapshots_by_date)

    # 1. Baseline run — uses the production band history as-is.
    baseline_run = run_backtest(
        band_history=baseline_band_history,
        prices=prices,
        params=params,
        benchmark_prices=benchmark_prices,
        per_band_sweep=False,
    )
    baseline_summary = baseline_run["summary"]

    per_pillar: Dict[str, Dict[str, Any]] = {}
    for idx, pillar in enumerate(PILLARS):
        variant_bh = rebanded_history_for_pillar(
            snapshots_by_date=snapshots_by_date,
            pillar_idx=idx,
            score_bands=score_bands,
        )
        if variant_bh.empty:
            per_pillar[pillar] = {
                "status": "empty",
                "note": "Re-banding produced no rows; pillar effectively missing in this window.",
            }
            continue
        variant_run = run_backtest(
            band_history=variant_bh,
            prices=prices,
            params=params,
            benchmark_prices=None,
            per_band_sweep=False,
        )
        variant_summary = variant_run["summary"]
        deltas = _summary_deltas(baseline_summary, variant_summary)
        per_pillar[pillar] = {
            "status": "ok",
            "pillar_index": idx,
            "variant_summary": {
                k: variant_summary.get(k)
                for k in (
                    "total_return",
                    "ann_return",
                    "sharpe",
                    "sortino",
                    "max_drawdown",
                    "hit_rate",
                    "avg_hold_days",
                    "turnover",
                    "n_trades",
                    "n_unique_tkr",
                )
            },
            **deltas,
        }

    n_snapshots = sum(
        1 for _, rows in snapshots_by_date.items() if isinstance(rows, list) and rows
    )
    return {
        "schema_version": "1.0.0",
        "approach": "B",
        "note": ATTRIBUTION_NOTE,
        "pillars": list(PILLARS),
        "baseline_summary": {
            k: baseline_summary.get(k)
            for k in (
                "total_return",
                "ann_return",
                "sharpe",
                "sortino",
                "max_drawdown",
                "hit_rate",
                "avg_hold_days",
                "turnover",
                "n_trades",
                "n_unique_tkr",
                "n_trading_days",
            )
        },
        "per_pillar": per_pillar,
        "params": params.to_jsonable(),
        "run_meta": {
            "engine_version": baseline_run.get("run_meta", {}).get("engine_version"),
            "attribution_version": "1.0.0",
            "n_snapshots": int(n_snapshots),
            "window": baseline_run.get("run_meta", {}).get("window"),
            "universe_size": baseline_run.get("run_meta", {}).get("universe_size"),
        },
    }


def _baseline_band_history(
    snapshots_by_date: Dict[str, List[Dict[str, Any]]],
) -> pd.DataFrame:
    """Pivot the production ``band`` field out of the snapshots into the
    same wide frame ``load_band_history`` produces. We bypass disk
    re-read so the attribution module can be called purely in-memory.
    """
    records: List[Dict[str, Any]] = []
    for date, rows in snapshots_by_date.items():
        if not isinstance(rows, list):
            continue
        for row in rows:
            t = row.get("ticker")
            band = row.get("band")
            if not isinstance(t, str) or not isinstance(band, str):
                continue
            records.append({"date": date, "ticker": t, "band": band})
    if not records:
        return pd.DataFrame()
    df = pd.DataFrame.from_records(records)
    df["date"] = pd.to_datetime(df["date"])
    wide = df.pivot_table(
        index="date", columns="ticker", values="band", aggfunc="last"
    )
    wide.sort_index(inplace=True)
    return wide


__all__ = [
    "PILLARS",
    "ATTRIBUTION_NOTE",
    "run_attribution",
    "rebanded_history_for_pillar",
]
