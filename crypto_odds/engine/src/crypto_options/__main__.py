"""CLI: python -m crypto_options {discover,snapshot,analyze,report}."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# single-threaded linear algebra so identical inputs reproduce identical floating-point outputs
for _v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
    os.environ.setdefault(_v, "1")

from .config import load_config


def main(argv=None):
    ap = argparse.ArgumentParser(prog="crypto_options")
    ap.add_argument("--config", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("discover", "snapshot"):
        p = sub.add_parser(name)
        p.add_argument("--assets", nargs="+", default=None)
    p = sub.add_parser("analyze")
    p.add_argument("--snapshot-id", default=None)
    p.add_argument("--fresh", action="store_true", help="take a new snapshot first")
    p.add_argument("--no-charts", action="store_true")
    p = sub.add_parser("report")
    p.add_argument("--run-id", required=True)
    p = sub.add_parser("history", help="refresh free settlement/spot/DVOL history")
    p.add_argument("--assets", nargs="+", default=None)
    p = sub.add_parser("backtest", help="walk-forward evaluation (development period; holdout sealed)")
    p.add_argument("--refresh", action="store_true", help="refresh history first")
    p.add_argument("--no-charts", action="store_true")
    p.add_argument("--open-final-holdout", default=None, metavar="CONFIRM",
                   help="score the sealed holdout; pass the literal text OPEN-ONCE. Logged permanently.")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    from . import pipeline

    if args.cmd == "discover":
        print(json.dumps(pipeline.run_discover(cfg, args.assets or cfg["assets"]), indent=2))
    elif args.cmd == "snapshot":
        sid = pipeline.run_snapshot(cfg, args.assets or cfg["assets"])
        mani = json.loads((Path(cfg["paths"]["normalized"]) / sid / "snapshot_manifest.json").read_text())
        for c in mani["chains"]:
            print(f"{c['asset']:5} {c.get('family') or '':8} {c['status']:12} "
                  f"accepted={c.get('n_accepted_otm', '-')} {c.get('error') or ''}")
        print(f"snapshot {sid}")
        if any(c["status"] == "failed" for c in mani["chains"]):
            return 2  # partial failure: other assets still saved
    elif args.cmd == "analyze":
        sid = pipeline.run_snapshot(cfg, cfg["assets"]) if args.fresh else args.snapshot_id
        rid = pipeline.run_analyze(cfg, sid, make_charts=not args.no_charts)
        print(f"run {rid}\nreport: {Path(cfg['paths']['runs']) / rid / 'report.md'}")
    elif args.cmd in ("history", "backtest"):
        from .backtest.run import fetch_history, run_backtest
        if args.cmd == "history" or args.refresh:
            st = fetch_history(cfg, getattr(args, "assets", None) or cfg["assets"])
            for k, v in st.items():
                print(f"{k:18} {v['status']:7} {v.get('rows', '')} {v.get('start', '') or ''} -> {v.get('end', '') or ''}")
        if args.cmd == "backtest":
            final = args.open_final_holdout == "OPEN-ONCE"
            if args.open_final_holdout is not None and not final:
                print("refusing: --open-final-holdout requires the literal text OPEN-ONCE")
                return 2
            rid = run_backtest(cfg, final=final, make_charts=not args.no_charts)
            print(f"backtest run backtest_{rid}\nreport: {Path(cfg['paths']['runs']) / ('backtest_' + rid) / 'report.md'}")
    elif args.cmd == "report":
        from .reporting import report
        print(report.write_report(Path(cfg["paths"]["runs"]) / args.run_id))
    return 0


if __name__ == "__main__":
    sys.exit(main())
