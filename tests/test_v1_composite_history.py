"""Contract tests for the V1 (PRODUCTION) composite-index history feature,
the V1 insights-bar freshness stamp, and the V1 Chart.js failure stub.

``app.py`` builds the root ``dashboard.html`` that ``.github/workflows/pages.yml``
deploys — this is the page the user actually looks at. Everything guarded here
landed in the V2 preview first; this file guards the production port, because a
fix that lands in only one frontend is a half fix and is this repo's most
persistent bug.

Three layers, mirroring tests/test_v1_freshness.py:

1. **Python builder** — ``app.py::load_composite_history`` folds
   ``data/composites/<date>.json`` into a per-index series that gets inlined
   into the payload. ``scripts/snapshot_composites.py`` had been writing those
   files every build since PR #23 with nothing reading them.

2. **The shipped JavaScript** — ``compositeHistoryChart`` and friends are
   extracted verbatim from ``app.py``'s HTML template and executed in V8, so
   the assertions run against the code that actually ships rather than a
   reimplementation of it.

3. **Cross-frontend parity + source contracts** — the pieces that must not
   drift from v2/app.py, and the things that must never reappear in the source
   (``as of ${DATA.generated_at}``, a bare ``fMax`` in a composite stamp).

The failure mode being guarded is the time-axis version of the whale defect:
a value observed weeks ago being PLOTTED at the day it happened to be captured.
That would turn the one archive built specifically to expose frozen sources
into a picture of a perfectly healthy daily cadence. Every assertion below is
some form of "the page must not claim more than the archive observed".
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
V1_APP = ROOT / "app.py"
V2_APP = ROOT / "v2" / "app.py"


# ---------------------------------------------------------------- helpers ---


@pytest.fixture(scope="module")
def v1app():
    """Import ``app.py`` under its own module name."""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location("v1_app_history_under_test", V1_APP)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as e:  # pragma: no cover - dependency guard
        pytest.skip(f"app.py not importable here: {e}")
    return mod


@pytest.fixture(scope="module")
def v1_js() -> str:
    """The inline dashboard JS, taken from the builder's template string."""
    src = V1_APP.read_text(encoding="utf-8")
    marker = 'HTML_TEMPLATE = r"""'
    i = src.index(marker)
    return src[i + len(marker):]


def extract_function(js: str, name: str) -> str:
    """Return the full source of top-level ``function name(...){...}``.

    Brace-matched rather than regex-terminated so nested blocks, object
    literals and template strings inside the body survive intact.
    """
    m = re.search(r"^function %s\s*\(" % re.escape(name), js, re.M)
    assert m, f"function {name}() not found in the V1 template"
    start = m.start()
    i = js.index("{", m.end() - 1)
    depth = 0
    for j in range(i, len(js)):
        c = js[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return js[start:j + 1]
    raise AssertionError(f"unbalanced braces extracting {name}()")  # pragma: no cover


def strip_comments(js: str) -> str:
    """Blank out `//` line comments and `/* */` blocks.

    Every check below that asks "does the SOURCE still do X?" has to run on
    code, not prose. The comments in app.py deliberately quote the defects
    they replaced (``as of ${DATA.generated_at}``, ``new Chart(...)``), so a
    naive substring search finds the very strings the fix removed.

    Not a JS parser — it does not understand `//` inside a string literal —
    but the checks it feeds are all "is this token absent", and blanking a
    little too much can only make those checks stricter, never laxer.
    """
    js = re.sub(r"/\*.*?\*/", " ", js, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", js)


def write_archive(tmp_path: Path, snapshots: dict) -> Path:
    """Materialise ``{filename_stem: snapshot_dict}`` as a composites dir."""
    d = tmp_path / "composites"
    d.mkdir(parents=True, exist_ok=True)
    for stem, blob in snapshots.items():
        (d / f"{stem}.json").write_text(json.dumps(blob))
    return d


def entry(score, as_of, stale=False, label=None, note=None):
    return {"score": score, "label": label, "as_of": as_of,
            "stale": stale, "note": note}


def snap(day, indexes):
    return {"as_of": day, "generated_at": day + "T20:00:00Z", "indexes": indexes}


@pytest.fixture
def load(v1app, tmp_path, monkeypatch):
    """Point ``app.DATA_DIR`` at a temp dir and return a loader callable."""
    def _load(snapshots, **kw):
        write_archive(tmp_path, snapshots)
        monkeypatch.setattr(v1app, "DATA_DIR", tmp_path)
        return v1app.load_composite_history(**kw)
    return _load


# ============================ 1. Python loader ==============================


def test_missing_directory_is_an_empty_archive_not_a_crash(v1app, tmp_path, monkeypatch):
    """A repo with no data/composites/ yet must build, not explode."""
    monkeypatch.setattr(v1app, "DATA_DIR", tmp_path / "nope")
    out = v1app.load_composite_history()
    assert out == {"snapshots": 0, "first_snapshot": None,
                   "last_snapshot": None, "indexes": {}}


def test_point_is_dated_by_its_own_as_of_not_the_filename(load):
    """THE core rule. A capture taken today can hold a value observed weeks
    ago; dating it from the filename destroys the only thing this archive
    records."""
    out = load({"2026-08-02": snap("2026-08-02", {"k": entry(5, "2026-07-11")})})
    pts = out["indexes"]["k"]["points"]
    assert [p["as_of"] for p in pts] == ["2026-07-11"]
    # the capture day survives separately, so the UI can show both
    assert pts[0]["snapshot"] == "2026-08-02"


def test_scored_but_undated_is_excluded_and_counted(load):
    """Refusing to guess is the point: an undated score is not 'today'."""
    out = load({"2026-08-02": snap("2026-08-02",
                                   {"k": {"score": 5, "as_of": None}})})
    e = out["indexes"]["k"]
    assert e["points"] == []
    assert e["undated"] == 1
    assert e["dated"] == 0 and e["missing"] == 0


def test_null_entry_is_a_gap_never_interpolated(load):
    out = load({
        "2026-08-01": snap("2026-08-01", {"k": entry(1, "2026-08-01")}),
        "2026-08-02": snap("2026-08-02", {"k": None}),
        "2026-08-03": snap("2026-08-03", {"k": entry(3, "2026-08-03")}),
    })
    e = out["indexes"]["k"]
    assert e["missing"] == 1
    assert [p["as_of"] for p in e["points"]] == ["2026-08-01", "2026-08-03"]


def test_repeat_captures_of_one_as_of_collapse_and_the_later_capture_wins(load):
    """A frozen source must render as one stationary dot, not a week of
    invented daily readings — and the newest recomputation of that one
    observation is the one that survives."""
    out = load({
        "2026-08-01": snap("2026-08-01", {"k": entry(9, "2026-07-30")}),
        "2026-08-02": snap("2026-08-02", {"k": entry(9, "2026-07-30", stale=True)}),
        "2026-08-03": snap("2026-08-03",
                           {"k": entry(9, "2026-07-30", stale=True, note="cache")}),
    })
    pts = out["indexes"]["k"]["points"]
    assert len(pts) == 1
    assert pts[0]["stale"] is True and pts[0]["note"] == "cache"
    assert pts[0]["snapshot"] == "2026-08-03"
    # all three captures are still counted
    assert out["indexes"]["k"]["dated"] == 3


def test_points_sort_by_observation_date_even_when_captured_out_of_order(load):
    out = load({
        "2026-08-01": snap("2026-08-01", {"k": entry(1, "2026-07-20")}),
        "2026-08-02": snap("2026-08-02", {"k": entry(2, "2026-07-05")}),
        "2026-08-03": snap("2026-08-03", {"k": entry(3, "2026-07-12")}),
    })
    assert [p["as_of"] for p in out["indexes"]["k"]["points"]] == [
        "2026-07-05", "2026-07-12", "2026-07-20"]


def test_key_seen_only_as_null_is_still_emitted_with_zero_points(load):
    """'tracked, nothing recorded yet' must be distinguishable from
    'not tracked at all' — the UI renders different copy for each."""
    out = load({"2026-08-02": snap("2026-08-02", {"k": None})})
    assert "k" in out["indexes"]
    assert out["indexes"]["k"]["points"] == []
    assert out["indexes"]["k"]["missing"] == 1


def test_booleans_are_not_scores(load):
    """`isinstance(True, int)` is True in Python; a bool must not plot as 1."""
    out = load({"2026-08-02": snap("2026-08-02", {"k": entry(True, "2026-08-01")})})
    assert out["indexes"]["k"]["points"] == []
    assert out["indexes"]["k"]["missing"] == 1


def test_unparseable_file_is_skipped_not_fatal(load, tmp_path):
    write_archive(tmp_path, {"2026-08-01": snap("2026-08-01",
                                                {"k": entry(1, "2026-08-01")})})
    (tmp_path / "composites" / "2026-08-02.json").write_text("{ not json")
    out = load({"2026-08-01": snap("2026-08-01", {"k": entry(1, "2026-08-01")}),
                "2026-08-03": snap("2026-08-03", {"k": entry(3, "2026-08-03")})})
    assert out["snapshots"] == 2
    assert len(out["indexes"]["k"]["points"]) == 2


def test_max_snapshots_keeps_the_most_recent_files(load):
    snaps = {f"2026-07-{d:02d}": snap(f"2026-07-{d:02d}",
                                      {"k": entry(d, f"2026-07-{d:02d}")})
             for d in range(1, 11)}
    out = load(snaps, max_snapshots=3)
    assert out["snapshots"] == 3
    assert [p["as_of"] for p in out["indexes"]["k"]["points"]] == [
        "2026-07-08", "2026-07-09", "2026-07-10"]


def test_build_payload_inlines_the_archive(v1app):
    """The loader is wired into the payload, not merely defined."""
    src = V1_APP.read_text(encoding="utf-8")
    assert '"composite_history": load_composite_history()' in src


def test_reads_the_real_archive_shape_written_by_snapshot_composites(v1app, tmp_path, monkeypatch):
    """Guard against the writer and the reader disagreeing about the schema:
    parse a file with the exact keys scripts/snapshot_composites.py emits."""
    real = ROOT / "data" / "composites"
    files = sorted(real.glob("*.json")) if real.is_dir() else []
    if not files:  # pragma: no cover - archive not committed in this checkout
        pytest.skip("no committed composite snapshots to read")
    d = tmp_path / "composites"
    d.mkdir(parents=True)
    (d / files[-1].name).write_bytes(files[-1].read_bytes())
    monkeypatch.setattr(v1app, "DATA_DIR", tmp_path)
    out = v1app.load_composite_history()
    assert out["snapshots"] == 1
    blob = json.loads(files[-1].read_text())
    # every key the writer emitted is represented, null or not
    assert set(out["indexes"]) == set(blob["indexes"])


# ====================== 2. The shipped chart JS in V8 =======================


@pytest.fixture(scope="module")
def chart_ctx(v1_js):
    """A V8 context exposing ``chart(points)`` -> SVG string, built from the
    functions app.py actually ships."""
    py_mini_racer = pytest.importorskip(
        "py_mini_racer", reason="V8 needed to execute the shipped JS")
    ctx = py_mini_racer.MiniRacer()
    bodies = "\n".join(
        extract_function(v1_js, n)
        for n in ("compositeHistoryChart", "freshnessDayUTC", "freshnessYmd",
                  "escapeHtml")
    )
    consts = "\n".join(
        m.group(0) for m in re.finditer(
            r"^const COMPOSITE_HISTORY_(?:MIN_TREND_POINTS|GAP_BREAK_DAYS"
            r"|TABLE_ROWS) = \d+;", v1_js, re.M)
    )
    assert "MIN_TREND_POINTS" in consts and "GAP_BREAK_DAYS" in consts
    ctx.eval(consts + "\n" + bodies + "\nfunction chart(p){return compositeHistoryChart(p);}")
    return ctx


def _pt(as_of, score, stale=False, label=None, note=None, snapshot=None):
    return {"as_of": as_of, "score": score, "stale": stale, "label": label,
            "note": note, "snapshot": snapshot or as_of}


def svg_for(ctx, points):
    return ctx.call("chart", points)


def test_two_points_draw_no_trend_line(chart_ctx):
    """Two dots joined by a line is a trend claim the archive cannot support
    yet — the directory only began accumulating recently."""
    out = svg_for(chart_ctx, [_pt("2026-07-01", 1), _pt("2026-07-02", 5)])
    assert out.count("<polyline") == 0
    assert out.count("<circle") == 2


def test_three_points_do_draw_a_line(chart_ctx):
    out = svg_for(chart_ctx, [_pt("2026-07-01", 1), _pt("2026-07-02", 5),
                              _pt("2026-07-03", 3)])
    assert out.count("<polyline") == 1


def test_a_gap_breaks_the_line_into_segments(chart_ctx):
    """A solid line across a three-week hole asserts continuity nobody
    observed."""
    pts = [_pt("2026-07-01", 1), _pt("2026-07-02", 2), _pt("2026-07-03", 3),
           _pt("2026-07-25", 4), _pt("2026-07-26", 5), _pt("2026-07-27", 6)]
    out = svg_for(chart_ctx, pts)
    assert out.count("<polyline") == 2


def test_stale_points_are_hollow_rings_not_solid_dots(chart_ctx):
    out = svg_for(chart_ctx, [_pt("2026-07-01", 1), _pt("2026-07-02", 2, stale=True),
                              _pt("2026-07-03", 3)])
    # hollow == filled with the panel colour and stroked amber
    assert out.count('fill="var(--panel)"') == 1
    assert out.count('stroke="var(--amber)"') == 1
    assert out.count('fill="var(--purple)"') == 2


def test_tooltip_discloses_cache_served_and_the_capture_day(chart_ctx):
    out = svg_for(chart_ctx, [_pt("2026-07-01", 1),
                              _pt("2026-07-02", 2, stale=True, snapshot="2026-07-20"),
                              _pt("2026-07-03", 3)])
    assert "source was cache-served (stale-kept)" in out
    assert "recorded 2026-07-20 (capture day, not the observation)" in out


def test_no_capture_note_when_capture_day_equals_observation(chart_ctx):
    out = svg_for(chart_ctx, [_pt("2026-07-01", 1), _pt("2026-07-02", 2),
                              _pt("2026-07-03", 3)])
    assert "capture day" not in out


def test_x_axis_is_time_proportional_not_index_proportional(chart_ctx):
    """A frozen source must show as a gap. If x were the array index, three
    evenly spaced dots would be drawn for wildly uneven observation dates."""
    out = svg_for(chart_ctx, [_pt("2026-07-01", 0), _pt("2026-07-02", 0),
                              _pt("2026-07-31", 0)])
    xs = [float(m) for m in re.findall(r'<circle cx="([\d.]+)"', out)]
    assert len(xs) == 3
    d01, d12 = xs[1] - xs[0], xs[2] - xs[1]
    # 1 day vs 29 days: the second span must dwarf the first
    assert d12 > d01 * 20


def test_x_axis_labels_are_observation_dates(chart_ctx):
    out = svg_for(chart_ctx, [_pt("2026-07-01", 1, snapshot="2026-08-01"),
                              _pt("2026-07-02", 2, snapshot="2026-08-01"),
                              _pt("2026-07-03", 3, snapshot="2026-08-01")])
    labels = re.findall(r'text-anchor="(?:start|end|middle)"[^>]*>(2026-\d\d-\d\d)<',
                        out)
    assert "2026-07-01" in labels and "2026-07-03" in labels
    assert "2026-08-01" not in labels


def test_single_point_still_renders_and_is_centred(chart_ctx):
    """x1 == x0 would divide by zero; the domain is widened instead."""
    out = svg_for(chart_ctx, [_pt("2026-07-01", 7)])
    assert out.count("<circle") == 1
    assert "NaN" not in out and "Infinity" not in out


def test_flat_series_does_not_divide_by_zero(chart_ctx):
    out = svg_for(chart_ctx, [_pt("2026-07-01", 4), _pt("2026-07-02", 4),
                              _pt("2026-07-03", 4)])
    assert "NaN" not in out and "Infinity" not in out


def test_point_text_is_escaped(chart_ctx):
    out = svg_for(chart_ctx, [_pt("2026-07-01", 1, label="<script>x</script>")])
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


# ===================== 3. Source + cross-frontend contracts =================


def test_insights_bar_no_longer_stamps_the_build_clock(v1_js):
    """THE fix. `as of ${DATA.generated_at}` is a clock read dressed up as a
    data date, over a bar summarising feeds that can be weeks old."""
    fn = strip_comments(extract_function(v1_js, "renderInsights"))
    assert "DATA.generated_at" not in fn, (
        "renderInsights() must not read the build clock")
    assert "insightsFreshness(" in fn


def test_only_the_header_stamp_renders_build_time(v1_js):
    """`#generatedAt` is the ONE place build time is allowed, and it says
    'built' so nobody reads it as a data date."""
    fn = extract_function(v1_js, "setBuildStamp")
    assert "'built ' + (DATA.generated_at" in fn
    # nothing else may phrase generated_at as an "as of"
    code = strip_comments(v1_js)
    assert not re.search(r"as of \$\{DATA\.generated_at", code)
    assert not re.search(r"'as of ' \+ DATA\.generated_at", code)


def test_insights_freshness_takes_the_oldest_feed(v1_js):
    """Rule 3: a composite is only as fresh as its OLDEST input."""
    fn = extract_function(v1_js, "insightsFreshness")
    assert "fMin(" in fn
    assert "fMax(" not in fn


def test_insights_freshness_aliases_the_markets_pool(v1_js):
    """insights.py emits tab='markets'; the page calls that surface
    'overview'. An unmapped tab would silently shrink the minimum."""
    assert "const INSIGHT_TAB_FRESHNESS_ALIAS = { markets: 'overview' };" in v1_js


def test_live_insight_surfaces_carry_a_data_stamp(v1_js):
    """V1's two rendered insight surfaces (#overviewInsights, #aiNewsInsights)
    both assert currency, so both must disclose the age of their DATA."""
    ov = extract_function(v1_js, "renderOverviewInsights")
    assert "insightsFreshness(" in ov and "freshnessHtml(" in ov
    ri = extract_function(v1_js, "renderInsights")
    assert ri.count("insightsFreshness(") >= 2, (
        "both the bar and the AI News inline card must be stamped")


def test_chart_unavailable_stub_exists(v1_js):
    """Chart.js is a CDN dependency behind an SRI pin. Without this stub a
    jsDelivr outage throws `Chart is not defined` mid-renderer and silently
    kills every freshness stamp painted after that line."""
    assert "function ChartUnavailable(canvas)" in v1_js
    assert "if (typeof window.Chart === 'undefined')" in v1_js
    assert "window.Chart.__unavailable = true;" in v1_js


def test_chart_unavailable_stub_is_defined_before_any_renderer_runs(v1_js):
    """It has to beat the first `new Chart(` in source order or it is useless."""
    code = strip_comments(v1_js)
    stub = code.index("function ChartUnavailable(canvas)")
    m = re.search(r"new Chart\(", code)
    assert m and stub < m.start()


def test_chart_unavailable_stub_matches_v2(v1_js):
    """Two frontends, one stub. V2 shipped it first."""
    if not V2_APP.exists():  # pragma: no cover
        pytest.skip("v2/app.py not present")
    v2 = V2_APP.read_text(encoding="utf-8")
    for line in ("function ChartUnavailable(canvas){",
                 "this.destroy = function(){};",
                 "window.Chart.__unavailable = true;",
                 "Chart library unavailable — the numbers and "):
        assert line in v1_js and line in v2, line


def test_history_card_map_covers_every_composite_card(v1_js):
    """Every composite card on the page is clickable — including the ones the
    archive does not record yet, which open an honest 'not recorded' note
    rather than silently having no affordance."""
    m = re.search(r"const COMPOSITE_HISTORY_CARDS = \[(.*?)\n\];", v1_js, re.S)
    assert m
    block = m.group(1)
    for card in ("cryptoSignalsSentimentCard", "pocSentimentCard",
                 "whaleSentimentCard", "whaleEthSentimentCard",
                 "overviewSentimentCard", "defiSentimentCard",
                 "etfFlowSentimentCard", "futuresSentimentCard",
                 "stocksSentimentCard"):
        assert card in block, f"{card} has no history affordance"
        assert f'id="{card}"' in v1_js, f"{card} is not in the V1 markup"


def test_archived_keys_match_the_snapshot_writer(v1_js):
    """The four keys scripts/snapshot_composites.py actually persists must be
    the four the cards claim are archived, or the affordance lies."""
    writer = (ROOT / "scripts" / "snapshot_composites.py").read_text(encoding="utf-8")
    for key in ("crypto_signal_sentiment", "poc_signal_breadth",
                "whale_sentiment_btc", "whale_sentiment_eth"):
        assert f"'{key}'" in writer or f'"{key}"' in writer, key
        assert f"key: '{key}'" in v1_js, key


def test_history_affordance_is_reattached_after_every_render(v1_js):
    """Several composite cards are rebuilt with innerHTML by their renderers,
    which drops the CTA. renderAll() must put it back."""
    fn = extract_function(v1_js, "renderAll")
    assert "refreshCompositeHistoryAffordances();" in fn


def test_history_modal_markup_is_present_and_accessible(v1_js):
    assert 'id="compositeHistoryModal"' in v1_js
    assert 'role="dialog"' in v1_js and 'aria-modal="true"' in v1_js
    assert 'aria-labelledby="compositeHistoryTitle"' in v1_js
    assert 'id="compositeHistoryClose"' in v1_js
    # keyboard entry point is a real <button>, so Enter/Space come free
    assert "cta.innerHTML = '<button type=\"button\" class=\"histbtn\"></button>'" in v1_js
    # Escape closes it
    assert "if (e.key === 'Escape') closeCompositeHistory();" in v1_js


def test_history_modal_returns_focus_to_the_opener(v1_js):
    fn = extract_function(v1_js, "closeCompositeHistory")
    assert "_compositeHistoryReturnFocus" in fn and ".focus()" in fn


def test_python_loader_matches_v2s(v1app):
    """The two builders must fold the archive identically — the whole point of
    a shared honesty contract is that both frontends tell the same story."""
    if not V2_APP.exists():  # pragma: no cover
        pytest.skip("v2/app.py not present")
    v1_src = V1_APP.read_text(encoding="utf-8")
    v2_src = V2_APP.read_text(encoding="utf-8")

    def body(src):
        i = src.index("def load_composite_history(")
        j = src.index("\ndef ", i + 10)
        # strip docstring + the print prefix that names the frontend
        b = src[i:j]
        b = re.sub(r'""".*?"""', "", b, count=1, flags=re.S)
        return b.replace("[v2][composites]", "[composites]")

    assert body(v1_src) == body(v2_src), (
        "load_composite_history() has drifted between app.py and v2/app.py")


def test_container_overflow_guard_is_present(v1_js):
    """The phone bug: `.container` is a grid whose auto track is sized by its
    widest item's MIN-CONTENT, so one wide card widened the whole document."""
    assert ".container > *{min-width:0}" in v1_js


def test_no_fixed_track_minimum_wider_than_a_phone(v1_js):
    """`minmax(420px,1fr)` hard-codes a track floor wider than a 360px
    viewport; auto-fit cannot rescue it, because the minimum is not relative.

    Two escapes are legitimate: wrap the minimum in ``min(Npx,100%)``, or give
    the selector an explicit single-column override in a mobile media query.
    ``.grid2`` / ``.grid3`` take the second route and never overflowed;
    ``.metals-grid2`` / ``.supplies-grid`` had neither and measured 432px at
    both 360 and 390.
    """
    css = strip_comments(v1_js)
    css = css[:css.index("</style>")] if "</style>" in css else css
    offenders = []
    for m in re.finditer(r"^(\.[A-Za-z0-9_.#-]+)\{[^}]*?minmax\((\d+)px,",
                         css, re.M):
        sel, px = m.group(1), int(m.group(2))
        if px <= 360:
            continue
        # a mobile override that collapses the same selector to one column
        base = sel.split(",")[0]
        if re.search(re.escape(base) + r"\{grid-template-columns:1fr", css):
            continue
        offenders.append((sel, px))
    assert not offenders, (
        f"grid track minima wider than a 360px phone with no mobile escape: "
        f"{offenders} — use minmax(min(Npx,100%),1fr)")
    # and the two that were fixed must stay fixed
    for sel in (".metals-grid2", ".supplies-grid"):
        assert re.search(re.escape(sel) + r"\{[^}]*minmax\(min\(420px,100%\),1fr\)",
                         css), f"{sel} lost its min() track floor"


def test_unbreakable_travel_text_can_wrap(v1_js):
    """A State-Dept excerpt arrives with double-escaped entities, so
    `&#8220;Unrest&#8221;` renders as one 20-character unbreakable token that
    paints past its (already shrunk) box and re-grows the document."""
    assert re.search(r"\.travel-bullet__excerpt[^{]*\{[^}]*overflow-wrap:anywhere",
                     v1_js) or (
        ".travel-bullet__excerpt" in v1_js
        and "overflow-wrap:anywhere" in v1_js)


# ================= 4. Wiring the five newly-archived composites =============
#
# scripts/snapshot_composites.py now records overview_sentiment, defi_sentiment,
# etf_flow_sentiment(_btc/_eth), futures_sentiment(_btc/_eth/_link/_ltc) and
# stocks_signal_breadth. PR #25 made all five cards clickable but their charts
# had nothing to plot; these guard the wiring that makes them plot the RIGHT
# series and say so honestly.

WRITER = ROOT / "scripts" / "snapshot_composites.py"

# Every key the registry may resolve to, and whether it is per-asset.
NEWLY_ARCHIVED = {
    "overview_sentiment": None,
    "defi_sentiment": None,
    "stocks_signal_breadth": None,
    "etf_flow_sentiment": ("btc", "eth"),
    "futures_sentiment": ("btc", "eth", "link", "ltc"),
}


def registry_block(js: str) -> str:
    m = re.search(r"const COMPOSITE_HISTORY_CARDS = \[(.*?)\n\];", js, re.S)
    assert m, "COMPOSITE_HISTORY_CARDS not found"
    return m.group(1)


@pytest.fixture(scope="module")
def registry_ctx(v1_js):
    """A V8 context holding the SHIPPED registry plus its key resolvers, with
    the two asset toggles stubbed so they can be driven from a test."""
    py_mini_racer = pytest.importorskip(
        "py_mini_racer", reason="V8 needed to execute the shipped JS")
    ctx = py_mini_racer.MiniRacer()
    m = re.search(r"const COMPOSITE_HISTORY_CARDS = \[.*?\n\];", v1_js, re.S)
    assert m
    bodies = "\n".join(
        extract_function(v1_js, n)
        for n in ("compositeHistoryCardFor", "compositeHistoryAssetOf",
                  "compositeHistoryKeyFor", "compositeHistoryTitleFor")
    )
    ctx.eval(
        "var state = {asset:'btc', etfAsset:'btc'};\n"
        "function etfAsset(){ return state.etfAsset; }\n"
        + m.group(0) + "\n" + bodies + "\n"
        "function specFor(card){ for (const c of COMPOSITE_HISTORY_CARDS)"
        " if (c.card === card) return c; return null; }\n"
        "function keyFor(card){ return compositeHistoryKeyFor(specFor(card)); }\n"
        "function setAssets(a, e){ state.asset = a; state.etfAsset = e; return true; }\n"
        "function archivedFlags(){ const o = {};"
        " COMPOSITE_HISTORY_CARDS.forEach(c => o[c.key] = !!c.archived); return o; }\n"
    )
    return ctx


def test_the_five_newly_archived_keys_are_marked_archived(registry_ctx):
    """`archived: true` is what switches the empty-state copy from "this index
    is not persisted at all" (now a lie) to "recording started, no snapshot
    carries it yet"."""
    flags = registry_ctx.call("archivedFlags")
    for key in NEWLY_ARCHIVED:
        assert flags.get(key) is True, f"{key} is not marked archived"


def test_archived_flag_agrees_with_the_snapshot_writer_both_ways(v1_js):
    """Two lies this forbids: claiming an index is archived when the writer
    never emits it, and claiming it is not when the writer does."""
    writer = WRITER.read_text(encoding="utf-8")
    for m in re.finditer(r"\{ card: '\w+', key: '(\w+)'(, archived: true)?", registry_block(v1_js)):
        key, archived = m.group(1), bool(m.group(2))
        emitted = (f'idx["{key}"]' in writer) or (f"idx[f\"{key}_" in writer) \
            or re.search(r'idx\[f?"%s(_\{asset\})?"\]' % re.escape(key), writer) is not None
        assert archived == emitted, (
            f"{key}: registry says archived={archived}, writer emits={emitted}")


def test_per_asset_card_charts_the_toggled_asset(registry_ctx):
    """The card shows the toggled asset's number; the chart under it must be
    that asset's own history, not whatever the default was."""
    for asset, etf in (("btc", "btc"), ("eth", "eth"), ("link", "btc"), ("ltc", "eth")):
        registry_ctx.call("setAssets", asset, etf)
        assert registry_ctx.call("keyFor", "futuresSentimentCard") == \
            "futures_sentiment_" + asset
        assert registry_ctx.call("keyFor", "etfFlowSentimentCard") == \
            "etf_flow_sentiment_" + etf


def test_the_bare_alias_key_is_never_charted(registry_ctx):
    """The writer also emits bare `etf_flow_sentiment` / `futures_sentiment`
    holding the DEFAULT asset's series, for shape compatibility. Plotting one
    would put BTC's past under an ETH card."""
    for asset in ("btc", "eth", "link", "ltc"):
        registry_ctx.call("setAssets", asset, "eth" if asset == "eth" else "btc")
        assert registry_ctx.call("keyFor", "futuresSentimentCard") != "futures_sentiment"
        assert registry_ctx.call("keyFor", "etfFlowSentimentCard") != "etf_flow_sentiment"


def test_an_unknown_toggle_value_falls_back_to_a_declared_asset(registry_ctx):
    """Never resolve to a key nobody writes — a typo'd or restored-from-storage
    asset must land on a real series, not `futures_sentiment_doge`."""
    registry_ctx.call("setAssets", "doge", "doge")
    assert registry_ctx.call("keyFor", "futuresSentimentCard") == "futures_sentiment_btc"
    assert registry_ctx.call("keyFor", "etfFlowSentimentCard") == "etf_flow_sentiment_btc"
    registry_ctx.call("setAssets", "btc", "btc")


def test_single_series_cards_resolve_to_their_plain_key(registry_ctx):
    for card, key in (("overviewSentimentCard", "overview_sentiment"),
                      ("defiSentimentCard", "defi_sentiment"),
                      ("stocksSentimentCard", "stocks_signal_breadth"),
                      ("whaleSentimentCard", "whale_sentiment_btc")):
        assert registry_ctx.call("keyFor", card) == key


def test_a_per_asset_key_maps_back_to_its_card_and_names_the_asset(registry_ctx):
    """The modal is opened with the resolved key, so the lookup has to work in
    reverse or the title falls back to a raw key string."""
    assert registry_ctx.call("compositeHistoryTitleFor", "futures_sentiment_link") == \
        "Futures Positioning Sentiment — LINK"
    assert registry_ctx.call("compositeHistoryTitleFor", "etf_flow_sentiment_eth") == \
        "ETF Flow Sentiment — ETH"
    assert registry_ctx.call("compositeHistoryTitleFor", "defi_sentiment") == "DeFi Sentiment"
    assert registry_ctx.call("compositeHistoryAssetOf", "futures_sentiment_ltc") == "ltc"
    assert registry_ctx.call("compositeHistoryAssetOf", "defi_sentiment") is None


def test_the_affordance_uses_the_resolved_key_not_the_base_key(v1_js):
    """Both the button's data-histindex and the card's data-histcard have to
    carry the per-asset key, or clicking the ETH card opens BTC's chart."""
    fn = extract_function(v1_js, "refreshCompositeHistoryAffordances")
    assert "const key = compositeHistoryKeyFor(spec);" in fn
    assert "btn.setAttribute('data-histindex', key);" in fn
    assert "card.setAttribute('data-histcard', key);" in fn
    assert "compositeHistoryFor(spec.key)" not in fn


def test_empty_state_separates_not_yet_captured_from_not_recorded(v1_js):
    """A key the writer emits but no snapshot carries yet is a DIFFERENT
    situation from one nothing records, and saying the second about the first
    is exactly the kind of stale claim this archive exists to kill."""
    body = extract_function(v1_js, "compositeHistoryBodyHtml")
    assert "spec.archived" in body
    assert "no snapshot in it carries this key yet" in body
    assert "cannot be backfilled" in body
    # the old copy must still exist for genuinely unrecorded cards
    assert "This index is not persisted to the daily " in body


def test_per_asset_modal_discloses_which_asset_it_is_showing(v1_js):
    body = extract_function(v1_js, "compositeHistoryBodyHtml")
    assert "This card is per-asset" in body
    assert "compositeHistoryAssetOf(key)" in body


def test_history_resolvers_match_v2s(v1_js):
    """A fix in one frontend only is a half fix."""
    if not V2_APP.exists():  # pragma: no cover
        pytest.skip("v2/app.py not present")
    src = V2_APP.read_text(encoding="utf-8")
    v2_js = src[src.index('HTML_TEMPLATE = r"""') + len('HTML_TEMPLATE = r"""'):]
    for name in ("compositeHistoryCardFor", "compositeHistoryAssetOf",
                 "compositeHistoryKeyFor", "compositeHistoryTitleFor"):
        assert extract_function(v1_js, name) == extract_function(v2_js, name), name
    assert registry_block(v1_js).replace("test_v1_composite_history", "X") == \
        registry_block(v2_js).replace("test_v2_composite_history", "X")


# ================= 5. Unbreakable upstream text (Item 8) ====================


def test_every_upstream_article_row_can_wrap(v1_js):
    """A headline is whatever the feed sent. One underscore-joined slug is a
    single unbreakable token, and V1 measured 1284px in a 360px viewport with
    one in DATA.market.ai_news. Every row built from an upstream URL carries
    the wrap class; overflow-wrap is inherited so the row covers its headline,
    body and source."""
    rows = re.findall(r"<a (class=\"feedrow\" )?href=\"\$\{sanitizeUrl\((?:n|p|art)\.url\)\}",
                      v1_js)
    assert rows, "no upstream article rows found — did the selector change?"
    unclassed = [r for r in rows if not r[0]]
    assert not unclassed, f"{len(unclassed)} upstream article row(s) with no wrap class"


def test_the_wrap_class_actually_sets_overflow_wrap(v1_js):
    css = v1_js[:v1_js.index("</style>")]
    m = re.search(r"^[^\n{]*\.feedrow[^\n{]*\{overflow-wrap:anywhere\}", css, re.M)
    assert m, ".feedrow has no overflow-wrap:anywhere rule"


def test_insight_cards_can_wrap_too(v1_js):
    """Insight headlines come from the same upstream feeds; they took the
    overview / stocks / lthcs tabs to 1274-1355px at 360."""
    assert v1_js.count('<div class="feedrow" style="display:flex;align-items:flex-start') >= 4


def test_chart_card_subtitles_can_wrap(v1_js):
    """`.chart-card .desc` carries the CoinGecko coin NAME under each per-coin
    signal card (app.py's renderSignalCardFromObj). A long unbreakable name
    sized the flex head to its min-content and took the signals tab to 717px
    at both 360 and 390."""
    css = v1_js[:v1_js.index("</style>")]
    assert re.search(r"\.chart-card \.desc[^{]*\{overflow-wrap:anywhere\}", css), \
        ".chart-card .desc has no overflow-wrap:anywhere rule"
