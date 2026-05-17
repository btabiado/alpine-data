#!/usr/bin/env python3
"""LTHCS backtest CLI.

Joins the LTHCS daily score history to Yahoo daily closes and emits:

  * portfolio_returns.json  — daily band-portfolio P&L
  * pillar_ic.json          — Spearman IC per pillar
  * quintile_returns.json   — quintile spread per pillar
  * summary.json            — top-level Sharpe / drawdown / hit-rate
  * stdout                  — concise summary table

Usage:
    python scripts/lthcs_backtest.py [--start YYYY-MM-DD] [--end YYYY-MM-DD]
                                     [--horizon 21]
                                     [--bands-long elite,high_confidence]
                                     [--bands-short review]
                                     [--output-dir data/lthcs/backtest/]
                                     [--run-id <id>]
                                     [--offline]

``--offline`` skips Yahoo and uses the indefinite price cache only;
useful in CI / when no network is available.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

# Allow running from anywhere — add repo root to path so we can import the
# lthcs package without requiring `pip install -e`.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pandas as pd  # noqa: E402

from lthcs import backtest  # noqa: E402


def _parse_band_list(raw: str) -> List[str]:
    return [b.strip().lower() for b in raw.split(",") if b.strip()]


def _build_run_id(now: Optional[datetime] = None) -> str:
    now = now or datetime.now()
    return now.strftime("%Y%m%dT%H%M%S")


def _hit_rate(daily_returns: pd.Series) -> float:
    s = daily_returns.dropna()
    if s.empty:
        return 0.0
    return float((s > 0).sum() / len(s))


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="LTHCS backtest runner")
    parser.add_argument("--start", type=str, default=None,
                        help="Start observation date (YYYY-MM-DD). "
                             "Default: earliest snapshot.")
    parser.add_argument("--end", type=str, default=None,
                        help="End observation date (YYYY-MM-DD). "
                             "Default: latest snapshot.")
    parser.add_argument("--horizon", type=int, default=21,
                        help="Forward-return horizon in trading days "
                             "(used for headline portfolio & quintile sort). "
                             "Default: 21 (~1 month).")
    parser.add_argument(
        "--bands-long",
        type=str,
        default=",".join(backtest.DEFAULT_LONG_BANDS),
        help="Comma-separated bands for the long leg.",
    )
    parser.add_argument(
        "--bands-short",
        type=str,
        default=",".join(backtest.DEFAULT_SHORT_BANDS),
        help="Comma-separated bands for the short leg.",
    )
    parser.add_argument("--output-dir", type=str,
                        default="data/lthcs/backtest",
                        help="Output directory for run artifacts.")
    parser.add_argument("--run-id", type=str, default=None,
                        help="Subdirectory name under output-dir. "
                             "Default: current timestamp.")
    parser.add_argument("--data-root", type=str, default=None,
                        help="Override data/lthcs/ root (mostly for tests).")
    parser.add_argument("--offline", action="store_true",
                        help="Use cached prices only; never call Yahoo.")
    args = parser.parse_args(argv)

    data_root = Path(args.data_root) if args.data_root else None
    bands_long = _parse_band_list(args.bands_long)
    bands_short = _parse_band_list(args.bands_short)
    horizon = int(args.horizon)
    run_id = args.run_id or _build_run_id()
    out_root = (REPO_ROOT / args.output_dir / run_id).resolve()
    out_root.mkdir(parents=True, exist_ok=True)

    # 1. Load history.
    score = backtest.load_score_history(data_root=data_root)
    band = backtest.load_band_history(data_root=data_root)
    pillar_hist: Dict[str, pd.DataFrame] = {}
    for p in backtest.PILLAR_NAMES:
        pillar_hist[p] = backtest.load_pillar_history(p, data_root=data_root)

    if score.empty:
        print("ERROR: no LTHCS history found under %s" %
              (data_root or backtest._default_data_root()))
        return 1

    if args.start is None:
        start = score.index.min().strftime("%Y-%m-%d")
    else:
        start = args.start
    if args.end is None:
        end = score.index.max().strftime("%Y-%m-%d")
    else:
        end = args.end

    tickers = list(score.columns)
    print("LTHCS backtest")
    print("  data root  : %s" % (data_root or backtest._default_data_root()))
    print("  obs window : %s → %s" % (start, end))
    print("  horizon    : %d trading days" % horizon)
    print("  long bands : %s" % bands_long)
    print("  short bands: %s" % bands_short)
    print("  n_tickers  : %d" % len(tickers))
    print("  n_dates    : %d" % len(score.index))

    # 2. Forward returns.
    if args.offline:
        # In offline mode we still call fetch_forward_returns; missing
        # cache files just yield NaN columns.
        class _NoFetchYahoo:
            @staticmethod
            def get_daily_prices(ticker, period="1y"):
                return []
        yahoo_module = _NoFetchYahoo
    else:
        yahoo_module = None

    fwd_returns = backtest.fetch_forward_returns(
        tickers=tickers,
        start_date=start,
        end_date=end,
        horizons_days=[1, 5, 21, 63] if horizon in {1, 5, 21, 63}
        else sorted({1, 5, 21, 63, horizon}),
        data_root=data_root,
        yahoo_module=yahoo_module,
    )
    headline_fwd = fwd_returns.get(horizon, pd.DataFrame())

    # 3. Portfolio P&L (headline horizon).
    portfolio = backtest.band_portfolio_returns(
        band_history=band,
        forward_returns=headline_fwd,
        bands_to_long=bands_long,
        bands_to_short=bands_short,
    )
    portfolio_json = backtest.serialize_portfolio_result(portfolio)
    backtest._atomic_write_json(out_root / "portfolio_returns.json", portfolio_json)

    # 4. Pillar IC.
    attribution = backtest.attribute_returns(
        score_history=score,
        pillar_histories=pillar_hist,
        forward_returns=headline_fwd,
    )
    backtest._atomic_write_json(
        out_root / "pillar_ic.json",
        attribution.to_dict(orient="records"),
    )

    # 5. Quintile returns per pillar.
    # On-disk shape: payload[pillar][quintile_label][date] = return.
    quintile_payload: Dict[str, Dict] = {}
    quintile_spread_by_pillar: Dict[str, List[float]] = {}
    for p in backtest.PILLAR_NAMES:
        ph = pillar_hist[p]
        q = backtest.pillar_quintile_returns(
            pillar_history=ph,
            forward_returns=headline_fwd,
            horizon_days=horizon,
        )
        # Build {quintile_label: {date_str: float}} so the JSON file is
        # human-readable per quintile.
        per_pillar: Dict[str, Dict[str, float]] = {}
        spreads: List[float] = []
        for label in q.index:
            per_pillar[str(label)] = {
                (c.strftime("%Y-%m-%d") if hasattr(c, "strftime") else str(c)): (
                    None if pd.isna(q.loc[label, c]) else float(q.loc[label, c])
                )
                for c in q.columns
            }
        # Collect Q5-Q1 spreads for the stdout summary.
        if "Q5-Q1" in q.index:
            for c in q.columns:
                v = q.loc["Q5-Q1", c]
                if pd.notna(v):
                    spreads.append(float(v))
        quintile_payload[p] = per_pillar
        quintile_spread_by_pillar[p] = spreads
    backtest._atomic_write_json(out_root / "quintile_returns.json", quintile_payload)

    # 6. Summary.
    summary = {
        "run_id": run_id,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "start": start,
        "end": end,
        "horizon_days": horizon,
        "bands_long": bands_long,
        "bands_short": bands_short,
        "n_tickers": len(tickers),
        "n_observation_dates": int(len(score.index)),
        "portfolio": {
            "cumulative_return": portfolio_json["cumulative_return"],
            "sharpe": portfolio_json["sharpe"],
            "max_drawdown": portfolio_json["max_drawdown"],
            "turnover_per_rebalance": portfolio_json["turnover_per_rebalance"],
            "n_rebalances": portfolio_json["n_rebalances"],
            "hit_rate": _hit_rate(portfolio["daily_returns"]),
            "n_long_avg": portfolio_json["n_long_avg"],
            "n_short_avg": portfolio_json["n_short_avg"],
        },
        "pillar_ic": attribution.to_dict(orient="records"),
    }
    backtest._atomic_write_json(out_root / "summary.json", summary)

    # 7. Stdout summary.
    print("")
    print("Portfolio (long %s, short %s, horizon=%dd):" %
          (bands_long, bands_short, horizon))
    print("  rebalances  : %d" % portfolio_json["n_rebalances"])
    print("  cum return  : %+.4f" % portfolio_json["cumulative_return"])
    print("  sharpe      : %+.3f" % portfolio_json["sharpe"])
    print("  max dd      : %+.4f" % portfolio_json["max_drawdown"])
    print("  hit rate    : %.3f" % summary["portfolio"]["hit_rate"])
    print("  avg n_long  : %.1f" % portfolio_json["n_long_avg"])
    print("  avg n_short : %.1f" % portfolio_json["n_short_avg"])

    print("")
    print("Pillar IC ranking (mean Spearman vs forward return):")
    print("  %-28s  %8s  %8s  %8s  %6s" % ("pillar", "ic_mean", "ic_std", "ic_sharpe", "n"))
    for row in attribution.to_dict(orient="records"):
        print("  %-28s  %+8.4f  %8.4f  %+8.3f  %6d" % (
            row["pillar"], row["ic_mean"], row["ic_std"],
            row["ic_sharpe"], row["n_obs"],
        ))

    print("")
    print("Quintile Q5-Q1 spread (mean across dates):")
    for p in backtest.PILLAR_NAMES:
        spreads = quintile_spread_by_pillar.get(p, [])
        if spreads:
            avg = sum(spreads) / len(spreads)
            print("  %-28s  %+8.4f  (n=%d)" % (p, avg, len(spreads)))
        else:
            print("  %-28s  %s  (n=0)" % (p, "n/a"))

    print("")
    print("Wrote artifacts to: %s" % out_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
