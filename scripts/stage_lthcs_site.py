#!/usr/bin/env python3
"""Stage the LTHCS pages and the data they read into the Pages tree (pages.yml).

Before this script, pages.yml copied all of data/lthcs (540+ MB, growing about
7 MB a day) into _site twice -- once at /data/lthcs/ for the card view and once
at /lthcs/data/lthcs/ for the subpages -- which put the published site over
GitHub Pages' 1 GB limit. Every file stays in the repository; this only
decides what the site publishes, and publishes it once.

URL layout (production, under https://btabiado.github.io/alpine-data/):

  /data/lthcs/...          the ONLY copy of the data (also the documented
                           public endpoint in data/lthcs/public/manifest.json)
  /lthcs/                  card view            <- lthcs_tab/ (no mockups/)
  /lthcs/heatmap/          heatmap              <- lthcs_tab/heatmap/
  /lthcs/lthcs_tab/        shared modules + CSS <- lthcs_tab/*.js, *.css
  /lthcs/<page>/           subpages             <- lthcs_<page>/ (see SUBPAGES)

In the repo every page sits one directory below the root (lthcs_tab/,
lthcs_table/, ...) and reads ``../data/lthcs/``. In production the subpages
and the shared-module copy sit two levels down, so their ``../data/lthcs``
used to land on the /lthcs/data/lthcs/ duplicate. This script stages those
files with one extra ``../`` (``../../data/lthcs``) so every page reads the
single /data/lthcs/ copy. The card view and heatmap already resolve there
unchanged. tests/test_stage_lthcs_site.py checks that every data reference in
the staged pages climbs exactly to the site root and names a staged path.

What is published from data/lthcs is an allow-list traced from the page
sources (see DATA_WHOLE / DATA_FILES / DATA_LATEST below). Directories the
pages read only for the newest date (the detail modal, table and health pages
ask for <calc_date>.json of the latest snapshot) are published for the newest
KEEP_LATEST dates; their full history stays in the repository. The deploy-time
file index is rewritten for the staged tree, so pages that consult it never
ask for a file that was not published.

Usage: python scripts/stage_lthcs_site.py [--repo .] [--site _site] [--keep-latest 7]
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import shutil
import sys
from pathlib import Path

# Repo directory -> path under /lthcs/ in production. Each is staged only when
# its index.html exists (same gate the old bash branches used).
SUBPAGES = {
    "lthcs_table": "table",
    "lthcs_health": "health",
    "lthcs_backtest": "backtest",
    "lthcs_crypto": "crypto",
    "lthcs_position": "position",
    "lthcs_public": "public",
    "lthcs_help": "help",
    "lthcs_diff": "diff",
    "lthcs_history": "history",
    "lthcs_leaderboards": "leaderboards",
}

# Directories of lthcs_tab/ that are not part of the card view. mockups/ holds
# old design variants with the old band ranges; nothing links to it.
CARD_VIEW_EXCLUDE = {"mockups"}

# --- data/lthcs allow-list -------------------------------------------------
# Every entry names the page(s) that read it. A data path no page reads is not
# published; tests/test_stage_lthcs_site.py fails if a page references a path
# outside this list.

# Published whole (every file).
DATA_WHOLE = {
    "snapshots": "card view, table, heatmap, position (latest); /lthcs/diff/ (any two dates); "
                 "detail-modal pillar fallback; health + pipeline pages",
    "snapshots_crypto": "/lthcs/crypto/, pipeline page, public manifest",
    "history": "by_ticker/ (detail chart, /lthcs/history/, leaderboards, crypto, compare), "
               "pillars_by_ticker/ + trend_index.json (built at deploy time)",
    "index": "card view composite index (index/<calc_date>.json)",
    "macro": "card-view regime strip, freshness stamp, health page",
    "trends": "health + pipeline pages (newest ISO week)",
    "backtest": "/lthcs/backtest/ (pinned validation run), ab.html (ab_latest.json run), "
                "pipeline page (newest daily + monthly run)",
    "adaptive_weights": "/lthcs/backtest/ walk-forward summary",
    "quality_audit": "/lthcs/health/quality.html",
    "public": "/lthcs/public/, leaderboards, card-view CSV export",
    "sentiment": "pipeline page (sentiment/AAPL.json freshness anchor)",
    "analyst_breadth": "health page (newest date)",
}

# Single files at the data/lthcs root.
DATA_FILES = {
    "universe.json": "card view, table, compare, heatmap, history, leaderboards, health",
    "weights.json": "band cutoffs (lthcs-bands.js), help page, pipeline page",
    "crypto_universe.json": "/lthcs/crypto/",
    "thesis_rotation.json": "card-view freshness stamp",
    "health_summary.json": "/lthcs/health/ (built at deploy time)",
    # file_index.json is rewritten for the staged tree by write_file_index().
}

# Dated directories (<name>/<YYYY-MM-DD>.json, one file per day) that the
# pages read only for the newest calc date. Published for the newest
# KEEP_LATEST dates; older files remain in the repository.
DATA_LATEST = {
    "variable_detail": "detail modal + explainer (latest calc date), health, pipeline",
    "insider": "card view + table (latest calc date)",
    "holdings": "detail modal + table (latest calc date)",
    "narratives": "pipeline page (newest date), public manifest (latest)",
    "narratives_llm": "detail modal LLM narrative (latest calc date), pipeline page",
}
KEEP_LATEST = 7

DATED_NAME = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2})\.json$")
# A relative reference to the data tree that is not already one level deeper.
DATA_REF = re.compile(r"(?<![\w./])\.\./data/lthcs")
TEXT_SUFFIXES = {".js", ".mjs", ".html", ".css"}


def _deepen(src: Path, dst: Path) -> None:
    """Copy a page file, giving its data references one more '../'."""
    if src.suffix in TEXT_SUFFIXES:
        text = src.read_text(encoding="utf-8")
        dst.write_text(DATA_REF.sub("../../data/lthcs", text), encoding="utf-8")
    else:
        shutil.copy2(src, dst)


def _copy_tree(src: Path, dst: Path, *, deepen: bool, exclude: set[str] = frozenset()) -> int:
    n = 0
    for f in sorted(src.rglob("*")):
        rel = f.relative_to(src)
        if rel.parts and rel.parts[0] in exclude:
            continue
        if f.is_dir() or f.name.startswith("."):
            continue
        out = dst / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        if deepen:
            _deepen(f, out)
        else:
            shutil.copy2(f, out)
        n += 1
    return n


def stage_pages(repo: Path, site: Path) -> dict[str, int]:
    """Card view, shared-module copy and subpages under site/lthcs/."""
    counts: dict[str, int] = {}
    tab = repo / "lthcs_tab"
    if not (tab / "index.html").is_file():
        return counts
    lthcs = site / "lthcs"
    counts["/lthcs/"] = _copy_tree(tab, lthcs, deepen=False, exclude=CARD_VIEW_EXCLUDE)
    # Shared modules for the subpages ("../lthcs_tab/x.js" from /lthcs/<page>/).
    shared = lthcs / "lthcs_tab"
    shared.mkdir(parents=True, exist_ok=True)
    n = 0
    for f in sorted(tab.iterdir()):
        if f.is_file() and f.suffix in {".js", ".css"}:
            _deepen(f, shared / f.name)
            n += 1
    counts["/lthcs/lthcs_tab/"] = n
    for src_name, dest in SUBPAGES.items():
        src = repo / src_name
        if (src / "index.html").is_file():
            counts[f"/lthcs/{dest}/"] = _copy_tree(src, lthcs / dest, deepen=True)
    return counts


def _dated_files(d: Path) -> list[Path]:
    return sorted((f for f in d.iterdir() if f.is_file() and DATED_NAME.match(f.name)),
                  key=lambda f: f.name, reverse=True)


def stage_data(src: Path, dst: Path, keep_latest: int = KEEP_LATEST) -> dict:
    """Copy the allow-listed part of data/lthcs. Returns what was staged."""
    report = {"whole": {}, "files": [], "latest": {}}
    if not src.is_dir():
        return report
    dst.mkdir(parents=True, exist_ok=True)
    for name in DATA_WHOLE:
        if (src / name).is_dir():
            report["whole"][name] = _copy_tree(src / name, dst / name, deepen=False)
    for name in DATA_FILES:
        if (src / name).is_file():
            shutil.copy2(src / name, dst / name)
            report["files"].append(name)
    for name in DATA_LATEST:
        d = src / name
        if not d.is_dir():
            continue
        keep = _dated_files(d)[:keep_latest]
        (dst / name).mkdir(parents=True, exist_ok=True)
        for f in keep:
            shutil.copy2(f, dst / name / f.name)
        report["latest"][name] = [f.stem for f in keep]
    return report


def _index_builder(repo: Path):
    spec = importlib.util.spec_from_file_location(
        "_build_lthcs_site_index", repo / "scripts" / "build_lthcs_site_index.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def write_file_index(repo: Path, dst: Path, keep_latest: int) -> dict:
    """file_index.json describing the STAGED tree, so a page that asks the
    index "does X exist?" is told about the published files only."""
    index = _index_builder(repo).build_file_index(dst)
    index["note"] = (
        "Listing of the files published under /data/lthcs/ at deploy time. Dates are "
        "file-name dates (observation days), not modification times. "
        + ", ".join(sorted(DATA_LATEST))
        + f" are published for the newest {keep_latest} dates only; every older file "
        "is kept in the repository (https://github.com/btabiado/alpine-data/tree/main/data/lthcs).")
    index["published_latest_only"] = {"dirs": sorted(DATA_LATEST), "keep_latest": keep_latest}
    (dst / "file_index.json").write_text(json.dumps(index, separators=(",", ":")), encoding="utf-8")
    return index


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", default=".")
    ap.add_argument("--site", default="_site")
    ap.add_argument("--keep-latest", type=int, default=KEEP_LATEST)
    args = ap.parse_args(argv)
    repo, site = Path(args.repo), Path(args.site)
    if args.keep_latest < 1:
        ap.error("--keep-latest must be >= 1")
    pages = stage_pages(repo, site)
    if not pages:
        print("[stage-lthcs] lthcs_tab/index.html missing; LTHCS not staged")
        return 0
    data = stage_data(repo / "data" / "lthcs", site / "data" / "lthcs", args.keep_latest)
    write_file_index(repo, site / "data" / "lthcs", args.keep_latest)
    for k, v in pages.items():
        print(f"[stage-lthcs] {k}: {v} files")
    print(f"[stage-lthcs] data whole: {', '.join(f'{k} ({v})' for k, v in data['whole'].items())}")
    print(f"[stage-lthcs] data files: {', '.join(data['files'])}")
    for k, v in data["latest"].items():
        span = f"{v[-1]}..{v[0]}" if v else "none"
        print(f"[stage-lthcs] data {k}: newest {len(v)} ({span})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
