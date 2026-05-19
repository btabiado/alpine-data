# LTHCS Tech sub-bucket split — Hardware / Semiconductors / Software / IT Services

**Status:** Spec (ready to build). Unblocks `lthcs-open-items-audit.md` Tier 2 #7
(compound peer-group key) which was deferred per `peer-group-audit.md` §3.4.

**Owner:** LTHCS pillar peer-cohort logic (`lthcs/peer_groups.py` + `data/lthcs/peer_groups.json` + `data/lthcs/universe.json`).

**Snapshot reference:** `data/lthcs/snapshots/2026-05-18.json` (latest snapshot
in repo at spec time; spec request named `2026-05-19.json` which does not yet
exist).

---

## 1. The problem

A naive Tech-compounder cohort is bimodal. From `peer-group-audit.md` §3.4
("Symptom 1: AAPL trapped at median revenue percentile"):

> | Cohort | n | Percentile |
> | --- | --- | --- |
> | universe | 160 | 46.2 |
> | standard_compounder (current fix) | 156 | 46.8 |
> | **standard_compounder × Technology** | **38** | **13.2** (worse!) |

Audit: Tech-compounder is half peak-earnings semis (MU +49%, NVDA +66%,
AMD +34%, MRVL +42%, SMCI +47%) and half megacap-mature (AAPL +6%, IBM
+8%, CSCO +5%, ORCL +8%). AAPL ranks low because it's benchmarked
against non-peers. Fix: peel mature-compounder out of tech-compounder
via a curated Hardware vs Software split — this spec.

Snapshot confirms: even the existing `tech_hardware` group in
`data/lthcs/peer_groups.json` (which lumps AAPL with semis) has 20-member
adoption stdev **28.2** and range 11→100 — still bimodal.

---

## 2. The split proposal — exhaustive Tech ticker classification

All 43 tickers with `sector: "Technology"` in `data/lthcs/universe.json`
(the universe uses `Technology`, not `Information Technology`; aliasing handled
in `lthcs/pillars/des.py` per Tier 2 #8). One canonical bucket per ticker.

| Ticker | Name | Industry (universe.json) | Proposed bucket | Reason |
|---|---|---|---|---|
| AAPL | Apple Inc. | Consumer Electronics | Hardware | iPhone/Mac/Wearables ≈80% revenue. |
| ACN | Accenture plc | IT Services | IT Services | Consulting + integration. |
| ADBE | Adobe Inc. | Software | Software | Creative + Document Cloud SaaS. |
| ADI | Analog Devices, Inc. | Semiconductors | Semiconductors | Pure-play analog silicon. |
| ADSK | Autodesk, Inc. | Software | Software | Design SaaS. |
| AMAT | Applied Materials, Inc. | Semiconductor Equipment | Semiconductors | Wafer-fab equipment. |
| AMD | Advanced Micro Devices | Semiconductors | Semiconductors | CPU/GPU silicon. |
| ANSS | ANSYS, Inc. | Engineering Software | Software | CAE simulation SaaS. |
| ARM | Arm Holdings plc | Semiconductors | Semiconductors | Silicon IP licensing. |
| ASML | ASML Holding N.V. | Semiconductor Equipment | Semiconductors | EUV lithography. |
| AVGO | Broadcom Inc. | Semiconductors | Semiconductors | Networking/ASIC silicon (VMware <25%). |
| CDNS | Cadence Design Systems | EDA Software | Software | Chip-design SaaS. |
| CDW | CDW Corporation | IT Distribution | IT Services | Reseller / managed services. |
| CRM | Salesforce, Inc. | Software | Software | CRM SaaS. |
| CRWD | CrowdStrike Holdings | Cybersecurity | Software | Cybersecurity SaaS. |
| CSCO | Cisco Systems, Inc. | Networking Equipment | Hardware | Routing/switching hardware. |
| CTSH | Cognizant Tech Solutions | IT Services | IT Services | IT consulting / outsourcing. |
| DDOG | Datadog, Inc. | Software | Software | Observability SaaS. |
| FTNT | Fortinet, Inc. | Cybersecurity | Software | Cyber SaaS. |
| GFS | GlobalFoundries Inc. | Semiconductors | Semiconductors | Foundry. |
| IBM | International Business Machines | IT Services | IT Services | Consulting + Red Hat. |
| INTC | Intel Corporation | Semiconductors | Semiconductors | CPU silicon + foundry. |
| INTU | Intuit Inc. | Software | Software | TurboTax + QuickBooks SaaS. |
| KLAC | KLA Corporation | Semiconductor Equipment | Semiconductors | Inspection/metrology. |
| LRCX | Lam Research Corp. | Semiconductor Equipment | Semiconductors | Etch/deposition. |
| MCHP | Microchip Technology | Semiconductors | Semiconductors | MCU + analog silicon. |
| MDB | MongoDB, Inc. | Software | Software | Database SaaS. |
| MRVL | Marvell Technology | Semiconductors | Semiconductors | Networking/storage silicon. |
| MSFT | Microsoft Corporation | Software | Software | Azure + M365 + LinkedIn. |
| MU | Micron Technology, Inc. | Semiconductors | Semiconductors | Memory (DRAM/NAND). |
| NOW | ServiceNow, Inc. | Software | Software | ITSM SaaS. |
| NVDA | NVIDIA Corporation | Semiconductors | Semiconductors | GPU silicon ≈95% revenue. |
| NXPI | NXP Semiconductors N.V. | Semiconductors | Semiconductors | Auto + IoT silicon. |
| ON | ON Semiconductor Corp. | Semiconductors | Semiconductors | Power + analog silicon. |
| ORCL | Oracle Corporation | Software | Software | DB + OCI cloud SaaS. |
| PANW | Palo Alto Networks | Cybersecurity | Software | NGFW + Prisma SaaS. |
| QCOM | QUALCOMM Incorporated | Semiconductors | Semiconductors | Mobile silicon + licensing. |
| SMCI | Super Micro Computer | Computer Hardware | Hardware | AI server OEM. |
| SNPS | Synopsys, Inc. | EDA Software | Software | EDA SaaS + IP. |
| TEAM | Atlassian Corporation | Software | Software | Jira/Confluence SaaS. |
| TXN | Texas Instruments | Semiconductors | Semiconductors | Analog + embedded silicon. |
| WDAY | Workday, Inc. | Software | Software | HCM + Financials SaaS. |
| ZS | Zscaler, Inc. | Cybersecurity | Software | Zero-trust SaaS. |

No `Hybrid` placements needed. Cybersecurity (CRWD/FTNT/PANW/ZS) rolls
into **Software** — subscription revenue model and growth distribution
match Software (§3). EDA tools (CDNS, SNPS) likewise roll into Software.

---

## 3. Sub-bucket statistics — bimodality check

Computed from `data/lthcs/snapshots/2026-05-18.json` adoption_momentum
sub-score (this is the pillar most exposed to the bimodality; revenue
sub-score dominates Adoption per `lthcs/pillars/adoption.py`). ANSS is
missing from today's snapshot (n=42 of 43 Tech tickers).

| Cohort | n | mean | median | stdev | min | max |
|---|---:|---:|---:|---:|---:|---:|
| **ALL TECH (parent)** | **42** | **53.3** | **51.8** | **25.8** | **11.0** | **100.0** |
| Hardware (AAPL, CSCO, SMCI) | 3 | 35.7 | 28.1 | 17.7 | 23.1 | 56.0 |
| Semiconductors | 18 | 52.1 | 50.9 | 29.2 | 11.0 | 100.0 |
| Software | 17 | 62.1 | 65.9 | 22.8 | 21.2 | 97.6 |
| IT Services | 4 | 34.4 | 34.9 | 6.0 | 27.5 | 40.4 |

**Composite (`lthcs_score`)**, which is what the user actually sees:

| Cohort | n | mean | stdev | min | max |
|---|---:|---:|---:|---:|---:|
| **ALL TECH** | **42** | **52.2** | **13.2** | **24.0** | **77.6** |
| Hardware | 3 | 51.2 | 3.7 | 47.1 | 54.2 |
| Semiconductors | 18 | 58.6 | 12.1 | 37.6 | 77.6 |
| Software | 17 | 49.8 | 12.3 | 24.0 | 69.2 |
| IT Services | 4 | 34.2 | 4.3 | 30.1 | 38.4 |

**Verdict.** Hardware (sd 3.7), IT Services (sd 4.3), and Software (sd
12.3) are tighter than parent Tech (sd 13.2). Semis stays wide on
Adoption (sd 29.2) because the semi cycle is legitimately bimodal across
maturity stages (NVDA/MU at 100 vs INTC at 13.5); the existing
`maturity_stage` axis handles this — Semis × growth (sd 24.5) and Semis ×
mature (sd 36.9) cascade via the resolver's `sector_group_only` →
`maturity_only` ladder when those cells are too sparse. The split kills
the AAPL-specific problem; residual semi-cycle spread is intentional.

AAPL's adoption_momentum today: 28.1. Under the proposed schema, AAPL's
compound cohort (Hardware × mature_compounder) collapses to {AAPL, CSCO},
fails the cohort-size floor, falls back through `STRATEGY_SECTOR_GROUP_ONLY`
(Hardware, 3 names) to `STRATEGY_MATURITY_ONLY` (the legacy maturity-only
cohort the audit shows lands AAPL at percentile 46.8). Desired behaviour.

---

## 4. Bucket → peer-cohort mapping

The compound peer-group key per `peer-group-audit.md` §3.4 was
`(maturity_stage, sector_group)`. With the split it becomes
`(maturity_stage, sector_group, tech_sub_bucket)` **for Tech tickers
only**:

| ticker.sector | Compound key |
|---|---|
| Technology / Information Technology | `(maturity_stage, sector_group, tech_sub_bucket)` |
| every other sector | `(maturity_stage, sector_group)` (unchanged) |

**Sector-group → sub-bucket dispatch (proposed `peer_groups.json` edit):**
the existing `tech_hardware` group splits into `tech_hardware` (AAPL,
CSCO, SMCI) and `tech_semiconductors` (the 18 semi tickers). The
existing `tech_software` group stays mostly intact but spins off
`tech_it_services` (ACN, IBM, CTSH, CDW). Mapping:

| Sub-bucket (proposed) | New `sector_group` key | Members | n |
|---|---|---:|---:|
| Hardware | `tech_hardware` | AAPL, CSCO, SMCI | 3 |
| Semiconductors | `tech_semiconductors` | (18 names listed in §2) | 18 |
| Software | `tech_software` | (18 names; incl. CRWD/FTNT/PANW/ZS/CDNS/SNPS/ANSS) | 18 |
| IT Services | `tech_it_services` | ACN, IBM, CTSH, CDW | 4 |

**Minimum cohort size.** `adoption.py:103` defines `_MIN_SECTOR_COHORT
= 20` for the sector-relative re-rank path. `peer_groups.json` ships
`min_cohort_size: 6` for the compound-key cascade. **Keep both
constants.** Hardware (3) and IT Services (4) cleanly fail the floor
and cascade to `STRATEGY_MATURITY_ONLY` — correct per audit.

---

## 5. Universe.json schema extension

Add a `tech_sub_bucket` field on every Tech ticker (and only on Tech
tickers). The field is optional everywhere else and absent on non-Tech
rows.

```json
{
  "ticker": "AAPL",
  "name": "Apple Inc.",
  "exchange": "NASDAQ",
  "index_membership": ["S&P 500", "S&P 100", "NASDAQ-100", "DJIA"],
  "sector": "Technology",
  "industry": "Consumer Electronics",
  "tech_sub_bucket": "Hardware",
  "maturity_stage": "mature_compounder",
  "active": true,
  "maturity_note": "Mega-cap +6%"
}
```

Allowed `tech_sub_bucket` values: `"Hardware"`, `"Semiconductors"`,
`"Software"`, `"IT Services"` (exact strings, title-cased to match
existing sector strings). Bump `data/lthcs/universe.json` `version` to
`2.2.0`.

---

## 6. Backwards compatibility

* **Non-Tech tickers.** `tech_sub_bucket` absent → resolver in
  `lthcs/peer_groups.py:168` continues to use the 2-tuple
  `(maturity_stage, sector_group)` key with no code change.
* **Existing pillar code touches:**
  * `lthcs/peer_groups.py:155-165 get_compound_peer_key` — extend to
    return `(stage, sector_group, tech_sub_bucket)` when the focal is
    Tech, else the existing 2-tuple. Add `_tech_sub_bucket_for(...)`.
  * `lthcs/peer_groups.py:215-220` (Level 1 compound match) — add
    `tech_sub_bucket` equality predicate when non-`None`.
  * `lthcs/pillars/adoption.py:638` — no signature change; resolver
    pulls `tech_sub_bucket` from the universe entry it already receives.
* **`maturity_stage` interaction.** `mature_compounder` means different
  things by sub-bucket: Hardware (AAPL/CSCO) ~5-7% growth, Software
  (MSFT/ORCL/ADBE) ~10-15%, Semi (AVGO/TXN/QCOM/AMAT) cycle-dependent
  (TXN +1% bottom vs AVGO +44%). Post-split, `mature × Hardware` and
  `mature × Software` are coherent; `mature × Semi` stays cycle-bimodal
  and cascades to `STRATEGY_SECTOR_GROUP_ONLY` (all-stage semis).

---

## 7. Test plan

Add to `tests/test_peer_groups.py` (existing module):

1. **Schema validation.** Every Tech ticker in `universe.json` has a
   `tech_sub_bucket` in the allowed set; no non-Tech ticker has it.
2. **Cohort size assertion.** `Semiconductors >= 6` and `Software >= 6`;
   document that `Hardware` (3) and `IT Services` (4) intentionally
   fall below floor and cascade.
3. **Distribution check.** For each sub-bucket with n ≥ 6, assert
   adoption_momentum stdev < parent Tech stdev. Target only `Software`
   (Tech 25.8 → Software 22.8 ✓) to stay deterministic; Semiconductors
   29.2 ✗ is expected per §3.
4. **AAPL-specific.** `get_peer_cohort_with_strategy("AAPL", ...)`
   returns a cohort containing none of {NVDA, AMD, MU, MRVL, SMCI,
   AVGO, QCOM}; strategy == `"maturity_only"`.
5. **Smoke.** `get_compound_peer_key("AAPL", ...)` returns a 3-tuple
   ending in `"Hardware"`; returns a 2-tuple for `"JPM"`.

---

## 8. Implementation effort

| Slice | Effort |
|---|---|
| `universe.json` edits (add `tech_sub_bucket` to 43 rows, bump version) | XS |
| `peer_groups.json` edits (split `tech_hardware` → `tech_hardware` + `tech_semiconductors`; spin off `tech_it_services`) | XS |
| `peer_groups.py` resolver (3-tuple key for Tech, helper, cascade) | S |
| Tests (5 cases above) | XS |
| **Total** | **S** |

vs the audit's M for the original compound-key implementation — the
curation work is what this spec delivers.

---

## 9. Roadmap / what to do next

1. Ship this spec (this commit).
2. Implementation agent: `universe.json` + `peer_groups.json` +
   `peer_groups.py` resolver + tests. Single PR.
3. Re-run snapshot; verify AAPL Adoption moves from 28.1. Expected
   direction: up. Audit predicts AAPL → ~46-58 range
   (`peer-group-audit.md` line 419 / 664).
4. Flip `docs/lthcs-open-items-audit.md` Tier 2 row 7 from "DEFERRED"
   to "SHIPPED" with the SHA.
5. Phase 2 candidate (out of scope): apply the same sub-bucket pattern
   to Health Care and Consumer Discretionary.
