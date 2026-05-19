# LTHCS Tier 5 #28 — LLM Sentiment Shadow Run (design spec)

**Status:** design only. No code in this PR.
**Audit reference:** `docs/lthcs-data-audit-2026-05-18.md` (§Recommended fixes,
P2 #7 + "Thesis live history") and `docs/lthcs-open-items-audit.md` row 28 +
"Suggested next 3 commits" #2.
**Audit cost claim:** ~$0.85/day. See §4 — the realistic Haiku-with-caching
number is closer to **$0.03/day**; the $0.85 figure assumed Sonnet without
caching. The "cheapest Tier 5 win" framing stands either way.

---

## 0. Pre-condition

`lthcs/sources/llm_sentiment.py` **already exists** (718 lines,
`claude-sonnet-4-5` default, prompt caching wired, fallback to engagement
heuristic, 513-line test file at `tests/lthcs/test_llm_sentiment.py`).
Today it is wired at `lthcs_daily.py:1250` as a **replacement** for the
AI-news engagement heuristic — it writes into the same per-ticker rotation
file via `_write_supplement(...)`. **This spec converts that wiring into a
true shadow run** (parallel write, separate field, never read by Stage 4) so
the audit's "no live history" gap (§γ) closes before any promotion.

Reuse the module surface verbatim; do not rewrite. Reference
`anthropic-skills:claude-api` for caching/retry patterns.

---

## 1. Goal

Replace the engagement-tier AI-news heuristic
(`ai_news.compute_thesis_signal_from_news`, mention-count multipliers) with a
real Claude-derived polarity per ticker per day, **but only as a SHADOW
signal**. The shadow value writes to a new `data/lthcs/llm_sentiment/`
namespace and a new variable_detail field; production Thesis sub-score
continues to flow from `thesis.compute_thesis_with_refinement` (Finnhub
recommendation base + 8-K + Yahoo earnings refinement). After 30 trading
days, compare LLM-sentiment 21d-forward Spearman IC vs Finnhub IC; promote
only if IC delta ≥ +0.03.

---

## 2. Architecture

**Trigger:** Stage 2 of `lthcs_daily.py`, gated by env flag
`LTHCS_LLM_SENTIMENT_ENABLED=1`. The current call at `lthcs_daily.py:1250`
is the integration site — but it must move out of the `_write_supplement`
path (see §7).

**Input per ticker (merged):**
1. Up to 8 most-recent Finnhub `company-news` headlines + snippets
   (`finnhub.get_company_news(sym, days=14)`).
2. Up to 4 SEC 8-K event titles from `state.sec_8k_by_ticker[sym]`.
3. AI-news mentions already in `state.ai_news_by_ticker[sym]`.
4. Company name + sector from `data/lthcs/universe.json`.

Cap merged input at `DEFAULT_MAX_NEWS_ITEMS = 10` (`llm_sentiment.py:68`);
existing `build_user_message(...)` (`:236`) ranks by HN points + recency.

**Call:** `compute_universe_llm_sentiment(...)` with `max_concurrency=5`.
System prompt cached (`ephemeral` marker at `llm_sentiment.py:188`).

**Output, two files:**
- `data/lthcs/llm_sentiment/<YYYY-MM-DD>.json` — one record per ticker,
  full dict from `compute_llm_sentiment`.
- `data/lthcs/llm_sentiment_by_ticker/<TICKER>.json` — rolling 60-entry
  history (O(ticker) backtest reads).

**Idempotency:** SHA-256 the `build_user_message` payload; skip if today's
record for this ticker has the same prompt hash.

**Stage-4 contract:** `thesis.compute_thesis_with_refinement` is **not
modified**. Shadow values plumb onto the variable_detail row as
`components.llm_sentiment_shadow_score` (0–100 mapped) and
`components.llm_sentiment_shadow_label`. Production sub_score is byte-
identical to the disabled-flag run.

---

## 3. Prompt template

**System prompt:** reuse `SYSTEM_PROMPT` at `llm_sentiment.py:85-139`
verbatim — covers LTHCS context, [-1,+1] anchors, calibration discipline,
JSON schema. `cache_control: ephemeral`; ~1.1k system tokens cache across
all 167 ticker calls per run.

**User message** (per ticker, NOT cached): built by
`build_user_message(ticker, news_items, max_news_items=10)`. Existing JSON
payload shape `{ticker, news_items[{title, snippet, source, date}],
item_count}` prefixed by `"Classify the news below ... Return JSON only."`.

**Required output** (verbatim from existing prompt):
`{mean_sentiment_score: float[-1,1], label: one of 5 bands,
polarity_confidence: float[0,1], key_drivers: [...], key_risks: [...],
rationale: str}`.

**Two upgrades** (user message only — do not touch the cached system
block):
1. Inject `company_name` + `sector` into the per-ticker JSON payload.
2. Prepend a one-line source summary (`"N Finnhub headlines, M 8-K titles,
   K AI-news mentions"`).

---

## 4. Cost model

**Default model:** switch from `claude-sonnet-4-5` to `claude-haiku-4-5`
for the shadow run. Sentiment is a structured classification with a fixed
schema; Haiku is sufficient. `LTHCS_LLM_SENTIMENT_MODEL=...` overrides.

**Per ticker:** ~1,100 system tokens (cached after call 1), ~400 user
tokens, ~120 output tokens.

**Per daily universe run (167 active tickers):** ~67,900 input tokens of
which ~182,600 are cache reads; ~20,040 output tokens.

**Haiku 4.5 pricing** ($1.00/$5.00 per MTok input/output, cache reads at
~10% of input):
- Uncached input: ~$0.068/day
- Cached reads: ~$0.018/day
- Output: ~$0.100/day
- **Total ~$0.19/day → ~$70/year.**

The audit's $0.85/day figure assumed Sonnet without caching. Sonnet +
caching is ~$0.50/day; Haiku is the recommended shadow config.

**Hard cap:** new env var `LTHCS_LLM_SENTIMENT_MAX_USD_PER_DAY` (default
`1.00`). After the Stage-2 call, sum tokens, apply pricing in a new
`_estimate_cost_usd(...)` helper. On overage, **abort the persistence
step** and log `! Stage 2: LLM sentiment cost cap hit ($X.XX > $Y.YY)`.
Production Thesis path is unaffected — the shadow file just isn't
written.

---

## 5. Comparison framework (the shadow eval)

After 30 trading days, `scripts/lthcs_compare_llm_sentiment.py` (Phase 5
follow-up, not this PR) does:

1. Build panel `(date, ticker, llm_polarity, finnhub_consensus,
   forward_21d_return)` from `data/lthcs/llm_sentiment/`,
   `recommendation_by_ticker`, and `lthcs/backtest.py:fetch_forward_returns`.
2. Compute mean cross-sectional Spearman IC of LLM polarity vs forward
   returns using `_spearman_ic` at `backtest.py:604`. Repeat for Finnhub.
3. **Promotion gate:** LLM IC > Finnhub IC + 0.03 AND LLM IC t-stat > 2.0
   AND fallback rate < 10% over the window → flip new `weights.json` flag
   `thesis.use_llm_primary=true` in a follow-up commit; Stage 4 sources
   the base from the LLM shadow file.
4. **Keep-shadow** if delta in [-0.03, +0.03]; **abandon** if LLM < Finnhub
   by ≥0.03 with t<2 (archive files; disable flag).

Matches the audit's "re-evaluate Thesis in 30 trading days" cadence
(§P2 #7).

---

## 6. Failure modes

| Mode | Behavior | Where it lives today |
| --- | --- | --- |
| API outage / 5xx | Per-ticker fallback to engagement heuristic dict marked `fallback=True, fallback_reason="api_error: …"`; pipeline continues. | `llm_sentiment.py:608-612` |
| Missing `ANTHROPIC_API_KEY` | Per-ticker fallback `fallback_reason="missing_api_key"`. | `llm_sentiment.py:579-580` |
| Missing `anthropic` SDK | Per-ticker fallback `fallback_reason="anthropic_sdk_unavailable"`. | `llm_sentiment.py:582-585` |
| Bad JSON output | Greedy curly-brace extraction → if still unparseable, fallback `fallback_reason="json_parse_error"`. | `llm_sentiment.py:333-368`, `:620-622` |
| Empty response | Fallback `fallback_reason="empty_response"`. | `llm_sentiment.py:615` |
| No news for ticker | `_neutral_no_news(...)` returns neutral non-signal; not a fallback per se. | `llm_sentiment.py:504-522` |
| Rate limit (Anthropic 429) | Currently bare `except Exception` swallows it. **Add to spec:** the implementation agent should wrap `_call_anthropic` in an exponential-backoff retry (3 attempts, 1s/4s/16s) before falling through to the fallback. Reference `anthropic-skills:claude-api` retry patterns. | New code at `llm_sentiment.py:267-290` |
| Cost cap hit | Abort persistence (do not write today's shadow file); log; the previous day's shadow file is the last good record. | New helper §4 |

The pipeline-level guarantee is unchanged: a failure in any branch never
raises out of Stage 2. Production Thesis math (Stage 4) is decoupled by
construction in this spec.

---

## 7. Files to create / modify (implementation checklist)

| File | Action | Notes |
| --- | --- | --- |
| `lthcs/sources/llm_sentiment.py` | Modify | Change `DEFAULT_MODEL` to `claude-haiku-4-5` (line 54). Add `_estimate_cost_usd(usage_dicts, model)`. Wrap `_call_anthropic` in `_call_anthropic_with_retry(...)` (1s/4s/16s exponential backoff on 429+5xx). Add `shadow_run_id` to output dict. |
| `lthcs_daily.py` | Modify | Refactor `:1250` block: decouple LLM path from `_write_supplement`. (a) Call `compute_universe_llm_sentiment` over merged news (Finnhub `company-news` + 8-K titles + ai_news), (b) write `data/lthcs/llm_sentiment/<calc_date>.json` + per-ticker rolling history, (c) stamp `components.llm_sentiment_shadow_*` on each variable_detail row. Never touch `state.rotation` or `state.recommendation_by_ticker`. Gate by `as_of is None` (no shadow on backfills — same logic as `:1233`). |
| `lthcs/pillars/thesis.py` | No change | Production math untouched. |
| `data/lthcs/weights.json` | No change (shadow). | Promotion phase: add `"thesis": {"use_llm_primary": false}` block. |
| `data/lthcs/llm_sentiment/` | Create dir | Commit JSON (mirrors `data/lthcs/sentiment/`). |
| `data/lthcs/llm_sentiment_by_ticker/` | Create dir | Rolling 60-entry history; commit. |
| `tests/lthcs/test_llm_sentiment.py` | Extend | Add: cost-cap-hit aborts persistence; retry-with-backoff fires then succeeds; per-ticker history appends + trims to 60. |
| `tests/lthcs/test_lthcs_daily_llm_shadow.py` | Create | Integration test: flag on with mock client → shadow file written; `state.recommendation_by_ticker` byte-identical to flag-off run. |
| `.github/workflows/lthcs-daily.yml` | Modify | At `:52` env block add `ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}` and `LTHCS_LLM_SENTIMENT_ENABLED: "0"`. User flips to `"1"` after Day 0 ships. |
| `README_LTHCS.md` | Modify | After §Troubleshooting, add: missing `ANTHROPIC_API_KEY` → shadow skipped, production Thesis unaffected. Add `ANTHROPIC_API_KEY` to §2 API key table (Phase 5 note). |
| `scripts/lthcs_compare_llm_sentiment.py` | Create (Phase 5 follow-up) | IC comparison harness per §5. |

Reference `anthropic-skills:claude-api` for: prompt-caching beta-header
currency, 429/5xx retry templates, token-counting/cost-estimation helpers,
post-Haiku 4.5 model-id changes.

---

## 8. Rollout plan

**Day 0** — Ship with `LTHCS_LLM_SENTIMENT_ENABLED=0`. Tests green.
Snapshots byte-identical to pre-change runs.

**Day 1** — User adds `ANTHROPIC_API_KEY` repo secret, flips env to `1`,
pushes. First nightly writes `data/lthcs/llm_sentiment/2026-05-19.json`.

**Days 1–7** — Monitor: daily cost (`$0.XX / cap $1.00`); cache hit rate
(target `cached_input_tokens / input_tokens > 0.6`); fallback rate (<5%).

**Days 8–30** — Shadow accumulates. No production-side changes.

**Day 31** — Run `scripts/lthcs_compare_llm_sentiment.py`; apply the §5
gate.

**Day 31+** — Three branches:
1. **Promote.** Flip `weights.json` `thesis.use_llm_primary=true`; Stage 4
   sources base from LLM file with Finnhub fallback. Audit item γ closes;
   `elite.min` recalibration becomes actionable.
2. **Keep shadow.** Re-evaluate in another 30 days.
3. **Abandon.** Disable env flag; archive shadow files under `.archived/`;
   module stays behind the flag. Cost → $0.

---

## 9. Cross-references

- Cost / caching patterns — `anthropic-skills:claude-api`
- Audit context — `docs/lthcs-data-audit-2026-05-18.md` (§γ, §P2 #7, §279
  the "next-step" note)
- Current LLM module — `lthcs/sources/llm_sentiment.py:1-718`
- Current wiring (replace, not shadow) — `lthcs_daily.py:1250-1265`
- Production Thesis math (untouched) —
  `lthcs/pillars/thesis.py:563-751` (`compute_thesis_with_refinement`)
- IC computation helper — `lthcs/backtest.py:604` (`_spearman_ic`)
- Sister LLM module (narratives, follows same patterns) —
  `lthcs/narratives_llm.py:1-50`
- Open-items audit row — `docs/lthcs-open-items-audit.md:267`,
  `:287` ("Suggested next 3 commits" #2)
