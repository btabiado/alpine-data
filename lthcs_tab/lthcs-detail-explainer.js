// lthcs-detail-explainer.js
// Phase 5 GAMMA — Score explainer tooltip for the LTHCS detail modal.
//
// Attached to each pillar sub-score chip in the Pillar Breakdown card. On
// hover / focus (desktop) or tap (mobile) the score becomes a popover that
// surfaces:
//   - pillar name + sub-score
//   - 3-5 raw signals from data/lthcs/variable_detail/<date>.json
//   - each signal's raw value + contribution to sub-score
//   - a "View full evidence" link that opens the matching Evidence accordion
//
// Reuses the variable_detail cache the detail modal already populates
// (moduleState.vardetailCache). No extra network calls.
//
// Edge cases:
//   - No variable_detail row for ticker -> plain-English formula fallback
//   - Pillar dropped (snapshotRow.dropped_pillars includes pillar) -> show
//     drop reason + responsible flag from data_quality_flags
//   - Sub-score at floor/ceiling (<= 1 or >= 99) -> highlight that
//
// Public API:
//   bindPillarExplainer(panel, { snapshotRow, ticker, calcDate, getVardetail })
//   refreshPillarExplainer(panel, { ... }) — call after vardetail loads
//
// 'use strict';

'use strict';

// ----------------------------------------------------------------------------
// Constants (kept local — explainer is self-contained and doesn't reach into
// detail.js internals beyond the panel DOM and a getVardetail() callback)
// ----------------------------------------------------------------------------

const PILLAR_DISPLAY = {
  adoption_momentum: 'Adoption Momentum',
  institutional_confidence: 'Institutional Confidence',
  financial_evolution: 'Financial Evolution',
  thesis_integrity: 'Thesis Integrity',
  des: 'Demand Environment',
};

const PILLAR_ORDER = [
  'adoption_momentum',
  'institutional_confidence',
  'financial_evolution',
  'thesis_integrity',
  'des',
];

// Plain-English formula text shown when variable_detail isn't available for
// the current ticker (e.g. crypto rows, or a vardetail file that failed to
// load). Mirrors the contract of lthcs/score.py at a high level so users
// understand what the number represents.
const PILLAR_FORMULA = {
  adoption_momentum:
    'Revenue growth percentile + sector-relative momentum + QoQ revenue acceleration. Blended to a 0–100 score, capped at the peer-cohort percentile.',
  institutional_confidence:
    'Price momentum (90d) + institutional holdings QoQ change + insider activity + holdings concentration. Combined adjustment applied to a base sub-score.',
  financial_evolution:
    'Revenue growth + operating margin trend + TTM operating cash flow margin. Penalties for negative margin slope.',
  thesis_integrity:
    'Recent news sentiment + relevance-weighted article count, blended with SEC 8-K and earnings event refinements. Stale-data penalty after 5 days.',
  des:
    'Macro signal tilts + sector regime score + tier-2 quality inputs. Capped contribution from any single macro feature.',
};

// Best-effort human label for a known component key. Falls back to
// humanCase() for unknowns so we never display raw snake_case.
const COMPONENT_LABEL = {
  revenue_growth_yoy: 'Revenue growth (YoY)',
  revenue_subscore: 'Revenue sub-score',
  trends_subscore: 'Google Trends sub-score',
  trends_slope: 'Google Trends slope',
  qoq_acceleration_pct: 'QoQ acceleration',
  qoq_subscore: 'QoQ acceleration sub-score',
  momentum_pct_90d: 'Price momentum (90d)',
  momentum_subscore: 'Momentum sub-score',
  inst_holdings_subscore: 'Institutional holdings sub-score',
  inst_holdings_change_qoq: 'Institutional holdings QoQ change',
  base_sub_score: 'Base sub-score',
  combined_adjustment_pts: 'Combined adjustment',
  margin_subscore: 'Operating margin sub-score',
  ocf_subscore: 'Operating cash flow sub-score',
  ttm_ocf_margin: 'TTM OCF margin',
  margin_trend_slope: 'Margin trend slope',
  article_count: 'News article count',
  mean_sentiment_score: 'Mean sentiment',
  mean_relevance_score: 'Mean relevance',
  sentiment_subscore_raw: 'Sentiment sub-score (raw)',
  events_score_raw: 'Events sub-score (raw)',
  events_weight: 'Events weight',
  yahoo_earnings_score: 'Yahoo earnings score',
  sec_8k_score: 'SEC 8-K score',
  total_contribution: 'Total macro contribution',
};

const COMPONENT_HIDE = new Set([
  'peer_cohort_strategy',
  'peer_cohort_size',
  'sector_cohort',
  'sector_cohort_size',
  'momentum_strategy_used',
  'momentum_cohort_size',
  'momentum_cohort_label',
  'margin_source',
  'confidence_blend',
  'applied_overrides',
  'tier2_inputs',
  'label_counts',
  'signal_tilts',
  'signal_contributions',
]);

// Heuristic mapping: data_quality_flag -> pillar most likely affected by it.
// Used to identify which flag is the "responsible flag" for a dropped pillar.
const FLAG_TO_PILLAR = {
  thesis_unavailable: 'thesis_integrity',
  thesis_stale: 'thesis_integrity',
  crypto_thesis_unavailable: 'thesis_integrity',
  no_sentiment: 'thesis_integrity',
  missing_sentiment: 'thesis_integrity',
  financial_unavailable: 'financial_evolution',
  no_margin: 'financial_evolution',
  no_ocf: 'financial_evolution',
  no_revenue: 'financial_evolution',
  institutional_unavailable: 'institutional_confidence',
  no_holdings: 'institutional_confidence',
  no_momentum: 'institutional_confidence',
  adoption_unavailable: 'adoption_momentum',
  no_trends: 'adoption_momentum',
  no_qoq: 'adoption_momentum',
  des_unavailable: 'des',
  no_macro: 'des',
};

// ----------------------------------------------------------------------------
// Module-level state — single tooltip instance reused across all pillars
// ----------------------------------------------------------------------------

const explainerState = {
  tooltipEl: null,
  arrowEl: null,
  backdropEl: null,
  activeTrigger: null,
  isMobile: false,
  outsideHandler: null,
  keyHandler: null,
  resizeHandler: null,
  scrollHandler: null,
  idCounter: 0,
};

function isMobileViewport() {
  try {
    return window.matchMedia('(max-width: 640px)').matches;
  } catch (_e) {
    return false;
  }
}

// ----------------------------------------------------------------------------
// Tiny element builder (kept local so this file has no deps on detail.js)
// ----------------------------------------------------------------------------

function el(tag, opts) {
  const node = document.createElement(tag);
  if (!opts) return node;
  if (opts.className) node.className = opts.className;
  if (opts.id) node.id = opts.id;
  if (opts.text != null) node.textContent = String(opts.text);
  if (opts.attrs) {
    for (const [k, v] of Object.entries(opts.attrs)) {
      if (v == null || v === false) continue;
      node.setAttribute(k, String(v));
    }
  }
  return node;
}

function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

function humanCase(s) {
  if (!s) return '';
  return String(s)
    .replace(/_/g, ' ')
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

function componentLabel(key) {
  return COMPONENT_LABEL[key] || humanCase(key);
}

function fmtNum(v) {
  if (v == null) return '—';
  if (typeof v === 'boolean') return v ? 'yes' : 'no';
  if (typeof v === 'string') return humanCase(v);
  const n = Number(v);
  if (!Number.isFinite(n)) return String(v);
  if (Math.abs(n) >= 100 && Number.isInteger(n)) return String(n);
  if (Math.abs(n) >= 10) return n.toFixed(1);
  if (Math.abs(n) >= 1) return n.toFixed(2);
  if (Math.abs(n) > 0.001) return n.toFixed(3);
  return n.toFixed(4);
}

function fmtScore(n) {
  const v = Number(n);
  return Number.isFinite(v) ? v.toFixed(1) : '—';
}

// ----------------------------------------------------------------------------
// Signal ranking — pick top 3-5 components from a vardetail row to surface
// ----------------------------------------------------------------------------
//
// We mirror the ranking heuristic from renderEvidence() so the explainer
// shows the same "story" as the Evidence accordion at the bottom of the
// modal, just in a denser form.

function rankSignals(components, limit = 5) {
  if (!components || typeof components !== 'object') return [];
  const out = [];
  for (const [k, v] of Object.entries(components)) {
    if (COMPONENT_HIDE.has(k)) continue;
    if (v == null) continue;

    if (typeof v === 'number') {
      if (!Number.isFinite(v)) continue;
      const isSubscore = /_subscore$|^sub_score$/.test(k) || k === 'base_sub_score';
      const magnitude = isSubscore ? Math.abs(v - 50) : Math.abs(v);
      // For sub-score-style values, contribution is signed distance from 50
      // (positive = lifts the pillar, negative = drags it).
      const contribution = isSubscore ? v - 50 : v;
      out.push({ key: k, value: v, magnitude, contribution, isSubscore });
      continue;
    }

    if (typeof v === 'object' && !Array.isArray(v)) {
      const candidates = ['signal_score', 'conviction_score', 'adjustment_pts', 'total_contribution', 'acceleration_4w_pct'];
      let pickedKey = null;
      let pickedVal = null;
      for (const c of candidates) {
        if (typeof v[c] === 'number' && Number.isFinite(v[c])) {
          pickedKey = c; pickedVal = v[c]; break;
        }
      }
      if (pickedKey == null) {
        for (const [ck, cv] of Object.entries(v)) {
          if (typeof cv === 'number' && Number.isFinite(cv)) {
            pickedKey = ck; pickedVal = cv; break;
          }
        }
      }
      if (pickedKey == null) {
        for (const labelKey of ['regime', 'conviction_signal']) {
          if (typeof v[labelKey] === 'string' && v[labelKey]) {
            out.push({
              key: `${k}.${labelKey}`,
              value: v[labelKey],
              magnitude: 1,
              contribution: 0,
              isCategorical: true,
            });
            break;
          }
        }
        continue;
      }
      out.push({
        key: `${k}.${pickedKey}`,
        value: pickedVal,
        magnitude: Math.abs(pickedVal),
        contribution: pickedVal,
      });
      continue;
    }
  }

  out.sort((a, b) => b.magnitude - a.magnitude);
  return out.slice(0, limit);
}

// ----------------------------------------------------------------------------
// Tooltip lifecycle
// ----------------------------------------------------------------------------

function ensureTooltipEl() {
  if (explainerState.tooltipEl) return explainerState.tooltipEl;
  const tip = el('div', {
    className: 'lthcs-explainer-tip',
    attrs: { role: 'tooltip', 'aria-hidden': 'true' },
  });
  tip.id = 'lthcs-explainer-tip';
  const arrow = el('div', { className: 'lthcs-explainer-tip-arrow', attrs: { 'aria-hidden': 'true' } });
  tip.appendChild(arrow);
  const content = el('div', { className: 'lthcs-explainer-tip-content' });
  tip.appendChild(content);
  document.body.appendChild(tip);
  // Backdrop (mobile bottom-sheet only)
  const backdrop = el('div', {
    className: 'lthcs-explainer-backdrop hidden',
    attrs: { 'aria-hidden': 'true' },
  });
  document.body.appendChild(backdrop);
  explainerState.tooltipEl = tip;
  explainerState.arrowEl = arrow;
  explainerState.backdropEl = backdrop;
  return tip;
}

function hideTooltip() {
  const tip = explainerState.tooltipEl;
  if (!tip) return;
  if (tip.classList.contains('hidden')) return;
  tip.classList.add('hidden');
  tip.classList.remove('is-mobile');
  tip.setAttribute('aria-hidden', 'true');
  const bk = explainerState.backdropEl;
  if (bk) bk.classList.add('hidden');
  const trig = explainerState.activeTrigger;
  if (trig) {
    trig.setAttribute('aria-expanded', 'false');
    trig.classList.remove('is-explainer-open');
  }
  explainerState.activeTrigger = null;
  if (explainerState.outsideHandler) {
    document.removeEventListener('pointerdown', explainerState.outsideHandler, true);
    explainerState.outsideHandler = null;
  }
  if (explainerState.keyHandler) {
    document.removeEventListener('keydown', explainerState.keyHandler, true);
    explainerState.keyHandler = null;
  }
  if (explainerState.resizeHandler) {
    window.removeEventListener('resize', explainerState.resizeHandler);
    explainerState.resizeHandler = null;
  }
  if (explainerState.scrollHandler) {
    window.removeEventListener('scroll', explainerState.scrollHandler, true);
    explainerState.scrollHandler = null;
  }
}

// Place the tooltip near `triggerEl`. Defaults to "above" the trigger; flips
// "below" if there's not enough room. On mobile we ignore position and slide
// up from the bottom of the viewport as a sheet.
function positionTooltip(triggerEl) {
  const tip = explainerState.tooltipEl;
  const arrow = explainerState.arrowEl;
  if (!tip || !triggerEl) return;

  if (explainerState.isMobile) {
    tip.classList.add('is-mobile');
    tip.style.left = '';
    tip.style.top = '';
    tip.style.transform = '';
    arrow.style.left = '';
    return;
  }
  tip.classList.remove('is-mobile');

  // Measure
  const r = triggerEl.getBoundingClientRect();
  const tipRect = tip.getBoundingClientRect();
  const margin = 8;
  const viewportW = window.innerWidth;
  const viewportH = window.innerHeight;

  // Horizontal: center on trigger, clamp into viewport
  let left = r.left + r.width / 2 - tipRect.width / 2;
  left = Math.max(margin, Math.min(left, viewportW - tipRect.width - margin));

  // Vertical: prefer above; flip below if not enough room
  let placeAbove = r.top - tipRect.height - 10 >= margin;
  let top;
  if (placeAbove) {
    top = r.top - tipRect.height - 10;
    tip.dataset.placement = 'above';
  } else if (r.bottom + tipRect.height + 10 <= viewportH - margin) {
    top = r.bottom + 10;
    tip.dataset.placement = 'below';
  } else {
    // viewport very short — pin to top with scroll
    top = margin;
    tip.dataset.placement = 'below';
  }

  tip.style.left = `${Math.round(left)}px`;
  tip.style.top = `${Math.round(top)}px`;

  // Arrow follows trigger center
  const arrowCenter = r.left + r.width / 2 - left;
  const clampedArrow = Math.max(14, Math.min(arrowCenter, tipRect.width - 14));
  arrow.style.left = `${Math.round(clampedArrow)}px`;
}

// ----------------------------------------------------------------------------
// Content builders
// ----------------------------------------------------------------------------

function buildHeader(pillarKey, subScore, { dropped, atFloor, atCeiling }) {
  const head = el('div', { className: 'lthcs-explainer-head' });
  head.appendChild(el('div', {
    className: 'lthcs-explainer-pillar',
    text: PILLAR_DISPLAY[pillarKey] || pillarKey,
  }));
  const scoreRow = el('div', { className: 'lthcs-explainer-score-row' });
  const scoreBadge = el('span', {
    className: 'lthcs-explainer-score',
    text: dropped ? 'dropped' : fmtScore(subScore),
  });
  scoreRow.appendChild(scoreBadge);
  if (!dropped && atFloor) {
    scoreRow.appendChild(el('span', {
      className: 'lthcs-explainer-extreme is-floor',
      text: 'at floor',
      attrs: { title: 'Sub-score is at the 0 floor — any further drag is clipped.' },
    }));
  } else if (!dropped && atCeiling) {
    scoreRow.appendChild(el('span', {
      className: 'lthcs-explainer-extreme is-ceiling',
      text: 'at ceiling',
      attrs: { title: 'Sub-score is at the 100 ceiling — any further lift is clipped.' },
    }));
  }
  head.appendChild(scoreRow);
  return head;
}

function buildSignalList(signals, subScore) {
  const list = el('ul', { className: 'lthcs-explainer-signals' });
  for (const s of signals) {
    const li = el('li', { className: 'lthcs-explainer-signal' });
    li.appendChild(el('span', {
      className: 'lthcs-explainer-signal-name',
      text: componentLabel(s.key),
    }));
    li.appendChild(el('span', {
      className: 'lthcs-explainer-signal-val',
      text: fmtNum(s.value),
    }));
    // Contribution direction. For sub-score-style components we already
    // computed a signed delta from 50. Otherwise we use the value's sign.
    if (!s.isCategorical) {
      const contrib = Number(s.contribution);
      if (Number.isFinite(contrib) && contrib !== 0) {
        const tone = contrib > 0 ? 'pos' : 'neg';
        const sym = contrib > 0 ? '+' : '−';
        const mag = Math.abs(contrib);
        const txt = s.isSubscore
          ? `${sym}${mag.toFixed(1)} vs neutral`
          : `${sym}${fmtNum(mag)}`;
        li.appendChild(el('span', {
          className: `lthcs-explainer-signal-contrib is-${tone}`,
          text: txt,
        }));
      }
    }
    list.appendChild(li);
  }
  if (signals.length === 0) {
    list.appendChild(el('li', {
      className: 'lthcs-explainer-signal is-empty',
      text: 'No individual signals available.',
    }));
  }
  return list;
}

function buildDroppedExplain(pillarKey, snapshotRow) {
  const flags = (snapshotRow && Array.isArray(snapshotRow.data_quality_flags))
    ? snapshotRow.data_quality_flags
    : [];
  // Try to find a flag whose mapping points at this pillar.
  let responsible = null;
  for (const f of flags) {
    if (FLAG_TO_PILLAR[f] === pillarKey) { responsible = f; break; }
  }
  // Fall back to the most "shaped" flag if no exact mapping. Many flags
  // reference the pillar in their name (e.g. "thesis_unavailable").
  if (!responsible) {
    const short = pillarKey.split('_')[0];
    for (const f of flags) {
      if (String(f).toLowerCase().includes(short)) { responsible = f; break; }
    }
  }
  const wrap = el('div', { className: 'lthcs-explainer-dropped' });
  wrap.appendChild(el('div', {
    className: 'lthcs-explainer-dropped-headline',
    text: 'Pillar dropped from this score.',
  }));
  if (responsible) {
    const reason = el('div', { className: 'lthcs-explainer-dropped-reason' });
    reason.appendChild(document.createTextNode('Responsible flag: '));
    reason.appendChild(el('code', {
      className: 'lthcs-explainer-flag',
      text: responsible,
    }));
    wrap.appendChild(reason);
  } else if (flags.length) {
    const reason = el('div', { className: 'lthcs-explainer-dropped-reason' });
    reason.appendChild(document.createTextNode('Data-quality flags fired: '));
    reason.appendChild(el('code', {
      className: 'lthcs-explainer-flag',
      text: flags.join(', '),
    }));
    wrap.appendChild(reason);
  } else {
    wrap.appendChild(el('div', {
      className: 'lthcs-explainer-dropped-reason',
      text: 'No supporting data available for this pillar on this date.',
    }));
  }
  wrap.appendChild(el('div', {
    className: 'lthcs-explainer-dropped-note',
    text: 'Its weight was redistributed proportionally across the remaining pillars.',
  }));
  return wrap;
}

function buildFallbackExplain(pillarKey) {
  const wrap = el('div', { className: 'lthcs-explainer-fallback' });
  wrap.appendChild(el('div', {
    className: 'lthcs-explainer-fallback-headline',
    text: 'Detailed breakdown not available for this ticker.',
  }));
  wrap.appendChild(el('p', {
    className: 'lthcs-explainer-fallback-formula',
    text: PILLAR_FORMULA[pillarKey] || 'No formula documented.',
  }));
  return wrap;
}

function buildFooter(pillarKey, panel) {
  const wrap = el('div', { className: 'lthcs-explainer-foot' });
  const link = el('button', {
    className: 'lthcs-explainer-evidence-link',
    text: 'View full evidence ›',
    attrs: { type: 'button' },
  });
  link.addEventListener('click', (e) => {
    e.stopPropagation();
    openEvidenceFor(panel, pillarKey);
  });
  wrap.appendChild(link);
  return wrap;
}

// Open and scroll-to the matching evidence accordion. Falls back to scrolling
// the Evidence section into view if a per-pillar <details> isn't found (e.g.
// vardetail still loading).
function openEvidenceFor(panel, pillarKey) {
  hideTooltip();
  if (!panel) return;
  const evidence = panel.querySelector('[data-slot="evidence"]');
  if (!evidence) return;
  let target = null;
  const accordions = evidence.querySelectorAll('details.lthcs-evidence-pillar');
  for (const d of accordions) {
    if (d.dataset.pillar === pillarKey) { target = d; break; }
    const head = d.querySelector('.lthcs-evidence-pillar-name');
    if (head && head.textContent === (PILLAR_DISPLAY[pillarKey] || pillarKey)) {
      target = d; break;
    }
  }
  if (target) {
    target.open = true;
    target.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    // Briefly highlight so users see what changed
    target.classList.add('is-flash');
    setTimeout(() => target.classList.remove('is-flash'), 1400);
  } else {
    evidence.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
}

// ----------------------------------------------------------------------------
// Main render — populates the tooltip for the given trigger+pillar
// ----------------------------------------------------------------------------

function renderTooltipContent(panel, ctx) {
  const tip = ensureTooltipEl();
  const content = tip.querySelector('.lthcs-explainer-tip-content');
  clear(content);

  const { pillarKey, snapshotRow, vardetailRow } = ctx;
  const subscores = (snapshotRow && snapshotRow.subscores) || {};
  const subScore = Number(subscores[pillarKey]);
  const dropped = Array.isArray(snapshotRow && snapshotRow.dropped_pillars)
    && snapshotRow.dropped_pillars.includes(pillarKey);
  const atFloor = Number.isFinite(subScore) && subScore <= 1;
  const atCeiling = Number.isFinite(subScore) && subScore >= 99;

  content.appendChild(buildHeader(pillarKey, subScore, { dropped, atFloor, atCeiling }));

  if (dropped) {
    content.appendChild(buildDroppedExplain(pillarKey, snapshotRow));
  } else if (!vardetailRow) {
    content.appendChild(buildFallbackExplain(pillarKey));
  } else {
    const signals = rankSignals(vardetailRow.components, 5);
    // Brief lead-in line explains what users are looking at.
    content.appendChild(el('div', {
      className: 'lthcs-explainer-leadin',
      text: signals.length
        ? `Top ${signals.length} signal${signals.length === 1 ? '' : 's'} driving this sub-score:`
        : 'Sub-score computed but individual signals not surfaced.',
    }));
    content.appendChild(buildSignalList(signals, subScore));
  }

  content.appendChild(buildFooter(pillarKey, panel));
}

// ----------------------------------------------------------------------------
// Show / hide handlers
// ----------------------------------------------------------------------------

function showTooltip(triggerEl, panel, ctx) {
  if (explainerState.activeTrigger === triggerEl) {
    hideTooltip();
    return;
  }
  if (explainerState.activeTrigger) hideTooltip();

  explainerState.isMobile = isMobileViewport();
  const tip = ensureTooltipEl();
  renderTooltipContent(panel, ctx);
  tip.classList.remove('hidden');
  tip.setAttribute('aria-hidden', 'false');
  if (explainerState.isMobile && explainerState.backdropEl) {
    explainerState.backdropEl.classList.remove('hidden');
  }
  explainerState.activeTrigger = triggerEl;
  triggerEl.setAttribute('aria-expanded', 'true');
  triggerEl.classList.add('is-explainer-open');

  // Two-frame position (first frame so the browser measures content, second
  // frame so position uses the final box).
  requestAnimationFrame(() => {
    positionTooltip(triggerEl);
    requestAnimationFrame(() => positionTooltip(triggerEl));
  });

  // Outside-click dismiss. We use pointerdown so the click that opens the
  // tooltip on a trigger doesn't immediately dismiss it (synthetic mousedown
  // happens before click but on the trigger itself, not outside).
  explainerState.outsideHandler = (ev) => {
    const t = ev.target;
    if (tip.contains(t)) return;
    if (triggerEl.contains(t)) return;
    if (explainerState.backdropEl && explainerState.backdropEl.contains(t)) {
      hideTooltip();
      return;
    }
    hideTooltip();
  };
  document.addEventListener('pointerdown', explainerState.outsideHandler, true);

  // ESC dismiss. We register on capture so we run BEFORE the modal's own
  // ESC handler (which would close the modal entirely).
  explainerState.keyHandler = (ev) => {
    if (ev.key === 'Escape') {
      ev.preventDefault();
      ev.stopPropagation();
      hideTooltip();
      try { triggerEl.focus(); } catch (_e) { /* ignore */ }
    }
  };
  document.addEventListener('keydown', explainerState.keyHandler, true);

  // Reposition on resize / scroll so the arrow stays glued to the trigger.
  explainerState.resizeHandler = () => {
    explainerState.isMobile = isMobileViewport();
    positionTooltip(triggerEl);
  };
  window.addEventListener('resize', explainerState.resizeHandler);
  explainerState.scrollHandler = () => positionTooltip(triggerEl);
  window.addEventListener('scroll', explainerState.scrollHandler, true);
}

// ----------------------------------------------------------------------------
// Public: bind explainer triggers to all pillar rows in the panel
// ----------------------------------------------------------------------------

function buildContext(panel, pillarKey, snapshotRow, getVardetail, ticker) {
  let vardetailRow = null;
  try {
    const rows = typeof getVardetail === 'function' ? getVardetail() : null;
    if (Array.isArray(rows)) {
      for (const r of rows) {
        if (r && r.ticker === ticker && r.pillar === pillarKey) {
          vardetailRow = r;
          break;
        }
      }
    }
  } catch (_e) {
    vardetailRow = null;
  }
  return { pillarKey, snapshotRow, vardetailRow, ticker };
}

// Wrap each pillar-row's score `<strong>` element in a focusable button-like
// trigger. We do this in-place so styling is shared with the existing
// `.lthcs-pillar-values strong` rule.
export function bindPillarExplainer(panel, { snapshotRow, ticker, getVardetail }) {
  if (!panel) return;
  const pillarRows = panel.querySelectorAll('.lthcs-modal-pillars .lthcs-pillar-row');
  let i = 0;
  for (const row of pillarRows) {
    const pillarKey = PILLAR_ORDER[i++] || null;
    if (!pillarKey) continue;
    const valuesEl = row.querySelector('.lthcs-pillar-values');
    if (!valuesEl) continue;
    const strong = valuesEl.querySelector('strong');
    if (!strong) continue;
    // Avoid double-binding (e.g. on refreshPillarExplainer re-runs we wipe
    // dataset.bound only when rebuilding rows).
    if (strong.dataset.explainerBound === '1') continue;
    strong.dataset.explainerBound = '1';
    strong.dataset.pillar = pillarKey;

    // Make it a proper interactive element. We can't change tag without
    // disturbing existing CSS, so we add ARIA + tabindex + role to make it
    // keyboard-accessible while keeping the visual.
    strong.setAttribute('role', 'button');
    strong.setAttribute('tabindex', '0');
    strong.setAttribute('aria-haspopup', 'dialog');
    strong.setAttribute('aria-expanded', 'false');
    explainerState.idCounter += 1;
    strong.setAttribute('aria-describedby', 'lthcs-explainer-tip');
    strong.setAttribute(
      'aria-label',
      `${PILLAR_DISPLAY[pillarKey] || pillarKey} sub-score; activate for explanation`,
    );
    strong.classList.add('lthcs-explainer-trigger');

    const handler = (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      const ctx = buildContext(panel, pillarKey, snapshotRow, getVardetail, ticker);
      showTooltip(strong, panel, ctx);
    };

    strong.addEventListener('click', handler);
    strong.addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter' || ev.key === ' ') {
        handler(ev);
      }
    });

    // Hover (desktop only). We use mouseenter + a small delay so the tooltip
    // doesn't flicker on a fast cursor sweep. mouseleave doesn't hide if the
    // user has clicked open the tooltip — that requires outside-click or ESC.
    let hoverTimer = null;
    strong.addEventListener('mouseenter', () => {
      if (isMobileViewport()) return;
      if (explainerState.activeTrigger === strong) return;
      if (hoverTimer) clearTimeout(hoverTimer);
      hoverTimer = setTimeout(() => {
        const ctx = buildContext(panel, pillarKey, snapshotRow, getVardetail, ticker);
        showTooltip(strong, panel, ctx);
      }, 180);
    });
    strong.addEventListener('mouseleave', () => {
      if (hoverTimer) { clearTimeout(hoverTimer); hoverTimer = null; }
      // Only auto-hide if the tooltip itself isn't being hovered.
      if (explainerState.activeTrigger !== strong) return;
      const tip = explainerState.tooltipEl;
      if (!tip) return;
      // Defer to next tick so a mouseenter on the tooltip can cancel.
      setTimeout(() => {
        if (explainerState.activeTrigger !== strong) return;
        if (tip.matches(':hover')) return;
        // Keep open if the user moved focus onto the trigger (keyboard mode).
        if (document.activeElement === strong) return;
        hideTooltip();
      }, 200);
    });
  }
}

// Force a fresh bind after the panel re-renders (e.g. when openDetail is
// called for a new ticker). Cheap: just resets dataset markers and rebinds.
export function refreshPillarExplainer(panel, args) {
  if (!panel) return;
  // Reset bound markers so bindPillarExplainer redoes the wiring.
  const triggers = panel.querySelectorAll('.lthcs-pillar-values strong[data-explainer-bound="1"]');
  for (const t of triggers) {
    t.removeAttribute('data-explainer-bound');
    t.removeAttribute('role');
    t.removeAttribute('tabindex');
    t.removeAttribute('aria-haspopup');
    t.removeAttribute('aria-expanded');
    t.removeAttribute('aria-describedby');
    t.removeAttribute('aria-label');
    t.removeAttribute('data-pillar');
    t.classList.remove('lthcs-explainer-trigger');
    // Clone-replace to drop event listeners cleanly.
    const fresh = t.cloneNode(true);
    if (t.parentNode) t.parentNode.replaceChild(fresh, t);
  }
  hideTooltip();
  bindPillarExplainer(panel, args);
}

// Convenience: hide the tooltip from outside callers (e.g. on closeDetail).
export function hidePillarExplainer() {
  hideTooltip();
}
