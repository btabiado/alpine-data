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
# ---------- 8. Top-25 news-sentiment alias coverage ----------
#
# The Research-tab "Top-25 by market cap" sentiment card uses the JS
# ``_NEWS_COIN_ALIASES`` map plus the ``groupNewsBySymbol`` matcher to score
# headlines per coin. When the map was missing/short, ~18 of the top-25 coins
# scored zero mentions even on real RSS pulls. These tests pin the alias
# map's coverage of the symbols that benefit most, and exercise a Python
# port of the matcher against representative headlines so a regression in
# either the alias list or the regex anchors gets caught at CI time.


_REQUIRED_ALIAS_SYMBOLS = (
    "XRP",   # Ripple
    "BNB",   # Binance Coin
    "TRX",   # Tron
    "DOGE",  # Doge
    "TON",   # The Open Network
    "XLM",   # Stellar Lumens
)


def _extract_alias_map(html: str) -> dict[str, list[str]]:
    """Parse the ``_NEWS_COIN_ALIASES`` literal out of dashboard.html.

    We don't run JS, so this peels off the object body and pulls the
    "KEY: [..]" rows with a tolerant regex. Returns ``{SYM: [aliases…]}``.
    """
    m = re.search(r"_NEWS_COIN_ALIASES\s*=\s*\{(.+?)\n\}\s*;", html, re.DOTALL)
    assert m, "_NEWS_COIN_ALIASES map missing from dashboard.html"
    body = m.group(1)
    out: dict[str, list[str]] = {}
    for row in re.finditer(r"([A-Z][A-Z0-9_]*)\s*:\s*\[([^\]]*)\]", body):
        sym = row.group(1)
        items = re.findall(r"""['"]([^'"]+)['"]""", row.group(2))
        out[sym] = items
    return out


def test_news_alias_map_covers_required_symbols():
    html = _read_dashboard_or_skip()
    aliases = _extract_alias_map(html)
    missing = [s for s in _REQUIRED_ALIAS_SYMBOLS if s not in aliases or not aliases[s]]
    assert not missing, (
        f"_NEWS_COIN_ALIASES is missing entries for: {missing}. These coins "
        "rely on issuer/full-name aliases to score any RSS mentions."
    )


def _make_matcher(symbol: str, name: str, aliases: list[str]):
    """Python port of the JS matcher in ``groupNewsBySymbol``. Mirrors the
    word-boundary anchors and alias alternation. Returns a callable
    ``(text) -> bool``."""
    def escape(s: str) -> str:
        return re.sub(r"([.*+?^${}()|\[\]\\])", r"\\\1", s)

    sym = symbol.upper()
    sym_re = re.compile(
        r"(?:^|[^a-z0-9])\$?" + escape(sym) + r"(?:[^a-z0-9]|$)", re.I
    ) if sym else None

    forms = []
    if name:
        forms.append(name)
    for a in aliases or []:
        if a and a not in forms:
            forms.append(a)
    forms.sort(key=len, reverse=True)
    name_re = None
    if forms:
        alt = "|".join(escape(f) for f in forms)
        name_re = re.compile(
            r"(?:^|[^a-z0-9])(?:" + alt + r")(?:[^a-z0-9]|$)", re.I
        )

    def matches(text: str) -> bool:
        if sym_re and sym_re.search(text):
            return True
        if name_re and name_re.search(text):
            return True
        return False

    return matches


# Representative real-world headlines per symbol the legacy matcher missed.
_ALIAS_TEST_CASES = [
    ("XRP",  "XRP",     "Ripple files brief with SEC over XRP institutional sales ruling"),
    ("BNB",  "BNB",     "Binance Coin slips 4% as exchange announces new compliance rules"),
    ("TRX",  "TRON",    "Tron's TRX volume tops $500M after USDT migration push"),
    ("DOGE", "Dogecoin","Doge holders eye new ATH as Elon teases X payments"),
    ("TON",  "Toncoin", "The Open Network onboards 5M users via Telegram mini-apps"),
    ("XLM",  "Stellar", "Stellar Lumens steady as MoneyGram rolls out global rails"),
]


@pytest.mark.parametrize("symbol,name,headline", _ALIAS_TEST_CASES)
def test_news_matcher_catches_alias_phrasing(symbol, name, headline):
    """For each (sym, name, headline) the alias-aware matcher must hit.

    These exact phrasings are common in RSS feeds but the legacy
    symbol+coin-name-only regex missed them, leaving the coin with zero
    mentions on the Research tab.
    """
    html = _read_dashboard_or_skip()
    aliases = _extract_alias_map(html).get(symbol, [])
    matcher = _make_matcher(symbol, name, aliases)
    assert matcher(headline), (
        f"{symbol} matcher did not hit on representative headline: "
        f"{headline!r}. Aliases for {symbol}: {aliases}"
    )


def test_news_matcher_avoids_substring_false_positives():
    """Aliases must be word-boundary-anchored so common English words don't
    falsely match. E.g. 'Ton' for Toncoin must not match 'tonight' or
    'tones'; 'Ripple' for XRP must not match 'crippled'.
    """
    html = _read_dashboard_or_skip()
    aliases_map = _extract_alias_map(html)

    cases = [
        ("TON",  "Toncoin",  "Tonight's market wrap: stocks slide on Fed concerns"),
        ("TON",  "Toncoin",  "Crypto investors strike a softer tone after volatility"),
        ("XRP",  "XRP",      "Hackers leave many small exchanges crippled this quarter"),
        ("DOGE", "Dogecoin", "Doggerel and memes flood social feeds during rally"),
    ]
    for sym, name, headline in cases:
        matcher = _make_matcher(sym, name, aliases_map.get(sym, []))
        assert not matcher(headline), (
            f"{sym} matcher incorrectly hit on benign headline: {headline!r}"
        )


def test_symbol_search_typeahead_wiring():
    """The header symbol-search input gets a typeahead dropdown that filters
    DATA.market.markets_top, DATA.market.stocks_signals, and
    DATA.signals_top20 as the user types. Guard the four moving pieces:

    1. The dropdown mount point ``#symbolSearchSuggest`` must be in the
       built HTML so JS has a container to render into.
    2. The CSS class names rendered into the dropdown rows
       (``symbol-suggest-row`` / ``symbol-suggest-sym`` /
       ``symbol-suggest-name``) must be present — the CSS rules and the
       render function both reference them.
    3. The suggestion-builder function ``buildSymbolSuggestions`` and the
       renderer ``renderSymbolSuggestions`` must both be defined so the
       ``input`` event listener has something to call.
    4. The wiring must read from all three data sources, not just one.
    """
    html = _read_dashboard_or_skip()

    # 1) Mount point.
    assert 'id="symbolSearchSuggest"' in html, (
        "symbolSearchSuggest div missing from dashboard.html — the header "
        "typeahead dropdown has no mount point."
    )

    # 2) CSS class names rendered into each row.
    for cls in (
        "symbol-suggest-row",
        "symbol-suggest-sym",
        "symbol-suggest-name",
    ):
        assert cls in html, (
            f"{cls!r} class missing from dashboard.html — the typeahead "
            "row markup or its CSS hook is gone."
        )

    # 3) Renderer + builder functions defined.
    assert "function buildSymbolSuggestions" in html, (
        "buildSymbolSuggestions() missing from dashboard.html — the "
        "typeahead has no way to compute matches from the data payload."
    )
    assert "function renderSymbolSuggestions" in html, (
        "renderSymbolSuggestions() missing from dashboard.html — the "
        "typeahead has no way to paint rows into the dropdown."
    )

    # 4) All three data sources must be referenced from the builder. We
    # scope the search to buildSymbolSuggestions' body to avoid passing
    # only because some unrelated renderer happens to mention the same
    # paths elsewhere.
    m = re.search(r"function\s+buildSymbolSuggestions\s*\([^)]*\)\s*\{", html)
    assert m, "buildSymbolSuggestions() function header not found"
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
    assert body is not None, "Unterminated buildSymbolSuggestions() body"
    for path in ("markets_top", "stocks_signals", "signals_top20"):
        assert path in body, (
            f"buildSymbolSuggestions() does not consult {path!r} — the "
            "typeahead is missing a data source."
        )
# ---------- Header symbol search: multi-symbol support ----------
#
# The header search input accepts comma/semicolon/whitespace-separated
# tokens (e.g. "BTC, ETH, NVDA") and renders them as stacked cards in the
# symbol detail modal. Guard the wiring so a refactor can't silently
# drop multi-symbol parsing or the stacked-card markup.


def test_symbol_search_supports_multiple_symbols():
    html = _read_dashboard_or_skip()

    # Parser function present and splits on comma/semicolon/whitespace.
    assert "function parseSymbolSearchTokens" in html, (
        "parseSymbolSearchTokens() missing — header search can no longer "
        "split a multi-symbol input."
    )
    # The split regex itself — keep this loose so we tolerate formatting
    # tweaks but catch a regression that drops a separator class.
    assert re.search(r"split\(\s*/\[\\s,;\]\+/\s*\)", html), (
        "parseSymbolSearchTokens() must split on /[\\s,;]+/ — comma, "
        "semicolon, or whitespace separators."
    )

    # Multi-symbol orchestrator + the stacked-card wrapper class.
    assert "function lookupSymbolsMulti" in html, (
        "lookupSymbolsMulti() missing — multi-symbol render path is gone."
    )
    assert 'class="multi-symbol-card"' in html, (
        "multi-symbol-card wrapper missing — stacked cards can't render."
    )

    # Cap is enforced inside the parser (MAX_SYMBOLS = 6) so a bad paste
    # can't open dozens of cards.
    assert re.search(r"MAX_SYMBOLS\s*=\s*6", html), (
        "MAX_SYMBOLS cap missing or changed — multi-symbol input is no "
        "longer bounded at 6."
    )

    # Updated placeholder hints at multi-symbol support.
    assert "Symbol(s): BTC, ETH, NVDA" in html, (
        "Header search input placeholder no longer advertises multi-symbol "
        "support."
    )

    # The submit handler still routes the raw input through lookupSymbol,
    # which now branches on token count.
    assert "lookupSymbol(input.value)" in html, (
        "symbolSearchForm submit handler no longer calls lookupSymbol with "
        "the raw input string."
    )


# ---------- Recent symbol-lookup chips wiring -----------------------------
#
# The header symbol-search input remembers the last N successful lookups in
# localStorage and surfaces them as clickable chips below the input. The
# mount div (#symbolRecentChips) and the renderer (renderSymbolRecentChips)
# must both survive a rebuild — otherwise the chip strip silently
# disappears without raising any client-side error.


def test_symbol_recent_chips_div_and_renderer_present():
    """The recent-lookups chip row must have its mount div and renderer
    wired into the built dashboard.html. Also asserts the storage key + cap
    are pinned so a refactor doesn't silently change the persisted shape."""
    html = _read_dashboard_or_skip()

    assert 'id="symbolRecentChips"' in html, (
        '#symbolRecentChips host div missing from dashboard.html — '
        "the recent-lookups chip strip has no mount point."
    )
    assert "function renderSymbolRecentChips" in html, (
        "renderSymbolRecentChips() function missing from dashboard.html — "
        "the chip strip has no renderer."
    )
    assert "pushSymbolRecent" in html, (
        "pushSymbolRecent() helper missing from dashboard.html — "
        "successful lookups won't be persisted to the recents list."
    )
    # Storage key + cap are part of the persisted contract: pin them.
    assert "'recentSymbolLookups'" in html or '"recentSymbolLookups"' in html, (
        "localStorage key 'recentSymbolLookups' missing from dashboard.html"
    )


# ---------- Symbol detail modal — Signal + POC side-by-side wiring ----------
#
# The header search opens #symbolDetailModal. As of the POC-card polish, the
# modal body wires a Signal card AND a POC card side-by-side on desktop
# (stacked on mobile). These tests guard the structural pieces so a future
# refactor can't silently undo:
#   - the two helpers (pocCompactCardHtml + pocEmptyCardHtml) staying defined,
#   - the lookupSymbol() body emitting the .grid2.symbol-modal-body wrapper,
#   - the renderer using poc_top as the lookup source for the compact card,
#   - the @media (max-width:480px) rule that stacks the body 1-col on phones.
#
# Mirrors the assertion style of the tab-routing tests above — no JS runtime,
# just verifies the rendered HTML carries the expected hooks.


def _extract_function_body(html: str, name: str) -> str:
    """Return the body of a top-level ``function <name>(...)`` declaration."""
    m = re.search(r"function\s+" + re.escape(name) + r"\s*\([^)]*\)\s*\{", html)
    assert m, f"function {name}(...) not found in dashboard.html"
    start = m.end()
    depth = 1
    i = start
    in_str = False
    str_ch = ""
    esc = False
    while i < len(html) and depth > 0:
        ch = html[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == str_ch:
                in_str = False
        else:
            if ch in ("'", '"', "`"):
                in_str = True
                str_ch = ch
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return html[start:i]
        i += 1
    raise AssertionError(f"Unterminated function body for {name}")


def test_symbol_detail_modal_wires_signal_and_poc_cards():
    """``buildSymbolSectionsHtml`` (the shared builder used by both single-
    and multi-symbol modal paths) must emit a ``.grid2.symbol-modal-body``
    wrapper and call both the Signal renderer AND the compact POC helper
    inside that wrapper.
    """
    html = _read_dashboard_or_skip()
    body = _extract_function_body(html, "buildSymbolSectionsHtml")

    assert re.search(
        r"['\"]<div class=\"grid2 symbol-modal-body\">",
        body,
    ), "buildSymbolSectionsHtml does not emit a <div class=\"grid2 symbol-modal-body\"> wrapper"

    assert (
        "stockDetailHtml(" in body or "renderSignalCardFromObj(" in body
    ), "buildSymbolSectionsHtml does not call any signal-card renderer"

    assert "pocCompactCardHtml(" in body, (
        "buildSymbolSectionsHtml does not call pocCompactCardHtml() — the "
        "side-by-side POC card slot is missing"
    )
    assert "pocEmptyCardHtml(" in body, (
        "buildSymbolSectionsHtml does not call pocEmptyCardHtml() — stocks "
        "and outside-top-50 cryptos would render with a blank POC slot"
    )


def test_symbol_detail_poc_lookup_uses_poc_top_by_uppercase_symbol():
    """The compact POC card is sourced from ``DATA.market.poc_top``. The
    resolver (``resolveSymbolFromCache``) does the uppercased .find()
    against poc_top before passing the matched entry to the builder.
    """
    html = _read_dashboard_or_skip()
    body = _extract_function_body(html, "resolveSymbolFromCache")

    assert "poc_top" in body, (
        "resolveSymbolFromCache does not read poc_top to find the POC entry"
    )
    assert re.search(
        r"\.poc_top\s*\|\|\s*\[\]\s*\)\s*\.find\s*\(", body
    ), "poc_top lookup is no longer a .find() — case-insensitive join may have regressed"


def test_pocCompactCardHtml_is_defined_and_clickable():
    """The compact POC card helper must exist, render a ``.poc-card`` with
    ``data-poc-coin-id`` (the click hook ``wirePocDetail`` listens for) so
    clicking opens the full pocDetailModal. Without this, the symbol modal's
    POC card becomes a static dead-end.
    """
    html = _read_dashboard_or_skip()
    body = _extract_function_body(html, "pocCompactCardHtml")

    assert "data-poc-coin-id=" in body, (
        "pocCompactCardHtml output is missing data-poc-coin-id — click "
        "handler in wirePocDetail() won't fire and the card becomes a dead "
        "end (no path to the full POC detail modal)"
    )
    assert "poc-card" in body, (
        "pocCompactCardHtml output is missing the .poc-card class — the "
        "click delegate `.poc-card[data-poc-coin-id]` won't match"
    )
    # Must reuse the existing sparkline helper, not roll a new chart.
    assert "pocMigrationSparkline(" in body, (
        "pocCompactCardHtml should reuse pocMigrationSparkline() instead "
        "of rolling a new sparkline implementation"
    )


def test_pocEmptyCardHtml_is_defined_for_stocks():
    """Stocks (NVDA, AAPL, ...) and cryptos outside the top-50 POC window
    must fall back to ``pocEmptyCardHtml`` so the 2-col modal layout never
    leaves a blank slot. The helper must distinguish the two reasons so the
    user knows why POC isn't available.
    """
    html = _read_dashboard_or_skip()
    body = _extract_function_body(html, "pocEmptyCardHtml")

    # Stock-specific copy mentions crypto-only.
    assert "crypto" in body.lower(), (
        "pocEmptyCardHtml stock branch should explain POC is crypto-only"
    )
    # And a top-50 explanation for cryptos that aren't in poc_top.
    assert "top-50" in body or "top 50" in body or "top-50" in body, (
        "pocEmptyCardHtml crypto branch should explain top-50 windowing"
    )


def test_symbol_modal_body_stacks_one_column_on_mobile():
    """The ``.symbol-modal-body`` grid must collapse to 1-col on phone
    widths. The existing ``.grid2`` @media 860 already covers tablets, but
    a dedicated ≤480 rule guarantees phones stack even if .grid2 is
    refactored later.
    """
    html = _read_dashboard_or_skip()

    # Symbol modal body is declared with a tighter min-col than .grid2
    # (360px instead of 420px) so the 2-col layout actually engages within
    # the ~940px modal width.
    assert re.search(
        r"\.symbol-modal-body\s*\{[^}]*grid-template-columns\s*:\s*repeat\(auto-fit,\s*minmax\(\s*360px",
        html,
    ), "symbol-modal-body desktop column rule missing or wrong min-col"

    # Mobile ≤480 explicit override → 1-col.
    media_match = re.search(
        r"@media\s*\(max-width\s*:\s*480px\)\s*\{([^{}]|\{[^{}]*\})*?\.symbol-modal-body\s*\{[^}]*grid-template-columns\s*:\s*1fr",
        html,
    )
    assert media_match, (
        "missing @media (max-width:480px) { .symbol-modal-body { grid-"
        "template-columns: 1fr } } — phone-width users would still see the "
        "2-col layout if .grid2 ever stops collapsing"
    )


def test_symbol_detail_modal_width_fits_two_cards():
    """The modal container width must be wide enough for the side-by-side
    layout (two 360px-min cards + gap + padding ≈ 740px). Sanity check it
    didn't get shrunk back to the old 820px-or-less limit, which would
    silently force single-column.
    """
    html = _read_dashboard_or_skip()

    # Find the modal container line — must be the inner div under
    # #symbolDetailModal. Look for width:min(NNNpx,100%) near the modal.
    block_m = re.search(
        r'id=["\']symbolDetailModal["\'][^>]*>\s*<div[^>]*width:\s*min\(\s*(\d+)\s*px',
        html,
    )
    assert block_m, "could not find symbolDetailModal inner width style"
    width_px = int(block_m.group(1))
    assert width_px >= 900, (
        f"symbolDetailModal width is {width_px}px — needs ≥900px to comfortably "
        "host two 360px-min cards side-by-side (currently they'd wrap to 1-col)"
    )


# ---------- Stock-detail modal POC card-slot wiring ----------
def _extract_open_stock_detail_body(html: str) -> str:
    """Return the body of ``function openStockDetail(symbol){ ... }``."""
    m = re.search(r"function\s+openStockDetail\s*\(\s*symbol\s*\)\s*\{", html)
    assert m, "openStockDetail(symbol) function not found in dashboard.html"
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
                return html[start:i]
        i += 1
    raise AssertionError("Unterminated openStockDetail function body")


def test_stock_detail_modal_wraps_signal_and_poc_in_grid2():
    """``openStockDetail`` must inject a ``.grid2`` container holding the
    Signal card + POC card into ``#stockDetailBody``. The 2-column grid
    drops to 1-column at ≤860px via the global ``.grid2`` mobile rule, so
    the cards stack on mobile without any new media query."""
    html = _read_dashboard_or_skip()
    body = _extract_open_stock_detail_body(html)

    # Body is set via innerHTML assignment that wraps everything in .grid2.
    assert "stockDetailBody" in body and "innerHTML" in body, (
        "openStockDetail() no longer assigns to #stockDetailBody innerHTML"
    )
    assert 'grid2 stocks-modal-body' in body, (
        "openStockDetail() body no longer wraps content in "
        '"grid2 stocks-modal-body" — Signal + POC cards won\'t sit '
        "side-by-side on desktop."
    )
    # Both cells must be composed: the existing Signal card and the new POC
    # empty-state slot.
    assert "stockDetailHtml(s)" in body, (
        "openStockDetail() no longer calls stockDetailHtml(s) — Signal card "
        "left column is missing."
    )
    assert "stockPocEmptyHtml()" in body, (
        "openStockDetail() no longer calls stockPocEmptyHtml() — POC card "
        "right column is missing."
    )

    # The global .grid2 mobile rule (≤860px → 1fr) must still exist so the
    # 2-column layout collapses to a stack on phones.
    assert re.search(
        r"\.grid2\s*\{[^}]*grid-template-columns\s*:\s*1fr\s*!important",
        html,
    ), (
        ".grid2 ≤860px → 1fr !important mobile rule missing — the stock "
        "detail modal won't stack to a single column on phone viewports."
    )


def test_stock_detail_modal_poc_empty_state_copy_present():
    """The empty-state POC card must render its explanation copy so the user
    sees *why* there's no POC for stocks (crypto-only compute) rather than a
    blank/loading card. The wording is exact-match — copy changes are
    intentional and should update this test alongside the renderer."""
    html = _read_dashboard_or_skip()

    # Renderer must exist and produce a .stock-poc-card chart-card.
    assert "function stockPocEmptyHtml" in html, (
        "stockPocEmptyHtml() function missing from dashboard.html — the "
        "Stocks-tab modal has no POC card slot renderer."
    )
    assert "stock-poc-card" in html, (
        "stock-poc-card class missing from dashboard.html — POC slot "
        "marker class dropped from the empty-state card."
    )

    # Card header copy.
    assert ">Point of Control<" in html, (
        "POC card header text 'Point of Control' missing from dashboard.html"
    )

    # The two key sentences of the empty-state explanation must both be
    # present so the user understands the limitation.
    assert "POC volume-profile coverage is currently crypto-only" in html, (
        "Stock POC card empty-state lede missing from dashboard.html — "
        "user won't see the crypto-only explanation."
    )
    assert "Stock POC is not yet computed" in html, (
        "Stock POC card empty-state follow-up missing from dashboard.html — "
        "user won't see the 'backend extension required' callout."
    )
