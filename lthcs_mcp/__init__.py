"""lthcs_mcp — Model Context Protocol server exposing LTHCS data as tools.

Read-only consumer of files under ``data/lthcs/``. No external network calls,
no writes, no dependency on the dashboard apps or daily pipeline.

The module is structured so that the data-access layer (:mod:`lthcs_mcp.data`)
is importable and testable without the ``mcp`` SDK installed; only the
:mod:`lthcs_mcp.server` entry point requires ``pip install mcp``.
"""

from lthcs_mcp.data import (
    DEFAULT_DATA_ROOT,
    get_composite_index,
    get_history,
    get_holdings,
    get_insider_signals,
    get_macro_regime,
    get_pillar_breakdown,
    get_ticker_score,
    get_top_movers,
    get_universe_distribution,
    search_tickers,
)

__all__ = [
    "DEFAULT_DATA_ROOT",
    "get_composite_index",
    "get_history",
    "get_holdings",
    "get_insider_signals",
    "get_macro_regime",
    "get_pillar_breakdown",
    "get_ticker_score",
    "get_top_movers",
    "get_universe_distribution",
    "search_tickers",
]

__version__ = "0.1.0"
