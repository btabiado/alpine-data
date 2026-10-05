// LTHCS — Index Exiles (shared by /lthcs/, /lthcs/table/, /lthcs/v2/,
// /lthcs/heatmap/ and /lthcs/leaderboards/).
//
// Owner's rule: a ticker that was in a tracked index (S&P 500, S&P 100,
// NASDAQ-100, DJIA) and has left all of them stays in the universe, is
// scored daily, and is grouped as an "Index Exile". universe.json marks it
// with `index_exile` and leaves `index_membership` empty, so exiles never
// count toward an index filter, index count or index aggregate; they only
// show up under the separate "Index Exiles" filter.

// Filter key used by every page's index filter for the exile group.
export const EXILE_FILTER_KEY = 'exiles';
export const EXILE_LABEL = 'Index Exiles';

// The exile marker of a universe entry, or null. Inactive (delisted) names
// are not exiles: they are not scored any more.
export function exileInfo(uni) {
  if (!uni || uni.active === false) return null;
  const m = uni.index_exile;
  if (!m || typeof m !== 'object') return null;
  const former = Array.isArray(m.former_indexes) ? m.former_indexes.filter(Boolean) : [];
  const drops = Array.isArray(m.drops) ? m.drops : [];
  return {
    formerIndexes: former,
    droppedOn: typeof m.dropped_on === 'string' && m.dropped_on ? m.dropped_on : null,
    drops: drops
      .filter((d) => d && d.index)
      .map((d) => ({ index: String(d.index), droppedOn: d.dropped_on || null })),
  };
}

function joinNames(names) {
  if (names.length <= 1) return names.join('');
  return `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`;
}

// Tooltip text: "Left NASDAQ-100 on 2026-01-20; still scored daily".
// Several indexes with their own dates read "Left NASDAQ-100 on 2025-12-22
// and S&P 500 on 2026-09-21; ...". A date the sources do not establish is
// never invented: "Left X (date not established); ...".
export function exileTooltip(info) {
  if (!info) return '';
  const dated = info.drops.length
    ? info.drops
      .slice()
      .sort((a, b) => String(a.droppedOn || '').localeCompare(String(b.droppedOn || '')))
      .map((d) => (d.droppedOn ? `${d.index} on ${d.droppedOn}` : `${d.index} (date not established)`))
    : null;
  let left;
  if (dated) {
    left = joinNames(dated);
  } else {
    const names = joinNames(info.formerIndexes.length ? info.formerIndexes : ['its index']);
    left = info.droppedOn ? `${names} on ${info.droppedOn}` : `${names} (date not established)`;
  }
  return `Left ${left}; still scored daily`;
}

function escapeAttr(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

// Small "Exile" badge. `cls` lets each page add its own sizing class.
export function exileBadgeHTML(info, cls = '') {
  if (!info) return '';
  const tip = escapeAttr(exileTooltip(info));
  const klass = `lthcs-exile-badge${cls ? ` ${cls}` : ''}`;
  return `<span class="${klass}" data-exile="1" title="${tip}" aria-label="Index exile: ${tip}">Exile</span>`;
}
