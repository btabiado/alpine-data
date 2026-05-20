// lthcs-compare.js
// Phase 4 — Side-by-side ticker comparison (2-4 tickers at once).
// Vanilla ES2020 module. Renders into #lthcs-compare-tray and
// #lthcs-compare-modal-root.
//
// UX flow:
//   1. User clicks a "+" button on a ticker card (data-compare-add="TICKER")
//      → ticker is added to the tray at the bottom of the page.
//   2. Tray shows 0-4 selected tickers as chips; the "Compare" CTA enables
//      once >=2 are selected.
//   3. Click "Compare" → wide modal opens with N columns (N = 2..4), each
//      showing condensed detail-modal content: composite + band + 90d
//      sparkline + 5 pillar bars + drift table + thesis call line.
//   4. Pillar bars that differ by >10 points across any pair are accented.
//   5. Set persists to localStorage under `lthcs.compareSet`; tray
//      auto-restores (collapsed) on next session.
//
// Reuses bandColorForScore + renderSparkline from lthcs-sparkline.

'use strict';

import { bandColorForScore, renderSparkline } from './lthcs-sparkline.js';

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const SNAPSHOTS_BASE = '../data/lthcs/snapshots';
const UNIVERSE_URL = '../data/lthcs/universe.json';
const HISTORY_BASE = '../data/lthcs/history/by_ticker';

const STORAGE_KEY = 'lthcs.compareSet';
const TRAY_COLLAPSED_KEY = 'lthcs.compareTrayCollapsed';

const MAX_COMPARE = 4;
const MIN_COMPARE = 2;

// Diff threshold for pillar accenting — see the README in the brief.
const PILLAR_DIFF_THRESHOLD = 10;

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

const PILLAR_SHORT = {
  adoption_momentum: 'Adoption',
  institutional_confidence: 'Institutional',
  financial_evolution: 'Financial',
  thesis_integrity: 'Thesis',
  des: 'Demand',
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

const state = {
  // ordered list of tickers in the compare set
  set: [],
  // tray-collapsed preference (true = chips visible but small)
  collapsed: false,
  // cached snapshot payload (latest); refreshed on demand
  snapshot: null,
  snapshotPromise: null,
  // cached universe map ticker -> entry
  universeByTicker: null,
  universePromise: null,
  // cached per-ticker history
  historyCache: new Map(),
  // modal DOM refs
  modalRoot: null,
  modalPanel: null,
  prevFocus: null,
  keyHandler: null,
  // tray DOM ref
  trayEl: null,
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
  while (node && node.firstChild) node.removeChild(node.firstChild);
}

function fmtScore(n) {
  const v = Number(n);
  return Number.isFinite(v) ? v.toFixed(1) : '—';
}

function fmtDrift(n) {
  const v = Number(n);
  if (!Number.isFinite(v)) return '0.0';
  const sign = v > 0 ? '+' : v < 0 ? '−' : '';
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

function safeGet(key) {
  try { return window.localStorage.getItem(key); } catch { return null; }
}

function safeSet(key, value) {
  try { window.localStorage.setItem(key, value); } catch { /* quota / private */ }
}

function persistSet() {
  safeSet(STORAGE_KEY, JSON.stringify(state.set));
}

function restoreSet() {
  const raw = safeGet(STORAGE_KEY);
  if (!raw) return;
  try {
    const arr = JSON.parse(raw);
    if (Array.isArray(arr)) {
      const cleaned = [];
      const seen = new Set();
      for (const t of arr) {
        if (typeof t !== 'string') continue;
        const tt = t.toUpperCase();
        if (seen.has(tt)) continue;
        seen.add(tt);
        cleaned.push(tt);
        if (cleaned.length >= MAX_COMPARE) break;
      }
      state.set = cleaned;
    }
  } catch { /* ignore */ }
  const c = safeGet(TRAY_COLLAPSED_KEY);
  state.collapsed = c === '1';
}

// ---------------------------------------------------------------------------
// Data loading
// ---------------------------------------------------------------------------

async function fetchJSON(url) {
  const res = await fetch(url, { cache: 'no-store' });
  if (!res.ok) throw new Error(`${url} ${res.status}`);
  return res.json();
}

async function loadSnapshot() {
  if (state.snapshot) return state.snapshot;
  if (state.snapshotPromise) return state.snapshotPromise;
  state.snapshotPromise = (async () => {
    const idx = await fetchJSON(`${SNAPSHOTS_BASE}/index.json`);
    const latest = (idx && Array.isArray(idx.dates) && idx.dates[0]) || null;
    if (!latest) throw new Error('no snapshot dates in index');
    const snap = await fetchJSON(`${SNAPSHOTS_BASE}/${latest}.json`);
    state.snapshot = snap;
    return snap;
  })();
  return state.snapshotPromise;
}

async function loadUniverse() {
  if (state.universeByTicker) return state.universeByTicker;
  if (state.universePromise) return state.universePromise;
  state.universePromise = (async () => {
    const u = await fetchJSON(UNIVERSE_URL);
    const map = {};
    if (u && Array.isArray(u.tickers)) {
      for (const row of u.tickers) {
        if (row && row.ticker) map[row.ticker] = row;
      }
    }
    state.universeByTicker = map;
    return map;
  })();
  return state.universePromise;
}

async function loadHistory(ticker) {
  if (state.historyCache.has(ticker)) return state.historyCache.get(ticker);
  const p = (async () => {
    try {
      const data = await fetchJSON(`${HISTORY_BASE}/${encodeURIComponent(ticker)}.json`);
      return data;
    } catch (_e) {
      return null;
    }
  })();
  state.historyCache.set(ticker, p);
  return p;
}

// Convert per-ticker history payload to the array shape renderSparkline wants.
// Accepts either {history:[{date, composite_score|score}]} or a top-level array.
function historyForSparkline(payload) {
  if (!payload) return [];
  const series = Array.isArray(payload) ? payload : (payload.history || payload.scores || []);
  const rows = [];
  for (const raw of series) {
    if (!raw) continue;
    const d = String(raw.date || '');
    const sRaw = (raw.composite_score != null) ? raw.composite_score : raw.score;
    const s = Number(sRaw);
    if (!d || !Number.isFinite(s)) continue;
    rows.push({ date: d, score: s, band: raw.band || null });
  }
  rows.sort((a, b) => (a.date < b.date ? -1 : a.date > b.date ? 1 : 0));
  // 90d window (or whatever's available)
  return rows.slice(-90);
}

// ---------------------------------------------------------------------------
// Compare set management
// ---------------------------------------------------------------------------

function isInSet(ticker) {
  return state.set.includes(ticker);
}

// Add ticker to set. If already in set, flash the existing chip red.
// If set is full, flash the tray's full-state warning.
function addTicker(ticker) {
  if (!ticker) return;
  const t = ticker.toUpperCase();
  if (isInSet(t)) {
    // duplicate — flash that chip red
    flashTrayChip(t);
    return;
  }
  if (state.set.length >= MAX_COMPARE) {
    flashTrayFull();
    return;
  }
  state.set.push(t);
  persistSet();
  renderTray();
}

function removeTicker(ticker) {
  const t = ticker && ticker.toUpperCase();
  const idx = state.set.indexOf(t);
  if (idx === -1) return;
  state.set.splice(idx, 1);
  persistSet();
  renderTray();
}

function clearSet() {
  state.set = [];
  persistSet();
  renderTray();
}

function swapAdjacent(ticker, dir) {
  // dir: -1 left, +1 right
  const i = state.set.indexOf(ticker);
  if (i === -1) return;
  const j = i + dir;
  if (j < 0 || j >= state.set.length) return;
  const tmp = state.set[i];
  state.set[i] = state.set[j];
  state.set[j] = tmp;
  persistSet();
  // If the modal is open, re-render columns in place.
  if (state.modalRoot && !state.modalRoot.classList.contains('hidden')) {
    renderCompareModal();
  } else {
    renderTray();
  }
}

// ---------------------------------------------------------------------------
// Tray
// ---------------------------------------------------------------------------

function ensureTray() {
  if (state.trayEl && document.body.contains(state.trayEl)) return state.trayEl;
  let tray = document.getElementById('lthcs-compare-tray');
  if (!tray) {
    tray = el('div', {
      id: 'lthcs-compare-tray',
      className: 'lthcs-compare-tray hidden',
      attrs: { role: 'region', 'aria-label': 'Compare tray', 'aria-live': 'polite' },
    });
    document.body.appendChild(tray);
  }
  state.trayEl = tray;
  return tray;
}

function flashTrayChip(ticker) {
  const tray = ensureTray();
  const chip = tray.querySelector(`[data-tray-chip="${ticker}"]`);
  if (!chip) return;
  chip.classList.remove('lthcs-compare-chip-flash');
  // force reflow to restart animation
  void chip.offsetWidth;
  chip.classList.add('lthcs-compare-chip-flash');
}

function flashTrayFull() {
  const tray = ensureTray();
  tray.classList.remove('lthcs-compare-tray-flash');
  void tray.offsetWidth;
  tray.classList.add('lthcs-compare-tray-flash');
  setTimeout(() => tray.classList.remove('lthcs-compare-tray-flash'), 600);
}

function renderTray() {
  const tray = ensureTray();
  clear(tray);

  if (state.set.length === 0) {
    tray.classList.add('hidden');
    return;
  }
  tray.classList.remove('hidden');
  tray.classList.toggle('is-collapsed', state.collapsed);

  // Inner layout
  const inner = el('div', { className: 'lthcs-compare-tray-inner' });

  const label = el('span', {
    className: 'lthcs-compare-tray-label',
    text: 'Compare',
  });
  inner.appendChild(label);

  const chips = el('div', { className: 'lthcs-compare-tray-chips' });
  for (const ticker of state.set) {
    const chip = el('span', {
      className: 'lthcs-compare-chip',
      attrs: { 'data-tray-chip': ticker },
    });
    chip.appendChild(el('span', { className: 'lthcs-compare-chip-ticker', text: ticker }));
    const remove = el('button', {
      className: 'lthcs-compare-chip-remove',
      text: '×',
      attrs: { type: 'button', 'aria-label': `Remove ${ticker} from compare` },
    });
    remove.addEventListener('click', (e) => {
      e.stopPropagation();
      removeTicker(ticker);
    });
    chip.appendChild(remove);
    chips.appendChild(chip);
  }
  inner.appendChild(chips);

  const actions = el('div', { className: 'lthcs-compare-tray-actions' });

  const compareBtn = el('button', {
    className: 'lthcs-compare-tray-btn lthcs-compare-tray-btn-primary',
    text: state.set.length >= MIN_COMPARE
      ? `Compare (${state.set.length})`
      : `Compare (need ${MIN_COMPARE - state.set.length} more)`,
    attrs: {
      type: 'button',
      'aria-label': 'Open side-by-side compare modal',
      disabled: state.set.length < MIN_COMPARE ? '' : null,
    },
  });
  if (state.set.length < MIN_COMPARE) compareBtn.disabled = true;
  compareBtn.addEventListener('click', () => openCompareModal());
  actions.appendChild(compareBtn);

  const clearBtn = el('button', {
    className: 'lthcs-compare-tray-btn',
    text: 'Clear',
    attrs: { type: 'button', 'aria-label': 'Clear compare set' },
  });
  clearBtn.addEventListener('click', () => clearSet());
  actions.appendChild(clearBtn);

  const collapseBtn = el('button', {
    className: 'lthcs-compare-tray-btn lthcs-compare-tray-btn-collapse',
    text: state.collapsed ? '▲' : '▼',
    attrs: {
      type: 'button',
      'aria-label': state.collapsed ? 'Expand compare tray' : 'Collapse compare tray',
      title: state.collapsed ? 'Expand' : 'Collapse',
    },
  });
  collapseBtn.addEventListener('click', () => {
    state.collapsed = !state.collapsed;
    safeSet(TRAY_COLLAPSED_KEY, state.collapsed ? '1' : '0');
    renderTray();
  });
  actions.appendChild(collapseBtn);

  inner.appendChild(actions);
  tray.appendChild(inner);
}

// ---------------------------------------------------------------------------
// Compare modal — main view
// ---------------------------------------------------------------------------

function ensureModalRoot() {
  if (state.modalRoot && document.body.contains(state.modalRoot)) return state.modalRoot;
  let root = document.getElementById('lthcs-compare-modal-root');
  if (!root) {
    root = el('div', {
      id: 'lthcs-compare-modal-root',
      className: 'lthcs-compare-modal-root hidden',
      attrs: { role: 'dialog', 'aria-modal': 'true', 'aria-hidden': 'true' },
    });
    document.body.appendChild(root);
  }
  state.modalRoot = root;
  return root;
}

function openCompareModal() {
  if (state.set.length < MIN_COMPARE) return;
  if (!state.modalRoot || state.modalRoot.classList.contains('hidden')) {
    state.prevFocus = document.activeElement;
  }
  buildCompareShell();
  renderCompareModal();

  const root = state.modalRoot;
  root.classList.remove('hidden');
  root.setAttribute('aria-hidden', 'false');
  document.body.style.overflow = 'hidden';

  if (state.keyHandler) {
    document.removeEventListener('keydown', state.keyHandler, true);
  }
  state.keyHandler = trapKeydown;
  document.addEventListener('keydown', state.keyHandler, true);

  // Focus close button as a sensible landing target
  const closeBtn = state.modalPanel && state.modalPanel.querySelector('.lthcs-compare-modal-close');
  if (closeBtn) {
    try { closeBtn.focus(); } catch { /* ignore */ }
  }
}

function closeCompareModal() {
  const root = state.modalRoot;
  if (!root) return;
  if (root.classList.contains('hidden')) return;
  root.classList.add('hidden');
  root.setAttribute('aria-hidden', 'true');
  document.body.style.overflow = '';
  if (state.keyHandler) {
    document.removeEventListener('keydown', state.keyHandler, true);
    state.keyHandler = null;
  }
  const prior = state.prevFocus;
  state.prevFocus = null;
  if (prior && typeof prior.focus === 'function' && document.contains(prior)) {
    try { prior.focus(); } catch { /* ignore */ }
  }
}

function buildCompareShell() {
  const root = ensureModalRoot();
  clear(root);

  const backdrop = el('div', { className: 'lthcs-compare-modal-backdrop' });
  backdrop.addEventListener('click', () => closeCompareModal());
  root.appendChild(backdrop);

  const panel = el('div', {
    className: 'lthcs-compare-modal-panel',
    attrs: { tabindex: '-1' },
  });

  // Header
  const header = el('div', { className: 'lthcs-compare-modal-header' });
  const titleBlock = el('div', { className: 'lthcs-compare-modal-title-block' });
  const title = el('h2', {
    className: 'lthcs-compare-modal-title',
    id: 'lthcs-compare-modal-title',
    text: 'Side-by-side compare',
  });
  titleBlock.appendChild(title);
  const sub = el('p', {
    className: 'lthcs-compare-modal-sub',
    text: 'Differences > 10pts on any pillar are accented.',
  });
  titleBlock.appendChild(sub);
  header.appendChild(titleBlock);

  const closeBtn = el('button', {
    className: 'lthcs-compare-modal-close',
    text: '×',
    attrs: { type: 'button', 'aria-label': 'Close compare' },
  });
  closeBtn.addEventListener('click', () => closeCompareModal());
  header.appendChild(closeBtn);
  panel.appendChild(header);

  // Body — column container (rendered async by renderCompareModal)
  const body = el('div', {
    className: 'lthcs-compare-modal-body',
    attrs: { 'data-slot': 'compare-cols' },
  });
  panel.appendChild(body);

  panel.setAttribute('aria-labelledby', 'lthcs-compare-modal-title');
  root.appendChild(panel);
  state.modalPanel = panel;
}

// Compute which (pillar, ticker) pairs should be accented as "differs > N pts
// from any other selected ticker on that pillar". Returns:
//   accents.get(pillarKey) === Set of ticker symbols whose value participates
//   in a pair with delta > threshold.
function computePillarAccents(snapshotRowsByTicker) {
  const accents = new Map();
  for (const pk of PILLAR_ORDER) accents.set(pk, new Set());
  const tickers = Object.keys(snapshotRowsByTicker);
  for (const pk of PILLAR_ORDER) {
    for (let i = 0; i < tickers.length; i++) {
      for (let j = i + 1; j < tickers.length; j++) {
        const a = snapshotRowsByTicker[tickers[i]];
        const b = snapshotRowsByTicker[tickers[j]];
        const va = Number(a && a.subscores && a.subscores[pk]);
        const vb = Number(b && b.subscores && b.subscores[pk]);
        if (!Number.isFinite(va) || !Number.isFinite(vb)) continue;
        if (Math.abs(va - vb) > PILLAR_DIFF_THRESHOLD) {
          accents.get(pk).add(tickers[i]);
          accents.get(pk).add(tickers[j]);
        }
      }
    }
  }
  return accents;
}

async function renderCompareModal() {
  const panel = state.modalPanel;
  if (!panel) return;
  const body = panel.querySelector('[data-slot="compare-cols"]');
  if (!body) return;
  clear(body);

  // Reflect cap on mobile: visually a 2-col cap is handled via CSS, but we
  // still render the columns the user picked; CSS wraps to a 2-up grid.
  const n = state.set.length;
  body.setAttribute('data-cols', String(Math.min(n, MAX_COMPARE)));

  // Loading skeleton per column
  for (const ticker of state.set) {
    const col = el('div', {
      className: 'lthcs-compare-col',
      attrs: { 'data-col-ticker': ticker },
    });
    col.appendChild(el('div', { className: 'lthcs-compare-col-loading', text: `Loading ${ticker}…` }));
    body.appendChild(col);
  }

  let snapshot, universe;
  try {
    [snapshot, universe] = await Promise.all([loadSnapshot(), loadUniverse()]);
  } catch (err) {
    console.warn('LTHCS compare: data load failed', err);
    clear(body);
    body.appendChild(el('div', {
      className: 'lthcs-compare-error',
      text: 'Could not load comparison data. Refresh the page and try again.',
    }));
    return;
  }

  const scoresByTicker = {};
  const scores = (snapshot && snapshot.scores) || [];
  for (const row of scores) {
    if (row && row.ticker) scoresByTicker[row.ticker] = row;
  }

  // Filter to known tickers for accent computation
  const presentRows = {};
  for (const t of state.set) {
    if (scoresByTicker[t]) presentRows[t] = scoresByTicker[t];
  }
  const accents = computePillarAccents(presentRows);

  clear(body);
  body.setAttribute('data-cols', String(Math.min(state.set.length, MAX_COMPARE)));

  state.set.forEach((ticker, idx) => {
    const row = scoresByTicker[ticker] || null;
    const uniEntry = (universe && universe[ticker]) || null;
    const col = buildColumn({
      ticker,
      idx,
      total: state.set.length,
      row,
      universeEntry: uniEntry,
      accents,
    });
    body.appendChild(col);
  });

  // Async per-column: load history for the sparkline.
  state.set.forEach((ticker) => {
    loadHistory(ticker).then((payload) => {
      if (!state.modalRoot || state.modalRoot.classList.contains('hidden')) return;
      const col = body.querySelector(`[data-col-ticker="${ticker}"]`);
      if (!col) return;
      const slot = col.querySelector('[data-slot="sparkline"]');
      if (!slot) return;
      const series = historyForSparkline(payload);
      clear(slot);
      if (series.length === 0) {
        slot.appendChild(el('div', { className: 'lthcs-compare-spark-empty', text: 'No history yet.' }));
        return;
      }
      const latest = series[series.length - 1];
      const stroke = bandColorForScore(latest.score) || 'currentColor';
      const svg = renderSparkline(series, {
        width: 240,
        height: 60,
        showBands: false,
        showAxes: false,
        showLastDot: true,
        strokeColor: stroke,
      });
      svg.setAttribute('aria-label', `${ticker} 90-day score history sparkline`);
      slot.appendChild(svg);
    });
  });
}

// Build one compare column for a ticker. Sections:
//   - Header: ticker, company name (truncated), reorder controls, remove
//   - Score block: composite + band badge
//   - Sparkline (loaded async)
//   - Pillar bars (5 rows) with accent class on >10pt diffs
//   - Drift table (1d/7d/30d/90d)
//   - Thesis call line (top driver / bottom driver summary)
//   - Data-quality flags (if any) — column-local, not global
function buildColumn({ ticker, idx, total, row, universeEntry, accents }) {
  const col = el('div', {
    className: 'lthcs-compare-col',
    attrs: { 'data-col-ticker': ticker },
  });

  // Header
  const head = el('div', { className: 'lthcs-compare-col-head' });
  const nameWrap = el('div', { className: 'lthcs-compare-col-name-wrap' });
  nameWrap.appendChild(el('div', { className: 'lthcs-compare-col-ticker', text: ticker }));
  const company = (universeEntry && universeEntry.name) || '';
  if (company) {
    nameWrap.appendChild(el('div', { className: 'lthcs-compare-col-company', text: company, attrs: { title: company } }));
  }
  head.appendChild(nameWrap);

  // Reorder / remove controls
  const ctrls = el('div', { className: 'lthcs-compare-col-ctrls' });
  const leftBtn = el('button', {
    className: 'lthcs-compare-col-ctrl',
    text: '←',
    attrs: { type: 'button', 'aria-label': `Move ${ticker} left`, title: 'Move left' },
  });
  leftBtn.disabled = idx === 0;
  leftBtn.addEventListener('click', () => swapAdjacent(ticker, -1));
  ctrls.appendChild(leftBtn);

  const rightBtn = el('button', {
    className: 'lthcs-compare-col-ctrl',
    text: '→',
    attrs: { type: 'button', 'aria-label': `Move ${ticker} right`, title: 'Move right' },
  });
  rightBtn.disabled = idx === total - 1;
  rightBtn.addEventListener('click', () => swapAdjacent(ticker, +1));
  ctrls.appendChild(rightBtn);

  const removeBtn = el('button', {
    className: 'lthcs-compare-col-ctrl lthcs-compare-col-ctrl-remove',
    text: '×',
    attrs: { type: 'button', 'aria-label': `Remove ${ticker}`, title: 'Remove from compare' },
  });
  removeBtn.addEventListener('click', () => {
    removeTicker(ticker);
    // If fewer than MIN_COMPARE remain, close the modal — comparing 1 ticker
    // is meaningless and the user has the regular detail modal for that.
    if (state.set.length < MIN_COMPARE) {
      closeCompareModal();
    } else {
      renderCompareModal();
    }
  });
  ctrls.appendChild(removeBtn);
  head.appendChild(ctrls);

  col.appendChild(head);

  if (!row) {
    col.appendChild(el('div', {
      className: 'lthcs-compare-col-missing',
      text: `No snapshot data for ${ticker}.`,
    }));
    return col;
  }

  // Score + band hero
  const hero = el('div', { className: 'lthcs-compare-col-hero' });
  const scoreEl = el('div', {
    className: 'lthcs-compare-col-score',
    text: fmtScore(row.lthcs_score),
  });
  const scoreColor = bandColorForScore(Number(row.lthcs_score));
  if (scoreColor) scoreEl.style.color = scoreColor;
  hero.appendChild(scoreEl);

  const band = (row.band || 'review').toLowerCase();
  const bandLabel = humanCase(band);
  hero.appendChild(el('span', {
    className: 'lthcs-compare-col-band',
    text: bandLabel,
    attrs: { 'data-band': band, 'aria-label': `Band: ${bandLabel}` },
  }));
  col.appendChild(hero);

  // Sparkline slot (async)
  const sparkSlot = el('div', {
    className: 'lthcs-compare-col-spark',
    attrs: { 'data-slot': 'sparkline' },
  });
  sparkSlot.appendChild(el('div', { className: 'lthcs-compare-spark-loading', text: 'Loading 90d…' }));
  col.appendChild(sparkSlot);

  // Pillar bars
  const pillarsWrap = el('div', { className: 'lthcs-compare-col-pillars' });
  PILLAR_ORDER.forEach((pk) => {
    const sub = Number((row.subscores && row.subscores[pk]));
    const isAccent = accents.get(pk) && accents.get(pk).has(ticker);
    const rowEl = el('div', {
      className: 'lthcs-compare-pillar-row' + (isAccent ? ' is-accent' : ''),
    });
    rowEl.appendChild(el('span', {
      className: 'lthcs-compare-pillar-label',
      text: PILLAR_SHORT[pk] || pk,
      attrs: { title: PILLAR_DISPLAY[pk] || pk },
    }));
    const track = el('div', { className: 'lthcs-compare-pillar-track' });
    const fill = el('div', { className: 'lthcs-compare-pillar-fill' });
    const pct = Number.isFinite(sub) ? Math.max(0, Math.min(100, sub)) : 0;
    fill.style.width = `${pct.toFixed(1)}%`;
    const fillColor = Number.isFinite(sub) ? bandColorForScore(sub) : null;
    if (fillColor) fill.style.background = fillColor;
    track.appendChild(fill);
    rowEl.appendChild(track);
    rowEl.appendChild(el('span', {
      className: 'lthcs-compare-pillar-val',
      text: fmtScore(sub),
    }));
    pillarsWrap.appendChild(rowEl);
  });
  col.appendChild(pillarsWrap);

  // Drift table
  const driftWrap = el('div', { className: 'lthcs-compare-col-drift' });
  const driftFields = [
    ['1d', row.drift_1d],
    ['7d', row.drift_7d],
    ['30d', row.drift_30d],
    ['90d', row.drift_90d],
  ];
  for (const [label, value] of driftFields) {
    const stat = el('div', { className: 'lthcs-compare-drift-stat' });
    stat.appendChild(el('span', { className: 'lthcs-compare-drift-label', text: label }));
    stat.appendChild(el('span', {
      className: 'lthcs-compare-drift-val',
      text: fmtDrift(value),
      attrs: { 'data-direction': driftDirection(value) },
    }));
    driftWrap.appendChild(stat);
  }
  col.appendChild(driftWrap);

  // Thesis call line
  const thesis = buildThesisLine(row);
  if (thesis) {
    col.appendChild(el('div', {
      className: 'lthcs-compare-col-thesis',
      text: thesis,
    }));
  }

  // Data quality flags — column-local
  const flags = Array.isArray(row.data_quality_flags) ? row.data_quality_flags : [];
  if (flags.length) {
    const flagWrap = el('div', { className: 'lthcs-compare-col-flags' });
    flagWrap.appendChild(el('span', { className: 'lthcs-compare-col-flags-label', text: 'Flags:' }));
    for (const f of flags) {
      flagWrap.appendChild(el('span', { className: 'lthcs-compare-col-flag-chip', text: humanCase(f) }));
    }
    col.appendChild(flagWrap);
  }

  return col;
}

// Build a short "thesis call" line: which pillar is leading, which is dragging.
function buildThesisLine(row) {
  const subs = row && row.subscores;
  if (!subs || typeof subs !== 'object') return null;
  let topKey = null;
  let topVal = -Infinity;
  let lowKey = null;
  let lowVal = Infinity;
  for (const pk of PILLAR_ORDER) {
    const v = Number(subs[pk]);
    if (!Number.isFinite(v)) continue;
    if (v > topVal) { topVal = v; topKey = pk; }
    if (v < lowVal) { lowVal = v; lowKey = pk; }
  }
  if (!topKey || !lowKey) return null;
  if (topKey === lowKey) {
    return `Leading: ${PILLAR_SHORT[topKey]} ${fmtScore(topVal)}`;
  }
  return `Leading ${PILLAR_SHORT[topKey]} ${fmtScore(topVal)} · Dragging ${PILLAR_SHORT[lowKey]} ${fmtScore(lowVal)}`;
}

// ---------------------------------------------------------------------------
// Focus trap (compare modal)
// ---------------------------------------------------------------------------

function trapKeydown(e) {
  const root = state.modalRoot;
  if (!root || root.classList.contains('hidden')) return;
  if (e.key === 'Escape') {
    e.preventDefault();
    closeCompareModal();
    return;
  }
  if (e.key !== 'Tab') return;
  const panel = state.modalPanel;
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
// Document-level event delegation
// ---------------------------------------------------------------------------

// Click anywhere with [data-compare-add="TICKER"] → add to set.
// This way each card doesn't have to wire its own listener and the compare
// module stays decoupled from lthcs-tab.js card rendering.
function wireDocClicks() {
  document.addEventListener('click', (e) => {
    const btn = e.target.closest && e.target.closest('[data-compare-add]');
    if (!btn) return;
    e.preventDefault();
    e.stopPropagation();
    const ticker = btn.getAttribute('data-compare-add');
    if (ticker) addTicker(ticker);
  }, true);
}

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

function init() {
  ensureTray();
  ensureModalRoot();
  restoreSet();
  // Restored sets render the tray collapsed by default — user re-opens by
  // clicking the chevron. This matches the brief's "page refresh mid-compare
  // → restore set from localStorage and reopen the tray (collapsed)".
  if (state.set.length > 0) {
    // Force the collapsed default for an auto-restored set; user prefs apply
    // only after explicit interaction this session.
    state.collapsed = true;
  }
  renderTray();
  wireDocClicks();
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}

// Tiny public API in case other modules want to push tickers programmatically
// (e.g. from the detail modal in the future). Kept off `window` to avoid
// global namespace pollution; importable via the module URL.
export const compare = {
  add: addTicker,
  remove: removeTicker,
  clear: clearSet,
  open: openCompareModal,
  close: closeCompareModal,
  getSet: () => state.set.slice(),
};
