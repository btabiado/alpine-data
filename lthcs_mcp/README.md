# lthcs_mcp — LTHCS as MCP Tools

A read-only [Model Context Protocol](https://modelcontextprotocol.io/) server
that exposes the LTHCS dataset (per-ticker scores, pillar breakdowns, insider
Form 4 conviction, 13F holdings, composite index, macro regime) as tools that
any MCP-compatible Claude client can call directly.

> "What's AAPL's LTHCS score?"
> "Show me today's cluster_buying tickers."
> "What's the composite index reading?"

The server is a thin FastMCP wrapper over the JSON files under `data/lthcs/`.
It does **not** modify any data, hit the network, or share code with the
dashboard or the daily pipeline.

---

## Install

The `mcp` Python SDK is not yet pinned in `requirements.txt`. Install it
into the project venv:

```bash
.venv/bin/pip install 'mcp[cli]'
```

(`mcp` brings in `pydantic`, which the repo already pins to `>=2.5`.)

Verify it imports:

```bash
.venv/bin/python -c "from mcp.server.fastmcp import FastMCP; print('ok')"
```

## Run locally (stdio — for Claude Code / Claude Desktop)

```bash
.venv/bin/python -m lthcs_mcp.server
```

The process listens on stdio. You will not see output until a client
connects — that's normal.

## Run remotely (streamable HTTP — optional)

```bash
.venv/bin/python -m lthcs_mcp.server --http --port 8000
```

## Register with Claude Code / Claude Desktop

Add this entry to `~/.claude/mcp.json` (Claude Code) or
`~/Library/Application Support/Claude/claude_desktop_config.json`
(Claude Desktop):

```json
{
  "mcpServers": {
    "lthcs": {
      "command": "/Users/bryantabiadon/Documents/btc-eth-etf-dashboard/.venv/bin/python",
      "args": ["-m", "lthcs_mcp.server"],
      "cwd": "/Users/bryantabiadon/Documents/btc-eth-etf-dashboard"
    }
  }
}
```

Restart the client and the 10 tools below will appear.

---

## Tools

| Tool | Description |
| --- | --- |
| `get_ticker_score` | Composite score, band, drift, and 5 pillar sub-scores for a ticker. |
| `get_universe_distribution` | Counts per band across the full universe. |
| `get_composite_index` | LTHCS Composite Index (score, label, color, 9 components, note). |
| `get_top_movers` | Top-N tickers by score delta over a window. |
| `get_insider_signals` | Form 4 insider conviction by ticker or by regime. |
| `get_holdings` | 13F institutional holdings for a ticker. |
| `get_pillar_breakdown` | Variable-detail rows (5 pillars + components) for a ticker. |
| `get_history` | Last N days of score history for a ticker. |
| `get_macro_regime` | FRED breadth + sector strength + breadth sentiment. |
| `search_tickers` | Fuzzy match against symbol or name; returns current scores. |

### Example invocations

```text
get_ticker_score(ticker="AAPL")
  → {score: 58.7, band: "weakening", drift: {1d: 0.0, ...},
     subscores: {adoption_momentum: 50.0, ...}, sector: "Technology", ...}

get_universe_distribution()
  → {date: "2026-05-17", total_tickers: 167,
     bands: {elite: 0, high_confidence: 1, constructive: 14,
             monitor: 28, weakening: 55, review: 69}}

get_composite_index()
  → {date: "2026-05-17", score: -15, label: "LTHCS NEUTRAL",
     band_key: "monitor", color: "#F0A861", components: [...]}

get_top_movers(direction="gainers", limit=5, period_days=30)
  → {direction: "gainers", count: 5, movers: [{ticker: "...", delta: 2.7, ...}]}

get_insider_signals(regime="cluster_buying")
  → {date: "2026-05-17", regime: "cluster_buying", count: N, tickers: [...]}

get_holdings(ticker="AAPL")
  → {ticker: "AAPL", conviction_signal: "mixed", signal_score: -0.1,
     manager_count: 10, top_holders: [{manager: "BlackRock", ...}, ...]}

get_pillar_breakdown(ticker="AAPL")
  → {ticker: "AAPL", pillars: [{pillar: "adoption_momentum", ...}, ...]}

get_history(ticker="AAPL", days=30)
  → {ticker: "AAPL", count: 2, history: [{date: "2026-05-17", score: 58.7, ...}, ...]}

get_macro_regime()
  → {date: "2026-05-17", available: ["breadth", "sector_strength", "breadth_sentiment"],
     breadth: {...}, sector_strength: {...}, breadth_sentiment: {...}}

search_tickers(query="apple")
  → {query: "apple", count: 1, matches: [{ticker: "AAPL", name: "Apple Inc.",
     score: 58.7, band: "weakening", ...}]}
```

## Error shape

Every tool returns `{"error": "human-readable message"}` rather than raising
when input is invalid or the underlying data file is missing. This keeps the
MCP transport's response shape consistent and easier for an LLM to recover
from.

## Tests

```bash
.venv/bin/python -m pytest tests/lthcs/test_mcp_server.py -q
```

The tests target the pure-Python data layer (`lthcs_mcp.data`) so they run
without the `mcp` SDK installed.
