/* =========================================================================
   LTHCS Pipeline Health — vanilla-JS observability dashboard.

   Read-only consumer of files under ../data/lthcs/. Renders:
     - Cadence (last run, cron, drift, catch-up)
     - Source coverage (today + 30d history per source)
     - Per-pillar data-quality counts (from variable_detail/<date>.json)
     - Recent runs (last 14 from snapshots/index.json)

   Data: health_summary.json (built at deploy time by
   scripts/build_lthcs_site_index.py) carries every count shown here. Only
   when it is missing does the page fall back to reading the raw per-date
   files — and then only the ones the file index says exist.
   ========================================================================= */

// Shared data-freshness stamp (ported from v2/app.py — one dialect site-wide).
import { paintComposite } from '../lthcs_tab/lthcs-freshness.js';
// Deploy-time listing of which data files exist (skips 404 probes).
import { loadFileIndex } from '../lthcs_tab/lthcs-files.js';

const DATA_ROOT = '../data/lthcs';

/* Cron is hard-coded to match .github/workflows/lthcs-daily.yml. Update
   here AND there if the schedule moves. The schedule is read-only; this
   page reflects what runs in CI, it doesn't drive it. */
const CRON_EXPR = '0 23 * * *';
const CRON_HUMAN = '23:00 UTC';

/* Coverage tier thresholds — match the design spec in the README header
   block on index.html. Green >= 90, amber 50–89, red < 50. */
function tierFor(pct) {
  if (pct >= 90) return 'ok';
  if (pct >= 50) return 'warn';
  return 'fail';
}

/* ----- DOM helpers ------------------------------------------------------ */
function $(id) { return document.getElementById(id); }
function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') node.className = v;
    else if (k === 'text') node.textContent = v;
    else if (k.startsWith('data-')) node.setAttribute(k, v);
    else node.setAttribute(k, v);
  }
  for (const c of (Array.isArray(children) ? children : [children])) {
    if (c == null) continue;
    node.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
  }
  return node;
}

/* ----- Date helpers ----------------------------------------------------- */
function parseISODate(s) {
  // s = "YYYY-MM-DD"; build UTC midnight so day-count math is timezone-safe.
  const [y, m, d] = s.split('-').map(Number);
  return new Date(Date.UTC(y, m - 1, d));
}
function isoToday() {
  const t = new Date();
  return `${t.getUTCFullYear()}-${String(t.getUTCMonth() + 1).padStart(2, '0')}-${String(t.getUTCDate()).padStart(2, '0')}`;
}
function daysBetween(a, b) {
  return Math.round((parseISODate(b) - parseISODate(a)) / (24 * 3600 * 1000));
}
function hoursAgoISO(iso) {
  // Treat snapshot date as run at the cron hour (23:00 UTC).
  const cronTime = new Date(`${iso}T23:00:00Z`);
  const diffMs = Date.now() - cronTime.getTime();
  return Math.round(diffMs / (3600 * 1000));
}

/* ----- Fetch with graceful 404 ----------------------------------------- */
async function tryFetch(url) {
  try {
    const r = await fetch(url, { cache: 'no-cache' });
    if (!r.ok) return null;
    return await r.json();
  } catch {
    return null;
  }
}

/* ----- Per-day coverage rows ------------------------------------------
   One shape for both data paths: the deploy-time health_summary.json
   (scripts/build_lthcs_site_index.py writes exactly these fields) and the
   in-browser fallback below, which derives them from the raw files. */
function dayRow(date, f) {
  const { snap, ins, hol, mb, mbs, mss } = f || {};
  const src = (o) => {
    const dq = o && o.data_quality;
    if (!dq || typeof dq !== 'object') return [null, null];
    return [Number.isInteger(dq.sources_ok) ? dq.sources_ok : null,
      Number.isInteger(dq.sources_failed) ? dq.sources_failed : null];
  };
  const [mbOk, mbBad] = src(mb);
  const [sOk, sBad] = src(mbs);
  return {
    date,
    snapshot: !!snap,
    snapshot_calc_date: snap ? (snap.calc_date || null) : null,
    tickers: Array.isArray(snap?.scores) ? snap.scores.length : null,
    insider: ins && typeof ins === 'object' ? Object.keys(ins).length : null,
    holdings_covered: hol && typeof hol === 'object'
      ? Object.values(hol).filter((v) => v && typeof v === 'object' && (v.manager_count || 0) > 0).length
      : null,
    macro_as_of: mb ? (mb.as_of || null) : null,
    macro_ok: mbOk, macro_failed: mbBad,
    sentiment_ok: sOk, sentiment_failed: sBad,
    sectors: mss?.sectors && typeof mss.sectors === 'object' ? Object.keys(mss.sectors).length : null,
  };
}

// Same aggregation as build_lthcs_site_index._pillar_flags().
function pillarFlags(variableDetail) {
  if (!Array.isArray(variableDetail?.variables)) return null;
  const by = {};
  for (const v of variableDetail.variables) {
    if (v && v.pillar) (by[v.pillar] ||= []).push(v);
  }
  const out = {};
  for (const [pillar, rows] of Object.entries(by)) {
    const first = rows[0].data_quality || {};
    const flags = {};
    for (const [k, sample] of Object.entries(first)) {
      if (typeof sample !== 'boolean') continue;
      flags[k] = rows.filter((r) => r.data_quality?.[k] === true).length;
    }
    out[pillar] = { total: rows.length, flags };
  }
  return out;
}

/* ======================================================================
   Main bootstrap
   ====================================================================== */
async function main() {
  const loading = $('health-loading');
  const errBox = $('health-error');
  const content = $('health-content');

  // Fast path: the deploy-time summary carries every count this page shows
  // (~10 KB). The old path downloaded 30 days of snapshot + insider + 13F
  // JSON (~50 MB, ~200 requests) only to count keys in the browser.
  const summary = await tryFetch(`${DATA_ROOT}/health_summary.json`);
  let datesDesc, latest, rows, ctx;
  if (summary && Array.isArray(summary.days) && summary.days.length
      && Array.isArray(summary.snapshot_dates) && summary.snapshot_dates.length) {
    datesDesc = [...summary.snapshot_dates].sort().reverse();
    latest = summary.latest || datesDesc[0];
    rows = summary.days;
    ctx = {
      variableDetailDate: summary.variable_detail ? summary.variable_detail.calc_date : null,
      pillars: summary.variable_detail ? summary.variable_detail.pillars : null,
      analyst: summary.analyst,
      trends: summary.trends,
      universeSize: summary.universe_size,
    };
  } else {
    const r = await loadFromRawFiles();
    if (!r) {
      loading.classList.add('hidden');
      errBox.classList.remove('hidden');
      // Phase 2 polish: friendlier copy + back-link so a cold visitor who hits
      // this page on a fresh deploy has somewhere to go instead of staring at
      // a dead-end error string.
      errBox.innerHTML =
        '<strong>Waiting on the first pipeline run.</strong><br>' +
        'The daily LTHCS pipeline cron has not produced any snapshots yet ' +
        '(<code>data/lthcs/snapshots/index.json</code> is empty or missing). ' +
        'Cron runs at 23:00 UTC. ' +
        '<a href="../" class="lthcs-footer-link">&larr; Back to card view</a>';
      return;
    }
    ({ datesDesc, latest, rows, ctx } = r);
  }

  const today = rows[0] || dayRow(latest, {});
  const universeSize = ctx.universeSize || today.tickers || 168;

  renderHeader(latest, { today, variableDetailDate: ctx.variableDetailDate, analyst: ctx.analyst });
  renderCadence(datesDesc, latest);
  renderSourceToday({ today, analyst: ctx.analyst, trends: ctx.trends, universeSize });
  renderSourceHistory(rows, universeSize);
  renderPillarBreakdown(ctx.pillars);
  renderRecentRuns(rows.slice(0, 14));

  loading.classList.add('hidden');
  content.classList.remove('hidden');
}

/* Fallback (local dev / summary not built): read the raw files, but only the
   ones that exist — the deploy-time file index (when present) says which, so
   optional stages such as sector_strength / analyst_breadth no longer 404 on
   every date. */
async function loadFromRawFiles() {
  const snapIndex = await tryFetch(`${DATA_ROOT}/snapshots/index.json`);
  if (!snapIndex || !Array.isArray(snapIndex.dates) || snapIndex.dates.length === 0) return null;
  // Dates in the index are newest-first per producer convention.
  const datesDesc = [...snapIndex.dates].sort().reverse();
  const latest = datesDesc[0];
  const fileIdx = await loadFileIndex();
  const has = (key, d) => {
    if (!fileIdx) return true;            // unknown -> try it
    const e = fileIdx.dated[key];
    return !!(e && e.dates.indexOf(d) !== -1);
  };
  const get = (key, d, url) => (has(key, d) ? tryFetch(url) : Promise.resolve(null));
  const weeks = fileIdx && fileIdx.weekly && fileIdx.weekly.trends ? fileIdx.weekly.trends.weeks : null;

  const [variableDetail, analystToday, trendsLatest, universe] = await Promise.all([
    get('variable_detail', latest, `${DATA_ROOT}/variable_detail/${latest}.json`),
    get('analyst_breadth', latest, `${DATA_ROOT}/analyst_breadth/${latest}.json`),
    weeks ? (weeks.length ? tryFetch(`${DATA_ROOT}/trends/${weeks[0]}.json`) : Promise.resolve(null))
      : fetchLatestWeeklyTrends(latest),
    tryFetch(`${DATA_ROOT}/universe.json`),
  ]);

  // 30-day history: per-date snapshot + insider + holdings + macro files.
  const historyDates = datesDesc.slice(0, 30);
  const rows = await Promise.all(historyDates.map(async (d) => {
    const [snap, ins, hol, mb, mbs, mss] = await Promise.all([
      get('snapshots', d, `${DATA_ROOT}/snapshots/${d}.json`),
      get('insider', d, `${DATA_ROOT}/insider/${d}.json`),
      get('holdings', d, `${DATA_ROOT}/holdings/${d}.json`),
      get('macro/breadth', d, `${DATA_ROOT}/macro/breadth_${d}.json`),
      get('macro/breadth_sentiment', d, `${DATA_ROOT}/macro/breadth_sentiment_${d}.json`),
      get('macro/sector_strength', d, `${DATA_ROOT}/macro/sector_strength_${d}.json`),
    ]);
    return dayRow(d, { snap, ins, hol, mb, mbs, mss });
  }));

  return {
    datesDesc,
    latest,
    rows,
    ctx: {
      variableDetailDate: variableDetail ? (variableDetail.calc_date || latest) : null,
      pillars: pillarFlags(variableDetail),
      analyst: analystToday && typeof analystToday === 'object' ? Object.keys(analystToday).length : null,
      trends: trendsLatest ? {
        as_of: trendsLatest.as_of || null,
        tickers: trendsLatest.tickers ? Object.keys(trendsLatest.tickers).length : 0,
        terms: trendsLatest.term_map ? Object.keys(trendsLatest.term_map).length : 0,
      } : null,
      universeSize: universe?.tickers?.length || null,
    },
  };
}

/* ----- Trends file is weekly, not daily. Resolve the ISO week. -------- */
async function fetchLatestWeeklyTrends(latestDate) {
  // Compute the ISO week for `latestDate`. Trends are named YYYY-W##.json.
  const d = parseISODate(latestDate);
  // ISO week algorithm: Thursday-anchored.
  const target = new Date(d.valueOf());
  const dayNr = (target.getUTCDay() + 6) % 7;
  target.setUTCDate(target.getUTCDate() - dayNr + 3);
  const firstThursday = new Date(Date.UTC(target.getUTCFullYear(), 0, 4));
  const diff = (target - firstThursday) / (24 * 3600 * 1000);
  const week = 1 + Math.round((diff - 3 + ((firstThursday.getUTCDay() + 6) % 7)) / 7);
  const wkStr = `${target.getUTCFullYear()}-W${String(week).padStart(2, '0')}`;
  return await tryFetch(`${DATA_ROOT}/trends/${wkStr}.json`);
}

/* ======================================================================
   Section renderers
   ====================================================================== */

// Freshness stamp for the pipeline-health header.
//
// This used to read `${latest} · ${isoToday()}` — a real snapshot date glued
// to a live clock read. The clock half was not a stamp at all: it aged by
// zero days no matter how long the cron had been dead, which is exactly the
// failure mode the honesty contract exists to prevent.
//
// The replacement is a composite over the pipeline outputs this page actually
// probes, so the headline is the OLDEST of them (Rule 2). A source whose file
// is missing for `latest` has no date and is disclosed as undated (Rule 4) —
// never quietly skipped, because a missing source is the worst case, not the
// best one.
function renderHeader(latest, sources) {
  const s = sources || {};
  const t = s.today || {};
  const components = [
    { label: 'snapshot', date: t.snapshot ? (t.snapshot_calc_date || latest) : null },
    { label: 'pillars', date: s.variableDetailDate || null },
    { label: 'macro', date: t.macro_ok != null || t.macro_as_of ? (t.macro_as_of || latest) : null },
    { label: 'insider', date: t.insider != null ? latest : null },
    { label: '13F', date: t.holdings_covered != null ? latest : null },
    {
      // Sector strength and analyst breadth are optional pipeline stages that
      // do not run every day. They are disclosed but do not age the headline,
      // because the page renders "not produced today" for them either way.
      label: 'sectors',
      date: t.sectors != null ? latest : null,
      contributes: false,
      tag: 'optional',
      note: 'optional stage',
    },
    {
      label: 'analysts',
      date: s.analyst != null ? latest : null,
      contributes: false,
      tag: 'optional',
      note: 'optional stage',
    },
  ];
  paintComposite($('health-generated'), components, {
    detailEl: $('health-fresh-note'),
    what: 'This pipeline-health view',
    baseClass: 'lthcs-meta-value',
    title: 'Newest snapshot on disk: ' + latest + '.',
  });
}

function renderCadence(datesDesc, latest) {
  const today = isoToday();
  const lagDays = daysBetween(latest, today);
  const lagHrs = hoursAgoISO(latest);

  // Last run pill.
  const status = lagDays === 0 ? 'ok' : lagDays === 1 ? 'warn' : 'fail';
  const statusGlyph = status === 'ok' ? '✓' : status === 'warn' ? '!' : '✗';
  const lagLabel = lagDays === 0
    ? `today; ${Math.max(lagHrs, 0)}h ago`
    : lagDays === 1
      ? '1 day ago'
      : `${lagDays} days ago`;
  const lastDd = $('cad-last-run');
  lastDd.replaceChildren(
    document.createTextNode(`${latest} (${lagLabel}) `),
    el('span', { class: 'lhealth-pill', 'data-status': status, text: statusGlyph }),
  );

  // Cron line is static.
  $('cad-cron').textContent = `${CRON_EXPR} (${CRON_HUMAN})`;

  // Drift detection — walk last-30-day window of dates.
  const cutoff = parseISODate(today);
  cutoff.setUTCDate(cutoff.getUTCDate() - 30);
  const recent = datesDesc
    .filter((d) => parseISODate(d) >= cutoff)
    .sort(); // ascending for gap math.
  const missed = [];
  for (let i = 1; i < recent.length; i += 1) {
    const gap = daysBetween(recent[i - 1], recent[i]);
    if (gap > 1) {
      // Fill in the missed days between.
      for (let g = 1; g < gap; g += 1) {
        const m = new Date(parseISODate(recent[i - 1]).valueOf());
        m.setUTCDate(m.getUTCDate() + g);
        missed.push(m.toISOString().slice(0, 10));
      }
    }
  }
  // Also flag gap between latest and today if applicable.
  if (lagDays > 1) {
    for (let g = 1; g < lagDays; g += 1) {
      const m = new Date(parseISODate(latest).valueOf());
      m.setUTCDate(m.getUTCDate() + g);
      missed.push(m.toISOString().slice(0, 10));
    }
  }

  const driftDd = $('cad-drift');
  if (missed.length === 0) {
    driftDd.replaceChildren(
      document.createTextNode('no missed days in last 30 days '),
      el('span', { class: 'lhealth-pill', 'data-status': 'ok', text: '✓' }),
    );
  } else {
    const shown = missed.slice(0, 6).join(', ');
    const more = missed.length > 6 ? ` +${missed.length - 6} more` : '';
    driftDd.replaceChildren(
      document.createTextNode(`${missed.length} missed day${missed.length === 1 ? '' : 's'} (${shown}${more}) `),
      el('span', { class: 'lhealth-pill', 'data-status': missed.length > 2 ? 'fail' : 'warn', text: '!' }),
    );
  }

  // Catch-up — we can detect synthetic / forward-filled snapshots heuristically
  // by looking for the same calc_date on consecutive snapshots, but the
  // current schema doesn't expose a "synthetic" flag. For now, report
  // 0 if we have no gaps; otherwise note the gap count. This stays honest
  // without making up data.
  $('cad-catchup').textContent = missed.length === 0
    ? '0 catch-up days needed since last run'
    : `${missed.length} catch-up day${missed.length === 1 ? '' : 's'} would be required (run with --catch-up)`;
}

/* ----- Today's source coverage ---------------------------------------- */
function renderSourceToday(ctx) {
  const { today, analyst, trends, universeSize } = ctx;
  const t = today || {};

  const tickerCount = t.tickers || 0;

  // Yahoo coverage — proxy by counting tickers in the snapshot
  // (scores list is produced from the Yahoo-fed price/momentum series).
  const yahooCovered = tickerCount;

  // SEC EDGAR (XBRL) — the financial pillar always runs, so the snapshot
  // ticker count is the lower-bound estimate.
  const xbrlCovered = tickerCount;

  // SEC Form 4 (insider) — keys in insider/<date>.json.
  const insiderCovered = t.insider || 0;

  // SEC 13F (holdings) — entries with manager_count > 0.
  const holdingsCovered = t.holdings_covered || 0;

  // FRED macro — breadth file's data_quality.sources_ok.
  const fredOk = t.macro_ok ?? 0;
  const fredTotal = (t.macro_ok ?? 0) + (t.macro_failed ?? 0) || 4; // DXY, HY OAS, IG OAS, 2s10s

  // Sector ETFs — count sectors in sector_strength.
  const sectorCount = t.sectors || 0;
  const sectorTotal = 11; // XLB/XLC/XLE/XLF/XLI/XLK/XLP/XLRE/XLU/XLV/XLY

  // Breadth sentiment — sources_ok inside breadth_sentiment.
  const sentOk = t.sentiment_ok ?? 0;
  const sentTotal = (t.sentiment_ok ?? 0) + (t.sentiment_failed ?? 0) || 3; // AAII, NAAIM, put/call

  // Google Trends — count term_map entries.
  const trendsCovered = trends ? (trends.tickers || 0) : 0;
  const trendsTotal = trends && trends.terms ? trends.terms : 30;

  // Analyst breadth — tickers with an entry. Universe size is the
  // denominator; coverage is intentionally sparse (Alpha Vantage quirk).
  const analystCovered = analyst || 0;
  const analystTotal = universeSize;

  const sources = [
    { name: 'Yahoo Finance (prices)', covered: yahooCovered, total: universeSize },
    { name: 'SEC EDGAR (XBRL)', covered: xbrlCovered, total: universeSize, note: 'denominator = scored tickers; XBRL feeds revenue/OCF/margin' },
    { name: 'SEC Form 4 (insider)', covered: insiderCovered, total: universeSize },
    { name: 'SEC 13F (holdings)', covered: holdingsCovered, total: universeSize, note: 'sparse on smaller-caps by design' },
    { name: 'FRED (macro)', covered: fredOk, total: fredTotal },
    { name: 'Sector ETFs', covered: sectorCount, total: sectorTotal },
    { name: 'Breadth sentiment', covered: sentOk, total: sentTotal },
    { name: 'Google Trends', covered: trendsCovered, total: trendsTotal, note: trendsCovered === 0 ? 'no terms covered — rate-limited?' : null },
    { name: 'Alpha Vantage NEWS_SENTIMENT', covered: analystCovered, total: analystTotal, note: 'AND-not-OR quirk; Thesis neutral 50 in CI' },
  ];

  const container = $('src-today');
  container.replaceChildren();
  for (const s of sources) {
    const pct = s.total > 0 ? Math.round((s.covered / s.total) * 100) : 0;
    const tier = tierFor(pct);
    const row = el('div', { class: 'lhealth-src-row' });
    row.appendChild(el('div', { class: 'lhealth-src-name', text: s.name }));
    const bar = el('div', { class: 'lhealth-src-bar' });
    const fill = el('div', { class: 'lhealth-src-bar-fill', 'data-tier': tier });
    fill.style.width = `${pct}%`;
    bar.appendChild(fill);
    row.appendChild(bar);
    const stat = el('div', { class: 'lhealth-src-stat' });
    stat.appendChild(el('strong', { text: `${s.covered}/${s.total}` }));
    stat.appendChild(document.createTextNode(` (${pct}%)`));
    row.appendChild(stat);
    if (s.note) {
      row.appendChild(el('div', { class: 'lhealth-src-note', text: s.note }));
    }
    container.appendChild(row);
  }
}

/* ----- 30-day history grid -------------------------------------------- */
function renderSourceHistory(history, universeSize) {
  // history is newest-first; render oldest -> newest so the chart reads
  // left-to-right chronologically.
  const ordered = [...history].reverse();

  // For each source, build a 30-day cell array (null = no file that day).
  const pctOf = (ok, bad, dflt) => {
    if (ok == null) return null;
    const tot = ok + (bad ?? 0) || dflt;
    return (ok / tot) * 100;
  };
  const sources = [
    { name: 'Yahoo / snapshots', pct: (h) => (h.tickers ? (h.tickers / universeSize) * 100 : null) },
    { name: 'SEC Form 4 (insider)', pct: (h) => (h.insider != null ? (h.insider / universeSize) * 100 : null) },
    { name: 'SEC 13F (holdings)', pct: (h) => (h.holdings_covered != null ? (h.holdings_covered / universeSize) * 100 : null) },
    { name: 'FRED (macro)', pct: (h) => pctOf(h.macro_ok, h.macro_failed, 4) },
    { name: 'Breadth sentiment', pct: (h) => pctOf(h.sentiment_ok, h.sentiment_failed, 3) },
    { name: 'Sector ETFs', pct: (h) => (h.sectors != null ? (h.sectors / 11) * 100 : null) },
  ];

  const container = $('src-history');
  container.replaceChildren();

  // We always render 30 cells; if history has fewer dates, pad with
  // "missing" tiles on the left so the bar still spans the row.
  const pad = Math.max(0, 30 - ordered.length);

  for (const s of sources) {
    const row = el('div', { class: 'lhealth-history-row' });
    row.appendChild(el('div', { class: 'lhealth-history-row-label', text: s.name }));
    const cells = el('div', { class: 'lhealth-history-cells' });
    for (let i = 0; i < pad; i += 1) {
      cells.appendChild(el('div', { class: 'lhealth-history-cell', 'data-tier': 'missing', title: 'no snapshot' }));
    }
    for (const h of ordered) {
      const pct = s.pct(h);
      const cell = el('div', { class: 'lhealth-history-cell' });
      if (pct === null || Number.isNaN(pct)) {
        cell.setAttribute('data-tier', 'missing');
        cell.title = `${h.date}: no data`;
      } else {
        cell.setAttribute('data-tier', tierFor(pct));
        cell.title = `${h.date}: ${Math.round(pct)}%`;
      }
      cells.appendChild(cell);
    }
    row.appendChild(cells);
    container.appendChild(row);
  }
}

/* ----- Per-pillar data quality ---------------------------------------- */
function renderPillarBreakdown(pillars) {
  const container = $('pillar-breakdown');
  container.replaceChildren();

  if (!pillars || !Object.keys(pillars).length) {
    container.appendChild(el('p', { class: 'lhealth-note', text: 'No variable_detail file available for today.' }));
    return;
  }

  // Display order matches the spec (Adoption / Institutional / Financial / Thesis / DES).
  const pillarOrder = [
    ['adoption_momentum', 'Adoption'],
    ['institutional_confidence', 'Institutional'],
    ['financial_evolution', 'Financial'],
    ['thesis_integrity', 'Thesis'],
    ['des', 'DES'],
  ];

  for (const [key, label] of pillarOrder) {
    const p = pillars[key];
    if (!p || !p.total) continue;
    const card = el('div', { class: 'lhealth-pillar' });
    card.appendChild(el('h3', { class: 'lhealth-pillar-title', text: label }));
    // Count of tickers where each boolean data_quality flag is true.
    for (const [k, trueCount] of Object.entries(p.flags || {})) {
      const stat = el('div', { class: 'lhealth-pillar-stat' });
      stat.appendChild(el('span', { text: prettyFlag(k) }));
      stat.appendChild(el('span', { text: `${trueCount}/${p.total}` }));
      card.appendChild(stat);
    }
    container.appendChild(card);
  }
}

function prettyFlag(k) {
  // has_revenue -> "with revenue", article_count_sufficient -> "article count sufficient"
  if (k.startsWith('has_')) return `with ${k.slice(4).replace(/_/g, ' ')}`;
  return k.replace(/_/g, ' ');
}

/* ----- Recent runs ----------------------------------------------------- */
function renderRecentRuns(history14) {
  const tbody = $('runs-tbody');
  tbody.replaceChildren();

  for (const h of history14) {
    const tr = el('tr');
    tr.appendChild(el('td', { text: h.date }));

    // Status — green if snapshot loaded; warn if partial (< 100 tickers);
    // fail if no snapshot at all.
    const tickers = h.tickers || 0;
    let status = 'ok';
    let glyph = '✓';
    if (!h.snapshot) { status = 'fail'; glyph = '✗'; }
    else if (tickers < 100) { status = 'warn'; glyph = '!'; }
    const stTd = el('td');
    stTd.appendChild(el('span', { class: 'lhealth-pill', 'data-status': status, text: glyph }));
    tr.appendChild(stTd);

    tr.appendChild(el('td', { text: tickers > 0 ? `${tickers}` : 'n/a' }));

    // Compute time would come from a per-date pipeline_metrics.json that
    // the daily action doesn't currently emit. Show "n/a" honestly until
    // the metrics file is added.
    tr.appendChild(el('td', { text: 'n/a' }));

    tbody.appendChild(tr);
  }
}

/* ======================================================================
   Auto-refresh every hour, matching pages.yml cron cadence. The page is
   cheap (a few hundred small JSON GETs) and the data only changes when
   pages.yml deploys, so hourly is plenty.
   ====================================================================== */
main().catch((e) => {
  console.error('[lthcs-health] fatal', e);
  $('health-loading')?.classList.add('hidden');
  const errBox = $('health-error');
  if (errBox) {
    errBox.classList.remove('hidden');
    errBox.textContent = `Failed to render pipeline health: ${e.message || e}`;
  }
});

setTimeout(() => { window.location.reload(); }, 60 * 60 * 1000);
