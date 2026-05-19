# LTHCS Tier 5 #23 — LLM-Generated Narratives (design spec)

**Status:** design only. No code in this PR.
**Audit reference:** `docs/lthcs-open-items-audit.md` row 23 ("Replace
templated narratives with LLM-generated"), effort tag **M-L**.
**Canonical Anthropic pattern:** `docs/lthcs-llm-sentiment-shadow-spec.md`
(commit `7732d9e`). Sentiment shadow shipped at `37199d7` and is the
template for env-flag-gated, prompt-cached, fallback-on-failure LLM
features in LTHCS.

---

## 0. Pre-condition (verified)

`lthcs/narratives_llm.py` **already exists (607 lines, not a stub).**
The module wires the Anthropic SDK with prompt caching, a long system
prompt, `_summarize_*` helpers for insider / 13F / macro context, a
fallback path to `lthcs.narratives.generate_narratives`, and a
thread-pool universe runner. Wired at `lthcs_daily.py:1750` behind
`LTHCS_NARRATIVES_LLM_ENABLED`, `DEFAULT_MODEL = "claude-sonnet-4-5"`.

Gaps vs. the LLM-sentiment shadow contract:

1. No **shadow write path** — today the LLM result *replaces*
   `state.narrative_rows`; no parallel `data/lthcs/narratives_llm/`.
2. No **cost cap** — `_estimate_cost_usd` exists in
   `llm_sentiment.py:857`; narratives_llm has no equivalent and no
   `LTHCS_LLM_NARRATIVES_MAX_USD_PER_DAY`.
3. No **retry-with-backoff** — 429/5xx falls straight through to
   per-ticker fallback.
4. **4-section output shape mismatch** — module returns a single
   `narrative` blob plus the four templated keys only on fallback. UI
   and `persist.write_narratives` expect V1 keys (`todays_take`,
   `why_changed`, `why_not_to_sell`, `what_would_break`,
   `confidence_level`) always.
5. **Default model is Sonnet**, not Haiku.
6. No **UI toggle** to read a shadow file.
7. No **tests** for the LLM narrative path.

**Reuse from LLM-sentiment shadow infrastructure: ~85%.** Anthropic
client construction, SDK detection, caching headers, fallback pattern,
env-flag pattern, thread-pool concurrency, `_estimate_cost_usd` pricing
table, `_max_usd_per_day` reader, retry helper (once added per
sentiment §7), and rolling-history persistence are all reusable. Only
the prompt body, per-ticker payload, and output validator are
narrative-specific.

---

## 1. Current state

Stage 7 of `lthcs_daily.py` runs `narratives.generate_narratives(...)`
for every snapshot row. Output is the canonical 4-section narrative:
`todays_take` (composite read), `why_changed` (drift explanation),
`why_not_to_sell` (bottom-pillar callout), `what_would_break` (band-down
threshold). Templates pull vocabulary from `BAND_DESCRIPTORS` and
substitute numbers from the snapshot row. Sample:
`data/lthcs/narratives/2026-05-18.json`.

Limitations the audit (row 23) flags:

- Cannot weave cross-pillar context (e.g. "Adoption weak but
  Institutional buying the dip — rotation pause, not thesis break").
- Cannot reference `data_quality` flags inline (e.g. "Thesis is
  Finnhub-only today; 8-K hasn't fired in 14 days").
- Cannot compare to historical state ("lowest composite since
  2026-03-15").
- Cannot acknowledge the **dragging-pillar** finding from `014aadc`.

---

## 2. Goal

Ship an LLM-generated 4-section narrative as a **shadow** alongside the
templated narrative. Same shape, same file format, same UI consumer —
just a separate path on disk and a toggle to flip between them.

The LLM has access to:

- All five pillar sub-scores + top components per pillar (from
  `variable_detail_rows`).
- All `data_quality` flags per pillar (`has_insider`, `has_qoq`,
  `has_sector_rss`, etc.).
- Today's composite + drift_1d / drift_7d / drift_30d.
- The dragging-pillar finding (lowest sub-score; the `_binding` half of
  the existing `_binding_and_supporting` helper).
- The pillar evidence (top components per pillar from
  `variable_detail`).

Output requirements:

- Specific values, not generic adjectives.
- Inline acknowledgement of data quality gaps when material.
- Comparison to recent state when material (lowest-since,
  highest-since, drift > 1σ, etc.).

---

## 3. Architecture

**Trigger:** new Stage **7.5b** in `lthcs_daily.py`, gated by
`LTHCS_LLM_NARRATIVES_ENABLED=1`. Stage 7 keeps producing the templated
narratives as today (production path). Stage 7.5b runs *after* Stage 7
using `state.narrative_rows` (templated) as the fallback source.

**Contract change:** rename env flag `LTHCS_NARRATIVES_LLM_ENABLED` →
`LTHCS_LLM_NARRATIVES_ENABLED` to match the sentiment shadow naming.
Both `lthcs_daily.py:1752` and `narratives_llm.py:51` change in
lockstep; old name kept as a synonym for one release with a
DeprecationWarning.

**Inputs per ticker (already plumbed):** snapshot row,
`variable_detail_rows[ticker]`, `state.insider_by_ticker[sym]`,
`state.holdings_by_ticker[sym]`, `state.breadth_snapshot`.

**Output, two files:**

- `data/lthcs/narratives/<calc_date>.json` — **templated, unchanged.**
  Production reads this by default.
- `data/lthcs/narratives_llm/<calc_date>.json` — **new.** Same JSON
  shape (`{calc_date, model_version, narratives: [...]}`), 167 entries,
  four-section keys filled by the LLM.

**Idempotency:** SHA-256 the per-ticker user message; skip the call if
yesterday's shadow record has the same prompt hash.

**Stage-8 contract:** `persist.write_narratives` unchanged. Add a
sibling `persist.write_narratives_llm(out_dir, calc_date, llm_rows)`.
UI sources `narratives/<date>.json` by default; behind a `localStorage`
flag it sources `narratives_llm/<date>.json` (see §8).

---

## 4. Prompt template

**System prompt:** reuse `SYSTEM_PROMPT` at `narratives_llm.py:64-113`
verbatim. Already cached via `cache_control: ephemeral` in
`build_system_blocks`. ~1.4k system tokens cache across all 167 calls.

**Two upgrades to the system prompt** (additive; cacheable as the new
prefix on day-1):

1. Replace the "one paragraph 80-150 words" instruction with the
   four-section spec:

   > Return JSON with five keys:
   > `{section_1_todays_take, section_2_why_changed,
   > section_3_why_not_to_sell, section_4_what_would_break,
   > confidence_level: "high"|"medium"|"low"}`.
   > Each section is 2-3 sentences. Section 1: composite read (band +
   > score + drift + supporting pillar). Section 2: what changed since
   > yesterday (cite specific component deltas). Section 3:
   > bottom-pillar callout (cite specific component values and data
   > quality gaps). Section 4: what would break the thesis (cite the
   > next band-down threshold and the structural-review threshold).
   > Reference specific numbers and `data_quality` flags. Don't use
   > generic adjectives like "strong" or "weak" without grounding them
   > in a metric.

2. Add a tone guideline: "Factual, analytical, no hype. American
   English. No emojis, no exclamations. Match the tone of an analyst
   desk note."

**User message** (per ticker, NOT cached): existing
`build_user_message(...)` returns a JSON payload with subscores,
binding/supporting, pillar components, insider, holdings. **Upgrade:**
also pass `drift_7d`, `data_quality_by_pillar`, and the prior-day
composite + subscores so the LLM can write the "why_changed" section
honestly. Module already has the inputs in `snapshot_row` — extend the
payload, do not change the call signature.

---

## 5. Cost model

**Default model:** switch from `claude-sonnet-4-5` to
`claude-haiku-4-5`. Narratives are a constrained-shape generation task;
Haiku is sufficient. `LTHCS_LLM_NARRATIVES_MODEL=...` overrides.

**Per ticker:** ~1,400 system tokens (cached after call 1), ~600 user
tokens, ~300 output tokens (4 sections × ~75 tokens each).

**Per daily universe run (167 active tickers):** ~100k uncached input
tokens (first call + per-ticker user blocks), ~234k cache reads, ~50k
output tokens.

**Haiku 4.5 pricing** ($1.00 / $5.00 per MTok input/output, cache reads
at ~10% of input):

- Uncached input: ~$0.040/day
- Cached reads: ~$0.023/day
- Output: ~$0.250/day
- **Total ~$0.31/day → ~$115/year.**

(Higher than sentiment because narratives produce ~2.5× the output
tokens per ticker. Still well under any meaningful threshold.)

**Hard cap:** new env var `LTHCS_LLM_NARRATIVES_MAX_USD_PER_DAY`
(default **`2.00`**). After the Stage-7.5b call, sum tokens, apply
pricing via the `_estimate_cost_usd` helper already in
`llm_sentiment.py:857`. Either:

- **Option A (preferred):** extract the helper to
  `lthcs/sources/_anthropic_cost.py` and import from both modules. One
  pricing table to update post Haiku 4.6.
- **Option B:** duplicate the helper inside `narratives_llm.py`. Faster
  to ship; one more place to forget when pricing changes.

On overage, **abort the shadow persistence step** and log
`! Stage 7.5b: LLM narrative cost cap hit ($X.XX > $Y.YY)`. Templated
narratives (Stage 7) are unaffected.

---

## 6. Comparison framework (shadow eval)

Unlike LLM sentiment (numeric IC-promotable signal), **LLM narratives
are a UX improvement, not a scoring change.** No IC test gates
promotion; the decision is qualitative.

**Chosen approach:** ship behind a user-facing toggle in the About
modal. Default off. Collect ad-hoc feedback for 30 days; flip the
default if positive, else hold indefinitely as opt-in.

**Rejected:** A/B with telemetry — no analytics layer exists, out of
scope.

**Light-touch automated check:** `scripts/lthcs_diff_narratives.py`
emits a markdown diff of 20 sampled tickers per run to
`data/lthcs/narratives_diff/<calc_date>.md`. Used for tone-drift
spotting during the first 14 days, then archived. Not a promotion
gate.

---

## 7. Failure modes

| Mode | Behavior | Where it lives today |
| --- | --- | --- |
| API outage / 5xx | Per-ticker fallback to templated narrative (existing path), `fallback=True, fallback_reason="api_error: …"`. Shadow file still written. | `narratives_llm.py:520-522` |
| Missing `ANTHROPIC_API_KEY` | Fallback `fallback_reason="missing_api_key"`. Shadow file written with all-templated entries (file shape preserved for the UI). | `narratives_llm.py:487-489` |
| Missing `anthropic` SDK | Fallback `fallback_reason="anthropic_sdk_unavailable"`. | `narratives_llm.py:490-492` |
| Bad JSON output | Per-ticker JSON parse; on failure, fallback to templated for that ticker, mark `fallback_reason="json_parse_error"`. (New; sentiment spec §6.) | New code at `narratives_llm.py:524` |
| Empty response | Fallback `fallback_reason="empty_response"`. | `narratives_llm.py:525-526` |
| Rate limit (429) | Wrap `_call_anthropic` in exponential backoff (3 attempts, 1s/4s/16s) — same helper as the sentiment-spec §6 ask. Reuse if extracted to `_anthropic_cost.py` (or sibling `_anthropic_retry.py`). | New code at `narratives_llm.py:347` |
| Cost cap hit | Abort shadow persistence; templated path is the user-visible default anyway. | New helper §5 |
| Tone drift | System-prompt tone guideline §4 + sampled markdown diff §6. | New |

Pipeline guarantee unchanged: failures never raise out of Stage 7.5b;
templated narratives (Stage 7) are decoupled by construction.

---

## 8. UI integration

The detail modal (`lthcs_tab/lthcs-detail.js`) currently fetches the
narrative for the active ticker from `data/lthcs/narratives/<date>.json`.

**Add:**

- A toggle in the About modal (`lthcs_tab/lthcs-about.js`) labeled
  "Use LLM-generated narratives (shadow)". Stored in
  `localStorage.lthcs_llm_narratives`.
- In `lthcs-detail.js`, branch on the localStorage flag: if set, fetch
  `narratives_llm/<date>.json`; otherwise the existing path. Fall back
  to templated path if the shadow file 404s (covers backfill dates and
  cost-cap-aborted days).
- A small "LLM" badge in the narrative card header when the LLM path is
  active, so users know what they are reading.

No change to the public V1 path. V1 dashboard never touches narratives.

---

## 9. Files to create / modify

| File | Action | Notes |
| --- | --- | --- |
| `lthcs/narratives_llm.py` | Modify | Rename env flag (with synonym), `DEFAULT_MODEL = "claude-haiku-4-5"`, upgrade `SYSTEM_PROMPT` for four-section JSON output (§4), add `_parse_four_section_json` validator, wrap `_call_anthropic` in retry-with-backoff, add `_estimate_cost_usd` (or import from shared helper §5A), stamp `shadow_run_id`. |
| `lthcs/sources/_anthropic_cost.py` | Create (§5 Option A) | Extract pricing table + `_estimate_cost_usd` + retry helper. Imported by both `llm_sentiment.py` and `narratives_llm.py`. |
| `lthcs_daily.py` | Modify | Move LLM block out of Stage 7 (Stage 7 always templated). Add Stage 7.5b: gated by flag, calls `generate_universe_narratives`, writes shadow file, cost cap, skip on `as_of is None` backfills. |
| `lthcs/persist.py` | Modify | Add `write_narratives_llm(out_dir, calc_date, llm_rows)`. |
| `data/lthcs/narratives_llm/` | Create dir | Commit `.gitkeep`. |
| `lthcs_tab/lthcs-detail.js` | Modify | Toggle wiring + shadow-file fetch + 404 fallback + "LLM" badge. |
| `lthcs_tab/lthcs-about.js` | Modify | Toggle UI; `localStorage.lthcs_llm_narratives`. |
| `tests/lthcs/test_narratives_llm.py` | Create | Mock client; cost-cap aborts persistence; retry fires then succeeds on 429; bad JSON triggers per-ticker fallback; shadow shape matches templated. |
| `tests/lthcs/test_lthcs_daily_llm_narratives_shadow.py` | Create | Flag on with mock → shadow written; `state.narrative_rows` byte-identical to flag-off run. |
| `.github/workflows/lthcs-daily.yml` | Modify | Add `LTHCS_LLM_NARRATIVES_ENABLED: "0"`. `ANTHROPIC_API_KEY` already wired per sentiment rollout. |
| `README_LTHCS.md` | Modify | Degradation matrix entry for shadow 404; cost notes per §5. |
| `scripts/lthcs_diff_narratives.py` | Create | 20-ticker diff per §6. |

Reference `anthropic-skills:claude-api` for caching beta-header,
429/5xx retry templates, post-Haiku 4.5 model-id changes.

---

## 10. Phasing

- **Phase 1 (M):** Module hardening + Stage 7.5b + shadow persistence +
  cost cap + retry + tests + flag wired to `"0"`. Production
  byte-identical to today.
- **Phase 2 (S):** UI toggle. Default off.
- **Phase 3 (S, optional):** Diff harness; drop after 14 days if tone
  stable.
- **Phase 4 (one-line decision):** flip localStorage default to "on"
  if feedback positive, else hold indefinitely as opt-in.

---

## 11. Open questions

1. V1 key names (`todays_take`, etc.) vs spec-aligned
   (`section_1_todays_take`)? **Keep V1 keys** so UI is a one-line path
   branch, not a two-shape adapter.
2. "Regenerate narrative" button in detail modal? Out of scope —
   requires a client API key or server endpoint, neither exist.
3. i18n. English-only today; the LLM could produce Spanish/Mandarin by
   appending a target language to the user message. Future spec.
4. Diff harness auto-promote on N consecutive low-drift days? **No.**
   Promotion is manual per §6.
5. Cost cap $2.00/day vs $1.00/day (sentiment default): $2.00 chosen
   because narratives produce ~2.5× more output tokens; revisit after
   7 days of cache-hit telemetry.

---

## 12. Cross-references

- Canonical Anthropic pattern — `docs/lthcs-llm-sentiment-shadow-spec.md`
  (commit `7732d9e`)
- Sentiment shadow ship commit — `37199d7`
- Detail modal dragging-pillar finding — `014aadc`
- Existing partial implementation — `lthcs/narratives_llm.py:1-607`
- Existing wiring (replace-not-shadow) — `lthcs_daily.py:1750-1804`
- Templated narrative renderer — `lthcs/narratives.py:1-349`
- Sample current output — `data/lthcs/narratives/2026-05-18.json`
- Cost helper to extract — `lthcs/sources/llm_sentiment.py:857`
- Audit row — `docs/lthcs-open-items-audit.md:426`
