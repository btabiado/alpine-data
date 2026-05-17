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
  ["Adoption Momentum", "25%", "Revenue growth vs peers + search-interest acceleration."],
  ["Institutional Confidence", "20%", "Trailing 90-day price momentum percentile + 13F change (V1 stub)."],
  ["Financial Evolution", "15%", "Revenue growth + gross margin trend + operating cash flow positivity."],
  ["Thesis Integrity", "20%", "Alpha Vantage news sentiment rolling 30-day average."],
  ["Demand Environment Score", "20%", "Sector-tilted macro: CPI, Fed Funds, 10Y, oil, unemployment."],
];

const SOURCE_LIST = [
  ["Yahoo Finance (yfinance)", "Daily prices, 90d momentum, 30d volatility. No API key."],
  ["SEC EDGAR XBRL", "Annual + quarterly revenue, gross profit, operating cash flow. User-Agent required."],
  ["FRED", "CPI, Fed Funds Rate, 10-Year Treasury, Unemployment Rate. Free API key."],
  ["EIA", "WTI / Brent / gasoline prices. Free API key."],
  ["Alpha Vantage", "News sentiment. Free tier: 25 req/day; V1 limitation documented below."],
];

const V1_LIMITATIONS = [
  "Thesis pillar uses a daily rotation — each run scores ≈6–25 of the 74 tickers via per-ticker Alpha Vantage news calls (free tier throttles bursts; daily cap is 25 nominal, often lower in practice). Tickers without fresh stored sentiment fall back to neutral 50 with a data-quality flag. Full universe is refreshed every ~3–14 days depending on throttle. Phase 2 unlocks AV Premium or an alternate news source to score all 74 daily.",
  "Google Trends acceleration (40% of Adoption pillar) is not driven for 74 tickers in V1 because Google rate-limits aggressively. Adoption uses revenue growth percentile only for V1.",
  "13F institutional holdings change (30% of Institutional pillar) is a Phase 2 stub. Institutional uses 90d momentum percentile alone in V1.",
  "Banks (e.g. JPM) score artificially low on Financial Evolution — they don't report GrossProfit / OCF the standard us-gaap way. Sector-aware financial scoring is Phase 2.",
  "WBA is marked inactive in the universe (Walgreens taken private late 2025; no longer files with SEC).",
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
