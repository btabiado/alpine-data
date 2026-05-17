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

// Sort + normalize: returns ascending-by-date list of {date, score, band}.
function normalizeHistory(series) {
  const rows = [];
  for (const raw of series) {
    if (!raw) continue;
    const d = String(raw.date || '');
    // Both legacy `composite_score` and current `score` are accepted.
    const sRaw = (raw.composite_score != null) ? raw.composite_score : raw.score;
    const s = Number(sRaw);
    if (!d || !Number.isFinite(s)) continue;
    rows.push({ date: d, score: s, band: raw.band || null });
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

function renderChart(panel, history) {
  const chartEl = panel.querySelector('[data-slot="chart"]');
  const tooltipEl = panel.querySelector('[data-slot="chart-tooltip"]');
  clear(chartEl);
  if (tooltipEl) {
    tooltipEl.textContent = '';
    tooltipEl.classList.remove('visible');
  }
  setChartNote(panel, '');

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
  // viewBox uses fixed units; CSS scales width to 100%. Height aspect ratio is
  // preserved via preserveAspectRatio=none on the wrapper rules, but for the
  // chart we keep aspect ratio so axes stay legible.
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
    'aria-label': 'Composite score history chart',
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
      x: M.left,
      y: yTop,
      width: plotW,
      height: h,
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

  // ---- Line path (colored by latest band) ----
  const points = series.map((row, i) => [xFor(i), yFor(row.score)]);
  const latestScore = series[series.length - 1].score;
  const lineColor = bandColorForScore(latestScore) || '#6EA8FE';
  const d = smoothPath(points);
  if (d) {
    // Faint fill below the line for visual weight
    const fillD = `${d} L${points[points.length-1][0].toFixed(2)},${(M.top+plotH).toFixed(2)} L${points[0][0].toFixed(2)},${(M.top+plotH).toFixed(2)} Z`;
    svg.appendChild(svgEl('path', {
      d: fillD,
      fill: lineColor,
      'fill-opacity': '0.08',
      stroke: 'none',
    }));
    svg.appendChild(svgEl('path', {
      d,
      fill: 'none',
      stroke: lineColor,
      'stroke-width': '2',
      'stroke-linecap': 'round',
      'stroke-linejoin': 'round',
    }));
  }

  // ---- Dots ----
  const dotsGroup = svgEl('g', { class: 'lthcs-chart-dots' });
  for (let i = 0; i < series.length; i++) {
    const [x, y] = points[i];
    dotsGroup.appendChild(svgEl('circle', {
      cx: x, cy: y, r: 2.5,
      fill: bandColorForScore(series[i].score) || lineColor,
      stroke: 'var(--bg-card, #171C22)',
      'stroke-width': '1.5',
    }));
  }
  svg.appendChild(dotsGroup);

  // Last-point highlight
  {
    const [x, y] = points[points.length - 1];
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

  // ---- Hover interactivity ----
  // Vertical guide + invisible overlay that snaps to nearest point.
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

  const hoverDot = svgEl('circle', {
    cx: 0, cy: 0, r: 4.5,
    fill: lineColor,
    stroke: 'var(--bg-card, #171C22)',
    'stroke-width': '2',
    visibility: 'hidden',
    class: 'lthcs-chart-hover-dot',
  });
  svg.appendChild(hoverDot);

  // Transparent overlay catches all pointer events across the whole plot.
  const overlay = svgEl('rect', {
    x: M.left, y: M.top,
    width: plotW, height: plotH,
    fill: 'transparent',
    class: 'lthcs-chart-overlay',
  });
  svg.appendChild(overlay);

  const hideHover = () => {
    guide.setAttribute('visibility', 'hidden');
    hoverDot.setAttribute('visibility', 'hidden');
    if (tooltipEl) {
      tooltipEl.classList.remove('visible');
      tooltipEl.setAttribute('aria-hidden', 'true');
    }
  };

  const showHoverAt = (clientX, clientY) => {
    if (!tooltipEl) return;
    const rect = svg.getBoundingClientRect();
    if (rect.width === 0 || rect.height === 0) return;
    const localX = (clientX - rect.left) * (W / rect.width);
    // Find nearest series index by x
    let bestI = 0;
    let bestDx = Infinity;
    for (let i = 0; i < points.length; i++) {
      const dx = Math.abs(points[i][0] - localX);
      if (dx < bestDx) { bestDx = dx; bestI = i; }
    }
    const [px, py] = points[bestI];
    guide.setAttribute('x1', px);
    guide.setAttribute('x2', px);
    guide.setAttribute('visibility', 'visible');
    hoverDot.setAttribute('cx', px);
    hoverDot.setAttribute('cy', py);
    hoverDot.setAttribute('visibility', 'visible');

    const row = series[bestI];
    const bandLabel = row.band ? humanCase(row.band) : (function () {
      // Derive from score if band is missing
      for (const b of BAND_BUCKETS) if (row.score >= b.min && row.score < b.max) return humanCase(b.key);
      return '';
    })();
    tooltipEl.textContent = '';
    const dateEl = el('span', { className: 'lthcs-chart-tip-date', text: formatTickDate(row.date) });
    const scoreEl = el('strong', { className: 'lthcs-chart-tip-score', text: row.score.toFixed(1) });
    const bandEl = el('span', { className: 'lthcs-chart-tip-band', text: bandLabel });
    if (bandLabel) bandEl.setAttribute('data-band', row.band || '');
    tooltipEl.appendChild(dateEl);
    tooltipEl.appendChild(scoreEl);
    if (bandLabel) tooltipEl.appendChild(bandEl);

    // Position tooltip — clamped to chart wrap
    const wrap = chartEl.parentElement;
    const wrapRect = wrap ? wrap.getBoundingClientRect() : rect;
    // svg-local px → wrap-local px
    const pxScale = rect.width / W;
    let left = (px * pxScale) + (rect.left - wrapRect.left) + 8;
    const tipW = tooltipEl.offsetWidth || 140;
    if (left + tipW > wrapRect.width - 4) left = (px * pxScale) + (rect.left - wrapRect.left) - tipW - 8;
    if (left < 4) left = 4;
    let top = (py * (rect.height / H)) + (rect.top - wrapRect.top) - 12;
    if (top < 4) top = 4;
    tooltipEl.style.left = `${left}px`;
    tooltipEl.style.top = `${top}px`;
    tooltipEl.classList.add('visible');
    tooltipEl.setAttribute('aria-hidden', 'false');
  };

  overlay.addEventListener('mousemove', (e) => showHoverAt(e.clientX, e.clientY));
  overlay.addEventListener('mouseleave', hideHover);
  overlay.addEventListener('touchstart', (e) => {
    const t = e.touches && e.touches[0];
    if (t) showHoverAt(t.clientX, t.clientY);
  }, { passive: true });
  overlay.addEventListener('touchmove', (e) => {
    const t = e.touches && e.touches[0];
    if (t) showHoverAt(t.clientX, t.clientY);
  }, { passive: true });
  overlay.addEventListener('touchend', hideHover);

  chartEl.appendChild(svg);

  // Sparse-history note
  if (series.length > 0 && series.length < 30) {
    setChartNote(panel,
      `Only ${series.length} day${series.length === 1 ? '' : 's'} of history; full chart available after 30+ days.`,
    );
  }
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
  renderInsider(panel, { insider });
  renderHoldingsSkeleton(panel);
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
