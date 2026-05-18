// lthcs-movers.js
// Tier 4 #18 — Movers leaderboard strip.
// Compact horizontal row of the top-10 gainers + top-10 decliners by score
// trend (delta over the trend period actually used per ticker — usually 30d,
// but the trend pipeline falls back to 14/7/3/1 when history is shallow).
//
// Drift-window definition:
//   delta = composite_score_today − composite_score_at_anchor
//   anchor = latest history entry on-or-before (today − N days), where N is
//   the longest of {30, 14, 7, 3, 1} that resolves to a real entry. The N
//   actually used is shown on each tile as a "30d" / "14d" / etc. suffix.
//   We require N >= MIN_PERIOD_DAYS (5) here, which also satisfies the spec's
//   "skip tickers with fewer than 6 days of history" floor (a 7d/14d/30d
//   anchor on disk implies at least that many history rows). This diverges
//   from the spec's "5d preferred, 1d fallback" suggestion in favour of the
//   already-deployed 30d trend pipeline — consistent with the trend pill
//   shown on each ticker card.
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

// Minimum trend window (in days) required to qualify a ticker as a "mover".
// The shared trend pipeline in lthcs-tab.js prefers 30d but falls back
// through 14/7/3/1 when per-ticker history is shallow. A 1d move is too
// noisy to belong on a leaderboard, so we require the resolved window to be
// at least this many days — which in practice also enforces the spec's
// "skip tickers with < 6 days of data" floor (any 7d/14d/30d anchor implies
// at least that many entries on disk).
const MIN_PERIOD_DAYS = 5;

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

function formatScore(n) {
  const v = Number(n);
  return Number.isFinite(v) ? v.toFixed(1) : '—';
}

function moverTile(row, kind) {
  const ticker = escapeHtml(row.ticker);
  const name = escapeHtml(row.name || row.ticker);
  const delta = formatDelta(row.delta);
  const score = formatScore(row.score);
  const arrow = kind === 'gainers' ? '▲' : '▼'; // ▲ / ▼
  const periodLabel = Number.isFinite(row.periodDays) ? `${row.periodDays}d` : '';
  const aria = `${row.ticker} composite ${score}, ${delta} over ${periodLabel || 'recent period'}, open details`;
  const titleAttr = `${name} · score ${score} · ${delta} ${periodLabel}`.trim();
  return (
    `<button type="button" class="lthcs-mover-tile" data-kind="${kind}" data-ticker="${ticker}" title="${escapeHtml(titleAttr)}" aria-label="${escapeHtml(aria)}">` +
      `<span class="lthcs-mover-ticker">${ticker}</span>` +
      `<span class="lthcs-mover-score">${escapeHtml(score)}</span>` +
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
    const periodDays = Number.isFinite(tr.periodDays) ? tr.periodDays : null;
    // Insufficient-history gate: skip rows whose resolved trend window is
    // shorter than MIN_PERIOD_DAYS. The trend pipeline's 1d/3d fallbacks
    // produce noisy "movers" on freshly-deployed tickers — keep them off
    // the leaderboard entirely rather than mixing them with real 30d moves.
    if (!Number.isFinite(periodDays) || periodDays < MIN_PERIOD_DAYS) continue;
    all.push({
      ticker: t,
      name: row.name || t,
      score: Number(row.score),  // current composite — surfaced on each tile
      delta,
      direction: tr.direction,
      periodDays,
    });
  }
  // Ties on delta: break by current composite score (higher first for
  // gainers; lower first for decliners) so the order is deterministic and
  // surfaces the more notable absolute level. Final tiebreak on ticker
  // for full stability.
  const gainers = all
    .filter((r) => r.direction === 'up')
    .sort((a, b) => (b.delta - a.delta) || (b.score - a.score) || a.ticker.localeCompare(b.ticker))
    .slice(0, MAX_PER_SIDE);
  const decliners = all
    .filter((r) => r.direction === 'down')
    .sort((a, b) => (a.delta - b.delta) || (a.score - b.score) || a.ticker.localeCompare(b.ticker))
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
