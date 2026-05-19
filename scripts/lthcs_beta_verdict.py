#!/usr/bin/env python3
"""LTHCS Adoption β-fix verdict report.

Compares post-β-fix IC (composite + per-pillar) against the frozen
pre-β baseline at ``data/lthcs/adaptive_weights/beta_fix_baseline_2026-05-18.json``
and emits a PASS / HOLD / FAIL verdict plus a markdown report.

Background: commit ``333e5dd`` (2026-05-18) raised ``_MIN_SECTOR_COHORT``
from 8 to 20 and softened mid-rank ties in ``lthcs/pillars/adoption.py``
to fix the 21d Adoption Q5-Q1 inversion (-1.4%, t = -4.97). The expected
IC delta is +0.02 to +0.03 at 21d. The IC re-measurement is calendar-gated
to ~30 trading days of forward data after the fix landed; earliest verdict
date is 2026-06-17.

Verdict rules (computed at horizon = 21 trading days):

    PASS  — composite IC delta >= +0.02 AND post-β Adoption IC >= 0
            AND n_obs >= 30 (the Adoption inversion has resolved).
    HOLD  — composite IC moves the right way but not enough, OR
            sample is too small (<30 obs), OR Adoption IC improves
            but is still <0.
    FAIL  — composite IC drops, OR Adoption inversion deepens
            (post Adoption IC < baseline Adoption IC).

Usage::

    python scripts/lthcs_beta_verdict.py
    python scripts/lthcs_beta_verdict.py --since 2026-05-18
    python scripts/lthcs_beta_verdict.py --since 2026-05-18 --output-dir /tmp

The script writes a markdown report
``data/lthcs/adaptive_weights/beta_fix_verdict_<today>.md`` and prints
the verdict + comparison table to stdout. Exit code is 0 on PASS/HOLD,
1 on FAIL, 2 on internal error (missing baseline, etc.).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pandas as pd  # noqa: E402

from lthcs import backtest  # noqa: E402

DEFAULT_HORIZON = 21
DEFAULT_BASELINE_PATH = (
    REPO_ROOT
    / "data"
    / "lthcs"
    / "adaptive_weights"
    / "beta_fix_baseline_2026-05-18.json"
)
DEFAULT_OUTPUT_DIR = REPO_ROOT / "data" / "lthcs" / "adaptive_weights"

# Verdict thresholds. Kept as module constants so tests can monkeypatch.
PASS_COMPOSITE_DELTA_MIN = 0.02
PASS_ADOPTION_IC_MIN = 0.0
MIN_OBS = 30


# ---------------------------------------------------------------------------
# Verdict logic — pure functions, no I/O, easy to unit-test
# ---------------------------------------------------------------------------

def classify_verdict(
    composite_post: Optional[float],
    composite_baseline: Optional[float],
    adoption_post: Optional[float],
    adoption_baseline: Optional[float],
    n_obs: int,
) -> Tuple[str, str]:
    """Return ``(verdict, reason)`` where verdict in {PASS, HOLD, FAIL}.

    Pure function; all numeric inputs may be ``None`` to indicate
    "not measurable". Missing post-period values collapse to HOLD;
    missing baseline values collapse to HOLD with an explanatory reason
    (we can't compare without a baseline).
    """
    if composite_baseline is None or adoption_baseline is None:
        return "HOLD", "baseline IC missing — cannot compare"
    if composite_post is None or adoption_post is None:
        return "HOLD", "post-β IC not computable (no forward returns yet)"

    composite_delta = composite_post - composite_baseline
    adoption_improved = adoption_post > adoption_baseline
    adoption_resolved = adoption_post >= PASS_ADOPTION_IC_MIN
    adoption_deepened = adoption_post < adoption_baseline

    # FAIL: composite IC dropped, OR Adoption inversion deepened.
    if composite_delta < 0.0:
        return (
            "FAIL",
            "composite IC dropped %+.4f (baseline %.4f -> post %.4f)" % (
                composite_delta, composite_baseline, composite_post,
            ),
        )
    if adoption_deepened:
        return (
            "FAIL",
            "Adoption inversion deepened (baseline %.4f -> post %.4f)" % (
                adoption_baseline, adoption_post,
            ),
        )

    # Sample size guard (after FAIL checks — a clear drop still fails).
    if n_obs < MIN_OBS:
        return (
            "HOLD",
            "insufficient sample (n_obs=%d < %d); window not mature" % (
                n_obs, MIN_OBS,
            ),
        )

    # PASS: composite IC up by threshold AND Adoption IC non-negative.
    if composite_delta >= PASS_COMPOSITE_DELTA_MIN and adoption_resolved:
        return (
            "PASS",
            "composite IC +%.4f (>= +%.4f) and Adoption IC %+.4f >= 0" % (
                composite_delta, PASS_COMPOSITE_DELTA_MIN, adoption_post,
            ),
        )

    # HOLD: directional improvement but not enough.
    bits: List[str] = []
    if composite_delta < PASS_COMPOSITE_DELTA_MIN:
        bits.append("composite IC +%.4f short of +%.4f" % (
            composite_delta, PASS_COMPOSITE_DELTA_MIN,
        ))
    if not adoption_resolved:
        if adoption_improved:
            bits.append("Adoption improved %+.4f -> %+.4f but still <0" % (
                adoption_baseline, adoption_post,
            ))
        else:
            bits.append("Adoption flat at %+.4f" % adoption_post)
    if not bits:
        bits.append("no movement")
    return "HOLD", "; ".join(bits)


# ---------------------------------------------------------------------------
# Baseline + IC plumbing
# ---------------------------------------------------------------------------

def load_baseline(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError("baseline not found at %s" % path)
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def extract_baseline_ic(
    baseline: Dict[str, Any], horizon: int = DEFAULT_HORIZON,
) -> Tuple[Optional[float], Optional[float]]:
    """Return ``(composite_ic_mean, adoption_ic_mean)`` from baseline JSON.

    Returns ``(None, None)`` if the requested horizon is absent.
    """
    horizon_key = "horizon_%dd" % int(horizon)
    composite_block = (baseline.get("composite_ic") or {}).get(horizon_key) or {}
    composite_ic = composite_block.get("ic_mean")

    pillar_block = (baseline.get("pillar_ic") or {}).get(horizon_key) or {}
    adoption_block = pillar_block.get("adoption_momentum") or {}
    adoption_ic = adoption_block.get("ic_mean")

    return (
        float(composite_ic) if composite_ic is not None else None,
        float(adoption_ic) if adoption_ic is not None else None,
    )


def compute_post_ic(
    since: str,
    end: str,
    horizon: int = DEFAULT_HORIZON,
    data_root: Optional[Path] = None,
    yahoo_module: Optional[Any] = None,
) -> Dict[str, Any]:
    """Compute composite + per-pillar IC over [since, end].

    Wraps ``lthcs.backtest.attribute_returns`` so the verdict script
    uses the exact same IC math as the weekly validator.

    Returns a dict shaped like::

        {
            "window": {"start": since, "end": end},
            "horizon_days": horizon,
            "n_obs": int,           # composite n_obs
            "composite_ic": float|None,
            "adoption_ic": float|None,
            "per_pillar": {pillar: {ic_mean, n_obs, ...}},
        }
    """
    score = backtest.load_score_history(data_root=data_root)
    pillar_hist: Dict[str, pd.DataFrame] = {}
    for p in backtest.PILLAR_NAMES:
        pillar_hist[p] = backtest.load_pillar_history(p, data_root=data_root)

    if score.empty:
        return {
            "window": {"start": since, "end": end},
            "horizon_days": int(horizon),
            "n_obs": 0,
            "composite_ic": None,
            "adoption_ic": None,
            "per_pillar": {},
            "note": "score history empty under %s" % (
                data_root or backtest._default_data_root()
            ),
        }

    # Filter the score frame to the requested observation window. We slice
    # the data here rather than at the SQL level because score is in-memory.
    since_ts = pd.Timestamp(since)
    end_ts = pd.Timestamp(end)
    score_slice = score.loc[(score.index >= since_ts) & (score.index <= end_ts)]
    pillar_slice = {
        p: ph.loc[(ph.index >= since_ts) & (ph.index <= end_ts)]
        for p, ph in pillar_hist.items()
    }

    tickers = list(score.columns)
    fwd_returns = backtest.fetch_forward_returns(
        tickers=tickers,
        start_date=since,
        end_date=end,
        horizons_days=[int(horizon)],
        data_root=data_root,
        yahoo_module=yahoo_module,
    )
    headline_fwd = fwd_returns.get(int(horizon), pd.DataFrame())

    attribution = backtest.attribute_returns(
        score_history=score_slice,
        pillar_histories=pillar_slice,
        forward_returns=headline_fwd,
    )
    per_pillar: Dict[str, Dict[str, Any]] = {}
    composite_ic: Optional[float] = None
    composite_n_obs = 0
    adoption_ic: Optional[float] = None
    for row in attribution.to_dict(orient="records"):
        per_pillar[row["pillar"]] = {
            "ic_mean": float(row["ic_mean"]),
            "ic_std": float(row["ic_std"]),
            "ic_sharpe": float(row["ic_sharpe"]),
            "n_obs": int(row["n_obs"]),
        }
        if row["pillar"] == "composite":
            composite_n_obs = int(row["n_obs"])
            composite_ic = (
                float(row["ic_mean"]) if composite_n_obs > 0 else None
            )
        elif row["pillar"] == "adoption_momentum":
            n = int(row["n_obs"])
            adoption_ic = float(row["ic_mean"]) if n > 0 else None

    return {
        "window": {"start": since, "end": end},
        "horizon_days": int(horizon),
        "n_obs": composite_n_obs,
        "composite_ic": composite_ic,
        "adoption_ic": adoption_ic,
        "per_pillar": per_pillar,
    }


# ---------------------------------------------------------------------------
# Markdown report
# ---------------------------------------------------------------------------

def _fmt_ic(v: Optional[float]) -> str:
    if v is None:
        return "n/a"
    try:
        return "%+0.4f" % float(v)
    except (TypeError, ValueError):
        return "n/a"


def _fmt_delta(post: Optional[float], baseline: Optional[float]) -> str:
    if post is None or baseline is None:
        return "n/a"
    try:
        return "%+0.4f" % (float(post) - float(baseline))
    except (TypeError, ValueError):
        return "n/a"


def render_markdown_report(
    *,
    today: str,
    verdict: str,
    reason: str,
    horizon: int,
    baseline: Dict[str, Any],
    post: Dict[str, Any],
) -> str:
    """Render the verdict markdown. Tolerates missing/NaN values.

    Pure (no I/O). Safe to call with empty baselines or all-None post
    metrics — used in tests to verify edge cases don't crash.
    """
    horizon_key = "horizon_%dd" % int(horizon)
    baseline_composite_block = (
        (baseline.get("composite_ic") or {}).get(horizon_key) or {}
    )
    baseline_pillar_block = (
        (baseline.get("pillar_ic") or {}).get(horizon_key) or {}
    )

    baseline_composite_ic = baseline_composite_block.get("ic_mean")
    baseline_adoption_ic = (
        (baseline_pillar_block.get("adoption_momentum") or {}).get("ic_mean")
    )

    post_per_pillar = post.get("per_pillar") or {}
    post_composite_ic = post.get("composite_ic")
    post_adoption_ic = post.get("adoption_ic")
    post_n_obs = post.get("n_obs", 0)

    meta = baseline.get("_meta") or {}

    lines: List[str] = []
    lines.append("# Adoption β-Fix Verdict — %s" % today)
    lines.append("")
    lines.append("**Verdict:** `%s`" % verdict)
    lines.append("")
    lines.append("**Reason:** %s" % reason)
    lines.append("")
    lines.append("**Anchor commit:** `%s` — %s" % (
        meta.get("anchor_commit", "n/a"),
        meta.get("anchor_commit_subject", "(β fix)"),
    ))
    lines.append("")
    lines.append("**Horizon:** %d trading days" % int(horizon))
    lines.append("")
    lines.append("**Post-β window:** %s → %s (n_obs=%s)" % (
        (post.get("window") or {}).get("start", "n/a"),
        (post.get("window") or {}).get("end", "n/a"),
        post_n_obs,
    ))
    lines.append("")

    # Comparison table.
    lines.append("## IC comparison")
    lines.append("")
    lines.append("| Metric | Baseline (pre-β) | Post-β | Δ |")
    lines.append("|---|---:|---:|---:|")
    lines.append("| Composite IC | %s | %s | %s |" % (
        _fmt_ic(baseline_composite_ic),
        _fmt_ic(post_composite_ic),
        _fmt_delta(post_composite_ic, baseline_composite_ic),
    ))
    lines.append("| Adoption IC | %s | %s | %s |" % (
        _fmt_ic(baseline_adoption_ic),
        _fmt_ic(post_adoption_ic),
        _fmt_delta(post_adoption_ic, baseline_adoption_ic),
    ))
    # Other pillars, for context. Skip composite (already shown above)
    # and adoption_momentum (already in the header rows).
    for pillar_name in (
        "institutional_confidence",
        "thesis_integrity",
        "financial_evolution",
        "des",
    ):
        bb = (baseline_pillar_block.get(pillar_name) or {}).get("ic_mean")
        pillar_post = post_per_pillar.get(pillar_name) or {}
        pp = pillar_post.get("ic_mean")
        # attribute_returns returns ic_mean=0.0 + n_obs=0 when a pillar
        # has no overlap with forward returns — collapse that to n/a so
        # the markdown isn't misleading.
        if int(pillar_post.get("n_obs", 0)) == 0:
            pp = None
        lines.append("| %s | %s | %s | %s |" % (
            pillar_name, _fmt_ic(bb), _fmt_ic(pp), _fmt_delta(pp, bb),
        ))
    lines.append("")

    # Verdict thresholds, for transparency.
    lines.append("## Thresholds")
    lines.append("")
    lines.append(
        "- **PASS**: composite IC Δ ≥ +%0.2f AND Adoption IC ≥ %0.2f AND "
        "n_obs ≥ %d" % (
            PASS_COMPOSITE_DELTA_MIN, PASS_ADOPTION_IC_MIN, MIN_OBS,
        )
    )
    lines.append(
        "- **HOLD**: directional improvement but short of threshold, "
        "or n_obs < %d." % MIN_OBS
    )
    lines.append(
        "- **FAIL**: composite IC drops, OR Adoption inversion deepens."
    )
    lines.append("")
    lines.append("Baseline frozen: %s" % meta.get("frozen_at", "n/a"))
    lines.append(
        "Source run: `%s` (window %s → %s)." % (
            meta.get("source_run", "n/a"),
            (meta.get("source_run_window") or {}).get("start", "n/a"),
            (meta.get("source_run_window") or {}).get("end", "n/a"),
        )
    )
    lines.append("")
    return "\n".join(lines)


def _write_report(out_path: Path, body: str) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(body, encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _default_since(today: date) -> str:
    return (today - timedelta(days=30)).strftime("%Y-%m-%d")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="LTHCS Adoption β-fix verdict report.",
    )
    parser.add_argument(
        "--since",
        type=str,
        default=None,
        help="Start of post-β window (YYYY-MM-DD). Default: 30d ago.",
    )
    parser.add_argument(
        "--end",
        type=str,
        default=None,
        help="End of post-β window (YYYY-MM-DD). Default: today.",
    )
    parser.add_argument(
        "--horizon",
        type=int,
        default=DEFAULT_HORIZON,
        help="Forward-return horizon in trading days. Default: 21.",
    )
    parser.add_argument(
        "--baseline",
        type=str,
        default=str(DEFAULT_BASELINE_PATH),
        help="Path to frozen baseline JSON.",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default=str(DEFAULT_OUTPUT_DIR),
        help="Directory for the markdown report.",
    )
    parser.add_argument(
        "--data-root",
        type=str,
        default=None,
        help="Override data/lthcs/ root (mostly for tests).",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Use cached prices only; never call Yahoo.",
    )
    parser.add_argument(
        "--json-only",
        action="store_true",
        help="Skip writing markdown; emit JSON verdict to stdout.",
    )
    args = parser.parse_args(argv)

    today = date.today()
    end = args.end or today.strftime("%Y-%m-%d")
    since = args.since or _default_since(today)

    try:
        baseline = load_baseline(Path(args.baseline))
    except FileNotFoundError as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 2

    composite_baseline, adoption_baseline = extract_baseline_ic(
        baseline, horizon=int(args.horizon)
    )

    if args.offline:
        class _NoFetchYahoo:
            @staticmethod
            def get_daily_prices(ticker, period="1y"):
                return []
        yahoo_module = _NoFetchYahoo
    else:
        yahoo_module = None

    data_root = Path(args.data_root) if args.data_root else None

    try:
        post = compute_post_ic(
            since=since,
            end=end,
            horizon=int(args.horizon),
            data_root=data_root,
            yahoo_module=yahoo_module,
        )
    except Exception as exc:  # pragma: no cover — defensive
        print("ERROR: failed to compute post-β IC: %s" % exc, file=sys.stderr)
        return 2

    verdict, reason = classify_verdict(
        composite_post=post.get("composite_ic"),
        composite_baseline=composite_baseline,
        adoption_post=post.get("adoption_ic"),
        adoption_baseline=adoption_baseline,
        n_obs=int(post.get("n_obs", 0)),
    )

    today_str = today.strftime("%Y-%m-%d")
    md = render_markdown_report(
        today=today_str,
        verdict=verdict,
        reason=reason,
        horizon=int(args.horizon),
        baseline=baseline,
        post=post,
    )

    out_dir = Path(args.output_dir)
    out_path = out_dir / ("beta_fix_verdict_%s.md" % today_str)
    if not args.json_only:
        _write_report(out_path, md)

    # Stdout summary.
    print("LTHCS β-fix verdict — %s" % today_str)
    print("  window      : %s → %s" % (since, end))
    print("  horizon     : %d trading days" % int(args.horizon))
    print("  n_obs       : %d" % int(post.get("n_obs", 0)))
    print("  composite IC: baseline=%s post=%s delta=%s" % (
        _fmt_ic(composite_baseline),
        _fmt_ic(post.get("composite_ic")),
        _fmt_delta(post.get("composite_ic"), composite_baseline),
    ))
    print("  adoption  IC: baseline=%s post=%s delta=%s" % (
        _fmt_ic(adoption_baseline),
        _fmt_ic(post.get("adoption_ic")),
        _fmt_delta(post.get("adoption_ic"), adoption_baseline),
    ))
    print("  verdict     : %s" % verdict)
    print("  reason      : %s" % reason)
    if not args.json_only:
        print("  report      : %s" % out_path)

    if args.json_only:
        payload = {
            "generated_at": datetime.utcnow().isoformat() + "Z",
            "today": today_str,
            "window": {"start": since, "end": end},
            "horizon_days": int(args.horizon),
            "n_obs": int(post.get("n_obs", 0)),
            "baseline": {
                "composite_ic": composite_baseline,
                "adoption_ic": adoption_baseline,
            },
            "post": {
                "composite_ic": post.get("composite_ic"),
                "adoption_ic": post.get("adoption_ic"),
                "per_pillar": post.get("per_pillar", {}),
            },
            "verdict": verdict,
            "reason": reason,
        }
        print("---JSON---")
        print(json.dumps(payload, indent=2))

    return 1 if verdict == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
