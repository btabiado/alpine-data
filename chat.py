"""
Chat backend for the dashboard. Uses the Anthropic API with a compact
context summary of the latest dashboard payload as the system prompt.

Env:
    ANTHROPIC_API_KEY    required
    CHAT_MODEL           override the default model (e.g. claude-haiku-...)

Note: The LunarCrush MCP integration was removed when we discovered the
v4 API requires the Builder plan (~$240/mo). The `mcp_servers_config`
hook is preserved as an empty list so future MCP integrations can be
wired in without changing the chat protocol.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Iterator


def _summarise_payload(payload: dict) -> dict:
    """Compact projection of the payload that fits comfortably in a prompt.

    Excludes long daily series (only includes last 30 days), excludes
    by_fund_daily (only top 5 by_fund rows per asset), keeps signals + stats.
    """
    out: dict[str, Any] = {"generated_at": payload.get("generated_at")}

    for asset in ("btc", "eth", "link"):
        a = payload.get(asset)
        if not a:
            continue
        ao: dict[str, Any] = {
            "stats": a.get("stats", {}),
            "last_date": a.get("last_date"),
        }
        # last 30 days of daily flows
        daily = a.get("daily") or []
        ao["recent_daily"] = daily[-30:]
        # top 8 funds (sorted by total in source)
        ao["by_fund_top"] = (a.get("by_fund") or [])[:8]
        out[asset] = ao

    # signals
    sigs = payload.get("signals") or {}
    out["signals"] = {
        k: {
            "score": v.get("score"),
            "label": v.get("label"),
            "as_of": v.get("as_of"),
            "components": v.get("components"),
            "price": v.get("price"),
        }
        for k, v in sigs.items() if v
    }

    # market snapshot — just latest values + 7-day funding/dvol summary
    market = payload.get("market") or {}
    snap: dict[str, Any] = {"global": market.get("global", {})}
    for asset in ("btc", "eth", "link"):
        m = market.get(asset) or {}
        snap[asset] = {
            "last_price": (m.get("price") or [{}])[-1].get("value") if m.get("price") else None,
            "last_volume": (m.get("volume") or [{}])[-1].get("value") if m.get("volume") else None,
            "last_funding": (m.get("funding") or [{}])[-1].get("rate") if m.get("funding") else None,
            "last_oi_usd": (m.get("open_interest_usd") or [{}])[-1].get("oi_usd") if m.get("open_interest_usd") else None,
            "last_long_short": (m.get("long_short_ratio") or [{}])[-1].get("ratio") if m.get("long_short_ratio") else None,
            "last_dvol": (m.get("dvol") or [{}])[-1].get("dvol") if m.get("dvol") else None,
        }
    snap["fear_greed_latest"] = (market.get("fear_greed") or [{}])[-1] if market.get("fear_greed") else None
    snap["ethbtc_latest"] = (market.get("ethbtc") or [{}])[-1] if market.get("ethbtc") else None
    out["market_snapshot"] = snap

    # whale (just latest)
    whale = (payload.get("whale") or {}).get("btc") or {}
    out["btc_whale_latest"] = {
        k: (v[-1] if v else None) for k, v in whale.items() if isinstance(v, list)
    }

    # insights (already a short list)
    out["insights"] = payload.get("insights") or []
    return out


SYSTEM_PROMPT = """You are an analyst embedded in a private dashboard that
tracks U.S. spot BTC and ETH ETF flows, LINK trading metrics, perpetual
funding, open interest, implied volatility (DVOL), Fear & Greed, and BTC
on-chain whale proxies. You ALSO see a rules-based composite signal
(-100..+100) for each asset.

When the user asks a question, answer concisely using ONLY the dashboard
context below. If the data needed is not present, say so plainly.

NEVER give explicit investment advice or recommendations to buy or sell
specific assets. If asked, you may explain what the indicators say and
let the user draw their own conclusions. You may discuss risk factors.

Format:
- Lead with the direct answer in 1-2 sentences.
- Then give 2-4 bullet points with the supporting numbers from the data.
- Cite the date and metric explicitly (e.g. "as of 2026-05-12, BTC ETF
  7-day net = +$543M").
- Keep total response under ~200 words unless the user asks for more.
- If the user asks vague meta-questions ("what should I watch?"), point
  to the strongest insights and signal flips.
%s
Dashboard context (JSON):
%s
"""


class APIKeyMissing(RuntimeError):
    """Raised when ANTHROPIC_API_KEY is not configured."""


def is_configured() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _mcp_servers_config() -> list[dict]:
    """Return MCP server configs to pass to Anthropic. Currently always
    empty — the LunarCrush MCP wiring was removed when we discovered the
    v4 API requires the Builder plan ($240/mo). Hook preserved for future
    free MCP integrations."""
    return []


def mcp_status() -> dict:
    """Return a small status dict describing which MCP servers are active.

    Designed for the /api/chat route to expose to the client so it can
    surface a "social tools active" badge or similar. Keeps `is_configured()`
    backwards-compatible (still a plain bool).
    """
    servers = _mcp_servers_config()
    return {
        "mcp_available": bool(servers),
        "servers": [s["name"] for s in servers],
    }


def _client():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise APIKeyMissing(
            "ANTHROPIC_API_KEY not set. To enable LLM-powered chat: get a key at "
            "console.anthropic.com -> API Keys, then run "
            "`export ANTHROPIC_API_KEY=sk-ant-...` before starting the server. "
            "Restart with: lsof -ti:8765 | xargs kill && "
            "cd ~/alpine-data && HOST=0.0.0.0 .venv/bin/python server.py"
        )
    import anthropic
    return anthropic.Anthropic(api_key=api_key)


def fallback_answer(question: str, payload: dict) -> str:
    """Rule-based fallback when no API key is set.

    Pattern-matches common questions and answers from the payload.
    """
    q = (question or "").lower()
    out: list[str] = []

    def stats_for(asset):
        return ((payload.get(asset) or {}).get("stats") or {})

    def fmt(n):
        if n is None: return "?"
        sign = "-" if n < 0 else ""
        a = abs(n)
        if a >= 1000: return f"{sign}${a/1000:.2f}B"
        if a >= 1: return f"{sign}${a:.1f}M"
        return f"{sign}${a*1000:.0f}K"

    # Insights summary
    if "insight" in q or "important" in q or "summari" in q or "biggest change" in q or "today" in q:
        ins = payload.get("insights") or []
        if ins:
            out.append("Top insights right now:")
            for i in ins[:6]:
                out.append(f"  • {i.get('headline','')}" + (f" — {i['detail']}" if i.get("detail") else ""))
            out.append("")
        else:
            out.append("No notable insights right now.")

    # Signal summary
    if "signal" in q or "score" in q or "positioning" in q:
        sigs = payload.get("signals") or {}
        out.append("Current signal scores:")
        for asset, sig in sigs.items():
            if not sig: continue
            out.append(f"  • {asset.upper()}: {sig.get('label')} (score {sig.get('score', 0):+d})")
            for c in (sig.get("components") or [])[:4]:
                if c.get("contribution"): out.append(f"      - {c['name']}: {c['value']} ({c['contribution']:+d}) — {c.get('explanation','')}")
        out.append("")

    # Per-asset breakdown
    for asset in ("btc", "eth", "link"):
        if asset in q or asset.upper() in (question or ""):
            s = stats_for(asset)
            if s:
                out.append(f"{asset.upper()} ETF stats (as of {s.get('last_date','?')}):")
                out.append(f"  • Last day: {fmt(s.get('last_day_flow'))}")
                out.append(f"  • 7d: {fmt(s.get('last_7d'))}   30d: {fmt(s.get('last_30d'))}   YTD: {fmt(s.get('ytd'))}")
                out.append(f"  • All-time net: {fmt(s.get('all_time'))}")
                streak = s.get("streak") or {}
                if streak: out.append(f"  • Current streak: {streak.get('length',0)}d {streak.get('direction','')}")
                out.append("")

    # Top funds
    if "fund" in q or "ibit" in q or "ethe" in q or "etha" in q:
        for asset in ("btc","eth"):
            funds = ((payload.get(asset) or {}).get("by_fund") or [])
            if funds:
                out.append(f"Top {asset.upper()} funds by all-time:")
                for f in funds[:5]:
                    out.append(f"  • {f['fund']} ({f.get('name','')[:30]}): all-time {fmt(f.get('total'))}, 30d {fmt(f.get('last_30d'))}, share {f.get('share_pct',0):.1f}%")
                out.append("")

    # Funding
    if "funding" in q:
        m = payload.get("market") or {}
        out.append("Funding rates (last value, per period):")
        for asset in ("btc","eth","link"):
            a = (m.get(asset) or {}); funding = a.get("funding") or []
            if funding:
                r = funding[-1].get("rate", 0)
                out.append(f"  • {asset.upper()}: {r*100:.4f}% on {funding[-1].get('date')}")
        out.append("")

    if not out:
        out = [
            "I can answer from your dashboard data even without an API key, but I need a more specific question.",
            "",
            "Try things like:",
            "  • Summarise the most important insights",
            "  • What's BTC ETF flow today?",
            "  • Compare BTC and ETH signals",
            "  • Which BTC fund had the biggest 30-day inflow?",
            "  • What does funding look like?",
            "",
            "For full LLM-powered chat, set ANTHROPIC_API_KEY (see console.anthropic.com) and restart the server.",
        ]
    return "\n".join(out)


def stream_answer(question: str, payload: dict) -> Iterator[str]:
    """Yield text chunks from Claude streaming. MCP tool calls (when any
    servers are wired via _mcp_servers_config) are resolved transparently
    by the SDK before the final assistant text streams back. Currently
    no MCP servers are wired (LunarCrush integration was removed)."""
    client = _client()
    model = os.environ.get("CHAT_MODEL", "claude-haiku-4-5-20251001")
    summary = json.dumps(_summarise_payload(payload), default=str)
    mcp_servers = _mcp_servers_config()
    # MCP note slot kept for future integrations; currently always empty.
    system = SYSTEM_PROMPT % ("", summary)

    # Only pass mcp_servers + the beta header when we actually have one;
    # otherwise the call is identical to the pre-MCP behaviour.
    extra_kwargs: dict[str, Any] = {}
    if mcp_servers:
        extra_kwargs["mcp_servers"] = mcp_servers
        extra_kwargs["extra_headers"] = {"anthropic-beta": "mcp-client-2025-04-04"}

    with client.messages.stream(
        model=model,
        max_tokens=800,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": question}],
        **extra_kwargs,
    ) as stream:
        for text in stream.text_stream:
            yield text


# ---- safety: pre-filter clearly out-of-scope questions ----

_OUT_OF_SCOPE = re.compile(
    r"\b(should|do|can|would|could)\s+(i|we|you)\s+(buy|sell|short|long)\b|"
    r"\b(buy|sell|short|long)\b.{0,40}\b(now|today|this|right\s+now)\b|"
    r"\bprice\s+target\b|"
    r"\bwill\s+(btc|eth|link|bitcoin|ethereum|chainlink)\b.*\b(go|reach|hit|moon)\b|"
    r"\b(prediction|forecast|will\s+pump|will\s+dump|to\s+the\s+moon)\b",
    re.IGNORECASE,
)


def is_out_of_scope(question: str) -> bool:
    """Soft check for questions asking for predictions / explicit calls."""
    return bool(_OUT_OF_SCOPE.search(question or ""))
