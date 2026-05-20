// =========================================================================
// LTHCS — Score History Search (/lthcs/history/)
// Phase 5 ZETA. Vanilla ES module, no build step.
//
// Data: data/lthcs/history/by_ticker/<TKR>.json
//   { ticker, model_version, history: [ { date, score, band }, ... ] }
// Note: history is stored in descending date order in the source files; we
// normalize to ascending (chronological) on load.
//
// Bands (from data/lthcs/weights.json `score_bands`):
//   elite, high_confidence, constructive, monitor, weakening, review
// =========================================================================

const DATA_BASE = '../data/lthcs/history/by_ticker';
const UNIVERSE_URL = '../data/lthcs/universe.json';

const BANDS = ['elite', 'high_confidence', 'constructive', 'monitor', 'weakening', 'review'];
const BAND_LABEL = {
  elite: 'Elite',
  high_confidence: 'High Confidence',
  constructive: 'Constructive',
  monitor: 'Monitor',
  weakening: 'Weakening',
  review: 'Review',
};

// In-memory cache: ticker -> { history: [...ascending] } or null on 404.
const HISTORY_CACHE = new Map();
let UNIVERSE_TICKERS = null; // string[] | null

// ----- utilities ----------------------------------------------------------

function $(id) { return document.getElementById(id); }

function setStatus(msg, kind) {
  const el = $('lthcs-history-status');
  if (!el) return;
  el.className = 'lthcs-section lthcs-history-status';
  if (!msg) { el.classList.add('hidden'); el.textContent = ''; return; }
  el.classList.remove('hidden');
  if (kind === 'error') el.classList.add('is-error');
  else if (kind === 'warn') el.classList.add('is-warn');
  el.textContent = msg;
}

function clearResults() { $('lthcs-history-results').innerHTML = ''; }

function fmtDate(iso) { return iso || '—'; }

function daysBetween(aIso, bIso) {
  // inclusive count: same date = 1 day
  const a = new Date(aIso + 'T00:00:00Z');
  const b = new Date(bIso + 'T00:00:00Z');
  if (Number.isNaN(a.getTime()) || Number.isNaN(b.getTime())) return 0;
  return Math.round((b - a) / 86400000) + 1;
}

function pill(band) {
  const key = band || 'unknown';
  const label = BAND_LABEL[key] || (band || 'unknown');
  const safe = String(label).replace(/[<>&]/g, (c) => ({ '<': '&lt;', '>': '&gt;', '&': '&amp;' }[c]));
  return `<span class="lthcs-history-pill" data-band="${key}">${safe}</span>`;
}

function escapeHtml(s) {
  return String(s == null ? '' : s).replace(/[<>&"]/g, (c) => ({
    '<': '&lt;', '>': '&gt;', '&': '&amp;', '"': '&quot;',
  }[c]));
}

async function fetchJson(url, { signal } = {}) {
  const r = await fetch(url, { cache: 'force-cache', signal });
  if (!r.ok) {
    const err = new Error(`HTTP ${r.status} for ${url}`);
    err.status = r.status;
    throw err;
  }
  return r.json();
}

// Load a ticker's history (ascending by date). Returns null on 404.
async function loadTickerHistory(ticker) {
  const key = ticker.toUpperCase();
  if (HISTORY_CACHE.has(key)) return HISTORY_CACHE.get(key);
  try {
    const raw = await fetchJson(`${DATA_BASE}/${encodeURIComponent(key)}.json`);
    const hist = Array.isArray(raw?.history) ? raw.history.slice() : [];
    // Source is desc by date; normalize ascending. Sort defensively too.
    hist.sort((a, b) => (a.date < b.date ? -1 : a.date > b.date ? 1 : 0));
    const value = { ticker: raw?.ticker || key, model_version: raw?.model_version || null, history: hist };
    HISTORY_CACHE.set(key, value);
    return value;
  } catch (e) {
    if (e && e.status === 404) {
      HISTORY_CACHE.set(key, null);
      return null;
    }
    throw e;
  }
}

// Lazy-load the ticker universe (for the datalist + bulk band/streak ops).
async function loadUniverseTickers() {
  if (UNIVERSE_TICKERS) return UNIVERSE_TICKERS;
  try {
    const u = await fetchJson(UNIVERSE_URL);
    // universe.json shape: { equities: [{ticker, ...}], crypto: [{ticker,...}] } or similar.
    // Be defensive: pull every string we find in a `ticker` field.
    const out = new Set();
    function walk(obj) {
      if (!obj) return;
      if (Array.isArray(obj)) { obj.forEach(walk); return; }
      if (typeof obj === 'object') {
        if (typeof obj.ticker === 'string') out.add(obj.ticker.toUpperCase());
        Object.values(obj).forEach(walk);
      }
    }
    walk(u);
    UNIVERSE_TICKERS = Array.from(out).sort();
  } catch {
    UNIVERSE_TICKERS = [];
  }
  return UNIVERSE_TICKERS;
}

// Run-length encode a history into [{band, start, end, len}, ...].
function bandRuns(history) {
  const runs = [];
  let cur = null;
  for (const h of history) {
    if (!h || !h.date) continue;
    const band = h.band || null;
    if (cur && cur.band === band) {
      cur.end = h.date;
      cur.len += 1;
    } else {
      if (cur) runs.push(cur);
      cur = { band, start: h.date, end: h.date, len: 1 };
    }
  }
  if (cur) runs.push(cur);
  return runs;
}

// Distill band-change events from runs: every run boundary is a "date in"
// for that run's band; the next run's start - 1 is "date out".
function bandChangeEvents(history) {
  const runs = bandRuns(history);
  return runs.map((r, i) => ({
    band: r.band,
    dateIn: r.start,
    dateOut: r.end,
    days: r.len,
    isCurrent: i === runs.length - 1,
  }));
}

// ----- ticker mode: chart + event table -----------------------------------

function renderTickerCard(data) {
  const { ticker, model_version, history } = data;
  const card = document.createElement('div');
  card.className = 'lthcs-history-card';

  const start = history[0]?.date;
  const end = history[history.length - 1]?.date;
  const events = bandChangeEvents(history);
  const current = events[events.length - 1];

  card.innerHTML = `
    <h2 class="lthcs-history-card-title">${escapeHtml(ticker)} &middot; composite history</h2>
    <p class="lthcs-history-card-sub">
      ${history.length} snapshot${history.length === 1 ? '' : 's'}
      &middot; ${escapeHtml(fmtDate(start))} &rarr; ${escapeHtml(fmtDate(end))}
      ${model_version ? `&middot; model ${escapeHtml(model_version)}` : ''}
      ${current ? `&middot; currently ${pill(current.band)} for ${current.days} day${current.days === 1 ? '' : 's'}` : ''}
    </p>
    <div class="lthcs-history-chart-wrap"></div>
    <h3 class="lthcs-history-card-title" style="margin-top:16px;font-size:14px">Band-change events (${events.length})</h3>
    <div class="lthcs-history-table-wrap"></div>
  `;

  card.querySelector('.lthcs-history-chart-wrap').appendChild(renderLineChart(history));
  card.querySelector('.lthcs-history-table-wrap').appendChild(renderEventTable(events));
  return card;
}

function renderLineChart(history) {
  // SVG with viewBox; line of score vs date, dots at each snapshot.
  const W = 800, H = 240, padL = 40, padR = 12, padT = 12, padB = 24;
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('class', 'lthcs-history-chart');
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  svg.setAttribute('preserveAspectRatio', 'none');
  svg.setAttribute('role', 'img');
  svg.setAttribute('aria-label', 'LTHCS composite score over time');

  if (!history.length) return svg;

  const xs = history.map((h, i) => i);
  const ys = history.map((h) => Number.isFinite(h.score) ? h.score : 50);
  const xMin = 0, xMax = Math.max(1, history.length - 1);
  // Score is a 0..100 composite; pin axis to [0,100] so band shading lines up.
  const yMin = 0, yMax = 100;

  const xScale = (x) => padL + (x - xMin) * (W - padL - padR) / (xMax - xMin || 1);
  const yScale = (y) => H - padB - (y - yMin) * (H - padT - padB) / (yMax - yMin);

  // Band shade rectangles. Approximate band-to-score map from the public
  // `score_bands` in data/lthcs/weights.json: 0-30 review, 30-45 weakening,
  // 45-55 monitor, 55-70 constructive, 70-85 high_confidence, 85-100 elite.
  const bands = [
    { band: 'review',           lo: 0,  hi: 30 },
    { band: 'weakening',        lo: 30, hi: 45 },
    { band: 'monitor',          lo: 45, hi: 55 },
    { band: 'constructive',     lo: 55, hi: 70 },
    { band: 'high_confidence',  lo: 70, hi: 85 },
    { band: 'elite',            lo: 85, hi: 100 },
  ];
  const colorVar = {
    elite: '--band-elite', high_confidence: '--band-high', constructive: '--band-constructive',
    monitor: '--band-monitor', weakening: '--band-weakening', review: '--band-review',
  };
  for (const b of bands) {
    const y1 = yScale(b.hi), y2 = yScale(b.lo);
    const rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
    rect.setAttribute('x', padL);
    rect.setAttribute('y', y1);
    rect.setAttribute('width', W - padL - padR);
    rect.setAttribute('height', y2 - y1);
    rect.setAttribute('class', 'band-shade');
    rect.setAttribute('fill', `var(${colorVar[b.band]})`);
    svg.appendChild(rect);
  }

  // Y-axis grid + labels at 0, 25, 50, 75, 100.
  const g = document.createElementNS('http://www.w3.org/2000/svg', 'g');
  g.setAttribute('class', 'axis');
  for (const v of [0, 25, 50, 75, 100]) {
    const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
    line.setAttribute('x1', padL);
    line.setAttribute('x2', W - padR);
    line.setAttribute('y1', yScale(v));
    line.setAttribute('y2', yScale(v));
    line.setAttribute('stroke', 'var(--border-subtle)');
    line.setAttribute('stroke-width', 1);
    svg.appendChild(line);
    const t = document.createElementNS('http://www.w3.org/2000/svg', 'text');
    t.setAttribute('x', padL - 6);
    t.setAttribute('y', yScale(v) + 3);
    t.setAttribute('text-anchor', 'end');
    t.textContent = String(v);
    g.appendChild(t);
  }
  // X-axis: first/middle/last labels.
  const xLabelIdx = [0, Math.floor((history.length - 1) / 2), history.length - 1];
  for (const i of xLabelIdx) {
    if (i < 0 || i >= history.length) continue;
    const t = document.createElementNS('http://www.w3.org/2000/svg', 'text');
    t.setAttribute('x', xScale(i));
    t.setAttribute('y', H - 6);
    t.setAttribute('text-anchor', i === 0 ? 'start' : i === history.length - 1 ? 'end' : 'middle');
    t.textContent = history[i].date;
    g.appendChild(t);
  }
  svg.appendChild(g);

  // Line path.
  const d = history.map((h, i) => `${i === 0 ? 'M' : 'L'} ${xScale(i).toFixed(1)} ${yScale(ys[i]).toFixed(1)}`).join(' ');
  const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  path.setAttribute('class', 'line');
  path.setAttribute('d', d);
  svg.appendChild(path);

  // Points (only if not too dense).
  if (history.length <= 120) {
    for (let i = 0; i < history.length; i++) {
      const c = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
      c.setAttribute('class', 'point');
      c.setAttribute('cx', xScale(i));
      c.setAttribute('cy', yScale(ys[i]));
      c.setAttribute('r', 1.8);
      const title = document.createElementNS('http://www.w3.org/2000/svg', 'title');
      title.textContent = `${history[i].date}: ${ys[i].toFixed(1)} (${history[i].band || 'unknown'})`;
      c.appendChild(title);
      svg.appendChild(c);
    }
  }
  return svg;
}

function renderEventTable(events) {
  const table = document.createElement('table');
  table.className = 'lthcs-history-table';
  table.innerHTML = `
    <thead>
      <tr>
        <th>Band</th>
        <th>Date in</th>
        <th>Date out</th>
        <th class="num">Days</th>
        <th>Status</th>
      </tr>
    </thead>
    <tbody></tbody>
  `;
  const tbody = table.querySelector('tbody');
  // Show most recent first; matches the spec of "every band-change event".
  const rows = events.slice().reverse();
  for (const e of rows) {
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td>${pill(e.band)}</td>
      <td>${escapeHtml(e.dateIn)}</td>
      <td>${escapeHtml(e.dateOut)}</td>
      <td class="num">${e.days}</td>
      <td>${e.isCurrent ? '<em>current</em>' : ''}</td>
    `;
    tbody.appendChild(tr);
  }
  return table;
}

// ----- band mode: tickers in a band over a window -------------------------

function windowStartDate(windowDays, todayIso) {
  if (windowDays === 'all') return null; // no lower bound
  const n = Number(windowDays);
  if (!Number.isFinite(n) || n <= 0) return null;
  const today = todayIso ? new Date(todayIso + 'T00:00:00Z') : new Date();
  const d = new Date(today.getTime() - (n - 1) * 86400000);
  return d.toISOString().slice(0, 10);
}

async function runBandSearch(band, windowDays) {
  setStatus('Loading universe history…');
  clearResults();
  const tickers = await loadUniverseTickers();
  if (!tickers.length) {
    setStatus('Could not load ticker universe.', 'error');
    return;
  }

  // Bail-after-5s: collect whatever resolved by then.
  const t0 = performance.now();
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 5000);

  const results = [];
  const errors = [];
  await Promise.all(tickers.map(async (tkr) => {
    try {
      const cached = HISTORY_CACHE.get(tkr);
      const data = cached === undefined
        ? await loadTickerHistoryWithSignal(tkr, controller.signal)
        : cached;
      if (!data) return;
      results.push({ ticker: tkr, data });
    } catch (e) {
      if (e.name !== 'AbortError') errors.push(tkr);
    }
  }));
  clearTimeout(timer);

  // Find the latest date across the universe — that's "today" for window calc.
  let latest = null;
  for (const r of results) {
    const last = r.data.history[r.data.history.length - 1]?.date;
    if (last && (!latest || last > latest)) latest = last;
  }
  const start = windowStartDate(windowDays, latest);
  const windowLabel = windowDays === 'all' ? 'all-time' : `last ${windowDays} days`;

  // For each ticker, count days in `band` within the window.
  const rows = [];
  for (const r of results) {
    let daysInBand = 0;
    let firstHit = null;
    let lastHit = null;
    for (const h of r.data.history) {
      if (!h.band) continue;
      if (start && h.date < start) continue;
      if (h.band === band) {
        daysInBand += 1;
        if (!firstHit) firstHit = h.date;
        lastHit = h.date;
      }
    }
    if (daysInBand > 0) rows.push({ ticker: r.ticker, days: daysInBand, firstHit, lastHit });
  }
  rows.sort((a, b) => b.days - a.days || a.ticker.localeCompare(b.ticker));

  const elapsed = ((performance.now() - t0) / 1000).toFixed(2);
  const aborted = errors.length > 0 || controller.signal.aborted;
  if (aborted) {
    setStatus(`Loaded ${results.length}/${tickers.length} tickers in ${elapsed}s (5s cutoff). Ranking with what's available.`, 'warn');
  } else {
    setStatus(`Loaded ${results.length}/${tickers.length} tickers in ${elapsed}s.`);
  }

  const card = document.createElement('div');
  card.className = 'lthcs-history-card';
  card.innerHTML = `
    <h2 class="lthcs-history-card-title">${escapeHtml(BAND_LABEL[band] || band)} &middot; ${escapeHtml(windowLabel)}</h2>
    <p class="lthcs-history-card-sub">
      ${rows.length} ticker${rows.length === 1 ? '' : 's'} touched this band in window.
      Sorted by days in band, descending.
    </p>
    <div class="lthcs-history-table-wrap"></div>
  `;
  const wrap = card.querySelector('.lthcs-history-table-wrap');
  if (!rows.length) {
    const empty = document.createElement('div');
    empty.className = 'lthcs-history-empty';
    empty.textContent = `No ticker hit ${BAND_LABEL[band] || band} during ${windowLabel}.`;
    wrap.appendChild(empty);
  } else {
    const table = document.createElement('table');
    table.className = 'lthcs-history-table';
    table.innerHTML = `
      <thead>
        <tr>
          <th>Ticker</th>
          <th>Band</th>
          <th class="num">Days in band</th>
          <th>First hit</th>
          <th>Last hit</th>
        </tr>
      </thead>
      <tbody></tbody>
    `;
    const tbody = table.querySelector('tbody');
    for (const r of rows) {
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td><strong>${escapeHtml(r.ticker)}</strong></td>
        <td>${pill(band)}</td>
        <td class="num">${r.days}</td>
        <td>${escapeHtml(r.firstHit || '')}</td>
        <td>${escapeHtml(r.lastHit || '')}</td>
      `;
      tbody.appendChild(tr);
    }
    wrap.appendChild(table);
  }
  clearResults();
  $('lthcs-history-results').appendChild(card);
}

// Like loadTickerHistory but respects an external AbortSignal so the 5s
// cutoff in the band/streak searches can yank in-flight fetches.
async function loadTickerHistoryWithSignal(ticker, signal) {
  const key = ticker.toUpperCase();
  if (HISTORY_CACHE.has(key)) return HISTORY_CACHE.get(key);
  try {
    const raw = await fetchJson(`${DATA_BASE}/${encodeURIComponent(key)}.json`, { signal });
    const hist = Array.isArray(raw?.history) ? raw.history.slice() : [];
    hist.sort((a, b) => (a.date < b.date ? -1 : a.date > b.date ? 1 : 0));
    const value = { ticker: raw?.ticker || key, model_version: raw?.model_version || null, history: hist };
    HISTORY_CACHE.set(key, value);
    return value;
  } catch (e) {
    if (e && e.status === 404) {
      HISTORY_CACHE.set(key, null);
      return null;
    }
    throw e;
  }
}

// ----- streak mode: top runs across all tickers ---------------------------

async function runStreakSearch() {
  setStatus('Loading universe history…');
  clearResults();
  const tickers = await loadUniverseTickers();
  if (!tickers.length) {
    setStatus('Could not load ticker universe.', 'error');
    return;
  }

  const t0 = performance.now();
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 5000);

  const datas = [];
  await Promise.all(tickers.map(async (tkr) => {
    try {
      const data = await loadTickerHistoryWithSignal(tkr, controller.signal);
      if (data) datas.push({ ticker: tkr, data });
    } catch { /* swallow */ }
  }));
  clearTimeout(timer);

  // Build streak list per band.
  const byBand = new Map(BANDS.map((b) => [b, []]));
  for (const d of datas) {
    const runs = bandRuns(d.data.history);
    for (const r of runs) {
      if (!r.band) continue;
      const arr = byBand.get(r.band) || [];
      arr.push({ ticker: d.ticker, start: r.start, end: r.end, days: r.len });
      byBand.set(r.band, arr);
    }
  }
  for (const b of BANDS) {
    const arr = byBand.get(b) || [];
    arr.sort((a, b2) => b2.days - a.days || a.ticker.localeCompare(b2.ticker));
    byBand.set(b, arr.slice(0, 10));
  }

  const elapsed = ((performance.now() - t0) / 1000).toFixed(2);
  setStatus(`Loaded ${datas.length}/${tickers.length} tickers in ${elapsed}s. Top 10 streaks per band.`);

  const wrap = document.createElement('div');
  wrap.className = 'lthcs-history-streak-grid';
  for (const b of BANDS) {
    const col = document.createElement('div');
    col.className = 'lthcs-history-streak-col';
    const rows = byBand.get(b) || [];
    col.innerHTML = `
      <div class="lthcs-history-streak-head">${pill(b)} <h3>${escapeHtml(BAND_LABEL[b] || b)}</h3></div>
      <div class="lthcs-history-table-wrap"></div>
    `;
    const tw = col.querySelector('.lthcs-history-table-wrap');
    if (!rows.length) {
      const empty = document.createElement('div');
      empty.className = 'lthcs-history-empty';
      empty.textContent = `No ticker has touched ${BAND_LABEL[b] || b}.`;
      tw.appendChild(empty);
    } else {
      const table = document.createElement('table');
      table.className = 'lthcs-history-table';
      table.innerHTML = `
        <thead><tr><th>Ticker</th><th class="num">Days</th><th>Window</th></tr></thead>
        <tbody></tbody>
      `;
      const tbody = table.querySelector('tbody');
      for (const r of rows) {
        const tr = document.createElement('tr');
        tr.innerHTML = `
          <td><strong>${escapeHtml(r.ticker)}</strong></td>
          <td class="num">${r.days}</td>
          <td>${escapeHtml(r.start)} &rarr; ${escapeHtml(r.end)}</td>
        `;
        tbody.appendChild(tr);
      }
      tw.appendChild(table);
    }
    wrap.appendChild(col);
  }
  clearResults();
  $('lthcs-history-results').appendChild(wrap);
}

// ----- handlers + boot ----------------------------------------------------

function switchMode(mode) {
  for (const btn of document.querySelectorAll('.lthcs-history-mode')) {
    const isActive = btn.dataset.mode === mode;
    btn.classList.toggle('is-active', isActive);
    btn.setAttribute('aria-selected', isActive ? 'true' : 'false');
  }
  for (const id of ['ticker', 'band', 'streak']) {
    const f = $(`lthcs-history-form-${id}`);
    if (f) f.classList.toggle('hidden', id !== mode);
  }
}

async function handleTickerGo() {
  const input = $('lthcs-history-ticker');
  const tkr = (input.value || '').trim().toUpperCase();
  if (!tkr) { setStatus('Enter a ticker first.', 'warn'); return; }
  setStatus(`Loading ${tkr}…`);
  clearResults();
  try {
    const data = await loadTickerHistory(tkr);
    if (!data || !data.history?.length) {
      const card = document.createElement('div');
      card.className = 'lthcs-history-card';
      card.innerHTML = `
        <h2 class="lthcs-history-card-title">${escapeHtml(tkr)}</h2>
        <div class="lthcs-history-empty">
          ${escapeHtml(tkr)} has no history on file yet (snapshot count: 0).
        </div>
      `;
      $('lthcs-history-results').appendChild(card);
      setStatus(null);
      return;
    }
    $('lthcs-history-results').appendChild(renderTickerCard(data));
    setStatus(null);
  } catch (e) {
    setStatus(`Error loading ${tkr}: ${e.message || e}`, 'error');
  }
}

async function handleBandGo() {
  const band = $('lthcs-history-band').value;
  const win = $('lthcs-history-window').value;
  try {
    await runBandSearch(band, win);
  } catch (e) {
    setStatus(`Band search failed: ${e.message || e}`, 'error');
  }
}

async function handleStreakGo() {
  try {
    await runStreakSearch();
  } catch (e) {
    setStatus(`Streak computation failed: ${e.message || e}`, 'error');
  }
}

function wireUp() {
  for (const btn of document.querySelectorAll('.lthcs-history-mode')) {
    btn.addEventListener('click', () => switchMode(btn.dataset.mode));
  }
  $('lthcs-history-ticker-go').addEventListener('click', handleTickerGo);
  $('lthcs-history-ticker').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); handleTickerGo(); }
  });
  $('lthcs-history-band-go').addEventListener('click', handleBandGo);
  $('lthcs-history-streak-go').addEventListener('click', handleStreakGo);

  // Populate datalist lazily so first paint is instant.
  loadUniverseTickers().then((list) => {
    const dl = $('lthcs-history-ticker-list');
    if (!dl) return;
    dl.innerHTML = list.map((t) => `<option value="${escapeHtml(t)}"></option>`).join('');
  });

  // Deep-link support: ?mode=ticker&q=AAPL / ?mode=band&band=elite&window=30
  try {
    const u = new URL(window.location.href);
    const mode = u.searchParams.get('mode');
    if (mode === 'ticker' || mode === 'band' || mode === 'streak') {
      switchMode(mode);
      if (mode === 'ticker') {
        const q = u.searchParams.get('q');
        if (q) { $('lthcs-history-ticker').value = q; handleTickerGo(); }
      } else if (mode === 'band') {
        const b = u.searchParams.get('band');
        const w = u.searchParams.get('window');
        if (b) $('lthcs-history-band').value = b;
        if (w) $('lthcs-history-window').value = w;
        if (u.searchParams.get('go') === '1') handleBandGo();
      } else if (mode === 'streak') {
        if (u.searchParams.get('go') === '1') handleStreakGo();
      }
    }
  } catch { /* ignore */ }
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', wireUp);
} else {
  wireUp();
}

// Export internals for tests / console exploration.
export const __internals__ = { bandRuns, bandChangeEvents, windowStartDate, daysBetween };
