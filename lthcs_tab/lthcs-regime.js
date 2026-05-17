// lthcs-regime.js
// Market Regime strip — compact tile row above the band-distribution cards.
// Fetches:
//   data/lthcs/macro/breadth_<date>.json
//   data/lthcs/macro/sector_strength_<date>.json
// Renders into #lthcs-regime-strip (created by index.html).
//
// Public API:
//   renderRegimeStrip(calcDate)  — fetches both JSONs and paints; degrades silently.

'use strict';

const MACRO_BASE = '../data/lthcs/macro';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function escapeHtml(s) {
  if (s == null) return '';
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

async function fetchJSONSafe(url) {
  try {
    const res = await fetch(url, { cache: 'no-store' });
    if (!res.ok) return null;
    return await res.json();
  } catch (err) {
    console.warn('LTHCS regime: fetch failed', url, err);
    return null;
  }
}

function fmtPct(v, digits = 1) {
  const n = Number(v);
  if (!Number.isFinite(n)) return '—';
  return `${(n * 100).toFixed(digits)}%`;
}

function fmtSignedPct(v, digits = 1) {
  const n = Number(v);
  if (!Number.isFinite(n)) return '—';
  const pct = n * 100;
  const sign = pct > 0 ? '+' : '';
  return `${sign}${pct.toFixed(digits)}%`;
}

function fmtSignedBp(v) {
  const n = Number(v);
  if (!Number.isFinite(n)) return '—';
  const sign = n > 0 ? '+' : '';
  return `${sign}${n.toFixed(0)}bp`;
}

function fmtNumber(v, digits = 2) {
  const n = Number(v);
  if (!Number.isFinite(n)) return '—';
  return n.toFixed(digits);
}

function percentileLabel(p) {
  const n = Number(p);
  if (!Number.isFinite(n)) return '—';
  const pctile = Math.round(n * 100);
  // Ordinal suffix
  const mod100 = pctile % 100;
  const mod10 = pctile % 10;
  let suffix = 'th';
  if (mod100 < 11 || mod100 > 13) {
    if (mod10 === 1) suffix = 'st';
    else if (mod10 === 2) suffix = 'nd';
    else if (mod10 === 3) suffix = 'rd';
  }
  return `${pctile}${suffix} %ile`;
}

// Tone classes: 'risk-on' (green), 'neutral' (gray), 'caution' (amber), 'risk-off' (red)
function hyTone(hyOas, flags) {
  if (flags && flags.hy_stress) return 'risk-off';
  const p = Number(hyOas && hyOas.percentile_2y);
  if (Number.isFinite(p)) {
    if (p > 0.7) return 'caution';
    if (p < 0.5) return 'risk-on';
  }
  return 'neutral';
}

function curveTone(curve, flags) {
  if (flags && flags.curve_inverted) return 'risk-off';
  const n = Number(curve && curve.current);
  if (Number.isFinite(n) && n < 0) return 'risk-off';
  return 'risk-on';
}

function dollarTone(_dollar, flags) {
  if (flags && flags.dollar_strong) return 'risk-off';
  return 'neutral';
}

// ---------------------------------------------------------------------------
// Renderers
// ---------------------------------------------------------------------------

function renderHYChip(breadth) {
  if (!breadth || !breadth.hy_oas) return '';
  const hy = breadth.hy_oas;
  const flags = breadth.regime_flags || {};
  const tone = hyTone(hy, flags);
  const current = fmtNumber(hy.current, 2);
  const pctile = percentileLabel(hy.percentile_2y);
  const change = fmtSignedBp(hy.change_30d_bp);
  return (
    `<div class="lthcs-regime-chip" data-tone="${tone}" title="High-yield credit spread vs 2-year history">` +
      `<span class="lthcs-regime-dot" aria-hidden="true"></span>` +
      `<span class="lthcs-regime-chip-body">` +
        `<span class="lthcs-regime-chip-label">HY OAS</span>` +
        `<span class="lthcs-regime-chip-value">${escapeHtml(current)}%` +
          ` <span class="lthcs-regime-chip-sub">${escapeHtml(pctile)} · ${escapeHtml(change)} 30d</span>` +
        `</span>` +
      `</span>` +
    `</div>`
  );
}

function renderCurveChip(breadth) {
  if (!breadth || !breadth.yield_curve_2s10s) return '';
  const c = breadth.yield_curve_2s10s;
  const flags = breadth.regime_flags || {};
  const tone = curveTone(c, flags);
  const cur = Number(c.current);
  const sign = Number.isFinite(cur) && cur > 0 ? '+' : '';
  const status = (c.inverted || (flags && flags.curve_inverted)) ? 'inverted' : 'not inverted';
  return (
    `<div class="lthcs-regime-chip" data-tone="${tone}" title="2s10s Treasury yield curve">` +
      `<span class="lthcs-regime-dot" aria-hidden="true"></span>` +
      `<span class="lthcs-regime-chip-body">` +
        `<span class="lthcs-regime-chip-label">2s10s</span>` +
        `<span class="lthcs-regime-chip-value">${sign}${escapeHtml(fmtNumber(cur, 2))}` +
          ` <span class="lthcs-regime-chip-sub">${escapeHtml(status)}</span>` +
        `</span>` +
      `</span>` +
    `</div>`
  );
}

function renderDollarChip(breadth) {
  if (!breadth || !breadth.broad_dollar) return '';
  const d = breadth.broad_dollar;
  const flags = breadth.regime_flags || {};
  const tone = dollarTone(d, flags);
  const current = fmtNumber(d.current, 2);
  const change = fmtSignedPct(d.change_30d_pct, 1);
  return (
    `<div class="lthcs-regime-chip" data-tone="${tone}" title="Broad trade-weighted US dollar index">` +
      `<span class="lthcs-regime-dot" aria-hidden="true"></span>` +
      `<span class="lthcs-regime-chip-body">` +
        `<span class="lthcs-regime-chip-label">USD</span>` +
        `<span class="lthcs-regime-chip-value">${escapeHtml(current)}` +
          ` <span class="lthcs-regime-chip-sub">${escapeHtml(change)} 30d</span>` +
        `</span>` +
      `</span>` +
    `</div>`
  );
}

function renderSectorTile(sym, info, kind) {
  // kind: 'top' (green-ish) or 'bottom' (red)
  const name = (info && info.sector_name) || sym;
  const rel = Number(info && info.relative_1m);
  const relText = fmtSignedPct(rel, 1);
  return (
    `<span class="lthcs-regime-sector" data-kind="${kind}" title="${escapeHtml(sym)} · 1m vs SPY">` +
      `<span class="lthcs-regime-sector-name">${escapeHtml(name)}</span>` +
      `<span class="lthcs-regime-sector-rel">${escapeHtml(relText)}</span>` +
    `</span>`
  );
}

function renderSectorRow(sectorData) {
  if (!sectorData || !sectorData.sectors) return '';
  const entries = Object.entries(sectorData.sectors)
    .map(([sym, info]) => ({ sym, info, rel: Number(info && info.relative_1m) }))
    .filter((row) => Number.isFinite(row.rel));
  if (entries.length === 0) return '';
  const sortedDesc = [...entries].sort((a, b) => b.rel - a.rel);
  const top3 = sortedDesc.slice(0, 3);
  const bot3 = sortedDesc.slice(-3).reverse(); // worst first
  const topHTML = top3.map((r) => renderSectorTile(r.sym, r.info, 'top')).join('');
  const botHTML = bot3.map((r) => renderSectorTile(r.sym, r.info, 'bottom')).join('');
  return (
    `<div class="lthcs-regime-sectors" aria-label="Sector strength 1m vs SPY">` +
      `<div class="lthcs-regime-sector-group" data-kind="top">` +
        `<span class="lthcs-regime-sector-label">Leaders 1m</span>` +
        `${topHTML}` +
      `</div>` +
      `<div class="lthcs-regime-sector-group" data-kind="bottom">` +
        `<span class="lthcs-regime-sector-label">Laggards 1m</span>` +
        `${botHTML}` +
      `</div>` +
    `</div>`
  );
}

// ---------------------------------------------------------------------------
// Public entry
// ---------------------------------------------------------------------------

export async function renderRegimeStrip(calcDate) {
  const host = document.getElementById('lthcs-regime-strip');
  if (!host) return;
  if (!calcDate) {
    host.classList.add('hidden');
    return;
  }

  const [breadth, sectors] = await Promise.all([
    fetchJSONSafe(`${MACRO_BASE}/breadth_${calcDate}.json`),
    fetchJSONSafe(`${MACRO_BASE}/sector_strength_${calcDate}.json`),
  ]);

  // If both missing, hide cleanly.
  if (!breadth && !sectors) {
    host.classList.add('hidden');
    host.innerHTML = '';
    return;
  }

  const macroHTML = breadth
    ? (
        `<div class="lthcs-regime-macro">` +
          renderHYChip(breadth) +
          renderCurveChip(breadth) +
          renderDollarChip(breadth) +
        `</div>`
      )
    : '';

  const sectorHTML = sectors ? renderSectorRow(sectors) : '';

  host.innerHTML = (
    `<div class="lthcs-regime-strip-inner">` +
      `<span class="lthcs-regime-title" aria-hidden="true">Market Regime</span>` +
      `${macroHTML}` +
      `${sectorHTML}` +
    `</div>`
  );
  host.classList.remove('hidden');
}
