"""/api-universe/: the page, its deploy-time data, and the wiring around it.

The page (api_universe/index.html) draws every Data Sources catalog entry on a
radial map from ./data.json, which scripts/build_api_universe.py builds in
pages.yml from the committed health/api_catalog.json and
health/catalog_health.json. These tests pin:

* the domain mapping: every committed category lands in exactly one domain,
  and an unknown, unnamed or ambiguous category stops the build by name;
* the counts: everything the page reports equals what the catalog says;
* the publish: pages.yml builds the data before staging, and the stage step
  ships the page and its data (and still ships the page without the data);
* the links: the V1 dashboard's tab bar, /health/, the old /health/apis.html
  address and the 404 page all point at it, and the daily UX audit loads it;
* the page itself: no count is written into it, it fetches its data, and it
  says so on the page when the data or d3 does not arrive.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import os
import re
import subprocess
import sys
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
PAGE = ROOT / "api_universe" / "index.html"
SCRIPT = ROOT / "scripts" / "build_api_universe.py"
CATALOG = ROOT / "health" / "api_catalog.json"
HEALTH = ROOT / "health" / "catalog_health.json"
PAGES_YML = ROOT / ".github" / "workflows" / "pages.yml"
DATA_REL = "api_universe/data.json"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod   # @dataclass needs the module registered
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def bau():
    return _load("_build_api_universe", SCRIPT)


@pytest.fixture(scope="module")
def catalog():
    return json.loads(CATALOG.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def health():
    return json.loads(HEALTH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def built(bau, catalog, health):
    return bau.build(catalog, health)


@pytest.fixture(scope="module")
def page() -> str:
    return PAGE.read_text(encoding="utf-8")


def _entries(catalog):
    return [(c["category"], e) for c in catalog["categories"] for e in c["entries"]]


def _norm(u):
    return (u or "").strip().rstrip("/").lower()


def _stage_step_run() -> str:
    wf = yaml.safe_load(PAGES_YML.read_text(encoding="utf-8"))
    step = next(s for s in wf["jobs"]["build"]["steps"] if s.get("name") == "Stage site directory")
    return step["run"]


# ------------------------------------------------------------ domain mapping


def test_every_committed_category_maps_to_exactly_one_domain(bau, catalog):
    used = Counter()
    for c in catalog["categories"]:
        used[bau.domain_of(c["category"])] += 1     # raises on none / several
    names = list(bau.DOMAINS)
    assert set(used) == set(range(len(names))), (
        f"domain(s) with no category: {[names[i] for i in range(len(names)) if i not in used]}")


def test_no_domain_prefix_is_stale(bau, catalog):
    """A prefix no category uses is not harmless: it would silently claim the
    next new category that happens to start the same way."""
    cats = [c["category"] for c in catalog["categories"]]
    stale = [(d, p) for d, ps in bau.DOMAINS.items() for p in ps
             if not any(c.startswith(p) for c in cats)]
    assert not stale, f"prefixes that match no catalog category: {stale}"


def test_an_unknown_category_fails_loudly_and_names_every_offender(bau, catalog, health):
    cat = copy.deepcopy(catalog)
    sample = copy.deepcopy(cat["categories"][0]["entries"][0])
    for name in ("Underwater basket weaving", "Zymurgy & brewing"):
        cat["categories"].append({"category": name, "count": 1, "entries": [sample]})
    cat["meta"]["total"] += 2
    with pytest.raises(bau.CatalogError) as ei:
        bau.build(cat, health)
    msg = str(ei.value)
    assert "Underwater basket weaving" in msg and "Zymurgy & brewing" in msg
    assert "DOMAINS" in msg and "scripts/build_api_universe.py" in msg


@pytest.mark.parametrize("record", [
    {"count": 0, "entries": []},                      # no "category" key at all
    {"category": "", "count": 0, "entries": []},       # empty name
    {"category": None, "count": 0, "entries": []},
])
def test_a_missing_category_name_fails(bau, catalog, health, record):
    cat = copy.deepcopy(catalog)
    cat["categories"].append(record)
    with pytest.raises(bau.CatalogError, match="has no name"):
        bau.build(cat, health)


def test_a_category_matching_two_domains_fails(bau, catalog, health, monkeypatch):
    monkeypatch.setitem(bau.DOMAINS, "Overlap", ("Agent",))
    with pytest.raises(bau.CatalogError, match="more than one domain"):
        bau.build(catalog, health)


def test_no_categories_fails(bau, health):
    with pytest.raises(bau.CatalogError, match="no categories"):
        bau.build({"meta": {}, "categories": []}, health)


@pytest.mark.parametrize("mutate,match", [
    (lambda c: c["meta"].__setitem__("total", c["meta"]["total"] + 1), "meta.total"),
    (lambda c: c["categories"][0].__setitem__("count", c["categories"][0]["count"] + 1), "says count="),
    (lambda c: c["categories"][0]["entries"][0].__setitem__("s", "Retired"), "has status"),
    (lambda c: c["categories"].append(copy.deepcopy(c["categories"][0])), "more than once"),
])
def test_an_inconsistent_catalog_fails(bau, catalog, health, mutate, match):
    cat = copy.deepcopy(catalog)
    mutate(cat)
    with pytest.raises(bau.CatalogError, match=match):
        bau.build(cat, health)


# ------------------------------------------------------------------- counts


def test_counts_match_the_catalog(bau, built, catalog, health):
    entries = _entries(catalog)
    m = built["meta"]
    assert m["total"] == len(entries) == len(built["apis"]) == catalog["meta"]["total"]
    assert m["categories"] == len(catalog["categories"]) == len(built["cats"])
    assert m["domains"] == len(bau.DOMAINS) == len(built["domains"])
    assert m["in_use"] == sum(e["s"] == "In use" for _, e in entries) == sum(a[6] for a in built["apis"])
    assert m["keyless"] == sum(bool(e["kl"]) for _, e in entries) == sum(a[7] for a in built["apis"])
    flagged = {_norm(r["url"]) for k in ("dead", "unreachable") for r in health[k]}
    assert m["link_problems"] == sum(_norm(e["u"]) in flagged for _, e in entries)
    assert m["link_problems"] == sum(1 for a in built["apis"] if a[10])
    assert m["checked"] == health["checked_at"]
    assert m["health"] == {k: health["counts"][k] for k in ("ok", "gated", "dead", "unreachable")}
    assert m["source"] == {"catalog": "health/api_catalog.json", "health": "health/catalog_health.json"}


def test_every_entry_is_drawn_once_under_its_own_category(built, catalog):
    cats = built["cats"]
    drawn = Counter((cats[a[0]][0], a[1], a[9]) for a in built["apis"])
    listed = Counter((c, e["n"], e["u"]) for c, e in _entries(catalog))
    assert drawn == listed
    per_cat = Counter(cats[a[0]][0] for a in built["apis"])
    assert per_cat == Counter({c["category"]: len(c["entries"]) for c in catalog["categories"]})


def test_categories_are_grouped_by_domain_in_domain_order(built):
    doms = [d for _, d in built["cats"]]
    assert doms == sorted(doms)
    for (name, d) in built["cats"]:
        assert 0 <= d < len(built["domains"]), name


def test_link_problems_agree_with_the_workbook(bau, catalog, health):
    """/health/'s workbook and this page must flag the same rows."""
    pytest.importorskip("openpyxl")
    xl = _load("_build_catalog_xlsx_for_universe", ROOT / "scripts" / "build_catalog_xlsx.py")
    status_for = xl.build_status_map(health)
    bad = bau.link_problems(health)
    for _, e in _entries(catalog):
        assert (bau._norm(e["u"]) in bad) == (status_for(e["u"]) != "OK / not flagged"), e["n"]


def test_rows_follow_FIELDS_and_the_page_reads_them_in_that_order(bau, built, page):
    assert all(len(a) == len(bau.FIELDS) for a in built["apis"])
    assert built["fields"] == list(bau.FIELDS)
    line = next(ln for ln in page.splitlines() if "const APIS = DATA.apis.map(" in ln)
    assert sorted({int(i) for i in re.findall(r"\ba\[(\d+)\]", line)}) == list(range(len(bau.FIELDS)))


def test_cli_writes_the_data_and_exits_by_kind_of_failure(tmp_path, catalog):
    out = tmp_path / "out" / "data.json"
    r = subprocess.run([sys.executable, str(SCRIPT), "--out", str(out)],
                       capture_output=True, text=True, cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["meta"]["total"] == catalog["meta"]["total"]
    assert f"{catalog['meta']['total']:,} APIs" in r.stdout

    bad = copy.deepcopy(catalog)
    bad["categories"][0]["category"] = "Something nobody mapped"
    bad_path = tmp_path / "catalog.json"
    bad_path.write_text(json.dumps(bad), encoding="utf-8")
    out2 = tmp_path / "out2" / "data.json"
    r = subprocess.run([sys.executable, str(SCRIPT), "--catalog", str(bad_path), "--out", str(out2)],
                       capture_output=True, text=True, cwd=tmp_path)
    assert r.returncode == 1
    assert "Something nobody mapped" in r.stderr
    assert not out2.exists(), "a failed build must not leave a data file behind"

    r = subprocess.run([sys.executable, str(SCRIPT), "--health", str(tmp_path / "nope.json"),
                        "--out", str(out2)], capture_output=True, text=True, cwd=tmp_path)
    assert r.returncode == 2 and "input missing" in r.stderr


# ------------------------------------------------------------- publishing


def test_data_json_is_built_not_committed():
    ignored = subprocess.run(["git", "check-ignore", "-q", "--no-index", DATA_REL], cwd=ROOT)
    assert ignored.returncode == 0, f"{DATA_REL} is not gitignored"
    tracked = subprocess.run(["git", "ls-files", "--error-unmatch", DATA_REL], cwd=ROOT,
                             capture_output=True, text=True)
    assert tracked.returncode != 0, f"{DATA_REL} is tracked; it is built at deploy time"


def test_pages_builds_the_data_before_staging_and_never_blocks_the_deploy():
    steps = yaml.safe_load(PAGES_YML.read_text(encoding="utf-8"))["jobs"]["build"]["steps"]
    idx = lambda pred: next(i for i, s in enumerate(steps) if pred(s))  # noqa: E731
    build = idx(lambda s: "scripts/build_api_universe.py" in (s.get("run") or ""))
    stage = idx(lambda s: s.get("name") == "Stage site directory")
    assert build < stage
    step = steps[build]
    assert step.get("continue-on-error") is True
    assert "::error title=API Universe data not built::" in step["run"]
    assert "exit 1" in step["run"], "a failed build should show as a soft-failed step"


def _api_universe_stage_block() -> str:
    """The `if [ -f api_universe/index.html ]` block of the stage step, as run."""
    lines = _stage_step_run().splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.strip() == "if [ -f api_universe/index.html ]; then")
    depth, out = 0, []
    for ln in lines[start:]:
        s = ln.strip()
        out.append(ln)
        if s.startswith("if "):
            depth += 1
        elif s == "fi":
            depth -= 1
            if depth == 0:
                return "\n".join(out)
    raise AssertionError("unterminated api_universe block in the stage step")


def _run_stage(repo: Path) -> subprocess.CompletedProcess:
    script = "set -e\nshopt -s nullglob\nmkdir -p _site\n" + _api_universe_stage_block() + "\n"
    return subprocess.run(["bash", "-c", script], cwd=repo, capture_output=True, text=True)


def test_stage_step_publishes_the_page_and_its_data(tmp_path, bau, catalog):
    src = tmp_path / "api_universe"
    src.mkdir()
    (src / "index.html").write_bytes(PAGE.read_bytes())
    assert bau.main(["--out", str(src / "data.json")]) == 0
    (src / "notes.txt").write_text("not for publishing")

    r = _run_stage(tmp_path)
    assert r.returncode == 0, r.stderr
    site = tmp_path / "_site" / "api-universe"
    assert (site / "index.html").read_bytes() == PAGE.read_bytes()
    data = json.loads((site / "data.json").read_text(encoding="utf-8"))
    assert data["meta"]["total"] == catalog["meta"]["total"]
    assert sorted(p.name for p in site.iterdir()) == ["data.json", "index.html"], "allow-list only"


def test_stage_step_still_publishes_the_page_without_data(tmp_path):
    src = tmp_path / "api_universe"
    src.mkdir()
    (src / "index.html").write_bytes(PAGE.read_bytes())
    r = _run_stage(tmp_path)
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "_site" / "api-universe" / "index.html").exists()
    assert not (tmp_path / "_site" / "api-universe" / "data.json").exists()
    assert "::warning title=API Universe published without data::" in r.stdout


def test_stage_block_is_a_no_op_without_the_page(tmp_path):
    r = _run_stage(tmp_path)
    assert r.returncode == 0, r.stderr
    assert not (tmp_path / "_site" / "api-universe").exists()


def test_data_health_does_not_treat_it_as_a_feed(tmp_path, monkeypatch):
    """Built from committed files on every deploy: there is nothing to go
    stale, so the watchdog must not discover it (and then call it unwatched)."""
    dh = _load("_dh_api_universe", ROOT / "scripts" / "data_health.py")
    (tmp_path / "api_universe").mkdir()
    (tmp_path / "api_universe" / "data.json").write_text("{}")
    (tmp_path / "data-example.json").write_text("{}")
    monkeypatch.setattr(dh, "REPO_ROOT", tmp_path)
    assert dh.discover() == ["data-example.json"]


# ------------------------------------------------------------------- links


NAV_LINK = re.compile(
    r'<a class="tab tab--solo tab--exit" href="api-universe/"(?P<attrs>[^>]*)>'
    r'API Universe<span class="exitmark" aria-hidden="true">&#8599;</span></a>')


@pytest.fixture(scope="module")
def v1_tpl() -> str:
    src = (ROOT / "app.py").read_text(encoding="utf-8")
    marker = 'HTML_TEMPLATE = r"""'
    return src[src.index(marker) + len(marker):]


def test_dashboard_tab_bar_links_to_it(v1_tpl):
    jumps = v1_tpl[v1_tpl.index('<span class="tabnav-jumps"'):]
    jumps = jumps[:jumps.index("</span>\n</div>")]
    m = NAV_LINK.search(jumps)
    assert m, "API Universe link missing from the tab bar's jump group"
    attrs = m.group("attrs")
    assert "data-tab" not in attrs and "aria-selected" not in attrs and "role=" not in attrs
    assert "leaves this dashboard" in attrs
    # It sits after the other exit, Summit.
    assert jumps.index('data-tab="summit"') < m.start()
    assert "a.tab--exit{text-decoration:none}" in v1_tpl


def test_tab_wiring_leaves_the_real_link_alone(v1_tpl):
    """The click/keydown wiring and the aria-selected loop must select
    .tab[data-tab]; on plain .tab they would hijack the link's Enter key and
    stamp aria-selected (not allowed on a link) onto it."""
    assert "document.querySelectorAll('.tab[data-tab]').forEach(b => {\n  b.addEventListener('click'" in v1_tpl
    assert "document.querySelectorAll('.tab').forEach(b => {" not in v1_tpl
    sel = v1_tpl[v1_tpl.index("function selectTab(t)"):]
    sel = sel[:sel.index("syncTabGroups();")]
    assert "document.querySelectorAll('.tab[data-tab]').forEach(el => {\n    const isActive" in sel


def test_generated_dashboard_has_the_link():
    built = ROOT / "dashboard.html"
    if not built.exists():
        if os.environ.get("REQUIRE_DASHBOARD") == "1":
            pytest.fail("dashboard.html not built but REQUIRE_DASHBOARD=1")
        pytest.skip("dashboard.html not built yet (python app.py --no-open)")
    assert NAV_LINK.search(built.read_text(encoding="utf-8")), "nav link missing from dashboard.html"


def test_other_pages_link_to_it():
    health = (ROOT / "health" / "index.html").read_text(encoding="utf-8")
    assert health.count('href="../api-universe/"') >= 2   # header + catalog pane
    assert 'href="../api-universe/"' in (ROOT / "health" / "apis.html").read_text(encoding="utf-8")
    assert 'href="/alpine-data/api-universe/"' in (ROOT / "site_static" / "404.html").read_text(encoding="utf-8")


def test_daily_ux_audit_loads_it():
    mjs = (ROOT / "scripts" / "daily_audit_ux.mjs").read_text(encoding="utf-8")
    pages = mjs[mjs.index("export const PAGES = ["):]
    pages = pages[:pages.index("];")]
    assert "{ key: 'api-universe', path: '/api-universe/' }" in pages
    dar = _load("_dar_api_universe", ROOT / "scripts" / "daily_audit_report.py")
    assert dar.PAGE_LABELS["api-universe"] == "/api-universe/"
    assert "api-universe" not in dar.TABBED_PAGES


# --------------------------------------------------------------- the page


class _HumanText(HTMLParser):
    """Text a reader can see or hear: text nodes outside <script>/<style>, the
    labelling attributes, and the string literals inside inline scripts."""
    ATTRS = {"aria-label", "title", "alt", "placeholder", "content", "value"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self._in = [], None

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._in = tag
        for k, v in attrs:
            if k in self.ATTRS and v and not (tag == "meta" and k == "content" and "width=" in v):
                self.parts.append(v)

    def handle_endtag(self, tag):
        if tag == self._in:
            self._in = None

    def handle_data(self, data):
        if self._in == "script":
            for m in re.findall(r"'([^'\n]*)'|\"([^\"\n]*)\"|`([^`]*)`", data):
                # ${...} is computed, not written: drop it (and the layout
                # arithmetic inside it) before looking for numbers.
                self.parts.append(re.sub(r"\$\{[^}]*\}", " ", "".join(m)))
        elif self._in is None:
            self.parts.append(data)


def test_page_writes_no_count_into_itself(page, built):
    p = _HumanText()
    p.feed(page)
    text = "\n".join(p.parts)
    m = built["meta"]
    counts = {m["total"], m["categories"], m["domains"], m["in_use"], m["keyless"],
              m["link_problems"], *m["health"].values()}
    # Small counts (8 domains) are caught by the phrase check below; a bare
    # single digit cannot be told apart from markup.
    for v in sorted(c for c in counts if c >= 20):
        for form in {str(v), f"{v:,}"}:
            hit = re.search(rf"(?<![\w,.]){re.escape(form)}(?![\w,]|\.\d)", text)
            assert not hit, f"count {form} is written into the page: …{text[max(0, hit.start()-40):hit.end()+40]}…"
    phrase = re.search(r"\b\d[\d,]*\s+(APIs?|categories|domains|in use|keyless|unreachable|dead|gated)\b", text)
    assert not phrase, f"hard-coded count phrase: {phrase.group(0)!r}"


def test_page_is_a_full_document(page):
    assert page.lstrip().lower().startswith("<!doctype html>")
    assert '<meta charset="utf-8">' in page
    assert re.search(r'<meta name="viewport" content="[^"]*width=device-width[^"]*viewport-fit=cover', page)
    assert "<title>" in page and "<body>" in page and "</html>" in page
    # The page's own light/dark tokens, guarded so a future data-theme toggle works.
    assert "@media (prefers-color-scheme: dark){ :root:not([data-theme=\"light\"])" in page
    assert ':root[data-theme="dark"]' in page
    assert re.search(r"body\{[^}]*background:var\(--bg\)", page)


def test_page_fetches_its_data_and_shows_failures(page):
    assert "fetch('data.json')" in page
    assert '<script id="data"' not in page and "__DATA__" not in page
    assert 'id="status" role="status"' in page
    assert "function showError(" in page
    assert "typeof window.d3 === 'undefined'" in page           # CDN blocked
    assert "answered HTTP ${r.status}" in page                   # 404 / 5xx
    assert "is not valid JSON" in page and "has no APIs in it" in page


def test_d3_follows_the_site_cdn_convention(page):
    """jsDelivr with an SRI pin and crossorigin, like Chart.js and Leaflet."""
    tags = re.findall(r"<script\b[^>]*\bsrc=\"([^\"]+)\"[^>]*>", page)
    assert tags == ["https://cdn.jsdelivr.net/npm/d3@7.9.0/dist/d3.min.js"]
    tag = re.search(r"<script\b[^>]*d3@7\.9\.0[^>]*>", page).group(0)
    assert 'integrity="sha384-CjloA8y00+1SDAUkjs099PVfnY2KmDC2BZnws9kh8D/lX1s46w6EPhpXdqMfjK6i"' in tag
    assert 'crossorigin="anonymous"' in tag
    # The webfont is not allowed to block first paint.
    css = re.search(r'<link rel="stylesheet" href="https://fonts\.googleapis\.com[^>]*>', page).group(0)
    assert 'media="print"' in css and "onload=" in css


def test_page_credits_its_sources_and_links_home(page):
    assert 'href="../"' in page
    assert 'href="../health/api_catalog.json"' in page
    assert 'href="../health/catalog_health.json"' in page
    assert "DATA.meta.checked" in page and "links checked" in page
