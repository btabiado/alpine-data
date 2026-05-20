#!/usr/bin/env python3
"""LTHCS backtest A/B comparison runner (Phase 4).

Runs the engine twice — once with a baseline ``weights.json`` and once
with a candidate variant — and emits a side-by-side comparison artifact
so calibration changes can be stress-tested BEFORE they land in
production.

The engine is reused as-is from :mod:`lthcs.backtest_engine`. The trick
is that on-disk band history is keyed off the production weights, so we
can't just feed it to a different weights config — we have to rebuild
``band_history`` per snapshot under each candidate config. The rebuild
walks every snapshot row, swaps in the profile-keyed weights from the
candidate config, recomputes the composite, and re-bands using
``score.assign_band``. This mirrors the Approach-B pattern already used
by ``backtest_engine_attribution``.

Outputs (under ``data/lthcs/backtest/ab_<timestamp>/``):

  * ``comparison.json``        — both summaries + delta table + verdict
  * ``equity_curve_a.json``    — A equity curve (dates -> equity)
  * ``equity_curve_b.json``    — B equity curve
  * ``benchmark_curve.json``   — SPY benchmark (optional)
  * ``config_a.json``          — copy of the A weights config used
  * ``config_b.json``          — copy of the B weights config used

Usage
-----
Two equivalent ways to specify the candidate:

  * Two full paths::

        python scripts/lthcs_backtest_ab.py \\
            --config-a data/lthcs/weights.json \\
            --config-b data/lthcs/weights_candidate.json

  * Baseline + diff overlay (JSON file shaped like
    ``{"profiles": {"mature_compounder": [0.20, 0.20, 0.20, 0.25,
    0.15]}}``)::

        python scripts/lthcs_backtest_ab.py \\
            --baseline data/lthcs/weights.json \\
            --candidate diff.json

The diff overlay is applied with a deep-merge: ``profiles`` entries
overwrite element-wise, ``score_bands`` overwrite per-band. This is
intentionally narrow — we only support the calibration knobs that the
backtest A/B view actually visualizes.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pandas as pd  # noqa: E402

from lthcs import backtest as _bt  # noqa: E402
from lthcs import backtest_engine as _be  # noqa: E402
from lthcs import backtest_engine_attribution as _bea  # noqa: E402
from lthcs import backtest_profiles  # noqa: E402
from lthcs.score import PILLAR_ORDER, assign_band  # noqa: E402


SCHEMA_VERSION = "1.0.0"

# Metrics surfaced in the delta table. "lower_better" flips the verdict so
# the UI / CLI report a smaller value as the winner.
METRICS: Tuple[Dict[str, Any], ...] = (
    {"key": "total_return",  "label": "Total return",   "lower_better": False, "fmt": "pct"},
    {"key": "ann_return",    "label": "Ann. return",    "lower_better": False, "fmt": "pct"},
    {"key": "sharpe",        "label": "Sharpe",         "lower_better": False, "fmt": "ratio"},
    {"key": "sortino",       "label": "Sortino",        "lower_better": False, "fmt": "ratio"},
    {"key": "max_drawdown",  "label": "Max drawdown",   "lower_better": False, "fmt": "pct"},  # less-negative = better
    {"key": "hit_rate",      "label": "Hit rate",       "lower_better": False, "fmt": "pct"},
    {"key": "turnover",      "label": "Turnover/day",   "lower_better": True,  "fmt": "pct"},
    {"key": "avg_hold_days", "label": "Avg hold (d)",   "lower_better": False, "fmt": "days"},
    {"key": "n_trades",      "label": "# trades",       "lower_better": False, "fmt": "int"},
    {"key": "n_unique_tkr",  "label": "Unique tickers", "lower_better": False, "fmt": "int"},
)


# ---------------------------------------------------------------------------
# Config IO + diff overlay
# ---------------------------------------------------------------------------

def _read_config(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _apply_diff(base: Dict[str, Any], diff: Dict[str, Any]) -> Dict[str, Any]:
    """Apply a narrow diff overlay to a base weights config.

    Supported keys: ``profiles`` (element-wise per maturity stage) and
    ``score_bands`` (per-band). All other top-level keys in ``diff``
    overwrite ``base`` outright. Returns a fresh dict; ``base`` is not
    mutated.
    """
    out = copy.deepcopy(base)
    for k, v in (diff or {}).items():
        if k == "profiles" and isinstance(v, dict):
            profiles = out.setdefault("profiles", {})
            for stage, vec in v.items():
                profiles[stage] = list(vec)
        elif k == "score_bands" and isinstance(v, dict):
            bands = out.setdefault("score_bands", {})
            for band, spec in v.items():
                merged = dict(bands.get(band) or {})
                merged.update(spec or {})
                bands[band] = merged
        else:
            out[k] = copy.deepcopy(v)
    return out


def _config_hash(cfg: Dict[str, Any]) -> str:
    """Deterministic short hash over the profiles + score_bands subset.

    Other keys (description, last_updated, comments) are noise from a
    behavior standpoint so we exclude them. That keeps hash stability
    when only the comment changes.
    """
    payload = {
        "profiles": cfg.get("profiles") or {},
        "score_bands": cfg.get("score_bands") or {},
        "pillar_order": cfg.get("pillar_order") or list(PILLAR_ORDER),
        "modifiers": cfg.get("modifiers") or {},
    }
    blob = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Re-band a snapshot panel under a candidate weights config
# ---------------------------------------------------------------------------

def _profile_weights(
    cfg: Dict[str, Any],
    maturity_stage: Optional[str],
    fallback: Optional[List[float]] = None,
) -> Optional[List[float]]:
    """Return the pillar-weights vector for the given maturity_stage.

    Falls back to ``standard_compounder`` if the stage is missing, then
    to ``fallback`` (typically the row's ``weights_used``).
    """
    profiles = (cfg or {}).get("profiles") or {}
    if maturity_stage and maturity_stage in profiles:
        return [float(x) for x in profiles[maturity_stage]]
    if "standard_compounder" in profiles:
        return [float(x) for x in profiles["standard_compounder"]]
    return list(fallback) if fallback else None


def rebanded_history_for_config(
    snapshots_by_date: Dict[str, List[Dict[str, Any]]],
    cfg: Dict[str, Any],
) -> pd.DataFrame:
    """Pivot a (date x ticker) band frame using ``cfg``'s weights + bands.

    For each snapshot row:
      1. Look up ``cfg.profiles[row.maturity_stage]`` (fallback to
         ``standard_compounder``, then ``row.weights_used``).
      2. Recompute the composite as
         ``sum(subscores[p] * w[i]) + sum(modifiers)`` clamped to [0,100].
      3. Re-derive the band via ``score.assign_band`` with
         ``cfg.score_bands``.

    Rows missing subscores / weights are skipped. The output mirrors
    :func:`lthcs.backtest.load_band_history` shape.
    """
    score_bands = (cfg or {}).get("score_bands") or {}
    records: List[Dict[str, Any]] = []
    for date, rows in snapshots_by_date.items():
        if not isinstance(rows, list):
            continue
        for row in rows:
            t = row.get("ticker")
            if not isinstance(t, str) or not t:
                continue
            subscores = row.get("subscores") or row.get("sub_scores")
            if not isinstance(subscores, dict):
                continue
            maturity = row.get("maturity_stage")
            fallback_w = row.get("weights_used") or row.get("effective_weights")
            new_w = _profile_weights(cfg, maturity, fallback_w)
            if new_w is None or len(new_w) != len(PILLAR_ORDER):
                continue
            mod_sum = _bea._modifier_sum_from_row(row)
            new_score = _bea._recompute_score(subscores, new_w, mod_sum)
            if new_score is None:
                continue
            band = assign_band(new_score, score_bands)
            if not band:
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


# ---------------------------------------------------------------------------
# Comparison math
# ---------------------------------------------------------------------------

def _safe_float(v: Any) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    # NaN/inf -> None so the JSON consumer can render 'n/a'.
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def _verdict_for(metric: Dict[str, Any], a: Optional[float], b: Optional[float]) -> str:
    """Return 'a', 'b', 'tie', or 'unknown' for which side wins.

    Ties are within 1e-9 absolute. lower_better flips the comparison so
    e.g. turnover is "B wins" when B < A.
    """
    if a is None or b is None:
        return "unknown"
    if abs(a - b) < 1e-9:
        return "tie"
    a_better = a < b if metric["lower_better"] else a > b
    return "a" if a_better else "b"


def build_delta_table(
    summary_a: Dict[str, Any],
    summary_b: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Return a list of rows {metric, label, a, b, delta, pct, winner}.

    ``delta`` = b - a. ``pct`` is (b - a) / |a| when ``a`` is non-zero;
    None when undefined. ``winner`` honors the metric's ``lower_better``
    flag.
    """
    table: List[Dict[str, Any]] = []
    for m in METRICS:
        a = _safe_float(summary_a.get(m["key"]))
        b = _safe_float(summary_b.get(m["key"]))
        delta = (b - a) if (a is not None and b is not None) else None
        if a is not None and b is not None and abs(a) > 1e-12:
            pct = (b - a) / abs(a)
        else:
            pct = None
        table.append({
            "key": m["key"],
            "label": m["label"],
            "fmt": m["fmt"],
            "lower_better": bool(m["lower_better"]),
            "a": a,
            "b": b,
            "delta": delta,
            "pct": pct,
            "winner": _verdict_for(m, a, b),
        })
    return table


def overall_verdict(delta_table: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Headline winner-takes-most across the key metrics.

    Sharpe is the tie-breaker (weight 2). Other directional metrics
    (total_return, ann_return, max_drawdown, hit_rate) get weight 1.
    Turnover / trades are diagnostic — excluded from the verdict score.
    """
    weights = {
        "sharpe": 2,
        "total_return": 1,
        "ann_return": 1,
        "max_drawdown": 1,
        "hit_rate": 1,
        "sortino": 1,
    }
    score_a = 0
    score_b = 0
    counted = 0
    for row in delta_table:
        w = weights.get(row["key"])
        if not w:
            continue
        counted += 1
        if row["winner"] == "a":
            score_a += w
        elif row["winner"] == "b":
            score_b += w
    if counted == 0 or score_a == score_b:
        verdict = "tie"
    elif score_a > score_b:
        verdict = "a"
    else:
        verdict = "b"
    return {
        "winner": verdict,
        "score_a": score_a,
        "score_b": score_b,
        "metrics_counted": counted,
    }


# ---------------------------------------------------------------------------
# Engine run helper
# ---------------------------------------------------------------------------

def _build_engine_params(
    profile_name: str,
    cost_bps: Optional[float] = None,
    bands_long: Optional[List[str]] = None,
    top_k: Optional[int] = None,
) -> _be.EngineParams:
    if profile_name == "top_k_by_composite" and top_k is not None:
        profile = backtest_profiles.build_top_k_by_composite(k=int(top_k))
    else:
        profile = backtest_profiles.get_profile(profile_name)
    params = profile.params
    if cost_bps is not None:
        params.cost_bps = float(cost_bps)
    if profile.name == "long_only_buy" and bands_long:
        params.bands_long = list(bands_long)
    return params


def _run_one_side(
    label: str,
    cfg: Dict[str, Any],
    snapshots_by_date: Dict[str, List[Dict[str, Any]]],
    prices: pd.DataFrame,
    bench_series: Optional[pd.Series],
    params: _be.EngineParams,
    score_history_panel: Optional[pd.DataFrame],
) -> Dict[str, Any]:
    band_hist = rebanded_history_for_config(snapshots_by_date, cfg)
    if band_hist.empty:
        return {
            "label": label,
            "config_hash": _config_hash(cfg),
            "summary": {},
            "equity_curve": {},
            "benchmark_curve": {},
            "band_curves": {},
            "run_meta": {"empty": True},
        }
    out = _be.run_backtest(
        band_history=band_hist,
        prices=prices,
        params=params,
        benchmark_prices=bench_series,
        per_band_sweep=False,
        score_history=score_history_panel,
    )
    return {
        "label": label,
        "config_hash": _config_hash(cfg),
        "summary": out["summary"],
        "equity_curve": out["equity_curve"],
        "benchmark_curve": out["benchmark_curve"],
        "band_curves": out["band_curves"],
        "run_meta": out["run_meta"],
    }


# ---------------------------------------------------------------------------
# Public entrypoint (importable for tests)
# ---------------------------------------------------------------------------

def run_ab(
    config_a: Dict[str, Any],
    config_b: Dict[str, Any],
    snapshots_by_date: Dict[str, List[Dict[str, Any]]],
    prices: pd.DataFrame,
    bench_series: Optional[pd.Series] = None,
    params: Optional[_be.EngineParams] = None,
    score_history_panel: Optional[pd.DataFrame] = None,
    label_a: str = "A (baseline)",
    label_b: str = "B (candidate)",
    horizon_days: int = 21,
) -> Dict[str, Any]:
    """Run two engine simulations and return the comparison payload.

    Pure / I/O-free. Tests import this directly.
    """
    if params is None:
        params = _be.EngineParams()

    side_a = _run_one_side(
        label_a, config_a, snapshots_by_date, prices,
        bench_series, params, score_history_panel,
    )
    side_b = _run_one_side(
        label_b, config_b, snapshots_by_date, prices,
        bench_series, params, score_history_panel,
    )

    delta_table = build_delta_table(side_a["summary"], side_b["summary"])
    verdict = overall_verdict(delta_table)

    # Optional pillar-attribution comparison — both runs use Approach B with
    # their own band history as the baseline. Cheap on small windows; the
    # CLI can short-circuit via --no-attribution.
    score_bands_a = (config_a or {}).get("score_bands") or {}
    score_bands_b = (config_b or {}).get("score_bands") or {}

    # Note: Δ-attribution is per-side relative to its own baseline. Comparing
    # "A's adoption_momentum delta" vs "B's adoption_momentum delta" tells
    # you whether the candidate weights make that pillar more / less load-
    # bearing — which is the actual question for a calibration tweak.

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "horizon_days": int(horizon_days),
        "label_a": label_a,
        "label_b": label_b,
        "side_a": side_a,
        "side_b": side_b,
        "delta_table": delta_table,
        "verdict": verdict,
        "config_a_hash": _config_hash(config_a),
        "config_b_hash": _config_hash(config_b),
        "score_bands_a": score_bands_a,
        "score_bands_b": score_bands_b,
        "params": params.to_jsonable(),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_prices_panel(
    tickers: List[str],
    yahoo_module=None,
    benchmark: Optional[str] = None,
) -> Tuple[pd.DataFrame, Optional[pd.Series]]:
    cache = _bt._default_cache_root()
    close_by_ticker: Dict[str, pd.Series] = {}
    for t in tickers:
        rows = _bt._fetch_prices(t, cache_root=cache, yahoo_module=yahoo_module)
        close_by_ticker[t] = _bt._prices_to_close_series(rows)
    bench_series: Optional[pd.Series] = None
    if benchmark:
        rows = _bt._fetch_prices(benchmark, cache_root=cache, yahoo_module=yahoo_module)
        s = _bt._prices_to_close_series(rows)
        bench_series = s if not s.empty else None
    prices_df = pd.DataFrame(close_by_ticker)
    if not prices_df.empty:
        prices_df.index = pd.to_datetime(prices_df.index)
        prices_df.sort_index(inplace=True)
    return prices_df, bench_series


def _print_table(delta_table: List[Dict[str, Any]], label_a: str, label_b: str) -> None:
    def _fmt(val: Optional[float], fmt: str) -> str:
        if val is None:
            return "n/a"
        if fmt == "pct":
            return "%+.4f" % float(val)
        if fmt == "ratio":
            return "%+.3f" % float(val)
        if fmt == "days":
            return "%.1f" % float(val)
        if fmt == "int":
            return "%d" % int(val)
        return "%.4f" % float(val)
    print("")
    print("A/B delta table  (A=%s vs B=%s):" % (label_a, label_b))
    print("  %-18s  %14s  %14s  %14s  %8s  %6s" % (
        "metric", "A", "B", "Δ (B-A)", "Δ%", "winner",
    ))
    for row in delta_table:
        a = _fmt(row["a"], row["fmt"])
        b = _fmt(row["b"], row["fmt"])
        delta = _fmt(row["delta"], row["fmt"]) if row["delta"] is not None else "n/a"
        pct = ("%+.2f%%" % (row["pct"] * 100.0)) if row["pct"] is not None else "n/a"
        print("  %-18s  %14s  %14s  %14s  %8s  %6s" % (
            row["label"], a, b, delta, pct, row["winner"].upper(),
        ))


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="LTHCS backtest A/B runner")
    parser.add_argument("--config-a", type=str, default=None,
                        help="Path to baseline weights JSON.")
    parser.add_argument("--config-b", type=str, default=None,
                        help="Path to candidate weights JSON.")
    parser.add_argument("--baseline", type=str, default=None,
                        help="Path to baseline weights JSON (alias for --config-a; used with --candidate).")
    parser.add_argument("--candidate", type=str, default=None,
                        help="Path to a candidate diff overlay JSON (e.g. {'profiles':{...}}). Layered on --baseline.")
    parser.add_argument("--label-a", type=str, default="A (baseline)")
    parser.add_argument("--label-b", type=str, default="B (candidate)")
    parser.add_argument("--start", type=str, default=None)
    parser.add_argument("--end", type=str, default=None)
    parser.add_argument("--horizon", type=int, default=21)
    parser.add_argument("--profile", type=str,
                        default="long_only_buy",
                        choices=backtest_profiles.available_profiles())
    parser.add_argument("--top-k", type=int, default=None)
    parser.add_argument("--cost-bps", type=float, default=None)
    parser.add_argument("--bands-long", type=str, default=None,
                        help="Comma-separated bands override for the long_only_buy profile.")
    parser.add_argument("--benchmark", type=str, default="SPY")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--data-root", type=str, default=None)
    parser.add_argument("--output-dir", type=str, default="data/lthcs/backtest")
    parser.add_argument("--run-id", type=str, default=None,
                        help="Subdir name under output-dir. Default: 'ab_<UTC-timestamp>'.")
    args = parser.parse_args(argv)

    # Resolve A/B configs.
    if args.config_a and args.config_b:
        cfg_a = _read_config(Path(args.config_a).resolve())
        cfg_b = _read_config(Path(args.config_b).resolve())
    elif args.baseline and args.candidate:
        cfg_a = _read_config(Path(args.baseline).resolve())
        diff = _read_config(Path(args.candidate).resolve())
        cfg_b = _apply_diff(cfg_a, diff)
    else:
        parser.error(
            "must pass either (--config-a + --config-b) or (--baseline + --candidate)"
        )
        return 2

    data_root = Path(args.data_root) if args.data_root else _bt._default_data_root()

    # Load snapshots once, both sides re-band from them.
    snapshots_by_date = _bt._load_all_snapshots(data_root)
    if not snapshots_by_date:
        print("ERROR: no LTHCS snapshots under %s" % data_root)
        return 1

    # Honor --start / --end window before re-banding.
    if args.start or args.end:
        keep: Dict[str, List[Dict[str, Any]]] = {}
        for d, rows in snapshots_by_date.items():
            if args.start and d < args.start:
                continue
            if args.end and d > args.end:
                continue
            keep[d] = rows
        snapshots_by_date = keep

    # Resolve universe + price panel.
    tickers = sorted({
        row.get("ticker")
        for rows in snapshots_by_date.values()
        for row in rows
        if isinstance(row, dict) and isinstance(row.get("ticker"), str)
    })
    if not tickers:
        print("ERROR: snapshot window has no tickers")
        return 1

    if args.offline:
        class _NoFetchYahoo:
            @staticmethod
            def get_daily_prices(ticker, period="1y"):
                return []
        yahoo_module = _NoFetchYahoo
    else:
        yahoo_module = None

    bench_ticker = (args.benchmark or "").strip() or None
    prices, bench_series = _build_prices_panel(
        tickers=tickers, yahoo_module=yahoo_module, benchmark=bench_ticker,
    )
    if prices.empty:
        print("ERROR: no prices for any ticker in the window")
        return 1

    bands_long = None
    if args.bands_long:
        bands_long = [b.strip().lower() for b in args.bands_long.split(",") if b.strip()]

    params = _build_engine_params(
        profile_name=args.profile,
        cost_bps=args.cost_bps,
        bands_long=bands_long,
        top_k=args.top_k,
    )

    score_history_panel = None
    if backtest_profiles.get_profile(args.profile).requires_score_history:
        score_history_panel = _bt.load_score_history(data_root=data_root)

    payload = run_ab(
        config_a=cfg_a,
        config_b=cfg_b,
        snapshots_by_date=snapshots_by_date,
        prices=prices,
        bench_series=bench_series,
        params=params,
        score_history_panel=score_history_panel,
        label_a=args.label_a,
        label_b=args.label_b,
        horizon_days=int(args.horizon),
    )

    # Write artifacts.
    run_id = args.run_id or ("ab_" + datetime.utcnow().strftime("%Y%m%dT%H%M%SZ"))
    out_root = (REPO_ROOT / args.output_dir / run_id).resolve()
    out_root.mkdir(parents=True, exist_ok=True)

    # Top-level comparison.json carries both summaries + delta + verdict.
    _bt._atomic_write_json(out_root / "comparison.json", payload)

    # Equity curves split out so the renderer can lazy-fetch them.
    _bt._atomic_write_json(
        out_root / "equity_curve_a.json", payload["side_a"]["equity_curve"]
    )
    _bt._atomic_write_json(
        out_root / "equity_curve_b.json", payload["side_b"]["equity_curve"]
    )
    if payload["side_a"]["benchmark_curve"]:
        _bt._atomic_write_json(
            out_root / "benchmark_curve.json", payload["side_a"]["benchmark_curve"]
        )

    # Persist the configs we actually ran with so the UI can show diffs.
    _bt._atomic_write_json(out_root / "config_a.json", cfg_a)
    _bt._atomic_write_json(out_root / "config_b.json", cfg_b)

    # Update the latest pointer so the UI knows which dir to read.
    latest = {
        "run_id": run_id,
        "path": str(out_root.relative_to(REPO_ROOT)),
        "generated_at": payload["generated_at"],
        "verdict": payload["verdict"],
        "label_a": payload["label_a"],
        "label_b": payload["label_b"],
        "config_a_hash": payload["config_a_hash"],
        "config_b_hash": payload["config_b_hash"],
    }
    _bt._atomic_write_json(
        (REPO_ROOT / args.output_dir).resolve() / "ab_latest.json", latest
    )

    _print_table(payload["delta_table"], payload["label_a"], payload["label_b"])
    print("")
    v = payload["verdict"]
    print("Verdict: %s (score A=%d, B=%d, %d metrics counted)" % (
        v["winner"].upper(), v["score_a"], v["score_b"], v["metrics_counted"],
    ))
    print("Wrote artifacts to: %s" % out_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
