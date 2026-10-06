/**
 * lthcs-about.js — "About LTHCS" info modal.
 *
 * Opens a small read-only modal explaining the framework, scoring inputs,
 * data sources, and known limitations. Wired to the #lthcs-about-btn
 * button in the header.
 *
 * Nothing here hard-codes a ticker count or a coverage figure: the universe
 * size and the per-pillar "has data today" column are filled on open from
 * universe.json and the latest snapshot (see lthcs-coverage.js), and the band
 * ranges from weights.json (see lthcs-bands.js).
 */

import { bandList, bandRangeText, bandsReady } from "./lthcs-bands.js";
import { activeCount, pageData, snapshotCoverage } from "./lthcs-coverage.js";

// Score-band rows come from the live weights.json score_bands (via
// lthcs-bands.js) so the legend tracks any band recalibration.
function bandRowsHtml() {
  return bandList().map(
    (b) =>
      `<tr><td><span class="lthcs-about-dot" data-band="${b.key}"></span> ${escapeHtml(b.label)}</td><td><code>${escapeHtml(bandRangeText(b.key))}</code></td></tr>`
  ).join("");
}

// [snapshot sub-score key, name, default weight, inputs]
const PILLAR_LIST = [
  ["adoption_momentum", "Adoption Momentum", "25%", "Revenue growth & QoQ vs peers + Google Trends search interest (small daily batches)."],
  ["institutional_confidence", "Institutional Confidence", "20%", "Form 4 insider conviction + 13F top-10 holdings change + 90d price momentum."],
  ["financial_evolution", "Financial Evolution", "15%", "Revenue growth + gross margin trend + operating cash flow + bank-cohort NII/PCL/noninterest."],
  ["thesis_integrity", "Thesis Integrity", "20%", "Finnhub analyst recommendations (primary) + SEC 8-K material events + Yahoo earnings refinement."],
  ["des", "Demand Environment Score", "20%", "Sector-tilted macro: FRED tier-1 (CPI/Fed Funds/10Y/Δ10Y/unemployment/real 10Y/VIX/M2) + WTI, plus tier-2 (Brent/gasoline/ISM/housing/sentiment/U6)."],
];

const SOURCE_LIST = [
  ["Yahoo Finance (yfinance)", "Daily prices, 90d momentum, 30d volatility, earnings refinement. No API key."],
  ["SEC EDGAR XBRL", "Annual + quarterly revenue, gross profit (with fallback chain), operating cash flow, bank-cohort NII/PCL/noninterest. User-Agent required."],
  ["SEC Form 4 (insider conviction)", "90-day rolling window of insider open-market buys vs sells, with cluster-buying and CEO/CFO flags. Feeds Institutional pillar and per-ticker detail."],
  ["SEC 13F (institutional holdings)", "Quarterly top-10 manager holdings change. Feeds Institutional pillar."],
  ["SEC 8-K (material events)", "Real-time material-event filter. Feeds Thesis pillar."],
  ["Finnhub", "Analyst recommendation distributions; the primary Thesis input. Needs an API key."],
  ["Google Trends (pytrends)", "Search-interest acceleration, fetched in small daily batches because Google rate-limits hard."],
  ["FRED", "Tier-1 macros (CPI, Fed Funds, 10Y, Δ10Y, unemployment, real 10Y, VIX, M2) + tier-2 (Brent, gasoline, ISM, housing, sentiment, U6). Free API key."],
  ["EIA", "WTI crude oil prices feeding DES energy tilt. Free API key."],
  ["SPDR sector ETFs", "11 sector ETFs (XLK / XLF / XLE / etc.) ranked vs SPY on 1m and 3m total return. Drives the Market Regime strip."],
];

const LIMITATIONS = [
  "Google Trends is fetched a small batch at a time, so each name's series refreshes only every few weeks. Names without a recent series are scored on their revenue signals alone.",
  "Gross margin (XBRL GrossProfit) is missing for many Financials, Communication Services and services-heavy names. A fallback concept chain closes part of the gap.",
  "The bank cohort (NII / PCL / noninterest income, a fixed allowlist) is a small cross-sectional pool, so its percentiles are coarse.",
  "A pillar with no data for a name is dropped and its weight is spread over the other pillars. The \"Has data today\" column above shows how often that happened in the latest snapshot.",
  "Delisted or taken-private names (for example WBA and EA) stay in universe.json marked inactive and are no longer scored.",
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

  const bandRows = bandRowsHtml();

  const pillarRows = PILLAR_LIST.map(
    ([key, name, weight, desc]) =>
      `<tr><td><strong>${escapeHtml(name)}</strong></td><td><code>${escapeHtml(weight)}</code></td><td>${escapeHtml(desc)}</td>` +
      `<td class="lthcs-about-cov"><code data-about-coverage="${key}">&hellip;</code></td></tr>`
  ).join("");

  const sourceList = SOURCE_LIST.map(
    ([name, desc]) =>
      `<li><strong>${escapeHtml(name)}</strong> — ${escapeHtml(desc)}</li>`
  ).join("");

  const limitList = LIMITATIONS.map(
    (text) => `<li>${escapeHtml(text)}</li>`
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
          The Long-Term Hold Confidence Score (LTHCS) is a daily 0–100 score for
          <span data-about-count>US-listed names</span> (the S&amp;P 500 and Dow 30, plus NASDAQ-100
          names and Index Exiles, which left every tracked index but are still scored). It blends fundamental,
          flow, sentiment, and macro signals into a single conviction score with a stage-aware weighting
          system (so a pre-profit growth name is judged differently than a mature compounder).
        </p>

        <h3>Score bands</h3>
        <table class="lthcs-about-table">
          <thead><tr><th>Band</th><th>Range</th></tr></thead>
          <tbody data-about-bands>${bandRows}</tbody>
        </table>

        <h3>Five pillars (default weights for standard compounder)</h3>
        <div class="lthcs-about-table-wrap">
          <table class="lthcs-about-table lthcs-about-wide-table">
            <thead><tr><th>Pillar</th><th>Weight</th><th>Inputs</th><th>Has data today</th></tr></thead>
            <tbody>${pillarRows}</tbody>
          </table>
        </div>
        <p class="lthcs-about-note" data-about-coverage-note>
          &ldquo;Has data today&rdquo; counts the names in the latest snapshot with a value for each pillar.
        </p>
        <p class="lthcs-about-note">
          Weights vary by <code>maturity_stage</code>. E.g. <code>pre_profit_growth</code> tilts to
          Adoption (30%); <code>recovery_stabilization</code> tilts to Financial Evolution (35%).
        </p>

        <h3>Data sources (all free tier)</h3>
        <ul class="lthcs-about-list">${sourceList}</ul>

        <h3>Known limitations</h3>
        <ul class="lthcs-about-list">${limitList}</ul>

        <h3>How daily updates work</h3>
        <p>
          A GitHub Actions workflow (<code>lthcs-daily.yml</code>) runs <code>python lthcs_daily.py</code>
          every day at 23:00 UTC, after the US close. It scores the universe, writes JSON files under
          <code>data/lthcs/</code> and commits them to the repository; the next site build publishes them
          on GitHub Pages. No server and no database: the dated snapshots in git history are the audit log.
        </p>

        <h3>Methodology source</h3>
        <p>
          The methodology is described in
          <a href="https://github.com/btabiado/alpine-data/blob/main/README_LTHCS.md" target="_blank" rel="noopener">README_LTHCS.md</a>;
          the scoring code lives in
          <a href="https://github.com/btabiado/alpine-data/tree/main/lthcs" target="_blank" rel="noopener"><code>lthcs/</code></a>.
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
  document.body.style.overflow = "hidden";
  document.addEventListener("keydown", escClose);
  document.addEventListener("keydown", trapTab);
  const closeBtn = root.querySelector(".lthcs-about-close");
  if (closeBtn) closeBtn.focus();
  bandsReady.then(() => {
    const tbody = root.querySelector("[data-about-bands]");
    if (tbody) tbody.innerHTML = bandRowsHtml();
  });
  pageData().then(({ universe, snapshot }) => paintLiveFacts(root, universe, snapshot));
}

// Universe size + per-pillar coverage from the files the page loads. Left as
// the neutral fallback text (no count, "…") when a file is unavailable.
function paintLiveFacts(root, universe, snapshot) {
  const n = activeCount(universe);
  const countEl = root.querySelector("[data-about-count]");
  if (countEl && n) countEl.textContent = `${n} US-listed names`;

  const cov = snapshotCoverage(snapshot);
  if (!cov) return;
  for (const [key] of PILLAR_LIST) {
    const el = root.querySelector(`[data-about-coverage="${key}"]`);
    if (!el) continue;
    const have = cov.pillars[key] || 0;
    el.textContent = `${have} / ${cov.scored} (${Math.round((100 * have) / cov.scored)}%)`;
  }
  const note = root.querySelector("[data-about-coverage-note]");
  if (note) {
    const flags = cov.flags.length
      ? cov.flags.map(([f, c]) => `${f} ${c}`).join(" · ")
      : "none";
    note.textContent =
      `“Has data today” counts the ${cov.scored} names scored in the ${cov.calcDate || "latest"} snapshot ` +
      `that have a value for each pillar. Data-quality flags in that snapshot: ${flags}.`;
  }
}

export function closeAbout() {
  const root = $("#lthcs-about-root");
  if (!root) return;
  root.classList.add("hidden");
  root.setAttribute("aria-hidden", "true");
  document.body.style.overflow = "";
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
