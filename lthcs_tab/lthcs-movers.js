// lthcs-movers.js
// Tier 4 #18 — Movers leaderboard strip.
// Compact horizontal row of the top-10 gainers + top-10 decliners by score
// trend (delta over the trend period actually used per ticker — usually 30d,
// but the trend pipeline falls back to 14/7/3/1 when history is shallow).
//
// Reads from state already populated by lthcs-tab.js:
//   - state.enriched         (per-ticker rows with .ticker, .name)
//   - state.trendByTicker    (ticker -> { delta, direction, periodDays })
//
// Public API:
//   renderMoversStrip(state, openDetail)
//     Re-renders into #lthcs-movers-strip. Always shows universe-wide top
//     movers (NOT affected by user filters — otherwise a "Constructive only"
//     filter would hide the most interesting moves). Hides the strip entirely
//     when there are no qualifying movers in either direction.
//
// Click handling:
//   Each tile carries data-ticker. The host page wires a delegated click
//   handler that calls openDetail(...) — same modal the cards use.

'use strict';

const MAX_PER_SIDE = 10;

function escapeHtml(s) {
  if (s == null) return '';
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function formatDelta(delta) {
  if (!Number.isFinite(delta)) return '—';
  const sign = delta > 0 ? '+' : '';
  return `${sign}${delta.toFixed(1)}`;
}

// Pick the most-common periodDays among a list of mover rows. Returns
// null if mixed (no clear majority — > half of the entries must agree).
function dominantPeriod(rows) {
  if (!rows || !rows.length) return null;
  const counts = new Map();
  for (const r of rows) {
    const p = r && r.periodDays;
    if (!Number.isFinite(p)) continue;
    counts.set(p, (counts.get(p) || 0) + 1);
  }
  if (!counts.size) return null;
  // If all entries share the same period, return it. Otherwise return null
  // (the section label drops the period suffix on mixed sets).
  if (counts.size === 1) {
    return rows[0].periodDays;
  }
  // Pick the strict majority, if any (>50% of rows).
  let bestP = null;
  let bestC = 0;
  for (const [p, c] of counts.entries()) {
    if (c > bestC) { bestC = c; bestP = p; }
  }
  if (bestC > rows.length / 2) return bestP;
  return null; // mixed
}

function sectionTitle(kind, rows) {
  const base = kind === 'gainers' ? 'Top Gainers' : 'Top Decliners';
  const count = rows.length;
  // "Top 10 Gainers" only when there are fewer than the max — the canonical
  // case ("there really are 10") shouldn't shout the number.
  const lead = count < MAX_PER_SIDE
    ? `Top ${count} ${kind === 'gainers' ? 'Gainers' : 'Decliners'}`
    : base;
  const period = dominantPeriod(rows);
  return period ? `${lead} ${period}D` : lead;
}

function moverTile(row, kind) {
  const ticker = escapeHtml(row.ticker);
  const name = escapeHtml(row.name || row.ticker);
  const delta = formatDelta(row.delta);
  const arrow = kind === 'gainers' ? '▲' : '▼'; // ▲ / ▼
  const periodLabel = Number.isFinite(row.periodDays) ? `${row.periodDays}d` : '';
  const aria = `${row.ticker} ${delta} over ${periodLabel || 'recent period'}, open details`;
  return (
    `<button type="button" class="lthcs-mover-tile" data-kind="${kind}" data-ticker="${ticker}" title="${name} · ${delta} ${escapeHtml(periodLabel)}" aria-label="${escapeHtml(aria)}">` +
      `<span class="lthcs-mover-ticker">${ticker}</span>` +
      `<span class="lthcs-mover-delta">` +
        `<span class="lthcs-mover-arrow" aria-hidden="true">${arrow}</span>` +
        `<span class="lthcs-mover-value">${escapeHtml(delta)}</span>` +
      `</span>` +
      (periodLabel ? `<span class="lthcs-mover-period">${escapeHtml(periodLabel)}</span>` : '') +
    `</button>`
  );
}

function buildMovers(state) {
  const enriched = (state && state.enriched) || [];
  const trendMap = (state && state.trendByTicker) || {};
  const all = [];
  for (const row of enriched) {
    const t = row && row.ticker;
    if (!t) continue;
    const tr = trendMap[t];
    if (!tr) continue;
    const delta = Number(tr.delta);
    if (!Number.isFinite(delta)) continue;
    if (tr.direction !== 'up' && tr.direction !== 'down') continue;
    all.push({
      ticker: t,
      name: row.name || t,
      delta,
      direction: tr.direction,
      periodDays: Number.isFinite(tr.periodDays) ? tr.periodDays : null,
    });
  }
  const gainers = all
    .filter((r) => r.direction === 'up')
    .sort((a, b) => b.delta - a.delta)
    .slice(0, MAX_PER_SIDE);
  const decliners = all
    .filter((r) => r.direction === 'down')
    .sort((a, b) => a.delta - b.delta)
    .slice(0, MAX_PER_SIDE);
  return { gainers, decliners };
}

export function renderMoversStrip(state) {
  const host = document.getElementById('lthcs-movers-strip');
  if (!host) return;

  const { gainers, decliners } = buildMovers(state);

  // No movers in either direction? Hide the whole strip — empty placeholders
  // are worse than no chrome on a quiet day.
  if (!gainers.length && !decliners.length) {
    host.classList.add('hidden');
    host.innerHTML = '';
    return;
  }

  const gainersHTML = gainers.length
    ? (
        `<div class="lthcs-movers-section" data-kind="gainers">` +
          `<span class="lthcs-movers-section-label">${escapeHtml(sectionTitle('gainers', gainers))}</span>` +
          `<div class="lthcs-movers-tiles">` +
            gainers.map((r) => moverTile(r, 'gainers')).join('') +
          `</div>` +
        `</div>`
      )
    : '';

  const declinersHTML = decliners.length
    ? (
        `<div class="lthcs-movers-section" data-kind="decliners">` +
          `<span class="lthcs-movers-section-label">${escapeHtml(sectionTitle('decliners', decliners))}</span>` +
          `<div class="lthcs-movers-tiles">` +
            decliners.map((r) => moverTile(r, 'decliners')).join('') +
          `</div>` +
        `</div>`
      )
    : '';

  host.innerHTML = (
    `<div class="lthcs-movers-strip-inner">` +
      `<span class="lthcs-movers-title" aria-hidden="true">Movers</span>` +
      `<div class="lthcs-movers-sections">` +
        gainersHTML +
        declinersHTML +
      `</div>` +
    `</div>`
  );
  host.classList.remove('hidden');
}
