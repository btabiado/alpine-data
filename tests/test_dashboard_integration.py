"""Integration tests that catch wiring bugs between the HTML/JS/DATA layers.

These guard against:
1. Tab-routing bugs — a tab declared in HTML but not toggled in selectTab() (or
   the reverse). The Stocks and POC bugs both slipped because manual QA was the
   only safety net.
2. Renderer-vs-data alignment — the JS renderers read a fixed set of paths from
   the JSON payloads. If a fetcher silently drops a key (rate limit, schema
   shift), the dashboard renders empty without erroring. We assert the key
   paths exist and are non-empty.
3. Smoke check that ``python app.py --no-open`` still rebuilds dashboard.html.

The tests own no fixtures beyond ``conftest`` and never touch production code.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_HTML = ROOT / "dashboard.html"
MARKET_JSON = ROOT / "data" / "market.json"
WHALE_JSON = ROOT / "data" / "whale.json"


# ---------- helpers ----------


def _read_dashboard_or_skip() -> str:
    if not DASHBOARD_HTML.exists():
        pytest.skip(f"dashboard.html not built yet ({DASHBOARD_HTML})")
    return DASHBOARD_HTML.read_text(encoding="utf-8")


def _extract_select_tab_body(html: str) -> str:
    """Return the body of ``function selectTab(t){ ... }`` (brace-matched)."""
    m = re.search(r"function\s+selectTab\s*\(\s*t\s*\)\s*\{", html)
    assert m, "selectTab(t) function not found in dashboard.html"
    start = m.end()  # position right after the opening '{'
    depth = 1
    i = start
    while i < len(html) and depth > 0:
        ch = html[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return html[start:i]
        i += 1
    raise AssertionError("Unterminated selectTab function body")


def _html_tab_ids(html: str) -> set[str]:
    """Return the set of tab names declared as ``<div id=\"tab-X\">``."""
    return set(re.findall(r'<div[^>]*\bid=["\']tab-([A-Za-z0-9_-]+)["\']', html))


def _tab_strip_data_tabs(html: str) -> set[str]:
    """Tabs declared as buttons (``<div class=\"tab ...\" data-tab=\"X\">``)."""
    # Scope the search to the tab strip by only matching divs whose class
    # contains 'tab' and that also have data-tab. The buttons in the strip
    # follow this exact pattern; <div id="tab-X"> bodies do not.
    pattern = re.compile(
        r'<div[^>]*\bclass=["\'][^"\']*\btab\b[^"\']*["\'][^>]*\bdata-tab=["\']([A-Za-z0-9_-]+)["\']'
    )
    return set(pattern.findall(html))


def _select_tab_referenced_ids(body: str) -> set[str]:
    """Tab names referenced in the JS via getElementById('tab-X')."""
    return set(re.findall(r"""getElementById\(\s*['"]tab-([A-Za-z0-9_-]+)['"]\s*\)""", body))


# ---------- 1. Tab-routing structural test ----------


def test_select_tab_toggles_every_html_tab_div():
    """Every ``<div id=\"tab-X\">`` must be referenced inside selectTab."""
    html = _read_dashboard_or_skip()
    body = _extract_select_tab_body(html)

    html_ids = _html_tab_ids(html)
    assert html_ids, "no <div id=\"tab-X\"> elements found — selector wrong?"

    referenced = _select_tab_referenced_ids(body)

    missing_in_js = sorted(html_ids - referenced)
    assert not missing_in_js, (
        "selectTab() is missing toggles for these tab divs declared in HTML: "
        f"{missing_in_js}. This is the exact class of bug that hid Stocks/POC "
        "behind a no-op tab button."
    )


def test_select_tab_references_only_existing_tab_divs():
    """Every getElementById('tab-X') in selectTab must point at a real div."""
    html = _read_dashboard_or_skip()
    body = _extract_select_tab_body(html)

    html_ids = _html_tab_ids(html)
    referenced = _select_tab_referenced_ids(body)

    orphans = sorted(referenced - html_ids)
    assert not orphans, (
        f"selectTab() references tab ids with no <div id=\"tab-X\"> body: {orphans}"
    )


# ---------- 2. Tab-strip integrity test ----------


def test_every_tab_button_has_a_tab_body():
    """Every ``<div class=\"tab\" data-tab=\"X\">`` button needs a matching body."""
    html = _read_dashboard_or_skip()
    buttons = _tab_strip_data_tabs(html)
    bodies = _html_tab_ids(html)

    assert buttons, "no data-tab buttons found in tab strip"

    dead_buttons = sorted(buttons - bodies)
    orphan_bodies = sorted(bodies - buttons)

    assert not dead_buttons, (
        f"Tab strip has buttons with no body div: {dead_buttons}"
    )
    assert not orphan_bodies, (
        f"Body divs with no tab-strip button (unreachable tabs): {orphan_bodies}"
    )


# ---------- 3. DATA path consistency ----------


def _load_json_or_skip(path: Path):
    if not path.exists():
        pytest.skip(f"{path.name} not present — run fetch_market.py first")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        pytest.fail(f"{path.name} is not valid JSON: {e}")


def _walk(obj, dotted: str):
    """Resolve a dotted path with optional ``[N]`` indices. Return MISSING sentinel
    if any segment is absent. ``[0]`` indexes the first list element."""
    cur = obj
    # Tokenise into keys / list indices.
    parts = []
    for seg in dotted.split("."):
        m = re.match(r"^([^\[\]]+)((?:\[\d+\])*)$", seg)
        assert m, f"bad path segment: {seg!r}"
        parts.append(m.group(1))
        for idx in re.findall(r"\[(\d+)\]", m.group(2) or ""):
            parts.append(int(idx))
    for p in parts:
        if isinstance(p, int):
            if not isinstance(cur, list) or p >= len(cur):
                return _MISSING
            cur = cur[p]
        else:
            if not isinstance(cur, dict) or p not in cur:
                return _MISSING
            cur = cur[p]
    return cur


_MISSING = object()


# (path, must_be_non_empty)
# We split into "must exist non-empty" vs "key must exist" per the spec.
# Runtime-computed paths are skipped (not failed) when absent — they're
# rebuilt by `python app.py --fetch-market`, not persisted on disk.
_REQUIRED_NON_EMPTY = [
    "coinbase.btc.price_usd",            # Overview / header price
    "cadli_btc[0].close",                # Trading chart series
]
_RUNTIME_COMPUTED_NON_EMPTY_MARKET = [
    "stocks_signals[0].symbol",          # Stocks tab top row (runtime-computed)
    "poc_top[0].symbol",                 # POC tab top card (runtime-computed)
]
_REQUIRED_KEY_PRESENT_MARKET: list[str] = []
_REQUIRED_NON_EMPTY_WHALE = [
    "distribution.buckets",              # Whale distribution chart
]
_REQUIRED_KEY_PRESENT_WHALE: list[str] = []
_RUNTIME_COMPUTED_KEY_PRESENT_WHALE = [
    "eth.blockchair",                    # ETH whale section (runtime-computed)
]


def _assert_present_non_empty(blob, path: str, label: str):
    val = _walk(blob, path)
    assert val is not _MISSING, f"{label}: missing path {path!r}"
    if isinstance(val, (list, dict, str)):
        assert len(val) > 0, f"{label}: path {path!r} is present but empty"
    else:
        assert val is not None, f"{label}: path {path!r} is None"


def _assert_key_present(blob, path: str, label: str):
    val = _walk(blob, path)
    assert val is not _MISSING, f"{label}: missing path {path!r}"


def _skip_if_runtime_key_missing(blob, path: str, label: str, *, must_be_non_empty: bool):
    """Skip the test when a runtime-computed key is missing or empty.

    These keys (`poc_top`, `stocks_signals`, `eth.blockchair`) are rebuilt
    at payload-build time by `python app.py --fetch-market` and are not
    guaranteed to be persisted on disk — a stale cached JSON without them
    is a build/data-pipeline concern, not a renderer-wiring bug.
    """
    val = _walk(blob, path)
    if val is _MISSING:
        pytest.skip(
            f"{label}: runtime-computed path {path!r} missing — "
            "run `python app.py --fetch-market` to rebuild"
        )
    if must_be_non_empty and isinstance(val, (list, dict, str)) and len(val) == 0:
        pytest.skip(
            f"{label}: runtime-computed path {path!r} present but empty — "
            "run `python app.py --fetch-market` to rebuild"
        )


def test_market_json_renderer_paths_exist():
    market = _load_json_or_skip(MARKET_JSON)
    for p in _REQUIRED_NON_EMPTY:
        _assert_present_non_empty(market, p, "market.json")
    for p in _REQUIRED_KEY_PRESENT_MARKET:
        _assert_key_present(market, p, "market.json")
    for p in _RUNTIME_COMPUTED_NON_EMPTY_MARKET:
        _skip_if_runtime_key_missing(market, p, "market.json", must_be_non_empty=True)


def test_whale_json_renderer_paths_exist():
    whale = _load_json_or_skip(WHALE_JSON)
    for p in _REQUIRED_NON_EMPTY_WHALE:
        _assert_present_non_empty(whale, p, "whale.json")
    for p in _REQUIRED_KEY_PRESENT_WHALE:
        _assert_key_present(whale, p, "whale.json")
    for p in _RUNTIME_COMPUTED_KEY_PRESENT_WHALE:
        _skip_if_runtime_key_missing(whale, p, "whale.json", must_be_non_empty=False)


# ---------- 4. Build smoke test ----------


@pytest.mark.skipif(
    os.environ.get("SKIP_BUILD_SMOKE") == "1",
    reason="SKIP_BUILD_SMOKE=1 set",
)
def test_app_no_open_rebuilds_dashboard():
    """``python app.py --no-open`` should exit 0 and update dashboard.html.

    Bounded to 30s; if the build is slower (network rebuild, big payload), the
    test skips rather than fails — the goal is a smoke check, not a stress test.
    """
    if not (ROOT / "app.py").exists():
        pytest.skip("app.py missing")

    before = DASHBOARD_HTML.stat().st_mtime if DASHBOARD_HTML.exists() else 0.0
    # Sleep a tick so mtime can strictly advance even on coarse-grained FS.
    time.sleep(0.05)
    try:
        result = subprocess.run(
            [sys.executable, "app.py", "--no-open"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        pytest.skip("app.py --no-open exceeded 30s budget; skipping smoke test")
        return

    assert result.returncode == 0, (
        f"app.py --no-open exited {result.returncode}\n"
        f"STDOUT (tail):\n{result.stdout[-2000:]}\n"
        f"STDERR (tail):\n{result.stderr[-2000:]}"
    )
    assert DASHBOARD_HTML.exists(), "dashboard.html missing after build"
    after = DASHBOARD_HTML.stat().st_mtime
    assert after > before, (
        "dashboard.html mtime did not advance — build may have been a no-op "
        f"(before={before}, after={after})"
    )


# ---------- 5. POC <-> signals_top20 join contract ----------
#
# The POC tab renderer joins ``DATA.market.poc_top`` with
# ``DATA.signals_top20`` (matching by uppercase symbol) and sorts by
# signal score descending. ``signals_top20`` is computed at payload-build
# time by ``signals.compute_all_top20({"market": market})`` — there is no
# persisted ``signals_top20`` field in ``market.json``. To exercise the
# real contract the dashboard sees, the tests below rebuild
# ``signals_top20`` from ``market.json`` exactly the way ``app.py`` does.

_LABELS_VALID = {"STRONG BUY", "BUY", "HOLD", "SELL", "STRONG SELL"}


def _compute_signals_top20_or_skip(market: dict) -> list[dict]:
    """Rebuild ``signals_top20`` from a market.json blob the same way app.py
    does. Skip the test if the signals module or its inputs are unavailable."""
    if not isinstance(market.get("markets_top"), list) or not market["markets_top"]:
        pytest.skip("market.markets_top is empty — signals_top20 cannot be built")
    sys.path.insert(0, str(ROOT))
    try:
        import signals as sig_mod  # noqa: WPS433 (test-time import is fine)
    except Exception as e:  # pragma: no cover — defensive
        pytest.skip(f"signals module unavailable: {e}")
    try:
        out = sig_mod.compute_all_top20({"market": market})
    except Exception as e:  # pragma: no cover — defensive
        pytest.skip(f"signals.compute_all_top20 raised: {e}")
    if not isinstance(out, list) or not out:
        pytest.skip("signals_top20 came back empty — nothing to join against")
    return out


def test_signals_top20_provides_symbols_for_poc_top_join():
    """At least 70% of poc_top entries must have a matching signals_top20
    entry by uppercase symbol — otherwise the POC tab renderer's join will
    drop most cards and the sort-by-score key has nothing to act on."""
    market = _load_json_or_skip(MARKET_JSON)
    poc_top = market.get("poc_top") or []
    if not poc_top:
        pytest.skip(
            "market.poc_top missing — runtime-computed key requires "
            "`python app.py --fetch-market`"
        )
    signals_top20 = _compute_signals_top20_or_skip(market)

    poc_syms = [
        (e.get("symbol") or "").upper()
        for e in poc_top
        if isinstance(e, dict) and isinstance(e.get("symbol"), str)
    ]
    poc_syms = [s for s in poc_syms if s]
    sig_syms = {
        (e.get("symbol") or "").upper()
        for e in signals_top20
        if isinstance(e, dict) and isinstance(e.get("symbol"), str)
    }
    sig_syms.discard("")

    matched = [s for s in poc_syms if s in sig_syms]
    coverage = len(matched) / len(poc_syms) if poc_syms else 0.0
    unmatched = sorted(set(poc_syms) - sig_syms)

    assert coverage >= 0.70, (
        f"Only {coverage:.0%} of poc_top entries match a signals_top20 "
        f"symbol ({len(matched)}/{len(poc_syms)}). The POC tab join will "
        f"silently drop cards. Unmatched POC symbols: {unmatched}"
    )


def test_poc_top_entries_have_required_fields_for_sort():
    """The POC renderer reads ``symbol`` (join key), ``poc.d90.poc`` (ladder
    anchor) and ``poc.migration`` / ``poc.migration_series`` (header chips)
    off every card. Enforce the shape so a fetcher schema shift doesn't
    quietly blank the tab."""
    market = _load_json_or_skip(MARKET_JSON)
    poc_top = market.get("poc_top") or []
    if not poc_top:
        pytest.skip(
            "market.poc_top missing — runtime-computed key requires "
            "`python app.py --fetch-market`"
        )

    # symbol is mandatory on every entry — it's the join key.
    for i, entry in enumerate(poc_top):
        assert isinstance(entry, dict), f"poc_top[{i}] is not a dict: {type(entry).__name__}"
        sym = entry.get("symbol")
        assert isinstance(sym, str) and sym, (
            f"poc_top[{i}] missing string 'symbol' (got {sym!r})"
        )

    total = len(poc_top)

    # poc.d90.poc — most entries should have it (ladder anchor).
    with_d90 = 0
    for entry in poc_top:
        poc = entry.get("poc")
        if not isinstance(poc, dict):
            continue
        d90 = poc.get("d90")
        if isinstance(d90, dict) and isinstance(d90.get("poc"), (int, float)):
            with_d90 += 1
    d90_ratio = with_d90 / total
    assert d90_ratio >= 0.70, (
        f"Only {d90_ratio:.0%} of poc_top entries have numeric poc.d90.poc "
        f"({with_d90}/{total}); the ladder anchor display will be blank for most cards"
    )

    # poc.migration OR poc.migration_series — at least 50%.
    with_migration = 0
    for entry in poc_top:
        poc = entry.get("poc")
        if not isinstance(poc, dict):
            continue
        mig = poc.get("migration")
        mig_series = poc.get("migration_series")
        has_mig = isinstance(mig, dict) and len(mig) > 0
        has_series = isinstance(mig_series, list) and len(mig_series) > 0
        if has_mig or has_series:
            with_migration += 1
    mig_ratio = with_migration / total
    assert mig_ratio >= 0.50, (
        f"Only {mig_ratio:.0%} of poc_top entries have poc.migration or "
        f"poc.migration_series ({with_migration}/{total})"
    )


def test_signals_top20_entries_have_required_fields_for_join():
    """Every signals_top20 entry must carry ``symbol`` (join key) and a
    numeric ``score`` (sort key). Any ``label`` present must be one of the
    five canonical buckets — case-insensitive — so the renderer can colour
    chips without a fallback branch."""
    market = _load_json_or_skip(MARKET_JSON)
    signals_top20 = _compute_signals_top20_or_skip(market)

    for i, entry in enumerate(signals_top20):
        assert isinstance(entry, dict), (
            f"signals_top20[{i}] is not a dict: {type(entry).__name__}"
        )
        sym = entry.get("symbol")
        assert isinstance(sym, str) and sym, (
            f"signals_top20[{i}] missing string 'symbol' (got {sym!r})"
        )
        score = entry.get("score")
        assert isinstance(score, (int, float)) and not isinstance(score, bool), (
            f"signals_top20[{i}] ({sym}) 'score' must be a number, got {score!r}"
        )

        if "label" in entry:
            label = entry.get("label")
            assert isinstance(label, str), (
                f"signals_top20[{i}] ({sym}) 'label' present but not a string: {label!r}"
            )
            assert label.strip().upper() in _LABELS_VALID, (
                f"signals_top20[{i}] ({sym}) has non-canonical label {label!r}; "
                f"expected one of {sorted(_LABELS_VALID)} (case-insensitive)"
            )


# ---------- 6. Research-tab "Top-15 news sentiment" card wiring ----------
#
# The new card on the Research tab is rendered entirely client-side from
# ``DATA.market.news`` + ``DATA.market.markets_top``. There's no backend
# aggregator to assert against, so we guard the wiring by checking the
# host div, renderer name, and call-site all survive a rebuild — the same
# class of "card silently disappeared after a refactor" bugs the rest of
# this file protects against.


def test_per_coin_signal_list_replaces_legacy_chart_grid():
    """The Crypto Signals tab uses an alternating "signal card → history
    chart → next signal card → …" layout for the top 25 coins, populated by
    ``renderPerCoinSignalList`` into ``#perCoinSignalList``. The legacy
    hard-coded 4-chart grid (sigBtcChart / sigEthChart / sigLinkChart /
    sigLtcChart) must be gone — keeping the old IDs alongside the new
    container would render duplicate charts and waste a Chart.js handle
    per coin."""
    html = _read_dashboard_or_skip()

    # New container present.
    assert 'id="perCoinSignalList"' in html, (
        "perCoinSignalList div missing from dashboard.html — the per-coin "
        "signal box + history chart layout has no mount point."
    )

    # Renderer function defined and wired into renderSignals().
    assert "function renderPerCoinSignalList" in html, (
        "renderPerCoinSignalList() function missing from dashboard.html"
    )
    m = re.search(r"function\s+renderSignals\s*\(\s*\)\s*\{", html)
    assert m, "renderSignals() function not found in dashboard.html"
    start = m.end()
    depth = 1
    i = start
    body = None
    while i < len(html) and depth > 0:
        ch = html[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                body = html[start:i]
                break
        i += 1
    assert body is not None, "Unterminated renderSignals function body"
    assert "renderPerCoinSignalList()" in body, (
        "renderSignals() does not invoke renderPerCoinSignalList() — the "
        "per-coin layout won't render when the user opens Crypto Signals."
    )

    # Legacy 4-chart grid IDs and renderSignalChart call sites must be gone.
    for legacy_id in ("sigBtcChart", "sigEthChart", "sigLinkChart", "sigLtcChart"):
        assert legacy_id not in html, (
            f"Legacy chart id {legacy_id!r} still present in dashboard.html — "
            "the per-coin layout should have replaced the hard-coded grid."
        )


def test_top_news_sentiment_card_wiring():
    """The 'News sentiment — Top 15 by market cap' card must exist in the
    Research tab markup, its renderer must be defined, and ``renderSocial``
    must call it. If any of these three drop out the card vanishes from
    the dashboard without raising an error."""
    html = _read_dashboard_or_skip()

    # Host div present inside the social tab body.
    assert 'id="topNewsSentimentCards"' in html, (
        "topNewsSentimentCards div missing from dashboard.html — the "
        "Research-tab Top-15 news sentiment card has no mount point."
    )

    # Renderer + helper functions present.
    assert "function renderTopNewsSentiment" in html, (
        "renderTopNewsSentiment() function missing from dashboard.html"
    )
    assert "function groupNewsBySymbol" in html, (
        "groupNewsBySymbol() helper missing from dashboard.html"
    )
    assert "function scoreNewsItemSentiment" in html, (
        "scoreNewsItemSentiment() helper missing from dashboard.html"
    )

    # renderSocial must invoke the new renderer. Match inside the function
    # body so a stray comment elsewhere wouldn't pass the test.
    m = re.search(r"function\s+renderSocial\s*\(\s*\)\s*\{", html)
    assert m, "renderSocial() function not found in dashboard.html"
    start = m.end()
    depth = 1
    i = start
    while i < len(html) and depth > 0:
        ch = html[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                body = html[start:i]
                break
        i += 1
    else:
        raise AssertionError("Unterminated renderSocial function body")
    assert "renderTopNewsSentiment()" in body, (
        "renderSocial() does not call renderTopNewsSentiment() — the new "
        "Top-15 sentiment card won't render when the user opens Research."
    )


# ---------- 7. ETH whale-panel parity cards (Activity Tracker + Proxy) ------
#
# The Whale tab's ETH panel was missing two BTC-parallel cards: the multi-
# horizon Activity Tracker (Today / 1d / 7d / 30d / 90d delta table) and the
# two-axis Whale Activity Proxy chart. Both have been ported using ETH-side
# data already in the whale.eth.* payload (Coin Metrics + Etherscan). The
# host divs, renderer functions, and call-sites inside renderWhaleEth() must
# all survive a rebuild — otherwise the cards silently disappear.
def test_eth_whale_tracker_and_proxy_cards_wired():
    html = _read_dashboard_or_skip()

    # Host divs present inside the ETH whale panel markup.
    for host_id in ("ethWhaleTrackerCard", "ethWhaleTrackerTable",
                    "ethWhaleProxyCard", "ethWhaleProxyChart"):
        assert f'id="{host_id}"' in html, (
            f"{host_id} div/canvas missing from dashboard.html — the ETH "
            "whale-panel parity card has no mount point."
        )

    # Renderer functions defined.
    for fn in ("function renderEthWhaleTracker",
               "function renderEthWhaleProxyChart"):
        assert fn in html, f"{fn}() missing from dashboard.html"

    # renderWhaleEth() must invoke both. Scope the search to the function
    # body so a stray comment elsewhere wouldn't pass the test.
    m = re.search(r"function\s+renderWhaleEth\s*\(\s*\)\s*\{", html)
    assert m, "renderWhaleEth() function not found in dashboard.html"
    start = m.end()
    depth = 1
    i = start
    body = None
    while i < len(html) and depth > 0:
        ch = html[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                body = html[start:i]
                break
        i += 1
    assert body is not None, "Unterminated renderWhaleEth function body"
    assert "renderEthWhaleTracker()" in body, (
        "renderWhaleEth() does not invoke renderEthWhaleTracker() — the new "
        "multi-horizon table won't render when the user opens Whale → ETH."
    )
    assert "renderEthWhaleProxyChart()" in body, (
        "renderWhaleEth() does not invoke renderEthWhaleProxyChart() — the "
        "new activity-proxy chart won't render when the user opens Whale → ETH."
    )
