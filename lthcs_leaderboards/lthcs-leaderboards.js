/* =========================================================================
   LTHCS Per-pillar Leaderboards (Phase 5 #7 — EPSILON).

   Renders 7 ranked cards from the latest snapshot:
     - Top 10 by Composite
     - Top 10 by Adoption Momentum
     - Top 10 by Institutional Confidence
     - Top 10 by Financial Evolution
     - Top 10 by Thesis Integrity
     - Top 10 by DES
     - Bottom 10 by Composite (the "trouble" watch list)

   Each row: rank, ticker, sparkline (from per-ticker history when fetchable),
   the pillar's score being ranked, band chip color-coded by composite score.

   Filters: chip group at the top toggles the universe scope between
   { All, DJIA 30, S&P 100, NASDAQ-100, <watchlists>, Active watchlist }.
   Persisted to localStorage so a refresh keeps the user's view.

   Reuses ../lthcs_tab/lthcs-sparkline.js for the sparkline + band-color
   helpers, so visual treatment stays in lockstep with the card view.
   ========================================================================= */

import {
  renderSparkline,
  bandColorForScore,
} from '../lthcs_tab/lthcs-sparkline.js';

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

// Use the stable filename — survives daily date changes without code edits.
const SNAPSHOT_URL = '../data/lthcs/public/latest_snapshot.json';
const UNIVERSE_URL = '../data/lthcs/universe.json';
const HISTORY_URL_TPL = '../data/lthcs/history/by_ticker/{TICKER}.json';

const SCOPE_STORAGE_KEY = 'lthcs.leaderboards.scope';

// Watchlist storage keys — must match lthcs_tab/lthcs-watchlists.js. We read
// them directly (no shared module) because the watchlists module mutates a
// running-page state cache, and we don't want a stale import to fight the
// card view if both pages are open in different tabs.
const WL_STORAGE_KEYS = {
  lists: 'lthcs.watchlists',
  active: 'lthcs.activeWatchlist',
};

// Maturity-stage band thresholds (mirrors weights.json `band_colors`).
const BAND_LABEL = {
  elite: 'Elite',
  high: 'High',
  constructive: 'Const.',
  monitor: 'Monitor',
  weakening: 'Weak.',
  review: 'Review',
};

// Pillar key → human label. Order here = render order of the leaderboard
// cards (after composite_top, before composite_bottom).
const PILLAR_LABEL = {
  adoption_momentum: 'Adoption Momentum',
  institutional_confidence: 'Institutional Confidence',
  financial_evolution: 'Financial Evolution',
  thesis_integrity: 'Thesis Integrity',
  des: 'DES',
};

// Index-membership label → universe.json `index_membership` array value.
// Matches the chips in the V1 card view's index drill-down (Variant C).
const SCOPE_TO_INDEX = {
  djia: 'DJIA',
  'sp-100': 'S&P 100',
  'nasdaq-100': 'NASDAQ-100',
};

const TOP_N = 10;
// Cap concurrent history fetches so we don't fire 70+ requests at once on
// scope=All. Modest because most tickers won't ever appear in any board.
const HISTORY_CONCURRENCY = 6;

// ---------------------------------------------------------------------------
// DOM helpers
// ---------------------------------------------------------------------------

function $(id) {
  return document.getElementById(id);
}
function show(id) {
  $(id)?.classList.remove('hidden');
}
function hide(id) {
  $(id)?.classList.add('hidden');
}
function setText(id, text) {
  const n = $(id);
  if (n) n.textContent = text;
}

// ---------------------------------------------------------------------------
// localStorage helpers (defensive — private-mode / quota / cross-origin)
// ---------------------------------------------------------------------------

function lsGet(key) {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}
function lsSet(key, value) {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    /* ignore */
  }
}

function loadWatchlists() {
  const raw = lsGet(WL_STORAGE_KEYS.lists);
  if (!raw) return {};
  try {
    const parsed = JSON.parse(raw);
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
      // Coerce values to arrays of strings — tolerate the same shape mutations
      // lthcs-watchlists.js does.
      const out = {};
      for (const [name, tickers] of Object.entries(parsed)) {
        if (Array.isArray(tickers)) {
          out[name] = tickers.filter((t) => typeof t === 'string');
        }
      }
      return out;
    }
  } catch {
    /* fallthrough */
  }
  return {};
}

function loadActiveWatchlistName() {
  const raw = lsGet(WL_STORAGE_KEYS.active);
  if (!raw) return null;
  try {
    return JSON.parse(raw);
  } catch {
    // Tolerate legacy unquoted string (see lthcs-watchlists.js comment).
    return raw;
  }
}

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

const state = {
  snapshot: null, // raw snapshot JSON
  universe: null, // raw universe JSON
  watchlists: {}, // name -> string[]
  activeWatchlistName: null, // string|null
  scope: 'all', // 'all'|'djia'|'sp-100'|'nasdaq-100'|'wl:<name>'|'wl:active'
  historyCache: new Map(), // ticker -> Array<{date, score, band}>
};

// ---------------------------------------------------------------------------
// Universe filtering
// ---------------------------------------------------------------------------

/**
 * Build the set of tickers in scope given the chip selection.
 *
 * Returns:
 *   - Set<string>  when the scope narrows the universe.
 *   - null         when scope=all (no filtering).
 *
 * Falls back to null on missing-data conditions (e.g. universe.json failed
 * to load) so the page still renders something useful instead of breaking.
 */
function tickerSetForScope(scope) {
  if (scope === 'all') return null;

  if (scope.startsWith('wl:')) {
    const name = scope === 'wl:active'
      ? state.activeWatchlistName
      : scope.slice(3);
    if (!name) return new Set();
    const list = state.watchlists[name];
    if (!Array.isArray(list)) return new Set();
    return new Set(list);
  }

  const indexLabel = SCOPE_TO_INDEX[scope];
  if (!indexLabel || !state.universe) return null;
  const out = new Set();
  for (const row of state.universe.tickers || []) {
    if (!row || row.active === false) continue;
    if (Array.isArray(row.index_membership)
        && row.index_membership.includes(indexLabel)) {
      out.add(row.ticker);
    }
  }
  return out;
}

// ---------------------------------------------------------------------------
// Leaderboard computation
// ---------------------------------------------------------------------------

/**
 * Compute the 7 leaderboards from the snapshot's scores array filtered to
 * `tickerSet` (or unfiltered when set is null).
 *
 * Each board entry is { ticker, composite, pillar_score, band }.
 *   - For pillar boards, pillar_score is the pillar value being ranked.
 *   - For composite_top / composite_bottom, pillar_score === composite.
 *
 * Returns the same shape regardless of board, so the renderer is uniform.
 */
function computeBoards(snapshot, tickerSet) {
  const scores = Array.isArray(snapshot?.scores) ? snapshot.scores : [];
  const inScope = tickerSet
    ? scores.filter((s) => tickerSet.has(s.ticker))
    : scores;

  // Composite-sorted base used by both the top + bottom composite boards.
  const byComposite = inScope
    .slice()
    .sort((a, b) => (b.lthcs_score ?? 0) - (a.lthcs_score ?? 0));

  const composite_top = byComposite.slice(0, TOP_N).map((s) => ({
    ticker: s.ticker,
    composite: s.lthcs_score,
    pillar_score: s.lthcs_score,
    band: s.band,
  }));
  const composite_bottom = byComposite
    .slice()
    .reverse()
    .slice(0, TOP_N)
    .map((s) => ({
      ticker: s.ticker,
      composite: s.lthcs_score,
      pillar_score: s.lthcs_score,
      band: s.band,
    }));

  const pillars = {};
  for (const pillarKey of Object.keys(PILLAR_LABEL)) {
    const sorted = inScope
      .slice()
      .sort((a, b) => {
        const av = a.subscores?.[pillarKey] ?? -Infinity;
        const bv = b.subscores?.[pillarKey] ?? -Infinity;
        return bv - av;
      });
    pillars[pillarKey] = sorted.slice(0, TOP_N).map((s) => ({
      ticker: s.ticker,
      composite: s.lthcs_score,
      pillar_score: s.subscores?.[pillarKey],
      band: s.band,
    }));
  }

  return { composite_top, pillars, composite_bottom };
}

// ---------------------------------------------------------------------------
// History fetching (best-effort — sparkline is decoration, not load-blocking)
// ---------------------------------------------------------------------------

async function fetchHistory(ticker) {
  if (state.historyCache.has(ticker)) {
    return state.historyCache.get(ticker);
  }
  const url = HISTORY_URL_TPL.replace('{TICKER}', encodeURIComponent(ticker));
  try {
    const resp = await fetch(url, { cache: 'no-store' });
    if (!resp.ok) {
      state.historyCache.set(ticker, []);
      return [];
    }
    const json = await resp.json();
    const hist = Array.isArray(json?.history) ? json.history : [];
    state.historyCache.set(ticker, hist);
    return hist;
  } catch {
    state.historyCache.set(ticker, []);
    return [];
  }
}

/**
 * Fetch histories for a list of tickers with bounded concurrency.
 * Returns when every ticker is settled (success or empty).
 */
async function batchFetchHistories(tickers) {
  const queue = tickers.slice();
  async function worker() {
    while (queue.length) {
      const t = queue.shift();
      // eslint-disable-next-line no-await-in-loop
      await fetchHistory(t);
    }
  }
  const workers = Array.from(
    { length: Math.min(HISTORY_CONCURRENCY, queue.length) },
    () => worker(),
  );
  await Promise.all(workers);
}

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------

const BOARD_ORDER = [
  ['composite_top', 'Top 10 by Composite', 'Headline LTHCS score.'],
  ['adoption_momentum', `Top 10 by ${PILLAR_LABEL.adoption_momentum}`,
    'Pillar 1 — analyst breadth, narrative, momentum.'],
  ['institutional_confidence', `Top 10 by ${PILLAR_LABEL.institutional_confidence}`,
    'Pillar 2 — 13F flows, insider activity.'],
  ['financial_evolution', `Top 10 by ${PILLAR_LABEL.financial_evolution}`,
    'Pillar 3 — earnings, margins, balance sheet.'],
  ['thesis_integrity', `Top 10 by ${PILLAR_LABEL.thesis_integrity}`,
    'Pillar 4 — story coherence + execution.'],
  ['des', `Top 10 by ${PILLAR_LABEL.des}`,
    'Pillar 5 — durability, ecosystem, scale.'],
  ['composite_bottom', 'Bottom 10 by Composite',
    'The "trouble" watch list — lowest composites in scope.'],
];

function renderRow(entry, rank, isPillarBoard) {
  const li = document.createElement('li');
  li.className = 'lb-row';

  const rankCell = document.createElement('span');
  rankCell.className = 'lb-row-rank';
  rankCell.textContent = String(rank);
  li.appendChild(rankCell);

  const tickerCell = document.createElement('span');
  tickerCell.className = 'lb-row-ticker';
  tickerCell.textContent = entry.ticker;
  li.appendChild(tickerCell);

  const sparkCell = document.createElement('span');
  sparkCell.className = 'lb-row-spark';
  const hist = state.historyCache.get(entry.ticker);
  if (Array.isArray(hist) && hist.length > 0) {
    const color = bandColorForScore(entry.composite) || 'currentColor';
    const svg = renderSparkline(hist, {
      width: 90,
      height: 18,
      showLastDot: true,
      strokeColor: color,
    });
    sparkCell.appendChild(svg);
  } else {
    const empty = document.createElement('span');
    empty.className = 'lb-row-spark-empty';
    empty.textContent = '—';
    sparkCell.appendChild(empty);
  }
  li.appendChild(sparkCell);

  const scoreCell = document.createElement('span');
  scoreCell.className = 'lb-row-score';
  const v = entry.pillar_score;
  scoreCell.textContent =
    typeof v === 'number' && Number.isFinite(v) ? v.toFixed(1) : '—';
  // Title carries the composite when ranking by a pillar — useful diagnostic.
  if (isPillarBoard
      && typeof entry.composite === 'number'
      && Number.isFinite(entry.composite)) {
    scoreCell.title = `Composite: ${entry.composite.toFixed(1)}`;
  }
  li.appendChild(scoreCell);

  const bandCell = document.createElement('span');
  bandCell.className = 'lb-row-band';
  const bandColor = bandColorForScore(entry.composite);
  if (bandColor) bandCell.style.background = bandColor;
  bandCell.textContent = BAND_LABEL[entry.band] || (entry.band || '');
  li.appendChild(bandCell);

  return li;
}

function renderCard(board, title, sub, entries, isPillarBoard) {
  const article = document.createElement('article');
  article.className = 'lb-card';
  article.dataset.board = board;

  const header = document.createElement('header');
  header.className = 'lb-card-header';
  const h2 = document.createElement('h2');
  h2.className = 'lb-card-title';
  h2.textContent = title;
  header.appendChild(h2);
  const subP = document.createElement('p');
  subP.className = 'lb-card-sub';
  subP.textContent = sub;
  header.appendChild(subP);
  article.appendChild(header);

  if (entries.length === 0) {
    const empty = document.createElement('div');
    empty.className = 'lb-empty';
    empty.textContent = 'No tickers in this scope.';
    article.appendChild(empty);
    return article;
  }

  const ol = document.createElement('ol');
  ol.className = 'lb-rows';
  entries.forEach((entry, idx) => {
    ol.appendChild(renderRow(entry, idx + 1, isPillarBoard));
  });
  article.appendChild(ol);
  return article;
}

function renderGrid(boards) {
  const grid = $('lb-grid');
  if (!grid) return;
  grid.textContent = '';

  for (const [key, title, sub] of BOARD_ORDER) {
    let entries;
    let isPillarBoard = false;
    if (key === 'composite_top') {
      entries = boards.composite_top;
    } else if (key === 'composite_bottom') {
      entries = boards.composite_bottom;
    } else {
      entries = boards.pillars[key] || [];
      isPillarBoard = true;
    }
    grid.appendChild(renderCard(key, title, sub, entries, isPillarBoard));
  }
  show('lb-grid');
}

// ---------------------------------------------------------------------------
// Scope-chip wiring
// ---------------------------------------------------------------------------

function appendWatchlistChips(chipsContainer) {
  // Synthetic "Active" chip — only useful when an active list is set AND has
  // at least one ticker. Otherwise it would silently degrade to an empty
  // scope, which is confusing UI.
  const activeList = state.activeWatchlistName
    ? state.watchlists[state.activeWatchlistName]
    : null;
  if (Array.isArray(activeList) && activeList.length > 0) {
    const chip = document.createElement('button');
    chip.type = 'button';
    chip.className = 'lthcs-chip';
    chip.dataset.scope = 'wl:active';
    chip.setAttribute('aria-pressed', 'false');
    chip.textContent = `★ Active: ${state.activeWatchlistName}`;
    chip.title = `${activeList.length} tickers`;
    chipsContainer.appendChild(chip);
  }

  for (const [name, tickers] of Object.entries(state.watchlists)) {
    if (!Array.isArray(tickers) || tickers.length === 0) continue;
    const chip = document.createElement('button');
    chip.type = 'button';
    chip.className = 'lthcs-chip';
    chip.dataset.scope = `wl:${name}`;
    chip.setAttribute('aria-pressed', 'false');
    chip.textContent = name;
    chip.title = `${tickers.length} tickers`;
    chipsContainer.appendChild(chip);
  }
}

function applyScope() {
  const chipsContainer = $('lb-scope-chips');
  if (chipsContainer) {
    for (const chip of chipsContainer.querySelectorAll('.lthcs-chip')) {
      const isActive = chip.dataset.scope === state.scope;
      chip.classList.toggle('is-active', isActive);
      chip.setAttribute('aria-pressed', isActive ? 'true' : 'false');
    }
  }

  const tickerSet = tickerSetForScope(state.scope);
  const totalScores = Array.isArray(state.snapshot?.scores)
    ? state.snapshot.scores.length
    : 0;
  const inScopeCount = tickerSet
    ? Array.from(tickerSet).filter((t) =>
        state.snapshot?.scores?.some((s) => s.ticker === t),
      ).length
    : totalScores;
  setText(
    'lb-scope-count',
    `${inScopeCount} tickers in scope (of ${totalScores})`,
  );

  const boards = computeBoards(state.snapshot, tickerSet);

  // Render once immediately so we don't block on history fetches, then
  // re-render after sparklines come in. Cheap because the snapshot work
  // is already done and the renderer is uniform.
  renderGrid(boards);

  // Collect unique tickers across all 7 boards and warm the history cache.
  const wanted = new Set();
  for (const e of boards.composite_top) wanted.add(e.ticker);
  for (const e of boards.composite_bottom) wanted.add(e.ticker);
  for (const arr of Object.values(boards.pillars)) {
    for (const e of arr) wanted.add(e.ticker);
  }
  const missing = Array.from(wanted).filter(
    (t) => !state.historyCache.has(t),
  );
  if (missing.length === 0) return;
  batchFetchHistories(missing).then(() => {
    // Re-render to attach sparklines. Scope might have changed in the
    // meantime — re-compute against the current scope to stay consistent.
    const tickerSet2 = tickerSetForScope(state.scope);
    const boards2 = computeBoards(state.snapshot, tickerSet2);
    renderGrid(boards2);
  });
}

function wireScopeChips() {
  const chipsContainer = $('lb-scope-chips');
  if (!chipsContainer) return;
  appendWatchlistChips(chipsContainer);
  chipsContainer.addEventListener('click', (ev) => {
    const target = ev.target instanceof HTMLElement
      ? ev.target.closest('[data-scope]')
      : null;
    if (!target) return;
    const next = target.dataset.scope;
    if (!next || next === state.scope) return;
    state.scope = next;
    lsSet(SCOPE_STORAGE_KEY, next);
    applyScope();
  });
}

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------

function renderError(message) {
  hide('lb-loading');
  hide('lb-grid');
  const node = $('lb-error');
  if (!node) return;
  node.textContent = message;
  node.classList.remove('hidden');
}

async function load() {
  // 1) Pull the snapshot (load-blocking).
  try {
    const resp = await fetch(SNAPSHOT_URL, { cache: 'no-store' });
    if (!resp.ok) throw new Error(`HTTP ${resp.status} fetching ${SNAPSHOT_URL}`);
    state.snapshot = await resp.json();
  } catch (err) {
    renderError(
      `Could not load latest snapshot: ${err && err.message ? err.message : err}. ` +
        'The site may be mid-deploy — try again in a minute.',
    );
    return;
  }

  // 2) Pull the universe (best-effort — needed only for DJIA/S&P/NASDAQ
  //    chips, all of which silently degrade to "all" without it).
  try {
    const resp = await fetch(UNIVERSE_URL, { cache: 'no-store' });
    if (resp.ok) state.universe = await resp.json();
  } catch {
    /* keep going */
  }

  // 3) Read watchlists out of localStorage (best-effort).
  state.watchlists = loadWatchlists();
  state.activeWatchlistName = loadActiveWatchlistName();

  // 4) Initial scope: prefer last-used, fall back to 'all'.
  const persisted = lsGet(SCOPE_STORAGE_KEY);
  if (persisted
      && (persisted === 'all'
          || persisted in SCOPE_TO_INDEX
          || persisted.startsWith('wl:'))) {
    state.scope = persisted;
  }

  // 5) Stamp header meta.
  setText('lb-snapshot-date', state.snapshot.calc_date || '—');

  // 6) Wire chips + render.
  hide('lb-loading');
  wireScopeChips();
  applyScope();
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', load);
} else {
  load();
}
