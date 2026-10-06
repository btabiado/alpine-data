"""Offline guards for the 2026-10-04 UX-audit P2 polish.

Each test pins one fix so a later edit can't quietly undo it: tab history
(pushState + popstate), the static-mirror 404 page, the Summit/Landscape
single-iframe view switch, the band-pill ink helper, the stale-sentiment
grey-out threshold (executed in V8), and the removed dead links.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _template_js(path: Path) -> str:
    src = path.read_text(encoding="utf-8")
    marker = 'HTML_TEMPLATE = r"""'
    return src[src.index(marker) + len(marker):]


def _extract(js: str, name: str) -> str:
    m = re.search(r"^function %s\s*\(" % re.escape(name), js, re.M)
    assert m, f"function {name}() not found"
    i = js.index("{", m.end() - 1)
    depth = 0
    for j in range(i, len(js)):
        if js[j] == "{":
            depth += 1
        elif js[j] == "}":
            depth -= 1
            if depth == 0:
                return js[m.start():j + 1]
    raise AssertionError("unbalanced")  # pragma: no cover


@pytest.fixture(scope="module", params=["app.py", "v2/app.py"])
def dash_js(request) -> str:
    return _template_js(ROOT / request.param)


# ------------------------------------------------------------ navigation ---

def test_tab_changes_push_history_and_popstate_routes_back(dash_js):
    fn = _extract(dash_js, "selectTab")
    assert "history.pushState({ tab: t }" in fn
    # Only the boot paint replaces; it flips the flag itself.
    assert "if (_tabHistReplace !== false) history.replaceState" in fn
    assert "_tabHistReplace = false;" in fn
    assert re.search(r"^var _tabHistReplace = true;", dash_js, re.M)
    assert "window.addEventListener('popstate', () => {" in dash_js
    assert "location.hash ? _tabFromHash() : 'overview'" in dash_js


def test_v2_tabs_support_arrow_keys():
    js = _template_js(ROOT / "v2" / "app.py")
    i = js.index("document.querySelectorAll('.tab').forEach(b => {")
    block = js[i:i + 2500]
    for key in ("ArrowRight", "ArrowLeft", "Home", "End"):
        assert key in block


def test_health_tabs_support_arrow_keys_and_skip_api_on_mirror():
    html = (ROOT / "health" / "index.html").read_text(encoding="utf-8")
    assert "e.key === 'ArrowRight'" in html and "e.key === 'ArrowLeft'" in html
    assert "const HAS_LIVE_SERVER" in html
    assert "if (!HAS_LIVE_SERVER) throw" in html


def test_custom_404_is_staged_and_mobile_friendly():
    page = (ROOT / "site_static" / "404.html").read_text(encoding="utf-8")
    assert 'name="viewport"' in page
    assert "color-scheme: dark" in page
    # Served for missing paths at ANY depth: links must be absolute.
    hrefs = re.findall(r'href="([^"]+)"', page)
    assert hrefs and all(h.startswith("/alpine-data/") for h in hrefs)
    yml = (ROOT / ".github" / "workflows" / "pages.yml").read_text(encoding="utf-8")
    assert "cp site_static/404.html _site/404.html" in yml


# ------------------------------------------------- landscape / summit -----

def test_landscape_embeds_summit_once_and_switches_views_by_message():
    src = (ROOT / "landscape" / "build.py").read_text(encoding="utf-8")
    assert src.count('<iframe class="summit-frame"') == 1
    assert "postMessage({type:'summit-view',view:view},location.origin)" in src
    summit = (ROOT / "snowflake_summit" / "build.py").read_text(encoding="utf-8")
    assert "function setSummitView(v)" in summit
    assert "if(e.origin!==location.origin) return;" in summit


def test_built_pages_match_their_builders():
    """dashboard.html files are committed build outputs; the fixes must be in
    the shipped HTML, not only in build.py."""
    land = (ROOT / "landscape" / "dashboard.html").read_text(encoding="utf-8")
    assert 'id="summitFrame"' in land
    for p in ("snowflake_summit/dashboard.html", "snowflake_summit/dashboard_standalone.html"):
        html = (ROOT / p).read_text(encoding="utf-8")
        assert '<h3 class="sec"' not in html          # section titles are h2 now
        assert 'aria-label="Search all vendors"' in html
        assert "function setSummitView(v)" in html
        assert "card(v, i)" in html                   # badge = position in list


DEAD = {
    "snowflake_summit/news.json": [
        "https://www.alation.com/blog/snowflake-summit-2026-guide/",
        "https://www.pr.com/press-release/970209",
        "https://www.redpanda.com/events/snowflake-summit-2",
        "https://www.fortino.vc/news/vaultspeed-secures-159-million-series-funding-accelerate-growth-its-automated-data",
    ],
    "landscape/non_summit_vendors.json": [
        "https://www.5x.co",
        "https://www.hitachivantara.com/en-us/products/pentaho-platform/pentaho-data-integration.html",
    ],
}


@pytest.mark.parametrize("path,urls", DEAD.items())
def test_verified_dead_links_are_gone(path, urls):
    text = (ROOT / path).read_text(encoding="utf-8")
    json.loads(text)  # still valid JSON
    for u in urls:
        assert f'"{u}"' not in text, f"{u} (404) is back in {path}"


# -------------------------------------------------------- contrast / ink ---

def test_band_ink_is_dark_on_light_fills():
    js = (ROOT / "lthcs_tab" / "lthcs-sparkline.js").read_text(encoding="utf-8")
    fn = js[js.index("export function bandInkForScore"):]
    fn = fn[:fn.index("\n}\n") + 2]
    # bandInkForScore resolves the band via lthcs-bands.js (live weights.json
    # cutoffs); load that module's pure helpers, minus the fetch loader.
    bands = (ROOT / "lthcs_tab" / "lthcs-bands.js").read_text(encoding="utf-8")
    bands = bands[:bands.index("const WEIGHTS_URL")] + bands[bands.index("/** Current band config"):]
    mr = pytest.importorskip("py_mini_racer", reason="V8 needed")
    ctx = mr.MiniRacer()
    ctx.eval(bands.replace("export ", "").replace("'use strict';", ""))
    # Fixed test band set (tests/lthcs/band_fixture.py: elite 85+, constructive
    # 70-79, monitor 60-69, weakening 50-59, review 0-49) in place of the
    # module's fallback, which mirrors the live weights.json and moves with a
    # recalibration.
    from tests.lthcs.band_fixture import FIXTURE_SCORE_BANDS
    ctx.eval("current = " + json.dumps(FIXTURE_SCORE_BANDS) + ";")
    ctx.eval(fn.replace("export ", ""))
    assert ctx.call("bandInkForScore", 95) == "#fff"     # elite (navy)
    assert ctx.call("bandInkForScore", 75) == "#111"     # constructive (gold)
    assert ctx.call("bandInkForScore", 65) == "#111"     # monitor (amber)
    assert ctx.call("bandInkForScore", 55) == "#fff"     # weakening
    assert ctx.call("bandInkForScore", 40) == "#fff"     # review
    assert ctx.call("bandInkForScore", None) is None


def test_strong_sell_text_is_not_the_low_contrast_red(dash_js):
    assert "label:'STRONG SELL', color:'#b91c1c'" not in dash_js
    fn = _extract(dash_js, "signalColor")
    assert "return '#b91c1c';" not in fn


def test_dark_only_pages_declare_color_scheme():
    for p in ("app.py", "v2/app.py", "health/index.html", "real_estate/index.html",
              "lthcs_tab/lthcs.css", "lthcs_tab_v2/lthcs-v2.css",
              "snowflake_summit/build.py", "landscape/build.py"):
        assert re.search(r"color-scheme:\s*dark", (ROOT / p).read_text(encoding="utf-8")), p


def test_meta_csp_no_longer_carries_frame_ancestors():
    for page in ROOT.glob("lthcs_*/**/*.html"):
        assert "frame-ancestors" not in page.read_text(encoding="utf-8"), page


# ------------------------------------------- stale sentiment (V8, V1+V2) ---

@pytest.fixture(scope="module", params=["app.py", "v2/app.py"])
def paint(request):
    mr = pytest.importorskip("py_mini_racer", reason="V8 needed")
    js = _template_js(ROOT / request.param)
    bodies = "\n".join(_extract(js, n) for n in
                       ("freshness", "freshnessDayUTC", "freshnessYmd", "paintSentimentCard"))
    ctx = mr.MiniRacer()
    ctx.eval("""
    function mkEl(){ return { style:{}, textContent:'', title:'', _cls:new Set(),
      setAttribute(k,v){ this[k]=v; }, removeAttribute(k){ this[k]=''; },
      classList:{ _s:null, toggle(c,on){ this._s=this._s||new Set(); if(on) this._s.add(c); else this._s.delete(c); },
                  contains(c){ return !!(this._s&&this._s.has(c)); } } }; }
    var els = {};
    var document = { getElementById(id){ return els[id] || (els[id] = mkEl()); } };
    function paintCompositeFreshness(){}
    function __run(nowIso, date){
      els = {};
      const RealDate = Date, nowMs = RealDate.parse(nowIso);
      class D extends RealDate { constructor(...a){ if(!a.length) super(nowMs); else super(...a); } static now(){ return nowMs; } }
      const Date_ = D;
      return (function(Date){
        %s
        paintSentimentCard('x', 40, 'BULLISH', '#22c55e', 50, 30, 20, 'sub', date ? {date: date} : null);
        return { color: els.xScore.style.color, stale: els.xCard.classList.contains('sentiment-stale') };
      })(Date_);
    }
    """ % bodies)
    return lambda now, date: ctx.call("__run", now, date)


def test_sentiment_score_greys_out_after_seven_days(paint):
    now = "2026-10-04T12:00:00Z"
    assert paint(now, "2026-09-27") == {"color": "#22c55e", "stale": False}   # 7d: still live
    assert paint(now, "2026-09-26") == {"color": "var(--muted)", "stale": True}  # 8d: greyed
    assert paint(now, None) == {"color": "#22c55e", "stale": False}          # undated: untouched


def test_research_strip_uses_santiment_dates(dash_js):
    fn = _extract(dash_js, "socialFreshness")
    assert "fLast(ser)" in fn and "fMin(lasts)" in fn
    assert "'Santiment data through'" in fn


def test_money_flow_tooltip_has_no_developer_text():
    fn = _extract(_template_js(ROOT / "app.py"), "moneyFlowFreshness")
    assert "_composite_as_of" not in fn
    assert "clock read" not in fn
