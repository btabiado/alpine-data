#!/usr/bin/env python3
"""LTHCS adaptive-weights tuner CLI.

Reads the daily score history under ``data/lthcs/snapshots/``, joins it
to Yahoo daily closes, and solves for the 5 pillar weights that best
explain forward returns. Defaults to walk-forward CV (60% train / 40%
test) so the report includes an out-of-sample read.

Usage:

    python scripts/lthcs_tune_weights.py
        [--horizon 21]
        [--ridge-alpha 0.5]
        [--universe-subset growth_compounder]
        [--out-dir data/lthcs/adaptive_weights/]
        [--train-fraction 0.6]
        [--walk-forward | --no-walk-forward]
        [--offline]
        [--dry-run]

Outputs to ``<out-dir>/<timestamp>.json`` unless ``--dry-run`` is set.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# Allow running from anywhere — add repo root to path.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pandas as pd  # noqa: E402

from lthcs import adaptive_weights, backtest  # noqa: E402


def _load_universe_subset(name: Optional[str], data_root: Optional[Path]) -> Optional[List[str]]:
    """Resolve a universe-subset filter.

    If ``name`` looks like a maturity-profile key (matches a key in
    ``data/lthcs/universe.json``'s per-ticker ``maturity_stage``), filter
    to tickers carrying that stage. Otherwise treat ``name`` as a
    comma-separated explicit ticker list.

    ``None`` → no filter.
    """
    if name is None:
        return None
    name = name.strip()
    if not name:
        return None

    root = Path(data_root) if data_root else REPO_ROOT / "data" / "lthcs"
    uni_path = root / "universe.json"
    if "," in name and not uni_path.exists():
        return [t.strip() for t in name.split(",") if t.strip()]

    try:
        with uni_path.open("r", encoding="utf-8") as fh:
            uni = json.load(fh)
    except (OSError, json.JSONDecodeError):
        # Fallback: treat as a literal comma-list (or single ticker).
        return [t.strip() for t in name.split(",") if t.strip()]

    tickers_block = uni.get("tickers")
    matched: List[str] = []
    if isinstance(tickers_block, dict):
        for ticker, meta in tickers_block.items():
            if isinstance(meta, dict) and meta.get("maturity_stage") == name:
                matched.append(str(ticker))
    elif isinstance(tickers_block, list):
        for entry in tickers_block:
            if isinstance(entry, dict) and entry.get("maturity_stage") == name:
                t = entry.get("ticker") or entry.get("symbol")
                if t:
                    matched.append(str(t))

    if matched:
        return sorted(matched)
    # Otherwise fall back to comma-list interpretation.
    return [t.strip() for t in name.split(",") if t.strip()]


def _print_tune_summary(result: Dict[str, Any]) -> None:
    print("")
    print("Tuned weights (sum=%.4f):" % sum(result["weights"].values()))
    for p, w in result["weights"].items():
        print("  %-28s  %6.4f" % (p, w))
    print("")
    print("Prior weights:")
    for p, w in zip(adaptive_weights.PILLAR_NAMES, result["prior_weights"]):
        print("  %-28s  %6.4f" % (p, w))
    print("")
    print("Diagnostics:")
    print("  ridge_alpha          : %.4f  (effective = α·n_obs = %.2f)" % (
        result["ridge_alpha"], result.get("ridge_alpha_effective", 0.0)
    ))
    print("  horizon_days         : %d" % result["horizon_days"])
    print("  n_obs                : %d" % result["n_obs"])
    print("  n_rejected_ffill     : %d" % result.get("n_rejected_ffill", 0))
    pillar_sigmas = result.get("pillar_sigmas") or {}
    if pillar_sigmas:
        print("  pillar_sigmas (Z):")
        for p, s in pillar_sigmas.items():
            print("    %-26s  %8.4f" % (p, s))
    print("  in_sample_ic         : %+0.4f" % result["in_sample_ic"])
    print("  fit_method           : %s" % result["fit_method"])
    print("  trained_at           : %s" % result["trained_at"])


def _print_walk_forward_summary(result: Dict[str, Any]) -> None:
    print("")
    print("Walk-forward CV (train %s..%s → test %s..%s):" % (
        result["train_dates"][0], result["train_dates"][1],
        result["test_dates"][0], result["test_dates"][1],
    ))
    real_test = result.get("test_dates_after_ffill_reject", (None, None))
    print("  test_dates (real fwd): %s..%s" % (real_test[0], real_test[1]))
    print("  train_weights :")
    for p, w in result["train_weights"].items():
        print("    %-26s  %6.4f" % (p, w))
    print("  train_ic            : %+0.4f  (n=%d)" % (result["train_ic"], result["n_train_obs"]))
    print("  test_ic             : %+0.4f  (n=%d)" % (result["test_ic"], result["n_test_obs"]))
    print("  overfit_gap         : %+0.4f" % result["overfit_gap"])
    print("  n_rejected_ffill    : %d  (cells nullified across train+test)" %
          result.get("n_rejected_ffill", 0))
    print("  ridge_alpha (user)  : %.4f  (effective_train = %.2f)" % (
        result["ridge_alpha"], result.get("ridge_alpha_effective_train", 0.0)
    ))
    pillar_sigmas = result.get("pillar_sigmas_train") or {}
    if pillar_sigmas:
        print("  pillar_sigmas_train :")
        for p, s in pillar_sigmas.items():
            print("    %-26s  %8.4f" % (p, s))
    print("")
    print("Recommendation: %s" % result["recommendation"].upper())
    print("  reason: %s" % result["recommendation_reason"])


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="LTHCS adaptive-weights tuner")
    parser.add_argument("--horizon", type=int, default=adaptive_weights.DEFAULT_HORIZON,
                        help="Forward-return horizon in trading days (default 21).")
    parser.add_argument("--ridge-alpha", type=float, default=adaptive_weights.DEFAULT_RIDGE_ALPHA,
                        help="L2 strength toward equal-weight prior, expressed as a "
                             "unit-free fraction in [0, ~1]. Internally multiplied by "
                             "n_obs so the same value gives comparable regularization "
                             "across sample sizes. Default 0.5 (~half-strength toward "
                             "the prior). NOTE: this is a breaking change from the "
                             "pre-2026-05-18 module where the same flag was a raw "
                             "scalar fed to the ridge normal equation; values that "
                             "were 'effectively zero' under the old scheme are now "
                             "meaningfully active.")
    parser.add_argument("--universe-subset", type=str, default=None,
                        help="Maturity stage name (e.g. growth_compounder) "
                             "OR comma-separated ticker list. "
                             "Default: all tickers in score history.")
    parser.add_argument("--out-dir", type=str, default="data/lthcs/adaptive_weights",
                        help="Output directory for the tune-result JSON.")
    parser.add_argument("--train-fraction", type=float,
                        default=adaptive_weights.DEFAULT_TRAIN_FRACTION,
                        help="Walk-forward train fraction (default 0.6).")
    parser.add_argument("--walk-forward", dest="walk_forward",
                        action="store_true", default=True,
                        help="Run walk-forward CV (default).")
    parser.add_argument("--no-walk-forward", dest="walk_forward",
                        action="store_false",
                        help="Skip walk-forward CV; do a single in-sample fit only.")
    parser.add_argument("--data-root", type=str, default=None,
                        help="Override data/lthcs/ root (mostly for tests).")
    parser.add_argument("--offline", action="store_true",
                        help="Use cached prices only; never call Yahoo.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print summary; do not write any output file.")
    args = parser.parse_args(argv)

    data_root = Path(args.data_root) if args.data_root else None

    # 1. Load history.
    score = backtest.load_score_history(data_root=data_root)
    if score.empty:
        print("ERROR: no LTHCS score history found under %s" %
              (data_root or backtest._default_data_root()), file=sys.stderr)
        return 1

    pillar_hist: Dict[str, pd.DataFrame] = {
        p: backtest.load_pillar_history(p, data_root=data_root)
        for p in backtest.PILLAR_NAMES
    }

    universe_subset = _load_universe_subset(args.universe_subset, data_root)

    # 2. Forward returns.
    if args.offline:
        class _NoFetchYahoo:
            @staticmethod
            def get_daily_prices(ticker, period="1y"):
                return []
        yahoo_module = _NoFetchYahoo
    else:
        yahoo_module = None

    tickers = list(score.columns)
    start = score.index.min().strftime("%Y-%m-%d")
    end = score.index.max().strftime("%Y-%m-%d")
    fwd_returns = backtest.fetch_forward_returns(
        tickers=tickers,
        start_date=start,
        end_date=end,
        horizons_days=[args.horizon],
        data_root=data_root,
        yahoo_module=yahoo_module,
    )
    fr = fwd_returns.get(args.horizon, pd.DataFrame())

    print("LTHCS adaptive-weights tuner")
    print("  data root      : %s" % (data_root or backtest._default_data_root()))
    print("  obs window     : %s → %s" % (start, end))
    print("  horizon        : %d trading days" % args.horizon)
    print("  ridge_alpha    : %.4f" % args.ridge_alpha)
    print("  n_tickers      : %d" % len(tickers))
    print("  n_dates        : %d" % len(score.index))
    print("  universe_subset: %s" % (
        ("%d names" % len(universe_subset)) if universe_subset else "all"
    ))
    print("  walk_forward   : %s" % args.walk_forward)

    # 3. Run tune.
    if args.walk_forward:
        result = adaptive_weights.walk_forward_tune(
            score_history=score,
            pillar_histories=pillar_hist,
            forward_returns=fr,
            horizon_days=args.horizon,
            ridge_alpha=args.ridge_alpha,
            train_fraction=args.train_fraction,
            universe_subset=universe_subset,
        )
        _print_walk_forward_summary(result)
    else:
        result = adaptive_weights.tune_weights(
            score_history=score,
            pillar_histories=pillar_hist,
            forward_returns=fr,
            horizon_days=args.horizon,
            ridge_alpha=args.ridge_alpha,
            universe_subset=universe_subset,
        )
        _print_tune_summary(result)

    # 4. Persist.
    if args.dry_run:
        print("")
        print("(--dry-run set; no output file written)")
        return 0

    out_root = (REPO_ROOT / args.out_dir).resolve()
    out_root.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = out_root / ("%s.json" % timestamp)
    payload = {
        "mode": "walk_forward" if args.walk_forward else "in_sample",
        "result": result,
        "config": {
            "horizon_days": args.horizon,
            "ridge_alpha": args.ridge_alpha,
            "train_fraction": args.train_fraction if args.walk_forward else None,
            "universe_subset": universe_subset,
            "offline": bool(args.offline),
        },
        "data_window": {"start": start, "end": end, "n_dates": int(len(score.index))},
        "n_tickers": len(tickers),
    }
    backtest._atomic_write_json(out_path, payload)
    print("")
    print("Wrote tune result to: %s" % out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
