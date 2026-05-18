// lthcs-tab.js
// Week 8 data layer for the standalone LTHCS tab page.
// Vanilla ES2020 module — no deps. Degrades gracefully if any DOM hook is missing.

'use strict';

// --- Week 9 (detail modal) hookup ---
import { openDetail } from './lthcs-detail.js';
// --- end Week 9 hookup ---

// --- Week 11 (market regime strip) hookup ---
import { renderRegimeStrip } from './lthcs-regime.js';
// --- end Week 11 hookup ---

// --- Tier 4 #18 (movers leaderboard strip) hookup ---
import { renderMoversStrip } from './lthcs-movers.js';
// --- end Tier 4 #18 hookup ---

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const SNAPSHOTS_BASE = '../data/lthcs/snapshots';
const UNIVERSE_URL = '../data/lthcs/universe.json';
const INDEX_URL = `${SNAPSHOTS_BASE}/index.json`;
const INSIDER_BASE = '../data/lthcs/insider';
const HISTORY_BASE = '../data/lthcs/history/by_ticker';

// Task 1: score-trend thresholds. Anything within +/-0.5 reads as
// "flat / stable"; beyond is up/down. The lookback prefers 30 days,
// but falls back through shorter windows so a freshly-deployed model
// (only a few days of history on disk) still surfaces direction.
// The actual period used is shown in the pill so the user knows
// whether they're looking at a 30d move or a 1d move.
const TREND_FLAT_THRESHOLD = 0.5;
const TREND_LOOKBACK_DAYS = 30;
// Fallback chain: try the longest period first, then progressively
// shorter, then give up. Stops as soon as ANY anchor on-or-before the
// target date exists in the history.
const TREND_FALLBACK_DAYS = [30, 14, 7, 3, 1];

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

const FILTER_GROUPS = ['index', 'band', 'drift'];

// Human-readable labels for the active-filters breadcrumb. Keys are the
// filter values stored in state.activeFilters; falls back to the raw key
// if a value isn't listed (so a future band wouldn't render as "undefined").
const FILTER_VALUE_LABELS = {
  index: {
    djia: 'DJIA 30',
    'nasdaq-100': 'NASDAQ-100',
    'sp-100': 'S&P 100',
  },
  band: {
    elite: 'Elite',
    high: 'High Confidence',
    constructive: 'Constructive',
    monitor: 'Monitor',
    weakening: 'Weakening',
    review: 'Review',
  },
  drift: {
    improving: 'Improving',
    stable: 'Stable',
    declining: 'Declining',
  },
};

const FILTER_GROUP_LABELS = {
  index: 'Index',
  band: 'Band',
  drift: 'Drift',
};

// Map universe.json `index_membership` values → short DOM keys used in
// filters and stat tiles.
const INDEX_KEY_NORMALIZE = {
  'DJIA': 'djia',
  'NASDAQ-100': 'nasdaq-100',
  'S&P 100': 'sp-100',
  'S&P 500': 'sp-500',
};
const INDEX_FILTERS = ['djia', 'nasdaq-100', 'sp-100'];

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

const state = {
  snapshot: null,        // raw snapshot JSON
  universe: null,        // raw universe JSON
  universeByTicker: {},  // ticker -> universe entry
  enriched: [],          // merged score+universe rows
  insiderByTicker: {},   // Week 11: ticker -> insider record (SEC Form 4)
  trendByTicker: {},     // Task 1: ticker -> { delta, direction } (computed from history/by_ticker)
  activeFilters: {
    index: 'all',
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
// Task 1: 30-day score-trend helpers
// ---------------------------------------------------------------------------

// Parse a "YYYY-MM-DD" string into a UTC ms timestamp. Returns NaN on bad input.
function parseISODateUTC(s) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(s || ''));
  if (!m) return NaN;
  return Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
}

// Pick the history entry whose date is closest to (currentDate - lookbackDays),
// preferring the latest entry that is on-or-before that target. Returns null
// if no entry is at least `lookbackDays` days old.
function pickAnchorForLookback(history, currentDateISO, lookbackDays) {
  if (!Array.isArray(history) || !history.length) return null;
  const currentMs = parseISODateUTC(currentDateISO);
  if (!Number.isFinite(currentMs)) return null;
  const targetMs = currentMs - lookbackDays * 24 * 60 * 60 * 1000;

  let best = null;
  let bestMs = -Infinity;
  for (const entry of history) {
    const ms = parseISODateUTC(entry && entry.date);
    if (!Number.isFinite(ms)) continue;
    // Take the most-recent date that is <= target.
    if (ms <= targetMs && ms > bestMs) {
      bestMs = ms;
      best = entry;
    }
  }
  return best;
}

// Try the longest lookback first; fall back through shorter windows
// until an anchor exists. Returns { anchor, periodDays } or null.
function pickAnchorWithFallback(history, currentDateISO) {
  for (const days of TREND_FALLBACK_DAYS) {
    const anchor = pickAnchorForLookback(history, currentDateISO, days);
    if (anchor && Number.isFinite(Number(anchor.score))) {
      return { anchor, periodDays: days };
    }
  }
  return null;
}

// Back-compat alias used elsewhere in the file. Prefers 30 days when
// possible but uses the fallback chain otherwise.
function pickThirtyDayAnchor(history, currentDateISO) {
  const result = pickAnchorWithFallback(history, currentDateISO);
  return result ? result.anchor : null;
}

// Compute the score trend for a single ticker. Returns
//   { delta, direction: 'up'|'down'|'flat', periodDays }  on success,
//   { delta: null, direction: 'unknown', periodDays: null } otherwise.
// periodDays is the ACTUAL window used (may be < 30 when history is
// still warming up). Renderer should surface this so a "+1.5" pill
// doesn't lie about being a 30d move when it's actually a 3d move.
function computeTrend(historyDoc, currentDateISO, currentScore) {
  if (!historyDoc || !Array.isArray(historyDoc.history)) {
    return { delta: null, direction: 'unknown', periodDays: null };
  }
  const result = pickAnchorWithFallback(historyDoc.history, currentDateISO);
  if (!result) {
    return { delta: null, direction: 'unknown', periodDays: null };
  }
  const cur = Number(currentScore);
  if (!Number.isFinite(cur)) {
    return { delta: null, direction: 'unknown', periodDays: null };
  }
  const delta = cur - Number(result.anchor.score);
  let direction = 'flat';
  if (delta > TREND_FLAT_THRESHOLD) direction = 'up';
  else if (delta < -TREND_FLAT_THRESHOLD) direction = 'down';
  return { delta, direction, periodDays: result.periodDays };
}

function trendArrow(direction) {
  if (direction === 'up') return '▲';
  if (direction === 'down') return '▼';
  if (direction === 'flat') return '→';
  return '?';
}

function formatTrendValue(delta, direction) {
  if (direction === 'unknown' || delta == null || !Number.isFinite(delta)) return '—';
  if (direction === 'flat') return '0.0';
  const sign = delta > 0 ? '+' : '';
  return `${sign}${delta.toFixed(1)}`;
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

// Task 1: load per-ticker history files in parallel. Returns
//   { TICKER: { delta, direction }, ... }
// Each fetch is best-effort — a missing or malformed file just yields an
// "unknown" trend for that ticker.
async function fetchTrendMap(tickers, calcDate) {
  if (!Array.isArray(tickers) || !tickers.length) return {};

  // Encode each ticker for the URL path (handles edge cases like "BRK.B").
  const fetches = tickers.map(async (row) => {
    const ticker = row && row.ticker;
    if (!ticker) return [null, { delta: null, direction: 'unknown' }];
    const url = `${HISTORY_BASE}/${encodeURIComponent(ticker)}.json`;
    try {
      const res = await fetch(url, { cache: 'no-store' });
      if (!res.ok) return [ticker, { delta: null, direction: 'unknown' }];
      const doc = await res.json();
      const trend = computeTrend(doc, calcDate, row.score);
      return [ticker, trend];
    } catch {
      return [ticker, { delta: null, direction: 'unknown' }];
    }
  });

  const results = await Promise.all(fetches);
  const map = {};
  for (const [ticker, trend] of results) {
    if (ticker) map[ticker] = trend;
  }
  return map;
}

async function fetchInsider(calcDate) {
  // Week 11: SEC Form 4 insider conviction map (universe-wide).
  // Optional enrichment — missing file is normal on older snapshots.
  if (!calcDate) return {};
  try {
    const res = await fetch(`${INSIDER_BASE}/${calcDate}.json`, { cache: 'no-store' });
    if (!res.ok) return {};
    const data = await res.json();
    return (data && typeof data === 'object') ? data : {};
  } catch (err) {
    console.warn('LTHCS: insider fetch failed; modal will skip insider section.', err);
    return {};
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
    const indexMembership = Array.isArray(uni.index_membership) ? uni.index_membership : [];
    const indices = indexMembership
      .map((s) => INDEX_KEY_NORMALIZE[s])
      .filter(Boolean);
    return {
      ticker: row.ticker,
      name: uni.name || row.ticker,
      exchange: uni.exchange || '',
      sector: uni.sector || row.sector || '',
      indices,                         // normalized short keys, e.g. ['djia','nasdaq-100','sp-100','sp-500']
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
  const { index, band, drift } = state.activeFilters;
  return state.enriched.filter((row) => {
    if (index !== 'all' && !row.indices.includes(index)) return false;
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

  // Task 1: score-trend pill replaces the old "→ 0.0" arrow. The pillar
  // of truth is state.trendByTicker, populated after history fetch.
  // Period prefers 30d but falls back to 14/7/3/1 when history is
  // shallow (e.g. just after a fresh deploy). The label reflects the
  // ACTUAL period so a "+1.5 ▲" pill never claims 30d when it's 3d.
  const trend = state.trendByTicker[row.ticker] || { delta: null, direction: 'unknown', periodDays: null };
  const trendDir = trend.direction || 'unknown';
  const trendArrowChar = trendArrow(trendDir);
  const trendValue = formatTrendValue(trend.delta, trendDir);
  const trendPeriodLabel = (trendDir === 'unknown' || !trend.periodDays)
    ? '30d'
    : `${trend.periodDays}d`;
  const trendAria = trendDir === 'unknown'
    ? 'Score trend unavailable'
    : `${trendPeriodLabel} score change ${trendValue}`;

  const driverLine = row.topDriverKey
    ? `Top: ${escapeHtml(pillarDisplayName(row.topDriverKey))} ${formatScore(row.topDriverValue)}`
    : 'Top: —';

  const indices = (row.indices || []).join(',');
  // Band badge is a button — clicking it filters to that band rather
  // than opening the detail modal. The click handler on the grid checks
  // for `[data-band-filter]` first and stops propagation.
  const bandFilterAria = `Filter to ${bandLabel} band`;
  return (
    `<div class="lthcs-card" data-ticker="${ticker}" data-band="${band}" data-exchange="${exchange}" data-indices="${indices}" data-drift-direction="${direction}">` +
      `<div class="lthcs-card-header">` +
        `<span class="lthcs-card-ticker">${ticker}</span>` +
        `<span class="lthcs-card-score">${score}</span>` +
      `</div>` +
      `<div class="lthcs-card-name">${name}</div>` +
      `<div class="lthcs-card-meta">` +
        `<span class="lthcs-card-sparkline">${sparkline}</span>` +
        `<span class="lthcs-card-trend" data-trend="${trendDir}" title="${trendAria}" aria-label="${trendAria}">` +
          `<span class="lthcs-card-trend-label">${trendPeriodLabel}</span>` +
          `<span class="lthcs-card-trend-arrow" aria-hidden="true">${trendArrowChar}</span>` +
          `<span class="lthcs-card-trend-value">${trendValue}</span>` +
        `</span>` +
      `</div>` +
      `<button type="button" class="lthcs-card-band" data-band-filter="${band}" aria-label="${bandFilterAria}">${bandLabel}</button>` +
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
  // Band counts (over the currently-filtered view).
  const bandCounts = Object.fromEntries(UI_BANDS.map((b) => [b, 0]));
  for (const row of filtered) {
    if (bandCounts[row.uiBand] != null) bandCounts[row.uiBand] += 1;
  }
  for (const band of UI_BANDS) {
    const el = document.querySelector(`[data-band-count="${band}"]`);
    if (el) el.textContent = String(bandCounts[band]);
  }

  // Summary buckets — Buy / Hold / Watch aggregate the granular bands.
  // Mapping: Buy = Elite + High + Constructive, Hold = Monitor, Watch
  // = Weakening + Review. Informational; not filterable (click the
  // underlying band tile to filter).
  const summaryCounts = {
    buy: (bandCounts.elite || 0) + (bandCounts.high || 0) + (bandCounts.constructive || 0),
    hold: bandCounts.monitor || 0,
    watch: (bandCounts.weakening || 0) + (bandCounts.review || 0),
  };
  for (const key of Object.keys(summaryCounts)) {
    const el = document.querySelector(`[data-summary-count="${key}"]`);
    if (el) el.textContent = String(summaryCounts[key]);
  }

  // Index counts — always show the FULL universe count per index (not
  // narrowed by current filter), since they double as filter buttons
  // and the count should reflect "what you'd get if you click here."
  const indexCounts = Object.fromEntries(INDEX_FILTERS.map((k) => [k, 0]));
  for (const row of state.enriched) {
    for (const idx of row.indices) {
      if (indexCounts[idx] != null) indexCounts[idx] += 1;
    }
  }
  for (const idx of INDEX_FILTERS) {
    const el = document.querySelector(`[data-index-count="${idx}"]`);
    if (el) el.textContent = String(indexCounts[idx]);
  }
}

// Render the "Active filters" breadcrumb above the result grid.
// Stays hidden when no filters are active. Each pill names the active
// filter and carries an "×" that clears just that filter; "Clear all"
// appears whenever 2+ filters (including search) are active.
function renderActiveFilters() {
  const el = $('#lthcs-active-filters');
  if (!el) return;

  const active = [];
  for (const group of FILTER_GROUPS) {
    const value = state.activeFilters[group];
    if (value && value !== 'all') {
      const label = (FILTER_VALUE_LABELS[group] && FILTER_VALUE_LABELS[group][value]) || value;
      active.push({ group, value, label });
    }
  }
  const hasSearch = !!(state.searchQuery && state.searchQuery.trim());

  if (!active.length && !hasSearch) {
    el.innerHTML = '';
    el.classList.add('hidden');
    return;
  }

  const parts = ['<span class="lthcs-active-filters-label">Active filters</span>'];

  for (const f of active) {
    const groupLabel = escapeHtml(FILTER_GROUP_LABELS[f.group] || f.group);
    const valueLabel = escapeHtml(f.label);
    const bandAttr = f.group === 'band' ? ` data-band="${escapeHtml(f.value)}"` : '';
    parts.push(
      `<span class="lthcs-active-filter-pill" data-group="${escapeHtml(f.group)}"${bandAttr}>` +
        `<span class="lthcs-active-filter-pill-kind">${groupLabel}</span>` +
        `<span class="lthcs-active-filter-pill-value">${valueLabel}</span>` +
        `<button type="button" class="lthcs-active-filter-pill-clear" data-clear-group="${escapeHtml(f.group)}" aria-label="Clear ${groupLabel} filter">&times;</button>` +
      `</span>`
    );
  }

  if (hasSearch) {
    const searchLabel = escapeHtml(state.searchQuery.trim());
    parts.push(
      `<span class="lthcs-active-filter-pill" data-group="search">` +
        `<span class="lthcs-active-filter-pill-kind">Search</span>` +
        `<span class="lthcs-active-filter-pill-value">${searchLabel}</span>` +
        `<button type="button" class="lthcs-active-filter-pill-clear" data-clear-group="search" aria-label="Clear search">&times;</button>` +
      `</span>`
    );
  }

  // "Clear all" appears as soon as there are 2+ active filters (including
  // search). With only one, the per-pill × is enough.
  const totalActive = active.length + (hasSearch ? 1 : 0);
  if (totalActive >= 2) {
    parts.push(
      `<button type="button" class="lthcs-active-filters-clear-all" data-clear-group="__all__">Clear all</button>`
    );
  }

  el.innerHTML = parts.join('');
  el.classList.remove('hidden');
}

function clearFilter(group) {
  if (group === 'search') {
    state.searchQuery = '';
    const input = $('#lthcs-search');
    if (input) input.value = '';
  } else if (group === '__all__') {
    for (const g of FILTER_GROUPS) state.activeFilters[g] = 'all';
    state.searchQuery = '';
    const input = $('#lthcs-search');
    if (input) input.value = '';
  } else if (FILTER_GROUPS.includes(group)) {
    state.activeFilters[group] = 'all';
  } else {
    return;
  }
  syncAllChips();
  persistFilterState();
  renderAll();
}

function renderAll() {
  const filtered = applyFilters();
  renderCards(filtered);
  updateStats(filtered);
  renderActiveFilters();
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

// Task 2: mirror chip activation onto the clickable stat tiles at the top.
// `group` is 'index' or 'band'; 'all' clears every tile in that group.
function setStatActive(group, value) {
  const attr = group === 'index' ? 'data-index' : 'data-band';
  const tiles = $$(`.lthcs-stat-clickable[${attr}]`);
  for (const tile of tiles) {
    const tileVal = tile.getAttribute(attr);
    if (value !== 'all' && tileVal === value) {
      tile.classList.add('is-active');
      tile.setAttribute('aria-pressed', 'true');
    } else {
      tile.classList.remove('is-active');
      tile.setAttribute('aria-pressed', 'false');
    }
  }
}

// Single source of truth: push activeFilters[group] into both chips and stats.
function syncFilterUI(group) {
  const value = state.activeFilters[group] || 'all';
  setChipActive(group, value);
  if (group === 'index' || group === 'band') {
    setStatActive(group, value);
  }
}

function syncAllChips() {
  for (const group of FILTER_GROUPS) {
    syncFilterUI(group);
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
    index: state.activeFilters.index,
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
      syncFilterUI(group);
      persistFilterState();
      renderAll();
    });
  }
}

// Index stat tiles are also filter buttons — click to activate that
// index filter (and toggle off back to "all" on a second click).
function wireIndexStats() {
  const tiles = $$('.lthcs-stat[data-index]');
  for (const tile of tiles) {
    const activate = () => {
      const idx = tile.dataset.index;
      if (!INDEX_FILTERS.includes(idx)) return;
      const next = state.activeFilters.index === idx ? 'all' : idx;
      state.activeFilters.index = next;
      syncFilterUI('index');
      persistFilterState();
      renderAll();
    };
    tile.addEventListener('click', activate);
    tile.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        activate();
      }
    });
  }
}

// Task 2: Band stat tiles act as primary band filters. Same toggle behavior
// as the index tiles — clicking the active band clears the filter.
function wireBandStats() {
  const tiles = $$('.lthcs-stat-clickable[data-band]');
  for (const tile of tiles) {
    const activate = () => {
      const band = tile.dataset.band;
      if (!UI_BANDS.includes(band)) return;
      const next = state.activeFilters.band === band ? 'all' : band;
      state.activeFilters.band = next;
      syncFilterUI('band');
      persistFilterState();
      renderAll();
    };
    tile.addEventListener('click', activate);
    tile.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        activate();
      }
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

// Delegated click handler for the active-filters breadcrumb.
// Each clear button carries data-clear-group=<group|'search'|'__all__'>.
function wireActiveFilters() {
  const el = $('#lthcs-active-filters');
  if (!el) return;
  el.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-clear-group]');
    if (!btn || !el.contains(btn)) return;
    clearFilter(btn.dataset.clearGroup);
  });
}

function wireEvents() {
  wireSearch();
  wireChips();
  wireIndexStats();
  wireBandStats();
  wireRefresh();
  wireCardClicks();
  wireMoverClicks();
  wireActiveFilters();
}

// Tier 4 #18: clicking a mover tile opens the same detail modal the cards
// use. Delegated handler — the strip is innerHTML'd on every refresh, so we
// bind once to the host section.
function wireMoverClicks() {
  const host = $('#lthcs-movers-strip');
  if (!host) return;
  host.addEventListener('click', (e) => {
    const tile = e.target.closest('[data-ticker]');
    if (!tile || !host.contains(tile)) return;
    const ticker = tile.dataset.ticker;
    if (!ticker) return;
    const scores = (state.snapshot && state.snapshot.scores) || [];
    const snapshotRow = scores.find((r) => r && r.ticker === ticker) || null;
    if (!snapshotRow) return;
    const universeEntry = state.universeByTicker[ticker] || null;
    const narrative = (state.narrativesByTicker && state.narrativesByTicker[ticker]) || null;
    const calcDate = (state.snapshot && state.snapshot.calc_date) || null;
    const insider = (state.insiderByTicker && state.insiderByTicker[ticker]) || null;
    openDetail({ ticker, snapshotRow, universeEntry, narrative, calcDate, insider });
  });
}

// --- Week 9 (detail modal) hookup ---
// Delegated click handler on the card grid. When a card is clicked, look up
// the underlying snapshot row + universe entry from in-memory state and open
// the detail modal. Narratives aren't loaded into state yet (V1) — pass null.
function wireCardClicks() {
  const container = $('#lthcs-cards');
  if (!container) return;
  container.addEventListener('click', (e) => {
    // Band badge: clicking it filters to that band instead of opening
    // the detail modal. Toggle behaviour matches the band tiles above —
    // a second click on the active band clears the filter.
    const bandBtn = e.target.closest('[data-band-filter]');
    if (bandBtn && container.contains(bandBtn)) {
      e.stopPropagation();
      const band = bandBtn.dataset.bandFilter;
      if (UI_BANDS.includes(band)) {
        const next = state.activeFilters.band === band ? 'all' : band;
        state.activeFilters.band = next;
        syncFilterUI('band');
        persistFilterState();
        renderAll();
      }
      return;
    }

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
    const insider = (state.insiderByTicker && state.insiderByTicker[ticker]) || null;
    openDetail({ ticker, snapshotRow, universeEntry, narrative, calcDate, insider });
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

    // Week 11: side-load insider map + regime strip. Both are best-effort
    // and must never block the main render or surface errors to the user.
    const calcDate = snapshot && snapshot.calc_date;
    fetchInsider(calcDate)
      .then((map) => { state.insiderByTicker = map || {}; })
      .catch((err) => console.warn('LTHCS: insider load failed', err));
    renderRegimeStrip(calcDate).catch((err) => {
      console.warn('LTHCS: regime strip render failed', err);
    });

    // Task 1: side-load per-ticker history → 30d score-trend deltas.
    // Cards render immediately with "?" placeholders, then re-render once
    // the history fetches resolve. Best-effort; per-ticker errors don't bubble.
    fetchTrendMap(state.enriched, calcDate)
      .then((map) => {
        state.trendByTicker = map || {};
        // Re-render only the cards — stats/chips are unaffected by trend.
        renderCards(applyFilters());
        // Tier 4 #18: paint the movers strip once trends are known. The
        // strip reads state.trendByTicker directly, so we wait until it's
        // populated before showing anything (the section starts hidden).
        try { renderMoversStrip(state); }
        catch (e) { console.warn('LTHCS: movers strip render failed', e); }
      })
      .catch((err) => console.warn('LTHCS: trend map load failed', err));
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
