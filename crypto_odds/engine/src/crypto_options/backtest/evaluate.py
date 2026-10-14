"""Walk-forward scoring with a sealed final holdout (brief s.8).

Splits. Each cohort has one holdout start date, common to all assets so pooled comparisons stay aligned: the
date at `1 - holdout_fraction` of the shortest asset's eligible 30-day timeline. It is written once to
config/holdout_seal.json and never moves as new data arrives. Development origins whose label interval
[t, t+h] reaches the holdout start are purged. Holdout outcomes are not loaded or written unless
`final=True`, and every final opening is appended to config/holdout_ledger.json.

Scoring. All models in a comparison are scored on identical eligible observations (the intersection),
with per-model coverage reported separately. Uncertainty uses a circular block bootstrap over origin dates
with block length h (overlapping horizons), resampling targets and models jointly so differences are paired.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from ..config import PROJECT_ROOT

SEAL = PROJECT_ROOT / "config" / "holdout_seal.json"
LEDGER = PROJECT_ROOT / "config" / "holdout_ledger.json"


def holdout_start(cohort: str, eligible_dates_by_asset: dict, frac: float, prior_seals: dict | None = None) -> pd.Timestamp:
    """Return the sealed holdout start; create it only if this cohort has never been sealed anywhere."""
    seal = json.loads(SEAL.read_text()) if SEAL.exists() else {}
    if cohort in seal:
        return pd.Timestamp(seal[cohort]["holdout_start"])
    if prior_seals and cohort in prior_seals:
        raise RuntimeError(f"{SEAL.name} has no '{cohort}' entry but earlier runs sealed it at "
                           f"{prior_seals[cohort]}. Restore the seal file (it is committed to git); "
                           "a new seal would move the holdout.")
    spans = {a: d for a, d in eligible_dates_by_asset.items() if len(d)}
    shortest = min(spans, key=lambda a: spans[a].max() - spans[a].min())
    d = spans[shortest]
    start = (d.min() + (d.max() - d.min()) * (1 - frac)).normalize()
    seal[cohort] = {"holdout_start": start.isoformat(), "set_by_asset": shortest,
                    "eligible_span": [d.min().isoformat(), d.max().isoformat()], "fraction": frac,
                    "sealed_utc": datetime.now(timezone.utc).isoformat(),
                    "rule": "never moved; holdout scored only once, at final evaluation"}
    SEAL.write_text(json.dumps(seal, indent=2))
    return start


def split(df: pd.DataFrame, start: pd.Timestamp) -> pd.Series:
    """'dev' | 'purged' | 'holdout' per row (needs origin_date, outcome_date)."""
    out = np.where(df["origin_date"] >= start, "holdout",
                   np.where(df["outcome_date"] >= start, "purged", "dev"))
    return pd.Series(out, index=df.index)


def record_final_opening(note: str, allow_reopen: bool = False, meta: dict | None = None) -> int:
    """The holdout is scored once. Refused if a previous final evaluation completed (unless explicitly allowed,
    never from the CLI). An attempt that crashed before completing is logged but does not block a retry."""
    led = json.loads(LEDGER.read_text()) if LEDGER.exists() else []
    done = [e for e in led if e.get("completed_utc")]
    if done and not allow_reopen:
        raise RuntimeError(f"holdout already scored on {done[0]['completed_utc']}; refusing to score it again")
    meta = meta or {}
    if led and not allow_reopen:   # a retry after a crash must use exactly the code and config of the first attempt
        first = led[0]
        for k in ("code_sha256", "config_sha256"):
            if first.get(k) and meta.get(k) and first[k] != meta[k]:
                raise RuntimeError(f"retry refused: {k} differs from the first holdout attempt "
                                   f"({first[k][:12]} vs {meta[k][:12]}); holdout results may already have been seen")
    led.append({"opened_utc": datetime.now(timezone.utc).isoformat(), "note": note, "completed_utc": None, **meta})
    LEDGER.write_text(json.dumps(led, indent=2))
    return len(led) - 1


def record_final_completed(entry: int, run_id: str):
    led = json.loads(LEDGER.read_text())
    led[entry]["completed_utc"] = datetime.now(timezone.utc).isoformat()
    led[entry]["run_id"] = run_id
    LEDGER.write_text(json.dumps(led, indent=2))


def brier(p, y):
    return (p - y) ** 2


def logloss(p, y, clip):
    p = np.clip(p, clip, 1 - clip)
    return -(y * np.log(p) + (1 - y) * np.log(1 - p))


def _cbb_indices(n, L, B, rng):
    """Circular block bootstrap index matrix (B, n)."""
    nb = int(np.ceil(n / L))
    starts = rng.integers(0, n, size=(B, nb))
    idx = (starts[:, :, None] + np.arange(L)[None, None, :]) % n
    return idx.reshape(B, -1)[:, :n]


def score_group(g: pd.DataFrame, models: list[str], h: int, cfg: dict, rng,
                baselines: list[str] | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """g: rows for one (asset, cohort, h) with columns origin_date, target, y, and one column per model.
    Returns per-target scores and the across-target leaderboard (with paired bootstrap CIs)."""
    clip = cfg["logloss_clip"]
    ref = "climatology"
    dates = np.sort(g["origin_date"].unique())
    pos = {d: i for i, d in enumerate(dates)}
    g = g.assign(pos=g["origin_date"].map(pos))
    n = len(dates)
    idx = _cbb_indices(n, h, cfg["bootstrap_reps"], rng)
    # per-(target, date) Brier matrices for paired resampling
    targets = sorted(g["target"].unique())
    rows, per_t = [], {}
    period = ((g["origin_date"] - g["origin_date"].min()).dt.days // h)
    g = g.assign(period=period)
    for t in targets:
        gt = g[g["target"] == t].sort_values("pos")
        y = gt["y"].to_numpy()
        win = gt.groupby("period")["y"].agg(["max", "min"])
        indep_event_windows = int((win["max"] == 1).sum())
        indep_nonevent_windows = int((win["min"] == 0).sum())
        testable = min(indep_event_windows, indep_nonevent_windows) >= cfg["min_independent_events"]
        bs = {m: brier(gt[m].to_numpy(), y) for m in models}
        per_t[t] = (gt["pos"].to_numpy(), bs, testable)
        for m in models:
            p = gt[m].to_numpy()
            b = bs[m].mean()
            bref = bs[ref].mean()
            d = bs[m] - bs[ref]
            # align this target's rows to the full origin-date grid (targets can cover different dates)
            dfull = np.full(n, np.nan)
            dfull[gt["pos"].to_numpy()] = d
            boot = (np.array([np.nanmean(dfull[idx_b]) for idx_b in idx]) if (m != ref and testable)
                    else np.full(1, np.nan))
            # non-overlapping sensitivity: one score per phase offset using every h-th origin only
            nono = [np.mean(d[k::h]) for k in range(h) if len(d[k::h])]
            rows.append({"target": t, "event": gt["event"].iat[0] if "event" in gt else "", "model": m,
                         "n": len(y), "events": int(y.sum()), "base_rate": y.mean(),
                         "indep_event_windows": indep_event_windows, "testable": testable,
                         "brier": b, "bss_vs_climatology": 1 - b / bref if bref > 0 else np.nan,
                         "logloss": logloss(p, y, clip).mean(), "mean_forecast": p.mean(),
                         "dbrier_vs_clim": d.mean(),
                         "dbrier_ci_lo": np.percentile(boot, 2.5), "dbrier_ci_hi": np.percentile(boot, 97.5),
                         "dbrier_nonoverlap_min": float(np.min(nono)), "dbrier_nonoverlap_max": float(np.max(nono)),
                         "ci_reps": len(boot) if testable and m != ref else 0})
    per_target = pd.DataFrame(rows)

    # across testable targets: mean Brier per model, paired joint bootstrap
    lead = []
    tt = [t for t in targets if per_t[t][2]]
    if tt:
        mats = {}
        for m in models:
            M = np.full((len(tt), n), np.nan)
            for a, t in enumerate(tt):
                ppos, bs, _ = per_t[t]
                M[a, ppos] = bs[m]
            mats[m] = M
        base = np.nanmean(mats[ref])
        bl = [m for m in (baselines or []) if m in mats]
        best_bl = min(bl, key=lambda m: np.nanmean(mats[m])) if bl else ref
        # verdict stability: the same paired bootstrap under several further seeds
        extra = [_cbb_indices(n, h, cfg["bootstrap_reps"], np.random.default_rng([cfg["seed"], k, h, n]))
                 for k in range(1, cfg.get("verdict_seeds", 5))]
        for m in models:
            mb = np.nanmean(mats[m])
            D = mats[m] - mats[ref]
            boot = np.array([np.nanmean(D[:, ib]) for ib in idx]) if m != ref else np.zeros(1)
            bss_boot = 1 - (np.array([np.nanmean(mats[m][:, ib]) for ib in idx]) /
                            np.array([np.nanmean(mats[ref][:, ib]) for ib in idx])) if m != ref else np.zeros(1)
            offs = [np.nanmean(D[:, k::h]) for k in range(h)]
            verdicts = set()
            if m != ref:
                for ix in [idx] + extra:
                    bb = np.array([np.nanmean(D[:, ib]) for ib in ix])
                    lo_, hi_ = np.percentile(bb, [2.5, 97.5])
                    verdicts.add("better" if hi_ < 0 else ("worse" if lo_ > 0 else "nd"))
            lead.append({"model": m, "targets_scored": len(tt), "origins": n,
                         "approx_independent_origins": round(n / h, 1), "mean_brier": mb,
                         "bss_vs_climatology": 1 - mb / base, "bss_ci_lo": np.percentile(bss_boot, 2.5),
                         "bss_ci_hi": np.percentile(bss_boot, 97.5), "dbrier_vs_clim": np.nanmean(D),
                         "dbrier_ci_lo": np.percentile(boot, 2.5), "dbrier_ci_hi": np.percentile(boot, 97.5),
                         "nonoverlap_dbrier_min": float(np.nanmin(offs)), "nonoverlap_dbrier_max": float(np.nanmax(offs)),
                         "nonoverlap_share_better": float(np.mean(np.array(offs) < 0)),
                         "verdict_seed_stable": len(verdicts) <= 1,
                         "best_baseline": best_bl,
                         "dbrier_vs_best_baseline": float(np.nanmean(mats[m] - mats[best_bl])),
                         **_ci_vs(mats[m] - mats[best_bl], idx, m == best_bl),
                         "dbest_seed_stable": _seed_stable(mats[m] - mats[best_bl], [idx] + extra, m == best_bl),
                         "verdict_seeds": len(extra) + 1 if m != ref else 0,
                         "mean_logloss": per_target[(per_target["model"] == m) & per_target["target"].isin(tt)]["logloss"].mean()})
    return per_target, pd.DataFrame(lead)


def _ci_vs(D, idx, same: bool) -> dict:
    if same:
        return {"dbest_ci_lo": 0.0, "dbest_ci_hi": 0.0}
    b = np.array([np.nanmean(D[:, ib]) for ib in idx])
    return {"dbest_ci_lo": float(np.percentile(b, 2.5)), "dbest_ci_hi": float(np.percentile(b, 97.5))}


def _seed_stable(D, idx_list, same: bool) -> bool:
    if same:
        return True
    v = set()
    for ix in idx_list:
        b = np.array([np.nanmean(D[:, ib]) for ib in ix])
        lo, hi = np.percentile(b, [2.5, 97.5])
        v.add("better" if hi < 0 else ("worse" if lo > 0 else "nd"))
    return len(v) <= 1


def reliability(df: pd.DataFrame, models: list[str], bins: list[float], h: int, B: int, rng) -> pd.DataFrame:
    """Reliability table with circular-block-bootstrap intervals (blocks of h origin dates) per bin."""
    dates = np.sort(df["origin_date"].unique())
    pos = df["origin_date"].map({d: i for i, d in enumerate(dates)}).to_numpy()
    idx = _cbb_indices(len(dates), h, B, rng)
    out = []
    for m in models:
        b = pd.cut(df[m], bins=bins, include_lowest=True)
        codes = b.cat.codes.to_numpy()
        y = df["y"].to_numpy()
        for c, label in enumerate(b.cat.categories):
            sel = codes == c
            if not sel.any():
                continue
            n_by_date = np.bincount(pos[sel], minlength=len(dates)).astype(float)
            k_by_date = np.bincount(pos[sel], weights=y[sel], minlength=len(dates))
            nb, kb = n_by_date[idx].sum(axis=1), k_by_date[idx].sum(axis=1)
            freq = np.divide(kb, nb, out=np.full(len(nb), np.nan), where=nb > 0)
            out.append({"model": m, "bin": str(label), "mean_forecast": df.loc[sel, m].mean(),
                        "observed": y[sel].mean(), "n": int(sel.sum()),
                        "distinct_dates": int((n_by_date > 0).sum()),
                        "lo": np.nanpercentile(freq, 2.5), "hi": np.nanpercentile(freq, 97.5)})
    return pd.DataFrame(out)
