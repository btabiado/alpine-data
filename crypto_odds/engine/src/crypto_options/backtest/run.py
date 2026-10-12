"""Stage 3 runner: history -> forecasts -> sealed split -> dev-period scores -> report."""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd

from ..adapters import history as H
from ..adapters.http import RawArchive
from ..pipeline import _code_hash, _git_rev, _sha256, _versions, write_json
from ..quality.normalize import utc_now
from . import evaluate as E
from . import ml as ML
from . import models as M
from .features import origin_features, row_features
from .panel import events, load_series


def fetch_history(cfg, assets) -> dict:
    """Refresh all free history series; per-series failures are recorded, not fatal."""
    archive = RawArchive(Path(cfg["paths"]["raw"]))
    out = Path(cfg["paths"]["history"])
    out.mkdir(parents=True, exist_ok=True)
    status = {}
    for a in assets:
        for name, fn in (("settlement", H.settlement_prices), ("spot", H.spot_closes), ("dvol", H.dvol)):
            try:
                df = fn(a, archive)
                if len(df):
                    df.to_parquet(out / f"{name}_{a}.parquet", index=False)
                status[f"{name}_{a}"] = {"rows": len(df), "start": str(df["date"].min()) if len(df) else None,
                                         "end": str(df["date"].max()) if len(df) else None, "status": "ok" if len(df) else "none"}
            except Exception as exc:  # keep other assets going
                status[f"{name}_{a}"] = {"status": "failed", "error": str(exc)[:300]}
    write_json(out / "history_manifest.json", {"fetched_utc": utc_now(), "series": status,
                                               "raw_files": len(archive.records)})
    return status


def gap_report(s, cohort) -> dict:
    d = s.dates
    gaps = np.flatnonzero((d[1:] - d[:-1]) > pd.Timedelta(days=1))
    return {"asset": s.asset, "cohort": cohort, "source": s.source, "start": d.min().date().isoformat(),
            "end": d.max().date().isoformat(), "days": len(s), "gaps": len(gaps),
            "gap_list": "; ".join(f"{d[i].date()}→{d[i + 1].date()}" for i in gaps[:5]) + (" …" if len(gaps) > 5 else ""),
            "methodology_break": s.excluded_outcome_from.date().isoformat() if s.excluded_outcome_from is not None else ""}


def run_ml(tasks: list, workers: int) -> tuple[pd.DataFrame, list]:
    """Run walk-forward ML tasks in parallel processes (each single-threaded, so results are deterministic)."""
    from concurrent.futures import ProcessPoolExecutor

    order = sorted(range(len(tasks)), key=lambda i: (tasks[i]["model"] != "rf_A", -len(tasks[i]["rows"])))
    outs, logs = [], []
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for res in pool.map(ML.walk_forward, [tasks[i] for i in order]):
            logs.extend(res.attrs.get("log", []))
            outs.append(res)
    preds = [o.assign(asset=tasks[i]["asset"], cohort=tasks[i]["cohort"], h=tasks[i]["h"])
             for o, i in zip(outs, order) if len(o)]
    return (pd.concat(preds, ignore_index=True) if preds else pd.DataFrame()), logs


def run_backtest(cfg, final: bool = False, make_charts: bool = True, ml_on: bool = True) -> str:
    t0, c0 = time.time(), time.process_time()
    bt = cfg["backtest"]
    targets = bt["targets_up"] + bt["targets_down"]
    rng = np.random.default_rng(bt["seed"])
    ledger_entry = E.record_final_opening("final evaluation via run_backtest(final=True)") if final else None

    all_rows, gaps, seals, coverage, garch_log = [], [], {}, [], []
    tasks, series, ml_log = [], {}, []
    prior = {}
    for m in Path(cfg["paths"]["runs"]).glob("backtest_*/manifest.json"):
        prior.update(json.loads(m.read_text()).get("holdout_seal", {}))
    for cohort in bt["cohorts"]:
        per_asset, eligible = {}, {}
        for a in cfg["assets"]:
            s = load_series(cfg, a, cohort)
            if s is None:
                continue
            gaps.append(gap_report(s, cohort))
            dv_path = Path(cfg["paths"]["history"]) / f"dvol_{a}.parquet"
            dv = pd.read_parquet(dv_path) if dv_path.exists() else None
            f = M.forecast(s, bt["horizons"], targets, bt, dv)
            garch_log.extend({"asset": a, "cohort": cohort, **g} for g in f.attrs.get("garch_refits", []))
            ev = pd.concat([events(s, h, targets) for h in bt["horizons"]], ignore_index=True)
            wide = f.pivot_table(index=["origin_idx", "h", "target"], columns="model", values="p", dropna=False).reset_index()
            df = ev.merge(wide, on=["origin_idx", "h", "target"], how="left").assign(asset=a, cohort=cohort)
            per_asset[a] = df
            series[(a, cohort)] = s
            e30 = df[(df["h"] == 30) & df["y"].notna() & (df["target"] == targets[0])]
            eligible[a] = pd.DatetimeIndex(e30["origin_date"])
        if not per_asset:
            continue
        start = E.holdout_start(cohort, eligible, bt["holdout_fraction"], prior)
        seals[cohort] = start
        for a, df in per_asset.items():
            df["split"] = E.split(df, start)
            all_rows.append(df)
            if ml_on:
                s = series[(a, cohort)]
                dv_path = Path(cfg["paths"]["history"]) / f"dvol_{a}.parquet"
                fo = origin_features(s, bt["ewma_lambda"], pd.read_parquet(dv_path) if dv_path.exists() else None)
                for c in ML.GROUP_D:
                    if c not in fo:
                        fo[c] = np.nan
                for h in bt["horizons"]:
                    for side, tl in (("up", bt["targets_up"]), ("down", bt["targets_down"])):
                        ev = df[(df["h"] == h) & df["target"].isin(tl)][
                            ["origin_idx", "origin_date", "outcome_date", "target", "y"]].reset_index(drop=True)
                        x = row_features(fo, ev["origin_idx"].to_numpy(), ev["target"].to_numpy(), h)
                        rows = pd.concat([ev, x], axis=1)
                        for m in bt["ml"]["models"]:
                            tasks.append({"rows": rows, "cfg": bt, "h": h, "side": side, "model": m, "asset": a,
                                          "cohort": cohort, "stop_date": None if final else start})

    data = pd.concat(all_rows, ignore_index=True)
    ml_models = []
    if ml_on and tasks:
        # cache keyed by everything that determines the predictions: ML code, config, inputs, holdout stop dates
        ml_code = hashlib.sha256(b"".join((Path(__file__).parent / f).read_bytes()
                                          for f in ("ml.py", "features.py", "panel.py", "models.py"))).hexdigest()
        key_src = json.dumps({"code": ml_code, "bt": bt, "final": final,
                              "seals": {c: str(d) for c, d in seals.items()},
                              "inputs": {p.name: _sha256(p) for p in sorted(Path(cfg["paths"]["history"]).glob("*.parquet"))}},
                             sort_keys=True, default=str)
        cache = Path(cfg["paths"]["runs"]) / "_ml_cache" / f"{hashlib.sha256(key_src.encode()).hexdigest()[:16]}"
        if (cache / "preds.parquet").exists():
            mlp = pd.read_parquet(cache / "preds.parquet")
            ml_log = json.loads((cache / "log.json").read_text())
        else:
            mlp, ml_log = run_ml(tasks, bt["ml"]["workers"])
            cache.mkdir(parents=True, exist_ok=True)
            mlp.to_parquet(cache / "preds.parquet", index=False)
            (cache / "log.json").write_text(json.dumps(ml_log, default=str))
        if len(mlp):
            wide = mlp.pivot_table(index=["asset", "cohort", "h", "origin_idx", "target"], columns="model",
                                   values="p").reset_index()
            data = data.merge(wide, on=["asset", "cohort", "h", "origin_idx", "target"], how="left")
            ml_models = [m for m in bt["ml"]["models"] if m in data.columns]
    models_all = [m for m in M.P_MODELS + M.Q_MODELS + ml_models if m in data.columns]
    for (a, c), g in data.groupby(["asset", "cohort"]):
        for m in models_all:
            dev = g[(g["split"] == "dev") & g["y"].notna()]
            coverage.append({"asset": a, "cohort": c, "model": m, "prob_type": M.PROB_TYPE.get(m, "P"),
                             "dev_rows": len(dev), "covered": float(dev[m].notna().mean()) if m in dev and len(dev) else 0.0,
                             "first_forecast": str(g.loc[g[m].notna(), "origin_date"].min().date()) if g[m].notna().any() else ""})

    scope = ["dev", "holdout"] if final else ["dev"]
    per_target, leader, relia, stab = [], [], [], []
    for (a, c, h), g in data[data["split"].isin(scope) & data["y"].notna()].groupby(["asset", "cohort", "h"]):
        for split_name in scope:
            gs = g[g["split"] == split_name]
            base_p = [m for m in M.P_MODELS if gs[m].notna().any()]
            ml_a = [m for m in ml_models if m != "xgb_AD" and gs[m].notna().any()]
            comps = [("P models", base_p),
                     ("P + Q-proxy", base_p + [m for m in M.Q_MODELS if m in gs and gs[m].notna().any()])]
            if ml_a:
                comps.append(("Stage 4", base_p + ml_a))
                if "xgb_AD" in gs and gs["xgb_AD"].notna().any():
                    comps.append(("Stage 4 + DVOL", comps[1][1] + ml_a + ["xgb_AD"]))
            for comp, mods in comps:
                if comp == "P + Q-proxy" and not any(m in M.Q_MODELS for m in mods):
                    continue
                common = gs.dropna(subset=mods)
                if common.empty:
                    continue
                pt, ld = E.score_group(common, mods, h, bt, rng, baselines=[m for m in mods if m in M.P_MODELS])
                key = {"asset": a, "cohort": c, "h": h, "split": split_name, "comparison": comp,
                       "common_start": str(common["origin_date"].min().date()),
                       "common_end": str(common["origin_date"].max().date())}
                per_target.append(pt.assign(**key))
                leader.append(ld.assign(**key))
                relia.append(E.reliability(common, mods, bt["reliability_bins"], h, 300, rng).assign(**key))
                if comp == "P models":  # stability on the primary comparison, testable targets only
                    ok_t = pt.loc[(pt["model"] == "climatology") & pt["testable"], "target"]
                    yr = common[common["target"].isin(ok_t)].assign(year=lambda d: d["origin_date"].dt.year)
                    for y_, gy in yr.groupby("year"):
                        b = {m: ((gy[m] - gy["y"]) ** 2).mean() for m in mods}
                        for m in mods:
                            if m == "climatology":
                                continue
                            stab.append({**key, "year": int(y_), "model": m, "n": len(gy),
                                         "bss_vs_climatology": 1 - b[m] / b["climatology"] if b["climatology"] > 0 else np.nan})

    rid = f"{utc_now():%Y%m%dT%H%M%SZ}_{hashlib.sha256((_code_hash() + json.dumps(bt, sort_keys=True)).encode()).hexdigest()[:8]}"
    rdir = Path(cfg["paths"]["runs"]) / f"backtest_{rid}"
    rdir.mkdir(parents=True, exist_ok=False)
    pd.concat(per_target).to_csv(rdir / "scores_by_target.csv", index=False)
    pd.concat(leader).to_csv(rdir / "leaderboard.csv", index=False)
    pd.concat(relia).to_csv(rdir / "reliability.csv", index=False)
    pd.DataFrame(stab).to_csv(rdir / "stability_by_year.csv", index=False)
    pd.DataFrame(gaps).to_csv(rdir / "data_gaps.csv", index=False)
    pd.DataFrame(coverage).to_csv(rdir / "coverage.csv", index=False)
    # Before the final evaluation only development rows are written: holdout-origin forecasts (e.g. climatology)
    # and purged rows would encode outcomes dated inside the holdout.
    keep = data if final else data[data["split"] == "dev"]
    keep.drop(columns=["threshold"]).to_parquet(rdir / "forecasts.parquet", index=False)
    if ml_log:
        mlog = pd.DataFrame(ml_log)
        mlog["refit"] = pd.to_datetime(mlog["refit"], utc=True)
        mlog["params"] = mlog["params"].astype(str)
        mlog.to_csv(rdir / "ml_refits.csv", index=False)
    gl = pd.DataFrame(garch_log)
    if len(gl) and not final:  # refits dated in the holdout were fitted on holdout returns: not written yet
        gl = gl[pd.to_datetime(gl["refit_date"], utc=True) < gl["cohort"].map(seals)]
    gl.to_csv(rdir / "garch_refits.csv", index=False)
    first = data[data["target"] == targets[0]]
    first = first.assign(forecastable=first[[m for m in M.P_MODELS if m in first]].notna().any(axis=1))
    counts = (first.groupby(["cohort", "asset", "h", "split"])
              .agg(origins=("origin_date", "nunique"), forecastable_origins=("forecastable", "sum"))
              .reset_index())
    counts.to_csv(rdir / "split_counts.csv", index=False)

    hist_dir = Path(cfg["paths"]["history"])
    write_json(rdir / "manifest.json", {
        "run_id": rid, "kind": "stage3_backtest", "created_utc": utc_now(), "final_holdout_opened": final,
        "holdout_seal": {c: str(d.date()) for c, d in seals.items()}, "config_backtest": bt,
        "assets": cfg["assets"], "source_revision": _git_rev(), "code_sha256": _code_hash(),
        "package_versions": {**_versions(), **{"arch": __import__("arch").__version__}},
        "history_inputs_sha256": {p.name: _sha256(p) for p in sorted(hist_dir.glob("*.parquet"))},
        "garch_refits_integrated": {f"{a}/{c}": f"{int(g['integrated'].sum())}/{len(g)}"
                                    for (a, c), g in gl.groupby(["asset", "cohort"])} if len(gl) else {},
        "seed": bt["seed"], "bootstrap": f"circular block bootstrap, block = h origin days, {bt['bootstrap_reps']} reps",
        "costs": {"wall_seconds": round(time.time() - t0, 1), "cpu_seconds": round(time.process_time() - c0, 1),
                  "paid_data_cost_usd": 0},
    })
    if make_charts:
        from . import charts
        charts.make_all(rdir)
    from . import report
    report.write(rdir)
    if final:
        E.record_final_completed(ledger_entry, rid)
    write_json(rdir / "outputs_sha256.json", {str(p.relative_to(rdir)): _sha256(p) for p in sorted(rdir.rglob("*"))
                                              if p.is_file() and p.name != "outputs_sha256.json"})
    return rid
