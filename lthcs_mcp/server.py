"""FastMCP server exposing LTHCS data as Claude-callable tools.

Run locally over stdio for Claude Code / Claude Desktop integration::

    python -m lthcs_mcp.server

Or over streamable HTTP for remote clients (optional)::

    python -m lthcs_mcp.server --http --port 8000

All tools are READ-ONLY consumers of files under ``data/lthcs/``.  No network
calls, no writes, no dependency on the dashboard apps or daily pipeline.
"""

from __future__ import annotations

import argparse
import sys
from typing import Optional

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as exc:  # pragma: no cover - import-time guard
    sys.stderr.write(
        "ERROR: the 'mcp' Python SDK is not installed.\n"
        "Install it with: pip install 'mcp[cli]'\n"
        f"Original error: {exc}\n"
    )
    raise

from pydantic import BaseModel, ConfigDict, Field, field_validator

from lthcs_mcp import data as ldata

mcp = FastMCP("lthcs_mcp")


# --- Pydantic input models -------------------------------------------------


class _StrictBase(BaseModel):
    model_config = ConfigDict(
        str_strip_whitespace=True,
        validate_assignment=True,
        extra="forbid",
    )


class TickerScoreInput(_StrictBase):
    ticker: str = Field(
        ...,
        description="Ticker symbol, e.g. 'AAPL'. Case-insensitive.",
        min_length=1,
        max_length=10,
    )
    date: Optional[str] = Field(
        default=None,
        description="ISO date YYYY-MM-DD (e.g. '2026-05-17'). Defaults to latest snapshot.",
    )


class DateOnlyInput(_StrictBase):
    date: Optional[str] = Field(
        default=None,
        description="ISO date YYYY-MM-DD. Defaults to latest snapshot.",
    )


class TopMoversInput(_StrictBase):
    direction: str = Field(
        default="gainers",
        description="'gainers' or 'decliners'.",
    )
    limit: int = Field(
        default=10, description="Max rows to return (1-100).", ge=1, le=100
    )
    period_days: int = Field(
        default=30,
        description="Lookback window in days (1-3650). Falls back to oldest available point if history is shorter.",
        ge=1,
        le=3650,
    )

    @field_validator("direction")
    @classmethod
    def _check_direction(cls, v: str) -> str:
        if v not in ("gainers", "decliners"):
            raise ValueError("direction must be 'gainers' or 'decliners'")
        return v


class InsiderSignalsInput(_StrictBase):
    ticker: Optional[str] = Field(
        default=None,
        description="Ticker symbol (e.g. 'AAPL'). Provide this OR regime.",
    )
    regime: Optional[str] = Field(
        default=None,
        description="One of: cluster_buying, buying, neutral, selling, heavy_selling, mixed.",
    )
    date: Optional[str] = Field(
        default=None, description="ISO date YYYY-MM-DD. Defaults to latest."
    )


class TickerOnlyInput(_StrictBase):
    ticker: str = Field(..., description="Ticker symbol.", min_length=1, max_length=10)
    date: Optional[str] = Field(
        default=None, description="ISO date YYYY-MM-DD. Defaults to latest."
    )


class HistoryInput(_StrictBase):
    ticker: str = Field(..., description="Ticker symbol.", min_length=1, max_length=10)
    days: int = Field(
        default=30, description="Number of recent points to return (1-3650).", ge=1, le=3650
    )


class SearchInput(_StrictBase):
    query: str = Field(
        ...,
        description="Substring to match against ticker symbol or company name. Case-insensitive.",
        min_length=1,
        max_length=100,
    )
    limit: int = Field(
        default=10, description="Max matches to return (1-50).", ge=1, le=50
    )


class DraggingPillarInput(_StrictBase):
    ticker: str = Field(
        ...,
        description="Ticker symbol (e.g. 'AAPL'). Case-insensitive; will be uppercased.",
        min_length=1,
        max_length=10,
    )


class ListBandInput(_StrictBase):
    band: str = Field(
        ...,
        description=(
            "Band name: one of 'elite', 'high_confidence', 'constructive', "
            "'monitor', 'weakening', 'review'. Case-insensitive."
        ),
        min_length=1,
        max_length=32,
    )
    limit: int = Field(
        default=20,
        description="Max tickers to return (1-500). Default 20.",
        ge=1,
        le=500,
    )
    date: Optional[str] = Field(
        default=None,
        description="ISO date YYYY-MM-DD. Defaults to latest snapshot.",
    )


class PillarAttributionInput(_StrictBase):
    ticker: str = Field(
        ..., description="Ticker symbol (case-insensitive).", min_length=1, max_length=10
    )
    pillar: str = Field(
        ...,
        description=(
            "Pillar name: one of 'adoption_momentum', 'institutional_confidence', "
            "'financial_evolution', 'thesis_integrity', 'des'."
        ),
        min_length=1,
        max_length=32,
    )
    date: Optional[str] = Field(
        default=None,
        description="ISO date YYYY-MM-DD. Defaults to latest snapshot.",
    )


class RecentMoversInput(_StrictBase):
    direction: str = Field(
        default="up",
        description="'up' for top gainers by drift_7d, 'down' for top decliners.",
    )
    limit: int = Field(
        default=10, description="Max rows to return (1-100).", ge=1, le=100
    )
    date: Optional[str] = Field(
        default=None,
        description="ISO date YYYY-MM-DD. Defaults to latest snapshot.",
    )

    @field_validator("direction")
    @classmethod
    def _check_direction(cls, v: str) -> str:
        if v not in ("up", "down"):
            raise ValueError("direction must be 'up' or 'down'")
        return v


class CryptoUniverseInput(_StrictBase):
    date: Optional[str] = Field(
        default=None,
        description="ISO date YYYY-MM-DD. Defaults to latest crypto snapshot.",
    )


# --- Tool definitions ------------------------------------------------------


@mcp.tool(
    name="get_ticker_score",
    annotations={
        "title": "Get LTHCS Score",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def get_ticker_score(params: TickerScoreInput) -> dict:
    """Return the LTHCS composite score, band, drift, and 5 pillar sub-scores
    for a ticker on a given date.

    Args:
        params (TickerScoreInput): ticker (required) and optional date.

    Returns:
        dict with keys: ticker, date, score, band, confidence_level,
        drift {1d, 7d, 30d, 90d}, subscores (adoption_momentum,
        institutional_confidence, financial_evolution, thesis_integrity, des),
        modifiers, maturity_stage, sector, data_quality_flags.
        On error: {"error": "..."}.
    """
    return ldata.get_ticker_score(params.ticker, params.date)


@mcp.tool(
    name="get_universe_distribution",
    annotations={
        "title": "Universe Band Distribution",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def get_universe_distribution(params: DateOnlyInput) -> dict:
    """Band counts (elite / high_confidence / constructive / monitor / weakening / review).

    Returns: {date, total_tickers, bands: {...}, other_bands: {...}}.
    """
    return ldata.get_universe_distribution(params.date)


@mcp.tool(
    name="get_composite_index",
    annotations={
        "title": "LTHCS Composite Index",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def get_composite_index(params: DateOnlyInput) -> dict:
    """Return the LTHCS Composite Index for a date.

    Returns: {date, score, label, band_key, color, components (9), note}.
    """
    return ldata.get_composite_index(params.date)


@mcp.tool(
    name="get_top_movers",
    annotations={
        "title": "Top Score Movers",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def get_top_movers(params: TopMoversInput) -> dict:
    """Top-N tickers by score delta over a lookback window. Direction is
    'gainers' (largest positive delta) or 'decliners' (largest negative).
    """
    return ldata.get_top_movers(
        direction=params.direction,
        limit=params.limit,
        period_days=params.period_days,
    )


@mcp.tool(
    name="get_insider_signals",
    annotations={
        "title": "Insider (Form 4) Signals",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def get_insider_signals(params: InsiderSignalsInput) -> dict:
    """Insider Form-4 conviction. Provide ``ticker`` for a single name, or
    ``regime`` (e.g. 'cluster_buying', 'heavy_selling') for a filtered list.
    """
    return ldata.get_insider_signals(
        ticker=params.ticker, regime=params.regime, date=params.date
    )


@mcp.tool(
    name="get_holdings",
    annotations={
        "title": "13F Institutional Holdings",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def get_holdings(params: TickerOnlyInput) -> dict:
    """13F holdings for a ticker: conviction_signal, signal_score, top_holders,
    manager_count, quarter_over_quarter changes.
    """
    return ldata.get_holdings(params.ticker, date=params.date)


@mcp.tool(
    name="get_pillar_breakdown",
    annotations={
        "title": "Per-Ticker Pillar Breakdown",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def get_pillar_breakdown(params: TickerOnlyInput) -> dict:
    """Variable-detail rows for a ticker (5 pillars + per-pillar components)."""
    return ldata.get_pillar_breakdown(params.ticker, date=params.date)


@mcp.tool(
    name="get_history",
    annotations={
        "title": "Per-Ticker Score History",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def get_history(params: HistoryInput) -> dict:
    """Last N daily score points for a ticker (newest first)."""
    return ldata.get_history(params.ticker, days=params.days)


@mcp.tool(
    name="get_macro_regime",
    annotations={
        "title": "Macro & Breadth Regime",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def get_macro_regime(params: DateOnlyInput) -> dict:
    """FRED breadth + sector strength + breadth sentiment for the date."""
    return ldata.get_macro_regime(params.date)


@mcp.tool(
    name="search_tickers",
    annotations={
        "title": "Search Tickers",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def search_tickers(params: SearchInput) -> dict:
    """Fuzzy match against ticker symbol or company name; return up to ``limit``
    matches with the current score and band attached.
    """
    return ldata.search_tickers(params.query, limit=params.limit)


@mcp.tool(
    name="get_dragging_pillar",
    annotations={
        "title": "Dragging Pillar (Weakening/Review)",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def get_dragging_pillar(params: DraggingPillarInput) -> dict:
    """For Weakening or Review tickers, return the pillar dragging the composite
    the most (lowest sub-score; ties broken by highest weight in weights_used).

    For Buy bucket (Elite/High/Constructive) and Hold (Monitor) tickers,
    returns ``{"dragging_pillar": null, "reason": "ticker is in Buy or Hold;
    no drag to surface"}``.

    Returns: {ticker, band, dragging_pillar, sub_score, rationale}.
    """
    return ldata.get_dragging_pillar(params.ticker)


@mcp.tool(
    name="list_band",
    annotations={
        "title": "List Tickers in Band",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def list_band(params: ListBandInput) -> dict:
    """Return tickers in a given band on a given date, sorted by composite desc.

    Args:
        params (ListBandInput): band (required), limit (default 20), date.

    Returns: {date, band, total_in_band, count, limit, tickers: [{ticker,
        score, drift_7d, drift_30d, sector, confidence_level}, ...]}.
    """
    return ldata.list_band(
        band=params.band, limit=params.limit, date=params.date
    )


@mcp.tool(
    name="get_pillar_attribution",
    annotations={
        "title": "Pillar Attribution Evidence",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def get_pillar_attribution(params: PillarAttributionInput) -> dict:
    """Return a single pillar's sub-score and the variable_detail evidence
    (raw signals + values) that fed into it.

    Returns: {date, ticker, pillar, sub_score, evidence: [...]}.
    """
    return ldata.get_pillar_attribution(
        ticker=params.ticker, pillar=params.pillar, date=params.date
    )


@mcp.tool(
    name="get_recent_movers",
    annotations={
        "title": "Recent Movers (drift_7d)",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def get_recent_movers(params: RecentMoversInput) -> dict:
    """Top N tickers by ``drift_7d`` (direction='up') or bottom N
    (direction='down'). Mirrors the Movers leaderboard from the UI.

    Returns: {date, direction, count, limit, movers: [{ticker, score, band,
        drift_7d, sector}, ...]}.
    """
    return ldata.get_recent_movers(
        direction=params.direction, limit=params.limit, date=params.date
    )


@mcp.tool(
    name="get_crypto_universe",
    annotations={
        "title": "LTHCS Crypto Universe",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def get_crypto_universe(params: CryptoUniverseInput) -> dict:
    """Latest BTC/ETH/SOL/etc LTHCS scores from
    ``data/lthcs/snapshots_crypto/<latest>.json``.

    Returns: {date, asset_class, model_version, count, tickers: [{ticker,
        score, band, subscores, dropped_pillars, ...}, ...]}.
    """
    return ldata.get_crypto_universe(date=params.date)


# --- Entry point -----------------------------------------------------------


def main(argv: Optional[list] = None) -> None:
    parser = argparse.ArgumentParser(
        prog="lthcs_mcp.server",
        description="MCP server exposing LTHCS data as tools.",
    )
    parser.add_argument(
        "--http",
        action="store_true",
        help="Run on streamable HTTP instead of stdio (for remote clients).",
    )
    parser.add_argument(
        "--port", type=int, default=8000, help="HTTP port (default 8000)."
    )
    args = parser.parse_args(argv)
    if args.http:
        mcp.run(transport="streamable-http")
    else:
        mcp.run()


if __name__ == "__main__":  # pragma: no cover
    main()
