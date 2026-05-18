"""A/B compare the three Institutional-momentum cohort strategies.

Tier 3 #16 audit follow-up. The Institutional pillar's price-momentum
sub-score is, today, percentile-ranked against the FULL LTHCS universe.
That means a flat Tech name gets a high momentum percentile during a Tech
rally — even when its intra-sector rank is mediocre.

This script re-computes the Institutional sub-score under each of the
three available momentum strategies for every ticker in today's
snapshot, then surfaces the biggest deltas so the user can decide
whether to flip the default.

The three strategies (see :mod:`lthcs.pillars.institutional`):

* ``universe``        — current V1 default; rank vs full universe.
* ``sector_relative`` — rank vs same-sector tickers.
* ``compound``        — rank vs same-sector_group cohort (Tier 2 #7
                        compound key); fall back to sector_relative then
                        universe if the group is missing / too small.

Usage::

    .venv/bin/python scripts/lthcs_compare_momentum_strategies.py
    .venv/bin/python scripts/lthcs_compare_momentum_strategies.py \\
        --snapshot data/lthcs/snapshots/2026-05-17.json \\
        --output data/lthcs/momentum_strategy_compare_2026-05-17.json

Outputs:

* Stdout table of the 10 biggest deltas (universe vs sector_relative).
* JSON dump at ``--output`` (default
  ``data/lthcs/momentum_strategy_compare_<date>.json``) with the full
  per-ticker breakdown for downstream analysis.

This script is **read-only** — it never modifies the snapshot itself.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# Allow running from repo root without an editable install.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from lthcs.pillars import institutional  # noqa: E402


def _load_json(path: Path) -> Dict[str, Any]:
    with path.open() as f:
        return json.load(f)


def _resolve_latest_snapshot(snapshots_dir: Path) -> Path:
    """Pick the lexicographically-latest ``YYYY-MM-DD.json`` snapshot."""
    candidates = sorted(
        p for p in snapshots_dir.glob("*.json")
        if p.stem != "index"
    )
    if not candidates:
        raise FileNotFoundError(f"No snapshot files under {snapshots_dir}")
    return candidates[-1]


def _load_inputs(
    snapshot_path: Path,
    variable_detail_path: Path,
    universe_path: Path,
    peer_groups_path: Optional[Path],
) -> Dict[str, Any]:
    """Pull the bits this script needs from the existing artifacts."""
    snapshot = _load_json(snapshot_path)
    var_detail = _load_json(variable_detail_path)
    universe = _load_json(universe_path)
    peer_groups = _load_json(peer_groups_path) if peer_groups_path else None

    # Per-ticker sector from snapshot.scores (canonical source).
    sector_assignments: Dict[str, str] = {}
    for row in snapshot.get("scores", []):
        sym = row.get("ticker")
        sec = row.get("sector")
        if sym and sec:
            sector_assignments[sym] = sec
    # Fall back to universe for tickers not in scores.
    for row in universe.get("tickers", []):
        sym = row.get("ticker")
        sec = row.get("sector")
        if sym and sec and sym not in sector_assignments:
            sector_assignments[sym] = sec

    # Per-ticker raw momentum + the Institutional component dict from
    # variable_detail (so we can rebuild the cohort map and reuse the
    # insider/holdings/inst_holdings inputs).
    inst_rows: Dict[str, Dict[str, Any]] = {}
    for entry in var_detail.get("variables", []):
        if entry.get("pillar") != "institutional_confidence":
            continue
        sym = entry.get("ticker")
        if sym:
            inst_rows[sym] = entry

    universe_momentum: Dict[str, Optional[float]] = {
        sym: (entry.get("components") or {}).get("momentum_pct_90d")
        for sym, entry in inst_rows.items()
    }

    return {
        "snapshot": snapshot,
        "var_detail": var_detail,
        "sector_assignments": sector_assignments,
        "inst_rows": inst_rows,
        "universe_momentum": universe_momentum,
        "peer_groups_config": peer_groups,
    }


def _score_under_strategy(
    ticker: str,
    universe_momentum: Dict[str, Optional[float]],
    inst_row: Dict[str, Any],
    sector_assignments: Dict[str, str],
    peer_groups_config: Optional[Dict[str, Any]],
    strategy: str,
) -> Dict[str, Any]:
    """Re-run :func:`institutional.compute_institutional` under one strategy.

    Reuses the existing snapshot's insider / holdings / 13F inputs so the
    comparison isolates the cohort effect — only the momentum sub-score
    moves between strategies.
    """
    comps = inst_row.get("components") or {}
    insider = comps.get("insider") or {}
    holdings = comps.get("holdings") or {}
    # Strip the snapshot's pre-computed adjustment_pts — institutional.py
    # recomputes from the raw fields.
    insider_payload = {
        "regime": insider.get("regime"),
        "conviction_score": insider.get("conviction_score"),
        "cluster_buying": insider.get("cluster_buying"),
        "ceo_cfo_action": insider.get("ceo_cfo_action"),
    }
    holdings_payload = {
        "conviction_signal": holdings.get("conviction_signal"),
        "signal_score": holdings.get("signal_score"),
        "manager_count": holdings.get("manager_count"),
        "data_quality": holdings.get("data_quality"),
        "quarter_over_quarter": {
            "share_change_pct": holdings.get("share_change_pct"),
            "net_buyers": holdings.get("net_buyers"),
            "net_sellers": holdings.get("net_sellers"),
        },
    }
    # The snapshot doesn't surface 13F-change-qoq (V1 always stubbed),
    # so leave that None to match production behavior.
    return institutional.compute_institutional(
        ticker,
        comps.get("momentum_pct_90d"),
        universe_momentum,
        insider_data=insider_payload if insider.get("regime") is not None else None,
        holdings_data=(
            holdings_payload if holdings.get("conviction_signal") is not None else None
        ),
        momentum_strategy=strategy,
        ticker_sector=sector_assignments.get(ticker),
        sector_assignments=sector_assignments,
        peer_groups_config=peer_groups_config,
    )


def _build_comparison_table(
    inputs: Dict[str, Any],
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    universe_momentum: Dict[str, Optional[float]] = inputs["universe_momentum"]
    sector_assignments: Dict[str, str] = inputs["sector_assignments"]
    peer_groups_config = inputs["peer_groups_config"]
    inst_rows: Dict[str, Dict[str, Any]] = inputs["inst_rows"]

    for ticker, inst_row in inst_rows.items():
        try:
            u = _score_under_strategy(
                ticker, universe_momentum, inst_row,
                sector_assignments, peer_groups_config, "universe",
            )
            s = _score_under_strategy(
                ticker, universe_momentum, inst_row,
                sector_assignments, peer_groups_config, "sector_relative",
            )
            c = _score_under_strategy(
                ticker, universe_momentum, inst_row,
                sector_assignments, peer_groups_config, "compound",
            )
        except Exception as exc:
            rows.append({
                "ticker": ticker,
                "error": str(exc),
            })
            continue

        u_sub = u["sub_score"]
        s_sub = s["sub_score"]
        c_sub = c["sub_score"]
        u_mom = u["components"]["momentum_subscore"]
        s_mom = s["components"]["momentum_subscore"]
        c_mom = c["components"]["momentum_subscore"]

        rows.append({
            "ticker": ticker,
            "sector": sector_assignments.get(ticker, ""),
            "momentum_pct_90d": (inst_row.get("components") or {}).get(
                "momentum_pct_90d"
            ),
            "universe": {
                "sub_score": u_sub,
                "momentum_subscore": u_mom,
                "cohort_label": u["components"]["momentum_cohort_label"],
                "cohort_size": u["components"]["momentum_cohort_size"],
                "strategy_used": u["components"]["momentum_strategy_used"],
            },
            "sector_relative": {
                "sub_score": s_sub,
                "momentum_subscore": s_mom,
                "cohort_label": s["components"]["momentum_cohort_label"],
                "cohort_size": s["components"]["momentum_cohort_size"],
                "strategy_used": s["components"]["momentum_strategy_used"],
            },
            "compound": {
                "sub_score": c_sub,
                "momentum_subscore": c_mom,
                "cohort_label": c["components"]["momentum_cohort_label"],
                "cohort_size": c["components"]["momentum_cohort_size"],
                "strategy_used": c["components"]["momentum_strategy_used"],
            },
            "deltas": {
                "universe_vs_sector_relative": round(u_sub - s_sub, 2),
                "universe_vs_compound": round(u_sub - c_sub, 2),
                "sector_relative_vs_compound": round(s_sub - c_sub, 2),
            },
            "max_delta": round(
                max(
                    abs(u_sub - s_sub),
                    abs(u_sub - c_sub),
                    abs(s_sub - c_sub),
                ),
                2,
            ),
        })
    return rows


def _print_top_deltas(rows: List[Dict[str, Any]], top_n: int = 10) -> None:
    valid = [r for r in rows if "error" not in r]
    valid.sort(key=lambda r: r["max_delta"], reverse=True)
    print()
    print(
        f"Top {top_n} tickers where momentum strategy matters most "
        "(largest sub_score delta across strategies):"
    )
    print()
    header = (
        f"{'TICKER':<8} {'SECTOR':<24} {'MOM%':>8} "
        f"{'UNIV':>8} {'SECT':>8} {'CMPD':>8} "
        f"{'U-S':>8} {'U-C':>8} {'COHORT_LABEL':<30}"
    )
    print(header)
    print("-" * len(header))
    for r in valid[:top_n]:
        mom = r.get("momentum_pct_90d")
        mom_s = f"{mom * 100:+.1f}%" if isinstance(mom, (int, float)) else "n/a"
        print(
            f"{r['ticker']:<8} {r['sector'][:23]:<24} {mom_s:>8} "
            f"{r['universe']['sub_score']:>8.1f} "
            f"{r['sector_relative']['sub_score']:>8.1f} "
            f"{r['compound']['sub_score']:>8.1f} "
            f"{r['deltas']['universe_vs_sector_relative']:>+8.1f} "
            f"{r['deltas']['universe_vs_compound']:>+8.1f} "
            f"{r['compound']['cohort_label']:<30}"
        )

    # Summary stats.
    deltas_us = [
        abs(r["deltas"]["universe_vs_sector_relative"]) for r in valid
    ]
    deltas_uc = [abs(r["deltas"]["universe_vs_compound"]) for r in valid]
    print()
    print("Summary (absolute sub_score delta, all tickers):")
    print(
        f"  universe vs sector_relative:  "
        f"mean={sum(deltas_us) / len(deltas_us):.2f}  "
        f"max={max(deltas_us):.2f}  "
        f"n>2pts={sum(1 for d in deltas_us if d > 2)}/{len(deltas_us)}"
    )
    print(
        f"  universe vs compound:         "
        f"mean={sum(deltas_uc) / len(deltas_uc):.2f}  "
        f"max={max(deltas_uc):.2f}  "
        f"n>2pts={sum(1 for d in deltas_uc if d > 2)}/{len(deltas_uc)}"
    )


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--snapshot",
        type=Path,
        default=None,
        help="Path to snapshot JSON (default: latest in data/lthcs/snapshots/).",
    )
    parser.add_argument(
        "--variable-detail",
        type=Path,
        default=None,
        help="Path to matching variable_detail JSON "
        "(default: same date as snapshot under data/lthcs/variable_detail/).",
    )
    parser.add_argument(
        "--universe",
        type=Path,
        default=REPO_ROOT / "data" / "lthcs" / "universe.json",
        help="Path to universe.json (sector source).",
    )
    parser.add_argument(
        "--peer-groups",
        type=Path,
        default=REPO_ROOT / "data" / "lthcs" / "peer_groups.json",
        help="Path to peer_groups.json (optional — needed for 'compound' strategy).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output JSON path (default: data/lthcs/momentum_strategy_compare_"
        "<date>.json).",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=10,
        help="Number of biggest-delta tickers to surface to stdout.",
    )
    args = parser.parse_args(argv)

    snapshots_dir = REPO_ROOT / "data" / "lthcs" / "snapshots"
    var_dir = REPO_ROOT / "data" / "lthcs" / "variable_detail"

    snapshot_path = args.snapshot or _resolve_latest_snapshot(snapshots_dir)
    if args.variable_detail:
        var_path = args.variable_detail
    else:
        # Match by filename.
        var_path = var_dir / snapshot_path.name
    if not var_path.exists():
        print(
            f"variable_detail file not found: {var_path}\n"
            "Re-run with --variable-detail PATH or generate the file via the "
            "daily pipeline.",
            file=sys.stderr,
        )
        return 2

    peer_groups_path: Optional[Path] = (
        args.peer_groups if args.peer_groups and args.peer_groups.exists() else None
    )

    print(f"snapshot:       {snapshot_path}")
    print(f"variable_detail:{var_path}")
    print(f"universe:       {args.universe}")
    pg_label = (
        str(peer_groups_path) if peer_groups_path
        else "(missing — compound will fall back to sector_relative)"
    )
    print(f"peer_groups:    {pg_label}")

    inputs = _load_inputs(
        snapshot_path, var_path, args.universe, peer_groups_path,
    )
    rows = _build_comparison_table(inputs)

    _print_top_deltas(rows, top_n=args.top)

    # Resolve output path.
    output = args.output or (
        REPO_ROOT
        / "data"
        / "lthcs"
        / f"momentum_strategy_compare_{snapshot_path.stem}.json"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(
        {
            "snapshot_date": snapshot_path.stem,
            "snapshot_path": str(snapshot_path),
            "variable_detail_path": str(var_path),
            "rows": rows,
        },
        indent=2,
        sort_keys=False,
    ))
    print()
    print(f"Full A/B JSON written to: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
