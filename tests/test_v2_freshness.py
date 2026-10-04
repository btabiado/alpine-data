"""Contract tests for the V2 dashboard's data-freshness stamps.

Two layers:

1. **Python builder** — ``v2/app.py``'s DeFi provenance backfill. The DeFi
   subtree used to ship with no date of any kind, which left the whole tab
   with nothing honest to stamp.

2. **The shipped JavaScript** — ``freshness()`` is the single implementation
   behind every stamp on the page (header, tab strips, composite cards,
   breadth charts, whale/ETF badges). Its source is extracted verbatim from
   ``v2/app.py``'s HTML template and executed in V8 with a pinned clock, so
   these assertions run against the code that actually ships rather than a
   reimplementation of it.

Why this file exists: the failure mode being guarded is a stamp that reports
BUILD time instead of DATA time. That reads as "just now" on a page which
rebuilds hourly, and it is what let the crypto breadth chart sit frozen at
2026-06-09 for eight weeks looking completely current. A stamp that lies is
worse than no stamp, so the honesty rules get tests.
"""
from __future__ import annotations

import importlib.util
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
V2_APP = ROOT / "v2" / "app.py"
V2_HTML = ROOT / "v2" / "dashboard.html"


# ---------------------------------------------------------------- helpers ---


@pytest.fixture(scope="module")
def v2app():
    """Import ``v2/app.py`` under its own module name.

    It cannot be imported as ``app`` — the repo root already owns that name
    and the two modules are different builders.
    """
    if not V2_APP.exists():  # pragma: no cover - repo layout guard
        pytest.skip("v2/app.py not present")
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location("v2_app_under_test", V2_APP)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as e:  # pragma: no cover - dependency guard
        pytest.skip(f"v2/app.py not importable here: {e}")
    return mod


@pytest.fixture(scope="module")
def v2_js() -> str:
    """The inline dashboard JS, taken from the builder's template string."""
    src = V2_APP.read_text(encoding="utf-8")
    marker = 'HTML_TEMPLATE = r"""'
    i = src.index(marker)
    return src[i + len(marker):]


def extract_function(js: str, name: str) -> str:
    """Return the full source of top-level ``function name(...){...}``.

    Brace-matched rather than regex-terminated so nested blocks, object
    literals and template strings inside the body survive intact.
    """
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


@pytest.fixture(scope="module")
def freshness_ctx(v2_js):
    """A V8 context exposing ``call(iso, opts, nowIso)`` for freshness().

    The clock is injected per call by shadowing ``Date`` inside a factory
    closure, so age assertions are deterministic instead of depending on
    when the suite runs.
    """
    py_mini_racer = pytest.importorskip(
        "py_mini_racer", reason="V8 needed to execute the shipped JS")
    ctx = py_mini_racer.MiniRacer()

    bodies = "\n".join(
        extract_function(v2_js, n)
        for n in ("freshness", "freshnessDayUTC", "freshnessYmd")
    )
    # RealDate is passed in as a parameter: `const Date = D` below puts the
    # name Date in TDZ for the whole factory body, so the real constructor
    # has to be captured before the body starts.
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

    def call(iso, opts=None, now="2026-08-02T12:00:00Z"):
        import json
        return ctx.call("__call", iso, opts or {}, now)

    return call


# --------------------------------------------------- 1. Python: DeFi as_of ---


def test_defi_observation_date_takes_oldest_chain(v2app):
    """A composite is only as fresh as its OLDEST input — min, not max."""
    defi = {"tvl_history": {
        "Ethereum": [{"date": "2026-07-30", "tvl_usd": 1}, {"date": "2026-08-01", "tvl_usd": 2}],
        "Solana":   [{"date": "2026-07-28", "tvl_usd": 3}],
        "Base":     [{"date": "2026-08-01", "tvl_usd": 4}],
    }}
    assert v2app.defi_observation_date(defi) == "2026-07-28"


def test_defi_observation_date_ignores_series_order(v2app):
    """An out-of-order series must not understate its own last date."""
    defi = {"tvl_history": {"Ethereum": [
        {"date": "2026-08-01"}, {"date": "2026-07-20"},
    ]}}
    assert v2app.defi_observation_date(defi) == "2026-08-01"


@pytest.mark.parametrize("defi", [
    {},
    {"tvl_history": {}},
    {"tvl_history": {"Ethereum": []}},
    {"tvl_history": {"Ethereum": [{"tvl_usd": 1}]}},
    {"tvl_history": "not-a-dict"},
])
def test_defi_observation_date_none_when_undatable(v2app, defi):
    """No date available ⇒ None. Never a fabricated stand-in."""
    assert v2app.defi_observation_date(defi) is None


def test_stamp_defi_provenance_fills_missing_as_of(v2app):
    defi = {"tvl_history": {"Ethereum": [{"date": "2026-07-31"}]}}
    v2app.stamp_defi_provenance(defi, {"fetched_at": "2026-08-02T21:00:00+00:00"})
    assert defi["as_of"] == "2026-07-31"
    assert defi["snapshot_fetched_at"] == "2026-08-02T21:00:00+00:00"


def test_stamp_defi_provenance_does_not_clobber_fetcher_as_of(v2app):
    """fetch_market.defi_provenance() writes a richer answer — keep it."""
    defi = {"as_of": "2026-07-01",
            "tvl_history": {"Ethereum": [{"date": "2026-07-31"}]}}
    v2app.stamp_defi_provenance(defi, {"fetched_at": "2026-08-02T21:00:00+00:00"})
    assert defi["as_of"] == "2026-07-01"


def test_stamp_defi_provenance_never_uses_fetch_time_as_as_of(v2app):
    """THE core rule: with no datable series, as_of stays None.

    Falling back to ``fetched_at`` here would recreate the exact bug this
    whole change exists to kill.
    """
    defi = {"chains": [{"name": "Ethereum", "change_7d_pct": 1.2}]}
    v2app.stamp_defi_provenance(defi, {"fetched_at": "2026-08-02T21:00:00+00:00"})
    assert defi["as_of"] is None
    assert defi["snapshot_fetched_at"] == "2026-08-02T21:00:00+00:00"


# ------------------------------------------- 2. Shipped JS: freshness() ------


def test_freshness_null_renders_explicit_unavailable(freshness_ctx):
    r = freshness_ctx(None)
    assert r["text"] == "as of —"
    assert r["tone"] == "none"
    assert r["ageDays"] is None


def test_freshness_respects_custom_label_when_unavailable(freshness_ctx):
    assert freshness_ctx(None, {"label": "last bar"})["text"] == "last bar —"


@pytest.mark.parametrize("bad", ["", "   ", "nonsense", "2026-13-01", "2026-02-31"])
def test_freshness_unparseable_is_unavailable_not_today(freshness_ctx, bad):
    """A malformed date must never silently resolve to a nearby real day."""
    r = freshness_ctx(bad)
    assert r["tone"] == "none", f"{bad!r} should not parse"
    assert r["ageDays"] is None


def test_freshness_text_shape(freshness_ctx):
    r = freshness_ctx("2026-07-30", now="2026-08-02T12:00:00Z")
    assert r["text"] == "as of 2026-07-30 (3d ago)"
    assert r["ageDays"] == 3
    assert r["tone"] == "ok"


def test_freshness_iso_datetime_renders_date_only(freshness_ctx):
    r = freshness_ctx("2026-07-30T16:49:31.736Z", now="2026-08-02T12:00:00Z")
    assert r["text"] == "as of 2026-07-30 (3d ago)"


@pytest.mark.parametrize("age,tone", [
    (0, "ok"), (7, "ok"),            # warnDays boundary is inclusive-ok
    (8, "warn"), (21, "warn"),       # badDays boundary is inclusive-warn
    (22, "bad"), (54, "bad"),        # the frozen-breadth-chart case
])
def test_freshness_tone_thresholds(freshness_ctx, age, tone):
    now = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
    d = (now - timedelta(days=age)).strftime("%Y-%m-%d")
    r = freshness_ctx(d, now=now.isoformat().replace("+00:00", "Z"))
    assert r["ageDays"] == age
    assert r["tone"] == tone


def test_freshness_custom_thresholds(freshness_ctx):
    r = freshness_ctx("2026-08-01", {"warnDays": 0, "badDays": 0},
                      now="2026-08-02T12:00:00Z")
    assert r["ageDays"] == 1 and r["tone"] == "bad"


def test_freshness_never_reports_negative_age_across_timezones(freshness_ctx):
    """Whole days in UTC — the guard against '(-1d ago)'.

    A viewer at UTC+13 sees "tomorrow's" date locally while the observation
    is stamped in UTC. Computed against UTC midnight this is 0d, and a
    genuinely future-dated row floors at 0 rather than going negative.
    """
    assert freshness_ctx("2026-08-02", now="2026-08-02T23:59:59Z")["ageDays"] == 0
    assert freshness_ctx("2026-08-02", now="2026-08-02T00:00:00Z")["ageDays"] == 0
    r = freshness_ctx("2026-08-09", now="2026-08-02T12:00:00Z")
    assert r["ageDays"] == 0
    assert "-1d" not in r["text"] and "-" not in r["text"].split("(")[1]


def test_freshness_discloses_cached_count(freshness_ctx):
    r = freshness_ctx("2026-07-30", {"stale": 12, "total": 50},
                      now="2026-08-02T12:00:00Z")
    assert r["text"] == "as of 2026-07-30 (3d ago) · 12 of 50 cached"


def test_freshness_omits_cached_suffix_when_nothing_cached(freshness_ctx):
    for opts in ({"stale": 0, "total": 50}, {"stale": None, "total": None}, {}):
        assert "cached" not in freshness_ctx("2026-07-30", opts,
                                             now="2026-08-02T12:00:00Z")["text"]


# ------------------------------------- 3. Wiring: what the page actually does ---


EXPECTED_STAMP_IDS = [
    # header
    "dataFreshness",
    # composite index cards
    "overviewSentimentFresh", "etfFlowSentimentFresh", "futuresSentimentFresh",
    "stocksSentimentFresh", "cryptoSignalsSentimentFresh", "pocSentimentFresh",
    "defiSentimentFresh",
    # breadth charts
    "cryptoSignalsBreadthFresh", "stocksBreadthFresh",
    # pre-existing badges kept, now on the shared helper
    "whaleAsOf", "etfAsOf",
]

EXPECTED_TAB_STRIPS = [
    "overview", "signals", "whale", "poc", "social", "defi", "etf", "trading",
    "stocks", "ainews", "travel", "cpi", "supplies", "metals", "mufon",
]


@pytest.mark.parametrize("elem_id", EXPECTED_STAMP_IDS)
def test_stamp_element_exists(v2_js, elem_id):
    assert f'id="{elem_id}"' in v2_js, f"missing stamp element #{elem_id}"


@pytest.mark.parametrize("tab", EXPECTED_TAB_STRIPS)
def test_every_tab_has_a_freshness_strip(v2_js, tab):
    """Every tab gets a strip — this is the mobile-visible stamp, since
    `header .meta` (which carries the global one) is display:none <480px."""
    assert f'id="tabFresh-{tab}"' in v2_js
    assert f"{tab}:" in v2_js.split("const TAB_FRESHNESS = {")[1].split("};")[0]


def test_build_stamp_is_labelled_built_not_generated(v2_js):
    """The one place build time may appear must name itself as build time."""
    assert "'built ' + (DATA.generated_at || '—')" in v2_js
    # …and nothing else may render generated_at as a freshness date.
    assert "generated ' + DATA.generated_at" not in v2_js


# Every name that is a CLOCK READ rather than an observation date.
#
# `generated_at` alone was not enough: the whale composite shipped for a
# release stamping itself with `whale.fetched_at[:10]` — a fetch clock that
# advances every run even when the stale-keep path served yesterday's
# numbers — and 126 green tests never noticed, because the guard below only
# knew about one of the four spellings.
CLOCK_FIELDS = ("generated_at", "fetched_at", "datetime.now", "Date.now",
                "computed_at", "snapshot_fetched_at", "observed_at")


def test_no_stamp_is_fed_from_build_or_fetch_time(v2_js):
    """No clock read may reach freshness()/paintFreshness()/freshnessHtml().

    Checks the FIRST argument (the date) of every stamp call — later
    arguments legitimately mention fetch time in hover copy ("Last fetch
    attempt … that is fetch time, not a data date"), which is disclosure,
    not a stamp.
    """
    for call in re.findall(
            r"(?:paintFreshness|freshnessHtml|freshness)\(([^\n]*)", v2_js):
        # First argument = up to the first top-level comma.
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
    "signalCardAsOf",
)


def test_no_resolver_pipes_a_clock_read_into_a_date(v2_js):
    """The per-surface resolvers decide WHICH date each stamp gets.

    A clock read may appear in hover copy (labelled as fetch time — that is
    disclosure) but it may never flow through the date plumbing:
    fDay/fLast/fMin/fMax, or the `date:` field of the returned object. That
    laundering is exactly how the whale stamp shipped `fetched_at` under a
    tooltip promising an observation date.
    """
    date_channel = re.compile(r"(?:fDay|fLast|fMin|fMax)\([^;\n]*|date:[^,\n]*")
    for name in RESOLVER_NAMES:
        fn = extract_function(v2_js, name)
        # Drop whole-line comments so prose about fetch time doesn't trip it.
        body = "\n".join(l for l in fn.splitlines()
                         if not l.strip().startswith("//"))
        for frag in date_channel.findall(body):
            for bad in CLOCK_FIELDS:
                assert bad not in frag, (
                    f"{name}() pipes clock field {bad!r} into a date: {frag[:120]}")


def test_signals_top20_as_of_is_gated_on_the_provenance_fix(v2_js):
    """signals_top20[].as_of used to be datetime.now(UTC).

    It is only readable once the row proves it came from the fixed
    signals.py (which also emits `computed_at`). Losing that gate would
    resurrect the poisoned field on any older cached payload.
    """
    fn = extract_function(v2_js, "signalsTop20Freshness")
    assert "typeof r.computed_at === 'string'" in fn
    assert "trustworthy" in fn
    # computed_at is build time — it must be a gate, never a rendered date.
    assert "fDay(r.computed_at)" not in fn


def test_composite_stamps_use_min_not_max(v2_js):
    """Rule 3: composites stamp the OLDEST contributing input."""
    for name in ("pocTopFreshness", "stocksFreshness", "futuresFreshness",
                 "overviewFreshness", "signalsTop20Freshness"):
        fn = extract_function(v2_js, name)
        assert "fMin(" in fn, f"{name} must take the oldest input"
        assert "fMax(" not in fn, f"{name} must not take the newest input"


def test_feed_surfaces_use_max(v2_js):
    """…and 'latest item in a feed' surfaces legitimately use the newest."""
    for name in ("aiNewsFreshness", "travelFreshness"):
        assert "fMax(" in extract_function(v2_js, name)


def test_breadth_chart_discloses_cached_coin_count(v2_js):
    """Rule 4 on the chart that motivated all of this.

    A date alone still misleads: the coins that fetched fine hold the
    series' last date current while cache-served ones freeze their own
    contribution.
    """
    fn = extract_function(v2_js, "renderCryptoSignalsBreadth")
    assert "pocTopFreshness()" in fn
    assert "stale:" in fn and "total:" in fn


def test_whale_as_of_has_exactly_one_implementation(v2_js):
    """The two hand-rolled #whaleAsOf writers were consolidated."""
    assert v2_js.count("getElementById('whaleAsOf')") == 1
    fn = extract_function(v2_js, "renderWhaleAsOf")
    assert "state.whaleAsset" in fn, "must follow the BTC/ETH panel toggle"
    assert "paintFreshness(" in fn


def test_built_artifact_carries_the_helper(v2_js):
    """Guard against the template and the artifact drifting apart."""
    if not V2_HTML.exists():
        pytest.skip("v2/dashboard.html not built here")
    html = V2_HTML.read_text(encoding="utf-8")
    assert extract_function(v2_js, "freshness") in html


# ----------------- 4. Resolvers against synthetic payloads (in V8) -----------
# The resolvers are where "which date do we trust" actually gets decided, so
# they get executed rather than grepped. No DOM is involved — these are pure
# functions of DATA — so they run in V8 with DATA/state/etfData/whaleData
# stubbed to whatever shape the case needs.

_RESOLVERS = (
    "freshness", "freshnessDayUTC", "freshnessYmd",
    "fDay", "fLast", "fMin", "fMax",
    "pocTopFreshness", "signalsTop20Freshness", "perpsFreshness",
    "stocksFreshness", "futuresFreshness", "etfFreshness", "whaleFreshness",
    "whaleSentimentAsOf", "signalCardAsOf",
    "overviewFreshness", "defiFreshness", "aiNewsFreshness",
    "computeSignalBreadth",
)


@pytest.fixture(scope="module")
def resolve(v2_js):
    py_mini_racer = pytest.importorskip(
        "py_mini_racer", reason="V8 needed to execute the shipped JS")
    ctx = py_mini_racer.MiniRacer()
    bodies = "\n".join(extract_function(v2_js, n) for n in _RESOLVERS)
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


def test_poc_resolver_takes_oldest_and_counts_cached(resolve):
    data = {"market": {"poc_top": [
        {"coin_id": "bitcoin",  "as_of": "2026-08-01"},
        {"coin_id": "ethereum", "as_of": "2026-06-09", "stale": True},
        {"coin_id": "solana",   "signal_history": [{"date": "2026-07-28"}]},
    ]}}
    r = resolve("pocTopFreshness", data)
    assert r["date"] == "2026-06-09"   # oldest, and it is the cached one
    assert r["stale"] == 1
    assert r["total"] == 3


def test_poc_resolver_falls_back_to_signal_history(resolve):
    """Older cached market.json has no per-entry as_of — still datable."""
    data = {"market": {"poc_top": [
        {"coin_id": "x", "signal_history": [{"date": "2026-07-01"},
                                            {"date": "2026-07-30"}]},
    ]}}
    assert resolve("pocTopFreshness", data)["date"] == "2026-07-30"


def test_signals_resolver_refuses_ungated_as_of(resolve):
    """Pre-fix payload: as_of is a clock read. Must NOT be rendered."""
    data = {"signals_top20": [{"symbol": "BTC", "as_of": "2026-08-02"}]}
    r = resolve("signalsTop20Freshness", data)
    assert r["date"] is None
    assert r["trusted"] is False


def test_signals_resolver_trusts_gated_as_of(resolve):
    data = {"signals_top20": [
        {"symbol": "BTC", "as_of": "2026-08-02", "stale": False,
         "computed_at": "2026-08-02T21:00:00+00:00"},
        {"symbol": "ETH", "as_of": "2026-07-25", "stale": True,
         "computed_at": "2026-08-02T21:00:00+00:00"},
    ]}
    r = resolve("signalsTop20Freshness", data)
    assert r["trusted"] is True
    assert r["date"] == "2026-07-25"
    assert r["stale"] == 1 and r["total"] == 2


def test_futures_resolver_is_min_of_three_and_follows_asset(resolve):
    """Three real dates → min of three, recomputed per asset toggle."""
    data = {"market": {
        "btc": {"funding": [{"date": "2026-08-02"}],
                "long_short_ratio": [{"date": "2026-08-01"}],
                "open_interest_usd": [{"date": "2026-07-29"}]},
        "eth": {"funding": [{"date": "2026-06-01"}]},
    }}
    btc = resolve("futuresFreshness", data, args=["btc"])
    assert btc["date"] == "2026-07-29" and btc["total"] == 3
    eth = resolve("futuresFreshness", data, args=["eth"])
    assert eth["date"] == "2026-06-01" and eth["total"] == 1
    assert resolve("futuresFreshness", data, args=["link"]) is None


def test_stocks_resolver_prefers_row_as_of(resolve):
    data = {"market": {"stocks_signals": [
        {"symbol": "AAPL", "as_of": "2026-08-01",
         "history": [{"date": "2026-08-01"}]},
        {"symbol": "MSFT", "history": [{"date": "2026-07-31"}]},
    ]}}
    r = resolve("stocksFreshness", data)
    assert r["date"] == "2026-07-31"
    assert r["total"] == 2


def test_defi_resolver_prefers_fetcher_as_of(resolve):
    data = {"defi": {"as_of": "2026-07-20",
                     "observed_at": "2026-08-02T21:00:00+00:00",
                     "tvl_history": {"Ethereum": [{"date": "2026-08-01"}]}}}
    r = resolve("defiFreshness", data)
    assert r["date"] == "2026-07-20"
    assert "fetch time" in r["title"]


def test_defi_resolver_falls_back_to_tvl_history(resolve):
    data = {"defi": {"tvl_history": {
        "Ethereum": [{"date": "2026-08-01"}],
        "Solana":   [{"date": "2026-07-26"}],
    }}}
    assert resolve("defiFreshness", data)["date"] == "2026-07-26"


def test_defi_resolver_unavailable_rather_than_fetch_time(resolve):
    data = {"defi": {"chains": [{"name": "Ethereum"}],
                     "observed_at": "2026-08-02T21:00:00+00:00"}}
    assert resolve("defiFreshness", data)["date"] is None


def test_overview_resolver_is_min_of_dated_series(resolve):
    data = {"market": {
        "btc": {"price": [{"date": "2026-08-02"}]},
        "eth": {"price": [{"date": "2026-08-02"}]},
        "fear_greed": [{"date": "2026-07-30", "value": 55}],
    }}
    assert resolve("overviewFreshness", data)["date"] == "2026-07-30"


def test_perps_resolver_uses_exchange_quote_timestamp(resolve):
    data = {"market": {"coinbase_intl_perps": [
        {"symbol": "BTC", "as_of": "2026-08-02", "as_of_ts": "2026-08-02T10:00:00Z"},
        {"symbol": "ETH", "as_of": "2026-08-01"},
    ]}}
    r = resolve("perpsFreshness", data)
    assert r["date"] == "2026-08-01" and r["total"] == 2


def test_perps_resolver_unavailable_on_old_undated_rows(resolve):
    data = {"market": {"coinbase_intl_perps": [{"symbol": "BTC", "funding_rate": 1e-5}]}}
    assert resolve("perpsFreshness", data)["date"] is None


def test_ainews_resolver_takes_newest_item(resolve):
    data = {"market": {"ai_news": {"items": [
        {"date": "2026-07-01"}, {"date": "2026-08-02"}, {"date": "2026-06-01"},
    ]}}}
    assert resolve("aiNewsFreshness", data)["date"] == "2026-08-02"


def test_whale_resolver_is_asset_scoped(resolve):
    """The ETH panel must not inherit BTC's dates (the bug this fixes)."""
    data = {"whale": {
        "btc": {"tx_volume_usd": [{"date": "2026-08-01"}],
                "active_addresses": [{"date": "2026-07-29"}]},
        "eth": {"coin_metrics": {"AdrActCnt": [{"date": "2026-06-15"}]}},
    }}
    assert resolve("whaleFreshness", data, args=["btc"])["date"] == "2026-07-29"
    assert resolve("whaleFreshness", data, args=["eth"])["date"] == "2026-06-15"


def test_etf_resolver_reads_the_daily_series(resolve):
    data = {"__etf": {"daily": [{"date": "2026-07-28", "flow": 10}],
                      "stats": {"last_date": "2026-07-28"}}}
    assert resolve("etfFreshness", data)["date"] == "2026-07-28"


# ---------------- 5. The max-vs-min defect on the breadth stamps -------------
# THE MOTIVATING SURFACE. computeSignalBreadth() lays its x-axis out as the
# UNION of every contributor's history dates, so breadth[last].date is the
# MAX across inputs: one coin that still updates holds the right edge at
# today while the other 49 sit frozen. The stamp has to report the MIN.
#
# The fixture below is the gate's proof shape: 49 coins frozen at 2026-06-09,
# one fresh at 2026-08-01.

FROZEN_DAY = "2026-06-09"
FRESH_DAY = "2026-08-01"


def _mixed_age_poc_top(n_frozen=49):
    """49 coins whose last history point is 2026-06-09, plus 1 at 2026-08-01."""
    rows = []
    for i in range(n_frozen):
        rows.append({
            "coin_id": f"frozen-{i}", "symbol": f"F{i}", "as_of": FROZEN_DAY,
            "signal_history": [{"date": "2026-06-08", "score": 10},
                               {"date": FROZEN_DAY, "score": 12}],
        })
    rows.append({
        "coin_id": "fresh", "symbol": "FRESH", "as_of": FRESH_DAY,
        "signal_history": [{"date": "2026-06-08", "score": 5},
                           {"date": FROZEN_DAY, "score": 6},
                           {"date": FRESH_DAY, "score": 7}],
    })
    return rows


def test_breadth_last_bucket_really_is_the_max(resolve):
    """Establishes the trap: the chart's right edge IS the newest coin."""
    items = [{"history": e["signal_history"]} for e in _mixed_age_poc_top()]
    breadth = resolve("computeSignalBreadth", {}, args=[items, 90])
    assert breadth[-1]["date"] == FRESH_DAY
    # …and only 1 of 50 coins actually reports on that day.
    assert breadth[-1]["total"] == 1


def test_poc_resolver_reports_the_min_on_the_same_fixture(resolve):
    r = resolve("pocTopFreshness", {"market": {"poc_top": _mixed_age_poc_top()}})
    assert r["date"] == FROZEN_DAY
    assert r["total"] == 50 and r["dated"] == 50


def test_stocks_resolver_reports_the_min_on_a_mixed_age_universe(resolve):
    rows = [{"symbol": f"F{i}", "as_of": FROZEN_DAY,
             "history": [{"date": FROZEN_DAY, "score": 1}]} for i in range(49)]
    rows.append({"symbol": "FRESH", "as_of": FRESH_DAY,
                 "history": [{"date": FROZEN_DAY, "score": 1},
                             {"date": FRESH_DAY, "score": 2}]})
    breadth = resolve("computeSignalBreadth", {},
                      args=[[{"history": r["history"]} for r in rows], 90])
    assert breadth[-1]["date"] == FRESH_DAY          # the trap
    r = resolve("stocksFreshness", {"market": {"stocks_signals": rows}})
    assert r["date"] == FROZEN_DAY                   # the honest answer
    assert r["total"] == 50 and r["dated"] == 50


def test_stocks_resolver_counts_undated_and_cached_rows(resolve):
    """#stocksBreadthFresh had no stale/undated disclosure at all."""
    rows = [
        {"symbol": "A", "as_of": "2026-07-30", "history": [{"date": "2026-07-30"}]},
        {"symbol": "B", "stale": True, "as_of": "2026-06-01",
         "history": [{"date": "2026-06-01"}]},
        {"symbol": "C"},                                   # undated
    ]
    r = resolve("stocksFreshness", {"market": {"stocks_signals": rows}})
    assert r["date"] == "2026-06-01"
    assert r["stale"] == 1
    assert r["dated"] == 2 and r["total"] == 3


@pytest.fixture(scope="module")
def paint(v2_js):
    """Execute a real renderer against a stub DOM and read the stamp back.

    Greps prove a call site LOOKS right; this proves what the element ends
    up saying. renderBreadthChart is stubbed out (it needs a canvas and
    Chart.js) — everything else is the shipped code.
    """
    py_mini_racer = pytest.importorskip(
        "py_mini_racer", reason="V8 needed to execute the shipped JS")
    ctx = py_mini_racer.MiniRacer()
    names = ("freshness", "freshnessDayUTC", "freshnessYmd", "paintFreshness",
             "fDay", "fLast", "fMin", "fMax", "pocTopFreshness",
             "computeSignalBreadth", "renderCryptoSignalsBreadth")
    bodies = "\n".join(extract_function(v2_js, n) for n in names)
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
    // Chart.js exploding must not stop the stamp from being painted.
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
    """THE regression test for the max-vs-min swap.

    Restoring `breadth[breadth.length-1].date` as the stamp's date makes
    this fail: the element would read 2026-08-01 while 49 of 50 coins have
    been frozen since 2026-06-09.
    """
    out = paint({"market": {"poc_top": _mixed_age_poc_top()}})
    assert FROZEN_DAY in out["text"], out
    assert FRESH_DAY not in out["text"], out
    # …and it is tinted by age, not printed bare (2026-06-09 is ancient).
    assert "v2-fresh--bad" in out["cls"], out


def test_breadth_stamp_counts_cached_coins(paint):
    rows = _mixed_age_poc_top()
    rows[0]["stale"] = True
    rows[1]["stale"] = True
    out = paint({"market": {"poc_top": rows}})
    assert "2 of 50 cached" in out["text"], out


def test_breadth_stamp_is_dashed_when_no_coin_is_datable(paint):
    """Rule 4: no honest date ⇒ 'as of —'-style unavailable, never today."""
    rows = [{"coin_id": "x", "signal_history": [{"score": 1}]}]
    out = paint({"market": {"poc_top": rows}})
    assert out["text"].endswith("—"), out
    assert "v2-fresh--none" in out["cls"], out


def test_breadth_stamp_survives_a_chart_failure(paint):
    """Chart.js is a CDN dep behind an SRI pin. When it dies the stamp lives.

    The stamp used to be painted AFTER the chart call, so a ReferenceError
    from `new Chart(...)` blanked it while the page still showed the data.
    """
    out = paint({"market": {"poc_top": _mixed_age_poc_top()}}, broken=True)
    assert FROZEN_DAY in out["text"], out


def test_stocks_breadth_stamp_is_wired_to_the_min_resolver(v2_js):
    """Source-level guard for the stocks twin of the same defect.

    (renderStocksTab is too entangled with the DOM to run in V8; the built
    page is probed for real in the Playwright pass.)
    """
    fn = extract_function(v2_js, "renderStocksTab")
    block = fn.split("stocksBreadthFresh")[1].split("renderBreadthChart")[0]
    assert "stocksFreshness()" in block
    assert "paintFreshness(sbEl, sf && sf.date" in block, \
        "the stocks breadth stamp must take the OLDEST ticker, not lastBar"
    assert "stale:" in block and "total:" in block, "rule 4 disclosure missing"
    # The chart must be drawn AFTER the stamp, so a Chart.js failure cannot
    # swallow it.
    assert fn.index("stocksBreadthFresh") < fn.index("renderBreadthChart('stocksBreadthChart'")


def test_crypto_breadth_stamp_paints_before_the_chart(v2_js):
    fn = extract_function(v2_js, "renderCryptoSignalsBreadth")
    assert fn.index("cryptoSignalsBreadthFresh") < fn.index("renderBreadthChart(")


# ------------- 6. Whale sentiment: no clock read behind the stamp ------------


def test_whale_sentiment_as_of_is_gated_on_the_provenance_fix(resolve):
    """`sentiment.as_of` used to be whale.fetched_at[:10].

    The whale subtree ships as a lazily fetched sidecar, so a CDN-cached
    payload from an older build can outlive this JS. Without `as_of_basis`
    (only the fixed fetch_market emits it) the date is refused.
    """
    poisoned = {"as_of": "2026-08-02"}                       # pre-fix shape
    fixed = {"as_of": "2026-06-09", "as_of_basis": "oldest contributing on-chain series"}
    assert resolve("whaleSentimentAsOf", {}, args=[poisoned]) is None
    assert resolve("whaleSentimentAsOf", {}, args=[fixed]) == "2026-06-09"
    assert resolve("whaleSentimentAsOf", {}, args=[None]) is None


def test_whale_resolver_is_null_when_primary_series_are_absent(resolve):
    """BLOCKER 2 in its exact shape.

    With no primary series, `sentiment.as_of` used to be the only candidate
    left — and it was the fetch clock, so the panel stamped TODAY. Now the
    resolver must report nothing at all.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    data = {"whale": {"btc": {},
                      "sentiment": {"as_of": today, "score": 5}}}   # pre-fix
    assert resolve("whaleFreshness", data, args=["btc"]) is None
    data_eth = {"whale": {"eth": {"coin_metrics": {},
                                  "sentiment": {"as_of": today}}}}
    assert resolve("whaleFreshness", data_eth, args=["eth"]) is None


def test_whale_resolver_uses_a_gated_sentiment_date(resolve):
    """A composite date from the FIXED shape is a legitimate input again."""
    data = {"whale": {
        "btc": {"tx_volume_usd": [{"date": "2026-08-01"}]},
        "sentiment": {"as_of": "2026-07-04",
                      "as_of_basis": "oldest contributing on-chain series"},
    }}
    assert resolve("whaleFreshness", data, args=["btc"])["date"] == "2026-07-04"


def test_whale_cards_render_the_gated_date(v2_js):
    """Both whale sentiment cards go through the gate + the helper."""
    for fn_name in ("renderWhaleSentiment", "renderWhaleSentimentEth"):
        fn = extract_function(v2_js, fn_name)
        assert "freshnessHtml(whaleSentimentAsOf(s)" in fn, fn_name
        assert "escapeHtml(s.as_of" not in fn, fn_name
    # The tooltip claim ("Observation date … Not the page build time") is
    # generated by a function that describes what is actually being shown.
    title = extract_function(v2_js, "whaleSentimentTitle")
    assert "as_of_basis" in title
    assert "fetch time, not a data date" in title


# ------------- 7. Signal cards: null as_of must render "as of —" -------------


def test_signal_card_as_of_gate(resolve):
    # signals_top20 row, pre-fix (no computed_at) → refused.
    assert resolve("signalCardAsOf", {},
                   args=[{"symbol": "BTC", "as_of": "2026-08-02"}]) is None
    # signals_top20 row, fixed shape, dated.
    assert resolve("signalCardAsOf", {},
                   args=[{"as_of": "2026-08-01",
                          "computed_at": "2026-08-02T21:00:00+00:00"}]) == "2026-08-01"
    # signals_top20 row, fixed shape, upstream carried no date → null, and it
    # must NOT come back as an empty string (that is the dangling "as of ").
    assert resolve("signalCardAsOf", {},
                   args=[{"as_of": None,
                          "computed_at": "2026-08-02T21:00:00+00:00"}]) is None
    # DATA.signals.btc (compute_signal): dated from the last daily bar.
    assert resolve("signalCardAsOf", {},
                   args=[{"as_of": "2026-07-31",
                          "history": [{"date": "2026-07-31", "score": 3}]}]) == "2026-07-31"


def test_signal_cards_never_emit_a_dangling_as_of(v2_js):
    """`· as of ${escapeHtml(s.as_of||'')}` rendered "as of " with a null."""
    for fn_name in ("renderSignalCard", "renderSignalCardFromObj"):
        fn = extract_function(v2_js, fn_name)
        assert "as of ${escapeHtml(s.as_of" not in fn, fn_name
        assert "freshnessHtml(signalCardAsOf(s)" in fn, fn_name


def test_signal_card_html_renders_the_dash_for_a_null_as_of(v2_js):
    """Executed, not grepped: build the card markup with as_of null."""
    py_mini_racer = pytest.importorskip("py_mini_racer")
    ctx = py_mini_racer.MiniRacer()
    names = ("freshness", "freshnessDayUTC", "freshnessYmd", "freshnessHtml",
             "escapeHtml", "signalColor", "signalCardAsOf", "signalCardAsOfTitle",
             "renderSignalCardFromObj")
    ctx.eval("function renderSignalSparkline(){ return ''; }\n"
             + "\n".join(extract_function(v2_js, n) for n in names))
    html = ctx.call("renderSignalCardFromObj", {
        "symbol": "frozen", "name": "Frozen Coin", "score": 12,
        "label": "BUY", "as_of": None, "price": 1.5,
        "computed_at": "2026-08-02T21:00:00+00:00", "components": [],
    })
    assert "as of —" in html
    assert "Frozen Coin ·" not in html
    assert "as of </div>" not in html


# --------- 8. A chart failure must not be able to blank the stamps -----------


def test_chart_js_failure_is_downgraded_to_a_no_op(v2_js):
    """Chart.js is a CDN dependency behind an SRI pin.

    When it fails to load, `new Chart(...)` throws a ReferenceError that
    aborts the whole renderer — everything after it, including that
    renderer's freshness stamp, silently never runs. Measured on the built
    page: with Chart absent and no stub, the Signals tab held 3 stamps; with
    the stub, 9. The page must degrade to "no chart", not to "no
    disclosure".
    """
    assert "if (typeof window.Chart === 'undefined')" in v2_js, \
        "the Chart.js absence guard is gone"
    # A real Chart.js must be left completely alone.
    i = v2_js.index("if (typeof window.Chart === 'undefined')")
    guard = v2_js[i:i + 1800]
    assert "window.Chart.__unavailable = true" in guard
    for method in ("destroy", "update", "resize"):
        assert re.search(r"this\.%s\s*=\s*function\s*\(\)\s*\{\}" % method,
                         guard), \
            f"the stub needs a no-op {method}() — callers invoke it"


def test_freshness_is_painted_before_the_tab_renderers(v2_js):
    """renderAll() used to paint every stamp LAST.

    Any throw in a tab renderer above therefore blanked all of them while
    the page still displayed the data. Paint first (safety net), then again
    at the end (accuracy pass after lazy sidecars land).
    """
    fn = extract_function(v2_js, "renderAll")
    first = fn.index("renderTabFreshness()")
    assert first < fn.index("renderInsights()"), \
        "freshness must be painted before any tab renderer runs"
    assert fn.count("renderTabFreshness()") >= 2
    assert fn.count("renderDataFreshness()") >= 2


def test_whale_tab_stamp_says_unavailable_rather_than_nothing(v2_js):
    """Rule 5 on the whale panel: blanking hides that the panel is undated."""
    fn = extract_function(v2_js, "renderWhaleAsOf")
    assert "paintFreshness(el, null" in fn
    assert "el.textContent = '';" not in fn
