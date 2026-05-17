// =============================================================================
// LTHCS Sector Heatmap — vanilla ES2020 module
//
// Sibling page to the main /lthcs/ card view. Renders a sector-grouped equal-cell
// grid of every ticker in the latest LTHCS snapshot, colored by band. Click a
// cell to open the card view with the ticker preselected via URL fragment.
//
// Data paths (relative to /lthcs/heatmap/index.html):
//   ../../data/lthcs/snapshots/index.json
//   ../../data/lthcs/snapshots/<latest>.json
//   ../../data/lthcs/universe.json
// =============================================================================

// ----- Constants -------------------------------------------------------------

// Map universe.json `index_membership` strings → short DOM keys.
// Mirrors the mapping in ../lthcs-tab.js so chip semantics stay aligned.
const INDEX_KEY_NORMALIZE = {
  'DJIA': 'djia',
  'NASDAQ-100': 'nasdaq-100',
  'S&P 100': 'sp-100',
  'S&P 500': 'sp-500',
};

const BAND_KEYS = ['elite', 'high', 'constructive', 'monitor', 'weakening', 'review'];

// Path resolution: this module sits in /lthcs/heatmap/, snapshots in /data/lthcs/.
const DATA_INDEX_URL = '../../data/lthcs/snapshots/index.json';
const UNIVERSE_URL = '../../data/lthcs/universe.json';
const snapshotUrlFor = (date) => `../../data/lthcs/snapshots/${date}.json`;

// Bust caches so a fresh deploy is picked up without hard-refresh.
const cacheBust = () => `?t=${Date.now()}`;

// ----- DOM helpers -----------------------------------------------------------

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

function show(el) { if (el) el.classList.remove('hidden'); }
function hide(el) { if (el) el.classList.add('hidden'); }

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

// ----- State -----------------------------------------------------------------

const state = {
  snapshot: null,           // { calc_date, scores: [...] }
  universe: null,           // { tickers: [...] }
  universeByTicker: {},     // ticker -> universe entry
  enrichedScores: [],       // merged rows: { ticker, score, band, sector, indexKeys: Set, name }
  activeFilters: {
    index: 'all',
    band: 'all',
  },
};

// ----- Fetch -----------------------------------------------------------------

async function fetchJson(url) {
  const res = await fetch(url + cacheBust(), { cache: 'no-store' });
  if (!res.ok) throw new Error(`Failed to fetch ${url}: ${res.status}`);
  return res.json();
}

async function loadData() {
  const index = await fetchJson(DATA_INDEX_URL);
  if (!index || !index.latest) throw new Error('Snapshot index missing `latest`');
  const [snapshot, universe] = await Promise.all([
    fetchJson(snapshotUrlFor(index.latest)),
    fetchJson(UNIVERSE_URL),
  ]);
  return { snapshot, universe };
}

// ----- Enrichment ------------------------------------------------------------

function buildUniverseIndex(universe) {
  const byTicker = {};
  const tickers = Array.isArray(universe?.tickers) ? universe.tickers : [];
  for (const t of tickers) {
    if (t && t.ticker) byTicker[t.ticker] = t;
  }
  return byTicker;
}

function enrichScores(snapshot, universeByTicker) {
  const scores = Array.isArray(snapshot?.scores) ? snapshot.scores : [];
  const out = [];
  for (const row of scores) {
    if (!row || !row.ticker) continue;
    const uni = universeByTicker[row.ticker] || {};
    const indexMembership = Array.isArray(uni.index_membership) ? uni.index_membership : [];
    const indexKeys = new Set();
    for (const im of indexMembership) {
      const k = INDEX_KEY_NORMALIZE[im];
      if (k) indexKeys.add(k);
    }
    out.push({
      ticker: row.ticker,
      score: Number(row.lthcs_score),
      band: String(row.band || '').toLowerCase(),
      sector: uni.sector || row.sector || 'Unknown',
      name: uni.name || row.ticker,
      indexKeys,
    });
  }
  return out;
}

// ----- Filtering -------------------------------------------------------------

function applyFilters(rows, filters) {
  return rows.filter((r) => {
    if (filters.index !== 'all' && !r.indexKeys.has(filters.index)) return false;
    if (filters.band !== 'all' && r.band !== filters.band) return false;
    return true;
  });
}

// ----- Sector grouping -------------------------------------------------------

function groupBySector(rows) {
  const groups = new Map();
  for (const r of rows) {
    const key = r.sector || 'Unknown';
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(r);
  }
  // Sort rows inside each sector: descending score, ties by ticker.
  for (const list of groups.values()) {
    list.sort((a, b) => {
      const sa = Number.isFinite(a.score) ? a.score : -Infinity;
      const sb = Number.isFinite(b.score) ? b.score : -Infinity;
      if (sb !== sa) return sb - sa;
      return a.ticker.localeCompare(b.ticker);
    });
  }
  // Sort sectors: count desc, then name asc.
  const sectors = Array.from(groups.entries()).map(([name, list]) => ({
    name,
    rows: list,
    count: list.length,
    avgScore: list.length
      ? list.reduce((acc, r) => acc + (Number.isFinite(r.score) ? r.score : 0), 0) / list.length
      : 0,
  }));
  sectors.sort((a, b) => {
    if (b.count !== a.count) return b.count - a.count;
    return a.name.localeCompare(b.name);
  });
  return sectors;
}

// ----- Render ----------------------------------------------------------------

function renderCell(row) {
  const band = BAND_KEYS.includes(row.band) ? row.band : '';
  const ticker = escapeHtml(row.ticker);
  const score = Number.isFinite(row.score) ? row.score.toFixed(1) : '—';
  const title = `${escapeHtml(row.name)} · ${escapeHtml(row.sector)} · ${score} (${escapeHtml(row.band || 'unknown')})`;
  // Cross-link to card view; open in new tab. Fragment-based ticker hint for a
  // future enhancement in the main tab (it will land on the cards page today).
  const href = `../#ticker=${encodeURIComponent(row.ticker)}`;
  return (
    `<a class="hm-cell" data-band="${escapeHtml(band)}" href="${href}" target="_blank" rel="noopener" title="${title}">` +
      `<span class="hm-cell-ticker">${ticker}</span>` +
      `<span class="hm-cell-score">${score}</span>` +
    `</a>`
  );
}

function renderSector(sector) {
  const avg = sector.avgScore.toFixed(1);
  const cells = sector.rows.map(renderCell).join('');
  return (
    `<div class="hm-sector">` +
      `<div class="hm-sector-header">` +
        `<span class="hm-sector-name">${escapeHtml(sector.name)}</span>` +
        `<span class="hm-sector-meta">${sector.count} ${sector.count === 1 ? 'name' : 'names'} &middot; avg ${avg}</span>` +
      `</div>` +
      `<div class="hm-sector-cells">${cells}</div>` +
    `</div>`
  );
}

function renderAll() {
  const gridEl = $('#hm-grid');
  const emptyEl = $('#hm-empty');
  if (!gridEl) return;

  const filtered = applyFilters(state.enrichedScores, state.activeFilters);
  if (filtered.length === 0) {
    hide(gridEl);
    show(emptyEl);
    gridEl.innerHTML = '';
    return;
  }
  hide(emptyEl);
  const sectors = groupBySector(filtered);
  gridEl.innerHTML = sectors.map(renderSector).join('');
  show(gridEl);
}

// ----- Chip UI ---------------------------------------------------------------

function bindChipGroup(groupEl) {
  if (!groupEl) return;
  groupEl.addEventListener('click', (ev) => {
    const btn = ev.target.closest('.lthcs-chip');
    if (!btn || !groupEl.contains(btn)) return;
    const filter = btn.dataset.filter;
    const value = btn.dataset.value;
    if (!filter || !value) return;
    if (state.activeFilters[filter] === value) return;
    state.activeFilters[filter] = value;
    // Toggle visual active state within this group only.
    for (const sibling of $$('.lthcs-chip', groupEl)) {
      sibling.classList.toggle('is-active', sibling === btn);
    }
    renderAll();
  });
}

// ----- About modal -----------------------------------------------------------

function bindAboutModal() {
  const modal = $('#hm-about-modal');
  const openBtn = $('#hm-about-btn');
  if (!modal || !openBtn) return;

  const open = () => {
    modal.classList.remove('hidden');
    modal.removeAttribute('hidden');
    document.body.style.overflow = 'hidden';
  };
  const close = () => {
    modal.classList.add('hidden');
    modal.setAttribute('hidden', '');
    document.body.style.overflow = '';
  };

  openBtn.addEventListener('click', open);
  modal.addEventListener('click', (ev) => {
    const t = ev.target;
    if (t instanceof HTMLElement && t.dataset.close === '1') close();
  });
  document.addEventListener('keydown', (ev) => {
    if (ev.key === 'Escape' && !modal.classList.contains('hidden')) close();
  });
}

// ----- Status panes ----------------------------------------------------------

function showError() {
  hide($('#hm-loading'));
  hide($('#hm-empty'));
  hide($('#hm-grid'));
  show($('#hm-error'));
}

function hideStatusPanes() {
  hide($('#hm-loading'));
  hide($('#hm-error'));
  hide($('#hm-empty'));
}

// ----- Init ------------------------------------------------------------------

async function init() {
  bindChipGroup($('#hm-filter-index'));
  bindChipGroup($('#hm-filter-band'));
  bindAboutModal();

  const refreshBtn = $('#hm-error-refresh');
  if (refreshBtn) refreshBtn.addEventListener('click', () => {
    hide($('#hm-error'));
    show($('#hm-loading'));
    init().catch(showError);
  });

  try {
    const { snapshot, universe } = await loadData();
    state.snapshot = snapshot;
    state.universe = universe;
    state.universeByTicker = buildUniverseIndex(universe);
    state.enrichedScores = enrichScores(snapshot, state.universeByTicker);

    const stamp = $('#hm-last-updated');
    if (stamp) stamp.textContent = formatDate(snapshot.calc_date);

    hideStatusPanes();
    renderAll();
  } catch (err) {
    console.error('[lthcs-heatmap] load failed', err);
    showError();
  }
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}
