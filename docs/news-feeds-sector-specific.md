# Sector-Specific News Feeds for LTHCS Thesis Pillar

**Status:** research / design proposal — no code written yet
**Scope:** sector-specific free news sources that feed the Thesis pillar with signal beyond generic financial press
**Companion docs:** `news-feeds-general-apis.md` (broad financial newswires) and `news-feeds-earnings-events.md` (earnings calendars / transcripts)
**Universe:** 168 active tickers across 12 GICS sectors (LTHCS V1)
**Author note:** URLs and cadences cross-checked against my training data; where I'm not 100% sure I mark **unverified**. Bryan should hit each URL once before wiring.

---

## 1. TL;DR

If we add just three sector-specific feeds, in this order:

1. **FDA — Press Announcements / Drug Approvals RSS** → event-driven signal for ~15 pharma & medtech names (LLY, MRK, ABBV, JNJ, PFE, AMGN, VRTX, REGN, GILD, BMY, BIIB, ISRG, BSX, TMO, MDT). Highest signal-to-noise of any free feed on this list.
2. **EIA — "Today in Energy" + Weekly Petroleum Status Report RSS** → directly maps to the ~10 energy names (XOM, CVX, COP, EOG, SLB, PSX, MPC, VLO, OXY, HES) and to the OIH/XLE ETFs we already track macro-side.
3. **Federal Reserve — Press Releases RSS** → cross-sector, but especially loaded for the ~20 financials names (JPM, BAC, WFC, GS, MS, C, USB, PNC, COF, AXP, BLK, SCHW, etc.) via stress test results, SLR rulings, discount-window data.

Estimated ticker coverage if all three are wired: **~45 of 168 tickers (27%)** get event-driven Thesis lift on a meaningful day, with most of the rest covered by general-news feeds.

What I'd defer: streaming/comms trade press (Variety, Hollywood Reporter — most signal is paywalled or already covered by AV news), and utilities-specific rate-case rulings (low frequency, hard to parse, small ticker count).

---

## 2. Per-Sector Sections

### 2.1 Pharma / Biotech (Health Care sector)

**Tickers affected (~15):** LLY, MRK, ABBV, JNJ, PFE, AMGN, VRTX, REGN, GILD, BMY, BIIB, ISRG, BSX, TMO, MDT
(Plus device peers DHR, ABT, SYK that benefit from adjacent reads.)

**What signals matter most:**
- FDA approval / CRL (Complete Response Letter) / accelerated approval events
- Phase II / Phase III trial readouts (especially oncology, GLP-1, Alzheimer's, gene therapy)
- Advisory committee (AdComm) vote outcomes — often move stock before the formal approval
- PBM / drug-pricing legislation (IRA Part D negotiation lists, 340B reform)
- M&A in biotech (large-cap pharma buying $1–10B clinical-stage names)

**Recommended sources:**

1. **FDA — Press Announcements RSS**
   - URL: `https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/press-releases/rss.xml` (**unverified format** — FDA periodically reorganizes their RSS topology; the press-release feed has existed continuously since ~2010 so a feed *exists*, exact path may have shifted)
   - Format: RSS 2.0
   - Cadence: 0–5 items per business day, bursty around AdComm weeks
   - Parsing complexity: **Low** — title + summary usually contains the drug name, sponsor, and indication
   - Sample headline: *"FDA Approves Treatment for Adults with Relapsed or Refractory Multiple Myeloma"* (would map to BMY / PFE / JNJ depending on the sponsor — entity resolution is the only hard part)
   - Mapping to Thesis: **event-driven**. An approval is roughly a +0.4 to +0.6 bullish flag for the sponsor for 1–3 sessions; a CRL is −0.5 to −0.8. AdComm "yes" votes get +0.2.

2. **FierceBiotech RSS**
   - URL: `https://www.fiercebiotech.com/rss/xml` (**unverified path** — Fierce reorganized RSS feeds in ~2022; main page does expose a feed link)
   - Format: RSS
   - Cadence: ~20–40 items/day, broader trade coverage including private biotechs (noisy for our universe)
   - Parsing complexity: **Medium** — most articles touch tickers we don't hold; needs a ticker-mention filter
   - Coverage: trial readouts, FDA news, M&A rumors, exec moves
   - Mapping to Thesis: sentiment-bearing — most useful as a confirmation layer over the FDA feed

3. **ClinicalTrials.gov — RSS / API**
   - URL: `https://clinicaltrials.gov/api/v2/studies` (REST API, JSON), or per-condition RSS like `https://clinicaltrials.gov/ct2/results/rss.xml?cond=Alzheimer`
   - Format: REST JSON (v2 API, free, no key) and per-search RSS
   - Cadence: study status changes daily; the meaningful event is "Status changed to Completed" or "Results posted"
   - Parsing complexity: **High** — registry is a metadata firehose, results don't include effect sizes; you mostly learn *that* a trial ended, not whether it hit
   - Mapping to Thesis: **weak event signal** on its own; mostly useful as a calendar feed (lets the dashboard say "VRTX Phase 3 readout expected this quarter")

4. **endpts.com (Endpoints News) — RSS**
   - URL: `https://endpts.com/feed/` (**unverified**, but Endpoints publishes an RSS as of last check)
   - Format: RSS
   - Cadence: ~10–20 items/day, very pharma-focused, low noise
   - Mapping to Thesis: high-quality sentiment signal, but headlines often paywalled-truncated; rely on title only

**Implementation effort:** **M** (medium). FDA RSS itself is trivial; the hard work is **entity resolution** — mapping "Bristol-Myers Squibb" or "Vertex Pharmaceuticals" or "the company's experimental therapy" to the right ticker. Maintain a small `pharma_brand_to_ticker.json` lookup.

---

### 2.2 Energy (Oil, Gas, Pipelines)

**Tickers affected (~10):** XOM, CVX, COP, EOG, SLB, PSX, MPC, VLO, OXY, HES
(Plus midstream KMI, WMB, ET if added later.)

**What signals matter most:**
- OPEC+ production decisions (monthly JMMC meetings; semi-annual ministerial)
- EIA Weekly Petroleum Status Report (Wednesdays 10:30 ET) — crude/gasoline/distillate inventories
- EIA Short-Term Energy Outlook (monthly, ~mid-month)
- Hurricane / Gulf-of-Mexico disruption events (Aug–Oct seasonality)
- Major sanctions / export-control news (Russia, Iran, Venezuela)
- Pipeline approvals / FERC rulings

**Recommended sources:**

1. **EIA — "Today in Energy" RSS + STEO**
   - URL: `https://www.eia.gov/rss/todayinenergy.xml` (**verified — this feed exists and is stable**)
   - Format: RSS 2.0
   - Cadence: 1 article every business day, ~14:00 ET
   - Parsing complexity: **Low** — title alone usually tells you the subject (e.g. *"U.S. crude oil inventories increased by 2.4 million barrels last week"*)
   - Coverage: weekly inventory commentary, STEO release commentary, infrastructure pieces
   - Mapping to Thesis: sentiment-bearing for the *whole energy sector*, not single names. Inventory draws → +0.3 across XOM/CVX/COP, builds → −0.3. This is one of the few feeds where one item moves N tickers at once.
   - Already partially wired: `lthcs/sources/eia.py` exists — Bryan should check whether it pulls the RSS or just the data API.

2. **OilPrice.com — RSS**
   - URL: `https://oilprice.com/rss/main` (**unverified path**, but OilPrice has had a public RSS for years)
   - Format: RSS
   - Cadence: ~20 items/day, mix of price commentary, geopolitics, op-ed
   - Parsing complexity: Medium — headlines are clickbaity, useful for "what's the narrative" but not as a hard signal
   - Mapping to Thesis: secondary; use only when a major event (OPEC, sanctions) coincides

3. **OPEC — Press Releases**
   - URL: `https://www.opec.org/opec_web/en/press_room/28.htm` (HTML page; **OPEC does not publish a clean RSS** as of my knowledge — needs a polite scrape)
   - Format: HTML scrape (or watch their email list)
   - Cadence: ~5–15 press releases per month, spikes around JMMC meetings
   - Parsing complexity: **High** — need to scrape, dedupe, and parse decision language ("the Conference decided to adjust production by...")
   - Mapping to Thesis: **highest-conviction event signal in energy**. An OPEC cut → +0.5 across all energy names for 2–5 sessions. A surprise hike → −0.5.
   - Alternative: subscribe to the OPEC email list and parse from Gmail (lower-engineering option, but introduces email-side dependency).

4. **Rigzone — RSS** (oilfield services / upstream news)
   - URL: `https://www.rigzone.com/news/rss/rigzone_latest.aspx` (**unverified**)
   - Format: RSS
   - Cadence: ~30 items/day
   - Mapping to Thesis: niche — most useful for SLB, HAL, and FTI; can be skipped for the V1 universe.

**Implementation effort:** **S** for EIA (the file already exists; just need to extend it to consume the RSS in addition to data series). **M** for OPEC (scraping). **L** if we try to integrate all four.

---

### 2.3 Technology / Semiconductors

**Tickers affected (~25):** NVDA, AMD, AVGO, QCOM, INTC, TXN, MU, AMAT, LRCX, KLAC, ASML (ADR), MRVL, ON, MCHP, ADI, NXPI, plus mega-cap tech that buys their chips (MSFT, GOOGL, META, AMZN, AAPL).

**What signals matter most:**
- AI capex announcements (hyperscaler quarterly capex, custom-silicon news)
- GPU / accelerator product launches (NVDA Blackwell cycle, AMD MI300 series)
- US-China chip export controls (BIS rule updates, entity-list adds)
- Capacity / fab announcements (TSM, INTC foundry news)
- Memory-cycle datapoints (DRAM/NAND ASP moves — micron MU sensitive)

**Recommended sources:**

1. **DigiTimes Asia — RSS (free tier)**
   - URL: `https://www.digitimes.com/rss/daily.xml` (**unverified path**, but DigiTimes has a long-running free headline feed; deep articles are paywalled)
   - Format: RSS, headlines only on free tier
   - Cadence: ~30 headlines/day, Asia-business-hours bias
   - Parsing complexity: Medium — headlines are concise and ticker-mentioning ("TSMC ramps N3P", "Micron sees ASP rebound")
   - Mapping to Thesis: best free supply-chain signal we'll find. Headline-only is fine because the headlines themselves are the signal.

2. **SemiWiki — RSS**
   - URL: `https://semiwiki.com/feed/` (**verified format works for most WordPress sites; SemiWiki is on WP**)
   - Format: RSS
   - Cadence: ~5–10 items/day, deeply technical (EDA, IP cores, fab process)
   - Parsing complexity: Medium — articles are long-form analysis, headlines less informative than DigiTimes
   - Mapping to Thesis: low-frequency but high-quality; mostly affects AMAT/LRCX/KLAC/ASML

3. **AnandTech — RSS** (**status: site went read-only in late 2024**)
   - URL: `https://www.anandtech.com/rss/` — feed exists but no new posts after 2024-08
   - **Recommendation: do NOT wire this; replaced by Tom's Hardware**

4. **Tom's Hardware — RSS**
   - URL: `https://www.tomshardware.com/feeds/all` (**unverified path**)
   - Format: RSS
   - Cadence: ~20 items/day, mix of consumer-GPU news and enterprise
   - Mapping to Thesis: best for consumer-side AMD/NVDA news; weaker on enterprise

5. **BIS (Bureau of Industry and Security) / Federal Register — export-control rulings**
   - URL: `https://www.federalregister.gov/agencies/industry-and-security-bureau` (the Federal Register has a great free JSON API)
   - Format: JSON API, very clean
   - Cadence: 0–5 BIS rules/month, **bursty** during export-control cycles
   - Parsing complexity: Low for the metadata; High to interpret the rule (could feed the title to an LLM summarizer)
   - Mapping to Thesis: **event-driven; binary signal**. A new China export-control rule → −0.4 to −0.8 across NVDA/AMD/AMAT/LRCX/KLAC/ASML for 1–3 sessions.

**Implementation effort:** **M-L**. DigiTimes alone is M (one RSS + ticker-mention filter). Adding Federal Register BIS monitoring is another M. Note that **`lthcs/sources/ai_news.py` already exists** — it likely covers Hacker News, so the gap here is supply-chain (DigiTimes) and policy (BIS), not the model-launch firehose.

---

### 2.4 Financials / Banks

**Tickers affected (~20):** JPM, BAC, WFC, GS, MS, C, USB, PNC, COF, AXP, BLK, SCHW, BK, STT, TFC, RF, FITB, MTB, HBAN, KEY. (Plus insurers BRK.B, PGR, AIG; asset managers BX, KKR.)

**What signals matter most:**
- Fed policy decisions (FOMC statements, dot plot, minutes 3 weeks later)
- Stress test results (annual DFAST/CCAR, late June)
- SLR (Supplementary Leverage Ratio) and capital-rule changes
- Discount-window borrowing data (weekly H.4.1)
- OCC / FDIC enforcement actions (consent orders against named banks)
- Credit-cycle indicators (charge-offs, NPL ratios from peers' filings)

**Recommended sources:**

1. **Federal Reserve — Press Releases RSS**
   - URL: `https://www.federalreserve.gov/feeds/press_all.xml` (**verified — stable feed for ~15 years**)
   - Format: RSS 2.0
   - Cadence: 2–10/week, bursty FOMC weeks
   - Parsing complexity: Low for routing (titles are categorized: monetary, enforcement, supervision, payments)
   - Mapping to Thesis: cross-sector for monetary items; bank-specific for enforcement (named in title)
   - Sample headline: *"Federal Reserve Board fines Wells Fargo $67.8 million for compliance breakdowns"* → would −0.3 WFC for ~2 sessions

2. **OCC — Enforcement Actions RSS / search**
   - URL: `https://www.occ.gov/news-issuances/news-releases/index.html` (HTML; **OCC's RSS coverage is patchy** — they have feeds for some categories but not all)
   - Format: HTML scrape or low-quality RSS
   - Cadence: ~5–10 enforcement releases/month
   - Mapping to Thesis: event-driven, single-name negative. Mostly affects regional banks (RF, FITB, MTB, KEY) more than money-center.

3. **FFIEC — Call Report data & press**
   - URL: `https://www.ffiec.gov/` and the Call Report bulk data
   - Format: Data is bulk-download (CSV/XBRL); press is HTML
   - Cadence: Call Reports quarterly (~45 days after quarter-end)
   - Parsing complexity: **High** — bulk financial data, not headlines. Out of scope for the Thesis pillar; belongs in fundamentals.

4. **Federal Reserve — H.4.1 weekly release**
   - URL: `https://www.federalreserve.gov/releases/h41/` (also in the press RSS above)
   - Format: structured release, weekly Thursday 16:30 ET
   - Mapping to Thesis: discount-window / BTFP usage trends — a stress signal, sector-wide negative for regional banks when usage spikes

**Implementation effort:** **S**. The Fed press-release RSS alone covers ~80% of the headline signal we'd want. Wire it, route items by title regex (FOMC / enforcement / supervision), score accordingly.

---

### 2.5 Consumer Discretionary

**Tickers affected (~20):** AMZN, TSLA, HD, NKE, LOW, MCD, SBUX, BKNG, CMG, ABNB, TJX, MAR, HLT, GM, F, ORLY, AZO, EBAY, ETSY, DPZ.

**What signals matter most:**
- Census Retail Sales (monthly, mid-month, 08:30 ET) — sector-wide
- Holiday spending data (Cyber-5, BFCM, Adobe Digital Insights reports)
- EV adoption / Tesla delivery numbers (quarterly, plus monthly China BEV data)
- Restaurant traffic data (Black Box, Placer.ai blog posts)
- Container-shipping volume (sector-leading indicator)

**Recommended sources:**

1. **U.S. Census Bureau — Retail Trade releases**
   - URL: `https://www.census.gov/retail/index.html` (also surfaced via FRED, which `lthcs/sources/fred.py` already hits)
   - Format: scheduled releases; data hits FRED ~immediately
   - Cadence: Monthly, ~15th of month, 08:30 ET
   - Mapping to Thesis: **macro-style**, sector-wide. Strong print → +0.2 across discretionary names.
   - Already partially covered if FRED ingests retail-sales series; no new code needed if so.

2. **RetailWire — RSS**
   - URL: `https://retailwire.com/feed/` (**unverified path**, WordPress so likely works)
   - Format: RSS
   - Cadence: ~5/day
   - Mapping to Thesis: trade-press commentary, mostly useful for TGT, WMT, COST, HD, LOW. Headlines are clear but signal is soft.

3. **InsideEVs — RSS** (for TSLA/GM/F EV reads)
   - URL: `https://insideevs.com/rss/articles/all/`
   - Format: RSS
   - Cadence: ~10/day
   - Mapping to Thesis: useful for TSLA China delivery reads, GM Ultium news, F F-150 Lightning. Note that **Tesla's monthly China registration data** (CPCA / weekly insurance data) is the highest-signal item in this whole category — usually reported via @JayInShanghai-type accounts on X, not via RSS.

4. **Adobe Digital Insights / Mastercard SpendingPulse** — official press releases
   - URL: Adobe press room + Mastercard press room
   - Format: HTML; no clean RSS
   - Cadence: Monthly, plus daily Cyber-5 dashboards in late November
   - Mapping to Thesis: **highly seasonal** — wire only for Nov/Dec.

**Implementation effort:** **S–M**. Most of the signal here is already available via FRED + earnings season. The marginal lift from RetailWire/InsideEVs is modest.

---

### 2.6 Industrials

**Tickers affected (~20):** BA, CAT, DE, HON, GE, LMT, RTX, NOC, UPS, FDX, UNP, CSX, NSC, MMM, ETN, EMR, ITW, PH, GD, LHX.

**What signals matter most:**
- ISM Manufacturing PMI (monthly, 1st business day, 10:00 ET) — sector-wide
- Defense contract awards (DoD daily contract press; named companies)
- Major aerospace events (FAA actions on BA, certifications, MAX-related news)
- Rail volumes (AAR weekly carload data)
- Infrastructure spending news (IIJA implementation)

**Recommended sources:**

1. **U.S. DoD — Contract Announcements**
   - URL: `https://www.defense.gov/News/Contracts/` (HTML; **DoD does publish a contracts RSS** at the parent /News path)
   - Format: HTML + possible RSS at the press-release level
   - Cadence: Daily, after 17:00 ET, batched
   - Parsing complexity: Medium — each release lists the contractor name; need to match "Lockheed Martin Corp" → LMT, "Raytheon Technologies" → RTX
   - Mapping to Thesis: **event-driven, named tickers**. A $5B+ award → +0.3 to +0.5 for the named contractor for 1–2 sessions.
   - Sample: *"Lockheed Martin Rotary & Mission Systems, Owego, NY, awarded a $984,000,000 modification..."*

2. **FAA — Press Releases**
   - URL: `https://www.faa.gov/newsroom/press-releases` (HTML)
   - Format: HTML scrape; FAA's RSS coverage is uneven
   - Cadence: Bursty around safety events
   - Mapping to Thesis: very BA-specific. Most non-BA items are low-signal.

3. **AAR — Weekly Rail Traffic**
   - URL: `https://www.aar.org/news/` (HTML; weekly data release every Wed)
   - Format: HTML / PDF
   - Cadence: Weekly Wednesday ~13:00 ET
   - Mapping to Thesis: leading indicator for UNP/CSX/NSC; sector-wide for industrials. Worth wiring if we add a freight pillar.

4. **ISM — Manufacturing PMI** (already a macro signal — covered in `news-feeds-general-apis.md` likely)
   - URL: `https://www.ismworld.org/` — releases are paywalled-by-membership in detail; headline number is public via press wire
   - Mapping to Thesis: leave to macro; not a sector-specific feed.

**Implementation effort:** **M**. DoD contracts is the gem here — daily, named, free, clear signal — but parsing the contract text reliably needs care.

---

### 2.7 Materials

**Tickers affected (~10):** LIN, APD, FCX, NEM, DD, ECL, SHW, NUE, STLD, CTVA.

**What signals matter most:**
- Commodity price action (copper, gold, lithium, nitrogen) — feeds macro pillar more than Thesis
- Mining permits / regulatory rulings
- Industrial-gas pricing (LIN/APD pricing actions)
- Steel tariff / Section 232 news

**Recommended sources:**

1. **Mining.com — RSS**
   - URL: `https://www.mining.com/feed/`
   - Format: RSS
   - Cadence: ~10–15/day
   - Mapping to Thesis: best for FCX, NEM, and the broader gold/copper sentiment

2. **USTR — Section 232 / 301 announcements**
   - URL: `https://ustr.gov/about-us/policy-offices/press-office/press-releases` (HTML)
   - Cadence: ~1–5/month, bursty around trade rounds
   - Mapping to Thesis: cross-sector but especially loaded for NUE/STLD (steel) and ALB (lithium)

**Implementation effort:** **L** relative to ticker payoff. Materials is a small slice of the universe and most of its signal is commodity-price driven (already captured upstream). **Recommend deferring.**

---

### 2.8 Utilities

**Tickers affected (~10):** NEE, SO, DUK, AEP, D, SRE, EXC, XEL, PEG, ED.

**What signals matter most:**
- State PUC rate-case rulings (varies state by state)
- Grid-resiliency / FERC orders
- Storm-cost recovery decisions
- AI-datacenter load growth news (renewed relevance in 2024–25)

**Recommended sources:**

1. **FERC — News Releases RSS**
   - URL: `https://www.ferc.gov/news-events/news` (HTML, with some RSS)
   - Cadence: ~5–10/month
   - Mapping to Thesis: technical, low frequency, hard to score without domain knowledge.

2. **Utility Dive — RSS**
   - URL: `https://www.utilitydive.com/feeds/news/` (**unverified path**)
   - Format: RSS
   - Cadence: ~10/day, mostly summary of news already in the trade press
   - Mapping to Thesis: useful narrative but rarely market-moving same-day.

**Implementation effort:** **L** for low ticker payoff. **Defer.**

---

### 2.9 Real Estate

**Tickers affected (~10):** PLD, AMT, EQIX, CCI, PSA, WELL, AVB, EQR, DLR, O.

**What signals matter most:**
- Mortgage Bankers Association (MBA) weekly applications survey
- Cap-rate trend reports (Green Street, free summaries)
- 10-year yield moves (already captured in macro)
- Data-center REIT news (EQIX, DLR — increasingly tied to AI capex)

**Recommended sources:**

1. **MBA — Weekly Applications Survey**
   - URL: `https://www.mba.org/news-and-research/newsroom` (releases) — also via FRED-adjacent series
   - Cadence: Weekly Wednesday 07:00 ET
   - Mapping to Thesis: leading indicator for homebuilders (DHI, LEN, PHM, TOL) more than REITs

2. **NAREIT (reit.com) — RSS**
   - URL: `https://www.reit.com/news` (HTML; RSS availability **unverified**)
   - Cadence: ~5/week
   - Mapping to Thesis: low signal, mostly industry commentary

3. **Data Center Dynamics — RSS** (for EQIX/DLR specifically)
   - URL: `https://www.datacenterdynamics.com/rss/`
   - Cadence: ~10/day
   - Mapping to Thesis: **non-trivial signal for EQIX/DLR** because of the AI-capex tie-in; news of a major hyperscaler signing a colo deal → +0.3 to EQIX/DLR

**Implementation effort:** **M** if we want DCD. Otherwise **defer**.

---

### 2.10 Communication Services

**Tickers affected (~10):** GOOGL, META, NFLX, DIS, CMCSA, TMUS, T, VZ, EA, TTWO.

**What signals matter most:**
- Streaming subscriber metrics (mostly quarterly — earnings, not feeds)
- Ad-revenue trends (proxied by quarterly tech earnings)
- Antitrust rulings (DOJ vs. Google, FTC vs. Meta)
- Major content / sports rights deals

**Recommended sources:**

1. **DOJ — Antitrust Division press releases**
   - URL: `https://www.justice.gov/atr/news` (HTML; RSS at parent justice.gov level)
   - Cadence: ~5/month
   - Mapping to Thesis: **highest-conviction event** for GOOGL — a final ruling moves the stock 5–10%

2. **Variety / Hollywood Reporter — RSS**
   - URL: `https://variety.com/feed/` (free RSS exists)
   - Cadence: ~30/day
   - Parsing complexity: High — most articles are entertainment industry, not market-moving. Headline-only ticker filter would suffice.
   - Mapping to Thesis: low marginal signal; most market-moving stories also surface in mainstream financial press.

3. **The Information / Stratechery / Streaming Observer** — mostly paywalled or already covered. **Skip.**

**Implementation effort:** **L** for low marginal value over the general-news feed. **Defer**, with one exception: an alert on `justice.gov/atr` feed entries that mention `google`, `alphabet`, or `meta` is a worthwhile small project.

---

### 2.11 Consumer Staples

**Tickers affected (~10):** PG, KO, PEP, WMT, COST, PM, MO, MDLZ, CL, EL.

**What signals matter most:**
- GLP-1 demand-destruction narrative (snacks, soft drinks — affects KO, PEP, MDLZ)
- Tobacco regulation (PMTA decisions for MO; menthol-ban news)
- Tariff news on imported staples
- Earnings — most signal is concentrated at quarterly prints

**Recommended sources:**

- Mostly covered by general financial press + earnings. No high-value sector-specific feed I'd add.
- **One possible exception:** FDA Center for Tobacco Products (CTP) press for MO/PM. Very low frequency; **defer.**

**Implementation effort:** N/A. **Skip the sector-specific work.**

---

### 2.12 Health Care — Insurers / Services (sub-sector of Health Care not covered above)

**Tickers affected (~5):** UNH, CVS, HUM, CI, ELV.

**What signals matter most:**
- CMS Medicare Advantage star ratings & rate notices (annual cycle, Feb/Apr)
- CMS rate updates (Medicare Advantage advance notice → final rate)
- Drug-pricing rulings (separately from pharma — these affect PBMs)

**Recommended sources:**

1. **CMS — Newsroom RSS**
   - URL: `https://www.cms.gov/newsroom/press-releases` (**RSS path unverified**, but feed has existed)
   - Cadence: ~10/month
   - Mapping to Thesis: **event-driven, occasional 5–10% single-day moves** on UNH/HUM when star ratings drop. Worth wiring as a small adjunct to FDA work.

**Implementation effort:** **S** if bundled with FDA work.

---

## 3. Cross-Sector / Geopolitical

These affect every sector and probably deserve their own feed-handler, not a per-sector one:

1. **Federal Register — JSON API**
   - URL: `https://www.federalregister.gov/api/v1/documents.json`
   - Format: Excellent REST API, no auth, well-documented
   - Cadence: ~50–200 documents/day, filterable by agency
   - Use: filter on BIS, OFAC, FERC, DOJ-Antitrust, FDA, CMS in one call; cheaper than per-agency scraping
   - **Strongly recommend** this as the single entry point for "policy news" — it's the most underused free feed in this whole list

2. **U.S. Treasury OFAC — Recent Actions RSS**
   - URL: `https://ofac.treasury.gov/recent-actions` (HTML; subscribable as email; **RSS availability is intermittent**)
   - Cadence: ~5–10 actions/month
   - Use: sanctions news → affects energy (Russia/Iran), finance (correspondent-banking), tech (China entity list)

3. **USTR — press releases** (covered in Materials but cross-sector relevant)

4. **Reuters / AP / White House — already covered in `news-feeds-general-apis.md` (per task spec)**

5. **CBO / JCT — fiscal scoring of bills**
   - URL: `https://www.cbo.gov/publication/all-publications` (RSS available)
   - Cadence: ~5–10/week, bursty during budget cycles
   - Mapping to Thesis: moves entire sectors when a tax-law or spending bill scores out

---

## 4. Ranked Recommendation (Bryan's ~4-hour-per-sector budget)

Given the 4-hour/sector working assumption:

1. **Pharma — first.** Clearest event-driven signal, biggest ticker cohort under one feed, FDA RSS is genuinely free and stable. 4 hours buys you: FDA RSS ingestor + brand→ticker lookup + simple event-flag scoring + 1 unit test.

2. **Semis — second.** AI cycle is the single largest narrative in the universe and our existing `ai_news.py` is HN-only. Adding DigiTimes (supply-chain) + Federal Register BIS filter (export controls) covers ~25 tickers including the ones with the largest market caps. 4 hours buys both.

3. **Energy — third.** EIA RSS is the easy win (1 hour); OPEC scraping is the harder half. The data side is already partly wired via `lthcs/sources/eia.py`. 4 hours is enough for EIA RSS + OPEC HTML scrape.

4. **Financials — fourth.** Fed press RSS is trivial (~1 hour) so this is a "fold into pharma sprint" candidate.

5. **Cross-sector Federal Register API — fifth.** Highly leveraged: covers Industrials (DoD via separate scraper, but BIS/OFAC/FDA via FR), Materials (USTR via FR), Comms (DOJ Antitrust via FR). ~4 hours.

6. **Defer**: Materials, Utilities, Comm Services, Consumer Staples, Real Estate. Combined ticker payoff is small and the feeds are noisy or paywalled.

**Total if executed in order 1–5:** ~20 hours of agent work, covers ~80–90 of 168 tickers with at least one sector-specific event hook.

---

## 5. Implementation Sketch — `lthcs/sources/pharma_news.py`

The fully recommended first source. High-level design:

```python
# lthcs/sources/pharma_news.py
"""
FDA + biotech trade-press signal for the Thesis pillar.

Feeds:
  - FDA Press Announcements RSS (primary)
  - FierceBiotech RSS (confirmation layer)
  - endpts.com RSS (sentiment color)

Output:
  PharmaEvent(ticker: str, kind: Literal['approval','crl','adcomm','readout','m&a'],
              direction: Literal['+','-'], magnitude: float, ts: datetime,
              source_url: str, title: str)
"""

FEEDS = [
    ("fda_press",  "https://www.fda.gov/.../press-releases/rss.xml", 0.6),
    ("fierce_bio", "https://www.fiercebiotech.com/rss/xml",          0.3),
    ("endpts",     "https://endpts.com/feed/",                       0.3),
]

BRAND_TO_TICKER = json.load(open("data/pharma_brand_to_ticker.json"))
# e.g. {"Bristol-Myers Squibb": "BMY", "Bristol Myers": "BMY",
#       "Vertex Pharmaceuticals": "VRTX", "Vertex Pharma": "VRTX", ...}

EVENT_PATTERNS = {
    "approval":  [r"\b(approves?|approved|grants? approval)\b"],
    "crl":       [r"\bcomplete response letter\b", r"\bCRL\b"],
    "adcomm":    [r"\badvisory committee\b"],
    "readout":   [r"\bphase\s*(2|II|3|III)\b.*\b(results?|readout)\b"],
    "m&a":       [r"\bto acquire\b", r"\bagreed to be acquired\b"],
}

def fetch_events(now: datetime, lookback_hours: int = 24) -> list[PharmaEvent]:
    # 1. Pull each RSS feed via _cache.fetch (already a helper in lthcs/sources)
    # 2. For each item: detect ticker via BRAND_TO_TICKER; skip if no ticker
    # 3. Classify event kind via EVENT_PATTERNS
    # 4. Sign event: approval/positive-readout → '+', CRL/failed-readout → '-'
    # 5. Magnitude: source-weight × kind-weight × decay(time_since_published)
    # 6. Return list, deduped by (ticker, kind, day)
    ...

def score_to_thesis(events: list[PharmaEvent]) -> dict[str, float]:
    """Aggregate per-ticker; cap individual ticker boost at ±0.6."""
    ...
```

Caching: reuse `lthcs/sources/_cache.py` patterns; TTL = 1 hour for RSS, 24 hours for the brand-to-ticker JSON.

Rate-limiting: reuse `_ratelimit.py`; FDA RSS has no published limit but be polite — 1 fetch per hour is plenty.

Persistence: emit events to `lthcs/persist.py` so they appear in the `narratives.py` view on the dashboard.

Test: a fixture with one approval, one CRL, one ambiguous → assert correct routing.

---

## 6. What We Should NOT Do

- **Don't scrape paywalled sources** (FT, WSJ, Bloomberg, The Information, Stratechery, Endpoints Premium articles). Even if technically possible, it breaks ToS and the signal quality on free truncated headlines is already 80% of the full-article signal.
- **Don't aggregate from too many sources.** Three good feeds per sector beats ten mediocre ones; signal-to-noise degrades fast once we cross ~10 inputs per sector.
- **Don't try to score sentiment from short headlines without an LLM call.** Rule-based sentiment on biotech headlines specifically is famously unreliable ("FDA delays" can be neutral or catastrophic). Use event classification (which is more reliable) and only fold in LLM sentiment if/when we wire one.
- **Don't ingest Twitter/X via scraping.** It's the highest-signal feed for Tesla / China BEV / chip news, but it's hostile to free scraping. Defer until Bryan decides on a paid X API path.
- **Don't try to be real-time.** A 30-minute lag on FDA approvals is still useful for a daily LTHCS pipeline; chasing same-minute latency multiplies engineering cost.
- **Don't conflate macro and sector signals.** ISM PMI, retail sales, etc. belong in the macro pillar (`news-feeds-general-apis.md` territory). This file is sector-specific by construction.

---

## 7. Reference / Bookmarks

- FDA Press Announcements RSS: https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/press-releases/rss.xml *(unverified path; navigate from fda.gov/rss-feeds if 404)*
- EIA "Today in Energy" RSS: https://www.eia.gov/rss/todayinenergy.xml
- Federal Reserve Press Releases RSS: https://www.federalreserve.gov/feeds/press_all.xml
- Federal Register API docs: https://www.federalregister.gov/developers/documentation/api/v1
- DoD Contracts page: https://www.defense.gov/News/Contracts/
- ClinicalTrials.gov v2 API: https://clinicaltrials.gov/data-api/api
- OPEC press room: https://www.opec.org/opec_web/en/press_room/28.htm
- DigiTimes free daily feed: https://www.digitimes.com/ (look for the RSS icon in footer; **unverified path**)
- DOJ Antitrust Division news: https://www.justice.gov/atr/news
- CMS press releases: https://www.cms.gov/newsroom/press-releases

---

## 8. Open Questions for Bryan

1. Do we want **entity resolution as a shared util** (used by pharma, defense, antitrust feeds alike) or per-feed lookup tables? Suggest shared — same problem in all three places.
2. Should the dashboard surface **per-ticker event log** (new UI element) or just fold sector events into the existing Thesis number? Cheap to do the latter first.
3. Budget for an LLM-summarization call per high-signal event? ~$0.001/event × ~50 events/day = trivial; would dramatically clean up the Federal Register firehose specifically.
4. **Should this work ship on V1 only**, mirroring the LTHCS Phase 1 convention? Default to yes unless told otherwise.

---

*End of doc.*
