/* =========================================================================
   LTHCS Backtest A/B view — Phase 4.

   Read-only consumer of the latest ``data/lthcs/backtest/ab_<id>/`` run.
   Companion to ``lthcs-backtest.js`` (the validator). The data shape is
   produced by ``scripts/lthcs_backtest_ab.py``:

     ab_latest.json          — pointer { run_id, path, verdict, ... }
     ab_<id>/comparison.json — both summaries + delta_table + verdict
     ab_<id>/equity_curve_a.json
     ab_<id>/equity_curve_b.json
     ab_<id>/config_a.json
     ab_<id>/config_b.json

   The renderer is intentionally tiny: one overlay chart, one delta table,
   one pillar-weight diff grid. Empty-state placeholder cards keep the
   page usable when no A/B run exists yet.
   ========================================================================= */

const DATA_ROOT = '../data/lthcs';
const BACKTEST_ROOT = `${DATA_ROOT}/backtest`;
const LAST_RUN_KEY = 'lthcs.backtest.ab.last_run';

const PILLAR_ORDER = [
  'adoption_momentum',
  'institutional_confidence',
  'financial_evolution',
  'thesis_integrity',
  'des',
];
const PILLAR_LABELS = {
  adoption_momentum: 'Adoption',
  institutional_confidence: 'Institutional',
  financial_evolution: 'Financial',
  thesis_integrity: 'Thesis',
  des: 'DES',
};

/* ----- DOM helpers ------------------------------------------------------ */
function $(id) { return document.getElementById(id); }
function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') node.className = v;
    else if (k === 'text') node.textContent = v;
    else node.setAttribute(k, v);
  }
  for (const c of (Array.isArray(children) ? children : [children])) {
    if (c == null) continue;
    node.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
  }
  return node;
}
function svgEl(tag, attrs = {}) {
  const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  return node;
}

/* ----- Fetch helpers ---------------------------------------------------- */
async function tryFetch(url) {
  try {
    const r = await fetch(url, { cache: 'no-cache' });
    if (!r.ok) return null;
    return await r.json();
  } catch { return null; }
}

/* ----- Formatters ------------------------------------------------------- */
function fmtBy(fmt, x) {
  if (x == null || Number.isNaN(x)) return 'n/a';
  if (fmt === 'pct')   return `${x >= 0 ? '+' : ''}${(x * 100).toFixed(2)}%`;
  if (fmt === 'ratio') return `${x >= 0 ? '+' : ''}${x.toFixed(3)}`;
  if (fmt === 'days')  return `${x.toFixed(1)}d`;
  if (fmt === 'int')   return `${Math.round(x)}`;
  return x.toFixed(4);
}
function fmtPctSigned(x) {
  if (x == null || Number.isNaN(x)) return 'n/a';
  return `${x >= 0 ? '+' : ''}${(x * 100).toFixed(2)}%`;
}
function fmtSharpe(x) {
  if (x == null || Number.isNaN(x)) return 'n/a';
  return `${x >= 0 ? '+' : ''}${x.toFixed(3)}`;
}

/* ----- Placeholder card builder ---------------------------------------- */
function makePlaceholder(msg) {
  const wrap = el('div', { class: 'lbt-placeholder' });
  wrap.appendChild(el('p', { class: 'lbt-placeholder-msg', text: msg }));
  return wrap;
}

/* ======================================================================
   Boot — locate the latest A/B run, then render each section.
   ====================================================================== */
async function main() {
  const loading = $('ab-loading');
  const errBox = $('ab-error');
  const content = $('ab-content');

  const latest = await tryFetch(`${BACKTEST_ROOT}/ab_latest.json`);
  if (!latest || !latest.path) {
    loading.classList.add('hidden');
    content.classList.remove('hidden');
    renderEmptyState();
    return;
  }

  // Persist the last successfully loaded run dir so a soft reload can
  // surface the same artifact even if the latest pointer races a write.
  try { window.localStorage.setItem(LAST_RUN_KEY, latest.path); } catch (_e) { /* ignore */ }

  const runRoot = `../${latest.path}`;
  const [comparison, equityA, equityB, cfgA, cfgB] = await Promise.all([
    tryFetch(`${runRoot}/comparison.json`),
    tryFetch(`${runRoot}/equity_curve_a.json`),
    tryFetch(`${runRoot}/equity_curve_b.json`),
    tryFetch(`${runRoot}/config_a.json`),
    tryFetch(`${runRoot}/config_b.json`),
  ]);

  if (!comparison) {
    loading.classList.add('hidden');
    errBox.classList.remove('hidden');
    errBox.textContent = `comparison.json missing or invalid at ${runRoot}`;
    return;
  }

  loading.classList.add('hidden');
  content.classList.remove('hidden');

  // Header.
  $('ab-generated').textContent = comparison.generated_at?.split('T')[0] ?? '?';
  const sub = `${comparison.label_a}  vs.  ${comparison.label_b}`;
  $('ab-subtitle').textContent = sub;

  renderVerdict(comparison);
  renderEquityOverlay(comparison, equityA || {}, equityB || {});
  renderStatsTable(comparison);
  renderPillarDiff(cfgA, cfgB, comparison);
  renderMetadata(comparison);
}

/* ======================================================================
   Empty state — no A/B run produced yet
   ====================================================================== */
function renderEmptyState() {
  const main = document.querySelector('.lthcs-main');
  if (!main) return;
  const card = el('section', { class: 'lthcs-section lbt-section lab-empty' });
  card.appendChild(el('h2', { class: 'lbt-h2', text: 'No A/B run available yet' }));
  card.appendChild(el('p', { class: 'lbt-note', text:
    "Produce one with: python scripts/lthcs_backtest_ab.py --baseline data/lthcs/weights.json --candidate diff.json --offline" }));
  card.appendChild(el('p', { class: 'lbt-note', text:
    "Output lands under data/lthcs/backtest/ab_<timestamp>/ and ab_latest.json points at the newest run." }));
  main.appendChild(card);
}

/* ======================================================================
   Verdict card
   ====================================================================== */
function renderVerdict(comparison) {
  const v = comparison.verdict || {};
  const sA = comparison.side_a?.summary || {};
  const sB = comparison.side_b?.summary || {};
  const tag = $('ab-verdict-tag');
  const prose = $('ab-verdict-prose');
  const sharpeA = (sA.sharpe != null) ? Number(sA.sharpe) : null;
  const sharpeB = (sB.sharpe != null) ? Number(sB.sharpe) : null;
  const dSharpe = (sharpeA != null && sharpeB != null) ? (sharpeB - sharpeA) : null;

  let label;
  let cls;
  if (v.winner === 'a') { label = `A WINS · ${comparison.label_a}`; cls = 'is-a'; }
  else if (v.winner === 'b') { label = `B WINS · ${comparison.label_b}`; cls = 'is-b'; }
  else { label = 'TIE — calibration change is a wash'; cls = 'is-tie'; }
  tag.textContent = label;
  tag.className = `lab-verdict-tag ${cls}`;

  prose.textContent = (v.metrics_counted != null && v.score_a != null && v.score_b != null)
    ? `Winner-takes-most across ${v.metrics_counted} weighted metrics (Sharpe ×2, others ×1). A=${v.score_a}, B=${v.score_b}.`
    : 'Verdict score unavailable.';

  $('ab-verdict-sharpe-a').textContent = fmtSharpe(sharpeA);
  $('ab-verdict-sharpe-b').textContent = fmtSharpe(sharpeB);
  const dEl = $('ab-verdict-dsharpe');
  dEl.textContent = fmtSharpe(dSharpe);
  if (dSharpe != null) {
    dEl.className = dSharpe > 0.001 ? 'pos' : (dSharpe < -0.001 ? 'neg' : '');
  }
}

/* ======================================================================
   Equity curve overlay — two lines in one chart
   ====================================================================== */
function renderEquityOverlay(comparison, equityA, equityB) {
  const svg = $('ab-chart');
  const legend = $('ab-legend');
  if (!svg || !legend) return;
  svg.replaceChildren();
  legend.replaceChildren();

  const datesA = Object.keys(equityA).sort();
  const datesB = Object.keys(equityB).sort();
  const allDates = Array.from(new Set([...datesA, ...datesB])).sort();
  if (allDates.length < 2) {
    svg.parentNode.appendChild(makePlaceholder('Equity curves too short to chart (need ≥2 trading days on each side).'));
    return;
  }

  // Carry-forward both series onto the union calendar so the chart aligns.
  function carry(series) {
    let last = null;
    return allDates.map((d) => {
      if (series[d] != null) last = series[d];
      return { d, v: last };
    });
  }
  const sA = carry(equityA);
  const sB = carry(equityB);

  const allVals = [...sA, ...sB].map((p) => p.v).filter((v) => v != null);
  if (allVals.length < 2) {
    svg.parentNode.appendChild(makePlaceholder('Equity curves have no overlapping non-null values.'));
    return;
  }
  const vMin = Math.min(...allVals);
  const vMax = Math.max(...allVals);
  const vSpan = (vMax - vMin) || 1;
  const padFrac = 0.05;
  const yMin = vMin - vSpan * padFrac;
  const yMax = vMax + vSpan * padFrac;
  const ySpan = yMax - yMin || 1;

  const VB_W = 800, VB_H = 280;
  const ML = 60, MR = 16, MT = 14, MB = 26;
  const W = VB_W - ML - MR;
  const H = VB_H - MT - MB;

  const xAt = (i, total) => ML + (W * i) / Math.max(1, total - 1);
  const yAt = (v) => MT + H - ((v - yMin) / ySpan) * H;

  // Grid lines (5-tick).
  const gridVals = [yMin, yMin + ySpan / 4, yMin + ySpan / 2, yMin + (3 * ySpan) / 4, yMax];
  if (yMin < 1.0 && yMax > 1.0 && !gridVals.some((g) => Math.abs(g - 1.0) < 1e-6)) gridVals.push(1.0);
  for (const gv of gridVals) {
    const y = yAt(gv);
    const isUnity = Math.abs(gv - 1.0) < 1e-6;
    svg.appendChild(svgEl('line', {
      x1: ML, x2: VB_W - MR, y1: y, y2: y,
      class: isUnity ? 'lbt-pnl-zero' : 'lbt-pnl-grid',
    }));
    const t = svgEl('text', {
      x: ML - 6, y: y + 3,
      class: 'lbt-pnl-yaxis',
      'text-anchor': 'end',
    });
    t.textContent = `${(gv * 100 - 100).toFixed(0)}%`;
    svg.appendChild(t);
  }
  // X-axis labels (3-tick).
  for (const i of [0, Math.floor(allDates.length / 2), allDates.length - 1]) {
    const x = xAt(i, allDates.length);
    const t = svgEl('text', {
      x, y: VB_H - 8,
      class: 'lbt-pnl-xaxis',
      'text-anchor': 'middle',
    });
    t.textContent = allDates[i];
    svg.appendChild(t);
  }

  function pathFor(series) {
    let d = '';
    let first = true;
    series.forEach((p, i) => {
      if (p.v == null) return;
      d += `${first ? 'M' : 'L'}${xAt(i, allDates.length)},${yAt(p.v)} `;
      first = false;
    });
    return d.trim();
  }

  // A first (baseline) then B (candidate) on top, so the variant pops.
  const pathA = pathFor(sA);
  const pathB = pathFor(sB);
  if (pathA) svg.appendChild(svgEl('path', { d: pathA, class: 'lab-line-a' }));
  if (pathB) svg.appendChild(svgEl('path', { d: pathB, class: 'lab-line-b' }));

  // Legend.
  const sumA = comparison.side_a?.summary || {};
  const sumB = comparison.side_b?.summary || {};
  legend.appendChild(el('span', {
    class: 'lbt-legend-item lab-legend-a',
    text: `${comparison.label_a} · total ${fmtPctSigned(sumA.total_return)} · Sharpe ${fmtSharpe(sumA.sharpe)}`,
  }));
  legend.appendChild(el('span', {
    class: 'lbt-legend-item lab-legend-b',
    text: `${comparison.label_b} · total ${fmtPctSigned(sumB.total_return)} · Sharpe ${fmtSharpe(sumB.sharpe)}`,
  }));
}

/* ======================================================================
   Side-by-side stats table
   ====================================================================== */
function renderStatsTable(comparison) {
  $('ab-th-a').textContent = comparison.label_a || 'A';
  $('ab-th-b').textContent = comparison.label_b || 'B';
  const tbody = $('ab-stats-body');
  if (!tbody) return;
  tbody.replaceChildren();
  for (const row of (comparison.delta_table || [])) {
    const tr = el('tr');
    tr.appendChild(el('th', { scope: 'row', text: row.label }));
    const tdA = el('td', { class: 'lab-cell-a' });
    tdA.textContent = fmtBy(row.fmt, row.a);
    if (row.winner === 'a') tdA.classList.add('is-winner');
    tr.appendChild(tdA);
    const tdB = el('td', { class: 'lab-cell-b' });
    tdB.textContent = fmtBy(row.fmt, row.b);
    if (row.winner === 'b') tdB.classList.add('is-winner');
    tr.appendChild(tdB);
    tr.appendChild(el('td', { text: fmtBy(row.fmt, row.delta) }));
    tr.appendChild(el('td', { text: row.pct == null ? 'n/a' : `${row.pct >= 0 ? '+' : ''}${(row.pct * 100).toFixed(2)}%` }));
    const winnerCell = el('td', { class: `lab-winner lab-winner-${row.winner}` });
    let icon;
    if (row.winner === 'a') icon = 'A';
    else if (row.winner === 'b') icon = 'B';
    else if (row.winner === 'tie') icon = '=';
    else icon = '?';
    winnerCell.textContent = icon;
    tr.appendChild(winnerCell);
    tbody.appendChild(tr);
  }
}

/* ======================================================================
   Pillar-weight diff grid — only profiles that differ between A and B
   ====================================================================== */
function renderPillarDiff(cfgA, cfgB, comparison) {
  const container = $('ab-pillar-chart');
  if (!container) return;
  container.replaceChildren();
  const profsA = (cfgA?.profiles) || {};
  const profsB = (cfgB?.profiles) || {};
  const allStages = Array.from(new Set([...Object.keys(profsA), ...Object.keys(profsB)])).sort();

  if (allStages.length === 0) {
    container.appendChild(makePlaceholder('No profile weights in either config.'));
    return;
  }

  const differ = [];
  const identical = [];
  for (const stage of allStages) {
    const wA = profsA[stage] || [];
    const wB = profsB[stage] || [];
    const isSame = wA.length === wB.length && wA.every((v, i) => Math.abs(Number(v) - Number(wB[i] ?? 0)) < 1e-9);
    if (isSame) identical.push(stage); else differ.push({ stage, wA, wB });
  }

  // Differ table.
  if (differ.length > 0) {
    const tableWrap = el('div', { class: 'lbt-table-wrap' });
    const table = el('table', { class: 'lbt-table lab-pillar-table' });
    const thead = el('thead');
    const headRow = el('tr');
    headRow.appendChild(el('th', { scope: 'col', text: 'Profile' }));
    headRow.appendChild(el('th', { scope: 'col', text: 'Side' }));
    for (const p of PILLAR_ORDER) {
      headRow.appendChild(el('th', { scope: 'col', text: PILLAR_LABELS[p] }));
    }
    thead.appendChild(headRow);
    table.appendChild(thead);
    const tbody = el('tbody');
    for (const { stage, wA, wB } of differ) {
      // A row
      const trA = el('tr', { class: 'lab-pillar-row-a' });
      trA.appendChild(el('th', { scope: 'row', rowspan: '3', text: stage }));
      trA.appendChild(el('td', { class: 'lab-pillar-side lab-cell-a', text: 'A' }));
      for (let i = 0; i < PILLAR_ORDER.length; i++) {
        const td = el('td', { text: (wA[i] != null ? Number(wA[i]).toFixed(2) : '–') });
        if (wB[i] != null && wA[i] != null && Math.abs(wA[i] - wB[i]) > 1e-9) td.classList.add('is-diff');
        trA.appendChild(td);
      }
      tbody.appendChild(trA);
      // B row
      const trB = el('tr', { class: 'lab-pillar-row-b' });
      trB.appendChild(el('td', { class: 'lab-pillar-side lab-cell-b', text: 'B' }));
      for (let i = 0; i < PILLAR_ORDER.length; i++) {
        const td = el('td', { text: (wB[i] != null ? Number(wB[i]).toFixed(2) : '–') });
        if (wB[i] != null && wA[i] != null && Math.abs(wA[i] - wB[i]) > 1e-9) td.classList.add('is-diff');
        trB.appendChild(td);
      }
      tbody.appendChild(trB);
      // Delta row
      const trD = el('tr', { class: 'lab-pillar-row-delta' });
      trD.appendChild(el('td', { class: 'lab-pillar-side', text: 'Δ' }));
      for (let i = 0; i < PILLAR_ORDER.length; i++) {
        const a = wA[i] != null ? Number(wA[i]) : null;
        const b = wB[i] != null ? Number(wB[i]) : null;
        const d = (a != null && b != null) ? (b - a) : null;
        const td = el('td');
        td.textContent = d == null ? '–' : (Math.abs(d) < 1e-9 ? '0.00' : `${d > 0 ? '+' : ''}${d.toFixed(2)}`);
        if (d != null) {
          if (d > 1e-9) td.classList.add('pos');
          else if (d < -1e-9) td.classList.add('neg');
        }
        trD.appendChild(td);
      }
      tbody.appendChild(trD);
    }
    table.appendChild(tbody);
    tableWrap.appendChild(table);
    container.appendChild(tableWrap);
  } else {
    container.appendChild(el('p', { class: 'lbt-note',
      text: 'No profile weights differ between A and B. Calibration tweak must be elsewhere (score_bands, modifiers, …).' }));
  }

  if (identical.length > 0) {
    const sum = el('details', { class: 'lab-pillar-identical' });
    sum.appendChild(el('summary', { text: `${identical.length} identical profiles (no change)` }));
    sum.appendChild(el('p', { class: 'lbt-note', text: identical.join(', ') }));
    container.appendChild(sum);
  }
}

/* ======================================================================
   Metadata footer
   ====================================================================== */
function renderMetadata(comparison) {
  $('ab-hash-a').textContent = comparison.config_a_hash || '?';
  $('ab-hash-b').textContent = comparison.config_b_hash || '?';
  const meta = comparison.side_a?.run_meta || {};
  const win = meta.window || {};
  $('ab-window').textContent = `${win.start ?? '?'} → ${win.end ?? '?'} (${win.n_trading_days ?? '?'} td)`;
  $('ab-universe').textContent = `${meta.universe_size ?? '?'} tickers`;
  const params = comparison.params || {};
  $('ab-profile').textContent = params.profile_name ?? 'long_only_buy';
  $('ab-cost').textContent = (params.cost_bps != null) ? params.cost_bps.toFixed(1) : '5.0';
}

/* ======================================================================
   Boot
   ====================================================================== */
main().catch((e) => {
  console.error('[lthcs-backtest-ab] fatal', e);
  $('ab-loading')?.classList.add('hidden');
  const errBox = $('ab-error');
  if (errBox) {
    errBox.classList.remove('hidden');
    errBox.textContent = `Failed to render A/B view: ${e.message || e}`;
  }
});
