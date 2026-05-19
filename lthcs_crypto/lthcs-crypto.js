/* =========================================================================
   LTHCS Crypto — vanilla-JS surface for BTC / ETH / SOL daily scores.

   Read-only consumer of files under ../data/lthcs/. Renders:
     - Per-asset composite cards (score, band, drift)
     - Pillar breakdown table (sub_score / weight / contribution)
     - Thesis Integrity call line (funding-rate + L/S normalcy)

   Defensive bootstrap:
     1. Try a snapshot index at data/lthcs/snapshots_crypto/index.json.
     2. If that's missing, probe the last 14 days for a <date>.json file.
     3. If still nothing, render the empty-state with the universe roster
        from data/lthcs/crypto_universe.json so the page is informative
        even before the daily runner ships a snapshot.

   Mirrors the defensive patterns in lthcs_backtest/lthcs-backtest.js
   (every fetch goes through tryFetch and returns null on 404).
   ========================================================================= */

const DATA_ROOT = '../data/lthcs';
const SNAPSHOT_DIR = `${DATA_ROOT}/snapshots_crypto`;
const UNIVERSE_PATH = `${DATA_ROOT}/crypto_universe.json`;
const PROBE_DAYS = 14;

const PILLAR_ORDER = [
  ['adoption_momentum', 'Adoption'],
  ['institutional_confidence', 'Institutional'],
  ['financial_evolution', 'Financial'],
  ['thesis_integrity', 'Thesis'],
  ['des', 'DES'],
];

const BAND_TOKENS = {
  elite: { color: 'var(--band-elite)', label: 'Elite' },
  high_confidence: { color: 'var(--band-high)', label: 'High' },
  constructive: { color: 'var(--band-constructive)', label: 'Constructive' },
  monitor: { color: 'var(--band-monitor)', label: 'Monitor' },
  weakening: { color: 'var(--band-weakening)', label: 'Weakening' },
  review: { color: 'var(--band-review)', label: 'Review' },
};

/* ----- DOM helpers ----------------------------------------------------- */
function $(id) { return document.getElementById(id); }
function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') node.className = v;
    else if (k === 'text') node.textContent = v;
    else if (k === 'style') node.setAttribute('style', v);
    else node.setAttribute(k, v);
  }
  for (const c of (Array.isArray(children) ? children : [children])) {
    if (c == null) continue;
    node.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
  }
  return node;
}

/* ----- Fetch with graceful 404 ---------------------------------------- */
async function tryFetch(url) {
  try {
    const r = await fetch(url, { cache: 'no-cache' });
    if (!r.ok) return null;
    return await r.json();
  } catch {
    return null;
  }
}

/* ----- Date helpers --------------------------------------------------- */
function isoDateUTC(d) {
  return `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, '0')}-${String(d.getUTCDate()).padStart(2, '0')}`;
}
function recentDates(n) {
  const out = [];
  const today = new Date();
  for (let i = 0; i < n; i++) {
    const d = new Date(today);
    d.setUTCDate(today.getUTCDate() - i);
    out.push(isoDateUTC(d));
  }
  return out;
}

/* ----- Number formatters --------------------------------------------- */
function fmtScore(x) {
  if (x == null || Number.isNaN(x)) return null;
  return Number(x).toFixed(1);
}
function fmtWeight(x) {
  if (x == null || Number.isNaN(x)) return 'n/a';
  return `${(Number(x) * 100).toFixed(0)}%`;
}
function fmtDrift(x) {
  if (x == null || Number.isNaN(x)) return null;
  const v = Number(x);
  const sign = v > 0 ? '+' : '';
  return `${sign}${v.toFixed(1)}`;
}
function driftClass(x) {
  if (x == null || Number.isNaN(x)) return 'flat';
  const v = Number(x);
  if (v > 0.2) return 'pos';
  if (v < -0.2) return 'neg';
  return 'flat';
}

/* ----- Snapshot loader ------------------------------------------------ */
async function loadLatestSnapshot() {
  // Prefer an index if the producer ever ships one.
  const idx = await tryFetch(`${SNAPSHOT_DIR}/index.json`);
  if (idx && Array.isArray(idx.dates) && idx.dates.length) {
    const datesDesc = [...idx.dates].sort().reverse();
    for (const d of datesDesc) {
      const snap = await tryFetch(`${SNAPSHOT_DIR}/${d}.json`);
      if (snap) return { date: d, snap };
    }
  }
  // Fall back: probe the most recent N days.
  for (const d of recentDates(PROBE_DAYS)) {
    const snap = await tryFetch(`${SNAPSHOT_DIR}/${d}.json`);
    if (snap) return { date: d, snap };
  }
  return null;
}

/* ----- Renderers ------------------------------------------------------ */
function renderEmpty(universe) {
  const host = $('lcry-universe');
  host.innerHTML = '';
  const assets = (universe && Array.isArray(universe.assets)) ? universe.assets : [];
  if (!assets.length) {
    host.appendChild(el('span', { class: 'lcry-note', text: 'No universe roster on disk.' }));
    return;
  }
  for (const a of assets) {
    host.appendChild(el('span', { class: 'lcry-universe-chip' }, [
      el('strong', { text: String(a.symbol || '').toUpperCase() }),
      el('span', { text: a.name || '' }),
      el('span', { text: a.weight_profile ? `· ${a.weight_profile}` : '' }),
    ]));
  }
}

function bandColor(bandKey) {
  return (BAND_TOKENS[bandKey] || {}).color || 'var(--text-tertiary)';
}
function bandLabel(bandKey) {
  return (BAND_TOKENS[bandKey] || {}).label || (bandKey || 'n/a');
}

function renderCards(rows) {
  const host = $('lcry-cards');
  host.innerHTML = '';
  for (const row of rows) {
    const score = fmtScore(row.lthcs_score);
    const color = bandColor(row.band);
    const card = el('div', {
      class: 'lcry-card',
      style: `--lcry-band: ${color};`,
    });
    card.appendChild(el('div', { class: 'lcry-card-head' }, [
      el('span', { class: 'lcry-card-symbol', text: String(row.ticker || '').toUpperCase() }),
      el('span', { class: 'lcry-card-name', text: row.maturity_stage || '' }),
    ]));
    const scoreNode = el('div', { class: 'lcry-card-score' });
    if (score == null) {
      scoreNode.appendChild(el('span', { class: 'lcry-na', text: 'n/a' }));
    } else {
      scoreNode.textContent = score;
    }
    card.appendChild(scoreNode);
    card.appendChild(el('span', {
      class: 'lcry-card-band',
      style: `background: ${color};`,
      text: bandLabel(row.band),
    }));
    // Drift row — 1d / 7d / 30d.
    const drifts = el('div', { class: 'lcry-card-drifts' });
    for (const key of ['drift_1d', 'drift_7d', 'drift_30d']) {
      const val = fmtDrift(row[key]);
      if (val == null) continue;
      drifts.appendChild(el('span', { class: 'lcry-card-drift' }, [
        el('span', { text: key.replace('drift_', 'Δ') }),
        el('span', { class: `lcry-card-drift-val ${driftClass(row[key])}`, text: val }),
      ]));
    }
    card.appendChild(drifts);
    host.appendChild(card);
  }
}

function renderPillars(rows) {
  const host = $('lcry-pillars');
  host.innerHTML = '';
  for (const row of rows) {
    const wrap = el('div', { class: 'lcry-pillar-asset' });
    wrap.appendChild(el('div', { class: 'lcry-pillar-asset-head' }, [
      el('span', { class: 'lcry-pillar-asset-symbol', text: String(row.ticker || '').toUpperCase() }),
      el('span', {
        class: 'lcry-pillar-asset-profile',
        text: `profile: ${row.maturity_stage || 'n/a'}`,
      }),
    ]));
    const table = el('table', { class: 'lcry-pillar-table' });
    const thead = el('thead', {}, el('tr', {}, [
      el('th', { text: 'Pillar' }),
      el('th', { text: 'Sub-score' }),
      el('th', { text: 'Weight' }),
      el('th', { text: 'Contribution' }),
      el('th', { text: '' }),
    ]));
    table.appendChild(thead);
    const tbody = el('tbody');
    const subscores = row.subscores || {};
    const docWeights = row.weights_used || [];
    const effWeights = row.effective_weights || [];
    const contribs = row.weighted_components || [];
    const dropped = new Set(row.dropped_pillars || []);
    PILLAR_ORDER.forEach(([key, label], i) => {
      const isDropped = dropped.has(key);
      const sub = subscores[key];
      const w = effWeights[i] != null ? effWeights[i] : docWeights[i];
      const c = contribs[i];
      const tr = el('tr', isDropped ? { class: 'lcry-dropped' } : {});
      tr.appendChild(el('td', { text: label + (isDropped ? ' (dropped)' : '') }));
      tr.appendChild(el('td', { text: isDropped ? 'n/a' : (fmtScore(sub) ?? 'n/a') }));
      tr.appendChild(el('td', { text: fmtWeight(w) }));
      tr.appendChild(el('td', { text: c != null ? Number(c).toFixed(1) : 'n/a' }));
      // Sub-score bar (0-100 scale).
      const barCell = el('td', { class: 'lcry-bar-cell' });
      if (!isDropped && sub != null && !Number.isNaN(sub)) {
        const track = el('div', { class: 'lcry-bar-track' });
        const pct = Math.max(0, Math.min(100, Number(sub)));
        track.appendChild(el('div', {
          class: 'lcry-bar-fill',
          style: `width: ${pct}%;`,
        }));
        barCell.appendChild(track);
      }
      tr.appendChild(barCell);
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    wrap.appendChild(table);
    host.appendChild(wrap);
  }
}

function thesisCall(row) {
  // Surfaces the Thesis-Integrity sub-score as a plain-English line.
  // Works whether Phase 2 wired real funding/LS or Phase 1 left it neutral.
  if ((row.dropped_pillars || []).includes('thesis_integrity')) {
    return { cls: 'dropped', text: 'data unavailable — pillar dropped, weights renormalized' };
  }
  const sub = (row.subscores || {}).thesis_integrity;
  if (sub == null || Number.isNaN(sub)) return { cls: 'dropped', text: 'no thesis sub-score' };
  const v = Number(sub);
  if (v >= 75) return { cls: 'neutral', text: 'funding + L/S near neutral (healthy)' };
  if (v >= 55) return { cls: 'neutral', text: 'thesis intact, mild positioning skew' };
  if (v >= 40) return { cls: 'stretched', text: 'positioning stretched — watch for unwind' };
  if (v >= 20) return { cls: 'euphoric', text: 'funding euphoric or L/S extreme' };
  return { cls: 'panicked', text: 'funding/LS at saturation — capitulation or mania' };
}

function renderThesis(rows) {
  const host = $('lcry-thesis');
  host.innerHTML = '';
  for (const row of rows) {
    const call = thesisCall(row);
    const sub = (row.subscores || {}).thesis_integrity;
    const detail = sub == null ? 'sub-score n/a' : `thesis sub-score: ${fmtScore(sub)}`;
    host.appendChild(el('div', { class: 'lcry-thesis-row' }, [
      el('span', { class: 'lcry-thesis-symbol', text: String(row.ticker || '').toUpperCase() }),
      el('span', { class: `lcry-thesis-call ${call.cls}`, text: call.text }),
      el('span', { class: 'lcry-thesis-detail', text: detail }),
    ]));
  }
}

/* ----- Bootstrap ------------------------------------------------------ */
async function main() {
  const loading = $('lcry-loading');
  const errBox = $('lcry-error');
  const emptyBox = $('lcry-empty');
  const content = $('lcry-content');
  const genEl = $('lcry-generated');

  let bundle = null;
  try {
    bundle = await loadLatestSnapshot();
  } catch (e) {
    loading.classList.add('hidden');
    errBox.classList.remove('hidden');
    errBox.textContent = `Failed to load crypto snapshots: ${e.message || e}`;
    return;
  }

  if (!bundle) {
    // Empty state — show the universe roster so the page is still useful.
    const universe = await tryFetch(UNIVERSE_PATH);
    loading.classList.add('hidden');
    emptyBox.classList.remove('hidden');
    genEl.textContent = 'n/a';
    renderEmpty(universe);
    return;
  }

  const { date, snap } = bundle;
  const rows = Array.isArray(snap.scores) ? snap.scores : [];
  if (!rows.length) {
    const universe = await tryFetch(UNIVERSE_PATH);
    loading.classList.add('hidden');
    emptyBox.classList.remove('hidden');
    genEl.textContent = date;
    renderEmpty(universe);
    return;
  }

  genEl.textContent = `${date}${snap.model_version ? ` · ${snap.model_version}` : ''}`;
  renderCards(rows);
  renderPillars(rows);
  renderThesis(rows);
  loading.classList.add('hidden');
  content.classList.remove('hidden');
}

main().catch((e) => {
  const loading = $('lcry-loading');
  const errBox = $('lcry-error');
  if (loading) loading.classList.add('hidden');
  if (errBox) {
    errBox.classList.remove('hidden');
    errBox.textContent = `Unexpected error: ${e.message || e}`;
  }
});
