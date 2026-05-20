# LTHCS weight + band threshold audit — verdict (2026-05-19)

**Phase 3 tasks 3.5 + 3.6 — swarm agent GG.**
Report-only audit. **No changes to `weights.json` or `score.py` are made by this
work.** Recommendations below are for the user's review after they cross-check
with siblings EE (per-pillar quality) and FF (distribution + outliers).

Source data:

- Equity snapshots: `data/lthcs/snapshots/` (91 daily files, 2026-02-17 → 2026-05-18, 167 tickers).
- Crypto snapshots: `data/lthcs/snapshots_crypto/` (8 daily files, 2026-05-12 → 2026-05-19, 10 tickers).
- Prices: `.cache/lthcs/backtest/prices/<ticker>.json` (yfinance-cached, equity-only).
- Weights config: `data/lthcs/weights.json` (v1.2.0, last_updated 2026-05-19).
- Generated tables: [`data/lthcs/quality_audit/2026-05-19_weights_vs_ic.md`](../data/lthcs/quality_audit/2026-05-19_weights_vs_ic.md), [`data/lthcs/quality_audit/2026-05-19_band_distribution.md`](../data/lthcs/quality_audit/2026-05-19_band_distribution.md).

---

## Sub-task 3.5 — per-cohort weight verdict

Measurable cohorts only (3 equity profiles with ≥ 800 (date, ticker) observations
at the 21-day horizon). All other profiles — every recovery / pre-revenue /
inflection bucket and **all 8 crypto cohorts** — were skipped for lack of data
and need a re-audit after another ~30 days of snapshot accumulation.

| Cohort | n_obs | Verdict | Worst mismatch | Direction |
|---|---:|---|---|---|
| `growth_compounder` | 826 | **MISALIGNED** | `des` weight 0.20 vs. implied 0.37 | UNDERWEIGHT DES |
| `mature_compounder` | 2891 | **MISALIGNED** | `thesis_integrity` weight 0.20 vs. implied 0.58 | UNDERWEIGHT THESIS |
| `standard_compounder` | 5900 | **MISALIGNED** | `institutional_confidence` weight 0.20 vs. implied 0.63 | UNDERWEIGHT INST.CONF |

### What the IC says, per cohort

**`growth_compounder` (NVDA / MU / AVGO etc.)**

The two pillars with the strongest information content are `des` (IC Sharpe
+1.18) and `institutional_confidence` (+0.99). The current profile weights
`adoption_momentum` and `thesis_integrity` at 0.25 / 0.20 but the IC there is
moderate (+0.40 / +0.50). `financial_evolution` is essentially noise inside
this cohort (IC Sharpe +0.07, IC mean +0.012) — the 0.15 weight buys little.
Direction of misalignment: **shift 5–10pp from `adoption_momentum` and
`financial_evolution` into `des`**.

**`mature_compounder` (AAPL / PG / KO etc.)**

`thesis_integrity` has the highest IC Sharpe in the cohort (+2.37, driven by a
small std). `institutional_confidence` is **negative** here (IC Sharpe −0.03,
IC mean −0.009) — i.e. inside the mature-compounder bucket, big-money tilt
ranking is essentially noise vs. forward returns. The equal-weight 0.20-each
profile gives the same weight to a non-signal as to the strongest signal.
Direction of misalignment: **the equal-weight prior is too flat; bump
`thesis_integrity` and trim `institutional_confidence`.**

**`standard_compounder` (the fallback bucket — 100 tickers)**

Critical reading: **most pillars have IC Sharpe near zero or negative inside
this bucket** (`adoption_momentum` −0.28, `thesis_integrity` −0.42, `des`
−0.47). The two positive pillars are `institutional_confidence` (+0.45) and
`financial_evolution` (+0.27). The current profile loads `adoption_momentum`
at 0.25 — the *highest* weight on the *most-negative* IC pillar. Most likely
explanation: `standard_compounder` is too heterogeneous to be a useful cohort
— it's a catch-all for tickers not classified into mature/growth/recovery
profiles. The signal-to-noise here is bad because the cohort itself is poorly
defined, not because the weights are wrong per se. **Recommendation: re-audit
*after* peer-group reclassification reduces this bucket below 50 tickers.**

### Thesis Integrity caveat — load-bearing

Today's commit `10daa39` migrated Thesis sentiment from AV `NEWS_SENTIMENT`
to Finnhub `/news-sentiment` (Tier 6 #31). The snapshot data feeding this
audit is **pre-Finnhub**. In V1's daily pipeline the AV multi-ticker AND-filter
collapsed most Thesis sub-scores to neutral 50 (see `MEMORY.md` /
[alpha_vantage_news_sentiment_quirk](../../../.claude/projects/-Users-bryantabiadon/memory/alpha_vantage_news_sentiment_quirk.md)).
Yet Thesis still shows up as the **highest-Sharpe pillar in
`mature_compounder`** (+2.37) despite that headwind — because of its tight
std, not its mean. Post-Finnhub, the IC *mean* should rise too, and the
implied Thesis weight in mature/growth cohorts will likely climb further.
**Don't act on the Thesis verdict yet. Re-audit on/after 2026-05-26.**

---

## Sub-task 3.6 — band threshold verdict

Latest equity snapshot (2026-05-18, n=167):

| Band | Range | Count | Pct | Verdict |
|---|---|---:|---:|---|
| elite | 85–100 | 0 | 0.0% | **SHIFT-DOWN** (empty — Elite is unreachable on current data) |
| high_confidence | 80–84 | 0 | 0.0% | **EMPTY** (also unreachable) |
| constructive | 70–79 | 5 | 3.0% | KEEP |
| monitor | 60–69 | 23 | 13.8% | KEEP |
| weakening | 50–59 | 53 | 31.7% | KEEP |
| review | <50 | 86 | 51.5% | **SHIFT-UP** (review is 51% of universe — threshold too low) |

Latest crypto snapshot (2026-05-19, n=10): everything sits in monitor /
weakening / review; same shape as equity but a 10-ticker sample is too thin
to draw threshold conclusions.

### Verdict: thresholds are misaligned with current score distribution

Half the universe sitting in **Structural Review Required** is not a useful
signal — it's grading on too tight a curve. Two options:

1. **Recalibrate thresholds downward** so Constructive starts at ~65 and Elite
   at ~80. This is the lower-cost change; doesn't touch the scoring math.
2. **Recalibrate the scoring math** so the upper bands fill naturally. This
   is the right long-term answer — the band labels (`Elite Confidence Hold`,
   `High Confidence Hold`) describe *strategic standing*, not a percentile.
   If no name in the universe meets the bar today, that's a real signal —
   but only if the bar is set correctly.

**Recommendation: don't move thresholds yet.** Re-audit after Finnhub
sentiment lifts Thesis scores out of the neutral-50 zone and after EE's
per-pillar quality fixes land. If Elite is still empty in ~30 days, that's
when to consider rescaling.

### Stability

30-day band churn across 167 equity tickers:

- mean churn rate: **0.048** (a typical ticker flips bands once every ~21
  trading days).
- p90: 0.138; only 5 tickers flip more often than once a week (UNP, CCEP,
  NEE, PM, SMCI).

**Verdict: churn is acceptable.** No hysteresis needed. The few high-churners
are concentrated near the weakening↔review boundary (50) which is consistent
with a universe mass-clumped just under 50.

---

## Top recommendations (highest-leverage, post-Finnhub stabilization)

1. **Re-run this audit on 2026-05-26** (or whenever Finnhub Thesis has accrued
   ≥ 5 trading days of data). The current numbers underestimate Thesis IC by
   construction. Until then, do not bump weights.
2. **Plan a `mature_compounder` weight shift** of +0.05 to `thesis_integrity`
   from `institutional_confidence` once the re-audit confirms Thesis IC mean
   is stable. This is the single change with the highest implied lift (gap
   = 0.384 implied vs. current). All other adjustments should wait.
3. **Defer `standard_compounder` adjustment** until peer-group reclassification
   shrinks the bucket. Tuning weights on a heterogeneous catch-all is
   chasing noise.
4. **Defer band threshold changes.** A 51% review-band concentration is a
   symptom of pre-Finnhub Thesis suppression compounding through five
   pillars; fix the inputs first, then revisit thresholds.

---

## Scope guardrails honoured

- This document is verdict-only.
- No edits to `data/lthcs/weights.json`, `lthcs/score.py`, `app.py`,
  `v2/app.py`, `.github/workflows/`, `lthcs/pillars/*.py`, or any sibling
  agent territory.
- Audit script (`scripts/lthcs_weight_threshold_audit.py`) and unit tests
  (`tests/lthcs/test_weight_threshold_audit.py`) are new files.
