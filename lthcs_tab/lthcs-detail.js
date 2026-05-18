// lthcs-detail.js
// Week 9 — Click-to-open detail modal for the LTHCS tab.
// Vanilla ES2020 module. Renders into #lthcs-modal-root.
//
// Public API:
//   openDetail({ ticker, snapshotRow, universeEntry, narrative })
//   closeDetail()

'use strict';

import { bandColorForScore } from './lthcs-sparkline.js';

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const HISTORY_BASE = '../data/lthcs/history/by_ticker';
const VARDETAIL_BASE = '../data/lthcs/variable_detail';
const HOLDINGS_BASE = '../data/lthcs/holdings';
const SNAPSHOTS_BASE = '../data/lthcs/snapshots';

// Maximum number of paragraph chars to show before offering a "Read more"
// toggle on the narrative panel. Picked empirically — current narratives top
// out around 350 chars per slot, this clamps to ~5 lines.
const NARRATIVE_CLAMP_CHARS = 280;

// Pillar palette for the multi-series chart (#20). Distinct hues that read
// against the dark theme; composite stays band-colored so the latest score is
// still the visual focal point.
const PILLAR_COLORS = {
  composite: null, // resolved at render time via bandColorForScore(latest)
  adoption_momentum: '#6EA8FE',
  institutional_confidence: '#9F7AEA',
  financial_evolution: '#4FD1C5',
  thesis_integrity: '#F6AD55',
  des: '#F687B3',
};

const PILLAR_SHORT = {
  adoption_momentum: 'Adoption',
  institutional_confidence: 'Institutional',
  financial_evolution: 'Financial',
  thesis_integrity: 'Thesis',
  des: 'Demand',
};

const SVG_NS = 'http://www.w3.org/2000/svg';

// Band buckets, lowest first. Each: [minInclusive, maxExclusive, key, color]
// Matches lthcs-sparkline DEFAULT_BAND_COLORS — kept here so we can tint with
// low alpha without re-parsing hex.
const BAND_BUCKETS = [
  { min:  0, max: 50,  key: 'review',       color: '#7A2E1F' },
  { min: 50, max: 60,  key: 'weakening',    color: '#B85A3E' },
  { min: 60, max: 70,  key: 'monitor',      color: '#D89148' },
  { min: 70, max: 80,  key: 'constructive', color: '#C9A227' },
  { min: 80, max: 90,  key: 'high',         color: '#4A8F5F' },
  { min: 90, max: 101, key: 'elite',        color: '#1F3A5F' },
];

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
  // cached per-ticker history payloads keyed by ticker
  historyCache: new Map(),
  // cached holdings maps keyed by calc_date
  holdingsCache: new Map(),
  // cached snapshot files keyed by calc_date (used by the multi-series
  // chart to derive 91-day pillar history on demand).
  snapshotCache: new Map(),
  // promise that resolves to a per-ticker pillar-history index, e.g.
  //   pillarSeriesByTicker.get('AAPL') ===
  //     [{date, composite, adoption_momentum, institutional_confidence, ...}, ...]
  // Lazily kicked off the first time any pillar legend item is enabled in
  // a session. One promise covers the universe so subsequent modal opens
  // get the data for free.
  pillarSeriesPromise: null,
  pillarSeriesByTicker: null,
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
        <div class="lthcs-modal-chart-tooltip" data-slot="chart-tooltip" aria-hidden="true"></div>
        <div class="lthcs-modal-chart-legend" data-slot="chart-legend" role="group" aria-label="Series toggles"></div>
        <div class="lthcs-modal-chart-note" data-slot="chart-note" hidden></div>
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

    <div>
      <h3 class="lthcs-modal-section-heading">Evidence — signals driving each pillar</h3>
      <div class="lthcs-modal-evidence" data-slot="evidence"></div>
    </div>

    <div data-slot="insider-wrap"></div>

    <div data-slot="holdings-wrap"></div>

    <div data-slot="flags-wrap"></div>

    <div class="lthcs-modal-vardetail" data-slot="vardetail">
      <button type="button" class="lthcs-vardetail-toggle" aria-expanded="false">
        <span data-slot="vardetail-label">Show variable detail</span>
        <span class="lthcs-vardetail-chevron" aria-hidden="true">&#9656;</span>
      </button>
      <div class="lthcs-vardetail-body hidden" data-slot="vardetail-body" hidden></div>
    </div>

    <div class="lthcs-modal-footer">
      Sources: yfinance (prices), SEC EDGAR (financials + Form 4 insider), FRED (macro + credit spreads), EIA (energy), Alpha Vantage (news). Not investment advice.
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
  setChartNote(panel, '');
  const tipEl = panel.querySelector('[data-slot="chart-tooltip"]');
  if (tipEl) { tipEl.textContent = ''; tipEl.classList.remove('visible'); }
  const legendEl = panel.querySelector('[data-slot="chart-legend"]');
  if (legendEl) clear(legendEl);
  chartEl.appendChild(el('div', {
    className: 'lthcs-modal-chart-placeholder',
    text: 'Loading history…',
  }));
}

function setChartNote(panel, text) {
  const noteEl = panel.querySelector('[data-slot="chart-note"]');
  if (!noteEl) return;
  if (!text) {
    noteEl.textContent = '';
    noteEl.setAttribute('hidden', '');
    return;
  }
  noteEl.textContent = text;
  noteEl.removeAttribute('hidden');
}

// SVG helpers --------------------------------------------------------------

function svgEl(tag, attrs) {
  const node = document.createElementNS(SVG_NS, tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v == null || v === false) continue;
      node.setAttribute(k, String(v));
    }
  }
  return node;
}

// Sort + normalize: returns ascending-by-date list of
// {date, score, band, synthetic}. The `synthetic` flag is preserved
// from the source payload — written by `LthcsPersist.fill_history_gaps`
// when the daily CI cron missed a run and forward-filled the gap with
// copies of the last real entry. Consumers (chart + trend pill) decide
// how to render. Trend pill ignores it (delta is already zero across
// flat synthetics); chart draws hollow markers + dashed line segments.
function normalizeHistory(series) {
  const rows = [];
  for (const raw of series) {
    if (!raw) continue;
    const d = String(raw.date || '');
    // Both legacy `composite_score` and current `score` are accepted.
    const sRaw = (raw.composite_score != null) ? raw.composite_score : raw.score;
    const s = Number(sRaw);
    if (!d || !Number.isFinite(s)) continue;
    rows.push({
      date: d,
      score: s,
      band: raw.band || null,
      synthetic: raw.synthetic === true,
    });
  }
  rows.sort((a, b) => (a.date < b.date ? -1 : a.date > b.date ? 1 : 0));
  return rows;
}

function formatTickDate(iso) {
  // iso = "YYYY-MM-DD"; produce "MMM DD" (e.g. "May 17")
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso || '');
  if (!m) return iso || '';
  const months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  const mi = Number(m[2]) - 1;
  return `${months[mi] || ''} ${String(Number(m[3]))}`;
}

// Pick ~4-6 evenly spaced tick indices across the series.
function pickTickIndices(n, target = 5) {
  if (n <= target) return Array.from({ length: n }, (_, i) => i);
  const out = new Set();
  for (let k = 0; k < target; k++) {
    out.add(Math.round(k * (n - 1) / (target - 1)));
  }
  return Array.from(out).sort((a, b) => a - b);
}

// Catmull-Rom → cubic Bezier smoothing for the line path.
function smoothPath(points) {
  if (points.length === 0) return '';
  if (points.length === 1) {
    const [x, y] = points[0];
    return `M${x.toFixed(2)},${y.toFixed(2)}`;
  }
  if (points.length === 2) {
    return `M${points[0][0].toFixed(2)},${points[0][1].toFixed(2)} L${points[1][0].toFixed(2)},${points[1][1].toFixed(2)}`;
  }
  const tension = 0.5; // mild smoothing
  const parts = [`M${points[0][0].toFixed(2)},${points[0][1].toFixed(2)}`];
  for (let i = 0; i < points.length - 1; i++) {
    const p0 = points[i - 1] || points[i];
    const p1 = points[i];
    const p2 = points[i + 1];
    const p3 = points[i + 2] || p2;
    const c1x = p1[0] + (p2[0] - p0[0]) * tension / 3;
    const c1y = p1[1] + (p2[1] - p0[1]) * tension / 3;
    const c2x = p2[0] - (p3[0] - p1[0]) * tension / 3;
    const c2y = p2[1] - (p3[1] - p1[1]) * tension / 3;
    parts.push(`C${c1x.toFixed(2)},${c1y.toFixed(2)} ${c2x.toFixed(2)},${c2y.toFixed(2)} ${p2[0].toFixed(2)},${p2[1].toFixed(2)}`);
  }
  return parts.join(' ');
}

// ---------------------------------------------------------------------------
// Multi-series time-series chart (#20)
//
// Plots the composite score over the available history (~91 days), plus up to
// five pillar sub_score series. The composite is loaded from the per-ticker
// history JSON (small, ~5KB). Pillar series are *lazy-loaded* on first toggle
// by aggregating across all daily snapshot files for the calc_date range —
// fetched in parallel, deduped, cached for the rest of the session so any
// further modal opens are free.
//
// At rest the chart shows composite-only. Each pillar has a legend chip that
// toggles its series on/off. The first time any pillar is enabled, we kick
// off the snapshot aggregation in the background; subsequent pillar toggles
// are instant.
// ---------------------------------------------------------------------------

// Module-level chart state — survives across mousemoves but not modal opens.
// We keep this off of `moduleState` because it is chart-instance-specific and
// would otherwise leak between renders.
function createChartState() {
  return {
    svg: null,
    overlay: null,
    guide: null,
    tooltipEl: null,
    chartEl: null,
    W: 640, H: 240,
    M: { top: 14, right: 14, bottom: 28, left: 36 },
    // composite + pillar series, indexed by key. value = {points:[[x,y]], data:[{date,value}]}
    seriesByKey: new Map(),
    visibleKeys: new Set(['composite']),
    // Marker layers per key so we can show/hide on toggle without rebuilding.
    layersByKey: new Map(),
    // hover marker dots per key
    hoverDotsByKey: new Map(),
    // x lookup by index → date string (composite is canonical x axis)
    dates: [],
    // map date → index for fast pillar alignment
    dateIndex: new Map(),
  };
}

let activeChartState = null;

function attachChartHover(state) {
  const { svg, overlay, guide, tooltipEl, chartEl, hoverDotsByKey, seriesByKey, visibleKeys, dates, W, H, M } = state;

  const hideHover = () => {
    guide.setAttribute('visibility', 'hidden');
    for (const dot of hoverDotsByKey.values()) dot.setAttribute('visibility', 'hidden');
    if (tooltipEl) {
      tooltipEl.classList.remove('visible');
      tooltipEl.setAttribute('aria-hidden', 'true');
    }
  };

  const showHoverAt = (clientX) => {
    if (!tooltipEl) return;
    const rect = svg.getBoundingClientRect();
    if (rect.width === 0 || rect.height === 0) return;
    const localX = (clientX - rect.left) * (W / rect.width);

    // Snap to nearest x along the composite series (the canonical date axis).
    const composite = seriesByKey.get('composite');
    if (!composite || !composite.points.length) return;
    let bestI = 0;
    let bestDx = Infinity;
    for (let i = 0; i < composite.points.length; i++) {
      const dx = Math.abs(composite.points[i][0] - localX);
      if (dx < bestDx) { bestDx = dx; bestI = i; }
    }
    const x = composite.points[bestI][0];
    guide.setAttribute('x1', x);
    guide.setAttribute('x2', x);
    guide.setAttribute('visibility', 'visible');

    const date = dates[bestI];

    tooltipEl.textContent = '';
    tooltipEl.appendChild(el('span', { className: 'lthcs-chart-tip-date', text: formatTickDate(date) }));

    // Composite first (largest read)
    if (visibleKeys.has('composite')) {
      const row = composite.data[bestI];
      const score = row ? row.value : null;
      if (Number.isFinite(score)) {
        const scoreEl = el('strong', { className: 'lthcs-chart-tip-score', text: score.toFixed(1) });
        tooltipEl.appendChild(scoreEl);
        // band readout
        const band = row.band || (function () {
          for (const b of BAND_BUCKETS) if (score >= b.min && score < b.max) return b.key;
          return null;
        })();
        if (band) {
          const bandEl = el('span', { className: 'lthcs-chart-tip-band', text: humanCase(band) });
          bandEl.setAttribute('data-band', band);
          tooltipEl.appendChild(bandEl);
        }
        const dot = hoverDotsByKey.get('composite');
        if (dot) {
          dot.setAttribute('cx', x);
          dot.setAttribute('cy', composite.points[bestI][1]);
          dot.setAttribute('visibility', 'visible');
        }
      }
    } else {
      const dot = hoverDotsByKey.get('composite');
      if (dot) dot.setAttribute('visibility', 'hidden');
    }

    // Pillar rows
    const rows = el('div', { className: 'lthcs-chart-tip-rows' });
    let anyPillar = false;
    for (const pillar of PILLAR_ORDER) {
      const series = seriesByKey.get(pillar);
      const dot = hoverDotsByKey.get(pillar);
      if (!series || !visibleKeys.has(pillar)) {
        if (dot) dot.setAttribute('visibility', 'hidden');
        continue;
      }
      const row = series.data[bestI];
      const val = row ? row.value : null;
      if (!Number.isFinite(val)) {
        if (dot) dot.setAttribute('visibility', 'hidden');
        continue;
      }
      const rowEl = el('div', { className: 'lthcs-chart-tip-row' });
      const swatch = el('span', { className: 'lthcs-chart-tip-swatch' });
      swatch.style.background = PILLAR_COLORS[pillar] || '#888';
      rowEl.appendChild(swatch);
      rowEl.appendChild(el('span', { className: 'lthcs-chart-tip-label', text: PILLAR_SHORT[pillar] || pillar }));
      rowEl.appendChild(el('span', { className: 'lthcs-chart-tip-val', text: val.toFixed(1) }));
      rows.appendChild(rowEl);
      anyPillar = true;
      if (dot) {
        dot.setAttribute('cx', x);
        dot.setAttribute('cy', series.points[bestI][1]);
        dot.setAttribute('visibility', 'visible');
      }
    }
    if (anyPillar) tooltipEl.appendChild(rows);

    // Position tooltip — clamped to chart wrap
    const wrap = chartEl.parentElement;
    const wrapRect = wrap ? wrap.getBoundingClientRect() : rect;
    const pxScale = rect.width / W;
    const py = composite.points[bestI][1];
    let left = (x * pxScale) + (rect.left - wrapRect.left) + 10;
    const tipW = tooltipEl.offsetWidth || 160;
    if (left + tipW > wrapRect.width - 4) left = (x * pxScale) + (rect.left - wrapRect.left) - tipW - 10;
    if (left < 4) left = 4;
    let top = (py * (rect.height / H)) + (rect.top - wrapRect.top) - 12;
    if (top < 4) top = 4;
    tooltipEl.style.left = `${left}px`;
    tooltipEl.style.top = `${top}px`;
    tooltipEl.classList.add('visible');
    tooltipEl.setAttribute('aria-hidden', 'false');
  };

  overlay.addEventListener('mousemove', (e) => showHoverAt(e.clientX));
  overlay.addEventListener('mouseleave', hideHover);
  overlay.addEventListener('touchstart', (e) => {
    const t = e.touches && e.touches[0];
    if (t) showHoverAt(t.clientX);
  }, { passive: true });
  overlay.addEventListener('touchmove', (e) => {
    const t = e.touches && e.touches[0];
    if (t) showHoverAt(t.clientX);
  }, { passive: true });
  overlay.addEventListener('touchend', hideHover);

  state.hideHover = hideHover;
}

// Add or replace a pillar series in the active chart. If `dataByDate` is null
// the layer is just hidden (used for "not loaded yet").
function setPillarSeries(state, key, dataByDate) {
  if (!state) return;
  // Remove any existing layer for this key
  const prior = state.layersByKey.get(key);
  if (prior && prior.parentNode) prior.parentNode.removeChild(prior);
  state.layersByKey.delete(key);
  const priorDot = state.hoverDotsByKey.get(key);
  if (priorDot && priorDot.parentNode) priorDot.parentNode.removeChild(priorDot);
  state.hoverDotsByKey.delete(key);

  if (!dataByDate) {
    state.seriesByKey.delete(key);
    return;
  }

  const { svg, M, W, H, dates } = state;
  const plotW = W - M.left - M.right;
  const plotH = H - M.top - M.bottom;
  const xFor = (i) => dates.length <= 1 ? M.left + plotW / 2 : M.left + (i / (dates.length - 1)) * plotW;
  const yFor = (s) => {
    const clamped = Math.max(0, Math.min(100, s));
    return M.top + (1 - clamped / 100) * plotH;
  };

  const data = [];
  const points = [];
  for (let i = 0; i < dates.length; i++) {
    const row = dataByDate.get(dates[i]);
    const v = row != null ? Number(row) : NaN;
    if (Number.isFinite(v)) {
      data.push({ date: dates[i], value: v });
      points.push([xFor(i), yFor(v)]);
    } else {
      data.push({ date: dates[i], value: NaN });
      points.push(null);
    }
  }

  // Build sub-paths split on NaN gaps. Pillars are mostly contiguous but
  // some tickers don't have early-history coverage for every pillar.
  const segments = [];
  let cur = [];
  for (let i = 0; i < points.length; i++) {
    if (points[i] == null) {
      if (cur.length >= 2) segments.push(cur);
      cur = [];
    } else {
      cur.push(points[i]);
    }
  }
  if (cur.length >= 2) segments.push(cur);
  if (segments.length === 0 && cur.length === 1) segments.push(cur); // single-point fallback

  const color = PILLAR_COLORS[key] || '#888';
  const group = svgEl('g', { class: 'lthcs-chart-pillar-layer', 'data-pillar': key });
  for (const seg of segments) {
    const d = smoothPath(seg);
    if (!d) continue;
    group.appendChild(svgEl('path', {
      d,
      fill: 'none',
      stroke: color,
      'stroke-width': '1.5',
      'stroke-linecap': 'round',
      'stroke-linejoin': 'round',
      'stroke-opacity': '0.85',
    }));
  }

  // Insert the layer below the hover dots/overlay but above the composite.
  // Order in the SVG matters: bands, grid, axes, composite, pillars, hover.
  // We track an anchor (the overlay) to insert before.
  const anchor = state.overlay;
  if (anchor && anchor.parentNode) {
    anchor.parentNode.insertBefore(group, anchor);
  } else {
    svg.appendChild(group);
  }
  state.layersByKey.set(key, group);
  state.seriesByKey.set(key, { points, data });

  // Hover marker dot for this pillar
  const dot = svgEl('circle', {
    cx: 0, cy: 0, r: 3.5,
    fill: color,
    stroke: 'var(--bg-card, #171C22)',
    'stroke-width': '1.5',
    visibility: 'hidden',
    class: 'lthcs-chart-hover-dot lthcs-chart-hover-dot-pillar',
  });
  svg.appendChild(dot);
  state.hoverDotsByKey.set(key, dot);

  if (!state.visibleKeys.has(key)) {
    group.style.display = 'none';
    dot.style.display = 'none';
  }
}

function setSeriesVisibility(state, key, visible) {
  if (!state) return;
  if (visible) state.visibleKeys.add(key);
  else state.visibleKeys.delete(key);
  const layer = state.layersByKey.get(key);
  if (layer) layer.style.display = visible ? '' : 'none';
  const dot = state.hoverDotsByKey.get(key);
  if (dot) dot.style.display = visible ? '' : 'none';
}

function renderChart(panel, history) {
  const chartEl = panel.querySelector('[data-slot="chart"]');
  const tooltipEl = panel.querySelector('[data-slot="chart-tooltip"]');
  const legendEl = panel.querySelector('[data-slot="chart-legend"]');
  clear(chartEl);
  if (legendEl) clear(legendEl);
  if (tooltipEl) {
    tooltipEl.textContent = '';
    tooltipEl.classList.remove('visible');
  }
  setChartNote(panel, '');
  activeChartState = null;

  const raw = (history && Array.isArray(history.history)) ? history.history : [];
  const series = normalizeHistory(raw);
  if (series.length === 0) {
    chartEl.appendChild(el('div', {
      className: 'lthcs-modal-chart-placeholder',
      text: 'No history yet for this ticker.',
    }));
    return;
  }

  // ---- Geometry ----
  const W = 640;
  const H = 240;
  const M = { top: 14, right: 14, bottom: 28, left: 36 };
  const plotW = W - M.left - M.right;
  const plotH = H - M.top - M.bottom;

  const xFor = (i) => {
    if (series.length === 1) return M.left + plotW / 2;
    return M.left + (i / (series.length - 1)) * plotW;
  };
  const yFor = (s) => {
    const clamped = Math.max(0, Math.min(100, s));
    return M.top + (1 - clamped / 100) * plotH;
  };

  // ---- SVG root ----
  const svg = svgEl('svg', {
    viewBox: `0 0 ${W} ${H}`,
    role: 'img',
    'aria-label': 'LTHCS composite and pillar history chart',
    class: 'lthcs-chart-svg',
    preserveAspectRatio: 'xMidYMid meet',
  });

  // ---- Band-tinted backgrounds ----
  const bandsGroup = svgEl('g', { class: 'lthcs-chart-bands' });
  for (const b of BAND_BUCKETS) {
    const yTop = yFor(Math.min(100, b.max));
    const yBot = yFor(b.min);
    const h = Math.max(0, yBot - yTop);
    if (h <= 0) continue;
    bandsGroup.appendChild(svgEl('rect', {
      x: M.left, y: yTop,
      width: plotW, height: h,
      fill: b.color,
      'fill-opacity': '0.06',
    }));
  }
  svg.appendChild(bandsGroup);

  // ---- Gridlines at 50/60/70/80/90 ----
  const gridGroup = svgEl('g', { class: 'lthcs-chart-grid' });
  for (const yVal of [50, 60, 70, 80, 90]) {
    const y = yFor(yVal);
    gridGroup.appendChild(svgEl('line', {
      x1: M.left, x2: M.left + plotW,
      y1: y, y2: y,
      stroke: 'currentColor',
      'stroke-opacity': '0.10',
      'stroke-dasharray': '2 4',
    }));
  }
  svg.appendChild(gridGroup);

  // ---- Y-axis ticks ----
  const yAxisGroup = svgEl('g', { class: 'lthcs-chart-axis lthcs-chart-yaxis' });
  for (const v of [0, 25, 50, 75, 100]) {
    const y = yFor(v);
    const t = svgEl('text', {
      x: M.left - 6,
      y: y + 3,
      'text-anchor': 'end',
      'font-size': '10',
      fill: 'currentColor',
      'fill-opacity': '0.55',
    });
    t.textContent = String(v);
    yAxisGroup.appendChild(t);
  }
  svg.appendChild(yAxisGroup);

  // ---- X-axis ticks ----
  const xAxisGroup = svgEl('g', { class: 'lthcs-chart-axis lthcs-chart-xaxis' });
  const tickIdx = pickTickIndices(series.length, 5);
  for (const i of tickIdx) {
    const x = xFor(i);
    xAxisGroup.appendChild(svgEl('line', {
      x1: x, x2: x,
      y1: M.top + plotH, y2: M.top + plotH + 3,
      stroke: 'currentColor', 'stroke-opacity': '0.35',
    }));
    const t = svgEl('text', {
      x,
      y: M.top + plotH + 16,
      'text-anchor': 'middle',
      'font-size': '10',
      fill: 'currentColor',
      'fill-opacity': '0.55',
    });
    t.textContent = formatTickDate(series[i].date);
    xAxisGroup.appendChild(t);
  }
  svg.appendChild(xAxisGroup);

  // ---- Composite line + fill ----
  const compositePoints = series.map((row, i) => [xFor(i), yFor(row.score)]);
  const latestScore = series[series.length - 1].score;
  const lineColor = bandColorForScore(latestScore) || '#6EA8FE';
  const compositePath = smoothPath(compositePoints);
  const hasSynthetic = series.some(r => r.synthetic);
  const compositeGroup = svgEl('g', { class: 'lthcs-chart-composite-layer' });
  if (compositePath) {
    const fillD = `${compositePath} L${compositePoints[compositePoints.length - 1][0].toFixed(2)},${(M.top + plotH).toFixed(2)} L${compositePoints[0][0].toFixed(2)},${(M.top + plotH).toFixed(2)} Z`;
    compositeGroup.appendChild(svgEl('path', {
      d: fillD,
      fill: lineColor,
      'fill-opacity': '0.08',
      stroke: 'none',
    }));
    compositeGroup.appendChild(svgEl('path', {
      d: compositePath,
      fill: 'none',
      stroke: lineColor,
      'stroke-width': '2',
      'stroke-linecap': 'round',
      'stroke-linejoin': 'round',
    }));
    if (hasSynthetic) {
      for (let i = 1; i < compositePoints.length; i++) {
        const isBackfilled = series[i - 1].synthetic || series[i].synthetic;
        if (!isBackfilled) continue;
        const segPath = `M${compositePoints[i - 1][0].toFixed(2)},${compositePoints[i - 1][1].toFixed(2)} L${compositePoints[i][0].toFixed(2)},${compositePoints[i][1].toFixed(2)}`;
        compositeGroup.appendChild(svgEl('path', {
          d: segPath,
          fill: 'none',
          stroke: 'var(--bg-card, #171C22)',
          'stroke-width': '2.5',
          'stroke-linecap': 'butt',
        }));
        compositeGroup.appendChild(svgEl('path', {
          d: segPath,
          fill: 'none',
          stroke: lineColor,
          'stroke-width': '2',
          'stroke-linecap': 'round',
          'stroke-dasharray': '3 3',
          'stroke-opacity': '0.75',
        }));
      }
    }
  }
  svg.appendChild(compositeGroup);

  // ---- Composite dots ----
  const dotsGroup = svgEl('g', { class: 'lthcs-chart-dots' });
  for (let i = 0; i < series.length; i++) {
    const [x, y] = compositePoints[i];
    const markerColor = bandColorForScore(series[i].score) || lineColor;
    if (series[i].synthetic) {
      dotsGroup.appendChild(svgEl('circle', {
        cx: x, cy: y, r: 2.75,
        fill: 'var(--bg-card, #171C22)',
        stroke: markerColor,
        'stroke-width': '1.5',
        class: 'lthcs-chart-dot-synthetic',
      }));
    } else {
      dotsGroup.appendChild(svgEl('circle', {
        cx: x, cy: y, r: 2.5,
        fill: markerColor,
        stroke: 'var(--bg-card, #171C22)',
        'stroke-width': '1.5',
      }));
    }
  }
  svg.appendChild(dotsGroup);

  // Last-point highlight
  {
    const [x, y] = compositePoints[compositePoints.length - 1];
    const halo = svgEl('circle', {
      cx: x, cy: y, r: 6,
      fill: lineColor,
      'fill-opacity': '0.20',
    });
    svg.appendChild(halo);
    svg.appendChild(svgEl('circle', {
      cx: x, cy: y, r: 3.5,
      fill: lineColor,
      stroke: 'var(--bg-card, #171C22)',
      'stroke-width': '1.5',
    }));
  }

  // ---- Hover plumbing ----
  const guide = svgEl('line', {
    x1: 0, x2: 0,
    y1: M.top, y2: M.top + plotH,
    stroke: 'currentColor',
    'stroke-opacity': '0.30',
    'stroke-dasharray': '2 3',
    visibility: 'hidden',
    class: 'lthcs-chart-guide',
  });
  svg.appendChild(guide);

  // Hover dot for composite (always present)
  const compositeHoverDot = svgEl('circle', {
    cx: 0, cy: 0, r: 4.5,
    fill: lineColor,
    stroke: 'var(--bg-card, #171C22)',
    'stroke-width': '2',
    visibility: 'hidden',
    class: 'lthcs-chart-hover-dot',
  });
  svg.appendChild(compositeHoverDot);

  const overlay = svgEl('rect', {
    x: M.left, y: M.top,
    width: plotW, height: plotH,
    fill: 'transparent',
    class: 'lthcs-chart-overlay',
  });
  svg.appendChild(overlay);

  chartEl.appendChild(svg);

  // ---- Build chart state ----
  const state = createChartState();
  state.svg = svg;
  state.overlay = overlay;
  state.guide = guide;
  state.tooltipEl = tooltipEl;
  state.chartEl = chartEl;
  state.W = W; state.H = H; state.M = M;
  state.dates = series.map(r => r.date);
  state.dateIndex = new Map(state.dates.map((d, i) => [d, i]));
  state.visibleKeys = new Set(['composite']);
  state.layersByKey.set('composite', compositeGroup);
  state.hoverDotsByKey.set('composite', compositeHoverDot);
  state.seriesByKey.set('composite', {
    points: compositePoints,
    data: series.map(r => ({ date: r.date, value: r.score, band: r.band })),
  });

  attachChartHover(state);
  activeChartState = state;

  // ---- Legend ----
  if (legendEl) {
    const compositeChip = buildLegendChip('composite', 'Composite', lineColor, true, state);
    legendEl.appendChild(compositeChip);
    for (const pillar of PILLAR_ORDER) {
      const chip = buildLegendChip(pillar, PILLAR_SHORT[pillar] || pillar, PILLAR_COLORS[pillar], false, state);
      legendEl.appendChild(chip);
    }
  }

  // Chart footnote
  const notes = [];
  if (hasSynthetic) notes.push('Hollow markers = backfilled days (CI gap).');
  if (series.length > 0 && series.length < 30) {
    notes.push(`Only ${series.length} day${series.length === 1 ? '' : 's'} of history; full chart available after 30+ days.`);
  }
  if (notes.length) setChartNote(panel, notes.join(' · '));
}

// Legend chip factory. Toggles its corresponding series; for pillars,
// triggers a lazy snapshot fetch on first activation in the session.
function buildLegendChip(key, label, color, initialActive, state) {
  const chip = el('button', {
    className: 'lthcs-chart-legend-chip',
    attrs: {
      type: 'button',
      'data-key': key,
      'data-active': initialActive ? 'true' : 'false',
      'aria-pressed': initialActive ? 'true' : 'false',
    },
  });
  chip.style.setProperty('--lthcs-chip-color', color || '#888');

  const swatch = el('span', { className: 'lthcs-chart-legend-swatch' });
  swatch.style.background = color || '#888';
  chip.appendChild(swatch);
  chip.appendChild(el('span', { className: 'lthcs-chart-legend-label', text: label }));

  chip.addEventListener('click', async () => {
    const wasActive = chip.getAttribute('data-active') === 'true';
    const nowActive = !wasActive;
    chip.setAttribute('data-active', nowActive ? 'true' : 'false');
    chip.setAttribute('aria-pressed', nowActive ? 'true' : 'false');

    if (key === 'composite') {
      setSeriesVisibility(state, 'composite', nowActive);
      return;
    }

    // Pillar
    setSeriesVisibility(state, key, nowActive);
    if (!nowActive) return;

    // Need data: kick off pillar series load if not present
    if (!state.seriesByKey.has(key)) {
      chip.classList.add('is-loading');
      try {
        const series = await ensurePillarSeriesForTicker(moduleState.activeTicker);
        // Bail if user moved on or chart was rebuilt
        if (activeChartState !== state) return;
        if (!series) {
          chip.classList.remove('is-loading');
          chip.classList.add('is-empty');
          chip.title = 'No pillar history available';
          return;
        }
        // Build per-date Map for this pillar
        const dataByDate = new Map();
        for (const row of series) {
          const v = row[key];
          if (Number.isFinite(Number(v))) dataByDate.set(row.date, Number(v));
        }
        setPillarSeries(state, key, dataByDate);
        chip.classList.remove('is-loading');
      } catch (err) {
        console.warn('LTHCS detail: pillar series load failed for', key, err);
        chip.classList.remove('is-loading');
        chip.classList.add('is-error');
        chip.title = 'Could not load pillar history';
      }
    }
  });

  return chip;
}

// Pillar-series aggregation. Fetches every daily snapshot (parallel,
// per-date cache shared across the module), builds per-ticker arrays of
// {date, adoption_momentum, institutional_confidence, ...}.
//
// Performance: 91 fetches × ~180KB = ~16MB transfer; runs in parallel so
// wall-time is bounded by max latency rather than sum. On a local static
// server (where this dashboard lives) this is <2s. Cached for the rest of
// the session so subsequent pillar toggles on any ticker are free.
async function ensurePillarSeriesIndex() {
  if (moduleState.pillarSeriesByTicker) return moduleState.pillarSeriesByTicker;
  if (moduleState.pillarSeriesPromise) return moduleState.pillarSeriesPromise;

  moduleState.pillarSeriesPromise = (async () => {
    // 1) get the list of dates
    let dates = [];
    try {
      const res = await fetch(`${SNAPSHOTS_BASE}/index.json`, { cache: 'no-store' });
      if (res.ok) {
        const idx = await res.json();
        if (Array.isArray(idx.dates)) dates = idx.dates.slice();
      }
    } catch (_) { /* fall through */ }
    if (!dates.length) {
      throw new Error('No snapshot date index available');
    }

    // 2) fetch in parallel; ignore individual failures
    const promises = dates.map(async (d) => {
      if (moduleState.snapshotCache.has(d)) return { date: d, snap: moduleState.snapshotCache.get(d) };
      try {
        const r = await fetch(`${SNAPSHOTS_BASE}/${d}.json`, { cache: 'no-store' });
        if (!r.ok) return { date: d, snap: null };
        const snap = await r.json();
        moduleState.snapshotCache.set(d, snap);
        return { date: d, snap };
      } catch (_) {
        return { date: d, snap: null };
      }
    });
    const settled = await Promise.all(promises);

    // 3) build per-ticker map: ticker → array of {date, ...subscores, composite}
    const byTicker = new Map();
    // Sort by ascending date (chronological) for chart-friendly iteration.
    settled.sort((a, b) => (a.date < b.date ? -1 : a.date > b.date ? 1 : 0));
    for (const { date, snap } of settled) {
      const scores = snap && Array.isArray(snap.scores) ? snap.scores : null;
      if (!scores) continue;
      for (const row of scores) {
        const t = row && row.ticker;
        if (!t) continue;
        const subs = row.subscores || {};
        const entry = {
          date,
          composite: Number(row.lthcs_score),
        };
        for (const p of PILLAR_ORDER) entry[p] = Number(subs[p]);
        let arr = byTicker.get(t);
        if (!arr) { arr = []; byTicker.set(t, arr); }
        arr.push(entry);
      }
    }
    moduleState.pillarSeriesByTicker = byTicker;
    return byTicker;
  })();

  try {
    return await moduleState.pillarSeriesPromise;
  } finally {
    // Allow retry on failure
    if (!moduleState.pillarSeriesByTicker) moduleState.pillarSeriesPromise = null;
  }
}

async function ensurePillarSeriesForTicker(ticker) {
  if (!ticker) return null;
  const idx = await ensurePillarSeriesIndex();
  return idx.get(ticker) || null;
}

function renderChartError(panel) {
  const chartEl = panel.querySelector('[data-slot="chart"]');
  clear(chartEl);
  setChartNote(panel, '');
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

// Per-paragraph rendering for the AI-narrative panel.
//
// We always render the full text (no server-side truncation), but for
// paragraphs over NARRATIVE_CLAMP_CHARS we add a one-click "Read more"
// toggle so the modal stays compact for the common case where users want
// the headline read, with the full text one tap away.
function buildNarrativePara(heading, body) {
  const para = el('div', { className: 'lthcs-narrative-para' });
  para.appendChild(el('h4', { text: heading }));

  const text = (body == null || body === '') ? '—' : String(body);
  const p = el('p', { className: 'lthcs-narrative-text', text });
  para.appendChild(p);

  const isLong = text.length > NARRATIVE_CLAMP_CHARS;
  if (isLong) {
    para.classList.add('is-clamped');
    const toggle = el('button', {
      className: 'lthcs-narrative-readmore',
      text: 'Read more',
      attrs: { type: 'button', 'aria-expanded': 'false' },
    });
    toggle.addEventListener('click', () => {
      const expanded = para.classList.toggle('is-expanded');
      toggle.textContent = expanded ? 'Read less' : 'Read more';
      toggle.setAttribute('aria-expanded', expanded ? 'true' : 'false');
    });
    para.appendChild(toggle);
  }
  return para;
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
    narrEl.appendChild(buildNarrativePara(heading, body));
  }

  // Footer chip with model confidence + model version when present.
  const confidence = narrative.confidence_level
    || (snapshotRow && snapshotRow.confidence_level)
    || null;
  if (confidence) {
    const meta = el('div', { className: 'lthcs-narrative-meta' });
    meta.appendChild(el('span', { text: `Narrative confidence: ${confidence}` }));
    narrEl.appendChild(meta);
  }
}

// ---------------------------------------------------------------------------
// Evidence panel (#19) — per-pillar variable-detail summary
//
// For each pillar we surface:
//   - sub_score (badge)
//   - top 3 components by magnitude of contribution to the sub_score
//   - data_quality boolean flags that are TRUE (chips)
//   - data_quality.source if present (e.g. "finnhub_recommendation")
//
// Backed by data/lthcs/variable_detail/<calc_date>.json which is also used
// by the legacy "Show variable detail" toggle below. We load it eagerly here
// because the evidence panel is now a primary surface of the modal — the
// raw-key/value variable-detail block stays as the deep-dive toggle.
// ---------------------------------------------------------------------------

// Component keys we never want to surface in the top-3 (they are metadata,
// peer-cohort labels, or strategy choices — not "signals that fired").
const COMPONENT_HIDE_KEYS = new Set([
  'peer_cohort_strategy',
  'peer_cohort_size',
  'sector_cohort',
  'sector_cohort_size',
  'momentum_strategy_used',
  'momentum_cohort_size',
  'momentum_cohort_label',
  'margin_source',
  'confidence_blend',
  'applied_overrides',
  'tier2_inputs',
  'label_counts',
  'signal_tilts',
  'signal_contributions',
]);

// Display labels for top-level component keys. We humanize anything not
// listed via humanCase().
const COMPONENT_LABEL = {
  revenue_growth_yoy: 'Revenue growth YoY',
  revenue_subscore: 'Revenue sub-score',
  trends_subscore: 'Google Trends sub-score',
  qoq_acceleration_pct: 'QoQ acceleration',
  qoq_subscore: 'QoQ sub-score',
  momentum_pct_90d: 'Price momentum (90d)',
  momentum_subscore: 'Momentum sub-score',
  inst_holdings_subscore: 'Institutional holdings sub-score',
  inst_holdings_change_qoq: 'Inst. holdings QoQ',
  base_sub_score: 'Base sub-score',
  combined_adjustment_pts: 'Combined adjustment',
  margin_subscore: 'Margin sub-score',
  ocf_subscore: 'Operating cash flow sub-score',
  ttm_ocf_margin: 'TTM OCF margin',
  margin_trend_slope: 'Margin trend slope',
  article_count: 'News article count',
  mean_sentiment_score: 'Mean sentiment',
  mean_relevance_score: 'Mean relevance',
  sentiment_subscore_raw: 'Sentiment sub-score (raw)',
  events_score_raw: 'Events sub-score (raw)',
  events_weight: 'Events weight',
  yahoo_earnings_score: 'Yahoo earnings score',
  sec_8k_score: 'SEC 8-K score',
  total_contribution: 'Macro contribution',
};

function componentLabel(key) {
  return COMPONENT_LABEL[key] || humanCase(key);
}

// Given a `components` object, return a ranked list of contributing
// signals as [{key, value, magnitude}], top N by magnitude. We use the
// absolute distance from a neutral baseline of 50 for sub-score-style
// numerics (those whose names end in `_subscore` or equal 50 by default),
// or |value| otherwise. Nested objects (e.g. components.trends) get
// "flattened" to one representative entry built from the most informative
// child key (sub_score-style if present, else signal_score-like).
function rankComponents(components, limit = 3) {
  if (!components || typeof components !== 'object') return [];
  const out = [];
  for (const [k, v] of Object.entries(components)) {
    if (COMPONENT_HIDE_KEYS.has(k)) continue;
    if (v == null) continue;

    if (typeof v === 'number') {
      if (!Number.isFinite(v)) continue;
      // Sub-score-style: ranked by distance from 50 (neutral).
      const isSubscore = /_subscore$|^sub_score$/.test(k) || k === 'base_sub_score';
      const magnitude = isSubscore ? Math.abs(v - 50) : Math.abs(v);
      out.push({ key: k, value: v, magnitude });
      continue;
    }

    if (typeof v === 'object' && !Array.isArray(v)) {
      // Nested signal block (insider, holdings, trends). Pick the "headline"
      // child: prefer `signal_score`/`conviction_score`/`adjustment_pts`,
      // fall back to the first numeric value.
      const candidates = ['signal_score', 'conviction_score', 'adjustment_pts', 'total_contribution', 'acceleration_4w_pct'];
      let pickedKey = null;
      let pickedVal = null;
      for (const c of candidates) {
        if (typeof v[c] === 'number' && Number.isFinite(v[c])) {
          pickedKey = c; pickedVal = v[c]; break;
        }
      }
      if (pickedKey == null) {
        for (const [ck, cv] of Object.entries(v)) {
          if (typeof cv === 'number' && Number.isFinite(cv)) {
            pickedKey = ck; pickedVal = cv; break;
          }
        }
      }
      // Even when no numeric child exists, surface a regime/signal string
      // as a top contributor — they're often the most readable evidence.
      if (pickedKey == null) {
        for (const labelKey of ['regime', 'conviction_signal', 'data_quality']) {
          if (typeof v[labelKey] === 'string' && v[labelKey]) {
            out.push({
              key: `${k}.${labelKey}`,
              value: v[labelKey],
              magnitude: 1, // small but non-zero so it can appear
              isCategorical: true,
            });
            break;
          }
        }
        continue;
      }
      out.push({
        key: `${k}.${pickedKey}`,
        value: pickedVal,
        magnitude: Math.abs(pickedVal),
      });
      continue;
    }

    if (typeof v === 'boolean') {
      // Boolean flags are minor contributors; only surface true ones.
      if (v) out.push({ key: k, value: true, magnitude: 0.1, isCategorical: true });
      continue;
    }
  }

  out.sort((a, b) => b.magnitude - a.magnitude);
  return out.slice(0, limit);
}

// Pretty-print a component value for display. Numbers get smart precision;
// objects degrade to JSON; strings + booleans pass through.
function fmtEvidenceValue(v) {
  if (v == null) return '—';
  if (typeof v === 'number') {
    if (!Number.isFinite(v)) return String(v);
    if (Math.abs(v) >= 100 && Number.isInteger(v)) return String(v);
    if (Math.abs(v) >= 10) return v.toFixed(1);
    if (Math.abs(v) >= 1) return v.toFixed(2);
    if (Math.abs(v) > 0.001) return v.toFixed(3);
    return v.toFixed(4);
  }
  if (typeof v === 'boolean') return v ? 'true' : 'false';
  if (typeof v === 'string') return humanCase(v);
  try { return JSON.stringify(v); } catch { return String(v); }
}

// True flags from a data_quality object (booleans only). Strings like
// `source` and numeric fields like `days_since_scored` are handled
// separately in the renderer.
function trueFlags(dq) {
  if (!dq || typeof dq !== 'object') return [];
  const out = [];
  for (const [k, v] of Object.entries(dq)) {
    if (v === true) out.push(k);
  }
  return out;
}

function renderEvidence(panel, { snapshotRow, vardetailRows, ticker }) {
  const wrap = panel.querySelector('[data-slot="evidence"]');
  if (!wrap) return;
  clear(wrap);

  if (!Array.isArray(vardetailRows) || vardetailRows.length === 0) {
    wrap.appendChild(el('div', {
      className: 'lthcs-evidence-placeholder',
      text: 'Variable detail not available for this snapshot.',
    }));
    return;
  }

  const byPillar = new Map();
  for (const row of vardetailRows) {
    if (!row || row.ticker !== ticker) continue;
    byPillar.set(row.pillar, row);
  }

  // Stable order, matches PILLAR_ORDER and the pillar-breakdown bars above.
  const subscores = (snapshotRow && snapshotRow.subscores) || {};

  let rendered = 0;
  for (const pillar of PILLAR_ORDER) {
    const row = byPillar.get(pillar);
    const subFromSnapshot = Number(subscores[pillar]);
    const subFromRow = row ? Number(row.sub_score) : NaN;
    const sub = Number.isFinite(subFromRow) ? subFromRow : subFromSnapshot;

    const acc = el('details', { className: 'lthcs-evidence-pillar' });
    if (rendered === 0) acc.setAttribute('open', 'open'); // first pillar open by default
    const sum = el('summary', { className: 'lthcs-evidence-summary' });

    const nameWrap = el('span', { className: 'lthcs-evidence-name' });
    nameWrap.appendChild(el('span', { className: 'lthcs-evidence-pillar-name', text: PILLAR_DISPLAY[pillar] || pillar }));
    sum.appendChild(nameWrap);

    const scoreBadge = el('span', { className: 'lthcs-evidence-score' });
    scoreBadge.textContent = fmtScore(sub);
    if (Number.isFinite(sub)) {
      const color = bandColorForScore(sub);
      if (color) scoreBadge.style.background = color;
    }
    sum.appendChild(scoreBadge);

    acc.appendChild(sum);

    const body = el('div', { className: 'lthcs-evidence-body' });

    if (!row) {
      body.appendChild(el('div', {
        className: 'lthcs-evidence-empty',
        text: 'No variable-detail row for this pillar.',
      }));
      acc.appendChild(body);
      wrap.appendChild(acc);
      rendered++;
      continue;
    }

    // Top contributing components
    const top = rankComponents(row.components, 3);
    if (top.length) {
      const list = el('ul', { className: 'lthcs-evidence-components' });
      for (const c of top) {
        const li = el('li', { className: 'lthcs-evidence-component' });
        li.appendChild(el('span', { className: 'lthcs-evidence-component-key', text: componentLabel(c.key) }));
        li.appendChild(el('span', { className: 'lthcs-evidence-component-val', text: fmtEvidenceValue(c.value) }));
        list.appendChild(li);
      }
      body.appendChild(list);
    } else {
      body.appendChild(el('div', {
        className: 'lthcs-evidence-empty',
        text: 'No numeric components recorded.',
      }));
    }

    // Data-quality flags (booleans + source)
    const dq = row.data_quality || {};
    const flags = trueFlags(dq);
    const source = (typeof dq.source === 'string' && dq.source) ? dq.source : null;
    const staleness = (typeof dq.is_stale === 'boolean') ? dq.is_stale : null;

    if (flags.length || source || staleness) {
      const chips = el('div', { className: 'lthcs-evidence-chips' });
      if (source) {
        chips.appendChild(el('span', {
          className: 'lthcs-evidence-chip lthcs-evidence-chip-source',
          text: `source: ${source}`,
        }));
      }
      for (const f of flags) {
        chips.appendChild(el('span', {
          className: 'lthcs-evidence-chip',
          text: f,
        }));
      }
      if (staleness === true) {
        chips.appendChild(el('span', {
          className: 'lthcs-evidence-chip lthcs-evidence-chip-stale',
          text: 'stale',
        }));
      }
      body.appendChild(chips);
    }

    acc.appendChild(body);
    wrap.appendChild(acc);
    rendered++;
  }

  if (rendered === 0) {
    wrap.appendChild(el('div', {
      className: 'lthcs-evidence-placeholder',
      text: 'No pillar evidence available.',
    }));
  }
}

function renderEvidenceLoading(panel) {
  const wrap = panel.querySelector('[data-slot="evidence"]');
  if (!wrap) return;
  clear(wrap);
  wrap.appendChild(el('div', {
    className: 'lthcs-evidence-placeholder',
    text: 'Loading per-pillar evidence…',
  }));
}

function renderEvidenceError(panel) {
  const wrap = panel.querySelector('[data-slot="evidence"]');
  if (!wrap) return;
  clear(wrap);
  wrap.appendChild(el('div', {
    className: 'lthcs-evidence-placeholder',
    text: 'Could not load per-pillar evidence.',
  }));
}

// ---------------------------------------------------------------------------
// Insider conviction (Week 11) — SEC Form 4 90-day window
// ---------------------------------------------------------------------------

const INSIDER_REGIME_LABEL = {
  strong_buying: 'Strong buying',
  mild_buying: 'Mild buying',
  neutral: 'Neutral',
  mild_selling: 'Mild selling',
  heavy_selling: 'Heavy selling',
};

function fmtDollars(n) {
  const v = Number(n);
  if (!Number.isFinite(v)) return '—';
  const abs = Math.abs(v);
  const sign = v < 0 ? '-' : '';
  if (abs >= 1e9) return `${sign}$${(abs / 1e9).toFixed(2)}B`;
  if (abs >= 1e6) return `${sign}$${(abs / 1e6).toFixed(2)}M`;
  if (abs >= 1e3) return `${sign}$${(abs / 1e3).toFixed(1)}K`;
  return `${sign}$${abs.toFixed(0)}`;
}

function fmtShares(n) {
  const v = Number(n);
  if (!Number.isFinite(v)) return '—';
  if (Math.abs(v) >= 1e6) return `${(v / 1e6).toFixed(2)}M`;
  if (Math.abs(v) >= 1e3) return `${(v / 1e3).toFixed(1)}K`;
  return String(Math.round(v));
}

function fmtConvictionPct(score) {
  const v = Number(score);
  if (!Number.isFinite(v)) return 0;
  // score is in [-1, 1]; map to [0, 100] for bar width.
  return Math.max(0, Math.min(100, ((v + 1) / 2) * 100));
}

function fmtConvictionText(score) {
  const v = Number(score);
  if (!Number.isFinite(v)) return '—';
  const sign = v > 0 ? '+' : v < 0 ? '−' : '';
  return `${sign}${Math.abs(v).toFixed(2)}`;
}

function renderInsider(panel, { insider }) {
  const wrap = panel.querySelector('[data-slot="insider-wrap"]');
  if (!wrap) return;
  clear(wrap);

  if (!insider || typeof insider !== 'object') {
    // No record at all: render a tiny "no activity" line so the section is acknowledged.
    const section = el('div', { className: 'lthcs-modal-insider' });
    section.appendChild(el('h3', { className: 'lthcs-modal-section-heading', text: 'Insider conviction' }));
    section.appendChild(el('div', {
      className: 'lthcs-insider-empty',
      text: 'No recent Form 4 activity in 90-day window.',
    }));
    wrap.appendChild(section);
    return;
  }

  const regime = String(insider.regime || 'neutral');
  const regimeLabel = INSIDER_REGIME_LABEL[regime] || humanCase(regime);
  const conviction = Number(insider.conviction_score);
  const buyCount = Number(insider.buy_count) || 0;
  const sellCount = Number(insider.sell_count) || 0;
  const windowDays = Number(insider.window_days) || 90;
  const cluster = !!insider.cluster_buying;
  const ceoCfo = String(insider.ceo_cfo_action || 'neutral');
  const netDollar = Number(insider.net_dollar_value);

  const section = el('div', { className: 'lthcs-modal-insider' });
  section.appendChild(el('h3', { className: 'lthcs-modal-section-heading', text: 'Insider conviction' }));

  // Header row: regime badge + (optional) cluster badge
  const headerRow = el('div', { className: 'lthcs-insider-header' });
  headerRow.appendChild(el('span', {
    className: 'lthcs-insider-regime',
    text: regimeLabel,
    attrs: { 'data-regime': regime },
  }));
  if (cluster) {
    headerRow.appendChild(el('span', {
      className: 'lthcs-insider-cluster',
      text: '🔥 CLUSTER BUYING',
      attrs: { 'aria-label': 'Cluster buying flag' },
    }));
  }
  headerRow.appendChild(el('span', {
    className: 'lthcs-insider-window',
    text: `${windowDays}-day window`,
  }));
  section.appendChild(headerRow);

  // Conviction bar
  const convWrap = el('div', { className: 'lthcs-insider-conviction' });
  const convLabel = el('div', { className: 'lthcs-insider-conviction-label' });
  convLabel.appendChild(el('span', { text: 'Conviction' }));
  convLabel.appendChild(el('strong', { text: fmtConvictionText(conviction) }));
  convWrap.appendChild(convLabel);
  const track = el('div', { className: 'lthcs-insider-conviction-track' });
  // mid-line marker at 50%
  track.appendChild(el('div', { className: 'lthcs-insider-conviction-mid', attrs: { 'aria-hidden': 'true' } }));
  const fill = el('div', { className: 'lthcs-insider-conviction-fill' });
  if (Number.isFinite(conviction)) {
    const pct = fmtConvictionPct(conviction);
    fill.style.left = conviction >= 0 ? '50%' : `${pct}%`;
    fill.style.width = `${Math.abs(pct - 50)}%`;
    fill.setAttribute('data-direction', conviction >= 0 ? 'buy' : 'sell');
  }
  track.appendChild(fill);
  convWrap.appendChild(track);
  section.appendChild(convWrap);

  // Counts + dollar value
  const stats = el('div', { className: 'lthcs-insider-stats' });
  const buyStat = el('div', { className: 'lthcs-insider-stat', attrs: { 'data-kind': 'buy' } });
  buyStat.appendChild(el('span', { className: 'lthcs-insider-stat-num', text: String(buyCount) }));
  buyStat.appendChild(el('span', { className: 'lthcs-insider-stat-label', text: buyCount === 1 ? 'open-market buy' : 'open-market buys' }));
  stats.appendChild(buyStat);

  const sellStat = el('div', { className: 'lthcs-insider-stat', attrs: { 'data-kind': 'sell' } });
  sellStat.appendChild(el('span', { className: 'lthcs-insider-stat-num', text: String(sellCount) }));
  sellStat.appendChild(el('span', { className: 'lthcs-insider-stat-label', text: sellCount === 1 ? 'sell' : 'sells' }));
  stats.appendChild(sellStat);

  if (Number.isFinite(netDollar) && netDollar !== 0) {
    const netStat = el('div', {
      className: 'lthcs-insider-stat',
      attrs: { 'data-kind': netDollar >= 0 ? 'buy' : 'sell' },
    });
    netStat.appendChild(el('span', { className: 'lthcs-insider-stat-num', text: fmtDollars(netDollar) }));
    netStat.appendChild(el('span', { className: 'lthcs-insider-stat-label', text: 'net 90d' }));
    stats.appendChild(netStat);
  }
  section.appendChild(stats);

  // CEO/CFO action — only if not neutral
  if (ceoCfo && ceoCfo !== 'neutral') {
    const ceoLine = el('div', {
      className: 'lthcs-insider-ceo',
      attrs: { 'data-action': ceoCfo },
    });
    ceoLine.appendChild(el('span', { className: 'lthcs-insider-ceo-label', text: 'CEO/CFO:' }));
    ceoLine.appendChild(el('strong', { text: ceoCfo }));
    section.appendChild(ceoLine);
  }

  // Raw transactions (collapsible)
  const txns = Array.isArray(insider.raw_transactions) ? insider.raw_transactions : [];
  if (txns.length > 0) {
    const details = el('details', { className: 'lthcs-insider-txns' });
    const summary = el('summary', { className: 'lthcs-insider-txns-summary' });
    summary.textContent = `Recent transactions (showing ${Math.min(5, txns.length)} of ${txns.length})`;
    details.appendChild(summary);

    const tableWrap = el('div', { className: 'lthcs-insider-txns-wrap' });
    const table = el('table', { className: 'lthcs-insider-txns-table' });
    const thead = el('thead');
    const headRow = el('tr');
    for (const h of ['Date', 'Insider', 'Role', 'Code', 'Shares', 'Value']) {
      headRow.appendChild(el('th', { text: h }));
    }
    thead.appendChild(headRow);
    table.appendChild(thead);

    const tbody = el('tbody');
    for (const tx of txns.slice(0, 5)) {
      const tr = el('tr');
      tr.appendChild(el('td', { text: String(tx.date || '—') }));
      tr.appendChild(el('td', { text: String(tx.insider || '—') }));
      tr.appendChild(el('td', { text: String(tx.role || '—') }));
      const codeTd = el('td');
      const code = String(tx.code || '');
      codeTd.appendChild(el('span', {
        className: 'lthcs-insider-code',
        text: code || '—',
        attrs: { 'data-code': code },
      }));
      tr.appendChild(codeTd);
      tr.appendChild(el('td', { text: fmtShares(tx.shares) }));
      tr.appendChild(el('td', { text: fmtDollars(tx.value) }));
      tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    tableWrap.appendChild(table);
    details.appendChild(tableWrap);
    section.appendChild(details);
  }

  wrap.appendChild(section);
}

// ---------------------------------------------------------------------------
// 13F institutional holdings (Week 11+) — universe-wide JSON, lazy-loaded
// ---------------------------------------------------------------------------

const HOLDINGS_SIGNAL_LABEL = {
  accumulating: 'Accumulating',
  steady: 'Steady',
  distributing: 'Distributing',
  mixed: 'Mixed',
};

function fmtSignedPct(n) {
  const v = Number(n);
  if (!Number.isFinite(v)) return '—';
  const sign = v > 0 ? '+' : v < 0 ? '−' : '';
  return `${sign}${Math.abs(v).toFixed(2)}%`;
}

function fmtSignedInt(n) {
  const v = Number(n);
  if (!Number.isFinite(v)) return '0';
  const sign = v > 0 ? '+' : v < 0 ? '−' : '';
  return `${sign}${Math.abs(Math.trunc(v))}`;
}

function fmtSignalText(score) {
  const v = Number(score);
  if (!Number.isFinite(v)) return '—';
  const sign = v > 0 ? '+' : v < 0 ? '−' : '';
  return `${sign}${Math.abs(v).toFixed(2)}`;
}

function fmtSharesMm(n) {
  const v = Number(n);
  if (!Number.isFinite(v)) return '—';
  if (Math.abs(v) >= 1000) return `${(v / 1000).toFixed(2)}B`;
  return `${v.toFixed(1)}M`;
}

function fmtValueBn(n) {
  const v = Number(n);
  if (!Number.isFinite(v)) return '—';
  if (Math.abs(v) >= 1000) return `$${(v / 1000).toFixed(2)}T`;
  if (Math.abs(v) >= 1) return `$${v.toFixed(2)}B`;
  return `$${(v * 1000).toFixed(0)}M`;
}

async function loadHoldings(calcDate) {
  if (!calcDate) throw new Error('no calc_date');
  if (moduleState.holdingsCache.has(calcDate)) {
    return moduleState.holdingsCache.get(calcDate);
  }
  const res = await fetch(`${HOLDINGS_BASE}/${calcDate}.json`, { cache: 'no-store' });
  if (!res.ok) throw new Error(`holdings fetch failed: ${res.status}`);
  const data = await res.json();
  moduleState.holdingsCache.set(calcDate, data);
  return data;
}

function renderHoldingsSkeleton(panel) {
  const wrap = panel.querySelector('[data-slot="holdings-wrap"]');
  if (!wrap) return;
  clear(wrap);
  const section = el('div', { className: 'lthcs-modal-holdings' });
  section.appendChild(el('h3', {
    className: 'lthcs-modal-section-heading',
    text: 'Institutional holdings',
  }));
  section.appendChild(el('div', {
    className: 'lthcs-holdings-loading',
    text: 'Loading institutional holdings…',
  }));
  wrap.appendChild(section);
}

function renderHoldingsEmpty(panel, message) {
  const wrap = panel.querySelector('[data-slot="holdings-wrap"]');
  if (!wrap) return;
  clear(wrap);
  const section = el('div', { className: 'lthcs-modal-holdings' });
  section.appendChild(el('h3', {
    className: 'lthcs-modal-section-heading',
    text: 'Institutional holdings',
  }));
  section.appendChild(el('div', {
    className: 'lthcs-holdings-empty',
    text: message,
  }));
  wrap.appendChild(section);
}

function renderHoldings(panel, { holdings }) {
  const wrap = panel.querySelector('[data-slot="holdings-wrap"]');
  if (!wrap) return;
  clear(wrap);

  if (!holdings || typeof holdings !== 'object') {
    renderHoldingsEmpty(panel,
      "No tracked-manager 13F coverage for this ticker (small/mid-cap; the 21 tracked institutions don't hold it).",
    );
    return;
  }

  const signal = String(holdings.conviction_signal || 'steady');
  const signalLabel = HOLDINGS_SIGNAL_LABEL[signal] || humanCase(signal);
  const signalScore = Number(holdings.signal_score);
  const managerCount = Number(holdings.manager_count) || 0;
  const quarter = String(holdings.latest_quarter || '');
  const dataQuality = String(holdings.data_quality || '');
  const totalShares = Number(holdings.total_shares_held_mm);
  const totalValue = Number(holdings.total_value_held_bn);
  const qoq = (holdings.quarter_over_quarter && typeof holdings.quarter_over_quarter === 'object')
    ? holdings.quarter_over_quarter : {};
  const holders = Array.isArray(holdings.top_holders) ? holdings.top_holders : [];

  if (managerCount === 0 && holders.length === 0) {
    renderHoldingsEmpty(panel,
      "No tracked-manager 13F coverage for this ticker (small/mid-cap; the 21 tracked institutions don't hold it).",
    );
    return;
  }

  const section = el('div', { className: 'lthcs-modal-holdings' });
  const heading = el('h3', { className: 'lthcs-modal-section-heading' });
  heading.textContent = quarter
    ? `Institutional holdings (${quarter})`
    : 'Institutional holdings';
  section.appendChild(heading);

  // Header row: signal badge + manager count + data quality
  const headerRow = el('div', { className: 'lthcs-holdings-header' });
  headerRow.appendChild(el('span', {
    className: 'lthcs-holdings-signal',
    text: signalLabel,
    attrs: { 'data-signal': signal },
  }));

  if (Number.isFinite(signalScore)) {
    headerRow.appendChild(el('span', {
      className: 'lthcs-holdings-signal-score',
      text: `${fmtSignalText(signalScore)} conviction`,
    }));
  }

  headerRow.appendChild(el('span', {
    className: 'lthcs-holdings-managers',
    text: `${managerCount} tracked manager${managerCount === 1 ? '' : 's'}`,
  }));

  if (dataQuality) {
    headerRow.appendChild(el('span', {
      className: 'lthcs-holdings-quality',
      text: dataQuality,
      attrs: { 'data-quality': dataQuality },
    }));
  }
  section.appendChild(headerRow);

  // Conviction bipolar bar (mirrors insider style)
  const convWrap = el('div', { className: 'lthcs-holdings-conviction' });
  const convLabel = el('div', { className: 'lthcs-holdings-conviction-label' });
  convLabel.appendChild(el('span', { text: 'Signal' }));
  convLabel.appendChild(el('strong', { text: fmtSignalText(signalScore) }));
  convWrap.appendChild(convLabel);
  const track = el('div', { className: 'lthcs-holdings-conviction-track' });
  track.appendChild(el('div', { className: 'lthcs-holdings-conviction-mid', attrs: { 'aria-hidden': 'true' } }));
  const fill = el('div', { className: 'lthcs-holdings-conviction-fill' });
  if (Number.isFinite(signalScore)) {
    // signal_score in [-1, 1]; map to bar width centered at 50%
    const pct = Math.max(0, Math.min(100, ((signalScore + 1) / 2) * 100));
    fill.style.left = signalScore >= 0 ? '50%' : `${pct}%`;
    fill.style.width = `${Math.abs(pct - 50)}%`;
    fill.setAttribute('data-direction',
      signalScore > 0 ? 'accumulating'
      : signalScore < 0 ? 'distributing'
      : 'neutral');
  }
  track.appendChild(fill);
  convWrap.appendChild(track);
  section.appendChild(convWrap);

  // Net flow line
  const netBuyers = Number(qoq.net_buyers) || 0;
  const netSellers = Number(qoq.net_sellers) || 0;
  const unchanged = Number(qoq.unchanged) || 0;
  const shareChange = Number(qoq.share_change_pct);
  const mgrChange = Number(qoq.manager_count_change);
  const priorQ = String(qoq.prior_quarter || '');

  const flowRow = el('div', { className: 'lthcs-holdings-flow' });
  const flowLabel = el('span', {
    className: 'lthcs-holdings-flow-label',
    text: priorQ ? `Net flow vs ${priorQ}:` : 'Net flow:',
  });
  flowRow.appendChild(flowLabel);
  flowRow.appendChild(el('span', {
    className: 'lthcs-holdings-flow-stat',
    text: `${netBuyers} buyer${netBuyers === 1 ? '' : 's'}`,
    attrs: { 'data-kind': 'buy' },
  }));
  flowRow.appendChild(el('span', { className: 'lthcs-holdings-flow-sep', text: '/' }));
  flowRow.appendChild(el('span', {
    className: 'lthcs-holdings-flow-stat',
    text: `${netSellers} seller${netSellers === 1 ? '' : 's'}`,
    attrs: { 'data-kind': 'sell' },
  }));
  flowRow.appendChild(el('span', { className: 'lthcs-holdings-flow-sep', text: '/' }));
  flowRow.appendChild(el('span', {
    className: 'lthcs-holdings-flow-stat',
    text: `${unchanged} unchanged`,
    attrs: { 'data-kind': 'flat' },
  }));
  section.appendChild(flowRow);

  // Totals + QoQ deltas
  const totalsRow = el('div', { className: 'lthcs-holdings-totals' });
  if (Number.isFinite(totalShares)) {
    const stat = el('div', { className: 'lthcs-holdings-total' });
    stat.appendChild(el('span', { className: 'lthcs-holdings-total-num', text: fmtSharesMm(totalShares) }));
    stat.appendChild(el('span', { className: 'lthcs-holdings-total-label', text: 'tracked shares' }));
    totalsRow.appendChild(stat);
  }
  if (Number.isFinite(totalValue)) {
    const stat = el('div', { className: 'lthcs-holdings-total' });
    stat.appendChild(el('span', { className: 'lthcs-holdings-total-num', text: fmtValueBn(totalValue) }));
    stat.appendChild(el('span', { className: 'lthcs-holdings-total-label', text: 'tracked value' }));
    totalsRow.appendChild(stat);
  }
  if (Number.isFinite(shareChange) && shareChange !== 0) {
    const stat = el('div', { className: 'lthcs-holdings-total', attrs: { 'data-dir': shareChange >= 0 ? 'up' : 'down' } });
    stat.appendChild(el('span', { className: 'lthcs-holdings-total-num', text: fmtSignedPct(shareChange) }));
    stat.appendChild(el('span', { className: 'lthcs-holdings-total-label', text: 'QoQ shares' }));
    totalsRow.appendChild(stat);
  }
  if (Number.isFinite(mgrChange) && mgrChange !== 0) {
    const stat = el('div', { className: 'lthcs-holdings-total', attrs: { 'data-dir': mgrChange >= 0 ? 'up' : 'down' } });
    stat.appendChild(el('span', { className: 'lthcs-holdings-total-num', text: fmtSignedInt(mgrChange) }));
    stat.appendChild(el('span', { className: 'lthcs-holdings-total-label', text: 'manager Δ' }));
    totalsRow.appendChild(stat);
  }
  if (totalsRow.childElementCount > 0) section.appendChild(totalsRow);

  // Top holders (collapsible details, default open)
  if (holders.length > 0) {
    const details = el('details', { className: 'lthcs-holdings-holders', attrs: { open: 'open' } });
    const summary = el('summary', { className: 'lthcs-holdings-holders-summary' });
    const topN = Math.min(3, holders.length);
    summary.textContent = `Top ${topN} holder${topN === 1 ? '' : 's'} (of ${holders.length})`;
    details.appendChild(summary);

    const tableWrap = el('div', { className: 'lthcs-holdings-holders-wrap' });
    const table = el('table', { className: 'lthcs-holdings-holders-table' });
    const thead = el('thead');
    const headRow = el('tr');
    for (const h of ['Manager', 'Shares', 'Value', 'Rank']) {
      headRow.appendChild(el('th', { text: h }));
    }
    thead.appendChild(headRow);
    table.appendChild(thead);

    const tbody = el('tbody');
    for (const h of holders.slice(0, 3)) {
      const tr = el('tr');
      tr.appendChild(el('td', { text: String(h.manager || '—') }));
      tr.appendChild(el('td', { text: fmtSharesMm(h.shares_mm) }));
      tr.appendChild(el('td', { text: fmtValueBn(h.value_bn) }));
      const rankTd = el('td');
      rankTd.appendChild(el('span', {
        className: 'lthcs-holdings-rank',
        text: `#${h.rank || '—'}`,
      }));
      tr.appendChild(rankTd);
      tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    tableWrap.appendChild(table);
    details.appendChild(tableWrap);
    section.appendChild(details);
  }

  wrap.appendChild(section);
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
 * @param {Object|null} [args.insider] Optional SEC Form 4 record for this ticker.
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
  const insider = (args && args.insider) || null;

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
  renderEvidenceLoading(panel);
  renderInsider(panel, { insider });
  renderHoldingsSkeleton(panel);
  renderFlags(panel, { snapshotRow });
  wireVardetailToggle(panel, { ticker, calcDate });

  // Show
  root.classList.remove('hidden');
  root.setAttribute('aria-hidden', 'false');
  document.body.style.overflow = 'hidden';

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

  // Async: fetch history (cached per ticker)
  const requestedTicker = ticker;
  const cachedHistory = moduleState.historyCache.get(requestedTicker);
  if (cachedHistory) {
    renderChart(panel, cachedHistory);
  } else {
    fetch(`${HISTORY_BASE}/${encodeURIComponent(ticker)}.json`, { cache: 'no-store' })
      .then((res) => {
        if (!res.ok) throw new Error(`history ${res.status}`);
        return res.json();
      })
      .then((history) => {
        moduleState.historyCache.set(requestedTicker, history);
        if (moduleState.activeTicker !== requestedTicker) return;
        renderChart(panel, history);
      })
      .catch((err) => {
        console.warn('LTHCS detail: history fetch failed for', requestedTicker, err);
        if (moduleState.activeTicker !== requestedTicker) return;
        renderChartError(panel);
      });
  }

  // Async: fetch holdings (cached per calc_date)
  if (calcDate) {
    loadHoldings(calcDate)
      .then((map) => {
        if (moduleState.activeTicker !== requestedTicker) return;
        const entry = (map && typeof map === 'object') ? map[requestedTicker] : null;
        renderHoldings(panel, { holdings: entry || null });
      })
      .catch((err) => {
        console.warn('LTHCS detail: holdings fetch failed for', requestedTicker, err);
        if (moduleState.activeTicker !== requestedTicker) return;
        renderHoldings(panel, { holdings: null });
      });
  } else {
    renderHoldings(panel, { holdings: null });
  }

  // Async: fetch variable_detail eagerly for the per-pillar Evidence panel.
  // The legacy raw "Show variable detail" toggle below shares this cache.
  if (calcDate) {
    loadVardetail(calcDate)
      .then((data) => {
        if (moduleState.activeTicker !== requestedTicker) return;
        const rows = (data && Array.isArray(data.variables)) ? data.variables : [];
        renderEvidence(panel, { snapshotRow, vardetailRows: rows, ticker: requestedTicker });
      })
      .catch((err) => {
        console.warn('LTHCS detail: variable_detail load failed', err);
        if (moduleState.activeTicker !== requestedTicker) return;
        renderEvidenceError(panel);
      });
  } else {
    renderEvidenceError(panel);
  }
}

export function closeDetail() {
  const root = moduleState.rootEl;
  if (!root) return;
  if (root.classList.contains('hidden')) return;
  root.classList.add('hidden');
  root.setAttribute('aria-hidden', 'true');
  document.body.style.overflow = '';
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
