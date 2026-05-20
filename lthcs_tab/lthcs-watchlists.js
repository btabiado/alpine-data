// lthcs-watchlists.js
// Phase 4: custom universes / watchlists on the LTHCS card view.
//
// A "watchlist" is a named set of ticker symbols persisted in
// localStorage under `lthcs.watchlists` as:
//
//   { "Bryan's watchlist": ["AAPL", "MSFT", ...], "Boring banks": [...] }
//
// The active selection (which watchlist chip is currently engaged, if any)
// is persisted separately under `lthcs.activeWatchlist`. Both keys are
// best-effort: any parse/quota failure degrades the page to "no watchlist
// active" rather than throwing.
//
// Public API (used by lthcs-tab.js):
//   initWatchlists({ onChange, getUniverseTickers })
//   getActiveWatchlist() -> string | null
//   getActiveTickerSet() -> Set<string> | null     // null = no watchlist active
//   activeIsEmpty()      -> boolean                 // true when watchlist has 0 tickers
//   getActiveName()      -> string | null           // name of active watchlist
//   renderChips()        -> re-paint chip row (call after data mutates)
//   warnIfStale()        -> show one-line warning + undo if active watchlist
//                           became entirely invalid against the current universe
//
// Mobile note: the chip row is the same `.lthcs-chip-group` family as the
// existing drift chips, so it inherits the wrap + min-height: 44px tap target.
// The modal uses the same backdrop-blur + max-height pattern as the detail
// modal — it scrolls on small viewports.

'use strict';

const STORAGE_KEYS = {
  lists: 'lthcs.watchlists',
  active: 'lthcs.activeWatchlist',
};

const DEFAULT_LIST_NAME = "Bryan's watchlist";
const MAX_WATCHLISTS = 5;
const MAX_NAME_LEN = 40;
const TICKER_RE = /^[A-Z][A-Z0-9.\-]{0,9}$/;

// In-memory cache. Always re-loaded from localStorage on init, but mutations
// go through saveLists()/saveActive() so the persisted copy stays in sync.
const state = {
  lists: {},               // { name -> string[] (uppercase tickers) }
  activeName: null,        // string | null
  universeTickers: new Set(),
  lastUndo: null,          // { name, removed: string[] } for the auto-clear undo banner
  onChange: () => {},      // injected by initWatchlists
};

// ---------------------------------------------------------------------------
// localStorage helpers (private)
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

function loadLists() {
  const raw = safeGet(STORAGE_KEYS.lists);
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return null;
    const out = {};
    for (const [name, arr] of Object.entries(parsed)) {
      if (typeof name !== 'string' || !name.trim()) continue;
      if (!Array.isArray(arr)) continue;
      out[name.slice(0, MAX_NAME_LEN)] = arr
        .filter((t) => typeof t === 'string')
        .map((t) => t.trim().toUpperCase())
        .filter((t) => t.length);
    }
    return out;
  } catch (err) {
    console.warn('LTHCS: failed to parse persisted watchlists.', err);
    return null;
  }
}

function saveLists() {
  safeSet(STORAGE_KEYS.lists, JSON.stringify(state.lists));
}

function loadActive() {
  const raw = safeGet(STORAGE_KEYS.active);
  if (raw == null) return null;
  try {
    const parsed = JSON.parse(raw);
    if (parsed === null) return null;
    if (typeof parsed === 'string' && parsed.trim()) return parsed.slice(0, MAX_NAME_LEN);
    return null;
  } catch {
    // Legacy unquoted string fallback — tolerate a non-JSON value.
    return raw.slice(0, MAX_NAME_LEN);
  }
}

function saveActive() {
  if (state.activeName) safeSet(STORAGE_KEYS.active, JSON.stringify(state.activeName));
  else safeDel(STORAGE_KEYS.active);
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

export function getActiveWatchlist() {
  return state.activeName;
}

export function getActiveName() {
  return state.activeName;
}

export function getActiveTickerSet() {
  if (!state.activeName) return null;
  const list = state.lists[state.activeName];
  if (!Array.isArray(list)) return null;
  return new Set(list);
}

export function activeIsEmpty() {
  if (!state.activeName) return false;
  const list = state.lists[state.activeName];
  return Array.isArray(list) && list.length === 0;
}

// Names of all saved watchlists, in stable insertion order.
function listNames() {
  return Object.keys(state.lists);
}

// ---------------------------------------------------------------------------
// HTML helpers (kept module-local to avoid pulling in lthcs-tab's escapeHtml)
// ---------------------------------------------------------------------------

function escapeHtml(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

// Parse a free-form textarea blob into a sorted, deduplicated list of
// uppercase ticker symbols. Accepts newline-, comma-, semicolon-, or
// whitespace-separated input. Silently drops tokens that don't match the
// ticker regex (so e.g. "AAPL, msft, # comment" -> ["AAPL", "MSFT"]).
function parseTickersBlob(raw) {
  if (typeof raw !== 'string') return [];
  const tokens = raw
    .split(/[\s,;]+/)
    .map((t) => t.trim().toUpperCase())
    .filter((t) => t.length);
  const seen = new Set();
  const out = [];
  for (const t of tokens) {
    if (!TICKER_RE.test(t)) continue;
    if (seen.has(t)) continue;
    seen.add(t);
    out.push(t);
  }
  return out;
}

// Split a tickers array into [valid, invalid] against the current universe.
// Invalid = not in universeTickers. Both arrays preserve input order.
function partitionByUniverse(tickers) {
  const valid = [];
  const invalid = [];
  for (const t of tickers) {
    if (state.universeTickers.has(t)) valid.push(t);
    else invalid.push(t);
  }
  return [valid, invalid];
}

// ---------------------------------------------------------------------------
// Chip row rendering
// ---------------------------------------------------------------------------

export function renderChips() {
  const host = document.getElementById('lthcs-watchlist-chips');
  if (!host) return;
  const names = listNames();
  const parts = [];
  for (const name of names) {
    const isActive = name === state.activeName;
    const count = (state.lists[name] || []).length;
    const ariaLabel = `Filter to watchlist ${name} (${count} ticker${count === 1 ? '' : 's'})`;
    parts.push(
      `<button type="button" class="lthcs-chip lthcs-watchlist-chip${isActive ? ' is-active' : ''}" ` +
        `data-watchlist-name="${escapeHtml(name)}" ` +
        `aria-pressed="${isActive ? 'true' : 'false'}" ` +
        `aria-label="${escapeHtml(ariaLabel)}">` +
        `<span class="lthcs-watchlist-chip-name">${escapeHtml(name)}</span>` +
        `<span class="lthcs-watchlist-chip-count" aria-hidden="true">${count}</span>` +
      `</button>`
    );
  }
  // Disabled "+ New" hint when at the cap — Manage still works to delete.
  const atCap = names.length >= MAX_WATCHLISTS;
  parts.push(
    `<button type="button" class="lthcs-chip lthcs-watchlist-manage" ` +
      `id="lthcs-watchlist-manage-btn" ` +
      `aria-label="Manage watchlists">` +
      `<span aria-hidden="true">${atCap ? '✎' : '+'}</span>` +
      `<span>Manage</span>` +
    `</button>`
  );
  host.innerHTML = parts.join('');
}

// ---------------------------------------------------------------------------
// Stale-watchlist warning banner
// ---------------------------------------------------------------------------

// If the active watchlist has tickers, but none of them are in the current
// universe (e.g. universe shrank, or user typed only future tickers), show
// a one-line warning + undo. We do NOT mutate the list — the user can undo
// after seeing the warning. The card view's empty-state already explains
// "no tickers match"; this banner is the actionable companion.
export function warnIfStale() {
  const host = document.getElementById('lthcs-watchlist-warning');
  if (!host) return;
  if (!state.activeName) {
    host.classList.add('hidden');
    host.innerHTML = '';
    return;
  }
  const list = state.lists[state.activeName] || [];
  if (!list.length) {
    host.classList.add('hidden');
    host.innerHTML = '';
    return;
  }
  const [valid, invalid] = partitionByUniverse(list);
  if (valid.length > 0 || invalid.length === 0) {
    host.classList.add('hidden');
    host.innerHTML = '';
    return;
  }
  // All tickers are invalid → flag + auto-clear with undo.
  const removed = list.slice();
  state.lastUndo = { name: state.activeName, removed };
  state.lists[state.activeName] = [];
  saveLists();
  host.classList.remove('hidden');
  host.innerHTML = (
    `<span class="lthcs-watchlist-warning-text">` +
      `All tickers in <strong>${escapeHtml(state.activeName)}</strong> are outside the current universe. Auto-cleared.` +
    `</span>` +
    `<button type="button" class="lthcs-watchlist-warning-undo" id="lthcs-watchlist-undo">Undo</button>`
  );
}

function applyUndo() {
  if (!state.lastUndo) return;
  const { name, removed } = state.lastUndo;
  if (state.lists[name]) {
    state.lists[name] = removed.slice();
    saveLists();
  }
  state.lastUndo = null;
  const host = document.getElementById('lthcs-watchlist-warning');
  if (host) {
    host.classList.add('hidden');
    host.innerHTML = '';
  }
  renderChips();
  state.onChange();
}

// ---------------------------------------------------------------------------
// Manage modal — open/close + content rendering
// ---------------------------------------------------------------------------

// We keep a single in-modal "draft" so the user can edit a watchlist's
// tickers without immediately mutating the persisted list — only Save
// commits the change. The draft tracks the currently-selected list inside
// the modal (independent of the page-level active filter).
const modal = {
  draftName: null,         // which watchlist is the textarea showing?
  draftText: '',           // current textarea contents
  pendingRename: null,     // { from, to } if user is editing the name field
};

function modalRoot() {
  return document.getElementById('lthcs-watchlist-modal');
}

function openModal(initialName) {
  const root = modalRoot();
  if (!root) return;
  const names = listNames();
  const startName = initialName && state.lists[initialName]
    ? initialName
    : (state.activeName && state.lists[state.activeName] ? state.activeName : names[0] || null);
  modal.draftName = startName;
  modal.draftText = startName ? (state.lists[startName] || []).join('\n') : '';
  modal.pendingRename = null;
  root.classList.remove('hidden');
  root.setAttribute('aria-hidden', 'false');
  renderModal();
  // Focus the new-list input by default if there are no watchlists yet,
  // otherwise the textarea.
  setTimeout(() => {
    const target = startName
      ? document.getElementById('lthcs-watchlist-textarea')
      : document.getElementById('lthcs-watchlist-new-name');
    if (target) target.focus();
  }, 0);
}

function closeModal() {
  const root = modalRoot();
  if (!root) return;
  root.classList.add('hidden');
  root.setAttribute('aria-hidden', 'true');
  modal.draftName = null;
  modal.draftText = '';
  modal.pendingRename = null;
}

function renderModal() {
  const root = modalRoot();
  if (!root) return;
  const names = listNames();
  const atCap = names.length >= MAX_WATCHLISTS;
  const selectedName = modal.draftName;
  const selectedTickers = selectedName ? parseTickersBlob(modal.draftText) : [];
  const [validSel, invalidSel] = partitionByUniverse(selectedTickers);

  // List of watchlists (left/top panel).
  const listRowsHtml = names.length
    ? names.map((name) => {
        const count = (state.lists[name] || []).length;
        const isSelected = name === selectedName;
        return (
          `<li class="lthcs-watchlist-row${isSelected ? ' is-selected' : ''}">` +
            `<button type="button" class="lthcs-watchlist-row-select" data-select-name="${escapeHtml(name)}" aria-pressed="${isSelected ? 'true' : 'false'}">` +
              `<span class="lthcs-watchlist-row-name">${escapeHtml(name)}</span>` +
              `<span class="lthcs-watchlist-row-count">${count}</span>` +
            `</button>` +
            `<button type="button" class="lthcs-watchlist-row-delete" data-delete-name="${escapeHtml(name)}" aria-label="Delete watchlist ${escapeHtml(name)}">&times;</button>` +
          `</li>`
        );
      }).join('')
    : `<li class="lthcs-watchlist-row-empty">No watchlists yet. Create one below.</li>`;

  // New-watchlist input (disabled at cap).
  const newInputHtml = (
    `<div class="lthcs-watchlist-new">` +
      `<input type="text" id="lthcs-watchlist-new-name" class="lthcs-watchlist-new-input" ` +
        `placeholder="${atCap ? 'Watchlist cap (5) reached' : 'New watchlist name'}" ` +
        `maxlength="${MAX_NAME_LEN}" ` +
        `${atCap ? 'disabled' : ''} ` +
        `autocomplete="off" spellcheck="false" />` +
      `<button type="button" class="lthcs-watchlist-new-btn" id="lthcs-watchlist-new-btn" ${atCap ? 'disabled' : ''}>` +
        `Create` +
      `</button>` +
    `</div>`
  );

  // Right panel: editor for the selected watchlist.
  let editorHtml = '';
  if (selectedName) {
    const validLine = validSel.length
      ? `<span class="lthcs-watchlist-validity is-ok">${validSel.length} valid</span>`
      : '';
    const invalidLine = invalidSel.length
      ? `<span class="lthcs-watchlist-validity is-warn" title="${escapeHtml(invalidSel.join(', '))}">${invalidSel.length} not in universe</span>`
      : '';
    editorHtml = (
      `<div class="lthcs-watchlist-editor">` +
        `<div class="lthcs-watchlist-editor-head">` +
          `<label class="lthcs-watchlist-editor-label" for="lthcs-watchlist-rename">Name</label>` +
          `<input type="text" id="lthcs-watchlist-rename" class="lthcs-watchlist-rename-input" ` +
            `value="${escapeHtml(selectedName)}" maxlength="${MAX_NAME_LEN}" ` +
            `autocomplete="off" spellcheck="false" />` +
        `</div>` +
        `<div class="lthcs-watchlist-editor-head">` +
          `<label class="lthcs-watchlist-editor-label" for="lthcs-watchlist-textarea">Tickers (one per line or comma-separated)</label>` +
          `<div class="lthcs-watchlist-validity-row">${validLine}${invalidLine}</div>` +
        `</div>` +
        `<textarea id="lthcs-watchlist-textarea" class="lthcs-watchlist-textarea" rows="8" ` +
          `placeholder="AAPL, MSFT, NVDA&#10;GOOGL" ` +
          `autocomplete="off" spellcheck="false">${escapeHtml(modal.draftText)}</textarea>` +
        (invalidSel.length
          ? `<div class="lthcs-watchlist-invalid-list">Not in universe: ${escapeHtml(invalidSel.join(', '))}</div>`
          : '') +
      `</div>`
    );
  } else {
    editorHtml = (
      `<div class="lthcs-watchlist-editor-empty">` +
        `Select a watchlist on the left, or create a new one below.` +
      `</div>`
    );
  }

  root.innerHTML = (
    `<div class="lthcs-modal-backdrop" data-watchlist-close></div>` +
    `<div class="lthcs-modal-panel lthcs-watchlist-panel" role="document">` +
      `<div class="lthcs-modal-header">` +
        `<div class="lthcs-modal-title-block">` +
          `<h2 class="lthcs-modal-ticker">Watchlists</h2>` +
          `<p class="lthcs-modal-company">Up to ${MAX_WATCHLISTS} named lists. Invalid tickers are flagged but not blocked.</p>` +
        `</div>` +
        `<button type="button" class="lthcs-modal-close" data-watchlist-close aria-label="Close">&times;</button>` +
      `</div>` +
      `<div class="lthcs-watchlist-body">` +
        `<div class="lthcs-watchlist-list-pane">` +
          `<ul class="lthcs-watchlist-list" role="list">${listRowsHtml}</ul>` +
          newInputHtml +
        `</div>` +
        `<div class="lthcs-watchlist-edit-pane">${editorHtml}</div>` +
      `</div>` +
      `<div class="lthcs-watchlist-footer">` +
        `<button type="button" class="lthcs-watchlist-cancel" id="lthcs-watchlist-cancel">Cancel</button>` +
        `<button type="button" class="lthcs-watchlist-save" id="lthcs-watchlist-save"${selectedName ? '' : ' disabled'}>Save</button>` +
      `</div>` +
    `</div>`
  );
  wireModalEvents();
}

function wireModalEvents() {
  const root = modalRoot();
  if (!root) return;

  // Close (X / backdrop / Cancel).
  root.querySelectorAll('[data-watchlist-close]').forEach((el) => {
    el.addEventListener('click', closeModal);
  });
  const cancelBtn = document.getElementById('lthcs-watchlist-cancel');
  if (cancelBtn) cancelBtn.addEventListener('click', closeModal);

  // Select a row (left pane).
  root.querySelectorAll('[data-select-name]').forEach((el) => {
    el.addEventListener('click', () => {
      const name = el.getAttribute('data-select-name');
      if (!name || !state.lists[name]) return;
      modal.draftName = name;
      modal.draftText = (state.lists[name] || []).join('\n');
      renderModal();
    });
  });

  // Delete a row.
  root.querySelectorAll('[data-delete-name]').forEach((el) => {
    el.addEventListener('click', (e) => {
      e.stopPropagation();
      const name = el.getAttribute('data-delete-name');
      if (!name || !state.lists[name]) return;
      if (!window.confirm(`Delete watchlist "${name}"?`)) return;
      delete state.lists[name];
      saveLists();
      // If we deleted the active or draft selection, clear those references.
      if (state.activeName === name) {
        state.activeName = null;
        saveActive();
      }
      if (modal.draftName === name) {
        const remaining = listNames();
        modal.draftName = remaining[0] || null;
        modal.draftText = modal.draftName ? (state.lists[modal.draftName] || []).join('\n') : '';
      }
      renderModal();
      renderChips();
      state.onChange();
    });
  });

  // New-watchlist Create button.
  const newBtn = document.getElementById('lthcs-watchlist-new-btn');
  const newInput = document.getElementById('lthcs-watchlist-new-name');
  const tryCreate = () => {
    if (!newInput) return;
    const raw = (newInput.value || '').trim().slice(0, MAX_NAME_LEN);
    if (!raw) return;
    if (state.lists[raw]) {
      window.alert(`A watchlist named "${raw}" already exists.`);
      return;
    }
    if (listNames().length >= MAX_WATCHLISTS) return;
    state.lists[raw] = [];
    saveLists();
    modal.draftName = raw;
    modal.draftText = '';
    renderModal();
    renderChips();
  };
  if (newBtn) newBtn.addEventListener('click', tryCreate);
  if (newInput) {
    newInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        tryCreate();
      }
    });
  }

  // Textarea — buffer in modal.draftText so validity re-renders without
  // losing focus. We re-render on blur (cheap and lossless for visible
  // counts) but keep keystrokes in the buffer so Save reads fresh content.
  const textarea = document.getElementById('lthcs-watchlist-textarea');
  if (textarea) {
    textarea.addEventListener('input', (e) => {
      modal.draftText = e.target.value || '';
    });
    textarea.addEventListener('blur', () => {
      renderModal();
      // Re-focus the textarea after re-render so blur doesn't trap the user.
      setTimeout(() => {
        const t = document.getElementById('lthcs-watchlist-textarea');
        if (t) {
          t.focus();
          // Move caret to end.
          const v = t.value || '';
          t.setSelectionRange(v.length, v.length);
        }
      }, 0);
    });
  }

  // Rename input (selected list).
  const renameInput = document.getElementById('lthcs-watchlist-rename');
  if (renameInput) {
    renameInput.addEventListener('input', (e) => {
      modal.pendingRename = {
        from: modal.draftName,
        to: (e.target.value || '').trim().slice(0, MAX_NAME_LEN),
      };
    });
  }

  // Save — commits draftText (parsed) + rename (if any) to the selected list.
  const saveBtn = document.getElementById('lthcs-watchlist-save');
  if (saveBtn) saveBtn.addEventListener('click', () => {
    if (!modal.draftName || !state.lists[modal.draftName]) return;
    let name = modal.draftName;

    // Apply rename first (if changed and not a collision).
    if (modal.pendingRename && modal.pendingRename.to && modal.pendingRename.to !== name) {
      const newName = modal.pendingRename.to;
      if (state.lists[newName]) {
        window.alert(`A watchlist named "${newName}" already exists. Save aborted.`);
        return;
      }
      // Reinsert under new key but preserve insertion order roughly by
      // rebuilding the object (object key order is insertion-order in modern engines).
      const next = {};
      for (const k of Object.keys(state.lists)) {
        if (k === name) next[newName] = state.lists[k];
        else next[k] = state.lists[k];
      }
      state.lists = next;
      if (state.activeName === name) {
        state.activeName = newName;
        saveActive();
      }
      name = newName;
      modal.draftName = newName;
    }

    const tickers = parseTickersBlob(modal.draftText);
    state.lists[name] = tickers;
    saveLists();
    modal.pendingRename = null;
    renderChips();
    state.onChange();
    closeModal();
  });

  // ESC to close.
  root.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeModal();
  });
}

// ---------------------------------------------------------------------------
// Public chip + warning event wiring (called once at init)
// ---------------------------------------------------------------------------

function wireChipRow() {
  const host = document.getElementById('lthcs-watchlist-chips');
  if (!host) return;
  host.addEventListener('click', (e) => {
    const manageBtn = e.target.closest('#lthcs-watchlist-manage-btn');
    if (manageBtn) {
      openModal(state.activeName || null);
      return;
    }
    const chip = e.target.closest('[data-watchlist-name]');
    if (!chip) return;
    const name = chip.getAttribute('data-watchlist-name');
    if (!name) return;
    // Toggle: clicking the active chip clears the watchlist filter.
    if (state.activeName === name) {
      state.activeName = null;
    } else if (state.lists[name]) {
      state.activeName = name;
    } else {
      return;
    }
    saveActive();
    renderChips();
    state.onChange();
  });
}

function wireWarningRow() {
  const host = document.getElementById('lthcs-watchlist-warning');
  if (!host) return;
  host.addEventListener('click', (e) => {
    const undoBtn = e.target.closest('#lthcs-watchlist-undo');
    if (undoBtn) {
      applyUndo();
      renderChips();
    }
  });
}

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

export function initWatchlists({ onChange, getUniverseTickers } = {}) {
  state.onChange = typeof onChange === 'function' ? onChange : () => {};
  const universeFn = typeof getUniverseTickers === 'function' ? getUniverseTickers : () => [];

  // Load persisted lists.
  const loaded = loadLists();
  if (loaded && Object.keys(loaded).length) {
    state.lists = loaded;
  } else {
    // First run: seed the default empty list per spec.
    state.lists = { [DEFAULT_LIST_NAME]: [] };
    saveLists();
  }

  // Restore active selection.
  const persistedActive = loadActive();
  if (persistedActive && state.lists[persistedActive]) {
    state.activeName = persistedActive;
  } else {
    state.activeName = null;
  }

  // Universe set is rebuilt by the caller; refresh on each onChange tick.
  refreshUniverse(universeFn);

  // Phase 5 #3: activate shared-watchlist from URL BEFORE first paint, so
  // the chips reflect the shared selection even if there's a persisted
  // activeName in localStorage.
  activateSharedFromUrl();

  // First paint.
  renderChips();
  renderSharedBanner();
  wireChipRow();
  wireWarningRow();
  startShareObserver();

  // Expose a refresh hook so lthcs-tab.js can re-sync universe + warning
  // after the snapshot loads. (Universe is empty during init since the
  // fetch is async.)
  return {
    onUniverseReady: () => {
      refreshUniverse(universeFn);
      warnIfStale();
    },
  };
}

function refreshUniverse(universeFn) {
  try {
    const list = universeFn() || [];
    state.universeTickers = new Set(
      list
        .filter((t) => typeof t === 'string')
        .map((t) => t.trim().toUpperCase())
    );
  } catch (err) {
    console.warn('LTHCS: watchlist universe refresh failed', err);
    state.universeTickers = new Set();
  }
}

// ---------------------------------------------------------------------------
// Phase 5 #3: shared-watchlist (URL ?watchlist=...) support
// ---------------------------------------------------------------------------
//
// When the user lands on /lthcs/?watchlist=AAPL,MSFT,...  we treat that as an
// EPHEMERAL watchlist: a virtual "Shared watchlist" entry that filters cards
// but is NOT persisted to localStorage. Until the user clicks "Save to my
// watchlists" (in the shared-watchlist banner), refreshing the page without
// the query param drops the entry.

const SHARED_LIST_NAME = 'Shared watchlist';
const sharedState = {
  active: false,           // true once we've parsed a ?watchlist= param this session
  tickers: [],             // uppercase, deduped
  invalidTickers: [],      // tickers that don't match TICKER_RE (warning only)
};

// Parse a watchlist URL param into [valid-tickers, invalid-tokens].
function parseSharedParam(raw) {
  if (typeof raw !== 'string' || !raw) return [[], []];
  // Accept comma-separated, semicolon-separated, or whitespace-separated.
  const tokens = raw
    .split(/[\s,;]+/)
    .map((t) => t.trim().toUpperCase())
    .filter((t) => t.length);
  const valid = [];
  const invalid = [];
  const seen = new Set();
  for (const t of tokens) {
    if (seen.has(t)) continue;
    seen.add(t);
    if (TICKER_RE.test(t)) valid.push(t);
    else invalid.push(t);
  }
  return [valid, invalid];
}

// Returns true if a shared watchlist is currently active for this session.
export function sharedIsActive() {
  return sharedState.active;
}

// Returns the ticker set when a shared watchlist is active, else null. This
// is merged into getActiveTickerSet() so the existing filter pipe just works.
export function getSharedTickerSet() {
  if (!sharedState.active) return null;
  return new Set(sharedState.tickers);
}

function renderSharedBanner() {
  const host = document.getElementById('lthcs-shared-watchlist');
  if (!host) return;
  if (!sharedState.active) {
    host.classList.add('hidden');
    host.innerHTML = '';
    return;
  }
  const count = sharedState.tickers.length;
  const invalidHtml = sharedState.invalidTickers.length
    ? `<div class="lthcs-shared-warning">Skipped (invalid): ${escapeHtml(sharedState.invalidTickers.join(', '))}</div>`
    : '';
  const atCap = listNames().length >= MAX_WATCHLISTS;
  const saveBtnHtml = atCap
    ? `<button type="button" class="lthcs-shared-save" disabled title="Watchlist cap reached">Save to my watchlists</button>`
    : `<button type="button" class="lthcs-shared-save" id="lthcs-shared-save">Save to my watchlists</button>`;
  host.classList.remove('hidden');
  host.innerHTML = (
    `<div class="lthcs-shared-bar">` +
      `<div class="lthcs-shared-title">` +
        `<span class="lthcs-shared-eyebrow">Shared watchlist</span>` +
        `<span class="lthcs-shared-count">${count} ticker${count === 1 ? '' : 's'}</span>` +
      `</div>` +
      `<div class="lthcs-shared-actions">` +
        saveBtnHtml +
        `<button type="button" class="lthcs-shared-dismiss" id="lthcs-shared-dismiss" aria-label="Dismiss shared watchlist">&times;</button>` +
      `</div>` +
    `</div>` +
    invalidHtml
  );

  const saveBtn = document.getElementById('lthcs-shared-save');
  if (saveBtn) saveBtn.addEventListener('click', () => {
    if (listNames().length >= MAX_WATCHLISTS) {
      window.alert(`Watchlist cap (${MAX_WATCHLISTS}) reached. Delete one before saving.`);
      return;
    }
    // Prompt for a name; default to "Shared <date>".
    const defaultName = `Shared (${new Date().toISOString().slice(0, 10)})`;
    let name = (window.prompt('Name this watchlist:', defaultName) || '').trim().slice(0, MAX_NAME_LEN);
    if (!name) return;
    // Suffix collisions: "name (2)", "name (3)", ...
    if (state.lists[name]) {
      let i = 2;
      while (state.lists[`${name} (${i})`]) i++;
      name = `${name} (${i})`;
    }
    state.lists[name] = sharedState.tickers.slice();
    saveLists();
    state.activeName = name;
    saveActive();
    sharedState.active = false;
    sharedState.tickers = [];
    sharedState.invalidTickers = [];
    renderSharedBanner();
    renderChips();
    state.onChange();
  });
  const dismissBtn = document.getElementById('lthcs-shared-dismiss');
  if (dismissBtn) dismissBtn.addEventListener('click', () => {
    sharedState.active = false;
    sharedState.tickers = [];
    sharedState.invalidTickers = [];
    // Clean URL too (drop ?watchlist=...).
    try {
      const url = new URL(window.location.href);
      url.searchParams.delete('watchlist');
      window.history.replaceState({}, '', url.toString());
    } catch { /* ignore */ }
    renderSharedBanner();
    state.onChange();
  });
}

// Called from initWatchlists() to activate the shared list on page load.
function activateSharedFromUrl() {
  try {
    const params = new URLSearchParams(window.location.search);
    const raw = params.get('watchlist');
    if (!raw) return;
    const [valid, invalid] = parseSharedParam(raw);
    if (!valid.length && !invalid.length) return;
    sharedState.active = true;
    sharedState.tickers = valid;
    sharedState.invalidTickers = invalid;
    // We do NOT mutate state.activeName here — shared is layered on top of
    // the persisted activeName via getEffectiveTickerSet(). Dismissing the
    // shared banner restores the previously-saved selection automatically.
  } catch (err) {
    console.warn('LTHCS: shared-watchlist URL parse failed', err);
  }
}

// ---------------------------------------------------------------------------
// Phase 5 #10: bookmark a band as a new watchlist
// ---------------------------------------------------------------------------

// Public helper used by lthcs-tab.js when the user clicks a band bookmark
// icon. Returns { ok, name, count, reason }.
//   - ok=true  → watchlist was created (or would be — see noPersist flag).
//   - ok=false → reason is one of 'empty' | 'cap' | 'invalid'.
// Names auto-suffix (2), (3), ... on collision. The new list is NOT made
// active automatically; the caller's toast just confirms creation.
export function createWatchlistFromTickers(name, tickers) {
  if (!name || typeof name !== 'string') return { ok: false, reason: 'invalid' };
  const arr = (Array.isArray(tickers) ? tickers : [])
    .map((t) => (typeof t === 'string' ? t.trim().toUpperCase() : ''))
    .filter((t) => t.length && TICKER_RE.test(t));
  if (!arr.length) return { ok: false, reason: 'empty', name };
  if (listNames().length >= MAX_WATCHLISTS) {
    return { ok: false, reason: 'cap', name };
  }
  let finalName = name.slice(0, MAX_NAME_LEN);
  if (state.lists[finalName]) {
    let i = 2;
    while (state.lists[`${finalName} (${i})`]) i++;
    finalName = `${finalName} (${i})`;
  }
  state.lists[finalName] = arr;
  saveLists();
  renderChips();
  state.onChange();
  return { ok: true, name: finalName, count: arr.length };
}

// ---------------------------------------------------------------------------
// Phase 5 #3: Share button — patch the modal renderer
// ---------------------------------------------------------------------------

// Helper: build the share URL for a given watchlist.
function shareUrlFor(name) {
  const tickers = (state.lists[name] || []).slice();
  // Build an absolute URL pointing at the /lthcs/ root. Strip any query
  // string from window.location so we always emit a clean shareable link.
  const base = `${window.location.origin}${window.location.pathname}`;
  return `${base}?watchlist=${encodeURIComponent(tickers.join(','))}`;
}

// Copy a string to the clipboard. Best-effort with execCommand fallback.
async function copyToClipboard(text) {
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch { /* fall through */ }
  try {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand('copy');
    document.body.removeChild(ta);
    return !!ok;
  } catch {
    return false;
  }
}

// Show a transient toast (#lthcs-toast). 2s default. Module-local so it
// works whether or not lthcs-tab.js has wired its own toast helper.
function flashToast(message, ms = 2000) {
  const el = document.getElementById('lthcs-toast');
  if (!el) return;
  el.textContent = message;
  el.classList.remove('hidden');
  el.classList.add('is-visible');
  // Clear any prior timer.
  if (flashToast._t) clearTimeout(flashToast._t);
  flashToast._t = setTimeout(() => {
    el.classList.remove('is-visible');
    el.classList.add('hidden');
  }, ms);
}

// Patch renderModal to inject a Share button. We monkey-patch via post-
// render DOM enrichment so the existing renderer logic doesn't fork.
function injectShareButton() {
  const root = modalRoot();
  if (!root) return;
  const footer = root.querySelector('.lthcs-watchlist-footer');
  if (!footer) return;
  if (footer.querySelector('#lthcs-watchlist-share')) return;
  const selectedName = modal.draftName;
  if (!selectedName) return;
  const list = state.lists[selectedName] || [];
  const disabled = list.length === 0 ? ' disabled title="No tickers to share"' : '';
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.id = 'lthcs-watchlist-share';
  btn.className = 'lthcs-watchlist-share';
  btn.textContent = 'Share';
  if (disabled) btn.setAttribute('disabled', '');
  // Insert before Cancel/Save so Share sits on the left of the action group.
  footer.insertBefore(btn, footer.firstChild);
  btn.addEventListener('click', async () => {
    if (!modal.draftName) return;
    // Use the persisted list (not the draft text) so users don't share
    // unsaved edits unintentionally.
    const tickers = state.lists[modal.draftName] || [];
    if (!tickers.length) return;
    const url = shareUrlFor(modal.draftName);
    const ok = await copyToClipboard(url);
    if (ok) flashToast(`Link copied (${tickers.length} ticker${tickers.length === 1 ? '' : 's'})`);
    else window.prompt('Copy this link:', url);
  });
}

// Wrap renderModal via MutationObserver: every time the modal innerHTML is
// repainted, ensure a Share button is present in the footer. This avoids
// reassigning the function-declared renderModal (which would error in strict
// mode) and keeps the patch additive.
function startShareObserver() {
  const root = modalRoot();
  if (!root || startShareObserver._wired) return;
  startShareObserver._wired = true;
  const obs = new MutationObserver(() => {
    if (root.classList.contains('hidden')) return;
    injectShareButton();
  });
  obs.observe(root, { childList: true, subtree: true });
}

// ---------------------------------------------------------------------------
// Init augmentation — wire shared-list activation + chip merge
// ---------------------------------------------------------------------------

// Wrap initWatchlists to ALSO activate shared-from-URL state. We can't reassign
// the exported binding, so this is done via a side effect within initWatchlists
// itself — see the patched body below. Instead, expose an extra hook.

export function activateSharedWatchlist() {
  activateSharedFromUrl();
  renderSharedBanner();
}

// Merge: when a shared watchlist is active, getActiveTickerSet() should
// return the shared set rather than null. We monkey-patch the existing
// export by re-defining the function reference inside the module — but
// since ES exports are bindings, we instead extend the function behavior
// through a wrapper getter exposed to lthcs-tab.js.

export function getEffectiveTickerSet() {
  if (sharedState.active) return new Set(sharedState.tickers);
  return getActiveTickerSet();
}

export function getEffectiveName() {
  if (sharedState.active) return SHARED_LIST_NAME;
  return getActiveName();
}

export function effectiveIsEmpty() {
  if (sharedState.active) return sharedState.tickers.length === 0;
  return activeIsEmpty();
}
