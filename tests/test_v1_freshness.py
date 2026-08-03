"""Contract tests for the V1 (PRODUCTION) dashboard's data-freshness stamps.

`app.py` builds the root `dashboard.html` that `.github/workflows/pages.yml`
deploys — this is the page the user actually looks at. `v2/app.py` is the
preview and carries a banner saying so. The stamps landed there first; this
file guards the production port.

Three layers:

1. **Python builder** — ``app.py``'s DeFi provenance backfill, byte-equivalent
   to v2/app.py's.

2. **The shipped JavaScript** — ``freshness()`` is the single implementation
   behind every stamp on the page (header, tab strips, composite cards,
   breadth charts, whale/ETF/money-flow badges). Its source is extracted
   verbatim from ``app.py``'s HTML template and executed in V8 with a pinned
   clock, so these assertions run against the code that actually ships rather
   than a reimplementation of it.

3. **Cross-frontend parity** — the helper family must stay byte-identical to
   v2/app.py's. Two frontends with two dialects of "how old is this?" is how
   the divergence being fixed here started.

Why this file exists: the failure mode being guarded is a stamp that reports
BUILD or FETCH time instead of DATA time. That reads as "just now" on a page
which rebuilds hourly, and it is what let the crypto breadth chart sit frozen
at 2026-06-09 for eight weeks looking completely current. A stamp that lies is
worse than no stamp, so the honesty rules get tests.
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
V1_APP = ROOT / "app.py"
V1_HTML = ROOT / "dashboard.html"
V2_APP = ROOT / "v2" / "app.py"


# ---------------------------------------------------------------- helpers ---


def _template_js(path: Path) -> str:
    src = path.read_text(encoding="utf-8")
    marker = 'HTML_TEMPLATE = r"""'
    i = src.index(marker)
    return src[i + len(marker):]


@pytest.fixture(scope="module")
def v1app():
    """Import ``app.py`` under its own module name."""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location("v1_app_under_test", V1_APP)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as e:  # pragma: no cover - dependency guard
        pytest.skip(f"app.py not importable here: {e}")
    return mod


@pytest.fixture(scope="module")
def v1_js() -> str:
    """The inline dashboard JS, taken from the builder's template string."""
    return _template_js(V1_APP)


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


@pytest.fixture(scope="module")
def freshness_ctx(v1_js):
    """A V8 context exposing ``call(iso, opts, nowIso)`` for freshness().

    The clock is injected per call by shadowing ``Date`` inside a factory
    closure, so age assertions are deterministic instead of depending on
    when the suite runs.
    """
    py_mini_racer = pytest.importorskip(
        "py_mini_racer", reason="V8 needed to execute the shipped JS")
    ctx = py_mini_racer.MiniRacer()

    bodies = "\n".join(
        extract_function(v1_js, n)
        for n in ("freshness", "freshnessDayUTC", "freshnessYmd")
    )
    harness = """
    function __makeFreshness(RealDate, fixedNowMs){
      class D extends RealDate {
        constructor(...a){ if (a.length === 0) super(fixedNowMs); else super(...a); }
        static now(){ return fixedNowMs; }
      }
      const Date = D;
      %s
      return freshness;
    }
    function __call(iso, opts, nowIso){
      const f = __makeFreshness(Date, Date.parse(nowIso));
      return f(iso, opts);
    }
    """ % bodies
    ctx.eval(harness)

    def call(iso, opts=None, now="2026-08-03T12:00:00Z"):
        return ctx.call("__call", iso, opts or {}, now)

    return call


# --------------------------------------------------- 1. Python: DeFi as_of ---


def test_defi_observation_date_takes_oldest_chain(v1app):
    """A composite is only as fresh as its OLDEST input — min, not max."""
    defi = {"tvl_history": {
        "Ethereum": [{"date": "2026-07-30", "tvl_usd": 1}, {"date": "2026-08-01", "tvl_usd": 2}],
        "Solana":   [{"date": "2026-07-28", "tvl_usd": 3}],
        "Base":     [{"date": "2026-08-01", "tvl_usd": 4}],
    }}
    assert v1app.defi_observation_date(defi) == "2026-07-28"


def test_defi_observation_date_ignores_series_order(v1app):
    defi = {"tvl_history": {"Ethereum": [{"date": "2026-08-01"}, {"date": "2026-07-20"}]}}
    assert v1app.defi_observation_date(defi) == "2026-08-01"


@pytest.mark.parametrize("defi", [
    {},
    {"tvl_history": {}},
    {"tvl_history": {"Ethereum": []}},
    {"tvl_history": {"Ethereum": [{"tvl_usd": 1}]}},
    {"tvl_history": "not-a-dict"},
])
def test_defi_observation_date_none_when_undatable(v1app, defi):
    assert v1app.defi_observation_date(defi) is None


def test_stamp_defi_provenance_fills_missing_as_of(v1app):
    defi = {"tvl_history": {"Ethereum": [{"date": "2026-07-31"}]}}
    v1app.stamp_defi_provenance(defi, {"fetched_at": "2026-08-02T21:00:00+00:00"})
    assert defi["as_of"] == "2026-07-31"
    assert defi["snapshot_fetched_at"] == "2026-08-02T21:00:00+00:00"


def test_stamp_defi_provenance_does_not_clobber_fetcher_as_of(v1app):
    defi = {"as_of": "2026-07-01", "tvl_history": {"Ethereum": [{"date": "2026-07-31"}]}}
    v1app.stamp_defi_provenance(defi, {"fetched_at": "2026-08-02T21:00:00+00:00"})
    assert defi["as_of"] == "2026-07-01"


def test_stamp_defi_provenance_never_uses_fetch_time_as_as_of(v1app):
    """THE core rule: with no datable series, as_of stays None."""
    defi = {"chains": [{"name": "Ethereum", "change_7d_pct": 1.2}]}
    v1app.stamp_defi_provenance(defi, {"fetched_at": "2026-08-02T21:00:00+00:00"})
    assert defi["as_of"] is None
    assert defi["snapshot_fetched_at"] == "2026-08-02T21:00:00+00:00"


# ------------------------------------------- 2. Shipped JS: freshness() ------


def test_freshness_exists_in_the_v1_bundle(v1_js):
    """The gate that failed before this change: V1 had no helper at all."""
    assert re.search(r"^function freshness\(", v1_js, re.M)


def test_freshness_null_renders_explicit_unavailable(freshness_ctx):
    r = freshness_ctx(None)
    assert r["text"] == "as of —"
    assert r["tone"] == "none"
    assert r["ageDays"] is None


def test_freshness_respects_custom_label_when_unavailable(freshness_ctx):
    assert freshness_ctx(None, {"label": "flows as of"})["text"] == "flows as of —"


@pytest.mark.parametrize("bad", ["", "  ", "not-a-date", "2026-13-01", "2026-02-31", None])
def test_freshness_unparseable_is_unavailable_not_today(freshness_ctx, bad):
    """Rule 5: an unreadable date must never silently become the clock."""
    r = freshness_ctx(bad)
    assert r["text"] == "as of —"
    assert r["ageDays"] is None


def test_freshness_text_shape(freshness_ctx):
    assert freshness_ctx("2026-06-09")["text"] == "as of 2026-06-09 (55d ago)"


def test_freshness_iso_datetime_renders_date_only(freshness_ctx):
    assert freshness_ctx("2026-08-01T23:59:59Z")["text"] == "as of 2026-08-01 (2d ago)"


@pytest.mark.parametrize("age,tone", [(0, "ok"), (7, "ok"), (8, "warn"),
                                      (21, "warn"), (22, "bad"), (400, "bad")])
def test_freshness_tone_thresholds(freshness_ctx, age, tone):
    """7/21 ok/warn/bad — identical to V2. The old V1 chips had a single
    7-day amber threshold and nothing beyond it."""
    from datetime import datetime, timedelta, timezone
    now = datetime(2026, 8, 3, 12, tzinfo=timezone.utc)
    d = (now - timedelta(days=age)).strftime("%Y-%m-%d")
    assert freshness_ctx(d)["tone"] == tone


def test_freshness_custom_thresholds(freshness_ctx):
    r = freshness_ctx("2026-07-24", {"warnDays": 2, "badDays": 5})
    assert r["tone"] == "bad"


def test_freshness_never_reports_negative_age_across_timezones(freshness_ctx):
    """A future-dated observation is a data bug, not negative age."""
    r = freshness_ctx("2026-08-06")
    assert r["ageDays"] == 0
    assert "-1d" not in r["text"]


def test_freshness_discloses_cached_count(freshness_ctx):
    """Rule 4: stale-kept entries are COUNTED next to the date."""
    r = freshness_ctx("2026-08-02", {"stale": 3, "total": 50})
    assert r["text"] == "as of 2026-08-02 (1d ago) · 3 of 50 cached"
    assert freshness_ctx("2026-08-02", {"stale": 3})["text"].endswith("· 3 cached")


def test_freshness_omits_cached_suffix_when_nothing_cached(freshness_ctx):
    assert "cached" not in freshness_ctx("2026-08-02", {"stale": 0, "total": 50})["text"]


# ------------------------------- 3. Cross-frontend parity with V2 ------------


HELPER_FAMILY = ("freshness", "freshnessDayUTC", "freshnessYmd",
                 "paintFreshness", "freshnessHtml",
                 "fDay", "fLast", "fMin", "fMax")


@pytest.mark.parametrize("name", HELPER_FAMILY)
def test_helper_is_byte_identical_to_v2(v1_js, name):
    """One dialect, two frontends.

    V1 and V2 render the same payloads; if their age arithmetic, thresholds
    or wording drift, the same data reads as two different ages depending on
    which URL you opened. Comments are allowed to differ (they explain
    V1-specific context); the CODE may not.
    """
    if not V2_APP.exists():  # pragma: no cover - repo layout guard
        pytest.skip("v2/app.py not present")
    v2_js = _template_js(V2_APP)

    def code_only(src: str) -> str:
        keep = [l.rstrip() for l in src.splitlines()
                if not l.strip().startswith("//")]
        return "\n".join(l for l in keep if l.strip())

    assert code_only(extract_function(v1_js, name)) == \
        code_only(extract_function(v2_js, name)), \
        f"{name}() has drifted between V1 and V2"


# ------------------------------- 4. Source-level honesty guards --------------


EXPECTED_STAMP_IDS = [
    "dataFreshness", "generatedAt",
    "overviewSentimentFresh", "etfFlowSentimentFresh", "futuresSentimentFresh",
    "stocksSentimentFresh", "cryptoSignalsSentimentFresh", "pocSentimentFresh",
    "defiSentimentFresh",
    "cryptoSignalsBreadthFresh", "stocksBreadthFresh",
    # pre-existing badges kept, now on the shared helper
    "whaleAsOf", "etfAsOf", "mfxAsOfChip", "sfxAsOfChip",
]

# Every tab panel in V1. V1 carries seven tabs V2 does not (money_flow,
# stockflow, lthcs, real_estate, city, aviation — plus `summit`, which is a
# pure redirect launcher with no panel and therefore no stamp).
EXPECTED_TAB_STRIPS = [
    "overview", "signals", "whale", "poc", "social", "defi", "etf", "trading",
    "stocks", "ainews", "travel", "cpi", "supplies", "metals", "mufon",
    "money_flow", "stockflow", "lthcs", "real_estate", "city", "aviation",
]


@pytest.mark.parametrize("elem_id", EXPECTED_STAMP_IDS)
def test_stamp_element_exists(v1_js, elem_id):
    assert f'id="{elem_id}"' in v1_js, f"missing stamp element #{elem_id}"


@pytest.mark.parametrize("tab", EXPECTED_TAB_STRIPS)
def test_every_tab_has_a_freshness_strip(v1_js, tab):
    assert f'id="tabFresh-{tab}"' in v1_js
    assert f"{tab}:" in v1_js.split("const TAB_FRESHNESS = {")[1].split("};")[0]


def test_every_tab_panel_is_covered_by_tab_freshness(v1_js):
    """No panel may quietly ship without a strip as tabs get added."""
    panels = set(re.findall(r'<div id="tab-([a-z_]+)"', v1_js))
    resolvers = set(re.findall(
        r"^\s*([a-z_]+):", v1_js.split("const TAB_FRESHNESS = {")[1].split("};")[0], re.M))
    assert panels - resolvers == set(), f"tabs with no freshness resolver: {panels - resolvers}"


def test_build_stamp_is_labelled_built_not_generated(v1_js):
    """The one place build time may appear must name itself as build time."""
    assert "'built ' + (DATA.generated_at || '—')" in v1_js
    assert "generated ' + DATA.generated_at" not in v1_js


CLOCK_FIELDS = ("generated_at", "fetched_at", "datetime.now", "Date.now",
                "computed_at", "snapshot_fetched_at", "observed_at")


def test_no_stamp_is_fed_from_build_or_fetch_time(v1_js):
    """No clock read may reach freshness()/paintFreshness()/freshnessHtml().

    Checks the FIRST argument (the date) of every stamp call — later
    arguments legitimately mention fetch time in hover copy ("money_flow.as_of
    is a clock read … and is deliberately not shown"), which is disclosure,
    not a stamp.
    """
    for call in re.findall(
            r"(?:paintFreshness|freshnessHtml|freshness)\(([^\n]*)", v1_js):
        depth, first = 0, []
        for ch in call:
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                if depth == 0:
                    break
                depth -= 1
            elif ch == "," and depth == 0:
                break
            first.append(ch)
        date_arg = "".join(first)
        for bad in CLOCK_FIELDS:
            assert bad not in date_arg, (
                f"clock read {bad!r} used as a freshness date: {call[:140]}")


RESOLVER_NAMES = (
    "pocTopFreshness", "signalsTop20Freshness", "perpsFreshness",
    "stocksFreshness", "futuresFreshness", "etfFreshness", "whaleFreshness",
    "whaleSentimentAsOf", "overviewFreshness", "defiFreshness",
    "signalCardAsOf", "moneyFlowFreshness", "stockFlowFreshness",
    "lthcsFreshness", "realEstateFreshness", "cityFreshness",
    "aviationFreshness",
)


def test_no_resolver_pipes_a_clock_read_into_a_date(v1_js):
    """The per-surface resolvers decide WHICH date each stamp gets.

    A clock read may appear in hover copy (labelled as fetch time — that is
    disclosure) but it may never flow through the date plumbing:
    fDay/fLast/fMin/fMax, or the `date:` field of the returned object.
    """
    date_channel = re.compile(r"(?:fDay|fLast|fMin|fMax)\([^;\n]*|date:[^,\n]*")
    for name in RESOLVER_NAMES:
        fn = extract_function(v1_js, name)
        body = "\n".join(l for l in fn.splitlines()
                         if not l.strip().startswith("//"))
        for frag in date_channel.findall(body):
            for bad in CLOCK_FIELDS:
                assert bad not in frag, (
                    f"{name}() pipes clock field {bad!r} into a date: {frag[:120]}")


def test_the_four_hand_rolled_age_computations_are_gone(v1_js):
    """V1 carried four copies of

        Math.floor((Date.now() - <date>) / 86400000)

    each with a single 7-day amber threshold and 'today'/'Nd ago' wording —
    a dialect that disagreed with V2's 7/21 and '(0d ago)'. One helper now.
    """
    assert "Math.floor((Date.now()" not in _code_lines(v1_js)
    assert "'today' : ageDays" not in _code_lines(v1_js)
    assert "⚠ stale" not in _code_lines(v1_js)


def _code_lines(js: str) -> str:
    """`js` with comments dropped.

    Both forms matter: whole-line ``//`` comments, and the ``/* was: … */``
    blocks left at each fixed site quoting the buggy template it replaced.
    Without stripping those, every "the old pattern is gone" assertion below
    would fail on its own documentation.
    """
    no_blocks = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    return "\n".join(l for l in no_blocks.splitlines()
                     if not l.strip().startswith("//"))


def test_signals_top20_as_of_is_gated_on_the_provenance_fix(v1_js):
    fn = extract_function(v1_js, "signalsTop20Freshness")
    assert "typeof r.computed_at === 'string'" in fn
    assert "trustworthy" in fn
    assert "fDay(r.computed_at)" not in fn


def test_whale_sentiment_as_of_is_gated_on_the_provenance_fix(v1_js):
    """whale.sentiment.as_of used to be whale.fetched_at[:10]."""
    fn = extract_function(v1_js, "whaleSentimentAsOf")
    assert "as_of_basis" in fn
    assert "fDay(sent.as_of)" in fn


def test_composite_stamps_use_min_not_max(v1_js):
    """Rule 3: composites stamp the OLDEST contributing input."""
    for name in ("pocTopFreshness", "stocksFreshness", "futuresFreshness",
                 "overviewFreshness", "signalsTop20Freshness", "whaleFreshness",
                 "moneyFlowFreshness", "realEstateFreshness"):
        fn = extract_function(v1_js, name)
        assert "fMin(" in fn, f"{name} must take the oldest input"
        assert "fMax(" not in fn, f"{name} must not take the newest input"


def test_feed_surfaces_use_max(v1_js):
    for name in ("aiNewsFreshness", "travelFreshness"):
        assert "fMax(" in extract_function(v1_js, name)


def test_breadth_chart_discloses_cached_coin_count(v1_js):
    fn = extract_function(v1_js, "renderCryptoSignalsBreadth")
    assert "pocTopFreshness()" in fn
    assert "stale:" in fn and "total:" in fn


def test_crypto_breadth_stamp_paints_before_the_chart(v1_js):
    """Chart.js is a CDN dep behind an SRI pin. When it dies the stamp lives."""
    fn = extract_function(v1_js, "renderCryptoSignalsBreadth")
    assert fn.index("cryptoSignalsBreadthFresh") < fn.index("renderBreadthChart(")


def test_stocks_breadth_stamp_is_wired_to_the_min_resolver(v1_js):
    fn = extract_function(v1_js, "renderStocksTab")
    block = fn.split("stocksBreadthFresh")[1].split("renderBreadthChart")[0]
    assert "stocksFreshness()" in block
    assert "paintFreshness(sbEl, sf && sf.date" in block, \
        "the stocks breadth stamp must take the OLDEST ticker, not lastBar"
    assert "stale:" in block and "total:" in block, "rule 4 disclosure missing"
    assert fn.index("stocksBreadthFresh") < fn.index("renderBreadthChart('stocksBreadthChart'")


def test_whale_as_of_has_exactly_one_writer_and_follows_the_panel(v1_js):
    """Two bugs at once.

    (a) #whaleAsOf used to reduce three series with `a.date >= b.date ? a : b`
        — fMax on a composite.
    (b) its only writer lived inside renderWhalePanel()'s BTC-only else-branch,
        so the ETH panel displayed BTC's dates.
    """
    assert v1_js.count("getElementById('whaleAsOf')") == 1
    fn = extract_function(v1_js, "renderWhaleAsOf")
    assert "state.whaleAsset" in fn, "must follow the BTC/ETH panel toggle"
    assert "whaleFreshness(asset)" in fn
    assert "paintFreshness(" in fn
    assert "a.date >= b.date" not in _code_lines(v1_js), "fMax reduce is back"


def test_render_whale_as_of_runs_for_both_panels(v1_js):
    """The asset-scoping bug, guarded structurally.

    ``renderWhalePanel()`` is ``if (eth) { … } else { … }``. The only
    #whaleAsOf writer used to live inside the ELSE branch, so the ETH panel
    displayed BTC's dates. The call must sit at the function-body level
    (brace depth 1), not inside either branch (depth 2) — that is the whole
    difference between "follows the toggle" and "shipped the bug".
    """
    panel = extract_function(v1_js, "renderWhalePanel")
    body = panel[panel.index("{"):]
    depth, depths = 0, []
    i = 0
    while i < len(body):
        ch = body[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        elif body.startswith("renderWhaleAsOf();", i):
            depths.append(depth)
            i += len("renderWhaleAsOf();")
            continue
        i += 1
    assert depths, "renderWhalePanel() never calls renderWhaleAsOf()"
    assert 1 in depths, (
        "renderWhaleAsOf() is only called inside a branch of renderWhalePanel() "
        f"(brace depths {depths}); it must run for BOTH the BTC and ETH panels")


def test_no_dangling_as_of_in_the_signal_card_templates(v1_js):
    """A null as_of used to render "Frozen Coin · as of " and "as of undefined".

    The removed patterns are quoted in ``/* was: … */`` comments at each fixed
    site, so the search runs over code lines only.
    """
    code = _code_lines(v1_js)
    assert "· as of ${escapeHtml(s.as_of||'')}" not in code
    assert "as of ${escapeHtml(s.as_of)}" not in code
    assert "as of ${escapeHtml(s.as_of||'?')}" not in code
    for name in ("renderSignalCard", "renderSignalCardFromObj"):
        fn = extract_function(v1_js, name)
        assert "freshnessHtml(signalCardAsOf(s)" in fn
    # …and the two whale sentiment cards, which had the same shape.
    for name in ("renderWhaleSentiment", "renderWhaleSentimentEth"):
        fn = extract_function(v1_js, name)
        assert "freshnessHtml(whaleSentimentAsOf(s)" in fn


def test_money_flow_chip_never_renders_the_payload_as_of(v1_js):
    """`money_flow.as_of` is a clock read (see the resolver's comment and
    test_money_flow_as_of_really_is_a_clock_read below)."""
    fn = extract_function(v1_js, "moneyFlowFreshness")
    assert "mfx.as_of" not in _code_lines(fn), \
        "money_flow.as_of is datetime.now() — it must never reach a stamp"
    assert "fMin(parts)" in fn


def test_built_artifact_carries_the_helper(v1_js):
    """Guard against the template and the deployed artifact drifting apart."""
    if not V1_HTML.exists():
        pytest.skip("dashboard.html not built here")
    html = V1_HTML.read_text(encoding="utf-8")
    assert extract_function(v1_js, "freshness") in html


# ----------------- 5. Resolvers against synthetic payloads (in V8) -----------


_RESOLVERS = (
    "freshness", "freshnessDayUTC", "freshnessYmd",
    "fDay", "fLast", "fMin", "fMax",
    "pocTopFreshness", "signalsTop20Freshness", "perpsFreshness",
    "stocksFreshness", "futuresFreshness", "etfFreshness", "whaleFreshness",
    "whaleSentimentAsOf", "signalCardAsOf",
    "overviewFreshness", "defiFreshness", "aiNewsFreshness",
    "moneyFlowFreshness", "stockFlowFreshness", "lthcsFreshness",
    "cityFreshness", "aviationFreshness",
    "computeSignalBreadth",
)


@pytest.fixture(scope="module")
def resolve(v1_js):
    py_mini_racer = pytest.importorskip(
        "py_mini_racer", reason="V8 needed to execute the shipped JS")
    ctx = py_mini_racer.MiniRacer()
    bodies = "\n".join(extract_function(v1_js, n) for n in _RESOLVERS)
    ctx.eval("""
    var DATA = {}, state = {};
    function etfData(){ return (DATA.__etf) || {}; }
    function whaleData(){ return ((DATA.whale || {}).btc) || {}; }
    function socialData(){ return ((DATA.market || {}).social) || {}; }
    %s
    var __FNS = {%s};
    function __resolve(name, data, st, args){
      DATA = data; state = st || {};
      const r = __FNS[name].apply(null, args || []);
      return r === null || r === undefined ? null : r;
    }
    """ % (bodies, ", ".join("%s: %s" % (n, n) for n in _RESOLVERS)))

    def run(name, data, st=None, args=None):
        return ctx.call("__resolve", name, data, st or {}, args or [])

    return run


FROZEN_DAY = "2026-06-09"
FRESH_DAY = "2026-08-01"


def _mixed_age_poc_top(n_frozen=49):
    rows = [{"coin_id": f"frozen{i}", "as_of": FROZEN_DAY,
             "signal_history": [{"date": FROZEN_DAY, "score": 1}]}
            for i in range(n_frozen)]
    rows.append({"coin_id": "fresh", "as_of": FRESH_DAY,
                 "signal_history": [{"date": FRESH_DAY, "score": 9}]})
    return rows


def test_poc_resolver_takes_oldest_and_counts_cached(resolve):
    data = {"market": {"poc_top": [
        {"coin_id": "bitcoin",  "as_of": FRESH_DAY},
        {"coin_id": "ethereum", "as_of": FROZEN_DAY, "stale": True},
        {"coin_id": "solana",   "signal_history": [{"date": "2026-07-28"}]},
    ]}}
    r = resolve("pocTopFreshness", data)
    assert r["date"] == FROZEN_DAY
    assert r["stale"] == 1
    assert r["total"] == 3
    assert r["dated"] == 3


def test_breadth_last_bucket_really_is_the_max(resolve):
    """Establishes the trap the breadth stamp must not fall into."""
    rows = _mixed_age_poc_top()
    breadth = resolve("computeSignalBreadth", {},
                      args=[[{"history": r["signal_history"]} for r in rows], 90])
    assert breadth[-1]["date"] == FRESH_DAY, "the union's right edge is the NEWEST coin"


def test_poc_resolver_reports_the_min_on_the_same_fixture(resolve):
    r = resolve("pocTopFreshness", {"market": {"poc_top": _mixed_age_poc_top()}})
    assert r["date"] == FROZEN_DAY
    assert r["total"] == 50


def test_signals_resolver_refuses_ungated_as_of(resolve):
    rows = [{"symbol": "BTC", "as_of": "2026-08-03"}]
    r = resolve("signalsTop20Freshness", {"signals_top20": rows})
    assert r["date"] is None
    assert r["trusted"] is False


def test_signals_resolver_trusts_gated_as_of(resolve):
    rows = [{"symbol": "BTC", "as_of": FROZEN_DAY, "computed_at": "2026-08-03T00:00:00Z"},
            {"symbol": "ETH", "as_of": FRESH_DAY, "computed_at": "2026-08-03T00:00:00Z",
             "stale": True}]
    r = resolve("signalsTop20Freshness", {"signals_top20": rows})
    assert r["date"] == FROZEN_DAY
    assert r["stale"] == 1


def test_stocks_resolver_counts_undated_and_cached_rows(resolve):
    rows = [{"symbol": "A", "as_of": FROZEN_DAY, "stale": True},
            {"symbol": "B", "as_of": FRESH_DAY},
            {"symbol": "C"}]
    r = resolve("stocksFreshness", {"market": {"stocks_signals": rows}})
    assert r["date"] == FROZEN_DAY
    assert r["stale"] == 1
    assert r["dated"] == 2 and r["total"] == 3


def test_futures_resolver_is_min_of_three_and_follows_asset(resolve):
    data = {"market": {
        "btc": {"funding": [{"date": FROZEN_DAY}], "long_short_ratio": [{"date": FRESH_DAY}],
                "open_interest_usd": [{"date": FRESH_DAY}]},
        "eth": {"funding": [{"date": "2026-07-20"}]},
    }}
    assert resolve("futuresFreshness", data, args=["btc"])["date"] == FROZEN_DAY
    assert resolve("futuresFreshness", data, args=["eth"])["date"] == "2026-07-20"


def test_whale_resolver_is_min_not_max(resolve):
    """THE regression test for #whaleAsOf's fMax reduce.

    The old code was `candidates.reduce((a,b) => a.date >= b.date ? a : b)`,
    which would answer 2026-08-01 here while active_addresses sat frozen.
    """
    data = {"whale": {"btc": {
        "tx_volume_usd": [{"date": FRESH_DAY}],
        "active_addresses": [{"date": FROZEN_DAY}],
        "miners_revenue_usd": [{"date": FRESH_DAY}],
    }}}
    r = resolve("whaleFreshness", data, args=["btc"])
    assert r["date"] == FROZEN_DAY


def test_whale_resolver_is_asset_scoped(resolve):
    """The ETH panel must not report BTC's dates."""
    data = {"whale": {
        "btc": {"tx_volume_usd": [{"date": FROZEN_DAY}]},
        "eth": {"coin_metrics": {"AdrActCnt": [{"date": "2026-07-20"}],
                                 "TxCnt": [{"date": FRESH_DAY}]}},
    }}
    assert resolve("whaleFreshness", data, args=["btc"])["date"] == FROZEN_DAY
    assert resolve("whaleFreshness", data, args=["eth"])["date"] == "2026-07-20"


def test_whale_resolver_is_null_when_primary_series_are_absent(resolve):
    """Rule 5, and the V2 lane's blocker 2: with no dated series and only a
    pre-fix sentiment block, the answer is nothing — never today."""
    poisoned = {"whale": {"btc": {}, "sentiment": {"as_of": "2026-08-03"}}}
    assert resolve("whaleFreshness", poisoned, args=["btc"]) is None
    assert resolve("whaleFreshness", {"whale": {"eth": {}}}, args=["eth"]) is None


def test_whale_sentiment_as_of_is_gated(resolve):
    poisoned = {"as_of": "2026-08-03"}
    fixed = {"as_of": FROZEN_DAY, "as_of_basis": "oldest contributing on-chain series"}
    assert resolve("whaleSentimentAsOf", {}, args=[poisoned]) is None
    assert resolve("whaleSentimentAsOf", {}, args=[fixed]) == FROZEN_DAY
    assert resolve("whaleSentimentAsOf", {}, args=[None]) is None


def test_defi_resolver_prefers_fetcher_as_of(resolve):
    data = {"defi": {"as_of": "2026-07-15",
                     "tvl_history": {"eth": [{"date": FRESH_DAY}]}}}
    assert resolve("defiFreshness", data)["date"] == "2026-07-15"


def test_defi_resolver_falls_back_to_tvl_history(resolve):
    data = {"defi": {"tvl_history": {"eth": [{"date": FROZEN_DAY}],
                                     "sol": [{"date": FRESH_DAY}]}}}
    assert resolve("defiFreshness", data)["date"] == FROZEN_DAY


def test_defi_resolver_unavailable_rather_than_fetch_time(resolve):
    data = {"defi": {"snapshot_fetched_at": "2026-08-03T00:00:00Z",
                     "chains": [{"name": "eth", "tvl_usd": 1}]}}
    assert resolve("defiFreshness", data)["date"] is None


def test_overview_resolver_is_min_of_dated_series(resolve):
    data = {"market": {"btc": {"price": [{"date": FROZEN_DAY}]},
                       "eth": {"price": [{"date": FRESH_DAY}]},
                       "fear_greed": [{"date": FRESH_DAY}]}}
    assert resolve("overviewFreshness", data)["date"] == FROZEN_DAY


def test_perps_resolver_uses_exchange_quote_timestamp(resolve):
    data = {"market": {"coinbase_intl_perps": [
        {"symbol": "A", "as_of": FRESH_DAY}, {"symbol": "B", "as_of": FROZEN_DAY}]}}
    assert resolve("perpsFreshness", data)["date"] == FROZEN_DAY


def test_ainews_resolver_takes_newest_item(resolve):
    data = {"market": {"ai_news": {"items": [{"date": FROZEN_DAY}, {"date": FRESH_DAY}]}}}
    assert resolve("aiNewsFreshness", data)["date"] == FRESH_DAY


def test_etf_resolver_reads_the_daily_series(resolve):
    data = {"__etf": {"daily": [{"date": "2026-05-10"}, {"date": "2026-05-12"}],
                      "stats": {"last_date": "2026-05-12"}}}
    assert resolve("etfFreshness", data)["date"] == "2026-05-12"


# --- V1-only surfaces --------------------------------------------------------


def test_money_flow_resolver_ignores_the_payload_as_of(resolve):
    """`money_flow.as_of` is datetime.now(). The resolver must read the ICI
    weeklies and the equity-ETF flow history instead — and take the oldest."""
    data = {"market": {"money_flow": {
        "as_of": "2026-08-03",                       # the clock read
        "per_index": [{"name": "Dow"}, {"name": "S&P"}, {"name": "Nasdaq"}],
        "sources": {
            "mmf": {"weekly": [{"date": "2026-07-15"}, {"date": "2026-07-22"}]},
            "mf_flows": {"weekly": [{"date": "2026-07-08"}]},
            "equity_etf_flows": {"tickers": {"SPY": {"history": [{"date": FRESH_DAY}]}}},
        }}}}
    r = resolve("moneyFlowFreshness", data)
    assert r["date"] == "2026-07-08", "must be the oldest dated input, not the clock"
    assert "2026-08-03" != r["date"]
    assert "clock read" in r["title"]
    assert "per-index MFI/CMF legs" in r["title"], "undated inputs must be disclosed"


def test_money_flow_resolver_is_unavailable_with_no_dated_sources(resolve):
    data = {"market": {"money_flow": {"as_of": "2026-08-03", "sources": {}}}}
    assert resolve("moneyFlowFreshness", data)["date"] is None


def test_stockflow_resolver_takes_the_oldest_bar(resolve):
    data = {"stockflow": {"as_of": FROZEN_DAY, "stocks": [
        {"ticker": "A", "as_of": FROZEN_DAY}, {"ticker": "B", "as_of": FRESH_DAY},
        {"ticker": "C"}]}}
    r = resolve("stockFlowFreshness", data)
    assert r["date"] == FROZEN_DAY
    assert "2 of 3 rows carry a date" in r["title"]


def test_stockflow_resolver_recomputes_when_as_of_missing(resolve):
    data = {"stockflow": {"stocks": [{"ticker": "A", "as_of": FRESH_DAY},
                                     {"ticker": "B", "as_of": FROZEN_DAY}]}}
    assert resolve("stockFlowFreshness", data)["date"] == FROZEN_DAY


def test_lthcs_resolver_reads_the_index_snapshot_date(resolve):
    data = {"lthcs": {"available": True, "as_of": "2026-07-01",
                      "index": {"as_of": "2026-08-01"}}}
    assert resolve("lthcsFreshness", data)["date"] == "2026-08-01"
    assert resolve("lthcsFreshness", {"lthcs": {"available": False}}) is None


def test_city_resolver_normalises_a_month_bucket_conservatively(resolve):
    """'2026-04' becomes 2026-04-01 — the OLDER reading of the bucket."""
    r = resolve("cityFreshness", {"city": {"as_of": "2026-04", "cities": [1, 2]}})
    assert r["date"] == "2026-04-01"
    assert "normalised to the 1st" in r["title"]


def test_aviation_resolver_refuses_a_prose_vintage(resolve):
    """Rule 5: an unparseable prose vintage is 'as of —' plus disclosure."""
    r = resolve("aviationFreshness", {"aviation": {"asOf": "FAA airman data Dec 31 2025"}})
    assert r["date"] is None
    assert "FAA airman data Dec 31 2025" in r["title"]


# ------------- 6. The Money Flow clock read, now fixed at the source ---------
#
# This section used to hold `test_money_flow_as_of_really_is_a_clock_read`,
# which ASSERTED that money_flow.build_money_flow_index() answered
# datetime.now(UTC) when the payload carried no observation date — the premise
# behind moneyFlowFreshness() refusing `mfx.as_of` outright. Its own docstring
# said "if the data layer starts passing a real observation date, this test
# fails and the resolver can be simplified". That is exactly what happened: the
# clock read is gone from money_flow.py, the legs now carry real dates from
# fetch_market.build_money_flow_payload(), and the composite reports the OLDEST
# contributing leg or None. The three tests below replace it and pin the new
# contract from both ends.
#
# FOR THE RESOLVER LANE: moneyFlowFreshness() can now read `mfx.as_of`
# directly when it is non-null, and fall back to its disclosure text (using
# `mfx.as_of_inputs.undated_contributors`) when it is null. Deliberately not
# changed here — app.py is not this lane's file.


def _mf_module():
    sys.path.insert(0, str(ROOT))
    return pytest.importorskip("money_flow")


def test_money_flow_never_manufactures_a_date():
    """Rule 4 at the source: an undatable composite says None, not today."""
    mf = _mf_module()
    out = mf.build_money_flow_index({"market": {"DIA": {}, "SPY": {}, "QQQ": {}}})
    assert out["as_of"] is None, (
        f"money_flow invented an as_of ({out['as_of']!r}) for a payload with no "
        f"data in it at all")
    # Structural, not behavioural: with no module-scope datetime the library
    # half of money_flow physically cannot read a clock, so this cannot regress
    # quietly through some new code path.
    assert not hasattr(mf, "datetime"), (
        "money_flow imported datetime at module scope again — that is the door "
        "the clock read walked through the first time")


def test_money_flow_as_of_is_the_oldest_contributing_leg():
    """Rule 2: a composite is only as fresh as its OLDEST input — min, not max."""
    mf = _mf_module()
    leg = lambda day: {"etf_flow": 5, "etf_flow_hist": [1, 2, 3],
                       "mfi": 60, "mfi_hist": [40, 50, 55], "as_of": day}
    out = mf.build_money_flow_index({"market": {
        "DIA": leg("2026-07-31"), "SPY": leg("2026-06-15"), "QQQ": leg("2026-08-01"),
        "ici_equity_flow": 3, "ici_equity_flow_hist": [1, 2, 4],
        "ici_as_of": "2026-07-25",
        "mmf_wow_change": 2, "mmf_wow_change_hist": [1, 3, 5],
        "mmf_as_of": "2026-07-24",
    }})
    assert out["as_of"] == "2026-06-15", out["as_of_inputs"]
    assert out["as_of_inputs"]["resolved_from"] == "oldest contributing leg"


def test_money_flow_undated_contributor_makes_the_date_unknown():
    """A dated subset is not the answer when an undated leg also contributed:
    the true oldest could be older still, so min() over what we have would
    overstate freshness. None + disclosure instead."""
    mf = _mf_module()
    leg = lambda day: {"etf_flow": 5, "etf_flow_hist": [1, 2, 3],
                       "mfi": 60, "mfi_hist": [40, 50, 55], **({"as_of": day} if day else {})}
    market = {"DIA": leg("2026-07-31"), "SPY": leg(None), "QQQ": leg("2026-08-01")}
    out = mf.build_money_flow_index({"market": market})
    assert out["as_of"] is None
    assert out["as_of_inputs"]["undated_contributors"] == ["SPY"]
    # An undated leg that contributes NOTHING must not poison the date — it
    # supplied no observation, so it cannot make the composite older.
    market["SPY"] = {}
    out = mf.build_money_flow_index({"market": market})
    assert out["as_of"] == "2026-07-31", out["as_of_inputs"]


# ------------------------ 7. Renderer executed against a stub DOM ------------


@pytest.fixture(scope="module")
def paint(v1_js):
    """Execute the real breadth renderer against a stub DOM and read the
    stamp back. Greps prove a call site LOOKS right; this proves what the
    element ends up saying."""
    py_mini_racer = pytest.importorskip(
        "py_mini_racer", reason="V8 needed to execute the shipped JS")
    ctx = py_mini_racer.MiniRacer()
    names = ("freshness", "freshnessDayUTC", "freshnessYmd", "paintFreshness",
             "fDay", "fLast", "fMin", "fMax", "pocTopFreshness",
             "computeSignalBreadth", "renderCryptoSignalsBreadth")
    bodies = "\n".join(extract_function(v1_js, n) for n in names)
    ctx.eval("""
    var DATA = {}, state = {}, __ELS = {};
    var __chartCalls = 0;
    function renderBreadthChart(){ __chartCalls++; }
    var document = {
      getElementById: function(id){
        if (!__ELS[id]) __ELS[id] = {id: id, textContent: '', className: '',
                                     style: {}, attrs: {},
                                     setAttribute: function(k,v){ this.attrs[k]=v; },
                                     removeAttribute: function(k){ delete this.attrs[k]; }};
        return __ELS[id];
      }
    };
    %s
    function __paintBreadth(data){
      DATA = data; __ELS = {}; __chartCalls = 0;
      renderCryptoSignalsBreadth();
      const el = __ELS['cryptoSignalsBreadthFresh'] || {};
      return {text: el.textContent || '', cls: el.className || '',
              title: (el.attrs || {}).title || '', charts: __chartCalls};
    }
    function __paintBreadthWithBrokenChart(data){
      DATA = data; __ELS = {}; __chartCalls = 0;
      const real = renderBreadthChart;
      renderBreadthChart = function(){ throw new ReferenceError('Chart is not defined'); };
      let threw = false;
      try { renderCryptoSignalsBreadth(); } catch (e) { threw = true; }
      renderBreadthChart = real;
      const el = __ELS['cryptoSignalsBreadthFresh'] || {};
      return {text: el.textContent || '', threw: threw};
    }
    """ % bodies)

    def run(data, broken=False):
        fn = "__paintBreadthWithBrokenChart" if broken else "__paintBreadth"
        return ctx.call(fn, data)

    return run


def test_breadth_stamp_paints_the_oldest_coin_not_the_right_edge(paint):
    """THE regression test for the max-vs-min swap."""
    out = paint({"market": {"poc_top": _mixed_age_poc_top()}})
    assert FROZEN_DAY in out["text"], out
    assert FRESH_DAY not in out["text"], out
    assert "v2-fresh--bad" in out["cls"], out


def test_breadth_stamp_counts_cached_coins(paint):
    rows = _mixed_age_poc_top()
    rows[0]["stale"] = True
    rows[1]["stale"] = True
    out = paint({"market": {"poc_top": rows}})
    assert "2 of 50 cached" in out["text"], out


def test_breadth_stamp_is_dashed_when_no_coin_is_datable(paint):
    rows = [{"coin_id": "x", "signal_history": [{"score": 1}]}]
    out = paint({"market": {"poc_top": rows}})
    assert out["text"].endswith("—"), out
    assert "v2-fresh--none" in out["cls"], out


def test_breadth_stamp_survives_a_chart_failure(paint):
    """Chart.js is a CDN dep behind an SRI pin. When it dies the stamp lives."""
    out = paint({"market": {"poc_top": _mixed_age_poc_top()}}, broken=True)
    assert out["threw"] is True, "the stub must actually raise"
    assert FROZEN_DAY in out["text"], out
