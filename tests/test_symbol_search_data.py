"""Contract tests for the universal symbol-search modal.

The header symbol-search box looks up a ticker (BTC, NVDA, SOL, AAPL, ...) and
surfaces a modal with: signal score, POC if available, recent news, and
sentiment for that symbol. The lookup itself is implemented in JS inside
``dashboard.html``; Python can't execute it. These tests instead verify the
DATA structures the JS lookup expects are present and shaped the way the
renderer reads them. If a fetcher silently drops one of these keys (rate
limit, schema shift), the symbol-search modal renders blank without erroring
— exactly the kind of bug ``test_dashboard_integration.py`` was designed to
catch for other tabs.

Each test prefers ``pytest.skip`` over ``assert False`` when the underlying
data is not present, since CI may not always have populated data.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_HTML = ROOT / "dashboard.html"
MARKET_JSON = ROOT / "data" / "market.json"


# ---------- helpers ----------


def _load_market_or_skip() -> dict:
    if not MARKET_JSON.exists():
        pytest.skip(f"{MARKET_JSON} not present — run fetch_market.py first")
    try:
        return json.loads(MARKET_JSON.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        pytest.fail(f"market.json is not valid JSON: {e}")


def _read_dashboard_or_skip() -> str:
    if not DASHBOARD_HTML.exists():
        pytest.skip(f"dashboard.html not built yet ({DASHBOARD_HTML})")
    return DASHBOARD_HTML.read_text(encoding="utf-8")


def _extract_json_array_at(html: str, key: str) -> list:
    """Extract the JSON array that follows ``"<key>":`` in the DATA blob.

    Uses brace/bracket-matched extraction (same approach as
    ``_extract_select_tab_body`` in test_dashboard_integration.py) so a
    nested array or object inside the value can't end the match early.
    """
    needle = f'"{key}":'
    idx = html.find(needle)
    assert idx != -1, f"{key!r} not found in dashboard.html DATA blob"
    # Find the next '[' after the key — that's the array start.
    i = html.find("[", idx + len(needle))
    assert i != -1, f"no '[' found after {key!r} in dashboard.html"
    start = i
    depth = 0
    in_str = False
    esc = False
    while i < len(html):
        ch = html[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    return json.loads(html[start:i + 1])
        i += 1
    raise AssertionError(f"Unterminated JSON array for key {key!r}")


# ---------- tests ----------


def test_news_searchable_by_symbol_keyword():
    """The symbol-search modal lists news entries whose title matches the
    queried ticker. Confirm ``market.news`` is a non-empty list of dicts
    each carrying a string ``title`` that the JS substring filter can scan."""
    market = _load_market_or_skip()
    news = market.get("news")
    if not isinstance(news, list) or not news:
        pytest.skip("market.news is empty — nothing to search")

    for i, entry in enumerate(news):
        assert isinstance(entry, dict), (
            f"news[{i}] is not a dict: {type(entry).__name__}"
        )
        title = entry.get("title")
        assert isinstance(title, str) and title, (
            f"news[{i}] missing string 'title' (got {title!r})"
        )


def test_stocks_signals_searchable_by_symbol():
    """The symbol-search modal joins user input against ``stocks_signals``
    by uppercase symbol. Confirm at least one entry has an uppercase
    ``symbol`` so the join key works."""
    market = _load_market_or_skip()
    stocks = market.get("stocks_signals")
    if not isinstance(stocks, list) or not stocks:
        pytest.skip(
            "market.stocks_signals missing — runtime-computed key requires "
            "`python app.py --fetch-market`"
        )

    upper_syms = [
        e.get("symbol")
        for e in stocks
        if isinstance(e, dict)
        and isinstance(e.get("symbol"), str)
        and e.get("symbol") == e.get("symbol", "").upper()
        and e.get("symbol")
    ]
    assert upper_syms, (
        "no stocks_signals entry has an uppercase 'symbol' — the symbol "
        "search modal cannot match user input against this list"
    )


def test_poc_top_searchable_by_symbol():
    """The symbol-search modal pulls the POC block from ``poc_top`` keyed
    by symbol. Confirm at least one entry carries a ``symbol`` field."""
    market = _load_market_or_skip()
    poc_top = market.get("poc_top")
    if not isinstance(poc_top, list) or not poc_top:
        pytest.skip(
            "market.poc_top missing — runtime-computed key requires "
            "`python app.py --fetch-market`"
        )

    with_symbol = [
        e for e in poc_top
        if isinstance(e, dict)
        and isinstance(e.get("symbol"), str)
        and e.get("symbol")
    ]
    assert with_symbol, (
        "no poc_top entry has a 'symbol' field — the symbol search modal "
        "cannot surface a POC block for any ticker"
    )


def test_signals_top20_searchable_by_symbol_in_dashboard_html():
    """``signals_top20`` is computed at payload-build time and embedded in
    ``dashboard.html`` (it is not persisted in market.json). The
    symbol-search modal reads it to surface the signal score for a coin.
    Extract it with a brace-matched JSON walk (same pattern as
    test_dashboard_integration.py) and confirm at least one entry has an
    uppercase symbol."""
    html = _read_dashboard_or_skip()
    try:
        signals_top20 = _extract_json_array_at(html, "signals_top20")
    except AssertionError as e:
        pytest.skip(f"signals_top20 not embedded in dashboard.html: {e}")
    if not signals_top20:
        pytest.skip("signals_top20 array is empty — nothing to search")

    upper_syms = [
        e.get("symbol")
        for e in signals_top20
        if isinstance(e, dict)
        and isinstance(e.get("symbol"), str)
        and e.get("symbol") == e.get("symbol", "").upper()
        and e.get("symbol")
    ]
    assert upper_syms, (
        "no signals_top20 entry has an uppercase 'symbol' — the symbol "
        "search modal cannot match user input against the coin signal list"
    )


def test_cc_news_sentiment_keyed_by_lowercase_symbol():
    """The symbol-search modal looks up sentiment for a coin via
    ``market.social.cc_news.coins[<lower-symbol>]``. Confirm the keys are
    lowercase ticker strings so the JS lookup (which lowercases user input)
    actually hits."""
    market = _load_market_or_skip()
    social = market.get("social")
    if not isinstance(social, dict):
        pytest.skip("market.social not available")
    cc_news = social.get("cc_news")
    if not isinstance(cc_news, dict):
        pytest.skip("market.social.cc_news not available")
    coins = cc_news.get("coins")
    if not isinstance(coins, dict) or not coins:
        pytest.skip("market.social.cc_news.coins not available or empty")

    for key in coins.keys():
        assert isinstance(key, str) and key, (
            f"cc_news.coins has a non-string or empty key: {key!r}"
        )
        assert key == key.lower(), (
            f"cc_news.coins key {key!r} is not lowercase; the symbol search "
            "modal lowercases user input before lookup, so non-lowercase "
            "keys will never be found"
        )
