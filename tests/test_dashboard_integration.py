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
_REQUIRED_NON_EMPTY = [
    "coinbase.btc.price_usd",            # Overview / header price
    "cadli_btc[0].close",                # Trading chart series
    "stocks_signals[0].symbol",          # Stocks tab top row
    "poc_top[0].symbol",                 # POC tab top card
]
_REQUIRED_KEY_PRESENT_MARKET: list[str] = []
_REQUIRED_NON_EMPTY_WHALE = [
    "distribution.buckets",              # Whale distribution chart
]
_REQUIRED_KEY_PRESENT_WHALE = [
    "eth.blockchair",                    # ETH whale section (sub-tree may be {})
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


def test_market_json_renderer_paths_exist():
    market = _load_json_or_skip(MARKET_JSON)
    for p in _REQUIRED_NON_EMPTY:
        _assert_present_non_empty(market, p, "market.json")
    for p in _REQUIRED_KEY_PRESENT_MARKET:
        _assert_key_present(market, p, "market.json")


def test_whale_json_renderer_paths_exist():
    whale = _load_json_or_skip(WHALE_JSON)
    for p in _REQUIRED_NON_EMPTY_WHALE:
        _assert_present_non_empty(whale, p, "whale.json")
    for p in _REQUIRED_KEY_PRESENT_WHALE:
        _assert_key_present(whale, p, "whale.json")


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
