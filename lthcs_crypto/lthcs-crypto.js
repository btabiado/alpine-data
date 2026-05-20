/* =========================================================================
   LTHCS Crypto — vanilla-JS surface for 10-asset daily scores.

   Read-only consumer of files under ../data/lthcs/. Renders:
     - Universe stat strip (avg composite, band distribution, snapshot age)
     - Per-asset composite cards (score, band, drift, sparkline, vs-avg tag)
     - Thesis Integrity call line (funding-rate + L/S normalcy)
     - Pillar breakdown table (sub_score / weight / contribution)
     - Click-to-open detail modal (pillar bars, 30d sparkline, components,
       data-quality chips, drift table)

   Defensive bootstrap:
     1. Try a snapshot index at data/lthcs/snapshots_crypto/index.json.
     2. If that's missing, probe the last 14 days for a <date>.json file.
     3. If still nothing, render the empty-state with the universe roster
        from data/lthcs/crypto_universe.json so the page is informative
        even before the daily runner ships a snapshot.

   Mirrors the defensive patterns in lthcs_backtest/lthcs-backtest.js
   (every fetch goes through tryFetch and returns null on 404).

   Sparkline rendering re-uses ../lthcs_tab/lthcs-sparkline.js so the
   crypto page picks up any future improvements to the equity sparkline
   for free. pages.yml mirrors lthcs_tab/ as a sibling of /lthcs/crypto/.
   ========================================================================= */

import { renderSparkline, bandColorForScore } from '../lthcs_tab/lthcs-sparkline.js';

const DATA_ROOT = '../data/lthcs';
const SNAPSHOT_DIR = `${DATA_ROOT}/snapshots_crypto`;
const UNIVERSE_PATH = `${DATA_ROOT}/crypto_universe.json`;
const HISTORY_DIR = `${DATA_ROOT}/history/by_ticker`;
const PROBE_DAYS = 14;
const CARD_SPARKLINE_DAYS = 14;
const MODAL_SPARKLINE_DAYS = 30;

const PILLAR_ORDER = [
  ['adoption_momentum', 'Adoption'],
  ['institutional_confidence', 'Institutional'],
  ['financial_evolution', 'Financial'],
  ['thesis_integrity', 'Thesis'],
  ['des', 'DES'],
];

const PILLAR_LONG = {
  adoption_momentum: 'Adoption Momentum',
  institutional_confidence: 'Institutional Confidence',
  financial_evolution: 'Financial Evolution',
  thesis_integrity: 'Thesis Integrity',
  des: 'Demand Environment',
};

const BAND_TOKENS = {
  elite: { color: 'var(--band-elite)', label: 'Elite' },
  high_confidence: { color: 'var(--band-high)', label: 'High' },
  constructive: { color: 'var(--band-constructive)', label: 'Constructive' },
  monitor: { color: 'var(--band-monitor)', label: 'Monitor' },
  weakening: { color: 'var(--band-weakening)', label: 'Weakening' },
  review: { color: 'var(--band-review)', label: 'Review' },
};

// Human-readable labels for data_quality_flags surfaced in snapshots.
const FLAG_LABELS = {
  thesis_unavailable: 'Thesis n/a — funding/LS feed missing',
  whale_unavailable: 'Whale cohort n/a',
  revenue_unavailable: 'Miner revenue n/a',
};

/* ----- DOM helpers ----------------------------------------------------- */
function $(id) { return document.getElementById(id); }
function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') node.className = v;
    else if (k === 'text') node.textContent = v;
    else if (k === 'style') node.setAttribute('style', v);
    else if (k.startsWith('on') && typeof v === 'function') node.addEventListener(k.slice(2), v);
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
function daysBetween(isoA, isoB) {
  // Days from isoA (older) to isoB (newer); both 'YYYY-MM-DD'.
  const a = new Date(`${isoA}T00:00:00Z`).getTime();
  const b = new Date(`${isoB}T00:00:00Z`).getTime();
  if (!Number.isFinite(a) || !Number.isFinite(b)) return null;
  return Math.round((b - a) / (24 * 3600 * 1000));
}
function freshnessLabel(snapDate) {
  const today = isoDateUTC(new Date());
  const delta = daysBetween(snapDate, today);
  if (delta == null) return null;
  if (delta <= 0) return { text: 'today', cls: 'fresh' };
  if (delta === 1) return { text: 'yesterday', cls: 'fresh' };
  if (delta <= 3) return { text: `${delta}d ago`, cls: 'recent' };
  if (delta <= 7) return { text: `${delta}d ago`, cls: 'stale' };
  return { text: `${delta}d ago`, cls: 'old' };
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

/* ----- Per-ticker history loader (lazy, cached) ----------------------- */
const _historyCache = new Map();
async function loadHistory(ticker) {
  const key = String(ticker || '').toUpperCase();
  if (_historyCache.has(key)) return _historyCache.get(key);
  const promise = tryFetch(`${HISTORY_DIR}/${key}.json`).then((doc) => {
    if (!doc || !Array.isArray(doc.history)) return [];
    return doc.history;
  });
  _historyCache.set(key, promise);
  return promise;
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

/* ----- Universe stat strip ------------------------------------------- */
function renderStrip(rows, snapDate) {
  const host = $('lcry-strip');
  host.innerHTML = '';

  // Universe average composite.
  const scored = rows.filter((r) => typeof r.lthcs_score === 'number' && !Number.isNaN(r.lthcs_score));
  const avg = scored.length
    ? scored.reduce((s, r) => s + Number(r.lthcs_score), 0) / scored.length
    : null;

  // Band distribution: any of the 6 band keys.
  const bandCounts = {};
  for (const r of rows) {
    const k = r.band || 'n/a';
    bandCounts[k] = (bandCounts[k] || 0) + 1;
  }
  const distinctBands = Object.keys(bandCounts).length;

  // Highest + lowest single asset.
  const sortedDesc = scored.slice().sort((a, b) => Number(b.lthcs_score) - Number(a.lthcs_score));
  const top = sortedDesc[0];
  const bot = sortedDesc[sortedDesc.length - 1];

  const fresh = freshnessLabel(snapDate);

  function tile(label, value, sub) {
    return el('div', { class: 'lcry-tile' }, [
      el('span', { class: 'lcry-tile-label', text: label }),
      el('span', { class: 'lcry-tile-value', text: value }),
      sub ? el('span', { class: 'lcry-tile-sub', text: sub }) : null,
    ]);
  }

  host.appendChild(tile(
    'Universe avg',
    avg == null ? 'n/a' : avg.toFixed(1),
    `${scored.length} of ${rows.length} scored`,
  ));
  host.appendChild(tile(
    'Best',
    top ? String(top.ticker).toUpperCase() : 'n/a',
    top ? `${fmtScore(top.lthcs_score)} · ${bandLabel(top.band)}` : null,
  ));
  host.appendChild(tile(
    'Lowest',
    bot ? String(bot.ticker).toUpperCase() : 'n/a',
    bot ? `${fmtScore(bot.lthcs_score)} · ${bandLabel(bot.band)}` : null,
  ));
  host.appendChild(tile(
    'Bands in play',
    String(distinctBands),
    Object.entries(bandCounts)
      .map(([k, n]) => `${n}× ${bandLabel(k)}`)
      .join(' · '),
  ));

  // Surface freshness in the header too (separate from the stat tile).
  if (fresh) {
    const freshLine = $('lcry-freshness');
    const freshVal = $('lcry-freshness-value');
    if (freshLine && freshVal) {
      freshLine.classList.remove('hidden');
      freshVal.textContent = fresh.text;
      freshVal.className = `lthcs-meta-value lcry-freshness-${fresh.cls}`;
    }
  }

  return { avg };
}

/* ----- Per-asset score cards ----------------------------------------- */
function renderCards(rows, { avg }) {
  const host = $('lcry-cards');
  host.innerHTML = '';
  for (const row of rows) {
    const score = fmtScore(row.lthcs_score);
    const color = bandColor(row.band);
    const card = el('button', {
      class: 'lcry-card',
      type: 'button',
      style: `--lcry-band: ${color};`,
      'aria-label': `Open ${row.ticker} detail`,
      onclick: () => openDetail(row),
    });

    // Head: symbol + maturity tag.
    card.appendChild(el('div', { class: 'lcry-card-head' }, [
      el('span', { class: 'lcry-card-symbol', text: String(row.ticker || '').toUpperCase() }),
      el('span', { class: 'lcry-card-name', text: row.maturity_stage || '' }),
    ]));

    // Score block (with vs-avg pill on the right).
    const scoreRow = el('div', { class: 'lcry-card-score-row' });
    const scoreNode = el('div', { class: 'lcry-card-score' });
    if (score == null) {
      scoreNode.appendChild(el('span', { class: 'lcry-na', text: 'n/a' }));
    } else {
      scoreNode.textContent = score;
    }
    scoreRow.appendChild(scoreNode);

    if (avg != null && typeof row.lthcs_score === 'number') {
      const delta = Number(row.lthcs_score) - Number(avg);
      const sign = delta > 0 ? '+' : '';
      const cls = delta > 0.5 ? 'pos' : delta < -0.5 ? 'neg' : 'flat';
      scoreRow.appendChild(el('span', {
        class: `lcry-card-vsavg ${cls}`,
        title: `vs universe average (${Number(avg).toFixed(1)})`,
        text: `vs avg ${sign}${delta.toFixed(1)}`,
      }));
    }
    card.appendChild(scoreRow);

    // Band chip.
    card.appendChild(el('span', {
      class: 'lcry-card-band',
      style: `background: ${color};`,
      text: bandLabel(row.band),
    }));

    // Sparkline (filled async; placeholder while loading).
    const sparkSlot = el('div', { class: 'lcry-card-spark', 'aria-label': 'score history' });
    card.appendChild(sparkSlot);

    // Drift row — 1d / 7d / 30d.
    const drifts = el('div', { class: 'lcry-card-drifts' });
    for (const [key, label] of [['drift_1d', '1d'], ['drift_7d', '7d'], ['drift_30d', '30d']]) {
      const val = fmtDrift(row[key]);
      if (val == null) continue;
      drifts.appendChild(el('span', { class: 'lcry-card-drift' }, [
        el('span', { class: 'lcry-card-drift-key', text: `Δ${label}` }),
        el('span', { class: `lcry-card-drift-val ${driftClass(row[key])}`, text: val }),
      ]));
    }
    card.appendChild(drifts);

    // Data-quality flags row (compact chips).
    const flags = Array.isArray(row.data_quality_flags) ? row.data_quality_flags : [];
    if (flags.length) {
      const flagBox = el('div', { class: 'lcry-card-flags' });
      for (const f of flags) {
        flagBox.appendChild(el('span', {
          class: 'lcry-card-flag',
          title: FLAG_LABELS[f] || f,
          text: f.replace(/_/g, ' '),
        }));
      }
      card.appendChild(flagBox);
    }

    host.appendChild(card);

    // Lazy-load sparkline so the rest of the page renders without blocking.
    loadHistory(row.ticker).then((history) => {
      const recent = history.slice(0, CARD_SPARKLINE_DAYS);
      const stroke = bandColorForScore(Number(row.lthcs_score)) || 'currentColor';
      const svg = renderSparkline(recent, {
        width: 220,
        height: 32,
        showLastDot: true,
        strokeColor: stroke,
      });
      // Make the SVG fluid so it adapts to card width without re-render.
      svg.removeAttribute('width');
      svg.removeAttribute('height');
      svg.setAttribute('preserveAspectRatio', 'none');
      svg.style.width = '100%';
      svg.style.height = '32px';
      sparkSlot.innerHTML = '';
      sparkSlot.appendChild(svg);
    }).catch(() => {
      sparkSlot.textContent = '';
    });
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

/* ----- Detail modal --------------------------------------------------- */
let _lastFocus = null;

function closeDetail() {
  const root = $('lcry-modal-root');
  if (!root) return;
  root.classList.add('hidden');
  root.innerHTML = '';
  document.body.classList.remove('lcry-modal-open');
  document.removeEventListener('keydown', _modalKeyHandler);
  if (_lastFocus && typeof _lastFocus.focus === 'function') {
    try { _lastFocus.focus(); } catch { /* no-op */ }
  }
}

function _modalKeyHandler(e) {
  if (e.key === 'Escape') {
    e.preventDefault();
    closeDetail();
  }
}

async function openDetail(row) {
  const root = $('lcry-modal-root');
  if (!root) return;
  _lastFocus = document.activeElement;
  root.innerHTML = '';

  const ticker = String(row.ticker || '').toUpperCase();
  const color = bandColor(row.band);

  const backdrop = el('div', {
    class: 'lcry-modal-backdrop',
    onclick: closeDetail,
  });

  const card = el('div', {
    class: 'lcry-modal-card',
    role: 'document',
    style: `--lcry-band: ${color};`,
  });

  // Header: title + close button.
  card.appendChild(el('div', { class: 'lcry-modal-head' }, [
    el('div', { class: 'lcry-modal-titles' }, [
      el('h2', { id: 'lcry-modal-title', class: 'lcry-modal-title', text: `${ticker} · ${row.maturity_stage || ''}` }),
      el('div', { class: 'lcry-modal-subtitle' }, [
        el('span', {
          class: 'lcry-modal-band',
          style: `background: ${color};`,
          text: bandLabel(row.band),
        }),
        el('span', { class: 'lcry-modal-score', text: fmtScore(row.lthcs_score) ?? 'n/a' }),
        el('span', { class: 'lcry-modal-conf', text: `confidence: ${row.confidence_level || 'n/a'}` }),
      ]),
    ]),
    el('button', {
      class: 'lcry-modal-close',
      type: 'button',
      'aria-label': 'Close detail',
      text: '×',
      onclick: closeDetail,
    }),
  ]));

  // 30d sparkline (large).
  const sparkBox = el('div', { class: 'lcry-modal-spark' }, [
    el('div', { class: 'lcry-modal-spark-label', text: 'Composite score · last 30 days' }),
    el('div', { class: 'lcry-modal-spark-svg', text: 'loading history…' }),
  ]);
  card.appendChild(sparkBox);

  // Pillar breakdown (bars).
  const pillarsBox = el('div', { class: 'lcry-modal-pillars' });
  pillarsBox.appendChild(el('div', { class: 'lcry-modal-section-label', text: 'Pillar breakdown' }));
  const subscores = row.subscores || {};
  const effWeights = row.effective_weights || [];
  const docWeights = row.weights_used || [];
  const dropped = new Set(row.dropped_pillars || []);
  PILLAR_ORDER.forEach(([key, label], i) => {
    const isDropped = dropped.has(key);
    const sub = subscores[key];
    const w = effWeights[i] != null ? effWeights[i] : docWeights[i];
    const pct = !isDropped && sub != null && !Number.isNaN(sub) ? Math.max(0, Math.min(100, Number(sub))) : 0;
    const fillColor = bandColorForScore(Number(sub)) || 'var(--accent)';
    pillarsBox.appendChild(el('div', { class: `lcry-modal-pillar ${isDropped ? 'dropped' : ''}` }, [
      el('div', { class: 'lcry-modal-pillar-head' }, [
        el('span', { class: 'lcry-modal-pillar-name', text: PILLAR_LONG[key] || label }),
        el('span', { class: 'lcry-modal-pillar-meta', text: isDropped
          ? 'dropped · weight renormalized'
          : `${fmtScore(sub) ?? 'n/a'} · ${fmtWeight(w)}` }),
      ]),
      el('div', { class: 'lcry-modal-pillar-track' },
        isDropped ? null : el('div', {
          class: 'lcry-modal-pillar-fill',
          style: `width: ${pct}%; background: ${fillColor};`,
        }),
      ),
    ]));
  });
  card.appendChild(pillarsBox);

  // Thesis call line.
  const call = thesisCall(row);
  card.appendChild(el('div', { class: 'lcry-modal-thesis' }, [
    el('span', { class: 'lcry-modal-section-label', text: 'Thesis integrity' }),
    el('span', { class: `lcry-thesis-call ${call.cls}`, text: call.text }),
  ]));

  // Drift table.
  const driftBox = el('div', { class: 'lcry-modal-drift' });
  driftBox.appendChild(el('div', { class: 'lcry-modal-section-label', text: 'Score drift' }));
  const driftGrid = el('div', { class: 'lcry-modal-drift-grid' });
  for (const [key, label] of [['drift_1d', '1d'], ['drift_7d', '7d'], ['drift_30d', '30d'], ['drift_90d', '90d']]) {
    const val = fmtDrift(row[key]);
    driftGrid.appendChild(el('div', { class: 'lcry-modal-drift-cell' }, [
      el('span', { class: 'lcry-modal-drift-key', text: `Δ${label}` }),
      el('span', {
        class: `lcry-card-drift-val ${driftClass(row[key])}`,
        text: val == null ? 'n/a' : val,
      }),
    ]));
  }
  driftBox.appendChild(driftGrid);
  card.appendChild(driftBox);

  // Data-quality flags (full sentences).
  const flags = Array.isArray(row.data_quality_flags) ? row.data_quality_flags : [];
  if (flags.length) {
    const flagBox = el('div', { class: 'lcry-modal-flags' });
    flagBox.appendChild(el('div', { class: 'lcry-modal-section-label', text: 'Data quality notes' }));
    for (const f of flags) {
      flagBox.appendChild(el('div', { class: 'lcry-modal-flag', text: FLAG_LABELS[f] || f }));
    }
    card.appendChild(flagBox);
  }

  root.appendChild(backdrop);
  root.appendChild(card);
  root.classList.remove('hidden');
  document.body.classList.add('lcry-modal-open');
  document.addEventListener('keydown', _modalKeyHandler);

  // Focus the close button for keyboard users.
  const closeBtn = card.querySelector('.lcry-modal-close');
  if (closeBtn) { try { closeBtn.focus(); } catch { /* no-op */ } }

  // Lazy-load history into the sparkline slot.
  try {
    const history = await loadHistory(row.ticker);
    const recent = history.slice(0, MODAL_SPARKLINE_DAYS);
    const svgSlot = card.querySelector('.lcry-modal-spark-svg');
    if (!svgSlot) return;
    svgSlot.innerHTML = '';
    if (!recent.length) {
      svgSlot.appendChild(el('span', { class: 'lcry-note', text: 'No score history on disk yet.' }));
      return;
    }
    const stroke = bandColorForScore(Number(row.lthcs_score)) || 'currentColor';
    const svg = renderSparkline(recent, {
      width: 640,
      height: 200,
      showBands: true,
      showAxes: true,
      showLastDot: true,
      strokeColor: stroke,
      fillColor: null,
    });
    svg.removeAttribute('width');
    svg.style.width = '100%';
    svg.style.height = '200px';
    svgSlot.appendChild(svg);
  } catch (e) {
    const svgSlot = card.querySelector('.lcry-modal-spark-svg');
    if (svgSlot) svgSlot.textContent = `history load failed: ${e.message || e}`;
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
    const universe = await tryFetch(UNIVERSE_PATH);
    loading.classList.add('hidden');
    emptyBox.classList.remove('hidden');
    // Customize empty-state copy with the actual probe window.
    const probeMarker = emptyBox.querySelector('.lcry-note');
    if (probeMarker) probeMarker.innerHTML = probeMarker.innerHTML.replace('{{PROBE_DAYS}}', String(PROBE_DAYS));
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
    const probeMarker = emptyBox.querySelector('.lcry-note');
    if (probeMarker) probeMarker.innerHTML = probeMarker.innerHTML.replace('{{PROBE_DAYS}}', String(PROBE_DAYS));
    genEl.textContent = date;
    renderEmpty(universe);
    return;
  }

  genEl.textContent = `${date}${snap.model_version ? ` · ${snap.model_version}` : ''}`;
  const { avg } = renderStrip(rows, date);
  renderCards(rows, { avg });
  renderThesis(rows);
  renderPillars(rows);
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
