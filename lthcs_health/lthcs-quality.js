// LTHCS quality-audit page renderer.
// Extracted from an inline <script> in quality.html (2026-05-20) so the page
// can run under a strict CSP with `script-src 'self'` (no 'unsafe-inline').
//
// Resolves relative to this HTML file so the page works under /lthcs_health/
// (dev) and /lthcs/lthcs_health/ (production gh-pages).
const SUMMARY_URL = '../data/lthcs/quality_audit/latest_summary.json';

const loadingEl = document.getElementById('lq-loading');
const errorEl = document.getElementById('lq-error');
const contentEl = document.getElementById('lq-content');

function showError(msg) {
  errorEl.textContent = msg;
  errorEl.classList.remove('hidden');
  loadingEl.classList.add('hidden');
}

function badgeClassFor(verdict) {
  if (verdict === 'HEALTHY' || verdict === 'ALIGNED' || verdict === 'BALANCED') return 'green';
  if (verdict === 'DEGRADED' || verdict === 'MISALIGNED' || verdict === 'SKEWED' || verdict === 'UNKNOWN') return 'yellow';
  if (verdict === 'STUB' || verdict === 'CRITICAL') return 'red';
  return 'grey';
}

function freshnessDays(asof) {
  const d = new Date(asof + 'T00:00:00Z');
  const now = new Date();
  return Math.floor((now - d) / (1000 * 60 * 60 * 24));
}

function renderPayload(data) {
  document.getElementById('lq-asof').textContent = data.asof || '?';

  const age = data.asof ? freshnessDays(data.asof) : null;
  if (age !== null && age > 45) {
    const stale = document.getElementById('lq-stale-banner');
    stale.textContent = `Audit is ${age} days old (monthly cadence — expected age 0–35 days). Check the workflow.`;
    stale.classList.remove('hidden');
  }

  // Overall
  const overall = data.overall_verdict || 'UNKNOWN';
  document.getElementById('lq-overall-text').textContent = overall;
  document.getElementById('lq-overall-badge').className = `lq-badge ${badgeClassFor(overall)}`;
  document.getElementById('lq-snapshot-line').textContent =
    `Snapshot: ${data.snapshot_date || '?'} · Generated: ${data.generated_at_utc || '?'}`;

  // Pillars
  const pillarsEl = document.getElementById('lq-pillars');
  pillarsEl.innerHTML = '';
  const order = ['adoption_momentum', 'institutional_confidence', 'financial_evolution', 'thesis_integrity', 'des'];
  for (const p of order) {
    const info = (data.pillars || {})[p] || {};
    const v = info.verdict || '?';
    const li = document.createElement('li');
    li.innerHTML = `
      <span class="lq-badge ${badgeClassFor(v)}"></span>
      <span class="lq-pillar-name">${p}</span>
      <span class="lq-pillar-sub">${v} · cov ${info.coverage_pct ?? '-'}% · μ=${info.mean ?? '-'} σ=${info.stdev ?? '-'}</span>
    `;
    pillarsEl.appendChild(li);
  }

  // Distribution
  const dist = data.distribution || {};
  const distVerd = dist.critical ? 'CRITICAL' : (dist.n ? 'OK' : 'EMPTY');
  document.getElementById('lq-dist-verdict').innerHTML =
    `<span class="lq-badge ${badgeClassFor(distVerd === 'OK' ? 'BALANCED' : 'CRITICAL')}"></span>${distVerd}`;
  document.getElementById('lq-dist-detail').textContent =
    `n=${dist.n ?? '-'}, μ=${dist.mean ?? '-'}, σ=${dist.stdev ?? '-'} · ` +
    `elite=${dist.elite_count ?? '-'}, high-conf=${dist.high_conf_count ?? '-'}, review=${dist.review_count ?? '-'} (${dist.review_pct ?? '-'}%)`;

  // Weights
  const w = data.weights || {};
  document.getElementById('lq-weights-verdict').innerHTML =
    `<span class="lq-badge ${badgeClassFor(w.verdict)}"></span>${w.verdict || '?'}`;
  const wmis = (w.misaligned_cohorts || []).slice(0, 3)
    .map(m => `${m.cohort} (${m.worst_pillar} ${m.worst_gap >= 0 ? '+' : ''}${m.worst_gap})`)
    .join('; ');
  document.getElementById('lq-weights-detail').textContent =
    `${w.n_cohorts ?? 0} cohort(s) with measurable IC${wmis ? ' · ' + wmis : ''}`;

  // Bands
  const b = data.bands || {};
  document.getElementById('lq-bands-verdict').innerHTML =
    `<span class="lq-badge ${badgeClassFor(b.verdict)}"></span>${b.verdict || '?'}`;
  const starved = (b.starved || []).join(', ') || 'none';
  document.getElementById('lq-bands-detail').textContent =
    `review share ${b.review_pct ?? '-'}% · starved: ${starved}`;

  // Alerts
  const alertsEl = document.getElementById('lq-alerts');
  alertsEl.innerHTML = '';
  const alerts = data.alerts || [];
  if (alerts.length === 0) {
    alertsEl.innerHTML = '<p style="color: rgba(255,255,255,0.6)">(none)</p>';
  } else {
    for (const a of alerts) {
      const div = document.createElement('div');
      div.className = 'lq-alert';
      div.textContent = a;
      alertsEl.appendChild(div);
    }
  }

  // Reports
  const reportsEl = document.getElementById('lq-reports');
  const asof = data.asof;
  const reports = [
    ['Combined summary', `${asof}_summary.md`],
    ['Pillar quality', `${asof}_pillar_quality.md`],
    ['Composite distribution', `${asof}_composite_distribution.md`],
    ['Pillar correlation', `${asof}_pillar_correlation.md`],
    ['Weights vs IC', `${asof}_weights_vs_ic.md`],
    ['Band distribution', `${asof}_band_distribution.md`],
  ];
  reportsEl.innerHTML = reports.map(([label, path]) =>
    `<a class="lq-link" href="../data/lthcs/quality_audit/${path}">${label}</a>`
  ).join(' ');

  loadingEl.classList.add('hidden');
  contentEl.classList.remove('hidden');
}

fetch(SUMMARY_URL, { cache: 'no-cache' })
  .then(r => {
    if (!r.ok) throw new Error(`HTTP ${r.status} fetching ${SUMMARY_URL}`);
    return r.json();
  })
  .then(renderPayload)
  .catch(err => showError(`Could not load quality audit summary: ${err.message}`));
