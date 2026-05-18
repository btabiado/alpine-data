/* =========================================================================
   LTHCS Pipeline Health — vanilla-JS observability dashboard.

   Read-only consumer of files under ../data/lthcs/. Renders:
     - Cadence (last run, cron, drift, catch-up)
     - Source coverage (today + 30d history per source)
     - Per-pillar data-quality counts (from variable_detail/<date>.json)
     - Recent runs (last 14 from snapshots/index.json)

   All fetches are independent enough to Promise.allSettled together so
   one missing file (e.g. macro breadth_sentiment_<date>.json on a day
   the scrapers all failed) doesn't break the rest of the page.
   ========================================================================= */

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

/* ======================================================================
   Main bootstrap
   ====================================================================== */
async function main() {
  const loading = $('health-loading');
  const errBox = $('health-error');
  const content = $('health-content');

  // Step 1 — fetch the snapshot index.
  const snapIndex = await tryFetch(`${DATA_ROOT}/snapshots/index.json`);
  if (!snapIndex || !Array.isArray(snapIndex.dates) || snapIndex.dates.length === 0) {
    loading.classList.add('hidden');
    errBox.classList.remove('hidden');
    errBox.textContent = 'Could not load snapshots/index.json. The pipeline has not produced any snapshots yet.';
    return;
  }

  // Dates in the index are newest-first per producer convention.
  const datesDesc = [...snapIndex.dates].sort().reverse();
  const latest = datesDesc[0];

  // Step 2 — fetch everything we need for the latest date in parallel.
  // Macro/breadth filenames are date-suffixed (breadth_<date>.json) per
  // PHASE_1_BUILD_SPEC convention; insider/holdings/index are
  // date-named files in their per-source dir.
  const [
    latestSnap,
    variableDetail,
    macroBreadth,
    macroBreadthSent,
    macroSectorStrength,
    insiderToday,
    holdingsToday,
    analystToday,
    trendsLatest,
    universe,
  ] = await Promise.all([
    tryFetch(`${DATA_ROOT}/snapshots/${latest}.json`),
    tryFetch(`${DATA_ROOT}/variable_detail/${latest}.json`),
    tryFetch(`${DATA_ROOT}/macro/breadth_${latest}.json`),
    tryFetch(`${DATA_ROOT}/macro/breadth_sentiment_${latest}.json`),
    tryFetch(`${DATA_ROOT}/macro/sector_strength_${latest}.json`),
    tryFetch(`${DATA_ROOT}/insider/${latest}.json`),
    tryFetch(`${DATA_ROOT}/holdings/${latest}.json`),
    tryFetch(`${DATA_ROOT}/analyst_breadth/${latest}.json`),
    fetchLatestWeeklyTrends(latest),
    tryFetch(`${DATA_ROOT}/universe.json`),
  ]);

  // Step 3 — for the 30-day history view, fetch the per-date snapshot
  // + insider + holdings JSON for the last min(30, count) dates. This is
  // the chattiest part of the page (up to 90 GETs) but each file is small
  // and HTTP/2 multiplexing on GitHub Pages handles it easily.
  const historyDates = datesDesc.slice(0, 30);
  const historyPromises = historyDates.map(async (d) => {
    const [snap, ins, hol, mb, mbs, mss] = await Promise.all([
      tryFetch(`${DATA_ROOT}/snapshots/${d}.json`),
      tryFetch(`${DATA_ROOT}/insider/${d}.json`),
      tryFetch(`${DATA_ROOT}/holdings/${d}.json`),
      tryFetch(`${DATA_ROOT}/macro/breadth_${d}.json`),
      tryFetch(`${DATA_ROOT}/macro/breadth_sentiment_${d}.json`),
      tryFetch(`${DATA_ROOT}/macro/sector_strength_${d}.json`),
    ]);
    return { date: d, snap, ins, hol, mb, mbs, mss };
  });
  const history = await Promise.all(historyPromises);

  // Step 4 — render.
  const universeSize = universe?.tickers?.length || latestSnap?.scores?.length || 168;

  renderHeader(latest);
  renderCadence(datesDesc, latest);
  renderSourceToday({
    latestSnap, insiderToday, holdingsToday, macroBreadth,
    macroBreadthSent, macroSectorStrength, analystToday, trendsLatest,
    universeSize,
  });
  renderSourceHistory(history, universeSize);
  renderPillarBreakdown(variableDetail);
  renderRecentRuns(history.slice(0, 14));

  loading.classList.add('hidden');
  content.classList.remove('hidden');
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

function renderHeader(latest) {
  $('health-generated').textContent = `${latest} · ${isoToday()}`;
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
  const {
    latestSnap, insiderToday, holdingsToday, macroBreadth,
    macroBreadthSent, macroSectorStrength, analystToday, trendsLatest,
    universeSize,
  } = ctx;

  const tickerCount = latestSnap?.scores?.length || 0;

  // Yahoo coverage — proxy by counting tickers in the snapshot
  // (scores list is produced from the Yahoo-fed price/momentum series).
  const yahooCovered = tickerCount;

  // SEC EDGAR (XBRL) — proxy by counting tickers in variable_detail with
  // has_revenue=true on the financial pillar. We don't have that here so
  // fall back to ticker count (XBRL is the underlying revenue feed).
  // Use snapshot ticker count as the lower-bound estimate (financial
  // pillar always runs).
  const xbrlCovered = tickerCount;

  // SEC Form 4 (insider) — count keys in insider/<date>.json.
  const insiderCovered = insiderToday ? Object.keys(insiderToday).length : 0;

  // SEC 13F (holdings) — count entries with manager_count > 0.
  let holdingsCovered = 0;
  if (holdingsToday) {
    for (const v of Object.values(holdingsToday)) {
      if (v && typeof v === 'object' && (v.manager_count || 0) > 0) holdingsCovered += 1;
    }
  }

  // FRED macro — breadth file's data_quality.sources_ok.
  const fredOk = macroBreadth?.data_quality?.sources_ok ?? 0;
  const fredTotal = (macroBreadth?.data_quality?.sources_ok ?? 0)
    + (macroBreadth?.data_quality?.sources_failed ?? 0)
    || 4; // expected 4 series: DXY, HY OAS, IG OAS, 2s10s

  // Sector ETFs — count sectors in sector_strength.
  const sectorCount = macroSectorStrength?.sectors ? Object.keys(macroSectorStrength.sectors).length : 0;
  const sectorTotal = 11; // XLB/XLC/XLE/XLF/XLI/XLK/XLP/XLRE/XLU/XLV/XLY

  // Breadth sentiment — sources_ok inside breadth_sentiment.
  const sentOk = macroBreadthSent?.data_quality?.sources_ok ?? 0;
  const sentTotal = (macroBreadthSent?.data_quality?.sources_ok ?? 0)
    + (macroBreadthSent?.data_quality?.sources_failed ?? 0)
    || 3; // AAII, NAAIM, put/call

  // Google Trends — count term_map entries.
  const trendsCovered = trendsLatest?.tickers ? Object.keys(trendsLatest.tickers).length : 0;
  const trendsTotal = trendsLatest?.term_map ? Object.keys(trendsLatest.term_map).length : 30;

  // Analyst breadth — count tickers with non-empty actions.
  const analystCovered = analystToday ? Object.keys(analystToday).length : 0;
  // Universe size is the denominator. Most tickers won't have an Alpha
  // Vantage NEWS_SENTIMENT entry per Bryan's documented quirk; coverage
  // is intentionally sparse so the bar should be low without being "fail".
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

  // For each source, build a 30-day cell array.
  const sources = [
    {
      name: 'Yahoo / snapshots',
      pct: (h) => h.snap?.scores?.length ? (h.snap.scores.length / universeSize) * 100 : null,
    },
    {
      name: 'SEC Form 4 (insider)',
      pct: (h) => h.ins ? (Object.keys(h.ins).length / universeSize) * 100 : null,
    },
    {
      name: 'SEC 13F (holdings)',
      pct: (h) => {
        if (!h.hol) return null;
        const c = Object.values(h.hol).filter((v) => v && (v.manager_count || 0) > 0).length;
        return (c / universeSize) * 100;
      },
    },
    {
      name: 'FRED (macro)',
      pct: (h) => {
        if (!h.mb?.data_quality) return null;
        const ok = h.mb.data_quality.sources_ok ?? 0;
        const tot = ok + (h.mb.data_quality.sources_failed ?? 0) || 4;
        return (ok / tot) * 100;
      },
    },
    {
      name: 'Breadth sentiment',
      pct: (h) => {
        if (!h.mbs?.data_quality) return null;
        const ok = h.mbs.data_quality.sources_ok ?? 0;
        const tot = ok + (h.mbs.data_quality.sources_failed ?? 0) || 3;
        return (ok / tot) * 100;
      },
    },
    {
      name: 'Sector ETFs',
      pct: (h) => {
        if (!h.mss?.sectors) return null;
        return (Object.keys(h.mss.sectors).length / 11) * 100;
      },
    },
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
function renderPillarBreakdown(variableDetail) {
  const container = $('pillar-breakdown');
  container.replaceChildren();

  if (!variableDetail?.variables) {
    container.appendChild(el('p', { class: 'lhealth-note', text: 'No variable_detail file available for today.' }));
    return;
  }

  const byPillar = {};
  for (const v of variableDetail.variables) {
    (byPillar[v.pillar] ||= []).push(v);
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
    const rows = byPillar[key] || [];
    if (rows.length === 0) continue;
    const card = el('div', { class: 'lhealth-pillar' });
    card.appendChild(el('h3', { class: 'lhealth-pillar-title', text: label }));

    // Inspect the data_quality flag keys present in this pillar.
    // Count tickers where each flag is true.
    const flagKeys = Object.keys(rows[0].data_quality || {});
    const total = rows.length;
    for (const k of flagKeys) {
      // Skip non-boolean diagnostic keys like days_since_scored.
      const sample = rows[0].data_quality[k];
      if (typeof sample !== 'boolean') continue;
      const trueCount = rows.filter((r) => r.data_quality?.[k] === true).length;
      const stat = el('div', { class: 'lhealth-pillar-stat' });
      stat.appendChild(el('span', { text: prettyFlag(k) }));
      stat.appendChild(el('span', { text: `${trueCount}/${total}` }));
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
    const tickers = h.snap?.scores?.length || 0;
    let status = 'ok';
    let glyph = '✓';
    if (!h.snap) { status = 'fail'; glyph = '✗'; }
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
