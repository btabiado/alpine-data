"""Snapshot collection and analysis runs with immutable IDs and manifests (brief s.9)."""
from __future__ import annotations

import hashlib
import json
import logging
import platform
import subprocess
import time
from importlib import metadata
from pathlib import Path

import numpy as np
import pandas as pd

from . import analysis
from .adapters import deribit
from .adapters.http import RawArchive
from .config import PROJECT_ROOT
from .quality.normalize import apply_quality, normalize_chain, parity_check, utc_now

_log = logging.getLogger(__name__)


def _git_rev():
    try:
        rev = subprocess.run(["git", "rev-parse", "HEAD"], cwd=PROJECT_ROOT, capture_output=True, text=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--", "src", "config"], cwd=PROJECT_ROOT,
                               capture_output=True, text=True).stdout.strip()
        return rev + ("+dirty" if dirty else "")
    except OSError:
        return "unknown"


def _sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _time_skew(df, idx, cfg):
    """Seconds between the inputs combined in one curve: quote responses, forward (same response) and index."""
    ts = pd.to_datetime(df["response_ts"])
    spread_s = (ts.max() - ts.min()).total_seconds()
    retrieved = pd.Timestamp(df["retrieval_ts"].iat[0])
    idx_times = [pd.Timestamp(v["retrieved_at"]) for v in idx.values() if v.get("retrieved_at")]
    idx_gap = max((abs((t - retrieved).total_seconds()) for t in idx_times), default=0.0)
    skew = max(spread_s, idx_gap)
    return {"time_skew_s": round(skew, 3), "time_skew_flag": bool(skew > cfg["quality"]["max_time_skew_s"]),
            "time_skew_note": "quotes, forwards and spot (estimated_delivery_price) share one book-summary "
                              "response; the separately fetched index prices feed only the USD/USDC depeg check"}


def _code_hash():
    """Content hash of the package source, so changed code never reuses a run id."""
    h = hashlib.sha256()
    for f in sorted((PROJECT_ROOT / "src" / "crypto_options").rglob("*.py")):
        h.update(f.relative_to(PROJECT_ROOT).as_posix().encode() + f.read_bytes())
    return h.hexdigest()


def _versions():
    pk = ["numpy", "scipy", "pandas", "pyarrow", "requests", "matplotlib", "pyyaml", "clarabel", "highspy"]
    out = {"python": platform.python_version()}
    for p in pk:
        try:
            out[p] = metadata.version(p)
        except metadata.PackageNotFoundError:
            _log.warning("package %s is not installed; omitting it from package_versions", p)
    return out


def _json_default(o):
    if isinstance(o, (pd.Timestamp,)):
        return o.isoformat()
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return None if not np.isfinite(o) else float(o)
    if isinstance(o, (np.bool_,)):
        return bool(o)
    return str(o)


def write_json(path: Path, obj):
    path.write_text(json.dumps(obj, indent=2, default=_json_default))


def run_discover(cfg, assets, session=None):
    client = deribit.make_client(cfg, None, session)
    return deribit.discover(cfg, assets, client)


def run_snapshot(cfg, assets, session=None) -> str:
    """Fetch, archive raw responses, normalize, apply quality rules. Returns the snapshot id."""
    t0 = time.time()
    started = utc_now()
    archive = RawArchive(Path(cfg["paths"]["raw"]))
    client = deribit.make_client(cfg, archive, session)
    chains = deribit.snapshot_chains(cfg, assets, client)
    idx = deribit.index_prices(cfg, assets, client)

    snap_id = started.strftime("%Y%m%dT%H%M%SZ")
    out = Path(cfg["paths"]["normalized"]) / snap_id
    out.mkdir(parents=True, exist_ok=False)
    frames, statuses = [], []
    for ch in chains:
        st = {"asset": ch.asset, "family": ch.family, "role": ch.role, "currency": ch.currency,
              "status": ch.status, "error": ch.error, "n_instruments": len(ch.instruments),
              "retrieved_at": ch.retrieved_at}
        if ch.status == "ok":
            df = apply_quality(normalize_chain(ch), cfg["quality"])
            st.update(_time_skew(df, idx, cfg))
            st["n_two_sided"] = int(df["excluded_reason"].ne("one_sided_or_no_quote").sum())
            st["n_accepted_otm"] = int(df["accepted"].sum())
            st["exclusions"] = df["excluded_reason"].value_counts().to_dict()
            frames.append(df)
        statuses.append(st)
    for asset in assets:
        if not cfg["venues"]["deribit"]["chains"].get(asset):
            statuses.append({"asset": asset, "status": "unavailable",
                             "error": cfg["unavailable_reasons"].get(asset, "no option route configured")})
    quotes_hash = None
    if frames:
        pd.concat(frames, ignore_index=True).to_parquet(out / "quotes.parquet", index=False)
        quotes_hash = _sha256(out / "quotes.parquet")
    write_json(out / "snapshot_manifest.json", {
        "snapshot_id": snap_id, "started_utc": started, "assets": assets, "chains": statuses,
        "index_prices": idx, "raw_files": archive.records, "n_api_calls": client.n_calls,
        "quotes_parquet_sha256": quotes_hash,
        "wall_seconds": round(time.time() - t0, 2), "source_revision": _git_rev(),
        "note": "quote_ts is unknown for every Deribit quote (no genuine last-update time in the API)",
    })
    return snap_id


def _spread_sensitivity(quotes, cfg, asof, base):
    """Re-run the full chain at a looser relative-spread threshold; report availability and moves (brief s.4)."""
    alt_thr = cfg["quality"].get("sensitivity_max_relative_spread")
    if not alt_thr or quotes.empty:
        return None
    alt_cfg = {**cfg, "quality": {**cfg["quality"], "max_relative_spread": alt_thr}}
    rows = []
    for _, df in quotes.groupby(["asset", "family"], sort=False):
        raw = df.drop(columns=["flags", "excluded_reason", "accepted", "otm", "rel_spread"], errors="ignore")
        rows.extend(analysis.analyze_chain(apply_quality(raw, alt_cfg["quality"]), alt_cfg, asof)[1])
    alt = pd.DataFrame(rows)
    key = ["asset", "family", "expiry_utc", "target_label"]
    b = base[base["model"] == "full_chain_constrained"].drop_duplicates(key)[key + ["probability"]]
    a = alt[alt["model"] == "full_chain_constrained"].drop_duplicates(key)[key + ["probability"]]
    m = b.merge(a, on=key, suffixes=("_base", "_alt"))
    m["base_threshold"], m["alt_threshold"] = cfg["quality"]["max_relative_spread"], alt_thr
    m["change"] = np.where(m["probability_base"].isna() & m["probability_alt"].notna(), "newly available",
                  np.where(m["probability_base"].notna() & m["probability_alt"].isna(), "lost",
                  np.where(m["probability_base"].isna(), "unavailable in both", "both available")))
    m["abs_diff"] = (m["probability_alt"] - m["probability_base"]).abs()
    return m


def latest_snapshot(cfg) -> str:
    snaps = sorted(p.name for p in Path(cfg["paths"]["normalized"]).iterdir() if (p / "quotes.parquet").exists())
    if not snaps:
        raise FileNotFoundError("no snapshot with quotes yet; run `python -m crypto_options snapshot` first")
    return snaps[-1]


def run_analyze(cfg, snapshot_id=None, make_charts=True) -> str:
    t0, c0 = time.time(), time.process_time()
    snapshot_id = snapshot_id or latest_snapshot(cfg)
    sdir = Path(cfg["paths"]["normalized"]) / snapshot_id
    smani = json.loads((sdir / "snapshot_manifest.json").read_text())
    quotes = pd.read_parquet(sdir / "quotes.parquet") if (sdir / "quotes.parquet").exists() else pd.DataFrame()
    asof = pd.Timestamp(smani["started_utc"])

    cfg_clean = {k: v for k, v in cfg.items() if not k.startswith("_")}
    cfg_hash = hashlib.sha256(json.dumps(cfg_clean, sort_keys=True, default=str).encode()).hexdigest()
    code_hash = _code_hash()
    run_id = f"{snapshot_id}_{hashlib.sha256((cfg_hash + code_hash).encode()).hexdigest()[:8]}"
    rdir = Path(cfg["paths"]["runs"]) / run_id
    rdir.mkdir(parents=True, exist_ok=False)  # immutable: a run id is never rewritten

    rows, curves, parity, all_fits = [], [], [], {}
    for (asset, family), df in (quotes.groupby(["asset", "family"], sort=False) if len(quotes) else []):
        fits, r = analysis.analyze_chain(df, cfg, asof)
        rows.extend(r)
        all_fits[(asset, family)] = fits
        pc = parity_check(df)
        pc.insert(0, "family", family); pc.insert(0, "asset", asset)
        parity.append(pc)
        for expiry, f in fits.items():
            fit = f["fit"]
            if fit.grid is None:
                continue
            mids = 0.5 * (fit.grid[:-1] + fit.grid[1:])
            curves.append(pd.DataFrame({"asset": asset, "family": family, "expiry_utc": expiry,
                                        "k": mids, "strike": mids * f["F"], "survival_Q": -fit.slopes}))
    for st in smani["chains"]:
        if st["status"] != "ok" and st.get("family") is None:
            rows.extend(analysis.unavailable_asset_rows(st["asset"], st["error"]))
        elif st["status"] != "ok":
            rows.extend(analysis.unavailable_asset_rows(st["asset"], f"{st['family']} chain {st['status']}: {st['error']}"))

    results = pd.DataFrame(rows)
    results.insert(0, "as_of_utc", asof)
    results.insert(1, "as_of_new_york", asof.tz_convert(cfg["display_timezone"]))
    results.to_csv(rdir / "results.csv", index=False)
    results.to_parquet(rdir / "results.parquet", index=False)
    if curves:
        pd.concat(curves).to_parquet(rdir / "curves.parquet", index=False)
    if parity:
        pd.concat(parity).to_csv(rdir / "parity_check.csv", index=False)
    depeg = analysis.depeg_check(smani["index_prices"], cfg)

    fit_diag = []
    for (asset, family), fits in all_fits.items():
        for expiry, f in fits.items():
            fit = f["fit"]
            fit_diag.append({"asset": asset, "family": family, "expiry_utc": expiry, "days": f["T"] * 365,
                             "forward": f["F"], "spot_index": f["spot"], "atm_iv": f["atm_iv"],
                             "status": fit.status, "reasons": "; ".join(fit.reasons), "n_quotes": fit.n_quotes,
                             "lambda": fit.lam, "rms_half_spreads": fit.rms_spread_units,
                             "max_half_spreads": fit.max_spread_units, "frac_outside_quotes": fit.frac_outside_quotes,
                             "lp_feasible": fit.lp_feasible, "uncovered_mass_below": fit.tail_below,
                             "uncovered_mass_above": fit.tail_above,
                             "strike_lo": float(f["quotes"]["strike"].min()) if len(f["quotes"]) else np.nan,
                             "strike_hi": float(f["quotes"]["strike"].max()) if len(f["quotes"]) else np.nan})
    pd.DataFrame(fit_diag).to_csv(rdir / "fit_diagnostics.csv", index=False)

    sens = _spread_sensitivity(quotes, cfg, asof, results)
    if sens is not None:
        sens.to_csv(rdir / "sensitivity_spread_threshold.csv", index=False)

    if make_charts:
        from .reporting import charts
        charts.make_all(all_fits, results, rdir, cfg)

    raw_hashes = {Path(r["path"]).name: r["sha256"] for r in smani["raw_files"]}
    from .reporting import report
    write_json(rdir / "manifest.json", {
        "run_id": run_id, "as_of_utc": asof, "created_utc": utc_now(), "snapshot_id": snapshot_id,
        "config_sha256": cfg_hash, "code_sha256": code_hash, "config": cfg_clean, "source_revision": _git_rev(),
        "package_versions": _versions(), "seeds": "none (deterministic solvers)",
        "raw_inputs_sha256": raw_hashes, "quotes_parquet_sha256": smani.get("quotes_parquet_sha256"),
        "depeg_check": depeg, "chains": smani["chains"],
        "costs": {"analyze_wall_seconds": round(time.time() - t0, 2),
                  "analyze_cpu_seconds": round(time.process_time() - c0, 2),
                  "snapshot_api_calls": smani.get("n_api_calls"),
                  "snapshot_wall_seconds": smani.get("wall_seconds"), "paid_data_cost_usd": 0},
    })
    report.write_report(rdir)
    outputs = {str(p.relative_to(rdir)): _sha256(p) for p in sorted(rdir.rglob("*"))
               if p.is_file() and p.name != "outputs_sha256.json"}
    write_json(rdir / "outputs_sha256.json", outputs)
    return run_id
