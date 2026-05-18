// lthcs-index.js
// Renders the "LTHCS Composite Index" headline card at the top of the
// classic /lthcs/ page. Mirrors the V1 Crypto Trading Dashboard's
// Whale Sentiment Index visual pattern: big colored score + horizontal
// range gauge (red → green) + collapsible component breakdown table.
//
// Pure side-effect: reads ../data/lthcs/index/<date>.json (written by
// lthcs_daily.py Stage 8) and paints into #lthcs-composite-index.
// Best-effort: failures are logged but the page keeps working.

'use strict';

const INDEX_BASE = '../data/lthcs/index';

// Band-bright tokens from lthcs.css (kept in sync with --band-*-bright).
const BAND_BRIGHT = {
  elite: 'var(--band-elite-bright)',
  high_confidence: 'var(--band-high-bright)',
  constructive: 'var(--band-constructive-bright)',
  monitor: 'var(--band-monitor-bright)',
  weakening: 'var(--band-weakening-bright)',
  review: 'var(--band-review-bright)',
};

function escapeHtml(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function indexUrlFor(calcDate) {
  return `${INDEX_BASE}/${calcDate}.json`;
}

async function fetchIndexFile(calcDate) {
  if (!calcDate) return null;
  const url = indexUrlFor(calcDate);
  try {
    const res = await fetch(url, { cache: 'no-store' });
    if (!res.ok) return null;
    return await res.json();
  } catch (err) {
    console.warn('LTHCS: index load failed', err);
    return null;
  }
}

function deltaClass(delta) {
  if (delta > 0) return 'lthcs-index-pos';
  if (delta < 0) return 'lthcs-index-neg';
  return 'lthcs-index-neutral';
}

function deltaStr(delta) {
  if (delta == null) return '—';
  return (delta >= 0 ? '+' : '') + delta;
}

// Map composite score in [-100, +100] to a 0-100% offset on the gauge.
function gaugePctFor(score) {
  if (typeof score !== 'number' || !Number.isFinite(score)) return 50;
  const pct = ((score + 100) / 200) * 100;
  if (pct < 0) return 0;
  if (pct > 100) return 100;
  return pct;
}

function renderComponentsTable(components) {
  if (!components || !components.length) return '';
  const rows = components.map((c) => {
    const cls = deltaClass(c.delta);
    return `
      <tr>
        <td class="lthcs-index-comp-name">${escapeHtml(c.name)}</td>
        <td class="lthcs-index-comp-value">${escapeHtml(c.value)}</td>
        <td class="lthcs-index-comp-delta ${cls}">${escapeHtml(deltaStr(c.delta))}</td>
        <td class="lthcs-index-comp-read">${escapeHtml(c.read || '')}</td>
      </tr>`;
  }).join('');
  return `
    <table class="lthcs-index-table">
      <thead>
        <tr>
          <th scope="col">Component</th>
          <th scope="col">Value</th>
          <th scope="col">&plusmn;</th>
          <th scope="col">Read</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>`;
}

function renderIndexInto(host, payload) {
  if (!payload) {
    host.innerHTML = '';
    host.classList.add('hidden');
    return;
  }
  host.classList.remove('hidden');
  const score = Number(payload.score);
  const scoreStr = (score >= 0 ? '+' : '') + score;
  const bandKey = payload.band_key || 'monitor';
  const color = BAND_BRIGHT[bandKey] || 'var(--band-monitor-bright)';
  const pct = gaugePctFor(score);
  const label = payload.label || 'LTHCS NEUTRAL';
  const note = payload.note || '';
  const asOf = payload.as_of || '';
  const componentsHtml = renderComponentsTable(payload.components || []);

  host.dataset.band = bandKey;
  host.style.setProperty('--lthcs-index-tone', color);
  host.innerHTML = `
    <div class="lthcs-index-card">
      <div class="lthcs-index-head">
        <div class="lthcs-index-titles">
          <div class="lthcs-index-title">LTHCS COMPOSITE INDEX</div>
          <div class="lthcs-index-sub">
            Where is the long-term-hold market? &middot; as of ${escapeHtml(asOf)}
          </div>
        </div>
        <div class="lthcs-index-score-block">
          <div class="lthcs-index-score" aria-label="LTHCS composite score">${escapeHtml(scoreStr)}</div>
          <div class="lthcs-index-label">${escapeHtml(label)}</div>
        </div>
      </div>
      <div class="lthcs-index-gauge" role="img" aria-label="LTHCS composite gauge at ${escapeHtml(scoreStr)}">
        <div class="lthcs-index-gauge-bar"></div>
        <div class="lthcs-index-gauge-marker" style="left: calc(${pct.toFixed(1)}% - 5px)"></div>
        <div class="lthcs-index-gauge-ticks" aria-hidden="true">
          <span>-100</span><span>0</span><span>+100</span>
        </div>
      </div>
      <details class="lthcs-index-details" open>
        <summary class="lthcs-index-summary">Component breakdown</summary>
        ${componentsHtml}
      </details>
      <div class="lthcs-index-note">${escapeHtml(note)}</div>
    </div>`;
}

export async function renderLthcsIndex(calcDate) {
  const host = document.getElementById('lthcs-composite-index');
  if (!host) return;
  const payload = await fetchIndexFile(calcDate);
  renderIndexInto(host, payload);
}

// Auto-wire: poll the snapshot index for today's calc_date and render.
// We do this independently of lthcs-tab.js so we don't have to touch its
// orchestration. lthcs-tab.js persists the latest snapshot date to
// localStorage, but we fall back to today's ISO date if missing.
async function discoverCalcDate() {
  // Prefer the snapshot index (canonical source of "what's the latest date").
  try {
    const res = await fetch('../data/lthcs/snapshots/index.json', { cache: 'no-store' });
    if (res.ok) {
      const idx = await res.json();
      const latest = idx && (idx.latest || idx.latest_calc_date);
      if (latest) return latest;
    }
  } catch (_) { /* fall through */ }
  // Fallback: today (UTC ISO).
  return new Date().toISOString().slice(0, 10);
}

document.addEventListener('DOMContentLoaded', () => {
  discoverCalcDate()
    .then(renderLthcsIndex)
    .catch((err) => console.warn('LTHCS: composite-index auto-render failed', err));
});
