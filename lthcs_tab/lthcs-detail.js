// lthcs-detail.js
// Week 9 — Click-to-open detail modal for the LTHCS tab.
// Vanilla ES2020 module. Renders into #lthcs-modal-root.
//
// Public API:
//   openDetail({ ticker, snapshotRow, universeEntry, narrative })
//   closeDetail()

'use strict';

import { renderSparkline, bandColorForScore } from './lthcs-sparkline.js';

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const HISTORY_BASE = '../data/lthcs/history/by_ticker';
const VARDETAIL_BASE = '../data/lthcs/variable_detail';

const PILLAR_ORDER = [
  'adoption_momentum',
  'institutional_confidence',
  'financial_evolution',
  'thesis_integrity',
  'des',
];

const PILLAR_DISPLAY = {
  adoption_momentum: 'Adoption Momentum',
  institutional_confidence: 'Institutional Confidence',
  financial_evolution: 'Financial Evolution',
  thesis_integrity: 'Thesis Integrity',
  des: 'Demand Environment',
};

const BAND_TONE = {
  // bands whose narrative slot 3 reads as "Why to review" instead of "Why not to sell"
  weakening: 'review',
  review: 'review',
};

const MODIFIER_LABELS = {
  macro_adj: 'macro',
  sector_adj: 'sector',
  volatility_mod: 'volatility',
};

const FOCUSABLE_SEL = [
  'a[href]',
  'area[href]',
  'button:not([disabled])',
  'input:not([disabled]):not([type="hidden"])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(',');

// ---------------------------------------------------------------------------
// Module state
// ---------------------------------------------------------------------------

const moduleState = {
  rootEl: null,
  panelEl: null,
  prevFocus: null,
  keyHandler: null,
  // currently displayed ticker — used to ignore stale async resolutions
  activeTicker: null,
  activeCalcDate: null,
  // cached variable_detail payloads keyed by calc_date
  vardetailCache: new Map(),
  // current toggle state for variable-detail section
  vardetailLoaded: false,
};

// ---------------------------------------------------------------------------
// Tiny helpers
// ---------------------------------------------------------------------------

function el(tag, opts) {
  const node = document.createElement(tag);
  if (!opts) return node;
  if (opts.className) node.className = opts.className;
  if (opts.id) node.id = opts.id;
  if (opts.text != null) node.textContent = String(opts.text);
  if (opts.attrs) {
    for (const [k, v] of Object.entries(opts.attrs)) {
      if (v == null || v === false) continue;
      node.setAttribute(k, String(v));
    }
  }
  return node;
}

function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

function fmtScore(n) {
  const v = Number(n);
  return Number.isFinite(v) ? v.toFixed(1) : '—';
}

function fmtDrift(n) {
  const v = Number(n);
  if (!Number.isFinite(v)) return '0.0';
  const sign = v > 0 ? '+' : v < 0 ? '−' : '';
  // use a real minus for negatives to match the "+1.0 / −1.0" feel
  return `${sign}${Math.abs(v).toFixed(1)}`;
}

function driftDirection(v) {
  const n = Number(v) || 0;
  if (n > 0.05) return 'improving';
  if (n < -0.05) return 'declining';
  return 'stable';
}

function humanCase(s) {
  if (!s) return '';
  return String(s)
    .replace(/_/g, ' ')
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

function metaJoin(parts) {
  return parts.filter((p) => p != null && p !== '').join(' · ');
}

function formatNumberish(v) {
  if (v == null) return '—';
  if (typeof v === 'number') {
    if (!Number.isFinite(v)) return String(v);
    // small floats → 4 sig digits; larger → 2 decimals; integers → integer
    if (Number.isInteger(v)) return String(v);
    if (Math.abs(v) < 1) return v.toFixed(4);
    return v.toFixed(2);
  }
  if (typeof v === 'string' || typeof v === 'boolean') return String(v);
  try { return JSON.stringify(v); } catch { return String(v); }
}

// ---------------------------------------------------------------------------
// Root + lifecycle
// ---------------------------------------------------------------------------

function ensureRoot() {
  if (moduleState.rootEl && document.body.contains(moduleState.rootEl)) {
    return moduleState.rootEl;
  }
  let root = document.getElementById('lthcs-modal-root');
  if (!root) {
    root = el('div', {
      id: 'lthcs-modal-root',
      className: 'lthcs-modal-root hidden',
      attrs: { role: 'dialog', 'aria-modal': 'true', 'aria-hidden': 'true' },
    });
    document.body.appendChild(root);
  }
  moduleState.rootEl = root;
  return root;
}

function buildShell() {
  const root = ensureRoot();
  clear(root);

  // Backdrop
  const backdrop = el('div', { className: 'lthcs-modal-backdrop' });
  backdrop.addEventListener('click', () => closeDetail());
  root.appendChild(backdrop);

  // Panel scaffolding — controlled markup (no user data here),
  // so a single innerHTML template is safe per the brief.
  const panel = el('div', { className: 'lthcs-modal-panel' });
  panel.setAttribute('tabindex', '-1');
  panel.innerHTML = `
    <div class="lthcs-modal-header">
      <div class="lthcs-modal-title-block">
        <h2 class="lthcs-modal-ticker" id="lthcs-modal-ticker"></h2>
        <p class="lthcs-modal-company" data-slot="company"></p>
        <div class="lthcs-modal-submeta" data-slot="submeta"></div>
      </div>
      <button type="button" class="lthcs-modal-close" aria-label="Close detail">&times;</button>
    </div>

    <div class="lthcs-modal-hero" data-slot="hero"></div>

    <div>
      <h3 class="lthcs-modal-section-heading">Score history</h3>
      <div class="lthcs-modal-chart-wrap">
        <div class="lthcs-modal-chart" data-slot="chart"></div>
      </div>
    </div>

    <div>
      <h3 class="lthcs-modal-section-heading">Pillar breakdown</h3>
      <div class="lthcs-modal-pillars" data-slot="pillars"></div>
    </div>

    <div>
      <h3 class="lthcs-modal-section-heading">AI narrative</h3>
      <div class="lthcs-modal-narrative" data-slot="narrative"></div>
    </div>

    <div data-slot="flags-wrap"></div>

    <div class="lthcs-modal-vardetail" data-slot="vardetail">
      <button type="button" class="lthcs-vardetail-toggle" aria-expanded="false">
        <span data-slot="vardetail-label">Show variable detail</span>
        <span class="lthcs-vardetail-chevron" aria-hidden="true">&#9656;</span>
      </button>
      <div class="lthcs-vardetail-body hidden" data-slot="vardetail-body" hidden></div>
    </div>

    <div class="lthcs-modal-footer">
      Sources: yfinance (prices), SEC EDGAR (financials), FRED (macro), EIA (energy), Alpha Vantage (news). Not investment advice.
    </div>
  `;
  root.appendChild(panel);
  moduleState.panelEl = panel;

  // Wire close button
  panel.querySelector('.lthcs-modal-close').addEventListener('click', () => closeDetail());

  return root;
}

// ---------------------------------------------------------------------------
// Section renderers
// ---------------------------------------------------------------------------

function renderHeader(panel, { ticker, universeEntry, snapshotRow }) {
  const tickerEl = panel.querySelector('[data-slot="company"]').parentElement.querySelector('.lthcs-modal-ticker');
  tickerEl.textContent = ticker || '—';
  panel.setAttribute('aria-labelledby', 'lthcs-modal-ticker');

  const companyEl = panel.querySelector('[data-slot="company"]');
  const name = (universeEntry && universeEntry.name) || ticker || '';
  companyEl.textContent = name;

  const submetaEl = panel.querySelector('[data-slot="submeta"]');
  clear(submetaEl);
  const sector = (universeEntry && universeEntry.sector) || (snapshotRow && snapshotRow.sector) || '';
  const industry = (universeEntry && universeEntry.industry) || '';
  const maturity = humanCase((snapshotRow && snapshotRow.maturity_stage) || '');
  for (const part of [sector, industry, maturity]) {
    if (!part) continue;
    submetaEl.appendChild(el('span', { text: part }));
  }
}

function renderHero(panel, { snapshotRow }) {
  const heroEl = panel.querySelector('[data-slot="hero"]');
  clear(heroEl);

  const score = (snapshotRow && snapshotRow.lthcs_score);
  const band = (snapshotRow && snapshotRow.band) || 'review';
  const confidence = (snapshotRow && snapshotRow.confidence_level) || null;

  heroEl.appendChild(el('div', { className: 'lthcs-modal-score', text: fmtScore(score) }));

  const bandLabel = humanCase(band);
  const badge = el('span', {
    className: 'lthcs-modal-band-badge',
    text: bandLabel,
    attrs: { 'data-band': band, 'aria-label': `Band: ${bandLabel}` },
  });
  heroEl.appendChild(badge);

  if (confidence) {
    heroEl.appendChild(el('span', {
      className: 'lthcs-modal-confidence-chip',
      text: `Confidence: ${confidence}`,
    }));
  }

  const driftRow = el('div', { className: 'lthcs-modal-drift-row' });
  const driftFields = [
    ['1d', snapshotRow && snapshotRow.drift_1d],
    ['7d', snapshotRow && snapshotRow.drift_7d],
    ['30d', snapshotRow && snapshotRow.drift_30d],
    ['90d', snapshotRow && snapshotRow.drift_90d],
  ];
  for (const [label, value] of driftFields) {
    const stat = el('div', { className: 'lthcs-modal-drift-stat' });
    stat.appendChild(el('span', { className: 'lthcs-modal-drift-stat-label', text: label }));
    const v = el('span', {
      className: 'lthcs-modal-drift-stat-value',
      text: fmtDrift(value),
      attrs: { 'data-direction': driftDirection(value) },
    });
    stat.appendChild(v);
    driftRow.appendChild(stat);
  }
  heroEl.appendChild(driftRow);
}

function renderChartSkeleton(panel) {
  const chartEl = panel.querySelector('[data-slot="chart"]');
  clear(chartEl);
  chartEl.appendChild(el('div', {
    className: 'lthcs-modal-chart-placeholder',
    text: 'Loading history…',
  }));
}

function renderChart(panel, history) {
  const chartEl = panel.querySelector('[data-slot="chart"]');
  clear(chartEl);
  const series = (history && Array.isArray(history.history)) ? history.history : [];
  if (series.length === 0) {
    chartEl.appendChild(el('div', {
      className: 'lthcs-modal-chart-placeholder',
      text: 'No history yet for this ticker.',
    }));
    return;
  }
  try {
    const svg = renderSparkline(series, {
      width: 600,
      height: 220,
      showBands: true,
      showAxes: true,
      showLastDot: true,
      fillColor: 'currentColor',
    });
    // Strip width/height so it scales — the CSS sets width:100%.
    svg.removeAttribute('width');
    svg.removeAttribute('height');
    chartEl.appendChild(svg);
  } catch (err) {
    console.warn('LTHCS detail: sparkline render failed', err);
    chartEl.appendChild(el('div', {
      className: 'lthcs-modal-chart-placeholder',
      text: 'History not available.',
    }));
  }
}

function renderChartError(panel) {
  const chartEl = panel.querySelector('[data-slot="chart"]');
  clear(chartEl);
  chartEl.appendChild(el('div', {
    className: 'lthcs-modal-chart-placeholder',
    text: 'History not available.',
  }));
}

function renderPillars(panel, { snapshotRow }) {
  const pillarsEl = panel.querySelector('[data-slot="pillars"]');
  clear(pillarsEl);

  const subscores = (snapshotRow && snapshotRow.subscores) || {};
  const weights = (snapshotRow && Array.isArray(snapshotRow.weights_used)) ? snapshotRow.weights_used : [];
  const contribs = (snapshotRow && Array.isArray(snapshotRow.weighted_components)) ? snapshotRow.weighted_components : [];

  PILLAR_ORDER.forEach((key, i) => {
    const sub = Number(subscores[key]);
    const weight = Number(weights[i]);
    const contrib = Number(contribs[i]);

    const row = el('div', { className: 'lthcs-pillar-row' });

    row.appendChild(el('div', { className: 'lthcs-pillar-label', text: PILLAR_DISPLAY[key] || key }));

    const track = el('div', { className: 'lthcs-pillar-bar-track' });
    const fill = el('div', { className: 'lthcs-pillar-bar-fill' });
    const pct = Number.isFinite(sub) ? Math.max(0, Math.min(100, sub)) : 0;
    fill.style.width = `${pct.toFixed(1)}%`;
    const color = Number.isFinite(sub) ? bandColorForScore(sub) : null;
    if (color) fill.style.background = color;
    track.appendChild(fill);
    row.appendChild(track);

    const values = el('div', { className: 'lthcs-pillar-values' });
    const scoreSpan = el('strong', { text: fmtScore(sub) });
    values.appendChild(scoreSpan);
    if (Number.isFinite(contrib) && Number.isFinite(weight)) {
      const contribText = el('span', {
        className: 'lthcs-pillar-contrib',
        text: ` (${contrib.toFixed(1)} contrib @ ${(weight * 100).toFixed(0)}%)`,
      });
      values.appendChild(contribText);
    }
    row.appendChild(values);
    pillarsEl.appendChild(row);
  });

  // Modifiers (only nonzero)
  const modifiers = (snapshotRow && snapshotRow.modifiers) || {};
  const nonzero = Object.entries(modifiers).filter(([, v]) => Number.isFinite(Number(v)) && Number(v) !== 0);
  if (nonzero.length) {
    const parts = nonzero.map(([k, v]) => {
      const label = MODIFIER_LABELS[k] || k;
      return `${label} ${fmtDrift(v)}`;
    });
    pillarsEl.appendChild(el('div', {
      className: 'lthcs-modal-modifiers',
      text: `Modifiers: ${parts.join(', ')}`,
    }));
  }
}

function renderNarrative(panel, { snapshotRow, narrative }) {
  const narrEl = panel.querySelector('[data-slot="narrative"]');
  clear(narrEl);

  if (!narrative) {
    narrEl.appendChild(el('div', {
      className: 'lthcs-narrative-placeholder',
      text: 'Narrative not loaded for this snapshot.',
    }));
    return;
  }

  const band = (snapshotRow && snapshotRow.band) || '';
  const reviewTone = BAND_TONE[band] === 'review';
  const slot3Label = reviewTone ? 'Why to review' : 'Why not to sell';

  const sections = [
    ["Today's take", narrative.todays_take],
    ['Why score changed', narrative.why_changed],
    [slot3Label, narrative.why_not_to_sell],
    ['What would break the thesis', narrative.what_would_break],
  ];

  for (const [heading, body] of sections) {
    const para = el('div', { className: 'lthcs-narrative-para' });
    para.appendChild(el('h4', { text: heading }));
    para.appendChild(el('p', { text: body || '—' }));
    narrEl.appendChild(para);
  }
}

function renderFlags(panel, { snapshotRow }) {
  const wrap = panel.querySelector('[data-slot="flags-wrap"]');
  clear(wrap);
  const flags = (snapshotRow && Array.isArray(snapshotRow.data_quality_flags))
    ? snapshotRow.data_quality_flags
    : [];
  if (!flags.length) return;
  const banner = el('div', { className: 'lthcs-modal-flags' });
  banner.appendChild(el('span', { text: 'Data quality notes:' }));
  for (const f of flags) {
    banner.appendChild(el('span', { className: 'lthcs-modal-flag-chip', text: String(f) }));
  }
  wrap.appendChild(banner);
}

// ---------------------------------------------------------------------------
// Variable-detail (lazy)
// ---------------------------------------------------------------------------

function renderVardetailRows(bodyEl, rows, ticker) {
  clear(bodyEl);
  const filtered = (Array.isArray(rows) ? rows : []).filter((r) => r && r.ticker === ticker);
  if (filtered.length === 0) {
    bodyEl.appendChild(el('div', {
      className: 'lthcs-vardetail-loading',
      text: 'No variable detail for this ticker.',
    }));
    return 0;
  }
  for (const row of filtered) {
    const wrap = el('div', { className: 'lthcs-vardetail-pillar' });
    wrap.appendChild(el('h5', {
      className: 'lthcs-vardetail-pillar-name',
      text: PILLAR_DISPLAY[row.pillar] || row.pillar || '—',
    }));
    const table = el('table', { className: 'lthcs-vardetail-kv' });
    const tbody = el('tbody');
    const components = (row.components && typeof row.components === 'object') ? row.components : {};
    const keys = Object.keys(components);
    if (Number.isFinite(Number(row.sub_score))) {
      const tr = el('tr');
      tr.appendChild(el('td', { text: 'sub_score' }));
      tr.appendChild(el('td', { text: fmtScore(row.sub_score) }));
      tbody.appendChild(tr);
    }
    for (const k of keys) {
      const tr = el('tr');
      tr.appendChild(el('td', { text: k }));
      tr.appendChild(el('td', { text: formatNumberish(components[k]) }));
      tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    wrap.appendChild(table);
    bodyEl.appendChild(wrap);
  }
  return filtered.length;
}

async function loadVardetail(calcDate) {
  if (!calcDate) throw new Error('no calc_date');
  if (moduleState.vardetailCache.has(calcDate)) {
    return moduleState.vardetailCache.get(calcDate);
  }
  const res = await fetch(`${VARDETAIL_BASE}/${calcDate}.json`, { cache: 'no-store' });
  if (!res.ok) throw new Error(`vardetail fetch failed: ${res.status}`);
  const data = await res.json();
  moduleState.vardetailCache.set(calcDate, data);
  return data;
}

function wireVardetailToggle(panel, { ticker, calcDate }) {
  const toggle = panel.querySelector('.lthcs-vardetail-toggle');
  const body = panel.querySelector('[data-slot="vardetail-body"]');
  const labelEl = panel.querySelector('[data-slot="vardetail-label"]');
  toggle.setAttribute('aria-expanded', 'false');
  body.classList.add('hidden');
  body.setAttribute('hidden', '');
  labelEl.textContent = 'Show variable detail';
  moduleState.vardetailLoaded = false;

  // Replace toggle to drop any prior listeners cleanly
  const fresh = toggle.cloneNode(true);
  toggle.parentNode.replaceChild(fresh, toggle);
  const freshBody = panel.querySelector('[data-slot="vardetail-body"]');
  const freshLabel = panel.querySelector('[data-slot="vardetail-label"]');

  fresh.addEventListener('click', async () => {
    const expanded = fresh.getAttribute('aria-expanded') === 'true';
    if (expanded) {
      fresh.setAttribute('aria-expanded', 'false');
      freshBody.classList.add('hidden');
      freshBody.setAttribute('hidden', '');
      return;
    }
    fresh.setAttribute('aria-expanded', 'true');
    freshBody.classList.remove('hidden');
    freshBody.removeAttribute('hidden');
    if (moduleState.vardetailLoaded) return;
    clear(freshBody);
    freshBody.appendChild(el('div', { className: 'lthcs-vardetail-loading', text: 'Loading variable detail…' }));
    try {
      if (!calcDate) {
        clear(freshBody);
        freshBody.appendChild(el('div', { className: 'lthcs-vardetail-error', text: 'No calc_date available.' }));
        return;
      }
      const data = await loadVardetail(calcDate);
      // Guard: bail if user already moved on
      if (moduleState.activeTicker !== ticker) return;
      const rows = (data && Array.isArray(data.variables)) ? data.variables : [];
      const n = renderVardetailRows(freshBody, rows, ticker);
      freshLabel.textContent = n > 0
        ? `Show variable detail (${n} rows)`
        : 'Show variable detail';
      moduleState.vardetailLoaded = true;
    } catch (err) {
      console.warn('LTHCS detail: variable_detail load failed', err);
      clear(freshBody);
      freshBody.appendChild(el('div', { className: 'lthcs-vardetail-error', text: 'Variable detail not available.' }));
    }
  });
}

// ---------------------------------------------------------------------------
// Focus trap + keyboard
// ---------------------------------------------------------------------------

function trapKeydown(e) {
  const root = moduleState.rootEl;
  if (!root || root.classList.contains('hidden')) return;
  if (e.key === 'Escape') {
    e.preventDefault();
    closeDetail();
    return;
  }
  if (e.key !== 'Tab') return;
  const panel = moduleState.panelEl;
  if (!panel) return;
  const focusables = Array.from(panel.querySelectorAll(FOCUSABLE_SEL))
    .filter((n) => !n.hasAttribute('disabled') && n.offsetParent !== null);
  if (focusables.length === 0) {
    e.preventDefault();
    panel.focus();
    return;
  }
  const first = focusables[0];
  const last = focusables[focusables.length - 1];
  const active = document.activeElement;
  if (e.shiftKey) {
    if (active === first || !panel.contains(active)) {
      e.preventDefault();
      last.focus();
    }
  } else {
    if (active === last) {
      e.preventDefault();
      first.focus();
    }
  }
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/**
 * Open the detail modal for a ticker.
 * @param {Object} args
 * @param {string} args.ticker
 * @param {Object} args.snapshotRow
 * @param {Object} [args.universeEntry]
 * @param {Object|null} [args.narrative]
 * @param {string} [args.calcDate] Optional explicit calc_date for variable_detail fetch.
 */
export function openDetail(args) {
  const { ticker } = args || {};
  if (!ticker) {
    console.warn('LTHCS detail: openDetail called without ticker.');
    return;
  }
  const snapshotRow = (args && args.snapshotRow) || {};
  const universeEntry = (args && args.universeEntry) || {};
  const narrative = (args && args.narrative) || null;
  const calcDate = (args && args.calcDate) || snapshotRow.calc_date || null;

  // Save prior focus for restoration on close
  if (!moduleState.rootEl || moduleState.rootEl.classList.contains('hidden')) {
    moduleState.prevFocus = document.activeElement;
  }

  const root = buildShell();
  const panel = moduleState.panelEl;

  moduleState.activeTicker = ticker;
  moduleState.activeCalcDate = calcDate;
  moduleState.vardetailLoaded = false;

  // Populate sections (synchronous parts)
  renderHeader(panel, { ticker, universeEntry, snapshotRow });
  renderHero(panel, { snapshotRow });
  renderChartSkeleton(panel);
  renderPillars(panel, { snapshotRow });
  renderNarrative(panel, { snapshotRow, narrative });
  renderFlags(panel, { snapshotRow });
  wireVardetailToggle(panel, { ticker, calcDate });

  // Show
  root.classList.remove('hidden');
  root.setAttribute('aria-hidden', 'false');

  // Keyboard handlers
  if (moduleState.keyHandler) {
    document.removeEventListener('keydown', moduleState.keyHandler, true);
  }
  moduleState.keyHandler = trapKeydown;
  document.addEventListener('keydown', moduleState.keyHandler, true);

  // Focus the close button as a sensible default landing focus.
  const closeBtn = panel.querySelector('.lthcs-modal-close');
  if (closeBtn) {
    try { closeBtn.focus(); } catch { /* ignore */ }
  } else {
    try { panel.focus(); } catch { /* ignore */ }
  }

  // Async: fetch history
  const requestedTicker = ticker;
  fetch(`${HISTORY_BASE}/${encodeURIComponent(ticker)}.json`, { cache: 'no-store' })
    .then((res) => {
      if (!res.ok) throw new Error(`history ${res.status}`);
      return res.json();
    })
    .then((history) => {
      if (moduleState.activeTicker !== requestedTicker) return;
      renderChart(panel, history);
    })
    .catch((err) => {
      console.warn('LTHCS detail: history fetch failed for', requestedTicker, err);
      if (moduleState.activeTicker !== requestedTicker) return;
      renderChartError(panel);
    });
}

export function closeDetail() {
  const root = moduleState.rootEl;
  if (!root) return;
  if (root.classList.contains('hidden')) return;
  root.classList.add('hidden');
  root.setAttribute('aria-hidden', 'true');
  if (moduleState.keyHandler) {
    document.removeEventListener('keydown', moduleState.keyHandler, true);
    moduleState.keyHandler = null;
  }
  // Restore focus to the previously-focused element (typically the card).
  const prior = moduleState.prevFocus;
  moduleState.prevFocus = null;
  moduleState.activeTicker = null;
  if (prior && typeof prior.focus === 'function' && document.contains(prior)) {
    try { prior.focus(); } catch { /* ignore */ }
  }
}
