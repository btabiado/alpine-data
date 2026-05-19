"""Boot test for the LTHCS FastMCP server.

This is intentionally a tiny smoke test: import the server module and verify
the tool registry contains the canonical set of tools we've shipped. The
heavy lifting (data-layer correctness) lives in ``test_mcp_server.py`` and
runs without the ``mcp`` SDK installed.

If the ``mcp`` SDK is not installed in the running venv this test is
SKIPPED — that's the expected state on a fresh checkout. Once
``mcp[cli]>=1.0`` is pip-installed the test runs and guards against
regressions in the FastMCP tool registration plumbing.
"""

from __future__ import annotations

import pytest

# Skip cleanly when the SDK isn't available — boot tests have no business
# breaking the suite for an optional integration dep.
pytest.importorskip("mcp")


# Canonical tool registry shipped by lthcs_mcp.server. Keep this in sync with
# the @mcp.tool decorators in lthcs_mcp/server.py.
_EXPECTED_TOOLS = {
    # 10 originals from the MCP survey (docs/lthcs-mcp-survey.md):
    "get_ticker_score",
    "get_universe_distribution",
    "get_composite_index",
    "get_top_movers",
    "get_insider_signals",
    "get_holdings",
    "get_pillar_breakdown",
    "get_history",
    "get_macro_regime",
    "search_tickers",
    # Tier 5 #26 follow-up — dragging-pillar callout as an MCP tool:
    "get_dragging_pillar",
}


def _registered_tool_names(server_module) -> set:
    """Pull the set of registered tool names from a FastMCP instance.

    FastMCP exposes its registry via ``list_tools()`` (async) and a private
    ``_tool_manager`` attribute. We try a couple of access paths so the test
    remains robust across minor SDK versions.
    """
    mcp = server_module.mcp
    # Newer FastMCP exposes _tool_manager._tools dict.
    tm = getattr(mcp, "_tool_manager", None)
    if tm is not None:
        tools = getattr(tm, "_tools", None)
        if isinstance(tools, dict):
            return set(tools.keys())
        list_tools = getattr(tm, "list_tools", None)
        if callable(list_tools):
            return {t.name for t in list_tools()}
    # Fallback: try the public async list_tools.
    list_tools = getattr(mcp, "list_tools", None)
    if callable(list_tools):
        import asyncio

        result = asyncio.run(list_tools())
        return {t.name for t in result}
    raise AssertionError("could not introspect FastMCP tool registry")


def test_server_module_imports() -> None:
    """The server module imports cleanly when ``mcp`` is installed."""
    from lthcs_mcp import server  # noqa: F401

    assert server.mcp is not None


def test_server_registers_all_expected_tools() -> None:
    """Every canonical LTHCS MCP tool is registered on the FastMCP instance."""
    from lthcs_mcp import server

    names = _registered_tool_names(server)
    missing = _EXPECTED_TOOLS - names
    assert not missing, f"missing expected MCP tools: {sorted(missing)}"
