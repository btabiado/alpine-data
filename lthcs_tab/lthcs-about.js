/**
 * lthcs-about.js — "About LTHCS" info modal.
 *
 * Opens a small read-only modal explaining the framework, scoring inputs,
 * data sources, and known V1 limitations. Wired to the #lthcs-about-btn
 * button in the header.
 */

const BAND_LIST = [
  ["elite", "90–100", "Elite Confidence Hold"],
  ["high_confidence", "80–89", "High Confidence Hold"],
  ["constructive", "70–79", "Constructive Hold"],
  ["monitor", "60–69", "Monitor Closely"],
  ["weakening", "50–59", "Confidence Weakening"],
  ["review", "0–49", "Structural Review Required"],
];

const PILLAR_LIST = [
  ["Adoption Momentum", "25%", "Revenue growth & QoQ vs peers + Google Trends search-interest (weekly batch)."],
  ["Institutional Confidence", "20%", "Form 4 insider conviction + 13F top-10 holdings change + 90d price momentum."],
  ["Financial Evolution", "15%", "Revenue growth + gross margin trend + operating cash flow + bank-cohort NII/PCL/noninterest."],
  ["Thesis Integrity", "20%", "Finnhub analyst recommendations (primary) + SEC 8-K material events + Yahoo earnings refinement."],
  ["Demand Environment Score", "20%", "Sector-tilted macro: FRED tier-1 (CPI/Fed Funds/10Y/Δ10Y/unemployment/real 10Y/VIX/M2) + WTI, plus tier-2 (Brent/gasoline/ISM/housing/sentiment/U6)."],
];

const SOURCE_LIST = [
  ["Yahoo Finance (yfinance)", "Daily prices, 90d momentum, 30d volatility, earnings refinement. No API key."],
  ["SEC EDGAR XBRL", "Annual + quarterly revenue, gross profit (with fallback chain), operating cash flow, bank-cohort NII/PCL/noninterest. User-Agent required."],
  ["SEC Form 4 (insider conviction)", "90-day rolling window of insider open-market buys vs sells, with cluster-buying and CEO/CFO flags. Feeds Institutional pillar and per-ticker detail."],
  ["SEC 13F (institutional holdings)", "Quarterly top-10 manager holdings change. Feeds Institutional pillar."],
  ["SEC 8-K (material events)", "Real-time material-event filter. Feeds Thesis pillar."],
  ["Finnhub", "Analyst recommendation distributions; primary Thesis input across 167 tickers."],
  ["Google Trends (pytrends)", "Search-interest acceleration on 11 representative tickers; weekly batch (rate-limit constrained)."],
  ["FRED", "Tier-1 macros (CPI, Fed Funds, 10Y, Δ10Y, unemployment, real 10Y, VIX, M2) + tier-2 (Brent, gasoline, ISM, housing, sentiment, U6). Free API key."],
  ["EIA", "WTI crude oil prices feeding DES energy tilt. Free API key."],
  ["SPDR sector ETFs", "11 sector ETFs (XLK / XLF / XLE / etc.) ranked vs SPY on 1m and 3m total return. Drives the Market Regime strip."],
];

const V1_LIMITATIONS = [
  "Google Trends drives only 11 representative tickers via a weekly batch (Google rate-limits aggressively). Remaining names get a peer-group fallback rather than a per-ticker series.",
  "Margin (XBRL GrossProfit) is missing on roughly 45% of the universe — disproportionately Financials, Comm Services, and services-heavy Consumer Discretionary. A fallback concept chain partially closes the gap; full sector-aware margin is Phase 6.",
  "Bank cohort (NII / PCL / noninterest) covers 11 tickers — enough to break the GrossProfit blind spot for universal & regional banks but still a small cross-sectional pool.",
  "Thesis has < 30 days of live history. Finnhub recommendations only began firing 2026-05-18; SEC 8-K and Yahoo earnings refinement are wired but unvalidated until enough sample accrues.",
  "WBA is marked inactive in the universe (Walgreens taken private late 2025; no longer files with SEC).",
];

/**
 * Data Feeds lineage (Phase 5, as of 2026-05-18).
 * Source-of-truth: docs/lthcs-data-audit-2026-05-18.md
 *   - Today's coverage matrix (n=167 active scored)
 *   - Recommended data sources to add
 *
 * Columns: Pillar, Component, Source, Coverage today, Notes.
 */
const FEED_LINEAGE = [
  ["Adoption", "Revenue / QoQ growth", "SEC EDGAR XBRL", "161 / 167 (96%)", "Wired; ~6 tickers w/ XBRL parse gaps."],
  ["Adoption", "Search-interest acceleration", "Google Trends (pytrends)", "11 / 167 batch", "Weekly batch, rate-limited; peer fallback on remainder."],
  ["Institutional", "Insider conviction (90d)", "SEC Form 4 (EDGAR)", "165 / 167 (99%)", "Wired Phase 5; cluster-buy & CEO/CFO flags."],
  ["Institutional", "Top-10 13F holdings change", "SEC 13F (EDGAR)", "167 / 167 (100%)", "Quarterly cadence; wired Phase 5."],
  ["Institutional", "90d price momentum", "Yahoo Finance (yfinance)", "166 / 167 (99%)", "BRK.B (.B suffix) is the lone miss."],
  ["Financial", "Revenue growth %", "SEC EDGAR XBRL", "162 / 167 (97%)", "Wired."],
  ["Financial", "Gross margin trend", "SEC EDGAR XBRL", "93 / 167 (56%)", "GrossProfit concept missing on services / banks; fallback chain partial."],
  ["Financial", "Operating cash flow", "SEC EDGAR XBRL", "158 / 167 (95%)", "9 missing across 6 sectors; XBRL parse-quality."],
  ["Financial", "Bank NII / PCL / noninterest", "SEC EDGAR XBRL (bank cohort)", "11 / 167 cohort", "Cohort-relative percentiles; only 11 banks in pool."],
  ["Thesis", "Analyst recommendations", "Finnhub", "167 / 167 (100%)", "Primary Thesis driver; live since 2026-05-18."],
  ["Thesis", "Material events", "SEC 8-K (EDGAR)", "167 / 167 (event-day)", "Real-time event filter; refines Thesis."],
  ["Thesis", "Earnings beat / miss", "Yahoo Finance (yfinance)", "167 / 167", "Refinement layer on top of Finnhub."],
  ["DES", "Tier-1 macros (9 signals)", "FRED + EIA", "9 / 9 daily", "WTI, CPI, Fed Funds, 10Y, Δ10Y, unemployment, real 10Y, VIX, M2."],
  ["DES", "Tier-2 macros (6 signals)", "FRED", "6 / 6 daily", "Brent, gasoline, ISM, housing, sentiment, U6."],
  ["DES", "Sector tilt weights", "sector_des_weights.json (static)", "167 / 167 (100%)", "Maps each ticker's sector to a macro-sensitivity vector."],
];

const $ = (sel, root = document) => root.querySelector(sel);

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

let lastFocus = null;

function buildModal() {
  let root = $("#lthcs-about-root");
  if (root) return root;
  root = document.createElement("div");
  root.id = "lthcs-about-root";
  root.className = "lthcs-about-root hidden";
  root.setAttribute("role", "dialog");
  root.setAttribute("aria-modal", "true");
  root.setAttribute("aria-labelledby", "lthcs-about-title");

  const bandRows = BAND_LIST.map(
    ([key, range, label]) =>
      `<tr><td><span class="lthcs-about-dot" data-band="${key}"></span> ${escapeHtml(label)}</td><td><code>${escapeHtml(range)}</code></td></tr>`
  ).join("");

  const pillarRows = PILLAR_LIST.map(
    ([name, weight, desc]) =>
      `<tr><td><strong>${escapeHtml(name)}</strong></td><td><code>${escapeHtml(weight)}</code></td><td>${escapeHtml(desc)}</td></tr>`
  ).join("");

  const sourceList = SOURCE_LIST.map(
    ([name, desc]) =>
      `<li><strong>${escapeHtml(name)}</strong> — ${escapeHtml(desc)}</li>`
  ).join("");

  const limitList = V1_LIMITATIONS.map(
    (text) => `<li>${escapeHtml(text)}</li>`
  ).join("");

  const feedRows = FEED_LINEAGE.map(
    ([pillar, component, source, coverage, notes]) =>
      `<tr>` +
      `<td><strong>${escapeHtml(pillar)}</strong></td>` +
      `<td>${escapeHtml(component)}</td>` +
      `<td>${escapeHtml(source)}</td>` +
      `<td><code>${escapeHtml(coverage)}</code></td>` +
      `<td class="lthcs-about-notes">${escapeHtml(notes)}</td>` +
      `</tr>`
  ).join("");

  root.innerHTML = `
    <div class="lthcs-about-backdrop" data-about-close></div>
    <div class="lthcs-about-panel">
      <header class="lthcs-about-header">
        <h2 id="lthcs-about-title">About LTHCS — V1</h2>
        <button type="button" class="lthcs-about-close" data-about-close aria-label="Close">&times;</button>
      </header>
      <div class="lthcs-about-body">
        <p class="lthcs-about-lead">
          The Long-Term Hold Confidence Score (LTHCS) is a daily 0–100 score for 74 US-listed names
          across the S&amp;P 500, Nasdaq-100, and Dow Jones Industrial Average. It blends fundamental,
          flow, sentiment, and macro signals into a single conviction score with a stage-aware weighting
          system (so a pre-profit growth name is judged differently than a mature compounder).
        </p>

        <h3>Score bands</h3>
        <table class="lthcs-about-table">
          <thead><tr><th>Band</th><th>Range</th></tr></thead>
          <tbody>${bandRows}</tbody>
        </table>

        <h3>Five pillars (default weights for standard compounder)</h3>
        <table class="lthcs-about-table">
          <thead><tr><th>Pillar</th><th>Weight</th><th>Inputs</th></tr></thead>
          <tbody>${pillarRows}</tbody>
        </table>
        <p class="lthcs-about-note">
          Weights vary by <code>maturity_stage</code>. E.g. <code>pre_profit_growth</code> tilts to
          Adoption (30%); <code>recovery_stabilization</code> tilts to Financial Evolution (35%).
        </p>

        <h3>Data sources (all free tier in V1)</h3>
        <ul class="lthcs-about-list">${sourceList}</ul>

        <h3>Data feeds — pillar lineage (Phase 5, 2026-05-18)</h3>
        <p class="lthcs-about-note">
          Which feed actually drives which pillar component today, and the live
          coverage across the 167 active-scored universe. Source-of-truth:
          <code>docs/lthcs-data-audit-2026-05-18.md</code>.
        </p>
        <div class="lthcs-about-table-wrap">
          <table class="lthcs-about-table lthcs-about-feed-table">
            <thead>
              <tr>
                <th>Pillar</th>
                <th>Component</th>
                <th>Source</th>
                <th>Coverage today</th>
                <th>Notes</th>
              </tr>
            </thead>
            <tbody>${feedRows}</tbody>
          </table>
        </div>

        <h3>V1 limitations (honestly disclosed)</h3>
        <ul class="lthcs-about-list">${limitList}</ul>

        <h3>How daily updates work</h3>
        <p>
          A single command <code>python lthcs_daily.py</code> runs the full 8-stage pipeline on Bryan's
          laptop, computes today's snapshot, and writes JSON files under <code>data/lthcs/</code>.
          A subsequent <code>git push</code> deploys the new snapshot to this page in about a minute
          via GitHub Pages. No server. No database. No cloud bill. The append-only daily snapshots in
          git history are the audit log.
        </p>

        <h3>Methodology source</h3>
        <p>
          Implementation specifications live in the repo:
          <a href="https://github.com/btabiado/btc-eth-etf-dashboard/blob/main/PHASE_1_BUILD_SPEC.md" target="_blank" rel="noopener">PHASE_1_BUILD_SPEC.md</a>
          and <a href="https://github.com/btabiado/btc-eth-etf-dashboard/blob/main/README_LTHCS.md" target="_blank" rel="noopener">README_LTHCS.md</a>.
        </p>

        <p class="lthcs-about-disclaimer">
          <strong>Not investment advice.</strong> LTHCS is a research framework for personal conviction
          tracking. Scores do not constitute recommendations to buy, sell, or hold any security. Do
          your own research. Past data does not predict future results.
        </p>
      </div>
    </div>
  `;

  document.body.appendChild(root);

  // Wire closers (backdrop + × + Esc).
  root.addEventListener("click", (e) => {
    if (e.target instanceof Element && e.target.closest("[data-about-close]")) {
      closeAbout();
    }
  });

  return root;
}

function trapTab(e) {
  if (e.key !== "Tab") return;
  const root = $("#lthcs-about-root");
  if (!root || root.classList.contains("hidden")) return;
  const focusable = root.querySelectorAll(
    'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
  );
  if (!focusable.length) return;
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (e.shiftKey && document.activeElement === first) {
    last.focus();
    e.preventDefault();
  } else if (!e.shiftKey && document.activeElement === last) {
    first.focus();
    e.preventDefault();
  }
}

function escClose(e) {
  if (e.key === "Escape") closeAbout();
}

export function openAbout() {
  lastFocus = document.activeElement;
  const root = buildModal();
  root.classList.remove("hidden");
  root.setAttribute("aria-hidden", "false");
  document.addEventListener("keydown", escClose);
  document.addEventListener("keydown", trapTab);
  const closeBtn = root.querySelector(".lthcs-about-close");
  if (closeBtn) closeBtn.focus();
}

export function closeAbout() {
  const root = $("#lthcs-about-root");
  if (!root) return;
  root.classList.add("hidden");
  root.setAttribute("aria-hidden", "true");
  document.removeEventListener("keydown", escClose);
  document.removeEventListener("keydown", trapTab);
  if (lastFocus && typeof lastFocus.focus === "function") {
    try { lastFocus.focus(); } catch (_e) { /* ignore */ }
  }
  lastFocus = null;
}

// Wire the header About button (added in index.html).
document.addEventListener("DOMContentLoaded", () => {
  const btn = document.getElementById("lthcs-about-btn");
  if (btn) btn.addEventListener("click", openAbout);
});
