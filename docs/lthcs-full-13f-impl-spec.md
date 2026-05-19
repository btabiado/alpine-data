# Tier 3 #13 — Full 13F institutional holdings implementation

**Status**: spec — ready for implementation agent
**Audit-claimed effort**: L (2–3 swarms)
**Revised effort** (post-scoping): **M (1 swarm) for Phase 1**, full 3 phases L
**Pillar touched**: Institutional Confidence
**Last updated**: 2026-05-18

---

## 1. Current state

The audit (`docs/lthcs-open-items-audit.md`, row 401) labels Tier 3 #13
as "Stubbed (renormed). Momentum carries 100%." This is **materially
out of date** post-Phase-5 P0 (`a55aab8`, 2026-05-18).

- `lthcs/sources/sec_13f.py` (1195 LOC) is a **full multi-manager
  aggregation client**. It tracks 21 top managers via
  `TRACKED_MANAGERS` (`sec_13f.py:105-127`: BlackRock, Vanguard, SSGA,
  Fidelity, T. Rowe, Capital Research + World, Berkshire, JPMorgan,
  Wellington, Geode, BNY Mellon, Morgan Stanley, Goldman, Bridgewater,
  Renaissance, Tiger, Citadel, Two Sigma, AQR, Millennium).
- `fetch_universe_institutional_holdings()` (`sec_13f.py:1113`) fans
  out per manager, then `aggregate_holdings_for_ticker()`
  (`sec_13f.py:965`) emits `manager_count`, `total_shares_held_mm`,
  `total_value_held_bn`, `top_holders[]`, `quarter_over_quarter
  {share_change_pct, manager_count_change, net_buyers, net_sellers}`,
  `conviction_signal` (accumulating/steady/mixed/distributing),
  `signal_score` ∈ [-1,+1], and `data_quality` band (good ≥10,
  partial 5–9, sparse <5).
- `lthcs/pillars/institutional.py:494-583` (`_apply_holdings_adjustment`)
  consumes that record. Combined insider+holdings adjustment cap
  `[-7, +12]` at lines 174-175. Audit's "renormed away to momentum" is
  the fallback path when `holdings_data=None`, not steady state.
- Phase 5 P0 verified `has_holdings=True` on **167/167** tickers in
  `variable_detail/2026-05-18.json`. Wire is live.

**Audit's substance still right at a deeper level.** In
`data/lthcs/holdings/2026-05-17.json`:

| data_quality | tickers | manager_count |
|---|---:|---|
| good (≥10 managers) | 33 | mega-caps (AAPL, MSFT, ABBV, JPM, …) |
| partial (5–9) | 12 | mid-caps |
| sparse (<5, mostly 0) | **123** | most of the universe |

123 / 168 tickers (73%) have **zero** tracked-manager coverage →
neutral `signal_score=0`. Of the 45 with non-zero coverage, the
pillar only fires on `accumulating`/`distributing` with
`|signal_score| ≥ 0.3` → real signal lands on ~10–15 tickers/day.
Wire works; **coverage is thin**.

---

## 2. The gap

Three concrete shortfalls:

1. **Manager breadth too narrow.** 21 managers ≈ ~25% of US institutional
   AUM. Sector-specialist funds, mid-tier mutuals, family offices,
   sovereign wealth, pensions all unobserved. Small/mid caps off
   BlackRock/Vanguard/SSGA index sheets get zero signal even with real
   active-fund accumulation.
2. **CUSIP coverage too shallow.** `TICKER_TO_CUSIP`
   (`sec_13f.py:208-258`) hand-codes ~50 mega-caps; `_build_name_lookup`
   (`sec_13f.py:296`) ~10 names. For the other ~120 LTHCS tickers,
   even when a manager DOES hold them the parser can't match the row.
3. **QoQ delta quantizes coarsely at 21-manager scale.** With most
   tickers at `manager_count ∈ {8,9,10,11}`, `(net_buyers−net_sellers)
   / manager_count` flips on one AQR rebalance.

SEC EDGAR is **not** the bottleneck — 13F-HRs are public, under the
10 req/sec SEC bucket shared by all SEC clients via
`sec_edgar._bucket`. Bottleneck is engineering: manager list, CUSIP
map, pillar consumption.

---

## 3. Design — data layer

**Recommendation: extend `sec_13f.py`, do not create a new module.**
Existing client has correct caching, stream-parsing
(`_iter_info_table_rows`, 5–30 MB XMLs via `ET.iterparse`), unit
handling (`sec_13f.py:628`, pre-2023 thousands → post dollars), and
amendment dedup (`_dedupe_filings_by_quarter`, `sec_13f.py:526`).
Reuse all of it.

### 3.1 Externalize the manager list

Today `TRACKED_MANAGERS` is hard-coded at `sec_13f.py:105-127`.
Move it to `data/lthcs/13f_institutions.json` with schema:

```
{
  "version": 2,
  "as_of": "2026-05-18",
  "managers": [
    {"name": "BlackRock", "cik": "0002012383",
     "aum_band": "mega", "type": "passive_complex", "active": true},
    ...
  ]
}
```

Load via a new `_load_managers()`; keep `TRACKED_MANAGERS` as
fallback for offline/test. `aum_band`/`type` unused in V1 math but
unblock Phase 2 passive/active weighting.

### 3.2 Externalize the CUSIP map

Move `TICKER_TO_CUSIP` (`sec_13f.py:208-258`) to
`data/lthcs/13f_cusip_map.json`:

```
{
  "version": 1,
  "as_of": "2026-05-18",
  "tickers": {
    "AAPL": {"cusips": ["037833100"], "name_aliases": ["apple"]},
    ...
  }
}
```

Backfill ALL ~168 LTHCS tickers. CUSIPs from OpenFIGI's free tier
or 10-K cover pages. New `scripts/build_13f_cusip_map.py` reads
`data/lthcs/universe.json` and emits the JSON for hand-review.

### 3.3 Expand the manager list — phased

| Phase | Manager count | Add roster |
|---|---:|---|
| Current | 21 | (existing `TRACKED_MANAGERS`) |
| Phase 1 | **50** | + Dimensional, Invesco, Schwab, Northern Trust, Franklin Templeton, Legal & General, Norges Bank, GIC, Putnam, Lord Abbett, Janus Henderson, Eaton Vance, Nuveen, Lazard, Manning & Napier, Acadian, Arrowstreet, D.E. Shaw, PointState, Lone Pine, Coatue, Maverick, Soroban, Viking, Pershing Square, Third Point, Greenlight, Glenview, Marshall Wace, Citadel Wellington |
| Phase 2 | **100** | + sector-specialist funds (Polen, Baillie Gifford, Sands, Edgewood for growth; Tweedy, Royce, FPA for value), regional pensions (CalPERS, CalSTRS, NYS Common, Texas Teachers, Ontario Teachers'), sovereign holding cos (Saudi PIF, Mubadala, ADIA via subsidiary CIKs) |

CIKs verified via SEC EDGAR full-text search filtered to recent
13F-HRs (methodology at `sec_13f.py:88-104`). One-time exercise.

### 3.4 Output schema — backward-compatible

Keep the existing return shape from `aggregate_holdings_for_ticker()`
(`sec_13f.py:1089-1108`). Add three OPTIONAL fields:

- `manager_universe_size: int` — count of managers actually scanned
  (so the pillar can normalize `signal_score` by coverage)
- `tracked_aum_pct: float` — fraction of US institutional AUM covered
  by the scanned manager set (Phase 1 ≈ 0.40, Phase 2 ≈ 0.65)
- `passive_active_split: {"passive_shares": float, "active_shares": float}`
  — Phase 2 only

These are additive; consumers that ignore them continue to work.

### 3.5 Cadence

13F-HR deadline: 45 days post-quarter-end → filings land Feb 15 / May
15 / Aug 15 / Nov 15. Existing 14-day aggregate TTL
(`sec_13f.py:150`) and 365-day per-filing TTL are correct. No change.

---

## 4. Design — pillar integration

`lthcs/pillars/institutional.py` already consumes the aggregate via
`_apply_holdings_adjustment()` (lines 494-583). Two refinements:

### 4.1 Coverage-aware scaling

Today `_HOLDINGS_PTS_STRONG_ACCUMULATING = 5.0` is constant. Post-50-manager
expansion, a strong signal at `manager_count=12` matters more than at
`manager_count=4`. Scale points by `min(1.0, manager_count / 10)` so
sparse-but-real signals get a proportional adjustment. New constant
`_HOLDINGS_COVERAGE_FLOOR_FOR_FULL_PTS = 10`.

### 4.2 Sub-component split (Phase 3)

Today three signals collapse into one `signal_score`. Phase 3
exposes them for evidence-modal UX:

- `institutional_breadth = manager_count / manager_universe_size`
- `institutional_conviction = signal_score` (net buyers/sellers ratio)
- `institutional_drift = quarter_over_quarter.share_change_pct / 100`

Lives under `components.holdings.subcomponents{breadth,conviction,
drift}`. Headline `sub_score` math unchanged.

### 4.3 Pillar weight distribution

**No weight changes.** Pillar's 70/30 momentum/13F base
(`institutional.py:105-106`) and combined cap `[-7,+12]`
(line 174-175) already correct. Phase 1 just makes the existing
adjustment fire on more tickers.

---

## 5. Data volume estimate

Per Phase:

| Phase | Managers | Cold-fetch XML volume | Cold-fetch wall time | Steady-state cache hit rate | Per-quarter incremental refresh |
|---|---:|---:|---:|---:|---:|
| Current | 21 | ~600 MB | ~8 min | >99% | 21 fetches × 4/yr = **84 fetches/yr** |
| Phase 1 | 50 | ~1.4 GB | ~18 min | >99% | **200 fetches/yr** |
| Phase 2 | 100 | ~2.8 GB | ~35 min | >99% | **400 fetches/yr** |

Cache footprint stays small (universe-extracted JSON ~100 KB each;
raw XMLs not cached per `sec_13f.py:459-477`). 200–400 fetches/year
is well under SEC's 10 req/sec per-UA limit even at cold start.

---

## 6. Implementation phases

### Phase 1 — coverage expansion (M, 1 swarm)
- Externalize `TRACKED_MANAGERS` → `data/lthcs/13f_institutions.json`
- Externalize `TICKER_TO_CUSIP` → `data/lthcs/13f_cusip_map.json`,
  backfill all 168 tickers via `scripts/build_13f_cusip_map.py`
- Expand to **50** managers
- Add coverage-aware scaling in `_apply_holdings_adjustment` (§4.1)
- Tests: existing `tests/lthcs/test_sec_13f*.py` + new fixtures for
  the 50-manager set + coverage-scaling

**Expected outcome**: `data_quality="good"` 33 → ~80; `manager_count>0`
45 → ~140/168. IC delta hard to predict pre-build but +0.02–0.05 at
21d plausible (headline-only → headline + tail).

### Phase 2 — backfill + manager-type weighting (M)
- Expand to 100 managers
- `aum_band`/`type` consumption: passive-complex moves get 0.5× weight
  on conviction (BlackRock buying ≠ active conviction)
- Backfill last 4 quarters of aggregates
- New cron `lthcs-13f-quarterly.yml` (§8)

### Phase 3 — UX + sub-component split (S)
- Pillar exposes `breadth/conviction/drift` (§4.2)
- Detail-modal UI in lthcs_tab + lthcs_tab_v2
- QoQ sparkline over last 4 quarters

---

## 7. Files to create / modify

**New**:
- `data/lthcs/13f_institutions.json` — externalized manager list
- `data/lthcs/13f_cusip_map.json` — externalized CUSIP map
- `scripts/build_13f_cusip_map.py` — one-shot CUSIP-map builder
- `.github/workflows/lthcs-13f-quarterly.yml` — Phase 2 quarterly cron
- `tests/lthcs/test_sec_13f_phase1.py` — new fixtures (50-manager)

**Modify**:
- `lthcs/sources/sec_13f.py`:
  - `_load_managers()` + `_load_cusip_map()` (JSON-first, constants
    fallback)
  - extend `aggregate_holdings_for_ticker()` (`sec_13f.py:965`) with
    additive fields from §3.4
  - Phase 2: `_classify_manager_type()` for passive/active split
- `lthcs/pillars/institutional.py`:
  - new `_HOLDINGS_COVERAGE_FLOOR_FOR_FULL_PTS` (Phase 1)
  - `_apply_holdings_adjustment` (line 494) — multiply `adj` by
    `min(1.0, manager_count / floor)`
  - Phase 3: expose `subcomponents{breadth,conviction,drift}` in
    `detail` dict
- `lthcs_daily.py:1124-1162` — no call-site change; manager count
  expansion is transparent.

**Do not touch**: `v2/app.py` (V2 doesn't render Institutional sub-
components); `app.py` evidence modal pre-Phase-3.

---

## 8. Cadence + workflow

**Current**: 13F runs inside daily LTHCS cron; 14-day aggregate TTL
absorbs the cost (`sec_13f.py:150`). Works at 21 managers; gets
uncomfortable at 50–100 if cold-cache hits ~35 min.

**Phase 2 recommendation**: split into a **quarterly batch** (cache
warmer) + **daily reader** (cache consumer). Analog: weekly Trends
batch pre-warms `data/lthcs/trends/`.

New `.github/workflows/lthcs-13f-quarterly.yml`:
- Triggers Feb 16 / May 16 / Aug 16 / Nov 16 at 06:00 UTC (one day
  past 45-day filing deadline)
- Runs `python -m lthcs.sources.sec_13f --refresh-all --managers all`
- Writes `data/lthcs/holdings/<YYYY-MM-DD>.json`
- Commits + pushes on diff (autopush per `lthcs_auto_push` memory)

Daily cron continues calling
`fetch_universe_institutional_holdings()` — hits warm aggregate
cache 99% of runs; 7-day-stale fallback (added in `a55aab8`) covers
the rare miss.

---

## 9. Open questions / blockers

1. **CUSIP authority.** OpenFIGI free tier: 25 req / 6 sec
   unauthenticated. Fine for one-shot 168-ticker map. If recurrent
   refresh needed, get an API key.
2. **Passive vs. active classification.** BlackRock files one
   consolidated 13F across iShares (passive) + active sleeves. Can't
   cleanly attribute shares without fund-level disclosure. Phase 2's
   "passive complex" tag is manager-level, not fund-level — document
   in the JSON schema.
3. **Maturity-stage interaction.** A `growth_compounder` accumulating
   ownership is stronger than the same delta in a `mature_compounder`
   (90% inst-owned baseline). Out of scope here; flag for Tier 3 #16
   companion item.
4. **Filing-lag funds.** Bridgewater / Renaissance occasionally file
   45–60 days late citing competitive sensitivity. Amendment dedup
   (`sec_13f.py:526`) already handles; flag in tests.

---

## 10. Effort revision

| Phase | Audit's L estimate | Revised |
|---|---|---|
| Phase 1 alone (50 managers + CUSIP backfill + coverage scaling) | (subset of L) | **M (1 swarm)** |
| Phases 1 + 2 (100 managers + manager-type weighting + quarterly cron) | L | **M-L (2 swarms)** |
| Full Phases 1–3 | L (2–3 swarms) | **L (2–3 swarms)** — matches audit |

Key insight: audit treated this as "build aggregation from scratch,"
but that work was already done (`sec_13f.py` is 1195 LOC of real
aggregation). Remaining work is **coverage expansion + UX polish**,
not core build-out.

**Recommended next step**: ship Phase 1 only. The IC delta from
33 → 80 "good" tickers is the highest-leverage cut. Defer Phase 2/3
until Phase 1's IC contribution is measured against the +0.204
Institutional baseline.
