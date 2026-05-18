// lthcs-v2.js
// V2 visual treatment of the LTHCS dashboard. Mirrors the crypto-dashboard
// layout (header → tab strip → sentiment gauge → movers cards → regime/sector
// cards → band pills → filter bar → ticker grid).
//
// Data flow is intentionally parallel to lthcs_tab/lthcs-tab.js but does NOT
// import it — V1 stays untouched as the fallback page. The detail modal IS
// reused via a relative import to ../lthcs_tab/lthcs-detail.js so any future
// modal upgrade is inherited here for free.
//
// All fetches use the same paths as V1 (`../data/lthcs/...`); when served
// from /lthcs/v2/ this resolves to /lthcs/data/lthcs/... (which the pages.yml
// staging branch mirrors from data/lthcs/).

'use strict';

import { openDetail } from '../lthcs_tab/lthcs-detail.js';
import { openAbout } from '../lthcs_tab/lthcs-about.js';

// ---------------------------------------------------------------------------
// Constants — paths mirror V1's lthcs-tab.js
// ---------------------------------------------------------------------------

const SNAPSHOTS_BASE = '../data/lthcs/snapshots';
const UNIVERSE_URL = '../data/lthcs/universe.json';
const INDEX_URL = `${SNAPSHOTS_BASE}/index.json`;
const HISTORY_BASE = '../data/lthcs/history/by_ticker';
const INSIDER_BASE = '../data/lthcs/insider';
const MACRO_BASE = '../data/lthcs/macro';
// LTHCS Composite Index (universe-level ±100 read), written by
// lthcs_daily.py Stage 8. The v2 sentiment gauge sources its real
// math + component breakdown from here.
const INDEX_BASE = '../data/lthcs/index';

const TREND_FLAT_THRESHOLD = 0.5;
const TREND_FALLBACK_DAYS = [30, 14, 7, 3, 1];

// V1's snapshot bands → V2 UI band keys (same mapping as lthcs-tab.js).
const BAND_SNAPSHOT_TO_UI = {
  elite: 'elite',
  high_confidence: 'high',
  constructive: 'constructive',
  monitor: 'monitor',
  weakening: 'weakening',
  review: 'review',
};

const UI_BANDS = ['elite', 'high', 'constructive', 'monitor', 'weakening', 'review'];

const BAND_LABELS = {
  elite: 'Elite',
  high: 'High Confidence',
  constructive: 'Constructive',
  monitor: 'Monitor',
  weakening: 'Weakening',
  review: 'Review',
};

const INDEX_KEY_NORMALIZE = {
  'DJIA': 'djia',
  'NASDAQ-100': 'nasdaq-100',
  'S&P 100': 'sp-100',
  'S&P 500': 'sp-500',
};
const INDEX_FILTERS = ['djia', 'nasdaq-100', 'sp-100'];

const STORAGE_KEY = 'lthcs.v2.filters';

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

const state = {
  snapshot: null,
  universe: null,
  universeByTicker: {},
  enriched: [],
  trendByTicker: {},
  insiderByTicker: {},
  breadth: null,
  breadthSentiment: null,
  sectors: null,
  // LTHCS Composite Index payload, fetched from data/lthcs/index/<date>.json.
  // When present, drives the sentiment gauge with real per-component data
  // instead of the legacy 3-input heuristic.
  lthcsIndex: null,
  filters: {
    band: 'all',
    drift: 'all',
    index: 'all',
    search: '',
  },
};

// ---------------------------------------------------------------------------
// DOM helpers
// ---------------------------------------------------------------------------

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

function show(el) { if (el) el.classList.remove('hidden'); }
function hide(el) { if (el) el.classList.add('hidden'); }

function escapeHtml(s) {
  if (s == null) return '';
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function formatScore(n) {
  const v = Number(n);
  return Number.isFinite(v) ? v.toFixed(1) : '—';
}

function formatDelta(n) {
  const v = Number(n);
  if (!Number.isFinite(v)) return '—';
  const sign = v > 0 ? '+' : '';
  return `${sign}${v.toFixed(1)}`;
}

function formatPct(n, digits = 1) {
  const v = Number(n);
  if (!Number.isFinite(v)) return '—';
  return `${(v * 100).toFixed(digits)}%`;
}

function formatSignedPct(n, digits = 1) {
  const v = Number(n);
  if (!Number.isFinite(v)) return '—';
  const pct = v * 100;
  const sign = pct > 0 ? '+' : '';
  return `${sign}${pct.toFixed(digits)}%`;
}

function formatSignedBp(n) {
  const v = Number(n);
  if (!Number.isFinite(v)) return '—';
  const sign = v > 0 ? '+' : '';
  return `${sign}${v.toFixed(0)}bp`;
}

function formatDate(isoDate) {
  if (!isoDate) return '—';
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(isoDate);
  if (!m) return isoDate;
  const d = new Date(Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3])));
  try {
    return d.toLocaleDateString(undefined, {
      year: 'numeric', month: 'short', day: 'numeric', timeZone: 'UTC',
    });
  } catch {
    return isoDate;
  }
}

function uiBandFor(snapshotBand) {
  return BAND_SNAPSHOT_TO_UI[snapshotBand] || snapshotBand || 'review';
}

function classifyDrift(drift30d) {
  const v = Number(drift30d) || 0;
  if (v > 1.0) return 'improving';
  if (v < -1.0) return 'declining';
  return 'stable';
}

// ---------------------------------------------------------------------------
// Trend computation (parallel to V1's lthcs-tab.js)
// ---------------------------------------------------------------------------

function parseISODateUTC(s) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(s || ''));
  if (!m) return NaN;
  return Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
}

function pickAnchorForLookback(history, currentDateISO, lookbackDays) {
  if (!Array.isArray(history) || !history.length) return null;
  const currentMs = parseISODateUTC(currentDateISO);
  if (!Number.isFinite(currentMs)) return null;
  const targetMs = currentMs - lookbackDays * 24 * 60 * 60 * 1000;

  let best = null;
  let bestMs = -Infinity;
  for (const entry of history) {
    const ms = parseISODateUTC(entry && entry.date);
    if (!Number.isFinite(ms)) continue;
    if (ms <= targetMs && ms > bestMs) {
      bestMs = ms;
      best = entry;
    }
  }
  return best;
}

function pickAnchorWithFallback(history, currentDateISO) {
  for (const days of TREND_FALLBACK_DAYS) {
    const anchor = pickAnchorForLookback(history, currentDateISO, days);
    if (anchor && Number.isFinite(Number(anchor.score))) {
      return { anchor, periodDays: days };
    }
  }
  return null;
}

function computeTrend(historyDoc, currentDateISO, currentScore) {
  if (!historyDoc || !Array.isArray(historyDoc.history)) {
    return { delta: null, direction: 'unknown', periodDays: null };
  }
  const result = pickAnchorWithFallback(historyDoc.history, currentDateISO);
  if (!result) {
    return { delta: null, direction: 'unknown', periodDays: null };
  }
  const cur = Number(currentScore);
  if (!Number.isFinite(cur)) {
    return { delta: null, direction: 'unknown', periodDays: null };
  }
  const delta = cur - Number(result.anchor.score);
  let direction = 'flat';
  if (delta > TREND_FLAT_THRESHOLD) direction = 'up';
  else if (delta < -TREND_FLAT_THRESHOLD) direction = 'down';
  return { delta, direction, periodDays: result.periodDays };
}

function trendArrow(direction) {
  if (direction === 'up') return '▲';
  if (direction === 'down') return '▼';
  if (direction === 'flat') return '→';
  return '?';
}

// ---------------------------------------------------------------------------
// Data fetching
// ---------------------------------------------------------------------------

async function fetchJSON(url) {
  const res = await fetch(url, { cache: 'no-store' });
  if (!res.ok) {
    throw new Error(`Fetch failed: ${url} → ${res.status} ${res.statusText}`);
  }
  return res.json();
}

async function fetchJSONSafe(url) {
  try {
    const res = await fetch(url, { cache: 'no-store' });
    if (!res.ok) return null;
    return await res.json();
  } catch (err) {
    console.warn('LTHCS V2: fetch failed', url, err);
    return null;
  }
}

async function fetchSnapshot() {
  const index = await fetchJSON(INDEX_URL);
  const latest = index && index.latest;
  if (!latest) throw new Error('Snapshot index has no `latest` date.');
  const snapshot = await fetchJSON(`${SNAPSHOTS_BASE}/${latest}.json`);
  return { index, snapshot };
}

async function fetchUniverse() {
  return (await fetchJSONSafe(UNIVERSE_URL)) || { tickers: [] };
}

async function fetchTrendMap(rows, calcDate) {
  if (!Array.isArray(rows) || !rows.length) return {};
  const fetches = rows.map(async (row) => {
    const ticker = row && row.ticker;
    if (!ticker) return [null, { delta: null, direction: 'unknown' }];
    const url = `${HISTORY_BASE}/${encodeURIComponent(ticker)}.json`;
    try {
      const res = await fetch(url, { cache: 'no-store' });
      if (!res.ok) return [ticker, { delta: null, direction: 'unknown' }];
      const doc = await res.json();
      const trend = computeTrend(doc, calcDate, row.score);
      return [ticker, trend];
    } catch {
      return [ticker, { delta: null, direction: 'unknown' }];
    }
  });
  const results = await Promise.all(fetches);
  const map = {};
  for (const [ticker, trend] of results) {
    if (ticker) map[ticker] = trend;
  }
  return map;
}

// ---------------------------------------------------------------------------
// Enrichment
// ---------------------------------------------------------------------------

function buildUniverseIndex(universe) {
  const idx = {};
  const list = (universe && universe.tickers) || [];
  for (const row of list) {
    if (row && row.ticker) idx[row.ticker] = row;
  }
  return idx;
}

function enrichScores(snapshot, universeByTicker) {
  const scores = (snapshot && snapshot.scores) || [];
  return scores.map((row) => {
    const uni = universeByTicker[row.ticker] || {};
    const direction = classifyDrift(row.drift_30d);
    const uiBand = uiBandFor(row.band);
    const indexMembership = Array.isArray(uni.index_membership) ? uni.index_membership : [];
    const indices = indexMembership.map((s) => INDEX_KEY_NORMALIZE[s]).filter(Boolean);
    return {
      ticker: row.ticker,
      name: uni.name || row.ticker,
      sector: uni.sector || row.sector || '',
      indices,
      score: Number(row.lthcs_score),
      snapshotBand: row.band,
      uiBand,
      drift30d: Number(row.drift_30d) || 0,
      driftDirection: direction,
    };
  });
}

// ---------------------------------------------------------------------------
// Sentiment gauge math
// ---------------------------------------------------------------------------

/**
 * Composite LTHCS sentiment score, -50 (very bearish) to +50 (very bullish).
 * Three inputs equally weighted:
 *
 *   1. Band distribution: net% in (elite + high) minus % in (review + weakening),
 *      scaled to a -1..+1 axis.
 *   2. Macro regime flags: hy_stress / curve_inverted / dollar_strong each
 *      knock 0.33 off; clean regime = +1.0.
 *   3. Breadth sentiment composite_regime: bullish=+1, neutral=0, bearish=-1.
 *
 * Returns { score: number in [-50, 50], tone: string, bullishPct, neutralPct,
 *          bearishPct, label, subline }.
 */
function computeSentiment(enriched, breadth, breadthSentiment) {
  const total = enriched.length || 1;
  let bullishCount = 0;
  let neutralCount = 0;
  let bearishCount = 0;
  for (const row of enriched) {
    if (row.uiBand === 'elite' || row.uiBand === 'high') bullishCount += 1;
    else if (row.uiBand === 'constructive' || row.uiBand === 'monitor') neutralCount += 1;
    else bearishCount += 1; // weakening + review
  }
  const bullishPct = bullishCount / total;
  const neutralPct = neutralCount / total;
  const bearishPct = bearishCount / total;

  // Input 1: net band sentiment in [-1, +1].
  const bandAxis = bullishPct - bearishPct;

  // Input 2: macro regime flags (hy_stress / curve_inverted / dollar_strong).
  // Start at +1.0; each set flag knocks 0.67 off (so two flags = ~-0.33,
  // three = -1.0). Missing data → neutral 0.
  let macroAxis = 0;
  if (breadth && breadth.regime_flags) {
    const f = breadth.regime_flags;
    let setCount = 0;
    if (f.hy_stress) setCount += 1;
    if (f.curve_inverted) setCount += 1;
    if (f.dollar_strong) setCount += 1;
    macroAxis = 1.0 - (setCount * 0.67);
    if (macroAxis < -1) macroAxis = -1;
  }

  // Input 3: breadth sentiment composite_regime.
  let breadthAxis = 0;
  if (breadthSentiment && breadthSentiment.composite_regime) {
    const r = String(breadthSentiment.composite_regime).toLowerCase();
    if (r === 'bullish') breadthAxis = 1;
    else if (r === 'bearish') breadthAxis = -1;
    else breadthAxis = 0;
  }

  // Equal-weighted average → scale to [-50, +50].
  const composite = (bandAxis + macroAxis + breadthAxis) / 3;
  const score = Math.round(composite * 50);

  let tone = 'neutral';
  let label = 'NEUTRAL';
  if (score >= 25) { tone = 'bullish'; label = 'BULLISH'; }
  else if (score >= 10) { tone = 'bullish'; label = 'CONSTRUCTIVE'; }
  else if (score <= -25) { tone = 'bearish'; label = 'BEARISH'; }
  else if (score <= -10) { tone = 'cautious'; label = 'CAUTIOUS'; }
  else { tone = 'neutral'; label = 'NEUTRAL'; }

  const subline = (
    `Bands net ${Math.round(bandAxis * 100)} ` +
    `· Macro ${Math.round(macroAxis * 100)} ` +
    `· Breadth ${Math.round(breadthAxis * 100)}`
  );

  return { score, tone, label, subline, bullishPct, neutralPct, bearishPct };
}

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------

function renderMeta(snapshot, enrichedCount) {
  const coverage = $('#lthcs-v2-coverage');
  if (coverage) coverage.textContent = `${enrichedCount} tickers`;
  const gen = $('#lthcs-v2-generated');
  if (gen && snapshot) {
    gen.textContent = `generated ${formatDate(snapshot.calc_date)}`;
  }
}

// Map LTHCS Index label → the v2 card's tone attribute (drives border + color).
function toneForLabel(label) {
  if (!label) return 'neutral';
  const L = String(label).toUpperCase();
  if (L.includes('ELITE')) return 'bullish';
  if (L.includes('CONSTRUCTIVE')) return 'bullish';
  if (L.includes('WEAKENING')) return 'cautious';
  if (L.includes('DISTRIBUTING')) return 'bearish';
  return 'neutral';
}

// Map composite score in [-100, +100] to a 0-100% offset on the gauge.
function gaugePctFor(score) {
  const n = Number(score);
  if (!Number.isFinite(n)) return 50;
  const pct = ((n + 100) / 200) * 100;
  if (pct < 0) return 0;
  if (pct > 100) return 100;
  return pct;
}

function renderSentimentFromIndex(card, payload) {
  card.dataset.tone = toneForLabel(payload.label);
  const score = Number(payload.score);
  const scoreEl = $('#lthcs-v2-sentiment-score');
  if (scoreEl) scoreEl.textContent = `${score >= 0 ? '+' : ''}${score}`;
  const labelEl = $('#lthcs-v2-sentiment-label');
  if (labelEl) labelEl.textContent = payload.label || 'NEUTRAL';
  const subEl = $('#lthcs-v2-sentiment-subline');
  if (subEl) {
    subEl.textContent = (
      `Composite ±100 from band lean, pillar avgs, macro regime, ` +
      `insider & 13F breadth · ${payload.as_of || ''}`
    );
  }

  // The legacy stacked bar (bullish/neutral/bearish %) still works as a
  // secondary signal — derive it from the enriched bands so the user gets
  // both the composite score and the band split. Falls back to even split.
  const total = state.enriched.length || 1;
  let bullishCount = 0;
  let neutralCount = 0;
  let bearishCount = 0;
  for (const row of state.enriched) {
    if (row.uiBand === 'elite' || row.uiBand === 'high') bullishCount += 1;
    else if (row.uiBand === 'constructive' || row.uiBand === 'monitor') neutralCount += 1;
    else bearishCount += 1;
  }
  const pos = $('#lthcs-v2-sentiment-bar-pos');
  const neu = $('#lthcs-v2-sentiment-bar-neu');
  const neg = $('#lthcs-v2-sentiment-bar-neg');
  if (pos) pos.style.width = `${(bullishCount / total * 100).toFixed(1)}%`;
  if (neu) neu.style.width = `${(neutralCount / total * 100).toFixed(1)}%`;
  if (neg) neg.style.width = `${(bearishCount / total * 100).toFixed(1)}%`;

  // Horizontal range gauge marker (red → green) with the composite marker
  // pinned at the right position. Built inline so the v2 HTML stays
  // unchanged — same pattern the v1 Whale card uses.
  let gauge = $('#lthcs-v2-sentiment-gauge');
  if (!gauge) {
    gauge = document.createElement('div');
    gauge.id = 'lthcs-v2-sentiment-gauge';
    gauge.className = 'sentiment-gauge';
    gauge.innerHTML = (
      `<div class="sentiment-gauge-bar"></div>` +
      `<div class="sentiment-gauge-marker"></div>`
    );
    // Insert directly after the stacked bar so the visual ordering is:
    // header → stacked bar → range gauge → legend → components.
    const legend = card.querySelector('.sentiment-legend');
    if (legend) card.insertBefore(gauge, legend);
    else card.appendChild(gauge);
  }
  const marker = gauge.querySelector('.sentiment-gauge-marker');
  if (marker) {
    const pct = gaugePctFor(score);
    marker.style.left = `calc(${pct.toFixed(1)}% - 5px)`;
  }

  // Component breakdown table — same shape as the v1 Whale Index.
  let host = $('#lthcs-v2-sentiment-components');
  if (!host) {
    host = document.createElement('details');
    host.id = 'lthcs-v2-sentiment-components';
    host.className = 'sentiment-components';
    host.open = true;
    card.appendChild(host);
  }
  const rows = (payload.components || []).map((c) => {
    const cls = c.delta > 0 ? 'pos' : (c.delta < 0 ? 'neg' : 'neu');
    const sign = c.delta >= 0 ? '+' : '';
    return (
      `<tr>` +
      `<td>${escapeHtml(c.name)}</td>` +
      `<td>${escapeHtml(c.value)}</td>` +
      `<td class="sc-delta sc-${cls}">${sign}${c.delta}</td>` +
      `<td class="sc-read">${escapeHtml(c.read || '')}</td>` +
      `</tr>`
    );
  }).join('');
  host.innerHTML = (
    `<summary class="sentiment-components-summary">Component breakdown</summary>` +
    `<table class="sentiment-components-table">` +
    `<thead><tr><th>Component</th><th>Value</th><th>&plusmn;</th><th>Read</th></tr></thead>` +
    `<tbody>${rows}</tbody>` +
    `</table>` +
    `<div class="sentiment-components-note">${escapeHtml(payload.note || '')}</div>`
  );
}

function renderSentiment() {
  const card = $('#lthcs-v2-sentiment-card');
  if (!card) return;

  // Prefer the real LTHCS Index payload (universe-level composite). If
  // it isn't on disk yet (older snapshot dates), fall back to the legacy
  // 3-input heuristic so the card never goes blank.
  if (state.lthcsIndex) {
    renderSentimentFromIndex(card, state.lthcsIndex);
    return;
  }

  const result = computeSentiment(state.enriched, state.breadth, state.breadthSentiment);
  card.dataset.tone = result.tone;

  const scoreEl = $('#lthcs-v2-sentiment-score');
  if (scoreEl) {
    const sign = result.score > 0 ? '+' : '';
    scoreEl.textContent = `${sign}${result.score}`;
  }
  const labelEl = $('#lthcs-v2-sentiment-label');
  if (labelEl) labelEl.textContent = result.label;
  const subEl = $('#lthcs-v2-sentiment-subline');
  if (subEl) subEl.textContent = result.subline;

  // Stacked horizontal bar — bullish / neutral / bearish split.
  const pos = $('#lthcs-v2-sentiment-bar-pos');
  const neu = $('#lthcs-v2-sentiment-bar-neu');
  const neg = $('#lthcs-v2-sentiment-bar-neg');
  if (pos) pos.style.width = `${(result.bullishPct * 100).toFixed(1)}%`;
  if (neu) neu.style.width = `${(result.neutralPct * 100).toFixed(1)}%`;
  if (neg) neg.style.width = `${(result.bearishPct * 100).toFixed(1)}%`;
}

function renderMovers() {
  const trendMap = state.trendByTicker;
  const candidates = [];
  for (const row of state.enriched) {
    const tr = trendMap[row.ticker];
    if (!tr) continue;
    const d = Number(tr.delta);
    if (!Number.isFinite(d)) continue;
    if (tr.direction !== 'up' && tr.direction !== 'down') continue;
    candidates.push({
      ticker: row.ticker,
      name: row.name,
      delta: d,
      direction: tr.direction,
      periodDays: Number.isFinite(tr.periodDays) ? tr.periodDays : null,
    });
  }
  const gainers = candidates
    .filter((c) => c.direction === 'up')
    .sort((a, b) => b.delta - a.delta)
    .slice(0, 5);
  const decliners = candidates
    .filter((c) => c.direction === 'down')
    .sort((a, b) => a.delta - b.delta)
    .slice(0, 5);

  paintMoverList('#lthcs-v2-gainers', gainers, 'gainers');
  paintMoverList('#lthcs-v2-decliners', decliners, 'decliners');
}

function paintMoverList(sel, rows, kind) {
  const host = $(sel);
  if (!host) return;
  if (!rows.length) {
    host.innerHTML = '<div class="movers-empty">No movers today.</div>';
    return;
  }
  host.innerHTML = rows.map((r) => moverRowHTML(r, kind)).join('');
}

function moverRowHTML(row, kind) {
  const ticker = escapeHtml(row.ticker);
  const name = escapeHtml(row.name || row.ticker);
  const delta = escapeHtml(formatDelta(row.delta));
  const arrow = kind === 'gainers' ? '▲' : '▼';
  const period = Number.isFinite(row.periodDays) ? `${row.periodDays}d` : '';
  return (
    `<button type="button" class="mover-row" data-kind="${kind}" data-ticker="${ticker}" aria-label="${ticker} ${delta} over ${period || 'recent period'}">` +
      `<span class="mover-sym">${ticker}</span>` +
      `<span class="mover-name">${name}</span>` +
      `<span class="mover-delta"><span aria-hidden="true">${arrow}</span> ${delta}</span>` +
      `<span class="mover-period">${escapeHtml(period)}</span>` +
    `</button>`
  );
}

function renderRegime() {
  const host = $('#lthcs-v2-regime-macro');
  if (!host) return;
  if (!state.breadth) {
    host.innerHTML = '<div class="regime-empty">Macro data unavailable.</div>';
    return;
  }
  const b = state.breadth;
  const flags = b.regime_flags || {};
  const chips = [];

  // HY OAS chip
  if (b.hy_oas) {
    const tone = flags.hy_stress ? 'risk-off'
      : (Number(b.hy_oas.percentile_2y) > 0.7 ? 'caution'
        : (Number(b.hy_oas.percentile_2y) < 0.5 ? 'risk-on' : 'neutral'));
    const pctile = Math.round((Number(b.hy_oas.percentile_2y) || 0) * 100);
    chips.push(
      `<div class="regime-chip" data-tone="${tone}">` +
        `<span class="regime-chip-dot" aria-hidden="true"></span>` +
        `<span><span class="regime-chip-label">HY OAS</span><br/>` +
          `<span class="regime-chip-value">${Number(b.hy_oas.current).toFixed(2)}%</span></span>` +
        `<span class="regime-chip-sub">${pctile}th %ile<br/>${formatSignedBp(b.hy_oas.change_30d_bp)} 30d</span>` +
      `</div>`
    );
  }
  // 2s10s curve
  if (b.yield_curve_2s10s) {
    const c = b.yield_curve_2s10s;
    const inv = c.inverted || flags.curve_inverted;
    const tone = inv ? 'risk-off' : 'risk-on';
    const cur = Number(c.current);
    const sign = cur > 0 ? '+' : '';
    chips.push(
      `<div class="regime-chip" data-tone="${tone}">` +
        `<span class="regime-chip-dot" aria-hidden="true"></span>` +
        `<span><span class="regime-chip-label">2s10s</span><br/>` +
          `<span class="regime-chip-value">${sign}${cur.toFixed(2)}</span></span>` +
        `<span class="regime-chip-sub">${inv ? 'inverted' : 'not inverted'}<br/>${formatSignedBp(c.change_30d_bp)} 30d</span>` +
      `</div>`
    );
  }
  // Broad dollar
  if (b.broad_dollar) {
    const d = b.broad_dollar;
    const tone = flags.dollar_strong ? 'risk-off' : 'neutral';
    chips.push(
      `<div class="regime-chip" data-tone="${tone}">` +
        `<span class="regime-chip-dot" aria-hidden="true"></span>` +
        `<span><span class="regime-chip-label">USD</span><br/>` +
          `<span class="regime-chip-value">${Number(d.current).toFixed(2)}</span></span>` +
        `<span class="regime-chip-sub">${formatSignedPct(d.change_30d_pct, 1)} 30d</span>` +
      `</div>`
    );
  }

  host.innerHTML = chips.length ? chips.join('') : '<div class="regime-empty">No macro chips.</div>';
}

function renderSectors() {
  const host = $('#lthcs-v2-sectors');
  if (!host) return;
  if (!state.sectors || !state.sectors.sectors) {
    host.innerHTML = '<div class="regime-empty">Sector data unavailable.</div>';
    return;
  }
  const entries = Object.entries(state.sectors.sectors)
    .map(([sym, info]) => ({ sym, info, rel: Number(info && info.relative_1m) }))
    .filter((r) => Number.isFinite(r.rel));
  if (!entries.length) {
    host.innerHTML = '<div class="regime-empty">No sectors.</div>';
    return;
  }
  const sorted = [...entries].sort((a, b) => b.rel - a.rel);
  const top = sorted.slice(0, 3);
  const bot = sorted.slice(-3).reverse();

  const topHTML = top.map((r) => sectorRowHTML(r, 'top')).join('');
  const botHTML = bot.map((r) => sectorRowHTML(r, 'bottom')).join('');

  host.innerHTML = (
    `<div class="sectors-col" data-kind="top">` +
      `<h3>Leaders 1m vs SPY</h3>${topHTML}` +
    `</div>` +
    `<div class="sectors-col" data-kind="bottom">` +
      `<h3>Laggards 1m vs SPY</h3>${botHTML}` +
    `</div>`
  );
}

function sectorRowHTML(r, kind) {
  const name = escapeHtml((r.info && r.info.sector_name) || r.sym);
  const rel = formatSignedPct(r.rel, 1);
  return (
    `<div class="sector-row" data-kind="${kind}">` +
      `<span class="sector-name">${name}</span>` +
      `<span class="sector-rel">${escapeHtml(rel)}</span>` +
    `</div>`
  );
}

function renderBandPills() {
  const host = $('#lthcs-v2-band-pills');
  if (!host) return;
  const counts = Object.fromEntries(UI_BANDS.map((b) => [b, 0]));
  for (const row of state.enriched) {
    if (counts[row.uiBand] != null) counts[row.uiBand] += 1;
  }
  host.innerHTML = UI_BANDS.map((b) => {
    const isActive = state.filters.band === b ? ' is-active' : '';
    return (
      `<button type="button" class="band-pill${isActive}" data-band="${b}" data-band-filter="${b}" aria-pressed="${state.filters.band === b}">` +
        `<span class="band-pill-count">${counts[b]}</span>` +
        `<span class="band-pill-label">${escapeHtml(BAND_LABELS[b])}</span>` +
      `</button>`
    );
  }).join('');
}

function applyFilters() {
  const q = state.filters.search.trim().toLowerCase();
  return state.enriched.filter((row) => {
    if (state.filters.band !== 'all' && row.uiBand !== state.filters.band) return false;
    if (state.filters.drift !== 'all' && row.driftDirection !== state.filters.drift) return false;
    if (state.filters.index !== 'all' && !row.indices.includes(state.filters.index)) return false;
    if (q) {
      const t = (row.ticker || '').toLowerCase();
      const n = (row.name || '').toLowerCase();
      if (!t.includes(q) && !n.includes(q)) return false;
    }
    return true;
  });
}

function tickerCardHTML(row) {
  const ticker = escapeHtml(row.ticker);
  const name = escapeHtml(row.name || '');
  const sector = escapeHtml(row.sector || '');
  const score = formatScore(row.score);
  const band = escapeHtml(row.uiBand);
  const bandLabel = escapeHtml(BAND_LABELS[row.uiBand] || row.uiBand);
  const trend = state.trendByTicker[row.ticker] || { delta: null, direction: 'unknown', periodDays: null };
  const trendDir = trend.direction || 'unknown';
  const arrow = trendArrow(trendDir);
  const trendVal = (trendDir === 'unknown' || !Number.isFinite(trend.delta))
    ? '—'
    : formatDelta(trend.delta);
  const period = trend.periodDays ? `${trend.periodDays}d` : '30d';

  return (
    `<button type="button" class="ticker-card" data-ticker="${ticker}" data-band="${band}" aria-label="${ticker} band ${bandLabel}">` +
      `<div class="ticker-head">` +
        `<span class="ticker-sym">${ticker}</span>` +
        `<span class="ticker-band">${bandLabel}</span>` +
      `</div>` +
      `<div class="ticker-score">${score}</div>` +
      `<div class="ticker-trend-row">` +
        `<span class="ticker-trend" data-trend="${trendDir}">` +
          `<span class="ticker-trend-arrow" aria-hidden="true">${arrow}</span>` +
          `<span>${escapeHtml(trendVal)}</span>` +
        `</span>` +
        `<span class="ticker-trend-period">${escapeHtml(period)}</span>` +
      `</div>` +
      `<div class="ticker-name">${name}</div>` +
      `<div class="ticker-sector">${sector}</div>` +
    `</button>`
  );
}

function renderTickerGrid() {
  const grid = $('#lthcs-v2-cards');
  const empty = $('#lthcs-v2-empty');
  const count = $('#lthcs-v2-result-count');
  if (!grid) return;
  const filtered = applyFilters();
  if (count) count.textContent = `${filtered.length} tickers shown`;
  if (!filtered.length) {
    grid.innerHTML = '';
    hide(grid);
    show(empty);
    return;
  }
  hide(empty);
  show(grid);
  grid.innerHTML = filtered.map(tickerCardHTML).join('');
}

function syncChipUI() {
  for (const group of ['drift', 'index']) {
    const value = state.filters[group];
    for (const chip of $$(`.chip[data-filter-group="${group}"]`)) {
      if (chip.dataset.filterValue === value) chip.classList.add('is-active');
      else chip.classList.remove('is-active');
    }
  }
}

function renderAll() {
  renderSentiment();
  renderMovers();
  renderRegime();
  renderSectors();
  renderBandPills();
  renderTickerGrid();
}

// ---------------------------------------------------------------------------
// Persistence
// ---------------------------------------------------------------------------

function restoreFilters() {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return;
    const parsed = JSON.parse(raw);
    if (parsed && typeof parsed === 'object') {
      for (const k of ['band', 'drift', 'index', 'search']) {
        if (typeof parsed[k] === 'string') state.filters[k] = parsed[k];
      }
    }
  } catch { /* ignore */ }
}

function persistFilters() {
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(state.filters));
  } catch { /* quota / private mode */ }
}

// ---------------------------------------------------------------------------
// Event wiring
// ---------------------------------------------------------------------------

function wireEvents() {
  // Reload button
  const reloadBtn = $('#lthcs-v2-refresh');
  if (reloadBtn) reloadBtn.addEventListener('click', () => refresh());

  // About tab → modal
  const aboutBtn = $('#lthcs-v2-about-tab');
  if (aboutBtn) aboutBtn.addEventListener('click', () => openAbout());

  // Search input
  const searchInput = $('#lthcs-v2-search');
  if (searchInput) {
    searchInput.value = state.filters.search || '';
    searchInput.addEventListener('input', (e) => {
      state.filters.search = e.target.value || '';
      persistFilters();
      renderTickerGrid();
    });
  }

  // Filter chips (drift + index)
  document.addEventListener('click', (e) => {
    const chip = e.target.closest('.chip[data-filter-group]');
    if (chip) {
      const group = chip.dataset.filterGroup;
      const value = chip.dataset.filterValue;
      if (!group || !value) return;
      state.filters[group] = value;
      syncChipUI();
      persistFilters();
      renderTickerGrid();
      return;
    }

    // Band pill — toggle to band filter (second click on active band clears).
    const pill = e.target.closest('.band-pill[data-band-filter]');
    if (pill) {
      const band = pill.dataset.bandFilter;
      const next = state.filters.band === band ? 'all' : band;
      state.filters.band = next;
      persistFilters();
      renderBandPills();
      renderTickerGrid();
      return;
    }

    // Ticker card → open detail modal (reused from ../lthcs_tab/lthcs-detail.js).
    const card = e.target.closest('.ticker-card[data-ticker]');
    if (card) {
      handleTickerClick(card.dataset.ticker);
      return;
    }

    // Mover row → open detail modal.
    const mover = e.target.closest('.mover-row[data-ticker]');
    if (mover) {
      handleTickerClick(mover.dataset.ticker);
      return;
    }
  });
}

function handleTickerClick(ticker) {
  if (!ticker) return;
  const scores = (state.snapshot && state.snapshot.scores) || [];
  const snapshotRow = scores.find((r) => r && r.ticker === ticker) || null;
  if (!snapshotRow) return;
  const universeEntry = state.universeByTicker[ticker] || null;
  const insider = state.insiderByTicker[ticker] || null;
  const calcDate = (state.snapshot && state.snapshot.calc_date) || null;
  try {
    openDetail({ ticker, snapshotRow, universeEntry, narrative: null, calcDate, insider });
  } catch (err) {
    console.warn('LTHCS V2: failed to open detail modal', err);
  }
}

// ---------------------------------------------------------------------------
// Top-level flow
// ---------------------------------------------------------------------------

function showError(err) {
  console.error('LTHCS V2:', err);
  const el = $('#lthcs-v2-error');
  if (el) {
    el.textContent = 'Could not load snapshot. Check that data/lthcs/snapshots/ exists.';
    show(el);
  }
  hide($('#lthcs-v2-loading'));
}

async function refresh() {
  const btn = $('#lthcs-v2-refresh');
  if (btn) btn.disabled = true;
  hide($('#lthcs-v2-error'));
  try {
    const [{ snapshot }, universe] = await Promise.all([fetchSnapshot(), fetchUniverse()]);
    state.snapshot = snapshot;
    state.universe = universe;
    state.universeByTicker = buildUniverseIndex(universe);
    state.enriched = enrichScores(snapshot, state.universeByTicker);

    renderMeta(snapshot, state.enriched.length);
    hide($('#lthcs-v2-loading'));
    renderAll();

    const calcDate = snapshot && snapshot.calc_date;

    // Side-load enrichment data in parallel — never blocks the main paint.
    Promise.all([
      fetchJSONSafe(`${MACRO_BASE}/breadth_${calcDate}.json`),
      fetchJSONSafe(`${MACRO_BASE}/breadth_sentiment_${calcDate}.json`),
      fetchJSONSafe(`${MACRO_BASE}/sector_strength_${calcDate}.json`),
      fetchJSONSafe(`${INSIDER_BASE}/${calcDate}.json`),
      fetchJSONSafe(`${INDEX_BASE}/${calcDate}.json`),
    ]).then(([breadth, breadthSent, sectors, insider, lthcsIndex]) => {
      state.breadth = breadth || null;
      state.breadthSentiment = breadthSent || null;
      state.sectors = sectors || null;
      state.insiderByTicker = (insider && typeof insider === 'object') ? insider : {};
      state.lthcsIndex = (lthcsIndex && typeof lthcsIndex === 'object') ? lthcsIndex : null;
      // Sentiment math depends on breadth + breadth_sentiment + index → re-render.
      renderSentiment();
      renderRegime();
      renderSectors();
    }).catch((err) => console.warn('LTHCS V2: side-load failed', err));

    // Trend map drives both the movers strip and the ticker-card pills.
    fetchTrendMap(state.enriched, calcDate).then((map) => {
      state.trendByTicker = map || {};
      renderMovers();
      renderTickerGrid();
    }).catch((err) => console.warn('LTHCS V2: trend map failed', err));
  } catch (err) {
    showError(err);
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function init() {
  restoreFilters();
  syncChipUI();
  wireEvents();
  await refresh();
}

document.addEventListener('DOMContentLoaded', () => {
  init().catch((err) => {
    console.error('LTHCS V2 init failed:', err);
    showError(err);
  });
});
