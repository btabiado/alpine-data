"""Contract tests for the LTHCS pages' data-freshness stamps.

The LTHCS family is a dozen standalone static pages (``lthcs_tab/`` and its
siblings) that live outside both app.py builders. Before this suite existed
they each printed a bare date -- ``Aug 1, 2026`` -- with no age, no tint, and
in one case (``lthcs_health``) a live clock read glued onto the end, which
reads as "0 days old" no matter how long the cron has been dead.

Three layers, all executed rather than inspected:

1. **The shipped JavaScript.** ``lthcs_tab/lthcs-freshness.js`` is executed in
   V8 with a pinned clock, so the assertions run against the code that ships.

2. **Dialect parity.** The same corpus is pushed through ``v2/app.py``'s
   ``freshness()`` and the two outputs must match byte for byte. This is the
   guard that matters most: three frontends now stamp dates and they must not
   drift into three different dialects.

3. **Wiring.** Every LTHCS page must own a stamp element, route it through the
   shared helper, and keep clock reads out of it.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FRESH_JS = ROOT / "lthcs_tab" / "lthcs-freshness.js"
V2_APP = ROOT / "v2" / "app.py"

NOW = "2026-08-02T12:00:00Z"


# ---------------------------------------------------------------- helpers ---


def extract_function(src: str, name: str) -> str:
    """Slice one top-level ``function <name>(...) {...}`` out of a blob."""
    start = src.index(f"function {name}(")
    i = src.index("{", start)
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[start:j + 1]
    raise AssertionError(f"unbalanced braces extracting {name}")


def _ctx_with(bodies: str, entry: str):
    """Eval `bodies` in V8 behind a pinned-clock factory; return a caller."""
    py_mini_racer = pytest.importorskip(
        "py_mini_racer", reason="V8 needed to execute the shipped JS")
    ctx = py_mini_racer.MiniRacer()
    # RealDate arrives as a parameter: `const Date = D` puts the name Date in
    # TDZ for the whole factory body, so the real constructor must be captured
    # before the body starts. Same shape as tests/test_v2_freshness.py.
    ctx.eval("""
    function __make(RealDate, fixedNowMs){
      class D extends RealDate {
        constructor(...a){ if (a.length === 0) super(fixedNowMs); else super(...a); }
        static now(){ return fixedNowMs; }
      }
      const Date = D;
      %s
      return %s;
    }
    function __call(args, nowIso){
      const fn = __make(Date, Date.parse(nowIso));
      return fn.apply(null, args);
    }
    """ % (bodies, entry))

    def call(*args, now=NOW):
        return ctx.call("__call", list(args), now)

    return call


@pytest.fixture(scope="module")
def fresh_src() -> str:
    if not FRESH_JS.exists():  # pragma: no cover - repo layout guard
        pytest.skip("lthcs_tab/lthcs-freshness.js not present")
    # V8 cannot eval ES module syntax; the exports are the only module-ism.
    return FRESH_JS.read_text(encoding="utf-8").replace("export function", "function")


@pytest.fixture(scope="module")
def lthcs_freshness(fresh_src):
    bodies = "\n".join(
        extract_function(fresh_src, n)
        for n in ("freshness", "freshnessDayUTC", "freshnessYmd")
    )
    return _ctx_with(bodies, "freshness")


@pytest.fixture(scope="module")
def lthcs_composite(fresh_src):
    bodies = "\n".join(
        extract_function(fresh_src, n)
        for n in ("freshness", "freshnessDayUTC", "freshnessYmd",
                  "fDay", "fMin", "fMax", "composite")
    )
    return _ctx_with(bodies, "composite")


@pytest.fixture(scope="module")
def v2_freshness():
    if not V2_APP.exists():  # pragma: no cover - repo layout guard
        pytest.skip("v2/app.py not present")
    src = V2_APP.read_text(encoding="utf-8")
    bodies = "\n".join(
        extract_function(src, n)
        for n in ("freshness", "freshnessDayUTC", "freshnessYmd")
    )
    return _ctx_with(bodies, "freshness")


# ------------------------------------------------ 1. the honesty contract ---


@pytest.mark.parametrize("iso,expected_text,expected_tone", [
    ("2026-08-02", "as of 2026-08-02 (0d ago)", "ok"),
    ("2026-07-26", "as of 2026-07-26 (7d ago)", "ok"),     # warn boundary
    ("2026-07-25", "as of 2026-07-25 (8d ago)", "warn"),
    ("2026-07-12", "as of 2026-07-12 (21d ago)", "warn"),  # bad boundary
    ("2026-07-11", "as of 2026-07-11 (22d ago)", "bad"),
    ("2026-05-17", "as of 2026-05-17 (77d ago)", "bad"),
])
def test_thresholds_and_wording(lthcs_freshness, iso, expected_text, expected_tone):
    """Rule 5: the age is computed and tinted, in the site-wide wording."""
    out = lthcs_freshness(iso, {})
    assert out["text"] == expected_text
    assert out["tone"] == expected_tone


@pytest.mark.parametrize("junk", [
    None, "", "   ", "not-a-date", "2026-02-31", "2026-13-01", "2026-00-10",
])
def test_no_honest_date_renders_em_dash_never_a_clock(lthcs_freshness, junk):
    """Rule 4: no honest date means "as of --", never a silent clock read."""
    out = lthcs_freshness(junk, {})
    assert out["text"] == "as of —"
    assert out["tone"] == "none"
    assert out["ageDays"] is None
    # The pinned clock is 2026-08-02. It must not leak into the stamp.
    assert "2026-08-02" not in out["text"]


def test_future_date_floors_at_zero(lthcs_freshness):
    """A future-dated observation is a data bug, not negative age."""
    out = lthcs_freshness("2099-01-01", {})
    assert out["ageDays"] == 0
    assert "-1d" not in out["text"]


def test_age_is_timezone_stable(fresh_src):
    """Both operands are midnight UTC, so age is whole days everywhere.

    Computing from local midnight is what produces "-1d ago" for viewers east
    of UTC in the early hours; pin the clock either side of a UTC day boundary
    and the answer must not move.
    """
    bodies = "\n".join(
        extract_function(fresh_src, n)
        for n in ("freshness", "freshnessDayUTC", "freshnessYmd")
    )
    for now in ("2026-08-02T00:00:01Z", "2026-08-02T23:59:59Z"):
        call = _ctx_with(bodies, "freshness")
        assert call("2026-08-01", {}, now=now)["ageDays"] == 1


def test_stale_entries_are_counted_and_disclosed(lthcs_freshness):
    """Rule 3: stale/incomplete entries are counted next to the date."""
    assert lthcs_freshness("2026-08-01", {"stale": 3, "total": 10})["text"] == (
        "as of 2026-08-01 (1d ago) · 3 of 10 cached")
    # LTHCS's stale entries are dropped pillars, not cached quotes. The noun is
    # overridable; the default must stay 'cached' so the dialect is unchanged.
    assert lthcs_freshness(
        "2026-08-01", {"stale": 216, "total": 216, "staleNoun": "incomplete"},
    )["text"] == "as of 2026-08-01 (1d ago) · 216 of 216 incomplete"
    # Zero stale entries add no suffix at all.
    assert lthcs_freshness("2026-08-01", {"stale": 0, "total": 10})["text"] == (
        "as of 2026-08-01 (1d ago)")


# ------------------------------------------- 2. Rule 2: composites use fMin --


def test_composite_takes_the_oldest_input(lthcs_composite):
    """A composite is only as fresh as its OLDEST contributing input."""
    out = lthcs_composite([
        {"label": "scores", "date": "2026-08-01"},
        {"label": "macro", "date": "2026-06-20"},
        {"label": "index", "date": "2026-07-28"},
    ])
    assert out["date"] == "2026-06-20", "composite must be fMin, never fMax"
    assert out["newest"] == "2026-08-01"
    assert out["spreadDays"] == 42


def test_composite_counts_undated_weighted_inputs(lthcs_composite):
    """An input whose date we cannot read is disclosed, never dropped."""
    out = lthcs_composite([
        {"label": "scores", "date": "2026-08-01"},
        {"label": "macro", "date": None},
    ])
    assert out["missing"] == 1
    assert out["total"] == 2


def test_composite_with_no_dates_yields_none(lthcs_composite):
    out = lthcs_composite([{"label": "a", "date": None}, {"label": "b", "date": ""}])
    assert out["date"] is None


def test_zero_weight_input_is_excluded_but_still_disclosed(lthcs_composite):
    """A dropped pillar contributes 0 to the number, so it must not age the
    headline -- but its real date must still reach the reader."""
    out = lthcs_composite([
        {"label": "scores", "date": "2026-08-01"},
        {"label": "thesis", "date": "2026-05-17", "contributes": False, "severe": True},
    ])
    assert out["date"] == "2026-08-01"
    labels = {r["label"]: r for r in out["rows"]}
    assert labels["thesis"]["date"] == "2026-05-17"
    assert labels["thesis"]["severe"] is True
    assert labels["thesis"]["tag"] == "dropped"
    assert "2026-05-17" in out["detail"]


def test_restoring_a_dropped_pillar_drags_the_headline(lthcs_composite):
    """The exclusion is data-driven, not a permanent excuse: the moment the
    pillar carries weight again its ancient date wins."""
    out = lthcs_composite([
        {"label": "scores", "date": "2026-08-01"},
        {"label": "thesis", "date": "2026-05-17", "contributes": True},
    ])
    assert out["date"] == "2026-05-17"


# --------------------------------------------------- 3. dialect parity -------


def _corpus():
    from datetime import date, timedelta
    base = date(2026, 8, 2)
    days = [(base - timedelta(days=d)).isoformat() for d in range(-3, 60)]
    return days + [
        None, "", "   ", "not-a-date", "2026-02-31", "2026-13-01", "2026-00-10",
        "2026-08-01T23:59:59Z", "2026-08-01T00:00:00+09:00", "2099-01-01",
        "1999-12-31", "2026-8-1", "20260801",
    ]


OPTS = [
    {}, {"warnDays": 3, "badDays": 10}, {"label": "updated"},
    {"stale": 5}, {"stale": 5, "total": 20}, {"stale": 0, "total": 20},
]


def test_lthcs_freshness_is_byte_identical_to_v2(lthcs_freshness, v2_freshness):
    """The whole point of porting rather than reinventing.

    Any divergence here means the LTHCS pages and the V2 dashboard are telling
    the user two different stories about the same rules.
    """
    mismatches = []
    for iso in _corpus():
        for opts in OPTS:
            a = json.dumps(v2_freshness(iso, opts), sort_keys=True)
            b = json.dumps(lthcs_freshness(iso, opts), sort_keys=True)
            if a != b:
                mismatches.append((iso, opts, a, b))
    assert not mismatches, f"dialect drift vs v2/app.py: {mismatches[:5]}"


@pytest.fixture(scope="module")
def realestate_freshness():
    """The /real-estate/ page deploys standalone with no shared-module root,
    so it carries an inlined copy of the helper. Held to the same dialect."""
    page = ROOT / "real_estate" / "index.html"
    if not page.exists():  # pragma: no cover - repo layout guard
        pytest.skip("real_estate/index.html not present")
    src = page.read_text(encoding="utf-8")
    bodies = "\n".join(
        extract_function(src, n) for n in ("reFreshness", "reDayUTC", "reYmd")
    )
    return _ctx_with(bodies, "reFreshness")


def test_realestate_inline_copy_is_byte_identical_to_v2(realestate_freshness, v2_freshness):
    mismatches = []
    for iso in _corpus():
        # The inline copy carries no stale-suffix support (the page has no
        # per-entry stale flags), so only the shared options are compared.
        for opts in ({}, {"warnDays": 3, "badDays": 10}, {"label": "updated"}):
            a = json.dumps(v2_freshness(iso, opts), sort_keys=True)
            b = json.dumps(realestate_freshness(iso, opts), sort_keys=True)
            if a != b:
                mismatches.append((iso, opts, a, b))
    assert not mismatches, f"real_estate dialect drift: {mismatches[:5]}"


def test_realestate_no_longer_stamps_the_fetch_time():
    """Regression: the header rendered `generated_at` -- the moment the daily
    workflow fetched the CSVs -- as "updated <today>". It read as current
    every day, including the eight weeks Redfin published nothing new.
    """
    src = (ROOT / "real_estate" / "index.html").read_text(encoding="utf-8")
    code = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    code = re.sub(r"^\s*//.*$", "", code, flags=re.M)
    assert "formatGenerated" not in code, "the fetch-time formatter is back"
    # generated_at may still appear as hover context, but never as the stamp.
    for line in code.splitlines():
        if "getElementById('generated')" in line and "textContent" in line:
            assert "generated_at" not in line, line.strip()


# ------------------------------------------------------- 4. page wiring ------


# (page file, stamp element id, module that must import the shared helper)
PAGES = [
    ("lthcs_tab/index.html", "lthcs-last-updated", "lthcs_tab/lthcs-tab.js"),
    ("lthcs_tab/heatmap/index.html", "hm-last-updated", "lthcs_tab/heatmap/lthcs-heatmap.js"),
    ("lthcs_table/index.html", "lthcs-table-last-updated", "lthcs_table/lthcs-table.js"),
    ("lthcs_tab_v2/index.html", "lthcs-v2-generated", "lthcs_tab_v2/lthcs-v2.js"),
    ("lthcs_crypto/index.html", "lcry-generated", "lthcs_crypto/lthcs-crypto.js"),
    ("lthcs_health/index.html", "health-generated", "lthcs_health/lthcs-health.js"),
    ("lthcs_health/pipeline.html", "freshness-data-asof", "lthcs_health/lthcs-pipeline.js"),
    ("lthcs_health/quality.html", "lq-asof", "lthcs_health/lthcs-quality.js"),
    ("lthcs_backtest/index.html", "bt-generated", "lthcs_backtest/lthcs-backtest.js"),
    ("lthcs_backtest/ab.html", "ab-generated", "lthcs_backtest/lthcs-backtest-ab.js"),
    ("lthcs_leaderboards/index.html", "lb-snapshot-date", "lthcs_leaderboards/lthcs-leaderboards.js"),
    ("lthcs_position/index.html", "lpos-snapshot-date", "lthcs_position/lthcs-position.js"),
    ("lthcs_public/index.html", "lpub-snapshot-date", "lthcs_public/lthcs-public.js"),
    ("lthcs_diff/index.html", "lthcs-diff-asof", "lthcs_diff/lthcs-diff.js"),
    ("lthcs_history/index.html", "lthcs-history-asof", "lthcs_history/lthcs-history.js"),
]


@pytest.mark.parametrize("page,stamp_id,module", PAGES)
def test_every_lthcs_page_has_a_stamp_wired_to_the_shared_helper(page, stamp_id, module):
    html = (ROOT / page).read_text(encoding="utf-8")
    assert f'id="{stamp_id}"' in html, f"{page} lost its stamp element"

    js = (ROOT / module).read_text(encoding="utf-8")
    assert "lthcs-freshness.js" in js, (
        f"{module} must route its stamp through the shared helper, "
        "not through a local date formatter")
    assert stamp_id in js, f"{module} never writes #{stamp_id}"


@pytest.mark.parametrize("page,stamp_id,module", PAGES)
def test_stamp_modules_do_not_read_the_clock_into_a_stamp(page, stamp_id, module):
    """Rule 1: a stamp reports data age. ``new Date()`` may only be used to
    work out *today* so an age can be subtracted from it -- which happens
    inside lthcs-freshness.js and nowhere else.

    lthcs-pipeline.js is the one allowed exception and it is explicit: its
    clock read is rendered under a separate "Probed at (clock)" label, into a
    different element than the data stamp.
    """
    js = (ROOT / module).read_text(encoding="utf-8")
    # Strip comments so prose about clocks doesn't trip the scan.
    stripped = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    stripped = re.sub(r"^\s*//.*$", "", stripped, flags=re.M)
    for match in re.finditer(r"[^\n]*\bnew Date\(\)[^\n]*", stripped):
        line = match.group(0)
        assert stamp_id not in line, (
            f"{module} writes a clock read into #{stamp_id}: {line.strip()}")


def test_health_header_no_longer_appends_today(lthcs_freshness):
    """Regression: lthcs_health printed `${latest} - ${isoToday()}`.

    The clock half aged by zero days no matter how dead the cron was, which is
    precisely the failure the contract exists to stop.
    """
    js = (ROOT / "lthcs_health" / "lthcs-health.js").read_text(encoding="utf-8")
    # Strip comments first: the replacement code quotes the old expression in
    # a comment explaining why it is gone, and that must not count as a hit.
    code = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    code = re.sub(r"^\s*//.*$", "", code, flags=re.M)
    assert "`${latest} · ${isoToday()}`" not in code
    header = code.split("function renderHeader")[1].split("\n}")[0]
    assert "isoToday()" not in header


def test_no_lthcs_page_still_prints_a_bare_localised_date(lthcs_freshness):
    """The old stamps rendered `toLocaleDateString` output with no age.

    Those helpers are deleted; this stops one drifting back in.
    """
    offenders = []
    for _page, _stamp, module in PAGES:
        js = (ROOT / module).read_text(encoding="utf-8")
        if re.search(r"^function formatDate\(", js, flags=re.M):
            offenders.append(module)
    assert not offenders, (
        f"bare-date formatters reintroduced in: {offenders}")


def test_tint_classes_exist_in_both_stylesheets():
    """Rule 5 needs the tints to actually resolve.

    Every LTHCS page loads lthcs_tab/lthcs.css except the V2 experiment, which
    ships its own stylesheet -- so the classes must be defined in both.
    """
    for css in ("lthcs_tab/lthcs.css", "lthcs_tab_v2/lthcs-v2.css"):
        text = (ROOT / css).read_text(encoding="utf-8")
        for tone in ("ok", "warn", "bad", "none"):
            assert f".lthcs-fresh--{tone}" in text, f"{css} missing --{tone} tint"
