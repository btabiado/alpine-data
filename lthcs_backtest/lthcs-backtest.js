/* =========================================================================
   LTHCS Backtest Validation — vanilla-JS visualizer.

   Read-only consumer of files under ../data/lthcs/backtest/ and
   ../data/lthcs/adaptive_weights/. Renders:
     - Headline verdict (composite IC, portfolio Sharpe, walk-forward test IC)
     - Per-pillar IC ranking (horizontal bars, zero-centered)
     - Band portfolio P&L curve (SVG line + area)
     - Quintile spread per pillar (grouped bars Q1-Q5 + Q5-Q1 column)
     - Walk-forward CV table parsed from markdown summary
     - Per-cohort breakdown table parsed from the same markdown

   Defensive: every section has a placeholder card + retry button so the
   page stays usable while concurrent agents are mid-run. The validation
   files under data/lthcs/backtest/<date>_validation/ are the canonical
   shape; the per-horizon dirs (<date>_h1, _h5, _h21) are fallbacks.
   ========================================================================= */

const DATA_ROOT = '../data/lthcs';
const VALIDATION_DATE = '2026-05-18';
const HORIZON = 'horizon_21d';        // primary display horizon
const HORIZON_DAYS = 21;
const NOISE_FLOOR = 0.04;             // |IC| < 0.04 is in the noise band at this sample size
const SHIP_GATE = 0.04;               // walk-forward test IC ship threshold

// ----- Profile selector (Tier 5 #24 P3 follow-on) -----------------------
// The engine ships 4 strategy profiles + the baseline. The baseline's
// artifacts live in the validation dir root; the other 4 live in
// ./profiles/<name>/. localStorage mirrors lthcs_tab's narrative-source
// toggle (Tier 5 #23 Phase 2 UI).
const PROFILE_STORAGE_KEY = 'lthcs.backtest.profile';
const PROFILE_BASELINE = 'long_only_buy';
const PROFILE_LABELS = {
  long_only_buy: 'Baseline (long-only)',
  long_buy_short_review: 'Long/Short Review',
  dollar_neutral: 'Dollar Neutral',
  top_k_by_composite: 'Top-K Composite',
};
const PROFILE_VALID = new Set(Object.keys(PROFILE_LABELS));

function readProfilePref() {
  try {
    const v = window.localStorage.getItem(PROFILE_STORAGE_KEY);
    if (v && PROFILE_VALID.has(v)) return v;
  } catch (_e) { /* localStorage disabled — fall through */ }
  return PROFILE_BASELINE;
}
function writeProfilePref(value) {
  const v = PROFILE_VALID.has(value) ? value : PROFILE_BASELINE;
  try { window.localStorage.setItem(PROFILE_STORAGE_KEY, v); } catch (_e) { /* ignore */ }
}
// The baseline artifacts live at the dir root; other profiles live under
// ./profiles/<name>/. The engine cron at 23:30 UTC produces the per-profile
// files — if a dir doesn't exist on disk yet, the underlying tryFetch
// returns null and the existing placeholder cards take over.
function engineDirFor(profile, base) {
  if (profile === PROFILE_BASELINE) return base;
  return `${base}/profiles/${profile}`;
}

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

/* ----- Fetch helpers --------------------------------------------------- */
async function tryFetch(url) {
  try {
    const r = await fetch(url, { cache: 'no-cache' });
    if (!r.ok) return null;
    return await r.json();
  } catch { return null; }
}
async function tryFetchText(url) {
  try {
    const r = await fetch(url, { cache: 'no-cache' });
    if (!r.ok) return null;
    return await r.text();
  } catch { return null; }
}

/* ----- Number formatters ----------------------------------------------- */
function fmtIC(x) {
  if (x == null || Number.isNaN(x)) return 'n/a';
  const sign = x >= 0 ? '+' : '';
  return `${sign}${x.toFixed(3)}`;
}
function fmtPct(x) {
  if (x == null || Number.isNaN(x)) return 'n/a';
  const sign = x >= 0 ? '+' : '';
  return `${sign}${(x * 100).toFixed(1)}%`;
}
function fmtSharpe(x) {
  if (x == null || Number.isNaN(x)) return 'n/a';
  const sign = x >= 0 ? '+' : '';
  return `${sign}${x.toFixed(2)}`;
}
function icClass(x) {
  if (x == null || Number.isNaN(x)) return 'noise';
  if (Math.abs(x) < NOISE_FLOOR) return 'noise';
  return x >= 0 ? 'pos' : 'neg';
}

/* ======================================================================
   Bootstrap
   ====================================================================== */
async function main() {
  const loading = $('bt-loading');
  const errBox = $('bt-error');
  const content = $('bt-content');

  // Canonical paths first, per-horizon fallbacks second.
  const base = `${DATA_ROOT}/backtest/${VALIDATION_DATE}_validation`;
  const fallback = `${DATA_ROOT}/backtest/${VALIDATION_DATE}_h21`;

  const [
    summary, pillarIC, quintile, portfolio, bandReturns,
    fbSummary, fbPillarIC, fbQuintile, fbPortfolio,
  ] = await Promise.all([
    tryFetch(`${base}/summary.json`),
    tryFetch(`${base}/pillar_ic.json`),
    tryFetch(`${base}/quintile_spreads.json`),
    tryFetch(`${base}/portfolio_returns.json`),
    tryFetch(`${base}/band_returns.json`),
    tryFetch(`${fallback}/summary.json`),
    tryFetch(`${fallback}/pillar_ic.json`),
    tryFetch(`${fallback}/quintile_returns.json`),
    tryFetch(`${fallback}/portfolio_returns.json`),
  ]);
  // Engine artifacts depend on the selected profile (P3 follow-on). Loaded
  // separately so the chip handler can re-fetch without touching the rest.
  const initialProfile = readProfilePref();
  const engineBundle = await loadEngineBundle(initialProfile, base);
  const { engineSummary } = engineBundle;
  const wfMd = await tryFetchText(
    `${DATA_ROOT}/adaptive_weights/${VALIDATION_DATE}_walk_forward_summary.md`,
  );
  const wf = parseWalkForwardMd(wfMd);

  // Pull h21 slice out of validation dicts; the per-horizon fallbacks are
  // already in flat shape (matching the older 2026-05-18_h21 layout).
  const summaryH = pickHorizon(summary, HORIZON) || fbSummary;
  const pillarRows = pickHorizon(pillarIC, HORIZON) || fbPillarIC;
  const quintileH = pickHorizon(quintile, HORIZON) || fbQuintile;
  const portfolioH = pickHorizon(portfolio, HORIZON) || fbPortfolio;

  // Hard "nothing has landed yet" — entire backtest output missing AND
  // no walk-forward markdown.
  if (!summaryH && !pillarRows && !portfolioH && !quintileH && !wf) {
    loading.classList.add('hidden');
    errBox.classList.remove('hidden');
    errBox.replaceChildren(
      el('strong', { text: 'Validation in progress.' }),
      el('br'),
      document.createTextNode(
        'No backtest or walk-forward output is on disk yet. The parallel agents are still running.',
      ),
      el('br'), el('br'),
      makeRetryButton(),
    );
    return;
  }

  renderHeader(summary, summaryH);
  renderVerdict(summary, summaryH, pillarRows, wf, engineSummary);
  renderPillarIC(pillarRows);
  renderPnL(portfolioH, summaryH);
  renderEngineBundle(engineBundle);
  renderQuintile(quintileH);
  renderWalkForward(wf);
  renderCohort(wf);

  // Wire up the profile selector once the page is wired. The chip click
  // re-fetches just the engine bundle and re-renders the Engine P&L section
  // — leaves the per-pillar IC, walk-forward CV, etc. untouched (those
  // numbers are profile-independent at the validation layer).
  setupProfileToggle(base, initialProfile);

  loading.classList.add('hidden');
  content.classList.remove('hidden');
}

/* ----- Engine artifact bundle loader (profile-aware) -------------------- */
/* Fetches the 5 engine files from the profile's dir (or the validation dir
   root for baseline). Returns null fields when a file is missing — the
   downstream renderers already render a placeholder card in that case. */
async function loadEngineBundle(profile, base) {
  const dir = engineDirFor(profile, base);
  const [engineSummary, equityCurve, bandCurves, benchmarkCurve, pillarAttribution] = await Promise.all([
    tryFetch(`${dir}/engine_summary.json`),
    tryFetch(`${dir}/equity_curve.json`),
    tryFetch(`${dir}/band_curves.json`),
    tryFetch(`${dir}/benchmark_curve.json`),
    tryFetch(`${dir}/pillar_attribution.json`),
  ]);
  return { profile, engineSummary, equityCurve, bandCurves, benchmarkCurve, pillarAttribution };
}

function renderEngineBundle(bundle) {
  const { profile, engineSummary, equityCurve, bandCurves, benchmarkCurve, pillarAttribution } = bundle;
  // If a non-baseline profile dir doesn't exist on disk (cron hasn't run for
  // this profile yet), every file is null. Show a profile-specific placeholder
  // instead of the generic "not yet computed" message.
  const statsEl = $('engine-stats');
  if (profile !== PROFILE_BASELINE
      && !engineSummary && !equityCurve && !bandCurves && !benchmarkCurve) {
    if (statsEl) statsEl.replaceChildren();
    const svg = $('engine-chart');
    if (svg) svg.replaceChildren();
    const legendEl = $('engine-legend');
    if (legendEl) legendEl.replaceChildren();
    const bandChartEl = $('engine-band-chart');
    if (bandChartEl) bandChartEl.replaceChildren();
    const label = PROFILE_LABELS[profile] || profile;
    if (statsEl) statsEl.appendChild(makePlaceholder(
      `Profile "${label}" data not yet available — the engine daily cron (23:30 UTC) produces it.`,
    ));
    // Clear attribution chart too (profile-specific files don't ship attribution).
    const attribEl = $('engine-attribution-chart');
    if (attribEl) attribEl.replaceChildren();
    const attribNote = $('engine-attribution-note');
    if (attribNote) attribNote.replaceChildren();
    if (attribEl) attribEl.appendChild(makePlaceholder(
      `Pillar attribution is only computed for the baseline profile.`,
    ));
    return;
  }
  renderEngine(engineSummary, equityCurve, bandCurves, benchmarkCurve);
  renderPillarAttribution(pillarAttribution);
}

/* ----- Profile selector wire-up ---------------------------------------- */
function setupProfileToggle(base, currentProfile) {
  const toggle = $('engine-profile-toggle');
  const caption = $('engine-profile-caption');
  if (!toggle) return;

  syncProfileChips(toggle, caption, currentProfile);

  toggle.addEventListener('click', async (ev) => {
    const target = ev.target.closest('.lbt-profile-chip');
    if (!target) return;
    const next = target.dataset.profile;
    if (!next || !PROFILE_VALID.has(next)) return;
    if (target.classList.contains('is-active')) return;  // no-op
    syncProfileChips(toggle, caption, next);
    writeProfilePref(next);
    const bundle = await loadEngineBundle(next, base);
    renderEngineBundle(bundle);
  });
}

function syncProfileChips(toggle, caption, profile) {
  const chips = toggle.querySelectorAll('.lbt-profile-chip');
  for (const chip of chips) {
    const isActive = chip.dataset.profile === profile;
    chip.classList.toggle('is-active', isActive);
    chip.setAttribute('aria-checked', isActive ? 'true' : 'false');
  }
  if (caption) {
    caption.textContent = `currently showing: ${PROFILE_LABELS[profile] || profile}`;
  }
}

/* Pull a horizon slice from a {horizon_Nd: ...} dict. If the data is
   already flat (older _h21 shape), pass through. */
function pickHorizon(obj, horizonKey) {
  if (!obj || typeof obj !== 'object') return null;
  if (Object.prototype.hasOwnProperty.call(obj, horizonKey)) return obj[horizonKey];
  return null;
}

function makeRetryButton() {
  const btn = el('button', { class: 'lbt-retry-btn', type: 'button', text: 'Retry now' });
  btn.addEventListener('click', () => window.location.reload());
  return btn;
}

function renderHeader(summary, summaryH) {
  const gen = summary?.generated_at;
  let label = VALIDATION_DATE;
  if (gen) {
    const iso = gen.replace('T', ' ').replace(/\..+Z$/, ' UTC');
    label = `${VALIDATION_DATE} (${iso})`;
  }
  $('bt-generated').textContent = label;

  if (summary || summaryH) {
    const days = summary?.n_observation_dates ?? summaryH?.n_observation_dates ?? 90;
    const tradingDays = Math.round(days * (5 / 7));
    const tickers = summary?.n_tickers ?? summaryH?.n_tickers ?? '?';
    $('bt-subtitle').textContent =
      `${days} days · ${tradingDays} trading days · ${tickers} tickers · horizon ${HORIZON_DAYS}d`;
  }
}

/* ======================================================================
   Headline verdict
   ====================================================================== */
function renderVerdict(summary, summaryH, pillarRows, wf, engineSummary) {
  const composite = (pillarRows || [])
    .find((p) => p.pillar === 'composite');
  const compIC = composite?.ic_mean ?? null;
  // Portfolio block shape varies between validation (sharpe_annualised on
  // the horizon slice) and the older fallback (.sharpe at root).
  const portfolioBlock = summary?.portfolio?.[HORIZON]
    ?? summary?.portfolio
    ?? summaryH?.portfolio
    ?? summaryH
    ?? null;
  // Prefer the engine's non-overlapping Sharpe when available; fall back to
  // the legacy overlapping number so the card still renders before the
  // engine has run for the first time.
  const engineBlock = engineSummary?.summary ?? null;
  const sharpe = engineBlock?.sharpe
    ?? portfolioBlock?.sharpe_annualised
    ?? portfolioBlock?.sharpe
    ?? null;
  const sharpeIsEngine = engineBlock?.sharpe != null;
  const totalReturn = engineBlock?.total_return ?? portfolioBlock?.cumulative_return ?? null;
  const testIC = wf?.bestTestIC ?? null;

  // Verdict: every gate has to clear noise/ship floor to say YES.
  let verdict = 'pending';
  let passed = 0, evaluated = 0;
  if (compIC != null) {
    evaluated += 1;
    if (Math.abs(compIC) >= NOISE_FLOOR && compIC > 0) passed += 1;
  }
  if (sharpe != null) {
    evaluated += 1;
    if (sharpe > 0) passed += 1;
  }
  if (testIC != null) {
    evaluated += 1;
    if (testIC >= SHIP_GATE) passed += 1;
  }
  // If the walk-forward authoritatively says HOLD/REJECT, that downgrades
  // the verdict even when the raw numbers look strong. The markdown for
  // 2026-05-18 explicitly flags the test IC as a structural artifact.
  const wfVerdict = (wf?.verdict || '').toLowerCase();
  if (wfVerdict.includes('hold')) verdict = 'mixed';
  else if (wfVerdict.includes('reject')) verdict = 'no';
  else if (evaluated > 0) {
    if (passed === evaluated) verdict = 'yes';
    else if (passed === 0) verdict = 'no';
    else verdict = 'mixed';
  }

  const tag = $('verdict-tag');
  tag.dataset.verdict = verdict;
  tag.textContent = verdict === 'pending' ? 'PENDING' : verdict.toUpperCase();

  const icEl = $('verdict-ic');
  icEl.className = icClass(compIC);
  icEl.replaceChildren(
    document.createTextNode(fmtIC(compIC)),
    el('small', { text: composite ? `n=${composite.n_obs ?? 0} obs · noise floor ±${NOISE_FLOOR}` : 'data pending' }),
  );

  const shEl = $('verdict-sharpe');
  shEl.className = sharpe == null ? 'neutral' : (sharpe > 0 ? 'pos' : 'neg');
  const sharpeSubtitle = sharpeIsEngine
    ? `engine, non-overlap · total ${fmtPct(totalReturn)}`
    : (portfolioBlock ? `cum. return ${fmtPct(portfolioBlock.cumulative_return)}` : 'portfolio pending');
  shEl.replaceChildren(
    document.createTextNode(fmtSharpe(sharpe)),
    el('small', { text: sharpeSubtitle }),
  );

  const wfEl = $('verdict-wf');
  if (testIC == null) {
    wfEl.className = 'neutral';
    wfEl.replaceChildren(
      document.createTextNode('n/a'),
      el('small', { text: 'walk-forward pending' }),
    );
  } else {
    wfEl.className = icClass(testIC);
    const gateHit = testIC >= SHIP_GATE ? '✓' : '✗';
    wfEl.replaceChildren(
      document.createTextNode(fmtIC(testIC)),
      el('small', { text: `ship gate (>${SHIP_GATE.toFixed(2)}): ${gateHit}` }),
    );
  }
}

/* ======================================================================
   Per-pillar IC ranking
   ====================================================================== */
const PILLAR_LABELS = {
  composite: 'Composite',
  institutional_confidence: 'Institutional Confidence',
  financial_evolution: 'Financial Evolution',
  des: 'DES',
  adoption_momentum: 'Adoption Momentum',
  thesis_integrity: 'Thesis Integrity',
};

function renderPillarIC(rows) {
  const container = $('pillar-ic-chart');
  container.replaceChildren();

  if (!rows || rows.length === 0) {
    container.appendChild(makePlaceholder(
      'Pillar IC data not yet available. The per-pillar IC step of the backtest agent has not produced output.',
    ));
    return;
  }

  // Drop composite (shown in the verdict card), sort by ic_mean desc.
  const pillars = rows
    .filter((r) => r.pillar !== 'composite')
    .sort((a, b) => (b.ic_mean ?? 0) - (a.ic_mean ?? 0));

  // Symmetric scale around zero, padded so the noise band is legible.
  const maxAbs = Math.max(
    ...pillars.map((p) => Math.abs(p.ic_mean ?? 0)),
    NOISE_FLOOR * 2,
  );

  for (const p of pillars) {
    const ic = p.ic_mean ?? 0;
    const isNoise = Math.abs(ic) < NOISE_FLOOR;
    const sign = ic >= 0 ? 'pos' : 'neg';
    const widthPct = (Math.abs(ic) / maxAbs) * 50;
    const left = ic >= 0 ? 50 : 50 - widthPct;

    const row = el('div', { class: 'lbt-pillar-row' });
    row.appendChild(el('div', { class: 'lbt-pillar-name', text: PILLAR_LABELS[p.pillar] || p.pillar }));

    const bar = el('div', { class: 'lbt-pillar-bar' });
    const fill = el('div', { class: 'lbt-pillar-bar-fill' });
    fill.dataset.sign = sign;
    if (isNoise) fill.dataset.noise = 'true';
    fill.style.left = `${left}%`;
    fill.style.width = `${widthPct}%`;
    bar.appendChild(fill);
    row.appendChild(bar);

    const statClass = isNoise ? 'noise' : sign;
    const stat = el('div', { class: `lbt-pillar-stat ${statClass}`, text: fmtIC(ic) });
    row.appendChild(stat);
    container.appendChild(row);
  }
}

/* ======================================================================
   Band portfolio P&L curve (SVG)
   ====================================================================== */
function renderPnL(portfolioH, summaryH) {
  const svg = $('pnl-chart');
  const statsEl = $('pnl-stats');
  svg.replaceChildren();
  statsEl.replaceChildren();

  // The validation file has shape {horizon_21d: {daily_returns: {date: val},
  // cumulative_return, sharpe_annualised, max_drawdown, ...}}.
  // The fallback _h21 file has the same inner shape but no outer horizon key.
  const block = portfolioH || summaryH?.portfolio || null;
  const dailyReturns = block?.daily_returns
    ?? (summaryH?.portfolio?.[HORIZON]?.daily_returns)
    ?? null;

  if (!block || !dailyReturns) {
    statsEl.appendChild(makePlaceholder(
      'Portfolio P&L data not yet available. The band-portfolio step has not produced output.',
    ));
    return;
  }

  const dates = Object.keys(dailyReturns).sort();
  // The series is a cumulative-return path (monotone-ish, ends at ~+280%).
  // Chart the path directly; summary fields (sharpe, max_drawdown, hit_rate)
  // are passed through verbatim.
  const series = dates.map((d) => ({ d, v: dailyReturns[d] }));

  // ----- Stat strip -----
  const sharpe = block.sharpe_annualised ?? block.sharpe ?? null;
  const stats = [
    ['Sharpe (ann.)', fmtSharpe(sharpe)],
    ['Cum. return', fmtPct(block.cumulative_return)],
    ['Max DD', fmtPct(block.max_drawdown)],
    ['Hit rate', block.hit_rate != null ? `${Math.round(block.hit_rate * 100)}%` : 'n/a'],
    ['Rebalances', `${block.n_rebalances ?? series.length}`],
  ];
  for (const [k, v] of stats) {
    const wrap = el('span');
    wrap.appendChild(document.createTextNode(`${k}:`));
    wrap.appendChild(el('strong', { text: v }));
    statsEl.appendChild(wrap);
  }

  // ----- SVG chart -----
  const VB_W = 800, VB_H = 280;
  const ML = 60, MR = 16, MT = 14, MB = 26;
  const W = VB_W - ML - MR;
  const H = VB_H - MT - MB;

  const vMin = Math.min(0, ...series.map((s) => s.v));
  const vMax = Math.max(0, ...series.map((s) => s.v));
  const vSpan = vMax - vMin || 1;

  const xAt = (i) => ML + (W * i) / Math.max(1, series.length - 1);
  const yAt = (v) => MT + H - ((v - vMin) / vSpan) * H;

  const gridVals = [vMin, vMin + vSpan / 4, vMin + vSpan / 2, vMin + (3 * vSpan) / 4, vMax];
  if (vMin < 0 && vMax > 0 && !gridVals.includes(0)) gridVals.push(0);

  for (const gv of gridVals) {
    const y = yAt(gv);
    const isZero = Math.abs(gv) < 1e-9;
    svg.appendChild(svgEl('line', {
      x1: ML, x2: VB_W - MR, y1: y, y2: y,
      class: isZero ? 'lbt-pnl-zero' : 'lbt-pnl-grid',
    }));
    const t = svgEl('text', {
      x: ML - 6, y: y + 3,
      class: 'lbt-pnl-tick',
      'text-anchor': 'end',
    });
    t.textContent = fmtPct(gv);
    svg.appendChild(t);
  }

  // X-axis: first/mid/last date.
  const xTicks = [0, Math.floor(series.length / 2), series.length - 1];
  for (const i of xTicks) {
    const s = series[i];
    if (!s) continue;
    const t = svgEl('text', {
      x: xAt(i), y: VB_H - 8,
      class: 'lbt-pnl-tick',
      'text-anchor': i === 0 ? 'start' : i === series.length - 1 ? 'end' : 'middle',
    });
    t.textContent = s.d;
    svg.appendChild(t);
  }

  let linePath = '';
  for (let i = 0; i < series.length; i += 1) {
    const x = xAt(i);
    const y = yAt(series[i].v);
    linePath += `${i === 0 ? 'M' : 'L'}${x.toFixed(2)},${y.toFixed(2)} `;
  }
  const baselineY = yAt(Math.max(vMin, 0));
  const areaPath = linePath
    + `L${xAt(series.length - 1).toFixed(2)},${baselineY.toFixed(2)} `
    + `L${xAt(0).toFixed(2)},${baselineY.toFixed(2)} Z`;

  svg.appendChild(svgEl('path', { d: areaPath, class: 'lbt-pnl-area' }));
  svg.appendChild(svgEl('path', { d: linePath.trim(), class: 'lbt-pnl-line' }));
}

/* ======================================================================
   Engine equity curve (non-overlapping, look-ahead-guarded)
   ====================================================================== */
const ENGINE_BAND_ORDER = [
  'elite', 'high_confidence', 'constructive', 'monitor', 'weakening', 'review',
];
const ENGINE_BAND_LABELS = {
  elite: 'Elite',
  high_confidence: 'High Confidence',
  constructive: 'Constructive',
  monitor: 'Monitor',
  weakening: 'Weakening',
  review: 'Review',
};

function renderEngine(engineSummary, equityCurve, bandCurves, benchmarkCurve) {
  const statsEl = $('engine-stats');
  const svg = $('engine-chart');
  const legendEl = $('engine-legend');
  const bandChartEl = $('engine-band-chart');
  if (!statsEl || !svg || !legendEl || !bandChartEl) return;
  statsEl.replaceChildren();
  svg.replaceChildren();
  legendEl.replaceChildren();
  bandChartEl.replaceChildren();

  const s = engineSummary?.summary ?? null;
  if (!s || !equityCurve || Object.keys(equityCurve).length === 0) {
    statsEl.appendChild(makePlaceholder(
      'Engine output not yet computed for this run. Re-run with `--engine pnl` or wait for the nightly backtest cron.',
    ));
    return;
  }

  // ----- Stat strip -----
  const meta = engineSummary?.run_meta ?? {};
  const win = meta.window ?? {};
  const params = s.params ?? {};
  const stats = [
    ['Sharpe (ann.)', fmtSharpe(s.sharpe)],
    ['Sortino (ann.)', fmtSharpe(s.sortino)],
    ['Total return', fmtPct(s.total_return)],
    ['Ann. return', fmtPct(s.ann_return)],
    ['Max DD', fmtPct(s.max_drawdown)],
    ['Hit rate', s.hit_rate != null ? `${Math.round(s.hit_rate * 100)}%` : 'n/a'],
    ['Avg hold', `${(s.avg_hold_days ?? 0).toFixed(1)}d`],
    ['Turnover/dy', `${((s.turnover ?? 0) * 100).toFixed(1)}%`],
    ['Trades', `${s.n_trades ?? 0}`],
    ['Unique tkr', `${s.n_unique_tkr ?? 0}`],
  ];
  for (const [k, v] of stats) {
    const wrap = el('span');
    wrap.appendChild(document.createTextNode(`${k}:`));
    wrap.appendChild(el('strong', { text: v }));
    statsEl.appendChild(wrap);
  }
  const winSpan = el('span', { class: 'lbt-engine-window' });
  winSpan.appendChild(document.createTextNode(
    `${win.start ?? '?'} → ${win.end ?? '?'} · ${win.n_trading_days ?? '?'} td · ${meta.universe_size ?? '?'} tkrs · cost ${params.cost_bps ?? 5}bps/side`,
  ));
  statsEl.appendChild(winSpan);

  // ----- Equity curve SVG -----
  const dates = Object.keys(equityCurve).sort();
  if (dates.length < 2) {
    statsEl.appendChild(makePlaceholder('Equity curve too short to chart (need ≥2 trading days).'));
    return;
  }
  const series = dates.map((d) => ({ d, v: equityCurve[d] }));
  // Optional benchmark series, reindexed to engine dates with last-value-carry.
  let benchSeries = null;
  if (benchmarkCurve && Object.keys(benchmarkCurve).length >= 2) {
    let last = null;
    benchSeries = dates.map((d) => {
      if (benchmarkCurve[d] != null) last = benchmarkCurve[d];
      return { d, v: last };
    }).filter((p) => p.v != null);
  }

  const VB_W = 800, VB_H = 280;
  const ML = 60, MR = 16, MT = 14, MB = 26;
  const W = VB_W - ML - MR;
  const H = VB_H - MT - MB;

  const allVals = series.map((p) => p.v).concat(benchSeries ? benchSeries.map((p) => p.v) : []);
  const vMin = Math.min(...allVals);
  const vMax = Math.max(...allVals);
  const vSpan = (vMax - vMin) || 1;
  const padFrac = 0.05;
  const yMin = vMin - vSpan * padFrac;
  const yMax = vMax + vSpan * padFrac;
  const ySpan = yMax - yMin || 1;

  const xAt = (i, total) => ML + (W * i) / Math.max(1, total - 1);
  const yAt = (v) => MT + H - ((v - yMin) / ySpan) * H;

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
  // X axis labels: first, middle, last.
  for (const i of [0, Math.floor(series.length / 2), series.length - 1]) {
    const x = xAt(i, series.length);
    const t = svgEl('text', {
      x, y: VB_H - 8,
      class: 'lbt-pnl-xaxis',
      'text-anchor': 'middle',
    });
    t.textContent = series[i].d;
    svg.appendChild(t);
  }

  // Benchmark line (drawn first so the strategy line sits on top).
  if (benchSeries && benchSeries.length >= 2) {
    let benchPath = '';
    benchSeries.forEach((p, i) => {
      const cmd = i === 0 ? 'M' : 'L';
      benchPath += `${cmd}${xAt(i, benchSeries.length)},${yAt(p.v)} `;
    });
    svg.appendChild(svgEl('path', {
      d: benchPath.trim(),
      class: 'lbt-engine-bench',
    }));
  }
  // Strategy line + area.
  let linePath = '';
  let areaPath = `M${xAt(0, series.length)},${yAt(1.0)} `;
  series.forEach((p, i) => {
    const cmd = i === 0 ? 'M' : 'L';
    linePath += `${cmd}${xAt(i, series.length)},${yAt(p.v)} `;
    areaPath += `L${xAt(i, series.length)},${yAt(p.v)} `;
  });
  areaPath += `L${xAt(series.length - 1, series.length)},${yAt(1.0)} Z`;
  svg.appendChild(svgEl('path', { d: areaPath, class: 'lbt-engine-area' }));
  svg.appendChild(svgEl('path', { d: linePath.trim(), class: 'lbt-engine-line' }));

  // ----- Legend -----
  legendEl.appendChild(el('span', { class: 'lbt-legend-item lbt-legend-strategy', text: 'Strategy (long elite/high/constructive)' }));
  if (benchSeries && benchSeries.length >= 2) {
    legendEl.appendChild(el('span', { class: 'lbt-legend-item lbt-legend-bench', text: 'Benchmark (SPY)' }));
  }

  // ----- Per-band sub-portfolio bar chart -----
  renderEngineBandChart(bandCurves);
}

function renderEngineBandChart(bandCurves) {
  const container = $('engine-band-chart');
  if (!container) return;
  container.replaceChildren();
  if (!bandCurves || typeof bandCurves !== 'object') {
    container.appendChild(makePlaceholder('Per-band sweep not yet computed.'));
    return;
  }
  // Compute total return per band.
  const rows = [];
  for (const band of ENGINE_BAND_ORDER) {
    const curve = bandCurves[band] || {};
    const vals = Object.keys(curve).sort().map((k) => curve[k]).filter((v) => v != null);
    if (vals.length < 2 || vals[0] <= 0) {
      rows.push({ band, total: null });
      continue;
    }
    const total = vals[vals.length - 1] / vals[0] - 1;
    rows.push({ band, total });
  }
  const valid = rows.filter((r) => r.total != null);
  if (valid.length === 0) {
    container.appendChild(makePlaceholder('Per-band sub-portfolio curves all empty.'));
    return;
  }
  const maxAbs = Math.max(...valid.map((r) => Math.abs(r.total)), 0.01);
  for (const r of rows) {
    const row = el('div', { class: 'lbt-band-row' });
    row.appendChild(el('div', {
      class: 'lbt-band-name',
      text: ENGINE_BAND_LABELS[r.band] || r.band,
    }));
    const bar = el('div', { class: 'lbt-band-bar' });
    if (r.total == null) {
      bar.appendChild(el('span', { class: 'lbt-band-empty', text: '—' }));
    } else {
      const sign = r.total >= 0 ? 'pos' : 'neg';
      const widthPct = (Math.abs(r.total) / maxAbs) * 50;
      const left = r.total >= 0 ? 50 : 50 - widthPct;
      const fill = el('div', { class: 'lbt-band-bar-fill' });
      fill.dataset.sign = sign;
      fill.dataset.band = r.band;
      fill.style.left = `${left}%`;
      fill.style.width = `${widthPct}%`;
      bar.appendChild(fill);
    }
    row.appendChild(bar);
    row.appendChild(el('div', {
      class: r.total == null ? 'lbt-band-stat noise' : `lbt-band-stat ${r.total >= 0 ? 'pos' : 'neg'}`,
      text: r.total == null ? 'n/a' : fmtPct(r.total),
    }));
    container.appendChild(row);
  }
}

/* ======================================================================
   Per-pillar attribution (Tier 5 #24, Phase 2)
   ----------------------------------------------------------------------
   Reads ``pillar_attribution.json`` and renders a Δ-Sharpe bar chart,
   one row per pillar. Δ = "engine Sharpe with this pillar's weight
   zeroed and the other four renormalized" minus the baseline engine
   Sharpe. A NEGATIVE Δ means removing that pillar HURT the strategy
   (i.e. the pillar contributed). A POSITIVE Δ means removing the
   pillar HELPED (the pillar was a drag).

   IMPORTANT: per spec §5, pillar attributions are NOT additive — the
   sum of the bars is not the total Sharpe. The tooltip on the section
   header surfaces this caveat. Modeled on renderEngineBandChart.
   ====================================================================== */
function renderPillarAttribution(payload) {
  const container = $('engine-attribution-chart');
  const noteEl = $('engine-attribution-note');
  if (!container) return;  // section absent (older index.html)
  container.replaceChildren();
  if (noteEl) noteEl.replaceChildren();

  if (!payload || typeof payload !== 'object' || !payload.per_pillar) {
    container.appendChild(makePlaceholder(
      'Pillar attribution not yet computed. Re-run the engine with --attribute.',
    ));
    return;
  }

  // Order matches PILLAR_LABELS top-down so the bar chart aligns with
  // the per-pillar IC ranking above. Drop composite (not a pillar).
  const order = [
    'adoption_momentum',
    'institutional_confidence',
    'financial_evolution',
    'thesis_integrity',
    'des',
  ];

  // Collect Δ-Sharpe (and absolute baseline numbers for the tooltip).
  const baselineSharpe = payload?.baseline_summary?.sharpe;
  const rows = order.map((p) => {
    const entry = payload.per_pillar[p];
    if (!entry || entry.status !== 'ok') {
      return { pillar: p, delta: null, variantSharpe: null };
    }
    return {
      pillar: p,
      delta: typeof entry.delta_sharpe === 'number' ? entry.delta_sharpe : null,
      variantSharpe: typeof entry.variant_sharpe === 'number'
        ? entry.variant_sharpe
        : entry?.variant_summary?.sharpe ?? null,
      deltaReturn: typeof entry.delta_total_return === 'number'
        ? entry.delta_total_return
        : null,
    };
  });

  const valid = rows.filter((r) => r.delta != null);
  if (valid.length === 0) {
    container.appendChild(makePlaceholder(
      'Per-pillar attribution had no usable Δ-Sharpe values (variant runs all skipped).',
    ));
    return;
  }

  // Symmetric scale around zero, modest floor so a flat run still
  // renders distinguishable bars.
  const maxAbs = Math.max(...valid.map((r) => Math.abs(r.delta)), 0.05);

  for (const r of rows) {
    const row = el('div', { class: 'lbt-attrib-row' });
    row.appendChild(el('div', {
      class: 'lbt-attrib-name',
      text: PILLAR_LABELS[r.pillar] || r.pillar,
    }));
    const bar = el('div', { class: 'lbt-attrib-bar' });
    if (r.delta == null) {
      bar.appendChild(el('span', { class: 'lbt-attrib-empty', text: 'n/a' }));
    } else {
      const sign = r.delta >= 0 ? 'pos' : 'neg';
      const widthPct = (Math.abs(r.delta) / maxAbs) * 50;
      const left = r.delta >= 0 ? 50 : 50 - widthPct;
      const fill = el('div', { class: 'lbt-attrib-bar-fill' });
      fill.dataset.sign = sign;
      fill.style.left = `${left}%`;
      fill.style.width = `${widthPct}%`;
      // Tooltip: full context for the pillar (variant Sharpe, Δret).
      let tip = `Δsharpe ${r.delta >= 0 ? '+' : ''}${r.delta.toFixed(3)}`;
      if (r.variantSharpe != null) {
        tip += ` (variant ${r.variantSharpe.toFixed(3)} vs baseline ${(baselineSharpe ?? 0).toFixed(3)})`;
      }
      if (r.deltaReturn != null) {
        tip += ` | Δret ${r.deltaReturn >= 0 ? '+' : ''}${(r.deltaReturn * 100).toFixed(2)}%`;
      }
      fill.title = tip;
      bar.appendChild(fill);
    }
    row.appendChild(bar);
    const statCls = r.delta == null
      ? 'lbt-attrib-stat noise'
      : `lbt-attrib-stat ${r.delta >= 0 ? 'pos' : 'neg'}`;
    row.appendChild(el('div', {
      class: statCls,
      text: r.delta == null
        ? 'n/a'
        : `${r.delta >= 0 ? '+' : ''}${r.delta.toFixed(3)}`,
    }));
    container.appendChild(row);
  }

  // Surface the non-additive caveat from the JSON note field (spec §5).
  // The producer always writes a `note` string; we read it back as the
  // tooltip body rather than hardcoding the warning here.
  if (noteEl) {
    const note = (payload.note || '').trim();
    if (note) {
      noteEl.textContent = note;
      noteEl.title = note;
    }
  }
}

/* ======================================================================
   Quintile spread per pillar
   ====================================================================== */
function renderQuintile(quintileH) {
  const container = $('quintile-chart');
  container.replaceChildren();

  if (!quintileH || typeof quintileH !== 'object') {
    container.appendChild(makePlaceholder(
      'Quintile-spread data not yet available. The quintile-spread step has not produced output.',
    ));
    return;
  }

  // Validation shape: {pillar: {Q1..Q5,Q5-Q1: {n_obs, mean, std, t_stat_vs_zero}}}
  // Older shape: {pillar: {Q1..Q5: {date: value}}}  — handle both.
  const pillars = Object.keys(quintileH);
  const rowsData = [];
  for (const p of pillars) {
    const q = quintileH[p];
    if (!q || typeof q !== 'object') continue;
    const qVals = {};
    for (const k of ['Q1', 'Q2', 'Q3', 'Q4', 'Q5']) {
      const cell = q[k];
      if (!cell) { qVals[k] = null; continue; }
      if (typeof cell === 'number') { qVals[k] = cell; continue; }
      // Object — either {mean, ...} or {date: value, ...}
      if (typeof cell.mean === 'number') { qVals[k] = cell.mean; continue; }
      const dates = Object.keys(cell).sort();
      qVals[k] = dates.length > 0 && typeof cell[dates[dates.length - 1]] === 'number'
        ? cell[dates[dates.length - 1]]
        : null;
    }
    // Pull Q5-Q1 directly if the producer included it.
    const directSpread = (q['Q5-Q1'] && typeof q['Q5-Q1'] === 'object')
      ? q['Q5-Q1'].mean
      : null;
    rowsData.push({ pillar: p, qVals, directSpread });
  }
  if (rowsData.length === 0) {
    container.appendChild(makePlaceholder('Quintile data has unexpected shape; cannot render.'));
    return;
  }

  const allValues = rowsData.flatMap((r) => Object.values(r.qVals).filter((v) => v != null));
  const maxAbs = Math.max(...allValues.map((v) => Math.abs(v)), 0.05);

  for (const { pillar, qVals, directSpread } of rowsData) {
    const row = el('div', { class: 'lbt-q-row' });
    row.appendChild(el('div', { class: 'lbt-q-name', text: PILLAR_LABELS[pillar] || pillar }));

    const barsBox = el('div', { class: 'lbt-q-bars' });
    for (const k of ['Q1', 'Q2', 'Q3', 'Q4', 'Q5']) {
      const v = qVals[k];
      const bar = el('div', { class: 'lbt-q-bar' });
      bar.dataset.q = k;
      if (v == null) {
        bar.style.height = '2px';
        bar.style.opacity = '0.2';
      } else {
        // Magnitude-only bars (mixed signs would be confusing in a stacked
        // mini-chart). Signed answer lives in the spread column.
        const h = Math.max(2, (Math.abs(v) / maxAbs) * 56);
        bar.style.height = `${h}px`;
        bar.title = `${k}: ${fmtPct(v)}`;
      }
      bar.appendChild(el('span', { class: 'lbt-q-bar-label', text: k }));
      barsBox.appendChild(bar);
    }
    row.appendChild(barsBox);

    let spread = directSpread;
    if (spread == null && qVals.Q1 != null && qVals.Q5 != null) {
      spread = qVals.Q5 - qVals.Q1;
    }
    const spreadCls = spread == null ? '' : (spread >= 0 ? 'pos' : 'neg');
    row.appendChild(el('div', {
      class: `lbt-q-spread ${spreadCls}`,
      text: spread == null ? 'n/a' : fmtPct(spread),
    }));
    container.appendChild(row);
  }
}

/* ======================================================================
   Walk-forward CV — parse the markdown summary
   ====================================================================== */
function parseWalkForwardMd(md) {
  if (!md) return null;
  const out = { rows: [], verdict: null, weights: {}, bestTestIC: null, cohorts: [] };

  // "**Verdict: HOLD.**" — strip bold + period.
  const verdictMatch = md.match(/Verdict\s*[:=]\s*\**\s*([A-Za-z]+)/i);
  if (verdictMatch) out.verdict = verdictMatch[1].toUpperCase();

  // Parse markdown tables. We look for rows whose first cell is numeric
  // (ridge_alpha sweep) or text (horizon / cohort sweep). Match defensively
  // and bucket rows by surrounding heading context.
  let currentTable = null;
  let currentHeading = '';
  const lines = md.split('\n');
  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i];
    const trimmed = line.trim();
    // Track section heading just before each table for context.
    if (/^#+\s/.test(trimmed)) {
      currentHeading = trimmed.replace(/^#+\s+/, '').toLowerCase();
      currentTable = null;
      continue;
    }
    if (/^\|[\s-:|]+\|$/.test(trimmed)) {
      // Separator row — header was line above.
      const header = (lines[i - 1] || '').trim();
      const cols = parseTableRow(header).map((s) => s.toLowerCase());
      currentTable = { header: cols, heading: currentHeading };
      continue;
    }
    if (!currentTable || !trimmed.startsWith('|')) continue;
    const cells = parseTableRow(trimmed);
    if (cells.length === 0) continue;
    const rec = {};
    for (let c = 0; c < currentTable.header.length && c < cells.length; c += 1) {
      rec[currentTable.header[c]] = cells[c];
    }

    // Identify the ridge_alpha sweep tables.
    if ('ridge_alpha' in rec) {
      out.rows.push({
        alpha: parseLooseFloat(rec.ridge_alpha),
        train_ic: parseLooseFloat(rec.train_ic),
        test_ic: parseLooseFloat(rec.test_ic),
        overfit_gap: parseLooseFloat(rec.overfit_gap),
        rec: rec.rec || rec.recommendation || null,
      });
    }
    // Per-horizon sweep table.
    if ('horizon' in rec && 'test_ic' in rec) {
      out.rows.push({
        alpha: rec.horizon,
        train_ic: parseLooseFloat(rec.train_ic),
        test_ic: parseLooseFloat(rec.test_ic),
        overfit_gap: parseLooseFloat(rec.overfit_gap),
        rec: rec.rec || rec.recommendation || null,
      });
    }
    // Per-cohort table.
    if ('cohort' in rec && 'test_ic' in rec) {
      // n_tickers might be "8" or "14"; sometimes "bank (8 tickers)" got into
      // the cohort cell with the count omitted — handle both.
      let cohortName = rec.cohort;
      let n = parseLooseInt(rec.n_tickers);
      const m = cohortName.match(/^(.*?)\s*\((\d+)\s*tickers?\)$/i);
      if (m) {
        cohortName = m[1].trim();
        if (n == null) n = parseInt(m[2], 10);
      }
      out.cohorts.push({
        cohort: cohortName,
        n,
        train_ic: parseLooseFloat(rec.train_ic),
        test_ic: parseLooseFloat(rec.test_ic),
        overfit_gap: parseLooseFloat(rec.overfit_gap),
        weight: rec['dominant weight'] || rec.dominant_weight || null,
      });
    }
  }

  if (out.rows.length > 0) {
    const testICs = out.rows.map((r) => r.test_ic).filter((x) => !Number.isNaN(x) && x != null);
    out.bestTestIC = testICs.length > 0 ? Math.max(...testICs) : null;
  }

  // Bullet "- key: value" weights.
  const weightLineRe = /^[-*]\s+([a-z_]+)\s*[:=]\s*([0-9.+-]+)/i;
  for (const line of lines) {
    const m = line.match(weightLineRe);
    if (m) {
      const k = m[1].toLowerCase();
      const v = parseFloat(m[2]);
      if (!Number.isNaN(v) && PILLAR_LABELS[k]) out.weights[k] = v;
    }
  }
  return out;
}

/* Pull cells from a markdown table row, stripping the | wrappers and
   normalizing whitespace. */
function parseTableRow(line) {
  return line
    .replace(/^\||\|$/g, '')
    .split('|')
    .map((s) => s.trim());
}
/* Numbers in the spec come with leading + (e.g. "+0.0920") or unicode minus
   "−0.1619" — handle both, plus an empty cell. */
function parseLooseFloat(s) {
  if (s == null) return NaN;
  const normalized = String(s).replace(/[−–—]/g, '-').replace(/^\+/, '').trim();
  if (normalized === '' || normalized === 'n/a') return NaN;
  return parseFloat(normalized);
}
function parseLooseInt(s) {
  if (s == null) return null;
  const n = parseInt(String(s).trim(), 10);
  return Number.isNaN(n) ? null : n;
}

function renderWalkForward(wf) {
  const statusEl = $('wf-status');
  const tbody = $('wf-tbody');
  const weightsBox = $('wf-weights');
  tbody.replaceChildren();
  weightsBox.replaceChildren();
  statusEl.replaceChildren();

  if (!wf) {
    statusEl.appendChild(makePlaceholder(
      'Walk-forward summary not yet available. The adaptive-weights agent has not produced its markdown summary.',
    ));
    return;
  }

  const verdictKey = (wf.verdict || '').toLowerCase();
  let pillVerdict = 'hold';
  if (verdictKey.includes('ship')) pillVerdict = 'ship';
  else if (verdictKey.includes('reject') || verdictKey === 'no') pillVerdict = 'reject';
  else if (verdictKey.includes('hold')) pillVerdict = 'hold';

  statusEl.append(
    document.createTextNode('Verdict: '),
    el('span', {
      class: 'lbt-pill', 'data-verdict': pillVerdict,
      text: wf.verdict || 'PENDING',
    }),
    document.createTextNode(
      `  ·  Best test IC ${fmtIC(wf.bestTestIC)}  ·  ship gate >${SHIP_GATE.toFixed(2)}`,
    ),
  );

  if (wf.rows.length === 0) {
    tbody.appendChild(el('tr', {}, [
      el('td', { colspan: '5', text: 'No ridge_alpha rows parsed from markdown summary.' }),
    ]));
  } else {
    for (const r of wf.rows) {
      const trainCls = icClass(r.train_ic);
      const testCls = icClass(r.test_ic);
      const gapCls = (r.overfit_gap != null && Math.abs(r.overfit_gap) > 0.10) ? 'neg' : '';
      // Trust the markdown's own recommendation when present (the 2026-05-18
      // summary marks rows "ship*" with a footnote — treat them as ship for
      // the column display; the SECTION verdict above gives the honest answer).
      let recPill = 'hold';
      const recRaw = (r.rec || '').toLowerCase();
      if (recRaw.includes('ship')) recPill = 'ship';
      else if (recRaw.includes('reject')) recPill = 'reject';
      else if (r.test_ic != null && r.test_ic >= SHIP_GATE) recPill = 'ship';

      const tr = el('tr');
      tr.appendChild(el('td', {
        text: typeof r.alpha === 'number'
          ? (Number.isNaN(r.alpha) ? 'n/a' : r.alpha.toFixed(3))
          : String(r.alpha ?? 'n/a'),
      }));
      tr.appendChild(el('td', { class: trainCls, text: fmtIC(r.train_ic) }));
      tr.appendChild(el('td', { class: testCls, text: fmtIC(r.test_ic) }));
      tr.appendChild(el('td', { class: gapCls, text: fmtIC(r.overfit_gap) }));
      const td = el('td');
      td.appendChild(el('span', {
        class: 'lbt-pill',
        'data-verdict': recPill,
        text: (r.rec || recPill).toString().replace(/\*+/g, '').trim() || recPill,
      }));
      tr.appendChild(td);
      tbody.appendChild(tr);
    }
  }

  // Recommended weights — only on a true SHIP verdict.
  if (pillVerdict === 'ship' && Object.keys(wf.weights).length > 0) {
    weightsBox.appendChild(el('h3', {
      class: 'lbt-pillar-name',
      style: 'margin:0 0 6px;font-size:12px;color:var(--text-secondary);text-transform:uppercase;letter-spacing:0.06em;',
      text: 'Recommended weights',
    }));
    for (const [k, v] of Object.entries(wf.weights)) {
      const card = el('div', { class: 'lbt-weights-card' });
      card.appendChild(el('dt', { text: PILLAR_LABELS[k] || k }));
      card.appendChild(el('dd', { text: v.toFixed(3) }));
      weightsBox.appendChild(card);
    }
  }
}

/* ======================================================================
   Per-cohort breakdown — sourced from the walk-forward markdown table
   ====================================================================== */
function renderCohort(wf) {
  const statusEl = $('cohort-status');
  const tbody = $('cohort-tbody');
  tbody.replaceChildren();
  statusEl.replaceChildren();

  const rows = wf?.cohorts || [];
  if (rows.length === 0) {
    statusEl.appendChild(makePlaceholder(
      'Per-cohort breakdown not yet available. The walk-forward summary has no cohort table.',
    ));
    return;
  }

  statusEl.textContent = 'Train/test IC and overfit gap per cohort (parsed from walk-forward summary). Test IC is the "would this generalize?" number.';

  // Re-target the header to match what we actually have.
  const thead = document.querySelector('#cohort-table thead tr');
  if (thead) {
    thead.replaceChildren(
      el('th', { scope: 'col', text: 'Cohort' }),
      el('th', { scope: 'col', text: 'N' }),
      el('th', { scope: 'col', text: 'Train IC' }),
      el('th', { scope: 'col', text: 'Test IC' }),
      el('th', { scope: 'col', text: 'Overfit gap' }),
    );
  }

  // Sort by test_ic desc; missing test_ic to the bottom.
  rows.sort((a, b) => (b.test_ic ?? -Infinity) - (a.test_ic ?? -Infinity));

  for (const r of rows) {
    const tr = el('tr');
    tr.appendChild(el('td', { text: r.cohort }));
    tr.appendChild(el('td', { text: r.n != null ? String(r.n) : 'n/a' }));
    tr.appendChild(el('td', { class: icClass(r.train_ic), text: fmtIC(r.train_ic) }));
    tr.appendChild(el('td', { class: icClass(r.test_ic), text: fmtIC(r.test_ic) }));
    const gapCls = (r.overfit_gap != null && Math.abs(r.overfit_gap) > 0.10) ? 'neg' : '';
    tr.appendChild(el('td', { class: gapCls, text: fmtIC(r.overfit_gap) }));
    tbody.appendChild(tr);
  }
}

/* ----- Placeholder card builder ---------------------------------------- */
function makePlaceholder(msg) {
  const wrap = el('div', { class: 'lbt-placeholder' });
  wrap.appendChild(el('p', { class: 'lbt-placeholder-msg', text: msg }));
  wrap.appendChild(makeRetryButton());
  return wrap;
}

/* ======================================================================
   Boot + auto-refresh
   ====================================================================== */
main().catch((e) => {
  console.error('[lthcs-backtest] fatal', e);
  $('bt-loading')?.classList.add('hidden');
  const errBox = $('bt-error');
  if (errBox) {
    errBox.classList.remove('hidden');
    errBox.textContent = `Failed to render backtest: ${e.message || e}`;
  }
});

// Auto-refresh every 5 minutes while the page is open. The placeholder
// cards inside each section have their own "Retry now" buttons for the
// "agents still running" case called out in the spec.
setTimeout(() => { window.location.reload(); }, 5 * 60 * 1000);
