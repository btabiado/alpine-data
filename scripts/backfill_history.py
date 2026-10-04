#!/usr/bin/env python3
"""Backfill gaps in the committed histories, from REAL sources only.

Never interpolates, never carries a value across a gap, never invents. Every
value it writes comes from one of:

  git     the committed file as it stood at the end of that day
          (`git show <last commit before midnight UTC>:<path>`), run through
          the same function the live snapshotter uses
  series  the upstream's own dated daily series (blockchain.info charts,
          bitinfocharts cohorts, Coin Metrics community) truncated to what
          existed at the end of that day, run through the shipped formula
  r2      the exact payload the site served that day, from the R2 archive
          (raw/alpine-data/<day>/<file>); CI only, needs the R2_* secrets

Each filled entry carries `backfilled_from` naming its source, and its `note`
says "backfilled" so the composite-history modal shows it too. An entry that
already holds a value is never overwritten, with one exception: a `series`
recompute is superseded by the `r2` copy of what the card actually displayed.

Subcommands
-----------
composites --whale PATH [--through DAY] [--fill-missing START..END]
    Fill null whale_sentiment_btc/eth (series) and etf_flow_sentiment_btc/eth
    (git) in data/composites/<day>.json, and create snapshot files for days no
    build ran, holding only the indexes a real source can reproduce
    (lthcs_composite from git as well) and null for the rest.

r2 --start DAY --end DAY
    For each day: exact whale sentiments into the composites, and the
    stock-money-flow / travel-advisory history rows (scripts/snapshot_history.py)
    rebuilt from the archived sidecars.

Run from the repo root.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT))

COMPOSITES = "data/composites"
WHALE_KEYS = ("whale_sentiment_btc", "whale_sentiment_eth")
ETF_KEYS = ("etf_flow_sentiment_btc", "etf_flow_sentiment_eth")
R2_PREFIX = "raw/alpine-data"
R2_ENDPOINT = "https://d486b561a8eacd568dd8edf9c749ee47.r2.cloudflarestorage.com"

# The blockchain.info / Coin Metrics daily series publish a day's value the
# following day: the live payload read on 2026-10-04 ends at 2026-10-03, while
# the bitinfocharts cohort table already carries 2026-10-04. So a snapshot of
# day D saw on-chain series through D-1 and cohorts through D. Recomputing
# 2026-10-04 under this rule reproduces the live card exactly (BTC -38,
# ETH -32), which is the check that the rule matches what the card saw.
ONCHAIN_LAG_DAYS = 1


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def _days(start: str, end: str) -> list[str]:
    d, stop, out = date.fromisoformat(start), date.fromisoformat(end), []
    while d <= stop:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _git(*args: str, root: Path = REPO_ROOT) -> str | None:
    out = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)
    return out.stdout if out.returncode == 0 else None


def git_rev_at_end_of(day: str, path: str, root: Path = REPO_ROOT) -> str | None:
    """Last commit touching `path` before midnight UTC after `day`."""
    nxt = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
    out = _git("rev-list", "-1", f"--before={nxt}T00:00:00Z", "HEAD", "--", path,
               root=root)
    return out.strip() or None if out is not None else None


def git_blob(rev: str, path: str, root: Path = REPO_ROOT) -> str | None:
    return _git("show", f"{rev}:{path}", root=root)


def _mark(entry: dict | None, source: str) -> dict | None:
    if not isinstance(entry, dict):
        return None
    out = dict(entry)
    out["backfilled_from"] = source
    note = out.get("note")
    out["note"] = (f"{note}; " if note else "") + f"backfilled ({source.split(':', 1)[0]})"
    return out


def _is_recompute(entry) -> bool:
    return isinstance(entry, dict) and str(entry.get("backfilled_from", "")).startswith("series:")


# --------------------------------------------------------------------------
# Whale sentiment from upstream daily series
# --------------------------------------------------------------------------

def truncate_whale(whale: dict, day: str, lag_days: int = ONCHAIN_LAG_DAYS) -> dict:
    """The whale tree as it stood at the end of `day`: on-chain daily series
    through day-lag, cohort rows through day. Non-series leaves (24h counters,
    large transactions) are dropped: they describe the fetch instant, not
    `day`, and neither sentiment function reads them."""
    cut_chain = (date.fromisoformat(day) - timedelta(days=lag_days)).isoformat()

    def cut(series, through):
        return [r for r in series if isinstance(r, dict)
                and isinstance(r.get("date"), str) and r["date"][:10] <= through]

    btc = {k: cut(v, cut_chain) for k, v in (whale.get("btc") or {}).items()
           if isinstance(v, list)}
    dist = dict(whale.get("distribution") or {})
    dist["buckets"] = cut(dist.get("buckets") or [], day)
    eth_src = whale.get("eth") or {}
    cm = {k: cut(v, cut_chain) for k, v in (eth_src.get("coin_metrics") or {}).items()
          if isinstance(v, list)}
    eds = eth_src.get("etherscan_daily") or {}
    if isinstance(eds, dict) and isinstance(eds.get("series"), list):
        eds = {**eds, "series": cut(eds["series"], cut_chain)}
    return {"btc": btc, "distribution": dist,
            "eth": {"coin_metrics": cm, "etherscan_daily": eds}}


def whale_entries_from_series(whale: dict, day: str, source_label: str) -> dict:
    """{whale_sentiment_btc, whale_sentiment_eth} recomputed for `day`."""
    import fetch_market as fm
    import snapshot_composites as sc
    tree = truncate_whale(whale, day)
    out = {}
    for key, fn in (("whale_sentiment_btc", fm.compute_whale_sentiment),
                    ("whale_sentiment_eth", fm.compute_whale_sentiment_eth)):
        sent = fn(tree)
        if not isinstance(sent, dict) or sent.get("available") is False \
                or sent.get("score") is None or not sent.get("as_of"):
            out[key] = None
            continue
        out[key] = _mark(sc._entry(sent["score"], sent.get("label"), sent["as_of"]),
                         f"series:{source_label} truncated to the end of {day}")
    return out


def whale_entries_from_payload(payload: dict, source: str) -> dict:
    """{whale_sentiment_btc, whale_sentiment_eth} exactly as a served
    data-whale.json carried them (app.py attaches both before writing it)."""
    import snapshot_composites as sc
    out = {}
    btc = payload.get("sentiment") if isinstance(payload.get("sentiment"), dict) else {}
    eth = ((payload.get("eth") or {}).get("sentiment")
           if isinstance((payload.get("eth") or {}).get("sentiment"), dict) else {})
    for key, sent in (("whale_sentiment_btc", btc), ("whale_sentiment_eth", eth)):
        if not sent or sent.get("available") is False or sent.get("score") is None:
            out[key] = None
            continue
        # Same date gate the snapshotter applies: only a payload stamped by
        # the provenance-fixed fetcher carries a trustworthy as_of.
        as_of = sc._whale_sentiment_date(sent)
        out[key] = _mark(sc._entry(sent["score"], sent.get("label"), as_of), source) \
            if as_of else None
    return out


# --------------------------------------------------------------------------
# ETF flow sentiment and LTHCS composite from git
# --------------------------------------------------------------------------

def etf_entries_from_git(day: str, root: Path = REPO_ROOT) -> dict:
    """etf_flow_sentiment_btc/eth (+ the btc alias) from the flow CSVs as
    committed at the end of `day`, through snapshot_composites' own code."""
    import snapshot_composites as sc
    out = {}
    with tempfile.TemporaryDirectory() as tmp:
        revs = {}
        for asset in ("btc", "eth"):
            rel = f"data/{asset}_flows.csv"
            rev = git_rev_at_end_of(day, rel, root)
            blob = git_blob(rev, rel, root) if rev else None
            if blob:
                (Path(tmp) / f"{asset}_flows.csv").write_text(blob, encoding="utf-8")
                revs[asset] = rev
        saved = sc.CACHE
        sc.CACHE = Path(tmp)
        try:
            for asset in ("btc", "eth"):
                key = f"etf_flow_sentiment_{asset}"
                e = sc.etf_flow_sentiment(asset) if asset in revs else None
                out[key] = _mark(e, f"git:{revs[asset][:10]}:data/{asset}_flows.csv "
                                    f"(as committed at the end of {day})") if e else None
        finally:
            sc.CACHE = saved
    out["etf_flow_sentiment"] = sc._alias(out.get("etf_flow_sentiment_btc"),
                                          "etf_flow_sentiment_btc")
    return out


def lthcs_entry_from_git(day: str, root: Path = REPO_ROOT) -> dict | None:
    """lthcs_composite as the snapshotter reads it (newest index file), from
    the tree committed at the end of `day`."""
    import snapshot_composites as sc
    rev = git_rev_at_end_of(day, "data/lthcs/index", root)
    if not rev:
        return None
    names = (_git("ls-tree", "--name-only", rev, "data/lthcs/index/", root=root) or "").split()
    dated = sorted(n for n in names if Path(n).stem[:4].isdigit() and Path(n).stem <= day)
    if not dated:
        return None
    blob = git_blob(rev, dated[-1], root)
    try:
        doc = json.loads(blob or "")
    except ValueError:
        return None
    e = sc._entry(doc.get("score"), doc.get("label"), doc.get("as_of") or Path(dated[-1]).stem)
    return _mark(e, f"git:{rev[:10]}:{dated[-1]} (newest index file at the end of {day})")


# --------------------------------------------------------------------------
# Merge into snapshot files
# --------------------------------------------------------------------------

def merge(indexes: dict, fills: dict) -> list[str]:
    """Put `fills` into `indexes` where the slot is empty, or holds a series
    recompute that an r2 copy supersedes. Returns the keys changed."""
    changed = []
    for k, v in fills.items():
        if v is None:
            continue
        cur = indexes.get(k)
        superseding = _is_recompute(cur) and str(v.get("backfilled_from", "")).startswith("r2:")
        if cur is None or superseding:
            indexes[k] = v
            changed.append(k)
    return changed


def _write_snapshot(path: Path, snap: dict) -> None:
    path.write_text(json.dumps(snap, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def backfill_composites(root: Path, whale: dict | None, whale_label: str,
                        through: str, fill_missing: tuple[str, str] | None,
                        now: datetime) -> dict:
    """Returns {day: [changed keys]} for every file touched."""
    cdir = root / COMPOSITES
    report: dict[str, list[str]] = {}
    days = sorted(p.stem for p in cdir.glob("*.json") if len(p.stem) == 10 and p.stem <= through)
    created = []
    if fill_missing:
        for d in _days(*fill_missing):
            if d <= through and not (cdir / f"{d}.json").exists():
                created.append(d)
    keyset: set[str] = set()
    for d in days:
        keyset |= set((json.loads((cdir / f"{d}.json").read_text()).get("indexes") or {}))
    for d in sorted(set(days) | set(created)):
        path = cdir / f"{d}.json"
        if path.exists():
            snap = json.loads(path.read_text())
        else:
            snap = {
                "as_of": d,
                "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "backfilled": (
                    "No pages build ran this day, so no snapshot was taken. "
                    "Entries carrying backfilled_from were reproduced from "
                    "real historical sources; every other index is null "
                    "because no record of it survives."),
                "indexes": {k: None for k in sorted(keyset)},
            }
        idx = snap.setdefault("indexes", {})
        fills = {}
        if whale is not None:
            fills.update(whale_entries_from_series(whale, d, whale_label))
        fills.update(etf_entries_from_git(d, root))
        if not path.exists():
            fills["lthcs_composite"] = lthcs_entry_from_git(d, root)
        changed = merge(idx, fills)
        if changed or not path.exists():
            _write_snapshot(path, snap)
            report[d] = changed
    return report


# --------------------------------------------------------------------------
# R2 (CI only)
# --------------------------------------------------------------------------

def _r2_client():
    import boto3  # installed by the workflow; never needed offline
    return boto3.client("s3", endpoint_url=R2_ENDPOINT, region_name="auto")


def r2_get_json(client, bucket: str, key: str):
    try:
        obj = client.get_object(Bucket=bucket, Key=key)
        return json.loads(obj["Body"].read())
    except Exception as exc:  # missing key, transient error: a gap, not a crash
        print(f"  [r2] {key}: {type(exc).__name__}")
        return None


def backfill_from_r2(root: Path, start: str, end: str, fetch) -> dict:
    """`fetch(day, file)` returns the archived payload or None. Injected so
    the merge logic is testable without R2."""
    import snapshot_history as sh
    report = {"composites": {}, "stock_rows": 0, "travel_days": 0}
    cdir = root / COMPOSITES
    for d in _days(start, end):
        whale = fetch(d, "data-whale.json")
        path = cdir / f"{d}.json"
        if whale and path.exists():
            snap = json.loads(path.read_text())
            changed = merge(snap.setdefault("indexes", {}),
                            whale_entries_from_payload(
                                whale, f"r2:{R2_PREFIX}/{d}/data-whale.json"))
            if changed:
                _write_snapshot(path, snap)
                report["composites"][d] = changed
        nxt = date.fromisoformat(d) + timedelta(days=1)
        stock = fetch(d, sh.STOCK_SRC)
        report["stock_rows"] += sh.apply_stock(root, stock, nxt, max_lag_days=None,
                                               provenance=f"r2@{d}")
        travel = fetch(d, sh.TRAVEL_SRC)
        n_day, _ = sh.apply_travel(root, travel, d, require_generated_on=d,
                                   provenance=f"r2@{d}")
        report["travel_days"] += n_day
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("composites")
    c.add_argument("--whale", required=True,
                   help="a served data-whale.json whose daily series cover the range")
    c.add_argument("--whale-label", default=None,
                   help="provenance label for --whale (default: its path)")
    c.add_argument("--through", default=None,
                   help="last day to touch (default: yesterday UTC; today's "
                        "file is still being written by the hourly build)")
    c.add_argument("--fill-missing", default=None, metavar="START..END",
                   help="create snapshot files for days in this range with none")
    r = sub.add_parser("r2")
    r.add_argument("--start", required=True)
    r.add_argument("--end", required=True)
    args = ap.parse_args(argv)
    now = datetime.now(timezone.utc)

    if args.cmd == "composites":
        whale = json.loads(Path(args.whale).read_text())
        through = args.through or (now.date() - timedelta(days=1)).isoformat()
        fm = tuple(args.fill_missing.split("..")) if args.fill_missing else None
        rep = backfill_composites(REPO_ROOT, whale, args.whale_label or args.whale,
                                  through, fm, now)
        for d, keys in sorted(rep.items()):
            print(f"{d}: {', '.join(keys) or 'created'}")
        print(f"[backfill] composites: {len(rep)} file(s) touched")
        return 0

    import os
    bucket = os.environ.get("R2_BUCKET_NAME")
    if not bucket:
        print("[backfill] R2_BUCKET_NAME unset; nothing to read")
        return 1
    client = _r2_client()
    rep = backfill_from_r2(
        REPO_ROOT, args.start, args.end,
        lambda d, f: r2_get_json(client, bucket, f"{R2_PREFIX}/{d}/{f}"))
    print(f"[backfill] r2: composites {len(rep['composites'])} file(s), "
          f"stock +{rep['stock_rows']} rows, travel +{rep['travel_days']} days")
    return 0


if __name__ == "__main__":
    sys.exit(main())
