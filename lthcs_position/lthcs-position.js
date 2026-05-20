/* =========================================================================
   LTHCS Position Sizing — paper-money allocation calculator.

   Inputs: total $, risk profile (Conservative / Balanced / Aggressive),
           optional cap override, and a ticker source (compare set,
           active watchlist, or manual entry).
   Output: per-ticker suggested $-allocation with rationale.

   Algorithm mirrors scripts/lthcs_position_sizing.py — keep both in sync:
     1) raw_weight = composite × band_multiplier
     2) normalise eligible weights to 1.0
     3) clip to per-position cap; redistribute overflow proportionally
     4) multiply by total $
   Review-band rows are excluded with a note. data_quality_flags surface
   inline but don't auto-exclude (per spec).
   ========================================================================= */

const DATA_ROOT = '../data/lthcs';

const BAND_MULTIPLIERS = {
  elite: 1.5,
  high: 1.2,
  high_confidence: 1.2,
  constructive: 1.0,
  monitor: 0.7,
  weakening: 0.3,
  review: 0.0,
};

const RISK_CAPS = {
  conservative: 0.05,
  balanced: 0.08,
  aggressive: 0.12,
};

const EPS = 1e-9;
const MAX_TICKERS = 20;
const MIN_TICKERS = 1;

/* ----- DOM helpers ------------------------------------------------------ */
function $(id) { return document.getElementById(id); }
function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null) continue;
    if (k === 'class') node.className = v;
    else if (k === 'text') node.textContent = v;
    else if (k === 'html') node.innerHTML = v;
    else if (k.startsWith('data-') || k.startsWith('aria-') || k === 'role' || k === 'tabindex' || k === 'scope') {
      node.setAttribute(k, v);
    } else {
      node.setAttribute(k, v);
    }
  }
  for (const c of (Array.isArray(children) ? children : [children])) {
    if (c == null) continue;
    node.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
  }
  return node;
}

function normalizeBand(band) {
  return String(band || '').trim().toLowerCase().replace(/-/g, '_').replace(/\s+/g, '_');
}
function bandMultiplier(band) {
  return BAND_MULTIPLIERS[normalizeBand(band)] ?? 0.0;
}

/* ----- Allocation algorithm — matches Python verbatim ------------------ */
function applyCap(weights, cap) {
  const n = weights.length;
  if (n === 0) return [];
  const w = weights.slice();
  // If the cap can't accommodate the bankroll, every position pegs at cap.
  if (cap * n <= 1.0 + EPS) {
    return w.map(x => Math.min(cap, x));
  }
  const locked = new Array(n).fill(false);
  for (let iter = 0; iter < 64; iter++) {
    let overflow = 0;
    for (let i = 0; i < n; i++) {
      if (locked[i]) continue;
      if (w[i] > cap + EPS) {
        overflow += w[i] - cap;
        w[i] = cap;
        locked[i] = true;
      }
    }
    if (overflow <= EPS) break;
    const recv = [];
    for (let i = 0; i < n; i++) {
      if (!locked[i] && w[i] < cap - EPS) recv.push(i);
    }
    if (recv.length === 0) break;
    let recvTotal = 0;
    for (const i of recv) recvTotal += w[i];
    if (recvTotal <= EPS) {
      const share = overflow / recv.length;
      for (const i of recv) w[i] = Math.min(cap, w[i] + share);
    } else {
      for (const i of recv) w[i] = Math.min(cap, w[i] + overflow * (w[i] / recvTotal));
    }
  }
  return w;
}

function suggestAllocations(inputs, totalDollars, cap) {
  // inputs: [{ticker, composite, band, data_quality_flags?: []}]
  // Returns {rows, total_allocated, cash_remaining, per_position_cap, included}
  if (!(totalDollars > 0)) throw new Error('total_dollars must be > 0');
  const rawScores = [];
  const eligibleIdx = [];
  for (let i = 0; i < inputs.length; i++) {
    const t = inputs[i];
    const mult = bandMultiplier(t.band);
    const score = Math.max(0, Number(t.composite) || 0) * mult;
    rawScores.push(score);
    if (mult > 0 && score > 0) eligibleIdx.push(i);
  }

  let totalRaw = 0;
  for (const i of eligibleIdx) totalRaw += rawScores[i];

  if (totalRaw <= EPS || eligibleIdx.length === 0) {
    // All-skipped / zero universe.
    const rows = inputs.map(t => {
      const mult = bandMultiplier(t.band);
      const note = normalizeBand(t.band) === 'review'
        ? 'Excluded per Review band'
        : 'No allocation (zero band-weighted score)';
      return {
        ticker: t.ticker,
        band: t.band,
        composite: Number(t.composite) || 0,
        band_multiplier: mult,
        raw_weight: 0,
        capped_weight: 0,
        dollars: 0,
        skipped: true,
        note,
        flags: t.data_quality_flags || [],
      };
    });
    return {
      rows,
      total_allocated: 0,
      cash_remaining: totalDollars,
      per_position_cap: cap,
      included: 0,
    };
  }

  const normalized = new Array(inputs.length).fill(0);
  for (const i of eligibleIdx) normalized[i] = rawScores[i] / totalRaw;
  const capped = applyCap(eligibleIdx.map(i => normalized[i]), cap);
  const finalWeights = new Array(inputs.length).fill(0);
  eligibleIdx.forEach((i, k) => { finalWeights[i] = capped[k]; });

  const rows = [];
  let totalAlloc = 0;
  let included = 0;
  for (let i = 0; i < inputs.length; i++) {
    const t = inputs[i];
    const mult = bandMultiplier(t.band);
    const rawW = normalized[i];
    const capW = finalWeights[i];
    const dollars = Math.round(capW * totalDollars * 100) / 100;
    const skipped = mult === 0 || rawW === 0;
    let note = '';
    const flags = t.data_quality_flags || [];
    if (skipped) {
      note = normalizeBand(t.band) === 'review'
        ? 'Excluded per Review band'
        : 'Skipped (band multiplier 0)';
    } else if (capW + EPS < rawW) {
      note = `Clipped to ${(cap * 100).toFixed(0)}% cap`;
    } else if (flags.length) {
      note = 'Low confidence: ' + flags.join(', ');
    }
    rows.push({
      ticker: t.ticker,
      band: t.band,
      composite: Number(t.composite) || 0,
      band_multiplier: mult,
      raw_weight: rawW,
      capped_weight: capW,
      dollars,
      skipped,
      note,
      flags,
    });
    if (!skipped) included += 1;
    totalAlloc += dollars;
  }
  return {
    rows,
    total_allocated: Math.round(totalAlloc * 100) / 100,
    cash_remaining: Math.round((totalDollars - totalAlloc) * 100) / 100,
    per_position_cap: cap,
    included,
  };
}

/* ----- Snapshot loading ------------------------------------------------- */
let SNAPSHOT_BY_TICKER = null; // { TICKER: {composite, band, flags} }
let SNAPSHOT_DATE = null;

async function loadLatestSnapshot() {
  // index.json carries the list of available snapshot dates.
  const idx = await fetchJSON(`${DATA_ROOT}/snapshots/index.json`);
  if (!idx || !Array.isArray(idx.dates) || idx.dates.length === 0) {
    throw new Error('No snapshot index found');
  }
  // `dates` is reverse-chronological (newest first). `latest` is the
  // canonical pointer; fall back to dates[0] for older index files that
  // don't carry it.
  const latest = idx.latest || idx.dates[0];
  const snap = await fetchJSON(`${DATA_ROOT}/snapshots/${latest}.json`);
  if (!snap || !Array.isArray(snap.scores)) {
    throw new Error(`Snapshot ${latest} is empty or malformed`);
  }
  const map = Object.create(null);
  for (const row of snap.scores) {
    map[row.ticker] = {
      composite: row.lthcs_score,
      band: row.band,
      flags: row.data_quality_flags || [],
      sector: row.sector || null,
    };
  }
  SNAPSHOT_BY_TICKER = map;
  SNAPSHOT_DATE = latest;
  $('lpos-snapshot-date').textContent = latest;
}

async function fetchJSON(url) {
  try {
    const r = await fetch(url, { cache: 'no-cache' });
    if (!r.ok) return null;
    return await r.json();
  } catch {
    return null;
  }
}

/* ----- Ticker source resolution ---------------------------------------- */
// Sources (Paths A/B/C):
//   compare    = JSON array of tickers in localStorage 'lthcs.compareSet'
//   watchlist  = lthcs.watchlists[lthcs.activeWatchlist].tickers
//   manual     = parsed from the textbox
const SOURCES = ['compare', 'watchlist', 'manual'];
const SOURCE_LABELS = {
  compare: 'Compare set',
  watchlist: 'Active watchlist',
  manual: 'Manual entry',
};

let ACTIVE_SOURCE = 'manual';

function readCompareSet() {
  try {
    const raw = localStorage.getItem('lthcs.compareSet');
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (Array.isArray(parsed) && parsed.length > 0) {
      return parsed.map(s => String(s).toUpperCase()).filter(Boolean);
    }
  } catch {}
  return null;
}

function readWatchlist() {
  try {
    const active = localStorage.getItem('lthcs.activeWatchlist');
    const listsRaw = localStorage.getItem('lthcs.watchlists');
    if (!active || !listsRaw) return null;
    const lists = JSON.parse(listsRaw);
    // Permit two shapes: { name: [tickers] } and { name: {tickers: [...]}}.
    const entry = lists?.[active];
    const tickers = Array.isArray(entry) ? entry : entry?.tickers;
    if (Array.isArray(tickers) && tickers.length > 0) {
      return tickers.map(s => String(s).toUpperCase()).filter(Boolean);
    }
  } catch {}
  return null;
}

function readManualTickers() {
  const raw = $('lpos-manual').value || '';
  // Split on comma, whitespace, or semicolon. Filter blanks.
  return raw
    .split(/[\s,;]+/)
    .map(s => s.trim().toUpperCase())
    .filter(Boolean);
}

function getTickersForSource(source) {
  if (source === 'compare') return readCompareSet() || [];
  if (source === 'watchlist') return readWatchlist() || [];
  return readManualTickers();
}

function renderSourceChips() {
  const wrap = $('lpos-source-chips');
  wrap.innerHTML = '';
  const compareAvail = !!readCompareSet();
  const watchlistAvail = !!readWatchlist();
  // If a higher-tier source is available, default to it on first render.
  if (ACTIVE_SOURCE === 'manual' && compareAvail) ACTIVE_SOURCE = 'compare';
  else if (ACTIVE_SOURCE === 'manual' && watchlistAvail) ACTIVE_SOURCE = 'watchlist';

  for (const src of SOURCES) {
    const avail = src === 'manual' || (src === 'compare' ? compareAvail : watchlistAvail);
    const chip = el('button', {
      type: 'button',
      class: 'lpos-source-chip',
      'aria-pressed': ACTIVE_SOURCE === src ? 'true' : 'false',
      'data-source': src,
    });
    chip.textContent = SOURCE_LABELS[src] + (avail || src === 'manual' ? '' : ' · none');
    chip.disabled = !avail && src !== 'manual';
    chip.addEventListener('click', () => {
      ACTIVE_SOURCE = src;
      renderSourceChips();
      syncManualVisibility();
      // Pre-fill the manual box from the resolved source so the user can
      // see (and edit) what will be priced.
      const tks = getTickersForSource(src);
      if (tks.length && src !== 'manual') {
        $('lpos-manual').value = tks.join(' ');
      }
    });
    wrap.appendChild(chip);
  }
}

function syncManualVisibility() {
  // The manual textbox stays visible even on compare/watchlist mode — it
  // lets the user override/edit the auto-resolved list. The only UX
  // change is the placeholder text.
  const input = $('lpos-manual');
  if (ACTIVE_SOURCE === 'compare') {
    input.placeholder = 'Edit to override compare set';
  } else if (ACTIVE_SOURCE === 'watchlist') {
    input.placeholder = 'Edit to override active watchlist';
  } else {
    input.placeholder = 'e.g. NVDA AAPL MSFT JPM TSLA';
  }
}

/* ----- Compute + render ------------------------------------------------- */
function setStatus(msg, tone) {
  const s = $('lpos-status');
  s.textContent = msg || '';
  if (tone) s.setAttribute('data-tone', tone);
  else s.removeAttribute('data-tone');
}

function computeAndRender() {
  if (!SNAPSHOT_BY_TICKER) {
    setStatus('Snapshot still loading…', 'warn');
    return;
  }
  const total = Number($('lpos-total').value);
  if (!(total > 0)) {
    setStatus('Enter a positive portfolio total.', 'error');
    return;
  }
  const profile = $('lpos-risk').value;
  let cap = RISK_CAPS[profile];
  const override = $('lpos-cap-override').value;
  if (override !== '' && override != null) {
    const pct = Number(override);
    if (!(pct > 0) || pct > 100) {
      setStatus('Cap override must be between 0 and 100.', 'error');
      return;
    }
    cap = pct / 100;
  }

  // Resolve tickers from the manual textbox — even in compare/watchlist
  // mode the user can have edited it. This is intentional.
  const tickers = readManualTickers();
  if (tickers.length < MIN_TICKERS) {
    setStatus('Enter at least one ticker.', 'error');
    $('lpos-output').classList.add('hidden');
    return;
  }
  if (tickers.length > MAX_TICKERS) {
    setStatus(`Limit ${MAX_TICKERS} tickers (got ${tickers.length}).`, 'error');
    return;
  }

  const inputs = [];
  const missing = [];
  for (const t of tickers) {
    const s = SNAPSHOT_BY_TICKER[t];
    if (!s) { missing.push(t); continue; }
    inputs.push({
      ticker: t,
      composite: s.composite,
      band: s.band,
      data_quality_flags: s.flags,
    });
  }

  if (inputs.length === 0) {
    setStatus(`None of those tickers are in the latest snapshot.`, 'error');
    $('lpos-output').classList.add('hidden');
    return;
  }
  if (missing.length) {
    setStatus(`Skipped (not in snapshot): ${missing.join(', ')}`, 'warn');
  } else {
    setStatus(`Allocated across ${inputs.length} ticker${inputs.length === 1 ? '' : 's'}.`, 'ok');
  }

  const result = suggestAllocations(inputs, total, cap);
  renderResult(result);
}

function fmtUSD(n) {
  return n.toLocaleString('en-US', {
    style: 'currency',
    currency: 'USD',
    maximumFractionDigits: 0,
  });
}
function fmtPct(n) {
  return `${(n * 100).toFixed(2)}%`;
}

function renderResult(result) {
  $('lpos-output').classList.remove('hidden');
  $('lpos-kpi-allocated').textContent = fmtUSD(result.total_allocated);
  $('lpos-kpi-cash').textContent = fmtUSD(result.cash_remaining);
  $('lpos-kpi-included').textContent = String(result.included);
  $('lpos-kpi-cap').textContent = `${(result.per_position_cap * 100).toFixed(0)}%`;

  const tbody = $('lpos-tbody');
  tbody.innerHTML = '';
  for (const r of result.rows) {
    const tr = el('tr', {
      'data-ticker': r.ticker,
      class: r.skipped ? 'is-skipped' : '',
    });
    const bandKey = normalizeBand(r.band).replace('_confidence', '');
    tr.appendChild(el('td', { 'data-label': 'Ticker' }, [el('strong', { text: r.ticker })]));
    tr.appendChild(el('td', { 'data-label': 'Band' }, [
      el('span', { class: 'lpos-band-pill', 'data-band': bandKey, text: r.band.replace(/_/g, ' ') }),
    ]));
    tr.appendChild(el('td', { 'data-label': 'Composite', class: 'lpos-num' }, r.composite.toFixed(1)));
    tr.appendChild(el('td', { 'data-label': 'Band mult.', class: 'lpos-num' }, `${r.band_multiplier.toFixed(1)}×`));
    tr.appendChild(el('td', { 'data-label': 'Raw %', class: 'lpos-num' }, fmtPct(r.raw_weight)));
    tr.appendChild(el('td', { 'data-label': 'Capped %', class: 'lpos-num' }, fmtPct(r.capped_weight)));
    tr.appendChild(el('td', { 'data-label': 'Suggested $', class: 'lpos-num' }, fmtUSD(r.dollars)));
    const noteTone = r.skipped ? 'error' : (r.note.startsWith('Clipped') ? 'warn' : (r.note.startsWith('Low confidence') ? 'warn' : ''));
    tr.appendChild(el('td', { 'data-label': 'Notes' }, [
      el('span', { class: 'lpos-note', 'data-tone': noteTone || null, text: r.note || '—' }),
    ]));
    tbody.appendChild(tr);
  }
}

/* ----- Bootstrap -------------------------------------------------------- */
async function init() {
  // Render chip UI early so the page isn't blank during snapshot fetch.
  renderSourceChips();
  syncManualVisibility();

  $('lpos-loading').classList.remove('hidden');
  try {
    await loadLatestSnapshot();
    $('lpos-loading').classList.add('hidden');
  } catch (err) {
    $('lpos-loading').classList.add('hidden');
    const e = $('lpos-error');
    e.textContent = `Could not load LTHCS snapshot: ${err.message || err}`;
    e.classList.remove('hidden');
    return;
  }

  // If the chips picked compare/watchlist by default, pre-fill the manual
  // box so the user sees what's about to be priced.
  if (ACTIVE_SOURCE !== 'manual') {
    const tks = getTickersForSource(ACTIVE_SOURCE);
    if (tks.length) $('lpos-manual').value = tks.join(' ');
  }

  $('lpos-compute').addEventListener('click', computeAndRender);

  // Recompute on Enter inside any input.
  for (const id of ['lpos-total', 'lpos-cap-override', 'lpos-manual']) {
    $(id).addEventListener('keydown', e => {
      if (e.key === 'Enter') {
        e.preventDefault();
        computeAndRender();
      }
    });
  }
  // Risk-profile change is a tap-to-recompute — cheap and the table is
  // small, so just rerun every change rather than gating behind the button.
  $('lpos-risk').addEventListener('change', () => {
    if ($('lpos-output').classList.contains('hidden')) return;
    computeAndRender();
  });
}

init();

// Exposed for unit testing via the browser console if needed.
export { suggestAllocations, bandMultiplier, RISK_CAPS };
