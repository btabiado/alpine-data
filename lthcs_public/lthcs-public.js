/* =========================================================================
   LTHCS Public Data Index — tiny manifest-driven landing page.

   Reads ../data/lthcs/public/manifest.json (built daily by
   scripts/lthcs_build_public_manifest.py) and renders:
     - header snapshot date
     - intro meta row (model version, universe sizes, pillars)
     - endpoints table (one row per data_endpoints[] entry)
     - license / disclaimer text

   No framework. No state. Just fetch → render once.
   ========================================================================= */

const MANIFEST_URL = '../data/lthcs/public/manifest.json';

function $(id) {
  return document.getElementById(id);
}

function setText(id, text) {
  const node = $(id);
  if (node) node.textContent = text;
}

function show(id) {
  const node = $(id);
  if (node) node.classList.remove('hidden');
}

function hide(id) {
  const node = $(id);
  if (node) node.classList.add('hidden');
}

function renderEndpoints(rows) {
  const body = $('lpub-endpoints-body');
  if (!body) return;
  // Defensive: clear any prior render so re-runs are idempotent.
  body.textContent = '';

  for (const row of rows || []) {
    const tr = document.createElement('tr');

    const endpointTd = document.createElement('td');
    endpointTd.className = 'lpub-endpoint-cell';
    const endpoint = row.endpoint || '';
    // Only render a clickable link if the path has no <PLACEHOLDER> tokens —
    // /<TICKER>/ or /<RUN_ID>/ links would 404. Templated paths still show
    // as code so consumers can copy + substitute.
    if (endpoint.indexOf('<') === -1 && endpoint.startsWith('/')) {
      const a = document.createElement('a');
      a.href = '..' + endpoint; // /data/lthcs/... -> ../data/lthcs/...
      a.textContent = endpoint;
      endpointTd.appendChild(a);
    } else {
      const code = document.createElement('code');
      code.textContent = endpoint;
      endpointTd.appendChild(code);
    }
    tr.appendChild(endpointTd);

    const descTd = document.createElement('td');
    descTd.textContent = row.description || '';
    tr.appendChild(descTd);

    const shapeTd = document.createElement('td');
    shapeTd.className = 'lpub-shape-cell';
    shapeTd.textContent = row.shape || '';
    tr.appendChild(shapeTd);

    body.appendChild(tr);
  }

  hide('lpub-loading');
  show('lpub-endpoints-table');
}

function renderError(message) {
  hide('lpub-loading');
  hide('lpub-endpoints-table');
  const node = $('lpub-error');
  if (!node) return;
  node.textContent = message;
  node.classList.remove('hidden');
}

function renderManifest(m) {
  // Header + intro meta.
  setText('lpub-snapshot-date', m.latest_snapshot_date || '—');
  setText('lpub-version', m.version || '—');
  setText('lpub-univ-eq', String(m.universe_size ?? '—'));
  setText('lpub-univ-cx', String(m.crypto_universe_size ?? '—'));
  setText('lpub-pillars', Array.isArray(m.pillars) ? m.pillars.join(', ') : '—');
  setText('lpub-license-text', m.license || '');

  renderEndpoints(m.data_endpoints || []);
}

async function load() {
  try {
    const resp = await fetch(MANIFEST_URL, { cache: 'no-store' });
    if (!resp.ok) {
      throw new Error(`HTTP ${resp.status} fetching ${MANIFEST_URL}`);
    }
    const m = await resp.json();
    renderManifest(m);
  } catch (err) {
    renderError(
      `Could not load manifest: ${err && err.message ? err.message : err}. ` +
        'The site may be mid-deploy — try again in a minute.'
    );
  }
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', load);
} else {
  load();
}
