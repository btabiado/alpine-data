# LTHCS Diagnostic Runbook

A repeatable procedure for figuring out why an LTHCS score looks the way it
does. Use this whenever a ticker scores unexpectedly high or low and you
want to know which of three things is going on:

- **(a) Real signal** — the model is telling you something genuine given the
  inputs we have.
- **(b) Calibration** — the inputs are right but weights, peer groups, or
  normalization bounds are mis-tuned.
- **(c) Missing data** — one or more pillar components fell back to neutral
  50 because an upstream fetch failed or a sub-component is a V1 stub.

This runbook accompanies `scripts/lthcs_diagnose.py`. It's the narrative
companion to the tool's output. Queue item: `docs/lthcs-followups-queue.md`
#1.

---

## 1. What this tool is for

`lthcs_diagnose.py` takes a small set of tickers you have strong priors
about — typically one expected HIGH, one expected LOW, one expected MID —
and pulls every pillar's input from the latest snapshot, the variable
detail file, the narrative, the sentiment file, the history file, and the
Thesis rotation log. It then labels each pillar as **REAL / PARTIAL /
NEUTRAL / STUB / MISSING** and writes a short diagnosis at the bottom of
each ticker block.

This tool does **not** modify any state. It is strictly read-only.

## 2. Quick usage

From repo root:

```bash
cd ~/Documents/btc-eth-etf-dashboard
source .venv/bin/activate
python scripts/lthcs_diagnose.py AAPL INTC NVDA
```

Other invocations:

| Command | Behaviour |
|---|---|
| `python scripts/lthcs_diagnose.py` | Defaults to `AAPL INTC NVDA`. |
| `python scripts/lthcs_diagnose.py LCID` | Single ticker. |
| `python scripts/lthcs_diagnose.py --snapshot 2026-05-16 AAPL` | Pin a specific snapshot date. |
| `python scripts/lthcs_diagnose.py --data-root /tmp/lthcs AAPL` | Read from a non-default data root (tests). |
| `python scripts/lthcs_diagnose.py --json AAPL INTC` | Emit structured JSON instead of text. |

Exit codes:
- `0` everything succeeded
- `1` data-load failure (missing snapshot index, malformed JSON, etc.)
- `2` one or more requested tickers were not in the snapshot

### Output structure

For each ticker the tool prints:

1. **Header** — name, sector, maturity stage.
2. **Score block** — composite, band, confidence, drift, snapshot flags,
   dropped pillars.
3. **Pillar breakdown table** — one row per pillar with `status` column.
4. **Modifiers line** — `macro_adj`, `sector_adj`, `volatility_mod`.
5. **Diagnosis bullets** — verdict + top driver + binding constraint +
   any calibration flag.
6. **Thesis rotation freshness** — when AV NEWS_SENTIMENT last touched
   this ticker.
7. **History sparkline** — last 5 snapshot scores.
8. **Narrative** — the four template fields from the narratives file.

After all tickers, a **cross-ticker comparison table** with the composite,
band, top driver, weakest pillar, and overall status (`REAL`, `PARTIAL`,
`REAL+NEUTRAL`, `DATA GAP`).

## 3. How to read each pillar's status

| Status | Meaning | Action |
|---|---|---|
| **REAL** | All sub-components have real data. Sub-score reflects the underlying signal. | Trust the contribution. If the sub-score is at an extreme, the pillar is genuinely calling that. |
| **PARTIAL** | At least one core sub-component is real, but at least one is stubbed (e.g. Adoption has revenue but no Trends; Institutional has momentum but no 13F; Financial has revenue + OCF but no margin). The pillar internally renormalizes among its surviving sub-components. | Score is directionally useful but not fully informed. A V2 build (Trends, 13F, full margin history) would refine it. |
| **NEUTRAL** | All sub-components have real data, but the resulting sub-score lands at exactly 50.0. The data was there; it just didn't tilt. | Treat as "no information," not "missing information." This is a *feature*: the model is honestly saying it sees no signal in this dimension. |
| **STUB** | The pillar is entirely stubbed — either the snapshot's `data_quality_flags` includes this pillar's stub flag (e.g. `thesis_unavailable`), or the upstream fetch found no usable data. STUB pillars are renormalized OUT of the composite (their weight is redistributed to surviving pillars; see the `effective_weight` column for the post-renorm percentage). | The composite IS NOT using this pillar. Other pillars are carrying its weight. Diagnosis is `DATA GAP`. |
| **MISSING** | No `variable_detail` row was found for this pillar at all. Shouldn't happen on a real pipeline run. | File a bug in `lthcs_daily.py` — the detail emitter dropped a pillar. |

### Per-pillar stub specifics (V1)

- **Adoption Momentum** — Google Trends slope is a V1 stub for everyone.
  Revenue carries the full pillar internally. Adoption will read `PARTIAL`
  on every ticker until Phase 2 wires Trends.
- **Institutional Confidence** — 13F holdings deltas are a V1 stub for
  everyone. 90d momentum carries the full pillar. Institutional will read
  `PARTIAL` on every ticker until Phase 2 wires 13F.
- **Financial Evolution** — needs revenue + margin history + OCF; if any of
  those three is missing for a ticker, it reads `PARTIAL`. LCID is the
  classic case (no margin history but has revenue + OCF).
- **Thesis Integrity** — depends on AV NEWS_SENTIMENT. The rotation is
  paced at ~5–25 tickers/day on the free tier; many names show up with
  `last_scored=null` (never scored) or with `article_count=0` (scored but
  AV returned nothing matching the filter). Both render as `STUB` and
  cause `thesis_unavailable` to appear in `data_quality_flags`, which
  renorms the pillar out of the composite.
- **DES** — driven by 6 macro signals (CPI, FF, 10Y, 10Y 30d change,
  unemployment, WTI oil). If the macro fetch succeeded, DES is `REAL` for
  every ticker — only the sector sensitivities and overrides differ.

## 4. The diagnostic decision tree

After running the tool on 3 high-conviction names:

```
Run lthcs_diagnose.py on 3 high-conviction names.
│
├─ All pillars REAL on all 3 tickers, scores still surprising
│   →  Probably a peer-group issue. AAPL ranked against LCID is wrong.
│      See queue item #3 (peer-group audit) and apply
│      peer_relative_percentile_by_sector to the right call sites.
│
├─ Some pillars STUB on high-conviction names (typically Thesis)
│   →  DATA gap, not a model problem. Either:
│        (a) Wait. The Thesis rotation ramps at 5–25 tickers/day; full
│            universe coverage in ~10–14 days from a fresh state.
│        (b) Force-prioritize. Edit lthcs_daily.py rotation order to
│            score the names you care about first.
│        (c) Accept renorm. The composite is still useful — the dropped
│            pillar's weight has been redistributed, and the surviving
│            pillars' contributions add to the composite.
│
├─ Pillars REAL but sub-scores feel mis-calibrated
│   →  CALIBRATION. Use the Tuning Kit (queue item #4):
│        - data/lthcs/weights.json for pillar weights by maturity stage
│        - data/lthcs/sector_des_weights.json for DES sector sensitivities,
│          normalization bounds, magnitude_scale, ticker overrides
│        - volatility-modifier threshold (currently 90th percentile, -3.0)
│      Bump model_version per §1.4 of the white paper.
│
└─ A whole pillar's inputs are STUBBED for the entire universe
    →  PHASE 2 build, not a recalibration. Examples:
        - 13F holdings (Institutional)
        - Google Trends acceleration (Adoption)
      Plan with a separate work item; nothing to tune today.
```

The tool's own VERDICT line gives you the first-level branch. The pillar
table tells you which branch to take when "DATA GAP" appears.

## 5. Three worked examples (live snapshot 2026-05-17)

### Example 1 — AAPL (expected HIGH, scored 55.1 "weakening")

What the tool says:
- Composite **55.1** (`weakening` band)
- Adoption Momentum **PARTIAL** at 46.8 — revenue YoY +6.4% maps to
  rev_subscore 46.8; Trends stub.
- Institutional **PARTIAL** at 71.5 — momentum +14.6% → 71.5 percentile;
  13F stub.
- Financial Evolution **REAL** at 66.2 — rev 46.8, margin 58.3 (slope
  +0.0083), OCF 100 (TTM OCF margin +35.5%).
- Thesis Integrity **STUB** — `thesis_unavailable` flag, pillar
  renormalized out, but only because AV found 0 articles for AAPL today.
- DES **REAL** at 45.7 — Technology in a rate-pressured macro.
- VERDICT: **DATA GAP** (Thesis stubbed).

Interpretation:
- The 55.1 is *not* the model being wrong about AAPL. It's a combination
  of (a) AAPL's +6.4% revenue growth ranking mid-pack against
  standard-compounder peers that include NVDA (+65%) and other
  hyper-growers — a **peer-group issue** that the queue item #3 audit
  will address — and (b) DES dragging because Technology is rate-sensitive
  in this macro regime, not because AAPL is uniquely vulnerable.
- The Thesis STUB is mechanical; it would not change the band materially
  even if filled in, because mean_sentiment is anchored at 50.
- Action: wait for the peer-group audit before treating AAPL=55 as a
  signal. The model is honestly reporting "this is what +6.4% looks like
  in a universe-relative percentile."

### Example 2 — INTC (expected LOW, scored 40.4 "review")

What the tool says:
- Composite **40.4** (`review` band).
- Adoption Momentum **PARTIAL** at 12.5 — revenue YoY -0.5% maps to
  rev_subscore 12.5; Trends stub.
- Institutional **PARTIAL** at 100.0 — momentum +171.7% is off the
  charts (rebound from oversold); 13F stub.
- Financial Evolution **REAL** at 31.4 — rev_subscore 12.5, margin 68.2
  (improving slope +0.0182), OCF 19.8 (TTM OCF margin -2.1%, still
  negative).
- Thesis Integrity **STUB** — never scored by AV; pillar renormalized
  out.
- DES **REAL** at 45.7 — same Technology macro drag as AAPL.
- Volatility modifier fired **-3.0** (INTC is in the high-vol cohort).
- VERDICT: **DATA GAP**.

Interpretation:
- This is largely **REAL SIGNAL** for the LOW thesis: revenue is
  shrinking, OCF margin is negative, and the volatility penalty is on.
  Adoption + Financial pillars are honestly bad.
- The 100.0 Institutional sub-score is a momentum bounce from a depressed
  base — informative but not necessarily contradictory. INTC is moving
  off lows; the model picks that up.
- The Thesis STUB doesn't change the verdict, but watching the bounce in
  press coverage when AV finally scores INTC would be the soonest
  refinement.
- Action: trust the 40.4 directionally. If you wanted to refine: force
  AV to prioritize INTC in the next Thesis rotation.

### Example 3 — LCID (expected LOW from a structural standpoint)

What the tool says:
- Composite **53.7** (`weakening` band) — higher than you'd expect for a
  pre-profit EV maker burning cash.
- Adoption Momentum **PARTIAL** at 100.0 — revenue YoY +67.6% (low base,
  high growth %); Trends stub.
- Institutional **PARTIAL** at 0.6 — momentum -47.9% is bottom-percentile;
  13F stub.
- Financial Evolution **PARTIAL** at 57.1 — revenue 100, margin missing
  (so it falls back to 50), OCF 0 (TTM OCF margin -263.3%).
- Thesis Integrity **STUB** — AV scored 0 articles today.
- DES **REAL** at 53.6 — Consumer Discretionary with a `wti_oil_usd`
  override applied.
- Volatility modifier fired **-3.0**.
- VERDICT: **DATA GAP**.

Interpretation:
- The 53.7 is materially driven by Adoption=100 on +67% revenue growth.
  But that growth percentile is **universe-relative**, ranking LCID's
  +67% against a universe of mature mega-caps. The number is real, but
  the percentile rank is structurally not comparable.
- This is a textbook **calibration / peer-group** case: peer-group audit
  (#3) plus possibly the tuning kit (#4) would dial this down.
- Financial Evolution PARTIAL at 57.1 is misleading: a missing
  `margin_subscore` defaults to 50, which props the pillar up despite
  OCF margin of -263%.
- Action: do not buy the 53.7 as a signal. Sector-relative percentiles
  would put LCID much lower on Adoption. Also consider whether
  pre-profit-growth maturity profile is the right weighting — the
  current profile gives Adoption 30% weight, the highest of any stage.

### Cross-ticker comparison

After running all three, the bottom table makes the picture obvious:

```
  ticker composite  band          top driver               weakest pillar         overall
  AAPL   55.1       weakening     institutional_conf (72)  des (46)               DATA GAP
  INTC   40.4       review        institutional_conf (100) adoption_momentum (12) DATA GAP
  NVDA   79.4       constructive  adoption_momentum (100)  des (49)               DATA GAP
```

(Substitute LCID for NVDA in the example above; the same column logic
holds.) The "DATA GAP" overall column reflects that Thesis Integrity is
stubbed on all three. The top-driver/weakest-pillar columns confirm
where each ticker is genuinely strong vs. binding.

## 6. Failure modes

| Symptom | Cause | Fix |
|---|---|---|
| `ERROR: required file missing: data/lthcs/snapshots/index.json` | First run before any snapshot was committed, or wrong CWD. | `cd` to repo root; verify `data/lthcs/snapshots/index.json` exists. |
| `ERROR: required file missing: data/lthcs/snapshots/YYYY-MM-DD.json` | The `latest` key in index.json points at a date whose file was deleted, or `--snapshot` was given a date with no file. | Re-run `python lthcs_daily.py` to regenerate, or pick an existing date with `--snapshot`. |
| `WARNING: tickers not in snapshot: XYZ` (exit 2) | Ticker isn't in `data/lthcs/universe.json` or was filtered out of the snapshot. | Verify spelling; check `universe.json`'s `active` flag for the ticker. |
| All pillars on all tickers read `MISSING` | `variable_detail/<date>.json` is empty or doesn't match the snapshot date. | Regenerate the day's data via `python lthcs_daily.py`. |
| Pillar status looks wrong (e.g. "this shows REAL but I know the input is stubbed") | The `data_quality` flags in `variable_detail/<date>.json` don't match what `lthcs_daily.py` actually computed. | File a bug; the classifier is downstream of those flags and trusts them. |
| Snapshot is stale (drift always 0.0) | You're on a snapshot from a day when only a partial Thesis rotation ran. Drift is 0 because the composite didn't move. | Not a tool bug. Run more daily snapshots; drift fills in. |
| Tool prints normally but cross-ticker table is empty | All requested tickers were missing from the snapshot. | Check exit code (2) and the WARNING line on stderr. |

## 7. What to do after running the tool

1. Note the three statuses for each pillar across all tickers.
2. Walk the decision tree (§4) to classify the situation as Peer-Group,
   DATA, CALIBRATION, or Phase-2.
3. Open the matching queue item:
   - DATA — wait for Thesis rotation OR edit `lthcs_daily.py` rotation
     order.
   - Peer-Group — proceed to `docs/peer-group-audit.md` (queue item #3).
   - CALIBRATION — proceed to `docs/lthcs-tuning-kit.md` (queue item #4).
   - Phase-2 stub — log against the Phase 2 backlog.
4. If you change weights or sensitivities, bump `model_version` per the
   procedure in the tuning kit, preserve the prior snapshot (git history
   is the audit log), and re-run this tool to compare.

---

*Companion to `scripts/lthcs_diagnose.py`. Read-only diagnostic — produces
no side effects on the repo or data files.*
