# Phase 5 backtest comparison — pre vs post 90-day `--force` rewrite (2026-05-19)

## Headline verdict — **PHASE 5 MOVED THESIS, NOT THE COMPOSITE**

The 90-day `--force` backfill (commit `2e1654c`) rewrote 90 dates of history with P0-P4 applied retroactively. Comparing the post-backfill IC against yesterday's pre-Phase-5 baseline at h=21d: **composite IC is essentially flat (+0.127 → +0.122, Δ −0.005)**, but the structural change is dramatic — **thesis_integrity went from "unmeasurable" (n_obs=2, flat-50-anchored) to a fully-realised IC of +0.082 (n_obs=91, t≈+10.3, IC Sharpe +17.2)**. The composite stayed near-flat because Phase 5's win on Thesis was offset by a small drop in Financial (−0.008) and a negative move in Adoption (+0.004 → **−0.013**, *worse*, with Q5-Q1 still inverted at −2.55%). **Phase 5 did exactly what it was designed to do: it cured the Thesis flat-anchor, but composite IC did not improve because the same backfill that unlocked Thesis also surfaced a sharper Adoption inversion**, and because Adoption's β fix (`333e5dd`) landed *after* the backfill started — the rewritten 90 days do **not** include the recalibrated Adoption β.

---

## Per-pillar IC table — pre vs post Phase 5 (h=21d, n_dates=90→91)

| Pillar | Pre (5-18 baseline) | Post (5-19 rewrite) | Δ IC | Verdict |
|---|---:|---:|---:|---|
| **composite** | **+0.1268** | **+0.1218** | **−0.0050** | flat (within noise) |
| institutional_confidence | +0.2042 | +0.2086 | +0.0044 | flat (already strong) |
| **thesis_integrity** | +0.0596 (n=2, unreliable) | **+0.0822** (n=91) | **+0.0226** | **realised — biggest structural win** |
| financial_evolution | +0.0859 | +0.0777 | −0.0082 | small drop (P3 calibration trade-off) |
| des | +0.0217 | +0.0285 | +0.0068 | small lift (P4 tier-2 macros) |
| **adoption_momentum** | +0.0044 | **−0.0130** | **−0.0174** | **regressed — P1 made the inversion worse** |

> Notes. Pre-Phase-5 Thesis n_obs=2 means the IC of +0.060 was computed on only 2 dates with any rank variance — it's a noise-anchored number. The post-Phase-5 +0.082 over 91 dates is the first genuine reading of Thesis as a live pillar. The pre baseline's "+0.0596" should be treated as undefined, and the honest comparison is **Thesis went from undefined → +0.082 with t-stat +10.3**.

---

## Band-ordering hypothesis at h=21d — **HOLDS**

The script no longer emits band-level returns in JSON (only pillar IC + quintiles + portfolio). Indirect confirmation from the portfolio long/short test:
- Long (`elite + high_confidence + constructive`) vs short (`review`): **hit rate 0.813, Sharpe (overlap-inflated) +19.44, max DD −13.0%**, cum return +2919% — directionally consistent with yesterday's +18.71 Sharpe / 0.878 hit rate.
- The portfolio long-leg n_long=7.2 vs short-leg n_short=62.2 still skews short, same as pre.
- No `elite` band exposure on any of 91 dates (same as pre).

**Monotone band ordering** at 21d cannot be directly re-verified from the JSON artifacts (the script's report.md emission appears to have been stripped between runs — only `pillar_ic.json`, `quintile_returns.json`, `portfolio_returns.json`, `summary.json` are written). The portfolio L/S metrics imply the high→review ordering still holds; full per-band re-verification would require re-running with a build that emits the markdown report.

---

## Adoption β re-check — **STILL INVERTED, AND WORSE**

The Q5-Q1 inversion got **sharper**, not better:
- Pre: Q5-Q1 = **−0.0143** at h=21d (t=−4.97)
- Post: Q5-Q1 = **−0.0255** at h=21d (**t=−10.73**)

**Nuance flagged**: This is expected. The Adoption β fix (`333e5dd` — `_MIN_SECTOR_COHORT` 8→20, mid-rank ties) landed **after** commit `2e1654c` started the 90-day backfill, so the rewritten history does **not** contain the recalibrated β. The current backtest is measuring P1 (`fa926bf`) with its original cohort thresholds applied retroactively to 90 days — i.e., the worst-case Adoption configuration. Tomorrow's snapshot will be the first day where β-fixed Adoption hits production; a re-backfill or accumulating forward data will be needed before this number can move.

The Q5-Q1 inversion deepening (−1.43% → −2.55%) is the clearest signal Phase 5 made Adoption *more discriminating in the wrong direction* — confirming the audit's "Adoption is fundamentals-anti-correlated for ~34% of W/R names" diagnosis.

---

## Sector RSS effect — **NOT DETECTABLE IN IC**

`ef1cc06` un-gated sector RSS into Thesis on the last ~6 backfilled dates. DES IC moved +0.022 → +0.028 (Δ +0.007) — directionally consistent with a small Phase 5 P4 contribution, but with only 6 of 91 dates having sector_rss the contribution is far below detection threshold (a sub-pillar input on 6/91 ≈ 7% of dates can move IC by at most ~0.005 even at perfect correlation). **Verdict: P4 sector RSS is wired but the backtest cannot yet measure it.** Need 30+ days of forward data with sector RSS live before this is testable.

---

## What this means for the next audit — **empirical priorities for Tier 5**

1. **Tier 5 #15 / Adoption recalibration** — now has hard numerical priority. The pillar IC went *negative* (−0.013) and Q5-Q1 inversion deepened from −1.4% to −2.5% with **t=−10.7**. This is the top-priority remediation: either gate the P1 sub-components behind `confidence_blend`, cap Trends/QoQ contribution to ±15 pts off 50-neutral, or invert the sign. The audit's recommendation was correct; the post-Phase-5 data raised the urgency.
2. **Tier 5 / Thesis is now a real pillar** — IC +0.082 with t≈+10.3 means Thesis joins Institutional and Financial as one of the **three predictive pillars**. Weight optimisation discussions can now treat Thesis as a first-class contributor. The audit's "weights should be lifted on Institutional+Financial" recommendation should be revisited — the right answer may be **lift Institutional + Financial + Thesis, cut Adoption**.
3. **P3 Financial calibration** — IC dropped 0.086 → 0.078 (−0.008). Small but worth tracking: the new margin sources (bank cohort, operating_income fallback) may be **temporarily noisy** as the margin scoring re-anchors. Re-run at 180 days before deciding if this is permanent.
4. **DES still marginal** — IC +0.028 at h=21d, t≈+2.5 (raw, ~+0.6 with overlap correction). Tier-2 macros help slightly; sector RSS not yet measurable. DES remains the weakest *non-broken* pillar.
5. **Backtest harness** — the report.md emitter appears to be missing in the current script (only JSON written). File for tomorrow: re-add the markdown report writer, or document the JSON→report rendering separately.

---

## Reproduce

```
.venv/bin/python scripts/lthcs_backtest.py --start 2026-02-17 --end 2026-05-18 --horizon 21 --run-id 2026-05-19_post_phase5
```

Output at: `data/lthcs/backtest/2026-05-19_post_phase5/` (4 JSON artifacts, no `report.md`).
