// lthcs-diff.js
// Snapshot diff view at /lthcs/diff/ (combined #1 yesterday-vs-today + #8 any-two-dates).
// Vanilla ES module — no framework. Mirrors the existing lthcs_tab/ pattern
// (relative fetch to ../data/lthcs/...) so it Just Works on the deployed
// Pages mirror and locally.
//
// URL params (both optional):
//   ?from=YYYY-MM-DD&to=YYYY-MM-DD
// If params are present and the dates exist, they fill the pickers and the
// view jumps straight to that comparison. Otherwise the default is the two
// most recent snapshots from the index.
//
// The diff math here is intentionally kept in lock-step with
// scripts/lthcs_diff_snapshots.py — any change should land in both, and the
// Python test suite is the source of truth.

'use strict';

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const SNAPSHOTS_BASE = '../data/lthcs/snapshots';
const INDEX_URL = `${SNAPSHOTS_BASE}/index.json`;

// Band order, strongest first. Must match BAND_ORDER in lthcs_diff_snapshots.py.
const BAND_ORDER = [
  'elite',
  'high_confidence',
  'constructive',
  'monitor',
  'weakening',
  'review',
];

const PILLAR_KEYS = [
  'adoption_momentum',
  'institutional_confidence',
  'financial_evolution',
  'thesis_integrity',
  'des',
];

// Short labels for the per-pillar badges (5-char ceiling so the row fits).
const PILLAR_SHORT = {
  adoption_momentum: 'Adopt',
  institutional_confidence: 'Inst',
  financial_evolution: 'Fin',
  thesis_integrity: 'Thes',
  des: 'DES',
};

// Anything with |delta| >= 5 gets the "large mover" pill treatment.
const LARGE_DELTA = 5;

// ---------------------------------------------------------------------------
// DOM refs
// ---------------------------------------------------------------------------

const $ = (sel) => document.querySelector(sel);

const els = {
  fromSel: null,
  toSel: null,
  sortSel: null,
  swapBtn: null,
  chips: null,
  kpiUp: null,
  kpiDown: null,
  kpiShift: null,
  kpiFlat: null,
  status: null,
  loading: null,
  error: null,
  tableWrap: null,
  tbody: null,
};

function bindRefs() {
  els.fromSel = $('#lthcs-diff-from');
  els.toSel = $('#lthcs-diff-to');
  els.sortSel = $('#lthcs-diff-sort');
  els.swapBtn = $('#lthcs-diff-swap');
  els.chips = document.querySelectorAll('.lthcs-diff-chips .lthcs-chip');
  els.kpiUp = $('[data-kpi-count="up"]');
  els.kpiDown = $('[data-kpi-count="down"]');
  els.kpiShift = $('[data-kpi-count="shift"]');
  els.kpiFlat = $('[data-kpi-count="flat"]');
  els.status = $('#lthcs-diff-status');
  els.loading = $('#lthcs-diff-loading');
  els.error = $('#lthcs-diff-error');
  els.tableWrap = $('#lthcs-diff-table-wrap');
  els.tbody = $('#lthcs-diff-tbody');
}

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

const state = {
  availableDates: [],     // strings, newest first
  snapshotCache: new Map(), // date -> snapshot dict
  diff: null,             // last computed diff result
  sort: 'absdelta-desc',
};

// ---------------------------------------------------------------------------
// Fetch helpers
// ---------------------------------------------------------------------------

async function fetchIndex() {
  const resp = await fetch(INDEX_URL, { cache: 'no-store' });
  if (!resp.ok) {
    throw new Error(`Snapshot index missing (HTTP ${resp.status})`);
  }
  const j = await resp.json();
  // index.json shape: { model_version, dates: [...] }
  const dates = Array.isArray(j?.dates) ? j.dates.slice() : [];
  // Defensive: ensure newest-first ordering.
  dates.sort((a, b) => (a < b ? 1 : a > b ? -1 : 0));
  return dates;
}

async function fetchSnapshot(date) {
  if (state.snapshotCache.has(date)) return state.snapshotCache.get(date);
  const resp = await fetch(`${SNAPSHOTS_BASE}/${date}.json`, { cache: 'no-store' });
  if (!resp.ok) {
    const err = new Error(`No snapshot for ${date} yet`);
    err.code = 'MISSING_SNAPSHOT';
    err.missingDate = date;
    throw err;
  }
  const snap = await resp.json();
  state.snapshotCache.set(date, snap);
  return snap;
}

// ---------------------------------------------------------------------------
// Diff math — mirror of scripts/lthcs_diff_snapshots.py
// ---------------------------------------------------------------------------

function bandRank(band) {
  const i = BAND_ORDER.indexOf(band);
  return i === -1 ? BAND_ORDER.length : i;
}

function tickerIndex(snap) {
  const out = new Map();
  const rows = Array.isArray(snap?.scores) ? snap.scores : [];
  for (const r of rows) {
    if (r && typeof r.ticker === 'string') out.set(r.ticker, r);
  }
  return out;
}

function pillarDelta(a, b, key) {
  const av = a?.subscores?.[key];
  const bv = b?.subscores?.[key];
  if (av == null || bv == null) return null;
  return Math.round((Number(bv) - Number(av)) * 100) / 100;
}

function diffSnapshots(snapA, snapB) {
  const idxA = tickerIndex(snapA);
  const idxB = tickerIndex(snapB);
  const tickers = new Set([...idxA.keys(), ...idxB.keys()]);
  const sortedTickers = [...tickers].sort();

  const rows = [];
  const deltas = [];
  let up = 0, down = 0, flat = 0, inactive = 0, newCt = 0;

  for (const ticker of sortedTickers) {
    const a = idxA.get(ticker);
    const b = idxB.get(ticker);

    if (a && !b) {
      inactive += 1;
      rows.push({
        ticker,
        score_a: a.lthcs_score ?? null,
        score_b: null,
        delta: null,
        band_a: a.band ?? null,
        band_b: null,
        band_change: 'inactive',
        pillar_deltas: {},
        sector: a.sector ?? null,
      });
      continue;
    }

    if (!a && b) {
      newCt += 1;
      rows.push({
        ticker,
        score_a: null,
        score_b: b.lthcs_score ?? null,
        delta: null,
        band_a: null,
        band_b: b.band ?? null,
        band_change: 'new',
        pillar_deltas: {},
        sector: b.sector ?? null,
      });
      continue;
    }

    const sa = Number(a.lthcs_score);
    const sb = Number(b.lthcs_score);
    const delta = (Number.isFinite(sa) && Number.isFinite(sb))
      ? Math.round((sb - sa) * 100) / 100
      : null;
    if (delta != null) deltas.push(delta);

    const bandA = a.band ?? null;
    const bandB = b.band ?? null;
    let bandChange;
    if (bandA === bandB) {
      bandChange = 'same';
      if (delta != null && Math.abs(delta) < 0.05) flat += 1;
    } else {
      const ra = bandRank(bandA);
      const rb = bandRank(bandB);
      if (rb < ra) { bandChange = 'promotion'; up += 1; }
      else if (rb > ra) { bandChange = 'demotion'; down += 1; }
      else bandChange = 'same';
    }

    const pillarDeltas = {};
    for (const key of PILLAR_KEYS) pillarDeltas[key] = pillarDelta(a, b, key);

    rows.push({
      ticker,
      score_a: sa,
      score_b: sb,
      delta,
      band_a: bandA,
      band_b: bandB,
      band_change: bandChange,
      pillar_deltas: pillarDeltas,
      sector: b.sector ?? a.sector ?? null,
    });
  }

  const totalCompared = deltas.length;
  const avgShift = totalCompared
    ? Math.round((deltas.reduce((s, d) => s + d, 0) / totalCompared) * 1000) / 1000
    : 0.0;

  return {
    date_a: snapA?.calc_date ?? null,
    date_b: snapB?.calc_date ?? null,
    tickers: rows,
    summary: {
      tickers_up: up,
      tickers_down: down,
      tickers_unchanged: flat,
      avg_composite_shift: avgShift,
      total_compared: totalCompared,
      tickers_inactive: inactive,
      tickers_new: newCt,
    },
  };
}

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------

function fmtSigned(n, digits = 2) {
  if (n == null || !Number.isFinite(n)) return '—';
  const s = n >= 0 ? '+' : '';
  return `${s}${n.toFixed(digits)}`;
}

function fmtScore(n) {
  if (n == null || !Number.isFinite(n)) return '—';
  return Number(n).toFixed(1);
}

function bandPill(band, override) {
  if (band == null) return '<span class="lthcs-band-pill" data-band="inactive">—</span>';
  const label = override ?? band.replace(/_/g, ' ');
  return `<span class="lthcs-band-pill" data-band="${band}">${label}</span>`;
}

function bandCell(row) {
  if (row.band_change === 'inactive') {
    return `<span class="lthcs-band-pill" data-band="inactive">Inactive</span>
            <span class="lthcs-diff-band-arrow">— was</span>
            ${bandPill(row.band_a)}`;
  }
  if (row.band_change === 'new') {
    return `<span class="lthcs-band-pill" data-band="inactive">New</span>
            <span class="lthcs-diff-band-arrow">&rarr;</span>
            ${bandPill(row.band_b)}`;
  }
  if (row.band_change === 'same') {
    return `<span class="lthcs-diff-band-same">— same (${(row.band_a ?? '').replace(/_/g, ' ')})</span>`;
  }
  return `${bandPill(row.band_a)}
          <span class="lthcs-diff-band-arrow">&rarr;</span>
          ${bandPill(row.band_b)}`;
}

function pillarBadges(pd) {
  if (!pd) return '';
  const out = [];
  for (const key of PILLAR_KEYS) {
    const v = pd[key];
    if (v == null) {
      out.push(
        `<span class="lthcs-pillar-badge is-flat" title="${key} (n/a)">` +
        `<span class="lthcs-pillar-key">${PILLAR_SHORT[key]}</span> —</span>`
      );
      continue;
    }
    let cls = 'is-flat';
    if (v > 0.05) cls = 'is-pos';
    else if (v < -0.05) cls = 'is-neg';
    out.push(
      `<span class="lthcs-pillar-badge ${cls}" title="${key}: ${fmtSigned(v)}">` +
      `<span class="lthcs-pillar-key">${PILLAR_SHORT[key]}</span> ${fmtSigned(v, 1)}</span>`
    );
  }
  return out.join('');
}

function renderKpis(summary) {
  els.kpiUp.textContent = String(summary.tickers_up);
  els.kpiDown.textContent = String(summary.tickers_down);
  const shift = summary.avg_composite_shift;
  els.kpiShift.textContent = fmtSigned(shift, 2);
  els.kpiShift.classList.remove('is-pos', 'is-neg');
  if (shift > 0.005) els.kpiShift.classList.add('is-pos');
  else if (shift < -0.005) els.kpiShift.classList.add('is-neg');
  els.kpiFlat.textContent = String(summary.tickers_unchanged);
}

function sortRows(rows, mode) {
  const arr = rows.slice();
  switch (mode) {
    case 'delta-desc':
      return arr.sort((a, b) => (b.delta ?? -Infinity) - (a.delta ?? -Infinity));
    case 'delta-asc':
      return arr.sort((a, b) => (a.delta ?? Infinity) - (b.delta ?? Infinity));
    case 'band-up':
      return arr.sort((a, b) => {
        const av = a.band_change === 'promotion' ? -1 : 0;
        const bv = b.band_change === 'promotion' ? -1 : 0;
        if (av !== bv) return av - bv;
        return (b.delta ?? 0) - (a.delta ?? 0);
      });
    case 'band-down':
      return arr.sort((a, b) => {
        const av = a.band_change === 'demotion' ? -1 : 0;
        const bv = b.band_change === 'demotion' ? -1 : 0;
        if (av !== bv) return av - bv;
        return (a.delta ?? 0) - (b.delta ?? 0);
      });
    case 'ticker-asc':
      return arr.sort((a, b) => a.ticker.localeCompare(b.ticker));
    case 'absdelta-desc':
    default:
      return arr.sort((a, b) => Math.abs(b.delta ?? 0) - Math.abs(a.delta ?? 0));
  }
}

function renderTable(diff) {
  const rows = sortRows(diff.tickers, state.sort);
  const html = rows.map((r) => {
    const deltaCls = ['lthcs-diff-delta'];
    if (r.delta == null) deltaCls.push('is-flat');
    else if (r.delta > 0.05) deltaCls.push('is-pos');
    else if (r.delta < -0.05) deltaCls.push('is-neg');
    else deltaCls.push('is-flat');
    if (r.delta != null && Math.abs(r.delta) >= LARGE_DELTA) deltaCls.push('is-large');

    const rowCls = ['lthcs-diff-row'];
    if (r.band_change === 'inactive') rowCls.push('is-inactive');

    return `
      <tr class="${rowCls.join(' ')}">
        <td class="lthcs-diff-cell-ticker"><span class="lthcs-diff-ticker">${r.ticker}</span></td>
        <td class="lthcs-diff-score" data-label="Score A">${fmtScore(r.score_a)}</td>
        <td class="lthcs-diff-score" data-label="Score B">${fmtScore(r.score_b)}</td>
        <td class="${deltaCls.join(' ')}" data-label="Δ">${r.delta == null ? '—' : fmtSigned(r.delta)}</td>
        <td class="lthcs-diff-band-cell" data-label="Band">${bandCell(r)}</td>
        <td data-label="Pillars"><div class="lthcs-diff-pillars">${pillarBadges(r.pillar_deltas)}</div></td>
      </tr>
    `;
  }).join('');

  els.tbody.innerHTML = html;
  els.tableWrap.classList.remove('hidden');
}

function setStatus(msg, isWarning = false) {
  els.status.textContent = msg;
  els.status.classList.toggle('is-warning', isWarning);
}

function showError(msg) {
  els.loading.classList.add('hidden');
  els.tableWrap.classList.add('hidden');
  els.error.classList.remove('hidden');
  els.error.textContent = msg;
}

function clearError() {
  els.error.classList.add('hidden');
  els.error.textContent = '';
}

// ---------------------------------------------------------------------------
// Date picker logic
// ---------------------------------------------------------------------------

function fillDateSelects(dates, fromDate, toDate) {
  const buildOptions = (selectedValue) =>
    dates.map((d) => `<option value="${d}"${d === selectedValue ? ' selected' : ''}>${d}</option>`).join('');
  els.fromSel.innerHTML = buildOptions(fromDate);
  els.toSel.innerHTML = buildOptions(toDate);
}

function pickDefaultDates(dates) {
  if (dates.length === 0) return [null, null];
  if (dates.length === 1) return [dates[0], dates[0]];
  // dates is newest-first → [newest, second-newest]
  return [dates[1], dates[0]];
}

function nearestAvailable(dates, target) {
  // Pick the closest date <= target. Fall back to closest overall.
  if (!dates.length) return null;
  const sorted = dates.slice().sort();
  let chosen = null;
  for (const d of sorted) {
    if (d <= target) chosen = d;
    else break;
  }
  if (chosen) return chosen;
  return sorted[0];
}

function quickRange(dates, kind) {
  if (!dates.length) return null;
  const newest = dates[0]; // newest first
  const newestDate = new Date(newest + 'T00:00:00Z');
  let targetMs;
  switch (kind) {
    case 'yesterday':
      // "Yesterday vs Today" → second-newest vs newest
      return [dates[1] ?? dates[0], newest];
    case 'week':
      targetMs = newestDate.getTime() - 7 * 86400000;
      break;
    case 'month':
      targetMs = newestDate.getTime() - 30 * 86400000;
      break;
    default:
      return [dates[1] ?? dates[0], newest];
  }
  const targetDate = new Date(targetMs).toISOString().slice(0, 10);
  const from = nearestAvailable(dates, targetDate);
  return [from, newest];
}

// ---------------------------------------------------------------------------
// URL params
// ---------------------------------------------------------------------------

function readUrlDates() {
  const sp = new URLSearchParams(window.location.search);
  return { from: sp.get('from'), to: sp.get('to') };
}

function writeUrlDates(from, to) {
  const sp = new URLSearchParams(window.location.search);
  sp.set('from', from);
  sp.set('to', to);
  const newUrl = `${window.location.pathname}?${sp.toString()}`;
  window.history.replaceState({}, '', newUrl);
}

// ---------------------------------------------------------------------------
// Main flow
// ---------------------------------------------------------------------------

async function runDiff() {
  clearError();
  const from = els.fromSel.value;
  const to = els.toSel.value;

  if (!from || !to) {
    showError('Pick two snapshot dates.');
    return;
  }
  if (from === to) {
    els.loading.classList.add('hidden');
    els.tableWrap.classList.add('hidden');
    setStatus('Pick two different dates to compare.', true);
    renderKpis({
      tickers_up: 0, tickers_down: 0, tickers_unchanged: 0,
      avg_composite_shift: 0, total_compared: 0,
      tickers_inactive: 0, tickers_new: 0,
    });
    return;
  }

  els.loading.classList.remove('hidden');
  els.tableWrap.classList.add('hidden');
  setStatus(`Loading ${from} -> ${to}…`);

  try {
    const [snapA, snapB] = await Promise.all([fetchSnapshot(from), fetchSnapshot(to)]);
    const diff = diffSnapshots(snapA, snapB);
    state.diff = diff;
    renderKpis(diff.summary);
    renderTable(diff);
    setStatus(
      `${from} → ${to} · ${diff.summary.total_compared} tickers compared` +
      (diff.summary.tickers_inactive ? ` · ${diff.summary.tickers_inactive} inactive` : '') +
      (diff.summary.tickers_new ? ` · ${diff.summary.tickers_new} new` : '')
    );
    writeUrlDates(from, to);
  } catch (err) {
    if (err && err.code === 'MISSING_SNAPSHOT') {
      showError(`No snapshot for ${err.missingDate} yet. Try a different date.`);
      setStatus('', false);
    } else {
      showError(err?.message ?? 'Failed to load snapshots');
    }
  } finally {
    els.loading.classList.add('hidden');
  }
}

function wireEvents() {
  els.fromSel.addEventListener('change', runDiff);
  els.toSel.addEventListener('change', runDiff);
  els.sortSel.addEventListener('change', () => {
    state.sort = els.sortSel.value;
    if (state.diff) renderTable(state.diff);
  });
  els.swapBtn.addEventListener('click', () => {
    const a = els.fromSel.value;
    const b = els.toSel.value;
    els.fromSel.value = b;
    els.toSel.value = a;
    runDiff();
  });
  els.chips.forEach((chip) => {
    chip.addEventListener('click', () => {
      const kind = chip.dataset.quick;
      const pick = quickRange(state.availableDates, kind);
      if (!pick) return;
      const [from, to] = pick;
      if (!from || !to) return;
      els.fromSel.value = from;
      els.toSel.value = to;
      runDiff();
    });
  });
}

async function init() {
  bindRefs();
  wireEvents();

  let dates;
  try {
    dates = await fetchIndex();
  } catch (err) {
    showError('Snapshot index unavailable. Run the LTHCS daily pipeline first.');
    return;
  }
  state.availableDates = dates;
  if (dates.length === 0) {
    showError('No snapshots available yet.');
    return;
  }

  // Resolve initial from/to from URL params, falling back to default.
  const url = readUrlDates();
  let [defaultFrom, defaultTo] = pickDefaultDates(dates);
  let from = url.from && dates.includes(url.from) ? url.from : defaultFrom;
  let to = url.to && dates.includes(url.to) ? url.to : defaultTo;

  fillDateSelects(dates, from, to);
  await runDiff();
}

// Boot.
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}

// Exported for testing-by-eyeball in the browser console.
export { diffSnapshots, BAND_ORDER, PILLAR_KEYS };
