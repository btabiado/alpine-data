"""Contract tests for the V2 composite-index history feature.

``scripts/snapshot_composites.py`` has written ``data/composites/<date>.json``
every build since PR #23 and, until now, nothing read it. These tests cover
the reader and the chart that finally does.

Two layers, mirroring tests/test_v2_freshness.py:

1. **Python builder** — ``v2/app.py::load_composite_history`` folds the daily
   snapshot files into a per-index series that gets inlined into the payload.

2. **The shipped JavaScript** — ``compositeHistoryChart`` is extracted verbatim
   from ``v2/app.py``'s HTML template and executed in V8, so the assertions run
   against the code that actually ships.

The failure mode being guarded is the time-axis version of the whale defect:
a value observed weeks ago being PLOTTED at the day it happened to be captured.
That would turn the one archive built specifically to expose frozen sources
into a picture of a perfectly healthy daily cadence. Every assertion below is
some form of "the chart must not claim more than the archive observed".
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
V2_APP = ROOT / "v2" / "app.py"


# ---------------------------------------------------------------- helpers ---


@pytest.fixture(scope="module")
def v2app():
    """Import ``v2/app.py`` under its own module name (the repo root already
    owns ``app``, and the two are different builders)."""
    if not V2_APP.exists():  # pragma: no cover - repo layout guard
        pytest.skip("v2/app.py not present")
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location("v2_app_history_under_test", V2_APP)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as e:  # pragma: no cover - dependency guard
        pytest.skip(f"v2/app.py not importable here: {e}")
    return mod


@pytest.fixture(scope="module")
def v2_js() -> str:
    src = V2_APP.read_text(encoding="utf-8")
    marker = 'HTML_TEMPLATE = r"""'
    i = src.index(marker)
    return src[i + len(marker):]


def extract_function(js: str, name: str) -> str:
    """Full source of a top-level ``function name(...){...}``, brace-matched."""
    m = re.search(r"^function %s\s*\(" % re.escape(name), js, re.M)
    assert m, f"function {name}() not found in the V2 template"
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


def strip_comment_lines(js: str) -> str:
    """Drop whole-line ``//`` comments.

    The comments above the fixed code quote the OLD broken expression on
    purpose — that is how the next reader learns what not to reintroduce — so
    the "no build clock" assertions have to look at code, not prose. Only
    full-line comments are removed, so a ``https://`` inside a string is safe.
    """
    return "\n".join(l for l in js.splitlines() if not l.lstrip().startswith("//"))


def extract_const(js: str, name: str) -> str:
    m = re.search(r"^const %s\s*=.*?;\s*$" % re.escape(name), js, re.M)
    assert m, f"const {name} not found in the V2 template"
    return m.group(0)


def write_snapshot(dirpath: Path, capture: str, indexes: dict) -> None:
    dirpath.mkdir(parents=True, exist_ok=True)
    (dirpath / f"{capture}.json").write_text(
        json.dumps({"as_of": capture,
                    "generated_at": capture + "T21:00:00Z",
                    "indexes": indexes}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")


def entry(score, as_of=None, stale=False, label=None, note=None) -> dict:
    return {"score": score, "as_of": as_of, "stale": stale,
            "label": label, "note": note}


@pytest.fixture
def archive(tmp_path, v2app, monkeypatch):
    """Point the builder's DATA_DIR at a throwaway tree and hand back the
    ``composites/`` dir inside it."""
    monkeypatch.setattr(v2app, "DATA_DIR", tmp_path)
    return tmp_path / "composites"


# ------------------------------------------ 1. Python: load_composite_history


def test_point_is_dated_by_its_own_as_of_not_the_filename(archive, v2app):
    """THE rule. A snapshot captured today can hold a three-week-old value;
    dating it from the filename erases the only thing the archive records."""
    write_snapshot(archive, "2026-08-02",
                   {"crypto_signal_sentiment": entry(-33, as_of="2026-07-11")})
    pts = v2app.load_composite_history()["indexes"]["crypto_signal_sentiment"]["points"]
    assert [p["as_of"] for p in pts] == ["2026-07-11"]
    # the capture day is kept, but as a SEPARATE field — never as the x value
    assert pts[0]["snapshot"] == "2026-08-02"


def test_repeat_captures_of_one_observation_collapse_to_one_point(archive, v2app):
    """A frozen source captured five days running is ONE observation, not five.
    Emitting five points would draw a healthy daily cadence over a dead feed."""
    for day in ("2026-07-20", "2026-07-21", "2026-07-22"):
        write_snapshot(archive, day,
                       {"x": entry(9, as_of="2026-07-19")})
    e = v2app.load_composite_history()["indexes"]["x"]
    assert len(e["points"]) == 1
    assert e["points"][0]["as_of"] == "2026-07-19"
    # every capture is still COUNTED, so the modal can say 3 snapshots → 1 point
    assert e["snapshots"] == 3 and e["dated"] == 3


def test_later_capture_wins_a_duplicate_as_of(archive, v2app):
    """When two captures disagree about the same observation date, the most
    recently recomputed view is the one kept — including its stale flag."""
    write_snapshot(archive, "2026-07-20", {"x": entry(9, as_of="2026-07-19")})
    write_snapshot(archive, "2026-07-22", {"x": entry(9, as_of="2026-07-19", stale=True)})
    pts = v2app.load_composite_history()["indexes"]["x"]["points"]
    assert len(pts) == 1
    assert pts[0]["stale"] is True
    assert pts[0]["snapshot"] == "2026-07-22"


def test_undated_score_is_excluded_and_counted(archive, v2app):
    """A score with no observation date is unplottable. It must not be dated
    from the filename, and the count must be disclosed rather than dropped."""
    write_snapshot(archive, "2026-07-23", {"x": entry(11, as_of=None)})
    e = v2app.load_composite_history()["indexes"]["x"]
    assert e["points"] == []
    assert e["undated"] == 1 and e["dated"] == 0


def test_null_entry_is_a_gap_never_interpolated(archive, v2app):
    write_snapshot(archive, "2026-07-20", {"x": entry(1, as_of="2026-07-20")})
    write_snapshot(archive, "2026-07-21", {"x": None})
    write_snapshot(archive, "2026-07-22", {"x": entry(5, as_of="2026-07-22")})
    e = v2app.load_composite_history()["indexes"]["x"]
    assert [p["as_of"] for p in e["points"]] == ["2026-07-20", "2026-07-22"]
    assert e["missing"] == 1


def test_tracked_but_never_recorded_key_is_still_emitted(archive, v2app):
    """"tracked, nothing recorded yet" and "not tracked at all" are different
    statements and the UI has to be able to tell them apart."""
    write_snapshot(archive, "2026-08-02", {"crypto_signal_sentiment": None})
    idx = v2app.load_composite_history()["indexes"]
    assert "crypto_signal_sentiment" in idx
    assert idx["crypto_signal_sentiment"]["points"] == []
    assert "never_snapshotted" not in idx


def test_points_sorted_by_observation_date_not_capture_order(archive, v2app):
    """A later capture can carry an OLDER observation (a backfilled feed);
    the series still has to come out in calendar order."""
    write_snapshot(archive, "2026-07-20", {"x": entry(1, as_of="2026-07-19")})
    write_snapshot(archive, "2026-07-21", {"x": entry(2, as_of="2026-07-05")})
    pts = v2app.load_composite_history()["indexes"]["x"]["points"]
    assert [p["as_of"] for p in pts] == ["2026-07-05", "2026-07-19"]


def test_non_numeric_and_boolean_scores_are_gaps(archive, v2app):
    """`True` is an int in Python; it is not a composite score."""
    write_snapshot(archive, "2026-07-20", {"x": entry("n/a", as_of="2026-07-20")})
    write_snapshot(archive, "2026-07-21", {"x": entry(True, as_of="2026-07-21")})
    e = v2app.load_composite_history()["indexes"]["x"]
    assert e["points"] == [] and e["missing"] == 2


def test_snapshot_cap_keeps_the_newest_files(archive, v2app):
    for d in range(1, 11):
        write_snapshot(archive, f"2026-07-{d:02d}", {"x": entry(d, as_of=f"2026-07-{d:02d}")})
    out = v2app.load_composite_history(max_snapshots=4)
    assert out["snapshots"] == 4
    assert [p["as_of"] for p in out["indexes"]["x"]["points"]] == [
        "2026-07-07", "2026-07-08", "2026-07-09", "2026-07-10"]


def test_missing_directory_and_unparseable_file_never_raise(archive, v2app, tmp_path):
    # no directory at all
    assert v2app.load_composite_history() == {
        "snapshots": 0, "first_snapshot": None, "last_snapshot": None, "indexes": {}}
    # one good file, one corrupt: the corrupt one is skipped, not fatal
    write_snapshot(archive, "2026-07-20", {"x": entry(1, as_of="2026-07-20")})
    (archive / "2026-07-21.json").write_text("{not json", encoding="utf-8")
    out = v2app.load_composite_history()
    assert out["snapshots"] == 1
    assert len(out["indexes"]["x"]["points"]) == 1


def test_payload_carries_the_history(v2app):
    """The reader is wired into the payload — otherwise the archive stays
    unread, which is the exact bug this feature fixes."""
    src = V2_APP.read_text(encoding="utf-8")
    assert '"composite_history": load_composite_history()' in src


# ------------------------------------------------ 2. Shipped JS: the chart ---


@pytest.fixture(scope="module")
def chart_ctx(v2_js):
    """V8 context exposing ``chart(points)`` -> SVG string."""
    py_mini_racer = pytest.importorskip(
        "py_mini_racer", reason="V8 needed to execute the shipped JS")
    ctx = py_mini_racer.MiniRacer()
    bodies = "\n".join(
        [extract_const(v2_js, "COMPOSITE_HISTORY_MIN_TREND_POINTS"),
         extract_const(v2_js, "COMPOSITE_HISTORY_GAP_BREAK_DAYS")]
        + [extract_function(v2_js, n) for n in
           ("escapeHtml", "freshnessDayUTC", "freshnessYmd", "compositeHistoryChart")]
    )
    ctx.eval(bodies + "\nfunction __chart(pts){ return compositeHistoryChart(pts); }")

    def chart(points):
        return ctx.call("__chart", points)
    return chart


def pt(as_of, score, stale=False, snapshot=None, label=None, note=None):
    return {"as_of": as_of, "score": score, "stale": stale,
            "snapshot": snapshot or as_of, "label": label, "note": note}


def test_chart_draws_no_trend_line_below_the_sparse_threshold(chart_ctx):
    """Two dots joined by a line is a trend claim. The archive only started
    accumulating recently, so it has to earn that line."""
    svg = chart_ctx([pt("2026-07-20", -5), pt("2026-07-21", 12)])
    assert "<polyline" not in svg
    assert svg.count("<circle") == 2      # the observations are still plotted


def test_chart_draws_a_line_once_there_are_enough_points(chart_ctx):
    svg = chart_ctx([pt("2026-07-20", -5), pt("2026-07-21", 12), pt("2026-07-22", 3)])
    assert svg.count("<polyline") == 1


def test_chart_breaks_the_line_across_a_gap(chart_ctx):
    """A solid line across a three-week hole asserts continuity nobody
    observed. Segments either side, no bridge."""
    pts = [pt("2026-07-01", 1), pt("2026-07-02", 2), pt("2026-07-03", 3),
           pt("2026-07-25", 4), pt("2026-07-26", 5), pt("2026-07-27", 6)]
    svg = chart_ctx(pts)
    assert svg.count("<polyline") == 2


def test_chart_marks_cache_served_points_differently(chart_ctx):
    """Stale-kept observations get a hollow amber ring, never a solid dot."""
    svg = chart_ctx([pt("2026-07-20", 1), pt("2026-07-21", 2, stale=True),
                     pt("2026-07-22", 3)])
    assert svg.count('stroke="var(--v2-warn)"') == 1
    assert svg.count('fill="var(--v2-ai)"') == 2
    assert "cache-served" in svg


def test_chart_x_axis_is_time_proportional_not_index_proportional(chart_ctx):
    """Equal spacing would hide the gap that the whole archive exists to show.
    Three points at day 0 / 1 / 11 must NOT land at 0% / 50% / 100%."""
    svg = chart_ctx([pt("2026-07-01", 0), pt("2026-07-02", 0), pt("2026-07-12", 0)])
    xs = [float(m) for m in re.findall(r'<circle cx="([\d.]+)"', svg)]
    assert len(xs) == 3
    span = xs[2] - xs[0]
    # index-proportional would put the middle point at the halfway mark;
    # time-proportional puts it at 1/11 of the span.
    assert (xs[1] - xs[0]) / span < 0.2


def test_chart_tooltip_separates_observation_from_capture(chart_ctx):
    svg = chart_ctx([pt("2026-07-11", -33, snapshot="2026-08-02"),
                     pt("2026-07-12", -30, snapshot="2026-08-02"),
                     pt("2026-07-13", -20, snapshot="2026-08-02")])
    assert "recorded 2026-08-02 (capture day, not the observation)" in svg


def test_chart_survives_a_single_point_and_undatable_rows(chart_ctx):
    assert "<circle" in chart_ctx([pt("2026-07-20", 4)])
    assert chart_ctx([{"as_of": None, "score": 4, "stale": False}]) == ""


# --------------------------------------- 3. No build clock on any insight ---


def test_insights_stamp_is_not_the_build_clock(v2_js):
    """The named defect: `as of ${DATA.generated_at}` in renderInsights().
    DATA.generated_at is the BUILD clock and the only place it may appear is
    the header stamp, which is labelled "built"."""
    body = strip_comment_lines(extract_function(v2_js, "renderInsights"))
    assert "DATA.generated_at" not in body
    assert "as of ${DATA.generated_at}" not in strip_comment_lines(v2_js)
    assert "insightsFreshness(" in body


def test_only_the_header_stamp_renders_build_time(v2_js):
    """Exactly one surface may print DATA.generated_at as a visible date, and
    it must say "built" so nobody reads it as a data date."""
    stamp = extract_function(v2_js, "setBuildStamp")
    assert "'built ' + (DATA.generated_at" in stamp


def test_insights_freshness_takes_the_oldest_feed(v2_js):
    """A bar summarising several feeds is only as fresh as its stalest one."""
    body = extract_function(v2_js, "insightsFreshness")
    assert "fMin(" in body and "fMax(" not in body


def test_every_composite_card_has_a_history_entry(v2_js):
    """Each composite index card is reachable from the history map — including
    the ones the daily snapshot does not record yet, which get an honest
    "not recorded" explanation instead of silence."""
    m = re.search(r"const COMPOSITE_HISTORY_CARDS = \[(.*?)\n\];", v2_js, re.S)
    assert m, "COMPOSITE_HISTORY_CARDS not found"
    listed = set(re.findall(r"card: '(\w+)'", m.group(1)))
    painted = set(re.findall(r"paintSentimentCard\('(\w+)'", v2_js))
    painted |= set(re.findall(r"paintCompositeFreshness\('(\w+)'", v2_js))
    painted.discard("prefix")
    expected = {p + "Card" for p in painted}
    expected |= {"whaleSentimentCard", "whaleEthSentimentCard"}
    missing = expected - listed
    assert not missing, f"composite cards with no history affordance: {sorted(missing)}"


# ================= 3. Wiring the five newly-archived composites =============
#
# scripts/snapshot_composites.py now records overview_sentiment, defi_sentiment,
# etf_flow_sentiment(_btc/_eth), futures_sentiment(_btc/_eth/_link/_ltc) and
# stocks_signal_breadth. PR #25 made all five cards clickable but their charts
# had nothing to plot. tests/test_v1_composite_history.py carries the same
# block against app.py — a fix in one frontend only is a half fix.

WRITER = ROOT / "scripts" / "snapshot_composites.py"

NEWLY_ARCHIVED = ("overview_sentiment", "defi_sentiment", "stocks_signal_breadth",
                  "etf_flow_sentiment", "futures_sentiment")


def registry_block(js: str) -> str:
    m = re.search(r"const COMPOSITE_HISTORY_CARDS = \[(.*?)\n\];", js, re.S)
    assert m, "COMPOSITE_HISTORY_CARDS not found"
    return m.group(1)


@pytest.fixture(scope="module")
def registry_ctx(v2_js):
    """V8 context with the SHIPPED registry and its key resolvers, asset
    toggles stubbed so a test can drive them."""
    py_mini_racer = pytest.importorskip(
        "py_mini_racer", reason="V8 needed to execute the shipped JS")
    ctx = py_mini_racer.MiniRacer()
    m = re.search(r"const COMPOSITE_HISTORY_CARDS = \[.*?\n\];", v2_js, re.S)
    assert m
    bodies = "\n".join(
        extract_function(v2_js, n)
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
    flags = registry_ctx.call("archivedFlags")
    for key in NEWLY_ARCHIVED:
        assert flags.get(key) is True, f"{key} is not marked archived"


def test_archived_flag_agrees_with_the_snapshot_writer_both_ways(v2_js):
    """`archived` means exactly "snapshot_composites.py emits this key". Both
    directions are lies worth failing on."""
    writer = WRITER.read_text(encoding="utf-8")
    for m in re.finditer(r"\{ card: '\w+', key: '(\w+)'(, archived: true)?",
                         registry_block(v2_js)):
        key, archived = m.group(1), bool(m.group(2))
        emitted = re.search(r'idx\[f?"%s(_\{asset\})?"\]' % re.escape(key),
                            writer) is not None
        assert archived == emitted, (
            f"{key}: registry says archived={archived}, writer emits={emitted}")


def test_per_asset_card_charts_the_toggled_asset(registry_ctx):
    for asset, etf in (("btc", "btc"), ("eth", "eth"), ("link", "btc"), ("ltc", "eth")):
        registry_ctx.call("setAssets", asset, etf)
        assert registry_ctx.call("keyFor", "futuresSentimentCard") == \
            "futures_sentiment_" + asset
        assert registry_ctx.call("keyFor", "etfFlowSentimentCard") == \
            "etf_flow_sentiment_" + etf
    registry_ctx.call("setAssets", "btc", "btc")


def test_the_bare_alias_key_is_never_charted(registry_ctx):
    """The writer emits bare `etf_flow_sentiment` / `futures_sentiment` holding
    the DEFAULT asset's series for shape compatibility. Plotting one would put
    BTC's past under an ETH card."""
    for asset in ("btc", "eth", "link", "ltc"):
        registry_ctx.call("setAssets", asset, "eth" if asset == "eth" else "btc")
        assert registry_ctx.call("keyFor", "futuresSentimentCard") != "futures_sentiment"
        assert registry_ctx.call("keyFor", "etfFlowSentimentCard") != "etf_flow_sentiment"
    registry_ctx.call("setAssets", "btc", "btc")


def test_an_unknown_toggle_value_falls_back_to_a_declared_asset(registry_ctx):
    registry_ctx.call("setAssets", "doge", "doge")
    assert registry_ctx.call("keyFor", "futuresSentimentCard") == "futures_sentiment_btc"
    assert registry_ctx.call("keyFor", "etfFlowSentimentCard") == "etf_flow_sentiment_btc"
    registry_ctx.call("setAssets", "btc", "btc")


def test_a_per_asset_key_maps_back_to_its_card_and_names_the_asset(registry_ctx):
    assert registry_ctx.call("compositeHistoryTitleFor", "futures_sentiment_link") == \
        "Futures Positioning Sentiment — LINK"
    assert registry_ctx.call("compositeHistoryTitleFor", "etf_flow_sentiment_eth") == \
        "ETF Flow Sentiment — ETH"
    assert registry_ctx.call("compositeHistoryAssetOf", "futures_sentiment_ltc") == "ltc"
    assert registry_ctx.call("compositeHistoryAssetOf", "defi_sentiment") is None


def test_the_affordance_uses_the_resolved_key_not_the_base_key(v2_js):
    fn = extract_function(v2_js, "refreshCompositeHistoryAffordances")
    assert "const key = compositeHistoryKeyFor(spec);" in fn
    assert "btn.setAttribute('data-histindex', key);" in fn
    assert "card.setAttribute('data-histcard', key);" in fn
    assert "compositeHistoryFor(spec.key)" not in fn


def test_empty_state_separates_not_yet_captured_from_not_recorded(v2_js):
    body = extract_function(v2_js, "compositeHistoryBodyHtml")
    assert "spec.archived" in body
    assert "no snapshot in it carries this key yet" in body
    assert "cannot be backfilled" in body
    assert "This index is not persisted to the daily " in body


def test_per_asset_modal_discloses_which_asset_it_is_showing(v2_js):
    body = extract_function(v2_js, "compositeHistoryBodyHtml")
    assert "This card is per-asset" in body
    assert "compositeHistoryAssetOf(key)" in body


# ================= 4. Unbreakable upstream text (Item 8) ====================


def test_every_upstream_article_row_can_wrap(v2_js):
    """A headline is whatever the feed sent. One underscore-joined slug is a
    single unbreakable token: measured, V2's overview tab hit 1362px and social
    1355px in a 360px viewport. #aiNewsFeed only looked safe because it is its
    own scroll container — the headline was still cut off mid-token."""
    rows = re.findall(
        r"<a (class=\"feedrow\" )?href=\"\$\{sanitizeUrl\((?:n|p|art)\.url\)\}", v2_js)
    assert rows, "no upstream article rows found — did the selector change?"
    assert not [r for r in rows if not r[0]], "upstream article row with no wrap class"


def test_the_wrap_class_actually_sets_overflow_wrap(v2_js):
    css = v2_js[:v2_js.index("</style>")]
    assert re.search(r"\.feedrow[^{]*\{overflow-wrap:anywhere\}", css), \
        ".feedrow has no overflow-wrap:anywhere rule"
    assert re.search(r"\.v2-insight[^_][^{]*\{overflow-wrap:anywhere\}", css) or \
        ".v2-insight,.v2-ai-take__bullet{overflow-wrap:anywhere}" in css


def test_chart_card_subtitles_can_wrap(v2_js):
    """Same `.chart-card .desc` subtitle carries upstream names in V2. Fixed in
    both builders together — a fix in one is a half fix."""
    css = v2_js[:v2_js.index("</style>")]
    assert re.search(r"\.chart-card \.desc[^{]*\{overflow-wrap:anywhere\}", css), \
        ".chart-card .desc has no overflow-wrap:anywhere rule"
