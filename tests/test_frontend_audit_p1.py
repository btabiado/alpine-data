"""Offline tests for the 2026-10-04 UX-audit P1 front-end fixes.

* scripts/build_lthcs_site_index.py — the deploy-time file listing, health
  summary and per-ticker pillar history the LTHCS pages now read instead of
  probing URLs / downloading every snapshot.
* The TSA stale-badge threshold and strict M/D/YYYY parsing in app.py,
  executed in V8 from the shipped template (same harness style as
  tests/test_v1_freshness.py).
* The V1 Real Estate tab's vendor-date stamp (oldest source, not fetch time).
* Both whale renderers coerce Blockchair numbers and undo a raw-wei fee.
"""
from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


# ------------------------------------------------ build_lthcs_site_index ---

@pytest.fixture(scope="module")
def builder():
    spec = importlib.util.spec_from_file_location(
        "build_lthcs_site_index", ROOT / "scripts" / "build_lthcs_site_index.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _w(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj), encoding="utf-8")


@pytest.fixture()
def lthcs_root(tmp_path):
    r = tmp_path / "lthcs"
    for d, n in (("2026-10-03", 2), ("2026-10-04", 3)):
        _w(r / "snapshots" / f"{d}.json", {"calc_date": d, "scores": [
            {"ticker": t, "lthcs_score": 50 + i,
             "subscores": {"adoption_momentum": 10 + i, "des": 40}}
            for i, t in enumerate(["AAPL", "MSFT", "NVDA"][:n])]})
        _w(r / "insider" / f"{d}.json", {"AAPL": {}, "MSFT": {}})
        _w(r / "holdings" / f"{d}.json", {"AAPL": {"manager_count": 3},
                                          "MSFT": {"manager_count": 0}})
        _w(r / "macro" / f"breadth_{d}.json",
           {"as_of": d, "data_quality": {"sources_ok": 3, "sources_failed": 1}})
        _w(r / "macro" / f"breadth_sentiment_{d}.json",
           {"data_quality": {"sources_ok": 2, "sources_failed": 1}})
    _w(r / "snapshots" / "index.json", {"dates": ["2026-10-04", "2026-10-03"]})
    _w(r / "macro" / "sector_strength_2026-05-17.json", {"sectors": {"XLK": {}}})
    _w(r / "variable_detail" / "2026-10-04.json", {"calc_date": "2026-10-04", "variables": [
        {"pillar": "des", "data_quality": {"has_x": True, "days": 3}},
        {"pillar": "des", "data_quality": {"has_x": False, "days": 1}}]})
    _w(r / "trends" / "2026-W40.json", {"as_of": "2026-10-04", "tickers": {"A": 1}, "term_map": {"a": 1, "b": 2}})
    _w(r / "trends" / "2026-W39.json", {})
    _w(r / "backtest" / "2026-10-04_validation" / "engine_summary.json", {})
    _w(r / "backtest" / "2026-10_monthly" / "20261001T115239" / "summary.json", {})
    _w(r / "universe.json", {"tickers": [1, 2, 3, 4]})
    _w(r / "weights.json", {})
    return r


def test_file_index_lists_dated_files_by_dir_and_prefix(builder, lthcs_root):
    idx = builder.build_file_index(lthcs_root)
    d = idx["dated"]
    assert d["snapshots"]["dates"] == ["2026-10-04", "2026-10-03"]
    assert d["snapshots"]["latest"] == "2026-10-04"
    # the prefix is split off so optional stages are discoverable by name
    assert d["macro/breadth_sentiment"]["latest"] == "2026-10-04"
    assert d["macro/sector_strength"]["dates"] == ["2026-05-17"]
    assert "macro/breadth" in d and "macro" not in d
    assert idx["weekly"]["trends"] == {"latest": "2026-W40", "count": 2,
                                       "weeks": ["2026-W40", "2026-W39"]}
    assert "2026-10-04_validation/engine_summary.json" in idx["backtest_files"]
    assert "2026-10_monthly/20261001T115239/summary.json" in idx["backtest_files"]
    assert idx["exists"]["weights.json"] is True
    assert idx["exists"]["sentiment_llm/AAPL.json"] is False


def test_health_summary_reproduces_the_in_browser_counts(builder, lthcs_root):
    idx = builder.build_file_index(lthcs_root)
    s = builder.build_health_summary(lthcs_root, idx)
    assert s["latest"] == "2026-10-04"
    today = s["days"][0]
    assert today["date"] == "2026-10-04"
    assert today["tickers"] == 3
    assert today["insider"] == 2
    assert today["holdings_covered"] == 1          # manager_count > 0 only
    assert (today["macro_ok"], today["macro_failed"]) == (3, 1)
    assert (today["sentiment_ok"], today["sentiment_failed"]) == (2, 1)
    assert today["sectors"] is None                # no file that day -> unknown, not 0
    assert s["universe_size"] == 4
    assert s["variable_detail"]["pillars"]["des"] == {"total": 2, "flags": {"has_x": 1}}
    assert s["trends"] == {"week": "2026-W40", "as_of": "2026-10-04", "tickers": 1, "terms": 2}
    assert s["analyst"] is None


def test_pillar_history_is_chronological_per_ticker(builder, lthcs_root):
    idx = builder.build_file_index(lthcs_root)
    h = builder.build_pillar_history(lthcs_root, idx)
    assert [r["date"] for r in h["MSFT"]] == ["2026-10-03", "2026-10-04"]
    assert h["MSFT"][0]["adoption_momentum"] == 11
    assert h["MSFT"][0]["composite"] == 51
    assert h["MSFT"][0]["thesis_integrity"] is None   # missing pillar stays null
    assert [r["date"] for r in h["NVDA"]] == ["2026-10-04"]


def test_main_writes_the_three_artifacts(builder, lthcs_root):
    assert builder.main(["--root", str(lthcs_root)]) == 0
    assert json.loads((lthcs_root / "file_index.json").read_text())["schema"] == 1
    assert json.loads((lthcs_root / "health_summary.json").read_text())["latest"] == "2026-10-04"
    aapl = json.loads((lthcs_root / "history" / "pillars_by_ticker" / "AAPL.json").read_text())
    assert aapl["ticker"] == "AAPL" and len(aapl["history"]) == 2


def _hist(root: Path, ticker: str, rows):
    _w(root / "history" / "by_ticker" / f"{ticker}.json",
       {"ticker": ticker, "history": [{"date": d, "score": sc, "band": "x"} for d, sc in rows]})


def test_trend_index_keeps_the_window_plus_one_anchor_row(builder, lthcs_root):
    # newest-first on purpose (the committed files are not chronological)
    _hist(lthcs_root, "AAPL", [("2026-10-04", 60), ("2026-09-20", 55),
                               ("2026-09-04", 50), ("2026-09-01", 49), ("2026-08-01", 40)])
    _hist(lthcs_root, "NVDA", [("2026-10-04", 70), ("2026-10-03", None)])
    t = builder.build_trend_index(lthcs_root, builder.build_file_index(lthcs_root))
    assert t["latest"] == "2026-10-04" and t["lookback_days"] == 30
    # cutoff 2026-09-04: everything after it, plus the newest row on/before it
    assert t["tickers"]["AAPL"] == [{"date": "2026-09-04", "score": 50},
                                    {"date": "2026-09-20", "score": 55},
                                    {"date": "2026-10-04", "score": 60}]
    assert t["tickers"]["NVDA"] == [{"date": "2026-10-04", "score": 70}]   # null dropped
    assert builder.main(["--root", str(lthcs_root)]) == 0
    assert (lthcs_root / "history" / "trend_index.json").is_file()


def test_trend_index_reproduces_the_browser_trend_for_every_committed_ticker(builder):
    """The /lthcs/ cards must read the same 30d delta from the one-file index
    as from each ticker's full history file. Runs the shipped computeTrend()
    in V8 on both, for every ticker in the committed data."""
    mr = pytest.importorskip("py_mini_racer", reason="V8 needed to run the shipped JS")
    root = ROOT / "data" / "lthcs"
    idx = builder.build_file_index(root)
    t = builder.build_trend_index(root, idx)
    if t is None:
        pytest.skip("no committed LTHCS history")
    src = (ROOT / "lthcs_tab" / "lthcs-tab.js").read_text(encoding="utf-8")

    def fn(name):
        start = src.index(f"function {name}(")
        i, depth = src.index("{", start), 0
        for j in range(i, len(src)):
            depth += {"{": 1, "}": -1}.get(src[j], 0)
            if depth == 0:
                return src[start:j + 1]
        raise AssertionError(f"unbalanced braces in {name}()")

    consts = re.search(r"const TREND_FLAT_THRESHOLD = [^;]+;", src).group(0) + \
        re.search(r"const TREND_FALLBACK_DAYS = [^;]+;", src).group(0)
    assert "[30," in consts, "TREND_LOOKBACK_DAYS must equal max(TREND_FALLBACK_DAYS)"
    ctx = mr.MiniRacer()
    ctx.eval(consts + "".join(fn(n) for n in (
        "parseISODateUTC", "pickAnchorForLookback", "pickAnchorWithFallback", "computeTrend")))
    snap = json.loads((root / "snapshots" / f"{t['latest']}.json").read_text())
    scores = {r["ticker"]: r["lthcs_score"] for r in snap["scores"]}
    checked = 0
    for ticker, rows in t["tickers"].items():
        if ticker not in scores:
            continue
        full = json.loads((root / "history" / "by_ticker" / f"{ticker}.json").read_text())
        a = ctx.call("computeTrend", full, t["latest"], scores[ticker])
        b = ctx.call("computeTrend", {"history": rows}, t["latest"], scores[ticker])
        assert a == b, ticker
        checked += 1
    assert checked >= len(scores) - 2


def test_index_page_reads_the_trend_index_before_per_ticker_files():
    src = (ROOT / "lthcs_tab" / "lthcs-tab.js").read_text(encoding="utf-8")
    assert "../data/lthcs/history/trend_index.json" in src
    body = src[src.index("async function fetchTrendMap("):src.index("async function fetchInsider(")]
    assert body.index("fetchTrendIndex(calcDate)") < body.index("HISTORY_BASE")
    guard = src[src.index("async function fetchTrendIndex("):src.index("async function fetchTrendMap(")]
    assert "doc.latest !== calcDate" in guard


def test_generated_artifacts_are_gitignored_and_built_in_pages_yml():
    gi = (ROOT / ".gitignore").read_text(encoding="utf-8")
    for p in ("data/lthcs/file_index.json", "data/lthcs/health_summary.json",
              "data/lthcs/history/pillars_by_ticker/",
              "data/lthcs/history/trend_index.json"):
        assert p in gi
    yml = (ROOT / ".github" / "workflows" / "pages.yml").read_text(encoding="utf-8")
    step = yml.index("scripts/build_lthcs_site_index.py --root data/lthcs")
    assert step < yml.index("- name: Stage site directory"), (
        "the index must be built before data/lthcs is copied into _site")


# ------------------------------------------------------- shipped JS (V8) ---

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


@pytest.fixture(scope="module")
def v1_js() -> str:
    return _template_js(ROOT / "app.py")


@pytest.fixture(scope="module")
def v8(v1_js):
    mr = pytest.importorskip("py_mini_racer", reason="V8 needed to execute the shipped JS")
    ctx = mr.MiniRacer()
    names = ("freshness", "freshnessDayUTC", "freshnessYmd", "fDay", "fMin",
             "tsaDayIso", "tsaStaleDays", "realEstateSourceDates")
    bodies = "\n".join(_extract(v1_js, n) for n in names)
    const = re.search(r"^const TSA_STALE_DAYS = \d+;", v1_js, re.M).group(0)
    ctx.eval("""
    function __make(RealDate, nowMs){
      class D extends RealDate {
        constructor(...a){ if (a.length === 0) super(nowMs); else super(...a); }
        static now(){ return nowMs; }
      }
      const Date = D;
      %s
      %s
      return { tsaDayIso, tsaStaleDays, realEstateSourceDates, TSA_STALE_DAYS };
    }
    function __call(fn, now, ...args){ return __make(Date, Date.parse(now))[fn](...args); }
    function __const(){ return __make(Date, 0).TSA_STALE_DAYS; }
    """ % (const, bodies))
    return ctx


@pytest.mark.parametrize("raw,iso", [
    ("6/17/2026", "2026-06-17"),
    ("12/1/2025", "2025-12-01"),
    (" 6/7/2026 ", "2026-06-07"),
    ("2026-06-17", None),        # not TSA's format -> refused, not guessed
    ("Jun 17 2026", None),
    ("2/30/2026", None),         # not a calendar day
    (None, None),
])
def test_tsa_day_parsing_is_strict(v8, raw, iso):
    assert v8.call("__call", "tsaDayIso", "2026-10-04T12:00:00Z", raw) == iso


def test_tsa_stale_threshold_is_three_days(v8):
    assert v8.call("__const") == 3
    now = "2026-10-04T12:00:00Z"
    assert v8.call("__call", "tsaStaleDays", now, "2026-10-04") == 0
    assert v8.call("__call", "tsaStaleDays", now, "2026-10-01") == 0   # 3d: weekend gap is normal
    assert v8.call("__call", "tsaStaleDays", now, "2026-09-30") == 4   # 4d: stale, age returned
    assert v8.call("__call", "tsaStaleDays", now, "2026-06-17") == 109
    assert v8.call("__call", "tsaStaleDays", now, None) is None


def test_real_estate_stamp_is_the_oldest_vendor_publish_date(v8):
    snap = {"generated_at": "2026-10-04T12:02:40Z", "sources": {
        "zillow": {"fetched_at": "2026-10-04T12:02:40Z", "last_modified": "Wed, 16 Sep 2026 02:11:30 GMT"},
        "redfin": {"fetched_at": "2026-10-04T12:02:40Z", "last_modified": "Tue, 02 Jun 2026 18:16:11 GMT"},
        "fred": {"fetched_at": "2026-10-04T12:02:40Z"}}}
    out = v8.call("__call", "realEstateSourceDates", "2026-10-04T12:00:00Z", snap)
    assert out["oldest"] == "2026-06-02"        # Redfin, never the fetch day
    assert [c["date"] for c in out["comps"]] == ["2026-09-16", "2026-06-02", None]


def test_tsa_copy_no_longer_claims_near_real_time(v1_js):
    assert "near-real-time" not in v1_js
    assert "most recent ~30 days" not in v1_js
    assert 'id="tsa-stale-badge"' in v1_js


# --------------------------------------------------------- whale fee units ---

@pytest.mark.parametrize("path", ["app.py"])
def test_whale_eth_renderers_coerce_and_undo_wei(path):
    js = _template_js(ROOT / path)
    i = js.index("const statsBox = document.getElementById('ethStatsBox');")
    block = js[i:i + 3000]
    assert "toFiniteNum(bc.avg_tx_fee_eth_24h)" in block
    assert "avgFee >= 1) avgFee = avgFee / 1e18" in block
    assert "toFiniteNum(bc.burned_eth_24h)" in block


@pytest.mark.parametrize("path", ["app.py"])
def test_static_mirror_drops_the_dead_bookmarklet_link(path):
    js = _template_js(ROOT / path)
    assert 'id="bookmarkletLink"' in js
    i = js.index("if (!isServer){")
    assert "getElementById('bookmarkletLink')" in js[i:i + 4000]
    assert ".remove()" in js[i:i + 4000]
