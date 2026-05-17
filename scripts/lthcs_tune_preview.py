#!/usr/bin/env python3
"""LTHCS tuning preview — predict the effect of a config change WITHOUT writing.

Reads the latest snapshot from ``data/lthcs/snapshots/`` plus the relevant
config files, applies a proposed change IN MEMORY, recomputes composites,
and prints a before/after comparison. Never writes any file.

See ``docs/lthcs-tuning-kit.md`` for the symptom-to-lever decision table
and the version-bump playbook this preview is intended to support.

Usage examples
--------------

    # Soften a sector sensitivity
    python3 scripts/lthcs_tune_preview.py \\
        --sensitivity "Information Technology:fed_funds_pct=-0.10" \\
        --top 10

    # Redistribute weights for one maturity stage
    python3 scripts/lthcs_tune_preview.py \\
        --weights-profile "standard_compounder=0.25,0.20,0.15,0.25,0.15"

    # Add a ticker override
    python3 scripts/lthcs_tune_preview.py \\
        --ticker-override "PLTR:fed_funds_pct=-0.10"

    # Drop the DES magnitude scale
    python3 scripts/lthcs_tune_preview.py --magnitude-scale 20

    # Shift bands down 5 points
    python3 scripts/lthcs_tune_preview.py \\
        --band-thresholds "elite=85,high_confidence=75,constructive=65,monitor=55,weakening=45,review=0"

    # Combine: scope to a few tickers
    python3 scripts/lthcs_tune_preview.py \\
        --sensitivity "Information Technology:fed_funds_pct=-0.10" \\
        --tickers NVDA,MSFT,AVGO,MU,AAPL --top 50

Output is to stdout only. No files are modified. Pure stdlib (Python 3.9+).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Path discovery
# ---------------------------------------------------------------------------

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
SNAPSHOT_DIR = os.path.join(REPO_ROOT, "data", "lthcs", "snapshots")
VARIABLE_DETAIL_DIR = os.path.join(REPO_ROOT, "data", "lthcs", "variable_detail")
WEIGHTS_PATH = os.path.join(REPO_ROOT, "data", "lthcs", "weights.json")
SECTOR_DES_PATH = os.path.join(REPO_ROOT, "data", "lthcs", "sector_des_weights.json")


PILLAR_ORDER = (
    "adoption_momentum",
    "institutional_confidence",
    "financial_evolution",
    "thesis_integrity",
    "des",
)


def _latest_snapshot_path() -> str:
    if not os.path.isdir(SNAPSHOT_DIR):
        raise SystemExit("snapshot dir not found: %s" % SNAPSHOT_DIR)
    candidates = []
    for fn in os.listdir(SNAPSHOT_DIR):
        # Daily files look like YYYY-MM-DD.json — skip index.json and others.
        if not fn.endswith(".json") or fn == "index.json":
            continue
        base = fn[:-5]
        if len(base) == 10 and base[4] == "-" and base[7] == "-":
            candidates.append(fn)
    if not candidates:
        raise SystemExit("no dated snapshot files found in %s" % SNAPSHOT_DIR)
    candidates.sort()
    return os.path.join(SNAPSHOT_DIR, candidates[-1])


def _load_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# CLI parsing
# ---------------------------------------------------------------------------

def _parse_weights_profile(s: str) -> Tuple[str, List[float]]:
    if "=" not in s:
        raise argparse.ArgumentTypeError(
            "--weights-profile must look like stage=w1,w2,w3,w4,w5"
        )
    stage, vec_str = s.split("=", 1)
    stage = stage.strip()
    try:
        vec = [float(x) for x in vec_str.split(",")]
    except ValueError as e:
        raise argparse.ArgumentTypeError("could not parse weights: %s" % e)
    if len(vec) != len(PILLAR_ORDER):
        raise argparse.ArgumentTypeError(
            "weights vector must have %d elements (got %d)"
            % (len(PILLAR_ORDER), len(vec))
        )
    return stage, vec


def _parse_sensitivity(s: str) -> Tuple[str, str, float]:
    # Format: "Sector:signal=value"
    if ":" not in s or "=" not in s:
        raise argparse.ArgumentTypeError(
            'expected "Sector:signal=value" (e.g. "Technology:fed_funds_pct=-0.10")'
        )
    head, val_str = s.split("=", 1)
    sector, signal = head.split(":", 1)
    try:
        val = float(val_str)
    except ValueError:
        raise argparse.ArgumentTypeError("non-numeric sensitivity: %r" % val_str)
    return sector.strip(), signal.strip(), val


def _parse_ticker_override(s: str) -> Tuple[str, str, float]:
    if ":" not in s or "=" not in s:
        raise argparse.ArgumentTypeError(
            'expected "TICKER:signal=value" (e.g. "PLTR:fed_funds_pct=-0.10")'
        )
    head, val_str = s.split("=", 1)
    tkr, signal = head.split(":", 1)
    try:
        val = float(val_str)
    except ValueError:
        raise argparse.ArgumentTypeError("non-numeric override: %r" % val_str)
    return tkr.strip().upper(), signal.strip(), val


def _parse_band_thresholds(s: str) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for chunk in s.split(","):
        if "=" not in chunk:
            raise argparse.ArgumentTypeError(
                "--band-thresholds must look like band1=N,band2=N,..."
            )
        band, val = chunk.split("=", 1)
        try:
            out[band.strip()] = float(val)
        except ValueError:
            raise argparse.ArgumentTypeError("non-numeric band threshold: %r" % val)
    return out


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Preview the effect of an LTHCS tuning move WITHOUT writing any file. "
            "See docs/lthcs-tuning-kit.md for the playbook."
        )
    )
    p.add_argument(
        "--snapshot",
        help="Path to a snapshot JSON. Defaults to the latest in data/lthcs/snapshots/.",
    )
    p.add_argument(
        "--variable-detail",
        help=(
            "Path to the variable_detail JSON for the same date. Required for "
            "--sensitivity / --ticker-override / --magnitude-scale. Auto-discovered."
        ),
    )
    p.add_argument(
        "--weights-profile",
        action="append",
        type=_parse_weights_profile,
        default=[],
        metavar="STAGE=W1,W2,W3,W4,W5",
        help=(
            "Override the pillar weight vector for one maturity stage. May be "
            "passed multiple times. Vector must sum to 1.0 (warned otherwise)."
        ),
    )
    p.add_argument(
        "--sensitivity",
        action="append",
        type=_parse_sensitivity,
        default=[],
        metavar="SECTOR:SIGNAL=VALUE",
        help="Override a sector sensitivity for one macro signal. May repeat.",
    )
    p.add_argument(
        "--ticker-override",
        action="append",
        type=_parse_ticker_override,
        default=[],
        metavar="TICKER:SIGNAL=VALUE",
        help=(
            "Override a single ticker's sensitivity to one macro signal "
            "(adds to ticker_overrides in memory). May repeat."
        ),
    )
    p.add_argument(
        "--magnitude-scale",
        type=float,
        default=None,
        help="Override the DES magnitude_scale (default 30.0).",
    )
    p.add_argument(
        "--band-thresholds",
        type=_parse_band_thresholds,
        default=None,
        metavar="BAND=N,...",
        help=(
            "Override band MIN values. Example: "
            "elite=85,high_confidence=75,constructive=65,monitor=55,weakening=45,review=0"
        ),
    )
    p.add_argument(
        "--top",
        type=int,
        default=15,
        help="Show top N movers by |Δ composite| (default 15).",
    )
    p.add_argument(
        "--tickers",
        type=str,
        default=None,
        help="Comma-separated subset of tickers to show in the table.",
    )
    p.add_argument(
        "--min-delta",
        type=float,
        default=0.1,
        help="Suppress movers with |Δ| below this threshold (default 0.1).",
    )
    return p


# ---------------------------------------------------------------------------
# Score recomputation helpers
# ---------------------------------------------------------------------------

DEFAULT_MAGNITUDE_SCALE = 30.0


def _normalize_sector_name(name: Optional[str]) -> str:
    return (name or "").strip()


def _sensitivities_for_ticker(
    ticker: str,
    sector: str,
    sector_weights: Dict[str, Any],
) -> Dict[str, float]:
    """Return the effective per-signal sensitivities for one ticker.

    Follows the same merge order as ``lthcs/pillars/des.compute_des``:
    start from the sector block, then per-signal override from the
    ticker_overrides block.
    """
    sectors_block = sector_weights.get("sectors") or {}
    overrides_block = sector_weights.get("ticker_overrides") or {}
    sector_block = sectors_block.get(sector) or {}
    out: Dict[str, float] = {}
    for k, v in sector_block.items():
        if not isinstance(k, str) or k.startswith("_"):
            continue
        try:
            out[k] = float(v)
        except (TypeError, ValueError):
            continue
    ticker_block = overrides_block.get(ticker) if isinstance(overrides_block, dict) else None
    if isinstance(ticker_block, dict):
        for k, v in ticker_block.items():
            if not isinstance(k, str) or k.startswith("_"):
                continue
            try:
                out[k] = float(v)
            except (TypeError, ValueError):
                continue
    return out


def _recompute_des_for_ticker(
    ticker: str,
    sector: str,
    signal_tilts: Dict[str, float],
    sector_weights: Dict[str, Any],
    magnitude_scale: float,
) -> float:
    """Recompute one ticker's DES sub-score under modified sensitivities.

    ``signal_tilts`` are the BAKED tilts from the snapshot's variable_detail
    record (these depend only on macro inputs + signal_normalization, NEITHER
    of which the preview script tunes). The sensitivities are read fresh from
    the (in-memory-modified) ``sector_weights``. Formula matches
    ``lthcs/pillars/des.compute_des`` exactly.
    """
    sensitivities = _sensitivities_for_ticker(ticker, sector, sector_weights)
    total_contribution = 0.0
    for sig, sens in sensitivities.items():
        tilt = float(signal_tilts.get(sig, 0.0))
        total_contribution += sens * tilt
    raw = 50.0 + total_contribution * float(magnitude_scale)
    if raw < 0.0:
        raw = 0.0
    elif raw > 100.0:
        raw = 100.0
    return round(raw, 1)


def _assign_band(score: float, score_bands: Dict[str, Dict[str, Any]]) -> str:
    try:
        s = float(score)
    except (TypeError, ValueError):
        s = 0.0
    if s != s:
        s = 0.0
    s = max(0.0, min(100.0, s))
    floored = int(math.floor(s))
    for name, spec in (score_bands or {}).items():
        try:
            lo = int(float(spec["min"]))
            hi = int(float(spec["max"]))
        except (KeyError, TypeError, ValueError):
            continue
        if lo <= floored <= hi:
            return name
    return "review"


def _recompute_composite(
    record: Dict[str, Any],
    effective_weights: List[float],
    new_des: Optional[float],
) -> Tuple[float, List[float]]:
    """Recompute composite from sub-scores + given effective_weights.

    Keeps the modifiers (macro_adj, sector_adj, volatility_mod) from the
    original record — those are NOT tuned by the preview script.
    """
    subs = record.get("subscores") or {}
    weighted = []
    weighted_sum = 0.0
    for w, name in zip(effective_weights, PILLAR_ORDER):
        if name == "des" and new_des is not None:
            sub = float(new_des)
        else:
            sub = float(subs.get(name, 50.0))
        contrib = float(w) * sub
        weighted.append(contrib)
        weighted_sum += contrib
    modifiers = record.get("modifiers") or {}
    final = (
        weighted_sum
        + float(modifiers.get("macro_adj", 0.0))
        + float(modifiers.get("sector_adj", 0.0))
        + float(modifiers.get("volatility_mod", 0.0))
    )
    if final < 0.0:
        final = 0.0
    elif final > 100.0:
        final = 100.0
    return round(float(final), 1), weighted


# ---------------------------------------------------------------------------
# Renorm-aware effective weights (matches lthcs/score.py)
# ---------------------------------------------------------------------------

_FLAGS_TO_DROPPED_PILLAR = {"thesis_unavailable": "thesis_integrity"}


def _effective_weights_from_profile(
    documented: List[float],
    data_quality_flags: List[str],
) -> List[float]:
    """Renorm documented weights against data-quality-driven dropped pillars.

    Mirrors ``lthcs/score.compute_lthcs_score`` so a preview that changes a
    profile vector still respects the dropped-pillar renormalization the
    daily pipeline applies.
    """
    flags_set = set(data_quality_flags or [])
    dropped = {
        _FLAGS_TO_DROPPED_PILLAR[f]
        for f in flags_set
        if f in _FLAGS_TO_DROPPED_PILLAR
    }
    if not dropped or len(dropped) >= len(PILLAR_ORDER):
        return [float(w) for w in documented]
    retained_sum = sum(
        w for w, n in zip(documented, PILLAR_ORDER) if n not in dropped
    ) or 1.0
    out: List[float] = []
    for w, name in zip(documented, PILLAR_ORDER):
        if name in dropped:
            out.append(0.0)
        else:
            out.append(float(w) / retained_sum)
    return out


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------

def _load_variable_detail_index(
    path: Optional[str],
    snapshot_path: str,
) -> Dict[str, Dict[str, float]]:
    """Return ``{ticker: signal_tilts_dict}`` from the variable_detail file.

    Auto-discovers the variable_detail file for the same date as the snapshot
    when ``path`` is None. Returns an empty index if the file is missing —
    DES recomputation flags are then disabled gracefully (the script warns).
    """
    if path is None:
        # Derive date from snapshot filename.
        base = os.path.basename(snapshot_path)
        if base.endswith(".json"):
            cand = os.path.join(VARIABLE_DETAIL_DIR, base)
            if os.path.exists(cand):
                path = cand
    if path is None or not os.path.exists(path):
        return {}
    payload = _load_json(path)
    variables = (payload or {}).get("variables") or []
    index: Dict[str, Dict[str, float]] = {}
    for row in variables:
        if not isinstance(row, dict):
            continue
        if row.get("pillar") != "des":
            continue
        tkr = row.get("ticker")
        comps = (row.get("components") or {}).get("signal_tilts") or {}
        if not tkr or not isinstance(comps, dict):
            continue
        index[tkr] = {k: float(v) for k, v in comps.items() if isinstance(v, (int, float))}
    return index


def _apply_profile_overrides(
    weights_config: Dict[str, Any],
    profile_overrides: List[Tuple[str, List[float]]],
    warnings: List[str],
) -> Dict[str, Any]:
    out = json.loads(json.dumps(weights_config))  # deep copy
    profiles = out.setdefault("profiles", {})
    for stage, vec in profile_overrides:
        if stage not in profiles:
            warnings.append(
                "weights-profile: unknown stage %r (known: %s)"
                % (stage, ", ".join(sorted(profiles.keys())))
            )
            continue
        total = sum(vec)
        if abs(total - 1.0) > 1e-6:
            warnings.append(
                "weights-profile: %s sums to %.4f, not 1.0 — applying anyway"
                % (stage, total)
            )
        profiles[stage] = list(vec)
    return out


def _apply_sensitivity_overrides(
    sector_weights: Dict[str, Any],
    sensitivity_overrides: List[Tuple[str, str, float]],
    ticker_overrides: List[Tuple[str, str, float]],
    warnings: List[str],
) -> Dict[str, Any]:
    out = json.loads(json.dumps(sector_weights))  # deep copy
    sectors = out.setdefault("sectors", {})
    for sector, signal, val in sensitivity_overrides:
        block = sectors.get(sector)
        if block is None:
            # Try alias resolution (Technology → Information Technology and vice versa).
            warnings.append(
                "sensitivity: sector %r not in config — creating entry" % sector
            )
            block = {}
            sectors[sector] = block
        block[signal] = float(val)
    overrides = out.setdefault("ticker_overrides", {})
    for tkr, signal, val in ticker_overrides:
        block = overrides.setdefault(tkr, {})
        # Drop ``_alias_of`` / ``_comment`` style keys we don't want to clobber.
        if not isinstance(block, dict):
            block = {}
            overrides[tkr] = block
        block[signal] = float(val)
    return out


def _apply_band_overrides(
    weights_config: Dict[str, Any],
    band_overrides: Optional[Dict[str, float]],
    warnings: List[str],
) -> Dict[str, Any]:
    if not band_overrides:
        return weights_config
    out = json.loads(json.dumps(weights_config))
    bands = out.setdefault("score_bands", {})
    # Recompute max as next-band-min - 1, keeping the order stable.
    # We accept the user's mins directly and reconstruct maxes.
    ordered = sorted(band_overrides.items(), key=lambda kv: -kv[1])
    last_min: Optional[int] = None
    for name, lo in ordered:
        if name not in bands:
            warnings.append("band-thresholds: unknown band %r — adding" % name)
            bands[name] = {}
        bands[name]["min"] = int(lo)
        if last_min is None:
            bands[name]["max"] = 100
        else:
            bands[name]["max"] = int(last_min) - 1
        last_min = int(lo)
    return out


def _describe_changes(args: argparse.Namespace) -> List[str]:
    """Human-readable summary of the applied changes."""
    out: List[str] = []
    for stage, vec in args.weights_profile:
        out.append(
            "  - weights[%s]: %s"
            % (stage, ", ".join("%.3f" % x for x in vec))
        )
    for sector, signal, val in args.sensitivity:
        out.append("  - %s.%s: → %+.3f (override)" % (sector, signal, val))
    for tkr, signal, val in args.ticker_override:
        out.append("  - ticker_override[%s].%s: → %+.3f" % (tkr, signal, val))
    if args.magnitude_scale is not None:
        out.append("  - magnitude_scale: %.2f → %.2f"
                   % (DEFAULT_MAGNITUDE_SCALE, args.magnitude_scale))
    if args.band_thresholds:
        out.append("  - band_thresholds: " + ", ".join(
            "%s=%d" % (k, int(v)) for k, v in sorted(args.band_thresholds.items())
        ))
    return out


def _band_distribution(records: List[Dict[str, Any]], key: str) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for r in records:
        b = r.get(key) or "review"
        counts[b] = counts.get(b, 0) + 1
    return counts


_BAND_ORDER = ("elite", "high_confidence", "constructive", "monitor", "weakening", "review")


def _print_distribution_shift(
    before: Dict[str, int],
    after: Dict[str, int],
) -> None:
    print("\nDistribution shift:")
    extras = sorted((set(before) | set(after)) - set(_BAND_ORDER))
    keys = list(_BAND_ORDER) + extras
    for b in keys:
        b_before = before.get(b, 0)
        b_after = after.get(b, 0)
        if b_before == 0 and b_after == 0:
            continue
        delta = b_after - b_before
        print(
            "  %-17s %3d → %3d  (%+d)" % (b, b_before, b_after, delta)
        )


def _print_top_movers(
    rows: List[Dict[str, Any]],
    top_n: int,
    min_delta: float,
    ticker_filter: Optional[List[str]],
) -> None:
    if ticker_filter:
        sieve = set(t.upper() for t in ticker_filter)
        rows = [r for r in rows if r["ticker"].upper() in sieve]
    movers = [r for r in rows if abs(r["delta"]) >= min_delta]
    movers.sort(key=lambda r: abs(r["delta"]), reverse=True)
    movers = movers[:top_n]
    if not movers:
        print("\nNo movers above |Δ| >= %.2f." % min_delta)
        return
    print("\nTop movers (|Δ| >= %.2f):" % min_delta)
    print("  %-6s  %-6s  %-6s  %-7s  %s"
          % ("ticker", "before", "after", "Δ", "band-shift"))
    for r in movers:
        if r["band_before"] == r["band_after"]:
            band_col = "%s (no change)" % r["band_before"]
        else:
            band_col = "%s → %s" % (r["band_before"], r["band_after"])
        print(
            "  %-6s  %6.1f  %6.1f  %+6.1f   %s"
            % (r["ticker"], r["before"], r["after"], r["delta"], band_col)
        )


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_argparser().parse_args(argv)

    # --- Load base data ------------------------------------------------------
    snapshot_path = args.snapshot or _latest_snapshot_path()
    snapshot = _load_json(snapshot_path)
    if not isinstance(snapshot, dict) or "scores" not in snapshot:
        raise SystemExit("snapshot missing 'scores' key: %s" % snapshot_path)
    scores: List[Dict[str, Any]] = snapshot["scores"]
    weights_cfg = _load_json(WEIGHTS_PATH)
    sector_cfg = _load_json(SECTOR_DES_PATH)

    # Variable detail (for DES recomputation under modified sensitivities).
    var_index = _load_variable_detail_index(args.variable_detail, snapshot_path)
    needs_des_recompute = bool(
        args.sensitivity or args.ticker_override or args.magnitude_scale is not None
    )
    if needs_des_recompute and not var_index:
        print(
            "warning: sensitivity / ticker-override / magnitude-scale supplied but "
            "no variable_detail file found alongside the snapshot — DES sub-scores "
            "will NOT be recomputed (only weight/band changes will be applied).",
            file=sys.stderr,
        )

    # --- Apply overrides in memory ------------------------------------------
    warnings: List[str] = []
    new_weights_cfg = _apply_profile_overrides(
        weights_cfg, args.weights_profile, warnings
    )
    new_sector_cfg = _apply_sensitivity_overrides(
        sector_cfg, args.sensitivity, args.ticker_override, warnings
    )
    new_weights_cfg = _apply_band_overrides(
        new_weights_cfg, args.band_thresholds, warnings
    )
    magnitude_scale = (
        DEFAULT_MAGNITUDE_SCALE
        if args.magnitude_scale is None
        else float(args.magnitude_scale)
    )

    # --- Header --------------------------------------------------------------
    print(
        "Tuning preview — base snapshot: %s (%d tickers, model_version=%s)"
        % (snapshot.get("calc_date", "?"), len(scores), snapshot.get("model_version", "?"))
    )
    changes = _describe_changes(args)
    if changes:
        print("Applied changes:")
        for line in changes:
            print(line)
    else:
        print("No changes specified — output will be a no-op verification.")
    for w in warnings:
        print("warning: " + w, file=sys.stderr)

    # --- Recompute -----------------------------------------------------------
    new_score_bands = new_weights_cfg.get("score_bands", {})
    profiles = new_weights_cfg.get("profiles", {})

    movers: List[Dict[str, Any]] = []
    before_records: List[Dict[str, Any]] = []
    after_records: List[Dict[str, Any]] = []

    for rec in scores:
        tkr = rec.get("ticker")
        sector = _normalize_sector_name(rec.get("sector"))
        stage = rec.get("maturity_stage") or "standard_compounder"

        # New DES sub-score if applicable.
        if needs_des_recompute and tkr in var_index:
            new_des = _recompute_des_for_ticker(
                tkr, sector, var_index[tkr], new_sector_cfg, magnitude_scale
            )
        else:
            new_des = None

        # New documented + effective weights for this ticker.
        documented_new = profiles.get(stage)
        if documented_new is None:
            documented_new = rec.get("weights_used") or [0.2] * 5
        effective_new = _effective_weights_from_profile(
            list(documented_new), rec.get("data_quality_flags") or []
        )

        new_composite, _ = _recompute_composite(rec, effective_new, new_des)
        before_band = rec.get("band") or _assign_band(
            float(rec.get("lthcs_score") or 0.0), weights_cfg.get("score_bands", {})
        )
        after_band = _assign_band(new_composite, new_score_bands)

        before_score = float(rec.get("lthcs_score") or 0.0)
        delta = round(new_composite - before_score, 1)
        movers.append(
            {
                "ticker": tkr,
                "before": before_score,
                "after": new_composite,
                "delta": delta,
                "band_before": before_band,
                "band_after": after_band,
            }
        )
        before_records.append({"band": before_band})
        after_records.append({"band": after_band})

    # --- Output --------------------------------------------------------------
    ticker_filter = None
    if args.tickers:
        ticker_filter = [t.strip() for t in args.tickers.split(",") if t.strip()]
    _print_top_movers(movers, args.top, args.min_delta, ticker_filter)

    dist_before = _band_distribution(before_records, "band")
    dist_after = _band_distribution(after_records, "band")
    _print_distribution_shift(dist_before, dist_after)

    n_changed = sum(1 for r in movers if abs(r["delta"]) >= args.min_delta)
    n_band_shifts = sum(1 for r in movers if r["band_before"] != r["band_after"])
    print(
        "\nSummary: %d/%d tickers moved |Δ| >= %.2f; %d band shifts."
        % (n_changed, len(movers), args.min_delta, n_band_shifts)
    )
    print("\n(no files modified)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
