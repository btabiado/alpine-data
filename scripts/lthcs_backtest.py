#!/usr/bin/env python3
"""LTHCS backtest CLI.

Joins the LTHCS daily score history to Yahoo daily closes and emits:

  * portfolio_returns.json  — daily band-portfolio P&L (legacy validator)
  * pillar_ic.json          — Spearman IC per pillar
  * quintile_returns.json   — quintile spread per pillar
  * summary.json            — top-level Sharpe / drawdown / hit-rate
  * report.md               — human-readable markdown report
  * equity_curve.csv/.json  — non-overlapping engine P&L (Tier 5 #24)
  * positions_daily.csv     — daily portfolio composition (engine)
  * trades.csv              — entry/exit pairs (engine)
  * band_curves.json        — per-band sub-portfolio equity (engine)
  * engine_summary.json     — engine headline stats + run_meta
  * engine_report.md        — engine markdown report
  * stdout                  — concise summary table

Usage:
    python scripts/lthcs_backtest.py [--start YYYY-MM-DD] [--end YYYY-MM-DD]
                                     [--horizon 21]
                                     [--bands-long elite,high_confidence]
                                     [--bands-short review]
                                     [--output-dir data/lthcs/backtest/]
                                     [--run-id <id>]
                                     [--engine {ic,pnl,both}]
                                     [--cost-bps 5.0]
                                     [--benchmark SPY]
                                     [--offline]
                                     [--no-report]
                                     [--from-json <run-dir>]

``--engine``  ic  -> IC + quintile only (legacy validator).
              pnl -> engine non-overlapping P&L only.
              both (default) -> both sections written to the same dir.

``--offline`` skips Yahoo and uses the indefinite price cache only;
useful in CI / when no network is available.

``--from-json <run-dir>`` skips the full backtest and only regenerates
``report.md`` from the JSON artifacts already in that directory. Useful
to backfill a run whose original report.md wasn't emitted.
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
from lthcs import backtest_engine  # noqa: E402
from lthcs import backtest_engine_attribution  # noqa: E402
from lthcs import backtest_profiles  # noqa: E402


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


def _build_prices_panel(
    tickers: List[str],
    cache_root: Optional[Path] = None,
    yahoo_module=None,
    benchmark: Optional[str] = None,
) -> tuple:
    """Build a (date x ticker) close-price panel plus optional benchmark series.

    Reuses ``lthcs.backtest._fetch_prices`` and ``_prices_to_close_series``
    so the engine sees the same indefinite cache the IC validator uses.
    Returns ``(prices_df, benchmark_series_or_None)``.
    """
    cache = cache_root if cache_root is not None else backtest._default_cache_root()
    close_by_ticker: Dict[str, pd.Series] = {}
    for t in tickers:
        rows = backtest._fetch_prices(t, cache_root=cache, yahoo_module=yahoo_module)
        close_by_ticker[t] = backtest._prices_to_close_series(rows)

    bench_series: Optional[pd.Series] = None
    if benchmark:
        rows = backtest._fetch_prices(benchmark, cache_root=cache, yahoo_module=yahoo_module)
        bench_series = backtest._prices_to_close_series(rows)
        if bench_series.empty:
            bench_series = None

    prices_df = pd.DataFrame(close_by_ticker)
    if not prices_df.empty:
        prices_df.index = pd.to_datetime(prices_df.index)
        prices_df.sort_index(inplace=True)
    return prices_df, bench_series


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
    parser.add_argument("--no-report", action="store_true",
                        help="Skip writing report.md (JSON artifacts only).")
    parser.add_argument("--from-json", type=str, default=None,
                        help="Skip the backtest and only (re)generate "
                             "report.md from JSON artifacts in this dir.")
    parser.add_argument(
        "--engine",
        type=str,
        choices=["ic", "pnl", "both"],
        default="both",
        help="Which sections to compute: 'ic' (legacy IC + quintile + "
             "band portfolio), 'pnl' (Tier 5 #24 non-overlapping engine), "
             "or 'both' (default).",
    )
    parser.add_argument(
        "--cost-bps",
        type=float,
        default=5.0,
        help="Engine cost in bps per side (default 5.0 = ~10bps round-trip).",
    )
    parser.add_argument(
        "--benchmark",
        type=str,
        default="SPY",
        help="Engine benchmark ticker (default SPY). Empty string to skip.",
    )
    parser.add_argument(
        "--profile",
        type=str,
        default="long_only_buy",
        choices=backtest_profiles.available_profiles(),
        help="Phase 3 strategy profile (Tier 5 #24). Default keeps the "
             "Phase 1 long-only Buy-band behavior. Other profiles override "
             "--bands-long / --bands-short / --cost-bps from the profile's "
             "params; pass them explicitly if you want to vary them.",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=None,
        help="Override the top-K parameter for the 'top_k_by_composite' "
             "profile. Ignored by other profiles.",
    )
    parser.add_argument(
        "--attribute",
        action="store_true",
        help="Engine Phase 2: emit pillar_attribution.json. Re-runs the engine "
             "5x (once per pillar with that pillar's weight zeroed and the "
             "remaining four renormalized) and writes Δ-Sharpe / Δ-return / "
             "Δ-max-DD relative to the baseline run. Requires --engine in "
             "{pnl, both}.",
    )
    args = parser.parse_args(argv)

    # --from-json: backfill report.md for a run that already has JSON.
    if args.from_json:
        target = Path(args.from_json).resolve()
        if not target.exists():
            print("ERROR: --from-json path does not exist: %s" % target)
            return 1
        report_path = backtest.write_report_from_dir(target)
        print("Regenerated report at: %s" % report_path)
        return 0

    data_root = Path(args.data_root) if args.data_root else None
    bands_long = _parse_band_list(args.bands_long)
    bands_short = _parse_band_list(args.bands_short)
    horizon = int(args.horizon)
    run_id = args.run_id or _build_run_id()
    out_root = (REPO_ROOT / args.output_dir / run_id).resolve()
    out_root.mkdir(parents=True, exist_ok=True)
    # --engine pnl skips writing the legacy IC/quintile/portfolio files so the
    # daily engine cron can land cleanly into the existing validation dir
    # without overwriting the weekly IC validator's outputs.
    write_legacy = args.engine in ("ic", "both")
    write_engine = args.engine in ("pnl", "both")

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
    if write_legacy:
        backtest._atomic_write_json(out_root / "portfolio_returns.json", portfolio_json)

    # 4. Pillar IC.
    attribution = backtest.attribute_returns(
        score_history=score,
        pillar_histories=pillar_hist,
        forward_returns=headline_fwd,
    )
    if write_legacy:
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
    if write_legacy:
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
    if write_legacy:
        backtest._atomic_write_json(out_root / "summary.json", summary)

        # 6b. Markdown report (small, fast; never fail the whole run on render error).
        if not args.no_report:
            try:
                backtest.write_report(
                    out_root,
                    summary=summary,
                    quintile_payload=quintile_payload,
                )
            except Exception as exc:  # pragma: no cover — defensive
                print("WARN: failed to write report.md: %s" % exc)

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

    # ----------------------------------------------------------------------
    # 8. Engine block (Tier 5 #24, Phase 1). Runs when --engine in {pnl, both}.
    # ----------------------------------------------------------------------
    if write_engine:
        engine_universe = list(band.columns) if not band.empty else list(score.columns)
        bench_ticker = (args.benchmark or "").strip() or None
        if bench_ticker and bench_ticker in engine_universe:
            # If the benchmark is somehow in our LTHCS universe, fetch
            # separately too (the engine wants it as a stand-alone series).
            pass

        prices_df, bench_series = _build_prices_panel(
            tickers=engine_universe,
            yahoo_module=yahoo_module,
            benchmark=bench_ticker,
        )

        # Phase 3: load the named profile, then layer CLI overrides on top.
        profile_name = args.profile or "long_only_buy"
        if profile_name == "top_k_by_composite" and args.top_k is not None:
            profile = backtest_profiles.build_top_k_by_composite(k=int(args.top_k))
        else:
            profile = backtest_profiles.get_profile(profile_name)
        engine_params = profile.params
        # CLI's --cost-bps still wins if the user explicitly set it.
        if args.cost_bps is not None:
            engine_params.cost_bps = float(args.cost_bps)
        # Honor --bands-long override for the default profile only --
        # other profiles ship with deliberate band sets.
        if profile.name == "long_only_buy" and args.bands_long:
            engine_params.bands_long = bands_long

        score_history_panel = None
        if profile.requires_score_history:
            score_history_panel = backtest.load_score_history(data_root=data_root)

        engine_out = backtest_engine.run_backtest(
            band_history=band,
            prices=prices_df,
            params=engine_params,
            benchmark_prices=bench_series,
            per_band_sweep=True,
            score_history=score_history_panel,
        )

        backtest._atomic_write_json(
            out_root / "equity_curve.json", engine_out["equity_curve"]
        )
        backtest._atomic_write_json(
            out_root / "band_curves.json", engine_out["band_curves"]
        )
        backtest._atomic_write_json(
            out_root / "benchmark_curve.json", engine_out["benchmark_curve"]
        )
        backtest._atomic_write_json(
            out_root / "engine_summary.json",
            {
                "summary": engine_out["summary"],
                "run_meta": engine_out["run_meta"],
                "n_positions": engine_out["n_positions"],
                "turnover_per_day": engine_out["turnover_per_day"],
            },
        )
        backtest_engine.equity_curve_to_csv(
            engine_out["equity_curve"], out_root / "equity_curve.csv"
        )
        backtest_engine.positions_daily_to_csv(
            engine_out["positions_daily"], out_root / "positions_daily.csv"
        )
        backtest_engine.trades_to_csv(
            engine_out["trades"], out_root / "trades.csv"
        )

        if not args.no_report:
            try:
                md = backtest_engine.build_engine_report_markdown(
                    summary=engine_out["summary"],
                    run_meta=engine_out["run_meta"],
                    band_curves=engine_out["band_curves"],
                    benchmark_curve=engine_out["benchmark_curve"],
                )
                (out_root / "engine_report.md").write_text(md, encoding="utf-8")
            except Exception as exc:  # pragma: no cover — defensive
                print("WARN: failed to write engine_report.md: %s" % exc)

        # 8b. Engine Phase 2 — per-pillar attribution (Tier 5 #24).
        if args.attribute:
            try:
                root = data_root or backtest._default_data_root()
                snapshots_by_date = backtest._load_all_snapshots(root)
                # Load score_bands from weights.json (same file score.py uses).
                weights_path = root / "weights.json"
                if weights_path.exists():
                    weights_cfg = backtest._read_json(weights_path) or {}
                else:
                    weights_cfg = {}
                score_bands = weights_cfg.get("score_bands") or {}

                attribution = backtest_engine_attribution.run_attribution(
                    snapshots_by_date=snapshots_by_date,
                    prices=prices_df,
                    score_bands=score_bands,
                    params=engine_params,
                    benchmark_prices=bench_series,
                    baseline_band_history=band,
                )
                backtest._atomic_write_json(
                    out_root / "pillar_attribution.json", attribution
                )

                print("")
                print("Pillar attribution (Phase 2, Δ vs baseline):")
                print("  %-26s  %10s  %10s  %10s" % (
                    "pillar", "Δsharpe", "Δret", "Δmax_dd"))
                for p_name in backtest_engine_attribution.PILLARS:
                    entry = attribution["per_pillar"].get(p_name, {})
                    if entry.get("status") != "ok":
                        print("  %-26s  %10s  %10s  %10s" % (
                            p_name, "n/a", "n/a", "n/a"))
                        continue
                    ds = entry.get("delta_sharpe")
                    dr = entry.get("delta_total_return")
                    dd = entry.get("delta_max_drawdown")
                    print("  %-26s  %+10.3f  %+10.4f  %+10.4f" % (
                        p_name,
                        ds if ds is not None else float("nan"),
                        dr if dr is not None else float("nan"),
                        dd if dd is not None else float("nan"),
                    ))
                print("  note: %s" % attribution["note"])
            except Exception as exc:  # pragma: no cover — defensive
                print("WARN: pillar_attribution failed: %s" % exc)

        es = engine_out["summary"]
        print("")
        print("Engine [profile=%s] (non-overlapping P&L, long %s, "
              "short %s, top_k=%d, cost=%.1fbps/side):" % (
                  profile.name,
                  engine_params.bands_long or "n/a (top_k)",
                  engine_params.bands_short or (
                      "bottom_quintile" if engine_params.short_bottom_quintile else "none"
                  ),
                  int(engine_params.top_k),
                  float(engine_params.cost_bps),
              ))
        print("  trading days: %d" % int(es["n_trading_days"]))
        print("  total return: %+.4f" % float(es["total_return"]))
        print("  ann. return : %+.4f" % float(es["ann_return"]))
        print("  ann. sharpe : %+.3f" % float(es["sharpe"]))
        print("  ann. sortino: %+.3f" % float(es["sortino"]))
        print("  max drawdown: %+.4f" % float(es["max_drawdown"]))
        print("  hit rate    : %.3f" % float(es["hit_rate"]))
        print("  avg hold d  : %.1f" % float(es["avg_hold_days"]))
        print("  turnover/dy : %.4f" % float(es["turnover"]))
        print("  n_trades    : %d" % int(es["n_trades"]))
        print("  n_unique_tkr: %d" % int(es["n_unique_tkr"]))

    print("")
    print("Wrote artifacts to: %s" % out_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
