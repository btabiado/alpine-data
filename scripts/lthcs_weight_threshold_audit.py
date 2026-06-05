"""LTHCS per-cohort pillar-weight + band-threshold audit (READ-ONLY).

Phase 3 tasks 3.5 + 3.6 — swarm agent GG.

Outputs (all under ``data/lthcs/quality_audit/``):
  * ``<TODAY>_weights_vs_ic.md`` — current vs IC-implied weights, per cohort
  * ``<TODAY>_band_distribution.md`` — band counts + 30-day stability + verdict

Methodology
-----------
* For each cohort (= ``maturity_stage`` in the snapshot rows), collect every
  (date, ticker) observation across the daily equity snapshots in
  ``data/lthcs/snapshots/`` plus the daily crypto snapshots in
  ``data/lthcs/snapshots_crypto/``.
* For each pillar, compute the per-date Spearman rank-IC between the
  pillar sub-score and the **forward 21-trading-day return** for the
  cohort's tickers on that date. Aggregate to mean/std/Sharpe across
  dates. (Mirrors the global ``pillar_ic.json`` schema.)
* Translate IC Sharpes into an implied weight vector by clipping
  negatives to zero and normalising so the five pillars sum to 1. Compare
  side-by-side against ``data/lthcs/weights.json``.
* Verdict per cohort: ALIGNED if every pillar's current weight is within
  ``ALIGN_TOL`` (=0.10) of its IC-implied weight; otherwise MISALIGNED
  and the worst-mismatched pillar is flagged.

Band audit (sub-task 3.6) is independent — it just tallies how many
tickers sit in each band on the latest snapshot and across the trailing
30 daily snapshots (churn = fraction of consecutive-day band changes
per ticker), then writes a KEEP/SHIFT verdict for each band.

This script is **report-only**: it never modifies ``weights.json`` or
``lthcs/score.py``.
"""

from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO = Path(__file__).resolve().parent.parent
DATA = REPO / "data" / "lthcs"
SNAPSHOTS_EQ = DATA / "snapshots"
SNAPSHOTS_CRYPTO = DATA / "snapshots_crypto"
WEIGHTS_PATH = DATA / "weights.json"
OUT_DIR = DATA / "quality_audit"

# Prices live under the main repo's cache (worktrees share the same .cache).
# We resolve via the parent-of-parent-of-parent walk so this script works
# from both the worktree and the main repo.
def _resolve_price_cache() -> Path:
    candidates = [
        REPO / ".cache" / "lthcs" / "backtest" / "prices",
        REPO.parent / ".cache" / "lthcs" / "backtest" / "prices",
        Path("/Users/bryantabiadon/Documents/alpine-data/.cache/lthcs/backtest/prices"),
    ]
    for c in candidates:
        if c.exists():
            return c
    return candidates[-1]


PRICE_CACHE = _resolve_price_cache()

PILLARS = [
    "adoption_momentum",
    "institutional_confidence",
    "financial_evolution",
    "thesis_integrity",
    "des",
]

FORWARD_DAYS = 21
TODAY = "2026-05-19"
ALIGN_TOL = 0.10  # |current - implied| <= 0.10 => still "aligned"


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------

def jload(p: Path) -> Any:
    with open(p) as f:
        return json.load(f)


def load_snapshots(directory: Path) -> Dict[str, List[Dict[str, Any]]]:
    """Return ``{date_str: [score_row, ...]}`` from a snapshots directory."""
    out: Dict[str, List[Dict[str, Any]]] = {}
    if not directory.exists():
        return out
    for fp in sorted(directory.glob("*.json")):
        if fp.name == "index.json":
            continue
        d = jload(fp)
        date = d.get("calc_date") or fp.stem
        out[date] = d.get("scores") or []
    return out


def load_price_series(ticker: str) -> Dict[str, float]:
    """Return ``{date_str: adj_close}`` for ``ticker`` from the cache."""
    fp = PRICE_CACHE / f"{ticker}.json"
    if not fp.exists():
        return {}
    rows = jload(fp)
    out: Dict[str, float] = {}
    for r in rows or []:
        d = r.get("date")
        v = r.get("adj_close")
        if v is None:
            v = r.get("close")
        if d is None or v is None:
            continue
        try:
            out[str(d)] = float(v)
        except (TypeError, ValueError):
            continue
    return out


# ---------------------------------------------------------------------------
# Math helpers (avoid scipy)
# ---------------------------------------------------------------------------

def _rankdata(xs: List[float]) -> List[float]:
    """Average-rank vector (ties get the mean rank)."""
    pairs = sorted(enumerate(xs), key=lambda p: p[1])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(pairs):
        j = i
        while j + 1 < len(pairs) and pairs[j + 1][1] == pairs[i][1]:
            j += 1
        avg = (i + j) / 2.0 + 1.0  # ranks are 1-indexed
        for k in range(i, j + 1):
            ranks[pairs[k][0]] = avg
        i = j + 1
    return ranks


def spearman(xs: List[float], ys: List[float]) -> Optional[float]:
    if len(xs) != len(ys) or len(xs) < 3:
        return None
    rx = _rankdata(xs)
    ry = _rankdata(ys)
    n = len(rx)
    mean_x = sum(rx) / n
    mean_y = sum(ry) / n
    num = sum((rx[i] - mean_x) * (ry[i] - mean_y) for i in range(n))
    dx = math.sqrt(sum((rx[i] - mean_x) ** 2 for i in range(n)))
    dy = math.sqrt(sum((ry[i] - mean_y) ** 2 for i in range(n)))
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


def mean_std(xs: List[float]) -> Tuple[float, float]:
    if not xs:
        return (0.0, 0.0)
    m = sum(xs) / len(xs)
    if len(xs) < 2:
        return (m, 0.0)
    var = sum((x - m) ** 2 for x in xs) / (len(xs) - 1)
    return (m, math.sqrt(var))


def forward_return(prices: Dict[str, float], date: str, horizon: int) -> Optional[float]:
    """Forward return from ``date`` to ``date + horizon trading days``.

    ``horizon`` is in trading days; we use the price-series sorted index
    so weekends/holidays are skipped automatically.
    """
    if not prices:
        return None
    dates = sorted(prices.keys())
    if date not in prices:
        # find nearest >= date
        idx = next((i for i, d in enumerate(dates) if d >= date), None)
        if idx is None:
            return None
    else:
        idx = dates.index(date)
    fwd_idx = idx + horizon
    if fwd_idx >= len(dates):
        return None
    p0 = prices[dates[idx]]
    p1 = prices[dates[fwd_idx]]
    if p0 <= 0:
        return None
    return (p1 / p0) - 1.0


# ---------------------------------------------------------------------------
# Cohort IC computation
# ---------------------------------------------------------------------------

def compute_cohort_ic(
    snapshots: Dict[str, List[Dict[str, Any]]],
    horizon: int = FORWARD_DAYS,
) -> Dict[str, Dict[str, Dict[str, float]]]:
    """Per-cohort, per-pillar IC stats.

    Returns ``{cohort: {pillar: {ic_mean, ic_std, ic_sharpe, n_dates, n_obs}}}``.
    Bundle daily Spearman correlations across observation dates within the
    cohort then aggregate to mean/std.
    """
    # Pre-load price series for every ticker that appears.
    all_tickers: set = set()
    for rows in snapshots.values():
        for r in rows:
            all_tickers.add(r["ticker"])
    prices = {t: load_price_series(t) for t in sorted(all_tickers)}

    # cohort -> pillar -> list of daily IC values
    daily_ic: Dict[str, Dict[str, List[float]]] = defaultdict(lambda: defaultdict(list))
    # cohort -> count of (date, ticker) observations
    obs_counts: Dict[str, int] = defaultdict(int)
    # cohort -> set of dates contributing
    date_counts: Dict[str, set] = defaultdict(set)

    for date, rows in sorted(snapshots.items()):
        # Bucket rows by cohort
        by_cohort: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for r in rows:
            stage = r.get("maturity_stage") or "unknown"
            by_cohort[stage].append(r)

        for cohort, cohort_rows in by_cohort.items():
            # Forward returns for this date
            rets: List[float] = []
            keep_idx: List[int] = []
            for i, r in enumerate(cohort_rows):
                fr = forward_return(prices.get(r["ticker"], {}), date, horizon)
                if fr is None:
                    continue
                rets.append(fr)
                keep_idx.append(i)
            if len(rets) < 3:
                continue
            obs_counts[cohort] += len(rets)
            date_counts[cohort].add(date)
            for pillar in PILLARS:
                xs = [
                    cohort_rows[i].get("subscores", {}).get(pillar)
                    for i in keep_idx
                ]
                # Drop rows where the pillar score is missing or NaN.
                pairs = [
                    (xs[k], rets[k])
                    for k in range(len(xs))
                    if isinstance(xs[k], (int, float)) and not (
                        isinstance(xs[k], float) and math.isnan(xs[k])
                    )
                ]
                if len(pairs) < 3:
                    continue
                xv = [p[0] for p in pairs]
                yv = [p[1] for p in pairs]
                # Degenerate (all-equal) pillar values -> no signal that day
                if min(xv) == max(xv):
                    continue
                ic = spearman(xv, yv)
                if ic is None or math.isnan(ic):
                    continue
                daily_ic[cohort][pillar].append(ic)

    # Aggregate
    out: Dict[str, Dict[str, Dict[str, float]]] = {}
    for cohort, pillar_map in daily_ic.items():
        out[cohort] = {}
        for pillar, ics in pillar_map.items():
            m, s = mean_std(ics)
            sharpe = (m / s) if s > 0 else 0.0
            out[cohort][pillar] = {
                "ic_mean": m,
                "ic_std": s,
                "ic_sharpe": sharpe,
                "n_dates": len(ics),
            }
        out[cohort]["__meta__"] = {
            "n_obs": obs_counts[cohort],
            "n_dates": len(date_counts[cohort]),
        }
    return out


def implied_weights(ic_for_cohort: Dict[str, Dict[str, float]]) -> Dict[str, float]:
    """Sharpe-proportional implied weights (negatives clipped, then L1-normalised).

    Falls back to equal-weight if every IC Sharpe is non-positive.
    """
    raw: Dict[str, float] = {}
    for p in PILLARS:
        sub = ic_for_cohort.get(p) or {}
        s = sub.get("ic_sharpe", 0.0)
        raw[p] = max(0.0, s)
    tot = sum(raw.values())
    if tot <= 0:
        return {p: 1.0 / len(PILLARS) for p in PILLARS}
    return {p: raw[p] / tot for p in PILLARS}


# ---------------------------------------------------------------------------
# Band audit
# ---------------------------------------------------------------------------

def band_distribution(snapshots: Dict[str, List[Dict[str, Any]]], date: str) -> Counter:
    rows = snapshots.get(date, [])
    return Counter(r.get("band", "?") for r in rows)


def band_churn(
    snapshots: Dict[str, List[Dict[str, Any]]],
    n_days: int = 30,
) -> Dict[str, float]:
    """Per-ticker fraction of consecutive-day band changes in the trailing window."""
    dates = sorted(snapshots.keys())[-n_days:]
    history: Dict[str, List[str]] = defaultdict(list)
    for d in dates:
        for r in snapshots[d]:
            history[r["ticker"]].append(r.get("band", "?"))
    out: Dict[str, float] = {}
    for t, bands in history.items():
        if len(bands) < 2:
            continue
        changes = sum(1 for i in range(1, len(bands)) if bands[i] != bands[i - 1])
        out[t] = changes / (len(bands) - 1)
    return out


# ---------------------------------------------------------------------------
# Verdict helpers
# ---------------------------------------------------------------------------

def cohort_verdict(
    current: List[float],
    implied: Dict[str, float],
    tol: float = ALIGN_TOL,
) -> Tuple[str, Optional[str], float]:
    """Return (verdict, worst_pillar, worst_gap_abs)."""
    worst = ""
    worst_gap = 0.0
    for i, p in enumerate(PILLARS):
        gap = abs(current[i] - implied[p])
        if gap > worst_gap:
            worst_gap = gap
            worst = p
    verdict = "ALIGNED" if worst_gap <= tol else "MISALIGNED"
    return (verdict, worst or None, worst_gap)


def band_verdict(count: int, band_name: str) -> str:
    """KEEP/SHIFT per-band verdict (heuristic)."""
    if band_name == "elite" and count == 0:
        return "SHIFT-DOWN (elite empty — threshold may be too high)"
    if band_name == "review" and count > 30:
        return "SHIFT-UP (review overflowing — threshold may be too low)"
    if count == 0:
        return "EMPTY (consider widening adjacent bands)"
    return "KEEP"


# ---------------------------------------------------------------------------
# Markdown writers
# ---------------------------------------------------------------------------

def fmt_weights_md(
    cohort_ic: Dict[str, Dict[str, Dict[str, float]]],
    weights_cfg: Dict[str, Any],
    today: str,
) -> str:
    profiles = weights_cfg["profiles"]
    pillar_order = weights_cfg["pillar_order"]

    # 1. First pass — build summary + per-cohort bodies in parallel.
    summary_rows: List[Tuple[str, str, str, float]] = []
    body: List[str] = []

    for cohort_name in sorted(cohort_ic.keys()):
        if cohort_name not in profiles:
            continue
        ic = cohort_ic[cohort_name]
        meta = ic.get("__meta__", {})
        cw = profiles[cohort_name]
        imp = implied_weights(ic)
        verdict, worst, gap = cohort_verdict(cw, imp)
        summary_rows.append((cohort_name, verdict, worst or "—", gap))

        body.append(f"## {cohort_name}")
        body.append("")
        body.append(f"_n_obs={meta.get('n_obs', 0)}, n_dates={meta.get('n_dates', 0)}_")
        body.append("")
        body.append("| Pillar | Current | IC mean | IC std | IC Sharpe | n | Implied | Gap |")
        body.append("|---|---:|---:|---:|---:|---:|---:|---:|")
        for i, p in enumerate(pillar_order):
            sub = ic.get(p, {})
            gap_p = cw[i] - imp[p]
            body.append(
                f"| {p} | {cw[i]:.3f} | {sub.get('ic_mean', float('nan')):+.4f} | "
                f"{sub.get('ic_std', float('nan')):.4f} | "
                f"{sub.get('ic_sharpe', float('nan')):+.3f} | "
                f"{int(sub.get('n_dates', 0))} | {imp[p]:.3f} | {gap_p:+.3f} |"
            )
        body.append("")
        body.append(f"**Verdict:** **{verdict}**"
                    + (f" — worst gap on `{worst}` ({gap:+.3f})" if worst else ""))
        body.append("")

    # 2. Cohorts with no measurable IC.
    no_ic = sorted(c for c in profiles if c not in cohort_ic)
    if no_ic:
        body.append("## Cohorts with insufficient observations")
        body.append("")
        body.append(
            "These profiles exist in `weights.json` but the snapshot window "
            "did not yield enough (date, ticker) observations for the "
            f"{FORWARD_DAYS}-day forward-return horizon. They are skipped here:"
        )
        body.append("")
        for c in no_ic:
            body.append(f"- `{c}`")
        body.append("")
        body.append(
            "_Crypto cohorts (`btc`/`eth`/`sol`/`layer_1_alt`/`oracle_defi`/"
            "`layer_2`/`payments`/`meme`) only have 8 daily snapshots and "
            "no cached daily prices — re-audit after ~30 days of crypto "
            "snapshot accumulation._"
        )
        body.append("")

    # 3. Assemble final document with header + summary + bodies.
    out: List[str] = []
    out.append("# LTHCS per-cohort weight audit — current vs IC-implied")
    out.append("")
    out.append(f"**Generated:** {today}")
    out.append(f"**Horizon:** {FORWARD_DAYS}-trading-day forward return")
    out.append("**Method:** per-date Spearman rank-IC within cohort, "
               "Sharpe-aggregated across dates, implied weight ∝ max(0, IC_Sharpe).")
    out.append("")
    out.append("> **Critical caveat — Thesis Integrity.** Today's commit "
               "`10daa39` migrated Thesis sentiment from AV NEWS_SENTIMENT to "
               "Finnhub `/news-sentiment`. The snapshot data used in this audit "
               "is **pre-Finnhub** (V1 daily pipeline kept Thesis neutral at 50 "
               "for most tickers due to the AV multi-ticker AND-filter quirk). "
               "As a result, the Thesis IC measured below understates the "
               "framework's realised Thesis signal post-Finnhub. **Re-run this "
               "audit ~7 days after Finnhub data accumulates** (~2026-05-26); "
               "expect Thesis IC to rise and Thesis weights to need an upward "
               "bump in several cohorts.")
    out.append("")
    out.append("## Summary table")
    out.append("")
    out.append("| Cohort | Verdict | Worst pillar | Gap |")
    out.append("|---|---|---|---:|")
    for cohort, verdict, worst, gap in summary_rows:
        out.append(f"| {cohort} | {verdict} | {worst} | {gap:+.3f} |")
    out.append("")
    out.extend(body)
    return "\n".join(out)


def fmt_band_md(
    eq_snapshots: Dict[str, List[Dict[str, Any]]],
    crypto_snapshots: Dict[str, List[Dict[str, Any]]],
    weights_cfg: Dict[str, Any],
    today: str,
) -> str:
    bands_cfg = weights_cfg["score_bands"]
    band_order = ["elite", "high_confidence", "constructive", "monitor", "weakening", "review"]

    # Use latest eq snapshot date <= today
    eq_dates = sorted(eq_snapshots.keys())
    crypto_dates = sorted(crypto_snapshots.keys())
    latest_eq = eq_dates[-1] if eq_dates else None
    latest_crypto = crypto_dates[-1] if crypto_dates else None

    lines: List[str] = []
    lines.append("# LTHCS band-threshold audit")
    lines.append("")
    lines.append(f"**Generated:** {today}")
    lines.append(f"**Latest equity snapshot:** `{latest_eq}`")
    lines.append(f"**Latest crypto snapshot:** `{latest_crypto}`")
    lines.append("")
    lines.append("## Threshold configuration (from `data/lthcs/weights.json`)")
    lines.append("")
    lines.append("| Band | Range | Label |")
    lines.append("|---|---|---|")
    for b in band_order:
        spec = bands_cfg.get(b, {})
        lines.append(f"| {b} | {spec.get('min')}–{spec.get('max')} | {spec.get('label')} |")
    lines.append("")
    lines.append(
        "_Note: the task brief lists thresholds at 90/80/70/60/50/<50, but the "
        "live `weights.json` config has Elite at 85+ (not 90+). All counts below "
        "are computed against the **live config**._"
    )
    lines.append("")

    def _band_section(title: str, snapshots: Dict[str, List[Dict[str, Any]]], date: str):
        lines.append(f"## {title} — band distribution on {date}")
        lines.append("")
        counts = band_distribution(snapshots, date)
        total = sum(counts.values())
        lines.append("| Band | Count | Pct | Verdict |")
        lines.append("|---|---:|---:|---|")
        for b in band_order:
            c = counts.get(b, 0)
            pct = (100.0 * c / total) if total else 0.0
            lines.append(f"| {b} | {c} | {pct:.1f}% | {band_verdict(c, b)} |")
        lines.append(f"| **TOTAL** | **{total}** |  |  |")
        lines.append("")

    if latest_eq:
        _band_section("Equity universe", eq_snapshots, latest_eq)
    if latest_crypto:
        _band_section("Crypto universe", crypto_snapshots, latest_crypto)

    # 30-day stability — equity only (crypto has too little history)
    lines.append("## Stability (30-day band churn) — equity universe")
    lines.append("")
    churn = band_churn(eq_snapshots, n_days=30)
    if churn:
        vals = sorted(churn.values())
        n = len(vals)
        mean = sum(vals) / n
        med = vals[n // 2]
        p90 = vals[min(n - 1, int(0.9 * (n - 1)))]
        high_churn = [(t, c) for t, c in churn.items() if c >= 0.20]
        high_churn.sort(key=lambda x: -x[1])
        lines.append(f"- tickers with band data: **{n}**")
        lines.append(f"- mean churn rate: **{mean:.3f}** changes per consecutive-day pair")
        lines.append(f"- median churn rate: **{med:.3f}**")
        lines.append(f"- p90 churn rate: **{p90:.3f}**")
        lines.append(f"- tickers with churn ≥ 0.20 (= ~6 band-flips in 30 days): **{len(high_churn)}**")
        if high_churn:
            lines.append("")
            lines.append("Top 10 churners:")
            lines.append("")
            lines.append("| Ticker | Churn rate |")
            lines.append("|---|---:|")
            for t, c in high_churn[:10]:
                lines.append(f"| {t} | {c:.3f} |")
        if mean > 0.10:
            lines.append("")
            lines.append("**Verdict:** churn rate elevated — consider adding "
                         "**band-edge hysteresis** (e.g. require 2 consecutive "
                         "snapshots above/below a threshold before reclassifying).")
        else:
            lines.append("")
            lines.append("**Verdict:** churn rate acceptable; no hysteresis needed.")
    else:
        lines.append("_No churn data available._")
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(today: str = TODAY) -> Tuple[Path, Path]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    weights_cfg = jload(WEIGHTS_PATH)
    eq = load_snapshots(SNAPSHOTS_EQ)
    crypto = load_snapshots(SNAPSHOTS_CRYPTO)

    # Combine snapshots for IC math (price cache is keyed by ticker; if
    # crypto tickers have no cached prices their IC will simply not be
    # measurable, which is the truthful answer.)
    combined: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for d, rows in eq.items():
        combined[d].extend(rows)
    for d, rows in crypto.items():
        combined[d].extend(rows)

    cohort_ic = compute_cohort_ic(combined, horizon=FORWARD_DAYS)

    weights_md = fmt_weights_md(cohort_ic, weights_cfg, today)
    band_md = fmt_band_md(eq, crypto, weights_cfg, today)

    weights_path = OUT_DIR / f"{today}_weights_vs_ic.md"
    band_path = OUT_DIR / f"{today}_band_distribution.md"
    weights_path.write_text(weights_md)
    band_path.write_text(band_md)
    return weights_path, band_path


if __name__ == "__main__":
    wp, bp = run()
    print(f"wrote {wp}")
    print(f"wrote {bp}")
