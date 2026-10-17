"""Daily refresh for /crypto-odds/: snapshot Deribit option chains, fit market-implied curves, write odds.json.

Runs the vendored research engine (crypto_odds/engine, copied from Back_Test_Model at the commit in
engine/VENDORED_FROM). Exits non-zero, leaving the last-good odds.json in place, if no chain is usable.
"""
import json
import os
import sys
from pathlib import Path

for _v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
    os.environ.setdefault(_v, "1")
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "engine" / "src"))

from crypto_options import pipeline  # noqa: E402
from crypto_options.config import load_config  # noqa: E402
from crypto_options.reporting.web_export import export_odds  # noqa: E402


def main() -> int:
    cfg = load_config(HERE / "engine" / "config" / "config.yaml")
    sid = pipeline.run_snapshot(cfg, cfg["assets"])
    rid = pipeline.run_analyze(cfg, sid, make_charts=False)
    odds = export_odds(Path(cfg["paths"]["runs"]) / rid)
    odds["engine_revision"] = (HERE / "engine" / "VENDORED_FROM").read_text().strip()
    usable = [a["asset"] for a in odds["assets"] if a["status"] == "ok"]
    if not usable:
        print("[crypto-odds] no usable option chains; keeping last-good odds.json", file=sys.stderr)
        return 1
    (HERE / "odds.json").write_text(json.dumps(odds, separators=(",", ":")))
    print(f"[crypto-odds] wrote odds.json for {', '.join(usable)} as of {odds['as_of_utc']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
