// lthcs-coverage.js
// Universe size and per-pillar coverage, computed from data/lthcs/universe.json
// and the latest snapshot, so the header subtitle and the About modal never
// carry a hard-coded ticker count or a dated coverage table.
//
// The card view (lthcs-tab.js) already fetches both files; it hands them over
// with setPageData() so nothing is downloaded twice. Any other page that opens
// the About modal falls back to fetching them here. URLs resolve against this
// module, like lthcs-bands.js and lthcs-files.js, so the module works from
// every path lthcs_tab/ is staged at.

'use strict';

export const PILLAR_KEYS = [
  'adoption_momentum',
  'institutional_confidence',
  'financial_evolution',
  'thesis_integrity',
  'des',
];

const DATA_ROOT = new URL('../data/lthcs/', import.meta.url);
const UNIVERSE_URL = new URL('universe.json', DATA_ROOT).href;
const SNAPSHOT_INDEX_URL = new URL('snapshots/index.json', DATA_ROOT).href;

let handedOver = null;
let fetched = null;

/** The card view passes what it already loaded ({universe, snapshot}). */
export function setPageData(data) {
  if (data && (data.universe || data.snapshot)) handedOver = data;
}

async function getJSON(url) {
  const res = await fetch(url, { cache: 'no-cache' });
  if (!res.ok) throw new Error(`${url} -> ${res.status}`);
  return res.json();
}

async function fetchPageData() {
  const [universe, snapshot] = await Promise.all([
    getJSON(UNIVERSE_URL).catch(() => null),
    getJSON(SNAPSHOT_INDEX_URL)
      .then((idx) => (idx && idx.latest
        ? getJSON(new URL(`snapshots/${idx.latest}.json`, DATA_ROOT).href)
        : null))
      .catch(() => null),
  ]);
  return { universe, snapshot };
}

/** Resolves (never rejects) to {universe, snapshot}; either may be null. */
export function pageData() {
  if (handedOver) return Promise.resolve(handedOver);
  if (!fetched) fetched = fetchPageData();
  return fetched;
}

/** Number of active tickers in universe.json, or null when unknown. */
export function activeCount(universe) {
  const rows = universe && Array.isArray(universe.tickers) ? universe.tickers : null;
  if (!rows || !rows.length) return null;
  return rows.filter((t) => t && t.active).length;
}

/**
 * Per-pillar coverage of the latest snapshot:
 *   { calcDate, scored, pillars: {key: n}, flags: [[flag, n], ...] }
 * where pillars[key] counts scored names with a value for that pillar (a
 * null sub-score means the pillar was dropped and its weight spread over the
 * rest). Returns null when the snapshot is missing or empty.
 */
export function snapshotCoverage(snapshot) {
  const rows = snapshot && Array.isArray(snapshot.scores) ? snapshot.scores : null;
  if (!rows || !rows.length) return null;
  const pillars = Object.fromEntries(PILLAR_KEYS.map((k) => [k, 0]));
  const flags = new Map();
  for (const row of rows) {
    const subs = (row && row.subscores) || {};
    for (const k of PILLAR_KEYS) {
      if (typeof subs[k] === 'number' && Number.isFinite(subs[k])) pillars[k] += 1;
    }
    for (const f of (row && row.data_quality_flags) || []) {
      flags.set(f, (flags.get(f) || 0) + 1);
    }
  }
  return {
    calcDate: snapshot.calc_date || null,
    scored: rows.length,
    pillars,
    flags: [...flags.entries()].sort((a, b) => b[1] - a[1]),
  };
}
