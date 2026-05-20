// lthcs-whatsnew.js
// Phase 5 #2: "What's New since your last visit" timeline panel.
//
// Tracks two pieces of state in localStorage:
//   - lthcs.lastVisitISO    : ISO timestamp of the previous visit. Updated
//                             only AFTER the panel paints, so the current
//                             visit always sees "last visit = previous one".
//   - lthcs.whatsNewDismiss : { atISO, snapshotDate, bandsByTicker }
//                             Records the band map at dismiss time so we
//                             can pop the panel back open when new band
//                             changes accumulate since dismissal.
//
// Public API:
//   initWhatsNew()                 — wire the panel root + restore state
//   updateWhatsNew(enrichedRows, snapshotDate)
//                                  — recompute the change set and paint
//
// The panel compares each enriched row's band to the SNAPSHOT of bands the
// user saw on their previous visit (snapshotted under `lthcs.bandsByTicker`).
// First-ever visit shows nothing — there's no baseline yet.

'use strict';

const STORAGE_KEYS = {
  lastVisit: 'lthcs.lastVisitISO',
  bandsByTicker: 'lthcs.bandsByTicker',
  dismiss: 'lthcs.whatsNewDismiss',
};

// Band ordering: higher index = stronger conviction. Used to label
// directional moves as "up" (toward Elite) vs "down" (toward Review).
const BAND_RANK = {
  review: 0,
  weakening: 1,
  monitor: 2,
  constructive: 3,
  high: 4,
  elite: 5,
};

const BAND_LABEL = {
  elite: 'Elite',
  high: 'High Confidence',
  constructive: 'Constructive',
  monitor: 'Monitor',
  weakening: 'Weakening',
  review: 'Review',
};

const state = {
  expanded: false,
  dismissed: false,
  lastChanges: [], // [{ ticker, from, to, direction }]
};

// ---------------------------------------------------------------------------
// Storage helpers (best-effort)
// ---------------------------------------------------------------------------

function safeGet(key) {
  try { return window.localStorage.getItem(key); } catch { return null; }
}
function safeSet(key, value) {
  try { window.localStorage.setItem(key, value); } catch { /* quota / private mode */ }
}
function safeDel(key) {
  try { window.localStorage.removeItem(key); } catch { /* ignore */ }
}

function loadBaselineBands() {
  const raw = safeGet(STORAGE_KEYS.bandsByTicker);
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw);
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) return parsed;
    return null;
  } catch {
    return null;
  }
}

function saveBaselineBands(map) {
  if (!map || typeof map !== 'object') return;
  safeSet(STORAGE_KEYS.bandsByTicker, JSON.stringify(map));
}

function loadDismiss() {
  const raw = safeGet(STORAGE_KEYS.dismiss);
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw);
    if (parsed && typeof parsed === 'object') return parsed;
    return null;
  } catch {
    return null;
  }
}

function saveDismiss(record) {
  if (!record) safeDel(STORAGE_KEYS.dismiss);
  else safeSet(STORAGE_KEYS.dismiss, JSON.stringify(record));
}

function escapeHtml(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function formatDismissedAt(iso) {
  if (!iso) return '—';
  try {
    const d = new Date(iso);
    return d.toLocaleString(undefined, {
      year: 'numeric', month: 'short', day: 'numeric',
      hour: 'numeric', minute: '2-digit',
    });
  } catch {
    return iso;
  }
}

// ---------------------------------------------------------------------------
// Diff computation
// ---------------------------------------------------------------------------

// Build a fresh { ticker -> uiBand } map from the enriched rows.
function bandsFromRows(rows) {
  const out = {};
  for (const r of rows) {
    if (r && r.ticker && r.uiBand) out[r.ticker] = r.uiBand;
  }
  return out;
}

// Returns [{ ticker, from, to, direction: 'up'|'down' }] for tickers whose
// band changed between `baseline` and `current`. Tickers present only in
// `current` are skipped (no prior baseline → not a "change"). Tickers present
// only in `baseline` are skipped too (universe shrinking is noise, not news).
function diffBands(baseline, current) {
  if (!baseline || !current) return [];
  const out = [];
  for (const ticker of Object.keys(current)) {
    const to = current[ticker];
    const from = baseline[ticker];
    if (!from || from === to) continue;
    const fromRank = BAND_RANK[from];
    const toRank = BAND_RANK[to];
    if (fromRank == null || toRank == null) continue;
    out.push({
      ticker,
      from,
      to,
      direction: toRank > fromRank ? 'up' : 'down',
    });
  }
  // Stable order: ticker A→Z for predictability.
  out.sort((a, b) => a.ticker.localeCompare(b.ticker));
  return out;
}

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------

function root() {
  return document.getElementById('lthcs-whatsnew');
}

function render(lastVisitISO) {
  const host = root();
  if (!host) return;
  const changes = state.lastChanges;
  if (!changes.length) {
    host.classList.add('hidden');
    host.innerHTML = '';
    return;
  }
  if (state.dismissed) {
    host.classList.add('hidden');
    host.innerHTML = '';
    return;
  }

  const ups = changes.filter((c) => c.direction === 'up').length;
  const downs = changes.filter((c) => c.direction === 'down').length;

  const dismissRec = loadDismiss();
  const dismissNote = (dismissRec && dismissRec.atISO)
    ? `<span class="lthcs-whatsnew-dismissed">Dismissed at ${escapeHtml(formatDismissedAt(dismissRec.atISO))} — reopened (new changes)</span>`
    : '';

  const lastVisitNote = lastVisitISO
    ? `<span class="lthcs-whatsnew-visit">Since ${escapeHtml(formatDismissedAt(lastVisitISO))}</span>`
    : '';

  const summary = (
    `<button type="button" class="lthcs-whatsnew-summary" aria-expanded="${state.expanded ? 'true' : 'false'}" id="lthcs-whatsnew-toggle">` +
      `<span class="lthcs-whatsnew-eyebrow">What's New</span>` +
      `<span class="lthcs-whatsnew-line">` +
        (ups ? `<span class="lthcs-whatsnew-up">${ups} ticker${ups === 1 ? '' : 's'} moved up</span>` : '') +
        (ups && downs ? `<span class="lthcs-whatsnew-sep">·</span>` : '') +
        (downs ? `<span class="lthcs-whatsnew-down">${downs} down</span>` : '') +
      `</span>` +
      `<span class="lthcs-whatsnew-caret" aria-hidden="true">${state.expanded ? '▾' : '▸'}</span>` +
    `</button>`
  );

  let timelineHtml = '';
  if (state.expanded) {
    const rows = changes.map((c) => {
      const fromLabel = escapeHtml(BAND_LABEL[c.from] || c.from);
      const toLabel = escapeHtml(BAND_LABEL[c.to] || c.to);
      const arrow = c.direction === 'up' ? '↑' : '↓';
      return (
        `<li class="lthcs-whatsnew-row" data-direction="${escapeHtml(c.direction)}">` +
          `<span class="lthcs-whatsnew-ticker">${escapeHtml(c.ticker)}</span>` +
          `<span class="lthcs-whatsnew-from band-${escapeHtml(c.from)}">${fromLabel}</span>` +
          `<span class="lthcs-whatsnew-arrow" aria-hidden="true">${arrow}</span>` +
          `<span class="lthcs-whatsnew-to band-${escapeHtml(c.to)}">${toLabel}</span>` +
        `</li>`
      );
    }).join('');
    timelineHtml = `<ul class="lthcs-whatsnew-list">${rows}</ul>`;
  }

  host.classList.remove('hidden');
  host.innerHTML = (
    `<div class="lthcs-whatsnew-bar">` +
      summary +
      `<div class="lthcs-whatsnew-meta">${lastVisitNote}${dismissNote}</div>` +
      `<button type="button" class="lthcs-whatsnew-dismiss" id="lthcs-whatsnew-dismiss" aria-label="Dismiss what's new">×</button>` +
    `</div>` +
    timelineHtml
  );

  const toggleBtn = document.getElementById('lthcs-whatsnew-toggle');
  if (toggleBtn) {
    toggleBtn.addEventListener('click', () => {
      state.expanded = !state.expanded;
      render(lastVisitISO);
    });
  }
  const dismissBtn = document.getElementById('lthcs-whatsnew-dismiss');
  if (dismissBtn) {
    dismissBtn.addEventListener('click', () => {
      state.dismissed = true;
      // Snapshot the current bands at dismiss-time so future renders only
      // re-open the panel when MORE changes accumulate beyond this point.
      const currentBands = bandsFromRows(window.__lthcsLastEnriched || []);
      saveDismiss({
        atISO: new Date().toISOString(),
        bandsByTicker: currentBands,
      });
      render(lastVisitISO);
    });
  }
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

let cachedLastVisit = null;

export function initWhatsNew() {
  // Capture the user's previous-visit timestamp BEFORE we overwrite it.
  cachedLastVisit = safeGet(STORAGE_KEYS.lastVisit);
  // Write the current visit immediately so a tab refresh doesn't keep
  // re-reading the same "last visit" forever. (We still use the cached
  // value for the in-memory render this session.)
  safeSet(STORAGE_KEYS.lastVisit, new Date().toISOString());
}

export function updateWhatsNew(enrichedRows, snapshotDate) {
  if (!Array.isArray(enrichedRows)) return;
  // Park the enriched bands map for the dismiss handler to snapshot.
  window.__lthcsLastEnriched = enrichedRows;

  const currentBands = bandsFromRows(enrichedRows);
  const baseline = loadBaselineBands();

  // Diff against baseline if available; otherwise no changes (first visit).
  let changes = diffBands(baseline, currentBands);

  // Dismiss reconciliation: if the user previously dismissed, only re-open
  // the panel if there are NEW changes beyond the dismiss-time snapshot.
  const dismissRec = loadDismiss();
  if (dismissRec && dismissRec.bandsByTicker) {
    const sinceDismiss = diffBands(dismissRec.bandsByTicker, currentBands);
    if (sinceDismiss.length === 0) {
      // Nothing new since dismiss — stay dismissed.
      state.dismissed = true;
    } else {
      // Re-open with the FULL change set (from baseline → now) so the user
      // sees the complete picture, not just the delta-since-dismiss.
      state.dismissed = false;
      // Optionally narrow to "since dismiss" — spec says pop back open when
      // any band change since dismiss-time, so include the full diff but
      // ensure at least one is newer than dismiss.
    }
  } else {
    state.dismissed = false;
  }

  state.lastChanges = changes;
  render(cachedLastVisit);

  // Persist current bands as the new baseline for the NEXT visit. We only
  // refresh the baseline once per snapshot date so multiple in-session
  // refreshes don't "consume" the user's change-set.
  const lastBaselineDate = safeGet('lthcs.bandsByTicker.snapshotDate');
  if (!baseline || lastBaselineDate !== snapshotDate) {
    saveBaselineBands(currentBands);
    safeSet('lthcs.bandsByTicker.snapshotDate', snapshotDate || '');
  }
}
