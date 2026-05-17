// lthcs-tab.js
// Week 8 data layer for the standalone LTHCS tab page.
// Vanilla ES2020 module — no deps. Degrades gracefully if any DOM hook is missing.

'use strict';

// --- Week 9 (detail modal) hookup ---
import { openDetail } from './lthcs-detail.js';
// --- end Week 9 hookup ---

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const SNAPSHOTS_BASE = '../data/lthcs/snapshots';
const UNIVERSE_URL = '../data/lthcs/universe.json';
const INDEX_URL = `${SNAPSHOTS_BASE}/index.json`;

const STORAGE_KEYS = {
  starred: 'lthcs.starred',
  lastFilter: 'lthcs.lastFilter',
  lastSnapshotDate: 'lthcs.lastSnapshotDate',
};

// Map the snapshot's `band` strings to the UI's chip/stat short-names.
// The HTML uses `high` as the chip/stat key; the snapshot emits `high_confidence`.
const BAND_SNAPSHOT_TO_UI = {
  elite: 'elite',
  high_confidence: 'high',
  constructive: 'constructive',
  monitor: 'monitor',
  weakening: 'weakening',
  review: 'review',
};

const UI_BANDS = ['elite', 'high', 'constructive', 'monitor', 'weakening', 'review'];

const PILLAR_DISPLAY = {
  adoption_momentum: 'Adoption Momentum',
  institutional_confidence: 'Institutional Confidence',
  financial_evolution: 'Financial Evolution',
  thesis_integrity: 'Thesis Integrity',
  des: 'Demand Environment',
};

const SPARKLINE_IMPROVING = '▁▂▃▄▅▆▇█';
const SPARKLINE_STABLE = '▄▄▅▄▅▄▄▄';
const SPARKLINE_DECLINING = '█▇▆▅▄▃▂▁';

const FILTER_GROUPS = ['exchange', 'band', 'drift'];

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

const state = {
  snapshot: null,        // raw snapshot JSON
  universe: null,        // raw universe JSON
  universeByTicker: {},  // ticker -> universe entry
  enriched: [],          // merged score+universe rows
  activeFilters: {
    exchange: 'all',
    band: 'all',
    drift: 'all',
  },
  searchQuery: '',
  starred: [],           // Week 9 stub — persisted but not surfaced yet
};

// ---------------------------------------------------------------------------
// DOM helpers
// ---------------------------------------------------------------------------

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

function show(el) { if (el) el.classList.remove('hidden'); }
function hide(el) { if (el) el.classList.add('hidden'); }

// ---------------------------------------------------------------------------
// Pure helpers
// ---------------------------------------------------------------------------

function escapeHtml(s) {
  if (s == null) return '';
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function formatDate(isoDate) {
  if (!isoDate) return '—';
  // Parse "YYYY-MM-DD" without timezone drift.
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(isoDate);
  if (!m) return isoDate;
  const d = new Date(Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3])));
  try {
    return d.toLocaleDateString(undefined, {
      year: 'numeric', month: 'short', day: 'numeric', timeZone: 'UTC',
    });
  } catch {
    return isoDate;
  }
}

function pillarDisplayName(key) {
  return PILLAR_DISPLAY[key] || key;
}

function classifyDrift(drift30d) {
  const v = Number(drift30d) || 0;
  if (v > 1.0) return 'improving';
  if (v < -1.0) return 'declining';
  return 'stable';
}

function driftArrow(direction) {
  return direction === 'improving' ? '↑'
       : direction === 'declining' ? '↓'
       : '→';
}

function sparklineFor(direction) {
  if (direction === 'improving') return SPARKLINE_IMPROVING;
  if (direction === 'declining') return SPARKLINE_DECLINING;
  return SPARKLINE_STABLE;
}

function topDriver(subscores) {
  if (!subscores) return null;
  let bestKey = null;
  let bestVal = -Infinity;
  for (const [k, v] of Object.entries(subscores)) {
    const n = Number(v);
    if (Number.isFinite(n) && n > bestVal) {
      bestVal = n;
      bestKey = k;
    }
  }
  if (bestKey == null) return null;
  return { key: bestKey, value: bestVal };
}

function uiBandFor(snapshotBand) {
  return BAND_SNAPSHOT_TO_UI[snapshotBand] || snapshotBand || 'review';
}

function formatScore(n) {
  const v = Number(n);
  return Number.isFinite(v) ? v.toFixed(1) : '—';
}

function formatDrift(n) {
  const v = Number(n);
  if (!Number.isFinite(v)) return '0.0';
  const sign = v > 0 ? '+' : '';
  return `${sign}${v.toFixed(1)}`;
}

// ---------------------------------------------------------------------------
// Data fetching
// ---------------------------------------------------------------------------

async function fetchJSON(url) {
  const res = await fetch(url, { cache: 'no-store' });
  if (!res.ok) {
    throw new Error(`Fetch failed: ${url} → ${res.status} ${res.statusText}`);
  }
  return res.json();
}

async function fetchSnapshot() {
  const index = await fetchJSON(INDEX_URL);
  const latest = index && index.latest;
  if (!latest) {
    throw new Error('Snapshot index has no `latest` date.');
  }
  const snapshot = await fetchJSON(`${SNAPSHOTS_BASE}/${latest}.json`);
  return { index, snapshot };
}

async function fetchUniverse() {
  try {
    return await fetchJSON(UNIVERSE_URL);
  } catch (err) {
    // Universe is enrichment only — proceed without it on failure.
    console.warn('LTHCS: universe fetch failed; proceeding without enrichment.', err);
    return { tickers: [] };
  }
}

// ---------------------------------------------------------------------------
// Enrichment
// ---------------------------------------------------------------------------

function buildUniverseIndex(universe) {
  const idx = {};
  const list = (universe && universe.tickers) || [];
  for (const row of list) {
    if (row && row.ticker) idx[row.ticker] = row;
  }
  return idx;
}

function enrichScores(snapshot, universeByTicker) {
  const scores = (snapshot && snapshot.scores) || [];
  return scores.map((row) => {
    const uni = universeByTicker[row.ticker] || {};
    const direction = classifyDrift(row.drift_30d);
    const uiBand = uiBandFor(row.band);
    const driver = topDriver(row.subscores);
    return {
      ticker: row.ticker,
      name: uni.name || row.ticker,
      exchange: uni.exchange || '',
      sector: uni.sector || row.sector || '',
      score: Number(row.lthcs_score),
      snapshotBand: row.band,
      uiBand,
      drift30d: Number(row.drift_30d) || 0,
      driftDirection: direction,
      subscores: row.subscores || {},
      topDriverKey: driver ? driver.key : null,
      topDriverValue: driver ? driver.value : null,
      confidenceLevel: row.confidence_level || null,
      dataQualityFlags: row.data_quality_flags || [],
    };
  });
}

// ---------------------------------------------------------------------------
// Filtering
// ---------------------------------------------------------------------------

function applyFilters() {
  const q = state.searchQuery.trim().toLowerCase();
  const { exchange, band, drift } = state.activeFilters;
  return state.enriched.filter((row) => {
    if (exchange !== 'all' && row.exchange !== exchange) return false;
    if (band !== 'all' && row.uiBand !== band) return false;
    if (drift !== 'all' && row.driftDirection !== drift) return false;
    if (q) {
      const t = (row.ticker || '').toLowerCase();
      const n = (row.name || '').toLowerCase();
      if (!t.includes(q) && !n.includes(q)) return false;
    }
    return true;
  });
}

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------

function cardHTML(row) {
  const ticker = escapeHtml(row.ticker);
  const name = escapeHtml(row.name);
  const exchange = escapeHtml(row.exchange);
  const score = formatScore(row.score);
  const band = escapeHtml(row.uiBand);
  const bandLabel = escapeHtml((row.snapshotBand || row.uiBand || '').replace(/_/g, ' ').toUpperCase());
  const direction = row.driftDirection;
  const sparkline = sparklineFor(direction);
  const arrow = driftArrow(direction);
  const driftText = `${arrow} ${formatDrift(row.drift30d)}`;
  const driverLine = row.topDriverKey
    ? `Top: ${escapeHtml(pillarDisplayName(row.topDriverKey))} ${formatScore(row.topDriverValue)}`
    : 'Top: —';

  return (
    `<div class="lthcs-card" data-ticker="${ticker}" data-band="${band}" data-exchange="${exchange}" data-drift-direction="${direction}">` +
      `<div class="lthcs-card-header">` +
        `<span class="lthcs-card-ticker">${ticker}</span>` +
        `<span class="lthcs-card-score">${score}</span>` +
      `</div>` +
      `<div class="lthcs-card-name">${name}</div>` +
      `<div class="lthcs-card-meta">` +
        `<span class="lthcs-card-sparkline">${sparkline}</span>` +
        `<span class="lthcs-card-drift" data-drift-direction="${direction}">${driftText}</span>` +
      `</div>` +
      `<div class="lthcs-card-band">${bandLabel}</div>` +
      `<div class="lthcs-card-driver">${driverLine}</div>` +
    `</div>`
  );
}

function renderCards(filtered) {
  const grid = $('#lthcs-cards');
  if (!grid) return;
  if (!filtered.length) {
    grid.innerHTML = '';
    hide(grid);
    show($('#lthcs-empty'));
    return;
  }
  hide($('#lthcs-empty'));
  show(grid);
  grid.innerHTML = filtered.map(cardHTML).join('');
}

function updateStats(filtered) {
  const total = filtered.length;
  const totalEl = $('#lthcs-stat-total');
  if (totalEl) totalEl.textContent = String(total);

  const counts = Object.fromEntries(UI_BANDS.map((b) => [b, 0]));
  for (const row of filtered) {
    if (counts[row.uiBand] != null) counts[row.uiBand] += 1;
  }
  for (const band of UI_BANDS) {
    const el = document.querySelector(`[data-band-count="${band}"]`);
    if (el) el.textContent = String(counts[band]);
  }
}

function renderAll() {
  const filtered = applyFilters();
  renderCards(filtered);
  updateStats(filtered);
}

function renderMeta(snapshot) {
  const lastEl = $('#lthcs-last-updated');
  if (lastEl) lastEl.textContent = formatDate(snapshot && snapshot.calc_date);
  const versionEl = $('#lthcs-model-version');
  if (versionEl && snapshot && snapshot.model_version) {
    versionEl.textContent = snapshot.model_version;
  }
}

// ---------------------------------------------------------------------------
// Filter chip UI
// ---------------------------------------------------------------------------

function setChipActive(group, value) {
  const chips = $$(`.lthcs-chip[data-filter-group="${group}"]`);
  for (const chip of chips) {
    if (chip.dataset.filterValue === value) {
      chip.classList.add('is-active');
    } else {
      chip.classList.remove('is-active');
    }
  }
}

function syncAllChips() {
  for (const group of FILTER_GROUPS) {
    setChipActive(group, state.activeFilters[group] || 'all');
  }
}

// ---------------------------------------------------------------------------
// LocalStorage persistence
// ---------------------------------------------------------------------------

function safeGet(key) {
  try { return window.localStorage.getItem(key); } catch { return null; }
}

function safeSet(key, value) {
  try { window.localStorage.setItem(key, value); } catch { /* quota / private mode */ }
}

function restoreFilterState() {
  const raw = safeGet(STORAGE_KEYS.lastFilter);
  if (raw) {
    try {
      const parsed = JSON.parse(raw);
      if (parsed && typeof parsed === 'object') {
        for (const group of FILTER_GROUPS) {
          if (typeof parsed[group] === 'string') {
            state.activeFilters[group] = parsed[group];
          }
        }
        if (typeof parsed.searchQuery === 'string') {
          state.searchQuery = parsed.searchQuery;
        }
      }
    } catch (err) {
      console.warn('LTHCS: failed to parse persisted filter state.', err);
    }
  }
  // Week 9 stub: starred tickers are persisted but not yet surfaced in UI.
  const rawStarred = safeGet(STORAGE_KEYS.starred);
  if (rawStarred) {
    try {
      const arr = JSON.parse(rawStarred);
      if (Array.isArray(arr)) state.starred = arr.filter((t) => typeof t === 'string');
    } catch { /* ignore */ }
  }
}

function persistFilterState() {
  const payload = {
    exchange: state.activeFilters.exchange,
    band: state.activeFilters.band,
    drift: state.activeFilters.drift,
    searchQuery: state.searchQuery,
  };
  safeSet(STORAGE_KEYS.lastFilter, JSON.stringify(payload));
}

function persistSnapshotDate(date) {
  if (date) safeSet(STORAGE_KEYS.lastSnapshotDate, date);
}

// Week 9 stub — kept here so the storage key is reserved.
// eslint-disable-next-line no-unused-vars
function persistStarred() {
  safeSet(STORAGE_KEYS.starred, JSON.stringify(state.starred));
}

// ---------------------------------------------------------------------------
// Event wiring
// ---------------------------------------------------------------------------

function wireSearch() {
  const input = $('#lthcs-search');
  if (!input) return;
  input.value = state.searchQuery || '';
  input.addEventListener('input', (e) => {
    state.searchQuery = e.target.value || '';
    persistFilterState();
    renderAll();
  });
}

function wireChips() {
  const chips = $$('.lthcs-chip[data-filter-group]');
  for (const chip of chips) {
    chip.addEventListener('click', () => {
      const group = chip.dataset.filterGroup;
      const value = chip.dataset.filterValue;
      if (!group || !value) return;
      if (!FILTER_GROUPS.includes(group)) return;
      state.activeFilters[group] = value;
      setChipActive(group, value);
      persistFilterState();
      renderAll();
    });
  }
}

function wireRefresh() {
  const btn = $('#lthcs-refresh');
  if (!btn) return;
  btn.addEventListener('click', () => {
    refresh().catch((err) => showError(err));
  });
}

function wireEvents() {
  wireSearch();
  wireChips();
  wireRefresh();
  wireCardClicks();
}

// --- Week 9 (detail modal) hookup ---
// Delegated click handler on the card grid. When a card is clicked, look up
// the underlying snapshot row + universe entry from in-memory state and open
// the detail modal. Narratives aren't loaded into state yet (V1) — pass null.
function wireCardClicks() {
  const container = $('#lthcs-cards');
  if (!container) return;
  container.addEventListener('click', (e) => {
    const cardEl = e.target.closest('.lthcs-card');
    if (!cardEl || !container.contains(cardEl)) return;
    const ticker = cardEl.dataset.ticker;
    if (!ticker) return;
    const scores = (state.snapshot && state.snapshot.scores) || [];
    const snapshotRow = scores.find((r) => r && r.ticker === ticker) || null;
    if (!snapshotRow) return;
    const universeEntry = state.universeByTicker[ticker] || null;
    const narrative = (state.narrativesByTicker && state.narrativesByTicker[ticker]) || null;
    const calcDate = (state.snapshot && state.snapshot.calc_date) || null;
    openDetail({ ticker, snapshotRow, universeEntry, narrative, calcDate });
  });
}
// --- end Week 9 hookup ---

// ---------------------------------------------------------------------------
// Top-level flow
// ---------------------------------------------------------------------------

function showError(err) {
  console.error('LTHCS:', err);
  const el = $('#lthcs-error');
  if (el) {
    el.textContent = 'Could not load snapshot. Try refresh, or check that data/lthcs/snapshots/ exists.';
    show(el);
  }
  hide($('#lthcs-loading'));
}

function clearError() {
  hide($('#lthcs-error'));
}

async function refresh() {
  clearError();
  const btn = $('#lthcs-refresh');
  if (btn) btn.disabled = true;
  try {
    const [{ snapshot }, universe] = await Promise.all([fetchSnapshot(), fetchUniverse()]);
    state.snapshot = snapshot;
    state.universe = universe;
    state.universeByTicker = buildUniverseIndex(universe);
    state.enriched = enrichScores(snapshot, state.universeByTicker);
    renderMeta(snapshot);
    persistSnapshotDate(snapshot && snapshot.calc_date);
    hide($('#lthcs-loading'));
    renderAll();
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function init() {
  restoreFilterState();
  syncAllChips();
  wireEvents();
  try {
    await refresh();
  } catch (err) {
    showError(err);
  }
}

document.addEventListener('DOMContentLoaded', () => {
  init().catch((err) => {
    console.error('LTHCS init failed:', err);
    showError(err);
  });
});
