// lthcs-table.js
// Bloomberg-style sortable table view over the LTHCS universe.
// Vanilla ES2020 module. Reuses the detail modal from ../lthcs_tab/lthcs-detail.js
// via dynamic import on first row click.

'use strict';

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const SNAPSHOTS_BASE = '../data/lthcs/snapshots';
const UNIVERSE_URL = '../data/lthcs/universe.json';
const INDEX_URL = `${SNAPSHOTS_BASE}/index.json`;
const INSIDER_BASE = '../data/lthcs/insider';
const HOLDINGS_BASE = '../data/lthcs/holdings';
const HISTORY_BASE = '../data/lthcs/history/by_ticker';

// Trend tuning — kept in lock-step with lthcs_tab/lthcs-tab.js so the
// arrows on this page match the cards on the main page.
const TREND_FLAT_THRESHOLD = 0.5;
const TREND_FALLBACK_DAYS = [30, 14, 7, 3, 1];

const STORAGE_KEYS = {
  starred: 'lthcs.starred',
  tableSort: 'lthcs.table.sort',
  tableFilters: 'lthcs.table.filters',
};

const BAND_SNAPSHOT_TO_UI = {
  elite: 'elite',
  high_confidence: 'high',
  constructive: 'constructive',
  monitor: 'monitor',
  weakening: 'weakening',
  review: 'review',
};

const BAND_LABEL = {
  elite: 'Elite',
  high: 'High Conf',
  constructive: 'Constructive',
  monitor: 'Monitor',
  weakening: 'Weakening',
  review: 'Review',
};

const INDEX_KEY_NORMALIZE = {
  'DJIA': 'DJIA',
  'NASDAQ-100': 'N100',
  'S&P 100': 'SP100',
  'S&P 500': 'SP500',
};
const INDEX_FILTER_MATCH = {
  djia: 'DJIA',
  'nasdaq-100': 'N100',
  'sp-100': 'SP100',
};

// Column definitions drive both the <thead> render and the sort comparator.
// `align: 'right'` flips text-alignment and applies the mono/tabular-nums
// styling. `mobileHidden` hides the column at ≤768px. `key` is the property
// on the row object; `sortKey` overrides the property used by the comparator
// when it differs (e.g. score uses the raw numeric).
const COLUMNS = [
  { id: 'star',     label: '★',      sortKey: 'starred',    align: 'left',  sortable: true,  cssClass: 'lthcs-col-star' },
  { id: 'ticker',   label: 'Ticker', sortKey: 'ticker',     align: 'left',  sortable: true,  cssClass: 'lthcs-col-ticker' },
  { id: 'name',     label: 'Name',   sortKey: 'name',       align: 'left',  sortable: true,  cssClass: 'lthcs-col-name' },
  { id: 'score',    label: 'Score',  sortKey: 'score',      align: 'right', sortable: true,  cssClass: 'lthcs-col-score' },
  { id: 'band',     label: 'Band',   sortKey: 'bandRank',   align: 'left',  sortable: true },
  { id: 'trend',    label: '30d Δ',  sortKey: 'trendDelta', align: 'right', sortable: true },
  { id: 'adopt',    label: 'Adopt',  sortKey: 'subAdopt',   align: 'right', sortable: true,  mobileHidden: true },
  { id: 'inst',     label: 'Inst',   sortKey: 'subInst',    align: 'right', sortable: true,  mobileHidden: true },
  { id: 'fin',      label: 'Fin',    sortKey: 'subFin',     align: 'right', sortable: true,  mobileHidden: true },
  { id: 'thes',     label: 'Thes',   sortKey: 'subThes',    align: 'right', sortable: true,  mobileHidden: true },
  { id: 'des',      label: 'DES',    sortKey: 'subDES',     align: 'right', sortable: true,  mobileHidden: true },
  { id: 'insider',  label: 'Insider', sortKey: 'insiderConv', align: 'left', sortable: true },
  { id: 'holdings', label: 'Holdings', sortKey: 'holdingsScore', align: 'left', sortable: true },
  { id: 'index',    label: 'Index',  sortKey: 'indicesLabel', align: 'left', sortable: true },
];

// Display-order rank for the band column so sort-by-band groups
// strongest→weakest rather than alphabetical.
const BAND_SORT_RANK = {
  elite: 0,
  high: 1,
  constructive: 2,
  monitor: 3,
  weakening: 4,
  review: 5,
};

const INSIDER_REGIME_LABEL = {
  strong_buying: 'strong buy',
  mild_buying: 'mild buy',
  neutral: 'neutral',
  mild_selling: 'mild sell',
  heavy_selling: 'heavy sell',
};

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

const state = {
  snapshot: null,
  universeByTicker: {},
  insiderByTicker: {},
  holdingsByTicker: {},
  trendByTicker: {},
  rows: [],                    // enriched merged rows
  filters: {
    index: 'all',
    band: 'all',
    drift: 'all',
    search: '',
  },
  // Sort spec: array of { key, dir }. Multi-column via shift-click; the
  // primary sort is index 0. Empty array = default (score desc).
  sorts: [{ key: 'score', dir: 'desc' }],
  starred: new Set(),
};

// ---------------------------------------------------------------------------
// DOM helpers
// ---------------------------------------------------------------------------

const $ = (sel, root = document) => root.querySelector(sel);
const show = (el) => el && el.classList.remove('hidden');
const hide = (el) => el && el.classList.add('hidden');

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

function fmtScore(n) {
  const v = Number(n);
  return Number.isFinite(v) ? v.toFixed(1) : '—';
}

function fmtSub(n) {
  const v = Number(n);
  return Number.isFinite(v) ? v.toFixed(0) : '—';
}

function fmtTrend(delta, direction) {
  if (direction === 'unknown' || delta == null || !Number.isFinite(delta)) return '—';
  if (direction === 'flat') return '0.0';
  const sign = delta > 0 ? '+' : '';
  return `${sign}${delta.toFixed(1)}`;
}

function trendArrow(direction) {
  if (direction === 'up') return '▲';
  if (direction === 'down') return '▼';
  if (direction === 'flat') return '→';
  return '?';
}

function classifyDriftFromDelta(delta) {
  // Drift-direction classification matches the chip filter on the main page:
  // ±1.0 30d-score change ≈ "improving" / "declining"; otherwise stable.
  // For this page we use the 30d snapshot drift (not the recomputed trend
  // delta) since the chip filter on the main page reads from drift_30d.
  const v = Number(delta) || 0;
  if (v > 1.0) return 'improving';
  if (v < -1.0) return 'declining';
  return 'stable';
}

// ---------------------------------------------------------------------------
// Trend helpers (mirror lthcs_tab/lthcs-tab.js)
// ---------------------------------------------------------------------------

function parseISODateUTC(s) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(s || ''));
  if (!m) return NaN;
  return Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
}

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
    if (ms <= targetMs && ms > bestMs) {
      bestMs = ms;
      best = entry;
    }
  }
  return best;
}

function computeTrend(historyDoc, currentDateISO, currentScore) {
  if (!historyDoc || !Array.isArray(historyDoc.history)) {
    return { delta: null, direction: 'unknown', periodDays: null };
  }
  for (const days of TREND_FALLBACK_DAYS) {
    const anchor = pickAnchorForLookback(historyDoc.history, currentDateISO, days);
    if (anchor && Number.isFinite(Number(anchor.score))) {
      const cur = Number(currentScore);
      if (!Number.isFinite(cur)) {
        return { delta: null, direction: 'unknown', periodDays: null };
      }
      const delta = cur - Number(anchor.score);
      let direction = 'flat';
      if (delta > TREND_FLAT_THRESHOLD) direction = 'up';
      else if (delta < -TREND_FLAT_THRESHOLD) direction = 'down';
      return { delta, direction, periodDays: days };
    }
  }
  return { delta: null, direction: 'unknown', periodDays: null };
}

// ---------------------------------------------------------------------------
// Data fetching
// ---------------------------------------------------------------------------

async function fetchJSON(url) {
  const res = await fetch(url, { cache: 'no-store' });
  if (!res.ok) throw new Error(`Fetch failed: ${url} → ${res.status}`);
  return res.json();
}

async function fetchSnapshot() {
  const index = await fetchJSON(INDEX_URL);
  const latest = index && index.latest;
  if (!latest) throw new Error('Snapshot index has no `latest` date.');
  const snapshot = await fetchJSON(`${SNAPSHOTS_BASE}/${latest}.json`);
  return { latest, snapshot };
}

async function fetchOptional(url) {
  try {
    const res = await fetch(url, { cache: 'no-store' });
    if (!res.ok) return null;
    return await res.json();
  } catch {
    return null;
  }
}

async function fetchUniverse() {
  const u = await fetchOptional(UNIVERSE_URL);
  return (u && Array.isArray(u.tickers)) ? u.tickers : [];
}

async function fetchTrendMap(rows, calcDate) {
  if (!Array.isArray(rows) || !rows.length || !calcDate) return {};
  const fetches = rows.map(async (row) => {
    const ticker = row && row.ticker;
    if (!ticker) return [null, { delta: null, direction: 'unknown', periodDays: null }];
    const url = `${HISTORY_BASE}/${encodeURIComponent(ticker)}.json`;
    try {
      const res = await fetch(url, { cache: 'no-store' });
      if (!res.ok) return [ticker, { delta: null, direction: 'unknown', periodDays: null }];
      const doc = await res.json();
      return [ticker, computeTrend(doc, calcDate, row.score)];
    } catch {
      return [ticker, { delta: null, direction: 'unknown', periodDays: null }];
    }
  });
  const results = await Promise.all(fetches);
  const map = {};
  for (const [ticker, trend] of results) {
    if (ticker) map[ticker] = trend;
  }
  return map;
}

// ---------------------------------------------------------------------------
// Enrichment
// ---------------------------------------------------------------------------

function buildUniverseIndex(tickers) {
  const idx = {};
  for (const row of tickers) {
    if (row && row.ticker) idx[row.ticker] = row;
  }
  return idx;
}

function enrichRows(snapshot, universeByTicker, insiderByTicker, holdingsByTicker) {
  const scores = (snapshot && snapshot.scores) || [];
  return scores.map((row) => {
    const uni = universeByTicker[row.ticker] || {};
    const sub = row.subscores || {};
    const insider = insiderByTicker[row.ticker] || null;
    const holdings = holdingsByTicker[row.ticker] || null;
    const uiBand = BAND_SNAPSHOT_TO_UI[row.band] || row.band || 'review';
    const indicesRaw = Array.isArray(uni.index_membership) ? uni.index_membership : [];
    const indices = indicesRaw.map((s) => INDEX_KEY_NORMALIZE[s]).filter(Boolean);

    return {
      ticker: row.ticker,
      name: uni.name || row.ticker,
      sector: uni.sector || row.sector || '',
      indices,
      indicesLabel: indices.join(','),
      score: Number(row.lthcs_score),
      snapshotBand: row.band,
      uiBand,
      bandLabel: BAND_LABEL[uiBand] || uiBand,
      bandRank: BAND_SORT_RANK[uiBand] != null ? BAND_SORT_RANK[uiBand] : 99,
      drift30d: Number(row.drift_30d) || 0,
      driftDirection: classifyDriftFromDelta(row.drift_30d),
      subAdopt: Number(sub.adoption_momentum),
      subInst: Number(sub.institutional_confidence),
      subFin: Number(sub.financial_evolution),
      subThes: Number(sub.thesis_integrity),
      subDES: Number(sub.des),
      // Insider — surface regime + numeric conviction for sort.
      insiderRegime: insider ? String(insider.regime || 'neutral') : 'none',
      insiderConv: insider && Number.isFinite(Number(insider.conviction_score))
        ? Number(insider.conviction_score) : null,
      // Holdings — conviction_signal string + signal_score numeric.
      holdingsSignal: holdings ? String(holdings.conviction_signal || 'unknown') : 'unknown',
      holdingsScore: holdings && Number.isFinite(Number(holdings.signal_score))
        ? Number(holdings.signal_score) : null,
      starred: state.starred.has(row.ticker),
      // raw snapshot row for the detail modal
      _snapshotRow: row,
      _universeEntry: uni,
      _insider: insider,
      _holdings: holdings,
    };
  });
}

// ---------------------------------------------------------------------------
// Filter + sort
// ---------------------------------------------------------------------------

function applyFilters(rows) {
  const q = (state.filters.search || '').trim().toLowerCase();
  const { index, band, drift } = state.filters;
  const indexMatch = INDEX_FILTER_MATCH[index];
  return rows.filter((row) => {
    if (index !== 'all' && (!indexMatch || !row.indices.includes(indexMatch))) return false;
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

function cmpValues(a, b) {
  // Robust null-aware compare. Nulls sort last regardless of direction
  // (so missing data doesn't dominate the top of a sorted view).
  const aNull = a == null || (typeof a === 'number' && !Number.isFinite(a));
  const bNull = b == null || (typeof b === 'number' && !Number.isFinite(b));
  if (aNull && bNull) return 0;
  if (aNull) return 1;
  if (bNull) return -1;
  if (typeof a === 'number' && typeof b === 'number') return a - b;
  return String(a).localeCompare(String(b));
}

function applySort(rows) {
  if (!state.sorts.length) return rows.slice();
  const sorts = state.sorts;
  const out = rows.slice();
  out.sort((ra, rb) => {
    for (const s of sorts) {
      let av = ra[s.key];
      let bv = rb[s.key];
      // The "star" column sorts starred-first regardless of direction
      // unless explicitly reversed: true > false sortable comparison.
      if (s.key === 'starred') {
        av = av ? 1 : 0;
        bv = bv ? 1 : 0;
      }
      let c = cmpValues(av, bv);
      if (c !== 0) return s.dir === 'desc' ? -c : c;
    }
    // Stable tie-break: ticker A→Z
    return String(ra.ticker || '').localeCompare(String(rb.ticker || ''));
  });
  return out;
}

// ---------------------------------------------------------------------------
// Rendering — header
// ---------------------------------------------------------------------------

function renderHead() {
  const thead = $('#lthcs-table-head');
  if (!thead) return;
  const sortMap = new Map(state.sorts.map((s, i) => [s.key, { dir: s.dir, rank: i }]));
  thead.innerHTML = COLUMNS.map((col) => {
    const sortInfo = sortMap.get(col.sortKey);
    const isSorted = !!sortInfo;
    const indicator = isSorted
      ? (sortInfo.dir === 'desc' ? '▼' : '▲')
      : '';
    const rank = (isSorted && state.sorts.length > 1)
      ? `<span class="lthcs-table-sort-rank">${sortInfo.rank + 1}</span>`
      : '';
    const ariaSort = isSorted
      ? (sortInfo.dir === 'desc' ? 'descending' : 'ascending')
      : 'none';
    const classes = [
      col.cssClass || '',
      isSorted ? 'is-sorted' : '',
      col.mobileHidden ? 'is-hidden-mobile' : '',
    ].filter(Boolean).join(' ');
    return (
      `<th scope="col" ` +
        `class="${classes}" ` +
        `data-col-id="${col.id}" ` +
        `data-sort-key="${col.sortKey}" ` +
        `data-align="${col.align}" ` +
        `aria-sort="${ariaSort}" ` +
        `title="Click to sort. Shift-click for multi-column sort.">` +
        escapeHtml(col.label) +
        `<span class="lthcs-table-sort-indicator">${indicator}</span>` +
        rank +
      `</th>`
    );
  }).join('');
}

// ---------------------------------------------------------------------------
// Rendering — body
// ---------------------------------------------------------------------------

function rowHTML(row) {
  const ticker = escapeHtml(row.ticker);
  const name = escapeHtml(row.name);
  const band = escapeHtml(row.uiBand);
  const bandLabel = escapeHtml(row.bandLabel);
  const indices = escapeHtml(row.indicesLabel || '—');

  const trend = state.trendByTicker[row.ticker] || { delta: null, direction: 'unknown', periodDays: null };
  const trendDir = trend.direction || 'unknown';
  const trendValue = fmtTrend(trend.delta, trendDir);
  const trendArr = trendArrow(trendDir);

  const insiderRegime = row.insiderRegime || 'none';
  const insiderLabel = INSIDER_REGIME_LABEL[insiderRegime] || (insiderRegime === 'none' ? '—' : insiderRegime);
  const holdingsSignal = row.holdingsSignal || 'unknown';
  const holdingsLabel = holdingsSignal === 'unknown' ? '—' : holdingsSignal;

  const starPressed = row.starred ? 'true' : 'false';
  const starChar = row.starred ? '★' : '☆';

  return (
    `<tr data-ticker="${ticker}">` +
      `<td class="lthcs-col-star" data-align="left">` +
        `<button type="button" class="lthcs-table-star" data-star="${ticker}" ` +
                `aria-pressed="${starPressed}" aria-label="Star ${ticker}">${starChar}</button>` +
      `</td>` +
      `<td class="lthcs-col-ticker" data-align="left">${ticker}</td>` +
      `<td class="lthcs-col-name" data-align="left" title="${name}">${name}</td>` +
      `<td class="lthcs-col-score" data-align="right">${fmtScore(row.score)}</td>` +
      `<td data-align="left">` +
        `<span class="lthcs-table-band-pill" data-band="${band}">${bandLabel}</span>` +
      `</td>` +
      `<td data-align="right">` +
        `<span class="lthcs-table-trend" data-trend="${trendDir}">` +
          `<span class="lthcs-table-trend-arrow" aria-hidden="true">${trendArr}</span>` +
          `<span>${trendValue}</span>` +
        `</span>` +
      `</td>` +
      `<td class="is-hidden-mobile" data-align="right">${fmtSub(row.subAdopt)}</td>` +
      `<td class="is-hidden-mobile" data-align="right">${fmtSub(row.subInst)}</td>` +
      `<td class="is-hidden-mobile" data-align="right">${fmtSub(row.subFin)}</td>` +
      `<td class="is-hidden-mobile" data-align="right">${fmtSub(row.subThes)}</td>` +
      `<td class="is-hidden-mobile" data-align="right">${fmtSub(row.subDES)}</td>` +
      `<td data-align="left">` +
        `<span class="lthcs-table-signal" data-regime="${escapeHtml(insiderRegime)}">${escapeHtml(insiderLabel)}</span>` +
      `</td>` +
      `<td data-align="left">` +
        `<span class="lthcs-table-signal" data-signal="${escapeHtml(holdingsSignal)}">${escapeHtml(holdingsLabel)}</span>` +
      `</td>` +
      `<td data-align="left"><span class="lthcs-table-indices">${indices}</span></td>` +
    `</tr>`
  );
}

function renderBody(filtered) {
  const tbody = $('#lthcs-table-body');
  const empty = $('#lthcs-table-empty');
  const scroll = $('#lthcs-table-scroll');
  const shown = $('#lthcs-table-shown');
  if (!tbody) return;
  if (shown) shown.textContent = String(filtered.length);
  if (!filtered.length) {
    tbody.innerHTML = '';
    hide(scroll);
    show(empty);
    return;
  }
  hide(empty);
  show(scroll);
  tbody.innerHTML = filtered.map(rowHTML).join('');
}

function renderAll() {
  const filtered = applySort(applyFilters(state.rows));
  renderHead();
  renderBody(filtered);
  // Keep the latest filtered view so Export CSV operates on the same set
  // the user is looking at.
  state._lastFiltered = filtered;
}

// ---------------------------------------------------------------------------
// CSV export
// ---------------------------------------------------------------------------

function csvEscape(value) {
  if (value == null) return '';
  const s = String(value);
  // Wrap in quotes if contains comma, quote, newline, or leading/trailing space.
  if (/[",\n\r]/.test(s) || /^\s|\s$/.test(s)) {
    return `"${s.replace(/"/g, '""')}"`;
  }
  return s;
}

function buildCSV(rows) {
  const headers = [
    'ticker', 'name', 'score', 'band', 'drift_30d',
    'adoption_momentum', 'institutional_confidence', 'financial_evolution',
    'thesis_integrity', 'des',
    'insider_regime', 'insider_conviction',
    'holdings_signal', 'holdings_score',
    'indices', 'sector',
  ];
  const lines = [headers.join(',')];
  for (const r of rows) {
    const trend = state.trendByTicker[r.ticker] || {};
    const trendNum = (trend.direction === 'unknown' || trend.delta == null) ? '' : trend.delta.toFixed(2);
    void trendNum; // (we expose drift_30d as the formal column; trend pill is a UI helper)
    lines.push([
      r.ticker,
      r.name,
      Number.isFinite(r.score) ? r.score.toFixed(2) : '',
      r.bandLabel,
      Number.isFinite(r.drift30d) ? r.drift30d.toFixed(2) : '',
      Number.isFinite(r.subAdopt) ? r.subAdopt.toFixed(2) : '',
      Number.isFinite(r.subInst) ? r.subInst.toFixed(2) : '',
      Number.isFinite(r.subFin) ? r.subFin.toFixed(2) : '',
      Number.isFinite(r.subThes) ? r.subThes.toFixed(2) : '',
      Number.isFinite(r.subDES) ? r.subDES.toFixed(2) : '',
      r.insiderRegime,
      r.insiderConv == null ? '' : r.insiderConv.toFixed(3),
      r.holdingsSignal,
      r.holdingsScore == null ? '' : r.holdingsScore.toFixed(3),
      r.indicesLabel,
      r.sector,
    ].map(csvEscape).join(','));
  }
  return lines.join('\r\n');
}

function downloadCSV() {
  const rows = state._lastFiltered || applySort(applyFilters(state.rows));
  const csv = buildCSV(rows);
  // Prepend UTF-8 BOM so Excel reads non-ASCII (e.g. "—") correctly.
  const blob = new Blob(['﻿', csv], { type: 'text/csv;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  const calc = (state.snapshot && state.snapshot.calc_date) || 'snapshot';
  a.href = url;
  a.download = `lthcs-${calc}.csv`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  // Defer revocation until after the click event has resolved.
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// ---------------------------------------------------------------------------
// Sort interactions
// ---------------------------------------------------------------------------

function toggleSort(sortKey, additive) {
  const existing = state.sorts.find((s) => s.key === sortKey);
  if (!additive) {
    // Single-column mode: cycle asc → desc → cleared on the active column,
    // otherwise replace the sort entirely with a fresh desc on the new col.
    if (existing && state.sorts.length === 1 && state.sorts[0].key === sortKey) {
      if (existing.dir === 'desc') {
        existing.dir = 'asc';
      } else {
        // 3rd click clears → fall back to default (score desc) so the table
        // never sits in an "unsorted" visually-random state.
        state.sorts = [{ key: 'score', dir: 'desc' }];
      }
    } else {
      // New primary sort. Numeric/score-y columns default to desc (high to
      // low feels natural). Text columns default to asc.
      const dir = isTextColumn(sortKey) ? 'asc' : 'desc';
      state.sorts = [{ key: sortKey, dir }];
    }
  } else {
    // Multi-column mode (shift-click): toggle direction if already present,
    // otherwise append as the next-rank tiebreaker.
    if (existing) {
      existing.dir = existing.dir === 'desc' ? 'asc' : 'desc';
    } else {
      const dir = isTextColumn(sortKey) ? 'asc' : 'desc';
      state.sorts.push({ key: sortKey, dir });
    }
  }
  persistSort();
  renderAll();
}

function isTextColumn(sortKey) {
  return sortKey === 'ticker' || sortKey === 'name' || sortKey === 'indicesLabel';
}

// ---------------------------------------------------------------------------
// Persistence
// ---------------------------------------------------------------------------

function safeGet(key) {
  try { return window.localStorage.getItem(key); } catch { return null; }
}
function safeSet(key, value) {
  try { window.localStorage.setItem(key, value); } catch { /* quota */ }
}

function loadStarred() {
  const raw = safeGet(STORAGE_KEYS.starred);
  if (!raw) return new Set();
  try {
    const arr = JSON.parse(raw);
    if (Array.isArray(arr)) return new Set(arr.filter((t) => typeof t === 'string'));
  } catch { /* ignore */ }
  return new Set();
}

function persistStarred() {
  safeSet(STORAGE_KEYS.starred, JSON.stringify(Array.from(state.starred)));
}

function loadSort() {
  const raw = safeGet(STORAGE_KEYS.tableSort);
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw);
    if (Array.isArray(parsed) && parsed.every((s) => s && typeof s.key === 'string')) {
      return parsed;
    }
  } catch { /* ignore */ }
  return null;
}

function persistSort() {
  safeSet(STORAGE_KEYS.tableSort, JSON.stringify(state.sorts));
}

function loadFilters() {
  const raw = safeGet(STORAGE_KEYS.tableFilters);
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw);
    if (parsed && typeof parsed === 'object') return parsed;
  } catch { /* ignore */ }
  return null;
}

function persistFilters() {
  safeSet(STORAGE_KEYS.tableFilters, JSON.stringify(state.filters));
}

// ---------------------------------------------------------------------------
// Event wiring
// ---------------------------------------------------------------------------

function wireHeader() {
  const thead = $('#lthcs-table-head');
  if (!thead) return;
  thead.addEventListener('click', (e) => {
    const th = e.target.closest('th[data-sort-key]');
    if (!th) return;
    const sortKey = th.dataset.sortKey;
    if (!sortKey) return;
    toggleSort(sortKey, e.shiftKey === true);
  });
}

function wireBody() {
  const tbody = $('#lthcs-table-body');
  if (!tbody) return;
  tbody.addEventListener('click', async (e) => {
    // Star toggle takes precedence — stops the row click from opening
    // the detail modal.
    const starBtn = e.target.closest('[data-star]');
    if (starBtn) {
      e.stopPropagation();
      const ticker = starBtn.dataset.star;
      if (!ticker) return;
      if (state.starred.has(ticker)) {
        state.starred.delete(ticker);
      } else {
        state.starred.add(ticker);
      }
      // Update the row state and re-render. We avoid a full re-render
      // because the sort might change if the user is sorting by star.
      for (const r of state.rows) {
        if (r.ticker === ticker) r.starred = state.starred.has(ticker);
      }
      persistStarred();
      renderAll();
      return;
    }

    const tr = e.target.closest('tr[data-ticker]');
    if (!tr) return;
    const ticker = tr.dataset.ticker;
    const row = state.rows.find((r) => r.ticker === ticker);
    if (!row) return;
    try {
      // Dynamic import — the detail module from lthcs_tab/ is fetched only
      // on first row click, keeping initial page load light.
      const mod = await import('../lthcs_tab/lthcs-detail.js');
      mod.openDetail({
        ticker,
        snapshotRow: row._snapshotRow,
        universeEntry: row._universeEntry,
        narrative: null,
        calcDate: state.snapshot && state.snapshot.calc_date,
        insider: row._insider,
        // The current detail module doesn't render holdings, but pass it
        // anyway so future extensions can pick it up without a new wiring.
        holdings: row._holdings,
      });
    } catch (err) {
      console.warn('LTHCS table: failed to open detail modal', err);
    }
  });
}

function wireControls() {
  const search = $('#lthcs-table-search');
  if (search) {
    search.value = state.filters.search || '';
    search.addEventListener('input', (e) => {
      state.filters.search = e.target.value || '';
      persistFilters();
      renderAll();
    });
  }

  const wireSelect = (id, key) => {
    const el = $(id);
    if (!el) return;
    el.value = state.filters[key] || 'all';
    el.addEventListener('change', (e) => {
      state.filters[key] = e.target.value || 'all';
      persistFilters();
      renderAll();
    });
  };
  wireSelect('#lthcs-table-filter-index', 'index');
  wireSelect('#lthcs-table-filter-band', 'band');
  wireSelect('#lthcs-table-filter-drift', 'drift');

  const exportBtn = $('#lthcs-table-export');
  if (exportBtn) exportBtn.addEventListener('click', downloadCSV);
}

// ---------------------------------------------------------------------------
// Top-level flow
// ---------------------------------------------------------------------------

function showError(err) {
  console.error('LTHCS table:', err);
  const el = $('#lthcs-table-error');
  if (el) {
    el.textContent = 'Could not load snapshot. Try refresh, or check that data/lthcs/snapshots/ exists.';
    show(el);
  }
  hide($('#lthcs-table-loading'));
  hide($('#lthcs-table-scroll'));
}

function renderMeta(snapshot, count) {
  const lastEl = $('#lthcs-table-last-updated');
  if (lastEl) lastEl.textContent = formatDate(snapshot && snapshot.calc_date);
  const versionEl = $('#lthcs-table-model-version');
  if (versionEl && snapshot && snapshot.model_version) {
    versionEl.textContent = snapshot.model_version;
  }
  const countEl = $('#lthcs-table-count');
  if (countEl) countEl.textContent = String(count);
}

async function init() {
  // Restore persisted UI state up-front so the first render is in the user's
  // last-used view (filters, sort, starred).
  state.starred = loadStarred();
  const savedFilters = loadFilters();
  if (savedFilters) Object.assign(state.filters, savedFilters);
  const savedSort = loadSort();
  if (savedSort && savedSort.length) state.sorts = savedSort;

  wireControls();
  wireHeader();
  wireBody();

  try {
    const [{ snapshot }, universeTickers] = await Promise.all([
      fetchSnapshot(),
      fetchUniverse(),
    ]);
    state.snapshot = snapshot;
    state.universeByTicker = buildUniverseIndex(universeTickers);

    const calcDate = snapshot && snapshot.calc_date;
    // Insider + holdings are best-effort enrichment. Older snapshots may
    // not have these files; render the table either way.
    const [insider, holdings] = await Promise.all([
      fetchOptional(`${INSIDER_BASE}/${calcDate}.json`),
      fetchOptional(`${HOLDINGS_BASE}/${calcDate}.json`),
    ]);
    state.insiderByTicker = (insider && typeof insider === 'object') ? insider : {};
    state.holdingsByTicker = (holdings && typeof holdings === 'object') ? holdings : {};

    state.rows = enrichRows(snapshot, state.universeByTicker,
                            state.insiderByTicker, state.holdingsByTicker);

    renderMeta(snapshot, state.rows.length);
    hide($('#lthcs-table-loading'));
    renderAll();

    // History → 30d trend deltas. Side-loaded so the table is interactive
    // immediately; the trend column re-renders once the history fetches
    // resolve. Per-ticker errors are swallowed.
    fetchTrendMap(state.rows, calcDate).then((map) => {
      state.trendByTicker = map || {};
      renderAll();
    }).catch((err) => console.warn('LTHCS table: trend load failed', err));
  } catch (err) {
    showError(err);
  }
}

document.addEventListener('DOMContentLoaded', () => {
  init().catch((err) => {
    console.error('LTHCS table init failed:', err);
    showError(err);
  });
});
