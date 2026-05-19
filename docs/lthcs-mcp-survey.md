# `lthcs_mcp/` State Survey — Tier 5 #26 Scoping

_Read-only survey, 2026-05-18. No code touched._

## 1. Verdict

**`lthcs_mcp/` is essentially done — ~85% complete.** It is a working FastMCP
server with 10 fully-implemented, validated, defensively-coded tools over the
LTHCS dataset, plus 31 passing unit tests against the data layer and a
README covering install / launch / Claude-client registration. The only
missing pieces are operational polish: pinning the `mcp` SDK in
`requirements.txt`, an end-to-end smoke test that boots the server, and a
decision on deployment target (local stdio vs hosted HTTP). It is **not** a
stub or skeleton — every tool has a real implementation, real input
validation (Pydantic `extra="forbid"`, bounded ints, regex-like enum
checks), and proper read-only / idempotent / closed-world annotations.

## 2. What's in the directory

File-by-file inventory (paths absolute):

| Path | LOC | Role |
|---|---|---|
| `/Users/bryantabiadon/Documents/btc-eth-etf-dashboard/lthcs_mcp/__init__.py` | 39 | Re-exports the 10 data-layer functions and `DEFAULT_DATA_ROOT`. `__version__ = "0.1.0"`. |
| `/Users/bryantabiadon/Documents/btc-eth-etf-dashboard/lthcs_mcp/data.py` | 598 | Pure-Python data-access layer. JSON readers for `data/lthcs/{snapshots,index,insider,holdings,variable_detail,history/by_ticker,macro,universe.json}`. Defensive `_err()` envelopes, future-date rejection, latest-snapshot resolution, fuzzy ticker search with prefix/contains rank. |
| `/Users/bryantabiadon/Documents/btc-eth-etf-dashboard/lthcs_mcp/server.py` | 341 | FastMCP wrapper. Pydantic input models with `extra="forbid"`, ten `@mcp.tool` definitions, `argparse` entry point supporting `--http --port 8000` (streamable HTTP) and default stdio. Import-guard message if `mcp` SDK missing. |
| `/Users/bryantabiadon/Documents/btc-eth-etf-dashboard/lthcs_mcp/README.md` | 142 | Install, launch (stdio + HTTP), Claude Desktop / Claude Code JSON config, full tool table with example invocations and return shapes. |

**Tests** (read-only): `/Users/bryantabiadon/Documents/btc-eth-etf-dashboard/tests/lthcs/test_mcp_server.py` — 521 lines, **31 passing** as of survey, with a `fake_root` fixture that builds a synthetic `data/lthcs/` tree under `tmp_path` plus two `skipif`-guarded smoke tests against real repo data (2026-05-17 snapshot).

**Dependencies**:
- `requirements.txt` line 12: `pydantic>=2.5` ✓ (used by `server.py` Pydantic v2 `ConfigDict`).
- `requirements.txt` line 3: `anthropic==0.101.0` ✓ (not consumed by lthcs_mcp itself — that's for elsewhere; MCP servers do not need the Anthropic SDK).
- **`mcp` (or `mcp[cli]`) is NOT pinned in requirements.txt** and is NOT currently installed in `.venv` (verified: `ModuleNotFoundError: No module named 'mcp'`). README instructs the user to `pip install 'mcp[cli]'` manually. This is the single biggest gap to "shippable."

## 3. What works today

The data layer works **right now** without any install — 31 tests green
(`.venv/bin/python -m pytest tests/lthcs/test_mcp_server.py -q` → 31 passed
in 0.06s).

Once `pip install 'mcp[cli]'` is run, the server launches via:

```bash
.venv/bin/python -m lthcs_mcp.server          # stdio (Claude Desktop / Code)
.venv/bin/python -m lthcs_mcp.server --http --port 8000   # streamable HTTP
```

10 tools exposed (all read-only, idempotent, closed-world):

1. `get_ticker_score(ticker, date?)` → composite score, band, drift 1/7/30/90d, 5 subscores, modifiers, sector, maturity_stage, data_quality_flags
2. `get_universe_distribution(date?)` → band counts (elite/high_confidence/constructive/monitor/weakening/review) + other_bands
3. `get_composite_index(date?)` → LTHCS composite score, label, band_key, color, 9 components, note
4. `get_top_movers(direction, limit, period_days)` → top-N gainers/decliners by score delta with fallback to oldest available history point
5. `get_insider_signals(ticker? OR regime?, date?)` → Form-4 conviction with regime classification (cluster_buying/buying/mixed/neutral/selling/heavy_selling)
6. `get_holdings(ticker, date?)` → 13F conviction_signal, signal_score, top_holders, manager_count, QoQ
7. `get_pillar_breakdown(ticker, date?)` → variable_detail rows (5 pillars + components)
8. `get_history(ticker, days=30)` → last N daily score points, newest first
9. `get_macro_regime(date?)` → FRED breadth + sector_strength + breadth_sentiment, with partial-data tolerance
10. `search_tickers(query, limit=10)` → ranked fuzzy match (exact > prefix > contains-symbol > contains-name) with latest score and band attached

Every tool returns `{"error": "..."}` rather than raising — consistent shape for LLM recovery.

## 4. What's missing for production

Measured against the `anthropic-skills:mcp-builder` "production MCP server" checklist:

- **Tool definitions** — Done. 10 tools cover all the major LTHCS reads (snapshots, history, narratives via variable_detail, holdings, insider, macro, composite, search). Writes are deliberately not exposed (read-only dataset is correct).
- **Resource exposure** — Not implemented. The current design exposes everything via `tools` only. MCP also supports `resources` (URI-addressable content) and `prompts`. For a read-only data server tools are fine, but resources (e.g., `lthcs://snapshot/2026-05-17`) would let clients enumerate available dates without a tool round-trip. Optional polish, not blocking.
- **Auth / API keys** — N/A. The server only reads local JSON. No secrets needed. (If we ever expose hosted HTTP publicly, see §7.)
- **Cache / rate limiting** — Not implemented; not needed for local file reads. If hosted, add a thin in-process LRU on `_read_json`.
- **Tests** — 31 unit tests on data layer (excellent coverage). **Missing**: an end-to-end test that imports `lthcs_mcp.server` with `mcp` actually installed and asserts FastMCP enumerates the 10 tools. Would catch decorator-level regressions.
- **Deployment story** — README documents both stdio and `--http`. Decision still open whether to (a) leave it as user-local stdio, (b) deploy a hosted HTTP endpoint and register in the Claude Code MCP registry, or (c) both.
- **`mcp` SDK pin** — Missing from `requirements.txt`. Required for both the install instructions in the README and for a future CI test that imports the server module.
- **Logging / instrumentation** — None. FastMCP has built-in stdio logging which is fine for local; if hosted, add structured logs.
- **`README` accuracy** — Tools table in README matches `server.py` exactly. Verified.

## 5. Effort estimate

Audit currently scores Tier 5 #26 as **M**.

**Revised: S** (small — polish only).

Concretely: maybe 1–2 sessions of work:
- Add `mcp[cli]>=1.0` to `requirements.txt` (1 line).
- Add a single `tests/lthcs/test_mcp_server_boot.py` that imports `lthcs_mcp.server` and asserts `mcp._tool_manager._tools` has 10 entries (~30 lines, conditionally skipped if `mcp` not installed in CI).
- Decide deployment target. If local stdio only: nothing else needed — README is shippable. If hosted HTTP: add a `Procfile` / `Dockerfile` and a deploy target, plus a registry submission.
- Optionally add MCP `resources` for date-addressable snapshots (nice-to-have, ~30 LOC).

The Tier 5 #26 row in `docs/lthcs-open-items-audit.md` line 265 should be updated from "M" to "S" (and arguably the "Why" column expanded to note the implementation already exists).

## 6. Recommended path

**Specific files to fill in**:
1. `requirements.txt` — append `mcp[cli]>=1.0`. (1 line.)
2. `tests/lthcs/test_mcp_server_boot.py` — new ~30-line test that does `pytest.importorskip("mcp")`, imports `lthcs_mcp.server`, and asserts the FastMCP instance has the 10 expected tool names.
3. `lthcs_mcp/README.md` — minor edit removing the "not yet pinned" caveat after step 1 lands.

**MCP tools to expose** — already done. The asked-for set (`get_lthcs_snapshot`, `get_ticker_history`, `get_pillar_breakdown`, `get_dragging_pillar`) maps cleanly:
- `get_lthcs_snapshot(date)` ≈ existing `get_universe_distribution` + `get_composite_index`. Could add a thin convenience tool combining both if desired.
- `get_ticker_history(ticker)` ≈ existing `get_history` ✓
- `get_pillar_breakdown(ticker, date)` ≈ existing `get_pillar_breakdown` ✓
- `get_dragging_pillar(ticker)` — **not currently exposed**. The variable_detail data is there, so a ~15-LOC helper that returns the lowest-scoring of the 5 pillars per ticker would close this gap. Recommend adding.

**Suggested deployment target**: **local stdio first** (zero infra, zero cost, works today with the README config), and consider hosted HTTP via the Claude Code MCP registry only after we get a sense of whether external users want it. The dataset is small and local-first; there's no compelling reason to host it for the MVP.

## 7. Blockers

- **Anthropic-side**: None. MCP servers do not require the Anthropic SDK. The repo's `anthropic==0.101.0` pin is for other code paths (LLM narratives, sentiment). The `mcp` SDK is independent and BSD-licensed.
- **Repo-side**: None. The module is self-contained, imports nothing from `app.py` / `v2/app.py` / the daily pipeline. Conforms to LTHCS auto-push policy (no app.py / CI touch needed).
- **User-side**: None. No secrets required for local stdio. If we later go hosted HTTP, we'd want a `MCP_SERVER_URL` env on the client side but nothing on the server side until we add auth.
- **Schema drift risk**: The data layer reads JSON shapes that the daily pipeline writes. If the pipeline ever renames a field (e.g., `lthcs_score` → `score`), tools silently return `None` for that field. The two real-data smoke tests at lines 499 and 517 of `test_mcp_server.py` will catch the most important cases (subscores set, components list), but coverage is partial. Worth adding one more real-data smoke per tool over time.

---

**Bottom line**: this is a polish job, not a build job. The hardest part (designing 10 well-shaped tools with strict input validation and consistent error envelopes) is already done.
