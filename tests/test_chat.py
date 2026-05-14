"""Tests for chat.py — focused on the MCP wiring helpers.

These tests deliberately avoid hitting the live LunarCrush MCP server or
the Anthropic API. They only verify the config-shape helpers Claude needs
to wire up the SDK call correctly.
"""
from __future__ import annotations

import chat


def test_mcp_servers_config_empty_when_no_keys(monkeypatch):
    """Neither LUNARCRUSH_API_KEY nor LUNARCRUSH_MCP_URL set → empty list.

    This is the no-op path: chat behaves exactly as before MCP was wired,
    and stream_answer() passes no mcp_servers / beta header to Anthropic.
    """
    monkeypatch.delenv("LUNARCRUSH_API_KEY", raising=False)
    monkeypatch.delenv("LUNARCRUSH_MCP_URL", raising=False)
    assert chat._mcp_servers_config() == []
    assert chat.mcp_status() == {"mcp_available": False, "servers": []}


def test_mcp_servers_config_built_from_api_key(monkeypatch):
    """With LUNARCRUSH_API_KEY set, the URL is constructed from the key."""
    monkeypatch.delenv("LUNARCRUSH_MCP_URL", raising=False)
    monkeypatch.setenv("LUNARCRUSH_API_KEY", "test-key-123")

    servers = chat._mcp_servers_config()
    assert len(servers) == 1
    s = servers[0]
    assert s["type"] == "url"
    assert s["name"] == "lunarcrush"
    assert s["url"] == "https://lunarcrush.ai/sse?key=test-key-123"

    status = chat.mcp_status()
    assert status == {"mcp_available": True, "servers": ["lunarcrush"]}


def test_mcp_servers_config_explicit_url_overrides_key(monkeypatch):
    """LUNARCRUSH_MCP_URL takes precedence over the key-derived URL.

    Lets the user point at a self-hosted proxy without having to unset
    the API key (which would also disable the REST snapshot path).
    """
    monkeypatch.setenv("LUNARCRUSH_API_KEY", "test-key-123")
    monkeypatch.setenv("LUNARCRUSH_MCP_URL", "https://proxy.example.com/sse?tok=abc")

    servers = chat._mcp_servers_config()
    assert len(servers) == 1
    assert servers[0]["url"] == "https://proxy.example.com/sse?tok=abc"
    assert servers[0]["name"] == "lunarcrush"


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
