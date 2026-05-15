"""Tests for chat.py — focused on the configuration helpers.

These tests deliberately avoid hitting the Anthropic API. They only verify
the config-shape helpers server.py needs to wire up the chat route correctly.
"""
from __future__ import annotations

import chat


def test_mcp_servers_config_empty():
    """No MCP servers wired anymore — LunarCrush was removed when we confirmed
    the v4 API requires the Builder plan ($240/mo). The hook is preserved
    as an always-empty list for future free MCP integrations.
    """
    assert chat._mcp_servers_config() == []
    assert chat.mcp_status() == {"mcp_available": False, "servers": []}


def test_is_configured_still_returns_bool(monkeypatch):
    """is_configured() must stay a plain bool — server.py treats it as one.

    Regression guard: if we ever change is_configured() to return a dict,
    the existing `if not configured:` branch in server.py would silently
    flip behaviour.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert chat.is_configured() is False
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    assert chat.is_configured() is True
