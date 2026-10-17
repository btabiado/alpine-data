"""scripts/stage_lthcs_site.py: publish the LTHCS pages and only the data they read, once.

The Pages artifact used to carry data/lthcs twice (/data/lthcs/ and
/lthcs/data/lthcs/), over GitHub Pages' 1 GB limit. These tests pin the three
things that keep the slimmer site working:

  * every data reference in a staged page climbs exactly to the site root,
    so it lands on the single /data/lthcs/ copy, and names something staged;
  * every data path a page reads is on the staging allow-list (so a new page
    that reads a new directory fails here, not as a 404 on the live site);
  * dated directories are trimmed to the newest N files and the file index
    lists only what was published.
"""
from __future__ import annotations

import importlib.util
import json
import re
import shutil
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
PAGES_YML = ROOT / ".github" / "workflows" / "pages.yml"

spec = importlib.util.spec_from_file_location("stage_lthcs_site", ROOT / "scripts" / "stage_lthcs_site.py")
stage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stage)

# Files scripts/build_lthcs_site_index.py writes into data/lthcs at deploy time
# (gitignored, so absent from a test checkout).
GENERATED = ("file_index.json", "health_summary.json",
             "history/trend_index.json", "history/pillars_by_ticker/AAPL.json")

REF = re.compile(r"((?:\.\./)+)data/lthcs/?([A-Za-z0-9_./-]*)")
PAGE_DIRS = ["lthcs_tab"] + list(stage.SUBPAGES)


@pytest.fixture(scope="module")
def staged(tmp_path_factory):
    """The real pages and an empty-file stand-in of the real data tree."""
    site = tmp_path_factory.mktemp("site")
    stage.stage_pages(ROOT, site)

    real_copy2 = shutil.copy2

    def touch(src, dst, *a, **k):
        dst = Path(dst)
        if dst.is_dir():
            dst = dst / Path(src).name
        if str(src).startswith(str(ROOT / "data" / "lthcs")):
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(b"")
            return dst
        return real_copy2(src, dst, *a, **k)

    stage.shutil.copy2 = touch
    try:
        report = stage.stage_data(ROOT / "data" / "lthcs", site / "data" / "lthcs", 3)
    finally:
        stage.shutil.copy2 = real_copy2
    for rel in GENERATED:
        p = site / "data" / "lthcs" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.touch()
    return site, report


def _text_files(d: Path):
    for f in sorted(d.rglob("*")):
        if f.is_file() and f.suffix in {".js", ".html", ".css", ".mjs"}:
            yield f


def test_every_staged_data_reference_reaches_the_single_copy(staged):
    site, _ = staged
    data = site / "data" / "lthcs"
    checked = 0
    for f in _text_files(site / "lthcs"):
        depth = len(f.relative_to(site).parts) - 1        # directories below the site root
        for m in REF.finditer(f.read_text(encoding="utf-8")):
            ups = m.group(1).count("../")
            assert ups == depth, (
                f"{f.relative_to(site)}: '{m.group(0)}' climbs {ups} level(s) from depth "
                f"{depth}; it would not land on /data/lthcs/")
            # The literal part of the path, up to any template/placeholder.
            # (Trailing dots are prose: "files under ../data/lthcs/." or ".../...".)
            lit = re.split(r"\$\{|<|\{", m.group(2))[0].rstrip(".")
            target = data / lit
            if lit.endswith("/") or not lit:
                assert target.is_dir(), f"{f.relative_to(site)} reads {m.group(0)}: not staged"
            else:
                # A file, a directory, or a filename prefix such as macro/breadth_.
                parent, stem = target.parent, target.name
                ok = target.exists() or (parent.is_dir() and any(
                    c.name.startswith(stem) for c in parent.iterdir()))
                assert ok, f"{f.relative_to(site)} reads {m.group(0)}: nothing staged there"
            checked += 1
    assert checked > 50


def test_subpages_and_shared_modules_are_one_level_deeper(staged):
    site, _ = staged
    table = (site / "lthcs" / "table" / "lthcs-table.js").read_text(encoding="utf-8")
    assert "'../../data/lthcs/snapshots'" in table
    shared = (site / "lthcs" / "lthcs_tab" / "lthcs-detail.js").read_text(encoding="utf-8")
    assert "'../../data/lthcs/variable_detail'" in shared
    # The card view sits where it does in the repo and is copied verbatim.
    card = site / "lthcs" / "lthcs-detail.js"
    assert card.read_bytes() == (ROOT / "lthcs_tab" / "lthcs-detail.js").read_bytes()
    # Already-deep references (the heatmap) are never deepened twice.
    assert stage.DATA_REF.sub("X", "'../../data/lthcs/x'") == "'../../data/lthcs/x'"
    assert stage.DATA_REF.sub("X", "'../data/lthcs/x'") == "'X/x'"


def test_no_page_builds_a_data_url_from_bare_dots():
    """The rewrite only sees the literal '../data/lthcs'. A URL assembled as
    '..' + path or `../${path}` would escape it and land on a dead copy, so
    data URLs must start from DATA_ROOT. The two non-data fallbacks below are
    the only exceptions (each sits behind a startsWith('data/lthcs') check)."""
    bad = re.compile(r"""['"`]\.\.['"`]\s*\+|`\.\./\$\{""")
    allowed = (": '..' + endpoint;", ": `../${latest.path}`;")
    hits = []
    for d in PAGE_DIRS:
        for f in _text_files(ROOT / d):
            if "mockups" in f.parts:
                continue
            for line in f.read_text(encoding="utf-8").splitlines():
                code = line.split("//", 1)[0].strip()
                if bad.search(code) and not code.startswith(allowed):
                    hits.append(f"{f.relative_to(ROOT)}: {line.strip()}")
    assert not hits, hits


def test_every_directory_a_page_reads_is_on_the_allow_list():
    allowed = set(stage.DATA_WHOLE) | set(stage.DATA_FILES) | set(stage.DATA_LATEST) | {"file_index.json"}
    seen = set()
    for d in PAGE_DIRS:
        for f in _text_files(ROOT / d):
            if "mockups" in f.parts:
                continue
            for m in REF.finditer(f.read_text(encoding="utf-8")):
                top = re.split(r"[/$<{]", m.group(2).rstrip("."))[0]
                if top:
                    seen.add((top, str(f.relative_to(ROOT))))
    missing = sorted((t, f) for t, f in seen if t not in allowed)
    assert not missing, f"pages read data/lthcs entries that are not staged: {missing}"
    assert len({t for t, _ in seen}) >= 15


def test_mockups_are_not_published(staged):
    site, _ = staged
    assert (ROOT / "lthcs_tab" / "mockups").is_dir()
    assert not (site / "lthcs" / "mockups").exists()
    assert not (site / "lthcs" / "lthcs_tab" / "mockups").exists()
    assert (site / "lthcs" / "heatmap" / "index.html").is_file()


def test_dated_dirs_keep_the_newest_n_and_unread_entries_stay_out(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    for name in ("variable_detail", "insider"):
        (src / name).mkdir(parents=True)
        for day in range(1, 11):
            (src / name / f"2026-10-{day:02d}.json").write_text("{}")
    (src / "insider" / "README.md").write_text("not dated")
    (src / "snapshots").mkdir()
    for day in range(1, 11):
        (src / "snapshots" / f"2026-10-{day:02d}.json").write_text("{}")
    (src / "snapshots" / "index.json").write_text("{}")
    (src / "universe_candidate" / "wave_a").mkdir(parents=True)
    (src / "universe_candidate" / "wave_a" / "x.json").write_text("{}")
    (src / "universe.json").write_text("{}")
    (src / "sp500_candidate_seed.json").write_text("{}")

    report = stage.stage_data(src, dst, keep_latest=3)

    assert sorted(p.name for p in (dst / "variable_detail").iterdir()) == [
        "2026-10-08.json", "2026-10-09.json", "2026-10-10.json"]
    assert sorted(p.name for p in (dst / "insider").iterdir()) == [
        "2026-10-08.json", "2026-10-09.json", "2026-10-10.json"]
    assert len(list((dst / "snapshots").iterdir())) == 11          # whole, index included
    assert (dst / "universe.json").is_file()
    assert not (dst / "universe_candidate").exists()
    assert not (dst / "sp500_candidate_seed.json").exists()
    assert report["latest"]["insider"] == ["2026-10-10", "2026-10-09", "2026-10-08"]


def test_file_index_describes_the_published_tree(tmp_path):
    dst = tmp_path / "lthcs"
    (dst / "variable_detail").mkdir(parents=True)
    (dst / "variable_detail" / "2026-10-06.json").write_text("{}")
    (dst / "snapshots").mkdir()
    (dst / "snapshots" / "2026-10-05.json").write_text("{}")
    (dst / "snapshots" / "2026-10-06.json").write_text("{}")
    idx = stage.write_file_index(ROOT, dst, 7)
    on_disk = json.loads((dst / "file_index.json").read_text())
    assert on_disk == idx
    assert idx["dated"]["variable_detail"]["dates"] == ["2026-10-06"]
    assert idx["dated"]["snapshots"]["dates"] == ["2026-10-06", "2026-10-05"]
    assert idx["published_latest_only"] == {"dirs": sorted(stage.DATA_LATEST), "keep_latest": 7}
    assert "kept in the repository" in idx["note"]


def test_pages_yml_stages_lthcs_once_through_the_script():
    text = PAGES_YML.read_text(encoding="utf-8")
    assert "cp -R data/lthcs" not in text
    assert "_site/lthcs/data" not in text
    wf = yaml.safe_load(text)
    steps = wf["jobs"]["build"]["steps"]
    stage_step = next(s for s in steps if s.get("name") == "Stage site directory")
    assert stage_step["run"].count("python scripts/stage_lthcs_site.py --repo . --site _site") == 1
    names = [s.get("name") for s in steps]
    # The deploy-time indexes must exist before the stager copies them.
    idx_step = next(i for i, s in enumerate(steps) if "build_lthcs_site_index.py" in (s.get("run") or ""))
    assert idx_step < names.index("Stage site directory")
