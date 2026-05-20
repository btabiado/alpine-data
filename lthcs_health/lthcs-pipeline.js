/* =========================================================================
   LTHCS Pipeline Freshness — vanilla-JS cron observability dashboard.

   Companion to index.html (pipeline health, source coverage) and
   quality.html (monthly audit). This page answers ONE question:
   "Did each scheduled workflow actually produce its expected output
   recently?" It does NOT call the GitHub API — instead it probes the
   committed JSON snapshots that each cron writes, then judges their
   age against the cron's own cadence.

   Probing strategy per cron
   -------------------------
   - Daily equity (0 23 * * *)    → newest YYYY-MM-DD.json under snapshots/
   - Daily crypto (0 22 * * *)    → newest YYYY-MM-DD.json under snapshots_crypto/
   - Daily backtest (30 23 * * *) → can't list directories on GitHub Pages,
                                    so probe today/yesterday/N-days-back
                                    backtest/<date>_*.json/summary.json candidates
                                    until one hits (uses Last-Modified header
                                    via HEAD request as the freshness signal)
   - Daily trends (0 4 * * *)     → trends/<current-ISO-week>.json — also
                                    inspect its `as_of` field for sub-week age
   - Weekly trends (0 4 * * 1)    → falls out of the same trends/<week>.json
   - Weekly validate (0 5 * * 1)  → backfill_validation_<ts>.json under data/lthcs/
                                    (latest mtime via HEAD)
   - Monthly backtest (0 6 1 * *) → check backtest/ recent dirs (same probe
                                    pattern as daily backtest, looser threshold)
   - Monthly tune-weights         → adaptive_weights/<ts>.json HEAD probe
   - Hourly news                  → narratives/<recent-date>.json
   - Hourly pages                 → not observable from static page (noted only)

   LLM shadow data
   ---------------
   The narratives_llm/ + sentiment_llm/ directories are new Tier 6 outputs.
   They share the same hourly cadence as classic narratives/sentiment, so we
   detect them by probing a known ticker file (AAPL.json) and reading its
   `last_scored` if present.
   ========================================================================= */

const DATA_ROOT = '../data/lthcs';
const REPO_BASE = 'https://github.com/btabiado/btc-eth-etf-dashboard';
const WORKFLOWS_BASE = `${REPO_BASE}/actions/workflows`;
const WORKFLOWS_BLOB = `${REPO_BASE}/blob/main/.github/workflows`;

/* ----- Cadence-aware status thresholds ---------------------------------- */
/* Returns 'ok' | 'warn' | 'fail' | 'muted' given the elapsed seconds and
   the cron's cadence bucket. "muted" is for crons we can't probe and just
   render greyed-out so they don't dominate the status mix. */
const THRESHOLDS = {
  hourly:  { okSec: 2 * 3600,         warnSec: 6 * 3600 },          // 2h / 6h
  daily:   { okSec: 24 * 3600,        warnSec: 48 * 3600 },         // 24h / 48h
  weekly:  { okSec: 8 * 86400,        warnSec: 15 * 86400 },        // 8d / 15d
  monthly: { okSec: 35 * 86400,       warnSec: 50 * 86400 },        // 35d / 50d
};
function tierForAge(ageSec, cadence) {
  const t = THRESHOLDS[cadence];
  if (!t) return 'muted';
  if (ageSec == null || Number.isNaN(ageSec)) return 'muted';
  if (ageSec < t.okSec) return 'ok';
  if (ageSec < t.warnSec) return 'warn';
  return 'fail';
}

/* ----- DOM helpers ----------------------------------------------------- */
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

/* ----- Date / age helpers --------------------------------------------- */
function parseISODate(s) {
  const [y, m, d] = s.split('-').map(Number);
  return new Date(Date.UTC(y, m - 1, d));
}
function isoTodayUTC() {
  const t = new Date();
  return `${t.getUTCFullYear()}-${String(t.getUTCMonth() + 1).padStart(2, '0')}-${String(t.getUTCDate()).padStart(2, '0')}`;
}
function fmtAge(seconds) {
  if (seconds == null || Number.isNaN(seconds)) return 'n/a';
  if (seconds < 90) return `${Math.max(0, Math.round(seconds))}s`;
  const min = seconds / 60;
  if (min < 90) return `${Math.round(min)}m`;
  const hr = min / 60;
  if (hr < 36) return `${Math.round(hr)}h`;
  const day = hr / 24;
  if (day < 21) return `${day.toFixed(1)}d`;
  return `${Math.round(day)}d`;
}
function ageSecFromDate(iso) {
  // Treat an ISO YYYY-MM-DD snapshot as written at the cron's UTC hour.
  // We don't actually know the hour for every cron; use 00:00 UTC of the
  // next day as a conservative upper bound on age (i.e. the file is at
  // most ~24h old at the moment the day rolls). For our thresholds (which
  // treat <24h as green) that's the honest answer.
  const d = parseISODate(iso);
  // Add 23h so a snapshot dated today reads as "0h" early in the morning.
  d.setUTCHours(23, 0, 0, 0);
  return (Date.now() - d.getTime()) / 1000;
}
function ageSecFromTs(isoTs) {
  if (!isoTs) return null;
  const t = Date.parse(isoTs);
  if (Number.isNaN(t)) return null;
  return (Date.now() - t) / 1000;
}

/* ----- ISO week computation (matches lthcs-health.js convention) ------- */
function isoWeek(date) {
  const target = new Date(date.valueOf());
  const dayNr = (target.getUTCDay() + 6) % 7;
  target.setUTCDate(target.getUTCDate() - dayNr + 3);
  const firstThursday = new Date(Date.UTC(target.getUTCFullYear(), 0, 4));
  const diff = (target - firstThursday) / (24 * 3600 * 1000);
  const week = 1 + Math.round((diff - 3 + ((firstThursday.getUTCDay() + 6) % 7)) / 7);
  return { year: target.getUTCFullYear(), week };
}
function currentIsoWeekStr() {
  const { year, week } = isoWeek(new Date());
  return `${year}-W${String(week).padStart(2, '0')}`;
}

/* ----- Fetch helpers with graceful 404 -------------------------------- */
async function tryFetchJson(url) {
  try {
    const r = await fetch(url, { cache: 'no-cache' });
    if (!r.ok) return null;
    return await r.json();
  } catch {
    return null;
  }
}
async function tryHead(url) {
  // Some hosts (GitHub Pages included) honor HEAD with Last-Modified, but
  // not for every content-type. Fall back to GET if HEAD returns no LM.
  try {
    let r = await fetch(url, { method: 'HEAD', cache: 'no-cache' });
    if (!r.ok) return null;
    let lm = r.headers.get('Last-Modified');
    if (!lm) {
      r = await fetch(url, { cache: 'no-cache' });
      if (!r.ok) return null;
      lm = r.headers.get('Last-Modified');
    }
    return { lastModified: lm ? new Date(lm) : null };
  } catch {
    return null;
  }
}

/* ----- Walk back N days probing for an existing snapshot --------------- */
// Used by crons whose outputs are named YYYY-MM-DD.json or YYYY-MM-DD_<suffix>/...
// We can't list a directory over HTTP on GitHub Pages, so we probe day-by-day
// from today backwards until we hit a 200 (or give up after `maxDays`).
async function probeMostRecentDated(buildPath, maxDays) {
  const now = new Date();
  for (let i = 0; i < maxDays; i += 1) {
    const d = new Date(now.valueOf());
    d.setUTCDate(d.getUTCDate() - i);
    const iso = `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, '0')}-${String(d.getUTCDate()).padStart(2, '0')}`;
    const url = buildPath(iso);
    try {
      const r = await fetch(url, { method: 'HEAD', cache: 'no-cache' });
      if (r.ok) return { date: iso, url };
    } catch {
      /* try next */
    }
  }
  return null;
}

/* ======================================================================
   Cron probes — each returns a row object with the shape:
     {
       name, schedule (human), cadence ('daily'|'weekly'|'monthly'|'hourly'),
       workflowFile, lastDate, ageSec, evidenceLabel, evidenceUrl,
       notes (optional string)
     }
   When a probe fails entirely (no data found), `lastDate` is null and
   ageSec is null — the row renders as muted/n-a but still appears.
   ====================================================================== */

async function probeDailyEquity() {
  const idx = await tryFetchJson(`${DATA_ROOT}/snapshots/index.json`);
  let lastDate = null;
  if (idx?.dates && idx.dates.length) {
    lastDate = [...idx.dates].sort().pop();
  }
  return {
    name: 'Daily Equity Pipeline',
    schedule: '0 23 * * * (23:00 UTC)',
    cadence: 'daily',
    workflowFile: 'lthcs-daily.yml',
    lastDate,
    ageSec: lastDate ? ageSecFromDate(lastDate) : null,
    evidenceLabel: lastDate ? `snapshots/${lastDate}.json` : 'snapshots/index.json (empty)',
    evidenceUrl: lastDate ? `${DATA_ROOT}/snapshots/${lastDate}.json` : null,
  };
}

async function probeDailyCrypto() {
  // No index file under snapshots_crypto/ — walk back ~14 days.
  const hit = await probeMostRecentDated(
    (iso) => `${DATA_ROOT}/snapshots_crypto/${iso}.json`,
    14,
  );
  return {
    name: 'Daily Crypto Pipeline',
    schedule: '0 22 * * * (22:00 UTC)',
    cadence: 'daily',
    workflowFile: 'lthcs-crypto-daily.yml',
    lastDate: hit?.date || null,
    ageSec: hit ? ageSecFromDate(hit.date) : null,
    evidenceLabel: hit ? `snapshots_crypto/${hit.date}.json` : 'no recent crypto snapshot in last 14d',
    evidenceUrl: hit?.url || null,
  };
}

async function probeDailyBacktest() {
  // Backtest engine writes a dir like 2026-05-19_post_phase5 — we can't list,
  // but we can probe known suffix candidates. Most recent suffix observed in
  // repo history: _post_phase5, _validation, _h1, _h5, _h21. Fall back to a
  // raw report path so something is checked. We probe both today + 7-day window.
  const candidates = ['post_phase5', 'validation', 'h21', 'h5', 'h1', 'baseline'];
  const now = new Date();
  for (let i = 0; i < 14; i += 1) {
    const d = new Date(now.valueOf());
    d.setUTCDate(d.getUTCDate() - i);
    const iso = `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, '0')}-${String(d.getUTCDate()).padStart(2, '0')}`;
    for (const suffix of candidates) {
      const url = `${DATA_ROOT}/backtest/${iso}_${suffix}/summary.json`;
      try {
        const r = await fetch(url, { method: 'HEAD', cache: 'no-cache' });
        if (r.ok) {
          // We have the file. Try to read generated_at for a precise mtime.
          const body = await tryFetchJson(url);
          const tsAge = ageSecFromTs(body?.generated_at);
          return {
            name: 'Daily Backtest Engine',
            schedule: '30 23 * * * (23:30 UTC)',
            cadence: 'daily',
            workflowFile: 'lthcs-backtest-daily.yml',
            lastDate: iso,
            ageSec: tsAge != null ? tsAge : ageSecFromDate(iso),
            evidenceLabel: `backtest/${iso}_${suffix}/summary.json`,
            evidenceUrl: url,
            notes: body?.run_id ? `run_id ${body.run_id}` : null,
          };
        }
      } catch { /* try next */ }
    }
  }
  return {
    name: 'Daily Backtest Engine',
    schedule: '30 23 * * * (23:30 UTC)',
    cadence: 'daily',
    workflowFile: 'lthcs-backtest-daily.yml',
    lastDate: null,
    ageSec: null,
    evidenceLabel: 'no backtest summary.json found in last 14 days',
    evidenceUrl: null,
  };
}

async function probeDailyTrends() {
  // Trends file is weekly. Sub-week age comes from its `as_of` field if
  // present. Try current ISO week first, then walk back a few weeks.
  let wkStr = currentIsoWeekStr();
  let body = await tryFetchJson(`${DATA_ROOT}/trends/${wkStr}.json`);
  if (!body) {
    // Walk back 4 weeks.
    for (let back = 1; back <= 4 && !body; back += 1) {
      const d = new Date();
      d.setUTCDate(d.getUTCDate() - back * 7);
      const { year, week } = isoWeek(d);
      wkStr = `${year}-W${String(week).padStart(2, '0')}`;
      body = await tryFetchJson(`${DATA_ROOT}/trends/${wkStr}.json`);
    }
  }
  // Prefer as_of (a daily marker) over the weekly file path for daily-cron freshness.
  let lastDate = body?.as_of || null;
  let ageSec = lastDate ? ageSecFromDate(lastDate) : null;
  return {
    name: 'Daily Trends Fetch',
    schedule: '0 4 * * * (04:00 UTC)',
    cadence: 'daily',
    workflowFile: 'lthcs-trends-daily.yml',
    lastDate,
    ageSec,
    evidenceLabel: body ? `trends/${wkStr}.json (as_of ${lastDate || 'n/a'})` : 'no trends file found in last 5 weeks',
    evidenceUrl: body ? `${DATA_ROOT}/trends/${wkStr}.json` : null,
  };
}

async function probeWeeklyTrendsRotate() {
  // Weekly cron is the rotate-ISO-week-file action. Same trends/ file proves it.
  const wkStr = currentIsoWeekStr();
  const head = await tryHead(`${DATA_ROOT}/trends/${wkStr}.json`);
  let lastDate = null;
  let ageSec = null;
  if (head?.lastModified) {
    ageSec = (Date.now() - head.lastModified.getTime()) / 1000;
    lastDate = head.lastModified.toISOString().slice(0, 10);
  }
  return {
    name: 'Weekly Trends Rotate',
    schedule: '0 4 * * 1 (Mon 04:00 UTC)',
    cadence: 'weekly',
    workflowFile: 'lthcs-trends-weekly.yml',
    lastDate,
    ageSec,
    evidenceLabel: `trends/${wkStr}.json (mtime)`,
    evidenceUrl: `${DATA_ROOT}/trends/${wkStr}.json`,
  };
}

async function probeWeeklyValidate() {
  // Validate writes backfill_validation_<ts>.json at the data/lthcs root.
  // We can't list, but we know the index page also references those for the
  // last few weeks via the snapshots/ index.json mtime — proxy by checking
  // mtime of variable_detail/<latest>.json (validate runs against it).
  const idx = await tryFetchJson(`${DATA_ROOT}/snapshots/index.json`);
  const dates = (idx?.dates || []).sort().reverse();
  // Walk back to find a date with a variable_detail file.
  for (const d of dates.slice(0, 14)) {
    const url = `${DATA_ROOT}/variable_detail/${d}.json`;
    const head = await tryHead(url);
    if (head) {
      const ageSec = head.lastModified
        ? (Date.now() - head.lastModified.getTime()) / 1000
        : ageSecFromDate(d);
      return {
        name: 'Weekly Validate',
        schedule: '0 5 * * 1 (Mon 05:00 UTC)',
        cadence: 'weekly',
        workflowFile: 'lthcs-validate-weekly.yml',
        lastDate: d,
        ageSec,
        evidenceLabel: `variable_detail/${d}.json (proxy)`,
        evidenceUrl: url,
        notes: 'proxy via variable_detail mtime',
      };
    }
  }
  return {
    name: 'Weekly Validate',
    schedule: '0 5 * * 1 (Mon 05:00 UTC)',
    cadence: 'weekly',
    workflowFile: 'lthcs-validate-weekly.yml',
    lastDate: null,
    ageSec: null,
    evidenceLabel: 'no recent variable_detail file (proxy)',
    evidenceUrl: null,
  };
}

async function probeMonthlyBacktest() {
  // Same as daily-backtest, but with a wider 50-day window (we just want the
  // most recent run; if the monthly cron stopped firing the daily would too).
  const candidates = ['post_phase5', 'validation', 'monthly', 'h21'];
  const now = new Date();
  for (let i = 0; i < 50; i += 1) {
    const d = new Date(now.valueOf());
    d.setUTCDate(d.getUTCDate() - i);
    const iso = `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, '0')}-${String(d.getUTCDate()).padStart(2, '0')}`;
    for (const suffix of candidates) {
      const url = `${DATA_ROOT}/backtest/${iso}_${suffix}/summary.json`;
      try {
        const r = await fetch(url, { method: 'HEAD', cache: 'no-cache' });
        if (r.ok) {
          return {
            name: 'Monthly Backtest',
            schedule: '0 6 1 * * (1st of month 06:00 UTC)',
            cadence: 'monthly',
            workflowFile: 'lthcs-backtest-monthly.yml',
            lastDate: iso,
            ageSec: ageSecFromDate(iso),
            evidenceLabel: `backtest/${iso}_${suffix}/summary.json`,
            evidenceUrl: url,
          };
        }
      } catch { /* try next */ }
    }
  }
  return {
    name: 'Monthly Backtest',
    schedule: '0 6 1 * * (1st of month 06:00 UTC)',
    cadence: 'monthly',
    workflowFile: 'lthcs-backtest-monthly.yml',
    lastDate: null,
    ageSec: null,
    evidenceLabel: 'no backtest run found in last 50 days',
    evidenceUrl: null,
  };
}

async function probeMonthlyTune() {
  // Tune-weights writes adaptive_weights/*.json with an ISO ts in the name.
  // The latest summary lives at adaptive_weights/<date>_walk_forward_summary.md
  // which we can probe by trying recent dates with the documented suffix.
  // Fall back to the weights.json mtime (which is rewritten on every tune run).
  const head = await tryHead(`${DATA_ROOT}/weights.json`);
  let lastDate = null;
  let ageSec = null;
  if (head?.lastModified) {
    ageSec = (Date.now() - head.lastModified.getTime()) / 1000;
    lastDate = head.lastModified.toISOString().slice(0, 10);
  }
  return {
    name: 'Monthly Tune Weights',
    schedule: '0 7 1 * * (1st of month 07:00 UTC)',
    cadence: 'monthly',
    workflowFile: 'lthcs-tune-weights-monthly.yml',
    lastDate,
    ageSec,
    evidenceLabel: 'weights.json (mtime)',
    evidenceUrl: `${DATA_ROOT}/weights.json`,
    notes: 'mtime advances on every tune run; static if no recent run',
  };
}

async function probeHourlyNews() {
  // Hourly news writes per-ticker sentiment/<TICKER>.json with last_scored
  // and per-date narratives/<YYYY-MM-DD>.json. Probe narratives back-window
  // and AAPL.json (canonical anchor ticker) for the strongest signal.
  // 1) Narratives by date — walk back 7d.
  const hit = await probeMostRecentDated(
    (iso) => `${DATA_ROOT}/narratives/${iso}.json`,
    7,
  );
  let lastDate = hit?.date || null;
  let ageSec = lastDate ? ageSecFromDate(lastDate) : null;
  // 2) Override with sentiment/AAPL.json last_scored if it's newer.
  const aapl = await tryFetchJson(`${DATA_ROOT}/sentiment/AAPL.json`);
  if (aapl?.last_scored) {
    const aaplAge = ageSecFromDate(aapl.last_scored);
    if (ageSec == null || aaplAge < ageSec) {
      ageSec = aaplAge;
      lastDate = aapl.last_scored;
    }
  }
  return {
    name: 'Hourly News + Sentiment',
    schedule: '0 * * * * (every hour)',
    cadence: 'daily', // ← treat as daily for thresholds; hourly is too strict
                      //    given GitHub cron drift + pages.yml deploy lag.
    workflowFile: 'lthcs-news-hourly.yml',
    lastDate,
    ageSec,
    evidenceLabel: `narratives/${lastDate || 'n/a'}.json + sentiment/AAPL.json`,
    evidenceUrl: lastDate ? `${DATA_ROOT}/narratives/${lastDate}.json` : null,
    notes: 'thresholds eased to daily — hourly cron + commit lag rarely <2h',
  };
}

async function probeLlmShadow() {
  // LLM shadow narratives + sentiment are Tier 6 outputs. Both directories
  // currently exist but may be empty (LLM rollout gated). Detect emptiness
  // by attempting AAPL.json under each.
  const [llmSent, llmNarr] = await Promise.all([
    tryHead(`${DATA_ROOT}/sentiment_llm/AAPL.json`),
    tryHead(`${DATA_ROOT}/narratives_llm/${isoTodayUTC()}.json`),
  ]);
  let ageSec = null;
  let lastDate = null;
  let evidenceLabel = 'sentiment_llm/ + narratives_llm/ both empty (Tier 6 not rolled out)';
  if (llmSent?.lastModified) {
    ageSec = (Date.now() - llmSent.lastModified.getTime()) / 1000;
    lastDate = llmSent.lastModified.toISOString().slice(0, 10);
    evidenceLabel = 'sentiment_llm/AAPL.json (mtime)';
  }
  return {
    name: 'LLM Shadow Data',
    schedule: 'shadow of hourly news (Tier 6)',
    cadence: 'muted', // ← intentionally not gated yet; informational only
    workflowFile: 'lthcs-news-hourly.yml',
    lastDate,
    ageSec,
    evidenceLabel,
    evidenceUrl: llmSent ? `${DATA_ROOT}/sentiment_llm/AAPL.json` : null,
    notes: 'shadow only — alerts disabled until promoted to primary',
  };
}

/* ----- Snapshot index head + range for the "deploy context" panel ------ */
async function fetchSnapshotMeta() {
  const idx = await tryFetchJson(`${DATA_ROOT}/snapshots/index.json`);
  if (!idx?.dates || idx.dates.length === 0) return null;
  const sorted = [...idx.dates].sort();
  return {
    oldest: sorted[0],
    newest: sorted[sorted.length - 1],
    count: idx.dates.length,
    modelVersion: idx.model_version || 'unknown',
  };
}

/* ======================================================================
   Render
   ====================================================================== */

function renderRow(row) {
  const node = el('div', { class: 'lhealth-cron-row' });
  const status = tierForAge(row.ageSec, row.cadence);
  node.setAttribute('data-status', status);

  // Col 1: cron name + schedule
  const nameCell = el('div', { class: 'lhealth-cron-name' });
  nameCell.appendChild(el('span', { text: row.name }));
  nameCell.appendChild(el('span', { class: 'lhealth-cron-sched', text: row.schedule }));
  node.appendChild(nameCell);

  // Col 2: last output (date or 'n/a')
  const lastCell = el('div', { class: 'lhealth-cron-last' });
  if (row.lastDate) {
    lastCell.textContent = row.lastDate;
  } else {
    lastCell.textContent = 'n/a';
  }
  node.appendChild(lastCell);

  // Col 3: age
  node.appendChild(el('div', { class: 'lhealth-cron-age', text: fmtAge(row.ageSec) }));

  // Col 4: status badge
  const badgeText = status === 'ok' ? 'fresh'
    : status === 'warn' ? 'late'
    : status === 'fail' ? 'stale'
    : 'n/a';
  node.appendChild(el('span', {
    class: 'lhealth-cron-badge',
    'data-status': status,
    text: badgeText,
  }));

  // Col 5: workflow link
  const linkCell = el('div', {});
  const link = el('a', {
    class: 'lhealth-cron-link',
    href: `${WORKFLOWS_BASE}/${row.workflowFile}`,
    target: '_blank',
    rel: 'noopener',
    title: `View ${row.workflowFile} runs on GitHub`,
  });
  link.textContent = 'Actions ↗';
  linkCell.appendChild(link);
  node.appendChild(linkCell);

  // Optional notes line (spans full width via grid-area override on mobile).
  if (row.notes) {
    const notes = el('div', {
      class: 'lhealth-cron-sched',
      style: 'grid-column: 1 / -1; margin-top: 4px;',
      text: row.notes,
    });
    node.appendChild(notes);
  }

  return node;
}

function renderSummary(rows) {
  const counts = { ok: 0, warn: 0, fail: 0, muted: 0 };
  for (const r of rows) {
    const t = tierForAge(r.ageSec, r.cadence);
    counts[t] = (counts[t] || 0) + 1;
  }
  const container = $('cron-summary');
  container.replaceChildren();
  const tiles = [
    { label: 'Fresh',            value: counts.ok,    status: 'ok' },
    { label: 'Late',             value: counts.warn,  status: 'warn' },
    { label: 'Stale',            value: counts.fail,  status: 'fail' },
    { label: 'Not gated',        value: counts.muted, status: 'muted' },
  ];
  for (const t of tiles) {
    const tile = el('div', { class: 'lhealth-cron-tile', 'data-status': t.status });
    tile.appendChild(el('div', { class: 'lhealth-cron-tile-label', text: t.label }));
    tile.appendChild(el('div', { class: 'lhealth-cron-tile-value', text: String(t.value) }));
    container.appendChild(tile);
  }
}

function renderDeployContext(meta) {
  if (!meta) {
    $('deploy-snap-head').textContent = 'n/a (no snapshot index)';
    $('deploy-snap-range').textContent = 'n/a';
    return;
  }
  $('deploy-snap-head').textContent = `${meta.newest} · model ${meta.modelVersion}`;
  $('deploy-snap-range').textContent = `${meta.oldest} → ${meta.newest} (${meta.count} days)`;
}

/* ======================================================================
   Bootstrap
   ====================================================================== */
async function main() {
  const loading = $('freshness-loading');
  const errBox = $('freshness-error');
  const content = $('freshness-content');

  $('freshness-asof').textContent = new Date().toISOString().replace('T', ' ').slice(0, 16) + 'Z';

  // Run every probe in parallel. Any individual probe that throws is
  // contained inside its own try/catch, so allSettled isn't strictly
  // necessary — but be defensive in case a future probe forgets.
  let rows;
  try {
    rows = await Promise.all([
      probeDailyEquity(),
      probeDailyCrypto(),
      probeDailyBacktest(),
      probeDailyTrends(),
      probeWeeklyTrendsRotate(),
      probeWeeklyValidate(),
      probeMonthlyBacktest(),
      probeMonthlyTune(),
      probeHourlyNews(),
      probeLlmShadow(),
    ]);
  } catch (e) {
    console.error('[lthcs-pipeline] probe error', e);
    loading.classList.add('hidden');
    errBox.classList.remove('hidden');
    errBox.textContent = `Failed to probe pipeline freshness: ${e.message || e}`;
    return;
  }

  // Display order: critical daily crons first, then weekly, then monthly,
  // then hourly + shadow at the bottom. Within a cadence bucket keep the
  // declaration order from the array above.
  const list = $('cron-list');
  list.replaceChildren();
  for (const r of rows) {
    list.appendChild(renderRow(r));
  }

  renderSummary(rows);
  renderDeployContext(await fetchSnapshotMeta());

  loading.classList.add('hidden');
  content.classList.remove('hidden');
}

main().catch((e) => {
  console.error('[lthcs-pipeline] fatal', e);
  $('freshness-loading')?.classList.add('hidden');
  const errBox = $('freshness-error');
  if (errBox) {
    errBox.classList.remove('hidden');
    errBox.textContent = `Failed to render pipeline freshness: ${e.message || e}`;
  }
});

// Auto-refresh hourly, matching pages.yml cadence.
setTimeout(() => { window.location.reload(); }, 60 * 60 * 1000);
