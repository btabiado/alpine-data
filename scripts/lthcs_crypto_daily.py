#!/usr/bin/env python3
"""LTHCS crypto daily runner.

Standalone daily script that produces an LTHCS-shaped composite score
for each crypto asset in ``data/lthcs/crypto_universe.json``. Mirrors
the equity ``lthcs_daily.py`` but with crypto-native pillars.

Outputs:

* ``data/lthcs/snapshots_crypto/<date>.json`` -- daily composite scores
  (one row per asset). Schema matches the equity snapshot.
* ``data/lthcs/history/by_ticker/<SYMBOL>.json`` -- per-asset rolling
  history. Same schema as the equity per-ticker history, sharing the
  same directory because tickers are namespaced (BTC/ETH/SOL never
  collide with equity symbols in this repo's universe).

Default OFF in the equity pipeline; gated by ``LTHCS_CRYPTO_ENABLED=1``
at the lthcs_daily.py dispatch site. Can also be invoked directly::

    python scripts/lthcs_crypto_daily.py                # full run
    python scripts/lthcs_crypto_daily.py --dry-run      # no writes
    python scripts/lthcs_crypto_daily.py --symbols BTC  # subset
    python scripts/lthcs_crypto_daily.py --offline      # skip HTTP
    python scripts/lthcs_crypto_daily.py --force        # overwrite snap
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

# Make the project root importable when run as a script.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from lthcs import MODEL_VERSION  # noqa: E402
from lthcs.persist import LthcsPersist, _atomic_write_json  # noqa: E402
from lthcs.pillars.crypto_adoption import compute_crypto_adoption  # noqa: E402
from lthcs.pillars.crypto_des import compute_crypto_des  # noqa: E402
from lthcs.pillars.crypto_financial import compute_crypto_financial  # noqa: E402
from lthcs.pillars.crypto_institutional import (  # noqa: E402
    compute_crypto_institutional,
)
from lthcs.pillars.crypto_thesis import compute_crypto_thesis  # noqa: E402
from lthcs.score import (  # noqa: E402
    PILLAR_ORDER,
    assign_band,
    compute_drift,
    get_maturity_weights,
)
from lthcs.sources.crypto_data import CryptoDataAdapter  # noqa: E402


_DEFAULT_UNIVERSE = (
    _PROJECT_ROOT / "data" / "lthcs" / "crypto_universe.json"
)
_DEFAULT_WEIGHTS = _PROJECT_ROOT / "data" / "lthcs" / "weights.json"
_DEFAULT_SNAPSHOT_DIR = (
    _PROJECT_ROOT / "data" / "lthcs" / "snapshots_crypto"
)


# ---------------------------------------------------------------------------
# Args / IO
# ---------------------------------------------------------------------------

def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="LTHCS crypto daily runner (5 crypto pillars + composite)."
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute everything but skip persistence (no disk writes).",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing snapshot for today.",
    )
    ap.add_argument(
        "--symbols",
        default="",
        help="Comma-separated symbols to score (default: all active in universe).",
    )
    ap.add_argument(
        "--offline",
        action="store_true",
        help="Skip HTTP fetches; rely on cached / on-disk data only.",
    )
    ap.add_argument(
        "--calc-date",
        default=None,
        help="ISO date to label the run (default: today).",
    )
    ap.add_argument(
        "--universe",
        default=str(_DEFAULT_UNIVERSE),
        help="Path to the crypto universe JSON.",
    )
    ap.add_argument(
        "--weights",
        default=str(_DEFAULT_WEIGHTS),
        help="Path to the weights JSON.",
    )
    ap.add_argument(
        "--verbose",
        action="store_true",
        help="Extra logging.",
    )
    return ap.parse_args(argv)


def load_universe(path: Path) -> List[Dict[str, Any]]:
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    assets = data.get("assets")
    if not isinstance(assets, list):
        raise ValueError("crypto_universe.json missing 'assets' list")
    return [a for a in assets if isinstance(a, dict) and a.get("active", False)]


def load_weights_config(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# Per-asset scoring
# ---------------------------------------------------------------------------

def score_asset(
    asset: Dict[str, Any],
    adapter: CryptoDataAdapter,
    weights_config: Dict[str, Any],
    *,
    calc_date: str,
    prior_scores: Optional[Dict[str, Optional[float]]] = None,
) -> Dict[str, Any]:
    """Run all 5 crypto pillars + composite for one asset.

    Returns a snapshot row dict with the same shape as the equity
    snapshot.
    """
    symbol = str(asset["symbol"]).upper()
    profile = str(asset.get("weight_profile") or symbol.lower())

    inputs = adapter.inputs_for(symbol)

    pillars = {
        "adoption_momentum": compute_crypto_adoption(symbol, inputs),
        "institutional_confidence": compute_crypto_institutional(symbol, inputs),
        "financial_evolution": compute_crypto_financial(symbol, inputs),
        "thesis_integrity": compute_crypto_thesis(symbol, inputs),
        "des": compute_crypto_des(symbol, inputs),
    }
    subscores = {name: float(p["sub_score"]) for name, p in pillars.items()}

    documented_weights = get_maturity_weights(profile, weights_config)

    # Drop the Thesis pillar's weight when all three thesis components
    # are missing (the V1 default — funding rate / L-S ratio aren't
    # wired into a per-asset crypto field yet). Mirrors the equity
    # `thesis_unavailable` renorm path.
    data_quality_flags: List[str] = []
    thesis_dq = pillars["thesis_integrity"].get("data_quality") or {}
    if not any(thesis_dq.values()):
        data_quality_flags.append("thesis_unavailable")

    dropped = {"thesis_integrity"} if "thesis_unavailable" in data_quality_flags else set()
    if dropped and len(dropped) < len(PILLAR_ORDER):
        retained_sum = sum(
            w for w, n in zip(documented_weights, PILLAR_ORDER) if n not in dropped
        ) or 1.0
        effective_weights = [
            (0.0 if n in dropped else float(w) / retained_sum)
            for w, n in zip(documented_weights, PILLAR_ORDER)
        ]
    else:
        effective_weights = [float(w) for w in documented_weights]

    weighted_components: List[float] = []
    weighted_sum = 0.0
    for w, n in zip(effective_weights, PILLAR_ORDER):
        contrib = w * subscores[n]
        weighted_components.append(float(contrib))
        weighted_sum += contrib

    final = round(max(0.0, min(100.0, weighted_sum)), 1)
    band = assign_band(final, weights_config.get("score_bands", {}))
    drift = compute_drift(final, prior_scores or {})

    confidence_level = (
        "high" if not data_quality_flags else ("medium" if len(data_quality_flags) <= 2 else "low")
    )

    return {
        "ticker": symbol,
        "lthcs_score": final,
        "band": band,
        "drift_1d": drift["drift_1d"],
        "drift_7d": drift["drift_7d"],
        "drift_30d": drift["drift_30d"],
        "drift_90d": drift["drift_90d"],
        "confidence_level": confidence_level,
        "data_quality_flags": data_quality_flags,
        "subscores": subscores,
        "modifiers": {
            "macro_adj": 0.0,
            "sector_adj": 0.0,
            "volatility_mod": 0.0,
        },
        "maturity_stage": profile,
        "weights_used": [float(w) for w in documented_weights],
        "effective_weights": effective_weights,
        "dropped_pillars": sorted(dropped),
        "weighted_components": weighted_components,
        "sector": "Crypto",
        "asset_class": "crypto",
        "pillars_detail": {
            name: {
                "sub_score": p["sub_score"],
                "components": p.get("components", {}),
                "data_quality": p.get("data_quality", {}),
            }
            for name, p in pillars.items()
        },
    }


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def write_snapshot(
    snapshot_dir: Path,
    calc_date: str,
    rows: List[Dict[str, Any]],
    *,
    force: bool = False,
) -> Path:
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    path = snapshot_dir / ("%s.json" % calc_date)
    if path.exists() and not force:
        raise FileExistsError(
            "crypto snapshot for %s already exists at %s "
            "(pass --force to overwrite)" % (calc_date, path)
        )
    payload = {
        "calc_date": calc_date,
        "model_version": MODEL_VERSION,
        "asset_class": "crypto",
        "scores": rows,
    }
    _atomic_write_json(path, payload)
    return path


def load_prior_score(persist: LthcsPersist, symbol: str) -> Dict[str, Optional[float]]:
    """Read the most recent N entries of the per-ticker history and
    return a ``{"1d": prev, "7d": ..., "30d": ..., "90d": ...}`` map.

    The history JSON is sorted desc by date. Picks the closest entry to
    each lookback horizon (calendar-day approximation, since trading-
    days isn't meaningful for crypto).
    """
    out: Dict[str, Optional[float]] = {"1d": None, "7d": None, "30d": None, "90d": None}
    try:
        hist = persist.read_history(symbol).get("history") or []
    except Exception:
        return out
    if not hist:
        return out
    # Each entry: {"date", "score", "band"}. Use index offsets as a V1 proxy.
    horizons = {"1d": 1, "7d": 7, "30d": 30, "90d": 90}
    for label, offset in horizons.items():
        if len(hist) > offset:
            entry = hist[offset]
            try:
                out[label] = float(entry.get("score"))
            except (TypeError, ValueError):
                out[label] = None
    return out


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    calc_date = args.calc_date or date.today().isoformat()

    universe_path = Path(args.universe)
    weights_path = Path(args.weights)
    snapshot_dir = _DEFAULT_SNAPSHOT_DIR

    if not universe_path.exists():
        print("✗ crypto universe missing at %s" % universe_path, file=sys.stderr)
        return 2
    if not weights_path.exists():
        print("✗ weights config missing at %s" % weights_path, file=sys.stderr)
        return 2

    universe = load_universe(universe_path)
    weights_config = load_weights_config(weights_path)

    explicit_symbols = [s.strip().upper() for s in (args.symbols or "").split(",") if s.strip()]
    if explicit_symbols:
        universe = [a for a in universe if a["symbol"].upper() in explicit_symbols]
        if not universe:
            print(
                "✗ no active assets matched --symbols=%s"
                % args.symbols, file=sys.stderr,
            )
            return 2

    adapter = CryptoDataAdapter(offline=bool(args.offline))
    persist = LthcsPersist()

    rows: List[Dict[str, Any]] = []
    for asset in universe:
        symbol = str(asset["symbol"]).upper()
        prior = load_prior_score(persist, symbol)
        row = score_asset(
            asset, adapter, weights_config,
            calc_date=calc_date, prior_scores=prior,
        )
        rows.append(row)
        if args.verbose:
            print(
                "  %s -> %.1f (%s) %s"
                % (symbol, row["lthcs_score"], row["band"], row["subscores"])
            )

    if args.dry_run:
        print(
            "✓ crypto (dry-run): scored %d assets for %s"
            % (len(rows), calc_date)
        )
        return 0

    # Write snapshot.
    try:
        snap_path = write_snapshot(
            snapshot_dir, calc_date, rows, force=bool(args.force)
        )
    except FileExistsError as exc:
        print("✗ %s" % exc, file=sys.stderr)
        return 3

    # Append to per-ticker history (shared dir with equities; symbols
    # don't collide in this repo). Use the same MODEL_VERSION so the
    # rebuild_index reader sees a consistent stamp.
    for row in rows:
        persist.append_history_entry(
            row["ticker"],
            calc_date,
            row["lthcs_score"],
            row["band"],
            MODEL_VERSION,
        )

    print(
        "✓ crypto: wrote %d-asset snapshot to %s"
        % (len(rows), snap_path)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
