# LTHCS Per-Pillar Quality Audit — 2026-05-19

**Scope:** Phase 3 task 3.1 — read-only signal quality audit of the 5 LTHCS pillars on current production data. Source snapshot: `data/lthcs/snapshots/2026-05-18.json` (model v1.1.0, n=167 tickers; the 2026-05-19 cron has not yet completed at audit time). Stability window: 30 snapshots (2026-04-19 → 2026-05-18). Raw numbers live at `data/lthcs/quality_audit/2026-05-19_pillar_quality.md`.

## Verdict summary

| Pillar | Verdict | Coverage | Today stdev | 30d per-ticker median stdev | Key concern |
| --- | --- | --- | --- | --- | --- |
| adoption_momentum | HEALTHY | 96.4% | 25.39 | 1.35 | Post-fa926bf overhaul looks well-spread; flatness over 30d suggests new daily-nudge cron hasn't yet had time to differentiate. |
| institutional_confidence | HEALTHY | 100.0% | 28.15 | 4.86 | Widest spread of any pillar (good); one stuck-at-floor cohort (`pre_profit_growth` mean 0.0) is a 1-ticker LCID artifact, not a broken signal. |
| financial_evolution | HEALTHY | 97.0% | 17.72 | 0.35 | XBRL margin chain produces a tight, well-ordered distribution; near-zero day-to-day movement is expected (filings are quarterly). |
| thesis_integrity | DEGRADED (transitional) | 86.8% | 7.99 | 1.79 | Pre-Finnhub-migration snapshot. p5..p95 span is only 30.5pts and 5 tickers tie at the bottom (41.2) — classic stub-cluster. Re-audit after 2026-05-20 23:00 UTC run. |
| des | DEGRADED | 100.0% | 9.94 | 1.45 | p25..p75 IQR = 4.0pts (43.5 → 46.5) — the pillar barely differentiates within the middle half of the universe. Top-5 all tie at 71.6 (financial-services group artifact). |

Legend: HEALTHY = real signal, well-spread, differentiates cohorts. DEGRADED = signal present but compressed / clustered / stale. STUB = uniformly default-50 or single-value.

## adoption_momentum — HEALTHY

- Coverage 96.4%, mean 51.64, stdev 25.39, p5/p95 = 13.3 / 94.9.
- 4 tickers at ceiling (PDD, NVDA, MELI, AVGO), 0 at floor; bottom-5 (PFE 1.8, MO 4.4, DE 4.7, TSLA 5.4, HD 6.3) reads correctly — declining/mature names.
- Cohort means differentiate: `pre_profit_growth` 70.0 > `growth_compounder` 57.56 > `mature_compounder` 50.2 > `recovery_stabilization` 47.0. Direction matches intuition.
- 30d median per-ticker stdev only 1.35 → very stable. Acceptable for now since the post-overhaul `adoption_trends_daily` nudge cron only landed recently; broader movement expected by ~2026-05-26.

## institutional_confidence — HEALTHY

- Coverage 100%, mean 48.74, stdev 28.15 — the widest distribution of any pillar.
- Top-5 INTC 97.0, MU 96.4, AMD 95.8, MRVL 95.2, CSCO 95.2 are all semis/networking — coherent with Form 4 + 13F live signal.
- One negative value: TEAM=-2.8 (likely a clamp miss; flag for FF's outlier sweep — out of scope here).
- `pre_profit_growth` cohort mean 0.0 is a single ticker (LCID at 0.0); not a broken pillar.
- 30d stdev median 4.86 — moves meaningfully week-over-week. Good.

## financial_evolution — HEALTHY

- Coverage 97%, mean 55.79, stdev 17.72, p5/p95 = 25.6 / 83.9.
- No floor/ceiling pileups. Top-5 dominated by chip/telecom margin compounders (MU, TMUS, ADI, BLK, ASML); bottom by banks and capital-intensive industrials (USB, WBD, WFC, GM, DE).
- 30d median stdev 0.35 is the lowest of any pillar, which is **correct** — XBRL margin signal moves on quarterly filings, not daily snapshots.

## thesis_integrity — DEGRADED (transitional)

- Coverage 86.8%, mean 56.3, **stdev only 7.99**, p5..p95 span 45.0 → 75.5.
- 5 tickers tie at the bottom value 41.2 (CCEP, NOW, ON, PDD, SPG) — classic sentinel/stub cluster from the legacy Alpha Vantage NEWS_SENTIMENT AND-not-OR behaviour.
- **This audit predates 10daa39** (Finnhub /news-sentiment shipped 2026-05-19). First production run with Finnhub data is 2026-05-20 23:00 UTC. Re-run this audit afterward; expected to either return to HEALTHY or surface a new failure mode.
- LLM shadows (sentiment + narratives) fire for the first time tonight; not integrated into production scores yet — flag for Phase 4 integration decision.

## des — DEGRADED

- Coverage 100% (synthetic), but **distribution is compressed**: p25..p75 spans only 4.0pts (43.5 → 46.5) and the median per-cohort spread is ~6pts.
- Top-5 tied at 71.6 (WFC, V, USB, TRV, SCHW) — financial-services group all sharing the same sector_des_weights output. Bottom-5 are REITs/utilities all near 33–34.
- The pillar appears to be acting more like a **sector multiplier than a per-ticker signal**. In its current form it adds little ticker-level discrimination beyond the sector tag.
- Stability looks reasonable (30d median stdev 1.45), but that's stability of a near-flat signal.
- Recommendation (for GG / weights team — not implemented here): consider whether DES should keep its 20% equal weight or be down-weighted until per-ticker macro/sector-rotation inputs are richer.

## Cross-pillar observations (non-correlation; FF owns full matrix)

- Visually, `financial_evolution` and `adoption_momentum` look independent (different ranking at top and bottom). Good.
- `thesis_integrity` is not yet cloning anything — its narrow band is a stub artifact, not a correlation issue.
- `des` and sector classification likely co-move; FF should confirm.

## Specific concerns / asks

1. **Thesis re-audit on 2026-05-21** after first Finnhub-driven snapshot. If the 41.2-cluster persists, the migration didn't help; if mean stdev jumps above ~15, we're good.
2. **DES discrimination is weak.** Either enrich sub-pillars or accept it as a sector tilt. Flag to GG for weight rebalance discussion.
3. **TEAM institutional_confidence = -2.8** — out-of-range; should be clamped to [0,100]. (Not fixed here; sibling FF is doing the outlier sweep.)
4. **Adoption Momentum** is post-overhaul-healthy but **30d stability is artificial** — the daily nudge cron hasn't accumulated enough runs yet. Re-check on 2026-05-26.
5. **LLM shadow integration**: sentiment + narratives shadows fire 2026-05-19 23:00 UTC for the first time. Not yet in any production score; recommend a Phase 4 "shadow vs prod" comparison once ≥ 7 shadow runs exist.

## Reproducing

```
.venv/bin/python scripts/lthcs_pillar_quality_audit.py \
  --asof 2026-05-18 \
  --out data/lthcs/quality_audit/2026-05-19_pillar_quality.md
```

Tests: `pytest tests/lthcs/test_pillar_quality_audit.py`.
