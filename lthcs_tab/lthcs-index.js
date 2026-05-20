// lthcs-index.js
// Renders the LTHCS Composite Index "guided narrative" landing card at
// the top of the /lthcs/ page. Reads ../data/lthcs/index/<date>.json
// (written by lthcs_daily.py Stage 8) and paints into
// #lthcs-composite-index.
//
// 2026-05-20: promoted from mockups/revamp-B-narrative — replaces the
// prior compact verdict + components table layout. The narrative pattern
// guides first-time visitors through 4 numbered steps: big picture,
// what changed, why it matters, how to read this. Styles live in
// lthcs-narrative.css (sibling file, linked from index.html).

'use strict';

const INDEX_BASE = '../data/lthcs/index';

const BAND_BRIGHT = {
  elite: 'var(--band-elite-bright)',
  high_confidence: 'var(--band-high-bright)',
  constructive: 'var(--band-constructive-bright)',
  monitor: 'var(--band-monitor-bright)',
  weakening: 'var(--band-weakening-bright)',
  review: 'var(--band-review-bright)',
};

// Per-component plain-English gloss + jargon term/definition for the
// inline <details> popover. Copy lifted from lthcs_help/index.html.
const COMP_META = {
  'Band lean (bullish % minus bearish %)': {
    gloss: 'Of every 168 names we track, what share is in the top 3 bands vs. the bottom 2.',
    term: 'band lean',
    def: '% of the universe in the top three bands (Elite + High + Constructive) minus % in the bottom two (Weakening + Review). Positive = more strong names than weak ones.'
  },
  'Adoption pillar avg': {
    gloss: 'Average of the "who actually uses or holds this" score across all names.',
    term: 'Adoption pillar',
    def: 'Product traction, user / holder growth, network footprint. Built from retail-app downloads, employment growth, Wikipedia pageview trend.'
  },
  'Institutional pillar avg': {
    gloss: 'Average of the "what sophisticated owners are doing" score.',
    term: 'Institutional pillar',
    def: 'Form 4 insider net buys, 13F qtr-over-qtr deltas, ETF AUM trend, put/call posture. Captures whether smart money is adding or trimming.'
  },
  'Financial pillar avg': {
    gloss: 'Average of the "can this business fund itself" score.',
    term: 'Financial pillar',
    def: 'TTM free cash flow yield, net cash, dividend coverage, buyback authorization remaining.'
  },
  'Thesis pillar avg': {
    gloss: 'Average of the "what the market thinks of the story" score. Often neutral in V1.',
    term: 'Thesis pillar',
    def: 'EPS revision breadth, price-target deltas, multi-timeframe trend posture. Falls back to neutral 50 when free-tier sentiment data is missing.'
  },
  'DES (demand environment) avg': {
    gloss: 'Average of Demand-vs-Earnings Strength: is the run-up earned, or all multiple expansion?',
    term: 'DES',
    def: 'Demand-vs-Earnings Strength. Compares trailing return against trailing EPS growth, sector-relative. Negative = price ran ahead of earnings.'
  },
  'Macro regime (HY OAS / curve / USD)': {
    gloss: 'Risk-on / risk-off composite from credit spreads, the yield curve, and the dollar.',
    term: 'Macro regime',
    def: 'HY OAS (junk-bond spreads), 2s10s (Treasury curve), and trade-weighted USD. Positive = risk-on backdrop that lifts long-duration assets.'
  },
  'Insider conviction breadth': {
    gloss: 'Across the universe, are insiders net buying or net selling?',
    term: 'Form 4',
    def: 'SEC filing insiders submit when they buy or sell their own company\'s stock. This signal counts net-buy minus net-sell breadth across all names.'
  },
  '13F conviction breadth (acc vs dist)': {
    gloss: 'Across the universe, are institutions accumulating or distributing? Lags one quarter.',
    term: '13F',
    def: 'Quarterly SEC filing from funds >$100M reporting their long equity holdings. Filed ~45 days after quarter-end, so this signal lags actual positioning.'
  }
};

// Verdict-band → human gloss. Keys are the backend labels emitted by
// lthcs/index_aggregate.py:_label_for (with the legacy "LTHCS " prefix
// stripped client-side).
const GLOSS_BY_LABEL = {
  'ELITE': 'Broad-based strength — pillar averages, band lean, and macro all leaning the same way. The universe looks healthy for long-term holders.',
  'CONSTRUCTIVE': 'More green than red, but mixed. Some signals are firming while others are catching up. Constructive backdrop, not all-clear.',
  'NEUTRAL': 'The universe is leaning slightly cautious today — more names softening than firming, but no clean directional bias yet. Worth watching the components below.',
  'WEAKENING': 'More red than green. Pillars or macro are weakening across the universe. Not a panic signal, but the burden of proof is on the bulls.',
  'DISTRIBUTING': 'Broad weakness across pillars and macro. Time to re-underwrite holdings rather than add risk.'
};

// Map the verdict label → which 5-cell legend cell to highlight.
const LEGEND_CELL_BY_LABEL = {
  'ELITE': 'elite',
  'CONSTRUCTIVE': 'constructive',
  'NEUTRAL': 'neutral',
  'WEAKENING': 'weakening',
  'DISTRIBUTING': 'distributing'
};

function escapeHtml(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function stripParen(s) {
  return String(s || '').replace(/\s*\(.+?\)\s*$/, '').trim();
}

function cleanLabel(label) {
  if (!label) return '';
  return String(label).replace(/^LTHCS\s+/i, '').toUpperCase();
}

function indexUrlFor(calcDate) {
  return `${INDEX_BASE}/${calcDate}.json`;
}

async function fetchIndexFile(calcDate) {
  if (!calcDate) return null;
  try {
    const res = await fetch(indexUrlFor(calcDate), { cache: 'no-store' });
    if (!res.ok) return null;
    return await res.json();
  } catch (err) {
    console.warn('LTHCS: index load failed', err);
    return null;
  }
}

function deltaClass(d) {
  if (d > 0) return 'pos';
  if (d < 0) return 'neg';
  return 'zero';
}

function capForComponent(name) {
  return /^Band lean/i.test(name) ? 30 : 10;
}

function gaugePctFor(score) {
  if (typeof score !== 'number' || !Number.isFinite(score)) return 50;
  const pct = ((score + 100) / 200) * 100;
  return Math.max(0, Math.min(100, pct));
}

// ---------------------------------------------------------------------
// Builders — each returns innerHTML for a section. Composed and injected
// into the host in renderNarrative().
// ---------------------------------------------------------------------

function buildWelcomeHtml() {
  return `
    <section class="lthcs-nar-welcome" id="lthcs-nar-welcome" aria-label="Welcome — first-time visitors">
      <div class="lthcs-nar-welcome-text">
        <div class="lthcs-nar-welcome-eyebrow">First time here?</div>
        <h2>This is a daily read on whether ~168 large US stocks look strong for long-term holders.</h2>
        <p>One number. Four numbered sections. Takes 30 seconds.</p>
      </div>
      <a href="#lthcs-nar-step-1" class="lthcs-nar-welcome-cta">Start the 30-second tour &rarr;</a>
      <button class="lthcs-nar-welcome-dismiss" type="button" data-lthcs-nar-dismiss="welcome">
        I&rsquo;ve seen this
      </button>
    </section>`;
}

function buildStep1Html(payload) {
  const score = Number(payload.score);
  const scoreStr = (score >= 0 ? '+' : '') + score;
  const label = cleanLabel(payload.label) || 'NEUTRAL';
  const gloss = GLOSS_BY_LABEL[label] || 'A daily directional read on long-term-hold sentiment across the universe.';
  const pct = gaugePctFor(score).toFixed(1);
  const here = LEGEND_CELL_BY_LABEL[label] || 'neutral';

  const legend = [
    { key: 'distributing', name: 'Distributing', range: '≤ −60' },
    { key: 'weakening',    name: 'Weakening',    range: '−60 to −30' },
    { key: 'neutral',      name: 'Neutral',      range: '−30 to +30' },
    { key: 'constructive', name: 'Constructive', range: '+30 to +60' },
    { key: 'elite',        name: 'Elite',        range: '≥ +60' }
  ].map((cell) => {
    const cls = cell.key === here ? 'lthcs-nar-band-legend-cell is-here' : 'lthcs-nar-band-legend-cell';
    return `
      <div class="${cls}" data-band="${cell.key}">
        <span class="lthcs-nar-band-legend-name">${cell.name}</span>
        <span class="lthcs-nar-band-legend-range">${cell.range}</span>
      </div>`;
  }).join('');

  return `
    <section class="lthcs-nar-step" id="lthcs-nar-step-1" aria-labelledby="lthcs-nar-step-1-title">
      <div class="lthcs-nar-step-head">
        <div class="lthcs-nar-step-num" aria-hidden="true">1</div>
        <div>
          <h2 class="lthcs-nar-step-title" id="lthcs-nar-step-1-title">The big picture</h2>
          <p class="lthcs-nar-step-question">Where is the market leaning right now?</p>
        </div>
      </div>
      <div class="lthcs-nar-step-body">
        <div class="lthcs-nar-verdict">
          <div class="lthcs-nar-verdict-score">${escapeHtml(scoreStr)}</div>
          <div class="lthcs-nar-verdict-text">
            <div class="lthcs-nar-verdict-label">${escapeHtml(label)}</div>
            <p class="lthcs-nar-verdict-gloss">${gloss}</p>
            <div class="lthcs-nar-verdict-sub">
              One number, scale &minus;100 to +100. Computed daily at 23:00 UTC from 9 underlying signals across the LTHCS universe.
            </div>
          </div>
        </div>

        <div class="lthcs-nar-gauge" role="img" aria-label="Composite gauge at ${escapeHtml(scoreStr)}">
          <div class="lthcs-nar-gauge-track"></div>
          <div class="lthcs-nar-gauge-marker" style="left: ${pct}%"></div>
        </div>
        <div class="lthcs-nar-gauge-ticks">
          <span>&minus;100</span><span>&minus;50</span><span>0</span><span>+50</span><span>+100</span>
        </div>

        <div class="lthcs-nar-band-legend" aria-label="Verdict bands">${legend}</div>

        <p class="lthcs-nar-step-next">
          Got the headline? Next: <a href="#lthcs-nar-step-2">2. What changed inside the number &rarr;</a>
        </p>
      </div>
    </section>`;
}

function buildComponentHtml(c) {
  const meta = COMP_META[c.name] || { gloss: '', term: null, def: '' };
  const dCls = deltaClass(c.delta);
  const dStr = (c.delta > 0 ? '+' : '') + c.delta;
  const cap = capForComponent(c.name);
  const ratio = Math.min(Math.abs(c.delta) / cap, 1);
  const fillPct = (ratio * 50).toFixed(1);
  const fillSide = c.delta >= 0 ? 'pos' : 'neg';
  const fillStyle = c.delta >= 0
    ? `left:50%;width:${fillPct}%`
    : `right:50%;width:${fillPct}%`;

  // Wrap the jargon term with an inline <details> popover, if there is one.
  let nameHtml = escapeHtml(c.name);
  if (meta.term) {
    const escaped = meta.term.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const re = new RegExp('(' + escaped + ')', 'i');
    nameHtml = escapeHtml(c.name).replace(re,
      `<details class="lthcs-nar-term"><summary>$1</summary>` +
      `<div class="lthcs-nar-popover" role="note"><strong>${escapeHtml(meta.term)}</strong>${meta.def}</div>` +
      `</details>`);
  }

  const valueStr = (c.value == null || c.value === '') ? '' : String(c.value);
  return `
    <div class="lthcs-nar-comp">
      <div class="lthcs-nar-comp-head">
        <div class="lthcs-nar-comp-name">${nameHtml}${valueStr ? `<span class="lthcs-nar-comp-value-inline">${escapeHtml(valueStr)}</span>` : ''}</div>
        <div class="lthcs-nar-comp-delta ${dCls}">${escapeHtml(dStr)}</div>
      </div>
      <p class="lthcs-nar-comp-read">${escapeHtml(c.read || '')}${meta.gloss ? `<span class="lthcs-nar-sep">·</span><span class="lthcs-nar-gloss">${meta.gloss}</span>` : ''}</p>
      <div class="lthcs-nar-comp-strength">
        <div class="lthcs-nar-comp-strength-spine"></div>
        <div class="lthcs-nar-comp-strength-fill ${fillSide}" style="${fillStyle}"></div>
      </div>
    </div>`;
}

function buildStep2Html(payload) {
  const comps = payload.components || [];
  const compsHtml = comps.map(buildComponentHtml).join('');
  return `
    <section class="lthcs-nar-step" id="lthcs-nar-step-2" aria-labelledby="lthcs-nar-step-2-title">
      <div class="lthcs-nar-step-head">
        <div class="lthcs-nar-step-num" aria-hidden="true">2</div>
        <div>
          <h2 class="lthcs-nar-step-title" id="lthcs-nar-step-2-title">What changed inside the number</h2>
          <p class="lthcs-nar-step-question">Which signals pushed the headline up or down today?</p>
        </div>
      </div>
      <div class="lthcs-nar-step-body">
        <p style="margin-top:0;color:var(--text-secondary);font-size:13px">
          The headline is an average of 9 underlying signals, each scaled to &minus;30 to +30 for band lean (the heaviest weight) or &plusmn;10 for the others. Green = pushed today&rsquo;s number up, red = pushed it down. Hover any underlined term for a plain-English definition.
        </p>
        <div class="lthcs-nar-components">${compsHtml}</div>
        <p class="lthcs-nar-step-next">
          See the parts? Next: <a href="#lthcs-nar-step-3">3. Why today&rsquo;s read matters &rarr;</a>
        </p>
      </div>
    </section>`;
}

// Step 3 + Step 4 render as collapsed <details> accordions to keep the
// landing page short — newcomers can click into them when they want the
// interpretation/cheat-sheet; returning users skip past them at zero cost.
function buildStep3Html(payload) {
  const comps = (payload.components || []).slice();
  const negs = comps.filter((c) => c.delta < 0).sort((a, b) => a.delta - b.delta);
  const poss = comps.filter((c) => c.delta > 0).sort((a, b) => b.delta - a.delta);

  const label = cleanLabel(payload.label) || 'NEUTRAL';
  const scoreStr = (payload.score >= 0 ? '+' : '') + payload.score;
  const lead = `Today the headline reads <strong>${escapeHtml(scoreStr)}</strong> &mdash; <em>${escapeHtml(label)}</em>. Here&rsquo;s the story behind it.`;

  let body = '';
  if (negs.length && poss.length) {
    const worst = negs[0];
    const best = poss[0];
    body =
      `The biggest drag is <strong>${escapeHtml(stripParen(worst.name))}</strong> at ` +
      `<code>${(worst.delta > 0 ? '+' : '') + worst.delta}</code> &mdash; ${escapeHtml(worst.read || '')}. ` +
      `Pulling the other direction: <strong>${escapeHtml(stripParen(best.name))}</strong> at ` +
      `<code>+${best.delta}</code> (${escapeHtml(best.read || '')}).`;
  } else if (negs.length) {
    const worst = negs[0];
    body =
      `The biggest drag is <strong>${escapeHtml(stripParen(worst.name))}</strong> at ` +
      `<code>${worst.delta}</code> &mdash; ${escapeHtml(worst.read || '')}. ` +
      `Nothing is pushing meaningfully the other way today.`;
  } else if (poss.length) {
    const best = poss[0];
    body =
      `The standout is <strong>${escapeHtml(stripParen(best.name))}</strong> at ` +
      `<code>+${best.delta}</code> (${escapeHtml(best.read || '')}). Nothing material is dragging.`;
  } else {
    body = 'All nine components are flat today &mdash; a genuinely quiet day under the headline.';
  }

  // Macro-vs-breadth divergence call-out, when both moved meaningfully in
  // opposite directions. This is the most useful read for the narrative.
  const macro = comps.find((c) => /^Macro regime/i.test(c.name));
  const bandLean = comps.find((c) => /^Band lean/i.test(c.name));
  let divergence = '';
  if (macro && bandLean &&
      Math.sign(macro.delta) !== Math.sign(bandLean.delta) &&
      Math.abs(macro.delta) >= 3 && Math.abs(bandLean.delta) >= 3) {
    const macroDir = macro.delta > 0 ? 'risk-on' : 'risk-off';
    const breadthDir = bandLean.delta > 0 ? 'accumulating' : 'distributing';
    divergence =
      `<div class="lthcs-nar-why-divergence"><strong>Watch the divergence:</strong> ` +
      `macro is <em>${macroDir}</em> (<code>${(macro.delta > 0 ? '+' : '') + macro.delta}</code>), ` +
      `but the breadth of the universe is <em>${breadthDir}</em> (<code>${(bandLean.delta > 0 ? '+' : '') + bandLean.delta}</code>). ` +
      `When those disagree, the signal is idiosyncratic, not market-wide &mdash; drill into individual tickers.</div>`;
  }

  return `
    <details class="lthcs-nar-step lthcs-nar-collapsed" id="lthcs-nar-step-3">
      <summary class="lthcs-nar-step-head lthcs-nar-step-summary">
        <div class="lthcs-nar-step-num" aria-hidden="true">3</div>
        <div>
          <h2 class="lthcs-nar-step-title">Why today&rsquo;s read matters</h2>
          <p class="lthcs-nar-step-question">What story do these numbers tell, in plain English?</p>
        </div>
        <span class="lthcs-nar-step-chevron" aria-hidden="true">&rsaquo;</span>
      </summary>
      <div class="lthcs-nar-step-body">
        <div class="lthcs-nar-why">
          <p>${lead}</p>
          <p>${body}</p>
          ${divergence}
        </div>
        <p class="lthcs-nar-why-disclaimer">
          Heads-up: this paragraph is generated from today&rsquo;s component deltas, not from human commentary. It points you to the parts to dig into &mdash; it&rsquo;s not a recommendation.
        </p>
      </div>
    </details>`;
}

function buildStep4Html() {
  return `
    <details class="lthcs-nar-step lthcs-nar-how lthcs-nar-collapsed" id="lthcs-nar-step-4">
      <summary class="lthcs-nar-step-head lthcs-nar-step-summary">
        <div class="lthcs-nar-step-num" aria-hidden="true">4</div>
        <div>
          <h2 class="lthcs-nar-step-title">How to read this dashboard</h2>
          <p class="lthcs-nar-step-question">A cheat sheet you can come back to.</p>
        </div>
        <span class="lthcs-nar-step-chevron" aria-hidden="true">&rsaquo;</span>
      </summary>
      <div class="lthcs-nar-step-body">
        <dl>
          <dt>Big swings matter</dt>
          <dd>A jump like <code>+30 &rarr; &minus;5</code> overnight is a real regime shift &mdash; click any signal above to see what drove it.</dd>

          <dt>Bands &gt; absolute number</dt>
          <dd>"Moved from Constructive to Neutral" is a real signal. "Still in Neutral" is mostly noise &mdash; bands hop at the edges.</dd>

          <dt>Disagreement is interesting</dt>
          <dd>When the macro signal goes one way and pillar averages go the other, that&rsquo;s where the active read lives. Look for green/red mixed in step 2.</dd>

          <dt>13F lags by a quarter</dt>
          <dd>Big institutional positions are reported with a ~45-day delay. Insider buys are fresher (within days) but can be noisy around earnings.</dd>

          <dt>&ldquo;as of&rdquo; = snapshot time</dt>
          <dd>The page doesn&rsquo;t auto-update. The number is fixed until the next 23:00 UTC run. If the date looks old, check the Health page.</dd>
        </dl>
        <div class="lthcs-nar-how-not">
          <strong>What this is NOT.</strong> Not a trading signal. Not investment advice. Not a forecast of next week&rsquo;s prices. It&rsquo;s a daily directional read on committed public data &mdash; insiders, 13F filers, fundamentals, macro. Do your own work before risking real money.
        </div>
      </div>
    </details>`;
}

function wireUpInteractions(host) {
  // Welcome-banner "I've seen this" dismiss. Persisted in localStorage
  // so the banner stays gone across page loads.
  const KEY = 'lthcs-nar-welcome-dismissed-v1';
  if (localStorage.getItem(KEY) === '1') {
    const welcome = host.querySelector('#lthcs-nar-welcome');
    if (welcome) welcome.style.display = 'none';
  }
  host.querySelectorAll('[data-lthcs-nar-dismiss="welcome"]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const welcome = host.querySelector('#lthcs-nar-welcome');
      if (welcome) welcome.style.display = 'none';
      try { localStorage.setItem(KEY, '1'); } catch (_) { /* ignore */ }
    });
  });

  // Close any open <details> popover when the user clicks outside it.
  // Scoped to host so it doesn't fight other components on the page.
  document.addEventListener('click', (e) => {
    host.querySelectorAll('details.lthcs-nar-term[open]').forEach((d) => {
      if (!d.contains(e.target)) d.removeAttribute('open');
    });
  });
}

function renderNarrative(host, payload) {
  // Scope --lthcs-nar-tone to the host so we don't bleed onto siblings.
  const tone = BAND_BRIGHT[payload.band_key] || BAND_BRIGHT.monitor;
  host.style.setProperty('--lthcs-nar-tone', tone);
  host.dataset.band = payload.band_key || 'monitor';

  host.innerHTML = `<div class="lthcs-nar-wrap">` +
    buildWelcomeHtml() +
    buildStep1Html(payload) +
    buildStep2Html(payload) +
    buildStep3Html(payload) +
    buildStep4Html() +
    `</div>`;

  wireUpInteractions(host);
}

export async function renderLthcsIndex(calcDate) {
  const host = document.getElementById('lthcs-composite-index');
  if (!host) return;
  const payload = await fetchIndexFile(calcDate);
  if (!payload) {
    host.innerHTML = '';
    host.classList.add('hidden');
    return;
  }
  host.classList.remove('hidden');
  renderNarrative(host, payload);
}

// Auto-wire: discover today's calc_date and render. Independent of
// lthcs-tab.js orchestration so we don't have to touch its boot flow.
async function discoverCalcDate() {
  try {
    const res = await fetch('../data/lthcs/snapshots/index.json', { cache: 'no-store' });
    if (res.ok) {
      const idx = await res.json();
      const latest = idx && (idx.latest || idx.latest_calc_date);
      if (latest) return latest;
    }
  } catch (_) { /* fall through */ }
  return new Date().toISOString().slice(0, 10);
}

document.addEventListener('DOMContentLoaded', () => {
  discoverCalcDate()
    .then(renderLthcsIndex)
    .catch((err) => console.warn('LTHCS: composite-index auto-render failed', err));
});
