/* =========================================================================
   LTHCS file index — "which files exist?" without guessing URLs.

   GitHub Pages cannot list a directory, so pages used to discover files by
   requesting them and watching for 404s (pipeline.html: 285 HEAD probes;
   v2: today's sector_strength file, which hasn't been produced since May).
   scripts/build_lthcs_site_index.py now writes data/lthcs/file_index.json at
   deploy time; this module reads it once per page and answers lookups.

   Every helper degrades to "unknown" (null) when the index is missing — on
   a local dev server, or if the build step failed — and callers keep their
   previous behaviour in that case.

   The URL is resolved against this module, not the page: lthcs_tab/ is
   staged both at /lthcs/ and at /lthcs/lthcs_tab/ (for the subpages), and
   scripts/stage_lthcs_site.py gives the second copy one more '../', so both
   resolve to the site's single /data/lthcs/ copy.
   ========================================================================= */

const INDEX_URL = new URL('../data/lthcs/file_index.json', import.meta.url).href;
let indexPromise = null;

export function loadFileIndex() {
  if (!indexPromise) {
    indexPromise = fetch(INDEX_URL, { cache: 'no-cache' })
      .then((r) => (r.ok ? r.json() : null))
      .then((j) => (j && j.dated ? j : null))
      .catch(() => null);
  }
  return indexPromise;
}

// Newest-first list of file-name dates for a key such as 'snapshots_crypto'
// or 'macro/sector_strength'. null = index unavailable (unknown), [] = none.
export async function datesFor(key) {
  const idx = await loadFileIndex();
  if (!idx) return null;
  const e = idx.dated[key];
  return e && Array.isArray(e.dates) ? e.dates : [];
}

export async function latestDate(key) {
  const d = await datesFor(key);
  if (d == null) return null;
  return d.length ? d[0] : '';
}

// true/false when the index knows, null when it doesn't.
export async function hasDated(key, date) {
  const d = await datesFor(key);
  if (d == null) return null;
  return d.indexOf(date) !== -1;
}

export async function latestWeek(key) {
  const idx = await loadFileIndex();
  if (!idx) return null;
  const e = (idx.weekly || {})[key];
  return e && e.latest ? e.latest : '';
}

// Is <relPath> (relative to data/lthcs/backtest/) on disk? null = unknown.
export async function hasBacktestFile(relPath) {
  const idx = await loadFileIndex();
  if (!idx || !Array.isArray(idx.backtest_files)) return null;
  return idx.backtest_files.indexOf(relPath) !== -1;
}

export async function exists(relPath) {
  const idx = await loadFileIndex();
  if (!idx || !idx.exists || !(relPath in idx.exists)) return null;
  return !!idx.exists[relPath];
}

// Sector strength is an optional pipeline stage that has not run daily since
// May, so asking for `sector_strength_<today>.json` 404s and the panel said
// "Sector data unavailable". Use the newest file that actually exists (and
// has sectors in it), never one dated after `onOrBefore`. Returns
// { data, date } — date is the FILE's date so the caller can show its age —
// or null. Without an index, falls back to the single dated request.
export async function fetchLatestSectorStrength(macroBase, onOrBefore, fetchJSON) {
  const get = fetchJSON || (async (u) => {
    try { const r = await fetch(u, { cache: 'no-cache' }); return r.ok ? await r.json() : null; } catch { return null; }
  });
  const dates = await datesFor('macro/sector_strength');
  if (dates == null) {
    if (!onOrBefore) return null;
    const data = await get(`${macroBase}/sector_strength_${onOrBefore}.json`);
    return data ? { data, date: onOrBefore } : null;
  }
  const candidates = dates.filter((d) => !onOrBefore || d <= onOrBefore).slice(0, 3);
  for (const d of candidates) {
    const data = await get(`${macroBase}/sector_strength_${d}.json`);
    if (data && data.sectors && Object.keys(data.sectors).length) return { data, date: d };
  }
  return null;
}
