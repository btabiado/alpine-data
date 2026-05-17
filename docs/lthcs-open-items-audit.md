# LTHCS Open Items — Consolidated Audit

Snapshot: 2026-05-17, model `v1.1.0`, commit `b64d9f7`.

Every open item across all research docs, recommendations, and known
limitations — categorized by priority, effort, and status.

---

## Audits already produced (research only — no model changes)

| Doc | Scope | Status |
|---|---|---|
| `docs/lthcs-ux-research.md` | Survey of 9 dashboards, recommends heatmap + table + movers | Heatmap ✅ shipped at `/lthcs/heatmap/`; table + movers ❌ |
| `docs/des-analysis.md` | DES underweight diagnosis + 6 ranked fixes | Option B + D ✅ shipped (sector softening + AI overrides) |
| `docs/des-audit-framework.md` | Macro signal inventory; what's wired vs. stub | 3 of 3 HIGH-priority gaps now ✅ wired (real_10y / VIX / M2) |
| `docs/peer-group-audit.md` | Every cross-ticker comparison + ranked fixes | Maturity-stage split ✅ shipped (v1.1.0); compound key ❌ |
| `docs/lthcs-tuning-kit.md` | Symptom-to-lever playbook + tune-preview script | Tooling ✅; specific tunings ❌ deferred to as-needed |
| `docs/lthcs-diagnostic-runbook.md` | How to diagnose surprise scores | Tool `scripts/lthcs_diagnose.py` ✅ |
| `docs/news-feeds-general-apis.md` | 13 free APIs evaluated; top 3 = Finnhub / Yahoo / MarketAux | All 3 ❌ not yet wired |
| `docs/news-feeds-sector-specific.md` | Per-sector RSS feeds; top 3 = FDA / EIA / Fed | All 3 ❌ not yet wired |
| `docs/news-feeds-earnings-events.md` | Event-driven sources; top 3 = 8-K / yfinance reco / Finnhub earnings | All 3 ❌ not yet wired |
| `docs/lthcs-followups-queue.md` | Original 5-item queue from Bryan's note | All 5 ✅ shipped |

**No outstanding research items.** All four queue items have research docs.

---

## TIER 1 — Open items with the highest ROI and shovel-ready specs

These are the "wire it next" items. All have concrete designs in their
respective research docs.

| # | Item | Doc | Effort | Predicted impact |
|---|---|---|---|---|
| 1 | **Finnhub news + sentiment** | news-feeds-general-apis §2.1 | M (~1 swarm) | Coverage 47/167 → ~155/167. Bullish/bearish % per ticker. Kills the AV rotation logic. |
| 2 | **SEC 8-K material event filter** | news-feeds-earnings-events §3 | S (~½ swarm) | 100% universe coverage on event days. Items 1.01/2.02/5.02/8.01. Already have SEC EDGAR access. |
| 3 | **yfinance earnings_dates + recommendations** | news-feeds-earnings-events §1.1, §2.1 | S (~½ swarm) | Earnings beats/misses + analyst actions for entire universe. Already pulling Yahoo for prices. |
| 4 | **FDA Press Announcements RSS** | news-feeds-sector-specific §2.1 | M | Event-driven Thesis lift for ~15 pharma names. Highest signal-to-noise per the audit. |
| 5 | **EIA "Today in Energy" + Fed press-release RSS** | news-feeds-sector-specific §2.2, §2.4 | S | Sector signal for ~30 energy + financials names. |
| 6 | **AI-news threshold polish** | (this audit) | XS | Final pass: keep `+0.60` for top-engagement; consider weighting by mention count so very-frequently-mentioned names cross 80. |

**If items 1+2+3 shipped together**: probably 8-12 names move from
Constructive → High Confidence given more honest sentiment data;
event-driven signal makes the framework feel "live" between Thesis
rotations.

---

## TIER 2 — Open items needing more design before build

| # | Item | Source | Effort | Why deferred |
|---|---|---|---|---|
| 7 | **Compound peer-group key** `(maturity_stage, sector_group)` | peer-group-audit §3.4 | M | Naive version makes AAPL worse (13.2 vs 46.8 inside Tech-compounder bimodal cohort). Needs a curated Hardware/Software split first. |
| 8 | **De-dup `Technology` ↔ `Information Technology`** in sector_des_weights.json | des-audit §6 | XS | Currently a comment only; the duplicated values are a drift risk on next retune. 5-min fix but needs care. |
| 9 | **Tier 2 macro signals**: Brent crude, gasoline cracks, ISM PMI, housing starts, consumer confidence, U-6 | des-audit | M-L | Lower marginal value than the 3 we shipped today. Build only when DES re-tilts. |
| 10 | **`peer_groups.json`** config file (declarative per-pillar peer-group strategy) | peer-group-audit §3.5 | L | Architecturally cleaner than hard-coded grouping in lthcs_daily.py but premature for V1 universe size. |
| 11 | **Volatility modifier → `modifiers.json`** | tuning-kit §4 | S | Decouple from code constants so it's tunable. Optional. |
| 12 | **`growth_compounder` weight retune** (currently 0.30/0.20/0.10/0.20/0.20) | tuning-kit + peer-group-audit | XS | The 0.30 Adoption weight amplified the AVGO/META drag pre-reclassification; may be too aggressive. Worth re-evaluating now that cohort changed. |

---

## TIER 3 — Phase 2 stub-replacement (real-data wires for currently-stubbed pillar components)

| # | Component | Pillar | Currently | Phase 2 plan |
|---|---|---|---|---|
| 13 | **13F institutional holdings** | Institutional | Stubbed (renormed). Momentum carries 100%. | Aggregate 13F filings across institutions per ticker; quarterly cadence. Genuine implementation work (~2-3 swarms). |
| 14 | **Google Trends acceleration** | Adoption | Renorms; revenue carries 100%. | pytrends is rate-limited so daily 168-ticker pulls don't work. Phase 2: do an offline weekly batch, cache, run during pipeline. |
| 15 | **Bank-specific revenue growth peer cohort** | Financial | Banks compete with all compounders on revenue % rank | Add `bank` peer group OR use NII growth percentile within bank cohort specifically. JPM revenue +2-3% YoY shouldn't be benchmarked against NVDA +65%. |
| 16 | **Sector-relative momentum for Institutional** | Institutional | Universe-relative | Peer-group audit argued KEEP universe-relative; flagged as not a fix. Re-evaluate if signal feels off. |

---

## TIER 4 — UX / dashboard layer

| # | Item | Source | Effort |
|---|---|---|---|
| 17 | **Sortable Bloomberg-style table view** at `/lthcs/table/` | ux-research §4.2 | M |
| 18 | **"Movers" leaderboard strip** (top-10 gainers + losers by drift) | ux-research §4.3 | S |
| 19 | **Detail modal: expand narrative + variable-detail evidence** | (this audit) | S — already 90% there |
| 20 | **Time-series chart on detail modal** showing composite history | (this audit) | M — sparkline exists; full chart is bigger |
| 21 | **About-modal updates** with current data-feed lineage (which sources feed which pillar) | (this audit) | XS |
| 22 | **Mobile/Safari testing pass** | ux-research | S — heatmap was tested; main tab probably needs one too |

---

## TIER 5 — V2/V3 framework changes (model-shape, not just config)

| # | Item | Why | Effort |
|---|---|---|---|
| 23 | **Replace templated narratives with LLM-generated** | V1 narratives are sentence templates; LLM would weave in actual data quality flags + cross-pillar context. | M-L |
| 24 | **Backtest engine** | Score history → P&L attribution to validate the framework | L |
| 25 | **Adaptive weights** (V2) | Use backtest to suggest per-ticker weight adjustments | XL (depends on 24) |
| 26 | **MCP server / API exposure** | LTHCS data as Claude Connector | M |
| 27 | **Crypto pillar adapter** | Score BTC/ETH/SOL in the same framework | M-L |
| 28 | **Real LLM-derived sentiment** (replace AI-news engagement heuristic) | Engagement ≠ sentiment direction; Claude call per ticker per day could give real polarity | M; cheap with prompt caching |

---

## TIER 6 — Known data outages / blocked

| # | Item | Status |
|---|---|---|
| 29 | **WBA inactive** | Walgreens taken private 2025; permanently inactive in universe. |
| 30 | **Reddit OAuth blocked** | Bryan can't register; defer indefinitely per `reddit_oauth_blocked` memory. |
| 31 | **AV NEWS_SENTIMENT free tier rate limit** | ~5-7 calls/day in practice (docs claim 25). Drives the rotation design. |
| 32 | **pytrends rate-limited** | Why Google Trends is stubbed in Adoption. |

---

## Suggested next 3 commits

If pushing forward, the cleanest sequencing:

1. **Items 1 + 8 + 12** — Wire Finnhub for real per-ticker sentiment (replaces the AI-news heuristic for the AI cohort and covers everyone else); fix the alias drift risk; retune growth_compounder weights now that cohort changed.
2. **Items 2 + 3** — SEC 8-K event filter + yfinance earnings/recommendations. Adds event-driven signal across the universe. Cheap.
3. **Items 4 + 5** — Sector-specific RSS (FDA + EIA + Fed). Pharma + energy + financials get event-driven signal.

After those 3, ~70% of the universe has substantively better data than V1 ship. Items in Tier 2-5 become "what do you want to tune next" rather than "what's broken."

---

## Outstanding from the diagnostic tool's perspective

Run `python scripts/lthcs_diagnose.py AAPL INTC NVDA LLY JPM` to see which
items in this audit are causing each ticker's current sub-pillar drag.
The tool labels each pillar as REAL / PARTIAL / STUB / NEUTRAL / MISSING
so you can map a ticker's score directly to the audit items that gate
the next composite move.
